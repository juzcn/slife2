"""The hop to the embeddings server, as a library both stores build one from.

**A library and not a server, because it has nothing of its own.**  It holds no
credential — the credential is the embeddings *server's*, which is the process
that reads the endpoint and the key off the config — and no state beyond one
connection.  What it is is a client, and there are two of them now: the context
plugin, for the turns' vectors, and the toolhub, for the catalogue's.  Two copies
of "ask the far side, check the shape of the answer" is how the two would come to
disagree about what an embedding is, so it is written once here.

That is also why this is not in `slife2.llm`: it is the *client* half, and the
one thing the client half must not do is drag a server into the process that
imports it.  `slife2.llm.embeddings_server` is the other half and neither
imports the other.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastmcp import Client

from slife2.config import EMBEDDINGS_SERVER_NAME
from slife2.db import Embedder
from slife2.mcp_server import close_server, open_server, tool_payload

logger = logging.getLogger(__name__)

#: The embeddings server's key in the config's `servers:` table.
EMBEDDINGS_KEY = "embeddings"

#: How long a hop to the embeddings server may take.  **Larger than that server's
#: own request timeout on purpose** (`slife2.llm.embeddings_server.
#: EMBED_TIMEOUT_SECONDS`): the inner deadline is the one that can name the
#: endpoint that did not answer, and an outer one that fired first would replace
#: that message with a timeout of its own.
EMBEDDINGS_TIMEOUT_SECONDS = 60.0


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
        self._identity = "|".join(
            str(described.get(field) or "")
            for field in ("provider", "model", "base_url")
        )
        self._dimension = int(described.get("dimension") or 0)
        self._max_chars = int(described.get("max_chars") or 0)
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

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One request to the far side, with the shape of the answer checked.

        Checked because a short answer would otherwise read as "these turns had
        nothing worth embedding": they would keep no vector at all, and the hole
        in the index would be silent.  The count is the part a caller cannot
        recover from, so it is the part that fails.
        """
        payload = tool_payload(await self._client.call_tool("embed", {"texts": texts}))
        vectors = payload.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            answered = len(vectors) if isinstance(vectors, list) else "no"
            raise RuntimeError(
                f"the embeddings server answered {answered} vectors for "
                f"{len(texts)} texts"
            )
        return [[float(value) for value in vector] for vector in vectors]


class EmbedderConnection:
    """One embedder, opened on first use and kept for the process.

    The same arrangement the agent server makes with a model backend and for the
    same reason: a handshake per call is a handshake per call.  It is a class
    rather than a function because two plugins now want it and neither may open a
    connection before there is a loop to open one on — so what is handed round is
    this object, and whoever asks first pays.

    **Opening one is what `slife2.mcp_server.open_server` decides about a missing
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
                    timeout=EMBEDDINGS_TIMEOUT_SECONDS,
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

    async def close(self) -> None:
        if self._client is not None:
            await close_server(self._client)
            self._client = None
            self._embedder = None


__all__ = [
    "EMBEDDINGS_KEY",
    "EMBEDDINGS_TIMEOUT_SECONDS",
    "Embedder",
    "EmbedderConnection",
    "RemoteEmbedder",
]
