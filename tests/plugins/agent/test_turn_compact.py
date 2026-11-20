"""`_turn_compact`: the view a turn's later calls run on, and the record.

The mechanism is a projection, so the two things worth proving are the two
lists: the model's last call is missing what the selector dropped, and the
turn's saved row has every message it ever had.  The selector is injected — the
question here is what the server does with an answer, not what a real model
would write.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from slife2.libs.host.config import (
    DEFAULT_AGENT,
    ToolServerSettings,
    default_config,
)
from slife2.libs.host.paths import DATA_ENV_VAR, agent_dir
from slife2.libs.model.base import StreamChatResult
from slife2.libs.turn.context import context_usage
from slife2.libs.turn.messages import ToolCall, Usage
from slife2.plugins.agent.server import build_server
from slife2.plugins.context.server import build_server as build_context
from slife2.plugins.toolhub.server import build_server as build_hub
from tests.fakes import (
    FakeBackend,
    ScriptedTurn,
    StubEmbedder,
    a_home,
    keep_the_context,
    plugin_transports,
    upstream_server,
    wait_until_settled,
)

pytestmark = pytest.mark.unit

#: What the tool answers with.  Small enough that the harness's own result
#: fitting never fires: the only reason this text leaves the view is the
#: compaction.
OLD = "the old tool result"


def config_over(*, compact_at: float = 0.9):
    base = default_config()
    return replace(
        base,
        agent=replace(base.agent, compact_at=compact_at),
        tool_load=replace(
            base.tool_load,
            autoload=(*base.tool_load.autoload, "fake__echo"),
        ),
        mcp={"fake": ToolServerSettings(name="fake", command="in-memory")},
    )


def cabinet_stub() -> FastMCP:
    """A cabinet with just the write the harness makes for a fitted result.

    This turn makes none — the result is well under `tool_result.bytes` — so the
    server is here to be a reachable peer rather than to be called.
    """
    server = FastMCP("cabinet-stub")

    @server.tool
    async def dump_save(call: str, output: str, tool: str = "") -> dict:
        return {
            "kept": call,
            "mirror": f"dumps/{call}.md",
            "path": f"agents/jack/cabinet/dumps/{call}.md",
            "size": len(output.encode("utf-8")),
        }

    return server


def calling(name: str, arguments: dict, call_id: str) -> ScriptedTurn:
    return ScriptedTurn(
        result=StreamChatResult(
            text="",
            tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
            stop_reason="tool_calls",
        )
    )


def answered(text: str) -> ScriptedTurn:
    return ScriptedTurn(result=StreamChatResult(text=text, stop_reason="stop"))


def stored_messages() -> list[dict]:
    return json.loads(
        sqlite3.connect(agent_dir(DEFAULT_AGENT) / "turns.db")
        .execute("SELECT messages FROM turn")
        .fetchone()[0]
    )


async def run_turn(cfg: Any, backend: FakeBackend, select: Any) -> Any:
    """One turn over the in-memory transport, with the agent as a hub source.

    **The hub has to reach the very agent server under test**, because the
    harness tools are declared by that server and are in the model's list
    through the hub's catalogue (`_turn_compact` is in `REQUIRED_AUTOLOAD`).
    `holder` is filled after the server exists and before the hub connects —
    the transport factory is called then, not at construction.
    """
    holder: dict[str, Any] = {}
    hub_client = Client(
        build_hub(
            cfg,
            transports=plugin_transports(
                cfg,
                {
                    "fake": lambda settings: upstream_server(),
                    "agent": lambda settings: holder["server"],
                },
            ),
            embedder=StubEmbedder(),
        )
    )
    context_client = Client(
        build_context(cfg, embedder=StubEmbedder(), ask=keep_the_context, select=select)
    )
    cabinet_client = Client(cabinet_stub())
    server = build_server(
        cfg,
        backend=backend,
        context_client=context_client,
        hub_client=hub_client,
        cabinet_client=cabinet_client,
    )
    # **Before the hub connects**, because its source factory asks for this
    # server the moment its own lifespan starts.
    holder["server"] = server
    async with hub_client, context_client, cabinet_client:
        await wait_until_settled(hub_client)
        async with Client(server) as client:
            return await client.call_tool(
                "send_message",
                {"agent": DEFAULT_AGENT, "prompt": "look something up"},
            )


@pytest.mark.asyncio
async def test_a_turn_drops_from_its_view_and_keeps_its_record(
    tmp_path, monkeypatch
) -> None:
    """**The whole mechanism, in one turn.**

    The model reads a tool result, then asks to compact; the selector drops the
    exchange that produced it; the model's next call is missing it.  The turn's
    saved row has the user's message, both calls, both results and the answer —
    the record is the conversation, not the view.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over()
    backend = FakeBackend(
        calling("fake__echo", {"text": OLD}, "c1"),
        calling("_turn_compact", {}, "c2"),
        answered("done"),
    )
    seen: list[list[dict]] = []
    usages: list[str] = []

    async def drop_all(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict],
        selectable: list[int],
        *,
        usage: str = "",
        previous: str = "",
        repair: str = "",
    ) -> str:
        seen.append([dict(message) for message in messages])
        usages.append(usage)
        return json.dumps({"compact": selectable})

    answer = await run_turn(cfg, backend, drop_all)

    assert answer.data["text"] == "done"
    assert len(backend.calls) == 3, "the model's own calls and no compaction call"
    assert len(seen) == 1, "one selector call"
    assert usages and usages[0], "the selector is told the context figure"
    assert any(str(message.get("content")) == OLD for message in seen[0]), (
        "the selector read the view before the drop"
    )

    final = backend.calls[-1][0]
    assert not any(
        message.role == "tool" and message.content == OLD for message in final
    ), "the model's later call is missing what it dropped"
    assert any(
        message.role == "user" and "look something up" in str(message.content)
        for message in final
    ), "the turn's opening message is fixed"

    rows = stored_messages()
    assert any(
        message.get("role") == "tool" and message.get("content") == OLD
        for message in rows
    ), "the record kept the dropped result"
    assert any("_turn_compact" in json.dumps(message) for message in rows), (
        "the pair is part of the turn"
    )
    report = [
        message.get("content")
        for message in rows
        if message.get("role") == "tool" and "Dropped" in str(message.get("content"))
    ]
    assert report and report[0].startswith("Dropped 2 message(s)"), (
        "the assistant call and its result went together"
    )


@pytest.mark.asyncio
async def test_the_harness_asks_at_the_measured_threshold(
    tmp_path, monkeypatch
) -> None:
    """**The automatic half**: the measured bill crosses `agent.compact_at`.

    The model never asks; the harness writes the pair at the iteration boundary,
    and the selector is told the same figure the status bar is showing.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over(compact_at=0.0005)
    base_turn = calling("fake__echo", {"text": OLD}, "c1")
    first = replace(
        base_turn,
        result=replace(
            base_turn.result,
            usage=Usage(prompt_tokens=500, completion_tokens=100),
        ),
    )
    backend = FakeBackend(first, answered("done"))
    usages: list[str] = []

    async def drop_all(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict],
        selectable: list[int],
        *,
        usage: str = "",
        previous: str = "",
        repair: str = "",
    ) -> str:
        usages.append(usage)
        return json.dumps({"compact": selectable})

    answer = await run_turn(cfg, backend, drop_all)

    assert answer.data["text"] == "done"
    assert len(backend.calls) == 2, "the compaction call is the harness's"
    final = backend.calls[-1][0]
    assert not any(
        message.role == "tool" and message.content == OLD for message in final
    )
    window = cfg.resolve(cfg.default)[2].context_window
    assert usages == [context_usage(600, window)[0]], (
        "the selector reads the last measured call, not an estimate"
    )

    rows = stored_messages()
    calls = [
        call
        for message in rows
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
    ]
    assert any(
        call["function"]["name"] == "_turn_compact"
        and call["id"].startswith("_harness_turn_compact_")
        for call in calls
    ), "the harness minted the pair, not the model"
    report = [
        message.get("content")
        for message in rows
        if message.get("role") == "tool" and "Dropped" in str(message.get("content"))
    ]
    assert report and report[0].startswith("Dropped 2 message(s)")


@pytest.mark.asyncio
async def test_a_selector_that_keeps_everything_drops_nothing(
    tmp_path, monkeypatch
) -> None:
    """The ordinary answer is `{}`; the turn continues on the full context."""
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over()
    backend = FakeBackend(
        calling("fake__echo", {"text": OLD}, "c1"),
        calling("_turn_compact", {}, "c2"),
        answered("done"),
    )

    async def keep_all(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict],
        selectable: list[int],
        *,
        usage: str = "",
        previous: str = "",
        repair: str = "",
    ) -> str:
        return "{}"

    answer = await run_turn(cfg, backend, keep_all)

    assert answer.data["text"] == "done"
    final = backend.calls[-1][0]
    assert any(message.role == "tool" and message.content == OLD for message in final)
    rows = stored_messages()
    report = [
        message.get("content")
        for message in rows
        if message.get("role") == "tool"
        and "Nothing dropped" in str(message.get("content"))
    ]
    assert report, "the pair says the turn kept everything"


@pytest.mark.asyncio
async def test_a_selector_failure_does_not_fail_the_turn(tmp_path, monkeypatch) -> None:
    """A provider failure is a fallback, not a failure mode: the turn goes on."""
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over()
    backend = FakeBackend(
        calling("fake__echo", {"text": OLD}, "c1"),
        calling("_turn_compact", {}, "c2"),
        answered("done"),
    )

    async def broken(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict],
        selectable: list[int],
        *,
        usage: str = "",
        previous: str = "",
        repair: str = "",
    ) -> str:
        raise RuntimeError("the provider is down")

    answer = await run_turn(cfg, backend, broken)

    assert answer.data["text"] == "done"
    rows = stored_messages()
    report = [
        message.get("content")
        for message in rows
        if message.get("role") == "tool"
        and "did not answer" in str(message.get("content"))
    ]
    assert report, "the pair names the failure rather than swallowing it"
    # The repair call is not made for a provider failure — there is no answer
    # to correct — so the selector was asked once.
    final = backend.calls[-1][0]
    assert any(message.role == "tool" and message.content == OLD for message in final)
