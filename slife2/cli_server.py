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

**What it serves today is one row per entry and no tools at all.**  A plugin
whose own tools are all our own code's is the ordinary case rather than a special
one — `slife2-llm-embeddings` and the agent server are the same, and the db marks
only `turn_list` and `turn_read` for the model — and it is what `list_sources` is
for: a search reads the catalogue, so an entry nobody wrote a row for is a
command only a model that already knew its name could reach.

**This process does not write the catalogue.**  It answers `list_sources` and
the hub merges the answer — the arrangement `slife2.mcp_server` describes — so
that the hub remains the only writer of the tool table, the only process holding
a connection to the db, and the only place two sources' claim on one name is
settled.  What belongs here is the *section*: parsing it is `slife2.config`'s
business and turning it into rows is this file's.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from slife2.config import Config, find_config_path, load
from slife2.mcp_server import (
    LIST_SOURCES,
    configure_logging,
    house_server,
    parse_serve_args,
    serve,
)

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

INSTRUCTIONS = (
    "The commands the config's `cli:` section records as already installed on "
    "this machine. Nothing here runs one yet — the entry is a row a search can "
    "find, and the tool that executes one is this server's next change."
)


def rows(config: Config) -> list[dict[str, Any]]:
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
        for entry in config.cli.values()
    ]


def build_server(config: Config) -> FastMCP:
    """Build the cli server.

    The config is read once, here, and the rows are built from it on each ask.
    A `cli:` entry is a fact about a file a running process has already loaded,
    so nothing can change underneath it that this process could observe — and a
    config edited on disk is a change the next start picks up, like every other.
    """
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
                    "rows": rows(config),
                }
            ]
        }

    return mcp


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
