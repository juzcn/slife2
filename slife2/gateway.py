"""The gateway: how this system talks to an MCP server somebody else runs.

One connection, held open, to one server: connect, list what it offers, call a
tool, say whether it is answering.  That is the whole of it, and the point of the
module is what it *does not* know — no catalogue, no category, no config section,
no naming rule, no idea that a model exists.  A plugin that fronts somebody
else's servers needs exactly this and nothing more (`slife2.toolfamily` is the
rest of that plugin), and so does the toolhub for the plugins it talks to, which
is why it is a module both import rather than a copy in each.

**It returns; the caller records.**  This is the seam, and it is the reason the
class is shaped the way it is.  What a tool list *means* — which of those tools
the model may be given, what they are called, which rows they become, whether a
failure is a source's problem or the system's — is the caller's, so the gateway
hands each listing over through `on_listed` and reports a source that stopped
answering through `on_failed`, and keeps no copy of either.  A gateway that
merged, or that named a tool, would be the toolhub's second half instead of a
piece both halves share.

**Health is a tool list, not a connection** — v1's rule, and most of what the
class does.  A server is either usable, meaning its tool list is in hand, or it
is not, and in the second case the useful fact is what it said the last time we
asked.  There is no connection state machine and no timer: the source stops
counting as usable when the peer says `tools/list_changed`, when a call fails at
the transport, or when a connect fails, and the next ask re-lists it.

**Two failures that look alike and are not.**  A *transport* failure and a peer's
*refusal* are opposites here.  A refusal — an unknown tool, bad arguments, a
permission it will not grant — is a value the caller reads and acts on, and
rebuilding the link would only be told the same thing again.  A link that died
mid-call is retried, **once**: a tool that never ran is worth a second attempt,
and a server that is down must not turn every call into two timeouts.  FastMCP
makes the split visible for free — `call_tool(..., raise_on_error=False)` returns
`is_error` where the raising form throws — which is one of the reasons this port
is a few hundred lines where v1's gateway was three thousand.

**A credential is held by the process that needs it.**  `env` and `headers` are
the two places a tool server is handed one, they are resolved by the config's own
secret chain, and they are handed to a child process or an HTTP client from
*here* — which is why the process that holds this module's `Connection` is the
process that holds the key, and nothing upstream of it ever sees one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Client
from fastmcp.client.messages import MessageHandler

from slife2.config import ToolServerSettings
from slife2.toolclient import SEPARATOR

logger = logging.getLogger(__name__)

#: How long to wait for one upstream to answer `tools/list` after connecting.
#: Generous, because a stdio server may be an `npx` invocation that has to fetch
#: itself the first time, and a deadline that fires during that leaves a tool
#: server permanently absent when it was only slow.
CONNECT_TIMEOUT_SECONDS = 60.0

#: How long a tool may run.  Long: a tool is a real action somewhere else, and
#: the alternative to waiting is a call the model cannot tell was cut off.
CALL_TIMEOUT_SECONDS = 300.0

#: How a client is built from a transport.  A seam rather than a direct call,
#: because the rebuild-once rule below is about a link that dies *during a call*,
#: and nothing about a real transport can be made to fail on demand from a test.
ClientFactory = Callable[[Any, MessageHandler], Client]


def sanitise(part: str) -> str:
    """A name a provider will accept as a tool name.

    Providers restrict tool names to letters, digits, underscore and hyphen, and
    reject the whole request over one that is not — so a single server tool
    called `read.file` would otherwise 400 every turn of every conversation, with
    a message about the tool list that names no server.  Replacing the character
    is the cheap half of the fix; the other half is that the server's own name is
    kept beside it (`UpstreamTool.tool`), because this one can no longer be used
    to address the far end.

    Length is deliberately not trimmed.  A provider's limit is real, but
    truncation has to invent a rule for what happens when two names truncate to
    the same string, and a collision would silently point a tool call at the
    wrong tool — a worse failure than a request the provider refuses loudly.
    """
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in part)


def proxied_name(server: str, tool: str) -> str:
    """What the model calls somebody else's tool: `server__tool`.

    **A name carries a server only where it has to.**  The operator may write
    down four servers that each offer a `search`, and `arxiv__search` against
    `serper__search` is the difference between a call reaching the tool the model
    read about and one reaching a stranger.  For our own plugins it never has to:
    there is one set of tools here, slife2's, and `now` is the name of one of
    them — which is why this function is for the servers somebody else runs, and
    the toolhub names its own plugins' tools bare.
    """
    return f"{sanitise(server)}{SEPARATOR}{sanitise(tool)}"


def mcp_config(settings: ToolServerSettings, *, cwd: str) -> dict[str, Any]:
    """One server, in the shape an MCP client takes.

    The standard `mcpServers` document, which is the ecosystem's own config
    format rather than one invented here — so a block copied out of another
    tool's documentation works after the names are checked, and `auth:` and the
    other keys this build does not implement still reach the SDK.

    `cwd` is the fallback working directory for a stdio server.  It matters
    because an entry is likely to name a path, and `.` has to mean something:
    a daemon's own working directory is its runtime folder, which is nobody's
    idea of where their files are.
    """
    entry: dict[str, Any] = {}
    if settings.url:
        entry["url"] = settings.url
        entry["transport"] = "http"
        if settings.headers:
            entry["headers"] = dict(settings.headers)
    else:
        entry["command"] = settings.command
        entry["args"] = list(settings.args)
        entry["transport"] = "stdio"
        entry["cwd"] = settings.cwd or cwd
        if settings.env:
            entry["env"] = dict(settings.env)
    return {"mcpServers": {settings.name: entry}}


def make_client(transport: Any, handler: MessageHandler) -> Client:
    """The real client: both timeouts pinned to the ones this module chose."""
    return Client(
        transport,
        message_handler=handler,
        init_timeout=CONNECT_TIMEOUT_SECONDS,
        timeout=CONNECT_TIMEOUT_SECONDS,
    )


def flatten(content: Any) -> str:
    """A call result's content blocks as text.

    Text is joined; anything else is *described* rather than dropped, because a
    model can act on knowing that an image came back and cannot act on silence.
    A result with no blocks at all comes back as a note saying so, so that an
    empty string is never mistaken for "the tool returned nothing".
    """
    blocks = content if isinstance(content, list) else [content]
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        mime = getattr(block, "mime_type", None) or "unknown type"
        payload = getattr(block, "data", None)
        size = f", {len(payload)} characters" if isinstance(payload, str) else ""
        parts.append(f"[{type(block).__name__}: {mime}{size}]")
    return "\n".join(parts) or "(the tool returned nothing)"


async def abandon(client: Client) -> None:
    """Let go of a client that was never handed to anyone.

    A `close` that raises must not replace the failure being reported, and that
    goes double here: this runs on the error path and on the way out of a
    cancellation.  One function rather than a method, because two owners reach
    it — a connection that could not establish, and a client that could not
    enter.
    """
    with contextlib.suppress(Exception):
        await client.__aexit__(None, None, None)


class Watching(MessageHandler):
    """Tells its connection when the peer says its tool list has changed.

    The whole point is that nothing polls: a server that grows a tool says so,
    the listing is dropped, and the next ask re-reads it.  This is what "health
    is a tool list, not a connection" costs — a flag and a callback, rather than
    a per-server state machine.
    """

    def __init__(self, changed: Callable[[], None]) -> None:
        self._changed = changed

    async def on_tool_list_changed(self, message: Any) -> None:
        logger.debug("the peer says its tool list changed: %s", message)
        self._changed()


class Connection:
    """One server somebody else runs, and the link this process keeps to it.

    **The connection is kept, not opened per call.**  For an HTTP server that is
    a handshake saved; for a stdio one it is everything, because the process
    behind it takes seconds to start and spawning it per tool call would make a
    tool call take seconds.

    **State is a snapshot plus an error, not a state machine.**  A server is
    either usable — meaning its tool list is in hand — or it is not, and in the
    second case the useful fact is what it said the last time we asked.

    Everything here is safe to call concurrently: a caller asks for a tool list
    while a turn that is already running is calling a tool.
    """

    def __init__(
        self,
        settings: ToolServerSettings,
        *,
        transport: Callable[[ToolServerSettings], Any],
        client_factory: ClientFactory | None = None,
        on_listed: Callable[[list[Any]], Awaitable[None]] | None = None,
        on_failed: Callable[[Exception], None] | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._make_client = client_factory or make_client
        #: Handed every listing, and the reason this class holds none: what a
        #: tool list becomes is the caller's business, and a copy here would be a
        #: second thing to keep in step.
        self._on_listed = on_listed
        #: Told when this server stops answering.  The caller decides what that
        #: means — the toolhub records a verdict on the catalogue rows, a plugin
        #: fronting somebody else's server does whatever its own bookkeeping is —
        #: and it is a callback rather than a return value because the failure
        #: happens inside a connect attempt the caller deliberately does not
        #: await.
        self._on_failed = on_failed
        self._client: Client | None = None
        self._error = ""
        self._ready = False
        self._lock = asyncio.Lock()
        #: The attempt in flight, if any.  Held so that a caller can wait for it
        #: and so that a second one is never started alongside it.
        self._attempt: asyncio.Task[None] | None = None

    # --- what the caller reports ---------------------------------------------

    @property
    def usable(self) -> bool:
        """Whether its tool list is in hand — asked without waiting for one.

        The synchronous half of :meth:`ready`, and the one a caller assembling a
        list uses: it must not block on a server that has been failing, and must
        not be told a source that is down is fine.
        """
        return self._ready

    @property
    def error(self) -> str:
        """What it said the last time it failed, or `""`."""
        return self._error

    @property
    def state(self) -> str:
        """One word for a log line or a report: what this link is doing."""
        if self._ready:
            return "ready"
        if self._attempt is not None and not self._attempt.done():
            return "connecting"
        return "failed" if self._error else "idle"

    # --- the connection ------------------------------------------------------

    def connecting(self) -> None:
        """Start a connect attempt unless one is running or one has succeeded.

        Separate from `ready` because the common case — a healthy server — must
        not await anything, and separate from the connect itself so that the
        callers who only want the list can start one and move on.
        """
        if self._ready:
            return
        if self._attempt is not None and not self._attempt.done():
            return
        self._attempt = asyncio.create_task(self._establish())

    @property
    def attempt(self) -> asyncio.Task[None] | None:
        """The connect in flight, if there is one — for a caller that wants to
        wait on attempts it did not start."""
        return None if self._attempt is None or self._attempt.done() else self._attempt

    async def ready(self) -> bool:
        """Whether the link is usable, starting one and waiting if it is not."""
        if self._ready:
            return True
        self.connecting()
        attempt = self._attempt
        if attempt is not None:
            # Shielded because another caller may be waiting on the same task,
            # and a cancellation here is about *this* caller giving up rather
            # than about the connection being unwanted.
            with contextlib.suppress(Exception):
                await asyncio.shield(attempt)
        return self._ready

    async def _establish(self) -> None:
        """Get to a tool list, connecting first if there is no connection yet.

        Both steps are here because both can be the thing that failed, and the
        answer to either is the same: keep the error, drop the link, and let the
        next ask try again.
        """
        async with self._lock:
            if self._ready:
                return
            if self._client is None:
                # Everything, including building the transport, is inside the
                # `try`: an entry FastMCP refuses — a malformed URL, a key it
                # does not know — raises here, and that is a *server* that
                # cannot be reached rather than a caller with a hole in it.
                client: Client | None = None
                try:
                    client = self._make_client(
                        self._transport(self.settings),
                        Watching(self.invalidate),
                    )
                    await client.__aenter__()
                except asyncio.CancelledError:
                    # Only a shutdown cancels an attempt, and a half-entered
                    # client would hold a connection nobody owns.
                    if client is not None:
                        await abandon(client)
                    raise
                except Exception as exc:  # noqa: BLE001 - every connect failure is this server's, and is reported
                    # `str(exc)` for a stdio server is usually the child's own
                    # last words, which is the only place they are readable —
                    # there is no console for it to have printed to.
                    if client is not None:
                        await abandon(client)
                    self.fail(exc)
                    return
                self._client = client
            await self._refresh()

    async def _refresh(self) -> None:
        """Ask for a tool list and hand it over, or mark this source unusable.

        **The listing is handed on and not kept.**  What came back is the whole
        truth about this source, and it is the caller's to record — see the
        module docstring.  The order matters: this link counts as usable as soon
        as the server answered, *before* the handover, so that a caller which
        decides the list cannot be recorded can take it back down (by calling
        `disconnect` or `fail`) and have the last word.
        """
        client = self._client
        if client is None:  # pragma: no cover - only reachable after `fail`
            return
        try:
            listed = await client.list_tools()
        except Exception as exc:  # noqa: BLE001 - a listing that threw is this source being unusable
            await self.disconnect()
            self.fail(exc)
            return
        self._error = ""
        self._ready = True
        if self._on_listed is not None:
            await self._on_listed(listed)

    def invalidate(self) -> None:
        """Forget the tool list, keeping the connection.

        Called from the peer's own `tools/list_changed`, and from there only: a
        call that failed at the transport takes `disconnect` instead, because
        what has to go in that case is the link and not merely the answer.  Named
        apart from it for that reason — the socket is usually fine and only the
        listing changed, and re-entering the transport on a notification would
        restart every stdio server that ever renames a tool.

        Forgetting it means the source is no longer *usable*, so a caller that
        gates on liveness stops counting it until the next ask re-lists it.
        """
        if self._ready:
            self._ready = False

    def fail(self, exc: Exception) -> None:
        """Record that this server is unusable, and tell the caller.

        The error is kept rather than raised, because it is the answer to "what
        did it say the last time we asked" — and the caller is told, because
        what a failure *means* is not this module's to decide.
        """
        self._ready = False
        self._error = f"{type(exc).__name__}: {exc}".strip()
        logger.warning("%s is not usable: %s", self.settings.name, self._error)
        if self._on_failed is not None:
            self._on_failed(exc)

    async def disconnect(self) -> None:
        """Drop the connection, keeping the configuration and the error."""
        client, self._client = self._client, None
        self._ready = False
        if client is not None:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)

    # --- a call --------------------------------------------------------------

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Run one of this server's tools, returning `(text, ok)`.  Never raises.

        `tool` is **the far end's own name for it**, which is not the name a
        caller used: the advertised name is sanitised and may carry the server in
        front of it, and only the row that recorded it knows both.

        **One rebuild, and only for a transport failure.**  A peer that answered
        and refused — an unknown tool, bad arguments, a permission it will not
        grant — has said something the model should read, and rebuilding the link
        would only repeat it.  A link that died mid-call is the one case where
        trying again is not superstition, and it is tried exactly once: a server
        that is down must not turn every call into two timeouts.

        `meta` is the caller's identity, carried across unchanged.  This module
        does not read it and could not: one process serves every conversation in
        the system over one connection, so whose behalf a call is on is a fact
        only the far end can act on — see `slife2.audience`.
        """
        if not await self.ready():
            return f"{self.settings.name} is not connected: {self._error}", False

        result = await self._call_once(tool, arguments, meta)
        if result is not None:
            return result

        logger.info("%s: rebuilding the link and retrying %s", self.settings.name, tool)
        await self.disconnect()
        if not await self.ready():
            return f"{self.settings.name} is not connected: {self._error}", False
        result = await self._call_once(tool, arguments, meta)
        if result is None:
            return f"{self.settings.name} is not connected: {self._error}", False
        return result

    async def _call_once(
        self,
        tool: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> tuple[str, bool] | None:
        """One attempt.  `None` means the transport failed, not the tool."""
        client = self._client
        if client is None:
            return None
        try:
            result = await client.call_tool(
                tool,
                arguments,
                timeout=CALL_TIMEOUT_SECONDS,
                # The SDK would otherwise raise for a tool that reported an
                # error, and this is the half of the contract that says a
                # refusal is a value: the difference between "the tool said no"
                # and "the link is gone" is exactly what the two branches below
                # are, and collapsing them would rebuild the link every time a
                # caller passed a bad argument.
                raise_on_error=False,
                # The caller's identity, forwarded rather than interpreted.  What
                # carries it is a proxy, and this is the one thing it passes on
                # that did not come from the model.
                meta=meta,
            )
        except Exception as exc:  # noqa: BLE001 - a call that threw is a transport failure, and returns None
            logger.warning("%s: calling %s failed: %s", self.settings.name, tool, exc)
            self._error = f"{type(exc).__name__}: {exc}".strip()
            return None
        return flatten(result.content), not result.is_error

    async def close(self) -> None:
        if self._attempt is not None:
            self._attempt.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._attempt
        await self.disconnect()


__all__ = [
    "CALL_TIMEOUT_SECONDS",
    "CONNECT_TIMEOUT_SECONDS",
    "ClientFactory",
    "Connection",
    "Watching",
    "abandon",
    "flatten",
    "make_client",
    "mcp_config",
    "proxied_name",
    "sanitise",
]
