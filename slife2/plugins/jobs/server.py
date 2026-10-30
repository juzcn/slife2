"""slife2-jobs — the `jobs/` folder, and the tools the model wrote in it.

A **job** is one Python function in a file under the home's `jobs/` folder.  The
file's stem is the job's name, the function must have that name, and its
docstring and typed signature become the tool's description and parameter
schema.  Each job is exposed as `job__<name>` — a real tool in the model's
catalogue, routed by the hub exactly like a tool from an external server.

**The folder is the truth, and a write is live.**  `job_set` writes a file,
loads it and registers its row in the same call; the hub re-reads `list_sources`
before every search, so the tool is findable from the model's next search with
no restart.  `job_remove` deletes the file and the row.  A broken file stays
visible as an `error` row — `job_read` shows the code, `job_set` replaces it —
rather than disappearing and leaving the model to guess.

**A job may be a new tool or a workflow.**  The mechanism does not distinguish:
a pure function is a new deterministic tool, and a function that calls the
hub's tools, a model or an agent in a loop is a workflow.  The three handles in
`slife2.job` are the seam, and the code is free Python around them.

This process owns no connection and no credential.  It loads the code, binds a
run context, and the handles make the calls through the servers that do hold
the credentials — the same isolation every other plugin keeps.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

import slife2.job as runtime
from slife2.libs.host.config import (
    JOB_SERVER_NAME,
    Config,
    load_cached,
)
from slife2.libs.host.paths import jobs_dir
from slife2.libs.plugin.audience import FOR_THE_MODEL, request_client
from slife2.libs.plugin.family import LIST_PAGE, Homes, listed
from slife2.libs.plugin.protocol import (
    CALL_SOURCE,
    JOB_SOURCE,
    LIST_SOURCES,
    close_names,
    house_server,
    serve_plugin,
    unknown_name,
)
from slife2.plugins.jobs import registry as jobs

logger = logging.getLogger(__name__)

SERVER_NAME = JOB_SERVER_NAME

#: This server's key in the config's `servers:` table, and so the name the
#: launcher starts it under and the hub connects to it by.
CONFIG_KEY = "job-server"

#: The catalogue source the job rows are filed under.  Its rows are callable —
#: a job is a function category — and the hub names them `job__<name>`.
SOURCE = JOB_SOURCE

#: The category, and the prefix the model's tool name carries.  The prefix is
#: not decoration: the jobs' names are a user's Python identifiers, and a
#: `job__turn_read` must never be confused with the system's own `turn_read`.
CATEGORY = "job"

#: What `list_sources` says this source is, for the hub and for `servers()`.
DESCRIPTION = (
    "Code-defined jobs: one Python function per file under the home's `jobs/` "
    "folder, each exposed to the model as `job__<name>`."
)

INSTRUCTIONS = (
    "The code-defined jobs in this home's `jobs/` folder, and the `job_*` tools "
    "that write, read and remove one. A job is a Python function; its docstring "
    "and typed signature are its tool definition, and the model calls it as "
    "`job__<name>`. A job may be a small deterministic tool or a workflow that "
    "calls other tools, a model or an agent through the handles it imports from "
    "`slife2.job`."
)

#: The names this plugin owns.  The exposed form is prefixed (`job__list`), so
#: a job called `list` would not actually collide with `job_list`; the bare
#: names are reserved anyway, because two tools one underscore apart are a
#: mistake waiting to be made.
_RESERVED = frozenset(
    {
        "job_list",
        "job_set",
        "job_remove",
        "job_run",
        "job_read",
        LIST_SOURCES,
        CALL_SOURCE,
    }
)

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_name(name: str) -> str | None:
    """Why *name* cannot be a job, or `None` when it can."""
    if not _NAME_RE.fullmatch(name):
        return (
            "a job name must be a Python identifier — letters, digits and "
            "underscores, not starting with a digit — because it is the "
            "function's name and the tool's name after it"
        )
    if name.startswith("_"):
        return "a job name may not start with `_`; that form is a private helper"
    if name in _RESERVED:
        return f"{name!r} is a tool this plugin owns; choose another job name"
    return None


def _exposed(name: str) -> str:
    """The name the model calls a job by (`slugify` -> `job__slugify`)."""
    return f"{CATEGORY}__{name}"


def _bare(name: str) -> str:
    """The job name behind an exposed one; *name* unchanged when it is bare."""
    return name.removeprefix(f"{CATEGORY}__")


def _unknown_job(name: str, known: Sequence[str]) -> str:
    """A missing job: a misspelling and an absence lead to different steps.

    A typo is corrected from the suggestion; a job that is not there is one to
    create — or one that was removed — and `job_list` says which names exist.
    One sentence for both would send the reader back to search.
    """
    return unknown_name(
        close_names(name, known),
        missing=f"no job called {name!r}",
        finder="`job_set` creates one; `job_list` shows what is installed",
        absent="installed",
    )


def _not_loaded(job: jobs.Job) -> str:
    """A broken job, and the way out — never the exception alone.

    **This failure is self-healable**, and the sentence says so: the file is
    still on disk, `job_read` reads it, and `job_set` replaces it.  A message
    that stopped at the exception would make a person search for whether the
    job is worth rescuing; the next step is the answer to that question.
    """
    return (
        f"job {job.name!r} is not loaded: {job.error} — read it with "
        f"`job_read`, fix the code and call `job_set` again"
    )


class Registry:
    """One home's jobs: the live functions, and the rows the hub reads.

    Held per home (`Homes`), because the folder is the home's: two homes served
    by one process keep two sets of jobs, and a `job_set` in one cannot shadow a
    name in the other.
    """

    def __init__(self, config: Config, root: Path) -> None:
        self.config = config
        self.root = root
        self._jobs = jobs.scan_jobs_dir(root)

    def find(self, name: str) -> jobs.Job | None:
        """One job, by its bare name or the exposed `job__<name>`."""
        return self._jobs.get(_bare(name.strip()))

    def put(self, job: jobs.Job) -> None:
        """Replace one entry — the registry is keyed by the job's name."""
        self._jobs[job.name] = job

    def forget(self, name: str) -> None:
        self._jobs.pop(name, None)

    def names(self) -> list[str]:
        """Every installed job's name, for a refusal that says what is there."""
        return sorted(self._jobs)

    def values(self) -> list[jobs.Job]:
        """Every job, sorted by name — the listing's order and one place it is."""
        return sorted(self._jobs.values(), key=lambda one: one.name)

    def rows(self) -> list[dict[str, Any]]:
        """Every job, as one catalogue row each.

        **A broken job keeps its row.**  Its `status` is `error` and its schema
        is empty, so it is findable and not callable; the alternative — hiding
        it — makes a file a person can see in the folder a tool nothing can
        find, which is the same mistake the skills folder refuses.
        """
        rows: list[dict[str, Any]] = []
        for job in self.values():
            if job.error:
                description = f"{job.name}: not loaded — {job.error}"
            else:
                description = job.description
            rows.append(
                {
                    # **The model-facing name is the row's name.**  A declared
                    # source names its own rows — the hub merges them as they
                    # are — so the `job__` prefix has to be here, not left to a
                    # naming rule the hub applies to its own connections.  The
                    # hub routes a call back by `remote_name`, which is the bare
                    # function name `call_source` receives.
                    "name": _exposed(job.name),
                    "description": description,
                    "remote_name": job.name,
                    "schema": job.schema if job.ok else "",
                    "status": "error" if job.error else "enabled",
                }
            )
        return rows

    def declare(self) -> dict[str, Any]:
        """This plugin's whole `list_sources` answer — one source, always up."""
        return {
            "sources": [
                {
                    "name": SOURCE,
                    "category": CATEGORY,
                    "enabled": True,
                    "up": True,
                    "description": DESCRIPTION,
                    "transport": "local",
                    "rows": self.rows(),
                }
            ]
        }

    async def run(
        self,
        job: jobs.Job,
        arguments: dict[str, Any] | None,
        *,
        agent: str = "",
        subagent: str = "",
    ) -> tuple[str, bool]:
        """Run one loaded job; `(text, ok)` and never raises for a job failure.

        The context is bound for the length of the call, so the handles see the
        conversation the job is running for and the config of this home.  A
        job's exception is a failed tool, not a failed plugin: the caller is the
        model, and the sentence it reads is how it learns what went wrong.
        """
        if not job.ok:
            return _not_loaded(job), False
        context = runtime.RunContext(
            config=self.config,
            agent=agent,
            subagent=subagent,
            run_id=uuid.uuid4().hex,
        )
        with runtime.running(context):
            try:
                result = await job.tool.run(dict(arguments or {}))
            except Exception as exc:  # noqa: BLE001 — a job failure is a result
                logger.warning("job_failed name=%s err=%s", job.name, exc)
                return f"{type(exc).__name__}: {exc}", False
        text = jobs.result_text(result)
        if getattr(result, "is_error", False):
            return text, False
        return text, True


def build_server(
    config: Config, *, source: Callable[[], Config] | None = None
) -> FastMCP:
    """Build the jobs server.

    **One registry per home**, built on the first ask (`Homes`): the folder a
    call is about is the caller's, and the jobs it holds are read once and then
    kept in step by the tools.  A file edited outside the plugin is picked up on
    the next start, which is the contract the authoring skill states.
    """
    homes: Homes[Registry] = Homes(
        lambda theirs: Registry(theirs, jobs_dir()),
        source,
        config,
    )

    mcp: FastMCP = house_server(SERVER_NAME, instructions=INSTRUCTIONS)

    @mcp.tool(name=LIST_SOURCES)
    def list_sources() -> dict[str, Any]:
        """Every job this plugin holds, as one source — the hub's read.

        Not a tool for the model: the hub asks for it before every search, which
        is what makes a `job_set` visible without a restart, and the model never
        sees the name.
        """
        return homes.current().declare()

    @mcp.tool(name=CALL_SOURCE)
    async def call_source(
        ctx: Context, source: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Run one job for the hub — the per-job tool's body.

        A refusal is `ok` false with the sentence: an unknown job, a broken
        file, and a job that raised are all things the model can read and act
        on, and the hub's call contract carries them without pretending the
        server broke.
        """
        if source != SOURCE:
            return {
                "text": f"{source!r} is not a source this plugin holds",
                "ok": False,
            }
        registry = homes.current()
        job = registry.find(tool)
        if job is None:
            return {"text": _unknown_job(tool, registry.names()), "ok": False}
        client = request_client(ctx) or ("", "")
        text, ok = await registry.run(
            job, arguments, agent=client[0], subagent=client[1]
        )
        return {"text": text, "ok": ok}

    # ── The management tools ─────────────────────────────────────────────

    def _home() -> Registry:
        return homes.current()

    def _refused(name: str) -> jobs.Job:
        registry = _home()
        job = registry.find(name)
        if job is None:
            raise ToolError(_unknown_job(name, registry.names()))
        return job

    @mcp.tool(name="job_list", meta=FOR_THE_MODEL)
    async def job_list(limit: int = 0, offset: int = 0) -> dict[str, Any]:
        """List the code-defined jobs installed in this home.

        Each row gives the bare `name`, the `tool` the model calls
        (`job__<name>`), what the job is for, and whether it loaded. A row with
        `status` `error` is a file that did not load: read it with `job_read`,
        fix it, and `job_set` it again.

        Args:
            limit: How many jobs this page may hold. Omit for the cap.
            offset: How many jobs to skip — that is how you page.

        Returns:
            `jobs` — one row per job: `name`, `tool`, `description`, `status`
            (`enabled` or `error`), and `error` when it did not load — plus
            `total`, `limit` and `offset`.
        """
        rows: list[dict[str, Any]] = []
        for job in _home().values():
            row: dict[str, Any] = {
                "name": job.name,
                "tool": _exposed(job.name),
                "description": job.description if job.ok else "",
                "status": "error" if job.error else "enabled",
            }
            if job.error:
                row["error"] = job.error
            rows.append(row)
        return listed(rows, "jobs", limit, offset, cap=LIST_PAGE)

    @mcp.tool(name="job_read", meta=FOR_THE_MODEL)
    async def job_read(name: str) -> dict[str, Any]:
        """Read a job's code: the function the tool runs.

        The answer is the code itself — the thing to copy from when changing it
        with `job_set`. A job that did not load says why, so the broken line is
        in front of you when you fix it.

        Args:
            name: The job's name, bare or as its `tool` row.

        Returns:
            `name`, `code` — the source as written — and `error`, which is
            empty for a job that loaded.
        """
        job = _refused(name)
        try:
            code = job.path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ToolError(
                f"could not read {job.path}: {exc} — close whatever holds the "
                f"file and try `job_read` again"
            ) from exc
        return {
            "name": job.name,
            "code": code,
            "error": job.error,
        }

    @mcp.tool(name="job_set", meta=FOR_THE_MODEL)
    async def job_set(name: str, code: str) -> dict[str, Any]:
        """Write a job — create or replace it — and register its tool now.

        Write the whole file, not a patch: one module-level function named like
        the job, a docstring that says what it is for, and a typed signature
        whose parameters are the tool's arguments. The code is ordinary Python;
        it may import the handles from `slife2.job` when it needs tools, a model
        or an agent.

        A write that does not load is refused and the previous version is put
        back, so the installed job is never a broken file. Replacing a job is
        the same call — keep the function name stable if you want the tool name
        and its arguments to stay the same.

        Args:
            name: The job's name and the file's stem; a Python identifier, not
                one of this plugin's own tool names.
            code: The job's complete Python source.

        Returns:
            `name`, the `tool` the model calls (`job__<name>`), and `status`.
        """
        name = name.strip()
        if not name:
            raise ToolError("a job needs a name")
        if why := _validate_name(name):
            raise ToolError(why)
        if not code.strip():
            raise ToolError("a job needs code")
        text = code if code.endswith("\n") else code + "\n"
        registry = _home()
        final = registry.root / f"{name}.py"
        previous = ""
        exists = final.exists()
        if exists:
            try:
                previous = final.read_text(encoding="utf-8")
            except OSError as exc:
                raise ToolError(
                    f"could not read {final}: {exc} — close whatever holds the "
                    f"file and try `job_set` again"
                ) from exc
        # **Written through a temporary file and then swapped in**, so a crash
        # or a full disk never leaves half a job where a whole one was.  The
        # load happens against the live name, which keeps the traceback and the
        # function's `__file__` honest; a failure restores what was there.
        staging = registry.root / f".{name}.installing"
        try:
            staging.write_text(text, encoding="utf-8")
            os.replace(staging, final)
        except OSError as exc:
            staging.unlink(missing_ok=True)
            raise ToolError(
                f"could not write {name!r}: {exc} — close whatever holds the "
                f"file (an editor, another slife2) and try `job_set` again"
            ) from exc
        loaded = jobs.load_job(final, name=name)
        if not loaded.ok:
            try:
                if exists:
                    staging.write_text(previous, encoding="utf-8")
                    os.replace(staging, final)
                else:
                    final.unlink(missing_ok=True)
            finally:
                staging.unlink(missing_ok=True)
            raise ToolError(
                f"{name!r} was not installed — {loaded.error}"
                + ("; the previous version was restored" if exists else "")
                + ". Fix the code and call `job_set` again"
            )
        registry.put(loaded)
        logger.info("job_set name=%s file=%s", name, final)
        return {"name": name, "tool": _exposed(name), "status": "enabled"}

    @mcp.tool(name="job_remove", meta=FOR_THE_MODEL)
    async def job_remove(name: str) -> dict[str, Any]:
        """Uninstall a job: delete its file and remove its tool.

        A name with no file is refused — nothing was removed, so the call
        failed. The tool's row goes with it, so the next search no longer
        offers it.

        Args:
            name: The job's name, bare or as its `tool` row.

        Returns:
            `name` and `removed`, which a success makes true.
        """
        job = _refused(name)
        try:
            job.path.unlink(missing_ok=True)
        except OSError as exc:
            raise ToolError(
                f"could not remove {job.path}: {exc} — close whatever holds the "
                f"file and try `job_remove` again"
            ) from exc
        _home().forget(job.name)
        logger.info("job_removed name=%s", job.name)
        return {"name": job.name, "removed": True}

    @mcp.tool(name="job_run", meta=FOR_THE_MODEL)
    async def job_run(
        ctx: Context,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a job by name — the test path before its tool is loaded.

        The per-job tool (`job__<name>`) is how a job is normally called: it
        has the job's own parameter schema, so the model passes args by name.
        This tool takes the arguments as one object and is for trying a job,
        including one the model has not loaded, while authoring it.

        Args:
            name: The job's name, bare or as its `tool` row.
            arguments: The job's arguments as one JSON object, keyed by the
                function's own parameter names.

        Returns:
            `name`, `ok` — whether the job completed — and `text`, its result or
            the sentence saying why it failed. An unknown or unloaded job is
            refused as a failed call, not returned as an `ok` false result.
        """
        job = _refused(name)
        if not job.ok:
            raise ToolError(_not_loaded(job))
        client = request_client(ctx) or ("", "")
        text, ok = await _home().run(
            job, arguments or {}, agent=client[0], subagent=client[1]
        )
        return {"name": job.name, "ok": ok, "text": text}

    return mcp


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
