"""The gateway: how this system talks to an MCP server somebody else runs.

One connection, held open, to one server: connect, list what it offers, call a
tool, say whether it is answering.  That is the whole of it, and the point of the
module is what it *does not* know — no catalogue, no category, no config section,
no naming rule, no idea that a model exists.  A plugin that fronts somebody
else's servers needs exactly this and nothing more (`slife2.libs.plugin.family` is the
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

**Health is a tool list, not a connection**, and that is most of what the
class does.  A server is either usable, meaning its tool list is in hand, or it
is not, and in the second case the useful fact is what it said the last time we
asked.  There is no connection state machine: the source stops counting as
usable when a call fails at the transport or when a connect fails, and the next
ask re-lists it.

**A listing ages, because the peer can no longer be asked to say it changed.**
This module used to re-list on one event and nothing else — the peer's
`notifications/tools/list_changed` — and at 2026-07-28 that event stopped
arriving on its own.  Change notifications moved onto `subscriptions/listen`, a
stream the *client* has to open, and measured against the installed FastMCP
4.0.11 there is no way to open one from here: `Client` exposes no such call, a
FastMCP **server** advertises `tools.listChanged: false` and answers the listen
request with `Method not found` — so our own plugins never send one either — and
the SDK's own `mcp.client.subscriptions.listen` has nothing to talk to.  So
`_T.timeouts.pacing.relist_after` is what keeps a listing true: an ask that finds one older
than that re-reads it.  **The age costs a round trip and never a tool** — a
stale listing is still `usable` and still in the model's list while it is being
replaced, and it is replaced inside the same ask that noticed, because that ask
— the hub's `list_tools` — waits briefly for the attempts it started
(`_T.timeouts.ready.list_settle`, in `slife2.plugins.toolhub.server`'s `settle`).  A peer that
*does* send the notification is believed at once (`invalidate`), which is the
whole of what a legacy server gets: the event is the fast path, and the age is
the floor under it.

**Two failures that look alike and are not.**  A *transport* failure and a peer's
*refusal* are opposites here.  A refusal — an unknown tool, bad arguments, a
permission it will not grant — is a value the caller reads and acts on, and
rebuilding the link would only be told the same thing again.  A transport
failure is *not* retried either: a request that was handed to the SDK may have
reached the peer, and re-running it is a second render, a second file write, a
second message sent.  What is retried is the one case this process can prove —
a connection with no client to ask with, where nothing was asked because there
was nothing to ask with — and it is rebuilt *before* the attempt rather than
after it.  FastMCP makes the refusal/transport split visible for free —
`call_tool(..., raise_on_error=False)` returns `is_error` where the raising form
throws — which is one of the reasons this gateway is a few hundred lines.

**And how failure is answered is all this module decides.**  It holds no clock of
its own: how long a tool may take is the *caller's* to say — the loop's
`_timeout`, or the tool's own declared deadline — and a caller that gives up
cancels the call rather than being answered by a timer down here.  A bound here
would cut off calls whose callers had said they were willing to wait, and a
timer's answer would arrive while the far end was still running the call it
timed out.

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
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import StdioTransport
from mcp import MCPError

import slife2.helpers.timeouts as _T
from slife2.clients.toolclient import SEPARATOR
from slife2.libs.host.config import ToolServerSettings
from slife2.libs.host.runtime import entry_log_path, truncate_if_huge
from slife2.libs.plugin.audience import with_home

logger = logging.getLogger(__name__)

#: The SDK's JSON-RPC `CONNECTION_CLOSED` code, spelled rather than imported:
#: `mcp_types` has the constant and `mcp` does not re-export it, and this build
#: does not depend on `mcp_types` by name.
#:
#: **It says the link is gone.  It says nothing about how far a request got**,
#: and reading it as "nothing was asked" is what ran a model's tool twice.  The
#: dispatcher raises it for a send onto a transport that is already torn down —
#: nothing reached the far end — and, with the same code, to wake *every request
#: already in flight* when the read stream ends
#: (`mcp/shared/jsonrpc_dispatcher.py`'s `_fan_out_closed`), and the streamable
#: HTTP transport synthesises it for a request whose SSE stream ended with no
#: answer (`mcp/client/streamable_http.py`).  So it is a reason to drop the link
#: and never a reason to run the call again.  `REQUEST_TIMEOUT` is outside the
#: class for the same reason: a call that timed out went out, and may still be
#: running.
LINK_DOWN_CODE = -32000


def _link_is_down(exc: BaseException) -> bool:
    """Whether a failed call is the link's failure rather than the peer's answer.

    True for the dispatcher's `CONNECTION_CLOSED` and for nothing else — see
    :data:`LINK_DOWN_CODE`.  **It is not "nothing was asked"**, which is why the
    call it came from is never run again: the same code wakes a request that was
    in flight when the stream ended, so the only thing this licenses is letting
    the link go.

    Everything else means the request went out and this is the far end's answer:
    a peer that refused, or a tool whose body threw.  Neither is a reason to
    rebuild, and neither is a reason to run the call again.
    """
    return isinstance(exc, MCPError) and getattr(exc, "code", None) == LINK_DOWN_CODE


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

    **The alphabet is ASCII, and `isalnum` alone does not say so.**  It is
    Unicode-aware, so `搜索` passed through unchanged and 400'd every request in
    that conversation — the exact failure this exists to prevent, and one no
    test caught because they all used punctuation.  What a provider accepts is
    `[A-Za-z0-9_-]`, and that is what this now emits.

    Length is deliberately not trimmed.  A provider's limit is real, but
    truncation has to invent a rule for what happens when two names truncate to
    the same string, and a collision would silently point a tool call at the
    wrong tool — a worse failure than a request the provider refuses loudly.
    Two names that sanitise alike are that same collision and take the same
    answer: the catalogue refuses the second rather than letting one name mean
    two tools.
    """
    return "".join(
        c if (c.isascii() and c.isalnum()) or c in "-_" else "_" for c in part
    )


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
    tool's documentation works after the names are checked.

    **The entry is written from the settings, so an unknown key does not reach
    the SDK.**  This used to claim `auth:` and the rest passed through, and they
    do not: `_tool_server` reads the keys it knows into `ToolServerSettings`, and
    nothing carries the others — so a key this build does not implement is
    dropped at load rather than at connect.  Supporting one is a field on the
    settings and a line in the loader; this is the place that would hand it on.

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
            entry["headers"] = settings.resolved_headers()
    else:
        entry["command"] = settings.command
        entry["args"] = list(settings.args)
        entry["transport"] = "stdio"
        entry["cwd"] = settings.cwd or cwd
        if settings.env:
            entry["env"] = settings.resolved_env()
    return {"mcpServers": {settings.name: entry}}


def transport_for(settings: ToolServerSettings, *, cwd: str) -> Any:
    """One server's transport, with a stdio child's own words kept apart.

    **The document cannot say where a child's stderr goes.**  `mcpServers` is the
    ecosystem's shape and stays the shape for HTTP, but it has no `log_file`
    (`StdioMCPServer` has no such field), so a stdio entry built through it hands
    its stderr to this process's stderr — which, in a toolhub, is the file every
    entry shares.  The 2026-10 corpus shows what that reads like: a node crash,
    a DuckDuckGo deprecation notice and four servers' start-up chatter interleaved
    in one file, none of it attributable.

    `StdioTransport` takes the field directly.  Each entry gets its own log under
    `runtime/logs/`, bounded by the same ceiling as a daemon's; that file is the
    child's own last words, which is the one thing `str(exc)` cannot carry for a
    process with no console (`Connection.fail` says the same).

    HTTP keeps the document: its headers and URL are resolved here and it has no
    child to attribute anything to.
    """
    if settings.url:
        return mcp_config(settings, cwd=cwd)
    log = entry_log_path(settings.name)
    truncate_if_huge(log)
    return StdioTransport(
        command=settings.command,
        args=list(settings.args),
        env=settings.resolved_env(),
        cwd=settings.cwd or cwd,
        log_file=log,
    )


def make_client(transport: Any, handler: MessageHandler) -> Client:
    """The real client: a bound on connecting, and **none on a call**.

    `init_timeout` is the connect's, and it belongs here: it bounds the
    initialize handshake, which is this module's own await rather than anybody's
    call.

    `timeout` is deliberately *not* set, and setting it was a bug with a long
    reach.  FastMCP turns it into the session's `read_timeout_seconds`, which the
    SDK applies as a **total deadline on every request made over that
    connection** — so the 60 s connect budget became a 60 s cap on every tool
    call to somebody else's server, whatever the caller had asked for.  A twenty-
    minute render called with the model's `_timeout: 600` died at 60 s as an
    ordinary-looking `ok=False`, with the tool still running at the far end, and
    the module's own prose above ("no clock lives here") was the thing it
    contradicted.  The rule the registry states is the one that was broken: the
    layer that awaits owns the bound, and a client library must not set a total
    for every caller that borrows it.

    What bounds a call now is the caller's own: the loop wraps the dispatch in
    the `_timeout` the model stated or the tool declared (and, with neither, in
    nothing, which is what `SDD` §12.4 says a foreground call gets).  The
    two awaits here that *no* caller bounds — a listing, and the hub's ask for a
    plugin's sources — carry their own, at the call site.

    **`mode="legacy"` is the third thing this constructor decides**, and it was
    found the hard way.  FastMCP 4.1's default is `"auto"`: the client probes
    `server/discover` at the newest modern protocol version first, and falls
    back to `initialize` only when the server answers the probe *as an MCP
    error*.  A server built on an older SDK does neither — the Python one logs
    a pydantic validation warning and drops the request — so the fallback never
    fires and the client sits there until `init_timeout` expires, reported as
    `RuntimeError: Client failed to connect: Failed to initialize server
    session`.  Measured on the slow machine (2026-10-21): all eighteen stdio
    `mcp:` entries failed at the 60 s connect budget this way, while the two
    HTTP entries — whose servers answer the probe with a 400 — fell back and
    worked; the same `fetch` entry connected in 22.2 s with `"legacy"` and
    never with `"auto"`.  **A longer timeout does not help**: nothing in an
    old server will ever answer that probe.  `initialize` is the one handshake
    every MCP server implements, and our own FastMCP servers answer it too, so
    this hop speaks the handshake era.  The hop that dials *our* servers
    (`slife2.libs.plugin.protocol.Peer`) keeps the modern probe, where both
    ends of the wire are ours.
    """
    return Client(
        transport,
        message_handler=handler,
        init_timeout=_T.timeouts.ready.connect_tools,
        mode="legacy",
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
    either usable — meaning its tool list is in hand and recorded — or it is
    not, and in the second case the useful fact is what it said the last time
    we asked.  The snapshot carries its age as well (`_listed_at`), and that is
    not a third state: an old listing is usable, it is simply due to be read
    again.

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
        #: How many times this link has been reported failing.  Read across the
        #: listing handover: the last failure is what a report shows until the
        #: new listing is *recorded*, and this is how the handover knows whether
        #: the error it finds afterwards is one the callback just set.
        self._failures = 0
        self._ready = False
        #: When the listing in hand was read, on the monotonic clock — the one
        #: clock that cannot go backwards, since a wall clock that jumps is how a
        #: listing becomes either immortal or instantly stale.  Zero means the
        #: same as "never", which is what a source that has not listed yet is.
        self._listed_at = 0.0
        self._lock = asyncio.Lock()
        #: The attempt in flight, if any.  Held so that a caller can wait for it
        #: and so that a second one is never started alongside it.
        self._attempt: asyncio.Task[None] | None = None

    # --- what the caller reports ---------------------------------------------

    @property
    def usable(self) -> bool:
        """Whether its tool list is in hand — asked without waiting for one.

        **In hand means recorded**, not merely answered: a listing is handed to
        `on_listed` to be written, and a source whose rows are still being
        merged, or were refused, has nothing a caller may serve.  The
        synchronous half of :meth:`ready`, and the one a caller assembling a
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
        """Start a connect, or a re-list, unless one is running or neither is due.

        Separate from `ready` because the common case — a healthy server with a
        current listing — must not await anything, and separate from the work
        itself so that the callers who only want the list can start one and move
        on.

        **This is where a listing is refreshed without anybody asking it to be**,
        and an ask is what this method means: a caller arriving here has decided
        to read the tool list, which is the moment a stale one is worth
        re-reading and no earlier.  `_stale` is what bounds how often — see
        `_T.timeouts.pacing.relist_after`.
        """
        if self._attempt is not None and not self._attempt.done():
            return
        if self._ready and not self._stale:
            return
        self._attempt = asyncio.create_task(self._establish())

    @property
    def _stale(self) -> bool:
        """Whether the listing in hand is old enough to be worth reading again.

        `_ready` is part of the answer rather than assumed by the caller: a
        source with no listing has none to age, and its reason to reconnect is
        its own rather than this one's.  So the only state this can be true in is
        "listed, and a while ago", and both callers above read it that way.
        """
        return (
            self._ready
            and time.monotonic() - self._listed_at >= _T.timeouts.pacing.relist_after
        )

    @property
    def attempt(self) -> asyncio.Task[None] | None:
        """The connect in flight, if there is one — for a caller that wants to
        wait on attempts it did not start."""
        return None if self._attempt is None or self._attempt.done() else self._attempt

    async def ready(self) -> bool:
        """Whether the link is usable, starting one and waiting if it is not.

        **Not where a listing is refreshed**, and the omission is deliberate: this
        is the question a *call* asks, and a call is by the far end's own name —
        the peer resolves it, so a listing that has aged changes nothing about
        whether the call can be made.  Freshness belongs to `connecting`, which
        the askers of the tool *list* call.
        """
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

        **The second step is also the whole of a re-list.**  A source that has
        aged out needs no new connection — only a new answer — so the client is
        kept and `_refresh` runs over it, which is what makes the age cost a
        round trip rather than a handshake (and, for a stdio server, rather than
        the seconds it takes to start a child process again).
        """
        async with self._lock:
            if self._ready and not self._stale:
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
        module docstring.

        **And a source is usable only once the handover has recorded it.**  A
        link that has answered but whose rows are still being merged is not a
        source anything may rely on: the merge is exactly where a name another
        source owns is refused, and a reader arriving during it would count
        this source as fine while its rows are absent — the invisible short
        list the hub's required-plugin gate exists to prevent.  A callback that
        takes the link down still has the last word, because `fail` and
        `disconnect` leave their mark and the flag below reads it.

        **The clock starts when the answer lands, not when it was asked for**,
        and that is the same rule as the flag above: what ages is the listing, so
        a slow answer is a *new* listing for as long as it takes to arrive and
        the next ask is not already overdue for the one after it.

        **The listing carries its own bound**, because no caller gives it one:
        `client.list_tools` takes no per-call timeout (only `call_tool` does), and
        `make_client` no longer sets a client-wide one.  Without this a server
        that accepts the connection and then never answers the listing would hold
        the hub's own ask open forever — the failure the removed client timeout
        was accidentally covering for, here stated where it belongs.
        """
        client = self._client
        if client is None:  # pragma: no cover - only reachable after `fail`
            return
        try:
            async with asyncio.timeout(_T.timeouts.ready.connect_tools):
                listed = await client.list_tools()
        except Exception as exc:  # noqa: BLE001 - a listing that threw is this source being unusable
            await self.disconnect()
            self.fail(exc)
            return
        failures = self._failures
        self._listed_at = time.monotonic()
        if self._on_listed is not None:
            await self._on_listed(listed)
        # **The handover decides whether there is a list to serve.**  A callback
        # that refused the rows has called `fail` (the error is set), or dropped
        # the link (`disconnect`, so there is no client); one that recorded them
        # leaves both alone, and only then does the old failure stop being the
        # last thing this source said.  For a re-list the old listing stayed
        # usable throughout, which is the point: it was already recorded, and
        # only a refusal may take it away.
        if self._failures == failures and self._client is not None:
            self._error = ""
        self._ready = not self._error and self._client is not None

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

        **A peer that says so is still worth listening to**, and this is the fast
        path over `_T.timeouts.pacing.relist_after`: where the age bounds how wrong a
        listing may be, this makes it zero — which is all a legacy server, whose
        notifications still arrive, gets out of having sent one.
        """
        if self._ready:
            self._ready = False

    def fail(self, exc: Exception) -> None:
        """Record that this server is unusable, and tell the caller.

        The error is kept rather than raised, because it is the answer to "what
        did it say the last time we asked" — and the caller is told, because
        what a failure *means* is not this module's to decide.

        **The report belongs to the caller, and the duplicate is what this split
        removes.**  A `Family` member and the hub's own sources both pass
        `on_failed`, and both name the source, its target and what the failure
        means; this line naming the same failure a millisecond earlier was the
        second half of a pair every unreachable server cost — seventeen of them
        in the 2026-10 corpus.  With a callback, the line here drops to DEBUG
        and carries the traceback instead — the cause chain a one-line report
        deliberately leaves out, available where somebody has asked for detail.
        """
        self._ready = False
        self._failures += 1
        self._error = f"{type(exc).__name__}: {exc}".strip()
        target = self.settings.url or self.settings.command
        if self._on_failed is None:
            logger.warning(
                "%s (%s) is not usable: %s",
                self.settings.name,
                target,
                self._error,
            )
        else:
            logger.debug(
                "%s (%s) is not usable: %s",
                self.settings.name,
                target,
                self._error,
                exc_info=exc,
            )
        if self._on_failed is not None:
            self._on_failed(exc)

    async def disconnect(self) -> None:
        """Drop the connection, keeping the configuration and the error.

        **A teardown that fails must not replace the caller's work**, so the
        failure is suppressed — but not silently: a transport that repeatedly
        fails to close is a leaked child or socket, and the debug line is the
        only trace it leaves.  A cancellation still wins (`BaseException`).
        """
        client, self._client = self._client, None
        self._ready = False
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception as exc:
                logger.debug(
                    "%s: the link did not tear down: %s",
                    self.settings.name,
                    exc,
                    exc_info=True,
                )

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

        **Nothing that was asked for is ever asked again.**  A peer that answered
        and refused — an unknown tool, bad arguments, a permission it will not
        grant — has said something the model should read, and rebuilding the link
        would only repeat it.  A call that *did* go out and then failed is not
        retried either, and that is the more important half of the rule: it may
        be running at the far end, so a second attempt is a second render, a
        second file write, a second message sent.  There is no exception whose
        code can tell that case from a send onto a link that was already gone
        (see :data:`LINK_DOWN_CODE`), so no failure is read as licence to retry.

        **The one thing tried again is a call there was no client to make**, and
        it is safe precisely because it was never asked: `_call_once` returns
        `None` for it, this method lets the dead connection go, connects, and
        asks — which is a first attempt rather than a second one.  A source does
        not stay broken by this: the link dropped by a failed call is rebuilt by
        the next ask, at the cost of one call answered with the failure.

        **No clock lives here**, which is the other half of the same rule: the
        caller that gave up cancels this call (`slife2.libs.turn.loop`'s `_timeout`, or the
        tool's own declared deadline), and a cancellation is not a failure that
        this method is entitled to answer for.

        `meta` is the caller's identity, carried across unchanged.  This module
        does not read it and could not: one process serves every conversation in
        the system over one connection, so whose behalf a call is on is a fact
        only the far end can act on — see `slife2.libs.plugin.audience`.
        """
        if not await self.ready():
            return f"{self.settings.name} is not connected: {self._error}", False

        result = await self._call_once(tool, arguments, meta)
        if result is not None:
            return result

        # Nothing was asked — there was no client to ask with — so connecting and
        # asking now is the first attempt at this call rather than a second one.
        logger.info("%s: reconnecting and calling %s", self.settings.name, tool)
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
        """One attempt.  `None` means there was no client to make it with.

        **`None` is a claim about this process, and never about the far end.**  A
        connection with no client is a connection nothing was asked of, so
        running the call after connecting is running it once — that is the whole
        of what makes the caller's reconnect safe, and it is decided *here*,
        before the call, rather than read out of an exception afterwards.  No
        exception can carry that claim: the SDK raises `CONNECTION_CLOSED` both
        for a send onto a torn-down link and for a request that was in flight
        when the link went (:data:`LINK_DOWN_CODE`), and reading the second as
        the first is what ran a model's file write twice.

        **A transport failure and a refusal are still told apart**, because they
        want different things: a refusal is the peer's answer, and the link is
        left alone for the next call to use; a link that is down is let go here,
        so the *next* ask reconnects rather than every call finding the same dead
        client.  The branch is narrow rather than an `except Exception` because
        the wide version costs a rebuild: on a peer's refusal it asks the same
        question again and is told the same thing.
        """
        client = self._client
        if client is None:
            return None
        try:
            result = await client.call_tool(
                tool,
                arguments,
                # The SDK would otherwise raise for a tool that reported an
                # error, and this is the half of the contract that says a
                # refusal is a value: the difference between "the tool said no"
                # and "the link is gone" is exactly what the two branches below
                # are, and collapsing them would drop the link every time a
                # caller passed a bad argument.
                raise_on_error=False,
                # The caller's identity, forwarded rather than interpreted.  What
                # carries it is a proxy, and this is the one thing it passes on
                # that did not come from the model.  **The home goes with it**,
                # because this is a hop: the plugin this reaches is a shared
                # process holding somebody else's data, and which directory the
                # call is about is the half of the identity the arguments cannot
                # say (`slife2.libs.plugin.audience.with_home`).
                meta=with_home(meta),
            )
        except Exception as exc:  # noqa: BLE001 - the split below is the decision, not the catch
            # Neither branch runs the call again — see the docstring.  What the
            # split decides is the *link*: a peer's refusal leaves it standing,
            # a dead one is dropped so the next ask rebuilds it.
            if _link_is_down(exc):
                # The link is gone, and this call is answered rather than
                # retried: the code reaches here for a request that was in
                # flight when the stream ended just as much as for a send onto a
                # dead transport, so the far end may already be running it.
                logger.warning(
                    "%s: calling %s found the link down: %s",
                    self.settings.name,
                    tool,
                    exc,
                )
                self._error = f"{type(exc).__name__}: {exc}".strip()
                await self.disconnect()
            else:
                # The request went out and this is the far end's answer — a
                # refusal, or a tool that threw.  The model's to read, and the
                # link is left alone.
                logger.warning(
                    "%s: calling %s failed: %s", self.settings.name, tool, exc
                )
            return (
                f"{self.settings.name}: {tool} failed: {type(exc).__name__}: {exc}",
                False,
            )
        return flatten(result.content), not result.is_error

    async def close(self) -> None:
        if self._attempt is not None:
            self._attempt.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._attempt
        await self.disconnect()


__all__ = [
    "ClientFactory",
    "Connection",
    "Watching",
    "abandon",
    "flatten",
    "make_client",
    "mcp_config",
    "proxied_name",
    "sanitise",
    "transport_for",
]
