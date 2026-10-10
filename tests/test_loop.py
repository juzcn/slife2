"""The agent loop.

Driven entirely by a scripted backend, so each of the loop's claims — that the
result wins over the deltas, that a tool error is a message rather than an
exception, that a broken observer cannot end a turn — is a direct assertion
rather than something inferred from a live model.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from fakes import FakeBackend, ListObserver, RaisingObserver, ScriptedTurn, text_turn

from slife2.builtins import evaluate
from slife2.events import TextDelta, ToolCallFinished, ToolCallStarted, TurnFinished
from slife2.llm.base import Chunk, ToolCallDelta
from slife2.loop import AgentLoop
from slife2.messages import Message, StreamChatResult, ToolCall, ToolSpec, Usage
from slife2.tools import Tool, ToolRegistry

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def a_tool(name: str, run) -> Tool:
    return Tool(
        spec=ToolSpec(name=name, description="", parameters={"type": "object"}),
        run=run,
    )


def registry() -> ToolRegistry:
    return ToolRegistry(tools())


def tools() -> list[Tool]:
    """Two tools, defined here rather than fetched from a server.

    The loop is handed a registry and must not care where it came from, so its
    tests build one instead of talking to the builtins server — what is under
    test is the round trip, not the arithmetic.  `evaluate` is borrowed because
    writing a second expression parser to test a loop would be silly, and `now`
    is real because a tool that returns text is all this file needs.
    """

    async def calc(arguments: dict[str, Any]) -> str:
        return str(evaluate(str(arguments.get("e") or "")))

    async def now(_arguments: dict[str, Any]) -> str:
        return datetime.now(UTC).isoformat()

    return [a_tool("now", now), a_tool("calc", calc)]


def tool_turn(*calls: ToolCall, text: str = "") -> ScriptedTurn:
    """A scripted model response that asks for tools.

    The result carries the complete calls — as the LLM server delivers them —
    while the chunks carry fragments, as the provider streams them.  Keeping
    both means the loop is exercised against the real arrangement.
    """
    chunks = [Chunk(text=text)] if text else []
    for index, call in enumerate(calls):
        chunks.append(
            Chunk(tool_call_deltas=(ToolCallDelta(index, id=call.id, name=call.name),))
        )
    return ScriptedTurn(
        result=StreamChatResult(
            text=text, tool_calls=tuple(calls), stop_reason="tool_calls"
        ),
        chunks=chunks,
    )


# --- the basic shapes --------------------------------------------------------


async def test_text_only_turn() -> None:
    backend = FakeBackend(text_turn("hello there", chunks=["hello", " there"]))
    observer = ListObserver()
    messages: list[Message] = []

    result = await AgentLoop(backend, registry()).run_turn(messages, "hi", observer)

    assert result.text == "hello there"
    assert result.steps == 1
    assert result.stop_reason == "stop"
    assert observer.kinds() == ["TextDelta", "TextDelta", "TurnFinished"]
    assert [m.role for m in messages] == ["user", "assistant"]


async def test_reasoning_lands_on_the_message_the_way_text_does() -> None:
    """Off the *result*, not off the deltas — the same rule as the answer.

    Both are streamed to the reader and both are kept, and only one of those two
    channels is allowed to drop fragments.  Scripted contradictorily on purpose:
    the chunks spell one thing and the result another, so a loop that
    reassembled the reasoning from what it forwarded would be caught here.  That
    the provider is never sent it back is `to_wire`'s, and `tests/test_wire.py`
    states it.
    """
    turn = ScriptedTurn(
        result=StreamChatResult(text="42", thinking="six sevens are forty-two"),
        chunks=[Chunk(thinking="six sevens are a dozen")],
    )
    backend = FakeBackend(turn)
    messages: list[Message] = []

    await AgentLoop(backend, registry()).run_turn(messages, "6*7?")

    assert messages[-1].thinking == "six sevens are forty-two"


async def test_the_result_wins_over_the_deltas() -> None:
    """Progress notifications are display; the call result is the truth.

    Scripted deliberately contradictory: the deltas spell one thing and the
    result says another.  A dropped or late notification must not corrupt the
    conversation, and this is what that guarantee costs.
    """
    turn = ScriptedTurn(
        result=StreamChatResult(text="authoritative", stop_reason="stop"),
        chunks=[Chunk(text="garbage")],
    )
    backend = FakeBackend(turn)
    messages: list[Message] = []

    result = await AgentLoop(backend, registry()).run_turn(messages, "hi")

    assert result.text == "authoritative"
    assert messages[-1].content == "authoritative"


# --- the tool round trip -----------------------------------------------------


async def test_tool_call_runs_and_feeds_back() -> None:
    backend = FakeBackend(
        tool_turn(
            ToolCall(id="c1", name="calc", arguments={"e": "2+2"}), text="let me check"
        ),
        text_turn("It is 4."),
    )
    observer = ListObserver()
    messages: list[Message] = []

    result = await AgentLoop(backend, registry()).run_turn(messages, "2+2?", observer)

    assert result.text == "It is 4."
    assert result.steps == 2
    # The structure, not the chunking: how the answer is split into deltas is
    # the provider's business and asserting on it would make this test break
    # for a reason that is not a regression.
    assert [k for k in observer.kinds() if k != "TextDelta"] == [
        "ToolCallStarted",
        "ToolCallFinished",
        "TurnFinished",
    ]

    # The conversation now holds the whole exchange, in order.
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    tool_message = messages[2]
    assert (tool_message.tool_call_id, tool_message.content) == ("c1", "4")

    # And the second model call saw it.
    second_call_messages = backend.calls[1][0]
    assert [m.role for m in second_call_messages] == ["user", "assistant", "tool"]


async def test_a_harness_tool_is_called_through_the_registry() -> None:
    """v1's auto-invoke: the *same* path the model's own calls take.

    The point is where the pair's text comes from.  The harness names a tool and
    the loop runs it out of `self._tools` — the registry whose `specs` went out
    with the last request — so the text is the tool's own answer rather than
    something written beside it, exactly as `trim_tools` gets the trim's text
    from `_func_tool_unload`.  A pair that could say something the tool would not
    say is the second source of truth this arrangement exists to avoid.
    """
    calls: list[dict[str, Any]] = []
    delivered = False

    async def check_new_input(arguments: dict[str, Any]) -> str:
        calls.append(arguments)
        return "the second message"

    backend = FakeBackend(
        tool_turn(ToolCall(id="c1", name="calc", arguments={"e": "2+2"})),
        text_turn("It is 4."),
    )
    harness_tools = [*tools(), a_tool("_check_new_input", check_new_input)]

    def auto() -> str | None:
        """Asked at the top of *every* step; delivers once, on the second.

        The gate is the caller's, and it is the cheap half of v1's split: whether
        anything arrived is answerable without a call, and which tool answers it
        is this side's business only because there is one.
        """
        nonlocal delivered
        if backend.calls and not delivered:
            delivered = True
            return "_check_new_input"
        return None

    observer = ListObserver()
    messages: list[Message] = []
    result = await AgentLoop(backend, ToolRegistry(harness_tools)).run_turn(
        messages, "one", observer, auto=auto
    )

    assert result.text == "It is 4."
    # Called once, with no arguments: there is nothing for the harness to choose.
    assert calls == [{}]

    # The pair is the tool's call and the tool's answer, in that shape — written
    # between the tool call the model made and the answer it gave afterwards.
    assert [m.role for m in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    opened, answered = messages[3], messages[4]
    assert opened.role == "assistant" and opened.content is None
    call = opened.tool_calls[0]
    assert call.name == "_check_new_input"
    assert call.id.startswith("_harness_check_new_input_")
    assert (answered.role, answered.tool_call_id) == ("tool", call.id)
    assert answered.content == "the second message"

    # The model's next request carries it — which is the only reason the pair is
    # written at all — and nothing pretended the call was the model's.
    assert [m.role for m in backend.calls[1][0]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    # Nobody watching is shown a tool call for it: v1 routes around the
    # tool-execution path, so the reader sees the model's `c1` and no other.
    assert [
        event.call_id for event in observer.events if isinstance(event, ToolCallStarted)
    ] == ["c1"]


async def test_a_failing_tool_is_fed_back_as_text() -> None:
    """The model gets to see the mistake and correct itself."""
    backend = FakeBackend(
        tool_turn(ToolCall(id="c1", name="calc", arguments={"e": "1/0"})),
        text_turn("That divides by zero."),
    )
    observer = ListObserver()
    messages: list[Message] = []

    result = await AgentLoop(backend, registry()).run_turn(messages, "1/0", observer)

    assert result.text == "That divides by zero."
    finished = [e for e in observer.events if isinstance(e, ToolCallFinished)]
    assert finished[0].ok is False
    assert "ZeroDivisionError" in (messages[2].content or "")


async def test_an_unknown_tool_is_fed_back_as_text() -> None:
    backend = FakeBackend(
        tool_turn(ToolCall(id="c1", name="wether", arguments={})),
        text_turn("No such tool."),
    )
    observer = ListObserver()
    messages: list[Message] = []

    await AgentLoop(backend, registry()).run_turn(messages, "weather?", observer)

    finished = [e for e in observer.events if isinstance(e, ToolCallFinished)]
    assert finished[0].ok is False
    assert "wether" in (messages[2].content or "")


async def test_parallel_tool_calls_all_run() -> None:
    backend = FakeBackend(
        tool_turn(
            ToolCall(id="c1", name="calc", arguments={"e": "1+1"}),
            ToolCall(id="c2", name="calc", arguments={"e": "2+2"}),
        ),
        text_turn("2 and 4."),
    )
    messages: list[Message] = []

    await AgentLoop(backend, registry()).run_turn(messages, "add", ListObserver())

    assert [m.content for m in messages if m.role == "tool"] == ["2", "4"]


async def test_tool_result_preview_is_truncated_but_the_model_gets_it_all() -> None:
    """The model sees the whole result; the progress stream sees a preview."""
    long_value = "x" * 5000
    backend = FakeBackend(
        tool_turn(ToolCall(id="c1", name="calc", arguments={"e": long_value})),
        text_turn("done"),
    )
    observer = ListObserver()
    messages: list[Message] = []

    await AgentLoop(backend, registry()).run_turn(messages, "go", observer)

    finished = next(e for e in observer.events if isinstance(e, ToolCallFinished))
    assert len(finished.result_preview) < 300
    # The tool message is the error text for an invalid expression, but the
    # point stands: it is not truncated anywhere in the conversation.
    assert finished.result_chars == len(messages[2].content or "")


# --- limits and failure ------------------------------------------------------


async def test_max_steps_cuts_off_a_runaway_loop() -> None:
    """A model that keeps calling tools produces a result, not a hang."""
    backend = FakeBackend(
        *[tool_turn(ToolCall(id=f"c{i}", name="now", arguments={})) for i in range(3)]
    )
    observer = ListObserver()

    result = await AgentLoop(backend, registry(), max_steps=3).run_turn(
        [], "go", observer
    )

    assert result.hit_step_limit is True
    assert result.steps == 3
    assert len(backend.calls) == 3
    final = observer.events[-1]
    assert isinstance(final, TurnFinished)
    assert final.stop_reason == "max_steps"


async def test_a_broken_observer_cannot_end_a_turn() -> None:
    """The answer is still correct and still returned."""
    backend = FakeBackend(text_turn("hello"))
    messages: list[Message] = []

    result = await AgentLoop(backend, registry()).run_turn(
        messages, "hi", RaisingObserver()
    )

    assert result.text == "hello"
    assert messages[-1].content == "hello"


async def test_cancellation_leaves_the_history_well_formed() -> None:
    """A turn cut off mid-stream must not leave a dangling user message pair.

    The loop appends the assistant message only after the stream completes, so
    an interrupted turn leaves the list ending in `user`.  What the loop
    guarantees is that nothing half-written is in the list; what happens to that
    trailing user message is the owner's decision, and the owner is the agent
    server — which keeps it, and records it, because losing it is the failure
    the whole loop design exists to prevent.  See DESIGN.md §3.
    """
    backend = FakeBackend(text_turn("slow", chunks=["s", "l", "o", "w"], delay=0.05))
    messages: list[Message] = []

    task = asyncio.create_task(AgentLoop(backend, registry()).run_turn(messages, "hi"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [m.role for m in messages] == ["user"]


async def test_observer_receives_deltas_in_order() -> None:
    backend = FakeBackend(text_turn("abc", chunks=["a", "b", "c"]))
    observer = ListObserver()

    await AgentLoop(backend, registry()).run_turn([], "hi", observer)

    assert [e.text for e in observer.events if isinstance(e, TextDelta)] == [
        "a",
        "b",
        "c",
    ]


async def test_usage_accumulates_across_steps() -> None:
    backend = FakeBackend(
        ScriptedTurn(
            result=StreamChatResult(
                tool_calls=(ToolCall(id="c1", name="now", arguments={}),),
                usage=Usage(prompt_tokens=10, completion_tokens=2),
                stop_reason="tool_calls",
            )
        ),
        ScriptedTurn(
            result=StreamChatResult(
                text="done", usage=Usage(prompt_tokens=20, completion_tokens=3)
            )
        ),
    )

    result = await AgentLoop(backend, registry()).run_turn([], "go")

    assert result.usage.prompt_tokens == 30
    assert result.usage.completion_tokens == 5


async def test_tool_specs_are_advertised_to_the_model() -> None:
    backend = FakeBackend(text_turn("hi"))
    await AgentLoop(backend, registry()).run_turn([], "hi")
    _, tools = backend.calls[0]
    assert {t.name for t in tools} == {"now", "calc"}


async def test_the_tool_list_is_asked_for_before_every_model_call() -> None:
    """Not once per turn — once per call, so a change lands mid-turn.

    A turn that takes several steps makes several requests to the model, and
    each carries its own tool list.  Asking once per turn would leave the
    second call advertising a tool the first call's answer had just removed.
    """
    asked = 0

    async def growing() -> ToolRegistry:
        nonlocal asked
        asked += 1
        late = (
            [a_tool("late", lambda arguments: asyncio.sleep(0, result="ok"))]
            if asked > 1
            else []
        )
        return ToolRegistry([*tools(), *late])

    backend = FakeBackend(
        tool_turn(ToolCall(id="c1", name="now", arguments={})), text_turn("done")
    )
    loop = AgentLoop(backend, await growing(), refresh=growing)

    await loop.run_turn([], "go")

    assert asked == 3, "once when it was built, then once per model call"
    assert [sorted(spec.name for spec in sent) for _, sent in backend.calls] == [
        ["calc", "late", "now"],
        ["calc", "late", "now"],
    ]


async def test_the_seed_list_is_the_loops_own_until_it_is_refreshed() -> None:
    """No refresh given, nothing is asked for: an injected backend's registry
    stands, which is what keeps the loop runnable without a hub at all."""
    backend = FakeBackend(text_turn("hi"))
    await AgentLoop(backend, registry()).run_turn([], "hi")
    _, tools = backend.calls[0]
    assert {t.name for t in tools} == {"now", "calc"}


async def test_a_refresh_that_fails_ends_the_turn() -> None:
    """Deliberately not swallowed.

    A model called with the previous step's tool list is a silently wrong
    request; a turn that fails is a visible one.  See
    `slife2.mcp_server.open_server`.
    """

    async def broken() -> ToolRegistry:
        raise ConnectionError("the toolhub is gone")

    backend = FakeBackend(text_turn("hi"))
    with pytest.raises(ConnectionError):
        await AgentLoop(backend, registry(), refresh=broken).run_turn([], "hi")


async def test_tool_call_started_precedes_finished() -> None:
    backend = FakeBackend(
        tool_turn(ToolCall(id="c1", name="now", arguments={})), text_turn("done")
    )
    observer = ListObserver()

    await AgentLoop(backend, registry()).run_turn([], "go", observer)

    kinds = observer.kinds()
    started, finished = kinds.index("ToolCallStarted"), kinds.index("ToolCallFinished")
    assert started < finished
    event = observer.events[started]
    assert isinstance(event, ToolCallStarted)
    assert (event.call_id, event.name) == ("c1", "now")
