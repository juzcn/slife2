"""The LLM MCP servers — one per wire protocol.

Three layers are tested here, and the middle one is the point of the file:

1. `translate` — the pure provider-event adapter, driven by *real* SDK objects
   built with `model_validate`.  No mocking library: the SDK parses its own wire
   format, and that is the shape we adapt to.
2. `stream_chat` end to end over FastMCP's in-memory transport, with a scripted
   streamer in place of the provider.  This is where tool-call fragment
   reassembly and progress delivery are proved.
3. Message conversion for the two backends whose format disagrees with the
   neutral model in ways that produce 400s if handled naively — Anthropic, and
   the Responses API.

Every test here is `unit`: the in-memory transport binds no port and the
scripted streamer makes no network call.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from anthropic.types import (
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
)
from fastmcp import Client
from openai.types.chat import ChatCompletionChunk
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseIncompleteEvent,
    ResponseOutputItemAddedEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseTextDeltaEvent,
)

from slife2.config import ModelSettings
from slife2.llm.anthropic_server import (
    DEFAULT_MAX_TOKENS,
    to_anthropic_blocks,
    to_anthropic_messages,
    to_anthropic_tools,
)
from slife2.llm.anthropic_server import (
    build_request as anthropic_build_request,
)
from slife2.llm.anthropic_server import translate as anthropic_translate
from slife2.llm.base import Chunk, Finish, Streamer, ToolCallDelta
from slife2.llm.openai_responses_server import (
    build_request as responses_build_request,
)
from slife2.llm.openai_responses_server import (
    to_responses_input,
    to_responses_tools,
)
from slife2.llm.openai_responses_server import (
    translate as responses_translate,
)
from slife2.llm.openai_server import (
    build_request as openai_build_request,
)
from slife2.llm.openai_server import (
    translate as openai_translate,
)
from slife2.llm.server_common import ToolCallAccumulator, build_llm_server
from slife2.llm.wire import decode_chunk
from slife2.messages import Message, ToolCall, ToolSpec

pytestmark = pytest.mark.unit


# --- OpenAI adapter ----------------------------------------------------------


def _oa(**chunk: Any) -> ChatCompletionChunk:
    return ChatCompletionChunk.model_validate(
        {
            "id": "1",
            "created": 0,
            "model": "m",
            "object": "chat.completion.chunk",
            **chunk,
        }
    )


def test_openai_text_delta() -> None:
    event = _oa(
        choices=[{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]
    )
    assert openai_translate(event) == [Chunk(text="hi")]


def test_openai_tool_call_fragments_carry_index_id_and_name() -> None:
    event = _oa(
        choices=[
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "calc", "arguments": '{"e"'},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ]
    )
    (chunk,) = openai_translate(event)
    (delta,) = chunk.tool_call_deltas
    assert delta == ToolCallDelta(index=0, id="c1", name="calc", arguments_delta='{"e"')


def test_openai_finish_reason_becomes_a_finish_event() -> None:
    event = _oa(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}])
    assert openai_translate(event) == [Finish(stop_reason="tool_calls")]


def test_openai_content_and_finish_in_one_chunk_yield_two_events() -> None:
    """Why `translate` returns a list: the last content chunk carries the stop."""
    event = _oa(
        choices=[{"index": 0, "delta": {"content": "bye"}, "finish_reason": "stop"}]
    )
    assert openai_translate(event) == [
        Chunk(text="bye"),
        Finish(stop_reason="stop"),
    ]


def test_openai_usage_only_chunk() -> None:
    """OpenAI reports usage on a chunk whose `choices` list is empty."""
    event = _oa(
        choices=[],
        usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    )
    (chunk,) = openai_translate(event)
    assert chunk.usage is not None
    assert (chunk.usage.prompt_tokens, chunk.usage.completion_tokens) == (3, 4)


def test_openai_usage_on_a_chunk_that_also_carries_a_choice() -> None:
    """DeepSeek's shape, and the reason usage is read before the choices branch.

    DeepSeek attaches token counts to the final chunk *with* a choice, where
    OpenAI sends them separately with an empty `choices` list.  Handling only
    the documented OpenAI shape reported zero tokens against a real DeepSeek
    endpoint -- a live call is what surfaced it.
    """
    event = _oa(
        choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
        usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    )
    usage_chunk, finish = openai_translate(event)
    assert usage_chunk.usage is not None
    assert (usage_chunk.usage.prompt_tokens, usage_chunk.usage.completion_tokens) == (
        3,
        4,
    )
    assert finish == Finish(stop_reason="stop")


def test_openai_ignores_an_empty_chunk() -> None:
    assert openai_translate(_oa(choices=[{"index": 0, "delta": {}}])) == []


# --- Anthropic adapter -------------------------------------------------------


def test_anthropic_text_delta() -> None:
    event = RawContentBlockDeltaEvent.model_validate(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hi"},
        }
    )
    assert anthropic_translate(event) == [Chunk(text="hi")]


def test_anthropic_tool_use_block_start_carries_id_and_name() -> None:
    event = RawContentBlockStartEvent.model_validate(
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "tu1",
                "name": "calc",
                "input": {},
            },
        }
    )
    (chunk,) = anthropic_translate(event)
    (delta,) = chunk.tool_call_deltas
    assert (delta.index, delta.id, delta.name) == (1, "tu1", "calc")


def test_anthropic_input_json_delta() -> None:
    event = RawContentBlockDeltaEvent.model_validate(
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"e"'},
        }
    )
    (chunk,) = anthropic_translate(event)
    assert chunk.tool_call_deltas == (ToolCallDelta(index=1, arguments_delta='{"e"'),)


def test_anthropic_usage_is_split_across_two_events_without_double_counting() -> None:
    """`message_start` reports output_tokens: 1 and `message_delta` reports a
    *cumulative* 22.  Adding both events' full usage would report 23.

    Each half therefore contributes only the field it owns.
    """
    start = RawMessageStartEvent.model_validate(
        {
            "type": "message_start",
            "message": {
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": "x",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 11, "output_tokens": 1},
            },
        }
    )
    delta = RawMessageDeltaEvent.model_validate(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 22},
        }
    )

    (start_chunk,) = anthropic_translate(start)
    delta_events = anthropic_translate(delta)

    assert start_chunk.usage is not None
    assert start_chunk.usage.prompt_tokens == 11
    assert start_chunk.usage.completion_tokens == 0

    usage_chunk, finish = delta_events
    assert usage_chunk.usage is not None
    assert usage_chunk.usage.prompt_tokens == 0
    assert usage_chunk.usage.completion_tokens == 22
    assert finish == Finish(stop_reason="tool_use")


def test_anthropic_forwards_thinking_as_its_own_kind() -> None:
    """Reasoning must arrive as `thinking`, never mixed into the answer.

    The two are displayed differently — reasoning is folded away — so an adapter
    that let a `thinking_delta` through as text would put the model's private
    working into the middle of its reply, which is hard to notice and impossible
    to undo after the fact.
    """
    event = RawContentBlockDeltaEvent.model_validate(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "hmm"},
        }
    )
    assert anthropic_translate(event) == [Chunk(thinking="hmm")]


def test_openai_forwards_reasoning_content() -> None:
    event = _oa(
        choices=[
            {
                "index": 0,
                "delta": {"reasoning_content": "thinking"},
                "finish_reason": None,
            }
        ]
    )
    assert openai_translate(event) == [Chunk(thinking="thinking")]


# --- Anthropic message conversion -------------------------------------------


def test_system_messages_are_hoisted_out_of_the_list() -> None:
    system, messages = to_anthropic_messages(
        [Message(role="system", content="be nice"), Message(role="user", content="hi")]
    )
    assert system == "be nice"
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_tool_results_and_following_text_merge_into_one_user_turn() -> None:
    """Strict endpoints 400 on two same-role turns in a row."""
    _, messages = to_anthropic_messages(
        [
            Message(role="user", content="2+2?"),
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="c1", name="calc", arguments={"e": "2+2"})],
            ),
            Message(role="tool", content="4", tool_call_id="c1"),
            Message(role="user", content="thanks"),
        ]
    )
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "c1", "content": "4"},
        {"type": "text", "text": "thanks"},
    ]


def test_parallel_tool_results_merge_into_one_user_turn() -> None:
    _, messages = to_anthropic_messages(
        [
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(id="c1", name="a", arguments={}),
                    ToolCall(id="c2", name="b", arguments={}),
                ],
            ),
            Message(role="tool", content="1", tool_call_id="c1"),
            Message(role="tool", content="2", tool_call_id="c2"),
        ]
    )
    assert [m["role"] for m in messages] == ["assistant", "user"]
    assert len(messages[-1]["content"]) == 2


def test_assistant_text_and_tool_calls_become_blocks() -> None:
    _, messages = to_anthropic_messages(
        [
            Message(
                role="assistant",
                content="let me check",
                tool_calls=[ToolCall(id="c1", name="calc", arguments={"e": "1"})],
            )
        ]
    )
    assert messages[0]["content"] == [
        {"type": "text", "text": "let me check"},
        {"type": "tool_use", "id": "c1", "name": "calc", "input": {"e": "1"}},
    ]


def test_empty_assistant_turn_is_dropped() -> None:
    _, messages = to_anthropic_messages([Message(role="assistant")])
    assert messages == []


def test_tools_use_input_schema() -> None:
    tools = to_anthropic_tools(
        [ToolSpec(name="calc", description="maths", parameters={"type": "object"})]
    )
    assert tools == [
        {"name": "calc", "description": "maths", "input_schema": {"type": "object"}}
    ]


# --- Responses adapter -------------------------------------------------------
#
# The Responses API streams events, not deltas on a choice — so every test here
# names the event type it is driving, and `translate` dispatches on it.  The
# objects are real SDK ones for the same reason as above: what we adapt to is
# what the SDK actually produces.


def _resp(**over: Any) -> dict[str, Any]:
    """A Response body, carrying the fields the model marks required."""
    base: dict[str, Any] = {
        "id": "resp_1",
        "created_at": 0.0,
        "model": "m",
        "object": "response",
        "output": [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }
    base.update(over)
    return base


def _completed(**over: Any) -> ResponseCompletedEvent:
    return ResponseCompletedEvent.model_validate(
        {
            "type": "response.completed",
            "sequence_number": 9,
            "response": _resp(**over),
        }
    )


def _text_delta(text: str) -> ResponseTextDeltaEvent:
    return ResponseTextDeltaEvent.model_validate(
        {
            "type": "response.output_text.delta",
            "content_index": 0,
            "delta": text,
            "item_id": "i",
            "logprobs": [],
            "output_index": 0,
            "sequence_number": 1,
        }
    )


def test_responses_text_delta() -> None:
    assert responses_translate(_text_delta("hi")) == [Chunk(text="hi")]


def test_responses_reasoning_summary_is_its_own_kind() -> None:
    """A reasoning summary must arrive as `thinking`, never as the answer.

    This API returns only a *summary* of the model's reasoning, which is still
    the model talking to itself — folded into the reply it would read as part of
    the answer, and no test elsewhere would notice.
    """
    event = ResponseReasoningSummaryTextDeltaEvent.model_validate(
        {
            "type": "response.reasoning_summary_text.delta",
            "delta": "hmm",
            "item_id": "i",
            "output_index": 0,
            "summary_index": 0,
            "sequence_number": 2,
        }
    )
    assert responses_translate(event) == [Chunk(thinking="hmm")]


def test_responses_function_call_item_announces_call_id_and_name() -> None:
    """`call_id` is this API's name for the id a tool result refers back to."""
    event = ResponseOutputItemAddedEvent.model_validate(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "sequence_number": 3,
            "item": {
                "type": "function_call",
                "call_id": "c1",
                "name": "calc",
                "arguments": "",
            },
        }
    )
    (chunk,) = responses_translate(event)
    assert chunk.tool_call_deltas == (ToolCallDelta(index=0, id="c1", name="calc"),)


def test_responses_arguments_delta_is_keyed_by_output_index() -> None:
    """Not by `item_id` — the index is what the accumulator keys on.

    Each function call is its own output item, so the index is stable and unique
    across the whole response: the job Anthropic's content-block index does.
    """
    event = ResponseFunctionCallArgumentsDeltaEvent.model_validate(
        {
            "type": "response.function_call_arguments.delta",
            "delta": '{"e"',
            "item_id": "i",
            "output_index": 2,
            "sequence_number": 4,
        }
    )
    (chunk,) = responses_translate(event)
    assert chunk.tool_call_deltas == (ToolCallDelta(index=2, arguments_delta='{"e"'),)


def test_responses_completion_carries_usage_and_a_stop_reason() -> None:
    """Usage arrives only on the terminal event, unlike chat-completions."""
    usage_chunk, finish = responses_translate(
        _completed(
            usage={
                "input_tokens": 3,
                "output_tokens": 4,
                "total_tokens": 7,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            }
        )
    )
    assert usage_chunk.usage is not None
    assert (usage_chunk.usage.prompt_tokens, usage_chunk.usage.completion_tokens) == (
        3,
        4,
    )
    assert finish == Finish(stop_reason="stop")


def test_responses_completion_with_a_function_call_says_tool_calls() -> None:
    """This API reports a status, not a finish reason.

    The distinction the rest of the system speaks in has to be recovered from
    the output, so that a caller comparing stop reasons across backends is not
    told a different story by this one.
    """
    (finish,) = responses_translate(
        _completed(
            output=[
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "calc",
                    "arguments": "{}",
                }
            ]
        )
    )
    assert finish == Finish(stop_reason="tool_calls")


def test_responses_an_incomplete_response_keeps_its_reason() -> None:
    """Cut off at the output cap is an ordinary outcome, not an error.

    The reason travels through as the stop reason, so the caller can tell a
    truncated answer from a finished one.
    """
    event = ResponseIncompleteEvent.model_validate(
        {
            "type": "response.incomplete",
            "sequence_number": 9,
            "response": _resp(
                status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
            ),
        }
    )
    assert responses_translate(event) == [Finish(stop_reason="max_output_tokens")]


def test_responses_a_failed_response_raises_with_the_providers_words() -> None:
    """Not an empty result — that is indistinguishable from a working answer.

    A blank reply would be recorded as a turn that succeeded, and the
    only symptom would be nothing at all.  Raising is what the rest of the
    system is built for: the backend re-raises and the TUI prints the message.
    """
    event = ResponseFailedEvent.model_validate(
        {
            "type": "response.failed",
            "sequence_number": 9,
            "response": _resp(
                status="failed",
                error={"code": "server_error", "message": "upstream exploded"},
            ),
        }
    )
    with pytest.raises(RuntimeError, match="upstream exploded"):
        responses_translate(event)


def test_responses_an_error_event_raises() -> None:
    event = ResponseErrorEvent.model_validate(
        {
            "type": "error",
            "code": "invalid_request",
            "message": "no such model",
            "param": None,
            "sequence_number": 1,
        }
    )
    with pytest.raises(RuntimeError, match="no such model"):
        responses_translate(event)


def test_responses_a_completed_response_carrying_an_error_raises() -> None:
    """A response can complete *and* hold an error it recovered from.

    Answering with the text that arrived while ignoring it is how a partial
    answer gets recorded as a whole one.
    """
    with pytest.raises(RuntimeError, match="slow down"):
        responses_translate(
            _completed(error={"code": "rate_limit_exceeded", "message": "slow down"})
        )


def test_responses_an_unknown_event_is_not_an_error() -> None:
    """The event list grows; a new `response.*` event is not a failed turn."""
    event = ResponseCompletedEvent.model_validate(
        {"type": "response.completed", "sequence_number": 1, "response": _resp()}
    )
    event.type = "response.something.new"  # type: ignore[misc]
    assert responses_translate(event) == []


# --- Responses message conversion --------------------------------------------
#
# Four disagreements with the neutral model, and each one is a 400 if it is not
# handled: the system prompt is a parameter, tools are flat, calls and results
# are top-level items, and content parts are renamed.


def test_responses_system_messages_become_instructions() -> None:
    instructions, items = to_responses_input(
        [Message(role="system", content="be nice"), Message(role="user", content="hi")]
    )
    assert instructions == "be nice"
    assert items == [{"role": "user", "content": "hi"}]


def test_responses_several_system_turns_are_joined() -> None:
    instructions, _ = to_responses_input(
        [
            Message(role="system", content="one"),
            Message(role="user", content="hi"),
            Message(role="system", content="two"),
        ]
    )
    assert instructions == "one\n\ntwo"


def test_responses_a_plain_message_stays_a_plain_string() -> None:
    """Wrapping every string in a one-element list buys nothing and costs
    readability in every request anyone ever inspects."""
    _, items = to_responses_input([Message(role="user", content="hi")])
    assert items == [{"role": "user", "content": "hi"}]


def test_responses_content_parts_are_renamed_and_reshaped() -> None:
    """An image nests a bare data URL here, not an `image_url` object."""
    _, items = to_responses_input(
        [
            Message(
                role="user",
                content=[
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            )
        ]
    )
    assert items[0]["content"] == [
        {"type": "input_text", "text": "what is this?"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,AAAA",
            "detail": "auto",
        },
    ]


def test_responses_a_remote_image_is_not_fetched() -> None:
    """Fetching an address a prompt named is a request nobody made.

    Dropping is safe because the agent server has already refused to send one.
    """
    _, items = to_responses_input(
        [
            Message(
                role="user",
                content=[
                    {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}},
                    {"type": "text", "text": "hi"},
                ],
            )
        ]
    )
    assert items[0]["content"] == [{"type": "input_text", "text": "hi"}]


def test_responses_tool_results_are_their_own_items() -> None:
    """The part with no analogue in either other backend.

    A tool result is not a message with a role here — it is a `function_call_output`
    item addressed by the id the call announced.
    """
    _, items = to_responses_input(
        [Message(role="tool", content="4", tool_call_id="c1")]
    )
    assert items == [{"type": "function_call_output", "call_id": "c1", "output": "4"}]


def test_responses_an_assistant_turn_is_its_text_then_its_calls() -> None:
    """The order the API itself emits, so a history round-trips unchanged."""
    _, items = to_responses_input(
        [
            Message(
                role="assistant",
                content="let me check",
                tool_calls=[ToolCall(id="c1", name="calc", arguments={"e": "1"})],
            )
        ]
    )
    assert items == [
        {"role": "assistant", "content": "let me check"},
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "calc",
            "arguments": '{"e": "1"}',
        },
    ]


def test_responses_an_assistant_turn_with_only_calls_has_no_message_item() -> None:
    _, items = to_responses_input(
        [Message(role="assistant", tool_calls=[ToolCall(id="c1", name="calc")])]
    )
    assert [item["type"] for item in items] == ["function_call"]


def test_responses_an_empty_turn_is_dropped() -> None:
    _, items = to_responses_input([Message(role="assistant")])
    assert items == []


def test_responses_tools_are_flat() -> None:
    """No `function` wrapper — the whole difference from chat-completions."""
    tools = to_responses_tools(
        [ToolSpec(name="calc", description="maths", parameters={"type": "object"})]
    )
    assert tools == [
        {
            "type": "function",
            "name": "calc",
            "description": "maths",
            "parameters": {"type": "object"},
        }
    ]


# --- tool-call reassembly ----------------------------------------------------


def test_accumulator_joins_fragmented_arguments() -> None:
    accumulator = ToolCallAccumulator()
    accumulator.add(
        ToolCallDelta(index=0, id="c1", name="calc", arguments_delta='{"e"')
    )
    accumulator.add(ToolCallDelta(index=0, arguments_delta=': "2+'))
    accumulator.add(ToolCallDelta(index=0, arguments_delta='2"}'))
    assert accumulator.complete() == [
        ToolCall(id="c1", name="calc", arguments={"e": "2+2"})
    ]


def test_accumulator_keeps_the_first_id_and_name() -> None:
    """Providers send id/name once, then only argument fragments."""
    accumulator = ToolCallAccumulator()
    accumulator.add(ToolCallDelta(index=0, id="c1", name="calc"))
    accumulator.add(ToolCallDelta(index=0, arguments_delta="{}"))
    (call,) = accumulator.complete()
    assert (call.id, call.name) == ("c1", "calc")


def test_accumulator_orders_by_index() -> None:
    accumulator = ToolCallAccumulator()
    accumulator.add(ToolCallDelta(index=2, id="c3", name="c", arguments_delta="{}"))
    accumulator.add(ToolCallDelta(index=0, id="c1", name="a", arguments_delta="{}"))
    accumulator.add(ToolCallDelta(index=1, id="c2", name="b", arguments_delta="{}"))
    assert [c.id for c in accumulator.complete()] == ["c1", "c2", "c3"]


def test_accumulator_synthesises_an_id_when_the_provider_sent_none() -> None:
    """A tool result refers back by id, so one must exist."""
    accumulator = ToolCallAccumulator()
    accumulator.add(ToolCallDelta(index=0, name="calc", arguments_delta="{}"))
    (call,) = accumulator.complete()
    assert call.id == "call_0"


def test_accumulator_degrades_unparseable_arguments_to_empty() -> None:
    """The tool reports the problem to the model; the turn does not die."""
    accumulator = ToolCallAccumulator()
    accumulator.add(
        ToolCallDelta(index=0, id="c1", name="calc", arguments_delta="{oops")
    )
    (call,) = accumulator.complete()
    assert call.arguments == {}


# --- end to end over the in-memory transport ---------------------------------


def scripted(*events) -> Streamer:
    """A streamer that replays fixed provider events, ignoring its input."""

    async def stream(
        provider: str, messages: list[Message], tools: list[ToolSpec], model: str
    ) -> AsyncIterator[Chunk | Finish]:
        for event in events:
            yield event

    return stream


async def call_stream_chat(server, **arguments):
    """Call `stream_chat` in memory, returning (result, decoded chunks)."""
    seen: list[Chunk] = []

    async def on_progress(
        progress: float, total: float | None, message: str | None
    ) -> None:
        decoded = decode_chunk(message or "")
        assert decoded is not None, f"not a chunk payload: {message!r}"
        seen.append(decoded)

    async with Client(server) as client:
        result = await client.call_tool(
            "stream_chat", {"provider": "p", **arguments}, progress_handler=on_progress
        )
    return result, seen


@pytest.mark.asyncio
async def test_stream_chat_returns_the_assembled_text() -> None:
    server = build_llm_server(
        name="t",
        streamer=scripted(Chunk(text="he"), Chunk(text="llo"), Finish("stop")),
    )
    result, chunks = await call_stream_chat(
        server, messages=[{"role": "user", "content": "hi"}], tools=[], model="m"
    )

    assert result.data["text"] == "hello"
    assert result.data["stop_reason"] == "stop"
    assert [c.text for c in chunks] == ["he", "llo"]


@pytest.mark.asyncio
async def test_stream_chat_reassembles_a_fragmented_tool_call() -> None:
    """The whole reason tool-call assembly lives in this server.

    The agent loop receives a complete call and needs no accumulator; the
    fragments never cross the MCP hop as fragments.
    """
    server = build_llm_server(
        name="t",
        streamer=scripted(
            Chunk(text="checking"),
            Chunk(tool_call_deltas=(ToolCallDelta(0, id="c1", name="calc"),)),
            Chunk(tool_call_deltas=(ToolCallDelta(0, arguments_delta='{"e"'),)),
            Chunk(tool_call_deltas=(ToolCallDelta(0, arguments_delta=': "2+2"}'),)),
            Finish("tool_calls"),
        ),
    )
    result, _ = await call_stream_chat(
        server, messages=[{"role": "user", "content": "2+2"}], tools=[], model="m"
    )

    assert result.data["text"] == "checking"
    assert result.data["stop_reason"] == "tool_calls"
    (call,) = result.data["tool_calls"]
    assert call["id"] == "c1"
    assert call["function"]["name"] == "calc"
    assert json.loads(call["function"]["arguments"]) == {"e": "2+2"}


@pytest.mark.asyncio
async def test_stream_chat_accumulates_usage_across_chunks() -> None:
    from slife2.messages import Usage

    server = build_llm_server(
        name="t",
        streamer=scripted(
            Chunk(text="x", usage=Usage(prompt_tokens=11)),
            Chunk(text="y", usage=Usage(completion_tokens=22)),
            Finish("stop"),
        ),
    )
    result, _ = await call_stream_chat(
        server, messages=[{"role": "user", "content": "hi"}], tools=[], model="m"
    )
    assert result.data["usage"] == {"prompt_tokens": 11, "completion_tokens": 22}


@pytest.mark.asyncio
async def test_stream_chat_works_without_a_progress_handler() -> None:
    """No progress token means no notifications, but the same result.

    This is the property that lets a non-streaming client — a smoke test, a
    script — call the same tool and get a correct answer.
    """
    server = build_llm_server(
        name="t", streamer=scripted(Chunk(text="hello"), Finish("stop"))
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "stream_chat",
            {
                "provider": "p",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [],
                "model": "m",
            },
        )
    assert result.data["text"] == "hello"


@pytest.mark.asyncio
async def test_stream_chat_receives_the_neutral_message_shape() -> None:
    """The streamer must see parsed Message/ToolSpec objects, not raw dicts."""
    seen: list[tuple[list[Message], list[ToolSpec]]] = []

    async def streamer(provider, messages, tools, model):
        seen.append((messages, tools))
        yield Finish("stop")

    server = build_llm_server(name="t", streamer=streamer)
    await call_stream_chat(
        server,
        messages=[
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "calc", "arguments": '{"e": "1"}'},
                    }
                ],
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "calc",
                    "description": "maths",
                    "parameters": {"type": "object"},
                },
            }
        ],
        model="m",
    )

    (messages, tools) = seen[0]
    assert isinstance(messages[0], Message)
    assert messages[1].tool_calls == [
        ToolCall(id="c1", name="calc", arguments={"e": "1"})
    ]
    assert tools == [
        ToolSpec(name="calc", description="maths", parameters={"type": "object"})
    ]


@pytest.mark.asyncio
async def test_stream_chat_is_listed_as_a_tool() -> None:
    server = build_llm_server(name="t", streamer=scripted())
    async with Client(server) as client:
        tools = await client.list_tools()
    assert [t.name for t in tools] == ["stream_chat"]


# --- the request an adapter builds -------------------------------------------
#
# The parameter surface is asserted directly because *absence* is a real choice
# here, not an oversight: `None` means "send nothing and let the gateway
# decide", and a gateway that rejects a temperature it did not ask for is a real
# thing.  A test that only checked the values would miss the whole point.


def test_openai_sends_only_what_was_configured() -> None:
    bare = ModelSettings(model="m")
    request = openai_build_request(
        [Message(role="user", content="hi")], [], bare, stream_usage=False
    )
    assert "temperature" not in request
    assert "top_p" not in request
    assert "max_tokens" not in request
    assert "thinking" not in request

    tuned = ModelSettings(model="m", temperature=0.3, top_p=0.9, max_tokens=100)
    request = openai_build_request(
        [Message(role="user", content="hi")], [], tuned, stream_usage=True
    )
    assert request["temperature"] == 0.3
    assert request["top_p"] == 0.9
    assert request["max_tokens"] == 100


def test_openai_thinking_is_opt_in() -> None:
    """The OpenAI-compatible wire has no standard thinking field.

    So an absent `compat.thinking` sends nothing, however much the model likes
    to reason — the models that reason natively do it unasked and report it in
    `reasoning_content`, which the adapter reads either way.
    """
    native = ModelSettings(model="m", reasoning=True)
    assert "thinking" not in openai_build_request([], [], native, stream_usage=True)

    asked = ModelSettings(
        model="m", reasoning=True, thinking="enabled", max_tokens=1000
    )
    request = openai_build_request([], [], asked, stream_usage=True)
    assert request["thinking"]["type"] == "enabled"
    # Half the budget, because the other half is what the answer is made of.
    assert request["thinking"]["budget_tokens"] == 500

    refused = ModelSettings(model="m", reasoning=True, thinking="disabled")
    assert (
        openai_build_request([], [], refused, stream_usage=True)["thinking"]["type"]
        == "disabled"
    )

    omitted = ModelSettings(model="m", thinking="omit")
    assert "thinking" not in openai_build_request([], [], omitted, stream_usage=True)


def test_anthropic_sends_max_tokens_always() -> None:
    """Anthropic requires it, so absent cannot mean omit here."""
    request = anthropic_build_request([], [], ModelSettings(model="m"))
    assert request["max_tokens"] == DEFAULT_MAX_TOKENS

    request = anthropic_build_request([], [], ModelSettings(model="m", max_tokens=999))
    assert request["max_tokens"] == 999


def test_anthropic_enables_thinking_for_a_reasoning_model() -> None:
    """Unlike the OpenAI-compatible wire, this parameter is part of the protocol.

    A model that reasons natively will not do it unless asked, and there is no
    ambiguity about the shape — so `reasoning: true` is enough.
    """
    plain = anthropic_build_request([], [], ModelSettings(model="m", max_tokens=8000))
    assert "thinking" not in plain

    reasoning = ModelSettings(model="m", reasoning=True, max_tokens=8000)
    request = anthropic_build_request([], [], reasoning)
    assert request["thinking"]["type"] == "enabled"
    assert request["thinking"]["budget_tokens"] == 4000

    # ...but a gateway that rejects the field can still say so.
    omitted = ModelSettings(model="m", reasoning=True, thinking="omit")
    assert "thinking" not in anthropic_build_request([], [], omitted)


def test_anthropic_drops_sampling_when_thinking_is_on() -> None:
    """The real API rejects temperature and top_p alongside thinking.

    Sending them would be a 400 on every call, which is the kind of thing worth
    encoding in a test rather than discovering against a live endpoint.
    """
    settings = ModelSettings(
        model="m", reasoning=True, temperature=0.7, top_p=1.0, max_tokens=8000
    )
    request = anthropic_build_request([], [], settings)
    assert "thinking" in request
    assert "temperature" not in request
    assert "top_p" not in request


def test_anthropic_thinking_budget_leaves_room_to_answer() -> None:
    """A model that spends everything thinking returns nothing."""
    request = anthropic_build_request(
        [], [], ModelSettings(model="m", reasoning=True, max_tokens=2000)
    )
    assert request["thinking"]["budget_tokens"] < 2000


def test_responses_sends_only_what_was_configured() -> None:
    bare = ModelSettings(model="m")
    request = responses_build_request([Message(role="user", content="hi")], [], bare)
    assert "temperature" not in request
    assert "top_p" not in request
    assert "max_output_tokens" not in request
    assert "reasoning" not in request
    assert "store" not in request

    tuned = ModelSettings(model="m", temperature=0.3, top_p=0.9, max_tokens=100)
    request = responses_build_request([Message(role="user", content="hi")], [], tuned)
    assert request["temperature"] == 0.3
    assert request["top_p"] == 0.9
    # This API calls the output cap something else, and does not require it —
    # unlike Anthropic, where an absent one has to become a real number.
    assert request["max_output_tokens"] == 100


def test_responses_thinking_is_opt_in() -> None:
    """`reasoning: true` asks for a summary; the model reasons natively
    otherwise, and a model the config does not claim reasons is not sent a
    parameter it has no use for."""
    plain = ModelSettings(model="m")
    assert "reasoning" not in responses_build_request([], [], plain)

    reasoning = ModelSettings(model="m", reasoning=True)
    assert responses_build_request([], [], reasoning)["reasoning"] == {
        "summary": "auto"
    }

    # ...but a gateway that rejects the field can still say so, as on the other
    # two backends — the gateways that need the escape hatch are the same ones.
    for setting in ("omit", "disabled"):
        refused = ModelSettings(model="m", reasoning=True, thinking=setting)
        assert "reasoning" not in responses_build_request([], [], refused), setting


def test_responses_store_is_absent_unless_the_config_asks() -> None:
    """Three states, and the middle one is the default.

    Absent means *send nothing*, which leaves each endpoint's own default in
    place — the API's default being to keep the response.  That is deliberate:
    Responses-compatible endpoints differ in whether they implement the field at
    all, so a server that picked a value for every call would break against the
    ones that do not accept it.
    """
    assert "store" not in responses_build_request([], [], ModelSettings(model="m"))
    assert (
        responses_build_request([], [], ModelSettings(model="m", store=False))["store"]
        is False
    )
    assert (
        responses_build_request([], [], ModelSettings(model="m", store=True))["store"]
        is True
    )


def test_reasoning_is_read_whatever_the_gateway_calls_it() -> None:
    """There is no standard field name, and a missed one is invisible.

    Nothing errors when the spelling is wrong: the answer is correct, the turn
    succeeds, and the only symptom is a line in the transcript that should have
    been there.  So every name in use is read, and the SDK's `extra` bag is
    checked too — an unrecognised field lands there rather than being dropped,
    where `getattr` would never find it.
    """
    for field in ("reasoning_content", "reasoning", "thinking"):
        event = _oa(
            choices=[{"index": 0, "delta": {field: "why"}, "finish_reason": None}]
        )
        assert openai_translate(event) == [Chunk(thinking="why")], field


def test_reasoning_in_an_extra_field_is_still_read() -> None:
    """An SDK that does not know a field keeps it in `model_extra`."""
    event = _oa(choices=[{"index": 0, "delta": {}, "finish_reason": None}])
    # `model_validate` puts unknown keys in `model_extra`, which is the point.
    event = type(event).model_validate(
        {
            "id": "1",
            "created": 0,
            "model": "m",
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {"reasoning_content": "why"},
                    "finish_reason": None,
                }
            ],
        }
    )
    assert openai_translate(event) == [Chunk(thinking="why")]


def test_an_empty_reasoning_field_is_not_an_event() -> None:
    event = _oa(
        choices=[
            {"index": 0, "delta": {"reasoning_content": ""}, "finish_reason": None}
        ]
    )
    assert openai_translate(event) == []


# --- images ------------------------------------------------------------------
#
# The two APIs disagree about images the usual way: OpenAI nests a data URL
# under `image_url`, Anthropic wants the media type and the payload as separate
# fields.  The neutral form is OpenAI's, so the conversion lives here.


def test_a_string_becomes_one_text_block() -> None:
    assert to_anthropic_blocks("hello") == [{"type": "text", "text": "hello"}]
    assert to_anthropic_blocks("") == []
    assert to_anthropic_blocks(None) == []


def test_content_parts_become_text_and_image_blocks() -> None:
    blocks = to_anthropic_blocks(
        [
            {"type": "text", "text": "what is this?"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
            },
        ]
    )
    assert blocks == [
        {"type": "text", "text": "what is this?"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
        },
    ]


def test_a_remote_image_url_is_not_fetched() -> None:
    """Fetching an address a prompt named is a request nobody made.

    So it is dropped here rather than turned into a request — and dropping is
    safe because the agent server has already refused to send it.
    """
    blocks = to_anthropic_blocks(
        [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]
    )
    assert blocks == []


def test_an_unrecognised_part_is_dropped() -> None:
    """Passing an unknown block through is a 400 on every call containing one."""
    blocks = to_anthropic_blocks(
        [{"type": "video", "video": {}}, {"type": "text", "text": "hi"}]
    )
    assert blocks == [{"type": "text", "text": "hi"}]


def test_a_user_message_with_an_image_survives_conversion() -> None:
    _, messages = to_anthropic_messages(
        [
            Message(
                role="user",
                content=[
                    {"type": "text", "text": "look"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,BBBB"},
                    },
                ],
            )
        ]
    )
    assert messages[0]["content"][0] == {"type": "text", "text": "look"}
    assert messages[0]["content"][1]["source"]["media_type"] == "image/jpeg"


# --- the `main` every model server shares ------------------------------------

#: One OpenAI-compatible provider and nothing else, so every *other* protocol
#: has no provider to serve.
ONLY_OPENAI = """
providers:
  local:
    api: openai-completions
    base_url: https://example.test/v1
    api_key: ${SLIFE2_TEST_KEY:-none}
    models:
      - model: big
default: local/big
"""


def _point_at(tmp_path, monkeypatch) -> None:
    (tmp_path / "slife2.yaml").write_text(ONLY_OPENAI, encoding="utf-8")
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(tmp_path))


def test_a_protocol_no_provider_uses_stops_with_an_answer(
    tmp_path, monkeypatch, capsys
) -> None:
    """Rather than starting a server that can answer nothing.

    `serve_backend` is the branch both model servers used to spell out
    separately and identically, and this is the branchy half of it — untested
    while it was duplicated, which is the other reason to have one copy.
    """
    _point_at(tmp_path, monkeypatch)
    from slife2.llm import anthropic_server

    assert anthropic_server.main([]) == 2
    assert "no anthropic-messages provider" in capsys.readouterr().out


def test_the_matching_protocol_serves_every_provider_of_it(
    tmp_path, monkeypatch
) -> None:
    """The other half of the same branch: a protocol that *is* in the config
    goes on to serve, carrying only that protocol's providers.

    `serve` is replaced rather than run, because the real one blocks on a
    socket — what is being asserted is what reaches it.
    """
    from slife2.llm import openai_server, server_common

    _point_at(tmp_path, monkeypatch)
    served: list[Any] = []
    monkeypatch.setattr(server_common, "serve", lambda *a, **k: served.append(a))

    assert openai_server.main([]) == 0
    (mcp, address, _args) = served[0]
    # The address is the one the *protocol* listens on, not the provider's — a
    # provider has no address of its own.
    assert address.url == "http://127.0.0.1:8001/mcp"
    # And it is this server, by the name a client checks it by.
    assert mcp.name == openai_server.SERVER_NAME


ONLY_RESPONSES = """
providers:
  oai:
    api: openai-responses
    base_url: https://example.test/v1
    api_key: ${SLIFE2_TEST_KEY:-none}
    models:
      - model: big
default: oai/big
"""


def test_responses_stops_with_an_answer_when_its_config_has_none(
    tmp_path, monkeypatch, capsys
) -> None:
    """The third backend takes the same branch as the other two, because it
    takes the same `main` — which is the point of there being one."""
    _point_at(tmp_path, monkeypatch)
    from slife2.llm import openai_responses_server

    assert openai_responses_server.main([]) == 2
    assert "no openai-responses provider" in capsys.readouterr().out


def test_responses_is_served_on_its_own_port(tmp_path, monkeypatch) -> None:
    """A third protocol gets a third address, keyed by the `api` it speaks.

    Sharing a port with the chat-completions server is the mistake this
    catches: both are "openai", and both would be reachable at :8001 if the
    registry keyed them by anything but the protocol.
    """
    from slife2.llm import openai_responses_server, server_common

    (tmp_path / "slife2.yaml").write_text(ONLY_RESPONSES, encoding="utf-8")
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(tmp_path))
    served: list[Any] = []
    monkeypatch.setattr(server_common, "serve", lambda *a, **k: served.append(a))

    assert openai_responses_server.main([]) == 0
    (mcp, address, _args) = served[0]
    assert address.url == "http://127.0.0.1:8003/mcp"
    assert mcp.name == openai_responses_server.SERVER_NAME
