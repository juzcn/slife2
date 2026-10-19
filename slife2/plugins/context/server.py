"""slife2-context — a conversation's memory, and the context it runs on.

**The turn log and the decision about it are one plugin, because they are one
subject.**  This is v1's `memdb`, ported: it records every turn, it answers what
was said before, it decides which turns the next turn runs on, and it is the
thing that puts a conversation back on its feet after a restart.  The turns
themselves come from the library (`slife2.libs.store.db`), and that is v1's arrangement too
— there, the main process imports `memdb.store` directly for the restore path
rather than reaching it over MCP, and here the whole plugin is that import with a
serve face on it.

Why the turns moved out of `slife2-db`
--------------------------------------
They used to be served by the db plugin, beside the tool catalogue.  Two things
were wrong with that, and they are the same thing said twice.  The first is that
a *store* which is shared by two clients has to be a process, and neither of
these is: the turns have exactly one writer (this plugin) and so does the
catalogue (the hub), and "one writer" is a property of a SQLite file rather than
of a server.  The second is that v1's own answer was a library — `memdb` ships as
a plugin *and* is imported, and `slife --headless` restores its session from
`SessionStore` with no MCP transport in the path at all.

So the db plugin is gone.  What it was for — one holder of the schema, one
embedder hop, one startup pass over every file — is what this plugin and the hub
each do for the half they own, from the same library.

The two decisions, and where they sit in a turn
-----------------------------------------------
`restore` runs when a conversation starts, and `rebuild` before every turn:

```
send_message
  └─ the first message for a key → restore      ← the exit-time context, replayed
  └─ per turn, inside the lock, before the user's message:
       rebuild                                  ← keep ∪ recall, one model call
       run the turn (the loop appends the message)
       save                                       the new id joins the live list
```

**Both are the harness's API and neither is the model's** — which is not the same
thing as a *harness tool*: those are LLM-visible and auto-invoked, and these two
are never in the model's list at all (`slife2.libs.turn.tools` is where the two words are
told apart).  The model has `turn_list`
and `turn_read` — a model that can read its history is the point of a turn log —
and it has no `rebuild`: what the context *is* is not something a model gets to
ask for, and the footnote it reads on each turn is how it says what it wants kept
without ever calling anything (`slife2.libs.turn.context`).

What is deliberately not ported
-------------------------------
**The trim.**  In v1 the ceiling is held by two mechanisms: the per-turn rebuild
selects against the window, and a trim after each save bounds what the selection
missed.  With the rebuild on, the *selection* is the bound and the trim stands
behind it as a guard for what no selection can see in advance — a turn whose tool
results ballooned while it ran.  That guard is a separate change and it is named
in DESIGN.md §9 rather than half-built here; what this plugin owes it is the
persisted list and the write that maintains it, which both are.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.clients.embedder import EmbedderConnection
from slife2.helpers.threads import off_loop
from slife2.libs.host.config import API_SERVER_NAMES, CONTEXT_SERVER_NAME, Config
from slife2.libs.host.paths import agents_dir, turn_dbs
from slife2.libs.plugin.audience import FOR_THE_MODEL, request_client, unaddressed
from slife2.libs.plugin.protocol import (
    close_server,
    describe,
    house_server,
    open_server,
    serve_plugin,
    unreadable,
)
from slife2.libs.store.db import TurnStore, store_for
from slife2.libs.store.index import Embedder, page_limit
from slife2.libs.store.textindex import terms
from slife2.libs.turn import context as decisions
from slife2.libs.turn.context import Decision
from slife2.libs.turn.messages import StreamChatResult
from slife2.libs.turn.tokens import estimate_turn_tokens

logger = logging.getLogger(__name__)

SERVER_NAME = CONTEXT_SERVER_NAME

#: This server's key in the config's `servers:` table, and the name the agent
#: server connects to it by.
CONFIG_KEY = "context"

INSTRUCTIONS = (
    "One conversation's memory, addressed by `(agent, subagent)`. Call "
    "`remember` after a turn: it is a record, not an interpretation, and it "
    "writes what it is given. `turn_list` browses, `turn_search` finds a turn by "
    "what it was about, and `turn_read` returns one turn whole — all three are "
    "the model's, and none names an agent, because they answer about the "
    "conversation the call came from. `restore` and `rebuild` are the harness's: "
    "they decide which turns a conversation is made of, and no model calls them."
)

#: The discriminator call, as a seam: the conversation and the input in, whatever
#: the model said out.  A test injects one; production uses the model server
#: named by the conversation's model.
Discriminator = Callable[
    [str, str, str, list[dict[str, Any]], str],
    Awaitable[str],
]


async def _ask_model(
    config: Config,
    clients: dict[str, Client],
    opening: asyncio.Lock,
    agent: str,
    subagent: str,
    model: str,
    messages: list[dict[str, Any]],
    user_input: str,
) -> str:
    """One `stream_chat` call, with no tools and nothing listening to it.

    **No progress handler**, which is what makes the call silent: the server
    reports progress only to a caller that asked for a token, so a discriminator
    turn reaches no transcript and no observer by construction rather than by a
    filter somebody has to remember.

    The client is opened once per *model server* and kept for the process — the
    agent server's arrangement, for the agent server's reason: a handshake per
    call is a handshake per call.  Keyed by URL because a server speaks one wire
    format for every provider that uses it.

    **An answer this build cannot read is refused, not read as a silent one.**
    FastMCP leaves `CallToolResult.data` as `None` whenever the far end sent no
    structured content, and `StreamChatResult.from_wire(None)` builds an empty
    result — so a model server from another build arrived here as "the model
    answered with nothing": `_discriminate` logged `recall_discriminator_unparsed`
    and kept the context, and every rebuild for the rest of the session did the
    same, with the log pointing at the model's output rather than at the
    transport.  `slife2.clients.model` raises for exactly this on the loop's own
    hop, and this is the same check with the same sentence, because the cause is
    the same: a daemon from another build.
    """
    name, provider, settings = config.resolve(model)
    url = config.server(provider.api).url
    async with opening:
        if url not in clients:
            clients[url] = await open_server(
                url,
                name=API_SERVER_NAMES[provider.api],
                fallback_tool="stream_chat",
                timeout=_T.timeouts.work.model_call,
            )
    asked = [
        *messages,
        {"role": "user", "content": decisions.instruction(user_input)},
    ]
    result = await clients[url].call_tool(
        "stream_chat",
        {
            "provider": name,
            "model": settings.model,
            "agent": agent,
            "subagent": subagent,
            # The model server's usage line says whose call it served and what
            # kind: this one is the recall decision, and reading it apart from
            # the turn's own calls is the whole reason the word travels.
            "kind": "discriminator",
            "tools": [],
            "messages": asked,
        },
        timeout=_T.timeouts.work.model_call,
    )
    if not isinstance(result.data, dict):
        raise unreadable("the model server", "return a stream_chat result", result.data)
    return StreamChatResult.from_wire(result.data).text


def build_server(
    config: Config,
    *,
    embedder: Embedder | None = None,
    ask: Discriminator | None = None,
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

    #: Files whose indexes have been brought up to date, once per process.  A
    #: file created after the startup pass is not re-synced: its own save writes
    #: the vector for the turn it is saving, and the only thing a sync would add
    #: is re-embedding turns that do not exist yet.
    synced: set[Path] = set()

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

    async def store_of(agent: str, subagent: str) -> TurnStore:
        """One conversation's store, with its indexes brought up to date."""
        store = await off_loop(store_for, agent, subagent)
        if store.path not in synced:
            await indexed(store, await embedding.get())
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

        This is the pass that used to run in the db plugin's lifespan.  It runs
        here now because the turns do: a file belongs to the plugin that owns its
        schema, and a startup pass is part of owning one.
        """
        model = await embedding.get()
        # Every per-agent store, the agent's own and its subagents'.  Named by
        # `slife2.libs.host.paths` rather than globbed here: a `*/*.db` in this directory
        # is not a description of a conversation store — `schedule.db` lives
        # beside each `turns.db` and matched it — so this pass would open the
        # scheduler's ledger as a `TurnStore` and write the turn schema into it.
        for path in turn_dbs():
            await indexed(await off_loop(TurnStore, path), model)
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

        **The new turn joins the live context in the same transaction**, which is
        why this answers with its id: the caller appends that id to the list it
        is holding, and a caller that did not get one back would have to guess
        what the store had just written.

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
            channel: Where the turn came in from — `tui`, or a client id.
                Empty when the caller has nothing to say about it.
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
        hub, and a reader who is told so knows where to look.  It was raised as a
        bare `ValueError` with the sentence in it, which crossed the wire as the
        same `ToolError` (FastMCP wraps what a handler raises) while being the
        one refusal in this system that was not written in the shared words.
        """
        found = request_client(ctx)
        if found is None:
            raise ToolError(
                unaddressed(
                    "memory to read",
                    "the memory",
                    why="it is reached through the toolhub, which forwards the "
                    "caller's identity (`slife2.libs.plugin.audience`)",
                )
            )
        return found

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
        limit: int = 20,
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
            limit: How many turns this page may hold.
            offset: How many turns to skip — that is how you page back.

        Returns:
            `entries` — `turn_id`, `created_at`, `user_message`,
            `assistant_message` and `token_count` per turn — and `total`, how
            many turns the window holds, so `offset + len(entries) < total`
            says whether more remain.
        """
        # `store_for` and not `store_of`: reading history needs the turn table
        # and nothing else, so it must not need an embedding model.  A browse
        # that failed because the embeddings endpoint was down would be a read
        # coupled to a write's dependency.  Building the store is still a
        # blocking call — it opens the file and runs the schema — so it goes off
        # the loop like every other one here.
        agent, subagent = _caller(ctx)
        store = await off_loop(store_for, agent, subagent)
        records, total = await off_loop(
            store.turns, since=since, until=until, limit=limit, offset=offset
        )
        return {
            "entries": [record.to_listing() for record in records],
            "total": total,
            "limit": page_limit(limit),
            "offset": max(0, offset),
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
        # `store_for`, for the reason `turn_list` gives: a read of one turn does
        # not need the embedding model, only the table the turn is in.
        store = await off_loop(store_for, agent, subagent)
        record = await off_loop(store.turn, turn_id)
        if record is None:
            raise ValueError(f"no turn {turn_id} in {describe((agent, subagent))}")
        return record.to_wire()

    @mcp.tool(meta=FOR_THE_MODEL)
    async def turn_search(
        ctx: Context,
        query: str,
        since: str | None = None,
        until: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search your own history by what a turn was about.

        Two legs decide the answer: the words a turn is found by, and what it
        was about. A turn both legs find ranks first. Each hit is the one line
        `turn_list` gives, plus `similarity` — the meaning score when the
        meaning leg measured the turn, `null` when only its words matched.
        Read one whole with `turn_read`.

        Args:
            query: What the turn was about.
            since: Lower bound on when the turn was written — an ISO date or
                datetime, or one of: today, yesterday, tomorrow, now,
                last|this week|month|quarter|year, or '<N> day(s)|week(s)|month(s)|year(s) ago'.
                Omit for no lower bound.
            until: Upper bound on the same grammar. A date means the whole day.
                Omit for no upper bound.
            limit: How many hits this page may hold.

        Returns:
            `entries` — `turn_id`, `created_at`, `user_message`,
            `assistant_message`, `token_count` and `similarity` per hit, best
            first — and `limit`.
        """
        agent, subagent = _caller(ctx)
        if not terms(query):
            raise ToolError(
                "turn_search needs something to look for — `turn_list` browses "
                "the history without a query."
            )
        store = await store_of(agent, subagent)
        ranked = await store.recall(
            query=query,
            embedder=await embedding.get(),
            since=since,
            until=until,
            limit=limit,
        )
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
        return {"entries": entries, "limit": page_limit(limit)}

    # --- the two decisions ---------------------------------------------------
    #
    #  Harness tools, unmarked, and the caller is the agent server.  They carry
    #  the client id as an ordinary argument rather than in `_meta`, and the
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

    def _in_hand(
        messages: Sequence[dict[str, Any]], turn_ids: Sequence[int]
    ) -> dict[str, Any]:
        """The answer when the turns in hand are what the turn runs on.

        Four of `rebuild`'s five exits are this one outcome — a discriminator
        that decided nothing, a decision that asks for nothing, a decision that
        equals what is held, and a selection that cannot be fetched — and they
        are one dict because they are one thing: nothing was fetched, nothing was
        written, and the caller's list stands.  Spelled out four times they were
        four chances to describe that differently, and four places to add a key
        to.

        It used to name a fifth exit that does not exist, "the switch is off":
        `ContextSettings` has no switch, and nothing here reads one, so a reader
        looking for the arm that honours it was looking for an arm nobody had
        written.  The discriminator that answers nothing is the real fourth, and
        `rebuild` logs it as its own reason (`no_decision`, not
        `context_sufficient`) because it is not a decision the model made.

        `kept` is what the caller in hand, `recalled` none of it — and the two
        together are what `rebuild` always reports, so a reader of the answer never
        has to know which of the five ways it was reached to read the counts.
        """
        return {
            "messages": list(messages),
            "turn_ids": list(turn_ids),
            "changed": False,
            "kept": len(turn_ids),
            "recalled": 0,
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
            boundaries are the record's and not the model's.  v1 answered both
            readers from one read for the same reason.
        """
        store = await store_of(agent, subagent)
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

        Zero when the config does not say, which is a real answer and not a
        missing one: the budget is a fraction of a window, and a window nobody
        declared is a budget nobody can compute.  The caller drops the token cap
        and keeps the count cap, rather than inventing a number — a guessed
        window is a context silently sized against a model that is not the one
        answering.

        **A model the config cannot resolve is not that answer.**  A broad
        `except` folded a `ConfigError` in with a declared-absent window, so a
        broken config silently dropped the token cap and let a recall spend by
        count — the overfill the cap exists to stop.  A window that cannot be
        *asked for* is a failure, not a zero (DESIGN.md §10.1: a weaker path is
        not a substitute for a missing capability).
        """
        return int(config.resolve(model)[2].context_window)

    async def _discriminate(
        agent: str,
        subagent: str,
        model: str,
        messages: list[dict[str, Any]],
        user_input: str,
    ) -> Decision | None:
        """One model call: what to keep, and what to recall.  `None` if unusable.

        **Never raises and never retries.**  A timeout, a provider failure and a
        reply that is not the requested object all return `None`, and the
        caller's fallback — keep the context — is perfectly good, so retrying
        would double the pre-turn latency of a call whose answer nobody needed.

        The instruction travels as a user message *appended to the conversation*,
        which is what lets a follow-up be recognised as one: the subject of "what
        about the other one" is in the list above it and nowhere else.
        """
        try:
            text = await asyncio.wait_for(
                ask_model(agent, subagent, model, messages, user_input),
                timeout=config.context.timeout,
            )
        except TimeoutError:
            logger.info("recall_discriminator_failed reason=timeout")
            return None
        except Exception as exc:  # noqa: BLE001 — a fallback, not a failure mode
            logger.info("recall_discriminator_failed reason=%s", type(exc).__name__)
            logger.debug("the discriminator call raised", exc_info=True)
            return None
        decision = decisions.parse(text)
        if decision is None:
            logger.info("recall_discriminator_unparsed")
        return decision

    #: The discriminator call: a test's, or one client per model server kept for
    #: the process.
    model_clients: dict[str, Client] = {}
    opening = asyncio.Lock()
    ask_model: Discriminator = injected or (
        lambda agent, subagent, model, messages, prompt: _ask_model(
            config, model_clients, opening, agent, subagent, model, messages, prompt
        )
    )

    @mcp.tool
    async def forget(agent: str, subagent: str = "") -> dict[str, Any]:
        """Clear a conversation's live context, and keep every turn of it.

        Called by the agent server's `reset`, and it is the second half of what
        that tool means: a conversation is forgotten by dropping the loop *and*
        the list, because a list left behind is a conversation that restores
        itself on the next prompt.

        **The turns are not deleted.**  They are the log, and the log is storage
        rather than state: `turn_list` goes on finding all of them, and what
        changed is only which of them this conversation is made of.  There is
        deliberately no tool here that deletes a turn — that is a request with a
        different blast radius and nobody has needed it.

        Args:
            agent: Whose conversation.
            subagent: Which of that agent's conversations; empty for its own.

        Returns:
            `turn_ids`: the list as it stands now, which is always `[]`.
        """
        store = await store_of(agent, subagent)
        written = await off_loop(store.set_context_turns, [])
        logger.info("forgot the context of %s", describe((agent, subagent)))
        return {"turn_ids": written}

    @mcp.tool
    async def rebuild(
        agent: str,
        messages: list[dict[str, Any]],
        turn_ids: list[int],
        prompt: str = "",
        subagent: str = "",
        model: str = "",
        carried: int = 0,
    ) -> dict[str, Any]:
        """Decide what the next turn runs on: keep ∪ recall.

        Called **once per turn, before the user's message is appended** — before,
        because this replaces the message list wholesale and anything appended
        first would be destroyed by it.

        One model call decides: which of the turns in hand to keep, and what to
        recall from the turn log.  The two are joined by id and never reconciled,
        so an empty recall is harmless and only an explicit `"clear"` empties the
        context (`slife2.libs.turn.context` is where that is argued).

        **A decision that asks for exactly what is in hand rebuilds nothing at
        all**, which is the common case and what keeps a call this expensive from
        being paid for nothing.

        Args:
            agent: Whose conversation.
            messages: The conversation as the caller holds it.  This is what the
                discriminator reads — not a re-render of the stored turns, which
                would be a second opinion about what is in hand.
            turn_ids: The turns those messages came from, in order.  The
                keep-list is read as an intersection with this, so an id that has
                left the context cannot be kept by naming it.
            prompt: What the user just said.  Passed **beside** the messages and
                not inside them, because this runs before the caller appends it
                — that order is the whole reason the argument exists, and reading
                it back out of the list would be reading the previous turn's.
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

        Returns:
            `messages` (what the turn should run on), `turn_ids` (what it was
            built from), `changed` (whether anything moved) and `recalled` (how
            many of the selected turns came from the turn log rather than from
            in hand).
        """
        store = await store_of(agent, subagent)
        decision = await _discriminate(agent, subagent, model, messages, prompt)
        # **Two ways to have no decision, and the log keeps them apart.**  Both
        # fall back to the turns in hand, and neither may be reported as the
        # other: this line is the only sight anybody gets of the discriminator
        # (`recall_discriminator_failed` and `_unparsed` are written a moment
        # above and say *why* there was none), and a single `context_sufficient`
        # for both made counting "the model decided the context was enough"
        # count every timeout and every unreadable reply as a decision the model
        # never made.  A decision that asks for nothing is the model's word; no
        # decision at all is the transport's silence.
        if decision is None:
            logger.info("recall_not_needed reason=no_decision")
            return _in_hand(messages, turn_ids)
        if decision.asks_for_nothing:
            # The ordinary case, and the one this fallback is for: the turns in
            # hand are the answer.  Nothing is fetched, nothing is rendered,
            # nothing is written — and the list already on disk still describes
            # what the caller is holding.
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
        if decision.recall is not None:
            recalled = await _recall_into(store, decision.recall, base, model)

        target = decisions.union(base, recalled)
        if target == sorted(in_hand):
            # The decision happens to equal what is in hand even though it was
            # asked for — same set, so the same context, and re-rendering it
            # would only cost a fetch and a write.
            logger.info("recall_not_needed reason=context_unchanged")
            return _in_hand(messages, turn_ids)

        rows = await off_loop(_rows, store, target)
        if len(rows) != len(target):
            # A named turn with no row: the list and the log disagree, which
            # means a file was rebuilt or pruned underneath.  Keeping the context
            # is the answer, because a context assembled from part of a selection
            # is neither what the model decided nor what it had.
            logger.warning("recall_abandoned reason=unfetchable")
            return _in_hand(messages, turn_ids)

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
        # wrong slice and told nothing.  `slife2.plugins.agent.server.rebuild_into`
        # is the one caller, and `Loop.covered` is where it keeps the run at the
        # end — a turn that follows unbacked messages does not join `turn_ids`,
        # which is what keeps the count honest.
        if carried > 0:
            rebuilt.extend(dict(message) for message in messages[-carried:])
        # Published before the caller is told, and a failure here **fails the
        # turn**.  The list is authoritative (DESIGN.md §5.1): the caller adopts
        # this answer as its context, so returning the in-memory rebuild while
        # the *persisted* list did not move leaves the two disagreeing, and the
        # next start replays the stale one — a conversation that comes back one
        # turn short, silently.  The guard that used to swallow this described a
        # tolerance the code did not have anyway (a busy or full disk raised out
        # of `rebuild` regardless); refusing it is the honest half, and the
        # caller that cannot have the write is told rather than handed a context
        # the log will not have on the next start.
        await off_loop(store.set_context_turns, target)
        logger.info(
            "recall_rebuilt turns=%d recalled=%d",
            len(target),
            len(set(recalled) - set(base)),
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
        }

    async def _recall_into(
        store: TurnStore, condition: dict[str, str | None], base: list[int], model: str
    ) -> list[int]:
        """Run one recall condition and fit its answer into the window.

        The three caps are the caller's and not the store's, which is v1's split
        and its reason: the store ranks, the policy caps, and the policy depends
        on a model's window and on what the decision kept — neither of which the
        store knows.

        **The budget is headroom below the *ceiling*, and the ceiling is the
        bound a kept context is measured against.**  Subtracting the *floor*
        instead would grant no headroom at all: the trim compacts down to the
        floor, so a live context sits at or above it for most of a session.
        """
        query = str(condition.get("query") or "")
        if query and not terms(query):
            # **A query with no term is not a query the store can run.**  The
            # keyword leg refuses such a thing — `"***"`, `"—"` — with
            # `EmptyQuery`, and the store's own contract is that an empty query
            # reaching the full-text index is a syntax error and embeds to noise.
            # Dropping the *query* and keeping the recall would run it as a
            # *period* recall instead — a different selection from the one the
            # model asked for — so the whole recall is dropped and the decision's
            # other half, the keep-list, runs on its own, which is §5.1's outcome
            # when a rebuild cannot do its job: keep the context it had.
            logger.info("recall_query_has_no_term")
            return []
        since = condition.get("since") or None
        until = condition.get("until") or None
        anchor = condition.get("anchor") or None
        if query and anchor:
            # Relevance decides which end a query is spent from, so an anchor
            # beside one is not consulted.  Logged rather than dropped silently,
            # because a model that wrote it meant something by it.
            logger.info("recall_anchor_ignored anchor=%s", anchor)
            anchor = None

        window = _window(model)
        # What the kept turns already cost, which is what the recall's budget is
        # the headroom *below*.  Summed here and not by the store: the store
        # ranks candidates, and how much room is left is a fact about a
        # selection the caller made.
        base_cost = sum((await off_loop(_cost_of, store, base)).values())
        if window:
            budget = min(
                int(window * config.context.floor),
                int(window * config.context.ceiling) - base_cost,
            )
        else:
            # No declared window: no token cap, and the count cap is what is
            # left.  Better a context bounded by count than one sized against a
            # window nobody stated.
            budget = 0
        if window and budget <= 0:
            logger.info("recall_no_headroom base=%d", base_cost)
            return []

        ranked = await store.recall(
            query=query,
            embedder=await embedding.get() if query else None,
            since=since,
            until=until,
            anchor=anchor,
            limit=config.context.recall_limit,
        )
        gated = decisions.gate(ranked, config.context.min_similarity)
        order = [turn_id for turn_id, _ in gated][: config.context.recall_limit]
        if not order:
            return []
        costs = await off_loop(_cost_of, store, order)
        if not window:
            return sorted(order)
        fit = decisions.fit_budget if query else decisions.fit_window
        return fit(order, costs, budget)

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
        build=build_server,
        logger=logger,
        note=lambda _config: f"turns in {agents_dir()}",
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
