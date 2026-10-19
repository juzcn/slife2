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

from slife2.libs.host.config import EmbeddingProviderSettings, default_config
from slife2.libs.plugin.audience import for_the_model
from slife2.libs.plugin.protocol import tool_payload
from slife2.plugins.llm.embeddings_server import EmbeddingClient, build_server

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
        dimension=999,
        listing=[SimpleNamespace(id="mystery", dimension=7, max_input_tokens=4096)],
    )
    described = await _client("mystery", fake).describe()

    assert described["dimension"] == 7
    assert described["max_chars"] == 4096, "the input limit came from the listing too"
    assert fake.requests == []


@pytest.mark.asyncio
async def test_an_unknown_width_is_measured() -> None:
    """Nothing left but to ask, with the cheapest question there is.

    The *width* is measured; the input limit is not, and is read from the
    listing — see `test_an_unknown_model_without_a_reported_input_limit_is_refused`.
    """
    fake = _FakeOpenAI(
        dimension=6, listing=[SimpleNamespace(id="mystery", max_input_tokens=4096)]
    )
    described = await _client("mystery", fake).describe()

    assert described["dimension"] == 6
    assert [texts for _, texts in fake.requests] == [["."]]


@pytest.mark.asyncio
async def test_a_listing_that_says_nothing_useful_falls_through_to_the_probe() -> None:
    """Most endpoints list model ids and no widths — the protocol has no field
    for one — so this is the ordinary path, not the exception."""
    fake = _FakeOpenAI(
        dimension=5, listing=[SimpleNamespace(id="mystery", max_input_tokens=4096)]
    )
    described = await _client("mystery", fake).describe()

    assert described["dimension"] == 5
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_an_unknown_model_without_a_reported_input_limit_is_refused() -> None:
    """**A model whose window is unknown must not select the degraded branch.**

    An unknown model used to be handed an assumed 8192-token limit, and the
    module's own history records what that arithmetic being wrong cost once: a
    chunk past the real limit was rejected and v1's indexer stalled on that turn
    for ever.  The width can be measured; an input limit cannot be measured
    without sending the oversized input, so an endpoint that reports neither is
    refused rather than guessed (`DESIGN.md` §11.1).
    """
    fake = _FakeOpenAI(dimension=3)  # no listing at all
    with pytest.raises(RuntimeError, match="input limit"):
        await _client("mystery", fake).describe()

    fake = _FakeOpenAI(dimension=3, listing=[SimpleNamespace(id="mystery")])
    with pytest.raises(RuntimeError, match="input limit"):
        await _client("mystery", fake).describe()


@pytest.mark.asyncio
async def test_a_listing_that_could_not_be_read_is_not_one_that_says_nothing() -> None:
    """**An unreadable answer is not a verdict.**

    A listing that could not be read was rendered as the same `0` as a listing
    that answered and reported no input limit, so "the endpoint did not answer"
    and "the endpoint does not say" became one refusal.  The width still falls
    through to the probe either way; the input limit names which happened
    (`DESIGN.md` §11.1).
    """
    unreadable = _FakeOpenAI(dimension=3)  # `models.list` raises
    with pytest.raises(RuntimeError, match="could not be read"):
        await _client("mystery", unreadable).describe()

    silent = _FakeOpenAI(dimension=3, listing=[SimpleNamespace(id="mystery")])
    with pytest.raises(RuntimeError, match="does not report one"):
        await _client("mystery", silent).describe()


@pytest.mark.asyncio
async def test_a_known_model_with_no_listing_at_all_still_works() -> None:
    """The common model costs no request at all, listing or not."""
    fake = _FakeOpenAI(dimension=3)
    described = await _client("bge-m3", fake).describe()

    assert described["dimension"] == 1024
    assert described["max_chars"] == 8192
    assert fake.requests == []


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
    from slife2.plugins.context.server import build_server as build_context
    from tests.fakes import StubEmbedder

    embeddings = build_server(default_config(), client=_client("bge-m3", _FakeOpenAI()))
    store = build_context(default_config(), embedder=StubEmbedder())

    async with Client(embeddings) as client:
        offered = {
            tool.name: for_the_model(getattr(tool, "meta", None))
            for tool in await client.list_tools()
        }
    async with Client(store) as client:
        offered |= {
            tool.name: for_the_model(getattr(tool, "meta", None))
            for tool in await client.list_tools()
        }

    assert offered["embed"] is False
    assert offered["describe"] is False
    assert offered["turn_list"] is True, "the marker did not survive the transport"


@pytest.mark.asyncio
async def test_describe_answers_over_the_wire() -> None:
    fake = _FakeOpenAI(
        dimension=8, listing=[SimpleNamespace(id="mystery", max_input_tokens=4096)]
    )
    server = build_server(default_config(), client=_client("mystery", fake))

    async with Client(server) as client:
        described = tool_payload(await client.call_tool("describe", {}))

    assert described["dimension"] == 8
    assert described["model"] == "mystery"
    # **The number the endpoint reported, not merely a positive one.**  The
    # ladder exists because the limit belongs to the endpoint
    # (`_input_limit_of`): a table guess here, the listing there, and this
    # fixture's listing says 4096 — so `> 0` passed for every rung of it,
    # including the ones that would be wrong.
    assert described["max_chars"] == 4096


# --- the index identity the client half records -------------------------------


def test_a_server_that_cannot_describe_its_endpoint_is_refused() -> None:
    """**An empty slot in the identity makes a change invisible.**

    `RemoteEmbedder.identity` is the lever that turns a changed model or a
    repointed `base_url` into a rebuild (`DESIGN.md` §2.4), and it is built from
    `provider`, `model` and `base_url`.  A description missing any of them used
    to contribute `""`, so the change could not be seen — the same argument the
    width and input limit are refused on two lines below.
    """
    from slife2.clients.embedder import RemoteEmbedder

    complete = {
        "provider": "p",
        "model": "m",
        "base_url": "http://example.invalid/v1",
        "dimension": 4,
        "max_chars": 4,
    }
    assert RemoteEmbedder(None, complete).identity == ("p|m|http://example.invalid/v1")

    for missing in ("provider", "model", "base_url"):
        described = {k: v for k, v in complete.items() if k != missing}
        with pytest.raises(RuntimeError, match="provider, a model or a base_url"):
            RemoteEmbedder(None, described)


@pytest.mark.asyncio
async def test_embed_answers_with_the_vectors_over_the_wire() -> None:
    fake = _FakeOpenAI(dimension=2)
    server = build_server(default_config(), client=_client("mystery", fake))

    async with Client(server) as client:
        result = await client.call_tool("embed", {"texts": ["a", "b"]})

    assert tool_payload(result)["vectors"] == [[1.0, 0.0], [2.0, 0.0]]
