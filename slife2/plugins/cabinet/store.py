"""The cabinet's index: six kinds of document, and one corpus to find them in.

The cabinet holds **notes** (keyed by subject), a **diary** (keyed by date),
**files** (bytes on disk, metadata here), **reports** (keyed by title),
**dumps** (one tool's own output, keyed by the call that produced it) and
**images** (a picture a turn carried, keyed by its digest).  v1
called this plugin `memfiles` and its store had four tables, a `cabinet_docs`
view that unioned them and twelve triggers keeping a shared FTS table in step
(`slife/plugins/memfiles/schema.sql`).  What is kept is the *design* that made
those things one thing, and what is dropped is the machinery slife2 does
without:

**A dump is the harness's, and it is here because it is text.**  A result too
large for the conversation is shortened for the model and kept whole for
everybody else — and *nothing decides which part of it mattered*, because that
is the tool designer's judgement and not the harness's.  So the whole of it is
stored, and the shortened copy is a selection of lines the model made with the
budget in hand.  Bytes belong on disk, but a tool's output is text, and text
belongs where the cabinet's other text is: this kind is a dump rather than a
note because nothing about it was written by anybody, and it is in **this
corpus** rather than in a directory of its own because the thing looking for it
later is a search.  The cost is the one DESIGN.md §5.3 names — a rank only means
something inside the corpus that produced it — and a dump is the one kind
nobody wrote, so a search that names no kind reaches machine output ranked
against a person's notes.  Whether a search that named no kind should skip this
one is open, and it is a question about this corpus rather than about dumps.

**And this kind's reader is bounded, like every tool of ours that hands text to
a model.**  What the harness writes into the transcript is a selection of lines
with this dump named beside it, so a model that wants a part it did not get
reads the row back with `dump_read` — a tool that returns **at most the budget's
worth of the original**, saying how much there is and where the next window
starts.  The reader's cap and the harness's budget are **one number**, and that
is the whole of what makes the arrangement closed: a reader that could hand back
more than the budget would be a tool of ours feeding the harness's own
shortening, which is a loop with this system's machinery at both ends.  The
bound is a property of the tool rather than a hope about how a model uses it.

**A dump is not what a search that named no kind is for.**  A result over the
window is a tool's *output*, and the address a model has for it is the marker in
the transcript — so the corpus does not have to carry it for retrieval, and
machine output ranked against a person's own notes is the dilution DESIGN.md §5.3 warns
about: one rank, and the thing nobody wrote matching on every word it happens to
contain.  Naming the kind reaches it (`kind="dump"`), which is also how it is
browsed and listed.

**One row per call, keyed by the call id.**  That id is the one identifier both
sides already carry: the turn's own messages hold it on the tool message, and
this row is keyed by it, so the association with the turn is an identity rather
than a column — nothing to write at save time, nothing to keep in step, and
nothing that has to exist before the turn is saved (which a turn id would not).
A second write under one key replaces, because it is the same call re-shortened
and not a second fact about anything.

**One table, and the kinds are a column.**  A note, a day, a saved file, a
report, a dump and an image are six *values* of `kind` in `cabinet_doc`, with
the union of the columns they need.  That is the "four kinds are ONE corpus" rule read literally:
the rowid **is** the corpus id, so the two legs of a search fuse on `rowid` the
way the turns and the tool catalogue already do (`slife2.libs.store.index.fuse_ranked`), where
v1 had to synthesise a global id out of a per-kind offset.  Four tables also
meant the FTS table could only be kept correct by a trigger per kind per verb;
here the keyword text is written in the same transaction as the row that
produced it, which is how both of slife2's other stores do it and why this
module has no trigger at all.

**The per-kind facts are a table of their own** (`SPECS`): which column is the
key, which column is the time axis a list orders by, whether a second write
appends or replaces, and where the markdown mirror goes.  Every one of those is
a fact about a kind rather than about a document, and v1 spelled each of them
out four times over — once per table, once per view arm, once per trigger set,
once per upsert.

**Every write embeds, and there is no drainer.**  v1's memfiles shared `memdb`'s
`SemanticManager`, a background drainer that walked unembedded documents and
caught them up on its own schedule.  slife2 has no such thing and does not need
one: a turn is stored with its vector in one transaction, and this store is the
same — **a write that cannot embed stores nothing**, which is why the plugin
removes the bytes it copied when the embedding call fails.  What is left of the
drainer is the one pass `sync` makes when a cabinet is opened by a *different*
embedding model, and it is a rebuild rather than a drain: it does not remember
which rows are missing (the index is derived, so "missing" is a query) and it
does not run in the background.

**A document is written twice, and the file is the one a person reads.**  Every
kind but `file` also lands as markdown under the cabinet directory — `notes/`,
`diary/`, `reports/` — because a cabinet nobody can open in an editor is a
database with a nicer name.  The two writes are one operation and they are this
module's: a row without its mirror is a note that exists and cannot be read by
the person it was written for, and the mirror a row holds is a function of its
*key* rather than of its key's slug, so two subjects that slugify alike cannot
silently share one file (see `mirror_stem` — the readable name where it is
unambiguous, and the readable name plus a digest of the key where it is not).

**The cabinet is one directory per agent** — `agents/<agent>/cabinet/` — and it
is one *directory* rather than a set of files, which is what makes copying or
deleting a cabinet one operation.  Its contents are v1's (`notes/`, `diary/`,
`reports/`, and the saved files by category) so a v1 cabinet's documents are
adopted by copying them in; what is dropped from v1's layout is the `files/`
level that used to wrap the categories, which only existed because the cabinet's
*own* directory was called `<agent>.files/`.  The index (`.index.db`) is this
build's own and is not promised to be v1's: a vector index is not portable
across embedding models anyway, and slife2's rule for a changed model is a
rebuild rather than a migration.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import sqlite_vec

from slife2.helpers.clock import now
from slife2.helpers.timeutil import normalize_bound
from slife2.libs.host.paths import work_dir
from slife2.libs.store import textindex
from slife2.libs.store.index import (
    MAX_PAGE,
    MAX_SQL_VARS,
    NO_SIMILARITY,
    SYNC_BATCH,
    Embedder,
    _marks,
    _rowid,
    as_similarity,
    checked_vectors,
    clear_vectors,
    connect_to,
    embed_chunks,
    filter_clause,
    fuse_ranked,
    index_meta,
    meanings_by_cosine,
    page_limit,
    place_vectors,
    rebuild_vector_index,
    rows_in_order,
    set_index_meta,
    storable,
    table_exists,
    vector_identity,
    where_clause,
)

logger = logging.getLogger(__name__)

#: The kinds, in the order a listing names them.  A closed set: `kind` is what a
#: read tool takes and what a search filters on, so another one is a change here
#: and in `SPECS` rather than a row somebody writes — plus the schema below,
#: whose `CHECK` is built from this tuple, and the two docstrings in
#: `slife2.plugins.cabinet.server` that enumerate the kinds to a model.
NOTE = "note"
DIARY = "diary"
FILE = "file"
REPORT = "report"
#: A tool's output, kept whole because nothing here may decide what part of it
#: mattered.  The only kind **no tool of this plugin writes**: the harness dumps
#: it (`slife2.libs.harness`) and writes a marked selection into the
#: conversation, and `dump_read` is how a model reads back the part it did not
#: get — a bounded answer, for the reason the module docstring gives.
DUMP = "dump"
#: An image attached to a turn, kept whole because the turn log is text.
#: **The second bytes-backed kind**, and the one whose reader is not a text
#: tool: the conversation keeps an `[Image:{…}]` reference and `attach_image`
#: is how the picture comes back (`slife2.libs.turn.attachments`).  A local
#: path or `data:` URI is stored as bytes under `pictures/`; an `http(s)` URL
#: has no bytes here at all — this system never fetches one — so the row keeps
#: the URL as its `source` and the provider fetches it.
IMAGE = "image"
KINDS = (NOTE, DIARY, FILE, REPORT, DUMP, IMAGE)

#: The sentence a caller gets for a kind that is not one of these.  Spelled
#: from `KINDS` so that another one cannot be added without this agreeing.
_KIND_HINT = f"kind must be one of: {', '.join(KINDS)}"

#: The index file, inside the cabinet it indexes.
INDEX_NAME = ".index.db"

#: The cabinet's directory inside one agent's own.  **Not `files/`**, which is
#: what the cabinet *contains* (the saved attachments): two of those in one path
#: — `agents/jack/files/files/images/a.png` — read as nothing at all.
CABINET_DIR = "cabinet"

# `SYNC_BATCH` is `slife2.libs.store.index`'s, imported above, and the cabinet used to spell
# the number a second time.  The bound it puts on one `sync` call — every
# document is embedded eventually, because a document without a vector is simply
# missing from the index and the next open finds it again — is the same bound
# `MAX_PAGE` puts on a page, read off the one request an `embed` call is.


@dataclass(frozen=True)
class Spec:
    """What is true of one *kind* rather than of one document.

    The key is what a read tool and a remove tool take, and it is the only
    column a kind is addressed by: a note by its subject, a day by its date, a
    file by the path it was saved to, a report by its title, a dump by the
    tool call it is the output of, and an image by its digest.  Six kinds and
    six keys, which is why this is a column and not six tables.
    """

    #: The column a list orders by and a since/until window filters on.  v1's
    #: `_KIND_SPECS`, kept because each kind really does have its own axis: a
    #: note is *when it was last written*, a diary is *the day it is about*, and
    #: a file and a report are when they arrived.
    time: str
    #: Whether that column holds a **day** rather than a moment.  A diary's axis
    #: is `YYYY-MM-DD`, so a window on it is day-granular and a datetime bound
    #: is read as its day.
    day: bool
    #: Whether a second write under one key **appends** rather than replaces.
    #: A day and a report's title accumulate — two entries under one date are two
    #: facts about it — and the two kinds that hold one current thing per key are
    #: the two with a remove tool.  v1's rule, and the whole of it.
    appends: bool
    #: The markdown mirror's directory, or `""` for the kind whose bytes *are*
    #: the file.  A saved file is not mirrored: the cabinet keeps the bytes, and
    #: a second copy of them as text would be a copy of something that is not
    #: text.
    mirror: str


SPECS: dict[str, Spec] = {
    NOTE: Spec(time="updated_at", day=False, appends=False, mirror="notes"),
    # The diary's time axis **is** its key: a day keyed by `YYYY-MM-DD` sorts
    # chronologically as a string, so the column that addresses the row is the
    # column that orders it and there is no second one to keep in step.
    DIARY: Spec(time="key", day=True, appends=True, mirror="diary"),
    FILE: Spec(time="created_at", day=False, appends=False, mirror=""),
    REPORT: Spec(time="created_at", day=False, appends=True, mirror="reports"),
    # A dump replaces and does not append: the key is one call, and a second
    # write under it is that call shortened again rather than a second fact
    # about anything.  Its time axis is when the output arrived — the only
    # moment this row has, since nobody wrote it.
    #
    # **Mirrored like a note, unlike a file.**  The kind whose `mirror` is empty
    # is the one whose bytes are the document; a dump's content *is* text, and
    # the mirror is the whole of it in one file on disk — which is what the
    # marker in the transcript points a reader at.  What that costs is a second
    # copy of the text, in a directory nothing searches.
    DUMP: Spec(time="created_at", day=False, appends=False, mirror="dumps"),
    # An image is a saved-file's shape with a different reader: bytes on disk
    # (`pictures/`), no markdown mirror, one row per content digest so a
    # picture attached twice is stored once.  An `http(s)` source has no file
    # and is found by its URL; the row is still one per digest of that URL.
    IMAGE: Spec(time="created_at", day=False, appends=False, mirror=""),
}


def cabinet_dir(agent: str, subagent: str = "") -> Path:
    """One conversation's cabinet: `work_dir(agent, subagent)/cabinet/`.

    **Per conversation, with forks sharing their maker's.**  A non-fork subagent
    owns its cabinet (`agents/<agent>/subagents/<name>/cabinet/`); a fork is a
    temporary copy of its maker's conversation and uses the maker's cabinet.  A
    diary is one person's, and the client id is what says whose.

    The name is sanitised by `safe_agent_name` for the reason that function
    gives: an agent name comes from a command line, and `jack` becoming a
    *directory* is exactly the kind of thing a `..` in the wrong place turns
    into a write somewhere nobody intended.
    """
    path = work_dir(agent, subagent) / CABINET_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def index_path(agent: str, subagent: str = "") -> Path:
    """The cabinet's index file, beside the documents it indexes.

    Inside the cabinet rather than in `slife2.libs.store.db/` with the other
    stores, so the cabinet is **one directory** — copy it, back it up, or delete
    it and the cabinet is gone with nothing left behind.  The cost is that the
    store's path is not derivable from the data directory alone, which is what
    these two functions are for.
    """
    return cabinet_dir(agent, subagent) / INDEX_NAME


# ── The text a document is found by, and the text it is about ────────────────
# v1's rule, and the one place the port's shape is visible in the search: which
# column a leg reads is a decision about a *kind*'s columns, and here the four
# kinds have been flattened into one row — so the composition is a function of
# the row rather than of the view arm it came from.


def search_text(row: Mapping[str, Any]) -> str:
    """The text a document is **found** by — what the keyword leg indexes.

    Everything the row knows that is words: its title, its key, the paths
    associated with it, its body, its tags and a model's summary.  The paths are
    in because a cabinet is where a *path* is the thing somebody remembers —
    "that paper I saved under reference" reaches a row whose body is empty
    (`slife2.libs.store.db.searchable_text` makes the same choice for a turn's tool calls).

    Normalized here rather than at the index, exactly as `searchable_text` is
    and for the reason it states: a query is normalized by the same rule, and
    this is the one place that rule is applied to a cabinet's stored text.
    """
    parts = [
        str(row.get("title") or ""),
        str(row.get("key") or ""),
        str(row.get("source") or ""),
        str(row.get("file_path") or ""),
        str(row.get("body") or ""),
        str(row.get("tags") or ""),
        str(row.get("summary") or ""),
    ]
    return storable(textindex.normalize("\n".join(part for part in parts if part)))


def vector_text(row: Mapping[str, Any]) -> str:
    """The text a document is **about** — what its vector is a vector of.

    The body alone for the text kinds, which is the document itself.  A **file**
    and an **image** are the exceptions and the reason this is a function: they
    have no text of their own, so what each is about is what it **is** — its
    title, the paths that place it, and the summary once a model writes one.
    That is not decoration: without the identity in it, a file would have
    nothing to embed until somebody described it, so a cabinet of saved files
    would be missing from the vector index from the moment it was saved — and
    `sync` would report every one of them as a document with no vector, a
    "failed save" it could never clear.  v1's rule, and why v1 composed a
    file's body the same way (DESIGN.md §5.3).

    Tags are out of it, unlike the keyword text: a tag is a word somebody chose
    to file a document under, and a vector built from one describes the filing
    rather than the document.
    """
    parts = (
        [row.get("title"), row.get("source"), row.get("file_path"), row.get("summary")]
        if str(row.get("kind") or "") in (FILE, IMAGE)
        # **Body alone, and the summary is deliberately not here.**  A summary is
        # something a model wrote *about* the row, and it is read by the keyword
        # leg on either side; the vector is what the document *is*.  Only a file
        # carries one into its vector, because a file has no text of its own —
        # and that is the one kind `annotate` re-embeds.  Including it here made
        # `cabinet_summarize(kind="note", …)` change the text a vector should
        # come from while leaving the stored vector alone, so the new words were
        # unreachable by meaning and nothing could repair them ("missing" means
        # "has no chunks", and it had them).
        else [row.get("body")]
    )
    return storable("\n\n".join(str(part) for part in parts if part))


# ── Slugs and mirrors ───────────────────────────────────────────────────────

#: What survives into a filename.  Letters, digits and CJK are kept — a note
#: about 会议 is a file a person can find by reading the directory, which is the
#: whole point of the mirror — and everything else (a path separator above all)
#: becomes a dash.
_KEEP_RE = re.compile(r"[^\w-]+")

#: How long a slug may be, in characters.  Long enough for a sentence of a
#: subject, short enough that a deep path does not walk into a filesystem's
#: own limit — the same compromise `skills`' directory names make.
_SLUG_MAX = 60

#: How much of a key's digest a *disambiguated* mirror stem carries, in
#: characters of hex.  Ten is 40 bits, which is what has to hold for two keys
#: whose slugs collided: the digest is what tells them apart, so it is longer
#: than the sixteen bits a shortened *name* would need to be unlikely to repeat.
_MIRROR_DIGEST_CHARS = 10


def slug(text: str) -> str:
    """One key, as a filename stem — readable, and **not** injective.

    Falls back to a short hash rather than to a name like `note`: a subject that
    is entirely punctuation is one a person wrote on purpose, and a directory of
    `untitled.md`, `untitled-2.md`, … tells them nothing about which is which.

    What it is not is one-to-one, and that is why the *mirror* is not this
    function: `a b` and `a/b` both reduce to `a-b`, `a--b` and `a-b` to one
    stem, and anything past `_SLUG_MAX` keeps only its head.  A slug is what a
    person reads in a directory listing; `mirror_stem` is what a file is called.
    """
    kept = _KEEP_RE.sub("-", text).strip("-")
    kept = re.sub(r"-{2,}", "-", kept)[:_SLUG_MAX].strip("-")
    if kept:
        return kept
    return f"doc-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"


def mirror_stem(key: str) -> str:
    """One key, as a filename stem **no other key can share**.

    The readable slug when it is the key itself, and the slug plus a digest of
    the key when the slug lost something — a character it replaced, a run of
    dashes it collapsed, a tail it truncated.  Both halves are needed: a mirror
    is browsed by a person, so `会议记录.md` is worth keeping, and two documents
    that share one file is the failure this exists to prevent — with the second
    write silently replacing the first's markdown while both rows stayed
    correct, and a `note_remove` of either one deleting the other's file.

    **Disjoint as a *path*, which is why case decides it and the separator is
    `~`.**  A path is not a string: on a case-insensitive filesystem — Windows,
    macOS — `Readme.md` and `readme.md` are one file, so "the readable half is
    only used when `slug(key) == key`" was not enough: `Readme` and `readme` both
    satisfy it, took both halves of that rule, and shared the file this function
    exists to keep apart.  A key is therefore readable as itself only when it is
    *already* spelled the way every case-insensitive lookup of it resolves —
    casefolded — which makes the readable family a set no two of whose members
    can collide, and sends every other key to the digest half.  The readable half
    of a digest-carrying name is still the key's own slug, so `Python~ab12cd34.md`
    is still read by a person; one key spelled two ways is two files, which is
    what the collision cost.  `~` is not a character `slug` can keep, so no
    digest-carrying name can equal a readable one either, and within the digests
    a collision needs two keys whose slugs *and* whose digests agree.

    The digest is of the key and not of the row's id, because the mirror's
    directory is the kind's (`notes/`, `reports/`) and a key is unique inside
    it — `cabinet_doc`'s `UNIQUE (kind, key)` is the whole of the identity that
    has to be told apart here.
    """
    slugged = slug(key)
    if slugged == key and key == key.casefold():
        return slugged
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:_MIRROR_DIGEST_CHARS]
    return f"{slugged}~{digest}"


def mirror_path(spec: Spec, key: str) -> str:
    """Where one document's markdown lives, relative to the cabinet.

    `notes/<stem>.md`, `reports/<stem>.md`, `diary/2026-10-11.md` — and a
    diary's needs no special case, because its key *is* its day and a slug of
    `2026-10-11` is `2026-10-11`: digits and dashes both survive, which is what
    makes a directory of days read as a calendar.  A digest-carrying stem
    (`mirror_stem`) is the one thing a day's never is, because a date is a key
    `slug` does not change.

    **Of the key's identity, and not of its slug alone**, which is the whole
    reason the row stores this string instead of deriving it from the key each
    time: two keys that slugify alike would otherwise be two rows naming one
    file, and the second write would overwrite the first's markdown — after
    which a remove of either one unlinks the survivor.  Storing it is what makes
    the row and the file agree even if this function changes; the function is
    what makes them agree in the first place.
    """
    return f"{spec.mirror}/{mirror_stem(key)}.md" if spec.mirror else ""


def _write_mirror(root: Path, relative: str, text: str, *, append: bool) -> None:
    """Put the markdown at *relative*, creating the directory it needs.

    A temp file and `os.replace` when the mirror is *replaced*, so a reader
    never sees half a note; a plain append when it is *added to*, because that
    is what an append is — and a day's mirror is the accumulation itself, so
    rewriting it would be a second implementation of the row's own rule.
    """
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def resolve(root: Path, relative: str) -> Path:
    """A cabinet-relative path, as a path inside the cabinet — or a refusal.

    **A model writes these paths**, which is the whole reason this exists: a
    `file_read("../slife2.yaml")` is a model reading the operator's provider
    keys.  Resolved before it is compared, so a symlink or a `..` that only
    looks contained is not, and refused rather than sanitised — a silently
    rewritten path is a model reading a different file from the one it named.

    Raises:
        ValueError: If the path leaves the cabinet.
    """
    candidate = (root / relative).resolve()
    if candidate != root.resolve() and root.resolve() not in candidate.parents:
        raise ValueError(f"{relative!r} is not inside the cabinet")
    return candidate


# ── The schema ──────────────────────────────────────────────────────────────

_SCHEMA = f"""
-- The corpus: six kinds of document in one table.
--
-- **One table rather than one per kind plus a view**, which is the design v1
-- reached for a view to express: the kinds are one corpus, and a corpus whose
-- rows live in four tables is a corpus that has to be unioned back together
-- before it can be ranked.  A `kind` column says what the row is, `key` is what
-- a read and a remove tool take, and the columns the kinds do not use are empty
-- rather than absent.
--
-- **The rowid is the corpus id**, which is what lets a search fuse its two legs
-- on it (`slife2.libs.store.index.fuse_ranked`) exactly as the turns and the catalogue do.
--
-- `mirror` is stored rather than derived, so that today's row keeps pointing at
-- today's file whatever `mirror_path` decides later: the path a row was written
-- with is the path a remove of it unlinks, and a change to the naming rule
-- orphans nothing that already exists.  The rule itself (`mirror_stem`) is what
-- keeps two keys from ever *needing* one file: two subjects can slugify to one
-- stem, and a derived path would have let the second silently overwrite the
-- first's markdown while both rows stayed correct.
CREATE TABLE IF NOT EXISTS cabinet_doc (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ({",".join(f"'{k}'" for k in KINDS)})),
    key          TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    tags         TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    file_path    TEXT NOT NULL DEFAULT '',
    mime         TEXT NOT NULL DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    summary      TEXT NOT NULL DEFAULT '',
    period_start TEXT NOT NULL DEFAULT '',
    period_end   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT '',
    mirror       TEXT NOT NULL DEFAULT '',
    UNIQUE (kind, key)
);

-- A kind's list orders by its own time column, so each one is indexed: one
-- small index per kind rather than one that a `kind = ?` prefix could serve
-- only for the column it names.
CREATE INDEX IF NOT EXISTS idx_cabinet_updated ON cabinet_doc(kind, updated_at);
CREATE INDEX IF NOT EXISTS idx_cabinet_created ON cabinet_doc(kind, created_at);

-- The keyword index, one row per document, written in the same transaction as
-- the row it indexes.  `unicode61 remove_diacritics 2` is what both of the
-- other stores use, and `textindex` is what makes a Chinese query match: the
-- text is normalized on the way in and the terms on the way out, so the two
-- agree about where a word ends.
CREATE VIRTUAL TABLE IF NOT EXISTS cabinet_fts USING fts5(
    search,
    {textindex.FTS_TOKENIZE}
);

-- Which document a vector belongs to.  A row here is a *chunk*, not a document
-- -- `vec0` holds one vector per row and a long document averaged into one
-- vector describes all of it a little and none of it well -- and this is what
-- remembers whose chunk it was.  The same arrangement as `turn_chunk`.


CREATE TABLE IF NOT EXISTS cabinet_chunk (
    id          INTEGER PRIMARY KEY,
    doc_id      INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cabinet_chunk_doc ON cabinet_chunk(doc_id);

-- Documents the vector pass has *finished* with, whether or not it placed a
-- chunk.  **"Has no chunk row" cannot answer "have we tried"**: a document with
-- nothing to embed (`vectors` returns none for it) produces no chunks, so the
-- pass that finishes the index would select it again — and, being a loop over
-- what is still missing, would never end.  A row is written by `place`, which is
-- every path that reaches the model, and dropped when the document is removed
-- or the index is rebuilt.
CREATE TABLE IF NOT EXISTS cabinet_embedded (
    doc_id INTEGER PRIMARY KEY
);

-- What the two indexes were built with, so a changed embedding model or a
-- changed rule is a rebuild rather than a migration.
CREATE TABLE IF NOT EXISTS index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


#: The `cabinet_doc` table's own statement, lifted out of the schema so that a
#: rebuild can create today's version of it under another name.  **Derived
#: rather than written twice**: its `CHECK` is built from `KINDS`, and a second
#: copy of the statement would be the copy that goes stale.
_DDL = _SCHEMA[
    _SCHEMA.index("CREATE TABLE IF NOT EXISTS cabinet_doc") : _SCHEMA.index(
        ");", _SCHEMA.index("CREATE TABLE IF NOT EXISTS cabinet_doc")
    )
    + 2
]

#: What a rebuild calls the table while its rows are being moved.
_REBUILDING = "cabinet_doc_rebuilding"


def store_of(path: Path) -> CabinetStore:
    """The cabinet at *path*, opened — `slife2.libs.store.db.store_for`'s counterpart.

    A function rather than a constructor call at the site, because the site is a
    thread hop (`asyncio.to_thread`) and what a hop needs is one callable taking
    one argument.  Opening is what creates the schema, so this is also what
    brings a cabinet into existence.
    """
    return CabinetStore(path)


#: Indexes already reported as predating a kind this build knows.  Reported once
#: per process per file, for the reason `slife2.libs.store.db` keeps the same set:
#: the store is built on every call, and the alternative is the same complaint
#: on every turn.
_older_reported: set[Path] = set()


def _report_older_index(connection: sqlite3.Connection, path: Path) -> bool:
    """Bring an index created before a kind this build knows up to date.

    **A table rebuild, because a `CHECK` cannot be altered.**  `kind` carries one
    listing the kinds, and `CREATE TABLE IF NOT EXISTS` leaves a table that
    already exists exactly as it was — so an index written when there were four
    kinds *refuses* a dump, and refuses it deep inside a transaction, in SQLite's
    own words about a constraint.  Nothing is wrong with that file except its
    age.

    The rows are the truth and the columns have not changed, so this is a copy:
    the table is created again under the name this build's schema gives it, the
    rows are moved **carrying their `id`** — the keyword index and the vectors
    are keyed by it, so a renumbering would leave every document findable by
    words it does not have — and the old table is dropped.  The schema is then
    re-run, which is what puts the indexes back: dropping a table takes its
    indexes with it.

    A file that is already current costs one read of `sqlite_master`, and a
    rebuild is reported, because it is a change to somebody's file and the one
    thing it may not be is silent.  Answers whether it did anything, so the
    caller can say so.
    """
    found = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cabinet_doc'"
    ).fetchone()
    if found is None:
        return False
    missing = [name for name in KINDS if f"'{name}'" not in str(found[0])]
    if not missing:
        return False

    statement = _DDL.replace("cabinet_doc", _REBUILDING, 1)
    connection.execute(statement)
    connection.execute(f"INSERT INTO {_REBUILDING} SELECT * FROM cabinet_doc")
    connection.execute("DROP TABLE cabinet_doc")
    connection.execute(f"ALTER TABLE {_REBUILDING} RENAME TO cabinet_doc")
    connection.executescript(_SCHEMA)
    if path not in _older_reported:
        _older_reported.add(path)
        logger.warning(
            "the cabinet index %s was created before %s and has been rebuilt to "
            "hold it — one copy of its rows, with their ids, and its indexes "
            "recreated",
            path,
            ", ".join(missing),
        )
    return True


@dataclass
class CabinetStore:
    """One agent's cabinet: the index, and the mirrors beside it."""

    path: Path
    #: The cabinet directory, which is where the mirrors and a saved file's
    #: bytes go.  Derived from the index's own path for the reason `index_path`
    #: gives — the two are one directory, and this is the one place that says so.
    root: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root = self.path.parent
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            # An index this build has outgrown is rebuilt here rather than
            # refused later: see `_report_older_index`.
            _report_older_index(connection, self.path)

    def _connect(self) -> sqlite3.Connection:
        return connect_to(self.path)

    # ── Bringing the index up to date ────────────────────────────────────

    async def sync(self, embedder: Embedder) -> dict[str, Any]:
        """Make both indexes this embedder's, or say what is missing.

        **One pass, and it is a rebuild rather than a catch-up.**  v1's drainer
        remembered which documents had no vector and embedded them on its own
        schedule; here a document can only be missing from the index when the
        index belongs to a different embedder or was built by different rules --
        because every write embeds in the same transaction, and a vector is
        dropped only by the branch below.  So the question "does this index need
        rebuilding" is one read of `index_meta`, and the answer is a *rebuild*
        of everything rather than a hunt for holes.

        A write that fails leaves no vector, and that is a real hole -- so the
        last thing this does is look for one (`missing`) and report it, which is
        what makes a failed save visible instead of a document that is quietly
        unreachable by meaning.

        Returns the same shape `slife2.libs.store.db`'s `index_status` does: `ready` and
        the `problems` that say why not.

        **Every blocking step hops off the event loop, and it is this method's
        job rather than its caller's.**  This is the pass that runs when a
        cabinet is *opened*, and what it can be asked to do is a whole-index
        rebuild: DDL, a delete-and-reindex of every row's keyword text, and an
        embed of every document with no vector.  On the loop that is a shared
        process not answering anything else until it finishes — the first call
        from one agent holding up every other agent's turn — and the plugin's
        own `off_loop` cannot reach it, because the embedder is awaited in the
        middle and a thread cannot await.  So this is `TurnStore.sync_indexes`'
        shape, one step at a time: the store hops each blocking step itself and
        keeps the awaits — `to_thread` is `async`, so an inline `await` of a
        blocking call is exactly the bug, and the hop has to be spelled out per
        step.
        """
        identity = vector_identity(embedder)
        problems: list[str] = []
        text_stale = await asyncio.to_thread(
            self._mark_stale, identity, embedder.dimension
        )
        if text_stale:
            await asyncio.to_thread(self._rebuild_text_index)

        # **Every document, not one batch of them.**  `embed_missing` is bounded
        # per call so that a call cannot embed a thousand documents before it
        # answers, so the loop that *finishes* the index belongs here — with the
        # same shape `TurnStore.sync_indexes` uses, and for the same reason.
        # Without it a changed embedding model left every document past the first
        # batch with no vector, reported it once, and never tried again: the
        # cabinet answered meaning queries about part of its corpus, silently,
        # for as long as the process lived.
        #
        # **And it terminates, which is the other half of the same rule.**  The
        # set `embed_missing` walks is "not yet tried" rather than "has no
        # chunk", because a document with nothing to embed never gets one — so
        # a loop over chunks would hand back the same document for ever.
        while True:
            _, done = await self.embed_missing(embedder)
            if not done:
                break

        # **And the pass is checked, because "tried" is not "has a vector".**
        # This was declared and never filled, so `ready` was `True` whatever the
        # index held: a document the vector pass had finished with and the index
        # does not hold — the state a failed placement leaves, and one the pass
        # cannot reach itself, since `cabinet_embedded` is exactly what stops it
        # being selected again — was silently absent from the meaning leg while
        # the store reported itself whole.  That is the report `cabinet.py` logs
        # on startup, and the reason the docstring calls this the last thing the
        # pass does.  A document with nothing to embed is not one of these; see
        # `_documents_without_vectors`.
        missing = await asyncio.to_thread(self._documents_without_vectors)
        documents = await asyncio.to_thread(self.count)
        if missing:
            problems.append(f"{len(missing)} of {documents} documents have no vector")
        return {
            "ready": not problems,
            "problems": problems,
            "identity": identity,
            "documents": documents,
        }

    def _mark_stale(self, identity: str, dimension: int) -> bool:
        """Drop a vector index built for another model, and say if the text is stale.

        One connection, one transaction, and the *blocking* half of `sync` — the
        DDL that rebuilding the vector index is made of, which is why `sync`
        calls it on a worker thread.  It answers with the one fact the caller
        needs afterwards: whether the keyword index was built by different rules
        (`textindex.RULES_VERSION`) and so has to be rebuilt too.
        """
        with self._connect() as connection:
            recorded = index_meta(connection)
            text_stale = recorded.get("text_version", "") != textindex.RULES_VERSION
            # The vectors are a function of the model *and* of the text they were
            # made from, so a change to either makes every stored vector the
            # wrong vector for its row.  Dropped rather than kept beside the new
            # ones: two models' vectors cannot be ranked against each other.
            #
            # `slife2.libs.store.index.rebuild_vector_index` is where that happens — the
            # same call the turn store and the catalogue make, with the one thing
            # this corpus adds to it: `cabinet_embedded` goes with the vectors,
            # because a rowid a `vec0` table has freed is reused and a document
            # that took an old id would read as one already tried.
            if rebuild_vector_index(
                connection,
                vec_table="cabinet_vec",
                chunk_table="cabinet_chunk",
                dimension=dimension,
                identity=identity,
                also_delete=("cabinet_embedded",),
            ):
                logger.info("cabinet %s: rebuilding the vector index", self.path)
        return text_stale

    def _documents_without_vectors(self) -> list[int]:
        """Documents the vector pass finished with and the index does not hold.

        `TurnStore._turns_without_vectors` read against a table that remembers
        what it has *tried*: the pass stops asking about a document the moment
        `cabinet_embedded` names it, so a row there with no `cabinet_chunk` is
        one nothing will ever embed again — a hole that is permanent, and the
        one an index cannot notice on its own, because the identity it compares
        is the identity it was written with.

        **A document with nothing to embed is not one of these.**  `vector_text`
        is empty for a note whose body is empty — a document whose whole text is
        an empty string is one the keyword leg still finds — and no model can be
        asked for a vector of nothing.  Counted here it would be a problem no
        sync could ever clear, which is the bug `_turns_without_vectors`'
        docstring describes on the turn side: the plugin refuses to serve an
        index that is not ready, so one empty note would keep the cabinet down.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, kind, key, body, title, summary, tags, source, file_path"
                " FROM cabinet_doc"
                " WHERE id IN (SELECT doc_id FROM cabinet_embedded)"
                " AND id NOT IN (SELECT doc_id FROM cabinet_chunk)"
            ).fetchall()
        return [int(row["id"]) for row in rows if vector_text(dict(row)).strip()]

    async def embed_missing(
        self, embedder: Embedder, *, limit: int | None = None
    ) -> tuple[int, int]:
        """Embed documents the vector index does not hold.  `(remaining, done)`.

        **Every one of them, a batch at a time** — the caller loops until the
        first number is zero.  Batching is what keeps one call from embedding a
        thousand documents before it answers, and it is why this can be called
        from anywhere rather than only from `sync`: a document whose embedding
        failed at save time is found by the same query.

        `(remaining, done)`: how many are still to try, and how many this call
        tried.  `remaining` is the count *after* the batch, read from the same
        connection that wrote it, so it cannot disagree with itself.

        **"Still to try" is `cabinet_embedded`, not "has no chunk."**  A document
        with nothing to embed places no chunk and never will, so counting chunks
        would hand it back for ever and the caller's loop would not end.

        **Its SQLite hops off the loop, one call at a time.**  This is the loop
        `sync` runs at open, and the documents it walks are the whole corpus the
        first time a changed model makes it run: a selection, a placement per
        document and a count, all of it blocking, all of it on the loop unless
        each one is sent to a worker (`sync` states why that is this side's job
        and not its caller's).  The embed between two of them is the one step
        that stays on the loop, because it is the one step that is already
        awaited.
        """
        limit = limit or SYNC_BATCH  # read here, so a test can size the batch
        rows = await asyncio.to_thread(self._unembedded, limit)
        if not rows:
            return 0, 0
        done = 0
        for row in rows:
            one = dict(row)
            vectors = await self.vectors(one, embedder)
            await asyncio.to_thread(self._place_one, int(one["id"]), vectors)
            done += 1
        remaining = await asyncio.to_thread(self._unembedded_count)
        return remaining, done

    def _unembedded(self, limit: int) -> list[sqlite3.Row]:
        """One batch of documents the vector pass has not tried yet.

        The selection `embed_missing` walks, blocking and therefore on a worker
        thread: `cabinet_embedded` is the marker, and "has no chunk" is a
        question that never ends — see that method for why.
        """
        with self._connect() as connection:
            return connection.execute(
                "SELECT id, kind, key, body, title, summary, tags, source, file_path"
                " FROM cabinet_doc"
                " WHERE id NOT IN (SELECT doc_id FROM cabinet_embedded)"
                " ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()

    def _unembedded_count(self) -> int:
        """How many documents are still to try, read after the batch was written.

        From its own connection, so it cannot disagree with what was just placed:
        the count is what `embed_missing` answers the caller's loop with, and a
        loop that stopped one batch early would leave the tail of the corpus out
        of the meaning leg in silence.
        """
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM cabinet_doc"
                    " WHERE id NOT IN (SELECT doc_id FROM cabinet_embedded)"
                ).fetchone()["n"]
            )

    def _place_one(self, doc_id: int, vectors: Sequence[Sequence[float]]) -> None:
        """Put one document's vectors in, on a worker thread and its own connection."""
        with self._connect() as connection:
            self.place(connection, doc_id, vectors)

    def _rebuild_text_index(self) -> None:
        """Rebuild the keyword index, whole, from the rows.

        The one case it can be wrong is the rules that built it having changed
        (`textindex.RULES_VERSION`) — a row cannot otherwise be missing from it,
        because the two are written together.  A file with no stamp at all is
        that case rather than an older file, which is why there is no migration
        here: a cabinet this build cannot read is reported by `sync`, not
        quietly upgraded.
        """
        with self._connect() as connection:
            connection.execute("DELETE FROM cabinet_fts")
            rows = connection.execute(
                "SELECT id, kind, key, title, body, tags, source, file_path, summary"
                " FROM cabinet_doc"
            ).fetchall()
            connection.executemany(
                "INSERT INTO cabinet_fts (rowid, search) VALUES (?, ?)",
                [(int(row["id"]), search_text(dict(row))) for row in rows],
            )
            set_index_meta(connection, text_version=textindex.RULES_VERSION)

    async def vectors(
        self, row: Mapping[str, Any], embedder: Embedder
    ) -> list[list[float]]:
        """One document's vectors, out of the model — **before anything is written**.

        The order is the contract: `write` calls this first, so a save that
        cannot embed has stored nothing yet, and the branch that has to undo
        something (the plugin's copy of a file) has only its own work to undo.
        v1's drainer is what made that order unnecessary — an unembedded document
        was simply caught up later — and slife2 has no drainer, so a save that
        cannot embed is a save that did not happen.

        An empty text is no vectors rather than an empty embedding: a document
        with nothing to say about itself is one the keyword leg still finds.

        **And an answer shorter than the request is refused** — the one thing
        every other embedding path in the tree checked and this one did not.  It
        is the worst place to miss it: `place` writes one `cabinet_chunk` row per
        vector it is handed and marks the document tried either way, so a short
        answer left the document holding vectors for *some* of its chunks,
        `cabinet_embedded` saying the vector pass had finished with it, and
        `embed_missing` therefore never selecting it again — the rest of the
        document unreachable by meaning, with `sync` reporting the cabinet
        ready.  `checked_vectors` is the check
        and the sentence; the corpus noun and what is lost are this caller's.
        """
        text = vector_text(row)
        pieces = embed_chunks(text, limit=embedder.max_chars) if text.strip() else []
        if not pieces:
            return []
        return await checked_vectors(
            embedder,
            pieces,
            unit="chunks",
            so="this document would be searchable only by keyword",
        )

    def place(
        self,
        connection: sqlite3.Connection,
        doc_id: int,
        vectors: Sequence[Sequence[float]],
    ) -> None:
        """Put one document's vectors in, replacing whatever it had.

        The chunks are rebuilt rather than appended to, because this is the one
        path that can be reached twice for one row: a first attempt that failed
        halfway would otherwise leave the row with a third of its vectors and no
        sign of it.

        The clear and the write are `slife2.libs.store.index`'s, with the marker this
        corpus keeps in between them — see below, and `place_vectors` for why a
        chunk's vector goes with the chunk and in that order.

        **The document is marked as tried either way**, because this is every
        path that reaches the model — so it is also what `sync` reads to decide
        what is left.  See `cabinet_embedded`.
        """
        self._clear_vectors(connection, doc_id)
        connection.execute(
            "INSERT OR REPLACE INTO cabinet_embedded (doc_id) VALUES (?)", (doc_id,)
        )
        place_vectors(
            connection,
            chunk_table="cabinet_chunk",
            vec_table="cabinet_vec",
            key_column="doc_id",
            row_key=doc_id,
            vectors=vectors,
            what=f"a chunk of document {doc_id}",
        )

    @staticmethod
    def _clear_vectors(connection: sqlite3.Connection, doc_id: int) -> None:
        """Forget one document's vectors, its chunks, and that it was tried.

        The marker goes with them because a deleted rowid is reused: without
        this, a new document taking the old id would be read as one the vector
        pass had already finished with, and would never be embedded.  That is
        the `also_delete` `slife2.libs.store.index.clear_vectors` takes, and this is its
        only caller that has one.
        """
        clear_vectors(
            connection,
            chunk_table="cabinet_chunk",
            vec_table="cabinet_vec",
            key_column="doc_id",
            keys=[doc_id],
            also_delete=(("cabinet_embedded", "doc_id"),),
        )

    def count(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute("SELECT COUNT(*) AS n FROM cabinet_doc").fetchone()[
                    "n"
                ]
            )

    # ── Writing ─────────────────────────────────────────────────────────

    async def write(
        self,
        kind: str,
        key: str,
        *,
        body: str = "",
        title: str = "",
        tags: str = "",
        source: str = "",
        file_path: str = "",
        mime: str = "",
        size: int = 0,
        summary: str = "",
        period_start: str = "",
        period_end: str = "",
        embedder: Embedder,
    ) -> dict[str, Any]:
        """Write one document, its mirror and its vector.  One operation.

        **The append kinds append and the rest replace**, which is `SPECS`'s
        rule and the whole difference between a note and a day: two writes under
        one date are two facts about it, and nothing in the later text says
        which part of the earlier one it replaces.  A report's title is the same
        kind of key for the same reason.  What appends also *replaces the key's
        title and tags* rather than appending them, because those are the
        document's own and not part of the log.

        **Nothing is written until the embedding is in hand.**  The model is
        asked first (`vectors`), and only then does one transaction write the
        row, its keyword text and its vectors — so a write that cannot embed
        raises having stored nothing, which is what the plugin's `file_save`
        relies on when it takes back the bytes it copied.

        **And an append is composed against the body that is in the row when it
        is written.**  A read here and a write there with a model call between
        them is the shape `append_context` exists to forbid: two writers that
        both read the old day and both write `old + own` lose one of the two
        entries, and the second writer is the ordinary case rather than a race —
        the plugin hops to a thread between the halves, so a wechat turn and a
        tui turn for one agent interleave.  So the row is written only if it
        still holds the body this call composed from (`_replace`'s `expected`),
        and a call that lost that race composes again against the body that won:
        what re-runs is the read, the compose and the embedding, because the
        vector has to be a vector *of* the text that ends up in the row and the
        model is asked before the write rather than inside it.
        """
        spec = SPECS.get(kind)
        if spec is None:
            raise ValueError(_KIND_HINT)
        stamp = now()
        # **A log's every write is a section, its first included**, so a
        # day's file reads the same way from the top as it does after the
        # fiftieth entry — and so nothing downstream has to know which write
        # happened to be the one that created the row.
        added = _section(stamp, body) if spec.appends else body
        while True:
            with self._connect() as connection:
                found = connection.execute(
                    "SELECT * FROM cabinet_doc WHERE kind = ? AND key = ?", (kind, key)
                ).fetchone()
            existing = dict(found) if found is not None else None
            row: dict[str, Any] = dict(existing) if existing is not None else {}
            if spec.appends and existing is not None:
                row["body"] = f"{existing.get('body') or ''}\n{added}".strip("\n")
            else:
                row["body"] = added
            row.update(
                kind=kind,
                key=key,
                title=title or row.get("title") or key,
                tags=tags or row.get("tags") or "",
                summary=summary or row.get("summary") or "",
                source=source or row.get("source") or "",
                file_path=file_path or row.get("file_path") or "",
                mime=mime or row.get("mime") or "",
                size=size or int(row.get("size") or 0),
                period_start=period_start or row.get("period_start") or "",
                period_end=period_end or row.get("period_end") or "",
                created_at=row.get("created_at") or stamp,
                updated_at=stamp,
            )
            if spec.mirror and not row.get("mirror"):
                row["mirror"] = mirror_path(spec, key)
            # **Embedded before anything is written**, so a write that cannot
            # embed has written nothing — see `vectors`.  What the row *is* does
            # not depend on the vectors, so this is a question asked of the model
            # about the document rather than a step in storing it.
            vectors = await self.vectors(row, embedder)
            with self._connect() as connection:
                placed = self._replace(
                    connection,
                    row,
                    expected=existing.get("body")
                    if spec.appends and existing
                    else None,
                )
                if placed is None:
                    # Another writer got there between this call's read and its
                    # write.  Recomposed rather than reported: both writes are
                    # facts about the same day, and the caller asked for this one
                    # to be in it.
                    continue
                row["id"] = placed
                self._index(connection, row)
                self.place(connection, placed, vectors)
            break
        if spec.mirror:
            # Last, because a file cannot be inside the database's transaction.
            # A mirror that fails leaves a row whose markdown is missing, and the
            # next write of the same key writes it — which is the lesser of the
            # two failures: markdown with no row is a document nothing can find.
            _write_mirror(
                self.root,
                str(row["mirror"]),
                _mirror_text(kind, row, added, first=existing is None),
                append=bool(spec.appends and existing is not None),
            )
        return row

    def _replace(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
        *,
        expected: str | None = None,
    ) -> int | None:
        """The row, written whole, and its id — or `None` if it lost a race.

        *expected* is the body this call composed its own body from, and it is
        what makes an append a compare-and-set: the `UPDATE` matches only while
        the row still holds that text, so a second writer's entry cannot be
        overwritten by a body composed before it arrived.  `None` means there is
        nothing to compare — a kind that replaces is *meant* to overwrite
        whatever is there, and a first write has no old body to lose.

        An insert that collides answers `None` too, and for the same reason: the
        row appeared under this key after this call read for it, which is the
        race seen from the other side.  The collision is checked against the key
        rather than trusted, so a constraint that fails for any *other* reason is
        raised instead of retried for ever.
        """
        columns = (
            "kind",
            "key",
            "title",
            "body",
            "tags",
            "source",
            "file_path",
            "mime",
            "size",
            "summary",
            "period_start",
            "period_end",
            "created_at",
            "updated_at",
            "mirror",
        )
        values = [
            storable(str(row.get(column) or ""))
            if column != "size"
            else int(row.get("size") or 0)
            for column in columns
        ]
        if row.get("id"):
            where = "WHERE id = ?"
            guard: list[Any] = []
            if expected is not None:
                where += " AND body = ?"
                guard.append(expected)
            placed = connection.execute(
                f"UPDATE cabinet_doc SET {', '.join(f'{c} = ?' for c in columns)}"
                f" {where}",
                (*values, int(row["id"]), *guard),
            )
            return int(row["id"]) if placed.rowcount else None
        try:
            placed = connection.execute(
                f"INSERT INTO cabinet_doc ({', '.join(columns)})"
                f" VALUES ({', '.join('?' * len(columns))})",
                values,
            )
        except sqlite3.IntegrityError:
            if (
                connection.execute(
                    "SELECT 1 FROM cabinet_doc WHERE kind = ? AND key = ?",
                    (row.get("kind"), row.get("key")),
                ).fetchone()
                is None
            ):
                raise
            return None
        return _rowid(placed, what=f"the {row.get('kind')} {row.get('key')!r}")

    def _index(self, connection: sqlite3.Connection, row: Mapping[str, Any]) -> None:
        """Write the row's keyword text, replacing what was there.

        In the same transaction as the row, which is what makes the two
        impossible to disagree — the reason this module needs no trigger.
        """
        connection.execute("DELETE FROM cabinet_fts WHERE rowid = ?", (int(row["id"]),))
        connection.execute(
            "INSERT INTO cabinet_fts (rowid, search) VALUES (?, ?)",
            (int(row["id"]), search_text(row)),
        )

    async def remove(self, kind: str, key: str) -> dict[str, Any] | None:
        """Delete one document: its vector, its row, its index entry, its mirror.

        **Only the two kinds with one current thing per key have this**, which is
        `SPECS.appends` read from the other side: a day and a report accumulate,
        so there is no "the" version of one to delete.  The bytes of a saved file
        go too — and never what it was copied *from*, which `source` records and
        which this has no business touching.
        """
        spec = SPECS.get(kind)
        if spec is None:
            raise ValueError(_KIND_HINT)
        if spec.appends:
            raise ValueError(
                f"a {kind} is a log and has nothing to remove — it accumulates"
            )
        with self._connect() as connection:
            found = connection.execute(
                "SELECT * FROM cabinet_doc WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()
            if found is None:
                return None
            row = dict(found)
            connection.execute(
                "DELETE FROM cabinet_fts WHERE rowid = ?", (int(row["id"]),)
            )
            self._clear_vectors(connection, int(row["id"]))
            connection.execute(
                "DELETE FROM cabinet_doc WHERE id = ?", (int(row["id"]),)
            )
        if mirror := str(row.get("mirror") or ""):
            (self.root / mirror).unlink(missing_ok=True)
        if kind in (FILE, IMAGE) and (saved := str(row.get("file_path") or "")):
            discard_file(self.root / saved)
        return row

    async def annotate(
        self,
        kind: str,
        key: str,
        *,
        summary: str = "",
        tags: str = "",
        embedder: Embedder,
    ) -> dict[str, Any] | None:
        """`cabinet_summarize`: a model's description of a row already saved.

        **A file or an image is re-embedded and the text kinds are not**, which
        is the one asymmetry DESIGN.md §5.3 states: a summary is part of the text
        a file's (or an image's) vector is built from, since neither has text of
        its own, so the stored vector is now the wrong vector for its row and
        has to be replaced.  A note's vector is its body, which a summary does
        not change — and rebuilding it anyway would be spending an embedding
        call to store the same vector.

        **The re-embedding happens before anything is written**, which is
        `write`'s order and not a detail: the summary was committed first and
        the vector second, so a `cabinet_summarize` that could not reach the
        model left the row holding the new summary, its keyword text rewritten
        and its vector still describing the text without it.  Nothing could
        repair that — `cabinet_embedded` marks a document the vector pass has
        *finished* with, and it has chunks, so `sync`/`embed_missing` never
        select it again: the words were permanently unreachable by meaning and
        the cabinet reported itself ready.  A failed re-embed now raises having
        written nothing, the same contract every other write in this module
        keeps.
        """
        spec = SPECS.get(kind)
        if spec is None:
            raise ValueError(_KIND_HINT)
        with self._connect() as connection:
            found = connection.execute(
                "SELECT * FROM cabinet_doc WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()
        if found is None:
            return None
        row = dict(found)
        row["summary"] = summary or row.get("summary") or ""
        row["tags"] = tags or row.get("tags") or ""
        row["updated_at"] = now()
        # A file's (and an image's) vector is of its identity *including* the
        # summary (`vector_text`), so the model is asked about the row as it
        # will be — and asked here, before the `UPDATE`, because a summary that
        # cannot be embedded is a summary that is not written.
        vectors = await self.vectors(row, embedder) if kind in (FILE, IMAGE) else None
        with self._connect() as connection:
            connection.execute(
                "UPDATE cabinet_doc SET summary = ?, tags = ?, updated_at = ? WHERE id = ?",
                (
                    storable(str(row["summary"])),
                    storable(str(row["tags"])),
                    row["updated_at"],
                    int(row["id"]),
                ),
            )
            self._index(connection, row)
            if vectors is not None:
                self.place(connection, int(row["id"]), vectors)
        return row

    # ── Reading ─────────────────────────────────────────────────────────

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        """One document by its kind and key, or `None`."""
        with self._connect() as connection:
            found = connection.execute(
                "SELECT * FROM cabinet_doc WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()
        return dict(found) if found is not None else None

    def listing(
        self,
        kind: str,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
        category: str = "",
    ) -> dict[str, Any]:
        """One kind's documents, newest first by that kind's own time axis.

        The envelope is `turn_list`'s — `total`, `limit`, `offset` and the rows
        — because two listings in one system that disagree about how a page is
        described are two things a model has to learn.  **Each entry is a
        `choice_row` and not a document**: a page is a thing to choose from, and
        the whole of one is what a read tool is for.  `category` narrows to a
        directory of saved files, which is the one filter a kind has that the
        others do not (v1's `file_list`).

        `total` is counted before the page is cut, so "there are 40 and here are
        10" is answerable rather than a model guessing from a full page.
        """
        spec = SPECS.get(kind)
        if spec is None:
            raise ValueError(_KIND_HINT)
        clauses, values = self._window(spec, since, until)
        if category:
            # **Escaped, or the filter is not the query it says it is.**  A
            # category is a folder name and `LIKE` reads two of the characters a
            # folder name may hold as wildcards: `my_notes` matched `myXnotes`
            # (the underscore is any one character) and a `%` matched anything at
            # all.  Bound rather than interpolated, so it was never injection —
            # it was a listing that answered about a folder nobody named.
            clauses.append(r"file_path LIKE ? ESCAPE '\'")
            values.append(f"{_like_literal(category)}/%")
        where = filter_clause(["kind = ?", *clauses])
        limit = page_limit(limit, MAX_PAGE)
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) AS n FROM cabinet_doc {where}", (kind, *values)
                ).fetchone()["n"]
            )
            rows = connection.execute(
                f"SELECT * FROM cabinet_doc {where}"
                f" ORDER BY {spec.time} DESC, id DESC LIMIT ? OFFSET ?",
                (kind, *values, limit, max(0, offset)),
            ).fetchall()
        return {
            "total": total,
            "limit": limit,
            "offset": max(0, offset),
            "entries": [choice_row(dict(row)) for row in rows],
        }

    def _window(
        self, spec: Spec, since: str | None, until: str | None
    ) -> tuple[list[str], list[Any]]:
        """A since/until window on **this kind's** time axis.

        Not `slife2.libs.store.index.time_window`, which is spelled for a `created_at` column
        because both of its callers have one; a cabinet's kinds use their own
        axes and v1 made the same distinction.  The bound grammar is
        the shared one (`slife2.helpers.timeutil`), so a model writes `yesterday` here
        as it does anywhere else.

        A **day** axis takes the day part of the bound, and its `until` is
        exclusive because that is the only comparison a column of days has:
        `2026-10-07` becomes `2026-10-08`, so the day named is the last one in.
        **A bound that carries a time has to be put past its day here**, because
        `normalize_bound` only advances a *date-only* `until` — that advance is
        written for a column holding a moment, where the bound is compared with
        `<=` and means the instant.  Left unadvanced, `until=2026-10-07T12:00:00`
        cut `< '2026-10-07'` and dropped the very day it names: the same day
        written two ways was two different answers, and a window whose two bounds
        were the same moment returned nothing at all.
        """
        clauses: list[str] = []
        values: list[Any] = []
        if since:
            bound = normalize_bound(since, role="since")
            clauses.append(f"{spec.time} >= ?")
            values.append(bound[:10] if spec.day else bound)
        if until:
            bound = normalize_bound(until, role="until")
            if spec.day:
                day = date.fromisoformat(bound[:10])
                bound = (day + timedelta(days=1) if "T" in bound else day).isoformat()
            clauses.append(f"{spec.time} {'<' if spec.day else '<='} ?")
            values.append(bound)
        return clauses, values

    # ── Searching ───────────────────────────────────────────────────────

    async def search(
        self,
        *,
        keywords: Sequence[str] = (),
        sentences: Sequence[str] = (),
        kind: str = "",
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
        embedder: Embedder,
    ) -> dict[str, Any]:
        """Documents matching the *words* and the *sentences* given, best first.

        **Two inputs, because the two legs want different things** — the same
        rule and the same names `tool_search` uses (`ToolStore.search`), because
        a model that has learned one should not have to learn the other.  The
        words go to the keyword leg, which asks for all of them at once; meaning
        goes to the semantic leg.  Either may be empty, and both empty is a
        browse: everything, newest first, which is what a caller who named a
        kind and nothing else is asking for.  **Dumps are the one kind a browse
        and a search that named none do not reach** — see the module docstring.

        **One corpus, whatever `kind` says.**  A filter narrows what is *shown*
        and never what is ranked, because a rank only means something inside the
        corpus that produced it (DESIGN.md §5.3): one index per kind would be one corpus per
        kind, and a query spanning them would answer with an order nothing
        measured.

        The window is the one `listing` uses, per kind — and with no kind it is
        the *document's* `updated_at`, which every kind has, because a window
        has to have a column and "when was this written" is the only axis every
        kind shares.
        """
        limit = page_limit(limit, MAX_PAGE)
        if kind and kind not in SPECS:
            raise ValueError(_KIND_HINT)
        clauses: list[str] = []
        values: list[Any] = []
        if kind:
            clauses.append("d.kind = ?")
            values.append(kind)
        else:
            # **A dump is not what a search that named no kind is for**, for the
            # reason the module docstring gives: nobody wrote it, it matches on
            # every word it happens to contain, and the address a model has for
            # one is the marker in the transcript.  Naming the kind reaches it.
            clauses.append("d.kind != ?")
            values.append(DUMP)
        if since or until:
            spec = (
                SPECS[kind]
                if kind
                else Spec(time="updated_at", day=False, appends=False, mirror="")
            )
            window, bounds = self._window(spec, since, until)
            clauses.extend(window)
            values.extend(bounds)
        where = filter_clause(clauses)

        if not keywords and not any(sentence.strip() for sentence in sentences):
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT d.* FROM cabinet_doc d {where}"
                    f" ORDER BY d.updated_at DESC, d.id DESC LIMIT ?",
                    (*values, limit),
                ).fetchall()
            return {"results": [_found(dict(row)) for row in rows], "browsed": True}

        expression = textindex.match_expression(list(keywords)) if keywords else ""
        keyword_hits = (
            self._keyword_hits(expression, limit * 4, clauses, values)
            if keywords
            else None
        )

        # **The shortlist, one list per question.**  `keywords` are what the
        # caller has in mind, so where there is a sentence it is that sentence's
        # other leg and is fused into that question — a row both legs found is
        # evidence *about that question*.  The questions are then unioned and the
        # page ordered once, by a rule the caller can read off it.
        #
        # **And the union is capped**, exactly as `ToolStore.search` caps its
        # own: nothing the caller passes bounds it (each sentence brings up to
        # `limit * 4` of its own ids, and the keyword leg as many again), while
        # the query that reads the rows binds one value per id and SQLite
        # refuses a statement with too many.  What is dropped is the tail of the
        # union, and the page is `limit` rows.
        phrases = [(index, s) for index, s in enumerate(sentences) if s.strip()]
        queries, lists = await self._semantic_lists(sentences, embedder, limit * 4)
        meaning: dict[int, float] = {}
        shortlist: list[int] = []
        seen: set[int] = set()
        for (index, _), hits in zip(phrases, lists, strict=True):
            legs = {f"sentence:{index}": [rowid for rowid, _ in hits]}
            if keyword_hits is not None:
                legs["keyword"] = keyword_hits
            for rowid, _ in fuse_ranked(legs):
                if rowid not in seen:
                    seen.add(rowid)
                    shortlist.append(rowid)
            for rowid, measured in hits:
                meaning[rowid] = max(meaning.get(rowid, NO_SIMILARITY), measured)
        if keyword_hits is not None and not shortlist:
            shortlist = list(dict.fromkeys(keyword_hits))
        shortlist = shortlist[:MAX_SQL_VARS]
        for rowid, value in self._meaning_of(
            [rowid for rowid in shortlist if rowid not in meaning], queries
        ).items():
            meaning[rowid] = max(meaning.get(rowid, NO_SIMILARITY), value)

        # Words first, then meaning — `ToolStore.search`'s rule, and the reason
        # the column a reader sees is the column that ordered the page: a cosine
        # cannot separate a cluster, and the word match knows which row was named.
        # `NO_SIMILARITY` and not `0.0` for a row nothing measured, which is the
        # same sentinel the catalogue uses: a cosine is a signed number, so a row
        # measured at -0.4 would otherwise sort *above* one nobody looked at.
        worded = set(keyword_hits or ())
        ordered = sorted(
            shortlist,
            key=lambda rowid: (
                rowid not in worded,
                -meaning.get(rowid, NO_SIMILARITY),
            ),
        )
        with self._connect() as connection:
            rows = self._rows_in_order(connection, ordered, clauses, values, limit)
        page = [int(row["id"]) for row in rows]
        inline = self._matched_in_line(expression, page) if worded else set()
        return {
            "results": [
                _found(
                    dict(row),
                    meaning.get(int(row["id"])),
                    matched_in=(
                        None
                        if int(row["id"]) not in worded
                        else "line"
                        if int(row["id"]) in inline
                        else "rest"
                    ),
                )
                for row in rows
            ]
        }

    def _keyword_hits(
        self, expression: str, limit: int, clauses: list[str], values: list[Any]
    ) -> list[int]:
        """Document ids by `bm25`, best first, with the caller's filters applied.

        **The `MATCH` is the query**, and leaving it out is not a syntax error:
        measured here, a version that passed the expression as a *parameter*
        whose placeholder was missing bound it to `LIMIT` instead and answered
        with every document in the cabinet, ranked — a search that looks like it
        worked and returns the whole corpus.  The filters ride in the same
        `WHERE` for the reason the catalogue's do: `rank` orders every match, so
        cutting to the limit afterwards still returns the best rows *inside* the
        filter.
        """
        extra = where_clause(clauses)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT f.rowid AS rowid FROM cabinet_fts f"
                f" JOIN cabinet_doc d ON d.id = f.rowid"
                f" WHERE cabinet_fts MATCH ? {extra}"
                f" ORDER BY bm25(cabinet_fts) LIMIT ?",
                (expression, *values, limit),
            ).fetchall()
        return [int(row["rowid"]) for row in rows]

    async def _semantic_lists(
        self, sentences: Sequence[str], embedder: Embedder, k: int
    ) -> tuple[list[list[float]], list[list[tuple[int, float]]]]:
        """`(the query vectors, one ranked list each)`, out of **one** `embed`.

        The answer is checked here for the reason the catalogue's is, and the
        symptom is why it is worth saying: the caller pairs each sentence with
        its own ranked list, so a vector that never arrived is a question with no
        answer — and an unchecked short answer reached it as a `zip` of two lists
        of different lengths, which names the symptom rather than the fact.
        """
        phrases = [sentence for sentence in sentences if sentence.strip()]
        if not phrases:
            return [], []
        queries = await checked_vectors(
            embedder,
            phrases,
            unit="sentences",
            so="this search cannot say what a document is about",
        )
        with self._connect() as connection:
            lists = [self._nearest(connection, vector, k) for vector in queries]
        return queries, lists

    def _nearest(
        self, connection: sqlite3.Connection, vector: list[float], k: int
    ) -> list[tuple[int, float]]:
        """One query vector's nearest documents, as `(id, similarity)`.

        A KNN counts *chunks*, several of which belong to one document, so the
        first row for a document is the nearest of its chunks and is what it is
        worth — the same rule the tool catalogue's search applies.
        """
        # **Asked, rather than found out by catching the query's failure.**  A
        # cabinet nobody has written to under this embedder has no `cabinet_vec`
        # at all, which is an empty meaning leg rather than a failure — the
        # keyword leg is still an answer, and `sync` is what says the index is
        # incomplete.  A bare `except sqlite3.OperationalError` answered that
        # question and every other one the same way: a malformed vector table, a
        # width this build cannot read, a corrupt file all came back as "nothing
        # matched by meaning", and the page then answered keyword-only without
        # saying so.
        if not table_exists(connection, "cabinet_vec"):
            return []
        rows = connection.execute(
            "SELECT c.doc_id AS doc_id, v.distance AS distance"
            " FROM cabinet_vec v JOIN cabinet_chunk c ON c.id = v.rowid"
            " WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (sqlite_vec.serialize_float32(vector), k),
        ).fetchall()
        found: dict[int, float] = {}
        for row in rows:
            doc_id = int(row["doc_id"])
            if doc_id not in found:
                found[doc_id] = as_similarity(float(row["distance"]))
        return list(found.items())

    def _meaning_of(
        self, ids: list[int], queries: list[list[float]]
    ) -> dict[int, float]:
        """The similarity of rows the meaning leg never returned.

        Left empty when there is no query vector: a browse has no meaning to
        measure, and inventing a number for it would be a page that looks ranked
        and is not.  The rows the *words* found are the ones that matter here —
        they are certain evidence and were the only rows on the page with no
        number at all, which made the strongest evidence read as the weakest.

        **The number is read from the stored vectors, not asked of the index
        again** (`meanings_by_cosine`), which is what `ToolStore._meaning_of`
        does and why this is one loop and not two implementations: the rows here
        are exactly the ones the KNN has already said are not in its page, so a
        second KNN — `k` = `MAX_PAGE`, the *nearest* rows — answered with the
        rows the first page already had and the word-found rows went unmeasured
        anyway.
        """
        if not ids or not queries:
            return {}
        with self._connect() as connection:
            # The table is absent on a cabinet nobody has written to under this
            # embedder, which is an empty answer rather than a failure — the
            # same question `_nearest` asks and for the reason it gives.
            if not table_exists(connection, "cabinet_vec"):
                return {}
            rows = connection.execute(
                "SELECT c.doc_id AS doc_id, v.embedding AS embedding"
                " FROM cabinet_vec v JOIN cabinet_chunk c ON c.id = v.rowid"
                f" WHERE c.doc_id IN ({','.join('?' * len(ids))})",
                tuple(ids),
            ).fetchall()
        return meanings_by_cosine(rows, "doc_id", queries)

    def _rows_in_order(
        self,
        connection: sqlite3.Connection,
        ordered: list[int],
        clauses: list[str],
        values: list[Any],
        limit: int,
    ) -> list[sqlite3.Row]:
        """The fused order, as rows — **in that order**, and filtered.

        `slife2.libs.store.index.rows_in_order`, which is where the ordering, the re-applied
        filters and the cap on the bound list are argued; this store's own part
        is the table's name and the `d` it is aliased to, because the filters it
        passes are written against that alias.
        """
        return rows_in_order(
            connection,
            ordered,
            select="SELECT d.* FROM cabinet_doc d",
            column="d.id",
            key="id",
            clauses=clauses,
            values=values,
            limit=limit,
        )

    def _matched_in_line(self, expression: str, page: list[int]) -> set[int]:
        """Which of the page's rows hold the caller's words in what is printed.

        `matched_in` is `"line"` or `"rest"` and the difference is not a detail:
        a mark whose evidence is not on the page is a mark the reader cannot
        check (`ToolStore._matched_in_line`).  What is printed for a cabinet row
        is the row itself (`line_text`, which is built from `choice_row`), so the
        test is whether the words are in *that* — a smaller text than the row
        was indexed by — and `line_text` is the one spelling of it, derived from
        the row the answer shows rather than written out beside it.

        **And the words are the index's, tested by the index's own tokenizer.**
        A containment test over `textindex.normalize`d Python strings is a second
        tokenizer, and it disagreed with the first exactly where it mattered: the
        index is built by `unicode61 remove_diacritics 2`, which folds `Café` to
        `cafe`, while `normalize` keeps the accent — so a caller asking for
        `cafe` matched a note at the index and was then reported as having
        matched *elsewhere*, with the word plainly on the line it had matched.
        The printed text therefore goes into a temporary FTS5 table built with
        the same tokenizer (`textindex.FTS_TOKENIZE`), and the caller's own
        expression is matched against that: one grammar and one tokenizer on
        both sides, and the scope is the text the line shows.
        """
        if not page or not expression:
            return set()
        placeholders = _marks(page)
        with self._connect() as connection:
            # The whole row, because `line_text` is built from the row the answer
            # shows (`choice_row`) and a column left out here would be one the
            # mark could not see.
            rows = connection.execute(
                f"SELECT * FROM cabinet_doc WHERE id IN ({placeholders})",
                tuple(page),
            ).fetchall()
            connection.execute(
                f"CREATE VIRTUAL TABLE temp.printed USING fts5("
                f"search, {textindex.FTS_TOKENIZE})"
            )
            connection.executemany(
                "INSERT INTO printed (rowid, search) VALUES (?, ?)",
                [
                    (int(row["id"]), textindex.normalize(line_text(dict(row))))
                    for row in rows
                ],
            )
            return {
                int(row["rowid"])
                for row in connection.execute(
                    "SELECT rowid FROM printed WHERE printed MATCH ?", (expression,)
                )
            }


#: How much of a body a chooser shows when a row has no summary.  A chooser is a
#: picker rather than a reader — the whole document is what
#: `note_read`/`diary_read`/`file_read` are for — and the one text it slices is
#: `choice_row`'s `excerpt`, which is the whole of what a listing and a search
#: print of a row.
EXCERPT_CHARS = 120


def line_excerpt(row: Mapping[str, Any]) -> str:
    """What a chooser row shows *under* one row's key: its summary, or its body's head.

    **One spelling of the rule**, because two readers have to agree about it:
    `choice_row` carries it as the row's `excerpt`, and `_matched_in_line` asks
    whether the caller's words are in it.  Written out twice they disagreed —
    the mark was decided against the first 400 characters and the excerpt shown
    stopped at 120, so a note whose word sat at character 300 was marked
    "matched your words" beside an excerpt the words were not in.  The body is
    whitespace-collapsed first, because that is what is shown: a newline in a
    note is not a line the reader sees.
    """
    summary = str(row.get("summary") or "")
    if summary:
        return summary
    body = " ".join(str(row.get("body") or "").split())
    return body[:EXCERPT_CHARS] + ("…" if len(body) > EXCERPT_CHARS else "")


def line_text(row: Mapping[str, Any]) -> str:
    """The text one row shows a reader, as one string.

    **What `_matched_in_line` is allowed to test**, and the whole of it — and the
    rule it keeps is the one this function has always kept: *the haystack is
    what the answer prints, and nothing else*.  A row's `title` was once in a
    haystack beside a line that printed only the key and the excerpt, so a file
    saved with a title and no body answered "matched your words" with every one
    of them missing from the page; the row is now the answer — `choice_row`, in
    JSON — and the title is printed with it.

    So the scope is derived from the row that is shown rather than written out
    again here: a field added to `choice_row` is one this cannot fall behind,
    and there is no second list to keep in step.  A term matching the row's
    *chrome* — its kind, its time, the path of its mirror — is a mark the reader
    can check as readily as any other, which is the whole of what the mark is
    for; a mark they cannot is the failure it exists not to be.
    """
    return "\n".join(str(value) for value in choice_row(row).values())


#: The columns a *kind* has and the others do not: `size`, `mime` and `source`
#: are a saved file's and an image's — what the bytes are and where they came
#: from — and `period_*` a report's.  A kind that cannot have one answers
#: nothing about it rather than answering "0" and "", which a model reads as a
#: fact about the document.
_EXTRA_COLUMNS: dict[str, tuple[str, ...]] = {
    FILE: ("size", "mime", "source"),
    IMAGE: ("size", "mime", "source"),
    REPORT: ("period_start", "period_end"),
}


def facts(row: Mapping[str, Any]) -> dict[str, Any]:
    """One document's own fields, as a model reads them — no text, no excerpt.

    **One place that says what a document is**, shared by every answer that
    describes one: a read adds its text to this, a write answers it, and
    `choice_row` adds the line a chooser shows.  The keys are fixed rather than
    "whatever is not empty", which is this schema's own rule — *the columns the
    kinds do not use are empty rather than absent* — so a model reading a note
    is not left wondering whether an absent `size` means zero or means "not
    asked".

    **One time, and it is the column the kind is *ordered* by** (`SPECS.time`),
    not two stamps under two names: a diary's axis is the day it is *about* and
    a note's is when it was last written, and a payload carrying both
    `created_at` and `updated_at` makes a reader work out which of them the list
    it came from was sorted on.  A row of a kind this build does not know, or
    one whose axis column is empty, answers with whichever moment it does carry
    rather than with no time at all.
    """
    kind = str(row.get("kind") or "")
    spec = SPECS.get(kind)
    ordered = str(row.get(spec.time) or "") if spec else ""
    found: dict[str, Any] = {
        "kind": kind,
        "key": str(row.get("key") or ""),
        "title": str(row.get("title") or ""),
        "time": ordered or str(row.get("updated_at") or row.get("created_at") or ""),
        "tags": str(row.get("tags") or ""),
        "summary": str(row.get("summary") or ""),
        "mirror": str(row.get("mirror") or ""),
    }
    for column in _EXTRA_COLUMNS.get(kind, ()):
        found[column] = (
            int(row.get(column) or 0)
            if column == "size"
            else str(row.get(column) or "")
        )
    return found


def choice_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One document as something to **choose**: `facts`, and the line a chooser shows.

    **A listing and a search answer with this and not with the row.**  A body is
    the document, and a model picking one out of fifty has asked for a way to
    tell them apart — the reason the tool catalogue reports a tool's schema as a
    *size* rather than as text (`slife2.libs.store.catalogue`).  `excerpt` is `line_excerpt`, the
    one spelling of what such a line shows, so a row marked `matched_in:
    "line"` has the caller's words in the text printed beside it.
    """
    return {**facts(row), "excerpt": line_excerpt(row)}


def _found(
    row: Mapping[str, Any],
    similarity: float | None = None,
    *,
    matched_in: str | None = None,
) -> dict[str, Any]:
    """One search result: what a chooser needs, and no more.

    `choice_row` and the two marks a search adds — `similarity`, how close the
    row is **by meaning** (`search` says why a row nothing measured carries none
    rather than a zero), and `matched_in`, which is not a score but the fact
    that the caller's words are in the text the answer prints.
    """
    found = choice_row(row)
    if similarity is not None:
        found["similarity"] = similarity
    if matched_in is not None:
        found["matched_in"] = matched_in
    return found


def _like_literal(text: str) -> str:
    """Text as a `LIKE` pattern that matches only itself.

    Three characters have to be escaped, and the backslash first: it is the
    escape character this module's `LIKE` declares, so a folder named with one
    in it would otherwise escape the character after it — and `%` and `_` are
    what a `LIKE` reads as "anything" and "any one thing", which is how a
    `file_list(category="my_notes")` came to answer about `myXnotes`.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _section(stamp: str, body: str) -> str:
    """One appended entry, as the section it is written as.

    The timestamp is kept because an append's *order* is the only thing that
    says which of two entries came later, and a file a person reads needs to be
    able to say so without the database.
    """
    return f"## {stamp}\n\n{body}"


def _mirror_text(kind: str, row: Mapping[str, Any], added: str, *, first: bool) -> str:
    """What is written to one kind's markdown.

    **A heading on the first write and never again**, which is the difference
    between a note (rewritten whole every time, so it always carries its
    heading) and a report (a log, so a second save appends a section under the
    heading the first one wrote).  A diary has no heading: its file *is* a day,
    and the day is its name.
    """
    if kind == NOTE:
        return f"# {row.get('title') or row.get('key')}\n\n{added}\n"
    if kind == REPORT and first:
        return f"# {row.get('title') or row.get('key')}\n\n{added}\n"
    return f"{added}\n"


def discard_file(path: Path) -> None:
    """Remove a file, and do not care that it was not there.

    Public because both halves of the cabinet need it — the store when a
    document's bytes go, the plugin when a save has to be taken back — and it
    had been written out once in each for that reason.  A removal that fails is
    logged rather than raised: the caller is unwinding something else that
    already went wrong, and replacing that message with "and the file is still
    there" helps nobody.
    """
    try:
        path.unlink()
    except OSError as exc:
        logger.debug("cabinet: %s not removed: %s", path, exc)


def parse_tags(value: Any) -> str:
    """Tags as the row holds them: one comma-separated string.

    A model writes them as a list or as a string, and a column holding both
    shapes is a column two readers have to agree about.  v1's choice; the
    alternative — a join table — is a second thing to keep in step for a field
    nothing ever queries alone.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Sequence):
        return ",".join(str(part).strip() for part in value if str(part).strip())
    return ""
