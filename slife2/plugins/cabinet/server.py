"""slife2-cabinet — the private cabinet: notes, a diary, saved files, reports,
and the dumps and images a turn leaves here.

The cabinet is typed knowledge stores — a note, a day, a saved file, a report, a
dump and an image — written both as a row and, for the text kinds, as a markdown
file.  The first four are the corpus a search ranks; the last two are a turn's
attachments, stored here and addressed by the turn's own markers.  It is built
the way a plugin here is built: one process, `house_server` plus `serve_plugin`,
the *store* as a library (`slife2.plugins.cabinet.store`), the embedder opened
once against the embeddings server, and state keyed by the client id every call
carries.

**The store's calls hop off the event loop where the hop pays**: opening a
cabinet, a single read, and any listing, which are the calls that run while
another agent's turn is being served.  A *write* does its SQLite inline — it is
one transaction against a local file, it is already the shortest path through a
sequence of awaited steps, and wrapping it would be a thread hop per write for a
wait nobody is making.

**Each document kind has the same four verbs.**  `{kind}_write` creates and
refuses a key that already exists, `{kind}_read` reads it, `{kind}_update`
replaces the whole document, and `{kind}_remove` deletes it.  A saved file is
the one kind whose write is not text — `file_save` copies bytes in — and its
read and remove keep their own names because its text is read from disk.

**Six kinds of row, four in the corpus.**  A note, a day, a saved file, a
report, a dump and an image are rows of one table with a `kind` column, because
they share a directory, a key and a write path.  The corpus is the four the
cabinet manages, so a search ranks them against each other rather than against
their own kind — a rank only means something inside the corpus that produced it,
and one index per kind would be one corpus per kind (SDD §8.3).  A dump and an
image are a turn's attachments: the turn's marker is their address
(`[DUMP:…]` → `dump_read`, `[Image:…]` → `attach_image`), so they are stored
here and are not indexed.  What differs per kind is what a *document* is, and
that is `SPECS` in the store: its key, its time axis, where its markdown goes,
and whether it is in the corpus.

**A text row is written twice, and one of the two is for a person.**  For the
corpus documents, the row is what a search reads; the markdown under
`<data>/agents/<agent>/cabinet/` is what somebody opens in an editor.  A dump is
mirrored too — it is text a person may want to read — while a file and an image
have no mirror, because their bytes are the document.  The store owns both
writes, because they are one operation.

**The bytes of a saved file live on disk and never in the index.**  `file_save`
copies them into the cabinet (never moving, never deleting what it copied from);
`file_read` returns a file's *text* or refuses with what it is instead, and the
path to the bytes is what every other tool takes.

**A file comes from a local path or an `http(s)` URL.**  A local file is copied,
never moved; a URL is fetched whole into the cabinet, bounded by
`MAX_FETCH_BYTES` and `transport.file_fetch`.  The URL is untrusted input, so the
scheme is `http(s)` and nothing else, credentials in a URL are refused, and a
host written as an address or as `localhost` must be globally routable — a
fetched URL reaches the internet, not this machine.  Every redirect is checked
again, because a public URL that answers `302 http://127.0.0.1/` is the same
request as naming the loopback address.

**An image is the same shape with a different reader.**  `image_save` is the
harness's — the agent server stores what a turn carried — and the model's way
back is `attach_image`, not a cabinet read: the turn keeps an `[Image:{…}]`
reference and `image_source` resolves its key to a `data:` URI or the original
URL.  An `http(s)` image is never fetched here; the row keeps the URL and the
provider fetches it.  Like a dump, an image is not in the corpus: the reference
is its address, so there is no index entry for it.

What this cut does **not** do, each named rather than half-built:

* **There is no `grep` leg.**  The cabinet has two search legs; the third is
  deferred in SDD §14.2 by name, and the cabinet inherits the deferral rather
  than inventing a second regex engine.
* **There is no schedule half.**  The cabinet owns no scheduled-task tables and
  no tools over them; there is no scheduler here, so a report is keyed by its
  **title** instead of by a task, and a second write under one title is a
  replacement of that one document.
* **`USER.md` is not served.**  This plugin owns no profile file and no builtin
  that writes one, and nothing here is rendered into the system prompt.  Wiring
  a file into a prompt is a change to the prompt, and this plugin does not make
  it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import urllib.parse
from datetime import date as _date
from pathlib import Path
from typing import Any

import httpx2
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from slife2.clients.embedder import EmbedderConnection
from slife2.helpers import timeouts as _T
from slife2.helpers.threads import off_loop
from slife2.libs.host.config import CABINET_SERVER_NAME, Config
from slife2.libs.plugin.audience import FOR_THE_MODEL
from slife2.libs.plugin.protocol import client_reader, house_server, serve_plugin
from slife2.libs.store.index import Embedder
from slife2.libs.turn.tokens import estimate_lines_tokens, estimate_text_tokens
from slife2.plugins.cabinet.store import (
    CORPUS_KIND_HINT,
    DIARY,
    DOCUMENT_KINDS,
    DUMP,
    FILE,
    IMAGE,
    NOTE,
    REPLACE,
    REPORT,
    UPSERT,
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
    "The cabinet: private notes, a diary, saved files and reports, all in one "
    "searchable corpus. A note and a report are keyed by a name you choose, a "
    "diary by its date, and a saved file by the path inside the cabinet. A "
    "note, a day and a report share four tools: write creates, read reads, "
    "update replaces the whole document, and remove deletes it — "
    "`note_write`/`note_read`/`note_update`/`note_remove`, and the same with "
    "`diary_` and `report_` in place of `note_`. `file_save` copies a local "
    "file or fetches an `http(s)` URL into the cabinet. A tool output the "
    "harness kept whole is read with `dump_read` by the call id in its "
    "`[DUMP:…]` marker; an image a turn carried comes back with `attach_image` "
    "from its `[Image:…]` key."
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


# ── Fetching a URL ──────────────────────────────────────────────────────────

#: The most a URL may bring into the cabinet, in bytes.  A cap rather than a
#: preference: a server that streams for ever would spend the operator's disk
#: and this process's memory, and a saved file is a file rather than the text
#: `MAX_READ_BYTES` bounds.
MAX_FETCH_BYTES = 50 * 1024 * 1024

#: How many redirects one fetch follows.  Every hop passes `_checked_url`, so
#: this bounds the chain rather than standing in for the guard.
MAX_REDIRECTS = 5


def _checked_url(url: str) -> urllib.parse.SplitResult:
    """One URL, or the refusal that says why it is not fetched.

    **The scheme and the destination are the guard.**  A `file_save` argument
    is a model's, so a URL in it is untrusted: only `http(s)` is accepted,
    credentials in the URL are refused (they would land in the row's `source`
    and the keyword index), and a host written *as an address* or as
    `localhost` must be globally routable.  Names are left to the machine's
    resolver — a fake-ip proxy answers an address the proxy owns rather than
    the destination, so resolving one here would test the proxy rather than the
    site — while a literal is the address itself and is the check that means
    what it says.  Every redirect passes through this again, so a public URL
    cannot hop to a private one.
    """

    parts = urllib.parse.urlsplit(url)
    if parts.scheme.casefold() not in ("http", "https"):
        raise ToolError(f"{url!r} is not an http(s) URL — only those are fetched.")
    if not parts.hostname:
        raise ToolError(f"{url!r} names no host.")
    if parts.username is not None or parts.password is not None:
        raise ToolError(
            f"{url!r} carries credentials — fetching does not sign in, and the "
            f"URL is stored where it could be read."
        )
    host = parts.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise ToolError(
            f"{url!r} names this machine — a fetched URL may only reach the "
            f"internet."
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # An address written in a form the parser does not read is a form some
        # resolvers do: `2130706433` and `0x7f000001` are both 127.0.0.1.  They
        # are refused rather than handed to a resolver as an unchecked address.
        labels = host.split(".")
        if host.replace(".", "").isdigit() or any(
            label.startswith("0x") for label in labels
        ):
            raise ToolError(f"{url!r} is not an address this tool can check.") from None
        return parts
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if not (address.is_global and not address.is_multicast):
        raise ToolError(
            f"{url!r} is this machine or a private network — a fetched URL may "
            f"only reach the internet."
        )
    return parts


async def _fetch_url(
    url: str, *, client: httpx2.AsyncClient | None = None
) -> tuple[bytes, str]:
    """One URL, whole and bounded, and its media type.

    **Redirects are followed here rather than by the client**, because every
    hop has to pass `_checked_url`: a public URL that answers
    `302 http://127.0.0.1/` is the same request as naming the loopback address.
    The body is read in chunks against `MAX_FETCH_BYTES`, so a server that
    streams for ever is stopped rather than followed.

    `client` is the seam an offline test uses to supply the response; in
    production one client is opened per call, with the one timeout and no
    automatic redirects.
    """

    async def fetched(one: httpx2.AsyncClient) -> tuple[bytes, str]:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            _checked_url(current)
            try:
                async with one.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ToolError(
                                f"{current} redirected without a location."
                            )
                        current = urllib.parse.urljoin(current, location)
                        continue
                    response.raise_for_status()
                    mime = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_FETCH_BYTES:
                            raise ToolError(
                                f"{url} is larger than the "
                                f"{MAX_FETCH_BYTES}-byte cap — the cabinet keeps "
                                f"files, not streams."
                            )
                        chunks.append(chunk)
                    return b"".join(chunks), mime
            except httpx2.HTTPError as exc:
                raise ToolError(f"could not fetch {current}: {exc}") from exc
        raise ToolError(f"{url} redirected more than {MAX_REDIRECTS} times.")

    if client is not None:
        return await fetched(client)
    # **Read at call time**, so tuning `transport.file_fetch` reaches this call
    # rather than a value captured when the module was imported.
    budget = _T.timeouts.transport.file_fetch
    try:
        async with asyncio.timeout(budget):
            async with httpx2.AsyncClient(
                timeout=httpx2.Timeout(budget), follow_redirects=False
            ) as opened:
                return await fetched(opened)
    except TimeoutError as exc:
        raise ToolError(
            f"fetching {url} took longer than {budget:.0f} seconds."
        ) from exc


#: What survives into a fetched file's name.  Letters, digits, dots and spaces
#: are kept — a name a site chose is worth keeping — and everything else becomes
#: a dash, the compromise `slug` makes for the mirrors.
_UNSAFE_NAME = re.compile(r"[^\w.\- ]+", re.UNICODE)

#: Names Windows gives a device rather than a file.  A fetched URL is the one
#: source of a filename nobody on this machine chose, so it is the one place
#: these have to become ordinary names.
_RESERVED_FILE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def _download_name(url: str, mime: str) -> str:
    """The filename a fetched URL is stored under.

    The last segment of the path, kept where it is a name; the media type's
    canonical extension when the site gave none; `download` when the URL says
    nothing at all.  What comes back is one filename — no separator, no device
    name, no over-long stem — because it is written under a directory the
    cabinet owns.
    """
    segment = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    name = _UNSAFE_NAME.sub("-", urllib.parse.unquote(segment)).strip(" .-")
    if not name:
        name = "download"
    stem, suffix = Path(name).stem, Path(name).suffix[:16]
    if stem.upper() in _RESERVED_FILE_NAMES:
        stem = f"_{stem}"
    if not suffix and mime:
        suffix = (mimetypes.guess_extension(mime) or "")[:16]
    return f"{stem[:80] or 'download'}{suffix}"


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
    client_of = client_reader("cabinet to act on", "the cabinet")

    async def cabinet(ctx: Context) -> CabinetStore:
        """The cabinet this call is about, with its index up to date."""
        agent, subagent = client_of(ctx)
        return await store(agent, subagent)

    # ── The same four verbs over every document kind ─────────────────────
    #
    # The four corpus kinds are served below — notes, diary days, files and
    # reports.  A note, a day and a report each answer write, read, update and
    # remove, plus a page; a saved file answers the same read and remove by its
    # own names because its text comes from disk, and `file_save` copies bytes
    # in rather than writing text.  The two attachments a turn leaves here are
    # read by address instead: a dump by `dump_read`, an image by
    # `image_source`.  What differs between the kinds is the key, the sentence
    # a refusal answers with, and — for a report — the period; those are
    # arguments.  What must *not* differ, and did while each kind had its own
    # copy, is everything a copy gets wrong quietly: the `""` that means "no
    # bound" on every listing, and the thread hop that keeps a store call off
    # the loop.
    #
    # Each answers what the store answered — the page, the row, whether it went
    # — so a tool stays a few lines whatever kind it serves.  **A refusal is
    # the tool's to say, in its own words** (a note is missing *about* a
    # subject, a report is missing *called* a title) and it is said by raising,
    # like every other refusal here.  A write's `None` is the store saying the
    # key was already there; an update's is the row not being there.  The
    # answers that need the *store* after the fetch (`file_read` resolves a
    # path, `dump_read` reads a window) keep their own body and say so by
    # asking `cabinet` directly.

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

    @mcp.tool(name="note_write", meta=FOR_THE_MODEL)
    async def note_write(
        ctx: Context, subject: str, content: str, tags: str = ""
    ) -> dict[str, Any]:
        """Create a private note about a subject.

        A subject that already has a note is refused rather than overwritten —
        read it with `note_read`, and change it with `note_update`.

        Args:
            subject: What the note is about — its name, and the key it is read
                by.
            content: The note itself.
            tags: Comma-separated words to file it under.

        Returns:
            The note as it is stored — `kind`, `key`, `title`, `time`, `tags`
            and `summary`.
        """
        opened = await cabinet(ctx)
        row = await opened.write(
            NOTE,
            subject,
            body=content,
            tags=parse_tags(tags),
            embedder=await embedding.get(),
        )
        if row is None:
            raise ToolError(
                f"there is already a note about {subject!r} — read it with "
                f"note_read, or replace it with note_update."
            )
        logger.info("note_write subject=%s", subject)
        return facts(row)

    @mcp.tool(name="note_update", meta=FOR_THE_MODEL)
    async def note_update(
        ctx: Context, subject: str, content: str, tags: str = ""
    ) -> dict[str, Any]:
        """Replace a note about a subject.

        The whole note becomes what you pass: `content` and `tags` are the
        document, not a change to it.  A subject that has no note is refused.

        Args:
            subject: The note's subject.
            content: The note, whole.
            tags: Comma-separated words to file it under. Empty clears them.

        Returns:
            The note as it is stored — `kind`, `key`, `title`, `time`, `tags`
            and `summary`.
        """
        opened = await cabinet(ctx)
        row = await opened.write(
            NOTE,
            subject,
            mode=REPLACE,
            body=content,
            tags=parse_tags(tags),
            embedder=await embedding.get(),
        )
        if row is None:
            raise ToolError(f"there is no note about {subject!r}.")
        logger.info("note_update subject=%s", subject)
        return facts(row)

    @mcp.tool(name="note_remove", meta=FOR_THE_MODEL)
    async def note_remove(ctx: Context, subject: str) -> dict[str, Any]:
        """Delete a note, and everything that made it findable.

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
            The note's `kind`, `key`, `title`, `time`, `tags` and `summary`,
            and the whole of it under `text`.
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
            `entries` — `kind`, `key`, `title`, `time`, `tags`, `summary` and
            `excerpt` per note — and `total`, how many notes match the filters,
            so `offset + len(entries) < total` says whether more remain.
        """
        return await page_of(
            ctx, NOTE, since=since, until=until, limit=limit, offset=offset
        )

    # ── The diary ────────────────────────────────────────────────────────

    @mcp.tool(name="diary_write", meta=FOR_THE_MODEL)
    async def diary_write(
        ctx: Context, content: str, date: str = "", tags: str = ""
    ) -> dict[str, Any]:
        """Create the diary entry for a day.

        One day is one document: a day that already has an entry is refused
        rather than overwritten — read it with `diary_read`, and change it with
        `diary_update`.

        Args:
            content: The entry.
            date: The day it is about, `YYYY-MM-DD`. Empty means today.
            tags: Comma-separated words to file it under.

        Returns:
            The day as it is stored — `kind`, `key`, `title`, `time` (the day
            itself), `tags` and `summary`.
        """
        opened = await cabinet(ctx)
        day = _diary_key(date)
        row = await opened.write(
            DIARY,
            day,
            body=content,
            tags=parse_tags(tags),
            embedder=await embedding.get(),
        )
        if row is None:
            raise ToolError(
                f"there is already a diary entry for {day} — read it with "
                f"diary_read, or replace it with diary_update."
            )
        logger.info("diary_write date=%s", day)
        return facts(row)

    @mcp.tool(name="diary_read", meta=FOR_THE_MODEL)
    async def diary_read(ctx: Context, date: str) -> dict[str, Any]:
        """Read one day's diary entry.

        Args:
            date: The day, `YYYY-MM-DD`.

        Returns:
            The day's `kind`, `key`, `title`, `time`, `tags` and `summary`, and
            the whole entry under `text`.
        """
        row = await missing_row(
            ctx, DIARY, date, f"there is no diary entry for {date}."
        )
        return _document(row)

    @mcp.tool(name="diary_update", meta=FOR_THE_MODEL)
    async def diary_update(
        ctx: Context, content: str, date: str = "", tags: str = ""
    ) -> dict[str, Any]:
        """Replace one day's diary entry.

        The whole entry becomes what you pass: `content` and `tags` are the
        document, not a change to it.  A day that has no entry is refused —
        `diary_write` creates it.

        Args:
            content: The entry, whole.
            date: The day, `YYYY-MM-DD`. Empty means today.
            tags: Comma-separated words to file it under. Empty clears them.

        Returns:
            The day as it is stored — `kind`, `key`, `title`, `time` (the day
            itself), `tags` and `summary`.
        """
        opened = await cabinet(ctx)
        day = _diary_key(date)
        row = await opened.write(
            DIARY,
            day,
            mode=REPLACE,
            body=content,
            tags=parse_tags(tags),
            embedder=await embedding.get(),
        )
        if row is None:
            raise ToolError(f"there is no diary entry for {day}.")
        logger.info("diary_update date=%s", day)
        return facts(row)

    @mcp.tool(name="diary_remove", meta=FOR_THE_MODEL)
    async def diary_remove(ctx: Context, date: str) -> dict[str, Any]:
        """Delete one day's diary entry.

        A day is one document, so this removes everything written for it.

        Args:
            date: The day, `YYYY-MM-DD`.

        Returns:
            `kind`, `key` and `removed` — always `true`, because a day with no
            entry is refused rather than answered.
        """
        await remove_of(ctx, DIARY, date, f"there is no diary entry for {date}.")
        logger.info("diary_remove date=%s", date)
        return {"kind": DIARY, "key": date, "removed": True}

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
            `summary` and `excerpt` per day — and `total`, how many days match
            the filters, so `offset + len(entries) < total` says whether more
            remain.
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
        """Copy a local file, or fetch a URL, into the cabinet.

        A local file is copied and never moved, so what you point at stays
        where it is. An `http(s)` URL is fetched and stored under the site's
        filename. A search does not read inside the file: it matches the file's
        name, where it came from and any `summary` you write.

        Args:
            paths: Local paths or `http(s)` URLs. Name the files in a directory
                rather than the directory itself.
            title: A name for it. Empty uses the filename.
            tags: Comma-separated words to file it under.
            summary: What the file is, in your words — the text its semantic
                search is built from.
            category: The directory inside the cabinet — `images`, `documents`
                and so on. Empty picks one from the extension.

        Returns:
            `saved` — one row per file, with the fields a listing shows, so the
            `key` is the path each file was stored under. A file already there
            under that name is stored beside it rather than over it, which is
            why the key is worth reading.
        """
        opened = await cabinet(ctx)
        entries = [path.strip() for path in paths if path.strip()]
        if not entries:
            raise ToolError("name at least one local path or http(s) URL.")
        local: list[Path] = []
        for entry in entries:
            scheme = urllib.parse.urlsplit(entry).scheme.casefold()
            if scheme in ("http", "https"):
                continue
            if scheme and "://" in entry:
                raise ToolError(
                    f"{entry!r} is not an http(s) URL — only those are fetched."
                )
            local.append(Path(entry))
        missing = [str(path) for path in local if not path.is_file()]
        if missing:
            raise ToolError("no such file: " + ", ".join(missing))
        saved: list[dict[str, Any]] = []
        for entry in entries:
            from_url = urllib.parse.urlsplit(entry).scheme.casefold() in (
                "http",
                "https",
            )
            data: bytes | None = None
            source: Path | None = None
            if from_url:
                data, mime = await _fetch_url(entry)
                name = _download_name(entry, mime)
                mime = mime or (mimetypes.guess_type(name)[0] or "")
            else:
                source = Path(entry)
                name = source.name
                mime = mimetypes.guess_type(name)[0] or ""
            chosen = category.strip() or category_for(name)
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
            target = unique_path(opened.root / chosen, name)
            relative = target.relative_to(opened.root).as_posix()
            try:
                # The write is inside the `try` because `unique_path` has
                # already created the destination: a copy that fails leaves an
                # empty file where a failed copy used to leave nothing at all.
                if from_url:
                    assert data is not None
                    await off_loop(target.write_bytes, data)
                    size = len(data)
                else:
                    assert source is not None
                    await off_loop(shutil.copy2, source, target)
                    size = target.stat().st_size
                written = await opened.write(
                    FILE,
                    relative,
                    # The path is the file's identity and `unique_path` has
                    # already claimed it: a row here is this document's, from an
                    # earlier save whose file went outside the cabinet, and
                    # replacing it is the repair rather than a second file.
                    mode=UPSERT,
                    title=title.strip() or Path(name).stem,
                    tags=parse_tags(tags),
                    summary=summary,
                    source=entry if from_url else str(source),
                    file_path=relative,
                    mime=mime,
                    size=size,
                    embedder=await embedding.get(),
                )
            except Exception:
                # **A save that raises stores nothing** — the same contract a
                # turn's save keeps.  The bytes are already on disk by now, so
                # they are what has to be taken back; a row without its file, or
                # a file the cabinet has forgotten, are both worse than a refusal.
                await off_loop(discard_file, target)
                raise
            assert written is not None, "an upsert always writes"
            saved.append(facts(written))
        logger.info("file_save count=%d", len(saved))
        return {"saved": saved}

    @mcp.tool(name="file_remove", meta=FOR_THE_MODEL)
    async def file_remove(ctx: Context, path: str) -> dict[str, Any]:
        """Delete a saved file, and everything that made it findable.

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
            per file — and `total`, how many files match the filters, so
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

    @mcp.tool(name="report_write", meta=FOR_THE_MODEL)
    async def report_write(
        ctx: Context,
        title: str,
        content: str,
        tags: str = "",
        period_start: str = "",
        period_end: str = "",
    ) -> dict[str, Any]:
        """Create a report under a title.

        A title that already has a report is refused rather than overwritten —
        read it with `report_read`, and change it with `report_update`.

        Args:
            title: What it is a report of. This is its key.
            content: The report.
            tags: Comma-separated words to file it under.
            period_start: The first day the report covers, `YYYY-MM-DD`.
            period_end: The last day it covers.

        Returns:
            The title as it is stored — `kind`, `key`, `title`, `time`, `tags`,
            `summary`, `period_start` and `period_end`.  `time` is when the
            report was first written.
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
        if row is None:
            raise ToolError(
                f"there is already a report called {title!r} — read it with "
                f"report_read, or replace it with report_update."
            )
        logger.info("report_write title=%s", title)
        return facts(row)

    @mcp.tool(name="report_read", meta=FOR_THE_MODEL)
    async def report_read(ctx: Context, title: str) -> dict[str, Any]:
        """Read a report by its title.

        Args:
            title: The report's title.

        Returns:
            The report's `kind`, `key`, `title`, `time`, `tags`, `summary`,
            `period_start` and `period_end`, and the whole of it under `text`.
            `time` is when the report was first written.
        """
        row = await missing_row(
            ctx, REPORT, title, f"there is no report called {title!r}."
        )
        return _document(row)

    @mcp.tool(name="report_update", meta=FOR_THE_MODEL)
    async def report_update(
        ctx: Context,
        title: str,
        content: str,
        tags: str = "",
        period_start: str = "",
        period_end: str = "",
    ) -> dict[str, Any]:
        """Replace a report under a title.

        The whole report becomes what you pass: `content`, `tags` and the
        period are the document, not a change to it — an omitted period is
        cleared.  A title that has no report is refused.

        Args:
            title: The report's title.
            content: The report, whole.
            tags: Comma-separated words to file it under. Empty clears them.
            period_start: The first day it covers, `YYYY-MM-DD`. Empty clears
                it.
            period_end: The last day it covers. Empty clears it.

        Returns:
            The title as it is stored — `kind`, `key`, `title`, `time`, `tags`,
            `summary`, `period_start` and `period_end`.
        """
        opened = await cabinet(ctx)
        row = await opened.write(
            REPORT,
            title,
            mode=REPLACE,
            body=content,
            tags=parse_tags(tags),
            period_start=period_start,
            period_end=period_end,
            embedder=await embedding.get(),
        )
        if row is None:
            raise ToolError(f"there is no report called {title!r}.")
        logger.info("report_update title=%s", title)
        return facts(row)

    @mcp.tool(name="report_remove", meta=FOR_THE_MODEL)
    async def report_remove(ctx: Context, title: str) -> dict[str, Any]:
        """Delete a report and everything filed under its title.

        Args:
            title: The report's title.

        Returns:
            `kind`, `key` and `removed` — always `true`, because a title with
            no report is refused rather than answered.
        """
        await remove_of(ctx, REPORT, title, f"there is no report called {title!r}.")
        logger.info("report_remove title=%s", title)
        return {"kind": REPORT, "key": title, "removed": True}

    @mcp.tool(name="report_list", meta=FOR_THE_MODEL)
    async def report_list(
        ctx: Context,
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List reports, newest first, by when each was first written.

        Replacing a report does not move it: the order is when the report was
        first written, not the last time it was updated.

        Args:
            since: Only reports first written at or after this.
            until: Only reports first written at or before this.
            limit: How many reports this page may hold.
            offset: How many reports to skip — that is how you page back.

        Returns:
            `entries` — `kind`, `key`, `title`, `time`, `tags`, `summary` and
            `excerpt` per report — and `total`, how many reports match the
            filters, so `offset + len(entries) < total` says whether more
            remain.
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
        """Read a tool output that was saved whole, by finding — not by scrolling.

        A result too large for the conversation is shortened for you and kept
        whole here; the marker standing in for it names the call. This is the way
        back to the part the shortening did not carry, and it is a *searching*
        reader: name what you are looking for and how much of what surrounds it,
        or name the line you already know and read from there. The lines are
        numbered, a matched line is marked `:` and the lines around it `-`, and
        the numbers are the ones the marker beside the result shows.

        Args:
            call: The tool call id, from the marker in the conversation.
            offset: The first line to read; `1` is the first and `-1` the last.
            limit: The most lines to show. `0` shows as many as fit.
            pattern: Show the lines containing this text, matched literally and
                ignoring case.
            context: With a pattern, how many lines either side of a match to
                show with it.

        Returns:
            `total`, how many lines the output has; `text`, the window, each line
            prefixed with its own number; `next`, the `offset` of the window
            after this one — absent when there is nothing further to read of what
            you asked for; and `matched`, how many lines at or after `offset`
            hold `pattern`, which is there only when you gave one, and is `0`
            with an empty window when nothing matched.
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

        **The row is not in the cabinet's corpus.**  Its address is the call id
        in the turn's `[DUMP:…]` marker, and `dump_read` is the reader, so no
        keyword entry and no vector are written: a search is not how a dump is
        found, and machine output ranked against a person's notes is the
        dilution the store's docstring names.

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
            mode=UPSERT,
            body=output,
            title=tool,
            source=tool,
            mime=mime,
            embedder=await embedding.get(),
        )
        assert row is not None, "an upsert always writes"
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
    #
    # **The row is not in the corpus.**  The turn's `[Image:{…}]` key is the
    # address and `attach_image` is the reader, so an image is stored here for
    # its bytes and metadata and is never indexed.

    @mcp.tool(name="image_save")
    async def image_save(ctx: Context, url: str) -> dict[str, Any]:
        """Keep one image whole, keyed by its content digest.

        Called by the agent server when a turn is recorded — never by a model.
        A `data:` URI is decoded and written under `pictures/`; an `http(s)`
        URL is kept as its own source and nothing is fetched here.  The row is
        not searched: the turn's `[Image:…]` key is its address, and
        `attach_image` is how the picture comes back.

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
                mode=UPSERT,
                title=mime or "image",
                source=url,
                mime=mime,
                embedder=await embedding.get(),
            )
            assert row is not None, "an upsert always writes"
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
                mode=UPSERT,
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
        assert row is not None, "an upsert always writes"
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

        Words and meaning add up rather than competing: give both for the
        strongest answer, a sentence alone for meaning, or words alone — the
        words are also read by meaning as a sentence made of them. Give neither
        to browse what is there, newest first.

        **A hit need not contain the words.**  Meaning ranks the nearest
        documents whether or not they carry them, and adding a sentence only
        adds hits; `matched_in` says when the words were found, and
        `similarity` says how close a meaning hit came. There is no exact-only
        mode, and an empty page means the filters left nothing to rank — not
        that nothing was close.

        A dump and an image are not searched. A dump is read with `dump_read`
        by the call id its `[DUMP:…]` marker names, and an image comes back with
        `attach_image` from its `[Image:…]` key.

        Args:
            keywords: Words to look for — a path like `files/docs/spec.pdf`
                works here. They rank documents that carry them first, but
                they are not a filter: with no sentence, the same words are
                also read by meaning, so a hit may be a document that does not
                contain them.
            sentences: What you are looking for, asked normally. One per
                question you have.
            kind: `note`, `diary`, `file` or `report` — to look in one of them.
                Empty searches all four. A dump or an image has no search
                entry; address it with its own reader instead.
            since: Only documents from this time on.
            until: Only documents up to this time.
            limit: How many hits this page may hold.

        Returns:
            `results` — `kind`, `key`, `title`, `time`, `tags`, `summary` and
            `excerpt` per hit, with `similarity` where meaning ranked it and
            `matched_in` (`line`, or `rest` for elsewhere in the document)
            where your words were found — and `browsed`, true when neither was
            given and the page is simply newest first.
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
        except ValueError as exc:
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

        Tags work for any corpus kind. A file has no text of its own, so until
        somebody describes it a search reaches it only by its name and paths; a
        summary is what a search by meaning reads instead.

        A dump and an image are not described: they are not found by a search.
        A dump is read with `dump_read` and an image is attached with
        `attach_image`, both by the address the turn carries.

        Args:
            kind: `note`, `diary`, `file` or `report`.
            key: The document's key — a subject, a date, a cabinet path or a
                title.
            summary: What it is, in words somebody would search with.
            tags: Comma-separated words to file it under.

        Returns:
            The document's own fields.
        """
        opened = await cabinet(ctx)
        wanted = kind.strip()
        if wanted not in DOCUMENT_KINDS:
            raise ToolError(CORPUS_KIND_HINT)
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
        return facts(row)

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
    from its end until it fits, because `total` and the count of what matched
    are part of what a model is handed.  What did not fit is named rather than
    lost: how many lines there are, how many matched, and the offset the next
    window starts at.

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
    grep leg).

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
        return {"total": total, "text": "", "matched": 0}
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


def _diary_key(value: str) -> str:
    """A diary write's key: today when empty, and a day or a refusal.

    One spelling of the rule for `diary_write` and `diary_update` both: a key
    that is not a day cannot sort with the rest, so it is refused before the
    store is asked to hold it.
    """
    day = value.strip() or _today()
    try:
        _day(day)
    except ValueError as exc:
        raise ToolError(f"{day!r} is not a date — write it as `YYYY-MM-DD`.") from exc
    return day


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
