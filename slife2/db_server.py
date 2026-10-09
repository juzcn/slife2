"""slife2-db — persistence: what slife2 keeps, one database per client id.

The turns are what it keeps today, and the **tool catalogue** is the second
thing — the rows and two indexes of v1's `tools.db`, in `<data>/slife2.db/
tools.db`, one file for the whole data directory rather than one per agent,
because the tool set is not a property of a conversation.  It is served here
rather than in the toolhub for the reason this module's opening paragraph gives:
a catalogue is rows and two indexes over them, and the store, the embedder and
the normalization they need are already in this plugin.

That makes the two halves of this server the same *kind* of thing — a thing
worth keeping, and the API to keep it — and it draws the line that keeps the
hub and this plugin out of each other's half: **which tools exist is the
hub's decision** (the servers, the names, who may call them), and **what is
known about them is this file's record** (the rows, the load state, the two
indexes, the budget).  The `tool_*` tools below are that record's public API,
callable by any peer over MCP; the hub is the caller today.

Keeping is the whole of the job — it does
not summarise, does not decide what mattered, does not put anything back into a
conversation, and does not decide *whether* a turn is worth keeping: the caller
does that, and the agent server is the caller that knows a worker's turns are
not.  A turn is stored as it happened — with one
deliberate exception, an oversized tool result, which is kept as an announced
head-and-tail digest rather than in full (see `slife2.db`) — so a question
this plugin cannot answer today can be asked of the same rows later without a
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
dependency of this plugin: it cannot be switched off, and an endpoint that
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
from slife2.db import (
    PREVIEW_CHARS,
    Embedder,
    ToolStore,
    TurnStore,
    page_limit,
    store_for,
)
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
from slife2.paths import db_dir, tools_db

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
    "returns one turn whole — which is the honest thing for a plugin that "
    "stores without judging. Both of those are the model's, and neither names an "
    "agent: they answer about the conversation the call came from. The same "
    "plugin keeps the tool catalogue, which the toolhub decides: the `tool_*` "
    "tools record what tools exist and find them by keyword and by meaning."
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
        plugin working with fewer abilities.
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

    #: The tool catalogue, built once per process, and whether its indexes have
    #: been brought up to date.  One file for the whole data directory, so unlike
    #: the turn stores there is no per-client id step: `--agent` partitions the
    #: turns and nothing else.
    catalogue: ToolStore | None = None
    catalogue_ready = False

    async def catalogue_store() -> ToolStore:
        """The tool catalogue, with both its indexes current.

        The config decides four things the store needs and cannot work out for
        itself: how many tools the model may hold, which sources are wanted every
        turn, which ones are switched off, and which ones it names at all — the
        last of those being what the boot pass needs to tell a stale row from one
        the hub is about to speak for.  Read here because this is the process
        that reads the config: the file is the operator's, and one reader is how
        "off is not down" means the same thing to the boot pass and to the
        budget.
        """
        nonlocal catalogue, catalogue_ready
        store = catalogue
        if store is None:
            # **The config decides one thing here now, and that is the budget.**
            # Which sources are wanted every turn, which are switched off and
            # which are named at all used to be read off `config.tools` — a
            # section this process has no business in, and one that belongs to
            # the plugin holding those servers.  So `autoload` arrives with each
            # merge and with each eviction, the operator's switch arrives as a
            # row's own status, and what is left for the boot pass is the names
            # of our own peers: the set slife2 starts.
            store = await _on_thread(
                ToolStore,
                tools_db(),
                threshold=config.tool_load.threshold,
                known=frozenset(config.plugins()),
            )
            catalogue = store
        if not catalogue_ready:
            # The same pass the turn files get, for the same reason: an index
            # built by another model (or another set of rules) is an index this
            # one cannot search, and a changed model re-embeds every tool.
            status = await store.sync_indexes(await embeddings())
            catalogue_ready = True
            logger.info("indexed the tool catalogue at %s (%s)", store.path, status)
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
        # The catalogue too, and for the same reason: a file whose vectors came
        # from another model, or whose rows predate this build, has to be put
        # right before anything reads it.  The first start pays for every tool
        # of every server; after that this is one read of `meta` and a query
        # that returns no rows.
        await catalogue_store()
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
                plugin that is not the agent — and it is better told than
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
            # The limit that *produced* this page, not the one asked for.  The
            # docstring above tells a caller to page with `offset + len(entries)
            # < total`, and that arithmetic is only right against the size the
            # page was actually built with — a request for 1000 answered with
            # `limit: 1000` and 200 rows skips the 800 in between.
            "limit": page_limit(limit),
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

    # --- the tool catalogue ---------------------------------------------------
    #
    #  The other thing this plugin keeps, and the same kind of API: the
    #  record's own operations, named for what they do to the record rather than
    #  for who calls them.  None of these carries the model's audience mark —
    #  they are the hub's tools, and the hub's own rule (`slife2.audience`) keeps
    #  them out of the model's list without a second filter here.
    #
    #  Two of them are worth reading twice, because they are where the layering
    #  shows.  `tool_merge` takes a source's whole list and the *facts* about it
    #  — the hub decided what the tools are called and who may call them, and
    #  this side decides nothing but how they are stored.  `tool_set_load`
    #  answers with a word rather than a sentence: what a refusal means to a
    #  model is the hub's to phrase, and a store that wrote prose would be the
    #  second place model-facing text lived.

    @mcp.tool
    async def tool_merge(
        source: str,
        category: str,
        tools: list[dict[str, Any]],
        autoload: bool = False,
    ) -> dict[str, Any]:
        """Merge one source's whole tool list into the catalogue.

        Four outcomes and no fifth: a name that is not here is added, one the
        source dropped is deleted, one whose columns changed is updated, and one
        already identical is left alone.  So a steady state — the hub asks before
        every model call — writes nothing at all.

        The list is the *whole* truth about that source: what a server offers is
        known only by asking it, so a name missing from the list is a tool it no
        longer has.

        Args:
            source: The server or plugin these tools came from — the key
                every row is owned by, and what `tool_injectable` filters on.
            category: `plugin` (ours), `mcp` or `rest` (somebody else's), or
                `skill`/`cli` for a family that declares rows and has no
                connection behind them.
            tools: One entry per tool: `name` (what the model calls, and the
                row's identity — two sources cannot offer one), `description`,
                `remote_name` (what the far end calls it, which differs when the
                name has been sanitised), and `schema` (the parameter JSON, or
                `'n/a'` when it declares nothing).

        Returns:
            `inserted`, `updated`, `reconnected` (rows whose source is answering
            again), `purged`, `skipped` — names, and a count for `skipped`.

        Raises:
            ValueError: If a name already belongs to another source.  A name is
                a row's identity, so this is refused rather than written over —
                and it fails this source's list, not the whole catalogue.
        """
        store = await catalogue_store()
        return await store.merge(
            source, category, tools, embedder=await embeddings(), autoload=autoload
        )

    @mcp.tool
    async def tool_source_state(source: str, state: str) -> dict[str, Any]:
        """Record the runtime's verdict on one source: `enabled` or `error`.

        Called when the hub has just listed a source, and when a link died or a
        connect would not start.  It says whether that source's tool list is in
        hand — which is the only thing either side can act on — and it never
        touches a source the config switches off: `disabled` is the operator's
        answer and this is the runtime's.

        Returns:
            `changed`: how many rows moved.
        """
        store = await catalogue_store()
        return {"changed": await _on_thread(store.set_source_state, source, state)}

    @mcp.tool
    async def tool_injectable(sources: list[str]) -> dict[str, Any]:
        """The tools the model may be given now: loaded, and owned by a live source.

        **`sources` is the caller's, because liveness is the one thing a database
        cannot know.**  Which sources are answering is a fact about the
        connections the caller holds — the hub's own to the plugins, and the ones
        the plugins hold on its behalf; this side has no way to ask, so the
        caller says which sources it is holding a tool list from.

        A name beginning with `_` is the harness's own and is not in this answer —
        with one exception, `_func_tool_unload`, which the model has too because
        the harness's trim is recorded as a call to it.  See `_injectable_sql` in
        `slife2.db`, which is where both halves of that rule are.

        **The budget is not applied here.**  Trimming the list is a turn-boundary
        decision and it has its own tool (`tool_evict`), because a gate that
        evicted would be taking tools away underneath a model still using them.

        Returns:
            `tools`: whole rows, schema included, for the caller to advertise.
        """
        store = await catalogue_store()
        return await _on_thread(store.injectable, sources)

    @mcp.tool
    async def tool_evict(
        sources: list[str], autoload: list[str] | None = None
    ) -> dict[str, Any]:
        """Trim the loaded set to `tool_load.threshold`, least recently used first.

        **Used**, which is the stamp `tool_touch` writes, and not merely when
        the row entered the list — a tool the model has been calling is the last
        thing the budget should take, whatever order things were loaded in.

        Called by the harness at a turn boundary — the hub's `_func_tool_unload`
        is what carries it, and the agent server invokes that before it saves a
        turn.  Nothing of ours is evicted and neither is anything marked
        `autoload: true`: the budget bounds somebody else's ninety tools, and
        `tools`/`loaded` in `tool_sources` are how the effect is seen.

        Args:
            sources: The live sources, as `tool_injectable` takes them — the
                budget bounds what the model is *holding*, so a server that is
                down cannot have its rows evicted by a trim it is not part of.

        Returns:
            `unloaded`: the names the budget took out, which is what the caller
            reports.  Empty when the list was already within it.
        """
        store = await catalogue_store()
        return {
            "unloaded": await _on_thread(store.evict, sources, autoload=autoload or [])
        }

    @mcp.tool
    async def tool_route(name: str) -> dict[str, Any]:
        """The row for one advertised name — how a call finds its way.

        What a call needs and what the model's list does not carry: which source
        owns the name, and what that source calls the tool itself.  A call is
        gated on there being something behind the name, never on the load state:
        a name the model just found with `tool_search` is a name it can use.

        Returns:
            `tool`: the row, or `None` when no such name is in the catalogue.
        """
        store = await catalogue_store()
        return {"tool": await _on_thread(store.route, name)}

    @mcp.tool
    async def tool_sources() -> dict[str, Any]:
        """Per source: how many tools it has, and how many the model holds.

        Two numbers, because "why is my tool missing" needs both: a source with
        ninety tools and none loaded is a healthy server the model has not asked
        anything of yet, and `tools` falling to zero is a server that is not
        listing what it used to.
        """
        store = await catalogue_store()
        return {"sources": await _on_thread(store.source_counts)}

    @mcp.tool
    async def tool_search(
        query: str,
        category: str = "",
        source_id: str = "",
        status: str = "",
        load_status: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        """Find a tool by keyword and by meaning — a hybrid search, fused by rank.

        Two legs over the same rows: `bm25` over what a tool is called and
        described, and a cosine distance over a vector of the same text, fused
        because their scores are not on one scale and a rank is comparable by
        construction.  A tool both legs found outranks one only a single leg did.

        An empty query **browses** rather than returning nothing: the filters
        alone are how "what is in this category" becomes answerable.

        Args:
            query: What to look for.  Empty browses, ordered by category and
                name.
            category: `plugin`, `mcp`, `rest`, `skill` or `cli`; empty for any.
            source_id: One owner's name; empty for any.
            status: `enabled`, `disabled` (the config switched it off) or
                `error` (its source is not answering); empty for any.
            load_status: `loaded`, `unloaded` or `n/a`; empty for any.
            limit: How many results at most.

        Returns:
            `results`, best first: `name`, `description`, `category`,
            `source_id`, `status`, `load_status`, `schema_bytes` (a size, not
            the schema — what it tells a reader is whether the tool declares
            anything at all), and `similarity` where the semantic leg found it.
            `browsed` is true when there was no query.
        """
        store = await catalogue_store()
        return await store.search(
            query,
            embedder=await embeddings(),
            limit=limit,
            category=category,
            source_id=source_id,
            status=status,
            load_status=load_status,
        )

    @mcp.tool
    async def tool_set_load(name: str, load_status: str) -> dict[str, Any]:
        """Put one tool in the model's list, or take it out.

        The answer is a *fact* — one of a closed set of words — and not a
        sentence, because what a refusal means to a model is the caller's to
        phrase:

        * `loaded` / `unloaded` — the row moved.
        * `already` — it is where the caller wants it.
        * `unknown` — no such name.
        * `no_load_state` — a skill is a document, not something to load.
        * `disabled` / `error` — its owner is switched off, or is not answering.

        Args:
            name: The advertised name, as `tool_search` reports it.
            load_status: `loaded` or `unloaded`.

        Returns:
            `outcome`, and `tool` — the row as it is after the move, or `None`
            when there was no such row.
        """
        store = await catalogue_store()
        return await _on_thread(store.set_load, name, load_status)

    @mcp.tool
    async def tool_touch(name: str) -> dict[str, Any]:
        """Mark one tool as *called*, which is what the eviction order reads.

        The hub calls this after every tool call it routes — it is the process
        that makes the call, so it is the process that knows.  Without it the
        budget would be ordering by when things were loaded, and a long turn
        would evict the very tool it was using.

        Returns:
            `changed`: 1 when the row was stamped, 0 when there is no such row.
        """
        store = await catalogue_store()
        return {"changed": await _on_thread(store.touch, name)}

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
