"""Round-trip tests for the two wire vocabularies and the message model.

Encoding and decoding live next to each other in the source so that one
round-trip test per vocabulary can stand in for the whole contract.  This file
is that test — it is why `encode` and `decode` are never allowed to drift apart
into separate modules.

Marked `unit`: nothing here touches the network or the disk.
"""

from __future__ import annotations

import pytest

from slife2.events import (
    NULL_OBSERVER,
    PREVIEW_CHARS,
    TextDelta,
    ToolCallFinished,
    TurnFinished,
    decode,
    encode,
    preview,
)
from slife2.llm.base import Chunk, ToolCallDelta
from slife2.llm.wire import decode_chunk, encode_chunk
from slife2.messages import (
    Message,
    StreamChatResult,
    ToolCall,
    ToolSpec,
    Usage,
)

pytestmark = pytest.mark.unit

EVENTS = [
    TextDelta(text="hello"),
    TextDelta(text="你好 — éè"),
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
    TurnFinished(text="done", usage=Usage(10, 20), steps=2, stop_reason="stop"),
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


def test_unparseable_tool_arguments_degrade_to_empty() -> None:
    """A model that emits bad JSON should get an error *from its tool*.

    Raising here would abort the turn; degrading lets the loop hand the tool an
    empty argument dict, the tool report the problem, and the model correct
    itself -- the loop's normal feedback path.
    """
    call = ToolCall.from_wire(
        {"id": "c1", "function": {"name": "calc", "arguments": "{not json"}}
    )
    assert call.arguments == {}
    assert call.name == "calc"


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
        usage=Usage(5, 6),
        stop_reason="tool_calls",
    )
    assert StreamChatResult.from_wire(result.to_wire()) == result


def test_stream_chat_result_from_wire_tolerates_an_empty_payload() -> None:
    assert StreamChatResult.from_wire(None) == StreamChatResult()


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
    for event in EVENTS:
        assert await NULL_OBSERVER.on_event(event) is None
