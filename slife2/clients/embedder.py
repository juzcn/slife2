"""The hop to the embeddings server, as a library every store builds one from.

**A library and not a server, because it has nothing of its own.**  It holds no
credential — the credential is the embeddings *server's*, which is the process
that reads the endpoint and the key off the config — and no state beyond one
connection.  What it is is a client, and there are three of them now: the context
plugin for the turns' vectors, the toolhub for the catalogue's, and the cabinet
for its documents.  Three copies of "ask the far side, check the shape of the
answer" is how they would come to disagree about what an embedding is, so it is
written once here.

That is also why this is not in `slife2.plugins.llm`: it is the *client* half, and the
one thing the client half must not do is drag a server into the process that
imports it.  `slife2.plugins.llm.embeddings_server` is the other half and neither
imports the other.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx2
from fastmcp import Client

import slife2.helpers.timeouts as _T
from slife2.libs.host.config import EMBEDDINGS_SERVER_NAME, plugin
from slife2.libs.plugin.protocol import close_server, open_server, tool_payload
from slife2.libs.store.index import Embedder

logger = logging.getLogger(__name__)

#: The embeddings server's key in the config's `servers:` table, read off the row
#: `PLUGINS` files it under.  It is the only key any client needs to know, and it
#: used to be a literal here: `servers:` keys are the launcher's, the plugins' and
#: now the registry's, and a client that spells one is a fourth place for it.
EMBEDDINGS_KEY = plugin("embeddings").key


class RemoteEmbedder:
    """The near side's view of the embeddings server: three facts, one call.

    Read once, because they cannot change while that server runs — it is what
    would change them, and changing one means a restart, which is exactly when an
    index asks whether it is still the right index.

    `identity` is the endpoint and the model, and deliberately not the model
    alone: two endpoints can serve one model id and mean different weights, so a
    repointed `base_url` has to count as a different model.  The alternative is a
    table holding two models' vectors, ranked against each other, with nothing
    able to say why the numbers went strange.
    """

    def __init__(self, client: Client, described: dict[str, Any]) -> None:
        self._client = client
        #: The three fields that say *which* endpoint this is.  All are
        #: required: a `""` slot makes a changed model or a repointed endpoint
        #: invisible in the identity, and the identity is the lever that turns
        #: either into a rebuild (SDD §5.5) — the same argument the width
        #: and input limit are refused on below.
        fields = ("provider", "model", "base_url")
        if not all(str(described.get(field) or "") for field in fields):
            raise RuntimeError(
                f"the embeddings server described itself without a provider, a "
                f"model or a base_url ({described!r}), so a changed model or a "
                f"repointed endpoint would not be a different index"
            )
        self._identity = "|".join(str(described[field]) for field in fields)
        self._dimension = int(described.get("dimension") or 0)
        self._max_chars = int(described.get("max_chars") or 0)
        #: Empty means the endpoint serves no cross-encoder; the recall then
        #: keeps its fused order and says nothing failed.  The key itself is
        #: required: `describe` always writes it, so a hole is another build.
        rerank_model = described.get("rerank_model")
        if not isinstance(rerank_model, str):
            raise RuntimeError(
                f"the embeddings server described itself without a rerank_model "
                f"({described!r}); an empty value is its answer for an endpoint "
                f"with no cross-encoder"
            )
        self._rerank_model = rerank_model
        if not self._dimension or not self._max_chars:
            raise RuntimeError(
                f"the embeddings server described itself without a width or an "
                f"input limit ({described!r}), so no index can be built for it"
            )

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def max_chars(self) -> int:
        return self._max_chars

    @property
    def rerank_model(self) -> str:
        return self._rerank_model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One request to the far side, with the shape of the answer checked.

        Checked because a short answer would otherwise read as "these turns had
        nothing worth embedding": they would keep no vector at all, and the hole
        in the index would be silent.  The count is the part a caller cannot
        recover from, so it is the part that fails.

        **A read on an already-closed connection is retried once.**  The client
        keeps its connection for the life of the process, and the server is free
        to close an idle keep-alive connection between two catalogue merges —
        which is what a busy start-up produces: the first listings embed while
        the connection is fresh, then `agent-task` and `schedule` start (they
        wait for the hub), and their listings are the first request after an
        idle gap.  The failure is a `ReadError` with an empty message; the
        client discards the dead connection, so the retry opens a new one.
        Embedding is idempotent, which is what makes the repeat safe.  A second
        failure is a server that is really gone and reaches the caller as before.
        """
        try:
            payload = tool_payload(
                await self._client.call_tool("embed", {"texts": texts})
            )
        except (httpx2.ReadError, httpx2.RemoteProtocolError):
            payload = tool_payload(
                await self._client.call_tool("embed", {"texts": texts})
            )
        vectors = payload.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            answered = len(vectors) if isinstance(vectors, list) else "no"
            raise RuntimeError(
                f"the embeddings server answered {answered} vectors for "
                f"{len(texts)} texts"
            )
        return [[float(value) for value in vector] for vector in vectors]

    async def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]] | None:
        """One cross-encoder call, or `None` when the endpoint serves none.

        **`None` is a capability answer, not a failure.**  A provider with no
        `rerank_model` is a config that chose an endpoint with no reranker; the
        caller keeps its fused order and does not report a failure nobody can
        fix.  An *answer* that is malformed is different, and fails: an index
        out of range would map one candidate's score onto another, silently,
        and a **short** answer would read as "the missing candidates scored
        nothing" — the caller drops everything it was not told a score for.  A
        short answer is refused here for the reason `embed` refuses one.

        The indices point into this call's `documents`, which is why the caller
        keeps its own list and does not re-sort before mapping back.
        """
        if not self._rerank_model:
            return None
        args: dict[str, Any] = {"query": query, "documents": documents}
        if top_n is not None:
            args["top_n"] = top_n
        try:
            payload = tool_payload(
                await self._client.call_tool(
                    "rerank", args, timeout=_T.timeouts.transport.rerank
                )
            )
        except (httpx2.ReadError, httpx2.RemoteProtocolError):
            payload = tool_payload(
                await self._client.call_tool(
                    "rerank", args, timeout=_T.timeouts.transport.rerank
                )
            )
        results = payload.get("results")
        if not isinstance(results, list):
            raise RuntimeError(
                f"the embeddings server answered no rerank results ({payload!r})"
            )
        found: list[tuple[int, float]] = []
        for entry in results:
            if not isinstance(entry, dict):
                raise RuntimeError(
                    f"the embeddings server answered a rerank result that is "
                    f"not an object ({entry!r})"
                )
            index = entry.get("index")
            score = entry.get("relevance_score")
            if not isinstance(index, int) or isinstance(index, bool):
                raise RuntimeError(
                    f"the embeddings server answered a rerank index {index!r}"
                )
            if not 0 <= index < len(documents):
                raise RuntimeError(
                    f"the embeddings server named document {index} for {len(documents)}"
                )
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                raise RuntimeError(
                    f"the embeddings server answered a rerank score {score!r}"
                )
            found.append((index, float(score)))
        # The whole list when no `top_n` was named, and only that many when one
        # was.  More than asked for is not a failure; fewer leaves candidates
        # the caller would treat as rejected.
        expected = len(documents) if top_n is None else min(top_n, len(documents))
        if len({index for index, _ in found}) < expected:
            raise RuntimeError(
                f"the embeddings server answered {len(found)} rerank scores "
                f"for {len(documents)} documents; {expected} were asked for"
            )
        return sorted(found, key=lambda pair: -pair[1])


class EmbedderConnection:
    """One embedder, opened on first use and kept for the process.

    The same arrangement the agent server makes with a model backend and for the
    same reason: a handshake per call is a handshake per call.  It is a class
    rather than a function because three plugins now want it and none may open a
    connection before there is a loop to open one on — so what is handed round is
    this object, and whoever asks first pays.

    **Opening one is what `slife2.libs.plugin.protocol.open_server` decides about a missing
    peer** — it raises, and it is meant to: a store with no embedder is a store
    that can write nothing, since a turn is written with its vector in one
    transaction.  `close` is the other half, for a lifespan that owns one.
    """

    def __init__(self, config: Any, *, embedder: Embedder | None = None) -> None:
        self._config = config
        #: An injected embedder is a test's, and it owns the connection.
        self._embedder: Embedder | None = embedder
        self._client: Client | None = None
        self._opening = asyncio.Lock()

    async def get(self) -> Embedder:
        if self._embedder is not None:
            return self._embedder
        async with self._opening:
            if self._embedder is None:
                client: Client = await open_server(
                    self._config.server(EMBEDDINGS_KEY).url,
                    name=EMBEDDINGS_SERVER_NAME,
                    fallback_tool="embed",
                    timeout=_T.timeouts.transport.embed,
                )
                self._client = client
                described = tool_payload(await client.call_tool("describe", {}))
                self._embedder = RemoteEmbedder(client, described)
                logger.info(
                    "embedding with %s (%s wide)",
                    self._embedder.identity,
                    self._embedder.dimension,
                )
            assert self._embedder is not None
            return self._embedder

    async def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]] | None:
        """The reranker this provider has, or `None` when it has none.

        The injected test embedder is asked the same way: a double that can
        rerank answers, and one that cannot is a provider without a
        cross-encoder rather than an error.
        """
        embedder = await self.get()
        method = getattr(embedder, "rerank", None)
        if method is None:
            return None
        return await method(query, documents, top_n)

    async def close(self) -> None:
        if self._client is not None:
            await close_server(self._client)
            self._client = None
            self._embedder = None


__all__ = [
    "EMBEDDINGS_KEY",
    "EmbedderConnection",
    "RemoteEmbedder",
]
