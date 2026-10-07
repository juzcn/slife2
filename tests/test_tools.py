"""The tool registry's failure contract.

Nothing here is about a particular tool.  The registry is the loop's vocabulary
for tools, and the one thing it promises is that :meth:`ToolRegistry.execute`
**never raises**: an unknown name, bad arguments, and a tool that throws all come
back as text with `ok` false — because the model reads the result and corrects
itself, where an exception would end the turn and lose the thread.

The tools themselves live with the server that serves them, so the tests for
what they *do* are next to it.  See `tests/test_builtins.py`.
"""

from __future__ import annotations

import pytest

from slife2.messages import ToolCall, ToolSpec
from slife2.tools import Tool, ToolFailed, ToolRegistry

pytestmark = pytest.mark.unit


def spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description="", parameters={"type": "object"})


async def _ok(_arguments: dict) -> str:
    return "fine"


async def _raises(_arguments: dict) -> str:
    raise ValueError("no")


async def _reports(_arguments: dict) -> str:
    raise ToolFailed("the peer said no")


def registry() -> ToolRegistry:
    return ToolRegistry(
        [
            Tool(spec=spec("ok"), run=_ok),
            Tool(spec=spec("raises"), run=_raises),
            Tool(spec=spec("reports"), run=_reports),
        ]
    )


def test_specs_are_advertised() -> None:
    assert {s.name for s in registry().specs} == {"ok", "raises", "reports"}


@pytest.mark.asyncio
async def test_execute_returns_the_result_and_ok() -> None:
    assert await registry().execute(ToolCall(id="c1", name="ok")) == ("fine", True)


@pytest.mark.asyncio
async def test_unknown_tool_is_a_result_not_an_exception() -> None:
    """The error is a message the model can act on, not a dead turn."""
    text, ok = await registry().execute(ToolCall(id="c1", name="wether"))
    assert ok is False
    assert "wether" in text
    assert "ok" in text  # names the alternatives


@pytest.mark.asyncio
async def test_a_raising_tool_becomes_an_error_result() -> None:
    text, ok = await registry().execute(ToolCall(id="c1", name="raises"))
    assert ok is False
    # The exception class is the useful part of the message here: "KeyError: 'e'"
    # tells the model which argument it got wrong, where "tool failed" does not.
    assert "ValueError" in text


@pytest.mark.asyncio
async def test_a_tool_that_knows_it_failed_is_rendered_bare() -> None:
    """`ToolFailed` is the one exception whose class name adds nothing.

    A hub-backed tool has already been told why it failed, and that message is
    the whole story — see `slife2.tools.ToolFailed`.
    """
    text, ok = await registry().execute(ToolCall(id="c1", name="reports"))
    assert (text, ok) == ("Error: the peer said no", False)
