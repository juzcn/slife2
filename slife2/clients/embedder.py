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
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
from fastmcp import Client
from mcp import MCPError
from mcp.types import CONNECTION_CLOSED

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

#: The SDK's JSON-RPC code for a link that is gone, read from `mcp.types` where
#: the SDK states it rather than spelled again here.
LINK_DOWN_CODE = CONNECTION_CLOSED


def _link_was_lost(exc: BaseException) -> bool:
    """Whether a failed call died with its connection rather than being answered.

    **Two shapes reach the caller, and which one is a race.**  Measured against
    mcp 2.3.0 / fastmcp 4.2.0: a request on a connection the server has closed
    dies inside the transport's POST task, which tears the whole MCP session
    down; the pending waiter is woken with `MCPError(CONNECTION_CLOSED)`, and
    when the session task wins instead, its own `ReadError` is what
    `_await_with_session_monitoring` raises.  Groups are unwrapped because a
    task group is how such a death travels in some timings — the caller must
    not depend on which branch won.

    **This is not "the peer refused".**  A tool error, a timeout or a malformed
    answer is the far end's answer and reaches the caller unchanged; only a
    link this process no longer has is worth opening again.
    """
    if isinstance(exc, MCPError):
        return getattr(exc, "code", None) == LINK_DOWN_CODE
    if isinstance(exc, (httpx2.ReadError, httpx2.RemoteProtocolError)):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_link_was_lost(one) for one in exc.exceptions)
    return False


#: How a dead connection is replaced.  The connection owns the opening policy —
#: which URL, name and timeout — so it is handed the embedder to re-point and
#: the client that died, and it validates the description it comes back with.
Reopen = Callable[["RemoteEmbedder", Client], Awaitable[None]]


def _described_facts(described: dict[str, Any]) -> tuple[str, int, int, str]:
    """The four facts a description must carry, or a refusal naming the hole.

    **Read once and compared on every reconnect.**  `identity` is the endpoint
    and the model, and deliberately not the model alone: two endpoints can
    serve one model id and mean different weights, so a repointed `base_url`
    has to count as a different model.  The alternative is a table holding two
    models' vectors, ranked against each other, with nothing able to say why
    the numbers went strange.

    All three identity fields are required: a `""` slot makes a changed model
    or a repointed endpoint invisible in the identity, and the identity is the
    lever that turns either into a rebuild (SDD §5.5) — the same argument the
    width and input limit are refused on.  An empty `rerank_model` is a real
    answer (an endpoint with no cross-encoder); a missing key is another build.
    """
    fields = ("provider", "model", "base_url")
    if not all(str(described.get(field) or "") for field in fields):
        raise RuntimeError(
            f"the embeddings server described itself without a provider, a "
            f"model or a base_url ({described!r}), so a changed model or a "
            f"repointed endpoint would not be a different index"
        )
    identity = "|".join(str(described[field]) for field in fields)
    dimension = int(described.get("dimension") or 0)
    max_chars = int(described.get("max_chars") or 0)
    rerank_model = described.get("rerank_model")
    if not isinstance(rerank_model, str):
        raise RuntimeError(
            f"the embeddings server described itself without a rerank_model "
            f"({described!r}); an empty value is its answer for an endpoint "
            f"with no cross-encoder"
        )
    if not dimension or not max_chars:
        raise RuntimeError(
            f"the embeddings server described itself without a width or an "
            f"input limit ({described!r}), so no index can be built for it"
        )
    return identity, dimension, max_chars, rerank_model


class RemoteEmbedder:
    """The near side's view of the embeddings server: three facts, one call.

    Read once, because they cannot change while that server runs — it is what
    would change them, and changing one means a restart, which is exactly when an
    index asks whether it is still the right index.
    """

    def __init__(
        self,
        client: Client,
        described: dict[str, Any],
        *,
        reopen: Reopen | None = None,
    ) -> None:
        self._client = client
        #: How a dead connection is replaced, or `None` when the caller owns the
        #: client outright (a test's double, or the identity tests' `None`).
        self._reopen = reopen
        (
            self._identity,
            self._dimension,
            self._max_chars,
            self._rerank_model,
        ) = _described_facts(described)

    def _adopt(self, client: Client, described: dict[str, Any]) -> None:
        """Take a reopened connection for this embedder, or refuse what changed.

        **A reconnect must not repoint a live index at another model.**  Every
        store built its index against `identity` and `dimension`, and vectors
        from two models cannot be ranked against each other (SDD §8.4), so
        adopting a server whose description differs would make the mismatch
        silent.  The change is a failure the caller sees instead, and a restart
        — which rebuilds the index — is the fix.
        """
        facts = _described_facts(described)
        if facts != (
            self._identity,
            self._dimension,
            self._max_chars,
            self._rerank_model,
        ):
            raise RuntimeError(
                f"the embeddings server came back as {facts[0]!r} ({facts[1]} "
                f"wide), not as {self._identity!r} ({self._dimension} wide), "
                f"which is what this index was built for; restart to rebuild it"
            )
        self._client = client

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

    async def _call(
        self, tool: str, args: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """One tool call, repeated once across a connection that died.

        **The repeat reopens the connection, because that is the whole
        recovery.**  A keep-alive the server closed surfaces as a dead MCP
        session, not as a bad socket a pool can silently replace: asking the
        same client again answers `CONNECTION_CLOSED` (measured; see
        :func:`_link_was_lost`).  So the connection is opened again — a new
        session, and a new `describe` that must agree about the model — and the
        call is made once more.  Embedding and reranking are idempotent, which
        is what makes the second send safe; a second failure is a server that
        is really gone and reaches the caller.
        """
        dead = self._client
        try:
            return await self._once(tool, args, timeout=timeout)
        except BaseException as exc:
            # `BaseException`, not `Exception`, because a group carrying the
            # transport's death can hold either kind.  Everything the predicate
            # does not name — a cancellation included — is re-raised unchanged.
            # A cancel that rides with a death is still a cancel: the caller
            # asked this process to stop, and a retry would outlive the ask.
            if isinstance(exc, BaseExceptionGroup) and (
                exc.subgroup(asyncio.CancelledError) is not None
            ):
                raise
            if self._reopen is None or not _link_was_lost(exc):
                raise
        logger.info(
            "the embeddings connection died under %s; reconnecting and asking once",
            tool,
        )
        await self._reopen(self, dead)
        return await self._once(tool, args, timeout=timeout)

    async def _once(
        self, tool: str, args: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        return tool_payload(await self._client.call_tool(tool, args, timeout=timeout))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One request to the far side, with the shape of the answer checked.

        Checked because a short answer would otherwise read as "these turns had
        nothing worth embedding": they would keep no vector at all, and the hole
        in the index would be silent.  The count is the part a caller cannot
        recover from, so it is the part that fails.

        **A call on a connection the server closed while it was idle is
        repeated once.**  The client keeps its MCP session for the life of the
        process, and the server is free to close an idle keep-alive connection
        between two catalogue merges — which is what a busy start-up produces:
        the first listings embed while the connection is fresh, then
        `agent-task` and `schedule` start (they wait for the hub), and their
        listings are the first request after an idle gap.  The death reaches
        this call as `MCPError(CONNECTION_CLOSED)`, and `_call` reopens the
        connection and asks again.  Embedding is idempotent, which is what
        makes the repeat safe; a second failure is a server that is really
        gone and reaches the caller as before.
        """
        payload = await self._call("embed", {"texts": texts})
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
        payload = await self._call("rerank", args, timeout=_T.timeouts.transport.rerank)
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

    async def _connect(self) -> tuple[Client, dict[str, Any]]:
        """One entered client, and the server's own description of itself."""
        client: Client = await open_server(
            self._config.server(EMBEDDINGS_KEY).url,
            name=EMBEDDINGS_SERVER_NAME,
            fallback_tool="embed",
            timeout=_T.timeouts.transport.embed,
        )
        try:
            described = tool_payload(await client.call_tool("describe", {}))
        except BaseException:
            # A describe that fails (an unreachable provider) must not leave
            # the entered client behind: the next get() would open another and
            # overwrite this one, leaking it for the life of the process.
            # Close it and let the failure stand.
            await close_server(client)
            raise
        return client, described

    async def get(self) -> Embedder:
        embedder = self._embedder
        if embedder is not None:
            return embedder
        async with self._opening:
            embedder = self._embedder
            if embedder is None:
                client, described = await self._connect()
                embedder = RemoteEmbedder(client, described, reopen=self._reopen)
                self._client = client
                self._embedder = embedder
                logger.info(
                    "embedding with %s (%s wide)",
                    embedder.identity,
                    embedder.dimension,
                )
            return embedder

    async def _reopen(self, embedder: RemoteEmbedder, dead: Client) -> None:
        """Replace the connection behind `embedder` after its session died.

        **Once per dead connection, however many calls noticed it.**  Callers
        in flight on the same connection fail together; the first under the
        lock opens a replacement and the rest find it already adopted — the
        embedder is one shared object, so re-pointing it is all they need.

        The replacement is described before it is adopted: a server that came
        back as another model must not be used for an index built for the old
        one (`RemoteEmbedder._adopt`).
        """
        async with self._opening:
            current = self._client
            if current is not None and current is not dead:
                return
            client, described = await self._connect()
            try:
                embedder._adopt(client, described)
            except BaseException:
                await close_server(client)
                raise
            self._client = client
            await close_server(dead)

    async def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]] | None:
        """The reranker this provider has, or `None` when it has none.

        **`rerank` is part of the embedder contract** (`slife2.libs.store.index`):
        a provider with no cross-encoder answers `None`, and a test double is a
        provider of that contract rather than an object this call probes.
        """
        embedder = await self.get()
        return await embedder.rerank(query, documents, top_n)

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
