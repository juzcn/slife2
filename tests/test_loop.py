"""The agent loop.

Driven entirely by a scripted backend, so each of the loop's claims — that the
result wins over the deltas, that a tool error is a message rather than an
exception, that a broken observer cannot end a turn — is a direct assertion
rather than something inferred from a live model.
"""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeBackend, ListObserver, RaisingObserver, ScriptedTurn, text_turn

from slife2.events import TextDelta, ToolCallFinished, ToolCallStarted, TurnFinished
from slife2.llm.base import Chunk, ToolCallDelta
from slife2.loop import AgentLoop
from slife2.messages import Message, StreamChatResult, ToolCall, Usage
from slife2.tools import ToolRegistry, builtin_tools

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def registry() -> ToolRegistry:
    return ToolRegistry(builtin_tools())


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
    an interrupted turn leaves the list ending in `user` — which the *server*
    repairs, because only it knows a partial answer was shown.  What the loop
    guarantees is that nothing half-written is in the list.
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
