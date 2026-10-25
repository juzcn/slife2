"""The TUI probe: an agent task's delivered answer, not a turn's assistant text.

The agent-task server and the agent server are faked here; what is pinned is
the probe's contract with them — start a uniquely named task as the target
agent, wait for its state, and read the message whose marker names that task.
"""

from __future__ import annotations

import json

import pytest

from slife2.clients import tui
from slife2.clients.a2a import ProbeError
from slife2.libs.host.config import default_config

pytestmark = pytest.mark.unit


class FakeTask:
    """The agent-task server: a task that starts running and then finishes."""

    def __init__(self, status: str = "finished") -> None:
        self.status = status
        self.name: str | None = None
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        if name == tui.START:
            self.name = arguments["name"]
            return {"task_id": f"agent-task:{self.name}", "status": "running"}
        if name == tui.STATUS:
            return {"task_id": f"agent-task:{self.name}", "status": self.status}
        raise AssertionError(name)


class FakeAgent:
    """The agent server: the delivered task message, and nothing else."""

    def __init__(self, task: FakeTask) -> None:
        self.task = task

    async def call_tool(self, name, arguments, **kwargs):
        assert name == tui.TRANSCRIPT
        marker = tui.MARKER_PREFIX + json.dumps(
            {"task_id": f"agent-task:{self.task.name}", "name": self.task.name}
        )
        return {
            "turns": [
                {
                    "turn_id": 1,
                    "messages": [{"role": "user", "content": f"{marker}] the answer"}],
                }
            ]
        }


def install(monkeypatch, task: FakeTask) -> FakeAgent:
    agent = FakeAgent(task)
    config = default_config()

    async def fake_open(url, *, fallback_tool="", timeout=None):
        if url == config.server("agent-task").url:
            return task
        if url == config.server("agent").url:
            return agent
        raise AssertionError(url)

    async def fake_close(client):
        return None

    monkeypatch.setattr(tui, "open_server", fake_open)
    monkeypatch.setattr(tui, "close_server", fake_close)
    monkeypatch.setattr(tui, "tool_payload", lambda result: result)
    monkeypatch.setattr(tui, "client_meta", lambda agent: {"agent": agent})
    return agent


@pytest.mark.asyncio
async def test_the_probe_returns_the_delivered_task_answer(monkeypatch) -> None:
    task = FakeTask()
    install(monkeypatch, task)

    result = await tui.probe_tui(default_config(), "tb-1", "do it", timeout=5)

    assert result.text == "the answer"
    assert result.task_id == f"agent-task:{task.name}"
    assert task.calls[0][0] == tui.START
    assert task.calls[0][1] == {"name": task.name, "description": "do it"}
    assert task.calls[1][0] == tui.STATUS


@pytest.mark.asyncio
async def test_a_failed_task_is_a_probe_failure(monkeypatch) -> None:
    task = FakeTask(status="failed")
    install(monkeypatch, task)

    with pytest.raises(ProbeError, match="failed"):
        await tui.probe_tui(default_config(), "tb-1", "do it", timeout=5)


def test_the_marker_is_matched_by_its_task_id() -> None:
    turns = [
        {
            "messages": [
                {
                    "content": (
                        tui.MARKER_PREFIX
                        + '{"task_id": "agent-task:probe-1", "name": "probe-1"}]'
                        " the answer"
                    )
                }
            ]
        }
    ]

    assert tui._answer(turns, "agent-task:probe-1") == "the answer"
    assert tui._answer(turns, "agent-task:another") is None
