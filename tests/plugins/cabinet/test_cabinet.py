"""`slife2-cabinet`: six kinds of row, a corpus of four, and the files beside it.

Driven over the in-memory transport with a stub embedder, so what is under test
is the cabinet itself: what a write lands on disk, what a search finds and what
it answers, and the four rules the cabinet keeps — a key is created once and
changed with an update, a file's bytes are never indexed, a path from a model
cannot leave the cabinet, and a write that cannot embed stores nothing.  A dump
and an image are stored here and addressed by the turn's markers; they are not
in the corpus, and the tests that used to search them now assert that they
cannot.

**Every tool here answers JSON**, which is what a model reads: a `payload` is one
call's parsed answer, an `ask` is the same call's raw text — the two the budget
test needs, since what a model is handed is the text and what it reads is the
fields in it.  A miss and a refusal both raise `ToolError`, so a test that used
to read a sentence reads it off the raised error instead.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from slife2.libs.host.config import ToolResultSettings, load
from slife2.libs.host.paths import DATA_ENV_VAR, agent_dir, data_dir, subagent_file
from slife2.libs.plugin.audience import client_meta, for_the_model
from slife2.libs.store import index as store_index
from slife2.libs.store.index import connect_to, table_exists
from slife2.libs.turn.tokens import estimate_text_tokens
from slife2.plugins.cabinet import server as cabinet_server
from slife2.plugins.cabinet import store as cabinet_store
from slife2.plugins.cabinet.server import (
    _binary_text,
    _head_encoding,
    build_server,
    category_for,
    unique_path,
)
from slife2.plugins.cabinet.store import (
    DIARY,
    DUMP,
    FILE,
    IMAGE,
    NOTE,
    REPORT,
    UPSERT,
    CabinetStore,
    index_path,
    slug,
    store_of,
    vector_text,
)
from tests.fakes import FailingEmbedder, StubEmbedder, home_config

pytestmark = pytest.mark.unit

#: The stub's vocabulary, so a semantic assertion is about the *cabinet* rather
#: than about a bag of words: text sharing no word with a query is orthogonal to
#: it, and a test that ignores that passes for the wrong reason.
WORD = "测试"
OTHER = "计算"


def cabinet() -> FastMCP:
    return build_server(load(), embedder=StubEmbedder())


async def ask(
    client: Client,
    tool: str,
    agent: str = "jack",
    subagent: str = "",
    **arguments: Any,
) -> str:
    """One tool call's answer as **text** — what a model is handed, byte for byte."""
    result = await client.call_tool(
        tool, arguments, meta=client_meta(agent, subagent) if agent else None
    )
    return "".join(getattr(block, "text", "") for block in result.content or [])


async def payload(
    client: Client,
    tool: str,
    agent: str = "jack",
    subagent: str = "",
    **arguments: Any,
) -> Any:
    """One tool call's answer as the JSON a model reads."""
    return json.loads(
        await ask(client, tool, agent=agent, subagent=subagent, **arguments)
    )


def keys(rows: list[dict[str, Any]]) -> list[str]:
    """The `key` of each row of a page or a result set, in the order it came back."""
    return [str(row["key"]) for row in rows]


def bag(agent: str = "jack") -> Path:
    """The agent's cabinet directory."""
    return agent_dir(agent) / "cabinet"


def store(agent: str = "jack") -> CabinetStore:
    return store_of(index_path(agent))


@pytest.mark.asyncio
async def test_a_subagents_cabinet_follows_its_fork_flag(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-fork subagent owns its cabinet; a fork uses its maker's.

    The cabinet is private work, so it follows the conversation.  A subagent
    that is not a fork is a persistent agent of the maker's and gets its own
    directory under `subagents/`; a fork is a temporary copy of its maker's
    conversation, and what it writes lands in the maker's cabinet beside it.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    # A home of its own needs its configuration (SDD §4.1), exactly as a real
    # one does: a provider, and nothing else.
    home_config(tmp_path)
    researcher = agent_dir("jack", "researcher")
    record = subagent_file("jack", "researcher")

    async with Client(cabinet()) as client:
        record.write_text("fork: false\n", encoding="utf-8")
        await payload(
            client,
            "note_write",
            subagent="researcher",
            subject="own",
            content="the subagent's note",
        )
        own_mirror = Path("notes") / f"{cabinet_store.mirror_stem('own')}.md"
        assert (researcher / "cabinet" / own_mirror).is_file()
        assert not (agent_dir("jack") / "cabinet" / own_mirror).exists()

        record.write_text("fork: true\n", encoding="utf-8")
        await payload(
            client,
            "note_write",
            subagent="researcher",
            subject="shared",
            content="the maker's note",
        )
        shared_mirror = Path("notes") / f"{cabinet_store.mirror_stem('shared')}.md"
        assert (agent_dir("jack") / "cabinet" / shared_mirror).is_file()
        assert not (researcher / "cabinet" / shared_mirror).exists()


# --- the surface the model is given -------------------------------------------


@pytest.mark.asyncio
async def test_the_tools_are_the_models_except_the_ones_the_harness_calls() -> None:
    """Every tool carries the mark **except the harness's three**, and that is the rule.

    The mark is the whole of what keeps a tool out of the model's hands: the hub
    merges only marked tools into the catalogue, so an unmarked one has no row to
    be listed from, searched for, or routed to — which is why the harness reaches
    them **directly**, as peers.  `dump_save` keeps a tool output whole;
    `image_save` keeps a picture a turn carried; `image_source` resolves the
    `[Image:…]` key in that turn's row back to something `attach_image` can take.
    None of the three is a model's move.  There is no `list_sources` and no
    catalogue row: a cabinet is found through `cabinet_search`, over its own
    index.
    """
    async with Client(cabinet()) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    harness = {"dump_save", "image_save", "image_source"}
    assert set(listed) == {
        "note_write",
        "note_read",
        "note_update",
        "note_remove",
        "note_list",
        "diary_write",
        "diary_read",
        "diary_update",
        "diary_remove",
        "diary_list",
        "file_save",
        "file_read",
        "file_grep",
        "file_remove",
        "file_list",
        "report_write",
        "report_read",
        "report_update",
        "report_remove",
        "report_list",
        "dump_read",
        "dump_grep",
        "dump_save",
        "image_save",
        "image_source",
        "cabinet_search",
        "cabinet_summarize",
    }
    offered = {name: tool for name, tool in listed.items() if name not in harness}
    assert all(for_the_model(tool.meta) for tool in offered.values())
    assert not any(for_the_model(listed[name].meta) for name in harness), (
        "the harness's, and nobody else's"
    )
    assert "list_sources" not in listed


@pytest.mark.asyncio
async def test_a_call_that_does_not_say_whose_it_is_gets_nowhere() -> None:
    """A shared process cannot guess the cabinet, so it refuses to.

    One cabinet per agent, and which agent is a fact only the caller has — so a
    call with no client id is **refused**, rather than answered with somebody
    else's diary or with a sentence a model reads as a result (the rule
    every other plugin of this kind states, for the same reason).
    """
    async with Client(cabinet()) as client:
        with pytest.raises(ToolError, match="did not say which conversation"):
            await ask(client, "note_list", agent="")


# --- notes: create, replace, delete -------------------------------------------


@pytest.mark.asyncio
async def test_a_note_is_created_then_replaced_by_an_update() -> None:
    """One verb creates, one replaces, and the key is refused between them.

    A note is a document: `note_write` creates it and refuses a subject that
    already has one, so a second write can never overwrite what it did not
    read.  `note_update` is the replacement, and the markdown is rewritten
    whole — the file is not appended to.
    """
    async with Client(cabinet()) as client:
        wrote = await payload(
            client, "note_write", subject="Python", content="first", tags="py"
        )
        with pytest.raises(
            ToolError,
            match="already a note.*note_read.*note_update",
        ):
            await ask(client, "note_write", subject="Python", content="second")
        replaced = await payload(
            client, "note_update", subject="Python", content="second", tags=""
        )
        read = await payload(client, "note_read", subject="Python")
        with pytest.raises(ToolError, match="no note about"):
            await ask(client, "note_update", subject="missing", content="x")

    assert [wrote[key] for key in ("kind", "key", "title", "tags", "summary")] == [
        NOTE,
        "Python",
        "Python",
        "py",
        "",
    ]
    assert wrote["time"], "the write answers the row it stored, stamp and all"
    assert "mirror" not in wrote, "the file's path is the cabinet's, not the model's"
    assert replaced["tags"] == "", "a replacement is the document, not a patch"
    # The path is the stem's to decide (`mirror_stem`), and that rule has a test
    # of its own: what this one is about is that the file was rewritten, not
    # what it is called.
    written = bag() / "notes" / f"{cabinet_store.mirror_stem('Python')}.md"
    assert written.read_text(encoding="utf-8") == "second"
    assert read["text"] == "second"
    assert "first" not in json.dumps(read), "the older body is gone"


def test_a_listing_orders_moments_and_windows_by_instant(tmp_path: Path) -> None:
    """Two notes in one repeated DST hour do not sort as offset strings.

    `2026-10-25T01:45:00+01:00` is 00:45Z and `2026-10-25T01:15:00+00:00` is
    01:15Z: the second is newer, but `01:45` sorts after `01:15` as text.  The
    same comparison decides whether a bound includes a row.
    """
    store = CabinetStore(tmp_path / "index.db")

    def add(key: str, stamp: str) -> None:
        with store._connect() as connection:
            connection.execute(
                "INSERT INTO cabinet_doc (kind, key, title, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?)",
                (NOTE, key, key, stamp, stamp),
            )

    add("older", "2026-10-25T01:45:00+01:00")
    add("newer", "2026-10-25T01:15:00+00:00")

    listed = store.listing(NOTE)
    assert [entry["key"] for entry in listed["entries"]] == ["newer", "older"]

    # 02:00+01:00 is 01:00Z: after `older` (00:45Z), before `newer` (01:15Z).
    windowed = store.listing(NOTE, since="2026-10-25T02:00:00+01:00")
    assert [entry["key"] for entry in windowed["entries"]] == ["newer"]


def test_a_listing_counts_and_pages_one_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write between the two statements used to split the page from `total`.

    The count and the page were separate autocommit reads.  A row committed
    between them appeared on the page but not in `total`, so a caller paging by
    `offset + len(entries) < total` either repeated or skipped a row.
    """
    store = CabinetStore(tmp_path / "index.db")
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO cabinet_doc (kind, key, title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                NOTE,
                "one",
                "one",
                "2026-10-25T01:00:00+00:00",
                "2026-10-25T01:00:00+00:00",
            ),
        )
    real_connect = store._connect

    class Interleaving:
        def __init__(self) -> None:
            self.connection = real_connect()
            self.injected = False

        def execute(self, sql: str, params=()):
            found = self.connection.execute(sql, params)
            if sql.startswith("SELECT COUNT") and not self.injected:
                self.injected = True
                other = connect_to(store.path)
                other.execute(
                    "INSERT INTO cabinet_doc (kind, key, title, created_at,"
                    " updated_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        NOTE,
                        "two",
                        "two",
                        "2026-10-25T02:00:00+00:00",
                        "2026-10-25T02:00:00+00:00",
                    ),
                )
                other.commit()
                other.close()
            return found

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *exc):
            return self.connection.__exit__(*exc)

    monkeypatch.setattr(store, "_connect", Interleaving)
    listed = store.listing(NOTE)

    assert listed["total"] == 1
    assert [entry["key"] for entry in listed["entries"]] == ["one"]


def test_two_mirror_writes_do_not_share_a_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overlapping writes of one key once raced on `<name>.tmp`.

    Both wrote the same temporary path and both moved it; the second `replace`
    found nothing and raised *after* the row was committed, so a save that had
    stored its note reported a failure.  The pause makes both writers reach the
    move together, which is the window that used to fail.
    """
    real_replace = cabinet_store.os.replace

    def replace(src: Path, dst: Path) -> None:
        time.sleep(0.01)
        real_replace(src, dst)

    monkeypatch.setattr(cabinet_store.os, "replace", replace)
    errors: list[BaseException] = []

    def write(text: str) -> None:
        try:
            cabinet_store._write_mirror(tmp_path, "notes/x.md", text)
        except BaseException as exc:  # noqa: BLE001 - the assertion is the failure
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(f"body {i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)  # timeouts: the test's own rendezvous

    assert not errors, errors
    assert (tmp_path / "notes" / "x.md").read_text(encoding="utf-8") in (
        "body 0",
        "body 1",
    )


@pytest.mark.asyncio
async def test_one_day_is_one_document_and_an_update_replaces_it() -> None:
    """A day is a key, so a second entry for it is a replacement, not a fact.

    `diary_write` creates the day and refuses one that already exists, naming
    `read` and `update` as the way on; `diary_update` replaces the whole entry,
    markdown included; and `diary_remove` deletes the day — the same four verbs
    a note has.
    """
    async with Client(cabinet()) as client:
        await ask(client, "diary_write", content="morning", date="2026-10-11")
        with pytest.raises(
            ToolError,
            match="already a diary entry.*diary_read.*diary_update",
        ):
            await ask(client, "diary_write", content="evening", date="2026-10-11")
        await ask(
            client,
            "diary_update",
            content="morning and evening",
            date="2026-10-11",
        )
        read = await payload(client, "diary_read", date="2026-10-11")
        mirror = (bag() / "diary" / "2026-10-11.md").read_text(encoding="utf-8")
        removed = await payload(client, "diary_remove", date="2026-10-11")
        with pytest.raises(ToolError, match="no diary entry"):
            await ask(client, "diary_read", date="2026-10-11")

    assert read["text"] == "morning and evening", "the update is the whole entry"
    assert read["key"] == read["time"] == "2026-10-11", "a day is its own axis"
    assert mirror == "morning and evening"
    assert removed == {"kind": DIARY, "key": "2026-10-11", "removed": True}
    assert not (bag() / "diary" / "2026-10-11.md").exists()


@pytest.mark.asyncio
async def test_a_report_is_one_document_under_one_title() -> None:
    """A title is a key; an update replaces the report and keeps its arrival.

    The period is part of the document, so a replacement carries the one it
    wants — and an omitted one is cleared, because a replacement is not a
    patch.  The listing's time is still when the report first arrived.
    """
    async with Client(cabinet()) as client:
        wrote = await payload(
            client,
            "report_write",
            title="weekly",
            content="week one",
            period_start="2026-10-05",
            period_end="2026-10-11",
        )
        with pytest.raises(
            ToolError,
            match="already a report.*report_read.*report_update",
        ):
            await ask(client, "report_write", title="weekly", content="week two")
        replaced = await payload(
            client,
            "report_update",
            title="weekly",
            content="week one and two",
            period_start="2026-10-05",
            period_end="2026-10-18",
        )
        read = await payload(client, "report_read", title="weekly")
        listed = await payload(client, "report_list")
        cleared = await payload(
            client, "report_update", title="weekly", content="week one and two"
        )
        removed = await payload(client, "report_remove", title="weekly")
        with pytest.raises(ToolError, match="no report called"):
            await ask(client, "report_read", title="weekly")

    assert wrote["time"] == replaced["time"], "a replacement does not move a report"
    assert read["text"] == "week one and two"
    assert keys(listed["entries"]) == ["weekly"]
    assert read["period_start"] == "2026-10-05"
    assert read["period_end"] == "2026-10-18"
    assert cleared["period_start"] == "" and cleared["period_end"] == "", (
        "an omitted period is cleared, not kept"
    )
    assert removed == {"kind": REPORT, "key": "weekly", "removed": True}


@pytest.mark.asyncio
async def test_a_bad_date_is_refused_rather_than_stored() -> None:
    """`2026-13-45` is a typo, and a diary keyed by it is a day that cannot sort."""
    async with Client(cabinet()) as client:
        with pytest.raises(ToolError, match="is not a date"):
            await ask(client, "diary_write", content="x", date="2026-13-45")

    assert store().get(DIARY, "2026-13-45") is None


@pytest.mark.asyncio
async def test_removing_a_note_takes_its_file_row_and_vectors() -> None:
    """A remove is one operation over three stores, or it is a leak."""
    async with Client(cabinet()) as client:
        await ask(client, "note_write", subject="gone", content=f"{WORD} here")
        assert (bag() / "notes" / "gone.md").exists()
        answer = await payload(client, "note_remove", subject="gone")
        with pytest.raises(ToolError, match="no note about"):
            await ask(client, "note_read", subject="gone")

    assert answer == {"kind": NOTE, "key": "gone", "removed": True}
    assert not (bag() / "notes" / "gone.md").exists()
    assert store().get(NOTE, "gone") is None
    with store()._connect() as connection:
        orphans = connection.execute(
            "SELECT COUNT(*) AS n FROM cabinet_vec"
            " WHERE rowid NOT IN (SELECT id FROM cabinet_chunk)"
        ).fetchone()["n"]
    assert int(orphans) == 0, "a deleted row's vectors go with it"


@pytest.mark.asyncio
async def test_a_listing_is_a_page_a_caller_can_walk() -> None:
    """**The envelope is `turn_list`'s, so `offset` is the one arithmetic a model
    needs**: `total` is counted before the page is cut, which is what makes
    "there are three and here are two" answerable rather than guessed from a
    full page.  Each entry is a row to *choose* from and not the document
    itself, which is what keeps a page of fifty inside the budget the harness
    spends on a tool result.
    """
    async with Client(cabinet()) as client:
        for index in range(3):
            await ask(
                client, "note_write", subject=f"n{index}", content=f"body {index}"
            )
        first = await payload(client, "note_list", limit=2)
        second = await payload(client, "note_list", limit=2, offset=2)

    assert (first["total"], first["limit"], first["offset"]) == (3, 2, 0)
    assert len(first["entries"]) == 2
    assert first["offset"] + len(first["entries"]) < first["total"], "there is more"
    assert (second["offset"], second["total"]) == (2, 3)
    assert second["offset"] + len(second["entries"]) == second["total"], "the end"
    assert keys(second["entries"]) == ["n0"], "and it is where the first page stopped"
    assert set(first["entries"][0]) == {
        "kind",
        "key",
        "title",
        "time",
        "tags",
        "summary",
        "excerpt",
    }, "the row itself, and not its body"
    assert first["entries"][0]["excerpt"] == "body 2"


# --- files: bytes on disk, never in the index ---------------------------------


@pytest.mark.asyncio
async def test_a_saved_file_is_copied_never_moved() -> None:
    """What the model points at stays where it is; the cabinet keeps its own."""
    source = data_dir() / "report.txt"
    source.write_bytes(b"the contents\n")

    async with Client(cabinet()) as client:
        saved = await payload(
            client, "file_save", paths=[str(source)], summary=f"a {WORD} file"
        )
        read = await payload(
            client, "file_read", path="documents/report.txt", mode="char"
        )
        removed = await payload(client, "file_remove", path="documents/report.txt")
        listed = await payload(client, "file_list")

    assert keys(saved["saved"]) == ["documents/report.txt"]
    assert saved["saved"][0]["size"] == len(b"the contents\n")
    assert read["text"] == "the contents\n"
    assert read["chars"] == len("the contents\n")
    assert source.read_bytes() == b"the contents\n", "the original stays"
    assert removed == {"kind": FILE, "key": "documents/report.txt", "removed": True}
    assert listed["total"] == 0 and listed["entries"] == []


@pytest.mark.asyncio
async def test_a_relative_local_path_is_the_models_working_directory() -> None:
    """**A relative path is the data directory's, not this daemon's.**

    The agent's tools run in the data directory, so a path one of them printed
    is relative to it.  The cabinet's own process runs in `runtime/`, so
    resolving a relative path here named a different file — or, as here, no
    file at all.
    """
    source = data_dir() / "relative.txt"
    source.write_bytes(b"found\n")

    async with Client(cabinet()) as client:
        saved = await payload(client, "file_save", paths=["relative.txt"])
        read = await payload(
            client, "file_read", path="documents/relative.txt", mode="char"
        )

    assert saved["saved"][0]["source"] == str(source), "the resolved source is kept"
    assert read["text"] == "found\n"


@pytest.mark.asyncio
async def test_two_files_of_one_name_are_two_files() -> None:
    """A cabinet that keeps one of them is a cabinet that lost the other."""
    first = data_dir() / "one" / "notes.txt"
    second = data_dir() / "two" / "notes.txt"
    for path in (first, second):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.parent.name, encoding="utf-8")

    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(first)])
        await ask(client, "file_save", paths=[str(second)])
        listed = await payload(client, "file_list")

    assert set(keys(listed["entries"])) == {
        "documents/notes.txt",
        "documents/notes-2.txt",
    }
    assert listed["total"] == 2
    assert (bag() / "documents" / "notes.txt").read_text(encoding="utf-8") == "one"
    assert (bag() / "documents" / "notes-2.txt").read_text(encoding="utf-8") == "two"


@pytest.mark.asyncio
async def test_a_url_is_fetched_and_its_text_read_back(monkeypatch) -> None:
    """A URL is the second source `file_save` takes, and the bytes are stored.

    The site's filename and media type are what the row records, the URL is the
    row's `source`, and `file_read` reads the fetched text like any other saved
    file — which is the point of fetching it into the cabinet rather than
    leaving it at the URL.
    """

    async def fetch(url: str) -> tuple[bytes, str]:
        assert url == "https://example.test/reports/weekly.txt"
        return b"the fetched text\n", "text/plain"

    monkeypatch.setattr(cabinet_server, "_fetch_url", fetch)

    async with Client(cabinet()) as client:
        saved = await payload(
            client, "file_save", paths=["https://example.test/reports/weekly.txt"]
        )
        read = await payload(
            client, "file_read", path="documents/weekly.txt", mode="char"
        )

    row = saved["saved"][0]
    assert row["key"] == "documents/weekly.txt"
    assert row["source"] == "https://example.test/reports/weekly.txt"
    assert row["mime"] == "text/plain"
    assert row["size"] == len(b"the fetched text\n")
    assert read["text"] == "the fetched text\n"


def test_a_fetched_name_is_the_sites_name_and_never_a_path() -> None:
    """The name is the URL's last segment, made into one filename."""
    assert cabinet_server._download_name("https://e.test/a/report.pdf", "") == (
        "report.pdf"
    )
    assert cabinet_server._download_name("https://e.test/a/report", "text/plain") == (
        "report.txt"
    )
    assert cabinet_server._download_name("https://e.test/", "text/plain") == (
        "download.txt"
    )
    assert cabinet_server._download_name("https://e.test/a/../../etc/passwd", "") == (
        "passwd"
    )
    assert cabinet_server._download_name("https://e.test/a/a%2Fb.txt", "") == "a-b.txt"
    assert cabinet_server._download_name("https://e.test/a/CON", "") == "_CON"


def test_a_url_that_points_at_this_machine_is_refused() -> None:
    """**A fetched URL reaches the internet, not this machine.**

    A literal address is the address itself, so it is checked before a request
    is made; `localhost` is the same destination written as a name.  The
    schemes that are not `http(s)` and a URL carrying credentials are refused
    for their own reasons.
    """
    for url in (
        "http://127.0.0.1/x",
        "http://[::1]/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/x",
        "http://localhost/x",
        "http://localhost./x",
        "http://2130706433/x",
        "http://0x7f000001/x",
        "http://user:pass@example.test/x",
        "file:///etc/passwd",
        "ftp://example.test/x",
    ):
        with pytest.raises(ToolError):
            cabinet_server._checked_url(url)

    assert cabinet_server._checked_url("https://example.test/a.txt").hostname == (
        "example.test"
    )


@pytest.mark.asyncio
async def test_a_public_redirect_is_followed_and_its_body_returned() -> None:
    """A relative redirect stays on the same public site, and the fetch follows
    it; the media type is the one the site answered with."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/start":
            return httpx2.Response(302, headers={"location": "/final"})
        return httpx2.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            content=b"hello",
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        found = await cabinet_server._fetch_url(
            "https://example.test/start", client=client
        )

    assert found == (b"hello", "text/plain")


@pytest.mark.asyncio
async def test_a_redirect_to_a_private_address_is_refused() -> None:
    """A public URL answering `302 http://127.0.0.1/` is the same request."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": "http://127.0.0.1/secret"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(ToolError, match="this machine|private network"):
            await cabinet_server._fetch_url("https://example.test/start", client=client)


@pytest.mark.asyncio
async def test_a_fetch_is_stopped_at_the_size_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that streams for ever is stopped, not followed."""
    monkeypatch.setattr(cabinet_server, "MAX_FETCH_BYTES", 10)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"x" * 11)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(ToolError, match="larger than"):
            await cabinet_server._fetch_url("https://example.test/big", client=client)


@pytest.mark.asyncio
async def test_a_source_that_is_neither_a_path_nor_an_http_url_is_refused() -> None:
    async with Client(cabinet()) as client:
        with pytest.raises(ToolError, match="not an http"):
            await ask(client, "file_save", paths=["ftp://example.test/x"])
        with pytest.raises(ToolError, match="no such file"):
            await ask(client, "file_save", paths=["/no/such/file"])


# --- images: bytes, addressed by content ------------------------------------


@pytest.mark.asyncio
async def test_an_image_is_stored_once_and_read_back_as_a_data_url() -> None:
    """Two attaches of one picture are one row; `image_source` answers a URL."""
    async with Client(cabinet()) as client:
        first = await payload(client, "image_save", url="data:image/png;base64,AAAA")
        again = await payload(client, "image_save", url="data:image/png;base64,AAAA")
        read = await payload(client, "image_source", key=first["key"])
        with pytest.raises(ToolError, match="kind must be one of"):
            await ask(client, "cabinet_search", kind=IMAGE)
        browsed = await payload(client, "cabinet_search")

    assert first["key"] == again["key"], "content-addressed, so one file and row"
    assert first["key"].startswith("pictures/")
    assert first["mime"] == "image/png"
    assert first["size"] == 3
    assert read["url"].startswith("data:image/png;base64,")
    assert first["key"] not in keys(browsed["results"]), (
        "an image is addressed by its turn, not ranked"
    )


@pytest.mark.asyncio
async def test_an_image_url_is_kept_without_being_fetched() -> None:
    """The URL is the source; nothing here makes a request nobody asked for."""
    url = "https://example.test/shot.png"
    async with Client(cabinet()) as client:
        saved = await payload(client, "image_save", url=url)
        read = await payload(client, "image_source", key=saved["key"])

    assert saved["source"] == url
    assert saved["size"] == 0, "no bytes here: the provider fetches it"
    assert read["url"] == url


@pytest.mark.asyncio
async def test_removing_an_image_takes_its_bytes_with_it() -> None:
    """The kind is bytes-backed, so a removal is a row *and* a file."""
    async with Client(cabinet()) as client:
        saved = await payload(client, "image_save", url="data:image/png;base64,AAAA")

    path = index_path("jack").parent / saved["key"]
    assert path.is_file()

    store = store_of(index_path("jack"))
    assert await store.remove(IMAGE, saved["key"]) is not None
    assert not path.is_file()


@pytest.mark.asyncio
async def test_a_file_with_no_text_is_read_as_an_escaped_rendering() -> None:
    """**A binary file is not refused; it is shown, bounded, for what it is.**

    A PDF's header stays readable and its bytes become `\\xNN` escapes, so the
    model sees `%PDF` and stops.  Refusing here would deny it the same look the
    dump readers give an arbitrary tool result.
    """
    binary = data_dir() / "scan.pdf"
    binary.write_bytes(b"%PDF-1.7\n\x00\x01\x02\xff\xfe binary\n")

    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(binary)])
        read = await payload(
            client, "file_read", path="documents/scan.pdf", mode="char"
        )

    assert "%PDF-1.7" in read["text"]
    assert r"\x00\x01\x02" in read["text"], "the bytes are escapes, not NULs"
    assert read["chars"] == len(read["text"])


@pytest.mark.asyncio
async def test_a_file_past_two_megabytes_is_read_in_a_window() -> None:
    """**No read cap**: both readers stream, so a large file is a window too.

    `file_read` counts the file once without holding it, then seeks the window —
    a line page or a character slice.  The answer is the same shape as a small
    file's; only the time to reach the window grows with its offset.
    """
    source = data_dir() / "huge.txt"
    source.write_bytes(("x" * 79 + "\n").encode() * 40_000 + b"the last line\n")
    size = source.stat().st_size

    async with Client(cabinet()) as client:
        saved = await payload(client, "file_save", paths=[str(source)])
        key = saved["saved"][0]["key"]
        by_line = await payload(client, "file_read", path=key)
        by_char = await payload(client, "file_read", path=key, mode="char", limit=100)

    assert by_line["lines"] == 40_001
    assert by_line["next"] > 1
    assert by_char["text"] == "x" * 79 + "\n" + "x" * 20, "characters, newline and all"
    assert by_char["chars"] == size, "the total is counted without holding the file"
    assert by_char["next"] == 100


@pytest.mark.asyncio
async def test_a_sentence_inside_a_binary_file_is_still_findable() -> None:
    """**The reason a binary is not refused**: it may carry a sentence."""
    source = data_dir() / "blob.bin"
    source.write_bytes(b"\x00\x01\x02 the secret sentence \xff\xfe\n")

    async with Client(cabinet()) as client:
        saved = await payload(client, "file_save", paths=[str(source)])
        found = await payload(
            client,
            "file_grep",
            path=saved["saved"][0]["key"],
            pattern="secret sentence",
        )

    assert found["matched"] == 1
    assert found["matches"][0]["line"] == 1
    assert "the secret sentence" in found["matches"][0]["text"]


@pytest.mark.asyncio
async def test_text_in_another_encoding_is_read() -> None:
    """A `.txt` from cmd is GBK, and refusing it would refuse text that exists.

    `decode`'s own unit test names the encoding; this one asserts the reader
    hands back the decoded text rather than a page of replacement characters.
    """
    gbk = data_dir() / "gbk.txt"
    gbk.write_bytes("中文记录\n".encode("gb18030"))
    utf8 = data_dir() / "utf8.txt"
    utf8.write_bytes(b"plain\n")

    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(gbk), str(utf8)])
        decoded = await payload(
            client, "file_read", path="documents/gbk.txt", mode="char"
        )
        plain = await payload(
            client, "file_read", path="documents/utf8.txt", mode="char"
        )

    assert "中文记录" in decoded["text"]
    assert plain["text"] == "plain\n"


@pytest.mark.asyncio
async def test_a_binary_sniff_beats_a_wrong_guess() -> None:
    """UTF-16's BOM is settled first, so it is not read as a binary by its NULs."""
    utf16 = data_dir() / "utf16.txt"
    utf16.write_bytes("记录\n".encode("utf-16"))

    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(utf16)])
        answer = await payload(client, "file_read", path="documents/utf16.txt")

    assert "记录" in answer["text"]


@pytest.mark.asyncio
async def test_a_saved_file_is_read_in_windows_like_a_dump() -> None:
    """**The same window over a file that `dump_read` gives a dump.**

    The file on disk stays whole; the answer is bounded by `tool_result.tokens`
    and `next` names the line the following window starts at.
    """
    source = data_dir() / "long.txt"
    source.write_text(
        "\n".join(f"line {number} of a saved file" for number in range(1, 4001)),
        encoding="utf-8",
    )

    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(source)])
        first = await payload(client, "file_read", path="documents/long.txt")
        assert first["next"] > 1, first
        second = await payload(
            client, "file_read", path="documents/long.txt", offset=first["next"]
        )

    assert first["lines"] == 4000
    assert first["text"].splitlines()[0].endswith("line 1 of a saved file")
    assert f"line {first['next']} of a saved file" in second["text"]
    assert "line 1 of a saved file" not in second["text"]


@pytest.mark.asyncio
async def test_a_saved_file_is_searched_like_a_dump() -> None:
    """**The same search over a file that `dump_grep` gives a dump.**"""
    source = data_dir() / "grep.txt"
    source.write_text(
        "\n".join(
            f"line {number} of a saved file" + (" MARKER" if number % 100 == 0 else "")
            for number in range(1, 301)
        ),
        encoding="utf-8",
    )

    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(source)])
        found = await payload(
            client, "file_grep", path="documents/grep.txt", pattern="marker"
        )
        some = await payload(
            client, "file_grep", path="documents/grep.txt", pattern="line", limit=5
        )
        by_char = await payload(
            client,
            "file_read",
            path="documents/grep.txt",
            mode="char",
            offset=found["matches"][0]["char"],
            limit=6,
        )

    assert found["matched"] == 3
    assert found["matches"][0]["line"] == 100
    assert by_char["text"] == "MARKER", "the character address is the match"
    assert some["matched"] == 300
    assert [match["line"] for match in some["matches"]] == [1, 2, 3, 4, 5]
    assert some["next"] == 6


@pytest.mark.asyncio
async def test_a_vectorized_write_that_cannot_embed_stores_nothing() -> None:
    """The contract an agent-written document keeps: embed before writing.

    A note that cannot be embedded is a note the meaning leg can never find, so
    the write raises before the row or its file exists.
    """
    server = build_server(load(), embedder=FailingEmbedder())
    mirror = bag() / "notes" / f"{cabinet_store.mirror_stem('orphan')}.md"

    async with Client(server) as client:
        with pytest.raises(Exception, match="down"):
            await ask(client, "note_write", subject="orphan", content="no vector")

    assert store().get(NOTE, "orphan") is None
    assert not mirror.exists()


class ShortEmbedder(StubEmbedder):
    """An endpoint that answers with one vector fewer than it was asked for.

    **Every answer but a single-text one**, and that is what lets it be used
    twice: a one-chunk document embeds correctly, so a test can write a corpus
    with it and then watch a *later* call be refused — a two-chunk document at
    the write path, a two-sentence search at the other — instead of having the
    first write fail and never reaching the thing under test.
    """

    identity: str = "stub:short"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await super().embed(texts[:-1] if len(texts) > 1 else texts)


#: Two paragraphs past the chunk size, so this document is more than one chunk —
#: without that, a short answer *is* the whole answer and the row stays sound.
TWO_CHUNKS = "x" * 1200 + "\n\n" + "y" * 1200


@pytest.mark.asyncio
async def test_a_short_embedding_answer_is_refused_at_the_write_path() -> None:
    """**A partial answer is the hole `sync` cannot see, and this path took it.**

    `place` writes one chunk row per vector it is handed and marks the document
    tried either way, so a document embedded only in part is one `embed_missing`
    never selects again: the rest of it unreachable by meaning while the keyword
    leg still finds it, and `sync` reporting the cabinet ready.  Every other
    embedding path in the tree refuses a short answer (`index.checked_vectors`);
    the cabinet's save path was the one that did not, and it is the worst place
    to miss it — the mark is what makes the hole permanent.
    """
    server = build_server(load(), embedder=ShortEmbedder())

    async with Client(server) as client:
        with pytest.raises(Exception, match="searchable only by keyword"):
            await ask(client, "note_write", subject="partial", content=TWO_CHUNKS)

    assert store().get(NOTE, "partial") is None, "a write that raised stored nothing"
    assert not list((bag() / "notes").glob("partial*")), "and wrote no mirror"


@pytest.mark.asyncio
async def test_a_short_embedding_answer_is_refused_at_the_search_path() -> None:
    """A query vector that never arrived is a question with no answer.

    The search pairs each sentence with its own ranked list, so an unchecked
    short answer reached that pairing as a `zip` of two lists of different
    lengths — a `ValueError` naming the symptom three steps from the fact, in a
    tool a model calls.  The check is the one every other embedding path makes;
    the sentence is this corpus's.
    """
    async with Client(cabinet()) as client:
        await ask(client, "note_write", subject="alpha", content=f"a note about {WORD}")

    server = build_server(load(), embedder=ShortEmbedder())
    async with Client(server) as client:
        with pytest.raises(Exception, match="cannot say what a document is about"):
            await ask(client, "cabinet_search", sentences=[WORD, OTHER])


# --- finding ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_two_legs_answer_different_questions() -> None:
    """Words are certain and a sentence is graded — and words are measured too.

    `tool_search`'s rule, restated for a cabinet: the words go to the leg that
    asks for all of them and, when no sentence is given, to the meaning leg as
    a sentence made of them; a sentence goes to the meaning leg alone. The page
    is ordered words-first, so the column a reader sees is the column that
    ordered it.
    """
    async with Client(cabinet()) as client:
        await ask(client, "note_write", subject="alpha", content=f"{WORD} in the body")
        await ask(client, "note_write", subject="beta", content="nothing to match")
        # One row, because the worded row is first and the routed meaning leg
        # also returns the unrelated note behind it at similarity 0.
        by_words = await payload(client, "cabinet_search", keywords=[WORD], limit=1)
        by_meaning = await payload(
            client, "cabinet_search", sentences=[f"{WORD} in the body"]
        )

    assert keys(by_words["results"]) == ["alpha"]
    assert by_words["results"][0]["matched_in"] == "line"
    assert by_words["results"][0]["similarity"] == 1.0, (
        "the words are a sentence when they are all there is"
    )
    best = by_meaning["results"][0]
    assert best["key"] == "alpha" and best["similarity"] == 1.0
    assert "matched_in" not in best


@pytest.mark.asyncio
async def test_words_alone_reach_the_meaning_leg() -> None:
    """**The words are a sentence when they are all there is.**

    The keyword leg asks for every word at once, so a note holding only one of
    two words is found by neither word *list*; the meaning leg reads the joined
    words and finds it. `ToolStore.search` measured this routing at 23/36 to
    34-36, and the cabinet was the corpus where the words never reached it.
    """
    async with Client(cabinet()) as client:
        await ask(
            client, "note_write", subject="partial", content=f"a note about {WORD}"
        )
        answer = await payload(client, "cabinet_search", keywords=[WORD, OTHER])

    assert keys(answer["results"]) == ["partial"]
    assert answer["results"][0]["similarity"] is not None
    assert "matched_in" not in answer["results"][0], "the keyword leg found nothing"


@pytest.mark.asyncio
async def test_a_mark_is_about_what_the_row_shows() -> None:
    """**A mark the reader cannot check is not evidence.**

    `matched_in` is the one piece of a cabinet search that is certain rather
    than graded — the cosine is a measurement and the rank is an order, and this
    says "your words are *there*".  So "there" has to be the row the model is
    handed, and the scope it was decided against was not: the store tested
    `title + key + body[:400] + summary` while the answer printed `key` and
    `summary` *or* `body[:120]`.  A saved file whose words are only in its title
    was therefore marked "matched your words" beside a line that never showed a
    title, and a note whose words sit at character 300 was marked beside an
    excerpt that stops at 120.  Both are the same failure — a claim the reader
    cannot check — and both are answered `elsewhere in the document`, which is
    where those words really are.

    The answer is the row itself now, so the rule is the one it always was with
    a different thing under it: the title is *on* the row and the mark says so,
    and a word at character 300 of the body is still not, because the row shows
    the body's head and no more.
    """
    source = data_dir() / "attached.txt"
    source.write_text("nothing to do with the words\n", encoding="utf-8")
    # The word at 300, so the printed excerpt (the first 120) cannot show it.
    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(source)], title="quarterly figures")
        await ask(client, "note_write", subject="deep", content=f"{'x' * 300} {WORD}")
        by_title = await payload(client, "cabinet_search", keywords=["quarterly"])
        by_tail = await payload(client, "cabinet_search", keywords=[WORD])

    assert by_title["results"][0]["title"] == "quarterly figures", "the title is shown"
    assert by_title["results"][0]["matched_in"] == "line"
    assert by_tail["results"][0]["matched_in"] == "rest", "character 300 is not shown"
    assert WORD not in by_tail["results"][0]["excerpt"]


@pytest.mark.asyncio
async def test_a_mark_is_decided_by_the_index_s_own_tokenizer() -> None:
    """**One grammar on both sides of the mark.**

    The store's half of "are the words here" was a containment test in Python
    over `textindex.normalize`d strings, and the index's half is
    `unicode61 remove_diacritics 2` — which folds `Café` to `cafe`, while
    `normalize` keeps the accent.  So a caller asking for `cafe` matched a note
    at the index, was tested against a copy of the printed text that still said
    `Café`, and was reported as having matched *elsewhere* with the word plainly
    on the line.  The printed text now goes through the index's own tokenizer,
    which is the only way the two can be the same rule.
    """
    async with Client(cabinet()) as client:
        await ask(
            client, "note_write", subject="spelling", content="A Café on the wall"
        )
        answer = await payload(client, "cabinet_search", keywords=["cafe"])

    assert answer["results"][0]["matched_in"] == "line", (
        "the index found it, so the row shows it"
    )


@pytest.mark.asyncio
async def test_the_open_pass_runs_on_another_thread() -> None:
    """**Opening a cabinet must not hold the loop while it rebuilds.**

    What `sync` can be asked to do is a whole-index rebuild — DDL, a
    delete-and-reindex of every row's keyword text, and an embed of every
    document with no vector — and it runs on the first call from an agent whose
    index is stale, which is to say while other agents are being served: on the
    loop it is the shared process answering nothing else until it finishes.
    Asserting the thread rather than the timing is deliberate: it is the
    mechanism, and it does not depend on how slow the machine is.
    """
    store = store_of(index_path("jack"))
    seen: dict[str, int] = {}
    stale = store._mark_stale
    batch = store._unembedded

    def spy_stale(*args: Any) -> bool:
        seen["stale"] = threading.get_ident()
        return stale(*args)

    def spy_batch(*args: Any) -> Any:
        seen["batch"] = threading.get_ident()
        return batch(*args)

    store._mark_stale = spy_stale  # type: ignore[method-assign]
    store._unembedded = spy_batch  # type: ignore[method-assign]
    seen["loop"] = threading.get_ident()
    await store.sync(StubEmbedder())

    assert seen["stale"] != seen["loop"], "the DDL and the vector rebuild"
    assert seen["batch"] != seen["loop"], "and the walk over the corpus"


@pytest.mark.asyncio
async def test_a_window_narrows_without_deciding() -> None:
    """A range removes rows from the page; it does not re-rank what is left."""
    async with Client(cabinet()) as client:
        await ask(client, "diary_write", content="old", date="2020-01-01")
        await ask(client, "diary_write", content="new", date="2030-01-01")
        windowed = await payload(
            client, "cabinet_search", kind=DIARY, since="2025-01-01"
        )

    assert keys(windowed["results"]) == ["2030-01-01"]


@pytest.mark.asyncio
async def test_a_day_axis_reads_a_datetime_bound_as_the_day_it_names() -> None:
    """**One day, named two ways, is one day.**

    `diary_list` promises "days at or before this", and a bound that carries a
    time names a day as surely as a date-only one does — but only the date-only
    bound was moved past its day, because that advance is written for a column
    holding a moment.  So `until=2026-10-07T12:00:00` cut `< '2026-10-07'` and
    dropped the very day it names, while `until=2026-10-07` kept it.  `since`
    truncated correctly, and that asymmetry is what made this a defect rather
    than a rule: a window whose two bounds were the same moment returned nothing
    at all.
    """
    async with Client(cabinet()) as client:
        for day in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"):
            await ask(client, "diary_write", content=f"on {day}", date=day)
        by_day = await payload(client, "diary_list", until="2026-10-07")
        by_moment = await payload(client, "diary_list", until="2026-10-07T12:00:00")
        equal = await payload(
            client,
            "diary_list",
            since="2026-10-07T12:00:00",
            until="2026-10-07T12:00:00",
        )

    assert by_moment == by_day, "the same day named two ways is two answers"
    assert "2026-10-07" in keys(by_moment["entries"]), "the day the bound names is in"
    assert "2026-10-08" not in keys(by_moment["entries"])
    assert "2026-10-07" in keys(equal["entries"]), "a window of one day is that day"


@pytest.mark.asyncio
async def test_a_search_with_neither_input_browses() -> None:
    """Both empty is a browse rather than an empty answer, and it says so.

    A keyword that holds no *term* is the other half of the same question, and
    it is refused: the index would read an empty match as "every row", which is
    the browse a caller did not ask for.
    """
    async with Client(cabinet()) as client:
        await ask(client, "note_write", subject="one", content="x")
        browsed = await payload(client, "cabinet_search")
        with pytest.raises(ToolError, match="no search term"):
            await ask(client, "cabinet_search", keywords=["  "])

    assert keys(browsed["results"]) == ["one"]
    assert browsed["browsed"] is True


@pytest.mark.asyncio
async def test_a_search_can_name_one_kind() -> None:
    """A filter narrows what is shown and never what is ranked."""
    async with Client(cabinet()) as client:
        await ask(client, "note_write", subject="same-word", content=WORD)
        await ask(client, "diary_write", content=WORD, date="2026-10-11")
        only_files = await payload(client, "cabinet_search", keywords=[WORD], kind=FILE)
        only_notes = await payload(client, "cabinet_search", keywords=[WORD], kind=NOTE)

    assert keys(only_notes["results"]) == ["same-word"]
    assert only_files["results"] == [], "a filter that matched nothing is an empty page"
    assert only_files["browsed"] is False, "and it was a search, not a browse"


@pytest.mark.asyncio
async def test_a_notes_day_and_reports_text_is_what_the_meaning_leg_ranks() -> None:
    """**The three agent-written kinds put their file's whole body in the
    vector index.**

    A note, a diary day and a report are one corpus, and the semantic leg ranks
    each by its own text — not by its title, its key or its path.  A saved file
    has no vector: it is found by its words (title, path, summary) through the
    keyword leg.
    """
    async with Client(cabinet()) as client:
        await ask(client, "note_write", subject="n", content="工具")
        await ask(client, "diary_write", date="2026-10-11", content=OTHER)
        await ask(client, "report_write", title="r", content=WORD)
        found = {
            word: await payload(client, "cabinet_search", sentences=[word], limit=1)
            for word in ("工具", OTHER, WORD)
        }

    for word, kind in (("工具", NOTE), (OTHER, DIARY), (WORD, REPORT)):
        best = found[word]["results"][0]
        assert (best["kind"], best["similarity"]) == (kind, 1.0), word


@pytest.mark.asyncio
async def test_a_search_answer_without_results_is_not_nothing_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A search answer that cannot be read is not a search that found nothing.**

    An answer with no `results` key used to render as the same "Nothing matched"
    a real miss produces, and that is a verdict a model acts on rather than a
    failure it retries (SDD Appendix C, "a failure is not a verdict").  Only a
    *readable* empty list may say it; anything else fails the call.
    """

    async def no_results(self: CabinetStore, **_: Any) -> dict[str, Any]:
        return {"rows": []}

    monkeypatch.setattr(CabinetStore, "search", no_results)

    async with Client(cabinet()) as client:
        with pytest.raises(ToolError, match="results"):
            await ask(client, "cabinet_search", keywords=[WORD])


@pytest.mark.asyncio
async def test_a_file_summary_is_keyword_text_and_never_a_vector() -> None:
    """**A saved file has no vector.**  Its summary is found by the keyword leg,
    and the meaning leg — which covers only note, diary and report — does not
    return it at all."""
    source = data_dir() / "spec.txt"
    source.write_text("body text\n", encoding="utf-8")

    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(source)], title="spec sheet")
        written = await payload(
            client,
            "cabinet_summarize",
            kind=FILE,
            key="documents/spec.txt",
            summary=f"about {OTHER}",
        )
        by_words = await payload(client, "cabinet_search", keywords=[OTHER])
        by_meaning = await payload(client, "cabinet_search", sentences=[OTHER])

    assert written["summary"] == f"about {OTHER}"
    assert [row["key"] for row in by_words["results"]] == ["documents/spec.txt"]
    assert "similarity" not in by_words["results"][0], "a file has no vector"
    assert by_meaning["results"] == [], "the meaning leg does not cover files"


@pytest.mark.asyncio
async def test_a_path_from_a_model_cannot_leave_the_cabinet() -> None:
    """**A model writes these paths**, so the boundary is the point.

    The row lookup refuses a name the cabinet does not hold before anything is
    opened, and `resolve` refuses one that escapes even if a row somehow names
    it — two answers to the same question, and this is the second.
    """
    from slife2.plugins.cabinet.store import resolve

    with pytest.raises(ValueError, match="not inside the cabinet"):
        resolve(bag(), "../slife2.yaml")

    async with Client(cabinet()) as client:
        with pytest.raises(ToolError, match="no saved file"):
            await ask(client, "file_read", path="../../slife2.yaml")


# --- the store, on its own ----------------------------------------------------


def test_one_table_holds_six_kinds_and_one_corpus(tmp_path: Path) -> None:
    """Six kinds are six values of a column; the four corpus kinds share a rowid.

    One table also means no view to union and no synthesised global id out of a
    per-kind offset for the fusion to rank: a corpus row's rowid *is* its corpus
    id — and the FTS table is written in the same transaction as the row, which
    is why there is no trigger anywhere.
    """
    store = store_of(tmp_path / ".index.db")
    with store._connect() as connection:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        triggers = connection.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'trigger'"
        ).fetchone()["n"]

    assert "cabinet_doc" in tables
    assert "cabinet_fts" in tables
    assert "cabinet_docs" not in tables, "no view: the kinds are a column"
    assert int(triggers) == 0, "the index is written with the row, not by a trigger"


def test_an_orphan_file_is_never_mistaken_for_the_source() -> None:
    """`unique_path` is what makes a saved file's key its path.

    Two documents called `report.pdf` are two documents; the second gets a
    different key rather than overwriting the first.
    """
    target = unique_path(bag() / "documents", "report.pdf")
    target.write_bytes(b"x")
    second = unique_path(bag() / "documents", "report.pdf")

    assert target.name == "report.pdf"
    assert second.name == "report-2.pdf"


def test_a_slug_keeps_what_a_person_reads_and_never_collides_silently() -> None:
    """A mirror is browsed by a person, so a Chinese subject keeps its name."""
    assert slug("会议 记录/2026") == "会议-记录-2026"
    assert slug("///").startswith("doc-"), "a name with no word in it is still a name"


def test_a_file_has_no_vector_text() -> None:
    """**A saved file is keyword-only.**  Its identity is not embedded.

    A file's size is not the agent's to control, so meaning search over files
    is deferred until a RAG pass exists; `vector_text` is the agent-written
    document's body, and a file has no body.
    """
    row = {
        "kind": FILE,
        "title": "spec sheet",
        "source": "C:/tmp/spec.pdf",
        "file_path": "documents/spec.pdf",
        "summary": "",
    }

    assert vector_text(row) == ""


def test_a_category_says_what_a_file_is_only_when_it_knows() -> None:
    assert category_for("a.png") == "images"
    assert category_for("a.PDF") == "documents"
    assert category_for("Makefile") == "other"
    assert category_for("a.zzz") == "other"


def test_the_head_decides_the_encoding_or_renders_the_bytes() -> None:
    """The sniff names an encoding; a file with no text is shown as its bytes.

    The same sniff decides how a loaded and a streamed reader see the file, so
    the two cannot disagree about what its text is.
    """
    assert _head_encoding(b"plain ascii\n") == "utf-8"
    assert _head_encoding(b"\x00\x01\x02") == "binary"
    assert _binary_text(b"\x00\x01\x02") == r"\x00\x01\x02"
    assert _head_encoding("记录\n".encode("utf-16")) == "utf-16"
    assert _head_encoding("记录\n".encode("gb18030")) == "gb18030"


@pytest.mark.asyncio
async def test_sync_embeds_every_document_and_not_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The loop `embed_missing`'s docstring promises lives in `sync`.**

    `embed_missing` is bounded per call, so a `sync` that called it once would
    leave every document past the first batch without a vector — and say so
    exactly once, at open, after which the plugin's `synced` set means nothing
    tries again for the life of the process.  A cabinet bigger than one batch
    would then answer meaning queries about part of its corpus and report itself
    ready.
    """
    monkeypatch.setattr(cabinet_store, "SYNC_BATCH", 1)
    store = store_of(index_path("jack"))
    embedder = StubEmbedder()
    await store.sync(embedder)
    for index in range(3):
        await store.write(NOTE, f"n{index}", body=f"body {index}", embedder=embedder)
    with store._connect() as connection:
        # The state a changed embedding model leaves: rows, no vectors.  The
        # rebuild clears all three tables, and the third is the one that says
        # what the pass has already tried — clearing only the first two would
        # leave every document marked done.
        connection.execute("DELETE FROM cabinet_vec")
        connection.execute("DELETE FROM cabinet_chunk")
        connection.execute("DELETE FROM cabinet_embedded")

    status = await store.sync(embedder)

    assert status["ready"] is True, status["problems"]
    with store._connect() as connection:
        chunks = connection.execute(
            "SELECT COUNT(*) AS n FROM cabinet_chunk"
        ).fetchone()["n"]
    assert int(chunks) == 3, "every document, not the first batch"


@pytest.mark.asyncio
async def test_sync_ends_with_a_document_that_has_nothing_to_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A document with nothing to embed is not one to try again.**

    It places no chunk and never will, so a pass whose question is "has it a
    chunk?" hands it back for ever — and `sync` loops over exactly that
    question, so the plugin spent its whole life in `embed_missing` before it
    answered anything.  The question is "have we tried", and what answers it is
    a marker rather than a chunk.
    """
    monkeypatch.setattr(cabinet_store, "SYNC_BATCH", 1)
    store = store_of(index_path("jack"))
    embedder = StubEmbedder()
    await store.sync(embedder)
    await store.write(NOTE, "empty", body="", embedder=embedder)

    status = await asyncio.wait_for(
        store.sync(embedder),
        timeout=5.0,  # timeouts: the value this test is built around, not a budget
    )  # timeouts: the value this test is built around, not a budget

    assert status["ready"] is True, status["problems"]


@pytest.mark.asyncio
async def test_a_category_is_one_folder_name() -> None:
    """**The guard has to see what the filesystem will see.**

    `./notes`, `notes/` and (on a case-insensitive filesystem) `NOTES` all name
    the notes directory once a path is normalised, so a guard comparing the raw
    string against the reserved set let them through — and the file a model saved
    landed among the notes' own mirrors, listed by `file_list(category="notes")`
    as if it were one of them.  The question asked first is therefore "is this one
    name at all", and the reserved check is asked of the component that would
    really be created.
    """
    source = data_dir() / "report.txt"
    source.write_bytes(b"x")
    async with Client(cabinet()) as client:
        for category in ("./documents", "documents/", "NOTES", "a/b"):
            with pytest.raises(ToolError) as refused:
                await ask(client, "file_save", paths=[str(source)], category=category)
            assert "not one folder name" in str(refused.value) or (
                "cannot be filed under it" in str(refused.value)
            ), category
    assert not (bag() / "notes").exists() or not list((bag() / "notes").glob("*.txt"))


# --- the rules the store keeps when nobody is watching ------------------------


def test_a_mirror_is_a_function_of_the_key_and_not_of_its_slug() -> None:
    """**Two keys can never share one file**, which the slug alone cannot say.

    `slug` is what a person reads in a directory listing and it is many-to-one
    on purpose: `a b` and `a/b` both reduce to `a-b`.  A mirror path derived
    from it therefore collided exactly where the keys did, which is the failure
    the stored `mirror` column was documented to prevent — the second note
    replaced the first's markdown, and a remove of either one unlinked the
    other's file.
    """
    assert cabinet_store.mirror_stem("python") == "python", "the readable case stays"
    assert slug("a b") == slug("a/b") == "a-b", "what the slug is, and is not"
    assert cabinet_store.mirror_stem("a b") != cabinet_store.mirror_stem("a/b")
    assert cabinet_store.mirror_stem("a b") == cabinet_store.mirror_stem("a b")
    # A plain stem can hold nothing but word characters and dashes, so the
    # digest's `~` is what makes the two families of name disjoint rather than
    # merely unlikely to meet.
    assert "~" in cabinet_store.mirror_stem("a b")
    assert cabinet_store.mirror_stem("2026-10-11") == "2026-10-11", "a day is a day"


def test_two_keys_that_differ_only_by_case_are_two_paths() -> None:
    """**A path is not a string, and the readable stem was chosen as one.**

    `Readme` and `readme` both satisfy `slug(key) == key`, so both took the
    readable half of `mirror_stem` — and on the filesystems this runs on
    (Windows, macOS) those are one file: the second write replaced the first's
    markdown while both rows stayed correct, and `note_remove` of either one
    unlinked the other's file.  That is verbatim the failure the digest exists
    to prevent, arriving through the one door the digest did not cover, which is
    why a readable stem now requires the key to be *already* spelled the way a
    case-insensitive lookup of it resolves.

    Asserted as the invariant the fix establishes — no two distinct keys may
    fold to one path — and not as what this machine's filesystem does with them:
    its own answer is the one that cannot be trusted here, since an Ubuntu
    runner keeps both files and sees the bug as green.
    """
    for one, other in [
        ("Readme", "readme"),
        ("Python", "python"),
        ("A-B", "a-b"),
        ("Notas de reunión", "notas de reunión"),
    ]:
        first = cabinet_store.mirror_stem(one)
        second = cabinet_store.mirror_stem(other)
        assert first != second, (one, other)
        assert first.casefold() != second.casefold(), (one, other)
    assert cabinet_store.mirror_stem("readme") == "readme", (
        "the canonical spelling keeps the readable name"
    )
    assert cabinet_store.mirror_stem("会议记录") == "会议记录", (
        "and a key with no case in it at all"
    )


@pytest.mark.asyncio
async def test_two_subjects_that_slugify_alike_are_two_files() -> None:
    """The end-to-end half of the rule above, including the remove.

    `note_remove` unlinks the path the *row* holds, so with one shared file a
    remove of one subject deleted the other's markdown — a document that exists
    and cannot be read, from a call about a different one.
    """
    store = store_of(index_path("jack"))
    embedder = StubEmbedder()
    await store.sync(embedder)
    await store.write(NOTE, "a b", body="first", embedder=embedder)
    await store.write(NOTE, "a/b", body="second", embedder=embedder)
    first = store.get(NOTE, "a b")
    second = store.get(NOTE, "a/b")
    assert first is not None and second is not None

    assert first["mirror"] != second["mirror"]
    assert (bag() / first["mirror"]).read_text(encoding="utf-8") == "first"
    assert (bag() / second["mirror"]).read_text(encoding="utf-8") == "second"

    await store.remove(NOTE, "a b")

    assert not (bag() / first["mirror"]).exists()
    assert (bag() / second["mirror"]).exists(), "the other note's file is not the one"


@pytest.mark.asyncio
async def test_a_file_summary_is_keyword_text_and_needs_no_embedding() -> None:
    """**A saved file has no vector.**  A summary is written and indexed as
    words, and `annotate` does not ask the embedding model at all.

    The vectorized documents are the agent-written ones — note, diary, report —
    and their vector is the body's file, which a summary does not change.
    """
    store = store_of(index_path("jack"))
    await store.sync(StubEmbedder())
    await store.write(
        FILE,
        "documents/spec.txt",
        body="",
        file_path="documents/spec.txt",
        title="spec sheet",
        embedder=StubEmbedder(),
    )
    after = await store.annotate(FILE, "documents/spec.txt", summary="about needleword")

    assert after is not None
    assert after["summary"] == "about needleword"
    with store._connect() as connection:
        indexed = connection.execute(
            "SELECT search FROM cabinet_fts WHERE rowid = ?", (int(after["id"]),)
        ).fetchone()["search"]
    assert "needleword" in indexed, "the summary is keyword text"


@pytest.mark.asyncio
async def test_sync_reports_a_document_the_index_has_no_vector_for() -> None:
    """**`ready` is not "the pass ran"**: a hole it cannot reach is named.

    `cabinet_embedded` is what stops a document being selected again, so a
    document the pass finished with and the index does not hold is *permanent*
    — no later `sync` will embed it.  The store answered `ready: True` over
    exactly that state, and `cabinet.py` logs `problems` on startup for the one
    it is about to serve anyway.
    """
    store = store_of(index_path("jack"))
    embedder = StubEmbedder()
    await store.sync(embedder)
    await store.write(NOTE, "hole", body="something to embed", embedder=embedder)
    with store._connect() as connection:
        # The state a failed placement leaves: tried, and nothing in the index.
        connection.execute("DELETE FROM cabinet_chunk")

    status = await store.sync(embedder)

    assert status["ready"] is False
    assert status["problems"] == ["1 of 1 documents have no vector"]
    assert status["documents"] == 1


@pytest.mark.asyncio
async def test_a_failed_index_rebuild_leaves_the_vectors_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rebuild is one transaction, because SQLite does not roll DDL back.

    A `DROP TABLE` in autocommit with the rest of the rebuild inside the
    transaction leaves a `CREATE` that failed having taken `cabinet_vec` with
    it, the `DELETE`s rolled back — chunk rows naming a table that is not there
    — and the recorded identity still describing the old index, which is a state
    nothing can detect or repair.

    The rebuild is `slife2.libs.store.index.rebuild_vector_index`, shared with the turn
    store and the catalogue, so the seam the failure is injected at is that
    module's: `cabinet_store` no longer owns the statement that can fail.
    """

    def explode(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("no DDL for you")

    store = store_of(index_path("jack"))
    embedder = StubEmbedder()
    await store.sync(embedder)
    await store.write(NOTE, "one", body="some text", embedder=embedder)
    monkeypatch.setattr("slife2.libs.store.index.vector_ddl", explode)

    with pytest.raises(RuntimeError, match="no DDL for you"):
        await store.sync(StubEmbedder(identity="stub:two"))

    with store._connect() as connection:
        assert table_exists(connection, "cabinet_vec"), "the old index is still here"
        chunks = connection.execute(
            "SELECT COUNT(*) AS n FROM cabinet_chunk"
        ).fetchone()["n"]
    assert int(chunks) == 1


@pytest.mark.asyncio
async def test_two_writes_of_one_key_make_one_document() -> None:
    """**One key, one document**: the create that loses the race writes nothing.

    Both callers can pass the "does this key exist" question before either
    writes, and the database's uniqueness is what decides between them.  The
    loser answers `None` and the winner's body is the whole document — there is
    no second entry to lose, because a write is a create.
    """
    store = store_of(index_path("jack"))
    embedder = StubEmbedder()
    await store.sync(embedder)
    day = "2026-10-11"

    first, second = await asyncio.gather(
        store.write(DIARY, day, body="morning", embedder=embedder),
        store.write(DIARY, day, body="evening", embedder=embedder),
    )

    assert (first is None) != (second is None), "exactly one create won"
    row = store.get(DIARY, day)
    assert row is not None and "body" not in row, "the text is not in the row"
    mirror = (bag() / row["mirror"]).read_text(encoding="utf-8")
    assert mirror in ("morning", "evening"), "the file is the document, whole"


@pytest.mark.asyncio
async def test_a_folder_filter_matches_only_the_folder_named() -> None:
    """`LIKE` reads `_` as "any character", so the filter was not its own query.

    `file_list(category="my_notes")` also answered about `myXnotes`, and a `%`
    matched every folder there is — bound parameters, so never injection, and a
    listing that answered about a folder nobody asked for.
    """
    store = store_of(index_path("jack"))
    embedder = StubEmbedder()
    await store.sync(embedder)
    for path in ("my_notes/one.txt", "myXnotes/two.txt", "other/three.txt"):
        await store.write(FILE, path, file_path=path, embedder=embedder)

    named = store.listing(FILE, category="my_notes")

    assert [entry["key"] for entry in named["entries"]] == ["my_notes/one.txt"]
    assert store.listing(FILE, category="%")["total"] == 0
    assert store.listing(FILE, category="my_notes")["total"] == 1


@pytest.mark.asyncio
async def test_a_search_caps_the_shortlist_it_hands_to_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The union is capped where it is built, which is what `MAX_SQL_VARS` says.

    Nothing the caller passes bounds the shortlist — every sentence contributes
    its meaning leg's ids and the keyword leg's — while the query that reads the
    rows binds one value per id.  The cap is small here so that the property is
    visible on a corpus of five: what the search hands on is the top of the
    union rather than all of it, and the page is what the caller asked for.
    """
    monkeypatch.setattr(store_index, "MAX_SQL_VARS", 2)
    store = store_of(index_path("jack"))
    embedder = StubEmbedder()
    await store.sync(embedder)
    for index in range(5):
        await store.write(NOTE, f"n{index}", body=WORD, embedder=embedder)

    answer = await store.search(keywords=[WORD], embedder=embedder)

    assert len(answer["results"]) == 2, "the shortlist, not the corpus"


# --- the dump, and the two readers that have to stay inside the budget --------
#
#  A dump is the kind no model-facing tool of this plugin writes: the harness
#  calls `dump_save` directly, keeping a tool output that was too large for the
#  conversation whole, and writes a marked head into the conversation instead.
#  `dump_read` and `dump_grep` are how a model gets the part the head did not
#  carry — and the property these tests are about is that their answers cannot
#  out-run the head, because a reader that could would be feeding the thing
#  that truncates results.


def dump_body(lines: int = 4000, *, every: int = 0) -> str:
    """One tool output, as the harness would have saved it."""
    return "\n".join(
        f"line {number} of a tool output"
        + (" MARKER" if every and number % every == 0 else "")
        for number in range(1, lines + 1)
    )


async def save_dump(call: str, body: str) -> None:
    """A dump written the way the harness will write one — through the store.

    Nothing of the plugin's writes this kind (that is the point of it: the write
    is the harness's move, not a model's), so the reader is driven here against
    a row put there the way the harness's own call will put it.
    """
    store = store_of(index_path("jack"))
    embedder = StubEmbedder()
    await store.sync(embedder)
    await store.write(DUMP, call, mode=UPSERT, body=body, embedder=embedder)


@pytest.mark.asyncio
async def test_dump_save_answers_the_files_size_and_a_relative_path() -> None:
    """**The two things a marker needs, without counting the text.**

    The size is the mirror file's, one `stat`; the path is relative to the
    data directory, so the marker survives moving the home.
    """
    async with Client(cabinet()) as client:
        answer = await payload(client, "dump_save", call="call_save", output="hello")

    assert answer["path"] == "agents/jack/cabinet/dumps/call_save.md"
    assert answer["size"] == (data_dir() / answer["path"]).stat().st_size
    assert "chars" not in answer


@pytest.mark.asyncio
async def test_a_dump_read_never_returns_more_than_the_budget() -> None:
    """**The property the arrangement rests on**, and not a nicety.

    A reader of ours handing back more than `tool_result.tokens` would be one
    system at both ends of a loop: the harness truncates a result, the model
    reads it back, and the truncation fires again on our own tool's answer.  So
    the bound is on the whole answer — envelope included, which is why the
    budget is checked here rather than the lines alone.
    """
    body = dump_body()
    await save_dump("call_big", body)
    tight = replace(load(), tool_result=ToolResultSettings(tokens=120))

    async with Client(build_server(tight, embedder=StubEmbedder())) as client:
        answer = await ask(client, "dump_read", call="call_big")

    assert estimate_text_tokens(answer) <= 120
    window = json.loads(answer)
    shown = window["text"].splitlines()
    assert shown[0].endswith("line 1 of a tool output"), (
        "it starts where it was asked to"
    )
    assert window["lines"] == 4000
    assert window["chars"] == len(body)
    assert window["next"] == len(shown) + 1, "the next window starts where this stopped"


@pytest.mark.asyncio
async def test_a_window_says_where_the_next_one_starts_and_it_is_there() -> None:
    """A page is only a page if the address it names resolves to the next one."""
    await save_dump("call_page", dump_body())

    async with Client(cabinet()) as client:
        first = await payload(client, "dump_read", call="call_page")
        assert first["next"] > 1, first
        second = await payload(
            client, "dump_read", call="call_page", offset=first["next"]
        )

    last = [int(n) for n in re.findall(r"^\s*(\d+)", first["text"], flags=re.M)][-1]
    assert last + 1 == first["next"], "the next window starts where this one stopped"
    assert f"line {first['next']} of a tool output" in second["text"]
    assert "line 1 of a tool output" not in second["text"]


@pytest.mark.asyncio
async def test_a_dump_with_no_useful_lines_is_read_by_character() -> None:
    """**The mode a file that is not organized in lines is read in.**

    A minified document or one long record has one line, far larger than the
    window.  Character mode reads a contiguous slice and answers the offset the
    next one starts at.
    """
    body = "0123456789" * 6000
    await save_dump("call_one_line", body)

    async with Client(cabinet()) as client:
        first = await payload(
            client, "dump_read", call="call_one_line", mode="char", limit=100
        )
        second = await payload(
            client,
            "dump_read",
            call="call_one_line",
            mode="char",
            offset=first["next"],
            limit=100,
        )
        tail = await payload(
            client, "dump_read", call="call_one_line", mode="char", offset=-10
        )

    assert "lines" not in first, "char mode does not scan the file for a count"
    assert first["chars"] == len(body)
    assert first["text"] == body[:100], "zero is the first character"
    assert first["next"] == 100
    assert second["text"] == body[100:200]
    assert tail["text"] == body[-10:]
    assert "next" not in tail, "the last window has nowhere further to read"


@pytest.mark.asyncio
async def test_a_line_too_large_for_the_window_names_char_mode() -> None:
    """The line reader refuses the one output it cannot show, and says which
    reader can — with the character offset and the file the bytes are in."""
    await save_dump("call_one", "x" * 60000)

    async with Client(cabinet()) as client:
        with pytest.raises(ToolError) as refused:
            await ask(client, "dump_read", call="call_one")

    assert "longer than" in str(refused.value)
    assert 'mode="char"' in str(refused.value)
    assert "offset=0" in str(refused.value), "the line starts at character zero"
    assert "dumps" in str(refused.value), "and the refusal names where the bytes are"


@pytest.mark.asyncio
async def test_a_grep_match_answers_both_addresses() -> None:
    """A match names its line and its character, so either reader can follow."""
    body = "alpha\nbeta marker\ngamma"
    await save_dump("call_both", body)

    async with Client(cabinet()) as client:
        found = await payload(client, "dump_grep", call="call_both", pattern="marker")
        by_char = await payload(
            client,
            "dump_read",
            call="call_both",
            mode="char",
            offset=found["matches"][0]["char"],
            limit=6,
        )
        by_line = await payload(
            client,
            "dump_read",
            call="call_both",
            offset=found["matches"][0]["line"],
            limit=1,
        )

    assert found["matched"] == 1
    assert found["matches"][0] == {
        "line": 2,
        "char": body.index("marker"),
        "text": "beta marker",
    }
    assert by_char["text"] == "marker"
    assert by_line["text"].endswith("beta marker")


@pytest.mark.asyncio
async def test_a_pattern_finds_lines_and_counts_them_across_the_whole_output() -> None:
    """A literal, case-insensitive match, and a count that is not just the page.

    The count is what tells a model whether to narrow the pattern or read on,
    so it is of the output rather than of what fitted — "640 of 4000" is a
    different instruction from "all 30".
    """
    await save_dump("call_grep", dump_body(3000, every=100))

    async with Client(cabinet()) as client:
        all_of_them = await payload(
            client, "dump_grep", call="call_grep", pattern="marker"
        )
        some = await payload(client, "dump_grep", call="call_grep", pattern="line")
        none = await payload(
            client, "dump_grep", call="call_grep", pattern="nothing here"
        )

    assert all_of_them["matched"] == 30, "the count is of the output, not the window"
    assert all_of_them["matches"][0]["text"].endswith(
        "line 100 of a tool output MARKER"
    )
    assert "next" not in all_of_them, (
        "every match fitted, so there is nowhere to read on"
    )
    assert some["matched"] == 3000, "a page of matches says how many there are"
    assert some["next"] > 1
    assert none["matched"] == 0 and none["matches"] == [], (
        "a pattern that found nothing"
    )


@pytest.mark.asyncio
async def test_a_grep_page_names_the_line_the_next_one_starts_at() -> None:
    """A match answer pages by match, and `next` resolves to the next match."""
    await save_dump("call_page_matches", dump_body(300))

    async with Client(cabinet()) as client:
        first = await payload(
            client, "dump_grep", call="call_page_matches", pattern="line", limit=5
        )
        second = await payload(
            client,
            "dump_grep",
            call="call_page_matches",
            pattern="line",
            offset=first["next"],
            limit=5,
        )

    assert first["matched"] == 300
    assert [match["line"] for match in first["matches"]] == [1, 2, 3, 4, 5]
    assert first["next"] == 6
    assert [match["line"] for match in second["matches"]] == [6, 7, 8, 9, 10]


@pytest.mark.asyncio
async def test_a_long_matched_line_keeps_its_addresses_and_is_marked_cut() -> None:
    """A match's line can be longer than the whole window.

    The two addresses are what let the model read it in character mode, so they
    stay; the text is cut and the match says so.
    """
    body = "start " + "x" * 60000
    await save_dump("call_long_match", body)

    async with Client(cabinet()) as client:
        found = await payload(
            client, "dump_grep", call="call_long_match", pattern="start"
        )

    match = found["matches"][0]
    assert found["matched"] == 1
    assert match["line"] == 1 and match["char"] == 0
    assert match["cut"] is True
    assert 0 < len(match["text"]) < len(body)
    assert estimate_text_tokens(json.dumps(found)) <= load().tool_result.tokens


@pytest.mark.asyncio
async def test_a_huge_pattern_is_not_quoted_back() -> None:
    """Nothing the caller wrote is echoed, so its length costs nothing."""
    await save_dump("call_pattern", dump_body(100))

    async with Client(cabinet()) as client:
        answer = await ask(
            client, "dump_grep", call="call_pattern", pattern="z" * 100_000
        )

    assert json.loads(answer)["matched"] == 0
    assert estimate_text_tokens(answer) <= load().tool_result.tokens


@pytest.mark.asyncio
async def test_a_dump_that_is_not_there_is_said_rather_than_guessed() -> None:
    async with Client(cabinet()) as client:
        with pytest.raises(ToolError, match="no saved output"):
            await ask(client, "dump_read", call="call_never")
        with pytest.raises(ToolError, match="no saved output"):
            await ask(client, "dump_grep", call="call_never", pattern="anything")


@pytest.mark.asyncio
async def test_a_dump_and_a_file_are_read_and_searched_the_same_way() -> None:
    """**One implementation, two kinds.**  The body's source is the only
    difference: given the same text, a dump reader and a file reader answer the
    same window and the same matches."""
    body = dump_body(500, every=50)
    await save_dump("call_same", body)
    source = data_dir() / "same.txt"
    # Bytes, not `write_text`: the platform's newline translation would make
    # the saved file's text a different text from the dump's body.
    source.write_bytes(body.encode("utf-8"))

    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(source)])
        dump_read = await payload(client, "dump_read", call="call_same")
        file_read = await payload(client, "file_read", path="documents/same.txt")
        dump_grep = await payload(
            client, "dump_grep", call="call_same", pattern="marker"
        )
        file_grep = await payload(
            client, "file_grep", path="documents/same.txt", pattern="marker"
        )

    assert file_read == dump_read
    assert file_grep == dump_grep


@pytest.mark.asyncio
async def test_a_dump_is_addressed_by_its_marker_and_never_searched() -> None:
    """A dump is reached by the marker that names it, not by a search.

    It is the one kind nobody wrote, and a body full of a tool's output matches
    on every word it happens to contain — so ranking it against a person's own
    notes is the dilution the one-corpus rule warns about.  It is not in the
    corpus at all: a search that names no kind cannot see it, and naming its
    kind is refused rather than answered with an empty page.  The marker's call
    id is the address, and `dump_read` is the reader.
    """
    await save_dump("call_word", f"{WORD} is in this output\nand a second line")

    async with Client(cabinet()) as client:
        anywhere = await payload(client, "cabinet_search", keywords=[WORD])
        with pytest.raises(ToolError, match="kind must be one of"):
            await ask(client, "cabinet_search", keywords=[WORD], kind=DUMP)
        read = await payload(client, "dump_read", call="call_word")

    assert "call_word" not in keys(anywhere["results"])
    assert "is in this output" in read["text"], "the marker is the address"


@pytest.mark.asyncio
async def test_an_attachment_is_stored_but_not_indexed() -> None:
    """The store holds six kinds and indexes four.

    A dump and an image share the directory, the table and the write path with
    the documents — that is what makes a turn's text and bytes one cabinet —
    but their address is the marker in the turn, so neither the keyword index
    nor the vector index is written for them.  `sync`'s count is the corpus's,
    because that is the set the vector pass is supposed to cover.
    """
    store = store_of(index_path("jack"))
    embedder = StubEmbedder()
    await store.sync(embedder)
    await store.write(
        DUMP, "call", mode=UPSERT, body=f"{WORD} in a tool output", embedder=embedder
    )
    await store.write(
        IMAGE,
        "pictures/x.png",
        mode=UPSERT,
        file_path="pictures/x.png",
        mime="image/png",
        embedder=embedder,
    )
    await store.write(NOTE, "readable", body=f"a note about {WORD}", embedder=embedder)

    attachments = "SELECT id FROM cabinet_doc WHERE kind IN (?, ?)"
    kinds = (DUMP, IMAGE)
    with store._connect() as connection:
        indexed = connection.execute(
            f"SELECT COUNT(*) AS n FROM cabinet_fts WHERE rowid IN ({attachments})",
            kinds,
        ).fetchone()["n"]
        chunks = connection.execute(
            f"SELECT COUNT(*) AS n FROM cabinet_chunk WHERE doc_id IN ({attachments})",
            kinds,
        ).fetchone()["n"]
        embedded = connection.execute(
            f"SELECT COUNT(*) AS n FROM cabinet_embedded WHERE doc_id IN ({attachments})",
            kinds,
        ).fetchone()["n"]

    status = await store.sync(embedder)

    assert int(indexed) == int(chunks) == int(embedded) == 0
    assert status["ready"] is True, status["problems"]
    assert status["documents"] == 1, "the corpus, not the rows in the directory"
