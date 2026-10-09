"""slife2-skills — the playbooks, as the plugin that owns them.

A **skill** is a directory under `<data>/skills/` with a `SKILL.md` in it: a
procedure for one kind of job, written to be *read* rather than called.  The
model gets two things from this server — the one tool that reads a playbook, and
one catalogue row per installed skill, so that `tool_search` can find a playbook
by what it is for instead of by a name the model would have had to know already.

**Why a process, when the folder is right there.**  A skill is a document and a
folder, so for a while this family needed no server at all and the hub read the
directory itself.  Two things made that the wrong home.  The first is that the
hub is the tool *set's* owner — which tools may be called, what a name resolves
to, what the budget trims — and reading somebody else's config section and
somebody else's folder is not that job.  The second is what is next: DESIGN.md
§9 has the rest of the reading family landing here (`skill_list`, the half a
model uses to find the name it then reads), and a model-facing tool needs a
server to be served from.

**This process does not write the catalogue.**  It answers `list_sources` and
the hub merges the answer, so the hub stays the only writer of the tool table,
the only process holding a connection to the db, and the only place where two
sources' claim on one name is settled (`slife2.mcp_server`).

**A credential is not a connection, and this server holds one.**  A skill whose
header declares `requires.env` — as these skills do for other hosts — needs a
value, and `skills:` in the config says where it comes from.  That resolution is
`slife2.config`'s, and what it resolved is held *here*, handed to whichever tool
reads the playbook, and reported to the model before it acts on instructions
that would fail.  Nothing about a declared key needs an address or a protocol;
what it needs is one process that knows the answer, and this is it.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from slife2 import skills
from slife2.audience import FOR_THE_MODEL
from slife2.config import Config, find_config_path, load
from slife2.mcp_server import (
    LIST_SOURCES,
    configure_logging,
    house_server,
    parse_serve_args,
    serve,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-skills"

#: This server's key in the config's `servers:` table, and so the name the
#: launcher starts it under and the hub connects to it by.
CONFIG_KEY = "skills-server"

#: The catalogue source these rows belong to, which is also what `tool_search`
#: filters on and what a result prints beside a hit.
#:
#: **Deliberately not this server's own name.**  A source's verdict is written
#: across every row it owns, so a source holding both this server's tool and its
#: documents would mark every playbook broken whenever the connection faltered —
#: and these rows have no connection whose state a verdict could come from,
#: which is why their `status` is the folder's to write and nothing else's.
SOURCE = "skills"

#: The category, and the namespace the row names carry (`skill:browser-harness`).
#: The prefix is not decoration: a name is a row's identity, and
#: `browser-harness` is both a skill *and* a `cli:` entry in this config.
CATEGORY = "skill"

INSTRUCTIONS = (
    "The playbooks kept in this machine's skills folder. `skill_use` reads one; "
    "the catalogue rows a search finds are the same documents, so a skill can be "
    "found by what it is for without knowing its name."
)


def catalogue(config: Config) -> list[dict[str, object]]:
    """Every installed skill as a catalogue row — the whole list, every time.

    **A skill's `schema` is the document itself.**  A playbook *is* its
    documentation, so the text is what the semantic leg of a search ranks, and
    "drive a browser" reaching `skill:browser-harness` is the entire point of
    having a row.  It is stored as the document `skill_use` would hand back, so
    what a search ranks and what a call returns are the same text and cannot
    drift apart.

    **A `SKILL.md` that cannot be read is a row with `error` on it**, not a row
    that is left out: the folder said the skill is installed, and a search that
    found nothing would send the model looking for a file the operator believes
    is there.  That verdict is the row's own, which is the one thing a merge
    honours from a source rather than from the runtime — there is no connection
    to have a state.

    **`remote_name` is what to call instead** — the argument `skill_use` takes —
    because the row is found and not called, and the answer a model gets for
    calling it should be the step that reaches the thing.

    The folder is re-read on every ask rather than snapshotted, and that is the
    whole of how a skill is installed: a directory dropped in is one the next
    call knows about, with nothing to restart and no list to keep in step.
    """
    environments = {
        name: dict(settings.env) for name, settings in config.skills.items()
    }
    documents: list[dict[str, object]] = []
    for skill in skills.scan():
        # Both names, because a skill is addressed by either and the operator
        # writing `skills:` has only one of them in front of them.
        supplied = environments.get(skill.name) or environments.get(
            skill.directory.name
        )
        try:
            note = skills.readiness(skill, supplied)
            text, status = skills.document(skill, note), "enabled"
        except OSError:
            text, status = "", "error"
        documents.append(
            {
                "name": f"{CATEGORY}:{skill.name}",
                "description": skill.description,
                "remote_name": skill.name,
                "schema": text,
                "status": status,
            }
        )
    return documents


def build_server(config: Config) -> FastMCP:
    """Build the skills server.

    The `skills:` section is read once, here, and that is the one part of this
    server that is not the folder's: what a skill is *given* is the operator's
    answer, resolved when the process started.  A `skills:` entry edited
    afterwards is a change the next start picks up, like every other.
    """
    environments = {
        name: dict(settings.env) for name, settings in config.skills.items()
    }

    mcp: FastMCP = house_server(SERVER_NAME, instructions=INSTRUCTIONS)

    @mcp.tool(name=LIST_SOURCES)
    def list_sources() -> dict[str, object]:
        """The skills folder as the one source this server holds.

        **Not a tool for the model**, and so not marked as one: the hub asks for
        this and records the answer, and the model never sees the name.  The
        whole list rather than a difference, because that is what makes a
        deleted skill stop being a hit — a merge reads an absent name as a row
        the source no longer has — and it is asked for again before every
        search, which is what keeps a skill dropped into the folder findable
        without restarting anything.

        **`up` is always true, and that is the honest answer.**  A skill is a
        file: there is no connection that could be down, so the folder is as
        reachable as the disk it is on.  A `SKILL.md` that cannot be read is a
        *row* carrying `error`, which is a different thing and is said where it
        belongs — the model is told the skill exists and is broken rather than
        not told at all.

        `transport` is empty, and that is not a missing value: these rows have
        nothing behind them to connect to, which is also why they are absent from
        `servers()` — a source with no connection is not a server.

        Returns:
            `sources`: this server's whole holding — one source, `skills`, with
            one row per installed skill, each carrying the name a search reports,
            the skill's own description, the name `skill_use` takes, the document
            itself as what a search can match on, and the folder's verdict on
            whether it can be read.
        """
        return {
            "sources": [
                {
                    "name": SOURCE,
                    "category": CATEGORY,
                    "enabled": True,
                    "up": True,
                    "description": (
                        "The playbooks installed on this machine, one directory "
                        "each under the data directory's `skills/`."
                    ),
                    "transport": "",
                    "rows": catalogue(config),
                }
            ]
        }

    @mcp.tool(name=skills.USE_TOOL, meta=FOR_THE_MODEL)
    async def skill_use(name: str) -> str:
        """Read a skill: a playbook kept on this machine, written to be followed
        rather than called.

        A skill carries the procedure for one kind of job and the details that
        are easy to get wrong — the commands, the paths, the order.  Read it
        before starting that job.

        Args:
            name: The skill's name, e.g. `browser-harness`.

        Returns:
            The playbook, preceded by one line saying what the paths in it are
            relative to — a skill's own paths are relative to its directory —
            and, when the skill declares requirements, a line saying whether
            they are met and how to supply what is missing.  For a name that is
            not installed, the answer lists the ones that are.
        """
        text, ok = await skills.use(name, environments=environments)
        if not ok:
            # Raised rather than returned, so the call is a *failure* the caller
            # can see: the loop's contract is that a tool produces text, and a
            # refusal returned as text would reach the transcript as a success.
            # It is still a value and not a crash — `slife2.toolhub` reads a
            # peer's refusal as the model's answer, not as a broken server.
            raise ToolError(text)
        return text

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
