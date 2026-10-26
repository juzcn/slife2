"""The TUI probe: one agent task, and the message it delivers.

**A window's turn has no answer of its own.**  A `submit` hands a message to the
window and returns; the turn's assistant text answers everything folded into
that turn, so no assistant text can be attributed to one probe.  The probe asks
through an *agent task* instead: the task runs one loop of the agent's own (no
inbox, no cut-ins), and its stop message is delivered to the conversation as an
`agent-task:<name>` message wearing `[AgentTaskResult:{…}]`.  That named message
is the answer.

The task's opening template (`agent_task.j2`) already says nobody is waiting,
nobody can answer a question, and the message sent when the task stops is the
whole of what comes back — so the probe adds no prompt of its own.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from slife2.clients.a2a import ProbeError, ProbeResult
from slife2.helpers import timeouts as _T
from slife2.libs.host.config import Config
from slife2.libs.plugin.audience import client_meta
from slife2.libs.plugin.protocol import close_server, open_server, tool_payload
from slife2.libs.store.db import message_text

#: The agent-task plugin's tool names and marker prefix, spelled because they
#: cross a process boundary: `plugins/agent_task/server.py` registers the tools
#: and writes the marker.  The agent server's `transcript` is the read.
START = "agent_task_start"
STATUS = "agent_task_status"
TRANSCRIPT = "transcript"
MARKER_PREFIX = "[AgentTaskResult:"

#: The states in which a task has stopped without an answer to read.
_ENDED = frozenset({"failed", "cancelled"})


def _answer(turns: list[dict[str, Any]], task_id: str) -> str | None:
    """The answer *task_id* delivered, or None while it has not landed.

    The marker's JSON is the task's report; the answer follows the closing
    bracket.  Matching the report's `task_id` is what keeps this read from
    returning another task's message — the reason the agent task is the TUI
    probe's road at all.
    """
    decoder = json.JSONDecoder()
    for turn in turns:
        for message in turn.get("messages") or []:
            text = message_text(message.get("content"))
            if not text.startswith(MARKER_PREFIX):
                continue
            try:
                report, end = decoder.raw_decode(text[len(MARKER_PREFIX) :])
            except ValueError:
                continue
            if not isinstance(report, dict) or report.get("task_id") != task_id:
                continue
            rest = text[len(MARKER_PREFIX) + end :]
            if rest.startswith("]"):
                rest = rest[1:]
            return rest.lstrip(" ")
    return None


def _unreadable_marker(turns: list[dict[str, Any]]) -> bool:
    """Whether a result marker was delivered that this build cannot read.

    **A marker that will not parse is not a marker that is absent.**  `_answer`
    skips a malformed one, because it may belong to another task or to a newer
    build; when no answer is found, this is what tells the probe's "not
    delivered" verdict apart from "delivered, and unreadable".
    """
    decoder = json.JSONDecoder()
    for turn in turns:
        for message in turn.get("messages") or []:
            text = message_text(message.get("content"))
            if not text.startswith(MARKER_PREFIX):
                continue
            try:
                decoder.raw_decode(text[len(MARKER_PREFIX) :])
            except ValueError:
                return True
    return False


async def probe_tui(
    config: Config,
    agent: str,
    prompt: str,
    *,
    timeout: float | None = None,
) -> ProbeResult:
    """Ask *agent* one task through the agent-task server, and return its answer.

    The task is started as *agent* (`client_meta`), so it runs on that agent's
    prompt, model, tools and context.  Its stop message is delivered to the
    conversation by the agent-task plugin; this reads the delivered message,
    never the turn's assistant text.

    Raises:
        ProbeError: the task server or agent server is unreachable, the task
            ends failed/cancelled, or no delivered answer appears within
            *timeout*.
    """
    if timeout is None:
        timeout = _T.timeouts.deliver.probe
    name = f"probe-{uuid.uuid4().hex[:12]}"
    task_id = f"agent-task:{name}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    meta = client_meta(agent)
    task_server = None
    agent_server = None
    try:
        try:
            task_server = await open_server(
                config.server("agent-task").url,
                fallback_tool=START,
                timeout=_T.timeouts.transport.window,
            )
            tool_payload(
                await task_server.call_tool(
                    START, {"name": name, "description": prompt}, meta=meta
                )
            )
        except Exception as exc:  # noqa: BLE001 - the start is the whole gate
            raise ProbeError(f"the agent task could not be started: {exc}") from exc

        while True:
            try:
                report = tool_payload(
                    await task_server.call_tool(STATUS, {"name": name}, meta=meta)
                )
            except Exception as exc:  # noqa: BLE001 - the task's state is the gate
                raise ProbeError(f"the task's state could not be read: {exc}") from exc
            status = str(report.get("status") or "unknown")
            if status != "running":
                break
            if loop.time() >= deadline:
                raise ProbeError(
                    f"the agent task {name!r} did not stop within {timeout:.0f}s"
                )
            await asyncio.sleep(_T.timeouts.pacing.probe_poll)

        if status in _ENDED or status == "unknown":
            raise ProbeError(f"the agent task {name!r} ended {status}")

        try:
            agent_server = await open_server(
                config.server("agent").url,
                fallback_tool=TRANSCRIPT,
                timeout=_T.timeouts.transport.window,
            )
        except Exception as exc:  # noqa: BLE001 - the read is the whole gate
            raise ProbeError(f"the agent server is not reachable: {exc}") from exc

        while True:
            try:
                payload = tool_payload(
                    await agent_server.call_tool(
                        TRANSCRIPT, {"agent": agent, "subagent": ""}, meta=meta
                    )
                )
            except Exception as exc:  # noqa: BLE001 - the read is the whole gate
                raise ProbeError(f"the transcript could not be read: {exc}") from exc
            turns = payload.get("turns")
            if not isinstance(turns, list):
                raise ProbeError("the agent server answered with no turn list")
            text = _answer(turns, task_id)
            if text is not None:
                return ProbeResult(text=text, task_id=task_id)
            if loop.time() >= deadline:
                if _unreadable_marker(turns):
                    raise ProbeError(
                        f"the agent task {name!r} finished, but a result marker "
                        f"in {agent!r}'s transcript could not be read"
                    )
                raise ProbeError(
                    f"the agent task {name!r} finished, but its answer was not "
                    f"delivered to {agent!r} within {timeout:.0f}s "
                    f"(is a window attached?)"
                )
            await asyncio.sleep(_T.timeouts.pacing.probe_poll)
    finally:
        if agent_server is not None:
            await close_server(agent_server)
        if task_server is not None:
            await close_server(task_server)
