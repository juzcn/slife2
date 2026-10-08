"""slife2-toolhub — the model's tools, and the only process that holds their
credentials.

Every other component in this system is a place a capability comes from: a model
backend speaks one wire protocol, the db keeps turns, the agent loop runs turns.
This one is where **the tools come from**, and it exists because tools are the
one capability that has to reach *outside* the machine — to somebody else's MCP
server, to a REST API, to a program that wants an API key in its environment.

    slife2-agent  ──MCP──▶  slife2-toolhub  ──MCP──▶  external tool servers
                             list_tools                 (stdio or http)
                             call_tool
                             servers

**The key property is the same one the model backends have, pointed at tools.**
A tool server's credentials are read by this process and by nothing else: the
agent loop cannot leak a token it never had, and `grep` for a provider SDK in
the agent's tree still finds nothing. Switching what tools the model has is
editing a URL or a command in `slife2.yaml`, exactly as switching models is.

Three sources, one list
-----------------------
The tools come from three places, and this is the only thing that knows all of
them.

**Plugins** are the servers slife2 starts — builtins, the db, the agent, a model
backend — and each offers its tools to one of two callers.  `now` and `calc` are
for the model; `remember` and `send_message` are for our own code, called at a
moment the code already knows.  **Tool servers** are everybody else's, under
`tools:` and `rest-api:`, and they are for the model by the simple fact that an
operator wrote them down.  **Local tools** are neither: they are the ones with
nothing behind them at all — a skill, which is a document in `<data>/skills/` —
and the hub serves those itself, for the reason given below.

Which source a tool came from is not what decides who may call it — which
*caller* it is for does, and that is said on the tool itself (`slife2.audience`)
rather than in its name, in this file, or in the config.  **A plugin's tools
belong to that plugin's own code until one of them says otherwise**, so
`remember` stays where it was and `now` carries the mark, while an entry under
`tools:` needs no mark at all: the operator opted in by writing the entry.  The
list of plugins is not written down here either — it is `Config.components()`,
which is what the launcher starts, so the hub cannot drift from the set of
processes that exist.

Why the tools are *here* and not in the agent
---------------------------------------------
`now` and `calc` are pure functions with no process and no credential, so
reaching them through a hub costs a hop — and they are behind one anyway,
because **the model's tool list is one thing and it should have one owner.**
Provenance (whose tool is this), the naming rule that keeps two servers' `search`
apart, and — the first time it appears — the question of which tools may run
without asking the user, are all questions about the *set*, and a set assembled
in two places is a set that will disagree with itself.

That is why `now` and `calc` are not served by this process but by
`slife2-builtins`, which the hub reaches exactly as it reaches somebody else's
arxiv server.  **Nothing that has a server behind it is served by this
process.**  A builtin that took a shortcut would be the second mechanism this
whole arrangement exists to avoid, and the first thing to drift: it would not be
in `servers()`, it would not have a connection to fail, and it would not appear
in the list a tool search would one day be built on.

One kind of tool has no server behind it, and is served here.  A **skill** is a
document on this machine and `skill_use` reads it: no process, no credential, no
protocol, no address — nothing a hop could reach, plus the fact that the hub
already holds the directory it would be reading.  A server invented to hold one
function whose whole body is a `read_text` is not uniformity, it is a second
process that exists to be connected to.  The rule that survives is the one worth
having, and it is the same rule: **everything with a server behind it is reached
by exactly one code path** — the builtins included, which is why they stay where
they are.  The hub's own tools are the other path, and they are named as
themselves (`skill_use`) rather than `{server}__{tool}`, because there is no
server to name.  See DESIGN.md §8.

REST APIs are not a second mechanism
------------------------------------
A `rest-api:` entry is expanded *by the config layer* into the stdio command that
serves it (`slife2.config._rest_api`), so what arrives here is an ordinary
upstream.  Nothing in this module knows what REST is, which is the point: there
is one kind of thing to connect to, and the wrapper people publish for OpenAPI
is just how a REST API becomes one.

What this deliberately does not do
----------------------------------
v1's gateway also let the *model* add and remove servers, through `mcp_set` and
friends, writing its own `tools.yaml`.  That is not ported.  slife2's config is
one file read by every process and by the launcher, and the launcher already
refuses to let a command line name an arbitrary program; a language model
choosing one is the same capability with a worse author.  The tools a model may
reach are the operator's decision, made in a file, once.

The failure rules, all three
----------------------------
* **A plugin that is not answering is a broken system.**  Everything under
  `servers:` is ours: slife2 starts it, the launcher refuses to bring the system
  up without it, and a hub that cannot read its tool list refuses to hand one out
  rather than serving the model a shorter one — a model that has quietly lost
  `now` and `calc` is a failure nobody can see.  It is a *flag on the
  connection* rather than a branch in the tool table, and which section a server
  was configured in is the whole of the difference: a plugin is required, an
  upstream is not.
* **An upstream missing is not.**  An external server is the operator's
  configuration and somebody else's process; it can be slow, paid, or down
  without our system being broken.  It is reported by `servers()` and left out
  of the tool list, and the next ask tries again — no timer, no backoff loop.
* **An upstream refusing a call is data.**  It comes back as text with `ok`
  false, which the model reads and acts on.  The loop's error path is a feedback
  channel, not a failure mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.client.messages import MessageHandler

from slife2 import skills
from slife2.audience import for_the_model, forwarded_client, request_meta
from slife2.config import Config, ToolServerSettings, find_config_path, load
from slife2.mcp_server import (
    configure_logging,
    house_server,
    parse_serve_args,
    serve,
)
from slife2.paths import data_dir
from slife2.toolclient import SEPARATOR, UpstreamTool

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-toolhub"

#: This server's key in the config's `servers:` table — and therefore the one
#: name in `Config.components()` that is not a source of tools.  The hub asks
#: every plugin but itself; a connection to itself would list the three tools of
#: its own API and drop all three, which is a loopback nobody should have to
#: reason about.
CONFIG_KEY = "toolhub"

#: Where the hub's own tools came from, in the `server` field of what it
#: advertises.  Not a server and never dialled: the field is provenance, and a
#: tool whose provenance is a folder should say so rather than leave it blank.
SKILLS_SOURCE = "skills"

INSTRUCTIONS = (
    "The tools the agent may run. Call `list_tools` for the whole set, "
    "`call_tool` to run one, and `servers` to see which external tool servers "
    "are connected. This server is called by the agent, not by a model: the "
    "tools it lists are what the agent offers onwards."
)

#: How long to wait for one upstream to answer `tools/list` after connecting.
#: Generous, because a stdio server may be an `npx` invocation that has to fetch
#: itself the first time, and a deadline that fires during that leaves a tool
#: server permanently absent when it was only slow.
CONNECT_TIMEOUT_SECONDS = 60.0

#: How long a tool may run.  Long: a tool is a real action somewhere else, and
#: the alternative to waiting is a call the model cannot tell was cut off.
CALL_TIMEOUT_SECONDS = 300.0

#: How long the *first* ask for a tool list waits for connects already in
#: flight, and it is a compromise between two silences.
#:
#: Answering instantly means a tool is missing from the first turn of a session
#: whenever an upstream is still starting — and "the model did not have the tool
#: yet" is invisible from the outside.  Waiting without a bound means a cold
#: `npx` that has to download itself holds a turn open for a minute.  So: wait,
#: but briefly and only for attempts that have already begun.  This is a cost
#: paid once per daemon, not once per turn.
LIST_SETTLE_SECONDS = 5.0


def sanitise(part: str) -> str:
    """A name a provider will accept as a tool name.

    Providers restrict tool names to letters, digits, underscore and hyphen, and
    reject the whole request over one that is not — so a single upstream tool
    called `read.file` would otherwise 400 every turn of every conversation, with
    a message about the tool list that names no server.  Replacing the character
    is the cheap half of the fix; the other half is that the upstream's own name
    is kept beside it (`UpstreamTool.tool`), because this one can no longer be
    used to address the far end.

    Length is deliberately not trimmed.  A provider's limit is real, but
    truncation has to invent a rule for what happens when two names truncate to
    the same string, and a collision would silently point a tool call at the
    wrong tool — a worse failure than a request the provider refuses loudly.
    """
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in part)


def proxied_name(server: str, tool: str) -> str:
    """What the model calls an upstream's tool: `server__tool`."""
    return f"{sanitise(server)}{SEPARATOR}{sanitise(tool)}"


def mcp_config(settings: ToolServerSettings, *, cwd: str) -> dict[str, Any]:
    """One upstream, in the shape an MCP client takes.

    The standard `mcpServers` document, which is the ecosystem's own config
    format rather than one invented here — so a block copied out of another
    tool's documentation works after the names are checked, and `auth:` and the
    other keys this build does not implement still reach the SDK.

    `cwd` is the fallback working directory for a stdio server.  It matters
    because an entry is likely to name a path, and `.` has to mean something:
    the daemon's own working directory is its runtime folder, which is nobody's
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


#: How a client is built from a transport and a message handler.  A seam rather
#: than a generality: the rebuild-once rule below is about a link that dies
#: *during a call*, and nothing about a real transport can be made to fail on
#: demand from a test.
ClientFactory = Callable[[Any, MessageHandler], Client]


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


class _Watching(MessageHandler):
    """Tells its upstream when the peer says its tool list has changed.

    The whole point is that nothing polls: a server that grows a tool says so,
    the snapshot is dropped, and the next turn's `list_tools` re-reads it.  This
    is what "health is a tool list, not a connection" costs — a flag and a
    callback, rather than a per-server state machine.
    """

    def __init__(self, changed: Callable[[], None]) -> None:
        self._changed = changed

    async def on_tool_list_changed(self, message: Any) -> None:
        logger.debug("the peer says its tool list changed: %s", message)
        self._changed()


class Upstream:
    """One external tool server, and the connection this process keeps to it.

    **The connection is kept, not opened per call.**  For an HTTP server that is
    a handshake saved; for a stdio one it is everything, because the process
    behind it takes seconds to start and spawning it per tool call would make a
    tool call take seconds.

    **State is a snapshot plus an error, not a state machine.**  A server is
    either usable — meaning its tool list is in hand — or it is not, and in the
    second case the useful fact is what it said the last time we asked.  What v1
    calls `tools_ok` falls out of that rather than being tracked separately.

    Everything here is safe to call concurrently: the agent asks for the tool
    list while a turn that is already running is calling a tool.
    """

    def __init__(
        self,
        settings: ToolServerSettings,
        *,
        transport: Callable[[ToolServerSettings], Any],
        client_factory: ClientFactory | None = None,
        required: bool = False,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._make_client = client_factory or make_client
        #: **Ours, rather than somebody else's.**  An optional upstream that
        #: cannot be reached is a tool the model does not have; a required one
        #: that cannot be reached is this system coming apart, and `list_tools`
        #: refuses rather than quietly serving a shorter list.  Nothing in
        #: `tools:` sets this — it is what makes a component a component, and it
        #: is read twice: for that failure rule, and for whether this server's
        #: tools have to ask before the model is given them (`_offered`).
        self.required = required
        self._client: Client | None = None
        self._tools: list[UpstreamTool] = []
        #: The upstream's own name for each advertised tool, so a call is
        #: addressed with the name its server knows rather than the one the
        #: model was given.
        self._names: dict[str, str] = {}
        self._error = ""
        self._ready = False
        self._lock = asyncio.Lock()
        #: The attempt in flight, if any.  Held so that a caller can wait for it
        #: and so that a second one is never started alongside it.
        self._attempt: asyncio.Task[None] | None = None

    # --- what the hub reports ------------------------------------------------

    @property
    def tools(self) -> list[UpstreamTool]:
        """What it listed, as of the last successful read."""
        return self._tools

    @property
    def usable(self) -> bool:
        """Whether its tool list is in hand — asked without waiting for one.

        The synchronous half of :meth:`ready`, and the one `list_tools` uses: a
        caller assembling the model's tool list must not block on a server that
        has been failing, and must not be told a required one is fine.
        """
        return self._ready

    def snapshot(self) -> dict[str, Any]:
        """This server's row in `servers()`, and in a log line."""
        if self._ready:
            state = "ready"
        elif self._attempt is not None and not self._attempt.done():
            state = "connecting"
        else:
            state = "failed" if self._error else "idle"
        return {
            "name": self.settings.name,
            "kind": self.settings.kind,
            "transport": self.settings.transport,
            "state": state,
            "tools": len(self._tools),
            "description": self.settings.description,
            "error": self._error,
            #: Ours rather than somebody else's: the answer to "why did a whole
            #: turn fail over a server that was merely down".
            "required": self.required,
        }

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
                # cannot be reached rather than a hub with a hole in it.
                client: Client | None = None
                try:
                    client = self._make_client(
                        self._transport(self.settings),
                        _Watching(self.invalidate),
                    )
                    await client.__aenter__()
                except asyncio.CancelledError:
                    # Only a shutdown cancels an attempt (see `settle`), and a
                    # half-entered client would hold a connection nobody owns.
                    if client is not None:
                        await self._abandon(client)
                    raise
                except Exception as exc:
                    # `str(exc)` for a stdio server is usually the child's own
                    # last words, which is the only place they are readable —
                    # there is no console for it to have printed to.
                    if client is not None:
                        await self._abandon(client)
                    self._fail(exc)
                    return
                self._client = client
            await self._relist()

    @staticmethod
    async def _abandon(client: Client) -> None:
        """Let go of a client that was never handed to anyone.

        A `close` that raises must not replace the failure being reported, and
        that goes double here: this runs on the error path and on the way out of
        a cancellation.
        """
        with contextlib.suppress(Exception):
            await client.__aexit__(None, None, None)

    async def _relist(self) -> None:
        client = self._client
        if client is None:  # pragma: no cover - only reachable after `_fail`
            return
        try:
            listed = await client.list_tools()
        except Exception as exc:
            await self.disconnect()
            self._fail(exc)
            return
        self._tools = self._offered(listed)
        self._names = {tool.name: tool.tool for tool in self._tools}
        self._error = ""
        self._ready = True
        # Both counts, because the interesting number when a tool is missing is
        # the one that says the server had it all along.
        logger.info(
            "%s: %d of %d tool(s) offered to the model, via %s",
            self.settings.name,
            len(self._tools),
            len(listed),
            self.settings.transport,
        )

    def _offered(self, listed: list[Any]) -> list[UpstreamTool]:
        """The tools of one listing the model may be given.

        **Ours have to ask, and the answer is no until they do**
        (`slife2.audience`).  A component's tools belong to that component's own
        code until one says otherwise, because the ones that would leak —
        `remember`, which writes into any agent's database, `send_message`,
        which drives another conversation — are exactly the ones a model would
        reach for if it could read their descriptions.  Somebody else's tools do
        not ask: the operator asked by writing the entry down.

        `required` is the flag this reads, and it is not a coincidence.  It means
        "slife2 starts this server", and a server we start is one whose tools are
        ours to decide about — which is why nothing under `tools:` sets it.
        """
        if not self.required:
            return [_advertise(self.settings.name, tool) for tool in listed]
        return [
            _advertise(self.settings.name, tool)
            for tool in listed
            if for_the_model(getattr(tool, "meta", None))
        ]

    def invalidate(self) -> None:
        """Forget the tool list, keeping the connection.

        Called from the peer's own `tools/list_changed`, and after a call that
        failed at the transport.  Not `disconnect`: the socket is usually fine
        and only the answer changed, and re-entering the transport on a
        notification would restart every stdio server that ever renames a tool.
        """
        if self._ready:
            self._ready = False

    def _fail(self, exc: Exception) -> None:
        self._ready = False
        self._tools = []
        self._names = {}
        self._error = f"{type(exc).__name__}: {exc}".strip()
        logger.warning("%s is not usable: %s", self.settings.name, self._error)

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
        name: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Run a tool, returning `(text, ok)`.  Never raises.

        **One rebuild, and only for a transport failure.**  A peer that answered
        and refused — an unknown tool, bad arguments, a permission it will not
        grant — has said something the model should read, and rebuilding the
        link would only repeat it.  A link that died mid-call is the one case
        where trying again is not superstition, and it is tried exactly once:
        a server that is down must not turn every call into two timeouts.

        `meta` is the caller's identity, carried across unchanged.  This process
        does not read it and could not: one hub serves every conversation in the
        system over one connection, so whose behalf a call is on is a fact only
        the far end can act on — see `slife2.audience`.
        """
        if not await self.ready():
            return f"{self.settings.name} is not connected: {self._error}", False

        result = await self._call_once(name, arguments, meta)
        if result is not None:
            return result

        logger.info("%s: rebuilding the link and retrying %s", self.settings.name, name)
        await self.disconnect()
        if not await self.ready():
            return f"{self.settings.name} is not connected: {self._error}", False
        result = await self._call_once(name, arguments, meta)
        if result is None:
            return f"{self.settings.name} is not connected: {self._error}", False
        return result

    async def _call_once(
        self,
        name: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> tuple[str, bool] | None:
        """One attempt.  `None` means the transport failed, not the tool."""
        client = self._client
        tool = self._names.get(name)
        if client is None or tool is None:
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
                # model passed a bad argument.
                raise_on_error=False,
                # The caller's identity, forwarded rather than interpreted.  A
                # hub is a proxy, and this is the one thing it passes on that
                # did not come from the model.
                meta=meta,
            )
        except Exception as exc:
            logger.warning("%s: calling %s failed: %s", self.settings.name, name, exc)
            self._error = f"{type(exc).__name__}: {exc}".strip()
            return None
        return flatten(result.content), not result.is_error

    async def close(self) -> None:
        if self._attempt is not None:
            self._attempt.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._attempt
        await self.disconnect()


@dataclass(frozen=True)
class LocalTool:
    """A tool the hub serves itself, because there is nothing else that could.

    A skill is the case.  It is a document in `<data>/skills/`, read when the
    model asks for it: there is no process to start, no credential to hold and
    no address to configure, so the two things an `Upstream` exists for — a
    connection, and a snapshot of what it last listed — have nothing to
    describe.  What is left is a name, the schema the model reads, and the body
    that answers a call.

    **A local tool keeps its own name**, `skill_use` rather than
    `{server}__{tool}`, and it is routed before the proxied names are looked
    at.  So a collision is possible in principle — somebody else's server may
    offer a tool of the same name — and local wins, which is the direction that
    keeps a tool this system guarantees from being shadowed by somebody else's
    configuration.
    """

    tool: UpstreamTool
    run: Callable[[dict[str, Any]], Awaitable[tuple[str, bool]]]


def skills_tools(config: Config) -> list[LocalTool]:
    """The skills source — what the hub reads out of `<data>/skills/`.

    One tool today, and a list because the family is not one tool wide in v1
    either: `skill_list` is the half a model uses to find out what to read.

    **The directory is read on every call, not snapshotted here.**  There is no
    connection to keep and nothing to keep in step, so dropping a skill into the
    folder is the whole of installing one — no restart, and no listing to go
    stale in a running process.  That is the same shape as `servers()`: what a
    caller gets is the truth as of the ask.

    The config is read *here* and once, because that is the one thing that is
    not the folder's: what a skill is given is the operator's answer, resolved
    when the hub started, and a `skills:` entry edited afterwards is a change
    the next start picks up like every other config change.
    """
    environments = {
        name: dict(settings.env) for name, settings in config.skills.items()
    }

    async def run(arguments: dict[str, Any]) -> tuple[str, bool]:
        return await skills.use(arguments, environments=environments)

    return [
        LocalTool(
            tool=UpstreamTool(
                name=skills.USE_TOOL,
                server=SKILLS_SOURCE,
                tool=skills.USE_TOOL,
                description=skills.USE_DESCRIPTION,
                parameters=dict(skills.USE_PARAMETERS),
            ),
            run=run,
        )
    ]


def _advertise(server: str, tool: Any) -> UpstreamTool:
    """One listed tool, named for the model.

    The description carries the server's name because the tool name cannot carry
    everything: `github__search` says where it came from to someone who knows,
    and a model choosing between four tools called `search` needs to be told in
    words.
    """
    description = (tool.description or "").strip()
    return UpstreamTool(
        name=proxied_name(server, tool.name),
        server=server,
        tool=tool.name,
        description=f"[{server}] {description}".strip(),
        parameters=dict(tool.input_schema or {}),
    )


def component_settings(config: Config, name: str) -> ToolServerSettings:
    """One of our own servers, as one upstream of the hub.

    **Written here rather than under `tools:` because it is ours.**  An entry in
    `tools:` is somebody else's process, which slife2 may fail to reach without
    anything being wrong; a component is one slife2 starts, and the hub treats it
    accordingly (`required` on an `Upstream`, which is also what makes its tools
    ask before they are offered to the model).

    What it shares with every other entry is the mechanism — a URL, a connection,
    a tool list — and that is the point of the hub having two *sources* rather
    than two code paths.  The address comes from `servers:`, so a config that
    moves a port moves both halves at once and there is no second place to
    update, and there is no list of components here at all: it is
    `Config.components()`, which is what the launcher starts.
    """
    return ToolServerSettings(
        name=name,
        kind="component",
        url=config.server(name).url,
        description="",
    )


def build_server(
    config: Config,
    *,
    transports: Mapping[str, Callable[[ToolServerSettings], Any]] | None = None,
    client_factory: ClientFactory | None = None,
) -> FastMCP:
    """Build the toolhub.

    `transports` maps an upstream's configured name to something a `Client` can
    be built from — the seam that lets a test drive the whole hub over in-memory
    servers, with no process and no port, while production builds a connection
    from the config entry.  `client_factory` is the narrower seam on top of it,
    for the tests that need a client which misbehaves.

    Nothing is served by this process itself.  Every tool it offers came from a
    server it connected to, the builtins included — which is what makes the
    builtins a normal case rather than a branch in the middle of the tool table.
    """
    directory = data_dir()

    def default_transport(settings: ToolServerSettings) -> Any:
        return mcp_config(settings, cwd=str(directory))

    #: The components first, in the order slife2 starts them, and required — see
    #: `list_tools`.  The hub asks every one of them, including the ones with
    #: nothing to offer the model: which
    #: tools a server has is not knowable without asking, and a second list of
    #: "components worth asking" is a list that goes stale the first time
    #: somebody adds a tool.
    upstreams = [
        *(
            Upstream(
                component_settings(config, name),
                transport=(transports or {}).get(name, default_transport),
                client_factory=client_factory,
                required=True,
            )
            for name in config.components()
            if name != CONFIG_KEY
        ),
        *(
            Upstream(
                settings,
                transport=(transports or {}).get(settings.name, default_transport),
                client_factory=client_factory,
            )
            for settings in config.tool_servers()
        ),
    ]

    #: Every tool this hub serves, by the name the model will use.  Built from
    #: the snapshots at each ask rather than kept in step: an upstream's list
    #: changes underneath, and a second index to maintain is a second index to
    #: get wrong.
    #:
    #: The name maps to the *upstream*, not to a pair of it and its own name for
    #: the tool: the proxy name is what a call is addressed with, and the
    #: upstream is the only party that needs to know what it meant.
    def routes() -> dict[str, Upstream]:
        return {
            tool.name: upstream for upstream in upstreams for tool in upstream.tools
        }

    #: The tools that have no server behind them.  Built once, unlike the
    #: upstreams' snapshots: a local tool's *schema* is a constant, and only the
    #: data its body reads can change — which it re-reads on every call.
    local = skills_tools(config)

    def local_route(name: str) -> LocalTool | None:
        for one in local:
            if one.tool.name == name:
                return one
        return None

    def advertised() -> list[UpstreamTool]:
        return [
            *(tool for upstream in upstreams for tool in upstream.tools),
            *(one.tool for one in local),
        ]

    def begin_connecting() -> None:
        for upstream in upstreams:
            upstream.connecting()

    async def settle() -> None:
        """Let attempts already in flight finish, briefly.  See
        `LIST_SETTLE_SECONDS`.

        **Wait, never cancel.**  The obvious spelling of this is
        `asyncio.gather(*pending)` under `asyncio.timeout`, and it is wrong in a
        way that took a live run to see: on timeout the timeout cancels *this*
        task, the cancellation propagates into the gather, and the connect being
        waited for dies with it — leaving the upstream `idle` with no error and
        no tools, which is the one state that describes nothing.  A caller's
        patience has nothing to do with whether a connection should continue.

        Measured against the real `arxiv` endpoint, the first connect takes
        longer than this window: the first call now answers `connecting` and
        leaves the attempt alone, and the second finds it ready.
        """
        pending = [attempt for upstream in upstreams if (attempt := upstream.attempt)]
        if not pending:
            return
        await asyncio.wait(pending, timeout=LIST_SETTLE_SECONDS)

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, object]]:
        """Connect while the server is up, and drop the links on the way out.

        **Not per client.**  Over the in-memory transport FastMCP runs the
        lifespan once per *session*, so closing here would restart every stdio
        server behind this hub each time a caller disconnected — and the pool is
        the process's, the way the agent server's conversations are its own.
        Closing on the way out is still right: it is the one moment this process
        knows it is finished.
        """
        begin_connecting()
        try:
            yield {}
        finally:
            for upstream in upstreams:
                await upstream.close()

    mcp: FastMCP = house_server(
        SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan
    )

    @mcp.tool
    async def list_tools() -> dict[str, Any]:
        """Every tool the agent may offer the model, with its schema.

        The whole set in one call, components and tool servers together, because
        the caller is assembling a tool list and half a tool list is not a
        smaller answer — it is a wrong one.

        A component's tools are left out unless they declare themselves the
        model's (`slife2.audience`); a tool server's are all here, because the
        operator put the server in the config.  Nothing in this answer says
        which is which — by the time a tool is listed, the question has been
        answered.  Neither rule has anything to ask of a tool the hub serves
        itself: there is no audience but the model's when the tool exists to be
        read by one, and no config entry to opt in with.

        Raises:
            ConnectionError: If a component is not answering.  Deliberately not
                a shorter list instead: a component that is gone is a system
                that has come apart, and a model that has quietly lost `now` and
                `calc` is a failure nobody can see.  A server from the `tools:`
                section is the opposite case and is simply left out — it is the
                operator's configuration and somebody else's process.  See
                DESIGN.md §5 and §8.

        Returns:
            `tools`: one entry per tool, with `name` as the model will call it,
            the `server` it came from, the upstream's own `tool` name, and the
            JSON Schema its arguments must match.
        """
        # Ask for connects that are not running, then give the ones that are a
        # moment: a tool server that is still starting is left out of this
        # answer, and the alternative — waiting on it properly — would hold a
        # turn open for as long as an `npx` takes to install itself.
        begin_connecting()
        await settle()

        missing = [
            row
            for one in upstreams
            if one.required and not one.usable
            for row in (one.snapshot(),)
        ]
        if missing:
            raise ConnectionError(
                "a component is not answering — "
                + "; ".join(
                    f"{row['name']}: {row['error'] or row['state']}" for row in missing
                )
            )
        return {"tools": [tool.to_wire() for tool in advertised()]}

    @mcp.tool
    async def call_tool(
        ctx: Context, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Run one tool by the name `list_tools` gave it.

        Never raises for anything a tool did: a refusal, a bad argument and a
        server that is down all come back as `ok` false with text saying so,
        which is what lets the model read the problem and correct itself.

        Most names are an upstream's, and reach it through the connection this
        process keeps to it.  A tool the hub serves itself — one with no server
        behind it, `skill_use` today — is answered here without leaving the
        process, and a call to one is the only kind that goes nowhere: there is
        no far end to be told whose behalf it is on, so nothing is forwarded and
        nothing is lost.

        A call that is made on behalf of one conversation carries that
        conversation in its `_meta`, and it is forwarded unchanged to whichever
        server ends up running the tool.  Nothing here reads it — one hub serves
        every conversation, so it is the far end that can act on it — and
        nothing here *adds* to it: what the caller said it was is the whole of
        what the far end is told.

        Args:
            name: The tool's advertised name, `server__tool` for an upstream's.
            arguments: Its arguments, as the tool's schema describes them.

        Returns:
            `text` — the result, or why there is none — and `ok`.
        """
        # Local first: a tool with no server behind it is answered here, and
        # this is the only branch in the hub that does not end in a connection.
        one = local_route(name)
        if one is not None:
            text, ok = await one.run(arguments)
            return {"text": text, "ok": ok}

        forwarded = forwarded_client(request_meta(ctx))
        upstream = routes().get(name)
        if upstream is None:
            # Not routable *yet*, which is not the same as unknown: a server may
            # be connecting, or coming back after a drop.  The caller was given
            # this name by `list_tools` a moment ago, so "unknown tool" is the
            # wrong answer for a link that is merely late — and this costs
            # nothing in the ordinary case, because a route that resolves is
            # never asked twice.
            begin_connecting()
            await settle()
            upstream = routes().get(name)
        if upstream is None:
            everything = sorted({*routes(), *(one.tool.name for one in local)})
            known = ", ".join(everything) or "(none)"
            return {
                "text": f"unknown tool {name!r}. Available tools: {known}",
                "ok": False,
            }

        text, ok = await upstream.call(name, arguments, forwarded)
        return {"text": text, "ok": ok}

    @mcp.tool
    async def servers() -> dict[str, Any]:
        """What each configured tool server is doing.

        The answer to "why is my tool missing", which is otherwise a log search:
        a server that failed to start, one that is still starting, and one that
        is connected and simply does not offer what you expected are three
        different problems that look identical from the tool list.

        Returns:
            `servers`: one row per source of tools — `name`, `kind` (`mcp`,
            `rest` or `component`), `transport`, `state` (`ready`, `connecting`,
            `failed` or `idle`), how many `tools` it offers *the model*, the
            `description` it was configured with, the `error` if there is one,
            and `required` — whether slife2 starts it, which is what decides if
            its absence fails a turn or merely shortens the tool list.  A
            component that offers fewer tools than it has is the normal case,
            not a fault: the rest are its own code's, and this count is the one
            the model sees.
        """
        # Settles for the same reason `list_tools` does: this is the answer to
        # "why is my tool missing", and a server that failed to start a moment
        # ago reads as one that is still starting.  The states are different
        # problems and this is the tool that is supposed to tell them apart.
        begin_connecting()
        await settle()
        return {"servers": [upstream.snapshot() for upstream in upstreams]}

    return mcp


def main(argv: list[str] | None = None) -> int:
    args = parse_serve_args(argv, SERVER_NAME)
    configure_logging()
    config_path: Path | None = find_config_path()
    config = load()
    settings = config.server(CONFIG_KEY)
    logger.info(
        "serving %s on http://%s:%d%s (%d component(s), %d tool server(s))",
        SERVER_NAME,
        args.host or settings.host,
        args.port or settings.port,
        settings.path,
        len([name for name in config.components() if name != CONFIG_KEY]),
        len(config.tool_servers()),
    )
    serve(
        build_server(config),
        settings,
        args,
        name=SERVER_NAME,
        config_path=config_path,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
