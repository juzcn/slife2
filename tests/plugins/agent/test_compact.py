"""A tool result too large for the conversation: what the turn carries instead.

The mechanism's halves live in three places — the choice and its rendering in
`slife2.libs.harness.dumps`, the seam in the loop, and the call in this server —
and what these tests are about is the third: that the harness reaches the
cabinet directly (it must, because `dump_save` carries no audience mark and the
hub has no row to route), that it asks the conversation's own model *under* the
conversation, and that the only fallback in the whole arrangement is the one
whose failure is expected.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace

import pytest
from fastmcp import Client, FastMCP

from slife2.libs.host.config import (
    DEFAULT_AGENT,
    ToolResultSettings,
    ToolServerSettings,
    default_config,
)
from slife2.libs.host.paths import DATA_ENV_VAR, agent_dir
from slife2.libs.model.base import StreamChatResult
from slife2.libs.turn.messages import ToolCall
from slife2.libs.turn.tokens import estimate_text_tokens
from slife2.plugins.agent.server import build_server
from slife2.plugins.context.server import build_server as build_context
from slife2.plugins.toolhub.server import build_server as build_hub
from tests.fakes import (
    FakeBackend,
    ScriptedTurn,
    StubEmbedder,
    keep_the_context,
    plugin_transports,
    upstream_server,
)

pytestmark = pytest.mark.unit

#: A window small enough that a test can overflow it with a body it also reads.
WINDOW = 400

#: The output a tool answers with, and the lines the discriminator picks out of
#: it.  Long enough to be over `WINDOW` many times over.
HUGE = "\n".join(f"line {number} of a tool output" for number in range(1, 3001))
PICKED = '{"lines": [[100, 102]]}'


def cabinet_stub(
    kept: dict[str, str], *, page: str = "[dump] page one"
) -> FastMCP:
    """A cabinet with just the two tools the window mechanism uses.

    Real and in-memory rather than mocked, for the reason the hub is: what is
    under test is the call this server makes and what it does with the answer,
    and a stand-in answering a different shape would prove nothing about the hop.
    """
    server = FastMCP("cabinet-stub")

    @server.tool
    async def dump_save(
        call: str, output: str, tool: str = "", mime: str = ""
    ) -> dict:
        kept[call] = output
        return {"kept": call, "mirror": f"dumps/{call}.md", "chars": len(output)}

    @server.tool
    async def dump_read(
        call: str,
        offset: int = 1,
        limit: int = 0,
        pattern: str = "",
        context: int = 0,
    ) -> str:
        return f"{page} over {call}"

    return server


def config_over(tokens: int):
    return replace(
        default_config(),
        tool_result=ToolResultSettings(tokens=tokens),
        # `autoload`, so the model may call the stand-in's tools without a
        # `func_tool_load` first: this test is about what happens to the result,
        # and a scripted load would be three lines of somebody else's subject.
        mcp={
            "fake": ToolServerSettings(
                name="fake", command="in-memory", autoload=True
            )
        },
    )


def calling(name: str, arguments: dict) -> ScriptedTurn:
    """A scripted turn where the model calls one tool."""
    return ScriptedTurn(
        result=StreamChatResult(
            text="",
            tool_calls=(ToolCall(id="c1", name=name, arguments=arguments),),
            stop_reason="tool_calls",
        ),
    )


def answered(text: str) -> ScriptedTurn:
    """A scripted turn where the model says something and stops."""
    return ScriptedTurn(result=StreamChatResult(text=text, stop_reason="stop"))


def stored_messages(agent: str = DEFAULT_AGENT) -> list[dict]:
    return json.loads(
        sqlite3.connect(agent_dir(agent) / "turns.db")
        .execute("SELECT messages FROM turn")
        .fetchone()[0]
    )


async def turn(cfg, backend, kept: dict, *, page: str = "[dump] page one") -> None:
    """One turn over the in-memory transport, with a cabinet that is a server."""
    async with (
        Client(
            build_hub(
                cfg,
                transports=plugin_transports(
                    cfg, {"fake": lambda settings: upstream_server()}
                ),
                embedder=StubEmbedder(),
            )
        ) as hub_client,
        Client(build_context(cfg, embedder=StubEmbedder(), ask=keep_the_context))
        as context_client,
        Client(cabinet_stub(kept, page=page)) as cabinet_client,
    ):
        server = build_server(
            cfg,
            backend=backend,
            context_client=context_client,
            hub_client=hub_client,
            cabinet_client=cabinet_client,
        )
        async with Client(server) as client:
            await client.call_tool(
                "send_message",
                {"agent": DEFAULT_AGENT, "prompt": "look something up"},
            )


@pytest.mark.asyncio
async def test_a_result_past_the_window_becomes_a_window_over_it(
    tmp_path, monkeypatch
) -> None:
    """**The whole mechanism, in one turn.**

    A tool answers with more than `tool_result.tokens`; the harness keeps the
    whole of it in the cabinet, asks the conversation's own model which lines
    this conversation needs, and what the conversation carries is those lines —
    the tool's own words, numbered, with the rest one `dump_read` away.

    Three scripted turns, and the middle one is the discriminator's: it runs on
    the same backend as the turn, because the selection is judged by the model
    that will read it.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    cfg = config_over(WINDOW)
    backend = FakeBackend(
        calling("fake__echo", {"text": HUGE}),
        answered(PICKED),
        answered("done"),
    )
    kept: dict[str, str] = {}

    await turn(cfg, backend, kept)

    results = [
        message for message in stored_messages() if message.get("role") == "tool"
    ]
    assert kept["c1"] == HUGE, "the whole of it was kept, keyed by the call"
    window = results[-1]["content"]
    assert window.startswith('[DUMP:{"call": "c1", "total": 3000}]')
    assert "line 100 of a tool output" in window
    assert "line 102 of a tool output" in window
    assert "line 103 of a tool output" not in window
    assert "dump_read" in window, "and the rest is named as reachable"
    assert estimate_text_tokens(window) <= WINDOW

    # The discriminator was shown the result **numbered** and the conversation it
    # is choosing for — the two things that make its reply an address.
    asked = backend.calls[1][0][-1].content
    assert " 100  line 100 of a tool output" in asked
    assert "look something up" in asked


@pytest.mark.asyncio
async def test_a_result_inside_the_window_is_left_alone(tmp_path, monkeypatch) -> None:
    """**The ordinary case costs nothing**: no dump, no discriminator call.

    Which is what makes the mechanism affordable — the results real turns carry
    are under the window, so none of this runs until one is not.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    cfg = config_over(4000)
    backend = FakeBackend(
        calling("fake__echo", {"text": "short"}),
        answered("done"),
    )
    kept: dict[str, str] = {}

    await turn(cfg, backend, kept)

    results = [
        message for message in stored_messages() if message.get("role") == "tool"
    ]
    assert results[-1]["content"] == "short", "as it came"
    assert kept == {}, "nothing was kept, and no model was asked"
    assert len(backend.calls) == 2, "the turn's two calls and no third"


@pytest.mark.asyncio
async def test_a_reply_nobody_can_read_gets_the_readers_own_first_window(
    tmp_path, monkeypatch, caplog
) -> None:
    """**The one fallback, and it is a design.**

    An unreadable reply is an ordinary event — a model is asked for a decision
    and answers in prose — and it has one answer rather than a retry: the
    conversation gets the reader's own first window, which is what a model that
    had asked for the beginning would have got.  It is bounded, so the loop this
    mechanism exists to prevent stays prevented.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    caplog.set_level(logging.INFO)
    cfg = config_over(WINDOW)
    backend = FakeBackend(
        calling("fake__echo", {"text": HUGE}),
        answered("I think the first few lines are the interesting ones."),
        answered("done"),
    )
    kept: dict[str, str] = {}

    await turn(cfg, backend, kept, page="[dump] page one")

    results = [
        message for message in stored_messages() if message.get("role") == "tool"
    ]
    assert results[-1]["content"] == "[dump] page one over c1", "the reader's own"
    assert "compact_discriminator_unparsed" in caplog.text


@pytest.mark.asyncio
async def test_a_dump_that_cannot_be_kept_fails_the_turn(tmp_path, monkeypatch) -> None:
    """**No fallback for a step that must succeed**, and this is that line.

    A conversation carrying an output nobody can read back has given up the bound
    *and* broken the address, and it would look like success — so a cabinet that
    cannot answer fails the turn, which is this file's rule for a missing peer
    everywhere else too.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    cfg = config_over(WINDOW)
    backend = FakeBackend(calling("fake__echo", {"text": HUGE}))
    refused = FastMCP("cabinet-refusing")

    @refused.tool
    async def dump_save(call: str, output: str, tool: str = "", mime: str = "") -> dict:
        raise ValueError("the cabinet is not answering")

    @refused.tool
    async def dump_read(
        call: str, offset: int = 1, limit: int = 0, pattern: str = "", context: int = 0
    ) -> str:
        return "[dump] a window"

    async with (
        Client(
            build_hub(
                cfg,
                transports=plugin_transports(
                    cfg, {"fake": lambda settings: upstream_server()}
                ),
                embedder=StubEmbedder(),
            )
        ) as hub_client,
        Client(build_context(cfg, embedder=StubEmbedder(), ask=keep_the_context))
        as context_client,
        Client(refused) as cabinet_client,
    ):
        server = build_server(
            cfg,
            backend=backend,
            context_client=context_client,
            hub_client=hub_client,
            cabinet_client=cabinet_client,
        )
        with pytest.raises(Exception):
            async with Client(server) as client:
                await client.call_tool(
                    "send_message",
                    {"agent": DEFAULT_AGENT, "prompt": "look something up"},
                )
