"""The indexes: how a corpus is found again by its words and by its meaning.

Three corpora in this system are searched the same way — a conversation's turns
(`slife2.libs.store.db.TurnStore`), the tool catalogue (`slife2.libs.store.catalogue.ToolStore`) and the
cabinet (`slife2.plugins.cabinet.store.CabinetStore`) — and what makes them searchable is
the same mechanism in all three: an FTS5 table for the words, a `sqlite-vec`
table for the vectors, a chunk table that says which row a chunk belongs to, and
an `index_meta` row that records what the two were built *with*.

**It lives here rather than in whichever store was written first.** It used to
be `slife2.libs.store.db`'s, and the cabinet reached into that module for twenty-two names
— so the store that owns the turns was also the store that owned the
machinery, and every change to it was a change to a file about turns. The
cost of that was not tidiness: a rule stated once is a rule the other copy can
be missing without anything saying so. `_rowid` below is the case in point — a
missing rowid is a fault rather than a zero, and the copy that read it as `0`
was the one nobody had looked at. What is shared is the
mechanism; what stays with each store is the corpus — the document a row is
indexed by (`searchable_text`, `embed_text`, `tool_document`, `search_text`,
`vector_text`), the shape an answer is dressed in, and the policy its search
follows.

## Two rules the whole file is written around

**An index is derived, so it is never migrated.** Not one column of the base
tables exists to make room for one, and there is no migration layer: a table
that does not match what the configuration now says (a different normalization
rule, a different model, a different width) is dropped and built again, which is
what `rebuild_vector_index` does and what `vector_identity` decides. The cost is
paid at startup rather than carried as an upgrade path (SDD §8.2).

**Embeddings are a hard dependency, not a feature flag.** A row is written with
its vectors in one transaction, so a write that cannot embed writes nothing —
which is why nothing here has a degraded mode and why `place_vectors` is called
inside the writer's transaction rather than after it.

## What the parameters are for

The three corpora differ in more than names, and each difference is a parameter
here rather than a copy: the table names, the key a chunk points at (`turn_id`,
the catalogue's `name`, `doc_id`), the meta table (only the catalogue renamed
it, to `meta`), and whether a row's vectors are cleared before they are written.
That last one is not a preference: `tool_chunk.id` and `cabinet_chunk.id` are
reused rowids, and a `vec0` primary key that a stale vector still holds is a
`UNIQUE constraint failed` on the next write of the same row.

What is *not* here is a general query builder. The three `_keyword_hits` differ
in their join, their ranking (`rank`, a weighted `bm25`, a bare `bm25`) and the
alias they answer under, and the three sync loops differ in what "missing"
means — a `NOT IN`, a `LEFT JOIN`, a marker table. Those stay with the store
that asks the question; a helper that took a callback for each of them would
hide the one part the tests pin.
"""

from __future__ import annotations

import array
import math
import re
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

import sqlite_vec

import slife2.helpers.timeouts as _T
from slife2.helpers.clock import at
from slife2.helpers.timeutil import normalize_bound
from slife2.libs.store import textindex

# --- the page, and how far past it a leg looks --------------------------------

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
#:
#: **Public, because the cabinet is the third store to bind a fused list.**
#: `slife2.plugins.cabinet.store` imports it for the same reason it imports `MAX_PAGE`:
#: a second cap of its own would be a second number to keep under SQLite's, and
#: the failure when one of them is wrong is an `OperationalError` about too many
#: variables raised by whichever store nobody tested at that size.
MAX_SQL_VARS = 900


def chunks(values: Sequence[Any]) -> Iterator[Sequence[Any]]:
    """`values` in the pieces one statement may bind (`MAX_SQL_VARS`).

    **The cap, where the binding happens.**  Four places bind a list whose
    length the caller decided, and two of them had written this loop out and two
    had not: a `DELETE … WHERE name IN (…)` over a whole source's tools is a
    statement SQLite's default 999-variable limit refuses, and the corpus it
    refuses on is the one nobody tests at that size (`github`'s 1239 tools, the
    number `slife2.libs.plugin.family` cites).  A rule about a limit belongs beside
    the limit.
    """
    for start in range(0, len(values), MAX_SQL_VARS):
        yield values[start : start + MAX_SQL_VARS]


#: What a row carries before anything has measured it, while a page's
#: similarities are being gathered.  Below every cosine there is, so a row
#: nobody measured sorts under one the meaning leg scored minus something —
#: and `max` can collect the best of several measurements without a branch.
#:
#: **One sentinel, and it used to be two**: the collection seeded with `0.0`
#: and the leg with `-1.0`, so a row the vector leg scored below zero was
#: reported and ordered as `0.0` — a number `_nearest` never measured, on
#: the line a model reads.
NO_SIMILARITY = -1.0


#: How many rows one embedding request may carry when an index is being built
#: back up.  **Bounded because the answer is one request**: a first start with a
#: published server in the config is thousands of tools, and the embedding
#: server's own timeout is what a request that size runs into — which fails the
#: whole tool list rather than embedding the rest.
#:
#: **The cabinet takes this number rather than keeping a second one**, which is
#: the same bound read from the other side: one `sync` pass embeds at most this
#: many documents before it answers, so what bounds one request is also what
#: bounds how long one call takes.
#:
#: Reading the catalogues is bounded by this, not by the model: `embed` takes a
#: list and makes one call, and how much text it can carry is the endpoint's
#: business rather than something this file could compute.
SYNC_BATCH = 64


# --- the file, and whether it can hold vectors --------------------------------


class VectorIndexUnavailable(RuntimeError):
    """The vector extension could not be loaded, so nothing here can work.

    Fatal rather than a degraded mode: an index that silently is not there
    stores turns nothing can find, which is the one failure this plugin must
    not have.  The message names both ways it happens — an interpreter built
    without `enable_load_extension`, and a missing wheel.
    """


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


def index_meta(
    connection: sqlite3.Connection, table: str = "index_meta"
) -> dict[str, str]:
    """What each index was built with, as recorded in the file itself.

    `table` is a parameter because the catalogue has a `meta` table of its own:
    the two files record different things (a turn's text rules and a tool's) and
    the shape of the record is all this helper has ever been.
    """
    return {
        str(row["key"]): str(row["value"])
        for row in connection.execute(f"SELECT key, value FROM {table}")
    }


def set_index_meta(
    connection: sqlite3.Connection, table: str = "index_meta", **values: str
) -> None:
    connection.executemany(
        f"INSERT INTO {table} (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(values.items()),
    )


def connect_to(path: Path) -> sqlite3.Connection:
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
    connection = sqlite3.connect(path, timeout=_T.timeouts.storage.sqlite_busy)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        f"PRAGMA busy_timeout={int(_T.timeouts.storage.sqlite_busy * 1000)}"
    )
    _load_vector_index(connection)
    return connection


def begin_rebuild(connection: sqlite3.Connection) -> None:
    """Open the transaction a rebuild's DDL has to be inside.

    **`sqlite3` does not roll DDL back, and this module's connections are the
    mode where that is reachable.**  `connect_to` leaves the interpreter's
    legacy transaction control in place, which opens a transaction implicitly
    before a `DELETE`/`INSERT`/`UPDATE` and never before a `CREATE`/`DROP` — so
    a rebuild that leads with `DROP TABLE IF EXISTS <vec>` and then fails left
    the vectors table dropped *in autocommit* while the `DELETE` and the
    recorded identity after it were rolled back.  What that file is, is an
    index with chunk rows and no vectors, and a fingerprint saying it is whole —
    the one state the store cannot detect and cannot rebuild, since the identity
    it compares is the one that was written.

    An explicit `BEGIN` puts the DDL inside the transaction, so the caller's
    `with` commits or rolls back the whole rebuild as one unit.  Four rebuilds
    call it — a turn index, a catalogue, a cabinet and the catalogue's
    stale-file rebuild — and each of them leads with a `DROP`, which is what
    makes one helper worth having rather than a `BEGIN` line at each site.
    """
    connection.execute("BEGIN")


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
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


def _table_ddl(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return None if row is None else str(row["sql"] or "")


# --- small SQL ----------------------------------------------------------------


def _marks(values: Any) -> str:
    """A SQL `IN` list of placeholders, for values that are bound."""
    return ",".join("?" * len(list(values)))


def _rowid(cursor: sqlite3.Cursor, *, what: str) -> int:
    """The rowid an INSERT just made, or the failure that it made none.

    **`or 0` is not a default here, it is a wrong fact.**  A rowid is what names
    a row to the keyword index, the vector index and the live-context list, so
    an INSERT that did not happen used to leave several rows agreeing on `0` —
    a turn nothing could ever find, in an index that counted as complete.
    `lastrowid` is `None` only when the statement recorded nothing, which is a
    fault rather than a case to carry on from (SDD Appendix C).
    """
    rowid = cursor.lastrowid
    if rowid is None:
        raise RuntimeError(f"{what} recorded no rowid")
    return int(rowid)


def storable(text: str) -> str:
    """Text SQLite will encode.

    Surrogates cannot be encoded to UTF-8, so binding one raises
    `UnicodeEncodeError` — which is a failed write, and a failed write now fails
    the turn outright rather than losing it quietly (see
    `slife2.libs.plugin.protocol.open_server`). Neither is something an emoji in somebody's
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


def where_clause(clauses: list[str]) -> str:
    """Those clauses as the tail of a query that already has a `WHERE`."""
    return f"AND {' AND '.join(clauses)}" if clauses else ""


def filter_clause(clauses: list[str]) -> str:
    """Those clauses as a query's own `WHERE`, or nothing to filter by.

    The stores need both forms — one filters a match and then a rowid list,
    another browses and then filters — and each spelled the two out by hand.
    One of the six was a copy of this and the other five are the same expression
    with a different leading word.
    """
    return f"WHERE {' AND '.join(clauses)}" if clauses else ""


def instant_bound(bound: str) -> str:
    """A normalized bound as a moment, for a `julianday` comparison.

    **A date-only bound is a local day, not UTC midnight.**  `normalize_bound`
    leaves such a value as `YYYY-MM-DD` (and advances an `until` past its day),
    because its other readers compare text prefixes.  Once the comparison is by
    instant, the day has to be given the offset the column's timestamps carry,
    or the first hours of the local day sort before it.
    """
    if len(bound) == 10 and "T" not in bound:
        day = date.fromisoformat(bound)
        return at(datetime(day.year, day.month, day.day))
    return bound


def time_window(since: str | None, until: str | None) -> tuple[list[str], list[str]]:
    """`(clauses, params)` for a `created_at` window.

    **Clauses and not a `WHERE`**, because there are three readers of this one
    time axis now and they compose it differently: the browse puts it alone, and
    both legs of a search put it beside a predicate of their own.  A second
    spelling of `created_at >= ?` would be a second place for the column name
    and the grammar to drift apart.

    Raises:
        InvalidTimeBound: If either bound is in no grammar `slife2.helpers.timeutil`
            speaks.  Deliberately not an empty result: a bound nobody understood
            and a window with nothing in it are the same answer from SQLite, and
            only one of them is the truth.
    """
    clauses: list[str] = []
    params: list[str] = []
    if since:
        # **By instant, not by string.**  `created_at` carries a local offset,
        # and two rows written on either side of a DST change do not sort as
        # strings in the order they happened: `01:15+00:00` is later than
        # `01:30+01:00`, but it is smaller as text.  `julianday` reads the
        # offset and compares the moments.
        clauses.append("julianday(created_at) >= julianday(?)")
        params.append(instant_bound(normalize_bound(since, role="since")))
    if until:
        clauses.append("julianday(created_at) <= julianday(?)")
        params.append(instant_bound(normalize_bound(until, role="until")))
    return clauses, params


def rows_in_order(
    connection: sqlite3.Connection,
    ordered: Sequence[int],
    *,
    select: str,
    column: str,
    key: str = "",
    clauses: Sequence[str] = (),
    values: Sequence[Any] = (),
    limit: int,
) -> list[sqlite3.Row]:
    """Those rows, in the order they were ranked, filtered and cut.

    **One function, because three stores ask one question.**  A search's order
    comes from a fusion in Python and its rows come from a table, so the ids
    travel as an `IN` list and the order is put back here — SQLite would return
    them in whatever order it liked.  The turn store, the catalogue and the
    cabinet each had their own copy of that, differing in the table's name, the
    column the id is called and which store opens the connection; the last of
    those is why this takes a connection rather than opening one, because the
    two stores that read on a thread hop and the one that reads inline disagree
    about where a connection may be opened and nothing else.

    `select` is the whole `SELECT … FROM …` head (it is the part that differs),
    `column` the id's name *in SQL* — qualified where the query needs it — and
    `key` the name to read the id out of the row that comes back, which is not
    always the same: a turn selects `rowid AS turn_id`.

    **The filters are re-applied here**, because a leg may have brought back a
    row the caller excluded — the semantic leg cannot filter at all, a KNN has
    no `WHERE` — and a page that silently ignores its own window is worse than a
    shorter page.

    **The list is bound in chunks of `MAX_SQL_VARS`, here, because this is the
    one place that binds it.**  A search's shortlist is the union of a leg's ids
    per question and is not bounded by anything the caller passed, and a
    statement with more values than SQLite accepts raises `OperationalError`.
    The truncation that used to answer that — keep the top of the list — was
    worse than it looked: the ranking was already complete, the caller's list is
    already in its final order, and a page that had quietly lost its tail is the
    one thing a caller cannot tell from a short page.  `_ids_in_window` binds in
    the same chunks for the same reason.
    """
    wanted = list(ordered)
    if not wanted:
        return []
    found: dict[int, sqlite3.Row] = {}
    for start in range(0, len(wanted), MAX_SQL_VARS):
        chunk = wanted[start : start + MAX_SQL_VARS]
        where = f"WHERE {column} IN ({_marks(chunk)})"
        if clauses:
            where += f" {where_clause(list(clauses))}"
        rows = connection.execute(f"{select} {where}", (*chunk, *values)).fetchall()
        found.update({int(row[key or column]): row for row in rows})
    return [found[rowid] for rowid in wanted if rowid in found][:limit]


# --- what an index was built with, and building it again ----------------------


def vector_ddl(dimension: int, table: str = "turn_vec") -> str:
    """The vector index's DDL, for one width.

    `distance_metric=cosine` is declared rather than left to the default, and
    the reason is not preference.  It decides whether `1 - distance` is a
    similarity at all: a backend does not have to return unit-norm vectors, so
    on an L2 table that arithmetic yields numbers that are plausible and wrong
    rather than visibly broken.  Without this clause, distances past 18 clamp
    every strong hit to zero similarity; declaring the metric is the fix.

    The accepted spelling was checked against the installed extension rather
    than its documentation, which describes itself as a work in progress.

    `table` is a parameter because there are three indexes — a turn's vectors,
    the tool catalogue's and the cabinet's — and one spelling of "cosine, this
    width" is all this needs to be.
    """
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
        f"embedding float[{int(dimension)}] distance_metric=cosine)"
    )


def vector_identity(embedder: Embedder) -> str:
    """What the vector index was built with, as one comparable string.

    The text contract is part of it and not only the model, because a vector is
    a function of the text it was made from: changing what goes into one makes
    every stored vector the wrong vector for its turn, which is the same failure
    as changing the model and takes the same rebuild.

    The catalogue's index asks the same question of the same three things,
    and used to have a byte-identical copy of this to ask it with.
    """
    return f"{textindex.RULES_VERSION}|{embedder.identity}|{embedder.dimension}"


def rebuild_vector_index(
    connection: sqlite3.Connection,
    *,
    vec_table: str,
    chunk_table: str,
    dimension: int,
    identity: str,
    meta_table: str = "index_meta",
    also_delete: Sequence[str] = (),
) -> bool:
    """Build one corpus's vector table again if the identity it was built with moved.

    Everything "an index is derived" means, in the one place it is written: read
    what the index was built with, compare it, and if it differs drop the table
    and build it empty again.  Cheap when it is current — a single read.

    **The `DROP` and the `CREATE` are inside one transaction** (`begin_rebuild`),
    and that is the half worth stating twice: SQLite rolls a `CREATE` back and
    not a `DROP`, so without it a `CREATE` that failed left the vectors table
    gone, the `DELETE` undone, and a recorded identity describing an index that
    is not there.

    **The rebuild drops the vectors without re-embedding anything.**  Putting
    them back is the store's sync, which runs at startup before anything is
    served; this is the part that runs on the write path, where one row is being
    embedded and the rest of the corpus must not be waited for.

    Answers whether it rebuilt.  A store that has something to say about a
    rebuild says it there — the catalogue logs a line naming the file and the
    identity, and the other two say nothing.

    `also_delete` is the cabinet's: its marker table records "this row has been
    tried", and a rowid a `vec0` table has freed is reused, so a document taking
    an old id would be read as one the vector pass had already finished with.
    `meta_table` is the catalogue's, which renamed the table `meta`.
    """
    if index_meta(connection, meta_table).get("vector_identity", "") == identity:
        return False
    begin_rebuild(connection)
    connection.execute(f"DROP TABLE IF EXISTS {vec_table}")
    connection.execute(f"DELETE FROM {chunk_table}")
    for table in also_delete:
        connection.execute(f"DELETE FROM {table}")
    connection.execute(vector_ddl(dimension, vec_table))
    set_index_meta(connection, meta_table, vector_identity=identity)
    return True


def clear_vectors(
    connection: sqlite3.Connection,
    *,
    chunk_table: str,
    vec_table: str,
    key_column: str,
    keys: Sequence[Any],
    also_delete: Sequence[tuple[str, str]] = (),
) -> None:
    """Forget these rows' vectors and chunks, keeping the rows themselves.

    **The vectors go first, through the chunks that name them.**  A `vec0` row
    is addressed by whatever rowid its chunk row was given, so deleting the
    chunks first would leave vectors that nothing points at — rows the next
    write of the same key collides with, which is the failure this exists to
    avoid rather than to cause.

    The vector table may not exist, and that is not an error to report: it is
    created with a width, which only an embedder knows, so a corpus that has
    never been embedded has no `*_vec` at all and nothing to clear.

    `also_delete` is for a table keyed the same way that has to go with them —
    the cabinet's marker, which says "this row has been tried" and would be a
    lie about a row whose vectors are gone.
    """
    if not keys:
        return
    # Chunked, for `rows_in_order`'s reason and by the same rule: `keys` is
    # whatever the caller is removing — a whole source's name list, in the
    # catalogue's case, which is four figures for `github` — and a statement
    # binding more values than SQLite accepts raises instead of deleting.
    for chunk in chunks(keys):
        marks = _marks(chunk)
        if table_exists(connection, vec_table):
            connection.execute(
                f"DELETE FROM {vec_table} WHERE rowid IN"
                f" (SELECT id FROM {chunk_table} WHERE {key_column} IN ({marks}))",
                tuple(chunk),
            )
        connection.execute(
            f"DELETE FROM {chunk_table} WHERE {key_column} IN ({marks})", tuple(chunk)
        )
        for table, column in also_delete:
            connection.execute(
                f"DELETE FROM {table} WHERE {column} IN ({marks})", tuple(chunk)
            )


def place_vectors(
    connection: sqlite3.Connection,
    *,
    chunk_table: str,
    vec_table: str,
    key_column: str,
    row_key: Any,
    vectors: Sequence[Sequence[float]],
    what: str,
    clear_first: bool = False,
) -> None:
    """Write one row's chunks, one vector each, under ids of their own.

    A row in a `*_vec` table is a *chunk*, not the row it came from: `vec0` has
    no way to hold several vectors in one row, and a long document averaged into
    one vector describes all of it a little and none of it well.  The chunk
    table is what remembers which row a chunk came from, and the two are written
    together — a chunk row whose vector is missing is a row the index believes
    it holds.

    **`clear_first` is for a corpus whose rows are rewritten rather than
    appended to.**  `tool_chunk.id` is a plain rowid and SQLite hands the same
    one out again once the last row using it is deleted, so new chunks written
    over an old row's ids write vectors over the ones still in `tool_vec` —
    which `vec0` refuses.  A turn is never rewritten, so it does not pay for a
    delete it would not need; the cabinet does its own clear (it has a marker to
    take with it) and passes `clear_first=False`.

    `what` is how the row is named in the failure `_rowid` raises when an insert
    recorded nothing, so that message says which corpus and which row.
    """
    if clear_first:
        clear_vectors(
            connection,
            chunk_table=chunk_table,
            vec_table=vec_table,
            key_column=key_column,
            keys=[row_key],
        )
    for index, vector in enumerate(vectors):
        row = connection.execute(
            f"INSERT INTO {chunk_table} ({key_column}, chunk_index) VALUES (?, ?)",
            (row_key, index),
        )
        connection.execute(
            f"INSERT INTO {vec_table} (rowid, embedding) VALUES (?, ?)",
            (_rowid(row, what=what), sqlite_vec.serialize_float32(list(vector))),
        )


# --- text into chunks into vectors --------------------------------------------

#: How much text one vector covers.  A turn is chunked rather than embedded in
#: one piece because a vector is an average: a long turn embedded whole
#: describes everything in it a little and nothing in it well.
EMBED_CHUNK_CHARS = 2000


#: How much of the last paragraph is repeated at the head of the next chunk.  A
#: sentence cut in half at a chunk boundary is a sentence neither vector holds,
#: and a paragraph is the least that puts it back together.
EMBED_OVERLAP_PARAGRAPHS = 1


_PARAGRAPH_RE = re.compile(r"\n\s*\n")


def embed_chunks(
    text: str, *, chars: int = EMBED_CHUNK_CHARS, limit: int = 0
) -> list[str]:
    """A row's text cut into the pieces that each get their own vector.

    Packed by paragraph, with the last paragraph of a chunk repeated at the head
    of the next: a chunk boundary that falls inside a sentence leaves neither
    vector holding it, and a paragraph of overlap is what puts it back.

    `limit` is the most text the embedding model will take, in characters.  A
    single paragraph longer than that is truncated rather than split — a
    paragraph that long is machine output, and its tail is what the keyword leg
    is for.

    **`limit` bounds the *packing*, and is not a cut applied to the end.**  The
    window used to be packed to `chars` and then sliced to `limit`, which is the
    same thing only when the two are equal: every caller passes the embedder's
    `max_chars` (`TurnStore.save_turn`, the turn sync, the catalogue, the
    cabinet), and that is the endpoint's own input limit, so an endpoint with a
    small one packs a window it will not see the end of.  Everything between the
    cut and the window's end then sat in **no chunk at all** — not in the vector
    index, and not in anything else built from these chunks — in silence, since
    the row still has a vector and still counts as indexed.  Worse than the
    truncation the docstring above names, which at least holds the head of the
    paragraph.  So when the limit is the smaller of the two, the window is
    bounded by it and the paragraphs go to the next chunk instead.

    **The overlap is given up before the paragraph is.**  A carry that leaves no
    room for the paragraph being packed would put that paragraph's tail past
    what the model takes, so the carry is dropped — which costs no text, because
    the previous chunk already holds it.  The paragraph is never dropped that
    way: an overlap is a repetition and a paragraph is not.
    """
    paragraphs = [found.strip() for found in _PARAGRAPH_RE.split(text) if found.strip()]
    if not paragraphs:
        return []

    limit = limit or chars
    chars = min(chars, limit)
    chunks: list[str] = []
    window: list[str] = []
    size = 0
    for paragraph in paragraphs:
        if window and size + len(paragraph) > chars:
            chunks.append("\n\n".join(window))
            window = (
                window[-EMBED_OVERLAP_PARAGRAPHS:] if EMBED_OVERLAP_PARAGRAPHS else []
            )
            size = sum(len(kept) for kept in window)
            if size + len(paragraph) > chars:
                window = []
                size = 0
        window.append(paragraph)
        size += len(paragraph)
    chunks.append("\n\n".join(window))
    # The one chunk that can still be over the limit is a single paragraph
    # longer than it, which the docstring above says is truncated rather than
    # split.  Slicing here rather than at each append is what keeps that from
    # being true of every chunk, which is the bug this widened.
    return [chunk[:limit] for chunk in chunks]


def _chunk_documents(
    rows: Sequence[tuple[Any, str]], max_chars: int
) -> tuple[list[str], list[tuple[Any, int, int]]]:
    """`(every chunk in order, where each row's chunks are)`.

    Split out so the one request that carries the whole batch can be built
    before anything is sent: an embedding call per row is a round trip per row,
    and a first run has every tool of every server — or every turn of a log — to
    embed.  The key is `Any` because the two callers key their batches
    differently: a tool by its name, a turn by its rowid.
    """
    chunks: list[str] = []
    spans: list[tuple[Any, int, int]] = []
    for key, document in rows:
        pieces = embed_chunks(document, limit=max_chars)
        spans.append((key, len(chunks), len(pieces)))
        chunks.extend(pieces)
    return chunks, spans


#: The identity of the text the *vector* index was built from, as opposed to the
#: keyword one.  Composed by the caller (`slife2.clients.embedder`) out of the provider,
#: model and endpoint, because what makes two embeddings comparable is a
#: question about the embedding service and not about this file.
class Embedder(Protocol):
    """What a store needs in order to place a row in the vector index.

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


async def checked_vectors(
    embedder: Embedder, texts: Sequence[str], *, unit: str, so: str
) -> list[list[float]]:
    """The model's answer to one request, refused unless it answers all of it.

    **A short answer is the one shape of answer a caller cannot recover from**,
    which is why it is the one thing every embedding path checks.  Fewer vectors
    than texts means the rows behind the missing ones keep *no* vector at all:
    they are not in the index, and a save over them still reports the index
    ready.  That hole is invisible from outside — nothing reads a corpus looking
    for the rows that are absent from it — so it is refused where it is made.

    The two halves of the sentence are the caller's, because the sentence names
    which corpus is short (`unit`) and what that costs whoever asked (`so`): a
    turn store and a tool catalogue lose different things, and the person reading
    the traceback is looking at the row that lost its vector.
    """
    vectors = await embedder.embed(list(texts))
    if len(vectors) != len(texts):
        raise RuntimeError(
            f"the embedding model answered {len(vectors)} vectors for "
            f"{len(texts)} {unit}, so {so}"
        )
    return vectors


async def embed_rows(
    rows: Sequence[tuple[Any, str]], embedder: Embedder, *, noun: str
) -> dict[Any, list[list[float]]]:
    """One request for every chunk of every row that needs a vector.

    The batch is the point: a call per row is a round trip per row, and a first
    run has every tool of every server — or every turn of a log — to embed.  So
    `_chunk_documents` cuts them all, one request carries the lot, and the answer
    is dealt back out to the rows it came from.

    The count is checked, by `checked_vectors` above and for its reasons.  `noun`
    is that refusal's word for the row — a tool, a turn — because the two stores
    that call this index different corpora and the sentence is read by whoever is
    looking at the one that lost its vector.
    """
    chunks, spans = _chunk_documents(rows, embedder.max_chars)
    if not chunks:
        return {}
    vectors = await checked_vectors(
        embedder,
        chunks,
        unit="chunks",
        so=f"{len(spans)} {noun}(s) would be searchable only by keyword",
    )
    return {key: vectors[start : start + count] for key, start, count in spans}


def as_similarity(distance: float) -> float:
    """A `vec0` distance as the *similarity* everything above this reads.

    **One place, because there are two indexes.**  Both `turn_vec` and
    `tool_vec` are declared `distance_metric=cosine`, so a distance is `1 -
    cosine` and this is its inverse — and the two readers of that fact are a
    thousand lines apart.  A caller that forgot it would hand a number whose
    scale runs the other way to something that gates on it: `decisions.gate`
    keeps what is *above* the floor, so a store offering distances would keep
    the farthest candidates and drop the nearest, and the keyword leg's exempt
    candidates would hide it.

    Derived rather than assumed: `vector_ddl` is where the metric is declared,
    and its docstring is where the arithmetic is argued.
    """
    return 1.0 - distance


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """The cosine of two vectors — the same number `as_similarity` would give.

    The index reports `1 - distance` for the rows it returns, and it will not
    score a row it did not return: a KNN answers "which rows are nearest", and a
    row outside its page comes back with no number at all.  So a page that is to
    carry one kind of number has to measure the rest this way, and it has to
    measure them *the same* way — a second definition of "how close" would put
    two kinds of number in one column and call them one.

    Measured against the index over a whole catalogue, the two agree to float32
    rounding (worst case 8e-7), which is what makes that claim a fact rather than
    a hope.

    **The two must be the same length, and a mismatch is not truncated.**  It
    cannot happen while the fingerprint above holds — `identity` and `dimension`
    are what a rebuild keys on, so a stored vector is always one this embedder
    wrote — and that is the reason to say so out loud rather than to read the
    shorter of the two: a dot product over the first *n* of two incomparable
    vectors is a plausible number, and a plausible number in the column that
    orders a search is worse than an exception that names the problem.
    """
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    sizes = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / sizes if sizes else 0.0


def meanings_by_cosine(
    rows: Iterable[sqlite3.Row], id_key: str, queries: Sequence[Sequence[float]]
) -> dict[int, float]:
    """How close each of those rows' stored vectors is to the nearest query.

    **Read from the index rather than asked of it**, and the two are the same
    number: `1 - distance` on a `cosine` table *is* the cosine, and the two
    agree to float32 rounding (measured, worst case 8e-7 over a catalogue).  A
    KNN answers "which rows are *nearest*", and the rows this is for are the ones
    it has already said are not: asking it again for everything, to find the
    rows its own page omitted, would be a second scan whose `k` is capped at
    4096 — so the way that scales is the way that reads what is already there.

    A row may hold several vectors, one per chunk, and **the best of them stands
    for the row** — the rule both KNN legs apply to their own page, and the
    reason this is a `max` and not the first vector it meets.

    One function for both stores: the catalogue and the cabinet ask exactly this
    question of their own tables, and the cabinet used to answer it a third way
    — a second KNN, `k` = `MAX_PAGE` — which is a page of the *nearest* rows
    handed to a filter for the rows the first page did not hold, so it answered
    with almost nothing and the rows the caller's own words found went without
    the number that orders the page.
    """
    best: dict[int, float] = {}
    for row in rows:
        vector = array.array("f")
        vector.frombytes(bytes(row["embedding"]))
        rowid = int(row[id_key])
        for query in queries:
            value = _cosine(vector, query)
            if value > best.get(rowid, NO_SIMILARITY):
                best[rowid] = value
    return best


# --- fusing the two legs ------------------------------------------------------

#: The fusion's constant: how quickly rank stops mattering.  Sixty is the value
#: the literature uses; nothing about this store is a reason to differ.
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
