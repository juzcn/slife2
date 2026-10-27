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
somebody else's folder is not that job.  The second is what is served here: the
reading family is a pair — `skill_list`, the half a model uses to find the name
it then reads, and `skill_use`, the half that reads it — and a model-facing tool
needs a server to be served from.

**This process does not write the catalogue.**  It answers `list_sources` and
the hub merges the answer, so the hub stays the only writer of the tool table
and the only place where two sources' claim on one name is settled
(`slife2.libs.plugin.protocol`).

**A credential is not a connection, and this server holds one.**  A skill whose
header declares `requires.env` — as these skills do for other hosts — needs a
value, and `skills:` in the config says where it comes from.  That resolution is
`slife2.libs.host.config`'s, and what it resolved is held *here*, handed to whichever tool
reads the playbook, and reported to the model before it acts on instructions
that would fail.  Nothing about a declared key needs an address or a protocol;
what it needs is one process that knows the answer, and this is it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from slife2.libs.host import configfile
from slife2.libs.host.config import (
    SKILLS_SERVER_NAME,
    Config,
    ConfigError,
    SkillSettings,
    load_cached,
    resolve_secret,
)
from slife2.libs.plugin.audience import FOR_THE_MODEL
from slife2.libs.plugin.family import (
    LIST_PAGE,
    Documents,
    Homes,
    build_document_server,
    listed,
)
from slife2.libs.plugin.protocol import SKILLS_SOURCE as _SOURCE
from slife2.libs.plugin.protocol import serve_plugin
from slife2.plugins.skills import playbooks as skills

logger = logging.getLogger(__name__)

SERVER_NAME = SKILLS_SERVER_NAME

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
#: Re-exported from the plugin contract — see `slife2.libs.plugin.protocol`.
SOURCE = _SOURCE

#: The category, and the namespace the row names carry (`skill:browser-harness`).
#: The prefix is not decoration: a name is a row's identity, and
#: `browser-harness` is both a skill *and* a `cli:` entry in this config.
CATEGORY = "skill"

#: The config section this family owns, and the only thing it tells
#: `slife2.libs.host.configfile`: that module edits a section and knows nothing about what
#: is in one.  It is *not* where a skill lives — the folder is — and the section
#: is only the two things a folder cannot say: what a skill is given, and
#: whether it is switched on.
SECTION = "skills"

INSTRUCTIONS = (
    "The playbooks kept in this machine's skills folder, and the `skill_*` tools "
    "that author, install, change, remove and switch one. `skill_use` reads one; "
    "`skill_draft` turns a workflow into a `SKILL.md` for `skill_set` or "
    "`skill_edit` to write; the catalogue rows a search finds are the same "
    "documents, so a skill can be found by what it is for without knowing its "
    "name."
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
        # Which entry supplies this skill is one question with one answer
        # (`skills.entry_for`), shared with `skill_use` and `skill_list`: read
        # three ways, a skill could be declared enabled with its requirements met
        # while `skill_use` refused it as switched off.
        found = skills.entry_for(skill, entries)
        supplied = dict(found.env) if found is not None else None
        note = skills.readiness(skill, supplied)
        # **The guard is around the read, not the check.**  `readiness` opens no
        # file (see `skill_list`), so an `OSError` can only come from `document`
        # — the one call here that touches one, and the only failure a row is
        # expected to report.  A `try` wide enough to cover `readiness` would
        # turn a failure nobody anticipated into a row that reads as unreadable.
        try:
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


#: What `list_sources` says this source is, for the hub and for `servers()`.
DESCRIPTION = (
    "The playbooks installed on this machine, one directory each under the "
    "data directory's `skills/`."
)


def build_server(
    config: Config, *, source: Callable[[], Config] | None = None
) -> FastMCP:
    """Build the skills server.

    **The entries are held and the tools change them in place**, for the reason
    the folder is re-read on every ask: a skill is installed by putting a
    directory in a folder, and what is *given* to one is an entry in the config —
    so neither half needs a restart, and the tool that changes either one is
    answering about a state the next search will already see.

    Held **per home** (`slife2.libs.plugin.family.Homes`): `skills:` is a section of
    the home's `slife2.yaml`, so two homes served by this process keep two
    mirrors and neither sees the other's `env`.  `source` is where a home's
    section comes from — `load_cached` in production, nothing when a test hands a
    config in.
    """

    def management(mcp: FastMCP, homes: Homes[Documents]) -> None:
        """Register this family's tools.

        **`skill_use` answers with the document itself, not a measured page.**
        A playbook is a procedure to follow, and a result cut at
        `tool_result.tokens` would stop in the middle of a step.  Its size is the
        installed skill's — the operator's decision — so the full document is
        the answer; one too large for a conversation is the harness's dump path
        like any other oversized result, not a shorter answer invented here.
        """
        register_tools(mcp, homes)

    return build_document_server(
        config,
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        catalogue_source=SOURCE,
        category=CATEGORY,
        section=SECTION,
        description=DESCRIPTION,
        entries=lambda theirs: theirs.skills,
        source=source,
        rows=catalogue,
        management=management,
    )


def register_tools(mcp: FastMCP, homes: Homes[Documents]) -> None:
    """The `skill_*` tools — and `skill_use`, which no other family has.

    **Every body resolves the home the call is for** (`home_entries`), because
    what these read and write is one home's mirror of `skills:`.  A mirror bound
    once when this process started would be that home's, served to every other —
    which for a skill is the operator's `env`, the credentials a playbook is run
    with.
    """

    def home_entries() -> dict[str, SkillSettings]:
        """This home's `skills:`, as the mirror holds it."""
        return homes.current().entries

    def supplied_for(name: str) -> dict[str, dict[str, str]]:
        """The `skills:` section as `skill_use` wants it — name → `env`."""
        return {one: dict(settings.env) for one, settings in home_entries().items()}

    @mcp.tool(name=skills.USE_TOOL, meta=FOR_THE_MODEL)
    async def skill_use(name: str) -> str:
        """Read a skill: a playbook to follow rather than a tool to call.

        Read the one for a job before starting it. The answer is the skill's
        whole `SKILL.md`, preceded by the directory its paths are relative to.
        It is the document itself rather than JSON, and it is not shortened
        here — a playbook cut in the middle would stop in the middle of a step.

        Args:
            name: The skill's name, e.g. `browser-harness`.

        Returns:
            The skill's whole `SKILL.md` as text, preceded by the directory the
            paths in it are relative to and a note saying whether what it
            requires is at hand.
        """
        # Found here to read the switch off it: a skill may be filed under either
        # its own name or its directory's, and only the folder knows which, so
        # this is the same both-names lookup `catalogue` makes.
        skill = skills.find(name.strip())
        if skill is not None:
            found = skills.entry_for(skill, home_entries())
            if found is not None and not found.enabled:
                raise ToolError(
                    f"{name!r} is switched off, so it cannot be read — its "
                    f"`skills:` entry says `enabled: false`, which is the "
                    f"operator's decision and not a fault. `skill_list` reports "
                    f"the state of every installed skill."
                )
        text, ok = await skills.use(name, environments=supplied_for(name))
        if not ok:
            # **Raised, so the call is a failure the caller can see.**  `ok`
            # false is this system's word for a refusal — `slife2.clients.gateway`
            # distinguishes a peer's refusal from a transport fault by exactly
            # this flag, `slife2.plugins.toolhub.server` reads a raised `ToolError` as a value
            # with `ok` false, and the tool panel renders `ok` false as a failed
            # call.  A refusal *returned* as ordinary text would arrive with
            # `ok` true and reach the transcript as a success.  It is still a
            # value and not a crash: `slife2.plugins.toolhub.server` reads a peer's refusal as
            # the model's answer rather than as a broken server.
            raise ToolError(text)
        return text

    @mcp.tool(name="skill_draft", meta=FOR_THE_MODEL)
    async def skill_draft(
        name: str,
        description: str,
        steps: list[str],
        notes: list[str] | None = None,
        requires: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Draft a skill from a workflow: turn a procedure you just followed into a playbook.

        Nothing is written. The answer is the `files` argument `skill_set` or
        `skill_edit` takes, so saving a workflow is: `skill_draft`, read the
        document it returns, then `skill_set` if the skill is new or
        `skill_edit` if it is already installed. Use it when a job is one you
        will have to do again; do not save a one-off. The steps are what worked,
        in order — commands exactly as they ran — and `notes` is what was easy
        to get wrong.

        Args:
            name: The skill's name — what `skill_use` takes.
            description: One line saying what job the skill is for. This is
                what a search finds it by and what `skill_list` shows, so name
                the task, not the technology.
            steps: The procedure, in order. Each string is one step; a command
                belongs in the step it was run in.
            notes: What is easy to get wrong, or how to tell it worked. Omit
                for a procedure that has nothing to add.
            requires: What the steps need before they work, as `env` names and
                `bins` programs — e.g. `{"env": ["API_KEY"], "bins": ["curl"]}`.
                It is declared in the header, so `skill_use` reports a missing
                value before the steps run.

        Returns:
            `name`, and `files` — the install's argument, whose `SKILL.md` is
            the drafted document. Pass both to `skill_set` or `skill_edit`.
        """
        try:
            return skills.draft(
                name,
                description,
                steps,
                notes=notes or (),
                requires=requires,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    # ── The management tools ─────────────────────────────────────────────
    # The `skill_*` set, and the half SDD §14.2 named as next: a skill is a
    # directory, so installing one is writing files — which is the only thing in
    # this system that writes anywhere but the config, and the only one whose
    # paths a model chooses.  `skill_draft` is the authoring half and writes
    # nothing; `skill_set` and `skill_edit` are the two writes, whole and part.

    def _settings(name: str, skill: skills.Skill | None) -> SkillSettings | None:
        """The `skills:` entry for a skill, under either of its two names."""
        if skill is not None:
            found = skills.entry_for(skill, home_entries())
            if found is not None:
                return found
        return home_entries().get(name)

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
            if candidate and candidate in home_entries():
                return candidate
        return skill.name if skill else name

    @mcp.tool(name="skill_list", meta=FOR_THE_MODEL)
    async def skill_list(limit: int = 0, offset: int = 0) -> dict[str, Any]:
        """List the playbooks installed on this machine, with their state.

        This is where the name `skill_use` takes comes from: each row says what
        the playbook is for and whether what it requires is at hand — a key, a
        program.

        Args:
            limit: How many skills this page may hold. Omit for the cap.
            offset: How many skills to skip — that is how you page.

        Returns:
            `skills` — one row per skill, each with `name`, `state` (`on` or
            `off`) and, when it has them, `description` and `requirements` —
            and `total`, `limit` and `offset`, so
            `offset + len(skills) < total` says whether more remain.
        """
        rows: list[dict[str, Any]] = []
        for skill in skills.scan():
            settings = _settings(skill.name, skill)
            row: dict[str, Any] = {
                "name": skill.name,
                "state": "off"
                if settings is not None and not settings.enabled
                else "on",
            }
            if skill.description:
                row["description"] = skill.description
            # **No `except OSError` here.**  `readiness` opens nothing: it reads
            # `os.environ`, the skill's `skills:` entry and `shutil.which`, so an
            # `OSError` arm could only fire for a failure nobody anticipated —
            # and it turned that into a note line (SDD Appendix C).  The guard
            # belongs where the read is, `skills.document` in `catalogue` below.
            note = skills.readiness(
                skill, dict(settings.env) if settings is not None else None
            )
            if note:
                row["requirements"] = note
            rows.append(row)
        return listed(rows, "skills", limit, offset, cap=LIST_PAGE)

    @mcp.tool(name="skill_set", meta=FOR_THE_MODEL)
    async def skill_set(
        name: str,
        files: list[dict[str, str]],
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Install or replace a skill: write a playbook onto this machine.

        `SKILL.md` is the playbook and has to be one of the paths; anything else
        you write lands beside it. Write the procedure for one kind of job — the
        commands, the order, the steps that are easy to get wrong — with paths
        relative to the skill's directory. A skill of the same name is replaced
        in one step; to change part of an installed skill without replacing the
        rest, use `skill_edit`.

        `env` is the `skills:` entry the playbook is given: the names its
        scripts read, each a `${VAR}` or `keyring:` reference or a literal
        value. Omit it on a reinstall to keep the operator's entry; pass `{}` to
        clear it. A switch already on the entry is kept either way.

        Args:
            name: The skill's name — what `skill_use` takes.
            files: The files to write, each `{"path": "SKILL.md", "content":
                "..."}`. Paths are relative to the skill's own directory, and
                one that leaves it is refused.
            env: What the playbook's scripts are given. A `${VAR}` resolves
                through the environment and the credential store when the
                playbook is used; a `keyring:` reference is resolved here, so a
                credential that is not stored fails this call.

        Returns:
            `name`, `state` (`on`, or `off` when the entry was already switched
            off and this call kept it that way), and `files`, how many were
            written. A refused install raises instead, so a success means the
            folder is there.
        """
        # **Refused by raising, like `skill_use`** — see that tool for why `ok`
        # false and not a `[refused]` sentence: a refusal is a failure the caller
        # can see.  The connection families raise too (`toolfamily.prepare_entry`
        # serves `mcp_set` and `rest_api_set`), so this is one contract and not
        # two.
        if not name.strip():
            raise ToolError("a skill needs a name")
        # **Resolved before anything changes.**  A `keyring:` reference with no
        # credential behind it is a config the next start would refuse, and
        # resolving it here is what makes that this call's failure rather than
        # one discovered later.  A `${VAR}` that is not set remains the
        # reference it was written as, and `skill_use` reports it missing.
        try:
            supplied = (
                {str(key): resolve_secret(value) for key, value in env.items()}
                if env is not None
                else None
            )
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        try:
            installed = skills.install(name.strip(), files)
        except ValueError as exc:
            raise ToolError(f"{name!r} was not installed: {exc}") from exc
        except OSError as exc:
            raise ToolError(f"could not write {name!r}: {exc}") from exc
        if env is not None:
            _write_env(name.strip(), env, supplied or {})
        key = _key_for(name.strip(), skills.find(name.strip()), home_entries())
        current = home_entries().get(key)
        logger.info("skill_set name=%s dir=%s", name, installed)
        return {
            "name": name,
            "state": "off" if current is not None and not current.enabled else "on",
            "files": len(files),
        }

    def _write_env(
        name: str, env: Mapping[str, str], supplied: Mapping[str, str]
    ) -> None:
        """Write the `skills:` entry's `env`, keeping the switch, and mirror it.

        **Read-modify-write, not a whole-entry `upsert`**, for `cli_set`'s
        reason: the entry has a field this call does not own (`enabled`, which
        `skill_set_enabled` writes), and replacing the entry would silently
        switch a skill the operator had taken out of the way back on.  The write
        goes through `configfile.update_entry`, so it is the same lock and the
        same loader validation as every other config edit.

        **The mirror is updated from what was written**, not from the argument:
        a concurrent edit may have added a switch, and the value `skill_use`
        reads next must be the one the file now holds.  `supplied` is the
        resolved form of `env` — what a reload would have produced — because the
        mirror is read by `skill_use` and `skill_list` without another load.
        """
        key = _key_for(name, skills.find(name), home_entries())
        written: dict[str, Any] = {}

        def change(current: dict[str, Any]) -> dict[str, Any] | None:
            updated = dict(current)
            if env:
                updated["env"] = dict(env)
            else:
                updated.pop("env", None)
            written.update(updated)
            return updated or None

        try:
            configfile.update_entry(SECTION, key, change)
        except ConfigError as exc:
            # The skill is installed; the entry is not, and the file is as it
            # was.  Saying both is what lets a caller retry the half that
            # failed without wondering about the half that worked.
            raise ToolError(
                f"{name!r} is installed, but its `skills:` entry was not written: {exc}"
            ) from exc
        mirror = homes.current()
        if written:
            mirror.entries[key] = SkillSettings(
                env=dict(supplied), enabled=written.get("enabled") is not False
            )
        else:
            mirror.forgotten(key)

    @mcp.tool(name="skill_edit", meta=FOR_THE_MODEL)
    async def skill_edit(name: str, files: list[dict[str, str]]) -> dict[str, Any]:
        """Change part of an installed skill, keeping every file you do not name.

        Use this to fix a step, tighten a description or add a script to a skill
        that is already installed. Read it first with `skill_use`, change what
        needs changing, and write only the files you changed: anything you leave
        out stays as it is — unlike `skill_set`, which replaces the whole skill.
        The `skills:` entry's `env` and switch are not touched; those are
        `skill_set` and `skill_set_enabled`.

        Args:
            name: The skill to change — the name `skill_list` shows.
            files: The files to write, each `{"path": "SKILL.md", "content":
                "..."}`. Paths are relative to the skill's own directory, and
                one that leaves it is refused. `SKILL.md` is optional.

        Returns:
            `name` and `files`, how many were written. A refused edit raises
            instead, and the installed skill is left as it was.
        """
        if not name.strip():
            raise ToolError("a skill needs a name")
        try:
            changed = skills.edit(name.strip(), files)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except OSError as exc:
            raise ToolError(f"could not write {name!r}: {exc}") from exc
        logger.info("skill_edit name=%s dir=%s files=%d", name, changed, len(files))
        return {"name": name, "files": len(files)}

    @mcp.tool(name="skill_remove", meta=FOR_THE_MODEL)
    async def skill_remove(name: str) -> dict[str, Any]:
        """Uninstall a skill: delete its files and its `skills:` entry.

        Its `env` and switch go with it, so nothing is left behind that
        describes a playbook nobody can read. A name with no skill is refused:
        nothing was deleted, so the call failed.

        Args:
            name: The skill to remove.

        Returns:
            `name` and `removed`, which a success makes true.
        """
        skill = skills.find(name.strip())
        if skill is None:
            raise ToolError(f"'{name}' is not installed — see `skill_list`.")
        key = _key_for(name.strip(), skill, home_entries())
        # **The entry first.**  It is the half a lock or an unreadable file can
        # refuse, and a call that failed there must leave the folder for the
        # next attempt; the folder is gone one step later and a retry of *it*
        # has nothing to find.
        try:
            configfile.remove(SECTION, key)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        homes.current().forgotten(key)
        try:
            # **The directory, not the name the caller used.**  `find` prefers
            # the frontmatter `name:`, which need not equal the folder's; the
            # raw name then removed nothing and left the entry already deleted.
            removed = skills.remove(skill.directory.name)
        except ValueError as exc:
            raise ToolError(f"{name!r} was not removed: {exc}") from exc
        if not removed:
            # `find` said it was there; a folder that vanished between the two
            # reads is the same refusal the old single check made.
            raise ToolError(f"'{name}' is not installed — see `skill_list`.")
        logger.info("skill_removed name=%s", name)
        return {"name": name, "removed": True}

    @mcp.tool(name="skill_set_enabled", meta=FOR_THE_MODEL)
    async def skill_set_enabled(name: str, enabled: bool) -> dict[str, Any]:
        """Switch a skill on or off, keeping it installed.

        Off keeps the files, stops it being findable, and `skill_use` refuses
        it.

        Args:
            name: The skill.
            enabled: True offers it again; False keeps it out of the way.

        Returns:
            `name`, `enabled`, and `state` — `on` or `off` — the same word a
            row of `skill_list` carries.
        """
        skill = skills.find(name.strip())
        if skill is None and name.strip() not in home_entries():
            raise ToolError(f"'{name}' is not installed — see `skill_list`.")
        key = _key_for(name.strip(), skill, home_entries())
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
        # A skill with no entry at all gets one: the switch has to live
        # somewhere, and the entry it lives in is what `skill_use` reads to know
        # what the playbook is given.  A skill that *is* held keeps the rest of
        # its entry — that is the operator's `env`, and this is a switch.
        homes.current().switched(
            key, enabled, make=lambda: SkillSettings(enabled=enabled)
        )
        logger.info("skill_set_enabled name=%s key=%s enabled=%s", name, key, enabled)
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
