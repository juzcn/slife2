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
attached image is a base64 data URL on that first entry, and it is the one thing
that does *not* reach the database: turning a turn into text is what a model
reads back, so the bytes become a note saying they were there, and the file they
came from is still named in the prompt (`strip_images`).

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
from slife2.timeutil import normalize_bound

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

#: Most turns one page may hold.  A cap rather than a preference: `limit` comes
#: from a model, and a model that asks for ten thousand turns should get a page
#: and a `total`, not a context window full of history.
MAX_PAGE = 200

#: How much of each message a listing shows.  The browse is for *choosing* a turn
#: to read, and a preview long enough to choose from is not the same thing as the
#: turn — which is what `turn_read` is for.  Four hundred characters is roughly a
#: paragraph: enough for a question and the shape of its answer, and twenty of
#: them is a request, not a transcript.
PREVIEW_CHARS = 400


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

    def to_listing(self, chars: int = PREVIEW_CHARS) -> dict[str, Any]:
        """One row of a browse: enough to choose from, and not the turn.

        **The two halves of the exchange, which no column holds.**  v1 keeps the
        user's message in a column of its own and lists the rows straight out of
        SQL; slife2 stores the turn as the list of messages the model saw, so
        what was asked is `messages[0]` and what was answered is the last
        assistant message in it.  Reading them back is this module's job and not
        the tool's, because this is the module that knows what a turn is.

        The cut is **announced** (`…`), for the reason the tool-result digest
        announces itself: a message that reads as short is a message the caller
        has no reason to `turn_read`, so a silent cut turns one wrong answer
        into a wrong answer nobody looks up.
        """
        return {
            "turn_id": self.turn_id,
            "created_at": self.created_at,
            "user_message": _cut(user_message(self.messages), chars),
            "assistant_message": _cut(assistant_message(self.messages), chars),
            "token_count": self.token_count,
        }

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
    -- An attachment arrives on that first entry as a base64 data URL and leaves
    -- as a note in the same place: the bytes are the one thing a turn does not
    -- keep (see `strip_images`).  There is no column for what was attached and
    -- there is not meant to be — the file is named in the text.
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


def strip_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace an attached image with a note saying it was there.

    **The bytes are not kept, and the name is not needed.**  An attachment
    arrives as a base64 data URL — up to ten megabytes of it (`slife2.tui.
    attachments`) — and a turn is text that a model reads back; putting the
    picture in the database would mean every later read of that turn carries
    megabytes to say what one line could.  The file it came from needs no field
    of its own, because `@screenshot.png` is still in the prompt: the marker is
    left in the text where the user put it (that is the whole of v1's
    convention), so what is stored says which file it was, and attaching it
    again is a new turn and a new model call.

    Announced, never silent, for the reason the tool-result digest is: a
    placeholder that reads as the real thing is a lie a model reasons from.

    Returns new dicts; the caller's list is untouched, because the live
    conversation is still using the image — the model that just read it may be
    reasoning about it in this very turn.
    """
    stripped: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            stripped.append(message)
            continue
        parts: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                parts.append({"type": "text", "text": _image_note(part)})
            else:
                parts.append(part)
        stripped.append({**message, "content": parts})
    return stripped


def _image_note(part: dict[str, Any]) -> str:
    """What one attached image leaves behind."""
    url = part.get("image_url")
    url = url.get("url") if isinstance(url, dict) else None
    media_type, _, payload = str(url or "").partition(";base64,")
    media_type = media_type.removeprefix("data:") or "image"
    # Three quarters: base64 carries four characters per three bytes.
    size = f", ~{len(payload) * 3 // 4 // 1024} KB" if payload else ""
    return (
        f"[image not stored: {media_type}{size} — the prompt names the file, "
        f"and attaching it again sends it]"
    )


# --- reading a turn back ------------------------------------------------------


def message_text(content: Any) -> str:
    """The text of one message's content, whatever shape it arrived in.

    Two shapes, because a provider's wire has two: a plain string, and the list
    of parts an attachment forces.  A part that is not text is *named* rather
    than dropped — `[image]` — for the reason `slife2.toolhub.flatten` describes:
    a reader can act on knowing an image was there, and cannot act on silence.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
        else:
            parts.append(f"[{part.get('type') or 'attachment'}]")
    return " ".join(parts)


def _first(messages: list[dict[str, Any]], role: str) -> str:
    for message in messages:
        if message.get("role") == role:
            return message_text(message.get("content"))
    return ""


def _last(messages: list[dict[str, Any]], role: str) -> str:
    """The last message of a role **that has text**.

    Backwards rather than forwards, and skipping empties, because a turn's last
    assistant message is not always its answer: a model that called a tool has
    an assistant message carrying the call and no prose at all, and the loop
    only stops when one of them says something.
    """
    for message in reversed(messages):
        if message.get("role") == role:
            text = message_text(message.get("content"))
            if text:
                return text
    return ""


def user_message(messages: list[dict[str, Any]]) -> str:
    """What was asked.  `messages[0]` in practice, and the first `user` in fact."""
    return _first(messages, "user")


def assistant_message(messages: list[dict[str, Any]]) -> str:
    """What was answered — the turn's last assistant message with prose in it."""
    return _last(messages, "assistant")


def _cut(text: str, chars: int) -> str:
    if len(text) <= chars:
        return text
    return text[:chars] + "…"


def _time_window(since: str | None, until: str | None) -> tuple[str, list[str]]:
    """`(where, params)` for a `created_at` window.

    Written once because there is one time axis and two readers of it: the
    browse below, and whatever a recall selector becomes.  A second spelling of
    `created_at >= ?` is a second place for the column name and the grammar to
    drift apart.

    Raises:
        InvalidTimeBound: If either bound is in no grammar `slife2.timeutil`
            speaks.  Deliberately not an empty result: a bound nobody understood
            and a window with nothing in it are the same answer from SQLite, and
            only one of them is the truth.
    """
    clauses: list[str] = []
    params: list[str] = []
    if since:
        clauses.append("created_at >= ?")
        params.append(normalize_bound(since, role="since"))
    if until:
        clauses.append("created_at <= ?")
        params.append(normalize_bound(until, role="until"))
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params


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

        What a turn keeps is decided here rather than by the caller: this is the
        boundary between the live conversation and the permanent record, and the
        rules about what is worth keeping belong on this side of it.  Two of
        them, and both announce themselves — an oversized tool result becomes a
        head-and-tail digest, and an attached image becomes a note saying it was
        there (`strip_images`).

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
                        json.dumps(
                            compact_tool_results(strip_images(messages)),
                            ensure_ascii=False,
                        )
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

    def turns(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[TurnRecord], int]:
        """Turns within a time window, newest first, as `(records, total)`.

        `total` counts the *window*, not the table, which is what lets a caller
        tell whether there is another page — `offset + len(records) < total` —
        without asking a second question.

        **Ordered by `rowid`, not by `created_at`.** Rowid is the turn id and is
        monotonic with creation, so a page boundary can never fall inside a group
        of turns sharing a timestamp; ordering by the timestamp would let two
        turns written in the same second be skipped or repeated by a page
        boundary that moved between calls. The window itself still filters on
        `created_at`, which is the column with an index on it.

        Raises:
            InvalidTimeBound: If a bound is in no known grammar.
        """
        limit = max(1, min(int(limit), MAX_PAGE))
        offset = max(0, int(offset))
        where, params = _time_window(since, until)
        with self._connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM turn {where}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT rowid AS turn_id, * FROM turn {where} "
                f"ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [_row_to_record(row) for row in rows], int(total)

    def turn(self, turn_id: int) -> TurnRecord | None:
        """One turn by id, or `None` if there is no such row.

        `None` rather than an exception: an id that does not resolve is a value
        the caller has something useful to say about — `turn_read` names the id
        it could not find, which is what a model that miscopied one needs.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT rowid AS turn_id, * FROM turn WHERE rowid = ?",
                (int(turn_id),),
            ).fetchone()
        return _row_to_record(row) if row else None

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
