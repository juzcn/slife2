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
from collections.abc import Mapping
from dataclasses import replace

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from slife2 import configfile, skills
from slife2.audience import FOR_THE_MODEL
from slife2.config import Config, SkillSettings
from slife2.mcp_server import LIST_SOURCES, house_server, serve_plugin

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

#: The config section this family owns, and the only thing it tells
#: `slife2.configfile`: that module edits a section and knows nothing about what
#: is in one.  It is *not* where a skill lives — the folder is — and the section
#: is only the two things a folder cannot say: what a skill is given, and
#: whether it is switched on.
SECTION = "skills"

INSTRUCTIONS = (
    "The playbooks kept in this machine's skills folder, and the `skill_*` tools "
    "that install, remove and switch one. `skill_use` reads one; the catalogue "
    "rows a search finds are the same documents, so a skill can be found by what "
    "it is for without knowing its name."
)


def catalogue(entries: Mapping[str, SkillSettings]) -> list[dict[str, object]]:
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

    Takes the `skills:` section rather than the `Config` because the section is
    what the tools change — and because the one thing the folder cannot say
    about a skill, whether it is switched on, is said there.
    """
    documents: list[dict[str, object]] = []
    for skill in skills.scan():
        # Both names, because a skill is addressed by either and the operator
        # writing `skills:` has only one of them in front of them.
        found = entries.get(skill.name) or entries.get(skill.directory.name)
        supplied = dict(found.env) if found is not None else None
        try:
            note = skills.readiness(skill, supplied)
            text, status = skills.document(skill, note), "enabled"
        except OSError:
            text, status = "", "error"
        if found is not None and not found.enabled:
            # The switch wins over the folder's verdict: both are true, and this
            # is the one the model has to act on — a switched-off skill is not a
            # broken one, and `_func_tool_load`'s two refusals say different
            # things about what to do next.
            status = "disabled"
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

    **The entries are held here and the tools change them in place**, for the
    reason the folder is re-read on every ask: a skill is installed by putting a
    directory in a folder, and what is *given* to one is an entry in the config
    — so neither half needs a restart, and the tool that changes either one is
    answering about a state the next search will already see.
    """
    entries: dict[str, SkillSettings] = dict(config.skills)

    def supplied_for(name: str) -> dict[str, dict[str, str]]:
        """The `skills:` section as `skill_use` wants it — name → `env`."""
        return {one: dict(settings.env) for one, settings in entries.items()}

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
                    "rows": catalogue(entries),
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
        # Found here only to read the switch off it: the skill may be filed under
        # either its own name or its directory's, and only the folder knows
        # which, so this is the same both-names lookup `catalogue` makes.
        skill = skills.find(name.strip())
        if skill is not None:
            found = entries.get(skill.name) or entries.get(skill.directory.name)
            if found is not None and not found.enabled:
                raise ToolError(
                    f"{name!r} is switched off, so it cannot be read — its "
                    f"`skills:` entry says `enabled: false`, which is the "
                    f"operator's decision and not a fault. `skill_list` reports "
                    f"the state of every installed skill."
                )
        text, ok = await skills.use(name, environments=supplied_for(name))
        if not ok:
            # Raised rather than returned, so the call is a *failure* the caller
            # can see: the loop's contract is that a tool produces text, and a
            # refusal returned as text would reach the transcript as a success.
            # It is still a value and not a crash — `slife2.toolhub` reads a
            # peer's refusal as the model's answer, not as a broken server.
            raise ToolError(text)
        return text

    # ── The management tools ─────────────────────────────────────────────
    # v1's `skill_*` set, and the half DESIGN.md §9 named as next: a skill is a
    # directory, so installing one is writing files — which is the only thing in
    # this system that writes anywhere but the config, and the only one whose
    # paths a model chooses.

    def _settings(name: str, skill: skills.Skill | None) -> SkillSettings | None:
        """The `skills:` entry for a skill, under either of its two names."""
        if skill is not None:
            found = entries.get(skill.name) or entries.get(skill.directory.name)
            if found is not None:
                return found
        return entries.get(name)

    def _key_for(
        name: str, skill: skills.Skill | None, entries: Mapping[str, SkillSettings]
    ) -> str:
        """Which `skills:` key names this skill.

        **The entry that is already there wins**, because that is the one the
        operator wrote and its value is what the skill is given.  The skill's
        own name is next, and the name the caller used last: a skill filed under
        a name nobody wrote an entry for gets one under its own name, which is
        the name `skill_list` prints.
        """
        for candidate in (
            skill.name if skill else None,
            skill.directory.name if skill else None,
            name,
        ):
            if candidate and candidate in entries:
                return candidate
        return skill.name if skill else name

    @mcp.tool(name="skill_list", meta=FOR_THE_MODEL)
    async def skill_list() -> str:
        """List the playbooks installed on this machine, with their state.

        A skill is a procedure you read before doing a job, and `skill_use` is
        what reads one. This is how you find the name to read: each line says
        what the playbook is for, and whether it needs something you have not
        got — a key, a program — before you follow instructions that would fail
        halfway through.
        """
        found = skills.scan()
        if not found:
            return (
                "No skills are installed. A skill is a directory with a "
                "`SKILL.md` in it; `skill_set` installs one."
            )
        lines: list[str] = []
        for skill in found:
            settings = _settings(skill.name, skill)
            state = "off" if settings is not None and not settings.enabled else "on"
            lines.append(f"- {skill.name} [{state}]")
            if skill.description:
                lines.append(f"    {skill.description}")
            try:
                note = skills.readiness(
                    skill, dict(settings.env) if settings is not None else None
                )
            except OSError as exc:
                note = f"cannot be read: {exc}"
            if note:
                lines.append(f"    {note}")
        return "\n".join(lines)

    @mcp.tool(name="skill_set", meta=FOR_THE_MODEL)
    async def skill_set(name: str, files: list[dict[str, str]]) -> str:
        """Install or replace a skill: write a playbook onto this machine.

        A skill is a directory with a `SKILL.md` in it, so that file has to be
        one of the paths. Write the playbook for one kind of job: the procedure,
        the commands, the order, the details that are easy to get wrong — it is
        read by a model at the moment it starts that job, not by a person
        browsing. Anything else you write lands beside it, and the paths inside
        the playbook are relative to the directory.

        It replaces an installed skill of the same name in one step, so a
        half-written playbook is never readable. `skill_remove` removes one.

        Args:
            name: The directory it lives in — what `skill_use` takes.
            files: The files to write, each `{"path": "SKILL.md", "content":
                "..."}`. Paths are relative to the skill's own directory, and
                one that leaves it is refused.
        """
        if not name.strip():
            return "[refused] a skill needs a name"
        try:
            installed = skills.install(name.strip(), files)
        except ValueError as exc:
            return f"[refused] {exc}"
        except OSError as exc:
            return f"[refused] could not write {name!r}: {exc}"
        logger.info("skill_set name=%s dir=%s", name, installed)
        return (
            f"`{name}` is installed ({len(files)} file(s)). A search finds it "
            f"from your next one, and `skill_use` reads it."
        )

    @mcp.tool(name="skill_remove", meta=FOR_THE_MODEL)
    async def skill_remove(name: str) -> str:
        """Uninstall a skill: delete its directory and everything in it.

        The playbook and any scripts beside it go. The `skills:` entry naming
        what it was given is left alone — it is harmless without a folder, and
        a name waiting for its files is what re-installing it uses.

        Args:
            name: The skill to remove, from `skill_list`.
        """
        try:
            removed = skills.remove(name.strip())
        except ValueError as exc:
            return f"[refused] {exc}"
        if not removed:
            return f"'{name}' is not installed — see `skill_list`."
        logger.info("skill_removed name=%s", name)
        return (
            f"`{name}` is uninstalled — the directory and everything in it. Its "
            f"`skills:` entry stays: it is what the playbook is given, and it is "
            f"what re-installing it needs."
        )

    @mcp.tool(name="skill_set_enabled", meta=FOR_THE_MODEL)
    async def skill_set_enabled(name: str, enabled: bool) -> str:
        """Switch a skill on or off, keeping it installed.

        Off is how a playbook stays on the machine and out of the way: it stops
        being findable and `skill_use` refuses it, and the files are untouched.
        The switch is written in the config, which is the only place a decision
        can live — what is installed is a directory, and a directory cannot say
        whether somebody wants it.

        Args:
            name: The skill, from `skill_list`.
            enabled: True offers it again; False keeps it out of the way.
        """
        skill = skills.find(name.strip())
        if skill is None and name.strip() not in entries:
            return f"'{name}' is not installed — see `skill_list`."
        key = _key_for(name.strip(), skill, entries)
        if key in configfile.read_section(SECTION):
            configfile.set_enabled(SECTION, key, enabled)
        elif not enabled:
            # **A skill with no entry is on**, so the only fact worth writing
            # down is that it is off — and switching one back on is the absence
            # of that fact rather than an `enabled: true` nothing reads.  Which
            # is also why this is not `set_enabled` alone: that call returns
            # False for an entry that is not there, and a tool that reported
            # success over an unwritten file is the failure this whole section
            # is arranged to avoid.
            configfile.upsert(SECTION, key, {"enabled": False})
        current = entries.get(key)
        entries[key] = (
            replace(current, enabled=enabled)
            if current is not None
            else SkillSettings(name=key, enabled=enabled)
        )
        logger.info("skill_set_enabled name=%s key=%s enabled=%s", name, key, enabled)
        if enabled:
            return f"`{name}` is on again; a search finds it from your next one."
        return f"`{name}` is off and still installed; its files are untouched."

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
