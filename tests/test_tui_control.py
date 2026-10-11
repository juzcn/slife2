"""The window's control surface: somebody handing it a message to run.

The whole of it is one tool, and the whole of *that* is "put these words in the
window's queue, and say so once they are there".  So these are small tests of a
small contract, plus one that binds a port — because a surface nobody can dial is
not a surface, and the address is the one thing about it that is not a call.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from fakes import FakeAgentClient
from fastmcp import Client

from slife2.config import TUI_SERVER_NAME
from slife2.events import TextDelta, TurnFinished
from slife2.messages import Usage
from slife2.tui import control
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
                steps=1,
                stop_reason="stop",
            )
        )
        return text

    return respond


def free_port() -> int:
    """A port nothing is listening on, which is all a test needs it to be."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def test_submit_hands_the_words_over_with_their_channel() -> None:
    delivered: list[tuple[str, str]] = []
    server = control.build_server(
        deliver=lambda text, channel: delivered.append((text, channel))
    )

    async with Client(server) as client:
        await client.call_tool("submit", {"text": "hi", "channel": "wechat"})

    # **Taken by the time the call returns** — which is what makes handing over
    # safe to retry: the caller knows the words are somebody else's problem, so a
    # dropped connection afterwards cannot run them twice.
    assert delivered == [("hi", "wechat")]


async def test_the_surface_answers_to_the_name_it_advertises() -> None:
    """`slife2.mcp_server.open_server` proves who it reached before handing over.

    A record or an address is never evidence (§4): what a caller trusts is the
    name the handshake reports, which is why every server here has one.
    """
    server = control.build_server(deliver=lambda text, channel: None)
    async with Client(server) as client:
        assert client.server_info is None or client.server_info.name == TUI_SERVER_NAME


@pytest.mark.integration
async def test_a_message_handed_to_the_window_runs_and_appears_as_a_turn() -> None:
    """End to end, over a real port: address → window → queue → turn → screen.

    This is the feature as a channel uses it, and the assertion that matters is
    the transcript's: the words are the *model's* (marker and all) and the line
    wears the channel they came in on, because the window ran them through the
    path a person's own typing goes through and nothing else.
    """
    client = FakeAgentClient(answering("你好"))
    url = f"http://127.0.0.1:{free_port()}/mcp"
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        agent="jack",
        control_url=url,
    )

    async with app.run_test(size=SIZE) as pilot:
        # The window serves as soon as it is mounted; a port takes a moment to
        # be dialable, so this is the probe every caller does too.
        delivered = False
        for _ in range(40):
            await pilot.pause()
            try:
                async with Client(url) as window:
                    await window.call_tool(
                        "submit",
                        {
                            "text": '[Wechat:{"peer_wechat_id": "p1"}] hi',
                            "channel": "wechat",
                        },
                    )
            except Exception:  # noqa: BLE001 - not up yet, which is the point
                await asyncio.sleep(0.05)
                continue
            delivered = True
            break

        for _ in range(40):
            await pilot.pause()

        assert delivered, "the window never answered on its address"
        text = app.query_one(ChatView).plain_text()

    assert "Wechat> hi" in text
    assert "peer_wechat_id" not in text
    assert client.channels == ["wechat"]
