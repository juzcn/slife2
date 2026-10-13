"""The scheduled-task ledger: what is scheduled, and what came of it.

**Two tables and one rule that is not written down as a constraint: no note
means it failed.**  A **task** is a name, a description and a cron expression; a
**run** is one fire of it, keyed by the moment it was due.  Nothing here decides
*when* anything happens — that is `slife2.schedule` and the poll loop — and
nothing here decides what a run means to the person reading it: this is the
write-and-read half, kept apart for the reason `slife2.loop` is kept apart from
the agent server, so that the rules about fires can be read without a database in
the way.

**`UNIQUE(task_id, due_at)` is the whole of the idempotence.**  A poll that ran
twice, a sweep that re-read a ledger, a model backfilling a run it already
backfilled — all of them arrive as the same insert, and the second one changes
nothing.  That is why a run is addressed by *when it was due* rather than by an
id: the due time is the run's identity, it is a thing the model can say, and a
minted handle would be a second name for the same fact that can be lost.

**One file per agent, and the file says whose it is.**  §5 isolates agents by
file rather than by a `WHERE` clause, and this is a store with one writer, so
`<data>/agents/<agent>/schedule.db` is the same shape `turns.db` has — see
`slife2.paths`.  The `meta` row exists because the directory *cannot* carry the
name: `safe_agent_name` is injective but not the identity (`a/b` becomes `a_b`),
and the poll loop, which is shared, has nothing but the directory to go on.
Firing a task by a name that has been sanitised would hand the trigger to a
conversation that is not the one that asked for it — the failure is silent, so it
is worth the one row.

A **row is never deleted**, except with its task.  What a scheduled task did last
month is the reason the ledger exists; a `remove` that took the history with it
would leave the one question a schedule raises — "did it run?" — unanswerable
after somebody tidies up.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import slife2.timeouts as _T
from slife2.clock import now

#: Every state a run can be in, in the order they can be reached.
#:
#: `pending` is written **before** the hand-over rather than after it, which is
#: what makes an interrupted fire visible instead of invisible: a run whose
#: trigger was never answered is a `pending` row the next start sweeps up, where
#: a row written after the hand-over would simply never have existed.
PENDING = "pending"
RAN = "ran"
FAILED = "failed"
MISSED = "missed"
SKIPPED = "skipped"

#: The ones a run can still be moved out of — everything a person or a model may
#: still have to settle.  `ran` and `skipped` are the two endings.
UNSETTLED = (PENDING, FAILED, MISSED)

STATUSES = (PENDING, RAN, FAILED, MISSED, SKIPPED)

#: How many runs a ledger read gives back when the caller does not say.  Here
#: rather than in the plugin because it is a fact about reading this table — a
#: ledger grows for as long as a task lives, so every read of it is bounded —
#: while the *ceiling* a tool will accept is the tool's own business.
RUNS_LIMIT = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    cron        TEXT NOT NULL,
    timezone    TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    last_due    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS scheduled_runs (
    id            INTEGER PRIMARY KEY,
    task_id       INTEGER NOT NULL REFERENCES scheduled_tasks(id),
    due_at        TEXT NOT NULL,
    status        TEXT NOT NULL,
    ran_at        TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    -- The background task this run's work is being done by, once it has been
    -- started.  Empty until then, and empty forever for a run that never got
    -- that far — which is the difference between "in flight" and "nobody acted
    -- on the trigger", and the reason this is a column rather than a guess made
    -- from the task's name.
    agent_task_id TEXT NOT NULL DEFAULT '',
    UNIQUE(task_id, due_at)
);

CREATE INDEX IF NOT EXISTS idx_runs_task ON scheduled_runs(task_id, due_at);
"""


@dataclass(frozen=True)
class Task:
    """One scheduled task, as it is stored.

    `last_due` is the **anchor**, and it is a column rather than a `MAX(due_at)`
    over the runs for two reasons: a backfill inserts a *late* row with an *old*
    due time, so the newest row is not the newest fire; and the anchor is read on
    every poll, where a scan of a task's whole run history is a scan that grows
    for as long as the task lives.
    """

    id: int
    name: str
    description: str
    cron: str
    timezone: str
    enabled: bool
    created_at: str
    updated_at: str
    last_due: str

    def row(self) -> dict[str, Any]:
        """The fields a tool answers with.

        One shape for every reader, so a model never has to translate between
        the row `schedule_list` gave it and the one `schedule_set` did.
        """
        return {
            "name": self.name,
            "description": self.description,
            "cron": self.cron,
            "timezone": self.timezone,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_due": self.last_due,
        }


@dataclass(frozen=True)
class Run:
    """One fire, and what came of it."""

    id: int
    task_id: int
    #: The moment it was *due*, which is the run's identity — not when it ran,
    #: which is a fact about the machine rather than about the schedule.
    due_at: str
    status: str
    ran_at: str
    note: str
    error: str
    #: The agent task doing this run's work, as agent-task named it, once
    #: `schedule_run_now` has started one.  This is the whole reason a run can be
    #: closed without the model having to remember anything: the plugin made the
    #: call, so it holds the id, so it can ask what became of it.
    agent_task_id: str = ""
    #: The task's name, carried on the value rather than looked up per row:
    #: `schedule_runs` lists across tasks, and a row that cannot say which task
    #: it belongs to is a row nobody can act on.  Empty only for a `Run` built
    #: without one, which no reader in this project does.
    name: str = ""

    def row(self) -> dict[str, Any]:
        """The fields a tool answers with, in one shape for every reader."""
        return {
            "name": self.name,
            "due_at": self.due_at,
            "status": self.status,
            "ran_at": self.ran_at,
            "note": self.note,
            "error": self.error,
            "agent_task_id": self.agent_task_id,
        }


def database_path(agent: str) -> Path:
    """Where one agent's ledger lives.

    Imported from `slife2.paths` rather than spelled out, so that the one
    statement of the layout stays the one statement.
    """
    from slife2.paths import schedule_db

    return schedule_db(agent)


class ScheduleStore:
    """One agent's ledger.

    A connection per call, like `slife2.db.TurnStore` and for the same reason:
    the file is the thing, and a handle held open across a poll is a handle to
    keep in step with a restart.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._ensure_schema()

    # ── opening ──────────────────────────────────────────────────────────

    @classmethod
    def open(cls, agent: str) -> ScheduleStore:
        """The ledger for one agent, created and stamped with its name.

        This is the only way a ledger comes into existence, and the reason the
        stamp is trustworthy: a file it did not make has no `meta` row, and
        `adopt` refuses one.
        """
        store = cls(database_path(agent))
        with store._connect() as connection:
            connection.execute(
                "INSERT INTO meta(key, value) VALUES('agent', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (agent,),
            )
        return store

    @classmethod
    def adopt(cls, path: Path) -> ScheduleStore | None:
        """The ledger at this path, if it is one — for the loop, which walks.

        `None` rather than a guess for a `schedule.db` whose owner is not written
        down: the whole point of the `meta` row is that the directory name is not
        the agent's name, so a file without one cannot be fired for anybody.
        """
        store = cls(path)
        with store._connect() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'agent'"
            ).fetchone()
        return store if row and row["value"] else None

    @property
    def agent(self) -> str:
        """Whose ledger this is.  Read once, at open, by the poll."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'agent'"
            ).fetchone()
        return str(row["value"]) if row else ""

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=_T.timeouts.storage.sqlite_busy)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            f"PRAGMA busy_timeout={int(_T.timeouts.storage.sqlite_busy * 1000)}"
        )
        return connection

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    # ── tasks ────────────────────────────────────────────────────────────

    def tasks(self, *, enabled_only: bool = False) -> list[Task]:
        """Every task, oldest first.  `enabled_only` is what the poll asks."""
        query = "SELECT * FROM scheduled_tasks"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY name"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [_task(row) for row in rows]

    def task(self, name: str) -> Task | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE name = ?", (name,)
            ).fetchone()
        return _task(row) if row else None

    def put_task(
        self,
        name: str,
        description: str,
        cron: str,
        timezone: str = "",
        *,
        enabled: bool = True,
    ) -> Task:
        """Write a task, whole, whether or not it already exists.

        **An upsert rather than a patch**: the arguments are the task, so a field
        left out is a field cleared, and there is no way to reach a task that is
        half of one description and half of another.  What that costs is that
        `enabled` has to be said every time, which is why it defaults to the
        answer somebody setting a task almost always means.

        **`last_due` moves to now when a task starts being enabled.**  A new task
        has no fires behind it, and a task switched back on has none that were
        *missed* — it was off — so the anchor is reset in exactly the two cases
        where leaving it would report a gap that nobody experienced.  A task that
        was already on keeps its anchor, so editing a description does not
        re-fire the hour that just passed.
        """
        moment = now()
        existing = self.task(name)
        anchor = existing.last_due if existing else moment
        if existing is not None and not existing.enabled and enabled:
            anchor = moment
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scheduled_tasks
                    (name, description, cron, timezone, enabled,
                     created_at, updated_at, last_due)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description = excluded.description,
                    cron        = excluded.cron,
                    timezone    = excluded.timezone,
                    enabled     = excluded.enabled,
                    updated_at  = excluded.updated_at,
                    last_due    = excluded.last_due
                """,
                (
                    name,
                    description,
                    cron,
                    timezone,
                    int(enabled),
                    existing.created_at if existing else moment,
                    moment,
                    anchor,
                ),
            )
            row = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE name = ?", (name,)
            ).fetchone()
        return _task(row)

    def remove_task(self, name: str) -> bool:
        """Forget a task and everything it ever did.

        The one delete here, and it is the caller's to make: `remove` is the
        model saying "this is not a thing any more", which is a different
        statement from switching it off, and keeping the runs of a task that no
        longer exists would be keeping rows nothing can name.
        """
        task = self.task(name)
        if task is None:
            return False
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM scheduled_runs WHERE task_id = ?", (task.id,)
            )
            connection.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task.id,))
        return True

    def advance(self, task: Task, due_at: str) -> None:
        """Move a task's anchor forward, if this fire is newer than it.

        Compared as moments rather than as strings: `slife2.clock` writes local
        time *with its offset*, and two rows either side of a DST change are two
        offsets — where one string being greater than another says nothing.
        """
        if not due_at:
            return
        if task.last_due and moment_of(due_at) <= moment_of(task.last_due):
            return
        with self._connect() as connection:
            connection.execute(
                "UPDATE scheduled_tasks SET last_due = ? WHERE id = ?",
                (due_at, task.id),
            )

    # ── runs ─────────────────────────────────────────────────────────────

    def record(self, task: Task, due_at: str) -> Run:
        """Write a run as `pending`, or hand back the one already there.

        **Insert-or-nothing, then read.**  A settled run is never reopened by
        this — a fire that ran is not re-recorded by a poll that ran twice — and
        a run that already exists comes back as it is, which is what lets the
        caller see `ran` and decide there is nothing left to do.
        """
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO scheduled_runs(task_id, due_at, status) "
                "VALUES (?, ?, ?) ON CONFLICT(task_id, due_at) DO NOTHING",
                (task.id, due_at, PENDING),
            )
        self.advance(task, due_at)
        found = self.run(task.name, due_at)
        assert found is not None  # just inserted, or already there
        return found

    def run(self, name: str, due_at: str) -> Run | None:
        task = self.task(name)
        if task is None:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_runs WHERE task_id = ? AND due_at = ?",
                (task.id, due_at),
            ).fetchone()
        return _run(row, name) if row else None

    def runs(
        self, name: str = "", status: str = "", limit: int = RUNS_LIMIT
    ) -> list[Run]:
        """The ledger, newest first, optionally narrowed.

        Newest by *when the run was due*, because that is the run's identity —
        and the ordering is done on the stored string rather than on a parsed
        moment, which is the one place this module trusts `slife2.clock`'s
        spelling: every row here was written by this process on this machine, so
        they share an offset and compare as the timestamps they look like.
        """
        query = (
            "SELECT r.*, t.name AS task_name FROM scheduled_runs r "
            "JOIN scheduled_tasks t ON t.id = r.task_id WHERE 1 = 1"
        )
        parameters: list[Any] = []
        if name:
            query += " AND t.name = ?"
            parameters.append(name)
        if status:
            query += " AND r.status = ?"
            parameters.append(status)
        query += " ORDER BY r.due_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_run(row, row["task_name"]) for row in rows]

    def newest(self, name: str, statuses: tuple[str, ...] = ()) -> Run | None:
        """A task's newest run, optionally restricted to some states.

        "Newest" by *when it was due*, not by rowid: a backfilled run is inserted
        late and belongs where its due time puts it.
        """
        task = self.task(name)
        if task is None:
            return None
        query = "SELECT * FROM scheduled_runs WHERE task_id = ?"
        parameters: list[Any] = [task.id]
        if statuses:
            query += f" AND status IN ({','.join('?' * len(statuses))})"
            parameters.extend(statuses)
        query += " ORDER BY due_at DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return _run(row, name) if row else None

    def settle(
        self,
        name: str,
        due_at: str,
        status: str,
        *,
        note: str = "",
        error: str = "",
        when: str = "",
    ) -> Run | None:
        """Move a run to an ending, and say what it produced.

        `ran_at` is stamped only for `ran`: a run that was skipped or that never
        happened has no moment it happened at, and writing one would make the
        ledger claim a completion that did not occur.  `when` is that moment,
        for the caller that knows it better than this process does — the harness
        closing a run reads it off the background task, which finished before
        anybody noticed.

        **A run that has already ended is not settled again.**  `ran` and
        `skipped` are the two endings — `UNSETTLED` names the ones still open —
        and an UPDATE that did not ask would let the last thing anybody said
        rewrite the ledger: `schedule_run_skip` on a run that already `ran` would
        clear its `ran_at` and file it as skipped, which is the same "history is
        a function of the last thing anybody said" that `reopen` refuses to do.
        The caller is told nothing happened by getting the row back unchanged,
        exactly as it is when a reopen is refused.
        """
        if status not in STATUSES:
            raise ValueError(f"{status!r} is not a run state")
        found = self.run(name, due_at)
        if found is None or found.status not in UNSETTLED:
            return found
        with self._connect() as connection:
            connection.execute(
                "UPDATE scheduled_runs SET status = ?, ran_at = ?, note = ?, "
                "error = ? WHERE id = ?",
                (
                    status,
                    (when or now()) if status == RAN else "",
                    note,
                    error,
                    found.id,
                ),
            )
        return self.run(name, due_at)

    def reopen(self, name: str, due_at: str, *, note: str = "") -> Run | None:
        """Put an unsettled run back to `pending`, ready to be fired again.

        What `schedule_run_now` does to a run that was missed or that failed: the
        run is the same run — same identity, same due time — and backing it is
        answering the question it raised rather than starting a new one.

        **A run that ended is not reopened.**  `ran` and `skipped` are answers,
        and a tool that quietly turned one back into work would make the ledger's
        history a function of the last thing anybody said.  The caller is told
        nothing happened by getting the row back unchanged.

        **The task id goes with the ending, and only with the ending.**  A run
        that *failed* after its background task started still carries that task's
        id, and reopening only the status would leave a `pending` row that still
        names a task — `schedule_run_now`'s in-flight guard reads exactly that
        pair, so the backfill would answer without dispatching anything, do
        nothing, and be settled `failed` again by the next reconcile.  Clearing
        the column is what makes that reopened run a run nobody is doing yet.

        But a run that is still `pending` *is* being done, or is waiting to be,
        and its id is how the guard knows: clearing it there would make a second
        `schedule_run_now` for the same due time dispatch a second task, which is
        the repeat the guard exists to prevent.  So the id is kept for `pending`
        and dropped for the two states that are genuinely over.
        """
        found = self.run(name, due_at)
        if found is None or found.status not in UNSETTLED:
            return found
        task_id = found.agent_task_id if found.status == PENDING else ""
        with self._connect() as connection:
            connection.execute(
                "UPDATE scheduled_runs SET status = ?, ran_at = '', note = ?, "
                "error = '', agent_task_id = ? WHERE id = ?",
                (PENDING, note, task_id, found.id),
            )
        return self.run(name, due_at)

    def attach(self, name: str, due_at: str, agent_task_id: str) -> Run | None:
        """Say which background task is doing this run's work.

        Written the moment the task has started, so that a process which stops
        afterwards leaves a row that says what it was waiting on.  A run whose
        work never got started keeps an empty column, which is how the poll tells
        "in flight" from "nobody acted on the trigger".
        """
        task = self.task(name)
        if task is None:
            return None
        with self._connect() as connection:
            connection.execute(
                "UPDATE scheduled_runs SET agent_task_id = ? "
                "WHERE task_id = ? AND due_at = ?",
                (agent_task_id, task.id, due_at),
            )
        return self.run(name, due_at)

    def in_flight(self) -> list[Run]:
        """Runs whose work is being done right now: `pending`, with a task.

        What the poll asks the agent-task plugin about.  A `pending` run with no
        task is *not* here — it is a trigger nobody acted on, which is a different
        thing and one this plugin deliberately does not chase (DESIGN.md §9).
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT r.*, t.name AS task_name FROM scheduled_runs r "
                "JOIN scheduled_tasks t ON t.id = r.task_id "
                "WHERE r.status = ? AND r.agent_task_id <> '' "
                "ORDER BY r.due_at",
                (PENDING,),
            ).fetchall()
        return [_run(row, row["task_name"]) for row in rows]

    def sweep(self) -> int:
        """Fail every `pending` run a stopped process left behind.

        **The one thing a crash can leave that nothing else can resolve.**  A
        `pending` row means the trigger was written and the process died before
        anything closed it, so the answer is not "unknown" — it is that this
        never finished, and saying so is what lets the agent decide whether to
        backfill it.  Returns how many it moved, so the caller can say.
        """
        moment = now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE scheduled_runs SET status = ?, error = ? WHERE status = ?",
                (
                    FAILED,
                    f"slife2 stopped before this run was settled ({moment})",
                    PENDING,
                ),
            )
        return cursor.rowcount


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=int(row["id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        cron=str(row["cron"]),
        timezone=str(row["timezone"]),
        enabled=bool(row["enabled"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_due=str(row["last_due"]),
    )


def _run(row: sqlite3.Row, name: str = "") -> Run:
    return Run(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        due_at=str(row["due_at"]),
        status=str(row["status"]),
        ran_at=str(row["ran_at"]),
        note=str(row["note"]),
        error=str(row["error"]),
        agent_task_id=str(row["agent_task_id"]),
        # `name` is a join column when the caller asked for one.  Run carries it
        # so that a ledger listing can say which task each row belongs to —
        # `schedule_runs` asks across tasks, and a row without a name is a row
        # nobody can act on.
        name=name or "",
    )


def moment_of(stamp: str) -> datetime:
    """A stored stamp as a moment, so two of them can be compared.

    `slife2.clock` is the only writer and its format is fixed, so this is a
    parse of our own output — the `ValueError` it can raise is a database
    written by something else, which is worth hearing about rather than
    papering over.
    """
    return datetime.fromisoformat(stamp)
