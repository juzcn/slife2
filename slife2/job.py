"""What a job imports: the agent's capabilities, without a list of them here.

A **job** is a Python function in the home's `jobs/` folder.  The function's
name, docstring and typed signature become a tool (`job__<name>`) the model can
call; running it is ordinary code execution with exactly the arguments the
caller passed.  A job can be a small new tool (slugify, parse, convert), a
one-shot model call, a call into the hub's catalogue, or a multi-step
orchestration of all three — *the code is the workflow*, and the same mechanism
covers both.

**Nothing here enumerates the capabilities.**  A capability is a server the
config already runs (`PLUGINS` / `servers:`) or a tool the hub's catalogue
already holds; a new plugin or a new tool is therefore usable from a job the
moment it exists, with no runtime change.  There are two ways to reach one:

* :func:`call` — one tool on **one of slife2's servers**, by the server's key and
  the tool's own name.  This is the full surface: `send_message` on `agent`,
  `stream_chat` on a model backend, any plugin's own tools.  Nothing checks that
  the pair is "a capability"; the server answers whether it has the tool.
* :func:`call_tool` — the **hub's** catalogue, by the name the model calls
  (`turn_read`, `mcp__serper__search`, another `job__…`).  It is a convenience
  over the hub's `call_tool`, and it is the path for every external MCP/REST
  tool.

:func:`servers` names the plugin keys that exist right now, read from the
config's one table; a job that wants to choose at run time rather than at
authoring time asks it.  `tool_search` is how the model finds a hub tool by
what it does; the authoring skill is where those names are documented.

**The handles are explicit.**  Nothing here injects a system prompt, a
conversation history or an agent loop into a job: the job's code builds every
message and every argument itself, which is what makes the control flow
deterministic even when a model call inside it is not.  The provider's
credential stays in the backend that speaks its wire format; a job never holds a
key.

**A handle only works while its job is running.**  The plugin binds a
:class:`RunContext` around the call, and the handles read it; a call made at
import time, or from a task the job left behind, has no run and says so.  The
context is a `ContextVar`, so two jobs running at once — in two conversations —
do not see each other's conversation or home.

The job code is ordinary Python and may import anything the plugin process can
import.  The handles are the supported seam: they preserve credential isolation,
forward the caller's `(agent, subagent)` identity, and keep the result contract
the rest of the system uses.  Code that reaches around them is the author's
choice, with the author's consequences.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.clients.toolclient import CALL_TOOL
from slife2.libs.host.config import Config, ConfigError, plugin
from slife2.libs.plugin.audience import client_meta
from slife2.libs.plugin.protocol import close_server, open_server, tool_payload

#: The hub's key in `servers:`.  One system fact, not a capability: every job's
#: model-facing tool path goes through the same process, and the convenience
#: below would be spelling the key at every call site otherwise.
_HUB = "toolhub"


@dataclass(frozen=True)
class ToolResult:
    """What one tool in the hub's catalogue answered.

    `ok` is the hub's verdict, not a judgement about the code: `False` means the
    tool ran and refused, and `text` is the sentence it refused with.  A system
    failure — the hub itself unreachable — raises instead, because a job that
    cannot ask the question is not a job that got an answer.
    """

    ok: bool
    text: str


@dataclass(frozen=True)
class CapabilityResult:
    """What one call to one of our servers answered.

    `ok` is whether the server ran the tool and returned an answer; a tool that
    refused raises on the wire and arrives here as `ok` false with its sentence
    in `text`.  `data` is the mapping the tool returned — the whole of it — so a
    job can read keys this module has never heard of (`tool_calls`, `usage`,
    `iterations`), which is the point of not enumerating capabilities here.

    A tool that answers in prose rather than a mapping has `data` empty and its
    text in `text`; `server` and `tool` are echoed so one log line says which
    pair produced the result.
    """

    server: str
    tool: str
    ok: bool
    text: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunContext:
    """One job run: the config its handles resolve through, and its identity.

    The plugin builds this and binds it for the length of the call.  `run_id` is
    unique per run; it is available to the job's code only through the plugin's
    logs, so a job that wants a conversation of its own names one.  The
    important part is `agent`/`subagent`: every call the handles make carries
    that identity, so tools that read a conversation read the right one.
    """

    config: Config
    agent: str
    subagent: str
    run_id: str


_current: ContextVar[RunContext | None] = ContextVar("slife2_job_run", default=None)


@contextmanager
def running(context: RunContext):
    """Bind *context* to the job running in this task; the plugin's half."""
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)


def _require() -> RunContext:
    context = _current.get()
    if context is None:
        raise RuntimeError(
            "a job's handles only work while the job is running — import "
            "`call`, `call_tool` and `servers` from `slife2.job` and call them "
            "inside the job function"
        )
    return context


async def _peer(url: str, name: str, fallback: str, invoke: Any) -> Any:
    """Connect to one of our servers, run *invoke*, and let go of the client.

    One client per call, closed in `finally`: the protocol is stateless here, so
    holding a session between calls buys nothing and goes stale when a plugin is
    restarted on the same port.  The connect is bounded by `work.hub`; the call
    itself is not — a job's length is the caller's `_timeout`, and a callee does
    not invent a total for its caller.
    """
    client = await open_server(
        url, name=name, fallback_tool=fallback, timeout=_T.timeouts.work.hub
    )
    try:
        return await invoke(client)
    finally:
        await close_server(client)


def _answer(result: Any) -> tuple[str, Mapping[str, Any]]:
    """One tool result as `(text, data)`.

    The structured mapping first — that is what our servers return — and the
    content blocks second, for a tool that answers in prose (`skill_use` is the
    shape).  Neither is invented when both are empty: `("", {})` is an honest
    answer and the caller can say so.
    """
    data = tool_payload(result)
    if isinstance(data, dict) and data:
        text = data.get("text")
        return (text if isinstance(text, str) else ""), data
    blocks = getattr(result, "content", None) or ()
    parts = [
        text
        for block in blocks
        if isinstance(text := getattr(block, "text", None), str)
    ]
    return "\n".join(parts), {}


async def call(
    server: str, tool: str, arguments: Mapping[str, Any] | None = None
) -> CapabilityResult:
    """Call one tool on one of slife2's servers, by server key and tool name.

    **The server key is the one in `servers:`** — `agent`, `openai-completions`,
    `cabinet`, `media`, and every other row of `PLUGINS`.  The tool is that
    server's own name (`send_message`, `stream_chat`, `generate_image`), not a
    hub name; this bypasses the hub because the model is not the caller.  The
    hub's catalogue is :func:`call_tool`'s job, and external MCP/REST tools are
    reached there.

    The call carries the job's `(agent, subagent)` identity.  A tool that
    refuses is a value (`ok` false, its sentence in `text`); a server that
    cannot be reached raises, because no question was asked.

    Args:
        server: The server's key in `servers:` (`agent`, a provider's `api`,
            `toolhub`, `cabinet`, …).
        tool: The tool's name on that server, as its own `tools/list` spells it.
        arguments: Its arguments, as that tool's schema describes them.

    Returns:
        `server`, `tool`, `ok`, `text` and `data` — the whole mapping the tool
        answered.  `data` is how a job reads keys this module does not know.
    """
    context = _require()
    try:
        spec = plugin(server)
    except KeyError:
        raise RuntimeError(
            f"{server!r} is not one of slife2's servers. Its key is a row of "
            f"`servers:`; external tools are called through `call_tool` and the "
            f"hub's catalogue. `servers()` lists what exists."
        ) from None
    try:
        url = context.config.server(server).url
    except ConfigError as exc:
        raise RuntimeError(f"{server!r} has no address in this config: {exc}") from exc

    async def invoke(client: Any) -> Any:
        return await client.call_tool(
            tool,
            dict(arguments or {}),
            meta=client_meta(context.agent, context.subagent),
        )

    try:
        result = await _peer(url, spec.name, spec.probe, invoke)
    except ToolError as exc:
        return CapabilityResult(
            server=server, tool=tool, ok=False, text=str(exc), data={}
        )
    text, data = _answer(result)
    return CapabilityResult(server=server, tool=tool, ok=True, text=text, data=data)


async def call_tool(
    name: str, arguments: Mapping[str, Any] | None = None
) -> ToolResult:
    """Call one tool in the hub's catalogue, by the name the model calls it.

    This is the composed surface: everything the model may use — a plugin's own
    tool (`turn_read`, `note_search`), an external `{server}__{tool}`, another
    `job__<name>` — and the hub route is the same one a model's call takes.  Use
    it for tools; use :func:`call` for a server's internal surface.

    Args:
        name: The tool's advertised name, exactly as `tool_search` reports it.
        arguments: Its arguments, as the tool's own schema describes them.

    Returns:
        `ok` — the hub's verdict — and `text`.  A tool that refused is a value;
        a hub that could not be reached raises, because no question was asked.
    """
    result = await call(
        _HUB, CALL_TOOL, {"name": name, "arguments": dict(arguments or {})}
    )
    data = result.data
    if result.ok and ("ok" not in data or "text" not in data):
        raise RuntimeError(
            "the toolhub answered without `ok`/`text` — a build this job cannot read"
        )
    if not result.ok:
        return ToolResult(ok=False, text=result.text)
    return ToolResult(ok=bool(data.get("ok")), text=str(data.get("text") or ""))


def servers() -> dict[str, str]:
    """The servers this home runs right now: key → the MCP name it advertises.

    **Read from the config's one table**, not from a list kept here: a plugin
    another change adds appears in this answer without this module changing,
    which is the whole reason a job's capabilities are not enumerated.  Use it
    when a job wants to choose a server at run time; use `call_tool` for the
    hub's catalogue.
    """
    context = _require()
    return {key: plugin(key).name for key in context.config.plugins()}


__all__ = [
    "CapabilityResult",
    "ToolResult",
    "call",
    "call_tool",
    "servers",
]
