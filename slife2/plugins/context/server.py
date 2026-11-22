"""slife2-context — a conversation's memory, and the context it runs on.

**The turn log and the decision about it are one plugin, because they are one
subject.**  It records every turn, it answers what was said before, it decides
which turns the next turn runs on, and it is the thing that puts a conversation
back on its feet after a restart.  The turns themselves come from the library
(`slife2.libs.store.db`): the plugin imports the store directly for the restore
path rather than reaching it over MCP, so the whole plugin is that import with a
serve face on it.

Turns are a library, not a plugin
---------------------------------
The turns have exactly one writer — this plugin — and the catalogue has exactly
one writer — the hub; "one writer" is a property of a SQLite file rather than of
a server.  `TurnStore` is therefore imported by the process that writes it, and
this plugin is a serve face over that import: one holder of the schema, one
embedder hop, one startup pass over every file, for the half it owns.

The two decisions, and where they sit in a turn
-----------------------------------------------
`restore` runs when a conversation starts, and `rebuild` is the other half of
the first boundary's `_context_rebuild`:

```
send_message
  └─ the first message for a key → restore      ← the exit-time context, replayed
  └─ per turn, inside the lock:
       run the turn (the loop appends the message)
       first iteration boundary → _context_rebuild   ← keep ∪ recall, one model call
       save                                       the new id joins the live list
```

**`restore` is the harness's API and not the model's.**  `rebuild` is this
plugin's API and not the model's either — but it is reached through
`_context_rebuild`, a *harness tool*: LLM-visible, auto-invoked at the first
boundary, and written into the conversation as a pair whose answer reports what
was kept and recalled (`slife2.libs.turn.tools` tells the two words apart).  The
model has `turn_list`, `turn_read` and `_context_rebuild`; the discriminator
still decides the selection, and the footnote it reads on each turn is how it
says what it wants kept without calling anything
(`slife2.libs.turn.context`).

What is deliberately absent
---------------------------
**The trim.**  The ceiling is held by the per-turn rebuild selecting against the
window; a trim after each save would additionally bound what the selection
missed.  With the rebuild on, the *selection* is the bound, and the trim would
stand behind it as a guard for what no selection can see in advance — a turn
whose tool results ballooned while it ran.  That guard is a separate change and
it is named in SDD §14.2 rather than half-built here; what this plugin owes it is
the persisted list and the write that maintains it, which both are.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

from slife2.clients.embedder import EmbedderConnection
from slife2.helpers.threads import off_loop
from slife2.libs.host.config import (
    API_SERVER_NAMES,
    CONTEXT_SERVER_NAME,
    Config,
    ContextSettings,
    for_the_call,
    load_cached,
)
from slife2.libs.host.paths import agents_dir, turn_dbs
from slife2.libs.paging import page_window
from slife2.libs.plugin.audience import FOR_THE_MODEL
from slife2.libs.plugin.protocol import (
    close_server,
    conversation_reader,
    describe,
    house_server,
    open_server,
    serve_plugin,
    unreadable,
)
from slife2.libs.plugin.vocabulary import TURN_SUMMARIZE
from slife2.libs.store.answer import search_answer
from slife2.libs.store.db import (
    StaleSchema,
    TurnStore,
    assistant_message,
    read_store_for,
    store_for,
    user_message,
)
from slife2.libs.store.index import MAX_PAGE, Embedder, page_limit, time_window
from slife2.libs.store.textindex import terms
from slife2.libs.turn import context as decisions
from slife2.libs.turn.context import Decision
from slife2.libs.turn.messages import StreamChatResult, Usage
from slife2.libs.turn.tokens import estimate_turn_tokens

logger = logging.getLogger(__name__)

SERVER_NAME = CONTEXT_SERVER_NAME

#: This server's key in the config's `servers:` table, and the name the agent
#: server connects to it by.
CONFIG_KEY = "context"

#: The page a `turn_list` that names no size gets — the same convention every
#: listing in this system uses (`slife2.libs.paging.page_window`): a caller
#: that names no size must not get the smallest legal page.
DEFAULT_TURN_PAGE = 20

#: How much of a candidate a cross-encoder reads.  The turn's own exchange is
#: what relevance is judged on; a tool result would swamp it (results are
#: 56–99% of a turn's text) and the reranker's own context is shorter than a
#: long turn anyway.  The cut is bounded here rather than left to the endpoint:
#: the whole pool is one call, and a pool of long turns is slow and
#: rate-limited even when the model is free.
RERANK_DOC_CHARS = 2000


def _rerank_text(messages: list[dict[str, Any]]) -> str:
    """The exchange a cross-encoder judges: what was asked and what was answered.

    **Not the whole row.**  The vector leg reads the turn's reasoning because
    reasoning is the model's statement of what it is doing; a reranker is
    judging *relevance to a question*, and the question and the answer are the
    signal.  Tool results are 56–99% of a turn's text and would swamp the pair;
    they are what `turn_read` and the keyword leg are for.

    **The two halves keep their own room.**  The clip is a bound, not a summary:
    the whole pool is one call, and a pool of long turns is slow and
    rate-limited even when the model is free.  Clipping the *joined* text would
    let a long question erase the answer, though — the pair is the signal, and
    half a pair is not — so each half is clipped to its own share.
    """
    question, answer = user_message(messages), assistant_message(messages)
    if question and answer:
        share = (RERANK_DOC_CHARS - len("\n\n")) // 2
        return "\n\n".join([question[:share], answer[:share]])
    only = question or answer
    return only[:RERANK_DOC_CHARS]


INSTRUCTIONS = (
    "One conversation's memory, addressed by `(agent, subagent)`. Call "
    "`remember` after a turn: it is a record, not an interpretation, and it "
    "writes what it is given. `turn_list` browses, `turn_search` finds a turn by "
    "what it was about or by an exact string from its record (a path, a command, "
    "an error, a tool name), `turn_summarize` annotates one for the words leg, "
    "and "
    "`turn_read` returns one turn whole — all four are the model's, and none "
    "names an agent, because they answer about the conversation the call came "
    "from. `restore` and `rebuild` are the harness's: they decide which turns a "
    "conversation is made of, and no model calls them."
)


#: The discriminator call, as a seam: the conversation and the input in, whatever
#: the model said out.  A test injects one; production uses the model server
#: named by the conversation's model.
#:
#: `previous` and `repair` are the second half of one call's life — the reply
#: that could not be read, and why — and are empty on the first ask.  A test that
#: varies what the model said can ignore them unless it is about the repair.
class Discriminator(Protocol):
    def __call__(
        self,
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict[str, Any]],
        *,
        previous: str = "",
        repair: str = "",
    ) -> Awaitable[str]: ...


class Selector(Protocol):
    """The live compaction selector, as a seam: the view and its candidates in,
    whatever the model said out.

    The same two-reply life as `Discriminator` — `previous` and `repair` carry
    a reply that could not be read — and a test that varies the answer injects
    one instead of reaching a model server.
    """

    def __call__(
        self,
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict[str, Any]],
        selectable: list[int],
        *,
        usage: str = "",
        previous: str = "",
        repair: str = "",
    ) -> Awaitable[str]: ...


async def _ask_model(
    config: Config,
    clients: dict[str, Client],
    opening: asyncio.Lock,
    agent: str,
    subagent: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    previous: str = "",
    repair: str = "",
) -> str:
    """One `stream_chat` call, with no tools and nothing listening to it.

    **No progress handler**, which is what makes the call silent: the server
    reports progress only to a caller that asked for a token, so a discriminator
    turn reaches no transcript and no observer by construction rather than by a
    filter somebody has to remember.

    **A repair ask is the same call with the conversation extended**: the
    instruction, the refused answer as the model's own message, and the reason
    it was refused (`decisions.repair_note`).  All of it is a prompt for one
    call and none of it is stored — the repair is how a bad answer is corrected,
    not a turn.

    The client is opened once per *model server* and kept for the process — the
    agent server's arrangement, for the agent server's reason: a handshake per
    call is a handshake per call.  Keyed by URL because a server speaks one wire
    format for every provider that uses it.

    **No timeout of its own.**  This is a model call like the turn's own, so it
    is bounded where every model call is — the provider's own limits inside the
    model server — and a slow answer only makes the turn late.  A second, shorter
    clock here was a knob with no job: it turned a working-but-slow provider
    into a refused recall, and the refusal looked exactly like a broken one.

    **An answer this build cannot read is refused, not read as a silent one.**
    FastMCP leaves `CallToolResult.data` as `None` whenever the far end sent no
    structured content, and `StreamChatResult.from_wire(None)` builds an empty
    result — so a model server from another build would arrive here as "the model
    answered with nothing": `_discriminate` logs `recall_discriminator_unparsed`
    and keeps the context, and every rebuild for the rest of the session does the
    same, with the log pointing at the model's output rather than at the
    transport.  `slife2.clients.model` raises for exactly this on the loop's own
    hop, and this is the same check with the same sentence, because the cause is
    the same: a daemon from another build.
    """
    asked = [
        *messages,
        {"role": "user", "content": decisions.instruction()},
    ]
    if repair:
        # The model's own refused answer, then what was wrong with it.  An empty
        # answer has nothing to show, and a provider may refuse an empty
        # assistant message — the note alone is the correction then.
        if previous:
            asked.append({"role": "assistant", "content": previous})
        asked.append({"role": "user", "content": decisions.repair_note(repair)})
    return await _stream_chat(
        config,
        clients,
        opening,
        agent,
        subagent,
        model,
        asked,
        kind="discriminator",
    )


async def _ask_selector(
    config: Config,
    clients: dict[str, Client],
    opening: asyncio.Lock,
    agent: str,
    subagent: str,
    model: str,
    messages: list[dict[str, Any]],
    selectable: list[int],
    *,
    usage: str = "",
    previous: str = "",
    repair: str = "",
) -> str:
    """The live selector's model call, built from the numbered copy.

    **The ids are written here and nowhere else.**  `with_message_id` prefixes
    each message of this request; the record, the turn's saved row and every
    ordinary call keep the list they had.  The instruction names the candidates,
    the legality rule and the context figure the caller measured — the same
    figure the status bar and the turn prompt show.  The transport's rules are
    `_stream_chat`'s.
    """
    numbered = [
        decisions.with_message_id(message, index)
        for index, message in enumerate(messages)
    ]
    asked = [
        *numbered,
        {"role": "user", "content": decisions.live_instruction(selectable, usage)},
    ]
    if repair:
        if previous:
            asked.append({"role": "assistant", "content": previous})
        asked.append({"role": "user", "content": decisions.repair_note(repair)})
    return await _stream_chat(
        config,
        clients,
        opening,
        agent,
        subagent,
        model,
        asked,
        kind="compactor",
    )


async def _stream_chat(
    config: Config,
    clients: dict[str, Client],
    opening: asyncio.Lock,
    agent: str,
    subagent: str,
    model: str,
    asked: list[dict[str, Any]],
    *,
    kind: str,
) -> str:
    """One silent `stream_chat` call: the transport both decisions share.

    **No progress handler**, which is what makes the call silent: the server
    reports progress only to a caller that asked for a token, so a decision
    reaches no transcript and no observer by construction rather than by a
    filter somebody has to remember.

    The client is opened once per *model server* and kept for the process — the
    agent server's arrangement, for the agent server's reason: a handshake per
    call is a handshake per call.  Keyed by URL because a server speaks one wire
    format for every provider that uses it.

    **No timeout of its own.**  This is a model call like the turn's own, so it
    is bounded where every model call is — the provider's own limits inside the
    model server — and a slow answer only makes the turn late.  A second, shorter
    clock here was a knob with no job: it turned a working-but-slow provider
    into a refused decision, and the refusal looked exactly like a broken one.

    **An answer this build cannot read is refused, not read as a silent one.**
    FastMCP leaves `CallToolResult.data` as `None` whenever the far end sent no
    structured content, and `StreamChatResult.from_wire(None)` builds an empty
    result — so a model server from another build would arrive here as "the model
    answered with nothing": the caller logs an unparsed reply and keeps the
    context, and every later decision does the same, with the log pointing at
    the model's output rather than at the transport.  `slife2.clients.model`
    raises for exactly this on the loop's own hop, and this is the same check
    with the same sentence, because the cause is the same: a daemon from another
    build.
    """
    name, provider, settings = config.resolve(model)
    url = config.server(provider.api).url
    async with opening:
        if url not in clients:
            clients[url] = await open_server(
                url,
                name=API_SERVER_NAMES[provider.api],
                fallback_tool="stream_chat",
            )
    result = await clients[url].call_tool(
        "stream_chat",
        {
            "provider": name,
            "model": settings.model,
            "agent": agent,
            "subagent": subagent,
            # The model server's usage line says whose call it served and what
            # kind: reading the decision apart from the turn's own calls is the
            # whole reason the word travels.
            "kind": kind,
            "tools": [],
            "messages": asked,
        },
    )
    if not isinstance(result.data, dict):
        raise unreadable("the model server", "return a stream_chat result", result.data)
    try:
        return StreamChatResult.from_wire(result.data).text
    except KeyError as exc:
        raise unreadable(
            "the model server", "return a stream_chat result", result.data
        ) from exc


def build_server(
    config: Config,
    *,
    source: Callable[[], Config] | None = None,
    embedder: Embedder | None = None,
    ask: Discriminator | None = None,
    select: Selector | None = None,
) -> FastMCP:
    """Build the context server.

    `embedder` and `ask` are injectable so the whole plugin — save, read, recall,
    restore, rebuild — can be exercised over the in-memory transport with no
    network at all.  In production the embedder is opened once against the
    embeddings server, because a turn is written with its vector in one
    transaction and a store that cannot embed stores nothing; and `ask` is the
    real model call, opened once per model server.

    `ask` is the *decision's* seam rather than the transport's, which is the one
    worth having: what a test of a rebuild wants to vary is what the model
    answered, and a fake model server would make it vary the wire instead.
    """
    embedding = EmbedderConnection(config, embedder=embedder)
    injected = ask

    def current() -> Config:
        """The config this call is served from, not the one this daemon started on.

        This process is shared by every home on the machine, and the model a
        `rebuild` names comes from the caller's conversation.  Resolving that
        reference against the daemon's own `slife2.yaml` made every rebuild in a
        second home fail as a `ConfigError` — swallowed as "keep the context",
        so recall never ran there.  `for_the_call` is the rule the agent server
        and the hub already use.
        """
        return for_the_call(source, config)

    #: Files whose indexes have been brought up to date, once per process.  A
    #: file created after the startup pass is not re-synced: its own save writes
    #: the vector for the turn it is saving, and the only thing a sync would add
    #: is re-embedding turns that do not exist yet.
    synced: set[Path] = set()

    #: **The check and the sync are one step per file.**  Two callers reaching
    #: a file created after the startup pass together would both run `indexed`
    #: — the membership check sits before a network-length await — and both
    #: place vectors for the same turns: `turn_chunk` has no uniqueness on
    #: `(turn_id, chunk_index)`, so the duplicates persist, consume KNN places
    #: and inflate the index's vector count.  A lock per path makes the second
    #: caller wait and then see the mark.
    syncing: dict[Path, asyncio.Lock] = {}

    async def indexed(store: TurnStore, model: Embedder) -> None:
        """Bring one file up to date with `model`, or say why it cannot be."""
        await store.sync_indexes(model)
        status = store.index_status(model)
        if not status["ready"]:
            raise RuntimeError(
                f"the turns at {store.path} cannot be searched semantically: "
                + "; ".join(status["problems"])
            )
        synced.add(store.path)
        logger.info("indexed %s (%s)", store.path, status)

    async def sync_once(store: TurnStore) -> None:
        """Bring one file up to date once, whoever asks first (`syncing`)."""
        if store.path in synced:
            return
        lock = syncing.setdefault(store.path, asyncio.Lock())
        async with lock:
            if store.path not in synced:
                await indexed(store, await embedding.get())

    async def store_of(agent: str, subagent: str) -> TurnStore:
        """One conversation's store, with its indexes brought up to date."""
        store = await off_loop(store_for, agent, subagent)
        await sync_once(store)
        return store

    async def read_store_of(agent: str, subagent: str) -> TurnStore | None:
        """As `store_of`, for a read: `None` when the conversation was never written.

        **A read must not create the conversation it is reading.**  See
        `slife2.libs.store.db.read_store_for` — a stale subagent name would
        otherwise come back as a directory with no record.
        """
        store = await off_loop(read_store_for, agent, subagent)
        if store is not None:
            await sync_once(store)
        return store

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, Any]]:
        """Bring every turn file up to date before answering anything at all.

        **Nothing is served until this has finished**, because the alternative is
        a window in which a search answers from an index that is not the one it
        claims to be — and the thing that hides, a turn stored with no vector,
        cannot be seen from outside.  A changed embedding model makes this the
        expensive step: every turn in every file is embedded again, and that is
        the price of changing the model rather than a fault to be avoided.

        The pass belongs here because the turns do: a file belongs to the plugin
        that owns its schema, and a startup pass is part of owning one.
        """
        model = await embedding.get()
        # Every per-agent store, the agent's own and its subagents'.  Named by
        # `slife2.libs.host.paths` rather than globbed here: a `*/*.db` in this directory
        # is not a description of a conversation store — `schedule.db` lives
        # beside each `turns.db` and matched it — so this pass would open the
        # scheduler's ledger as a `TurnStore` and write the turn schema into it.
        for path in turn_dbs():
            try:
                await indexed(await off_loop(TurnStore, path), model)
            except StaleSchema:
                # The store has named the file and the column, which is the
                # report this pass exists to make.  **The pass continues**, and
                # that is not a lenient reading of "nothing is served until this
                # has finished": a scan of every known home is not one
                # conversation, and a file from an older build — possibly in a
                # home nobody is using — must not take every readable
                # conversation down with it.  Calls that do use the file still
                # fail, with the same sentence, at `store_of`.
                continue
        try:
            yield {}
        finally:
            # Both halves of what this process holds open: the embedder it opened
            # for the startup pass, and one connection per model server it was
            # asked to discriminate with.  Cleared as well as closed, because
            # this hook is not the process's lifetime over the in-memory
            # transport — a client left in place is one the next session is
            # handed already shut, and every `rebuild` of it would fail at its
            # first ask.
            await embedding.close()
            for client in list(model_clients.values()):
                await close_server(client)
            model_clients.clear()

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
        usage: dict[str, int] | None = None,
        who_helped: str = "",
        what_model: str = "",
        channel: str = "",
        summary: str = "",
        tags: str = "",
        created_at: str | None = None,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        """Persist one turn.

        A turn is one exchange: what the user said, and everything the agent did
        about it — assistant messages, tool calls and their results — in the
        order they happened.  Store it as it is; this is a record, not an
        interpretation.

        **The new turn joins the live context in the same transaction**, which is
        why this answers with its id: the caller appends that id to the list it
        is holding, and a caller that did not get one back would have to guess
        what the store had just written.

        Args:
            agent: Whose memory.  With `subagent` it names the database file, so
                conversations are isolated from each other by construction.
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
            usage: The bill's breakdown — `prompt_tokens`, `completion_tokens`,
                `cache_read`, `cache_creation` — as `Usage.to_wire()` spells it.
                `token_count` is its total; this is what a cache-hit ratio is
                computed from.  Omitted when the caller measured none.
            who_helped: The agent that answered.
            what_model: Which model answered, as `provider/model`.
            channel: Where the turn came in from — `tui`, or a conversation id.
                Empty when the caller has nothing to say about it.
            summary: The summary a `turn_summarize` call wrote for this turn in
                flight; the agent server captures it at the save point.
            tags: The tags from that call, comma-separated.
            created_at: When the user pressed enter.  Defaults to now, which is
                the same moment to within a hop for a caller on loopback.
            completed_at: When the assistant finished.  Defaults to now.

        Returns:
            `turn_id` of the stored turn, and the file it went into.
        """
        store = await store_of(agent, subagent)
        turn_id = await store.save_turn(
            messages=messages,
            embedder=await embedding.get(),
            token_count=token_count,
            context_tokens=context_tokens,
            usage=Usage.from_wire(usage) if usage is not None else None,
            who_helped=who_helped,
            what_model=what_model,
            channel=channel,
            summary=summary,
            tags=tags,
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
        conversation is a fact the caller's side holds (`slife2.clients.toolclient`
        attaches it to the call) and this side reads; the model neither sees it
        nor can write it.

        Raises:
            ToolError: If the call arrived without one.  Every real caller is
                the hub, which forwards what the agent gave it, so this is a
                caller that reached this server directly — a test, or a plugin
                that is not the agent — and it is better told than quietly served
                whatever it named.

        **The refusal is the house sentence with this plugin's own `why`**, which
        is the hole `slife2.libs.plugin.audience.unaddressed` leaves for exactly
        this case: the situation is the shared one — a call that did not say
        whose it is, on a process that serves everybody — but the *reason* is
        this plugin's, because the road that always supplies an identity is the
        hub, and a reader who is told so knows where to look.
        """
        return conversation_reader(
            "memory to read",
            "the memory",
            why="it is reached through the toolhub, which forwards the "
            "caller's identity (`slife2.libs.plugin.audience`)",
        )(ctx)

    # This docstring is the model's description, so it says what the tool
    # answers and nothing about why it is shaped this way.  The two arguments
    # that reasoning belongs to, kept here instead: **a bound narrows what you
    # see and never decides it** — a default window would hide turns with
    # nothing saying they were hidden, which is the one thing a browse must not
    # do — and **there is no argument naming whose memory to read**, because a
    # model that could name one could read somebody else's.
    @mcp.tool(meta=FOR_THE_MODEL)
    async def turn_list(
        ctx: Context,
        since: str | None = None,
        until: str | None = None,
        limit: int = DEFAULT_TURN_PAGE,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Browse what was said before, newest first, one line per turn.

        Each line carries `turn_id`, when the turn was, and what was asked and
        answered, both cut short. Read one whole with `turn_read`. Your own
        history: no argument names whose memory to read.

        Args:
            since: Lower bound on when the turn was written — an ISO date or
                datetime, or one of: today, yesterday, tomorrow, now,
                last|this week|month|quarter|year, or '<N> day(s)|week(s)|month(s)|year(s) ago'.
                Omit for no lower bound.
            until: Upper bound on the same grammar. A date means the whole day.
                Omit for no upper bound.
            limit: How many turns this page may hold. Omit for the default
                page; zero or less means the same.
            offset: How many turns to skip — that is how you page back.

        Returns:
            `entries` — `turn_id`, `created_at`, `user_message` and
            `assistant_message` per turn — and `total`, how many turns match the
            filters, so `offset + len(entries) < total` says whether more remain.
        """
        # `read_store_for` and not `store_of`: reading history needs the turn
        # table and nothing else, so it must not need an embedding model.  A
        # browse that failed because the embeddings endpoint was down would be
        # a read coupled to a write's dependency.  Nor may it create the file:
        # a browse of a conversation that has never run is the empty page, not
        # a store this call invented (`read_store_for`).  Building the store is
        # still a blocking call — it opens the file and runs the schema — so it
        # goes off the loop like every other one here.
        agent, subagent = _caller(ctx)
        # **The window is the caller's input, so it is judged before the history
        # is.**  A spelling the grammar cannot place is refused even when the
        # conversation has never run and the page is empty — the refusal is
        # about what was asked, not about what was found.
        time_window(since, until)
        # The applied numbers are the answer's, so a caller pages by the size
        # this call actually used (`page_window`'s convention).
        size, start = page_window(limit, offset, unset=DEFAULT_TURN_PAGE, cap=MAX_PAGE)
        store = await off_loop(read_store_for, agent, subagent)
        if store is None:
            return {"entries": [], "total": 0, "limit": size, "offset": start}
        records, total = await off_loop(
            store.turns, since=since, until=until, limit=size, offset=start
        )
        return {
            "entries": [record.to_listing() for record in records],
            "total": total,
            "limit": size,
            "offset": start,
        }

    @mcp.tool(meta=FOR_THE_MODEL)
    async def turn_read(ctx: Context, turn_id: int) -> dict[str, Any]:
        """One turn in full, by the id `turn_list` gave you.

        Everything it holds, in the order it happened: what was said, every
        assistant message, the tool calls and their answers. Your own history,
        like `turn_list` — an id resolves inside it and nowhere else.

        Args:
            turn_id: The turn to read, as `turn_list` reported it.
        """
        agent, subagent = _caller(ctx)
        # `read_store_for`, for the reason `turn_list` gives: a read of one turn
        # does not need the embedding model, only the table the turn is in — and
        # a conversation that was never written has no turn to find.
        store = await off_loop(read_store_for, agent, subagent)
        record = await off_loop(store.turn, turn_id) if store is not None else None
        if record is None:
            raise ValueError(f"no turn {turn_id} in {describe((agent, subagent))}")
        wire = record.to_wire()
        # **The bill fields answer the operator's question, not this reader's.**
        # `token_count` was the sum of every model call the turn made, each
        # carrying the conversation of its moment; `context_tokens` was that
        # conversation's final size; `usage` is the price.  A reader of this
        # turn acts on its content, and the result is bounded before it enters
        # the conversation.  `who_helped` is the same name on every row of this
        # file, so a read of that conversation gains nothing from it.
        wire.pop("token_count", None)
        wire.pop("context_tokens", None)
        wire.pop("who_helped", None)
        wire.pop("usage", None)
        return wire

    @mcp.tool(meta=FOR_THE_MODEL)
    async def turn_search(
        ctx: Context,
        keywords: list[str] | None = None,
        sentences: list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 10,
        exclude_context: bool = False,
    ) -> dict[str, Any]:
        """Search your own history by what a turn was about, or by what it did.

        Words and meaning decide the answer together: `keywords` are the words
        and exact strings a turn is found by — what was asked and answered, and
        the tool calls and their results — and `sentences` are what it was
        about. A path, a command, an error string or a tool name from the record
        is therefore as searchable as the subject the turn discussed. A turn
        both find ranks first. Each hit is the one line `turn_list` gives, plus
        `similarity` — the meaning score when meaning measured the turn, `null`
        when only its words matched. Read one whole with `turn_read`.

        **A hit need not contain the keywords.**  Meaning ranks the nearest
        turns whether or not they carry them, and a low `similarity` says the
        words were not found; there is no exact-only mode, so a nonsense query
        can come back with the history's closest turns.

        The whole log is searched by default, the turns the conversation is
        already running on included; `exclude_context` leaves those out.

        Args:
            keywords: Words to look for — names, paths, an exact string. They
                rank turns that carry them first, but they are not a filter:
                when `sentences` is empty the same words are also read by
                meaning, so a hit may be a turn that does not contain them.
            sentences: What to recall, in your own words, one sentence per idea.
                Read by meaning; another sentence is another chance to be found.
            since: Lower bound on when the turn was written — an ISO date or
                datetime, or one of: today, yesterday, tomorrow, now,
                last|this week|month|quarter|year, or '<N> day(s)|week(s)|month(s)|year(s) ago'.
                Omit for no lower bound.
            until: Upper bound on the same grammar. A date means the whole day.
                Omit for no upper bound.
            limit: How many hits this page may hold.
            exclude_context: Leave the turns the conversation is already
                running on out of the answer. False by default: a turn in the
                context is still one you may want to read or name.

        Returns:
            The one search answer: `results` — `turn_id`, `created_at`,
            `user_message`, `assistant_message` and `similarity` per hit, best
            first — `best` (the highest meaning score on the page, `null` when
            no hit was scored by meaning), `confidence` (`sure`, `weak` or
            `none`: how sure this page is), `browsed` (always false here;
            `turn_list` is the browse) and `limit` (the page size this answer
            was cut to).
        """
        agent, subagent = _caller(ctx)
        words = [word for word in (keywords or []) if terms(word)]
        asked = [sentence.strip() for sentence in (sentences or []) if sentence.strip()]
        if not words and not asked:
            raise ToolError(
                "turn_search needs something to look for — `keywords`, "
                "`sentences`, or `turn_list` to browse the history without one."
            )
        store = await read_store_of(agent, subagent)
        if store is None:
            # A conversation that was never written has no history to search,
            # and the answer is the empty page rather than a store invented to
            # have searched it — the window is still judged first, for
            # `turn_list`'s reason.
            time_window(since, until)
            return search_answer([], scores=[], worded=False, limit=page_limit(limit))
        # The store's keyword leg reads one string, and its terms are ANDed
        # either way; those same words are the meaning query when no sentence is
        # given.
        query = " ".join(words)
        # The search runs on the caller's `keyword_weight`, the same vote the
        # rebuild's recall reads: one home, one weight between the legs.
        # **The live list is the caller's choice, not the tool's rule.**  A turn
        # in the context is still history the model may want to read or name,
        # so it is included unless `exclude_context` asks otherwise; only the
        # rebuild's recall treats "in hand" as "not missing" (`_recall_into`).
        live = await off_loop(store.context_turns) if exclude_context else []
        ranked = (
            await store.recall(
                query=query,
                sentences=asked,
                embedder=await embedding.get(),
                since=since,
                until=until,
                limit=limit,
                exclude=live,
                keyword_weight=current().context.keyword_weight,
            )
        )[: page_limit(limit)]
        records = {
            record.turn_id: record
            for record in await off_loop(
                store.turns_by_ids, [turn_id for turn_id, _ in ranked]
            )
        }
        entries: list[dict[str, Any]] = []
        for turn_id, similarity in ranked:
            record = records.get(turn_id)
            if record is None:
                continue
            entry = record.to_listing()
            entry["similarity"] = similarity
            entries.append(entry)
        # **Word evidence comes from the keyword leg, not from a missing
        # score.**  A hit found by both legs carries the meaning score, so a
        # page whose every hit really does contain the words has no `None`
        # similarity — and reporting `confidence: none` for an exact match
        # hides the one certain fact the page has.  The page's own ids are what
        # is asked about: evidence outside the page is not evidence about it.
        worded = bool(
            words
            and await off_loop(
                store.matched_words,
                query,
                [entry["turn_id"] for entry in entries],
            )
        )
        return search_answer(
            entries,
            scores=[entry["similarity"] for entry in entries],
            worded=worded,
            limit=page_limit(limit),
        )

    @mcp.tool(name=TURN_SUMMARIZE, meta=FOR_THE_MODEL)
    async def turn_summarize(
        ctx: Context,
        turn_id: int | None = None,
        summary: str | None = None,
        tags: str | None = None,
    ) -> dict[str, Any]:
        """Write a summary and tags for a turn, so a keyword search can find it.

        A summary is one or two sentences about what the turn was about; tags
        are comma-separated words. Either or both. They join what a search by
        words reads, so the turn can be found by words it never said.

        Omit `turn_id` to annotate the turn being run: the annotation is
        captured now and written when this turn is saved. With a `turn_id`, an
        earlier turn is annotated at once.

        Args:
            turn_id: The turn to annotate; omit for the turn being run.
            summary: What the turn was about, in a sentence or two.
            tags: Comma-separated words to file it under.

        Returns:
            `status` — `captured` for the turn being run, `updated` for one
            written now — and `turn_id`, which is `null` for a captured one.
        """
        agent, subagent = _caller(ctx)
        summary = (summary or "").strip()
        tags = (tags or "").strip()
        if not summary and not tags:
            raise ToolError(f"{TURN_SUMMARIZE} needs a summary and/or tags")
        if turn_id is None:
            return {
                "status": "captured",
                "turn_id": None,
                "message": (
                    "annotation captured for the current turn — written when "
                    "this turn is saved"
                ),
            }
        store = await off_loop(store_for, agent, subagent)
        changed = await off_loop(
            store.update_summary, turn_id, summary=summary, tags=tags
        )
        if not changed:
            raise ToolError(f"no turn {turn_id} in {describe((agent, subagent))}")
        logger.info("summarized turn %s for %s", turn_id, describe((agent, subagent)))
        return {"status": "updated", "turn_id": turn_id}

    # --- the two decisions ---------------------------------------------------
    #
    #  Harness tools, unmarked, and the caller is the agent server.  They carry
    #  the conversation id as an ordinary argument rather than in `_meta`, and the
    #  difference from `turn_list` above is the point: a model *chooses* to read
    #  its history and must not be able to say whose, while the agent server is
    #  the party that already knows whose conversation it is running.  Same
    #  split as `remember`.

    def _head(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The caller's system message, if it leads with one.

        Kept rather than re-rendered: the prompt is a property of the
        conversation and was rendered when it started, and a rebuild that
        re-rendered it would be this plugin deciding something it was not asked
        about (`slife2.libs.turn.prompt`).
        """
        if messages and messages[0].get("role") == "system":
            return messages[0]
        return None

    def _rows(store: TurnStore, turn_ids: list[int]) -> list[dict[str, Any]]:
        return [record.to_wire() for record in store.turns_by_ids(turn_ids)]

    def _decision_words(decision: Decision) -> str:
        """The decision's two fields as `recall_decided` spells them.

        **The fields are otherwise invisible.**  `rebuild` reports its outcome
        (`turns`, `recalled`) and the two fields that chose it are consumed and
        dropped, so a line reading `turns=0 recalled=0` could not say whether
        the model cleared the turns, kept none to begin with, or what it went to
        the log for.  That was the question a live turn raised and no artifact
        could answer.

        The keep list's boundary values are spelled the way the reply spells
        them (`KEEP_ALL` / `KEEP_NONE`) rather than as `None` and `[]`, because
        the reader is comparing this line with the model's own answer.  An
        absent recall is `none`; a present one is the condition as JSON, so a
        sentence with Chinese in it stays readable in the log.
        """
        if decision.keep is None:
            keep = decisions.KEEP_ALL
        elif not decision.keep:
            keep = decisions.KEEP_NONE
        else:
            keep = ",".join(str(turn_id) for turn_id in decision.keep)
        recall = (
            "none"
            if decision.recall is None
            else json.dumps(decision.recall, ensure_ascii=False)
        )
        return f"keep={keep} recall={recall}"

    def _in_hand(
        messages: Sequence[dict[str, Any]],
        turn_ids: Sequence[int],
        rerank: str = "",
        discriminator: str = "",
    ) -> dict[str, Any]:
        """The answer when the turns in hand are what the turn runs on.

        **Four of `rebuild`'s exits are this one outcome** — a discriminator
        that decided nothing, a decision that asks for nothing, a decision that
        equals what is held, and a selection that cannot be fetched — and they
        are one dict because they are one thing: nothing was fetched, nothing
        was written, and the caller's list stands.  Spelled out four times they
        were four chances to describe that differently, and four places to add a
        key to.

        There is no other exit named "the switch is off": `ContextSettings` has
        no switch, and nothing here reads one.  The discriminator that answers
        nothing is the fourth, and `rebuild` logs it as its own reason
        (`no_decision`, not `context_sufficient`) because it is not a decision
        the model made.

        `kept` is what the caller has in hand, `recalled` none of it — and the
        two together are what `rebuild` always reports, so a reader of the
        answer never has to know which of the four ways it was reached to read
        the counts.

        `rerank` is the stage's report when a recall *did* run and the
        selection still equals what is in hand.  The exits that never asked
        for a recall leave it empty; they did not lose a reranker, they never
        needed one.

        `discriminator` is why no decision was made — the one exit above that is
        a failure rather than an answer.  It is what the caller reads to say the
        recall *failed* rather than found nothing; the four exits are still one
        answer, and only this one has something to report.
        """
        return {
            "messages": list(messages),
            "turn_ids": list(turn_ids),
            "changed": False,
            "kept": len(turn_ids),
            "recalled": 0,
            "rerank": rerank,
            "discriminator": discriminator,
        }

    @mcp.tool
    async def restore(
        agent: str,
        messages: list[dict[str, Any]],
        subagent: str = "",
    ) -> dict[str, Any]:
        """Put a conversation back on the context it had when it last stopped.

        Called **once, when a conversation starts** — the first message under a
        key — which is the moment a process that restarted, or a loop that was
        reaped for being idle, finds out it is continuing something.

        The list is replayed **verbatim**: in its own order, with no ceiling
        re-slicing, because the list already encodes what was kept and what was
        dropped and re-selecting would undo both.  A conversation whose context
        has never been recorded gets its own messages back unchanged, which is
        the honest answer for a key that is genuinely new.

        Args:
            agent: Whose conversation.
            messages: What the caller holds now — normally the system prompt
                alone.  Its head is preserved as the first message of the answer.
            subagent: Which of that agent's conversations; empty for its own.

        Returns:
            `messages` (the rebuilt list), `turn_ids` (what it was built from —
            the caller should adopt it, since a turn named by the list and no
            longer stored is dropped here and must not be kept in hand), and
            `turns` (the stored rows themselves).

            `turns` is here for the reader that wants the conversation rather
            than the context: a terminal that has just opened has to *show* what
            the previous one was showing, and the message list will not do —
            timestamps, the channel a turn arrived on and the turn's own
            boundaries are the record's and not the model's.  One read answers
            both readers for that reason.
        """
        store = await read_store_of(agent, subagent)
        if store is None:
            # A conversation that was never written is this list unchanged — the
            # honest answer for a genuinely new key, and for a subagent that has
            # been removed, whose name must not be recreated by a read.
            return {"messages": list(messages), "turn_ids": [], "turns": []}
        turn_ids = await off_loop(store.context_turns)
        if not turn_ids:
            return {"messages": list(messages), "turn_ids": [], "turns": []}
        rows = await off_loop(_rows, store, turn_ids)
        rebuilt = decisions.consistent(
            decisions.messages_from_turns(rows, head=_head(messages))
        )
        restored = [int(row["turn_id"]) for row in rows]
        logger.info(
            "restored %d turn(s) for %s", len(restored), describe((agent, subagent))
        )
        return {"messages": rebuilt, "turn_ids": restored, "turns": rows}

    def _window(model: str) -> int:
        """How many tokens the model this conversation runs on can hold.

        The model entry states it, and the loader refuses an entry that does
        not, so this is always a positive number — the recall's budget is a
        fraction of it, and there is no second way to size a recall.

        **A model the config cannot resolve is a failure, not a zero.**  A
        window that cannot be *asked for* fails the turn, rather than leaving a
        recall to spend by retrieval width with no token cap (SDD Appendix C: a
        weaker path is not a substitute for a missing capability).
        """
        return int(current().resolve(model)[2].context_window)

    async def _discriminate(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict[str, Any]],
    ) -> tuple[Decision | None, str]:
        """One model call — or two, when the first answer could not be read.

        **Never raises, and the one retry is a repair.**  A provider failure and
        a reply that is not the requested object both return `None`, and the
        caller's fallback — keep the context — is perfectly good.  A provider
        failure is *not* asked again: there is no answer to correct.  An
        unreadable reply is different — the provider answered, the answer is in
        hand, and the refusal names the field — so the model is asked once more
        with its own reply and that reason (`repair_note`).  A second refusal
        ends it; the repair is one round, not a loop.

        **No clock of its own.**  The waits are the model service's, exactly as
        for the turn's own calls: a slow provider makes the turn late, which the
        design accepts, rather than making the recall fail for a reason that
        says nothing about the answer.

        The second half is *why* there is no decision — `""` when the model
        answered, otherwise `"failed"` or `"unparsed"`.  It rides the rebuild's
        answer because `recalled: 0` is the same number for a recall that found
        nothing and one nobody asked, and the person watching is owed the
        difference.

        The instruction travels as a user message *appended to the conversation*,
        which is what lets a follow-up be recognised as one: the subject of "what
        about the other one" is in the list above it and nowhere else.  There is
        no separate input — the conversation is the input.
        """
        try:
            text = await ask_model(agent, subagent, model, messages)
        except Exception as exc:  # noqa: BLE001 — a fallback, not a failure mode
            logger.info("recall_discriminator_failed reason=%s", type(exc).__name__)
            logger.debug("the discriminator call raised", exc_info=True)
            return None, "failed"
        # **The raw reply, for the one question the parsed answer cannot
        # answer**: *why* a model that wrote something the parser read a
        # certain way wrote it that way.  DEBUG because it is the model's text
        # and this line is for whoever is already looking at the protocol; the
        # parsed fields are the INFO line `rebuild` writes.
        logger.debug("recall_discriminator_reply attempt=first %r", text)
        decision, refusal = decisions.parse_with_reason(text)
        if decision is not None:
            return decision, ""
        logger.info("recall_discriminator_unparsed reason=%s", refusal)
        # **The one retry, and it is not the same bet as asking again.**  The
        # model has already answered, so the provider works and the round trip
        # is priced; what failed is the shape, and the shape is what the refusal
        # names.  Asking once more with the answer and the reason is a
        # correction, not a blind retry — and a model that cannot write the
        # object twice falls back exactly as before.
        try:
            corrected = await ask_model(
                agent,
                subagent,
                model,
                messages,
                previous=text,
                repair=refusal,
            )
        except Exception as exc:  # noqa: BLE001 — the same fallback as above
            logger.info(
                "recall_discriminator_repair_failed reason=%s", type(exc).__name__
            )
            logger.debug("the repair call raised", exc_info=True)
            return None, "failed"
        logger.debug("recall_discriminator_reply attempt=repair %r", corrected)
        decision, refusal = decisions.parse_with_reason(corrected)
        if decision is not None:
            logger.info("recall_discriminator_repaired")
            return decision, ""
        logger.info("recall_discriminator_unparsed reason=%s attempt=repair", refusal)
        return None, "unparsed"

    #: The discriminator call: a test's, or one client per model server kept for
    #: the process.
    model_clients: dict[str, Client] = {}
    opening = asyncio.Lock()
    ask_model: Discriminator = injected or (
        lambda agent, subagent, model, messages, *, previous="", repair="": _ask_model(
            current(),
            model_clients,
            opening,
            agent,
            subagent,
            model,
            messages,
            previous=previous,
            repair=repair,
        )
    )

    #: The live selector: a test's, or the same client arrangement one call
    #: further, with its own instruction and its own `kind`.
    selector_injected = select
    select_model: Selector = selector_injected or (
        lambda agent, subagent, model, messages, selectable, *, usage="", previous="", repair="": (
            _ask_selector(
                current(),
                model_clients,
                opening,
                agent,
                subagent,
                model,
                messages,
                selectable,
                usage=usage,
                previous=previous,
                repair=repair,
            )
        )
    )

    async def _select_live(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict[str, Any]],
        selectable: list[int],
        usage: str = "",
    ) -> tuple[list[int] | None, str]:
        """One selector call — or two, when the first answer could not be read.

        **Never raises, and the one retry is a repair**, the rule `_discriminate`
        states: a provider failure has no answer to correct and is `failed`; an
        answer in hand this build cannot read is asked to correct itself once,
        with the refusal naming the field.  A second refusal is `unparsed`, and
        the caller compacts nothing — the turn continues and its next call is
        the judge of whether that was enough.
        """
        try:
            text = await select_model(
                agent, subagent, model, messages, selectable, usage=usage
            )
        except Exception as exc:  # noqa: BLE001 — a fallback, not a failure mode
            logger.info("live_selector_failed reason=%s", type(exc).__name__)
            logger.debug("the selector call raised", exc_info=True)
            return None, "failed"
        logger.debug("live_selector_reply attempt=first %r", text)
        drop, refusal = decisions.parse_live(text)
        if drop is not None:
            return drop, ""
        logger.info("live_selector_unparsed reason=%s", refusal)
        try:
            corrected = await select_model(
                agent,
                subagent,
                model,
                messages,
                selectable,
                usage=usage,
                previous=text,
                repair=refusal,
            )
        except Exception as exc:  # noqa: BLE001 — the same fallback as above
            logger.info("live_selector_repair_failed reason=%s", type(exc).__name__)
            logger.debug("the repair call raised", exc_info=True)
            return None, "failed"
        logger.debug("live_selector_reply attempt=repair %r", corrected)
        drop, refusal = decisions.parse_live(corrected)
        if drop is not None:
            logger.info("live_selector_repaired")
            return drop, ""
        logger.info("live_selector_unparsed reason=%s attempt=repair", refusal)
        return None, "unparsed"

    @mcp.tool
    async def select_live(
        agent: str,
        messages: list[dict[str, Any]],
        selectable: list[int],
        context_window: int,
        model: str = "",
        subagent: str = "",
        context_tokens: int = 0,
    ) -> dict[str, Any]:
        """Decide which of a running turn's live messages it no longer needs.

        Called by the agent server's `_turn_compact`, **not by a model**: the
        ids are the caller's view numbering, they mean nothing outside that
        call, and nothing is stored with them.  `messages` is the view — the
        list the model will run on, not the turn's record — and `selectable`
        names the view positions that are candidates.

        `context_tokens` is the conversation's last measured call and
        `context_window` its model's window: the same pair the status bar
        shows, rendered by the same `context_usage`, so the selector and the
        person read one figure.

        The answer is `compact` (the positions to drop, as the model named them)
        and `decision` (`""` for an answer, otherwise `failed` or `unparsed`).
        The caller applies the positions as they came: the legality of a tool
        exchange is a prompt rule, and a harness that repaired a split one would
        be making a decision the model was asked to make.

        Args:
            agent: Whose conversation.
            messages: The view the turn is running on, in order.
            selectable: The view positions the model may drop.
            context_window: The conversation's model window, in tokens.
            model: Which model this conversation runs on, as `provider/model`.
            subagent: Which of that agent's conversations; empty for its own.
            context_tokens: The last call's measured size.
        """
        usage, _used = decisions.context_usage(context_tokens, context_window)
        drop, decision = await _select_live(
            agent, subagent, model, messages, selectable, usage
        )
        return {"compact": drop or [], "decision": decision}

    @mcp.tool
    async def rebuild(
        agent: str,
        messages: list[dict[str, Any]],
        turn_ids: list[int],
        subagent: str = "",
        model: str = "",
        carried: int = 0,
        publish: bool = True,
    ) -> dict[str, Any]:
        """Decide what this turn runs on: keep ∪ recall.

        Called from `_context_rebuild`, **after** the turn's message was
        appended — at the first boundary, or mid-turn after a cut-in.  The
        current turn has no id and is not in `turn_ids`; `carried` carries it
        back verbatim, so the selection is the historical turns only.

        One model call decides: which of the turns in hand to keep, and what to
        recall from the turn log.  The two are joined by id and never reconciled,
        so an empty recall is harmless and only an explicit `"clear"` empties the
        context (`slife2.libs.turn.context` is where that is argued).  Every
        turn in hand is excluded from the recall: the discriminator read all of
        them, so a turn it dropped from the keep-list was dropped deliberately,
        and a turn in both would spend retrieval depth and budget on something
        the union would collapse anyway.  The conversation is the input; there is
        no separate prompt, and the decision's sentences are the recall's
        questions.

        **A decision that asks for exactly what is in hand rebuilds nothing at
        all**, which is the common case and what keeps a call this expensive from
        being paid for nothing.

        **A private rebuild chooses and publishes nothing.**  It exists for the
        agent task, whose turn leaves no record: the task needs a decided
        context for its own turn, and the conversation it stands in for must not
        be moved by a turn that is not part of its sequence.  The answer has the
        same shape; only the write is absent.

        Args:
            agent: Whose conversation.
            messages: The conversation as the caller holds it.  This is what the
                discriminator reads — not a re-render of the stored turns, which
                would be a second opinion about what is in hand.
            turn_ids: The turns those messages came from, in order.  The
                keep-list is read as an intersection with this, so an id that has
                left the context cannot be kept by naming it.
            subagent: Which of that agent's conversations; empty for its own.
            model: Which model this conversation runs on, as `provider/model`.
                The window the selection is sized against is *this* model's, so
                an empty reference is sized against the config's default and says
                so in the log.
            carried: How many messages at the **end** of `messages` no turn in
                `turn_ids` accounts for.  They are re-appended verbatim after the
                rebuilt turns, which is what keeps a cancelled turn's repair —
                the one message a conversation can hold with no turn behind it —
                from being destroyed by a rebuild that was never told about it.
                A count rather than a list, because the caller is the party that
                knows what it appended and a list of messages sent twice is a
                second copy to keep in step.
            publish: Whether this decision is written back to the conversation's
                live-context list.  **A private rebuild is a clone**: an agent
                task records nothing, so its context selection must not move the
                conversation it stands in for.  The selection is returned to the
                caller and nothing is written.

        Returns:
            `messages` (what the turn should run on), `turn_ids` (what it was
            built from), `changed` (whether anything moved), `recalled` (how
            many of the selected turns came from the turn log rather than from
            in hand), `rerank` — `""` when the cross-encoder ran or no
            question was asked, otherwise `"unavailable"`, `"failed"` or
            `"empty"`: the recall kept its fused order and the caller shows
            that rather than swallowing it — and `discriminator`, `""` when
            there was a decision, otherwise `"failed"` or `"unparsed"`: the
            recall never ran, and `recalled: 0` would otherwise read as one
            that found nothing.
        """
        store = await store_of(agent, subagent)
        # **The conversation is the input.**  The rebuild runs at the first
        # boundary, after the turn's message was appended (or mid-turn, after a
        # cut-in); there is no separate prompt to pass.
        decision, discriminator = await _discriminate(agent, subagent, model, messages)
        # **Two ways to have no decision, and the log keeps them apart.**  Both
        # fall back to the turns in hand, and neither may be reported as the
        # other: this line is the only sight anybody gets of the discriminator
        # (`recall_discriminator_failed` and `_unparsed` are written a moment
        # above and say *why* there was none), and a single `context_sufficient`
        # for both would make counting "the model decided the context was enough"
        # count every failed call and every unreadable reply as a decision the
        # model never made.  A decision that asks for nothing is the model's
        # word; no decision at all is the transport's.
        if decision is None:
            logger.info("recall_not_needed reason=no_decision")
            return _in_hand(messages, turn_ids, discriminator=discriminator)
        # **What was decided, before what it led to.**  Every line below reports
        # an outcome — `recall_not_needed`, `recall_abandoned`, `recall_rebuilt`
        # — and none of them carries the two fields that chose it.  A live
        # rebuild that logged `turns=0 recalled=0` was indistinguishable from a
        # model that kept nothing and one that cleared what it had, which is the
        # question this line exists to answer without a replay.
        logger.info("recall_decided %s", _decision_words(decision))
        if decision.asks_for_nothing:
            # The ordinary case, and the one this fallback is for: the turns in
            # hand are the answer.  Nothing is fetched, nothing is rendered,
            # nothing is written — and the list already on disk still describes
            # what the caller is holding, which is why it is read once before
            # this answer counts.
            logger.info("recall_not_needed reason=context_sufficient")
            return _in_hand(messages, turn_ids)

        # A keep-list is an **intersection** with what is in hand.  A model that
        # names a turn it can no longer see has made a mistake it cannot be told
        # about — there is no turn to refuse — so the id is dropped and the log
        # says so, which is where whoever has to explain a missing turn looks.
        in_hand = {int(turn_id) for turn_id in turn_ids}
        base = (
            list(turn_ids)
            if decision.keep is None
            else [turn_id for turn_id in decision.keep if turn_id in in_hand]
        )
        unknown = (
            []
            if decision.keep is None
            else [turn_id for turn_id in decision.keep if turn_id not in in_hand]
        )
        if unknown:
            logger.info("recall_kept_ids_unknown ids=%s", unknown)

        recalled: list[int] = []
        rerank = ""
        if decision.recall is not None:
            recalled, rerank = await _recall_into(
                store, decision.recall, base, turn_ids, model
            )

        target = decisions.union(base, recalled)
        if target == sorted(in_hand):
            # The decision happens to equal what is in hand even though it was
            # asked for — same set, so the same context, and re-rendering it
            # would only cost a fetch and a write.
            logger.info("recall_not_needed reason=context_unchanged")
            return _in_hand(messages, turn_ids, rerank=rerank)

        rows = await off_loop(_rows, store, target)
        if len(rows) != len(target):
            # A named turn with no row: the list and the log disagree, which
            # means a file was rebuilt or pruned underneath.  Keeping the context
            # is the answer, because a context assembled from part of a selection
            # is neither what the model decided nor what it had.
            logger.warning("recall_abandoned reason=unfetchable")
            return _in_hand(messages, turn_ids, rerank=rerank)

        rebuilt = decisions.consistent(
            decisions.messages_from_turns(rows, head=_head(messages))
        )
        # The carried tail goes back verbatim, after the turns.  It is the one
        # part of the list no turn in `turn_ids` describes, so it is the one part
        # a rebuild cannot reconstruct — and dropping it would silently delete
        # the user's own message from a turn that was cut off mid-flight.
        #
        # **`carried` is a count *from the end*, and it is the caller's word that
        # the run is the end.**  Nothing here can check that: the ids and the
        # messages arrive as two shapes and are joined by neither, so a caller
        # whose unbacked messages sat somewhere else would be handed back the
        # wrong slice and told nothing.  `slife2.plugins.agent.core.rebuild_into`
        # is the one caller that can pass a nonzero `carried`, and `Loop.covered`
        # is where it keeps the run at the end — a turn that follows unbacked
        # messages does not join `turn_ids`, which is what keeps the count
        # honest.  A task restores its list and carries nothing.
        if carried > 0:
            rebuilt.extend(dict(message) for message in messages[-carried:])
        # **A private rebuild stops here.**  The decision belongs to the
        # caller's own turn and is returned without a write: a task records
        # nothing, so its selection must not move the conversation it stands in
        # for.
        if publish:
            # Published before the caller is told, and a failure here **fails
            # the turn**.  The list is authoritative (SDD §5.5): the caller
            # adopts this answer as its context, so returning the in-memory
            # rebuild while the *persisted* list did not move leaves the two
            # disagreeing, and the next start replays the stale one — a
            # conversation that comes back one turn short, silently.  The
            # caller that cannot have the write is told rather than handed a
            # context the log will not have on the next start.
            await off_loop(store.set_context_turns, target)
        logger.info(
            "recall_rebuilt turns=%d recalled=%d published=%s",
            len(target),
            len(set(recalled) - set(base)),
            publish,
        )
        return {
            "messages": rebuilt,
            "turn_ids": target,
            # **Always a change here, and it is not a comparison any more.**  The
            # branch above returns when the decision equals what is in hand, so
            # reaching this point means the list this turn runs on is not the one
            # the conversation had — which is the whole of what this reports.
            "changed": True,
            # What survived the decision, and what came back from the log.  The
            # two add to `len(target)`, which is what the turn runs on.
            "kept": len(base),
            "recalled": len(set(recalled) - set(base)),
            # The precision stage's report — empty when it ran or no question
            # was asked.  A recall that kept the fused order is a fact the
            # caller shows, not one it silently absorbs.
            "rerank": rerank,
            # There *was* a decision — this path is only reached past the
            # `None` branch — so no failure is owed to the caller.
            "discriminator": "",
        }

    async def _recall_into(
        store: TurnStore,
        condition: dict[str, Any],
        base: list[int],
        in_hand: Sequence[int],
        model: str,
    ) -> tuple[list[int], str]:
        """Run one recall condition, fit it, and report the rerank stage.

        The second half of the answer is the stage's status — `""` when it ran
        or no question was asked, `"unavailable"` when the provider serves no
        cross-encoder, `"failed"` when the call raised or could not be mapped
        back, and `"empty"` when it answered with no scores.  The caller shows
        it: a recall that kept its fused order is a fact the person watching is
        owed, not a fallback to swallow.

        **The recall is a funnel, and the floors and the budget decide
        membership.**  The store fuses the legs' pages into a pool that is
        their union (twice the configured width for one question, more for
        several); `min_similarity` drops measured noise; the cross-encoder
        reorders the survivors and drops those below `rerank_min_score`; the
        kept turns' estimated cost is subtracted from `ceiling × window`, and
        the budget spends what is left.  **No count cuts the selection**: a
        turn that passed both floors is a turn the budget may buy, and a count
        applied after the ranker would discard relevant turns on the strength
        of an order the chronological rebuild does not keep.

        **The pool only bounds what the ranker may see.**  Each leg retrieves
        `recall_page_limit` candidates, and the cross-encoder judges their fused
        union; there is no count cap on the pool itself.  The whole pool is one
        cross-encoder call, and a slow or rate-limited reranker is paid for
        before every turn.

        Every cap here is the caller's and not the store's, for the split of
        duties: the store ranks, the policy caps, and the policy depends on a
        model's window and on what the decision kept — neither of which the
        store knows.

        **One budget, and it is the ceiling's.**  The kept turns are estimated
        first, and the recall may spend what they leave below
        `ceiling × window` — no second fraction of its own.  Keeping less
        leaves the recall more; a kept context at the ceiling leaves it
        nothing, so the two halves cannot overfill the window by construction.

        **Every turn in hand is excluded from what is recalled.**  The
        discriminator read the whole conversation, so a turn it left out of the
        keep-list was left out deliberately; a recall that answered a query with
        it would overturn that, and spend retrieval depth and a share of the
        budget on a turn the caller already holds.  The exclusion is handed to
        the store, which can look past those turns for more rather than
        filtering a short answer here.

        **The decision's sentences are the questions.**  There is no separate
        input to append: the discriminator read the conversation, and a
        sentence it wrote is the subject it chose to ask the log for.  A
        keyword-only recall keeps its query as the one meaning question, and a
        period recall has no question at all.
        """
        query = str(condition.get("query") or "")
        raw_sentences = condition.get("sentences")
        sentences = [
            str(sentence).strip()
            for sentence in (raw_sentences if isinstance(raw_sentences, list) else [])
            if str(sentence).strip()
        ]
        if query and not terms(query):
            # **A query with no term is not a keyword leg.**  The keyword leg
            # refuses such a thing — `"***"`, `"—"` — with `EmptyQuery`, and the
            # store's own contract is that an empty query reaching the full-text
            # index is a syntax error and embeds to noise.  With a sentence
            # beside it the recall still asks a question, so only the query is
            # dropped; without one, dropping the query and running the period
            # would be a different selection from the one the model asked for,
            # so the whole recall is dropped and the decision's other half, the
            # keep-list, runs on its own — SDD §5.5's outcome when a
            # rebuild cannot do its job: keep the context it had.
            if not sentences:
                logger.info("recall_query_has_no_term")
                return [], ""
            logger.info("recall_query_dropped")
            query = ""
        since = condition.get("since") or None
        until = condition.get("until") or None
        anchor = condition.get("anchor") or None
        if (query or sentences) and anchor:
            # Relevance decides which end a question is spent from, so an anchor
            # beside one is not consulted.  Logged rather than dropped silently,
            # because a model that wrote it meant something by it.
            logger.info("recall_anchor_ignored anchor=%s", anchor)
            anchor = None

        # The meaning leg's questions are the decision's sentences; a period
        # recall has no question, and a keyword-only recall has only its query.
        asker = await embedding.get() if (query or sentences) else None

        # The policy is the caller's home's, like the window and the model:
        # this daemon serves every home, and `context:` is a section of the
        # caller's `slife2.yaml` (`current`).  Read from the config this process
        # started on, an edit would reach the window and not the numbers sized
        # against it, and a second home would be graded by the first home's
        # ceiling.
        policy = current().context
        if not model:
            # The promise `rebuild`'s `model` argument makes: an empty reference
            # is sized against the config's default, and says so.
            logger.info("recall_window_default_model")
        window = _window(model)
        # What the kept turns already cost, which is what the recall's budget is
        # the headroom *below*.  Summed here and not by the store: the store
        # ranks candidates, and how much room is left is a fact about a
        # selection the caller made.
        base_cost = sum((await off_loop(_cost_of, store, base)).values())
        # One budget, the ceiling's: what the kept turns leave below it.
        # A kept context at the ceiling leaves the recall nothing, and one
        # already over it is a negative budget — both answered by the
        # `budget <= 0` exit below rather than by a recall that overfills.
        budget = int(window * policy.ceiling) - base_cost
        if budget <= 0:
            logger.info("recall_no_headroom base=%d", base_cost)
            return [], ""

        # The pool is the ranker's, and it is a configured retrieval width: no
        # count cuts the selection afterwards.  The store applies its own page
        # bound to every query, so a configured width larger than one page is
        # capped there rather than here.
        ranked = await store.recall(
            query=query,
            sentences=sentences,
            embedder=asker,
            since=since,
            until=until,
            anchor=anchor,
            limit=policy.recall_page_limit,
            # **What is in hand, not only what was kept.**  The discriminator
            # read every turn in the conversation, so a turn it dropped was
            # dropped deliberately; and a turn in both would spend retrieval
            # depth or budget on something already held.  The exclusion is what
            # makes the recall the half that looks elsewhere.
            exclude=in_hand,
            keyword_weight=policy.keyword_weight,
        )
        gated = decisions.gate(ranked, policy.min_similarity)
        order = [turn_id for turn_id, _ in gated]
        if not order:
            return [], ""
        # The cross-encoder sees the question, not the whole decision: the
        # sentences are the subject the model wrote (and already carry the
        # person's input when it was read as one), and a keyword-only decision
        # has the query words as its subject.  Several sentences are joined
        # into one question because that is the one call the endpoint takes;
        # the retrieval above is what gives each sentence its own chance to
        # find a turn.  A period-only recall has no question, so it keeps its
        # time order (`fit_window`) and never asks a ranker to order a period
        # by relevance.
        ask = " ".join(sentences).strip() or query.strip()
        rerank = ""
        if ask:
            order, rerank = await _rerank_into(store, order, ask, policy)
            if not order:
                return [], rerank
        # **No count cut.**  What passed the floors is what the budget may buy;
        # a count here would make membership depend on a rank order the rebuilt
        # list then discards.  A time-ordered pool is bounded the same way, by
        # the budget, from the end its anchor named.
        costs = await off_loop(_cost_of, store, order)
        fit = decisions.fit_budget if (query or sentences) else decisions.fit_window
        return fit(order, costs, budget), rerank

    async def _rerank_into(
        store: TurnStore, order: list[int], ask: str, policy: ContextSettings
    ) -> tuple[list[int], str]:
        """Reorder the pool with the provider's cross-encoder.

        **A precision stage over the whole fused pool.**  Each leg retrieves
        `recall_page_limit` candidates, so a turn the two legs ranked low is
        still judged; `rerank_min_score` is the membership decision this stage
        makes, and its order matters only where the token budget spends down
        it.

        **An unavailable or failing reranker is not a failed recall.**  The
        provider may serve no cross-encoder (`rerank_model` empty), the call may
        time out, or the answer may be malformed; all three keep the fused
        order and log why, in the same spirit as a discriminator that could not
        answer.  A *short* answer is malformed too: it is refused at both hops
        (`slife2.clients.embedder.RemoteEmbedder`, `slife2.plugins.llm.embeddings_server`)
        rather than read as "the missing candidates scored nothing".  What is
        *not* kept is a candidate the reranker scored below `rerank_min_score`
        — that is the stage doing its job.  A threshold that rejects the whole
        pool ends the recall: that is the ranker's verdict, not a reason to
        fall back to the unjudged order.
        """
        if not ask or not order:
            return order, ""
        rows = await off_loop(store.turns_by_ids, order)
        if not rows:
            return order, "failed"
        ids = [int(record.turn_id) for record in rows]
        documents = [_rerank_text(record.messages) for record in rows]
        try:
            scored = await embedding.rerank(ask, documents, top_n=len(documents))
        except Exception as exc:  # noqa: BLE001 — an optional stage's failure
            logger.info("recall_rerank_failed reason=%s", type(exc).__name__)
            logger.debug("the rerank call raised", exc_info=True)
            return order, "failed"
        if scored is None:
            # No `rerank_model` on this endpoint: a capability answer, not a
            # failure, and nothing to log per turn.
            return order, "unavailable"
        if not scored:
            logger.info("recall_rerank_empty")
            return order, "empty"
        kept: list[int] = []
        for index, score in scored:
            if score >= policy.rerank_min_score:
                turn_id = ids[index]
                if turn_id not in kept:
                    kept.append(turn_id)
        logger.info("recall_reranked candidates=%d kept=%d", len(ids), len(kept))
        if not kept:
            logger.info("recall_reranked_nothing")
            return [], ""
        return kept, ""

    def _cost_of(store: TurnStore, turn_ids: list[int]) -> dict[int, int]:
        """What each of those turns is estimated to cost, for the budget.

        An **estimate**, and `slife2.libs.turn.tokens` is where that is argued: nothing can
        have measured a turn that has not been sent, and the number this feeds is
        a fraction of a window rather than a report about one.
        """
        return {
            record.turn_id: estimate_turn_tokens(record.messages)
            for record in store.turns_by_ids(turn_ids)
        }

    return mcp


def main(argv: list[str] | None = None) -> int:
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        build=lambda config: build_server(config, source=load_cached),
        logger=logger,
        note=lambda _config: f"turns in {agents_dir()}",
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
