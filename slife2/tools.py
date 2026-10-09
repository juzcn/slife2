"""The tool registry: what the loop can call, and what happens when a call fails.

This is the loop's *vocabulary* for tools and nothing more — `Tool` is a spec
and a function, `ToolRegistry` looks one up and runs it, and both are unaware
that anything is served over a socket.  The tools themselves live where they are
served: `slife2.builtins` for the ones slife2 ships, and whatever server the
toolhub reaches for everything else.  That split is deliberate — `loop.py`
imports this module, and the loop must not import a server.

The registry's one interesting property is that :meth:`ToolRegistry.execute`
**never raises**.  A tool that fails produces error *text*, which is handed back
to the model as the tool's result.  That is deliberate: the model can read
"unknown tool 'wether'" and correct itself, where an exception would end the
turn and lose the conversation's thread.  The error path is a feedback channel,
not a failure mode.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from slife2.messages import ToolCall, ToolSpec

logger = logging.getLogger(__name__)

#: How a tool is implemented: arguments in, result text out.  Returning text
#: rather than raising is the convention — see the module docstring.
ToolFunc = Callable[[dict[str, Any]], Awaitable[str]]


class ToolFailed(Exception):
    """A tool that failed *without* anything going wrong inside it.

    The distinction is what the exception's name would have carried.  When a
    tool raises by itself, the class is the useful part of the message —
    `KeyError: 'e'` tells the model which argument it got wrong, and
    `ZeroDivisionError` tells it what to avoid.  But a tool that proxies another
    process has already been *told* it failed, and its own message is the whole
    story: naming this class in front of it would add a word that was never in
    the error and that no model can act on.

    So this is the one exception `ToolRegistry.execute` renders bare, and it
    exists so that "the call failed" can reach the transcript as a failure.  A
    hub-backed tool that returned error text as an ordinary result would show up
    as a success with a discouraging message under it, which is worse than
    either.
    """


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
        except ToolFailed as exc:
            logger.warning("tool %s failed: %s", call.name, exc)
            return f"Error: {exc}", False
        except Exception as exc:  # noqa: BLE001 - a tool failure is a message, never an exception
            # Includes the exception type: "KeyError: 'e'" tells the model which
            # argument it got wrong, where "tool failed" does not.
            logger.warning("tool %s raised: %s", call.name, exc)
            return f"Error: {type(exc).__name__}: {exc}", False
