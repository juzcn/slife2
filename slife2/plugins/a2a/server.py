"""slife2-a2a — the A2A mesh, as a plugin.

v1's `slife.plugins.a2a.server`, and the two things that changed are §4's and
§1.2's rather than anything about the mesh.

**§4: one process, one mesh per member.**  v1 spawned one plugin per agent, so its
module globals *were* that agent's — one mesh, one set of queues, one identity.
slife2's servers are shared, so the same things live in a :class:`Session` keyed
by the **client id every call carries**, exactly as `slife2-wechat`'s login does.
That is not a formality here: the name *is* the mesh identity — the MQTT client
id and the request topic's last segment — so a second member is a second
connection with a second card, and the name-collision check that protects it is
per member rather than per machine.  A peer on this machine and a peer on another
one are the same thing to all of this: both are agents on the broker, and nothing
in the code can tell them apart.

**A member is an agent, or one of an agent's subagents** — and the second half of
the client id is an identity on the **local** network and only there, which is the
whole of the difference between the two instances of this module.  Everything the
local one adds is the part a peer without a process needs: a place held for it by
somebody else (:data:`MEMBER`), a road that is not a window (`run_turn`), and a
send path that gives the receiver its place before it publishes (`send_message`).
`sdd` §3.5 and DESIGN.md §1.10 are where those are argued.

**§1.2: the drain tool was the seam, and the seam is gone.**  v1's harness polled
`__a2a_drain_incoming` once a second and injected what it found into the agent's
inbox.  There is no harness here, and what replaced that seam is two roads out of
one queue: an inbound message for an *agent* is handed to the terminal showing
that conversation — `slife2.libs.harness.handover`, the same queue the person's own
typing goes into — and one for a subagent, which has no window and never will, is
run as a turn in its own conversation (`run_turn`).  So the five queues become
**one ordering queue per session** with one writer behind it, which is the job the
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
ask for is `peer_agent_list`, and a peer that wants to be answered sends a task.
One queue, one writer, two kinds — because arrival order is worth keeping across
both.

**Who is on the mesh is a window, not a ledger.**  v1's plugin *was* its agent's
process, so being up and being online were the same fact.  A shared process has
to decide, and reading it off `agents/<agent>/inbound.yaml` — the record of inbound
tasks a process died holding — made it mean "has spoken at least once, ever": a
card for a conversation nobody was having, and none for the window somebody had
just opened.  So the mesh is joined by `join` for as long as a window holds that
call, and by an `a2a_*` call for as long as the task it started may be answered.
That ledger is still the plugin's own file, and the orphan notice is still its
job; it is just no longer an enrollment list.

Two things v1 had here are deliberately absent.  `__check` has no port: v2 has no
`system_health` and no `__`-prefixed tools at all, and what it reported is now
`peer_agent_list`' answer, which is where a model would look anyway.  And the
orphan reminder is no longer a line in a per-turn prompt — there is no per-turn
prompt — so it is a message handed over once, at the start that found it.

**And the broker is started rather than merely probed**, which is the one place
this port contradicts v1's own rule and is the operator's call rather than a
discovery: v1 left mosquitto to the user and treated a listener as the feature's
on/off switch, where here a config that says `peer_agent:` has said it wants
the mesh.
`slife2/plugins/a2a/broker.py` holds the policy and the hard failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.libs.harness.handover import DeliveryFailed, hand_over, note_to_window
from slife2.libs.host.config import (
    AGENT_SERVER_NAME,
    BROADCAST,
    CANCEL_TASK,
    LIST,
    LOCAL,
    REMOTE,
    SEND_MESSAGE,
    TASK_LIST,
    A2ASettings,
    Config,
    Net,
    load_cached,
    net_settings,
)
from slife2.libs.host.paths import a2a_inbound_paths, home, in_home, subagent_dirs
from slife2.libs.plugin.audience import FOR_THE_MODEL, request_client, unaddressed
from slife2.libs.plugin.protocol import (
    close_server,
    describe,
    house_server,
    open_server,
    serve_plugin,
    tool_payload,
)
from slife2.libs.turn.prompt import render
from slife2.plugins.a2a import mesh
from slife2.plugins.a2a.broker import ensure as ensure_broker
from slife2.plugins.a2a.inbound_store import InboundStore
from slife2.plugins.a2a.mesh import (
    A2AMesh,
    AgentCard,
    format_presence_line,
    member_name,
    safe_name,
)

logger = logging.getLogger(__name__)

SERVER_NAME = REMOTE.server_name

#: This instance's key in the config's `servers:` table — and the name of the
#: section it reads, which is the one section here whose *presence* is a
#: setting: a machine with no `peer_agent:` is a machine that is not on that
#: network (`slife2.libs.host.config.A2ASettings`).  **The key is the semantic
#: one and the process is not**: `peer_agent` is what a person writes, and
#: `slife2-a2a` is the plugin that serves it.
CONFIG_KEY = REMOTE.key

#: The remote network's five tools, named.  **`PEER_*` and not `A2A_*`**, because
#: what a model is handed is the semantic side of `Net`: this process speaks A2A,
#: and the model is told it is talking to peer agents (`slife2.libs.host.config.REMOTE`
#: is where that split is argued).  Two things that look like omissions are not: a
#: `task_response` is not a tool, it is `peer_agent_send_message` with
#: `message_type` said, because on the wire it *is* the same operation; and
#: `PEER_TASK_LIST` is not a fifth *operation* either — it reads the two tables
#: the others leave behind, and it is here because "what is still open" is the
#: one question none of them can answer.
#:
#: **These are the remote network's**, and the local instance registers its own
#: from the same verbs (`net.tool`), which is why every registration site composes
#: rather than naming a constant.  They are spelled out because the tests and a
#: reader arriving here want the remote network's names in one place.
PEER_SEND_MESSAGE = REMOTE.tool(SEND_MESSAGE)
PEER_CANCEL_TASK = REMOTE.tool(CANCEL_TASK)
PEER_LIST = REMOTE.tool(LIST)
PEER_BROADCAST = REMOTE.tool(BROADCAST)
PEER_TASK_LIST = REMOTE.tool(TASK_LIST)

#: The tool that puts an agent *on* the mesh — and it is not the model's.  A
#: window calls it and holds it open; see `join` for why the mesh's membership
#: is a call rather than a state the plugin keeps.  Spelled here because a name
#: crossing a process boundary is written in two places
#: (`slife2.tui.client`), like the tui plugin's `attach` and `submit`.
JOIN = "join"

#: The other call that is not the model's: **give up a member's place on the
#: mesh** (`forget`).  Its caller is the agent server, in the middle of
#: `subagent_remove` — a member can stop existing while this process is serving
#: it, and a directory that is gone cannot unsubscribe anything.  Spelled there
#: too, for the reason `JOIN` is spelled in `slife2.tui.client`: a name crossing a
#: process boundary is written in two places, and the pair is held together by
#: those two comments.
FORGET = "forget"

#: The three reasons a member is on the mesh (`Session.reasons`), and they differ
#: in whether they ever go away.  `USED` is a tool's: it is added when a call
#: needs the mesh and never released, because what follows a send is a reply the
#: agent is waiting for.  `WINDOW` is a window's (`join`), and it is released the
#: moment that window stops holding the call.  `MEMBER` is a subagent's, and it
#: is never released either — but for a different reason, and that is why it is
#: a third word rather than `USED`: nothing here is waiting for a reply, it is
#: that a *name* has to be listened for before the message that would wake it can
#: arrive (`send_message`).  A member holds it for as long as the process serves
#: its maker, and leaving with the process is the offline card being honest.
USED = "used"
WINDOW = "window"
MEMBER = "member"

#: The channel an inbound message arrives on, and the first half of what it is
#: keyed by: `peer_agent:jill`.  The window draws a bubble from the channel alone
#: (`slife2.tui.widgets.channel_prefix`), so the peer has to be *in* it for a
#: person to see who spoke — and what it draws is `PeerAgent(jill)> `, the same
#: word the marker below opens with.
CHANNEL = REMOTE.channel

#: What the marker opens with.  A constant because the marker is *read* as well
#: as written: stored text carries it into a transcript, and the window takes it
#: back off by this string rather than by a second copy of the spelling.
#:
#: **Word-shaped, like every other name this build writes into a row.**  v1
#: spelled it `[A2A:`, and capitals are the convention for the *other* kind of
#: bracket — `[TURN:{…}]`, which says "this is what the process knows about the
#: conversation, not what was said" (`slife2.plugins.wechat.server`'s module docstring,
#: DESIGN.md §1.2).  This one *is* what was said: it goes into the turn's row
#: and comes back out of it verbatim, so v1's capitals told the model the
#: opposite of the truth about it.  What is *not* renamed is anything that names
#: the plugin — the `a2a` channel, its config key, `A2A_TYPES` — because those
#: are this plugin's own name rather than a word in somebody's conversation.
#:
#: **`PeerAgent`, because `Agent` alone is the one word this marker must not
#: say.**  Its `from` field is a peer and never the receiver, for the reason the
#: marker's own docstring gives — v1 measured receivers reading the sender as
#: themselves — and a name that says "an agent did this" reintroduces exactly
#: that ambiguity on the line where the model is told who spoke.  `Peer` is what
#: settles it, and it is the word the window's bubble uses too
#: (`slife2.tui.widgets.channel_prefix`).
MARKER_PREFIX = REMOTE.marker

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
#: is the second half of the channel `channel_for` builds (`peer_agent:orphans`), which
#: the window draws on its own (`PeerAgent(orphans)> `) rather than as somebody
#: speaking, and the writer's log line names it the same way.
ORPHAN_PEER = "orphans"

#: How long the writer waits before offering an orphan notice again, and how many
#: times it will.  **The first attempt is expected to fail**, and not because
#: anything is broken: a notice is handed over at a start whose peers cannot run
#: a turn yet — the hub waits for every plugin, so it is up *after* this one
#: (`slife2.libs.host.launcher.NEEDS`) — and the delay is what lets the peers the turn
#: needs arrive underneath it.  Tens of seconds is what the launcher's own waves
#: cost, so the count is generous against that and still a bound: a notice nobody
#: can receive is not a reason to keep saying it.
ORPHAN_RETRIES = 30

#: What a call that does not say which agent it is for is told.  A shared process
#: cannot guess it: one `slife2-a2a` serves every agent, and a mesh identity is
#: the agent's own name.


def marker(
    net: Net,
    agent_name: str,
    task_id: str | None = None,
    type: str = "task_request",
) -> str:
    """Content prefix for ANY inbound A2A message — one envelope, a type field.

    ``[PeerAgent:{"from": …, "task_id": …, "type": …}] `` — ``from`` names the
    *sending* peer (never the receiver).  ``type`` tells what it is:
    ``task_request`` (an inbound task to answer, via
    ``peer_agent_send_message(message_type="task_response", task_id=…)`` — it may take
    many turns), ``task_response`` (an auto-delivered task result),
    ``cancel_task`` (the peer withdrew that task — its completion bridge died
    with it, so a ``task_response`` for it is refused: answer with a plain
    message or stay silent), ``message`` (a bare conversation), ``broadcast`` (a
    fire-and-forget event, informational).  ``task_id`` rides for tasks, results
    and withdrawals.

    The key is ``from``, not ``agent_name`` — that word in a system prompt is
    the agent's own identity, and v1 measured the confusion it caused: receivers
    misread the sender as themselves.  The marker is machine-facing; the window
    drops it for display (the ``PeerAgent(<peer>)> `` bubble prefix instead).
    """
    # ``from`` — the peer's name — deliberately not ``agent_name``: that word
    # is the agent's self-identity, and reusing it here made receivers misread
    # the sender as themselves.
    payload: dict[str, str] = {"from": agent_name, "type": type}
    if task_id is not None:
        payload["task_id"] = task_id
    return f"{net.marker}{json.dumps(payload, ensure_ascii=False)}] "


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
    `PeerAgent(jill)> ` and never looks at the words.  It stays because it is the
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


def channel_for(net: Net, peer: str) -> str:
    """The channel an inbound message from *peer* arrives on: `<net>:<peer>`."""
    return f"{net.channel}:{peer}"


def peer_phrase(net: Net, peer: str) -> str:
    """How a line about the far end names it: `Peer **jill**`, or `**jill**`.

    **`Peer` is the remote network's word and the subagent network's is nothing**,
    which is why this is a function rather than a hole in a template — the hole
    would be empty.  The remote network needs the word for the reason
    `MARKER_PREFIX` is `PeerAgent`: its `from` must not be read as the receiver.

    On the subagent network the same sentence has two possible readers — a maker
    reading about one of its subagents, and a subagent reading about its maker or
    a sibling — and no relationship word is true for both: "your subagent" is
    false for a subagent reading about its maker, and so is plain `Subagent`.
    What the far end *is* is already on the line, in the word this project uses
    for it: the `[Subagent:…]` envelope, and the `Subagent(<name>)> ` bubble
    the window draws from the channel.
    """
    return f"Peer **{peer}**" if net is REMOTE else f"**{peer}**"


# ── one agent's mesh, and what is waiting to be handed over ──────────────────


@dataclass
class Session:
    """One member's mesh, its connect state, and its delivery queue.

    The queue is the piece §1.2's removal of the drain leaves behind.  A mesh
    callback runs on the MQTT reader's stack, so it must not await a hop to
    another server; and arrival order is the order the model should read things
    in.  One queue with one writer behind it is both, and it is exactly what
    v1's five queues were, minus the harness on the far end.

    **`agent` is the maker and `subagent` the member, which is the client id
    every server in this system keys by** — and which of them a delivery runs a
    turn for is decided by the second: an agent's conversation is run by its
    window, a subagent's by this plugin, because a subagent has none.
    """

    agent: str
    #: Which network this member is on.  On the Session rather than passed to
    #: every function, because everything a member says or is told is in *its*
    #: network's vocabulary — the four tool names, the channel a turn arrived
    #: on, the envelope a message opens with (`Net`).
    net: Net
    mesh: A2AMesh
    #: The other half of this session's client id, and empty for an agent's own.
    #: **It is what the member answers as** (`mesh.agent_name` is `subagent or
    #: agent`), so it is also what a delivery that is not the window's has to
    #: address its turn with (`run_turn`).
    subagent: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Why the last connect attempt failed.  A failed mesh is discarded, so this
    #: is the only place the reason survives to `peer_agent_list` — otherwise it
    #: reports a bare "not connected" for what is really a name collision.
    connect_error: str = ""
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(MAX_QUEUED))
    writer: asyncio.Task | None = None
    #: Why this agent is on the mesh — and the mesh lives exactly as long as this
    #: table has something in it.  `USED` is added by any call that needs the mesh
    #: and is never taken back: a send is followed by a reply the agent is
    #: waiting for, so an agent with a task outstanding *is* on the mesh and
    #: nothing inside this process's life makes that stop being true.  `WINDOW` is
    #: added by `join` for as long as its call is open, which is what makes a
    #: closed window show up as `✗ offline` rather than as a card nobody retires.
    #:
    #: **A count per reason rather than a set of them.**  A set held one entry
    #: for each *kind*, so two callers holding the same kind were one entry and
    #: the first to finish was the last one — see `hold`, which is where that
    #: cost a live window its mesh.
    reasons: dict[str, int] = field(default_factory=dict)

    def hold(self, reason: str) -> None:
        """Add one reason for this agent to be on the mesh: one more *holder*.

        **Two holders of one reason are two reasons to stay.**  `reasons` was a
        set, so a second `join` for an agent was a no-op and the *first* of the
        two to end tore the mesh down while the other was still holding it.  The
        survivor is left parked in `_hold_open`, which only pings, and `join`
        never re-checks the connection — so the agent stayed falsely `offline` on
        the broker for the life of that window and every task a peer sent to it
        was lost silently.  Two windows showing one agent get there directly, and
        one window gets there alone: `slife2.tui.app._hold_mesh` retries a dropped
        stream after `pacing.reconnect`, while the stale call is not cancelled
        until its next `report_progress` fails — up to a `deliver.keepalive`
        later, as `_hold_open`'s own docstring says.  The class's contract is
        that the mesh lives exactly as long as this has something in it, and a
        count is what makes a second holder something.
        """
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def release(self, reason: str) -> bool:
        """Give up one holder of *reason*.  True when it was the last one."""
        held = self.reasons.get(reason, 0)
        if held <= 1:
            self.reasons.pop(reason, None)
        else:
            self.reasons[reason] = held - 1
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
        (`slife2.libs.harness.handover.note_to_window`), so it is never a turn, never stored,
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
    `report_progress` is then a silent no-op (`slife2.libs.turn.events`): such a window is
    still held and still leaves when its stream is cancelled, but nothing here
    can hasten the noticing, and that is a fact about the caller rather than
    something to paper over.
    """
    while True:
        await asyncio.sleep(_T.timeouts.deliver.keepalive)
        await ctx.report_progress(0, None, None)


async def run_turn(
    config: Config,
    agent: str,
    subagent: str,
    text: str,
    channel: str,
    *,
    client: Client | None = None,
) -> str:
    """Run one turn in a subagent's conversation, and answer with what it said.

    **This is the road a subagent has instead of a window, and it is the second
    road on purpose rather than a fallback.**  `slife2.libs.harness.handover` argues the
    point for an *agent*: the window is what runs it, and a delivery that finds
    no window is a hard failure — because work advancing where no person can see
    it, answering a peer that believes it was heard, is the thing that argument
    refuses.  A subagent is defined as not having a window and never getting one,
    so there is nothing to find; and its answer does not go to a person at all.
    It goes to its maker, which is a model, and what the maker is handed is a
    message in *its own* conversation — the member's turn is in the member's
    store, where its maker can read it.  DESIGN.md §1.10 states that cost and
    accepts it.

    **The turn is run by the agent server, so it is an ordinary turn**: the same
    loop, the same tools, the same store, addressed by the client id the message
    named — `send_message(agent=<maker>, subagent=<receiver>)`, which is the
    shape an agent task already has.  `watched` is left False for the reason
    `Pending.watched` gives: nobody is watching, so a tool call the model puts up
    for approval is granted rather than waiting for an answer nobody will send.

    **No progress stream and no deadline.**  A caller with no handler gets none —
    `report_progress` is a silent no-op without a progress token — and this is a
    caller with nothing to draw.  `timeout=None` on the call because the turn's
    length is the answer's own business, and `timeout=None` on the *connection*
    because a deadline there is one on every request that crosses it, which for
    this client is the whole turn (the agent server's own `hub()` says the same
    thing about the same trap).

    **What comes back is not relayed anywhere, and that is the design.**  A
    subagent's answer reaches its maker as a tool call of its own — a
    `task_response` — so the return value here is a log line and a test's
    evidence; nothing else reads it (`_drain`).  Relaying it as well would be the
    second road for one answer that §1.10 refuses: the maker would hear the same
    thing twice, once as a fact and once as a reply.

    Raises:
        DeliveryFailed: The agent server could not be reached, so the message did
            not reach the conversation it was addressed to.  Its own type, so
            `_drain` says that in its own words and does not carry the message on
            anywhere — the same answer the window road gives for the same
            situation.
    """
    opened = client is None
    try:
        peer = client or await open_server(
            config.server("agent").url,
            name=AGENT_SERVER_NAME,
            fallback_tool="send_message",
            timeout=None,
        )
    except Exception as exc:  # noqa: BLE001 — a peer that is not there is a delivery that did not happen, and every way it can be missing means that
        raise DeliveryFailed(
            describe((agent, subagent)),
            f"the agent server could not be reached: {exc}",
        ) from exc
    try:
        answer = await peer.call_tool(
            "send_message",
            {
                "agent": agent,
                "subagent": subagent,
                "prompt": text,
                "channel": channel,
            },
            timeout=None,
        )
    finally:
        if opened:
            # `shield`, for `slife2.libs.harness.handover._peer`'s reason: a cancellation
            # arriving while the close is awaiting would pass straight through
            # it and leave the connection open — `CancelledError` is not an
            # `Exception` — and the cancellation still propagates once the close
            # has finished, so the caller's turn is cancelled as it expected.
            await asyncio.shield(close_server(peer))
    payload = tool_payload(answer)
    said = payload.get("text")
    if not isinstance(said, str):
        # **A turn nobody can read is said rather than counted as nothing.**
        # The answer is not relayed (see above), but it is the evidence that the
        # turn happened at all — so an unreadable shape is a build mismatch worth
        # a warning, and it must not read as a member that answered with silence.
        logger.warning(
            "a2a_turn_unreadable agent=%s — the turn's answer was not a shape "
            "this build can read: %s",
            describe((agent, subagent)),
            payload,
        )
        return ""
    return said


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
        #
        # **Scrubbed first, and the channel is the reason.**  `source` comes off
        # the MQTT wire with nothing having validated it, and it becomes not only
        # the marker's `from` but the *channel* the message is handed over on
        # (`channel_for`), which the window draws verbatim as the bubble prefix
        # (`slife2.tui.widgets.channel_prefix`), which is stored in the turn row
        # and which the log lines below name.  A peer calling itself `jill` plus a
        # newline and `✗ offline`, or carrying an ESC, forges lines in every
        # window showing this agent's conversation — the shape `safe_name` exists
        # to refuse, and every callback here has to pass its peer through it or
        # the one that does not is the hole.
        source = safe_name(source)
        marker_id = (task_id or None) if kind == "task" else None
        marker_type = "message" if kind != "task" else "task_request"
        session.post(
            f"{marker(session.net, source, marker_id, type=marker_type)}{content}",
            source,
            f"an inbound {marker_type} from {source}",
        )

    def on_task_completion(
        corr_id: str,
        result: str,
        outcome: str,
        peer: str,
        kind: str = "task",
    ) -> None:
        """Say what became of a send, in the send's own words.

        `outcome` is one value — `mesh.COMPLETED`, `CANCELLED`, `FAILED` or
        `UNREACHABLE` — rather than the pair of flags this took before: a
        `cancelled` boolean and a `failed` one, of which only the second could
        mean two different things, and of which the first could not express the
        third ending at all.  A peer that *answered* with a failure was rendered
        as "completed" — the confident negative DESIGN.md §10.1 forbids — because
        the renderer had no value that said so.
        """
        # Nothing to say about an empty *success*.  A withdrawal, a failure and
        # an unreachable peer are worth a line even with no words in them: the
        # conversation is waiting for an answer that will not come.
        if not result and outcome == mesh.COMPLETED:
            return
        if outcome == mesh.UNREACHABLE:
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
                    f"{marker(session.net, peer, type='message')}"
                    f"**{peer}** was never reached, so nothing will answer your "
                    f"message:\n\n{result}"
                )
            else:
                text = (
                    f"{marker(session.net, peer, corr_id or None, type='task_response')}"
                    f"**{peer}** was never reached, so no result will arrive for "
                    f"this task (ID: `{corr_id}`):\n\n{result}"
                )
        elif kind == "message":
            # A MESSAGE conversation was answered: a bare message reply, no
            # task involved.
            text = (
                f"{marker(session.net, peer, type='message')}"
                f"{peer_phrase(session.net, peer)} replied to your message:"
                f"\n\n{result}"
            )
        else:
            # The peer answered, and this is what it said: `completed`,
            # `cancelled` or `failed`, in the peer's own word.  All three are a
            # verdict it had, which is what separates them from `UNREACHABLE`.
            text = (
                f"{marker(session.net, peer, corr_id or None, type='task_response')}"
                f"{peer_phrase(session.net, peer)} {outcome} async task "
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
        # `peer_agent_list`' answer.  Handing a transition to the conversation
        # instead spends a whole turn and a context on a fact nobody has to act
        # on, which is what v1 avoided by never putting presence in the inbox.
        #
        # **And on the local network nothing is drawn at all.**  The cards here
        # are the maker's own subagents, so the line this would draw in the
        # maker's window is `⚡ the member you woke is now awake` — the member
        # waking up *because* something was sent, which the sender knows.  What a
        # subagent's coming and going should look like on a terminal is not
        # designed yet, so this draws nothing rather than inventing it: the card
        # is still cached, which is what the roster reads
        # (`peer_agent_list`), and `_drain` drops the note a member's own session
        # would otherwise hand to a window it does not have.
        if session.net is LOCAL:
            return
        text = format_presence_line(card, event)
        if text is None:
            return
        # **Scrubbed once and used for both**, rather than scrubbed for the peer
        # and left raw in the sentence beside it.  `what` is the delivery's own
        # name for itself and it reaches log lines, which is the third place the
        # module's `safe_name` docstring names — so a card whose name carries a
        # newline was still forging a line there while the peer beside it was
        # clean.  `text` is safe on its own: `format_presence_line` scrubs the
        # name it renders, which is why this is the log and not the window.
        name = safe_name(card.agent_name)
        session.post(
            text,
            name,
            f"{name} is {event}",
            note=True,
        )

    def on_peer_cancel(task_id: str, peer: str) -> None:
        """A peer withdrew an inbound task — hand it over like any other message.

        The withdrawal is NOT a control signal: it reaches the model the same
        way every other peer event does, and the model — the only party that
        knows whether it is still working on that task — decides what to do with
        it.  The one constraint rides the marker: the completion bridge died with
        the task, so a `task_response` for it is refused.

        **The peer is scrubbed here too**, for the reason `on_inbound_task`
        gives: a withdrawal is handed over like any other message, so its
        `peer` is the channel this line arrives on and the name in the log —
        both of them drawn or written verbatim.
        """
        peer = safe_name(peer)
        session.post(
            f"{marker(session.net, peer, task_id, type='cancel_task')}"
            f"The sender withdrew this task — stop working on it.",
            peer,
            f"a withdrawal from {peer}",
        )

    def on_broadcast_event(sender: str, text: str) -> None:
        # **And the third one.**  A broadcast carries the same untrusted sender
        # as everything else on this wire, and it takes the same road into a
        # channel, a stored turn and a log line.
        sender = safe_name(sender)
        session.post(
            f"{marker(session.net, sender, type='broadcast')}{text}",
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
    agent_client: Client | None = None,
) -> None:
    """Hand over what the mesh delivers, one message at a time, in order.

    **Two kinds of thing take this one queue, and that is the point of it.**  A
    message is a turn somewhere — in the conversation the window is running
    (`slife2.libs.harness.handover.hand_over`: the window, and nothing else) or, when it is
    addressed to a subagent, in *that* conversation, run here (`run_turn`) — and
    a note is drawn in a window and nowhere else.  Sharing the queue is what
    keeps them in arrival order with each other — a peer's arrival line lands
    between the two turns it happened between — and one writer is what keeps a
    mesh callback off the MQTT reader's stack.

    **A delivery that fails is an error, and the message is not carried on
    anywhere.**  There is no second road for it (`slife2.libs.harness.handover`), so a peer's
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
    This task is one per member for the life of the process and nothing watches
    it until shutdown, so a raise escaping the loop is silent and permanent:
    the mesh still reports connected, and every later arrival queues behind a
    writer that no longer exists.  So each message is guarded on its own and a
    failure is logged and dropped, and the loop goes on to the next.
    """
    while True:
        text, peer, what, retries, note = await session.queue.get()
        try:
            if note:
                if session.subagent:
                    # **A note has no second road, and a subagent has no window.**
                    # Presence is what these lines are — a peer arriving, a peer
                    # going — and the roster a subagent can ask for is
                    # `subagent_list`, so drawing one for it would be a
                    # turn spent on a fact nobody here has to act on; and it
                    # cannot be drawn in its maker's window either, which is
                    # where it would read as the harness telling somebody their
                    # own subagent is online.  Dropped, and said at debug.
                    logger.debug(
                        "a2a_note_no_window agent=%s line=%s",
                        describe((session.agent, session.subagent)),
                        what,
                    )
                    continue
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
                if session.subagent:
                    said = await run_turn(
                        config,
                        session.agent,
                        session.subagent,
                        text,
                        channel_for(session.net, peer),
                        client=agent_client,
                    )
                    # The length and not the words: the member's answer is its
                    # own, and what it says to its maker is the reply it sends
                    # (`run_turn`).  This line is the evidence that the turn ran.
                    logger.info(
                        "a2a_turn_answered agent=%s peer=%s chars=%d",
                        describe((session.agent, session.subagent)),
                        peer,
                        len(said),
                    )
                else:
                    await hand_over(
                        config,
                        session.agent,
                        text,
                        channel_for(session.net, peer),
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
                    describe((session.agent, session.subagent)),
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
            #
            # **A member's turn that failed reaches here too**, and that is the
            # honest answer for it: the message was received and its turn did not
            # happen (a provider that answered 500, a store that refused), so the
            # peer is owed an answer this process no longer holds a turn for, and
            # the line names the conversation it was for (`describe`).
            logger.exception(
                "a2a_delivery_failed agent=%s peer=%s — that message dropped",
                describe((session.agent, session.subagent)),
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
    names the one reply that still works — **and it is named in this network's
    words**: one module serves both, and the tool a maker replies with is
    `peer_agent_send_message` while the one a subagent replies with is
    `subagent_send_message`.  The notice used to spell the remote one, so half
    its readers were told to call a tool they do not have.
    """
    orphans = session.mesh.stale_inbound()
    if not orphans:
        return
    session.post(
        render(ORPHAN_TEMPLATE, tasks=orphans, send=session.net.tool(SEND_MESSAGE)),
        ORPHAN_PEER,
        f"{len(orphans)} orphaned inbound task(s)",
        retries=ORPHAN_RETRIES,
    )


def build_server(
    config: Config,
    *,
    net: Net = REMOTE,
    window_client: Client | None = None,
    agent_client: Client | None = None,
    source: Callable[[], Config] | None = None,
    connecting: bool = True,
) -> FastMCP:
    """Build the A2A server.

    **Three seams, and every one is a peer**: the window and the agent server,
    which are the two places a message can be handed to (`slife2.libs.harness.handover` for
    the first, `run_turn` for the second), and the config source.  A test wires
    recorder versions of the peers over the in-memory transport and drives the
    mesh's callbacks directly; there is no seam for the mesh itself, because the
    mesh is the thing under test and a broker is what a test would need to
    replace — see `tests/test_a2a.py`, which tests the driver without one.

    `connecting=False` is for a build that must not dial a broker: the lifespan
    resumes nothing.  The section is still read, so the tools answer with what
    the config says rather than with a connection nobody asked for.
    """
    live: Callable[[], Config] = source or load_cached
    #: One session per **(home, agent, subagent)** — see `session_of` for why
    #: neither the name alone nor the pair is the identity this process serves.
    sessions: dict[tuple[str, str, str], Session] = {}

    def mesh_settings() -> A2ASettings:
        """This network's settings, as the config has them *now*.

        Read per use rather than once at build, which is `current`'s rule for
        the model config and for the same reason: the section is a file an
        operator edits, and a mesh that kept the address it started with would
        be the one thing in this process that could not be corrected.
        """
        return net_settings(live(), net)

    def mesh_for(maker: str, subagent: str = "") -> A2AMesh:
        """A member's mesh, on this network and in the mesh it belongs to.

        `Net.namespace` is the whole of the difference between the two networks
        here: the remote one is the file's `{org}/{unit}`, which is a room
        somebody shares, and a subagent network is **one mesh per agent** — the
        unit is the agent's name, so two agents that each named a subagent
        `researcher` get two meshes rather than one MQTT identity a broker
        settles by kicking the older one offline.

        **The mesh is the maker's and the identity is the member's**, which is
        why both are arguments here and neither is derivable from the other: a
        subagent meets its peers in its maker's mesh and is addressed by its own
        name inside it — and the maker is a peer of that same mesh, which is what
        lets a subagent answer it by name.

        **Nothing dials from here.**  A mesh is built rather than connected
        (`session_of`), and the mesh's own `connect` is what joins — so building
        one for a name that never joins is a ledger file and a card, not a
        connection.
        """
        found = mesh_settings()
        org, unit = net.namespace(found, maker)
        return A2AMesh(
            replace(found, org=org, unit=unit),
            maker,
            net.ledger,
            subagent=subagent,
        )

    async def writer_in(root: Path, session: Session) -> None:
        """Run *session*'s writer **in its own home**, for the life of this process.

        **A loop outlives the call that started it**, so by the time it resolves
        a path the ambient home is the daemon's own — and everything this writer
        does resolves one: `hand_over` stamps the caller's home on the call it
        makes to the window (`client_meta`), and the window it reaches is the one
        showing *that* home's conversation.  Leaving the home to the ambient one
        is what handed another home's message to this one's window, which the tui
        refuses with `a2a_delivery_unreachable` while the peer is told nothing.
        The shape is `slife2.plugins.wechat.server.Wechat._spawn_in_home`'s, for the
        same reason.  It is the same fact for a subagent's turn: `run_turn` opens
        its own client inside this, so the agent server writes the turn into the
        store of the home the member is in.
        """
        with in_home(root):
            await _drain(session, live(), window_client, agent_client)

    def session_of(agent: str, subagent: str = "", root: Path | None = None) -> Session:
        """This member's mesh, built on first use and kept.

        **Keyed by the home and by the whole client id, because neither the name
        nor the pair is an identity here.**  One process serves every home, two
        homes may each be running an agent called `jack`, and a table keyed by
        the name alone answers the second home with the first one's mesh — the
        arrangement `slife2.libs.plugin.audience.client_key` and
        `slife2.plugins.tui.server.Windows` exist to prevent, and here it cost the mesh
        itself: the session's writer ran in whichever home the *first* caller was
        in, so every message found for the second home's agent was handed to the
        first home's conversation, and the mesh's own ledger was opened there — a
        file that either does not exist or says something else
        (`announce_ledgers`).  The second half is the same argument one level in:
        on the local network two agents may each have a `researcher`, and a
        table keyed by the member's name alone would answer one with the other's
        mesh — one MQTT identity for two agents' subagents.

        *root* is the home the caller means, and it is what the mesh and the
        writer resolve their files in.  A caller inside a served call passes
        nothing and gets the ambient home, which `slife2.libs.plugin.protocol.serve`
        has already made the caller's; the startup walk passes the home each
        ledger was *found* in.

        Built rather than connected, and the difference is the whole of what
        enrollment means here: building writes the member's ledger
        (`InboundStore.open`), which is the record of the *inbound tasks a
        process died holding* — a fact about work already received, not a claim
        about who is here now.  What connects is `join` (a window is showing the
        agent), the first `a2a_*` call that needs the mesh, or a message to a
        subagent, which gives it its place before publishing (`place_for`).
        """
        where = root or home()
        found = sessions.get((str(where), agent, subagent))
        if found is None:
            # Both of these resolve a home's own files — the mesh opens the
            # member's ledger at `agents/<agent>/<ledger>`, or one level in for a
            # subagent — so the home goes around the build rather than being read
            # off whatever directory the daemon happens to be started in.
            with in_home(where):
                found = Session(
                    agent=agent,
                    net=net,
                    mesh=mesh_for(agent, subagent),
                    subagent=subagent,
                )
            _wire(found)
            found.writer = asyncio.create_task(writer_in(where, found))
            sessions[(str(where), agent, subagent)] = found
        return found

    async def drop_session(session: Session, key: tuple[str, str, str]) -> None:
        """Give up one session: **its writer, its connections, and its entry.**

        Three things, and they are one act: a session is a task and a pair of
        connections that only mean something together.  The writer goes first,
        because it is what delivers what the mesh hands it — nothing new is taken
        on — and it is cancelled rather than asked to finish, which is what a
        forced cleanup means for a session that happens to be mid-turn.  Then the
        connections are retired: `disconnect` publishes the offline card, which is
        how a broker stops advertising a name nobody holds.  The entry is dropped
        last, so a caller arriving in between finds something coherent rather than
        half of a session.

        **Never its own caller.**  A session's writer is `_drain`, and a teardown
        that cancelled the frame it was called from would never return
        (`mesh.A2AMesh.disconnect` states the same rule for the same reason).
        Nothing does that today — the callers are `forget` and the lifespan — and
        the guard is one comparison.

        **A teardown that could not retire the name is reported, not swallowed**
        (DESIGN.md §1.8): the offline card is how a peer is told this member left,
        and a failure here leaves it advertised with nobody to retire it.
        """
        sessions.pop(key, None)
        writer, session.writer = session.writer, None
        if writer is not None and writer is not asyncio.current_task():
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer
        try:
            await session.mesh.disconnect()
        except Exception as e:  # noqa: BLE001 - a teardown that carries a protocol fact is reported, not swallowed: the offline card is how a peer is told this member left, and a failure here leaves it advertised online with nobody to retire it (DESIGN.md §1.8)
            logger.error(
                "a2a %s: the mesh would not leave the broker, so it may stay "
                "advertised: %s",
                describe((session.agent, session.subagent)),
                e,
            )

    async def connected(agent: str, reason: str, subagent: str = "") -> Session:
        """This member's mesh, connected — or a refusal naming why it is not.

        Double-checked under the session's lock, v1's shape: two calls arriving
        together must not both dial the broker, and a mesh whose connect failed
        is discarded rather than half-kept, so a retry starts from nothing.

        **The fast path is inside that lock too, and the lock is the same one the
        release takes** (`join`'s `finally`), because the two are one decision
        read from either end: a caller may hold this mesh only if nothing is
        taking it down.  Outside the lock the already-connected check was a read
        of state a teardown was three awaits from clearing — `disconnect` only
        clears `_connected` once the cancelled outbound task has reached its
        client context — so a tool call could be handed a mesh on its way out and
        hold `USED` on it for ever: its publish landed on a client whose reply
        subscription was being dropped, and the conversation then waited for a
        result no topic was listening for.  Held under this lock, either the hold
        comes first and the release finds the mesh still wanted, or the teardown
        comes first and this sees a mesh that is genuinely down and re-dials it.

        **`reason` is the caller's, and it is what the mesh's lifetime is made
        of** (`Session.reasons`).  A caller that is waiting for something back —
        every tool here but `join` — holds `USED`, for the rest of this process's
        life: what follows a send is a reply the agent is waiting for, and an
        agent with a task outstanding is on the mesh in the only sense that
        matters to the peer answering it.  A window passes `WINDOW`, which it
        gives back when its call ends.  Which one it is has to be said by the
        caller: a `join` that inherited a tool's reason would keep the agent
        online after the window it was joining for had gone.

        **And a subagent's is `MEMBER`, which the same caller takes on somebody
        else's behalf** — see `place_for`, which is the only caller that passes a
        `subagent` at all.
        """
        settings = mesh_settings()
        if not settings.configured:
            raise ToolError(switch_off(net))
        session = session_of(agent, subagent)
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
                        describe((agent, subagent)),
                        teardown,
                    )
                raise ToolError(
                    f"could not join the mesh as {session.mesh.agent_name!r} on "
                    f"{settings.address}: {e}"
                ) from e
            session.connect_error = ""
            session.hold(reason)
            logger.info("a2a_mesh_connected agent=%s", describe((agent, subagent)))
        return session

    def speaker(ctx: Any) -> tuple[str, str]:
        """The `(agent, subagent)` a call is on behalf of — its client id.

        **Both halves, because on the local network the second one is who is
        speaking.**  A subagent's tool call carries the pair it is run under, and
        a message from it has to leave as *it* rather than as its maker: the
        maker is a peer it addresses (`subagent_send_message(agent="slife2")`),
        and a reply published under the maker's own name would be the maker
        talking to itself — and would find no completion bridge, because the task
        it is answering is on *its* responder.

        Empty for a subagent on the remote network, and that is not a gap: a
        remote peer sees an agent name, a subagent is not an instance and never
        claimed one (sdd §3.5), so its maker is the name anybody out there knows.
        """
        found = request_client(ctx)
        if found is None or not found[0]:
            raise ToolError(
                unaddressed(
                    "mesh it could be sent from",
                    "",
                    why="one process serves every agent, and the agent name is "
                    "the identity a peer sees",
                )
            )
        agent, subagent = found
        return agent, (subagent if net is LOCAL else "")

    def membership(agent: str) -> set[str]:
        """The names of this agent's subagents, off the filesystem.

        `subagent_dirs` is the whole of what "a member" means
        (`slife2.libs.host.paths`): a directory under the agent's own carrying a
        `subagent.yaml`, which is `schedule_dbs`' principle one level in.  Read
        per use rather than cached, so a subagent made while everything is up is
        a member by the next look — which is the look the send path takes anyway,
        and the reason nothing has to keep a list of them.
        """
        return {directory.name for directory in subagent_dirs(agent)}

    async def place_for(agent: str, peer: str) -> bool:
        """Give *peer* its place on this network, when it is one of *agent*'s.

        **The one thing a send has to do that publishing does not**, and a
        subagent is the only member that needs it: a message published to a name
        whose responder is not subscribed goes to nobody and reports nothing —
        the requester ladder then runs out and the sender is told the peer was
        never reached, which is what a maker saw for every `subagent_send_message`
        before this existed.  An agent's own window holds its `join`, and a peer
        on the remote network is somebody else's process, so on those there is
        nothing to ensure; here nothing else is holding the name at all.

        True when *peer* names a member of this agent's.  False is an ordinary
        answer — the name may be the agent itself, which its own call has already
        connected, or a peer on the remote network, which this process does not
        place — and it is not an error for a send either way: what a send to a
        name nobody holds does is the ladder's business.
        """
        if net is not LOCAL or not peer or peer not in membership(agent):
            return False
        await connected(agent, MEMBER, peer)
        return True

    async def place_for_all(agent: str) -> None:
        """Give every member of *agent*'s its place — what a broadcast needs.

        One rule rather than two: a broadcast is delivered to whoever is
        subscribed, so publishing one to a maker's own network without this
        reaches only the subagents some earlier message happened to wake.  The
        roster is the same look (`membership`), taken once.
        """
        if net is not LOCAL:
            return
        for peer in sorted(membership(agent)):
            await connected(agent, MEMBER, peer)

    async def announce_ledgers(config_now: Config) -> None:
        """Tell the model about what a previous process died holding.

        **The ledger is the orphan record, and that is all it is.**  It used to
        be read here as an enrollment list too — a start joined the mesh as every
        agent that had ever made an `a2a_*` call — and that made an agent's
        presence depend on having spoken once and then last forever: a card for a
        conversation nobody was having, which is the thing `slife2.libs.host.paths` says
        the ledger must not become.  What puts an agent on the mesh is a window
        showing it (`join`); what this does is the other half of the file's job,
        which no window can do for it — a peer is still owed an answer, and the
        only party who can give one is the model, at a start that is *not* the
        one that received the task.

        The broker is asked about first, and it is the one thing not tolerated:
        a machine whose config says `peer_agent:` has said it wants the mesh, so an
        absent broker is a missing dependency rather than a preference.
        """
        # **The broker, before any of it.**  One is started if it can be, and one
        # that will not serve raises out of the lifespan — which is this plugin
        # failing to start, which is `slife2` refusing to start rather than
        # handing the model five tools attached to nothing
        # (`slife2/plugins/a2a/broker.py`).
        logger.info("a2a_broker %s", ensure_broker(config_now.peer_agent))

        for ledger in a2a_inbound_paths(net.ledger):
            store = InboundStore.at(ledger.path)
            if store is None:
                logger.warning("a2a_ledger_without_an_owner path=%s", ledger.path)
                continue
            # Queued rather than sent from here: this start's peers cannot run a
            # turn yet — a turn's first step is the hub, and the hub waits for
            # every plugin, so it is up *after* this one — see `announce_orphans`,
            # which is also where the retries come from.  The session is built,
            # not connected: the notice needs the agent's queue and its writer,
            # and being on the mesh is not what a notice is for.
            #
            # **In the home the ledger was found in**, which is the half the walk
            # is for: the notice is about work *that* home's peer is owed, and
            # the session it goes to opens its mesh on that home's file and hands
            # over in that home (`session_of`).
            announce_orphans(session_of(store.agent, root=ledger.home))

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
            if connecting and mesh_settings().configured:
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
    mcp: FastMCP = house_server(net.server_name, lifespan=lifespan)

    @mcp.tool
    async def join(agent: str, ctx: Context) -> None:
        """Put *agent* on the mesh, and keep it there for as long as this call.

        **Hold this call open for as long as a window is showing that agent.**
        It does not return: the mesh joins on entry and its cancellation is the
        departure, so a window that quits, is killed, or loses its stream takes
        its agent off the mesh with nothing to send — the offline card and the
        last will are then both honest about when it left.  That is the contract
        `slife2.plugins.tui.server`'s `attach` has on the same window's inbox, and it is
        the same lifetime for the same reason: what a peer is told about an agent
        should be decided by whether anyone is there, not by which process
        happened to be running.

        **This is the enrollment.**  The ledger at `agents/<agent>/inbound.yaml` is
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
            # **Under the lock `connected` takes**, so the departure and a call
            # arriving at the same moment cannot both be right about this mesh:
            # either that call holds its reason first and `release` below finds
            # the agent still wanted, or this takes the mesh down first and the
            # call finds it down and dials it again.  Awaiting the teardown
            # outside the lock made the second answer a lie — `_connected` is
            # cleared only at the end of `disconnect`, so a caller arriving
            # mid-teardown was handed a mesh whose reply topic was already being
            # unsubscribed, and held `USED` on it for ever.
            #
            # Only when this was the last reason: an agent that has sent
            # something is waiting for a reply, and a window closing is not a
            # reason to stop listening for it.  The reason itself was taken by
            # `connected`, which is where every caller says which one it is.
            async with session.lock:
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
    # the conversation later (a [PeerAgent:…] marker of type 'task_response');
    # 'task_response' completes an inbound task; 'message' is a bare
    # conversation.  No waiting, no timeout parameter.

    @mcp.tool(name=FORGET)
    async def forget(agent: str, subagent: str = "") -> dict[str, Any]:
        """Give up this member's place on the mesh, and forget its session.

        **A member can stop existing while a process is serving it**, and this is
        how that process is told.  `subagent_remove` runs in the agent server: it
        stops the loop and deletes the directory, and neither of those reaches an
        MQTT connection this process is holding for a name that is gone — a
        responder still subscribed to a dead subagent's topic, answering whoever
        sends to it, keeping its card on the broker.

        **The counterpart of `join`, and not of a delivery.**  Nobody is owed
        anything by this call and it holds nothing open: it answers whether there
        was a session to forget, and a name that is used again afterwards gets a
        session built from scratch (`place_for`), which is the same road a member
        that was never woken takes.  A machine with nothing to forget — a member
        that never took its place, or a `subagent` this process never built —
        answers `forgotten: false` rather than refusing anything.

        **Nothing is asked of the member and nothing waits for it.**  Whether its
        conversation is mid-turn or idle, the removal it is part of has already
        been decided by the agent that owns it: the turn in flight dies with the
        session (`drop_session`), which is what a *forced* cleanup means and why
        this is not a message to the member.

        Args:
            agent: Whose member it is — the client id's first half.
            subagent: The member's own name.  Empty is the agent itself, which
                only the remote network has a session for.
        """
        found = sessions.get((str(home()), agent, subagent))
        if found is None:
            return {"agent": agent, "subagent": subagent, "forgotten": False}
        await drop_session(found, (str(home()), agent, subagent))
        logger.info("a2a_member_forgotten agent=%s", describe((agent, subagent)))
        return {"agent": agent, "subagent": subagent, "forgotten": True}

    @mcp.tool(
        name=net.tool(SEND_MESSAGE),
        description=describe_tool(net, SEND_MESSAGE),
        meta=FOR_THE_MODEL,
    )
    async def peer_agent_send_message(
        ctx: Context,
        agent: str,
        message: str,
        message_type: str = "task_request",
        task_id: str = "",
    ) -> dict[str, Any]:
        """Send a message to another agent on the A2A mesh.

        `message_type` decides what it is. 'task_request' (the default) asks for
        work and returns an id straight away — the peer answers later, and its
        answer arrives on its own as a `[PeerAgent:…]` of type 'task_response'. Never
        wait for it and never poll: there is nothing to poll. 'task_response'
        completes a task somebody sent you, and needs the `task_id` from that
        task's `[PeerAgent:…]` marker. 'message' is a plain conversation — no task, no
        completion, and the reply arrives as a message.

        `agent` is a peer's name, which `peer_agent_list` gives you. A peer may
        be another agent on this machine or one somewhere else; the mesh does not
        tell you which, and nothing you send depends on it.

        **The receiver's place is made here when the network is the local one.**
        Everything else that is on a network is on it because somebody is there —
        a window holding a `join`, a peer's own process — and a subagent is the
        one member nothing speaks for, so a send to it is also the thing that
        gives it a place to receive in (`place_for`).  Without that the message
        was published to a name nobody was subscribed to: the requester ladder
        ran out and the sender was told the subagent had never been reached, for
        every message, for ever.
        """
        if message_type not in ("message", "task_request", "task_response"):
            raise ToolError(
                f"message_type must be 'message', 'task_request' or "
                f"'task_response', got {message_type!r}."
            )
        sender, mine = speaker(ctx)
        # What the arguments say is settled before the mesh is dialled: a call
        # that cannot work must not be the reason a broker is contacted, and the
        # refusal reads better than a connect failure about a typo.
        if message_type == "task_response" and not task_id:
            raise ToolError(
                "task_id is required for message_type='task_response' — it "
                "is the [PeerAgent:…] marker's task_id."
            )
        session = await connected(sender, USED, mine)
        if message_type == "task_response":
            answer = session.mesh.complete_task(task_id, message, False)
            if answer != "ok":
                raise ToolError(answer.removeprefix("Error: "))
            return {"peer": agent, "task_id": task_id, "completed": True}
        # Before the publish, and only for a message somebody has to receive in.
        # A `task_response` above needs nothing placed: what it completes is a
        # bridge on *this* responder, which is already up.
        await place_for(sender, agent)
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

    @mcp.tool(
        name=net.tool(CANCEL_TASK),
        description=describe_tool(net, CANCEL_TASK),
        meta=FOR_THE_MODEL,
    )
    async def peer_agent_cancel_task(
        ctx: Context, agent: str, task_id: str
    ) -> dict[str, Any]:
        """Withdraw a task you sent to another agent.

        `status` is the answer: 'cancelled' when the withdrawal went out,
        'completed'/'failed'/'cancelled' when the task had already ended (a
        finished result is never lost by asking), or 'not_found' for an id this
        process never sent.
        """
        sender, mine = speaker(ctx)
        session = await connected(sender, USED, mine)
        # As in `send_message`: a withdrawal published to a member with nowhere
        # to receive it goes to nobody, and the peer goes on working on a task
        # somebody believes they took back.
        await place_for(sender, agent)
        status = await session.mesh.cancel_task(agent, task_id)
        return {"peer": agent, "task_id": task_id, "status": status}

    @mcp.tool(
        name=net.tool(LIST),
        description=describe_tool(net, LIST),
        meta=FOR_THE_MODEL,
    )
    async def peer_agent_list(ctx: Context) -> dict[str, Any]:
        """List the agents on the A2A mesh — you first, then each peer by name.

        Every card is `{"agent_name": …, "status": "online"|"offline"}`, and
        `status` on the first one is your own mesh state. Offline peers are ones
        whose card is still retained on the broker but which have said they are
        leaving, so they are listed rather than hidden.

        **On the local network the roster is the maker's subagents, awake or
        not**, and that is the difference the vocabulary turns on: a member with
        no card is not a peer that left, it is one nothing has woken yet, and a
        message to it wakes it (`place_for`) — so it is listed rather than
        waiting for a first send to appear.  The names come from the same look
        the send path takes (`membership`), and the status from the mesh: a
        member that has taken its place is `online` on the wire.

        `error` says why the list is short when it is: the last failure to join,
        or another agent holding your name — the two states a bare "offline"
        cannot tell apart.

        `org` and `unit` are the mesh this is the roster *of* (`Net.namespace`),
        and on a subagent network they are what the mesh is: the org is this
        network's own word and the unit is the agent's name — so the same two
        fields say which network and whose mesh, and a model reading them is
        told both without the file being consulted.
        """
        agent, mine = speaker(ctx)
        settings = mesh_settings()
        if not settings.configured:
            return {
                "agent_name": member_name(agent, mine),
                "broker": settings.address,
                "agents": [],
                "error": switch_off(net),
            }
        session = session_of(agent, mine)
        mesh = session.mesh
        own, *peers = mesh.list_agents()
        found = {safe_name(card.agent_name): safe_name(card.status) for card in peers}
        if net is LOCAL:
            # `offline` is the card vocabulary's word and the honest one here:
            # nothing is listening for this member at this moment.  It says
            # nothing about whether a message would reach it — that is the one
            # thing a send does that this read does not.
            #
            # **The caller is not added to its own roster**, and it is the one
            # name that can be: the mesh never caches its own card as a peer
            # (`_handle_discovery` skips our own topic), so a *member* asking
            # this question — which has itself in `membership` like any other
            # member — would otherwise be listed twice, once as `you` and once as
            # a member nothing has woken.
            for name in membership(agent):
                if name != mesh.agent_name:
                    found.setdefault(safe_name(name), "offline")
        cards = [
            {"agent_name": safe_name(own.agent_name), "status": safe_name(own.status)}
        ] + [
            {"agent_name": name, "status": status}
            for name, status in sorted(found.items())
        ]
        error = mesh.collision or session.connect_error
        if not mesh.is_connected and not error:
            error = f"not connected to the broker at {settings.address}"
        org, unit = mesh.namespace
        return {
            "agent_name": safe_name(own.agent_name),
            "status": mesh.status,
            "broker": settings.address,
            "org": org,
            "unit": unit,
            "agents": cards,
            "error": error,
        }

    @mcp.tool(
        name=net.tool(TASK_LIST),
        description=describe_tool(net, TASK_LIST),
        meta=FOR_THE_MODEL,
    )
    async def peer_agent_task_list(ctx: Context) -> dict[str, Any]:
        """List the tasks this conversation has open — both ways, oldest first.

        **The send tool promises an answer will arrive on its own, and says not
        to poll; this is not a poll.**  It answers a different question, and the
        one the model cannot answer from memory: *what did I start and never
        hear back about, and what was I asked and still owe?*  A conversation
        that has been compacted, a process that restarted, an answer that
        arrived as a message nobody matched to its id — each leaves a task open
        that nothing else names.

        It reads the two tables the send and receive paths already keep, so it
        costs no model call and dials nothing (`A2AMesh.open_tasks`): `_sends`
        is what has not terminaled, and the ledger is what is owed — pending
        bridges, and a previous process's orphans as 'stale'.

        `member` rather than `peer`, because one word is true of both directions
        and this network's members are not the remote network's peers.

        Args:
            ctx: The call, whose client id is whose tasks these are.
        """
        agent, mine = speaker(ctx)
        settings = mesh_settings()
        if not settings.configured:
            return {
                "agent_name": member_name(agent, mine),
                "tasks": [],
                "error": switch_off(net),
            }
        session = session_of(agent, mine)
        return {
            "agent_name": safe_name(session.mesh.agent_name),
            "tasks": session.mesh.open_tasks(),
            "error": "",
        }

    @mcp.tool(
        name=net.tool(BROADCAST),
        description=describe_tool(net, BROADCAST),
        meta=FOR_THE_MODEL,
    )
    async def peer_agent_broadcast(ctx: Context, event: str) -> dict[str, Any]:
        """Publish a fire-and-forget event to every agent on the mesh.

        Nobody answers one and it is delivered at most once, so it is for
        information a peer may act on and not for anything you need a reply to —
        use `peer_agent_send_message` for that. Peers receive it as a `[PeerAgent:…]` of type
        'broadcast', with no task and no completion.

        **A local broadcast wakes the whole roster first**, because published is
        not subscribed: an event reaches whoever is listening, and a maker's
        subagents listen only once somebody has given them a place
        (`place_for_all`).
        """
        sender, mine = speaker(ctx)
        session = await connected(sender, USED, mine)
        await place_for_all(sender)
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


#: What the five tools say, **with the words that differ per network left as
#: holes**.  One text per verb and two word-sets (`_NET_WORDS`), because a second
#: copy of this reasoning is a second copy to keep in step — and because the
#: thing that must differ between the networks is the *vocabulary* a model is
#: taught, which is what the holes are: who it is talking to, and what an
#: envelope looks like.
#:
#: **Keyed by the remote network's verb for the roster**, while the *name* the
#: local instance serves it under is the local network's (`net.roster_tool`):
#: the two networks are different things that happen to share an implementation,
#: and the holes below are where each of them says which thing it is.
_TOOL_TEXTS: dict[str, str] = {
    SEND_MESSAGE: (
        "Send a message to {them}. **A reply is this call**: ending a turn\n"
        "sends nothing to the other side.\n"
        "\n"
        "`message_type` decides what it is. 'task_request' (the default) asks for\n"
        "work and returns an id straight away — the answer arrives later, on its\n"
        "own, as a `{marker}…` of type 'task_response'. Never wait for it and\n"
        "never poll: there is nothing to poll. 'task_response' completes a task\n"
        "somebody sent you, and needs the `task_id` from that task's `{marker}…`\n"
        "marker. 'message' is a plain conversation — no task, no completion, and\n"
        "the reply arrives as a message."
    ),
    CANCEL_TASK: (
        "Withdraw a task you sent to {them}.\n"
        "\n"
        "`status` is the answer: 'cancelled' when the withdrawal went out,\n"
        "'completed'/'failed'/'cancelled' when the task had already ended (a\n"
        "finished result is never lost by asking), or 'not_found' for an id this\n"
        "process never sent."
    ),
    LIST: (
        "List {roster}.\n"
        "\n"
        'Every card is `{{"agent_name": …, "status": "online"|"offline"}}`, and\n'
        "`status` on the first one is your own state on this network.\n"
        "\n"
        "{offline}\n"
        "\n"
        "`error` says why the list is short when it is."
    ),
    BROADCAST: (
        "Publish a fire-and-forget event to {everyone}.\n"
        "\n"
        "Nobody answers one and it is delivered at most once, so it is for\n"
        "information somebody may act on and not for anything you need a reply\n"
        "to — use `{send}` for that. It arrives as a `{marker}…` of type\n"
        "'broadcast', with no task and no completion."
    ),
    TASK_LIST: (
        "List the tasks you have open on {name}.\n"
        "\n"
        "A task is open while it is waiting for an answer ('sent') or while an\n"
        'answer is owed (\'received\'). Every row is `{{"task_id": …, "member": …,\n'
        '"direction": "sent"|"received", "status": …, "since": …}}`, and `member`\n'
        "is the name the task is with — what `{send}` and `{cancel}` address.\n"
        "\n"
        "`status` is 'pending' for a task still running, 'unreachable' for one you\n"
        "sent that was never acknowledged — a late answer can still arrive, so\n"
        "check before sending it again — and 'stale' for a task a previous process\n"
        "died holding: it can never be completed, so answer that member with a\n"
        "plain message.\n"
        "\n"
        "An answer that already arrived is not listed; it is in this conversation."
    ),
}

#: The holes, per network.  `roster` is the one that says what a model is
#: looking at rather than who it is talking to — "you first, then each peer" is
#: true of the remote network and false of the subagent one, where the reader is
#: the agent that made them.  `offline` is that same difference one line down and
#: it is not cosmetic: a peer that said it was leaving and a subagent nobody has
#: woken yet are both `offline` on the card, and what a model should do about
#: them is the opposite — wait, and send.
#:
#: **The remote network's words are its semantic ones.**  No hole here says
#: "A2A", "mesh", "broker" or "MQTT": a model is talking to peer agents, and the
#: wire that carries them is the operator's fact and the plugin's
#: (`slife2.libs.host.config.REMOTE` argues the line).  `section` is the one
#: exception and it has to be — it names the key a person writes in
#: `slife2.yaml` to turn the network on.
_NET_WORDS: dict[str, dict[str, str]] = {
    REMOTE.key: {
        "them": "another peer agent",
        "everyone": "every peer agent",
        "roster": "the peer agents — you first, then each by name",
        "name": "the peer network",
        "section": "`peer_agent:`",
        "offline": (
            "Offline entries are the ones that have said they are leaving, so\n"
            "they are listed rather than hidden."
        ),
    },
    LOCAL.key: {
        # **The members are an agent and the subagents it made, and every one of
        # them may address every other** — a message, a task or a broadcast goes
        # any way round the group.  So no word here may name a relationship: "your
        # subagents" is false for the subagent doing the addressing, and "your
        # agent" is false for the maker — which is the same problem, and the same
        # answer, as `peer_phrase`'s (`**jill**` and no `Peer`).  What every
        # member can read is *member*, and each hole below says what a member is
        # in the words of whoever is reading it.
        #
        # **And the network is named `subagent`, which is this project's word for
        # it** — the section, the tool prefix, the channel and the envelope are
        # all that word.  `local` is the gloss this design uses when talking about
        # the network itself (and the value `unit:` happens to carry); it is not
        # what anything here is called, and a model taught it would be reading a
        # word the rest of its tools do not use.
        "them": (
            "another member of your subagent network —\n"
            "one of the subagents in it, or your agent if you are one of them"
        ),
        "everyone": "every member of your subagent network",
        "roster": (
            "the members of your subagent network —\nyou first, then each by name"
        ),
        "name": "the subagent network",
        "section": "`subagent:`",
        "offline": (
            "A member is listed whether or not it is awake: `offline` means\n"
            "nothing is listening for it at this moment, and a message to it\n"
            "wakes it."
        ),
    },
}


def switch_off(net: Net) -> str:
    """What a machine that has not written this network's section is told.

    **One sentence and two holes, because the fact is one**: `_a2a`'s rule is
    that the section's *presence* is the switch, so the only thing that differs
    between the networks is what the network is called and which section to
    write.  Said here rather than at each of its readers — the tool that raises,
    the roster's `error` field, and the start-up note — because a sentence that
    names a section is one place to name the wrong one, and the remote network's
    words were being said on the subagent network: a machine with a `subagent:`
    section and no mesh was told there was no `peer_agent:` section, and taught a name
    for a thing the rest of its tools do not use.
    """
    words = _NET_WORDS[net.key]
    return (
        f"this machine is not on {words['name']} — there is no {words['section']} "
        f"section in slife2.yaml, and that section is the whole of the switch."
    )


def describe_tool(net: Net, verb: str) -> str:
    """What one of this network's tools says to a model.

    **The schema is where the confusion the two names exist to prevent would
    happen.**  The names separate the networks; if the text did not, a model
    would be told it is sending to "a peer" by a tool called `subagent_send_message`
    — the same sentence for two different things, which is what two vocabularies
    were meant to avoid.  So the words a model reads are this network's, and the
    reasoning behind them is written once above.
    """
    words = _NET_WORDS[net.key]
    return _TOOL_TEXTS[verb].format(
        send=net.tool(SEND_MESSAGE),
        cancel=net.tool(CANCEL_TASK),
        marker=net.marker,
        **words,
    )


def server_note(config: Config, net: Net) -> str:
    """What one instance says about itself at start: which network, and where.

    Two texts because they are two different things to a reader — a machine off
    the mesh has no peers, and one with no subagent network has no subagents to
    reach — and `serve_plugin`'s note is a callable for exactly this: which
    config it describes is only known in there.  Both halves come out of the same
    word-set the refusals use (`switch_off`), so the line that says a network is
    off and the refusal for trying to use it name the same section.
    """
    settings = net_settings(config, net)
    words = _NET_WORDS[net.key]
    if not settings.configured:
        return f"off {words['name']} (no {words['section']} section)"
    return f"{net.bubble} network on {settings.address}, broker started if absent"


def main(argv: list[str] | None = None, *, net: Net = REMOTE) -> int:
    """Serve one of the two networks — the remote one unless told otherwise.

    **`net` is a parameter rather than a second `main`**, because everything
    below it is the same call with different names in it: the thin entry point
    `slife2.plugins.a2a.local_server` is what the launcher starts for the local
    one, and it is three lines because this function already knew how.
    """
    _ensure_windows_selector_loop()
    return serve_plugin(
        argv,
        server_name=net.server_name,
        config_key=net.key,
        build=lambda config: build_server(config, net=net),
        logger=logger,
        note=lambda config: server_note(config, net),
    )


if __name__ == "__main__":
    raise SystemExit(main())
