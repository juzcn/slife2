"""slife2-toolhub — the model's tools, and the one process that decides them.

Every other plugin in this system is a place a capability comes from: a model
backend speaks one wire protocol, the db keeps turns, the agent loop runs turns.
This one is where **the tools come from**, and it exists because tools are the
one capability that has to reach *outside* the machine — to somebody else's MCP
server, to a REST API, to a program that wants an API key in its environment.

    slife2-agent  ──MCP──▶  slife2-toolhub  ──MCP──▶  plugins      (ours)
                             list_tools                    │
                             call_tool                     ├─ their own tools, by `tools/list`
                             servers                       └─ what they declare: a `mcp:` or
                             _func_tool_unload                `rest-api:` entry, held and called
                                    │                         by mcp-tools / restapi-tools
                                    └──in-process──▶  `slife2.libs.store.catalogue.ToolStore`
                                                       (the catalogue's own file)

**What this process owns is the set, and it owns all of it.**  Which tools
exist, what the model is holding, what a name resolves to, what the budget takes
back, and who may call what — every one of those is a question about the *whole*
list, so it is answered in one place or it is answered twice and differently.
What it does **not** own any more is a connection to anybody but our own plugins:
the servers under `mcp:` and `rest-api:` are held by `slife2-mcp-tools` and
`slife2-restapi-tools`, which say what they hold and run a call back when this
process asks.  The credential story is unchanged — a tool server's key lives in
the process that reaches it, and the agent loop never sees one — but the process
that holds it is the plugin that owns the config section, not this one.
`slife2.clients.gateway` is the link itself; `slife2.libs.plugin.family` is the half that the two
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
the model's (`slife2.libs.plugin.audience`), because they belong to that plugin's code:
`remember` writes into any agent's database and `send_message` drives another
conversation, and those are exactly the tools a model would reach for if it
could read their descriptions.  **What a plugin holds** arrives by declaration
instead — one tool, `list_sources`, answering with a source's whole list — and is
not gated, because it is the operator's configuration: a `mcp:` entry, a
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
*caller* it is for does, and that is said on the tool itself (`slife2.libs.plugin.audience`)
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
is a catalogue entry until one of them is wanted, and so is a plugin's tool the
operator did not put in `tool_load.autoload` — the list of names that are wanted
every turn, and the only tools the budget may never take back.  The catalogue is
where that state lives and the db is where it is kept, so a tool loaded in one
conversation is loaded for the next, and a tool loaded yesterday is still loaded
after a restart — the two things a snapshot in this process could never do.

The budget that bounds it is enforced by the *harness*, not by the gate:
`_func_tool_unload` is called by the agent server before it saves a turn, and the
names it unloaded come back — to the harness's log, and to the model, as the
tool pair the trim is recorded as.  The leading underscore is the system's mark
for a tool the machinery calls rather than one a model chooses, and the names
that carry it *and* are in the model's list are the ones whose pairs the harness
writes into a conversation — the trim, and the message a running turn is handed
(`_check_new_input`).  A pair has to name a declared tool, so the model has them
too; `ALWAYS_LOADED` is where they are kept from leaving.

Why the tools are *here* and not in the agent
---------------------------------------------
`now` and `calc` are pure functions with no process and no credential, so
reaching them through a hub costs a hop — and they are behind one anyway,
because **the model's tool list is one thing and it should have one owner.**
Provenance (whose tool is this), the naming rule that keeps two servers' `search`
apart, and — the first time it appears — the question of which tools may run
without asking the user, are all questions about the *set*, and a set assembled
in two places is a set that will disagree with itself.

That is why no plugin's tools are served by this process — each one is reached
exactly as somebody else's arxiv server is, over the plugin that holds it.
**Nothing that has a server behind it is served by this process.**  A tool that
took a shortcut would be the second mechanism this whole arrangement exists to
avoid, and the first thing to drift: it would not be in `servers()`, it would not
have a connection to fail, and it would not be a row in the catalogue the model's
search reads.

`skill_use` used to be the exception, and it is one no longer: a skill is a
document and `slife2-skills` serves it (`slife2.plugins.skills.server`), because a family
that owns a config section *and* its own tool is a family whose next change has
somewhere to land.  So the hub's own tools are exactly the set-level three —
`tool_search`, `func_tool_load`, `_func_tool_unload` — and they are named as
themselves rather than `{server}__{tool}`, because there is no server to name.
Neither are ours for the neighbouring reason that the server is slife2:
`turn_read`, not `context__turn_read`.  `model_name` is that rule and DESIGN.md
DESIGN.md §4.1 is the argument for it.

They are still *rows*, though — owned by this plugin, like every other tool's
is owned by its source.  That is what makes one query enough to answer what the
model may call, with no list of exceptions kept beside it here.

REST APIs are not a second mechanism
------------------------------------
A `rest-api:` entry is expanded *by the config layer* into the stdio command that
serves it (`slife2.libs.host.config._rest_api`), so what arrives at `slife2-restapi-tools`
is an ordinary stdio server — the same kind of thing a `mcp:` entry describes,
held by the same code (`slife2.libs.plugin.family`) and declared to this process the same
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
import json
import logging
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.clients.embedder import EmbedderConnection
from slife2.clients.gateway import (
    ClientFactory,
    Connection,
    mcp_config,
    proxied_name,
    sanitise,
)
from slife2.clients.toolclient import UpstreamTool
from slife2.helpers.threads import off_loop
from slife2.libs.host.config import (
    TOOLHUB_SERVER_NAME,
    Config,
    ToolServerSettings,
    for_the_call,
    load_cached,
)
from slife2.libs.host.paths import catalogue_db, data_dir, in_home
from slife2.libs.host.paths import home as home_dir
from slife2.libs.plugin.audience import for_the_model, forwarded_client, request_meta
from slife2.libs.plugin.family import settle_attempts
from slife2.libs.plugin.protocol import (
    CALL_SOURCE,
    LIST_SOURCES,
    RETURNS,
    house_server,
    serve_plugin,
)
from slife2.libs.plugin.protocol import (
    CLI_SOURCE as _CLI_SOURCE,
)
from slife2.libs.plugin.protocol import (
    SKILLS_SOURCE as _SKILLS_SOURCE,
)
from slife2.libs.store.catalogue import ToolStore
from slife2.libs.store.index import Embedder
from slife2.libs.store.textindex import terms
from slife2.libs.turn.tools import FUNC_TOOL_UNLOAD as _FUNC_TOOL_UNLOAD
from slife2.libs.turn.tools import as_tool_text

logger = logging.getLogger(__name__)

SERVER_NAME = TOOLHUB_SERVER_NAME

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
#: (`slife2.libs.store.catalogue.PLUGIN`), and the two categories nothing is connected to
#: (`slife2.libs.store.catalogue.SKILL`, `slife2.libs.store.catalogue.CLI`).  Spelled in two modules
#: rather than imported for the reason the config gives: the hub must not import
#: a server, and the db was one.  They are facts about the *system* — a plugin is
#: a server slife2 starts, a skill is a document, a command is a program already
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
#: process does not**.  Spelled here rather than imported from `slife2.plugins.skills.playbooks`,
#: because a name crossing a process boundary is spelled on both sides and the
#: hub has no other business with the module — `tests/test_config.py` holds the
#: two spellings together.  What the hub uses it for is the one thing that is
#: still the hub's: not letting the model throw it away (see `ALWAYS_LOADED`).
SKILL_USE = "skill_use"

#: The two **source** names the document families file their rows under, which
#: are `slife2.plugins.skills.server.SOURCE` and `slife2.plugins.cli.server.SOURCE` — spelled
#: here for the reason above, and held together with those two in
#: `tests/test_config.py`.  The catalogue files a document under these, and the
#: boot pass has to be able to name them without asking anybody (see
#: `configured_sources`), which is the one thing this process needs them for.
#:
#: `CLI_SOURCE` and `CLI` are the same word and stay two names: a *source* is
#: what a row is filed under, and a *category* is what it is — and for `cli:`
#: entries the operator's section happens to be called the same thing twice.
#: Re-exported rather than defined here: the two declaring plugins and the
#: two family plugins need these names too, and a plugin reaching into this
#: one for a string is how `slife2-mcp-tools` came to import the whole hub.
#: See `slife2.libs.plugin.protocol.SKILLS_SOURCE`.
SKILLS_SOURCE = _SKILLS_SOURCE
CLI_SOURCE = _CLI_SOURCE

#: The trim, and the only name here with **two callers**.  The leading
#: underscore is the convention — **a name beginning with `_` is a harness
#: tool**, one the machinery drives rather than one the model chooses — and this
#: is one of the exceptions to it: the model has it too, because the harness's
#: trim is recorded in the conversation as a tool pair, and a pair names a tool
#: the request declares (v1's rule, and the reason its `_func_tool_unload` is
#: a `_` tool a model sees).  The other is `_check_new_input`, which reaches the
#: model the same way from the other direction — a message delivered inside a
#: running turn (`ALWAYS_LOADED`, and `slife2.plugins.agent.server`).  So the model may
#: call it with the names it
#: is done with, while the *harness* calls it with none — on this server's API
#: rather than through `call_tool`, so the trim does not depend on the
#: catalogue, the routing or the model's list being in any state.  See
#: `build_server`'s `_func_tool_unload`.
#: Re-exported for the same reason, and one better: the *agent server* is the
#: other end of this hop, and it holds `slife2.libs.turn.tools.FUNC_TOOL_UNLOAD`.  Two
#: copies of a tool name across a wire is a rename that half-works.
FUNC_TOOL_UNLOAD = _FUNC_TOOL_UNLOAD

#: What a model may not take out of its own list.  The three this process
#: serves are how a tool list is managed at all — a budget that could take them
#: away would leave the model holding a set it cannot change — and the fourth is
#: `skill_use`, which reads the playbooks every session is meant to reach for.
#:
#: **It lives in a plugin and is still on this list.**  It is also in
#: `tool_load.autoload` by construction (`slife2.libs.host.config.REQUIRED_AUTOLOAD`), so the
#: budget could not evict it; what this adds is the other half: the model naming
#: it in `_func_tool_unload` and being obeyed.  One string is cheaper than a
#: column the row would have to carry to say "the system needs me", and the name
#: it must match is `slife2.plugins.skills.playbooks.USE_TOOL`.
#:
#: **This is a question about unloadability and not about who calls a tool** —
#: `slife2.libs.turn.tools` separates the two and this set is where the third one is
#: answered.  So it holds `skill_use`, which nothing but the model drives,
#: alongside the harness tools, which the machinery drives and the model may call
#: too; what all of them share is that something *else* refers to them by name.
#:
#: **A tool the model calls for itself is not one of them**, however important:
#: `attach_image` is a plugin's ordinary model-facing tool and is not here,
#: because nothing *else* names it — no pair the harness writes, no prompt the
#: system builds.  Being wanted in every turn is a separate answer, and it is
#: `tool_load.autoload`'s: the operator lists the tools that start loaded, and
#: the budget protects that list rather than this one.
#: `CHECK_NEW_INPUT` is the sharpest case of that and is spelled below
#: (the hub must not import a server): a pair the harness wrote into the history
#: names it, so a turn that unloaded it would send a request whose history calls
#: a tool its `tools` array no longer declares — a 400, and one the model could
#: not have avoided.
ALWAYS_LOADED = frozenset(
    {
        TOOL_SEARCH,
        FUNC_TOOL_LOAD,
        FUNC_TOOL_UNLOAD,
        SKILL_USE,
        # slife2.plugins.agent.server.CHECK_NEW_INPUT — the tool a message that arrives
        # mid-turn is delivered under.  Spelled here rather than imported for the
        # reason this module spells `SKILL_USE`: the hub is the model's tool
        # list, and it must not be importable from an agent.
        "_check_new_input",
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

#: How many names one `func_tool_load` may carry.  Each name is its own
#: catalogue write and its own embedding call — the loop is a `for` over a hop
#: rather than a batch — and the model's list is trimmed to the budget at every
#: turn boundary, so a list longer than the budget holds is one the next trim
#: takes back at the price of embedding everything in it.
MAX_LOAD_NAMES = 50


def model_name(server: str, tool: str, category: str) -> str:
    """What the model calls one tool: **the bare name for ours**, `server__tool`
    for everybody else's.

    `context__turn_read` would be a name telling the model about a division it
    has no business reasoning about: there is one set of tools here, slife2's,
    and `turn_read` is the name of one of them.  A server in front of a name earns its
    place by keeping two of *somebody else's* tools apart — the operator may
    write down four servers that each offer a `search`, and `arxiv__search`
    against `serper__search` is the difference between a call reaching the tool
    the model read about and one reaching a stranger.

    `category` is the row's own word for where a tool came from
    (`slife2.libs.store.catalogue.PLUGIN` for ours, `mcp`/`rest` for the rest), so the rule reads
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
    `slife2.libs.store.catalogue.ToolStore`, opened on first use, and every call below is a method
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
        #: One store per home, keyed by the data directory it was opened in.
        self._stores: dict[str, ToolStore] = {}
        self._opening = asyncio.Lock()
        #: Why the catalogue could not take each source's listing, **by home and
        #: then by source** — see `failure`, which is the only reader, and
        #: `_write`, which is where it is written.
        #:
        #: The home is the outer key because a write is *to one home's file*:
        #: `_write` is called with the key of the file it writes, and the fan-out
        #: in `remember` calls it for every other home as well.  Keyed by source
        #: alone, a file that will not take a listing in one home failed
        #: `list_tools` in all of them — with a reason naming a source and no
        #: home, in a turn whose own catalogue was working, which is the
        #: module's third failure rule applied to the wrong party.
        self._failed: dict[str, dict[str, str]] = {}
        #: What the sources **this machine** reaches last said — the hub's own
        #: rows, and every plugin's listing — by source: its category and its
        #: rows.  See `remember`, which is the only writer, and `seed`, which is
        #: what a home that has not asked yet is built from.
        self._held: dict[str, tuple[str, list[Mapping[str, Any]]]] = {}

    @property
    def failure(self) -> str:
        """Why the catalogue could not take one of **this home's** listings, or `""`.

        **A catalogue that did not work is a broken system, and this is where
        that fact is kept.**  Everything this process answers about the tool set
        is a call to the catalogue, so the one thing that must not happen is the
        failure being reported against the *source* that happened to be listed at
        the time — "that tool server is broken" would send the operator to the
        wrong place, which is the module docstring's third failure rule.

        A *state* rather than a raised exception, because one of the callers runs
        inside a connect attempt nothing awaits (`Upstream._listed`): an
        exception raised there is retrieved by nobody and arrives, minutes later,
        as asyncio's "Task exception was never retrieved" — a message naming no
        server and no reason, which is worse than either.  Kept here, the reason
        reaches `list_tools`, which fails the whole list with it, and `servers`,
        which reports it (`build_server`).

        **Keyed by source, because one source's success is not another's health.**
        A single slot would be cleared by the next merge that worked — a plugin
        whose rows are all unchanged, or one that offers no tool at all, embeds
        nothing and so writes nothing — and the hub would be back to reporting a
        listing it could not record as a source that is merely "connecting".  An
        entry goes when a merge for *that* source works, which is exactly when
        the fact stops being true.

        **And reported for the home whose file it was**, which the source alone
        cannot say: the fan-out writes one listing into every home
        (`remember`), so a key of source only would carry one home's broken file
        into every other home's `list_tools` — "the tool catalogue is not working
        — context: …" in a turn reading a catalogue that is working.  What this
        home's answer is made of is therefore this home's own entry, and the
        other homes' are read by their own asks.
        """
        return "; ".join(
            f"{source}: {reason}"
            for source, reason in self._failed.get(str(home_dir()), {}).items()
        )

    async def store(self) -> ToolStore:
        """The catalogue of **the home this call is for**, opened on first use.

        One file per home (`slife2.libs.host.paths.catalogue_db`), and the key is
        the ambient home rather than a parameter because every caller of this is
        inside a call: the hub is shared, so a `tool_search` from one home must
        read the catalogue that home is configured with, and a `func_tool_load`
        must not write a loaded tool into another home's file.

        Opened here rather than at start-up for the reason the whole class is
        lazy: a home is a data directory the hub may never have heard of until
        somebody in it asks something, and opening every home's catalogue when
        the process comes up would be work for directories nobody is using.

        **And a store opens complete**, which is `seed`'s doing and not
        `refresh_declared`'s: the hub reaches its plugins itself, and what those
        links last listed is a fact about *the machine*, so it goes into every
        home's file (`remember` holds it for the homes that have not asked yet).
        What is genuinely this home's is the declarations — the `mcp:` entries,
        the playbooks, the commands of *this* section — and those are what a
        per-home refresh merges.

        **The lock is held for the whole of that**, and `remember` takes it for
        its hold and fan-out, so "complete" has no window: a listing that lands
        during an open is either in `seed`'s snapshot or in the fan-out's list,
        and never in neither.  Registering the store after `_open` returns is
        therefore not a race with that fan-out but the other end of one handoff:
        a fan-out cannot run while the lock is held, so one that has not seen
        this file has already left its rows in `_held` for `seed` to write.
        """
        key = str(home_dir())
        found = self._stores.get(key)
        if found is None:
            async with self._opening:
                found = self._stores.get(key)
                if found is None:
                    found = await self._open()
                    self._stores[key] = found
        return found

    async def merge(
        self,
        source: str,
        category: str,
        tools: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Record a source's whole tool list **in this home's file**.  See
        `slife2.libs.store.catalogue.ToolStore.merge`.

        This is the *per-home* write, and the one thing that makes it per-home is
        what it is for: a declaration is a statement about the caller's own
        `slife2.yaml` section — the `mcp:` entries, the playbooks, the commands of
        *that* directory — so it belongs in that directory's file and nowhere
        else.  What the sources *the hub reaches itself* say is a fact about the
        machine and takes `remember` instead.

        **Nothing about the operator's list travels with a merge.**  Which tools
        start loaded is `tool_load.autoload`, read where the catalogue is opened
        (`ToolStore.autoload`), and a merge only seeds a *new* row from it.  The
        old `autoload` a plugin declared here was a fact about a section; the
        list is about the model's tool names, and those are the hub's own
        namespace.
        """
        store = await self.store()
        return await self._write(str(home_dir()), store, source, category, tools)

    async def remember(
        self,
        source: str,
        category: str,
        tools: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Record a source **the whole machine has**, in every home's file.

        **The hub's own connections are the machine's, and so are their rows.**
        `upstreams` are one set of links for every home (`build_server`), so what
        they list is one fact — and a fact written into only the home that
        happened to be in front of the hub when the link came up is a fact every
        *other* home's file is missing.  That is not a corner: a plugin's listing
        is re-read only when the link is due (`gateway.Connection.connecting`,
        `timeouts.pacing.relist_after`), so a second home's ask could not repair
        it by waiting, and this is where it is repaired instead — by writing what
        the link did list into every home, and holding it for the homes that have
        not asked yet (`seed`).

        Written to *every open store* rather than to the ambient one, and each
        one through its own home's embedder (`_embedder_in`), because two homes
        may be configured with two different embedding models and an index built
        by one of them is not searchable by the other.

        Reached from the two places that speak for the machine: `Upstream._listed`
        and the hub's own rows at start-up.

        **The ambient home is written first, through `store()`**, and that is not
        politeness: a listing that cannot be recorded has to be *this source's*
        problem, and only a write against a real file can refuse one (a name
        another source already holds).  `_listed` is what turns that refusal into
        the source's verdict, and it can only do so if the write happened here
        rather than one call later, at a home's first ask.  The rows are held
        *after* that write, so a refusal is not remembered as the truth.

        **And the hold and the fan-out are one step, under the lock a store
        opens under**, which is what lets `seed` and this function divide the
        work between them with no window: see the comment on the `async with`
        below and `seed`, which is the other half of that handoff.
        """
        rows = list(tools)
        here = str(home_dir())
        found = await self._write(
            here,
            await self.store(),
            source,
            category,
            rows,
        )
        # **The hold and the fan-out are one step, and the lock is `_opening`**
        # — the one `store` builds a file under — because the two writers that
        # can put this source into a home's file have to agree about which home
        # is missing it.  Without it, a listing that lands while a home is
        # opening is written into every home *but* that one: `_stores` cannot
        # hold a file that has not finished opening (publishing it is the last
        # thing `store` does), and `seed`'s snapshot of `_held` was taken before
        # these rows were held — so neither writer covers it, and nothing repairs
        # it until this source's next relist (`timeouts.pacing.relist_after`,
        # minutes) or a restart.  Under the lock the two are exclusive and the
        # split is exact: a store that opens after this step is seeded from
        # `_held`, and one that was already open is in the fan-out's list here —
        # once either way, which matters because two overlapping merges of one
        # source into one file are a race on the tool table's primary key (the
        # same fact `Declarations.lock` is built on).
        async with self._opening:
            self._held[source] = (category, rows)
            for key, store in list(self._stores.items()):
                if key != here:
                    await self._write(key, store, source, category, rows)
        return found

    async def seed(self, key: str, store: ToolStore) -> None:
        """Write everything the machine holds into a home's freshly opened file.

        **Called once, and it is what makes a store complete the moment it
        exists** — including the hub's own tools, which the model needs before it
        can find or load anything else.  A home whose file was built without this
        got the declarations of its own config and nothing else: measured
        against the real plugin set, twenty-four of twenty-eight tools missing.

        Takes the store rather than reaching for it, because `store()` is holding
        `_opening` while this runs and `asyncio.Lock` is not reentrant.

        **And a source that will not go in does not stop the file opening.**
        Writing is where a collision is caught, and a *catalogue* that cannot
        record is a state rather than an exception — `_write` keeps it for
        `list_tools` to fail the whole list with and for `servers` to report, and
        raising here would instead take the store's opening down with it, leaving
        the home with no catalogue at all rather than one carrying the reason.

        **It is the other half of a handoff with `remember`**, which is why that
        one holds `_opening` for its own hold and fan-out: a listing that arrives
        while this runs is either in `_held` already and written by the loop
        below, or held after the open published this file and written by that
        fan-out.  Neither half covers both orders on its own, and the window
        between them is a `seed`'s length — every source the machine holds, each
        with an embedding call behind it.
        """
        for source, (category, rows) in list(self._held.items()):
            try:
                await self._write(key, store, source, category, rows)
            except Exception as exc:  # noqa: BLE001 - kept, and the file opens
                if not _refused(exc):
                    continue  # `_write` has already said why, and kept it
                logger.warning(
                    "the catalogue at %s did not take %s: %s",
                    store.path,
                    source,
                    exc,
                )

    async def _write(
        self,
        key: str,
        store: ToolStore,
        source: str,
        category: str,
        tools: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """One source's rows into one home's file, and the health of the write.

        **A merge is where the catalogue's own health is learned**, which is why
        the failure is recorded here rather than at each call site: every write
        goes through this one, whether it came from a plugin's `tools/list`, from
        a declaration, or from this process's own rows at start-up.  A *refusal*
        is the record answering and saying no, which is the catalogue working —
        the data is not its — and only anything else is kept (`failure`).
        """
        try:
            found = await store.merge(
                source,
                category,
                list(tools),
                embedder=await self._embedder_in(key),
            )
        except Exception as exc:  # noqa: BLE001 - `_refused` is the split, below
            if _refused(exc):
                # The record answered and said no, which is the catalogue
                # working: this source is well again as far as it is concerned,
                # whatever it was told.
                self._failed.get(key, {}).pop(source, None)
            else:
                # Against the home whose file it was, because that is the only
                # one that cannot answer for itself (`failure`).
                self._failed.setdefault(key, {})[source] = (
                    f"{type(exc).__name__}: {exc}"
                )
                logger.warning("the tool catalogue did not take %s: %s", source, exc)
            raise
        self._failed.get(key, {}).pop(source, None)
        return found

    async def _embedder_in(self, key: str) -> Embedder:
        """The embedder of **the home the file belongs to**, not the caller's.

        The difference only shows on the fan-out: a listing that arrives while
        one home is asking is written into every *other* home's file as well, and
        an index placed by the asking home's model would be vectors the owning
        home cannot search — its file would claim an identity its numbers do not
        have, which is the one failure here that would look like success.

        `in_home` is what makes "which home" explicit for the length of one
        lookup, which is exactly the question `embedding_for` caches on.
        """
        with in_home(key):
            return await self._embedder()

    async def source_state(self, source: str, state: str) -> None:
        """Record the verdict on one source: `enabled`, `error` or `disabled`.

        **Written to every open store, and the ambient one first**, exactly as a
        listing is (`remember`), because it is the same fact about the same rows:
        a source the hub reaches itself has its rows written into every home's
        file, and a verdict landing in one of them leaves the others telling
        their models that a broken server's tools are `enabled` and loaded —
        which is what a `tool_search` in a home that was not asking would go on
        offering for the life of the process.  A *declared* source's verdict is
        written the same way and is not a stretch of the rule: the rows it marks
        are the ones that home's own refresh wrote for the same source, and a
        home whose config switched that source off is left alone by the store's
        own `WHERE` (`ToolStore.set_source_state`).

        The ambient home is written first and **through the failure**, which is
        the one home whose answer cannot be kept for later: it is the home the
        ask itself is for.  The others are the fan-out's, and one whose file will
        not take it keeps the reason for its own `list_tools` rather than
        stopping the rest (`_write`'s rule, and `seed`'s).
        """
        here = str(home_dir())
        await off_loop((await self.store()).set_source_state, source, state)
        async with self._opening:
            for key, store in list(self._stores.items()):
                if key == here:
                    continue
                try:
                    await off_loop(store.set_source_state, source, state)
                except Exception as exc:  # noqa: BLE001 - kept, and the rest go on
                    self._failed.setdefault(key, {})[source] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    logger.warning(
                        "the catalogue at %s did not take the verdict on %s: %s",
                        store.path,
                        source,
                        exc,
                    )

    async def forget_source(self, source: str) -> list[str]:
        """Delete a source's rows, because nothing will declare it again.

        **The write that goes with the verdict, and the one case where a source
        is removed rather than reported.**  A plugin that answers without a
        source it held last time is saying the entry has been taken out of its
        section; a row for a source nobody declares is a search hit that leads
        to a refusal, so the rows go with the entry.  See
        `slife2.libs.store.catalogue.ToolStore.forget_source` for why this deletes where an
        unreachable server's rows are only marked, and
        `ToolStore.reset` for the other moment the same thing has to happen.

        Its failure is forgotten here with its rows, because this is the one
        write after which no merge can clear it: nothing will declare the source
        again, so `failure` would carry it for the life of the process.  The
        deletion is this home's — `declare` is asked about one home's section and
        the rows are in that home's file — so the entry forgotten is this home's
        too.
        """
        self._failed.get(str(home_dir()), {}).pop(source, None)
        return [
            str(name)
            for name in await off_loop((await self.store()).forget_source, source)
        ]

    async def injectable(self, sources: Sequence[str]) -> dict[str, Any]:
        """The tools the model may be given now.

        `sources` is the caller's because liveness is: the hub is the process
        holding the connections, and a source is live when its tool list is in
        hand.  See `ToolStore.injectable`.
        """
        return await off_loop((await self.store()).injectable, list(sources))

    async def evict(self, sources: Sequence[str]) -> list[str]:
        """Trim the loaded set to the configured budget; name what it took out.

        The names the budget may never touch come from `tool_load.autoload`, read
        once where the store is opened.  Nothing about a *source* is passed here:
        which tools are protected is a fact about the names the model calls, not
        about which server happens to offer them.
        """
        found = await off_loop((await self.store()).evict, list(sources))
        return [str(name) for name in found]

    async def route(self, name: str) -> dict[str, Any] | None:
        """The row for one advertised name, or `None` if there is no such tool."""
        return await off_loop((await self.store()).route, name)

    async def sources(self, live: Sequence[str]) -> dict[str, dict[str, int]]:
        """Per source: how many tools it has, and how many the model holds.

        `live` is the caller's, and only the second number reads it: what a
        source *offers* is a row on disk, and what the model *holds* is a row
        the gate would hand over — which is `slife2.libs.store.catalogue.ToolStore.source_counts`.
        """
        return await off_loop((await self.store()).source_counts, list(live))

    async def total(self) -> int:
        """How many tools the catalogue holds, all categories included.

        **The number a display shows, and it is not `servers()`' sum.**  Those
        are the model-facing rows grouped by source; a skill and a CLI
        entry are rows this catalogue holds and no server offered, and leaving
        them out would be a smaller answer than the question — "how many tools
        are there" is about everything `ToolStore.merge` has been given, not
        about what a model could call this moment.  See `ToolStore.count`, which
        is the query and the argument.
        """
        return await off_loop((await self.store()).count)

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
        return await off_loop((await self.store()).set_load, name, load_status)

    async def touch(self, name: str) -> None:
        """Mark one tool *called*, for the eviction order.  Best-effort.

        **The budget evicts by this and not by the load stamp** — see
        `slife2.libs.store.catalogue.ToolStore.evict` — and this is the only place it is written,
        so this call is what makes "the least recently used tool goes" true
        rather than merely intended.

        A failure is not the error the model reads — this runs after a call that
        already happened, and the tool *did* run — but it is not nothing either:
        a stamp that did not land leaves the budget evicting the wrong tool, and
        nothing else reads this write to notice.  DESIGN.md §11.1's rule for a bookkeeping
        write is that it either lands or is reported, so it is logged, naming the
        tool, rather than suppressed.
        """
        try:
            await off_loop((await self.store()).touch, name)
        except Exception as exc:  # noqa: BLE001 — a stamp that failed is not a call that failed
            logger.warning("the catalogue did not stamp %s as used: %s", name, exc)

    async def close(self) -> None:
        """Nothing to release, and deliberately still a method.

        The store holds no connection between calls — every method opens one of
        its own — so there is nothing here to hand back.  It exists because the
        lifespan calls it, and a hook that silently does nothing is better named
        than deleted: whoever adds a held resource to the store will find the
        place it has to be released.

        Every home's, because the stores are per home
        (`slife2.libs.plugin.audience.client_key` is the same fact one plugin over).
        """
        self._stores.clear()


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


#: What a source that has never been counted says where its counts go.
#: `ToolStore.source_counts` has no row for a source with no rows at all, so a
#: `tools` of `0` would be a count this process does not have — the absent value
#: wearing the empty one's shape (DESIGN.md §11.1).
NOT_COUNTED = "not counted"


def _server_row(
    *,
    name: str,
    kind: str,
    transport: str,
    state: str,
    counts: Mapping[str, int] | None,
    description: str,
    error: str,
    required: bool,
) -> dict[str, Any]:
    """One row of `servers()`, for a source whichever way the hub reaches it.

    **One shape, because it is one question.**  "Why is my tool missing" has to
    read the same whether the answer is a plugin, an entry under `mcp:` that
    this process connects to, or a source a plugin holds on its behalf — and two
    builders of the same row is how the third one ends up missing a field.

    **A source nobody has counted says so, rather than reporting zero.**  The
    counts come from `catalogue.sources`, which has an entry only for a source
    that has rows; one that never answered has none, and `0` there would be a
    confident count this process does not have — the same confusion DESIGN.md §11.1 names
    between a value that is absent and the empty one it is spelled like.  A
    counted source keeps its number, so a reader that compares the two still can.
    """
    counted = counts or {}
    return {
        "name": name,
        "kind": kind,
        "transport": transport,
        "state": state,
        #: What it last offered, and how much of that the model has now.  A
        #: source with ninety tools and none loaded is a healthy server the model
        #: has not asked anything of — and that is a different answer from a
        #: server that has stopped offering them.
        "tools": counted.get("tools", NOT_COUNTED),
        "loaded": counted.get("loaded", NOT_COUNTED),
        "description": description,
        "error": error,
        #: Ours rather than somebody else's: the answer to "why did a whole turn
        #: fail over a server that was merely down".
        "required": required,
    }


@dataclass
class Declarations:
    """What the plugins hold for **one home**, and the lock that guards it.

    A dataclass rather than three parallel dictionaries in `build_server`,
    because they are one fact written three ways: the sources a plugin declared,
    which plugin holds each, and the lock that keeps two overlapping refreshes
    from interleaving their swaps — and the hub reconciles them *per home*,
    since each home's catalogue is a file of its own.
    """

    #: One entry per declared source, as the plugin answered.
    held: dict[str, DeclaredSource] = field(default_factory=dict)
    #: Who to ask for each one, which is the route a call takes.
    holders: dict[str, Upstream] = field(default_factory=dict)
    #: One refresh at a time for this home — see `build_server`'s
    #: `refresh_declared`, which is the only holder.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
    error: str
    description: str
    transport: str

    def snapshot(self, counts: Mapping[str, int] | None = None) -> dict[str, Any]:
        """This source's row in `servers()`.

        **`off` is the fourth word, and the one that keeps a switched-off source
        reportable.**  Without it the other three all lie about it: nothing was
        asked to connect, so `connecting` is a wait nobody is making and
        `failed` is an error nobody hit — which is what the report used to say by
        leaving the source out, and the reason it was out.  But "why is my tool
        missing" is the question `servers()` answers, and `enabled: false` is the
        commonest reason a *configured* server's tools are not there, so the word
        had to be added rather than the row dropped.
        """
        if not self.enabled:
            state = "off"
        elif self.up:
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
            description=self.description,
            error=self.error,
            # Nothing a plugin holds is one slife2 starts: those are the hub's
            # own direct upstreams, and `required` is what that means.
            required=False,
        )


class Upstream:
    """One source of tools this process reaches directly, and what it means.

    **The link is `slife2.clients.gateway`'s and the meaning is this class's.**  What
    lives here is everything the gateway deliberately does not know: that a
    plugin's tools have to declare themselves the model's before the model is
    given them, that a listing becomes catalogue rows, and that a source which
    stops answering has a verdict written against the rows it left behind.

    **Recorded, not kept.**  An upstream holds a connection and a verdict and no
    tool table: what it last listed is a fact about the catalogue, and a second
    copy here would be a second thing to keep in step — which is what replaced
    the snapshot this class used to keep.  A listing now reaches *every* home's
    catalogue rather than the one in front of this process
    (`Catalogue.remember`), and what waits for a home that has not asked yet is
    the catalogue's own record of it, beside the writes it is the source of
    rather than in the layer that holds connections.

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
        #: `mcp:` sets this — it is what makes a plugin a plugin, and it
        #: is read twice: for that failure rule, and for whether this server's
        #: tools have to ask before the model is given them (`_offered`).
        self.required = required
        #: **Whether this source declares catalogue rows**, which is a fact
        #: about the listing and not a second copy of it: the one tool name
        #: `LIST_SOURCES` either is or is not in what the server offered.  See
        #: `declare`, which is the only thing that reads it.
        self._declares = False
        #: The source names it declared last time, **per home**, so that one it
        #: has stopped holding can be forgotten — see `declare`.
        #:
        #: Per home because *the answer* is: `declare` asks the plugin for the
        #: calling home's section (`Connection._call_once` attaches the caller's
        #: identity, so the plugin's `family.current()` answers that home's
        #: entries), and a set held for the process is therefore one home's
        #: answer being read as another's.  The damage is not a wrong row but a
        #: *deletion*: a home with no `mcp:` entry declares nothing, so the
        #: difference against a set another home's ask filled is that home's
        #: sources — and `forget_source` deletes their rows against *this* home's
        #: catalogue, where they are this home's tools.  The set then held
        #: nothing, so the home that did hold them can never compute the removal
        #: a deletion is for: an entry taken out of its section leaves its rows
        #: in its catalogue for the life of the process, and `tool_search` keeps
        #: offering tools the row's own text says go the next time the plugin
        #: that held them answers.  `Declarations.held`/`holders` are per home
        #: for the same fact seen from the other side, and this is the field that
        #: was left behind.
        self._declared: dict[str, set[str]] = {}
        #: Verdicts written without waiting for them.  Held for the reason
        #: `slife2.plugins.agent.server.detach` holds its own: a task nothing
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

        **The tools go to the catalogues and are not kept here.**  What came back
        is the whole truth about this source, so it is merged — added, updated,
        deleted, or left alone — and this object's own copy of it is still
        nothing at all: `list_tools` reads the catalogue and `call_tool` routes
        through it, which is what makes the row the one place a tool is
        described.  What differs from a declaration is *which* catalogue:
        a plugin is the machine's, so its listing goes into every home's file
        (`Catalogue.remember`), and `Catalogue` is the one that holds it for the
        homes whose file does not exist yet.

        Both failures here end the same way for this source and differently for
        the system.  A *catalogue* that did not work is a broken system too, and
        it is neither filed against this source nor raised: `Catalogue.merge` has
        recorded it, the next `list_tools` fails the whole list with the real
        reason, and `servers` reports it — see the `except` below for why raising
        here would be the same as saying nothing.  A db that answered and
        *refused* — a name another source owns, say — is this source's list that
        cannot be recorded, so it is this source that is unusable until it is
        fixed.
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
        #: every tool an entry under `mcp:` offers.
        self._declares = self.required and any(
            getattr(tool, "name", "") == LIST_SOURCES for tool in listed
        )
        offered = self._offered(listed)
        try:
            await self._catalogue.remember(
                self.settings.name,
                self.settings.kind,
                [_row_of(tool) for tool in offered],
            )
        except Exception as exc:  # noqa: BLE001 - split by _refused, below
            await self._connection.disconnect()
            if not _refused(exc):
                # The catalogue itself did not work, which is not this source's
                # problem and must not be filed as one.  It is **not re-raised**,
                # and that is not a swallowed failure: this runs inside the
                # connect attempt `Connection.connecting` started, which nothing
                # awaits (`settle` uses `asyncio.wait`, which does not re-raise a
                # child), so an exception raised here is retrieved by nobody and
                # arrives later as asyncio's "Task exception was never
                # retrieved" — naming no server and no reason.  The reason is the
                # one thing worth keeping, and `Catalogue.merge` has kept it.
                return
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
        (`slife2.libs.plugin.audience`).  A plugin's tools belong to that plugin's own code
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
        """Run one of this server's tools.  See `slife2.clients.gateway.Connection`."""
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
        held = await self.fetch()
        if held is None:
            return []
        return await self.record(held)

    async def fetch(self) -> list[Any] | None:
        """The round trip alone: what this plugin says it holds.

        **Split from `record` so a refresh can ask every plugin at once and
        merge one answer at a time.**  Each ask is a round trip and they run
        together; each merge writes the catalogue and stays sequential, in
        plugin order, because that order is what settles two plugins claiming
        one source.  Asking in sequence was the whole cost of a build: with
        enough plugins slow to answer, the sum outlived the launcher's
        catalogue budget and took the stack down with it.

        **The one call in this process that no caller bounds.**  Every other
        `call` here runs inside a turn, where the loop holds the model's own
        `_timeout`; this one runs from `refresh_declared`, which `start()` and
        the first `list_tools` reach before any turn exists — and
        `gateway.make_client` deliberately sets no client-wide deadline, so
        without this a plugin that accepted the connection and then never
        answered would hold the hub's first listing open for good.  The bound is
        the connect budget, which is what this await has always effectively had.

        Returns:
            The raw `sources` list, or None when there is nothing to ask.
        """
        if not self._connection.usable or not self._declares:
            return None
        try:
            async with asyncio.timeout(_T.timeouts.ready.connect_tools):
                text, ok = await self.call(LIST_SOURCES, {})
        except TimeoutError:
            raise ConnectionError(
                f"{self.settings.name} took too long to say what it holds "
                f"({_T.timeouts.ready.connect_tools:.0f}s); it is running but not "
                f"answering `{LIST_SOURCES}`"
            ) from None
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
        return held

    async def record(self, held: list[Any]) -> list[DeclaredSource]:
        """Merge one fetched answer and report what it leaves in the catalogue.

        The writing half of `declare`, run one plugin's answer at a time:
        `_record` merges into the catalogue, and a second merge of one source is
        what the refresh lock and this order exist to keep apart.
        """
        declared = [one for raw in held if (one := await self._record(raw)) is not None]
        # **A source it used to hold and does not any more.**  The entry was
        # taken out of the section, so nothing will declare it again and its rows
        # go with it — deleted, not marked.  `error` is the verdict for a source
        # that is *there* and cannot be reached; this one is not there at all, and
        # a row left behind would be a search hit saying "not answering" about a
        # server nobody is asking.  The other moment an entry can be removed is
        # while slife2 is not running, which is the db's boot pass
        # (`ToolStore.reset`); the two agree, so a removal does not depend on
        # whether a process happened to be up when it was made.
        #
        # **Against *this* home's last answer**, which is the home the ask was
        # for and the home these rows are in (`_declared`): comparing against a
        # set shared with the process would compute the difference across two
        # sections, and the row it deletes would be one of this home's.
        here = str(home_dir())
        last = self._declared.get(here, set())
        for source in sorted(last - {one.name for one in declared}):
            gone = await self._catalogue.forget_source(source)
            logger.info(
                "%s: %r is no longer in its section — %d tool(s) removed",
                self.settings.name,
                source,
                len(gone),
            )
        self._declared[here] = {one.name for one in declared}
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

        **A mark lands on rows, so a source with none has nothing to mark.**
        That is not a hole in this rule but a limit of it: an entry switched off
        before it ever answered has no rows, so there is nothing for the
        catalogue to say — `servers()` is what reports it (`off`), and the
        `state` written here is what tells a *previously working* server's rows
        they are off rather than broken.
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
                await self._catalogue.merge(name, category, rows)
            except Exception as exc:  # noqa: BLE001 - split by _refused, below
                if not _refused(exc):
                    # The catalogue itself did not work, and it has recorded the
                    # reason (`Catalogue.merge`).  The raise stays here where it
                    # does not in `_listed`, because this one is awaited by the
                    # caller that asked: aborting the declaration is what keeps a
                    # source whose rows failed to merge from being read as one
                    # the plugin has stopped holding — which would delete its
                    # rows (`declare`, at the end of its own loop).
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

    #: Arguments in, and the answer out — an object `call_tool` says as JSON for
    #: the model (`slife2.libs.turn.tools.as_tool_text`), or text for an answer
    #: that is already words.  **A refusal is raised** (`ToolError`), which is the
    #: contract every tool in this system keeps, and which is why there is no
    #: second half to this answer: a call that did nothing is not an answer at
    #: all, and the one that does something *partly* says so in its own fields.
    tool: UpstreamTool
    run: Callable[[dict[str, Any]], Awaitable[Any]]


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
#: answer shape (a list, with no ranking), and DESIGN.md §10 of DESIGN says so.
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
#: words-as-a-sentence routing in `ToolStore.search` sound.  The two inputs are one call apart,
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
            # rather than advice about how to use it: what the words rank, which
            # words are worth giving, and what empty means.  The warning this
            # used to carry ("guessing at words makes it find nothing") was true
            # while a keywords-only call was answered by the keyword leg alone;
            # with the routing in `ToolStore.search` a guess costs nothing, so
            # the sentence had become false and would only talk a model out of
            # the words it was sure of.  **And the words are not a filter**: a
            # keywords-only call reads them through both legs, so the result may
            # not carry them — saying "must contain" here promised the opposite
            # of what the search does.
            "description": (
                "Words a tool's own text is likely to carry — 'screenshot', "
                "'pdf'. They rank exact matches first but are not a filter: with "
                "no sentence they are also read by meaning. Empty when you have "
                "none."
            ),
        },
        "sentences": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "What you want to do, in your own words, one phrase per idea — "
                "'take a screenshot of a web page'. Matched by meaning. Empty "
                "when you have none."
            ),
        },
        "unloaded_only": {
            "type": "boolean",
            "description": (
                "Only tools not in your list now — a tool you already hold is "
                "not shown. Skills and CLI entries have no load state and are "
                "included. Omit for the whole catalogue."
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
    "Find a tool by what it does, across the whole catalogue — including MCP, "
    "REST API, CLI and SKILL tools. By default it includes tools already in "
    "your list; `unloaded_only` narrows it to the rest."
)

FUNC_TOOL_LOAD_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The tool names, as `tool_search` reports them: bare for "
                "slife2's own tools, '{server}__{tool}' for a server's. One or "
                "several."
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
    "that list. The least recently used go when the list passes its cap."
)

FUNC_TOOL_UNLOAD_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The tools to take out of your list, as `tool_search` reports "
                "them. Empty hands the trim to the harness, which calls it that "
                "way at a turn boundary."
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
    "empty and the harness's trim runs at a turn boundary. Either way the "
    "answer names what went."
)


def local_tools(
    catalogue: Catalogue,
    live_sources: Callable[[], list[str]],
    refresh_declared: Callable[[], Awaitable[None]],
) -> list[LocalTool]:
    """What this process serves a model itself: the three it manages tools with.

    **These are the set-level tools, and that is why they are here.**  Which
    tools exist, which of them the model is holding, and what the budget takes
    back are all questions about the *whole* catalogue — so the process that
    owns the set answers them, and no plugin can.  Everything else a model can
    *call* is served by a plugin: bare-named when it is that plugin's own tool
    (`turn_read`), `{server}__{tool}` when it is an entry under `mcp:`.  What the hub
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

    async def search(arguments: dict[str, Any]) -> dict[str, Any]:
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
            raise ToolError(
                "tool_search needs something to look for: `sentences` is what you "
                "want to do ('take a screenshot of a page') and `keywords` are "
                "words a tool's own text would carry ('screenshot'). Give one of "
                "them — both empty is not a search."
            )
        # **A keywords-only call reaches both legs, and the rule is the store's.**
        # `ToolStore.search` turns words with no sentence into a sentence of them
        # — the measurement and the reason are there.  What is here is only the
        # fact that this tool has nothing to add: the same call is both legs
        # whether the model gave it one list or two.
        found = await catalogue.search(
            keywords=keywords,
            sentences=sentences,
            limit=SEARCH_LIMIT,
            # Booleans from a model are JSON booleans; `is True` is what keeps a
            # string "false" from reading as a filter that hides everything it
            # was asked to show.
            unloaded_only=arguments.get("unloaded_only") is True,
        )
        return _results(found)

    async def load(arguments: dict[str, Any]) -> dict[str, Any]:
        names = _names_of(arguments)
        if not names:
            raise ToolError("func_tool_load needs at least one tool name")
        if len(names) > MAX_LOAD_NAMES:
            # A bound, because each name is its own catalogue write and its own
            # embedding call — this is a `for` over a hop, not a batch — and
            # because a list longer than the budget holds is a list the next
            # trim takes back anyway.  Refused as a whole rather than truncated:
            # a model told *which* half was loaded has to diff two lists, and
            # one told to ask for fewer knows exactly what to do.
            raise ToolError(
                f"func_tool_load takes at most {MAX_LOAD_NAMES} names at a time "
                f"({len(names)} given): the list is trimmed to the budget at "
                f"every turn boundary, so load the ones you want now."
            )
        loaded: list[str] = []
        failed: list[dict[str, str]] = []
        for name in names:
            answer = await catalogue.set_load(name, "loaded")
            reason = _load_refusal(name, answer)
            if reason:
                failed.append({"name": name, "reason": reason})
            else:
                loaded.append(name)
        # **A name that cannot be loaded is a verdict, not a failure.**  The
        # call did what a load does: it asked the catalogue about every name and
        # got an answer for each.  Marking the call failed would make *that*
        # answer an error, and the four `failed` reasons are exactly what the
        # model has to read to correct itself — a server that is switched off, a
        # server that is not answering, a playbook that is read with `skill_use`,
        # a name nothing holds.  What *is* a failure is a call that cannot be
        # made at all: no names, or too many to be one call.
        return {"loaded": loaded, "failed": failed}

    async def unload(arguments: dict[str, Any]) -> dict[str, Any]:
        """Take tools out of the model's list, by name or by budget.

        Both callers of `_func_tool_unload` arrive here — the model with the
        names it is done with, and the harness with none — because a tool has
        one body however many ways it is called.  What the *harness* uses
        instead is the hub's own API tool of the same name: the trim must not
        depend on this list, this catalogue or this routing being in any
        particular state, and `_func_tool_unload` is where that is argued.
        """
        found = await unload_tools(catalogue, live_sources(), _names_of(arguments))
        # **Every one of the five lists is a verdict about a name, and all five
        # are the answer.**  Nothing here is a failure: the model asked about its
        # own list and this is what is true of it — `tool_search` cannot go
        # because the system works by it, `nowhere` because no row has that name,
        # and a name already out of the list is the state the model asked for.
        # The one caller that must not read it as a judgement is the harness,
        # which asks with no names and reads `unloaded` (`trim_answer`).
        return _unload_answer(found)

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
        `ALWAYS_LOADED`), `no_load_state` (a playbook or an installed command —
        a row with no load state to take away), `unknown` (no row has that
        name), and `not_loaded` (named but already out of the list).

        The three that are not `unloaded` are apart on purpose.  Three of them
        are facts about a row's *state* and one about the name itself, and each
        reads as a different sentence: a caller told "refused, the system needs
        them" about a name that does not exist has been told something false,
        and the same sentence about `cli:yt-dlp` claims the system protects a
        tool it never had.
    """
    if not names:
        return {
            "unloaded": await catalogue.evict(sources),
            "refused": [],
            "no_load_state": [],
            "unknown": [],
            "not_loaded": [],
        }
    unloaded: list[str] = []
    refused: list[str] = []
    no_load_state: list[str] = []
    unknown: list[str] = []
    not_loaded: list[str] = []
    for name in names:
        if name in ALWAYS_LOADED:
            refused.append(name)
            continue
        outcome = str((await catalogue.set_load(name, "unloaded")).get("outcome") or "")
        if outcome == "unknown":
            # A name nothing holds.  Not a refusal, and saying so as one would
            # be the opposite of true: "refused, the system needs them" tells a
            # caller that a tool it invented is a tool this process protects.
            unknown.append(name)
        elif outcome == "no_load_state":
            # The two document families, which are findable and not callable:
            # they were never in the list, so nothing was taken out of it.  The
            # sibling `_load_as_text` splits this outcome out for the same
            # reason, and the fallback below used to answer for both of them.
            no_load_state.append(name)
        elif outcome == "already":
            not_loaded.append(name)
        else:
            # **There is no fifth outcome to fall through to, and that is a
            # property of the store rather than a hope**: asking for `unloaded`
            # can only answer `unknown`, `no_load_state`, `already` or the move
            # itself, because `_load_outcome` reaches `disabled`/`error` for a
            # *load* alone (`slife2.libs.store.catalogue._load_outcome`).  So this arm is the
            # `unloaded` one, and the `else` that used to sit here — sending
            # whatever was left to `refused` — described an outcome nothing can
            # produce.  It is how a reader came to believe a fifth existed, and
            # that a *row* could be "refused (the system needs them)"; `refused`
            # is only ever the names above it.
            unloaded.append(name)
    return {
        "unloaded": unloaded,
        "refused": refused,
        "no_load_state": no_load_state,
        "unknown": unknown,
        "not_loaded": not_loaded,
    }


def _unload_names(found: Mapping[str, Any], key: str) -> list[str]:
    """One name list out of a trim's answer, refusing an answer that is not one.

    **A trim whose answer cannot be read is not a trim that took nothing out of
    the list.**  The five lists are the whole of the answer, and `unloaded` is
    the one a caller acts on: an unreadable `found` used to be rendered as the
    same "nothing to unload" a real no-op produces, which is a verdict rather
    than a failure (DESIGN.md §11.1, "a failure is not a verdict").  Raising is what the
    two callers already handle — `call_tool`'s local branch answers `ok` false,
    and the harness's trim leaves the list alone and logs why.
    """
    names = found.get(key)
    if not isinstance(names, list):
        raise ConnectionError(
            f"the trim's answer could not be read — {key!r} came back as {names!r}"
        )
    return [str(name) for name in names]


def _unload_answer(found: Mapping[str, Any]) -> dict[str, Any]:
    """What a trim did, as the object both of its readers read.

    **Five lists, one word each, and every name in exactly one of them.**  The
    sentence this replaced said the same thing in prose — "3 tool(s) unloaded: a,
    b, c; refused (the system needs them): tool_search" — and a sentence is what
    the *model* then had to take apart to find out which of its tools it still
    had.  The names are the whole point of the answer (`unload_tools`), and the
    list a name is in is the verdict about it.

    Every list is read through `_unload_names`, so an answer this process cannot
    read fails rather than reading as "nothing to unload" — a model that lost
    tools and reads that stops looking (DESIGN.md §11.1, "a failure is not a
    verdict").
    """
    return {
        "unloaded": _unload_names(found, "unloaded"),
        "refused": _unload_names(found, "refused"),
        "no_load_state": _unload_names(found, "no_load_state"),
        "unknown": _unload_names(found, "unknown"),
        "not_loaded": _unload_names(found, "not_loaded"),
    }


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


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    """One search row, with the same fields every time.

    **The same fields, because the reader is a model**: a list whose entries
    cannot be compared is a list it has to guess at, and the one job this answer
    has is letting it choose between tools.  `meaning` is how close the row is by
    meaning — the same measurement for every row, including the ones the words
    found and the meaning leg never returned, which used to carry no number at all
    (`ToolStore.search` fills it in) — and it is what orders the page: words
    first, then meaning, each descending.  So the number a reader sees is the
    number that ordered what it sees, which is the one property this answer has to
    keep — a number that does not order the page is a number a model will sort by
    and be wrong about.

    `matched` is which leg found it, and it is the half of the answer that is
    certain rather than graded: `words` is the caller's own words **in this
    row's text**, `returns` is the same evidence in the tool's `Returns:`
    prose, and `body`/`parameters` is the rest behind the line — a playbook's
    whole text, or the parameters of a tool.  Naming which is what makes the
    mark checkable instead of mysterious: a reader sent to the parameters for a
    word that is in the answer's description looks in the wrong place.

    `status` is left as the catalogue's own word rather than filtered to unusable
    rows: `enabled` is an answer, and "its server is switched off" is exactly what
    v1 kept the rows of a failing server to be able to say.

    `description` is **never cut**.  It is the one thing a model chooses by, and
    `mcp-pandoc`'s is ninety lines against a catalogue median of one — a search
    answer that outgrows `tool_result.tokens` because of it is the harness's
    problem (`slife2.plugins.llm`'s dump), and a truncated description would hide
    the problem rather than answer it.
    """
    matched = row.get("matched_in")
    if matched == "line":
        found_by = "words"
    elif matched == "returns":
        found_by = "returns"
    elif matched == "rest":
        found_by = "body" if str(row.get("category")) == SKILL else "parameters"
    else:
        found_by = ""
    similarity = row.get("similarity")
    return {
        "name": row.get("name"),
        "category": row.get("category"),
        "source": row.get("source_id"),
        "status": row.get("status"),
        "loaded": row.get("load_status") == "loaded",
        "matched": found_by,
        "meaning": similarity if isinstance(similarity, float) else None,
        "description": str(row.get("description") or ""),
    }


def _confidence(rows: Sequence[Mapping[str, Any]], best: float) -> str:
    """How sure this page is, as one of three words.

    **A judgement the model cannot make for itself**, which is why it is here and
    not left to the numbers: the floors are this system's (`MATCH_FLOOR`,
    `WEAK_FLOOR`), and a page of the ten nearest rows says nothing about whether
    any of them is for the thing asked — the same shape came back for a query
    this catalogue can answer and for one it cannot, and the caller was left to
    guess which it had.

    `sure` is a row above the match floor, `weak` is one above the weak floor (or
    a page the caller's own words reached at all — that evidence is certain where
    a cosine is graded), and `none` is neither.  **And never a count of matches**,
    because there is no such number to give: the floors label a page and do not
    cut it — a weak answer still shows its ten rows — so a count would be the
    page size wearing the word "found".
    """
    if best >= MATCH_FLOOR:
        return "sure"
    if best >= WEAK_FLOOR or any(row.get("matched_in") for row in rows):
        return "weak"
    return "none"


def _results(found: Mapping[str, Any]) -> dict[str, Any]:
    """A tool search's rows, as the object a model reads.

    `results` is in the order they are meant to be read — the caller's own words
    first, then by meaning — and `best` and `confidence` are the verdict about
    the page as a whole (`_confidence`).  No pointer at `func_tool_load` follows
    them any more: it is in every model's list from the first turn, its own
    description says what it does, and a tool's answer that instructs a model
    about another tool is the cross-reference this surface is trying not to
    carry.

    **A search answer that cannot be read is not a search that found nothing.**
    An empty `results` is a verdict a model acts on rather than a failure it
    retries, so only a *readable* empty list may say it; an answer with no
    `results` list at all is damage, and it raises (DESIGN.md §11.1, "a failure is not a
    verdict").  `call_tool` turns the raise into `ok` false, so the model reads
    the failure instead of the confident negative.
    """
    rows = found.get("results")
    if not isinstance(rows, list):
        raise ConnectionError(
            f"tool_search could not read the catalogue's answer — `results` came "
            f"back as {rows!r}"
        )
    scored = [
        float(row["similarity"])
        for row in rows
        if isinstance(row.get("similarity"), float)
    ]
    best = max(scored) if scored else 0.0
    return {
        "results": [_record(row) for row in rows],
        "best": best,
        "confidence": _confidence(rows, best),
    }


def document_is_not_callable(name: str, row: Mapping[str, Any]) -> str:
    """Why one of the two document families cannot be called, and what to do.

    **A search result is an invitation to call the name in it**, so the two
    families that are findable and not callable have to answer like grown-ups:
    the model read the name, tried it, and is owed the step that actually
    reaches the thing — `skill_use` for a playbook, and for a command the truth
    that nothing runs one yet (DESIGN.md §10).
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


def _load_refusal(name: str, answer: Mapping[str, Any]) -> str:
    """Why one load did not end up in the model's list, or `""` when it did.

    **One sentence per outcome, and the outcome is a fact the db gave us** —
    `ok` used to be the second half of this function's answer, which is what made
    a name that was *already* loaded a success and a name that could not be loaded
    a failure.  Here the two are the same kind of thing: a name that landed says
    nothing, and a name that did not says why.

    The three refusals that are not a fault say so, because the model acts on
    them differently: a switched-off entry is the operator's decision, a server
    that is not answering is one to try later (`servers` says which), and the two
    document families are findable and not loadable — `skill_use` reads a
    playbook, and nothing runs an installed command yet (DESIGN.md §10).
    """
    outcome = str(answer.get("outcome") or "")
    if outcome in ("loaded", "already"):
        return ""
    if outcome == "unknown":
        return (
            f"unknown tool {name!r} — find it with tool_search, which reports "
            f"names exactly as they are called"
        )
    if outcome == "no_load_state":
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
        return f"{name!r} has no load state: {what}"
    if outcome == "disabled":
        return (
            f"{name!r} is switched off in the config, so it cannot be loaded — "
            f"its server has `enabled: false`, which is the operator's decision "
            f"and not a fault"
        )
    if outcome == "error":
        return (
            f"{name!r} cannot be loaded: the server that owns it is not "
            f"answering. It is in the catalogue, and `servers` says why"
        )
    return f"{name!r} could not be loaded ({outcome or 'no answer'})"


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
        returns=str((getattr(tool, "meta", None) or {}).get(RETURNS) or ""),
    )


def _row_of(tool: UpstreamTool) -> dict[str, Any]:
    """One advertised tool, as the catalogue stores it.

    The description is stored **raw** and the `[server]` prefix is put on at
    advertisement time (`_from_row`), because the row is also what a search
    reads and indexes: a prefix repeated in every result is noise in the text a
    model searches by, and the row already says which source it came from.

    An empty `schema` means "nothing to declare" and the store turns it into its
    own sentinel — the hub has no opinion about how a store spells absence.

    **`returns` is the tool's `Returns:` prose**, which the plugin put on
    `_meta` (`slife2.libs.plugin.protocol.DeclareReturns`).  Empty is the same
    absence as an empty schema: most external servers say nothing, and that is a
    fact about them rather than a reason not to store what the ones that do say.
    """
    return {
        "name": tool.name,
        "description": tool.description,
        "remote_name": tool.tool,
        "schema": json.dumps(tool.parameters) if tool.parameters else "",
        "returns": tool.returns,
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
    two ends both doing it is how the model came to read the same label twice.
    """
    server = str(row.get("source_id") or "")
    description = str(row.get("description") or "")
    return UpstreamTool(
        name=str(row.get("name") or ""),
        server=server,
        tool=str(row.get("remote_name") or row.get("name") or ""),
        description=f"[{server}] {description}".strip() if server else description,
        parameters=_parameters(str(row.get("schema") or "")),
        returns=str(row.get("returns") or ""),
    )


def _parameters(schema: str) -> dict[str, Any]:
    """A stored schema, as the parameter mapping the model's tool list takes.

    Tolerant on purpose, and the tolerance is a decision rather than an accident:
    the column holds a JSON document *or* a sentinel a store chose for "nothing
    to declare", and this process deliberately has no opinion about how absence
    is spelled (`_row_of`).  So a value that will not parse becomes "no
    arguments" rather than a reason to fail the whole list.

    **What it costs is registered, not argued away.**  A row whose *real* schema
    was damaged is handed to the model as a tool with no arguments, so the model
    calls it with none and reads the tool's own error several frames from the
    cause — the weaker path standing in for a value nobody could read (DESIGN.md §11.1).
    Failing the row instead would need this half to tell the sentinel from the
    damage, which is the opinion it must not have; SDD Appendix C carries it
    for that reason.
    """
    try:
        found = json.loads(schema)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def plugin_settings(config: Config, name: str) -> ToolServerSettings:
    """One of our own servers, as one upstream of the hub.

    **Written here rather than under `mcp:` because it is ours.**  An entry in
    `mcp:` is somebody else's process, which slife2 may fail to reach without
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


def configured_sources(config: Config) -> frozenset[str]:
    """Every source name the config carries, which the boot pass is handed.

    `ToolStore.reset` runs on the way up, before anything is asked over any
    wire, and it has one job: withdraw the verdicts on rows whose source the
    config no longer names — because those are the rows nothing will ever speak
    for again.  The other direction is the one this has to get right.  A source
    the config *does* still name is one this process is about to ask about, and
    whether it answers is the runtime's to say; marking it `error` on the way up
    is a guess, and the guess is visible while it stands.

    **And it is more than the plugins.**  A plugin is the only source name this
    process *connects* under, which is what the set used to be — but the
    entries under `mcp:` and `rest-api:` are named in the config too, and so
    are the two document sources, and none of them has answered yet at the
    moment `reset` runs.  Handed the plugins alone, the boot pass read every one
    of them as a source that has gone: a restart marked every external tool
    `error`, and the declaring plugin wrote them all back — a window in which a
    search called those tools unusable and `func_tool_load` refused them, and a
    rewrite of every row to end it.  Measured on the real catalogue, that is 239
    rows rewritten per restart, logged as "239 changed" about a config nobody
    edited.

    The two document sources are here as constants rather than read off their
    servers, because a name crossing a process boundary is spelled on both sides
    (`SKILLS_SOURCE`).  Which means a *disabled* source is in this set too, and
    must be: `enabled: false` is a switch, not a deletion, and its rows are the
    ones the catalogue keeps on purpose.
    """
    return frozenset(
        {
            *config.plugins(),
            *config.mcp,
            *config.rest_apis,
            SKILLS_SOURCE,
            CLI_SOURCE,
        }
    )


def build_server(
    config: Config,
    *,
    source: Callable[[], Config] | None = None,
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

    `source` is what makes this hub serve *the caller's* home rather than the one
    it was started in: `load_cached` in production, nothing at all when a test
    hands a config in (`slife2.libs.host.config.for_the_call`).  Everything below
    that reads the config at call time goes through `current()` — the catalogue's
    budget and its boot pass, the embedder, and the declarations — while what the
    *process* is built from stays machine-level on purpose: `upstreams` are the
    plugins slife2 starts, one set of ports for every home, which is the whole
    reason a second home costs no second stack.

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

    def current() -> Config:
        """The config the call in front of us is served from.

        Not a parameter and not read once, for `slife2.libs.host.config.for_the_call`'s
        reason: a hub answers every conversation in the system, and the budget,
        the source names and the embedder behind the file it opens are the
        *caller's* home's.
        """
        return for_the_call(source, config)

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

        **`known` is the whole config, and it is what the file's own boot pass
        runs on**: opening a store withdraws the verdicts on rows whose source
        the config no longer names, and every other source has to be named here
        or it is read as one that has gone — see `configured_sources`, which is
        where that argument is.

        **`autoload` is `tool_load.autoload`**, and it is read here because this
        is where one home's file is opened: the names start loaded, the budget
        may not take them, and both halves read the `ToolStore` that holds them.
        The boot pass on the same store applies the list to rows that already
        exist, which is what makes an edited list take effect at the next start.

        `transports` is still the seam at the level below — every *upstream* is
        built from a wired entry in a test — and the catalogue is deliberately
        no longer one of them: a file is not a plugin, and a test that wants it
        elsewhere sets the data directory, which is what `conftest.py` does for
        every test in this suite.
        """
        theirs = current()
        store = await asyncio.to_thread(
            ToolStore,
            catalogue_db(),
            threshold=theirs.tool_load.threshold,
            autoload=theirs.tool_load.autoload,
            known=configured_sources(theirs),
        )
        # **Everything the machine already holds goes in before this home is
        # asked anything**, and it is the difference between a file with this
        # home's declarations in it and a *catalogue*: without it the home opens
        # with the `mcp:` entries of its own section and none of the tools the
        # plugins offer — the hub's own four among them, which are what finds and
        # loads all the others.  See `Catalogue.remember`.
        #
        # The key is this process's ambient home, which is the one `store()`
        # asked for: a store is only ever opened from inside the call that wanted
        # it.  The sync below may therefore use the ambient embedder, and the
        # fan-out inside a `remember` may not — there it is another home's file.
        await catalogue.seed(str(home_dir()), store)
        status = await store.sync_indexes(await embedder_of_call())
        logger.info("indexed the tool catalogue at %s (%s)", store.path, status)
        return store

    #: The embedder, opened on first use: the catalogue's rows are placed in a
    #: vector index by the same model the turns are, through the same hop.
    #:
    #: **One per home**, because the width and the model come from the
    #: embeddings plugin's `describe` — which answers for the home the call came
    #: from — while the endpoint comes from `servers:` and is the machine's.  One
    #: connection for the process would fix the first home's model on every other
    #: (`EmbedderConnection` keeps what it opened), and an index built at one
    #: width cannot be searched at another.
    embeddings: dict[str, EmbedderConnection] = {}

    def embedding_for() -> EmbedderConnection:
        found = embeddings.get(str(home_dir()))
        if found is None:
            found = EmbedderConnection(current(), embedder=embedder)
            embeddings[str(home_dir())] = found
        return found

    async def embedder_of_call() -> Embedder:
        """The embedder of the home this call is for, opened on first use."""
        return await embedding_for().get()

    #: The catalogue, and the one file it opens when something first asks.
    catalogue = Catalogue(open_catalogue, embedder_of_call)

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

    #: Every source a plugin declared, by home, and which plugin holds it — the
    #: hub's address book for calls it cannot make itself.  **Rebuilt from the
    #: declarations on every refresh**, because a source is not a connection this
    #: process keeps and there is nowhere else the fact could live.
    #:
    #: **Per home**, and that is the fact the whole hub turns on: what a plugin
    #: holds is its section of the *caller's* `slife2.yaml` — `mcp:` entries,
    #: playbooks, commands, and the server a home's model may call — so one table
    #: for the process would answer one home with another's address book, and
    #: route its calls to servers configured somewhere else.  The hub's own
    #: connections (`upstreams`) are not in here and do not move: those are the
    #: plugins slife2 starts, one set of ports for every home.
    #:
    #: `Declarations.lock` is per home for the reason the table is: the race it
    #: serialises is two overlapping merges of one source *into one catalogue*,
    #: and each home has its own file (`Catalogue.store`).
    declared: dict[str, Declarations] = {}

    def declarations() -> Declarations:
        """This home's address book, made on the first ask."""
        key = str(home_dir())
        found = declared.get(key)
        if found is None:
            found = Declarations()
            declared[key] = found
        return found

    #: **One refresh at a time, per home** (`Declarations.lock`), and this
    #: process is the one that has to say so: the hub answers every conversation
    #: in the system, so two asks arriving together — two turns, or a turn and a
    #: search — would otherwise declare the same plugin twice at once.  Two
    #: things break when that happens, and the second is why a lock and not a
    #: repair: `ToolStore.merge` *plans* its writes and applies them in a second
    #: pass with an embed in between (`slife2.libs.store.db`), so two overlapping merges
    #: of one source are a check-then-act race on the tool table's primary key —
    #: and the two swaps of the tables can interleave, leaving a source held with
    #: the route to another plugin's connection.  It is a small critical section:
    #: `refresh_declared`'s body is plugin round trips and catalogues, and nothing
    #: a reader of `live_sources` can see (the swap at the end has no await in
    #: it).  Two homes refreshing at once is not a race — each writes its own
    #: catalogue — so they do not wait for each other.

    def by_source(name: str) -> Upstream | None:
        """Who to ask for one source: the connection we hold, or the plugin.

        A direct upstream first, because those are the names this process is
        configured with; a declared source name may not collide with one, so the
        two can never disagree about who owns a name.
        """
        for one in upstreams:
            if one.settings.name == name:
                return one
        return declarations().holders.get(name)

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
            + [one.name for one in declarations().held.values() if one.up]
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

        **And that rule is enforced, not assumed.**  `Upstream.declare` has a
        bound of its own (`ready.connect_tools`) and raises when a plugin that is
        up does not answer within it — a real state in a cold container, where
        `mcp-tools` is still installing the external servers it holds.  The
        raise is caught per upstream below: the source keeps its last rows, the
        warning names it, and the refresh goes on.  Letting it escape was how a
        slow external source failed the whole catalogue and, through
        `_open_catalogue`, the launcher itself.

        **The two names that cannot be declared are checked here**, and only here
        can they be: a source may not take a name this process is already
        connected to, and two plugins may not claim one source between them —
        a merge is the whole truth about a source, so two holders would each
        purge the other's rows on every ask.

        **The sets are built aside and swapped in, not emptied first.**  The
        awaits below are round trips to plugins, one of which waits up to
        `slife2.helpers.timeouts.timeouts.ready.list_settle` for the servers it holds — and
        `live_sources` is read the whole time by whatever else this process is
        serving, since one hub answers every conversation.  A reader that caught
        the function between an emptying and a filling would see no declared
        source at all and hand its model a list missing every external tool; the
        swap has no await in it, so no reader can see a half-built set.

        **And one refresh at a time** (`Declarations.lock`), for the same fact seen
        from the other side: two asks arriving at once would each run this, and
        two overlapping merges of one source are a race on the tool table's
        primary key (`slife2.libs.store.catalogue.ToolStore.merge` plans and applies in two halves).
        An ask that arrives during one waits for it; what it waits for is one
        round trip to a plugin, and what it buys is not merging a source twice.
        """
        async with declarations().lock:
            begin_connecting()
            known = {one.settings.name for one in upstreams} | {CONFIG_KEY}
            found: dict[str, DeclaredSource] = {}
            owners: dict[str, Upstream] = {}
            asked = [one for one in upstreams if one.usable]

            async def fetched(
                upstream: Upstream,
            ) -> tuple[Upstream, list[Any] | None, ConnectionError | None]:
                try:
                    return upstream, await upstream.fetch(), None
                except ConnectionError as exc:
                    return upstream, None, exc

            # **Every ask at once, every merge in turn.**  A round trip is what
            # a build waits for — a plugin that is up but still starting the
            # servers it holds answers late — and an ask does not touch the
            # catalogue, so asking them together costs the slowest one instead
            # of their sum.  The sum was the failure: enough slow plugins
            # outlived the launcher's catalogue budget and took the stack down
            # with them.  The merges below do write rows and stay sequential in
            # plugin order, which is what settles two plugins claiming one
            # source and what the lock through this function exists to keep
            # apart.
            answers = await asyncio.gather(*(fetched(one) for one in asked))
            for upstream, held, failure in answers:
                if failure is not None:
                    # **A source that is slow to say what it holds is not a
                    # broken catalogue.**  The rule this file states for a
                    # plugin that is not up — it contributes nothing and is
                    # asked again next time — is the same rule for one that is
                    # up but still installing the external servers it holds:
                    # its last rows stay in the catalogue, and this refresh
                    # loses freshness rather than failing the stack.  The
                    # required-plugin gate is what still fails a turn, and it
                    # reads `usable`, not this answer.
                    logger.warning(
                        "%s: declarations are not current (%s); keeping the "
                        "rows from the last refresh",
                        upstream.settings.name,
                        failure,
                    )
                    continue
                if held is None:
                    continue
                declared = await upstream.record(held)
                for one in declared:
                    if one.name in known:
                        logger.warning(
                            "%s: %r is a name this hub is already connected under",
                            upstream.settings.name,
                            one.name,
                        )
                        continue
                    if one.name in owners:
                        logger.warning(
                            "%s: %r is already declared by %s",
                            upstream.settings.name,
                            one.name,
                            owners[one.name].settings.name,
                        )
                        continue
                    known.add(one.name)
                    found[one.name] = one
                    owners[one.name] = upstream
            mine = declarations()
            mine.held.clear()
            mine.held.update(found)
            mine.holders.clear()
            mine.holders.update(owners)

    #: The tools this process serves itself.  Built once — a local tool's
    #: *schema* is a constant, and only the data its body reads can change,
    #: which it re-reads on every call.  `live_sources` is handed in because the
    #: trim asks the catalogue for what the model is *holding*, and only this
    #: process knows which sources are answering; `refresh_declared` because a
    #: search is where a plugin's declarations have to be current.  Which tools
    #: the budget may not take is **not** handed in: it is
    #: `tool_load.autoload`, read where the catalogue is opened, so the trim and
    #: the seed read the same set without a second copy to keep in step.
    local: list[LocalTool] = local_tools(
        catalogue,
        live_sources,
        refresh_declared,
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

        **An answer with no tool list is a failure, not an empty list.**  A
        catalogue whose shape this process does not know used to be served as no
        tools at all, which is the one answer the model cannot tell from a hub
        that has nothing — and DESIGN.md §4.2's rule is the opposite in as many
        words: a hub that cannot read a plugin's tool list refuses to list
        anything.  The same rule reaches a shape change here (DESIGN.md §11.1, "a value
        that cannot be read is not a value that is absent").
        """
        rows = found.get("tools")
        if not isinstance(rows, list):
            raise ConnectionError(
                f"the tool catalogue answered without a tool list — `tools` "
                f"came back as {rows!r}"
            )
        return [_from_row(row) for row in rows]

    def begin_connecting() -> None:
        for upstream in upstreams:
            upstream.connecting()

    async def settle() -> None:
        """Let the attempts this process started finish, briefly.

        `slife2.libs.plugin.family.settle_attempts` is the wait and the rule behind it
        ("wait, never cancel") — this half is only *which* attempts: the links to
        the plugins, where the family's is the entries of one config section.

        Measured against the real `arxiv` endpoint, the first connect takes
        longer than this window: the first call now answers `connecting` and
        leaves the attempt alone, and the second finds it ready.
        """
        await settle_attempts(upstream.attempt for upstream in upstreams)

    async def refresh() -> None:
        """Bring the links and the declarations up to date, briefly.

        **The three steps are one thing, and it is the ask's.**  Everything the
        hub answers about the tool set is read off the catalogue and off
        `live_sources`, and both are only as fresh as the last time the links were
        asked: a plugin that has just come up is a source with no rows yet, and a
        plugin that has just gone down is one whose rows the gate would still hand
        over.  So an ask starts the attempts that are not running
        (`begin_connecting`), gives the ones that are a moment (`settle`), and
        re-reads what every plugin *holds* (`refresh_declared`).

        **Three calls and not one because the middle one is the only one that
        waits**, and it is deliberately bounded: a plugin that is still starting
        is left out of this answer rather than waited for, because waiting on it
        properly would hold a turn open for as long as an `npx` takes to install
        itself.  A search asks for the declarations alone — `local_tools`'
        `search` calls `refresh_declared` directly — because a search must not
        wait at all.
        """
        begin_connecting()
        await settle()
        await refresh_declared()

    #: The refresh a non-waiting ask started, while it is still running.  One at
    #: a time: a second ask joins it rather than starting another merge.
    refreshing: asyncio.Task[None] | None = None

    async def _refresh_in_background() -> None:
        try:
            await refresh()
        except Exception:  # noqa: BLE001 - the next ask is the retry
            logger.exception("the tool catalogue refresh failed")

    def refresh_soon() -> None:
        """Start one refresh and return; the ask does not wait for it."""
        nonlocal refreshing
        if refreshing is not None and not refreshing.done():
            return
        refreshing = asyncio.get_running_loop().create_task(_refresh_in_background())

    async def start() -> None:
        """What the hub does before it serves anything.

        **The plugins are asked for their tools, and everything this process
        already knows is written down.**  The upstreams find theirs by
        connecting, this process's own three are known without connecting to
        anything, and what each plugin *holds* is asked for once every plugin
        that answered is in hand.

        **The order is load-bearing twice.**  This process's own rows go first —
        *held* rather than written, because no home has asked yet — and that is
        what makes the first `list_tools` complete: the model's first answer
        would otherwise be missing the tools that find and load tools.  And the
        declarations come last, because they arrive from a plugin, so they cannot
        be asked for until the listing that says which plugins have any is in
        hand: `settle`'s bounded wait is what lets a plugin that is still starting
        be one of them, and one that is not is asked again by the next ask
        (`refresh_declared`).
        """
        await catalogue.remember(
            CONFIG_KEY, PLUGIN, [_row_of(one.tool) for one in local]
        )
        await refresh()

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
            for one in embeddings.values():
                await one.close()

    mcp: FastMCP = house_server(
        SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan
    )

    @mcp.tool
    async def list_tools(wait: bool = True) -> dict[str, Any]:
        """Every tool the agent may offer the model **now**, with its schema.

        **The loaded set, not the whole catalogue.**  A tool the model has not
        loaded is not in this answer, and it is found with `tool_search` and put
        here with `func_tool_load` — which is what keeps the list that goes out
        with every request from growing to the size of everything installed.
        The four tools that find, load and trim are always in it: they are the
        mechanism, and a budget that could take them away would leave the model
        holding a set it cannot change.

        A plugin's tools are left out unless they declare themselves the
        model's (`slife2.libs.plugin.audience`); a tool server's are all offered, because the
        operator put the server in the config.  Both were decided when the
        listing was merged, so nothing in this answer says which is which — by
        the time a tool is listed, the question has been answered.

        Raises:
            ConnectionError: If a plugin — or the catalogue itself — is not
                answering.  Deliberately not a shorter list instead: a plugin
                that is gone is a system that has come apart, and a model that
                has quietly lost `now` and `calc` is a failure nobody can see.  A
                server from the `mcp:` section is the opposite case and is
                simply left out — it is the operator's configuration and somebody
                else's process, and its tools are in the catalogue with `error`
                on them.  See DESIGN.md §5.1 and §4.1.

        Args:
            wait: Whether this answer waits for the sources' declarations.
                **The start-up build does; a turn's list does not.**  The
                declarations refresh talks to every plugin and can outlast a
                turn's `work.hub` when external sources are connecting — making
                the turn's ask wait for it is how a fresh catalogue becomes
                `Request 'tools/call' timed out`.  A late source costs freshness
                and not the turn: the refresh this answer starts runs on, and the
                source is in the next list.  **The connections are waited for
                either way**: the refusal below reads `usable`, and an attempt
                still running — a plugin that has just come up — must not be
                read as a plugin that is gone.

        Returns:
            `tools`: one entry per tool the model may call, with `name` as the
            model will call it, the `server` it came from, the upstream's own
            `tool` name, and the JSON Schema its arguments must match.

            `total`: how many tools the catalogue holds — **every category**,
            so skills and CLI entries are in it, and so are the rows of a source
            that is down or switched off.  The list above is the model's *loaded*
            set; this is the size of the catalogue it was chosen from, and it is
            carried here because the start-up ask that reads the list is the ask
            that has already refreshed the declarations (`refresh`), so a
            display can show a current number without a second question of its
            own.
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
        #
        # **But only the ask that can afford it waits.**  The start-up build's
        # budget is `work.catalogue`; a turn's is `work.hub`, and the refresh's
        # plugin round trips can exceed the latter.  A turn starts the refresh
        # and answers from the catalogue it has.
        #
        # **The declarations are the part it does not wait for, and the links
        # are not.**  The refusal below is an answer about `usable`, so an
        # attempt that is merely running must not be read as a plugin that is
        # gone — and a fresh stack is exactly that state: the launcher starts
        # the hub before `agent-task` and `schedule` (they reach the hub, so it
        # cannot wait for them), so their first asks fail while they start and
        # the next ask is the one that finds them.  `settle` is bounded by
        # `ready.list_settle` and fits a turn; the declarations refresh is what
        # can outlast `work.hub`, and that is what the background task carries.
        if wait:
            await refresh()
        else:
            begin_connecting()
            await settle()
            refresh_soon()

        # **The catalogue's own failure comes first, and it is first because it
        # is what made a plugin look broken.**  A listing the catalogue could not
        # record drops that source's link (`Upstream._listed`), so `missing`
        # below would name the source as one that is "connecting" — about a
        # plugin that answered, and for a reason the operator could never see.
        # The module's third failure rule is the argument: a broken catalogue
        # fails the list rather than being filed as somebody's server.
        if catalogue.failure:
            raise ConnectionError(
                f"the tool catalogue is not working — {catalogue.failure}"
            )

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
        return {
            "tools": [tool.to_wire() for tool in advertised(found)],
            # After the gate, and from the same store: the model's list is one
            # number and the catalogue's size is another, and a caller that
            # wanted only the first reads `tools` and ignores this.
            "total": await catalogue.total(),
        }

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
                answer = await one.run(arguments)
            except ToolError as exc:
                # A refusal is the sentence and nothing else: `tool_search failed:
                # give me something to look for` reads as a broken tool, and what
                # happened is that the tool worked and said no.
                return {"text": str(exc), "ok": False}
            except Exception as exc:  # noqa: BLE001 — a tool failure is a message
                logger.exception("%s failed", name)
                return {"text": f"{name} failed: {exc}", "ok": False}
            return {"text": as_tool_text(answer), "ok": True}

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
            await refresh()
            row = await catalogue.route(name)
        if row is None:
            return {"text": await unknown_tool(name), "ok": False}

        if str(row.get("category") or "") in (SKILL, CLI):
            # A row a search can find and a call cannot reach, which is the
            # shape of both document families.  Said here rather than left to
            # the branch below, whose answer is about a server that is not
            # holding the row any more — false for these, which never had one
            # behind them, and which the model reached by reading a search
            # result that told it so.
            return {"text": document_is_not_callable(name, row), "ok": False}

        source = str(row.get("source_id") or "")
        upstream = by_source(source)
        if upstream is None:
            # **The row is real and nothing is holding it.**  Its owner is not
            # one of our own connections and no plugin has declared it *now*,
            # which has two causes and the caller's own config tells them apart
            # — where the answer here used to be one guess at both.
            #
            # A source the config still names and no plugin declares is a plugin
            # that is not answering: `refresh_declared` skips a plugin that is
            # down whole, so its sources leave this home's address book while
            # their rows stay on purpose (`Upstream.declare`, and
            # `test_a_plugin_that_stops_answering_keeps_the_rows_it_declared`).
            # The other shape is an entry taken out of the section while the
            # plugin that held it was silent: `forget_source` is that plugin's
            # deletion to ask for, and it has not been asked.
            #
            # **Neither sentence may tell the caller the config does not name the
            # source when it does**, which is the one thing the text here got
            # wrong: it answered "not a server this hub is configured with any
            # more — check `mcp:` in the config" for a source whose entry was
            # there all along and whose *plugin* was the thing that was down.
            # That is the shape this is reached in: a switched-off entry stays
            # declared and is refused by its plugin, and a removal whose plugin
            # did answer has already had its rows deleted (`forget_source`).
            if source in configured_sources(current()):
                return {
                    "text": (
                        f"{name!r} belongs to {source!r}, which is still in this "
                        f"home's config — no plugin is declaring it right now, "
                        f"and the plugin that serves its section is the one that "
                        f"would. `servers` says whether it is answering."
                    ),
                    "ok": False,
                }
            return {
                "text": (
                    f"{name!r} belongs to {source!r}, which is no longer in this "
                    f"home's config — its entry was removed, and this row goes "
                    f"the next time the plugin that held it answers."
                ),
                "ok": False,
            }

        remote = str(row.get("remote_name") or name)
        if source in declarations().holders:
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
        and the reason `slife2.plugins.toolhub.server.model_name` does not filter it), and the
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
                `tool_load.threshold` goes, least recently used first, and
                nothing named in `tool_load.autoload` is ever a candidate.
                Naming one of the five this system works by is refused rather
                than obeyed.

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
        )
        # `text` is the same answer said as the model reads it — the harness
        # writes the trim into the conversation as a tool pair, and the pair's
        # result is text on every wire (`slife2.clients.toolclient.trim_answer`).
        return {**found, "text": as_tool_text(_unload_answer(found))}

    @mcp.tool
    async def servers() -> dict[str, Any]:
        """What each configured tool server is doing.

        The answer to "why is my tool missing", which is otherwise a log search:
        a server that failed to start, one that is still starting, and one that
        is connected and simply does not offer what you expected are three
        different problems that look identical from the tool list.

        Returns:
            `catalogue` — `""`, or why the catalogue last failed to record a
            source's tools.  Every row below is read off it, so a catalogue that
            is not working is the reason the counts are stale or a source reads
            as one that is not answering; it has no name to be a row under,
            because it is not a source of tools but the file they live in.

            `servers`: one row per source of tools — `name`, `kind` (`mcp`,
            `rest` or `plugin`), `transport`, `state` (`ready`, `connecting`,
            `failed`, `idle` or `off`), how many `tools` it last offered, how
            many of them are `loaded` (which is what the model has now, so a
            source that is not live is `0` whatever its rows say), the
            `description` it was configured with, the `error` if there is one,
            and `required` — whether slife2 starts it, which is what decides if
            its absence fails a turn or merely shortens the tool list.  A
            source nobody has counted reports `"not counted"` for both counts
            rather than `0`, because `0` would be a number this process does not
            have (`NOT_COUNTED`).  A plugin that offers fewer tools than it has
            is the normal case, not a fault: the rest are its own code's, and
            this count is the one the model sees.
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
        #
        # `live_sources` goes to the counts for the reason the gate reads it:
        # what a source offers is a row, and what the model *holds* is a row this
        # process would hand over — so a server that is down must not be reported
        # as one whose tools are loaded (`slife2.libs.store.catalogue.ToolStore.source_counts`).
        await refresh()

        # **And the catalogue's own health is a field of this answer rather than
        # a row**, because it is not a source: it is the file every row below
        # comes from, so a catalogue that could not record a listing is the
        # reason a source reads as one that is not answering — and it has no name
        # of its own to be a row under.  `""` is the ordinary answer, and this is
        # deliberately not a raise: this is the tool that is supposed to tell the
        # three problems apart, and failing it would leave the operator with
        # nothing to read at all.
        counts = await catalogue.sources(live_sources())
        return {
            "catalogue": catalogue.failure,
            "servers": [
                *(
                    upstream.snapshot(counts.get(upstream.settings.name))
                    for upstream in upstreams
                ),
                # One kind of declared source is not reported, and it is not a
                # gap: one with no transport has nothing to connect to at all —
                # a folder of playbooks is a source of rows, not a server, and
                # this answer is about servers.  A *switched-off* one is here,
                # saying `off`: it is a configured server, and the operator
                # turning it off is the answer to "why is my tool missing" more
                # often than a fault is.
                *(
                    one.snapshot(counts.get(one.name))
                    for one in declarations().held.values()
                    if one.transport
                ),
            ],
        }

    return mcp


def main(argv: list[str] | None = None) -> int:
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        build=lambda config: build_server(config, source=load_cached),
        logger=logger,
        note=lambda config: (
            f"{len([name for name in config.plugins() if name != CONFIG_KEY])} "
            f"plugin(s) to ask"
        ),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
