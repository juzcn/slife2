"""slife2-mcp-tools — the `mcp:` section, and the servers it names.

Other people's MCP servers, each one an entry the operator wrote down: a command
to start or a URL to reach, with whatever credential it needs.  This is the
process that connects to them now, and the reason it is a process rather than a
paragraph of the hub is the same one `skills-server` and `cli-server` have — a
config section belongs to the thing that owns it, and the hub's job is the tool
*set*, not twenty links.

**What it serves is `list_sources` and `call_source`**, and nothing else: the
model never sees this server.  Each entry is declared as a source, the hub merges
its rows and enforces the naming rule, and a call comes back here to be run.  The
whole arrangement is `slife2.libs.plugin.family`'s, which is also `restapi-tools`'s — the
two differ in which section they read and nothing else, which is the honest
consequence of REST being an MCP server behind a proxy.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastmcp import FastMCP

from slife2.clients.gateway import ClientFactory
from slife2.libs.host.config import (
    MCP_TOOLS_SERVER_NAME,
    Config,
    ToolServerSettings,
    _tool_server,
    load,
    load_cached,
)
from slife2.libs.plugin.audience import FOR_THE_MODEL
from slife2.libs.plugin.family import (
    Family,
    FamilyWords,
    Homes,
    apply_and_report,
    build_family_server,
    list_entries,
    list_entry_tools,
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

#: The catalogue category its sources are filed under — the db's word for
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

#: What this family calls things, for the payloads `slife2.libs.plugin.family` builds
#: once for both connection families.  Every word here is one a model reads
#: back: a `server` here is an `API` next door, and a `tool` here is an
#: `operation`, over one road underneath.
#:
#: **Nothing here is an adjective.**  `connected` and `connecting` used to be
#: fields, because a link's state was read off a sentence; as a payload field it
#: is `state_word`'s own vocabulary (`ready`, `connecting`, `failed`, `idle`,
#: `off`, `not held`), which is the same one the hub answers `servers` with — so
#: the words that remain name *things* a family may legitimately call by another
#: name, and the state is one vocabulary everywhere.
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
# v1's `mcp_*` set, ported — the model may add, remove and switch a server, and
# what it writes is the same `mcp:` section the operator writes by hand.  The
# difference from v1 is where the write goes: v1's gateway mutated a live pool of
# its own, so every one of these had to reconcile "the file" against "the pool",
# while here the family *is* the pool — the same object the file was read into —
# so a change is written once and held once.


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
    """One row of `mcp_list` — the file's words and the link's state.

    Both halves are needed and neither is the other's summary: the file says what
    was configured (**with `${VAR}` intact** — the settings this process holds
    have been through `resolve_secret`, so handing those over would put the
    operator's live keys in the conversation and in the turn log), and the link
    says whether it is answering — `state_word`'s word, which is the same one a
    `set` answer and the hub's `servers` use.

    A key the file does not have is **left out** rather than sent empty: an
    entry with no `args` and one with `args: []` are the same absence, and a row
    that spelled both as `[]` would be inventing a field the operator never wrote.
    """
    row: dict[str, Any] = {"name": name, "state": state_word(raw, one)}
    if raw.get("url"):
        row["transport"] = "http"
        row["url"] = str(raw["url"])
    else:
        row["transport"] = "stdio"
        row["command"] = str(raw.get("command") or "")
    args = raw.get("args")
    if isinstance(args, list) and args:
        row["args"] = [str(part) for part in args]
    # The credentials are named and never shown — `KEY=${VAR}`, exactly as the
    # file has it.  Worth listing for the usual reason a listing exists (which
    # key does this server need?) and safe for the same one: the reference is
    # what the file holds, and the resolved value never gets here.
    for field in ("env", "headers"):
        values = raw.get(field)
        if isinstance(values, dict) and values:
            row[field] = {str(key): str(value) for key, value in values.items()}
    if description := str(raw.get("description") or ""):
        row["description"] = description
    return row


def register_management(mcp: FastMCP, homes: Homes[Family]) -> None:
    """Serve the `mcp_*` tools on this family's server."""

    @mcp.tool(name="mcp_list", meta=FOR_THE_MODEL)
    async def mcp_list(limit: int = 0, offset: int = 0) -> dict[str, Any]:
        """List the MCP servers configured under `mcp:`, with their state.

        Each entry as the file writes it — how it is reached, what it is for,
        and whether its link is answering.

        Args:
            limit: How many entries this page may hold. Omit for the cap.
            offset: How many entries to skip — that is how you page.

        Returns:
            `servers` — one row per entry, each with `name`, `state`
            (`ready`, `connecting`, `failed`, `idle` or `off`) and whatever the
            file has of it — and `total`, `limit` and `offset`, so
            `offset + len(servers) < total` says whether more remain.
        """
        return await list_entries(
            homes.current(), limit, offset, words=WORDS, describe=_describe
        )

    @mcp.tool(name="mcp_set", meta=FOR_THE_MODEL)
    async def mcp_set(
        name: str,
        command: str = "",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        url: str = "",
        headers: dict[str, str] | None = None,
        cwd: str = "",
        description: str = "",
        enabled: bool = True,
        autoload: bool = False,
    ) -> dict[str, Any]:
        """Add or update an MCP server under `mcp:` (upsert).

        One entry is one transport: `command` for a process slife2 starts, `url`
        for an endpoint somebody else runs. This is the whole entry — a field
        left out is dropped, so restate everything, which is also how a server
        switches between the two. It is connected here and now, and the answer
        says whether it answered.

        Args:
            name: The server's name — the key it is written under, and the
                prefix every tool of it reaches you as (`{name}__{tool}`).
            command: stdio — the program to start (npx, uvx, python).
            args: stdio — its arguments.
            env: stdio — environment overrides; `${VAR}` resolves when needed.
            url: http — the endpoint (SSE or Streamable HTTP).
            headers: http — request headers, `${VAR}` for anything secret.
            cwd: stdio — the process's working directory; the data directory
                when left out.
            description: What it is for, for whoever reads the config.
            enabled: Connect it now. False writes the entry switched off.
            autoload: Every tool it offers starts in your list and is never
                evicted.

        Returns:
            `name` and `enabled`; `state`, one of `ready`, `connecting`,
            `failed`, `idle` or `off`; `tools`, how many it offers, when it
            answered; `large`, true when that count is past a page's worth and
            the next search has a row per tool to write and embed; and `error`,
            what it said, when it has not answered.
        """
        settings = _prepare(
            load(),
            name,
            {
                "command": command or None,
                "args": args,
                "env": env,
                "url": url or None,
                "headers": headers,
                "cwd": cwd or None,
                "description": description or None,
                "enabled": enabled,
                "autoload": autoload or None,
            },
        )
        return await apply_and_report(homes.current(), settings, words=WORDS)

    @mcp.tool(name="mcp_remove", meta=FOR_THE_MODEL)
    async def mcp_remove(name: str) -> dict[str, Any]:
        """Stop an MCP server and delete its entry from `mcp:`.

        Its tools leave the catalogue at the next search. A package the command
        names stays on the machine. A name that is not in `mcp:` is refused, so
        a success means the entry is gone.

        Args:
            name: The server to remove — the key it is written under.

        Returns:
            `removed`, whether the entry was in the file, and `stopped`, whether
            this process was holding a link to it.
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

    @mcp.tool(name="mcp_list_tools", meta=FOR_THE_MODEL)
    async def mcp_list_tools(
        name: str, limit: int = 0, offset: int = 0
    ) -> dict[str, Any]:
        """List the tools one MCP server offers.

        These are the names to call them by, each `{name}__{tool}`.

        Args:
            name: The server — the key it is written under.
            limit: How many tools this page may hold. Omit for the cap.
            offset: How many tools to skip — that is how you page.

        Returns:
            `name`, `state` (`ready`, `connecting`, `failed`, `idle` or `off`),
            `tools` — `name` and `description` per tool — `total`, `limit` and
            `offset`, and `error` when the link has one. An empty `tools` with a
            `state` other than `ready` is why it is empty.
        """
        return await list_entry_tools(homes.current(), name, limit, offset, words=WORDS)


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
