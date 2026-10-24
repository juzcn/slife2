"""In-memory record of the inbound A2A tasks one mesh is holding.

An inbound task's completion bridge is **process-local**.  The SDK responder runs
a task's whole lifecycle (ack → working → artifact → terminal) inside the process
that received it, and ``MeshResponder`` resolves it from a map that lives no
longer than that process.  The reply path is process-local too: the requester's
``ResponseTopic`` and ``CorrelationData`` are read off the inbound request's MQTT
properties and never leave it.

So a restart **drops** every inbound task the previous process was holding: the
bridge is gone, and with it any way to publish the terminal the task is owed.
Nothing on the wire can fix that — the peer's reply topic is not reconstructible,
and the SDK offers no post-restart completion path.  This build therefore starts
empty and does not carry a `stale` record or announce the loss to the model.

**A peer that is still waiting is visible on the peer's side**, where the party
who can act on it is: its own `task_list` names the task it sent and never heard
back about, and it can cancel or re-send.  A notice here would not settle
anything — the only reply an orphaned task can still get is a plain message, and
a plain message does not complete the peer's task either — so it would be the
harness speaking for a conversation it cannot finish (SDD §4.5).

The store is per mesh, which is per `(agent, subagent)` and per network: bridges
are held by one mesh's responder, and two meshes must never share the map.
Nothing here touches the disk; a restart is the process starting, and its
in-flight set is empty by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


def iso_now() -> str:
    """Current UTC time as an ISO-8601 string (official A2A timestamps).

    Public because it is a **shape** and not this store's clock: a task's row
    says when it started wherever it is reported from, and two spellings of the
    same timestamp in one answer would be the reader's problem to reconcile
    (`slife2.plugins.a2a.mesh`'s outgoing sends stamp themselves with this too).
    """
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class InboundTask:
    """One inbound A2A task this process is holding a bridge for."""

    task_id: str
    peer: str
    """Sending peer's agent name."""

    since: str
    """ISO-8601 UTC arrival time."""


class InboundStore:
    """The inbound tasks one mesh is holding a completion bridge for.

    ``pending`` — tasks this process is holding a bridge for.  A restart has
    none, which is the whole of the restart rule: the map is the process.
    """

    def __init__(self) -> None:
        self._pending: dict[str, InboundTask] = {}

    # ── Live inbound tasks ────────────────────────────────────────────

    def add(self, task_id: str, peer: str) -> None:
        """A task just arrived — this process now holds its bridge."""
        if not task_id:
            return
        self._pending[task_id] = InboundTask(task_id, peer, iso_now())

    def drop(self, task_id: str) -> None:
        """The task left the responder — completed, cancelled, or the peer
        vanished.  It is no longer awaiting anything from this process."""
        self._pending.pop(task_id, None)

    # ── Read ──────────────────────────────────────────────────────────

    def pending(self) -> list[InboundTask]:
        """Tasks this process is holding a completion bridge for, oldest first.

        **A bridge is a task still owed an answer**, which is what `task_list`
        reports (`slife2.plugins.a2a.mesh.open_tasks`): the model that has lost
        track of what it was going to answer has this to ask, and the
        `[Agent:…]`/`[Subagent:…]`/`[PeerAgent:…]` marker in its own turn is the
        id.
        """
        return sorted(self._pending.values(), key=lambda t: t.since or "")
