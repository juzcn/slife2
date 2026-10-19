"""Generate `TOOLS.md` from the tool catalogue and the plugin sources.

The catalogue (`agents/tools.db`) is the truth about which tools exist, what
their wire schemas are and what the model is told about them.  It does not
carry what a tool *returns*, so that half comes from the docstring — its
`Returns:` section where there is one, and an explicit note below where the
implementation is the only statement of it.

The result is a reference for developers rather than the model: one entry per
plugin tool, grouped by the server that serves it, with the source line that
registers it.

    python scripts/gen_tools_doc.py
    python scripts/gen_tools_doc.py --check

`--check` writes nothing and exits nonzero when `TOOLS.md` is out of date.
The catalogue is built when slife2 first runs in a home; a fresh checkout has
none until then, so `--db` can name another one.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sqlite3
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "agents" / "tools.db"
DEFAULT_OUT = ROOT / "TOOLS.md"

#: The order the document lists them in, with the server's own file.
GROUPS = [
    (
        "agent",
        "Agent",
        "The conversation loop, model configuration, images and subagents.",
        "slife2/plugins/agent/server.py",
    ),
    (
        "agent-task",
        "Agent tasks",
        "Background loops started by the model.",
        "slife2/plugins/agent_task/server.py",
    ),
    (
        "cabinet",
        "Cabinet",
        "Notes, diary, files, reports and tool dumps.",
        "slife2/plugins/cabinet/server.py",
    ),
    (
        "cli-server",
        "CLI catalogue",
        "Commands installed on this machine.",
        "slife2/plugins/cli/server.py",
    ),
    (
        "context",
        "History",
        "The agent's own stored turns.",
        "slife2/plugins/context/server.py",
    ),
    (
        "file-tunnel",
        "File tunnel",
        "Public URLs for local files.",
        "slife2/plugins/file_tunnel/server.py",
    ),
    (
        "mcp-tools",
        "MCP configuration",
        "Other people's MCP servers, as configured entries.",
        "slife2/plugins/mcp_tools/server.py",
    ),
    (
        "media",
        "Media",
        "Image, video, speech and transcription models.",
        "slife2/plugins/media/server.py",
    ),
    (
        "peer_agent",
        "Peer agent network",
        "The remote A2A mesh.",
        "slife2/plugins/a2a/server.py",
    ),
    (
        "restapi-tools",
        "REST configuration",
        "OpenAPI documents exposed as tools.",
        "slife2/plugins/restapi/server.py",
    ),
    (
        "schedule",
        "Schedule",
        "Cron tasks and their ledger.",
        "slife2/plugins/schedule/server.py",
    ),
    (
        "skills-server",
        "Skills",
        "Playbooks under `skills/`.",
        "slife2/plugins/skills/server.py",
    ),
    (
        "subagent",
        "Subagent network",
        "The local network of an agent's subagents.",
        "slife2/plugins/a2a/server.py",
    ),
    (
        "toolhub",
        "Tool catalogue",
        "Searching, loading and unloading tools.",
        "slife2/plugins/toolhub/server.py",
    ),
    (
        "wechat",
        "WeChat",
        "Login and messaging through WeChat.",
        "slife2/plugins/wechat/server.py",
    ),
]

#: Tools whose answers the docstrings do not state.
RETURNS = {
    "_check_new_input": (
        "A string: the first message waiting for this conversation, verbatim, "
        'or `"Nothing waiting: no message has arrived since the last boundary."`'
    ),
    "agent_task_start": (
        "`task_id`, `name`, `status` (`running`), `started_at` and `finished_at`."
    ),
    "agent_task_status": (
        "`task_id`, `name`, `status` (`running`, `finished`, `failed`, "
        "`cancelled` or `unknown`), `started_at` and `finished_at`. Never the "
        "task's result."
    ),
    "agent_task_list": (
        "`tasks` — one row per task, running first, each `task_id`, `name`, "
        "`status`, `started_at`, `finished_at` and a shortened `description`."
    ),
    "agent_task_cancel": (
        "`task_id`, `name`, `status` after the call (`cancelled`, `finished`, "
        "`failed` or `unknown`), `started_at` and `finished_at`."
    ),
    "turn_read": (
        "The whole turn: `turn_id`, `messages`, `summary`, `tags`, "
        "`created_at`, `completed_at`, `channel`, `who_helped`, `what_model`, "
        "`token_count` and `context_tokens`."
    ),
    "peer_agent_send_message": (
        "`peer` (the member addressed), `task_id`, `message_type` and `note`; a "
        "`task_response` answers `peer`, `task_id` and `completed`."
    ),
    "peer_agent_cancel_task": (
        "`peer`, `task_id` and `status` — `cancelled`, `completed`, `failed` or "
        "`not_found`."
    ),
    "peer_agent_list": (
        "`agent_name`, `status` (your own), `broker`, `org`, `unit`, `agents` — "
        "cards of `{agent_name, status}` — and `error`, empty when nothing is "
        "wrong."
    ),
    "peer_agent_task_list": (
        "`agent_name`, `tasks` — rows of `task_id`, `member`, `direction` "
        "(`sent` or `received`), `status` (`pending` or `unreachable`) and "
        "`since` — and `error`."
    ),
    "peer_agent_broadcast": "`broadcast` (the event sent) and `note`.",
    "schedule_set": (
        "The task row: `name`, `description`, `cron`, `timezone`, `enabled`, "
        "`created_at`, `last_due` and `next_run`."
    ),
    "schedule_remove": "`name` and `removed` (always `true`).",
    "schedule_run_now": (
        "The run's row: `name`, `due_at`, `status`, `ran_at`, `note`, `error` "
        "and `agent_task_id`."
    ),
    "schedule_run_skip": (
        "The run's row: `name`, `due_at`, `status` (`skipped`), `ran_at`, "
        "`note`, `error` and `agent_task_id`."
    ),
    "tool_search": (
        "`results` — rows of `name`, `category`, `source`, `status`, `loaded`, "
        "`matched`, `meaning` and `description` — plus `best` and `confidence`."
        "\n\n"
        "- `meaning` — the row's cosine similarity to the closest thing asked; "
        "higher is closer. Every row on the page has one, including rows the "
        "keyword leg found; `null` only when the row has no vector.\n"
        "- `matched` — what found the row: `words` when every keyword you gave "
        "is in the fields the result prints (name, description, category, "
        "source), `parameters` when the match is only in a tool's parameter "
        "schema, `body` for the same in a playbook's text, and empty when only "
        "the meaning leg returned it.\n"
        "- `best` — the largest `meaning` on the page (`0.0` when no row has "
        "one). `confidence` is derived from it and from the word matches: "
        "`sure`, `weak` or `none`."
    ),
    "func_tool_load": (
        "`loaded` — the names now in the model's list — and `failed` — rows of "
        "`name` and `reason` for each name that could not be loaded."
    ),
}

#: The subagent network serves the same five A2A tools under its own names.
SUBAGENT_SAME = {
    "subagent_send_message": "peer_agent_send_message",
    "subagent_cancel_task": "peer_agent_cancel_task",
    "subagent_list": "peer_agent_list",
    "subagent_task_list": "peer_agent_task_list",
    "subagent_broadcast": "peer_agent_broadcast",
}

#: Tools registered from description constants rather than a docstring.
SOURCE_OVERRIDES = {
    "tool_search": {"file": "slife2/plugins/toolhub/server.py", "line": 1866},
    "func_tool_load": {"file": "slife2/plugins/toolhub/server.py", "line": 1876},
}

#: Parameters the docstring and the schema both leave unexplained.
PARAM_NOTES: dict[str, dict[str, str]] = {
    "agent_task_start": {
        "name": "The handle the task is asked about and stopped by.",
        "description": "What the task should do — the whole of what it is given.",
    },
    "agent_task_status": {"name": "The task's handle."},
    "agent_task_cancel": {"name": "The task's handle."},
    "peer_agent_send_message": {
        "agent": "The member's name, as the network's list tool gives it.",
        "message": "The message, or the result for a `task_response`.",
        "message_type": "`task_request` (default), `task_response` or `message`.",
        "task_id": "The task being answered, for `task_response`.",
    },
    "peer_agent_cancel_task": {
        "agent": "The member the task was sent to.",
        "task_id": "The task to withdraw.",
    },
    "peer_agent_broadcast": {"event": "The event to publish."},
    "schedule_set": {
        "name": "The task's name, used by every other schedule tool.",
        "description": "What to do when it comes due, and what done looks like.",
        "cron": "Five fields: `minute hour day-of-month month day-of-week`.",
        "timezone": (
            "IANA name, e.g. `Asia/Shanghai`; empty follows this machine's clock."
        ),
        "enabled": "`false` keeps the task written down and never fires it.",
    },
    "schedule_remove": {"name": "The scheduled task's name."},
    "schedule_run_now": {
        "name": "The scheduled task's name.",
        "due_at": "The moment the run was due; empty means now.",
    },
    "schedule_run_skip": {
        "name": "The scheduled task's name.",
        "due_at": "The run's due moment; empty means the newest open run.",
        "note": "Why it is being skipped, recorded on the run.",
    },
    "schedule_list": {
        "limit": "How many tasks this page may hold (at most 200; default 50).",
        "offset": "How many tasks to skip.",
    },
    "schedule_runs": {
        "name": "Narrow to one scheduled task.",
        "status": "One run state: `pending`, `ran`, `failed`, `missed` or `skipped`.",
        "limit": "How many runs this page may hold (at most 200; default 20).",
        "offset": "How many runs to skip.",
    },
}


def ast_tools(root: Path) -> dict[str, dict]:
    """Every `@mcp.tool` function under `root/slife2/plugins`, by name."""
    found: dict[str, dict] = {}
    for path in sorted((root / "slife2" / "plugins").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if not (
                    isinstance(target, ast.Attribute)
                    and target.attr == "tool"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "mcp"
                ):
                    continue
                found.setdefault(
                    node.name,
                    {
                        "file": path.relative_to(root).as_posix(),
                        "line": node.lineno,
                        "doc": ast.get_docstring(node) or "",
                    },
                )
    return found


def returns_of(name: str, source: dict | None) -> str:
    """The answer a tool gives, from its note or its `Returns:` section."""
    if name in RETURNS:
        return RETURNS[name]
    if source is None:
        return "—"
    match = re.search(r"\n\s*Returns:\s*\n", source.get("doc") or "")
    if not match:
        return "—"
    return textwrap.dedent(source["doc"][match.end() :]).strip()


def args_of(source: dict | None) -> dict[str, str]:
    """The `Args:` notes from a docstring, by parameter name."""
    if source is None:
        return {}
    doc = source.get("doc") or ""
    match = re.search(r"\n\s*Args:\s*\n", doc)
    if not match:
        return {}
    found: dict[str, str] = {}
    current = ""
    for line in textwrap.dedent(doc[match.end() :]).splitlines():
        stripped = line.strip()
        if stripped in ("Returns:", "Raises:", "Yields:", "Examples:", "Note:"):
            break
        if not stripped:
            current = ""
        elif line[:1].isspace():
            if current:
                found[current] = f"{found[current]} {stripped}".strip()
        else:
            name, separator, rest = line.partition(":")
            current = name.strip() if separator else ""
            if current:
                found[current] = rest.strip()
    return found


def description_of(description: str) -> str:
    """The model-facing description without a trailing `Returns` block."""
    return re.split(r"\n\s*Returns:\s*\n", description.strip())[0].strip()


def type_of(prop: dict) -> str:
    """A JSON Schema type as a person reads it."""
    if "anyOf" in prop:
        parts: list[str] = []
        for one in prop["anyOf"]:
            rendered = type_of(one)
            if rendered not in parts:
                parts.append(rendered)
        return " or ".join(parts)
    kind = prop.get("type", "any")
    if kind == "array":
        return f"array<{type_of(prop.get('items', {}))}>"
    return kind


def default_of(prop: dict) -> str:
    if "default" not in prop:
        return "—"
    return f"`{json.dumps(prop['default'], ensure_ascii=False)}`"


def cell(text: str) -> str:
    """One table cell: newlines flattened and pipes escaped."""
    return " ".join(str(text).split()).replace("|", "\\|")


def params_table(schema: dict, notes: dict[str, str]) -> str:
    props = schema.get("properties") or {}
    if not props:
        return "No parameters."
    required = set(schema.get("required") or [])
    lines = [
        "| Parameter | Type | Required | Default | Description |",
        "|---|---|---|---|---|",
    ]
    for name, prop in props.items():
        lines.append(
            f"| `{name}` | `{type_of(prop)}` | "
            f"{'yes' if name in required else 'no'} | "
            f"{default_of(prop)} | "
            f"{cell(prop.get('description') or notes.get(name, ''))} |"
        )
    return "\n".join(lines)


def build_document(root: Path, db: Path) -> str:
    """The whole of `TOOLS.md`, as the text to write."""
    tools = ast_tools(root)
    connection = sqlite3.connect(db)
    try:
        rows = list(
            connection.execute(
                "select name, description, source_id, schema from tool "
                "where category = 'plugin' order by source_id, name"
            )
        )
    finally:
        connection.close()

    by_group: dict[str, list] = {}
    for name, description, source_id, schema in rows:
        by_group.setdefault(source_id, []).append((name, description, schema))

    out: list[str] = [
        "# Tools",
        "",
        textwrap.fill(
            "slife2's own tools — the `plugin` category of the tool catalogue. "
            "External `mcp:`, `rest-api:`, `cli:` and `skills:` entries are not "
            "listed.",
            width=80,
        ),
        "",
        textwrap.fill(
            "Arguments are the wire schemas the hub advertises. A tool's answer "
            "is not part of that schema, so every entry says what it returns. "
            f"Generated from `agents/tools.db` ({len(rows)} tools) by "
            "`scripts/gen_tools_doc.py`.",
            width=80,
        ),
        "",
        "## Contents",
        "",
    ]
    for key, title, _, _ in GROUPS:
        out.append(
            f"- [{title}](#{title.lower().replace(' ', '-')}) "
            f"({len(by_group.get(key, []))})"
        )
    out.append("")

    for key, title, blurb, path in GROUPS:
        entries = by_group.get(key, [])
        if not entries:
            continue
        out.extend(
            [
                f"## {title}",
                "",
                blurb,
                "",
                f"Source: [`{path}`]({path})",
                "",
            ]
        )
        for name, description, schema in entries:
            lookup = SUBAGENT_SAME.get(name, name)
            source = tools.get(lookup) or SOURCE_OVERRIDES.get(lookup)
            if source is None and lookup.startswith("_"):
                source = tools.get(lookup[1:])
            out.append(f"### `{name}`")
            out.append("")
            if source is not None:
                out.append(
                    f"[`{source['file']}:{source['line']}`]"
                    f"({source['file']}#L{source['line']})"
                )
                out.append("")
            out.extend(
                [
                    description_of(description),
                    "",
                    "**Parameters**",
                    "",
                    params_table(
                        json.loads(schema),
                        args_of(source)
                        | PARAM_NOTES.get(SUBAGENT_SAME.get(name, name), {}),
                    ),
                    "",
                    "**Returns**",
                    "",
                    returns_of(SUBAGENT_SAME.get(name, name), source),
                    "",
                ]
            )
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"the tool catalogue (default: {DEFAULT_DB.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"the document to write (default: {DEFAULT_OUT.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 when the document is out of date",
    )
    args = parser.parse_args(argv)

    if not args.db.is_file():
        print(
            f"no tool catalogue at {args.db} — run slife2 once to build it, "
            "or pass --db",
            file=sys.stderr,
        )
        return 2

    document = build_document(ROOT, args.db)
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.is_file() else ""
        if current == document:
            print(f"{args.out} is up to date")
            return 0
        print(f"{args.out} is out of date; run scripts/gen_tools_doc.py", file=sys.stderr)
        return 1

    args.out.write_text(document, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
