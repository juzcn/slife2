"""Round-trip tests for the two wire vocabularies and the message model.

Encoding and decoding live next to each other in the source so that one
round-trip test per vocabulary can stand in for the whole contract.  This file
is that test — it is why `encode` and `decode` are never allowed to drift apart
into separate modules.

Marked `unit`: nothing here touches the network or the disk.
"""

from __future__ import annotations

import pytest

from slife2.libs.model.base import Chunk, ToolCallDelta
from slife2.libs.model.wire import decode_chunk, encode_chunk
from slife2.libs.turn.events import (
    NULL_OBSERVER,
    PREVIEW_CHARS,
    MessageCutIn,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    ToolsCounted,
    TurnFinished,
    UsageReported,
    decode,
    encode,
    preview,
)
from slife2.libs.turn.messages import (
    Message,
    StreamChatResult,
    ToolCall,
    ToolSpec,
    Usage,
)

pytestmark = pytest.mark.unit

EVENTS = [
    # The model's list against the catalogue: both halves on one ask.
    ToolsCounted(loaded=24, total=253),
    # One model call's own usage, the figure the bar follows during a turn.
    UsageReported(last_usage=Usage(7, 3, cache_read=2, cache_creation=1)),
    TextDelta(text="hello"),
    TextDelta(text="你好 — éè"),
    # The iteration counter, and the two keys that are omitted at zero: the first
    # entry round trips a capped iteration, the second one that arrived without
    # an iteration at all — a call drawn into a rebuilt transcript.
    ToolCallStarted(
        call_id="c1",
        name="calc",
        arguments={"e": "6*7"},
        iteration=2,
        max_iterations=30,
    ),
    ToolCallStarted(call_id="c2", name="now"),
    # A message that arrived mid-turn, with the receipt the window matches back
    # to the submission it answered, and one from another caller without one:
    # both shapes travel, and the absent receipt is the empty string rather
    # than a guessed id.
    MessageCutIn(text="and another thing", channel="tui", receipt="7"),
    MessageCutIn(text="from a peer", channel="peer_agent:jill", receipt=""),
    ToolCallFinished(
        call_id="c1",
        name="calc",
        ok=True,
        result_preview="4",
        result_chars=1,
        elapsed_ms=3,
    ),
    ToolCallFinished(
        call_id="c2",
        name="now",
        ok=False,
        result_preview="boom",
        result_chars=4,
        elapsed_ms=0,
    ),
    # A turn's bill and the size it left the conversation at are different
    # numbers, so the fixture makes them different and the round trip has to
    # keep them apart.
    TurnFinished(
        text="done",
        usage=Usage(10, 20),
        last_usage=Usage(7, 3),
        iterations=2,
        stop_reason="stop",
    ),
]


@pytest.mark.parametrize("event", EVENTS, ids=lambda e: type(e).__name__)
def test_event_round_trips(event) -> None:
    assert decode(encode(event)) == event


@pytest.mark.parametrize("event", EVENTS, ids=lambda e: type(e).__name__)
def test_event_encoding_is_pure_ascii(event) -> None:
    """Non-ASCII text must be escaped, not passed through.

    The payload crosses a UTF-8 JSON-RPC boundary either way, but the same
    string also reaches a server log on a Windows console whose codepage may not
    be UTF-8, and `UnicodeEncodeError` mid-turn is a much worse failure than an
    escaped log line.
    """
    assert encode(event).isascii()


def test_decode_returns_none_for_foreign_progress_text() -> None:
    """Another server's progress message must not kill the reader."""
    assert decode("Downloading: 5/10") is None
    assert decode("") is None
    assert decode("[1, 2, 3]") is None


def test_decode_returns_none_for_foreign_json() -> None:
    """A payload with our shape but not our tags is still not ours."""
    assert decode('{"t": "unknown", "d": "x"}') is None
    # Right tag, wrong payload type.
    assert decode('{"t": "text", "d": 42}') is None
    # A count that is not a number is not this build's count: reading it as
    # zero would put "no tools" on the bar.
    assert decode('{"t": "tools", "n": "many"}') is None


CHUNKS = [
    Chunk(text="a"),
    Chunk(text="你好"),
    Chunk(text="partial", usage=Usage(1, 2)),
    Chunk(tool_call_deltas=(ToolCallDelta(index=0, id="c1", name="calc"),)),
    Chunk(tool_call_deltas=(ToolCallDelta(index=0, arguments_delta='{"e'),)),
    Chunk(tool_call_deltas=(ToolCallDelta(index=0, arguments_delta='":1}'),)),
    Chunk(
        tool_call_deltas=(
            ToolCallDelta(index=0, id="c1", name="calc", arguments_delta='{"e"'),
            ToolCallDelta(index=1, id="c2", name="now"),
        ),
        usage=Usage(3, 4),
    ),
]


@pytest.mark.parametrize("chunk", CHUNKS, ids=range(len(CHUNKS)))
def test_chunk_round_trips(chunk: Chunk) -> None:
    assert decode_chunk(encode_chunk(chunk)) == chunk


def test_chunk_encoding_is_pure_ascii() -> None:
    assert encode_chunk(Chunk(text="你好")).isascii()


def test_plain_text_chunk_uses_the_compact_form() -> None:
    """The hot path is one notification per token, so it gets a small payload.

    Guarding the size rather than the exact bytes: the point is that the wrapper
    keys of the general form are absent, and that is what the `k` tag says.
    """
    encoded = encode_chunk(Chunk(text="a"))
    assert encoded == '{"k":"text","d":"a"}'


def test_decode_chunk_returns_none_for_foreign_text() -> None:
    assert decode_chunk("Progress: 3/7") is None
    assert decode_chunk('{"k": "other"}') is None


@pytest.mark.parametrize(
    "line",
    [
        '{"k":"chunk","d":42}',
        '{"k":"chunk","th":42}',
        '{"k":"chunk","t":"x"}',
        '{"k":"chunk","t":[{"i":"x"}]}',
        '{"k":"chunk","t":[{"i":0,"a":5}]}',
        '{"k":"chunk","u":{"prompt_tokens":"lots"}}',
        '{"k":"chunk","u":{"prompt_tokens":1}}',
    ],
)
def test_a_malformed_chunk_is_dropped_not_raised(line: str) -> None:
    """**The sentinel is the whole contract.**  The decoder exists for a line
    from another build; coercing its fields read `{"d": 42}` as text `"42"`,
    and casting its index or usage raised out of the progress handler — a
    display line failing the model call streaming beside it.
    """
    assert decode_chunk(line) is None


def test_assistant_message_with_tool_calls_round_trips() -> None:
    """Including the `content: ""` normalisation `to_wire` introduces."""
    message = Message(
        role="assistant",
        tool_calls=[ToolCall(id="c1", name="calc", arguments={"e": "2+2"})],
    )
    assert Message.from_wire(message.to_wire()) == message


def test_tool_call_arguments_survive_as_a_string_on_the_wire() -> None:
    """The wire format wants a JSON string; the model must not see one."""
    call = ToolCall(id="c1", name="calc", arguments={"e": "2+2"})
    wire = call.to_wire()
    assert isinstance(wire["function"]["arguments"], str)
    assert ToolCall.from_wire(wire) == call


def test_unparseable_tool_arguments_are_kept_and_marked() -> None:
    """The call is answered with an error, and the tool is never run.

    An empty argument dict would dispatch the tool on defaults the model never
    chose; raising would end the turn over one call.  So the call keeps what
    arrived — its id, its name and the text — and carries the sentence the
    registry reports in place of a result.
    """
    call = ToolCall.from_wire(
        {"id": "c1", "function": {"name": "calc", "arguments": "{not json"}}
    )
    assert call.arguments == {}
    assert call.name == "calc"
    assert call.error is not None and "was not run" in call.error
    assert call.to_wire()["function"]["arguments"] == "{not json"


def test_tool_message_omits_the_tool_calls_key() -> None:
    """Some OpenAI-compatible servers 400 on a `tool` message carrying it."""
    wire = Message(role="tool", content="4", tool_call_id="c1").to_wire()
    assert "tool_calls" not in wire
    assert wire == {"role": "tool", "content": "4", "tool_call_id": "c1"}


def test_tool_spec_round_trips() -> None:
    spec = ToolSpec(
        name="calc",
        description="Arithmetic",
        parameters={"type": "object", "properties": {"e": {"type": "string"}}},
    )
    assert ToolSpec.from_wire(spec.to_wire()) == spec


def test_stream_chat_result_round_trips() -> None:
    result = StreamChatResult(
        text="answer",
        tool_calls=(ToolCall(id="c1", name="calc", arguments={"e": "1+1"}),),
        thinking="eight times eight is sixty-four",
        usage=Usage(5, 6, cache_read=4, cache_creation=1),
        stop_reason="tool_calls",
    )
    assert StreamChatResult.from_wire(result.to_wire()) == result


def test_usage_keeps_the_cache_breakdown_out_of_the_total() -> None:
    """The cache fields are *parts of* the prompt, not tokens on top of it.

    Every provider counts a cached prefix inside its own prompt count, so a
    `total_tokens` that added the breakdown would bill the prompt twice — and
    the loop accumulates these with `+`, which makes the double count add up
    across a turn's calls.
    """
    usage = Usage(prompt_tokens=10, completion_tokens=2, cache_read=7, cache_creation=3)
    assert usage.total_tokens == 12
    assert Usage.from_wire(usage.to_wire()) == usage


def test_reasoning_rides_our_wire_and_comes_back() -> None:
    """`thinking` is not an OpenAI field, and it travels with the message anyway.

    This shape is what *we* pass a conversation around in — the hop to an LLM
    server, and the turn log — and each provider adapter builds its own request
    from it, so a key no provider has a name for can ride here and be renamed or
    dropped at the edge.  It has to ride: the log stores messages, and a
    conversation read back with the reasoning stripped has a hole exactly where
    the reader was looking.
    """
    message = Message(
        role="assistant",
        content="42",
        thinking="let me work it out",
        tool_calls=[ToolCall(id="c1", name="calc", arguments={"e": "6*7"})],
    )

    assert message.to_wire()["thinking"] == "let me work it out"
    assert Message.from_wire(message.to_wire()) == message


def test_a_message_without_reasoning_has_no_thinking_key() -> None:
    """Absent, not empty — which is what a turn stored before the field existed
    has, so the two are the same bytes and neither adapter sees a stray key."""
    wire = Message(role="assistant", content="hello").to_wire()
    assert "thinking" not in wire
    assert Message.from_wire(wire).thinking == ""


def test_the_opaque_reasoning_payload_round_trips() -> None:
    """The adapter's own blocks are stored and travelled exactly as they arrived.

    They are not display and not a provider field: each adapter replays its own
    blocks and ignores the others, so the envelope has to survive the hop and
    the turn log byte for byte.
    """
    payload = {
        "model": "m",
        "blocks": [
            {"type": "thinking", "thinking": "hmm", "signature": "sig-1"},
            {"type": "redacted_thinking", "data": "zzz"},
        ],
    }
    message = Message(role="assistant", content="42", reasoning=payload)

    wire = message.to_wire()
    assert wire["reasoning"] == payload
    assert Message.from_wire(wire) == message
    assert message.to_record()["reasoning"] == payload


def test_an_unreadable_reasoning_payload_is_dropped_and_named(caplog) -> None:
    """A structural value is not a payload, and a provider would answer 400.

    The message keeps its place — the words are what the conversation is for —
    and the loss is named rather than passing a shape nobody wrote downstream.
    """
    with caplog.at_level("WARNING"):
        message = Message.from_wire(
            {"role": "assistant", "content": "42", "reasoning": "not an envelope"}
        )

    assert message.reasoning is None
    assert "reasoning payload" in caplog.text


def test_a_message_without_a_readable_role_keeps_its_shape() -> None:
    """**A role that cannot be read is not permission to call it the user's.**

    The three arms are told apart by what the message carries: a tool result
    addresses a call, an assistant turn makes one, and anything else is the
    person's.  The warning names the inference (`SDD Appendix C`); the
    conversation keeps a message whose role would otherwise be guessed wrong.
    """
    tool = Message.from_wire({"content": "4", "tool_call_id": "c1"})
    assistant = Message.from_wire(
        {
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "calc", "arguments": "{}"}}
            ],
        }
    )
    user = Message.from_wire({"content": "hi"})
    unknown = Message.from_wire({"role": "robot", "content": "hi"})

    assert tool.role == "tool"
    assert assistant.role == "assistant"
    assert user.role == "user"
    assert unknown.role == "user", "an unknown role is not passed to a provider"


def test_stream_chat_result_from_wire_tolerates_an_empty_payload() -> None:
    assert StreamChatResult.from_wire(None) == StreamChatResult()


def test_a_stream_chat_result_with_a_hole_is_refused() -> None:
    """`to_wire` writes all six keys; a mapping missing one is not this
    build's answer, and defaulting the hole would invent a fact."""
    wire = StreamChatResult(text="answer").to_wire()
    del wire["usage"]
    with pytest.raises(KeyError, match="usage"):
        StreamChatResult.from_wire(wire)


@pytest.mark.parametrize(
    "key",
    ["text", "tool_calls", "thinking", "reasoning", "usage", "stop_reason"],
)
def test_a_stream_chat_result_with_a_null_field_is_refused(key: str) -> None:
    """**A null is a value `to_wire` never writes, not an empty answer.**

    A server from another build can send `tool_calls: null`, `thinking: null`
    or `usage: null`; read as no calls, no reasoning and zero tokens it turns
    an unreadable answer into a confident empty one — the model's real reply
    discarded and the turn reported as a success.
    """
    wire = StreamChatResult(text="answer").to_wire()
    wire[key] = None
    with pytest.raises(KeyError, match=key):
        StreamChatResult.from_wire(wire)


def test_a_stream_chat_result_with_a_malformed_tool_call_is_refused() -> None:
    """A list is the shape; a null element in it is not a call."""
    wire = StreamChatResult(text="answer").to_wire()
    wire["tool_calls"] = [None]
    with pytest.raises(KeyError, match="tool call"):
        StreamChatResult.from_wire(wire)


def test_a_tool_call_whose_function_is_not_an_object_is_refused() -> None:
    """**The nesting is checked, so damage is a `KeyError` and not an
    `AttributeError`.**  The model client turns `KeyError` into its named
    "a model server from another build" refusal; `AttributeError` escaped the
    turn as a Python traceback instead, on the hop that exists for exactly
    this case.
    """
    for function in ("calc", 5, ["calc"]):
        with pytest.raises(KeyError, match="function"):
            ToolCall.from_wire({"id": "c1", "function": function})


@pytest.mark.parametrize("value", ["12", 1.5, True, None])
def test_a_stream_chat_result_with_a_non_integer_usage_is_refused(value) -> None:
    """`int("12")` is a measurement nobody made, and `int("abc")` raised a
    `ValueError` past the client's `KeyError` guard.  The four counts are
    integers in this build's shape, so anything else is unreadable."""
    wire = StreamChatResult(text="answer").to_wire()
    wire["usage"]["prompt_tokens"] = value
    with pytest.raises(KeyError, match="usage"):
        StreamChatResult.from_wire(wire)


# --- preview truncation ------------------------------------------------------


def test_preview_leaves_short_text_alone() -> None:
    assert preview("short") == "short"


def test_preview_truncates_and_marks_it() -> None:
    truncated = preview("x" * (PREVIEW_CHARS + 100))
    assert len(truncated) == PREVIEW_CHARS + 3
    assert truncated.endswith("...")
    assert truncated.isascii()


@pytest.mark.asyncio
async def test_null_observer_accepts_every_event() -> None:
    """The observer for a turn nobody is watching, and the whole of its contract.

    **Not "it returns `None`"**, which is what this asserted: a method with no
    return statement answers `None` whatever it is handed, so that line passed
    for an observer that mangled every event on the way past.  A null observer
    has to be *usable where an observer is required*, and the only thing that can
    go wrong is the call — so every event this build can emit is handed to it and
    none of them raises.  "Nobody is watching" is that and no more.
    """
    for event in EVENTS:
        await NULL_OBSERVER.on_event(event)
