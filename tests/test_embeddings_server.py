"""The embeddings endpoint, and the one number that has to be right.

Almost everything here is about the **width**.  It is discovered rather than
configured because `vec0` fixes a table's width in its DDL and cannot alter it
afterwards — so a wrong answer does not fail loudly, it produces an index whose
every insert is rejected while the table itself looks fine.  The three ways it
can be learned, and the order they are tried in, are therefore the tests that
matter most; the rest is the hop the db makes through this server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client

from slife2.audience import for_the_model
from slife2.config import EmbeddingProviderSettings, default_config
from slife2.llm.embeddings_server import EmbeddingClient, build_server
from slife2.mcp_server import tool_payload

pytestmark = pytest.mark.unit


# --- a stand-in for the OpenAI SDK, at the two methods that are used ----------


@dataclass
class _FakeOpenAI:
    """Enough of `AsyncOpenAI` to answer `/embeddings` and `/models`."""

    dimension: int = 4
    listing: list[Any] | None = None
    requests: list[tuple[str, list[str]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.embeddings = SimpleNamespace(create=self._create)
        self.models = SimpleNamespace(list=self._list)

    async def _create(self, *, model: str, input: list[str]) -> Any:
        self.requests.append((model, list(input)))
        return SimpleNamespace(
            data=[
                SimpleNamespace(embedding=[float(index)] + [0.0] * (self.dimension - 1))
                for index, _ in enumerate(input, start=1)
            ]
        )

    async def _list(self) -> Any:
        if self.listing is None:
            raise RuntimeError("this endpoint serves no model listing")
        return SimpleNamespace(data=self.listing)


def _client(model: str, fake: _FakeOpenAI) -> EmbeddingClient:
    client = EmbeddingClient(
        EmbeddingProviderSettings(
            name="test",
            base_url="http://example.invalid/v1",
            model=model,
            api_key_ref="k",
        )
    )
    client._client = fake
    return client


# --- the width, three ways ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_known_model_needs_no_probe_request() -> None:
    """The common case costs nothing: the width is a fact about the model id."""
    fake = _FakeOpenAI(dimension=999)
    described = await _client("bge-m3", fake).describe()

    assert described["dimension"] == 1024
    assert described["max_chars"] == 8192
    assert fake.requests == [], "a known model should not be embedded to find out"


@pytest.mark.asyncio
async def test_a_width_the_endpoint_reports_is_taken_from_it() -> None:
    """For an endpoint serving several models behind one base_url."""
    fake = _FakeOpenAI(
        dimension=999, listing=[SimpleNamespace(id="mystery", dimension=7)]
    )
    described = await _client("mystery", fake).describe()

    assert described["dimension"] == 7
    assert fake.requests == []


@pytest.mark.asyncio
async def test_an_unknown_width_is_measured() -> None:
    """Nothing left but to ask, with the cheapest question there is."""
    fake = _FakeOpenAI(dimension=6)
    described = await _client("mystery", fake).describe()

    assert described["dimension"] == 6
    assert [texts for _, texts in fake.requests] == [["."]]


@pytest.mark.asyncio
async def test_a_listing_that_says_nothing_useful_falls_through_to_the_probe() -> None:
    """Most endpoints list model ids and no widths — the protocol has no field
    for one — so this is the ordinary path, not the exception."""
    fake = _FakeOpenAI(dimension=5, listing=[SimpleNamespace(id="mystery")])
    described = await _client("mystery", fake).describe()

    assert described["dimension"] == 5
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_an_endpoint_with_no_listing_at_all_still_works() -> None:
    fake = _FakeOpenAI(dimension=3)
    described = await _client("mystery", fake).describe()

    assert described["dimension"] == 3


# --- the call ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_text_goes_in_one_request_in_order() -> None:
    fake = _FakeOpenAI(dimension=2)
    vectors = await _client("mystery", fake).embed(["one", "two", "three"])

    assert len(fake.requests) == 1
    assert fake.requests[0][1] == ["one", "two", "three"]
    assert [vector[0] for vector in vectors] == [1.0, 2.0, 3.0]


@pytest.mark.asyncio
async def test_an_empty_list_asks_nothing() -> None:
    fake = _FakeOpenAI()
    assert await _client("mystery", fake).embed([]) == []
    assert fake.requests == []


@pytest.mark.asyncio
async def test_a_key_that_did_not_resolve_is_named() -> None:
    """Better than the 401 a placeholder bearer token produces, which says
    nothing about which `${VAR}` is missing."""
    client = EmbeddingClient(
        EmbeddingProviderSettings(
            name="test",
            base_url="http://example.invalid/v1",
            model="bge-m3",
            api_key_ref="${NOT_SET_ANYWHERE_12345}",
        )
    )
    with pytest.raises(RuntimeError, match="NOT_SET_ANYWHERE_12345"):
        await client.embed(["x"])


# --- the server, over the transport the db uses ------------------------------


@pytest.mark.asyncio
async def test_the_two_tools_are_not_offered_to_the_model() -> None:
    """Neither carries the audience marker, so the toolhub never lists them.

    Asserted beside the db's `turn_list`, which *does* carry it — without that
    half the test would pass just as well if the marker never survived the
    transport at all.
    """
    from slife2.db_server import build_server as build_db
    from tests.fakes import StubEmbedder

    embeddings = build_server(default_config(), client=_client("bge-m3", _FakeOpenAI()))
    db = build_db(default_config(), embedder=StubEmbedder())

    async with Client(embeddings) as client:
        offered = {
            tool.name: for_the_model(getattr(tool, "meta", None))
            for tool in await client.list_tools()
        }
    async with Client(db) as client:
        offered |= {
            tool.name: for_the_model(getattr(tool, "meta", None))
            for tool in await client.list_tools()
        }

    assert offered["embed"] is False
    assert offered["describe"] is False
    assert offered["turn_list"] is True, "the marker did not survive the transport"


@pytest.mark.asyncio
async def test_describe_answers_over_the_wire() -> None:
    fake = _FakeOpenAI(dimension=8)
    server = build_server(default_config(), client=_client("mystery", fake))

    async with Client(server) as client:
        described = tool_payload(await client.call_tool("describe", {}))

    assert described["dimension"] == 8
    assert described["model"] == "mystery"
    assert described["max_chars"] > 0


@pytest.mark.asyncio
async def test_embed_answers_with_the_vectors_over_the_wire() -> None:
    fake = _FakeOpenAI(dimension=2)
    server = build_server(default_config(), client=_client("mystery", fake))

    async with Client(server) as client:
        result = await client.call_tool("embed", {"texts": ["a", "b"]})

    assert tool_payload(result)["vectors"] == [[1.0, 0.0], [2.0, 0.0]]
