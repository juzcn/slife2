"""The db's storage — a conversation's turns.

**The turns** are one SQLite file per agent: a conversation's history, which
cannot be derived from anything and would be a loss.  The tool catalogue was the
second thing this module kept, and it is `slife2.libs.store.catalogue` now: the two have no
name in common — one is a history nothing else can reconstruct, the other a set
of rows the servers can be asked again — and a module that holds both makes
every question about one of them a question about a file that is mostly about
the other.

The two indexes over these turns are `slife2.libs.store.index`'s, which is the same
machinery the catalogue and the cabinet are searched by.

The schema separates the facts the first `turns` table conflated.  This module
originally had a `turns` table of its own invention — `(id, agent, created_at,
prompt, messages, model, usage, steps)` — and it was the wrong shape in ways that
only show up later: one column for two different token counts, one column for two
different moments, and no place for the reasoning at all.  The columns here
answer those, arrived at by use.  There is no `user_message` column beside the
assistant's half — see below.

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

**Agents are isolated by file.**  `agents/<agent>/turns.db` — there is no agent
column and no query that can reach another agent's turns, because there is no
other agent's turns in the file.  `who_helped` records the name for display.
**A subagent's is one directory further in** (`agents/<agent>/subagents/<subagent>/
turns.db`), which is the same isolation rather than a weaker one: what a
subagent is, is an agent that belongs to whoever made it, so its conversation is
its own file inside its own directory — the directory that removing it takes
away.

**No migration layer.**  This is a decision, not an omission: schema changes
land in `_SCHEMA` for fresh databases, and an old one is deleted and rebuilt
rather than upgraded.  The data is derived from conversations that can happen
again, and a migration path is a permanent cost paid to avoid a one-off
annoyance.  `CREATE TABLE IF NOT EXISTS` covers *additions* — a new table, a new
index — which is most of what tends to arrive.

A file that is missing one of the columns this build declares is **refused at
open** (`StaleSchema`), naming the column and the file.  Reported and served
anyway — which is what this did — the failure lands later and per row as
SQLite's `No item with that key`: a message that names neither.

**The search indexes are derived, not migrated.**  Finding a turn by what it was
about needs a keyword index over its text and a vector index over what it was
about, and both are built from rows that are already here — so neither is a
thing this schema has to carry forward.  They are three tables added beside
`turn` — `turn_fts`, `turn_vec`, `index_meta` — and not one column of `turn`
changed, which is the addition `CREATE TABLE IF NOT EXISTS` already covers.
`index_meta` records the identity each index was built with: the rules the text
was normalized by (`slife2.libs.store.textindex`), and the embedding provider, model,
endpoint and width.  An index whose identity no longer matches the
configuration is dropped and rebuilt rather than read, which is what makes a
changed embedding model a rebuild instead of a migration — and it is one
mechanism for all four things that can invalidate one (`slife2.libs.store.index`).

**A turn is written with its vector, in one transaction.**  `save_turn` is
async because the embedding is a call to a model, and its three writes are
atomic because the alternative is a turn that is half indexed — and because a
save that raises has to be a save that stored nothing.  That is the one property
that makes the failure honest, and it is why the embedding model is a hard
dependency of this plugin rather than something that can be switched off: an
endpoint that cannot be reached fails the save, where an endpoint that silently
degraded would store turns nothing could ever find.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlite_vec

from slife2.helpers.clock import now
from slife2.helpers.threads import off_loop
from slife2.libs.host.paths import TURNS_DB, work_dir
from slife2.libs.store import textindex
from slife2.libs.store.index import (
    _OVERFETCH,
    MAX_SQL_VARS,
    SYNC_BATCH,
    Embedder,
    _marks,
    _rowid,
    as_similarity,
    checked_vectors,
    connect_to,
    embed_chunks,
    embed_rows,
    filter_clause,
    fuse_ranked,
    index_meta,
    page_limit,
    place_vectors,
    rebuild_vector_index,
    rows_in_order,
    set_index_meta,
    storable,
    time_window,
    vector_identity,
    where_clause,
)
from slife2.libs.turn import attachments
from slife2.libs.turn.context import normalise_ids
from slife2.libs.turn.messages import Usage
from slife2.libs.turn.tokens import estimate_turn_tokens

logger = logging.getLogger(__name__)

#: Characters an agent name may keep when it becomes a filename.  Everything
#: else is replaced, because the name arrives from `--agent` on a command line
#: and a path is not something a command line should be able to reach.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: Database files whose old schema has already been named — a missing column
#: (`_refuse_old_schema`) or the previous `turns` table (`_report_legacy_file`).
#: The report is once per process per file, which is why this is not simply
#: computed.
_legacy_reported: set[Path] = set()

#: How much of each message a listing shows.  The browse is for *choosing* a turn
#: to read, and a preview long enough to choose from is not the same thing as the
#: turn — which is what `turn_read` is for.  Four hundred characters is roughly a
#: paragraph: enough for a question and the shape of its answer, and twenty of
#: them is a request, not a transcript.
PREVIEW_CHARS = 400


#: How long an agent name may be as a path component.  Most filesystems stop at
#: 255, so this is a house limit rather than one anybody hits by accident.
MAX_AGENT_NAME = 64

#: How much of the digest survives truncation, plus the `-` in front of it.
_DIGEST_CHARS = 9


def safe_agent_name(name: str) -> str:
    """An agent name fit to be a filename.  **Injective, because it is a path.**

    `--agent` comes from a command line, and `jack` becoming `jack.turn.db` is
    exactly the kind of thing a `..` in the wrong place turns into a write
    somewhere nobody intended.  Anything outside a conservative set is replaced
    rather than escaped, and a name that reduces to nothing — or to `.` or `..`
    — is refused, because silently writing to a surprising path is worse than
    saying no.

    **A name that is too long keeps a digest.**  Truncating alone would be a
    many-to-one function, and this one names a *directory*: two long names
    sharing their first 64 characters would be two agents with one `turns.db`
    between them, which is the isolation `database_path` exists to provide.  The
    digest is taken of the cleaned name, so two raw spellings that already
    collide here still collide — this narrows the collisions it can fix to the
    one it introduces.

    Raises:
        ValueError: If the name has no characters that can be kept.
    """
    cleaned = _SAFE_NAME.sub("_", name).strip("._-")
    if not cleaned:
        raise ValueError(f"agent name {name!r} cannot name a database file")
    if len(cleaned) <= MAX_AGENT_NAME:
        return cleaned
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    keep = MAX_AGENT_NAME - _DIGEST_CHARS - 1
    return f"{cleaned[:keep]}-{digest}"


def database_path(agent: str, subagent: str = "") -> Path:
    """The database for one client id.

    **One file per id, not a column.**  Isolation as a property of the filesystem
    beats isolation as a `WHERE` clause somebody can forget to write — the same
    argument SDD §8.1 makes for agents, applied one level down.

    **A non-fork subagent's store is its own directory**,
    `agents/<agent>/subagents/<subagent>/`, because a subagent is an agent that
    belongs to whoever made it rather than a means to an end inside one of that
    agent's turns. Its conversation, its `subagent.yaml` record, its cabinet and
    its media are its own.

    **A fork has no store of its own.** It is a temporary copy of its maker's
    conversation, held in memory and never written down, so its context reads
    the maker's turn log and its cabinet and media are the maker's. Reads go
    there; writes never do. `work_dir` is what resolves the two cases; who lays
    the directory out is
    `slife2.libs.host.paths.agent_dir`, and this function is one line because of
    it: the layout belongs in one place, and the file name in another.
    """
    return work_dir(agent, subagent) / TURNS_DB


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
    #: The bill's breakdown — prompt, completion, cache read and cache
    #: creation — as the loop measured it.  `token_count` is that bill's total,
    #: which is what a reader asks for first; the breakdown is what a cache-hit
    #: ratio is computed from, and no sum can reconstruct it.  None for a row
    #: written before this was recorded: the totals it has are kept, and the
    #: absence says the split was never measured rather than that it was zero.
    usage: Usage | None = None

    def to_listing(self, chars: int = PREVIEW_CHARS) -> dict[str, Any]:
        """One row of a browse: enough to choose from, and not the turn.

        **The two halves of the exchange, which no column holds.**  slife2 stores
        the turn as the list of messages the model saw, so what was asked is
        `messages[0]` and what was answered is the last assistant message in it.
        Reading them back is this module's job and not the tool's, because this
        is the module that knows what a turn is.

        The cut is **announced** (`…`), for the reason the tool-result digest
        announces itself: a message that reads as short is a message the caller
        has no reason to `turn_read`, so a silent cut turns one wrong answer
        into a wrong answer nobody looks up.

        **`token_count` is the turn's own size, not the bill `self.token_count`
        records.**  The stored count sums every model call the turn made, and
        each call carried the whole context of its moment; it answers "what did
        this turn cost", not "how much room does this turn take".  The estimate
        from its messages is the second question — the one a caller choosing a
        turn can act on.
        """
        return {
            "turn_id": self.turn_id,
            "created_at": self.created_at,
            "user_message": _cut(user_message(self.messages), chars),
            "assistant_message": _cut(assistant_message(self.messages), chars),
            "token_count": estimate_turn_tokens(self.messages),
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
            # Null rather than a zeroed breakdown: a reader can tell a turn
            # nobody measured from one that measured nothing.
            "usage": self.usage.to_wire() if self.usage is not None else None,
        }


@dataclass(frozen=True)
class _Stored:
    """One turn, ready to be written — everything but its vectors.

    Assembled before the transaction and before the embed, so that the two
    halves of a save can run in different places: the embedding on the event
    loop, where a network call belongs, and the inserts on a thread, where
    blocking belongs.  A plain bundle rather than fifteen arguments across that
    boundary.
    """

    document: str
    search: str
    summary: str
    tags: str
    created_at: str
    completed_at: str
    channel: str
    who_helped: str
    what_model: str
    token_count: int
    context_tokens: int
    #: The usage breakdown as JSON, or None for a writer that did not measure
    #: one — the totals above are then all the row can say.
    usage: str | None


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS turn (
    -- The whole turn as OpenAI-shaped message JSON, the user's message first.
    -- An attachment arrives on that first entry as a base64 data URL and leaves
    -- as an `[Image:…]` reference to the cabinet copy the agent server wrote
    -- before the save; a part that reached here without being kept leaves as a
    -- note instead (see `strip_images`).  There is no column for what was
    -- attached and there is not meant to be — the reference or the words that
    -- named the source are in the text.
    messages       TEXT    NOT NULL DEFAULT '[]',

    -- Retrieval hooks.  `turn_summarize` writes them for a turn the model
    -- annotated, and they join the text the keyword index holds; the vector
    -- index deliberately keeps only the conversation.
    summary        TEXT             DEFAULT '',
    tags           TEXT             DEFAULT '',

    created_at     TEXT    NOT NULL,
    completed_at   TEXT,

    channel        TEXT             DEFAULT '',
    who_helped     TEXT             DEFAULT '',
    what_model     TEXT             DEFAULT '',

    token_count    INTEGER NOT NULL DEFAULT 0,
    context_tokens INTEGER NOT NULL DEFAULT 0,

    -- The bill's breakdown, as `Usage.to_wire()` wrote it.  `token_count` is
    -- this bill's total and `context_tokens` the last call's; the split is
    -- here because a cache-hit ratio cannot be reconstructed from sums.
    -- NULL for a row written before it was recorded — the totals it has are
    -- kept, and NULL says the split was never measured.
    usage          TEXT
);

CREATE INDEX IF NOT EXISTS idx_turn_created ON turn(created_at);

-- The keyword index: one row per turn, holding the derived text it is found by
-- and carrying the turn's own rowid so a hit *is* a turn id.
--
-- It stores that text rather than reading it back out of `turn`
-- (`content='turn'`), because the text here is not the text there: it carries a
-- space between every pair of CJK characters, so there is no column of `turn`
-- an external-content index could point at.  One copy of a normalized string
-- per turn is what that costs, and what it buys is an index that can be
-- UPDATEd when a summary is written later — which a contentless index cannot,
-- having nothing to delete.
CREATE VIRTUAL TABLE IF NOT EXISTS turn_fts USING fts5(
    search,
    {textindex.FTS_TOKENIZE}
);

-- Which chunks belong to which turn.  The vector table's rowid is a *chunk* and
-- not a turn — a turn has as many rows there as it has chunks — so this is what
-- turns a hit back into a turn, and what a rebuild deletes by.
CREATE TABLE IF NOT EXISTS turn_chunk (
    id          INTEGER PRIMARY KEY,
    turn_id     INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turn_chunk_turn ON turn_chunk(turn_id);

-- What each derived index was built with.  An index is only readable by the
-- rules and the model that produced it, so its identity is kept beside it and
-- compared before use: a mismatch is a rebuild, never a migration.
CREATE TABLE IF NOT EXISTS index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The **live context**: which of this file's turns the conversation is
-- currently made of, in order.  It is a selection over the turn log, not a
-- property of any turn, which is why it is a second table and not a column.
--
-- **`id` is pinned to 1, and that is the schema saying what a file is.**  There
-- is one conversation per file (the client id *is* the filename), so there is
-- exactly one live context to record; a plain `id INTEGER PRIMARY KEY` would
-- permit a second row, and a second row would be a conversation this file is
-- not.  The `CHECK` makes that a constraint rather than a convention.
--
-- **The order is the whole of the content.**  Reads replay it as written and
-- never re-sort: the list already encodes what was kept, what was trimmed away
-- and what was recalled, and re-sorting it by rowid would silently undo all
-- three — a rebuild's selection is not always contiguous.
--
-- An empty file, or one that predates this table, has no row: `turn_ids` reads
-- as `[]`, which is the honest answer for a conversation whose context has
-- never been recorded.
CREATE TABLE IF NOT EXISTS context (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    turn_ids TEXT    NOT NULL DEFAULT '[]'
);
"""


#: The columns this build's `turn` table declares, and so the ones any file it
#: serves must already have.  `CREATE TABLE IF NOT EXISTS` cannot add a column
#: to a table that exists, so a file from before one of these can neither be
#: read to its end nor written at all.
#:
#: Spelled out rather than parsed from `_SCHEMA`, for the reason
#: `slife2.libs.store.catalogue._TOOL_COLUMNS` gives; the two are kept in step
#: by `test_the_table_is_the_agreed_schema`, which asserts this set and a fresh
#: file's `PRAGMA` agree.
_TURN_COLUMNS = frozenset(
    {
        "messages",
        "summary",
        "tags",
        "created_at",
        "completed_at",
        "channel",
        "who_helped",
        "what_model",
        "token_count",
        "context_tokens",
        "usage",
    }
)


class StaleSchema(RuntimeError):
    """A file whose `turn` table is missing a column this build declares.

    **A refusal, not a report.**  There is no migration layer (SDD §8.1), so a
    file this build cannot write is a file it must not serve.  Served anyway, it
    fails per row at the first lookup of the absent column, as SQLite's `No item
    with that key` — an `IndexError` from a row, naming neither the file nor the
    remedy.  The caller gets the sentence instead, and the operator deletes the
    file, whose turns are then recorded afresh.
    """


def strip_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace an image part that reached the store with a note saying it was there.

    **The fallback, not the usual path.**  The agent server keeps every image a
    recorded turn carried in the cabinet and replaces the part in the row with
    an `[Image:…]` reference before calling here (`record`'s `keep_images`), so a
    kept picture never reaches this function.  What does is a part from a caller
    that did not keep it — a failed turn whose provider refused it (the repair
    replaced it in place first), or a direct caller of this store.

    **The bytes are not kept, and the name is not needed.**  An attachment
    arrives as a base64 data URL — up to ten megabytes of it (`slife2.tui.
    attachments`) — and a turn is text that a model reads back; putting the
    picture in the database would mean every later read of that turn carries
    megabytes to say what one line could.  The source needs no field of its own:
    the person's own words are stored with the turn, and when those words name a
    path or a URL, the model can attach it again in a new call.

    Announced, never silent, for the reason the tool-result digest is: a
    placeholder that reads as the real thing is a lie a model reasons from.

    Returns new dicts; the caller's list is untouched, because the live
    conversation is still using the image — the model that just read it may be
    reasoning about it in this very turn.  That is the one difference between
    this and `slife2.libs.turn.attachments.drop`, which answers the same question in place
    for a turn that has already ended badly; the note they leave is one function,
    so the row and the live list cannot come to describe one attachment two ways.
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
                parts.append({"type": "text", "text": attachments.image_note(part)})
            else:
                parts.append(part)
        stripped.append({**message, "content": parts})
    return stripped


# --- reading a turn back ------------------------------------------------------


def message_text(content: Any) -> str:
    """The text of one message's content, whatever shape it arrived in.

    Two shapes, because a provider's wire has two: a plain string, and the list
    of parts an attachment forces.  A part that is not text is *named* rather
    than dropped — `[image]` — for the reason `slife2.clients.gateway.flatten` describes:
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
    """Truncate for a listing, saying so with `…`.

    **The same two lines as `slife2.libs.turn.events.preview`, and deliberately
    not the same ellipsis**, which is the one thing a reader would otherwise have
    to guess at: that one writes `"..."` because its string reaches a Windows
    console through the server's *log* and U+2026 is not in every codepage, while
    this one writes a listing that a model reads back and a person reads on a
    terminal that has been drawing box characters since it opened. Both announce
    the cut, which is the property that matters — a message that reads as short
    is one the caller has no reason to `turn_read`.
    """
    if len(text) <= chars:
        return text
    return text[:chars] + "…"


# --- what a turn is found by, and what it is about ----------------------------
#
# Two different strings, and the difference is deliberate.  A turn is *found* by
# everything a person would look for it with, and it is *about* what the turn
# itself said — the person's question, the model's reasoning, and the answer it
# settled on.  Evidence is what the turn was made from, not what it was about,
# but it is still what a person *finds* it by when they remember a path, an
# error string or a command: so the keyword index keeps the whole of it (tool
# calls, results, summaries and tags included) while the vector index keeps the
# turn's own voice.  The two legs are complements — exact things on one side,
# what the turn was about on the other.


#: The keyword text's own contract, separate from the tokenizer rules.
#:
#: `textindex.RULES_VERSION` says what a term is; it does not say which fields
#: `searchable_text` composes into a row.  A row built from the old fields
#: cannot be found by a term only the new ones hold, so the recorded
#: `text_version` carries this constant too and a startup rebuilds the rows
#: instead of leaving half the log unsearchable.  Bump it with any change to
#: what `searchable_text` returns.
#:
#: `1` was the composition that recorded no such version (the two halves, the
#: summary and the tags); `2` adds the tool calls and their results.
SEARCH_TEXT_VERSION = "2"


#: The vector text's own contract, separate from the keyword rules.
#:
#: `textindex.RULES_VERSION` governs `normalize` and `terms`, the keyword
#: grammar, and says nothing about which messages `embed_text` chooses.  A
#: vector is a function of the text it was made from, so a change here has to
#: invalidate every stored vector the way a changed model does;
#: `_vector_identity` folds this constant in for exactly that reason.  Bump it
#: with any change to what `embed_text` returns.
#:
#: `1` was the composition that recorded no such version (user, tool calls with
#: their argument heads, answer); `2` is the turn's own voice.
EMBED_TEXT_VERSION = "2"


def _tool_text(messages: list[dict[str, Any]]) -> list[str]:
    """What each tool call asked and what each result answered, in turn order.

    **Whole, not a head.**  A path, a hash or an error string can sit anywhere in
    an argument or a result, and this is the leg that is supposed to find it; a
    cap picked here would be a term nobody can search for.  `message_text` reads
    a result, so a non-text part is *named* there rather than dropped.
    """
    found: list[str] = []
    for message in messages:
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                function = function if isinstance(function, dict) else {}
                line = (
                    f"{function.get('name') or ''} {function.get('arguments') or ''}"
                ).strip()
                if line:
                    found.append(line)
        if message.get("role") == "tool":
            answered = message_text(message.get("content"))
            if answered:
                found.append(answered)
    return found


def searchable_text(
    messages: list[dict[str, Any]], summary: str = "", tags: str = ""
) -> str:
    """The text a turn is *found* by — everything worth looking for it with.

    Both halves of the exchange, every tool call and every tool result, plus the
    two retrieval hooks `turn_summarize` writes: a term that survives only in a
    summary is the keyword leg's to find, and that is the whole reason `summary`
    is in this string.

    **The tools are here because this is the exact leg.**  A path, an error
    string, a command, an identifier or a hash is named literally by the query
    that wants it, and an embedding of a machine output is a poor handle on any
    of them; the vector keeps the turn's own voice instead (`embed_text`).  The
    arguments and results are whole rather than a head, which is why this string
    can be several times the size of the vector's.

    Normalized here rather than at the index, because a query has to be
    normalized by the same rule (`slife2.libs.store.textindex`) and this is the one place
    that rule is applied to stored text.

    Through `storable` like the turn itself, and for the same reason: this
    string is bound to SQLite too, so a lone surrogate arriving in an answer
    would fail the insert and take the turn with it.
    """
    parts = [
        user_message(messages),
        assistant_message(messages),
        *_tool_text(messages),
        summary,
        tags,
    ]
    return storable(textindex.normalize("\n".join(part for part in parts if part)))


def _thinking_text(messages: list[dict[str, Any]]) -> list[str]:
    """What the model reasoned, in the order it reasoned it.

    **`thinking` is part of the message and not of its `content`.**  An assistant
    message that calls a tool usually has no prose at all, so the reasoning is
    the only place it says what it is doing; `message_text` deliberately does
    not read the field (a listing is what was asked and answered), which is why
    the vector builder reads it here.
    """
    found: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        reasoning = message.get("thinking")
        if isinstance(reasoning, str) and reasoning.strip():
            found.append(reasoning)
    return found


def embed_text(messages: list[dict[str, Any]]) -> str:
    """The text a turn is *about*, which is not the text it is found by.

    **The turn's own voice: what was asked, what the model reasoned, and what it
    finally answered.**  The reasoning is included because it is the model's own
    statement of what the turn is doing and it is stored with the message
    (`slife2.libs.turn.messages`); measured on this repository's live log it was
    83.6k characters against a 73.9k semantic text and visible to neither leg.
    The answer is the turn's own synthesis, written with the whole turn in view.

    **Tool results and tool-call arguments are out, and that is measured.**  With
    30 discriminator-style subject queries over the same log, dropping the
    per-call argument head raised MRR from 0.83 to 0.88, and adding the reasoning
    raised it to 0.92 (hit@1 0.77 to 0.90); keeping the full arguments as well
    cost 2.7x the chunks and recovered one query.  The reason results were out
    before still holds — they were 56–99% of a turn's text — but with chunking it
    is a statement about *candidates*: evidence chunks crowd the `k` a turn-level
    answer is chosen from.  A result is still in the turn, which is what the
    keyword leg and `turn_read` read.

    Summaries and tags are out for a second reason: they are written *after* the
    turn, and a vector index that depended on them would have to be rebuilt
    every time one was added.

    Through `storable` for the reason `searchable_text` gives: this string goes
    out as JSON on its way to a model, and a lone surrogate cannot be encoded.
    """
    parts = [
        user_message(messages),
        *_thinking_text(messages),
        assistant_message(messages),
    ]
    return storable("\n\n".join(part for part in parts if part))


def _vector_identity(embedder: Embedder) -> str:
    """The identity the turn vector index is built with.

    The shared identity names the model, the endpoint and the width; this corpus
    adds its text contract, because changing what `embed_text` returns changes
    every vector in the table and takes the same rebuild.
    """
    return f"{EMBED_TEXT_VERSION}|{vector_identity(embedder)}"


def _search_text_version() -> str:
    """The stamp the keyword index records: the fields and the tokenizer.

    Both halves matter.  A row built from other fields cannot be found by a term
    only the new ones hold, and a row tokenized by other rules cannot be queried
    by this one (`slife2.libs.store.textindex` argues the second); one stamp answers
    both, which is why it is composed here rather than written out at the two
    call sites.
    """
    return f"{SEARCH_TEXT_VERSION}|{textindex.RULES_VERSION}"


def _exclusion_clause(excluded: set[int]) -> tuple[list[str], list[int]]:
    """A `NOT IN` clause for turns already in hand, or nothing when it will not bind.

    A `NOT IN` cannot be split across statements the way an `IN` filter can, and
    `MAX_SQL_VARS` is the house bound under SQLite's own.  A kept list larger
    than one statement may bind is therefore left to the Python filters in
    `_time_ranked` and `recall`, which hold the same set and remove it exactly;
    the clause only keeps the candidate legs from spending their depth on turns
    already in hand.  It is never what makes the answer correct.
    """
    if not excluded or len(excluded) > MAX_SQL_VARS:
        return [], []
    values = sorted(excluded)
    return [f"t.rowid NOT IN ({_marks(values)})"], values


def _fuse_questions(
    keyword: Sequence[int], questions: Sequence[Sequence[int]]
) -> list[int]:
    """Fuse the words into each question, then keep each turn's best question.

    **The keyword leg is evidence *about a question*.**  The catalogue already
    fuses it that way; the turn recall used to fuse one keyword list against one
    merged meaning list, which credited a turn by its best similarity across
    questions rather than by how well it answers the question the words belong
    to.  A turn is kept once, at the score of its best question, so another
    question can only add a turn or raise one — the same rule the measured
    similarity follows.

    Ties fall to the newer turn, the order `fuse_ranked` uses.  The words are one
    list and the same for every question; an empty keyword leg is a question
    with one leg, not an error.
    """
    best: dict[int, float] = {}
    for question in questions:
        for turn_id, score in fuse_ranked(
            {"keyword": list(keyword), "sentence": list(question)}
        ):
            if score > best.get(turn_id, 0.0):
                best[turn_id] = score
    return [
        turn_id
        for turn_id, _ in sorted(best.items(), key=lambda pair: (-pair[1], -pair[0]))
    ]


class TurnStore:
    """Reads and writes one agent's turns, and the two indexes over them.

    A connection per call rather than one held open, **and closed when the call
    ends** — see `_connect`, which is the one place that is decided.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._ensure_schema()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One connection for this call, and **it is closed on the way out**.

        `with sqlite3.connect(...)` is a transaction, not a lifetime: it commits
        and leaves the connection *open*.  What closed these was the garbage
        collector — and a connection that has survived one young collection waits
        for a full one, which in a daemon serving turns can be minutes away.
        Nothing sees that until something wants the file: on Windows an open
        handle is a file nobody else can delete, and `subagent_remove` met
        exactly that — `[WinError 32] … tester/turns.db`, with a non-fork
        subagent's own last turn long since answered and its directory
        undeletable.
        (`slife2.plugins.agent.server.subagent_remove`.)

        So the lifetime is stated where the connection is made.  Every call site
        keeps the `with` it already had and gets the close as well as the commit;
        the commit is what `with connection:` here is for, and the `finally` is
        the half that was missing.
        """
        connection = connect_to(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            self._refuse_old_schema(connection)
            connection.executescript(_SCHEMA)
            self._report_legacy_file(connection)

    def _refuse_old_schema(self, connection: sqlite3.Connection) -> None:
        """Refuse a file whose `turn` table is older than this build's columns.

        **Checked before the DDL, so a file this build will not serve is not
        touched.**  `executescript` would otherwise write every table the file
        is missing into a file that is about to be refused; the check costs one
        `PRAGMA` and leaves the file as it was found.

        **Every column this build declares, not the one that arrived last.**
        `usage` is what made the gap reachable, but the rule is the table's:
        `CREATE TABLE IF NOT EXISTS` cannot add a column to a table that already
        exists, so the next addition is the same case, and a file missing any of
        them can neither be read to its end nor written at all.

        **Reported once per process per file, and raised every time.**  The
        report is the operator's — the store is built on every call, so a
        complaint per turn would be a log nobody reads — while the raise is the
        caller's.  Reporting and then serving is what produced the failure this
        exists to prevent: `_row_to_record` reading `row["usage"]` raised
        SQLite's `No item with that key` for every turn in the file, naming
        neither the file nor the remedy.
        """
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(turn)")
        }
        if not columns:  # no `turn` table yet: the schema creates it complete
            return
        missing = sorted(_TURN_COLUMNS - columns)
        if not missing:
            return
        message = (
            f"{self.path} predates this build's `turn` columns (no "
            f"{', '.join(missing)}); there is no migration — delete the file and "
            f"its turns are recorded afresh"
        )
        if self.path not in _legacy_reported:
            _legacy_reported.add(self.path)
            logger.error("%s", message)
        raise StaleSchema(message)

    def _report_legacy_file(self, connection: sqlite3.Connection) -> None:
        """Say so when this file keeps the previous schema's `turns` table.

        A file from before the `turn` table keeps its `turns` table and gains an
        empty `turn` beside it, so every read thereafter honestly reports no
        history: turns on disk and invisible, which is worse than turns that are
        gone because nothing looks wrong.

        This one is **reported rather than refused**, which is the one way it
        differs from a file missing a column (`_refuse_old_schema`): the file
        answers every call it is given — with a history of zero — so it is
        usable, just not the history the person remembers.  A missing column is
        not usable at all.

        Reported once per process per file.  The store is built on every call,
        so the alternative is the same complaint on every turn, which is the
        kind of log nobody reads.
        """
        if self.path in _legacy_reported:
            return
        _legacy_reported.add(self.path)
        legacy = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='turns'"
        ).fetchone()[0]
        if legacy:
            logger.error(
                "%s holds the previous schema's `turns` table, which this version "
                "does not read; there is no migration — delete the file and its "
                "turns are recorded afresh",
                self.path,
            )

    async def save_turn(
        self,
        *,
        messages: list[dict[str, Any]],
        embedder: Embedder,
        channel: str = "",
        summary: str = "",
        tags: str = "",
        who_helped: str = "",
        what_model: str = "",
        token_count: int = 0,
        context_tokens: int = 0,
        usage: Usage | None = None,
        created_at: str | None = None,
        completed_at: str | None = None,
    ) -> int:
        """Append one turn with both of its indexes, in one transaction.

        What a turn keeps is decided here rather than by the caller: this is the
        boundary between the live conversation and the permanent record, and the
        rules about what is worth keeping belong on this side of it.  **There is
        one such rule, and it announces itself** — an attached image becomes a
        note saying it was there (`strip_images`).

        `summary` and `tags` are the annotation a `turn_summarize` call made in
        flight; they are written with the row and join the text the keyword
        index holds.  The vector is the conversation's alone, so they are not
        embedded.

        **There used to be a second one, and its absence is a decision.**  An
        oversized tool result was kept as a head-and-tail digest, on the argument
        that tool output is reproducible — re-run the tool and the answer comes
        back — and that argument was wrong twice over: nothing checked it (a
        `git status` after a commit, a `ps` a second later and a search over a
        moving corpus all answer differently the second time), and deciding
        *which* part of an output to keep is the tool designer's judgement rather
        than the store's.  The record now keeps the whole of what happened, and
        what a model is *shown* is bounded where text enters a context instead —
        the harness's window over a result, and the cabinet's `dump_read` over
        what it was windowed out of.  That bound is a number about tokens rather
        than a guess about content, and it is one a model can read past.

        The document is passed through `storable` first, for the reason given
        there: the one thing this method may not do is drop a turn because of
        what was in it.  `ensure_ascii=False` is kept — the alternative escapes
        every non-Latin character, and a Chinese conversation should not pay six
        bytes a character to sit in the database — because `storable` handles
        the one thing the escaping would have protected against.

        **The embedding is awaited before the transaction opens.**  A write
        transaction held across a call to a model holds SQLite's write lock for
        as long as the model takes and blocks every other writer, so "atomic"
        covers the three writes and not the call.  What it buys is the property
        worth having: a save that raises stored nothing.  A slow embed on the
        save path would raise an alarm about a row that had in fact been stored —
        the complaint is the ambiguity, and there is none here.
        """
        stored = strip_images(messages)
        record = _Stored(
            document=storable(json.dumps(stored, ensure_ascii=False)),
            search=searchable_text(stored, summary, tags),
            summary=summary,
            tags=tags,
            created_at=created_at or now(),
            completed_at=completed_at or now(),
            channel=channel,
            who_helped=who_helped,
            what_model=what_model,
            token_count=int(token_count),
            context_tokens=int(context_tokens),
            usage=json.dumps(usage.to_wire()) if usage is not None else None,
        )
        chunks = embed_chunks(embed_text(stored), limit=embedder.max_chars)
        # Awaited on the loop, because it is a call to a model and not a call to
        # a disk: the one thing that must not happen here is a synchronous
        # request blocking every other call this server is answering.
        #
        # **The count is checked, as every other embedding path checks it.**  A
        # short answer used to be written as-is, and a *partial* one is the worst
        # shape it can take: the turn gets `turn_chunk` rows for some of its
        # chunks, and `_turns_without_vectors` selects turns with *no* chunk row
        # at all — so `sync_indexes` can never see this one again and
        # `index_status` reports `ready: True` over an index that is missing
        # vectors.  The save path was the one of them that did not refuse it;
        # `checked_vectors` is the one seat that check has now.  Raised before the
        # transaction opens, so a refused save is a save that stored nothing.
        vectors = (
            await checked_vectors(
                embedder,
                chunks,
                unit="chunks",
                so="this turn would be searchable only by keyword",
            )
            if chunks
            else []
        )
        # The writes are the other way round — blocking, and off the loop.
        await off_loop(self.ensure_indexes, embedder)
        return await off_loop(self._write_turn, record, vectors)

    def _write_turn(self, record: _Stored, vectors: list[list[float]]) -> int:
        """The three inserts, in one transaction, on a thread of their own."""
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO turn (messages, summary, tags, created_at,"
                " completed_at, channel, who_helped, what_model,"
                " token_count, context_tokens, usage)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.document,
                    record.summary,
                    record.tags,
                    record.created_at,
                    record.completed_at,
                    record.channel,
                    record.who_helped,
                    record.what_model,
                    record.token_count,
                    record.context_tokens,
                    record.usage,
                ),
            )
            turn_id = _rowid(cursor, what="a turn")
            connection.execute(
                "INSERT INTO turn_fts (rowid, search) VALUES (?, ?)",
                (turn_id, record.search),
            )
            _write_vectors(connection, turn_id, vectors)
            # **The live-context list is appended here, in this transaction.**
            # The reason is the one the three writes above already answer to: a
            # turn that is stored but not in the list is a turn the next restore
            # silently drops, and the window in which that is true is exactly the
            # window between two transactions.  One transaction
            # means there is no such window — a process that dies mid-save
            # leaves either both or neither.
            append_context(connection, turn_id)
            return turn_id

    def update_summary(
        self,
        turn_id: int,
        *,
        summary: str | None = None,
        tags: str | None = None,
    ) -> bool:
        """Write a summary and/or tags, and the text the turn is found by.

        **Two writes in one transaction, because the keyword index is a copy.**
        `turn_fts.search` holds `searchable_text` for the row (the schema says
        why it is a copy rather than an external-content index), so updating
        `turn` alone would leave the new words unfindable.  The vector is not
        touched: `embed_text` keeps the conversation and only the conversation,
        and a summary is a retrieval hook for the keyword leg.

        `False` means no row has that id, which the caller reports.  A call
        that names neither field is the caller's mistake and writes nothing.
        """
        if summary is None and tags is None:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT messages, summary, tags FROM turn WHERE rowid = ?",
                (turn_id,),
            ).fetchone()
            if row is None:
                return False
            messages = json.loads(str(row["messages"] or "[]"))
            new_summary = str(row["summary"] or "") if summary is None else summary
            new_tags = str(row["tags"] or "") if tags is None else tags
            connection.execute(
                "UPDATE turn SET summary = ?, tags = ? WHERE rowid = ?",
                (new_summary, new_tags, turn_id),
            )
            connection.execute(
                "UPDATE turn_fts SET search = ? WHERE rowid = ?",
                (searchable_text(messages, new_summary, new_tags), turn_id),
            )
        return True

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
        limit = page_limit(limit)
        offset = max(0, int(offset))
        clauses, params = time_window(since, until)
        where = filter_clause(clauses)
        with self._connect() as connection:
            # One read transaction for both statements.  They are two reads of a
            # table a turn can be saved to at any moment, and a save landing
            # between them makes `total` describe a different table than the
            # page does — so a caller paging by offset skips or repeats a turn.
            # WAL gives a reader one consistent snapshot for as long as its
            # transaction lasts, which is exactly this; the `with` commits it,
            # and a read-only commit changes nothing.
            connection.execute("BEGIN")
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

    # --- the live context -----------------------------------------------------

    def context_turns(self) -> list[int]:
        """The live-context list: which turns this conversation is made of.

        `[]` for a conversation whose context has never been recorded, which is
        every file on the day this table arrives — an addition, so a file
        predating it opens unchanged and answers honestly rather than needing a
        migration (`CREATE TABLE IF NOT EXISTS` is what additions need).
        """
        with self._connect() as connection:
            return read_context(connection)

    def set_context_turns(
        self, turn_ids: Sequence[int], *, expected: Sequence[int] | None = None
    ) -> list[int] | None:
        """Replace the live-context list; answers with what was written.

        The caller is the party that decided, and `[]` is one of the things it
        may decide — see `write_context` for why this store does not refuse an
        empty list.

        **`expected` is the list the caller read, and it makes the write safe
        against a conversation that moved while it decided.**  A rebuild is a
        model call between the `restore` that answered with a list and this
        write, and the callers are separate processes — the agent server's turn
        and an agent task of the same agent both rebuild one conversation, and
        either may save a turn while the other is deciding.  Without the guard
        the write composed from the older snapshot erased the newer turn: the
        row stayed in the log and left the context, silently, which is the one
        failure `append_context` exists to prevent on the save path.

        The write is one `BEGIN IMMEDIATE` transaction, so the read of the
        current list and the write cannot have a writer between them.  When the
        list moved at all — a turn saved, a reset, another rebuild — the write
        is refused and `None` is the answer: a decision taken against an older
        snapshot may not overwrite a newer conversation, and the caller keeps
        what it holds.  `None` is the refusal; the store is untouched.

        With no `expected` the write is unconditional, which is `forget`'s
        shape: a reset is an answer about the conversation and not about a
        snapshot of it.
        """
        with self._connect() as connection:
            if expected is None:
                return write_context(connection, turn_ids)
            # The read has to be inside the write lock: a deferred transaction
            # opens at the `INSERT`, so the compare would run outside it and a
            # save landing in between would be invisible to the guard.
            connection.execute("BEGIN IMMEDIATE")
            return write_context(connection, turn_ids, expected=expected)

    def turns_by_ids(self, turn_ids: Sequence[int]) -> list[TurnRecord]:
        """Those turns, **in the order asked for**, and never re-sliced.

        The order is not decoration: the live-context list *is* an ordering (it
        records what a rebuild kept alongside what it recalled, which is not
        always ascending), so this replays the list rather than re-sorting it —
        and it applies no ceiling, because the list is already the bound.  A
        method that quietly re-sorted by rowid would undo a recall every time a
        conversation was restored.

        Ids with no row are dropped rather than refused: a turn can be missing
        because a file was rebuilt or a row pruned, and a restore that refused to
        run over one absent id would turn a partial loss into a total one.  What
        that costs is a conversation that comes back shorter than it was, so the
        drop is counted and named in the log rather than passed over in silence —
        the ids are the only thing that says a log is damaged at all.

        Chunked, because the list can be longer than one statement may bind
        (`MAX_SQL_VARS`) and an unbounded `IN (…)` is a query that works in
        testing and raises in a long session.
        """
        wanted = normalise_ids(turn_ids)
        found: dict[int, TurnRecord] = {}
        with self._connect() as connection:
            for start in range(0, len(wanted), MAX_SQL_VARS):
                chunk = wanted[start : start + MAX_SQL_VARS]
                marks = _marks(chunk)
                rows = connection.execute(
                    f"SELECT rowid AS turn_id, * FROM turn WHERE rowid IN ({marks})",
                    chunk,
                ).fetchall()
                for row in rows:
                    found[int(row["turn_id"])] = _row_to_record(row)
        if len(found) != len(wanted):
            logger.warning(
                "%d of %d turns in a live context have no row: %s",
                len(wanted) - len(found),
                len(wanted),
                [turn_id for turn_id in wanted if turn_id not in found],
            )
        return [found[turn_id] for turn_id in wanted if turn_id in found]

    def _ids_in_window(
        self, turn_ids: Sequence[int], clauses: list[str], params: list[str]
    ) -> set[int]:
        """Which of those ids fall inside a `created_at` window.

        The semantic leg cannot be windowed in SQL — a KNN has no column to
        filter on — so the window is applied to the fused answer instead, which
        is the one place the two legs can be held to one rule.
        """
        if not turn_ids:
            return set()
        if not clauses:
            return set(turn_ids)
        allowed: set[int] = set()
        with self._connect() as connection:
            for start in range(0, len(turn_ids), MAX_SQL_VARS):
                chunk = turn_ids[start : start + MAX_SQL_VARS]
                marks = _marks(chunk)
                rows = connection.execute(
                    f"SELECT rowid AS turn_id FROM turn WHERE rowid IN ({marks})"
                    f" {where_clause(clauses)}",
                    (*chunk, *params),
                ).fetchall()
                allowed |= {int(row["turn_id"]) for row in rows}
        return allowed

    def _time_ranked(
        self,
        since: str | None,
        until: str | None,
        limit: int,
        newest_first: bool,
        excluded: set[int] | frozenset[int] = frozenset(),
    ) -> list[tuple[int, float | None]]:
        """Turn ids in a window, from the end `anchor` named, with no similarity.

        `None` and not zero: there is no query, so nothing was measured, and
        "no number" is a different fact from "measured as zero" — which is what
        lets the caller exempt these from a similarity floor rather than drop
        them all (see `recall`).

        **The scan is widened by the excluded ids, not the answer.**  A period
        recall asks for the newest (or oldest) turns; a kept turn in that list
        is one the caller already holds, so the window is read past it and the
        filter below is the guarantee that `limit` *new* turns come back.  The
        SQL clause is skipped when the kept list is too large to bind, and the
        filter is exact either way.
        """
        clauses, params = time_window(since, until)
        exclude_clauses, exclude_params = _exclusion_clause(set(excluded))
        where = filter_clause([*clauses, *exclude_clauses])
        order = "DESC" if newest_first else "ASC"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT t.rowid AS turn_id FROM turn t {where}"
                f" ORDER BY t.rowid {order} LIMIT ?",
                (*params, *exclude_params, limit + len(excluded)),
            ).fetchall()
        found: list[tuple[int, float | None]] = [
            (int(row["turn_id"]), None)
            for row in rows
            if int(row["turn_id"]) not in excluded
        ]
        return found[:limit]

    async def recall(
        self,
        *,
        query: str = "",
        sentences: Sequence[str] = (),
        embedder: Embedder | None = None,
        since: str | None = None,
        until: str | None = None,
        anchor: str | None = None,
        limit: int = 20,
        exclude: Sequence[int] = (),
    ) -> list[tuple[int, float | None]]:
        """Turns a rebuild may add, best first — the *ranking*, and no policy.

        One of the store's three shapes, chosen by what the caller asked for:

        * **a question** — the same hybrid as `search`, windowed, ranked by
          relevance.  `anchor` is not consulted here; relevance decides which end
          the caller spends from, and the caller is where that is logged.
        * **no question, a window or an anchor** — ranked by *time* instead, from
          the end `anchor` named (`newest` when it says nothing).  This branch has
          to run before the hybrid one, and not for symmetry: an empty question
          reaches the full-text index as a syntax error and embeds to noise.
        * **neither** — nothing, which is not an error: a recall with no
          condition is a caller that has not decided what to look for.

        **`sentences` are the meaning questions; `query` is the words.**  Each
        sentence is embedded on its own and a turn keeps its best similarity
        across them, so a second sentence can only add a turn or raise one.
        `query` is the keyword leg's AND — names, paths, identifiers, an exact
        string — and it is also the meaning query when no sentence is given;
        that is the fallback a caller uses when it cannot put the subject into a
        sentence.  A recall may carry either, or both.

        **The words are fused into each question's list, and a turn keeps its
        best question.**  A turn found by both legs *for the same sentence* is
        evidence about that sentence; a turn several sentences found keeps the
        best score rather than a sum, the same max the measured similarity
        follows.  That is the catalogue's per-question fusion applied to a
        ranking a budget can spend.

        **What is returned is ranked pairs, not a selection.**  The count, the
        similarity floor and the token budget are the *caller's* — they depend on
        a model's window and on what the decision kept, neither of which this
        store knows — and the split is deliberate: the store ranks, the policy
        caps.

        The similarity is `None` for a turn only the keyword leg found, and that
        it is `None` rather than `0.0` is load-bearing: a fused score is a
        function of rank position and carries no magnitude to threshold, so a
        floor can only gate the *measured* similarity — and an exact keyword hit
        has not been measured against anything.  An exact match is a stronger
        signal than a cosine neighbourhood, and "no number" is not evidence
        against it.

        **`exclude` is the caller's keep-list.**  A rebuild decides both halves
        in one call — what stays in hand and what is missing — and a turn in
        both is a turn the union collapses: recalling it spends a candidate
        slot, a place in the `k` and a share of the token budget on something
        already there.  The keyword leg and the time-ranked branch filter them
        in SQL; the semantic leg cannot (a `vec0` KNN has no turn id to filter
        on), so its answer is filtered after fusion — where the time window is
        filtered too — and `over` is widened by the number excluded so the
        places they took can be refilled.

        Raises:
            EmptyQuery: If no sentence is given and the query holds no term.
            InvalidTimeBound: If a bound is in no grammar `slife2.helpers.timeutil`
                speaks, for the reason `turns` gives.
        """
        limit = page_limit(limit)
        excluded = set(normalise_ids(exclude))
        meaning = [sentence for sentence in sentences if sentence.strip()]
        words = query.strip()
        if not meaning and not words:
            if not since and not until and not anchor:
                return []
            return await off_loop(
                self._time_ranked,
                since,
                until,
                limit,
                anchor != "oldest",
                excluded,
            )

        if embedder is None:
            raise ValueError(
                "a recall with a question has to be ranked by meaning, and no "
                "embedder was given"
            )
        clauses, params = time_window(since, until)
        exclude_clauses, exclude_params = _exclusion_clause(excluded)
        over = min((limit + len(excluded)) * _OVERFETCH, MAX_SQL_VARS)
        keyword: list[int] = []
        if words:
            try:
                expression = textindex.match_expression(words)
            except textindex.EmptyQuery:
                # A query with no term is no keyword leg.  With a sentence
                # beside it the recall still asks a question; without one the
                # caller asked for a search that cannot run, and the store says
                # so rather than answering the period it might have run instead.
                if not meaning:
                    raise
            else:
                # The keyword leg is windowed and excluded in SQL (its `rank`
                # orders every match, so limiting after the window is still the
                # best inside it); the semantic leg is neither, and the fusion
                # is filtered below.
                keyword = await off_loop(
                    self._keyword_hits,
                    expression,
                    over,
                    [*clauses, *exclude_clauses],
                    [*params, *exclude_params],
                )
        lists = await self._semantic_lists(meaning or [words], embedder, over)
        nearest: dict[int, float] = {}
        for found in lists:
            for turn_id, similarity in found:
                if similarity > nearest.get(turn_id, float("-inf")):
                    nearest[turn_id] = similarity
        fused = [
            turn_id
            for turn_id in _fuse_questions(
                keyword,
                [[turn_id for turn_id, _ in found] for found in lists],
            )
            if turn_id not in excluded
        ][:MAX_SQL_VARS]
        allowed = await off_loop(self._ids_in_window, fused, clauses, params)
        return [
            (turn_id, nearest.get(turn_id)) for turn_id in fused if turn_id in allowed
        ][:limit]

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM turn").fetchone()[0])

    # --- the derived indexes --------------------------------------------------

    async def sync_indexes(self, embedder: Embedder) -> None:
        """Make both indexes agree with `embedder`, rebuilding what does not.

        The four things that can leave an index unreadable — text written by
        other normalization rules, a different embedding model, a different
        width, a different endpoint — all arrive here as "the recorded identity
        is not the current one", and all take the same answer: build it again
        from the turns.  That is what makes a changed model a rebuild rather
        than a migration, and it is one mechanism for every one of them.

        Run at startup.  It is the only thing that re-embeds turns which lost
        their vectors, because a save embeds the turn it is saving and nothing
        else.

        **A batch at a time, and the loop is what finishes the index.**  A
        changed embedding model makes *every* turn missing, so a request per
        turn was a startup that paid a round trip per turn of the log — the thing
        `SYNC_BATCH` exists to bound, and the shape both other stores already
        have (`slife2.libs.store.catalogue.ToolStore.sync_indexes`, `CabinetStore.sync`).
        The store bounds a
        call and the caller finishes the index, which is the division those two
        state: a request carrying the whole log is the request that times out,
        and one that answers having embedded the first sixty-four turns says
        nothing about the rest.

        It terminates because every turn selected here is one `_place_vectors`
        then writes a chunk row for — `_turns_without_vectors` selects rows with
        none, and a turn with nothing to embed is not selected at all.  A model
        that answers with nothing is *not* the other way out: `embed_rows`
        refuses an answer shorter than the batch it was asked for, so this ends
        either with every turn placed or with the failure named.  (An arm here
        used to read an empty answer as "the index is as complete as it will
        get", which is the one thing an empty answer does not mean.)
        """
        await off_loop(self._sync_text_index)
        await off_loop(self.ensure_indexes, embedder)
        while True:
            missing = await off_loop(self._turns_without_vectors, SYNC_BATCH)
            if not missing:
                break
            vectors = await embed_rows(missing, embedder, noun="turn")
            await off_loop(self._place_vectors, vectors)

    def _place_vectors(self, vectors: Mapping[int, list[list[float]]]) -> None:
        """Write a batch of vectors, in one transaction."""
        with self._connect() as connection:
            for turn_id, rows in vectors.items():
                _write_vectors(connection, turn_id, rows)

    def ensure_indexes(self, embedder: Embedder) -> None:
        """Create the vector index, or rebuild it if its identity has changed.

        Cheap when the index is current: one read of `index_meta`.  A rebuild
        here **drops the vectors** without re-embedding anything, because the
        caller is a save that is embedding one turn — putting the rest back is
        `sync_indexes`, which startup runs before anything is served.

        The five statements themselves are `slife2.libs.store.index.rebuild_vector_index`'s,
        which is the same rebuild the catalogue and the cabinet make — including
        why the `DROP` and the `CREATE` have to be inside one transaction.
        """
        with self._connect() as connection:
            rebuild_vector_index(
                connection,
                vec_table="turn_vec",
                chunk_table="turn_chunk",
                dimension=embedder.dimension,
                identity=_vector_identity(embedder),
            )

    def _sync_text_index(self) -> None:
        """Rebuild the keyword index when the text or the rules built it changed.

        **One condition, because the other case cannot happen.**  A row can only
        be missing from this index if the version stamp says the text and the
        rules match — and then it cannot be, since the rows are written in the
        same transaction as the turns they index.  A database with no stamp at
        all is simply the version-mismatch case, and is rebuilt whole; that is
        why there is nothing here for "an older file", and why there is no
        migration to write: a file this build cannot read is named by
        `index_status`, not quietly upgraded.
        """
        with self._connect() as connection:
            if (
                self.index_meta(connection).get("text_version", "")
                == _search_text_version()
            ):
                return
            connection.execute("DELETE FROM turn_fts")
            rows = connection.execute(
                "SELECT rowid, messages, summary, tags FROM turn"
            ).fetchall()
            connection.executemany(
                "INSERT INTO turn_fts (rowid, search) VALUES (?, ?)",
                [
                    (
                        int(row["rowid"]),
                        searchable_text(
                            _messages(
                                row["messages"],
                                what=f"turn {row['rowid']}'s messages",
                            ),
                            row["summary"],
                            row["tags"],
                        ),
                    )
                    for row in rows
                ],
            )
            self.set_index_meta(connection, text_version=_search_text_version())

    def _turns_without_vectors(self, limit: int = 0) -> list[tuple[int, str]]:
        """Every turn with a vector still owed, oldest first.

        The index is derived, so "has no vector" is the whole of the state a
        pending turn has — there is no column to keep in step and nothing to
        mark, and a turn whose embedding failed is simply found here.

        **A turn with nothing to embed owes no vector, and is not one of
        these.**  `embed_text` is empty for a turn whose only message is blank —
        a bare `send_message(agent, "")` that the model answered with neither
        text nor a tool call — and no model can be asked for a vector of
        nothing.  Counted as pending it would be pending for ever: no sync could
        ever place a vector for it, so `index_status` would never be clean and
        the context plugin, which refuses to serve an index that is not, would
        never come up.

        `limit` bounds one call, `0` meaning all of them — the caller that loops
        passes `SYNC_BATCH`, because "every turn" on a log written by a different
        embedding model is every turn there is.  **The bound is applied after the
        filter and not in SQL**, unlike the catalogue's: what a turn is about is
        composed in Python (`embed_text`), so a `LIMIT` in the query would count
        rows the filter is about to drop and a batch of turns with nothing to say
        would end the caller's loop with rows still owed behind it.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT rowid, messages FROM turn "
                "WHERE rowid NOT IN (SELECT DISTINCT turn_id FROM turn_chunk) "
                "ORDER BY rowid"
            ).fetchall()
        found = [
            (int(row["rowid"]), text)
            for row in rows
            if (
                text := embed_text(
                    _messages(row["messages"], what=f"turn {row['rowid']}'s messages")
                )
            ).strip()
        ]
        return found[:limit] if limit else found

    def index_status(self, embedder: Embedder) -> dict[str, Any]:
        """Whether a search can actually run on both legs, and what is missing.

        **Looked at rather than remembered.**  The question a caller needs
        answered is not "did the sync run" but "is every turn in the index, and
        is it the index this embedder built" — and only counting can answer
        that, because a sync that died halfway leaves nothing to remember it by.
        Both halves of the plugin that owns this store ask it before they serve
        and refuse to if the answer is not clean: there is no mode in which a
        turn with text is stored that semantic search cannot find, so an index
        that is not ready is a system that has come apart rather than a plugin
        working with less.  A turn with nothing to embed is the one exception
        and is not a problem — see `_turns_without_vectors`.

        `problems` is written to be read by a person looking at a startup
        failure, so each entry names what is wrong rather than which check ran.

        **The keyword rules are deliberately not one of these facts.**  They can
        only change when this code changes, and that means a restart, which runs
        `sync_indexes` — so by the time anything asks, the rules have already
        been reconciled.  Checking the version here would instead report a fresh
        database, written entirely by the current rules, as unready.
        """
        with self._connect() as connection:
            turns = int(connection.execute("SELECT COUNT(*) FROM turn").fetchone()[0])
            vectors = int(
                connection.execute("SELECT COUNT(*) FROM turn_chunk").fetchone()[0]
            )
            keyword = int(
                connection.execute("SELECT COUNT(*) FROM turn_fts").fetchone()[0]
            )
            meta = self.index_meta(connection)
        # **Asked of the same helper `sync_indexes` uses, not of a second copy
        # of the query.**  "Pending" has to mean what the sync can fix: a turn
        # with nothing to embed is not one of these (see
        # `_turns_without_vectors`), and a count read straight off `turn_chunk`
        # would call it pending for ever — which is the whole of what this
        # reports, so the two answers must not be able to disagree.
        pending = len(self._turns_without_vectors())

        problems: list[str] = []
        if meta.get("vector_identity", "") != _vector_identity(embedder):
            problems.append(
                "the vector index was built by a different model or a different "
                "text contract than the configured one"
            )
        if pending:
            problems.append(f"{pending} of {turns} turns have no vector")
        if keyword != turns:
            # Said with the answer in it, because there is no migration to run:
            # a file in this state is one this build will not upgrade, and the
            # only thing to do about it is the thing `slife2.libs.store.db`'s docstring
            # already says about every schema change.
            problems.append(
                f"{turns - keyword} of {turns} turns are not in the keyword "
                f"index and there is no migration — delete the file and let it "
                f"be recorded afresh"
            )

        return {
            "turns": turns,
            "vectors": vectors,
            "keyword": keyword,
            "pending": pending,
            "ready": not problems,
            "problems": problems,
        }

    @staticmethod
    def index_meta(connection: sqlite3.Connection) -> dict[str, str]:
        """What each index was built with, as recorded in the file itself."""
        return index_meta(connection)

    @staticmethod
    def set_index_meta(connection: sqlite3.Connection, **values: str) -> None:
        set_index_meta(connection, **values)

    # --- finding a turn by what it was about ----------------------------------

    async def search(
        self,
        query: str,
        *,
        embedder: Embedder,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
    ) -> list[TurnRecord]:
        """Turns matching `query`, best first — by keyword and by meaning.

        Two legs and one fusion.  The keyword leg is FTS5's `bm25` over the text
        a turn is found by (`searchable_text`); the semantic leg is a KNN over
        the vectors of what it was about (`embed_text`).  They are fused by rank
        because their scores are not on one scale at all — `bm25` is unbounded
        and depends on the corpus, a cosine distance depends on the model — and
        a turn both legs found outranks a turn only one did.

        No `total`, deliberately: the two legs rank different numbers of
        candidates and a fused top-k has no total that means anything.  A page
        of best-first turns is what this answers.

        Raises:
            EmptyQuery: If the query holds no term — see `slife2.libs.store.textindex`.
            InvalidTimeBound: If a bound is in no grammar `slife2.helpers.timeutil`
                speaks, for the reason `turns` gives.
        """
        expression = textindex.match_expression(query)
        clauses, params = time_window(since, until)
        limit = page_limit(limit)
        over = min(limit * _OVERFETCH, MAX_SQL_VARS)

        ranked = {
            "keyword": await off_loop(
                self._keyword_hits, expression, over, clauses, params
            ),
            # `list(...)` of the mapping, which is in nearest-first order: the
            # fusion wants a *ranking*, and the similarities the same call
            # measured are what `recall` gates on.
            "semantic": list(await self._semantic_hits([query], embedder, over)),
        }
        # Capped here and not on the legs: fusing takes the *union* of two lists
        # that are each already `over` long, and the next query binds one value
        # per id.  A cap applied to each leg would leave the union at twice it.
        fused = [turn_id for turn_id, _ in fuse_ranked(ranked)][:MAX_SQL_VARS]
        return await off_loop(self._records_in_order, fused, clauses, params, limit)

    def _keyword_hits(
        self,
        expression: str,
        limit: int,
        clauses: list[str],
        params: Sequence[Any],
    ) -> list[int]:
        """Turn ids by `bm25`, best first.

        The window is applied here in SQL rather than to the fused answer, which
        is the one place it can be exact: `rank` orders *all* the matches, so
        limiting after the window still returns the best turns inside it.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT f.rowid AS turn_id FROM turn_fts f"
                " JOIN turn t ON t.rowid = f.rowid"
                f" WHERE turn_fts MATCH ? {where_clause(clauses)}"
                " ORDER BY rank LIMIT ?",
                (expression, *params, limit),
            ).fetchall()
        return [int(row["turn_id"]) for row in rows]

    async def _semantic_lists(
        self, queries: Sequence[str], embedder: Embedder, k: int
    ) -> list[list[tuple[int, float]]]:
        """One ranked list per question, out of **one** `embed`, nearest first.

        **The questions are asked in a batch.**  A call per sentence is a round
        trip per sentence, and the answer to one is not looked at while the next
        is asked for; `checked_vectors` is the one count check every embedding
        path shares.  A blank question is dropped before the call, so the
        caller's list and the answer line up by position.

        `k` counts **chunks**, not turns, which is why it is over-fetched: a
        turn with four chunks can occupy four of the k places, and the answer
        wants turns.  Deduplicated in `_nearest_turns` rather than in SQL
        because a vec0 KNN refuses `GROUP BY`.  The window is *not* applied — a
        KNN cannot be constrained by a column it does not have — so `recall`
        filters the fused answer instead, and a window narrow enough to exclude
        the k nearest chunks can return fewer turns than it asked for.

        Each list is sorted by similarity, ties to the older turn, so the rank a
        fusion reads is stable whatever order `vec0` returned equal distances
        in.  The **raw** questions, not `textindex.normalize(...)`:
        normalization is the keyword leg's rule and it inserts a space between
        every pair of CJK characters, which is what makes them tokens there and
        is nonsense as text handed to a model.  Measured: the normalized query
        landed nearest the turns sharing *no* word with it.
        """
        asked = [query for query in queries if query.strip()]
        if not asked:
            return []
        vectors = await checked_vectors(
            embedder,
            asked,
            unit="questions",
            so="this search cannot say what a turn was about",
        )
        lists: list[list[tuple[int, float]]] = []
        for vector in vectors:
            found = await off_loop(
                self._nearest_turns, sqlite_vec.serialize_float32(vector), k
            )
            lists.append(sorted(found, key=lambda pair: (-pair[1], pair[0])))
        return lists

    async def _semantic_hits(
        self, queries: Sequence[str], embedder: Embedder, k: int
    ) -> dict[int, float]:
        """Turn ids by vector distance across one or more questions, nearest first.

        **Several questions are several chances, and one must not cost another.**
        Each sentence is embedded on its own — one `embed` call for the batch,
        from `_semantic_lists` — and each has its own KNN; a turn keeps its
        *best* similarity across them.  A second sentence can therefore add a
        turn or raise a turn's score, never lower one or remove it.  This is the
        rule the catalogue's `sentences` already state, applied to turns.

        **A mapping, in nearest-first order, and the value is a similarity.**
        The order is what `search` fuses; the value is what `recall` gates on —
        and it is a *measured* number, unlike the fused score, which is a
        function of rank position and carries no magnitude to threshold
        (`recall`'s docstring is where that distinction is argued).
        """
        nearest: dict[int, float] = {}
        for found in await self._semantic_lists(queries, embedder, k):
            for turn_id, similarity in found:
                if similarity > nearest.get(turn_id, float("-inf")):
                    nearest[turn_id] = similarity
        return dict(sorted(nearest.items(), key=lambda pair: (-pair[1], pair[0])))

    def _nearest_turns(self, query_vector: bytes, k: int) -> list[tuple[int, float]]:
        """The KNN, the dedup it cannot do itself, and the similarity.

        **The *best* chunk of a turn stands for the turn**, which is what the
        `setdefault` is: a turn's chunks are ranked separately, so the first one
        seen for a turn is its nearest, and a later, worse chunk must not
        overwrite it.

        `1 - distance` is a similarity only because the DDL says `cosine` — on an
        L2 table that arithmetic yields numbers that are plausible and wrong
        rather than visibly broken, which is what `vector_ddl` exists to
        prevent.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT c.turn_id AS turn_id, v.distance AS distance"
                " FROM turn_vec v JOIN turn_chunk c ON c.id = v.rowid"
                " WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
                (query_vector, k),
            ).fetchall()
        nearest: dict[int, float] = {}
        for row in rows:
            nearest.setdefault(int(row["turn_id"]), as_similarity(row["distance"]))
        return list(nearest.items())

    def _records_in_order(
        self,
        turn_ids: list[int],
        clauses: list[str],
        params: list[str],
        limit: int,
    ) -> list[TurnRecord]:
        """Those turns, in the order they were ranked, windowed and cut.

        One query rather than one per turn, because the order comes from the
        fusion and the rows come from the table; a window can only remove turns
        from the answer, never reorder it.  The query itself is `rows_in_order`,
        which is the same one the catalogue's search makes with its own table's
        name in it.
        """
        with self._connect() as connection:
            rows = rows_in_order(
                connection,
                turn_ids,
                select="SELECT rowid AS turn_id, * FROM turn",
                column="rowid",
                key="turn_id",
                clauses=clauses,
                values=params,
                limit=limit,
            )
        return [_row_to_record(row) for row in rows]


def _write_vectors(
    connection: sqlite3.Connection, turn_id: int, vectors: list[list[float]]
) -> None:
    """Place a turn's chunks, one vector each, under ids of their own.

    The write is `slife2.libs.store.index.place_vectors`'s, with this corpus's names on it;
    that is where the reason a chunk is a chunk lives.  No clear comes first,
    and that is the turn store's one difference from the other two: a turn is
    appended and never rewritten, so there is no old row's rowids to collide
    with.
    """
    place_vectors(
        connection,
        chunk_table="turn_chunk",
        vec_table="turn_vec",
        key_column="turn_id",
        row_key=turn_id,
        vectors=vectors,
        what="a turn chunk",
    )


# --- the live context ---------------------------------------------------------


def read_context(connection: sqlite3.Connection) -> list[int]:
    """The persisted live-context list, in the order it was written.

    **A damaged list is refused rather than read as empty.**  The empty list
    means something here — "this conversation's context has not been recorded",
    which is every file predating the table, and one of the three things a
    rebuild may decide — and it is also what `_messages` would answer for a
    `turn_ids` whose JSON will not parse, or parses to something that is not a
    list of turn ids at all.  Those are not the same fact, and the
    difference is not academic: `append_context` reads this list and appends to
    it, so a damaged list read as `[]` becomes a **one-turn context** on the
    next save, permanently and without a word.  `_structural` is the refusal,
    and SDD Appendix C is the rule it follows.
    """
    row = connection.execute("SELECT turn_ids FROM context WHERE id = 1").fetchone()
    if row is None:
        return []
    return normalise_ids(_structural(row["turn_ids"], what="context.turn_ids"))


def write_context(
    connection: sqlite3.Connection,
    turn_ids: Sequence[int],
    *,
    expected: Sequence[int] | None = None,
) -> list[int] | None:
    """Replace the live-context list, and answer with what was written.

    **One operation, not four.**  Every editor already holds the whole list it
    wants next (a rebuild has the selection, a trim has the survivors, a reset
    has nothing), so the operation they all want is "this is the list now";
    `set`, `drop`, `clear` and a read-modify-write on the save path would be four
    spellings of it.  The save's *append* is the one exception, and it lives in
    `append_context` where the transaction is.

    **An empty list is a legitimate list.**  A guard protecting a *partial*
    selection from being mistaken for a deliberate one has nothing to protect
    against here: the caller has already made the decision — "none of them" is
    one of the three things `context` can say — and a store that second-guessed
    it would turn the one explicit clear into an error.

    **`expected` makes it a compare-and-set**, for the reason
    `TurnStore.set_context_turns` states: a decision is composed against a
    snapshot and written later, and a save landing in between must not be
    erased.  The comparison and the write are the caller's one transaction —
    this function is called with the write lock already held when `expected` is
    given.

    `None` means the snapshot no longer describes the conversation: a turn
    arrived, one left, or another decision was written since the caller read it.
    The newer list is the one that stands — `rebuild` adopts it for the turn it
    is about to run — and this store is untouched.  A merge was considered and
    refused: the choice of list is the caller's to make, and a store that
    invented one from two decisions would be answering a question it was not
    asked.
    """
    written = normalise_ids(turn_ids)
    if expected is not None and read_context(connection) != normalise_ids(expected):
        return None
    connection.execute(
        "INSERT INTO context (id, turn_ids) VALUES (1, ?)"
        " ON CONFLICT(id) DO UPDATE SET turn_ids = excluded.turn_ids",
        (json.dumps(written),),
    )
    return written


def append_context(
    connection: sqlite3.Connection, turn_id: int
) -> list[int] | None:
    """Add one turn to the end of the live-context list, read inside the write.

    Read-modify-write inside the caller's transaction rather than by the caller,
    because the two halves have to be one statement's worth of atomic: a caller
    that read the list, and then wrote it back, would lose whatever a second
    writer appended in between — and the second writer is the ordinary case, not
    a race, since a queued turn's save can land while the next turn is being
    built.
    """
    return write_context(connection, [*read_context(connection), int(turn_id)])


def _row_to_record(row: sqlite3.Row) -> TurnRecord:
    """Rebuild a record, tolerating a row whose `messages` will not read.

    A corrupt row is one turn, and refusing to read *any* history because one
    row is damaged would turn a small problem into a total one.
    """
    return TurnRecord(
        turn_id=int(row["turn_id"]),
        messages=_messages(
            row["messages"], what=f"turn {int(row['turn_id'])}'s messages"
        ),
        summary=str(row["summary"] or ""),
        tags=str(row["tags"] or ""),
        created_at=str(row["created_at"]),
        completed_at=row["completed_at"],
        channel=str(row["channel"] or ""),
        who_helped=str(row["who_helped"] or ""),
        what_model=str(row["what_model"] or ""),
        token_count=int(row["token_count"]),
        context_tokens=int(row["context_tokens"]),
        usage=_usage(row["usage"], what=f"turn {int(row['turn_id'])}"),
    )


def _usage(raw: Any, *, what: str) -> Usage | None:
    """The stored breakdown, or None when this row carries none.

    None is the honest answer twice over: a row written before the column
    existed, and a value that will not read as one.  The totals beside it are
    still the row's own, so a damaged breakdown costs the split and not the
    bill — the same field-not-structure rule `_messages` follows.

    **The damaged half is the fallback registered in SDD Appendix C**: *it
    gives up* the token split for that turn, *it does not cover* the messages,
    which have their own row, and the failure goes to the warning naming the
    row.  Refusing the row instead would turn one damaged field into unreadable
    history, which is the total loss `_messages` exists to avoid.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("usage is not a JSON object")
        return Usage.from_wire(parsed)
    except (TypeError, ValueError) as exc:
        logger.warning("%s: %s; the totals are kept", what, exc)
        return None


def _messages(raw: Any, *, what: str) -> list[dict[str, Any]]:
    """A stored turn's `messages`, tolerating one that will not read as them.

    **For a field, and never for a structure.**  A turn whose `messages` are
    damaged is one damaged turn, and refusing to read any history because of it
    would turn a small problem into a total one — so the empty list is the
    answer and the warning is the report.  `_structural` is the other half, and
    the question that tells them apart is what the empty value *means*: here it
    means "this one turn's body is unreadable", which is a smaller claim than
    the damaged value made.  There it means something the damage did not
    (SDD Appendix C).

    **The *shape* is damage too, and it is the half that was missing.**  A
    stored value that parses to a string, a number, or a list of strings is
    readable JSON and is not `messages`, and every reader of one asks it for a
    key: `to_listing`'s two halves, `embed_text`, `searchable_text`.  Each of
    them used to fail with `<type> object has no attribute 'get'` from inside a
    comprehension — and a failure is not the one damaged turn this tolerance
    trades for, it is the whole history, plus the whole `slife2-context` plugin,
    which lists and indexes every turn file it knows of as it comes up.  That is
    strictly worse than the total loss this function exists to avoid, so a value
    that is not a list of mappings is refused exactly as one that will not parse
    is: they are the same fact about the row, and the same answer is honest for
    both.

    `what` is not decoration: a damaged row that is read as empty leaves a log
    line as the only trace of it, so the line has to say *which* row — the
    alternative is an operator counting rows by hand.
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, list) and all(isinstance(one, dict) for one in parsed):
        return parsed
    logger.warning("%s did not read as a list of messages; reading it as empty", what)
    return []


def _structural(raw: Any, *, what: str) -> Any:
    """A stored value whose damage the caller cannot carry on past.

    Raised rather than answered with a default, and it names what could not be
    read.  A damaged *structure* has no honest empty value, because whatever
    reads it goes on to write: an empty list is a decision, and the damage did
    not make it.
    """
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"a stored value did not parse: {what}") from exc


def store_for(agent: str, subagent: str = "") -> TurnStore:
    """The store for one client id, creating its file if needed."""
    return TurnStore(database_path(agent, subagent))
