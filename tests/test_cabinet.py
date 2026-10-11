"""`slife2-cabinet`: four kinds of document, one corpus, and the files beside it.

Driven over the in-memory transport with a stub embedder, so what is under test
is the cabinet itself: what a write lands on disk, what a search finds and what
it says, and the four rules the port had to keep — a day accumulates while a note
is replaced, a file's bytes are never indexed, a path from a model cannot leave
the cabinet, and a save that cannot embed stores nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from slife2.audience import client_meta, for_the_model
from slife2.cabinet import build_server, category_for, decode, unique_path
from slife2.cabinet_store import (
    DIARY,
    FILE,
    NOTE,
    CabinetStore,
    index_path,
    slug,
    store_of,
    vector_text,
)
from slife2.config import load
from slife2.paths import data_dir
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
    return data_dir() / f"{agent}.files"


def store(agent: str = "jack") -> CabinetStore:
    return store_of(index_path(agent))


# --- the surface the model is given -------------------------------------------


@pytest.mark.asyncio
async def test_the_sixteen_tools_are_the_models() -> None:
    """Every one of them carries the mark, which is what offers it to the model.

    There is no `list_sources` and no catalogue row: a cabinet is found through
    `cabinet_search`, over its own index — not by the hub, whose rows are things
    a model *calls*.
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
        "cabinet_search",
        "cabinet_summarize",
    }
    assert all(for_the_model(tool.meta) for tool in listed.values())
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
    assert (bag() / "notes" / "Python.md").read_text(encoding="utf-8") == (
        "# Python\n\nsecond\n"
    )


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
        read = await ask(client, "file_read", path="files/documents/report.txt")
        await ask(client, "file_remove", path="files/documents/report.txt")
        listed = await ask(client, "file_list")

    assert "files/documents/report.txt" in saved
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

    assert "files/documents/notes.txt" in listed
    assert "files/documents/notes-2.txt" in listed
    assert (bag() / "files" / "documents" / "notes.txt").read_text(
        encoding="utf-8"
    ) == "one"
    assert (bag() / "files" / "documents" / "notes-2.txt").read_text(
        encoding="utf-8"
    ) == "two"


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
        answer = await ask(client, "file_read", path="files/documents/scan.pdf")

    assert "is not text" in answer
    assert "application/pdf" in answer
    assert str(bag() / "files" / "documents" / "scan.pdf") in answer


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
        decoded = await ask(client, "file_read", path="files/documents/gbk.txt")
        plain = await ask(client, "file_read", path="files/documents/utf8.txt")

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
        answer = await ask(client, "file_read", path="files/documents/utf16.txt")

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

    assert not (bag() / "files" / "documents" / "orphan.txt").exists()
    assert store().get(FILE, "files/documents/orphan.txt") is None


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
async def test_a_window_narrows_without_deciding() -> None:
    """A range removes rows from the page; it does not re-rank what is left."""
    async with Client(cabinet()) as client:
        await ask(client, "diary_write", content="old", date="2020-01-01")
        await ask(client, "diary_write", content="new", date="2030-01-01")
        windowed = await ask(client, "cabinet_search", kind=DIARY, since="2025-01-01")

    assert "2030-01-01" in windowed
    assert "2020-01-01" not in windowed


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
            key="files/documents/spec.txt",
            summary=f"about {OTHER}",
        )
        after = await ask(client, "cabinet_search", sentences=[OTHER])

    assert "spec sheet" not in before, "nothing describes it yet but its name"
    assert "re-embedded" in written
    assert "files/documents/spec.txt" in after


@pytest.mark.asyncio
async def test_a_path_from_a_model_cannot_leave_the_cabinet() -> None:
    """**A model writes these paths**, so the boundary is the point.

    The row lookup refuses a name the cabinet does not hold before anything is
    opened, and `resolve` refuses one that escapes even if a row somehow names
    it — two answers to the same question, and this is the second.
    """
    from slife2.cabinet_store import resolve

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
    target = unique_path(bag() / "files" / "documents", "report.pdf")
    target.write_bytes(b"x")
    second = unique_path(bag() / "files" / "documents", "report.pdf")

    assert target.name == "report.pdf"
    assert second.name == "report-2.pdf"


def test_a_slug_keeps_what_a_person_reads_and_never_collides_silently() -> None:
    """A mirror is browsed by a person, so a Chinese subject keeps its name."""
    assert slug("会议 记录/2026") == "会议-记录-2026"
    assert slug("///").startswith("doc-"), "a name with no word in it is still a name"
    assert slug("///") == slug("///"), "and the same one twice, across processes"


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
        "file_path": "files/documents/spec.pdf",
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
