"""slife2-cli — the `cli:` section, as the plugin that owns it.

A `cli:` entry is a program already on this machine — `yt-dlp`, a browser
harness — written down so the model can be told it exists.  There is no process
to start, no URL to connect to and no credential: what an entry has is a
command, a sentence about what it does, and how to install it if it is missing.

**Why a process for a family with nothing to connect to.**  Nothing here needs a
connection, and that was the argument for keeping it out of one — a server
invented to wrap a row-builder is a process that exists to be connected to.
What the process is for is the *section*: a command is reachable only if a
search can find it, and the search reads the catalogue the hub merges from this
process's declaration.  So this is where the `cli_*` tools that edit the section
live, and **nothing here runs a command: a command is run through the shell the
model's tool set provides, the same way a skill is carried out by the model
rather than by a tool.**  An entry has no argv of ours: it says what the command
is, what it is for, and how to install it.

**What it serves to the model is the `cli_*` tools that edit the section**, and
to the hub the one row per entry it declares.  This was the last of the four
families to get its tools, and the reason it read as the odd one out for so long:
its *entries* are not tools and nothing here runs one, so it had nothing
model-facing to offer until the tools that write the section arrived.  What a
command's row is for is `list_sources`: a search reads the catalogue, so an
entry nobody wrote a row for is a command only a model that already knew its
name could reach.

**This process does not write the catalogue.**  It answers `list_sources` and
the hub merges the answer — the arrangement `slife2.libs.plugin.protocol` describes — so
that the hub remains the only writer of the tool table and the only place two
sources' claim on one name is settled.  What belongs here is the *section*:
parsing it is `slife2.libs.host.config`'s business and turning it into rows is
this file's.
"""

from __future__ import annotations

import logging
import shlex
import shutil
from collections.abc import Callable, Mapping
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from slife2.libs.host import configfile
from slife2.libs.host.config import (
    CLI_SERVER_NAME,
    CliToolSettings,
    Config,
    ConfigError,
    _cli_tool,
    load_cached,
)
from slife2.libs.host.tool_config import CliConfig, cli_entry, load_cli
from slife2.libs.plugin.audience import FOR_THE_MODEL
from slife2.libs.plugin.family import (
    LIST_PAGE,
    Documents,
    Homes,
    build_document_server,
    listed,
)
from slife2.libs.plugin.protocol import CLI_SOURCE as _SOURCE
from slife2.libs.plugin.protocol import serve_plugin

logger = logging.getLogger(__name__)

SERVER_NAME = CLI_SERVER_NAME

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
#: Re-exported from the plugin contract, because three other places need the
#: same word: the hub files under it, and the two family plugins refuse a
#: name that would collide with it.  See `slife2.libs.plugin.protocol.SKILLS_SOURCE`.
SOURCE = _SOURCE

#: The category, and the namespace the row names carry (`cli:yt-dlp`).  The
#: prefix is not decoration: a name is a row's identity, and `browser-harness` is
#: both a `cli:` entry *and* the skill that documents it.
CATEGORY = "cli"

#: The config section this family owns, and the only thing it tells
#: `slife2.libs.host.configfile`: that module edits a section and knows nothing about what
#: is in one.
SECTION = "cli"

INSTRUCTIONS = (
    "The commands the config's `cli:` section records as already installed on "
    "this machine, and the `cli_*` tools that edit that section. Nothing here "
    "runs a command — an entry is a row a search can find, and a command is run "
    "through the shell, not called as a tool."
)


def rows(entries: Mapping[str, CliToolSettings]) -> list[dict[str, Any]]:
    """The `cli:` section as catalogue rows, one per entry.

    **A switched-off entry is a row too**, carrying `disabled`, because the model
    is better told that the command exists and is off than not told at all.  That
    verdict rides on the row rather than coming from the runtime, and it is the
    one family where that is true (`slife2.libs.store.db._plan`): there is no connection to
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
# The `cli_*` set.  This family has no connection, so none of these have a
# link to rebuild or a tool count to report — what they change is the *section*,
# and what follows from that is the row a search finds.  The one thing worth
# checking is the thing this family is certain to meet: whether the program is
# actually on this machine.


def _prepare(name: str, config: CliConfig) -> CliToolSettings:
    """Validate a `cli:` entry and write it, or raise why it cannot be.

    **Raised, not returned**, which is the contract `skill_set` and `skill_use`
    keep: a refusal returned as ordinary text arrives with `ok` true and reaches
    the transcript as a success.

    **Both refusals come before the write.**  `_cli_tool` refuses an entry
    naming no program, and the split below refuses a command line `shlex` cannot
    take apart — the same split `_missing_program` reads to name the program.
    A line nothing can split is a row whose program cannot be checked against
    this machine, so it is refused here rather than written over a success.
    Neither is a name collision: a `cli:` row is called `cli:{name}`, so it
    cannot collide with anything — which is why this family needs no
    `slife2.libs.plugin.family.refusal` and the other two do.

    **No `Config`, unlike the two connection families' `_prepare`s.**  Theirs
    take it because they ask `slife2.libs.plugin.family.refusal` whether the
    name is one the hub already owns, and that question needs the whole config;
    this family has no such question, so the parameter used to sit here
    unreferenced while `cli_set` still paid a `load()` for it on every call.
    The parser below (`_cli_tool`) needs only the entry and the name, and the
    write is `slife2.libs.host.configfile`'s own.
    """
    command = config.command
    try:
        # The same split `_missing_program` makes, moved before the write: the
        # program it names is what the on-PATH check reads, and refusing an
        # unsplittable line here is the only way the refusal lands on the call
        # rather than a note.
        shlex.split(command, posix=False)
    except ValueError as exc:
        raise ToolError(
            f"`{command}` cannot be split into a command line — check for an "
            f"unbalanced quote, or how the path is quoted"
        ) from exc
    entry = cli_entry(config)
    try:
        settings = _cli_tool(entry, name)
        configfile.upsert(SECTION, name, entry)
    except ConfigError as exc:
        raise ToolError(str(exc)) from exc
    return settings


def _missing_program(command: str) -> str:
    """The program this entry names and this machine has not got, or `""`.

    **The one failure this family is certain to meet**, in its own docstring's
    words: an entry is a program already on the machine, and a model writing one
    down from a documentation page has no way to know whether that is true here.
    Answering it after the fact — `install` is shown when the command turns out
    to be missing — says it too late; this says it at the moment the entry is
    written, which is the only moment anybody is looking.

    **A name and not a sentence about it.**  It comes back as the answer's
    `missing_program`, the program itself, because that is what a caller acts on:
    the old note wrapped it in a paragraph of advice, and the two things worth
    doing about it (add an `install` line, or fix the spelling) are the tool's to
    say once rather than every answer's to repeat.

    **The line is split only to name the program, so that quotes are honoured**:
    `"C:\\Program Files\\tool\\tool.exe" --json` names one program, not
    `"C:\\Program`, and a Windows path's backslashes are literal rather than
    escape sequences — which is why this is `posix=False` and why the quotes it
    leaves on a word come off here.  The first word only, because
    `python -m mytool` is a program plus its arguments and the program is what
    has to exist.  Running the line is the shell's, not this split's.

    **The split cannot fail here**, and there is deliberately no `try` for it:
    `_prepare` has already refused a line `shlex` cannot take apart, so the only
    caller that reaches this has one that splits.  The `except ValueError` this
    used to carry answered `""` — the same silence as "the program is on PATH" —
    for a failure that by then could not happen.
    """
    words = shlex.split(command, posix=False)
    program = words[0].strip("\"'") if words else ""
    if not program or shutil.which(program):
        return ""
    return program


def _describe(name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """One row of `cli_list` — what the command is, and whether it is usable.

    **No state word beyond the switch**, and that is this family's whole
    difference: `ready` and `failed` are facts about a link, and a command has
    none — the program is either on the machine or the entry is wrong about it,
    and neither is something a running process can watch for.  So the two words
    are `on` and `off`, and `off` is the word the hub uses for a switch in the
    same position.

    **A listing is for choosing, not for reading the entry.**  `command` is how
    it is used, `description` is what it is for, `install` is how to get it, and
    `missing_program` is the one machine fact that can make it unusable; the
    `source` provenance is `cli_get`'s.
    """
    command = str(raw.get("command") or "")
    row: dict[str, Any] = {
        "name": name,
        "state": "off" if raw.get("enabled") is False else "on",
        "command": command,
    }
    if install := raw.get("install"):
        row["install"] = str(install)
    if description := str(raw.get("description") or ""):
        row["description"] = description
    if missing := _missing_program(command):
        row["missing_program"] = missing
    return row


#: What `list_sources` says this source is, for the hub and for `servers()`.
DESCRIPTION = (
    "The commands this machine already has, as the `cli:` section records them."
)


def build_server(
    config: Config, *, source: Callable[[], Config] | None = None
) -> FastMCP:
    """Build the cli server.

    **The entries are held and the tools change them in place**, which is the
    one thing about this family that is not like the connection families: a
    `cli:` entry has no connection, so there is nothing to rebuild and nothing a
    restart is needed for — the row a search reads is derived from these entries
    on every `list_sources`, and the hub re-declares before every search.

    Held **per home** (`slife2.libs.plugin.family.Homes`), like every other
    family: `cli:` is a section of the home's `slife2.yaml`, so a process serving
    two data directories keeps two mirrors and neither sees the other's commands.
    `source` is what says where a home's section comes from — `load_cached` in
    production, nothing at all when a test hands a config in.
    """
    return build_document_server(
        config,
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        catalogue_source=SOURCE,
        category=CATEGORY,
        section=SECTION,
        description=DESCRIPTION,
        entries=lambda theirs: theirs.cli,
        source=source,
        rows=rows,
        management=register_tools,
    )


def register_tools(mcp: FastMCP, homes: Homes[Documents]) -> None:
    """The `cli_*` tools, which are this family's vocabulary.

    Every body opens by resolving the home the call is for: what these tools
    read and mutate is one home's mirror, and a mirror bound once at build time
    would be whichever home this process happened to be started in.
    """

    def entry_of(name: str) -> CliConfig:
        """One entry's canonical object, or this family's miss."""
        raw = configfile.read_section(SECTION).get(name)
        if not isinstance(raw, dict):
            raise ToolError(
                f"'{name}' is not recorded under `cli:` — `cli_set` records "
                f"one, `cli_list` shows what is there."
            )
        try:
            return load_cli(raw, where=f"cli.{name}")
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(name="cli_list", meta=FOR_THE_MODEL)
    async def cli_list(limit: int = 0, offset: int = 0) -> dict[str, Any]:
        """List the commands recorded under `cli:`, with their switch.

        Each row says what the command is for, whether it is on, its invocation
        and how to install it, and `missing_program` when the program is not on
        `PATH`. The `source` provenance is `cli_get`'s, not the browse's.

        Args:
            limit: How many entries this page may hold. Omit for the cap.
            offset: How many entries to skip — that is how you page.

        Returns:
            `commands` — one row per entry, each with `name`, `state` (`on` or
            `off`), `command`, `description` when the entry has one, `install`
            when it has one, and `missing_program` when the program is absent —
            and `total`, `limit` and `offset`, so
            `offset + len(commands) < total` says whether more remain.
        """
        written = configfile.read_section(SECTION)
        rows = [_describe(name, raw) for name, raw in written.items()]
        return listed(rows, "commands", limit, offset, cap=LIST_PAGE)

    @mcp.tool(name="cli_get", meta=FOR_THE_MODEL)
    async def cli_get(name: str) -> dict[str, Any]:
        """Read one recorded command's whole entry, by name.

        The answer is the canonical entry — `config` has `command`,
        `description`, `install`, `source` and `enabled` — so `cli_set` takes
        the same object back. `state` is `on` or `off`, and `missing_program`
        names the program this machine has not got, when it has not.

        Args:
            name: The key it is recorded under.
        """
        config = entry_of(name)
        answer: dict[str, Any] = {
            "name": name,
            "config": config.model_dump(),
            "state": "on" if config.enabled else "off",
        }
        if missing := _missing_program(config.command):
            answer["missing_program"] = missing
        return answer

    @mcp.tool(name="cli_set", meta=FOR_THE_MODEL)
    async def cli_set(name: str, config: CliConfig) -> dict[str, Any]:
        """Record or replace one command — the whole entry, and no patch.

        It writes the entry down for a search to find, installing and running
        nothing. Every field is required; a field left out is a schema error,
        not a silent default. Read `cli_get`, change what you mean to change,
        and pass the whole `config` back.

        The answer is the entry as written — the same `config` shape `cli_get`
        gives — plus `state` (`on`, or `off` for a switch written off), and
        `missing_program` when the program this entry names is not on `PATH`.
        That is a note about the machine and not about the entry: it is written
        down either way.

        Args:
            name: The key it is recorded under.
            config: The whole entry — the `config` object `cli_get` returned.
        """
        settings = _prepare(name, config)
        homes.current().entries[name] = settings
        logger.info("cli_set name=%s", name)
        answer: dict[str, Any] = {
            "name": name,
            "config": config.model_dump(),
            "state": "on" if config.enabled else "off",
        }
        if missing := _missing_program(config.command):
            answer["missing_program"] = missing
        return answer

    @mcp.tool(name="cli_remove", meta=FOR_THE_MODEL)
    async def cli_remove(name: str) -> dict[str, Any]:
        """Forget a command recorded under `cli:`.

        The program itself stays on the machine. A name that is not recorded is
        refused, so a success means the entry is gone.

        Args:
            name: The key it is recorded under.

        Returns:
            `name` and `removed`, which a success makes true.
        """
        if name not in configfile.read_section(SECTION):
            # **Refused by raising, for `cli_set`'s reason**: returned as
            # ordinary text this arrives with `ok` true and reads as a removal
            # that happened.  Nothing was written, so the call is a failure the
            # caller can see.
            raise ToolError(f"'{name}' is not recorded under `cli:` — see `cli_list`.")
        configfile.remove(SECTION, name)
        homes.current().forgotten(name)
        logger.info("cli_removed name=%s", name)
        return {"name": name, "removed": True}

    @mcp.tool(name="cli_set_enabled", meta=FOR_THE_MODEL)
    async def cli_set_enabled(name: str, enabled: bool) -> dict[str, Any]:
        """Switch a recorded command on or off, keeping the entry.

        Args:
            name: The key it is recorded under.
            enabled: True records it as usable; False keeps it out of search,
                the same switch as on a server.

        Returns:
            `name`, `enabled`, and `state` — `on` or `off` — which is the same
            word the listing's rows carry.
        """
        if name not in configfile.read_section(SECTION):
            # The same refusal as `cli_remove`'s, and for the same reason: a
            # switch answered as ordinary text arrives with `ok` true and reads
            # as one that was set.
            raise ToolError(
                f"'{name}' is not recorded under `cli:` — `cli_set` records one, "
                f"`cli_list` shows what is there."
            )
        configfile.set_enabled(SECTION, name, enabled)
        # No `make`: an entry this process is not holding is one another instance
        # wrote, and it is the file's answer that counts until the next start.
        homes.current().switched(name, enabled)
        logger.info("cli_set_enabled name=%s enabled=%s", name, enabled)
        return {"name": name, "enabled": enabled, "state": "on" if enabled else "off"}


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
