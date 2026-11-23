"""`_turn_compact`: the view a turn's later calls run on, and the record.

The mechanism is a projection, so the two things worth proving are the two
lists: the model's last call is missing what the selector dropped, and the
turn's saved row has every message it ever had.  The selector is injected — the
question here is what the server does with an answer, not what a real model
would write.
"""

from __future__ import annotations

import json
import re
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
from slife2.libs.turn.events import ToolCallFinished, decode
from slife2.libs.turn.messages import ToolCall, Usage
from slife2.libs.turn.tools import CONTEXT_REBUILD, TURN_COMPACT
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


LIVE_ID = re.compile(r' ?\[MESSAGE:\{"id": "([^"]+)"\}\]')


def without_id(content: Any) -> str:
    """A view message's text with the live-id footnote taken back off."""
    return LIVE_ID.sub("", str(content or ""))


def view_id(message: dict) -> str:
    """The live id the view wrote onto a message, or `""` for the system prompt."""
    content = message.get("content")
    if isinstance(content, str):
        found = LIVE_ID.search(content)
        return found.group(1) if found else ""
    if isinstance(content, list):
        for part in reversed(content):
            if isinstance(part, dict) and part.get("type") == "text":
                found = LIVE_ID.search(str(part.get("text") or ""))
                if found:
                    return found.group(1)
    return ""


def drop_echo(messages: list[dict], selectable: list[str]) -> list[str]:
    """The `fake__echo` exchange's live ids — the pair this test is about.

    The turn now opens with `_context_rebuild`'s harness pair, so "drop
    everything selectable" would take that too; the mechanism under test is the
    echo exchange and its result.
    """
    ids = [view_id(message) for message in messages]
    wanted: list[str] = []
    for index, message in enumerate(messages):
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        if message.get("role") != "tool" or OLD not in text:
            continue
        if ids[index] and ids[index] in selectable:
            wanted.append(ids[index])
        if (
            index
            and messages[index - 1].get("role") == "assistant"
            and ids[index - 1]
            and ids[index - 1] in selectable
        ):
            wanted.append(ids[index - 1])
    return sorted(dict.fromkeys(wanted))


async def no_drop(*_args: Any, **_kwargs: Any) -> str:
    """A selector that keeps everything; tests that must not reach it use it."""
    return json.dumps({"compact": []})


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


def high(text: str, call_id: str) -> ScriptedTurn:
    """A tool turn whose own call reports a context over a tiny threshold."""
    base = calling("fake__echo", {"text": text}, call_id)
    return replace(
        base,
        result=replace(
            base.result,
            usage=Usage(prompt_tokens=500, completion_tokens=100),
        ),
    )


def stored_messages() -> list[dict]:
    """The latest turn's saved row: the turn whose view the test is about."""
    return json.loads(
        sqlite3.connect(agent_dir(DEFAULT_AGENT) / "turns.db")
        .execute("SELECT messages FROM turn ORDER BY rowid DESC LIMIT 1")
        .fetchone()[0]
    )


async def run_turn(
    cfg: Any,
    backend: FakeBackend,
    select: Any,
    on_progress: Any = None,
    ask: Any = keep_the_context,
) -> Any:
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
        build_context(cfg, embedder=StubEmbedder(), ask=ask, select=select)
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
                progress_handler=on_progress,
            )


@pytest.mark.asyncio
async def test_a_turn_drops_from_its_view_and_keeps_its_record(
    tmp_path, monkeypatch
) -> None:
    """**The whole mechanism, in one turn.**

    The model reads a tool result, then calls `_turn_compact` with the exchange
    that produced it; the tool drops it from the view; the model's next call is
    missing it.  The turn's saved row has the user's message, both calls, both
    results and the answer — the record is the conversation, not the view.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over()
    backend = FakeBackend(
        calling("fake__echo", {"text": OLD}, "c1"),
        calling("_turn_compact", {"compact": ["cur.4"]}, "c2"),
        answered("done"),
    )
    seen: list[list[dict]] = []
    usages: list[str] = []
    events: list[Any] = []

    async def on_progress(progress: Any, total: Any, message: Any) -> None:
        event = decode(message or "")
        if event is not None:
            events.append(event)

    async def drop_all(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict],
        selectable: list[str],
        *,
        usage: str = "",
        previous: str = "",
        repair: str = "",
    ) -> str:
        seen.append([dict(message) for message in messages])
        usages.append(usage)
        return json.dumps({"compact": drop_echo(messages, selectable)})

    answer = await run_turn(cfg, backend, drop_all, on_progress)

    assert answer.data["text"] == "done"
    assert len(backend.calls) == 3, "the model's own calls and no compaction call"
    assert not seen, "the model's own selection does not ask the selector"
    finished = [
        event
        for event in events
        if isinstance(event, ToolCallFinished) and event.name == TURN_COMPACT
    ]
    assert finished, "the tool's output reaches the turn's stream"

    final = backend.calls[-1][0]
    assert not any(
        message.role == "tool" and without_id(message.content) == OLD
        for message in final
    ), "the model's later call is missing what it dropped"
    assert any(
        message.role == "user" and "look something up" in str(message.content)
        for message in final
    ), "the turn's opening message is fixed"

    assert not any(
        message.role == "assistant" and "[MESSAGE:" in str(message.content)
        for message in final
    ), "an annotation is never written into the model's own words"
    rows = stored_messages()
    assert "[MESSAGE:" not in json.dumps(rows), (
        "the record is the conversation, not the view"
    )
    assert any(
        message.get("role") == "tool" and without_id(message.get("content")) == OLD
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
    assert finished[0].result_preview == report[0], (
        "the note the person sees and the tool's output are one text"
    )


@pytest.mark.asyncio
async def test_the_model_fills_the_compaction_itself(tmp_path, monkeypatch) -> None:
    """One model call, no selector: the ids it read are the ids it writes."""
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over()

    class Fills(FakeBackend):
        """Answers the second call with the ids its own view carried."""

        def stream(self, messages: list[Any], tools: list[Any]) -> Any:
            if len(self.calls) == 1:
                view = [message.to_wire() for message in messages]
                ids = [view_id(message) for message in view]
                self._turns[0] = calling(
                    TURN_COMPACT, {"compact": drop_echo(view, ids)}, "c2"
                )
            return super().stream(messages, tools)

    backend = Fills(
        calling("fake__echo", {"text": OLD}, "c1"),
        answered("unused"),
        answered("done"),
    )
    selected: list[Any] = []

    async def selector(*args: Any, **kwargs: Any) -> str:
        selected.append(1)
        return json.dumps({"compact": []})

    answer = await run_turn(cfg, backend, selector)

    assert answer.data["text"] == "done"
    assert selected == [], "the model's own call does not ask the selector"
    final = backend.calls[-1][0]
    assert not any(
        message.role == "tool" and without_id(message.content) == OLD
        for message in final
    ), "the view drops what the model named"
    rows = stored_messages()
    assert any(
        message.get("role") == "tool" and without_id(message.get("content")) == OLD
        for message in rows
    ), "the record keeps it"
    pair = [
        message
        for message in rows
        if message.get("tool_calls")
        and message["tool_calls"][0]["function"]["name"] == TURN_COMPACT
    ]
    assert pair, "the model's call is in the turn"
    assert json.loads(pair[0]["tool_calls"][0]["function"]["arguments"]).get(
        "compact"
    ), "the pair carries the ids the model named"


@pytest.mark.asyncio
async def test_the_model_fills_the_rebuild_itself(tmp_path, monkeypatch) -> None:
    """The automatic rebuild decides once; the model's own call carries its args."""
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over()
    first = await run_turn(cfg, FakeBackend(answered("first")), no_drop)
    assert first.data["text"] == "first"

    asked: list[Any] = []

    async def ask(*args: Any, **kwargs: Any) -> str:
        asked.append(1)
        return "{}"

    backend = FakeBackend(
        calling(CONTEXT_REBUILD, {"mode": "clear"}, "c1"),
        answered("second"),
    )
    answer = await run_turn(cfg, backend, no_drop, ask=ask)

    assert answer.data["text"] == "second"
    assert len(asked) == 1, "the automatic rebuild asked; the model's own call did not"
    final = backend.calls[-1][0]
    assert not any(
        message.role == "assistant" and without_id(message.content) == "first"
        for message in final
    ), "the first turn is no longer in the view"
    calls = [
        json.loads(message["tool_calls"][0]["function"]["arguments"])
        for message in stored_messages()
        if message.get("tool_calls")
        and message["tool_calls"][0]["function"]["name"] == CONTEXT_REBUILD
    ]
    assert {"mode": "clear"} in calls, "the pair carries the selection"


@pytest.mark.asyncio
async def test_a_selection_of_one_result_takes_its_whole_exchange(
    tmp_path, monkeypatch
) -> None:
    """**The model names what it can see; the harness completes the exchange.**

    A tool-call-only assistant message carries no annotation, so the model's
    natural selection is the result.  The schema says an exchange goes as one,
    and the call follows the result instead of leaving a split the next request
    would reject.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over()

    class Splits(FakeBackend):
        def stream(self, messages: list[Any], tools: list[Any]) -> Any:
            if len(self.calls) == 1:
                view = [message.to_wire() for message in messages]
                tool_id = next(
                    view_id(message)
                    for message in view
                    if message.get("role") == "tool"
                    and OLD in str(message.get("content"))
                )
                self._turns[0] = calling(TURN_COMPACT, {"compact": [tool_id]}, "c2")
            return super().stream(messages, tools)

    backend = Splits(
        calling("fake__echo", {"text": OLD}, "c1"),
        answered("unused"),
        answered("done"),
    )
    selected: list[Any] = []

    async def selector(*args: Any, **kwargs: Any) -> str:
        selected.append(1)
        return json.dumps({"compact": []})

    answer = await run_turn(cfg, backend, selector)

    assert answer.data["text"] == "done"
    assert selected == [], "the model's own call does not ask the selector"
    final = backend.calls[-1][0]
    assert not any(
        message.role == "tool" and without_id(message.content) == OLD
        for message in final
    ), "the exchange left the view with its result"
    assert not any(
        message.role == "assistant"
        and any(call.name == "fake__echo" for call in message.tool_calls)
        for message in final
    ), "the call went with it"
    rows = stored_messages()
    assert any(
        message.get("role") == "tool" and without_id(message.get("content")) == OLD
        for message in rows
    ), "the record keeps every message"


@pytest.mark.asyncio
async def test_an_automatic_selection_of_one_result_takes_the_exchange(
    tmp_path, monkeypatch
) -> None:
    """The automatic pass names a result; the exchange completes with it."""
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

    async def one_result(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict],
        selectable: list[str],
        *,
        usage: str = "",
        previous: str = "",
        repair: str = "",
    ) -> str:
        ids = [
            view_id(message)
            for message in messages
            if message.get("role") == "tool" and OLD in str(message.get("content"))
        ]
        return json.dumps({"compact": ids})

    answer = await run_turn(cfg, backend, one_result)

    assert answer.data["text"] == "done", "the turn continues"
    final = backend.calls[-1][0]
    assert not any(
        message.role == "tool" and without_id(message.content) == OLD
        for message in final
    ), "the exchange left the view"
    rows = stored_messages()
    report = [
        message.get("content")
        for message in rows
        if message.get("role") == "tool" and "Dropped" in str(message.get("content"))
    ]
    assert report and report[0].startswith("Dropped 2 message(s)"), (
        "the result and the call that asked for it"
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
        selectable: list[str],
        *,
        usage: str = "",
        previous: str = "",
        repair: str = "",
    ) -> str:
        usages.append(usage)
        return json.dumps({"compact": drop_echo(messages, selectable)})

    answer = await run_turn(cfg, backend, drop_all)

    assert answer.data["text"] == "done"
    assert len(backend.calls) == 2, "the compaction call is the harness's"
    final = backend.calls[-1][0]
    assert not any(
        message.role == "tool" and without_id(message.content) == OLD
        for message in final
    )
    window = cfg.resolve(cfg.default)[2].prompt_window
    assert usages == [context_usage(600, window)[0]], (
        "the selector reads the last measured call against the prompt budget"
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
    arguments = [
        json.loads(call["function"]["arguments"])
        for call in calls
        if call["function"]["name"] == "_turn_compact"
    ]
    assert arguments and arguments[0].get("compact"), (
        "the harness pair carries the selector's ids"
    )
    report = [
        message.get("content")
        for message in rows
        if message.get("role") == "tool" and "Dropped" in str(message.get("content"))
    ]
    assert report and report[0].startswith("Dropped 2 message(s)")


@pytest.mark.asyncio
async def test_a_successful_compaction_can_be_asked_again(
    tmp_path, monkeypatch
) -> None:
    """**No once-per-turn cap**: a context that grows back over the fraction is
    asked about again at the next boundary."""
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over(compact_at=0.0005)
    backend = FakeBackend(high(OLD, "c1"), high(OLD, "c2"), answered("done"))
    usages: list[str] = []

    async def drop_all(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict],
        selectable: list[str],
        *,
        usage: str = "",
        previous: str = "",
        repair: str = "",
    ) -> str:
        usages.append(usage)
        return json.dumps({"compact": drop_echo(messages, selectable)})

    answer = await run_turn(cfg, backend, drop_all)

    assert answer.data["text"] == "done"
    assert len(usages) == 2, "the second crossing asked again"
    rows = stored_messages()
    pairs = [
        call
        for message in rows
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
        if call["function"]["name"] == "_turn_compact"
    ]
    assert len(pairs) == 2, "both asks are in the record"


@pytest.mark.asyncio
async def test_a_keep_all_decision_stops_the_automatic_pass(
    tmp_path, monkeypatch
) -> None:
    """A selector that keeps everything has decided the context is enough.

    The harness must not pay for the same answer at every boundary; the model
    can still call the tool itself, and a successful call resumes the pass.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over(compact_at=0.0005)
    backend = FakeBackend(high(OLD, "c1"), high(OLD, "c2"), answered("done"))
    asked: list[str] = []

    async def keep_all(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict],
        selectable: list[str],
        *,
        usage: str = "",
        previous: str = "",
        repair: str = "",
    ) -> str:
        asked.append(usage)
        return json.dumps({"compact": []})

    answer = await run_turn(cfg, backend, keep_all)

    assert answer.data["text"] == "done"
    assert len(asked) == 1, "the keep-all stopped the automatic pass"
    rows = stored_messages()
    pairs = [
        call
        for message in rows
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
        if call["function"]["name"] == "_turn_compact"
    ]
    assert len(pairs) == 1
    assert any(
        message.get("role") == "tool"
        and "Nothing dropped" in str(message.get("content"))
        for message in rows
    )


@pytest.mark.asyncio
async def test_a_selector_that_keeps_everything_drops_nothing(
    tmp_path, monkeypatch
) -> None:
    """The ordinary answer is `{}`; the turn continues on the full context."""
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over()
    backend = FakeBackend(
        calling("fake__echo", {"text": OLD}, "c1"),
        calling("_turn_compact", {"compact": []}, "c2"),
        answered("done"),
    )

    async def keep_all(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict],
        selectable: list[str],
        *,
        usage: str = "",
        previous: str = "",
        repair: str = "",
    ) -> str:
        return json.dumps({"compact": []})

    answer = await run_turn(cfg, backend, keep_all)

    assert answer.data["text"] == "done"
    final = backend.calls[-1][0]
    assert any(
        message.role == "tool" and without_id(message.content) == OLD
        for message in final
    )
    rows = stored_messages()
    report = [
        message.get("content")
        for message in rows
        if message.get("role") == "tool"
        and "Nothing dropped" in str(message.get("content"))
    ]
    assert report, "the pair says the turn kept everything"


@pytest.mark.asyncio
async def test_a_later_turn_can_drop_a_historical_exchange(
    tmp_path, monkeypatch
) -> None:
    """**The history stays addressable across turns.**

    A turn's live ids are written when its id arrives, and the rebuild hop
    carries them back unchanged — the automatic rebuild of the next turn is a
    no-op that echoes the caller's own list.  Without both halves the view
    offered only the messages of the turn running, so a compaction could never
    name a turn that had already been saved.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over()
    first = await run_turn(
        cfg,
        FakeBackend(calling("fake__echo", {"text": OLD}, "c1"), answered("first")),
        no_drop,
    )
    assert first.data["text"] == "first"

    class DropsHistory(FakeBackend):
        """Names the historical echo exchange by the ids its own view shows."""

        def stream(self, messages: list[Any], tools: list[Any]) -> Any:
            if len(self.calls) == 0:
                view = [message.to_wire() for message in messages]
                ids = [view_id(message) for message in view]
                self._turns[0] = calling(
                    TURN_COMPACT,
                    {"compact": drop_echo(view, [mid for mid in ids if mid])},
                    "c2",
                )
            return super().stream(messages, tools)

    backend = DropsHistory(answered("unused"), answered("done"))
    answer = await run_turn(cfg, backend, no_drop)

    assert answer.data["text"] == "done"
    final = backend.calls[-1][0]
    assert not any(
        message.role == "tool" and without_id(message.content) == OLD
        for message in final
    ), "the historical exchange left this turn's view"
    report = [
        message.get("content")
        for message in stored_messages()
        if message.get("role") == "tool" and "Dropped" in str(message.get("content"))
    ]
    assert report and report[0].startswith("Dropped 2 message(s)"), (
        "the id the view showed was the one the selection applied"
    )


@pytest.mark.asyncio
async def test_a_selection_naming_an_id_the_view_does_not_offer_is_refused(
    tmp_path, monkeypatch
) -> None:
    """The model's own call is read strictly, not filtered.

    An id the view did not offer is a message the model cannot name — the fixed
    two, something an earlier compaction hid, or no id at all.  Applying the
    part that resolves would make half a selection the model did not make, so
    the whole answer is a tool error it can correct.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over()
    backend = FakeBackend(
        calling("fake__echo", {"text": OLD}, "c1"),
        calling(TURN_COMPACT, {"compact": ["99.9"]}, "c2"),
        answered("done"),
    )

    answer = await run_turn(cfg, backend, no_drop)

    assert answer.data["text"] == "done", "a refusal is a tool error, not a turn"
    final = backend.calls[-1][0]
    assert any(
        message.role == "tool" and without_id(message.content) == OLD
        for message in final
    ), "nothing was hidden"
    rows = stored_messages()
    assert any(
        message.get("role") == "tool"
        and "does not offer" in str(message.get("content"))
        for message in rows
    ), "the tool error names the id the view never showed"


@pytest.mark.asyncio
async def test_a_malformed_selection_is_a_tool_error_not_a_silent_no_op(
    tmp_path, monkeypatch
) -> None:
    """A string is iterable, and reading it as ids would hide the wrong messages.

    The schema says the field is an array, but a provider that ignores the
    schema can still hand the body a string; the answer is the one the model
    can act on, not "nothing dropped".
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over()
    backend = FakeBackend(
        calling("fake__echo", {"text": OLD}, "c1"),
        calling(TURN_COMPACT, {"compact": "cur.1"}, "c2"),
        answered("done"),
    )

    answer = await run_turn(cfg, backend, no_drop)

    assert answer.data["text"] == "done"
    final = backend.calls[-1][0]
    assert any(
        message.role == "tool" and without_id(message.content) == OLD
        for message in final
    ), "nothing was hidden"
    rows = stored_messages()
    assert any(
        message.get("role") == "tool"
        and "could not be read" in str(message.get("content"))
        for message in rows
    )


@pytest.mark.asyncio
async def test_a_selector_failure_does_not_fail_the_turn(tmp_path, monkeypatch) -> None:
    """A provider failure is a fallback, not a failure mode: the turn goes on."""
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over(compact_at=0.0005)
    backend = FakeBackend(high(OLD, "c1"), answered("done"))

    async def broken(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict],
        selectable: list[str],
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
    assert "RuntimeError: the provider is down" in str(report[0]), (
        "the provider's own words are in the report, as on a failed turn's line"
    )
    # One selector call, and no repair: the failure is the tool's result.
    final = backend.calls[-1][0]
    assert any(
        message.role == "tool" and without_id(message.content) == OLD
        for message in final
    )


@pytest.mark.asyncio
async def test_a_failed_selector_is_not_asked_again_this_turn(
    tmp_path, monkeypatch
) -> None:
    """**A failure is a decision about the whole view, like a keep-all.**

    The threshold stays crossed and the turn keeps running tools, so a pass
    that did not stop would pay for the same failing provider at every
    boundary.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over(compact_at=0.0005)
    backend = FakeBackend(high(OLD, "c1"), high(OLD, "c2"), answered("done"))
    asked: list[str] = []

    async def broken(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict],
        selectable: list[str],
        *,
        usage: str = "",
        previous: str = "",
        repair: str = "",
    ) -> str:
        asked.append(usage)
        raise RuntimeError("the provider is down")

    answer = await run_turn(cfg, backend, broken)

    assert answer.data["text"] == "done"
    assert len(asked) == 1, "the failed provider was not asked again"
    rows = stored_messages()
    pairs = [
        call
        for message in rows
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
        if call["function"]["name"] == TURN_COMPACT
    ]
    assert len(pairs) == 1, "one pair, carrying the failure"
