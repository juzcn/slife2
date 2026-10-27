---
name: job-coding
description: Create code-defined tools — deterministic Jobs, each exposed as its own MCP tool (job__<name>). Use when a task is well-specified, repeatable and data-in/result-out, or when a multi-step procedure should stop being a prompt and become code. A job may be a small new tool or a workflow that composes tools, models and agents. Load this skill before writing a job.
---

# job-coding — how to write a Job

A **Job** is a deterministic, code-defined unit of work. Its control flow is
Python, not prompts. It is also a **new tool the model can call**: one function
in `<data>/jobs/<name>.py` becomes the tool `job__<name>`, with the docstring as
its description and the typed signature as its parameter schema.

The same mechanism covers both cases:

- a **new tool** — `slugify`, `parse_csv`, `convert_document`;
- a **workflow** — a procedure that calls tools, models and agents in an order
  you wrote down, with loops, branches and error handling.

The difference is only what the code does. There is no separate "workflow"
format.

## When to write a Job

| The task is… | Use |
|---|---|
| Stable, reusable, data-in/result-out — a fixed interface | **Job** |
| A multi-step procedure whose steps are known and should not be re-decided each run | **Job** (its code is the workflow) |
| Open-ended, exploratory — the agent must judge as it goes | A **skill** (a playbook it reads), or do it inline |
| Nothing you will reuse | Do it inline |

A Job is the answer to "do this the same way every time". If the value is the
model's judgement at each step, write a skill instead.

## Where jobs live

- `<data>/jobs/<name>.py` — one file, one job.
- The file stem is the job's name; the file must define a **module-level public
  function with that same name**. `_`-prefixed functions are private helpers.
- The exposed tool is `job__<name>`. The `job__` prefix is fixed and keeps a
  job from displacing a system tool (`job__turn_read` is not `turn_read`).
- A file that does not load is kept as an `error` row: `job_use` shows the code,
  `job_set` replaces it. It is never silently hidden.

## Lifecycle

| Tool | Purpose |
|---|---|
| `job_list` | What is installed: `name`, the `tool` (`job__<name>`), description, status, file. |
| `job_use` | Read a job's code and path. This is what you edit. |
| `job_set` | Write a job — create or replace — and register its tool now. A broken write is refused and the previous version restored. |
| `job_remove` | Delete the file and remove the tool. |
| `job_run` | Run a job by name with one JSON arguments object; the test path before its tool is loaded. |

`job_set` is live: the hub re-reads the jobs' declaration before its next
search, so the new `job__<name>` is findable without a restart. The same is
true of `job_remove`.

## Grammar

- **Use `async def` when the job calls a handle**, and `await` every call. A
  pure-computation job can be plain `def`.
- **The docstring is the tool description.** Give it a line that says what the
  job is for, and an `Args:` block for each parameter. This is what
  `tool_search` ranks and what the model reads when choosing the tool.
- **Parameter types must be JSON-friendly**: `str`, `int`, `float`, `bool`,
  `list[...]`, `dict[...]`. No `*args` / `**kwargs`, no `datetime`/`Path`/custom
  objects, no defaults that are not simple literals — the tool schema is derived
  from the signature.
- **Return the result. Do not `print()` it** — a printed value is invisible to
  the caller. A `str` is the clearest return; a `dict`/`list` is rendered as
  JSON text.
- **One public function per file**, named exactly like the file. Helpers are
  `_`-prefixed.

## The handles — capabilities are discovered, not listed

```python
from slife2.job import call, call_tool, servers
```

**Nothing in `slife2.job` enumerates capabilities.** There are two generic
doors; every capability the home has is behind one of them.

### `call_tool(name, arguments)` — the hub's catalogue

This is the composed surface: every tool the model may use, by the name
`tool_search` reports.

- a plugin's own tool: `turn_read`, `note_search`, `generate_image`;
- an external tool: `serper__search`, `arxiv__search_papers`;
- another job: `job__slugify`.

It returns `ToolResult(ok, text)`. A tool that refused is `ok=False` with its
sentence; the hub being unreachable raises. Branch on `ok` when a refusal is a
normal outcome.

### `call(server, tool, arguments)` — one of slife2's servers, by key

This is the full surface, including the tools the model does not call itself.
`server` is the key in `servers:`; `tool` is that server's own tool name.

- **An agent turn** — the complete loop, with its context and tools:
  `call("agent", "send_message", {"agent": "jack", "prompt": ..., "subagent": "job-<something>"})`.
  The result's `data` is the whole `send_message` payload; `text` is the answer.
  **Never send to the conversation that is running the job** — that turn is
  waiting on you, and it deadlocks. Always pass a distinct `subagent`; a job's
  own run id is a good suffix.
- **A raw model call** — one completion, no loop:
  `call("openai-completions", "stream_chat", {"provider": "<provider key>", "model": "<api model>", "messages": [...], "tools": [...]})`.
  The provider key is the `providers:` key whose `api` is that server; the API
  model id is the model's `model:` field. Find them with `model_list` or the
  config at authoring time; a provider's credential stays in its backend.
- **Anything else a plugin serves** — `cabinet`, `media`, `schedule`,
  `agent-task`, and every future plugin: the same `call`, with its key and its
  tool name. A new plugin is usable the moment it exists.

`call` returns `CapabilityResult(server, tool, ok, text, data)`. `data` is the
whole mapping, so the job reads keys `slife2.job` has never heard of
(`tool_calls`, `usage`, `iterations`) — that is what makes the surface dynamic.

### `servers()`

The keys this home runs right now, read from the config's one table. Use it when
a job chooses a server at run time; use `tool_search` when you (the author) are
choosing one now.

### Authoring-time discovery

Never write a tool name or schema from memory:

- `tool_search(sentences=[...], keywords=[...])` finds hub tools by what they do;
  the result includes the schema.
- `mcp_list` / `mcp_list_tools` probe an external server's real tools and
  schemas.
- `model_list` lists providers and model refs.
- `servers()` (in the job, or the config at authoring time) names the plugin
  keys.

Freeze the exact names and argument shapes you observed into the job.

## Examples

### A new deterministic tool

```python
import re

def slugify(title: str, sep: str = "-") -> str:
    """Convert a title into a URL slug.

    Args:
        title: The title to slugify.
        sep: Separator between words.
    """
    return sep.join(re.findall(r"[a-z0-9]+", title.lower()))
```

### A one-shot model tool

```python
from slife2.job import call

async def translate(text: str, lang: str = "zh") -> str:
    """Translate text into a target language.

    Args:
        text: Source text.
        lang: Target language code.
    """
    result = await call(
        "openai-completions",
        "stream_chat",
        {
            "provider": "deepseek",
            "model": "deepseek-flash",
            "messages": [
                {"role": "system", "content": "Output only the translation."},
                {"role": "user", "content": f"Translate into {lang}:\n{text}"},
            ],
        },
    )
    return result.text
```

### A workflow: search → agent → save

```python
from slife2.job import call, call_tool

async def daily_report(topic: str) -> str:
    """Search a topic, summarize it and save the report.

    Args:
        topic: What to research.
    """
    found = await call_tool("serper__search", {"q": topic})
    if not found.ok:
        return f"Search failed: {found.text}"

    answer = await call(
        "agent",
        "send_message",
        {"agent": "jack", "prompt": f"Summarize these results:\n{found.text}",
         "subagent": "job-daily-report"},
    )
    saved = await call_tool(
        "report_save", {"title": topic, "body": answer.text}
    )
    return answer.text if saved.ok else f"Saved? {saved.ok}: {saved.text}"
```

The Python is the workflow: loops, `if`, `try`, `asyncio.gather`, helper
functions. The handles are the leaves.

## Determinism and trust

- **Control flow is deterministic; model and tool outputs are not.** A job makes
  them deterministic where it can: validate, assert, retry, fall back to a clear
  error.
- The handles carry the caller's `(agent, subagent)` identity, so a tool that
  reads a conversation sees the one the job is running for.
- A handle only works **while the job is running**; a call at import time, or
  from a task the job leaves behind, says so.
- The code runs in the jobs plugin and may import anything that plugin can
  import. Keep it reviewable, validate input, and return clear errors. The
  top-level tool call is where a person can be asked to approve the run
  (`_approve`); the job's inner calls do not ask again.

## Fast-fail rules

- File stem and function name match; the function is public and module-level.
- A docstring is required — it is the tool's description.
- Jobs that call a handle are `async def` and `await` it.
- Parameters and defaults are JSON-serialisable; no `*args` / `**kwargs`.
- Return a value; never rely on `print`.
- One public function per file; helpers start with `_`.
- Do not send an agent turn to the conversation running the job; give it its own
  `subagent`.
- Reserved names (`job_list`, `job_set`, `job_remove`, `job_run`, `job_use`) and
  names beginning with `_` are refused.
