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

The other section worth reading here is `tools:` — the *external* MCP servers,
which the plugin that owns the section holds a connection to:

    tools:
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

Note the two words that are one letter apart throughout this file: `servers:` is
*our* plugins — the ones slife2 starts, shares and stops — while `tools:` is
other people's, which `slife2-mcp-tools` holds as a client and declares to the
hub.  They are different things with different failure rules
(`slife2.toolhub`), and this is the only place both are configured.

And one section for a program that is neither, because it is already here:

    cli:
      yt-dlp:
        command: yt-dlp                 # what runs
        description: Download video from 1000+ sites, subtitles and playlists.
        install: uv pip install yt-dlp  # what a person is told when it is absent

An entry names a command on this machine rather than a server to connect to:
there is no process for slife2 to start, no URL, and nothing to keep alive — so
it is not a `tools:` entry, and nothing about it is a plugin either, because
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

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from slife2.paths import data_dir


class ConfigError(Exception):
    """A config file was found and could not be used.

    A *missing* file is not an error — the defaults are the config — but a
    malformed one is fatal, because silently ignoring a file the user wrote is
    the worst of the three possible behaviours.
    """


#: The wire protocols this distribution implements, and the server module that
#: speaks each.  `api` selects one; an unknown value is a typo worth reporting
#: rather than a provider that quietly never starts.
API_BACKENDS: dict[str, str] = {
    "openai-completions": "slife2.llm.openai_server",
    "anthropic-messages": "slife2.llm.anthropic_server",
    # A protocol, not a flag on the one above: the Responses API takes a
    # different input shape, names its tools differently and streams different
    # events, so it is a process of its own like every other entry here.
    # Appended last on purpose — `apis_in_use` preserves this order and the
    # launcher starts them in it.
    "openai-responses": "slife2.llm.openai_responses_server",
}

#: The MCP name the server for each wire protocol advertises — what
#: `Client.server_info` reports, and so what a caller compares against to prove
#: it reached the server it meant rather than some other MCP server on the port.
#:
#: Beside `API_BACKENDS` because the same thing decides both: a module is only
#: reachable through the protocol it speaks.  It cannot be derived from the
#: module name without importing the module, and importing `openai_server` is
#: exactly what the agent server must never do, so it is spelled out here — and
#: `tests/test_config.py` asserts every entry still matches its module's own
#: `SERVER_NAME`, which is the only way this can drift.
API_SERVER_NAMES: dict[str, str] = {
    "openai-completions": "slife2-llm-openai",
    "anthropic-messages": "slife2-llm-anthropic",
    "openai-responses": "slife2-llm-openai-responses",
}

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

#: The MCP names the servers in this system advertise, as `Client.server_info`
#: reports them.  A client proves it reached the server it meant by comparing
#: against these; see `slife2.mcp_server.identifies`.
#:
#: Spelled out rather than read off the modules, because reading them means
#: importing them: the TUI and the agent loop must not pull in a server, and the
#: agent loop in particular must never reach `openai_server`.  `tests/test_config.py`
#: asserts each still matches its module's own `SERVER_NAME`.
AGENT_SERVER_NAME = "slife2-agent"
CONTEXT_SERVER_NAME = "slife2-context"
MCP_TOOLS_SERVER_NAME = "slife2-mcp-tools"
RESTAPI_TOOLS_SERVER_NAME = "slife2-restapi-tools"
SKILLS_SERVER_NAME = "slife2-skills"
CLI_SERVER_NAME = "slife2-cli"
TOOLHUB_SERVER_NAME = "slife2-toolhub"
EMBEDDINGS_SERVER_NAME = "slife2-llm-embeddings"

#: The plugins that are not model backends, and so have a name of their own
#: rather than one derived from a wire protocol.  This is the *set* of ours, in
#: the order `slife2.config.Config.plugins` reads them out in.
#:
#: **The two edges between them are not here any more.**  They were prose in this
#: comment — `embeddings` before `context`, because the context plugin opens the
#: embedder before it serves anything; and every plugin the hub asks for tools
#: before `toolhub` — and prose is not something a launcher can act on, so it
#: serialised all ten for the sake of two.  They are a table now,
#: `slife2.launcher.NEEDS`, and everything not named there starts at once.
#:
#: What is left in this order is a preference rather than a dependency, and it is
#: the one `plugins()` states: a model server answering first is what keeps a
#: first turn from failing and retrying.
#:
#: **There is no `db` here any more**, and its absence is the design rather than
#: a gap.  It served the turns and the tool catalogue, and neither needed a
#: process: a store has one writer, and "one writer" is a property of a SQLite
#: file, not of a server.  The turns are `context`'s now and the catalogue is the
#: hub's, both out of the same library (`slife2.db`), and v1's own answer was the
#: same — `memdb` ships as a plugin *and* is imported, and the headless host
#: restores its session straight from `SessionStore` with no transport in the
#: path.
#:
#: `skills-server` and `cli-server` are the two families with nothing to connect
#: to — a folder of playbooks and a list of programs already installed.  They are
#: plugins all the same, because a family's rows and the tool that reads them
#: belong to the process that owns the family, and because both have a
#: model-facing tool still to come (DESIGN.md §9).  Their keys carry the suffix
#: because the *catalogue sources* they declare under are `skills` and `cli`,
#: which are also their config sections' names — and a source's rows must not
#: share an id with the tools of the server that declares them.
LOCAL_SERVERS = (
    "embeddings",
    "context",
    "skills-server",
    "cli-server",
    "mcp-tools",
    "restapi-tools",
    "toolhub",
    "agent",
)


def _credstore_lookup(key: str) -> str | None:
    """Look up an environment-variable name in the OS keyring.

    The env var name *is* the credential-store key, so ``DEEPSEEK_API_KEY``
    resolves whether it was exported or stored with ``credstore set``.

    Every failure is swallowed into None.  credstore spans three platform
    backends plus an encrypted-file fallback, and a machine with no keyring at
    all (a CI runner, a container) must degrade to "not found" rather than take
    the process down over a secret it may not even need.
    """
    try:
        from credstore import get_credential

        return get_credential(key)
    except Exception:  # noqa: BLE001 - a keyring that is not there is one this chain steps past
        return None


def resolve_secret(value: object) -> str:
    """Resolve one config value through the chain in the module docstring.

    Takes `object` because YAML hands back ints and bools for unquoted scalars,
    and an API key written as a bare number should become a string rather than
    an exception.
    """
    if not isinstance(value, str):
        return str(value)

    try:
        from credstore import is_keyring_uri, resolve_uri

        if is_keyring_uri(value):
            return resolve_uri(value)
    except Exception:  # noqa: BLE001 - an unresolvable reference is left verbatim, by design
        pass

    def _replace(match: re.Match[str]) -> str:
        var, default = match.group(1), match.group(2)
        found = os.environ.get(var) or _credstore_lookup(var)
        if found:
            return found
        # Unresolvable and no default: leave the reference verbatim, so the
        # failure surfaces where the value is used with its own name intact.
        return default if default is not None else match.group(0)

    return _ENV_PATTERN.sub(_replace, value)


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
    #: `slife2.llm.openai_server` re-sends it as `reasoning_content`, which the
    #: DeepSeek reasoners require on every assistant message once thinking has
    #: been asked for.  v1's rule, and the two belong together — a model that
    #: thinks is the model whose endpoint knows the field's name.  Nothing
    #: depends on it otherwise: this is a fact about the model, not a switch.
    reasoning: bool = False
    #: Modalities accepted.  This is the config that says whether a model can
    #: read an image, and the agent server refuses an attachment against a model
    #: whose `input` does not list `image` — silently dropping one is worse than
    #: saying the model cannot read it.  See `slife2.server.server._with_images`.
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
    #: it.  See `slife2.llm.openai_responses_server.store_parameter`.
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
        """The provider in use, falling back to the first.

        Raises:
            ConfigError: If there are none, which `_embeddings` refuses at load
                time — so reaching this means a `Config` assembled in code
                rather than read from a file.
        """
        if not self.providers:
            raise ConfigError("no embedding provider configured")
        if self.active in self.providers:
            return self.providers[self.active]
        return next(iter(self.providers.values()))


@dataclass(frozen=True)
class AgentSettings:
    """How the agent loop behaves, and where it listens.  Not an identity."""

    server: ServerSettings = field(default_factory=ServerSettings)
    max_steps: int = 16
    #: A Jinja2 template for the system prompt — an absolute path by the time
    #: this is built, or "" for the one the distribution ships.  A template
    #: rather than a string because the prompt has a hole in it where the agent
    #: name goes, and the hole has to be *somewhere* the caller can see.
    system_prompt: str = ""


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
    #: match is a stronger signal than a cosine neighbourhood (`slife2.context`).
    min_similarity: float = 0.45
    #: How long the discriminator call may take before the turn runs on the
    #: context it already has.  Generous for a remote model, tight for the
    #: reason `slife2.context_server` gives: the answer is optional by design, so
    #: a slow one is only making the *turn* late.
    timeout: float = 20.0


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

    #: The name it is configured under.  For an entry under `tools:` it prefixes
    #: every tool that entry offers, so it is also the name a person reads in a
    #: tool call; for one of ours (`kind="plugin"`) it prefixes nothing — a
    #: plugin's tools are the model's under their own names, and this is the
    #: name `servers()` reports them by.  See `slife2.toolhub.model_name`.
    name: str
    #: Which kind of entry this is: `"mcp"` or `"rest"` for one somebody wrote
    #: under `tools:`, and `"plugin"` for one of ours, which has no section of
    #: its own — `slife2.toolhub.plugin_settings` builds it from `servers:`.  A
    #: label, not a behaviour: a REST API entry has already been expanded into
    #: the stdio command it describes by the time one of these exists (see
    #: `_rest_api`), so nothing downstream branches on this.  It is kept because
    #: it is the answer to the first question anyone debugging asks, which is why
    #: a server they never wrote a `command:` for is running `uvx`.
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
    about.  See `slife2.db.ToolStore.injectable`.
    """

    threshold: int = DEFAULT_TOOL_LOAD


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
    needs (`slife2.skills`).  This is the other half: where the value comes
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
    the whole of `slife2.skills`, and nothing has to be kept in step with it —
    but a skill somebody wants out of the model's way is a *decision*, and a
    decision belongs in the file the operator edits rather than in a name
    convention inside a directory.  It is the same switch, with the same meaning
    and the same spelling, as on a server and on a `cli:` entry.
    """

    name: str
    env: dict[str, str] = field(default_factory=dict)
    #: `false` keeps the playbook installed and out of the catalogue.  Read by
    #: the plugin that owns the section, which declares the row `disabled`.
    enabled: bool = True


@dataclass(frozen=True)
class Config:
    """Every section of one config file, already defaulted."""

    #: name -> where it listens, keyed by `"agent"` and by each `api` in use.
    servers: dict[str, ServerSettings] = field(default_factory=dict)
    providers: dict[str, ProviderSettings] = field(default_factory=dict)
    #: Other people's MCP servers, from `tools:`.  Held by `slife2-mcp-tools`,
    #: which declares each entry to the hub as a source of its own.
    tools: dict[str, ToolServerSettings] = field(default_factory=dict)

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
    #: How a conversation's context is decided, from `context:`.  Read by
    #: `slife2-context`, which is the plugin that owns the turn log.
    context: ContextSettings = field(default_factory=ContextSettings)
    #: How many function tools the model's list may hold.  Read by the toolhub,
    #: which owns the catalogue and therefore the budget: the count it bounds is
    #: a `SELECT COUNT(*)` over its own rows.
    tool_load: ToolLoadSettings = field(default_factory=ToolLoadSettings)
    #: The endpoints vectors come from, and which one is in use.  Its own
    #: section rather than entries under `providers:`, for the reason
    #: `EmbeddingProviderSettings` gives.
    embeddings: EmbeddingsSettings = field(default_factory=EmbeddingsSettings)
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
        servers under `tools:` — and this is the first of them: derived here
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
    # for the reason the `tools:` section's accessor went the same way: `enabled`
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
        servers={
            "agent": ServerSettings(port=8000),
            # A plugin of its own: keeping a conversation's turns — and deciding
            # which of them it runs on — is one job, and it is not a wire
            # protocol like the model backends.  This is v1's `memdb`, and it
            # took the port the db plugin had.
            "context": ServerSettings(port=8010),
            # The two families that are not tools but have to be findable, each
            # owned by the process that owns its config section: the playbooks
            # in `<data>/skills/` (`slife2.skills_server`) and the programs the
            # config's `cli:` section records as installed (`slife2.cli_server`).
            # They declare catalogue rows rather than tools, and the hub is what
            # merges them — see `slife2.mcp_server.LIST_SOURCES`.
            "skills-server": ServerSettings(port=8031),
            "cli-server": ServerSettings(port=8032),
            # And the two whose whole job is somebody else's servers: the
            # `tools:` section (`slife2.mcp_tools`) and the `rest-api:` one
            # (`slife2.restapi_tools`).  They hold the connections the toolhub
            # used to hold, and declare what they hold to it — the hub keeps the
            # tool *set* and no link to anybody.
            "mcp-tools": ServerSettings(port=8033),
            "restapi-tools": ServerSettings(port=8034),
            # The model's tools, and the only process that decides what they
            # are.  It holds no credential and no connection to anybody's
            # server: every plugin above is asked for a tool list and for the
            # sources it holds, and the two `*-tools` plugins above are the ones
            # that reach the entries under `tools:` and `rest-api:`.
            "toolhub": ServerSettings(port=8020),
            # One port per wire protocol, not per provider: a process speaks one
            # format, and `stream_chat(provider=...)` picks whose credentials.
            "openai-completions": ServerSettings(port=8001),
            "anthropic-messages": ServerSettings(port=8002),
            "openai-responses": ServerSettings(port=8003),
            # Embeddings are an OpenAI-protocol endpoint and *not* one of the
            # chat backends above, so they are served by a process of their own
            # rather than borrowed from `openai-completions`: that process is
            # built from the chat providers, and there is no chat provider here
            # to attach an embedding model to.  See `EmbeddingsSettings`.
            "embeddings": ServerSettings(port=8004),
        },
        providers={"deepseek": deepseek},
        tools={},
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
    run, the runtime state of what is running, and the turns they produced.
    Naming the config separately would mean two answers to "where is this
    installation?", and the data directory would still have to be found another
    way.
    """
    if explicit is not None:
        return Path(explicit)
    candidate = data_dir() / DEFAULT_CONFIG_NAME
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

    providers_raw = _mapping(raw.get("providers"), "providers")
    providers = (
        {str(name): _provider(spec, str(name)) for name, spec in providers_raw.items()}
        if providers_raw
        else dict(base.providers)
    )

    agent_raw = _mapping(raw.get("agent"), "agent")
    agent = AgentSettings(
        server=servers["agent"],
        max_steps=_int_or(agent_raw.get("max_steps"), base.agent.max_steps),
        system_prompt=_template_path(
            agent_raw.get("system_prompt"), config_dir, base.agent.system_prompt
        ),
    )

    # **A name in both sections is refused rather than resolved**, and this is
    # the only place both are known: each is read by the plugin that owns it, so
    # neither can check the other.  Both would become one entry called
    # `{name}__{tool}` in the model's tool list, so which of the two won would be
    # invisible at every point a person could look.
    tools = _mcp_servers(raw)
    rest_apis = _rest_servers(raw)
    for name in rest_apis:
        if name in tools:
            raise ConfigError(
                f"rest-api.{name}: already configured under `tools:`; the two "
                f"would collide as tool names"
            )

    return Config(
        servers=servers,
        providers=providers,
        tools=tools,
        rest_apis=rest_apis,
        cli=_cli_tools(raw),
        skills=_skills(raw),
        agent=agent,
        context=_context(raw.get("context"), base.context),
        tool_load=_tool_load(raw.get("tool_load"), base.tool_load),
        embeddings=_embeddings(raw.get("embeddings"), base.embeddings),
        default=str(raw.get("default") or _first_reference(providers)),
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
    threshold = _int_or(section.get("threshold"), base.threshold)
    if threshold < 1:
        raise ConfigError(
            f"tool_load.threshold: {threshold} would evict every tool the model "
            f"loads, leaving it a search it cannot keep"
        )
    return ToolLoadSettings(threshold=threshold)


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
    ceiling = _optional_float(section.get("ceiling"))
    floor = _optional_float(section.get("floor"))
    ceiling = base.ceiling if ceiling is None else ceiling
    floor = base.floor if floor is None else floor
    if not 0 < floor < ceiling <= 1:
        raise ConfigError(
            f"context: floor={floor} and ceiling={ceiling} are not a window "
            f"(0 < floor < ceiling <= 1)"
        )
    limit = _int_or(section.get("recall_limit"), base.recall_limit)
    if limit < 1:
        raise ConfigError(f"context.recall_limit: {limit} recalls nothing")
    timeout = _optional_float(section.get("timeout"))
    if timeout is not None and timeout <= 0:
        raise ConfigError(
            f"context.timeout: {timeout} would abandon every discriminator call "
            f"before it was made"
        )
    similarity = _optional_float(section.get("min_similarity"))
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

    A stale `active_model` falls back to the first provider rather than
    refusing, which is v1's rule and the useful one: the failure it prevents is
    a config that stops loading because one word went stale, and the failure it
    allows — vectors silently coming from a provider nobody chose — is visible
    in `slife2 status` and in the index's recorded identity.
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
    return EmbeddingsSettings(
        providers=providers,
        active=active if active in providers else next(iter(providers)),
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
        raise ConfigError(
            f"embeddings.providers.{name}: needs `base_url` — without one the "
            f"client would send this provider's key to its own default host"
        )
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
    """The `tools:` section, as upstreams: an MCP server written out."""
    return {
        str(name): _tool_server(spec, str(name))
        for name, spec in _mapping(raw.get("tools"), "tools").items()
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
    """One `tools:` entry: an MCP server, over stdio or over the network."""
    if not isinstance(raw, dict):
        raise ConfigError(f"tools.{name}: expected a mapping")

    command = str(raw.get("command") or "")
    url = str(raw.get("url") or "")
    if command and url:
        raise ConfigError(
            f"tools.{name}: has both `command` and `url`; one entry is one "
            f"transport — write two entries if you meant two servers"
        )
    if not command and not url:
        raise ConfigError(
            f"tools.{name}: needs `command` (a program to start) or `url` (an "
            f"endpoint to connect to)"
        )

    return ToolServerSettings(
        name=name,
        description=str(raw.get("description") or ""),
        command=command,
        args=_string_list(raw.get("args"), f"tools.{name}.args"),
        env=_secrets(raw.get("env"), f"tools.{name}.env"),
        url=url,
        headers=_secrets(raw.get("headers"), f"tools.{name}.headers"),
        cwd=str(raw.get("cwd") or ""),
        enabled=bool(raw.get("enabled", True)),
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
        args = (*OPENAPI_PROXY_ARGS, *_string_list(raw.get("args"), name))
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
        enabled=bool(raw.get("enabled", True)),
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

    Unlike `tools:`, there is no second transport to choose between and so
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
        enabled=bool(raw.get("enabled", True)),
    )


def _skills(raw: dict[str, Any]) -> dict[str, SkillSettings]:
    """The `skills:` section — what each playbook in `<data>/skills/` is given.

    Absent means empty, like every other section, and there is no requirement
    that a name here exist on disk: the directory is the truth about what is
    installed (`slife2.skills`), and an entry for a skill somebody has not put
    in the folder yet is a key waiting for its lock rather than a mistake.
    """
    return {
        str(name): SkillSettings(
            name=str(name),
            env=_secrets(spec.get("env"), f"skills.{name}.env")
            if isinstance(spec, dict)
            else {},
            enabled=bool(spec.get("enabled", True)) if isinstance(spec, dict) else True,
        )
        for name, spec in _mapping(raw.get("skills"), "skills").items()
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
    port = _int_or(raw.get("port"), default.port)
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
    return ServerSettings(
        host=str(raw.get("host") or default.host),
        port=port,
        path=str(raw.get("path") or default.path),
    )


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
        # The same hole `_embedding_provider` guards, and the same reason: a
        # key with nowhere to go makes the SDK fall back to its **own** default
        # host, so the key and the whole conversation are posted to somebody
        # else's server.  Chat providers had no equivalent check.
        raise ConfigError(
            f"providers.{name}: needs `base_url` — without one the SDK sends "
            f"this provider's key and the conversation to its own default host"
        )

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
        context_window=_int_or(raw.get("context_window"), 0),
        max_tokens=_optional_int(raw.get("max_tokens")),
        temperature=_optional_float(raw.get("temperature")),
        top_p=_optional_float(raw.get("top_p")),
        thinking=str(compat.get("thinking") or ""),
        stream_usage=str(compat.get("stream_usage") or ""),
        store=None if store_raw is None else bool(store_raw),
    )


def _int_or(value: Any, default: int) -> int:
    """`value` as an int, or `default` when the key is **absent**.

    Absent means absent, and that is the whole of what this adds over
    `int(value or default)`: the `or` idiom also treats an explicit `0` as "not
    given", so `tool_load.threshold: 0` — the value the guard below exists to
    refuse — was silently replaced by the default, as were `max_steps: 0` (a
    turn with no model call in it) and `port: 0` (the kernel's "pick one for
    me", which is not something a server of ours should be handed by accident).
    """
    return default if value is None else int(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


__all__ = [
    "AGENT_SERVER_NAME",
    "API_BACKENDS",
    "API_SERVER_NAMES",
    "CLI_SERVER_NAME",
    "CONTEXT_SERVER_NAME",
    "DEFAULT_AGENT",
    "EMBEDDINGS_SERVER_NAME",
    "EmbeddingProviderSettings",
    "EmbeddingsSettings",
    "LOCAL_SERVERS",
    "MCP_TOOLS_SERVER_NAME",
    "RESTAPI_TOOLS_SERVER_NAME",
    "SKILLS_SERVER_NAME",
    "TOOLHUB_SERVER_NAME",
    "DEFAULT_CONFIG_NAME",
    "AgentSettings",
    "CliToolSettings",
    "Config",
    "ConfigError",
    "ContextSettings",
    "ModelSettings",
    "ProviderSettings",
    "ServerSettings",
    "SkillSettings",
    "ToolLoadSettings",
    "ToolServerSettings",
    "default_config",
    "find_config_path",
    "load",
    "resolve_secret",
]
