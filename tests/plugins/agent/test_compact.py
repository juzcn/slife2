"""A tool result too large for the conversation: what the turn carries instead.

The mechanism's halves live in three places — the truncation and its rendering
in `slife2.libs.harness.dumps`, the seam in the loop, and the two calls in this
server — and what these tests are about is the third: that the harness reaches
the cabinet directly (it must, because `dump_save` carries no audience mark and
the hub has no row to route), that the whole output is written before the
marker enters the conversation, and that no model is asked to choose what to
keep.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

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
    a_home,
    keep_the_context,
    plugin_transports,
    upstream_server,
    wait_until_settled,
)

pytestmark = pytest.mark.unit

#: A window small enough that a test can overflow it with a body it also reads.
WINDOW = 512

#: The output a tool answers with.  Long enough to be over `WINDOW` many times.
HUGE = "\n".join(f"line {number} of a tool output" for number in range(1, 3001))


def cabinet_stub(
    kept: dict[str, str],
    *,
    path: str | None = None,
) -> FastMCP:
    """A cabinet with just the tool the truncation uses.

    Real and in-memory rather than mocked, for the reason the hub is: what is
    under test is the call this server makes and what it does with the answer,
    and a stand-in answering a different shape would prove nothing about the hop.

    `path` is the file the answer names; it defaults to a path relative to the
    data directory, which is the shape the real cabinet answers with.
    """
    server = FastMCP("cabinet-stub")

    @server.tool
    async def dump_save(call: str, output: str, tool: str = "") -> dict:
        kept[call] = output
        return {
            "kept": call,
            "mirror": f"dumps/{call}.md",
            "path": path or f"agents/jack/cabinet/dumps/{call}.md",
            # The mirror is the output exactly.
            "size": len(output.encode("utf-8")),
        }

    return server


def config_over(tokens: int):
    base = default_config()
    return replace(
        base,
        tool_result=ToolResultSettings(tokens=tokens),
        # The fake server's tool is on the load list, so the model may call it
        # without a `func_tool_load` first: this test is about what happens to
        # the result, and a scripted load would be three lines of somebody
        # else's subject.
        tool_load=replace(
            base.tool_load,
            autoload=(*base.tool_load.autoload, "fake__echo"),
        ),
        mcp={"fake": ToolServerSettings(name="fake", command="in-memory")},
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


async def turn(
    cfg,
    backend,
    kept: dict,
    *,
    path: str | None = None,
) -> None:
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
        Client(
            build_context(cfg, embedder=StubEmbedder(), ask=keep_the_context)
        ) as context_client,
        Client(cabinet_stub(kept, path=path)) as cabinet_client,
    ):
        # **The hub's own start-up is waited for, because its bounds are not
        # promises.**  A source still connecting while the catalogue is being
        # written is the "not in the catalogue yet" answer, and a loaded runner
        # can exceed the wait an ask gives it — so the turn below waits for the
        # whole catalogue rather than for a fast machine.
        await wait_until_settled(hub_client)
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
async def test_a_result_past_the_window_becomes_a_head_and_a_file(
    tmp_path, monkeypatch
) -> None:
    """**The whole mechanism, in one turn.**

    A tool answers with more than `tool_result.tokens`; the harness keeps the
    whole of it in the cabinet and the conversation carries a fixed head of it,
    the file's size, and the path the rest can be read from.  No model is asked
    what to keep: the turn's two calls are its own, and the head is the tool's
    own words.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over(WINDOW)
    backend = FakeBackend(
        calling("fake__echo", {"text": HUGE}),
        answered("done"),
    )
    kept: dict[str, str] = {}

    where = "agents/jack/cabinet/dumps/c1.md"
    await turn(cfg, backend, kept, path=where)

    results = [
        message for message in stored_messages() if message.get("role") == "tool"
    ]
    assert kept["c1"] == HUGE, "the whole of it was kept, keyed by the call"
    window = results[-1]["content"]
    assert window.startswith("[DUMP:")
    assert "line 1 of a tool output" in window, "the head is the tool's own words"
    assert where in window, "the file is named"
    assert "dump_read" in window and "dump_grep" in window
    assert estimate_text_tokens(window) <= WINDOW

    started = window.index("[DUMP:") + len("[DUMP:")
    ended = window.index("]", started)
    marker = json.loads(window[started:ended])
    assert marker == {"call": "c1", "bytes": len(HUGE), "file": where}, (
        "the marker carries the call, the file's size and its path — no counts"
    )

    assert len(backend.calls) == 2, "the turn's two calls and no third"


@pytest.mark.asyncio
async def test_a_result_inside_the_window_is_left_alone(tmp_path, monkeypatch) -> None:
    """**The ordinary case costs nothing**: no dump, no truncation.

    Which is what makes the mechanism affordable — the results real turns carry
    are under the window, so none of this runs until one is not.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
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
async def test_a_dump_that_cannot_be_kept_fails_the_turn(tmp_path, monkeypatch) -> None:
    """**No fallback for a step that must succeed**, and this is that line.

    A conversation carrying a marker whose file is not there has given up the
    bound *and* broken the address, and it would look like success — so a
    cabinet that cannot answer fails the turn.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over(WINDOW)
    backend = FakeBackend(calling("fake__echo", {"text": HUGE}))
    refused = FastMCP("cabinet-refusing")

    @refused.tool
    async def dump_save(call: str, output: str, tool: str = "") -> dict:
        raise ValueError("the cabinet is not answering")

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
        Client(
            build_context(cfg, embedder=StubEmbedder(), ask=keep_the_context)
        ) as context_client,
        Client(refused) as cabinet_client,
    ):
        server = build_server(
            cfg,
            backend=backend,
            context_client=context_client,
            hub_client=hub_client,
            cabinet_client=cabinet_client,
        )
        with pytest.raises(ToolError, match="the cabinet is not answering"):
            async with Client(server) as client:
                await client.call_tool(
                    "send_message",
                    {"agent": DEFAULT_AGENT, "prompt": "look something up"},
                )


@pytest.mark.asyncio
async def test_a_dump_answer_without_a_path_fails_the_turn(
    tmp_path, monkeypatch
) -> None:
    """**An answer of the wrong shape is a harness failure.**

    The marker promises a file path; a cabinet that wrote the row but answered
    without one is a different build, and reading that as "no dump" would put a
    marker with no address into the conversation.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over(WINDOW)
    backend = FakeBackend(calling("fake__echo", {"text": HUGE}))
    shapeless = FastMCP("cabinet-shapeless")

    @shapeless.tool
    async def dump_save(call: str, output: str, tool: str = "") -> dict:
        return {"kept": call}

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
        Client(
            build_context(cfg, embedder=StubEmbedder(), ask=keep_the_context)
        ) as context_client,
        Client(shapeless) as cabinet_client,
    ):
        server = build_server(
            cfg,
            backend=backend,
            context_client=context_client,
            hub_client=hub_client,
            cabinet_client=cabinet_client,
        )
        with pytest.raises(ToolError, match="dump's path and size"):
            async with Client(server) as client:
                await client.call_tool(
                    "send_message",
                    {"agent": DEFAULT_AGENT, "prompt": "look something up"},
                )


@pytest.mark.asyncio
async def test_the_cabinet_is_closed_and_forgotten_with_the_session(
    tmp_path, monkeypatch
) -> None:
    """A client the lifespan closed is also a client it forgot — the third one.

    FastMCP runs the lifespan once per client session over the in-memory
    transport, and every caller here reconnects per call, so a client left in
    place after being closed is one the *next* session is handed already shut.
    `clients`, `context_conn` and `hub_conn` were closed **and cleared** for
    exactly that reason (`lifespan`'s own comment); the cabinet — the one peer
    this server reaches directly, because `dump_save` carries no audience mark
    for the hub to route — was neither.  So a session that ran one result past
    the window left the next one calling `dump_save` on a dead client, and
    `compress_for` deliberately does not catch a failed write: every turn of
    that session with an oversized result failed at its first hop.

    Two sessions, one oversized result each: the cabinet is opened twice, and
    the one the first session opened is closed on the way out.
    """
    from slife2.libs.host.config import (
        CABINET_SERVER_NAME,
        CONTEXT_SERVER_NAME,
        TOOLHUB_SERVER_NAME,
    )
    from slife2.plugins.agent import server as server_module

    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config_over(WINDOW)
    backend = FakeBackend(
        *(calling("fake__echo", {"text": HUGE}), answered("done")) * 2
    )
    kept: dict[str, str] = {}
    cabinets: list[Client] = []
    closed: list[Client] = []
    real_close = server_module.close_server

    async def open_ours(_url, **kwargs):
        """The server's own lazily-opened peers, in memory and by name."""
        name = kwargs.get("name", "")
        if name == CABINET_SERVER_NAME:
            client = Client(cabinet_stub(kept))
        elif name == CONTEXT_SERVER_NAME:
            client = Client(
                build_context(cfg, embedder=StubEmbedder(), ask=keep_the_context)
            )
        else:
            assert name == TOOLHUB_SERVER_NAME
            client = Client(
                build_hub(
                    cfg,
                    transports=plugin_transports(
                        cfg, {"fake": lambda settings: upstream_server()}
                    ),
                    embedder=StubEmbedder(),
                )
            )
        await client.__aenter__()
        if name == CABINET_SERVER_NAME:
            cabinets.append(client)
        return client

    async def close_ours(client):
        closed.append(client)
        await real_close(client)

    monkeypatch.setattr(server_module, "open_server", open_ours)
    monkeypatch.setattr(server_module, "close_server", close_ours)
    server = build_server(cfg, backend=backend)

    # **A session each**, which is what makes the second one a fresh start: the
    # lifespan runs once per client session over the in-memory transport, and
    # that is the hook that closes and clears this server's own clients.
    for prompt in ("look one up", "look another up"):
        async with Client(server) as client:
            answer = await client.call_tool(
                "send_message", {"agent": DEFAULT_AGENT, "prompt": prompt}
            )
        assert answer.data["text"] == "done"

    assert len(cabinets) == 2, "a session was handed a cabinet that was closed"
    assert all(one in closed for one in cabinets), "the cabinet was never closed"
    results = [
        message for message in stored_messages() if message.get("role") == "tool"
    ]
    assert results[-1]["content"].startswith("[DUMP:"), (
        "both sessions ran their oversized result through a live cabinet"
    )
