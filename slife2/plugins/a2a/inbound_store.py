"""Persisted record of inbound A2A tasks — in flight, and orphaned by a restart.

An inbound task's completion bridge is **process-local**.  The SDK responder runs
a task's whole lifecycle (ack → working → artifact → terminal) inside the process
that received it, and ``MeshResponder`` resolves it from a dict that lives no
longer than that process.  The reply path is process-local too: the requester's
``ResponseTopic`` and ``CorrelationData`` are read off the inbound request's MQTT
properties and never leave it.

So a restart orphans every inbound task still in flight — the bridge is gone, and
with it any way to publish the terminal the task is owed.  Nothing on the wire can
fix that: the peer's reply topic is not reconstructible, and the SDK offers no
post-restart completion path.

This store is what lets a restarted process tell those tasks apart from ids it has
simply never seen.  Every inbound task is written here when it arrives and removed
when it leaves the responder, so whatever a fresh process finds on disk is exactly
the set its predecessor died holding.  Those are reported *stale* — never
completable — so the model answers their peers with a plain message instead of
walking into a refused ``task_response``.

**That premise is single-process, so the ledger is per agent** — this is v1's own
reason, and the file has v1's shape.  v1 wrote it to ``a2a_inbound_<agent>.yaml``
in the data directory, with the agent in the *filename*; here an agent's state is
a directory (§5, and `slife2.libs.host.paths`), so it is ``<agent>/a2a.yaml`` beside
``turns.db`` and ``wechat.yaml`` — and that move is why there is an ``agent:`` key
v1 did not need.  The directory holds the **sanitised** name
(``slife2.libs.store.db.safe_agent_name``: ``a/b`` lands in ``a_b``), and whose ledger it is
decides *which conversation an orphan notice is handed to*, so a name read off the
directory would address a
conversation that is not the one that received the task.  Two instances on one
machine are still the normal way to run two agents, and a shared file would have
each read the other's in-flight tasks as its own orphans.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ruamel.yaml.error import YAMLError

from slife2.libs.host import statefile
from slife2.libs.host.paths import a2a_inbound_path

logger = logging.getLogger(__name__)

#: Bound each section — a peer that never answers must not grow the file
#: without limit.  A2A task arrivals are far below this in any sane session.
_MAX_ENTRIES = 200


def _iso_now() -> str:
    """Current UTC time as an ISO-8601 string (official A2A timestamps)."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class InboundTask:
    """One inbound A2A task, as this store tracks it."""

    task_id: str
    peer: str
    """Sending peer's agent name."""

    since: str
    """ISO-8601 UTC arrival time."""

    def as_dict(self) -> dict:
        return {"task_id": self.task_id, "peer": self.peer, "since": self.since}


class InboundStore:
    """In-flight inbound tasks, persisted across restarts.

    ``pending`` — tasks this process is holding a completion bridge for.
    ``stale`` — tasks a *previous* process was holding.  They can never be
    completed, and stay listed until their peer is answered or the task is
    re-registered by a retry.
    """

    def __init__(self, path: Path, agent: str = "") -> None:
        self._path = path
        self._agent = agent
        self._pending: dict[str, InboundTask] = {}
        self._stale: dict[str, InboundTask] = {}
        self._load()

    @classmethod
    def open(cls, agent: str, ledger: str) -> InboundStore:
        """The ledger for one agent *on one network*, created and stamped with its name.

        `ledger` is `Net.ledger`, and it is the network's own file: what a
        ledger records is the completion bridges *this* mesh is holding, so two
        processes serving one agent's name over two networks must not share one
        (see `Net.ledger` for what sharing cost).

        This is the only way a ledger comes into existence, and the file's
        existence says one thing: this agent has had inbound tasks, so a *later*
        start has something to read — `slife2.plugins.a2a.server.announce_ledgers` walks
        `paths.a2a_inbound_paths()` and tells the model what a previous process
        died holding.  **It is not an enrollment list**, and treating it as one
        (a start joining the mesh as every agent that had ever spoken) made
        presence mean "has spoken at least once, ever".  What puts an agent on
        the mesh is a window showing it (`slife2.plugins.a2a.server.join`).

        **So this writes, where v1's constructor only read.**  v1's file turned up
        when a task did, and a shared process needs the file to say *whose* it is
        before any task has arrived — an empty ledger is a name, and the name is
        what an orphan notice is addressed to.  It is also where the
        ``pending → stale`` migration above gets persisted.
        """
        store = cls(a2a_inbound_path(agent, ledger), agent)
        store._save()
        return store

    @classmethod
    def at(cls, path: Path) -> InboundStore | None:
        """The ledger at this path, if it is one — for the loop, which walks.

        ``None`` rather than a guess for a file whose owner is not written down:
        the whole point of the ``agent:`` key is that the directory name is not
        the agent's name, so a file without one cannot be resumed for anybody.
        """
        store = cls(path)
        return store if store.agent else None

    @property
    def agent(self) -> str:
        """Whose ledger this is.  Read once, at open, by whoever walks them."""
        return self._agent

    # ── Load / save ───────────────────────────────────────────────────

    def _load(self) -> None:
        """Adopt the previous run's ``pending`` as ``stale``.

        This process starts holding no bridges, so everything the last one was
        holding is orphaned by definition.  Anything already stale stays stale —
        a task orphaned by a restart two restarts ago is no more completable than
        one orphaned by the last, and dropping it here would silently forgive a
        reply the peer is still owed — but the section is still held to
        :data:`_MAX_ENTRIES` like the live one.

        **A file that cannot be read is refused, not read as empty**
        (DESIGN.md §10.1).  The old answer — a warning and a return — left an
        empty store whose very next `_save` wrote itself over the file, so the
        evidence was consumed with the data, and what was lost that way is the
        one thing this file is for: the peers a previous process died owing an
        answer, which then went neither answered nor reported.  The read raises
        naming the file instead, and because it runs before `open`'s write, the
        refusal is also what makes the overwrite impossible.
        """
        try:
            raw = statefile.reader().load(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, YAMLError) as e:
            raise ValueError(
                f"the inbound ledger at {self._path} could not be read ({e}); "
                f"it is not empty, so reading it as empty would overwrite what "
                f"a previous process left owing — move the file aside to start "
                f"from nothing"
            ) from e
        if not isinstance(raw, dict):
            return
        self._agent = str(raw.get("agent") or self._agent)
        self._stale = self._parse("stale", raw.get("stale"))
        for task_id, task in self._parse("pending", raw.get("pending")).items():
            self._stale.setdefault(task_id, task)
        # ``_trim`` runs on ``pending`` at every add, but ``stale`` is only ever
        # filled HERE — so without this the section was exempt from the cap the
        # class documents, growing across restarts without limit, and the whole
        # list is rendered into every turn's prompt.
        self._trim(self._stale)
        if self._stale:
            logger.info("a2a_inbound_orphaned count=%d", len(self._stale))

    def _parse(self, name: str, section) -> dict[str, InboundTask]:
        """Read one named section of the ledger, refusing what cannot be read.

        **A section that is not a mapping is damage, not an empty section**
        (DESIGN.md §10.1).  Read as ``{}`` it was written back as one on the
        next `_save`, so a peer a previous process died owing an answer was
        dropped from a file whose whole purpose is to remember it — the same
        loss the file-level read refuses one function up, one level down.  A
        non-mapping section, and a non-mapping entry inside one, each raise
        naming the section and the row rather than being quietly forgotten.

        ``None`` is the absent section — no ``pending`` or ``stale`` key at all
        — and that is genuinely empty, so it answers ``{}`` without a word.
        """
        if section is None:
            return {}
        if not isinstance(section, dict):
            raise ValueError(
                f"the {name!r} section of the inbound ledger at {self._path} is "
                f"not a mapping ({section!r}); it is not empty, so reading it as "
                f"empty would overwrite what a previous process left owing"
            )
        out: dict[str, InboundTask] = {}
        for task_id, entry in section.items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f"the entry {task_id!r} in the {name!r} section of the "
                    f"inbound ledger at {self._path} is not a mapping ({entry!r}); "
                    f"it is not empty, so reading it as empty would overwrite what "
                    f"a previous process left owing"
                )
            out[str(task_id)] = InboundTask(
                task_id=str(task_id),
                peer=str(entry.get("peer", "unknown")),
                since=str(entry.get("since", "")),
            )
        return out

    def _save(self) -> None:
        """Write atomically — a half-written file read by the next process
        would look like corruption (and the crash that caused it is exactly
        when the stale list matters most).

        A temp file in the same directory and `os.replace`, the same guarantee
        `slife2.plugins.wechat.server.save_session` gives and for the same reason: this file is
        rewritten on every inbound task's arrival and completion.  There is
        nothing here to preserve from the *previous* write — it is
        machine-written — so the atomic replace alone is the guarantee it needs:
        a reader sees all of the old file or all of the new one.

        **A write that did not land is reported, not whispered.**  This file is
        the only line between a task that is orphaned and one that was never
        seen (DESIGN.md §1.8), so a write that fails costs exactly the fact the
        next process would have read — and nothing else will ever say so.  It is
        still not the caller's failure: an inbound task was answered from
        memory, and refusing the arrival because the disk is full would fail a
        turn over bookkeeping.  So the failure is not raised — it is an error
        naming the file, which is the only place an operator will see it — and
        the caller goes on.
        """
        payload = {
            "agent": self._agent,
            "pending": {k: v.as_dict() for k, v in self._pending.items()},
            "stale": {k: v.as_dict() for k, v in self._stale.items()},
        }
        try:
            statefile.write(self._path, payload)
        except OSError as e:
            logger.error(
                "a2a_inbound_state_write_failed path=%s err=%s — what this "
                "process is holding is not on disk, so a restart would read it "
                "as ids it has never seen",
                self._path,
                e,
            )

    @staticmethod
    def _trim(section: dict[str, InboundTask]) -> None:
        """Drop the oldest entries past the cap (in place)."""
        if len(section) <= _MAX_ENTRIES:
            return
        ordered = sorted(section.values(), key=lambda t: t.since or "")
        for task in ordered[: len(section) - _MAX_ENTRIES]:
            section.pop(task.task_id, None)

    # ── Live inbound tasks ────────────────────────────────────────────

    def add(self, task_id: str, peer: str) -> None:
        """A task just arrived — this process now holds its bridge.

        Clears any stale entry for the same id: a peer that retries re-sends
        the same ``Task.id``, and the retry genuinely re-registers the bridge,
        so the task is completable again.
        """
        if not task_id:
            return
        was_stale = self._stale.pop(task_id, None)
        self._pending[task_id] = InboundTask(task_id, peer, _iso_now())
        self._trim(self._pending)
        if was_stale is not None:
            logger.info("a2a_inbound_revived task=%s peer=%s", task_id, peer)
        self._save()

    def drop(self, task_id: str) -> None:
        """The task left the responder — completed, cancelled, or the peer
        vanished.  It is no longer awaiting anything from this process."""
        if self._pending.pop(task_id, None) is None:
            return
        self._save()

    # ── Stale (restart-orphaned) tasks ────────────────────────────────

    def stale(self) -> list[InboundTask]:
        """Tasks a previous process died holding, oldest first."""
        return sorted(self._stale.values(), key=lambda t: t.since or "")

    def stale_entry(self, task_id: str) -> InboundTask | None:
        return self._stale.get(task_id)

    def clear_stale_peer(self, peer: str) -> None:
        """Forget this peer's orphaned tasks — the model has answered them
        with a plain message, which is the only reply they can still get."""
        drop = [k for k, v in self._stale.items() if v.peer == peer]
        if not drop:
            return
        for task_id in drop:
            self._stale.pop(task_id, None)
        self._save()
