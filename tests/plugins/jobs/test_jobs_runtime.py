"""The job runtime: the generic capability call, and the category the rows land in.

The handles are tested against a fake peer rather than a live stack: what they
must get right is the *call they make* — which server, under whose identity —
and a fake records that exactly.  That the runtime names no capability is the
point: the tests reach the agent and a model backend through the same `call`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import slife2.job as job
from slife2.libs.host.config import default_config
from slife2.libs.store.catalogue import CATEGORIES, FUNCTION_CATEGORIES, JOB
from slife2.plugins.jobs import registry
from slife2.plugins.toolhub.server import DECLARABLE_CATEGORIES
from slife2.plugins.toolhub.server import (
    JOB as HUB_JOB,
)

pytestmark = pytest.mark.unit


# ── the files ─────────────────────────────────────────────────────────


def write(tmp_path: Path, name: str, code: str) -> Path:
    path = tmp_path / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    return path


def test_a_job_file_becomes_a_function_tool(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "slugify",
        'def slugify(title: str, sep: str = "-") -> str:\n'
        '    """Make a slug."""\n'
        "    return title\n",
    )
    found = registry.load_job(path)

    assert found.ok
    assert found.name == "slugify"
    assert found.description == "Make a slug."
    assert '"title"' in found.schema
    assert found.tool is not None


def test_a_broken_file_is_a_job_with_an_error_not_an_exception(
    tmp_path: Path,
) -> None:
    path = write(tmp_path, "broken", "def broken(:\n")
    found = registry.load_job(path)

    assert not found.ok
    assert found.name == "broken"
    assert found.error and "broken" in found.error


def test_a_scan_keeps_broken_files_and_skips_private_ones(tmp_path: Path) -> None:
    write(tmp_path, "good", 'def good() -> str:\n    """Good."""\n    return "g"\n')
    write(tmp_path, "bad", "def bad(:\n")
    (tmp_path / "_helper.py").write_text("def helper(): pass\n", encoding="utf-8")
    (tmp_path / ".staging").write_text("nope", encoding="utf-8")

    found = registry.scan_jobs_dir(tmp_path)

    assert set(found) == {"good", "bad"}
    assert found["good"].ok and not found["bad"].ok


def test_an_imported_function_is_not_a_job(tmp_path: Path) -> None:
    """Only a function *defined in the file* is the job; an import that happens
    to carry the name is refused, because the file did not write it."""
    path = write(tmp_path, "imported", "from json import dumps as imported\n")
    found = registry.load_job(path)

    assert not found.ok
    assert "imported" in found.error


def test_result_text_reads_content_then_structured() -> None:
    block = SimpleNamespace(type="text", text="hello")
    assert registry.result_text(SimpleNamespace(content=[block])) == "hello"
    assert (
        registry.result_text(SimpleNamespace(content=[], structured_content={"a": 1}))
        == '{"a": 1}'
    )


# ── the category ──────────────────────────────────────────────────────


def test_a_job_is_a_function_category_the_hub_accepts() -> None:
    """The catalogue has to accept the category, and the hub has to let a plugin
    declare it.  Both are one fact each, and both are here so adding the family
    cannot half-land.  The model-facing name (`job__slugify`) is the *row's*,
    set by the jobs plugin; the hub merges a declared row under the name it was
    given."""
    assert JOB in FUNCTION_CATEGORIES
    assert JOB in CATEGORIES
    assert HUB_JOB in DECLARABLE_CATEGORIES


# ── the handles ───────────────────────────────────────────────────────


class FakeClient:
    """One peer: records every call and answers with a per-tool payload."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[tuple[str, dict[str, Any], Any]] = []
        self.opened: list[tuple[str, str]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, meta: Any = None
    ) -> Any:
        self.calls.append((name, arguments, meta))
        value = self.data[name]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(data=value)


@pytest.fixture
def peers(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient(
        {
            "call_tool": {"text": "hub answer", "ok": True},
            "send_message": {"text": "agent answer"},
            "stream_chat": {"text": "model answer", "tool_calls": []},
        }
    )

    async def open_server(
        url: str,
        *,
        name: str = "",
        fallback_tool: str = "",
        timeout: float | None = None,
    ) -> FakeClient:
        client.opened.append((url, name))
        return client

    async def close_server(_client: Any) -> None:
        return None

    monkeypatch.setattr(job, "open_server", open_server)
    monkeypatch.setattr(job, "close_server", close_server)
    return client


def context(**overrides: Any) -> job.RunContext:
    values = {
        "config": default_config(),
        "agent": "jack",
        "subagent": "",
        "run_id": "run1",
    }
    values.update(overrides)
    return job.RunContext(**values)


@pytest.mark.asyncio
async def test_call_tool_goes_through_the_hub_under_the_jobs_identity(
    peers: FakeClient,
) -> None:
    with job.running(context()):
        result = await job.call_tool("turn_read", {"turn": 3})

    assert result == job.ToolResult(ok=True, text="hub answer")
    ((name, arguments, meta),) = peers.calls
    assert name == "call_tool"
    assert arguments == {"name": "turn_read", "arguments": {"turn": 3}}
    assert meta["slife2/client"]["agent"] == "jack"
    assert peers.opened[0][1] == "slife2-toolhub"


@pytest.mark.asyncio
async def test_call_reaches_any_configured_server_by_its_key(
    peers: FakeClient,
) -> None:
    """**No capability is named in the runtime.**  The agent's turn and the
    model backend are reached by the same `call`, through their config keys; a
    plugin another change adds is reachable the same way."""
    with job.running(context()):
        agent = await job.call(
            "agent",
            "send_message",
            {"agent": "jack", "prompt": "summarize this", "subagent": "job-run1"},
        )
        model = await job.call(
            "openai-completions",
            "stream_chat",
            {
                "provider": "deepseek",
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert agent.ok and agent.text == "agent answer"
    assert agent.server == "agent" and agent.tool == "send_message"
    assert model.ok and model.text == "model answer"
    assert model.data["tool_calls"] == []
    assert [one[0] for one in peers.calls] == ["send_message", "stream_chat"]
    assert peers.opened[0][1] == "slife2-agent"
    assert peers.opened[1][1] == "slife2-llm-openai"


@pytest.mark.asyncio
async def test_a_tool_that_refuses_is_a_value_and_a_missing_server_is_not(
    peers: FakeClient,
) -> None:
    from fastmcp.exceptions import ToolError

    peers.data["send_message"] = ToolError("the agent said no")
    with job.running(context()):
        refused = await job.call("agent", "send_message", {})
        with pytest.raises(RuntimeError, match="not one of slife2's servers"):
            await job.call("nobody", "anything")

    assert refused.ok is False
    assert refused.text == "the agent said no"


@pytest.mark.asyncio
async def test_servers_is_read_from_the_config_table(peers: FakeClient) -> None:
    with job.running(context()):
        found = job.servers()

    assert found["agent"] == "slife2-agent"
    assert found["toolhub"] == "slife2-toolhub"
    assert found["openai-completions"] == "slife2-llm-openai"
    assert "job-server" in found


@pytest.mark.asyncio
async def test_a_handle_outside_a_run_says_so() -> None:
    with pytest.raises(RuntimeError, match="only work while"):
        await job.call_tool("turn_read")
    with pytest.raises(RuntimeError, match="only work while"):
        job.servers()
