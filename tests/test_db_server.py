"""slife2-db over the wire.

The tools are thin — a store call hopped off the event loop — and that thinness
is exactly what a payload mismatch hides behind.  FastMCP validates the
arguments it is handed, so the names the agent server sends and the names the
tool accepts have to be checked against each other rather than assumed; when
they drift, the failure is a refused request that the agent server logs as a
db problem and swallows.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from slife2.audience import client_meta
from slife2.config import default_config
from slife2.db import TurnStore
from slife2.db_server import RemoteEmbedder, build_server
from slife2.paths import DATA_ENV_VAR, db_dir
from tests.fakes import StubEmbedder

pytestmark = pytest.mark.unit

#: The db server embeds every turn it stores, so driving it needs an embedder.
#: A stub rather than an endpoint: what these tests are about is the server's
#: own behaviour, and a real call would make them about somebody else's service
#: being up.
EMBEDDER = StubEmbedder()


@pytest.mark.asyncio
async def test_a_turn_goes_in_and_comes_back(tmp_path, monkeypatch) -> None:
    """The write and the read are one contract, so they are tested as one."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    messages = [
        {"role": "user", "content": "what is 2+2?"},
        {"role": "assistant", "content": "It is 42."},
    ]

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
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

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
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

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in ("turn_list", "turn_read"):
        assert "agent" not in tools[name].input_schema["properties"]
        assert "subagent" not in tools[name].input_schema["properties"]
    # And the one the agent side calls still takes it: `remember` is called by
    # code that knows which conversation it is writing for.
    assert "agent" in tools["remember"].input_schema["properties"]


@pytest.mark.asyncio
async def test_a_model_reads_the_history_it_is_calling_from(
    tmp_path, monkeypatch
) -> None:
    """Two conversations, one server, and neither sees the other's turns."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        for agent, question in (
            ("jack", "jack's question"),
            ("jill", "jill's question"),
        ):
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

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="did not say whose"):
            await client.call_tool("turn_list", {})


@pytest.mark.asyncio
async def test_browsing_pages_and_reports_a_total(tmp_path, monkeypatch) -> None:
    """What the model gets back for a page: four fields a turn and the count."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
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

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="invalid since bound"):
            await client.call_tool(
                "turn_list", {"since": "whenever"}, meta=client_meta("jack")
            )


@pytest.mark.asyncio
async def test_reading_a_turn_that_is_not_there_says_which_one(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="no turn 7"):
            await client.call_tool(
                "turn_read", {"turn_id": 7}, meta=client_meta("jack")
            )


# --- startup: the index is brought up to date with the model ------------------


@pytest.mark.asyncio
async def test_startup_leaves_a_current_index_alone(tmp_path, monkeypatch) -> None:
    """The ordinary start costs nothing: the identity matches, so no embedding.

    Worth asserting because it is the difference between a server that starts in
    milliseconds and one that re-embeds a history every time it is launched.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    store = TurnStore(db_dir() / "jack.turn.db")
    for said in ("工具", "trump"):
        await store.save_turn(
            messages=[{"role": "user", "content": said}], embedder=EMBEDDER
        )

    again = StubEmbedder()
    async with Client(build_server(default_config(), embedder=again)):
        pass

    assert again.calls == [], "a current index was rebuilt anyway"


@pytest.mark.asyncio
async def test_startup_reindexes_every_turn_when_the_model_changed(
    tmp_path, monkeypatch
) -> None:
    """The cost of changing the embedding model, paid once at start.

    Every turn has to be embedded again — vectors from two models cannot be
    ranked against each other — and the thing that decides it is the identity
    recorded in the file, not anything remembered across restarts.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    first = StubEmbedder()
    store = TurnStore(db_dir() / "jack.turn.db")
    for said in ("工具", "trump", "792"):
        await store.save_turn(
            messages=[{"role": "user", "content": said}], embedder=first
        )

    second = StubEmbedder(identity="stub:two")
    async with Client(build_server(default_config(), embedder=second)):
        pass

    assert sum(len(call) for call in second.calls) == 3
    assert store._turns_without_vectors() == []
    assert store.index_status(second)["ready"]
    with sqlite3.connect(store.path) as connection:
        recorded = connection.execute(
            "SELECT value FROM index_meta WHERE key = 'vector_identity'"
        ).fetchone()[0]
    assert recorded.startswith("1|stub:two|"), recorded


# --- the far side of the hop --------------------------------------------------


class _Peer:
    """A stand-in for the client to the embeddings server."""

    def __init__(self, **answers: object) -> None:
        self._answers = answers

    async def call_tool(self, name: str, arguments: dict | None = None):
        return SimpleNamespace(data=self._answers[name])


DESCRIBED = {
    "provider": "local",
    "model": "bge-m3",
    "base_url": "http://127.0.0.1:17347/v1",
    "dimension": 1024,
    "max_chars": 8192,
}


@pytest.mark.asyncio
async def test_the_identity_is_the_endpoint_the_model_and_nothing_else() -> None:
    """A repointed base_url counts as a different model.

    Two endpoints can serve one model id and mean different weights, and mixing
    their vectors in one table is a ranking nobody could explain afterwards.
    """
    embedder = RemoteEmbedder(_Peer(), DESCRIBED)

    assert embedder.identity == "local|bge-m3|http://127.0.0.1:17347/v1"
    assert embedder.dimension == 1024
    assert embedder.max_chars == 8192

    moved = RemoteEmbedder(_Peer(), {**DESCRIBED, "base_url": "http://elsewhere/v1"})
    assert moved.identity != embedder.identity


def test_a_description_without_a_width_is_refused() -> None:
    """No index can be built for it, and building one at the wrong width is the
    silent failure this whole path exists to avoid."""
    with pytest.raises(RuntimeError, match="width"):
        RemoteEmbedder(_Peer(), {**DESCRIBED, "dimension": 0})


@pytest.mark.asyncio
async def test_a_short_answer_is_refused() -> None:
    """Read as "these texts had nothing worth embedding", it would leave a hole
    in the index that nothing could see."""
    peer = _Peer(embed={"vectors": [[0.0, 1.0]]})
    with pytest.raises(RuntimeError, match="1 vectors for 2 texts"):
        await RemoteEmbedder(peer, DESCRIBED).embed(["a", "b"])


@pytest.mark.asyncio
async def test_the_vectors_come_back_as_floats() -> None:
    peer = _Peer(embed={"vectors": [[1, 2], [3, 4]]})
    assert await RemoteEmbedder(peer, DESCRIBED).embed(["a", "b"]) == [
        [1.0, 2.0],
        [3.0, 4.0],
    ]


@pytest.mark.asyncio
async def test_the_catalogue_is_a_capability_of_this_component(
    tmp_path, monkeypatch
) -> None:
    """The `tool_*` tools, and who may see them.

    They are the record's public API — the hub is the caller today, and a second
    caller is an ordinary thing rather than a surprise — so none of them carries
    the model's audience mark.  That is what keeps them out of the model's tool
    list without a second filter anywhere: the hub's own rule drops an unmarked
    component tool by itself.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed}
        assert {
            "tool_merge",
            "tool_source_state",
            "tool_injectable",
            "tool_evict",
            "tool_route",
            "tool_sources",
            "tool_search",
            "tool_set_load",
            "tool_touch",
        } <= names

        merged = await client.call_tool(
            "tool_merge",
            {
                "source": "builtins",
                "category": "component",
                "tools": [
                    {
                        "name": "builtins__calc",
                        "description": "Evaluate an arithmetic expression.",
                        "remote_name": "calc",
                        "schema": '{"e": "expression"}',
                    }
                ],
            },
        )
        assert merged.data["inserted"] == ["builtins__calc"]

        injected = await client.call_tool("tool_injectable", {"sources": ["builtins"]})
        assert [row["name"] for row in injected.data["tools"]] == ["builtins__calc"]

        found = await client.call_tool("tool_search", {"query": "arithmetic"})
        assert [row["name"] for row in found.data["results"]] == ["builtins__calc"]

        routed = await client.call_tool("tool_route", {"name": "builtins__calc"})
        assert routed.data["tool"]["remote_name"] == "calc"


@pytest.mark.asyncio
async def test_the_catalogue_is_indexed_before_anything_is_served(
    tmp_path, monkeypatch
) -> None:
    """A file whose vectors came from another model is put right on the way up.

    The same pass the turn files get: the identity is compared and a mismatch
    re-embeds every tool.  Afterwards the identity matches, so the next start is
    a read of one row and a query that returns nothing — which is why the first
    start pays and the rest do not.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    first = StubEmbedder()
    async with Client(build_server(default_config(), embedder=first)) as client:
        await client.call_tool(
            "tool_merge",
            {
                "source": "builtins",
                "category": "component",
                "tools": [
                    {
                        "name": "builtins__calc",
                        "description": "Evaluate an arithmetic expression.",
                        "remote_name": "calc",
                        "schema": "",
                    }
                ],
            },
        )
    assert first.calls, "the row was embedded when it was written"

    # A second process, with a different model: the index it left behind cannot
    # be searched by this one, so every tool is embedded again.
    second = StubEmbedder(identity="stub:two")
    async with Client(build_server(default_config(), embedder=second)) as client:
        found = await client.call_tool("tool_search", {"query": "arithmetic"})
        assert [row["name"] for row in found.data["results"]] == ["builtins__calc"]
    assert second.calls, "rebuilt for the model that is in use now"
