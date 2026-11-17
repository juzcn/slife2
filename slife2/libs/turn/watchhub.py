"""The tap and watcher table a server keeps for the turns it runs.

**Watching has two halves.**  `slife2.libs.turn.watch` is the wire vocabulary: a
snapshot, a turn's start, its events, and the closing frame.  This module is the
state behind that vocabulary — the turn in flight, kept so a watcher that joins
mid-turn is handed its start, and the watchers themselves, each with a queue.
One implementation, because every server that runs a turn and lets somebody
else read it faces the same race: `watch` registers a reader and `replay` copies
the tap, and the servers call the two with no await between them, while
`publish` appends to the tap before it serves the readers — so an event between
the two is seen once rather than lost or doubled.  A copy per server would be
two answers to one race; the pair being two calls is caller discipline, and the
one contract this table cannot enforce.

**A tap is replaced when the next turn starts**, so the table holds at most one
turn per key.  `Tap.recorded_turn` is the store id a conversation's turn was
given; a task records nothing and leaves it `None`, and a watcher then replays
the tap rather than trusting an id the snapshot cannot contain.

**`publish` is synchronous by contract, not by accident.**  It is called from a
turn's observer, between model calls, and `put_nowait` is the whole cost a
watcher may add: a display that cannot keep up is dropped by its own client,
never waited for here.  The tap is appended before the watchers are served.

**The key is the server's.**  The agent server uses the conversation's
`(home, agent, subagent)` triple and the agent-task server its own triple; the
table does not know what a conversation is.  `Watcher` is `eq=False` because it
is a session rather than a value: two readers are two watchers however alike
their queues look, and the generated equality would make the second one replace
the first in the set a removal then takes the wrong one out of.
"""

from __future__ import annotations

import asyncio
from collections.abc import Hashable
from dataclasses import dataclass, field

from slife2.libs.turn.events import TurnEvent
from slife2.libs.turn.watch import Closed, StartFrame, WatchItem


@dataclass
class Tap:
    """The last or current turn of one key, kept for a watcher.

    **A watcher arrives mid-turn by definition** — that is when somebody wants
    to look — and the store has nothing for the turn in flight: a turn is
    written when it ends.  So the turn's own display events are kept here from
    the moment it opens, and a watcher that joins is handed them before it joins
    the live queue.  The alternative is showing a turn's tail without its head,
    which is the one thing the main window never does.

    `recorded_turn` is the store id the turn was given, once it has one.  A
    watcher uses it to tell whether the snapshot it has just read already
    contains this turn: the same turn may be both in the store and here, and
    drawing it twice is the duplicate this field exists to prevent.  A task has
    no store and leaves it `None`.  **The id is set by the write, not by the
    store**, so `writing` is how a watcher tells "not recorded" from "not
    recorded yet" — the two are the same `recorded_turn` and opposite answers.

    **One per key, replaced when the next turn starts.**  A turn's record is
    awaited before its lock is released (or taken up by the next turn's
    `settle_record`), so by the time a replacement happens the store has the old
    one — which is why nothing here outlives the turn it describes by more than
    one.
    """

    started: StartFrame
    events: list[TurnEvent] = field(default_factory=list)
    recorded_turn: int | None = None
    #: The write that gives this turn its store id, while it is still running.
    #:
    #: **The id is knowable only after the store has the row, and the store can
    #: have it before `recorded_turn` is set.**  A watcher that reads a snapshot
    #: in that window cannot tell whether the turn it holds is already in it, so
    #: it waits for this task before deciding — otherwise the turn is drawn in
    #: both the snapshot and the replay.  `None` once the turn has no write in
    #: flight, or never had one (a turn that died before it opened a tap).
    writing: asyncio.Task[int | None] | None = None


@dataclass(eq=False)
class Watcher:
    """One open `watch` call, and the queue its frames are passed through.

    **The queue is the Context seam**, and it is not an optimisation.  A
    watcher's frames must be reported from the watcher's own handler task:
    `Context.report_progress` resolves its progress token from the request that
    is current when it is called, so an observer reporting from a *turn's* task
    would push into the turn caller's stream.  The handler owns the reporting;
    everything else only `put_nowait`s.

    Unbounded because every entry is a display frame for one connected client,
    and the client is a local terminal reading as fast as it is written.  A
    watcher that genuinely falls behind is the client's problem to fix by
    closing the view; the turn it is watching never waits for it.
    """

    queue: asyncio.Queue[WatchItem] = field(default_factory=asyncio.Queue)


@dataclass(frozen=True)
class Replay:
    """What a joining watcher is handed before the live queue.

    A copy of the tap taken in the watcher's registration step: `events` is the
    list as it stood then, so events published afterwards arrive only through
    the queue and are drawn once.
    """

    started: StartFrame
    events: list[TurnEvent]
    recorded_turn: int | None


class WatchHub[K: Hashable]:
    """Every tap and every open watch call, keyed the way its server keys turns."""

    def __init__(self) -> None:
        self._taps: dict[K, Tap] = {}
        self._watchers: dict[K, set[Watcher]] = {}

    # --- the tap -------------------------------------------------------------

    def tap(self, key: K) -> Tap | None:
        """The turn this key is running or last ran, or `None` for an idle key."""
        return self._taps.get(key)

    def start_tap(self, key: K, started: StartFrame) -> Tap:
        """Open a turn for watchers, and replace the key's last one.

        Called immediately before the turn's first observable step — the
        rebuild, which may emit `ContextChosen` — so the tap is the turn and not
        the one before it.

        A watcher of an idle conversation learns a turn has begun from this
        frame, not from its first delta: the message itself is the half of a
        turn a watcher never sent and cannot otherwise see.
        """
        tap = Tap(started=started)
        self._taps[key] = tap
        self._announce(key, started)
        return tap

    def drop(self, key: K) -> None:
        """Drop the tap, leaving the watchers to read the live queue."""
        self._taps.pop(key, None)

    def drop_if(self, key: K, tap: Tap) -> None:
        """Drop the tap only when it is still the one a caller holds.

        A turn's record can fail after the next turn has started; dropping by
        key alone would take the live turn's tap with it.
        """
        if self._taps.get(key) is tap:
            self._taps.pop(key, None)

    def replay(self, key: K) -> Replay | None:
        """The frame a joining watcher starts from, or `None` for no turn."""
        tap = self._taps.get(key)
        if tap is None:
            return None
        return Replay(tap.started, list(tap.events), tap.recorded_turn)

    # --- the readers ---------------------------------------------------------

    def publish(self, key: K, event: TurnEvent) -> None:
        """Offer one turn event to the tap, and to whoever is watching."""
        tap = self._taps.get(key)
        if tap is not None:
            tap.events.append(event)
        self._announce(key, event)

    def _announce(self, key: K, item: WatchItem) -> None:
        for watcher in tuple(self._watchers.get(key, ())):
            watcher.queue.put_nowait(item)

    def watch(self, key: K) -> Watcher:
        """Register a reader; the caller drains `watcher.queue` until `Closed`."""
        watcher = Watcher()
        self._watchers.setdefault(key, set()).add(watcher)
        return watcher

    def unwatch(self, key: K, watcher: Watcher) -> None:
        """Take one reader off a key, and forget the key when it was the last."""
        found = self._watchers.get(key)
        if found is None:
            return
        found.discard(watcher)
        if not found:
            self._watchers.pop(key, None)

    def close(self, key: K, reason: str) -> None:
        """End every watch of a key, with the reason as its last frame.

        **The frame rather than a cancelled call**, because the watcher is owed
        a sentence: a stream that simply stops is indistinguishable from one
        that is quiet between turns, and the two mean opposite things.  The
        watcher's handler sends this and returns, which is what makes the far
        end's call come back normally.
        """
        for watcher in self._watchers.pop(key, set()):
            watcher.queue.put_nowait(Closed(reason))
