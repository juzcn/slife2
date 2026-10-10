"""slife2-mcp-tools — the `tools:` section, and the servers it names.

Other people's MCP servers, each one an entry the operator wrote down: a command
to start or a URL to reach, with whatever credential it needs.  This is the
process that connects to them now, and the reason it is a process rather than a
paragraph of the hub is the same one `skills-server` and `cli-server` have — a
config section belongs to the thing that owns it, and the hub's job is the tool
*set*, not twenty links.

**What it serves is `list_sources` and `call_source`**, and nothing else: the
model never sees this server.  Each entry is declared as a source, the hub merges
its rows and enforces the naming rule, and a call comes back here to be run.  The
whole arrangement is `slife2.toolfamily`'s, which is also `restapi-tools`'s — the
two differ in which section they read and nothing else, which is the honest
consequence of REST being an MCP server behind a proxy.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastmcp import FastMCP

from slife2.audience import FOR_THE_MODEL
from slife2.config import (
    Config,
    ToolServerSettings,
    _tool_server,
    load,
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

SERVER_NAME = "slife2-mcp-tools"

#: This server's key in the config's `servers:` table, and the name the launcher
#: starts it under.  It is `mcp-tools` rather than `tools` because the catalogue
#: source of each entry is that entry's own name: a plugin cannot hold a source
#: under its own name, or its verdict would be written across rows that have no
#: connection behind them.
CONFIG_KEY = "mcp-tools"

#: The catalogue category its sources are filed under — the db's word for
#: "somebody else's MCP server", and the one thing that decides a row is
#: callable and the model may be given it.
CATEGORY = "mcp"

#: The config section this family owns, and the only thing it tells
#: `slife2.configfile`: that module edits a section and knows nothing about what
#: is in one.
SECTION = "tools"

INSTRUCTIONS = (
    "The MCP servers the config's `tools:` section names. This server is called "
    "by the toolhub, not by a model: it holds the connections, and the tools "
    "themselves reach the model through the hub."
)

#: What this family calls things, for the sentences `slife2.toolfamily` writes
#: once for both connection families.  Every word here is one a model reads
#: back: `a server` here is `an API` next door, over one road underneath.
WORDS = FamilyWords(
    section=SECTION,
    plural="MCP servers",
    entry="a server",
    unit="tool",
    list_tool="mcp_list",
    set_tool="mcp_set",
    remove_log="mcp_removed",
    present="connected",
    past="connected",
    starting="connecting",
)


# ── The management tools ────────────────────────────────────────────────
# v1's `mcp_*` set, ported — the model may add, remove and switch a server, and
# what it writes is the same `tools:` section the operator writes by hand.  The
# difference from v1 is where the write goes: v1's gateway mutated a live pool of
# its own, so every one of these had to reconcile "the file" against "the pool",
# while here the family *is* the pool — the same object the file was read into —
# so a change is written once and held once.


def _prepare(
    config: Config, name: str, entry: dict[str, Any]
) -> tuple[ToolServerSettings | None, str]:
    """Write one `tools:` entry, or answer with why it cannot be.

    `(settings, "")` means it was written; `(None, refusal)` is a sentence for
    the caller to return.  Two refusals, and both happen *before* anything is
    written — a name the hub is already using, and an entry the config layer
    will not parse.

    **Validated by the parser the loader uses.**  `slife2.config._tool_server`
    is what turns an entry into settings at every start, so an entry that goes
    through it here is one the next start accepts, and an entry that fails
    ("both `command` and `url`", "needs `command` or `url`") is refused in the
    words the loader would have used — a restart later, and in a process that
    never saw this call.
    """
    return prepare_entry(
        config, name, lambda: (_tool_server(entry, name), entry), section=SECTION
    )


def _describe(one: Any, name: str, raw: Mapping[str, Any]) -> str:
    """One line of `mcp_list` — the file's words and the link's state.

    Both halves are needed and neither is the other's summary: the file says what
    was configured (**with `${VAR}` intact** — the settings this process holds
    have been through `resolve_secret`, so printing those would put the
    operator's live keys in the conversation), and the link says whether it is
    answering — one word in brackets, `state_word`'s to choose and explain.
    """
    if raw.get("url"):
        reach = "http  " + str(raw["url"])
    else:
        reach = "stdio " + str(raw.get("command") or "")
    args = raw.get("args")
    if isinstance(args, list) and args:
        reach += " " + " ".join(str(part) for part in args)
    state = state_word(raw, one)
    line = f"- {name} [{state}]\n    {reach}"
    # The credentials are named and never shown — `KEY=${VAR}`, exactly as the
    # file has it.  Worth printing for the usual reason a listing exists (which
    # key does this server need?) and safe for the same one: the reference is
    # what the file holds, and the resolved value never gets here.
    for field, label in (("env", "env"), ("headers", "headers")):
        values = raw.get(field)
        if isinstance(values, dict) and values:
            for key, value in values.items():
                line += f"\n    {label} {key}={value}"
    if description := str(raw.get("description") or ""):
        line += f"\n    {description}"
    return line


def register_management(mcp: FastMCP, family: Family) -> None:
    """Serve the `mcp_*` tools on this family's server."""

    @mcp.tool(name="mcp_list", meta=FOR_THE_MODEL)
    async def mcp_list() -> str:
        """List the MCP servers configured under `tools:`, with their state.

        The answer to "what do I have and which of them are working": each entry
        as the file writes it (command or URL, description) and whether its link
        is answering. A server whose tools are missing is either switched off,
        still starting, or failing here — `mcp_list_tools` shows what one offers
        once it does.
        """
        return await list_entries(family, words=WORDS, describe=_describe)

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
    ) -> str:
        """Add or update an MCP server under `tools:` (upsert; idempotent).

        One entry is one transport, so give `command` for a process slife2 starts
        or `url` for an endpoint somebody else runs — never both. **This is the
        whole entry**: a field left out is not kept from an older version, so
        restate everything the entry should have — which is also how a server
        is switched from `url` to `command`.

        Call this once you know the server works: it is connected here and now,
        and the answer says whether it did.

        Args:
            name: The server's name. Every tool it offers reaches you as
                `{name}__{tool}`, so it is also how you will call them.
            command: stdio — the program to start (npx, uvx, python).
            args: stdio — its arguments.
            env: stdio — environment overrides. `${VAR}` names a value that is
                resolved when it is needed; that is how a key stays out of the
                config file.
            url: http — the endpoint (SSE or Streamable HTTP).
            headers: http — request headers, `${VAR}` for anything secret.
            cwd: stdio — the process's working directory; the data directory
                when left out, which is what `.` in an argument means.
            description: What it is for, in its own language — a note for a
                person reading the config, not the text you are given per tool.
            enabled: Connect it now. False writes the entry switched off.
            autoload: Every tool this server offers starts in your list and is
                never evicted. Worth setting only for one you want every turn.
        """
        settings, why = _prepare(
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
        if settings is None:
            return why
        return await apply_and_report(family, settings, section=SECTION)

    @mcp.tool(name="mcp_remove", meta=FOR_THE_MODEL)
    async def mcp_remove(name: str) -> str:
        """Stop an MCP server and delete its entry from `tools:`.

        Its tools stop being offered — the catalogue drops them at the next
        search — and everything that was configured for it here is gone. Nothing
        is uninstalled: a package the command names is still on the machine.

        Args:
            name: The server to remove, from `mcp_list`.
        """
        return await remove_entry(family, name, words=WORDS, logger=logger)

    @mcp.tool(name="mcp_set_enabled", meta=FOR_THE_MODEL)
    async def mcp_set_enabled(name: str, enabled: bool) -> str:
        """Switch an MCP server on or off, keeping its entry in `tools:`.

        Off is how a slow, paid or broken server stays configured without being
        connected: its tools stop being offered and it is not started at the next
        start either, but nothing about the entry is lost. Turning it back on
        connects it again.

        Args:
            name: The server, from `mcp_list`.
            enabled: True connects it; False stops it and keeps the entry.
        """
        return await switch_entry(family, name, enabled, words=WORDS)

    @mcp.tool(name="mcp_list_tools", meta=FOR_THE_MODEL)
    async def mcp_list_tools(name: str, limit: int = 0) -> str:
        """List the tools one MCP server offers.

        These are the names behind `{name}__…` in your tool list. It is capped,
        because a published server can offer more tools than are worth reading —
        for anything specific, `tool_search` finds it by what it does.

        Args:
            name: The server, from `mcp_list`.
            limit: How many to show. Omit for the cap.
        """
        return await list_entry_tools(family, name, limit, words=WORDS)


def build_server(
    config: Config,
    *,
    transports: Mapping[str, Callable[[ToolServerSettings], Any]] | None = None,
    client_factory: ClientFactory | None = None,
) -> FastMCP:
    """Build the server over the `tools:` section, in file order."""
    return build_family_server(
        config,
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        category=CATEGORY,
        entries=config.tools.values(),
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
