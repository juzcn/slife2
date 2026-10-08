"""slife2-db — persistence: what slife2 keeps, one database per client id.

The turns are what it keeps today.  Keeping is the whole of the job — it does
not summarise, does not decide what mattered, does not put anything back into a
conversation, and does not decide *whether* a turn is worth keeping: the caller
does that, and the agent server is the caller that knows a worker's turns are
not.  A turn is stored as it happened — with one
deliberate exception, an oversized tool result, which is kept as an announced
head-and-tail digest rather than in full (see `slife2.db`) — so a question
this component cannot answer today can be asked of the same rows later without a
migration: search, embedding and summarising are each a table the schema has a
place for and nothing here builds yet.

**Client ids are isolated by file.**  `("jack", "")` reads and writes
`jack.turn.db`, and `("jack", "worker")` writes `jack@worker.turn.db`; there is no
query that can reach another id's turns, because there is no other id's turns in
the file.  One process serves every id — the same arrangement as the model
backends, where one process speaks one wire format for every provider — and the
`(agent, subagent)` pair picks the file.

It is shared infrastructure like the rest: started on demand, reused if already
running, never per-agent.

**A turn is stored with the two indexes over it, in one transaction.**  The
keyword index is built here from the text; the vector index needs an embedding,
so this process reaches the embeddings server (`slife2-llm-embeddings`) before
it opens its transaction — and a save that cannot embed stores nothing, rather
than storing a turn nothing can find.  That makes the embedding model a hard
dependency of this component: it cannot be switched off, and an endpoint that
cannot be reached fails the save and is reported, because there is no mode in
which this server runs without semantic search.

**Startup is where an index is brought up to date with the model.**  Every
database in the data directory is opened, its recorded index identity compared
with the configured embedder's, and — if they differ — the vectors are dropped
and every turn embedded again.  The same pass fills in turns that have no
vector for any other reason.  Nothing is served until it has finished, and a
database that is still not ready afterwards fails the start with its reasons:
`slife2.db.TurnStore.index_status` exists to be able to *say* that rather than
assume it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastmcp import Client, Context, FastMCP

from slife2.audience import FOR_THE_MODEL, request_client
from slife2.config import (
    EMBEDDINGS_SERVER_NAME,
    Config,
    find_config_path,
    load,
)
from slife2.db import PREVIEW_CHARS, Embedder, TurnStore, store_for
from slife2.mcp_server import (
    close_server,
    configure_logging,
    describe,
    house_server,
    open_server,
    parse_serve_args,
    serve,
    tool_payload,
)
from slife2.paths import db_dir

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-db"

#: This server's key in the config's `servers:` table.
CONFIG_KEY = "db"

#: The embeddings server's, which is one of `slife2.config.LOCAL_SERVERS`.  Not
#: `CONFIG_KEY` — this process reads one section and hops to another.
EMBEDDINGS_KEY = "embeddings"

#: How long a hop to the embeddings server may take.  **Larger than that
#: server's own request timeout on purpose** (`slife2.llm.embeddings_server.
#: EMBED_TIMEOUT_SECONDS`): the inner deadline is the one that can name the
#: endpoint that did not answer, and an outer one that fired first would replace
#: that message with a timeout of its own.
EMBEDDINGS_TIMEOUT_SECONDS = 60.0

INSTRUCTIONS = (
    "Persisted turns, one database per client id. Call `remember` after a turn, "
    "naming the `(agent, subagent)` the turn was taken under. The store keeps no "
    "opinion about what matters: it writes what it is given and returns it in "
    "order. Reading is by time — `turn_list` browses and pages, `turn_read` "
    "returns one turn whole — which is the honest thing for a component that "
    "stores without judging. Both of those are the model's, and neither names an "
    "agent: they answer about the conversation the call came from."
)


class RemoteEmbedder:
    """The db's view of the embeddings server: three facts and one call.

    Read once, because they cannot change while that server runs — it is what
    would change them, and changing one means a restart, which is exactly when
    an index asks whether it is still the right index.

    `identity` is the endpoint and the model, and deliberately not the model
    alone: two endpoints can serve one model id and mean different weights, so a
    repointed `base_url` has to count as a different model.  The alternative is
    a table holding two models' vectors, ranked against each other, with nothing
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


def build_server(config: Config, *, embedder: Embedder | None = None) -> FastMCP:
    """Build the db MCP server.

    `embedder` is injectable for tests, the same seam the model servers have: a
    whole server can be driven over the in-memory transport with no embeddings
    endpoint and no network behind it.
    """

    async def _on_thread(function, *args, **kwargs):
        """Run a blocking store call off the event loop.

        SQLite is synchronous, and a tool that blocks the loop blocks every
        other call this server is handling.  A connection per call is what makes
        the thread hop safe — a connection is not shareable across threads, so
        there is none to share.
        """
        return await asyncio.to_thread(function, *args, **kwargs)

    embedding = embedder
    peers: dict[str, Client] = {}
    opening = asyncio.Lock()
    synced: set[Path] = set()

    async def embeddings() -> Embedder:
        """The embedding model, opened once for the process.

        A peer that is not there **raises**, like every other peer in this
        system: this server cannot store a turn it cannot index, so a missing
        embeddings server is a system that has come apart rather than a
        component working with fewer abilities.
        """
        nonlocal embedding
        if embedding is not None:
            return embedding
        async with opening:
            if embedding is None:
                client = await open_server(
                    config.server(EMBEDDINGS_KEY).url,
                    name=EMBEDDINGS_SERVER_NAME,
                    fallback_tool="embed",
                    timeout=EMBEDDINGS_TIMEOUT_SECONDS,
                )
                peers[EMBEDDINGS_KEY] = client
                described = tool_payload(await client.call_tool("describe", {}))
                embedding = RemoteEmbedder(client, described)
        return embedding

    async def indexed(store: TurnStore, model: Embedder) -> None:
        """Bring one file up to date with `model`, or say why it cannot be."""
        await store.sync_indexes(model)
        status = store.index_status(model)
        if not status["ready"]:
            raise RuntimeError(
                f"the db at {store.path} cannot be searched semantically: "
                + "; ".join(status["problems"])
            )
        synced.add(store.path)
        logger.info("indexed %s (%s)", store.path, status)

    async def store_of(agent: str, subagent: str) -> TurnStore:
        """One conversation's store, with its indexes brought up to date.

        Synced once per file per process.  A file created after the startup pass
        is not re-synced by this: its own save writes the vector for the turn it
        is saving, and the only thing a sync would add is re-embedding turns
        that do not exist yet.
        """
        store = await _on_thread(store_for, agent, subagent)
        if store.path not in synced:
            await indexed(store, await embeddings())
        return store

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, Any]]:
        """Bring every database up to date before answering anything at all.

        **Nothing is served until this has finished**, because the alternative
        is a window in which a search answers from an index that is not the one
        it claims to be — and the thing that hides, a turn stored with no
        vector, cannot be seen from outside.  A changed embedding model makes
        this the expensive step: every turn in every file is embedded again, and
        that is the price of changing the model rather than a fault to be
        avoided.
        """
        model = await embeddings()
        for path in sorted(db_dir().glob("*.turn.db")):
            await indexed(await _on_thread(TurnStore, path), model)
        try:
            yield {}
        finally:
            client = peers.pop(EMBEDDINGS_KEY, None)
            if client is not None:
                await close_server(client)

    mcp: FastMCP = house_server(
        SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan
    )

    @mcp.tool
    async def remember(
        agent: str,
        messages: list[dict[str, Any]],
        subagent: str = "",
        token_count: int = 0,
        context_tokens: int = 0,
        who_helped: str = "",
        what_model: str = "",
        channel: str = "",
        created_at: str | None = None,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        """Persist one turn.

        A turn is one exchange: what the user said, and everything the agent did
        about it — assistant messages, tool calls and their results — in the
        order they happened.  Store it as it is; this is a record, not an
        interpretation.

        Args:
            agent: Whose memory.  With `subagent` it names the database file, so
                client ids are isolated from each other by construction.
            messages: The whole turn, as the agent loop returned it: the user's
                message first, then every assistant message, tool call and
                result.  Store it as it is.  What the user said is in there as
                the first entry, and so is an attached image — which is the one
                thing this does not keep: the bytes become a note saying they
                were there, and the file is still named in the prompt.
            subagent: Which of that agent's conversations; empty for its own.
            token_count: What the turn cost, summed over every model call in it.
            context_tokens: The last model call's prompt plus completion — how
                large the conversation had become, which is what the next
                request resends.  A different question from `token_count`: one
                is a bill, the other is what the context window has to hold.
            who_helped: The agent that answered.
            what_model: Which model answered, as `provider/model`.
            channel: Where the turn came in from — `human`, or a client id.
                Empty when the caller has nothing to say about it.
            created_at: When the user pressed enter.  Defaults to now, which is
                the same moment to within a hop for a caller on loopback.
            completed_at: When the assistant finished.  Defaults to now.

        Returns:
            `turn_id` of the stored turn, and the file it went into.
        """
        store = await store_of(agent, subagent)
        # Awaited here, and off this server's loop inside the store: the store
        # embeds on the loop, where a network call belongs, and does its three
        # inserts on a thread, where blocking belongs.  A save that raises
        # stored nothing, which is what the caller is told.
        turn_id = await store.save_turn(
            messages=messages,
            embedder=await embeddings(),
            token_count=token_count,
            context_tokens=context_tokens,
            who_helped=who_helped,
            what_model=what_model,
            channel=channel,
            created_at=created_at,
            completed_at=completed_at,
        )
        logger.info("stored turn %s for %s", turn_id, describe((agent, subagent)))
        return {"turn_id": turn_id, "database": str(store.path)}

    def _caller(ctx: Context) -> tuple[str, str]:
        """Whose memory a model's call is about — read from the request.

        **There is no `agent` argument, and that is the whole point.**  A model
        that could name a database could read somebody else's memory, and the
        only thing standing between it and that would be a sentence in its own
        system prompt — which is an instruction, not a boundary.  The
        conversation is a fact the caller's side holds (`slife2.toolclient`
        attaches it to the call) and this side reads; the model neither sees it
        nor can write it.

        Raises:
            ValueError: If the call arrived without one.  Every real caller is
                the hub, which forwards what the agent gave it, so this is a
                caller that reached the db server directly — a test, or a
                component that is not the agent — and it is better told than
                quietly served whatever it named.
        """
        found = request_client(ctx)
        if found is None:
            raise ValueError(
                "this tool reads one conversation's memory and the call did not "
                "say whose; it is called through the toolhub, which forwards the "
                "caller's identity (`slife2.audience`)"
            )
        return found

    @mcp.tool(meta=FOR_THE_MODEL)
    async def turn_list(
        ctx: Context,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Browse what was said before, newest first, one line per turn.

        The way back into a conversation you were not in, or were in long enough
        ago to have lost: when each turn was, what was asked, what was answered,
        and the id to read the whole of it with `turn_read`.  Both messages come
        back cut short — enough to tell whether this is the turn you wanted, not
        enough to be the turn itself.

        Neither bound is required, and neither has a default window: leaving
        both out browses the newest turns.  A bound narrows what you see and
        never decides it — a default range would hide turns with nothing saying
        they were hidden, which is the one thing a browse must not do.

        Your own history, and only your own: there is no argument naming whose
        memory to read, because a model that could name one could read somebody
        else's.

        Args:
            since: Lower bound on when the turn was written — an ISO date or
                datetime, or one of: today, yesterday, tomorrow, now,
                last|this week|month|quarter|year, or '<N> day(s)|week(s)|month(s)|year(s) ago'.
                Omit for no lower bound.
            until: Upper bound on the same grammar.  A date means the whole day.
                Omit for no upper bound.
            limit: How many turns this page may hold.
            offset: Skip this many turns — that is how you page back.

        Returns:
            `entries` — `turn_id`, `created_at`, `user_message`,
            `assistant_message` and `token_count` per turn — `total`, how many
            turns the window holds, so `offset + len(entries) < total` says
            whether there is more — and the `limit` and `offset` that produced
            this page.
        """
        # `store_for` and not `store_of`: reading history needs the turn table
        # and nothing else, so it must not need an embedding model.  A browse
        # that failed because the embeddings endpoint was down would be a read
        # coupled to a write's dependency.  Building the store is still a
        # blocking call — it opens the file and runs the schema — so it goes off
        # the loop like every other one here.
        store = await _on_thread(store_for, *_caller(ctx))
        records, total = await _on_thread(
            store.turns, since=since, until=until, limit=limit, offset=offset
        )
        return {
            "entries": [record.to_listing(PREVIEW_CHARS) for record in records],
            "total": total,
            "limit": limit,
            "offset": max(0, offset),
        }

    @mcp.tool(meta=FOR_THE_MODEL)
    async def turn_read(ctx: Context, turn_id: int) -> dict[str, Any]:
        """One turn in full, by the id `turn_list` gave you.

        Everything that happened in it, in the order it happened: what the user
        said, every assistant message, the tool calls and what they answered.
        Your own history, like `turn_list` — an id only resolves inside it.

        Args:
            turn_id: The turn to read, as `turn_list` reported it.
        """
        agent, subagent = _caller(ctx)
        # `store_for`, for the reason `turn_list` gives: a read of one turn does
        # not need the embedding model, only the table the turn is in.
        store = await _on_thread(store_for, agent, subagent)
        record = await _on_thread(store.turn, turn_id)
        if record is None:
            raise ValueError(f"no turn {turn_id} in {describe((agent, subagent))}")
        return record.to_wire()

    return mcp


def main(argv: list[str] | None = None) -> int:
    args = parse_serve_args(argv, SERVER_NAME)
    configure_logging()
    config_path = find_config_path()
    config = load()

    address = config.server(CONFIG_KEY)
    logger.info(
        "serving %s on http://%s:%d%s (databases in %s)",
        SERVER_NAME,
        args.host or address.host,
        args.port or address.port,
        address.path,
        db_dir(),
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
