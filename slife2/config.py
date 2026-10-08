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

The other section worth reading here is `tools:` — the *external* MCP servers the
toolhub connects to:

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
*our* components — the ones slife2 starts, shares and stops — while `tools:` is
other people's, which the toolhub connects to as a client.  They are different
things with different failure rules (`slife2.toolhub`), and this is the only
place both are configured.

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
DB_SERVER_NAME = "slife2-db"
BUILTINS_SERVER_NAME = "slife2-builtins"
TOOLHUB_SERVER_NAME = "slife2-toolhub"

#: The components that are not model backends, and so have a name of their own
#: rather than one derived from a wire protocol.  The order is the order the
#: launcher starts them in — see `slife2.config.Config.components` — which is why
#: `builtins` comes before `toolhub`: the hub asks it for a tool list, and the
#: answer to a first turn should not be "not connected yet".
LOCAL_SERVERS = ("db", "builtins", "toolhub", "agent")


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
    except Exception:
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
    except Exception:
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
class ToolServerSettings:
    """One external MCP server, as the toolhub needs to reach it.

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

    #: The name it is configured under.  It prefixes every tool this server
    #: offers, so it is also the name a person reads in a tool call.
    name: str
    #: Which section it was written in — `"mcp"` or `"rest"`.  A label, not a
    #: behaviour: a REST API entry has already been expanded into the stdio
    #: command it describes by the time one of these exists (see `_rest_api`),
    #: so nothing downstream branches on this.  It is kept because it is the
    #: answer to the first question anyone debugging asks, which is why a server
    #: they never wrote a `command:` for is running `uvx`.
    kind: str = "mcp"
    #: What the server is for, in the operator's words.  Not the model's: the
    #: descriptions the model reads come from the server itself, tool by tool.
    #: This one is for whoever opens the config a year later and for `servers()`,
    #: which is where "why is that connected" gets answered.
    description: str = ""
    #: stdio: the program to start.
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    #: http: the endpoint, either transport.  A URL ending in `/sse` is the
    #: older SSE transport; anything else is Streamable HTTP.
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    #: stdio only, and it matters more than it looks: a server started with a
    #: relative path in its arguments resolves that path against its working
    #: directory, and a daemon's own working directory is the runtime folder.
    #: Empty means the data directory — where a person means `.` to point.
    cwd: str = ""
    #: `false` stays configured but is never connected, and by itself keeps the
    #: hub process from starting at all.  This is how a slow, paid or
    #: currently-broken server stays in the file without being in the way.
    enabled: bool = True

    @property
    def transport(self) -> str:
        """`"http"` or `"stdio"` — read off which field is set."""
        return "http" if self.url else "stdio"


@dataclass(frozen=True)
class Config:
    """Every section of one config file, already defaulted."""

    #: name -> where it listens, keyed by `"agent"` and by each `api` in use.
    servers: dict[str, ServerSettings] = field(default_factory=dict)
    providers: dict[str, ProviderSettings] = field(default_factory=dict)
    #: The external tool servers the toolhub connects to, from both `tools:` and
    #: `rest-api:` — one mapping, because the hub connects to one kind of thing
    #: and the difference between the two sections is how an entry is written,
    #: not what it becomes.  `ToolServerSettings.kind` keeps the provenance.
    tools: dict[str, ToolServerSettings] = field(default_factory=dict)
    agent: AgentSettings = field(default_factory=AgentSettings)
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

    def components(self) -> list[str]:
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

    def tool_servers(self) -> list[ToolServerSettings]:
        """The external tool servers the hub should connect to, in file order.

        **One accessor, because "disabled is not connected" has to be one rule.**
        The launcher and the hub both ask this question — the first to decide
        whether there is anything to connect at all, the second to decide what to
        connect — and two spellings of `enabled` is how an entry ends up started
        by one and skipped by the other.
        """
        return [server for server in self.tools.values() if server.enabled]


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
            # A component of its own: keeping turns is one job, and it is not a
            # wire protocol like the model backends.
            "db": ServerSettings(port=8010),
            # The tools slife2 ships — `echo`, `now`, `calc` — served like
            # anybody else's, because the hub is the one place that decides what
            # the model may call; see `slife2.builtins` and DESIGN.md §8.  It is
            # not special to the hub, which asks every component above for a
            # tool list and keeps the ones marked for the model.
            "builtins": ServerSettings(port=8030),
            # The model's tools, and the only process that holds a tool server's
            # credentials.  It has two sources: every component above, and
            # everything under `tools:` in the config file.
            "toolhub": ServerSettings(port=8020),
            # One port per wire protocol, not per provider: a process speaks one
            # format, and `stream_chat(provider=...)` picks whose credentials.
            "openai-completions": ServerSettings(port=8001),
            "anthropic-messages": ServerSettings(port=8002),
            "openai-responses": ServerSettings(port=8003),
        },
        providers={"deepseek": deepseek},
        tools={},
        agent=AgentSettings(),
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
        max_steps=int(agent_raw.get("max_steps") or base.agent.max_steps),
        system_prompt=_template_path(
            agent_raw.get("system_prompt"), config_dir, base.agent.system_prompt
        ),
    )

    return Config(
        servers=servers,
        providers=providers,
        tools=_tools(raw),
        agent=agent,
        default=str(raw.get("default") or _first_reference(providers)),
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


def _tools(raw: dict[str, Any]) -> dict[str, ToolServerSettings]:
    """Both tool sections, as one mapping of upstreams.

    `tools:` is an MCP server written out; `rest-api:` is a REST API written
    out, and is *expanded* here into the stdio command that serves it.  After
    this function there is only the first kind, which is what keeps the hub from
    having to know that REST exists.

    A name in both sections is refused rather than resolved.  Both would become
    one entry called `{name}__{tool}` in the model's tool list, so which of the
    two won would be invisible at every point a person could look.
    """
    result: dict[str, ToolServerSettings] = {}

    for name, spec in _mapping(raw.get("tools"), "tools").items():
        result[str(name)] = _tool_server(spec, str(name))

    for name, spec in _mapping(raw.get("rest-api"), "rest-api").items():
        if str(name) in result:
            raise ConfigError(
                f"rest-api.{name}: already configured under `tools:`; the two "
                f"would collide as tool names"
            )
        result[str(name)] = _rest_api(spec, str(name))

    return result


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
    )


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
    return ServerSettings(
        host=str(raw.get("host") or default.host),
        port=int(raw.get("port") or default.port),
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

    return ProviderSettings(
        api=api,
        base_url=str(raw.get("base_url") or ""),
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
        context_window=int(raw.get("context_window") or 0),
        max_tokens=_optional_int(raw.get("max_tokens")),
        temperature=_optional_float(raw.get("temperature")),
        top_p=_optional_float(raw.get("top_p")),
        thinking=str(compat.get("thinking") or ""),
        store=None if store_raw is None else bool(store_raw),
    )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


__all__ = [
    "AGENT_SERVER_NAME",
    "API_BACKENDS",
    "API_SERVER_NAMES",
    "BUILTINS_SERVER_NAME",
    "DB_SERVER_NAME",
    "DEFAULT_AGENT",
    "LOCAL_SERVERS",
    "TOOLHUB_SERVER_NAME",
    "DEFAULT_CONFIG_NAME",
    "AgentSettings",
    "Config",
    "ConfigError",
    "ModelSettings",
    "ProviderSettings",
    "ServerSettings",
    "ToolServerSettings",
    "default_config",
    "find_config_path",
    "load",
    "resolve_secret",
]
