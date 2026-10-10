"""slife2-cli — the `cli:` section, as the plugin that owns it.

A `cli:` entry is a program already on this machine — `yt-dlp`, a browser
harness — written down so the model can be told it exists.  There is no process
to start, no URL to connect to and no credential: what an entry has is a
command, a sentence about what it does, and how to install it if it is missing.

**Why a process for a family with nothing to connect to.**  Nothing here needs a
connection, and that was the argument for keeping it out of one — a server
invented to wrap a row-builder is a process that exists to be connected to.
What changed is what is next: DESIGN.md §9 has an entry becoming *a tool the
operator's own config gave the model*, run as an argv rather than through a
shell so that the arguments a model invents cannot become commands it invented.
One tool per entry is a model-facing tool, and this is the process that will
serve them.  Building it while the family is cheap beats bolting twenty tools
onto the hub, which is supposed to hold the tool *set* and nothing else.

**What it serves to the model is the four `cli_*` tools that edit the section**,
and to the hub the one row per entry it declares.  This was the last of the four
families to get its tools, and the reason it read as the odd one out for so long:
its *entries* are not tools and nothing runs one, so it had nothing model-facing
to offer until the tools that write the section arrived.  What a command's row is
for is `list_sources`: a search reads the catalogue, so an entry nobody wrote a
row for is a command only a model that already knew its name could reach.

**This process does not write the catalogue.**  It answers `list_sources` and
the hub merges the answer — the arrangement `slife2.mcp_server` describes — so
that the hub remains the only writer of the tool table, the only process holding
a connection to the db, and the only place two sources' claim on one name is
settled.  What belongs here is the *section*: parsing it is `slife2.config`'s
business and turning it into rows is this file's.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from fastmcp import FastMCP

from slife2 import configfile
from slife2.audience import FOR_THE_MODEL
from slife2.config import (
    CliToolSettings,
    Config,
    ConfigError,
    _cli_tool,
    load,
)
from slife2.mcp_server import LIST_SOURCES, house_server, serve_plugin

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-cli"

#: This server's key in the config's `servers:` table, and so the name the
#: launcher starts it under and the hub connects to it by.
CONFIG_KEY = "cli-server"

#: The catalogue source and category these rows carry, which is also what
#: `tool_search` filters on and what a result prints beside a hit.
#:
#: **Deliberately not this server's own name.**  A source's verdict is written
#: across every row it owns, so a source holding both a tool and a set of
#: documents would report its playbooks as broken whenever it faltered — and
#: these rows have no connection whose state a verdict could come from, which is
#: why their `status` is the config's to write and nothing else's.
SOURCE = "cli"

#: The category, and the namespace the row names carry (`cli:yt-dlp`).  The
#: prefix is not decoration: a name is a row's identity, and `browser-harness` is
#: both a `cli:` entry *and* the skill that documents it.
CATEGORY = "cli"

#: The config section this family owns, and the only thing it tells
#: `slife2.configfile`: that module edits a section and knows nothing about what
#: is in one.
SECTION = "cli"

INSTRUCTIONS = (
    "The commands the config's `cli:` section records as already installed on "
    "this machine, and as the `cli_*` tools that edit that section. Nothing here "
    "runs a command — an entry is a row a search can find, and the tool that "
    "executes one is this server's next change."
)


def rows(entries: Mapping[str, CliToolSettings]) -> list[dict[str, Any]]:
    """The `cli:` section as catalogue rows, one per entry.

    **A switched-off entry is a row too**, carrying `disabled`, because the model
    is better told that the command exists and is off than not told at all.  That
    verdict rides on the row rather than coming from the runtime, and it is the
    one family where that is true (`slife2.db._plan`): there is no connection to
    have a state, so the config is the authority.  Filtering here instead would
    make "the operator turned it off" and "nobody ever wrote it down" the same
    silence — and the first is a thing a model can ask a person about.

    `remote_name` is the command and `schema` is everything else the entry
    knows — the invocation and the line that installs it — because that is what
    a search matches on when the model asks for a job rather than a name.  The
    description is the operator's sentence about what it does, and it is not cut
    short: a model choosing from half a sentence is a model guessing.

    Takes the entries rather than the `Config` because the switch does not sit
    where the rest of the config does: this server *holds* what it reads, so
    that `cli_set` and `cli_set_enabled` can change it here and now instead of
    waiting for a restart.
    """
    return [
        {
            "name": f"{CATEGORY}:{entry.name}",
            "description": entry.description,
            "remote_name": entry.command,
            "schema": "\n".join(
                part for part in (entry.command, entry.install) if part
            ),
            "status": "enabled" if entry.enabled else "disabled",
        }
        for entry in entries.values()
    ]


# ── The management tools ────────────────────────────────────────────────
# v1's `cli_*` set.  This family has no connection, so none of these have a
# link to rebuild or a tool count to report — what they change is the *section*,
# and what follows from that is the row a search finds.  The one thing worth
# checking is the thing this family is certain to meet: whether the program is
# actually on this machine.


def _prepare(
    config: Config, name: str, entry: dict[str, Any]
) -> tuple[CliToolSettings | None, str]:
    """Validate a `cli:` entry and write it, or say why not.

    Only one refusal, and it is not a name collision: a `cli:` row is called
    `cli:{name}`, so it cannot collide with anything — which is why this family
    needs no `toolfamily.refusal` and the other two do.
    """
    try:
        settings = _cli_tool(entry, name)
        configfile.upsert(SECTION, name, entry)
    except ConfigError as exc:
        return None, f"[refused] {exc}"
    return settings, ""


def _on_path(command: str) -> str:
    """A sentence about a command that is not installed, or `""`.

    **The one failure this family is certain to meet**, in its own docstring's
    words: an entry is a program already on the machine, and a model writing one
    down from a documentation page has no way to know whether that is true here.
    v1 answers it after the fact — `install` is shown when the command turns out
    to be missing — and this says it at the moment the entry is written, which is
    the only moment anybody is looking.

    The first word only: `python -m mytool` is a program plus its arguments, and
    the program is what has to exist.
    """
    words = command.split()
    program = words[0] if words else ""
    if not program or shutil.which(program):
        return ""
    return (
        f"\nNote: `{program}` is not on PATH, so this entry names a program that "
        f"is not installed here. It is still written down — add an `install` line "
        f"so a person is told how, or check the spelling."
    )


def _describe(name: str, raw: Mapping[str, Any]) -> str:
    """One line of `cli_list` — the entry as the file has it, and its switch.

    **No state word beyond the switch**, and that is this family's whole
    difference: `ready` and `failed` are facts about a link, and a command has
    none — the program is either on the machine or the entry is wrong about it,
    and neither is something a running process can watch for.  So the two words
    are `on` and `off`, and `off` is the hub's own word for a switch in the same
    position.
    """
    state = "off" if raw.get("enabled") is False else "on"
    lines = [f"- {name} [{state}]", f"    command  {raw.get('command', '')}"]
    if install := raw.get("install"):
        lines.append(f"    install  {install}")
    if source := raw.get("source"):
        parts = [str(source.get(key, "")) for key in ("type", "url", "version")]
        note = " ".join(part for part in parts if part)
        if note:
            lines.append(f"    source   {note}")
    if description := str(raw.get("description") or ""):
        lines.append(f"    {description}")
    return "\n".join(lines)


def build_server(config: Config) -> FastMCP:
    """Build the cli server.

    **The entries are held here and the tools change them in place**, which is
    the one thing about this family that is not like the other two: a `cli:`
    entry has no connection, so there is nothing to rebuild and nothing a
    restart is needed for — the row a search reads is derived from these entries
    on every `list_sources`, and the hub re-declares before every search.
    """
    entries: dict[str, CliToolSettings] = dict(config.cli)
    mcp: FastMCP = house_server(SERVER_NAME, instructions=INSTRUCTIONS)

    @mcp.tool(name=LIST_SOURCES)
    def list_sources() -> dict[str, Any]:
        """The `cli:` section as the one source this server holds.

        **Not a tool for the model**, and so not marked as one: the hub asks for
        this and records the answer, and the model never sees the name.  The
        whole list rather than a difference, because that is what makes a deleted
        entry stop being a hit — a merge reads an absent name as a row the source
        no longer has.

        **`up` and `enabled` are the same answer here and are still both said.**
        A command is a program on this machine: there is no connection that could
        be down, so what the section says is the whole truth about it — every
        entry is enabled or it is not, and nothing is ever *unreachable*.  The
        `enabled` flag is the operator's switch, and a switched-off entry is
        still declared, with `status: disabled` on its rows, because a model
        told "there is a command for this and somebody turned it off" is better
        off than one told nothing.

        `transport` is empty, and that is not a missing value: these rows have
        nothing behind them to connect to, which is also why they are absent from
        `servers()` — a source with no connection is not a server.

        Returns:
            `sources`: this server's whole holding — one source, `cli`, with its
            rows, one per configured entry.
        """
        return {
            "sources": [
                {
                    "name": SOURCE,
                    "category": CATEGORY,
                    "enabled": True,
                    "up": True,
                    "description": (
                        "The commands this machine already has, as the `cli:` "
                        "section records them."
                    ),
                    "transport": "",
                    "rows": rows(entries),
                }
            ]
        }

    @mcp.tool(name="cli_list", meta=FOR_THE_MODEL)
    async def cli_list() -> str:
        """List the commands recorded under `cli:`, with their switch.

        These are programs already installed on this machine, written down so
        you can be told they exist. They are catalogue rows rather than tools:
        `tool_search` finds a command by what it does, and nothing runs one yet
        (DESIGN.md §9).
        """
        written = configfile.read_section(SECTION)
        if not written:
            return "No commands are recorded under `cli:` yet. `cli_set` records one."
        return "\n".join(_describe(name, raw) for name, raw in written.items())

    @mcp.tool(name="cli_set", meta=FOR_THE_MODEL)
    async def cli_set(
        name: str,
        command: str,
        description: str,
        install: str = "",
        source: dict[str, str] | None = None,
        enabled: bool = True,
    ) -> str:
        """Record a command that is already installed on this machine.

        **This does not install anything and does not run anything.** It writes
        the entry down so you can find it with `tool_search` and so a person can
        see what this machine has. Record a command only when you know it is
        here — the answer says so if it is not on `PATH`. **This is the whole
        entry**: an `install` or `source` left out is not kept from an older
        version, so restate everything the entry should have.

        Args:
            name: The name it is recorded under, from `cli_list`.
            command: The invocation, resolved on `PATH`. One string, and it may
                be more than one word (`python -m mytool`).
            description: What it does and how it is called — this is the text you
                will be given when a search finds it, so say what it is for.
            install: How to get it, for a person who does not have it.
            source: Where the entry came from — `url`, `type`, `version`.
            enabled: False records it and keeps it out of the catalogue.
        """
        settings, why = _prepare(
            load(),
            name,
            {
                "command": command,
                "description": description or None,
                "install": install or None,
                "source": source,
                "enabled": enabled,
            },
        )
        if settings is None:
            return why
        entries[name] = settings
        logger.info("cli_set name=%s", name)
        return (
            f"`{name}` is recorded as `{command}`; a search finds it from your "
            f"next one."
        ) + _on_path(command)

    @mcp.tool(name="cli_remove", meta=FOR_THE_MODEL)
    async def cli_remove(name: str) -> str:
        """Forget a command recorded under `cli:`.

        Nothing is uninstalled — the program stays on the machine, and what goes
        is the entry that told you about it.

        Args:
            name: The entry to forget, from `cli_list`.
        """
        if name not in configfile.read_section(SECTION):
            return f"'{name}' is not recorded under `cli:` — see `cli_list`."
        configfile.remove(SECTION, name)
        entries.pop(name, None)
        logger.info("cli_removed name=%s", name)
        return (
            f"`{name}` is forgotten; its row leaves the catalogue at your next search."
        )

    @mcp.tool(name="cli_set_enabled", meta=FOR_THE_MODEL)
    async def cli_set_enabled(name: str, enabled: bool) -> str:
        """Switch a recorded command on or off, keeping the entry.

        Off keeps the entry written down and out of the catalogue — the same
        switch, with the same meaning, as on a server. Nothing is uninstalled
        either way.

        Args:
            name: The entry, from `cli_list`.
            enabled: True records it as usable; False keeps it out of the catalogue.
        """
        if name not in configfile.read_section(SECTION):
            return (
                f"'{name}' is not recorded under `cli:` — `cli_set` records one, "
                f"`cli_list` shows what is there."
            )
        configfile.set_enabled(SECTION, name, enabled)
        current = entries.get(name)
        if current is not None:
            entries[name] = replace(current, enabled=enabled)
        logger.info("cli_set_enabled name=%s enabled=%s", name, enabled)
        state = "on" if enabled else "off"
        return f"`{name}` is {state}; its entry stays in `cli:`."

    return mcp


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
