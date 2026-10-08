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
    STATUS_DISABLED,
    STATUS_ERROR,
    ToolStore,
)
from tests.fakes import StubEmbedder

pytestmark = pytest.mark.unit

#: Saving a row embeds it, and the vectors have to be comparable for a semantic
#: assertion to mean anything — see `tests.fakes.StubEmbedder`.
EMBEDDER = StubEmbedder()


def store_at(tmp_path, **kwargs) -> ToolStore:
    """A catalogue with the defaults these tests want: room, and no autoload."""
    return ToolStore(
        tmp_path / "tools.db",
        threshold=kwargs.pop("threshold", 100),
        autoload=kwargs.pop("autoload", ()),
        disabled=kwargs.pop("disabled", ()),
    )


def tool(name: str, description: str = "Does a thing.", schema: str = "") -> dict:
    """One row of a source's tool list, in the shape the hub sends."""
    return {
        "name": name,
        "description": description,
        "remote_name": name.split("__", 1)[-1],
        "schema": schema,
    }


def merge(store: ToolStore, source: str, category: str, rows: list[dict]) -> dict:
    """One source's list, merged.  The embedder is a parameter of the real call."""
    return asyncio.run(store.merge(source, category, rows, embedder=EMBEDDER))


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


def test_a_source_the_config_switches_off_is_not_a_source_that_is_down(
    tmp_path,
) -> None:
    """The distinction the column exists for, and the reason the order matters.

    The boot pass runs when a store is built — a store is a process that has
    just started, and at that moment nothing is connected — and the config's arm
    runs first: it takes its rows out of the way, and only then does the
    runtime's arm move what is left.  Off is a decision, with nothing wrong
    behind it; down is a verdict that will be withdrawn by itself.
    """
    path = tmp_path / "tools.db"
    first = ToolStore(path, threshold=100)
    merge(first, "filesystem", "mcp", [tool("filesystem__read")])
    assert rows_in(first)[0][1] == "enabled", "a source that answered is usable"

    store = ToolStore(path, threshold=100, disabled={"filesystem"})
    assert rows_in(store)[0][1] == STATUS_DISABLED

    # And the way back is the *merge*, not the boot pass: a source that lists
    # its tools again is a source that is answering, and answering withdraws the
    # verdict — which is what puts a server switched back on into the model's
    # list.  The boot pass cannot know a config it was not given.
    merge(store, "filesystem", "mcp", [tool("filesystem__read")])
    assert rows_in(store)[0][1] == "enabled"


def test_the_boot_pass_counts_what_it_moved(tmp_path) -> None:
    """`reset` answers with what it changed, which is what a log line reads.

    Nothing connected is a verdict per row, and a run that changes nothing —
    because nothing had been stored yet — says so rather than reporting work it
    did not do.
    """
    store = ToolStore(tmp_path / "tools.db", threshold=100)
    assert store.reset() == {"error": 0, "disabled": 0}

    merge(store, "arxiv", "mcp", [tool("arxiv__search"), tool("arxiv__other")])
    assert store.reset() == {"error": 2, "disabled": 0}
    assert store.reset() == {"error": 0, "disabled": 0}, "already said"


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


def test_a_harness_tool_is_never_injected(tmp_path) -> None:
    """A name beginning with `_` is the machinery's, and a model never sees it.

    The catalogue holds its row — it is a tool like everything else, with a
    state and a source — so the *gate* is what keeps it out, and the convention
    is a name rather than a column for v1's reason: what makes a tool the
    harness's is who calls it, and that belongs on the thing itself.
    """
    store = store_at(tmp_path)
    merge(
        store, "toolhub", "component", [tool("_func_tool_unload"), tool("tool_search")]
    )

    injected = [row["name"] for row in store.injectable(["toolhub"])["tools"]]
    assert injected == ["tool_search"], "the harness's own is not the model's"


def test_a_components_tools_start_loaded_and_a_servers_do_not(tmp_path) -> None:
    """Ours are few and wanted; somebody else's may be ninety and are on demand.

    `autoload: true` is the operator saying a server is wanted every turn, and
    it decides the same set the budget protects: a tool that starts loaded
    because somebody asked for it must not be evicted by the count either.
    """
    store = store_at(tmp_path, autoload={"serper"})
    merge(store, "builtins", "component", [tool("builtins__calc")])
    merge(store, "serper", "mcp", [tool("serper__search")])
    merge(store, "arxiv", "mcp", [tool("arxiv__search")])

    assert rows_in(store) == [
        ("arxiv__search", "enabled", "unloaded"),
        ("builtins__calc", "enabled", LOADED),
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
    store = store_at(tmp_path, disabled={"filesystem"})
    merge(store, "arxiv", "mcp", [tool("arxiv__search")])
    merge(store, "filesystem", "mcp", [tool("filesystem__read")])
    merge(store, "skills", "skill", [tool("skill:browser")])
    store.reset()  # the boot pass is the only writer of `disabled`
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

    A component's tools and anything marked `autoload: true` are never victims:
    the budget exists to stop somebody else's ninety tools crowding the request,
    not to take away a tool slife2 guarantees — a model that has quietly lost
    `now` and `calc` is the failure DESIGN.md §8 is built around.
    """
    store = store_at(tmp_path, threshold=4, autoload={"serper"})
    merge(store, "builtins", "component", [tool("builtins__calc")])
    merge(store, "serper", "mcp", [tool("serper__search")])
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

    taken = store.evict(["builtins", "serper", "arxiv"])

    assert taken == ["arxiv__old"], "the oldest of the three, and only it"
    loaded = {row[0]: row[2] for row in rows_in(store)}
    assert loaded["builtins__calc"] == LOADED
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

    keyword = asyncio.run(store.search("screenshot", embedder=EMBEDDER))
    assert keyword["results"][0]["name"] == "browser__open", (
        "the only row whose text holds the word"
    )

    # The stub's vocabulary is the four words it counts, and only one row's
    # document holds `测试` — so this is the semantic leg answering.
    semantic = asyncio.run(store.search("测试", embedder=EMBEDDER))
    assert semantic["results"][0]["name"] == "browser__open"
    assert semantic["results"][0]["similarity"] > 0


def test_a_row_with_nothing_to_embed_is_still_found_by_keyword(tmp_path) -> None:
    """The sentinel is a value in a column, not a document to index or embed.

    A tool that declares no parameters is not thereby unfindable — the keyword
    leg reads its name and description — and it does not get a vector made of
    the word `n/a` either.
    """
    store = store_at(tmp_path)
    merge(store, "arxiv", "mcp", [tool("arxiv__search", "Find papers by keyword.")])

    found = asyncio.run(store.search("keyword", embedder=EMBEDDER))
    assert [row["name"] for row in found["results"]] == ["arxiv__search"]
    assert found["results"][0]["schema_bytes"] == 0


def test_an_empty_query_browses_and_the_filters_narrow(tmp_path) -> None:
    """No query is not no results, and a filter is a question of its own.

    Browsing is how "what is installed" and "what is switched off" become
    answerable — a row that cannot be found by text is still a row.
    """
    store = store_at(tmp_path, disabled={"filesystem"})
    merge(store, "arxiv", "mcp", [tool("arxiv__search")])
    merge(store, "filesystem", "mcp", [tool("filesystem__read")])
    store.reset(), "the boot pass is what writes `disabled`"
    browsed = asyncio.run(store.search("", embedder=EMBEDDER))
    assert browsed["browsed"] is True
    assert [row["name"] for row in browsed["results"]] == [
        "arxiv__search",
        "filesystem__read",
    ]

    off = asyncio.run(store.search("", embedder=EMBEDDER, status=STATUS_DISABLED))
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
