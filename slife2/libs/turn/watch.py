"""What a watcher is handed: a conversation's history, then its turns.

The vocabulary is `slife2.libs.turn.events`' for everything a turn produces.  What is
added here is the three things a *watcher* needs that a turn's own caller
already knows or never sees:

* **`Snapshot`** — the turns the store already holds.  It is sent once, first,
  so a screen that has just opened draws the same history `transcript` would
  have given it, and so the live events that follow have a defined place to
  start from.  A watcher that skipped it would show a conversation beginning in
  the middle of somebody else's work.
* **`TurnStarted`** — the message that opened a turn.  A turn's caller sent
  that message and has it on its own screen; a watcher did not, so without this
  frame the first thing it would see is an answer to a question it never read.
* **`Closed`** — the conversation is not watchable any more (the subagent was
  removed), and this is the last frame.  The call ends with it rather than
  hanging on a stream nothing will ever write to again.

**The encode and decode halves live here, together, for `events`' reason**: one
round trip is the whole contract, and a decoder in the TUI with an encoder in
the agent server is how the two come to disagree about a key.  The turn events
themselves are encoded by `events.encode`, unchanged — a second spelling of a
`TextDelta` would be a second vocabulary for one thing.

**The frames are progress notifications on a held-open call**, exactly as a
turn's events are (`slife2.plugins.tui.server._push` is the same arrangement from
the other direction).  Nothing here knows about FastMCP, a transport, or a
queue; it is the bytes and their meaning.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from slife2.libs.turn.events import TurnEvent
from slife2.libs.turn.events import decode as decode_event
from slife2.libs.turn.events import encode as encode_event


@dataclass(frozen=True)
class Snapshot:
    """The stored turns a conversation is made of, sent once and first.

    `turns` is the shape `slife2.plugins.agent.server.transcript` returns — the
    same list a window draws when it opens — so a watcher and a restarted
    window cannot show different histories for one conversation.
    """

    turns: list[dict[str, Any]]


@dataclass(frozen=True)
class TurnStarted:
    """A turn began, with the message that opened it.

    Carried because a watcher did not send the message: the words are the
    caller's, and the channel is where they arrived.  Empty for a turn whose
    caller named no channel, which is the same spelling `Pending` uses.
    """

    text: str
    channel: str = ""


@dataclass(frozen=True)
class Closed:
    """The conversation stopped being watchable, and this is the last frame.

    `reason` is the server's sentence, written for a person to read; a window
    that has its own wording for the known cases may ignore it, and one that
    does not has something to draw rather than a stream that simply stopped.
    """

    reason: str = ""


#: Everything that can travel on a watch stream.  The turn events are the loop's
#: own vocabulary, not new variants: the three above are the frames a watcher
#: needs *around* a turn, and adding them to `TurnEvent` would make every other
#: observer of a turn answer for words it never sees.
WatchItem = Snapshot | TurnStarted | Closed | TurnEvent

#: The control frames' tags.  They are `t` values on the wire, like a turn
#: event's, and they are deliberately words no turn event uses.
SNAPSHOT = "snapshot"
TURN_STARTED = "turn_start"
CLOSED = "closed"


def encode(item: WatchItem) -> str:
    """Encode one watch item for a progress notification's `message` field.

    Same convention as `slife2.libs.turn.events.encode`: compact JSON,
    `ensure_ascii=True`, because the payload crosses a UTF-8 boundary but the
    same string can reach a console whose codepage is not UTF-8.
    """
    if isinstance(item, Snapshot):
        return json.dumps(
            {"t": SNAPSHOT, "turns": item.turns},
            ensure_ascii=True,
            separators=(",", ":"),
        )
    if isinstance(item, TurnStarted):
        return json.dumps(
            {"t": TURN_STARTED, "d": item.text, "c": item.channel},
            ensure_ascii=True,
            separators=(",", ":"),
        )
    if isinstance(item, Closed):
        return json.dumps(
            {"t": CLOSED, "d": item.reason},
            ensure_ascii=True,
            separators=(",", ":"),
        )
    # Everything left is a turn event; the branches above have narrowed the
    # union for the checker as well as for a reader.
    return encode_event(item)


def decode(message: str) -> WatchItem | None:
    """Decode a progress notification's `message`, or None if it is not ours.

    None rather than an exception, for `slife2.libs.turn.events.decode`'s reason:
    a notification this build has no word for is a line not read, and the caller
    is a display that should stay up rather than a turn that should die.  A
    control frame whose payload is damaged is such a line — read as absent, not
    guessed at, which is `SDD Appendix C's rule for a value that cannot be
    read.
    """
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None

    if isinstance(payload, dict):
        kind = payload.get("t")
        if kind == SNAPSHOT:
            turns = payload.get("turns")
            if isinstance(turns, list):
                return Snapshot(turns)
            return None
        if kind == TURN_STARTED:
            text = payload.get("d")
            channel = payload.get("c")
            if isinstance(text, str) and isinstance(channel, str):
                return TurnStarted(text, channel)
            return None
        if kind == CLOSED:
            reason = payload.get("d")
            if isinstance(reason, str):
                return Closed(reason)
            return None
    return decode_event(message)
