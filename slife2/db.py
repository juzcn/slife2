"""The db's storage — the turns, and the tool catalogue.

Two things are kept, and they are not the same kind of thing.  **The turns** are
one SQLite file per agent: a conversation's history, which cannot be derived
from anything and would be a loss.  **The tool catalogue** is one file for the
whole data directory: what tools exist, what each one is, and which of them the
model has loaded — derived from servers that can be asked again, except for that
last column, which is the model's own decision.  The first half of this module
is the turns; `ToolStore` and everything after it is the catalogue.

That a second thing worth keeping belongs *here* rather than in a plugin of
its own is what this module's opening sentence always said — and it is not a
guess: the machinery a catalogue needs is the machinery the turns already have,
so sharing it means writing one embedder, one normalization and one vector
index rather than two.  What the two files have in common ends at the file:
they are written by the same process, in different shapes, for different
questions, and `ToolStore` is the second half rather than a variation on the
first.

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
dependency of this plugin rather than something that can be switched off: an
endpoint that cannot be reached fails the save, where an endpoint that silently
degraded would store turns nothing could ever find.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
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


def page_limit(limit: int, cap: int = MAX_PAGE) -> int:
    """The page size a request for `limit` actually gets.

    One function because two parties have to agree on the number: the query
    that builds the page, and the answer that says which page this was.  A
    caller pages by `offset`, so a response echoing the `limit` *asked* for —
    1000, say, for a 200-row cap — is a response that makes the caller's own
    arithmetic skip the rows in between.
    """
    return max(1, min(int(limit), cap))


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

#: How many values one statement may bind, and therefore how long the list a
#: search hands to its final query may be.  SQLite's default limit is 999 and
#: the fused id list costs a bound value per *row*, so the cap belongs on the
#: fused list — two over-fetched legs fuse to twice the length either one had,
#: which is the number that has to stay under this.
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

-- The **live context**: which of this file's turns the conversation is
-- currently made of, in order.  v1 kept the same list in `turn_meta` under
-- `context_turns`, and it is the same idea — a selection over the turn log, not
-- a property of any turn, which is why it is a second table and not a column.
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
    than dropped — `[image]` — for the reason `slife2.gateway.flatten` describes:
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
    stores turns nothing can find, which is the one failure this plugin must
    not have.  The message names both ways it happens — an interpreter built
    without `enable_load_extension`, and a missing wheel.
    """


#: The identity of the text the *vector* index was built from, as opposed to the
#: keyword one.  Composed by the caller (`slife2.embedder`) out of the provider,
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


def _vector_ddl(dimension: int, table: str = "turn_vec") -> str:
    """The vector index's DDL, for one width.

    `distance_metric=cosine` is declared rather than left to the default, and
    the reason is not preference.  It decides whether `1 - distance` is a
    similarity at all: a backend does not have to return unit-norm vectors, so
    on an L2 table that arithmetic yields numbers that are plausible and wrong
    rather than visibly broken.  v1 shipped that bug — distances past 18, every
    strong hit clamped to zero similarity — and the fix was this clause.

    The accepted spelling was checked against the installed extension rather
    than its documentation, which describes itself as a work in progress.

    `table` is a parameter because there are two indexes now — a turn's vectors
    and the tool catalogue's — and one of the two spellings of "cosine, this
    width" is all this needs to be.
    """
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
        f"embedding float[{int(dimension)}] distance_metric=cosine)"
    )


def _load_vector_index(connection: sqlite3.Connection) -> None:
    """Load the extension into one connection, or say why nothing can work.

    Raises:
        VectorIndexUnavailable: If this interpreter cannot load extensions
            (Apple's and python.org's builds both ship that way) or the wheel is
            not importable.  Named here rather than left to surface as an
            `OperationalError` about a missing function, because the two causes
            need different things done about them.
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


def _meta(connection: sqlite3.Connection, table: str = "index_meta") -> dict[str, str]:
    """What each index was built with, as recorded in the file itself.

    `table` is a parameter because the catalogue has a `meta` table of its own:
    the two files record different things (a turn's text rules and a tool's) and
    the shape of the record is all this helper has ever been.
    """
    return {
        str(row["key"]): str(row["value"])
        for row in connection.execute(f"SELECT key, value FROM {table}")
    }


def _set_meta(
    connection: sqlite3.Connection, table: str = "index_meta", **values: str
) -> None:
    connection.executemany(
        f"INSERT INTO {table} (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(values.items()),
    )


def _connect_to(path: Path) -> sqlite3.Connection:
    """One connection to one database file, with this module's pragmas.

    A connection per call rather than one held open.  SQLite connections are
    cheap and are not shareable across threads, and the callers — MCP tools and
    the hub — run on an event loop where blocking it even briefly is worth
    avoiding; a fresh connection per call means the stores need no locking of
    their own.

    Write-ahead logging, so a read while a write is in flight sees the last
    committed state instead of waiting for the writer, and a `busy_timeout`, so
    the short load/evict writes back off instead of failing with
    `SQLITE_BUSY` — which matters more here than for the turns, because two of
    these files are written by different processes.

    The vector index arrives as a loadable extension, and an extension is loaded
    *into a connection* — so this, which opens one per call, loads it every
    time.  Measured at about half a microsecond, which is why it happens here
    unconditionally: a second connection path used only where a vector is
    touched would be a second thing to keep in step, and would buy nothing.
    """
    connection = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    _load_vector_index(connection)
    return connection


class TurnStore:
    """Reads and writes one agent's turns, and the two indexes over them.

    A connection per call rather than one held open — see `_connect`, which is
    the one place that is decided for both stores in this module.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return _connect_to(self.path)

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
            # **The live-context list is appended here, in this transaction.**
            # v1's rule, and the reason is the one the three writes above already
            # answer to: a turn that is stored but not in the list is a turn the
            # next restore silently drops, and the window in which that is true
            # is exactly the window between two transactions.  One transaction
            # means there is no such window — a process that dies mid-save
            # leaves either both or neither.
            append_context(connection, turn_id)
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
        limit = page_limit(limit)
        offset = max(0, int(offset))
        clauses, params = _time_window(since, until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
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

    def set_context_turns(self, turn_ids: Sequence[int]) -> list[int]:
        """Replace the live-context list; answers with what was written.

        The caller is the party that decided, and `[]` is one of the things it
        may decide — see `write_context` for why this store does not refuse an
        empty list the way v1's does.
        """
        with self._connect() as connection:
            return write_context(connection, turn_ids)

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
        run over one absent id would turn a partial loss into a total one.

        Chunked, because the list can be longer than one statement may bind
        (`_MAX_SQL_VARS`) and an unbounded `IN (…)` is a query that works in
        testing and raises in a long session.
        """
        wanted = normalise_ids(turn_ids)
        found: dict[int, TurnRecord] = {}
        with self._connect() as connection:
            for start in range(0, len(wanted), _MAX_SQL_VARS):
                chunk = wanted[start : start + _MAX_SQL_VARS]
                marks = ",".join("?" * len(chunk))
                rows = connection.execute(
                    f"SELECT rowid AS turn_id, * FROM turn WHERE rowid IN ({marks})",
                    chunk,
                ).fetchall()
                for row in rows:
                    found[int(row["turn_id"])] = _row_to_record(row)
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
            for start in range(0, len(turn_ids), _MAX_SQL_VARS):
                chunk = turn_ids[start : start + _MAX_SQL_VARS]
                marks = ",".join("?" * len(chunk))
                rows = connection.execute(
                    f"SELECT rowid AS turn_id FROM turn WHERE rowid IN ({marks})"
                    f" {_where(clauses)}",
                    (*chunk, *params),
                ).fetchall()
                allowed |= {int(row["turn_id"]) for row in rows}
        return allowed

    def _time_ranked(
        self, since: str | None, until: str | None, limit: int, newest_first: bool
    ) -> list[tuple[int, float | None]]:
        """Turn ids in a window, from the end `anchor` named, with no similarity.

        `None` and not zero: there is no query, so nothing was measured, and
        "no number" is a different fact from "measured as zero" — which is what
        lets the caller exempt these from a similarity floor rather than drop
        them all (see `recall`).
        """
        clauses, params = _time_window(since, until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "DESC" if newest_first else "ASC"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT rowid AS turn_id FROM turn {where}"
                f" ORDER BY rowid {order} LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [(int(row["turn_id"]), None) for row in rows]

    async def recall(
        self,
        *,
        query: str = "",
        embedder: Embedder | None = None,
        since: str | None = None,
        until: str | None = None,
        anchor: str | None = None,
        limit: int = 20,
    ) -> list[tuple[int, float | None]]:
        """Turns a rebuild may add, best first — the *ranking*, and no policy.

        One of the store's three shapes, chosen by what the caller asked for:

        * **a query** — the same hybrid as `search`, windowed, ranked by
          relevance.  `anchor` is not consulted here; relevance decides which end
          the caller spends from, and the caller is where that is logged.
        * **no query, a window or an anchor** — ranked by *time* instead, from the
          end `anchor` named (`newest` when it says nothing).  This branch has to
          run before the hybrid one, and not for symmetry: an empty query reaches
          the full-text index as a syntax error and embeds to noise.
        * **neither** — nothing, which is not an error: a recall with no
          condition is a caller that has not decided what to look for.

        **What is returned is ranked pairs, not a selection.**  The count, the
        similarity floor and the token budget are the *caller's* — they depend on
        a model's window and on what the decision kept, neither of which this
        store knows — and the split is v1's: the store ranks, the policy caps.

        The similarity is `None` for a turn only the keyword leg found, and that
        it is `None` rather than `0.0` is load-bearing: a fused score is a
        function of rank position and carries no magnitude to threshold, so a
        floor can only gate the *measured* similarity — and an exact keyword hit
        has not been measured against anything.  An exact match is a stronger
        signal than a cosine neighbourhood, and "no number" is not evidence
        against it.

        Raises:
            EmptyQuery: If the query holds no term.
            InvalidTimeBound: If a bound is in no grammar `slife2.timeutil`
                speaks, for the reason `turns` gives.
        """
        limit = page_limit(limit)
        if not query.strip():
            if not since and not until and not anchor:
                return []
            return await self._off_loop(
                self._time_ranked, since, until, limit, anchor != "oldest"
            )

        if embedder is None:
            raise ValueError(
                "a recall with a query has to be ranked by meaning, and no "
                "embedder was given"
            )
        expression = textindex.match_expression(query)
        clauses, params = _time_window(since, until)
        over = min(limit * _OVERFETCH, _MAX_SQL_VARS)
        # The keyword leg is windowed in SQL (its `rank` orders every match, so
        # limiting after the window is still the best inside it); the semantic
        # leg is not, and the fusion is filtered below.
        keyword = await self._off_loop(
            self._keyword_hits, expression, over, clauses, params
        )
        semantic = await self._semantic_hits(query, embedder, over)
        fused = [
            turn_id
            for turn_id, _ in fuse_ranked(
                {"keyword": keyword, "semantic": list(semantic)}
            )
        ][:_MAX_SQL_VARS]
        allowed = await self._off_loop(self._ids_in_window, fused, clauses, params)
        return [
            (turn_id, semantic.get(turn_id))
            for turn_id in fused
            if turn_id in allowed
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
            if (
                self._meta(connection).get("text_version", "")
                == textindex.RULES_VERSION
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
                            _loads(row["messages"], []), row["summary"], row["tags"]
                        ),
                    )
                    for row in rows
                ],
            )
            self._set_meta(connection, text_version=textindex.RULES_VERSION)

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
        Both halves of the plugin that owns this store ask it before they serve
        and refuse to if the answer is not clean: there is no mode in which a
        turn is stored that
        semantic search cannot find, so an index that is not ready is a system
        that has come apart rather than a plugin working with less.

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
        return _meta(connection)

    @staticmethod
    def _set_meta(connection: sqlite3.Connection, **values: str) -> None:
        _set_meta(connection, **values)

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
        limit = page_limit(limit)
        over = min(limit * _OVERFETCH, _MAX_SQL_VARS)

        ranked = {
            "keyword": await self._off_loop(
                self._keyword_hits, expression, over, clauses, params
            ),
            # `list(...)` of the mapping, which is in nearest-first order: the
            # fusion wants a *ranking*, and the similarities the same call
            # measured are what `recall` gates on.
            "semantic": list(await self._semantic_hits(query, embedder, over)),
        }
        # Capped here and not on the legs: fusing takes the *union* of two lists
        # that are each already `over` long, and the next query binds one value
        # per id.  A cap applied to each leg would leave the union at twice it.
        fused = [turn_id for turn_id, _ in fuse_ranked(ranked)][:_MAX_SQL_VARS]
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

    async def _semantic_hits(
        self, query: str, embedder: Embedder, k: int
    ) -> dict[int, float]:
        """Turn ids by vector distance, nearest first.

        `k` counts **chunks**, not turns, which is why it is over-fetched: a
        turn with four chunks can occupy four of the k places, and the answer
        wants turns.  Deduplicated here rather than in SQL because a vec0 KNN
        refuses `GROUP BY`.  The window is *not* applied — a KNN cannot be
        constrained by a column it does not have — so `search` filters the fused
        answer instead, and a window narrow enough to exclude the k nearest
        chunks can return fewer turns than it asked for.

        **A mapping, in nearest-first order, and the value is a similarity.**
        The order is what `search` fuses; the value is what `recall` gates on —
        and it is a *measured* number, unlike the fused score, which is a
        function of rank position and carries no magnitude to threshold
        (`recall`'s docstring is where that distinction is argued).
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
        return dict(
            await self._off_loop(
                self._nearest_turns, sqlite_vec.serialize_float32(vectors[0]), k
            )
        )

    def _nearest_turns(self, query_vector: bytes, k: int) -> list[tuple[int, float]]:
        """The KNN, the dedup it cannot do itself, and the similarity.

        **The *best* chunk of a turn stands for the turn**, which is what the
        `setdefault` is: a turn's chunks are ranked separately, so the first one
        seen for a turn is its nearest, and a later, worse chunk must not
        overwrite it.

        `1 - distance` is a similarity only because the DDL says `cosine` — on an
        L2 table that arithmetic yields numbers that are plausible and wrong
        rather than visibly broken, which is what `_vector_ddl` exists to
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
            nearest.setdefault(int(row["turn_id"]), 1.0 - float(row["distance"]))
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
    return f"{textindex.RULES_VERSION}|{embedder.identity}|{embedder.dimension}"


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


# --- the live context ---------------------------------------------------------


def normalise_ids(turn_ids: Iterable[int]) -> list[int]:
    """An id list with duplicates collapsed to their **first** position.

    First and not last, which is the choice v1 made and the only one that keeps
    the list's meaning: the order *is* the content (see the `context` DDL), so a
    later mention of an id already in the list is a repeat rather than a
    correction — nothing in a second mention says where in time it belongs.

    The ints are coerced here rather than at each call site because a list that
    arrives over a wire as JSON is a list of whatever the encoder wrote, and a
    stringly `"7"` that reaches a `rowid IN (…)` query matches nothing — so the
    failure would be a silently short context rather than an error.
    """
    seen: dict[int, None] = {}
    for turn_id in turn_ids:
        seen.setdefault(int(turn_id), None)
    return list(seen)


def read_context(connection: sqlite3.Connection) -> list[int]:
    """The persisted live-context list, in the order it was written."""
    row = connection.execute("SELECT turn_ids FROM context WHERE id = 1").fetchone()
    if row is None:
        return []
    return normalise_ids(_loads(row["turn_ids"], []))


def write_context(connection: sqlite3.Connection, turn_ids: Sequence[int]) -> list[int]:
    """Replace the live-context list, and answer with what was written.

    **One operation, not four.**  v1 has `set`, `drop`, `clear` and a read-modify
    -write on the save path because its list is edited from four places that each
    know one thing; here every editor already holds the whole list it wants next
    (a rebuild has the selection, a trim has the survivors, a reset has nothing),
    so the operation they all want is "this is the list now" and the other three
    would be spellings of it.  The save's *append* is the one exception, and it
    lives in `append_context` where the transaction is.

    **An empty list is a legitimate list**, which is the half v1's `set` refuses:
    there, a guard protects a *partial* selection from being mistaken for a
    deliberate one.  Here the caller has already made the decision — "none of
    them" is one of the three things `context` can say — and a store that
    second-guessed it would turn the one explicit clear into an error.
    """
    written = normalise_ids(turn_ids)
    connection.execute(
        "INSERT INTO context (id, turn_ids) VALUES (1, ?)"
        " ON CONFLICT(id) DO UPDATE SET turn_ids = excluded.turn_ids",
        (json.dumps(written),),
    )
    return written


def append_context(connection: sqlite3.Connection, turn_id: int) -> list[int]:
    """Add one turn to the end of the live-context list, read inside the write.

    Read-modify-write inside the caller's transaction rather than by the caller,
    because the two halves have to be one statement's worth of atomic: a caller
    that read the list, and then wrote it back, would lose whatever a second
    writer appended in between — and the second writer is the ordinary case, not
    a race, since a queued turn's save can land while the next turn is being
    built.
    """
    return write_context(connection, [*read_context(connection), int(turn_id)])


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


# ══════════════════════════════════════════════════════════════════════════════
#  The tool catalogue — v1's `tools.db`
#
#  One row per tool the model may be given, with two indexes over the rows: a
#  keyword one (`tool_fts`) and a semantic one (`tool_vec`).  It lives in this
#  module rather than in a plugin of its own because that is what this
#  module's opening paragraph already said the second thing worth keeping would
#  be: rows and two indexes over them, with the embedder, the normalization and
#  the vector index already here.
#
#  **One file for the whole data directory, not one per agent.**  `--agent`
#  partitions the turns and nothing else: one hub serves every conversation and
#  cannot tell them apart, so which tools are installed — and which of them the
#  model has loaded — is a property of the machine.
#
#  **The hub opens this file, and holds nothing else of it.**  The catalogue
#  used to be served over MCP by a plugin of its own; it is an in-process
#  `ToolStore` now (`slife2.toolhub.Catalogue`), because a store has one writer
#  and one writer is what a SQLite lock already is (DESIGN.md §1.1).  The split
#  that survives is the one that matters: which tools exist is the hub's
#  decision, what is known about them is this file's record.
# ══════════════════════════════════════════════════════════════════════════════

#: The categories whose owner is a *server* — and therefore the only rows with a
#: load state and a connectivity verdict.  This set is the whole of what "is
#: this a function tool?" means, so there is no second column to keep in sync
#: with it: v1 dropped its derived `type` column for exactly this reason.
#:
#: `plugin` is ours, and it is the only one of the three that is: a plugin is a
#: server slife2 starts and therefore one that carries the plugin contract
#: (`slife2.mcp_server`), while `mcp` and `rest` are somebody else's process,
#: reached as a client.  The word is chosen for what it keeps apart in a
#: sentence — a plugin *is* an MCP server, so calling ours "MCP servers" would
#: distinguish them from the twenty under `tools:` not at all — and the
#: difference it names is load-bearing: `required` on the hub's connection, a
#: missing one failing the turn, and tools that start loaded (DESIGN.md §8).
PLUGIN = "plugin"

FUNCTION_CATEGORIES = frozenset({PLUGIN, "mcp", "rest"})

#: The two categories **nothing is connected to**, and the two the model cannot
#: load.  A skill is a document, read by `slife2-skills`; a `cli:` entry is a
#: program already on this machine, declared by `slife2-cli`.  Neither has a
#: connection that could be down and neither has a load state to have — and both
#: are rows all the same, because that is how `tool_search` reaches them: a
#: playbook nobody catalogued is a playbook found only by a model that already
#: knew its name (`skill_use` reads one; a row is what makes it findable).
SKILL = "skill"
CLI = "cli"

#: Everything the code can write, which is what the live table's `CHECK` must
#: accept.  v1 also had `job`, and a `plugin` that was a package of somebody's
#: own code rather than a server; nothing writes `job` here yet, and adding one
#: later is free: the DDL is checked at open and a file that is not this build's
#: is rebuilt rather than migrated — which is what adding `cli` did.
CATEGORIES = FUNCTION_CATEGORIES | {SKILL, CLI}

#: `tool.status` — the row's whole closed domain, and three MUTUALLY EXCLUSIVE
#: values: a row is in exactly one of them, so an `error` row is never also an
#: enabled one.  `disabled` is the *config's* answer (the `enabled: false`
#: switch), `error` is the *runtime's* verdict (whose owner is unusable right
#: now), and `enabled` is everything else.  Off is not down: a server switched
#: off while it was failing is `disabled`, and the two are different things to
#: do something about.
STATUS_ENABLED = "enabled"
STATUS_DISABLED = "disabled"
STATUS_ERROR = "error"

#: The column's whole domain as a set, for the two places that ask membership
#: rather than compare: the DDL check read at open, and a merged row that
#: carries its own verdict (`_plan`).
STATUSES = frozenset({STATUS_ENABLED, STATUS_DISABLED, STATUS_ERROR})

#: `tool.load_status` — what the *model* decided, and the one thing in this file
#: that is not derived from anything: every other column can be rebuilt by
#: asking the servers again, and this one cannot.  `'n/a'` for a skill, which
#: has no load concept.
LOADED = "loaded"
UNLOADED = "unloaded"

#: The stored spelling of "not applicable" — the same literal for every column
#: that would otherwise be NULL: no owner, no schema, no load state.  One value
#: per absence rather than NULL, so that no read point needs an `IS NULL` branch
#: and a forgotten one cannot silently return an empty set.
NA = "n/a"

#: The `_`-prefixed names the model *does* get, and there is exactly one.
#:
#: A name in this set is injected, callable, and evictable only in the sense
#: that nothing of ours ever is — see `_injectable_sql` for the argument and
#: `slife2.toolhub.FUNC_TOOL_UNLOAD` for the tool.  Spelled here rather than
#: imported for the reason `PLUGIN` is spelled in two modules: the hub must not
#: be importable from the catalogue's half, and the db must not import a server.
#: The two spellings are the same fact — the name of one tool — which is why
#: this set has one member and is not a pattern.
MODEL_VISIBLE_HARNESS_TOOLS = frozenset({"_func_tool_unload"})

#: The columns the code reads or writes on `tool`, checked at open in BOTH
#: directions: a missing column cannot answer a query, and an unknown one is a
#: leftover from a schema this code no longer writes.
_TOOL_COLUMNS = frozenset(
    {
        "name",
        "description",
        "category",
        "source_id",
        "remote_name",
        "schema",
        "status",
        "load_status",
        "last_loaded",
        "last_used",
    }
)

#: The `category` values a live `tool` DDL accepts, parsed rather than
#: substring-matched: `'skill'` is an ordinary word another clause could carry.
#: The columns of `tool` whose live DDL is a closed domain the code writes into,
#: and the whole set of values this build writes into each.  Read at open: a file
#: whose DDL accepts fewer values than this build uses passes a column check and
#: then raises `IntegrityError` at the first write — inside a tool call, where
#: the hub reads it as the db refusing one caller's data and carries on.
_CHECKED_DOMAINS: dict[str, frozenset[str] | set[str]] = {
    "category": CATEGORIES,
    "status": STATUSES,
    "load_status": {LOADED, UNLOADED, NA},
}


def _check_values(ddl: str, column: str) -> set[str] | None:
    """The values a live `tool` DDL accepts for one CHECKed column, or `None`.

    Scoped to that column's own clause rather than the whole statement on
    purpose: `'skill'` and `'rest'` are ordinary words another clause could
    carry, and a substring test would call a list complete when it was not.
    """
    found = re.search(
        rf"check\s*\(\s*{re.escape(column)}\s+in\s*\(([^)]*)\)",
        ddl,
        re.IGNORECASE,
    )
    if found is None:
        return None
    return {
        value.strip().strip("'\"")
        for value in found.group(1).split(",")
        if value.strip()
    }


#: The version of what a tool row is *found* by and *about* is
#: `slife2.textindex.RULES_VERSION` — the same one the turns use, because it is
#: the same `normalize`/`terms` that produce both.  A second constant here would
#: be the second lever this module keeps saying it does not have.
#:
#: The `bm25` column weights, in `tool_fts`'s column order.  A tool is found by
#: its name far more often than by anything else about it — "the calculator" and
#: `calc` have to meet — so the name dominates and the schema, which is
#: long and full of punctuation, counts for least.  v1's weights.
BM25_WEIGHTS = (5.0, 2.0, 1.0, 1.0, 0.5)

#: How many rows a tool search may return.  A cap rather than a preference:
#: `limit` arrives from a model, and a model that asks for the whole catalogue
#: should get a page and be told how many there are.
MAX_TOOL_PAGE = 200

#: The category list the DDL's `CHECK` is built from, so the two cannot drift:
#: the constant is the one statement of which categories exist, and the live
#: table is compared against it at open.
_CATEGORY_CHECK = ",".join(f"'{name}'" for name in sorted(CATEGORIES))

_TOOL_SCHEMA = f"""
-- One row is one tool, and `category` is the whole of what it is: there is no
-- derived `type` column, because every question one would answer ("does this
-- row have a load state?") is a membership test over FUNCTION_CATEGORIES.
--
-- Every column carries a default and none is nullable: "local", "no schema" and
-- "no load state" are real values ('n/a'), so no reader needs an IS NULL arm.
--
-- Two stamps, and they answer two different questions.  `last_loaded` is when
-- the row entered the model's list — the operator's `autoload`, or the model's
-- own `func_tool_load`.  `last_used` is when the model last *called* it, written
-- by the hub after every routed call.  The budget evicts by whichever of the two
-- is newer (`MAX`), so a tool that is being called outlives one that was merely
-- loaded later, and a tool loaded a moment ago and not yet used is not the first
-- thing to go.  `''` means "never", and sorts below every timestamp.
CREATE TABLE IF NOT EXISTS tool (
    name        TEXT PRIMARY KEY NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL CHECK (category IN ({_CATEGORY_CHECK})),
    source_id   TEXT NOT NULL DEFAULT 'n/a',
    remote_name TEXT NOT NULL DEFAULT 'n/a',
    schema      TEXT NOT NULL DEFAULT 'n/a',
    status      TEXT NOT NULL DEFAULT 'enabled'
                CHECK (status IN ('enabled','disabled','error')),
    load_status TEXT NOT NULL DEFAULT 'unloaded'
                CHECK (load_status IN ('loaded','unloaded','n/a')),
    last_loaded TEXT NOT NULL DEFAULT '',
    last_used   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_tool_status ON tool(status);
CREATE INDEX IF NOT EXISTS idx_tool_source ON tool(source_id);
CREATE INDEX IF NOT EXISTS idx_tool_category ON tool(category);
CREATE INDEX IF NOT EXISTS idx_tool_load ON tool(load_status);

-- The keyword leg.  Not `content='tool'`: the text an FTS5 table has to hold is
-- *normalized* — a space between every pair of CJK characters, because that is
-- what makes each of them a token (`slife2.textindex`) — and normalization is
-- not a column value, so there is nothing for an external-content index to
-- point at.  The row is written by this module, in the same transaction as the
-- row it indexes.
CREATE VIRTUAL TABLE IF NOT EXISTS tool_fts USING fts5(
    name, description, category, source_id, schema,
    tokenize='unicode61 remove_diacritics 2'
);

-- Which tool a vector belongs to.  A row in `tool_vec` is a *chunk*, not a tool
-- — `vec0` holds one vector per row, and a long schema embedded whole would
-- describe all of it a little and none of it well — so this is what turns a hit
-- back into a name, and what a rebuild deletes by.
CREATE TABLE IF NOT EXISTS tool_chunk (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    chunk_index INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_chunk_name ON tool_chunk(name);

-- What the two indexes were built with, so that a changed embedding model, width
-- or normalization rule is a rebuild rather than a migration.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: The columns a search result carries, in the order it carries them.  `schema`
#: is reported as a *size* rather than as text: it is the tool's whole parameter
#: JSON, a model reading a result has no use for it, and what it does tell a
#: reader is whether the tool declares anything at all.  v1's choice.
_SEARCH_COLUMNS = (
    "name",
    "description",
    "category",
    "source_id",
    "status",
    "load_status",
)

#: The five columns the keyword index holds, in `BM25_WEIGHTS`'s order.
_FTS_FIELDS = ("name", "description", "category", "source_id", "schema")

#: How many candidates each leg of a search brings to the fusion, as a multiple
#: of what was asked for.  A fusion is a vote between ranked lists, so the lists
#: want to be deeper than the answer; and one leg needs the slack for a second
#: reason, since a KNN counts *chunks* and several of them belong to one tool.
_TOOL_OVERFETCH = 4


def _in_list(values: Any) -> str:
    """A SQL `IN` list of quoted literals, from a set of constants."""
    return ",".join(f"'{value}'" for value in sorted(values))


def _marks(values: Any) -> str:
    """A SQL `IN` list of placeholders, for values that are bound."""
    return ",".join("?" * len(list(values)))


def _indexable(value: Any) -> str:
    """One column's contribution to the keyword document.

    **A sentinel is a value in the column, not a word in the document.**  `'n/a'`
    means "this tool has no schema" — indexing it would make a search for `n/a`
    match every tool that declares nothing, and the row would be found by text
    that was never about it.
    """
    text = str(value or "")
    return "" if text == NA else text


def tool_document(row: Mapping[str, Any]) -> str:
    """The text a tool is *about*, which is what its vector is a vector of.

    Name, description and the parameter schema, and nothing else: those are the
    three things a model chooses a tool by, and the rest of the row is
    bookkeeping (`source_id`, `status`) that describes the record rather than the
    tool.  Through `_storable` for the reason the turn's text is: this string
    goes out as JSON on its way to an embedding model, and a lone surrogate
    cannot be encoded.

    **Every row has one, so every row has a vector.**  v1 asked whether the
    *schema* was worth embedding, because in v1 the column held the whole tool
    definition; here it holds only the parameters, and a tool with none is
    exactly the kind a model looks for by what it does — "open a page and take a
    screenshot" is a description, not an argument list.  A name is never empty,
    so the document never is either.
    """
    parts = [str(row.get("name") or ""), str(row.get("description") or "").strip()]
    schema = _indexable(row.get("schema"))
    if schema.strip():
        parts.append(schema)
    return _storable("\n\n".join(part for part in parts if part))


def _tool_dict(row: sqlite3.Row) -> dict[str, Any]:
    """One stored row, as the callers of this module read it."""
    return {
        "name": str(row["name"]),
        "description": str(row["description"]),
        "category": str(row["category"]),
        "source_id": str(row["source_id"]),
        "remote_name": str(row["remote_name"]),
        "schema": str(row["schema"]),
        "status": str(row["status"]),
        "load_status": str(row["load_status"]),
    }


def _search_dict(row: sqlite3.Row, similarity: float | None = None) -> dict[str, Any]:
    """One search result: what a chooser needs, and no more.

    The similarity is present only when the semantic leg produced one — a
    keyword hit has no distance to report, and a `0.0` invented for it would
    read as "found by meaning, and it is a bad match".
    """
    found: dict[str, Any] = {column: str(row[column]) for column in _SEARCH_COLUMNS}
    schema = str(row["schema"] or "")
    found["schema_bytes"] = 0 if schema in ("", NA) else len(schema)
    if similarity is not None:
        found["similarity"] = similarity
    return found


class ToolStore:
    """The tool catalogue: what the model may call, and what it has loaded.

    The hub writes and reads it, over MCP, and this is the half that remembers —
    see the section banner above for why the two are separate.

    **Nothing here is per-conversation.**  One file, and one loaded set in it:
    the hub serves every conversation and cannot tell them apart, so what is
    installed and what has been loaded belongs to the data directory.

    **A row goes in with its search text, and its vector right after.**  The
    keyword row is written in the same transaction as the row it indexes, so the
    two cannot disagree; the vector needs a call to a model, which belongs on
    the event loop and not inside a transaction, so a row can be left without
    one for as long as that call takes.  It is not a hole: a vector is derived
    from the row, and `sync_indexes` puts back whatever a crash lost — where a
    *turn* with no vector would be a turn nothing could ever find, which is why
    that store embeds before it writes and refuses to store otherwise.
    """

    def __init__(
        self,
        path: Path,
        *,
        threshold: int,
        known: Iterable[str] = (),
    ) -> None:
        #: The most function tools the model may hold — see
        #: `slife2.config.ToolLoadSettings`.  Held here rather than passed per
        #: call because the count it bounds is a `SELECT COUNT(*)` over these
        #: rows: the budget and the rows it applies to live in one place.
        self.threshold = max(1, int(threshold))
        #: Every source this process can name at all, which is what the boot
        #: pass needs to tell a stale row from a slow one: see `reset`.  It is
        #: the *peers* — the plugins slife2 starts — and nothing else, because
        #: every other source is held by one of them and named over the wire:
        #: see `merge`'s `autoload` and `evict`'s, which is where the other two
        #: facts the config used to supply now arrive.
        self.known = frozenset(str(name) for name in known)
        self.path = path
        self._ensure_schema()
        # Built means a process has just started, and a verdict this file
        # carries from an older run may be one nothing can justify any more —
        # `reset` is what withdraws the ones the config can speak to.
        self.reset()

    def _connect(self) -> sqlite3.Connection:
        return _connect_to(self.path)

    # --- the file -------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create the tables, on a file that is not this build's.

        **No migration layer, for the reason `db.py` gives for the turns**, and
        one thing more: the only column here that is not derived from a server
        is `load_status`, so a stale file costs the model its loaded set — one
        `tool_search` and a `tool_func_load` each to get back — where an upgrade
        in place would be a permanent second way to build these tables.  v1
        reported the stale file and asked a person to delete it; this deletes
        and rebuilds, because a rebuild is a statement this layer can run
        itself, and the report is a log line naming the file.
        """
        with self._connect() as connection:
            self._rebuild_if_stale(connection)
            connection.executescript(_TOOL_SCHEMA)

    def _rebuild_if_stale(self, connection: sqlite3.Connection) -> None:
        """Empty a `tool` table that is not the one this code writes.

        Checked against the DDL rather than a version number, because the DDL is
        what actually decides whether an INSERT is accepted: `CREATE TABLE IF
        NOT EXISTS` never touches an existing table, so a file from another
        build keeps its own columns and its own `CHECK` for as long as it lives.
        Both directions are checked — a missing column cannot answer a query and
        an unknown one is a leftover nothing maintains.
        """
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(tool)")
        }
        if not columns:  # no table yet: the schema creates it complete
            return
        ddl = str(_table_ddl(connection, "tool") or "")
        missing = sorted(_TOOL_COLUMNS - columns)
        unexpected = sorted(columns - _TOOL_COLUMNS)
        short = [
            f"no {column} value {'/'.join(sorted(wanted - allowed))}"
            for column, wanted in _CHECKED_DOMAINS.items()
            if (allowed := _check_values(ddl, column)) is not None and wanted - allowed
        ]
        if not missing and not unexpected and not short:
            return
        logger.warning(
            "%s holds a tool catalogue this build does not write (%s); it is "
            "rebuilt from the servers, which costs the model whatever it had "
            "loaded",
            self.path,
            "/".join(
                [
                    *(f"no {name} column" for name in missing),
                    *(f"unknown {name} column" for name in unexpected),
                    *short,
                ]
            ),
        )
        for table in ("tool_fts", "tool", "tool_chunk", "tool_vec", "meta"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")

    # --- what the hub writes --------------------------------------------------

    async def merge(
        self,
        source: str,
        category: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        embedder: Embedder,
        autoload: bool = False,
    ) -> dict[str, Any]:
        """Merge one source's whole tool list into the catalogue.

        **Four outcomes and no fifth**: a name that is not here is *added*, one
        the source dropped is *deleted*, one whose columns changed is *updated*,
        and one already identical is *left alone*.  A steady state — the hub
        asks before every model call — therefore touches no row at all, which
        matters because the keyword document is rewritten with every write.

        **`name` is the row's identity, and two sources cannot own one.**  That
        is the primary key's rule and not a convention: a name is what the model
        calls, what a vector belongs to, and what the merge matches on, so an
        incoming name another source already owns is refused rather than written
        over — silently replacing somebody's tool is how a model calls `search`
        and reaches a different server than the one it read about.

        **The load state is the model's, and a merge never touches it.**  A new
        row gets the seed this module decides (`_seed`); a row that exists keeps
        whatever it says, so a server relisting its tools cannot unload one the
        model is holding.  `status` is the other half of that: a source that
        answers has usable tools, so rows that were `error` go back to `enabled`
        — that, and not the load state, is what a merge may move.

        **Rows and vectors are written together, in one transaction.**  The
        embedding is a call to a model, so it happens between the two halves —
        planned, embedded, then applied — and a merge that cannot embed stores
        nothing rather than storing rows the semantic leg could never find.  That
        is the turn store's arrangement, for the turn store's reason.

        Returns `{inserted, updated, reconnected, purged, skipped}`.

        Raises:
            ValueError: If `category` is not one of `CATEGORIES`, or if a name
                belongs to another source.  Both are facts about the caller's
                input, and the second is a naming problem the caller can fix —
                so it fails this source's list rather than the whole catalogue.
        """
        plan = await asyncio.to_thread(self._plan, source, category, rows, autoload)
        # Before anything is written: a vector needs a table, the table needs a
        # width, and only the embedder knows the width.  One read of `meta` once
        # it is there.
        await asyncio.to_thread(self.ensure_index, embedder)
        vectors = await self._embed(plan["documents"], embedder)
        applied = await asyncio.to_thread(self._apply, plan, vectors)
        logger.info(
            "%s: %d tool(s) — %d added, %d changed, %d deleted, %d unchanged",
            source,
            len(plan["incoming"]),
            len(plan["inserts"]),
            len(plan["updates"]),
            len(plan["purged"]),
            plan["skipped"],
        )
        return applied

    def _plan(
        self,
        source: str,
        category: str,
        rows: Sequence[Mapping[str, Any]],
        autoload: bool = False,
    ) -> dict[str, Any]:
        """The difference between what a source offers and what is stored.

        Read-only, and separate from the write for the reason the write is
        separate from the read: what has to be embedded is known only after the
        comparison, and the embedding belongs on the event loop with the notes
        of the file made before it.  Nothing is written until all of it is
        known, so a refused merge leaves the catalogue exactly as it was.
        """
        if category not in CATEGORIES:
            raise ValueError(
                f"{category!r} is not a tool category; known: "
                + ", ".join(sorted(CATEGORIES))
            )
        inserts: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        reconnected: list[str] = []
        documents: list[tuple[str, str]] = []
        skipped = 0
        incoming: set[str] = set()

        with self._connect() as connection:
            existing = {
                str(row["name"]): _tool_dict(row)
                for row in connection.execute(
                    "SELECT * FROM tool WHERE source_id = ?", (source,)
                )
            }
            owned_elsewhere = {
                str(row["name"]): str(row["source_id"])
                for row in connection.execute(
                    "SELECT name, source_id FROM tool WHERE source_id != ?", (source,)
                )
            }
            for row in rows:
                name = str(row.get("name") or "")
                if not name:  # a tool with no name is one nothing could call
                    skipped += 1
                    continue
                if name in owned_elsewhere:
                    raise ValueError(
                        f"{name!r} is already {owned_elsewhere[name]}'s tool; a name "
                        f"is a row's identity, so two sources cannot offer one "
                        f"(rename the server or the tool)"
                    )
                if name in incoming:  # one list naming one tool twice
                    logger.warning(
                        "%s listed %s twice in one list; keeping the first",
                        source,
                        name,
                    )
                    skipped += 1
                    continue
                incoming.add(name)
                fields = {
                    "name": name,
                    "description": str(row.get("description") or ""),
                    "category": category,
                    "source_id": source,
                    "remote_name": str(row.get("remote_name") or name),
                    "schema": str(row.get("schema") or NA) or NA,
                }
                # **A row may carry its own verdict.**  Every other row's status
                # is the *runtime's* — `source_state` is the only writer, because
                # whether a server is up is not something a config can say.  The
                # two document families are the exception and the reason is the
                # same fact twice: there is no connection to have a state, so
                # the mirror is the authority.  A `cli:` entry that is switched
                # off mirrors `disabled`; a `SKILL.md` that cannot be read
                # mirrors `error`.
                verdict = str(row.get("status") or "")
                own_verdict = verdict in STATUSES
                if own_verdict:
                    fields["status"] = verdict
                previous = existing.get(name)
                if previous is None:
                    load_status = self._seed(category, autoload)
                    entry = {
                        **fields,
                        "status": fields.get("status", STATUS_ENABLED),
                        "load_status": load_status,
                        # The list's stamp, and only that one: a row nobody has
                        # called yet has no call to record, and `''` is what
                        # says so — see `touch`.
                        "last_loaded": now() if load_status == LOADED else "",
                        "last_used": "",
                    }
                    inserts.append(entry)
                    documents.append((name, tool_document(entry)))
                    continue

                moved = {
                    column: value
                    for column, value in fields.items()
                    if previous[column] != value
                }
                if moved:
                    updates.append({**previous, **fields, "moved": sorted(moved)})
                    # A description or a schema that moved is a document that
                    # moved, and the vector it had is a vector of text that is
                    # no longer there — those two are the whole of what
                    # `tool_document` reads.  Anything else that moved (a
                    # `remote_name`, a status) leaves the document identical, so
                    # re-embedding it would buy a vector of the same text at the
                    # price of an embedding call and a rewritten row.
                    if moved.keys() & {"description", "schema"}:
                        documents.append((name, tool_document(fields)))
                # Except a row that stated its own: a merge re-enables what an
                # older *config* switched off, and for these two families the
                # config is the thing that just spoke — so re-enabling a
                # `cli:` entry the operator has switched off would undo the
                # setting on every search.
                if previous["status"] != STATUS_ENABLED and not own_verdict:
                    reconnected.append(name)
                if not moved and previous["status"] == STATUS_ENABLED:
                    skipped += 1

        return {
            "incoming": incoming,
            "inserts": inserts,
            "updates": updates,
            "reconnected": reconnected,
            "purged": sorted(name for name in existing if name not in incoming),
            "documents": documents,
            "skipped": skipped,
        }

    async def _embed(
        self, documents: Sequence[tuple[str, str]], embedder: Embedder
    ) -> dict[str, list[list[float]]]:
        """One request for every chunk of every row that needs a vector.

        The count is checked: a short answer would otherwise read as "these
        tools had nothing worth embedding", and the rows would keep no vector at
        all — a hole in the index that nothing outside could see.
        """
        chunks, spans = _chunk_documents(documents, embedder.max_chars)
        if not chunks:
            return {}
        vectors = await embedder.embed(chunks)
        if len(vectors) != len(chunks):
            raise RuntimeError(
                f"the embedding model answered {len(vectors)} vectors for "
                f"{len(chunks)} chunks, so {len(spans)} tool(s) would be "
                f"searchable only by keyword"
            )
        return {name: vectors[start : start + count] for name, start, count in spans}

    def _apply(
        self, plan: Mapping[str, Any], vectors: Mapping[str, list[list[float]]]
    ) -> dict[str, Any]:
        """Write the plan, and the vectors it earned, in one transaction.

        Everything a merge does to the file happens here: the rows, the keyword
        documents, the vectors, and the deletions.  So the catalogue a reader
        sees is always a whole source's list as of one moment — never a tool
        that is stored but unindexed, or a vector whose row is gone.
        """
        inserted: list[str] = []
        updated: list[str] = []
        with self._connect() as connection:
            for entry in plan["inserts"]:
                cursor = connection.execute(
                    "INSERT INTO tool(name, description, category, source_id,"
                    " remote_name, schema, status, load_status, last_loaded,"
                    " last_used)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry["name"],
                        entry["description"],
                        entry["category"],
                        entry["source_id"],
                        entry["remote_name"],
                        entry["schema"],
                        entry["status"],
                        entry["load_status"],
                        entry["last_loaded"],
                        entry["last_used"],
                    ),
                )
                rowid = int(cursor.lastrowid or 0)
                self._index(connection, rowid, entry)
                _write_tool_vectors(
                    connection, str(entry["name"]), vectors.get(entry["name"], [])
                )
                inserted.append(str(entry["name"]))

            for entry in plan["updates"]:
                name = str(entry["name"])
                sets = [f"{column} = ?" for column in entry["moved"]]
                values = [entry[column] for column in entry["moved"]]
                connection.execute(
                    f"UPDATE tool SET {', '.join(sets)} WHERE name = ?",
                    [*values, name],
                )
                rowid = int(
                    connection.execute(
                        "SELECT rowid FROM tool WHERE name = ?", (name,)
                    ).fetchone()["rowid"]
                )
                self._index(connection, rowid, entry)
                if name in vectors:
                    _write_tool_vectors(connection, name, vectors[name])
                updated.append(name)

            for name in plan["reconnected"]:
                # No `status != 'disabled'` guard here, and that is the one place
                # this differs from v1: a merge only ever runs for a source that
                # has just answered, and a source that answers is one the config
                # connects — so a row left `disabled` by an older config is a row
                # whose switch has been turned back on.
                connection.execute(
                    "UPDATE tool SET status = ? WHERE name = ? AND status != ?",
                    (STATUS_ENABLED, name, STATUS_ENABLED),
                )

            self._delete(connection, plan["purged"])

        return {
            "inserted": inserted,
            "updated": updated,
            "reconnected": list(plan["reconnected"]),
            "purged": list(plan["purged"]),
            "skipped": int(plan["skipped"]),
        }

    def _seed(self, category: str, autoload: bool) -> str:
        """What a tool's load state is when it is first seen.

        **Ours are loaded; somebody else's are on demand.**  A plugin's tools
        are few and the model is expected to have them — a model that has
        quietly lost `now` and `calc` is the failure DESIGN.md §8 is built
        around — while a server under `tools:` may offer ninety tools that cost
        a prompt on every call until one is wanted.  `autoload: true` is the
        operator saying this one is wanted, and it is the same set of sources
        `_protected` keeps from eviction: a tool that starts loaded because
        somebody asked for it must not be evicted by the budget either.

        A new row is the only thing this decides.  Discovery never puts a tool
        into the model's list by itself, and it never takes one out.
        """
        if category in (SKILL, CLI):
            # Neither has a load state at all: a skill is a document the hub
            # reads and a command is a program already installed, so "not loaded
            # yet" would be a promise about a step that does not exist — and
            # `load_status='n/a'` is what `tool_search`'s own filter and the
            # column's documentation both say these two carry.  It is also what
            # keeps them out of the model's list: the gate is the function
            # categories, so a row can be findable without being callable.
            return NA
        if category == PLUGIN or autoload:
            return LOADED
        return UNLOADED

    def _index(
        self, connection: sqlite3.Connection, rowid: int, fields: Mapping[str, Any]
    ) -> None:
        """Rewrite one row's keyword document, in the caller's transaction.

        Deleted and inserted rather than updated, because an FTS5 row has no
        `UPDATE` that replaces its text: what a row is found by is a whole
        document, and writing the new one is the only way to stop the old one
        being a hit.
        """
        connection.execute("DELETE FROM tool_fts WHERE rowid = ?", (rowid,))
        connection.execute(
            "INSERT INTO tool_fts(rowid, name, description, category,"
            " source_id, schema) VALUES (?, ?, ?, ?, ?, ?)",
            (
                rowid,
                *(
                    textindex.normalize(_indexable(fields.get(field, "")))
                    for field in _FTS_FIELDS
                ),
            ),
        )

    def _delete(self, connection: sqlite3.Connection, names: Sequence[str]) -> None:
        """Remove rows, and everything derived from them, explicitly.

        Not by trusting the foreign key's cascade: SQLite does not enforce
        foreign keys unless it is asked to, and a chunk row whose tool is gone is
        a search that returns a name nothing can resolve.  Takes a connection so
        that a merge's deletions happen in the merge's transaction — the rows it
        wrote and the rows it removed are one change to the file.
        """
        if not names:
            return
        marks = _marks(names)
        _clear_vectors(connection, names)
        connection.execute(
            f"DELETE FROM tool_fts WHERE rowid IN"
            f" (SELECT rowid FROM tool WHERE name IN ({marks}))",
            list(names),
        )
        connection.execute(f"DELETE FROM tool WHERE name IN ({marks})", list(names))

    def set_source_state(self, source: str, state: str) -> int:
        """Record the verdict on one source: `enabled`, `error` or `disabled`.

        Called when a source has answered, when a link failed or a connect would
        not start — and, since the process that holds a source is the one that
        reads the section it was configured in, when the operator has switched it
        off.  It is a *verdict* and not a connection state: what it says is
        whether the thing behind those rows is reachable, which is the only thing
        either side can act on.

        **It never touches a switched-off source.**  `disabled` is a standing
        answer and the other two are about *now*: a server the operator turned
        off cannot become `error` because somebody tried to reach it, and one
        that comes back cannot resurrect a row the config switched off — which is
        why that arm is in the `WHERE` and not up to the caller.
        """
        if state not in (STATUS_ENABLED, STATUS_ERROR, STATUS_DISABLED):
            raise ValueError(
                f"{state!r} is not a verdict; use enabled, error or disabled"
            )
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tool SET status = ? WHERE source_id = ? AND status != ?",
                (state, source, STATUS_DISABLED),
            )
        return int(cursor.rowcount)

    def reset(self) -> dict[str, int]:
        """Withdraw the verdicts this config can no longer justify — run at open.

        A verdict is a statement about *now*, and this file outlives the process
        that wrote it, so some of what it holds is about a moment that has
        passed.  One of those this process can speak to on its own: a source
        **it cannot name at all** is `error`, whatever wrote its rows not being
        something this file can be asked about any more.

        **Everything else is left alone, deliberately.**  A source this process
        can name is one the hub is about to ask about, and whether it is
        answering is the hub's to say — it is the party that asks, and it writes
        the verdict when a source answers, when one is switched off and when a
        link fails.  Marking those rows `error` here would be this file guessing
        at a fact it cannot observe, and the guess would be visible: a db
        restarted under a running hub would report every tool as unusable while
        the model was still holding and calling them.

        **Which is why the sources a plugin holds read as `error` here**, in the
        window between this running and the hub's first declaration: this process
        cannot name them, and they are not reachable by anybody *yet*.  The
        declaration writes the truth over it a moment later.
        """
        with self._connect() as connection:
            marks = _marks(self.known)
            cursor = connection.execute(
                f"UPDATE tool SET status = ? WHERE status = ?"
                f" AND category IN ({_in_list(FUNCTION_CATEGORIES)})"
                + (f" AND source_id NOT IN ({marks})" if self.known else ""),
                (STATUS_ERROR, STATUS_ENABLED, *sorted(self.known)),
            )
        return {"error": int(cursor.rowcount)}

    def set_load(self, name: str, load_status: str) -> dict[str, Any]:
        """Flip one row's load state, and say what happened.

        The answer is a *fact* — one of a closed set of words — and not a
        sentence: what a refusal means to a model is the caller's to phrase, and
        this layer has no business writing prose.  The facts are `unknown` (no
        such row), `no_load_state` (a skill is a document, not something to
        load), `disabled` and `error` (its owner is switched off, or is not
        answering), `already` (it is where the caller wants it), and the two
        states themselves when the row moved.

        The guards are checked here rather than by the caller so that "cannot be
        loaded" is one statement in one place — and so that the row returned is
        the row *after* the move, which is what makes the answer worth reading.

        **A load stamps the list and nothing else.**  `last_loaded` moves and
        `last_used` does not, because loading is not a use: the model asked to
        *see* the tool, and the budget's ordering depends on the two staying
        apart.  See `touch`.
        """
        if load_status not in (LOADED, UNLOADED):
            raise ValueError(f"{load_status!r} is not a load state")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return {"outcome": "unknown", "tool": None}
            found = _tool_dict(row)
            outcome = _load_outcome(found, load_status)
            if outcome not in (LOADED, UNLOADED):
                return {"outcome": outcome, "tool": found}
            bump = ", last_loaded = ?" if load_status == LOADED else ""
            values: list[Any] = [load_status]
            if load_status == LOADED:
                values.append(now())
            values.extend([name, load_status])
            connection.execute(
                f"UPDATE tool SET load_status = ?{bump}"
                " WHERE name = ? AND load_status != ?",
                values,
            )
            found["load_status"] = load_status
        return {"outcome": outcome, "tool": found}

    def touch(self, name: str) -> int:
        """Mark one row as *called* — the stamp the budget mostly evicts by.

        **Called, and not loaded**, which is the whole distinction `last_used`
        exists for.  The budget's question is which tools the model has stopped
        reaching for, and loading is not a use: a tool the model asked for by
        name five minutes ago is a better thing to keep than one it pulled in
        with a batch load this second.  Loading keeps its own stamp
        (`last_loaded`, written by `set_load` and by a new row's seed), and
        `evict` orders by whichever of the two is *newer* — so a tool that was
        just loaded is not the first victim either, which is the answer a
        strictly-called ordering would get wrong.

        **Every row, not only a loaded one.**  What the model called is a fact
        about the tool; the ordering takes the newer stamp, so a call recorded
        against a row nobody is holding cannot make it look older than it is,
        and it is *worth* recording — the model may call a name it found with
        `tool_search` without loading it, and that call is evidence about the
        tool the next eviction should see.
        """
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tool SET last_used = ? WHERE name = ?", (now(), name)
            )
        return int(cursor.rowcount)

    # --- what the hub reads ---------------------------------------------------

    def injectable(self, sources: Sequence[str]) -> dict[str, Any]:
        """The tools the model may be given now: loaded, and owned by a live source.

        **`sources` is the caller's, because liveness is the one thing this file
        cannot know.**  Which servers are connected is a fact about the hub's
        connections — its rule is that a source is usable when its tool list is
        in hand — and the db has no way to ask.  So the caller says which sources
        it is holding a list from, and this answers with those.

        **The row's `status` deliberately does not gate this.**  It is the
        record of the last verdict, which is what `tool_search` reports and what
        a person reads when something is missing; the *gate* is the live-source
        list, and the two disagreeing — a db restarted under a running hub —
        must not be able to empty the model's tool list.

        The budget is **not** enforced here.  It is a turn-boundary decision —
        the harness trims the list before it saves the turn (`evict`) — and a
        gate that also evicted would be trimming the list underneath a model
        that is still using it.
        """
        live = [str(name) for name in dict.fromkeys(sources)]
        if not live:
            return {"tools": []}
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM tool WHERE {_injectable_sql(live)} ORDER BY name", live
            ).fetchall()
        return {"tools": [_tool_dict(row) for row in rows]}

    def evict(
        self, sources: Sequence[str], *, autoload: Iterable[str] = ()
    ) -> list[str]:
        """Trim the loaded set to `threshold`, least recently *used* first.

        **Use, with the load as the fallback, and not the load alone.**  The
        ordering key is the newer of the two stamps (`last_used`, which the hub
        writes after every call the model makes, and `last_loaded`, written when
        the row entered the list), so a tool the model has been calling outlives
        one that was merely loaded after it, while a tool loaded a moment ago
        and not yet reached for is not the first thing thrown away.  **The
        obvious rule — order by the load — is the one that gets it wrong**: a
        batch `func_tool_load` restamps everything it brings in, so the tools
        the model is actually working with, loaded long ago and called all turn,
        look like the oldest in the list.

        **Called at a turn boundary, by the harness** — `_func_tool_unload` is
        the tool that carries it, and the agent server invokes it before a turn
        is saved.  That is where a trim belongs: the *model's* list is rebuilt
        before every request, so trimming it mid-turn would take away a tool the
        model had just loaded and was about to use, while a boundary is a moment
        nothing is in flight.

        The victim is chosen among the tools the model is *holding* — the same
        rows `injectable` answers with — so a server that is down, or a row the
        config switched off, cannot absorb the budget by being counted and not
        injected.  Nothing of ours is ever a victim, and neither is anything the
        operator marked `autoload`: the budget exists to stop somebody else's
        ninety tools crowding the prompt, not to take away the tools this system
        guarantees (DESIGN.md §8).

        **The count is of everything held, protected tools included** — v1's
        arithmetic, and it is worth knowing what it means at the edges: a
        threshold below the number of tools slife2 ships means the budget can
        never be met, and every evictable tool goes.  A sane threshold is well
        above them, which is why the default is a hundred.

        Returns the names it unloaded, which is what the caller reports.
        """
        live = [str(name) for name in dict.fromkeys(sources)]
        if not live:
            return []
        where = _injectable_sql(live)
        with self._connect() as connection:
            held = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM tool WHERE {where}", live
                ).fetchone()[0]
            )
            excess = held - self.threshold
            if excess <= 0:
                return []
            values = list(live)
            protected = f"category IN ({_in_list({PLUGIN})})"
            wanted = frozenset(str(name) for name in autoload)
            if wanted:
                protected += f" OR source_id IN ({_marks(wanted)})"
                values.extend(sorted(wanted))
            # `MAX` of the two stamps: the newer event wins, so a call outranks
            # a later load and a load outranks an earlier one.  Both columns are
            # `''` for "never", which sorts below every timestamp — so a row
            # with neither is the first to go, which is the right answer for a
            # row nothing has ever happened to.
            victims = [
                str(row["name"])
                for row in connection.execute(
                    f"SELECT name FROM tool WHERE {where} AND NOT ({protected})"
                    " ORDER BY MAX(last_used, last_loaded) ASC, name LIMIT ?",
                    (*values, excess),
                )
            ]
            if not victims:
                return []
            connection.execute(
                f"UPDATE tool SET load_status = ? WHERE name IN ({_marks(victims)})",
                (UNLOADED, *victims),
            )
        logger.info(
            "%d tool(s) unloaded to stay under %d: %s",
            len(victims),
            self.threshold,
            ", ".join(victims),
        )
        return victims

    def route(self, name: str) -> dict[str, Any] | None:
        """The row for one advertised name, or `None` if there is no such tool.

        What a call needs and what the gate does not carry: which source owns
        the name, and what that source calls the tool itself.  A routed call is
        gated on there being an instance behind the name and **not** on the load
        state — v1's rule, and the right one: loading is about what the model can
        *see*, and a name it just found with `tool_search` is a name it can use.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool WHERE name = ?", (name,)
            ).fetchone()
        return _tool_dict(row) if row else None

    def source_counts(self) -> dict[str, dict[str, int]]:
        """Per source: how many tools it has, and how many the model holds.

        Two numbers because they answer two questions.  `tools` is what the
        source last offered — the answer to "why is my tool missing" when it is
        not the same as `loaded`, which is how many of them the model has in its
        list right now.  A source with ninety tools and none loaded is a healthy
        server, and saying so is the difference between a fault and a choice.
        """
        where = f"category IN ({_in_list(FUNCTION_CATEGORIES)})"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT source_id, COUNT(*) AS tools,"
                f" SUM(CASE WHEN load_status = '{LOADED}' THEN 1 ELSE 0 END) AS loaded"
                f" FROM tool WHERE {where} GROUP BY source_id"
            ).fetchall()
        return {
            str(row["source_id"]): {
                "tools": int(row["tools"]),
                "loaded": int(row["loaded"]),
            }
            for row in rows
        }

    # --- finding a tool -------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        embedder: Embedder,
        limit: int = 10,
        category: str = "",
        source_id: str = "",
        status: str = "",
        load_status: str = "",
    ) -> dict[str, Any]:
        """Tools matching `query`, best first — by keyword and by meaning.

        Two legs and one fusion, the same shape as a turn search and for the
        same reason: `bm25` is unbounded and depends on the corpus, a cosine
        distance depends on the model, and a rank is comparable by construction.
        A tool both legs found outranks one only one of them did.

        **An empty query is a browse, not an empty answer.**  Nothing asks for
        the whole catalogue by accident — the caller has to write no query —
        and v1's rule is the useful one: a list of what exists is how a model or
        a person finds out what a category holds.

        The filters narrow and never decide: a search with a category is about
        that category, and one with `status='disabled'` is how "it exists but is
        switched off" becomes answerable rather than a guess.
        """
        limit = page_limit(limit, MAX_TOOL_PAGE)
        clauses, values = _tool_filters(
            category=category,
            source_id=source_id,
            status=status,
            load_status=load_status,
        )
        if not query.strip():
            rows = [
                _search_dict(row)
                for row in await asyncio.to_thread(self._browse, limit, clauses, values)
            ]
            return {"results": rows, "browsed": True}

        expression = textindex.match_expression(query)
        over = min(limit * _TOOL_OVERFETCH, _MAX_SQL_VARS)
        ranked = {
            "keyword": await asyncio.to_thread(
                self._keyword_hits, expression, over, clauses, values
            ),
            "semantic": await self._semantic_hits(query, embedder, over),
        }
        similarity = dict(ranked["semantic"])
        fused = [
            rowid
            for rowid, _ in fuse_ranked(
                {
                    "keyword": ranked["keyword"],
                    "semantic": [rowid for rowid, _ in ranked["semantic"]],
                }
            )
        ]
        rows = await asyncio.to_thread(
            self._rows_in_order, fused[:_MAX_SQL_VARS], clauses, values, limit
        )
        results = []
        for row in rows:
            rowid = int(row["rowid"])
            results.append(_search_dict(row, similarity.get(rowid)))
        return {"results": results, "browsed": False}

    def _browse(
        self, limit: int, clauses: list[str], values: list[Any]
    ) -> list[sqlite3.Row]:
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            return connection.execute(
                f"SELECT rowid AS rowid, * FROM tool {where}"
                " ORDER BY category, name LIMIT ?",
                (*values, limit),
            ).fetchall()

    def _keyword_hits(
        self, expression: str, limit: int, clauses: list[str], values: list[Any]
    ) -> list[int]:
        """Rowids by `bm25`, best first.

        The filters are applied in SQL rather than to the fused answer, which is
        the one place they can be exact: `rank` orders *every* match, so cutting
        to the limit afterwards still returns the best rows inside the filter.
        """
        weights = ", ".join(str(weight) for weight in BM25_WEIGHTS)
        where = f"AND {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT f.rowid AS rowid FROM tool_fts f"
                f" JOIN tool t ON t.rowid = f.rowid"
                f" WHERE tool_fts MATCH ? {where}"
                f" ORDER BY bm25(tool_fts, {weights}) LIMIT ?",
                (expression, *values, limit),
            ).fetchall()
        return [int(row["rowid"]) for row in rows]

    async def _semantic_hits(
        self, query: str, embedder: Embedder, k: int
    ) -> list[tuple[int, float]]:
        """Rowids by vector distance, nearest first, with a similarity each.

        The **raw** query, not the normalized one: normalization is the keyword
        leg's rule, and it inserts a space between every pair of CJK characters,
        which is what makes them tokens there and is nonsense as text handed to
        a model.
        """
        vectors = await embedder.embed([query])
        if len(vectors) != 1:
            raise RuntimeError(
                f"the embedding model answered {len(vectors)} vectors for one "
                f"query, so this search cannot say what a tool is about"
            )
        nearest = await asyncio.to_thread(
            self._nearest, sqlite_vec.serialize_float32(vectors[0]), k
        )
        return [
            (rowid, round(max(0.0, 1.0 - distance), 4)) for rowid, distance in nearest
        ]

    def _nearest(self, query_vector: bytes, k: int) -> list[tuple[int, float]]:
        """The KNN, and the dedup it cannot do itself.

        `k` counts *chunks*, and a tool with a long schema has several, so the
        best chunk per tool is what is kept — a KNN in `vec0` refuses the
        `GROUP BY` that would say so.  A vector of another width is skipped
        rather than compared: rows from two models cannot be ranked against each
        other, and a distance between them is a number with no meaning at all.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT t.rowid AS rowid, t.name AS name, v.distance AS distance"
                " FROM tool_vec v JOIN tool_chunk c ON c.id = v.rowid"
                " JOIN tool t ON t.name = c.name"
                " WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
                (query_vector, k),
            ).fetchall()
        nearest: dict[str, tuple[int, float]] = {}
        for row in rows:
            nearest.setdefault(
                str(row["name"]), (int(row["rowid"]), float(row["distance"]))
            )
        return list(nearest.values())

    def _rows_in_order(
        self,
        rowids: list[int],
        clauses: list[str],
        values: list[Any],
        limit: int,
    ) -> list[sqlite3.Row]:
        """Those rows, in the order they were ranked, filtered and cut.

        One query rather than one per row, because the order comes from the
        fusion and the rows come from the table.  The semantic leg cannot apply
        a filter — a KNN has no `WHERE` — so it is applied here, which is why a
        filtered search can return fewer than it was asked for.
        """
        if not rowids:
            return []
        where = f"AND {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT rowid AS rowid, * FROM tool"
                f" WHERE rowid IN ({_marks(rowids)}) {where}",
                (*rowids, *values),
            ).fetchall()
        found = {int(row["rowid"]): row for row in rows}
        return [found[rowid] for rowid in rowids if rowid in found][:limit]

    # --- the derived indexes --------------------------------------------------

    async def sync_indexes(self, embedder: Embedder) -> dict[str, Any]:
        """Make both indexes agree with `embedder`, rebuilding what does not.

        Run at startup, before anything is served.  The things that can leave an
        index unreadable — text written by other rules, a different embedding
        model, a different width, a repointed endpoint — all arrive here as "the
        recorded identity is not the current one", and all take the same answer:
        build it again.  That is what makes a changed embedding model a rebuild
        rather than a migration, and it is one mechanism for all of them.

        This is also the only thing that embeds a row which lost its vector, for
        the reason it is the only thing that does so for turns: a merge embeds
        what it changed and nothing else.

        **The first start pays and the rest do not.**  A fresh identity — a new
        file, or a changed model — re-embeds every tool of every server, which
        is a real wait before anything is served; afterwards the identity
        matches, nothing is missing, and this is one read of `meta` and one
        query that returns no rows.
        """
        await asyncio.to_thread(self._sync_text_index)
        await asyncio.to_thread(self.ensure_index, embedder)
        missing = await asyncio.to_thread(self.tools_without_vectors)
        vectors = await self._embed(missing, embedder)
        await asyncio.to_thread(self._place, vectors)
        return {
            "vectors": len(vectors),
            "identity": _tool_index_identity(embedder),
        }

    def ensure_index(self, embedder: Embedder) -> None:
        """Create the vector index, or rebuild it if its identity has changed.

        Cheap when it is current: one read of `meta`.  A rebuild here **drops
        the vectors** without re-embedding anything, because the caller is a
        reconcile that is embedding the rows it just wrote — putting the rest
        back is `sync_indexes`, and the two are separate for the same reason
        they are in the turn store.
        """
        identity = _tool_index_identity(embedder)
        with self._connect() as connection:
            if _meta(connection, "meta").get("vector_identity", "") == identity:
                return
            connection.execute("DROP TABLE IF EXISTS tool_vec")
            connection.execute("DELETE FROM tool_chunk")
            connection.execute(_vector_ddl(embedder.dimension, "tool_vec"))
            _set_meta(
                connection,
                "meta",
                vector_identity=identity,
                vector_dim=str(embedder.dimension),
            )
            logger.info(
                "the tool index is built again for %s (%.60s)", self.path, identity
            )

    def _place(self, vectors: Mapping[str, list[list[float]]]) -> None:
        """Write a batch of vectors, in one transaction."""
        with self._connect() as connection:
            for name, rows in vectors.items():
                _write_tool_vectors(connection, name, rows)

    def _sync_text_index(self) -> None:
        """Rebuild the keyword index when the rules that built it have changed.

        One condition, because the other case cannot happen: a row can only be
        missing from this index if the version stamp says the rules match — and
        then it cannot be, since the row is indexed in the transaction that
        writes it.  A file with no stamp at all is the version-mismatch case,
        and is rebuilt whole.
        """
        with self._connect() as connection:
            if (
                _meta(connection, "meta").get("text_version", "")
                == textindex.RULES_VERSION
            ):
                return
            connection.execute("DELETE FROM tool_fts")
            for row in connection.execute("SELECT rowid AS rowid, * FROM tool"):
                self._index(
                    connection,
                    int(row["rowid"]),
                    {
                        "name": str(row["name"]),
                        "description": str(row["description"]),
                        "category": str(row["category"]),
                        "source_id": str(row["source_id"]),
                        "schema": str(row["schema"]),
                    },
                )
            _set_meta(connection, "meta", text_version=textindex.RULES_VERSION)

    def tools_without_vectors(self) -> list[tuple[str, str]]:
        """Every row the vector index does not hold, as `(name, document)`.

        Oldest first, so a first run embeds a server's tools in the order they
        were first seen rather than in whatever order the table happens to be
        in.  "Has no vector" means exactly one thing here — the row is new, or
        its text moved — because those are the only two paths that leave a row
        without one; a rebuild for a changed model leaves them all without one,
        which is how the same query fills the index back in.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT t.rowid AS rowid, t.name AS name, t.description AS description,"
                " t.schema AS schema FROM tool t"
                " LEFT JOIN tool_chunk c ON c.name = t.name"
                " WHERE c.id IS NULL"
                " ORDER BY t.rowid",
            ).fetchall()
        return [(str(row["name"]), tool_document(dict(row))) for row in rows]


def _tool_index_identity(embedder: Embedder) -> str:
    """What the vector index was built with, as one comparable string.

    The text contract is part of it and not only the model, because a vector is
    a function of the text it was made from: changing what goes into one makes
    every stored vector the wrong vector for its tool, which is the same failure
    as changing the model and takes the same rebuild.
    """
    return f"{textindex.RULES_VERSION}|{embedder.identity}|{embedder.dimension}"


def _chunk_documents(
    rows: Sequence[tuple[str, str]], max_chars: int
) -> tuple[list[str], list[tuple[str, int, int]]]:
    """`(every chunk in order, where each row's chunks are)`.

    Split out so the one request that carries the whole batch can be built
    before anything is sent: an embedding call per tool is a round trip per
    tool, and a first run has every tool of every server to embed.
    """
    chunks: list[str] = []
    spans: list[tuple[str, int, int]] = []
    for name, document in rows:
        pieces = embed_chunks(document, limit=max_chars)
        spans.append((name, len(chunks), len(pieces)))
        chunks.extend(pieces)
    return chunks, spans


def _load_outcome(row: Mapping[str, Any], load_status: str) -> str:
    """What flipping one row's load state would do — v1's refusals, in order.

    Order matters for one case: a row can be `loaded` *and* unusable — its
    server went down after the model loaded it — and answering "already loaded"
    there would be a lie the model can see through, since nothing of it is in
    its list.  So the owner's state is asked first, and the answer is the reason
    rather than the state.
    """
    if row["category"] not in FUNCTION_CATEGORIES:
        return "no_load_state"
    if load_status == LOADED:
        if row["status"] == STATUS_DISABLED:
            return STATUS_DISABLED
        if row["status"] == STATUS_ERROR:
            return STATUS_ERROR
    if row["load_status"] == load_status:
        return "already"
    return str(load_status)


def _injectable_sql(sources: Sequence[str]) -> str:
    """The gate, in SQL: a loaded function tool whose owner is live.

    Built from the constants rather than spelled out, so the SQL cannot drift
    from `FUNCTION_CATEGORIES` — the same reason `_CATEGORY_CHECK` is.

    **A name beginning with `_` is a harness tool and is not injected** — with
    one exception, below.  v1's convention, and it is a name and not a column
    for v1's reason: what makes a tool the harness's is that the *machinery*
    calls it, and a fact about who calls a thing belongs on the thing, where
    both sides can read it without a second register to keep in step.

    **The exception is `_func_tool_unload`**, and it is the only one.  The
    harness's trim is written into the conversation as a tool pair, a pair names
    a tool, and a request whose history calls a tool its `tools` array does not
    declare is a 400 from the Responses and Messages backends (v1's rule, and
    v1's single exception: one `_` tool the model sees).  A tool the model reads
    in its own history but cannot call would be inconsistent with itself, so it
    is injected, callable, and named the same way it is named here.
    """
    visible = _in_list(MODEL_VISIBLE_HARNESS_TOOLS) or "''"
    return (
        f"category IN ({_in_list(FUNCTION_CATEGORIES)})"
        f" AND load_status = '{LOADED}'"
        f" AND source_id IN ({_marks(sources)})"
        f" AND (name NOT LIKE '\\_%' ESCAPE '\\' OR name IN ({visible}))"
    )


def _table_ddl(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return None if row is None else str(row["sql"] or "")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    """Whether a table (or a virtual one) is there at all.

    Asked for the vector index, which cannot be created with the rest of the
    schema: `vec0` fixes its width in the DDL and only an embedder knows the
    width, so a catalogue can hold rows before it holds vectors.
    """
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (table,)
        ).fetchone()
        is not None
    )


def _tool_filters(
    *, category: str, source_id: str, status: str, load_status: str
) -> tuple[list[str], list[Any]]:
    """The optional filters of a search, as `(clauses, params)`.

    Each is an equality on a column whose domain is closed, so a caller that
    passes something else gets no rows rather than an error: a filter that
    matched everything because of a typo would be worse than one that matches
    nothing.
    """
    clauses: list[str] = []
    values: list[Any] = []
    for column, value in (
        ("category", category),
        ("source_id", source_id),
        ("status", status),
        ("load_status", load_status),
    ):
        if value:
            clauses.append(f"{column} = ?")
            values.append(str(value))
    return clauses, values


def _write_tool_vectors(
    connection: sqlite3.Connection, name: str, vectors: list[list[float]]
) -> None:
    """Place one tool's chunks, one vector each, under ids of their own.

    A row in `tool_vec` is a chunk and not a tool — `vec0` has no way to hold
    several vectors in one row — so `tool_chunk` is what remembers which tool a
    chunk came from, and the two are written together: a chunk whose vector is
    missing would be a tool the index believes it holds.

    **What was there goes first, vectors included.**  `tool_chunk.id` is a
    plain rowid and SQLite hands the same one out again once the last row using
    it is deleted — so writing new chunks over an old tool's ids means writing
    vectors over the ones still in `tool_vec`, which `vec0` refuses.  The
    delete is the only thing that makes this call idempotent, and it is why
    re-embedding a row is safe.
    """
    _clear_vectors(connection, [name])
    for index, vector in enumerate(vectors):
        cursor = connection.execute(
            "INSERT INTO tool_chunk (name, chunk_index) VALUES (?, ?)", (name, index)
        )
        connection.execute(
            "INSERT INTO tool_vec (rowid, embedding) VALUES (?, ?)",
            (int(cursor.lastrowid or 0), sqlite_vec.serialize_float32(vector)),
        )


def _clear_vectors(connection: sqlite3.Connection, names: Sequence[str]) -> None:
    """Forget the vectors of these rows, keeping the rows themselves.

    The vector table may not exist yet — it is created with a *width*, which
    only an embedder can supply — and nothing to clear is not an error.
    """
    if not names:
        return
    marks = _marks(names)
    if _table_exists(connection, "tool_vec"):
        connection.execute(
            f"DELETE FROM tool_vec WHERE rowid IN"
            f" (SELECT id FROM tool_chunk WHERE name IN ({marks}))",
            list(names),
        )
    connection.execute(f"DELETE FROM tool_chunk WHERE name IN ({marks})", list(names))
