"""slife2-toolhub — the model's tools, and the one process that decides them.

Every other plugin in this system is a place a capability comes from: a model
backend speaks one wire protocol, the db keeps turns, the agent loop runs turns.
This one is where **the tools come from**, and it exists because tools are the
one capability that has to reach *outside* the machine — to somebody else's MCP
server, to a REST API, to a program that wants an API key in its environment.

    slife2-agent  ──MCP──▶  slife2-toolhub  ──MCP──▶  plugins      (ours)
                             list_tools                    │
                             call_tool                     ├─ their own tools, by `tools/list`
                             servers                       └─ what they declare: a `tools:` or
                             _func_tool_unload                `rest-api:` entry, held and called
                                    │                         by mcp-tools / restapi-tools
                                    └──in-process──▶  `slife2.db.ToolStore`
                                                       (the catalogue's own file)

**What this process owns is the set, and it owns all of it.**  Which tools
exist, what the model is holding, what a name resolves to, what the budget takes
back, and who may call what — every one of those is a question about the *whole*
list, so it is answered in one place or it is answered twice and differently.
What it does **not** own any more is a connection to anybody but our own plugins:
the servers under `tools:` and `rest-api:` are held by `slife2-mcp-tools` and
`slife2-restapi-tools`, which say what they hold and run a call back when this
process asks.  The credential story is unchanged — a tool server's key lives in
the process that reaches it, and the agent loop never sees one — but the process
that holds it is the plugin that owns the config section, not this one.
`slife2.gateway` is the link itself; `slife2.toolfamily` is the half that the two
"hold somebody else's servers" plugins share.

**The set is decided here and remembered there.**  Which tools exist, what they
are called and who may call one is this process's to say; the rows, the load
state, the two search indexes and the budget are the db plugin's to keep, and
every operation on them is a call over MCP (`Catalogue`).  Nothing in this
module opens a database, and nothing in that one knows what a proxy name is —
which is the same line the two plugins draw everywhere else, and the reason
the next thing that needs the catalogue can have it.

Two ways in, and they are not the same kind of claim
----------------------------------------------------
**A plugin's own tools** arrive by `tools/list` and have to declare themselves
the model's (`slife2.audience`), because they belong to that plugin's code:
`remember` writes into any agent's database and `send_message` drives another
conversation, and those are exactly the tools a model would reach for if it
could read their descriptions.  **What a plugin holds** arrives by declaration
instead — one tool, `list_sources`, answering with a source's whole list — and is
not gated, because it is the operator's configuration: a `tools:` entry, a
playbook, a command.  The entry is the opt-in; there was never a mark to forget.

Which makes a source something a plugin owns rather than something the hub
holds.  A source — `arxiv`, `skills`, `cli` — is a name, a category, its rows,
and two facts about it: whether the operator switched it off and whether it is
answering.  The hub merges the rows, gates them by category, counts them and
routes by them, exactly as it did when it held the connection itself; what
changed is that it asks who holds one rather than being the one who does.  **The
price is freshness**, and it is the price of anything over a wire: an answer is
as old as the last declaration, which is why declarations are refreshed wherever
liveness is read, and why a plugin that holds sources and cannot answer for them
fails the list rather than quietly contributing none.

Which source a tool came from is not what decides who may call it — which
*caller* it is for does, and that is said on the tool itself (`slife2.audience`)
rather than in its name, in this file, or in the config.  **A plugin's tools
belong to that plugin's own code until one of them says otherwise**, so
`remember` stays where it was and `now` carries the mark, while a declared row
needs no mark at all: declaring is the opt-in.  The list of plugins is not
written down here either — it is `Config.plugins()`, which is what the launcher
starts, so the hub cannot drift from the set of processes that exist.

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
names it unloaded come back — to the harness's log, and to the model, as the
tool pair the trim is recorded as.  The leading underscore is the system's mark
for a tool the machinery calls rather than one a model chooses, and this is the
one name that carries it *and* is in the model's list: the pair has to name a
declared tool, so the model has it too — see `_func_tool_unload`.

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
arxiv server — over the plugin that holds it.  **Nothing that has a server behind
it is served by this process.**  A builtin that took a shortcut would be the
second mechanism this whole arrangement exists to avoid, and the first thing to
drift: it would not be in `servers()`, it would not have a connection to fail,
and it would not be a row in the catalogue the model's search reads.

`skill_use` used to be the exception, and it is one no longer: a skill is a
document and `slife2-skills` serves it (`slife2.skills_server`), because a family
that owns a config section *and* its own tool is a family whose next change has
somewhere to land.  So the hub's own tools are exactly the set-level three —
`tool_search`, `func_tool_load`, `_func_tool_unload` — and they are named as
themselves rather than `{server}__{tool}`, because there is no server to name.
Neither are ours for the neighbouring reason that the server is slife2: `now`,
not `builtins__now`.  `model_name` is that rule and DESIGN.md §8 is the argument
for it.

They are still *rows*, though — owned by this plugin, like every other tool's
is owned by its source.  That is what makes one query enough to answer what the
model may call, with no list of exceptions kept beside it here.

REST APIs are not a second mechanism
------------------------------------
A `rest-api:` entry is expanded *by the config layer* into the stdio command that
serves it (`slife2.config._rest_api`), so what arrives at `slife2-restapi-tools`
is an ordinary stdio server — the same kind of thing a `tools:` entry describes,
held by the same code (`slife2.toolfamily`) and declared to this process the same
way.  Nothing outside the config layer knows what REST is, which is the point:
there is one kind of thing to connect to, and the wrapper people publish for
OpenAPI is just how a REST API becomes one.

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
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP

from slife2.audience import for_the_model, forwarded_client, request_meta
from slife2.config import Config, ToolServerSettings, find_config_path, load
from slife2.db import Embedder, ToolStore
from slife2.embedder import EmbedderConnection
from slife2.gateway import (
    ClientFactory,
    Connection,
    mcp_config,
    proxied_name,
    sanitise,
)
from slife2.mcp_server import (
    CALL_SOURCE,
    LIST_SOURCES,
    configure_logging,
    house_server,
    parse_serve_args,
    serve,
)
from slife2.paths import data_dir, tools_db
from slife2.textindex import terms
from slife2.toolclient import UpstreamTool

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-toolhub"

#: This server's key in the config's `servers:` table — and therefore the one
#: name in `Config.plugins()` that is not a source of tools.  The hub asks
#: every plugin but itself; a connection to itself would list the tools of its
#: own API and drop them, which is a loopback nobody should have to reason
#: about.
#:
#: It is also the name the hub's **own** tools are catalogued under.  They are
#: this plugin's tools, served by this process: `tool_search`, `func_tool_load`
#: and `_func_tool_unload` — the set-level three, and nothing else.  Being rows
#: like everything else is what makes one query enough to answer "what may the
#: model call", and it is why nothing here has to remember a list of its own.
CONFIG_KEY = "toolhub"

#: The catalogue's vocabulary, in the three words this process has to say out
#: loud: the category — and the config `kind` — of a tool that is *ours*
#: (`slife2.db.PLUGIN`), and the two categories nothing is connected to
#: (`slife2.db.SKILL`, `slife2.db.CLI`).  Spelled in two modules rather than
#: imported for the reason the config gives: the hub must not import a server,
#: and the db is one.  They are facts about the *system* — a plugin is a server
#: slife2 starts, a skill is a document, a command is a program already
#: installed — and `model_name` reads the first to decide whether a name carries
#: its server or is the whole of what the model says.
PLUGIN = "plugin"
SKILL = "skill"
CLI = "cli"
MCP = "mcp"
REST = "rest"

#: The categories a plugin may **declare** a source under — everything except
#: `plugin`, which means *the servers slife2 starts* and is the category whose
#: rows the audience gate decides about.  Refusing it is what keeps the two ways
#: a plugin can reach the model from being confused with each other: its own
#: tools arrive by `tools/list` and have to declare themselves the model's, while
#: what it *holds* is the operator's configuration, where the entry is the
#: opt-in and there was never a mark to forget.
#:
#: So a declaration cannot mint a `plugin` source, and a declared source's name
#: may not collide with an upstream's either — which `Upstream.declare` checks
#: against what this process is actually connected to.
DECLARABLE_CATEGORIES = frozenset({SKILL, CLI, MCP, REST})

#: The two tools this process serves a *model* to manage its own tool list.
#: Named here because they are also what the hub will not let go of: see
#: `ALWAYS_LOADED`.
TOOL_SEARCH = "tool_search"
FUNC_TOOL_LOAD = "func_tool_load"

#: The tool that reads a playbook, which **`slife2-skills` serves and this
#: process does not**.  Spelled here rather than imported from `slife2.skills`,
#: because a name crossing a process boundary is spelled on both sides and the
#: hub has no other business with the module — `tests/test_config.py` holds the
#: two spellings together.  What the hub uses it for is the one thing that is
#: still the hub's: not letting the model throw it away (see `ALWAYS_LOADED`).
SKILL_USE = "skill_use"

#: The trim, and the only name here with **two callers**.  The leading
#: underscore is the convention — **a name beginning with `_` is a harness
#: tool**, one the machinery drives rather than one the model chooses — and this
#: is the single exception to it: the model has it too, because the harness's
#: trim is recorded in the conversation as a tool pair, and a pair names a tool
#: the request declares (v1's rule, and the reason its `_func_tool_unload` is
#: the one `_` tool a model sees).  So the model may call it with the names it
#: is done with, while the *harness* calls it with none — on this server's API
#: rather than through `call_tool`, so the trim does not depend on the
#: catalogue, the routing or the model's list being in any state.  See
#: `build_server`'s `_func_tool_unload`.
FUNC_TOOL_UNLOAD = "_func_tool_unload"

#: What a model may not take out of its own list.  The three this process
#: serves are how a tool list is managed at all — a budget that could take them
#: away would leave the model holding a set it cannot change — and the fourth is
#: `skill_use`, which reads the playbooks every session is meant to reach for.
#:
#: **It lives in a plugin and is still on this list.**  The budget would never
#: evict it either way (a plugin's rows are exempt, `slife2.db.evict`), so what
#: this adds is the other half: the model naming it in `_func_tool_unload` and
#: being obeyed.  One string is cheaper than a column the row would have to
#: carry to say "the system needs me", and the name it must match is
#: `slife2.skills.USE_TOOL`.
ALWAYS_LOADED = frozenset(
    {
        TOOL_SEARCH,
        FUNC_TOOL_LOAD,
        FUNC_TOOL_UNLOAD,
        SKILL_USE,
    }
)

def _refused(exc: BaseException) -> bool:
    """Whether a catalogue call was a *refusal* rather than a fault.

    The line the hub has always drawn, and the only thing that changed when the
    catalogue stopped being a plugin is how it is spelled.  A `ValueError` is the
    record saying no to *this* data — two sources claiming one name, a category
    that does not exist — and that is the calling source's own problem to report,
    because it is the thing that offered the name.  Everything else is the
    catalogue failing to work at all, which is this system come apart: it fails
    the tool list rather than quietly shortening it.

    A predicate over the exception rather than a class of our own, because the
    store is in this process now: there is no hop to fail, so there is nothing
    for a `CatalogueUnavailable` to describe that a traceback from `ToolStore`
    does not describe better.
    """
    return isinstance(exc, ValueError)


INSTRUCTIONS = (
    "The tools the agent may run. Call `list_tools` for the whole set, "
    "`call_tool` to run one, and `servers` to see which external tool servers "
    "are connected. This server is called by the agent, not by a model: the "
    "tools it lists are what the agent offers onwards."
)

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

#: How many names one `func_tool_load` may carry.  Each name is its own
#: catalogue write and its own embedding call — the loop is a `for` over a hop
#: rather than a batch — and the model's list is trimmed to the budget at every
#: turn boundary, so a list longer than the budget holds is one the next trim
#: takes back at the price of embedding everything in it.
MAX_LOAD_NAMES = 50


def model_name(server: str, tool: str, category: str) -> str:
    """What the model calls one tool: **the bare name for ours**, `server__tool`
    for everybody else's.

    `builtins__now` was a name telling the model about a division it has no
    business reasoning about: there is one set of tools here, slife2's, and
    `now` is the name of one of them.  A server in front of a name earns its
    place by keeping two of *somebody else's* tools apart — the operator may
    write down four servers that each offer a `search`, and `arxiv__search`
    against `serper__search` is the difference between a call reaching the tool
    the model read about and one reaching a stranger.

    `category` is the row's own word for where a tool came from
    (`slife2.db.PLUGIN` for ours, `mcp`/`rest` for the rest), so the rule reads
    the same on both paths: this one names a tool on the way *in*, and the row
    keeps the name for the way back out.  Nothing else has to agree about it —
    a name is opaque to the catalogue, which is what lets a stored row mean the
    same thing to a build that names differently (`_from_row`).

    The sanitising is not optional for ours: a plugin is our code, and our code
    can still name a tool something a provider rejects.
    """
    if category == PLUGIN:
        return sanitise(tool)
    return proxied_name(server, tool)


class Catalogue:
    """The hub's side of the tool catalogue, which this process holds.

    **Every database operation the hub makes goes through here.**  Which tools
    exist is this process's decision; what is known about them is the record.
    What changed is that the record is no longer a plugin: the store is
    `slife2.db.ToolStore`, opened on first use, and every call below is a method
    on it rather than a tool call to somebody else.

    That it was ever a hop is worth stating, because the reason it stopped being
    one is the reason the `db` plugin stopped existing.  A store needs a process
    when something *else* has to reach it, and the two things that used to were
    the turns and this catalogue — one writer each, and one writer is a property
    of a SQLite file rather than of a server.  v1 answers the same question the
    same way: its `memdb` is a plugin *and* a library, imported by the host for
    the paths where a hop buys nothing, and the tool catalogue there lives in the
    host rather than in a plugin at all.

    **Opened on first use, and once.**  Everything holding a `Catalogue` is built
    before there is a loop to open anything on — the upstreams, the hub's own
    tools, the server itself — so what is handed round is this object and not a
    store, and whoever asks first pays.  The store's startup sync runs there too,
    which is what makes "the model may be given these tools" true of an index
    that was built by the model currently configured.
    """

    def __init__(
        self,
        open_store: Callable[[], Awaitable[ToolStore]],
        embedder: Callable[[], Awaitable[Embedder]],
    ) -> None:
        self._open = open_store
        self._embedder = embedder
        self._store: ToolStore | None = None
        self._opening = asyncio.Lock()

    async def store(self) -> ToolStore:
        if self._store is None:
            async with self._opening:
                if self._store is None:
                    self._store = await self._open()
        return self._store

    @staticmethod
    async def _off_loop(function, *args, **kwargs):
        """Run one blocking store call on a worker thread.

        The hub answers every conversation, so a synchronous read of the
        catalogue on the event loop stalls turns that have nothing to do with it.
        The stores open a connection per call, which is what makes the hop safe:
        a connection is not shareable across threads, so there is none to share.
        """
        return await asyncio.to_thread(function, *args, **kwargs)

    async def merge(
        self,
        source: str,
        category: str,
        tools: Sequence[Mapping[str, Any]],
        *,
        autoload: bool = False,
    ) -> dict[str, Any]:
        """Record a source's whole tool list.  See `slife2.db.ToolStore.merge`.

        `autoload` still travels with the merge for the reason it always did: it
        is the operator's `autoload: true`, which lives in the section the plugin
        that holds the source owns — so the plugin says it and the hub passes it
        on, exactly as liveness is passed on.
        """
        store = await self.store()
        return await store.merge(
            source,
            category,
            list(tools),
            embedder=await self._embedder(),
            autoload=autoload,
        )

    async def source_state(self, source: str, state: str) -> None:
        """Record the verdict on one source: `enabled` or `error`."""
        await self._off_loop((await self.store()).set_source_state, source, state)

    async def injectable(self, sources: Sequence[str]) -> dict[str, Any]:
        """The tools the model may be given now.

        `sources` is the caller's because liveness is: the hub is the process
        holding the connections, and a source is live when its tool list is in
        hand.  See `ToolStore.injectable`.
        """
        return await self._off_loop((await self.store()).injectable, list(sources))

    async def evict(
        self, sources: Sequence[str], *, autoload: Iterable[str] = ()
    ) -> list[str]:
        """Trim the loaded set to the configured budget; name what it took out.

        `autoload` is the set of sources the budget may never touch, and it comes
        from the declarations for the same reason the flag above does: the store
        cannot read the sections those sources were configured in.
        """
        found = await self._off_loop(
            (await self.store()).evict, list(sources), autoload=list(autoload)
        )
        return [str(name) for name in found]

    async def route(self, name: str) -> dict[str, Any] | None:
        """The row for one advertised name, or `None` if there is no such tool."""
        return await self._off_loop((await self.store()).route, name)

    async def sources(self) -> dict[str, dict[str, int]]:
        """Per source: how many tools it has, and how many the model holds."""
        return await self._off_loop((await self.store()).source_counts)

    async def search(self, **arguments: Any) -> dict[str, Any]:
        """Both legs of a tool search, fused.  See `ToolStore.search`.

        The two inputs travel separately because the legs want different things
        (`ToolStore.search` argues it): the model's *words* are matched exactly
        and its *sentences* by meaning.
        """
        store = await self.store()
        return await store.search(embedder=await self._embedder(), **arguments)

    async def set_load(self, name: str, load_status: str) -> dict[str, Any]:
        """Move one tool in or out of the model's list.

        The answer is the store's *fact* — `loaded`, `unloaded`, `already`,
        `unknown`, `no_load_state`, `disabled`, `error` — and the sentence a
        model reads is built from it here, because a store that wrote prose
        would be the second place model-facing text lived.
        """
        return await self._off_loop(
            (await self.store()).set_load, name, load_status
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
            await self._off_loop((await self.store()).touch, name)

    async def close(self) -> None:
        """Nothing to release, and deliberately still a method.

        The store holds no connection between calls — every method opens one of
        its own — so there is nothing here to hand back.  It exists because the
        lifespan calls it, and a hook that silently does nothing is better named
        than deleted: whoever adds a held resource to the store will find the
        place it has to be released.
        """
        self._store = None


def _unpacked(text: str, ok: bool) -> tuple[str, bool]:
    """A delegated call's answer, unpacked into the shape this call returns.

    A plugin reports a call the way the hub does — `text` and `ok` — and MCP
    carries one result, so it arrives as JSON in a string.  Undoing that here is
    what keeps a delegated call indistinguishable from a direct one to every
    caller above this line, and a plugin that answered with something else is
    reported as the failure it is rather than passed on as text nobody can read.
    """
    if not ok:
        return text, False
    try:
        answer = json.loads(text)
    except ValueError:
        return f"the plugin answered with something that is not a result: {text}", False
    if not isinstance(answer, dict) or "text" not in answer:
        return f"the plugin answered without a result: {text}", False
    return str(answer.get("text") or ""), bool(answer.get("ok"))


def _server_row(
    *,
    name: str,
    kind: str,
    transport: str,
    state: str,
    counts: Mapping[str, int] | None,
    autoload: bool,
    description: str,
    error: str,
    required: bool,
) -> dict[str, Any]:
    """One row of `servers()`, for a source whichever way the hub reaches it.

    **One shape, because it is one question.**  "Why is my tool missing" has to
    read the same whether the answer is a plugin, an entry under `tools:` that
    this process connects to, or a source a plugin holds on its behalf — and two
    builders of the same row is how the third one ends up missing a field.
    """
    return {
        "name": name,
        "kind": kind,
        "transport": transport,
        "state": state,
        #: What it last offered, and how much of that the model has now.  A
        #: source with ninety tools and none loaded is a healthy server the model
        #: has not asked anything of — and that is a different answer from a
        #: server that has stopped offering them.
        "tools": int((counts or {}).get("tools", 0)),
        "loaded": int((counts or {}).get("loaded", 0)),
        #: Wanted every turn: its tools start loaded and are never evicted
        #: (`autoload` in the config, or `required`, which means the same thing
        #: for our own servers).
        "autoload": autoload,
        "description": description,
        "error": error,
        #: Ours rather than somebody else's: the answer to "why did a whole turn
        #: fail over a server that was merely down".
        "required": required,
    }


@dataclass(frozen=True)
class DeclaredSource:
    """One source a plugin holds, as the plugin itself describes it.

    The facts the hub used to read off a connection of its own — whether it is
    switched off, whether it is answering, what it is for, how it is reached —
    which is why a plugin that holds the connection is the one that has to say
    them.  Nothing here is a tool: the rows are in the catalogue, and this is
    what `servers()` reports beside them.
    """

    name: str
    category: str
    enabled: bool
    up: bool
    autoload: bool
    error: str
    description: str
    transport: str

    def snapshot(self, counts: Mapping[str, int] | None = None) -> dict[str, Any]:
        """This source's row in `servers()`."""
        if self.up:
            state = "ready"
        elif self.error:
            state = "failed"
        else:
            # Declared, enabled and not answering, with nothing to say about it:
            # an entry whose plugin has it but has not reached it yet.
            state = "connecting"
        return _server_row(
            name=self.name,
            kind=self.category,
            transport=self.transport,
            state=state,
            counts=counts,
            autoload=self.autoload,
            description=self.description,
            error=self.error,
            # Nothing a plugin holds is one slife2 starts: those are the hub's
            # own direct upstreams, and `required` is what that means.
            required=False,
        )


class Upstream:
    """One source of tools this process reaches directly, and what it means.

    **The link is `slife2.gateway`'s and the meaning is this class's.**  What
    lives here is everything the gateway deliberately does not know: that a
    plugin's tools have to declare themselves the model's before the model is
    given them, that a listing becomes catalogue rows, and that a source which
    stops answering has a verdict written against the rows it left behind.

    **Recorded, not kept.**  An upstream holds a connection and a verdict and no
    tool table: what it last listed is a fact about the catalogue, and a second
    copy here would be a second thing to keep in step — which is what replaced
    the snapshot this class used to keep.

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
        #: Where this server's tools are *recorded*.
        self._catalogue = catalogue
        #: **Ours, rather than somebody else's.**  An optional upstream that
        #: cannot be reached is a tool the model does not have; a required one
        #: that cannot be reached is this system coming apart, and `list_tools`
        #: refuses rather than quietly serving a shorter list.  Nothing in
        #: `tools:` sets this — it is what makes a plugin a plugin, and it
        #: is read twice: for that failure rule, and for whether this server's
        #: tools have to ask before the model is given them (`_offered`).
        self.required = required
        #: **Whether this source declares catalogue rows**, which is a fact
        #: about the listing and not a second copy of it: the one tool name
        #: `LIST_SOURCES` either is or is not in what the server offered.  See
        #: `declare`, which is the only thing that reads it.
        self._declares = False
        #: The source names it declared last time, so that one it has stopped
        #: holding can be marked stale — see `declare`.
        self._declared: set[str] = set()
        #: Verdicts written without waiting for them.  Held for the reason
        #: `slife2.server.server.detach` holds its own: a task nothing
        #: references can be collected before it runs, and its failure is
        #: otherwise only ever reported as a warning about a task nobody
        #: awaited.
        self._verdicts: set[asyncio.Task[None]] = set()
        self._connection = Connection(
            settings,
            transport=transport,
            client_factory=client_factory,
            on_listed=self._listed,
            on_failed=self._failed,
        )

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
        return self._connection.usable

    def snapshot(self, counts: Mapping[str, int] | None = None) -> dict[str, Any]:
        """This server's row in `servers()`, and in a log line.

        `counts` is the catalogue's answer for this source — how many tools it
        has and how many the model is holding — and it is passed in rather than
        looked up here because this method is also the one `list_tools` uses for
        its error message, on a path where the catalogue has already been asked.
        """
        return _server_row(
            name=self.settings.name,
            kind=self.settings.kind,
            transport=self.settings.transport,
            state=self._connection.state,
            counts=counts,
            autoload=self.settings.autoload or self.required,
            description=self.settings.description,
            error=self._connection.error,
            required=self.required,
        )

    def connecting(self) -> None:
        """Start a connect attempt, unless one is running or one has succeeded.

        Separate from `ready` because the common case — a healthy server — must
        not await anything, and separate from the connect itself so that the
        callers who only want the list can start one and move on.
        """
        self._connection.connecting()

    @property
    def attempt(self) -> asyncio.Task[None] | None:
        """The connect in flight, if there is one — for a caller that wants to
        wait on attempts it did not start."""
        return self._connection.attempt

    async def ready(self) -> bool:
        """Whether the link is usable, starting one and waiting if it is not."""
        return await self._connection.ready()

    # --- what the gateway hands over ------------------------------------------

    async def _listed(self, listed: list[Any]) -> None:
        """One listing, recorded — the half the gateway will not do.

        **The tools go to the catalogue and are not kept here.**  What came back
        is the whole truth about this source, so it is merged — added, updated,
        deleted, or left alone — and this process's own copy of it is nothing at
        all: `list_tools` reads the catalogue and `call_tool` routes through it,
        which is what makes the row the one place a tool is described.

        Both failures here end the same way for this source and differently for
        the system.  A *catalogue* that is not answering is a plugin gone, and it
        is raised: it must not be reported as "that tool server is broken",
        because the next ask would then look in the wrong place.  A db that
        answered and *refused* — a name another source owns, say — is this
        source's list that cannot be recorded, so it is this source that is
        unusable until it is fixed.
        """
        #: Read off the *raw* listing and not off `offered`: this is the one tool
        #: a source has that the model must never be given, so the audience gate
        #: is exactly what removes it from the filtered list.  A bool and not a
        #: set of names, for the reason this class keeps no tool list at all —
        #: the question `declare` asks is a yes-or-no about one name.
        #:
        #: **`required` is half the answer**, and the half that is not about the
        #: listing.  A declared row is merged without passing the audience gate
        #: — that is what declaring *is* — so the permission has to come from
        #: somewhere else, and "slife2 starts this server" is the only thing here
        #: that means *ours*.  Somebody else's server that happens to define a
        #: tool with this name gets nothing, which matters because the hub takes
        #: every tool an entry under `tools:` offers.
        self._declares = self.required and any(
            getattr(tool, "name", "") == LIST_SOURCES for tool in listed
        )
        offered = self._offered(listed)
        try:
            await self._catalogue.merge(
                self.settings.name,
                self.settings.kind,
                [_row_of(self.settings.name, tool) for tool in offered],
            )
        except Exception as exc:  # noqa: BLE001 - split by _refused, below
            await self._connection.disconnect()
            if not _refused(exc):
                # The catalogue itself did not work, which is not this source's
                # problem and must not be filed as one: `list_tools` fails the
                # whole list rather than quietly losing a server's tools.
                raise
            self._connection.fail(exc)
            return
        # Both counts, because the interesting number when a tool is missing is
        # the one that says the server had it all along.
        logger.info(
            "%s: %d of %d tool(s) offered to the model, via %s",
            self.settings.name,
            len(offered),
            len(listed),
            self.settings.transport,
        )

    def _failed(self, _exc: Exception) -> None:
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
        try:
            verdict = asyncio.get_running_loop().create_task(
                self._catalogue.source_state(self.settings.name, "error")
            )
        except RuntimeError:  # pragma: no cover - a failure is reached from an await
            # No loop to write on.  The verdict is the catalogue's next-start
            # problem, and nothing here may replace the reason this failed.
            return
        self._verdicts.add(verdict)
        verdict.add_done_callback(self._verdict_written)

    def _verdict_written(self, verdict: asyncio.Task[None]) -> None:
        """Retrieve a detached verdict, so a failure is logged and not a warning.

        `create_task` is the only statement in the block above that cannot
        raise for a reason worth reporting, which is why the retrieval is here
        rather than a `suppress` around the call: a `suppress` would swallow the
        *creation* and leave the coroutine's own failure to surface as "Task
        exception was never retrieved" — a message about nothing, naming no
        server, some minutes later.
        """
        self._verdicts.discard(verdict)
        if not verdict.cancelled() and (failure := verdict.exception()):
            logger.info(
                "the verdict on %s was not recorded: %s", self.settings.name, failure
            )

    def _offered(self, listed: list[Any]) -> list[UpstreamTool]:
        """The tools of one listing the model may be given.

        **Ours have to ask, and the answer is no until they do**
        (`slife2.audience`).  A plugin's tools belong to that plugin's own code
        until one says otherwise, because the ones that would leak —
        `remember`, which writes into any agent's database, `send_message`,
        which drives another conversation — are exactly the ones a model would
        reach for if it could read their descriptions.

        **This is the only way into the model's list that asks.**  A source the
        operator configured arrives by declaration instead, and is not gated,
        because the entry in the config is the opt-in — see `Upstream.declare`.
        """
        # **And there is no other case any more.**  A source the operator
        # configured is not an upstream of this process — a plugin holds it and
        # declares it, and what it declares was never gated, because the entry
        # in the config is the opt-in.  So every tool that arrives here by
        # `tools/list` belongs to a plugin's own code, and every one of them
        # has to ask.
        return [
            _advertise(self.settings, tool)
            for tool in listed
            if for_the_model(getattr(tool, "meta", None))
        ]

    # --- a call --------------------------------------------------------------

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Run one of this server's tools.  See `slife2.gateway.Connection`."""
        return await self._connection.call(tool, arguments, meta)

    async def declare(self) -> list[DeclaredSource]:
        """Hand over the sources this plugin holds, and record what they are.

        **A second kind of thing a plugin can offer, beside its own tools.**  A
        plugin that fronts somebody else's servers holds *sources*: the entries
        the operator wrote down, each with its own name, its own tools and its
        own health.  So does one whose family is not a tool at all — a skill is a
        document and a `cli:` entry is a command, and `tool_search` reads the
        catalogue, so a family no row describes is a family only a model that
        already knew a name could find.

        **The hub merges; the plugin does not write.**  That is the whole reason
        this is a call and not a connection from the plugin to the db: one writer
        of the tool table, one place where two sources' claim on a name is
        settled, and one process holding a catalogue connection.

        **Asked only of a plugin that is already usable, and only of one of
        ours.**  Usable is what keeps it off the session path — `start()` runs
        per session and `tool_search` per search, and neither may wait for a
        connect; a plugin that is not up yet contributes nothing and is asked
        again on the next ask, and the rows it declared last time are still in
        the catalogue, so a late plugin is a delay rather than a loss.  Ours is
        the permission: see `_listed`, which is where `_declares` is decided.

        **A plugin that serves this tool and cannot answer it is a fault**, and
        it is raised as one.  A family that quietly contributed no sources would
        take every one of its tools out of the model's list without anything
        reporting a failure — which is precisely the invisible break the
        required-plugin rule exists to prevent.

        Returns:
            One `DeclaredSource` per source it holds, which is the hub's address
            book for calls it cannot make itself — and what `servers()` reports.
        """
        if not self._connection.usable or not self._declares:
            return []
        text, ok = await self.call(LIST_SOURCES, {})
        if not ok:
            raise ConnectionError(
                f"{self.settings.name} is not answering ({text or 'no answer'})"
            )
        try:
            answer = json.loads(text)
        except ValueError as exc:
            raise ConnectionError(
                f"{self.settings.name} answered with something that is not JSON"
            ) from exc
        held = answer.get("sources")
        if not isinstance(held, list):
            raise ConnectionError(
                f"{self.settings.name} answered without a list of sources"
            )
        declared = [one for raw in held if (one := await self._record(raw)) is not None]
        # **A source it used to hold and does not any more.**  The entry was
        # taken out of the section, and its rows have to stop claiming to work —
        # so what is left of it is marked the way a server that is gone is.  This
        # and the db's own boot pass between them cover both moments an entry can
        # be removed: while slife2 is running, and while it was not.
        for source in self._declared - {one.name for one in declared}:
            await self._catalogue.source_state(source, "error")
        self._declared = {one.name for one in declared}
        return declared

    async def _record(self, raw: Any) -> DeclaredSource | None:
        """One declared source: checked, merged if it is answering, reported.

        The three cases are the whole of the rule, and each is what a source's
        state means rather than a special case: what is **up** is merged, so its
        rows are the truth about it; what is **down** is not merged but marked,
        because a plugin cannot list what it cannot reach and merging an empty
        list would purge exactly the rows this system keeps on purpose; and what
        the operator **switched off** is marked `disabled` and is the one state
        the runtime never overwrites.
        """
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("name") or "")
        category = str(raw.get("category") or "")
        enabled = bool(raw.get("enabled", True))
        up = enabled and bool(raw.get("up"))
        error = str(raw.get("error") or "")
        if not name or category not in DECLARABLE_CATEGORIES:
            # **The audience gate is not renegotiable over this channel.**  A
            # source that could name its own category could offer the model
            # `remember`: a function category under a live source is exactly what
            # `injectable` answers with.  Refused rather than clamped — a plugin
            # asking for this is either confused or lying, and both are worth a
            # line in the log.
            logger.warning(
                "%s: may not declare %r as %r (known: %s)",
                self.settings.name,
                name,
                category,
                "/".join(sorted(DECLARABLE_CATEGORIES)),
            )
            return None
        if name == self.settings.name:
            # The old rule, kept: a source's verdict is written across every row
            # it owns, so a plugin that filed its held sources under its own name
            # would have its documents marked broken whenever it faltered.
            logger.warning(
                "%s: may not declare a source under its own name", self.settings.name
            )
            return None
        rows = raw.get("rows")
        if up and isinstance(rows, list):
            try:
                await self._catalogue.merge(
                    name, category, rows, autoload=bool(raw.get("autoload"))
                )
            except Exception as exc:  # noqa: BLE001 - split by _refused, below
                if not _refused(exc):
                    raise
                # Refused rows are this *source's* problem: it is the thing that
                # offered a name somebody else already owns, and the rest of the
                # catalogue is untouched.
                logger.warning(
                    "%s: the rows of %r were refused: %s", self.settings.name, name, exc
                )
                up, error = False, str(exc)
        if not enabled:
            await self._catalogue.source_state(name, "disabled")
        elif not up:
            await self._catalogue.source_state(name, "error")
        return DeclaredSource(
            name=name,
            category=category,
            enabled=enabled,
            up=up,
            autoload=bool(raw.get("autoload")),
            error=error,
            description=str(raw.get("description") or ""),
            transport=str(raw.get("transport") or ""),
        )

    async def disconnect(self) -> None:
        """Drop the connection, keeping the configuration and the error."""
        await self._connection.disconnect()

    async def close(self) -> None:
        await self._connection.close()


@dataclass(frozen=True)
class LocalTool:
    """A tool the hub serves itself, because there is nothing else that could.

    The three that find, load and trim tools are the case, and they are the whole
    of it: each is a question about the *whole* catalogue, so the process that
    owns the set answers it and no plugin can.  None of them has a process to
    start, a credential to hold or an address to configure: what an `Upstream`
    exists for — a connection — has nothing to describe.  What is left is a
    name, the schema the model reads, and the body that answers a call.

    **A local tool's name is its own**, `tool_search`, the way every tool of ours
    is (`model_name`) — and it is routed *before* the catalogue is asked, which
    is the part that is local to this class.  Being first is what makes a
    collision safe: somebody else's server may offer a tool of the same name,
    and local wins, which is the direction that keeps a tool this system
    guarantees from being shadowed by somebody else's configuration.

    Its *row*, though, is an ordinary one, owned by this plugin: that is what
    makes the catalogue the single answer to "what may the model call", instead
    of that answer plus a list of exceptions kept here.
    """

    tool: UpstreamTool
    run: Callable[[dict[str, Any]], Awaitable[tuple[str, bool]]]


#: How many rows one search answers with.
#:
#: The harness's number and not the model's, which is a change: `limit` used to
#: be a parameter, so a model could ask for the whole catalogue a page at a time
#: — and did.  What a search is for is finding *a* tool, and a page big enough to
#: be a listing is the thing this tool stopped being.  `tool_search` finds by
#: meaning; the tool that enumerates is a different tool, and it will own the
#: question of how much of the catalogue fits in an answer.
SEARCH_LIMIT = 10

#: **Two inputs, because the two legs want different things, and nothing else.**
#: `category`, `source_id`, `status`, `load_status` and `limit` used to be here,
#: and five filters on a search is five ways to ask a question that is not "find
#: me the tool that does this": narrowing to one server, or to the rows that are
#: switched off, is *reading the catalogue* — a different job with a different
#: answer shape (a list, with no ranking), and §9 of DESIGN says so.
#:
#: The single `query` that replaced them is gone too, and this is the reason:
#: one string went to *both* legs, and one of the two cannot read a sentence.
#: The keyword leg asks for every term it is handed, so a sentence — "take a
#: screenshot of a web page" — demands six words at once and matches nothing.
#:
#: **What is *not* the reason is that the semantic leg wants a phrase over
#: words.**  An earlier version of this comment said that, and it was measured
#: and is false: a compact word list embeds as well as the sentence it was taken
#: from — 36 of 36 either way on this catalogue — which is what makes the
#: fallback in `local_tools.search` sound.  The two inputs are one call apart,
#: not one being the other's poor relation.
#:
#: Measured on the live catalogue, splitting them is 20/20 against 18/20, and
#: the two queries the single string missed are found by the two halves of the
#: split (`work out 17 times 23` by a second sentence, `读一下这个网页的内容` by
#: one written in English).  Both fields are `required` and either may be an
#: empty array: a model made to answer both has said which one it means, and a
#: field that may go unfilled is a leg that silently never runs.
TOOL_SEARCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keywords": {
            "type": "array",
            "items": {"type": "string"},
            # Three short sentences, and each one is a fact about this search
            # rather than advice about how to use it: what the words must be
            # true of, which words are worth giving, and what empty means.  The
            # warning this used to carry ("guessing at words makes it find
            # nothing") was true while a keywords-only call was answered by the
            # keyword leg alone; with the fallback in `local_tools.search` a
            # guess costs nothing, so the sentence had become false and would
            # only talk a model out of the words it was sure of.
            "description": (
                "Words a tool's own text has to contain, every one of them — "
                "'screenshot', 'pdf'. A word you are confident is in that text "
                "is the sharpest thing you can give this search. Empty if you "
                "have none."
            ),
        },
        "sentences": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "What you want to do, in your own words, one phrase per idea — "
                "'take a screenshot of a web page'. Matched by meaning, not by "
                "wording. A second entry is a second chance, not a longer "
                "query. Empty if you have none."
            ),
        },
    },
    # Both, and that is the design: the two legs want different inputs, so a
    # model made to answer both has told the search which one it means — and a
    # field that may go unfilled is a leg that silently never runs. An empty
    # array is a real answer (that half does not apply), and both empty is the
    # one refusal the tool has.
    "required": ["keywords", "sentences"],
}

#: No sentence here names another tool.  The one that did — "Load what you need
#: with func_tool_load" — was a reference this text cannot keep true: what a
#: model's list holds is decided elsewhere, so the wording was a promise about
#: somebody else's state made by a description that cannot see it.  What a model
#: needs from here is what this search is and what its two inputs mean.
TOOL_SEARCH_DESCRIPTION = (
    "Find a tool by what it does, across the whole catalogue — including tools "
    "whose server is off or is not answering, not only the tools you have "
    "loaded. `keywords` are words a tool's own text must contain, all of them; "
    "`sentences` is what you want to do, matched by meaning. Give both, either "
    "may be empty."
)

FUNC_TOOL_LOAD_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The tool names, as `tool_search` reports them: bare for "
                "slife2's own tools ('now'), '{server}__{tool}' for a tool "
                "server's ('arxiv__search'). One or several — a single name as "
                "a bare string works too."
            ),
        }
    },
    "required": ["names"],
}

#: What loading is *for*, which is not what it used to say.  "into your tool
#: list so you can call them" contradicted the sentence three lines later — a
#: name can be called without being loaded, because routing looks the name up
#: in the catalogue and never asks `load_status` (`Catalogue.route`).  Loading
#: is what puts a tool in the list the model is *shown*, so the accurate
#: version is what the text below says.
FUNC_TOOL_LOAD_DESCRIPTION = (
    "Put tools into the list you are shown before each request. A name from "
    "`tool_search` can be called without loading; loading is what keeps it in "
    "the list, there to be chosen next step. The list is rebuilt before every "
    "request, and when it grows past its cap the least recently used go."
)

FUNC_TOOL_UNLOAD_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The tools to take out of your list, as `tool_search` reports "
                "them. Empty leaves the budget to the harness, which calls this "
                "that way at a turn boundary. One or several — a single bare "
                "string works too."
            ),
        }
    },
}

#: The description is written for **both** readers of this tool, because it has
#: two callers and the model is the one that has to be able to tell them apart:
#: a call it makes itself, and one the harness makes at a turn boundary — the
#: pair that appears in its history with no names in it.  `_func_tool_unload`'s
#: own docstring is where the double life is argued.
FUNC_TOOL_UNLOAD_DESCRIPTION = (
    "Take tools out of your list. Name the ones you are done with, or leave it "
    "empty to leave the budget to the harness, which calls this with no names "
    "at a turn boundary. Either way the answer names what went."
)


def local_tools(
    catalogue: Catalogue,
    live_sources: Callable[[], list[str]],
    refresh_declared: Callable[[], Awaitable[None]],
    autoload_sources: Callable[[], list[str]],
) -> list[LocalTool]:
    """What this process serves a model itself: the three it manages tools with.

    **These are the set-level tools, and that is why they are here.**  Which
    tools exist, which of them the model is holding, and what the budget takes
    back are all questions about the *whole* catalogue — so the process that
    owns the set answers them, and no plugin can.  Everything else a model can
    *call* is served by a plugin: bare-named when it is that plugin's own tool
    (`now`), `{server}__{tool}` when it is an entry under `tools:`.  What the hub
    kept when `skill_use` moved out to `slife2-skills` is exactly this remainder,
    and the remainder is the reason `LocalTool` still exists at all.

    **Find a tool** — `tool_search`, the hybrid search over the catalogue.  What
    it *does* is the db's: the two legs, the fusion and the filters all happen
    there, and this half turns rows into text a model reads.

    **Load one** — `func_tool_load`, whose answer is likewise the db's verdict
    phrased for a model.

    **Trim the list** — `_func_tool_unload`, the one of the three with a second
    caller.  With no names it is the *budget*: the agent server calls it that way
    at a turn boundary, the least recently used tools over `tool_load.threshold`
    go, and the answer names them.  With names it is the model saying what it is
    done with, which is v1's meta tool.  Being a tool the *model* has is also
    what makes the harness's trim recordable: it is written into the
    conversation as a pair under this name, so the name has to be one the
    model's tool list declares — `_func_tool_unload` argues that, and it is the
    one caller that made this the third rather than the second.

    `refresh_declared` is handed in rather than reached for because a search is
    where a family that declares rows has to be current: dropping a directory
    into `<data>/skills/` is the whole of installing a skill, and a row that
    appeared only at the next hub start would make a new playbook readable and
    unfindable at the same time.  See `Upstream.declare`.
    """

    async def search(arguments: dict[str, Any]) -> tuple[str, bool]:
        # Every family that owns rows is asked for its list on the way in,
        # because a search is exactly where something that arrived since the hub
        # started has to be findable — see `Upstream.declare`.  Nothing is
        # written when nothing changed, and nothing is waited for: a source that
        # is not up yet is asked again on the next search.
        await refresh_declared()
        # A keyword that holds no *term* — `"***"`, `"—"` — is dropped here
        # rather than passed on: `match_expression` refuses such a thing with
        # `EmptyQuery`, and an exception out of a tool call is an MCP error where
        # the model needed a sentence it can act on.  What is left of the two
        # lists is what the search is actually asked for.
        keywords = [word for word in _listed(arguments.get("keywords")) if terms(word)]
        sentences = _listed(arguments.get("sentences"))
        if not keywords and not sentences:
            # Refused rather than answered with everything, which is what this
            # used to do: an empty query was a *browse*, and a browse is a list.
            # A list is not a search — it has no ranking, it is as long as the
            # catalogue, and a model that wanted one tool has to read all of
            # them — so the tool that finds things by meaning declines to be the
            # tool that enumerates.
            #
            # **And it does not say where else to look.**  It used to end "ask
            # for the list" — a pointer at a tool this one cannot see, and in
            # the log of a real turn a model followed it: it called `skill_use`
            # with an invented name and got "no skill called '__list__'".  A
            # tool's own answer promising that somebody else can do the job is a
            # promise it cannot keep; when there is a tool that enumerates, the
            # sentence can name it and be true.
            return (
                "tool_search needs something to look for: `sentences` is what you "
                "want to do ('take a screenshot of a page') and `keywords` are "
                "words a tool's own text would carry ('screenshot'). Give one of "
                "them — both empty is not a search.",
                False,
            )
        if keywords and not sentences:
            # **The words are a sentence when they are all there is.**  A
            # keywords-only call used to be answered by the keyword leg alone,
            # and measured on this catalogue that leg found 23 of the 36 tools a
            # caller asks for — the same 23 whether the catalogue held 130 of
            # them or 253, because what it misses is word forms and not choice.
            # Routing the same words through both legs finds 34 to 36.
            #
            # The loss was never that words embed badly: a word list embeds as
            # well as the sentence it was taken from.  It was that they never
            # reached the leg that could answer them.  The cost of closing that
            # is one embedding call on a call that used to make none.
            sentences = [" ".join(keywords)]
        found = await catalogue.search(
            keywords=keywords, sentences=sentences, limit=SEARCH_LIMIT
        )
        return _results_as_text(found), True

    async def load(arguments: dict[str, Any]) -> tuple[str, bool]:
        names = _names_of(arguments)
        if not names:
            return "func_tool_load needs at least one tool name", False
        if len(names) > MAX_LOAD_NAMES:
            # A bound, because each name is its own catalogue write and its own
            # embedding call — this is a `for` over a hop, not a batch — and
            # because a list longer than the budget holds is a list the next
            # trim takes back anyway.  Refused as a whole rather than truncated:
            # a model told *which* half was loaded has to diff two lists, and
            # one told to ask for fewer knows exactly what to do.
            return (
                f"func_tool_load takes at most {MAX_LOAD_NAMES} names at a time "
                f"({len(names)} given): the list is trimmed to the budget at "
                f"every turn boundary, so load the ones you want now."
            ), False
        lines: list[str] = []
        ok = True
        for name in names:
            answer = await catalogue.set_load(name, "loaded")
            text, loaded = _load_as_text(name, answer)
            lines.append(text)
            ok = ok and loaded
        return "\n".join(lines), ok

    async def unload(arguments: dict[str, Any]) -> tuple[str, bool]:
        """Take tools out of the model's list, by name or by budget.

        Both callers of `_func_tool_unload` arrive here — the model with the
        names it is done with, and the harness with none — because a tool has
        one body however many ways it is called.  What the *harness* uses
        instead is the hub's own API tool of the same name: the trim must not
        depend on this list, this catalogue or this routing being in any
        particular state, and `_func_tool_unload` is where that is argued.
        """
        found = await unload_tools(
            catalogue, live_sources(), _names_of(arguments), autoload_sources()
        )
        # Nothing it could not do is a success.  A refused name (one the system
        # works by) and an unknown one are both the model asking for something
        # that did not happen, which is what `ok` is for — the trim the harness
        # asks for has neither, and comes back true.
        return _unload_as_text(found), not (
            found.get("refused") or found.get("unknown")
        )

    return [
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
        LocalTool(
            tool=UpstreamTool(
                name=FUNC_TOOL_UNLOAD,
                server=CONFIG_KEY,
                tool=FUNC_TOOL_UNLOAD,
                description=FUNC_TOOL_UNLOAD_DESCRIPTION,
                parameters=FUNC_TOOL_UNLOAD_PARAMETERS,
            ),
            run=unload,
        ),
    ]


async def unload_tools(
    catalogue: Catalogue,
    sources: Sequence[str],
    names: Sequence[str],
    autoload: Iterable[str] = (),
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
        `ALWAYS_LOADED`), `unknown` (no row has that name), and `not_loaded`
        (named but already out of the list).

        `unknown` is apart from `refused` on purpose.  Three of the four are
        facts about a row's *state* — a skill has no load state, a switched-off
        source is not the runtime's to touch — and one is a fact about the name
        itself; a caller told "refused, the system needs them" about a name that
        does not exist has been told something false.
    """
    if not names:
        return {
            "unloaded": await catalogue.evict(sources, autoload=autoload),
            "refused": [],
            "not_loaded": [],
        }
    unloaded: list[str] = []
    refused: list[str] = []
    unknown: list[str] = []
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
        elif outcome == "unknown":
            # A name nothing holds.  Not a refusal, and saying so as one would
            # be the opposite of true: "refused, the system needs them" tells a
            # caller that a tool it invented is a tool this process protects.
            unknown.append(name)
        else:
            refused.append(name)
    return {
        "unloaded": unloaded,
        "refused": refused,
        "unknown": unknown,
        "not_loaded": not_loaded,
    }


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
    unknown = [str(name) for name in found.get("unknown") or []]
    if unknown:
        parts.append("no such tool: " + ", ".join(unknown))
    not_loaded = [str(name) for name in found.get("not_loaded") or []]
    if not_loaded:
        parts.append("already out of the list: " + ", ".join(not_loaded))
    return "; ".join(parts)


def _listed(given: Any) -> list[str]:
    """One of `tool_search`'s two lists, as the list it should have been.

    The schema asks for arrays of strings, and a model that writes one bare
    string instead of `["..."]` is asking for the same thing — `_names_of`
    makes that argument for `func_tool_load`, and it holds here for the same
    reason: refusing it would be pedantry with a round trip as the price.

    Blank entries are dropped rather than kept: `[""]` is a *present* field
    whose content is nothing, which the schema's `required` invites, and a
    caller that filled both fields that way has given the search no term at all
    — which is the one case the handler refuses.
    """
    if isinstance(given, str):
        given = [given]
    if not isinstance(given, Sequence):
        return []
    return [str(item) for item in dict.fromkeys(given) if str(item).strip()]


def _names_of(arguments: Mapping[str, Any]) -> list[str]:
    """The tool names one `func_tool_load` call is about.

    `_listed` is the reading — one or several, a bare string understood,
    duplicates and blanks dropped.  This exists to say *which* field of which
    call it is read from, which is the part a reader of `_listed` cannot see.
    """
    return _listed(arguments.get("names"))


#: Where "the meaning leg is sure" begins, and where "it found nothing" ends.
#:
#: Measured on the live catalogue with the real embedder — 36 queries it can
#: answer against 10 it cannot, taking each page's best `meaning`:
#:
#:   can answer     min 0.477   median 0.631   max 0.782
#:   cannot         min 0.389   median 0.487   max 0.512
#:
#: So the two overlap in a 0.035 band and no cutoff is exact.  `MATCH_FLOOR` is
#: the conservative end of the band: at 0.55 every unanswerable query is caught
#: and 32 of the 36 answerable ones stay a match.  `WEAK_FLOOR` is inside the
#: overlap, and it exists to be *said* rather than decided — what sits between
#: the two is genuinely undecided, and which of the three tiers a page lands in
#: is a sentence in the answer, not a row removed from it.
#:
#: **The words override both.**  A row whose text contains every word the caller
#: gave is certain, whatever it scores — `pandoc` scores 0.514 against the row
#: *named* `mcp-pandoc`, and a cutoff alone would answer "nothing here" to
#: somebody who just said the tool's name.
MATCH_FLOOR = 0.55
WEAK_FLOOR = 0.48

def _quoted(description: Any) -> list[str]:
    """A tool's description, as the lines under its record.

    **Indented, and never cut.**  The indentation is what makes the line count
    mean anything: every record begins at the left margin with its number, so a
    description may be as long as it likes — `mcp-pandoc`'s is ninety lines
    against a catalogue median of one — and the answer is still countable at a
    glance.  Cutting it would have hidden the problem rather than fixed it, and
    at the price of the one thing a model chooses by.

    Empty lines stay empty rather than becoming three spaces, and they cannot be
    mistaken for a record because a record starts with a number.
    """
    text = str(description or "").strip()
    if not text:
        return []
    return [f"   {line.rstrip()}" if line.strip() else "" for line in text.split("\n")]


def _verdict(rows: Sequence[Mapping[str, Any]], best: float) -> str:
    """The first line: how sure, and how the rows below are ordered.

    **No count of matches**, because there is no such number to give: the floors
    label a page and do not cut it — a weak answer still shows its ten rows — so
    what is known is "these rows", and a count would be the page size wearing the
    word "found".

    Sure / weak / nothing rather than a bare list, because a page of the ten
    nearest rows says nothing about whether any of them is for the thing asked:
    the same shape came back for a query this catalogue can answer and for one it
    cannot, and the caller was left to guess which it had.

    The second clause says what orders the page, in one clause, because the order
    is two rules and only one of them is visible as a number: the rows the
    caller's own words matched come first — that evidence is certain where a
    cosine is graded — and the rest are by meaning.  A model that assumed one
    rule would misread a page that has both.
    """
    worded = sum(1 for row in rows if row.get("matched_words"))
    if best >= MATCH_FLOOR:
        head = f"Best match {best:.2f} by meaning."
    elif best >= WEAK_FLOOR or worded:
        why = (
            "your words match one, but the meaning leg is close to none"
            if best < WEAK_FLOOR
            else f"nothing is above {MATCH_FLOOR:.2f} by meaning"
        )
        head = f"Weak — {why}."
    else:
        head = f"Nothing here matches: no row is above {WEAK_FLOOR:.2f} by meaning."
    if not worded:
        return f"{head} Closest first by meaning."
    if worded == len(rows):
        # No "rest" to describe.  Saying there was one made the header read as a
        # claim about the whole column when the whole column was one group.
        return f"{head} All of these matched your words."
    # **"listed first" is the load-bearing half.**  Without it the clause says
    # which rows are which and not that the order puts one group above the other,
    # so a reader takes "the rest by meaning" as describing the whole column — and
    # then `pdf_render_pages` at 0.38 sitting above `scan_workspace` at 0.56 reads
    # as the numbers being out of order rather than as a tier boundary.
    return (
        f"{head} Your words matched {worded} of these, listed first; "
        f"the rest by meaning."
    )


def _results_as_text(found: Mapping[str, Any]) -> str:
    """A tool search's rows, as the text a model reads.

    **One numbered record per result, the same fields every time**, because the
    reader is a model and a list whose entries cannot be counted or compared is a
    list it has to guess at:

        Found 3. Best match 0.73 by meaning.
        1. name  (category, source; meaning 0.73)
           one line of description

    `meaning` is how close the row is by meaning — the same measurement for every
    row, including the ones the words found and the meaning leg never returned,
    which used to carry no number at all (`ToolStore.search` fills it in).  It
    orders the page, within the rows the header's second clause separates: words
    first, then meaning, each descending.  So the column a reader sees is the
    column that ordered it, which is the one property this format has to keep — a
    number that does not order the page is a number a model will sort by and be
    wrong about.

    `matched your words` marks the rows the keyword leg found, which is the one
    piece of evidence here that is certain rather than graded.

    A row that is not usable says so where its state would otherwise be silent:
    "switched off" and "its server is not answering" are answers the model can
    act on, and are exactly what v1 kept rows for a failing server to be able to
    say.
    """
    rows = found.get("results") or []
    if not rows:
        # The advice this used to carry — "call tool_search with an empty query
        # to see what is installed" — is gone with the browse it named, and a
        # tool that suggested it would be sending the model to a refusal.  What
        # is left is the one thing that can still work on a miss.
        return "Nothing matched. Try fewer or different words."

    scored = [
        float(row["similarity"]) for row in rows if isinstance(row.get("similarity"), float)
    ]
    best = max(scored) if scored else 0.0

    lines = [_verdict(rows, best), ""]
    for index, row in enumerate(rows, start=1):
        state = [str(row.get("category") or ""), str(row.get("source_id") or "")]
        status = str(row.get("status") or "")
        if status and status != "enabled":
            state.append(f"NOT USABLE: {status}")
        elif row.get("load_status") == "loaded":
            state.append("loaded")
        if row.get("matched_words"):
            state.append("matched your words")
        similarity = row.get("similarity")
        if isinstance(similarity, float):
            state.append(f"meaning {similarity:.2f}")
        lines.append(f"{index}. {row.get('name')}  ({'; '.join(state)})")
        lines.extend(_quoted(row.get("description")))
    # Said for a browse as well as for a search, which is the opposite of what
    # this did: an empty query is the "what is installed, and how do I get one
    # of them" question, and answering it with a list and no next step leaves
    # the model to guess the mechanism a moment after asking about it.
    lines.append("")
    lines.append("Call func_tool_load(name) to put one of these in your list.")
    return "\n".join(lines)


def document_is_not_callable(name: str, row: Mapping[str, Any]) -> str:
    """Why one of the two document families cannot be called, and what to do.

    **A search result is an invitation to call the name in it**, so the two
    families that are findable and not callable have to answer like grown-ups:
    the model read the name, tried it, and is owed the step that actually
    reaches the thing — `skill_use` for a playbook, and for a command the truth
    that nothing runs one yet (DESIGN.md §9).
    """
    if str(row.get("category") or "") == SKILL:
        return (
            f"{name!r} is a playbook, not a tool: read it with "
            f"skill_use(name={str(row.get('remote_name') or name.split(':', 1)[-1])!r})"
        )
    command = str(row.get("remote_name") or "")
    return (
        f"{name!r} is a command already installed on this machine"
        + (f" ({command})" if command else "")
        + " — nothing here runs one yet, so there is no tool to call"
    )


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
        # The two document families, and the answer differs because what the
        # model does next differs: a skill it reads with `skill_use`, and a
        # command — installed, but not a tool — has nothing serving it yet
        # (DESIGN.md §9).  Both are findable, which is the half that landed.
        if name.startswith(f"{SKILL}:"):
            what = (
                f"it is a playbook, read with skill_use(name="
                f"{name.split(':', 1)[1]!r}), not a tool to load"
            )
        elif name.startswith(f"{CLI}:"):
            what = (
                "it is a command already installed on this machine, not a tool "
                "to load — nothing runs one yet"
            )
        else:
            what = "it has nothing behind it that could be loaded"
        return f"{name!r} has no load state: {what}", False
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


def _advertise(settings: ToolServerSettings, tool: Any) -> UpstreamTool:
    """One listed tool, named for the model.

    **The name is the whole of what this decides**; the description is kept
    exactly as the server wrote it.  What the model reads is assembled on the
    way out of the catalogue (`_from_row`), which is the one place that knows
    the row is ours to label — and the one place it can be done once, which is
    the bug this pairing had while both ends did it.
    """
    server = settings.name
    return UpstreamTool(
        name=model_name(server, tool.name, settings.kind),
        server=server,
        tool=tool.name,
        description=(tool.description or "").strip(),
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

    The inverse of `_row_of`, and **the one place the `[server]` label is put
    on** — the name is whatever the row says (so a row written by another hub,
    or by a build from last week, reads as it always read), and the description
    is the server's own with the server in front of it.  A model choosing
    between four tools called `search` cannot do it from the name alone, and the
    name is not allowed to carry a sentence; where a name *can* carry it — ours,
    which have no server in front of them — the label keeps saying where it came
    from anyway, because a label costs nothing and nothing else would.

    Doing it here rather than in `_advertise` is what makes it happen once: the
    two ends both doing it is how the model came to read `[builtins]
    [builtins] Evaluate an arithmetic expression`.
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


def plugin_settings(config: Config, name: str) -> ToolServerSettings:
    """One of our own servers, as one upstream of the hub.

    **Written here rather than under `tools:` because it is ours.**  An entry in
    `tools:` is somebody else's process, which slife2 may fail to reach without
    anything being wrong; a plugin is one slife2 starts, and the hub treats it
    accordingly (`required` on an `Upstream`, which is also what makes its tools
    ask before they are offered to the model).

    What it shares with every other entry is the mechanism — a URL, a connection,
    a tool list — and that is the point of the hub having two *sources* rather
    than two code paths.  The address comes from `servers:`, so a config that
    moves a port moves both halves at once and there is no second place to
    update, and there is no list of plugins here at all: it is
    `Config.plugins()`, which is what the launcher starts.
    """
    return ToolServerSettings(
        name=name,
        kind=PLUGIN,
        url=config.server(name).url,
        description="",
    )


def build_server(
    config: Config,
    *,
    transports: Mapping[str, Callable[[ToolServerSettings], Any]] | None = None,
    client_factory: ClientFactory | None = None,
    embedder: Embedder | None = None,
) -> FastMCP:
    """Build the toolhub.

    `transports` maps a plugin's configured name to something a `Client` can be
    built from — the seam that lets a test drive the whole hub over in-memory
    servers, with no process and no port, while production builds a connection
    from the config entry.  `client_factory` is the narrower seam on top of it,
    for the tests that need a client which misbehaves.

    `embedder` is the third, and it is not a seam in the same sense: the
    catalogue is a *file* this process opens, and its vectors come from the
    embedding model the config names.  Injecting one is what lets a test index a
    catalogue with no endpoint behind it, exactly as it does for the turns.

    **The hub's own tools are rows too.**  `tool_search`, `func_tool_load` and
    `_func_tool_unload` are this plugin's, and the hub merges them into the
    catalogue when it starts — so one query answers "what may the model call",
    with no list of exceptions kept beside it.  What has a *body* is still only
    known here: the row says what the tool is, and `local_route` says what
    running it means.

    **This function is long and is not going to be split.**  Measured: of its
    455 lines, 202 are docstrings and 80 are blank or comment, leaving 173 lines
    of code across fifteen nested helpers — the longest run of them is 22 lines.
    What it looks like it wants is a `Hub` object holding `upstreams`,
    `catalogue` and `mcp`, and what that would buy is moving the same prose and
    the same closures one indent to the left, at the price of a name that has to
    be threaded through every one of them.  The nesting here is the *state*, not
    an accident; the model-facing prose it used to mix in lives at module level
    already (`_results_as_text`, `_load_as_text`, `_unload_as_text`).
    """
    directory = data_dir()

    def default_transport(settings: ToolServerSettings) -> Any:
        return mcp_config(settings, cwd=str(directory))

    async def open_catalogue() -> ToolStore:
        """The tool catalogue's own file, with both its indexes current.

        **Opened here rather than reached over a wire**, which is the whole of
        what happened to the `db` plugin: the catalogue has one writer — this
        process — and "one writer" is what a SQLite file already guarantees.
        Opening it can fail (a file this build cannot read, a directory that does
        not exist), and that still fails the tool list rather than shortening it,
        for the reason `list_tools` gives: a hub that cannot say what the model
        may call is not a hub with fewer abilities.

        The **sync runs here, once**, and it is the same pass the turns get: an
        index built by another model, or by other normalization rules, is an
        index this one cannot search, and the answer is to build it again from
        the rows rather than to read numbers that do not mean what they say.

        `transports` is still the seam at the level below — every *upstream* is
        built from a wired entry in a test — and the catalogue is deliberately
        no longer one of them: a file is not a plugin, and a test that wants it
        elsewhere sets the data directory, which is what `conftest.py` does for
        every test in this suite.
        """
        store = await asyncio.to_thread(
            ToolStore,
            tools_db(),
            threshold=config.tool_load.threshold,
            known=frozenset(config.plugins()),
        )
        status = await store.sync_indexes(await embedding.get())
        logger.info("indexed the tool catalogue at %s (%s)", store.path, status)
        return store

    #: The embedder, opened on first use: the catalogue's rows are placed in a
    #: vector index by the same model the turns are, through the same hop.
    embedding = EmbedderConnection(config, embedder=embedder)

    #: The catalogue, and the one file it opens when something first asks.
    catalogue = Catalogue(open_catalogue, embedding.get)

    #: The plugins first, in the order slife2 starts them, and required — see
    #: `list_tools`.  The hub asks every one of them, including the ones with
    #: nothing to offer the model: which
    #: tools a server has is not knowable without asking, and a second list of
    #: "plugins worth asking" is a list that goes stale the first time
    #: somebody adds a tool.
    #: **Ours, and only ours.**  Every one of these is a server slife2 starts,
    #: which is why they are all `required` and why their tools have to declare
    #: themselves the model's.  Somebody else's servers are not here at all: a
    #: plugin holds those and *declares* them, and this process holds the set.
    upstreams: list[Upstream] = [
        Upstream(
            plugin_settings(config, name),
            transport=(transports or {}).get(name, default_transport),
            catalogue=catalogue,
            client_factory=client_factory,
            required=True,
        )
        for name in config.plugins()
        if name != CONFIG_KEY
    ]

    #: Every source a plugin declared, and which plugin holds it — the hub's
    #: address book for calls it cannot make itself.  **Rebuilt from the
    #: declarations on every refresh**, because a source is not a connection this
    #: process keeps and there is nowhere else the fact could live.
    held: dict[str, DeclaredSource] = {}
    #: And who holds each one, which is the route a call takes.
    holders: dict[str, Upstream] = {}

    def by_source(name: str) -> Upstream | None:
        """Who to ask for one source: the connection we hold, or the plugin.

        A direct upstream first, because those are the names this process is
        configured with; a declared source name may not collide with one, so the
        two can never disagree about who owns a name.
        """
        for one in upstreams:
            if one.settings.name == name:
                return one
        return holders.get(name)

    def live_sources() -> list[str]:
        """The sources whose tool lists are in hand, plus this one.

        **Liveness is the hub's to know** — it is the process holding the
        connections — and this is the same fact `usable` already is, handed to
        the catalogue so that a server which is down contributes nothing without
        the db having to know anything about connections.

        A *declared* source is live when the plugin that holds it says it is up,
        which is the one part of this that now arrives over a wire rather than
        from a connection of our own — see `refresh_declared`, which is why the
        answer is as fresh as the last refresh and no fresher.

        The hub's own name is in the list because its tools have no connection
        that could be down: they are functions in this process, so its source is
        live whenever the process is.
        """
        return (
            [one.settings.name for one in upstreams if one.usable]
            + [CONFIG_KEY]
            + [one.name for one in held.values() if one.up]
        )

    async def refresh_declared() -> None:
        """Ask every answering plugin for the sources it holds, and record them.

        **Everything that is not one of our own connections is reached this
        way**: somebody else's servers, a folder of playbooks, a list of
        commands.  A search reads the catalogue, so a tool nobody declared a row
        for is one only a model that already knew its name could call.

        **Non-blocking by construction, because this is on the search path.**  A
        plugin that is not up contributes nothing and is asked again next time;
        `begin_connecting` is called first so that next time is sooner, and it
        starts an attempt rather than waiting for one.  Waiting — even `settle`'s
        bounded wait — would put a plugin's start-up time inside a `tool_search`,
        which is the call a model makes while it is stuck.  What a plugin
        declared last time is still in the catalogue, so a late one costs
        freshness and nothing else.

        **The two names that cannot be declared are checked here**, and only here
        can they be: a source may not take a name this process is already
        connected to, and two plugins may not claim one source between them —
        a merge is the whole truth about a source, so two holders would each
        purge the other's rows on every ask.
        """
        begin_connecting()
        known = {one.settings.name for one in upstreams} | {CONFIG_KEY}
        held.clear()
        holders.clear()
        for upstream in upstreams:
            if not upstream.usable:
                continue
            for one in await upstream.declare():
                if one.name in known:
                    logger.warning(
                        "%s: %r is a name this hub is already connected under",
                        upstream.settings.name,
                        one.name,
                    )
                    continue
                if one.name in holders:
                    logger.warning(
                        "%s: %r is already declared by %s",
                        upstream.settings.name,
                        one.name,
                        holders[one.name].settings.name,
                    )
                    continue
                known.add(one.name)
                held[one.name] = one
                holders[one.name] = upstream

    #: The tools this process serves itself.  Built once — a local tool's
    #: *schema* is a constant, and only the data its body reads can change,
    #: which it re-reads on every call.  `live_sources` is handed in because the
    #: trim asks the catalogue for what the model is *holding*, and only this
    #: process knows which sources are answering; `refresh_declared` because a
    #: search is where a plugin's declarations have to be current.
    local: list[LocalTool] = local_tools(
        catalogue,
        live_sources,
        refresh_declared,
        lambda: [one.name for one in held.values() if one.autoload],
    )

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

        **The plugins are asked for their tools, and everything this process
        already knows is written down.**  The upstreams find theirs by
        connecting, this process's own three are known without connecting to
        anything, and what each plugin *holds* is asked for once every plugin
        that answered is in hand.

        **The order is load-bearing twice.**  This process's own rows go first,
        because this has to happen before the first `list_tools` — the model's
        first answer would otherwise be missing the tools that find and load
        tools.  And the declarations come last, because they arrive from a
        plugin, so they cannot be asked for until the listing that says which
        plugins have any is in hand: `settle`'s bounded wait is what lets a
        plugin that is still starting be one of them, and one that is not is
        asked again by the next ask (`refresh_declared`).
        """
        category = PLUGIN
        await catalogue.merge(
            CONFIG_KEY, category, [_row_of(CONFIG_KEY, one.tool) for one in local]
        )
        begin_connecting()
        await settle()
        await refresh_declared()

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
            # In-process, so this releases nothing; it is here because the
            # embedder it opened *is* a connection, and a hub that exits leaving
            # one open is a hub whose last act is a half-closed socket.
            await catalogue.close()
            await embedding.close()

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
        The four tools that find, load and trim are always in it: they are the
        mechanism, and a budget that could take them away would leave the model
        holding a set it cannot change.

        A plugin's tools are left out unless they declare themselves the
        model's (`slife2.audience`); a tool server's are all offered, because the
        operator put the server in the config.  Both were decided when the
        listing was merged, so nothing in this answer says which is which — by
        the time a tool is listed, the question has been answered.

        Raises:
            ConnectionError: If a plugin — or the catalogue itself — is not
                answering.  Deliberately not a shorter list instead: a plugin
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
        #
        # The declarations are refreshed here as well as before a search, and it
        # is not symmetry: `live_sources` is what this answer gates on, and it
        # now comes from what a plugin last said rather than from a connection
        # this process holds.  Without this, a source that had just gone down
        # would keep its tools in the model's list for one more call.
        begin_connecting()
        await settle()
        await refresh_declared()

        missing = [
            row
            for one in upstreams
            if one.required and not one.usable
            for row in (one.snapshot(),)
        ]
        if missing:
            raise ConnectionError(
                "a plugin is not answering — "
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
        `_func_tool_unload` — is answered here without leaving the process.  A
        plugin's tool reaches it over the connection this process keeps, and one
        a plugin *declares* is routed through the plugin that holds it
        (`call_source`); either way a call is gated on there being something
        behind the name, never on the load state: a name the model just found
        with `tool_search` is a name it can use.

        A call that is made on behalf of one conversation carries that
        conversation in its `_meta`, and it is forwarded unchanged to whichever
        server ends up running the tool.  Nothing here reads it — one hub serves
        every conversation, so it is the far end that can act on it — and
        nothing here *adds* to it: what the caller said it was is the whole of
        what the far end is told.

        Args:
            name: The tool's advertised name — its own for ours, `server__tool`
                for somebody else's (`model_name`).
            arguments: Its arguments, as the tool's schema describes them.

        Returns:
            `text` — the result, or why there is none — and `ok`.
        """
        # Local first: a tool with no server behind it is answered here, and
        # this is the only branch in the hub that does not end in a connection.
        one = local_route(name)
        if one is not None:
            # A local tool is the one kind with no server to phrase a refusal,
            # so its failure has to be phrased here.  This is the same contract
            # the rest of the path keeps — a `ToolError` is a value the model
            # reads and acts on — and without the wrapper an exception escaping
            # a body becomes an MCP error carrying our own traceback text,
            # which is neither actionable nor true.
            try:
                text, ok = await one.run(arguments)
            except Exception as exc:  # noqa: BLE001 — a tool failure is a message
                logger.exception("%s failed", name)
                return {"text": f"{name} failed: {exc}", "ok": False}
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
            #
            # The declarations are refreshed here for the same reason and one
            # more: `by_source` can only answer for a source a plugin has *told*
            # us about, so a row written by a plugin that has not been asked
            # since would otherwise fall through to "not a server this hub is
            # configured with any more" — a false statement about a tool the
            # operator wrote down.
            begin_connecting()
            await settle()
            await refresh_declared()
            row = await catalogue.route(name)
        if row is None:
            return {"text": await unknown_tool(name), "ok": False}

        if str(row.get("category") or "") in (SKILL, CLI):
            # A row a search can find and a call cannot reach, which is the
            # shape of both document families.  Said here rather than left to
            # the branch below, whose answer ("not a server this hub is
            # configured with any more") would be false: these have never had a
            # server behind them, and the model reached this name by reading a
            # search result that told it so.
            return {"text": document_is_not_callable(name, row), "ok": False}

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

        remote = str(row.get("remote_name") or name)
        if source in holders:
            # **Declared, so somebody else holds the link.**  The hub owns the
            # set and not the connection: what it knows is which plugin to ask,
            # and the far end's own name for the tool travels with the call,
            # because the plugin is not the one that named it.
            text, ok = _unpacked(
                *await upstream.call(
                    CALL_SOURCE,
                    {"source": source, "tool": remote, "arguments": arguments},
                    forwarded,
                )
            )
        else:
            text, ok = await upstream.call(remote, arguments, forwarded)
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
        """Trim the model's tool list — the harness's call, and the model's too.

        **Two callers, and the same name, deliberately.**  The agent server runs
        this at a turn boundary (no names: enforce the budget), and the model
        may run it too (names: the tools it is done with).  The second caller is
        what makes the *first* one visible: the harness writes its trim into the
        conversation as a tool pair, a pair names the tool it calls, and v1's
        rule is the one to port — a pair whose name is not in the request's
        declared tool list is a call the Responses and Messages backends reject.
        So this is the single `_`-prefixed name a model sees (v1's exception,
        and the reason `slife2.toolhub.model_name` does not filter it), and the
        harness's own trim is a call this tool could genuinely have made.

        **Why the trim is a call at all, rather than the gate dropping the
        excess.**  Because somebody has to *know*: the tools the model has
        loaded are what its next request carries, and a list that quietly lost
        three of them between two turns is a model that looks for a tool it
        still believes it has.  The harness reads the names back for its log,
        and the model reads them in the pair — the two halves of that sentence
        are why the answer is names and text rather than a boolean.

        A turn boundary is also the right moment, and the reason it is not the
        gate: the list is rebuilt before every model call, so trimming it
        mid-turn would take away a tool the model had just loaded and was about
        to use.

        **This is the hub's API, not the model's route.**  The model reaches the
        same body through `call_tool` — it is a `LocalTool` like `tool_search` —
        but the harness calls *this*, so a trim cannot be blocked by the
        catalogue, the routing or the model's list being in some other state
        than the harness expects.

        Args:
            names: The tools to take out — one or several.  **Empty means
                "enforce the budget"**: whatever is over
                `tool_load.threshold` goes, least recently used first, and none
                of this process's own tools and nothing marked `autoload: true`
                is ever a candidate.  Naming one of the four this system works
                by is refused rather than obeyed.

        Returns:
            `unloaded` — the names that moved, which is the answer this exists
            for — plus `refused` (named but not unloadable), `unknown` (no row
            has that name) and `not_loaded` (named but already out of the
            list), and `text`, the same thing said in a sentence.
        """
        found = await unload_tools(
            catalogue,
            live_sources(),
            list(names or []),
            [one.name for one in held.values() if one.autoload],
        )
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
            `rest` or `plugin`), `transport`, `state` (`ready`, `connecting`,
            `failed` or `idle`), how many `tools` it last offered, how many of
            them are `loaded` (which is what the model has now), `autoload` —
            whether they are wanted every turn, so they are never evicted — the
            `description` it was configured with, the `error` if there is one,
            and `required` — whether slife2 starts it, which is what decides if
            its absence fails a turn or merely shortens the tool list.  A
            plugin that offers fewer tools than it has is the normal case,
            not a fault: the rest are its own code's, and this count is the one
            the model sees.
        """
        # Settles for the same reason `list_tools` does: this is the answer to
        # "why is my tool missing", and a server that failed to start a moment
        # ago reads as one that is still starting.  The states are different
        # problems and this is the tool that is supposed to tell them apart.
        #
        # **One row per source, whichever way the hub reaches it** — the plugins
        # it connects to itself, and the sources those plugins hold on the
        # operator's behalf.  That second kind is why this is still the answer to
        # read: what a plugin fronts is a server in its own right, with its own
        # health and its own error, and reporting the plugin in its place would
        # hide exactly the thing a person came here looking for.
        begin_connecting()
        await settle()
        await refresh_declared()
        counts = await catalogue.sources()
        return {
            "servers": [
                *(
                    upstream.snapshot(counts.get(upstream.settings.name))
                    for upstream in upstreams
                ),
                # Two kinds of declared source are not reported, and neither is
                # a gap: one the operator switched off is not connected, so there
                # is no connection to have a state; and one with no transport has
                # nothing to connect to at all — a folder of playbooks is a
                # source of rows, not a server, and this answer is about servers.
                *(
                    one.snapshot(counts.get(one.name))
                    for one in held.values()
                    if one.enabled and one.transport
                ),
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
        "serving %s on http://%s:%d%s (%d plugin(s) to ask)",
        SERVER_NAME,
        args.host or settings.host,
        args.port or settings.port,
        settings.path,
        len([name for name in config.plugins() if name != CONFIG_KEY]),
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
