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
(`[DUMP:…]` → `dump_read`/`dump_grep`, `[Image:…]` → `attach_image`), so they
are stored here and are not indexed.  What differs per kind is what a *document* is, and
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
`file_read` windows the decoded *text* — a binary file's escaped rendering —
`file_grep` searches the same text, and the path to the bytes is what every
other tool takes.

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

* **There is no corpus-wide `grep` leg.**  The cabinet's search ranks notes,
  days, files and reports by words and meaning; `dump_grep` finds text in one
  saved output by a literal match, which is a reader's address rather than a
  second search engine (SDD §14.2).
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
import codecs
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import urllib.parse
from collections.abc import Iterable, Iterator, Sequence
from datetime import date as _date
from pathlib import Path
from typing import Any, Literal

import httpx2
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from slife2.clients.embedder import EmbedderConnection
from slife2.helpers import timeouts as _T
from slife2.helpers.threads import off_loop
from slife2.libs.host.config import CABINET_SERVER_NAME, Config
from slife2.libs.host.paths import home
from slife2.libs.plugin.audience import FOR_THE_MODEL
from slife2.libs.plugin.protocol import client_reader, house_server, serve_plugin
from slife2.libs.store.index import Embedder
from slife2.libs.turn.tokens import estimate_text_tokens
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
    accessible_path,
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
    "note, a day and a report share the same verbs: write creates, read reads, "
    "update replaces the whole document, remove deletes it, and list pages "
    "them — `note_write`/`note_read`/`note_update`/`note_remove`/`note_list`, "
    "and the same with `diary_` and `report_` in place of `note_`. `file_save` "
    "copies a local file or fetches an `http(s)` URL into the cabinet; "
    "`file_read` and `file_grep` read and search it in the same "
    "line-or-character units, `file_remove` deletes it, and `file_list` pages "
    "it. `cabinet_search` ranks all four kinds by words and by meaning, and "
    "`cabinet_summarize` describes one so it can be found. Every answer about "
    "a document carries `path`, the file relative to your working directory, "
    "so you can hand it to a tool of your own (`pandoc`, `rg`, …). A tool "
    "output the harness kept whole is read with `dump_read` by the call id in "
    "its `[DUMP:…]` marker, in line or character units, and searched with "
    "`dump_grep`; an image a turn carried comes back with `attach_image` from "
    "its `[Image:…]` key."
)

#: How much of a file is sniffed for the two things that settle its encoding
#: before anything is decoded: a BOM, and a NUL byte.
SNIFF_BYTES = 8192

#: Where a saved file goes, by what it is.  Auto-filing by extension is
#: deliberate: the cabinet is browsed by a person, and `images/` is a
#: directory they can find something in.  An extension nobody here knows lands in
#: `other/`, which is a name that says exactly that rather than guessing.
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
#: and this process's memory, and a saved file is a file rather than the window
#: a reader hands back.
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
            f"{url!r} names this machine — a fetched URL may only reach the internet."
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
                            raise ToolError(f"{current} redirected without a location.")
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


def _local_path(entry: str) -> Path:
    """One local source path, as the caller's own working directory spells it.

    **A relative path is the model's**, and its working directory is the data
    directory — where its own tools run, and where a path a tool printed is
    relative to.  This daemon's working directory is `runtime/`, so resolving a
    relative path here would name a different file, or none at all.
    """
    path = Path(entry)
    return path if path.is_absolute() or path.drive else home() / path


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


#: A byte as a reader sees it in a file that is not text: printable ASCII and
#: tab/newline/carriage-return survive, every other byte becomes `\xNN`.  The
#: mapping is one entry per byte, so a sentence inside a binary is still
#: findable by `file_grep`, and the rest is visibly not text.
_BINARY_CHARS = {
    byte: chr(byte)
    if (byte in (9, 10, 13) or 0x20 <= byte <= 0x7E)
    else f"\\x{byte:02x}"
    for byte in range(256)
}


def _binary_text(data: bytes) -> str:
    """A non-text file as bytes a reader can search and read.

    `latin-1` maps each byte to one character without guessing an encoding, and
    `translate` turns the non-printable ones into `\\xNN`.  The result is not a
    decode of anything — it is the bytes, shown.
    """
    return data.decode("latin-1").translate(_BINARY_CHARS)


def _head_encoding(head: bytes) -> str:
    """The encoding a file's first block says it is, or `"binary"`.

    **"No text" is not the same question as "is it UTF-8"**, and reading it as
    one refuses text that plainly exists — a `.txt` written by cmd is GBK, and
    one redirected by PowerShell is UTF-16LE.  So the order is: a BOM settles it
    before anything else (UTF-16LE's BOM cannot be mistaken for a PDF, and UTF-8
    with a BOM is still UTF-8); then a NUL is the tell that the bytes are not
    text — a PDF, an image, an archive; then UTF-8 and GB18030 are tried with an
    incremental decoder, so a character cut at the read boundary is not mistaken
    for a decode failure.

    **One sniff for both readers**: `decode` hands it the whole file's head and
    `_file_encoding` hands it the file's own first block, so a file is read as
    the same text whether it is loaded or streamed.
    """
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if b"\x00" in head:
        return "binary"
    for encoding in ("utf-8", "gb18030"):
        try:
            codecs.getincrementaldecoder(encoding)().decode(head, final=False)
            return encoding
        except UnicodeDecodeError:
            continue
    return "binary"


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
    # read by address instead: a dump by `dump_read` or `dump_grep`, an image
    # by `image_source`.  What differs between the kinds is the key, the sentence
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
    # path, the dump readers read a window) keep their own body and say so by
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

    async def dump_target(ctx: Context, call: str) -> Path:
        """The file a dump's call id names.

        **A dump's text is in its file, not in SQLite.**  The row is the
        address — the call id and the mirror path — and the file is the one
        copy of the output.  So this resolves the mirror and checks that it is
        still there, and the readers stream it like any other text file.
        """
        opened = await cabinet(ctx)
        row = await missing_row(
            ctx, DUMP, call, f"there is no saved output for the call {call!r}."
        )
        mirror = str(row.get("mirror") or "")
        if not mirror:
            raise ToolError(f"the output for the call {call!r} has no file to read.")
        try:
            target = resolve(opened.root, mirror)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        if not target.is_file():
            raise ToolError(
                f"the output for the call {call!r} is recorded but its file is gone."
            )
        return target

    async def document_text(ctx: Context, row: dict[str, Any]) -> str:
        """One corpus document's text, from the file its row points at.

        A note, a day and a report are one copy on disk; the row carries the
        metadata and the address.  An old row written before that still has its
        body and is read from there (`body_of`).
        """
        opened = await cabinet(ctx)
        return await off_loop(opened.body_of, row)

    async def file_target(ctx: Context, path: str) -> Path:
        """The saved file a path names, resolved and checked.

        One definition for both file readers: the row must exist, the path must
        stay inside the cabinet, and the bytes must still be on disk.
        """
        opened = await cabinet(ctx)
        await missing_row(ctx, FILE, path, f"there is no saved file at {path!r}.")
        try:
            target = resolve(opened.root, path)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        if not target.is_file():
            raise ToolError(f"{path!r} is recorded but is not on disk any more.")
        return target

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
            content: The note itself — the whole of its file; the subject is
                not prepended.
            tags: Comma-separated words to file it under.

        Returns:
            The note as it is stored — `kind`, `key`, `title`, `time`, `tags`,
            `summary` and `path`, the file an agent's own tools can open.
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
            content: The note, whole — the whole of its file; the subject is
                not prepended.
            tags: Comma-separated words to file it under. Empty clears them.

        Returns:
            The note as it is stored — `kind`, `key`, `title`, `time`, `tags`,
            `summary` and `path`, the file an agent's own tools can open.
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
            The note's `kind`, `key`, `title`, `time`, `tags`, `summary` and
            `path` — the file an agent's own tools can open — and its whole
            file text under `text`; the subject is the key, not part of the
            text.
        """
        row = await missing_row(
            ctx, NOTE, subject, f"there is no note about {subject!r}."
        )
        return _document(row, text=await document_text(ctx, row))

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
            `path` and `excerpt` per note — and `total`, how many notes match
            the filters, so `offset + len(entries) < total` says whether more
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
        """Create the diary entry for a day.

        One day is one document: a day that already has an entry is refused
        rather than overwritten — read it with `diary_read`, and change it with
        `diary_update`.

        Args:
            content: The entry — the whole of its file; the date is not
                prepended.
            date: The day it is about, `YYYY-MM-DD`. Empty means today.
            tags: Comma-separated words to file it under.

        Returns:
            The day as it is stored — `kind`, `key`, `title`, `time` (the day
            itself), `tags`, `summary` and `path`, the file an agent's own
            tools can open.
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
            The day's `kind`, `key`, `title`, `time`, `tags`, `summary` and
            `path` — the file an agent's own tools can open — and its whole
            file text under `text`; the date is the key, not part of the text.
        """
        row = await missing_row(
            ctx, DIARY, date, f"there is no diary entry for {date}."
        )
        return _document(row, text=await document_text(ctx, row))

    @mcp.tool(name="diary_update", meta=FOR_THE_MODEL)
    async def diary_update(
        ctx: Context, content: str, date: str = "", tags: str = ""
    ) -> dict[str, Any]:
        """Replace one day's diary entry.

        The whole entry becomes what you pass: `content` and `tags` are the
        document, not a change to it.  A day that has no entry is refused —
        `diary_write` creates it.

        Args:
            content: The entry, whole — the whole of its file; the date is not
                prepended.
            date: The day, `YYYY-MM-DD`. Empty means today.
            tags: Comma-separated words to file it under. Empty clears them.

        Returns:
            The day as it is stored — `kind`, `key`, `title`, `time` (the day
            itself), `tags`, `summary` and `path`, the file an agent's own
            tools can open.
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
            `summary`, `path` and `excerpt` per day — and `total`, how many
            days match the filters, so `offset + len(entries) < total` says
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
        """Copy a local file, or fetch a URL, into the cabinet.

        A local file is copied and never moved, so what you point at stays
        where it is. An `http(s)` URL is fetched and stored under the site's
        filename. A search does not read inside the file: it matches the file's
        name, where it came from and any `summary` you write.

        Args:
            paths: Local paths or `http(s)` URLs. A relative path is relative
                to your working directory. Name the files in a directory rather
                than the directory itself.
            title: A name for it. Empty uses the filename.
            tags: Comma-separated words to file it under.
            summary: What the file is, in your words — words a search can
                find it by.
            category: The directory inside the cabinet — `images`, `documents`
                and so on. Empty picks one from the extension.

        Returns:
            `saved` — one row per file, with the fields a listing shows
            (including `path`, the file an agent's own tools can open), so the
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
            local.append(_local_path(entry))
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
                source = _local_path(entry)
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
    async def file_read(
        ctx: Context,
        path: str,
        mode: Literal["line", "char"] = "line",
        offset: int | None = None,
        limit: int = 0,
    ) -> dict[str, Any]:
        """Read a saved file's text by line or by character.

        A line-organized file is read by line number; a file with no useful
        lines — minified JSON, one long record — is read by character offset. A
        PDF, an image or an archive has no text: its bytes are shown as an
        escaped rendering, so you can see what it is. Text in another encoding
        reaches you decoded.

        Args:
            path: The saved file's path inside the cabinet.
            mode: `"line"` reads lines; `"char"` reads characters.
            offset: The first line (1-based, negative from the end) or the
                first character (0-based, negative from the end). The first
                line or character when omitted.
            limit: The most lines or characters to show. `0` shows as many as
                fit.

        Returns:
            `chars`, how many characters the whole text has; `lines`, how many
            lines it has (line mode only — that mode already splits it);
            `text`, the window, each line prefixed with its own number in line
            mode; `path`, the file relative to the data directory, so an
            agent's own tools can open it; and `next`, the offset the following
            window starts at — in the same units as `offset` — absent when this
            window reaches the end.
        """
        target = await file_target(ctx, path)
        return _stream_read(
            target,
            where=accessible_path(target),
            mode=mode,
            offset=offset,
            limit=limit,
            budget=config.tool_result.tokens,
        )

    @mcp.tool(name="file_grep", meta=FOR_THE_MODEL)
    async def file_grep(
        ctx: Context,
        path: str,
        pattern: str,
        offset: int = 1,
        limit: int = 0,
    ) -> dict[str, Any]:
        """Find text in a saved file.

        The pattern is matched literally and ignoring case within one line;
        every match answers with both addresses — the line and the character
        offset — so either read mode can continue from it.

        Args:
            path: The saved file's path inside the cabinet.
            pattern: The text to find.
            offset: The first line to search, 1-based; negative counts from the
                end.
            limit: The most matches to show. `0` shows as many as fit.

        Returns:
            `lines` and `chars`, how many lines and characters the whole text
            has; `matched`, how many lines hold the pattern at or after
            `offset`; `matches`, each with `line`, `char` (the 0-based character
            offset the match starts at) and `text` (the line; `cut` is true when
            only the beginning of a very long line fits); `path`, the file
            relative to the data directory; and `next`, the line of the next
            match when not all of them are shown.
        """
        target = await file_target(ctx, path)
        return _stream_grep(
            target,
            pattern,
            where=accessible_path(target),
            offset=offset,
            limit=limit,
            budget=config.tool_result.tokens,
        )

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
            `time`, `tags`, `summary`, `size`, `mime`, `source`, `path` and
            `excerpt` per file — and `total`, how many files match the filters,
            so `offset + len(entries) < total` says whether more remain.
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
            content: The report — the whole of its file; the title is not
                prepended.
            tags: Comma-separated words to file it under.
            period_start: The first day the report covers, `YYYY-MM-DD`.
            period_end: The last day it covers.

        Returns:
            The title as it is stored — `kind`, `key`, `title`, `time`, `tags`,
            `summary`, `period_start`, `period_end` and `path`, the file an
            agent's own tools can open.  `time` is when the report was first
            written.
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
            `period_start`, `period_end` and `path` — the file an agent's own
            tools can open — and its whole file text under `text`; the title is
            the key, not part of the text.  `time` is when the report was first
            written.
        """
        row = await missing_row(
            ctx, REPORT, title, f"there is no report called {title!r}."
        )
        return _document(row, text=await document_text(ctx, row))

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
            content: The report, whole — the whole of its file; the title is
                not prepended.
            tags: Comma-separated words to file it under. Empty clears them.
            period_start: The first day it covers, `YYYY-MM-DD`. Empty clears
                it.
            period_end: The last day it covers. Empty clears it.

        Returns:
            The title as it is stored — `kind`, `key`, `title`, `time`, `tags`,
            `summary`, `period_start`, `period_end` and `path`, the file an
            agent's own tools can open.
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
            `entries` — `kind`, `key`, `title`, `time`, `tags`, `summary`,
            `path` and `excerpt` per report — and `total`, how many reports
            match the filters, so `offset + len(entries) < total` says whether
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
        mode: Literal["line", "char"] = "line",
        offset: int | None = None,
        limit: int = 0,
    ) -> dict[str, Any]:
        """Read a saved tool output by line or by character.

        A tool output too large for the conversation is saved as a file, and
        the `[DUMP:…]` marker beside the result names the tool call it belongs
        to. This reads any part of it. A line-organized file is read by line
        number; a file with no useful lines — minified JSON, one long record —
        is read by character offset.

        Args:
            call: The tool call id, from the marker in the conversation.
            mode: `"line"` reads lines; `"char"` reads characters.
            offset: The first line (1-based, negative from the end) or the
                first character (0-based, negative from the end). The first
                line or character when omitted.
            limit: The most lines or characters to show. `0` shows as many as
                fit.

        Returns:
            `chars`, how many characters the whole output has; `lines`, how
            many lines it has (line mode only — that mode already splits it);
            `text`, the window, each line prefixed with its own number in line
            mode; `path`, the file relative to the data directory, so an
            agent's own tools can open it; and `next`, the offset the following
            window starts at — in the same units as `offset` — absent when this
            window reaches the end.
        """
        target = await dump_target(ctx, call)
        return _stream_read(
            target,
            where=accessible_path(target),
            mode=mode,
            offset=offset,
            limit=limit,
            budget=config.tool_result.tokens,
        )

    @mcp.tool(name="dump_grep", meta=FOR_THE_MODEL)
    async def dump_grep(
        ctx: Context,
        call: str,
        pattern: str,
        offset: int = 1,
        limit: int = 0,
    ) -> dict[str, Any]:
        """Find text in a saved tool output.

        A tool output too large for the conversation is saved as a file, and
        the `[DUMP:…]` marker beside the result names the tool call it belongs
        to. This finds what a model is looking for in the part the conversation
        did not carry. The pattern is matched literally and ignoring case
        within one line; every match answers with both addresses — the line and
        the character offset — so either read mode can continue from it.

        Args:
            call: The tool call id, from the marker in the conversation.
            pattern: The text to find.
            offset: The first line to search, 1-based; negative counts from the
                end.
            limit: The most matches to show. `0` shows as many as fit.

        Returns:
            `lines` and `chars`, how many lines and characters the whole output
            has; `matched`, how many lines hold the pattern at or after
            `offset`; `matches`, each with `line`, `char` (the 0-based character
            offset the match starts at) and `text` (the line; `cut` is true when
            only the beginning of a very long line fits); `path`, the file
            relative to the data directory; and `next`, the line of the next
            match when not all of them are shown.
        """
        target = await dump_target(ctx, call)
        return _stream_grep(
            target,
            pattern,
            where=accessible_path(target),
            offset=offset,
            limit=limit,
            budget=config.tool_result.tokens,
        )

    @mcp.tool(name="dump_save")
    async def dump_save(
        ctx: Context,
        call: str,
        output: str,
        tool: str = "",
    ) -> dict[str, Any]:
        """Keep one tool output whole, keyed by the call it came from.

        **The harness's call and no model's.**  Nothing here decides whether an
        output is worth keeping or which part of it matters, so this carries no
        audience mark — which keeps it out of the hub's catalogue, and therefore
        out of the model's list and out of `tool_search`.  The harness reaches
        this server **directly**, as a peer, exactly as `slife2-context` reaches a
        model backend; the model's own routes back in are `dump_read` and
        `dump_grep`, which *are* marked and therefore *are* in the catalogue.

        A second call under one key replaces, because the key is one call: a
        re-run writes the same output twice rather than two facts.

        **The text is the mirror file; the row holds no body.**  A dump can be
        a gigabyte of output, and keeping the same bytes in SQLite as well
        would be the duplication this arrangement exists to avoid.

        **The row is not in the cabinet's corpus.**  Its address is the call id
        in the turn's `[DUMP:…]` marker, and `dump_read`/`dump_grep` are the
        readers, so no keyword entry and no vector are written: a search is not
        how a dump is found, and machine output ranked against a person's notes
        is the dilution the store's docstring names.

        Answers with a payload and **refuses by raising**, because its caller is
        the harness and not a model: an answer it cannot read has to be
        distinguishable from one that says nothing went wrong.

        Args:
            call: The tool call id the output came from — the key, and the one
                identifier the conversation and this row both carry.
            output: The whole of what the tool returned.
            tool: The tool's name, so a listing says what produced this.
        """
        opened = await cabinet(ctx)
        row = await opened.write(
            DUMP,
            call,
            mode=UPSERT,
            body=output,
            title=tool,
            source=tool,
            embedder=await embedding.get(),
        )
        assert row is not None, "an upsert always writes"
        written = opened.root / str(row["mirror"])
        # **The size is the file's, not the string's.**  One `stat` is O(1) and
        # is the number a reader sees; encoding the output again to count its
        # bytes would copy every byte of a file that is already on disk.
        size = written.stat().st_size
        logger.info("dump_save call=%s tool=%s bytes=%d", call, tool, size)
        return {
            "kept": call,
            "mirror": str(row["mirror"]),
            "path": accessible_path(written),
            "size": size,
        }

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
        agent-written documents — notes, days and reports — by their whole file
        text, whether or not they carry the words, and adding a sentence only
        adds hits; `matched_in` says when the words were found, and `similarity`
        says how close a meaning hit came. A saved file has no vector: it is
        found by its words (title, path, summary) and never appears as a
        meaning-only hit. There is no exact-only mode, and an empty page means
        the filters left nothing to rank — not that nothing was close.

        A dump and an image are not searched. A dump is read with `dump_read`
        or searched with `dump_grep` by the call id its `[DUMP:…]` marker names,
        and an image comes back with `attach_image` from its `[Image:…]` key.

        Args:
            keywords: Words to look for — a path like `documents/spec.pdf`
                works here. They rank documents that carry them first, but
                they are not a filter: with no sentence, the same words are
                also read by meaning, so a note, day or report hit may not
                contain them. A file hit does.
            sentences: What you are looking for, asked normally. One per
                question you have.
            kind: `note`, `diary`, `file` or `report` — to look in one of them.
                Empty searches all four. A dump or an image has no search
                entry; address it with its own reader instead.
            since: Only documents from this time on.
            until: Only documents up to this time.
            limit: How many hits this page may hold.

        Returns:
            `results` — `kind`, `key`, `title`, `time`, `tags`, `summary`,
            `path` and `excerpt` per hit, with `similarity` where meaning ranked
            it and `matched_in` (`line`, or `rest` for elsewhere in the
            document) where your words were found — and `browsed`, true when
            neither was given and the page is simply newest first.
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

        Tags work for any corpus kind. A note, a day and a report are found by
        their whole file text by meaning; a summary is keyword text for them.
        A saved file has no vector, so its summary and tags are what a search
        has besides its name and paths.

        A dump and an image are not described: they are not found by a search.
        A dump is read with `dump_read` or `dump_grep`, and an image is
        attached with `attach_image`, both by the address the turn carries.

        Args:
            kind: `note`, `diary`, `file` or `report`.
            key: The document's key — a subject, a date, a cabinet path or a
                title.
            summary: What it is, in words somebody would search with.
            tags: Comma-separated words to file it under.

        Returns:
            The document's own fields, including `path`, the file an agent's
            own tools can open.
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
        )
        if row is None:
            raise ToolError(f"there is no {wanted} with the key {key!r}.")
        logger.info("cabinet_summarize kind=%s key=%s", kind, key)
        return facts(row)

    return mcp


def _line_start(offset: int, total: int) -> int:
    """A 1-based line address, negative counting from the end; always a line.

    A negative number is where a log's own summary is, and a reader asking for
    the tail of something it has never read does not know how long it is.
    """
    start = offset if offset > 0 else total + offset + 1
    if start < 1 or start > total:
        raise ToolError(
            f"{offset} is not a line of this text: it has {total}, and 1 to "
            f"{total} (or -1 to -{total}) are the addresses."
        )
    return start


def _char_start(offset: int, total: int) -> int:
    """A 0-based character address, negative counting from the end.

    Zero is the first character, because a character address is an index into
    the file: the count of characters shown is exactly the offset the next read
    starts at, which is the arithmetic the truncation marker hands over.
    """
    start = offset if offset >= 0 else total + offset
    if start < 0 or start >= total:
        raise ToolError(
            f"{offset} is not a character of this text: it has {total}, and "
            f"0 to {total - 1} (or -1 to -{total}) are the offsets."
        )
    return start


def _read_mode(mode: str) -> str:
    """The reader's mode, or the refusal an unknown one earns."""
    wanted = (mode or "line").strip().lower()
    if wanted not in ("line", "char"):
        raise ToolError(
            f"mode must be 'line' or 'char'; got {mode!r}. Use line mode for a "
            f"file organized in lines, char mode for one that is not."
        )
    return wanted


def _line_fill(
    lines: Iterable[tuple[int, int, str]],
    *,
    total_lines: int,
    total_chars: int,
    start: int,
    limit: int,
    budget: int,
    where: str,
) -> dict[str, Any]:
    """Fill a line page from a stream, **never larger than `budget` tokens**.

    The bound is on the whole answer, envelope included: a reader that handed
    back more than the budget would be a tool of ours feeding the harness's own
    truncation, which is one system at both ends of a loop.  The page is filled
    from a line source — an in-memory list for a dump, a file read line by line
    for a saved file — and only the page is held.

    **A line longer than the window is not truncated here.**  Line mode is for
    a text organized in lines; a single line too large for the whole window is
    the case char mode exists for, and the refusal names the character offset
    and the file rather than pretending the line ended.
    """
    kept: list[tuple[int, str]] = []
    next_line: int | None = None
    first_offset = 0
    for number, offset, text in lines:
        if number < start:
            continue
        if number == start:
            first_offset = offset
        if limit and len(kept) >= limit:
            next_line = number
            break
        kept.append((number, text))
        following = number + 1 if number < total_lines else None
        if (
            estimate_text_tokens(
                _wire(_line_payload(kept, total_lines, total_chars, following, where))
            )
            > budget
        ):
            kept.pop()
            next_line = number
            break
        next_line = following
    if not kept:
        raise ToolError(
            f"line {start} is longer than a {budget}-token window on its own — "
            f'read it with mode="char", offset={first_offset} '
            f"(the file is at {_where(where)})."
        )
    return _line_payload(kept, total_lines, total_chars, next_line, where)


def _line_payload(
    kept: Sequence[tuple[int, str]],
    total: int,
    total_chars: int,
    following: int | None,
    where: str,
) -> dict[str, Any]:
    """Lines as a model reads them: the original numbers, and where to read on.

    The number is the output's line number and not the window's, so the line a
    model copies out of the conversation reaches the same text a later read
    does.  `chars` is carried even though this is a line answer because it is
    what lets the model choose character mode without another call.  `path` is
    the file itself, relative to the data directory, so the model can hand the
    document to a tool of its own instead of paging it.
    """
    width = len(str(kept[-1][0]))
    text = "\n".join(f"{number:>{width}}  {line}" for number, line in kept)
    page: dict[str, Any] = {
        "lines": total,
        "chars": total_chars,
        "text": text,
        "path": where,
    }
    if following is not None:
        page["next"] = following
    return page


def _char_window(
    text: str,
    *,
    index: int,
    base: int,
    total_chars: int,
    limit: int,
    budget: int,
    where: str,
) -> dict[str, Any]:
    """Fit a character window of *text* starting at *index*, within *budget*.

    `base` is the absolute offset of `text[0]` in the text a model addresses:
    `0` for a whole dump body, and the file offset of a gathered chunk for a
    saved file.  So `next` is always an address in the original, not in the
    chunk.  **The window is filled against the budget, and no line is
    scanned** — this is the mode for text without useful lines.
    """
    room = len(text) - index
    if limit:
        room = min(room, limit)

    def page_of(count: int) -> dict[str, Any]:
        piece = text[index : index + count]
        page: dict[str, Any] = {
            "chars": total_chars,
            "text": piece,
            "path": where,
        }
        if base + index + count < total_chars:
            page["next"] = base + index + count
        return page

    low, high = 0, room
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text_tokens(_wire(page_of(middle))) <= budget:
            low = middle
        else:
            high = middle - 1
    count = low
    page = page_of(count)
    while count > 0 and estimate_text_tokens(_wire(page)) > budget:
        count -= max(count // 16, 1)
        page = page_of(count)
    if count == 0:
        raise ToolError(
            f"a {budget}-token window cannot hold a character of this text — "
            f"raise `tool_result.tokens`; the file is at {_where(where)}."
        )
    return page


def _grep_pattern(pattern: str) -> str:
    """The folded pattern, or the refusal an empty one earns."""
    if not pattern:
        raise ToolError("pattern must be the text to find; give a word or a phrase.")
    return pattern.casefold()


def _grep_hits(
    lines: Iterable[tuple[int, int, str]], wanted: str, start: int
) -> Iterator[tuple[int, int, str]]:
    """Every match in *lines* at or after line *start*, with both addresses.

    **The matching is the same for a dump and a file.**  It reads a line source,
    not a body: both readers feed lines as they are read from the file.  The
    folded index can be larger than
    the line's when casefolding expands a character (`ß` -> `ss`); clamping keeps
    the address inside the line whose text it belongs to.
    """
    for number, offset, line in lines:
        if number < start:
            continue
        found = line.casefold().find(wanted)
        if found >= 0:
            yield number, offset + min(found, len(line)), line


def _grep_payload(
    total_lines: int,
    total_chars: int,
    matched: int,
    shown: Sequence[dict[str, Any]],
    first_unshown: int | None,
    where: str,
) -> dict[str, Any]:
    """One grep answer, before it is measured."""
    page: dict[str, Any] = {
        "lines": total_lines,
        "chars": total_chars,
        "matched": matched,
        "matches": list(shown),
        "path": where,
    }
    if first_unshown is not None:
        page["next"] = first_unshown
    return page


def _grep_finalize(
    shown: list[dict[str, Any]],
    first_unshown: int | None,
    matched: int,
    *,
    total_lines: int,
    total_chars: int,
    budget: int,
    where: str,
) -> dict[str, Any]:
    """Trim a page until it fits, keeping the address of what it sheds.

    **A page that still goes over after the scan is trimmed from its end**, and
    the line it sheds becomes `next` — so a model that asked for too much gets
    the start of what it asked for and an address for the rest, never an answer
    over the budget.  A single match too large for the window keeps its two
    addresses and loses text, marked `cut`.
    """
    while True:
        page = _grep_payload(
            total_lines, total_chars, matched, shown, first_unshown, where
        )
        if estimate_text_tokens(_wire(page)) <= budget:
            return page
        if len(shown) > 1:
            dropped = shown.pop()
            first_unshown = dropped["line"]
            continue
        if len(shown) == 1:
            match = shown[0]
            if not match.get("cut"):
                match["cut"] = True
                match["text"] = _fit_match_text(page, match["text"], budget)
                continue
            first_unshown = match["line"]
            shown.clear()
            continue
        raise ToolError(
            f"a {budget}-token window cannot hold this answer — raise "
            f"`tool_result.tokens`."
        )


def _grep_answer(
    hits: Iterable[tuple[int, int, str]],
    *,
    total_lines: int,
    total_chars: int,
    limit: int,
    budget: int,
    where: str,
) -> dict[str, Any]:
    """The page over *hits*, never larger than *budget*.

    **The one page builder behind both greps.**  The hits may come from a dump
    already in memory or from a file read line by line; the scan stops showing
    once the page is full and keeps counting, so `matched` is a fact about the
    whole text and the memory held is the page, not the file.
    """
    limit = max(limit, 0)
    shown: list[dict[str, Any]] = []
    first_unshown: int | None = None
    matched = 0
    full = False
    for number, at, line in hits:
        matched += 1
        if full or (limit and len(shown) >= limit):
            if first_unshown is None:
                first_unshown = number
            continue
        shown.append({"line": number, "char": at, "text": line})
        if (
            estimate_text_tokens(
                _wire(
                    _grep_payload(total_lines, total_chars, matched, shown, None, where)
                )
            )
            > budget
        ):
            if len(shown) == 1:
                # Keep the only address there is; the finalizer cuts its text.
                full = True
            else:
                shown.pop()
                first_unshown = number
                full = True
    return _grep_finalize(
        shown,
        first_unshown,
        matched,
        total_lines=total_lines,
        total_chars=total_chars,
        budget=budget,
        where=where,
    )


def _file_encoding(target: Path) -> str:
    """The decoder a saved file's bytes were written in, or `"binary"`.

    The same `_head_encoding` a loaded read uses, over the file's own first
    block: one answer about what the file's text is, for both readers.
    """
    with target.open("rb") as handle:
        return _head_encoding(handle.read(SNIFF_BYTES))


def _file_lines(
    target: Path, encoding: str
) -> Iterator[tuple[int, int, str, str, int]]:
    """A file's lines, streamed: `(number, start, text, ending, following)`.

    `text` is the line without its ending, `ending` is the line's own ending as
    a reader sees it (`\\n`, `\\r\\n` or `\\r`), `start` is its first character's
    offset in the text a reader sees, and `following` is the offset after the
    ending.  **Nothing holds the whole file**: one line is read, yielded, and
    dropped.
    """
    number = 0
    start = 0
    if encoding == "binary":
        with target.open("rb") as handle:
            for raw in handle:
                number += 1
                if raw.endswith(b"\r\n"):
                    content, raw_ending = raw[:-2], b"\r\n"
                elif raw.endswith((b"\n", b"\r")):
                    content, raw_ending = raw[:-1], raw[-1:]
                else:
                    content, raw_ending = raw, b""
                text = _binary_text(content)
                ending = _binary_text(raw_ending)
                following = start + len(text) + len(ending)
                yield number, start, text, ending, following
                start = following
        return
    with target.open(
        "r", encoding=encoding, errors="backslashreplace", newline=""
    ) as handle:
        for raw in handle:
            number += 1
            if raw.endswith("\r\n"):
                content, ending = raw[:-2], "\r\n"
            elif raw.endswith(("\n", "\r")):
                content, ending = raw[:-1], raw[-1:]
            else:
                content, ending = raw, ""
            following = start + len(content) + len(ending)
            yield number, start, content, ending, following
            start = following


def _file_totals(target: Path, encoding: str) -> tuple[int, int]:
    """How many lines and characters a streamed file has."""
    total_lines = 0
    total_chars = 0
    for number, _start, _text, _ending, following in _file_lines(target, encoding):
        total_lines = number
        total_chars = following
    return total_lines, total_chars


def _file_char_text(target: Path, encoding: str, start: int, want: int) -> str:
    """At most *want* characters of a file's text, from character *start*.

    **Streamed, not sliced from the whole file**: only the lines needed to
    reach the window are decoded, and only the window is kept.  The offset is a
    character address, so reaching it means decoding from the start; the memory
    is the window, the time is where the window is.
    """
    pieces: list[str] = []
    got = 0
    for _number, line_start, text, ending, following in _file_lines(target, encoding):
        if following <= start:
            continue
        unit = text + ending
        local = max(start - line_start, 0)
        take = want - got
        if take <= 0:
            break
        piece = unit[local : local + take]
        if piece:
            pieces.append(piece)
            got += len(piece)
        if got >= want:
            break
    return "".join(pieces)


def _stream_read(
    target: Path,
    *,
    where: str,
    mode: str,
    offset: int | None,
    limit: int,
    budget: int,
) -> dict[str, Any]:
    """A window of a file's text, streamed: one count, then the window.

    **The one reader behind `dump_read` and `file_read`.**  The two differ only
    in how they find the file — a dump's call id or a saved file's cabinet path
    — and not at all in how the window is cut or how the file is read.
    """
    wanted = _read_mode(mode)
    encoding = _file_encoding(target)
    total_lines, total_chars = _file_totals(target, encoding)
    if not total_lines:
        page: dict[str, Any] = {"chars": 0, "text": "", "path": where}
        if wanted == "line":
            page["lines"] = 0
        return page
    limit = max(limit, 0)
    if wanted == "line":
        start = _line_start(1 if offset is None else offset, total_lines)
        lines = (
            (number, line_start, text)
            for number, line_start, text, _ending, _following in _file_lines(
                target, encoding
            )
        )
        return _line_fill(
            lines,
            total_lines=total_lines,
            total_chars=total_chars,
            start=start,
            limit=limit,
            budget=budget,
            where=where,
        )
    start = _char_start(0 if offset is None else offset, total_chars)
    # The window's size in characters is not known until it is measured;
    # gather a generous multiple of the budget (a token is at least one
    # character, and four covers the priciest BPE splits) and let
    # `_char_window` cut it back to what fits.
    room = budget * 4 + 1024
    if limit:
        room = min(room, limit)
    chunk = _file_char_text(target, encoding, start, room)
    return _char_window(
        chunk,
        index=0,
        base=start,
        total_chars=total_chars,
        limit=limit,
        budget=budget,
        where=where,
    )


def _stream_grep(
    target: Path,
    pattern: str,
    *,
    where: str,
    offset: int,
    limit: int,
    budget: int,
) -> dict[str, Any]:
    """The shared grep over a file, streamed: count, then page the hits.

    **The one search behind `dump_grep` and `file_grep`**, just as `_stream_read`
    is the one reader: a dump is a file in the cabinet too, so both scan the
    same way and hold only the page.
    """
    wanted = _grep_pattern(pattern)
    encoding = _file_encoding(target)
    total_lines, total_chars = _file_totals(target, encoding)
    if not total_lines:
        return {
            "lines": 0,
            "chars": 0,
            "matched": 0,
            "matches": [],
            "path": where,
        }
    start = _line_start(offset, total_lines)
    hits = _grep_hits(
        (
            (number, line_start, text)
            for number, line_start, text, _ending, _following in _file_lines(
                target, encoding
            )
        ),
        wanted,
        start,
    )
    return _grep_answer(
        hits,
        total_lines=total_lines,
        total_chars=total_chars,
        limit=limit,
        budget=budget,
        where=where,
    )


def _fit_match_text(page: dict[str, Any], line: str, budget: int) -> str:
    """As much of a very long matched line as fits, with the rest of the page.

    The match keeps its two addresses and loses only text; `cut` is already on
    the match, so a reader can tell a shortened line from one that ended there.
    """
    match = dict(page["matches"][-1])

    def with_text(text: str) -> dict[str, Any]:
        candidate = dict(page)
        candidate["matches"] = [*page["matches"][:-1], {**match, "text": text}]
        return candidate

    low, high = 0, len(line)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text_tokens(_wire(with_text(line[:middle]))) <= budget:
            low = middle
        else:
            high = middle - 1
    text = line[:low]
    while low > 0 and estimate_text_tokens(_wire(with_text(text))) > budget:
        low -= max(low // 16, 1)
        text = line[:low]
    if estimate_text_tokens(_wire(with_text(text))) > budget:
        raise ToolError(
            f"this line is longer than a {budget}-token window even as an "
            f"address; raise `tool_result.tokens`."
        )
    return text


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


def _document(row: dict[str, Any], *, text: str) -> dict[str, Any]:
    """One document as the payload a model reads: its own fields, and its text.

    The text is always the caller's: a note, a day and a report live in their
    file, and the row holds no body.  One key for every kind, because "the text
    of this document" is one question however a kind answers it.
    """
    return {**facts(row), "text": text}


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
