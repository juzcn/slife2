"""slife2-file-tunnel — a public HTTPS URL for a local file.

The plugin's job is the one thing a local path cannot do: a vision model asked
about `D:\\pictures\\cat.png`, or a chat API that takes a URL and nothing else,
cannot read this machine's disk.  So the plugin does three things and they are
the whole of it:

* it holds a **tunnel** — a loopback port, published as an HTTPS URL
  (`providers.py`, and the `file-tunnel:` section says which one),
* it holds a **registry** of what has been exposed: a token per file, pinned to
  the exact file it was minted for,
* it serves the bytes at ``GET /share/{token}``, on a listener of its own that
  is the *only* thing the tunnel forwards to.

**A plugin, by SDD §2.4's test.**  It holds somebody else's connection — a
tunnel process, or an SDK holding an account — so it is a process rather than a
library, for the same reason the model backends are: a library would put the
tunnel in every importer.

**The tunnel is pointed at the share listener, not at the MCP app.**  A tunnel
forwards a port, and every provider here — ngrok, localhost.run, cloudflared —
forwards the whole of it.  A route registered beside the MCP endpoint would
therefore publish the endpoint as well: a stranger who learned the hostname
could call every tool, not only fetch the one file.  So the public port is a
server that answers `/share/{token}` and nothing else, and the MCP app keeps
`servers.file-tunnel.port` to itself.  That is the one structural difference
from every other plugin here.

LLM-visible tools: ``expose_local_image``, ``unexpose_local_image``.
Internal tools (no ``meta``, so the hub withholds them): ``tunnel_status``.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import mimetypes
import os
import re
import secrets
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import uvicorn
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

import slife2.helpers.timeouts as _T
from slife2.libs.host.config import FILE_TUNNEL_SERVER_NAME, Config
from slife2.libs.plugin.audience import FOR_THE_MODEL, request_conversation
from slife2.libs.plugin.protocol import house_server, serve_plugin
from slife2.plugins.file_tunnel.providers import (
    TunnelProvider,
    create_provider,
)

logger = logging.getLogger(__name__)

SERVER_NAME = FILE_TUNNEL_SERVER_NAME

#: This server's key in the config's `servers:` table, and the section it owns.
CONFIG_KEY = "file-tunnel"

INSTRUCTIONS = (
    "A public HTTPS URL for a local file, for callers that cannot read this "
    "machine's disk — a vision model, a chat API that takes links. The file "
    "stays where it is, and the place it is served from is `/share/` on the "
    "tunnel's own address."
)

#: Read in 64 KB chunks, so a large file is not held in memory to be served.
_CHUNK = 65536


# ── What will not be published ───────────────────────────────────────────────
# A share URL is public for as long as the tunnel is up, so a leaked key is a
# home-readable key.  The list covers private-key names, dotenv, and the
# cloud/package credential files that carry a token in plain text.

_CREDENTIAL_NAMES = frozenset(
    {
        ".env",
        "credentials",
        "known_hosts",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        ".npmrc",
        ".pypirc",
        "netrc",
        ".netrc",
    }
)


def is_credential_path(path: Path) -> bool:
    """Whether *path* looks like a private credential that must not be shared.

    Matches the basename and each path fragment, case-insensitively, against a
    small denylist — and `.env.anything`, which is how a project keeps one per
    environment.
    """
    parts = {part.lower() for part in path.parts}
    if parts & _CREDENTIAL_NAMES:
        return True
    name = path.name.lower()
    return name.startswith(".env.")


# ═══════════════════════════════════════════════════════════════════════
# The registry — token -> the exact file it was minted for
# ═══════════════════════════════════════════════════════════════════════


class Revoked(enum.Enum):
    """What `Exposed.revoke` found, so the tool can say which."""

    OK = "ok"
    UNKNOWN = "unknown"
    FOREIGN = "foreign"


class Exposure:
    """One published file: its token, its path, and what it was pinned to.

    ``(dev, ino, size, mtime_ns)`` pins the exact file that was exposed.  The
    serve path re-checks it, so a path that is later replaced — or swapped for
    a symlink to something else — has a different inode or mtime and is refused
    rather than silently serving new content to everyone holding the old link.
    An exposure that could not be pinned at all — the file was absent for the
    moment ``register`` looked — has no baseline to compare against, so it is
    refused too rather than serving whatever turns up at that path.
    """

    __slots__ = ("token", "path", "owner", "dev", "ino", "size", "mtime_ns")

    def __init__(self, token: str, path: str, owner: tuple[str, str] | None) -> None:
        self.token = token
        self.path = path
        self.owner = owner
        self.dev: int | None = None
        self.ino: int | None = None
        self.size: int | None = None
        self.mtime_ns: int | None = None
        try:
            stat = Path(path).stat()
        except OSError:
            # Not present yet.  Registering anyway is the honest reading of the
            # tool's own existence check a moment ago, and a race here is not
            # worth refusing a call over — but the exposure stays *unpinned*,
            # and :meth:`matches` refuses an unpinned one rather than serving
            # whatever later appears at the path (see the comment there).
            return
        self.dev = stat.st_dev
        self.ino = stat.st_ino
        self.size = stat.st_size
        self.mtime_ns = stat.st_mtime_ns

    def matches(self, stat: os.stat_result | None) -> bool:
        """Whether *stat* is still the file this token was minted for.

        ``None`` — the file is not there any more — is not a match, and saying
        so here rather than at each caller is what keeps a path deleted between
        the tool's existence check and this call from raising instead of being
        refused.  Check-then-act always has that window; the only question is
        whether it fails as a sentence or as a traceback.
        """
        if stat is None:
            return False
        if self.ino is None:
            # Nothing was pinned: the file was absent when this link was minted
            # (`__init__` swallowed the `OSError`).  "Nothing can have changed"
            # is the tempting reading and it is the wrong one — with no baseline
            # there is nothing to compare, so *whatever* sits at this path now
            # is not provably the file the caller asked to publish.  A path that
            # was empty at mint time and has bytes in it at serve time is
            # exactly the "swapped underneath a link" swap this pin exists to
            # refuse, so an unpinned exposure never serves.
            return False
        return (
            stat.st_dev == self.dev
            and stat.st_ino == self.ino
            and stat.st_size == self.size
            and stat.st_mtime_ns == self.mtime_ns
        )

    @property
    def name(self) -> str:
        """The file's basename, for a sentence about it."""
        return Path(self.path).name


class Exposed:
    """The tokens this process has handed out, and the files behind them.

    **One map for the whole process, not one per agent**, and that is
    deliberate rather than an oversight.  One process serves every agent — but
    the thing being addressed is `GET /share/{token}`, and a third party
    fetching that URL says nothing about who they are.  The token *is* the
    capability, so the map is keyed by it and cannot be partitioned by a caller
    that the route never sees.

    What is held per agent is the *owner*, which is what lets one conversation
    refuse to revoke another's link (see :meth:`revoke`) — the one place the
    identity is available and can be made to matter.
    """

    def __init__(self) -> None:
        self._by_token: dict[str, Exposure] = {}
        self._by_path: dict[str, str] = {}

    def register(self, path: str, owner: tuple[str, str] | None) -> Exposure:
        """Publish *path*, reusing its token when the file has not changed.

        The token is 30 hex characters (``secrets.token_hex(15)``) —
        deliberately under 32, which is the length a generic redactor treats as
        a secret, and hex rather than base64url so nothing in it breaks the URL
        detection of whatever is drawing the answer.

        A path that already has a token whose pin no longer matches gets a NEW
        one: otherwise the caller would be handed the same URL that now answers
        403 to everyone holding it.
        """
        existing = self._by_path.get(path)
        if existing is not None:
            found = self._by_token.get(existing)
            if found is not None and found.matches(_stat(path)):
                return found
            # Stale: the file changed since this link was minted.  Revoke and
            # mint again, so re-exposing actually re-exposes.
            self._drop(existing)

        token = secrets.token_hex(15)
        exposure = Exposure(token, path, owner)
        self._by_token[token] = exposure
        self._by_path[path] = token
        logger.debug("exposed path=%s token=%s", path, token)
        return exposure

    def lookup(self, token: str) -> Exposure | None:
        """The exposure behind *token*, or ``None`` if this process never
        minted it.
        """
        return self._by_token.get(token)

    def revoke(self, token: str, owner: tuple[str, str] | None) -> Revoked:
        """Withdraw *token*, unless it belongs to somebody else.

        An exposure made by a call that named no conversation has no owner and
        anyone may revoke it.  One that named a conversation may be revoked by
        that conversation and no other: the tokens are in one map, so without
        this rule any agent could kill any other's link.
        """
        found = self._by_token.get(token)
        if found is None:
            return Revoked.UNKNOWN
        if found.owner is not None and found.owner != owner:
            return Revoked.FOREIGN
        self._drop(token)
        return Revoked.OK

    def _drop(self, token: str) -> None:
        found = self._by_token.pop(token, None)
        if found is not None:
            self._by_path.pop(found.path, None)

    def __len__(self) -> int:
        return len(self._by_token)


def _stat(path: str) -> os.stat_result | None:
    """``stat`` for *path*, or ``None`` — a missing file is not an error here."""
    try:
        return Path(path).stat()
    except OSError:
        return None


# ═══════════════════════════════════════════════════════════════════════
# Serving one file
# ═══════════════════════════════════════════════════════════════════════


def content_disposition(filename: str) -> str:
    """RFC 5987 ``Content-Disposition`` for a (possibly) non-ASCII filename.

    HTTP header values are Latin-1.  For a filename outside it we emit an
    ASCII fallback in ``filename=`` plus the real name percent-encoded in
    ``filename*=UTF-8''…`` — without which Starlette raises ``UnicodeEncodeError``
    while writing the header, and a file named in Chinese answers 500.
    """
    try:
        filename.encode("latin-1")
    except UnicodeEncodeError:
        ascii_fallback = re.sub(r"[^ -~]", "_", filename)
        ascii_fallback = ascii_fallback.replace('"', "_").replace("\\", "_")
        ascii_fallback = ascii_fallback[:80] or "file"
        return (
            f"inline; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"
        )
    safe = filename.replace('"', "_").replace("\\", "_")
    return f'inline; filename="{safe}"'


def _content_type(path: Path) -> str:
    if not mimetypes.inited:
        mimetypes.init()
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _chunks(path: Path):
    """Yield *path* in chunks, closing the handle however the reader stops."""
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            yield chunk


async def serve_exposed(exposure: Exposure) -> Response:
    """The body of ``GET /share/{token}``, given what the token addresses.

    Split out from the route so it can be driven directly: the interesting
    answers (403 for a file replaced since it was pinned, 404 for one deleted,
    a chunked 200) are decisions about *what* to serve, and they cost nothing
    to test without a tunnel or a socket.
    """
    path = Path(exposure.path)
    stat = _stat(exposure.path)
    if stat is None:
        return Response("The file is no longer there.", status_code=404)
    if not path.is_file():
        return Response("The file is no longer there.", status_code=404)
    if not exposure.matches(stat):
        logger.warning(
            "share_changed_since_exposed token=%s path=%s — refusing",
            exposure.token,
            path,
        )
        return Response(
            "The file changed since this link was made. Expose it again for "
            "the current one.",
            status_code=403,
        )

    content_type = _content_type(path)
    logger.info(
        "share_served path=%s mime=%s size=%s", path, content_type, stat.st_size
    )
    return StreamingResponse(
        _chunks(path),
        status_code=200,
        headers={
            "Content-Type": content_type,
            "Content-Length": str(stat.st_size),
            "Content-Disposition": content_disposition(path.name),
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# The listener the tunnel is pointed at
# ═══════════════════════════════════════════════════════════════════════


def share_app(exposed: Exposed) -> Starlette:
    """The whole public surface: ``GET /share/{token}`` and nothing else.

    **An app of its own rather than a route on the MCP app.**  A tunnel
    forwards a port, so whatever that port answers is what the hostname
    publishes; mounting this route beside the MCP endpoint would publish the
    endpoint too.  The tunnel is pointed here instead, and the MCP app is not
    reachable through it at all.

    Split from the listener for the same reason `serve_exposed` is split from
    the route: the interesting answers are decisions about *what* to serve,
    and they cost nothing to test without a socket.
    """

    async def share(request: Request) -> Response:
        """Serve an exposed file to whoever holds its token."""
        token = request.path_params["token"]
        found = exposed.lookup(token)
        if found is None:
            return Response("Unknown share link.", status_code=403)
        return await serve_exposed(found)

    return Starlette(routes=[Route("/share/{token}", share, methods=["GET"])])


class ShareListener:
    """A loopback HTTP server for :func:`share_app`, on an ephemeral port.

    The tunnel needs a port, and it must not be the MCP server's.  Binding
    ``127.0.0.1:0`` lets the OS pick one; this app is the only thing that port
    ever answers, so "the tunnel publishes the share route only" is a property
    of what is listening rather than of a filter that would have to keep pace
    with every route the MCP app grows.

    It runs as a task in the plugin's loop, the way the MCP server's uvicorn
    does, so no second thread and no second event loop are involved.  Uvicorn
    logs through the root handler (`log_config=None`), which is where the
    house formatter masks credentials; a served file is already named by
    `share_served`, and the house pins `uvicorn.access` above DEBUG, so no
    per-request line is added.
    """

    def __init__(self, app: Starlette) -> None:
        self._app = app
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._sock: socket.socket | None = None
        self._port: int | None = None

    @property
    def port(self) -> int:
        """The bound port — the address the tunnel is pointed at."""
        if self._port is None:
            raise RuntimeError("the share listener is not running")
        return self._port

    async def start(self) -> int:
        """Bind the loopback port, serve on it, and answer with the port.

        The socket is bound and listening here rather than by uvicorn, so the
        port is known before the server starts — the tunnel is handed an
        address that is already accepting connections, not one that will
        exist momentarily.
        """
        if self._port is not None:
            return self._port

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = int(sock.getsockname()[1])
        config = uvicorn.Config(
            self._app,
            host="127.0.0.1",
            port=port,
            log_config=None,  # the root handler is the masking point
            lifespan="off",  # this app has nothing to start or stop
            # **No `access_log=False`.**  That flag mutates the process-wide
            # `uvicorn.access` logger (handlers and propagation), which is the
            # MCP server's too.  The house already pins that logger above
            # DEBUG, so a share request leaves `share_served` as its line and
            # nothing here changes the logger the host runs on.
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(
            server.serve(sockets=[sock]), name="file-tunnel-share"
        )

        deadline = time.monotonic() + _T.timeouts.ready.connect_ours
        while not server.started:
            if task.done():
                sock.close()
                error = task.exception()
                raise RuntimeError(
                    f"the share listener could not start: {error!r}"
                ) from error
            if time.monotonic() >= deadline:
                task.cancel()
                sock.close()
                raise RuntimeError(
                    "the share listener did not start within "
                    f"{_T.timeouts.ready.connect_ours:.0f}s"
                )
            await asyncio.sleep(_T.timeouts.pacing.lock_poll)

        self._server, self._task, self._sock, self._port = server, task, sock, port
        logger.info("share_listener_started url=http://127.0.0.1:%d", port)
        return port

    async def stop(self) -> None:
        """Ask the listener to exit and wait for it, bounded at every step."""
        server, task, sock = self._server, self._task, self._sock
        self._server = self._task = self._sock = None
        self._port = None
        if server is None or task is None:
            if sock is not None:
                sock.close()
            return

        server.should_exit = True
        _, pending = await asyncio.wait({task}, timeout=_T.timeouts.grace.drain)
        if pending:
            # Uvicorn's own graceful window did not end it — a connection it
            # was waiting on, or a shutdown that was never woken.
            server.force_exit = True
            _, pending = await asyncio.wait({task}, timeout=_T.timeouts.grace.drain)
        if pending:
            task.cancel()
            logger.warning("share_listener_stop_timeout")
        if sock is not None:
            sock.close()


# ═══════════════════════════════════════════════════════════════════════
# The plugin
# ═══════════════════════════════════════════════════════════════════════


def build_server(config: Config, *, provider: TunnelProvider | None = None) -> FastMCP:
    """Build the file-tunnel server.

    `provider` is the seam, and it is the one worth having: every test of the
    two tools drives a fake tunnel, so what is under test is the *tool* half
    (which file, which token, what the far end is told) with no tunnel, no
    credential and no network anywhere.  The providers' own shapes are tested
    against fake subprocesses in
    `tests/plugins/file_tunnel/test_file_tunnel_providers.py`.

    **The tunnel is pointed at the share listener, not at this server.**  The
    MCP address from `servers:` is where slife2's own clients connect; it must
    not be what the public hostname forwards to.  The listener binds a
    loopback port of its own when the plugin starts, and the tunnel takes that
    port — so `/mcp` is not on the public surface, and the `--port` override
    moves the MCP server without moving the tunnel.
    """
    exposed = Exposed()
    tunnel = provider or create_provider(
        config.file_tunnel.active,
        config.file_tunnel.options_for(config.file_tunnel.active),
    )
    share = ShareListener(share_app(exposed))

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        """Start the share listener and the tunnel; stop both on the way out.

        **The listener comes first, because the tunnel is pointed at it.**
        It is local and must come up: a listener that cannot bind fails the
        plugin's start rather than letting a tunnel publish a wider surface
        than the one file an exposure means.

        The tunnel's start is a task and is never awaited here.  A tunnel
        takes seconds (an ngrok session, an SSH handshake, a cloudflared
        registration) and the plugin must not be slower to load for it — the
        monitor that follows picks up anything the eager attempt did not
        finish, so a failed start costs a log line rather than the plugin.
        """
        port = await share.start()
        eager = asyncio.create_task(_start(tunnel, port, why="eager"))
        tunnel.start_monitor(port)
        try:
            yield {}
        finally:
            tunnel.stop_monitor()
            eager.cancel()
            try:
                # `stop` kills a child and waits for it — up to the kill timeout.
                await asyncio.to_thread(tunnel.stop)
            finally:
                # The listener goes even if the tunnel's stop was cancelled:
                # a socket left bound would outlive the state that explains it.
                await share.stop()

    mcp: FastMCP = house_server(
        SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan
    )

    # ── For the model ────────────────────────────────────────────────────

    @mcp.tool(name="expose_local_image", meta=FOR_THE_MODEL)
    async def expose_local_image(ctx: Context, path: str) -> dict[str, Any]:
        """Give a local file a public HTTPS URL, so something that cannot read
        this machine's disk can fetch it — a vision model asked about a
        picture, a chat API that only accepts links.

        The file is not moved or copied: it is served from where it is, for as
        long as slife2 is running. It stops being served the moment the file
        changes, and `unexpose_local_image` stops it sooner — call that when the
        file is no longer wanted out there, and expect the URL to outlive the
        conversation otherwise.

        Args:
            path: The file on this machine, as an absolute path or one
                relative to where slife2 was started.

        Returns:
            `url` — the public link to hand to whoever needs the file — its
            `token`, which is what `unexpose_local_image` takes — and `file`,
            the name being served.
        """
        if not path.strip():
            raise ToolError("no file was named — pass the path of the file to publish.")
        target = Path(path)
        if not target.exists():
            raise ToolError(f"there is no file at {path}.")
        if not target.is_file():
            raise ToolError(f"{path} is a directory, not a file.")
        # **Judge the file that will actually be served, not the name it was
        # asked for.**  The registry stores `str(target.resolve())` and the
        # route serves *that* path, so a symlink named `photo.png` pointing at
        # `~/.ssh/id_rsa` would sail past a denylist check made on the name as
        # given and then hand the private key out under a share URL — exactly
        # the thing the list exists to refuse.  `resolve()` follows the whole
        # chain (and the file is proven to exist just above), so what is judged
        # here is the path the token will actually address.
        served = target.resolve()
        if is_credential_path(served):
            raise ToolError(
                f"{served.name} looks like a private credential "
                f"(a key, a `.env`, a saved password). A share URL is public "
                f"while the tunnel is up, so this would publish it."
            )

        await _ensure(tunnel, share.port)
        if not tunnel.is_active:
            raise ToolError(
                "the tunnel is not up, so there is no public URL to "
                "hand out. `tunnel_status` says what the tunnel is doing."
            )
        # A published URL is not a working one: a transport that lost the edge
        # keeps its URL and answers every request to it with HTTP 530.  Handing
        # that out is handing out a dead link, and the caller finds out later —
        # from a model that could not fetch the file.  Refusing is the honest
        # answer, and the tunnel keeps retrying in the background.
        # **The probe is the provider's own I/O** — a metrics HTTP call for
        # cloudflared, a process check for a CLI child — so it goes off the
        # loop: a probe that accepted but does not answer would otherwise stall
        # every other call this server is answering, including the share a
        # remote model is fetching.
        if not await asyncio.to_thread(tunnel.is_reachable):
            raise ToolError(
                "the tunnel has published a URL but nothing is "
                "reaching it from outside right now — every fetch would answer "
                "HTTP 530. `tunnel_status` has the detail; try again shortly."
            )

        found = exposed.register(str(served), request_conversation(ctx))
        url = tunnel.share_url_for(found.token)
        if url is None:
            raise ToolError("the tunnel went away as the file was published.")
        return {"url": url, "token": found.token, "file": found.name}

    @mcp.tool(name="unexpose_local_image", meta=FOR_THE_MODEL)
    async def unexpose_local_image(ctx: Context, token: str) -> dict[str, Any]:
        """Stop serving a file that `expose_local_image` made public. Its URL
        answers 403 from then on, and the file itself is untouched.

        Args:
            token: The last part of the share URL — the `xxxx` in
                `https://…/share/xxxx`.

        Returns:
            `token` — the link that was withdrawn, whether it arrived as a bare
            token or as a whole share URL. From here that URL answers 403.
        """
        wanted = token.strip().rstrip("/").rsplit("/", 1)[-1]
        if not wanted:
            raise ToolError(
                "no token was given — it is the last part of the share URL."
            )
        outcome = exposed.revoke(wanted, request_conversation(ctx))
        if outcome is Revoked.UNKNOWN:
            raise ToolError(f"no link with the token {wanted!r} is being served.")
        if outcome is Revoked.FOREIGN:
            raise ToolError(
                f"the token {wanted!r} belongs to another conversation's link."
            )
        logger.info("unexposed token=%s", wanted)
        return {"token": wanted}

    # ── Not for the model ────────────────────────────────────────────────
    # No `meta`, so the hub withholds it (slife2/libs/plugin/audience.py).  It is here
    # because the tunnel's state is otherwise invisible: `starting` and
    # `failed` look identical from the outside, and `reachable` is the
    # difference between a URL and a working URL.

    @mcp.tool(name="tunnel_status")
    async def tunnel_status() -> str:
        """The tunnel's live state as JSON facts: `state` (`active` /
        `starting` / `failed` / `idle`), the public `url`, `reachable`,
        the `provider` in use, and — for a CLI tunnel — the `binary` it
        actually spawned, the `edge_ip` it is dialing plus `edge_via_proxy`,
        which is true when a fake-ip proxy is what a flapping tunnel should be
        blamed on.

        `binary` is reported because the provider name does not say which
        program is running: a CLI provider resolves its binary on PATH first and
        then in guessed install locations (`_which`), and which of those won is
        otherwise invisible.
        """
        state = tunnel.status()
        edge_ip = getattr(tunnel, "edge_ip", "")
        if not isinstance(edge_ip, str):
            # A test double or a future transport: report "not observed"
            # rather than serialising something that is not an address.
            edge_ip = ""
        binary = getattr(tunnel, "binary_path", "")
        if not isinstance(binary, str):
            # The same guard, for the same reason: an SDK transport has no
            # binary, and a double may report none — never a non-path.
            binary = ""
        return json.dumps(
            {
                "state": state["state"],
                "url": state.get("url", ""),
                "reason": state.get("reason", ""),
                "reachable": await asyncio.to_thread(tunnel.is_reachable),
                "provider": config.file_tunnel.active,
                "binary": binary,
                "edge_ip": edge_ip,
                "edge_via_proxy": bool(getattr(tunnel, "edge_via_proxy", False)),
                "exposed": len(exposed),
            },
            ensure_ascii=False,
        )

    return mcp


async def _start(tunnel: TunnelProvider, port: int, *, why: str) -> None:
    """Bring the tunnel up off the event loop, reporting a failure rather than
    raising it.

    `start` blocks — an SDK handshake, an SSH connect, a child's first output —
    so it goes through `asyncio.to_thread`.  It is reached from two places that
    must not care whether it works (the lifespan's eager attempt and the tool's
    on-demand one), which is why the failure is a log line and the *state* is
    what callers read.
    """
    try:
        await asyncio.to_thread(tunnel.start, port)
    except Exception as exc:  # noqa: BLE001 — the tunnel's state is the report
        logger.warning("file_tunnel_start_failed why=%s err=%s", why, exc)


async def _ensure(tunnel: TunnelProvider, port: int) -> None:
    """Start the tunnel if it is not up — the on-demand fallback.

    The eager start at load time is the usual path and the monitor is the
    retry; this exists so a first call does not fail during the seconds
    between the plugin being ready and the tunnel being established.

    **Which is why an attempt already in flight is waited out rather than
    returned from.**  The eager start is a task of its own (the lifespan in
    `build_server`), so a first call routinely lands in the middle of it, and
    returning there would hand the caller the very "not up" answer this
    function exists to avoid: a tunnel that answers two seconds later is of no
    use to a call that has already raised.  The wait is bounded by the same
    budget a start attempt gets (`_T.timeouts.ready.tunnel_start`), so a start
    that hangs still ends as an honest "not up" instead of a call that never
    returns.
    """
    if tunnel.is_active:
        return
    deadline = time.monotonic() + _T.timeouts.ready.tunnel_start
    if tunnel.is_starting and await _wait_for_start(tunnel, deadline):
        return
    if tunnel.is_active:
        return  # the in-flight attempt was the answer
    await _start(tunnel, port, why="on-demand")
    # **The guard can be taken between the check above and `start`'s own
    # lock.**  A start that arrives after the monitor's attempt has begun is
    # refused as "already in progress"; that is not this call's attempt failing,
    # so wait for the one that won rather than reporting a tunnel that is
    # seconds from publishing as down.
    if tunnel.is_starting:
        await _wait_for_start(tunnel, deadline)


async def _wait_for_start(tunnel: TunnelProvider, deadline: float) -> bool:
    """Wait out a start already in flight; True when the budget ran out."""
    while tunnel.is_starting and not tunnel.is_active:
        if time.monotonic() >= deadline:
            # The attempt outlived its own budget — do not start a second one
            # on top of it (the provider would refuse it as "already in
            # progress" and log a warning for a start that is not broken).
            logger.warning(
                "file_tunnel_start_still_in_flight timeout=%.0fs",
                _T.timeouts.ready.tunnel_start,
            )
            return True
        await asyncio.sleep(_T.timeouts.pacing.tunnel_retry_delay)
    return False


def main(argv: list[str] | None = None) -> int:
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        build=build_server,
        logger=logger,
        note=lambda config: (
            f"exposing through {config.file_tunnel.active}"
            if config.file_tunnel.providers
            else f"no `{CONFIG_KEY}:` section — using {config.file_tunnel.active}"
        ),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
