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

from slife2 import configfile
from slife2.audience import FOR_THE_MODEL
from slife2.config import (
    Config,
    ConfigError,
    ToolServerSettings,
    _tool_server,
    find_config_path,
    load,
)
from slife2.gateway import ClientFactory
from slife2.mcp_server import configure_logging, parse_serve_args, serve
from slife2.toolfamily import (
    TOOL_LIST_LIMIT,
    Family,
    apply_and_report,
    build_family_server,
    refusal,
    settled,
    tools_as_text,
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
    if why := refusal(config, name):
        return None, why
    try:
        settings = _tool_server(entry, name)
    except ConfigError as exc:
        return None, f"[refused] {exc}"
    try:
        configfile.upsert(SECTION, name, entry)
    except ConfigError as exc:
        return None, f"[refused] {exc}"
    return settings, ""


def _describe(one: Any, name: str, raw: Mapping[str, Any]) -> str:
    """One line of `mcp_list` — the file's words and the link's state.

    Both halves are needed and neither is the other's summary: the file says what
    was configured (**with `${VAR}` intact** — the settings this process holds
    have been through `resolve_secret`, so printing those would put the
    operator's live keys in the conversation), and the link says whether it is
    answering.

    The state words are the hub's own (`off`, `ready`, `connecting`, `failed`,
    `idle`, `servers()`), because they are answers to the same question and a
    second vocabulary for it would be a second thing to learn.  `off` is read
    off the *file* rather than the link: a switched-off server was never asked,
    so its link has nothing to say.
    """
    if raw.get("url"):
        reach = "http  " + str(raw["url"])
    else:
        reach = "stdio " + str(raw.get("command") or "")
    args = raw.get("args")
    if isinstance(args, list) and args:
        reach += " " + " ".join(str(part) for part in args)
    if raw.get("enabled") is False:
        state = "off"
    elif one is None:
        state = "not held"
    else:
        state = one.connection.state
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
        # Settles first, for the reason the hub's `servers()` does: this is the
        # answer to "why is my tool missing", and a server that started a moment
        # ago reads as one that is still starting.  The two are different
        # problems and this is the tool that is supposed to tell them apart.
        # Bounded, and only for attempts already in flight — a cold `npx` that
        # this process started has had its own start-up to answer by now.
        family.connecting()
        await family.settle()
        entries = configfile.read_section(SECTION)
        if not entries:
            return (
                "No MCP servers are configured under `tools:` yet. `mcp_set` adds one."
            )
        return "\n".join(
            _describe(family.held(name), name, raw) for name, raw in entries.items()
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
    ) -> str:
        """Add or update an MCP server under `tools:` (upsert; idempotent).

        One entry is one transport, so give `command` for a process slife2 starts
        or `url` for an endpoint somebody else runs — never both. An entry that
        is already there keeps every field this call does not mention.

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
        if family.held(name) is None and name not in configfile.read_section(SECTION):
            return f"'{name}' is not a server under `tools:` — see `mcp_list`."
        removed = configfile.remove(SECTION, name)
        await family.remove(name)
        if not removed:
            return f"'{name}' was not written in `tools:`, so nothing was removed."
        logger.info("mcp_removed name=%s", name)
        return f"'{name}' is stopped and its entry is gone from `tools:`."

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
        if name not in configfile.read_section(SECTION):
            return f"'{name}' is not in `tools:` — `mcp_set` adds it, `mcp_list` shows what is there."
        configfile.set_enabled(SECTION, name, enabled)
        one = await family.set_enabled(name, enabled)
        if one is None:
            # Written but not held: this process did not have it, which happens
            # when the entry was added by another instance since this one read
            # the config.  The file is right; the next start holds it.
            return (
                f"'{name}' is now {'enabled' if enabled else 'disabled'} in "
                f"`tools:`. It was not one of the servers this process is "
                f"holding, so it is connected from the next start."
            )
        if not enabled:
            return f"'{name}' is switched off; its entry stays in `tools:`."
        await settled(one)
        if one.connection.usable:
            return (
                f"'{name}' is connected and offers {len(one.rows or [])} tool(s); "
                f"they are in your tool list from the next search."
            )
        reason = one.connection.error or "it has not answered yet"
        return f"'{name}' is switched on and connecting — {reason}."

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
        one = family.held(name)
        if one is None:
            return f"'{name}' is not a server this process holds — see `mcp_list`."
        # Waits for the link, for the reason `mcp_list` does: what this reports
        # is the tools the server *offers*, and "it has no tool list" is the
        # answer for both a broken server and one that is still starting.
        await settled(one)
        return tools_as_text(one, name, limit if limit > 0 else TOOL_LIST_LIMIT)


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
    args = parse_serve_args(argv, SERVER_NAME)
    configure_logging()
    config_path = find_config_path()
    config = load()

    address = config.server(CONFIG_KEY)
    logger.info(
        "serving %s on http://%s:%d%s",
        SERVER_NAME,
        args.host or address.host,
        args.port or address.port,
        address.path,
    )
    serve(
        build_server(config),
        address,
        args,
        name=SERVER_NAME,
        config_path=config_path,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
