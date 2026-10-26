"""The tool registry's failure contract.

Nothing here is about a particular tool.  The registry is the loop's vocabulary
for tools, and the one thing it promises is that :meth:`ToolRegistry.execute`
**never raises a tool failure**: an unknown name, bad arguments, and a tool that
throws all come back as text with `ok` false — because the model reads the
result and corrects itself, where an exception would end the turn and lose the
thread.  A *harness* failure is the exception: `UnreadableAnswer` is re-raised
so the turn fails rather than the model being told a foreign hub "said no".

The tools themselves live with the server that serves them, so the tests for
what they *do* are next to it.  See `tests/test_skills_server.py`.
"""

from __future__ import annotations

import pytest

from slife2.libs.plugin.protocol import UnreadableAnswer
from slife2.libs.turn.messages import ToolCall, ToolSpec
from slife2.libs.turn.tools import Tool, ToolFailed, ToolRegistry

pytestmark = pytest.mark.unit


def spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description="", parameters={"type": "object"})


async def _ok(_arguments: dict) -> str:
    return "fine"


async def _raises(_arguments: dict) -> str:
    raise ValueError("no")


async def _reports(_arguments: dict) -> str:
    raise ToolFailed("the peer said no")


async def _unreadable(_arguments: dict) -> str:
    raise UnreadableAnswer("the toolhub did not return a tool list")


def registry() -> ToolRegistry:
    return ToolRegistry(
        [
            Tool(spec=spec("ok"), run=_ok),
            Tool(spec=spec("raises"), run=_raises),
            Tool(spec=spec("reports"), run=_reports),
            Tool(spec=spec("unreadable"), run=_unreadable),
        ]
    )


def test_specs_are_advertised() -> None:
    assert {s.name for s in registry().specs} == {
        "ok",
        "raises",
        "reports",
        "unreadable",
    }


@pytest.mark.asyncio
async def test_execute_returns_the_result_and_ok() -> None:
    assert await registry().execute(ToolCall(id="c1", name="ok")) == ("fine", True)


@pytest.mark.asyncio
async def test_a_call_with_unreadable_arguments_is_not_dispatched() -> None:
    """**The tool is never run on arguments the model did not send.**

    The call carries its error from the hop that decoded it; `execute` answers
    that sentence and skips the lookup and the tool.  The tool picked here
    would have answered "fine", which is what proves the dispatch did not
    happen.
    """
    text, ok = await registry().execute(
        ToolCall(id="c1", name="ok", error="tool call 'ok' was not run: nope")
    )

    assert ok is False
    assert text == "Error: tool call 'ok' was not run: nope"


@pytest.mark.asyncio
async def test_unknown_tool_is_a_result_not_an_exception() -> None:
    """The error is a message the model can act on, not a dead turn.

    The way back is `tool_search` and `func_tool_load`, not a list of every
    loaded name: the list is bounded by the budget and travels in the
    conversation, and a name that is missing is usually one the budget unloaded
    rather than one that does not exist.
    """
    text, ok = await registry().execute(ToolCall(id="c1", name="wether"))
    assert ok is False
    assert "wether" in text
    assert "tool_search" in text
    assert "func_tool_load" in text
    assert "raises" not in text, "the alternatives are found, not enumerated"


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
    the whole story — see `slife2.libs.turn.tools.ToolFailed`.
    """
    text, ok = await registry().execute(ToolCall(id="c1", name="reports"))
    assert (text, ok) == ("Error: the peer said no", False)


@pytest.mark.asyncio
async def test_a_harness_failure_is_not_read_as_a_tool_that_said_no() -> None:
    """**A hub from another build is not a tool result.**  Reading
    `UnreadableAnswer` as `ok: false` would tell the model a peer refused, when
    what happened is that this build cannot read the peer — the version
    mismatch must fail the turn (SDD Appendix C)."""
    with pytest.raises(UnreadableAnswer):
        await registry().execute(ToolCall(id="c1", name="unreadable"))
