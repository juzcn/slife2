"""Encoding a :class:`~slife2.llm.base.Chunk` onto the progress channel.

This is the agent-loop-to-LLM-server wire, one hop below `slife2.events`.  It is
a separate vocabulary because it carries different things: turn events describe
what the *agent* is doing (a tool ran, the turn ended), while chunks are raw
provider output (a text fragment, a slice of a tool call's JSON arguments) that
the agent loop only forwards for display.

Both halves live here so the format has one definition and one round-trip test —
the same arrangement as `slife2.events`, for the same reason.

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

from slife2.llm.base import Chunk, ToolCallDelta
from slife2.messages import Usage


def encode_chunk(chunk: Chunk) -> str:
    """Encode a chunk for an MCP progress notification's `message` field.

    `ensure_ascii=True` for the reason given in `slife2.messages`: the payload
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
            deltas = payload.get("t")
            return Chunk(
                text=str(payload.get("d") or ""),
                thinking=str(payload.get("th") or ""),
                tool_call_deltas=tuple(
                    _delta_from_wire(d) for d in deltas if isinstance(d, dict)
                )
                if isinstance(deltas, list)
                else (),
                usage=(
                    Usage.from_wire(payload["u"])
                    if isinstance(payload.get("u"), dict)
                    else None
                ),
            )
        case _:
            return None


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


def _delta_from_wire(raw: dict[str, Any]) -> ToolCallDelta:
    """Rebuild one tool-call fragment."""
    return ToolCallDelta(
        index=int(raw.get("i") or 0),
        id=raw.get("id"),
        name=raw.get("name"),
        arguments_delta=str(raw.get("a") or ""),
    )
