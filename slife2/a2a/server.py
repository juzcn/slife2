"""slife2-a2a — the A2A mesh, as a plugin.

v1's `slife.plugins.a2a.server`, and the two things that changed are §4's and
§1.2's rather than anything about the mesh.

**§4: one process, one mesh per agent.**  v1 spawned one plugin per agent, so its
module globals *were* that agent's — one mesh, one set of queues, one identity.
slife2's servers are shared, so the same things live in a :class:`Session` keyed
by the **calling agent** (the first half of the client id every call carries),
exactly as `slife2-wechat`'s login does.  That is not a formality here: the agent
name *is* the mesh identity — the MQTT client id and the request topic's last
segment — so a second agent is a second connection with a second card, and the
name-collision check that protects it is per agent rather than per machine.  A
peer on this machine and a peer on another one are the same thing to all of this:
both are agents on the broker, and nothing in the code can tell them apart.

**§1.2: the drain tool was the seam, and the seam is gone.**  v1's harness polled
`__a2a_drain_incoming` once a second and injected what it found into the agent's
inbox.  There is no harness here, and what replaced that seam is
`slife2.handover`: an inbound message is handed to the terminal showing that
conversation — the same queue the person's own typing goes into — and to the
agent server when no window is showing it.  So the five queues become **one
ordering queue per session** with one writer behind it, which is the job the
queues were doing for the harness: they kept arrival order, and they kept a mesh
callback from blocking the MQTT reader loop.

Two things v1 had here are deliberately absent.  `__check` has no port: v2 has no
`system_health` and no `__`-prefixed tools at all, and what it reported is now
`a2a_list_agents`' answer, which is where a model would look anyway.  And the
orphan reminder is no longer a line in a per-turn prompt — there is no per-turn
prompt — so it is a message handed over once, at the start that found it.

**And the broker is started rather than merely probed**, which is the one place
this port contradicts v1's own rule and is the operator's call rather than a
discovery: v1 left mosquitto to the user and treated a listener as the feature's
on/off switch, where here a config that says `a2a:` has said it wants the mesh.
`slife2/a2a/broker.py` holds the policy and the hard failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

from slife2.a2a.broker import ensure as ensure_broker
from slife2.a2a.inbound_store import InboundStore
from slife2.a2a.mesh import (
    A2AMesh,
    AgentCard,
    format_presence_line,
    safe_name,
)
from slife2.audience import FOR_THE_MODEL, request_client
from slife2.config import A2A_SERVER_NAME, Config, load_cached
from slife2.handover import hand_over
from slife2.mcp_server import house_server, serve_plugin
from slife2.paths import a2a_inbound_paths
from slife2.prompt import render

logger = logging.getLogger(__name__)

SERVER_NAME = A2A_SERVER_NAME

#: This server's key in the config's `servers:` table — and the name of the
#: section it reads, which is the one section here whose *presence* is a
#: setting: a machine with no `a2a:` is a machine that is not on the mesh
#: (`slife2.config.A2ASettings`).
CONFIG_KEY = "a2a"

#: The four tools — v1's four, under v1's names, and the whole surface.  A
#: `task_response` is not a fifth tool: it is `a2a_send_message` with
#: `message_type` said, because on the wire it *is* the same operation and a
#: second name for it would be a second thing for a model to learn.
A2A_SEND_MESSAGE = "a2a_send_message"
A2A_CANCEL_TASK = "a2a_cancel_task"
A2A_LIST_AGENTS = "a2a_list_agents"
A2A_BROADCAST = "a2a_broadcast"

#: The channel an inbound message arrives on, and the first half of what it is
#: keyed by: `a2a:jill`.  The window draws a bubble from the channel alone
#: (`slife2.tui.widgets.channel_prefix`), so the peer has to be *in* it for a
#: person to see who spoke — and v1's bubble, `A2A(jill)> `, is what it draws.
CHANNEL = "a2a"

#: What the marker opens with.  A constant because the marker is *read* as well
#: as written: stored text carries it into a transcript, and the window takes it
#: back off by this string rather than by a second copy of the spelling.
MARKER_PREFIX = "[A2A:"

#: The `type` values an inbound message can carry — the vocabulary the marker's
#: `type` field is drawn from, and v1's list unchanged.  `task_request` (answer
#: it), `task_response` (an auto-delivered result), `cancel_task` (the sender
#: withdrew it), `message` (a conversation), `broadcast` (an event).
A2A_TYPES = (
    "task_request", "task_response", "cancel_task", "message", "broadcast",
)

#: How many inbound messages may be waiting for a hand-over.  v1's bound on its
#: queues, and for v1's reason: a peer flooding a conversation must not grow this
#: process without limit.  Past it the *oldest* waiting message is dropped, which
#: is the one whose moment has most likely passed.
MAX_QUEUED = 500

#: The template the orphan notice is rendered from.  Machinery rather than
#: something an operator writes, like `schedule.j2`, so it is asked for by bare
#: name and is the shipped one.
ORPHAN_TEMPLATE = "a2a_orphans.j2"

#: What a call that does not say which agent it is for is told.  A shared process
#: cannot guess it: one `slife2-a2a` serves every agent, and a mesh identity is
#: the agent's own name.
UNADDRESSED = (
    "This call did not say which conversation it is for, so there is no mesh "
    "it could be sent from — one process serves every agent, and the agent name "
    "is the identity a peer sees."
)


def marker(agent_name: str, task_id: str | None = None, type: str = "task_request") -> str:
    """Content prefix for ANY inbound A2A message — one envelope, a type field.

    ``[A2A:{"from": …, "task_id": …, "type": …}] `` — ``from`` names the
    *sending* peer (never the receiver).  ``type`` tells what it is:
    ``task_request`` (an inbound task to answer, via
    ``a2a_send_message(message_type="task_response", task_id=…)`` — it may take
    many turns), ``task_response`` (an auto-delivered task result),
    ``cancel_task`` (the peer withdrew that task — its completion bridge died
    with it, so a ``task_response`` for it is refused: answer with a plain
    message or stay silent), ``message`` (a bare conversation), ``broadcast`` (a
    fire-and-forget event, informational).  ``task_id`` rides for tasks, results
    and withdrawals.

    The key is ``from``, not ``agent_name`` — that word in a system prompt is
    the agent's own identity, and v1 measured the confusion it caused: receivers
    misread the sender as themselves.  The marker is machine-facing; the window
    drops it for display (the ``A2A(<peer>)> `` bubble prefix instead).
    """
    # ``from`` — the peer's name — deliberately not ``agent_name``: that word
    # is the agent's self-identity, and reusing it here made receivers misread
    # the sender as themselves.
    payload: dict[str, str] = {"from": agent_name, "type": type}
    if task_id is not None:
        payload["task_id"] = task_id
    return f"{MARKER_PREFIX}{json.dumps(payload, ensure_ascii=False)}] "


def a2a_message_type(text: str) -> str | None:
    """The wire ``type`` of an inbound A2A message, read from its marker.

    The marker is the one envelope that classifies every inbound A2A message,
    and it is what the receiver's stored turn carries — so a reader that wants
    to know *what kind of thing* arrived has to ask the text, not a parallel
    field threaded alongside it.  Returns one of :data:`A2A_TYPES`, or ``None``
    when *text* carries no (or a malformed) A2A marker, or a type outside the
    set — the caller then falls back to a type-less label.

    **It is v1's reader and it has no caller in this build**, which is worth
    saying rather than leaving to be discovered.  v1 read the type for the
    window's turn-end line ("✓ A2A task_request from jill handled"), and this
    window labels a turn from its *channel* instead — `channel_prefix` draws
    `A2A(jill)> ` and never looks at the words.  It stays because it is the
    other half of :func:`marker`: the pair is what makes `A2A_TYPES` a
    vocabulary rather than a list, and a reader added later that guessed at the
    envelope instead of asking this would be the second spelling of a format
    this module already owns.
    """
    if not text.startswith(MARKER_PREFIX):
        return None
    end = text.find("]", len(MARKER_PREFIX))
    if end == -1:
        return None
    try:
        payload = json.loads(text[len(MARKER_PREFIX):end])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    mtype = payload.get("type")
    return mtype if mtype in A2A_TYPES else None


def channel_for(peer: str) -> str:
    """The channel an inbound message from *peer* arrives on: `a2a:<peer>`."""
    return f"{CHANNEL}:{peer}"


# ── one agent's mesh, and what is waiting to be handed over ──────────────────


@dataclass
class Session:
    """One agent's mesh, its connect state, and its delivery queue.

    The queue is the piece §1.2's removal of the drain leaves behind.  A mesh
    callback runs on the MQTT reader's stack, so it must not await a hop to
    another server; and arrival order is the order the model should read things
    in.  One queue with one writer behind it is both, and it is exactly what
    v1's five queues were, minus the harness on the far end.
    """

    agent: str
    mesh: A2AMesh
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Why the last connect attempt failed.  A failed mesh is discarded, so this
    #: is the only place the reason survives to `a2a_list_agents` — otherwise it
    #: reports a bare "not connected" for what is really a name collision.
    connect_error: str = ""
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(MAX_QUEUED))
    writer: asyncio.Task | None = None

    def post(self, text: str, peer: str, what: str) -> None:
        """Queue one message for the hand-over writer.

        Never blocks and never raises: this runs on the mesh's reader, and a
        delivery that has to wait is a delivery that stops the mesh reading.
        Overflow drops the oldest waiting message rather than the newest.
        """
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            logger.warning("a2a_delivery_overflow dropped=1 peer=%s", peer)
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait((text, peer, what))


def _wire(session: Session) -> None:
    """Attach this session's callbacks to its mesh.

    Each one renders the message v1's drain loop rendered for the same event,
    from the same fields — the shapes are v1's deliberately, because a
    conversation that reads the same in both builds is one less thing to
    relearn.
    """

    def on_inbound_task(
        source: str, content: str, task_id: str, kind: str = "task",
    ) -> None:
        # Only TASK_REQUEST messages are tasks (task_id present); a MESSAGE
        # conversation arrives task-less with kind="message" — no completion.
        # The marker's `from` names the sending peer — never the receiver — and
        # its task id, so the model can attribute the turn and reference the
        # task it is responding to instead of making one up.
        marker_id = (task_id or None) if kind == "task" else None
        marker_type = "message" if kind != "task" else "task_request"
        session.post(
            f"{marker(source, marker_id, type=marker_type)}{content}",
            source,
            f"an inbound {marker_type} from {source}",
        )

    def on_task_completion(
        corr_id: str, result: str, cancelled: bool, peer: str,
        kind: str = "task",
    ) -> None:
        if not result and not cancelled:
            return
        if kind == "message":
            # A MESSAGE conversation was answered: a bare message reply, no
            # task involved.
            text = (
                f"{marker(peer, type='message')}"
                f"Peer **{peer}** replied to your message:\n\n{result}"
            )
        else:
            state = "cancelled" if cancelled else "completed"
            text = (
                f"{marker(peer, corr_id or None, type='task_response')}"
                f"Peer **{peer}** {state} async task "
                f"(ID: `{corr_id}`):\n\n{result}"
            )
        session.post(text, peer, f"a reply from {peer}")

    def on_agent_change(card: AgentCard, event: str) -> None:
        # Only real transitions are worth anybody's attention: a cold retained
        # offline card is cached by the mesh and never announced, and a
        # non-transition event renders as None (`format_presence_line`).
        text = format_presence_line(card, event)
        if text is None:
            return
        session.post(text, safe_name(card.agent_name), f"{card.agent_name} is {event}")

    def on_peer_cancel(task_id: str, peer: str) -> None:
        """A peer withdrew an inbound task — hand it over like any other message.

        The withdrawal is NOT a control signal: it reaches the model the same
        way every other peer event does, and the model — the only party that
        knows whether it is still working on that task — decides what to do with
        it.  The one constraint rides the marker: the completion bridge died with
        the task, so a `task_response` for it is refused.
        """
        session.post(
            f"{marker(peer, task_id, type='cancel_task')}"
            f"The sender withdrew this task — stop working on it.",
            peer,
            f"a withdrawal from {peer}",
        )

    def on_broadcast_event(sender: str, text: str) -> None:
        session.post(
            f"{marker(sender, type='broadcast')}{text}",
            sender,
            f"a broadcast from {sender}",
        )

    session.mesh.on_inbound_task = on_inbound_task
    session.mesh.on_task_completion = on_task_completion
    session.mesh.on_agent_change = on_agent_change
    session.mesh.on_peer_cancel = on_peer_cancel
    session.mesh.on_broadcast_event = on_broadcast_event


async def _drain(
    session: Session,
    config: Config,
    window_client: Client | None,
    agent_client: Client | None,
) -> None:
    """Hand over what the mesh delivers, one message at a time, in order.

    The three-answer rule is `slife2.handover`'s and this keeps it: `False` is
    the one result that proves nothing was written, `True` means it reached a
    conversation, and `None` means this cannot tell — and **`None` is not tried
    anywhere else**, because the words may already be down the stream and a
    second copy of a peer's message is worse than a late one.  Nothing here
    retries: a peer re-sends a task itself when it gets no answer, and that is
    the retry that knows what it is retrying.
    """
    try:
        while True:
            text, peer, what = await session.queue.get()
            handed = await hand_over(
                config,
                session.agent,
                text,
                channel_for(peer),
                what=what,
                window_client=window_client,
                agent_client=agent_client,
            )
            if handed is not True:
                logger.warning(
                    "a2a_delivery_not_confirmed agent=%s peer=%s handed=%s",
                    session.agent, peer, handed,
                )
    except asyncio.CancelledError:
        raise


def build_server(
    config: Config,
    *,
    window_client: Client | None = None,
    agent_client: Client | None = None,
    source: Callable[[], Config] | None = None,
    connecting: bool = True,
) -> FastMCP:
    """Build the A2A server.

    **Two seams, and both are peers**: the window and the agent server, which
    are the two places a message can be handed to (`slife2.handover`).  A test
    wires recorder versions of both over the in-memory transport and drives the
    mesh's callbacks directly; there is no seam for the mesh itself, because the
    mesh is the thing under test and a broker is what a test would need to
    replace — see `tests/test_a2a.py`, which tests the driver without one.

    `connecting=False` is for a build that must not dial a broker: the lifespan
    resumes nothing.  The section is still read, so the tools answer with what
    the config says rather than with a connection nobody asked for.
    """
    live: Callable[[], Config] = source or load_cached
    sessions: dict[str, Session] = {}

    def session_of(agent: str) -> Session:
        """This agent's mesh, built on first use and kept.

        Built rather than connected: the mesh's constructor writes the agent's
        ledger (`InboundStore.open`), which is also how a *later* start knows to
        resume this agent — the file is the record, as it is for a login and for
        a schedule.
        """
        found = sessions.get(agent)
        if found is None:
            found = Session(agent=agent, mesh=A2AMesh(live().a2a, agent))
            _wire(found)
            found.writer = asyncio.create_task(
                _drain(found, live(), window_client, agent_client)
            )
            sessions[agent] = found
        return found

    async def connected(agent: str) -> Session:
        """The agent's mesh, connected — or a refusal naming why it is not.

        Double-checked under the session's lock, v1's shape: two calls arriving
        together must not both dial the broker, and a mesh whose connect failed
        is discarded rather than half-kept, so a retry starts from nothing.
        """
        settings = live().a2a
        if not settings.configured:
            raise ToolError(
                "this machine is not on the A2A mesh — there is no `a2a:` "
                "section in slife2.yaml, which is the whole of the switch."
            )
        session = session_of(agent)
        if session.mesh.is_connected:
            return session
        async with session.lock:
            if session.mesh.is_connected:
                return session
            try:
                await session.mesh.connect()
            except Exception as e:
                session.connect_error = str(e)
                # connect() can fail mid-way (broker refused, timeout, our name
                # already taken) — never leak a half-started mesh on retry.
                with contextlib.suppress(Exception):
                    await session.mesh.disconnect()
                raise ToolError(
                    f"could not join the mesh as {agent!r} on "
                    f"{settings.address}: {e}"
                ) from e
            session.connect_error = ""
            logger.info("a2a_mesh_connected agent=%s", agent)
        return session

    def agent_of(ctx: Context) -> str:
        """The agent a call is on behalf of, refused rather than guessed."""
        found = request_client(ctx)
        if found is None or not found[0]:
            raise ToolError(UNADDRESSED)
        return found[0]

    async def announce_orphans(session: Session, config_now: Config) -> None:
        """Tell the agent about the inbound tasks its predecessor died holding.

        **This is the one thing this plugin says that nobody asked for**, and
        v1 said it differently: the orphan list rode every turn's prompt, so the
        model could not miss it.  There is no per-turn prompt here, so the same
        information is handed over once, on the channel the window draws as its
        own bubble — the peer is waiting, and a task nobody can complete is a
        debt the agent has to be told about to pay.

        A `task_response` for any of these is refused by the mesh, so the notice
        names the one reply that still works.
        """
        orphans = session.mesh.stale_inbound()
        if not orphans:
            return
        text = render(ORPHAN_TEMPLATE, tasks=orphans)
        await hand_over(
            config_now,
            session.agent,
            text,
            f"{CHANNEL}:orphans",
            what=f"{len(orphans)} orphaned inbound task(s)",
            window_client=window_client,
            agent_client=agent_client,
        )

    async def resume(config_now: Config) -> None:
        """Join the mesh as every agent this plugin has a ledger for.

        The file is the record — there is no registry of agents anywhere
        (`slife2.paths`) — so what a start resumes is exactly what an earlier
        call enrolled.  A failure to *join* is logged and the others go on: one
        agent's name being taken is not a reason for a second agent to be
        unreachable.  The broker is the one thing that is not tolerated, and it
        is asked about first.
        """
        # **The broker, before any of it.**  A machine whose config says `a2a:`
        # has said it wants the mesh, so an absent broker is a missing
        # dependency: one is started if it can be, and one that will not serve
        # raises out of the lifespan — which is this plugin failing to start,
        # which is `slife2` refusing to start rather than handing the model four
        # tools attached to nothing (`slife2/a2a/broker.py`).
        logger.info("a2a_broker %s", ensure_broker(config_now.a2a))

        for path in a2a_inbound_paths():
            store = InboundStore.at(path)
            if store is None:
                logger.warning("a2a_ledger_without_an_owner path=%s", path)
                continue
            agent = store.agent
            try:
                session = await connected(agent)
            except ToolError as e:
                logger.warning("a2a_resume_failed agent=%s err=%s", agent, e)
                continue
            await announce_orphans(session, config_now)

    @contextlib.asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, object]]:
        """Join the mesh eagerly, so a peer can reach this agent unprompted.

        v1's reasoning, unchanged: the mesh announces presence (a retained card)
        and is reachable without any `a2a_*` call, and a *failed* eager connect
        is tolerated — it is logged, and the tools connect lazily on demand.  On
        shutdown every mesh disconnects, which is what publishes the offline card
        that retires this agent's name rather than leaving a card nobody holds.
        """
        if connecting and live().a2a.configured:
            await resume(live())
        try:
            yield {}
        finally:
            for session in sessions.values():
                if session.writer is not None:
                    session.writer.cancel()
                with contextlib.suppress(Exception):
                    await session.mesh.disconnect()
            for session in sessions.values():
                if session.writer is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await session.writer
            sessions.clear()

    #  No `instructions`: each tool's own description says what a model cannot
    #  work out for itself — `house_server` says the same thing about the spec's
    #  own advice, and the other plugins here make the same choice.
    mcp: FastMCP = house_server(SERVER_NAME, lifespan=lifespan)

    # ═══════════════════════════════════════════════════════════════════════
    # The mesh tools (standard A2A operations, v1's four)
    # ═══════════════════════════════════════════════════════════════════════
    #
    # The standard A2A-over-MQTT profile is async-native: a message/send is typed
    # — 'task_request' returns a task id immediately and the result delivers to
    # the conversation later (an [A2A:…] marker of type 'task_response');
    # 'task_response' completes an inbound task; 'message' is a bare
    # conversation.  No waiting, no timeout parameter.

    @mcp.tool(name=A2A_SEND_MESSAGE, meta=FOR_THE_MODEL)
    async def a2a_send_message(
        ctx: Context,
        agent: str,
        message: str,
        message_type: str = "task_request",
        task_id: str = "",
    ) -> dict[str, Any]:
        """Send a message to another agent on the A2A mesh.

        `message_type` decides what it is. 'task_request' (the default) asks for
        work and returns an id straight away — the peer answers later, and its
        answer arrives on its own as an `[A2A:…]` of type 'task_response'. Never
        wait for it and never poll: there is nothing to poll. 'task_response'
        completes a task somebody sent you, and needs the `task_id` from that
        task's `[A2A:…]` marker. 'message' is a plain conversation — no task, no
        completion, and the reply arrives as a message.

        `agent` is a peer's name, which `a2a_list_agents` gives you. A peer may
        be another agent on this machine or one somewhere else; the mesh does not
        tell you which, and nothing you send depends on it.
        """
        if message_type not in ("message", "task_request", "task_response"):
            raise ToolError(
                f"message_type must be 'message', 'task_request' or "
                f"'task_response', got {message_type!r}."
            )
        who = agent_of(ctx)
        # What the arguments say is settled before the mesh is dialled: a call
        # that cannot work must not be the reason a broker is contacted, and the
        # refusal reads better than a connect failure about a typo.
        if message_type == "task_response" and not task_id:
            raise ToolError(
                "task_id is required for message_type='task_response' — it "
                "is the [A2A:…] marker's task_id."
            )
        session = await connected(who)
        if message_type == "task_response":
            answer = session.mesh.complete_task(task_id, message, False)
            if answer != "ok":
                raise ToolError(answer.removeprefix("Error: "))
            return {"peer": agent, "task_id": task_id, "completed": True}
        sent = await session.mesh.send_message(
            agent, message, message_type=message_type
        )
        if message_type == "message":
            return {
                "peer": agent,
                "task_id": sent,
                "message_type": message_type,
                "note": "No task created — the reply arrives as a message.",
            }
        return {
            "peer": agent,
            "task_id": sent,
            "message_type": message_type,
            "note": "The result will arrive in this conversation.",
        }

    @mcp.tool(name=A2A_CANCEL_TASK, meta=FOR_THE_MODEL)
    async def a2a_cancel_task(ctx: Context, agent: str, task_id: str) -> dict[str, Any]:
        """Withdraw a task you sent to another agent.

        `status` is the answer: 'cancelled' when the withdrawal went out,
        'completed'/'failed'/'cancelled' when the task had already ended (a
        finished result is never lost by asking), or 'not_found' for an id this
        process never sent.
        """
        who = agent_of(ctx)
        session = await connected(who)
        status = await session.mesh.cancel_task(agent, task_id)
        return {"peer": agent, "task_id": task_id, "status": status}

    @mcp.tool(name=A2A_LIST_AGENTS, meta=FOR_THE_MODEL)
    async def a2a_list_agents(ctx: Context) -> dict[str, Any]:
        """List the agents on the A2A mesh — you first, then each peer by name.

        Every card is `{"agent_name": …, "status": "online"|"offline"}`, and
        `status` on the first one is your own mesh state. Offline peers are ones
        whose card is still retained on the broker but which have said they are
        leaving, so they are listed rather than hidden.

        `error` says why the list is short when it is: the last failure to join,
        or another agent holding your name — the two states a bare "offline"
        cannot tell apart.
        """
        who = agent_of(ctx)
        settings = live().a2a
        if not settings.configured:
            return {
                "agent_name": who,
                "broker": settings.address,
                "agents": [],
                "error": (
                    "this machine is not on the A2A mesh: there is no `a2a:` "
                    "section in slife2.yaml, and that section is the switch."
                ),
            }
        session = session_of(who)
        mesh = session.mesh
        cards = [
            {"agent_name": safe_name(c.agent_name), "status": safe_name(c.status)}
            for c in mesh.list_agents()
        ]
        error = mesh.collision or session.connect_error
        if not mesh.is_connected and not error:
            error = f"not connected to the broker at {settings.address}"
        return {
            "agent_name": who,
            "status": mesh.status,
            "broker": settings.address,
            "org": settings.org,
            "unit": settings.unit,
            "agents": cards,
            "error": error,
        }

    @mcp.tool(name=A2A_BROADCAST, meta=FOR_THE_MODEL)
    async def a2a_broadcast(ctx: Context, event: str) -> dict[str, Any]:
        """Publish a fire-and-forget event to every agent on the mesh.

        Nobody answers one and it is delivered at most once, so it is for
        information a peer may act on and not for anything you need a reply to —
        use `a2a_send_message` for that. Peers receive it as an `[A2A:…]` of type
        'broadcast', with no task and no completion.
        """
        who = agent_of(ctx)
        session = await connected(who)
        await session.mesh.broadcast(event)
        return {
            "broadcast": event,
            "note": "Sent — fire-and-forget, no reply expected.",
        }

    return mcp


def _ensure_windows_selector_loop() -> None:
    """aiomqtt's socket callbacks (loop.add_reader/add_writer) are unsupported
    by the default Windows Proactor event loop — its connections fail with
    NotImplementedError.  This process is dedicated to the mesh (it spawns no
    subprocesses, which is what the Proactor loop is for), so switching to
    the selector loop is safe here.

    Deliberately NOT at module import: the module is imported in-process by
    tests, where the process-wide policy change would break the suite's
    asyncio-subprocess tests.  Scoped to the plugin entry.
    """
    if sys.platform == "win32":  # pragma: no cover — Windows-only
        try:
            asyncio.set_event_loop_policy(
                asyncio.WindowsSelectorEventLoopPolicy(),
            )
        except (ValueError, AttributeError):
            pass


def main(argv: list[str] | None = None) -> int:
    _ensure_windows_selector_loop()
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        build=build_server,
        logger=logger,
        note=lambda config: (
            f"mesh on {config.a2a.address}, broker started if absent"
            if config.a2a.configured
            else "off the mesh (no `a2a:` section)"
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
