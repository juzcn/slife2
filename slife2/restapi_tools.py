"""slife2-restapi-tools — the `rest-api:` section, held like any other server.

A REST API is written down as a spec, a base URL and a key, and the config layer
expands it into the `uvx mcp-openapi-proxy` command that serves it — so what
arrives here is an ordinary stdio MCP server like every entry under `tools:`, and
this plugin holds it the same way.  That is the whole of the difference between
the two: the section, and the shape an entry is written in.

**Why it is a plugin of its own anyway.**  Because the sections are separate, and
an operator's mental model is the section: `rest-api:` entries are a shorter way
to say the same thing, not a second mechanism, and a family that owns its config
is a family whose next change has somewhere to land.  The machinery is shared —
see `slife2.toolfamily` — so this costs a module and a port, not a second
implementation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP

from slife2.audience import FOR_THE_MODEL
from slife2.config import (
    Config,
    ConfigError,
    ToolServerSettings,
    _rest_api,
    load,
    resolve_secret,
)
from slife2.gateway import ClientFactory
from slife2.mcp_server import serve_plugin
from slife2.toolfamily import (
    Family,
    FamilyWords,
    apply_and_report,
    build_family_server,
    list_entries,
    list_entry_tools,
    prepare_entry,
    remove_entry,
    state_word,
    switch_entry,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-restapi-tools"

#: This server's key in the config's `servers:` table.
CONFIG_KEY = "restapi-tools"

#: The catalogue category its sources are filed under.  The db keeps `rest`
#: apart from `mcp` for the reason the sections are apart: it is the answer to
#: the first question anyone debugging asks, which is why a server they never
#: wrote a `command:` for is running `uvx`.
CATEGORY = "rest"

#: The config section this family owns, and the only thing it tells
#: `slife2.configfile`: that module edits a section and knows nothing about what
#: is in one.
SECTION = "rest-api"

INSTRUCTIONS = (
    "The REST APIs the config's `rest-api:` section expands into MCP servers. "
    "This server is called by the toolhub, not by a model: it holds the "
    "connections, and the tools themselves reach the model through the hub."
)

#: What this family calls things, for the sentences `slife2.toolfamily` writes
#: once for both connection families.  An API is *served* where a server is
#: *connected*, and it exposes *operations* where one of ours exposes tools —
#: the same mechanism under the two names a person already uses for them.
WORDS = FamilyWords(
    section=SECTION,
    plural="REST APIs",
    entry="an API",
    unit="operation",
    list_tool="rest_api_list",
    set_tool="rest_api_set",
    remove_log="rest_api_removed",
    present="serving",
    past="served",
    starting="starting",
)


# ── The management tools ────────────────────────────────────────────────
# The same five names as the MCP family, over this section — v1's split, kept
# because a REST API and an MCP server are different things to a person even
# when one is served by the other.  What differs is only the entry: an API is
# written as a spec and a base URL, and the config layer expands it into the
# `mcp-openapi-proxy` command that serves it (`_rest_api`), so nothing here
# knows what the proxy is called.


def _reference(value: str) -> str:
    """A credential written the way the config writes one: `${VAR}`.

    **The name, not the secret**, which is the whole of what makes this tool
    safe to hand a model.  v1's `api_key` is a *variable name* for the same
    reason; a value that is already a reference (or a `keyring:` URI) is left
    exactly as given, so a caller that knows the syntax is not second-guessed.
    """
    credential = value.strip()
    if credential.startswith("${") or credential.startswith("keyring:"):
        return credential
    return f"${{{credential}}}"


def _validate_http_url(url: str, what: str) -> str:
    """Require an `http(s)` URL with a host — v1's check, and v1's reason.

    **The proxy child fetches the spec**, so a `file://` or an internal-host URL
    handed to `spec` is a way to make a slife2 process read something it was
    never pointed at.  Private addresses are deliberately still allowed: a local
    API is a legitimate thing to configure, and blocking it would be this check
    guessing at somebody's network.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"{what} must be an http(s) URL with a host, got {url!r}")
    return url


def _prepare(
    config: Config, name: str, spec: str, base_url: str, api_key: str, **rest: Any
) -> tuple[ToolServerSettings | None, str]:
    """Validate a `rest-api:` entry and write it, or say why not."""

    def build() -> tuple[ToolServerSettings, dict[str, Any]]:
        # The two URL checks run here, inside the shared helper's `try`: a
        # `file://` spec is refused as a `ConfigError` like any other bad entry.
        entry: dict[str, Any] = {
            "spec": _validate_http_url(spec, "spec") if spec else None,
            "base_url": _validate_http_url(base_url, "base_url") if base_url else None,
            "api_key": _reference(api_key) if api_key else None,
            **rest,
        }
        return _rest_api(entry, name), entry

    return prepare_entry(config, name, build, section=SECTION)


def _unresolved(api_key: str) -> str:
    """A sentence about a credential that is not there yet, or `""`.

    **Said at the moment it is written, and not saved for the call that fails**:
    a `${VAR}` nothing resolves to is not an error — the config keeps the
    reference and the failure surfaces where the value is used (`slife2.config`
    says so) — but for an *API key* the place it surfaces is a 401 from somebody
    else's server, one turn later, naming nothing.  One line here is the
    difference between that and knowing now.
    """
    if not api_key:
        return ""
    reference = _reference(api_key)
    if reference.startswith("keyring:"):
        return ""
    if resolve_secret(reference) != reference:
        return ""
    return (
        f"\nNote: {reference} does not resolve to anything yet — set it with "
        f"`credstore set {reference[2:-1]}` (or export it) before this API is "
        f"called."
    )


def _describe(one: Any, name: str, raw: Mapping[str, Any]) -> str:
    """One line of `rest_api_list` — the spec, and whether it is serving.

    The spec and the base URL are what this family *is*, and they are not what
    the entry becomes: the config layer has already turned them into a proxy
    command by the time a `ToolServerSettings` exists, so a listing built from
    the held settings would print `uvx mcp-openapi-proxy` and lose the only
    thing a reader wanted.
    """
    state = state_word(raw, one)
    lines = [f"- {name} [{state}]"]
    if spec := raw.get("spec"):
        lines.append(f"    spec     {spec}")
    else:
        lines.append(f"    command  {raw.get('command', '(none)')}")
    if base_url := raw.get("base_url"):
        lines.append(f"    base_url {base_url}")
    if api_key := raw.get("api_key"):
        lines.append(f"    auth     {api_key}")
    if description := str(raw.get("description") or ""):
        lines.append(f"    {description}")
    return "\n".join(lines)


def register_management(mcp: FastMCP, family: Family) -> None:
    """Serve the `rest_api_*` tools on this family's server."""

    @mcp.tool(name="rest_api_list", meta=FOR_THE_MODEL)
    async def rest_api_list() -> str:
        """List the REST APIs configured under `rest-api:`, with their state.

        Each entry as the file writes it — the OpenAPI spec, the base URL, which
        credential it uses — and whether it is serving. A REST API is expanded
        into one MCP server per entry, so its state is a server's state.
        """
        return await list_entries(family, words=WORDS, describe=_describe)

    @mcp.tool(name="rest_api_set", meta=FOR_THE_MODEL)
    async def rest_api_set(
        name: str,
        spec: str,
        base_url: str,
        api_key: str = "",
        description: str = "",
        enabled: bool = True,
        autoload: bool = False,
    ) -> str:
        """Add or update a REST API under `rest-api:` (upsert; idempotent).

        Every endpoint in the OpenAPI document becomes a tool named
        `{name}__{operation}`, so a published API is worth checking before you
        add it: one spec can be hundreds of tools. Set `enabled: false` — or
        turn it off afterwards — to keep it written down without serving it.

        **This is the whole entry**: an `api_key` or `description` left out is
        not kept from an older version, so restate everything the API should
        have.

        Args:
            name: The API's name; its tools reach you as `{name}__{operation}`.
            spec: The OpenAPI document's URL (JSON or YAML). It is fetched, so
                it has to be http(s) and reachable.
            base_url: Where the API actually is, which overrides whatever server
                the document names — e.g. https://api.github.com.
            api_key: The *name* of the credential, not the secret —
                `GITHUB_TOKEN`, and `${GITHUB_TOKEN}` is stored. Leave it out for
                an API that needs no key.
            description: What the API is for, in its own language.
            enabled: Serve it now. False writes the entry switched off.
            autoload: Every operation starts in your tool list and is never
                evicted. Rarely what you want — see how many there are first.
        """
        settings, why = _prepare(
            load(),
            name,
            spec,
            base_url,
            api_key,
            description=description or None,
            enabled=enabled,
            autoload=autoload or None,
        )
        if settings is None:
            return why
        return await apply_and_report(family, settings, section=SECTION) + _unresolved(
            api_key
        )

    @mcp.tool(name="rest_api_remove", meta=FOR_THE_MODEL)
    async def rest_api_remove(name: str) -> str:
        """Stop a REST API and delete its entry from `rest-api:`.

        Its operations stop being offered — the catalogue drops them at the next
        search. Nothing outside the config is touched.

        Args:
            name: The API to remove, from `rest_api_list`.
        """
        return await remove_entry(family, name, words=WORDS, logger=logger)

    @mcp.tool(name="rest_api_set_enabled", meta=FOR_THE_MODEL)
    async def rest_api_set_enabled(name: str, enabled: bool) -> str:
        """Switch a REST API on or off, keeping its entry in `rest-api:`.

        Worth knowing for this family in particular: a spec is a large document
        and a proxy generates a tool per endpoint, so one served API can outweigh
        every other tool in the catalogue. Off is how it stays written down
        without being served.

        Args:
            name: The API, from `rest_api_list`.
            enabled: True serves it; False stops it and keeps the entry.
        """
        return await switch_entry(family, name, enabled, words=WORDS)

    @mcp.tool(name="rest_api_list_tools", meta=FOR_THE_MODEL)
    async def rest_api_list_tools(name: str, limit: int = 0) -> str:
        """List the operations one REST API exposes.

        These are the names behind `{name}__…` in your tool list, one per
        endpoint in the spec. It is capped: an API can expose hundreds of
        operations, and `tool_search` finds the one you want by what it does.

        Args:
            name: The API, from `rest_api_list`.
            limit: How many to show. Omit for the cap.
        """
        return await list_entry_tools(family, name, limit, words=WORDS)


def build_server(
    config: Config,
    *,
    transports: Mapping[str, Callable[[ToolServerSettings], Any]] | None = None,
    client_factory: ClientFactory | None = None,
) -> FastMCP:
    """Build the server over the `rest-api:` section, in file order."""
    return build_family_server(
        config,
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        category=CATEGORY,
        entries=config.rest_apis.values(),
        transports=transports,
        client_factory=client_factory,
        management=register_management,
    )


def main(argv: list[str] | None = None) -> int:
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        build=build_server,
        logger=logger,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
