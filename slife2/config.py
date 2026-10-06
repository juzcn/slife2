"""slife2.yaml — one config file, read by every process and by the launcher.

The shape is slife v1's, because it already answers the questions a model
configuration has to answer:

    providers:
      deepseek:
        api: openai-completions          # the wire protocol
        base_url: https://api.deepseek.com
        api_key: ${DEEPSEEK_API_KEY}
        server: {host: 127.0.0.1, port: 8001, path: /mcp}
        models:
          - model: deepseek-flash        # the API name, and the local id
            name: DeepSeek Flash         # what to call it on screen
            reasoning: true
            input: [text, image]
            context_window: 1000000
            max_tokens: 384000
            temperature: 0.7
            top_p: 1.0

Three things are worth stating outright.

**A provider holds its own credentials, and therefore its own process.**  A
server process can only hold one `base_url` and one key, so two
OpenAI-compatible providers cannot share one — the launcher starts one server
per provider, and a key exists only in the process that needs it.  This is why
`server:` sits inside the provider rather than in a separate address table.

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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from slife2.paths import data_dir

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


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
}

#: Matches `${VAR}` and `${VAR:-default}`.  The name is deliberately restricted
#: to shell-safe identifiers so a literal `${...}` in a prompt does not become a
#: lookup.
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

#: The config file's name inside the data directory.
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
    #: Modalities accepted.  Carried so a caller can refuse to attach an image
    #: to a model that cannot read one; slife2 attaches nothing yet, so today
    #: this is the config that says so.
    input: tuple[str, ...] = ("text",)
    #: Token budget, used for the context percentage in the status bar.
    context_window: int = 0
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    #: `compat.thinking` — `enabled`, `disabled`, or `omit` for gateways that
    #: reject the standard shape while reasoning anyway.
    thinking: str = ""

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
class Config:
    """Every section of one config file, already defaulted."""

    #: name -> where it listens, keyed by `"agent"` and by each `api` in use.
    servers: dict[str, ServerSettings] = field(default_factory=dict)
    providers: dict[str, ProviderSettings] = field(default_factory=dict)
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

    def apis_in_use(self) -> list[str]:
        """The wire protocols some provider uses, in a stable order.

        This is what the launcher starts: a family with no providers has nothing
        to serve, and starting it would be a process holding nobody's key.
        """
        used = {p.api for p in self.providers.values()}
        return [api for api in API_BACKENDS if api in used]


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
            "memory": ServerSettings(port=8010),
            # One port per wire protocol, not per provider: a process speaks one
            # format, and `stream_chat(provider=...)` picks whose credentials.
            "openai-completions": ServerSettings(port=8001),
            "anthropic-messages": ServerSettings(port=8002),
        },
        providers={"deepseek": deepseek},
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
        if name not in ("agent", "memory") and name not in API_BACKENDS:
            raise ConfigError(
                f"servers.{name}: not a server this system runs "
                f"(known: agent, memory, {', '.join(API_BACKENDS)})"
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
        agent=agent,
        default=str(raw.get("default") or _first_reference(providers)),
    )


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
    )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


__all__ = [
    "API_BACKENDS",
    "CONFIG_ENV_VAR",
    "DEFAULT_AGENT",
    "DEFAULT_CONFIG_NAME",
    "AgentSettings",
    "Config",
    "ConfigError",
    "ModelSettings",
    "ProviderSettings",
    "ServerSettings",
    "default_config",
    "find_config_path",
    "load",
    "resolve_secret",
    "replace",
]
