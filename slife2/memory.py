"""Persisted turns, one SQLite file per agent.

Three decisions shape this module:

**A file per agent.**  `<agent>.turn.db` names the agent it belongs to, so
isolation between agents is a property of the filesystem rather than a `WHERE`
clause somebody can forget to write.  The agent name is in a column too, making
each file self-describing — a copy still knows whose it is.

**Messages are stored as they arrived.**  No summary, no extraction, no
interpretation: the JSON that `run_turn` handed back.  Everything this schema
cannot answer today is a question it can be asked later without a migration,
because nothing was thrown away to make it fit.

**It is data, not cache.**  The runtime directory holds a daemon's bookkeeping
and may be cleared; this lives under the platform's data directory, because
losing it means losing the thing the component exists to keep.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from slife2.paths import turns_dir

logger = logging.getLogger(__name__)

#: Characters an agent name may keep when it becomes a filename.  Everything
#: else is replaced, because the name arrives from `--agent` on a command line
#: and a path is not something a command line should be able to reach.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: How long SQLite waits for a lock before giving up.  The memory server is the
#: only writer, but `recent` may be reading while a turn is being written.
_BUSY_TIMEOUT_MS = 5000


def safe_agent_name(name: str) -> str:
    """An agent name fit to be a filename.

    `--agent` comes from a command line, and `jack` becoming `jack.turn.db` is
    exactly the kind of thing a `..` in the wrong place turns into a write
    somewhere nobody intended.  Anything outside a conservative set is replaced
    rather than escaped, and a name that reduces to nothing — or to `.` or `..`
    — is refused, because silently writing to a surprising path is worse than
    saying no.

    Raises:
        ValueError: If the name has no characters that can be kept.
    """
    cleaned = _SAFE_NAME.sub("_", name).strip("._-")
    if not cleaned:
        raise ValueError(f"agent name {name!r} cannot name a database file")
    return cleaned[:64]


def database_path(agent: str) -> Path:
    """The database for one agent."""
    return turns_dir() / f"{safe_agent_name(agent)}.turn.db"


@dataclass(frozen=True)
class TurnRecord:
    """One turn as it is stored."""

    agent: str
    created_at: str
    prompt: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    steps: int = 0
    id: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "created_at": self.created_at,
            "prompt": self.prompt,
            "messages": self.messages,
            "model": self.model,
            "usage": self.usage,
            "steps": self.steps,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    prompt     TEXT NOT NULL,
    messages   TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    usage      TEXT NOT NULL DEFAULT '{}',
    steps      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS turns_by_time ON turns(created_at DESC);
"""


class TurnStore:
    """Reads and writes one agent's turns.

    A connection per call rather than one held open.  SQLite connections are
    cheap and are not shareable across threads, and the caller — an MCP tool —
    runs on an event loop where blocking it even briefly is worth avoiding; a
    fresh connection per call means the store needs no locking of its own.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        # Write-ahead logging: a `recent` while a turn is being written reads
        # the last committed state instead of waiting for the writer.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def remember(
        self,
        *,
        prompt: str,
        messages: list[dict[str, Any]],
        model: str = "",
        usage: dict[str, Any] | None = None,
        steps: int = 0,
    ) -> int:
        """Append one turn, returning its id."""
        agent = self.path.stem.removesuffix(".turn")
        created = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO turns (agent, created_at, prompt, messages, model, usage, steps)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    agent,
                    created,
                    prompt,
                    json.dumps(messages, ensure_ascii=False),
                    model,
                    json.dumps(usage or {}, ensure_ascii=False),
                    int(steps),
                ),
            )
            return int(cursor.lastrowid or 0)

    def recent(self, limit: int = 10) -> list[TurnRecord]:
        """The most recent turns, newest first."""
        limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM turns ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0])


def _row_to_record(row: sqlite3.Row) -> TurnRecord:
    """Rebuild a record, tolerating a row whose JSON will not parse.

    A corrupt row is one turn, and refusing to read *any* history because one
    row is damaged would turn a small problem into a total one.
    """
    return TurnRecord(
        id=int(row["id"]),
        agent=str(row["agent"]),
        created_at=str(row["created_at"]),
        prompt=str(row["prompt"]),
        messages=_loads(row["messages"], []),
        model=str(row["model"]),
        usage=_loads(row["usage"], {}),
        steps=int(row["steps"]),
    )


def _loads(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("a stored value did not parse; reading it as empty")
        return default


def store_for(agent: str) -> TurnStore:
    """The store for one agent, creating its file if needed."""
    return TurnStore(database_path(agent))
