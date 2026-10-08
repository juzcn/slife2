"""slife2-builtins — the tools slife2 ships, as an MCP server like any other.

Every tool in this system comes from a server, and these come from this one.  It
is not a special case in the hub: the hub connects to it, lists it and calls it
through exactly the code path it uses for somebody else's arxiv server, which is
the point.  A builtin that reached the model by a shortcut would be a second
mechanism, and the first thing to drift.

**Why a process rather than a function call.**  `now` and `calc` are pure and
have no credential and no config, so a hop to reach them buys nothing — and they
are here because the *list* has to have one owner (DESIGN.md §8): what the model
may call, whose tool each one is, and what to do about a name two servers both
claim are questions about the set, and a set assembled in two places disagrees
with itself.  The cost is one loopback call per model call; the alternative is a
second registry in the agent, kept in step by hand.

    builtins   the tools that ship with slife2, always configured
    tools:     other people's MCP servers, connected by the same hub
    rest-api:  other people's REST APIs, expanded into servers of the first kind

Adding a tool is one function, and one mark
-----------------------------
Decorate a plain function and it is done — `echo` below is the example, and it
is deliberately trivial.  FastMCP reads the signature for the schema the model
sees and the docstring for the description, so there is no hand-written JSON
Schema to drift from the code and no registry entry to forget.  Type the
arguments, say what it does and what each argument means, and it is a tool.

`meta=FOR_THE_MODEL` is the other half, and it is not decoration.  The hub finds
this server the way it finds every other plugin, and a plugin's tools are **not**
the model's by default — the db's `remember` and the agent's `send_message` are
served by the same mechanism, and those are exactly the tools a model must never
pick by reading descriptions (`slife2.audience`).  So a tool here is invisible to
the model until it says otherwise, and forgetting the mark costs a tool that is
merely absent rather than one that is merely dangerous.  `echo`, `now` and
`calc` each carry it because this whole server exists to hold tools the model may
call — a second builtin server would be the second mechanism this one is.

The two rules worth keeping: a tool **returns text** (raising is for a bug, and
`calc` only raises because a model asking for `1/0` should be told so), and it
must be safe to call with anything a model can invent.  `calc` is the worked
example of the second — it walks an AST rather than calling `eval`, because the
input comes from a language model and a model is an attacker who has read the
prompt.
"""

from __future__ import annotations

import ast
import logging
import operator
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastmcp import FastMCP

from slife2.audience import FOR_THE_MODEL
from slife2.config import Config, find_config_path, load
from slife2.mcp_server import (
    configure_logging,
    house_server,
    parse_serve_args,
    serve,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-builtins"

#: The config key this server's address is filed under, and the name the
#: toolhub connects to it by.
CONFIG_KEY = "builtins"

INSTRUCTIONS = (
    "Tools that ship with slife2 itself, rather than coming from a server "
    "somebody else runs. Nothing here reaches outside this machine."
)


def build_server(config: Config) -> FastMCP:  # noqa: ARG001 - house signature
    """Build the builtins server.

    Takes the config and reads none of it, which is not dead weight: every
    server in this system is built the same way, and a component that is one
    argument short of the others is a component somebody has to remember is
    different.
    """
    mcp: FastMCP = house_server(SERVER_NAME, instructions=INSTRUCTIONS)

    @mcp.tool(meta=FOR_THE_MODEL)
    def echo(text: str) -> str:
        """Say it back, unchanged.

        The smallest possible builtin, and the one to copy when adding the next:
        a plain function, a decorator, a signature FastMCP turns into the schema
        the model sees, and a docstring that becomes the tool's description.

        It is also useful for what it does — a tool that cannot fail is how you
        find out whether tool calling works at all.

        Args:
            text: Anything at all.

        Returns:
            The same text.
        """
        return text

    @mcp.tool(meta=FOR_THE_MODEL)
    def now() -> str:
        """Current date and time in UTC, ISO 8601.

        Deliberately takes no timezone.  Accepting an IANA zone would mean
        `zoneinfo.ZoneInfo("Asia/Shanghai")`, which raises on a Windows machine
        with no `tzdata` package installed — so the tool would drag in a
        dependency to answer a question the model can do arithmetic on.  UTC has
        no such problem.
        """
        return datetime.now(UTC).isoformat()

    @mcp.tool(meta=FOR_THE_MODEL)
    def calc(e: str = "", expression: str = "") -> str:
        """Evaluate an arithmetic expression, e.g. '2 + 2 * 3'.

        Args:
            e: The expression to evaluate.  Terse, because it is the common call
                and the model writes it more often.
            expression: The same argument under a longer name.  Two spellings is
                not tidiness — it is that a model which writes the long form
                should get an answer rather than an argument error.

        Returns:
            The result, as text.
        """
        given = e or expression
        if not given.strip():
            raise ValueError("expected an arithmetic expression in 'e'")
        return str(evaluate(given))

    return mcp


# --- calc's evaluator ---------------------------------------------------------

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


def main(argv: list[str] | None = None) -> int:
    args = parse_serve_args(argv, SERVER_NAME)
    configure_logging()
    config_path = find_config_path()
    config = load()

    address = config.server(CONFIG_KEY)
    logger.info(
        "serving %s on http://%s:%d%s",
        SERVER_NAME,
        args.host or address.host,
        args.port or address.port,
        address.path,
    )
    serve(
        build_server(config),
        address,
        args,
        name=SERVER_NAME,
        config_path=config_path,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
