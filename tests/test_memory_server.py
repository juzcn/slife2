"""slife2-memory over the wire.

The tools are thin — a store call hopped off the event loop — and that thinness
is exactly what a payload mismatch hides behind.  FastMCP validates the
arguments it is handed, so the names the agent server sends and the names the
tool accepts have to be checked against each other rather than assumed; when
they drift, the failure is a refused request that the agent server logs as a
memory problem and swallows.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from slife2.config import default_config
from slife2.memory_server import build_server
from slife2.paths import DATA_ENV_VAR

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_a_turn_goes_in_and_comes_back(tmp_path, monkeypatch) -> None:
    """The write and the read are one contract, so they are tested as one."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    messages = [{"role": "assistant", "content": "It is 42."}]

    async with Client(build_server(default_config())) as client:
        stored = await client.call_tool(
            "remember",
            {
                "agent": "jack",
                "user_message": "what is 2+2?",
                "messages": messages,
                "token_count": 245,
                "context_tokens": 135,
                "who_helped": "jack",
                "what_model": "deepseek/deepseek-flash",
            },
        )
        read = await client.call_tool("recent", {"agent": "jack", "limit": 5})

    assert stored.data["turn_id"] == 1
    (record,) = read.data
    assert record["turn_id"] == 1
    assert record["user_message"] == "what is 2+2?"
    assert record["messages"] == messages
    assert record["who_helped"] == "jack"
    assert record["what_model"] == "deepseek/deepseek-flash"
    assert (record["token_count"], record["context_tokens"]) == (245, 135)
    assert record["created_at"] and record["completed_at"]


@pytest.mark.asyncio
async def test_an_agent_name_that_cannot_be_a_file_is_refused(
    tmp_path, monkeypatch
) -> None:
    """The one refusal the agent server tells apart from a dead transport.

    `server.py` reads a `ToolError` as "this one request, from this one caller"
    and keeps talking to the memory server; anything else it reads as the
    transport being gone.  That distinction only holds if a bad name really does
    come back as a `ToolError` rather than as a dropped connection.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config())) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "remember",
                {"agent": "..", "user_message": "hello", "messages": []},
            )
