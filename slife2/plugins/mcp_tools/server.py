"""slife2-mcp-tools — the `mcp:` section, and the servers it names.

Other people's MCP servers, each one an entry the operator wrote down: a command
to start or a URL to reach, with whatever credential it needs.  This is the
process that connects to them now, and the reason it is a process rather than a
paragraph of the hub is the same one `skills-server` and `cli-server` have — a
config section belongs to the thing that owns it, and the hub's job is the tool
*set*, not twenty links.

**What it serves the hub is `list_sources` and `call_source`**, and what it
serves the model is the four `mcp_*` management tools.  Each entry is declared
as a source, the hub merges its rows and enforces the naming rule, and a call
comes back here to be run.  The whole arrangement is
`slife2.libs.plugin.family`'s, which is also `restapi-tools`'s — the two differ
in which section they read and nothing else, which is the honest consequence of
REST being an MCP server behind a proxy.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from slife2.clients.gateway import ClientFactory
from slife2.libs.host import configfile
from slife2.libs.host.config import (
    MCP_TOOLS_SERVER_NAME,
    Config,
    ToolServerSettings,
    _tool_server,
    load,
    load_cached,
)
from slife2.libs.host.tool_config import McpConfig, load_mcp, mcp_entry
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

SERVER_NAME = MCP_TOOLS_SERVER_NAME

#: This server's key in the config's `servers:` table, and the name the launcher
#: starts it under.  It is `mcp-tools` rather than `mcp` because the catalogue
#: source of each entry is that entry's own name: a plugin cannot hold a source
#: under its own name, or its verdict would be written across rows that have no
#: connection behind them.
CONFIG_KEY = "mcp-tools"

#: The catalogue category its sources are filed under — the catalogue's word for
#: "somebody else's MCP server", and the one thing that decides a row is
#: callable and the model may be given it.
CATEGORY = "mcp"

#: The config section this family owns, and the only thing it tells
#: `slife2.libs.host.configfile`: that module edits a section and knows nothing about what
#: is in one.  It is the category's own name, because a section of this kind is
#: named for the category its rows are declared under.
SECTION = "mcp"

INSTRUCTIONS = (
    "The MCP servers the config's `mcp:` section names. This server is called "
    "by the toolhub, not by a model: it holds the connections, and the tools "
    "themselves reach the model through the hub."
)

#: What this family calls things, for the payloads `slife2.libs.plugin.family`
#: builds once for both connection families.  Every word here is one a model
#: reads back: a `server` here is an `API` next door, and a `tool` here is an
#: `operation`, over one road underneath.
#:
#: **Nothing here is an adjective.**  A link's state is `state_word`'s vocabulary
#: (`ready`, `connecting`, `failed`, `idle`, `off`, `not held`), the same one the
#: hub answers `servers` with, so the words that remain name *things* a family
#: may legitimately call by another name.
WORDS = FamilyWords(
    section=SECTION,
    collection="servers",
    entry="a server",
    unit="tool",
    list_tool="mcp_list",
    set_tool="mcp_set",
    enable_tool="mcp_set_enabled",
    remove_log="mcp_removed",
)


# ── The management tools ────────────────────────────────────────────────
# The `mcp_*` set — the model may add, remove and switch a server, and what it
# writes is the same `mcp:` section the operator writes by hand.  Where the write
# goes is what shapes these: a gateway that mutated a live pool of its own would
# have to reconcile "the file" against "the pool" on every one, while here the
# family *is* the pool — the same object the file was read into — so a change is
# written once and held once.


def _prepare(config: Config, name: str, entry: dict[str, Any]) -> ToolServerSettings:
    """Write one `mcp:` entry, or raise `ToolError` saying why it cannot be.

    Two refusals, and both happen *before* anything is written — a name the hub
    is already using, and an entry the config layer will not parse.

    **Validated by the parser the loader uses.**  `slife2.libs.host.config._tool_server`
    is what turns an entry into settings at every start, so an entry that goes
    through it here is one the next start accepts, and an entry that fails
    ("both `command` and `url`", "needs `command` or `url`") is refused in the
    words the loader would have used — a restart later, and in a process that
    never saw this call.
    """
    return prepare_entry(
        config, name, lambda: (_tool_server(entry, name), entry), section=SECTION
    )


def _describe(one: Any, name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """One row of `mcp_list` — what the server is, and whether it is usable.

    **A listing is for choosing, not for reading the entry.**  The key, the
    operator's sentence about what it offers, the link's state, and a count or
    the failure's reason are what a model chooses between servers with; the
    command, URL, args, cwd and credentials are `mcp_get`'s, and putting them
    here would make the browse pay for a configuration it is not editing.
    """
    row: dict[str, Any] = {"name": name, "state": state_word(raw, one)}
    if one is not None and one.settings.enabled:
        if one.usable:
            row["tools"] = len(one.rows or [])
        elif one.error:
            row["error"] = one.error
    if description := str(raw.get("description") or ""):
        row["description"] = description
    return row


def register_management(mcp: FastMCP, homes: Homes[Family]) -> None:
    """Serve the `mcp_*` tools on this family's server."""

    def entry_of(name: str) -> tuple[dict[str, Any], McpConfig]:
        """One entry's raw mapping and its canonical object, or this family's miss."""
        raw = configfile.read_section(SECTION).get(name)
        if not isinstance(raw, dict):
            raise ToolError(
                f"`{name}` is not in `mcp:` — `mcp_set` adds it, `mcp_list` "
                f"shows what is there."
            )
        try:
            return raw, load_mcp(raw, where=f"mcp.{name}")
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    async def report_of(name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """The read-only half: the link's state and, when it answered, its count."""
        family = homes.current()
        family.connecting()
        await family.settle()
        one = family.held(name)
        if one is None:
            return {"name": name, "state": state_word(raw, None)}
        return entry_report(one, name, words=WORDS)

    @mcp.tool(name="mcp_list", meta=FOR_THE_MODEL)
    async def mcp_list(limit: int = 0, offset: int = 0) -> dict[str, Any]:
        """List the MCP servers configured under `mcp:`, with their state.

        Each row says what the server is for and whether its link is answering:
        a `tools` count when it answered, or `error` when it did not. The
        command, URL, arguments and credentials are `mcp_get`'s, not the
        browse's.

        Args:
            limit: How many entries this page may hold. Omit for the cap.
            offset: How many entries to skip — that is how you page.

        Returns:
            `servers` — one row per entry, each with `name`, `state` (`ready`,
            `connecting`, `failed`, `idle`, `off` or `not held`), `description`
            when the file has one, and `tools` or `error` when the link has
            one — and `total`, `limit` and `offset`, so
            `offset + len(servers) < total` says whether more remain.
        """
        return await list_entries(
            homes.current(), limit, offset, words=WORDS, describe=_describe
        )

    @mcp.tool(name="mcp_get", meta=FOR_THE_MODEL)
    async def mcp_get(name: str) -> dict[str, Any]:
        """Read one MCP server's whole entry, by name.

        The answer is the canonical entry — `config` has every writable field:
        `transport`, `command`, `args`, `env`, `url`, `headers`, `cwd`,
        `description` and `enabled` — so `mcp_set` takes the same object back.
        `state` is the link's read-only word, and `tools` or `error` says what it
        answered when it did.

        Args:
            name: The server's name — the key it is written under.
        """
        raw, config = entry_of(name)
        report = await report_of(name, raw)
        report.pop("enabled", None)  # the writable field is in `config`
        return {**report, "config": config.model_dump()}

    @mcp.tool(name="mcp_set", meta=FOR_THE_MODEL)
    async def mcp_set(name: str, config: McpConfig) -> dict[str, Any]:
        """Add or replace one MCP server — the whole entry, and no patch.

        `transport` chooses the shape: `stdio` needs `command`, `http` needs
        `url`. Every field is required; a field left out is a schema error, not
        a silent default. Read `mcp_get`, change what you mean to change, and
        pass the whole `config` back.

        The entry is connected here and now, and the answer is the entry as
        written — the same `config` shape `mcp_get` gives — plus the link's
        read-only `state`, and `tools` or `error` when it has one.

        Args:
            name: The server's name — the key it is written under, and the
                prefix every tool of it reaches you as (`{name}__{tool}`).
            config: The whole entry — the `config` object `mcp_get` returned.
        """
        settings = _prepare(load(), name, mcp_entry(config))
        report = await apply_and_report(homes.current(), settings, words=WORDS)
        report.pop("enabled", None)  # the writable field is in `config`
        _, written = entry_of(name)
        return {**report, "config": written.model_dump()}

    @mcp.tool(name="mcp_remove", meta=FOR_THE_MODEL)
    async def mcp_remove(name: str) -> dict[str, Any]:
        """Stop an MCP server and delete its entry from `mcp:`.

        Its tools stop being offered at the next search. A package the command
        names stays on the machine. A name that is not in `mcp:` is refused, so
        a success means the entry is gone.

        Args:
            name: The server to remove — the key it is written under.

        Returns:
            `name` and `removed` — true, because a name in neither the file nor
            a live link is refused rather than answered.
        """
        return await remove_entry(homes.current(), name, words=WORDS, logger=logger)

    @mcp.tool(name="mcp_set_enabled", meta=FOR_THE_MODEL)
    async def mcp_set_enabled(name: str, enabled: bool) -> dict[str, Any]:
        """Switch an MCP server on or off, keeping its entry in `mcp:`.

        Off keeps the entry and stops the server being connected or started.

        Args:
            name: The server — the key it is written under.
            enabled: True connects it; False stops it and keeps the entry.

        Returns:
            The same payload `mcp_set` answers with: `name`, `enabled`, `state`,
            and `tools` or `error` when the link has one of them.
        """
        return await switch_entry(homes.current(), name, enabled, words=WORDS)


def build_server(
    config: Config,
    *,
    source: Callable[[], Config] | None = None,
    transports: Mapping[str, Callable[[ToolServerSettings], Any]] | None = None,
    client_factory: ClientFactory | None = None,
) -> FastMCP:
    """Build the server over the `mcp:` section, in file order."""
    return build_family_server(
        config,
        source=source,
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        category=CATEGORY,
        entries=lambda theirs: theirs.mcp.values(),
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
