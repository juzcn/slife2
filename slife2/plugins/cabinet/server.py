"""slife2-cabinet — the private cabinet: notes, a diary, saved files, reports.

v1 called this plugin `memfiles` and described it as four typed knowledge stores
dual-written to a markdown file and a SQLite index.  This is that, built the way
a plugin here is built: one process, `house_server` plus `serve_plugin`, the
*store* as a library (`slife2.plugins.cabinet.store`), the embedder opened once against
the embeddings server, and state keyed by the client id every call carries.

**The store's calls hop off the event loop where the hop pays**: opening a
cabinet, a single read, and any listing, which are the calls that run while
another agent's turn is being served.  A *write* does its SQLite inline — it is
one transaction against a local file, it is already the shortest path through a
sequence of awaited steps, and wrapping it would be a thread hop per write for a
wait nobody is making.

**Four kinds, one corpus.**  A note, a day, a saved file and a report are rows
of one table with a `kind` column, so a search ranks them against each other
rather than against their own kind — a rank only means something inside the
corpus that produced it, and four indexes would be four corpora (§5).  What
differs per kind is what a *document* is, and that is `SPECS` in the store: its
key, its time axis, whether a second write appends, and where its markdown goes.

**A document is written twice, and one of the two is for a person.**  The row is
what a search reads; the markdown under `<data>/agents/<agent>/cabinet/` is what
somebody opens in an editor.  The store owns both writes, because they are one
operation.

**The bytes of a saved file live on disk and never in the index.**  `file_save`
copies them into the cabinet (never moving, never deleting what it copied from);
`file_read` returns a file's *text* or refuses with what it is instead, and the
path to the bytes is what every other tool takes.

What this cut does **not** do, each named rather than half-built:

* **A URL is not fetched.**  `file_save` takes local paths; v1 also downloaded
  public `http(s)` URLs behind an SSRF guard of its own (`slife/net.py`), and
  that guard is a change of its own — it is a library with a threat model, a
  config switch and its own tests, and porting it half-way would be a guard that
  looks like one.
* **There is no `grep` leg.**  v1's cabinet had three search legs and this has
  the two slife2 has; the third is deferred in DESIGN.md §9 by name, and the
  cabinet inherits the deferral rather than inventing a second regex engine.
* **There is no schedule half.**  v1's `memfiles` also owned the scheduled-task
  tables and sixteen tools over them, and reports bound to a task.  There is no
  scheduler here, so a report is keyed by its **title** instead of by a task — a
  report is still a log, and repeated saves under one title still accumulate.
* **`USER.md` is not served.**  v1's profile was a file this plugin owned *and* a
  builtin that wrote it, with the system prompt rendering it.  Wiring a file into
  a prompt is a change to the prompt, and this plugin does not make it.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import shutil
from datetime import date as _date
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from slife2.clients.embedder import EmbedderConnection
from slife2.helpers.threads import off_loop
from slife2.libs.host.config import CABINET_SERVER_NAME, Config
from slife2.libs.plugin.audience import FOR_THE_MODEL, unaddressed
from slife2.libs.plugin.protocol import agent_or_blank, house_server, serve_plugin
from slife2.libs.store import textindex
from slife2.libs.store.index import Embedder
from slife2.libs.turn.tokens import clip_to_tokens, estimate_text_tokens
from slife2.plugins.cabinet.store import (
    DIARY,
    DUMP,
    FILE,
    KINDS,
    NOTE,
    REPORT,
    SPECS,
    CabinetStore,
    discard_file,
    index_path,
    line_excerpt,
    parse_tags,
    resolve,
    store_of,
)

logger = logging.getLogger(__name__)

SERVER_NAME = CABINET_SERVER_NAME

#: This server's key in the config's `servers:` table.
CONFIG_KEY = "cabinet"

INSTRUCTIONS = (
    "The cabinet: private notes, a diary, saved files and reports, all in one "
    "searchable corpus. A note and a report are keyed by a name you choose, a "
    "diary by its date, a saved file by the path inside the cabinet. `file_save` "
    "copies a file that is already on this machine — nothing here fetches a URL."
)

#: The most of a file `file_read` will read.  A cap rather than a preference: a
#: tool that returns a gigabyte of text to a model has spent a context window to
#: say "this is not a document".  (It used to read "and the most of a URL it
#: would fetch" — nothing here fetches a URL, which the instructions above say
#: and which was v1's `file_save` half that was deliberately not ported.)
MAX_READ_BYTES = 2 * 1024 * 1024

#: How much of a file is sniffed for the two things that settle its encoding
#: before anything is decoded: a BOM, and a NUL byte.
SNIFF_BYTES = 8192

#: Where a saved file goes, by what it is.  v1 auto-filed by extension and this
#: keeps that: the cabinet is browsed by a person, and `images/` is a
#: directory they can find something in.  An extension nobody here knows lands in
#: `files/other/`, which is a name that says exactly that rather than guessing.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "images": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"),
    "documents": (
        ".pdf",
        ".doc",
        ".docx",
        ".odt",
        ".rtf",
        ".txt",
        ".md",
        ".xls",
        ".xlsx",
        ".csv",
        ".ppt",
        ".pptx",
        ".epub",
    ),
    "code": (
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".html",
        ".css",
        ".sh",
        ".ps1",
        ".sql",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".rb",
        ".php",
        ".ipynb",
    ),
    "archives": (".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar"),
    "audio": (".mp3", ".wav", ".ogg", ".flac", ".m4a"),
    "video": (".mp4", ".mkv", ".mov", ".avi", ".webm"),
}

#: Where an unknown extension goes.
OTHER = "other"

#: Names a `category` may not take, because they are the cabinet's own
#: directories.  **The flattening is what makes this necessary**: the categories
#: sit beside `notes/`, `diary/` and `reports/` at the cabinet root, so
#: `category="notes"` would put a saved file among the notes' own mirrors and
#: `file_list(category="notes")` would list both as one thing.
RESERVED_CATEGORIES = frozenset({"notes", "diary", "reports"})


def category_for(name: str) -> str:
    """Which directory a file belongs in, by its extension.

    The extension and not the MIME type: nothing here has opened the file, and
    `mimetypes` would answer from the same string with more machinery.  A file
    with no extension is `other`, which is a true answer rather than a guess.
    """
    suffix = Path(name).suffix.lower()
    for category, suffixes in CATEGORIES.items():
        if suffix in suffixes:
            return category
    return OTHER


def unique_path(directory: Path, name: str) -> Path:
    """A free path in *directory*, keeping the name where it can.  **Reserved.**

    A file is never overwritten, and never overwritten *silently*: two documents
    called `report.pdf` are two documents, and a cabinet that keeps one of them
    is a cabinet that lost the other.  v1's rule, and the reason a saved file's
    key is its path rather than its name.

    **The name is claimed by creating it**, and that is the difference between a
    rule and a rule that holds.  Asking `exists()` and returning the answer is a
    check a second caller can pass at the same time as the first — and it does:
    the copy that follows is awaited on a thread, so two `file_save` calls whose
    sources share a name both saw the same free path and the second overwrote
    the first.  `O_EXCL` is the same question asked by the kernel, which is the
    only party that can answer it once.

    The file is left empty for the caller to fill, and empty is what it stays if
    the caller's own work fails — which is why the caller removes it on the way
    out rather than assuming a failed save left nothing behind.
    """
    directory.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(name).stem, Path(name).suffix
    for index in range(1, 1000):
        candidate = directory / (name if index == 1 else f"{stem}-{index}{suffix}")
        try:
            handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(handle)
        return candidate
    raise ValueError(f"{directory} has a thousand files called {name}")


def decode(data: bytes) -> tuple[str, str]:
    """A file's bytes as text, and the header to say how.  `("", refusal)` if not.

    **"No text" is not the same question as "is it UTF-8"**, and reading it as
    one refuses text that plainly exists — a `.txt` written by cmd is GBK, and
    one redirected by PowerShell is UTF-16LE.  So the order is: a BOM settles it
    before anything else (UTF-16LE's BOM cannot be mistaken for a PDF, and UTF-8
    with a BOM is still UTF-8); then a NUL in the first block is the tell that a
    file has no text to return at all — a PDF, an image, an archive; then UTF-8
    strict; and only then the guess, which is *announced*, because the one
    outcome still worth refusing is a **silent** wrong decode.

    Returns `(text, header)` — the header empty for UTF-8, which is the cabinet's
    own encoding and the web's, and a sentence naming the encoding otherwise.
    """
    head = data[:SNIFF_BYTES]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "[decoded as utf-16]\n"
        except UnicodeDecodeError:
            pass
    if head.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), ""
    if b"\x00" in head:
        return "", "binary"
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030"), "[decoded as gb18030; the file is not utf-8]\n"
    except UnicodeDecodeError:
        return "", "binary"


def build_server(config: Config, *, embedder: Embedder | None = None) -> FastMCP:
    """Build the cabinet server.

    `embedder` is the seam, and it is injectable for the reason
    `slife2.plugins.context.server`'s is: the whole plugin — every write, every read,
    both search legs — can then be exercised over the in-memory transport with
    no embedding endpoint behind it.  In production it is opened once against
    the embeddings server, because a write places its vector in the same call
    and a store that cannot embed stores nothing.
    """
    embedding = EmbedderConnection(config, embedder=embedder)

    #: Cabinets whose indexes have been brought up to date, once per process.
    #: A cabinet opened after this pass is not re-synced on every call: its own
    #: writes embed, and the only thing a sync adds is a rebuild for a *changed*
    #: embedder, which cannot happen while this process is running.
    synced: set[Path] = set()

    async def store(agent: str) -> CabinetStore:
        """One agent's cabinet, with its indexes brought up to date."""
        opened = await off_loop(store_of, index_path(agent))
        if opened.path not in synced:
            status = await opened.sync(await embedding.get())
            if not status["ready"]:
                # Said, and served anyway: the keyword leg still works, and
                # refusing to open a cabinet because its vectors are behind is
                # a person locked out of their own notes.
                logger.warning(
                    "cabinet %s: %s", opened.path, "; ".join(status["problems"])
                )
            synced.add(opened.path)
        return opened

    mcp: FastMCP = house_server(SERVER_NAME, instructions=INSTRUCTIONS)

    # ── Whose cabinet ────────────────────────────────────────────────────

    #: Whose cabinet a call is about — or `""`, which every tool below turns into
    #: `UNADDRESSED`.  **A shared process cannot guess it**: one cabinet serves
    #: every agent, and which one is a fact only the caller has, which is why a
    #: call that says nothing is refused rather than answered with somebody
    #: else's diary.  `slife2.plugins.wechat.server` asks the same question and
    #: gets the same answer out of the same helper; both answer in a *sentence*
    #: rather than by raising, which is why neither is `protocol.agent_reader`.
    agent_of = agent_or_blank

    UNADDRESSED = unaddressed("cabinet to act on", "the cabinet")

    async def cabinet(ctx: Context) -> tuple[CabinetStore, str]:
        """`(the store, "")`, or `(nothing, refusal)` — so a tool reads as one line."""
        agent = agent_of(ctx)
        if not agent:
            return None, UNADDRESSED  # type: ignore[return-value]
        return await store(agent), ""

    # ── The three moves every kind makes ─────────────────────────────────
    #
    # Five kinds are served below — notes, diary days, files, reports, and the
    # tool outputs the harness dumps — and each is the same three moves: a page,
    # one row, a delete.  What differs between them is the kind and the sentence
    # a miss answers with, and those are arguments; what must *not* differ, and
    # did while each kind had its own copy, is everything a copy gets wrong
    # quietly: the `""` that means "no bound" on every listing, and the thread
    # hop that keeps a store call off the loop.  Eleven lines per listing and the
    # sentences a model reads are not a good place for four chances to disagree.
    #
    # Each answers `(what, refusal)` — `cabinet`'s own shape, read one step on —
    # so a tool stays three or four lines whatever kind it serves.  The answers
    # that need the *store* after the fetch rather than the row (`file_read`
    # resolves a path, `dump_read` renders a window) keep their own body and say
    # so by asking `cabinet` directly.

    async def page_of(
        ctx: Context,
        kind: str,
        *,
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
        **filters: Any,
    ) -> tuple[dict[str, Any], str]:
        """One page of one kind, or `({}, the refusal)`."""
        opened, refusal = await cabinet(ctx)
        if refusal:
            return {}, refusal
        return (
            await off_loop(
                opened.listing,
                kind,
                since=since or None,
                until=until or None,
                limit=limit,
                offset=offset,
                **filters,
            ),
            "",
        )

    async def row_of(
        ctx: Context, kind: str, key: str
    ) -> tuple[dict[str, Any] | None, str]:
        """One row of one kind — `(None, "")` when there is no such row.

        A miss is not a refusal, and the difference is the tool's to say: a note
        is missing *about* a subject, a report is missing *called* a title, and
        those are the words a model reads back.
        """
        opened, refusal = await cabinet(ctx)
        if refusal:
            return None, refusal
        return await off_loop(opened.get, kind, key), ""

    async def remove_of(ctx: Context, kind: str, key: str) -> tuple[bool, str]:
        """Delete one row of one kind — `(it went, the refusal)`."""
        opened, refusal = await cabinet(ctx)
        if refusal:
            return False, refusal
        return await opened.remove(kind, key) is not None, ""

    # ── Notes ────────────────────────────────────────────────────────────

    @mcp.tool(name="note_edit", meta=FOR_THE_MODEL)
    async def note_edit(
        ctx: Context, subject: str, content: str, tags: str = ""
    ) -> str:
        """Write a private note about a subject, replacing what it held.

        A note is a document: this is the whole of what the cabinet keeps under
        that subject, and an older body goes. It lands as markdown in the
        agent's cabinet directory too.

        Args:
            subject: What the note is about — its name, and reusing one replaces
                the note rather than making a second.
            content: The note itself.
            tags: Comma-separated words to file it under.
        """
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        row = await opened.write(
            NOTE,
            subject,
            body=content,
            tags=parse_tags(tags),
            embedder=await embedding.get(),
        )
        logger.info("note_edit subject=%s", subject)
        return f"Saved `{subject}` to {row['mirror']}."

    @mcp.tool(name="note_remove", meta=FOR_THE_MODEL)
    async def note_remove(ctx: Context, subject: str) -> str:
        """Delete a note — its markdown, its index entry and its vectors.

        Args:
            subject: The note's subject, from `note_list`.
        """
        deleted, refusal = await remove_of(ctx, NOTE, subject)
        if refusal:
            return refusal
        if not deleted:
            return (
                f"There is no note about {subject!r} — `note_list` shows what there is."
            )
        logger.info("note_remove subject=%s", subject)
        return f"`{subject}` is deleted, and its file with it."

    @mcp.tool(name="note_read", meta=FOR_THE_MODEL)
    async def note_read(ctx: Context, subject: str) -> str:
        """Read a note by its subject.

        Args:
            subject: The note's subject, from `note_list` or a search result.
        """
        row, refusal = await row_of(ctx, NOTE, subject)
        if refusal:
            return refusal
        if row is None:
            return (
                f"There is no note about {subject!r} — `note_list` shows what there is."
            )
        return _document(NOTE, row)

    @mcp.tool(name="note_list", meta=FOR_THE_MODEL)
    async def note_list(
        ctx: Context,
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """List notes, most recently written first, with their subjects.

        Args:
            since: Only notes written at or after this. Accepts `2026-10-07`,
                `last week`, `3 days ago`.
            until: Only notes written at or before this.
            limit: How many to show; a page, not a cap on the answer.
            offset: Skip this many, to page.
        """
        page, refusal = await page_of(
            ctx, NOTE, since=since, until=until, limit=limit, offset=offset
        )
        if refusal:
            return refusal
        return _listing(NOTE, "notes", page, lambda row: str(row["key"]))

    # ── The diary ────────────────────────────────────────────────────────

    @mcp.tool(name="diary_write", meta=FOR_THE_MODEL)
    async def diary_write(
        ctx: Context, content: str, date: str = "", tags: str = ""
    ) -> str:
        """Write today's diary entry, or add to the entry for a given day.

        A later write of the same day appends, so this is safe to call again:
        two entries under one date are two facts about that day.

        Args:
            content: The entry.
            date: The day it is about, `YYYY-MM-DD`. Empty means today.
            tags: Comma-separated words to file it under.
        """
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        day = date.strip() or _today()
        try:
            _day(day)
        except ValueError:
            return f"[refused] {day!r} is not a date — write it as `YYYY-MM-DD`."
        row = await opened.write(
            DIARY,
            day,
            body=content,
            tags=parse_tags(tags),
            embedder=await embedding.get(),
        )
        logger.info("diary_write date=%s", day)
        return f"Added to {row['mirror']}."

    @mcp.tool(name="diary_read", meta=FOR_THE_MODEL)
    async def diary_read(ctx: Context, date: str) -> str:
        """Read one day's diary entry.

        Args:
            date: The day, `YYYY-MM-DD`.
        """
        row, refusal = await row_of(ctx, DIARY, date)
        if refusal:
            return refusal
        if row is None:
            return f"There is no diary entry for {date}."
        return _document(DIARY, row)

    @mcp.tool(name="diary_list", meta=FOR_THE_MODEL)
    async def diary_list(
        ctx: Context,
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """List diary days, newest first.

        Args:
            since: Only days at or after this.
            until: Only days at or before this.
            limit: How many to show.
            offset: Skip this many, to page.
        """
        page, refusal = await page_of(
            ctx, DIARY, since=since, until=until, limit=limit, offset=offset
        )
        if refusal:
            return refusal
        return _listing(DIARY, "diary entries", page, lambda row: str(row["key"]))

    # ── Files ────────────────────────────────────────────────────────────

    @mcp.tool(name="file_save", meta=FOR_THE_MODEL)
    async def file_save(
        ctx: Context,
        paths: list[str],
        title: str = "",
        tags: str = "",
        summary: str = "",
        category: str = "",
    ) -> str:
        """Copy files from this machine into the cabinet.

        It copies and never moves, so what you point at stays where it is. The
        bytes are not indexed: a search matches the file's name, where it came
        from and any `summary` you write.

        Args:
            paths: Local paths to copy. Name the files in a directory rather
                than the directory itself.
            title: A name for it. Empty uses the filename.
            tags: Comma-separated words to file it under.
            summary: What the file is, in your words — the text its semantic
                search is built from.
            category: The directory inside the cabinet — `images`, `documents`
                and so on. Empty picks one from the extension.
        """
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        wanted = [Path(path) for path in paths]
        missing = [str(path) for path in wanted if not path.is_file()]
        if missing:
            return "Not saved — no such file: " + ", ".join(missing)
        saved: list[str] = []
        for source in wanted:
            chosen = category.strip() or category_for(source.name)
            # **Two questions, and the first is the one the second depends on.**
            # A category is *one directory name*: `./notes`, `notes/`, `a/b` and
            # an absolute path all name something else once a filesystem has
            # normalised them, so a guard that compared the raw string against a
            # set would let `"./notes"` through and file a saved attachment among
            # the notes' own mirrors.  The second question is then asked of the
            # component that would actually be created, and case-insensitively —
            # this platform's filesystem is, so `"NOTES"` is the notes directory.
            if not chosen or Path(chosen).name != chosen or ".." in Path(chosen).parts:
                return (
                    f"Not saved — {chosen!r} is not one folder name. A category is "
                    f"a single name like `images`, not a path."
                )
            if chosen.casefold() in RESERVED_CATEGORIES:
                return (
                    f"Not saved — {chosen!r} is where the cabinet keeps its "
                    f"{chosen.casefold()}, so a file cannot be filed under it. "
                    f"Choose another category, or leave it out to file by extension."
                )
            target = unique_path(opened.root / chosen, source.name)
            relative = target.relative_to(opened.root).as_posix()
            try:
                # The copy is inside the `try` because `unique_path` has already
                # created the destination: a copy that fails leaves an empty file
                # where a failed copy used to leave nothing at all.
                await off_loop(shutil.copy2, source, target)
                await opened.write(
                    FILE,
                    relative,
                    title=title.strip() or source.stem,
                    tags=parse_tags(tags),
                    summary=summary,
                    source=str(source),
                    file_path=relative,
                    mime=mimetypes.guess_type(source.name)[0] or "",
                    size=target.stat().st_size,
                    embedder=await embedding.get(),
                )
            except Exception:
                # **A save that raises stores nothing** — the same contract a
                # turn's save keeps.  The bytes are already on disk by now, so
                # they are what has to be taken back; a row without its file, or
                # a file the cabinet has forgotten, are both worse than a refusal.
                await off_loop(discard_file, target)
                raise
            saved.append(relative)
        logger.info("file_save count=%d", len(saved))
        return "Saved into the cabinet:\n" + "\n".join(f"- {path}" for path in saved)

    @mcp.tool(name="file_remove", meta=FOR_THE_MODEL)
    async def file_remove(ctx: Context, path: str) -> str:
        """Delete a saved file — the cabinet's copy, its row and its vectors.

        The file it was copied from stays where it is.

        Args:
            path: The file's cabinet-relative path, from `file_list`.
        """
        deleted, refusal = await remove_of(ctx, FILE, path)
        if refusal:
            return refusal
        if not deleted:
            return (
                f"There is no saved file at {path!r} — `file_list` shows what there is."
            )
        logger.info("file_remove path=%s", path)
        return f"`{path}` is deleted from the cabinet."

    @mcp.tool(name="file_read", meta=FOR_THE_MODEL)
    async def file_read(ctx: Context, path: str) -> str:
        """Read a saved file's text.

        A PDF, an image or an archive answers with what it is, how big, and the
        path the tools that take bytes take. Text in another encoding reaches
        you decoded, with the encoding named on the first line.

        Args:
            path: The file's cabinet-relative path, from `file_list`.
        """
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        row = await off_loop(opened.get, FILE, path)
        if row is None:
            return (
                f"There is no saved file at {path!r} — `file_list` shows what there is."
            )
        try:
            target = resolve(opened.root, path)
        except ValueError as exc:
            return f"[refused] {exc}"
        if not target.is_file():
            return f"[refused] {path!r} is recorded but is not on disk any more."
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            return (
                f"[refused] {path} is {size} bytes, past the {MAX_READ_BYTES}-byte "
                f"cap — read it with a tool that takes a path."
            )
        data = await off_loop(target.read_bytes)
        text, header = decode(data)
        if header == "binary":
            return (
                f"`{path}` is not text ({row['mime'] or 'unknown type'}, {size} bytes). "
                f"Its bytes are at {target}."
            )
        return f"{header}{text}"

    @mcp.tool(name="file_list", meta=FOR_THE_MODEL)
    async def file_list(
        ctx: Context,
        category: str = "",
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """List saved files, newest first.

        Args:
            category: Only files in this directory of `files/`.
            since: Only files saved at or after this.
            until: Only files saved at or before this.
            limit: How many to show.
            offset: Skip this many, to page.
        """
        page, refusal = await page_of(
            ctx,
            FILE,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            category=category.strip(),
        )
        if refusal:
            return refusal
        return _listing(
            FILE,
            "files",
            page,
            lambda row: f"{row['file_path']}  ({row['size']} bytes)",
            summaries=True,
        )

    # ── Reports ──────────────────────────────────────────────────────────

    @mcp.tool(name="report_save", meta=FOR_THE_MODEL)
    async def report_save(
        ctx: Context,
        title: str,
        content: str,
        tags: str = "",
        period_start: str = "",
        period_end: str = "",
    ) -> str:
        """Save a report under a title, or add to the one already there.

        A report is a log like a diary: a title that exists gets a new dated
        section, and the earlier one stays.

        Args:
            title: What it is a report of. This is its key.
            content: The report.
            tags: Comma-separated words to file it under.
            period_start: The first day the report covers, `YYYY-MM-DD`.
            period_end: The last day it covers.
        """
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        row = await opened.write(
            REPORT,
            title,
            body=content,
            tags=parse_tags(tags),
            period_start=period_start,
            period_end=period_end,
            embedder=await embedding.get(),
        )
        logger.info("report_save title=%s", title)
        return f"Saved into {row['mirror']}."

    @mcp.tool(name="report_read", meta=FOR_THE_MODEL)
    async def report_read(ctx: Context, title: str) -> str:
        """Read a report by its title.

        Args:
            title: The report's title, from `report_list`.
        """
        row, refusal = await row_of(ctx, REPORT, title)
        if refusal:
            return refusal
        if row is None:
            return f"There is no report called {title!r} — `report_list` shows what there is."
        return _document(REPORT, row)

    @mcp.tool(name="report_list", meta=FOR_THE_MODEL)
    async def report_list(
        ctx: Context,
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """List reports, newest first, by when they were written.

        Args:
            since: Only reports written at or after this.
            until: Only reports written at or before this.
            limit: How many to show.
            offset: Skip this many, to page.
        """
        page, refusal = await page_of(
            ctx, REPORT, since=since, until=until, limit=limit, offset=offset
        )
        if refusal:
            return refusal
        return _listing(REPORT, "reports", page, lambda row: str(row["key"]))

    # ── Reading a dump back ──────────────────────────────────────────────

    @mcp.tool(name="dump_read", meta=FOR_THE_MODEL)
    async def dump_read(
        ctx: Context,
        call: str,
        offset: int = 1,
        limit: int = 0,
        pattern: str = "",
        context: int = 0,
    ) -> str:
        """Read a tool output the harness saved, by finding — not by scrolling.

        A result too large for the conversation is shortened for the model and
        kept whole here; the marker standing in for it names the call. This is
        the way back to the part the shortening did not carry, and it is a
        *searching* reader: name what you are looking for and how much of what
        surrounds it, or name the line you already know and read from there.
        Lines are numbered, matches are marked `:`, the lines around them `-`,
        and the numbers are the same ones the marker's selection uses.

        **Every answer is within the configured budget** (`tool_result.tokens`),
        the same number the harness spends shortening a result — so reading an
        output back can never hand over more than the shortening took away, and
        this tool can never feed the thing that shortens results. Read as much
        as you like, in as many windows as it takes.

        Args:
            call: The tool call id, from the marker in the conversation.
            offset: The first line to read; `1` is the first and `-1` the last.
            limit: The most lines to show. `0` shows as many as the budget holds.
            pattern: Show the lines containing this text, matched literally and
                ignoring case.
            context: With a pattern, how many lines either side of a match to
                show with it.
        """
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        budget = config.tool_result.tokens
        row = await off_loop(opened.get, DUMP, call)
        if row is None:
            answer = (
                f"There is no saved output for the call {call!r} — the marker "
                f"beside the shortened result names the calls there are."
            )
        else:
            mirror = str(row.get("mirror") or "")
            where = str(opened.root / mirror) if mirror else ""
            mime = str(row.get("mime") or "")
            if mime and not mime.startswith("text/"):
                # **A dump that is not text is dumped and not read.**  Nothing
                # here decides that a payload is not text: the judgement is made
                # where the result arrived, and the harness writes it down as the
                # row's mime.  Sniffing for it here would be this module deciding
                # what a tool returned, which is the one thing the arrangement
                # keeps out of the harness's hands — and there are no lines of a
                # picture to find anyway.
                answer = (
                    f"The output of {call!r} is not text ({mime}), so there are "
                    f"no lines to read. Its bytes are at {where}."
                )
            else:
                answer = _window_of(
                    str(row.get("body") or ""),
                    where=where,
                    offset=offset,
                    limit=limit,
                    pattern=pattern,
                    context=context,
                    budget=budget,
                )
        # **One way out, and it is measured.**  Every path above is bounded
        # already, except the ones that quote the caller back at itself — a
        # `pattern` with a megabyte in it, a `call` that is not an id — and an
        # answer over the budget is one the harness would shorten, which is our
        # own reader feeding our own shortening.  So the bound is enforced here,
        # once, rather than argued for at each return.
        return clip_to_tokens(answer, budget)

    @mcp.tool(name="dump_save")
    async def dump_save(
        ctx: Context,
        call: str,
        output: str,
        tool: str = "",
        mime: str = "",
    ) -> dict[str, Any]:
        """Keep one tool output whole, keyed by the call it came from.

        **The harness's call and no model's.**  Nothing here decides whether an
        output is worth keeping or which part of it matters, so this carries no
        audience mark — which keeps it out of the hub's catalogue, and therefore
        out of the model's list and out of `tool_search`.  The harness reaches
        this server **directly**, as a peer, exactly as `slife2-context` reaches a
        model backend; the model's own route back in is `dump_read`, which *is*
        marked and therefore *is* in the catalogue.

        A second call under one key replaces, because the key is one call: a
        re-run writes the same output twice rather than two facts.

        Answers with a payload rather than a sentence, and **refuses by raising**,
        because its caller is the harness and not a model: an answer it cannot
        read has to be distinguishable from one that says nothing went wrong.

        Args:
            call: The tool call id the output came from — the key, and the one
                identifier the conversation and this row both carry.
            output: The whole of what the tool returned.
            tool: The tool's name, so a listing says what produced this.
            mime: What the output is, when it is known not to be text.
        """
        opened, refusal = await cabinet(ctx)
        if refusal:
            raise ToolError(refusal)
        row = await opened.write(
            DUMP,
            call,
            body=output,
            title=tool,
            source=tool,
            mime=mime,
            embedder=await embedding.get(),
        )
        logger.info("dump_save call=%s tool=%s chars=%d", call, tool, len(output))
        return {"kept": call, "mirror": str(row["mirror"]), "chars": len(output)}

    # ── Finding, and describing ──────────────────────────────────────────
    @mcp.tool(name="cabinet_search", meta=FOR_THE_MODEL)
    async def cabinet_search(
        ctx: Context,
        keywords: list[str] | None = None,
        sentences: list[str] | None = None,
        kind: str = "",
        since: str = "",
        until: str = "",
        limit: int = 20,
    ) -> str:
        """Search the cabinet — every note, day, file and report at once.

        The two legs add up rather than competing: give both for the strongest
        answer, either alone for one leg, and neither to browse what is there,
        newest first.

        Args:
            keywords: Words that must appear. A path like `files/docs/spec.pdf`
                works here.
            sentences: What you are looking for, asked normally. One per
                question you have.
            kind: `note`, `diary`, `file`, `report` or `dump` — a tool's own
                output, which the harness saves — to look in one of them. Empty
                searches all of them but the dumps, which are reached by the
                marker naming them rather than by a search.
            since: Only documents from this time on.
            until: Only documents up to this time.
            limit: How many results.
        """
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        try:
            found = await opened.search(
                keywords=keywords or (),
                sentences=sentences or (),
                kind=kind.strip(),
                since=since or None,
                until=until or None,
                limit=limit,
                embedder=await embedding.get(),
            )
        except textindex.EmptyQuery as exc:
            return f"[refused] {exc}"
        return _found_text(found)

    @mcp.tool(name="cabinet_summarize", meta=FOR_THE_MODEL)
    async def cabinet_summarize(
        ctx: Context,
        kind: str,
        key: str,
        summary: str = "",
        tags: str = "",
    ) -> str:
        """Describe a document already in the cabinet so it can be found.

        Tags work for any kind; a summary is what a file's meaning is built
        from, so writing one for a file re-embeds it — a file has no text of its
        own, and a search reaches it by its name and paths until somebody
        describes it.

        Args:
            kind: `note`, `diary`, `file`, `report` or `dump`.
            key: The document's key — a subject, a date, a cabinet path, a title
                or the tool call a dump is the output of.
            summary: What it is, in words somebody would search with.
            tags: Comma-separated words to file it under.
        """
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        if kind.strip() not in KINDS:
            return f"`kind` must be one of: {', '.join(KINDS)}."
        row = await opened.annotate(
            kind.strip(),
            key,
            summary=summary,
            tags=parse_tags(tags),
            embedder=await embedding.get(),
        )
        if row is None:
            return f"There is no {kind} with the key {key!r}."
        logger.info("cabinet_summarize kind=%s key=%s", kind, key)
        extra = (
            " It is re-embedded, so a search by meaning finds the new words."
            if kind.strip() == FILE and summary
            else ""
        )
        return f"`{key}` is annotated.{extra}"

    return mcp


def _window_of(
    body: str,
    *,
    where: str,
    offset: int,
    limit: int,
    pattern: str,
    context: int,
    budget: int,
) -> str:
    """A window of a dump, read by *finding*: never larger than `budget` tokens.

    **Finding and reading are one gesture here, because neither half answers on
    its own.**  A match without its neighbours is a line torn out of a stack
    trace, and a window without a match is a page a model reads to the end to
    learn nothing.  So `pattern` chooses the lines and `context` brings their
    neighbours with them, marked so that the two are told apart at a glance, and
    two neighbourhoods that meet are **merged** — one block, which is what a
    reader sees, rather than two that overlap and spend the window twice.

    **The bound is on the whole answer, header included**, and that is the
    property the arrangement rests on rather than a nicety: a reader that could
    hand back more than the budget would be a tool of ours feeding the harness's
    own shortening, which is one system at both ends of a loop.  So the window
    is filled line by line against the budget — measured by the one estimator,
    `slife2.libs.turn.tokens` — and then the *rendering* is measured and trimmed
    from its end until it fits, because the header and the sentence about what
    is missing are part of what a model is handed.  What did not fit is named
    rather than lost: how many lines there are, how many matched, and the offset
    the next window starts at.

    **The budget is the config's** (`tool_result.tokens`) and not a number of
    this module's, because the harness spends the same one shortening a result
    for the conversation: a reader free to differ from the shortening is the
    loop again, one config edit away.

    **The pattern is text, not an expression.**  A regex over a 240,000-line
    output is a program whose runtime nobody bounds, running in a server every
    agent shares — and the code-reading tools that take a regex take it because
    their engines cannot backtrack, which Python's can.  A literal substring,
    ignoring case, is also what the common case wants: a symbol, a path, a
    spelling remembered approximately (§9's own three examples for the grep leg
    v1 had).  A model that needs a real regex has the file — `where` names it,
    and the operator's own tools read it.

    **The numbers are the ones the harness's shortening shows a model**, on
    purpose: a line named in a discriminator's reply and a line read back here
    have to be one address, and two renderings that disagreed would be two
    addresses for one line.
    """
    lines = body.splitlines()
    total = len(lines)
    if not total:
        return "[refused] this output was saved empty, so there is nothing to read."
    # **A negative `offset` counts from the end**, which is where a log's and a
    # command's own summary is: a model asking for the tail of something it has
    # never read does not know how long it is, and every reader it has used
    # before takes `-1` for the last line.
    start = offset if offset > 0 else total + offset + 1
    if start < 1 or start > total:
        return (
            f"[refused] {offset} is not a line of this output: it has {total}, "
            f"and 1 to {total} (or -1 to -{total}) are the addresses."
        )

    wanted = pattern.casefold()
    hits = (
        [n for n in range(start, total + 1) if wanted in lines[n - 1].casefold()]
        if wanted
        else []
    )
    # **The count is a fact about the whole output, so it is taken in its own
    # pass.**  The window stops as soon as it is full, and a count that stopped
    # with it would report "385 of 502" — a number about the budget dressed up
    # as a number about the output, which is exactly what a model reads to
    # decide whether to narrow the pattern or read on.
    matched = len(hits)
    runs = _runs(hits, max(context, 0), total) if wanted else [(start, total)]

    kept: list[tuple[int, str]] = []
    full = False
    for first, last in runs:
        for number in range(first, last + 1):
            if limit and len(kept) >= limit:
                full = True
                break
            kept.append((number, lines[number - 1]))
            if _spent(kept) > budget:
                kept.pop()
                full = True
                break
        if full:
            break

    while True:
        answer = _render_window(
            kept,
            total=total,
            start=start,
            seen=matched,
            hits=set(hits),
            pattern=pattern,
            context=max(context, 0),
            where=where,
            budget=budget,
        )
        # An empty window renders as the refusal below rather than as a header,
        # so this is the one case that returns without fitting anything: there
        # is nothing left to trim.
        if not kept or estimate_text_tokens(answer) <= budget:
            return answer
        kept.pop()


def _runs(hits: list[int], context: int, total: int) -> list[tuple[int, int]]:
    """The hits with their neighbours, as disjoint inclusive runs.

    Merged rather than one run per hit, because the neighbours of two hits a
    line apart *are* one block of text: showing the same line twice spends the
    window on saying it twice, and a reader that saw one line under two
    addresses would have two addresses for one line.
    """
    runs: list[list[int]] = []
    for number in hits:
        first, last = max(number - context, 1), min(number + context, total)
        if runs and first <= runs[-1][1] + 1:
            runs[-1][1] = max(runs[-1][1], last)
        else:
            runs.append([first, last])
    return [(first, last) for first, last in runs]


def _spent(kept: list[tuple[int, str]]) -> int:
    """What the lines held so far cost, by the one estimator."""
    return estimate_text_tokens("\n".join(text for _, text in kept))


def _render_window(
    kept: list[tuple[int, str]],
    *,
    total: int,
    start: int,
    seen: int,
    hits: set[int],
    pattern: str,
    context: int,
    where: str,
    budget: int,
) -> str:
    """A window as the text a model reads: a header, numbered lines, and the count.

    **A matching line and its neighbours are told apart by one character**,
    `12: the error` against `11- the line before it`, which is the convention
    every grep has and therefore the one a model reads without being told.  With
    no pattern there is nothing to tell apart and the column is left blank.

    The sentences this can end with are the ways a window can be partial, and a
    reader that could not tell them apart would not know whether to read on,
    page, or narrow the pattern.  An **empty** window is the one case that is
    not a window at all — one line too large to show — and it says so under a
    header that names the file, because that is where the bytes are.
    """
    where = where or "the cabinet"
    head = f"[dump] {total} line{'s' if total != 1 else ''} — the whole of it is at {where}"
    # **No match is not an empty window**, and the order is the whole of the
    # difference: a pattern that found nothing returned no *lines*, and reading
    # that as "one line was too large to show" would send a model looking for a
    # file problem it does not have.
    if pattern and not seen:
        return f"{head}\n\nno line from {start} contains {pattern!r}"
    if not kept:
        return (
            f"{head}\n\n[refused] a line from {start} does not fit a "
            f"{budget}-token window on its own."
        )
    first, last = kept[0][0], kept[-1][0]
    around = f", each with the {context} lines around it" if context else ""
    if not pattern:
        note = (
            f"lines {first}-{last} of {total}; the next window starts at offset {last + 1}"
            if last < total
            else f"lines {first}-{total}; that is the end of it"
        )
    elif sum(1 for number in hits if first <= number <= last) < seen:
        note = (
            f"{sum(1 for number in hits if first <= number <= last)} of {seen} lines "
            f"containing {pattern!r}{around}; the next start is offset {last + 1}"
        )
    else:
        note = f"all {seen} lines containing {pattern!r}{around}, from line {start}"
    width = len(str(last))
    if pattern:
        shown = "\n".join(
            f"{number:>{width}}{':' if number in hits else '-'} {text}"
            for number, text in kept
        )
    else:
        shown = "\n".join(f"{number:>{width}}  {text}" for number, text in kept)
    return f"{head}\n\n{shown}\n\n({note})"


def _document(kind: str, row: dict[str, Any]) -> str:
    """One document, as the text a model reads."""
    lines = [f"[{kind}] {row['key']}"]
    if row.get("title") and row["title"] != row["key"]:
        lines[0] += f" — {row['title']}"
    for label, column in (
        ("updated", "updated_at"),
        ("saved", "created_at"),
        ("period", "period_start"),
        ("source", "source"),
        ("tags", "tags"),
    ):
        if row.get(column):
            lines.append(f"{label}: {row[column]}")
    if row.get("summary"):
        lines.append(f"summary: {row['summary']}")
    lines.append("")
    lines.append(row.get("body") or "(no text — see the file)")
    return "\n".join(lines)


def _listing(
    kind: str,
    noun: str,
    page: dict[str, Any],
    describe: Any,
    *,
    summaries: bool = False,
) -> str:
    """A page of documents, with the count and the window it is a page of.

    *kind* is here for the date on each line and nothing else: each kind is
    ordered by its own time axis (`cabinet_store.SPECS`), and the bracketed date
    a reader compares against the order they are reading is that axis.  Reading
    `updated_at` for all four printed a column that ran *against* the list — a
    report created in June and appended to today sorted first and showed today's
    date, and a diary entry showed the day it was written rather than the day it
    is about.
    """
    entries = page["entries"]
    if not entries:
        return f"No {noun} matched. `cabinet_search` looks across all four kinds."
    head = f"{page['total']} {noun}, showing {len(entries)} from {page['offset'] + 1}."
    axis = SPECS[kind].time
    lines = [head]
    for row in entries:
        line = f"- {describe(row)}  [{_when(row, axis)}]"
        if summaries and row.get("summary"):
            line += f"\n    {row['summary']}"
        lines.append(line)
    if page["offset"] + len(entries) < page["total"]:
        lines.append(
            f"\n{page['total'] - page['offset'] - len(entries)} more — pass a "
            f"higher `offset`."
        )
    return "\n".join(lines)


def _when(row: dict[str, Any], axis: str) -> str:
    """A row's time, short, read off *axis* — the column that orders its kind.

    Two spellings because there are two readers: a stored row has the columns
    and is ordered by its kind's own axis (`cabinet_store.SPECS`), and a *search
    result* carries the one time it was given, under `time`.  A renderer that
    knew only one of them printed `?` for every hit; one that read one fixed
    column for both printed a date that did not match the order on screen.
    """
    stamp = str(row.get(axis) or row.get("updated_at") or row.get("created_at") or "")
    return stamp[:10] or "?"


def _found_text(found: dict[str, Any]) -> str:
    """A search answer, in the shape `tool_search` answers in.

    The first line is a verdict and the numbers on it are the ones that ordered
    the page — a number that does not order what a reader sees is a number they
    will sort by and be wrong about. `matched your words` marks the rows the
    keyword leg found, which is the one piece of evidence here that is certain
    rather than graded, and it says *where* when that is not the line printed.

    **A search answer that cannot be read is not a search that found nothing.**
    An answer with no `results` list used to render as "Nothing matched", which
    is a verdict a model acts on rather than a failure it retries (§10.1, "a
    failure is not a verdict"); only a *readable* empty list may say nothing
    matched, and anything else raises.  The raise is this plugin's to make — the
    store's shape is the thing that is wrong — and the far end reads it as a
    failed call rather than as a confident negative.
    """
    rows = found.get("results")
    if not isinstance(rows, list):
        raise ToolError(
            f"cabinet_search could not read the cabinet's answer — `results` "
            f"came back as {rows!r}"
        )
    if not rows:
        return "Nothing matched. Try fewer or different words."
    scored = [
        float(row["similarity"])
        for row in rows
        if isinstance(row.get("similarity"), float)
    ]
    best = max(scored) if scored else 0.0
    browsed = found.get("browsed")
    head = (
        f"{len(rows)} document(s), newest first."
        if browsed
        else (
            f"Found {len(rows)}. Best match {best:.2f} by meaning."
            if scored
            else f"Found {len(rows)} by their words."
        )
    )
    lines = [head, ""]
    for index, row in enumerate(rows, start=1):
        # A search result is not a listing row: it carries the one time it was
        # given, under `time`, whichever kind it came from (`cabinet_store`).
        state = [str(row.get("kind") or ""), _when(row, "time")]
        if row.get("matched_in") == "line":
            state.append("matched your words")
        elif row.get("matched_in") == "rest":
            state.append("matched your words elsewhere in the document")
        if isinstance(row.get("similarity"), float):
            state.append(f"meaning {row['similarity']:.2f}")
        lines.append(f"{index}. {row['key']}  ({'; '.join(state)})")
        # **The one rule for what a line shows**, shared with the test that
        # decides whether the caller's words are on it (`cabinet_store
        # .line_excerpt`): a mark is evidence about *this* text, and the two
        # halves disagreeing — 400 characters tested, 120 printed — put "matched
        # your words" beside an excerpt the words were not in.
        if shown := line_excerpt(row):
            lines.append(f"   {shown}")
    return "\n".join(lines)


def _today() -> str:
    """Today, as a diary key — the one place the diary's date format is decided."""
    return _date.today().isoformat()


def _day(value: str) -> _date:
    """A diary key, parsed — refused rather than stored if it is not a day."""
    return _date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        build=build_server,
        logger=logger,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
