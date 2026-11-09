"""slife2-restapi-tools — the `rest-api:` section, held like any other server.

A REST API is written down as a spec, a base URL and a key, and the config layer
expands it into the `uvx mcp-openapi-proxy` command that serves it — so what
arrives here is an ordinary stdio MCP server like every entry under `mcp:`, and
this plugin holds it the same way.  That is the whole of the difference between
the two: the section, and the shape an entry is written in.

**Why it is a plugin of its own anyway.**  Because the sections are separate, and
an operator's mental model is the section: `rest-api:` entries are a shorter way
to say the same thing, not a second mechanism, and a family that owns its config
is a family whose next change has somewhere to land.  The machinery is shared —
see `slife2.libs.plugin.family` — so this costs a module and a port, not a second
implementation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from slife2.clients.gateway import ClientFactory
from slife2.libs.host import configfile
from slife2.libs.host.config import (
    RESTAPI_TOOLS_SERVER_NAME,
    Config,
    ConfigError,
    ToolServerSettings,
    _rest_api,
    load,
    load_cached,
    resolve_secret,
    unresolved_reference,
)
from slife2.libs.host.model_config import show_api_key
from slife2.libs.host.tool_config import RestApiConfig, load_rest_api, rest_entry
from slife2.libs.plugin.audience import FOR_THE_MODEL
from slife2.libs.plugin.family import (
    Family,
    FamilyWords,
    Homes,
    apply_and_report,
    build_family_server,
    entry_report,
    list_entries,
    prepare_entry,
    remove_entry,
    state_word,
    switch_entry,
)
from slife2.libs.plugin.protocol import serve_plugin

logger = logging.getLogger(__name__)

SERVER_NAME = RESTAPI_TOOLS_SERVER_NAME

#: This server's key in the config's `servers:` table.
CONFIG_KEY = "restapi-tools"

#: The catalogue category its sources are filed under.  The db keeps `rest`
#: apart from `mcp` for the reason the sections are apart: it is the answer to
#: the first question anyone debugging asks, which is why a server they never
#: wrote a `command:` for is running `uvx`.
CATEGORY = "rest"

#: The config section this family owns, and the only thing it tells
#: `slife2.libs.host.configfile`: that module edits a section and knows nothing about what
#: is in one.
SECTION = "rest-api"

INSTRUCTIONS = (
    "The REST APIs the config's `rest-api:` section expands into MCP servers. "
    "This server is called by the toolhub, not by a model: it holds the "
    "connections, and the tools themselves reach the model through the hub."
)

#: What this family calls things, for the payloads `slife2.libs.plugin.family` builds
#: once for both connection families.  An API exposes *operations* where a
#: server exposes tools, and its listing's rows sit under `apis` — the same
#: mechanism under the two names a person already uses for them.  **The state is
#: not one of these**: `ready` and `failed` are `state_word`'s, because the hub
#: answers that question in the same words for every kind of source.
WORDS = FamilyWords(
    section=SECTION,
    collection="apis",
    entry="an API",
    unit="operation",
    list_tool="rest_api_list",
    set_tool="rest_api_set",
    enable_tool="rest_api_set_enabled",
    remove_log="rest_api_removed",
)


# ── The management tools ────────────────────────────────────────────────
# The same four names as the MCP family, over this section — the split is kept
# because a REST API and an MCP server are different things to a person even
# when one is served by the other.  What differs is only the entry: an API is
# written as a spec and a base URL, and the config layer expands it into the
# `mcp-openapi-proxy` command that serves it (`_rest_api`), so nothing here
# knows what the proxy is called.


def _reference(value: str) -> str:
    """A credential written the way the config writes one: `${VAR}`.

    **The name, not the secret**, which is the whole of what makes this tool
    safe to hand a model.  The stored credential is a *variable name* for the
    same reason; a value that is already a reference (or a `keyring:` URI) is
    left exactly as given, so a caller that knows the syntax is not
    second-guessed.
    """
    credential = value.strip()
    if credential.startswith("${") or credential.startswith("keyring:"):
        return credential
    return f"${{{credential}}}"


def _validate_http_url(url: str, what: str) -> str:
    """Require an `http(s)` URL with a host.  **The proxy child fetches the
    spec**, so a `file://` or an internal-host URL
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
    config: Config, name: str, rest: RestApiConfig, *, keep_key: str = ""
) -> ToolServerSettings:
    """Validate a `rest-api:` entry and write it, or raise why it cannot be.

    The entry is the canonical object's serialization (`rest_entry`); the two
    URL checks and the key's reference normalization are this family's extra
    guards, run inside the shared helper's `try` so a `file://` spec is refused
    as a `ConfigError` like any other bad entry.

    `keep_key` is a literal key the file already has and `get` could not show.
    An empty `api_key` otherwise means "no key" — but a whole-set from `get`
    cannot tell that apart from "a literal is there", so the caller passes the
    literal here and the write leaves it alone.
    """

    def build() -> tuple[ToolServerSettings, dict[str, Any]]:
        entry = rest_entry(rest)
        entry["spec"] = _validate_http_url(rest.spec, "spec")
        if rest.base_url:
            entry["base_url"] = _validate_http_url(rest.base_url, "base_url")
        if rest.api_key:
            entry["api_key"] = _reference(rest.api_key)
        elif keep_key:
            entry["api_key"] = keep_key
        return _rest_api(entry, name), entry

    return prepare_entry(config, name, build, section=SECTION)


def _unresolved(api_key: str) -> str:
    """The credential that is not there yet, or `""`.

    **Said at the moment it is written, and not saved for the call that fails**:
    a `${VAR}` nothing resolves to is not an error — the config keeps the
    reference and the failure surfaces where the value is used (`slife2.libs.host.config`
    says so) — but for an *API key* the place it surfaces is a 401 from somebody
    else's server, one turn later, naming nothing.  A field here is the
    difference between that and knowing now, and the model that reads it knows
    what to do: the value is a reference, so it needs storing.

    **A *name*, and never the sentence that used to carry it.**  What a caller
    acts on is which reference did not resolve, and the answer's `unresolved`
    field is exactly that string.  The old sentence spelled the fix out —
    `credstore set <name>` — which put the remedy in prose in a payload that is
    otherwise fields.

    **And "is this still a reference" is not answered here.**
    `slife2.libs.host.config.unresolved_reference` is where that question is answered, and
    its docstring names the four places that used to answer it separately and the
    leak that came of it.  This was one of them: a `keyring:` value returned
    `""` — nothing to say — where the rule says *unresolved*, so on a host with
    no `credstore` (the degrade `resolve_secret` names) a
    `keyring:slife2/github` did not resolve, was left verbatim, was written into
    the proxy child's `API_KEY` as the credential, and the person was told
    nothing while the sibling `${VAR}` case returned the name — two halves of
    one rule disagreeing.
    """
    if not api_key:
        return ""
    reference = _reference(api_key)
    if not unresolved_reference(resolve_secret(reference)):
        return ""
    return reference


def _describe(one: Any, name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """One row of `rest_api_list` — what the API is, and whether it is usable.

    **A listing is for choosing, not for reading the entry.**  The key, the
    operator's sentence about the API, the link's state, and an operation count
    or the failure's reason are what a model chooses between APIs with; the
    spec, base URL and credential are `rest_api_get`'s.  The spec in particular
    is the proxy's document, not the API's purpose — a row that showed it would
    spend the browse's tokens on the one field a chooser cannot use.
    """
    row: dict[str, Any] = {"name": name, "state": state_word(raw, one)}
    if one is not None and one.settings.enabled:
        if one.usable:
            row["operations"] = len(one.rows or [])
        elif one.error:
            row["error"] = one.error
    if description := str(raw.get("description") or ""):
        row["description"] = description
    return row


def register_management(mcp: FastMCP, homes: Homes[Family]) -> None:
    """Serve the `rest_api_*` tools on this family's server."""

    def entry_of(name: str) -> tuple[dict[str, Any], RestApiConfig]:
        """One entry's raw mapping and its canonical object, or this family's miss."""
        raw = configfile.read_section(SECTION).get(name)
        if not isinstance(raw, dict):
            raise ToolError(
                f"`{name}` is not in `rest-api:` — `rest_api_set` adds it, "
                f"`rest_api_list` shows what is there."
            )
        try:
            config = load_rest_api(raw, where=f"rest-api.{name}")
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        # The tool can write only the URLs it checked; a spec path or another
        # scheme is a hand-written entry this tool does not edit.  Refusing it
        # here keeps `set(get())` true rather than handing back an object `set`
        # would then reject.
        try:
            _validate_http_url(config.spec, "spec")
            if config.base_url:
                _validate_http_url(config.base_url, "base_url")
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        return raw, config.model_copy(update={"api_key": show_api_key(config.api_key)})

    async def report_of(name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """The read-only half: the link's state and, when it answered, its count."""
        family = homes.current()
        family.connecting()
        await family.settle()
        one = family.held(name)
        if one is None:
            return {"name": name, "state": state_word(raw, None)}
        return entry_report(one, name, words=WORDS)

    @mcp.tool(name="rest_api_list", meta=FOR_THE_MODEL)
    async def rest_api_list(limit: int = 0, offset: int = 0) -> dict[str, Any]:
        """List the REST APIs configured under `rest-api:`, with their state.

        Each row says what the API is for and whether its link is answering: an
        `operations` count when it answered, or `error` when it did not. The
        spec, base URL and credential are `rest_api_get`'s, not the browse's.

        Args:
            limit: How many entries this page may hold. Omit for the cap.
            offset: How many entries to skip — that is how you page.

        Returns:
            `apis` — one row per entry, each with `name`, `state` (`ready`,
            `connecting`, `failed`, `idle`, `off` or `not held`), `description`
            when the file has one, and `operations` or `error` when the link
            has one — and `total`, `limit` and `offset`, so
            `offset + len(apis) < total` says whether more remain.
        """
        return await list_entries(
            homes.current(), limit, offset, words=WORDS, describe=_describe
        )

    @mcp.tool(name="rest_api_get", meta=FOR_THE_MODEL)
    async def rest_api_get(name: str) -> dict[str, Any]:
        """Read one REST API's whole entry, by name.

        The answer is the canonical entry — `config` has `spec`, `base_url`,
        `api_key`, `args`, `env`, `cwd`, `description` and `enabled` — so
        `rest_api_set` takes the same object back. `api_key` is the
        `${VAR}`/`keyring:` reference, never the resolved key; empty means the
        file has a literal key that is not shown.

        `state` is the link's read-only word, and `operations` or `error` says
        what it answered when it did. `unresolved` names a reference that
        resolves to nothing yet, when there is one.

        Args:
            name: The API's name — the key it is written under.
        """
        raw, config = entry_of(name)
        report = await report_of(name, raw)
        report.pop("enabled", None)  # the writable field is in `config`
        answer = {**report, "config": config.model_dump()}
        if unresolved := _unresolved(config.api_key):
            answer["unresolved"] = unresolved
        return answer

    @mcp.tool(name="rest_api_set", meta=FOR_THE_MODEL)
    async def rest_api_set(name: str, config: RestApiConfig) -> dict[str, Any]:
        """Add or replace one REST API — the whole entry, and no patch.

        Every field is required; a field left out is a schema error, not a
        silent default. Read `rest_api_get`, change what you mean to change, and
        pass the whole `config` back. `api_key` is a `${VAR}`/`keyring:`
        reference; empty means no key.

        Every endpoint in the OpenAPI document becomes an operation named
        `{name}__{operation}`, so one spec can be hundreds of them. The answer is
        the entry as written — the same `config` shape `rest_api_get` gives —
        plus the link's read-only `state`, `operations`/`error`, and
        `unresolved` when the key's reference resolves to nothing yet.

        Args:
            name: The API's name — the key it is written under, and the prefix
                every operation of it reaches you as (`{name}__{operation}`).
            config: The whole entry — the `config` object `rest_api_get`
                returned.
        """
        existing = configfile.read_section(SECTION).get(name)
        keep_key = ""
        if isinstance(existing, dict) and not config.api_key:
            current = str(existing.get("api_key") or "")
            # A reference is shown by get, so an empty `api_key` is the caller
            # clearing it.  A literal is hidden, so an empty one means "the
            # object I was handed did not contain it" — keep it.
            if current and not show_api_key(current):
                keep_key = current
        settings = _prepare(load(), name, config, keep_key=keep_key)
        report = await apply_and_report(homes.current(), settings, words=WORDS)
        report.pop("enabled", None)  # the writable field is in `config`
        _, written = entry_of(name)
        answer = {**report, "config": written.model_dump()}
        if unresolved := _unresolved(config.api_key):
            answer["unresolved"] = unresolved
        return answer

    @mcp.tool(name="rest_api_remove", meta=FOR_THE_MODEL)
    async def rest_api_remove(name: str) -> dict[str, Any]:
        """Stop a REST API and delete its entry from `rest-api:`.

        Its operations stop being offered at the next search. A name that is
        not in `rest-api:` is refused, so a success means the entry is gone.

        Args:
            name: The API to remove — the key it is written under.

        Returns:
            `name` and `removed` — true, because a name in neither the file nor
            a live link is refused rather than answered.
        """
        return await remove_entry(homes.current(), name, words=WORDS, logger=logger)

    @mcp.tool(name="rest_api_set_enabled", meta=FOR_THE_MODEL)
    async def rest_api_set_enabled(name: str, enabled: bool) -> dict[str, Any]:
        """Switch a REST API on or off, keeping its entry in `rest-api:`.

        A spec generates an operation per endpoint, so one served API can
        outweigh every other tool in search; off keeps it written down.

        Args:
            name: The API — the key it is written under.
            enabled: True serves it; False stops it and keeps the entry.

        Returns:
            The same payload `rest_api_set` answers with, and no `unresolved`:
            writing the entry is what put the credential there, and this call
            changed only the switch.
        """
        return await switch_entry(homes.current(), name, enabled, words=WORDS)


def build_server(
    config: Config,
    *,
    source: Callable[[], Config] | None = None,
    transports: Mapping[str, Callable[[ToolServerSettings], Any]] | None = None,
    client_factory: ClientFactory | None = None,
) -> FastMCP:
    """Build the server over the `rest-api:` section, in file order."""
    return build_family_server(
        config,
        source=source,
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        category=CATEGORY,
        entries=lambda theirs: theirs.rest_apis.values(),
        words=WORDS,
        transports=transports,
        client_factory=client_factory,
        management=register_management,
    )


def main(argv: list[str] | None = None) -> int:
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        build=lambda config: build_server(config, source=load_cached),
        logger=logger,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
