"""Encoding a :class:`~slife2.libs.model.base.Chunk` onto the progress channel.

This is the agent-loop-to-LLM-server wire, one hop below `slife2.libs.turn.events`.  It is
a separate vocabulary because it carries different things: turn events describe
what the *agent* is doing (a tool ran, the turn ended), while chunks are raw
provider output (a text fragment, a slice of a tool call's JSON arguments) that
the agent loop only forwards for display.

Both halves live here so the format has one definition and one round-trip test —
the same arrangement as `slife2.libs.turn.events`, for the same reason.

Shape
-----
The overwhelmingly common chunk is a few characters of text, so that case gets
its own two-key form (``{"k":"text","d":"..."}``) and everything else uses the
fuller ``chunk`` form.  At one notification per token, the wrapper keys are a
measurable share of the bytes on the hot path.
"""

from __future__ import annotations

import json
from typing import Any

from slife2.libs.model.base import Chunk, ToolCallDelta
from slife2.libs.turn.messages import Usage


def encode_chunk(chunk: Chunk) -> str:
    """Encode a chunk for an MCP progress notification's `message` field.

    `ensure_ascii=True` for the reason given in `slife2.libs.turn.messages`: the payload
    can reach a log on a console whose codepage is not UTF-8.
    """
    if (
        chunk.text
        and not chunk.thinking
        and not chunk.tool_call_deltas
        and chunk.usage is None
    ):
        payload: dict[str, Any] = {"k": "text", "d": chunk.text}
    else:
        payload = {"k": "chunk"}
        if chunk.text:
            payload["d"] = chunk.text
        if chunk.thinking:
            payload["th"] = chunk.thinking
        if chunk.tool_call_deltas:
            payload["t"] = [_delta_to_wire(d) for d in chunk.tool_call_deltas]
        if chunk.usage is not None:
            payload["u"] = chunk.usage.to_wire()

    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def decode_chunk(message: str) -> Chunk | None:
    """Decode a progress notification's `message` back into a chunk.

    `None` for anything that is not one of ours, and the caller drops it: a
    progress message this build cannot read is somebody else's — a future
    version of the server, or a peer on the same stream — and rendering it as
    the model's answer would be inventing an answer out of a log line.  The
    sentinel is what makes that safe rather than a crash, and dropping is the
    whole of what it is for.
    """
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(payload, dict):
        return None

    match payload.get("k"):
        case "text" if isinstance(payload.get("d"), str):
            return Chunk(text=payload["d"])
        case "chunk":
            return _chunk_from_wire(payload)
        case _:
            return None


#: The four counts `Usage.to_wire` writes; a payload with a hole was not this
#: build's line.
_USAGE_FIELDS = tuple(Usage().to_wire())


def _chunk_from_wire(payload: dict[str, Any]) -> Chunk | None:
    """The fuller chunk shape, or `None` when a field is not this build's.

    **Every field is checked, not coerced.**  The docstring above promises a
    line this build cannot read is dropped, and `str(payload.get("d"))` made
    `{"d": 42}` a chunk of text `"42"` while `int(raw.get("i"))` and
    `Usage.from_wire` raised out of the progress handler — a display line
    failing the model call that was streaming beside it.
    """
    text = payload.get("d", "")
    thinking = payload.get("th", "")
    if not isinstance(text, str) or not isinstance(thinking, str):
        return None
    raw_deltas = payload.get("t", [])
    if not isinstance(raw_deltas, list):
        return None
    deltas: list[ToolCallDelta] = []
    for raw in raw_deltas:
        delta = _delta_from_wire(raw)
        if delta is None:
            return None
        deltas.append(delta)
    usage = None
    raw_usage = payload.get("u")
    if raw_usage is not None:
        if not _usage_ok(raw_usage):
            return None
        usage = Usage.from_wire(raw_usage)
    return Chunk(
        text=text,
        thinking=thinking,
        tool_call_deltas=tuple(deltas),
        usage=usage,
    )


def _usage_ok(raw: Any) -> bool:
    """The four token counts, present and integers rather than bools or text."""
    return isinstance(raw, dict) and all(
        isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool)
        for key in _USAGE_FIELDS
    )


def _delta_to_wire(delta: ToolCallDelta) -> dict[str, Any]:
    """Encode one tool-call fragment, omitting fields the provider did not send.

    Omitting rather than nulling matters: a fragment usually carries only the
    argument text, and `{"i":0,"a":"{\\"e"}` is a third the size of the same
    thing with four explicit nulls, on a payload sent once per fragment.
    """
    payload: dict[str, Any] = {"i": delta.index}
    if delta.id is not None:
        payload["id"] = delta.id
    if delta.name is not None:
        payload["name"] = delta.name
    if delta.arguments_delta:
        payload["a"] = delta.arguments_delta
    return payload


def _delta_from_wire(raw: Any) -> ToolCallDelta | None:
    """Rebuild one tool-call fragment, or `None` when it is not this build's.

    `index` is an integer in `_delta_to_wire`; text and a non-integral index
    are another build's line, and the whole chunk is dropped rather than a
    fragment invented around them.
    """
    if not isinstance(raw, dict):
        return None
    index = raw.get("i", 0)
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    call_id = raw.get("id")
    name = raw.get("name")
    arguments = raw.get("a", "")
    if call_id is not None and not isinstance(call_id, str):
        return None
    if name is not None and not isinstance(name, str):
        return None
    if not isinstance(arguments, str):
        return None
    return ToolCallDelta(
        index=index,
        id=call_id,
        name=name,
        arguments_delta=arguments,
    )
