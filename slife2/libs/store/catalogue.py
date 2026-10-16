"""The tool catalogue — v1's `tools.db`.

One row per tool the model may be given, with two indexes over the rows: a
keyword one (`tool_fts`) and a semantic one (`tool_vec`).  **The machinery those
two are made of is `slife2.libs.store.index`'s** — the connection and its pragmas, the DDL,
the recorded identity that decides a rebuild, the chunk write, the paging and
the fusion — so a catalogue of its own costs a table and a set of documents
rather than a second implementation of all six.

**One file for the whole data directory, not one per agent.**  `--agent`
partitions the turns and nothing else: one hub serves every conversation and
cannot tell them apart, so which tools are installed — and which of them the
model has loaded — is a property of the machine.

**The hub opens this file, and holds nothing else of it.**  The catalogue used
to be served over MCP by a plugin of its own; it is an in-process `ToolStore`
now (`slife2.plugins.toolhub.server.Catalogue`), because a store has one writer and one writer
is what a SQLite lock already is (DESIGN.md §1.1).  The split that survives is
the one that matters: which tools exist is the hub's decision, what is known
about them is this file's record.

**It was `slife2.libs.store.db`'s until the two halves stopped sharing anything but a
logger.**  What is left in common is the record of what the model has *loaded*,
and that is a column here rather than a fact about a conversation.  A module
holding both makes every question about one of them a question about a file that
is mostly about the other — the turns are a history nothing can reconstruct,
and this is a set of rows the servers can be asked again.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import sqlite_vec

from slife2.helpers.clock import now
from slife2.libs.store import textindex
from slife2.libs.store.index import (
    _OVERFETCH,
    MAX_SQL_VARS,
    NO_SIMILARITY,
    SYNC_BATCH,
    Embedder,
    _chunk_documents,
    _marks,
    _rowid,
    _table_ddl,
    as_similarity,
    begin_rebuild,
    chunks,
    clear_vectors,
    connect_to,
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
    vector_identity,
)

logger = logging.getLogger(__name__)

#: The categories whose owner is a *server* — and therefore the only rows with a
#: load state and a connectivity verdict.  This set is the whole of what "is
#: this a function tool?" means, so there is no second column to keep in sync
#: with it: v1 dropped its derived `type` column for exactly this reason.
#:
#: `plugin` is ours, and it is the only one of the three that is: a plugin is a
#: server slife2 starts and therefore one that carries the plugin contract
#: (`slife2.libs.plugin.protocol`), while `mcp` and `rest` are somebody else's process,
#: reached as a client.  The word is chosen for what it keeps apart in a
#: sentence — a plugin *is* an MCP server, so calling ours "MCP servers" would
#: distinguish them from the twenty under `mcp:` not at all — and the
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

#: The `_`-prefixed names that are still injected: the harness tools, which are
#: in the model's list *because* their pairs are written into its history.  A
#: name in this set is injected, callable, and evictable only in the sense that
#: nothing of ours ever is — see `_injectable_sql` for the argument,
#: `slife2.plugins.toolhub.server.FUNC_TOOL_UNLOAD` for the trim, and
#: `slife2.plugins.agent.server.CHECK_NEW_INPUT` for the mid-turn delivery.
#:
#: **This set is the intersection of two different questions**, which is why it
#: is spelled out rather than derived: `slife2.libs.turn.tools` has the vocabulary, and its
#: point is that the `_` mark answers *who calls it* — the model's list and the
#: unload rule are asked separately.  Here the two answers happen to line up,
#: because what makes a tool a harness tool is a pair, and a pair has to name a
#: tool the request declares.  Both names are here for that one reason.
#:
#: Spelled rather than imported for the reason `PLUGIN` is spelled in two
#: modules: the hub must not be importable from the catalogue's half, and the db
#: must not import a server.
MODEL_VISIBLE_HARNESS_TOOLS = frozenset({"_func_tool_unload", "_check_new_input"})

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
#: `slife2.libs.store.textindex.RULES_VERSION` — the same one the turns use, because it is
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
-- what makes each of them a token (`slife2.libs.store.textindex`) — and normalization is
-- not a column value, so there is nothing for an external-content index to
-- point at.  The row is written by this module, in the same transaction as the
-- row it indexes.
CREATE VIRTUAL TABLE IF NOT EXISTS tool_fts USING fts5(
    name, description, category, source_id, schema,
    {textindex.FTS_TOKENIZE}
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


def _in_list(values: Any) -> str:
    """A SQL `IN` list of quoted literals, from a set of constants."""
    return ",".join(f"'{value}'" for value in sorted(values))


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
    tool.  Through `storable` for the reason the turn's text is: this string
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
    return storable("\n\n".join(part for part in parts if part))


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


def _search_dict(
    row: sqlite3.Row,
    similarity: float | None = None,
    *,
    matched_in: str | None = None,
) -> dict[str, Any]:
    """One search result: what a chooser needs, and no more.

    `similarity` is how close the row is **by meaning**, and every row on a page
    has one — `search` fills it in for the rows the meaning leg never returned,
    which used to be the rows with no number at all.  A caller that has none
    still gets no key rather than an invented `0.0`, which would read as "found
    by meaning, and it is a bad match".

    `matched_in` is the other half and is not a score: the row's own text holds
    every word the caller gave, which is certain where a similarity is graded —
    and is the one piece of evidence a cutoff cannot express, since a row can be
    named exactly whatever it scores (`pandoc`, at 0.514).  **`"line"` or
    `"rest"` says whether those words are in the text the answer prints**, and
    that is not a detail: a mark whose evidence is not on the page is a mark the
    reader cannot check.
    """
    found: dict[str, Any] = {column: str(row[column]) for column in _SEARCH_COLUMNS}
    schema = str(row["schema"] or "")
    found["schema_bytes"] = 0 if schema in ("", NA) else len(schema)
    if similarity is not None:
        found["similarity"] = similarity
    if matched_in is not None:
        found["matched_in"] = matched_in
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
        known: Iterable[str] | None = None,
    ) -> None:
        #: The most function tools the model may hold — see
        #: `slife2.libs.host.config.ToolLoadSettings`.  Held here rather than passed per
        #: call because the count it bounds is a `SELECT COUNT(*)` over these
        #: rows: the budget and the rows it applies to live in one place.
        self.threshold = max(1, int(threshold))
        #: Every source name the *config* carries, which is what the boot pass
        #: needs to tell a stale row from a slow one: see `reset`.  It is not the
        #: peers alone — a `mcp:` entry, a `rest-api:` one and the two document
        #: sources are named in the config too, and a boot pass told only about
        #: the plugins reads every one of them as a source that has gone.  The
        #: caller assembles the set (`slife2.plugins.toolhub.server.configured_sources`), because
        #: which sections name sources is knowledge about the config and this
        #: module has none — and it is *names*, not a list of connections: this
        #: file is still told nothing about liveness (`merge`'s `autoload` and
        #: `evict`'s are where the other facts the config used to supply arrive).
        #:
        #: **`None` is not the empty set, and the difference is destructive.**
        #: `None` is a caller that has not said which sources the config carries,
        #: which is a different fact from a set that happens to be empty — and
        #: the boot pass below is the thing that can tell them apart.  It was a
        #: default of `()`, so omitting the argument *was* the destructive case:
        #: every `plugin`/`mcp`/`rest` row in this file was deleted as the store
        #: opened, with its vectors and its keyword rows, and `tool_search` then
        #: answered about a catalogue nothing had gone wrong with.
        self.known: frozenset[str] | None = (
            None if known is None else frozenset(str(name) for name in known)
        )
        # The **set** is what the boot pass filters with, so an empty one means
        # "the config names no sources" — that is the case `reset` exists for,
        # and it is reachable only from a caller that has read the config
        # (`slife2.plugins.toolhub.server.configured_sources`).  A caller that has not read it
        # passes nothing, and nothing is what the pass then does.
        self.path = path
        self._ensure_schema()
        # Built means a process has just started, and a verdict this file
        # carries from an older run may be one nothing can justify any more —
        # `reset` is what withdraws the ones the config can speak to, and it
        # runs **only for a caller that named the config's sources**: a store
        # nobody told cannot tell a stale row from one whose section it was
        # never given, and every answer it could give is a deletion.
        if self.known is not None:
            self.reset()

    def _connect(self) -> sqlite3.Connection:
        return connect_to(self.path)

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
        # **One transaction, because SQLite does not roll DDL back** — a `DROP`
        # issued with no transaction open runs in autocommit, so a rebuild that
        # failed halfway through its list of five left some tables gone and the
        # rest standing, which is a catalogue with no keyword index and rows
        # nothing can read.  The `executescript` that follows commits this.
        begin_rebuild(connection)
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
                # **Whitespace is no name either**, and it is the same fact the
                # sentence above states: nothing can call a tool whose name is
                # blank.  It is not merely untidy to admit one — `tool_document`
                # builds the chunk text from this name, and `embed_chunks` drops
                # every paragraph that strips to empty, so such a row yields no
                # chunk at all.  `sync_indexes` terminates on "a selected row is
                # no longer missing", and `tools_without_vectors` asks for rows
                # with no chunk row, so the row it wrote is selected again and
                # again for ever: the hub awaits that loop while building its
                # store, so one such tool from any source hung every turn of
                # every home, on one core, with nothing said.
                if not name.strip():
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
                rowid = _rowid(cursor, what=f"the tool {entry['name']!r}")
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
        around — while a server under `mcp:` may offer ninety tools that cost
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
        # Chunked like every other statement that binds a caller-sized list
        # (`slife2.libs.store.index.chunks`): `forget_source` and `reset` hand
        # this *every* name of a source, and an `mcp:` entry can carry four
        # figures of tools — an `IN (…)` with more values than SQLite accepts
        # raises `OperationalError` and leaves the source's rows in place.
        for chunk in chunks(names):
            marks = _marks(chunk)
            _clear_vectors(connection, chunk)
            connection.execute(
                f"DELETE FROM tool_fts WHERE rowid IN"
                f" (SELECT rowid FROM tool WHERE name IN ({marks}))",
                list(chunk),
            )
            connection.execute(f"DELETE FROM tool WHERE name IN ({marks})", list(chunk))

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

    def forget_source(self, source: str) -> list[str]:
        """Delete every row of one source, and everything derived from them.

        **The other answer to "its entry is gone", and the one a verdict is
        not.**  `set_source_state` is about a source that is *there* — a server
        that is down, an entry the operator switched off — and it leaves the
        rows standing on purpose, because those are tools the model wants back
        the moment it answers.  A source the config no longer names is the case
        the boot pass already singles out as "the rows nothing will ever speak
        for again", and marking it was one step short of the conclusion: an
        `error` row is still found by a search, still ranked by the semantic
        leg, still counted by the budget, and `func_tool_load` refuses it with
        "the server that owns it is not answering" — a true sentence about a
        link, and the wrong one about a server that is not there at all.

        Deleting is the truth about an entry taken out of its section, and it is
        what re-adding one rebuilds: a merge writes the whole list, so the rows
        come back with their vectors the first time the holding plugin declares
        the source again.

        Returns the names removed — for the log line, and so a caller can say
        what went rather than only that something did.
        """
        with self._connect() as connection:
            names = [
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM tool WHERE source_id = ?", (source,)
                )
            ]
            self._delete(connection, names)
        return names

    def reset(self) -> dict[str, int]:
        """Forget what this config can no longer justify — run at open.

        A verdict is a statement about *now*, and this file outlives the process
        that wrote it, so some of what it holds is about a moment that has
        passed.  One of those this process can speak to on its own: a source
        **it cannot name at all** has no entry in the config any more, so
        nothing will ever declare it again — and that is `forget_source`, the
        rows and their vectors gone rather than marked.  An entry somebody
        deleted must stop being a search hit at the next start as well as during
        the run, or "removed" would mean "hidden until next time", and
        `Upstream.declare` covers the other moment.

        **Everything else is left alone, deliberately.**  A source this process
        can name is one the hub is about to ask about, and whether it is
        answering is the hub's to say — it is the party that asks, and it writes
        the verdict when a source answers, when one is switched off and when a
        link fails.  Touching those rows here would be this file guessing at a
        fact it cannot observe, and the guess would be visible: a db restarted
        under a running hub would report every tool as unusable while the model
        was still holding and calling them.

        **`known` is therefore every name the config carries, and not the peers
        alone** — more so than when the wrong answer was a mark.  A source held
        by a plugin is named over the wire, but this runs *before* any wire — so
        a boot pass told about the plugins only reads every `mcp:` entry as a
        source that has gone, and the window it opens is not the moment the
        docstring above admits to: it lasts until the holding plugin has
        started, connected to that entry and declared it, which for a section of
        twenty `npx` servers is tens of seconds.  Inside it a search reports
        those tools `error` and `func_tool_load` refuses them — "its owner is not
        answering", about a server that is answering — and when the declaration
        lands it re-enables every row one at a time, so the log of a restart is
        "239 changed" about a catalogue nothing changed.  A deletion is worse
        than that mark was: a mark the next declaration writes back costs a
        noisy restart, and a deletion costs a restart that re-embeds every one
        of those tools.  `slife2.plugins.toolhub.server.configured_sources` is the set; what
        this must not be given is a source the config has *dropped*, because
        that is precisely the stale row this exists to forget.

        **A store nobody told which sources the config carries forgets
        nothing.**  `known` of `None` is that caller, and it gets the answer a
        process that has not read the config deserves: `{"forgotten": 0}`.  The
        alternative — treating "not told" as "the config carries none" — is a
        boot pass that deletes every `plugin`, `mcp` and `rest` row in the file
        and their vectors, which is the one thing here that cannot be undone by
        asking a server again (it re-embeds them).  A store that was told
        nothing does not run this on the way up at all — `ToolStore.__init__`
        has the argument — but a *caller* naming this method has asked for the
        pass, so it keeps the meaning an empty set has always had.
        """
        if self.known is None:
            # **Not told is not told "none".**  `self.known or frozenset()`
            # used to collapse the two, which made this method the destructive
            # case the constructor's own comment says it must never be: a store
            # nobody told which sources the config carries deleted every
            # `plugin`, `mcp` and `rest` row in the file and their vectors, on
            # a call it was asked for outright.  `__init__` already declines to
            # run the pass for that store; the state it declines to run is now
            # the state the method has (DESIGN.md §10.1).
            return {"forgotten": 0}
        known = self.known
        with self._connect() as connection:
            where = f"category IN ({_in_list(FUNCTION_CATEGORIES)})"
            if known:
                where += f" AND source_id NOT IN ({_marks(known)})"
            names = [
                str(row["name"])
                for row in connection.execute(
                    f"SELECT name FROM tool WHERE {where}", sorted(known)
                )
            ]
            self._delete(connection, names)
        return {"forgotten": len(names)}

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

    def source_counts(self, sources: Sequence[str]) -> dict[str, dict[str, int]]:
        """Per source: how many tools it has, and how many the model holds.

        Two numbers because they answer two questions, and only the second is
        gated on liveness.  `tools` is what the source last offered — the answer
        to "why is my tool missing" when it is not the same as `loaded`, which is
        how many of them the model has in its list right now.  A source with
        ninety tools and none loaded is a healthy server, and saying so is the
        difference between a fault and a choice.

        **`loaded` is a fact about the model's list, and the column alone does not
        say it.**  A load is a decision the model made and it outlives the
        connection — that is the point of the column — so a server that is down
        leaves every one of its rows saying `loaded` while the gate hands the
        model none of them.  `sources` is the caller's for the reason `injectable`
        and `evict` state: liveness is a fact about connections, and this file
        holds none.  Quoting those rows as "the model has them now" would be the
        one number in `servers()` that contradicts `list_tools`.
        """
        live = [str(name) for name in dict.fromkeys(sources)]
        gate = f" AND source_id IN ({_marks(live)})" if live else " AND 0"
        where = f"category IN ({_in_list(FUNCTION_CATEGORIES)})"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT source_id, COUNT(*) AS tools,"
                f" SUM(CASE WHEN load_status = '{LOADED}'{gate}"
                f" THEN 1 ELSE 0 END) AS loaded"
                f" FROM tool WHERE {where} GROUP BY source_id",
                live,
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
        *,
        keywords: Sequence[str] = (),
        sentences: Sequence[str] = (),
        embedder: Embedder,
        limit: int = 10,
        category: str = "",
        source_id: str = "",
        status: str = "",
        load_status: str = "",
    ) -> dict[str, Any]:
        """Tools matching the *words* and the *sentences* given, best first.

        **Two inputs, because the two legs want different things**, and giving
        them the same string was the one thing that could not work: the keyword
        leg asks for every term it is handed, so a sentence — "take a screenshot
        of a web page" — demands six words at once and matches nothing, which is
        most of why a sentence-shaped search was really a semantic search.  The
        words go to `keywords` and meaning goes to `sentences`, either may be
        empty, and the fusion is the same one it always was.

        **The fusion decides the shortlist; the page is ordered by what the caller
        can see.**  Within one question the two legs are fused by rank, for the
        reason a turn search fuses its own: `bm25` is unbounded and depends on the
        corpus, a cosine depends on the model, and a rank is comparable by
        construction — so a row both legs found outranks one only one of them did.
        Across questions there is no such evidence, and the page order is not the
        fusion's anyway: it is *the words first, then the meaning*, both of which
        are on the page for the reader to check.  Ranking across questions instead
        gave each of them an alternating half of the page, so a second question
        the caller appended cost the first one five of its ten rows.

        **Several sentences are several *questions*, not one longer query.**  Not
        one string glued together, because that dilutes the vector; and not
        several lists under one fusion either.  What a caller can rely on is that
        **another question is another chance to be found, and cannot cost a row
        that an earlier one already found** — which the ordering above is what
        makes true, since adding a question can only add rows to the shortlist and
        a row's place no longer depends on which question found it.  They are one
        `embed` call whatever there are of them; the model is asked for a batch.

        **Both empty is a browse, not an empty answer.**  Nothing asks for the
        whole catalogue by accident, and a list of what exists is how a model or
        a person finds out what a category holds — which is why the store still
        answers it, and why the tool does not (`tool_search` refuses it: a
        listing is a different question with a different shape, §9 of DESIGN).

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
        if not keywords and not any(s.strip() for s in sentences):
            rows = [
                _search_dict(row)
                for row in await asyncio.to_thread(self._browse, limit, clauses, values)
            ]
            return {"results": rows, "browsed": True}

        # **The words are a sentence when they are all there is.**  A
        # keywords-only call used to be answered by the keyword leg alone, and
        # measured on the live catalogue that leg found 23 of the 36 tools a
        # caller asks for — the same 23 whether the catalogue held 130 of them or
        # 253, because what it misses is word forms and not choice.  Routing the
        # same words through both legs finds 34 to 36.
        #
        # The loss was never that words embed badly — a word list embeds as well
        # as the sentence taken from it.  It was that they never reached the leg
        # that could answer them.  The cost of closing that is one embedding call
        # on a call that used to make none.
        #
        # Here rather than in `toolhub`, which had it: this is a fact about the
        # *search* and not about the tool, and a second caller of this method got
        # rows with no number on them because the words had been routed by the
        # caller that remembered to.
        if keywords and not any(s.strip() for s in sentences):
            sentences = [" ".join(str(word) for word in keywords)]

        over = min(limit * _OVERFETCH, MAX_SQL_VARS)
        expression = textindex.match_expression(list(keywords)) if keywords else ""
        keyword_hits: list[int] | None = None
        if keywords:
            keyword_hits = await asyncio.to_thread(
                self._keyword_hits, expression, over, clauses, values
            )

        # **The shortlist, one list per question.**  `keywords` is what the caller
        # has in mind, so where there is a sentence it is that sentence's other
        # leg and is fused into that question — a row both legs found is evidence
        # *about that question* — and `fuse_ranked` is what orders that list.  The
        # questions are then unioned and the whole shortlist is ordered once,
        # below, by a rule the caller can read off the page.
        phrases = [(index, s) for index, s in enumerate(sentences) if s.strip()]
        meaning: dict[int, float] = {}
        shortlist: list[int] = []
        seen: set[int] = set()
        queries, lists = await self._semantic_lists(sentences, embedder, over)
        for (index, _), hits in zip(phrases, lists, strict=True):
            legs = {f"sentence:{index}": [rowid for rowid, _ in hits]}
            if keyword_hits is not None:
                legs["keyword"] = keyword_hits
            for rowid, _ in fuse_ranked(legs):
                if rowid not in seen:
                    seen.add(rowid)
                    shortlist.append(rowid)
            # A row several sentences found takes the *nearest* of them, so the
            # number is how close the row came to the nearest thing the caller
            # asked for — not whichever sentence happened to be looked at first.
            for rowid, measured in hits:
                meaning[rowid] = max(meaning.get(rowid, NO_SIMILARITY), measured)
        # (A `if keyword_hits is not None and not shortlist` fallback stood
        # here, assigning the keyword list when the fusion had produced nothing.
        # It could not: the words are always *some* question's other leg — a
        # keywords-only call is routed into `sentences` above — so every keyword
        # id is already in `shortlist` by the time this line is reached.  The
        # byte-identical line in `slife2.plugins.cabinet.store` is live, because
        # that corpus does not do the routing; this copy was the one that
        # outlived its reason.)
        # **The union is capped, and each question may bring `over` ids of its
        # own.**  Nothing a caller passes bounds this list: every sentence
        # contributes its meaning leg's ids plus the keyword leg's, so forty
        # sentences is forty times a page, and the query that reads the rows
        # binds one value per id.  What is dropped is the tail of the *union*,
        # which is the weakest evidence there is — a page is at most `limit`
        # rows and every question's own list is already ranked.
        shortlist = shortlist[:MAX_SQL_VARS]

        # **Every row of the shortlist gets its number**, not only the rows the
        # meaning leg returned — those are exactly the rows the words found, and
        # they are about to be shown first.  They used to be the only rows with no
        # number at all, so the strongest evidence on the page read as the
        # weakest thing on it.
        for rowid, value in self._meaning_of(
            [rowid for rowid in shortlist if rowid not in meaning], queries
        ).items():
            meaning[rowid] = max(meaning.get(rowid, NO_SIMILARITY), value)

        # **The page is ordered by two things the caller can see.**  The words
        # first, because a row whose own text contains them is certain where a
        # cosine is graded, and the cosines cannot separate a cluster at all:
        # `set_table_column_width` and `..._widths` measure 0.63 and 0.62, `v0`
        # and `v0_1` both 0.66 — only the word match knows which was named.  Then
        # by meaning, descending.
        #
        # Ordered *here* rather than by the fusion's ranks, which is what this
        # did: ranking across questions gave each of them an alternating half of
        # the page, so a second question the caller appended cost the first one
        # five of its ten rows — measured, a row at rank 5 of question one came
        # back at rank 9, under four rows of a question it was unrelated to.
        # The sort is **stable and carries no id in its key**, so rows whose
        # meaning is equal keep the order the fusion gave them: where the number
        # cannot decide, the evidence does, rather than whichever rowid sorts
        # first.  That is also what makes a tie fall back to what this did before
        # there was a number to order by.
        worded = set(keyword_hits or ())
        ordered = sorted(
            shortlist,
            key=lambda rowid: (rowid not in worded, -meaning.get(rowid, NO_SIMILARITY)),
        )
        rows = await asyncio.to_thread(
            self._rows_in_order, ordered, clauses, values, limit
        )

        # **Where the words are, for the rows they matched** — the line shows two
        # of the five columns a row is indexed by, so a row can be marked `matched
        # your words` with nothing on the line to check it against: a skill's
        # whole playbook rides in its `schema` column, and `skill:browser-harness`
        # matches `read`/`file` through the body while its description says
        # nothing about either.  A mark whose evidence cannot be seen is a mark
        # that cannot be checked, so the answer says which side it is on.
        page = [int(row["rowid"]) for row in rows]
        inline = (
            await asyncio.to_thread(self._matched_in_line, expression, page)
            if worded
            else set()
        )
        return {
            "results": [
                _search_dict(
                    row,
                    meaning.get(int(row["rowid"])),
                    matched_in=(
                        None
                        if int(row["rowid"]) not in worded
                        else "line"
                        if int(row["rowid"]) in inline
                        else "rest"
                    ),
                )
                for row in rows
            ],
            "browsed": False,
        }

    def _matched_in_line(self, expression: str, rowids: Sequence[int]) -> set[int]:
        """Which of those rows hold the words in the columns the line prints.

        `{name description}` is what a result line shows; `category`, `source_id`
        and `schema` are indexed and unshown, and `schema` is the one that carries
        something substantial — a tool's parameters, or for a skill the whole
        playbook (`tool_document` argues the column, and this is not the place it
        is what it says).

        The filter is a column-scoped `MATCH` rather than a token comparison in
        Python: what counts as a word here is `unicode61`'s answer, and a second
        implementation of that would disagree with the index at the edges — which
        is the same reason `normalize` and `terms` are one module.
        """
        if not rowids:
            return set()
        with self._connect() as connection:
            return {
                int(row["rowid"])
                for row in connection.execute(
                    "SELECT f.rowid AS rowid FROM tool_fts f"
                    f" WHERE tool_fts MATCH ? AND f.rowid IN ({_marks(rowids)})",
                    (f"{{name description}} : ({expression})", *rowids),
                )
            }

    def _meaning_of(
        self, rowids: Sequence[int], queries: Sequence[Sequence[float]]
    ) -> dict[int, float]:
        """How close those rows are, by meaning, to the nearest thing asked.

        The cosine, computed from the vectors the index already holds — which is
        the same number and not a second derivation of it, and `meanings_by_cosine`
        is where that is argued.  The query is the store's own: a `tool_vec` row
        is a chunk, so which tool it belongs to is one join away, and the max
        over a tool's chunks is what stands for the tool.
        """
        if not rowids or not queries:
            return {}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT t.rowid AS rowid, v.embedding AS embedding"
                " FROM tool_vec v JOIN tool_chunk c ON c.id = v.rowid"
                " JOIN tool t ON t.name = c.name"
                f" WHERE t.rowid IN ({_marks(rowids)})",
                tuple(rowids),
            ).fetchall()
        return meanings_by_cosine(rows, "rowid", queries)

    def _browse(
        self, limit: int, clauses: list[str], values: list[Any]
    ) -> list[sqlite3.Row]:
        where = filter_clause(clauses)
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

        **And they are the *rows*', not the match's.**  `_tool_filters` builds
        unqualified clauses naming columns of `tool`, and `tool_fts` declares two
        of those names itself (`name`, `source_id`, `category`, `schema`), so a
        `JOIN tool` that put both tables in scope made every filtered keyword
        search fail with `ambiguous column name: category` — a crash, on the
        documented path ("a search with a category is about that category"),
        whose working siblings are the browse and the meaning leg.  A subquery
        keeps one table in scope for the `MATCH` and one for the filters, which
        is also the whole of what the join was for.
        """
        weights = ", ".join(str(weight) for weight in BM25_WEIGHTS)
        where = filter_clause(clauses)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT rowid FROM tool_fts"
                f" WHERE tool_fts MATCH ?"
                f" AND rowid IN (SELECT rowid FROM tool {where})"
                f" ORDER BY bm25(tool_fts, {weights}) LIMIT ?",
                (expression, *values, limit),
            ).fetchall()
        return [int(row["rowid"]) for row in rows]

    async def _semantic_lists(
        self, sentences: Sequence[str], embedder: Embedder, k: int
    ) -> tuple[list[list[float]], list[list[tuple[int, float]]]]:
        """`(the query vectors, one ranked list each)`, out of **one** `embed`.

        A batch, which is what `search`'s docstring has always claimed and what
        a call per sentence did not do: three sentences were three round trips to
        the model, with the answer to each never looked at while the next was
        asked for.  Blank sentences are dropped before the call, so the caller's
        list and the answer line up by position.

        The vectors come back as well as the lists because the number a result
        shows is the meaning number of *every* row on the page, including rows
        the leg did not return — `search` asks `_nearest` again with a `k` that
        reaches them, and needs the same vectors to do it with.

        Each rowid carries `as_similarity(d)` — **a similarity, not a distance**,
        converted in `_nearest` and not again here.  That is what `_nearest`
        warns about below, and this method used to be the thing it was warning
        about: it took an already-converted similarity under a variable named
        `distance` and inverted it a second time, so the nearest tool carried the
        lowest score in a result a model reads.

        The **raw** sentences, not normalized ones: normalization is the keyword
        leg's rule, and it inserts a space between every pair of CJK characters,
        which is what makes them tokens there and is nonsense as text handed to a
        model.
        """
        wanted = [sentence for sentence in sentences if sentence.strip()]
        if not wanted:
            return [], []
        vectors = await embedder.embed(wanted)
        if len(vectors) != len(wanted):
            raise RuntimeError(
                f"the embedding model answered {len(vectors)} vectors for "
                f"{len(wanted)} sentences, so this search cannot say what a "
                f"tool is about"
            )
        return vectors, [
            await asyncio.to_thread(
                self._nearest, sqlite_vec.serialize_float32(vector), k
            )
            for vector in vectors
        ]

    def _nearest(self, query_vector: bytes, k: int) -> list[tuple[int, float]]:
        """The KNN, and the dedup it cannot do itself.

        `k` counts *chunks*, and a tool with a long schema has several, so the
        best chunk per tool is what is kept — a KNN in `vec0` refuses the
        `GROUP BY` that would say so.

        **Rows from two models cannot be ranked against each other, and this
        does not have to check.**  It used to say it skipped a vector of another
        width, and nothing did: what makes that impossible is `ensure_index`,
        which drops and rebuilds the whole table when the identity changes — so
        one table is one model's, and the width is fixed by its DDL.  A second
        guarantee here would be a second thing to keep true, for a state the
        first one cannot reach.
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
            # `as_similarity`, the same conversion the turn store makes, and it
            # is not cosmetic that both make it: this one's number is printed in
            # a search result a *model* reads, and a model told "similarity 0.30"
            # for the nearest row and 0.70 for a worse one sorts them backwards.
            nearest.setdefault(
                str(row["name"]),
                (int(row["rowid"]), as_similarity(row["distance"])),
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

        The query is `rows_in_order`, which is where the ordering, the
        re-applied filters and the cap on the bound list are argued; what is
        this store's own is the connection, because this is the call that runs
        on a thread hop.
        """
        with self._connect() as connection:
            return rows_in_order(
                connection,
                rowids,
                select="SELECT rowid AS rowid, * FROM tool",
                column="rowid",
                clauses=clauses,
                values=values,
                limit=limit,
            )

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
        # **A batch at a time, and the loop is what finishes the index.**  A
        # first start over a config with a published server in it is thousands
        # of tools, and one request carrying every one of them is the request
        # that times out — which fails the tool list rather than embedding the
        # rest, since the caller is the hub's own startup.  `SYNC_BATCH` is what
        # bounds a call, and the loop belongs here for the reason the cabinet
        # states: the store bounds, the caller finishes.
        #
        # It terminates because `_place` writes a chunk row for every tool it
        # was handed, and every tool has at least a name to be a chunk of
        # (`tool_document`) — so a row that was selected is a row that is no
        # longer missing.  An empty answer is not the other way out: `_embed`
        # refuses an answer shorter than the batch, so the loop ends either with
        # every tool placed or with the failure named.
        placed = 0
        while True:
            missing = await asyncio.to_thread(self.tools_without_vectors, SYNC_BATCH)
            if not missing:
                break
            vectors = await self._embed(missing, embedder)
            await asyncio.to_thread(self._place, vectors)
            placed += len(vectors)
        return {
            "vectors": placed,
            "identity": vector_identity(embedder),
        }

    def ensure_index(self, embedder: Embedder) -> None:
        """Create the vector index, or rebuild it if its identity has changed.

        Cheap when it is current: one read of `meta`.  A rebuild here **drops
        the vectors** without re-embedding anything, because the caller is a
        reconcile that is embedding the rows it just wrote — putting the rest
        back is `sync_indexes`, and the two are separate for the same reason
        they are in the turn store.

        The rebuild is `slife2.libs.store.index.rebuild_vector_index`'s.  Two of its three
        callers pass `meta_table="meta"`, and this is that difference: the
        catalogue's file has called its table `meta` since before the other two
        existed.
        """
        identity = vector_identity(embedder)
        with self._connect() as connection:
            rebuilt = rebuild_vector_index(
                connection,
                vec_table="tool_vec",
                chunk_table="tool_chunk",
                dimension=embedder.dimension,
                identity=identity,
                meta_table="meta",
            )
        if rebuilt:
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
                index_meta(connection, "meta").get("text_version", "")
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
            set_index_meta(connection, "meta", text_version=textindex.RULES_VERSION)

    def tools_without_vectors(self, limit: int | None = None) -> list[tuple[str, str]]:
        """Rows the vector index does not hold, as `(name, document)`.

        Oldest first, so a first run embeds a server's tools in the order they
        were first seen rather than in whatever order the table happens to be
        in.  "Has no vector" means exactly one thing here — the row is new, or
        its text moved — because those are the only two paths that leave a row
        without one; a rebuild for a changed model leaves them all without one,
        which is how the same query fills the index back in.

        `limit` is how many one call may carry, and the caller loops until this
        returns nothing (`sync_indexes`).  Bounded because a first run is every
        tool of every server, and one request for all of them is the request
        that times out.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT t.rowid AS rowid, t.name AS name, t.description AS description,"
                " t.schema AS schema FROM tool t"
                " LEFT JOIN tool_chunk c ON c.name = t.name"
                " WHERE c.id IS NULL"
                " ORDER BY t.rowid"
                " LIMIT ?",
                (limit or -1,),
            ).fetchall()
        return [(str(row["name"]), tool_document(dict(row))) for row in rows]


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

    **A name beginning with `_` is not injected** — with the exceptions in
    `MODEL_VISIBLE_HARNESS_TOOLS`, below.  v1's convention, and it is a name and
    not a column for v1's reason: what makes a tool the machinery's is that the
    *machinery* calls it, and a fact about who calls a thing belongs on the
    thing, where both sides can read it without a second register to keep in
    step.  (`slife2.libs.turn.tools` is where the word is defined; the mark is about who
    calls it, and this filter is a *policy about the list* that mostly coincides
    with it.)

    **The exceptions are the harness pairs** — `_func_tool_unload` for a turn's
    trim, `_check_new_input` for a message delivered inside a running turn.  Each
    is written into the conversation as a tool pair, a pair names a tool, and a
    request whose history calls a tool its `tools` array does not declare is a
    400 from the Responses and Messages backends (v1's rule).  A tool the model
    reads in its own history but cannot call would be inconsistent with itself,
    so each is injected, callable, and named the same way it is named here — and
    what qualifies one for this exception is being a pair, which is why the set
    is written out beside the tool names rather than derived from anything.
    """
    visible = _in_list(MODEL_VISIBLE_HARNESS_TOOLS)
    return (
        f"category IN ({_in_list(FUNCTION_CATEGORIES)})"
        f" AND load_status = '{LOADED}'"
        f" AND source_id IN ({_marks(sources)})"
        f" AND (name NOT LIKE '\\_%' ESCAPE '\\' OR name IN ({visible}))"
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

    `slife2.libs.store.index.place_vectors` with this corpus's names on it, and
    `clear_first=True` is the whole of what the catalogue asks for beyond the
    turn store: a tool's row is *rewritten* when its schema or description
    moves, `tool_chunk.id` is a plain rowid SQLite hands out again, and a new
    chunk written over an old id would write a vector over one `tool_vec` still
    holds.  The delete is what makes re-embedding a row safe.
    """
    place_vectors(
        connection,
        chunk_table="tool_chunk",
        vec_table="tool_vec",
        key_column="name",
        row_key=name,
        vectors=vectors,
        what=f"a chunk of {name!r}",
        clear_first=True,
    )


def _clear_vectors(connection: sqlite3.Connection, names: Sequence[str]) -> None:
    """Forget the vectors of these tool rows, keeping the rows themselves.

    `slife2.libs.store.index.clear_vectors` with this corpus's names on it — the one place
    the catalogue calls it for several rows at once (`reset`), which is why the
    key is a list here and a single row in the two writers.
    """
    clear_vectors(
        connection,
        chunk_table="tool_chunk",
        vec_table="tool_vec",
        key_column="name",
        keys=names,
    )
