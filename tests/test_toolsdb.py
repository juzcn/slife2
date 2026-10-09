"""The tool catalogue, on its own.

`tests/test_toolhub.py` reaches it through a hub and two MCP hops, so what it
can assert is what survives the trip.  These are about the store itself: what a
merge does to the rows, what the gate answers, who the budget may take out, and
what the two search legs find.  The store is where the four things the whole
design rests on are decided — a name is a row's identity, off is not down, the
load state is the model's and nobody else's, and a name beginning with `_` is
the harness's.

The embedder is the deterministic stub, for the reason `tests/test_db.py` gives:
these are about what the store does with a vector, not about the model that
produced it.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from slife2.db import (
    LOADED,
    NA,
    STATUS_DISABLED,
    STATUS_ENABLED,
    STATUS_ERROR,
    UNLOADED,
    ToolStore,
    fuse_by_best_rank,
    fuse_ranked,
)
from tests.fakes import StubEmbedder

pytestmark = pytest.mark.unit

#: Saving a row embeds it, and the vectors have to be comparable for a semantic
#: assertion to mean anything — see `tests.fakes.StubEmbedder`.
EMBEDDER = StubEmbedder()


def store_at(tmp_path, **kwargs) -> ToolStore:
    """A catalogue with the defaults these tests want: room to hold everything.

    `autoload` and `disabled` are not here any more, and their absence is the
    point: which sources are wanted every turn and which are switched off used to
    be read off the config at construction, and both arrive with the *merge* now
    — `autoload` as an argument, the switch as a row's own status.
    """
    return ToolStore(tmp_path / "tools.db", threshold=kwargs.pop("threshold", 100))


def tool(
    name: str, description: str = "Does a thing.", schema: str = "", **extra
) -> dict:
    """One row of a source's tool list, in the shape the hub sends.

    `extra` is for the two document families, whose rows may carry a `status`:
    nothing else has a verdict of its own to send (`slife2.db._plan`).
    """
    return {
        "name": name,
        "description": description,
        "remote_name": name.split("__", 1)[-1],
        "schema": schema,
        **extra,
    }


def merge(
    store: ToolStore,
    source: str,
    category: str,
    rows: list[dict],
    *,
    autoload: bool = False,
) -> dict:
    """One source's list, merged.  The embedder is a parameter of the real call.

    `autoload` travels with the merge now rather than with the store's
    construction: the operator's `autoload: true` lives in the section a plugin
    owns, so the plugin states it and the hub passes it on.
    """
    return asyncio.run(
        store.merge(source, category, rows, embedder=EMBEDDER, autoload=autoload)
    )


def rows_in(store: ToolStore) -> list[tuple]:
    """The stored rows, as `(name, status, load_status)` — ordered for comparing."""
    with sqlite3.connect(store.path) as connection:
        return connection.execute(
            "SELECT name, status, load_status FROM tool ORDER BY name"
        ).fetchall()


# --- the merge: four outcomes, and no fifth -----------------------------------


def test_a_new_tool_is_added_and_a_steady_state_writes_nothing(tmp_path) -> None:
    """The delta contract, which is what makes a per-call merge affordable.

    The hub merges a source's whole list every time it lists one, so a merge
    that rewrote every row would rewrite the keyword document with it — and a
    steady state has to cost one comparison per tool and not one write.
    """
    store = store_at(tmp_path)
    first = merge(store, "arxiv", "mcp", [tool("arxiv__search")])
    assert first["inserted"] == ["arxiv__search"]

    again = merge(store, "arxiv", "mcp", [tool("arxiv__search")])
    assert again["inserted"] == []
    assert again["updated"] == []
    assert again["skipped"] == 1, "one row, compared and left alone"


def test_a_tool_whose_description_moved_is_updated(tmp_path) -> None:
    store = store_at(tmp_path)
    merge(store, "arxiv", "mcp", [tool("arxiv__search", "Search papers.")])
    moved = merge(
        store, "arxiv", "mcp", [tool("arxiv__search", "Search papers by author.")]
    )

    assert moved["updated"] == ["arxiv__search"]
    with sqlite3.connect(store.path) as connection:
        description = connection.execute(
            "SELECT description FROM tool WHERE name = 'arxiv__search'"
        ).fetchone()[0]
    assert description == "Search papers by author."


def test_a_tool_the_source_dropped_is_deleted(tmp_path) -> None:
    """A list is the whole truth about a source: a name missing from it is gone.

    And the row's derived parts go with it — the keyword document and the
    vectors — because a hit that resolves to no row is a search that returns a
    name nothing can answer for.
    """
    store = store_at(tmp_path)
    merge(
        store, "arxiv", "mcp", [tool("arxiv__a"), tool("arxiv__b", schema="{'q': 1}")]
    )
    dropped = merge(store, "arxiv", "mcp", [tool("arxiv__a")])

    assert dropped["purged"] == ["arxiv__b"]
    assert [row[0] for row in rows_in(store)] == ["arxiv__a"]
    with sqlite3.connect(store.path) as connection:
        chunks = connection.execute(
            "SELECT COUNT(*) FROM tool_chunk WHERE name = 'arxiv__b'"
        ).fetchone()[0]
    assert chunks == 0


def test_a_name_belonging_to_another_source_is_refused(tmp_path) -> None:
    """`name` is the row's identity, so two sources cannot own one.

    Refused rather than written over: silently replacing somebody's tool is how
    a model calls `search` and reaches a different server than the one it read
    about.  The whole merge fails, so nothing of the second source is
    half-recorded either.
    """
    store = store_at(tmp_path)
    merge(store, "arxiv", "mcp", [tool("arxiv__search")])

    with pytest.raises(ValueError, match="already arxiv's tool"):
        merge(store, "other", "mcp", [tool("arxiv__search")])
    assert [row[0] for row in rows_in(store)] == ["arxiv__search"]


def test_a_source_that_answers_again_puts_its_rows_back(tmp_path) -> None:
    """`error` is a verdict about now, and the next successful list withdraws it.

    The load state is not touched by any of it: what the model decided is not a
    thing a reconnect gets to have an opinion about.
    """
    store = store_at(tmp_path)
    merge(store, "arxiv", "mcp", [tool("arxiv__search")])
    store.set_load("arxiv__search", LOADED)
    store.set_source_state("arxiv", STATUS_ERROR)
    assert rows_in(store)[0][1] == STATUS_ERROR

    merge(store, "arxiv", "mcp", [tool("arxiv__search")])
    assert rows_in(store)[0] == ("arxiv__search", "enabled", LOADED)


def test_rows_and_vectors_are_written_together(tmp_path) -> None:
    """There is no window in which a row is stored and unindexed.

    The merge embeds what changed *before* it writes, so a merge that cannot
    embed stores nothing — which is the turn store's arrangement and the reason
    there is no drainer and no "the index is not ready" state.
    """
    store = store_at(tmp_path)
    merge(store, "arxiv", "mcp", [tool("arxiv__a", schema='{"q": "a query"}')])

    with sqlite3.connect(store.path) as connection:
        chunks = connection.execute("SELECT COUNT(*) FROM tool_chunk").fetchone()[0]
    assert chunks == 1, "the schema is embeddable, so it has a vector"

    # A row that declares nothing still has a vector: what a tool is *about* is
    # its name and description, and "open a page and take a screenshot" is a
    # description rather than an argument list.  v1 asked whether the *schema*
    # was worth embedding, because in v1 that column held the whole tool
    # definition; here it holds only the parameters, and a tool with none is
    # exactly the kind a model looks for by what it does.
    merge(
        store,
        "arxiv",
        "mcp",
        [tool("arxiv__a", schema='{"q": "a query"}'), tool("arxiv__b")],
    )
    with sqlite3.connect(store.path) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM tool_chunk")]
    assert names == ["arxiv__a", "arxiv__b"]

    # A schema that moves takes its vectors with it: they were made from text
    # that is no longer there — and the new text gets its own, in the same
    # transaction, so nothing is ever left unindexed.
    merge(store, "arxiv", "mcp", [tool("arxiv__a"), tool("arxiv__b")])
    with sqlite3.connect(store.path) as connection:
        kept = connection.execute(
            "SELECT name, COUNT(*) FROM tool_chunk GROUP BY name ORDER BY name"
        ).fetchall()
    assert kept == [("arxiv__a", 1), ("arxiv__b", 1)]


def test_a_stale_file_is_rebuilt_rather_than_migrated(tmp_path) -> None:
    """`CREATE TABLE IF NOT EXISTS` never touches an existing table.

    So a file written by another build keeps its own columns and its own CHECK
    for as long as it lives, and the first INSERT fails with something that
    reads like a bug in this code.  This module has no migration layer — the
    rows are derived from servers that can be asked again, and the only thing
    that is not derived is the loaded set, which a model can restore with one
    call each — so the answer is to empty it and build it again.
    """
    path = tmp_path / "tools.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE tool (name TEXT PRIMARY KEY, nonsense TEXT)")

    store = ToolStore(path, threshold=100)
    merge(store, "arxiv", "mcp", [tool("arxiv__search")])
    assert [row[0] for row in rows_in(store)] == ["arxiv__search"]


def test_a_catalogue_from_before_the_use_stamp_is_rebuilt(tmp_path) -> None:
    """The upgrade this change is, and the reason it needs no migration.

    A file written before `last_used` existed has every other column, so the
    test above — a table that is obviously nothing like this one — is not the
    case anybody will actually meet.  This is: the check is the column set in
    *both* directions, so a table that is merely missing one is as stale as one
    carrying a column nothing writes.  Nothing is carried over: the rest of the
    row is derived from servers that can be asked again, and the loaded set is
    restored by the model with one `func_tool_load` each.
    """
    path = tmp_path / "tools.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE tool (name TEXT PRIMARY KEY, description TEXT,"
            " category TEXT, source_id TEXT, remote_name TEXT, schema TEXT,"
            " status TEXT, load_status TEXT, last_loaded TEXT)"
        )

    store = ToolStore(path, threshold=100)
    merge(store, "arxiv", "mcp", [tool("arxiv__search")])
    assert [row[0] for row in rows_in(store)] == ["arxiv__search"]


def test_a_file_whose_check_predates_a_value_is_rebuilt(tmp_path) -> None:
    """A closed domain is what decides an INSERT, and there are three of them.

    A file whose `category` list is current and whose `status` list is not is
    the case a category-only check waves through: every column is there, the
    open succeeds, and the first verdict that is neither `enabled` nor
    `disabled` raises `IntegrityError` *inside* a tool call — where the hub
    reads it as the db refusing one caller's data and carries on, every turn.
    """
    path = tmp_path / "tools.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE tool (name TEXT PRIMARY KEY, description TEXT,"
            " category TEXT CHECK (category IN ('plugin','mcp','rest','skill')),"
            " source_id TEXT, remote_name TEXT, schema TEXT,"
            " status TEXT CHECK (status IN ('enabled','disabled')),"
            " load_status TEXT, last_loaded TEXT, last_used TEXT)"
        )

    store = ToolStore(path, threshold=100)
    merge(store, "arxiv", "mcp", [tool("arxiv__search")])

    assert store.set_source_state("arxiv", STATUS_ERROR) == 1, "a verdict was refused"
    assert rows_in(store) == [("arxiv__search", STATUS_ERROR, UNLOADED)]


def test_a_skill_row_carries_no_load_state(tmp_path) -> None:
    """`n/a` is what the column documents for a skill, and what its filter means.

    A skill is a document the hub reads, not a tool behind a connection: there
    is no load step for "not loaded yet" to describe, and `tool_search`'s
    `load_status` filter says `n/a` for exactly this row — so a skill stored as
    `unloaded` is one the model is told to load and can never find.
    """
    store = store_at(tmp_path)
    merge(store, "skills", "skill", [tool("skill:browser-harness")])

    assert rows_in(store) == [("skill:browser-harness", STATUS_ENABLED, NA)]


def test_a_move_that_is_not_the_document_does_not_re_embed(tmp_path) -> None:
    """A vector is of the name, the description and the schema — nothing else.

    So a `remote_name` that moved leaves the document byte-identical, and
    re-embedding it would buy the same vector for the price of an embedding
    call and a rewritten row on a path that runs every time a server is
    listed.
    """
    embedder = StubEmbedder()
    store = store_at(tmp_path)
    asyncio.run(store.merge("arxiv", "mcp", [tool("arxiv__search")], embedder=embedder))
    before = len(embedder.calls)

    renamed = tool("arxiv__search")
    renamed["remote_name"] = "search_v2"
    answer = asyncio.run(store.merge("arxiv", "mcp", [renamed], embedder=embedder))

    assert answer["updated"] == ["arxiv__search"], "the row did move"
    assert len(embedder.calls) == before, "a renamed tool was embedded again"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT remote_name FROM tool").fetchall() == [
            ("search_v2",)
        ]


# --- the boot pass: off is not down -------------------------------------------


def test_the_boot_pass_withdraws_only_what_the_config_cannot_justify(tmp_path) -> None:
    """A source the config still names is the hub's to speak for, not this file's.

    Whether a server is *answering* is a fact about connections, and this store
    holds none — so marking its rows `error` on the way up would be a guess, and
    a visible one: a db restarted under a running hub would report every tool as
    unusable while the model was still holding and calling them.  What the config
    can speak to on its own is a source it no longer names at all, and that is
    what the boot pass withdraws.
    """
    path = tmp_path / "tools.db"
    store = ToolStore(path, threshold=100)
    merge(store, "arxiv", "mcp", [tool("arxiv__search")])
    merge(store, "gone", "mcp", [tool("gone__thing")])

    # A new run: `arxiv` is still configured, `gone` is not.
    again = ToolStore(path, threshold=100, known={"arxiv"})
    assert [row for row in rows_in(again) if row[0] == "arxiv__search"] == [
        ("arxiv__search", "enabled", "unloaded")
    ], "left alone: the hub is about to say whether it is answering"
    assert [row for row in rows_in(again) if row[0] == "gone__thing"] == [
        ("gone__thing", STATUS_ERROR, "unloaded")
    ]


def test_a_source_that_is_switched_off_is_not_a_source_that_is_down(
    tmp_path,
) -> None:
    """The distinction the column exists for, and who writes which verdict.

    **Off is a decision and down is a verdict**, and they arrive by different
    roads now.  Which sources the operator switched off used to be read off the
    config when the store was built; it comes as a *declaration* instead — the
    plugin that owns the section states it, and its rows carry `status:
    disabled` — which is the one status a merge writes from the row rather than
    from the runtime.  Nothing about the runtime can overwrite it: `error` is
    the word for a source that is not answering, and a switched-off one is not
    answering because nobody asked it to.
    """
    store = store_at(tmp_path)
    merge(store, "filesystem", "mcp", [tool("filesystem__read")])
    assert rows_in(store)[0][1] == "enabled", "a source that answered is usable"

    # The switch: the row says so itself, which is the only way it arrives.
    merge(
        store,
        "filesystem",
        "mcp",
        [{**tool("filesystem__read"), "status": STATUS_DISABLED}],
    )
    assert rows_in(store)[0][1] == STATUS_DISABLED

    # And the runtime cannot talk it back on — off is not a connection state.
    store.set_source_state("filesystem", STATUS_ERROR)
    assert rows_in(store)[0][1] == STATUS_DISABLED

    # The way back is the operator's, through the same door: a declaration that
    # no longer says `disabled`.
    merge(store, "filesystem", "mcp", [tool("filesystem__read")])
    assert rows_in(store)[0][1] == "enabled"


def test_the_boot_pass_counts_what_it_moved(tmp_path) -> None:
    """`reset` answers with what it changed, which is what a log line reads.

    Nothing connected is a verdict per row, and a run that changes nothing —
    because nothing had been stored yet — says so rather than reporting work it
    did not do.
    """
    store = ToolStore(tmp_path / "tools.db", threshold=100)
    assert store.reset() == {"error": 0}

    merge(store, "arxiv", "mcp", [tool("arxiv__search"), tool("arxiv__other")])
    assert store.reset() == {"error": 2}
    assert store.reset() == {"error": 0}, "already said"


# --- the gate -----------------------------------------------------------------


def test_the_gate_answers_with_loaded_tools_of_live_sources(tmp_path) -> None:
    """Three conditions, and each of them is somebody else's fact.

    `load_status` is the model's decision, `source_id` is liveness — which only
    the hub can know, because it holds the connections — and the category is
    what makes a row a function tool at all.  A tool that is merely *known* is
    not injected: discovery never puts anything in front of a model by itself.
    """
    store = store_at(tmp_path)
    merge(store, "arxiv", "mcp", [tool("arxiv__search"), tool("arxiv__other")])
    assert store.injectable(["arxiv"])["tools"] == []

    store.set_load("arxiv__search", LOADED)
    injected = [row["name"] for row in store.injectable(["arxiv"])["tools"]]
    assert injected == ["arxiv__search"]

    # A source that is not live contributes nothing, however loaded it is.
    assert store.injectable(["builtins"])["tools"] == []


def test_a_harness_tool_is_injected_only_by_name(tmp_path) -> None:
    """A name beginning with `_` is the machinery's, and the gate keeps it back.

    The catalogue holds its row either way — it is a tool like everything else,
    with a state and a source — so the *gate* is what decides, and the
    convention is a name rather than a column for v1's reason: what makes a tool
    the harness's is who calls it, and that belongs on the thing itself.

    **`_func_tool_unload` is the one exception, and it is by name and not by
    shape.**  The harness's trim is recorded in the conversation as a tool pair,
    so the model reads this name in its own history; a tool the model can read
    there but not call would be inconsistent with itself, and v1's rule is that
    the name has to be declared for the backends to take the pair at all.  Any
    other underscore name stays out — which is what the second half of this
    asserts, and it is the half that keeps the convention from eroding into
    "names the hub happens to like".
    """
    store = store_at(tmp_path)
    merge(
        store,
        "toolhub",
        "plugin",
        [tool("_func_tool_unload"), tool("_func_something_new"), tool("tool_search")],
    )

    injected = [row["name"] for row in store.injectable(["toolhub"])["tools"]]
    assert injected == ["_func_tool_unload", "tool_search"]


def test_a_document_row_is_findable_and_never_injected(tmp_path) -> None:
    """A skill and a `cli:` entry are rows, and they are not tools.

    Both halves are the point.  The row exists so `tool_search` can reach a
    playbook or a command the model would otherwise have to know the name of —
    and it carries no load state (`n/a`), which is also what keeps it out of the
    model's list: the gate is the function categories, so a row can be findable
    without being callable.  Nothing is connected to either family, so nothing
    could be "loaded" and nothing can be down.
    """
    store = store_at(tmp_path)
    merge(
        store,
        "skills",
        "skill",
        [tool("skill:browser-harness", "Drive a browser.", schema="# Browser")],
    )
    merge(
        store,
        "cli",
        "cli",
        [tool("cli:yt-dlp", "Download video.", schema="yt-dlp")],
    )

    assert rows_in(store) == [
        ("cli:yt-dlp", "enabled", "n/a"),
        ("skill:browser-harness", "enabled", "n/a"),
    ]
    assert store.injectable(["skills", "cli"])["tools"] == []
    assert merge(store, "cli", "cli", [])["purged"] == ["cli:yt-dlp"]


def test_a_document_row_carries_its_own_verdict(tmp_path) -> None:
    """The one family whose status comes from its source rather than from a link.

    Every other row's status is the *runtime's*, because whether a server is up
    is not something a config can say — and the merge re-enables what an older
    config switched off, since a merge only runs for a source that answered.
    For these two there is no link to answer, so the mirror is the authority:
    a `cli:` entry with `enabled: false` is `disabled`, and a `SKILL.md` that
    cannot be read is `error` (v1's `status_verdict`, in this build's words).

    **And a re-merge must not undo it** — the mirror runs again before every
    search, so "re-enable what the config switched off" would flip this row
    back on several times a minute.
    """
    store = store_at(tmp_path)
    off = {
        "name": "cli:iflow",
        "description": "Switched off.",
        "remote_name": "iflow",
        "schema": "iflow",
        "status": "disabled",
    }
    unreadable = {
        "name": "skill:broken",
        "description": "",
        "remote_name": "broken",
        "schema": "",
        "status": "error",
    }
    merge(store, "cli", "cli", [off])
    merge(store, "skills", "skill", [unreadable])

    assert rows_in(store) == [
        ("cli:iflow", "disabled", "n/a"),
        ("skill:broken", "error", "n/a"),
    ]
    merge(store, "cli", "cli", [off])
    merge(store, "skills", "skill", [unreadable])
    assert rows_in(store) == [
        ("cli:iflow", "disabled", "n/a"),
        ("skill:broken", "error", "n/a"),
    ], "a mirror does not reconnect a document"


def test_a_plugins_tools_start_loaded_and_a_servers_do_not(tmp_path) -> None:
    """Ours are few and wanted; somebody else's may be ninety and are on demand.

    `autoload: true` is the operator saying a server is wanted every turn, and
    it decides the same set the budget protects: a tool that starts loaded
    because somebody asked for it must not be evicted by the count either.
    """
    store = store_at(tmp_path)
    merge(store, "builtins", "plugin", [tool("calc")])
    merge(store, "serper", "mcp", [tool("serper__search")], autoload=True)
    merge(store, "arxiv", "mcp", [tool("arxiv__search")])

    assert rows_in(store) == [
        ("arxiv__search", "enabled", "unloaded"),
        ("calc", "enabled", LOADED),
        ("serper__search", "enabled", LOADED),
    ]


def test_a_reconnect_never_touches_what_the_model_decided(tmp_path) -> None:
    """The one column in this file that is not derived from anything.

    Every other column can be rebuilt by asking the servers again; a load cannot
    be, which is why a merge writes the seed on a *new* row and nothing at all
    on a row that already exists.
    """
    store = store_at(tmp_path)
    merge(store, "arxiv", "mcp", [tool("arxiv__search")])
    store.set_load("arxiv__search", LOADED)

    merge(store, "arxiv", "mcp", [tool("arxiv__search", "A new description.")])
    assert rows_in(store)[0][2] == LOADED


# --- loading and the budget ---------------------------------------------------


def test_loading_refuses_for_a_reason_and_not_with_a_shrug(tmp_path) -> None:
    """Each refusal is a different thing to do about it, so each has its own word.

    A store answers with the *fact*; the sentence a model reads is built by the
    hub.  Two of these are the owner's state rather than the row's, and the
    order they are asked in matters: a tool can be loaded *and* unusable, and
    "already loaded" there would be a lie the model can see through.
    """
    store = store_at(tmp_path)
    merge(store, "arxiv", "mcp", [tool("arxiv__search")])
    # The switch is a row's own status, which is how it arrives now: the section
    # it was written in belongs to a plugin, so the plugin states the verdict and
    # the merge writes it.
    merge(
        store,
        "filesystem",
        "mcp",
        [{**tool("filesystem__read"), "status": STATUS_DISABLED}],
    )
    merge(store, "skills", "skill", [tool("skill:browser")])
    store.set_source_state("arxiv", STATUS_ERROR)

    assert store.set_load("nothing", LOADED)["outcome"] == "unknown"
    assert store.set_load("skill:browser", LOADED)["outcome"] == "no_load_state"
    assert store.set_load("filesystem__read", LOADED)["outcome"] == "disabled"
    assert store.set_load("arxiv__search", LOADED)["outcome"] == "error"
    assert store.set_load("arxiv__search", "unloaded")["outcome"] == "already"
    assert store.set_load("nothing", "unloaded")["outcome"] == "unknown"


def stamp(store: ToolStore, name: str, when: str) -> None:
    """Set one row's recency by hand — both stamps, to the same moment.

    The budget orders by the newer of `last_loaded` and `last_used`, and both
    are written by the clock `slife2.clock` owns — which is second-precision on
    purpose, so three tools loaded in one call share a stamp and the order falls
    back to the name.  A test about the *ordering* therefore has to say what the
    stamps are, exactly as a test about a window has to say what the timestamps
    are.

    Both, because that is the honest shape of a row in a test: a tool that was
    loaded and called at one moment has one moment in it.  What the two stamps
    are *for* — a call against a load — needs them apart, and that is
    `called` and `stamps`.
    """
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE tool SET last_loaded = ?, last_used = ? WHERE name = ?",
            (when, when, name),
        )


def called(store: ToolStore, name: str, when: str) -> None:
    """Say when the model last called one row, leaving its load stamp alone.

    `stamp` sets both stamps, which is what an ordering test wants; this is the
    one test that needs them apart, because "a call outranks a load that came
    after it" is not a sentence either stamp can state by itself.
    """
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE tool SET last_used = ? WHERE name = ?", (when, name))


def stamps(store: ToolStore, name: str) -> tuple[str, str]:
    """One row's two recency stamps, `(last_loaded, last_used)`."""
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT last_loaded, last_used FROM tool WHERE name = ?", (name,)
        ).fetchone()
    return (str(row[0]), str(row[1])) if row else ("", "")


def test_the_budget_takes_the_least_recently_used_and_nothing_else(tmp_path) -> None:
    """Three candidates, and the two the system will not give up.

    A plugin's tools and anything the operator marked `autoload: true` are never
    victims — and which sources those are is a *parameter* of the eviction now
    rather than something this file read off the config when it opened, because
    the section they were written in belongs to a plugin:
    the budget exists to stop somebody else's ninety tools crowding the request,
    not to take away a tool slife2 guarantees — a model that has quietly lost
    `now` and `calc` is the failure DESIGN.md §8 is built around.
    """
    store = store_at(tmp_path, threshold=4)
    merge(store, "builtins", "plugin", [tool("calc")])
    merge(store, "serper", "mcp", [tool("serper__search")], autoload=True)
    merge(
        store,
        "arxiv",
        "mcp",
        [tool("arxiv__old"), tool("arxiv__new"), tool("arxiv__also")],
    )
    for name in ("arxiv__old", "arxiv__new", "arxiv__also"):
        store.set_load(name, LOADED)
    stamp(store, "arxiv__old", "2026-01-01T00:00:00+00:00")
    stamp(store, "arxiv__also", "2026-01-02T00:00:00+00:00")
    stamp(store, "arxiv__new", "2026-01-03T00:00:00+00:00")

    taken = store.evict(["builtins", "serper", "arxiv"], autoload=["serper"])

    assert taken == ["arxiv__old"], "the oldest of the three, and only it"
    loaded = {row[0]: row[2] for row in rows_in(store)}
    assert loaded["calc"] == LOADED
    assert loaded["serper__search"] == LOADED


def test_the_budget_counts_what_the_model_is_holding(tmp_path) -> None:
    """A source that is down cannot absorb it by being counted and not injected.

    The count and the victim query are the same predicate, so a tool whose
    server has stopped answering — or one the config switched off — is neither a
    reason to trim nor something a trim can take.
    """
    store = store_at(tmp_path, threshold=1)
    merge(store, "arxiv", "mcp", [tool("arxiv__a"), tool("arxiv__b")])
    for name in ("arxiv__a", "arxiv__b"):
        store.set_load(name, LOADED)

    assert store.evict(["elsewhere"]) == [], "not what the model is holding"

    # And within what it *is* holding, recency is what decides.
    stamp(store, "arxiv__a", "2026-01-01T00:00:00+00:00")
    stamp(store, "arxiv__b", "2026-01-02T00:00:00+00:00")
    assert store.evict(["arxiv"]) == ["arxiv__a"]


def test_a_call_outranks_a_load_that_came_after_it(tmp_path) -> None:
    """The reason there are two stamps and not one.

    Both rows were loaded, `arxiv__b` after `arxiv__a`, and then the model
    *called* `a`.  Ordering by the load alone takes the tool it is working with
    and keeps the one it has only read the name of.
    """
    store = store_at(tmp_path, threshold=1)
    merge(store, "arxiv", "mcp", [tool("arxiv__a"), tool("arxiv__b")])
    for name in ("arxiv__a", "arxiv__b"):
        store.set_load(name, LOADED)
    stamp(store, "arxiv__a", "2026-01-01T00:00:00+00:00")
    stamp(store, "arxiv__b", "2026-01-02T00:00:00+00:00")
    called(store, "arxiv__a", "2026-01-03T00:00:00+00:00")

    assert store.evict(["arxiv"]) == ["arxiv__b"], "the one it has not called"


def test_a_reload_outranks_a_call_that_came_before_it(tmp_path) -> None:
    """The newer stamp wins, whichever kind it is — which is why it is `MAX`.

    `arxiv__again` was called once, long ago, and then loaded again; `other` has
    been in the list since and has never been called.  Ordering by the call
    alone would take the tool the model asked for a moment ago; the newer of the
    two takes the one nothing has happened to since it arrived.
    """
    store = store_at(tmp_path, threshold=1)
    merge(store, "arxiv", "mcp", [tool("arxiv__again"), tool("arxiv__other")])
    for name in ("arxiv__again", "arxiv__other"):
        store.set_load(name, LOADED)
    stamp(store, "arxiv__again", "2026-01-05T00:00:00+00:00")
    called(store, "arxiv__again", "2026-01-01T00:00:00+00:00")
    stamp(store, "arxiv__other", "2026-01-03T00:00:00+00:00")

    assert store.evict(["arxiv"]) == ["arxiv__other"]


def test_a_call_is_recorded_even_for_a_row_the_model_is_not_holding(
    tmp_path,
) -> None:
    """A name found with `tool_search` can be called without being loaded.

    The call is evidence about the tool, so it is written down wherever the row
    is — and it is harmless there, because what the ordering reads is the newer
    of the two stamps, and a row that is loaded later refreshes its load stamp.
    """
    store = store_at(tmp_path)
    merge(store, "arxiv", "mcp", [tool("arxiv__a")])
    assert stamps(store, "arxiv__a") == ("", ""), "not loaded, never called"

    assert store.touch("arxiv__a") == 1
    assert stamps(store, "arxiv__a")[1] != "", "the call is what was recorded"
    assert store.touch("nothing") == 0, "a name with no row is not an error"


# --- both legs ------------------------------------------------------------------


def test_a_tool_is_found_by_its_words_and_by_what_it_is_about(tmp_path) -> None:
    """Two legs, one fusion — the same shape the turn store's search has.

    `bm25` over the text a tool is described by, a cosine distance over a vector
    of the same text, fused by rank because the two scores are not on one scale.
    Ranks and not sets, which is what these assertions are: a KNN answers with
    its k nearest whatever the distance, so a search returns rows no leg would
    have chosen and the *ordering* is the answer.
    """
    store = store_at(tmp_path)
    merge(
        store,
        "browser",
        "mcp",
        [tool("browser__open", "工具测试浏览器 — open a page and take a screenshot.")],
    )
    merge(store, "arxiv", "mcp", [tool("arxiv__search", "Find papers by keyword.")])

    keyword = asyncio.run(store.search(keywords=["screenshot"], embedder=EMBEDDER))
    assert keyword["results"][0]["name"] == "browser__open", (
        "the only row whose text holds the word"
    )

    # The stub's vocabulary is the four words it counts, and only one row's
    # document holds `测试` — so this is the semantic leg answering.
    semantic = asyncio.run(store.search(sentences=["测试"], embedder=EMBEDDER))
    assert semantic["results"][0]["name"] == "browser__open"
    # **A similarity and not a distance**, which the value pins because the two
    # scales run opposite ways: the row's document holds two of the stub's words
    # (`工具测试`) and the query one, so the cosine is 1/sqrt(2) ≈ 0.71 and a
    # cosine distance would be 0.29.  The number is printed in a search result a
    # model reads and handed to nothing that gates on it here, but the turn store
    # converts its own (`decisions.gate` keeps what is *above* the floor) — so
    # one store returning a distance is one store whose numbers mean the other
    # thing.
    assert semantic["results"][0]["similarity"] == pytest.approx(1 / 2**0.5, abs=0.02)


def test_lists_that_answer_different_questions_do_not_borrow_from_each_other() -> None:
    """A row in two lists beats a row that is first in one — under the wrong rule.

    `fuse_ranked` sums `1/(k+rank)` over its lists, and that is right when they
    are one question asked twice: a row both legs found is evidence about *that*
    question.  Given two different questions it inverts the answer, and not
    marginally — a row present in both scores at least `1/(k+40) + 1/(k+40)`
    = 0.0200, where a row that is *first* in one of them scores `1/(k+1)`
    = 0.0164.  So `20`, second in both lists, is the sum's first answer and the
    best placement's last.

    Both spellings are asserted, because the point of the second is the first:
    whoever reaches for `fuse_ranked` on several sentences should meet this
    test's name rather than a search that answers a question nobody asked.
    """
    one, two, both = 10, 20, 30

    assert next(iter(fuse_ranked({"a": [one, both], "b": [two, both]})))[0] == both, (
        "the sum rewards agreeing across questions"
    )
    assert fuse_by_best_rank([[one, both], [two, both]]) == [
        (one, 1),
        (two, 1),
        (both, 2),
    ], "and the best placement does not"


def test_a_second_question_does_not_demote_what_the_first_one_found(tmp_path) -> None:
    """Another question is another chance, and costs the rows already found nothing.

    The promise a second entry in `sentences` makes, and the one the store broke:
    fused as if both questions were one, the first question's first answer came
    back *below* rows that were merely mediocre in both lists — `fetch` at tenth
    behind two chrome-devtools rows that neither question had ranked above
    thirtieth.

    The stub's vectors are orthogonal, so each question has exactly one row that
    is genuinely nearest and the other at zero.  The second row is merged second
    so that it carries the *higher* rowid, which is what the old tie-break —
    a tie at `1/61 + 1/62` each, settled by the larger id — handed the win to.
    """
    store = store_at(tmp_path)
    merge(store, "browser", "mcp", [tool("browser__tools", "工具")])
    merge(store, "arxiv", "mcp", [tool("arxiv__papers", "测试")])

    alone = asyncio.run(store.search(sentences=["工具"], embedder=EMBEDDER))
    together = asyncio.run(
        store.search(sentences=["工具", "测试"], embedder=EMBEDDER)
    )

    assert alone["results"][0]["name"] == "browser__tools", "the first question"
    assert together["results"][0]["name"] == "browser__tools", (
        "and the second question did not displace it"
    )


def test_a_row_with_nothing_to_embed_is_still_found_by_keyword(tmp_path) -> None:
    """The sentinel is a value in a column, not a document to index or embed.

    A tool that declares no parameters is not thereby unfindable — the keyword
    leg reads its name and description — and it does not get a vector made of
    the word `n/a` either.
    """
    store = store_at(tmp_path)
    merge(store, "arxiv", "mcp", [tool("arxiv__search", "Find papers by keyword.")])

    found = asyncio.run(store.search(keywords=["keyword"], embedder=EMBEDDER))
    assert [row["name"] for row in found["results"]] == ["arxiv__search"]
    assert found["results"][0]["schema_bytes"] == 0


def test_an_empty_query_browses_and_the_filters_narrow(tmp_path) -> None:
    """No query is not no results, and a filter is a question of its own.

    Browsing is how "what is installed" and "what is switched off" become
    answerable — a row that cannot be found by text is still a row.
    """
    store = store_at(tmp_path)
    merge(store, "arxiv", "mcp", [tool("arxiv__search")])
    merge(
        store,
        "filesystem",
        "mcp",
        [{**tool("filesystem__read"), "status": STATUS_DISABLED}],
    )
    browsed = asyncio.run(store.search(embedder=EMBEDDER))
    assert browsed["browsed"] is True
    assert [row["name"] for row in browsed["results"]] == [
        "arxiv__search",
        "filesystem__read",
    ]

    off = asyncio.run(store.search(embedder=EMBEDDER, status=STATUS_DISABLED))
    assert [row["name"] for row in off["results"]] == ["filesystem__read"]
    assert off["results"][0]["status"] == STATUS_DISABLED


def test_the_catalogue_is_one_file_for_the_whole_data_directory(
    tmp_path, monkeypatch
) -> None:
    """Not one per agent, which is the difference from the turns.

    `--agent` partitions the turns and nothing else: the servers are shared, the
    hub is shared, and a tool one conversation loaded is one the next can call.
    Nothing about the path depends on who is asking — which is the whole of what
    "shared" means here.
    """
    from slife2 import paths

    monkeypatch.setenv(paths.DATA_ENV_VAR, str(tmp_path))
    assert paths.tools_db().name == "tools.db"
    assert paths.tools_db().parent == paths.db_dir()
