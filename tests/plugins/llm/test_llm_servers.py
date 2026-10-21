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
import logging
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
    ResponseTextDoneEvent,
)

from slife2.libs.host.config import Config, ModelSettings, ProviderSettings
from slife2.libs.model.base import Chunk, Finish, Streamer, ToolCallDelta
from slife2.libs.model.wire import decode_chunk
from slife2.libs.turn.messages import Message, ToolCall, ToolSpec, Usage
from slife2.plugins.llm import openai_server
from slife2.plugins.llm.anthropic_server import (
    DEFAULT_MAX_TOKENS,
    as_delta,
    normalised_stop,
    to_anthropic_blocks,
    to_anthropic_messages,
    to_anthropic_tools,
)
from slife2.plugins.llm.anthropic_server import (
    build_request as anthropic_build_request,
)
from slife2.plugins.llm.anthropic_server import translate as anthropic_translate
from slife2.plugins.llm.openai_responses_server import (
    build_request as responses_build_request,
)
from slife2.plugins.llm.openai_responses_server import (
    to_responses_input,
    to_responses_tools,
)
from slife2.plugins.llm.openai_responses_server import (
    translate as responses_translate,
)
from slife2.plugins.llm.openai_server import (
    build_request as openai_build_request,
)
from slife2.plugins.llm.openai_server import (
    to_provider_messages as openai_provider_messages,
)
from slife2.plugins.llm.openai_server import (
    translate as openai_translate,
)
from slife2.plugins.llm.server_common import ToolCallAccumulator, build_llm_server

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


def test_openai_a_gateway_streaming_the_deprecated_function_call_is_read() -> None:
    """**The old spelling of the same call, and it used to vanish.**

    `delta.function_call` is what the API streamed before `tool_calls` existed —
    one call, no index, no id — and a gateway that still speaks it (or a proxy
    translating for an endpoint that does) carries the model's whole call there.
    Reading only `tool_calls` meant the call disappeared: no tool ran, the turn
    was recorded as a plain answer, and nothing anywhere said a call had been
    made.  No warning fired either — the field is one the SDK declares, so
    `_reasoning`'s unknown-field report has nothing to say about it.

    Translated rather than named, because the field is legible: it holds what
    `tool_calls[0].function` holds, index 0 is the only index the one-call form
    had, and the id it lacks is the one `ToolCallAccumulator.complete`
    synthesises for every call that arrives without one.
    """
    event = _oa(
        choices=[
            {
                "index": 0,
                "delta": {"function_call": {"name": "calc", "arguments": '{"e"'}},
                "finish_reason": None,
            }
        ]
    )
    (chunk,) = openai_translate(event)
    assert chunk.tool_call_deltas == (
        ToolCallDelta(index=0, name="calc", arguments_delta='{"e"'),
    )
    # And the fragments are one call once assembled, id and all.
    accumulator = ToolCallAccumulator()
    for delta in chunk.tool_call_deltas:
        accumulator.add(delta)
    accumulator.add(ToolCallDelta(index=0, arguments_delta=': "6*7"}'))
    (call,) = accumulator.complete()
    assert (call.name, call.arguments, call.id) == ("calc", {"e": "6*7"}, "call_0")


def test_openai_a_chunk_carrying_both_spellings_is_one_call() -> None:
    """A proxy translating for an old endpoint can send both, and the modern
    field is the one that is authoritative — reading both would dispatch the
    same call twice."""
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
                            "function": {"name": "calc", "arguments": "{}"},
                        }
                    ],
                    "function_call": {"name": "calc", "arguments": "{}"},
                },
                "finish_reason": None,
            }
        ]
    )
    (chunk,) = openai_translate(event)
    assert chunk.tool_call_deltas == (
        ToolCallDelta(index=0, id="c1", name="calc", arguments_delta="{}"),
    )


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


def test_openai_cached_prompt_tokens_are_read_from_details() -> None:
    """The standard spelling: the cached prefix is a detail of the prompt.

    `cached_tokens` is already inside `prompt_tokens` on this wire, so the
    breakdown is a part of the prompt and not a second count — the same rule
    `Usage` states for all three adapters.
    """
    event = _oa(
        choices=[],
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "prompt_tokens_details": {
                "cached_tokens": 7,
                "cache_write_tokens": 2,
            },
        },
    )
    (chunk,) = openai_translate(event)
    assert chunk.usage is not None
    assert (chunk.usage.prompt_tokens, chunk.usage.completion_tokens) == (10, 4)
    assert (chunk.usage.cache_read, chunk.usage.cache_creation) == (7, 2)


def test_openai_reads_deepseek_top_level_cache_hit_tokens() -> None:
    """DeepSeek reports the same fact under its own name, at the top level.

    The SDK keeps a field it does not model in `model_extra`, so `getattr`
    answers — measured against `openai` 3.24.
    """
    event = _oa(
        choices=[],
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "prompt_cache_hit_tokens": 7,
            "prompt_cache_miss_tokens": 3,
        },
    )
    (chunk,) = openai_translate(event)
    assert chunk.usage is not None
    assert chunk.usage.cache_read == 7


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
    # Said the way every backend says it — `slife2.plugins.llm.openai_responses_server`
    # normalises too, and `TurnResult.stop_reason` is read by the status line
    # and by `send_message`'s result, where two backends telling one story
    # different words is a caller branching on the backend it chose.
    assert finish == Finish(stop_reason="tool_calls")


def test_anthropic_cached_prompt_tokens_are_counted() -> None:
    """`input_tokens` is the uncached part, and the cache fields are the rest.

    A gateway with prompt caching on reports the cached prefix in two fields of
    its own, so counting only `input_tokens` undercounts the prompt by the whole
    of the cache — silently, because the number that comes out still looks like
    a plausible prompt size.
    """
    event = RawMessageStartEvent.model_validate(
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
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 900,
                    "cache_read_input_tokens": 4000,
                },
            },
        }
    )

    (chunk,) = anthropic_translate(event)

    assert chunk.usage is not None
    assert chunk.usage.prompt_tokens == 11 + 900 + 4000
    # And the two fields keep their own counts, because how much of that prompt
    # the cache served is the question the numbers are read for.
    assert chunk.usage.cache_creation == 900
    assert chunk.usage.cache_read == 4000


def test_anthropic_output_tokens_are_a_running_total() -> None:
    """The field is cumulative, and what the accumulator is given must not be.

    One `message_delta` is what the API sends, and a gateway that sends several
    repeats the running total in each.  Added up as they arrive, a two-delta
    stream would report twice the tokens the turn produced — in the number the
    status bar shows and the turn's own row records as the bill.
    """
    counted, seen = as_delta(Chunk(usage=Usage(completion_tokens=22)), 0)
    assert counted.usage == Usage(completion_tokens=22)
    assert seen == 22

    counted, seen = as_delta(Chunk(usage=Usage(completion_tokens=30)), seen)
    assert counted.usage == Usage(completion_tokens=8), "the difference, not the total"
    assert seen == 30

    counted, seen = as_delta(Chunk(usage=Usage(completion_tokens=30)), seen)
    assert counted.usage is None, "a repeated total has nothing to add"
    assert seen == 30


def test_anthropic_stop_reasons_speak_the_shared_vocabulary() -> None:
    """Every reason this protocol sends, in the words the system uses.

    The rest of the home reads `stop_reason` without knowing which backend
    answered: the loop decides whether to continue on `tool_calls`, and a
    reason this build has not heard of is passed through rather than folded
    into "stop" — a fact about the model, not a gap to paper over.
    """
    assert normalised_stop("end_turn") == "stop"
    assert normalised_stop("stop_sequence") == "stop"
    assert normalised_stop("tool_use") == "tool_calls"
    assert normalised_stop("max_tokens") == "length"
    assert normalised_stop("pause_turn") == "pause_turn"


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


def test_a_system_turn_carrying_blocks_is_still_a_prompt(caplog) -> None:
    """**Taken, and not dropped.**

    The system prompt is a string parameter here, so a system turn carrying
    content parts has to have its text taken out of them — and reading only
    `str` content meant such a turn contributed nothing at all, with no word
    anywhere: a prompt the caller wrote left the request (DESIGN.md §11.1).  A
    block the API has no field for is left out, and said.
    """
    system, _ = to_anthropic_messages(
        [
            Message(role="system", content=[{"type": "text", "text": "be nice"}]),
            Message(role="user", content="hi"),
        ]
    )
    assert system == "be nice", "the caller's prompt is in the request"

    with caplog.at_level("WARNING"):
        system, _ = to_anthropic_messages(
            [
                Message(
                    role="system",
                    content=[
                        {"type": "text", "text": "be nice"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                    ],
                ),
            ]
        )
    assert system == "be nice", "the text beside it is kept"
    assert "no field for" in caplog.text, "and the block it cannot carry is named"


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


def test_a_non_string_tool_result_is_rendered_not_emptied() -> None:
    """The defect: a result that was not a string became `""`.

    Anthropic's `tool_result.content` is legitimately a block list, so this is a
    shape a caller can really send — and replacing it with `""` dropped the
    tool's whole output from the request with nothing said, under a comment that
    called a non-string result a bug upstream and then guessed at it anyway.  It
    is *rendered* now, so the model still sees what the tool answered.
    """
    _, messages = to_anthropic_messages(
        [
            Message(
                role="tool",
                content=[{"type": "text", "text": "two files matched"}],
                tool_call_id="c1",
            )
        ]
    )
    (result,) = messages[0]["content"]
    assert result["type"] == "tool_result"
    assert result["content"] != ""
    assert "two files matched" in result["content"]

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
                "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 1},
                "output_tokens_details": {"reasoning_tokens": 0},
            }
        )
    )
    assert usage_chunk.usage is not None
    assert (usage_chunk.usage.prompt_tokens, usage_chunk.usage.completion_tokens) == (
        3,
        4,
    )
    assert (usage_chunk.usage.cache_read, usage_chunk.usage.cache_creation) == (2, 1)
    assert finish == Finish(stop_reason="stop")


def test_responses_completion_with_a_function_call_says_tool_calls() -> None:
    """This API reports a status, not a finish reason.

    The distinction the rest of the system speaks in has to be recovered from
    the output, so that a caller comparing stop reasons across backends is not
    told a different story by this one.
    """
    events = responses_translate(
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

    assert Finish(stop_reason="tool_calls") in events


def test_responses_fills_in_arguments_that_never_streamed() -> None:
    """An endpoint that sends the call whole must not leave the model with `{}`.

    The accumulator is fed by the argument *deltas* and by nothing else, so a
    call that arrives complete — which some endpoints do — would run on empty
    input, or fail on it, with nothing anywhere saying a thing was lost.  The
    item in the terminal response carries the whole string, so it is read from
    there for any index that never streamed one.
    """
    event = _completed(
        output=[
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "calc",
                "arguments": '{"e": "6*7"}',
            }
        ]
    )

    assert responses_translate(event) == [
        Chunk(
            tool_call_deltas=(ToolCallDelta(index=0, arguments_delta='{"e": "6*7"}'),)
        ),
        Finish(stop_reason="tool_calls"),
    ]

    # An index that *did* stream is left alone: the whole string appended after
    # its own deltas would be the same arguments twice.
    assert responses_translate(event, streamed={0}) == [
        Finish(stop_reason="tool_calls")
    ]


def _text_done(text: str) -> ResponseTextDoneEvent:
    return ResponseTextDoneEvent.model_validate(
        {
            "type": "response.output_text.done",
            "content_index": 0,
            "item_id": "i",
            "logprobs": [],
            "output_index": 0,
            "sequence_number": 2,
            "text": text,
        }
    )


def _message_item(text: str) -> dict[str, Any]:
    """The assembled answer, as `response.output` carries it."""
    return {
        "type": "message",
        "id": "m1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def test_responses_fills_in_the_text_that_never_streamed() -> None:
    """**The text half of the argument back-fill, and it was missing.**

    The turn's answer is assembled from `response.output_text.delta` and from
    nothing else, so an endpoint that assembles the text and sends it whole —
    a gateway implementing this API partially, which this module treats as real
    for `store` — left the text parts empty.  A `Finish` still arrives from
    `response.completed`, so the cut-stream check passes and the turn is stored
    and displayed with the *authoritative* answer `""`: a blank reply recorded
    as the model's whole output, with nothing anywhere saying a thing was lost.
    """
    event = _completed(output=[_message_item("forty-two")])

    assert responses_translate(event) == [
        Chunk(text="forty-two"),
        Finish(stop_reason="stop"),
    ]
    # An index that *did* stream is left alone: the whole text appended after its
    # own deltas would be the same answer twice.
    assert responses_translate(event, streamed={0}) == [Finish(stop_reason="stop")]


def test_responses_reads_the_answer_a_gateway_sends_whole() -> None:
    """The same endpoint's other half: `response.output_text.done`.

    A real event of this API, with the text and its `output_index`, and the one
    an endpoint that sends no per-token deltas has to send its answer in.
    """
    assert responses_translate(_text_done("forty-two")) == [Chunk(text="forty-two")]
    assert responses_translate(_text_done("forty-two"), streamed={0}) == []


def test_responses_a_stream_that_delivered_its_text_is_not_answered_twice() -> None:
    """The memory is per stream, and it is what the terminal event reads.

    Every event of one stream shares one `streamed` set, exactly as the plugin's
    streamer drives it: a gateway that sends the deltas *and* the assembled
    answer — which the real API does — must reach the transcript once.
    """
    streamed: set[int] = set()

    assert responses_translate(_text_delta("forty"), streamed=streamed) == [
        Chunk(text="forty")
    ]
    assert responses_translate(_text_done("forty-two"), streamed=streamed) == []
    assert responses_translate(
        _completed(output=[_message_item("forty-two")]), streamed=streamed
    ) == [Finish(stop_reason="stop")]


def test_responses_a_truncated_response_says_length() -> None:
    """Cut off at the output cap is an ordinary outcome, not an error.

    The reason travels through as the stop reason, and it is **normalised for
    the same reason the Anthropic one is**: the other two backends report
    `length` for the same condition (chat-completions passes its raw `length`
    through; Anthropic maps `max_tokens` to `length`), so a caller asking "was
    this answer truncated?" with `stop_reason == "length"` must not be told a
    different story by this backend of the three.
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
    assert responses_translate(event) == [Finish(stop_reason="length")]


def test_responses_an_unknown_incomplete_reason_is_passed_through() -> None:
    """A reason this build has not heard of is a fact about the model, not a gap
    to fold into something familiar."""
    event = ResponseIncompleteEvent.model_validate(
        {
            "type": "response.incomplete",
            "sequence_number": 9,
            "response": _resp(
                status="incomplete",
                incomplete_details={"reason": "content_filter"},
            ),
        }
    )
    assert responses_translate(event) == [Finish(stop_reason="content_filter")]


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


def test_responses_a_system_turn_of_parts_contributes_its_text() -> None:
    """**One input, three adapters, one answer.**

    `Message.content` permits a list of parts, and the chat-completions adapter
    passes a non-string through and the Anthropic one extracts its text and
    *warns* about what it cannot carry — with a comment naming exactly this
    failure.  This adapter read only `str` content, so a system turn carrying
    blocks contributed nothing at all, with no word anywhere: a prompt the caller
    wrote left the request and the model answered a question nobody had asked
    (DESIGN.md §11.1).
    """
    instructions, items = to_responses_input(
        [
            Message(
                role="system",
                content=[
                    {"type": "text", "text": "be "},
                    {"type": "text", "text": "nice"},
                ],
            ),
            Message(role="user", content="hi"),
        ]
    )
    assert instructions == "be nice"
    assert items == [{"role": "user", "content": "hi"}]


def test_responses_a_system_turn_names_the_part_it_cannot_carry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A block `instructions` has no field for is dropped *and named* — the
    alternative is an attachment that vanishes with nothing anywhere saying so,
    which is the failure the part converter refuses one function up."""
    caplog.set_level(
        logging.WARNING, logger="slife2.plugins.llm.openai_responses_server"
    )
    instructions, _ = to_responses_input(
        [
            Message(
                role="system",
                content=[
                    {"type": "text", "text": "be nice"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,eA=="},
                    },
                ],
            )
        ]
    )
    assert instructions == "be nice", "the text beside it is still the prompt"
    assert any(
        "system turn carries input_image" in record.getMessage()
        for record in caplog.records
    ), "the part that could not be carried left no trace"


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


def test_responses_a_remote_image_url_is_passed_on_and_not_fetched() -> None:
    """The API fetches it, and **no request leaves this process**.

    A remote `http(s)` URL is a valid `input_image.image_url`, and the Responses
    API downloads it on its own side — so passing it through is exactly what
    "not fetching" looks like, and it is the third kind of attachment the file
    tunnel exists to make work (DESIGN.md §8.1).  This used to be dropped, on the
    same wrong premise the Anthropic adapter once had: dropping it silently lost
    the image the caller sent, with nothing anywhere reporting the loss — an
    attached picture reached the model on two backends and vanished on this one.
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
    assert items[0]["content"] == [
        {
            "type": "input_image",
            "image_url": "https://x.test/a.png",
            "detail": "auto",
        },
        {"type": "input_text", "text": "hi"},
    ]


def test_responses_something_that_is_neither_inline_nor_a_url_is_dropped() -> None:
    """A `file:` URL or a bare word is not an image source in either form, so it
    is dropped rather than sent to 400 on every call that carries one."""
    _, items = to_responses_input(
        [
            Message(
                role="user",
                content=[
                    {"type": "image_url", "image_url": {"url": "file:///etc/passwd"}},
                    {"type": "text", "text": "hi"},
                ],
            )
        ]
    )
    assert items[0]["content"] == [{"type": "input_text", "text": "hi"}]


def test_responses_a_source_that_is_neither_data_nor_http_is_dropped_but_named(
    caplog,
) -> None:
    """The same silent drop the *part type* one function up once was.

    A source that is neither `data:` nor http(s) is no `input_image` this API
    takes, so it is dropped rather than sent to 400 on every call carrying it —
    but dropping it with no word loses the caller's attachment with nothing
    anywhere saying so.  The scheme is logged; the drop stands.
    """
    part = {"type": "image_url", "image_url": {"url": "file:///etc/passwd"}}
    with caplog.at_level(logging.WARNING):
        _, items = to_responses_input(
            [Message(role="user", content=[part, {"type": "text", "text": "hi"}])]
        )
    assert items[0]["content"] == [{"type": "input_text", "text": "hi"}]
    assert any("'file'" in r.getMessage() for r in caplog.records)


def test_responses_a_part_that_is_not_a_mapping_is_dropped_but_named(caplog) -> None:
    """A part the adapter cannot look inside is dropped, not passed on.

    This API takes input items as mappings, so a bare string in the content
    list is a 400 on every call — but a skip with no word loses the caller's
    part with nothing saying so.  What arrived is named.
    """
    with caplog.at_level(logging.WARNING):
        _, items = to_responses_input(
            [Message(role="user", content=["oops", {"type": "text", "text": "hi"}])]
        )
    assert items[0]["content"] == [{"type": "input_text", "text": "hi"}]
    assert any("'str'" in r.getMessage() for r in caplog.records)


def test_responses_an_unrecognised_part_is_dropped_but_named(caplog) -> None:
    """An unknown input item is a 400, so it is dropped — and named.

    Dropping without a word loses the caller's part with nothing saying so; the
    type is logged so a newer part kind or a stray attachment is findable.
    """
    with caplog.at_level(logging.WARNING):
        _, items = to_responses_input(
            [
                Message(
                    role="user",
                    content=[
                        {"type": "video", "video": {}},
                        {"type": "text", "text": "hi"},
                    ],
                )
            ]
        )
    assert items[0]["content"] == [{"type": "input_text", "text": "hi"}]
    assert any("'video'" in r.getMessage() for r in caplog.records)


def test_responses_tool_results_are_their_own_items() -> None:
    """The part with no analogue in either other backend.

    A tool result is not a message with a role here — it is a `function_call_output`
    item addressed by the id the call announced.
    """
    _, items = to_responses_input(
        [Message(role="tool", content="4", tool_call_id="c1")]
    )
    assert items == [{"type": "function_call_output", "call_id": "c1", "output": "4"}]


def test_responses_render_a_non_string_tool_result_rather_than_emptying_it() -> None:
    """The same defect as the Anthropic adapter's, and the same fix.

    A result that was not a string became `""`, so the tool's whole output left
    the request with nothing saying it had.  It is rendered to JSON text now.
    """
    _, items = to_responses_input(
        [Message(role="tool", content=[{"matches": 2}], tool_call_id="c1")]
    )
    (item,) = items
    assert item["type"] == "function_call_output"
    assert item["output"] != ""
    assert "matches" in item["output"]


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


def test_accumulator_joins_a_name_the_provider_sent_in_pieces() -> None:
    """**A name that arrives in fragments has no other way to be read.**

    Overwriting took the last fragment for the whole name — `read_` followed by
    `file` became a call to `file` — and nothing compared the reassembled name
    against anything, so the call was dispatched, its arguments intact, to
    whichever tool happened to be called that: an unknown-tool error at best, a
    *different* tool than the model chose at worst.
    """
    accumulator = ToolCallAccumulator()
    accumulator.add(ToolCallDelta(index=0, id="c1", name="read_"))
    accumulator.add(ToolCallDelta(index=0, name="file", arguments_delta='{"p": "a"}'))
    (call,) = accumulator.complete()
    assert call.name == "read_file"
    assert call.arguments == {"p": "a"}


def test_accumulator_does_not_double_a_name_the_provider_repeats() -> None:
    """The case the overwrite got right, which accumulating must not break.

    A provider that restates the whole name on every fragment — rather than
    sending it once — still yields the name once.
    """
    accumulator = ToolCallAccumulator()
    accumulator.add(ToolCallDelta(index=0, id="c1", name="read_file"))
    accumulator.add(ToolCallDelta(index=0, name="read_file", arguments_delta='{"p"'))
    accumulator.add(ToolCallDelta(index=0, name="read_file", arguments_delta=': "a"}'))
    (call,) = accumulator.complete()
    assert call.name == "read_file"
    assert call.arguments == {"p": "a"}


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


def test_accumulator_refuses_arguments_that_never_parsed() -> None:
    """Reading a truncated stream as `{}` re-attributes the fault to the model.

    The provider cuts the JSON short or mangles it; an empty argument dict then
    reaches the tool, which reports a validation error about a call the model
    got right — and the model retries the call it never got wrong.  So the turn
    fails, naming the call (DESIGN.md §11.1: a value that cannot be read is not a value
    that is absent).
    """
    accumulator = ToolCallAccumulator()
    accumulator.add(
        ToolCallDelta(index=0, id="c1", name="calc", arguments_delta="{oops")
    )
    with pytest.raises(ValueError, match="did not parse as JSON"):
        accumulator.complete()


def test_accumulator_refuses_arguments_that_are_not_an_object() -> None:
    """Valid JSON that is not an object is as unreadable as JSON that never
    parsed.  `[]`, `"x"` and `null` all parse, and a call's arguments are an
    object by the protocol — so coercing one to `{}` hands the tool an empty
    argument dict the model never sent, the very substitution the unparseable
    case refuses (DESIGN.md §11.1: a value that cannot be read is not a value that is
    absent)."""
    for raw in ("[]", '"x"', "null"):
        accumulator = ToolCallAccumulator()
        accumulator.add(
            ToolCallDelta(index=0, id="c1", name="calc", arguments_delta=raw)
        )
        with pytest.raises(ValueError, match="not a JSON object"):
            accumulator.complete()


def test_accumulator_keeps_an_empty_argument_dict_as_empty() -> None:
    """A call that genuinely took no arguments is a real absence, not damage."""
    accumulator = ToolCallAccumulator()
    accumulator.add(ToolCallDelta(index=0, id="c1", name="now", arguments_delta=""))
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
async def test_a_resolved_credential_is_redacted_from_the_result() -> None:
    """A provider that echoes the key it was given is not put in front of the model.

    The result is the tool output and is what the turn stores; the progress
    stream is display only and keeps its chunk boundaries.
    """
    from slife2.libs.host import secrets

    secrets.reset()
    secrets.register("sk-live-123456")
    try:
        server = build_llm_server(
            name="t",
            streamer=scripted(
                Chunk(text="the key is sk-live-"),
                Chunk(text="123456 done"),
                Finish("stop"),
            ),
        )
        result, _chunks = await call_stream_chat(
            server, messages=[{"role": "user", "content": "hi"}], tools=[], model="m"
        )
    finally:
        secrets.reset()

    assert result.data["text"] == "the key is [redacted] done"


@pytest.mark.asyncio
async def test_a_stream_that_ends_without_a_finish_is_not_a_whole_answer() -> None:
    """**The stream ending is not the provider saying it finished.**

    The SDK's iteration ends *normally* when the response body simply stops — a
    chat-completions reply whose proxy idle-timeout fired between the last
    content chunk and the chunk carrying `finish_reason`, a server restarted
    mid-answer, an SSE body closed early — so a partial generation and a
    finished one are the same shape at this layer.  Nothing compared them, and
    the partial text was returned with `stop_reason: ""`, which the loop turns
    into "stop": a truncated answer appended to the history and shown as the
    turn's answer, with zero tokens and no word anywhere that the provider had
    gone silent.

    Every one of the three wires does signal the end — `finish_reason` on the
    last chunk, Anthropic's `message_delta`, `response.completed` — and each
    becomes a `Finish`, so a stream that carries none is one that was cut.
    """
    server = build_llm_server(
        name="t", streamer=scripted(Chunk(text="the answer is 4"))
    )
    with pytest.raises(Exception, match="without a finish"):
        await call_stream_chat(
            server, messages=[{"role": "user", "content": "2+2"}], tools=[], model="m"
        )


@pytest.mark.asyncio
async def test_stream_chat_returns_the_assembled_reasoning() -> None:
    """Assembled the same way the text is, and for the same reason.

    The chunks go to the reader to be watched; the *result* is what the turn
    keeps, and a consumer that reassembled the reasoning out of the progress
    stream would be reading the record off a channel that is allowed to drop
    fragments.  One accumulator, both fields.
    """
    server = build_llm_server(
        name="t",
        streamer=scripted(
            Chunk(thinking="six sevens "),
            Chunk(thinking="are forty-two"),
            Chunk(text="42"),
            Finish("stop"),
        ),
    )
    result, chunks = await call_stream_chat(
        server, messages=[{"role": "user", "content": "6*7?"}], tools=[], model="m"
    )

    assert result.data["thinking"] == "six sevens are forty-two"
    assert result.data["text"] == "42"
    # ...and the reader saw it as it arrived, on its own kind of chunk.
    assert [c.thinking for c in chunks] == ["six sevens ", "are forty-two", ""]


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
    from slife2.libs.turn.messages import Usage

    server = build_llm_server(
        name="t",
        streamer=scripted(
            Chunk(text="x", usage=Usage(prompt_tokens=11, cache_read=7)),
            Chunk(text="y", usage=Usage(completion_tokens=22, cache_creation=3)),
            Finish("stop"),
        ),
    )
    result, _ = await call_stream_chat(
        server, messages=[{"role": "user", "content": "hi"}], tools=[], model="m"
    )
    assert result.data["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 22,
        "cache_read": 7,
        "cache_creation": 3,
    }


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


def test_openai_stream_usage_is_asked_for_unless_a_gateway_refuses_it() -> None:
    """The one compat knob whose default is *send*, and why.

    Usage arrives only because it was asked for, so an absent value has to mean
    send — unlike `compat.store`, where an absent value leaves the endpoint's
    own default in place and that is the right one.  `omit` is the escape hatch
    for a gateway that rejects the extension with a 400, which is the whole
    reason the knob exists: without it such a gateway is unusable with no
    workaround at all.
    """
    plain = ModelSettings(model="m")
    request = openai_build_request([], [], plain, stream_usage=True)
    assert request["stream_options"] == {"include_usage": True}

    omitted = ModelSettings(model="m", stream_usage="omit")
    assert "stream_options" not in openai_build_request(
        [], [], omitted, stream_usage=True
    )

    # The knob is per model, and the parameter is the server's own switch: a
    # call that asked for nothing is not given a field back by a model setting.
    assert "stream_options" not in openai_build_request(
        [], [], plain, stream_usage=False
    )


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
    `reasoning_content`, which the adapter reads either way.  When one *is*
    asked for it rides in `extra_body`, because the SDK's typed surface has no
    `thinking` parameter to pass it as — the guard test below is what holds that.
    """
    native = ModelSettings(model="m", reasoning=True)
    assert "thinking" not in openai_build_request([], [], native, stream_usage=True)

    asked = ModelSettings(
        model="m", reasoning=True, thinking="enabled", max_tokens=1000
    )
    request = openai_build_request([], [], asked, stream_usage=True)
    assert request["extra_body"]["thinking"]["type"] == "enabled"
    # Half the budget, because the other half is what the answer is made of.
    assert request["extra_body"]["thinking"]["budget_tokens"] == 500

    refused = ModelSettings(model="m", reasoning=True, thinking="disabled")
    refused_request = openai_build_request([], [], refused, stream_usage=True)
    assert refused_request["extra_body"]["thinking"]["type"] == "disabled"

    omitted = ModelSettings(model="m", thinking="omit")
    omitted_request = openai_build_request([], [], omitted, stream_usage=True)
    assert "thinking" not in omitted_request
    assert "extra_body" not in omitted_request, "nothing to carry, no hatch opened"


def test_thinking_enabled_without_a_cap_is_refused() -> None:
    """**The budget is a share of a number the config has to have said.**

    An absent `max_tokens` used to become 8192 inside the adapter, which is this
    module inventing an output budget for somebody else's model — and the
    thinking budget is half of it, so the invented number decided how long the
    model could think.  `thinking: enabled` with no cap is a configuration that
    cannot be satisfied, and DESIGN.md §11.1's rule is to name it where it is used.
    """
    asked = ModelSettings(model="m", reasoning=True, thinking="enabled")

    with pytest.raises(ValueError) as raised:
        openai_build_request([], [], asked, stream_usage=True)

    assert "max_tokens" in str(raised.value)


def test_an_unrecognised_thinking_value_is_refused_not_ignored() -> None:
    """A typo in `compat.thinking` used to send nothing, silently.

    The field has a closed set of spellings, so an unrecognised value fell
    through the adapter's match to `case _` and read as "omit" — the operator's
    intent to ask for thinking dropped with no word while the request went out
    unchanged, the deformation DESIGN.md §11.1 names.  The value is refused now, naming it
    and the spellings this adapter knows.
    """
    settings = ModelSettings(model="m", reasoning=True, thinking="enabledd")
    with pytest.raises(ValueError) as raised:
        openai_build_request([], [], settings, stream_usage=True)

    message = str(raised.value)
    assert "enabledd" in message, "the value is named"
    assert "omit" in message and "disabled" in message, "and the spellings it knows"


def test_the_openai_request_only_names_arguments_the_sdk_has() -> None:
    """Every key this module builds is one `chat.completions.create` accepts.

    The bug this pins is the one that shipped: the request body is assembled as
    a dict and handed to the SDK as `**kwargs`, so a key the SDK does not have is
    a `TypeError` at the call site — invisible to a test that only reads the dict,
    and fatal on *every* call of *every* conversation.  `compat.thinking:
    enabled` was exactly that: it is not part of the typed surface, so it has to
    reach the wire through `extra_body`, and this is what makes the next such
    field fail here instead of in a conversation.  Comparing the built request
    against the *installed* signature is the whole fact, and it needs no endpoint.
    """
    import inspect

    from openai.resources.chat.completions import AsyncCompletions

    accepted = set(inspect.signature(AsyncCompletions.create).parameters)
    settings = ModelSettings(
        model="m",
        temperature=0.7,
        top_p=0.9,
        max_tokens=8000,
        reasoning=True,
        thinking="enabled",
    )
    request = openai_build_request(
        [
            Message(role="system", content="be brief"),
            Message(role="user", content="hi"),
        ],
        [ToolSpec(name="t", description="d", parameters={"type": "object"})],
        settings,
        stream_usage=True,
    )

    assert set(request) - accepted == set(), "a key the SDK would refuse"
    assert {"extra_body", "stream_options", "tools"} <= set(request), "and not empty"


def keys_in(value: Any) -> set[str]:
    """Every key at any depth, for asking what reached a provider."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            found |= keys_in(item)
    elif isinstance(value, list):
        for item in value:
            found |= keys_in(item)
    return found


def test_openai_renames_reasoning_for_a_model_that_reasons() -> None:
    """The one place what we carry differs from what a provider is sent.

    `thinking` is ours: the turn log stores messages, and a conversation read
    back with the reasoning stripped has a hole in it.  This wire has no such
    name and DeepSeek's is `reasoning_content`, so here it is renamed — and the
    empty string below is the load-bearing part.  A reasoner that is asked to
    think must carry the field on *every* assistant message in the history or
    the API answers 400, and an assistant message that reported nothing is
    exactly the one that would be missing it.
    """
    messages = [
        Message(role="user", content="6*7?"),
        Message(
            role="assistant",
            content="",
            thinking="six sevens",
            tool_calls=[ToolCall(id="c1", name="calc", arguments={"e": "6*7"})],
        ),
        Message(role="tool", content="42", tool_call_id="c1"),
        Message(role="assistant", content="it is 42"),
    ]

    converted = openai_provider_messages(messages, reasoning=True)

    assert converted[1]["reasoning_content"] == "six sevens"
    assert "thinking" not in converted[1], "our own name never leaves"
    assert converted[3]["reasoning_content"] == "", "nor is the field ever absent"
    # The filler is an assistant-side rule: a user message and a tool result
    # have no reasoning to report and say nothing about any.
    assert "reasoning_content" not in converted[0]
    assert "reasoning_content" not in converted[2]


def test_the_filler_is_only_for_models_that_reason() -> None:
    """Two rules, drawn the way v1 draws them.

    A message that *has* reasoning is renamed wherever it turns up — an endpoint
    that produced it is an endpoint that knows the name.  The empty filler is
    the other rule: only a model the config says reasons gets one, because an
    endpoint that never reports reasoning is an endpoint whose acceptance of the
    key is unknown.
    """
    quiet = openai_provider_messages(
        [Message(role="assistant", content="hi")], reasoning=False
    )
    assert "reasoning_content" not in quiet[0]

    carried = openai_provider_messages(
        [Message(role="assistant", content="hi", thinking="because")], reasoning=False
    )
    assert carried[0]["reasoning_content"] == "because"


def test_the_other_two_protocols_drop_reasoning_altogether() -> None:
    """Not sent, and not quietly carried either.

    The Anthropic and Responses shapes have nowhere to put a model's own earlier
    reasoning, so it is simply not built into their requests — v1's behaviour
    for both, and the reason the rename lives in one adapter rather than in
    `to_wire`.  Asserted by asking what keys reached the request, because a
    field that leaked would be present and unnoticed rather than wrong.
    """
    assistant = Message(role="assistant", content="42", thinking="sevens")

    _, blocks = to_anthropic_messages([assistant])
    assert not (keys_in(blocks) & {"thinking", "reasoning_content"})

    request = responses_build_request([assistant], [], ModelSettings(model="m"))
    assert not (keys_in(request) & {"thinking", "reasoning_content"})


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


def test_anthropic_sampling_rides_in_extra_body() -> None:
    """The SDK no longer *types* `temperature`, so it cannot be an argument.

    `anthropic` 1.11's `messages.create` has neither `temperature` nor `top_p`
    — they went when the first-party API stopped accepting them — so a model
    entry that configures one used to raise a `TypeError` from this process on
    every call: a message about a keyword, before anything was sent, with
    nothing naming the model that was misconfigured.  `extra_body` is the
    hatch a field the SDK no longer types reaches the wire through, and what
    the config asked for is what the provider is asked for.

    Measured against `api.deepseek.com/anthropic` (`anthropic-messages`, a
    gateway): the typed form raises, this one answers.
    """
    settings = ModelSettings(model="m", temperature=0.7, top_p=1.0)
    request = anthropic_build_request([], [], settings)
    assert request["extra_body"] == {"temperature": 0.7, "top_p": 1.0}
    assert "temperature" not in request, "the top level is what the SDK types"

    # Only what was configured, and nothing at all when nothing was.
    one = anthropic_build_request([], [], ModelSettings(model="m", temperature=0.3))
    assert one["extra_body"] == {"temperature": 0.3}
    assert "extra_body" not in anthropic_build_request([], [], ModelSettings(model="m"))


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
    assert "extra_body" not in request, "dropped, not moved to the hatch"


def test_the_anthropic_request_only_names_arguments_the_sdk_has() -> None:
    """Every key this module builds is one `messages.create` accepts.

    The bug this pins is the one that shipped: a request body is assembled as a
    dict and handed to the SDK as `**kwargs`, so a key the SDK does not have is
    a `TypeError` at the call site — invisible to a test that only reads the
    dict, and fatal on every call.  Comparing the built request against the
    *installed* signature is what makes that class of drift fail here instead
    of in a conversation, and it needs no endpoint: the signature is the whole
    fact.
    """
    import inspect

    from anthropic.resources.messages import AsyncMessages

    accepted = set(inspect.signature(AsyncMessages.create).parameters)
    settings = ModelSettings(
        model="m",
        temperature=0.7,
        top_p=0.9,
        max_tokens=8000,
        reasoning=True,
        thinking="omit",
    )
    request = anthropic_build_request(
        [
            Message(role="system", content="be brief"),
            Message(role="user", content="hi"),
        ],
        [ToolSpec(name="t", description="d", parameters={"type": "object"})],
        settings,
    )

    assert set(request) - accepted == set(), "a key the SDK would refuse"
    assert {"system", "tools", "extra_body"} <= set(request), "and it is not empty"


def test_anthropic_thinking_budget_leaves_room_to_answer() -> None:
    """A model that spends everything thinking returns nothing.

    **Both of the API's bounds, and the second one is the trap.**  A budget has
    to be at least 1024 *and* strictly smaller than `max_tokens`, so the two
    together leave a window only when the cap is above the floor.  The clamp
    guarded the second alone — `max(1024, half)` then `min(…, cap - 1)` — and so
    answered 1023 for a `max_tokens: 1024` model, which is a 400 on every call.

    **A cap that cannot hold a budget is a configuration fault, and is refused.**
    It used to turn thinking off with a warning, so `reasoning: true` was
    quietly not honoured and the caller was told nothing — a weaker path standing
    in for the capability that was asked for (DESIGN.md §11.1).  The refusal names the two
    numbers.
    """
    request = anthropic_build_request(
        [], [], ModelSettings(model="m", reasoning=True, max_tokens=2000)
    )
    assert request["thinking"]["budget_tokens"] == 1024, "the floor, under the cap"

    # The smallest cap that can hold a budget at all: 1025 leaves exactly 1024.
    narrowest = anthropic_build_request(
        [], [], ModelSettings(model="m", reasoning=True, max_tokens=1025)
    )
    assert narrowest["thinking"]["budget_tokens"] == 1024

    # 1024 and below: no budget is both >= 1024 and < the cap, so no
    # configuration satisfies `reasoning: true` — refused rather than silently
    # dropped, and the message names max_tokens and the floor.
    for cap in (1024, 1):
        with pytest.raises(ValueError, match=f"max_tokens={cap}.*1024"):
            anthropic_build_request(
                [], [], ModelSettings(model="m", reasoning=True, max_tokens=cap)
            )


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


def test_the_responses_request_only_names_arguments_the_sdk_has() -> None:
    """Every key this module builds is one `responses.create` accepts.

    The same guard the other two adapters carry, and for the same reason: the
    body is a dict splatted into a typed SDK method, so a key the SDK does not
    have is a `TypeError` at the call site — on every call, invisible to a test
    that only reads the dict.  This adapter has no non-typed field of its own
    yet, so every key it emits is one the API types; the day one is not, it fails
    here rather than in a conversation.  Comparing against the *installed*
    signature is the whole fact, and it needs no endpoint.
    """
    import inspect

    from openai.resources.responses import AsyncResponses

    accepted = set(inspect.signature(AsyncResponses.create).parameters)
    settings = ModelSettings(
        model="m",
        temperature=0.7,
        top_p=0.9,
        max_tokens=8000,
        reasoning=True,
        store=False,
    )
    request = responses_build_request(
        [
            Message(role="system", content="be brief"),
            Message(role="user", content="hi"),
        ],
        [ToolSpec(name="t", description="d", parameters={"type": "object"})],
        settings,
    )

    assert set(request) - accepted == set(), "a key the SDK would refuse"
    assert {"instructions", "tools", "reasoning", "store"} <= set(request), "not empty"


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


def test_a_third_spelling_of_reasoning_is_reported_once(monkeypatch, caplog) -> None:
    """A gateway using a spelling this adapter does not read is invisible.

    Nothing errors, the answer is correct, and the only symptom is a line the
    transcript should have carried.  So the unrecognised field is *named*, once
    per process, and a busy stream pays one line rather than one per token.
    """
    monkeypatch.setattr(openai_server, "_reported_unknown_fields", set())
    event = _oa(
        choices=[
            {"index": 0, "delta": {"reasoning_text": "why"}, "finish_reason": None}
        ]
    )
    with caplog.at_level(logging.WARNING):
        assert openai_translate(event) == []
        assert openai_translate(event) == []

    reported = [
        r for r in caplog.records if "reasoning may be dropped" in r.getMessage()
    ]
    assert len(reported) == 1, "once per process, not once per token"
    assert "reasoning_text" in reported[0].getMessage()


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


def test_a_remote_image_url_is_passed_on_and_not_fetched_here() -> None:
    """Anthropic downloads it, and **no request leaves this process**.

    This used to be dropped, on the argument that fetching a URL a prompt named
    is a capability this plugin has no business having.  The premise was wrong
    for this form of it: the Messages API takes a `url` source and fetches it on
    its own side, so passing it on is exactly what "not fetching" looks like —
    and dropping it silently was the one outcome worse than either.

    v1 did this (`llm_backends/anthropic.py`), and it is what makes the third
    kind of attachment — a URL — work at all.
    """
    blocks = to_anthropic_blocks(
        [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]
    )
    assert blocks == [
        {
            "type": "image",
            "source": {"type": "url", "url": "https://example.test/a.png"},
        }
    ]


def test_something_that_is_neither_inline_nor_a_url_is_dropped() -> None:
    """A `file:` URL or a bare word is not an image source in either form."""
    blocks = to_anthropic_blocks(
        [{"type": "image_url", "image_url": {"url": "file:///etc/passwd"}}]
    )
    assert blocks == []


def test_a_source_that_is_neither_data_nor_http_is_dropped_but_named(caplog) -> None:
    """The same silent drop the *part type* one function up once was.

    A source the Messages API cannot read is a 400 on every call that carries
    it, so the block cannot be built and the attachment is dropped — but
    dropping it with no word loses the picture the caller sent with nothing
    anywhere saying so.  The scheme is logged; the drop stands.
    """
    with caplog.at_level(logging.WARNING):
        blocks = to_anthropic_blocks(
            [{"type": "image_url", "image_url": {"url": "file:///etc/passwd"}}]
        )
    assert blocks == []
    assert any("'file'" in r.getMessage() for r in caplog.records)


def test_a_data_url_with_no_payload_is_dropped_but_named(caplog) -> None:
    """The same silent drop one branch below the unwritable-scheme one.

    A `data:` URL that names a type and carries no bytes has nothing for the
    Messages API to read as a base64 source, so it is dropped — but dropping it
    with no word loses the caller's attachment with nothing anywhere saying so.
    The type it claimed is logged; the drop stands.
    """
    with caplog.at_level(logging.WARNING):
        blocks = to_anthropic_blocks(
            [{"type": "image_url", "image_url": {"url": "data:image/png;base64,"}}]
        )
    assert blocks == []
    assert any("image/png" in r.getMessage() for r in caplog.records)


def test_a_part_that_is_not_a_mapping_is_dropped_but_named(caplog) -> None:
    """A part the adapter cannot even look inside is dropped, not passed on.

    The Messages API takes block mappings, so a bare string in the list is a
    400 on every call — but a skip with no word loses the caller's part with
    nothing saying so.  What arrived is named.
    """
    with caplog.at_level(logging.WARNING):
        blocks = to_anthropic_blocks(["oops", {"type": "text", "text": "hi"}])
    assert blocks == [{"type": "text", "text": "hi"}]
    assert any("'str'" in r.getMessage() for r in caplog.records)


def test_an_unrecognised_part_is_dropped_but_named(caplog) -> None:
    """Passing an unknown block through is a 400 on every call containing one.

    Dropping it is right; dropping it *silently* is not — an attachment or a
    newer part type then disappears from the request with nothing anywhere
    saying so, which is a failure nobody can see from inside this module.  The
    type is logged.
    """
    with caplog.at_level(logging.WARNING):
        blocks = to_anthropic_blocks(
            [{"type": "video", "video": {}}, {"type": "text", "text": "hi"}]
        )
    assert blocks == [{"type": "text", "text": "hi"}]
    assert any("'video'" in r.getMessage() for r in caplog.records)


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


# --- the provider pool -------------------------------------------------------


class _FakeSDKClient:
    """Stands in for `AsyncOpenAI`/`AsyncAnthropic`: a thing that can be closed."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _config(base_url: str) -> Config:
    """A config with one provider, so a *new* object is an edit as the pool reads it."""
    return Config(
        providers={
            "p": ProviderSettings(
                api="openai-completions",
                base_url=base_url,
                api_key_ref="a-plain-literal-key",
                models={"m": ModelSettings(model="m")},
            )
        }
    )


def _pool(monkeypatch, built: list[_FakeSDKClient], current: list[Config]):
    """A `LiveProviders` whose config read is `current`, and the clients it built."""
    from slife2.plugins.llm import server_common

    def make_client(_provider: Any, _key: str) -> Any:
        built.append(_FakeSDKClient())
        return built[-1]

    monkeypatch.setattr(server_common, "load_cached", lambda: current[0])
    return server_common.LiveProviders("openai-completions", "t", make_client)


@pytest.mark.asyncio
async def test_a_config_change_closes_the_client_it_replaces(monkeypatch) -> None:
    """**The rebuild is the only thing that ever drops an SDK client**, and it
    must let go of it rather than abandon it.

    `AsyncOpenAI` and `AsyncAnthropic` each hold an `httpx` client with its open
    socket to the provider — `AsyncAPIClient` defines no `__del__` and `httpx`
    closes nothing at collection — so a client dropped without a close keeps its
    connections until the collector happens to reach it, once per changed config
    read.  The rebuild is the *designed* live-reload path, so this is an ordinary
    edit rather than a corner.
    """
    built: list[_FakeSDKClient] = []
    current = [_config("https://first.test/v1")]
    pool = _pool(monkeypatch, built, current)

    async with pool.lease() as clients:
        clients.client("p")
    assert built[0].closed is False, "the pool in use is not touched"

    current[0] = _config("https://second.test/v1")  # the edit
    async with pool.lease() as clients:
        clients.client("p")

    assert built[0].closed is True, "the replaced client is let go, not abandoned"
    assert built[1].closed is False, "and the one now in use is not"
    assert len(built) == 2, "one client per config, built on first use"


@pytest.mark.asyncio
async def test_a_client_is_not_closed_under_a_stream_that_is_reading_it(
    monkeypatch,
) -> None:
    """The close waits for the last holder, and that is the whole reason for one.

    The SDK's `close()` closes the connection under a response that is still
    being read, and a model answer takes minutes — so an edit that landed
    mid-answer would cut the answer off if the retired pool closed eagerly.  It
    closes when the stream holding it is done instead.
    """
    built: list[_FakeSDKClient] = []
    current = [_config("https://first.test/v1")]
    pool = _pool(monkeypatch, built, current)

    async with pool.lease() as running:  # a stream in flight
        running.client("p")

        current[0] = _config("https://second.test/v1")  # the edit lands mid-answer
        async with pool.lease() as fresh:
            fresh.client("p")

        assert built[0].closed is False, "the answer being read is left alone"

    assert built[0].closed is True, "and the client is let go the moment it ends"


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
    from slife2.plugins.llm import anthropic_server

    assert anthropic_server.main([]) == 2
    # **On stderr, and that is the half worth asserting**: a start-up refusal is
    # a sentence about a process that did not start, and stdout in this system is
    # reserved (see `slife2.libs.plugin.protocol.configure_logging`).
    assert "no anthropic-messages provider" in capsys.readouterr().err


def test_the_matching_protocol_serves_every_provider_of_it(
    tmp_path, monkeypatch
) -> None:
    """The other half of the same branch: a protocol that *is* in the config
    goes on to serve, carrying only that protocol's providers.

    `serve` is replaced rather than run, because the real one blocks on a
    socket — what is being asserted is what reaches it.  **The seam is
    `slife2.libs.plugin.protocol.serve`**, which is where every server in this
    system is started from: `server_common` used to be its own copy of that
    call, and a test that patched the copy proved something about the copy.
    """
    from slife2.libs.plugin import protocol
    from slife2.plugins.llm import openai_server

    _point_at(tmp_path, monkeypatch)
    served: list[Any] = []
    monkeypatch.setattr(protocol, "serve", lambda *a, **k: served.append(a))

    assert openai_server.main([]) == 0
    (mcp, address, _args) = served[0]
    # The address is the one the *protocol* listens on, not the provider's — a
    # provider has no address of its own.
    assert address.url == "http://127.0.0.1:24001/mcp"
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
    from slife2.plugins.llm import openai_responses_server

    assert openai_responses_server.main([]) == 2
    assert "no openai-responses provider" in capsys.readouterr().err


def test_responses_is_served_on_its_own_port(tmp_path, monkeypatch) -> None:
    """A third protocol gets a third address, keyed by the `api` it speaks.

    Sharing a port with the chat-completions server is the mistake this
    catches: both are "openai", and both would be reachable at :24001 if the
    registry keyed them by anything but the protocol.
    """
    from slife2.libs.plugin import protocol
    from slife2.plugins.llm import openai_responses_server

    (tmp_path / "slife2.yaml").write_text(ONLY_RESPONSES, encoding="utf-8")
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(tmp_path))
    served: list[Any] = []
    monkeypatch.setattr(protocol, "serve", lambda *a, **k: served.append(a))

    assert openai_responses_server.main([]) == 0
    (mcp, address, _args) = served[0]
    assert address.url == "http://127.0.0.1:24003/mcp"
    assert mcp.name == openai_responses_server.SERVER_NAME
