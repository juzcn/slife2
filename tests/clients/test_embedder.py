"""The client half of the embeddings hop, and the one retry it owns.

A kept-alive connection may be closed by the server while it is idle; the next
request then fails while reading an answer that will never come.  The failure is
a `ReadError` with an empty message, and the client discards the connection, so
repeating the request is all recovery needs.  These two tests pin the boundary:
one retry, and a second failure reaches the caller.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx2
import pytest

from slife2.clients.embedder import RemoteEmbedder

pytestmark = pytest.mark.unit

DESCRIBED = {
    "provider": "p",
    "model": "m",
    "base_url": "http://example.invalid/v1",
    "dimension": 2,
    "max_chars": 10,
}


class _FlakyClient:
    """A client whose first *failures* calls lose the connection."""

    def __init__(self, failures: int) -> None:
        self._failures = failures
        self.calls = 0

    async def call_tool(self, name: str, arguments: dict) -> SimpleNamespace:
        self.calls += 1
        if self.calls <= self._failures:
            raise httpx2.ReadError("")
        return SimpleNamespace(
            data={"vectors": [[1.0, 2.0] for _ in arguments["texts"]]}
        )


@pytest.mark.asyncio
async def test_a_closed_keep_alive_is_retried_once() -> None:
    client = _FlakyClient(failures=1)
    embedder = RemoteEmbedder(client, DESCRIBED)

    assert await embedder.embed(["a", "b"]) == [[1.0, 2.0], [1.0, 2.0]]
    assert client.calls == 2


@pytest.mark.asyncio
async def test_a_second_read_error_reaches_the_caller() -> None:
    client = _FlakyClient(failures=2)
    embedder = RemoteEmbedder(client, DESCRIBED)

    with pytest.raises(httpx2.ReadError):
        await embedder.embed(["a"])
    assert client.calls == 2
