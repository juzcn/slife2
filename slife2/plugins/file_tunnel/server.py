"""slife2-file-tunnel — a public HTTPS URL for a local file.

v1's `sharefile` plugin.  Its job is the one thing a local path cannot do: a
vision model asked about `D:\\pictures\\cat.png`, or a chat API that takes a URL
and nothing else, cannot read this machine's disk.  So the plugin does three
things and they are the whole of it:

* it holds a **tunnel** — this machine's port, published as an HTTPS URL
  (`providers.py`, and the `file-tunnel:` section says which one),
* it holds a **registry** of what has been exposed: a token per file, pinned to
  the exact file it was minted for,
* it serves the bytes at ``GET /share/{token}``, on the same port, because the
  far end fetches that path *through the tunnel*.

**A plugin, by §1.1's own table.**  It holds somebody else's connection — a
tunnel process, or an SDK holding an account — so it is a process rather than a
library, for the same reason the model backends are: a library would put the
tunnel in every importer.

**And it is the first plugin here with a second HTTP route.**  Every other one
serves a single MCP endpoint at `servers.<name>.path`; this one answers
`/share/{token}` beside it, on the same port, registered with FastMCP's
`custom_route` and served by the same `mcp.run(...)` the house `serve()` makes.
Nothing about `mcp_server.py` changes for it — but it is a real difference, and
it is why this plugin cannot be reached by a client that only speaks MCP.

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
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

import slife2.helpers.timeouts as _T
from slife2.libs.host.config import Config
from slife2.plugins.audience import FOR_THE_MODEL, request_client
from slife2.plugins.file_tunnel.providers import (
    TunnelProvider,
    create_provider,
)
from slife2.plugins.protocol import house_server, serve_plugin

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-file-tunnel"

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
# world-readable key.  The list is v1's: private-key names, dotenv, and the
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

    **One map for the whole process, not one per agent**, and that is the
    difference from v1 rather than an oversight.  There, one child process
    served one agent, so a module global *was* that agent's.  Here one process
    serves every agent — but the thing being addressed is `GET /share/{token}`,
    and a third party fetching that URL says nothing about who they are.  The
    token *is* the capability, so the map is keyed by it and cannot be
    partitioned by a caller that the route never sees.

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
# The plugin
# ═══════════════════════════════════════════════════════════════════════


def build_server(config: Config, *, provider: TunnelProvider | None = None) -> FastMCP:
    """Build the file-tunnel server.

    `provider` is the seam, and it is the one worth having: every test of the
    two tools drives a fake tunnel — v1's do the same by patching a module
    global, which this replaces — so what is under test is the *tool* half
    (which file, which token, what the far end is told) with no tunnel, no
    credential and no network anywhere.  The providers' own shapes are tested
    against fake subprocesses in `tests/test_file_tunnel_providers.py`.

    **The port comes from the config, not from a bound socket.**  v1 bound a
    free port for itself so the tunnel could be pointed at it; here
    `serve_plugin` already knows the address, and the tunnel is pointed at the
    port it says.  What that costs is the `--port` override on *this* plugin:
    it moves the MCP server and leaves the tunnel forwarding to the configured
    port.  Nothing calls it that way, and a tunnel needs an address that is
    written down somewhere either way.
    """
    exposed = Exposed()
    tunnel = provider or create_provider(
        config.file_tunnel.active,
        config.file_tunnel.options_for(config.file_tunnel.active),
    )
    port = config.server(CONFIG_KEY).port

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        """Start the tunnel at startup, and keep it up; stop it on the way out.

        The start is a task and is never awaited here.  A tunnel takes seconds
        (an ngrok session, an SSH handshake, a cloudflared registration) and
        the plugin must not be slower to load for it — the monitor that
        follows picks up anything the eager attempt did not finish, so a
        failed start costs a log line rather than the plugin.
        """
        eager = asyncio.create_task(_start(tunnel, port, why="eager"))
        tunnel.start_monitor(port)
        try:
            yield {}
        finally:
            tunnel.stop_monitor()
            eager.cancel()
            # `stop` kills a child and waits for it — up to the kill timeout.
            await asyncio.to_thread(tunnel.stop)

    mcp: FastMCP = house_server(
        SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan
    )

    @mcp.custom_route("/share/{token}", methods=["GET"])
    async def share(request: Request) -> Response:
        """Serve an exposed file to whoever holds its token."""
        token = request.path_params["token"]
        found = exposed.lookup(token)
        if found is None:
            return Response("Unknown share link.", status_code=403)
        return await serve_exposed(found)

    # ── For the model ────────────────────────────────────────────────────

    @mcp.tool(name="expose_local_image", meta=FOR_THE_MODEL)
    async def expose_local_image(ctx: Context, path: str) -> str:
        """Give a local file a public HTTPS URL, so something that cannot read
        this machine's disk can fetch it — a vision model asked about a
        picture, a chat API that only accepts links.

        The file is not moved or copied: it is served from where it is, for as
        long as this process is running, and it stops being served if the file
        changes or when the conversation ends.

        Args:
            path: The file on this machine, as an absolute path or one
                relative to where slife2 was started.
        """
        if not path.strip():
            raise ToolError("[refused] no file was named.")
        target = Path(path)
        if not target.exists():
            raise ToolError(f"[refused] there is no file at {path}.")
        if not target.is_file():
            raise ToolError(f"[refused] {path} is a directory, not a file.")
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
                f"[refused] {served.name} looks like a private credential "
                f"(a key, a `.env`, a saved password). A share URL is public "
                f"while the tunnel is up, so this would publish it."
            )

        await _ensure(tunnel, port)
        if not tunnel.is_active:
            raise ToolError(
                "[failed] the tunnel is not up, so there is no public URL to "
                "hand out. `tunnel_status` says what the tunnel is doing."
            )
        # A published URL is not a working one: a transport that lost the edge
        # keeps its URL and answers every request to it with HTTP 530.  Handing
        # that out is handing out a dead link, and the caller finds out later —
        # from a model that could not fetch the file.  Refusing is the honest
        # answer, and the tunnel keeps retrying in the background.
        if not tunnel.is_reachable():
            raise ToolError(
                "[failed] the tunnel has published a URL but nothing is "
                "reaching it from outside right now — every fetch would answer "
                "HTTP 530. `tunnel_status` has the detail; try again shortly."
            )

        found = exposed.register(str(served), request_client(ctx))
        url = tunnel.share_url_for(found.token)
        if url is None:
            raise ToolError("[failed] the tunnel went away as the file was published.")
        return f"{found.name} is now at:\n{url}\n\nThat URL works for as long as this is running. Fetch it, or hand it to whatever needs the file."

    @mcp.tool(name="unexpose_local_image", meta=FOR_THE_MODEL)
    async def unexpose_local_image(ctx: Context, token: str) -> str:
        """Stop serving a file that `expose_local_image` published. Its URL
        answers 403 from then on, and the file itself is untouched.

        Args:
            token: The last part of the share URL — the `xxxx` in
                `https://…/share/xxxx`.
        """
        wanted = token.strip().rstrip("/").rsplit("/", 1)[-1]
        if not wanted:
            raise ToolError(
                "[refused] no token was given — it is the last part of the share URL."
            )
        outcome = exposed.revoke(wanted, request_client(ctx))
        if outcome is Revoked.UNKNOWN:
            raise ToolError(
                f"[refused] no link with the token {wanted!r} is being served."
            )
        if outcome is Revoked.FOREIGN:
            raise ToolError(
                f"[refused] the token {wanted!r} belongs to another "
                f"conversation's link."
            )
        logger.info("unexposed token=%s", wanted)
        return f"Stopped serving {wanted}. That URL no longer works."

    # ── Not for the model ────────────────────────────────────────────────
    # No `meta`, so the hub withholds it (slife2/plugins/audience.py).  It is here
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
                "reachable": tunnel.is_reachable(),
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
    if tunnel.is_starting:
        deadline = time.monotonic() + _T.timeouts.ready.tunnel_start
        while tunnel.is_starting and not tunnel.is_active:
            if time.monotonic() >= deadline:
                # The attempt outlived its own budget — do not start a second
                # one on top of it (the provider would refuse it as "already in
                # progress" and log a warning for a start that is not broken).
                logger.warning(
                    "file_tunnel_start_still_in_flight timeout=%.0fs",
                    _T.timeouts.ready.tunnel_start,
                )
                return
            await asyncio.sleep(_T.timeouts.pacing.tunnel_retry_delay)
        if tunnel.is_active:
            return  # the in-flight attempt was the answer
    await _start(tunnel, port, why="on-demand")


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
            else f"no `{CONFIG_KEY}:` section — falling back to "
            f"{config.file_tunnel.active}"
        ),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
