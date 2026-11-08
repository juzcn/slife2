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


def _client(
    model: str, fake: _FakeOpenAI, *, rerank_model: str = ""
) -> EmbeddingClient:
    client = EmbeddingClient(
        EmbeddingProviderSettings(
            name="test",
            base_url="http://example.invalid/v1",
            model=model,
            api_key_ref="k",
            rerank_model=rerank_model,
        )
    )
    client._client = fake
    return client


class _FakeResponse:
    """Enough of an `httpx2.Response` for the rerank call."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return self.payload


@dataclass
class _FakeHTTP:
    """Enough of `httpx2.AsyncClient` for the rerank call."""

    payload: Any
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
        self.requests.append((url, json))
        return _FakeResponse(self.payload)


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

    An assumed 8192-token limit is wrong for any model whose window differs, and
    the cost is not local: a chunk past the real limit is rejected, and the turn
    that sent it stays unindexed for ever.  The width can be measured; an input
    limit cannot be measured without sending the oversized input, so an endpoint
    that reports neither is refused rather than guessed (`SDD Appendix C).
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
    (`SDD Appendix C).
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


# --- the cross-encoder -------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_posts_the_model_query_and_documents() -> None:
    client = _client(
        "BAAI/bge-m3", _FakeOpenAI(), rerank_model="BAAI/bge-reranker-v2-m3"
    )
    http = _FakeHTTP(
        {
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.2},
            ]
        }
    )
    client._http = http

    scored = await client.rerank("the query", ["first", "second"], top_n=2)

    assert scored == [(1, 0.9), (0, 0.2)]
    url, body = http.requests[0]
    assert url == "http://example.invalid/v1/rerank"
    assert body == {
        "model": "BAAI/bge-reranker-v2-m3",
        "query": "the query",
        "documents": ["first", "second"],
        "return_documents": False,
        "top_n": 2,
    }


@pytest.mark.asyncio
async def test_rerank_without_a_rerank_model_is_refused() -> None:
    """A capability the config did not choose is named, not guessed."""
    with pytest.raises(RuntimeError, match="rerank_model"):
        await _client("bge-m3", _FakeOpenAI()).rerank("q", ["a"])


@pytest.mark.asyncio
async def test_a_rerank_index_out_of_range_is_refused() -> None:
    """An index is how a score maps back to a document; a wrong one is silent."""
    client = _client(
        "BAAI/bge-m3", _FakeOpenAI(), rerank_model="BAAI/bge-reranker-v2-m3"
    )
    client._http = _FakeHTTP({"results": [{"index": 9, "relevance_score": 0.5}]})
    with pytest.raises(RuntimeError, match="document 9"):
        await client.rerank("q", ["a", "b"])


@pytest.mark.asyncio
async def test_a_short_rerank_answer_is_refused() -> None:
    """An unscored document is not one the ranker judged worthless.

    The caller drops what it was not told a score for, so accepting a short
    list turns a protocol failure into a silent filter.
    """
    client = _client(
        "BAAI/bge-m3", _FakeOpenAI(), rerank_model="BAAI/bge-reranker-v2-m3"
    )
    client._http = _FakeHTTP({"results": [{"index": 0, "relevance_score": 0.5}]})

    with pytest.raises(RuntimeError, match="answered 1 scores for 2 documents"):
        await client.rerank("q", ["a", "b"])


@pytest.mark.asyncio
async def test_a_top_n_answer_is_not_a_short_answer() -> None:
    """`top_n` is how many the caller asked for; fewer than that is the fault."""
    client = _client(
        "BAAI/bge-m3", _FakeOpenAI(), rerank_model="BAAI/bge-reranker-v2-m3"
    )
    client._http = _FakeHTTP({"results": [{"index": 1, "relevance_score": 0.9}]})

    assert await client.rerank("q", ["a", "b"], top_n=1) == [(1, 0.9)]


@pytest.mark.asyncio
async def test_describe_reports_the_rerank_model() -> None:
    described = await _client(
        "bge-m3", _FakeOpenAI(), rerank_model="BAAI/bge-reranker-v2-m3"
    ).describe()
    assert described["rerank_model"] == "BAAI/bge-reranker-v2-m3"

    none = await _client("bge-m3", _FakeOpenAI()).describe()
    assert none["rerank_model"] == "", "no reranker is a capability answer"


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
    assert offered["rerank"] is False
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


@pytest.mark.asyncio
async def test_rerank_answers_over_the_wire() -> None:
    client = _client(
        "BAAI/bge-m3", _FakeOpenAI(), rerank_model="BAAI/bge-reranker-v2-m3"
    )
    client._http = _FakeHTTP({"results": [{"index": 0, "relevance_score": 0.7}]})
    server = build_server(default_config(), client=client)

    async with Client(server) as wire:
        answer = tool_payload(
            await wire.call_tool("rerank", {"query": "q", "documents": ["a"]})
        )

    assert answer["results"] == [{"index": 0, "relevance_score": 0.7}]


# --- the index identity the client half records -------------------------------


def test_a_server_that_cannot_describe_its_endpoint_is_refused() -> None:
    """**An empty slot in the identity makes a change invisible.**

    `RemoteEmbedder.identity` is the lever that turns a changed model or a
    repointed `base_url` into a rebuild (`SDD` §5.5), and it is built from
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
        "rerank_model": "",
    }
    assert RemoteEmbedder(None, complete).identity == ("p|m|http://example.invalid/v1")

    for missing in ("provider", "model", "base_url"):
        described = {k: v for k, v in complete.items() if k != missing}
        with pytest.raises(RuntimeError, match="provider, a model or a base_url"):
            RemoteEmbedder(None, described)

    without_rerank = {k: v for k, v in complete.items() if k != "rerank_model"}
    with pytest.raises(RuntimeError, match="rerank_model"):
        RemoteEmbedder(None, without_rerank)


@pytest.mark.asyncio
async def test_embed_answers_with_the_vectors_over_the_wire() -> None:
    fake = _FakeOpenAI(dimension=2)
    server = build_server(default_config(), client=_client("mystery", fake))

    async with Client(server) as client:
        result = await client.call_tool("embed", {"texts": ["a", "b"]})

    assert tool_payload(result)["vectors"] == [[1.0, 0.0], [2.0, 0.0]]


def _unreachable() -> Any:
    """The SDK's connect failure: nothing was sent, so nothing was billed."""
    import httpx2
    from openai import APIConnectionError

    return APIConnectionError(
        request=httpx2.Request("POST", "http://example.invalid/v1/embeddings")
    )


@pytest.mark.asyncio
async def test_a_connection_that_never_opened_is_tried_once_more() -> None:
    """**The one embedding failure the server repeats**, because it is not one.

    A `ConnectError` means the request never reached the provider: the repeat
    cannot double-charge, and embedding is idempotent.  On a cold start this is
    the difference between a catalogue build and a stack that will not start.
    """
    fake = _FakeOpenAI(dimension=2)
    answer = fake.embeddings.create
    attempts: list[list[str]] = []

    async def flaky(*, model: str, input: list[str]) -> Any:
        attempts.append(list(input))
        if len(attempts) == 1:
            raise _unreachable()
        return await answer(model=model, input=input)

    fake.embeddings.create = flaky
    server = build_server(default_config(), client=_client("mystery", fake))

    async with Client(server) as client:
        result = await client.call_tool("embed", {"texts": ["a"]})

    assert tool_payload(result)["vectors"] == [[1.0, 0.0]]
    assert attempts == [["a"], ["a"]], "the retry asked with exactly that request"


@pytest.mark.asyncio
async def test_a_connection_that_keeps_failing_is_not_retried_for_ever() -> None:
    """One retry, and then the caller hears the failure — no loop, no silence."""
    from fastmcp.exceptions import ToolError

    fake = _FakeOpenAI(dimension=2)
    attempts: list[list[str]] = []

    async def unreachable(*, model: str, input: list[str]) -> Any:
        attempts.append(list(input))
        raise _unreachable()

    fake.embeddings.create = unreachable
    server = build_server(default_config(), client=_client("mystery", fake))

    async with Client(server) as client:
        with pytest.raises(ToolError, match="Connection error"):
            await client.call_tool("embed", {"texts": ["a"]})

    assert attempts == [["a"], ["a"]], "exactly the one retry, and no more"


@pytest.mark.asyncio
async def test_a_timeout_is_not_retried() -> None:
    """**A request that was sent may have been billed.**

    `APITimeoutError` derives from `APIConnectionError`, so the connect-failure
    retry caught the one failure that must never be repeated: the same
    embeddings request went to the provider a second time.  `ConnectTimeout`
    is mapped to the same type, so even a timeout before the request was sent
    is given the conservative answer.
    """
    import httpx2
    from fastmcp.exceptions import ToolError
    from openai import APITimeoutError

    fake = _FakeOpenAI(dimension=2)
    attempts: list[list[str]] = []

    async def timed_out(*, model: str, input: list[str]) -> Any:
        attempts.append(list(input))
        raise APITimeoutError(
            request=httpx2.Request("POST", "http://example.invalid/v1/embeddings")
        )

    fake.embeddings.create = timed_out
    server = build_server(default_config(), client=_client("mystery", fake))

    async with Client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool("embed", {"texts": ["a"]})

    assert attempts == [["a"]], "a timed-out request is not repeated"


@pytest.mark.asyncio
async def test_a_provider_refusal_is_a_sentence_not_a_traceback() -> None:
    """A 402 is operational news, and the sentence names the setting that moves it.

    The SDK's own `str()` is the whole response body; a person starting the
    stack needs the provider's message and the one config line to change, and
    the tool boundary is where that is turned into a `ToolError` — a refusal,
    not a traceback.
    """
    import httpx2
    from fastmcp.exceptions import ToolError
    from openai import APIStatusError

    refused = APIStatusError(
        "Error code: 402",
        response=httpx2.Response(
            402,
            request=httpx2.Request("POST", "http://example.invalid/v1/embeddings"),
        ),
        body={
            "code": 30001,
            "message": "Sorry, your account balance is insufficient",
        },
    )

    class Refusing(_FakeOpenAI):
        async def _create(self, *, model: str, input: list[str]) -> Any:
            raise refused

    server = build_server(default_config(), client=_client("mystery", Refusing()))
    async with Client(server) as client:
        with pytest.raises(
            ToolError, match="account balance is insufficient"
        ) as caught:
            await client.call_tool("embed", {"texts": ["a"]})

    assert "402" in str(caught.value)
    assert "embeddings.active_model" in str(caught.value)


def test_each_refusal_status_gets_its_own_action() -> None:
    """402 is the bill, 401 the key, 403 the permission.

    A free model does not make 402 a rate-limit answer — a free model is still
    gated on the account not being in arrears — and 403 is the provider's usual
    answer to a model that needs real-name verification.
    """
    import httpx2
    from openai import APIStatusError

    from slife2.plugins.llm.embeddings_server import provider_refusal

    def refusal(status: int) -> APIStatusError:
        return APIStatusError(
            f"Error code: {status}",
            response=httpx2.Response(
                status,
                request=httpx2.Request("POST", "http://example.invalid/v1/embeddings"),
            ),
            body={"message": "the provider's sentence"},
        )

    assert "in arrears" in provider_refusal(refusal(402))
    assert "api_key" in provider_refusal(refusal(401))
    assert "real-name" in provider_refusal(refusal(403))
