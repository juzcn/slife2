"""slife2-cabinet — the private cabinet: notes, a diary, saved files, reports.

v1 called this plugin `memfiles` and described it as four typed knowledge stores
dual-written to a markdown file and a SQLite index.  This is that, built the way
a plugin here is built: one process, `house_server` plus `serve_plugin`, the
*store* as a library (`slife2.cabinet_store`), the embedder opened once against
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

import asyncio
import logging
import mimetypes
import os
import shutil
from datetime import date as _date
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP

from slife2 import textindex
from slife2.audience import FOR_THE_MODEL, request_client
from slife2.cabinet_store import (
    DIARY,
    FILE,
    KINDS,
    NOTE,
    REPORT,
    CabinetStore,
    discard_file,
    index_path,
    parse_tags,
    resolve,
    store_of,
)
from slife2.config import Config
from slife2.db import Embedder
from slife2.embedder import EmbedderConnection
from slife2.mcp_server import house_server, serve_plugin

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-cabinet"

#: This server's key in the config's `servers:` table.
CONFIG_KEY = "cabinet"

INSTRUCTIONS = (
    "The cabinet: private notes, a diary, saved files and reports, all in one "
    "searchable corpus. A note and a report are keyed by a name you choose, a "
    "diary by its date, a saved file by the path inside the cabinet. `file_save` "
    "copies a file that is already on this machine — nothing here fetches a URL."
)

#: The most of a file `file_read` will read, and the most of a URL it would
#: fetch.  A cap rather than a preference: a tool that returns a gigabyte of text
#: to a model has spent a context window to say "this is not a document".
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
    `slife2.context_server`'s is: the whole plugin — every write, every read,
    both search legs — can then be exercised over the in-memory transport with
    no embedding endpoint behind it.  In production it is opened once against
    the embeddings server, because a write places its vector in the same call
    and a store that cannot embed stores nothing.
    """
    embedding = EmbedderConnection(config, embedder=embedder)

    async def on_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking store call off the event loop.

        SQLite is synchronous and one connection per call is what makes the hop
        safe, so a cabinet that is being written does not stop the process from
        answering anything else.
        """
        return await asyncio.to_thread(function, *args, **kwargs)

    #: Cabinets whose indexes have been brought up to date, once per process.
    #: A cabinet opened after this pass is not re-synced on every call: its own
    #: writes embed, and the only thing a sync adds is a rebuild for a *changed*
    #: embedder, which cannot happen while this process is running.
    synced: set[Path] = set()

    async def store(agent: str) -> CabinetStore:
        """One agent's cabinet, with its indexes brought up to date."""
        opened = await on_thread(store_of, index_path(agent))
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

    def agent_of(ctx: Context) -> str:
        """The agent a call is on behalf of, or `""` when the call does not say.

        A shared process cannot guess it: one cabinet serves every agent, and
        which one is a fact only the caller has — the same rule `slife2-wechat`
        states, and the reason a call that says nothing is refused rather than
        answered with somebody else's diary.
        """
        found = request_client(ctx)
        return found[0] if found else ""

    UNADDRESSED = (
        "This call did not say which conversation it is for, so there is no "
        "cabinet to act on — one process serves every agent, and the cabinet is "
        "the agent's."
    )

    async def cabinet(ctx: Context) -> tuple[CabinetStore, str]:
        """`(the store, "")`, or `(nothing, refusal)` — so a tool reads as one line."""
        agent = agent_of(ctx)
        if not agent:
            return None, UNADDRESSED  # type: ignore[return-value]
        return await store(agent), ""

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
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        if await opened.remove(NOTE, subject) is None:
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
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        row = await on_thread(opened.get, NOTE, subject)
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
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        page = await on_thread(
            opened.listing,
            NOTE,
            since=since or None,
            until=until or None,
            limit=limit,
            offset=offset,
        )
        return _listing("notes", page, lambda row: str(row["key"]))

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
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        row = await on_thread(opened.get, DIARY, date)
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
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        page = await on_thread(
            opened.listing,
            DIARY,
            since=since or None,
            until=until or None,
            limit=limit,
            offset=offset,
        )
        return _listing("diary entries", page, lambda row: str(row["key"]))

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
                await on_thread(shutil.copy2, source, target)
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
                await on_thread(discard_file, target)
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
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        if await opened.remove(FILE, path) is None:
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
        row = await on_thread(opened.get, FILE, path)
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
        data = await on_thread(target.read_bytes)
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
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        page = await on_thread(
            opened.listing,
            FILE,
            since=since or None,
            until=until or None,
            limit=limit,
            offset=offset,
            category=category.strip(),
        )
        return _listing(
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
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        row = await on_thread(opened.get, REPORT, title)
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
        opened, refusal = await cabinet(ctx)
        if refusal:
            return refusal
        page = await on_thread(
            opened.listing,
            REPORT,
            since=since or None,
            until=until or None,
            limit=limit,
            offset=offset,
        )
        return _listing("reports", page, lambda row: str(row["key"]))

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
            kind: `note`, `diary`, `file` or `report` to look in one of them.
                Empty searches all four.
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
            kind: `note`, `diary`, `file` or `report`.
            key: The document's key — a subject, a date, a cabinet path or a title.
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
    noun: str,
    page: dict[str, Any],
    describe: Any,
    *,
    summaries: bool = False,
) -> str:
    """A page of documents, with the count and the window it is a page of."""
    entries = page["entries"]
    if not entries:
        return f"No {noun} matched. `cabinet_search` looks across all four kinds."
    head = f"{page['total']} {noun}, showing {len(entries)} from {page['offset'] + 1}."
    lines = [head]
    for row in entries:
        line = f"- {describe(row)}  [{_when(row)}]"
        if summaries and row.get("summary"):
            line += f"\n    {row['summary']}"
        lines.append(line)
    if page["offset"] + len(entries) < page["total"]:
        lines.append(
            f"\n{page['total'] - page['offset'] - len(entries)} more — pass a "
            f"higher `offset`."
        )
    return "\n".join(lines)


def _when(row: dict[str, Any]) -> str:
    """A row's time, short: the date is what a reader is comparing.

    Two spellings because there are two readers: a stored row has the columns,
    and a *search result* carries the one time it was given, under `time`.  A
    renderer that knew only one of them printed `?` for every hit.
    """
    stamp = str(row.get("time") or row.get("updated_at") or row.get("created_at") or "")
    return stamp[:10] or "?"


def _found_text(found: dict[str, Any]) -> str:
    """A search answer, in the shape `tool_search` answers in.

    The first line is a verdict and the numbers on it are the ones that ordered
    the page — a number that does not order what a reader sees is a number they
    will sort by and be wrong about. `matched your words` marks the rows the
    keyword leg found, which is the one piece of evidence here that is certain
    rather than graded, and it says *where* when that is not the line printed.
    """
    rows = found.get("results") or []
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
        state = [str(row.get("kind") or ""), _when(row)]
        if row.get("matched_in") == "line":
            state.append("matched your words")
        elif row.get("matched_in") == "rest":
            state.append("matched your words elsewhere in the document")
        if isinstance(row.get("similarity"), float):
            state.append(f"meaning {row['similarity']:.2f}")
        body = " ".join(str(row.get("body") or "").split())
        excerpt = body[:120] + ("…" if len(body) > 120 else "")
        lines.append(f"{index}. {row['key']}  ({'; '.join(state)})")
        if row.get("summary"):
            lines.append(f"   {row['summary']}")
        elif excerpt:
            lines.append(f"   {excerpt}")
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
