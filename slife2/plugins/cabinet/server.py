"""slife2-cabinet — the private cabinet: notes, a diary, saved files, reports,
dumps and images.

The cabinet is typed knowledge stores — a note, a day, a saved file, a report, a
dump and an image — dual-written to a markdown file and a SQLite index.  It is
built the way a plugin here is built: one process, `house_server` plus
`serve_plugin`, the *store* as a library (`slife2.plugins.cabinet.store`), the
embedder opened once against the embeddings server, and state keyed by the
client id every call carries.

**The store's calls hop off the event loop where the hop pays**: opening a
cabinet, a single read, and any listing, which are the calls that run while
another agent's turn is being served.  A *write* does its SQLite inline — it is
one transaction against a local file, it is already the shortest path through a
sequence of awaited steps, and wrapping it would be a thread hop per write for a
wait nobody is making.

**Six kinds, one corpus.**  A note, a day, a saved file, a report, a dump and
an image are rows of one table with a `kind` column, so a search ranks them
against each other rather than against their own kind — a rank only means
something inside the corpus that produced it, and one index per kind would be
one corpus per kind (SDD §8.3).  What differs per kind is what a
*document* is, and that is `SPECS` in the store: its key, its time axis, whether
a second write appends, and where its markdown goes.

**A document is written twice, and one of the two is for a person.**  The row is
what a search reads; the markdown under `<data>/agents/<agent>/cabinet/` is what
somebody opens in an editor.  The store owns both writes, because they are one
operation.

**The bytes of a saved file live on disk and never in the index.**  `file_save`
copies them into the cabinet (never moving, never deleting what it copied from);
`file_read` returns a file's *text* or refuses with what it is instead, and the
path to the bytes is what every other tool takes.

**An image is the same shape with a different reader.**  `image_save` is the
harness's — the agent server stores what a turn carried — and the model's way
back is `attach_image`, not a cabinet read: the turn keeps an `[Image:{…}]`
reference and `image_source` resolves its key to a `data:` URI or the original
URL.  An `http(s)` image is never fetched here; the row keeps the URL and the
provider fetches it.

What this cut does **not** do, each named rather than half-built:

* **A URL is not fetched.**  `file_save` takes local paths, and a public
  `http(s)` URL is not a second input: an SSRF guard is a change of its own — a
  library with a threat model, a config switch and its own tests — and building
  it half-way would be a guard that looks like one.
* **There is no `grep` leg.**  The cabinet has two search legs; the third is
  deferred in SDD §14.2 by name, and the cabinet inherits the deferral rather
  than inventing a second regex engine.
* **There is no schedule half.**  The cabinet owns no scheduled-task tables and
  no tools over them; there is no scheduler here, so a report is keyed by its
  **title** instead of by a task — a report is still a log, and repeated saves
  under one title still accumulate.
* **`USER.md` is not served.**  This plugin owns no profile file and no builtin
  that writes one, and nothing here is rendered into the system prompt.  Wiring
  a file into a prompt is a change to the prompt, and this plugin does not make
  it.
"""

from __future__ import annotations

import base64
import hashlib
import json
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
from slife2.libs.plugin.audience import (
    FOR_THE_MODEL,
    request_client,
    unaddressed,
)
from slife2.libs.plugin.protocol import house_server, serve_plugin
from slife2.libs.store import textindex
from slife2.libs.store.index import Embedder
from slife2.libs.turn.tokens import estimate_lines_tokens, estimate_text_tokens
from slife2.plugins.cabinet.store import (
    DIARY,
    DUMP,
    FILE,
    IMAGE,
    KINDS,
    NOTE,
    REPORT,
    CabinetStore,
    discard_file,
    facts,
    index_path,
    parse_tags,
    resolve,
    store_of,
)

logger = logging.getLogger(__name__)

SERVER_NAME = CABINET_SERVER_NAME

#: This server's key in the config's `servers:` table.
CONFIG_KEY = "cabinet"

INSTRUCTIONS = (
    "The cabinet: private notes, a diary, saved files, reports and images, all "
    "in one searchable corpus. A note and a report are keyed by a name you "
    "choose, a diary by its date, a saved file by the path inside the cabinet, "
    "an image by its content digest. `file_save` copies a file that is already "
    "on this machine — nothing here fetches a URL; an image a turn carried is "
    "`image_save`'s, and `attach_image` is how it comes back."
)

#: The most of a file `file_read` will read.  A cap rather than a preference: a
#: tool that returns a gigabyte of text to a model has spent a context window to
#: say "this is not a document".  A URL has no share of this cap, because
#: nothing here fetches a URL — the instructions above say so.
MAX_READ_BYTES = 2 * 1024 * 1024

#: How much of a file is sniffed for the two things that settle its encoding
#: before anything is decoded: a BOM, and a NUL byte.
SNIFF_BYTES = 8192

#: Where a saved file goes, by what it is.  Auto-filing by extension is
#: deliberate: the cabinet is browsed by a person, and `images/` is a
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
#: `file_list(category="notes")` would list both as one thing.  `pictures` is
#: the `image` kind's directory: a file saved there would be indistinguishable
#: from a picture a turn's `[Image:…]` reference points at.
RESERVED_CATEGORIES = frozenset({"notes", "diary", "reports", "pictures"})


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
    is a cabinet that lost the other.  That rule is the reason a saved file's
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
    """A file's bytes as text, and the encoding they were read as.

    **"No text" is not the same question as "is it UTF-8"**, and reading it as
    one refuses text that plainly exists — a `.txt` written by cmd is GBK, and
    one redirected by PowerShell is UTF-16LE.  So the order is: a BOM settles it
    before anything else (UTF-16LE's BOM cannot be mistaken for a PDF, and UTF-8
    with a BOM is still UTF-8); then a NUL in the first block is the tell that a
    file has no text to return at all — a PDF, an image, an archive; then UTF-8
    strict; and only then the guess, which is *announced*, because the one
    outcome still worth refusing is a **silent** wrong decode.

    Returns `(text, encoding)` — the name empty for UTF-8, which is the cabinet's
    own encoding and the web's, and otherwise the encoding the bytes were read
    as, or `"binary"` for a file that has no text to return at all.  A name
    rather than the sentence it used to be, because the answer is JSON: what a
    caller does with it is a fact on the payload, not a line of prose.
    """
    head = data[:SNIFF_BYTES]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "utf-16"
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
        return data.decode("gb18030"), "gb18030"
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

    async def store(agent: str, subagent: str = "") -> CabinetStore:
        """One conversation's cabinet, with its indexes brought up to date."""
        opened = await off_loop(store_of, index_path(agent, subagent))
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

    #: Whose cabinet a call is about, **refused rather than guessed**.  One
    #: cabinet serves every agent, and which one a call is for is a fact only the
    #: caller has — so a call that says nothing is refused rather than answered
    #: with somebody else's diary.  The pair is the client id: a non-fork
    #: subagent resolves to its own cabinet, and a fork to its maker's.
    def client_of(ctx: Context) -> tuple[str, str]:
        found = request_client(ctx)
        if found is None or not found[0]:
            raise ToolError(unaddressed("cabinet to act on", "the cabinet"))
        return found

    async def cabinet(ctx: Context) -> CabinetStore:
        """The cabinet this call is about, with its index up to date."""
        agent, subagent = client_of(ctx)
        return await store(agent, subagent)

    # ── The three moves every kind makes ─────────────────────────────────
    #
    # Six kinds are served below — notes, diary days, files, reports, the tool
    # outputs the harness dumps and the images it keeps — and each is the same
    # three moves: a page,
    # one row, a delete.  What differs between them is the kind and the sentence
    # a miss answers with, and those are arguments; what must *not* differ, and
    # did while each kind had its own copy, is everything a copy gets wrong
    # quietly: the `""` that means "no bound" on every listing, and the thread
    # hop that keeps a store call off the loop.
    #
    # Each answers what the store answered — the page, the row, whether it went —
    # so a tool stays three or four lines whatever kind it serves.  **A miss is
    # the tool's to say, in its own words** (a note is missing *about* a subject,
    # a report is missing *called* a title) and it is said by raising, like every
    # other refusal here.  The answers that need the *store* after the fetch
    # (`file_read` resolves a path, `dump_read` reads a window) keep their own
    # body and say so by asking `cabinet` directly.

    async def page_of(
        ctx: Context,
        kind: str,
        *,
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
        **filters: Any,
    ) -> dict[str, Any]:
        """One page of one kind, as the store built it."""
        opened = await cabinet(ctx)
        return await off_loop(
            opened.listing,
            kind,
            since=since or None,
            until=until or None,
            limit=limit,
            offset=offset,
            **filters,
        )

    async def missing_row(
        ctx: Context, kind: str, key: str, said: str
    ) -> dict[str, Any]:
        """One row of one kind, or this tool's miss — `said` is the sentence.

        A miss is not a refusal to be worded once: a note is missing *about* a
        subject, a report *called* a title, and the sentence a model reads back
        is the one the tool that asked named.
        """
        opened = await cabinet(ctx)
        row = await off_loop(opened.get, kind, key)
        if row is None:
            raise ToolError(said)
        return row

    async def remove_of(ctx: Context, kind: str, key: str, said: str) -> None:
        """Delete one row of one kind, or raise this tool's miss."""
        opened = await cabinet(ctx)
        if await opened.remove(kind, key) is None:
            raise ToolError(said)

    # ── Notes ────────────────────────────────────────────────────────────

    @mcp.tool(name="note_edit", meta=FOR_THE_MODEL)
    async def note_edit(
        ctx: Context, subject: str, content: str, tags: str = ""
    ) -> dict[str, Any]:
        """Write a private note about a subject, replacing what it held.

        A note is a document: this is the whole of what the cabinet keeps under
        that subject, and an older body goes. It lands as markdown in the
        agent's cabinet directory too.

        Args:
            subject: What the note is about — its name, and reusing one replaces
                the note rather than making a second.
            content: The note itself.
            tags: Comma-separated words to file it under.

        Returns:
            The note as it is stored — `kind`, `key`, `title`, `time`, `tags`,
            `summary` and `mirror`.
        """
        opened = await cabinet(ctx)
        row = await opened.write(
            NOTE,
            subject,
            body=content,
            tags=parse_tags(tags),
            embedder=await embedding.get(),
        )
        logger.info("note_edit subject=%s", subject)
        return facts(row)

    @mcp.tool(name="note_remove", meta=FOR_THE_MODEL)
    async def note_remove(ctx: Context, subject: str) -> dict[str, Any]:
        """Delete a note — its markdown, its index entry and its vectors.

        Args:
            subject: The note's subject.

        Returns:
            `kind`, `key` and `removed` — always `true`, because a subject with
            no note is refused rather than answered.
        """
        await remove_of(ctx, NOTE, subject, f"there is no note about {subject!r}.")
        logger.info("note_remove subject=%s", subject)
        return {"kind": NOTE, "key": subject, "removed": True}

    @mcp.tool(name="note_read", meta=FOR_THE_MODEL)
    async def note_read(ctx: Context, subject: str) -> dict[str, Any]:
        """Read a note by its subject.

        Args:
            subject: The note's subject.

        Returns:
            The note's `kind`, `key`, `title`, `time`, `tags`, `summary` and
            `mirror`, and the whole of it under `text`.
        """
        row = await missing_row(
            ctx, NOTE, subject, f"there is no note about {subject!r}."
        )
        return _document(row)

    @mcp.tool(name="note_list", meta=FOR_THE_MODEL)
    async def note_list(
        ctx: Context,
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List notes, most recently written first.

        Args:
            since: Only notes written at or after this. Accepts `2026-10-07`,
                `last week`, `3 days ago`.
            until: Only notes written at or before this.
            limit: How many notes this page may hold.
            offset: How many notes to skip — that is how you page back.

        Returns:
            `entries` — `kind`, `key`, `title`, `time`, `tags`, `summary`,
            `mirror` and `excerpt` per note — and `total`, how many notes the
            window holds, so `offset + len(entries) < total` says whether more
            remain.
        """
        return await page_of(
            ctx, NOTE, since=since, until=until, limit=limit, offset=offset
        )

    # ── The diary ────────────────────────────────────────────────────────

    @mcp.tool(name="diary_write", meta=FOR_THE_MODEL)
    async def diary_write(
        ctx: Context, content: str, date: str = "", tags: str = ""
    ) -> dict[str, Any]:
        """Write today's diary entry, or add to the entry for a given day.

        A later write of the same day appends, so this is safe to call again:
        two entries under one date are two facts about that day.

        Args:
            content: The entry.
            date: The day it is about, `YYYY-MM-DD`. Empty means today.
            tags: Comma-separated words to file it under.

        Returns:
            The day as it is stored — `kind`, `key`, `title`, `time` (the day
            itself), `tags`, `summary` and `mirror`.
        """
        opened = await cabinet(ctx)
        day = date.strip() or _today()
        try:
            _day(day)
        except ValueError as exc:
            raise ToolError(
                f"{day!r} is not a date — write it as `YYYY-MM-DD`."
            ) from exc
        row = await opened.write(
            DIARY,
            day,
            body=content,
            tags=parse_tags(tags),
            embedder=await embedding.get(),
        )
        logger.info("diary_write date=%s", day)
        return facts(row)

    @mcp.tool(name="diary_read", meta=FOR_THE_MODEL)
    async def diary_read(ctx: Context, date: str) -> dict[str, Any]:
        """Read one day's diary entry.

        Args:
            date: The day, `YYYY-MM-DD`.

        Returns:
            The day's `kind`, `key`, `title`, `time`, `tags`, `summary` and
            `mirror`, and the whole entry under `text`.
        """
        row = await missing_row(
            ctx, DIARY, date, f"there is no diary entry for {date}."
        )
        return _document(row)

    @mcp.tool(name="diary_list", meta=FOR_THE_MODEL)
    async def diary_list(
        ctx: Context,
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List diary days, newest first.

        Args:
            since: Only days at or after this.
            until: Only days at or before this.
            limit: How many days this page may hold.
            offset: How many days to skip — that is how you page back.

        Returns:
            `entries` — `kind`, `key` (the day), `title`, `time`, `tags`,
            `summary`, `mirror` and `excerpt` per day — and `total`, how many
            days the window holds, so `offset + len(entries) < total` says
            whether more remain.
        """
        return await page_of(
            ctx, DIARY, since=since, until=until, limit=limit, offset=offset
        )

    # ── Files ────────────────────────────────────────────────────────────

    @mcp.tool(name="file_save", meta=FOR_THE_MODEL)
    async def file_save(
        ctx: Context,
        paths: list[str],
        title: str = "",
        tags: str = "",
        summary: str = "",
        category: str = "",
    ) -> dict[str, Any]:
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

        Returns:
            `saved` — one row per file, as `file_read` describes it, so the
            `key` is the path each file was stored under. A file already there
            under that name is stored beside it rather than over it, which is
            why the key is worth reading.
        """
        opened = await cabinet(ctx)
        wanted = [Path(path) for path in paths]
        missing = [str(path) for path in wanted if not path.is_file()]
        if missing:
            raise ToolError("no such file: " + ", ".join(missing))
        saved: list[dict[str, Any]] = []
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
                raise ToolError(
                    f"{chosen!r} is not one folder name — a category is a single "
                    f"name like `images`, not a path."
                )
            if chosen.casefold() in RESERVED_CATEGORIES:
                raise ToolError(
                    f"{chosen!r} is where the cabinet keeps its "
                    f"{chosen.casefold()}, so a file cannot be filed under it — "
                    f"choose another category, or leave it out to file by extension."
                )
            target = unique_path(opened.root / chosen, source.name)
            relative = target.relative_to(opened.root).as_posix()
            try:
                # The copy is inside the `try` because `unique_path` has already
                # created the destination: a copy that fails leaves an empty file
                # where a failed copy used to leave nothing at all.
                await off_loop(shutil.copy2, source, target)
                written = await opened.write(
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
            saved.append(facts(written))
        logger.info("file_save count=%d", len(saved))
        return {"saved": saved}

    @mcp.tool(name="file_remove", meta=FOR_THE_MODEL)
    async def file_remove(ctx: Context, path: str) -> dict[str, Any]:
        """Delete a saved file — the cabinet's copy, its row and its vectors.

        The file it was copied from stays where it is.

        Args:
            path: The saved file's path inside the cabinet.

        Returns:
            `kind`, `key` and `removed` — always `true`, because a path the
            cabinet has no file at is refused rather than answered.
        """
        await remove_of(ctx, FILE, path, f"there is no saved file at {path!r}.")
        logger.info("file_remove path=%s", path)
        return {"kind": FILE, "key": path, "removed": True}

    @mcp.tool(name="file_read", meta=FOR_THE_MODEL)
    async def file_read(ctx: Context, path: str) -> dict[str, Any]:
        """Read a saved file's text.

        A PDF, an image or an archive has no text: the call is refused with what
        the file is, how big, and where its bytes are. Text in another encoding
        reaches you decoded, with the encoding named beside it.

        Args:
            path: The saved file's path inside the cabinet.

        Returns:
            The file's `kind`, `key`, `title`, `time`, `tags`, `summary`,
            `size`, `mime` and `source`, its text under `text`, and `encoding`
            when that is not UTF-8.
        """
        opened = await cabinet(ctx)
        row = await missing_row(ctx, FILE, path, f"there is no saved file at {path!r}.")
        try:
            target = resolve(opened.root, path)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        if not target.is_file():
            raise ToolError(f"{path!r} is recorded but is not on disk any more.")
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            raise ToolError(
                f"{path} is {size} bytes, past the {MAX_READ_BYTES}-byte cap — "
                f"read it with a tool that takes a path."
            )
        data = await off_loop(target.read_bytes)
        text, encoding = decode(data)
        if encoding == "binary":
            raise ToolError(
                f"{path} is not text ({row['mime'] or 'unknown type'}, {size} "
                f"bytes) — its bytes are at {target}."
            )
        read = _document(row, text=text)
        if encoding:
            read["encoding"] = encoding
        return read

    @mcp.tool(name="file_list", meta=FOR_THE_MODEL)
    async def file_list(
        ctx: Context,
        category: str = "",
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List saved files, newest first.

        Args:
            category: Only files in this directory of the cabinet.
            since: Only files saved at or after this.
            until: Only files saved at or before this.
            limit: How many files this page may hold.
            offset: How many files to skip — that is how you page back.

        Returns:
            `entries` — `kind`, `key` (the path inside the cabinet), `title`,
            `time`, `tags`, `summary`, `size`, `mime`, `source` and `excerpt`
            per file — and `total`, how many files the window holds, so
            `offset + len(entries) < total` says whether more remain.
        """
        return await page_of(
            ctx,
            FILE,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            category=category.strip(),
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
    ) -> dict[str, Any]:
        """Save a report under a title, or add to the one already there.

        A report is a log like a diary: a title that exists gets a new dated
        section, and the earlier one stays.

        Args:
            title: What it is a report of. This is its key.
            content: The report.
            tags: Comma-separated words to file it under.
            period_start: The first day the report covers, `YYYY-MM-DD`.
            period_end: The last day it covers.

        Returns:
            The title as it is stored — `kind`, `key`, `title`, `time`, `tags`,
            `summary`, `mirror`, `period_start` and `period_end`.  `time` is
            when the report was first written; adding a section does not
            change it.
        """
        opened = await cabinet(ctx)
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
        return facts(row)

    @mcp.tool(name="report_read", meta=FOR_THE_MODEL)
    async def report_read(ctx: Context, title: str) -> dict[str, Any]:
        """Read a report by its title.

        Args:
            title: The report's title.

        Returns:
            The report's `kind`, `key`, `title`, `time`, `tags`, `summary`,
            `mirror`, `period_start` and `period_end`, and every section under
            `text`.  `time` is when the report was first written.
        """
        row = await missing_row(
            ctx, REPORT, title, f"there is no report called {title!r}."
        )
        return _document(row)

    @mcp.tool(name="report_list", meta=FOR_THE_MODEL)
    async def report_list(
        ctx: Context,
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List reports, newest first, by when each was first written.

        Adding a section to a report does not move it: the order is when the
        report was first written, not the last time it was added to.

        Args:
            since: Only reports first written at or after this.
            until: Only reports first written at or before this.
            limit: How many reports this page may hold.
            offset: How many reports to skip — that is how you page back.

        Returns:
            `entries` — `kind`, `key`, `title`, `time`, `tags`, `summary`,
            `mirror` and `excerpt` per report — and `total`, how many reports
            the window holds, so `offset + len(entries) < total` says whether
            more remain.
        """
        return await page_of(
            ctx, REPORT, since=since, until=until, limit=limit, offset=offset
        )

    # ── Reading a dump back ──────────────────────────────────────────────

    @mcp.tool(name="dump_read", meta=FOR_THE_MODEL)
    async def dump_read(
        ctx: Context,
        call: str,
        offset: int = 1,
        limit: int = 0,
        pattern: str = "",
        context: int = 0,
    ) -> dict[str, Any]:
        """Read a tool output the harness saved, by finding — not by scrolling.

        A result too large for the conversation is shortened for you and kept
        whole here; the marker standing in for it names the call. This is the way
        back to the part the shortening did not carry, and it is a *searching*
        reader: name what you are looking for and how much of what surrounds it,
        or name the line you already know and read from there. The lines are
        numbered, a matched line is marked `:` and the lines around it `-`, and
        the numbers are the ones the marker beside the result shows.

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

        Returns:
            `total`, how many lines the output has; `text`, the window, each line
            prefixed with its own number; `where`, the file the whole output is
            in; `next`, the `offset` of the window after this one — absent when
            there is nothing further to read of what you asked for; and
            `matched`, how many lines at or after `offset` hold `pattern`, which
            is there only when you gave one, and is `0` with an empty window when
            nothing matched.
        """
        opened = await cabinet(ctx)
        budget = config.tool_result.tokens
        row = await missing_row(
            ctx, DUMP, call, f"there is no saved output for the call {call!r}."
        )
        mirror = str(row.get("mirror") or "")
        where = str(opened.root / mirror) if mirror else ""
        mime = str(row.get("mime") or "")
        if mime and not mime.startswith("text/"):
            # **A dump that is not text is dumped and not read.**  Nothing here
            # decides that a payload is not text: the judgement is made where the
            # result arrived, and the harness writes it down as the row's mime.
            # Sniffing for it here would be this module deciding what a tool
            # returned, which is the one thing the arrangement keeps out of the
            # harness's hands — and there are no lines of a picture to find
            # anyway.
            raise ToolError(
                f"the output of {call!r} is not text ({mime}), so there are no "
                f"lines to read — its bytes are at {where}."
            )
        return _window_of(
            str(row.get("body") or ""),
            where=where,
            offset=offset,
            limit=limit,
            pattern=pattern,
            context=context,
            budget=budget,
        )

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

        Answers with a payload and **refuses by raising**, because its caller is
        the harness and not a model: an answer it cannot read has to be
        distinguishable from one that says nothing went wrong.

        Args:
            call: The tool call id the output came from — the key, and the one
                identifier the conversation and this row both carry.
            output: The whole of what the tool returned.
            tool: The tool's name, so a listing says what produced this.
            mime: What the output is, when it is known not to be text.
        """
        opened = await cabinet(ctx)
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

    # ── Images ───────────────────────────────────────────────────────────
    #
    # The harness's kind, like a dump, and for the same reason: the writer is
    # the agent server's `record`, and the model's way back is not a text
    # reader but `attach_image`.  Both tools are therefore unmarked — they are
    # not in the hub's catalogue, and a model reaches an image through the
    # `[Image:{…}]` reference in its own conversation.
    #
    # **The bytes are content-addressed, so a picture attached twice is one
    # file and one row.**  An `http(s)` URL is not downloaded: this system
    # never makes a request nobody asked for, so such a row keeps the URL in
    # `source` and the provider fetches it (`slife2.libs.turn.attachments`
    # states the same rule for the encoder).

    @mcp.tool(name="image_save")
    async def image_save(ctx: Context, url: str) -> dict[str, Any]:
        """Keep one image whole, keyed by its content digest.

        Called by the agent server when a turn is recorded — never by a model.
        A `data:` URI is decoded and written under `pictures/`; an `http(s)`
        URL is kept as its own source and nothing is fetched here.

        Args:
            url: The `image_url` the conversation carries: a base64 `data:` URI
                or an `http(s)` URL.

        Returns:
            `key` (the cabinet path, and what the turn's `[Image:…]` reference
            names), `source` (the URL, empty for stored bytes), `mime` and
            `size`.
        """
        opened = await cabinet(ctx)
        if url.startswith(("http://", "https://")):
            key = f"pictures/url-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"
            mime = mimetypes.guess_type(url)[0] or ""
            row = await opened.write(
                IMAGE,
                key,
                title=mime or "image",
                source=url,
                mime=mime,
                embedder=await embedding.get(),
            )
            return {
                "key": key,
                "source": url,
                "mime": str(row.get("mime") or ""),
                "size": int(row.get("size") or 0),
            }
        if not url.startswith("data:"):
            raise ToolError("image_save takes a base64 data: URI or an http(s) URL")
        header, _, payload = url.partition(",")
        if not header.endswith(";base64") or not payload:
            raise ToolError("image_save takes a base64 data: URI")
        try:
            data = base64.b64decode(payload, validate=True)
        except Exception as exc:  # noqa: BLE001 - every decode failure is one refusal
            raise ToolError(f"that data: URI is not valid base64: {exc}") from exc
        mime = header.removeprefix("data:").removesuffix(";base64") or (
            "application/octet-stream"
        )
        relative = (
            f"pictures/{hashlib.sha256(data).hexdigest()[:16]}"
            f"{mimetypes.guess_extension(mime) or '.img'}"
        )
        target = opened.root / relative
        created = not target.exists()
        if created:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f"{target.name}.{os.getpid()}.part")
            try:
                with temporary.open("wb") as handle:
                    handle.write(data)
                os.replace(temporary, target)
            except OSError as exc:
                temporary.unlink(missing_ok=True)
                raise ToolError(f"the image could not be written: {exc}") from exc
        try:
            row = await opened.write(
                IMAGE,
                relative,
                title=mime or "image",
                file_path=relative,
                mime=mime,
                size=len(data),
                embedder=await embedding.get(),
            )
        except Exception:
            # **Only a file this call created is taken back.**  A content
            # address can already hold the bytes of an earlier save, and that
            # row (and every marker pointing at it) still needs them.
            if created:
                discard_file(target)
            raise
        logger.info("image_save key=%s mime=%s bytes=%d", relative, mime, len(data))
        return {
            "key": relative,
            "source": "",
            "mime": str(row.get("mime") or ""),
            "size": len(data),
        }

    @mcp.tool(name="image_source")
    async def image_source(ctx: Context, key: str) -> dict[str, Any]:
        """Resolve an image's cabinet key to a URL `attach_image` can take.

        **Called by the model's own `attach_image`**, through the agent server,
        when a `[Image:…]` reference names a key rather than a path.  A stored
        image comes back as a `data:` URI built from its bytes — which is also
        why a data: source with a type this build has no extension for can be
        attached again — and a URL-only row comes back as its URL.

        Args:
            key: The cabinet path the reference names, e.g. `pictures/ab12.png`.

        Returns:
            `key`, `url`, `mime` and `size`.
        """
        opened = await cabinet(ctx)
        row = await missing_row(ctx, IMAGE, key, f"there is no image at {key!r}.")
        saved = str(row.get("file_path") or "")
        if not saved:
            source = str(row.get("source") or "")
            if not source:
                raise ToolError(f"the image at {key!r} has no source left to attach")
            return {
                "key": key,
                "url": source,
                "mime": str(row.get("mime") or ""),
                "size": int(row.get("size") or 0),
            }
        try:
            target = resolve(opened.root, saved)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        if not target.is_file():
            raise ToolError(f"{key!r} is recorded but is not on disk any more.")
        data = await off_loop(target.read_bytes)
        mime = str(row.get("mime") or "application/octet-stream")
        return {
            "key": key,
            "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
            "mime": mime,
            "size": len(data),
        }

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
    ) -> dict[str, Any]:
        """Search the cabinet — every note, day, file and report at once.

        The two legs add up rather than competing: give both for the strongest
        answer, a sentence alone for meaning, or words alone — the words are
        also read by meaning as a sentence made of them. Give neither to browse
        what is there, newest first.

        **A hit need not contain the words.**  The meaning leg ranks the
        nearest documents whether or not they carry them, and adding a sentence
        only adds rows; `matched_in` says when the words were found, and
        `similarity` says how close a meaning hit came. There is no exact-only
        mode, and an empty page means the filters left nothing to rank — not
        that nothing was close.

        Args:
            keywords: Words to look for — a path like `files/docs/spec.pdf`
                works here. They rank documents that carry them first, but
                they are not a filter: with no sentence, the same words are
                also read by meaning, so a hit may be a document that does not
                contain them.
            sentences: What you are looking for, asked normally. One per
                question you have.
            kind: `note`, `diary`, `file`, `report`, `dump` or `image` — the
                last two are the harness's (a tool's output and a picture a
                turn carried) — to look in one of them. Empty searches all of
                them but the dumps, which are reached by the marker naming them
                rather than by a search.
            since: Only documents from this time on.
            until: Only documents up to this time.
            limit: How many hits this page may hold.

        Returns:
            `results` — `kind`, `key`, `title`, `time`, `tags`, `summary` and
            `excerpt` per hit, with `similarity` where the meaning leg ranked it
            and `matched_in` (`line`, or `rest` for elsewhere in the document)
            where the words leg found it — and `browsed`, true when neither leg
            was given and the page is simply newest first.
        """
        opened = await cabinet(ctx)
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
            raise ToolError(str(exc)) from exc
        return _hits(found)

    @mcp.tool(name="cabinet_summarize", meta=FOR_THE_MODEL)
    async def cabinet_summarize(
        ctx: Context,
        kind: str,
        key: str,
        summary: str = "",
        tags: str = "",
    ) -> dict[str, Any]:
        """Describe a document already in the cabinet so it can be found.

        Tags work for any kind; a summary is what a file's meaning is built
        from, so writing one for a file re-embeds it — a file has no text of its
        own, and a search reaches it by its name and paths until somebody
        describes it.

        Args:
            kind: `note`, `diary`, `file`, `report`, `dump` or `image`.
            key: The document's key — a subject, a date, a cabinet path, a title
                or the tool call a dump is the output of.
            summary: What it is, in words somebody would search with.
            tags: Comma-separated words to file it under.

        Returns:
            The document's own fields, and `reembedded` — true when the file's
            vector was built again, so a search by meaning finds the new words.
        """
        opened = await cabinet(ctx)
        wanted = kind.strip()
        if wanted not in KINDS:
            raise ToolError(f"`kind` must be one of: {', '.join(KINDS)}.")
        row = await opened.annotate(
            wanted,
            key,
            summary=summary,
            tags=parse_tags(tags),
            embedder=await embedding.get(),
        )
        if row is None:
            raise ToolError(f"there is no {wanted} with the key {key!r}.")
        logger.info("cabinet_summarize kind=%s key=%s", kind, key)
        return {
            **facts(row),
            "reembedded": wanted in (FILE, IMAGE) and bool(summary),
        }

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
) -> dict[str, Any]:
    """A window of a dump, read by *finding*: never larger than `budget` tokens.

    **Finding and reading are one gesture here, because neither half answers on
    its own.**  A match without its neighbours is a line torn out of a stack
    trace, and a window without a match is a page a model reads to the end to
    learn nothing.  So `pattern` chooses the lines and `context` brings their
    neighbours with them, marked so that the two are told apart at a glance, and
    two neighbourhoods that meet are **merged** — one block, which is what a
    reader sees, rather than two that overlap and spend the window twice.

    **The bound is on the whole answer, the envelope included**, and that is the
    property the arrangement rests on rather than a nicety: a reader that could
    hand back more than the budget would be a tool of ours feeding the harness's
    own shortening, which is one system at both ends of a loop.  So the window
    is filled line by line against the budget — measured by the one estimator,
    `slife2.libs.turn.tokens` — and then the *payload* is measured and trimmed
    from its end until it fits, because `total`, `where` and the count of what
    matched are part of what a model is handed.  What did not fit is named
    rather than lost: how many lines there are, how many matched, and the offset
    the next window starts at.

    **Nothing the caller wrote is quoted back**, which is why there is no
    clipping left to do here: the old reader put `pattern` into a sentence about
    it, so a megabyte of pattern was a megabyte of answer and only a
    `clip_to_tokens` at the exit kept it inside the budget.  The payload names
    none of it — a model knows what it asked for — and what is left is bounded
    by the window it was just fitted to.

    **The budget is the config's** (`tool_result.tokens`) and not a number of
    this module's, because the harness spends the same one shortening a result
    for the conversation: a reader free to differ from the shortening is the
    loop again, one config edit away.

    **The pattern is text, not an expression.**  A regex over a 240,000-line
    output is a program whose runtime nobody bounds, running in a server every
    agent shares — and the code-reading tools that take a regex take it because
    their engines cannot backtrack, which Python's can.  A literal substring,
    ignoring case, is also what the common case wants: a symbol, a path, a
    spelling remembered approximately (SDD §14.2's own three examples for the
    grep leg).  A model that needs a real regex has the file — `where` names
    it, and the operator's own tools read it.

    **The numbers are the ones the harness's shortening shows a model**, on
    purpose: a line named in a discriminator's reply and a line read back here
    have to be one address, and two renderings that disagreed would be two
    addresses for one line.
    """
    lines = body.splitlines()
    total = len(lines)
    if not total:
        raise ToolError("this output was saved empty, so there is nothing to read.")
    # **A negative `offset` counts from the end**, which is where a log's and a
    # command's own summary is: a model asking for the tail of something it has
    # never read does not know how long it is, and every reader it has used
    # before takes `-1` for the last line.
    start = offset if offset > 0 else total + offset + 1
    if start < 1 or start > total:
        raise ToolError(
            f"{offset} is not a line of this output: it has {total}, and 1 to "
            f"{total} (or -1 to -{total}) are the addresses."
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
    # **No match is not an empty window.**  A pattern that found nothing
    # returned no *lines*, and answering that with an empty window and no count
    # would read as "there is nothing here", which is a different and false
    # claim — so the count is what says so, and it is what sends a model to
    # fewer or different words rather than to a file problem it does not have.
    if wanted and not matched:
        return {"total": total, "text": "", "where": _where(where), "matched": 0}
    runs = _runs(hits, max(context, 0), total) if wanted else [(start, total)]

    kept: list[tuple[int, str]] = []
    full = False
    for first, last in runs:
        for number in range(first, last + 1):
            if limit and len(kept) >= limit:
                full = True
                break
            kept.append((number, lines[number - 1]))
            if estimate_lines_tokens(kept) > budget:
                kept.pop()
                full = True
                break
        if full:
            break

    while kept:
        window = _render_window(
            kept,
            total=total,
            matched=matched,
            hits=set(hits),
            pattern=pattern,
            where=where,
        )
        if estimate_text_tokens(_wire(window)) <= budget:
            return window
        kept.pop()
    # The one output a window cannot show, and it is a fact about the *budget*
    # rather than about the output: nothing here truncates a line to make it
    # fit, so a line longer than the whole window is named as such with the file
    # it is in, which is where the operator's own reader can page it.
    raise ToolError(
        f"a line from {start} does not fit a {budget}-token window on its own — "
        f"the whole output is at {_where(where)}."
    )


def _where(where: str) -> str:
    """Where a dump's bytes are, or the cabinet — the one spelling of it.

    A row written before the harness named a mirror has none, and an answer that
    said `""` would be one a reader cannot act on; the cabinet is true of every
    dump, because that is the directory they live in.
    """
    return where or "the cabinet"


def _wire(payload: dict[str, Any]) -> str:
    """A payload as the text a model is handed, for measuring it against the budget.

    JSON, which is what every tool of ours answers in — and deliberately the
    *wider* of the two spellings of it: the serializer that puts a structured
    result in a tool message drops the spaces after `,` and `:`, and this keeps
    them, so the number a window is measured against is never smaller than what
    the model reads.
    """
    return json.dumps(payload, ensure_ascii=True, default=str)


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


def _render_window(
    kept: list[tuple[int, str]],
    *,
    total: int,
    matched: int,
    hits: set[int],
    pattern: str,
    where: str,
) -> dict[str, Any]:
    """A window as the payload a model reads: numbered lines, and where they sit.

    **A matching line and its neighbours are told apart by one character**,
    `12: the error` against `11- the line before it`, which is the convention
    every grep has and therefore the one a model reads without being told.  With
    no pattern there is nothing to tell apart and the column is left blank.

    The keys are the ways a window can be partial, and a reader that could not
    tell them apart would not know whether to read on, page, or narrow the
    pattern — `total` says how long the output is, `next` where this window
    stops, and `matched` how many lines hold the pattern.  **The window itself is
    one `text`**, numbered, and not a list of line objects: a number is the
    address both halves of this arrangement use, and JSON around each line would
    spend a third of a small budget saying `{"n":` again.
    """
    first, last = kept[0][0], kept[-1][0]
    width = len(str(last))
    if pattern:
        text = "\n".join(
            f"{number:>{width}}{':' if number in hits else '-'} {line}"
            for number, line in kept
        )
    else:
        text = "\n".join(f"{number:>{width}}  {line}" for number, line in kept)
    window: dict[str, Any] = {
        "total": total,
        "text": text,
        "where": _where(where),
    }
    # What is still to come, of what was asked for: with a pattern that is the
    # next match rather than the next line, so a reader is not sent to a window
    # that can only answer "none of them".
    if pattern:
        window["matched"] = matched
        if sum(1 for number in hits if first <= number <= last) < matched:
            window["next"] = last + 1
    elif last < total:
        window["next"] = last + 1
    return window


def _document(row: dict[str, Any], *, text: str | None = None) -> dict[str, Any]:
    """One document as the payload a model reads: its own fields, and its text.

    `text` is the caller's where the document's text is not the row's — a saved
    file's bytes are on disk and its `body` column is empty — and the row's own
    `body` otherwise.  One key for both, because "the text of this document" is
    one question however a kind answers it.
    """
    return {
        **facts(row),
        "text": str(row.get("body") or "") if text is None else text,
    }


def _hits(found: dict[str, Any]) -> dict[str, Any]:
    """A search answer as the payload a model reads.

    **A search answer that cannot be read is not a search that found nothing.**
    An answer with no `results` list used to render as the same "nothing
    matched" a real miss produces, which is a verdict a model acts on rather
    than a failure it retries (SDD Appendix C, "a failure is not a verdict"); only a
    *readable* empty list may say the search found nothing, and anything else
    fails the call.  The raise is this plugin's to make — the store's shape is
    the thing that is wrong — and the far end reads it as a failed call rather
    than as a confident negative.

    `browsed` is given whether or not it is true, because "these are the newest,
    unranked" and "these are what your words and your sentences found" are the
    two things a reader of the page has to tell apart, and a key that is only
    present in one of the two cases is one they have to know to look for.
    """
    rows = found.get("results")
    if not isinstance(rows, list):
        raise ToolError(
            f"cabinet_search could not read the cabinet's answer — `results` "
            f"came back as {rows!r}"
        )
    return {"results": rows, "browsed": bool(found.get("browsed"))}


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
