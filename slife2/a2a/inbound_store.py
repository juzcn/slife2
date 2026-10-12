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
a directory (§5, and `slife2.paths`), so it is ``<agent>/a2a.yaml`` beside
``turns.db`` and ``wechat.yaml`` — and that move is why there is an ``agent:`` key
v1 did not need.  The directory holds the **sanitised** name
(``slife2.db.safe_agent_name``: ``a/b`` lands in ``a_b``), and whose ledger it is
decides *which conversation an orphan notice is handed to* and *which name the
mesh resumes under*, so a name read off the directory would address a
conversation that is not the one that received the task.  Two instances on one
machine are still the normal way to run two agents, and a shared file would have
each read the other's in-flight tasks as its own orphans.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from slife2.paths import a2a_inbound_path

logger = logging.getLogger(__name__)

#: Bound each section — a peer that never answers must not grow the file
#: without limit.  A2A task arrivals are far below this in any sane session.
_MAX_ENTRIES = 200


def _yaml() -> YAML:
    """The writer and reader for this file, set up the way `slife2.wechat`'s is.

    Block style, so the file reads as the document it is meant to be — an entry
    in it is a peer still owed a reply, and that is a thing a person may open.
    """
    yaml = YAML()
    yaml.default_flow_style = False
    return yaml


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
    def open(cls, agent: str) -> InboundStore:
        """The ledger for one agent, created and stamped with its name.

        This is the only way a ledger comes into existence, and — with the file
        itself — the record that this agent is on the mesh: the plugin resumes
        from `paths.a2a_inbound_paths()`, so the file's existence is why an agent
        is reachable the next time slife2 starts.

        **So this writes, where v1's constructor only read.**  v1's file turned up
        when a task did, because v1 had a plugin process per agent and always knew
        which names to be reachable under.  A shared process learns that from
        these files and nowhere else, so an agent that has only ever *sent* would
        be one nothing resumes — reachable the day it spoke and gone the next
        morning.  Writing an empty ledger at open is what makes the file the
        enrollment it is described as; it is also where the `pending → stale`
        migration above gets persisted.
        """
        store = cls(a2a_inbound_path(agent), agent)
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
        """
        try:
            raw = _yaml().load(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, YAMLError) as e:
            # A corrupt state file must never block the mesh — losing it costs
            # the stale list, which is a reminder, not protocol state.
            logger.warning("a2a_inbound_state_unreadable err=%s", e)
            return
        if not isinstance(raw, dict):
            return
        self._agent = str(raw.get("agent") or self._agent)
        self._stale = self._parse(raw.get("stale"))
        for task_id, task in self._parse(raw.get("pending")).items():
            self._stale.setdefault(task_id, task)
        # ``_trim`` runs on ``pending`` at every add, but ``stale`` is only ever
        # filled HERE — so without this the section was exempt from the cap the
        # class documents, growing across restarts without limit, and the whole
        # list is rendered into every turn's prompt.
        self._trim(self._stale)
        if self._stale:
            logger.info("a2a_inbound_orphaned count=%d", len(self._stale))

    def _parse(self, section) -> dict[str, InboundTask]:
        out: dict[str, InboundTask] = {}
        if not isinstance(section, dict):
            return out
        for task_id, entry in section.items():
            if not isinstance(entry, dict):
                continue
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
        `slife2.wechat.save_session` gives and for the same reason: this file is
        rewritten on every inbound task's arrival and completion.  There is
        nothing here to preserve — it is machine-written, and losing it costs a
        reminder rather than protocol state — so the atomic replace alone is the
        guarantee it needs: a reader sees all of the old file or all of the new
        one.
        """
        payload = {
            "agent": self._agent,
            "pending": {k: v.as_dict() for k, v in self._pending.items()},
            "stale": {k: v.as_dict() for k, v in self._stale.items()},
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as opened:
                _yaml().dump(payload, opened)
                opened.flush()
                os.fsync(opened.fileno())
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning("a2a_inbound_state_write_failed err=%s", e)

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
