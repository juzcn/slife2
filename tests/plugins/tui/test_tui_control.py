"""The window as an inbox: somebody handing it a message to run.

It is *two* tools where it used to be one, and that is the whole of what changed:
a window **attaches** and is pushed to, a channel **submits**.  The point of the
split is that the address is now shared — one plugin, a session per agent — so a
call can say which window it means, where the address alone could not.

So these are small tests of a small contract, plus the ones that need a socket: a
surface nobody can dial is not a surface, and a session nobody can end is not a
session.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fakes import FakeAgentClient
from fastmcp import Client
from fastmcp.exceptions import ToolError

from slife2.libs.host.config import TUI_SERVER_NAME
from slife2.libs.plugin.audience import for_the_model
from slife2.libs.turn.events import TextDelta, TurnFinished
from slife2.libs.turn.messages import Usage
from slife2.plugins.tui import server as control
from slife2.tui.app import SlifeApp
from slife2.tui.widgets import ChatView

SIZE = (100, 30)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def answering(text: str):
    """A scripted client whose one turn says *text*, one character at a time."""

    def respond(prompt, on_event):
        for char in text:
            on_event(TextDelta(char))
        on_event(
            TurnFinished(
                text=text,
                usage=Usage(),
                last_usage=Usage(),
                iterations=1,
                stop_reason="stop",
            )
        )
        return text

    return respond


async def until(
    predicate: Any,
    *,
    timeout: float = 5.0,  # timeouts: the value this test is built around, not a budget
) -> None:  # timeouts: the value this test is built around, not a budget
    """Wait for a background loop to get somewhere, or say it did not."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the background loop did not get there in time")


@contextlib.asynccontextmanager
async def attached(server: Any, agent: str = "jack") -> AsyncGenerator[list]:
    """A window holding its session open, and the hand-overs it was handed.

    The attach call is started as a task and *probed for* rather than slept on:
    the only thing that proves the server has the session is that a `submit`
    stops being refused, which is exactly the question every caller asks.
    """
    seen: list[control.Handover] = []

    async with Client(server) as window:

        async def on_progress(
            progress: float, total: float | None, message: str | None
        ) -> None:
            handover = control.decode(message or "")
            if handover is not None:
                seen.append(handover)

        task = asyncio.create_task(
            window.call_tool(
                control.ATTACH,
                {"agent": agent},
                progress_handler=on_progress,
                timeout=None,
            )
        )
        try:
            yield seen
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def submitted(client: Client, agent: str, **payload: Any) -> None:
    """Submit, retrying only while the window has not attached yet.

    A refusal is not a delivery, so retrying one cannot run the words twice —
    which is the property the retry is safe on, and the reason this helper is
    allowed to exist at all.
    """
    for _ in range(200):
        try:
            await client.call_tool(control.SUBMIT, {"agent": agent, **payload})
            return
        except ToolError:
            await asyncio.sleep(0.01)
    raise AssertionError(f"no window ever attached for {agent}")


async def noted(client: Client, agent: str, text: str) -> None:
    """Draw one line on the attached window, with `submitted`'s safe retry.

    Safe for the same reason and no further: a refusal proves nothing was
    written, so trying again cannot draw the line twice.  What no caller may do
    is retry a note whose write could not be *confirmed* — everything a note says
    is about a moment that has passed.
    """
    for _ in range(200):
        try:
            await client.call_tool(control.NOTE, {"agent": agent, "text": text})
            return
        except ToolError:
            await asyncio.sleep(0.01)
    raise AssertionError(f"no window ever attached for {agent}")


async def test_a_handed_over_message_reaches_the_attached_window() -> None:
    server = control.build_server()

    async with attached(server) as seen, Client(server) as channel:
        await submitted(channel, "jack", text="hi", channel="wechat")
        await until(lambda: seen)

    assert seen == [control.Handover(ticket=1, text="hi", channel="wechat")]


async def test_submit_answers_once_the_words_are_written() -> None:
    """The acknowledgement, measured: the window has them by the time it returns.

    This is what makes handing over safe to retry, and it is a fact about
    *ordering* rather than about the message arriving at some point afterwards —
    a `submit` that returned before the push would let a caller retry a message
    the window was still about to be handed.
    """
    server = control.build_server()

    async with attached(server) as seen, Client(server) as channel:
        await submitted(channel, "jack", text="hi", channel="wechat")

        # **No sleep, no `until`**: the assert is at the instant `submit` came
        # back, which is the whole claim.
        assert [handover.text for handover in seen] == ["hi"]


async def test_a_message_for_an_agent_nobody_is_showing_is_refused() -> None:
    server = control.build_server()

    async with Client(server) as channel:
        with pytest.raises(ToolError) as refused:
            await channel.call_tool(
                control.SUBMIT, {"agent": "nobody", "text": "hi", "channel": "wechat"}
            )

    # Named, because "no window is showing nobody's conversation" is the whole of
    # what a caller can act on: it is what says which terminal to open.
    assert "nobody" in str(refused.value)


async def test_two_agents_are_two_windows() -> None:
    """One plugin, one session per agent — the reason the address may be shared.

    Two windows attach, and a message addressed to one of them is handed to that
    one.  This is the property the old shape could not have at all: the address
    was one per machine, so the second window could not be reached.
    """
    server = control.build_server()

    async with attached(server, "jack") as jack, attached(server, "jill") as jill:
        async with Client(server) as channel:
            await submitted(channel, "jack", text="for jack", channel="wechat")
            await submitted(channel, "jill", text="for jill", channel="wechat")
            await until(lambda: jack and jill)

    assert [handover.text for handover in jack] == ["for jack"]
    assert [handover.text for handover in jill] == ["for jill"]


async def test_a_hand_over_is_either_written_or_withdrawn_and_never_both() -> None:
    """The race the acknowledgement rests on, decided where it is decided.

    Caller and push run in one event loop, and the ticket coming out of
    `pending` is what settles which of them owns the message.  Asserted here
    rather than through the timing that would otherwise be needed to reach it:
    the property is the tie-break itself, and it has exactly two orders.
    """
    session = control.WindowSession("jack")

    # The caller gives up first: the push wakes to nothing and writes nothing,
    # so a retry is a retry and not a second turn.
    handover, _ = session.post("hi", "wechat")
    assert session.abandon(handover.ticket) is True
    assert session.claim(handover.ticket) is None

    # The push gets there first: the words are already going, and the caller
    # cannot take them back — it is told the hand-over is not its to withdraw.
    handover, waiter = session.post("hi", "wechat")
    assert session.claim(handover.ticket) is waiter
    assert session.abandon(handover.ticket) is False


async def test_a_window_that_goes_away_answers_every_caller_still_waiting() -> None:
    """Nobody is left parked on a window that has stopped holding the call.

    The alternative — leaving these unresolved — is a `submit` that hangs until
    its caller's own timeout, which reads as a slow terminal rather than as a
    terminal that is gone.
    """
    session = control.WindowSession("jack")
    _, first = session.post("one", "wechat")
    _, second = session.post("two", "wechat")

    session.fail_pending("the window showing jack went away")

    for waiter in (first, second):
        with pytest.raises(ToolError, match="went away"):
            await waiter
    assert session.pending == {}


async def test_nothing_here_is_offered_to_the_model() -> None:
    """Three tools, and the hub must leave all of them out of the tool set.

    None carries the audience mark, and the plugin declares no sources — so it
    contributes no catalogue rows either, which is what makes it one of the
    plugins the hub has no reason to ask (`slife2.libs.plugin.protocol.LIST_SOURCES`).
    """
    server = control.build_server()

    async with Client(server) as client:
        tools = await client.list_tools()

    assert {tool.name for tool in tools} == {
        control.ATTACH,
        control.SUBMIT,
        control.NOTE,
    }
    assert not any(for_the_model(tool.meta) for tool in tools)


async def test_the_surface_answers_to_the_name_it_advertises() -> None:
    """`slife2.libs.plugin.protocol.open_server` proves who it reached before handing over.

    A record or an address is never evidence (DESIGN.md §1.4): what a caller trusts is the
    name the handshake reports, which is why every server here has one.
    """
    server = control.build_server()
    async with Client(server) as client:
        assert client.server_info is None or client.server_info.name == TUI_SERVER_NAME


async def test_a_handover_round_trips_and_foreign_lines_are_dropped() -> None:
    """One codec, tested once, for `slife2.libs.turn.events`' reason: the halves live
    beside each other so a single round trip covers the contract.

    And a line that is not ours reads as None rather than raising: it arrives on
    a display, and a display that dies on somebody else's notification is worse
    than one that ignores it.
    """
    handover = control.Handover(ticket=7, text="你好", channel="wechat")
    assert control.decode(control.encode(handover)) == handover
    # Non-ASCII survives, which is not free: it is what `ensure_ascii` is for.
    assert control.encode(handover).isascii()

    assert control.decode("") is None
    assert control.decode("not json") is None
    assert control.decode('{"t":"text","d":"hi"}') is None
    assert control.decode('{"t":"handover","d":"hi"}') is None


async def test_a_kind_this_build_has_no_word_for_is_refused_rather_than_run() -> None:
    """The kind is on the wire, and a hand-over may only be what it says it is.

    An absent or unknown `k` used to read as `TURN`, argued from the
    older-plugin direction.  The direction that costs something is the other
    one: a *newer* plugin's third kind would be executed as a conversation turn
    instead of drawn as a line, and a line run as a turn is a model call nobody
    asked for.  So an absent or unknown kind is refused like any other line this
    build has no word for — `decode` answers None rather than picking the more
    consequential branch.
    """
    turn = control.Handover(ticket=1, text="hi", channel="wechat")
    note = control.Handover(
        ticket=2, text="⚡ jill online", channel="", kind=control.NOTE
    )

    assert control.decode(control.encode(turn)) == turn
    assert control.decode(control.encode(note)) == note
    assert turn.kind == control.TURN, "the default, for every existing caller"

    # No `k` at all: refused, not run as the more consequential branch.
    assert control.decode('{"t":"handover","id":3,"d":"hi","c":"wechat"}') is None
    # A kind from a future build is not a kind this one may act on.
    assert control.decode('{"t":"handover","id":4,"d":"hi","c":"","k":"shrug"}') is None


async def test_a_note_reaches_the_window_as_a_note_and_is_acknowledged() -> None:
    """The `note` tool, down the same held call a `submit` uses.

    Same ticket, same acknowledgement, same refusal — one road with two kinds on
    it, which is what keeps a note in arrival order with the turns it landed
    between.  What it does *not* carry is a channel: a note is the harness
    talking, and there is no channel for that.
    """
    server = control.build_server()
    async with Client(server) as client:
        async with attached(server) as seen:
            await noted(client, "jack", "⚡ jill online")
            assert [h.kind for h in seen] == [control.NOTE]
            assert seen[0].text == "⚡ jill online"
            assert seen[0].channel == ""

        with pytest.raises(ToolError, match=control.NO_WINDOW):
            await client.call_tool(control.NOTE, {"agent": "nobody", "text": "hi"})


async def test_a_refusal_is_not_written_to_the_server_log_as_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Waiting for a window is a state, not a fault in the plugin.

    FastMCP logs every `ToolError` at its own level before answering, and the
    default is ERROR — so the readiness probe (a `note` until one is accepted)
    wrote `Error calling tool 'note'` every two seconds of every cold start.
    The caller is still refused in exactly the words it matches on; only the
    server's own copy of the same fact is demoted.
    """
    server = control.build_server()
    with caplog.at_level(logging.ERROR):
        async with Client(server) as client:
            with pytest.raises(ToolError, match=control.NO_WINDOW):
                await client.call_tool(control.NOTE, {"agent": "nobody", "text": "hi"})

    errors = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
    ]
    assert not any("Error calling tool" in message for message in errors), errors


@pytest.mark.integration
async def test_a_message_handed_to_the_window_runs_and_appears_as_a_turn() -> None:
    """End to end, over a real port: plugin → attach → window → queue → screen.

    This is the feature as a channel uses it, and the assertion that matters is
    the transcript's: the words are the *model's* (marker and all) and the line
    wears the channel they came in on, because the window ran them through the
    path a person's own typing goes through and nothing else.

    A real socket and the real client on both ends — the window uses its own
    `MCPWindowClient`, not a stand-in — because what is being shown is a
    *session*: a call this process holds open to another one.
    """
    client = FakeAgentClient(answering("你好"))
    mcp = control.build_server()

    async with served(mcp) as url:
        app = SlifeApp(
            "http://test/mcp",
            client_factory=lambda: client,
            agent="jack",
            control_url=url,
        )
        async with app.run_test(size=SIZE) as pilot:
            async with Client(url) as channel:
                await submitted(
                    channel,
                    "jack",
                    text='[Wechat:{"peer_wechat_id": "p1"}] hi',
                    channel="wechat",
                )
                for _ in range(40):
                    await pilot.pause()
            text = app.query_one(ChatView).plain_text()

    assert "Wechat> hi" in text
    assert "peer_wechat_id" not in text
    assert client.channels == ["wechat"]


@pytest.mark.integration
async def test_the_session_ends_when_the_window_stops_holding_it() -> None:
    """A window that goes away takes its session with it, with no `detach` sent.

    This is a property of the *wire* and cannot be shown in process: closing a
    response stream cancels the handler behind it, which is what makes the
    session self-cleaning.  The assertion is the honest one for it — the next
    caller is refused by name rather than handed to a window that has gone.
    """
    server = control.build_server()

    async with served(server) as url:
        window = Client(url)
        await window.__aenter__()
        task = asyncio.create_task(
            window.call_tool(
                control.ATTACH,
                {"agent": "jack"},
                progress_handler=lambda progress, total, message: asyncio.sleep(0),
                timeout=None,
            )
        )
        try:
            async with Client(url) as channel:
                await submitted(channel, "jack", text="hi", channel="wechat")

                # The window stops holding the call — closing the client ends the
                # stream, which is the only detach this system has.
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await window.__aexit__(None, None, None)

                for _ in range(400):
                    try:
                        await channel.call_tool(
                            control.SUBMIT,
                            {"agent": "jack", "text": "again", "channel": "wechat"},
                        )
                    except ToolError:
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("the session outlived the window")
        finally:
            with contextlib.suppress(Exception):
                await window.__aexit__(None, None, None)


@contextlib.asynccontextmanager
async def served(mcp: Any) -> AsyncGenerator[str]:
    """Serve a built server on a real port, and yield its URL.

    The tests own this rather than the plugin, because serving is no longer
    something the window does: `slife2.libs.plugin.protocol.serve` is how a plugin is
    served in production, and a unit test binding a socket would be exercising
    that rather than the window.
    """
    app = mcp.http_app(path="/mcp", json_response=False, stateless_http=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    import uvicorn

    uv = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    serve_task = asyncio.create_task(uv.serve(sockets=[sock]))
    try:
        await until(lambda: uv.started)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uv.should_exit = True
        await asyncio.wait_for(
            serve_task,
            timeout=5,  # timeouts: the value this test is built around, not a budget
        )  # timeouts: the value this test is built around, not a budget
        sock.close()


async def test_a_hand_over_with_an_unreadable_ticket_is_dropped_not_raised() -> None:
    """**The reader for the stream the window holds open.**

    `decode` answers `None` for a message it cannot read rather than raising, and
    the ticket was the one field it *cast* instead: `int("x")` and `int([1])`
    raise, and a raise inside the window's progress callback ends the delivery of
    every hand-over for that attachment — one malformed push from a peer on
    another build and the path is gone for the session.  `True` also read as
    ticket 1, which is a ticket somebody else's hand-over may hold.
    """
    for payload in (
        '{"t":"handover","id":"x","d":"hi","c":"wechat","k":"note"}',
        '{"t":"handover","id":[1],"d":"hi","c":"wechat","k":"note"}',
        '{"t":"handover","id":true,"d":"hi","c":"wechat","k":"note"}',
    ):
        assert control.decode(payload) is None

    good = control.decode('{"t":"handover","id":7,"d":"hi","c":"wechat","k":"note"}')
    assert good is not None and good.ticket == 7
