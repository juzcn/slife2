"""slife2-db over the wire.

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

from slife2.audience import client_meta
from slife2.config import default_config
from slife2.db_server import build_server
from slife2.paths import DATA_ENV_VAR

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_a_turn_goes_in_and_comes_back(tmp_path, monkeypatch) -> None:
    """The write and the read are one contract, so they are tested as one."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    messages = [
        {"role": "user", "content": "what is 2+2?"},
        {"role": "assistant", "content": "It is 42."},
    ]

    async with Client(build_server(default_config())) as client:
        stored = await client.call_tool(
            "remember",
            {
                "agent": "jack",
                "messages": messages,
                "token_count": 245,
                "context_tokens": 135,
                "who_helped": "jack",
                "what_model": "deepseek/deepseek-flash",
            },
        )
        read = await client.call_tool(
            "turn_read", {"turn_id": 1}, meta=client_meta("jack")
        )

    assert stored.data["turn_id"] == 1
    record = read.data
    assert record["turn_id"] == 1
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
    and keeps talking to the db server; anything else it reads as the
    transport being gone.  That distinction only holds if a bad name really does
    come back as a `ToolError` rather than as a dropped connection.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config())) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "remember",
                {"agent": "..", "messages": []},
            )


# --- the model's half, which reads one conversation and only one --------------


def _exchange(question: str, answer: str) -> list[dict]:
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ]


@pytest.mark.asyncio
async def test_the_model_tools_name_no_agent(tmp_path, monkeypatch) -> None:
    """The schema is the boundary, not the system prompt.

    A `agent` argument would make "read my history" into "read anybody's", with
    nothing between the model and somebody else's turns but a sentence it was
    told to obey.  The identity reaches the server on the call instead
    (`slife2.audience`), so there is no argument to get wrong — and this is the
    check that none comes back.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config())) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in ("turn_list", "turn_read"):
        assert "agent" not in tools[name].input_schema["properties"]
        assert "subagent" not in tools[name].input_schema["properties"]
    # And the one the agent side calls still takes it: `remember` is called by
    # code that knows which conversation it is writing for.
    assert "agent" in tools["remember"].input_schema["properties"]


@pytest.mark.asyncio
async def test_a_model_reads_the_history_it_is_calling_from(tmp_path, monkeypatch) -> None:
    """Two conversations, one server, and neither sees the other's turns."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config())) as client:
        for agent, question in (("jack", "jack's question"), ("jill", "jill's question")):
            await client.call_tool(
                "remember", {"agent": agent, "messages": _exchange(question, "…")}
            )

        # A third id, on the same server: `subagent` is part of the identity
        # rather than a label on it, and it names a file of its own.
        await client.call_tool(
            "remember",
            {
                "agent": "jill",
                "subagent": "worker",
                "messages": _exchange("the worker's question", "…"),
            },
        )

        jack = await client.call_tool("turn_list", {}, meta=client_meta("jack"))
        jill = await client.call_tool("turn_list", {}, meta=client_meta("jill"))
        # Turn 1 exists in all three files, and is a different turn in each.
        read = await client.call_tool(
            "turn_read", {"turn_id": 1}, meta=client_meta("jack")
        )
        scoped = await client.call_tool(
            "turn_read", {"turn_id": 1}, meta=client_meta("jill", "worker")
        )

    assert [e["user_message"] for e in jack.data["entries"]] == ["jack's question"]
    assert [e["user_message"] for e in jill.data["entries"]] == ["jill's question"]
    assert read.data["messages"][0]["content"] == "jack's question"
    assert scoped.data["messages"][0]["content"] == "the worker's question"


@pytest.mark.asyncio
async def test_a_call_that_says_nobody_is_refused(tmp_path, monkeypatch) -> None:
    """Better told than quietly served: the hub forwards an identity it was
    given, so a call arriving without one did not come through the hub."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config())) as client:
        with pytest.raises(ToolError, match="did not say whose"):
            await client.call_tool("turn_list", {})


@pytest.mark.asyncio
async def test_browsing_pages_and_reports_a_total(tmp_path, monkeypatch) -> None:
    """What the model gets back for a page: four fields a turn and the count."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config())) as client:
        for number in range(3):
            await client.call_tool(
                "remember",
                {"agent": "jack", "messages": _exchange(f"q{number}", f"a{number}")},
            )

        page = await client.call_tool(
            "turn_list", {"limit": 2, "offset": 1}, meta=client_meta("jack")
        )

    assert page.data["total"] == 3
    assert (page.data["limit"], page.data["offset"]) == (2, 1)
    assert [e["user_message"] for e in page.data["entries"]] == ["q1", "q0"]
    assert [e["assistant_message"] for e in page.data["entries"]] == ["a1", "a0"]


@pytest.mark.asyncio
async def test_a_bound_the_grammar_does_not_know_is_a_refusal(
    tmp_path, monkeypatch
) -> None:
    """A `ToolError` the model reads and corrects, not an empty page."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config())) as client:
        with pytest.raises(ToolError, match="invalid since bound"):
            await client.call_tool(
                "turn_list", {"since": "whenever"}, meta=client_meta("jack")
            )


@pytest.mark.asyncio
async def test_reading_a_turn_that_is_not_there_says_which_one(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config())) as client:
        with pytest.raises(ToolError, match="no turn 7"):
            await client.call_tool("turn_read", {"turn_id": 7}, meta=client_meta("jack"))
