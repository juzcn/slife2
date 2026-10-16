"""slife2.yaml — one config file, read by every process and by the launcher.

The shape is slife v1's, because it already answers the questions a model
configuration has to answer:

    providers:
      deepseek:
        api: openai-completions          # the wire protocol
        base_url: https://api.deepseek.com
        api_key: ${DEEPSEEK_API_KEY}
        models:
          - model: deepseek-flash        # the API name, and the local id
            name: DeepSeek Flash         # what to call it on screen
            reasoning: true
            input: [text, image]
            context_window: 1000000
            max_tokens: 384000
            temperature: 0.7
            top_p: 1.0

The other section worth reading here is `mcp:` — the *external* MCP servers,
which the plugin that owns the section holds a connection to:

    mcp:
      filesystem:
        command: npx                    # stdio: a process we start
        args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      serper:
        command: npx
        args: ["-y", "serper-search-scrape-mcp-server"]
        env:
          SERPER_API_KEY: ${SERPER_API_KEY}
      arxiv:
        url: https://arxiv.mcp.brunosan.de/mcp    # http: someone else's process
        headers:
          Authorization: Bearer ${ARXIV_TOKEN}

Note the two words kept apart throughout this file: `servers:` is *our* plugins
— the ones slife2 starts, shares and stops — while `mcp:` is other people's,
which `slife2-mcp-tools` holds as a client and declares to the hub.  They are
different things with different failure rules (`slife2.plugins.toolhub.server`), and this is
the only place both are configured.

And one section for a program that is neither, because it is already here:

    cli:
      yt-dlp:
        command: yt-dlp                 # what runs
        description: Download video from 1000+ sites, subtitles and playlists.
        install: uv pip install yt-dlp  # what a person is told when it is absent

An entry names a command on this machine rather than a server to connect to:
there is no process for slife2 to start, no URL, and nothing to keep alive — so
it is not a `mcp:` entry, and nothing about it is a plugin either, because
slife2 did not write it.  What it shares with both is the thing that matters
here: **an entry is the operator's opt-in**, and this file is where the operator
says it.  That is v1's `cli:` section, ported as configuration.

**`slife2-cli` reads this section**, and declares one catalogue row per entry
(`cli:yt-dlp`) so that `tool_search` finds a command by what it does.  What is
still missing is the tool that would *run* one — the row is declared and the
entry is not yet a tool the model can call (DESIGN.md §9) — so what is settled
for now is where an entry is written down and what it may say, which is what
lets a config copied from v1 keep working when that tool lands.  The other half
of the same family, `skills/`, is read by `slife2-skills`, which serves
`skill_use`; where a skill's key comes from is the `skills:` section below.

Three things are worth stating outright.

**A provider holds its own credentials; a wire protocol holds the process.**  A
server process can only hold one `base_url` and one key, so providers cannot
share one — but they do not need to have one each either, because a single
process can serve every provider that speaks the same wire format and
`stream_chat(provider=...)` says whose credentials a call uses.  So the address
is keyed by `api` in the `servers:` section, not by provider, and a key exists
only in the process that needs it.

**A model is named `provider/model`.**  That is the reference used in `default`
and on the command line, and it is unambiguous in a file where several providers
may offer a model of the same name.

**Every model parameter is optional.**  Omitted means "say nothing and let the
gateway decide", which is not the same as passing a default — slife learned that
the hard way, and the fields carry the same meaning here.

Secrets
-------
1. ``keyring:<service>/<key>``  -> credstore
2. ``${VAR}``                   -> os.environ -> credstore
3. ``${VAR:-default}``          -> os.environ -> credstore -> literal default
4. anything else                -> as-is

Resolution never raises.  A missing secret degrades to its literal form
(``"${DEEPSEEK_API_KEY}"``), which fails loudly at the API call where it is a
readable error, rather than at startup where it is not.  It is also *lazy*: the
provider keeps the raw reference and only touches credstore when `api_key` is
read, so the TUI process never opens the OS keyring.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

import slife2.helpers.timeouts as _T
from slife2.libs.host.paths import home

#: The one place this module warns rather than refuses.  Everything else here
#: raises a `ConfigError` at load, because a malformed section is a mistake in
#: a file a person edits.  A `file-tunnel:` naming a provider this build does
#: not have is the exception — see `_file_tunnel` — so this exists for that
#: single line, and the asymmetry is the point rather than an oversight.
logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """A config file was found and could not be used.

    A *missing* file is not an error — the defaults are the config — but a
    malformed one is fatal, because silently ignoring a file the user wrote is
    the worst of the three possible behaviours.
    """


class WrongKindError(ConfigError):
    """A reference that resolves, and names something of another kind.

    A subclass rather than a flag because one reader has to tell it apart from
    every other refusal: `MediaSettings.resolve` falls back to the first model of
    the kind when the operator's `defaults:` entry has gone *stale* — that is a
    registered fallback (DESIGN.md §10.2), and its whole scope is "the name no
    longer resolves".  A name that resolves to a `tts` model where an `image` one
    was wanted is not stale, it is wrong, and its own docstring says that case is
    "refused here rather than at the provider, where it costs money".  With one
    exception type the two were indistinguishable and the fallback swallowed
    both, so the operator's stated default was discarded in silence and a paid
    call went to a model named in no `defaults:` line.
    """


#: The tool every model server answers to.  One tool for all of them, because a
#: backend is a backend whatever protocol it speaks: `api` selects the process,
#: and the request names the provider whose credentials to use.
MODEL_TOOL = "stream_chat"

#: Matches `${VAR}` and `${VAR:-default}`.  The name is deliberately restricted
#: to shell-safe identifiers so a literal `${...}` in a prompt does not become a
#: lookup.
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

#: The config file's name inside the data directory.
DEFAULT_CONFIG_NAME = "slife2.yaml"

#: The agent label used when `--agent` is not given.
#:
#: Not to be confused with `AGENT_SERVER_NAME` below.  This one is an *identity*
#: — it titles the window and is passed to the agent server — while that one is
#: the MCP name of the *process* that serves every agent.  They are one hyphen
#: apart, which is exactly why both say so here.
DEFAULT_AGENT = "slife2"

#: The topic namespace a local network gets when its section names none.  Its
#: own rather than `default`, because the two networks can meet on one broker and
#: `{org}/{unit}` is what tells them apart — see `SubagentSettings.network`.
SUBAGENT_UNIT = "local"


@dataclass(frozen=True)
class Net:
    """One of the two networks — everything about it that a *name* decides.

    sdd §3.5's two are different in kind: the agent↔agent one carries an agent's
    **remote** peers, the agent↔its-subagents one carries its **local** ones.  A
    record rather than prose, because each field is a name that crosses a process
    boundary and two of them have to agree: the section the plugin reads, the
    name it answers as, the tool names the model is handed, the channel a turn
    says it arrived on, and the envelope its messages open with.

    **Here rather than in the plugin**, because that is where names that cross
    boundaries already live (`A2A_SERVER_NAME`, `TUI_SERVER_NAME`): the launcher
    builds its probe out of `tool()`, the plugin builds its four tools out of the
    same call, and one spelling is what keeps a probe looking for a tool the
    server actually serves.
    """

    #: The section, and the key in `servers:` — the launcher's address for this
    #: network and the plugin's settings for it are one word.  **Also the keys
    #: this network's plugin is filed under in `PLUGINS`**, which is why
    #: `server_name` below can read its answer off that table rather than saying
    #: it a second time.
    key: str
    #: Where this network's settings are on `Config`, as the attributes to walk:
    #: the remote one *is* its section, and the local one is the network half of
    #: a section that also holds what a subagent costs (`SubagentSettings`).
    settings: tuple[str, ...]
    #: The four model tools are this plus a verb (`a2a_send_message`).
    prefix: str
    #: What a turn that arrived over this network says it came in on.
    channel: str
    #: What an inbound message's stored envelope opens with.
    marker: str
    #: What the window calls a line from this network's sender.
    bubble: str
    #: **The inbound ledger this network's process owns**, under an agent's own
    #: directory (`slife2.libs.host.paths.a2a_inbound_path`).  One file per
    #: network, and a name rather than a shared one because the file records
    #: something process-local: the completion bridges *this* mesh is holding.
    #: Both networks are served by one module and both are started on a machine
    #: that configures both, so one file between them meant each process reading
    #: the other's live pending tasks as a dead predecessor's orphans and
    #: rewriting the whole file from its own memory — a task announced as
    #: unanswerable while its bridge was answering, and a task added by one
    #: process erased by the other's next save, which is the one fact the file
    #: exists to keep.  **The remote network keeps the bare name it has always
    #: had**, so a home that already has one is not made to forget the peers it
    #: owes an answer to.
    ledger: str
    #: Whether every member of this network shares one topic namespace.
    #: True for the remote network, and false for the local one — see
    #: `unit_for`, which is where that is used and argued.
    shared_unit: bool = True

    @property
    def server_name(self) -> str:
        """The MCP name this instance answers as — `PLUGINS`', not a second copy.

        **A property rather than a field**, and the two networks are why:
        `A2A_SERVER_NAME` is the same fact as this one for the remote network,
        and a field here plus a constant there is the pair of literals that
        drifts.  Different for the two networks because one machine runs both and
        a probe has to be able to tell them apart — which is a fact about their
        rows in `PLUGINS` like every other server's name is.
        """
        return plugin(self.key).name

    def tool(self, verb: str) -> str:
        """One of this network's tools, named: `tool("list_agents")`."""
        return f"{self.prefix}{verb}"

    def unit_for(self, unit: str, member: str) -> str:
        """The topic namespace *member* belongs in on this network.

        **The remote network is the machine's one namespace; the local one is per
        maker.**  A member of the local network is named by the agent that made
        it, and two agents may each have a `researcher`: one namespace would make
        those two one MQTT identity (`{org}/{unit}/{agent_id}`), which a broker
        settles by disconnecting the older one — a kick loop rather than an
        error.  So the maker is in the unit on that network, and the subagents of
        two agents are as separate as their directories are.
        """
        return unit if self.shared_unit else f"{unit}-{member}"


#: The remote network: an agent's peers, which may be on another machine and on
#: somebody else's broker.  v1's mesh, unchanged.
REMOTE = Net(
    key="a2a",
    settings=("a2a",),
    prefix="a2a_",
    channel="a2a",
    marker="[PeerAgent:",
    bubble="PeerAgent",
    ledger="a2a.yaml",
)

#: The local network: an agent and the subagents it made, which are directories
#: under its own — so its broker is this machine's and its members never leave.
#:
#: **Its envelope says `SubagentPeer`, and the window says `Subagent(name)>`** —
#: two words because they have two readers, and only one of them can be the
#: sender's role.  The window's line is read by the agent that made the subagent,
#: so naming the relationship is right there; the envelope is read by whoever the
#: turn runs for, and on this network that is a subagent as often as its maker —
#: where `Subagent` would name the *receiver*.  `Peer` is the word that cannot be
#: read as the reader, which is why the remote envelope is `[PeerAgent:` and this
#: one is `[SubagentPeer:`: the pair names the two networks, and neither can be
#: mistaken for the agent reading it (`slife2.plugins.a2a.server`'s marker).
LOCAL = Net(
    key="subagent",
    settings=("subagent", "network"),
    prefix="subagent_",
    channel="subagent",
    marker="[SubagentPeer:",
    bubble="Subagent",
    # Beside the remote network's rather than the same file: see `Net.ledger`.
    ledger="a2a-subagent.yaml",
    shared_unit=False,
)

#: The four tools a network offers the model, by verb.  **Here rather than in the
#: plugin that serves them**, because one of them is also what the launcher
#: probes with (`expected_tool`) and a probe looking for a name the server does
#: not serve is a server that never starts.  `Net.tool` is the other half:
#: `a2a_send_message` and `subagent_send_message` are one verb and two networks.
SEND_MESSAGE = "send_message"
CANCEL_TASK = "cancel_task"
LIST_AGENTS = "list_agents"
BROADCAST = "broadcast"

#: The remote network's section key, which is also its key in `servers:`.
A2A_KEY = REMOTE.key


@dataclass(frozen=True)
class Plugin:
    """One server slife2 starts: everything about it that is a fact about *it*.

    **A plugin used to be described five times over**, and this is the one place
    that replaced them.  Its MCP name was a constant here and a `SERVER_NAME`
    literal in its own module, held together by an assertion; its module, name
    and identity tool were a tuple in `slife2.libs.host.launcher`; the order it
    starts in was `LOCAL_SERVERS` here; and its port was a row of
    `default_config`'s `servers` table.  Adding a plugin meant finding all five,
    and replacing one — the thing this arrangement is for — meant finding them
    again.  Nothing about a plugin except *what it does* lives outside this
    table now, and a plugin reads its own name out of it (`SERVER_NAME`), so the
    two spellings that used to drift have one home between them.

    **Spelled out rather than read off the modules, and this is the reason it
    stays that way.**  Reading them means importing them: the TUI and the agent
    loop must not pull in a server, and the agent loop in particular must never
    reach `openai_server`.  So the module is a *string* here, exactly as
    `API_BACKENDS` was, and the process that serves a plugin is started by name.

    The five fields are what every reader needs and nothing else:

    * `key` — the plugin's key in the `servers:` table, and so the word
      `config.server(key)` answers to, `slife2 down` stops it by, and a `status`
      line prints.  Two families' keys carry a suffix the others do not
      (`skills-server`, `cli-server`) because the *catalogue source* they declare
      their rows under is `skills`/`cli`, and a source's rows must not share an
      id with the tools of the server that declares them.
    * `module` — what `python -m` runs, which is also the file: a plugin is a
      process and this is the only thing that says which.
    * `name` — the MCP name the server advertises, which is what a client proves
      it reached the server it meant with (`slife2.libs.plugin.protocol.identifies`).
    * `probe` — the tool that identifies it when it advertises no name at all.
      It has to be one the server certainly serves, and one that answers without
      anything having happened yet: a server that has just started is the state a
      probe runs in.
    * `port` — where it listens when the config's `servers:` block names none.

    **`api` is the one field that is not about the process.**  It says this
    server speaks one wire protocol rather than serving a capability of its own,
    and it is what a caller filters on: `API_BACKENDS` and `LOCAL_SERVERS` below
    are the two halves this table splits into.  A backend is started only when
    the config has a provider that speaks its protocol, because a protocol no
    provider uses has nothing to serve and starting it would be a process
    holding nobody's key.
    """

    key: str
    module: str
    name: str
    probe: str
    port: int
    api: bool = False


#: Every server slife2 starts, in the order they are described in.
#:
#: **The order of the two halves is load-bearing and this is the whole of it.**
#: The model backends come first because a model server answering first is what
#: keeps a first turn from failing and retrying; of those, `openai-responses` is
#: appended last on purpose, and to its own protocol rather than as a flag on
#: `openai-completions` — the Responses API takes a different input shape, names
#: its tools differently and streams different events, so it is a process of its
#: own like every other row here.  What follows is `LOCAL_SERVERS`' order, which
#: is a preference rather than a dependency: **the two edges that are
#: dependencies are not here** — `embeddings` before `context`, because the
#: context plugin opens the embedder before it serves anything, and every plugin
#: the hub asks before `toolhub` — because prose is not something a launcher can
#: act on, and they are a table (`slife2.libs.host.launcher.NEEDS`).  Everything
#: not named there starts at once, so this order only decides who is first off
#: the line.
#:
#: **There is no `db` here**, and its absence is the design rather than a gap.
#: It served the turns and the tool catalogue, and neither needed a process: a
#: store has one writer, and "one writer" is a property of a SQLite file, not of
#: a server.  The turns are `context`'s now and the catalogue is the hub's, both
#: out of the same library (`slife2.libs.store.db`) — v1's own answer was the
#: same, `memdb` shipping as a plugin *and* being imported.
PLUGINS: tuple[Plugin, ...] = (
    # One port per wire protocol, not per provider: a process speaks one format,
    # and `stream_chat(provider=...)` picks whose credentials.
    Plugin(
        key="openai-completions",
        module="slife2.plugins.llm.openai_server",
        name="slife2-llm-openai",
        probe=MODEL_TOOL,
        port=8001,
        api=True,
    ),
    Plugin(
        key="anthropic-messages",
        module="slife2.plugins.llm.anthropic_server",
        name="slife2-llm-anthropic",
        probe=MODEL_TOOL,
        port=8002,
        api=True,
    ),
    Plugin(
        key="openai-responses",
        module="slife2.plugins.llm.openai_responses_server",
        name="slife2-llm-openai-responses",
        probe=MODEL_TOOL,
        port=8003,
        api=True,
    ),
    # Embeddings are an OpenAI-protocol endpoint and *not* one of the three
    # above, so they are served by a process of their own rather than borrowed
    # from `openai-completions`: that process is built from the chat providers,
    # and there is no chat provider here to attach an embedding model to.  It is
    # a plugin (rather than a library) for §1.1's reason — it holds a key — and
    # it identifies itself by `describe`, the tool the db calls before it can
    # build an index.  See `slife2.plugins.llm.embeddings_server`.
    Plugin(
        key="embeddings",
        module="slife2.plugins.llm.embeddings_server",
        name="slife2-llm-embeddings",
        probe="describe",
        port=8004,
    ),
    # A conversation's memory: the turn log, and the decision about which of its
    # turns a turn runs on.  It identifies itself by `remember`, the write
    # everything else about a conversation follows from.
    Plugin(
        key="context",
        module="slife2.plugins.context.server",
        name="slife2-context",
        probe="remember",
        port=8010,
    ),
    # The two families that are not tools but have to be findable, each owned by
    # the process that owns its config section: the playbooks in `<data>/skills/`
    # and the programs the config's `cli:` section records as installed.  They
    # are plugins all the same, because a family's rows and the tool that reads
    # them belong to the process that owns the family — and because both have
    # model-facing tools now, and a tool needs a server to be served from.  Their
    # identifying tool is what the fallback check looks for when a server reports
    # no name of its own, so it has to be one they certainly serve: `cli-server`
    # offers the model four `cli_*` tools and no more, and `list_sources` is the
    # one the hub asks it for.
    Plugin(
        key="skills-server",
        module="slife2.plugins.skills.server",
        name="slife2-skills",
        probe="skill_use",
        port=8031,
    ),
    Plugin(
        key="cli-server",
        module="slife2.plugins.cli.server",
        name="slife2-cli",
        probe="list_sources",
        port=8032,
    ),
    # The two whose whole job is somebody else's servers: the `mcp:` section and
    # the `rest-api:` one.  They hold the connections the toolhub used to hold,
    # and declare what they hold to it — the hub keeps the tool *set* and no link
    # to anybody.  Neither offers the model anything of its own, so each
    # identifies itself by the tool it holds them *with*.
    Plugin(
        key="mcp-tools",
        module="slife2.plugins.mcp_tools.server",
        name="slife2-mcp-tools",
        probe="list_sources",
        port=8033,
    ),
    Plugin(
        key="restapi-tools",
        module="slife2.plugins.restapi.server",
        name="slife2-restapi-tools",
        probe="list_sources",
        port=8034,
    ),
    # WeChat, and the one plugin here that is nobody's family: it declares no
    # catalogue rows and owns no entries, and what it holds is a login per agent
    # and a conversation with somebody off this machine.  It is a plugin for the
    # reason §1.1's first table row gives — it holds a credential, and a library
    # would put the token in every importer — and the only thing written down
    # about it is an address; a login is scanned rather than configured, and
    # lives inside the agent's own directory.  It identifies itself by
    # `wechat_check_status` rather than by a tool the hub asks it for, because
    # the hub asks it for nothing — and the status tool is the one that works
    # before any login exists, which is the state it starts in.
    Plugin(
        key="wechat",
        module="slife2.plugins.wechat.server",
        name="slife2-wechat",
        probe="wechat_check_status",
        port=8030,
    ),
    # The private cabinet: notes, a diary, saved files and reports, one corpus
    # per agent under `<data>/agents/<agent>/cabinet/`.  It reads no config
    # section of its own — a cabinet is written by the model and by a person,
    # never configured — so what is here is its address.  It identifies itself by
    # `note_list`, which is a tool it serves before anything has been written to
    # it: the state a fresh cabinet is in.
    Plugin(
        key="cabinet",
        module="slife2.plugins.cabinet.server",
        name="slife2-cabinet",
        probe="note_list",
        port=8035,
    ),
    # Rendering, speech and transcription, behind `media:`.  A plugin for §1.1's
    # table — it holds a provider's key and speaks to somebody else's endpoint —
    # and a *family* of endpoints rather than one, which is why it has a module
    # of its own.  It identifies itself by `generate_image`, the first of the
    # four capabilities and the one a config is most likely to have.
    Plugin(
        key="media",
        module="slife2.plugins.media.server",
        name="slife2-media",
        probe="generate_image",
        port=8036,
    ),
    # A public HTTPS URL for a local file, so something that cannot read this
    # machine's disk — a vision model, a chat API that takes links — can fetch
    # one anyway.  A plugin for §1.1's reason: what it holds is somebody else's
    # connection (an ngrok session, an SSH forward, a cloudflared child), and it
    # is the one plugin here that serves a second HTTP route beside its MCP
    # endpoint, because the far end fetches `/share/<token>` through the tunnel.
    # Its settings are the `file-tunnel:` section; it identifies itself by
    # `expose_local_image`, the tool it exists for and the one a config is most
    # likely to have been set up to use.
    Plugin(
        key="file-tunnel",
        module="slife2.plugins.file_tunnel.server",
        name="slife2-file-tunnel",
        probe="expose_local_image",
        port=8038,
    ),
    # The terminal's own inbox: where a channel hands a message so that the
    # window showing that conversation runs it as something the person typed.
    # One process and a session per agent — the window attaches to it and holds
    # that call open, and nothing binds a port per terminal.  It has no section:
    # an address is the whole of what is written down about it, which is also why
    # it reads no config of its own.  It identifies itself by `attach` — the call
    # a window holds open for as long as it is showing a conversation, which is
    # served before any window exists and so is the state this plugin starts in.
    # The probe only looks for the name in `tools/list`, so naming a tool that
    # blocks is safe.
    Plugin(
        key="tui",
        module="slife2.plugins.tui.server",
        name="slife2-tui",
        probe="attach",
        port=8005,
    ),
    # The model's tools, and the only process that decides what they are.  It
    # holds no credential and no connection to anybody's server: every plugin
    # above is asked for a tool list and for the sources it holds, and the two
    # `*-tools` plugins above are the ones that reach the entries under `mcp:`
    # and `rest-api:`.  It identifies itself by `list_tools`, the call the agent
    # loop makes to find out what it may offer.
    Plugin(
        key="toolhub",
        module="slife2.plugins.toolhub.server",
        name="slife2-toolhub",
        probe="list_tools",
        port=8020,
    ),
    # The loop itself, shared by every conversation on this machine and
    # stateless: the agent name is passed in on each call.  A plugin because what
    # it holds is loops — state with a lifetime rather than a file — and it
    # identifies itself by `send_message`, the one call everything else about a
    # conversation is made of.
    Plugin(
        key="agent",
        module="slife2.plugins.agent.server",
        name="slife2-agent",
        probe="send_message",
        port=8000,
    ),
    # Background tasks: a loop of the calling agent's own, one per task, answered
    # by a message in the conversation that asked for it.  A plugin because what
    # it holds is loops — the same reason the agent server is one — and it
    # reaches three peers rather than being reached by them: the hub for a tool
    # list, a model server for the loop, and either the terminal or the agent
    # server to hand the answer over.  It reads one section of its own,
    # `agent-task:`, and only for its cap; a task's prompt and step count are the
    # agent's.  It identifies itself by `agent_task_list`, the one tool it serves
    # that takes no argument and needs nothing running, which is the state it
    # starts in.
    Plugin(
        key="agent-task",
        module="slife2.plugins.agent_task.server",
        name="slife2-agent-task",
        probe="agent_task_list",
        port=8037,
    ),
    # The clock: the one thing here that starts work nobody asked for at the
    # moment it starts, and the ledger of what it fired — what one agent has
    # scheduled, and every run of it, in `<agent>/schedule.db`.  A plugin because
    # what it holds is timers, and it identifies itself by `schedule_list`, which
    # takes no argument and needs nothing running — the state this plugin starts
    # in, like the task server's own probe tool and for the same reason.
    Plugin(
        key="schedule",
        module="slife2.plugins.schedule.server",
        name="slife2-schedule",
        probe="schedule_list",
        port=8039,
    ),
    # Agent-to-agent traffic over MQTT, on the published A2A-over-MQTT profile.
    # A plugin for §1.1's reason — what it holds is somebody else's connection —
    # and the only one here whose *identity* is per agent: the agent name is the
    # MQTT client id, so one shared process holds one mesh per agent it serves.
    # Its settings are the `a2a:` section, and **that section's presence is the
    # machine's opt-in**: a machine that should not be on the mesh is a machine
    # where nobody writes one, which is the same argument the `wechat` section is
    # absent by.  It identifies itself by `a2a_list_agents`, by the test that
    # picked `schedule_list`: the tool takes no argument and works before any
    # connection exists — the state this plugin starts in on a machine whose
    # broker is not up yet, which is the state a probe has to be able to name.
    Plugin(
        key=REMOTE.key,
        module="slife2.plugins.a2a.server",
        name="slife2-a2a",
        probe=REMOTE.tool(LIST_AGENTS),
        port=8040,
    ),
    # The **local** network: the same module, one port over, and the other half
    # of sdd §3.5's pair.  It is here rather than absent for `a2a`'s reason read
    # the other way round: a machine reaches its own subagents, and `subagent_*`
    # is a feature the config can turn on by writing the section — while the
    # *port* is nobody's decision, so it is written down with every other one.
    # A second plugin row rather than a second module because it is the same
    # protocol and the same mesh: what a subagent's traffic needs is a namespace
    # and a broker of its own (`Net`).  **Its name is its own** because identity
    # is what a probe checks: one machine runs both, and a `status` that could
    # not tell them apart would be the same as no name at all.  It identifies
    # itself by *its* list tool, which is that probe's whole test — the two
    # instances are one machine apart and a `status` line has to say which.
    Plugin(
        key=LOCAL.key,
        module="slife2.plugins.a2a.local_server",
        name="slife2-subagent",
        probe=LOCAL.tool(LIST_AGENTS),
        port=8041,
    ),
)

#: The table by key, which is how a name is looked up from a key.  Built once,
#: because every reader below wants it and rebuilding it per call is how a
#: `plugin()` in a loop becomes a scan.
_BY_KEY: dict[str, Plugin] = {one.key: one for one in PLUGINS}


def plugin(key: str) -> Plugin:
    """The row *key* names.  **Never raises**, and that is on purpose.

    The keys that reach here are the ones this module wrote down — a `Net`'s own
    key, a constant's own key — so an unknown one is a programming error inside
    this file rather than anything a config can cause, and a `KeyError` traceback
    naming the key is a better answer than a config refusal nobody can act on.
    A key from *outside* is checked against `Config.server` first, which is the
    one place a word somebody typed is judged.
    """
    return _BY_KEY[key]


#: The MCP names the servers in this system advertise, as `Client.server_info`
#: reports them.  A client proves it reached the server it meant by comparing
#: against these; see `slife2.libs.plugin.protocol.identifies`.
#:
#: **Views over `PLUGINS` and not a second table.**  They are spelled as
#: constants rather than read out of the rows at each call site because that is
#: how they were written down before there was a table, and a caller that wants
#: `AGENT_SERVER_NAME` should not have to know that a registry exists to get it.
#: Two things follow, and both are the point of the arrangement: a plugin module
#: binds its own `SERVER_NAME` to the constant rather than to a literal, so the
#: two spellings that used to drift are one; and `REMOTE`/`LOCAL` read their
#: `server_name` off the same rows (`Net.server_name`), which is what removed the
#: last place the mesh's name was written twice.
AGENT_SERVER_NAME = plugin("agent").name
TUI_SERVER_NAME = plugin("tui").name
CONTEXT_SERVER_NAME = plugin("context").name
MCP_TOOLS_SERVER_NAME = plugin("mcp-tools").name
RESTAPI_TOOLS_SERVER_NAME = plugin("restapi-tools").name
SKILLS_SERVER_NAME = plugin("skills-server").name
CLI_SERVER_NAME = plugin("cli-server").name
TOOLHUB_SERVER_NAME = plugin("toolhub").name
EMBEDDINGS_SERVER_NAME = plugin("embeddings").name
WECHAT_SERVER_NAME = plugin("wechat").name
CABINET_SERVER_NAME = plugin("cabinet").name
MEDIA_SERVER_NAME = plugin("media").name
FILE_TUNNEL_SERVER_NAME = plugin("file-tunnel").name
AGENT_TASK_SERVER_NAME = plugin("agent-task").name
SCHEDULE_SERVER_NAME = plugin("schedule").name
A2A_SERVER_NAME = plugin(REMOTE.key).name

#: The plugins that are not model backends, and so have a name of their own
#: rather than one derived from a wire protocol.  This is the *set* of ours, in
#: the order `Config.plugins` reads them out in, and it is derived from `PLUGINS`
#: rather than written down beside it: two lists kept in step by hand is exactly
#: what this table replaced.
LOCAL_SERVERS: tuple[str, ...] = tuple(one.key for one in PLUGINS if not one.api)

#: The wire protocols this distribution implements, and the server module that
#: speaks each.  `api` selects one; an unknown value is a typo worth reporting
#: rather than a provider that quietly never starts.  Derived from `PLUGINS`, so
#: that adding a protocol is a row and not a row and a dict entry.
API_BACKENDS: dict[str, str] = {one.key: one.module for one in PLUGINS if one.api}

#: The MCP name the server for each wire protocol advertises — what
#: `Client.server_info` reports, and so what a caller compares against to prove
#: it reached the server it meant rather than some other MCP server on the port.
API_SERVER_NAMES: dict[str, str] = {one.key: one.name for one in PLUGINS if one.api}


def net_settings(config: Config, net: Net) -> A2ASettings:
    """Where one network's settings are: its section, or the network half of one.

    A walk rather than a branch, because which of the two shapes a network's
    settings have is `Net.settings`' own fact and a second place to spell it is a
    second place to get it wrong.
    """
    found: Any = config
    for part in net.settings:
        found = getattr(found, part)
    assert isinstance(found, A2ASettings)
    return found


#: Where `--a2a-broker` puts the address it was given, for the servers that have
#: to read it.  An environment variable rather than an argument, which is
#: `slife2.libs.host.paths.apply_data_dir`'s mechanism and its reason: the process
#: that must end up on that broker is generally not the one that was told.
A2A_BROKER_ENV_VAR = "SLIFE2_A2A_BROKER"


def _credstore_lookup(key: str) -> str | None:
    """Look up an environment-variable name in the OS keyring.

    The env var name *is* the credential-store key, so ``DEEPSEEK_API_KEY``
    resolves whether it was exported or stored with ``credstore set``.

    **No backend at all degrades to "not found"; a backend that failed does
    not.**  credstore itself answers None when this host has no keyring — a CI
    runner, a container — and that is the declared degrade: the chain steps past
    it to the reference.  A keyring that *is* there and refused (locked, corrupt,
    a platform credstore does not support) raises instead, because reading that
    as "not found" turns a credential nobody could read into a credential that
    is absent, and the `${VAR}` text then travels as the secret itself
    (DESIGN.md §10.1).  The failure lands where the secret is resolved.
    """
    try:
        from credstore import get_credential
    except ImportError:
        return None  # credstore is not installed: there is no backend to consult
    return get_credential(key)


def resolve_secret(value: object) -> str:
    """Resolve one config value through the chain in the module docstring.

    Takes `object` because YAML hands back ints and bools for unquoted scalars,
    and an API key written as a bare number should become a string rather than
    an exception.

    **A keyring that is not installed degrades; a keyring that failed does
    not.**  With no `credstore` there is no backend to consult, so a
    ``keyring:`` reference falls to the `${VAR}` pass below and is left verbatim,
    which is the declared degrade `_credstore_lookup` also names.  With one
    installed, a `resolve_uri` that raises — the credential is not stored, the
    backend is locked — is allowed out: reading it as "not found" would leave
    the ``keyring:`` text to travel as the secret itself, which is the
    deformation DESIGN.md §10.1 names.  The failure lands where the secret is
    resolved.

    **A credential that was never stored is a config mistake, and it is said as
    one.**  credstore answers `KeyError` for that — its documented "not found",
    as against a store that was asked and refused — and a bare `KeyError` out of
    `load()` is not something any caller on this path catches: `slife2`'s entry
    point and `family.py` both answer a bad config with their own one line for
    `ConfigError` alone, so `env: {TOKEN: 'keyring:slife/a-key-nobody-stored'}`
    printed a traceback instead of naming the reference, at the one place where
    naming it is the whole of the fix.  The store that is *there* and refused
    keeps raising rather than degrading, which is the registered behaviour and
    the reason this catches the one exception type rather than all of them.
    """
    if not isinstance(value, str):
        return str(value)

    try:
        from credstore import is_keyring_uri, resolve_uri
    except ImportError:
        # no backend to consult, so a `keyring:` reference is left to the
        # `${VAR}` pass below, which leaves it verbatim — the declared degrade
        pass
    else:
        if is_keyring_uri(value):
            try:
                return resolve_uri(value)
            except KeyError as exc:
                raise ConfigError(
                    f"{value}: this machine's credential store has no such "
                    f"credential — store it with "
                    f"`credstore set {value.removeprefix('keyring:')}`, or write "
                    f"the value itself"
                ) from exc

    def _replace(match: re.Match[str]) -> str:
        var, default = match.group(1), match.group(2)
        found = os.environ.get(var) or _credstore_lookup(var)
        if found:
            return found
        # Unresolvable and no default: leave the reference verbatim, so the
        # failure surfaces where the value is used with its own name intact.
        return default if default is not None else match.group(0)

    return _ENV_PATTERN.sub(_replace, value)


def unresolved_reference(value: object) -> str:
    """The reference a secret value **still is**, or `""` when it resolved.

    **The other half of `resolve_secret`'s degrade, and the only place it is
    read.**  A value that could not be resolved is left *verbatim* by the
    function above — a `${VAR}` neither the environment nor the keyring had, or
    a `keyring:NAME` with no credstore installed to consult it — and every
    caller that is about to *use* the value has to answer the same question
    about it.  Four of them answered it separately, with four different rules:
    the model servers tested `startswith("${")` (so a bare `keyring:NAME`
    travelled as the secret), the tunnel and the skills reader had a regex each,
    and `rest-api`'s note had a third.  A rule that decides whether a credential
    is real cannot be four rules, because the permissive copy is the one that
    sends the reference to somebody else's server.

    Returning the *reference* rather than a boolean is what lets a caller say
    which one did not resolve, which is the whole of what a person needs to fix
    it; a `401` from the far end says nothing.
    """
    text = str(value).strip()
    if text.startswith("keyring:"):
        return text
    match = _ENV_PATTERN.search(text)
    return match.group(0) if match else ""


@dataclass(frozen=True)
class ServerSettings:
    """Where one MCP server listens."""

    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"

    @property
    def url(self) -> str:
        """The streamable-HTTP endpoint, as a client should address it."""
        return f"http://{self.host}:{self.port}{self.path}"


@dataclass(frozen=True)
class ModelSettings:
    """One model a provider serves, and how to call it.

    Every numeric field is optional and `None` means *omit it*, which is not the
    same as passing a sensible-looking default: several gateways reject a
    `temperature` they did not ask for, and a model that reasons natively may
    refuse a `thinking` field entirely.  Saying nothing is the safe value, so
    saying nothing is what an absent field means.
    """

    #: The API's model name, and the local id — `"deepseek/deepseek-flash"` in
    #: references is built from the provider name plus this.
    model: str
    #: What to show a person.  Falls back to `model` when absent.
    name: str = ""
    #: The model thinks natively, so reasoning is worth asking for.
    #:
    #: On the OpenAI-compatible wire this also decides that the model's own
    #: reasoning is carried *back* to it: the turn keeps it, and
    #: `slife2.plugins.llm.openai_server` re-sends it as `reasoning_content`, which the
    #: DeepSeek reasoners require on every assistant message once thinking has
    #: been asked for.  v1's rule, and the two belong together — a model that
    #: thinks is the model whose endpoint knows the field's name.  Nothing
    #: depends on it otherwise: this is a fact about the model, not a switch.
    reasoning: bool = False
    #: Modalities accepted.  This is the config that says whether a model can
    #: read an image, and the agent server refuses an attachment against a model
    #: whose `input` does not list `image` — silently dropping one is worse than
    #: saying the model cannot read it.  The check is `attach_image`'s, in
    #: `slife2.plugins.agent.server`.
    input: tuple[str, ...] = ("text",)
    #: Token budget, used for the context percentage in the status bar.
    context_window: int = 0
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    #: `compat.thinking` — `enabled`, `disabled`, or `omit` for gateways that
    #: reject the standard shape while reasoning anyway.
    thinking: str = ""
    #: `compat.stream_usage` — `omit` for a gateway that rejects OpenAI's
    #: `stream_options` extension with a 400.
    #:
    #: **Not tri-state like `store`, and the difference is which way the default
    #: has to fall.**  There, saying nothing leaves the endpoint's own default in
    #: place and that is the right one.  Here it is not: usage arrives only
    #: because it was asked for, and §6 of DESIGN.md is the measurement that
    #: made the counts load-bearing — an adapter reading only the documented
    #: OpenAI shape reported zero tokens against a real endpoint.  So an absent
    #: value means *send*, and the escape hatch is a marked one.
    stream_usage: str = ""
    #: `compat.store` — whether the Responses API may keep the response
    #: server-side.  **Tri-state, and the third state is the default.**
    #:
    #: `None` means *send nothing*, leaving each endpoint's own default in
    #: place; `True` and `False` are requests.  The distinction is not academic
    #: here: the API's default is to store, and Responses-compatible endpoints
    #: differ in whether they implement the field at all, so a server that
    #: picked a value for every call would break against the ones that do not
    #: accept it.  Only the Responses backend reads this; the other two ignore
    #: it.  See `slife2.plugins.llm.openai_responses_server.store_parameter`.
    store: bool | None = None

    @property
    def label(self) -> str:
        return self.name or self.model

    @property
    def accepts_images(self) -> bool:
        return "image" in self.input


@dataclass(frozen=True)
class ProviderSettings:
    """One endpoint and one set of credentials, speaking one wire protocol.

    No address of its own: a provider is reached through the server for its
    `api`, because one process speaks one wire format for every provider that
    uses it.  `stream_chat(provider=..., model=...)` says which.

    `api_key` is read lazily — the raw reference is kept and credstore is only
    consulted on access — so a key is only resolved when a call actually needs
    it.
    """

    api: str
    base_url: str
    api_key_ref: str
    models: dict[str, ModelSettings] = field(default_factory=dict)

    @property
    def api_key(self) -> str:
        """The resolved key.  Touches credstore, so call it where it is needed."""
        return resolve_secret(self.api_key_ref)

    def model(self, model_id: str = "") -> ModelSettings:
        """Look up a model by its API name, defaulting to the first.

        Raises:
            ConfigError: If the id is unknown.  A message naming the models this
                provider does offer is more use than a KeyError, and this is a
                config mistake rather than a runtime one.
        """
        if not model_id:
            if not self.models:
                raise ConfigError("provider has no models")
            return next(iter(self.models.values()))
        try:
            return self.models[model_id]
        except KeyError:
            known = ", ".join(self.models) or "(none)"
            raise ConfigError(
                f"provider offers no model {model_id!r}; known: {known}"
            ) from None


@dataclass(frozen=True)
class EmbeddingProviderSettings:
    """One OpenAI-compatible endpoint that turns text into vectors.

    **Not a `ProviderSettings`, and not one of the `providers:`.**  What arrives
    here is `base_url`, one model id and a key: an embedding endpoint speaks no
    chat protocol, has no reasoning to ask for and no sampling to control, and
    is called at `{base_url}/embeddings` and nowhere else.  A provider entry
    would be a row of fields none of which this uses, plus a chat backend
    started for a model no conversation can call.

    `api_key` is resolved lazily like `ProviderSettings.api_key`, so credstore is
    only consulted when a call needs it.
    """

    name: str
    base_url: str
    model: str
    api_key_ref: str = ""

    @property
    def api_key(self) -> str:
        """The resolved key.  Touches credstore, so call it where it is needed."""
        return resolve_secret(self.api_key_ref)

    @property
    def label(self) -> str:
        return f"{self.name}/{self.model}"


@dataclass(frozen=True)
class EmbeddingsSettings:
    """Where vectors come from — and there is always somewhere.

    **No `enabled` switch, on purpose.**  v1 had one, and its cost was a
    misconfiguration that looked exactly like a working system with nothing to
    recall: semantic search quietly off, every keyword search still answering.
    The db plugin cannot be run without an embedding model, so the switch had
    one useful setting, and a setting with one useful value is a way to be wrong.
    """

    #: name -> endpoint.  Never empty in a config that loaded; see `_embeddings`.
    providers: dict[str, EmbeddingProviderSettings] = field(default_factory=dict)
    #: Which of them is in use.  A provider id, not a `provider/model` ref: there
    #: is one model per endpoint here, and the endpoint is the choice being made.
    active: str = ""

    def active_provider(self) -> EmbeddingProviderSettings:
        """The provider in use — `active`, or the first when none was named.

        Raises:
            ConfigError: If there are none, which `_embeddings` refuses at load
                time — so reaching this means a `Config` assembled in code
                rather than read from a file — or if `active` names a provider
                this config does not have.  Substituting the first for an
                unknown name is the deformation §10.1 names: vectors would come
                from an endpoint nobody chose, and the index's recorded identity
                would change with nothing in any file to say why.
        """
        if not self.providers:
            raise ConfigError("no embedding provider configured")
        if not self.active:
            return next(iter(self.providers.values()))
        if self.active not in self.providers:
            known = ", ".join(sorted(self.providers))
            raise ConfigError(
                f"embeddings.active_model: {self.active!r} is not a configured "
                f"provider; it has: {known}"
            )
        return self.providers[self.active]


@dataclass(frozen=True)
class AgentSettings:
    """How the agent loop behaves, and where it listens.  Not an identity."""

    server: ServerSettings = field(default_factory=ServerSettings)
    #: Model calls allowed in one turn, or **0 for no cap** — which is also what
    #: an absent key reads as.  "Nobody wrote a number" and "somebody wrote
    #: zero" are one statement here on purpose rather than by accident: a cap is
    #: something asked for, so a config that does not ask for one does not have
    #: one, and inventing a number for it is how a turn comes to be cut off at a
    #: bound nobody chose.  `AgentLoop` reads the same rule off the number it is
    #: handed, so "is there a cap" is decided in exactly one place.
    max_steps: int = 0
    #: A Jinja2 template for the system prompt — an absolute path by the time
    #: this is built, or "" for the one the distribution ships.  A template
    #: rather than a string because the prompt has a hole in it where the agent
    #: name goes, and the hole has to be *somewhere* the caller can see.
    system_prompt: str = ""


@dataclass(frozen=True)
class AgentTaskSettings:
    """The `agent-task:` section, and the one number an agent task costs.

    **`agent:`, read by two plugins.**  Everything else a task's turn is made of
    — the system prompt and `max_steps` — is the agent's own and comes out of
    that section, because a task *is* the agent working: a prompt or a step count
    that meant something different for a task would be two agents wearing one
    name.  So the rule that the section a key lives in is the plugin that reads
    it has exactly one exception, and this is where it is stated.

    What is left is the number only this plugin has an opinion about.
    """

    #: How many tasks one agent may have running at once.
    #:
    #: A cap rather than a preference, and the only bound this feature has: a
    #: task may start a task — the tool set is the one catalogue and nothing
    #: filters it — so without a cap a model can grow loops without limit.  What
    #: it does not bound is how much work happens over *time*; DESIGN.md §9
    #: holds that one open, and `agent_task_list` with `agent_task_cancel` is
    #: what makes it visible and stoppable in the meantime.
    #:
    #: **The number lives here and nowhere else**, and `slife2-agent-task` reads
    #: it off the config at the call that enforces it rather than keeping a
    #: constant of its own — there *was* one, nothing read it, and it was free
    #: to disagree with the file an operator edits.
    max_running: int = 4


@dataclass(frozen=True)
class SubagentSettings:
    """The `subagent:` section, and the one number a maker may have.

    Read by `slife2-agent`, which is where the two tools that make and unmake a
    subagent live (`slife2.libs.host.launcher` starts no plugin for this: a
    subagent is not a process, so it has no server and no address — sdd §3.5).

    Everything else about a subagent is an agent's and comes out of `agent:`, for
    `AgentTaskSettings`' reason one class up: a subagent is an agent that belongs
    to another one, so a step count that meant something different for it would
    be two agents wearing one name.

    **What is not here, and not in any config, is anything about *one*
    subagent.**  The model it runs on, the role it was made for and whether it
    was made as a fork are in the subagent's own file, inside its own directory,
    and they are deleted with it
    (`slife2.plugins.agent.server.subagent_spec`): a subagent is made today and
    removed tomorrow, so its settings go where its directory goes rather than
    into a file an operator has to keep in step with it.  Its *prompt* is not
    `agent.system_prompt` either — it is the shipped `subagent.j2`, because what a
    subagent has to be told is what it is, and a prompt written for the agent
    somebody is running cannot say that.

    What is left is what the *machine* has an opinion about.
    """

    #: Where a subagent's network is, from the same section — `broker`, `org`
    #: and `unit`, parsed by `_a2a` because a section holding a network should
    #: not spell its keys a second way.  **`configured` here is this section's
    #: presence**, which is what makes a machine with no `subagent:` a machine
    #: whose subagents are not reachable — the same shape `a2a:` has, one level
    #: down.
    #:
    #: The default unit is its own rather than `default`, and that is not
    #: decoration: the two networks can share one broker, and a namespace is
    #: `{org}/{unit}` — a local network left at the remote one's default would be
    #: the *same* network, with the machine's own subagents visible to its remote
    #: peers.
    network: A2ASettings = field(
        default_factory=lambda: A2ASettings(unit=SUBAGENT_UNIT)
    )

    #: How many subagents one agent may have — a cap, not a target.
    #:
    #: **A cap because nothing else bounds it.**  The tool set is the one
    #: catalogue and nothing filters it, so a subagent may make a subagent, and
    #: without this a model can grow a tree of them without limit.  What it costs
    #: when it does is small per member — a subagent is a conversation in the
    #: shared agent server, measured at ~1.5 MB against a `--headless` instance's
    #: ~100 MB (`DESIGN §1.10`) — so the number here is about a runaway rather
    #: than about the machine, and it is larger than the four an agent task gets
    #: because a subagent is a thing an agent *has* rather than a run it started.
    max_running: int = 32


@dataclass(frozen=True)
class ContextSettings:
    """How a conversation's context is decided.  Read by `slife2-context`.

    Its own section rather than more keys under `agent:`, because the section a
    key lives in is the plugin that reads it: `agent:` is what the agent server
    reads, and every number here is the context plugin's.  Two readers of one
    section is how a setting comes to mean two things.
    """

    #: Both are **fractions of the model's own context window**, and both are
    #: therefore only meaningful where the model's config declares one.  The
    #: ceiling is what a kept context is measured against — a recall's budget is
    #: the headroom below it — and the floor is the size a recall may spend when
    #: nothing was kept.
    ceiling: float = 0.8
    floor: float = 0.2
    #: Most turns one recall may select.  A cap rather than a preference, for the
    #: reason `MAX_PAGE` is one: the count is what is left to bound a context by
    #: when the model's window is undeclared.
    recall_limit: int = 40
    #: The similarity a *measured* candidate must reach to be recalled.  The
    #: keyword leg is exempt — it has no similarity to measure, and an exact
    #: match is a stronger signal than a cosine neighbourhood (`slife2.libs.turn.context`).
    min_similarity: float = 0.45
    #: How long the discriminator call may take before the turn runs on the
    #: context it already has.  Generous for a remote model, tight for the
    #: reason `slife2.plugins.context.server` gives: the answer is optional by design, so
    #: a slow one is only making the *turn* late.
    #:
    #: **The default is the registry's value, read when a config is built** —
    #: `slife2.helpers.timeouts.work.recall_discriminator` — so this is an *override* and
    #: not a second seat for the number.  Read through a factory rather than as a
    #: def-time default, which would capture it at import and ignore a patch
    #: (`slife2.helpers.timeouts` states the rule).
    timeout: float = field(
        default_factory=lambda: _T.timeouts.work.recall_discriminator
    )


@dataclass(frozen=True)
class ToolServerSettings:
    """One external MCP server, as the plugin that holds it needs to reach it.

    **Two transports, and which one is a fact about the entry rather than a
    field.**  A `command` means a process we start and talk to over its standard
    input; a `url` means somebody else's process, reached over the network.  So
    :attr:`transport` is computed rather than stored, and an entry that names
    both — or neither — is refused at load time (`_tool_server`) rather than
    resolved by a precedence rule nobody would remember.

    `env` and `headers` are where a secret lives, and both go through
    :func:`resolve_secret`, so `${VAR}` resolves through the same chain as a
    provider key.  Nothing else here is secret: a `command` is a program name
    and an `args` list is on the command line of a process anyone can inspect.

    Not frozen-by-accident: this is read-only configuration, and the hub keeps
    its *state* — whether it is connected, what it last listed — in its own
    object beside it.  Config that a connection could write back into is how a
    running system stops matching the file it was started from.
    """

    #: The name it is configured under.  For an entry under `mcp:` it prefixes
    #: every tool that entry offers, so it is also the name a person reads in a
    #: tool call; for one of ours (`kind="plugin"`) it prefixes nothing — a
    #: plugin's tools are the model's under their own names, and this is the
    #: name `servers()` reports them by.  See `slife2.plugins.toolhub.server.model_name`.
    name: str
    #: Which kind of entry this is: `"mcp"` or `"rest"` for one somebody wrote
    #: under `mcp:`, and `"plugin"` for one of ours, which has no section of
    #: its own — `slife2.plugins.toolhub.server.plugin_settings` builds it from `servers:`.
    #:
    #: **It is load-bearing, and saying otherwise cost a reader a wrong answer.**
    #: A REST API entry has already been expanded into the stdio command it
    #: describes by the time one of these exists (see `_rest_api`), so it decides
    #: nothing about *running* — but a plugin's tools are the model's under their
    #: own names while everybody else's are prefixed by the entry's, and that
    #: branch is `slife2.plugins.toolhub.server.model_name` reading exactly this field.  Dropping
    #: it would rename every plugin tool (`turn_read` → `toolhub__turn_read`).
    kind: str = "mcp"
    #: What the server is for, in the operator's words.  Not the model's: the
    #: descriptions the model reads come from the server itself, tool by tool.
    #: This one is for whoever opens the config a year later and for `servers()`,
    #: which is where "why is that connected" gets answered.
    description: str = ""
    #: stdio: the program to start.
    command: str = ""
    args: tuple[str, ...] = ()
    #:
    #: `repr=False` on this and `headers`, and it is the one place in this
    #: module where a resolved secret is *held* rather than referenced: a
    #: `ProviderSettings` keeps `api_key_ref` and resolves on access, while a
    #: tool server's environment has to be materialised to be handed to a child
    #: process.  Everything that prints a config — a log line, a traceback, a
    #: REPL — goes through the generated `__repr__`, so this is what keeps a
    #: live key out of all three.
    env: dict[str, str] = field(default_factory=dict, repr=False)
    #: http: the endpoint, either transport.  A URL ending in `/sse` is the
    #: older SSE transport; anything else is Streamable HTTP.
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    #: stdio only, and it matters more than it looks: a server started with a
    #: relative path in its arguments resolves that path against its working
    #: directory, and a daemon's own working directory is the runtime folder.
    #: Empty means the data directory — where a person means `.` to point.
    cwd: str = ""
    #: `false` stays configured but is never connected, and by itself keeps the
    #: hub process from starting at all.  This is how a slow, paid or
    #: currently-broken server stays in the file without being in the way.
    enabled: bool = True
    #: Every tool this server offers starts **loaded** and is never evicted,
    #: where the default is that they are on demand — the model finds one with
    #: `tool_search` and puts it in its list with `func_tool_load`.  v1's flag,
    #: in v1's place and with v1's meaning, and the reason it is worth having: a
    #: server whose tools are wanted every turn should not cost a search and a
    #: load, and one with ninety tools should not cost a prompt.
    #:
    #: A flag rather than a load state for the same reason `enabled` is: the
    #: file says what the operator decided, and `load_status` — which the model
    #: decides — lives in the catalogue, where a reconcile can never overwrite
    #: it by accident.
    autoload: bool = False

    @property
    def transport(self) -> str:
        """`"http"` or `"stdio"` — read off which field is set."""
        return "http" if self.url else "stdio"


#: How many function tools the model's list may hold before the least recently
#: used are evicted.  v1's `tool_load: threshold: 100`, and the number is v1's:
#: it was arrived at by watching a real tool set, and nothing about slife2 is a
#: reason to differ.
DEFAULT_TOOL_LOAD = 100


@dataclass(frozen=True)
class ToolLoadSettings:
    """The `tool_load:` section — how many function tools the model may hold.

    **A cap, because the list goes out with every request.**  The tools the model
    has loaded are re-sent on every model call, so an unbounded loaded set is an
    unbounded prompt — and the thing that makes the bound bearable is that being
    evicted costs a `tool_search` and a `func_tool_load`, not a capability.

    Eviction never touches a tool of ours (a plugin's) or a server marked
    `autoload: true`: those are loaded because the operator said so, and a
    budget that could take `turn_read` away is the failure DESIGN.md §8 is
    about.  See `slife2.libs.store.catalogue.ToolStore.injectable`.
    """

    threshold: int = DEFAULT_TOOL_LOAD


#: How much of one tool result a model is handed.  **One number for both sides
#: of the arrangement**, and that is the whole of why it is a setting rather
#: than two: the harness spends it shortening a result too large for the
#: conversation, and a tool of ours that hands text back — `dump_read` today —
#: caps itself with it.  Two numbers would be two answers to "how much fits",
#: and the failure is a loop with this system's machinery at both ends: a reader
#: handing the harness's own shortening more than it shortened away.
#:
#: **The number is measured, and it moved once — down in effect, not up.**
#: Measured off this machine's turn log on 2026-10-16: **182 turns, 349 tool
#: results, median 48 tokens, p90 1,587, largest 5,284**.  Against the 4,000
#: this used to be, the mechanism fired on **7 results (2.0%)**; against 8,000 it
#: fires on none, and still catches the case it exists for — an output whose
#: length nobody controls (a shell command, a huge file read) riding in every
#: request the conversation makes afterwards.
#:
#: **The window sits between the ordinary result and the deliberately long one,
#: and that is what makes shortening a rare event** — which is what it should be,
#: because what it costs is out of proportion to what it saves.  A result over
#: the window pays a *discriminator call* — a model call carrying the whole
#: conversation — plus a dump file and, when the model wants the rest, a
#: `dump_read` round trip; what it buys is the lines the window did not keep,
#: which on this log is at most 1,284 tokens (5,284 − 4,000).  A model call to
#: save a kilobyte is a bad trade at any window size, and the ~1M-token windows
#: this system runs on (`slife2.yaml`'s `context_window`) make it worse rather
#: than better: the largest result this machine has produced is 0.5% of one.
#:
#: **Above the ordinary and below the deliberate.**  Every ordinary result here —
#: a file read, a command's `stdout`, a page of a listing — is under 6,000
#: tokens, and the case the mechanism exists for is the tool *bound* to be long
#: (the designer's own example is a ten-thousand-word essay, ~13,000 tokens).
#: 8,000 is above the first and below the second, so shortening fires on what
#: nobody controls rather than on what a model asked for once.
#:
#: What would move it again is the same instrument — the rows themselves, so
#: re-reading it is a query, which is the observation §8.5 makes about the tool
#: list's threshold.
DEFAULT_TOOL_RESULT_TOKENS = 8000

#: The smallest window that can be *read*, and it is measured rather than
#: chosen.  A window over a shortened result is built by
#: `slife2.libs.harness.dumps.window`, and the line that says where the whole of
#: it is — `[DUMP:{"call": "…", "total": N}]` — is what makes the rest reachable
#: at all.  Measured with the same estimator the harness spends, that address
#: alone costs 50 tokens for a short call id and 61 for the ~20-character ids
#: the providers actually emit, before a single line of the output is kept.
#: `dumps.window` now refuses to hand back a window whose address it had to cut
#: (an output nobody can name is one nobody can read back, so it fails the turn
#: rather than looking like an answer) — and a budget that can *only* fail is a
#: configuration that looks like a working one and answers nothing, which is the
#: same argument this section has always made about zero, applied to the number
#: the address actually costs.
MIN_TOOL_RESULT_TOKENS = 128


@dataclass(frozen=True)
class ToolResultSettings:
    """The `tool_result:` section — how much of one tool result a model is handed.

    **This is a bound on what enters a context, and not a judgement about what a
    tool returned.**  Whether an output is the right length is the tool
    designer's business; what the harness owns is the context that has to carry
    it and the whole of what was returned, and the two are answered separately —
    the whole is kept (a dump, or the file it already came from) and the model
    is handed a window of it, addressed by line, which it can read past.  There
    is no truncation and no summary anywhere in that arrangement, on purpose:
    both decide something about the content, and neither can be undone by the
    model that needs the part they dropped.

    There is deliberately no "off".  A window with no bound is the whole output,
    which is the thing this exists to prevent.
    """

    tokens: int = DEFAULT_TOOL_RESULT_TOKENS


@dataclass(frozen=True)
class CliToolSettings:
    """One external command the model may run, as its `cli:` entry describes it.

    Not a `ToolServerSettings`, and the difference is the whole of what this
    class is for.  There is no `url`, no `args`, no `env` and no `cwd`, because
    there is nothing to connect to: a CLI entry is a program already installed
    on this machine, named so that the model can be told it exists.

    **`command` is one string, and it may be more than one word.**  v1 allowed
    `python -m mytool` as readily as `gh`, and a port that quietly narrowed that
    to a single binary would refuse an entry the file it was copied from
    accepted.  How a caller turns it into a process is the caller's business —
    this class records what the operator wrote and nothing more.

    `install` and `source` are for a person rather than for a call.  The first
    is what to say when the command turns out not to be on `PATH`, which is the
    one failure this family is certain to meet; the second is where the entry
    came from, so that the answer to "why is this here" is written down beside
    it instead of remembered.  Nothing reads `source`.
    """

    #: The name it is configured under — what a person types, and what the tool
    #: built from this entry is called.
    name: str
    #: The invocation, resolved on `PATH`.  Required: an entry that names no
    #: program is a CLI nobody can run, and that is a config mistake rather than
    #: an entry that does nothing.
    command: str
    #: What it is for, in the operator's words.  This is the description the
    #: model reads, which is the one place in the config where that is true —
    #: a tool server's description is the operator's too, but its *tools'*
    #: descriptions come from the server itself.
    description: str = ""
    #: How to install it, shown when it is missing.
    install: str = ""
    #: Where it came from — `url`, `type`, `version`, and whatever else whoever
    #: wrote the entry thought worth noting.
    source: dict[str, str] = field(default_factory=dict)
    #: `false` keeps the entry written down and out of the model's hands.  The
    #: same switch, and the same meaning, as on a tool server.
    enabled: bool = True


@dataclass(frozen=True)
class SkillSettings:
    """What one skill is given, as its `skills:` entry says.

    **A skill has no process and can still have a credential.**  A playbook that
    says *run `scripts/search.py`* is worth nothing if the script dies on a
    missing `BAIDU_API_KEY`, and the skill's own header declares which names it
    needs (`slife2.plugins.skills.playbooks`).  This is the other half: where the value comes
    from.

    `env` resolves through the same chain as a provider key — shell, then
    credstore — and that is the whole point of having the section at all.  A
    daemon started days ago never saw the key somebody exported this morning,
    and `keyring:…` is not an environment variable in the first place; an entry
    here is what turns either into what a skill's script is handed.

    A skill with no entry is not misconfigured: the environment alone is a real
    answer, and a playbook that needs nothing is the common case.

    **`enabled` is here and not in the folder**, which is the one thing about a
    skill that the folder cannot say.  What is installed is a directory — that is
    the whole of `slife2.plugins.skills.playbooks`, and nothing has to be kept in step with it —
    but a skill somebody wants out of the model's way is a *decision*, and a
    decision belongs in the file the operator edits rather than in a name
    convention inside a directory.  It is the same switch, with the same meaning
    and the same spelling, as on a server and on a `cli:` entry.
    """

    #: **No `name` field**, unlike the other settings here: a skill's name is
    #: the key the section holds it under, and every reader already has it —
    #: a copy on the value was written by both writers and read by neither.
    env: dict[str, str] = field(default_factory=dict)
    #: `false` keeps the playbook installed and out of the catalogue.  Read by
    #: the plugin that owns the section, which declares the row `disabled`.
    enabled: bool = True


#: The four things a media provider can be asked for.  A closed set, and it is
#: v1's: each one names a *capability* rather than a model family, because that
#: is what a caller knows it wants — "make me a picture" — while which model
#: draws it is the config's business.
MEDIA_KINDS = ("image", "video", "tts", "asr")


@dataclass(frozen=True)
class MediaModelSettings:
    """One model in a `media:` provider's list.

    `kind` is what the model is *for*, and asking for the wrong kind is refused
    rather than tried: a video model handed an image prompt is a paid failure
    with a provider-side error message, and the config already said which one
    draws pictures.

    `params` is free-form and is the model's own defaults — a resolution, an
    aspect ratio, a duration — merged under whatever the call passes.  v1's
    shape, and it is what lets a config carry "this model always renders 720P"
    without the tool growing a field per provider.
    """

    model: str
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    voice: str = ""


@dataclass(frozen=True)
class MediaProviderSettings:
    """One `media:` provider: an endpoint, a key, and what it can do.

    **Not a `ProviderSettings` and not one of the `providers:`.**  A media
    endpoint speaks no chat protocol — it renders, synthesizes and transcribes —
    and its models are declared per *kind*, which nothing in `providers:` has.
    Putting one there would start a chat backend for a model no conversation can
    call.

    `api_key` is resolved lazily like every other key here, so credstore is
    consulted only when a call needs it.
    """

    name: str
    api: str
    base_url: str
    models: tuple[MediaModelSettings, ...] = ()
    api_key_ref: str = ""

    @property
    def api_key(self) -> str:
        """The resolved key.  Touches credstore, so call it where it is needed."""
        return resolve_secret(self.api_key_ref)

    def kinds(self) -> tuple[str, ...]:
        """What this provider can be asked for, in `MEDIA_KINDS` order."""
        found = {entry.kind for entry in self.models}
        return tuple(kind for kind in MEDIA_KINDS if kind in found)


@dataclass(frozen=True)
class MediaSettings:
    """Where images, video, speech and transcripts come from.  May be empty.

    **No `enabled` switch and no requirement**, unlike `embeddings:` — and the
    difference is what each one is *for*.  A turn cannot be stored without a
    vector, so an embeddings config that is absent is a system that cannot run;
    nothing here is on any other plugin's path, so a config with no `media:`
    section is a system where the model has four fewer tools and everything else
    works.  The tools refuse, and say what to add.
    """

    providers: dict[str, MediaProviderSettings] = field(default_factory=dict)
    #: kind -> `"provider/model"`, the model a call gets when it names none.
    defaults: dict[str, str] = field(default_factory=dict)

    def resolve(
        self, kind: str, reference: str = ""
    ) -> tuple[MediaProviderSettings, MediaModelSettings]:
        """The provider and model one request should use.

        v1's four steps, in its order: an explicit `provider/model`, then the
        bare model name across providers, then this section's `defaults[kind]`,
        then the first configured model of that kind.  Each step is a *weaker*
        statement than the one before it, so nothing here can override something
        the caller said.

        **The fourth step is also the fifth, and that is a registered
        fallback**: a `defaults[kind]` whose reference no longer resolves falls
        through to the first model of the kind rather than stopping the call.
        It is kept because the failure it prevents is a config that stops
        answering because one word went stale, and it is visible because the
        answer carries its own `provider/model`.  *Gives up:* the operator's
        `defaults:` intent for that kind.  *Does not cover:* a reference that
        names a model of the wrong kind or an unknown provider, which is refused
        here.  *Failure goes:* nowhere beyond the answer itself — flag for
        `DESIGN.md` §10.2.

        Raises:
            ConfigError: If nothing can answer — including the case of a
                reference that names a model of the wrong kind, which is
                refused here rather than at the provider, where it costs money.
        """
        if reference:
            return self._named(reference, kind)
        default = self.defaults.get(kind, "")
        if default:
            try:
                return self._named(default, kind)
            except WrongKindError:
                # ... but not *this* refusal.  A `defaults:` entry that resolves
                # to another kind is the operator's mistake, and this is where
                # the docstring says it is refused rather than at the provider:
                # swallowed here, it was replaced in silence by the first model
                # of the kind — a paid call to a model nobody had named.
                raise
            except ConfigError:
                # A stale `defaults:` entry falls through to the first model of
                # the kind rather than stopping the call — the registered
                # fallback the docstring states, and one the answer's own
                # `provider/model` makes visible (DESIGN.md §10.2).
                pass
        for provider in self.providers.values():
            for entry in provider.models:
                if entry.kind == kind:
                    return provider, entry
        raise ConfigError(
            f"No {kind} model configured. Add one with `kind: {kind}` under "
            f"`media:` in the config."
        )

    def _named(
        self, reference: str, kind: str
    ) -> tuple[MediaProviderSettings, MediaModelSettings]:
        """`provider/model`, or a bare model name found across providers."""
        name, _, wanted = reference.partition("/")
        if wanted:
            provider = self.providers.get(name)
            if provider is None:
                known = ", ".join(sorted(self.providers)) or "(none)"
                raise ConfigError(
                    f"Unknown media provider {name!r} in {reference!r}; "
                    f"configured: {known}"
                )
            for entry in provider.models:
                if entry.model == wanted:
                    return self._checked(provider, entry, kind, reference)
            known = ", ".join(entry.model for entry in provider.models) or "(none)"
            raise ConfigError(
                f"Unknown model {wanted!r} for provider {name!r}; it has: {known}"
            )
        for provider in self.providers.values():
            for entry in provider.models:
                if entry.model == name:
                    return self._checked(provider, entry, kind, reference)
        raise ConfigError(f"Unknown media model {reference!r}")

    @staticmethod
    def _checked(
        provider: MediaProviderSettings,
        entry: MediaModelSettings,
        kind: str,
        reference: str,
    ) -> tuple[MediaProviderSettings, MediaModelSettings]:
        if entry.kind != kind:
            raise WrongKindError(f"{reference!r} is a {entry.kind} model, not {kind}")
        return provider, entry


@dataclass(frozen=True)
class FileTunnelSettings:
    """How this machine's port becomes a public HTTPS URL, from `file-tunnel:`.

    A provider table like `providers:` or `embeddings:`, with the one thing
    that makes it unlike both: **its entries are options, not schemas.**  What
    a provider needs is a binary name, a host, a protocol — vendor words that
    belong to the tunnel rather than to this project, so they are kept as the
    mapping they were written as and read by the provider that understands
    them (`slife2.plugins.file_tunnel.providers`), which is the one place that knows
    ngrok from cloudflared.  A typed field per option would put three vendors'
    vocabulary in this file and change it every time a tunnel learns a flag.

    **An unknown `active:` falls back rather than raising**, which is §1.6's
    registered exception and the reason a tunnel is allowed it (it is no longer
    shared with `embeddings.active_model`, which now refuses an undefined
    name): a tunnel is a *subordinate* dependency, and a name this build does
    not have must not be able to keep the plugin from loading.  What a
    malformed *shape* does is the opposite — the loader refuses that, like
    every other section, because a `providers:` that is not a mapping is a
    mistake somebody has to be told about rather than a vendor this build
    lacks.
    """

    #: Which provider is live.  The others stay configured and inert.
    #:
    #: Spelled out rather than read off `slife2.plugins.file_tunnel.providers`, by the
    #: same rule `AGENT_SERVER_NAME` states above: reading it means importing
    #: it, and this module is imported by the TUI and the agent loop, neither
    #: of which should pull in a tunnel's SDK or `subprocess`.  `DEFAULT_PROVIDER`
    #: is the other end of this fact and `tests/test_config.py` pins the two
    #: together.
    active: str = "ngrok"
    #: provider name -> its own options, resolved but otherwise untouched.
    providers: dict[str, dict] = field(default_factory=dict)

    def options_for(self, name: str) -> dict:
        """*name*'s options — ``{}`` when it has no entry.

        Empty rather than an error, because every provider has to work from
        its own defaults: that is what makes ``ngrok: {}`` a complete entry,
        and an `active:` naming a provider nobody configured a working one.
        """
        found = self.providers.get(name)
        return dict(found) if isinstance(found, dict) else {}


@dataclass(frozen=True)
class A2ASettings:
    """The `a2a:` section: where the mesh's broker is, and whether there is one.

    **An absent section is the whole of the off switch**, which is v1's rule and
    the reason there is no `enabled:` key here — a section that carried one would
    have two ways of saying the same thing and a way of contradicting itself.
    v1 went further and made the section's *presence* merely permission to
    **probe** the broker, deciding enablement at runtime on whether anything
    answered; that half does not port, because it existed to decide whether to
    spawn a per-agent plugin process and there is no spawn to gate here.  The
    plugin always starts (§4: every plugin the config names is started and
    shared) and what the section decides is whether it opens a mesh at all.

    **Mosquitto is a hard constraint and not a default.**  v1 carried a
    `transport:` field with an http skeleton behind it and answered a value it
    did not implement by disabling the feature with a warning at config load —
    which is the shape this project refuses: a config naming a transport that
    does not exist is a mistake in a file a person edits, and a warning on the
    way past it means the model is offered `a2a_*` tools for a mesh that was
    silently switched off.  So the field is a `provider:` naming the broker
    implementation, exactly one value is accepted, and anything else is a
    `ConfigError` at load.  Adding a provider is adding a branch to
    `_a2a` and a driver to `slife2/a2a/`; it is not a config value.
    """

    #: The broker implementation.  `mosquitto` is the only one this build has.
    provider: str = "mosquitto"
    #: Whether the operator wrote the section.  Not a field in the file — it is
    #: what "the section was there" means, and it is what the plugin reads to
    #: decide whether this machine is on the mesh (`a2a_dbs` is the other half,
    #: and it says which *agents* are).
    configured: bool = False
    #: Where the broker is.  **The operator's to run, unless it is this
    #: machine's own and nothing is there**: `slife2.plugins.a2a.broker` probes the
    #: address and, for a *local* one that is absent, starts and supervises a
    #: `mosquitto` — v1's rule was to dial and never spawn, and this build
    #: starts one because a mesh with no broker is a feature that silently does
    #: nothing.  A remote address is still only ever dialled.
    broker_host: str = "localhost"
    broker_port: int = 1883
    #: The first two segments of the topic namespace,
    #: `$a2a/v1/{discovery|request|reply|event}/{org}/{unit}/{agent_id}`.  Two
    #: brokers, or two groups on one broker, are told apart by these, which is
    #: why they are configuration rather than constants.
    org: str = "default"
    unit: str = "default"

    #: Whether the address above came from the command line rather than from the
    #: file (`--a2a-broker`).  Read by the launcher, which is the only party that
    #: can tell whether the flag was honoured: a mesh belongs to a *process*, so
    #: a server this command did not start is on whatever broker it was started
    #: with, and a client that asked for another one has to be told rather than
    #: left believing it (`slife2.libs.host.launcher`).
    overridden: bool = False

    @property
    def address(self) -> str:
        """`host:port`, as every log line and refusal names it."""
        return f"{self.broker_host}:{self.broker_port}"


def add_a2a_broker_argument(parser: argparse.ArgumentParser) -> None:
    """Add `--a2a-broker`, which is the flag `a2a.broker` is written as.

    A flag rather than a config edit because the useful case is somebody else's
    broker — a team's, a machine's, one already running — and pointing at one for
    a session should not mean editing a file that every other session reads.
    """
    parser.add_argument(
        "--a2a-broker",
        default=None,
        metavar="HOST:PORT",
        help=(
            "use this broker for the mesh instead of the one in the config — "
            "somebody else's, or one `slife2` should not start itself"
        ),
    )


def apply_a2a_broker(args: argparse.Namespace) -> None:
    """Honour `--a2a-broker` by putting it in the environment.

    The same mechanism `--data-dir` uses, for the same reason and with one more:
    the servers a process starts must read the same address, and the *broker* is
    the plugin's connection rather than a client's, so a value carried in an
    argument would have to be handed to a process this one may not be starting.
    """
    if getattr(args, "a2a_broker", None):
        os.environ[A2A_BROKER_ENV_VAR] = args.a2a_broker


def broker_address(text: str) -> tuple[str, int]:
    """`host:port` as a broker's host and port, or a `ConfigError` saying why not.

    Split from the **right**, because a host may be an IPv6 address and those are
    full of colons: `::1:1883` is `::1` on 1883, and splitting from the left
    would read it as a host called `::1:1883`.  What is *not* checked here is the
    address itself — a wildcard, or a name that is not this machine, is
    `slife2.plugins.a2a.broker`'s question, and it answers it once for every way an
    address can be written down.
    """
    host, _, port = text.rpartition(":")
    if not host or not port:
        raise ConfigError(
            f"--a2a-broker {text!r} is not `host:port` — write it as "
            f"`127.0.0.1:1883` or `broker.example:1883`"
        )
    try:
        number = int(port)
    except ValueError:
        number = 0
    if not 0 < number < 65536:
        raise ConfigError(
            f"--a2a-broker {text!r}: {port!r} is not a port number (1-65535)"
        )
    return host, number


@dataclass(frozen=True)
class Config:
    """Every section of one config file, already defaulted."""

    #: name -> where it listens, keyed by `"agent"` and by each `api` in use.
    servers: dict[str, ServerSettings] = field(default_factory=dict)
    providers: dict[str, ProviderSettings] = field(default_factory=dict)
    #: Other people's MCP servers, from `mcp:`.  Held by `slife2-mcp-tools`,
    #: which declares each entry to the hub as a source of its own.
    mcp: dict[str, ToolServerSettings] = field(default_factory=dict)

    #: REST APIs, from `rest-api:`, already expanded into the stdio command that
    #: serves each one.  A section of its own and a mapping of its own, read by
    #: `slife2-restapi-tools` — the two are one *mechanism*, which is why the
    #: entries look identical here, but they are two places an operator writes
    #: down a server, and a plugin reads the section it owns.
    rest_apis: dict[str, ToolServerSettings] = field(default_factory=dict)
    #: The programs already on this machine that the model may be told about,
    #: from `cli:`.  A third kind of source beside the plugins and the tool
    #: servers, and the only one with no connection in it at all.
    cli: dict[str, CliToolSettings] = field(default_factory=dict)
    #: The playbooks in `<data>/skills/` that need something supplied to them,
    #: from `skills:`.  Keyed by the skill's name; an absent entry means the
    #: ambient environment is the whole of what that skill is given.
    skills: dict[str, SkillSettings] = field(default_factory=dict)
    agent: AgentSettings = field(default_factory=AgentSettings)
    #: How many background tasks one agent may run at once, from `agent-task:`.
    #: Read by `slife2-agent-task`, which owns that section — and the *only*
    #: thing it owns, since a task's prompt and step count are the agent's and
    #: come out of `agent:` above (see `AgentTaskSettings`).
    agent_task: AgentTaskSettings = field(default_factory=AgentTaskSettings)
    #: How many subagents one agent may have, from `subagent:`.  Read by
    #: `slife2-agent`, which owns the two tools that make and unmake one — there
    #: is no plugin for this, because a subagent is not a process (sdd §3.5).
    subagent: SubagentSettings = field(default_factory=SubagentSettings)
    #: How a conversation's context is decided, from `context:`.  Read by
    #: `slife2-context`, which is the plugin that owns the turn log.
    context: ContextSettings = field(default_factory=ContextSettings)
    #: How many function tools the model's list may hold.  Read by the toolhub,
    #: which owns the catalogue and therefore the budget: the count it bounds is
    #: a `SELECT COUNT(*)` over its own rows.
    tool_load: ToolLoadSettings = field(default_factory=ToolLoadSettings)
    #: How much of one tool result a model is handed.  Read by whoever hands
    #: text back (the cabinet's `dump_read`) and spent by the harness when it
    #: shortens a result too large for the conversation — one number, because
    #: two would let a reader return more than the shortening removed.
    tool_result: ToolResultSettings = field(default_factory=ToolResultSettings)
    #: The endpoints vectors come from, and which one is in use.  Its own
    #: section rather than entries under `providers:`, for the reason
    #: `EmbeddingProviderSettings` gives.
    embeddings: EmbeddingsSettings = field(default_factory=EmbeddingsSettings)
    #: Where generated media come from, from `media:`.  Read by `slife2-media`,
    #: which is the plugin that owns the section and the only one that holds a
    #: rendering provider's key.
    media: MediaSettings = field(default_factory=MediaSettings)
    #: How this machine's port becomes a public HTTPS URL, from `file-tunnel:`.
    #: Read by `slife2-file-tunnel`, which holds the tunnel — somebody else's
    #: process, or an SDK holding an account, which is why it is a plugin.
    file_tunnel: FileTunnelSettings = field(default_factory=FileTunnelSettings)
    #: Where the A2A mesh's broker is, from `a2a:`.  Read by `slife2-a2a`, which
    #: holds the connections — and the one section whose *presence* is a
    #: setting, since a machine with no section is a machine not on the mesh.
    #: Where the **remote** network's broker is, from `a2a:`.  A network of
    #: agents that may be on other machines — which is what makes the protocol
    #: the point here (`Net`).
    a2a: A2ASettings = field(default_factory=A2ASettings)
    #: `"provider/model"`, the model the agent starts with.
    default: str = ""

    def provider(self, name: str) -> ProviderSettings:
        try:
            return self.providers[name]
        except KeyError:
            known = ", ".join(sorted(self.providers)) or "(none)"
            raise ConfigError(f"unknown provider {name!r}; known: {known}") from None

    def resolve(
        self, reference: str = ""
    ) -> tuple[str, ProviderSettings, ModelSettings]:
        """Split a `provider/model` reference, defaulting to `default`.

        A bare provider name is accepted and means that provider's first model,
        because someone typing `--model deepseek` means the obvious thing.

        Raises:
            ConfigError: If the reference is malformed or names something that
                does not exist.
        """
        reference = reference or self.default
        if not reference:
            raise ConfigError("no model configured; set `default: provider/model`")

        name, _, model_id = reference.partition("/")
        provider = self.provider(name)
        return name, provider, provider.model(model_id)

    def server(self, name: str) -> ServerSettings:
        """Where a named server listens.

        Raises:
            ConfigError: If the name is unknown — which means a caller asked for
                a server this config does not bring up.
        """
        try:
            return self.servers[name]
        except KeyError:
            known = ", ".join(sorted(self.servers)) or "(none)"
            raise ConfigError(f"unknown server {name!r}; known: {known}") from None

    def url_for(self, reference: str = "") -> str:
        """The endpoint serving a model reference.

        The *family's* server, not the provider's: a provider has no address of
        its own, because one process speaks one wire format for all of them.
        """
        _, provider, _ = self.resolve(reference)
        return self.server(provider.api).url

    def plugins(self) -> list[str]:
        """Every server slife2 itself brings up, in the order it must start.

        The model backends first, then the rest, because a model server
        answering first is what makes the first turn work rather than fail and
        retry.

        One statement of what this system runs, read by the launcher that starts
        them and by the toolhub that asks each one what tools it offers.  The
        hub has two sources — our own plugins, and the external MCP and REST
        servers under `mcp:` — and this is the first of them: derived here
        rather than written down beside the hub, because a list of "which of
        ours" kept in a second place is a list that disagrees with the
        launcher's.
        """
        return [*self.apis_in_use(), *LOCAL_SERVERS]

    def apis_in_use(self) -> list[str]:
        """The wire protocols some provider uses, in a stable order.

        This is what the launcher starts: a family with no providers has nothing
        to serve, and starting it would be a process holding nobody's key.
        """
        used = {p.api for p in self.providers.values()}
        return [api for api in API_BACKENDS if api in used]

    # **There is no `cli_tools()` here, and there is no `tools()` beside it**,
    # for the reason the `mcp:` section's accessor went the same way: `enabled`
    # is one rule, and a filtered view of a section is a second spelling of it
    # kept by whoever does not read the section.  The reader is the plugin that
    # owns the section — `slife2-cli`, `slife2-mcp-tools` — and a switched-off
    # entry is a row it declares with `status: disabled`, which is not the same
    # as an entry nobody wrote down.


def default_config() -> Config:
    """The config used when no file is found.

    Deliberately a single OpenAI-compatible provider: it is the shape the
    project is developed against, and a second provider nobody has credentials
    for would only make `slife2 status` report a server that can never start.
    """
    deepseek = ProviderSettings(
        api="openai-completions",
        base_url="https://api.deepseek.com",
        api_key_ref="${DEEPSEEK_API_KEY}",
        models={
            "deepseek-flash": ModelSettings(
                model="deepseek-flash",
                name="DeepSeek Flash",
                reasoning=True,
                input=("text", "image"),
                context_window=1_000_000,
                max_tokens=384_000,
                temperature=0.7,
                top_p=1.0,
            )
        },
    )
    return Config(
        # Every server this system starts, from the one table that describes
        # them (`PLUGINS`): the key it is filed under, the port it listens on
        # when this file names none, and whether it is a model backend — which
        # is a fact about *it* rather than about this config and so is not
        # repeated here.  A `servers:` block still overrides any of it, which is
        # what the shipped `slife2.yaml` does.
        servers={one.key: ServerSettings(port=one.port) for one in PLUGINS},
        providers={"deepseek": deepseek},
        mcp={},
        agent=AgentSettings(),
        embeddings=EmbeddingsSettings(
            providers={
                # The default points at a *local* embedder, which is the one
                # endpoint that can be reached without a credential and the
                # reason the shipped config can run at all.  `bge-m3` is
                # multilingual, which is what a history mixing Chinese and
                # English needs; its width is discovered, never configured.
                "local": EmbeddingProviderSettings(
                    name="local",
                    base_url="http://127.0.0.1:17347/v1",
                    model="bge-m3",
                    api_key_ref="local",
                )
            },
            active="local",
        ),
        default="deepseek/deepseek-flash",
    )


def find_config_path(explicit: str | Path | None = None) -> Path | None:
    """The config file, which lives *inside* the data directory.

    The directory is the knob — `--data-dir`, or `SLIFE2_DATA_DIR` — because
    everything slife2 keeps belongs to one folder: the config that says what to
    run and the agents whose data it describes.  Naming the config separately
    would mean two answers to "where is this installation?", and the data
    directory would still have to be found another way.

    **The home's while a call is being served, and this process's otherwise**
    (`slife2.libs.host.paths.home`), which is the difference between the two readers
    of it.  A server loads its own config at start-up and runs on it — that is
    its identity, and no call changes it — while the tools that *edit* a config
    (`model_switch`, the `mcp_*` and `rest-api_*` families) are editing the
    caller's file: a window in one home changing the model must not rewrite the
    config another home is running on.  Start-up and the launcher are outside
    any call, so both get this process's own directory, which is all they ever
    wanted.
    """
    if explicit is not None:
        return Path(explicit)
    candidate = home() / DEFAULT_CONFIG_NAME
    return candidate if candidate.is_file() else None


def load(explicit: str | Path | None = None) -> Config:
    """Load the config, falling back to :func:`default_config`.

    A named path that does not exist *is* an error (the user asked for that
    file); an unnamed one that does not exist is not.
    """
    path = find_config_path(explicit)

    if path is None or not path.is_file():
        if explicit is not None:
            raise ConfigError(f"config file not found: {path}")
        # No config is not an error: the defaults are a working single-provider
        # setup, and a fresh data directory has nothing in it yet.
        return default_config()

    yaml = YAML(typ="safe")
    try:
        raw = yaml.load(path) or {}
    except (YAMLError, OSError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    return _build(raw, config_dir=path.parent)


#: What each file looked like when it was last read: by path, `((mtime_ns, size),
#: config)`.
#:
#: **One entry per file, and not one slot, because a process serves more than one
#: home.**  `find_config_path` resolves in the caller's home, so a daemon shared
#: by two data directories reads two `slife2.yaml` — and one slot would thrash
#: between them.  Thrashing is not merely wasteful here: the whole staleness test
#: a reader makes is `config is not self._config`, so a cache that answered with
#: a fresh object on every alternation would look to `LiveProviders`, and to
#: every conversation, like a config that is edited between calls — rebuilding the
#: provider table, its cached SDK clients, and the model a new conversation starts
#: on, every single turn.
#:
#: A `None` key is the *no file at all* state, which is not per home: two homes
#: with no config have the same built-in defaults, and they are one object because
#: they are one answer.
_latest: dict[Path | None, tuple[tuple[int, int] | None, Config]] = {}


def load_cached(explicit: str | Path | None = None) -> Config:
    """`load`, re-reading the file only when it changed since the last call.

    **Identity is the staleness test, and that is the whole point.**  While the
    file is untouched this hands back the *same* `Config` object, so a reader
    asks "did an edit happen" with one `is` comparison — no watcher, no version
    counter, no lock.  `slife2.libs.host.configfile` swaps the file atomically and never
    writes identical bytes, so both `st_mtime_ns` and `st_size` move on a real
    edit and neither does on a no-op; a local edit by hand is picked up the same
    way, which is the point of caching on the file rather than on a call.

    A stat on the common path and a parse only when something changed.  A
    missing file is the built-in defaults, exactly as in `load` — and **the same
    object**, which is the half that used to be missing: a config-less install
    was handed a fresh `Config` per call, so `LiveProviders`, whose whole
    staleness test is `config is not self._config`, rebuilt the provider table
    and every cached SDK client on every model call.  That is the state a fresh
    checkout starts in, so it was the common case rather than a corner.

    A file that is there but cannot be *stat'd* is neither: it raises, naming
    the path, rather than being read as "changed" and quietly answered with a
    fresh object (DESIGN.md §10.1).

    **And the answer follows the caller's home, like every other read a shared
    plugin makes.**  `find_config_path` resolves in `slife2.libs.host.paths.home`,
    so a daemon serving two data directories reads two `slife2.yaml` — each
    cached on its own file, which is what keeps "has this config changed?" the
    one `is` comparison its readers rely on rather than a question that a second
    home's traffic keeps answering "yes" to.
    """
    global _latest
    path = find_config_path(explicit)
    if path is None or not path.is_file():
        if explicit is not None:
            # **A *named* file that is not there is an error here too.**  The
            # cache slot below is the "this installation has no config at all"
            # state, and it is keyed on nothing but `None`: once a config-less
            # start had filled it, `load_cached("/tmp/typo.yaml")` answered with
            # the built-in defaults while `load("/tmp/typo.yaml")` raised
            # `config file not found`.  A caller that named a file is asking
            # about *that* file, and answering with a different configuration is
            # the silent fallback `load` refuses (DESIGN.md §10.1).
            return load(explicit)
        # The `None` key is the "there was no file" state, so a file appearing
        # afterwards takes the reload path below rather than being missed.
        cached = _latest.get(None)
        if cached is not None:
            return cached[1]
        config = load(explicit)
        _latest[None] = (None, config)
        return config
    try:
        stat = path.stat()
    except OSError as exc:
        # A stat that failed is not a file that changed.  Reading it as one
        # would hand a fresh `Config` to a caller whose whole staleness test is
        # object identity, so the edit would look like it never happened
        # (DESIGN.md §10.1).  `load` reports an unreadable file the same way.
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _latest.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    config = load(path)
    _latest[path] = (stamp, config)
    return config


def for_the_call(source: Callable[[], Config] | None, injected: Config) -> Config:
    """The config the call in front of us is served from.

    **A shared server reads the caller's home's file; a server that was handed a
    config keeps it.**  Those are the only two cases and they are one line apart,
    which is why this is a function rather than each server deciding for itself:
    what a process serves is *the world's* configuration (`slife2.libs.host.paths.home`),
    and the one thing that outranks it is a config somebody injected — every test
    in this suite builds its server with one, and a server that went looking for
    a file instead would be a test that reads a developer's own `slife2.yaml`.

    `source` is `load_cached` in production, which is what makes an edit to the
    file — by a model or by hand — reach the *next* call without a restart.  It
    is a callable rather than a flag so that the reader is resolved at the moment
    of use: `load_cached` answers per home (`slife2.libs.host.paths.home`), so the
    same `source` serves every home this process is asked about.
    """
    return injected if source is None else source()


def _mapping(raw: Any, where: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping")
    return raw


def _build(raw: dict[str, Any], config_dir: Path | None = None) -> Config:
    """Assemble a Config from a parsed mapping, defaulting what is absent.

    `config_dir` is where a relative `system_prompt` is looked for, so a
    template can sit next to the config that names it.
    """
    base = default_config()

    servers = dict(base.servers)
    for name, spec in _mapping(raw.get("servers"), "servers").items():
        if name not in LOCAL_SERVERS and name not in API_BACKENDS:
            raise ConfigError(
                f"servers.{name}: not a server this system runs "
                f"(known: {', '.join(LOCAL_SERVERS)}, {', '.join(API_BACKENDS)})"
            )
        servers[str(name)] = _server(
            spec, servers.get(str(name), ServerSettings()), f"servers.{name}"
        )

    # `tui:` used to be a section of its own, and a config written before that
    # changed would still parse — every other key in it is optional, so the
    # section would simply be ignored and a port somebody chose would quietly
    # revert to 8005.  Refused rather than migrated, because the fix is one
    # indentation and the failure is silent either way.
    if raw.get("tui") is not None:
        raise ConfigError(
            "tui: is a `servers:` entry now, not a section of its own; "
            "move it under `servers:` as `servers.tui`"
        )

    # `tools:` is `mcp:` now, and the same argument applies with more force: an
    # unknown top-level key is simply not read, so a config that still says
    # `tools:` would load with every one of those servers gone and nothing said.
    # The section is named for the catalogue category its rows are declared
    # under, and that category is `mcp`.
    if raw.get("tools") is not None:
        raise ConfigError(
            "tools: is `mcp:` now — the section is named for the catalogue "
            "category its rows are declared under; rename the key"
        )

    # **An absent section is the built-in provider; an *empty* one is a
    # mistake.**  `providers: {}` used to fall through the same `if not raw`
    # test as no section at all, so a config that named no provider quietly ran
    # on the built-in one and called api.deepseek.com — with the operator's file
    # saying, in the section they had just edited, that there was no such
    # provider.  `embeddings.providers` already draws the line this way, and the
    # section is the same shape.
    if raw.get("providers") is None:
        providers = dict(base.providers)
    else:
        providers_raw = _mapping(raw.get("providers"), "providers")
        if not providers_raw:
            raise ConfigError(
                "providers: needs at least one provider — leave the section out "
                "for the built-in one"
            )
        providers = {
            str(name): _provider(spec, str(name))
            for name, spec in providers_raw.items()
        }

    agent_raw = _mapping(raw.get("agent"), "agent")
    max_steps = _int_or(
        agent_raw.get("max_steps"), base.agent.max_steps, "agent.max_steps"
    )
    if max_steps < 0:
        # Refused rather than read, and refused for the reason `port: 0` is: a
        # negative count is not a number of anything.  Nothing this value could
        # be read as is the answer to a typo — and the one direction it would be
        # read in, "no cap", is the setting that never stops, so a config that
        # missed a keystroke would get the most expensive meaning of the three.
        raise ConfigError(
            f"agent.max_steps: {max_steps} is not a step count; leave the key "
            f"out or write 0 for no cap"
        )
    agent = AgentSettings(
        server=servers["agent"],
        max_steps=max_steps,
        system_prompt=_template_path(
            agent_raw.get("system_prompt"), config_dir, base.agent.system_prompt
        ),
    )

    # **A name in both sections is refused rather than resolved**, and this is
    # the only place both are known: each is read by the plugin that owns it, so
    # neither can check the other.  Both would become one entry called
    # `{name}__{tool}` in the model's tool list, so which of the two won would be
    # invisible at every point a person could look.
    mcp = _mcp_servers(raw)
    rest_apis = _rest_servers(raw)
    for name in rest_apis:
        if name in mcp:
            raise ConfigError(
                f"rest-api.{name}: already configured under `mcp:`; the two "
                f"would collide as tool names"
            )

    return Config(
        servers=servers,
        providers=providers,
        mcp=mcp,
        rest_apis=rest_apis,
        cli=_cli_tools(raw),
        skills=_skills(raw),
        agent=agent,
        agent_task=_agent_task(raw.get("agent-task"), base.agent_task),
        subagent=_subagent(raw.get("subagent"), base.subagent),
        context=_context(raw.get("context"), base.context),
        tool_load=_tool_load(raw.get("tool_load"), base.tool_load),
        tool_result=_tool_result(raw.get("tool_result"), base.tool_result),
        embeddings=_embeddings(raw.get("embeddings"), base.embeddings),
        media=_media(raw.get("media"), base.media),
        file_tunnel=_file_tunnel(raw.get("file-tunnel"), base.file_tunnel),
        a2a=_a2a(
            raw.get(A2A_KEY), base.a2a, section=A2A_KEY, override=A2A_BROKER_ENV_VAR
        ),
        default=str(raw.get("default") or _first_reference(providers)),
    )


def _a2a(
    raw: Any,
    base: A2ASettings,
    *,
    section: str = A2A_KEY,
    override: str = "",
) -> A2ASettings:
    """One `a2a:`-shaped section, defaulted — and the one place `configured` is set.

    **`section` because there are two of them** (§3.5): the remote network's
    `a2a:` and the local one's `a2a-subagent:`, parsed by the same rules and told
    apart by the name every refusal is written with — a `provider:` mistake in
    the local one must not be reported against a key the file does not have.

    **`override` names an environment variable only the remote one is given.**
    The local network's broker is this machine's own and is the file's to say
    (`--a2a-broker` names somebody else's, and a subagent has no business on
    one); passing it for the remote network and not for the local one is the
    whole of that rule.

    **Absent means off**, so the built-in answer comes back with
    `configured=False` and the plugin opens no mesh.  The shape is v1's —
    `broker:` nested with `host` and `port` under it — so a `slife.yaml` copied
    over keeps working and keeps meaning the same thing.

    **An unknown `provider:` is refused rather than downgraded.**  This is the
    section's whole difference from `file_tunnel.active`, the one remaining
    section that falls back (§1.6): that names a *subordinate* dependency this
    build may not have, where falling back leaves a working system; this names
    the transport of the feature, where falling back would leave `a2a_*` tools
    offered to a model for a mesh that is not running.  v1 answered that case
    with a warning and `enabled = False`, which is the silence-with-tools
    outcome, so it does not port.

    **`--a2a-broker` is applied here, after the file**, so the flag wins over the
    section it is an override of and everything downstream sees one answer.  It
    cannot *make* a mesh where the section was left out: the section's presence is
    the switch, and a flag that turned it on would be a second way of saying the
    same thing — so on a machine with no `a2a:` the flag is refused, by name,
    rather than answered with a mesh nobody asked the file for.
    """
    # **Read through the parameter, which is the whole of who gets it.**  This
    # line used to name `A2A_BROKER_ENV_VAR` itself, so the parameter above was
    # dead and `--a2a-broker` reached *both* networks: the local mesh was dialled
    # at somebody else's broker under its per-maker namespace, and because
    # `slife2.plugins.a2a.broker.ensure` never starts a broker that is not this
    # machine's, the local one was no longer brought up either.  A subagent has
    # no business on anybody else's broker — the docstring above is the rule, and
    # this is the one line that has to obey it.
    override = os.environ.get(override, "").strip() if override else ""
    if raw is None and override:
        raise ConfigError(
            f"--a2a-broker {override!r} names a broker to use, but this machine "
            f"is not on the mesh: there is no `a2a:` section, and that section "
            f"is what being on one means"
        )
    if raw is None:
        return base
    found = _mapping(raw, section)

    provider = str(found.get("provider") or base.provider).strip()
    if provider != base.provider:
        raise ConfigError(
            f"{section}.provider: {provider!r} is not a broker this build speaks "
            f"to (the only one is {base.provider!r}); leave the key out for "
            f"{base.provider!r}, or leave the section out to stay off the mesh"
        )

    # `broker:` is a mapping or nothing.  A `broker: localhost:1883` written as a
    # scalar is the mistake the nesting invites, and it deserves the section's
    # name rather than a `str` object's `get` failing somewhere further down.
    broker = found.get("broker")
    if broker is None:
        broker = {}
    broker = _mapping(broker, f"{section}.broker")

    host = str(broker.get("host") or base.broker_host)
    port = _int_or(broker.get("port"), base.broker_port, f"{section}.broker.port")
    if not 0 < port < 65536:
        # **The flag refused this and the file did not.**  `broker_address`
        # refuses `host:0` and `host:70000` for `--a2a-broker`, and the section
        # that overrides is written in the same shape but was read straight
        # through, so `broker: {port: 0}` loaded and the mesh dialled
        # `localhost:0` at every start.  Zero is the value that costs the most
        # and reads the least like a mistake: the port is not the kernel's to
        # choose here (nobody binds this end), so it is a connection to a port
        # no process can hold — the dial hangs until its timeout rather than
        # failing on the line that named it.
        raise ConfigError(
            f"{section}.broker.port: {port} is not a port (1-65535); leave it "
            f"out for the default ({base.broker_port})"
        )
    if override:
        host, port = broker_address(override)

    return A2ASettings(
        provider=provider,
        configured=True,
        broker_host=host,
        broker_port=port,
        org=str(found.get("org") or base.org),
        unit=str(found.get("unit") or base.unit),
        overridden=bool(override),
    )


def _agent_task(raw: Any, base: AgentTaskSettings) -> AgentTaskSettings:
    """The `agent-task:` section, defaulted — one number, and it has to be real.

    Zero is refused rather than accepted as "no tasks": a cap of zero is a tool
    the model can see, can call, and can never be answered by, which is a
    configuration nobody wants and a failure that would arrive as a refusal
    rather than as the config mistake it is.
    """
    if raw is None:
        return base
    section = _mapping(raw, "agent-task")
    max_running = _int_or(
        section.get("max_running"), base.max_running, "agent-task.max_running"
    )
    if max_running < 1:
        raise ConfigError(
            f"agent-task.max_running: {max_running} would refuse every task the "
            f"model starts — set at least 1, or leave the section out for "
            f"{base.max_running}"
        )
    return AgentTaskSettings(max_running=max_running)


def _subagent(raw: Any, base: SubagentSettings) -> SubagentSettings:
    """The `subagent:` section, defaulted — a cap and a network.

    Zero is refused, and `_agent_task` says why in the words this borrows: a cap
    of zero is a tool the model can see, can call, and can never be answered by.

    **The network comes from the same mapping, through `_a2a`.**  A section
    holding a network parses its `broker`/`org`/`unit` the one way those keys are
    parsed anywhere, so a `subagent:` that names a broker means what an `a2a:`
    naming one means — and the two readers this section has (the agent server for
    the cap, the local network's plugin for the address) are `AgentTaskSettings`'
    arrangement one section over, which is where that exception is argued.
    """
    if raw is None:
        return base
    section = _mapping(raw, "subagent")
    max_running = _int_or(
        section.get("max_running"), base.max_running, "subagent.max_running"
    )
    if max_running < 1:
        raise ConfigError(
            f"subagent.max_running: {max_running} would refuse every subagent "
            f"the model names — set at least 1, or leave the section out for "
            f"{base.max_running}"
        )
    return SubagentSettings(
        max_running=max_running,
        network=_a2a(raw, base.network, section="subagent"),
    )


def _tool_load(raw: Any, base: ToolLoadSettings) -> ToolLoadSettings:
    """The `tool_load:` section, defaulted.

    Absent means v1's default rather than an error, the same rule every other
    section follows.  A present one has to hold a positive number: a threshold of
    zero is a tool list that is always empty, which is not a configuration to
    accept quietly — the model would have `tool_search` and no way to keep what
    it found.
    """
    if raw is None:
        return base
    section = _mapping(raw, "tool_load")
    threshold = _int_or(section.get("threshold"), base.threshold, "tool_load.threshold")
    if threshold < 1:
        raise ConfigError(
            f"tool_load.threshold: {threshold} would evict every tool the model "
            f"loads, leaving it a search it cannot keep"
        )
    return ToolLoadSettings(threshold=threshold)


def _tool_result(raw: Any, base: ToolResultSettings) -> ToolResultSettings:
    """The `tool_result:` section, defaulted.

    Absent means the default rather than an error, the rule every other section
    follows.  A present one has to hold a positive number: a budget of zero is a
    reader that can return no line at all and a shortening that can keep none,
    and the model would be left with a marker naming a file it is not allowed to
    read — a configuration that looks like a working one and answers nothing.
    """
    if raw is None:
        return base
    section = _mapping(raw, "tool_result")
    tokens = _int_or(section.get("tokens"), base.tokens, "tool_result.tokens")
    if tokens < MIN_TOOL_RESULT_TOKENS:
        raise ConfigError(
            f"tool_result.tokens: {tokens} is smaller than the window's own "
            f"address — set at least {MIN_TOOL_RESULT_TOKENS} (the default is "
            f"{DEFAULT_TOOL_RESULT_TOKENS}), or leave the section out"
        )
    return ToolResultSettings(tokens=tokens)


def _media(raw: Any, base: MediaSettings) -> MediaSettings:
    """The `media:` section.  Absent is empty, and empty is a working system.

    **Refused rather than skipped**, which is the opposite of v1 and right for
    where the parsing happens.  v1's plugin read the section itself at call
    time, so a bad entry had to be skipped — refusing would have taken the whole
    plugin down over one provider.  Here the config is parsed once, at load,
    where this project's rule is that the loader is the judge: a `media:` entry
    naming no model is a typo worth refusing before anything is asked of a
    provider, and the file a person reads is the file that would have to be
    fixed either way.
    """
    if raw is None:
        return base
    section = _mapping(raw, "media")
    providers = {
        str(name): _media_provider(spec, str(name))
        for name, spec in _mapping(section.get("providers"), "media.providers").items()
    }
    defaults_raw = _mapping(section.get("defaults"), "media.defaults")
    defaults = {
        str(kind): str(reference)
        for kind, reference in defaults_raw.items()
        if str(reference)
    }
    for kind in defaults:
        if kind not in MEDIA_KINDS:
            raise ConfigError(
                f"media.defaults.{kind}: not a kind of media "
                f"(known: {', '.join(MEDIA_KINDS)})"
            )
    return MediaSettings(providers=providers, defaults=defaults)


def _file_tunnel(raw: Any, base: FileTunnelSettings) -> FileTunnelSettings:
    """The `file-tunnel:` section.

    One judgement call, and it is the whole of this function: **a malformed
    shape raises and a name this build does not have falls back.**

    The first is this module's rule everywhere — a `providers:` that is not a
    mapping, or an entry that is not one, is a mistake in a file a person
    edits, and every other section refuses it at load rather than at the call
    that finds it.  The second is the opposite, and it is v1's: a tunnel
    provider is a *subordinate* dependency, so an `active:` naming something
    this build cannot construct — a typo, a provider removed since the config
    was written, a config copied from another machine — degrades to the default
    with a warning instead of a plugin that will not load.  What makes that
    safe rather than silent is `tunnel_status`, which reports the provider
    actually in use.

    Imported inside the function because the providers module reaches
    `subprocess` and an SDK, and this module is on the import path of the TUI
    and of the agent loop.
    """
    from slife2.plugins.file_tunnel.providers import DEFAULT_PROVIDER, KNOWN_PROVIDERS

    if raw is None:
        return base
    section = _mapping(raw, "file-tunnel")
    providers: dict[str, dict] = {}
    for name, spec in _mapping(
        section.get("providers"), "file-tunnel.providers"
    ).items():
        if not isinstance(spec, dict):
            raise ConfigError(
                f"file-tunnel.providers.{name}: expected a mapping of that "
                f"provider's own options (`{name}: {{}}` is a complete entry)"
            )
        # Resolved but otherwise untouched: `${VAR}` here is a token or a
        # binary path, and the provider that understands the option is the one
        # that should see its value.  An unresolved reference is left verbatim
        # by `resolve_secret` and reported by that provider.
        providers[str(name)] = {
            str(key): resolve_secret(value) for key, value in spec.items()
        }

    active = str(section.get("active") or DEFAULT_PROVIDER).strip()
    if active not in KNOWN_PROVIDERS:
        logger.warning(
            "file_tunnel_active_unknown active=%s fallback=%s known=%s",
            active,
            DEFAULT_PROVIDER,
            sorted(KNOWN_PROVIDERS),
        )
        active = DEFAULT_PROVIDER

    return FileTunnelSettings(active=active, providers=providers)


def _needs_base_url(where: str, *, also: str = "") -> ConfigError:
    """The refusal for a provider that has a key and nowhere to send it.

    **One sentence, because it is one mistake.**  Every provider here — chat,
    embedding, media — is a base URL and a key, and a key with no URL makes the
    SDK fall back to its **own** default host: the key goes to somebody else's
    server, and for a chat provider the whole conversation goes with it.  The
    reason is identical in the three places, so the sentence is written once;
    what varies is only the prefix naming the entry, and whether the
    conversation travels alongside the key.

    It was three copies, and they had already drifted — two said "client" and
    one "SDK" for the same fallback, and the third was added *after* a review
    noticed the check was missing entirely from the chat providers, which is
    what a sentence in two places that a third caller cannot reach looks like.
    """
    extra = f" {also}" if also else ""
    return ConfigError(
        f"{where}: needs `base_url` — without one the SDK sends this provider's "
        f"key{extra} to its own default host"
    )


def _media_provider(raw: Any, name: str) -> MediaProviderSettings:
    """One `media.providers:` entry: an endpoint, a key and what it can do.

    `api` is required and is a *wire*, not a brand: it selects the adapter that
    speaks to this endpoint, and a name with no adapter behind it is refused at
    the first call rather than silently falling back to another one.  `base_url`
    is required for the reason `_embedding_provider` gives — an entry carrying a
    key with nowhere to send it posts that key to somebody else's host.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"media.providers.{name}: expected a mapping")
    api = str(raw.get("api") or "")
    base_url = str(raw.get("base_url") or "").rstrip("/")
    if not api:
        raise ConfigError(
            f"media.providers.{name}: needs `api` — the wire adapter that "
            f"speaks to this endpoint"
        )
    if not base_url:
        raise _needs_base_url(f"media.providers.{name}")
    models_raw = raw.get("models")
    if not isinstance(models_raw, list) or not models_raw:
        raise ConfigError(
            f"media.providers.{name}: needs `models` — a provider with no model "
            f"is a key and an endpoint nobody can ask for anything"
        )
    return MediaProviderSettings(
        name=name,
        api=api,
        base_url=base_url,
        models=tuple(
            _media_model(entry, f"media.providers.{name}.models")
            for entry in models_raw
        ),
        api_key_ref=str(raw.get("api_key") or ""),
    )


def _media_model(raw: Any, where: str) -> MediaModelSettings:
    """One entry of a provider's `models:` list.

    `kind` is checked here rather than at the call: an entry whose kind nothing
    knows is a model no tool can ever ask for, and finding that out at load is
    the difference between a typo and a capability that quietly does not exist.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping")
    model = str(raw.get("model") or "")
    kind = str(raw.get("kind") or "")
    if not model:
        raise ConfigError(f"{where}: an entry needs `model`")
    if kind not in MEDIA_KINDS:
        # `{model}` rather than `{model or '?'}`: the check above has already
        # refused an entry with no model, so the `or` could never fire — and a
        # name that cannot be empty reads as a name that might be.
        raise ConfigError(
            f"{where}.{model}: `kind` must be one of "
            f"{', '.join(MEDIA_KINDS)} — not {kind!r}"
        )
    params = _mapping(raw.get("params"), f"{where}.{model}.params")
    return MediaModelSettings(
        model=model,
        kind=kind,
        params={str(key): value for key, value in params.items()},
        voice=str(raw.get("voice") or ""),
    )


def _context(raw: Any, base: ContextSettings) -> ContextSettings:
    """The `context:` section, defaulted and bounded.

    **Ceiling above floor, and both within (0, 1].**  The pair is a window's
    ends, so a ceiling below the floor is a selection with a negative budget —
    which would recall nothing, forever, without anything saying why — and a
    fraction above one is a budget larger than the window it is a fraction of.
    An absent section is the default, as everywhere else; an *explicit* zero is
    refused rather than read as absent, which is `_int_or`'s rule one type over.
    """
    if raw is None:
        return base
    section = _mapping(raw, "context")
    ceiling = _optional_float(section.get("ceiling"), "context.ceiling")
    floor = _optional_float(section.get("floor"), "context.floor")
    ceiling = base.ceiling if ceiling is None else ceiling
    floor = base.floor if floor is None else floor
    if not 0 < floor < ceiling <= 1:
        raise ConfigError(
            f"context: floor={floor} and ceiling={ceiling} are not a window "
            f"(0 < floor < ceiling <= 1)"
        )
    limit = _int_or(
        section.get("recall_limit"), base.recall_limit, "context.recall_limit"
    )
    if limit < 1:
        raise ConfigError(f"context.recall_limit: {limit} recalls nothing")
    timeout = _optional_float(section.get("timeout"), "context.timeout")
    if timeout is not None and timeout <= 0:
        raise ConfigError(
            f"context.timeout: {timeout} would abandon every discriminator call "
            f"before it was made"
        )
    similarity = _optional_float(
        section.get("min_similarity"), "context.min_similarity"
    )
    if similarity is not None and not 0 <= similarity <= 1:
        # Refused for the reason the window above is: a cosine is a similarity
        # in [0, 1], so `min_similarity: 2` is a gate nothing can pass.  It
        # would recall nothing from the meaning leg — and recall nothing
        # *silently*, since a keyword hit is exempt and the turn would simply
        # run on less context than the operator asked for.
        raise ConfigError(
            f"context.min_similarity: {similarity} is not a similarity (0-1)"
        )
    return ContextSettings(
        ceiling=ceiling,
        floor=floor,
        recall_limit=limit,
        min_similarity=base.min_similarity if similarity is None else similarity,
        timeout=base.timeout if timeout is None else timeout,
    )


def _embeddings(raw: Any, base: EmbeddingsSettings) -> EmbeddingsSettings:
    """The `embeddings:` section, defaulted.

    **Absent means the default, not an error.**  A config file written before
    embeddings existed still loads, and a fresh checkout runs — the alternative
    would be a migration in a different coat, which is the thing this project
    does not have.  The requirement is enforced where it is real: a section that
    *is* there may not be empty, because there is no configuration in which the
    db plugin runs without an embedding model.

    **An `active_model` this config does not define is refused, naming the
    providers that do exist.**  It used to fall back to the first provider, so
    vectors came from an endpoint nobody chose and the index's recorded
    identity changed underneath — a name the config does not define is not a
    vote for the first name that does (DESIGN.md §10.1).  A section that names
    no `active_model` still takes the first, which is the designed default and
    not a substitution.
    """
    if raw is None:
        return base

    section = _mapping(raw, "embeddings")
    providers_raw = _mapping(section.get("providers"), "embeddings.providers")
    if not providers_raw:
        raise ConfigError(
            "embeddings.providers: needs at least one provider — there is no "
            "mode in which slife2 stores turns it cannot search semantically"
        )
    providers = {
        str(name): _embedding_provider(spec, str(name))
        for name, spec in providers_raw.items()
    }
    active = str(section.get("active_model") or "")
    if active and active not in providers:
        known = ", ".join(sorted(providers))
        raise ConfigError(
            f"embeddings.active_model: {active!r} is not a configured provider; "
            f"it has: {known}"
        )
    return EmbeddingsSettings(
        providers=providers,
        active=active or next(iter(providers)),
    )


def _embedding_provider(raw: Any, name: str) -> EmbeddingProviderSettings:
    """One `embeddings.providers:` entry: an OpenAI-compatible endpoint.

    Its address is required, and not only its key.  That is v1's rule and it
    closes a real hole rather than a hypothetical one: an entry carrying a key
    with nowhere to send it makes the OpenAI client fall back to its **own**
    default host, which means the key and every document in the history are
    posted to somebody else's server.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"embeddings.providers.{name}: expected a mapping")

    base_url = str(raw.get("base_url") or "")
    model = str(raw.get("model") or "")
    if not base_url:
        raise _needs_base_url(f"embeddings.providers.{name}")
    if not model:
        raise ConfigError(
            f"embeddings.providers.{name}: needs `model` — the id sent as "
            f"`model` on /embeddings"
        )
    return EmbeddingProviderSettings(
        name=name,
        base_url=base_url,
        model=model,
        api_key_ref=str(raw.get("api_key") or ""),
    )


#: The proxy a `rest-api:` entry is expanded into when it does not name a command
#: of its own: an OpenAPI document in, an MCP server out.  Both halves are v1's,
#: kept because they are what the ecosystem actually publishes — there is no
#: other well-known tool that does this — and because keeping them in one place
#: is what lets the *hub* stay ignorant of REST entirely.
OPENAPI_PROXY_COMMAND = "uvx"
OPENAPI_PROXY_ARGS = ("mcp-openapi-proxy",)

#: The environment the proxy reads.  v1's names, for the same reason.
OPENAPI_SPEC_URL = "OPENAPI_SPEC_URL"
OPENAPI_SERVER_URL = "SERVER_URL_OVERRIDE"
OPENAPI_API_KEY = "API_KEY"


def _mcp_servers(raw: dict[str, Any]) -> dict[str, ToolServerSettings]:
    """The `mcp:` section, as upstreams: an MCP server written out."""
    return {
        str(name): _tool_server(spec, str(name))
        for name, spec in _mapping(raw.get("mcp"), "mcp").items()
    }


def _rest_servers(raw: dict[str, Any]) -> dict[str, ToolServerSettings]:
    """The `rest-api:` section, as upstreams: a REST API *expanded* into one.

    `spec:` becomes the `uvx mcp-openapi-proxy` command that serves it, which is
    done here rather than by the plugin that holds it: turning a declarative
    entry into a runnable command is the config layer's business, and a plugin
    that did it would have to learn a second entry shape to validate.

    **The two sections are checked against each other and kept apart.**  Both
    would become one entry called `{name}__{tool}` in the model's tool list, so a
    name in both is refused rather than resolved — and they are two mappings
    rather than one because each is read by the plugin that owns it.
    """
    return {
        str(name): _rest_api(spec, str(name))
        for name, spec in _mapping(raw.get("rest-api"), "rest-api").items()
    }


def _tool_server(raw: Any, name: str) -> ToolServerSettings:
    """One `mcp:` entry: an MCP server, over stdio or over the network."""
    if not isinstance(raw, dict):
        raise ConfigError(f"mcp.{name}: expected a mapping")

    command = str(raw.get("command") or "")
    url = str(raw.get("url") or "")
    if command and url:
        raise ConfigError(
            f"mcp.{name}: has both `command` and `url`; one entry is one "
            f"transport — write two entries if you meant two servers"
        )
    if not command and not url:
        raise ConfigError(
            f"mcp.{name}: needs `command` (a program to start) or `url` (an "
            f"endpoint to connect to)"
        )

    return ToolServerSettings(
        name=name,
        description=str(raw.get("description") or ""),
        command=command,
        args=_string_list(raw.get("args"), f"mcp.{name}.args"),
        env=_secrets(raw.get("env"), f"mcp.{name}.env"),
        url=url,
        headers=_secrets(raw.get("headers"), f"mcp.{name}.headers"),
        cwd=str(raw.get("cwd") or ""),
        enabled=_enabled(raw, f"mcp.{name}"),
        autoload=raw.get("autoload") is True,
    )


def _rest_api(raw: Any, name: str) -> ToolServerSettings:
    """One `rest-api:` entry, expanded into the stdio upstream that serves it.

    Two spellings, one mechanism.  Writing `spec:` gets the standard proxy
    (an OpenAPI document in, an MCP server out) with the three environment
    variables it reads filled in from this entry; writing `command:` gets
    whatever proxy you have, for the cases the standard one does not cover — a
    pinned version, a private fork, a spec that needs an argument.

    What is *not* available is neither: an entry with no `spec` and no `command`
    describes a REST API nobody can reach, and finding that out at load says so
    once instead of on every turn.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"rest-api.{name}: expected a mapping")

    command = str(raw.get("command") or "")
    spec = str(raw.get("spec") or "")
    if not command and not spec:
        raise ConfigError(
            f"rest-api.{name}: needs `spec` (the OpenAPI document's URL or "
            f"path) or an explicit `command`"
        )

    env = _secrets(raw.get("env"), f"rest-api.{name}.env")
    if not command:
        command = OPENAPI_PROXY_COMMAND
        env = {OPENAPI_SPEC_URL: spec, **env}
        args = (
            *OPENAPI_PROXY_ARGS,
            *_string_list(raw.get("args"), f"rest-api.{name}.args"),
        )
    else:
        args = _string_list(raw.get("args"), f"rest-api.{name}.args")
        if spec:
            env = {OPENAPI_SPEC_URL: spec, **env}

    # These two are the proxy's, and they are written from the entry rather than
    # into `env:` because they are what the entry is *about*: which API, and
    # where, is not an environment variable a person should have to spell.
    if base_url := str(raw.get("base_url") or ""):
        env[OPENAPI_SERVER_URL] = base_url
    if raw.get("api_key") is not None:
        env[OPENAPI_API_KEY] = resolve_secret(raw.get("api_key"))

    return ToolServerSettings(
        name=name,
        kind="rest",
        description=str(raw.get("description") or ""),
        command=command,
        args=args,
        env=env,
        cwd=str(raw.get("cwd") or ""),
        enabled=_enabled(raw, f"rest-api.{name}"),
        autoload=raw.get("autoload") is True,
    )


def _cli_tools(raw: dict[str, Any]) -> dict[str, CliToolSettings]:
    """The `cli:` section — programs already on this machine, by name.

    Absent means empty rather than an error, like every other section here: a
    config written before this existed still loads, and there is a configuration
    in which slife2 runs with no external command at all — which is the one it
    ships with.
    """
    return {
        str(name): _cli_tool(spec, str(name))
        for name, spec in _mapping(raw.get("cli"), "cli").items()
    }


def _cli_tool(raw: Any, name: str) -> CliToolSettings:
    """One `cli:` entry: a command, and what a person needs to know about it.

    Only `command` is required, and it is required loudly.  An entry naming no
    program is not a disabled entry — it is one that can never work, and saying
    so at load names the entry once instead of failing at every call.

    Unlike `mcp:`, there is no second transport to choose between and so
    nothing to refuse for naming both.  The strictness that survives is the
    strictness that has something to be strict about.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"cli.{name}: expected a mapping")

    command = str(raw.get("command") or "")
    if not command:
        raise ConfigError(
            f"cli.{name}: needs `command` — the program the model may run "
            f"(e.g. `gh`, or `python -m mytool`)"
        )

    return CliToolSettings(
        name=name,
        command=command,
        description=str(raw.get("description") or ""),
        install=str(raw.get("install") or ""),
        source=_provenance(raw.get("source"), f"cli.{name}.source"),
        enabled=_enabled(raw, f"cli.{name}"),
    )


def _skills(raw: dict[str, Any]) -> dict[str, SkillSettings]:
    """The `skills:` section — what each playbook in `<data>/skills/` is given.

    Absent means empty, like every other section, and there is no requirement
    that a name here exist on disk: the directory is the truth about what is
    installed (`slife2.plugins.skills.playbooks`), and an entry for a skill somebody has not put
    in the folder yet is a key waiting for its lock rather than a mistake.

    **An entry that is not a mapping is refused**, as it is in every other
    section that holds entries (`_cli_tools`, `_tool_server`).  Read as an empty
    enabled entry it dropped the operator's key with nothing said, and the model
    later ran a playbook that died on the credential this section exists to
    supply (DESIGN.md §10.1).
    """
    entries = _mapping(raw.get("skills"), "skills")
    for name, spec in entries.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"skills.{name}: expected a mapping")
    return {
        str(name): SkillSettings(
            env=_secrets(spec.get("env"), f"skills.{name}.env"),
            enabled=_enabled(spec, f"skills.{name}"),
        )
        for name, spec in entries.items()
    }


def _provenance(raw: Any, where: str) -> dict[str, str]:
    """An entry's `source:`, as strings.

    Not `_secrets`: nothing here is a credential, and resolving `${VAR}` in a
    note about where an entry came from would invent a lookup the operator did
    not write.  Values are stringified for the same reason a provider's are —
    a version written bare is a YAML float more often than anybody expects.
    """
    return {str(key): str(value) for key, value in _mapping(raw, where).items()}


def _string_list(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: expected a list")
    return tuple(str(item) for item in raw)


def _enabled(raw: Mapping[str, Any], where: str) -> bool:
    """An entry's `enabled:` switch — a real YAML boolean, and absent means on.

    **`bool(raw.get("enabled", True))` read a string as a switch.**  ruamel's
    loader is YAML 1.2, so `enabled: no` and `enabled: off` are the *strings*
    `no` and `off` — not the booleans PyYAML would have made of them — and every
    non-empty string is true to `bool`.  So an entry the operator had switched
    off came back on, and `family.py` dialled it at every start, because
    `settings.enabled` is the whole of what decides whether a link is made.  The
    reading is wrong in the one direction that costs something: the server is
    started and asked, rather than not.

    **The value is refused rather than guessed at.**  Coercing a string to a
    boolean is the other half of the same mistake, one spelling over — it
    replaces "the switch this says is on" with "the switch it did not say"; a
    quoted `"false"` is a hand-edit that means nothing at all, and a *value that
    cannot be read is not an absent one*.  Every other field in this module is
    refused at load with a sentence naming it, and a switch decides whether a
    process is dialled, so it is refused the same way.  A key that is not there
    is the one other answer, and it is the documented default: `enabled: true`
    is what the file never writes and absent is what it means
    (`slife2.libs.host.configfile.upsert`).
    """
    value = raw.get("enabled", True)
    if not isinstance(value, bool):
        raise ConfigError(
            f"{where}.enabled: expected `true` or `false`, got {value!r} — YAML "
            f"spells a boolean as one, so `no`, `off` and a quoted 'false' are "
            f"strings; leave the key out for true"
        )
    return value


def _secrets(raw: Any, where: str) -> dict[str, str]:
    """A mapping whose values go through the secret chain.

    Only `env` and `headers`, because those are the two places a tool server is
    handed a credential; a `command` or an `args` list is a program name and is
    visible in the process table regardless.
    """
    return {
        str(key): resolve_secret(value) for key, value in _mapping(raw, where).items()
    }


def _template_path(raw: Any, config_dir: Path | None, default: str) -> str:
    """Resolve `agent.system_prompt` to a template file, or the shipped default.

    A relative path is looked for beside the config that named it — the useful
    place for a prompt somebody is editing — and leaving the field out means the
    template the distribution ships.  A path that is not there is a config
    mistake, and failing at load says so once rather than on every turn.
    """
    if not raw:
        return default
    path = Path(str(raw))
    if not path.is_absolute():
        path = (config_dir / path) if config_dir else path
    if not path.is_file():
        raise ConfigError(f"agent.system_prompt: no such template: {path}")
    return str(path)


def _first_reference(providers: dict[str, ProviderSettings]) -> str:
    """`provider/model` for the first configured model, as a fallback default."""
    for name, provider in providers.items():
        if provider.models:
            return f"{name}/{next(iter(provider.models))}"
    return ""


def _server(raw: Any, default: ServerSettings, where: str) -> ServerSettings:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping with host/port/path")
    host = str(raw.get("host") or default.host)
    if ":" in host or "/" in host:
        # **The first of the three fields, and the last to be refused.**  `port`
        # and `path` are refused below and the host was copied through,
        # so `host: "::1"` — the natural spelling of an IPv6 loopback, and one
        # `a2a.broker.host` accepts — built `http://::1:8010/mcp`.
        # `ServerSettings.url` writes the address as `host:port` and puts no
        # brackets on it, so the first thing to *read* that URL
        # (`runtime.normalize_url`'s `urlsplit(...).port`, inside `start_lock`)
        # raised `ValueError: Port could not be cast to integer value as
        # ':1:8010'` — after some servers were already up, in a command that
        # answers every bad field with one sentence and catches `ConfigError`.
        # A `/` is the same mistake the other way: `host: 0.0.0.0/x` moves the
        # path into the address, and the port is then not there at all.
        raise ConfigError(
            f"{where}.host: {host!r} is not a host — the endpoint is written as "
            f"`host:port` with no brackets, so an IPv6 address needs the path "
            f"spelt out another way, and a `/` belongs in `path:`; leave it out "
            f"for the default ({default.host})"
        )
    port = _int_or(raw.get("port"), default.port, f"{where}.port")
    if not 0 < port < 65536:
        # Refused rather than defaulted, and rather than passed on: a port is
        # how the launcher finds this server again — it probes the address it
        # decided, before it starts anything — so `port: 0` would bind whatever
        # the kernel liked and then be probed at a port nobody is on.  The
        # `_int_or` above is what makes this reachable at all; `int(x or
        # default)` read the zero as "not given".
        raise ConfigError(
            f"{where}.port: {port} is not a port (1-65535); leave it out for "
            f"the default ({default.port})"
        )
    path = str(raw.get("path") or default.path)
    if not path.startswith("/"):
        # **The third field, and the one that was copied through.**  `host` and
        # `port` are read one line up and `path` was copied through, so `path: mcp`
        # was accepted and `ServerSettings.url` concatenates the two halves
        # with nothing between them: the endpoint became
        # `http://127.0.0.1:8005mcp`, whose *port* is the string `8005mcp`.
        # Nothing raised here — the first thing to read the URL does
        # (`runtime.normalize_url`'s `urlsplit(...).port`, inside
        # `start_lock`), so the typo arrived as a `ValueError` out of the
        # start-up loop, after some servers were already up and running, in a
        # command that catches `ConfigError` and answers every other bad field
        # with one sentence.  A path is the third of the three fields this
        # function reads, so it is refused where they are.
        raise ConfigError(
            f"{where}.path: {path!r} is not a path — the endpoint is the "
            f"address and the path together, so it has to start with `/`; "
            f"leave it out for the default ({default.path})"
        )
    return ServerSettings(host=host, port=port, path=path)


def _provider(raw: Any, name: str) -> ProviderSettings:
    if not isinstance(raw, dict):
        raise ConfigError(f"providers.{name}: expected a mapping")

    api = str(raw.get("api") or "")
    if api not in API_BACKENDS:
        raise ConfigError(
            f"providers.{name}: unknown api {api!r}; known: {', '.join(API_BACKENDS)}"
        )

    models_raw = raw.get("models")
    if not isinstance(models_raw, list) or not models_raw:
        raise ConfigError(f"providers.{name}: needs a non-empty `models:` list")

    models: dict[str, ModelSettings] = {}
    for entry in models_raw:
        model = _model(entry, name)
        models[model.model] = model

    base_url = str(raw.get("base_url") or "")
    if not base_url:
        # The same hole the other two guard, and the same reason — and this is
        # the one the check was *missing* on for the longest, because the
        # sentence existed twice and neither copy was in reach of this function.
        raise _needs_base_url(f"providers.{name}", also="and the conversation")

    return ProviderSettings(
        api=api,
        base_url=base_url,
        api_key_ref=str(raw.get("api_key") or ""),
        models=models,
    )


def _model(raw: Any, provider: str) -> ModelSettings:
    if not isinstance(raw, dict):
        raise ConfigError(f"providers.{provider}: each model must be a mapping")
    model = str(raw.get("model") or "")
    if not model:
        raise ConfigError(f"providers.{provider}: a model entry needs `model:`")

    compat = raw.get("compat") or {}
    if not isinstance(compat, dict):
        raise ConfigError(f"providers.{provider}.{model}: compat must be a mapping")

    modalities = raw.get("input")
    if isinstance(modalities, list):
        inputs = tuple(str(m) for m in modalities)
    elif modalities is None:
        inputs = ("text",)
    else:
        inputs = (str(modalities),)

    # `None` here is a value, not an absence: `compat.store` says "send
    # nothing" and must stay distinguishable from `compat.store: false`, which
    # says "send false".  A plain `bool(...)` would collapse the two into False.
    store_raw = compat.get("store")

    return ModelSettings(
        model=model,
        name=str(raw.get("name") or ""),
        reasoning=bool(raw.get("reasoning", False)),
        input=inputs,
        context_window=_int_or(
            raw.get("context_window"), 0, f"providers.{provider}.{model}.context_window"
        ),
        max_tokens=_optional_int(
            raw.get("max_tokens"), f"providers.{provider}.{model}.max_tokens"
        ),
        temperature=_optional_float(
            raw.get("temperature"), f"providers.{provider}.{model}.temperature"
        ),
        top_p=_optional_float(raw.get("top_p"), f"providers.{provider}.{model}.top_p"),
        thinking=str(compat.get("thinking") or ""),
        stream_usage=str(compat.get("stream_usage") or ""),
        store=None if store_raw is None else bool(store_raw),
    )


def _int_or(value: Any, default: int, where: str) -> int:
    """`value` as an int, or `default` when the key is **absent**.

    Absent means absent, and that is the whole of what this adds over
    `int(value or default)`: the `or` idiom also treats an explicit `0` as "not
    given", so `tool_load.threshold: 0` — the value the guard below exists to
    refuse — was silently replaced by the default, and so was `port: 0` (the
    kernel's "pick one for me", which is not something a server of ours should
    be handed by accident).  `max_steps: 0` was the third example and is now
    the one this reader exists to *keep*: a written zero is the config's "no
    cap", and it means the same thing as leaving the key out
    (`slife2.libs.host.config.AgentSettings.max_steps`).

    `where` names the field, because a value that is not a number is the
    caller's typo and has to be reported like every other one.  A bare `int()`
    raises `ValueError`, which nothing above this catches: `max_steps: many`
    reached the user as a traceback out of the `slife2` console script rather
    than as the one line every other bad value produces.
    """
    return default if value is None else _as_int(value, where)


def _optional_int(value: Any, where: str) -> int | None:
    return None if value is None else _as_int(value, where)


def _optional_float(value: Any, where: str) -> float | None:
    return None if value is None else _as_float(value, where)


def _as_int(value: Any, where: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: expected a whole number, got {value!r}") from exc


def _as_float(value: Any, where: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: expected a number, got {value!r}") from exc


__all__ = [
    "AGENT_SERVER_NAME",
    "AGENT_TASK_SERVER_NAME",
    "API_BACKENDS",
    "API_SERVER_NAMES",
    "CABINET_SERVER_NAME",
    "CLI_SERVER_NAME",
    "CONTEXT_SERVER_NAME",
    "DEFAULT_AGENT",
    "DEFAULT_CONFIG_NAME",
    "EMBEDDINGS_SERVER_NAME",
    "LOCAL_SERVERS",
    "MCP_TOOLS_SERVER_NAME",
    "MEDIA_KINDS",
    "MEDIA_SERVER_NAME",
    "MODEL_TOOL",
    "PLUGINS",
    "RESTAPI_TOOLS_SERVER_NAME",
    "SKILLS_SERVER_NAME",
    "TOOLHUB_SERVER_NAME",
    "WECHAT_SERVER_NAME",
    "AgentSettings",
    "AgentTaskSettings",
    "CliToolSettings",
    "Config",
    "ConfigError",
    "ContextSettings",
    "EmbeddingProviderSettings",
    "EmbeddingsSettings",
    "MediaModelSettings",
    "MediaProviderSettings",
    "MediaSettings",
    "ModelSettings",
    "Plugin",
    "ProviderSettings",
    "ServerSettings",
    "SkillSettings",
    "ToolLoadSettings",
    "ToolResultSettings",
    "ToolServerSettings",
    "default_config",
    "find_config_path",
    "for_the_call",
    "load",
    "load_cached",
    "resolve_secret",
]
