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

That queue carries the plugin's *own* message as well as the mesh's — the orphan
notice (§1.8), which is the one thing here that nobody asked for and the one
thing whose delivery this plugin repeats, for the reason it has no peer behind
it: a peer re-sends its task when it gets no answer, so nothing a peer sent is
ever offered twice.

**And it carries a second kind of thing, which is not a message at all.**  A
presence transition — a peer arriving, a peer going — is a *note*: the window
draws it and nothing else happens, no turn is run and the conversation never
learns of it.  v1 kept presence out of the inbox for the same reason (its TUI had
an activity feed and its model a per-turn prompt); here the roster a model can
ask for is `a2a_list_agents`, and a peer that wants to be answered sends a task.
One queue, one writer, two kinds — because arrival order is worth keeping across
both.

**Who is on the mesh is a window, not a ledger.**  v1's plugin *was* its agent's
process, so being up and being online were the same fact.  A shared process has
to decide, and reading it off `agents/<agent>/a2a.yaml` — the record of inbound
tasks a process died holding — made it mean "has spoken at least once, ever": a
card for a conversation nobody was having, and none for the window somebody had
just opened.  So the mesh is joined by `join` for as long as a window holds that
call, and by an `a2a_*` call for as long as the task it started may be answered.
That ledger is still the plugin's own file, and the orphan notice is still its
job; it is just no longer an enrollment list.

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

import slife2.timeouts as _T
from slife2.a2a.broker import ensure as ensure_broker
from slife2.a2a.inbound_store import InboundStore
from slife2.a2a.mesh import (
    A2AMesh,
    AgentCard,
    format_presence_line,
    safe_name,
)
from slife2.audience import FOR_THE_MODEL
from slife2.config import A2A_SERVER_NAME, Config, load_cached
from slife2.handover import DeliveryFailed, hand_over, note_to_window
from slife2.mcp_server import agent_reader, house_server, serve_plugin
from slife2.paths import a2a_inbound_paths
from slife2.prompt import render

logger = logging.getLogger(__name__)

SERVER_NAME = A2A_SERVER_NAME

#: This server's key in the config's `servers:` table — and the name of the
#: section it reads, which is the one section here whose *presence* is a
#: setting: a machine with no `a2a:` is a machine that is not on the mesh
#: (`slife2.config.A2ASettings`).
CONFIG_KEY = "a2a"

#: The four tools the model gets — v1's four, under v1's names.  A
#: `task_response` is not a fifth: it is `a2a_send_message` with `message_type`
#: said, because on the wire it *is* the same operation and a second name for it
#: would be a second thing for a model to learn.
A2A_SEND_MESSAGE = "a2a_send_message"
A2A_CANCEL_TASK = "a2a_cancel_task"
A2A_LIST_AGENTS = "a2a_list_agents"
A2A_BROADCAST = "a2a_broadcast"

#: The tool that puts an agent *on* the mesh — and it is not the model's.  A
#: window calls it and holds it open; see `join` for why the mesh's membership
#: is a call rather than a state the plugin keeps.  Spelled here because a name
#: crossing a process boundary is written in two places
#: (`slife2.tui.client`), like the tui plugin's `attach` and `submit`.
JOIN = "join"

#: The two reasons an agent is on the mesh (`Session.reasons`), and they differ
#: in whether they ever go away.  `USED` is a tool's: it is added when a call
#: needs the mesh and never released, because what follows a send is a reply the
#: agent is waiting for.  `WINDOW` is a window's (`join`), and it is released the
#: moment that window stops holding the call.
USED = "used"
WINDOW = "window"

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
    "task_request",
    "task_response",
    "cancel_task",
    "message",
    "broadcast",
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

#: What the orphan notice is handed over as — and **it is not a peer.**  The name
#: is the second half of the channel `channel_for` builds (`a2a:orphans`), which
#: the window draws on its own (`A2A(orphans)> `) rather than as somebody
#: speaking, and the writer's log line names it the same way.
ORPHAN_PEER = "orphans"

#: How long the writer waits before offering an orphan notice again, and how many
#: times it will.  **The first attempt is expected to fail**, and not because
#: anything is broken: a notice is handed over at a start whose peers cannot run
#: a turn yet — the hub waits for every plugin, so it is up *after* this one
#: (`slife2.launcher.NEEDS`) — and the delay is what lets the peers the turn
#: needs arrive underneath it.  Tens of seconds is what the launcher's own waves
#: cost, so the count is generous against that and still a bound: a notice nobody
#: can receive is not a reason to keep saying it.
ORPHAN_RETRIES = 30

#: What a call that does not say which agent it is for is told.  A shared process
#: cannot guess it: one `slife2-a2a` serves every agent, and a mesh identity is
#: the agent's own name.


def marker(
    agent_name: str, task_id: str | None = None, type: str = "task_request"
) -> str:
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
        payload = json.loads(text[len(MARKER_PREFIX) : end])
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
    #: Why this agent is on the mesh — and the mesh lives exactly as long as this
    #: set has something in it.  `USED` is added by any call that needs the mesh
    #: and is never taken back: a send is followed by a reply the agent is
    #: waiting for, so an agent with a task outstanding *is* on the mesh and
    #: nothing inside this process's life makes that stop being true.  `WINDOW` is
    #: added by `join` for as long as its call is open, which is what makes a
    #: closed window show up as `✗ offline` rather than as a card nobody retires.
    reasons: set[str] = field(default_factory=set)

    def hold(self, reason: str) -> None:
        """Add one reason for this agent to be on the mesh."""
        self.reasons.add(reason)

    def release(self, reason: str) -> bool:
        """Give up one reason.  True when it was the last one."""
        self.reasons.discard(reason)
        return not self.reasons

    def post(
        self,
        text: str,
        peer: str,
        what: str,
        retries: int = 0,
        *,
        note: bool = False,
    ) -> None:
        """Queue one line for the writer — a message, or a note with `note`.

        Never blocks and never raises: this runs on the mesh's reader, and a
        delivery that has to wait is a delivery that stops the mesh reading.
        Overflow drops the oldest waiting message rather than the newest.

        `retries` is how many further times the writer may offer this one after a
        hand-over that came back "nothing was written", and it is zero for
        everything a peer sent: a peer re-sends a task itself when it gets no
        answer, and a second copy of its message is worse than a late one.  The
        orphan notice is the one message with no peer behind it, so its delivery
        is this plugin's to repeat (`_drain`).

        `note` says this line is the harness's rather than anybody's: the writer
        draws it in the window that is showing this agent and goes no further
        (`slife2.handover.note_to_window`), so it is never a turn, never stored,
        and never retried.
        """
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            logger.warning("a2a_delivery_overflow dropped=1 peer=%s", peer)
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait((text, peer, what, retries, note))


async def _hold_open(ctx: Context) -> None:
    """Wait for the window that called `join` to go away, and notice by writing.

    **Silence is not a signal.**  A stream closed while nothing was being written
    on it may sit unnoticed until something tries to write — and this call is
    *defined* by writing nothing, so a window that was killed rather than quit
    could leave its agent advertised online for the life of the process.  That is
    the lie this whole arrangement exists to avoid, so the hold says something
    instead: the same keepalive `MeshResponder` pings a working task's stream
    with, on the same cadence, and the first ping that fails is the departure —
    it raises, and `join`'s `finally` takes the agent off the mesh.

    **A caller that sent no progress token gets no keepalive**, because
    `report_progress` is then a silent no-op (`slife2.events`): such a window is
    still held and still leaves when its stream is cancelled, but nothing here
    can hasten the noticing, and that is a fact about the caller rather than
    something to paper over.
    """
    while True:
        await asyncio.sleep(_T.timeouts.deliver.keepalive)
        await ctx.report_progress(0, None, None)


def _wire(session: Session) -> None:
    """Attach this session's callbacks to its mesh.

    Each one renders the message v1's drain loop rendered for the same event,
    from the same fields — the shapes are v1's deliberately, because a
    conversation that reads the same in both builds is one less thing to
    relearn.
    """

    def on_inbound_task(
        source: str,
        content: str,
        task_id: str,
        kind: str = "task",
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
        corr_id: str,
        result: str,
        cancelled: bool,
        peer: str,
        kind: str = "task",
        failed: bool = False,
    ) -> None:
        if not result and not cancelled:
            return
        if failed:
            # **A send that never landed is told as itself.**  The mesh's
            # requester ladder ran out with no reply at all (`_deliver`), so
            # this is not the peer answering and must not be rendered as one:
            # "completed"/"replied" over a message no peer sent is exactly the
            # confident negative the model would act on instead of re-sending
            # (DESIGN.md §10.1).  The words are the mesh's — the failure is the
            # requester's, not the peer's — and the marker and channel stay a
            # terminal reply's, so the line lands where the task was discussed.
            if kind == "message":
                text = (
                    f"{marker(peer, type='message')}"
                    f"**{peer}** was never reached, so nothing will answer your "
                    f"message:\n\n{result}"
                )
            else:
                text = (
                    f"{marker(peer, corr_id or None, type='task_response')}"
                    f"**{peer}** was never reached, so no result will arrive for "
                    f"this task (ID: `{corr_id}`):\n\n{result}"
                )
        elif kind == "message":
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
        #
        # **A note, not a message** — which is where this parts company with
        # every other callback here.  A peer coming online asks the model for
        # nothing: the peer that wants to talk sends a task, and the roster is
        # `a2a_list_agents`' answer.  Handing a transition to the conversation
        # instead spends a whole turn and a context on a fact nobody has to act
        # on, which is what v1 avoided by never putting presence in the inbox.
        text = format_presence_line(card, event)
        if text is None:
            return
        session.post(
            text,
            safe_name(card.agent_name),
            f"{card.agent_name} is {event}",
            note=True,
        )

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
) -> None:
    """Hand over what the mesh delivers, one message at a time, in order.

    **Two kinds of thing take this one road, and that is the point of it.**  A
    message is handed to the conversation the window is running
    (`slife2.handover.hand_over`: the window, and nothing else) and a note is
    drawn in that window and nowhere else.  Sharing the queue is what keeps them
    in arrival order with each other — a peer's arrival line lands between the
    two turns it happened between — and one writer is what keeps a mesh callback
    off the MQTT reader's stack.

    **A delivery that fails is an error, and the message is not carried on
    anywhere.**  There is no second road for it (`slife2.handover`), so a peer's
    message that reached no conversation has not been answered — and the one
    party who can do something about that is the peer, which is why the line
    says whose message it was.  Nothing is softened into a state here: this
    plugin has no ledger to write it into and no right to claim the work
    happened.

    **The retry below is this plugin's own notices, and the count is the queue
    item's.**  An orphan notice is a message this process owes a peer about work
    its predecessor died holding: nobody will re-send it, and the window may be
    attaching right now, so it gets another attempt after `orphan_retry` — which
    is the delay that makes the wait worth having, and the notice goes to the
    *back* of the queue so everything that arrived meanwhile is handed over
    first, in arrival order.  A peer's message carries zero, because a peer that
    wants it again can send it again.

    **A message that cannot be delivered must not take the writer with it.**
    This task is one per agent for the life of the process and nothing watches
    it until shutdown, so a raise escaping the loop is silent and permanent:
    the mesh still reports connected, and every later arrival queues behind a
    writer that no longer exists.  So each message is guarded on its own and a
    failure is logged and dropped, and the loop goes on to the next.
    """
    while True:
        text, peer, what, retries, note = await session.queue.get()
        try:
            if note:
                # A harness line for whoever is looking, and that is the whole of
                # where it goes: a note is not a turn anybody runs, and it is not
                # retried (a `✗ offline` that arrives after the window has moved on
                # is worth less than silence).
                if (
                    await note_to_window(
                        config, session.agent, text, client=window_client
                    )
                    is False
                ):
                    logger.debug(
                        "a2a_note_no_window agent=%s line=%s", session.agent, what
                    )
                continue
            try:
                await hand_over(
                    config,
                    session.agent,
                    text,
                    channel_for(peer),
                    what=what,
                    window_client=window_client,
                )
            except DeliveryFailed as exc:
                # **An error, because the words are the only thing that could
                # answer `peer` and they went nowhere.**  Its own key rather than
                # the handler's below, which is about this plugin's own fault:
                # this one is a conversation that could not be reached.  The
                # count is logged rather than the text: the text is the
                # conversation's business.
                logger.error(
                    "a2a_delivery_unreachable agent=%s peer=%s chars=%d: %s",
                    session.agent,
                    peer,
                    len(text),
                    exc,
                )
                if retries > 0:
                    await asyncio.sleep(_T.timeouts.pacing.orphan_retry)
                    session.post(text, peer, what, retries=retries - 1)
        except asyncio.CancelledError:
            # `shutdown` cancels this writer; a cancellation is not a
            # delivery failure and must reach the awaiting task as one.
            raise
        except Exception:  # noqa: BLE001 — one bad message must not end the writer
            # **Not fatal, and the difference is this agent's whole mesh.**
            # This task is never awaited until shutdown, so an exception
            # escaping here used to kill the writer *silently*: every later
            # inbound task, result, broadcast and presence event queued and
            # was never handed over, for the life of the process, while the
            # mesh still looked connected and the model simply never heard
            # anything.  One message that could not be rendered or handed
            # over is dropped — a peer re-sends its own task, and the notice
            # has its retries — and the next one is read.
            logger.exception(
                "a2a_delivery_failed agent=%s peer=%s — that message dropped",
                session.agent,
                peer,
            )


def announce_orphans(session: Session) -> None:
    """Queue the notice about the inbound tasks this agent's predecessor died holding.

    **This is the one thing this plugin says that nobody asked for**, and v1 said
    it differently: the orphan list rode every turn's prompt, so the model could
    not miss it.  There is no per-turn prompt here, so the same information is
    handed over once, on the channel the window draws as its own bubble — the
    peer is waiting, and a task nobody can complete is a debt the agent has to be
    told about to pay.

    **Queued, and not handed over from where the orphan is found.**  Both halves
    of that are paid for rather than defensive.  The start that finds an orphan
    is a start whose peers cannot run a turn yet — a turn's first step is the
    hub, and the hub waits for every plugin, so it is up *after* this one
    (DESIGN.md §4) — so a hand-over awaited here is lost by construction; and
    when it is not lost it is worse, because the turn it runs takes as long as a
    model takes, and the deadline judging *this* plugin is the launcher's
    `_T.timeouts.ready.plugin_start` (90 s by default).  Queued, the notice
    rides the one writer every arrival already takes (`_drain`), which is this
    session's own machinery, and the retries it carries are what let it outlast
    the launcher's start-up instead of dying of it.

    A `task_response` for any of these is refused by the mesh, so the notice
    names the one reply that still works.
    """
    orphans = session.mesh.stale_inbound()
    if not orphans:
        return
    session.post(
        render(ORPHAN_TEMPLATE, tasks=orphans),
        ORPHAN_PEER,
        f"{len(orphans)} orphaned inbound task(s)",
        retries=ORPHAN_RETRIES,
    )


def build_server(
    config: Config,
    *,
    window_client: Client | None = None,
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

        Built rather than connected, and the difference is the whole of what
        enrollment means here: building writes the agent's ledger
        (`InboundStore.open`), which is the record of the *inbound tasks a
        process died holding* — a fact about work already received, not a claim
        about who is here now.  What connects is `join` (a window is showing the
        agent) or the first `a2a_*` call that needs the mesh.
        """
        found = sessions.get(agent)
        if found is None:
            found = Session(agent=agent, mesh=A2AMesh(live().a2a, agent))
            _wire(found)
            found.writer = asyncio.create_task(_drain(found, live(), window_client))
            sessions[agent] = found
        return found

    async def connected(agent: str, reason: str) -> Session:
        """The agent's mesh, connected — or a refusal naming why it is not.

        Double-checked under the session's lock, v1's shape: two calls arriving
        together must not both dial the broker, and a mesh whose connect failed
        is discarded rather than half-kept, so a retry starts from nothing.

        **`reason` is the caller's, and it is what the mesh's lifetime is made
        of** (`Session.reasons`).  A caller that is waiting for something back —
        every tool here but `join` — holds `USED`, for the rest of this process's
        life: what follows a send is a reply the agent is waiting for, and an
        agent with a task outstanding is on the mesh in the only sense that
        matters to the peer answering it.  A window passes `WINDOW`, which it
        gives back when its call ends.  Which one it is has to be said by the
        caller: a `join` that inherited a tool's reason would keep the agent
        online after the window it was joining for had gone.
        """
        settings = live().a2a
        if not settings.configured:
            raise ToolError(
                "this machine is not on the A2A mesh — there is no `a2a:` "
                "section in slife2.yaml, which is the whole of the switch."
            )
        session = session_of(agent)
        if session.mesh.is_connected:
            session.hold(reason)
            return session
        async with session.lock:
            if session.mesh.is_connected:
                session.hold(reason)
                return session
            try:
                await session.mesh.connect()
            except Exception as e:
                session.connect_error = str(e)
                # connect() can fail mid-way (broker refused, timeout, our name
                # already taken) — never leak a half-started mesh on retry.
                try:
                    await session.mesh.disconnect()
                except Exception as teardown:  # noqa: BLE001 - a teardown that carries a protocol fact is reported, not swallowed: a responder that advertised before the outbound connect failed is still on the broker, and a failure here leaves it advertised with nobody to retire it (DESIGN.md §1.8)
                    logger.error(
                        "a2a %s: the mesh would not be torn down after a failed "
                        "connect, so it may stay advertised: %s",
                        agent,
                        teardown,
                    )
                raise ToolError(
                    f"could not join the mesh as {agent!r} on {settings.address}: {e}"
                ) from e
            session.connect_error = ""
            session.hold(reason)
            logger.info("a2a_mesh_connected agent=%s", agent)
        return session

    #: The refusal this plugin says, with its own reason rather than the shared
    #: one: a peer sees an *identity*, not a piece of somebody's state.
    agent_of = agent_reader(
        "mesh it could be sent from",
        "",
        why="one process serves every agent, and the agent name is the identity "
        "a peer sees",
    )

    async def announce_ledgers(config_now: Config) -> None:
        """Tell the model about what a previous process died holding.

        **The ledger is the orphan record, and that is all it is.**  It used to
        be read here as an enrollment list too — a start joined the mesh as every
        agent that had ever made an `a2a_*` call — and that made an agent's
        presence depend on having spoken once and then last forever: a card for a
        conversation nobody was having, which is the thing `slife2.paths` says
        the ledger must not become.  What puts an agent on the mesh is a window
        showing it (`join`); what this does is the other half of the file's job,
        which no window can do for it — a peer is still owed an answer, and the
        only party who can give one is the model, at a start that is *not* the
        one that received the task.

        The broker is asked about first, and it is the one thing not tolerated:
        a machine whose config says `a2a:` has said it wants the mesh, so an
        absent broker is a missing dependency rather than a preference.
        """
        # **The broker, before any of it.**  One is started if it can be, and one
        # that will not serve raises out of the lifespan — which is this plugin
        # failing to start, which is `slife2` refusing to start rather than
        # handing the model four tools attached to nothing
        # (`slife2/a2a/broker.py`).
        logger.info("a2a_broker %s", ensure_broker(config_now.a2a))

        for path in a2a_inbound_paths():
            store = InboundStore.at(path)
            if store is None:
                logger.warning("a2a_ledger_without_an_owner path=%s", path)
                continue
            # Queued rather than sent from here: this start's peers cannot run a
            # turn yet — a turn's first step is the hub, and the hub waits for
            # every plugin, so it is up *after* this one — see `announce_orphans`,
            # which is also where the retries come from.  The session is built,
            # not connected: the notice needs the agent's queue and its writer,
            # and being on the mesh is not what a notice is for.
            announce_orphans(session_of(store.agent))

    @contextlib.asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, object]]:
        """Start the broker, and announce what the last process left owed.

        **Nothing joins the mesh here, and that is the change this port needed.**
        v1's plugin *was* its agent's process, so coming up meant coming online; a
        shared process has no such moment, and reading one out of the ledgers made
        presence mean "has spoken at least once, ever" — a card for a conversation
        nobody was having, and none at all for the window somebody had just
        opened.  So what a start does is the half no window can do for it: the
        broker is brought up, and every ledger's orphans are handed to the model.
        An agent comes onto the mesh when a window shows it (`join`), which is
        also the only thing that takes it off again.

        On shutdown every mesh disconnects, which publishes the offline card that
        retires the names this process was holding rather than leaving cards
        nobody holds.

        **The start-up work is inside the guard, and that is the point of the
        guard.**  A start that fails halfway — the broker comes up, one agent's
        session is built, and the next step raises (a template that will not
        render, a ledger path that will not read) — would otherwise leave that
        session's `_drain` writer running with nothing watching it.  The `finally`
        is what hands those back, so it has to be entered *before* the first
        session exists rather than after the last one succeeded.
        """
        try:
            if connecting and live().a2a.configured:
                await announce_ledgers(live())
            yield {}
        finally:
            for session in sessions.values():
                if session.writer is not None:
                    session.writer.cancel()
                try:
                    await session.mesh.disconnect()
                except Exception as e:  # noqa: BLE001 - a teardown that carries a protocol fact is reported, not swallowed: the offline card is how a peer is told this agent left, and a failure here leaves it advertised online with nobody to retire it (DESIGN.md §1.8)
                    logger.error(
                        "a2a %s: the mesh would not leave the broker on "
                        "shutdown, so it may stay advertised: %s",
                        session.agent,
                        e,
                    )
            for session in sessions.values():
                if session.writer is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await session.writer
            sessions.clear()

    #  No `instructions`: each tool's own description says what a model cannot
    #  work out for itself — `house_server` says the same thing about the spec's
    #  own advice, and the other plugins here make the same choice.
    mcp: FastMCP = house_server(SERVER_NAME, lifespan=lifespan)

    @mcp.tool
    async def join(agent: str, ctx: Context) -> None:
        """Put *agent* on the mesh, and keep it there for as long as this call.

        **Hold this call open for as long as a window is showing that agent.**
        It does not return: the mesh joins on entry and its cancellation is the
        departure, so a window that quits, is killed, or loses its stream takes
        its agent off the mesh with nothing to send — the offline card and the
        last will are then both honest about when it left.  That is the contract
        `slife2.tui.control`'s `attach` has on the same window's inbox, and it is
        the same lifetime for the same reason: what a peer is told about an agent
        should be decided by whether anyone is there, not by which process
        happened to be running.

        **This is the enrollment.**  The ledger at `agents/<agent>/a2a.yaml` is
        the record of inbound tasks a process died holding, which is a fact about
        work already received rather than a claim about who is here; a window is
        the only thing that is both true while somebody is present and untrue the
        moment they leave.  An `a2a_*` call still joins its agent lazily, for as
        long as the task it started may be answered.

        **Nobody is told that the agent itself arrived.**  A window showing *jack*
        is jack; a line reading `⚡ jack online` in it is the harness telling
        somebody what they already are — and the mesh's own discovery path has
        never announced it for the same reason (`_handle_discovery` skips our own
        topic).  What a window shows is its *peers* coming and going, which is
        the one thing it cannot otherwise know.

        Raises:
            ToolError: If the mesh refused the agent — the refusal, verbatim: the
                name already in use on this broker, or a broker that is not
                there.  A caller that retries should say so once rather than once
                per attempt: the reason does not change between them.
        """
        session = await connected(agent, WINDOW)
        try:
            await _hold_open(ctx)
        finally:
            # Only when this was the last reason: an agent that has sent
            # something is waiting for a reply, and a window closing is not a
            # reason to stop listening for it.  The reason itself was taken by
            # `connected`, which is where every caller says which one it is.
            if session.release(WINDOW):
                try:
                    await session.mesh.disconnect()
                except Exception as e:  # noqa: BLE001 - a teardown that carries a protocol fact is reported, not swallowed: the offline card is how a peer is told this agent left, and a failure here leaves it advertised online with nobody to retire it (DESIGN.md §1.8)
                    logger.error(
                        "a2a %s: the mesh would not leave the broker as its "
                        "window left, so the agent may stay advertised: %s",
                        session.agent,
                        e,
                    )

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
        session = await connected(who, USED)
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
        session = await connected(who, USED)
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
        session = await connected(who, USED)
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
