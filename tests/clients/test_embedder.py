"""The client half of the embeddings hop, and the one retry it owns.

A kept-alive connection may be closed by the server while it is idle; the next
request then dies with the MCP session, and the caller sees
`MCPError(CONNECTION_CLOSED)` (measured against mcp 2.3.0 / fastmcp 4.2.0) or
the session task's own `ReadError` when that task wins the race.  Repeating the
call on the same client cannot work, because the session is gone — the retry
reopens the connection and asks once.  These tests pin the boundary: the shapes
that count as a death, one reopen, a refusal that is not retried, and a second
failure reaching the caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from fastmcp import FastMCP
from mcp import MCPError
from mcp.types import CONNECTION_CLOSED, INTERNAL_ERROR

from slife2.clients.embedder import EmbedderConnection, RemoteEmbedder
from slife2.libs.host.config import EMBEDDINGS_SERVER_NAME
from slife2.libs.plugin.protocol import house_server

pytestmark = pytest.mark.unit

DESCRIBED = {
    "provider": "p",
    "model": "m",
    "base_url": "http://example.invalid/v1",
    "dimension": 2,
    "max_chars": 10,
    "rerank_model": "",
}


def closed() -> MCPError:
    """What a request on a dead MCP session raises."""
    return MCPError(code=CONNECTION_CLOSED, message="Connection closed")


class _Client:
    """A client that answers embedded vectors, or raises its scripted errors."""

    def __init__(self, errors: list[BaseException] | None = None) -> None:
        self.errors = list(errors or [])
        self.calls = 0

    async def call_tool(
        self, name: str, arguments: dict, *, timeout: float | None = None
    ) -> SimpleNamespace:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        if name == "describe":
            return SimpleNamespace(data=DESCRIBED)
        return SimpleNamespace(
            data={"vectors": [[1.0, 2.0] for _ in arguments["texts"]]}
        )


class _Reopen:
    """What `EmbedderConnection._reopen` does for a live connection: adopt."""

    def __init__(self, fresh: Any, described: dict[str, Any] | None = None) -> None:
        self.fresh = fresh
        self.described = DESCRIBED if described is None else described
        self.calls = 0
        self.dead: list[Any] = []

    async def __call__(self, embedder: RemoteEmbedder, dead: Any) -> None:
        self.calls += 1
        self.dead.append(dead)
        embedder._adopt(self.fresh, self.described)


@pytest.mark.asyncio
async def test_a_closed_session_is_reopened_and_the_call_repeated_once() -> None:
    old, fresh = _Client([closed()]), _Client()
    reopen = _Reopen(fresh)
    embedder = RemoteEmbedder(old, DESCRIBED, reopen=reopen)

    assert await embedder.embed(["a", "b"]) == [[1.0, 2.0], [1.0, 2.0]]
    assert (old.calls, reopen.calls, fresh.calls) == (1, 1, 1)
    assert reopen.dead == [old], "the reopen is told which client died"


@pytest.mark.asyncio
async def test_a_group_carrying_the_death_is_unwrapped() -> None:
    death = ExceptionGroup("transport", [ExceptionGroup("session", [closed()])])
    old, fresh = _Client([death]), _Client()
    reopen = _Reopen(fresh)
    embedder = RemoteEmbedder(old, DESCRIBED, reopen=reopen)

    assert await embedder.embed(["a"]) == [[1.0, 2.0]]
    assert reopen.calls == 1


@pytest.mark.asyncio
async def test_the_session_tasks_read_error_is_also_a_death() -> None:
    """The other branch of the race: `_await_with_session_monitoring` raises it."""
    old, fresh = _Client([httpx2.ReadError("")]), _Client()
    reopen = _Reopen(fresh)
    embedder = RemoteEmbedder(old, DESCRIBED, reopen=reopen)

    assert await embedder.embed(["a"]) == [[1.0, 2.0]]
    assert reopen.calls == 1


@pytest.mark.asyncio
async def test_a_refusal_is_not_retried() -> None:
    """The far end answered; asking again is not recovery."""
    refused = MCPError(code=INTERNAL_ERROR, message="the tool said no")
    old = _Client([refused])
    reopen = _Reopen(_Client())
    embedder = RemoteEmbedder(old, DESCRIBED, reopen=reopen)

    with pytest.raises(MCPError) as caught:
        await embedder.embed(["a"])
    assert caught.value is refused
    assert reopen.calls == 0


@pytest.mark.asyncio
async def test_a_cancel_that_rides_with_a_death_is_not_retried() -> None:
    """The caller asked this process to stop; a retry would outlive the ask."""
    both = BaseExceptionGroup("both", [asyncio.CancelledError(), closed()])
    old = _Client([both])
    reopen = _Reopen(_Client())
    embedder = RemoteEmbedder(old, DESCRIBED, reopen=reopen)

    with pytest.raises(BaseExceptionGroup):
        await embedder.embed(["a"])
    assert reopen.calls == 0


@pytest.mark.asyncio
async def test_a_second_death_reaches_the_caller() -> None:
    old, fresh = _Client([closed()]), _Client([closed()])
    reopen = _Reopen(fresh)
    embedder = RemoteEmbedder(old, DESCRIBED, reopen=reopen)

    with pytest.raises(MCPError):
        await embedder.embed(["a"])
    assert (old.calls, reopen.calls, fresh.calls) == (1, 1, 1)


@pytest.mark.asyncio
async def test_without_a_reopen_a_dead_connection_reaches_the_caller() -> None:
    """A caller-owned client (a test's double) has no connection to rebuild."""
    old = _Client([closed()])
    embedder = RemoteEmbedder(old, DESCRIBED)

    with pytest.raises(MCPError):
        await embedder.embed(["a"])
    assert old.calls == 1


@pytest.mark.asyncio
async def test_a_reconnect_that_serves_another_model_is_refused() -> None:
    old = _Client([closed()])
    reopen = _Reopen(_Client(), {**DESCRIBED, "model": "other"})
    embedder = RemoteEmbedder(old, DESCRIBED, reopen=reopen)

    with pytest.raises(RuntimeError, match="came back as"):
        await embedder.embed(["a"])


class _ServerClient:
    """A client double that records whether it was closed, and can die once."""

    def __init__(
        self, described: dict[str, Any], errors: list[BaseException] | None = None
    ) -> None:
        self.described = described
        self.errors = list(errors or [])
        self.closed = False

    async def call_tool(
        self, name: str, arguments: dict, *, timeout: float | None = None
    ) -> SimpleNamespace:
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(data=self.described)

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_a_refused_reconnect_closes_the_new_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that came back as another model leaves no socket behind."""
    connection = EmbedderConnection(SimpleNamespace())
    dead = _ServerClient(DESCRIBED, [closed()])
    fresh = _ServerClient({**DESCRIBED, "provider": "other"})
    embedder = RemoteEmbedder(dead, DESCRIBED, reopen=connection._reopen)
    connection._client = dead
    connection._embedder = embedder

    async def connect() -> tuple[Any, dict[str, Any]]:
        return fresh, fresh.described

    monkeypatch.setattr(connection, "_connect", connect)

    with pytest.raises(RuntimeError, match="came back as"):
        await embedder.embed(["a"])
    assert fresh.closed, "the refused connection must not leak"
    assert connection._client is dead, "the old connection is still the one held"


class _RerankClient:
    """An embeddings server that answers `rerank` with one canned payload."""

    def __init__(self, data: dict, errors: list[BaseException] | None = None) -> None:
        self.data = data
        self.errors = list(errors or [])
        self.calls = 0

    async def call_tool(
        self, name: str, arguments: dict, *, timeout: float | None = None
    ) -> SimpleNamespace:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(data=self.data)


@pytest.mark.asyncio
async def test_rerank_repeats_a_call_that_died() -> None:
    old = _RerankClient({"results": [{"index": 0, "relevance_score": 0.5}]}, [closed()])
    fresh = _RerankClient({"results": [{"index": 0, "relevance_score": 0.5}]})
    described = {**DESCRIBED, "rerank_model": "BAAI/bge-reranker-v2-m3"}
    reopen = _Reopen(fresh, described)
    embedder = RemoteEmbedder(old, described, reopen=reopen)

    assert await embedder.rerank("q", ["a"]) == [(0, 0.5)]
    assert (old.calls, reopen.calls, fresh.calls) == (1, 1, 1)


@pytest.mark.asyncio
async def test_rerank_answers_none_when_the_endpoint_has_no_cross_encoder() -> None:
    """A capability, not a failure: the caller keeps its fused order."""
    client = _RerankClient({"results": []})
    embedder = RemoteEmbedder(client, DESCRIBED)

    assert await embedder.rerank("q", ["a"]) is None
    assert client.calls == 0, "nothing was asked of an endpoint with no reranker"


@pytest.mark.asyncio
async def test_rerank_maps_indices_back_and_sorts() -> None:
    client = _RerankClient(
        {
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.2},
            ]
        }
    )
    embedder = RemoteEmbedder(
        client, {**DESCRIBED, "rerank_model": "BAAI/bge-reranker-v2-m3"}
    )

    assert await embedder.rerank("q", ["a", "b"]) == [(1, 0.9), (0, 0.2)]


@pytest.mark.asyncio
async def test_a_short_rerank_answer_is_refused() -> None:
    """A missing score would read as "the ranker rejected that turn"."""
    client = _RerankClient({"results": [{"index": 0, "relevance_score": 0.5}]})
    embedder = RemoteEmbedder(
        client, {**DESCRIBED, "rerank_model": "BAAI/bge-reranker-v2-m3"}
    )

    with pytest.raises(RuntimeError, match="answered 1 rerank scores"):
        await embedder.rerank("q", ["a", "b"])


@pytest.mark.asyncio
async def test_a_rerank_index_that_was_not_sent_is_refused() -> None:
    """A score mapped onto the wrong document is a silent mismatch."""
    client = _RerankClient({"results": [{"index": 9, "relevance_score": 0.5}]})
    embedder = RemoteEmbedder(
        client, {**DESCRIBED, "rerank_model": "BAAI/bge-reranker-v2-m3"}
    )

    with pytest.raises(RuntimeError, match="document 9"):
        await embedder.rerank("q", ["a", "b"])


class _Config:
    """What `EmbedderConnection` reads from a config: one URL."""

    def __init__(self, url: str) -> None:
        self._url = url

    def server(self, key: str) -> SimpleNamespace:
        assert key == "embeddings"
        return SimpleNamespace(url=self._url)


@contextlib.asynccontextmanager
async def _over_http(server: FastMCP) -> AsyncGenerator[str]:
    """Serve a built server on a real port, and yield its URL."""
    import uvicorn

    app = server.http_app(path="/mcp", json_response=False, stateless_http=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    serve_task = asyncio.create_task(uv.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if uv.started:
                break
            await asyncio.sleep(0.05)
        assert uv.started, "the server did not start"
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uv.should_exit = True
        await asyncio.wait_for(serve_task, timeout=10)
        sock.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_read_error_on_the_wire_is_recovered_over_http(
    monkeypatch: pytest.MonkeyPatch, real_dialer: Any
) -> None:
    """The real stack's shape, through a real server and a real MCP session.

    A request on a connection the server closed dies inside the transport and
    takes the whole session with it (measured: `MCPError(CONNECTION_CLOSED)`),
    so a retry that merely repeated the call would keep failing.  This forces
    that read at the transport's own boundary and pins the recovery: the
    connection is reopened, the embedding is answered, and the session that
    replaced it serves the next call too.
    """
    import mcp.client.streamable_http as shttp

    # The dial is this test's subject, so the suite's refusal is lifted for the
    # module that makes it (tests/conftest.py::real_dialer).
    real_dialer("slife2.clients.embedder")

    real = shttp.stream_within_origin
    armed = {"on": False}

    @contextlib.asynccontextmanager
    async def flaky(*args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        if armed["on"]:
            armed["on"] = False
            raise httpx2.ReadError("")
        async with real(*args, **kwargs) as response:
            yield response

    monkeypatch.setattr(shttp, "stream_within_origin", flaky)

    server: FastMCP = house_server(EMBEDDINGS_SERVER_NAME)

    @server.tool
    def describe() -> dict[str, Any]:
        return DESCRIBED

    @server.tool
    def embed(texts: list[str]) -> dict[str, Any]:
        return {"vectors": [[1.0, 2.0] for _ in texts]}

    async with _over_http(server) as url:
        connection = EmbedderConnection(_Config(url))
        try:
            embedder = await connection.get()
            assert await embedder.embed(["a"]) == [[1.0, 2.0]]
            original = connection._client
            armed["on"] = True
            assert await embedder.embed(["b"]) == [[1.0, 2.0]], "reopened and repeated"
            assert connection._client is not original, "the dead session was replaced"
            assert await embedder.embed(["c"]) == [[1.0, 2.0]], "the new session lasts"
        finally:
            await connection.close()
