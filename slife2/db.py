"""The db's storage — the turns, one SQLite file per agent.

The turns are what it holds today, and they are the whole of what slife2
persists: a second thing worth keeping — a tool catalogue, a search index —
belongs beside these rows or beside these files rather than in a component of
its own, which is why this module is named for the component and not for the
table.  Everything below the next paragraph is about turns.

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

**The search indexes are derived, not migrated.**  Finding a turn by what it was
about needs a keyword index over its text and a vector index over what it was
about, and both are built from rows that are already here — so neither is a
thing this schema has to carry forward.  They are three tables added beside
`turn` — `turn_fts`, `turn_vec`, `index_meta` — and not one column of `turn`
changed, which is the addition `CREATE TABLE IF NOT EXISTS` already covers.
`index_meta` records the identity each index was built with: the rules the text
was normalized by (`slife2.textindex`), and the embedding provider, model,
endpoint and width.  An index whose identity no longer matches the
configuration is dropped and rebuilt rather than read, which is what makes a
changed embedding model a rebuild instead of a migration — and it is one
mechanism for all four things that can invalidate one.

**A turn is written with its vector, in one transaction.**  `save_turn` is
async because the embedding is a call to a model, and its three writes are
atomic because the alternative is a turn that is half indexed — and because a
save that raises has to be a save that stored nothing.  That is the one property
that makes the failure honest, and it is why the embedding model is a hard
dependency of this component rather than something that can be switched off: an
endpoint that cannot be reached fails the save, where an endpoint that silently
degraded would store turns nothing could ever find.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import sqlite_vec

from slife2 import textindex
from slife2.clock import now
from slife2.paths import db_dir
from slife2.timeutil import normalize_bound

logger = logging.getLogger(__name__)

#: Characters an agent name may keep when it becomes a filename.  Everything
#: else is replaced, because the name arrives from `--agent` on a command line
#: and a path is not something a command line should be able to reach.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: How long SQLite waits for a lock before giving up.  The database server is the
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

#: How many candidates each leg of a search brings to the fusion, as a multiple
#: of what was asked for.  Fusion is a vote between ranked lists, so the lists
#: want to be deeper than the answer — and one leg needs the slack for a second
#: reason: `k` on a vector search counts *chunks*, and several of them can belong
#: to one turn.
_OVERFETCH = 4

#: How many values one statement may bind.  SQLite's default limit is 999, and
#: the fused id list costs a bound value per row, so a search over-fetching a
#: full page would otherwise run past it.
_MAX_SQL_VARS = 900


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
    return db_dir() / f"{safe}.turn.db"


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
    created_at: str
    completed_at: str
    channel: str
    who_helped: str
    what_model: str
    token_count: int
    context_tokens: int


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
    tokenize='unicode61 remove_diacritics 2'
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


# --- what a turn is found by, and what it is about ----------------------------
#
# Two different strings, and the difference is deliberate.  A turn is *found* by
# everything a person would look for it with, and it is *about* what was said —
# so the keyword index keeps the whole of it (summaries and tags included, which
# is what those columns are for) while the vector index keeps only the
# conversation, for the reason below.


#: The version of both contracts above.  Recorded beside the indexes, so a
#: change to either is what makes an index rebuild — the same lever as a changed
#: embedding model, and deliberately not a second one.
TEXT_VERSION = "1"

#: How much of a tool call's arguments a vector keeps.  The call is part of what
#: a turn was about — "the one where it used the calculator" is a real question —
#: but the arguments are incidental and can be enormous.
TOOL_ARG_CHARS = 400

#: How much text one vector covers.  A turn is chunked rather than embedded in
#: one piece because a vector is an average: a long turn embedded whole
#: describes everything in it a little and nothing in it well.
EMBED_CHUNK_CHARS = 2000

#: How much of the last paragraph is repeated at the head of the next chunk.  A
#: sentence cut in half at a chunk boundary is a sentence neither vector holds,
#: and a paragraph is the least that puts it back together.
EMBED_OVERLAP_PARAGRAPHS = 1

_PARAGRAPH_RE = re.compile(r"\n\s*\n")


def searchable_text(
    messages: list[dict[str, Any]], summary: str = "", tags: str = ""
) -> str:
    """The text a turn is *found* by — everything worth looking for it with.

    Both halves of the exchange, plus the two retrieval hooks for whoever
    writes them later: a term that survives only in a summary is the keyword
    leg's to find, and that is the whole reason `summary` is in this string.

    Normalized here rather than at the index, because a query has to be
    normalized by the same rule (`slife2.textindex`) and this is the one place
    that rule is applied to stored text.

    Through `_storable` like the turn itself, and for the same reason: this
    string is bound to SQLite too, so a lone surrogate arriving in an answer
    would fail the insert and take the turn with it.
    """
    parts = [
        user_message(messages),
        assistant_message(messages),
        summary,
        tags,
    ]
    return _storable(textindex.normalize("\n".join(part for part in parts if part)))


def _tool_call_text(messages: list[dict[str, Any]]) -> list[str]:
    """What each tool call was, as `name` and the head of its arguments."""
    found: list[str] = []
    for message in messages:
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            name = str(function.get("name") or "")
            arguments = str(function.get("arguments") or "")[:TOOL_ARG_CHARS]
            line = f"{name} {arguments}".strip()
            if line:
                found.append(line)
    return found


def embed_text(messages: list[dict[str, Any]]) -> str:
    """The text a turn is *about*, which is not the text it is found by.

    What was asked, what was answered, and which tools were called — **and not
    what the tools answered**.  Measured on v1's live turn log, tool results were
    56–99% of a turn's text, so an index built on them describes "an agent ran
    tools" rather than what the turn was about: every turn lands in one narrow
    cosine band and a similarity floor has nothing left to separate.  Nothing is
    lost to search by leaving them out — a result is still in the turn, which is
    what the keyword leg and `turn_read` read.

    Summaries and tags are out for a second reason: they are written *after* the
    turn, and a vector index that depended on them would have to be rebuilt
    every time one was added.

    Through `_storable` for the reason `searchable_text` gives: this string goes
    out as JSON on its way to a model, and a lone surrogate cannot be encoded.
    """
    parts = [
        user_message(messages),
        *_tool_call_text(messages),
        assistant_message(messages),
    ]
    return _storable("\n\n".join(part for part in parts if part))


def embed_chunks(
    text: str, *, chars: int = EMBED_CHUNK_CHARS, limit: int = 0
) -> list[str]:
    """A turn's text cut into the pieces that each get their own vector.

    Packed by paragraph, with the last paragraph of a chunk repeated at the head
    of the next: a chunk boundary that falls inside a sentence leaves neither
    vector holding it, and a paragraph of overlap is what puts it back.

    `limit` is the most text the embedding model will take, in characters.  A
    single paragraph longer than that is truncated rather than split — a
    paragraph that long is machine output, and its tail is what the keyword leg
    is for.
    """
    paragraphs = [found.strip() for found in _PARAGRAPH_RE.split(text) if found.strip()]
    if not paragraphs:
        return []

    limit = limit or chars
    chunks: list[str] = []
    window: list[str] = []
    size = 0
    for paragraph in paragraphs:
        if window and size + len(paragraph) > chars:
            chunks.append("\n\n".join(window)[:limit])
            window = (
                window[-EMBED_OVERLAP_PARAGRAPHS:] if EMBED_OVERLAP_PARAGRAPHS else []
            )
            size = sum(len(kept) for kept in window)
        window.append(paragraph)
        size += len(paragraph)
    chunks.append("\n\n".join(window)[:limit])
    return chunks


# --- fusing the two legs ------------------------------------------------------

#: The fusion's constant: how quickly rank stops mattering.  Sixty is the value
#: the literature uses and v1 shipped; nothing about this store is a reason to
#: differ.
RRF_K = 60


def fuse_ranked(legs: dict[str, list[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal rank fusion of named ranked lists of turn ids.

    Rank and not score, because the two legs do not share a scale: `bm25` is
    unbounded and depends on the corpus, and a cosine distance is bounded and
    depends on the model.  Normalizing one onto the other is the part that
    breaks; a rank is comparable by construction.

    Ties fall to the newer turn, which is the order the browse is in — a tie is
    a real possibility when both legs put the same turns in the same order, and
    an arbitrary but stable answer beats an unstable one.
    """
    scores: dict[int, float] = {}
    for ranked in legs.values():
        for rank, turn_id in enumerate(ranked, start=1):
            scores[turn_id] = scores.get(turn_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], -pair[0]))


def _time_window(since: str | None, until: str | None) -> tuple[list[str], list[str]]:
    """`(clauses, params)` for a `created_at` window.

    **Clauses and not a `WHERE`**, because there are three readers of this one
    time axis now and they compose it differently: the browse puts it alone, and
    both legs of a search put it beside a predicate of their own.  A second
    spelling of `created_at >= ?` would be a second place for the column name
    and the grammar to drift apart.

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
    return clauses, params


def _where(clauses: list[str]) -> str:
    """Those clauses as the tail of a query that already has a `WHERE`."""
    return f"AND {' AND '.join(clauses)}" if clauses else ""


class VectorIndexUnavailable(RuntimeError):
    """The vector extension could not be loaded, so nothing here can work.

    Fatal rather than a degraded mode: an index that silently is not there
    stores turns nothing can find, which is the one failure this component must
    not have.  The message names both ways it happens — an interpreter built
    without `enable_load_extension`, and a missing wheel.
    """


#: The identity of the text the *vector* index was built from, as opposed to the
#: keyword one.  Composed by the caller (`slife2.db_server`) out of the provider,
#: model and endpoint, because what makes two embeddings comparable is a
#: question about the embedding service and not about this file.
class Embedder(Protocol):
    """What the store needs in order to place a turn in the vector index.

    Three facts and one call.  The identity is opaque here — it is compared and
    recorded, never interpreted — and a change to it is what makes the index
    rebuild.  `dimension` has to be known *before* the first embedding, because
    `vec0` fixes its width in the DDL and cannot be altered afterwards.
    """

    @property
    def identity(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    @property
    def max_chars(self) -> int:
        """The most text one request may carry, in characters."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _vector_ddl(dimension: int) -> str:
    """The vector index's DDL, for one width.

    `distance_metric=cosine` is declared rather than left to the default, and
    the reason is not preference.  It decides whether `1 - distance` is a
    similarity at all: a backend does not have to return unit-norm vectors, so
    on an L2 table that arithmetic yields numbers that are plausible and wrong
    rather than visibly broken.  v1 shipped that bug — distances past 18, every
    strong hit clamped to zero similarity — and the fix was this clause.

    The accepted spelling was checked against the installed extension rather
    than its documentation, which describes itself as a work in progress.
    """
    return (
        "CREATE VIRTUAL TABLE IF NOT EXISTS turn_vec USING vec0("
        f"embedding float[{int(dimension)}] distance_metric=cosine)"
    )


class TurnStore:
    """Reads and writes one agent's turns, and the two indexes over them.

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
        # The vector index arrives as a loadable extension, and an extension is
        # loaded *into a connection* — so this store, which opens one per call,
        # loads it every time.  Measured at about half a microsecond, which is
        # why it happens here unconditionally: a second connection path used
        # only where a vector is touched would be a second thing to keep in
        # step, and would buy nothing.
        self._load_vector_index(connection)
        return connection

    @staticmethod
    def _load_vector_index(connection: sqlite3.Connection) -> None:
        """Load the extension, or say why the component cannot run at all.

        Raises:
            VectorIndexUnavailable: If this interpreter cannot load extensions
                (Apple's and python.org's builds both ship that way) or the
                wheel is not importable.  Named here rather than left to surface
                as an `OperationalError` about a missing function, because the
                two causes need different things done about them.
        """
        if not hasattr(connection, "enable_load_extension"):
            raise VectorIndexUnavailable(
                "this Python's sqlite3 cannot load extensions, so there is no "
                "vector index: it was built without "
                "--enable-loadable-sqlite-extensions"
            )
        try:
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
        except Exception as exc:  # noqa: BLE001 — re-raised as the one cause
            raise VectorIndexUnavailable(f"cannot load sqlite-vec: {exc}") from exc
        finally:
            connection.enable_load_extension(False)

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

    async def save_turn(
        self,
        *,
        messages: list[dict[str, Any]],
        embedder: Embedder,
        channel: str = "",
        who_helped: str = "",
        what_model: str = "",
        token_count: int = 0,
        context_tokens: int = 0,
        created_at: str | None = None,
        completed_at: str | None = None,
    ) -> int:
        """Append one turn with both of its indexes, in one transaction.

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

        **The embedding is awaited before the transaction opens.**  A write
        transaction held across a call to a model holds SQLite's write lock for
        as long as the model takes and blocks every other writer, so "atomic"
        covers the three writes and not the call.  What it buys is the property
        worth having: a save that raises stored nothing.  v1 moved embedding off
        the save path because a slow embed raised an alarm about a row that had
        in fact been stored — the complaint was the ambiguity, and there is none
        here.
        """
        stored = strip_images(messages)
        record = _Stored(
            document=_storable(
                json.dumps(compact_tool_results(stored), ensure_ascii=False)
            ),
            search=searchable_text(stored),
            created_at=created_at or now(),
            completed_at=completed_at or now(),
            channel=channel,
            who_helped=who_helped,
            what_model=what_model,
            token_count=int(token_count),
            context_tokens=int(context_tokens),
        )
        chunks = embed_chunks(embed_text(stored), limit=embedder.max_chars)
        # Awaited on the loop, because it is a call to a model and not a call to
        # a disk: the one thing that must not happen here is a synchronous
        # request blocking every other call this server is answering.
        vectors = await embedder.embed(chunks) if chunks else []
        # The writes are the other way round — blocking, and off the loop.
        await self._off_loop(self.ensure_indexes, embedder)
        return await self._off_loop(self._write_turn, record, vectors)

    def _write_turn(self, record: _Stored, vectors: list[list[float]]) -> int:
        """The three inserts, in one transaction, on a thread of their own."""
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO turn (messages, created_at, completed_at,"
                " channel, who_helped, what_model, token_count, context_tokens)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.document,
                    record.created_at,
                    record.completed_at,
                    record.channel,
                    record.who_helped,
                    record.what_model,
                    record.token_count,
                    record.context_tokens,
                ),
            )
            turn_id = int(cursor.lastrowid or 0)
            connection.execute(
                "INSERT INTO turn_fts (rowid, search) VALUES (?, ?)",
                (turn_id, record.search),
            )
            _write_vectors(connection, turn_id, vectors)
            return turn_id

    @staticmethod
    async def _off_loop(function, *args, **kwargs):
        """Run one blocking store call on a worker thread.

        **Being `async` is not what keeps the loop free.**  SQLite here is
        blocking: a statement issued on the loop holds it for as long as the
        disk takes, and this server answers every agent instance from one loop,
        so one slow read would stall an unrelated conversation.  The thread hop
        is what actually yields — and it is safe precisely because a connection
        is never shared: every call opens its own (see the class docstring).
        """
        return await asyncio.to_thread(function, *args, **kwargs)

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
        clauses, params = _time_window(since, until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
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
        """
        await self._off_loop(self._sync_text_index)
        await self._off_loop(self.ensure_indexes, embedder)
        for turn_id, text in await self._off_loop(self._turns_without_vectors):
            vectors = await embedder.embed(embed_chunks(text, limit=embedder.max_chars))
            await self._off_loop(self._place_vectors, turn_id, vectors)

    def _place_vectors(self, turn_id: int, vectors: list[list[float]]) -> None:
        with self._connect() as connection:
            _write_vectors(connection, turn_id, vectors)

    def ensure_indexes(self, embedder: Embedder) -> None:
        """Create the vector index, or rebuild it if its identity has changed.

        Cheap when the index is current: one read of `index_meta`.  A rebuild
        here **drops the vectors** without re-embedding anything, because the
        caller is a save that is embedding one turn — putting the rest back is
        `sync_indexes`, which startup runs before anything is served.
        """
        identity = _vector_identity(embedder)
        with self._connect() as connection:
            if self._meta(connection).get("vector_identity", "") == identity:
                return
            connection.execute("DROP TABLE IF EXISTS turn_vec")
            connection.execute("DELETE FROM turn_chunk")
            connection.execute(_vector_ddl(embedder.dimension))
            self._set_meta(
                connection,
                vector_identity=identity,
                vector_dim=str(embedder.dimension),
            )

    def _sync_text_index(self) -> None:
        """Rebuild the keyword index when the rules that built it have changed.

        **One condition, because the other case cannot happen.**  A row can only
        be missing from this index if the version stamp says the rules match —
        and then it cannot be, since the rows are written in the same
        transaction as the turns they index.  A database with no stamp at all is
        simply the version-mismatch case, and is rebuilt whole; that is why
        there is nothing here for "an older file", and why there is no migration
        to write: a file this build cannot read is named by `index_status`, not
        quietly upgraded.
        """
        with self._connect() as connection:
            if self._meta(connection).get("text_version", "") == TEXT_VERSION:
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
                            _loads(row["messages"], []), row["summary"], row["tags"]
                        ),
                    )
                    for row in rows
                ],
            )
            self._set_meta(connection, text_version=TEXT_VERSION)

    def _turns_without_vectors(self) -> list[tuple[int, str]]:
        """Every turn the vector index does not hold, oldest first.

        The index is derived, so "has no vector" is the whole of the state a
        pending turn has — there is no column to keep in step and nothing to
        mark, and a turn whose embedding failed is simply found here.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT rowid, messages FROM turn "
                "WHERE rowid NOT IN (SELECT DISTINCT turn_id FROM turn_chunk) "
                "ORDER BY rowid"
            ).fetchall()
        return [
            (int(row["rowid"]), embed_text(_loads(row["messages"], []))) for row in rows
        ]

    def index_status(self, embedder: Embedder) -> dict[str, Any]:
        """Whether a search can actually run on both legs, and what is missing.

        **Looked at rather than remembered.**  The question a caller needs
        answered is not "did the sync run" but "is every turn in the index, and
        is it the index this embedder built" — and only counting can answer
        that, because a sync that died halfway leaves nothing to remember it by.
        `slife2.db_server` asks this before it serves and refuses to if the
        answer is not clean: there is no mode in which a turn is stored that
        semantic search cannot find, so an index that is not ready is a system
        that has come apart rather than a component working with less.

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
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) FROM turn WHERE rowid NOT IN"
                    " (SELECT DISTINCT turn_id FROM turn_chunk)"
                ).fetchone()[0]
            )
            meta = self._meta(connection)

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
            # only thing to do about it is the thing `slife2.db`'s docstring
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
    def _meta(connection: sqlite3.Connection) -> dict[str, str]:
        """What each index was built with, as recorded in the file itself."""
        return {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM index_meta")
        }

    @staticmethod
    def _set_meta(connection: sqlite3.Connection, **values: str) -> None:
        connection.executemany(
            "INSERT INTO index_meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(values.items()),
        )

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
            EmptyQuery: If the query holds no term — see `slife2.textindex`.
            InvalidTimeBound: If a bound is in no grammar `slife2.timeutil`
                speaks, for the reason `turns` gives.
        """
        expression = textindex.match_expression(query)
        clauses, params = _time_window(since, until)
        limit = max(1, min(int(limit), MAX_PAGE))
        over = min(limit * _OVERFETCH, _MAX_SQL_VARS)

        ranked = {
            "keyword": await self._off_loop(
                self._keyword_hits, expression, over, clauses, params
            ),
            "semantic": await self._semantic_hits(query, embedder, over),
        }
        fused = [turn_id for turn_id, _ in fuse_ranked(ranked)]
        return await self._off_loop(
            self._records_in_order, fused, clauses, params, limit
        )

    def _keyword_hits(
        self,
        expression: str,
        limit: int,
        clauses: list[str],
        params: list[str],
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
                f" WHERE turn_fts MATCH ? {_where(clauses)}"
                " ORDER BY rank LIMIT ?",
                (expression, *params, limit),
            ).fetchall()
        return [int(row["turn_id"]) for row in rows]

    async def _semantic_hits(self, query: str, embedder: Embedder, k: int) -> list[int]:
        """Turn ids by vector distance, nearest first.

        `k` counts **chunks**, not turns, which is why it is over-fetched: a
        turn with four chunks can occupy four of the k places, and the answer
        wants turns.  Deduplicated here rather than in SQL because a vec0 KNN
        refuses `GROUP BY`.  The window is *not* applied — a KNN cannot be
        constrained by a column it does not have — so `search` filters the fused
        answer instead, and a window narrow enough to exclude the k nearest
        chunks can return fewer turns than it asked for.
        """
        # The **raw** query, not `textindex.normalize(query)`.  Normalization is
        # the keyword leg's rule and it inserts a space between every pair of
        # CJK characters, which is what makes them tokens there — and is
        # nonsense as text handed to a model, where it would ask for a vector of
        # `工 具` rather than of `工具`.  Measured: the normalized query landed
        # nearest the turns sharing *no* word with it.
        vectors = await embedder.embed([query])
        if len(vectors) != 1:
            # Refused rather than answered without the semantic half: there is
            # no degraded mode here, and a search that quietly lost one of its
            # two legs would answer a different question than the one asked.
            raise RuntimeError(
                f"the embedding model answered {len(vectors)} vectors for one "
                f"query, so this search cannot say what a turn was about"
            )
        return await self._off_loop(
            self._nearest_turns, sqlite_vec.serialize_float32(vectors[0]), k
        )

    def _nearest_turns(self, query_vector: bytes, k: int) -> list[int]:
        """The KNN, and the dedup it cannot do itself."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT c.turn_id AS turn_id, v.distance AS distance"
                " FROM turn_vec v JOIN turn_chunk c ON c.id = v.rowid"
                " WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
                (query_vector, k),
            ).fetchall()
        nearest: dict[int, float] = {}
        for row in rows:
            nearest.setdefault(int(row["turn_id"]), float(row["distance"]))
        return list(nearest)

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
        from the answer, never reorder it.
        """
        if not turn_ids:
            return []
        marks = ",".join("?" * len(turn_ids))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT rowid AS turn_id, * FROM turn WHERE rowid IN ({marks})"
                f" {_where(clauses)}",
                (*turn_ids, *params),
            ).fetchall()
        found = {int(row["turn_id"]): _row_to_record(row) for row in rows}
        return [found[turn_id] for turn_id in turn_ids if turn_id in found][:limit]


def _vector_identity(embedder: Embedder) -> str:
    """What the vector index was built with, as one comparable string.

    The text contract is part of it and not only the model, because a vector is
    a function of the text it was made from: changing what goes into one makes
    every stored vector the wrong vector for its turn, which is the same failure
    as changing the model and takes the same rebuild.
    """
    return f"{TEXT_VERSION}|{embedder.identity}|{embedder.dimension}"


def _write_vectors(
    connection: sqlite3.Connection, turn_id: int, vectors: list[list[float]]
) -> None:
    """Place a turn's chunks, one vector each, under ids of their own.

    A row in `turn_vec` is a *chunk*, not a turn: `vec0` has no way to hold
    several vectors in one row, and a long turn averaged into one vector
    describes all of it a little and none of it well.  `turn_chunk` is what
    remembers which turn a chunk came from, and the two are written together —
    a chunk row whose vector is missing would be a turn the index believes it
    holds.
    """
    for index, vector in enumerate(vectors):
        row = connection.execute(
            "INSERT INTO turn_chunk (turn_id, chunk_index) VALUES (?, ?)",
            (turn_id, index),
        )
        connection.execute(
            "INSERT INTO turn_vec (rowid, embedding) VALUES (?, ?)",
            (int(row.lastrowid or 0), sqlite_vec.serialize_float32(vector)),
        )


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
