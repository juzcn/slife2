"""slife2-llm-embeddings — the one process that calls an embeddings endpoint.

**A process of its own, like every other backend here, and for the same
reason**: the provider SDK is imported only inside a server process, so the db,
the agent loop and the TUI stay free of `openai`.  It is not one of the chat
backends because it has nothing to do with them — an embedding endpoint is
reached at `{base_url}/embeddings`, speaks no chat protocol, and is configured
under its own `embeddings:` section rather than under `providers:`.  A chat
server built from the chat providers has no chat provider to attach it to.

**Two tools, neither of them the model's.**  `embed` and `describe` carry no
`meta=FOR_THE_MODEL`, so the toolhub never offers them to a model — the same
standing as the db's `remember`.  They are the db plugin's plumbing: the
index needs vectors and needs to know how wide they are, and this is where both
questions are answered.

**The width is discovered and never configured.**  `vec0` fixes a table's width
in its DDL and cannot be altered afterwards, so the number has to be right
before the first table exists — and a wrong one is not a visible failure but a
silent one: inserts of a different length are rejected one by one while a
correct-looking index stays empty.  Three ways to learn it, tried in order:
a table of known models, the endpoint's own model listing, and finally a probe
embed of one character, whose length *is* the answer.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from slife2.config import (
    Config,
    EmbeddingProviderSettings,
    find_config_path,
    load,
)
from slife2.llm.server_common import unresolved_key
from slife2.mcp_server import (
    configure_logging,
    house_server,
    parse_serve_args,
    serve,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-llm-embeddings"

#: This server's key in the config's `servers:` table.
CONFIG_KEY = "embeddings"

#: How long one embeddings request may take.  Generous, because a local
#: embedding service loads its weights on the first call and that is not a
#: failure — but a deadline all the same, because the db awaits this call on its
#: save path and an endpoint that never answers must not become a turn that
#: never finishes.
EMBED_TIMEOUT_SECONDS = 30.0

#: Retries are off.  The SDK's default is a long exponential backoff, which
#: turns an endpoint that is simply down into a call that hangs for minutes —
#: measured on v1, where the fix was this line.  The db's startup sync is what
#: retries, by running again, and it can say which provider it could not reach.
EMBED_MAX_RETRIES = 0

#: Widths and input limits of models whose answers are known, so the common case
#: costs no probe request.  Both numbers are per model, not per provider: the
#: same weights served by a local daemon and by a hosted API embed identically,
#: which is what makes this table a fact about the model id.
_KNOWN_MODELS: dict[str, tuple[int, int]] = {
    # model id: (dimension, max input tokens)
    "bge-m3": (1024, 8192),
    "BAAI/bge-m3": (1024, 8192),
    "text-embedding-3-small": (1536, 8191),
    "text-embedding-3-large": (3072, 8191),
    "text-embedding-ada-002": (1536, 8191),
    "nomic-embed-text": (768, 8192),
}

#: The input limit assumed for a model this module does not know.
_DEFAULT_MAX_TOKENS = 8192

#: Characters per token, for turning that limit into a character budget.  One,
#: deliberately: an earlier estimate of about four let a thirty-thousand
#: character line of escaped JSON ride as a single chunk, and the model rejected
#: it — after which v1's indexer stalled on that turn forever.  Overestimating
#: the budget costs a shorter chunk; underestimating it costs the turn.
_CHARS_PER_TOKEN = 1


class EmbeddingClient:
    """One OpenAI-compatible embeddings endpoint, and its discoverable width.

    The SDK client is created on first use, not in the constructor, for the
    reason `slife2.llm.openai_server` gives: the key is resolved at that moment,
    so a server nobody calls never opens the OS keyring.
    """

    def __init__(self, provider: EmbeddingProviderSettings) -> None:
        self.provider = provider
        self._client: Any = None

    @property
    def _known(self) -> tuple[int, int] | None:
        return _KNOWN_MODELS.get(self.provider.model)

    def client(self) -> Any:
        """The SDK client, created on first use.

        Raises:
            RuntimeError: If the key did not resolve.  Named here rather than
                left to the SDK, because a placeholder arriving as a bearer
                token produces a 401 that says nothing about which `${VAR}` is
                missing.
        """
        if self._client is not None:
            return self._client

        key = unresolved_key(SERVER_NAME, self.provider.name, self.provider)
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            base_url=self.provider.base_url,
            api_key=key,
            timeout=EMBED_TIMEOUT_SECONDS,
            max_retries=EMBED_MAX_RETRIES,
        )
        return self._client

    async def describe(self) -> dict[str, Any]:
        """What the db needs to know before it can build an index.

        The width is established once per process and remembered by the caller;
        this is the only place the three-step ladder lives.
        """
        dimension = await self._dimension_of()
        max_tokens = self._known[1] if self._known else _DEFAULT_MAX_TOKENS
        return {
            "provider": self.provider.name,
            "model": self.provider.model,
            "base_url": self.provider.base_url,
            "dimension": dimension,
            "max_chars": max_tokens * _CHARS_PER_TOKEN,
        }

    async def _dimension_of(self) -> int:
        """The width, by table, by listing, or by measurement — in that order."""
        if self._known is not None:
            return self._known[0]

        listed = await self._listed_dimension()
        if listed:
            logger.info(
                "%s: width %d read from the endpoint's model listing",
                self.provider.model,
                listed,
            )
            return listed

        # Nothing left but to ask, with the cheapest question there is: one
        # character, whose vector is the width.
        answer = await self.embed(["."])
        if len(answer) != 1 or not answer[0]:
            # Named rather than left to `(vector,) = ...`, whose failure is a
            # bare ValueError about unpacking that says neither which endpoint
            # nor which model answered it — and a zero-length vector would
            # otherwise be accepted as a width of zero, which is the silent
            # failure this whole ladder exists to prevent.
            raise RuntimeError(
                f"{SERVER_NAME}: {self.provider.name!r} answered the probe with "
                f"{len(answer)} vector(s) for one input; cannot read a width "
                f"for {self.provider.model!r} from {self.provider.base_url!r}"
            )
        width = len(answer[0])
        logger.info(
            "%s: width %d measured with a probe embedding", self.provider.model, width
        )
        return width

    async def _listed_dimension(self) -> int:
        """The width the endpoint reports for its model, if it reports one.

        Most do not — the OpenAI protocol has no field for it — so this usually
        answers nothing and the probe runs.  It is kept because the endpoints
        that do report it are the ones with several models behind one base_url,
        which is exactly where guessing goes wrong.
        """
        try:
            listing = await self.client().models.list()
        except Exception as exc:  # noqa: BLE001 — an unhelpful listing is not fatal
            logger.debug("%s: no model listing (%s)", self.provider.model, exc)
            return 0
        for entry in getattr(listing, "data", []) or []:
            if getattr(entry, "id", "") != self.provider.model:
                continue
            reported = getattr(entry, "dimension", None)
            if isinstance(reported, int) and reported > 0:
                return reported
        return 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One request, every text, in order.

        A whole list per request, as the protocol is shaped: the caller decides
        how much to put in one, and it decides one chunk at a time because a
        long document batched together can exceed the timeout on a service
        computing embeddings on a CPU.
        """
        if not texts:
            return []
        response = await self.client().embeddings.create(
            model=self.provider.model, input=texts
        )
        return [list(item.embedding) for item in response.data]


def build_server(config: Config, *, client: EmbeddingClient | None = None) -> FastMCP:
    """Build the embeddings MCP server.  `client` is injectable for tests."""
    embedder = (
        client
        if client is not None
        else EmbeddingClient(config.embeddings.active_provider())
    )

    mcp: FastMCP = house_server(
        SERVER_NAME,
        instructions=(
            "Embeddings for the db plugin's vector index. `describe` reports "
            "the width and input limit of the configured model; `embed` turns "
            "text into vectors. Neither is offered to the model."
        ),
    )

    @mcp.tool
    async def describe() -> dict[str, Any]:
        """The configured embedding model, and how wide its vectors are.

        The width is discovered rather than configured, because a vector table
        fixes its width in its DDL and cannot be altered after the fact.  The db
        asks this once per process, before it builds or rebuilds anything.
        """
        return await embedder.describe()

    @mcp.tool
    async def embed(texts: list[str]) -> dict[str, Any]:
        """Embed texts, one vector each, in the order they were given.

        Args:
            texts: The chunks to embed.  One call is one request to the model, so
                the caller decides how much goes in it.
        """
        return {"vectors": await embedder.embed(texts)}

    return mcp


def main(argv: list[str] | None = None) -> int:
    args = parse_serve_args(argv, SERVER_NAME)
    configure_logging()
    config_path = find_config_path()
    config = load()

    address = config.server(CONFIG_KEY)
    provider = config.embeddings.active_provider()
    logger.info(
        "serving %s on http://%s:%d%s (embeddings from %s at %s)",
        SERVER_NAME,
        args.host or address.host,
        args.port or address.port,
        address.path,
        provider.label,
        provider.base_url,
    )
    serve(
        build_server(config),
        address,
        args,
        name=SERVER_NAME,
        config_path=config_path,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
