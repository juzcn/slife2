"""The tools the agent loop can run, and the registry that dispatches them.

This cut ships two side-effect-free tools.  They are not meant to be useful —
they exist to prove the chain works end to end: the model asks for a call, the
loop runs it, the result goes back into the conversation, and the model answers
using it.  A tool that touched the filesystem would test the same chain while
adding a sandboxing question this cut is not ready to answer.

The registry's one interesting property is that :meth:`ToolRegistry.execute`
**never raises**.  A tool that fails produces error *text*, which is handed back
to the model as the tool's result.  That is deliberate: the model can read
"unknown tool 'wether'" and correct itself, where an exception would end the
turn and lose the conversation's thread.  The error path is a feedback channel,
not a failure mode.
"""

from __future__ import annotations

import ast
import logging
import operator
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from slife2.messages import ToolCall, ToolSpec

logger = logging.getLogger(__name__)

#: How a tool is implemented: arguments in, result text out.  Returning text
#: rather than raising is the convention — see the module docstring.
ToolFunc = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class Tool:
    """A spec the model sees, and the function behind it."""

    spec: ToolSpec
    run: ToolFunc


class ToolRegistry:
    """The loop's view of what it can call."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {t.spec.name: t for t in tools or []}

    @property
    def specs(self) -> list[ToolSpec]:
        """What to advertise to the model."""
        return [t.spec for t in self._tools.values()]

    async def execute(self, call: ToolCall) -> tuple[str, bool]:
        """Run a call, returning `(result_text, ok)`.

        Never raises.  An unknown name, bad arguments, or a tool that throws all
        come back as `ok=False` with a message a model can act on.
        """
        tool = self._tools.get(call.name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "(none)"
            return f"Error: unknown tool {call.name!r}. Available tools: {known}", False

        try:
            return await tool.run(call.arguments), True
        except Exception as exc:
            # Includes the exception type: "KeyError: 'e'" tells the model which
            # argument it got wrong, where "tool failed" does not.
            logger.warning("tool %s raised: %s", call.name, exc)
            return f"Error: {type(exc).__name__}: {exc}", False


# --- builtin tools -----------------------------------------------------------


async def _now(_arguments: dict[str, Any]) -> str:
    """Current UTC time, ISO 8601.

    Deliberately takes no timezone.  Accepting an IANA zone would mean
    `zoneinfo.ZoneInfo("Asia/Shanghai")`, which raises on a Windows machine with
    no `tzdata` package installed — so the tool would drag in a dependency to
    answer a question the model can do arithmetic on.  UTC has no such problem.
    """
    return datetime.now(UTC).isoformat()


#: Binary operators `calc` will evaluate, and the one-line reason each is here.
_BINARY_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

#: Largest exponent `calc` will evaluate.  `9**9**9` is a few keystrokes that
#: would otherwise hang the process computing an integer with millions of
#: digits; the model gets an error instead.
_MAX_EXPONENT = 1000


def evaluate(expression: str) -> float | int:
    """Evaluate an arithmetic expression safely.

    Walks the AST and permits only numbers, parentheses, and the operators in
    `_BINARY_OPS`/`_UNARY_OPS`.  `eval()` is not an option — this input comes
    from a language model, and a model is an attacker who has read the prompt.

    Raises:
        ValueError: On any expression containing something not on the list.
        ZeroDivisionError: On division by zero, which the caller renders.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"not a valid expression: {expression!r}") from exc
    return _eval_node(tree.body)


def _eval_node(node: ast.expr) -> float | int:
    match node:
        case ast.Constant(value=bool()):
            # Checked before the numeric case: `bool` is a subclass of `int`,
            # so `True + 1` would otherwise quietly evaluate to 2.
            raise ValueError("booleans are not numbers here")
        case ast.Constant(value=int() | float() as value):
            return value
        case ast.BinOp(left=left, op=op, right=right):
            handler = _BINARY_OPS.get(type(op))
            if handler is None:
                raise ValueError(f"operator not allowed: {type(op).__name__}")
            return _apply_binary(handler, _eval_node(left), _eval_node(right), op)
        case ast.UnaryOp(op=op, operand=operand):
            unary = _UNARY_OPS.get(type(op))
            if unary is None:
                raise ValueError(f"operator not allowed: {type(op).__name__}")
            return unary(_eval_node(operand))
        case _:
            raise ValueError(f"not allowed in an expression: {type(node).__name__}")


def _apply_binary(
    handler: Callable[[Any, Any], Any],
    left: float | int,
    right: float | int,
    op: ast.operator,
) -> float | int:
    """Apply an operator, bounding the one that can run away."""
    if isinstance(op, ast.Pow) and abs(right) > _MAX_EXPONENT:
        raise ValueError(f"exponent too large (limit {_MAX_EXPONENT})")
    return handler(left, right)


async def _calc(arguments: dict[str, Any]) -> str:
    """Evaluate an arithmetic expression."""
    expression = arguments.get("e") or arguments.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("expected a string expression in the 'e' argument")
    return str(evaluate(expression))


def builtin_tools() -> list[Tool]:
    """The tools this cut ships."""
    return [
        Tool(
            spec=ToolSpec(
                name="now",
                description="Current date and time in UTC, ISO 8601.",
                parameters={"type": "object", "properties": {}},
            ),
            run=_now,
        ),
        Tool(
            spec=ToolSpec(
                name="calc",
                description="Evaluate an arithmetic expression, e.g. '2 + 2 * 3'.",
                parameters={
                    "type": "object",
                    "properties": {
                        "e": {
                            "type": "string",
                            "description": "The expression to evaluate.",
                        }
                    },
                    "required": ["e"],
                },
            ),
            run=_calc,
        ),
    ]
