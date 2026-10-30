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
from slife2.libs.turn.tools import (
    CATALOGUE_MISSING,
    CATALOGUE_PRESENT,
    CATALOGUE_STARTING,
    CatalogueAnswer,
    Tool,
    ToolFailed,
    ToolRegistry,
)

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
async def test_an_unloaded_tool_is_told_to_load_not_to_search() -> None:
    """**A name the catalogue holds is not a name that does not exist.**

    The budget unloading a tool leaves a name the model has seen before, and
    making it search again for the tool it already had is a wasted turn.  The
    registry asks the catalogue (`exists`) and answers with the one step that
    helps.
    """

    async def known(_name: str) -> CatalogueAnswer:
        return CATALOGUE_PRESENT

    found = ToolRegistry([Tool(spec=spec("ok"), run=_ok)], exists=known)
    text, ok = await found.execute(ToolCall(id="c1", name="peer_agent_send_message"))

    assert ok is False
    assert "in the catalogue" in text
    assert "func_tool_load" in text
    assert "tool_search" not in text, "it is a load, not a search"


@pytest.mark.asyncio
async def test_a_name_the_catalogue_does_not_hold_is_told_to_search() -> None:
    """The other half: no row anywhere, so searching for *the name* is a dead end."""

    async def missing(_name: str) -> CatalogueAnswer:
        return CATALOGUE_MISSING

    found = ToolRegistry([Tool(spec=spec("ok"), run=_ok)], exists=missing)
    text, ok = await found.execute(ToolCall(id="c1", name="peer_agent_send_message"))

    assert ok is False
    assert "no tool named" in text
    assert "tool_search" in text
    assert "func_tool_load" in text


@pytest.mark.asyncio
async def test_a_catalogue_still_starting_is_told_to_retry() -> None:
    """**A row that is still being written is not a capability the machine lacks.**

    The hub merges one source's tools at a time, and a session's first turn can
    land inside that pass: the tool's server has answered and its row has not
    been written yet.  Answering "no such tool" there sends the model searching
    for a name it is about to have; the next move is to ask again.
    """

    async def starting(_name: str) -> CatalogueAnswer:
        return CATALOGUE_STARTING

    found = ToolRegistry([Tool(spec=spec("ok"), run=_ok)], exists=starting)
    text, ok = await found.execute(ToolCall(id="c1", name="deepseek-search__webSearch"))

    assert ok is False
    assert "not in the catalogue yet" in text
    assert "retry" in text.lower()
    assert "does not exist" not in text
    assert "tool_search" in text, "the fallback if it is still missing"


@pytest.mark.asyncio
async def test_a_catalogue_that_cannot_answer_leaves_the_both_cases_sentence() -> None:
    """A diagnostic may not turn the model's failed call into a failed turn."""

    async def broken(_name: str) -> CatalogueAnswer:
        raise RuntimeError("the hub is gone")

    found = ToolRegistry([Tool(spec=spec("ok"), run=_ok)], exists=broken)
    text, ok = await found.execute(ToolCall(id="c1", name="peer_agent_send_message"))

    assert ok is False
    assert "not in your tool list" in text


@pytest.mark.asyncio
async def test_acquire_makes_a_catalogued_tool_runnable() -> None:
    """**The harness invokes the load, and the call runs in the same turn.**

    The callback is handed the arguments the loop stripped of its meta
    parameters, and the tool it returns is the ordinary kind: the registry
    advertises it, `spec_of` answers for it, and `execute` runs it through the
    same body every listed tool has.
    """

    async def acquire(name: str, arguments: dict) -> Tool | None:
        assert (name, arguments) == ("late", {"e": "1"})
        return Tool(spec=spec("late"), run=_ok)

    found = ToolRegistry([], acquire=acquire)

    assert await found.acquire("late", {"e": "1"}) is True
    assert "late" in {one.name for one in found.specs}
    assert found.spec_of("late") is not None
    assert await found.execute(ToolCall(id="c1", name="late")) == ("fine", True)


@pytest.mark.asyncio
async def test_acquire_is_not_asked_for_a_tool_already_in_the_list() -> None:
    async def acquire(_name: str, _arguments: dict) -> Tool | None:
        raise AssertionError("a listed tool must not be acquired again")

    found = ToolRegistry([Tool(spec=spec("ok"), run=_ok)], acquire=acquire)

    assert await found.acquire("ok", {}) is True


@pytest.mark.asyncio
async def test_a_refused_acquire_leaves_the_three_case_sentence() -> None:
    """A call that does not fit its row is answered, not run — and the hint is
    the unchanged one."""

    async def acquire(_name: str, _arguments: dict) -> Tool | None:
        return None

    async def present(_name: str) -> CatalogueAnswer:
        return CATALOGUE_PRESENT

    found = ToolRegistry([], exists=present, acquire=acquire)

    assert await found.acquire("peer_agent_send_message", {"e": "1"}) is False
    text, ok = await found.execute(
        ToolCall(id="c1", name="peer_agent_send_message", arguments={"e": "1"})
    )

    assert ok is False
    assert "in the catalogue" in text
    assert "func_tool_load" in text


@pytest.mark.asyncio
async def test_an_acquire_that_fails_is_a_hint_not_a_turn_failure() -> None:
    """The call has already failed; its explanation may not fail the turn."""

    async def broken(_name: str, _arguments: dict) -> Tool | None:
        raise RuntimeError("the hub is gone")

    async def present(_name: str) -> CatalogueAnswer:
        return CATALOGUE_PRESENT

    found = ToolRegistry([], exists=present, acquire=broken)

    assert await found.acquire("peer_agent_send_message", {}) is False
    text, ok = await found.execute(ToolCall(id="c1", name="peer_agent_send_message"))
    assert ok is False
    assert "in the catalogue" in text


@pytest.mark.asyncio
async def test_unreadable_arguments_on_a_catalogued_name_keep_the_load_hint() -> None:
    """**The schema may have changed under the model's history.**

    A call whose arguments did not survive the wire cannot be checked against
    the row's current schema — so it is not run, and the model is told both why
    it did not run and the one step that puts the current schema in front of
    it.  This is the parse-error arm of the same rule `acquire` applies to a
    readable call.
    """

    async def present(_name: str) -> CatalogueAnswer:
        return CATALOGUE_PRESENT

    found = ToolRegistry([], exists=present)
    text, ok = await found.execute(
        ToolCall(id="c1", name="peer_agent_send_message", error="its arguments are not JSON")
    )

    assert ok is False
    assert "its arguments are not JSON" in text
    assert "in the catalogue" in text
    assert "func_tool_load" in text


@pytest.mark.asyncio
async def test_unreadable_arguments_on_a_known_tool_stay_the_parse_error() -> None:
    text, ok = await registry().execute(
        ToolCall(id="c1", name="ok", error="its arguments are not JSON")
    )

    assert (text, ok) == ("Error: its arguments are not JSON", False)


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
