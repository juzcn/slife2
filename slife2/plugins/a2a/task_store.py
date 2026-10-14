"""Task store — shared A2A task-lifecycle tracking.

Every A2A operation (send, result, cancel) records metadata here so the mesh can
attribute results to their send and answer cancel/status lookups.  Internal
bookkeeping only — the wire is the official ``a2a-over-mqtt`` SDK.

v1's module, ported as it stands.  The one thing worth saying about the port is
that the same in-memory store serves every agent's mesh in this process — v1 had
one mesh per process, so a singleton was the same thing as "this agent's".  It is
still right here, and for a reason rather than by inheritance: a task id is a
`uuid4().hex`, so two agents cannot collide in the table, and the agent a record
names is written into the record and read from it rather than taken from
whichever mesh is asking.  What the sharing costs is one prune cap instead of
one per agent, which is a bound on memory and not a fact about any task.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field

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
    """One of ``"pending"``, ``"completed"``, ``"failed"``, ``"cancelled"``."""

    created_at: float = field(default_factory=_time.monotonic)


# ── Task store ──────────────────────────────────────────────────────────


class TaskStore:
    """Task-lifecycle store shared by every mesh in this process."""

    MAX_RECORDS = 500  # soft cap — oldest completed entries pruned first

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}

    # ── Write ─────────────────────────────────────────────────────────

    def record_send(self, task_id: str, agent_name: str) -> TaskRecord:
        """Record a newly-sent task (status = pending)."""
        rec = TaskRecord(
            task_id=task_id,
            agent_name=agent_name,
            status="pending",
        )
        self._records[task_id] = rec
        self._maybe_prune()
        return rec

    def record_result(self, task_id: str) -> TaskRecord | None:
        """Mark a task as completed.

        A task that is already in a terminal state (cancelled or failed) stays
        there — a result arriving from a peer after the caller was told about a
        cancel must not flip the record back to completed and contradict what
        the caller saw.  A ``pending`` record flips to completed here and the
        late result still delivers.
        """
        rec = self._records.get(task_id)
        if rec is None:
            return None
        if rec.status in ("cancelled", "failed"):
            return rec
        rec.status = "completed"
        return rec

    def record_error(self, task_id: str) -> TaskRecord | None:
        """Mark a task as failed.

        **A withdrawal outranks a failure, for `record_result`'s reason.**  A
        caller who was told `cancelled` has already acted on it, and a peer that
        answers the cancel with `failed` — the SDK publishes exactly that when
        its handler raises on a cancelled task — must not rewrite the ending the
        caller saw.  The two recorders are one rule with one exception, and it
        was only in the other one.
        """
        rec = self._records.get(task_id)
        if rec is None:
            return None
        if rec.status == "cancelled":
            return rec
        rec.status = "failed"
        return rec

    def record_cancel(self, task_id: str) -> TaskRecord | None:
        """Mark a task as cancelled."""
        rec = self._records.get(task_id)
        if rec is None:
            return None
        rec.status = "cancelled"
        return rec

    # ── Read ──────────────────────────────────────────────────────────

    def get(self, task_id: str) -> TaskRecord | None:
        """Return a task record by id, or ``None``."""
        return self._records.get(task_id)

    # ── Maintenance ───────────────────────────────────────────────────

    def _maybe_prune(self) -> None:
        """Drop oldest entries when over max — terminal status first, then the
        oldest pending so a burst of async sends to slow/hung peers can't grow
        the in-memory store past the cap."""
        if len(self._records) <= self.MAX_RECORDS:
            return
        excess = len(self._records) - self.MAX_RECORDS + 50
        # Oldest first; terminal-status entries preferred for removal.
        ordered = sorted(
            self._records.values(),
            key=lambda r: (
                r.status not in ("completed", "cancelled", "failed"),
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
