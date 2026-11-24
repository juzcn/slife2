"""The session name, as the four tools take it.

`conversation_key` does not normalise, so a name with stray whitespace used to
address a different session than the one `attach` registered: `attached`
stripped the name and the other three tools did not.  One rule at the wire —
all four take it stripped — driven here over the plugin's own surface on the
in-memory transport (`tests/plugins/tui/test_tui_control.py` holds the socket
half and the rest of the contract).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from slife2.libs.plugin.vocabulary import (
    ATTACH,
    ATTACHED,
    NOTE,
    SUBMIT,
    Handover,
    decode,
)
from slife2.plugins.tui import server as control

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@contextlib.asynccontextmanager
async def attached(server: Any, agent: str) -> AsyncGenerator[list[Handover]]:
    """A window holding its session open, and the hand-overs it was handed."""
    seen: list[Handover] = []

    async with Client(server) as window:

        async def on_progress(
            progress: float, total: float | None, message: str | None
        ) -> None:
            handover = decode(message or "")
            if handover is not None:
                seen.append(handover)

        task = asyncio.create_task(
            window.call_tool(
                ATTACH, {"agent": agent}, progress_handler=on_progress, timeout=None
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
    `tests/plugins/tui/test_tui_control.py`'s helper, for the same reason.
    """
    for _ in range(200):
        try:
            await client.call_tool(SUBMIT, {"agent": agent, **payload})
            return
        except ToolError:
            await asyncio.sleep(0.005)
    raise AssertionError(f"no window ever attached for {agent!r}")


async def test_the_four_tools_take_one_name_the_same_way() -> None:
    """A whitespace attach is the session the clean name asks about and writes to.

    The probe is the fix itself: `submitted` retries while the clean name is
    refused, and before the rule was one it was refused for ever — the session
    was filed under the stray spelling, and `attached` (the one tool that
    stripped) said nobody was there.  The note takes the other spelling, and
    both hand-overs land on the one stream.
    """
    server = control.build_server()
    async with attached(server, " jack ") as seen, Client(server) as channel:
        await submitted(channel, "jack", text="hi", channel="wechat")
        here = await channel.call_tool(ATTACHED, {"agent": "jack"})
        await channel.call_tool(NOTE, {"agent": " jack", "text": "⚡ jill online"})

    assert here.data == {"attached": True}
    assert [handover.text for handover in seen] == ["hi", "⚡ jill online"]


async def test_a_whitespace_name_still_asks_about_a_session_nobody_holds() -> None:
    """Stripping is one rule, not a wildcard: a name nobody attached is still
    nobody, whichever spelling asks."""
    server = control.build_server()
    async with Client(server) as channel:
        answer = await channel.call_tool(ATTACHED, {"agent": " nobody "})
    assert answer.data == {"attached": False}
