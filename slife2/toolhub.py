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
                             _func_tool_unload
                                    │
                                    └──MCP──▶  slife2-db  (the tool catalogue)

**The key property is the same one the model backends have, pointed at tools.**
A tool server's credentials are read by this process and by nothing else: the
agent loop cannot leak a token it never had, and `grep` for a provider SDK in
the agent's tree still finds nothing. Switching what tools the model has is
editing a URL or a command in `slife2.yaml`, exactly as switching models is.

**The set is decided here and remembered there.**  Which tools exist, what they
are called and who may call one is this process's to say; the rows, the load
state, the two search indexes and the budget are the db component's to keep, and
every operation on them is a call over MCP (`Catalogue`).  Nothing in this
module opens a database, and nothing in that one knows what a proxy name is —
which is the same line the two components draw everywhere else, and the reason
the next thing that needs the catalogue can have it.

Three sources, one list
-----------------------
The tools come from three places, and this is the only thing that knows all of
them.

**Components** are the servers slife2 starts — builtins, the db, the agent, a
model backend — and each offers its tools to one of two callers.  `now` and
`calc` are for the model; `remember` and `send_message` are for our own code,
called at a moment the code already knows.  **Tool servers** are everybody
else's, under `tools:` and `rest-api:`, and they are for the model by the simple
fact that an operator wrote them down.  **Local tools** are neither: they are
the ones with nothing behind them at all — a skill, which is a document in
`<data>/skills/`, and the three that find and load tools — and the hub serves
those itself, for the reason given below.

Which source a tool came from is not what decides who may call it — which
*caller* it is for does, and that is said on the tool itself (`slife2.audience`)
rather than in its name, in this file, or in the config.  **A component's tools
belong to that component's own code until one of them says otherwise**, so
`remember` stays where it was and `now` carries the mark, while an entry under
`tools:` needs no mark at all: the operator opted in by writing the entry.  The
list of components is not written down here either — it is `Config.components()`,
which is what the launcher starts, so the hub cannot drift from the set of
processes that exist.

The list the model gets is not everything
-----------------------------------------
**What goes out with a request is the tools the model has *loaded*, not the tools
that exist.**  Two tools find and load (`tool_search`, `func_tool_load`), one
reads a skill, and everything else arrives on demand: a server with ninety tools
is a catalogue entry until one of them is wanted, and `autoload: true` on an
entry is the operator saying this one is different.  The catalogue is where that
state lives and the db is where it is kept, so a tool loaded in one conversation
is loaded for the next, and a tool loaded yesterday is still loaded after a
restart — the two things a snapshot in this process could never do.

The budget that bounds it is enforced by the *harness*, not by the gate:
`_func_tool_unload` is called by the agent server before it saves a turn, and the
names it unloaded come back to the caller.  The leading underscore is the
system's mark for a tool the machinery calls rather than one a model chooses, and
it is why that name is never in a model's tool list — see `_func_tool_unload`.

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
in `servers()`, it would not have a connection to fail, and it would not be a row
in the catalogue the model's search reads.

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

They are still *rows*, though — owned by this component, like every other tool's
is owned by its source.  That is what makes one query enough to answer what the
model may call, with no list of exceptions kept beside it here.

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

The failure rules, all four
---------------------------
* **A component that is not answering is a broken system.**  Everything under
  `servers:` is ours: slife2 starts it, the launcher refuses to bring the system
  up without it, and a hub that cannot read its tool list refuses to hand one out
  rather than serving the model a shorter one — a model that has quietly lost
  `now` and `calc` is a failure nobody can see.  It is a *flag on the
  connection* rather than a branch in the tool table, and which section a server
  was configured in is the whole of the difference: a component is required, an
  upstream is not.
* **An upstream missing is not.**  An external server is the operator's
  configuration and somebody else's process; it can be slow, paid, or down
  without our system being broken.  It is reported by `servers()`, its rows stay
  in the catalogue with `error` on them — so a search can still say the tool
  exists and its owner is not answering — and it is left out of the list.  The
  next ask tries again: no timer, no backoff loop.
* **The catalogue missing is a broken system too.**  Every answer this process
  gives about the tool set is a call to the db, so a db that is not there is a
  turn that fails with that said out loud.  It is deliberately *not* reported as
  "that tool server is broken": the next ask would then look in the wrong place.
* **An upstream refusing a call is data.**  It comes back as text with `ok`
  false, which the model reads and acts on.  The loop's error path is a feedback
  channel, not a failure mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.client.messages import MessageHandler

from slife2 import skills
from slife2.audience import for_the_model, forwarded_client, request_meta
from slife2.config import (
    DB_KEY,
    DB_SERVER_NAME,
    Config,
    ToolServerSettings,
    find_config_path,
    load,
)
from slife2.mcp_server import (
    close_server,
    configure_logging,
    house_server,
    open_server,
    parse_serve_args,
    serve,
    tool_payload,
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
#:
#: It is also the name the hub's **own** tools are catalogued under.  They are
#: this component's tools, served by this process: `tool_search`, the two
#: loaders, and `skill_use`.  Being rows like everything else is what makes one
#: query enough to answer "what may the model call", and it is why nothing here
#: has to remember a list of its own.
CONFIG_KEY = "toolhub"

#: The two tools this process serves a *model* to manage its own tool list.
#: Named here because they are also what the hub will not let go of: see
#: `ALWAYS_LOADED`.
TOOL_SEARCH = "tool_search"
FUNC_TOOL_LOAD = "func_tool_load"

#: The harness's own trim, and the one tool it calls on this server's *API*
#: rather than through `call_tool`.  The leading underscore is the convention:
#: **a name beginning with `_` is a harness tool** — the machinery calls it, not
#: the model — and this one is not in the catalogue at all, for the reason the
#: hub's other API tools are not: what the catalogue holds is the model's tools,
#: and this is not one of them.  See `build_server`'s `_func_tool_unload`.
FUNC_TOOL_UNLOAD = "_func_tool_unload"

#: What this process guarantees: the three tools it serves a model and the one
#: the harness drives.  Always available, never evicted, and never unloaded —
#: the first three are how a tool is found and loaded, so a budget that could
#: take them away would leave the model holding a set it cannot change.
ALWAYS_LOADED = frozenset(
    {
        TOOL_SEARCH,
        FUNC_TOOL_LOAD,
        FUNC_TOOL_UNLOAD,
        skills.USE_TOOL,
    }
)

#: How long a hop to the db — which is to say, to the tool catalogue — may take.
#: Generous, for the reason the connect timeout is: the db's answer to a
#: reconcile can include embedding every tool a server just offered, and a
#: deadline that fired during a first embedding would read as a broken database.
CATALOGUE_TIMEOUT_SECONDS = 120.0

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


class CatalogueUnavailable(ConnectionError):
    """The tool catalogue is not answering: a component that is gone.

    Deliberately not the same thing as a source failing.  An upstream that will
    not start is one tool server the operator can look at; a catalogue that is
    not there is this system come apart, and it fails the tool list rather than
    shortening it — the same rule a missing component has always had.
    """


class Catalogue:
    """The hub's side of the tool catalogue, which `slife2-db` owns.

    **Every database operation the hub makes goes through here**, and through
    MCP: the hub holds no rows, no query and no budget — which tools exist is
    this process's decision, and what is known about them is the db's record.
    One loopback hop per call, on a path that already exists.

    The three kinds of answer are kept apart, because they mean different things
    to the caller: a payload (the db answered), `CatalogueUnavailable` (the db
    is not there), and everything else — a refusal the db phrased, like two
    sources claiming one name — which is that caller's own problem to report.

    **The connection is opened on first use, and once.**  Everything that holds
    a `Catalogue` is built before there is a loop to open anything on: the
    upstreams, the hub's own tools, the server itself.  So what is handed round
    is this object and not a client, and whoever asks first pays for the
    connection — under a lock, so that two of them cannot open two.
    """

    def __init__(self, connect: Callable[[], Awaitable[Client]]) -> None:
        self._connect = connect
        self._client: Client | None = None
        self._opening = asyncio.Lock()

    async def client(self) -> Client:
        if self._client is None:
            async with self._opening:
                if self._client is None:
                    self._client = await self._connect()
        return self._client

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            client = await self.client()
            result = await client.call_tool(tool, arguments)
        except CatalogueUnavailable:
            raise
        except Exception as exc:
            # The db is a component: not answering is a system that has come
            # apart, and it is named here rather than surfaced as whatever the
            # transport happened to say.
            raise CatalogueUnavailable(
                f"the tool catalogue ({DB_SERVER_NAME}) is not answering "
                f"{tool}: {type(exc).__name__}: {exc}"
            ) from exc
        return tool_payload(result)

    async def merge(
        self, source: str, category: str, tools: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Record a source's whole tool list.  See `slife2.db.ToolStore.merge`."""
        return await self._call(
            "tool_merge",
            {"source": source, "category": category, "tools": list(tools)},
        )

    async def source_state(self, source: str, state: str) -> None:
        """Record the verdict on one source: `enabled` or `error`."""
        await self._call("tool_source_state", {"source": source, "state": state})

    async def injectable(self, sources: Sequence[str]) -> dict[str, Any]:
        """The tools the model may be given now.

        `sources` is the caller's because liveness is: the hub is the process
        holding the connections, and a source is live when its tool list is in
        hand.  See `ToolStore.injectable`.
        """
        return await self._call("tool_injectable", {"sources": list(sources)})

    async def evict(self, sources: Sequence[str]) -> list[str]:
        """Trim the loaded set to the configured budget; name what it took out."""
        found = await self._call("tool_evict", {"sources": list(sources)})
        unloaded = found.get("unloaded")
        return [str(name) for name in unloaded] if isinstance(unloaded, list) else []

    async def route(self, name: str) -> dict[str, Any] | None:
        """The row for one advertised name, or `None` if there is no such tool."""
        found = (await self._call("tool_route", {"name": name})).get("tool")
        return found if isinstance(found, dict) else None

    async def sources(self) -> dict[str, dict[str, int]]:
        """Per source: how many tools it has, and how many the model holds."""
        found = (await self._call("tool_sources", {})).get("sources")
        return found if isinstance(found, dict) else {}

    async def search(self, **arguments: Any) -> dict[str, Any]:
        """Both legs of a tool search, fused.  See `ToolStore.search`."""
        return await self._call("tool_search", arguments)

    async def set_load(self, name: str, load_status: str) -> dict[str, Any]:
        """Move one tool in or out of the model's list.

        The answer is the db's *fact* — `loaded`, `unloaded`, `already`,
        `unknown`, `no_load_state`, `disabled`, `error` — and the sentence a
        model reads is built from it here, because a store that wrote prose
        would be the second place model-facing text lived.
        """
        return await self._call(
            "tool_set_load", {"name": name, "load_status": load_status}
        )

    async def touch(self, name: str) -> None:
        """Mark one tool *called*, for the eviction order.  Best-effort.

        **The budget evicts by this and not by the load stamp** — see
        `slife2.db.ToolStore.evict` — and this is the only place it is written,
        so this call is what makes "the least recently used tool goes" true
        rather than merely intended.

        Suppressed on failure: this runs after a call that already happened, and
        turning a bookkeeping stamp into the error a model reads would report
        the wrong thing — the tool *did* run.
        """
        with contextlib.suppress(Exception):
            await self._call("tool_touch", {"name": name})

    async def close(self) -> None:
        if self._client is not None:
            await close_server(self._client)
            self._client = None


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
        catalogue: Catalogue,
        client_factory: ClientFactory | None = None,
        required: bool = False,
    ) -> None:
        self.settings = settings
        self._transport = transport
        #: Where this server's tools are *recorded*.  An upstream holds a
        #: connection and a verdict and no tool table: what it last listed is a
        #: fact about the catalogue, and a second copy here would be a second
        #: thing to keep in step — which is what replaced the snapshot this
        #: class used to keep.
        self._catalogue = catalogue
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
        self._error = ""
        self._ready = False
        self._lock = asyncio.Lock()
        #: The attempt in flight, if any.  Held so that a caller can wait for it
        #: and so that a second one is never started alongside it.
        self._attempt: asyncio.Task[None] | None = None

    # --- what the hub reports ------------------------------------------------

    @property
    def usable(self) -> bool:
        """Whether its tool list is in hand — asked without waiting for one.

        The synchronous half of :meth:`ready`, and the one `list_tools` uses: a
        caller assembling the model's tool list must not block on a server that
        has been failing, and must not be told a required one is fine.

        **This is the whole of what "live" means for the catalogue too.**  The
        gate is asked for the tools of the sources whose lists are in hand, so a
        server that is down contributes nothing without the db having to know
        anything about connections — see `Catalogue.injectable`.
        """
        return self._ready

    def snapshot(self, counts: Mapping[str, int] | None = None) -> dict[str, Any]:
        """This server's row in `servers()`, and in a log line.

        `counts` is the catalogue's answer for this source — how many tools it
        has and how many the model is holding — and it is passed in rather than
        looked up here because this method is also the one `list_tools` uses for
        its error message, on a path where the catalogue has already been asked.
        """
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
            #: What it last offered, and how much of that the model has now.  A
            #: source with ninety tools and none loaded is a healthy server the
            #: model has not asked anything of — and that is a different answer
            #: from a server that has stopped offering them.
            "tools": int((counts or {}).get("tools", 0)),
            "loaded": int((counts or {}).get("loaded", 0)),
            #: Wanted every turn: this server's tools start loaded and are never
            #: evicted (`autoload` in the config, or `required` below, which
            #: means the same thing for our own servers).
            "autoload": self.settings.autoload or self.required,
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
        """Ask for a tool list and record it, or fail this source.

        **The listing goes to the catalogue and is not kept here.**  What came
        back is the whole truth about this source, so it is merged — added,
        updated, deleted, or left alone — and the hub's own copy of it is
        nothing at all: `list_tools` reads the catalogue and `call_tool` routes
        through it, which is what makes the row the one place a tool is
        described.

        Both failures below end the same way for this source and differently for
        the system.  A *transport* failure is this server's problem, and it is
        recorded as such.  A *catalogue* that is not answering is a component
        gone, and it is raised: it must not be reported as "that tool server is
        broken", because the next ask would then look in the wrong place.
        """
        client = self._client
        if client is None:  # pragma: no cover - only reachable after `_fail`
            return
        try:
            listed = await client.list_tools()
        except Exception as exc:
            await self.disconnect()
            self._fail(exc)
            return
        offered = self._offered(listed)
        try:
            await self._catalogue.merge(
                self.settings.name,
                self.settings.kind,
                [_row_of(self.settings.name, tool) for tool in offered],
            )
        except CatalogueUnavailable:
            await self.disconnect()
            raise
        except Exception as exc:
            # The db answered and refused: a name another source owns, say.  That
            # is this source's list that cannot be recorded, so it is this
            # source that is unusable until it is fixed.
            await self.disconnect()
            self._fail(exc)
            return
        self._error = ""
        self._ready = True
        # Both counts, because the interesting number when a tool is missing is
        # the one that says the server had it all along.
        logger.info(
            "%s: %d of %d tool(s) offered to the model, via %s",
            self.settings.name,
            len(offered),
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

        Forgetting it means the source is no longer *live*, so its rows stop
        being injected until the next ask re-lists it — which is what "the
        snapshot is dropped" has always meant, and it is the same flag: what a
        caller gets from the catalogue is the tools of the sources whose lists
        are in hand.
        """
        if self._ready:
            self._ready = False

    def _fail(self, exc: Exception) -> None:
        """This source is unusable, and the catalogue is told so.

        The verdict is recorded on the rows rather than only held here, which is
        the whole reason the column exists: a server that is down keeps its tool
        rows, so `tool_search` can still say the tool exists and the thing that
        owns it is not answering — where before, a failed server's list was
        simply gone and nothing could say what it used to offer.

        Best-effort, and deliberately: a catalogue that cannot take the verdict
        is itself the failure, and the next `list_tools` says so in its own
        words.  A bookkeeping call must not replace the reason this one failed.
        """
        self._ready = False
        self._error = f"{type(exc).__name__}: {exc}".strip()
        logger.warning("%s is not usable: %s", self.settings.name, self._error)
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().create_task(
                self._catalogue.source_state(self.settings.name, "error")
            )

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

        `tool` is **the far end's own name for it**, which is not the name the
        model used: the advertised name is sanitised and carries the server in
        front of it, and only the catalogue row knows both.  The hub asks for
        the row and hands this the half that addresses the far end.

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
                # model passed a bad argument.
                raise_on_error=False,
                # The caller's identity, forwarded rather than interpreted.  A
                # hub is a proxy, and this is the one thing it passes on that
                # did not come from the model.
                meta=meta,
            )
        except Exception as exc:
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


@dataclass(frozen=True)
class LocalTool:
    """A tool the hub serves itself, because there is nothing else that could.

    A skill is the case, and so are the three that find and load tools.  None of
    them has a process to start, a credential to hold or an address to
    configure: what an `Upstream` exists for — a connection — has nothing to
    describe.  What is left is a name, the schema the model reads, and the body
    that answers a call.

    **A local tool keeps its own name**, `skill_use` rather than
    `{server}__{tool}`, and it is routed before the catalogue is asked.  So a
    collision is possible in principle — somebody else's server may offer a tool
    of the same name — and local wins, which is the direction that keeps a tool
    this system guarantees from being shadowed by somebody else's configuration.

    Its *row*, though, is an ordinary one, owned by this component: that is what
    makes the catalogue the single answer to "what may the model call", instead
    of that answer plus a list of exceptions kept here.
    """

    tool: UpstreamTool
    run: Callable[[dict[str, Any]], Awaitable[tuple[str, bool]]]


#: What `tool_search` takes.  `query` is required and may be **empty**, which
#: browses rather than searching — v1's shape, and the reason it is required at
#: all is that a search with no query is a different request from a search whose
#: query was forgotten.
TOOL_SEARCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "What the tool is for, in words and phrases. Leave it empty to "
                "browse what is installed instead."
            ),
        },
        "category": {
            "type": "string",
            "enum": ["component", "mcp", "rest", "skill"],
            "description": "One kind of tool only.",
        },
        "source_id": {
            "type": "string",
            "description": "One server's or component's tools only.",
        },
        "status": {
            "type": "string",
            "enum": ["enabled", "disabled", "error"],
            "description": (
                "`disabled` is switched off in the config; `error` means the "
                "thing that owns it is not answering."
            ),
        },
        "load_status": {
            "type": "string",
            "enum": ["loaded", "unloaded", "n/a"],
            "description": "`loaded` is what you already have in your tool list.",
        },
        "limit": {"type": "integer", "description": "How many results at most."},
    },
    "required": ["query"],
}

TOOL_SEARCH_DESCRIPTION = (
    "Find a tool by what it does — by keyword and by meaning, in one search. "
    "Your tool list holds only the tools you have loaded; this searches the "
    "whole catalogue of everything installed, including tools whose server is "
    "switched off or is not answering. Load what you need with func_tool_load. "
    "An empty query lists what is installed."
)

FUNC_TOOL_LOAD_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The tool names, as tool_search reports them — "
                "'{server}__{tool}' for a server's, bare for one of the hub's. "
                "One name or several: ['arxiv__search'] and "
                "['now', 'browser__open'] are the same kind of request. A single "
                "name given as a bare string is understood too."
            ),
        }
    },
    "required": ["names"],
}

FUNC_TOOL_LOAD_DESCRIPTION = (
    "Load one tool, or several at once, into your tool list so you can call "
    "them. Find them with tool_search first. They are there from your next step "
    "— the tool list is rebuilt before every request. Loading is about seeing "
    "them: a name you found with tool_search can be called without loading, and "
    "the least recently used tools are unloaded again when the list grows past "
    "its cap. Each name gets its own line in the answer."
)


def local_tools(config: Config, catalogue: Catalogue) -> list[LocalTool]:
    """What this process serves a model itself: the three it can manage tools with.

    **Read a skill** — `skill_use`, which is v1's pinned reader and the read
    half of that family.  The directory it reads is `<data>/skills/`, and it is
    read on every call rather than snapshotted: there is no connection to keep
    and nothing to keep in step, so dropping a skill into the folder is the
    whole of installing one.

    **Find a tool** — `tool_search`, the hybrid search over the catalogue.  The
    hub serves it rather than the db for the reason it serves `skill_use`: the
    model's tool list is this process's surface, and a name the model calls is
    either `{server}__{tool}` or one of these.  What the tool *does* is the db's:
    the two legs, the fusion and the filters all happen there, and this half
    only turns rows into text a model reads.

    **Load one** — `func_tool_load`, whose answer is likewise the db's verdict
    phrased for a model.

    **Trim the list** — `_func_tool_unload`, which is the harness's and not the
    model's (the underscore says so, and the gate agrees).  The agent server
    calls it before it saves a turn: over the configured threshold, the least
    recently used tools go, and **the caller is told which ones** — that is the
    point of the trim being a call rather than something the gate does quietly,
    because the harness is the party that has to know what the model just lost.

    The config is read *here* and once, because that is the one thing that is
    not the folder's: what a skill is given is the operator's answer, resolved
    when the hub started, and a `skills:` entry edited afterwards is a change
    the next start picks up like every other config change.
    """
    environments = {
        name: dict(settings.env) for name, settings in config.skills.items()
    }

    async def use_skill(arguments: dict[str, Any]) -> tuple[str, bool]:
        return await skills.use(arguments, environments=environments)

    async def search(arguments: dict[str, Any]) -> tuple[str, bool]:
        found = await catalogue.search(
            query=str(arguments.get("query") or ""),
            category=str(arguments.get("category") or ""),
            source_id=str(arguments.get("source_id") or ""),
            status=str(arguments.get("status") or ""),
            load_status=str(arguments.get("load_status") or ""),
            limit=int(arguments.get("limit") or 10),
        )
        return _results_as_text(found), True

    async def load(arguments: dict[str, Any]) -> tuple[str, bool]:
        names = _names_of(arguments)
        if not names:
            return "func_tool_load needs at least one tool name", False
        lines: list[str] = []
        ok = True
        for name in names:
            answer = await catalogue.set_load(name, "loaded")
            text, loaded = _load_as_text(name, answer)
            lines.append(text)
            ok = ok and loaded
        return "\n".join(lines), ok

    return [
        LocalTool(
            tool=UpstreamTool(
                name=skills.USE_TOOL,
                server=CONFIG_KEY,
                tool=skills.USE_TOOL,
                description=skills.USE_DESCRIPTION,
                parameters=dict(skills.USE_PARAMETERS),
            ),
            run=use_skill,
        ),
        LocalTool(
            tool=UpstreamTool(
                name=TOOL_SEARCH,
                server=CONFIG_KEY,
                tool=TOOL_SEARCH,
                description=TOOL_SEARCH_DESCRIPTION,
                parameters=TOOL_SEARCH_PARAMETERS,
            ),
            run=search,
        ),
        LocalTool(
            tool=UpstreamTool(
                name=FUNC_TOOL_LOAD,
                server=CONFIG_KEY,
                tool=FUNC_TOOL_LOAD,
                description=FUNC_TOOL_LOAD_DESCRIPTION,
                parameters=FUNC_TOOL_LOAD_PARAMETERS,
            ),
            run=load,
        ),
    ]


async def unload_tools(
    catalogue: Catalogue, sources: Sequence[str], names: Sequence[str]
) -> dict[str, Any]:
    """Take tools out of the model's list — the harness's trim.

    **Two ways to say it, and one meaning.**  With names, exactly those go, and
    the four the system works by are refused.  With no names, the *budget* is
    enforced: the catalogue unloads whatever is over `tool_load.threshold`,
    least recently used first.

    **The names that moved are the answer**, and that is the whole reason the
    trim is a call rather than something the gate does quietly: the caller is
    the harness — the agent server, at a turn boundary — and *what the model
    just lost* is a fact only this side can tell it.

    Returns:
        `unloaded`, `refused` (named but not unloadable: one of
        `ALWAYS_LOADED`), and `not_loaded` (named but already out of the list).
    """
    if not names:
        return {
            "unloaded": await catalogue.evict(sources),
            "refused": [],
            "not_loaded": [],
        }
    unloaded: list[str] = []
    refused: list[str] = []
    not_loaded: list[str] = []
    for name in names:
        if name in ALWAYS_LOADED:
            refused.append(name)
            continue
        outcome = str((await catalogue.set_load(name, "unloaded")).get("outcome") or "")
        if outcome == "unloaded":
            unloaded.append(name)
        elif outcome == "already":
            not_loaded.append(name)
        else:
            refused.append(name)
    return {"unloaded": unloaded, "refused": refused, "not_loaded": not_loaded}


def _unload_as_text(found: Mapping[str, Any]) -> str:
    """What a trim did, said in a sentence — for a log line and for a person."""
    unloaded = [str(name) for name in found.get("unloaded") or []]
    parts = [
        f"{len(unloaded)} tool(s) unloaded: " + ", ".join(unloaded)
        if unloaded
        else "nothing to unload: the list is within its budget"
    ]
    refused = [str(name) for name in found.get("refused") or []]
    if refused:
        parts.append("refused (the system needs them): " + ", ".join(refused))
    not_loaded = [str(name) for name in found.get("not_loaded") or []]
    if not_loaded:
        parts.append("already out of the list: " + ", ".join(not_loaded))
    return "; ".join(parts)


def _names_of(arguments: Mapping[str, Any]) -> list[str]:
    """The tool names one `func_tool_load` call is about.

    **One name or several, and a bare string is one name.**  The schema asks for
    a list, because that is the shape every provider assembles reliably — but a
    model that passes `"arxiv__search"` instead of `["arxiv__search"]` is asking
    for exactly the same thing, and refusing it would be pedantry with a
    round trip as the price.  Duplicates are dropped: loading a name twice is
    one request.
    """
    given = arguments.get("names")
    if isinstance(given, str):
        given = [given]
    if not isinstance(given, Sequence):
        return []
    return [str(name) for name in dict.fromkeys(given) if str(name).strip()]


def _results_as_text(found: Mapping[str, Any]) -> str:
    """A tool search's rows, as the text a model reads.

    Name and provenance first, because that is what a call is made of, then the
    description — which is the whole of what a model chooses by and is *not* cut
    short: a tool's own description is a sentence or two, and a model deciding
    from half of one is a model guessing.

    A row that is not usable says so where its state would otherwise be silent:
    "switched off" and "its server is not answering" are answers the model can
    act on, and are exactly what v1 kept rows for a failing server to be able to
    say.
    """
    rows = found.get("results") or []
    if not rows:
        return (
            "Nothing matched. Try fewer or different words — or call tool_search "
            "with an empty query to see what is installed."
        )
    lines: list[str] = []
    for row in rows:
        state = [str(row.get("category") or ""), str(row.get("source_id") or "")]
        status = str(row.get("status") or "")
        if status and status != "enabled":
            state.append(f"NOT USABLE: {status}")
        elif row.get("load_status") == "loaded":
            state.append("loaded")
        similarity = row.get("similarity")
        if isinstance(similarity, float):
            state.append(f"similarity {similarity:.2f}")
        lines.append(
            f"{row.get('name')}  ({', '.join(part for part in state if part)})"
        )
        lines.append(f"    {row.get('description')}")
    if not found.get("browsed"):
        lines.append("")
        lines.append("Call func_tool_load(name) to put one of these in your list.")
    return "\n".join(lines)


def _load_as_text(name: str, answer: Mapping[str, Any]) -> tuple[str, bool]:
    """What the db said about one load, said to a model.

    The outcome is a fact and this is the sentence: `ok` is false only when the
    tool did not end up in the model's list, so a refusal reaches the transcript
    as a failure and "it was already there" does not.
    """
    outcome = str(answer.get("outcome") or "")
    if outcome == "loaded":
        return f"{name} is in your tool list from the next step.", True
    if outcome == "already":
        return f"{name} is already in your tool list.", True
    if outcome == "unknown":
        return (
            f"unknown tool {name!r} — find it with tool_search, which reports "
            f"names exactly as they are called",
            False,
        )
    if outcome == "no_load_state":
        return (
            f"{name!r} has no load state: it is a document (a skill), read by "
            f"calling it, not a tool to load",
            False,
        )
    if outcome == "disabled":
        return (
            f"{name!r} is switched off in the config, so it cannot be loaded — "
            f"its server has `enabled: false`, which is the operator's decision "
            f"and not a fault",
            False,
        )
    if outcome == "error":
        return (
            f"{name!r} cannot be loaded: the server that owns it is not "
            f"answering. It is in the catalogue, and `servers` says why",
            False,
        )
    return f"{name!r} could not be loaded ({outcome or 'no answer'})", False


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


def _row_of(server: str, tool: UpstreamTool) -> dict[str, Any]:
    """One advertised tool, as the catalogue stores it.

    The description is stored **raw** and the `[server]` prefix is put on at
    advertisement time (`_from_row`), because the row is also what a search
    reads and indexes: a prefix repeated in every result is noise in the text a
    model searches by, and the row already says which source it came from.

    An empty `schema` means "nothing to declare" and the store turns it into its
    own sentinel — the hub has no opinion about how a store spells absence.
    """
    return {
        "name": tool.name,
        "description": tool.description,
        "remote_name": tool.tool,
        "schema": json.dumps(tool.parameters) if tool.parameters else "",
    }


def _from_row(row: Mapping[str, Any]) -> UpstreamTool:
    """One catalogue row, as the model's tool list wants it.

    The inverse of `_row_of`, and where the `[server]` prefix comes back — so
    what the model reads is what it always read, whether the row was written by
    this hub, by another one, or by a build from last week.
    """
    server = str(row.get("source_id") or "")
    description = str(row.get("description") or "")
    return UpstreamTool(
        name=str(row.get("name") or ""),
        server=server,
        tool=str(row.get("remote_name") or row.get("name") or ""),
        description=f"[{server}] {description}".strip() if server else description,
        parameters=_parameters(str(row.get("schema") or "")),
    )


def _parameters(schema: str) -> dict[str, Any]:
    """A stored schema, as the parameter mapping the model's tool list takes.

    Tolerant on purpose: the column holds a JSON document *or* a store's
    sentinel for "nothing to declare", and a tool whose schema cannot be read is
    a tool with no arguments rather than a reason to fail the whole list.
    """
    try:
        found = json.loads(schema)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


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
    catalogue_client: Client | None = None,
) -> FastMCP:
    """Build the toolhub.

    `transports` maps an upstream's configured name to something a `Client` can
    be built from — the seam that lets a test drive the whole hub over in-memory
    servers, with no process and no port, while production builds a connection
    from the config entry.  `client_factory` is the narrower seam on top of it,
    for the tests that need a client which misbehaves.  `catalogue_client` is
    the third: a connection to the tool catalogue, which a test supplies as the
    real db server over the in-memory transport.

    **The hub's own tools are rows too.**  `tool_search`, `func_tool_load` and
    `skill_use` are this component's, and the hub merges them into the catalogue
    when it starts — so one query answers "what may the model call", with no
    list of exceptions kept beside it.  What has a *body* is still only known
    here: the row says what the tool is, and `local_route` says what running it
    means.
    """
    directory = data_dir()

    def default_transport(settings: ToolServerSettings) -> Any:
        return mcp_config(settings, cwd=str(directory))

    async def open_catalogue() -> Client:
        """A connection to the db — the component that holds the catalogue.

        A peer that is not there **raises**, like every other peer in this
        system: the hub cannot say what the model may call without it, so a
        missing db is a system that has come apart rather than a hub working
        with fewer abilities.  `CatalogueUnavailable` is that answer, and
        `list_tools` passes it on.

        `catalogue_client` is the seam a test uses to hand over the real db
        server over the in-memory transport — no process, no port, and the same
        tools either way.  `transports` is the same seam one level up: a wired
        `db` entry is a transport, and a hub built for a test has one.
        """
        if catalogue_client is not None:
            return catalogue_client
        wired = (transports or {}).get(DB_KEY)
        if wired is not None:
            client = make_client(
                wired(component_settings(config, DB_KEY)), MessageHandler()
            )
            await client.__aenter__()
            return client
        return await open_server(
            config.server(DB_KEY).url,
            name=DB_SERVER_NAME,
            fallback_tool="tool_merge",
            timeout=CATALOGUE_TIMEOUT_SECONDS,
        )

    #: The catalogue, and the one connection it opens when something first asks.
    catalogue = Catalogue(open_catalogue)

    #: The components first, in the order slife2 starts them, and required — see
    #: `list_tools`.  The hub asks every one of them, including the ones with
    #: nothing to offer the model: which
    #: tools a server has is not knowable without asking, and a second list of
    #: "components worth asking" is a list that goes stale the first time
    #: somebody adds a tool.
    upstreams: list[Upstream] = [
        *(
            Upstream(
                component_settings(config, name),
                transport=(transports or {}).get(name, default_transport),
                catalogue=catalogue,
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
                catalogue=catalogue,
                client_factory=client_factory,
            )
            for settings in config.tool_servers()
        ),
    ]

    def by_source(name: str) -> Upstream | None:
        for one in upstreams:
            if one.settings.name == name:
                return one
        return None

    def live_sources() -> list[str]:
        """The sources whose tool lists are in hand, plus this one.

        **Liveness is the hub's to know** — it is the process holding the
        connections — and this is the same fact `usable` already is, handed to
        the catalogue so that a server which is down contributes nothing without
        the db having to know anything about connections.

        The hub's own name is in the list because its tools have no connection
        that could be down: they are functions in this process, so its source is
        live whenever the process is.
        """
        return [one.settings.name for one in upstreams if one.usable] + [CONFIG_KEY]

    #: The tools this process serves itself.  Built once — a local tool's
    #: *schema* is a constant, and only the data its body reads can change,
    #: which it re-reads on every call.  `live_sources` is handed in because the
    #: trim asks the catalogue for what the model is *holding*, and only this
    #: process knows which sources are answering.
    local: list[LocalTool] = local_tools(config, catalogue)

    def local_route(name: str) -> LocalTool | None:
        for one in local:
            if one.tool.name == name:
                return one
        return None

    def advertised(found: Mapping[str, Any]) -> list[UpstreamTool]:
        """The rows the catalogue offered, as the model's tool list.

        Nothing is added here for the hub's own tools: they are rows like
        everything else, which is the property worth having — one query, one
        naming rule, and no second list to disagree with the first.
        """
        rows = found.get("tools")
        return [_from_row(row) for row in rows] if isinstance(rows, list) else []

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

    async def start() -> None:
        """What the hub does before it serves anything.

        **The components are asked for their tools, and the hub's own tools are
        written down.**  Both are merges into the catalogue: the upstreams find
        theirs by connecting, and this process's three are known without
        connecting to anything — so they are recorded here, once, and are rows
        like every other tool from then on.

        Order matters once: this has to happen before the first `list_tools`, or
        the model's first answer would be missing the tool that finds tools.
        """
        category = "component"
        await catalogue.merge(
            CONFIG_KEY, category, [_row_of(CONFIG_KEY, one.tool) for one in local]
        )
        begin_connecting()

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
        await start()
        try:
            yield {}
        finally:
            for upstream in upstreams:
                await upstream.close()
            await catalogue.close()

    mcp: FastMCP = house_server(
        SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan
    )

    @mcp.tool
    async def list_tools() -> dict[str, Any]:
        """Every tool the agent may offer the model **now**, with its schema.

        **The loaded set, not the whole catalogue.**  A tool the model has not
        loaded is not in this answer, and it is found with `tool_search` and put
        here with `func_tool_load` — which is what keeps the list that goes out
        with every request from growing to the size of everything installed.
        The three tools that do the finding and the loading are always in it:
        they are the mechanism, and a budget that could take them away would
        leave the model holding a set it cannot change.

        A component's tools are left out unless they declare themselves the
        model's (`slife2.audience`); a tool server's are all offered, because the
        operator put the server in the config.  Both were decided when the
        listing was merged, so nothing in this answer says which is which — by
        the time a tool is listed, the question has been answered.

        Raises:
            ConnectionError: If a component — or the catalogue itself — is not
                answering.  Deliberately not a shorter list instead: a component
                that is gone is a system that has come apart, and a model that
                has quietly lost `now` and `calc` is a failure nobody can see.  A
                server from the `tools:` section is the opposite case and is
                simply left out — it is the operator's configuration and somebody
                else's process, and its tools are in the catalogue with `error`
                on them.  See DESIGN.md §5 and §8.

        Returns:
            `tools`: one entry per tool the model may call, with `name` as the
            model will call it, the `server` it came from, the upstream's own
            `tool` name, and the JSON Schema its arguments must match.
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
        found = await catalogue.injectable(live_sources())
        return {"tools": [tool.to_wire() for tool in advertised(found)]}

    @mcp.tool
    async def call_tool(
        ctx: Context, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Run one tool by the name `list_tools` gave it.

        Never raises for anything a tool did: a refusal, a bad argument and a
        server that is down all come back as `ok` false with text saying so,
        which is what lets the model read the problem and correct itself.

        **The route comes from the catalogue**, which is the point of it: which
        server owns a name, and what that server calls the tool itself.  The hub
        keeps no table of its own — a second index built from the same rows is a
        second index to get wrong.

        A tool the hub serves itself — `tool_search`, `func_tool_load`,
        `skill_use` — is answered here without leaving the process.  A proxied
        one reaches its server through the connection this process keeps, and a
        call is gated on there being something behind the name, never on the
        load state: a name the model just found with `tool_search` is a name it
        can use.

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
        row = await catalogue.route(name)
        if row is None:
            # Not routable *yet*, which is not the same as unknown: a server may
            # be connecting, or coming back after a drop, and its rows are
            # written only once it has answered.  The caller was given this name
            # by `list_tools` a moment ago, so "unknown tool" is the wrong answer
            # for a link that is merely late — and this costs nothing in the
            # ordinary case, because a route that resolves is never asked twice.
            begin_connecting()
            await settle()
            row = await catalogue.route(name)
        if row is None:
            return {"text": await unknown_tool(name), "ok": False}

        source = str(row.get("source_id") or "")
        upstream = by_source(source)
        if upstream is None:
            # The row is real and its owner is not one of ours: the entry was
            # removed from the config, or switched off, since this row was
            # written.  Said as what it is rather than as an unknown tool.
            return {
                "text": (
                    f"{name!r} belongs to {source!r}, which is not a server this "
                    f"hub is configured with any more — check `tools:` in the "
                    f"config"
                ),
                "ok": False,
            }

        text, ok = await upstream.call(
            str(row.get("remote_name") or name), arguments, forwarded
        )
        # Recency, for the budget — **and this is where it is learned**.  The
        # hub is not told which tools the model called: it is the process that
        # calls them, so the stamp belongs beside the call rather than on a
        # notification from the loop, which would be a second mechanism for a
        # fact already in hand.
        #
        # **Every routed call, refused or not.**  The model reaching for a tool
        # is the evidence the budget is deciding on, and a call the far end said
        # no to is still the model asking — a tool that keeps erroring is one it
        # keeps wanting, not one to throw away.
        #
        # **Before the answer is returned, and deliberately.**  The stamp is the
        # turn boundary's input: the harness trims the list after the turn, and
        # a write detached from this call could land *after* the trim and cost
        # the model the tool it had just been using.
        await catalogue.touch(name)
        return {"text": text, "ok": ok}

    async def unknown_tool(name: str) -> str:
        """Why a name resolves to nothing, naming what does.

        The error path is a feedback channel, so it lists the alternatives — and
        they are the *loaded* ones, because those are what the caller could have
        been given.  A name from somewhere else is a name for `tool_search`.
        """
        found = await catalogue.injectable(live_sources())
        known = ", ".join(sorted(tool.name for tool in advertised(found)))
        return (
            f"unknown tool {name!r}. In your list: {known or '(none)'} — for "
            f"anything else, search the catalogue with tool_search"
        )

    @mcp.tool
    async def _func_tool_unload(names: list[str] | None = None) -> dict[str, Any]:
        """Trim the model's tool list — called by the harness, never by a model.

        The name carries a leading underscore, which is this system's mark for a
        tool the *machinery* calls rather than one a model chooses, and this is
        the only one there is: the agent server runs it before it saves a turn.

        **Why a call at all, rather than the gate just dropping the excess.**
        Because the harness is the party that has to *know*: the tools the model
        has loaded are what its next request carries, and a list that quietly
        lost three of them between two turns is a model that will look for a
        tool it still believes it has.  So the names come back, and the caller
        says so in its log and to whoever is watching.

        A turn boundary is also the right moment and the reason it is not the
        gate: the list is rebuilt before every model call, so trimming it
        mid-turn would take away a tool the model had just loaded and was about
        to use.

        Args:
            names: The tools to take out — one or several.  **Empty means
                "enforce the budget"**: whatever is over
                `tool_load.threshold` goes, least recently used first, and none
                of this process's own tools and nothing marked `autoload: true`
                is ever a candidate.  Naming one of the four this system works
                by is refused rather than obeyed.

        Returns:
            `unloaded` — the names that moved, which is the answer this exists
            for — plus `refused` (named but not unloadable) and `not_loaded`
            (named but already out of the list), and `text`, the same thing said
            in a sentence.
        """
        found = await unload_tools(catalogue, live_sources(), list(names or []))
        return {**found, "text": _unload_as_text(found)}

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
            `failed` or `idle`), how many `tools` it last offered, how many of
            them are `loaded` (which is what the model has now), `autoload` —
            whether they are wanted every turn, so they are never evicted — the
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
        counts = await catalogue.sources()
        return {
            "servers": [
                upstream.snapshot(counts.get(upstream.settings.name))
                for upstream in upstreams
            ]
        }

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
