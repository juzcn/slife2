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

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
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
from slife2.libs.model.base import Stream, StreamChatResult
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
    kept: dict[str, str],
    *,
    page: str = "[dump] page one",
    mimes: dict[str, str] | None = None,
) -> FastMCP:
    """A cabinet with just the two tools the window mechanism uses.

    Real and in-memory rather than mocked, for the reason the hub is: what is
    under test is the call this server makes and what it does with the answer,
    and a stand-in answering a different shape would prove nothing about the hop.

    `mimes` is the recorder for the one argument the harness has to *supply* —
    what the output is, when it is known not to be text.  A dict rather than a
    mock's assertion, because what is being asserted is what crossed the hop.
    """
    server = FastMCP("cabinet-stub")

    @server.tool
    async def dump_save(call: str, output: str, tool: str = "", mime: str = "") -> dict:
        kept[call] = output
        if mimes is not None:
            mimes[call] = mime
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
            "fake": ToolServerSettings(name="fake", command="in-memory", autoload=True)
        },
    )


#: How long a `StreamedBackend` result waits for somebody to read its chunks
#: before it gives up.  Not a budget the product has — the real bound is the
#: stall watchdog (`slife2.helpers.timeouts`) — but the stand-in for one, so a
#: caller that never drains fails a test in a second instead of hanging it.
UNREAD_SECONDS = 1.0


class StreamedBackend(FakeBackend):
    """A `FakeBackend` whose result arrives only once its chunks are read.

    **The coupling the real backend has and this fake did not.**  `MCPBackend.
    stream` hands back a `Stream` whose `chunks` is the drain — the thing that
    holds the stall watchdog — and whose `result` is the call itself, so a
    caller that awaits the result *without* iterating the chunks waits on a
    connection nothing is watching for silence.  Two independent halves cannot
    show that, which is how a discriminator call that awaited `result` alone
    went unnoticed: on the real backend it is a turn that hangs for ever rather
    than one that fails.
    """

    def stream(self, messages, tools):
        stream = super().stream(messages, tools)
        inner = stream.chunks
        read = asyncio.Event()

        async def chunks() -> AsyncIterator:
            async for chunk in inner:
                yield chunk
            read.set()

        async def result() -> StreamChatResult:
            await asyncio.wait_for(read.wait(), timeout=UNREAD_SECONDS)
            return await stream.result

        return Stream(chunks=chunks(), result=result())


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
    page: str = "[dump] page one",
    mimes: dict[str, str] | None = None,
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
        Client(cabinet_stub(kept, page=page, mimes=mimes)) as cabinet_client,
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
    mimes: dict[str, str] = {}

    await turn(cfg, backend, kept, mimes=mimes)

    results = [
        message for message in stored_messages() if message.get("role") == "tool"
    ]
    assert kept["c1"] == HUGE, "the whole of it was kept, keyed by the call"
    # ...and no verdict about what it is: the model selected lines out of it, so
    # the row is text, and `dump_read` opening it is the promise the window's own
    # sentence makes.  The empty string is the store's answer for a row nobody
    # has a type for, which is exactly what the harness knows here.
    assert mimes["c1"] == ""
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
async def test_the_discriminator_call_is_read_as_a_stream(
    tmp_path, monkeypatch
) -> None:
    """**The discriminator drains its stream, so a silent provider fails it.**

    The stall watchdog that bounds every model call lives in the drain
    (`slife2.clients.model.MCPBackend._drain`) and is armed by *iterating* the
    chunks — and `open_backend` deliberately passes no timeout, because the
    watchdog is the bound.  So a discriminator that awaited `stream.result`
    alone would wait on a connection with no deadline at all: a provider that
    took the call and then went quiet would hang the turn for ever with the
    conversation's lock held, where the same silence fails a turn in the loop
    in `timeouts.work.stall` seconds.

    `StreamedBackend` is what makes that difference visible — its result does
    not arrive until its chunks have been read — so this test is about the
    *contract* the loop above already keeps and this step has to keep too.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    cfg = config_over(WINDOW)
    backend = StreamedBackend(
        calling("fake__echo", {"text": HUGE}),
        answered(PICKED),
        answered("done"),
    )
    kept: dict[str, str] = {}

    await turn(cfg, backend, kept)

    results = [
        message for message in stored_messages() if message.get("role") == "tool"
    ]
    window = results[-1]["content"]
    assert window, "the turn finished rather than hanging on an unread stream"
    assert "line 100 of a tool output" in window
    assert estimate_text_tokens(window) <= WINDOW


@pytest.mark.asyncio
async def test_an_output_that_is_not_text_is_dumped_as_one(
    tmp_path, monkeypatch
) -> None:
    """**The one judgement the cabinet cannot make for itself.**

    A payload that is not text arrives as a `str` like any other, so nothing in
    the harness knows the difference — which is why the model is the party asked,
    and why the answer has to travel *with the dump*: `dump_read` refuses a row
    whose mime is not `text/`, and `dumps.window(binary=True)` already writes "the
    whole of it is kept, and `dump_read` will not open it" into the conversation.
    A row written without the verdict makes that sentence false — `dump_read`
    hands back a numbered window of a picture — so the write and the answer are
    one step: the row is written once (`dump_save` replaces by key), with what the
    model said in it.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    cfg = config_over(WINDOW)
    backend = FakeBackend(
        calling("fake__echo", {"text": HUGE}),
        answered('{"lines": "binary"}'),
        answered("done"),
    )
    kept: dict[str, str] = {}
    mimes: dict[str, str] = {}

    await turn(cfg, backend, kept, mimes=mimes)

    from slife2.libs.harness.dumps import NOT_TEXT

    assert kept["c1"] == HUGE, "a binary output is still kept whole"
    assert mimes["c1"] == NOT_TEXT
    results = [
        message for message in stored_messages() if message.get("role") == "tool"
    ]
    window = results[-1]["content"]
    assert "not text" in window
    assert "dump_read" in window and "will not open it" in window


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
    this server reaches directly, because `dump_save` carries no audience mark for
    the hub to route — was neither.  So a session that ran one result past the
    window left the next one calling `dump_save` on a dead client, and
    `compress_for` deliberately does not catch a failed write: every turn of that
    session with an oversized result failed at its first hop.

    Two sessions, one oversized result each: the cabinet is opened twice, and the
    one the first session opened is closed on the way out.
    """
    from slife2.libs.host.config import (
        CABINET_SERVER_NAME,
        CONTEXT_SERVER_NAME,
        TOOLHUB_SERVER_NAME,
    )
    from slife2.plugins.agent import server as server_module

    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    cfg = config_over(WINDOW)
    backend = FakeBackend(
        *(calling("fake__echo", {"text": HUGE}), answered(PICKED), answered("done")) * 2
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

    The discriminator is scripted ahead of it, and that is the *order* and not a
    detail: the model is asked what this output is before the row is written, so
    that the mime it answers with is on the row (`compress_for`).  A cabinet that
    refuses the write still fails the turn.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    cfg = config_over(WINDOW)
    backend = FakeBackend(calling("fake__echo", {"text": HUGE}), answered(PICKED))
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
async def test_a_window_that_cannot_be_read_back_fails_the_turn(
    tmp_path, monkeypatch
) -> None:
    """**The fallback is a read, not a licence to carry nothing.**

    The reader's own first window is the answer when the *discriminator* cannot
    be read — an ordinary thing for a model to produce — and it is a real read of
    the row this same process wrote a moment ago, on the side of the steps that
    must succeed (`compress_for`: "nothing else here falls back").  A cabinet
    whose save landed and whose read then failed has given up the output
    entirely, and a turn that swallowed that would enter the conversation with an
    empty tool result: the bound gone *and* the address gone, with nothing
    anywhere saying so — which is the failure that catch was hiding.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    cfg = config_over(WINDOW)
    backend = FakeBackend(
        calling("fake__echo", {"text": HUGE}),
        answered("I am not sure what to make of this."),
        answered("done"),
    )
    kept: dict[str, str] = {}
    half_broken = FastMCP("cabinet-half-broken")

    @half_broken.tool
    async def dump_save(call: str, output: str, tool: str = "", mime: str = "") -> dict:
        kept[call] = output
        return {"kept": call, "mirror": f"dumps/{call}.md", "chars": len(output)}

    @half_broken.tool
    async def dump_read(
        call: str, offset: int = 1, limit: int = 0, pattern: str = "", context: int = 0
    ) -> str:
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
        Client(half_broken) as cabinet_client,
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
