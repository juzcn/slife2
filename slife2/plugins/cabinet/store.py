"""The cabinet's index: six kinds of row, and a corpus of four.

The cabinet holds **notes** (keyed by subject), a **diary** (keyed by date),
**files** (bytes on disk, metadata here), **reports** (keyed by title),
**dumps** (one tool's own output, keyed by the call that produced it) and
**images** (a picture a turn carried, keyed by its digest).  The first four are
the **documents the cabinet manages**: a model writes them through its own
tools, a key is a name it or a person gives, and a search ranks them against
each other — by words for all four, and by meaning for the three agent-written
kinds (note, diary, report); a saved file has no vector.  The last two are a
**turn's attachments**: the harness writes them, the key is an identity the turn
already carries, and the turn's own marker is the address — so they are stored
here and are **not in the corpus**.

**The file is the document.**  A note, a day, a report and a dump keep their
text in the file their row points at; the row holds the key, the metadata and
the address, and **there is no body column**.  Only the agent-written kinds are
vectorized (their whole file text); a saved file is keyword text, and a dump is
not searched.  What is kept is the *design* that made those things one
directory, and what is dropped is the machinery slife2 does without:

**A dump is the harness's, and it is here because it is text.**  A result too
large for the conversation is truncated for the model and kept whole for
everybody else — and *nothing decides which part of it mattered*, because that
is the tool designer's judgement and not the harness's.  So the whole of it is
stored **as one file**, and the conversation carries a fixed head of it within
the budget.  The row is its address and nothing more: it has no body, because
the text is already on disk and a second copy in SQLite would be the same bytes
twice.  What that does not make it is a document: the address a
model has for it is the `[DUMP:{…}]` marker in the transcript, which names the
call id `dump_read` and `dump_grep` take, and ranking machine output against a
person's notes is the dilution SDD §8.3 warns about.  So the row is written by
the harness, keyed by the call, and **not indexed**.

**And this kind's readers are bounded, like every tool of ours that hands text
to a model.**  What the harness writes into the transcript is the head of the
dump with the dump named beside it, so a model that wants a part it did not get
reads the file back with `dump_read` or finds it with `dump_grep` — tools that
return **at most the budget's worth of the original**, saying how much there is
and where the next window starts.  The readers' cap and the harness's budget
are **one number**, and that is the whole of what makes the arrangement closed:
a reader that could hand back more than the budget would be a tool of ours
feeding the harness's own truncation, which is a loop with this system's
machinery at both ends.  The bound is a property of the tool rather than a hope
about how a model uses it.

**An image is the same shape with a different reader.**  A picture a turn
carried is kept whole because the turn log is text; the conversation keeps an
`[Image:{…}]` reference and `attach_image` is how the picture comes back.  An
`http(s)` URL has no bytes here at all — an image URL is never fetched, unlike a
saved file's (`file_save`) — so the row keeps the URL as its `source` and the
provider fetches it.  Like a dump, it is addressed by the turn rather than
found by a search, so it is not in the corpus either.

**One row per call, keyed by the call id.**  That id is the one identifier both
sides already carry: the turn's own messages hold it on the tool message, and
this row is keyed by it, so the association with the turn is an identity rather
than a column — nothing to write at save time, nothing to keep in step, and
nothing that has to exist before the turn is saved (which a turn id would not).
A second write under one key replaces, because it is the same call truncated
again and not a second fact about anything.

**One table, and the kinds are a column.**  A note, a day, a saved file, a
report, a dump and an image are six *values* of `kind` in `cabinet_doc`, with
the union of the columns they need; the store is one table because they share a
directory, a key and a write path.  **The corpus is what the two indexes cover,
and that is the first four.**  A corpus row's rowid **is** the corpus id, so
the two legs of a search fuse on `rowid` the way the turns and the tool
catalogue already do (`slife2.libs.store.index.fuse_ranked`), with no global id
synthesised out of a per-kind offset.  Per-kind tables would also mean the FTS
table could only be kept correct by a trigger per kind per verb; here the
keyword text is written in the same transaction as the row that produced it,
which is how both of slife2's other stores do it and why this module has no
trigger at all.

**The per-kind facts are a table of their own** (`SPECS`): the time axis a list
orders by, whether that axis is a day, where the markdown mirror goes, and
whether the kind is in the corpus.  Every one of those is a fact
about a kind rather than about a document, so each is spelled out once rather
than once per table, view arm, trigger set and write.

**Every vectorized write embeds, and there is no drainer.**  There is no
background walk over unembedded documents catching them up on its own schedule,
and none is needed: a turn is stored with its vector in one transaction, and
this store is the same — **a note, a day or a report that cannot embed stores
nothing**.  A saved file is in the keyword corpus and has no vector; a dump and
an image are not embedded at all, because they are not in the corpus; their
address is in the turn.  What stands in place of a drainer is the one pass
`sync` makes when a cabinet is opened by a *different* embedding model, and it
is a rebuild rather than a drain: it does not remember which rows are missing
(the index is derived, so "missing" is a query) and it does not run in the
background.

**A document is written once, to its file, and the row is the address.**  Every
text kind — a note, a diary, a report, a dump — lands as markdown under the
cabinet directory (`notes/`, `diary/`, `reports/`, `dumps/`), because a cabinet
nobody can open in an editor is a database with a nicer name; the row holds no
copy of the text.  A file and an image have no mirror: their bytes are the
document.  The row and the file are one operation and they are this module's: a
row without its mirror is a note that exists and cannot be read by the person it
was written for, and the mirror a row holds is a function of its *key* rather
than of its key's slug, so two subjects that slugify alike cannot silently share
one file (see `mirror_stem` — the readable name where it is unambiguous, and the
readable name plus a digest of the key where it is not).

**The cabinet is one directory per agent** — `agents/<agent>/cabinet/` — and it
is one *directory* rather than a set of files, which is what makes copying or
deleting a cabinet one operation.  Its contents are `notes/`, `diary/`,
`reports/`, `dumps/`, `pictures/`, and the saved files by category, so an
existing cabinet's documents are adopted by copying them in; there is no
`files/` level wrapping the categories, because the cabinet's *own* directory
already names the agent.  The
index (`.index.db`) is this build's own and is not portable: a vector index
does not survive an embedding-model change anyway, and slife2's rule for a
changed model is a rebuild rather than a migration.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import sqlite_vec

from slife2.helpers.clock import now
from slife2.helpers.timeutil import normalize_bound
from slife2.libs.host.paths import home, work_dir
from slife2.libs.store import textindex
from slife2.libs.store.index import (
    _OVERFETCH,
    MAX_PAGE,
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
    fuse_shortlist,
    index_meta,
    instant_bound,
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

#: The kind constants.  **The storage set and the corpus are not one set**:
#: `cabinet_doc` holds all six, because a dump's text and an image's bytes live
#: in this directory beside everything else; the two indexes cover the four a
#: search ranks.  `SPECS.searchable` is the one place that says which is which,
#: and `DOCUMENT_KINDS`, `ATTACHMENT_KINDS` and `KINDS` are derived from it so
#: none of the three can drift from the facts.
NOTE = "note"
DIARY = "diary"
FILE = "file"
REPORT = "report"
#: A tool's output, kept whole as a file because nothing here may decide what
#: part of it mattered.  The row carries the address (the call id and the mirror
#: path), not the text; the file is the one copy.  The only kind **no
#: model-facing tool of this plugin writes**: the harness calls `dump_save`
#: directly and writes a marked head into the conversation, and
#: `dump_read`/`dump_grep` are how a model gets the part it did not get — a
#: bounded answer, for the reason the module docstring gives.
DUMP = "dump"
#: An image attached to a turn, kept whole because the turn log is text.
#: **The second bytes-backed kind**, and the one whose reader is not a text
#: tool: the conversation keeps an `[Image:{…}]` reference and `attach_image`
#: is how the picture comes back (`slife2.libs.turn.attachments`).  A local
#: path or `data:` URI is stored as bytes under `pictures/`; an `http(s)` URL
#: has no bytes here at all — an image URL is never fetched, unlike a saved
#: file's `file_save` — so the row keeps the URL as its `source` and the
#: provider fetches it.
IMAGE = "image"

#: The index file, inside the cabinet it indexes.
INDEX_NAME = ".index.db"

#: The cabinet's directory inside one agent's own.  **Not `files/`**, which is
#: what the cabinet *contains* (the saved attachments): two of those in one path
#: — `agents/jack/files/files/images/a.png` — read as nothing at all.
CABINET_DIR = "cabinet"

#: Bump when the **vectorized kinds** or what their vectors are built from
#: change.  It rides in the vector identity, so an index built under the old
#: rules is dropped and rebuilt rather than ranked against the new one: only the
#: agent-written documents (note, diary, report) are vectors now, and their
#: text is their file's whole body.
VECTOR_RULES_VERSION = "agent-written-v1"

# `SYNC_BATCH` is `slife2.libs.store.index`'s, imported above.  The bound it puts
# on one `sync` call — every corpus document is embedded eventually, because a
# document without a vector is simply missing from the index and the next open
# finds it again — is the same bound `MAX_PAGE` puts on a page, read off the one
# request an `embed` call is.


@dataclass(frozen=True)
class Spec:
    """What is true of one *kind* rather than of one document.

    The key is what every per-kind tool takes — a write, a read, an update and
    a remove — and it is the only column a kind is addressed by: a note by its
    subject, a day by its date, a file by the path it was saved to, a report by
    its title, a dump by the tool call it is the output of, and an image by its
    digest.  Six kinds and six keys, which is why this is a column and not six
    tables.
    """

    #: The column a list orders by and a since/until window filters on.  Each
    #: kind really does have its own axis: a note is *when it was last written*,
    #: a diary is *the day it is about*, and a file and a report are when they
    #: arrived.
    time: str
    #: Whether that column holds a **day** rather than a moment.  A diary's axis
    #: is `YYYY-MM-DD`, so a window on it is day-granular and a datetime bound
    #: is read as its day.
    day: bool
    #: The document file's directory, or `""` for the kind whose bytes *are*
    #: the file.  For every mirrored kind — note, diary, report, dump — the file
    #: is the text and the row holds no body.  A saved file is not mirrored: the
    #: cabinet keeps the bytes, and a second copy of them as text would be a
    #: copy of something that is not text.
    mirror: str
    #: Whether this kind is in the cabinet's **corpus** — the rows the keyword
    #: index covers, and therefore the only ones a search ranks.  A note, a
    #: day, a saved file and a report are documents the cabinet manages.  A dump
    #: and an image are a turn's attachments: the turn's marker is their address
    #: (`dump_read`, `attach_image`), so an index over them would be a second
    #: retrieval path nothing uses — and machine output ranked against a
    #: person's notes is the dilution SDD §8.3 warns about.
    searchable: bool
    #: Whether this kind has a **vector**.  The agent-written documents — a
    #: note, a day and a report — do; their text is the document, and it is read
    #: from the file the row points at.  A saved file does not: it is found by
    #: its words (title, path, summary).  **Its size is not the agent's to
    #: control**, and meaning search over a file needs a RAG pass over its
    #: contents — deferred until one exists.  A dump is deferred for the same
    #: reason, and an image is not text at all.
    vectorized: bool


SPECS: dict[str, Spec] = {
    NOTE: Spec(
        time="updated_at", day=False, mirror="notes", searchable=True, vectorized=True
    ),
    # The diary's time axis **is** its key: a day keyed by `YYYY-MM-DD` sorts
    # chronologically as a string, so the column that addresses the row is the
    # column that orders it and there is no second one to keep in step.
    DIARY: Spec(time="key", day=True, mirror="diary", searchable=True, vectorized=True),
    FILE: Spec(
        time="created_at", day=False, mirror="", searchable=True, vectorized=False
    ),
    # A report's axis is when it was first written: an update replaces its
    # body, and the moment the report arrived is the one that does not move.
    REPORT: Spec(
        time="created_at", day=False, mirror="reports", searchable=True, vectorized=True
    ),
    # A dump's key is one call, and a second write under it is that call
    # truncated again rather than a second fact about anything — which is why
    # the harness's write is an upsert (`UPSERT`) and not a document's create.
    # Its time axis is when the output arrived — the only moment this row has,
    # since nobody wrote it.
    #
    # **The mirror is the document.**  Unlike a note, where the row is what a
    # search reads and the mirror is for a person, a dump's text lives only in
    # its file: the row holds no body, so a gigabyte of output is not stored
    # twice.  The mirror is written in the same call as the row and is the
    # address `dump_read`/`dump_grep` stream.
    #
    # **Not in the corpus.**  Its address is the call id in the turn's
    # `[DUMP:{…}]` marker, and `dump_read` is the reader, so there is nothing
    # for a search to add — and everything for machine output to dilute.
    DUMP: Spec(
        time="created_at", day=False, mirror="dumps", searchable=False, vectorized=False
    ),
    # An image is a saved-file's shape with a different reader: bytes on disk
    # (`pictures/`), no markdown mirror, one row per content digest so a
    # picture attached twice is stored once.  An `http(s)` source has no file
    # and is found by its URL; the row is still one per digest of that URL.
    # Its address is the turn's `[Image:{…}]` key, so it is not in the corpus.
    IMAGE: Spec(
        time="created_at", day=False, mirror="", searchable=False, vectorized=False
    ),
}

#: The corpus: the kinds a search ranks, in `SPECS` order.
DOCUMENT_KINDS: tuple[str, ...] = tuple(
    kind for kind, spec in SPECS.items() if spec.searchable
)
#: The vectorized corpus: the agent-written documents, in `SPECS` order.
VECTOR_KINDS: tuple[str, ...] = tuple(
    kind for kind, spec in SPECS.items() if spec.vectorized
)
#: The turn's attachments: stored here, addressed by its markers, never indexed.
ATTACHMENT_KINDS: tuple[str, ...] = tuple(
    kind for kind, spec in SPECS.items() if not spec.searchable
)
#: Every kind the store holds: the corpus first, then the attachments.  The
#: schema's `CHECK` is built from this tuple, so adding a kind is a change here
#: rather than a row somebody writes.
KINDS: tuple[str, ...] = DOCUMENT_KINDS + ATTACHMENT_KINDS

#: The sentence a caller gets for a kind the store does not hold.  Spelled
#: from `KINDS` so that another one cannot be added without this agreeing.
_KIND_HINT = f"kind must be one of: {', '.join(KINDS)}"

#: The sentence a caller gets for asking the **corpus** for an attachment.
#: A search is not how either is found, so an empty page would be the wrong
#: answer; the address is named instead.
CORPUS_KIND_HINT = (
    f"kind must be one of: {', '.join(DOCUMENT_KINDS)} — a dump is read by "
    "the call id in its `[DUMP:…]` marker with `dump_read` or `dump_grep`, "
    "and an image by its `[Image:…]` key with `attach_image`"
)

#: How a write treats the key it is given.  The first two are a document's own
#: verbs — `write` creates and refuses an existing key, `update` replaces and
#: refuses a missing one — and the third is the harness's: a dump and an image
#: are keyed by an identity the turn already carries, so writing one again is
#: the same fact restated rather than a second document.  A property of the
#: call, not of the kind, which is why it is not a `Spec` field.
CREATE = "create"
REPLACE = "replace"
UPSERT = "upsert"


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

    Inside the cabinet rather than beside `turns.db` and `schedule.db` in the
    agent's directory, so the cabinet is **one directory** — copy it, back it
    up, or delete it and the cabinet is gone with nothing left behind.  The
    cost is that the store's path is not derivable from the data directory
    alone, which is what these two functions are for.
    """
    return cabinet_dir(agent, subagent) / INDEX_NAME


# ── The text a document is found by, and the text it is about ────────────────
# Which column a leg reads is a decision about a *kind*'s columns, and here the
# kinds share one row — so the composition is a function of the row rather than
# of the view arm it came from.


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
    """The text a document is a vector of: the whole text of an agent-written one.

    **Only a note, a day and a report are vectorized.**  Their text is the
    document the agent wrote, and it lives in the file the row points at; the
    caller loads it before asking (`CabinetStore._with_body`).

    **A saved file is not vectorized.**  It is found by its words — title, path
    and a summary — through the keyword leg; its bytes are not a text this
    cabinet wrote, its size is not the agent's to control, and meaning search
    over it needs a RAG pass over its contents.  `file_read`/`file_grep` are how
    they are read.  A dump is deferred for the same reason, and an image is not
    in the corpus at all (`Spec.vectorized`).

    Tags are out of it, unlike the keyword text: a tag is a word somebody chose
    to file a document under, and a vector built from one describes the filing
    rather than the document.
    """
    return storable(str(row.get("body") or ""))


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


#: One mirror is written at a time.  **Not for the temp name** — that is
#: unique per write — but for the destination: on Windows two threads replacing
#: onto one path at the same moment can make the second `os.replace` fail after
#: its row has already committed, which reports a failed write for a document
#: that was stored.  The writes are small, and the cabinet is one process.
_MIRROR_LOCK = threading.Lock()


def _write_mirror(root: Path, relative: str, text: str) -> None:
    """Put the document's text at *relative*, creating the directory it needs.

    **The mirror is the document**, for every mirrored kind: the row holds the
    metadata and the address, and this file is the one copy of the text.  A
    temp file and `os.replace`, so a reader never sees half a document.
    """
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    # **A temp name per write, not per path.**  Two writes of one key overlap
    # normally (the embedding before the transaction is awaited), and a shared
    # `<name>.tmp` would let the second writer's `os.replace` find the first had
    # already moved it — raising after the row was committed, so a call fails
    # that had stored its document.  The lock covers the destination for the
    # same reason.
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with _MIRROR_LOCK:
        try:
            # **The text as it is**, not a platform translation of it: a dump's
            # mirror is the one copy of its output, and a reader's line and
            # character addresses have to be the output's own.
            tmp.write_text(text, encoding="utf-8", newline="")
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise


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


def accessible_path(path: Path) -> str:
    """A path the agent can open: **relative to its data directory**.

    **Relative, because the marker outlives the directory's location.**  A
    dump's marker is written into the turn and carried by every later request;
    an absolute path recorded today names a directory that may have moved when
    the home is migrated.  The agent's own tools run in the home — the tool
    servers are started there — so `agents/<agent>/cabinet/dumps/<call>.md` is
    an address they resolve, and it survives the move.

    The path is absolute when it is outside the home, because then no relative
    spelling means anything.  Forward slashes because Windows accepts them and
    a backslash in JSON is escaped to `\\\\`, which spends tokens on the escape
    and reads as a different path to a model.
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(home().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def document_path(root: Path, row: Mapping[str, Any]) -> str:
    """The path an agent opens for one document, or `""` when it has no file.

    **The agent's path, not the cabinet's internal one.**  A note, a day, a
    report and a dump are their mirror; a saved file and a stored image are
    their `file_path`; an `http(s)` image has no bytes here and answers `""`.
    The result is relative to the data directory (`accessible_path`), so a
    shell, `pandoc` or `rg` running where the agent's own tools run can open
    the same document the cabinet tools read.
    """
    relative = str(row.get("mirror") or row.get("file_path") or "")
    return accessible_path(root / relative) if relative else ""


# ── The schema ──────────────────────────────────────────────────────────────

_SCHEMA = f"""
-- The store: six kinds of row in one table, four of them the corpus.
--
-- **One table rather than one per kind plus a view**: the kinds share a
-- directory, a key and a write path, and the four corpus kinds have to be
-- rankable together — a corpus whose rows lived in four tables would have to
-- be unioned back together before it could be ranked.  A `kind` column says
-- what the row is, `key` is what a read and a remove tool take, and the columns
-- the kinds do not use are empty rather than absent.
--
-- **There is no body column.**  A mirrored kind's text is its file; a saved
-- file's bytes are its file; the row is the address and the metadata.  A
-- document is one copy, not a file plus a database copy.
--
-- **A corpus row's rowid is the corpus id**, which is what lets a search fuse
-- its two legs on it (`slife2.libs.store.index.fuse_ranked`) exactly as the
-- turns and the catalogue do.  A dump and an image are rows here and not in
-- the corpus: the turn's marker is their address, so they carry no index entry.
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

-- The keyword index, one row per **corpus** document, written in the same
-- transaction as the row it indexes.  `unicode61 remove_diacritics 2` is what
-- both of the other stores use, and `textindex` is what makes a Chinese query
-- match: the text is normalized on the way in and the terms on the way out, so
-- the two agree about where a word ends.
CREATE VIRTUAL TABLE IF NOT EXISTS cabinet_fts USING fts5(
    search,
    {textindex.FTS_TOKENIZE}
);

-- Which corpus document a vector belongs to.  A row here is a *chunk*, not a
-- document -- `vec0` holds one vector per row and a long document averaged
-- into one vector describes all of it a little and none of it well -- and this
-- is what remembers whose chunk it was.  The same arrangement as `turn_chunk`.


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


def store_of(path: Path) -> CabinetStore:
    """The cabinet at *path*, opened — `slife2.libs.store.db.store_for`'s counterpart.

    A function rather than a constructor call at the site, because the site is a
    thread hop (`asyncio.to_thread`) and what a hop needs is one callable taking
    one argument.  Opening is what creates the schema, so this is also what
    brings a cabinet into existence.
    """
    return CabinetStore(path)


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

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One connection for this call, and **it is closed on the way out**.

        `with sqlite3.connect(...)` is a transaction, not a lifetime: it commits
        and leaves the connection open.  On Windows an open handle is a file
        nobody else can delete, and a connection per call with none closed is a
        descriptor leak on every other platform.  The turn store and the
        catalogue state the same rule; this is the third store.

        What the caller's `with` gets is the commit (`with connection:`); the
        `finally` is the close.
        """
        connection = connect_to(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # ── Bringing the index up to date ────────────────────────────────────

    async def sync(self, embedder: Embedder) -> dict[str, Any]:
        """Make both indexes this embedder's over the corpus, or say what is missing.

        **One pass, and it is a rebuild rather than a catch-up.**  A vectorized
        document can only be missing from the index when the index belongs to a
        different embedder or was built by different rules -- because every
        vectorized write embeds in the same transaction, and a vector is
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
        identity = f"{vector_identity(embedder)}|{VECTOR_RULES_VERSION}"
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
        # Without it a changed embedding model would leave every document past
        # the first batch with no vector, reported once and never tried again:
        # the cabinet answering meaning queries about part of its corpus,
        # silently, for as long as the process lived.
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
        # A document the vector pass has finished with and the index does not
        # hold — the state a failed placement leaves, and one the pass cannot
        # reach itself, since `cabinet_embedded` is exactly what stops it being
        # selected again — would be silently absent from the meaning leg while
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
        """Vectorized documents the pass finished with and the index lacks.

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
        sync could ever clear — and because the plugin refuses to serve an index
        that is not ready, one empty note would keep the cabinet down
        (`_turns_without_vectors` describes the same case on the turn side).

        **An attachment cannot be one of these**, because it is never embedded
        and has no `cabinet_embedded` row; the kind filter says so rather than
        relying on that.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, kind, key, title, summary, tags, source,"
                " file_path, mirror FROM cabinet_doc"
                " WHERE id IN (SELECT doc_id FROM cabinet_embedded)"
                " AND id NOT IN (SELECT doc_id FROM cabinet_chunk)"
                f" AND kind IN ({_marks(VECTOR_KINDS)})",
                VECTOR_KINDS,
            ).fetchall()
        return [
            int(row["id"]) for row in rows if vector_text(self._with_body(row)).strip()
        ]

    async def embed_missing(
        self, embedder: Embedder, *, limit: int | None = None
    ) -> tuple[int, int]:
        """Embed vectorized documents the vector index does not hold.  `(remaining, done)`.

        **Every one of them, a batch at a time** — the caller loops until the
        first number is zero.  Batching is what keeps one call from embedding a
        thousand documents before it answers, and it is why this can be called
        from anywhere rather than only from `sync`: a document whose embedding
        failed at save time is found by the same query.  A saved file and an
        attachment are never selected: neither is vectorized.

        `(remaining, done)`: how many are still to try, and how many this call
        tried.  `remaining` is the count *after* the batch, read from the same
        connection that wrote it, so it cannot disagree with itself.

        **"Still to try" is `cabinet_embedded`, not "has no chunk."**  A document
        with nothing to embed places no chunk and never will, so counting chunks
        would hand it back for ever and the caller's loop would not end.

        **Its SQLite hops off the loop, one call at a time.**  This is the loop
        `sync` runs at open, and the documents it walks are the whole vectorized
        corpus the first time a changed model makes it run: a selection, a
        placement per document and a count, all of it blocking, all of it on the
        loop unless each one is sent to a worker (`sync` states why that is this
        side's job and not its caller's).  The embed between two of them is the
        one step that stays on the loop, because it is the one step that is
        already awaited.
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

    def _unembedded(self, limit: int) -> list[dict[str, Any]]:
        """One batch of vectorized documents the pass has not tried yet.

        The selection `embed_missing` walks, blocking and therefore on a worker
        thread: `cabinet_embedded` is the marker, and "has no chunk" is a
        question that never ends — see that method for why.  The kind filter is
        the vectorized set: a saved file, a dump and an image have no vector, so
        they must not come back for ever as documents this pass still has to
        try.  Each row carries its text loaded from its file.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, kind, key, title, summary, tags, source,"
                " file_path, mirror FROM cabinet_doc"
                " WHERE id NOT IN (SELECT doc_id FROM cabinet_embedded)"
                f" AND kind IN ({_marks(VECTOR_KINDS)})"
                " ORDER BY id LIMIT ?",
                (*VECTOR_KINDS, limit),
            ).fetchall()
        return [self._with_body(row) for row in rows]

    def _unembedded_count(self) -> int:
        """How many vectorized documents are still to try, read after the batch.

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
                    f" AND kind IN ({_marks(VECTOR_KINDS)})",
                    VECTOR_KINDS,
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
                "SELECT id, kind, key, title, tags, source, file_path,"
                " summary, mirror FROM cabinet_doc"
                f" WHERE kind IN ({_marks(DOCUMENT_KINDS)})",
                DOCUMENT_KINDS,
            ).fetchall()
            connection.executemany(
                "INSERT INTO cabinet_fts (rowid, search) VALUES (?, ?)",
                [(int(row["id"]), search_text(self._with_body(row))) for row in rows],
            )
            set_index_meta(connection, text_version=textindex.RULES_VERSION)

    async def vectors(
        self, row: Mapping[str, Any], embedder: Embedder
    ) -> list[list[float]]:
        """One document's vectors, out of the model — **before anything is written**.

        The order is the contract: `write` calls this first, so a save that
        cannot embed has stored nothing yet, and the branch that has to undo
        something (the plugin's copy of a file) has only its own work to undo.
        There is no drainer to catch an unembedded document up later, so a save
        that cannot embed is a save that did not happen.

        An empty text is no vectors rather than an empty embedding: a document
        with nothing to say about itself is one the keyword leg still finds.

        **And an answer shorter than the request is refused**, because it is the
        worst place to miss it: `place` writes one `cabinet_chunk` row per vector
        it is handed and marks the document tried either way, so a short answer
        would leave the document holding vectors for *some* of its chunks,
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
        """How many documents the vector index is supposed to cover.

        `sync` answers with this number, and the number it is about is the one
        the vector pass can be missing: the agent-written kinds — note, diary,
        report.  A saved file is in the keyword corpus and has no vector; a dump
        or an image is a row in this directory and not a document the pass can
        be missing.
        """
        with self._connect() as connection:
            return int(
                connection.execute(
                    f"SELECT COUNT(*) AS n FROM cabinet_doc"
                    f" WHERE kind IN ({_marks(VECTOR_KINDS)})",
                    VECTOR_KINDS,
                ).fetchone()["n"]
            )

    # ── Writing ─────────────────────────────────────────────────────────

    async def write(
        self,
        kind: str,
        key: str,
        *,
        mode: str = CREATE,
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
    ) -> dict[str, Any] | None:
        """Write one row, its file and — for an agent-written kind — its vector.

        **`mode` says what the key means to this call.**  `CREATE` is a
        document's write: an existing key is refused, and `None` is the answer
        rather than a row, so nothing is overwritten and nothing is embedded.
        `REPLACE` is a document's update: a missing key is `None`, and an
        existing one is overwritten whole — body, tags and a report's period are
        what the caller passed, including the empty ones, because a replacement
        is not a patch.  `UPSERT` is the harness's: a dump and an image are
        keyed by an identity the turn already carries, so a second write under
        one key is the same fact restated and the row keeps its `created_at`.

        **The text lives in the file, not in the row.**  Every mirrored kind —
        note, diary, report, dump — writes the text it was given to `mirror`,
        exactly; **there is no body column**, so a document is one copy on disk
        rather than two.  The row is the address and the metadata, and a title
        is the row's: it is never prepended to the file.  (A saved file's bytes
        are already on disk; an image's are too.)

        **An agent-written document embeds before anything is written.**  The
        model is asked first (`vectors`), and only then does one transaction
        write the row, its keyword text and its vectors — so a write that
        cannot embed raises having stored nothing, which is what the plugin's
        `file_save` relies on when it takes back the bytes it copied.  A saved
        file is in the keyword corpus but has no vector; a dump and an image are
        in neither, because the turn's marker is their address.
        """
        if mode not in (CREATE, REPLACE, UPSERT):
            raise ValueError(f"mode must be one of: {CREATE}, {REPLACE}, {UPSERT}")
        # **The body is normalised before anything is written.**  A lone
        # surrogate arrives from the same model answer the turn store handles,
        # and the mirror is written *after* the row's transaction: a body the
        # file could not encode failed there — a call reporting failure over a
        # row, index and vector already committed, so every retry answered
        # "already a note" and an update left the old text beside the new row.
        # `storable` makes the row's derived text and the file one text.
        body = storable(body)
        spec = SPECS.get(kind)
        if spec is None:
            raise ValueError(_KIND_HINT)
        stamp = now()
        with self._connect() as connection:
            found = connection.execute(
                "SELECT * FROM cabinet_doc WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()
        existing = dict(found) if found is not None else None
        if mode == CREATE and existing is not None:
            return None
        if mode == REPLACE and existing is None:
            return None
        row: dict[str, Any] = dict(existing) if existing is not None else {}
        # **There is no body column.**  A mirrored document's text lives only in
        # its file, a saved file's bytes are already on disk, and the row is the
        # address and the metadata — so SQLite never holds a second copy of the
        # text.  The mirror is written from the argument.
        row.pop("body", None)
        if mode == REPLACE:
            # **A replacement is the document the caller describes**, so the
            # fields it owns are the caller's — an empty tag is a tag cleared,
            # not one left alone.  What it does not own stays: the summary is
            # `annotate`'s, the created stamp is when the report arrived, and a
            # saved file's bytes and a dump's source still describe this row.
            row.update(
                tags=tags,
                period_start=period_start,
                period_end=period_end,
            )
            row["title"] = row.get("title") or title or key
        else:
            row.update(
                title=title or row.get("title") or key,
                tags=tags or row.get("tags") or "",
                summary=summary or row.get("summary") or "",
                source=source or row.get("source") or "",
                file_path=file_path or row.get("file_path") or "",
                mime=mime or row.get("mime") or "",
                size=size or int(row.get("size") or 0),
                period_start=period_start or row.get("period_start") or "",
                period_end=period_end or row.get("period_end") or "",
            )
        row.update(
            kind=kind,
            key=key,
            created_at=row.get("created_at") or stamp,
            updated_at=stamp,
        )
        if spec.mirror and not row.get("mirror"):
            row["mirror"] = mirror_path(spec, key)
        # **An agent-written document is embedded before anything is written**,
        # so a write that cannot embed has written nothing — see `vectors`.  The
        # index views the row as it will be, with the body the row does not
        # keep: the keyword text and the vector are derived from the file's
        # text, and they are written in the same transaction as the row.
        index_row = {**row, "body": body}
        vectors = await self.vectors(index_row, embedder) if spec.vectorized else []
        with self._connect() as connection:
            placed = self._store(connection, row, mode)
            if placed is None:
                return None
            row["id"] = placed
            index_row["id"] = placed
            if spec.searchable:
                self._index(connection, index_row)
            if spec.vectorized:
                self.place(connection, placed, vectors)
        if spec.mirror:
            # Last, because a file cannot be inside the database's transaction.
            # The row is written first; a mirror that fails leaves an address
            # with no text, and the next write of the same key writes it.  (A
            # file written before the row would leave a document nothing can
            # find, which is the worse of the two failures.)
            _write_mirror(self.root, str(row["mirror"]), body)
        return self._with_path(row)

    def _store(
        self, connection: sqlite3.Connection, row: Mapping[str, Any], mode: str
    ) -> int | None:
        """The row, written by the rule its `mode` names, and its id.

        **Three statements, because the three modes ask three different
        questions of the key.**  A create is an `INSERT` that collides only when
        another writer got there first, and the collision is checked against the
        key so a constraint that failed for any other reason is raised.  A
        replacement is an `UPDATE` by the key, and a row that disappeared
        between the read and the write answers `None` rather than being
        resurrected.  The harness's upsert is `INSERT … ON CONFLICT … DO
        UPDATE`, which leaves `created_at` alone: the output of one call may be
        written twice, but it arrived once.
        """
        columns = (
            "kind",
            "key",
            "title",
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
        if mode == REPLACE:
            placed = connection.execute(
                f"UPDATE cabinet_doc SET {', '.join(f'{c} = ?' for c in columns)}"
                " WHERE kind = ? AND key = ?",
                (*values, row.get("kind"), row.get("key")),
            )
            return int(row["id"]) if placed.rowcount else None
        insert = (
            f"INSERT INTO cabinet_doc ({', '.join(columns)})"
            f" VALUES ({', '.join('?' * len(columns))})"
        )
        if mode == UPSERT:
            updates = ", ".join(
                f"{column} = excluded.{column}"
                for column in columns
                if column not in ("kind", "key", "created_at")
            )
            connection.execute(
                f"{insert} ON CONFLICT (kind, key) DO UPDATE SET {updates}", values
            )
            found = connection.execute(
                "SELECT id FROM cabinet_doc WHERE kind = ? AND key = ?",
                (row.get("kind"), row.get("key")),
            ).fetchone()
            return int(found["id"])
        try:
            placed = connection.execute(insert, values)
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

        **The whole document under the key**, which for a diary is that day's
        entries and for a report everything filed under that title — there is no
        smaller unit, because the key is the only address a document has.  The
        bytes of a saved file go too — and never what it was copied *from*,
        which `source` records and which this has no business touching.
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
    ) -> dict[str, Any] | None:
        """`cabinet_summarize`: a model's description of a corpus document.

        **The summary changes the keyword text and no vector.**  A vectorized
        document's vector is its body (`vector_text`), and a summary is not
        part of it; a saved file has no vector at all, so its summary is found
        by the keyword leg only.  Nothing here re-embeds, which is also why
        there is no `embedder` argument.

        **A dump and an image are refused**, because they are not in the corpus:
        a description is what makes a document findable, and neither is found by
        a search — the turn's marker is the address.
        """
        spec = SPECS.get(kind)
        if spec is None:
            raise ValueError(_KIND_HINT)
        if not spec.searchable:
            raise ValueError(CORPUS_KIND_HINT)
        with self._connect() as connection:
            found = connection.execute(
                "SELECT * FROM cabinet_doc WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()
        if found is None:
            return None
        row = self._with_body(dict(found))
        row["summary"] = summary or row.get("summary") or ""
        row["tags"] = tags or row.get("tags") or ""
        row["updated_at"] = now()
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
        return self._with_path(row)

    # ── Reading ─────────────────────────────────────────────────────────

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        """One document by its kind and key, or `None`."""
        with self._connect() as connection:
            found = connection.execute(
                "SELECT * FROM cabinet_doc WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()
        return self._with_path(found) if found is not None else None

    def body_of(self, row: Mapping[str, Any]) -> str:
        """One document's text: its file.

        The row has no body column.  A missing file raises: a document whose
        text is gone is not one with an empty text.
        """
        mirror = str(row.get("mirror") or "")
        if not mirror:
            return ""
        return (self.root / mirror).read_bytes().decode("utf-8")

    def _with_body(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """A row with `body` filled in, for the index passes."""
        found = dict(row)
        found["body"] = self.body_of(found)
        return found

    def _with_path(self, row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
        """A row with `path` filled in: where the agent's own tools can open it.

        The agent-relative path is derived here rather than by every answer,
        for the reason `facts` gives: one spelling of it, and a row fetched by
        any read carries it already.  A `sqlite3.Row` is converted because the
        path is read through a mapping, not a column.
        """
        found = dict(row)
        found["path"] = document_path(self.root, found)
        return found

    def _with_excerpt(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """A row with enough of its text for the line a chooser prints.

        A listing or a search shows a summary or the first `EXCERPT_CHARS` of
        the body; reading a whole document for that would be the read tool's
        job, not a picker's.  Four bytes per character is the UTF-8 ceiling, so
        this head is enough for every script.
        """
        found = dict(row)
        if found.get("summary"):
            return found
        mirror = str(found.get("mirror") or "")
        if not mirror:
            return found
        try:
            with (self.root / mirror).open("rb") as handle:
                head = handle.read((EXCERPT_CHARS + 1) * 4)
        except OSError:
            return found
        found["body"] = head.decode("utf-8", "ignore")
        return found

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
        others do not.

        `total` is counted before the page is cut, so "there are 40 and here are
        10" is answerable rather than a model guessing from a full page.
        """
        spec = SPECS.get(kind)
        if spec is None:
            raise ValueError(_KIND_HINT)
        clauses, values = self._window(spec.time, spec.day, since, until)
        if category:
            # **Escaped, or the filter is not the query it says it is.**  A
            # category is a folder name and `LIKE` reads two of the characters a
            # folder name may hold as wildcards: `my_notes` would match
            # `myXnotes` (the underscore is any one character) and a `%` would
            # match anything at all — a listing that answers about a folder
            # nobody named, bound rather than interpolated, so never injection.
            clauses.append(r"file_path LIKE ? ESCAPE '\'")
            values.append(f"{_like_literal(category)}/%")
        where = filter_clause(["kind = ?", *clauses])
        limit = page_limit(limit, MAX_PAGE)
        with self._connect() as connection:
            # **One read snapshot for the count and the page**, because two
            # autocommit statements let a write between them put a row on the
            # page that `total` did not count (or the reverse), and the page
            # boundary a caller computes from the two then repeats or skips a
            # row.
            connection.execute("BEGIN")
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) AS n FROM cabinet_doc {where}", (kind, *values)
                ).fetchone()["n"]
            )
            order = spec.time if spec.day else f"julianday({spec.time})"
            rows = connection.execute(
                f"SELECT * FROM cabinet_doc {where}"
                f" ORDER BY {order} DESC, id DESC LIMIT ? OFFSET ?",
                (kind, *values, limit, max(0, offset)),
            ).fetchall()
        return {
            "total": total,
            "limit": limit,
            "offset": max(0, offset),
            "entries": [
                choice_row(self._with_excerpt(self._with_path(row))) for row in rows
            ],
        }

    def _window(
        self, axis: str, day: bool, since: str | None, until: str | None
    ) -> tuple[list[str], list[Any]]:
        """A since/until window on **one kind's** time axis.

        `axis` is the column and `day` whether it holds days rather than
        instants; a browse that names no kind passes the `updated_at` axis the
        corpus kinds share, so this takes the axis rather than the whole spec —
        the rest of a spec is not a window's business.

        Not `slife2.libs.store.index.time_window`, which is spelled for a `created_at` column
        because both of its callers have one; a cabinet's kinds use their own
        axes.  The bound grammar is
        the shared one (`slife2.helpers.timeutil`), so a model writes `yesterday` here
        as it does anywhere else.

        A **day** axis takes the day part of the bound, and its `until` is
        exclusive because that is the only comparison a column of days has:
        `2026-10-07` becomes `2026-10-08`, so the day named is the last one in.
        **A bound that carries a time has to be put past its day here**, because
        `normalize_bound` only advances a *date-only* `until` — that advance is
        written for a column holding a moment, where the bound is compared with
        `<=` and means the instant.  Left unadvanced, `until=2026-10-07T12:00:00`
        would cut `< '2026-10-07'` and drop the very day it names: the same day
        written two ways, two different answers, and a window whose two bounds
        are the same moment returning nothing at all.
        """
        clauses: list[str] = []
        values: list[Any] = []
        if since:
            bound = normalize_bound(since, role="since")
            if day:
                clauses.append(f"{axis} >= ?")
                values.append(bound[:10])
            else:
                # By instant, not by offset string — see `time_window`.
                clauses.append(f"julianday({axis}) >= julianday(?)")
                values.append(instant_bound(bound))
        if until:
            bound = normalize_bound(until, role="until")
            if day:
                last = date.fromisoformat(bound[:10])
                bound = (last + timedelta(days=1) if "T" in bound else last).isoformat()
                clauses.append(f"{axis} < ?")
                values.append(bound)
            else:
                # A date-only bound was advanced to the next midnight; like the
                # day axis above, that instant is the first one *outside* the
                # day named, so the comparison is exclusive.  A datetime names
                # an instant and stays inclusive (`time_window` states it too).
                comparison = "<" if len(bound) == 10 else "<="
                clauses.append(f"julianday({axis}) {comparison} julianday(?)")
                values.append(instant_bound(bound))
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
        """Corpus documents matching the *words* and the *sentences*, best first.

        **Two inputs, because the two legs want different things** — the same
        rule and the same names `tool_search` uses (`ToolStore.search`), because
        a model that has learned one should not have to learn the other.  The
        words go to the keyword leg, which asks for all of them at once; meaning
        goes to the semantic leg.  Either may be empty, and both empty is a
        browse: everything, newest first, which is what a caller who named a
        kind and nothing else is asking for.

        **The corpus is the four documents the cabinet manages.**  A dump and an
        image are not in it: their address is the turn's marker
        (`[DUMP:{…}]`, `[Image:{…}]`), so they are read with `dump_read` and
        `attach_image` rather than ranked.  Naming one of them as `kind` is
        refused rather than answered with an empty page — an empty page is a
        verdict a model acts on, and it would be the wrong verdict.

        **A keywords-only call routes the same words through both legs.**  The
        joined words are its one sentence, because a word list is as good a
        meaning question as the sentence it came from (`ToolStore.search`
        measured it) and the keyword leg's AND is why a word form the text does
        not carry finds nothing otherwise.  With a sentence present, the words
        are that sentence's other leg.

        **One corpus, whatever `kind` says.**  A filter narrows what is *shown*
        and never what is ranked, because a rank only means something inside the
        corpus that produced it (SDD §8.3): one index per kind would be one corpus per
        kind, and a query spanning them would answer with an order nothing
        measured.  The corpus boundary is a filter like any other.

        **The meaning leg covers the agent-written documents.**  Only note,
        diary and report have vectors — their whole file text.  A saved file is
        keyword-only (title, paths, summary); a dump and an image are not in the
        corpus at all.  A file's or a dump's size is not the agent's to control,
        so meaning search over them is deferred until a RAG pass exists.

        The window is the one `listing` uses, per kind — and with no kind it is
        the *document's* `updated_at`, which every corpus kind has, because a
        window has to have a column and "when was this written" is the only axis
        they share.
        """
        limit = page_limit(limit, MAX_PAGE)
        if kind and kind not in DOCUMENT_KINDS:
            raise ValueError(CORPUS_KIND_HINT)
        clauses: list[str] = []
        values: list[Any] = []
        if kind:
            clauses.append("d.kind = ?")
            values.append(kind)
        else:
            # **The corpus boundary, as one rule.**  Neither index has an
            # attachment row, but a browse reads `cabinet_doc` directly, so the
            # four kinds are named here as the set the page may show.
            clauses.append(f"d.kind IN ({_marks(DOCUMENT_KINDS)})")
            values.extend(DOCUMENT_KINDS)
        if since or until:
            axis, day = (
                (SPECS[kind].time, SPECS[kind].day) if kind else ("updated_at", False)
            )
            window, bounds = self._window(axis, day, since, until)
            clauses.extend(window)
            values.extend(bounds)
        where = filter_clause(clauses)

        if not keywords and not any(sentence.strip() for sentence in sentences):
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT d.* FROM cabinet_doc d {where}"
                    f" ORDER BY julianday(d.updated_at) DESC, d.id DESC LIMIT ?",
                    (*values, limit),
                ).fetchall()
            return {
                "results": [
                    _found(self._with_excerpt(self._with_path(row))) for row in rows
                ],
                "browsed": True,
            }

        # **The words are a sentence when they are all there is.**  The keyword
        # leg asks for every word at once, so a word form the text does not
        # carry finds nothing; the meaning leg reads the same joined words.
        # `ToolStore.search` measured the difference (23/36 to 34-36).
        if keywords and not any(sentence.strip() for sentence in sentences):
            sentences = [" ".join(str(word) for word in keywords)]

        over = limit * _OVERFETCH
        expression = textindex.match_expression(list(keywords)) if keywords else ""
        keyword_hits = (
            self._keyword_hits(expression, over, clauses, values) if keywords else None
        )

        # **The shortlist, its numbers and its order are `fuse_shortlist`'s**,
        # so this page is put together by the same rule the catalogue's is.
        queries, lists = await self._semantic_lists(sentences, embedder, over)
        ordered, meaning, worded = fuse_shortlist(
            lists, keyword_hits, queries, self._meaning_of
        )
        with self._connect() as connection:
            rows = self._rows_in_order(connection, ordered, clauses, values, limit)
        page = [int(row["id"]) for row in rows]
        inline = self._matched_in_line(expression, page) if worded else set()
        return {
            "results": [
                _found(
                    self._with_excerpt(self._with_path(row)),
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
        an expression passed as a *parameter* whose placeholder is missing binds
        to `LIMIT` instead and answers with every document in the cabinet,
        ranked — a search that looks like it worked and returns the whole
        corpus.  The filters ride in the same
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
        # incomplete.  A bare `except sqlite3.OperationalError` would answer that
        # question and every other one the same way: a malformed vector table, a
        # width this build cannot read, a corrupt file all coming back as
        # "nothing matched by meaning", and the page then answering keyword-only
        # without saying so.
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
        they are certain evidence and would otherwise be the only rows on the
        page with no number at all, which makes the strongest evidence read as
        the weakest.

        **The number is read from the stored vectors, not asked of the index
        again** (`meanings_by_cosine`), which is what `ToolStore._meaning_of`
        does and why this is one loop and not two implementations: the rows here
        are exactly the ones the KNN has already said are not in its page, so a
        second KNN — `k` = `MAX_PAGE`, the *nearest* rows — would answer with
        the rows the first page already had, and the word-found rows would go
        unmeasured anyway.
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
    ) -> list[dict[str, Any]]:
        """The fused order, as mappings — **in that order**, and filtered.

        `slife2.libs.store.index.rows_in_order`, which is where the ordering, the re-applied
        filters and the cap on the bound list are argued; this store's own part
        is the table's name and the `d` it is aliased to, because the filters it
        passes are written against that alias.  The rows are converted here
        because everything that reads one in this module takes a mapping.
        """
        return [
            dict(row)
            for row in rows_in_order(
                connection,
                ordered,
                select="SELECT d.* FROM cabinet_doc d",
                column="d.id",
                key="id",
                clauses=clauses,
                values=values,
                limit=limit,
            )
        ]

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
        tokenizer, and it disagrees with the first exactly where it matters: the
        index is built by `unicode61 remove_diacritics 2`, which folds `Café` to
        `cafe`, while `normalize` keeps the accent — so a caller asking for
        `cafe` matches a note at the index and would then be reported as having
        matched *elsewhere*, with the word plainly on the line it matched.
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
                    (
                        int(row["id"]),
                        textindex.normalize(
                            line_text(self._with_excerpt(self._with_path(row)))
                        ),
                    )
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
    whether the caller's words are in it.  Written twice they disagree — a mark
    decided against a longer head than the excerpt shows puts "matched your
    words" beside an excerpt the words are not in.  The body is
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

    **What `_matched_in_line` is allowed to test**, and the whole of it: *the
    haystack is what the answer prints, and nothing else*.  A haystack that
    carried a row's `title` beside a line printing only the key and the excerpt
    would answer "matched your words" for a file saved with a title and no body,
    with every one of them missing from the page; the row is the answer —
    `choice_row`, in JSON — and the title is printed with it.

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

    **`path` is here, and `mirror` is not.**  The mirror is the cabinet's own
    spelling (`notes/<stem>.md`); the model's is the same file relative to its
    data directory (`agents/<agent>/cabinet/notes/<stem>.md`), where its own
    tools run.  That is deliberate: the cabinet's readers are primitive, and a
    path lets the model hand the document to `pandoc`, `rg` or any other tool
    it has.  A row with no file of its own — an `http(s)` image — answers `""`.
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
        "path": str(row.get("path") or ""),
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
    `file_list(category="my_notes")` would answer about `myXnotes`.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def discard_file(path: Path) -> None:
    """Remove a file, and do not care that it was not there.

    Public because both halves of the cabinet need it — the store when a
    document's bytes go, the plugin when a save has to be taken back.  A
    removal that fails is logged rather than raised: the caller is unwinding
    something else that already went wrong, and replacing that message with "and
    the file is still there" helps nobody.
    """
    try:
        path.unlink()
    except OSError as exc:
        logger.debug("cabinet: %s not removed: %s", path, exc)


def parse_tags(value: Any) -> str:
    """Tags as the row holds them: one comma-separated string.

    A model writes them as a list or as a string, and a column holding both
    shapes is a column two readers have to agree about.  The alternative — a
    join table — is a second thing to keep in step for a field nothing ever
    queries alone.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Sequence):
        return ",".join(str(part).strip() for part in value if str(part).strip())
    return ""
