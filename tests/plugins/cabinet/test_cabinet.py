"""`slife2-cabinet`: five kinds of document, one corpus, and the files beside it.

Driven over the in-memory transport with a stub embedder, so what is under test
is the cabinet itself: what a write lands on disk, what a search finds and what
it says, and the four rules the port had to keep — a day accumulates while a note
is replaced, a file's bytes are never indexed, a path from a model cannot leave
the cabinet, and a save that cannot embed stores nothing.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from slife2.libs.host.config import ToolResultSettings, load
from slife2.libs.host.paths import agent_dir, data_dir
from slife2.libs.plugin.audience import client_meta, for_the_model
from slife2.libs.store.index import table_exists
from slife2.libs.turn.tokens import estimate_text_tokens
from slife2.plugins.cabinet import store as cabinet_store
from slife2.plugins.cabinet.server import (
    build_server,
    category_for,
    decode,
    unique_path,
)
from slife2.plugins.cabinet.store import (
    DIARY,
    DUMP,
    FILE,
    NOTE,
    CabinetStore,
    index_path,
    slug,
    store_of,
    vector_text,
)
from tests.fakes import FailingEmbedder, StubEmbedder

pytestmark = pytest.mark.unit

#: The stub's vocabulary, so a semantic assertion is about the *cabinet* rather
#: than about a bag of words: text sharing no word with a query is orthogonal to
#: it, and a test that ignores that passes for the wrong reason.
WORD = "测试"
OTHER = "计算"


def cabinet() -> FastMCP:
    return build_server(load(), embedder=StubEmbedder())


async def ask(client: Client, tool: str, agent: str = "jack", **arguments: Any) -> str:
    """One tool call, on behalf of a conversation, as the model reads it."""
    result = await client.call_tool(
        tool, arguments, meta=client_meta(agent) if agent else None
    )
    return "".join(getattr(block, "text", "") for block in result.content or [])


def bag(agent: str = "jack") -> Path:
    """The agent's cabinet directory."""
    return agent_dir(agent) / "cabinet"


def store(agent: str = "jack") -> CabinetStore:
    return store_of(index_path(agent))


# --- the surface the model is given -------------------------------------------


@pytest.mark.asyncio
async def test_the_tools_are_the_models_except_the_one_only_the_harness_calls() -> None:
    """Every tool carries the mark **except `dump_save`**, and that is the rule.

    The mark is the whole of what keeps a tool out of the model's hands: the hub
    merges only marked tools into the catalogue, so an unmarked one has no row to
    be listed from, searched for, or routed to — which is why the harness reaches
    this one **directly**, as a peer, and why the model cannot keep an output
    whole itself.  There is no `list_sources` and no catalogue row: a cabinet is
    found through `cabinet_search`, over its own index.
    """
    async with Client(cabinet()) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    assert set(listed) == {
        "note_edit",
        "note_remove",
        "note_read",
        "note_list",
        "diary_write",
        "diary_read",
        "diary_list",
        "file_save",
        "file_remove",
        "file_read",
        "file_list",
        "report_save",
        "report_read",
        "report_list",
        "dump_read",
        "dump_save",
        "cabinet_search",
        "cabinet_summarize",
    }
    offered = {name: tool for name, tool in listed.items() if name != "dump_save"}
    assert all(for_the_model(tool.meta) for tool in offered.values())
    assert not for_the_model(listed["dump_save"].meta), (
        "the harness's, and nobody else's"
    )
    assert "list_sources" not in listed


@pytest.mark.asyncio
async def test_a_call_that_does_not_say_whose_it_is_gets_nowhere() -> None:
    """A shared process cannot guess the cabinet, so it refuses to.

    One cabinet per agent, and which agent is a fact only the caller has — so a
    call with no client id is answered with why rather than with somebody else's
    diary (the rule `slife2-wechat` states, for the same reason).
    """
    async with Client(cabinet()) as client:
        answer = await ask(client, "note_list", agent="")

    assert "did not say which conversation" in answer


# --- notes and the diary: replace, and accumulate -----------------------------


@pytest.mark.asyncio
async def test_a_note_is_replaced_and_its_mirror_is_rewritten() -> None:
    """A note is a document: the second write *is* the note, and the file too."""
    async with Client(cabinet()) as client:
        await ask(client, "note_edit", subject="Python", content="first", tags="py")
        await ask(client, "note_edit", subject="Python", content="second")
        read = await ask(client, "note_read", subject="Python")

    assert "second" in read
    assert "first" not in read
    # The path is the stem's to decide (`mirror_stem`), and that rule has a test
    # of its own: what this one is about is that the file was rewritten, not
    # what it is called.
    written = bag() / "notes" / f"{cabinet_store.mirror_stem('Python')}.md"
    assert written.read_text(encoding="utf-8") == "# Python\n\nsecond\n"


@pytest.mark.asyncio
async def test_a_days_diary_accumulates_because_two_entries_are_two_facts() -> None:
    """The port's first rule, and the reason only two kinds have a remove tool.

    Nothing in a later entry says which part of the earlier one it replaces, so
    appending is the only honest thing a second write of one day can do — v1's
    distinction between a document and a log, kept whole.
    """
    async with Client(cabinet()) as client:
        await ask(client, "diary_write", content="morning", date="2026-10-11")
        await ask(client, "diary_write", content="evening", date="2026-10-11")
        read = await ask(client, "diary_read", date="2026-10-11")
        refused = await ask(client, "note_remove", subject="2026-10-11")

    assert "morning" in read and "evening" in read
    assert read.index("morning") < read.index("evening"), "the day reads forwards"
    assert "[refused]" in refused or "no note" in refused
    assert (bag() / "diary" / "2026-10-11.md").read_text(encoding="utf-8").count(
        "## "
    ) == 2


@pytest.mark.asyncio
async def test_a_report_is_a_log_under_one_title() -> None:
    """Reports accumulate for the diary's reason, and keep their own period."""
    async with Client(cabinet()) as client:
        await ask(
            client,
            "report_save",
            title="weekly",
            content="week one",
            period_start="2026-10-05",
            period_end="2026-10-11",
        )
        await ask(client, "report_save", title="weekly", content="week two")
        read = await ask(client, "report_read", title="weekly")
        listed = await ask(client, "report_list")

    assert "week one" in read and "week two" in read
    assert "weekly" in listed
    assert "2026-10-05" in read


@pytest.mark.asyncio
async def test_a_bad_date_is_refused_rather_than_stored() -> None:
    """`2026-13-45` is a typo, and a diary keyed by it is a day that cannot sort."""
    async with Client(cabinet()) as client:
        answer = await ask(client, "diary_write", content="x", date="2026-13-45")

    assert "[refused]" in answer
    assert store().get(DIARY, "2026-13-45") is None


@pytest.mark.asyncio
async def test_removing_a_note_takes_its_file_row_and_vectors() -> None:
    """A remove is one operation over three stores, or it is a leak."""
    async with Client(cabinet()) as client:
        await ask(client, "note_edit", subject="gone", content=f"{WORD} here")
        assert (bag() / "notes" / "gone.md").exists()
        answer = await ask(client, "note_remove", subject="gone")
        await ask(client, "note_read", subject="gone")

    assert "deleted" in answer
    assert not (bag() / "notes" / "gone.md").exists()
    assert store().get(NOTE, "gone") is None
    with store()._connect() as connection:
        orphans = connection.execute(
            "SELECT COUNT(*) AS n FROM cabinet_vec"
            " WHERE rowid NOT IN (SELECT id FROM cabinet_chunk)"
        ).fetchone()["n"]
    assert int(orphans) == 0, "a deleted row's vectors go with it"


# --- files: bytes on disk, never in the index ---------------------------------


@pytest.mark.asyncio
async def test_a_saved_file_is_copied_never_moved() -> None:
    """What the model points at stays where it is; the cabinet keeps its own."""
    source = data_dir() / "report.txt"
    source.write_bytes(b"the contents\n")

    async with Client(cabinet()) as client:
        saved = await ask(
            client, "file_save", paths=[str(source)], summary=f"a {WORD} file"
        )
        read = await ask(client, "file_read", path="documents/report.txt")
        await ask(client, "file_remove", path="documents/report.txt")
        listed = await ask(client, "file_list")

    assert "documents/report.txt" in saved
    assert read == "the contents\n"
    assert source.read_bytes() == b"the contents\n", "the original stays"
    assert "No files" in listed


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
        listed = await ask(client, "file_list")

    assert "documents/notes.txt" in listed
    assert "documents/notes-2.txt" in listed
    assert (bag() / "documents" / "notes.txt").read_text(encoding="utf-8") == "one"
    assert (bag() / "documents" / "notes-2.txt").read_text(encoding="utf-8") == "two"


@pytest.mark.asyncio
async def test_a_file_with_no_text_is_refused_not_decoded() -> None:
    """Silently decoding a PDF used to *succeed* — a page of replacement characters.

    What a reader got was indistinguishable from a file that is genuinely full of
    gibberish, so the refusal names what the file is and where its bytes are.
    """
    binary = data_dir() / "scan.pdf"
    binary.write_bytes(b"%PDF-1.7\n\x00\x01\x02\xff\xfe binary\n")

    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(binary)])
        answer = await ask(client, "file_read", path="documents/scan.pdf")

    assert "is not text" in answer
    assert "application/pdf" in answer
    assert str(bag() / "documents" / "scan.pdf") in answer


@pytest.mark.asyncio
async def test_text_in_another_encoding_is_read_and_the_encoding_named() -> None:
    """A `.txt` from cmd is GBK, and refusing it would refuse text that exists.

    The one outcome still worth refusing is a *silent* wrong decode, so the
    encoding is announced on the first line and UTF-8 — the cabinet's own — is
    returned with no header at all.
    """
    gbk = data_dir() / "gbk.txt"
    gbk.write_bytes("中文记录\n".encode("gb18030"))
    utf8 = data_dir() / "utf8.txt"
    utf8.write_bytes(b"plain\n")

    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(gbk), str(utf8)])
        decoded = await ask(client, "file_read", path="documents/gbk.txt")
        plain = await ask(client, "file_read", path="documents/utf8.txt")

    assert decoded.startswith("[decoded as gb18030; the file is not utf-8]\n")
    assert "中文记录" in decoded
    assert plain == "plain\n"


@pytest.mark.asyncio
async def test_a_binary_sniff_beats_a_wrong_guess() -> None:
    """UTF-16's BOM is settled first, so it is not read as a binary by its NULs."""
    utf16 = data_dir() / "utf16.txt"
    utf16.write_bytes("记录\n".encode("utf-16"))

    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(utf16)])
        answer = await ask(client, "file_read", path="documents/utf16.txt")

    assert "记录" in answer
    assert "is not text" not in answer


@pytest.mark.asyncio
async def test_a_save_that_cannot_embed_stores_nothing() -> None:
    """The contract every write in this system keeps, and the bytes are the tell.

    A copy that succeeded and an index that did not is a file the cabinet has
    forgotten and cannot find again — so the copy is what is taken back.
    """
    source = data_dir() / "orphan.txt"
    source.write_text("no vector for this\n", encoding="utf-8")
    server = build_server(load(), embedder=FailingEmbedder())

    async with Client(server) as client:
        with pytest.raises(Exception, match="down"):
            await ask(client, "file_save", paths=[str(source)])

    assert not (bag() / "documents" / "orphan.txt").exists()
    assert store().get(FILE, "documents/orphan.txt") is None


# --- finding ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_two_legs_answer_different_questions() -> None:
    """Words are certain and a sentence is graded — and both are readable.

    `tool_search`'s rule, restated for a cabinet: the words go to the leg that
    asks for all of them, a sentence goes to the one that measures meaning, and
    the page is ordered words-first so the column a reader sees is the column
    that ordered it.
    """
    async with Client(cabinet()) as client:
        await ask(client, "note_edit", subject="alpha", content=f"{WORD} in the body")
        await ask(client, "note_edit", subject="beta", content="nothing to match")
        by_words = await ask(client, "cabinet_search", keywords=[WORD])
        by_meaning = await ask(
            client, "cabinet_search", sentences=[f"{WORD} in the body"]
        )

    assert "alpha" in by_words
    assert "beta" not in by_words, "an AND over the words is the words not being there"
    assert "matched your words" in by_words
    assert "alpha" in by_meaning
    assert "meaning 1.00" in by_meaning


@pytest.mark.asyncio
async def test_a_mark_is_about_what_the_line_shows() -> None:
    """**A mark the reader cannot check is not evidence.**

    `matched_in` is the one piece of a cabinet search that is certain rather
    than graded — the cosine is a measurement and the rank is an order, and this
    says "your words are *there*".  So "there" has to be the line it is printed
    on, and the scope it was decided against was not: the store tested
    `title + key + body[:400] + summary` while the line prints `key` and
    `summary` *or* `body[:120]`.  A saved file whose words are only in its title
    was therefore marked "matched your words" on a line that never prints a
    title, and a note whose words sit at character 300 was marked beside an
    excerpt that stops at 120.  Both are the same failure — a claim the reader
    cannot check — and both are answered `elsewhere in the document`, which is
    where those words really are.
    """
    source = data_dir() / "attached.txt"
    source.write_text("nothing to do with the words\n", encoding="utf-8")
    # The word at 300, so the printed excerpt (the first 120) cannot show it.
    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(source)], title="quarterly figures")
        await ask(client, "note_edit", subject="deep", content=f"{'x' * 300} {WORD}")
        by_title = await ask(client, "cabinet_search", keywords=["quarterly"])
        by_tail = await ask(client, "cabinet_search", keywords=[WORD])

    assert "elsewhere in the document" in by_title, "the title is not on the line"
    assert "elsewhere in the document" in by_tail, "nor is character 300"


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
        await ask(client, "note_edit", subject="spelling", content="A Café on the wall")
        answer = await ask(client, "cabinet_search", keywords=["cafe"])

    assert "matched your words" in answer, "the index found it, so the line has it"
    assert "elsewhere" not in answer


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
        windowed = await ask(client, "cabinet_search", kind=DIARY, since="2025-01-01")

    assert "2030-01-01" in windowed
    assert "2020-01-01" not in windowed


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
        by_day = await ask(client, "diary_list", until="2026-10-07")
        by_moment = await ask(client, "diary_list", until="2026-10-07T12:00:00")
        equal = await ask(
            client,
            "diary_list",
            since="2026-10-07T12:00:00",
            until="2026-10-07T12:00:00",
        )

    assert by_moment == by_day, "the same day named two ways is two answers"
    assert "- 2026-10-07" in by_moment, "the day the bound names is in its window"
    assert "2026-10-08" not in by_moment
    assert "- 2026-10-07" in equal, "a window whose bounds are one day is that day"


@pytest.mark.asyncio
async def test_a_search_with_neither_input_browses() -> None:
    """Both empty is a browse rather than an empty answer, and it says so."""
    async with Client(cabinet()) as client:
        await ask(client, "note_edit", subject="one", content="x")
        browsed = await ask(client, "cabinet_search")
        refused = await ask(client, "cabinet_search", keywords=["  "])

    assert "one" in browsed
    assert "newest first" in browsed
    assert "refused" in refused


@pytest.mark.asyncio
async def test_a_search_can_name_one_kind() -> None:
    """A filter narrows what is shown and never what is ranked."""
    async with Client(cabinet()) as client:
        await ask(client, "note_edit", subject="same-word", content=WORD)
        await ask(client, "diary_write", content=WORD, date="2026-10-11")
        only_files = await ask(client, "cabinet_search", keywords=[WORD], kind=FILE)
        only_notes = await ask(client, "cabinet_search", keywords=[WORD], kind=NOTE)

    assert "same-word" in only_notes
    assert "same-word" not in only_files
    assert "Nothing matched" in only_files


@pytest.mark.asyncio
async def test_a_search_answer_without_results_is_not_nothing_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A search answer that cannot be read is not a search that found nothing.**

    An answer with no `results` key used to render as the same "Nothing matched"
    a real miss produces, and that is a verdict a model acts on rather than a
    failure it retries (DESIGN.md §10.1, "a failure is not a verdict").  Only a
    *readable* empty list may say it; anything else fails the call.
    """

    async def no_results(self: CabinetStore, **_: Any) -> dict[str, Any]:
        return {"rows": []}

    monkeypatch.setattr(CabinetStore, "search", no_results)

    async with Client(cabinet()) as client:
        with pytest.raises(ToolError, match="results"):
            await ask(client, "cabinet_search", keywords=[WORD])


@pytest.mark.asyncio
async def test_a_summary_is_what_makes_a_file_findable_by_meaning() -> None:
    """A file has no text of its own, so describing it is the only way in — and
    writing a summary **re-embeds** it, because the stored vector was the text
    without it (§7.5's one asymmetry)."""
    source = data_dir() / "spec.txt"
    source.write_text("body text\n", encoding="utf-8")

    async with Client(cabinet()) as client:
        await ask(client, "file_save", paths=[str(source)], title="spec sheet")
        before = await ask(client, "cabinet_search", sentences=[OTHER])
        written = await ask(
            client,
            "cabinet_summarize",
            kind=FILE,
            key="documents/spec.txt",
            summary=f"about {OTHER}",
        )
        after = await ask(client, "cabinet_search", sentences=[OTHER])

    assert "spec sheet" not in before, "nothing describes it yet but its name"
    assert "re-embedded" in written
    assert "documents/spec.txt" in after


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
        answer = await ask(client, "file_read", path="../../slife2.yaml")

    assert "no saved file" in answer


# --- the store, on its own ----------------------------------------------------


def test_one_corpus_is_one_table(tmp_path: Path) -> None:
    """Four kinds are four values of a column, so the two legs fuse on a rowid.

    v1 had four tables and a view to union them, and had to synthesise a global
    id out of a per-kind offset for the fusion to have anything to rank. Here the
    rowid *is* the corpus id — and the FTS table is written in the same
    transaction as the row, which is why there is no trigger anywhere.
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


def test_a_file_is_what_it_is_when_it_has_nothing_else() -> None:
    """The vector text of a file is its identity, or it has no vector at all.

    Measured while porting: composing a file's vector text from its summary alone
    left every summary-less file with no vector, which `sync` then reported as a
    failed save it could never clear.
    """
    row = {
        "kind": FILE,
        "title": "spec sheet",
        "source": "C:/tmp/spec.pdf",
        "file_path": "documents/spec.pdf",
        "summary": "",
    }

    text = vector_text(row)

    assert "spec sheet" in text
    assert "spec.pdf" in text
    assert "C:/tmp/spec.pdf" in text


def test_a_category_says_what_a_file_is_only_when_it_knows() -> None:
    assert category_for("a.png") == "images"
    assert category_for("a.PDF") == "documents"
    assert category_for("Makefile") == "other"
    assert category_for("a.zzz") == "other"


def test_decoding_refuses_a_file_that_is_not_text() -> None:
    assert decode(b"plain ascii\n") == ("plain ascii\n", "")
    assert decode(b"\x00\x01\x02")[0] == ""
    assert decode(b"\x00\x01\x02")[1] == "binary"
    assert decode("记录\n".encode("utf-16"))[0].strip() == "记录"


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
        traversing = await ask(
            client, "file_save", paths=[str(source)], category="./documents"
        )
        trailing = await ask(
            client, "file_save", paths=[str(source)], category="documents/"
        )
        reserved = await ask(client, "file_save", paths=[str(source)], category="NOTES")
        nested = await ask(client, "file_save", paths=[str(source)], category="a/b")

    for answer in (traversing, trailing, reserved, nested):
        assert "not one folder name" in answer or "cannot be filed under it" in answer
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
    assert (bag() / first["mirror"]).read_text(encoding="utf-8") == "# a b\n\nfirst\n"
    assert (bag() / second["mirror"]).read_text(encoding="utf-8") == "# a/b\n\nsecond\n"

    await store.remove(NOTE, "a b")

    assert not (bag() / first["mirror"]).exists()
    assert (bag() / second["mirror"]).exists(), "the other note's file is not the one"


@pytest.mark.asyncio
async def test_a_summary_that_cannot_be_embedded_is_not_stored() -> None:
    """`annotate` embeds before it writes, which is `write`'s order.

    The summary used to be committed first and the re-embedding second, so a
    `cabinet_summarize` while the embeddings endpoint was down left the row
    holding the new summary, its keyword text rewritten around it, and its
    vector still describing the text *without* it.  Nothing could repair that:
    the document has chunks, so `cabinet_embedded` stops the vector pass
    selecting it again, and the cabinet still reported itself ready — the words
    were permanently unreachable by meaning.
    """
    store = store_of(index_path("jack"))
    source = data_dir() / "spec.txt"
    source.write_text("body text\n", encoding="utf-8")
    await store.sync(StubEmbedder())
    await store.write(
        FILE,
        "documents/spec.txt",
        body="body text",
        file_path="documents/spec.txt",
        title="spec sheet",
        embedder=StubEmbedder(),
    )
    before = store.get(FILE, "documents/spec.txt")
    assert before is not None

    with pytest.raises(RuntimeError, match="down"):
        await store.annotate(
            FILE,
            "documents/spec.txt",
            summary=f"about {OTHER}",
            embedder=FailingEmbedder(),
        )

    after = store.get(FILE, "documents/spec.txt")
    assert after is not None
    assert after["summary"] == "", "a summary that could not be embedded was stored"
    with store._connect() as connection:
        indexed = connection.execute(
            "SELECT search FROM cabinet_fts WHERE rowid = ?", (int(after["id"]),)
        ).fetchone()["search"]
    assert OTHER not in indexed, "and the keyword text moved with it"
    assert indexed == _keyword_text(before)


def _keyword_text(row: dict[str, Any]) -> str:
    """One row's keyword text, as `_index` would write it.

    The comparison the summary test needs: what the index held before the write
    that failed, composed the same way the store composes it.
    """
    return cabinet_store.search_text(row)


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
async def test_two_writes_of_one_day_both_survive() -> None:
    """**The append is composed inside the write, not before it.**

    A read on one connection and a write on another, with an embedding call
    between, is the shape `append_context` exists to forbid: two writers that
    both read the old day and both write `old + own` lose one of the two
    entries, and the second writer is the ordinary case — the plugin hops to a
    thread between the halves, so two conversations writing one agent's diary
    interleave.  The schedule below is that interleaving made deterministic: the
    first call's embedding is held until the second has begun its own.
    """
    store = store_of(index_path("jack"))
    seeded = StubEmbedder()
    await store.sync(seeded)
    day = "2026-10-11"
    await store.write(DIARY, day, body="seed", embedder=seeded)
    embedder = _Interleaving()

    # **Bounded, because a test that hangs reports nothing.**  The interleaving
    # below is what the stub waits for, and a write that never reaches its
    # embedding would leave the first caller waiting on an event the second one
    # never sets — with the suite stopped rather than failed.  The bound is this
    # file's (`test_sync_ends_with_a_document_that_has_nothing_to_embed`): long
    # enough that only a deadlock reaches it, and named as the value the test is
    # built around rather than as a budget.
    await asyncio.wait_for(
        asyncio.gather(
            store.write(DIARY, day, body="morning", embedder=embedder),
            store.write(DIARY, day, body="evening", embedder=embedder),
        ),
        timeout=5.0,  # timeouts: the value this test is built around, not a budget
    )

    row = store.get(DIARY, day)
    assert row is not None
    for entry in ("seed", "morning", "evening"):
        assert entry in row["body"], f"{entry!r} was lost: {row['body']!r}"
    mirror = (bag() / row["mirror"]).read_text(encoding="utf-8")
    assert mirror.count("## ") == 3, "the whole day, in the file too"
    assert "morning" in embedder.calls[-1][0] and "evening" in embedder.calls[-1][0], (
        "the vector is of the body the row ended up with"
    )


class _Interleaving(StubEmbedder):
    """One embed whose first caller waits for the second to start.

    The gap between `write`'s read and its write is where a lost append lives,
    and it is one `await` wide — so a stub that holds the first caller there
    while the second runs past it is what makes the race a test rather than a
    hope.
    """

    def __init__(self) -> None:
        super().__init__()
        self.started = 0
        self.both = asyncio.Event()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.started += 1
        if self.started == 1:
            await self.both.wait()
        else:
            self.both.set()
        return await super().embed(texts)


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
    monkeypatch.setattr(cabinet_store, "MAX_SQL_VARS", 2)
    store = store_of(index_path("jack"))
    embedder = StubEmbedder()
    await store.sync(embedder)
    for index in range(5):
        await store.write(NOTE, f"n{index}", body=WORD, embedder=embedder)

    answer = await store.search(keywords=[WORD], embedder=embedder)

    assert len(answer["results"]) == 2, "the shortlist, not the corpus"


# --- the dump, and the one reader that has to stay inside the budget ----------
#
#  A dump is the kind no tool of this plugin writes: the harness saves a tool
#  output that was too large for the conversation, keeps the whole of it, and
#  writes a marked selection into the conversation instead.  `dump_read` is how
#  a model gets the part the selection did not carry — and the property these
#  tests are about is that its answers cannot out-run the selection, because a
#  reader that could would be feeding the thing that shortens results.


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
    await store.write(DUMP, call, body=body, embedder=embedder)


@pytest.mark.asyncio
async def test_a_dump_read_never_returns_more_than_the_budget() -> None:
    """**The property the arrangement rests on**, and not a nicety.

    A reader of ours handing back more than `tool_result.tokens` would be one
    system at both ends of a loop: the harness shortens a result, the model
    reads it back, and the shortening fires again on our own tool's answer.  So
    the bound is on the whole answer — header included, which is why the budget
    is checked here rather than the lines alone — and it holds at whatever
    number the config states.
    """
    await save_dump("call_big", dump_body())
    tight = replace(load(), tool_result=ToolResultSettings(tokens=120))

    async with Client(build_server(tight, embedder=StubEmbedder())) as client:
        answer = await ask(client, "dump_read", call="call_big")

    assert estimate_text_tokens(answer) <= 120
    assert "line 1 of a tool output" in answer
    assert "the next window starts at offset" in answer


@pytest.mark.asyncio
async def test_a_window_says_where_the_next_one_starts_and_it_is_there() -> None:
    """A page is only a page if the address it names resolves to the next one."""
    await save_dump("call_page", dump_body())

    async with Client(cabinet()) as client:
        first = await ask(client, "dump_read", call="call_page")
        next_window = re.search(r"offset (\d+)", first)
        assert next_window is not None, first
        following = int(next_window.group(1))
        second = await ask(client, "dump_read", call="call_page", offset=following)

    stopped = re.search(r"lines \d+-(\d+) of", first)
    assert stopped is not None, first
    assert int(stopped.group(1)) + 1 == following, (
        "the next window starts where this one stopped"
    )
    assert f"line {following} of a tool output" in second
    assert "line 1 of a tool output" not in second


@pytest.mark.asyncio
async def test_a_pattern_finds_lines_and_counts_them_across_the_whole_output() -> None:
    """A literal, case-insensitive match, and a count that is not just the page.

    The count is what tells a model whether to narrow the pattern or read on,
    so it is of the output rather than of what fitted — "640 of 4000" is a
    different instruction from "all 30".
    """
    await save_dump("call_grep", dump_body(3000, every=100))

    async with Client(cabinet()) as client:
        all_of_them = await ask(client, "dump_read", call="call_grep", pattern="marker")
        some = await ask(client, "dump_read", call="call_grep", pattern="line")
        none = await ask(client, "dump_read", call="call_grep", pattern="nothing here")

    assert "all 30 lines containing 'marker', from line 1" in all_of_them
    assert "line 100 of a tool output MARKER" in all_of_them
    assert re.search(
        r"\(\d+ of 3000 lines containing 'line'; the next start is offset \d+\)", some
    ), "a page of matches says how many there are"
    assert "no line from 1 contains 'nothing here'" in none


@pytest.mark.asyncio
async def test_a_dump_that_is_not_there_is_said_rather_than_guessed() -> None:
    async with Client(cabinet()) as client:
        answer = await ask(client, "dump_read", call="call_never")

    assert "no saved output" in answer


@pytest.mark.asyncio
async def test_a_line_too_large_for_the_window_names_the_file_instead() -> None:
    """The one output a window cannot show, and the way out of it is a path.

    Nothing is truncated to make it fit: the answer says the file, which is
    where the bytes are, and the operator's own reader can page it.
    """
    await save_dump("call_one", "x" * 60000)

    async with Client(cabinet()) as client:
        answer = await ask(client, "dump_read", call="call_one")

    assert "does not fit" in answer
    assert "dumps" in answer, "the header names where the bytes are"


@pytest.mark.asyncio
async def test_a_search_that_names_no_kind_does_not_reach_the_dumps() -> None:
    """A dump is reached by the marker that names it, not by a search.

    It is the one kind nobody wrote, and a body full of a tool's output matches
    on every word it happens to contain — so a search that named no kind would
    rank machine output against a person's own notes, which is the dilution the
    one-corpus rule warns about.  Naming the kind still reaches it, and that is
    how it is browsed and listed.
    """
    await save_dump("call_word", f"{WORD} is in this output\nand a second line")

    async with Client(cabinet()) as client:
        anywhere = await ask(client, "cabinet_search", keywords=[WORD])
        named = await ask(client, "cabinet_search", keywords=[WORD], kind=DUMP)

    assert "call_word" not in anywhere
    assert "call_word" in named


def test_an_index_created_before_a_kind_is_rebuilt_with_its_rows(caplog) -> None:
    """**The bill for adding a kind, paid once.**

    `kind` carries a `CHECK` listing the kinds and `CREATE TABLE IF NOT EXISTS`
    leaves an existing table exactly as it was — so an index written when there
    were four kinds refuses a dump, deep inside a transaction, in SQLite's own
    words about a constraint.  Opening it rebuilds the table: the rows are the
    truth, the columns have not changed, and the ids come with them because the
    keyword index and the vectors are keyed by id.
    """
    path = index_path("jack")
    path.parent.mkdir(parents=True, exist_ok=True)
    # The schema as it was before this kind existed: the `CHECK` is the only
    # part of it that changed, which is the whole reason a rebuild is needed.
    old = cabinet_store._SCHEMA.replace(",'dump'", "")
    with sqlite3.connect(path) as connection:
        connection.executescript(old)
        connection.execute(
            "INSERT INTO cabinet_doc (kind, key, body, created_at, updated_at) "
            "VALUES ('note', 'kept', 'a note that predates the dump kind', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        assert connection.execute("SELECT COUNT(*) FROM cabinet_doc").fetchone()[0] == 1

    caplog.set_level(logging.WARNING)
    opened = store_of(path)

    with opened._connect() as connection:
        rows = connection.execute("SELECT id, kind, key FROM cabinet_doc").fetchall()
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'cabinet_doc'"
        ).fetchone()[0]
    assert [tuple(row) for row in rows] == [(1, "note", "kept")], "its id came too"
    assert "'dump'" in ddl, "and the table can hold one now"
    assert "rebuilt to hold it" in caplog.text


@pytest.mark.asyncio
async def test_a_dump_can_be_written_after_that_rebuild() -> None:
    """The rebuild is not just a table: the kind it was rebuilt for has to work."""
    path = index_path("jack")
    path.parent.mkdir(parents=True, exist_ok=True)
    old = cabinet_store._SCHEMA.replace(",'dump'", "")
    with sqlite3.connect(path) as connection:
        connection.executescript(old)

    await save_dump("call_after", "one line\nand another")

    async with Client(cabinet()) as client:
        answer = await ask(client, "dump_read", call="call_after")

    assert "one line" in answer
