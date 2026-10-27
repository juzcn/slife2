"""The three per-call meta-parameters, and the background they open onto.

One file for `_timeout`, `_async` and `_approve`, because they are one
mechanism: three names the loop reads off every call and strips before dispatch
(`slife2.libs.turn.tools`), and the behaviours they select — who enforces a deadline, where
a result goes, who is asked before a call runs.

The assertions are the mechanism's own rules: garbage `_timeout` must not kill a
turn, `0` never means "no bound" and never reaches a tool as an instant kill, a
tool that declares its own `timeout` is never wrapped, and a denied call runs
nothing and mounts nothing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fakes import FakeBackend, ListObserver, ScriptedTurn, text_turn

import slife2.libs.harness.async_tasks as async_tasks
from slife2.libs.harness.async_tasks import (
    CANCELLED,
    CHECK_ASYNC,
    FINISHED,
    MARKER_PREFIX,
    RUNNING,
    cancel_async,
    channel_for,
    check_async,
    marker,
)
from slife2.libs.turn.events import ToolCallFinished, ToolCallStarted
from slife2.libs.turn.loop import BACKGROUND_UNAVAILABLE, DENIED, AgentLoop
from slife2.libs.turn.messages import Message, StreamChatResult, ToolCall, ToolSpec
from slife2.libs.turn.tools import (
    Tool,
    ToolFailed,
    ToolRegistry,
    bind_timeout,
    read_timeout,
)

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────


def a_tool(name: str, run, **properties: Any) -> Tool:
    """One tool, with whatever `properties` its schema should declare.

    A `timeout` property is what makes a tool "native" to the loop
    (`slife2.libs.turn.tools.declares_timeout`), so the tests that care say so here rather
    than in a second helper.
    """
    parameters: dict[str, Any] = {"type": "object"}
    if properties:
        parameters["properties"] = properties
    return Tool(
        spec=ToolSpec(name=name, description="", parameters=parameters), run=run
    )


async def collected(_text: str, _channel: str) -> bool:
    """A hand-back that goes nowhere — for tests about the *other* half of `_async`."""
    return True


def tool_turn(*calls: ToolCall) -> ScriptedTurn:
    """A scripted response that asks for tools, then stops."""
    return ScriptedTurn(
        result=StreamChatResult(tool_calls=tuple(calls), stop_reason="tool_calls")
    )


def one(call: ToolCall, tools: list[Tool], **kwargs: Any) -> AgentLoop:
    """A loop that calls `call` and then answers — the shortest turn there is."""
    return AgentLoop(
        FakeBackend(tool_turn(call), text_turn("done")),
        ToolRegistry(tools),
        **kwargs,
    )


def tool_message(messages: list[Message]) -> str:
    """The text the model was handed for the call this turn made.

    The *tool* message, not the last one: a turn that called a tool has a second
    model call after it, so the list ends with the assistant's answer.
    """
    for message in reversed(messages):
        if message.role == "tool":
            return str(message.content or "")
    raise AssertionError("no tool message in this turn")


def marker_fields(text: str) -> dict[str, Any]:
    """The JSON out of the marker a background result wears."""
    assert text.startswith(MARKER_PREFIX), text
    return json.loads(text[len(MARKER_PREFIX) : text.index("]")])


@pytest.fixture(autouse=True)
def empty_registry():
    """A process-wide registry, emptied around every test.

    `slife2.libs.harness.async_tasks` is the process's — that is the design — so a test that
    leaves a task behind would be a task the next test can see under an id it
    never made.
    """
    async_tasks._tasks.clear()  # noqa: SLF001 - the registry is the thing under test
    yield
    async_tasks._tasks.clear()  # noqa: SLF001


# ── `_timeout` ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_timeout_bounds_a_call_that_has_no_bound_of_its_own() -> None:
    """The number the model asked for is the number that fires."""
    never = asyncio.Event()

    async def hangs(_arguments: dict[str, Any]) -> str:
        await never.wait()
        return "never"

    call = ToolCall(id="1", name="hangs", arguments={"_timeout": 0.05})
    messages: list[Message] = []
    await one(call, [a_tool("hangs", hangs)]).run_turn(messages, "hi")

    assert tool_message(messages) == "Error: Tool 'hangs' timed out (0.05s)."


@pytest.mark.asyncio
async def test_an_unreadable_timeout_is_reported_and_the_tool_never_runs() -> None:
    """**A bound nobody can read is not "no bound".**  The call is the answer.

    It used to run the tool anyway, unbounded, with a debug line nobody reads —
    a number the model wrote, silently ignored, and a call that could outlive
    anything the model meant.  Now the tool is never dispatched and the sentence
    names the parameter and the value, so the model can write one this system
    can honour.
    """
    ran: list[str] = []

    async def quick(_arguments: dict[str, Any]) -> str:
        ran.append("ran")
        return "ran"

    for written in ("5 seconds", "x", 0, -1, True):
        messages: list[Message] = []
        call = ToolCall(id="1", name="quick", arguments={"_timeout": written})
        await one(call, [a_tool("quick", quick)]).run_turn(messages, "hi")
        answer = tool_message(messages)
        assert answer.startswith(
            "Error: tool call 'quick' was not run: `_timeout` must be a positive, "
            "finite number"
        ), (written, answer)
        assert repr(written) in answer, written
    assert ran == [], "the tool was dispatched with a bound it could not honour"

    # Omitted and an explicit null are "nobody said": the call runs.
    for arguments in ({}, {"_timeout": None}):
        messages = []
        await one(
            ToolCall(id="1", name="quick", arguments=arguments),
            [a_tool("quick", quick)],
        ).run_turn(messages, "hi")
        assert tool_message(messages) == "ran", arguments


@pytest.mark.asyncio
async def test_a_tool_with_its_own_timeout_is_never_wrapped() -> None:
    """Native tools self-enforce, and the loop's bound is not a second timer.

    `_timeout: 7` reaches the tool as its own argument, and a `_timeout` that
    truncates to zero is dropped rather than forwarded: the tools that read this
    argument document "0 = your default", and one of them would fire instantly on
    it.
    """
    seen: list[dict[str, Any]] = []

    async def native(arguments: dict[str, Any]) -> str:
        seen.append(dict(arguments))
        return "ran"

    tool = a_tool("native", native, timeout={"type": "integer"})

    messages: list[Message] = []
    await one(
        ToolCall(id="1", name="native", arguments={"_timeout": 7}), [tool]
    ).run_turn(messages, "hi")
    assert seen[-1] == {"timeout": 7}

    await one(
        ToolCall(id="2", name="native", arguments={"_timeout": 0.4}), [tool]
    ).run_turn(messages, "hi")
    # Truncated to 0 → removed, so the tool's own default governs.
    assert seen[-1] == {}


@pytest.mark.asyncio
async def test_a_bare_timeout_on_a_tool_without_one_is_read_and_stripped() -> None:
    """The model's habit of appending `timeout` is answered, not forwarded.

    A tool server validates its parameters, and an argument its schema does not
    declare makes it refuse the WHOLE call.  So a bare `timeout` becomes this
    call's bound and never reaches the tool.
    """
    never = asyncio.Event()
    seen: list[dict[str, Any]] = []

    async def hangs(arguments: dict[str, Any]) -> str:
        seen.append(dict(arguments))
        await never.wait()
        return "never"

    call = ToolCall(id="1", name="hangs", arguments={"e": "1", "timeout": 0.05})
    messages: list[Message] = []
    await one(call, [a_tool("hangs", hangs)]).run_turn(messages, "hi")

    assert tool_message(messages) == "Error: Tool 'hangs' timed out (0.05s)."
    assert seen and seen[0] == {"e": "1"}


@pytest.mark.asyncio
async def test_a_bare_timeout_is_stripped_even_when_timeout_was_also_given() -> None:
    """**Both spellings at once, and the bare one still never reaches the tool.**

    The harness's `_timeout` wins, but winning is not the same as excusing the
    other key: a tool that does not declare `timeout` refuses the whole call for
    an argument its schema does not have, so the bare key has to come out
    whether or not `_timeout` was written.  The call that writes both is exactly
    the one where it used to stay.
    """
    seen: list[dict[str, Any]] = []

    async def capture(arguments: dict[str, Any]) -> str:
        seen.append(dict(arguments))
        return "ran"

    call = ToolCall(
        id="1", name="capture", arguments={"e": "1", "_timeout": 30, "timeout": 60}
    )
    await one(call, [a_tool("capture", capture)]).run_turn([], "hi")

    assert seen[0] == {"e": "1"}, "neither spelling is the tool's business"


@pytest.mark.asyncio
async def test_the_meta_parameters_reach_the_history_but_not_the_tool() -> None:
    """The strip is a copy: what the model said is what the conversation keeps."""
    seen: list[dict[str, Any]] = []

    async def capture(arguments: dict[str, Any]) -> str:
        seen.append(dict(arguments))
        return "ran"

    call = ToolCall(id="1", name="capture", arguments={"e": "2", "_timeout": 3})
    messages: list[Message] = []
    loop = one(call, [a_tool("capture", capture)])
    await loop.run_turn(messages, "hi")

    assert seen == [{"e": "2"}]
    assistant = next(m for m in messages if m.role == "assistant" and m.tool_calls)
    assert assistant.tool_calls[0].arguments == {"e": "2", "_timeout": 3}


# ── `_async` ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_background_call_answers_a_marker_naming_the_call() -> None:
    """The reply is the marker naming all four things, under the sentence that
    says the task started."""

    async def slow(_arguments: dict[str, Any]) -> str:
        return "the answer"

    call = ToolCall(id="call-7", name="slow", arguments={"_async": True})
    messages: list[Message] = []
    await one(call, [a_tool("slow", slow)], deliver=collected).run_turn(messages, "hi")

    fields = marker_fields(tool_message(messages))
    assert fields["call_id"] == "call-7"
    assert fields["tool"] == "slow"
    assert fields["status"] == RUNNING
    assert len(fields["task_id"]) == 8
    assert "✓ Async task started." in tool_message(messages)


@pytest.mark.asyncio
async def test_a_background_result_is_handed_back_to_the_conversation() -> None:
    """The result arrives on its own, wearing the status that says what it is.

    Pushed rather than polled: the delivery is the whole point of the mode, and
    what `check_async` answers afterwards is *where the call is*, not what it
    said.
    """
    delivered: list[str] = []
    channels: list[str] = []

    async def deliver(text: str, channel: str) -> bool:
        delivered.append(text)
        channels.append(channel)
        return True

    async def slow(_arguments: dict[str, Any]) -> str:
        await asyncio.sleep(0)
        return "the answer"

    call = ToolCall(id="call-1", name="slow", arguments={"_async": True})
    messages: list[Message] = []
    loop = one(call, [a_tool("slow", slow)], deliver=deliver)
    await loop.run_turn(messages, "hi")

    task_id = marker_fields(tool_message(messages))["task_id"]
    for _ in range(50):  # the background task, not this one, is doing the work
        if delivered:
            break
        await asyncio.sleep(0)

    assert delivered, "the result was never handed back"
    # Which call it answers rides the channel as well as the marker, so the
    # bubble a person sees names the tool (`slife2.libs.harness.async_tasks.channel_for`).
    assert channels == [channel_for("slow")]
    fields = marker_fields(delivered[0])
    assert fields["status"] == FINISHED
    assert fields["task_id"] == task_id
    assert "the answer" in delivered[0]

    # And the status query says so, without repeating the result: it was already
    # delivered, and a result read twice is a result the model keeps paying for.
    answer = await check_async({"task_id": task_id})
    assert answer["status"] == FINISHED
    assert answer["call_id"] == "call-1" and answer["tool"] == "slow"
    assert "result" not in answer, "the answer was already handed over"


@pytest.mark.asyncio
async def test_an_undelivered_result_stays_readable() -> None:
    """A hand-back that could not land is kept, rather than lost with it.

    The two "no" answers are both possible in production — a window not showing
    that agent, an agent server that refused — and the fallback road under the
    fallback road is the point of keeping the text.
    """

    async def never_delivers(_text: str, _channel: str) -> bool:
        return False

    async def slow(_arguments: dict[str, Any]) -> str:
        await asyncio.sleep(0)
        return "lost unless kept"

    call = ToolCall(id="call-2", name="slow", arguments={"_async": True})
    messages: list[Message] = []
    loop = one(call, [a_tool("slow", slow)], deliver=never_delivers)
    await loop.run_turn(messages, "hi")
    task_id = marker_fields(tool_message(messages))["task_id"]

    for _ in range(50):
        task = async_tasks._get_task(task_id)  # noqa: SLF001 - see the fixture
        if task is not None and task.handle.done():
            break
        await asyncio.sleep(0)

    answer = await check_async({"task_id": task_id})
    assert answer["result"] == "lost unless kept", "nobody took it, so it is still here"
    assert answer["status"] == FINISHED


@pytest.mark.asyncio
async def test_a_background_result_is_windowed_before_it_is_handed_back() -> None:
    """**A result is bounded whichever road it took.**

    A background call's answer reaches the conversation as a *message* — a turn
    of its own, several turns later — which is the one road a tool result takes
    that the loop's own fit does not cover.  So the whole output entered the
    conversation and its turn log and was carried by every later request, and one
    call's answer was bounded or unbounded depending only on whether that
    delivery landed.  The window is over the body alone: the marker and the
    sentence that name the call this answers are how the model pairs the result
    with the call that asked for it, and a window may not take them away.
    """
    big = "\n".join(f"line {number} of a tool output" for number in range(1, 400))
    fitted: list[tuple[str, int]] = []
    handed: list[str] = []

    async def fit(call: ToolCall, text: str, _messages: list[Message]) -> str:
        fitted.append((call.name, len(text)))
        return "WINDOW"

    async def deliver(text: str, _channel: str) -> bool:
        handed.append(text)
        return True

    async def slow(_arguments: dict[str, Any]) -> str:
        await asyncio.sleep(0)
        return big

    call = ToolCall(id="call-fit", name="slow", arguments={"_async": True})
    messages: list[Message] = []
    loop = one(call, [a_tool("slow", slow)], deliver=deliver, compress=fit)
    await loop.run_turn(messages, "hi")

    for _ in range(100):  # the background task, not this one, is doing the work
        if handed:
            break
        await asyncio.sleep(0.01)  # timeouts: a fixture's magnitude, not a budget

    # The loop fits the *started* text it appends as this call's tool message,
    # and the hand-back fits the tool's own words — the second is what this is
    # about, and without it the fit never sees the output at all.
    assert ("slow", len(big)) in fitted, "the window never saw the tool's own words"
    assert handed, "the result was never handed back"
    assert "WINDOW" in handed[0], "the hand-back carried the whole output"
    assert big not in handed[0]
    # The two halves that are not the output stand: they are the address.
    assert handed[0].startswith(MARKER_PREFIX)
    assert "The background call to slow finished." in handed[0]


@pytest.mark.asyncio
async def test_a_background_timeout_says_which_call_and_which_bound() -> None:
    """The foreground's sentence, on the road that used to say nothing.

    `_background` wrapped its call in `asyncio.wait_for`, whose `TimeoutError`
    carries no message — and `slife2.libs.harness.async_tasks._runner` renders
    whatever escapes a task as `Error: {type}: {exc}`, so the conversation read
    literally `Error: TimeoutError: `: no tool, no bound, and nothing the model
    could act on.  A bound that fired is news it acts on, and the sentence that
    carries it is the one the foreground path already writes.
    """
    handed: list[str] = []
    never = asyncio.Event()

    async def deliver(text: str, _channel: str) -> bool:
        handed.append(text)
        return True

    async def hangs(_arguments: dict[str, Any]) -> tuple[str, bool]:
        await never.wait()
        return "never", True

    call = ToolCall(
        id="call-slow", name="hangs", arguments={"_async": True, "_timeout": 0.05}
    )
    messages: list[Message] = []
    loop = one(call, [a_tool("hangs", hangs)], deliver=deliver)
    await loop.run_turn(messages, "hi")

    for _ in range(100):  # the bound has to fire before the hand-back exists
        if handed:
            break
        await asyncio.sleep(0.01)  # timeouts: a fixture's magnitude, not a budget

    assert handed, "the timed-out call was never handed back"
    assert "Error: Tool 'hangs' timed out (0.05s)." in handed[0]
    assert "TimeoutError" not in handed[0]


@pytest.mark.asyncio
async def test_a_background_call_is_refused_where_nothing_can_be_handed_back() -> None:
    """No `deliver` means the mode is refused, in a sentence that can be acted on.

    An agent task's loop is that case (SDD §14.2): it runs one turn and has no
    second one for a result to arrive in, so a task id would be a promise nothing
    could keep.  The refusal says which one line fixes it.
    """
    seen: list[str] = []

    async def slow(_arguments: dict[str, Any]) -> str:
        seen.append("ran")
        return "the answer"

    call = ToolCall(id="1", name="slow", arguments={"_async": True})
    messages: list[Message] = []
    await one(call, [a_tool("slow", slow)]).run_turn(messages, "hi")

    assert tool_message(messages) == BACKGROUND_UNAVAILABLE
    assert seen == []


@pytest.mark.asyncio
async def test_a_background_task_outlives_the_turn_that_started_it() -> None:
    """Cancelling the turn does not cancel the work it handed off.

    The turn's cancellation reaches what the turn is *awaiting*; this it stopped
    awaiting the moment it was scheduled, which is the whole of what background
    means.
    """
    finished = asyncio.Event()

    async def deliver(_text: str, _channel: str) -> bool:
        finished.set()
        return True

    async def slow(_arguments: dict[str, Any]) -> str:
        await asyncio.sleep(0)
        return "still here"

    call = ToolCall(id="1", name="slow", arguments={"_async": True})
    messages: list[Message] = []
    # A second model call that takes its time, so the turn is still in flight
    # when it is cancelled — what this test is about.
    slower_answer = text_turn("done", delay=0.2)  # timeouts: the point of this test
    loop = AgentLoop(
        FakeBackend(tool_turn(call), slower_answer),
        ToolRegistry([a_tool("slow", slow)]),
        deliver=deliver,
    )
    turn = asyncio.create_task(loop.run_turn(messages, "hi"))
    # Cancelled *after* the call has been handed off — the moment that matters is
    # the one where the turn has stopped awaiting it, which is when its tool
    # result lands (the assistant message is written before the call runs).
    await asyncio.wait_for(
        _until(lambda: any(m.role == "tool" for m in messages)),
        timeout=1.0,  # timeouts: the value this test is built around, not a budget
    )
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    await asyncio.wait_for(
        finished.wait(),
        timeout=1.0,  # timeouts: the value this test is built around, not a budget
    )  # timeouts: the value this test is built around, not a budget


async def _until(predicate) -> None:
    """Spin until `predicate` holds — a detached hand-back has no handle to await."""
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    return


@pytest.mark.asyncio
async def test_cancelling_a_background_call_hands_back_that_it_was_cancelled() -> None:
    """A cancellation the conversation never hears about is a panel running forever."""
    handed: list[str] = []

    async def deliver(text: str, _channel: str) -> bool:
        handed.append(text)
        return True

    never = asyncio.Event()

    async def hangs(_arguments: dict[str, Any]) -> tuple[str, bool]:
        await never.wait()
        return "never", True

    task_id = async_tasks.schedule(
        hangs({}), call_id="call-9", tool="hangs", deliver=deliver
    )
    await asyncio.sleep(0)
    answer = await cancel_async({"task_id": task_id})

    assert answer["status"] == CANCELLED
    assert answer["call_id"] == "call-9"
    await asyncio.wait_for(  # the hand-back is detached, so it is waited for here
        _until(lambda: bool(handed)),
        timeout=1.0,  # timeouts: the value this test is built around, not a budget
    )
    fields = marker_fields(handed[0])
    assert fields["status"] == CANCELLED
    assert fields["call_id"] == "call-9"


# ── `_approve` ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_denied_call_runs_nothing_and_mounts_nothing() -> None:
    """The refusal is the whole of what the model and the screen get.

    No `ToolCallStarted`, and so no panel: the reason is the display — one that
    appeared and then said "denied" would have told a reader the tool ran.
    """
    seen: list[str] = []

    async def sensitive(_arguments: dict[str, Any]) -> str:
        seen.append("ran")
        return "did it"

    async def deny(_call: ToolCall) -> bool:
        return False

    observer = ListObserver()
    call = ToolCall(id="1", name="sensitive", arguments={"_approve": True})
    messages: list[Message] = []
    await one(call, [a_tool("sensitive", sensitive)], approve=deny).run_turn(
        messages, "hi", observer
    )

    assert seen == []
    assert tool_message(messages) == DENIED
    started = [e for e in observer.events if isinstance(e, ToolCallStarted)]
    finished = [e for e in observer.events if isinstance(e, ToolCallFinished)]
    assert started == []
    assert finished == []


@pytest.mark.asyncio
async def test_an_approved_call_runs_and_is_announced_once() -> None:
    """The gate is a pause, not a second path: everything after it is ordinary."""
    asked: list[str] = []

    async def yes(call: ToolCall) -> bool:
        asked.append(call.name)
        return True

    async def sensitive(_arguments: dict[str, Any]) -> str:
        return "did it"

    observer = ListObserver()
    call = ToolCall(id="1", name="sensitive", arguments={"_approve": True})
    messages: list[Message] = []
    await one(call, [a_tool("sensitive", sensitive)], approve=yes).run_turn(
        messages, "hi", observer
    )

    assert asked == ["sensitive"]
    assert tool_message(messages) == "did it"
    assert len([e for e in observer.events if isinstance(e, ToolCallStarted)]) == 1


@pytest.mark.asyncio
async def test_no_approve_means_the_gate_is_never_asked() -> None:
    """An approval nobody asked for is not a confirmation."""
    asked: list[str] = []

    async def yes(call: ToolCall) -> bool:
        asked.append(call.name)
        return True

    async def plain(_arguments: dict[str, Any]) -> str:
        return "ran"

    messages: list[Message] = []
    await one(
        ToolCall(id="1", name="plain", arguments={}),
        [a_tool("plain", plain)],
        approve=yes,
    ).run_turn(messages, "hi")

    assert asked == []
    assert tool_message(messages) == "ran"


@pytest.mark.asyncio
async def test_a_loop_with_nobody_to_ask_runs_the_call() -> None:
    """The rule for a worker: the decision belongs to whoever is watching, and
    nobody is — so the call runs rather than becoming one the model can never use.
    """

    async def sensitive(_arguments: dict[str, Any]) -> str:
        return "did it"

    call = ToolCall(id="1", name="sensitive", arguments={"_approve": True})
    messages: list[Message] = []
    await one(call, [a_tool("sensitive", sensitive)]).run_turn(messages, "hi")

    assert tool_message(messages) == "did it"


# ── the two tools, and the marker they share ──────────────────────────────────


def test_the_marker_is_one_shape_for_every_status() -> None:
    """Four statuses, one prefix, one JSON object — read by a model, a screen and
    this build's own tests, which is why it is built rather than hand-spelled.
    """
    for status in (RUNNING, FINISHED, CANCELLED):
        fields = marker_fields(
            marker(status, call_id="c", tool="t", task_id="deadbeef")
        )
        assert fields == {
            "call_id": "c",
            "tool": "t",
            "task_id": "deadbeef",
            "status": status,
        }


@pytest.mark.asyncio
async def test_check_and_cancel_refuse_a_task_they_do_not_have() -> None:
    """**A miss is a refusal and not an answer.**  The sentence used to be
    *returned*, so a call naming a task nobody had ever made was recorded as a
    call that succeeded — an `ok` with "Error:" in its text — and the distinction
    is the registry's to make (`ToolRegistry.execute` renders this one bare)."""
    for tool in (check_async, cancel_async):
        with pytest.raises(ToolFailed, match="nosuch"):
            await tool({"task_id": "nosuch"})


def test_the_two_tools_keep_the_names_the_prompt_uses() -> None:
    """One spelling per tool, and the prompt's own sentence uses the same one.

    The tools themselves are declared by the agent server and catalogued like
    every other model tool (`slife2.plugins.agent.server`); what this holds is
    the name the model is taught and the name the tool answers to.
    """
    assert CHECK_ASYNC == "check_async"
    assert async_tasks.CANCEL_ASYNC == "cancel_async"
    assert 'check_async(task_id="' in async_tasks.started("deadbeef", "c", "t")


def test_an_endless_timeout_is_refused_like_any_other_bad_number() -> None:
    """**`1e999` is valid JSON and is not a duration anyone can honour.**

    It arrives as `inf`, and it used to pass through as a bound until `int(inf)`
    raised `OverflowError` out of a turn.  It is now refused like every other
    value that is not a positive, finite number, and the call is answered with
    the sentence instead of being run.
    """
    for value in (1e999, float("inf"), "inf"):
        seconds, error = read_timeout(value)
        assert seconds is None
        assert error is not None and "positive, finite" in error

    # And the same through the binder, where the crash surfaced: a bare
    # `timeout` the tool does not declare is read just as strictly.
    spec = ToolSpec(name="render", description="", parameters={"properties": {}})
    arguments: dict[str, Any] = {"timeout": 1e999}
    bound, error = bind_timeout(arguments, spec, None)
    assert bound is None and error is not None
    assert "timeout" not in arguments


@pytest.mark.asyncio
async def test_a_finished_call_whose_delivery_is_in_flight_is_not_settled() -> None:
    """**Final status is not a finished delivery**, and the window between them
    is one a poll can land in.

    `_runner` records the ending before it awaits the hand-back, so a task can be
    `FINISHED` while its answer is still on its way.  `check_async` read that as
    "finished, and its result could not be handed back" — telling the model a
    delivery that was about to land had failed, and *popping* the task, so the
    copy of the answer was gone if that delivery then failed.  `cancel_async` was
    worse: it cancelled the handle, which aborted a completed call's delivery
    mid-flight and reported "cancelled, so it has no result".

    The JSON says the same thing one field at a time: the call is `finished` and
    nothing has been delivered *yet* — and the result is not offered, because a
    delivery about to land must not be read twice.
    """
    parked = asyncio.Event()

    async def never_returns() -> str:
        await parked.wait()
        return ""

    delivered: list[str] = []

    async def deliver(text: str, channel: str) -> bool:
        delivered.append(text)
        await parked.wait()
        return True

    task = async_tasks.Task(
        handle=asyncio.create_task(never_returns()),
        call_id="c",
        tool="pdf_to_md",
        status=async_tasks.FINISHED,
        deliver=deliver,
        result="the text",
    )
    async_tasks._tasks["inflight"] = task
    try:
        answer = await check_async({"task_id": "inflight"})
        assert answer["status"] == async_tasks.FINISHED
        assert "result" not in answer, "the delivery it is about to make, twice"
        assert async_tasks._get_task("inflight") is task, "and it was not popped"

        refused = await cancel_async({"task_id": "inflight"})
        assert refused["status"] == async_tasks.FINISHED, "nothing to cancel"
        assert not task.handle.cancelled(), "a delivery in flight is not cancelled"
    finally:
        async_tasks._pop_task("inflight")
        parked.set()
        task.handle.cancel()
