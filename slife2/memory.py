"""Persisted turns, one SQLite file per agent.

The schema is slife v1's, minus one column.  This module originally had a `turns`
table of its own invention — `(id, agent, created_at, prompt, messages, model,
usage, steps)` — and it was the wrong shape in ways that only show up later: one
column for two different token counts, one column for two different moments, and
no place for the reasoning at all.  v1's design has answers to those, arrived at
by use.  The exception is `user_message`, which v1 keeps beside the assistant's
half and which is not a column here at all — see below.

**One row is one turn**: what the user said, and everything the agent did about
it.  Turns are independent — no session grouping, no lifecycle.  Nothing here
deletes one.

**A turn is one list of messages**, the user's own first, in the shape the model
saw it — OpenAI-shaped JSON in a single column.  There is no separate column for
what the user said: it is `messages[0]`, and the turn therefore reads back on its
own, as a conversation rather than as an answer whose question is missing.  An
attached image is a base64 data URL on that first entry and is stored with the
rest, so the payload is in the database and every `recent` reads it.

**Agents are isolated by file.**  `<agent>.turn.db` — there is no agent column
and no query that can reach another agent's turns, because there is no other
agent's turns in the file.  `who_helped` records the name for display.

**No migration layer.**  This is a decision, not an omission: schema changes
land in `_SCHEMA` for fresh databases, and an old one is deleted and rebuilt
rather than upgraded.  The data is derived from conversations that can happen
again, and a migration path is a permanent cost paid to avoid a one-off
annoyance.  `CREATE TABLE IF NOT EXISTS` covers *additions* — a new table, a new
index — which is most of what tends to arrive.

A *virtual* table is the addition that is not quite free, and it is the one
v1's schema has three of.  An index built later holds only the rows that arrived
after it, so landing one means filling it from `turn` in the same change — and
an external-content FTS index (`content='turn'`) is only told about an `UPDATE`
if a trigger says so, which is what makes `summary` searchable at all.  Both are
one-time costs paid by whoever adds it; neither is a reason to add the columns
now, which is all this module is holding open.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slife2.clock import now
from slife2.paths import turns_dir

logger = logging.getLogger(__name__)

#: Characters an agent name may keep when it becomes a filename.  Everything
#: else is replaced, because the name arrives from `--agent` on a command line
#: and a path is not something a command line should be able to reach.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: How long SQLite waits for a lock before giving up.  The memory server is the
#: only writer, but a read may be in flight while a turn is being written.
_BUSY_TIMEOUT_MS = 5000

#: Database files already reported as belonging to the previous schema.  See
#: `TurnStore._report_legacy_file`, which is why this is not simply computed.
_legacy_reported: set[Path] = set()

#: How much of an oversized tool result is kept, head and tail together.  Tool
#: output is *reproducible* — re-run the tool and the answer comes back — so the
#: permanent record keeps a digest rather than the whole thing, and a single
#: result cannot grow a turn past what is reasonable to store and re-read.
#: Always announced to the model (see the marker), never silently truncated.
TOOL_RESULT_CHARS = 8000


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


#: What separates the two halves of a client id in a filename.  It has to be a
#: character `safe_agent_name` cannot leave behind, or `("a.b", "c")` and
#: `("a", "b.c")` would name the same file — and `@` is not in that set.
_CLIENT_SEPARATOR = "@"


def database_path(agent: str, subagent: str = "") -> Path:
    """The database for one client id.

    **One file per id, not a column.**  Isolation as a property of the filesystem
    beats isolation as a `WHERE` clause somebody can forget to write — the same
    argument DESIGN.md §5 makes for agents, applied one level down.  An agent's
    own conversation keeps the bare name it always had, so the common case does
    not pay for the one that is not.
    """
    safe = safe_agent_name(agent)
    if subagent:
        safe = f"{safe}{_CLIENT_SEPARATOR}{safe_agent_name(subagent)}"
    return turns_dir() / f"{safe}.turn.db"


@dataclass(frozen=True)
class TurnRecord:
    """One turn as it is stored."""

    #: The SQLite rowid.  There is no `id` column: rowid is already monotonic
    #: with creation, and a second one would be a second thing to keep in step.
    turn_id: int
    #: The whole turn, the user's message first.  Nothing here singles that
    #: message out: it is `messages[0]`, and a reader that wants what was asked
    #: reads it there, where the model read it too.
    messages: list[dict[str, Any]] = field(default_factory=list)
    #: A retrieval hook the model may write later.  Nothing writes it yet.
    summary: str = ""
    tags: str = ""
    #: When the user pressed enter, and when the agent finished.  Two columns
    #: because they answer different questions — a turn that took two minutes
    #: is a different thing from one that took two seconds.
    created_at: str = ""
    completed_at: str | None = None
    #: Where it came from, and who answered.
    channel: str = ""
    who_helped: str = ""
    what_model: str = ""
    #: Two counts, because there are two questions.  `token_count` is what the
    #: turn cost; `context_tokens` is how large the conversation had become by
    #: the end of it — the number the *next* request would resend.  A single
    #: field cannot answer both, and the difference matters: one is a bill, the
    #: other is what the context window has to hold.
    token_count: int = 0
    context_tokens: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "messages": self.messages,
            "summary": self.summary,
            "tags": self.tags,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "channel": self.channel,
            "who_helped": self.who_helped,
            "what_model": self.what_model,
            "token_count": self.token_count,
            "context_tokens": self.context_tokens,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS turn (
    -- The whole turn as OpenAI-shaped message JSON, the user's message first.
    -- An attachment rides on that first entry as a base64 data URL, so the
    -- payload lives here with everything else rather than in a column of its
    -- own.
    messages       TEXT    NOT NULL DEFAULT '[]',

    -- Retrieval hooks for later.  Nothing writes them yet; they are here
    -- because adding a column to a table with rows in it is the thing this
    -- schema has no mechanism for.
    summary        TEXT             DEFAULT '',
    tags           TEXT             DEFAULT '',

    created_at     TEXT    NOT NULL,
    completed_at   TEXT,

    channel        TEXT             DEFAULT '',
    who_helped     TEXT             DEFAULT '',
    what_model     TEXT             DEFAULT '',

    token_count    INTEGER NOT NULL DEFAULT 0,
    context_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_turn_created ON turn(created_at);
"""


def compact_tool_results(
    messages: list[dict[str, Any]], budget: int = TOOL_RESULT_CHARS
) -> list[dict[str, Any]]:
    """Replace oversized tool results with a head+tail digest.

    Returns new dicts; the caller's list is untouched, because the live
    conversation must keep the whole result — the model may still be reasoning
    about it in this session.

    Truncation is **announced**, never silent.  A model reading a shortened
    result back later has to know it is shortened, and how to get the rest;
    a digest that looked complete would be a lie it could reason from.

    Idempotent: a result already carrying the marker is left alone, so a turn
    that is re-saved is not compacted twice.
    """
    compacted: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if (
            message.get("role") != "tool"
            or not isinstance(content, str)
            or len(content) <= budget
            or _COMPACTION_MARKER in content
        ):
            compacted.append(message)
            continue

        half = budget // 2
        name = message.get("name") or message.get("tool_call_id") or "the tool"
        marker = (
            f"\n{_COMPACTION_MARKER} original {len(content)} chars — "
            f"full output retrievable by running {name} again]\n"
        )
        compacted.append(
            {**message, "content": content[:half] + marker + content[-half:]}
        )

    return compacted


#: The announcement.  ASCII, and distinctive enough that the idempotence check
#: cannot be fooled by a tool that happened to return the same words.
_COMPACTION_MARKER = "... [compacted at save:"


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
        # Write-ahead logging: a read while a turn is being written sees the
        # last committed state instead of waiting for the writer.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            self._report_legacy_file(connection)

    def _report_legacy_file(self, connection: sqlite3.Connection) -> None:
        """Say so when this file predates the `turn` table.

        There is no migration layer, and the doctrine above says an old database
        is deleted rather than upgraded — but nothing said *which* one, and the
        failure it produces is the confusing kind: the file keeps its `turns`
        table, gains an empty `turn` beside it, and every read thereafter
        honestly reports no history.  Turns that are on disk and invisible are
        worse than turns that are gone, because nothing looks wrong.

        Reported once per process per file.  The store is built on every call,
        so the alternative is the same complaint on every turn, which is the
        kind of log nobody reads.
        """
        if self.path in _legacy_reported:
            return
        legacy = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='turns'"
        ).fetchone()[0]
        if not legacy:
            return
        _legacy_reported.add(self.path)
        logger.error(
            "%s holds the previous schema's `turns` table, which this version "
            "does not read; there is no migration — delete the file and its "
            "turns are recorded afresh",
            self.path,
        )

    def save_turn(
        self,
        *,
        messages: list[dict[str, Any]],
        channel: str = "",
        who_helped: str = "",
        what_model: str = "",
        token_count: int = 0,
        context_tokens: int = 0,
        created_at: str | None = None,
        completed_at: str | None = None,
    ) -> int:
        """Append one turn, returning its rowid.

        The tool results are compacted here rather than by the caller: this is
        the boundary between the live conversation and the permanent record, and
        the rule about what is worth keeping belongs on this side of it.

        The document is passed through `_storable` first, for the reason given
        there: the one thing this method may not do is drop a turn because of
        what was in it.  `ensure_ascii=False` is kept — the alternative escapes
        every non-Latin character, and a Chinese conversation should not pay six
        bytes a character to sit in the database — because `_storable` handles
        the one thing the escaping would have protected against.
        """
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO turn (messages, created_at, completed_at,"
                " channel, who_helped, what_model, token_count, context_tokens)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _storable(
                        json.dumps(compact_tool_results(messages), ensure_ascii=False)
                    ),
                    created_at or now(),
                    completed_at or now(),
                    channel,
                    who_helped,
                    what_model,
                    int(token_count),
                    int(context_tokens),
                ),
            )
            return int(cursor.lastrowid or 0)

    def recent(self, limit: int = 10) -> list[TurnRecord]:
        """The most recent turns, newest first."""
        limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT rowid AS turn_id, * FROM turn ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM turn").fetchone()[0])


def _storable(text: str) -> str:
    """Text SQLite will encode.

    Surrogates cannot be encoded to UTF-8, so binding one raises
    `UnicodeEncodeError` — which is a failed write, and a failed write now fails
    the turn outright rather than losing it quietly (see
    `slife2.mcp_server.open_server`). Neither is something an emoji in somebody's
    answer should be able to bring about, which is why the surrogate is dealt
    with here rather than the failure being handled there.

    Surrogates are reachable without anybody typing one, because a provider's
    token stream is JSON and CPython's `json` does not combine `\\uXXXX` pairs:
    `json.loads('"\\\\ud83d\\\\ude00"')` returns **two** characters, and both are
    surrogates.  That is how an emoji arrives.  So there are two cases and they
    want opposite treatment:

    * a **pair** is a real character — an emoji — split by the escape and wanting
      to be put back together;
    * a **lone** surrogate is not a character at all.  It is an artefact of a
      split mid-character, it cannot be displayed, and U+FFFD is what every
      other decoder in the stack substitutes.

    The UTF-16 round trip does exactly that and nothing else: `surrogatepass`
    gets the surrogates *out* of the string as bytes, and UTF-16's decoder is
    the one Python codec that recombines a valid pair and reports an unpaired
    one as a replacement.  Encoding to UTF-8 with `errors="replace"` is the
    tempting one-liner and is wrong — it turns the emoji into `??` and a lone
    surrogate into a question mark nobody can tell from a typed one.

    Safe on a JSON document as well as on plain text: a surrogate can only come
    out of `json.dumps` from inside a string literal, and U+FFFD is not one of
    JSON's metacharacters.
    """
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _row_to_record(row: sqlite3.Row) -> TurnRecord:
    """Rebuild a record, tolerating a row whose JSON will not parse.

    A corrupt row is one turn, and refusing to read *any* history because one
    row is damaged would turn a small problem into a total one.
    """
    return TurnRecord(
        turn_id=int(row["turn_id"]),
        messages=_loads(row["messages"], []),
        summary=str(row["summary"] or ""),
        tags=str(row["tags"] or ""),
        created_at=str(row["created_at"]),
        completed_at=row["completed_at"],
        channel=str(row["channel"] or ""),
        who_helped=str(row["who_helped"] or ""),
        what_model=str(row["what_model"] or ""),
        token_count=int(row["token_count"]),
        context_tokens=int(row["context_tokens"]),
    )


def _loads(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("a stored value did not parse; reading it as empty")
        return default


def store_for(agent: str, subagent: str = "") -> TurnStore:
    """The store for one client id, creating its file if needed."""
    return TurnStore(database_path(agent, subagent))
