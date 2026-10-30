"""In-memory record of the inbound A2A tasks one mesh is holding.

**A task's state lives here, not in a coroutine.**  One round is one
``SendMessage``; the responder runs a short-lived handler for it and then lets
go.  An interrupted task (``input-required``/``auth-required``) has no handler
at all: the profile lets the peer continue it later with the same ``Task.id``
and ``context_id``, and this table is what remembers the question and the
round in between.  The table is also the single answer to "what is this task
now" — the responder, ``task_list`` and the dedup path all read it.

Lifetime:

- ``working`` — a round is live.  A dropped connection kills the round and the
  record: the model's bridge died with it, and the peer's requester profile
  will retry (a fresh request, a fresh round).
- ``interrupted`` / terminal — kept for process life.  A reconnect keeps them,
  so a continuation or a retry can still be answered.  A **restart** starts
  empty: the bridge and the reply topic are process-local, exactly as before,
  and the profile does not require durable task state.  A peer that is still
  waiting is visible on its own side, where it can cancel or re-send.
- Terminal records are kept for the dedup TTL (not reported as open), then
  pruned.  The cap is terminal-first, so interrupted work is the last thing
  evicted.

Nothing here touches the disk.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

WORKING = "working"
INTERRUPTED = "interrupted"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
REJECTED = "rejected"

#: A task cannot leave these.  The same four words `task_store` writes.
TERMINAL_STATUSES = (COMPLETED, FAILED, CANCELLED, REJECTED)

#: One mesh's inbound table is bounded; terminal records go first.
MAX_INBOUND = 500

#: How many message ids/texts a task remembers for retry detection.  A retry
#: reuses one of these; the cap keeps a long-lived interrupted task small.
_MAX_SEEN = 32


def iso_now() -> str:
    """Current UTC time as an ISO-8601 string (official A2A timestamps).

    Public because it is a **shape** and not this store's clock: a task's row
    says when it started wherever it is reported from, and two spellings of the
    same timestamp in one answer would be the reader's problem to reconcile
    (`slife2.plugins.a2a.mesh`'s outgoing sends stamp themselves with this too).
    """
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class InboundTask:
    """One inbound A2A task, and the state that survives between rounds."""

    task_id: str
    peer: str
    """Sending peer's agent name."""

    since: str
    """ISO-8601 UTC arrival time."""

    state: str = WORKING
    """``working``, ``interrupted``, or one of `TERMINAL_STATUSES`."""

    interrupt: str = ""
    """``input_required`` / ``auth_required`` while interrupted, else ``""``."""

    question: str = ""
    """What the peer asked for while interrupted."""

    result: str = ""
    """The terminal artifact/reason."""

    context_id: str = ""
    """The conversation this task belongs to (the profile's continuation key)."""

    kind: str = "task"
    """``task`` or the private ``message`` shape."""

    round: int = 1
    """Which ``SendMessage`` this task is on (1 for the first)."""

    seen_ids: list[str] = field(default_factory=list)
    seen_texts: list[str] = field(default_factory=list)
    updated: float = field(default_factory=time.monotonic)

    def seen(self, message_id: str) -> bool:
        """Whether this message id has already been accepted for this task."""
        return bool(message_id) and message_id in self.seen_ids

    def last_text(self) -> str:
        return self.seen_texts[-1] if self.seen_texts else ""

    def remember(self, message_id: str, text: str) -> None:
        """Record one accepted message for retry detection."""
        if message_id and message_id not in self.seen_ids:
            self.seen_ids.append(message_id)
            del self.seen_ids[:-_MAX_SEEN]
        if text and (not self.seen_texts or self.seen_texts[-1] != text):
            self.seen_texts.append(text)
            del self.seen_texts[:-_MAX_SEEN]


class InboundStore:
    """The inbound tasks one mesh is holding, keyed by ``Task.id``."""

    def __init__(self) -> None:
        self._tasks: dict[str, InboundTask] = {}

    # ── Lifecycle ───────────────────────────────────────────────────────

    def add(
        self,
        task_id: str,
        peer: str,
        *,
        context_id: str = "",
        kind: str = "task",
        message_id: str = "",
        text: str = "",
    ) -> InboundTask:
        """A task just arrived — this process now holds its bridge."""
        if not task_id:
            return InboundTask("", peer, iso_now())
        task = self._tasks.get(task_id)
        if task is None:
            task = InboundTask(
                task_id,
                peer,
                iso_now(),
                context_id=context_id,
                kind=kind,
            )
            self._tasks[task_id] = task
        elif context_id and not task.context_id:
            task.context_id = context_id
        task.remember(message_id, text)
        task.updated = time.monotonic()
        return task

    def begin_round(self, task_id: str, message_id: str, text: str) -> bool:
        """Resume an interrupted task on a continuation's new round."""
        task = self._tasks.get(task_id)
        if task is None or task.state != INTERRUPTED:
            return False
        task.state = WORKING
        task.interrupt = ""
        task.question = ""
        task.round += 1
        task.remember(message_id, text)
        task.updated = time.monotonic()
        return True

    def interrupt(self, task_id: str, interrupt: str, question: str) -> None:
        """The round ended waiting for the peer; no terminal is published."""
        task = self._tasks.get(task_id)
        if task is None or task.state in TERMINAL_STATUSES:
            return
        task.state = INTERRUPTED
        task.interrupt = interrupt
        task.question = question or ""
        task.updated = time.monotonic()

    def terminal(self, task_id: str, state: str, result: str = "") -> None:
        """The task ended; the record stays for dedup until the TTL."""
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.state = state
        task.interrupt = ""
        task.question = ""
        task.result = result or ""
        task.updated = time.monotonic()

    def drop(self, task_id: str) -> None:
        """Forget one task (used by tests and by a forced cleanup)."""
        self._tasks.pop(task_id, None)

    def forget_working(self) -> None:
        """A connection ended: every live round's bridge died with it.

        Interrupted and terminal records stay.  An interrupted task has no
        coroutine to lose, and a terminal one is what a retry replays.
        """
        for task_id in [k for k, v in self._tasks.items() if v.state == WORKING]:
            self._tasks.pop(task_id, None)

    def get(self, task_id: str) -> InboundTask | None:
        return self._tasks.get(task_id)

    # ── Read ────────────────────────────────────────────────────────────

    def pending(self) -> list[InboundTask]:
        """Tasks still open — a round is live, or the peer is waiting on us."""
        rows = [
            task
            for task in self._tasks.values()
            if task.state in (WORKING, INTERRUPTED)
        ]
        return sorted(rows, key=lambda t: t.since or "")

    # ── Maintenance ─────────────────────────────────────────────────────

    def prune(self, ttl: float) -> None:
        """Drop old terminal records, then the oldest of anything over the cap."""
        now = time.monotonic()
        for task_id in [
            k
            for k, v in self._tasks.items()
            if v.state in TERMINAL_STATUSES and now - v.updated > ttl
        ]:
            self._tasks.pop(task_id, None)
        if len(self._tasks) <= MAX_INBOUND:
            return
        excess = len(self._tasks) - MAX_INBOUND + 50
        ordered = sorted(
            self._tasks.values(),
            key=lambda t: (t.state not in TERMINAL_STATUSES, t.updated),
        )
        for task in ordered[:excess]:
            self._tasks.pop(task.task_id, None)
