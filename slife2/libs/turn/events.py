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

from slife2.libs.turn.messages import Usage

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
class ContextChosen:
    """What the turn is about to run on, reported before it runs.

    The discriminator's answer (`slife2.libs.turn.context`).  It arrives **first**, before
    any text, because it is a fact about what the conversation is about to be
    and not about how the turn went — a display that only learned it afterwards
    would be showing one turn's context under another turn's answer.

    Two counts, because the decision has two halves and they are what somebody
    watching wants to know: `kept` is how much of what was in hand survived it,
    `recalled` is how much came back from the turn log.  Their sum is how many
    turns the turn runs on.
    """

    kept: int
    recalled: int


@dataclass(frozen=True)
class ToolsCounted:
    """How many tools the catalogue holds, as the hub answered the last ask.

    **A fact about the set, not about the request.**  The list that goes out
    with a model call is the *loaded* subset, so its length is not this: a tool
    the model has not loaded, and a skill or a CLI entry — which have no load
    state at all — are still tools that exist.  `total` is the size of the
    catalogue the loaded set was chosen from, which is the number a display can
    show without pretending it is the model's list.

    **Emitted before every model call, not once per turn**, because that is when
    the list is asked for.  The hub's answer is as fresh as that ask and no
    fresher — there is no timer behind it, deliberately (`slife2.plugins.toolhub.server`:
    freshness is "the next ask tries again").  So an unchanged count arrives
    again with each call; a display overwrites its own number and the wire cost
    is an integer per call.
    """

    total: int


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
    #: Which model call of this turn asked for it (1-based), and the cap that
    #: turn is running under — `0` for no cap, the same spelling
    #: `max_iterations` uses everywhere else.  v1's `iteration`/
    #: `max_iterations`, carried on the call rather than on an event of its own
    #: for the reason v1 carried them here: a tool call is the only thing that
    #: marks an iteration, so this is where somebody watching a long turn sees
    #: it advancing.
    #:
    #: `iteration` of 0 is **"nobody said" rather than an iteration**: a rebuilt
    #: transcript has no iteration numbers to draw, and neither has a server
    #: from before these keys — so the counter is left off rather than drawn as
    #: `(0)`.
    iteration: int = 0
    max_iterations: int = 0


@dataclass(frozen=True)
class ToolApprovalRequested:
    """A tool call is waiting for somebody to confirm it.

    The model asked to be checked (`_approve`), and this is the request on its
    way to whoever is watching — the loop emits it and then waits.  **The answer
    does not travel back on this stream**: it arrives as a *call* (`answer_
    approval`) on whatever is watching, because a turn's progress channel runs
    one way and a prompt that could only be answered by a reply to a
    notification would be a prompt nothing could answer.

    It is emitted only where somebody can be asked.  A caller with nobody to ask
    never sees one, and the call simply runs — so a display that shows this event
    and waits is a display that was promised an answer.
    """

    call_id: str
    name: str
    #: What is being asked about, carried here rather than read back off the
    #: tool panel: the panel is drawn *after* the answer (a refused call mounts
    #: nothing), so the prompt is the only place a reader can see what it is
    #: they are being asked to allow.
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
class MessageCutIn:
    """A message that arrived while the turn was running, and was handed to it.

    **One event, and every source of a mid-turn message goes through it** — the
    person typing in a window, a channel handing one over, a peer.  The loop
    emits it when it writes the `_check_new_input` pair, so the words here are
    the tool's own answer rather than something written beside it: the screen
    can no more invent what a person said than the model can.

    `channel` is where the message came in on, unchanged from the `Pending` it
    waited in, so the line wears the same bubble a turn of its own would.

    `receipt` is the submission's own opaque id, echoed back by the server for
    the one reader that has to match a delivery to the request it answered: the
    window that submitted it.  Empty for a message from another caller — that
    caller has its own stream and this event is not for it.
    """

    text: str
    channel: str = ""
    receipt: str = ""


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
    #: iterations, `usage` is the sum of three calls and reads as a context
    #: nearly three times its real size.
    last_usage: Usage
    iterations: int
    stop_reason: str


TurnEvent = (
    ContextChosen
    | ToolsCounted
    | TextDelta
    | ThinkingDelta
    | ToolCallStarted
    | ToolApprovalRequested
    | ToolCallFinished
    | MessageCutIn
    | TurnFinished
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
    log on a console whose codepage is not UTF-8 — see `slife2.libs.turn.messages`.
    """
    # Bound before the match so the exhaustiveness guard below cannot leave it
    # unbound from the checker's point of view.
    payload: dict[str, Any] = {}

    match event:
        case ContextChosen(kept=kept, recalled=recalled):
            payload = {"t": "context", "kept": kept, "recalled": recalled}
        case ToolsCounted(total=total):
            # One integer per model call; a short key like every other,
            # because the volume this convention is for is per token and this
            # is not, but a second spelling of "the count" would be.
            payload = {"t": "tools", "n": total}
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
                # The iteration and the turn's cap, omitted at zero for the
                # reason above: "nobody said" is not a number, and a written `0`
                # would have to be read back as one.  `max_iterations: 0` is no
                # cap, so a turn that has one shows `(3/30)` and a turn that does
                # not shows a bare `(3)` — both keys absent hides the counter
                # entirely.
                **({"iteration": event.iteration} if event.iteration else {}),
                **(
                    {"max_iterations": event.max_iterations}
                    if event.max_iterations
                    else {}
                ),
            }
        case ToolApprovalRequested(call_id=call_id, name=name, arguments=arguments):
            payload = {
                "t": "tool_approval",
                "id": call_id,
                "name": name,
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
        case MessageCutIn(text=text, channel=channel, receipt=receipt):
            payload = {
                "t": "cut_in",
                "d": text,
                "c": channel,
                # Omitted when there is nobody to match it to.  The receipt is
                # the submitting window's, and a message from any other caller
                # has none.
                **({"r": receipt} if receipt else {}),
            }
        case TurnFinished(text, usage, last_usage, iterations, stop_reason):
            payload = {
                "t": "done",
                "d": text,
                "iterations": iterations,
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
    other MCP server — or a future version of this one — may report progress
    this build has no word for, and the caller drops it: a progress stream is
    display, so an unreadable line is a line not shown rather than a turn that
    dies or an answer invented out of a log message.
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
        case "context":
            return ContextChosen(
                kept=int(payload.get("kept") or 0),
                recalled=int(payload.get("recalled") or 0),
            )
        case "tools" if isinstance(payload.get("n"), int) and not isinstance(
            payload.get("n"), bool
        ):
            return ToolsCounted(total=payload["n"])
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
                # Absent reads as 0, which is the counter's "nobody said" — the
                # honest answer for a server that predates these keys.
                iteration=int(payload.get("iteration") or 0),
                max_iterations=int(payload.get("max_iterations") or 0),
            )
        case "tool_approval" if isinstance(payload.get("name"), str):
            arguments = payload.get("args")
            return ToolApprovalRequested(
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
        case "cut_in" if isinstance(payload.get("d"), str):
            return MessageCutIn(
                text=payload["d"],
                # Absent reads as the stream's own default, which is the honest
                # answer for a build that predates the field: the words are the
                # message, and where they came from is a display detail.
                channel=str(payload.get("c") or ""),
                receipt=str(payload.get("r") or ""),
            )
        case "done":
            return TurnFinished(
                text=str(payload.get("d") or ""),
                usage=Usage.from_wire(payload.get("usage")),
                # An absent key reads as zero, which is the honest answer to
                # "how full is the context" when nobody said — and is what a
                # server from before this key existed sends.
                last_usage=Usage.from_wire(payload.get("last")),
                iterations=int(payload.get("iterations") or 0),
                stop_reason=str(payload.get("stop") or ""),
            )
        case _:
            return None
