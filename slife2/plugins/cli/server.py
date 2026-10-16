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
the hub merges the answer — the arrangement `slife2.libs.plugin.protocol` describes — so
that the hub remains the only writer of the tool table, the only process holding
a connection to the db, and the only place two sources' claim on one name is
settled.  What belongs here is the *section*: parsing it is `slife2.libs.host.config`'s
business and turning it into rows is this file's.
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
from slife2.libs.plugin.audience import FOR_THE_MODEL
from slife2.libs.plugin.family import Documents, Homes, build_document_server
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
    "this machine, and as the `cli_*` tools that edit that section. Nothing here "
    "runs a command — an entry is a row a search can find, and the tool that "
    "executes one is this server's next change."
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
# v1's `cli_*` set.  This family has no connection, so none of these have a
# link to rebuild or a tool count to report — what they change is the *section*,
# and what follows from that is the row a search finds.  The one thing worth
# checking is the thing this family is certain to meet: whether the program is
# actually on this machine.


def _prepare(name: str, entry: dict[str, Any]) -> tuple[CliToolSettings | None, str]:
    """Validate a `cli:` entry and write it, or say why not.

    `(settings, "")` means it was written; `(None, why)` is a sentence for the
    caller to raise as the refusal — the contract `skill_set` and `skill_use`
    keep, and the one whose text a `[refused]` prefix used to mark while it was
    returned as a success.

    **Both refusals come before the write.**  `_cli_tool` refuses an entry
    naming no program, and the split below refuses a command line `shlex` cannot
    take apart — which the argv runner (`DESIGN.md` §9) would meet on the way in
    too.  A line nothing can split is a row a search finds and no call can run,
    so it is refused here rather than written over a success.  Neither is a name
    collision: a `cli:` row is called `cli:{name}`, so it cannot collide with
    anything — which is why this family needs no `toolfamily.refusal` and the
    other two do.

    **No `Config`, unlike the two connection families' `_prepare`s.**  Theirs
    take it because they ask `toolfamily.refusal` whether the name is one the hub
    already owns, and that question needs the whole config; this family has no
    such question, so the parameter used to sit here unreferenced while
    `cli_set` still paid a `load()` for it on every call.  The parser below
    (`_cli_tool`) needs only the entry and the name, and the write is
    `slife2.libs.host.configfile`'s own.
    """
    command = entry.get("command")
    try:
        # The same split `_on_path` makes, moved before the write: the entry is
        # what the argv runner reads, and refusing an unsplittable line here is
        # the only way the refusal lands on the call rather than a note.
        shlex.split(command if isinstance(command, str) else "", posix=False)
    except ValueError:
        return None, (
            f"`{command}` cannot be split into a command line — check for an "
            f"unbalanced quote, or how the path is quoted"
        )
    try:
        settings = _cli_tool(entry, name)
        configfile.upsert(SECTION, name, entry)
    except ConfigError as exc:
        return None, str(exc)
    return settings, ""


def _on_path(command: str) -> str:
    """A sentence about a command that is not installed, or `""`.

    **The one failure this family is certain to meet**, in its own docstring's
    words: an entry is a program already on the machine, and a model writing one
    down from a documentation page has no way to know whether that is true here.
    v1 answers it after the fact — `install` is shown when the command turns out
    to be missing — and this says it at the moment the entry is written, which is
    the only moment anybody is looking.

    **The command line is split the way the argv runner will take it apart**
    (`DESIGN.md` §9: an argv rather than a shell), so that quotes are honoured:
    `"C:\\Program Files\\tool\\tool.exe" --json` names one program, not
    `"C:\\Program`, and a Windows path's backslashes are literal rather than
    escape sequences — which is why this is `posix=False` and why the quotes it
    leaves on a word come off here.  The first word only, because
    `python -m mytool` is a program plus its arguments and the program is what
    has to exist.

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

    @mcp.tool(name="cli_list", meta=FOR_THE_MODEL)
    async def cli_list() -> str:
        """List the commands recorded under `cli:`, with their switch.

        Each is a command recorded as installed on this machine, and a row in
        the tool catalogue: `tool_search` finds one by what it does.
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

        It writes the entry down for `tool_search`, installing and running
        nothing; the answer says whether the command is on `PATH`. This is the
        whole entry: a field left out is dropped, so restate everything.

        Args:
            name: The name it is recorded under, from `cli_list`.
            command: The invocation, resolved on `PATH` — one string, which may
                be more than one word (`python -m mytool`).
            description: What it does, in the words a search should find it by.
            install: How to get it, for somebody who does not have it.
            source: Where the entry came from — `url`, `type`, `version`.
            enabled: False records it and keeps it out of the catalogue.
        """
        settings, why = _prepare(
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
            # **Refused by raising, so the call is a failure the caller can see.**
            # `ok` false is this system's word for a refusal — `slife2.clients.gateway`
            # splits a peer's refusal from a transport fault on exactly this flag,
            # `slife2.plugins.toolhub.server` reads a raised `ToolError` as a value with `ok`
            # false, and the tool panel renders `ok` false as a failed call.
            # Returned as ordinary text the refusal would arrive with `ok` true
            # and read as a success.
            raise ToolError(why)
        homes.current().entries[name] = settings
        logger.info("cli_set name=%s", name)
        return (
            f"`{name}` is recorded as `{command}`; a search finds it from your "
            f"next one."
        ) + _on_path(command)

    @mcp.tool(name="cli_remove", meta=FOR_THE_MODEL)
    async def cli_remove(name: str) -> str:
        """Forget a command recorded under `cli:`.

        The program itself stays on the machine.

        Args:
            name: The entry to forget, from `cli_list`.
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
        return (
            f"`{name}` is forgotten; its row leaves the catalogue at your next search."
        )

    @mcp.tool(name="cli_set_enabled", meta=FOR_THE_MODEL)
    async def cli_set_enabled(name: str, enabled: bool) -> str:
        """Switch a recorded command on or off, keeping the entry.

        Args:
            name: The entry, from `cli_list`.
            enabled: True records it as usable; False keeps it out of the
                catalogue, the same switch as on a server.
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
        state = "on" if enabled else "off"
        return f"`{name}` is {state}; its entry stays in `cli:`."


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
