"""The canonical config objects for the three tool-server families.

Same shape and same rule as `slife2.libs.host.model_config`: the YAML is an
artifact, the object is the truth, and the loader, `*_get` and `*_set` validate
against this one definition.  A hand-written typo is refused where it is read;
a `*_set` that leaves a field out is a schema error, not a silent reset.

**Every field is required here; the loader fills the defaults.**  The file may
omit `args`, `env`, `cwd` and the rest; `load_mcp`, `load_rest_api` and
`load_cli` complete the object before validation, so a caller always sees the
whole entry.  The tools complete nothing: their `config` is the whole entry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from slife2.libs.host.validation import validation_refusal

Transport = Literal["stdio", "http"]


class McpConfig(BaseModel):
    """One `mcp:` entry: one transport, and the fields that belong to it."""

    model_config = ConfigDict(strict=True, extra="forbid")

    transport: Transport = Field(
        description="`stdio` for a process slife2 starts (`command`), `http` "
        "for an endpoint somebody else runs (`url`)."
    )
    command: str | None = Field(
        description="stdio: the program to start (npx, uvx, python)."
    )
    args: list[str] | None = Field(description="stdio: its arguments.")
    env: dict[str, str] | None = Field(
        description="stdio: environment overrides; `${VAR}` resolves when the "
        "connection starts."
    )
    url: str | None = Field(description="http: the endpoint (SSE or Streamable HTTP).")
    headers: dict[str, str] | None = Field(
        description="http: request headers; `${VAR}` for anything secret."
    )
    cwd: str = Field(
        description="stdio: the process's working directory; empty is the data "
        "directory."
    )
    description: str = Field(description="What it is for.")
    enabled: bool = Field(
        description="False keeps the entry written down and does not connect it."
    )

    @model_validator(mode="after")
    def _one_transport(self) -> McpConfig:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio needs `command` — the program to start")
            if self.url:
                raise ValueError("stdio must not name `url` — choose one transport")
        else:
            if not self.url:
                raise ValueError("http needs `url` — the endpoint to connect to")
            if self.command:
                raise ValueError("http must not name `command` — choose one transport")
        return self


class RestApiConfig(BaseModel):
    """One `rest-api:` entry in its declarative form: a spec, a base URL, a key."""

    model_config = ConfigDict(strict=True, extra="forbid")

    spec: str = Field(
        min_length=1,
        description="The OpenAPI document's URL (JSON or YAML); it is fetched "
        "by the proxy.",
    )
    base_url: str = Field(
        description="Where the API actually is; empty uses the server the "
        "document names."
    )
    api_key: str = Field(
        description="A `${VAR}` reference; empty for an API that needs no key."
    )
    args: list[str] | None = Field(description="Extra arguments for the proxy.")
    env: dict[str, str] | None = Field(
        description="Extra environment for the proxy; `${VAR}` resolves when "
        "the proxy starts."
    )
    cwd: str = Field(
        description="The proxy's working directory; empty is the data directory."
    )
    description: str = Field(description="What the API is for.")
    enabled: bool = Field(
        description="False keeps the entry written down and does not serve it."
    )


class CliConfig(BaseModel):
    """One `cli:` entry: a program already on this machine."""

    model_config = ConfigDict(strict=True, extra="forbid")

    command: str = Field(
        description="The invocation, resolved on `PATH`; it may be more than "
        "one word (`python -m mytool`).",
    )
    description: str = Field(
        description="What it does, in the words a search should find it by."
    )
    install: str = Field(
        description="How to get it, for somebody who does not have it."
    )
    source: dict[str, str] = Field(
        description="Where the entry came from — `url`, `type`, `version`."
    )
    enabled: bool = Field(description="False records it and keeps it out of search.")

    @model_validator(mode="after")
    def _needs_command(self) -> CliConfig:
        if not self.command.strip():
            raise ValueError(
                "needs `command` — the program the model may run (e.g. `gh`, "
                "or `python -m mytool`)"
            )
        return self


#: What a file that omits one `mcp:` field means.
MCP_DEFAULTS: dict[str, Any] = {
    "args": None,
    "env": None,
    "headers": None,
    "cwd": "",
    "description": "",
    "enabled": True,
}

#: What a file that omits one `rest-api:` field means.
REST_DEFAULTS: dict[str, Any] = {
    "base_url": "",
    "api_key": "",
    "args": None,
    "env": None,
    "cwd": "",
    "description": "",
    "enabled": True,
}

#: What a file that omits one `cli:` field means.
CLI_DEFAULTS: dict[str, Any] = {
    "description": "",
    "install": "",
    "source": {},
    "enabled": True,
}


def _check_enabled(data: Mapping[str, Any], where: str) -> None:
    """The switch is a real YAML boolean, and absence means on.

    The same rule and the same sentence as `slife2.libs.host.config._enabled`:
    a YAML 1.2 `no`/`off` is a string, every non-empty string is true, and a
    switch that decides whether a process is dialled is refused rather than
    guessed at.
    """
    value = data.get("enabled", True)
    if not isinstance(value, bool):
        raise ValueError(
            f"{where}.enabled: expected `true` or `false`, got {value!r} — YAML "
            f"spells a boolean as one, so `no`, `off` and a quoted 'false' are "
            f"strings; leave the key out for true"
        )


def load_mcp(raw: Any, *, where: str) -> McpConfig:
    """One `mcp:` entry, validated and completed with its defaults."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: expected a mapping")
    data = {str(key): value for key, value in raw.items()}
    if "transport" in data:
        raise ValueError(
            f"{where}: `transport` is derived from `command`/`url`; do not write it"
        )
    command = str(data.get("command") or "").strip()
    url = str(data.get("url") or "").strip()
    if command and url:
        raise ValueError(
            f"{where}: has both `command` and `url`; one entry is one "
            f"transport — write two entries if you meant two servers"
        )
    if not command and not url:
        raise ValueError(
            f"{where}: needs `command` (a program to start) or `url` (an "
            f"endpoint to connect to)"
        )
    data["command"] = command or None
    data["url"] = url or None
    data["transport"] = "http" if url else "stdio"
    _check_enabled(data, where)
    for key in ("args", "env", "headers", "cwd", "description"):
        if data.get(key) is None:
            data.pop(key, None)
    data = {**MCP_DEFAULTS, **data}
    try:
        return McpConfig.model_validate(data)
    except ValidationError as exc:
        raise validation_refusal(where, exc) from exc


def load_rest_api(raw: Any, *, where: str) -> RestApiConfig:
    """One `rest-api:` entry in the spec form, validated and completed.

    The `command:` form is a different spelling for a proxy somebody else pins;
    it has no spec and this object does not describe it.  A caller that reaches
    one is told to edit the file, rather than handed an object that omits the
    command it actually runs.
    """
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: expected a mapping")
    data = {str(key): value for key, value in raw.items()}
    if data.get("command"):
        raise ValueError(
            f"{where}: uses the `command:` form, which `rest_api_get` and "
            f"`rest_api_set` do not edit — they edit the `spec:` form; edit "
            f"slife2.yaml directly"
        )
    if not data.get("spec"):
        raise ValueError(f"{where}: needs `spec` — the OpenAPI document's URL or path")
    _check_enabled(data, where)
    for key in ("base_url", "api_key", "args", "env", "cwd", "description"):
        if data.get(key) is None:
            data.pop(key, None)
    data = {**REST_DEFAULTS, **data}
    try:
        return RestApiConfig.model_validate(data)
    except ValidationError as exc:
        raise validation_refusal(where, exc) from exc


def load_cli(raw: Any, *, where: str) -> CliConfig:
    """One `cli:` entry, validated and completed with its defaults."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: expected a mapping")
    data = {str(key): value for key, value in raw.items()}
    command = str(data.get("command") or "")
    if not command:
        raise ValueError(
            f"{where}: needs `command` — the program the model may run "
            f"(e.g. `gh`, or `python -m mytool`)"
        )
    data["command"] = command
    _check_enabled(data, where)
    for key in ("description", "install"):
        if data.get(key) is None:
            data.pop(key, None)
    source = data.get("source")
    if source is None:
        data.pop("source", None)
    elif isinstance(source, Mapping):
        # The file's provenance is free-form metadata; the runtime has always
        # read it as text, so a numeric `version:` is not a reason to refuse it.
        data["source"] = {str(key): str(value) for key, value in source.items()}
    data = {**CLI_DEFAULTS, **data}
    try:
        return CliConfig.model_validate(data)
    except ValidationError as exc:
        raise validation_refusal(where, exc) from exc


def mcp_entry(config: McpConfig) -> dict[str, Any]:
    """An `mcp:` entry, from the canonical object.

    The transport decides which of `command`/`url` is written; the file does not
    carry `transport`, because it is a fact about which key is present.  A value
    equal to the loader's default is omitted, so a `set` from `get` leaves the
    file's shape alone.
    """
    entry: dict[str, Any] = {}
    if config.transport == "stdio":
        entry["command"] = config.command
    else:
        entry["url"] = config.url
    if config.args is not None:
        entry["args"] = list(config.args)
    if config.env is not None:
        entry["env"] = dict(config.env)
    if config.headers is not None:
        entry["headers"] = dict(config.headers)
    if config.cwd:
        entry["cwd"] = config.cwd
    if config.description:
        entry["description"] = config.description
    if not config.enabled:
        entry["enabled"] = False
    return entry


def rest_entry(config: RestApiConfig) -> dict[str, Any]:
    """A `rest-api:` entry, from the canonical object."""
    entry: dict[str, Any] = {"spec": config.spec}
    if config.base_url:
        entry["base_url"] = config.base_url
    if config.api_key:
        entry["api_key"] = config.api_key
    if config.args is not None:
        entry["args"] = list(config.args)
    if config.env is not None:
        entry["env"] = dict(config.env)
    if config.cwd:
        entry["cwd"] = config.cwd
    if config.description:
        entry["description"] = config.description
    if not config.enabled:
        entry["enabled"] = False
    return entry


def cli_entry(config: CliConfig) -> dict[str, Any]:
    """A `cli:` entry, from the canonical object."""
    entry: dict[str, Any] = {"command": config.command}
    if config.description:
        entry["description"] = config.description
    if config.install:
        entry["install"] = config.install
    if config.source:
        entry["source"] = dict(config.source)
    if not config.enabled:
        entry["enabled"] = False
    return entry
