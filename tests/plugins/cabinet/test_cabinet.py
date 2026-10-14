"""`slife2-cabinet`: four kinds of document, one corpus, and the files beside it.

Driven over the in-memory transport with a stub embedder, so what is under test
is the cabinet itself: what a write lands on disk, what a search finds and what
it says, and the four rules the port had to keep — a day accumulates while a note
is replaced, a file's bytes are never indexed, a path from a model cannot leave
the cabinet, and a save that cannot embed stores nothing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from slife2.libs.host.config import load
from slife2.libs.host.paths import agent_dir, data_dir
from slife2.libs.store.index import table_exists
from slife2.plugins.audience import client_meta, for_the_model
from slife2.plugins.cabinet import store as cabinet_store
from slife2.plugins.cabinet.server import (
    build_server,
    category_for,
    decode,
    unique_path,
)
from slife2.plugins.cabinet.store import (
    DIARY,
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
    assert cabinet_store.mirror_stem("Python") == "Python", "the readable case stays"
    assert slug("a b") == slug("a/b") == "a-b", "what the slug is, and is not"
    assert cabinet_store.mirror_stem("a b") != cabinet_store.mirror_stem("a/b")
    assert cabinet_store.mirror_stem("a b") == cabinet_store.mirror_stem("a b")
    # A plain stem can hold nothing but word characters and dashes, so the
    # digest's `~` is what makes the two families of name disjoint rather than
    # merely unlikely to meet.
    assert "~" in cabinet_store.mirror_stem("a b")
    assert cabinet_store.mirror_stem("2026-10-11") == "2026-10-11", "a day is a day"


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

    await asyncio.gather(
        store.write(DIARY, day, body="morning", embedder=embedder),
        store.write(DIARY, day, body="evening", embedder=embedder),
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
