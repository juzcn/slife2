"""Jobs: the code files, and what one file becomes.

A job is one module-level **public function** in a `.py` file under the home's
`jobs/` folder.  The file's stem is the job's name, the function must have that
name, and its docstring and typed signature become the tool's description and
parameter schema — ordinary MCP tool conventions, with no manifest to keep in
step.  A leading underscore makes a helper: it is module-level, and it is not a
job.

**A broken file is kept, not skipped.**  The folder says a job is installed, so
hiding it would send a model looking for a tool the operator can see; the file
is loaded far enough to report what went wrong, the row says `error`, and
`job_use`/`job_set` are how it is read and fixed.  That is the same choice
`slife2.plugins.skills.playbooks` makes for a damaged `SKILL.md`, and for the
same reason.

**Re-import is repeatable.**  The module is evicted from `sys.modules` before
each load and compiled from source, so a rewritten file is always re-read — a
bytecode cache keyed on the same mtime would otherwise serve the old code.
"""

from __future__ import annotations

import inspect
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp.tools import FunctionTool

#: The module-name namespace job files are loaded under.  Private, so a job file
#: called `re.py` cannot shadow the standard library one.
_MODULE_PREFIX = "_slife2_job_"


class JobLoadError(Exception):
    """A job file could not be imported, or does not define its job."""


@dataclass(frozen=True)
class Job:
    """One job file, loaded or broken.

    `tool` is the FastMCP function tool — the schema and the body — and is
    `None` exactly when `error` says why the file did not become one.  `path`
    is the live file in both states, because a broken job is still a file a
    person or the model can open.
    """

    name: str
    path: Path
    description: str = ""
    schema: str = ""
    tool: Any = None
    error: str = ""

    @property
    def ok(self) -> bool:
        """Whether this job can be called at all."""
        return not self.error and self.tool is not None


def load_module(path: Path, name: str) -> types.ModuleType:
    """Import one job file under a private module name.

    Compiled and `exec`'d rather than imported through a spec loader, for the
    reason the docstring states: every load must read the file as it is now.
    """
    module_name = f"{_MODULE_PREFIX}{name}"
    sys.modules.pop(module_name, None)
    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[module_name] = module
    try:
        source = path.read_text(encoding="utf-8-sig")
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)  # noqa: S102 — job files are code, by design
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise JobLoadError(
            f"importing {path.name} failed: {type(exc).__name__}: {exc}"
        ) from exc
    return module


def load_job(path: Path, name: str = "") -> Job:
    """Load one file as the job *name*, or return it with `error` set.

    The function must be module-level, public, named exactly like the job, and
    carry a docstring: a tool with no description is one `tool_search` cannot
    find, so the authoring gate asks for the line the model will read.
    """
    job_name = name or path.stem
    try:
        module = load_module(path, job_name)
        function = getattr(module, job_name, None)
        if not inspect.isfunction(function):
            raise JobLoadError(
                f"{path.name} must define a module-level function named {job_name!r}"
            )
        if getattr(function, "__module__", "") != module.__name__:
            raise JobLoadError(
                f"{path.name}: {job_name!r} is imported, not defined here"
            )
        description = inspect.getdoc(function) or ""
        if not description.strip():
            raise JobLoadError(
                f"{path.name}: {job_name!r} needs a docstring — it is the tool "
                f"description a search finds the job by"
            )
        tool = FunctionTool.from_function(function, name=job_name)
        schema = json.dumps(tool.to_mcp_tool().input_schema, ensure_ascii=False)
        return Job(
            name=job_name,
            path=path,
            description=description,
            schema=schema,
            tool=tool,
        )
    except Exception as exc:  # noqa: BLE001 — a broken job is a value, never a crash
        return Job(
            name=job_name,
            path=path,
            error=f"{type(exc).__name__}: {exc}"
            if not isinstance(exc, JobLoadError)
            else str(exc),
        )


def scan_jobs_dir(root: Path) -> dict[str, Job]:
    """Every `.py` file in *root*, as jobs, sorted by name.

    A missing folder is an empty mapping rather than an error: a fresh home has
    no jobs, and that is the honest answer to which ones are installed.  A
    broken file is included with `error` set — see the module docstring.
    """
    if not root.is_dir():
        return {}
    jobs: dict[str, Job] = {}
    for path in sorted(root.glob("*.py")):
        if path.name.startswith(("_", ".")):
            continue
        job = load_job(path)
        jobs[job.name] = job
    return jobs


def result_text(result: Any) -> str:
    """The text FastMCP would have shown a caller for one tool result.

    A job returns whatever Python it likes; FastMCP converts that to content
    blocks here, and the tool result the model reads is those blocks' text.  The
    structured form is the fallback for a return the SDK kept only there.
    """
    blocks = getattr(result, "content", None) or ()
    parts = [
        text
        for block in blocks
        if isinstance(text := getattr(block, "text", None), str)
    ]
    if parts:
        return "\n".join(parts)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    return ""


__all__ = [
    "Job",
    "JobLoadError",
    "load_job",
    "load_module",
    "result_text",
    "scan_jobs_dir",
]
