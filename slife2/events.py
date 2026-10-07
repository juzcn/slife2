"""The turn event vocabulary, and its encoding onto the MCP progress channel.

One vocabulary, three consumers: the loop emits these, the agent server encodes
them into `notifications/progress`, and the TUI decodes them back.  Keeping the
encode and decode halves in one module is what lets a single round-trip test
stand in for the whole contract.

Why a tagged union rather than a method per event
-------------------------------------------------
v1 gives its event handler eight methods (`on_text_chunk`, `on_tool_call`,
`on_tool_approval`, ...).  The cost shows up the moment a ninth event is added:
every observer in the codebase — the TUI, the tests, the recorder — has to grow
a method, whether or not it cares about the new event.  Here, adding a variant is
one dataclass and one line in `_DECODERS`; observers that do not match on it keep
working untouched.

The price is that observers pattern-match (`case TextDelta():`) rather than
implement a named method, which is slightly more verbose at the call site.  That
is the trade, and it is worth it at three observers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, assert_never

from slife2.messages import Usage

#: How much of a tool result travels in a progress notification.  The model
#: still receives the full result — this caps only what is *displayed*, because
#: a tool returning 100 KB would otherwise be duplicated into the notification
#: stream and into every log line that touches it.
PREVIEW_CHARS = 200


def preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    """Truncate `text` for display, marking that it was truncated.

    The ellipsis is ASCII on purpose: this string reaches a Windows console via
    the server's log, and U+2026 is not representable in every codepage.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


@dataclass(frozen=True)
class TextDelta:
    """A piece of the assistant's visible answer."""

    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    """A piece of the model's reasoning.

    Carried separately from `TextDelta` because it is shown separately — folded
    away unless asked for — and because a model that reasons natively produces
    a great deal of it that nobody reads.
    """

    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    """The model asked for a tool and the loop is about to run it."""

    call_id: str
    name: str
    #: What it was asked with.  Carried so the transcript can say *what* was
    #: read or computed rather than only which tool ran — "calc(e=6*7)" is a
    #: sentence; "calc" is a word.
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallFinished:
    """A tool ran.  `ok` is False when the tool reported an error.

    A failed tool is not an exception anywhere in this system — it is a result
    the model reads and reacts to — so this event carries failures too, and the
    TUI renders both.
    """

    call_id: str
    name: str
    ok: bool
    result_preview: str
    result_chars: int
    elapsed_ms: int


@dataclass(frozen=True)
class TurnFinished:
    """The turn ended.  `text` is the final answer, and is authoritative.

    This duplicates the return value of `send_message` on purpose: the return
    value is what a *client* renders, while this event is what the progress
    stream reports.  When they disagree — a dropped notification, a coalescing
    layer, a late delivery — the return value wins.
    """

    text: str
    #: The turn's total across every model call — what it cost.
    usage: Usage
    #: The last call's own usage: how large the conversation had become, which
    #: is what a "how full is the context" display is asking about.  Carried
    #: separately because `usage` cannot answer it — on a turn that took three
    #: steps, `usage` is the sum of three calls and reads as a context nearly
    #: three times its real size.
    last_usage: Usage
    steps: int
    stop_reason: str


TurnEvent = (
    TextDelta | ThinkingDelta | ToolCallStarted | ToolCallFinished | TurnFinished
)


class TurnObserver(Protocol):
    """Anything that wants to watch a turn as it happens.

    Deliberately one method.  See the module docstring for why.
    """

    async def on_event(self, event: TurnEvent) -> None: ...


class NullObserver:
    """Observer for callers that want a result and do not care about the middle.

    A real class rather than `None` so the loop never has to branch on whether
    anyone is watching, and so tests can assert against a default-constructed
    loop without special-casing.
    """

    async def on_event(self, event: TurnEvent) -> None:
        return None


#: The default observer.  Stateless and immutable in practice, so one shared
#: instance is enough.
NULL_OBSERVER = NullObserver()


def encode(event: TurnEvent) -> str:
    """Encode a turn event for an MCP progress notification's `message` field.

    The field is a plain string, so the event travels as compact JSON.  Short
    keys (`t`, `d`) are used because this payload is sent once per token, and at
    that volume the key names are a measurable fraction of the bytes.

    `ensure_ascii=True` keeps the encoding pure ASCII.  The payload crosses a
    UTF-8 JSON-RPC boundary either way, but the same string can reach a server
    log on a console whose codepage is not UTF-8 — see `slife2.messages`.
    """
    # Bound before the match so the exhaustiveness guard below cannot leave it
    # unbound from the checker's point of view.
    payload: dict[str, Any] = {}

    match event:
        case TextDelta(text):
            payload = {"t": "text", "d": text}
        case ThinkingDelta(text=text):
            payload = {"t": "thinking", "d": text}
        case ToolCallStarted(call_id=call_id, name=name, arguments=arguments):
            payload = {
                "t": "tool_start",
                "id": call_id,
                "name": name,
                # Omitted when empty: most calls carry no arguments in the
                # display sense, and a notification is not the place for an
                # empty object on every single tool.
                **({"args": arguments} if arguments else {}),
            }
        case ToolCallFinished(
            call_id, name, ok, result_preview, result_chars, elapsed_ms
        ):
            payload = {
                "t": "tool_end",
                "id": call_id,
                "name": name,
                "ok": ok,
                "d": result_preview,
                "n": result_chars,
                "ms": elapsed_ms,
            }
        case TurnFinished(text, usage, last_usage, steps, stop_reason):
            payload = {
                "t": "done",
                "d": text,
                "steps": steps,
                "stop": stop_reason,
                "usage": usage.to_wire(),
                # One per turn, not one per token, so the extra key costs
                # nothing that the short-key convention above is protecting.
                "last": last_usage.to_wire(),
            }
        case _:
            # `assert_never` rather than a bare raise: it is the idiom the type
            # checker understands, so adding a variant to TurnEvent without
            # teaching `encode` about it becomes a static error here instead of
            # a runtime one in production.
            assert_never(event)

    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def decode(message: str) -> TurnEvent | None:
    """Decode a progress notification's `message` back into a turn event.

    Returns None when `message` is not one of ours, rather than raising.  Some
    other MCP server — or a future version of this one — may report plain
    progress text, and the TUI renders that verbatim instead of dying on it.
    """
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(payload, dict):
        return None

    # `d` is always a string when present; a payload whose `d` is a number is
    # somebody else's message that happens to share our key space.
    match payload.get("t"):
        case "text" if isinstance(payload.get("d"), str):
            return TextDelta(text=payload["d"])
        case "thinking" if isinstance(payload.get("d"), str):
            return ThinkingDelta(text=payload["d"])
        case "tool_start" if isinstance(payload.get("name"), str):
            arguments = payload.get("args")
            return ToolCallStarted(
                call_id=str(payload.get("id") or ""),
                name=payload["name"],
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        case "tool_end" if isinstance(payload.get("name"), str):
            return ToolCallFinished(
                call_id=str(payload.get("id") or ""),
                name=payload["name"],
                ok=bool(payload.get("ok")),
                result_preview=str(payload.get("d") or ""),
                result_chars=int(payload.get("n") or 0),
                elapsed_ms=int(payload.get("ms") or 0),
            )
        case "done":
            return TurnFinished(
                text=str(payload.get("d") or ""),
                usage=Usage.from_wire(payload.get("usage")),
                # An absent key reads as zero, which is the honest answer to
                # "how full is the context" when nobody said — and is what a
                # server from before this key existed sends.
                last_usage=Usage.from_wire(payload.get("last")),
                steps=int(payload.get("steps") or 0),
                stop_reason=str(payload.get("stop") or ""),
            )
        case _:
            return None
