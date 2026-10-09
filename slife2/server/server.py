"""slife2-agent — the agent loop, as an MCP server that owns its conversations.

The middle of the chain.  It is a server to the TUI and a client to an LLM
server, and it holds nothing else: no provider SDK is importable here, no API key
is readable here, and the only thing it knows about the model behind it is a URL
and a model name from the config.

**This server keeps the conversations**, and it addresses each one by
`(agent, subagent)` — the same pair every other server in this system keys its
own state by.  `subagent=""` is the agent's own conversation; anything else is a
worker it is running.  A caller submits a user message under that key and gets
that turn's answer back; the surface is three tools — `send_message`, `reset`,
and `transcript`, which is what a window that has just opened asks for.

**A key is created when it is first used, and it never expires.**  That is the
difference from the handle this server used to mint: an id could go stale — a
daemon restart, an idle window — and every caller then had to carry a "your loop
is gone" path.  A name cannot go stale.  The idle sweep still runs, but it only
reclaims memory; nothing a caller can observe depends on it.

**The key travels with the call.**  Everything this server asks of a peer is
asked on behalf of the same `(agent, subagent)` — the write to the db and the
model call both carry it — so a hop is never anonymous.  What the model servers do
*not* do with it is keep the conversation: see DESIGN.md §3 for why the history
has to live here, on the side of the hop that does not speak a wire protocol.

**The tools come from the toolhub**, and are asked for again before every model
call — the list goes out with each request, so it is read with each request.
That is why this server holds no tool registry of its own and why `loop.py`
still knows nothing about MCP: it is handed a coroutine, and where the registry
came from is nobody else's business.  The builtins included: one owner for the
model's whole tool list is worth one hop.  See DESIGN.md §8.

Two consequences of owning the history are paid for here rather than avoided,
because they are the price of the state:

* **A per-key lock**, so two turns cannot race over one list.  `send_message`
  takes it; a second caller waits, and that wait *is* the inbox.  Distinct keys
  hold distinct locks, so two agents' conversations run at the same time.
* **Repair on cancellation.**  A cancelled turn can leave the list holding an
  assistant message whose tool calls are only partly answered, which is a 400
  from every provider.  `run_turn_into` truncates back to the user's own message,
  inside the lock.

What statelessness bought and is now given up deliberately: a conversation store
that can grow without bound.  See DESIGN.md §9 — trimming is the next thing, not
a thing this cut pretends to have solved.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

from slife2.clock import now
from slife2.config import (
    API_SERVER_NAMES,
    CONTEXT_SERVER_NAME,
    TOOLHUB_SERVER_NAME,
    Config,
    find_config_path,
    load,
)
from slife2.context import turn_note, with_note
from slife2.events import ContextChosen, TurnEvent, TurnObserver, encode
from slife2.llm.base import LLMBackend
from slife2.llm.client import MCPBackend, open_backend
from slife2.loop import AgentLoop, TurnResult
from slife2.mcp_server import (
    ClientId,
    close_server,
    configure_logging,
    describe,
    house_server,
    open_server,
    parse_serve_args,
    serve,
    tool_payload,
)
from slife2.messages import Message, ToolCall
from slife2.prompt import render as render_system_prompt
from slife2.toolclient import FUNC_TOOL_UNLOAD, remote_tools, unload_tools
from slife2.tools import Tool, ToolRegistry

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-agent"

#: How long to wait on the context plugin.  Short for the `remember` it was
#: written for — that write happens once the answer already exists, and a slow
#: store is not worth holding the answer for — and long enough for the two calls
#: that are not writes at all: `restore` replays a whole context, and `rebuild`
#: makes a model call the plugin has already bounded by `context.timeout` plus a
#: recall that may embed a query.  A peer that is *gone* is a different matter
#: again — that fails the turn outright; see `slife2.mcp_server.open_server`.
CONTEXT_TIMEOUT_SECONDS = 120.0

#: How long to wait on the toolhub.  Longer than the db server's, because
#: this call is not a write after the fact: `list_tools` is what the turn's tool
#: list is built from, and it may briefly wait for tool servers that are still
#: connecting (`slife2.toolhub.LIST_SETTLE_SECONDS`).  A timeout shorter than
#: that would turn "waiting for a server that is starting" into a failed turn.
TOOLHUB_TIMEOUT_SECONDS = 30.0

#: How long a conversation survives with nothing asked of it.  This is
#: housekeeping, not a lifetime a caller can observe: the sweep only reclaims
#: memory, and the next message under the same key starts the state again.
LOOP_IDLE_SECONDS = 30 * 60.0

#: How many messages one loop may have waiting.  The client's per-call timeout
#: starts when the call is made, not when its turn starts, so an unbounded queue
#: is an unbounded wait — and a wait longer than the timeout closes the stream
#: and cancels the turn, which is the very way a message gets lost.  A bound is
#: what keeps "queued" from meaning "eventually dropped".
MAX_QUEUED = 32

INSTRUCTIONS = (
    "A conversational agent. Call `send_message` with an agent name and what was "
    "said; assistant output arrives as progress notifications on that call, and "
    "the result carries the final answer. The conversation is kept here and is "
    "addressed by `(agent, subagent)` — send only what is new, and use `reset` to "
    "start one over."
)


class ProgressObserver:
    """Reports turn events as MCP progress notifications.

    `progress` is a monotonic counter and `total` stays None: how many deltas a
    turn will produce is genuinely unknown, and inventing a total would make
    clients render a percentage that means nothing.

    This class is also the seam for coalescing.  A fast local model can produce
    hundreds of notifications a second, and if that ever matters the fix is a
    small buffer here — one place, not threaded through the loop.
    """

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._count = 0

    async def on_event(self, event: TurnEvent) -> None:
        self._count += 1
        # A no-op when the caller sent no progress token.  That is intended and
        # it is silent: `report_progress` checks for the token itself, so a
        # client that does not want a stream simply does not get one, and still
        # gets a correct result at the end.
        await self._ctx.report_progress(self._count, None, encode(event))


@dataclass(eq=False)
class Pending:
    """A message submitted to a loop whose turn has not started yet.

    It is held here, and *not* appended to the loop's messages, until its turn
    begins.  `AgentLoop.run_turn` re-reads the message list at every step, so a
    message appended early would be seen by the model mid-turn — steering that
    nobody asked for, arriving silently.

    `eq=False` because the inbox is a queue of *submissions* and not of values.
    `deque.remove` compares by equality, so the generated `__eq__` — which
    compares `(prompt, images, channel)` — would make two callers who sent the
    same text one entry: the first turn takes its own message out of the queue,
    and its closing `remove` then takes the *other* caller's place in it, since
    that entry is equal.  The count `MAX_QUEUED` guards would then fall below
    the number of callers actually waiting, which is the bound's whole job.
    """

    prompt: str
    images: list[str]
    channel: str


@dataclass
class Outcome:
    """What one turn did, filled in even when the turn was cancelled.

    `messages` is a copy taken after any repair, so it is the slice this turn is
    answerable for: the whole exchange, or the user's message alone when the
    turn was cut off.
    """

    messages: list[Message] = field(default_factory=list)
    result: TurnResult | None = None
    started_at: str = ""
    completed_at: str = ""


@dataclass
class Loop:
    """One conversation, and everything that makes it a thing.

    `subagent` is part of the identity rather than a label on it: two keys differ
    exactly when their pairs differ, and the pair is what the lock, the inbox and
    the write to the db are each scoped to.
    """

    agent: str
    subagent: str
    model: str
    messages: list[Message]
    lock: asyncio.Lock
    last_used: float
    inbox: deque[Pending] = field(default_factory=deque)
    #: Which turns of the log `messages` is made of, in order — the agent's copy
    #: of the live-context list the store persists.  Held here rather than read
    #: per turn because it *is* the in-memory state: `rebuild` is handed it and
    #: answers with its replacement, and the two must be the same list or a
    #: keep-list would name turns the messages do not have.
    turn_ids: list[int] = field(default_factory=list)
    #: How many of `messages` those ids account for — the system prompt, then
    #: each turn in the list, in order.  What follows is the **carried tail**:
    #: messages this conversation holds that no turn in the list covers, which is
    #: a cancelled turn's repair and nothing else.  v1 re-derives the same run by
    #: grouping messages into turns; a count is enough here because nothing else
    #: can leave a message unbacked, and it is what lets a rebuild replace the
    #: turns without destroying a message that has no turn to be replaced by.
    covered: int = 0

    @property
    def records(self) -> bool:
        """Whether this conversation's turns are written to the db.

        A worker's are not.  A subagent is a means to an end inside one turn of
        its parent's conversation, and recording its round trips would file them
        under a conversation they were never part of — which is the one thing the
        record must not do, since the whole point of it is to be readable as a
        conversation.
        """
        return not self.subagent


def build_server(
    config: Config,
    *,
    backend: LLMBackend | None = None,
    context_client: Client | None = None,
    hub_client: Client | None = None,
) -> FastMCP:
    """Build the agent MCP server.

    `backend`, `context_client` and `hub_client` are injectable so the whole
    server — model call, tools, the restore, the rebuild, the write — can be
    exercised over the in-memory transport with no network at all.

    Otherwise a connection is opened **per model server, on first use, and kept
    for the process**.  Not per turn — that would pay a handshake for every step
    of every turn — and not once at startup either, because which model is
    wanted is a property of the *loop*: a caller names one when it opens one, and
    this server serves every caller.  A model nobody asks for is a connection
    nobody opens.

    The toolhub is the same arrangement with a different lifetime question
    answered: its *connection* is kept for the process, and its *tool list* is
    asked for on every turn.  See `turn_tools` for why those are not the same
    choice.
    """
    #: Cached per model *name*, because the connection behind one is expensive
    #: and a conversation's model never changes.  `MCPBackend` rather than the
    #: protocol, because only this one can carry a key — an injected backend is
    #: a test's, and has no model server to name a conversation to.
    model_backends: dict[str, MCPBackend] = {}
    clients: dict[str, Client] = {}
    #: The context client once we have one — injected, or opened on first use.
    context_conn: Client | None = context_client
    #: Whether *we* opened it, and so whether we should close it.  An injected
    #: client belongs to whoever made it.
    context_owned = context_client is None
    #: The toolhub client, on the same terms.
    hub_conn: Client | None = hub_client
    hub_owned = hub_client is None

    #: Every conversation, by key.  One entry per `(agent, subagent)` that has
    #: been used, which is what makes isolation a property of the key rather than
    #: a rule somebody has to remember to apply.
    loops: dict[ClientId, Loop] = {}

    #: Serialises the two lazy caches below.  Without it, two turns that start
    #: together both miss and both open a connection, and the loser is
    #: overwritten in `clients` — which makes it unreachable to the lifespan's
    #: cleanup and leaks it along with its task group.  Two loops on one default
    #: model is the ordinary case now, so this is not a hypothetical race.
    opening = asyncio.Lock()

    #: Writes detached from a cancelled turn.  Kept so they are not collected
    #: mid-flight, and drained on the way out.
    background: set[asyncio.Task[None]] = set()

    async def memory() -> Client:
        """The client for the context plugin, opened on first use.

        Opened once and kept for the process, the same arrangement as the model
        backends and for the same reason: a handshake per turn is a handshake
        per turn.

        A context plugin that is not there **raises**, like every other peer in
        this system.  `slife2.mcp_server.open_server` is where that rule lives
        and why; what matters here is that this plugin is not the exception
        to it — and it is the peer whose absence is felt soonest, since a turn
        whose context cannot be restored is a turn that should not be run.
        """
        nonlocal context_conn
        # Fast path outside the lock: once a loop has been opened there is no
        # await between the check and the use, and the event loop is
        # single-threaded, so reading it unlocked is sound.
        if context_conn is not None:
            return context_conn
        async with opening:
            if context_conn is None:
                context_conn = await open_server(
                    config.server("context").url,
                    name=CONTEXT_SERVER_NAME,
                    fallback_tool="remember",
                    timeout=CONTEXT_TIMEOUT_SECONDS,
                )
            assert context_conn is not None  # open_server returns one or raises
            return context_conn

    async def hub() -> Client:
        """The client for the toolhub, opened on first use.

        **Never optional.**  There used to be a branch here for a config with no
        tools at all, and it was wrong for the reason the builtins are in the hub
        rather than here: the model's tool list has one owner, so a system whose
        toolhub is missing is not a system with no tools — it is a system that
        has come apart, and it fails the turn like any other missing peer.

        A toolhub that is there and whose *upstreams* are not is an entirely
        different thing, and is not an error at all: that is the operator's
        configuration and somebody else's process (`slife2.toolhub`).
        """
        nonlocal hub_conn
        if hub_conn is not None:
            return hub_conn
        async with opening:
            if hub_conn is None:
                hub_conn = await open_server(
                    config.server("toolhub").url,
                    name=TOOLHUB_SERVER_NAME,
                    fallback_tool="list_tools",
                    timeout=TOOLHUB_TIMEOUT_SECONDS,
                )
            assert hub_conn is not None
            return hub_conn

    async def turn_tools(client_id: ClientId) -> list[Tool]:
        """What the model may call, asked of the hub.

        Asked **before every model call**, not once per turn: the turn's tool
        list goes out with each request to the model, so each request asks
        afresh.  One loopback round trip on a path that already exists, and what
        it buys is a list that is live — a tool server that finished starting,
        or grew a tool, is in the next *call*'s list, and nothing here had to
        notice that it happened.

        v1 needed a `tools/list_changed` subscription, a shared catalog and a
        reconcile pass to arrive at the same place.  Here the answer can be at
        most one call old, and nothing has to be kept in step to make that true.

        It is also asked once when the loop is built, before the user's message
        is appended: a hub that is not there is a system that has come apart, and
        the moment to find that out is before the conversation has been touched.

        **The tools come back bound to `client_id`**, so a tool that reads the db
        reads *this* conversation's.  That is the one thing this process knows
        and the model does not, and it is why the binding happens here rather
        than in an argument — see `slife2.toolclient`.
        """
        return await remote_tools(await hub(), client_id)

    async def registry(client_id: ClientId) -> ToolRegistry:
        return ToolRegistry(await turn_tools(client_id))

    async def remember_turn(
        agent: str,
        subagent: str,
        messages: list[dict],
        result: TurnResult | None,
        *,
        model: str,
        channel: str,
        created_at: str,
        completed_at: str,
    ) -> int | None:
        """Persist a turn, and answer with the id it was given.

        **This is called for every turn of a loop that records**, cancelled
        ones included, and `result` is None exactly when the turn did not
        finish.  The rule is deliberately not "remember to record the cancel
        path": a write that is conditional on how a turn ended is a write
        somebody can forget to make, and the failure it produces is a
        conversation in the transcript that the database has never heard of.

        A store that is *gone* fails the turn, and that is the system's rule
        rather than this function's — `slife2.mcp_server.open_server` is where a
        missing peer is decided to be a broken system rather than a degraded
        one.  A `ToolError` is left alone, and it is a different thing: it means
        the db server answered and refused *this* request — an agent name
        that cannot be a filename, say — which is one caller's problem rather
        than a sign that anything is down.

        Two token counts go over, and they are not interchangeable.  `usage` is
        the turn's total across however many model calls it took — the bill.
        `last_usage` is the final call's own, which is how much conversation
        existed when the turn ended: the number the *next* request would resend.
        A cancelled turn has neither, and reports zero rather than a guess.
        """
        client = await memory()

        try:
            payload = tool_payload(
                await client.call_tool(
                    "remember",
                    {
                        "agent": agent,
                        "subagent": subagent,
                        "messages": messages,
                        "token_count": result.usage.total_tokens if result else 0,
                        "context_tokens": result.last_usage.total_tokens
                        if result
                        else 0,
                        "who_helped": agent,
                        # What the loop was opened on, not the reference as typed:
                        # a caller may name a bare provider or nothing at all, and
                        # neither says which model wrote the answer.
                        "what_model": model,
                        "channel": channel,
                        "created_at": created_at,
                        "completed_at": completed_at,
                    },
                )
            )
        except ToolError as exc:
            # Caught, and only this. The server answered and refused one
            # request; everything else — the transport gone, a store that
            # stopped answering — is a system that has come apart and is meant
            # to fail here rather than be logged and stepped over.
            logger.warning("the store refused the turn for %s: %s", agent, exc)
            return None
        turn_id = payload.get("turn_id")
        return int(turn_id) if isinstance(turn_id, int) else None

    async def make_loop(active: LLMBackend, client_id: ClientId) -> AgentLoop:
        return AgentLoop(
            active,
            await registry(client_id),
            max_steps=config.agent.max_steps,
            # Handed to the loop so the list is re-read before each model call;
            # see `turn_tools`.  Bound to the key for the same reason the first
            # call is: every refresh has to hand back tools for *this*
            # conversation, and a bare `registry` would forget whose it was.
            refresh=partial(registry, client_id),
        )

    async def loop_for(reference: str, client_id: ClientId) -> AgentLoop:
        """The agent loop for a conversation's model, bound to that conversation.

        A conversation's model is fixed when it starts, so this is asked the same
        question every turn — which is why the connection is cached per model
        server, and why the cache is taken under a lock.

        What is *not* cached is the key.  `with_key` hands back a view of the
        shared backend carrying this conversation's `(agent, subagent)`, so the
        key reaches the model server on every call without splitting the cache
        per conversation and without `loop.py` ever learning that keys exist.
        """
        if backend is not None:
            return await make_loop(backend, client_id)

        name, provider, model = config.resolve(reference)
        async with opening:
            if name not in model_backends:
                url = config.server(provider.api).url
                if url not in clients:
                    # One client per *server*, not per provider: a server speaks
                    # one wire format for every provider that uses it.
                    client, _ = await open_backend(
                        url,
                        model.model,
                        provider=name,
                        name=f"{name}/{model.model}",
                        # Checked against the name the protocol's server
                        # advertises, so a URL pointed at the wrong backend is
                        # caught here rather than at the first turn.
                        server_name=API_SERVER_NAMES[provider.api],
                    )
                    clients[url] = client
                model_backends[name] = MCPBackend(
                    clients[url],
                    model.model,
                    provider=name,
                    name=f"{name}/{model.model}",
                )
                logger.info(
                    "model %s via %s", reference, config.server(provider.api).url
                )
        return await make_loop(model_backends[name].with_key(*client_id), client_id)

    # --- the registry --------------------------------------------------------

    def resolved_model(reference: str) -> str:
        """The model a conversation started on, as `provider/model`.

        Read once, when the conversation starts, and kept: a conversation's model
        is a property of the conversation, so a later message naming a different
        one is not obeyed — it is what `reset` is for.
        """
        name, _, settings = config.resolve(reference)
        return f"{name}/{settings.model}"

    def opening_messages(agent: str) -> list[Message]:
        """What a conversation starts with: its system prompt, and nothing else.

        Rendered when the conversation starts rather than once at startup,
        because the agent name is a property of the *key* — the server is shared,
        so two agents are two names asking one process.
        """
        system = render_system_prompt(config.agent.system_prompt, agent_name=agent)
        return [Message(role="system", content=system)] if system else []

    def reap() -> None:
        """Drop conversations nothing has asked anything of for a while.

        In-process state only.  Nothing a caller can observe depends on this — a key is
        recreated on the next message that uses it — which is the whole reason
        the sweep is allowed to be this casual.

        A conversation with a turn in flight is never idle, whatever its
        timestamp says: its own turn refreshes `last_used` when it finishes.
        """
        cutoff = time.monotonic() - LOOP_IDLE_SECONDS
        for client, loop in list(loops.items()):
            if loop.lock.locked():
                continue
            if loop.last_used < cutoff:
                logger.info("reaping idle conversation %s", describe(client))
                loops.pop(client, None)

    def conversation(agent: str, subagent: str, model: str) -> tuple[Loop, bool]:
        """The conversation for this key, and whether this call started it.

        Created on demand rather than opened by a separate call, because a key
        that cannot go stale is worth more than the round trip it costs: there is
        no handle to carry, no lifetime to observe, and no "not found" for a
        caller to handle.  The idle sweep may have dropped the state; the caller
        cannot tell, and does not have to.

        **`started` is the restore's trigger, and it is why this answers a pair.**
        A loop that already exists is one whose context is in hand; a loop that
        has just been built holds the system prompt and nothing else, and that is
        exactly the moment to ask the store what this conversation was made of —
        which is the same event as "a process restarted" and "a reaped
        conversation came back", at the only level this server can see.
        """
        client = (agent, subagent)
        loop = loops.get(client)
        started = loop is None
        if loop is None:
            messages = opening_messages(agent)
            loop = Loop(
                agent=agent,
                subagent=subagent,
                model=resolved_model(model),
                messages=messages,
                lock=asyncio.Lock(),
                last_used=time.monotonic(),
                # The head is what an empty list covers, so a conversation that
                # restores nothing carries nothing either.
                covered=len(messages),
            )
            loops[client] = loop
            logger.debug(
                "started %s on %s", describe(client), model or "the default model"
            )
        loop.last_used = time.monotonic()
        return loop, started

    def detach(coro: Coroutine[Any, Any, Any]) -> None:
        """Run a write that must outlive the cancellation that prompted it.

        Measured against the real Streamable HTTP transport: from a cancelled
        handler a plain `await` is cancelled again at its next checkpoint, so a
        record written that way is simply lost.  A task created here is outside
        the cancelled scope and does land.  We cannot wait for it, so its
        failures are logged rather than dropped on the floor.
        """
        task = asyncio.ensure_future(coro)
        background.add(task)

        def done(finished: asyncio.Task[None]) -> None:
            background.discard(finished)
            if not finished.cancelled() and (exc := finished.exception()):
                logger.warning("recording a cancelled turn failed: %s", exc)

        task.add_done_callback(done)

    def recorded_model(loop: Loop) -> str:
        """What to write in the `what_model` column.

        The loop's own model, except when a backend was injected — in which case
        the thing that ran genuinely is not anything the config names, and
        writing the config's model would be recording a model that never
        answered.  This is the one place that distinction has to be made, so it
        is one function rather than a condition repeated at each call site.
        """
        return backend.name if backend is not None else loop.model

    async def record(loop: Loop, item: Pending, outcome: Outcome) -> int | None:
        """Write this turn to the log if this loop has any, and answer with its id.

        **Called inside the lock, which reverses an earlier decision.**  It used
        to run after the lock was released, on the argument that a queued turn
        waiting on a network call is a wait with no reason behind it — and that
        argument held while the write was a store's alone.  It does not hold now
        that the *live-context list* is what the write maintains: a rebuild reads
        that list, so a queued turn that started before the write landed would be
        handed a context missing the turn it is following up on, and would drop
        it.  The price is one loopback before a queued turn starts; the thing it
        buys is that `turn_ids` and `messages` are never out of step.
        """
        if not loop.records or not outcome.messages:
            # Nothing to record, or a caller cancelled while queued — in which
            # case its turn never started and there is nothing to record.
            return None
        return await remember_turn(
            loop.agent,
            loop.subagent,
            [message.to_wire() for message in outcome.messages],
            outcome.result,
            model=recorded_model(loop),
            channel=item.channel,
            created_at=outcome.started_at or outcome.completed_at,
            completed_at=outcome.completed_at,
        )

    # --- the two decisions, and the one place they are asked for -------------
    #
    #  Both are the context plugin's, and this server is the caller — it is the
    #  party that owns the conversation, so it is the party that knows when one
    #  has started and when a turn is about to run.  What it does with each
    #  answer is the same: adopt it wholesale, because a context that is half
    #  this server's and half the store's is not a context either of them knows.

    def _adopt(loop: Loop, payload: dict[str, Any], *, carried: int) -> None:
        """Take a rebuilt context as this conversation's, or refuse the answer.

        Refused rather than ignored, and that is the load-bearing half: a peer
        that answers with a shape this build cannot read is a system that has
        come apart — the same rule `slife2.toolclient.remote_tools` applies to a
        tool list — and quietly keeping the context we had would make an
        unreadable answer indistinguishable from a decision to keep it.
        """
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise ConnectionError(
                f"the context store did not return a message list (it said "
                f"{payload!r}); a daemon from another build does this — try "
                f"`slife2 down`"
            )
        loop.messages = [Message.from_wire(raw) for raw in messages]
        loop.turn_ids = [int(turn_id) for turn_id in payload.get("turn_ids") or []]
        loop.covered = len(loop.messages) - carried

    async def restore_into(loop: Loop) -> None:
        """Put a conversation back on the context it had when it last stopped.

        Called when a loop is *built* — a process that restarted, a conversation
        the idle sweep let go — which is why `reset` clears the stored list
        rather than only dropping the loop: forgetting a conversation and then
        having it restore itself is not forgetting it.
        """
        payload = tool_payload(
            await (await memory()).call_tool(
                "restore",
                {
                    "agent": loop.agent,
                    "subagent": loop.subagent,
                    "messages": [message.to_wire() for message in loop.messages],
                },
            )
        )
        _adopt(loop, payload, carried=0)
        if loop.turn_ids:
            logger.info(
                "restored %d turn(s) for %s",
                len(loop.turn_ids),
                describe((loop.agent, loop.subagent)),
            )

    async def rebuild_into(loop: Loop, prompt: str) -> ContextChosen:
        """Ask what this turn runs on, take the answer, and report the counts.

        **Before the user's message is appended**, because the answer replaces
        the list and an appended message would be destroyed by it — and the
        message therefore travels as `prompt` rather than being read back out of
        the list, where it is not yet.

        `carried` is what the message list holds beyond the turns in `turn_ids`:
        a cancelled turn's repair, and nothing else.  The store puts those
        messages back verbatim, so a rebuild replaces the *turns* without
        destroying something that has no turn to be replaced by.

        Returns the counts as the event that reports them, because the
        discriminator is the most expensive thing a turn does and the two
        numbers are the only sight of it anybody gets — how much of what was in
        hand it kept, and how much it went back to the log for.  Handing back the
        event rather than the counts keeps the vocabulary in one module: this
        knows *what happened*, `slife2.events` knows how it is said.
"""
        carried = max(len(loop.messages) - loop.covered, 0)
        payload = tool_payload(
            await (await memory()).call_tool(
                "rebuild",
                {
                    "agent": loop.agent,
                    "subagent": loop.subagent,
                    "messages": [message.to_wire() for message in loop.messages],
                    "turn_ids": list(loop.turn_ids),
                    "prompt": prompt,
                    "model": loop.model,
                    "carried": carried,
                },
            )
        )
        _adopt(loop, payload, carried=carried)
        return ContextChosen(
            kept=int(payload.get("kept") or 0),
            recalled=int(payload.get("recalled") or 0),
        )

    async def forget_context(agent: str, subagent: str) -> None:
        """Clear a conversation's stored live context.

        A store that is gone **fails the reset**, which is this system's rule and
        not an exception made for tidiness: a reset that quietly did not happen
        is a conversation that comes back on the next message, which is the one
        outcome the caller was trying to prevent.
        """
        await (await memory()).call_tool(
            "forget", {"agent": agent, "subagent": subagent}
        )

    async def trim_tools(loop: Loop) -> None:
        """Bring the model's tool list back within its budget — and say so in the
        turn, rather than doing it behind the model's back.

        **A turn boundary, and the harness's own call.**  The tools a model has
        loaded go out with every request, so a turn that loaded several has left
        the list longer than the config allows — and it is trimmed here rather
        than by the gate because this is the moment nothing is in flight: the
        list is rebuilt before every *model call*, so dropping the excess
        mid-turn would take away a tool the model had just loaded and was about
        to use.  The count and the threshold are the catalogue's (`evict`); what
        this side decides is *when* to ask, which is the half a provider cannot
        do for us.

        **What went is written into the conversation.**  The trim is recorded as
        a tool pair — `_func_tool_unload` called with no names — so the model
        reads that its list shrank and which tools left, instead of reaching for
        a tool it still believes it has.  That is v1's *harness tool-pair*
        (DESIGN.md §2.5 there), and it is why the tool is in the model's list at
        all: v1's rule is that such a pair has to name a **declared** tool,
        because the Responses and Messages backends reject a tool call in
        history that is not in the request's tool list.  A pair invented in the
        history layer would be exactly that.

        Nothing is written when nothing moved: no tools unloaded, no pair.  An
        empty pair every turn would be a record of something that did not
        happen, and a model reading it would learn to distrust the mechanism.

        Best-effort by construction: the turn is over and has been answered, and
        bookkeeping that runs after it must not turn a good turn into a failed
        one.  A hub that is really gone makes itself heard on the next turn's
        tool list, which is asked for before the conversation is touched.
        """
        try:
            found = await unload_tools(await hub())
        except Exception as exc:  # noqa: BLE001 — bookkeeping does not fail a turn
            logger.warning(
                "%s: the tool list was not trimmed: %s",
                describe((loop.agent, loop.subagent)),
                exc,
            )
            return
        unloaded = [str(name) for name in found.get("unloaded") or []]
        if not unloaded:
            return
        logger.info(
            "%s: %d tool(s) unloaded to stay within the budget: %s",
            describe((loop.agent, loop.subagent)),
            len(unloaded),
            ", ".join(unloaded),
        )
        call_id = f"_harness_func_tool_unload_{time.time_ns():x}"
        # **Both halves with nothing awaited between them.**  An interruption in
        # that gap leaves an assistant message whose call is never answered, and
        # that is the one history state every provider rejects — the same reason
        # the caller refuses to start a trim on a cancelled turn.  Two appends
        # with no suspension point between them cannot be split.
        loop.messages.append(
            Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id=call_id, name=FUNC_TOOL_UNLOAD, arguments={})],
            )
        )
        loop.messages.append(
            Message(
                role="tool",
                content=str(found.get("text") or ""),
                tool_call_id=call_id,
            )
        )

    def annotate_turn(outcome: Outcome, item: Pending, turn_id: int) -> None:
        """Write a turn's id and span onto the message that opened it.

        **The footnote is how a turn id reaches the model at all**, and
        therefore how a keep-list is expressible: the ids a model writes back
        are the ids it read.  `messages_from_turns` puts one on every turn a
        *rebuild* builds, which covers everything that came out of the store —
        and this is the other half, and v1's: the turn that has just run is in
        memory and in no rebuilt list yet, so without this the newest turns are
        the only ones the model cannot name, and a keep-list silently drops
        exactly those.

        Called **after** the save, and never written into it.  The row holds the
        user's own words and the footnote is derived from the row when a list is
        built; storing it as well would put two of them in one message on the
        next rebuild.  That is also why the timestamps are the ones the row was
        written with — the two spellings have to agree to the character.

        The message is `outcome.messages[0]`, which is the *same object* the
        loop is holding: the snapshot is a shallow copy, so there is no index to
        carry and the live list is annotated by annotating this.  A turn with no
        id — a subagent's, which is not written to the log — is left alone
        rather than given a number nothing can be looked up by.
        """
        opening = outcome.messages[0] if outcome.messages else None
        if opening is None or opening.role != "user":
            return
        opening.content = with_note(
            opening.content,
            turn_note(
                turn_id,
                outcome.started_at,
                outcome.completed_at,
                # The channel the row is written with, from the same field — a
                # footnote that disagreed with the record would be the one thing
                # the model has no way to check.
                item.channel,
            ),
        )

    async def run_turn_into(
        loop: Loop, item: Pending, observer: TurnObserver, outcome: Outcome
    ) -> None:
        """One turn, and the repair a cancellation needs.

        `outcome` is filled in a `finally`, *after* the repair, so it always
        holds what this turn is answerable for: the whole exchange, or the
        user's message alone when the turn was cut off.

        **The trim happens here, inside the lock, and no longer after it.**  The
        pair it records has to be part of *this* turn — the saved record and the
        live history are one list until the snapshot is taken — and it has to be
        written before the next queued turn can append anything after it.  The
        price is that a queued turn waits for one loopback to the hub; the thing
        it buys is a transcript in which a tool the model lost is something it
        read in the turn that took it away, rather than a gap somebody has to
        explain.
        """
        snapshot = len(loop.messages)
        outcome.started_at = now()
        user = _with_images(item.prompt, item.images, config, loop.model)
        agent_loop = await loop_for(loop.model, (loop.agent, loop.subagent))
        cancelled = False
        try:
            outcome.result = await agent_loop.run_turn(loop.messages, user, observer)
        except asyncio.CancelledError:
            # The repair, and it has to happen *here* — inside the lock, before
            # it is released.  Releasing first and truncating after would let
            # the next queued turn start on a list that is not yet well formed,
            # and one of the states a cancellation can land in is invalid on the
            # wire rather than merely untidy: an assistant message whose tool
            # calls are only partly answered is a 400 from every provider.
            #
            # `+ 1` is load-bearing.  The loop appends the user's message
            # itself, so truncating to the snapshot would delete the message
            # this whole arrangement exists to not lose.
            del loop.messages[snapshot + 1 :]
            cancelled = True
            raise
        finally:
            # Not on the cancelled path, and not merely to save a call: this is
            # a `finally` running *during* a cancellation, where the next await
            # is cancelled at its first checkpoint — so a pair could be started
            # and never finished, which is the one history state that is a 400
            # from every provider.  v1's `_auto_invoke` refuses to start for the
            # same reason.  The turn is over; the budget can wait a turn.
            if not cancelled:
                await trim_tools(loop)
            outcome.messages = list(loop.messages[snapshot:])
            outcome.completed_at = now()

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, object]]:
        nonlocal context_conn, hub_conn
        try:
            yield {}
        finally:
            # A detached write exists because a turn was cancelled; letting the
            # process exit on top of it would lose the record it was written
            # for.  Bounded, because a store that has stopped answering must not
            # hold up the shutdown.
            if background:
                await asyncio.wait(list(background), timeout=5.0)
            for client in clients.values():
                await close_server(client)
            clients.clear()
            model_backends.clear()
            # Closed *and* forgotten, which is what `clients.clear()` above is
            # for as well: this hook is not the process's lifetime over the
            # in-memory transport, so a client left in place here is one the
            # next session is handed already shut — every turn of it failing at
            # its first hop.  Clearing is what lets `memory()` and `hub()` open
            # again.
            if context_owned and context_conn is not None:
                await close_server(context_conn)
                context_conn = None
            if hub_owned and hub_conn is not None:
                await close_server(hub_conn)
                hub_conn = None

            # `loops` is deliberately **not** cleared here.  It is state the
            # process owns, and this hook is not the process's lifetime: over
            # the in-memory transport FastMCP runs the lifespan once per client
            # session, so clearing here would empty the registry every time a
            # caller disconnected — which is precisely what a loop is supposed
            # to survive.  The dictionary goes away with the process, which is
            # the only lifetime it has.

    mcp: FastMCP = house_server(
        SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan
    )

    @mcp.tool
    async def send_message(
        agent: str,
        prompt: str,
        ctx: Context,
        subagent: str = "",
        model: str = "",
        images: list[str] | None = None,
        channel: str = "",
    ) -> dict[str, Any]:
        """Say something to a conversation and get that turn's answer.

        The conversation is addressed by `(agent, subagent)` and **starts on the
        first message sent to a key** — there is nothing to open and no id to
        carry.  Sending to a key that has been idle for a long time simply starts
        it again; that is not an error and a caller cannot tell it happened.

        Assistant output streams back as `notifications/progress` on this
        request, one notification per chunk.  The returned `text` is the final
        answer and is authoritative — a client that ignores the progress stream
        still gets the whole thing.

        **A message sent to a busy conversation waits rather than displacing
        anything.**  Turns on one key run one at a time, in the order they were
        submitted, and a queued message becomes a turn of its own when its place
        comes up.  Nothing is dropped and nothing is cancelled — up to a bound,
        because a client's call timeout does not know it is waiting.  Different
        keys do not wait for each other at all.

        Args:
            agent: Whose conversation.  It renders the system prompt and is the
                name its turns are recorded under.
            prompt: What the user just said.
            subagent: Which of that agent's conversations.  Empty — the default —
                is the agent's own, the one a person is watching.  Anything else
                is a worker the agent is running: a separate conversation, with
                its own history and its own inbox, whose turns are **not** written
                to the db.  A subagent is a means to an end inside one turn of its
                parent's conversation, and filing its round trips in the record
                would file them under a conversation they were never part of.
            model: Which model this conversation runs on, as `provider/model`.
                Left out, the config's `default` is used.  Read when the
                conversation starts and kept — a later message naming a different
                model is ignored, because the model, the system prompt and the
                recorded name are all properties of the conversation.  Change it
                by resetting the conversation.
            images: Images to send with it, each a `data:` URL.  Refused unless
                the model's config lists `image` under `input` — silently
                dropping an attachment somebody made is worse than saying the
                model cannot read it.
            channel: Where this turn came in from — `tui`, or the key of
                whoever sent it.  Recorded with the turn and used for nothing
                else; the caller is the only party that knows, which is why it is
                a parameter rather than something this server infers.  It is also
                what a turn's footnote carries, so a model reading a conversation
                can tell a person's turn from a worker's.

        Returns:
            `text` (the final answer), `usage`, `steps`, `stop_reason`, and the
            `model` that answered — which a bare provider or an empty reference
            does not otherwise reveal.
        """
        # Asked before anything is spent.  A context plugin that is not there is
        # a broken system rather than a degraded one, and the moment to find that
        # out is *before* the first model call has been paid for — not at the
        # write, when the answer exists and has nowhere to go.
        await memory()
        reap()
        loop, started = conversation(agent, subagent, model)
        if started:
            # A conversation that has just been built is one that has no context
            # in hand, and this is the only moment that is true.  Done here and
            # not under the lock because nothing else can be holding one: the
            # loop was created by *this* call and no other caller has its key.
            await restore_into(loop)

        if len(loop.inbox) >= MAX_QUEUED:
            raise ToolError(
                f"{describe((agent, subagent))} already has {len(loop.inbox)} "
                f"messages waiting, which is the limit.  Wait for them, or reset "
                f"the conversation."
            )

        item = Pending(prompt=prompt, images=list(images or []), channel=channel)
        loop.inbox.append(item)
        outcome = Outcome()
        try:
            async with loop.lock:
                # Ours, and its turn is starting now.  A message waiting in
                # the inbox is *not* in `messages`: `AgentLoop.run_turn`
                # re-reads the list every step, so an early append would be
                # seen by the running turn as steering nobody asked for.
                with contextlib.suppress(ValueError):
                    loop.inbox.remove(item)
                # **Before the turn, and inside the lock.**  It replaces the
                # message list wholesale — anything appended first, which
                # includes this very prompt, would be destroyed by it — and it
                # reads the list the previous turn's write maintains, so it has
                # to wait for that write the way the next turn does.
                chosen = await rebuild_into(loop, item.prompt)
                observer = ProgressObserver(ctx)
                # **Reported before the turn**, because it is a fact about what
                # the turn runs on and not about how it went — and because the
                # answer that follows is the thing it is about.  The
                # discriminator is one model call the caller never sees, so this
                # is the only place its decision is visible at all.
                await observer.on_event(chosen)
                try:
                    await run_turn_into(loop, item, observer, outcome)
                except BaseException:
                    # A turn that happened is recorded however it ended, and this
                    # is deliberately one clause rather than a handler per ending
                    # — see `remember_turn` for why the rule is written that way.
                    # A provider that answered with a 500 is the case that made it
                    # matter: the same turn that leaves the user's message in the
                    # transcript must not leave it missing from the record, which
                    # is the failure a write conditional on *how* a turn ended
                    # produces.
                    #
                    # Detached for two reasons.  From a cancelled handler a plain
                    # await is cancelled again, which is the measurement `detach`
                    # records.  And on any other failure the store may be the very
                    # thing that is gone, where waiting on it would replace the
                    # error the caller needs to see with the one it caused.
                    #
                    # Its id is deliberately dropped: this turn's messages are left
                    # **unbacked**, so the next rebuild carries them verbatim
                    # rather than fetching a turn whose id the list on disk may or
                    # may not have by then.  Appending here would race that write
                    # and could count the same messages twice.
                    if outcome.messages:
                        detach(record(loop, item, outcome))
                    raise
                else:
                    turn_id = await record(loop, item, outcome)
                    if turn_id is not None:
                        loop.turn_ids.append(turn_id)
                        # The turn's own messages are covered now, so what was
                        # carried stays carried and nothing else does.
                        loop.covered = len(loop.messages)
                        # ...and it is addressable now, which is what the next
                        # turn's discriminator needs: a turn it cannot name is
                        # one a keep-list cannot keep.
                        annotate_turn(outcome, item, turn_id)
        finally:
            # On every path, including a cancellation while queued: the turn
            # never started, so there is nothing to record, but the inbox entry
            # must not be left behind.
            loop.last_used = time.monotonic()
            with contextlib.suppress(ValueError):
                loop.inbox.remove(item)
                # On every path, including a cancellation while queued: the turn
                # never started, so there is nothing to record, but the inbox
                # entry must not be left behind.
                loop.last_used = time.monotonic()
                with contextlib.suppress(ValueError):
                    loop.inbox.remove(item)

        result = outcome.result
        return {
            "text": result.text if result else "",
            "usage": result.usage.to_wire() if result else None,
            "steps": result.steps if result else 0,
            "stop_reason": result.stop_reason if result else "cancelled",
            "model": loop.model,
        }

    @mcp.tool
    async def transcript(agent: str, subagent: str = "") -> dict[str, Any]:
        """What a conversation is made of, for a screen that has just opened.

        The restoring read's second reader.  `restore` exists because a
        conversation that has just been *built* has to be put back on its
        context before it can run; a terminal that has just been opened asks the
        opposite question — the context is fine, and what it needs is to show
        the conversation the previous terminal was showing.  One read answers
        both, and this is it: the same list, answered with the turns it was
        built from rather than with the message list.

        **A read, deliberately.**  Opening a window must not start, end or
        rebuild anything, so this touches neither the loops nor the list — which
        is also why it is the *stored* list rather than the in-memory one: a
        window opened against a server that has been up for a week still shows
        the conversation, and so does one opened against a server that has just
        started.

        Args:
            agent: Whose conversation.
            subagent: Which of that agent's conversations; empty for its own.

        Returns:
            `turns`: the stored turns this conversation's context is made of, in
                the list's own order, oldest first.  Empty for a conversation
                that has never run — the honest answer for a genuinely new
                name, and not an error.
        """
        payload = tool_payload(
            await (await memory()).call_tool(
                "restore",
                {
                    "agent": agent,
                    "subagent": subagent,
                    # Read and thrown away: `restore` answers with a rebuilt
                    # message list as well, and that half is the agent loop's.
                    # The opening prompt is what a conversation that has never
                    # run would head with, so it is the honest thing to hand
                    # over rather than an empty list.
                    "messages": [m.to_wire() for m in opening_messages(agent)],
                },
            )
        )
        turns = payload.get("turns")
        return {"turns": turns if isinstance(turns, list) else []}

    @mcp.tool
    async def reset(agent: str, subagent: str = "") -> dict[str, Any]:
        """Forget a conversation, and its context with it.

        Idempotent, and deliberately not an error when there was nothing there:
        the caller's intent — that this conversation should not continue — is
        satisfied either way, and a caller that had nothing to forget is in
        exactly the state it asked for.

        **The stored live-context list is cleared too**, and that is a change:
        dropping the in-memory loop used to be the whole of it, and it cannot be
        any more, because a key that has just been dropped is a key whose next
        message *restores*.  Forgetting a conversation and having it come back on
        the next prompt is not forgetting it.

        Only the *conversation* is forgotten.  The turns it produced are in the
        log — storage rather than state — and clearing those is a different
        request with a different blast radius: `turn_list` still finds every one
        of them, they are simply no longer what this conversation is made of.

        Args:
            agent: Whose conversation.
            subagent: Which of that agent's conversations; empty for its own.

        Returns:
            `reset`: whether there was a conversation there to forget.
        """
        await forget_context(agent, subagent)
        forgotten = loops.pop((agent, subagent), None) is not None
        logger.debug(
            "reset: %s %s",
            describe((agent, subagent)),
            "forgotten" if forgotten else "was not running",
        )
        return {"reset": forgotten}

    return mcp


def _with_images(
    prompt: str, images: list[str], config: Config, model: str
) -> str | list[dict[str, Any]]:
    """The user's message: text, or text and images.

    The model has to be *able* to read them.  A config that does not list
    `image` under a model's `input` is the config saying so, and the alternative
    to refusing is worse than it looks: the images would be dropped somewhere
    along the way and the user would be left wondering why the model ignored
    what they attached.
    """
    if not images:
        return prompt

    settings = config.resolve(model)[2]
    if not settings.accepts_images:
        raise ValueError(
            f"{settings.model} cannot read images "
            f"(its config lists input: {', '.join(settings.input)})"
        )

    parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    parts += [{"type": "image_url", "image_url": {"url": url}} for url in images]
    return parts


def main(argv: list[str] | None = None) -> int:
    args = parse_serve_args(argv, SERVER_NAME)
    configure_logging()
    config_path = find_config_path()
    config = load()
    settings = config.agent.server

    logger.info(
        "serving %s on http://%s:%d%s (default model: %s)",
        SERVER_NAME,
        args.host or settings.host,
        args.port or settings.port,
        settings.path,
        config.default,
    )
    serve(
        build_server(config),
        settings,
        args,
        name=SERVER_NAME,
        config_path=config_path,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
