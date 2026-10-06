"""The two LLM MCP servers.

Three layers are tested here, and the middle one is the point of the file:

1. `translate` — the pure provider-event adapter, driven by *real* SDK objects
   built with `model_validate`.  No mocking library: the SDK parses its own wire
   format, and that is the shape we adapt to.
2. `stream_chat` end to end over FastMCP's in-memory transport, with a scripted
   streamer in place of the provider.  This is where tool-call fragment
   reassembly and progress delivery are proved.
3. Message conversion for Anthropic, whose format disagrees with the neutral
   model in ways that produce 400s if handled naively.

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

from slife2.config import ModelSettings
from slife2.llm.anthropic_server import (
    DEFAULT_MAX_TOKENS,
    to_anthropic_messages,
    to_anthropic_tools,
)
from slife2.llm.anthropic_server import (
    build_request as anthropic_build_request,
)
from slife2.llm.anthropic_server import translate as anthropic_translate
from slife2.llm.base import Chunk, Finish, Streamer, ToolCallDelta
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
        messages: list[Message], tools: list[ToolSpec], model: str
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
            "stream_chat", arguments, progress_handler=on_progress
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

    async def streamer(messages, tools, model):
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
