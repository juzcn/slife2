"""slife2.yaml — one config file, read by all four processes.

Each component reads only its own section, so the addresses are written once and
cannot drift apart: the TUI derives its URL from what the agent server is told to
listen on, and the agent server's provider table points at the ports the LLM
servers are told to listen on.

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

Resolution is *lazy*: :class:`OpenAISettings` keeps the raw reference and only
touches credstore when ``api_key`` is read.  The TUI process therefore never
opens the OS keyring, and a headless CI run never reaches for a backend that
does not exist.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
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


#: Matches `${VAR}` and `${VAR:-default}`.  The name is deliberately restricted
#: to shell-safe identifiers so a literal `${...}` in a prompt does not become a
#: lookup.
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

#: Environment variable naming an explicit config file.
CONFIG_ENV_VAR = "SLIFE2_CONFIG"

#: The file looked for in the working directory when nothing else is named.
DEFAULT_CONFIG_NAME = "slife2.yaml"


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
    """One LLM MCP server the agent loop may talk to."""

    url: str
    model: str


@dataclass(frozen=True)
class AgentSettings:
    """The agent loop's own settings."""

    server: ServerSettings = field(default_factory=ServerSettings)
    max_steps: int = 16
    system_prompt: str = "You are slife2, a terminal agent. Be concise."
    providers: dict[str, ProviderSettings] = field(default_factory=dict)
    default: str = ""

    def provider(self, name: str | None = None) -> ProviderSettings:
        """Look up a provider, defaulting to the configured default.

        Raises:
            ConfigError: If the name is unknown, or nothing is configured.  Both
                are config mistakes that deserve a message naming the valid
                choices rather than a KeyError.
        """
        key = name or self.default
        if not key:
            raise ConfigError("no provider configured; set agent.default")
        try:
            return self.providers[key]
        except KeyError:
            known = ", ".join(sorted(self.providers)) or "(none)"
            raise ConfigError(f"unknown provider {key!r}; known: {known}") from None


@dataclass(frozen=True)
class OpenAISettings:
    """The openai-compatible LLM server: where it listens, what it calls."""

    server: ServerSettings = field(default_factory=lambda: ServerSettings(port=8001))
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
    """The Anthropic LLM server: where it listens, what it calls."""

    server: ServerSettings = field(default_factory=lambda: ServerSettings(port=8002))
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

    agent: AgentSettings
    llm_openai: OpenAISettings
    llm_anthropic: AnthropicSettings
    tui_url: str


def _server_settings(raw: Any, default: ServerSettings) -> ServerSettings:
    """Build ServerSettings from a config mapping, falling back per field."""
    if not isinstance(raw, dict):
        return default
    host = str(raw.get("host") or default.host)
    port = int(raw.get("port") or default.port)
    path = str(raw.get("path") or default.path)
    return ServerSettings(host=host, port=port, path=path)


def default_config() -> Config:
    """The config used when no file is found.

    Ports are assigned so the four processes do not collide, and the provider
    table points at the two LLM servers' default ports.  Nothing here is a
    secret, so this is usable as-is for a first run once a key is exported.
    """
    agent_server = ServerSettings(port=8000)
    openai_server = ServerSettings(port=8001)
    anthropic_server = ServerSettings(port=8002)
    providers = {
        "deepseek": ProviderSettings(url=openai_server.url, model="deepseek-chat"),
        "claude": ProviderSettings(url=anthropic_server.url, model="claude-sonnet-5-5"),
    }
    return Config(
        agent=AgentSettings(
            server=agent_server, providers=providers, default="deepseek"
        ),
        llm_openai=OpenAISettings(
            server=openai_server,
            base_url="https://api.deepseek.com/v1",
            api_key_ref="${DEEPSEEK_API_KEY}",
        ),
        llm_anthropic=AnthropicSettings(
            server=anthropic_server, api_key_ref="${ANTHROPIC_API_KEY}"
        ),
        tui_url=agent_server.url,
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


def _build(raw: dict[str, Any]) -> Config:
    """Assemble a Config from a parsed mapping, defaulting what is absent."""
    base = default_config()

    agent_raw = raw.get("agent") or {}
    if not isinstance(agent_raw, dict):
        raise ConfigError("agent: expected a mapping")

    agent_server = _server_settings(agent_raw.get("server"), base.agent.server)

    providers_raw = agent_raw.get("providers") or {}
    if not isinstance(providers_raw, dict):
        raise ConfigError("agent.providers: expected a mapping")
    providers = {
        str(name): ProviderSettings(
            url=str(spec.get("url") or ""),
            model=str(spec.get("model") or ""),
        )
        for name, spec in providers_raw.items()
        if isinstance(spec, dict)
    } or base.agent.providers

    agent = AgentSettings(
        server=agent_server,
        max_steps=int(agent_raw.get("max_steps") or base.agent.max_steps),
        system_prompt=str(agent_raw.get("system_prompt") or base.agent.system_prompt),
        providers=providers,
        default=str(agent_raw.get("default") or base.agent.default),
    )

    openai_raw = raw.get("llm_openai") or {}
    anthropic_raw = raw.get("llm_anthropic") or {}
    tui_raw = raw.get("tui") or {}
    if not all(isinstance(s, dict) for s in (openai_raw, anthropic_raw, tui_raw)):
        raise ConfigError("llm_openai / llm_anthropic / tui: expected mappings")

    return Config(
        agent=agent,
        llm_openai=OpenAISettings(
            server=_server_settings(openai_raw.get("server"), base.llm_openai.server),
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
            server=_server_settings(
                anthropic_raw.get("server"), base.llm_anthropic.server
            ),
            api_key_ref=str(
                anthropic_raw.get("api_key") or base.llm_anthropic.api_key_ref
            ),
            max_tokens=int(
                anthropic_raw.get("max_tokens") or base.llm_anthropic.max_tokens
            ),
        ),
        # The TUI's URL defaults to wherever the agent server was told to
        # listen, not to a second hard-coded address that could disagree.
        tui_url=str(tui_raw.get("url") or agent.server.url),
    )
