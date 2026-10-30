"""Task store — shared A2A task-lifecycle tracking.

Every A2A operation (send, result, cancel, continuation) records metadata here
so the mesh can attribute results to their send and answer cancel/status
lookups.  Internal bookkeeping only — the wire is the official
``a2a-over-mqtt`` SDK.

**A terminal status never flips.**  The four terminal words are the profile's
``completed``/``failed``/``cancelled``/``rejected``; once one is written, a
late result, error, rejection or withdrawal leaves it alone.  `interrupted`
is the one non-terminal word beyond `pending`: the peer paused the task and a
continuation can resume it.

The same in-memory store serves every agent's mesh in this process.  That is
right for a reason rather than by inheritance: a task id is a `uuid4` hex
string, so two agents cannot collide in the table, and the agent a record
names is written into the record and read from it rather than taken from
whichever mesh is asking.  What the sharing costs is one prune cap instead of
one per agent, which is a bound on memory and not a fact about any task.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field

# ── Status vocabulary ───────────────────────────────────────────────────

PENDING = "pending"
INTERRUPTED = "interrupted"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
REJECTED = "rejected"

#: A task cannot leave these.  One tuple so a guard, the prune order and the
#: display cannot drift from one another.
TERMINAL_STATUSES = (COMPLETED, FAILED, CANCELLED, REJECTED)

# ── Task record ─────────────────────────────────────────────────────────


@dataclass
class TaskRecord:
    """Lifecycle metadata for one A2A task — what the mesh routes on.

    Deliberately narrow: the mesh reads ``agent_name`` to attribute a terminal
    reply and ``status`` to answer cancel/status lookups, so those (plus the id
    and the prune clock) are the whole record.  The task text and the result
    body live on the wire and in the conversation, not here.
    """

    task_id: str
    """Unique correlation / rpc id."""

    agent_name: str
    """Target agent this task was sent to."""

    status: str
    """One of ``PENDING``, ``INTERRUPTED``, or `TERMINAL_STATUSES`."""

    created_at: float = field(default_factory=_time.monotonic)


# ── Task store ──────────────────────────────────────────────────────────


class TaskStore:
    """Task-lifecycle store shared by every mesh in this process."""

    MAX_RECORDS = 500  # soft cap — oldest terminal entries pruned first

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}

    # ── Write ─────────────────────────────────────────────────────────

    def record_send(self, task_id: str, agent_name: str) -> TaskRecord:
        """Record a newly-sent task (status = pending)."""
        rec = TaskRecord(
            task_id=task_id,
            agent_name=agent_name,
            status=PENDING,
        )
        self._records[task_id] = rec
        self._maybe_prune()
        return rec

    def record_resume(self, task_id: str) -> TaskRecord | None:
        """A continuation went out: the task is running again, not waiting."""
        rec = self._records.get(task_id)
        if rec is None:
            return None
        if rec.status == INTERRUPTED:
            rec.status = PENDING
        return rec

    def record_interrupt(self, task_id: str) -> TaskRecord | None:
        """The peer paused the task; it is not terminal and can be continued."""
        rec = self._records.get(task_id)
        if rec is None:
            return None
        if rec.status not in TERMINAL_STATUSES:
            rec.status = INTERRUPTED
        return rec

    def record_result(self, task_id: str) -> TaskRecord | None:
        """Mark a task as completed.

        A task that is already terminal stays there — a result arriving from a
        peer after the caller was told about a cancel/rejection must not flip
        the record back to completed and contradict what the caller saw.
        """
        rec = self._records.get(task_id)
        if rec is None:
            return None
        if rec.status in TERMINAL_STATUSES:
            return rec
        rec.status = COMPLETED
        return rec

    def record_error(self, task_id: str) -> TaskRecord | None:
        """Mark a task as failed.  A terminal status is never rewritten."""
        rec = self._records.get(task_id)
        if rec is None:
            return None
        if rec.status in TERMINAL_STATUSES:
            return rec
        rec.status = FAILED
        return rec

    def record_reject(self, task_id: str) -> TaskRecord | None:
        """The peer declined the task.  It is terminal, and not a failure."""
        rec = self._records.get(task_id)
        if rec is None:
            return None
        if rec.status in TERMINAL_STATUSES:
            return rec
        rec.status = REJECTED
        return rec

    def record_cancel(self, task_id: str) -> TaskRecord | None:
        """Mark a task as cancelled.  A terminal status is never rewritten."""
        rec = self._records.get(task_id)
        if rec is None:
            return None
        if rec.status in TERMINAL_STATUSES:
            return rec
        rec.status = CANCELLED
        return rec

    # ── Read ──────────────────────────────────────────────────────────

    def get(self, task_id: str) -> TaskRecord | None:
        """Return a task record by id, or ``None``."""
        return self._records.get(task_id)

    # ── Maintenance ───────────────────────────────────────────────────

    def _maybe_prune(self) -> None:
        """Drop oldest entries when over max — terminal status first, then the
        oldest pending, so a burst of async sends to slow/hung peers can't grow
        the in-memory store past the cap."""
        if len(self._records) <= self.MAX_RECORDS:
            return
        excess = len(self._records) - self.MAX_RECORDS + 50
        # Oldest first; terminal-status entries preferred for removal.
        ordered = sorted(
            self._records.values(),
            key=lambda r: (
                r.status not in TERMINAL_STATUSES,
                r.created_at,
            ),
        )
        to_remove = ordered[:excess]
        for r in to_remove:
            self._records.pop(r.task_id, None)


# ── Module-level singleton ──────────────────────────────────────────────

_store: TaskStore | None = None


def get_store() -> TaskStore:
    """Return the module-level :class:`TaskStore` singleton.

    Created on first access if not already set by the plugin.
    """
    global _store
    if _store is None:
        _store = TaskStore()
    return _store
