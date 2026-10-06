"""slife2.yaml — one config file, read by every process and by the launcher.

The shape has one organising idea: **`servers:` holds addresses, and nothing
else.**  Every component that listens on a port is named there, once, and every
other section refers to it by name — so the launcher knows where to look for a
server, the agent server knows where to find its model, and none of them can
disagree about a port.

Secrets
-------
Values are resolved through the same lenient chain v1 uses, so a config file can
be committed and shared:

1. ``keyring:<service>/<key>``  -> credstore
2. ``${VAR}``                   -> os.environ -> credstore
3. ``${VAR:-default}``          -> os.environ -> credstore -> literal default
4. anything else                -> as-is

Resolution never raises.  A missing secret degrades to its literal form
(``"${DEEPSEEK_API_KEY}"``), which fails loudly at the API call where it is a
readable error, rather than at startup where it is not.

Resolution is *lazy*: the LLM settings keep the raw reference and only touch
credstore when ``api_key`` is read.  The TUI process therefore never opens the
OS keyring, and a headless CI run never reaches for a backend that does not
exist.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


class ConfigError(Exception):
    """A config file was found and could not be used.

    A *missing* file is not an error — the defaults are the config — but a
    malformed one is fatal, because silently ignoring a file the user wrote is
    the worst of the three possible behaviours.
    """


#: Names of the MCP servers this distribution ships.  The launcher can only
#: start what it has a module for, so an unknown name in `servers:` is a typo
#: worth reporting rather than a server it quietly fails to run.
KNOWN_SERVERS = ("agent", "llm-openai", "llm-anthropic")

#: Matches `${VAR}` and `${VAR:-default}`.  The name is deliberately restricted
#: to shell-safe identifiers so a literal `${...}` in a prompt does not become a
#: lookup.
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

#: Environment variable naming an explicit config file.
CONFIG_ENV_VAR = "SLIFE2_CONFIG"

#: The file looked for in the working directory when nothing else is named.
DEFAULT_CONFIG_NAME = "slife2.yaml"

#: The agent label used when `--agent` is not given.
DEFAULT_AGENT = "slife2"


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

    Handles references embedded in a larger string as well as whole-value ones,
    so ``base_url: "https://${HOST}/v1"`` behaves as the reader expects.

    Takes `object` because YAML hands back ints and bools for unquoted scalars,
    and an API key written as a bare number should become a string rather than
    an exception.
    """
    if not isinstance(value, str):
        return str(value)

    # `keyring:` URIs are whole-value only; a URI spliced into a larger string
    # is ambiguous and is left alone.
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
class ProviderSettings:
    """One model the agent loop can talk to, and who serves it.

    `server` names an entry in `servers:` — the launcher starts those on demand
    and shares them.  A provider may instead carry a bare `url` pointing at a
    server this installation does not manage (something already running
    elsewhere), in which case `server` is None and the launcher leaves it alone.
    """

    model: str
    url: str
    server: str | None = None


@dataclass(frozen=True)
class AgentSettings:
    """How the agent loop behaves.  Not an identity — that is `--agent`."""

    max_steps: int = 16
    system_prompt: str = "You are slife2, a terminal agent. Be concise."


@dataclass(frozen=True)
class OpenAISettings:
    """The openai-compatible LLM server: what it calls, and with what key."""

    base_url: str | None = None
    #: The reference exactly as written in the file — `${DEEPSEEK_API_KEY}`, a
    #: `keyring:` URI, or a literal.  Read `api_key` to resolve it.
    api_key_ref: str = ""
    #: Ask the provider to report token usage on the final stream chunk.
    #: OpenAI and DeepSeek do this via `stream_options`, which is an extension:
    #: some OpenAI-compatible servers reject the parameter outright with a 400.
    #: Turn it off for one of those and the only loss is the token counts.
    stream_usage: bool = True

    @property
    def api_key(self) -> str:
        """The resolved key.  Touches credstore, so call it where it is needed."""
        return resolve_secret(self.api_key_ref)


@dataclass(frozen=True)
class AnthropicSettings:
    """The Anthropic LLM server: what it calls, and with what key."""

    api_key_ref: str = ""
    #: Anthropic requires a max_tokens on every request; the OpenAI-compatible
    #: API does not, so there is no shared setting for it.
    max_tokens: int = 4096

    @property
    def api_key(self) -> str:
        """The resolved key.  See :attr:`OpenAISettings.api_key`."""
        return resolve_secret(self.api_key_ref)


@dataclass(frozen=True)
class Config:
    """Every section of one config file, already defaulted."""

    #: name -> where it listens.  The single source of truth for addresses.
    servers: dict[str, ServerSettings] = field(default_factory=dict)
    providers: dict[str, ProviderSettings] = field(default_factory=dict)
    default_provider: str = ""
    agent: AgentSettings = field(default_factory=AgentSettings)
    llm_openai: OpenAISettings = field(default_factory=OpenAISettings)
    llm_anthropic: AnthropicSettings = field(default_factory=AnthropicSettings)
    tui_url: str = ""

    def server(self, name: str) -> ServerSettings:
        """Where a named server listens.

        Raises:
            ConfigError: If the name is unknown — which at this point means a
                caller asked for a server that is not in `KNOWN_SERVERS`.
        """
        try:
            return self.servers[name]
        except KeyError:
            known = ", ".join(sorted(self.servers)) or "(none)"
            raise ConfigError(f"unknown server {name!r}; known: {known}") from None

    def provider(self, name: str | None = None) -> ProviderSettings:
        """Look up a provider, defaulting to the configured default.

        Raises:
            ConfigError: If the name is unknown, or nothing is configured.  Both
                are config mistakes that deserve a message naming the valid
                choices rather than a KeyError.
        """
        key = name or self.default_provider
        if not key:
            raise ConfigError("no provider configured; set `default:`")
        try:
            return self.providers[key]
        except KeyError:
            known = ", ".join(sorted(self.providers)) or "(none)"
            raise ConfigError(f"unknown provider {key!r}; known: {known}") from None


def default_config() -> Config:
    """The config used when no file is found.

    Everything here is also what `slife2.yaml` says, which is why that file can
    be deleted without changing behaviour — see the file's own header.
    """
    servers = {
        "agent": ServerSettings(port=8000),
        "llm-openai": ServerSettings(port=8001),
        "llm-anthropic": ServerSettings(port=8002),
    }
    providers = {
        "deepseek": ProviderSettings(
            model="deepseek-flash", url=servers["llm-openai"].url, server="llm-openai"
        ),
        "claude": ProviderSettings(
            model="claude-sonnet-5-5",
            url=servers["llm-anthropic"].url,
            server="llm-anthropic",
        ),
    }
    return Config(
        servers=servers,
        providers=providers,
        default_provider="deepseek",
        agent=AgentSettings(),
        llm_openai=OpenAISettings(
            base_url="https://api.deepseek.com/v1",
            api_key_ref="${DEEPSEEK_API_KEY}",
        ),
        llm_anthropic=AnthropicSettings(api_key_ref="${ANTHROPIC_API_KEY}"),
        tui_url=servers["agent"].url,
    )


def find_config_path(explicit: str | Path | None = None) -> Path | None:
    """Locate the config file: argument, then env var, then working directory.

    Returns None when nothing is named and nothing is there, which is not an
    error — see :func:`load`.
    """
    if explicit is not None:
        return Path(explicit)
    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        return Path(from_env)
    candidate = Path.cwd() / DEFAULT_CONFIG_NAME
    return candidate if candidate.is_file() else None


def load(explicit: str | Path | None = None) -> Config:
    """Load the config, falling back to :func:`default_config`.

    A named path that does not exist *is* an error (the user asked for that
    file); an unnamed one that does not exist is not.

    Raises:
        ConfigError: If a file exists but cannot be read or parsed.
    """
    explicit_given = explicit is not None or os.environ.get(CONFIG_ENV_VAR)
    path = find_config_path(explicit)

    if path is None or not path.is_file():
        if explicit_given:
            raise ConfigError(f"config file not found: {path}")
        return default_config()

    yaml = YAML(typ="safe")
    try:
        raw = yaml.load(path) or {}
    except (YAMLError, OSError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    return _build(raw)


def _mapping(raw: Any) -> dict[str, Any]:
    """Coerce a config value to a mapping, or raise a message naming the section."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("expected a mapping")
    return raw


def _build(raw: dict[str, Any]) -> Config:
    """Assemble a Config from a parsed mapping, defaulting what is absent."""
    base = default_config()

    # --- servers ------------------------------------------------------------
    servers = dict(base.servers)
    for name, spec in _mapping(raw.get("servers")).items():
        if name not in KNOWN_SERVERS:
            raise ConfigError(
                f"servers.{name}: not a server this distribution ships "
                f"(known: {', '.join(KNOWN_SERVERS)})"
            )
        try:
            servers[str(name)] = _server_settings(spec, servers[str(name)])
        except ConfigError as exc:
            raise ConfigError(f"servers.{name}: {exc}") from exc

    # --- providers ----------------------------------------------------------
    providers_raw = _mapping(raw.get("providers"))
    if providers_raw:
        providers: dict[str, ProviderSettings] = {}
        for name, spec in providers_raw.items():
            providers[str(name)] = _provider_settings(spec, servers, str(name))
    else:
        providers = {
            name: replace(p, url=servers[p.server].url)
            for name, p in base.providers.items()
            if p.server in servers
        }

    # --- the rest -----------------------------------------------------------
    agent_raw = _mapping(raw.get("agent"))
    openai_raw = _mapping(raw.get("llm_openai"))
    anthropic_raw = _mapping(raw.get("llm_anthropic"))
    tui_raw = _mapping(raw.get("tui"))

    agent = AgentSettings(
        max_steps=int(agent_raw.get("max_steps") or base.agent.max_steps),
        system_prompt=str(agent_raw.get("system_prompt") or base.agent.system_prompt),
    )

    return Config(
        servers=servers,
        providers=providers,
        default_provider=str(raw.get("default") or base.default_provider),
        agent=agent,
        llm_openai=OpenAISettings(
            base_url=(
                str(openai_raw["base_url"])
                if openai_raw.get("base_url")
                else base.llm_openai.base_url
            ),
            api_key_ref=str(openai_raw.get("api_key") or base.llm_openai.api_key_ref),
            stream_usage=bool(
                openai_raw.get("stream_usage", base.llm_openai.stream_usage)
            ),
        ),
        llm_anthropic=AnthropicSettings(
            api_key_ref=str(
                anthropic_raw.get("api_key") or base.llm_anthropic.api_key_ref
            ),
            max_tokens=int(
                anthropic_raw.get("max_tokens") or base.llm_anthropic.max_tokens
            ),
        ),
        # The TUI's URL defaults to wherever the agent server was told to
        # listen, not to a second hard-coded address that could disagree.
        tui_url=str(tui_raw.get("url") or servers["agent"].url),
    )


def _server_settings(raw: Any, default: ServerSettings) -> ServerSettings:
    """Build ServerSettings from a config mapping, falling back per field."""
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ConfigError("expected a mapping with host/port/path")
    return ServerSettings(
        host=str(raw.get("host") or default.host),
        port=int(raw.get("port") or default.port),
        path=str(raw.get("path") or default.path),
    )


def _provider_settings(
    raw: Any, servers: dict[str, ServerSettings], name: str
) -> ProviderSettings:
    """Build a provider, resolving its URL from `servers` when it names one."""
    if not isinstance(raw, dict):
        raise ConfigError(f"providers.{name}: expected a mapping")

    model = str(raw.get("model") or "")
    if not model:
        raise ConfigError(f"providers.{name}: needs a `model`")

    server_name = raw.get("server")
    if server_name is not None:
        server_name = str(server_name)
        if server_name not in servers:
            known = ", ".join(sorted(servers)) or "(none)"
            raise ConfigError(
                f"providers.{name}: unknown server {server_name!r}; known: {known}"
            )
        return ProviderSettings(
            model=model, url=servers[server_name].url, server=server_name
        )

    url = raw.get("url")
    if not url:
        raise ConfigError(f"providers.{name}: needs either a `server` name or a `url`")
    # A bare url means a server this installation does not manage — already
    # running elsewhere — so the launcher will not try to start it.
    return ProviderSettings(model=model, url=str(url), server=None)
