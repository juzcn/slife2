"""A2A mesh — standard A2A-over-MQTT transport on the official EMQX SDK.

The wire is 100% the ``a2a-over-mqtt`` SDK: topic scheme
(``$a2a/v1/{discovery|request|reply|event}/{org}/{unit}/{agent_id}``),
JSON-RPC 2.0 over MQTT v5 ``ResponseTopic``/``CorrelationData`` properties,
Agent Cards with ``a2a-status`` presence, and the ack → stream → artifact →
terminal task lifecycle.  This module adds only slife's harness glue:

* **Inbound** — a :class:`MeshResponder` (SDK ``Responder`` subclass) whose
  ``on_request`` enqueues the task for the harness and blocks until the model
  completes it out-of-band (:meth:`A2AMesh.complete_task`); the SDK then
  finishes the standard lifecycle (artifact + terminal / cancel) itself.
* **Outbound** — a thin persistent driver (the SDK's ``Requester`` is
  connection-per-call, which would break our async-push / late-result model):
  sends, reply listening, discovery/presence, cancellation.  All wire
  building/parsing uses SDK primitives directly.
* **Presence** — discovery wildcard → peer cache + transition events; the
  SDK's ``Responder`` owns the retained card / LWT for our own identity.
* **Identity** — the agent name is the MQTT client id *and* the request
  topic's last segment, so two instances sharing one would take each other
  over and duplicate every task.  The name is therefore checked against the
  broker's retained card BEFORE joining (:meth:`A2AMesh._probe_own_name`),
  and a claim seen afterwards takes the mesh down rather than letting it
  fight for the name.

Two connections, distinct client ids: the SDK ``Responder`` connects on its
own (presence publishing stays inside the SDK, untouched); the outbound
client (identifier ``{org}/{unit}/{agent}-out``) only subscribes discovery
+ our reply sessions and publishes requests — never presence.

**One mesh per agent, in a process that serves several.**  v1's identity *was*
the process: `--agent` named the MQTT client id, so there was one of these.  Here
the plugin holds one per agent it serves, and everything below is unchanged by
that — the collision check, the two client ids and the topic namespace are all
keyed by the name this object was built with.  What the name *is* comes from the
caller's client id rather than from the config, which is why it is a constructor
argument here and a config field in v1.

The timeouts are v1's numbers, and they are module constants rather than v1's
`slife.timeouts` registry: that registry existed so a value could be re-read at
call time in a long-lived harness process, and nothing here wants one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import uuid
from dataclasses import dataclass
from typing import NewType

import aiomqtt
from a2a_over_mqtt import (
    TERMINAL_KINDS,
    A2ARequest,
    MqttConfig,
    Responder,
    TopicSpace,
    build_card,
    classify_reply,
    get_correlation_data,
    make_properties,
)
from a2a_over_mqtt.protocol import REPLY_ARTIFACT, REPLY_SUBMITTED, REPLY_TEXT

import slife2.timeouts as _T
from slife2.a2a.inbound_store import InboundStore
from slife2.a2a.task_store import get_store
from slife2.config import A2ASettings

logger = logging.getLogger(__name__)

_MAX_INFLIGHT = 16
#: The card extension key holding a mesh instance's id — our only way to tell
#: our own card from a same-named twin's (see ``A2AMesh.__init__``).  The SDK's
#: ``build_card`` nests extensions under ``capabilities.extensions``.
_INSTANCE_EXT = "instance"

# How long `connect()` waits for both connections to be live.  A broker probe
# ran before this in v1; here the caller has already decided the machine is on
# the mesh, so a failure to connect is reported by this deadline.

# How long the pre-join name probe listens for a retained card.  MQTT has no
# end-of-retained marker, so silence within this budget *is* the answer "no
# card".

# A2A-over-MQTT requester retry profile (the profile's standard defaults,
# implemented by the SDK's Requester — mirrored here for the push model):
# first reply within 15 s, retry with exponential backoff 1000/2000/4000 ms
# ± 20 % jitter (a list — the SDK's shape, kept verbatim), at most 3 attempts.
# Retries reuse the same Task.id + context_id (same payload) and generate a
# fresh Correlation Data.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = [1.0, 2.0, 4.0]
_JITTER_FACTOR = 0.2
_MAX_TRACKED_SENDS = 500  # delivery-tracking cap (mirrors the task store)
#: Cap on correlation→task routing entries.  Every correlation a task was
#: published under stays routed until its TERMINAL reply lands (the peer may
#: answer on any of them — see ``_deliver``), then all of them are dropped
#: together.  The cap is a safety valve for sends that never complete — a
#: long session (or sends evicted from the capped ``_sends`` cache before
#: their reply) would otherwise grow ``_corr_to_task`` without limit.
_MAX_CORR_ENTRIES = 2 * _MAX_TRACKED_SENDS


class FIFOCache(dict):
    """A dict that forgets its oldest entry past a cap.

    v1 took this from `cachetools`; this project does not declare a dependency
    for a dict and a cut-off (`slife2.wechat`'s `Window` makes the same call
    about v1's `TTLCache`).  A `dict` is insertion-ordered, so the oldest key is
    always `next(iter(self))` and the eviction is one line — and the API the mesh
    uses (`[]=`, `get`, `pop`, `values`, `clear`, `len`) is `dict`'s own, which is
    what lets the ported code below stay as it was written.
    """

    def __init__(self, maxsize: int) -> None:
        super().__init__()
        self.maxsize = maxsize

    def __setitem__(self, key: str, value: object) -> None:
        super().__setitem__(key, value)
        while len(self) > self.maxsize:
            del self[next(iter(self))]


# ── Card: who a peer is, and what may be printed about it ───────────────
#
# v1 kept this in `a2a/card.py`, apart from the mesh, because the TUI and the
# per-turn prompt both imported it.  Neither reader survives the port — the TUI
# draws an `A2A(<peer>)> ` bubble from the channel, and a presence *change* now
# reaches the model as a message — so the two things this project actually uses,
# the record and the scrubber, are here beside the code that produces them.

AgentName = NewType("AgentName", str)
"""Identifies an agent by its name — the mesh identity, and the MQTT client id."""

# Control characters a remote peer could use to break out of a single display
# line in a handed-over message, the TUI, or logs (newlines, tabs, ESC, …).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def safe_name(value: object) -> str:
    """Display-safe form of a remote, untrusted peer value.

    Presence fields (``agent_name``, ``status``) come from the MQTT wire with no
    validation.  Strip control characters — which would otherwise let a peer
    inject instructions into the message handed to the model — and cap the length
    so a name cannot bloat a context.
    """
    s = _CONTROL_RE.sub(" ", str(value))
    return " ".join(s.split())[:128]


@dataclass
class AgentCard:
    """Who is this agent and is it alive right now?

    ``status`` is one of ``"online"`` / ``"offline"`` on the wire (the standard
    ``a2a-status`` presence, mapped by the mesh).  The dataclass default keeps
    the historic ``"idle"`` for constructed-but-unset cards.
    """

    agent_name: AgentName
    status: str = "idle"  # "online" or "offline"


def format_presence_line(card: AgentCard, event: str) -> str | None:
    """Render a presence event as the one line it is worth telling anybody.

    Returns ``None`` for events that are not visible transitions
    (``"status_change"`` — a heartbeat from an already-known peer) so callers can
    filter them out.

    v1 shared this between the TUI and the per-turn prompt so the two could not
    drift; the line is unchanged and its one reader now is the message the mesh
    hands to a conversation.
    """
    name = safe_name(card.agent_name)
    status = safe_name(card.status)
    if event == "online":
        return f"⚡ {name} online [{status}]"
    if event == "offline":
        return f"✗ {name} offline"
    return None


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with ±20 % jitter for retry attempt (0-indexed)."""
    base = _BACKOFF_BASE_S[min(attempt, len(_BACKOFF_BASE_S) - 1)]
    jitter = base * _JITTER_FACTOR * (2 * random.random() - 1)
    return base + jitter


def _decode_payload(msg) -> dict | None:
    """Decode an MQTT payload to JSON; None when empty or unparseable.

    ``utf-8 replace`` keeps a binary or mis-encoded payload from raising,
    and an unparseable body is dropped by callers rather than fatal.
    """
    raw = msg.payload.decode("utf-8", "replace") if msg.payload else ""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


class _OutboundSend:
    """One outbound message being delivered with the requester retry profile."""

    __slots__ = (
        "task_id",
        "agent",
        "session",
        "payload",
        "attempts",
        "delivered",
        "loops",
        "record",
    )

    def __init__(
        self,
        task_id: str,
        agent: str,
        session: str,
        payload: str,
        *,
        record: bool = True,
    ) -> None:
        self.task_id = task_id
        self.agent = agent
        self.session = session
        self.payload = payload
        self.attempts = 1
        self.delivered = False
        self.loops: set[asyncio.Task] = set()
        #: Whether this send created a task (TASK_REQUEST) or is a bare
        #: conversation (MESSAGE) — the reply is pushed as a completion or as a
        #: message accordingly.
        self.record = record


class _InboundWait:
    """One inbound task's completion bridge: resolved out of band, once."""

    __slots__ = ("outcome", "_event")

    def __init__(self) -> None:
        self.outcome: tuple[bool, str] | None = None
        self._event = asyncio.Event()

    def resolve(self, result: str, cancelled: bool) -> bool:
        if self.outcome is not None:
            return False
        self.outcome = (cancelled, result)
        self._event.set()
        return True

    async def wait(self) -> tuple[bool, str]:
        await self._event.wait()
        assert self.outcome is not None
        return self.outcome


class MeshResponder(Responder):
    """SDK :class:`Responder` bridged to the harness's out-of-band completion.

    The SDK runs the full protocol lifecycle inside ``run()`` (ack → working →
    artifact → terminal, per-task dedup, ``CancelTask``, LWT).  slife completes
    a task from OUTSIDE the request scope — the plugin hands the task to the
    agent's conversation and the model answers, possibly many turns later, by
    sending a ``message_type="task_response"`` message that resolves
    ``on_request``'s blocked outcome — so ``on_request`` enqueues the task and
    blocks on a per-task outcome.
    """

    def __init__(self, *args, mesh: A2AMesh, **kwargs) -> None:
        super().__init__(*args, max_concurrent=_MAX_INFLIGHT, **kwargs)
        self._mesh = mesh
        self._completion: dict[str, _InboundWait] = {}

    async def on_request(
        self,
        request: A2ARequest,
        stream,
    ) -> str | None:
        task_id = str(request.task_id)
        waiter = _InboundWait()
        self._completion[task_id] = waiter

        # TASK_RESPONSE continuity: keep the standard stream alive while the
        # agent works on the turn.  Stops the moment the outcome resolves; a
        # dead connection just ends the pings.
        async def _keepalive() -> None:
            try:
                while waiter.outcome is None:
                    await asyncio.sleep(_T.timeouts.deliver.keepalive)
                    if waiter.outcome is not None:
                        return
                    try:
                        await stream("task acknowledged — working on it")
                    except Exception:  # noqa: BLE001 - any failure here is the stream, and the stream is the thing this exists to keep
                        return  # connection is gone; stop pinging
            except asyncio.CancelledError:
                pass

        pinger = asyncio.create_task(_keepalive())
        try:
            message_type = request.variables.get("message_type", "task_request")
            if message_type == "task_response":
                # A received TASK_RESPONSE is NOT a new task: a peer that
                # streams its result reaches us via the requester's response
                # topic (classified as a result push); a stray response-typed
                # message on our request topic is acknowledged silently and
                # never enqueued as an inbound task.  Return None so the SDK
                # completes it (empty artifact, terminal) without a record.
                logger.info(
                    "a2a_inbound_response_ignored task=%s from=%s",
                    task_id,
                    request.sender or "unknown",
                )
                return None
            if message_type == "message":
                # MESSAGE: a conversational message — not a task.  Enqueue it
                # task-less (no completion expected, no bridge) and finish:
                # the SDK empty-completes it, and the peer's real reply
                # arrives as a NEW inbound message of its own.
                self._mesh.on_inbound_task(
                    request.sender or "unknown",
                    request.text,
                    "",
                    kind="message",
                )
                return None
            self._mesh.on_inbound_task(
                request.sender or "unknown",
                request.text,
                task_id,
            )
            # Recorded only now — a MESSAGE / stray TASK_RESPONSE reaches the
            # early returns above and must NOT be tracked as an inbound task
            # (nothing will ever complete it, so it would be reported stale
            # forever after the next restart).
            self._mesh._note_inbound(task_id, request.sender or "unknown")
            try:
                cancelled, result = await waiter.wait()
            except BaseException:
                # The SDK cancelled this task.  Surface the peer's withdrawal
                # to the agent — as an inbound MESSAGE, never a control
                # signal — only when it can actually have been one: not our
                # own completion, not shutdown, and on a LIVE link.
                #
                # The link check matters: when the responder's own connection
                # ends, the SDK's ``run()`` cancels every inflight handler the
                # same way (``responder.py`` finally), so a dead link would
                # otherwise report a withdrawal that never happened.  Silence
                # is right there — the peer's requester profile re-sends the
                # task (first reply within ``_T.timeouts.deliver.reply_first``, ≤3
                # attempts) and the retry arrives as a fresh request that this
                # side re-enqueues.
                # Residual and accepted: a responder-only
                # session loss with the outbound link still up looks like a
                # peer cancel.
                if (
                    waiter.outcome is None
                    and not self._mesh._closing
                    and self._mesh.is_connected
                ):
                    peer = request.sender or "unknown"
                    self._mesh._note_withdrawn(task_id, peer)
                    self._mesh.on_peer_cancel(task_id, peer)
                raise
            if cancelled:
                raise asyncio.CancelledError("task cancelled by the agent")
            return result
        finally:
            pinger.cancel()
            self._completion.pop(task_id, None)
            self._mesh._drop_inbound(task_id)

    def resolve(self, task_id: str, result: str, cancelled: bool) -> str:
        """Resolve an inbound task's completion bridge.

        Returns ``"ok"``, or an error string.  Refuses unknown or
        already-completed task ids — each task completes exactly once (the SDK
        publishes its final status exactly once).
        """
        waiter = self._completion.get(task_id)
        if waiter is None:
            return self._mesh._unresolvable(task_id)
        if not waiter.resolve(result or "", cancelled):
            return f"Error: task_id {task_id!r} already completed."
        return "ok"


class _PresencePeer:
    __slots__ = ("name", "status")

    def __init__(self, name: str, status: str) -> None:
        self.name = name
        self.status = status  # "online" | "offline"


def _user_property(props, key: str) -> str:
    """Read an MQTT v5 user property (list of (k, v) tuples)."""
    if props is None:
        return ""
    user_props = getattr(props, "UserProperty", None)
    if not user_props:
        return ""
    for k, v in user_props:
        if k == key:
            return str(v)
    return ""


def _presence_status(props) -> str:
    """Map the standard ``a2a-status`` presence property to online/offline.

    Absent property (a card without presence stamping) defaults to online.
    """
    status = _user_property(props, "a2a-status")
    if not status:
        return "online"
    return "offline" if status == "offline" else "online"


def _card_instance(data: dict | None) -> str:
    """The mesh instance id a card advertises — ``""`` when it carries none.

    Read from ``capabilities.extensions`` (where the SDK's ``build_card``
    nests them).  A card from another implementation, or from an older Slife,
    simply has none: not ours, but also not evidence of a twin.
    """
    caps = (data or {}).get("capabilities")
    if not isinstance(caps, dict):
        return ""
    for ext in caps.get("extensions") or []:
        if isinstance(ext, dict) and ext.get(_INSTANCE_EXT):
            return str(ext[_INSTANCE_EXT])
    return ""


def _reply_state(data: dict) -> str:
    """Store status implied by a terminal reply's raw wire state."""
    result = data.get("result") or {}
    status = result.get("statusUpdate", {}).get("status", {})
    if not status:
        status = result.get("task", {}).get("status", {})
    state = (status.get("state") or "").upper()
    if state == "TASK_STATE_COMPLETED":
        return "completed"
    if state == "TASK_STATE_CANCELED":
        return "cancelled"
    return "failed"


class A2AMesh:
    """Standard A2A mesh driver — inbound responder + thin outbound client.

    The plugin sets the five callbacks, then :meth:`connect` /
    :meth:`disconnect`.  Tools call :meth:`send_message`, :meth:`cancel_task`,
    :meth:`list_agents`, :meth:`complete_task`.
    """

    def __init__(self, config: A2ASettings, agent: str) -> None:
        self._config = config
        self._topics = TopicSpace(
            org=config.org or "default",
            unit=config.unit or "default",
        )
        self.agent_name = agent
        self._mqtt_cfg = MqttConfig(
            host=config.broker_host,
            port=config.broker_port,
        )
        #: This process's A2A instance id, carried in our own card.  The card
        #: schema has no field for "which process", and the agent name cannot
        #: serve — telling our own card from a same-named twin's is exactly
        #: what it must not do.  A fresh id per mesh, so a predecessor's
        #: retained card is never mistaken for our own.
        self._instance = uuid.uuid4().hex
        self._card = build_card(
            name=self.agent_name,
            description="slife agent — A2A mesh peer",
            url=f"mqtt://{config.broker_host}:{config.broker_port}",
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            extensions=[{_INSTANCE_EXT: self._instance}],
        )
        #: Inbound tasks awaiting a result — persisted, per agent, so a
        #: restarted process can tell the ones its predecessor died holding
        #: (which can never be completed) from ids it has simply never seen.
        self._inbound = InboundStore.open(self.agent_name)
        self._responder = MeshResponder(
            agent_id=self.agent_name,
            mqtt=self._mqtt_cfg,
            topics=self._topics,
            card=self._card,
            mesh=self,
        )

        self._outbound: aiomqtt.Client | None = None
        self._connected = False
        self._closing = False
        #: Set when another agent turns out to hold our name — the mesh then
        #: leaves the broker (see :meth:`_note_collision`).
        self._collision = ""
        self._ready = asyncio.Event()  # outbound connected + subscribed
        self._responder_ready = asyncio.Event()  # inbound responder subscribed
        self._tasks: list[asyncio.Task] = []
        self._peers: dict[str, _PresencePeer] = {}
        # task_id → last artifact text.  A FIFOCache like the ones below, and for
        # the same reason: an artifact that never reaches a terminal must not
        # accumulate strings for the session, and the oldest is the one least
        # likely to ever be answered.  The bound used to be hand-rolled at the
        # insertion site; it lives in the class now so the cap is stated once.
        self._pending: FIFOCache = FIFOCache(maxsize=_MAX_TRACKED_SENDS)
        # task_id → the peer that withdrew it.  A note, not a recovery path:
        # the completion bridge died with the withdrawal, so this exists only
        # so a late ``task_response`` for that id gets the truthful answer
        # instead of the typo-flavoured "unknown id" (:meth:`_unresolvable`).
        # FIFO-bounded like the other per-session caches.
        self._withdrawn: FIFOCache = FIFOCache(maxsize=_MAX_TRACKED_SENDS)
        # FIFO-capacity: evicts the oldest tracked send past _MAX_TRACKED_SENDS.
        self._sends: FIFOCache = FIFOCache(maxsize=_MAX_TRACKED_SENDS)
        # correlation → task_id.  Capped at twice ``_sends`` because every
        # correlation a task was published under stays routed until its TERMINAL
        # reply lands (see ``_deliver``), so a task's retries each cost a route.
        # FIFOCache again: the bound used to be a hand-rolled ``pop`` in
        # ``_publish_attempt``, which is the same one line the class already is.
        self._corr_to_task: FIFOCache = FIFOCache(maxsize=_MAX_CORR_ENTRIES)

        # Agent-side callbacks — replaced by the plugin before connect().
        self.on_inbound_task = lambda source, content, task_id, kind="task": None
        self.on_task_completion = lambda corr_id, result, cancelled, peer, kind="task": (
            None
        )
        self.on_agent_change = lambda card, event: None
        # A peer's CancelTask, delivered to the agent: the model is told and
        # decides, because whether the running turn is working on that task is
        # known only to the model.  Never a control signal.
        self.on_peer_cancel = lambda task_id, peer: None
        self.on_broadcast_event = lambda sender, text: None

    # ── Connection ──────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """Whether the outbound connection is live (health probe trivium)."""
        return self._connected

    @property
    def status(self) -> str:
        return "online" if self._connected else "offline"

    @property
    def broker_address(self) -> str:
        return f"{self._config.broker_host}:{self._config.broker_port}"

    @property
    def collision(self) -> str:
        """Why this mesh refused the name — empty when it holds it.

        Non-empty means another agent is using our name on the broker: either
        the pre-join check saw it and :meth:`connect` raised, or it announced
        itself afterwards and the mesh left (see :meth:`_note_collision`).
        """
        return self._collision

    async def connect(self) -> None:
        """Start the inbound responder and the outbound driver.

        Returns once the outbound connection is established — the caller has
        already decided this machine is on the mesh, so a failure here is a
        genuine error surfaced to the caller.  Raises on connect timeout, and
        raises *before* connecting at all when another agent already holds our
        name on the broker (see :meth:`_probe_own_name`) — joining would take
        that agent's session over and duplicate every task sent to the name.
        """
        await self.disconnect()
        self._closing = False
        self._collision = ""
        self._ready = asyncio.Event()
        self._responder_ready = asyncio.Event()
        claim = await self._probe_own_name()
        if claim:
            raise RuntimeError(claim)
        self._tasks.append(asyncio.create_task(self._run_responder()))
        self._tasks.append(asyncio.create_task(self._run_outbound()))
        try:
            await asyncio.wait_for(
                self._ready.wait(), timeout=_T.timeouts.deliver.mqtt_connect
            )
            # The SDK responder subscribes its request topic BEFORE publishing
            # the retained online card — so our own online card sighted on the
            # discovery wildcard means inbound delivery is live.  Without this
            # gate, a send issued right after connect() could be dropped before
            # the responder subscribed (recovered only by the 15 s delivery
            # retry).
            await asyncio.wait_for(
                self._responder_ready.wait(),
                timeout=_T.timeouts.deliver.mqtt_connect,
            )
        except TimeoutError:
            await self.disconnect()
            raise RuntimeError(
                f"A2A mesh connect timeout — is the broker {self.broker_address} up?"
            ) from None

    async def _probe_own_name(self) -> str | None:
        """Whether another agent already holds our name on this broker.

        The name is the MQTT client id *and* the request topic's last segment,
        and a broker's answer to a duplicate client id is to disconnect the
        incumbent — so two instances sharing a name kick each other in a loop
        and every task sent to the name is executed by both (each subscribes
        to the same request topic).  Nothing on the wire arbitrates it, so the
        check is made here, BEFORE joining: a throwaway connection — its own
        unique client id, so it can never take a session over — reads the
        retained card on our own discovery topic.

        Returns a description of the claim, or ``None`` when the name is free.
        """
        kwargs = self._mqtt_cfg.client_kwargs(
            identifier=(
                f"{self._topics.org}/{self._topics.unit}/{self.agent_name}"
                f"-probe-{uuid.uuid4().hex[:8]}"
            ),
        )
        try:
            async with aiomqtt.Client(**kwargs) as client:
                await client.subscribe(
                    self._topics.discovery(self.agent_name),
                    qos=1,
                )
                try:
                    async with asyncio.timeout(_T.timeouts.deliver.name_probe):
                        async for msg in client.messages:
                            claim = self._foreign_claim(msg)
                            if claim is not None:
                                return claim
                except TimeoutError:
                    return None
        except (aiomqtt.MqttError, OSError, ValueError) as e:
            # An unreachable or rejecting broker is not a name problem — the
            # connect below reports it, with the error that belongs to it.
            logger.debug("a2a_name_probe_failed err=%s", e)
        return None

    def _foreign_claim(self, msg) -> str | None:
        """Whether *msg* is another agent holding our name — and how it looks.

        A card on our own discovery topic claims the name unless it is our own
        instance's or a *retirement*: ``a2a-status: offline`` is what a
        departing agent and a last will both publish, so a predecessor's
        retained card is a card nobody holds.  A deleted card (empty payload)
        is the absence of a claim, not one.
        """
        try:
            agent_id = msg.topic.value.rsplit("/", 1)[-1]
        except (AttributeError, IndexError):
            return None
        if agent_id != self.agent_name or not msg.payload:
            return None
        instance = _card_instance(_decode_payload(msg))
        if instance and instance == self._instance:
            return None  # our own card, echoed back
        if _presence_status(msg.properties) == "offline":
            return None  # retired — nobody is holding the name
        who = (
            f"another A2A instance ({instance[:8]})"
            if instance
            else "an agent that carries no instance id"
        )
        return (
            f"agent name {self.agent_name!r} is already in use on "
            f"{self._topics.org}/{self._topics.unit} — {who} announced it "
            f"online"
        )

    def _note_collision(self, claim: str) -> None:
        """Record that another agent holds our name; the outbound loop leaves
        the broker on the message that carried it (:meth:`_run_outbound`)."""
        if self._collision:
            return
        self._collision = claim
        logger.error("a2a_name_collision %s", claim)

    async def disconnect(self) -> None:
        """Stop both connections; inflight inbound tasks get cancelled by the
        SDK (their cancel noise is suppressed by the ``_closing`` gate).

        Never cancels its own caller: a mesh task tearing the mesh down (the
        outbound loop, on a name collision) must be skipped, or it would
        cancel itself mid-teardown.
        """
        self._closing = True
        current = asyncio.current_task()
        tasks, self._tasks = self._tasks, []
        loops = [loop for send in self._sends.values() for loop in send.loops]
        mine = [t for t in tasks + loops if t is not current]
        for task in mine:
            if not task.done():
                task.cancel()
        if mine:
            await asyncio.gather(*mine, return_exceptions=True)
        self._sends.clear()
        self._corr_to_task.clear()
        self._pending.clear()  # artifact texts for tasks that never completed
        self._connected = False
        self._outbound = None

    async def _run_responder(self) -> None:
        """The SDK responder: own connection, presence, inbound lifecycle.

        run() ends when the connection drops (the SDK has no reconnect) — loop
        until shutdown so a broker hiccup re-advertises presence + requests.
        """
        while not self._closing:
            try:
                await self._responder.run()
            except asyncio.CancelledError:
                raise
            # The SDK's `run()` ends however the connection ends — a broker
            # restart, a network blip, a protocol error — and it has no
            # reconnect of its own, so every one of them is this loop's to
            # answer.  Narrowing it would mean listing the MQTT library's
            # exception classes here, and a class it grows tomorrow would take
            # the responder down for the life of the process.
            except Exception as e:  # noqa: BLE001 - see above
                if self._closing:
                    break
                logger.warning(
                    "a2a_responder_disconnected err=%s retry=%.1fs",
                    e,
                    _T.timeouts.pacing.mesh_retry_delay,
                )
                try:
                    await asyncio.sleep(_T.timeouts.pacing.mesh_retry_delay)
                except asyncio.CancelledError:
                    raise

    async def _run_outbound(self) -> None:
        """Persistent outbound client: sends, reply listening, discovery.

        Subscribes discovery wildcard + our reply sessions; reconnects on drop
        until shutdown.  Publishes requests only — the responder owns presence.
        """
        kwargs = self._mqtt_cfg.client_kwargs(
            identifier=(
                f"{self._topics.org}/{self._topics.unit}/{self.agent_name}-out"
            ),
        )
        while not self._closing:
            try:
                async with aiomqtt.Client(**kwargs) as client:
                    await client.subscribe(
                        self._topics.discovery_wildcard(),
                        qos=1,
                    )
                    await client.subscribe(
                        self._topics.reply(self.agent_name, "+"),
                        qos=1,
                    )
                    await client.subscribe(
                        self._topics.event("+"),
                        qos=1,
                    )
                    self._outbound = client
                    self._connected = True
                    self._ready.set()
                    logger.info(
                        "a2a_outbound_connected id=%s broker=%s",
                        kwargs["identifier"],
                        self.broker_address,
                    )
                    try:
                        async for msg in client.messages:
                            self._handle_message(msg)
                            if self._collision:
                                # A twin announced our name while we were live.
                                # Leave rather than fight it for the client id
                                # — reconnecting would take its session over and
                                # the two of us would trade the name forever.
                                break
                    except asyncio.CancelledError:
                        raise
                    except (aiomqtt.MqttError, AttributeError, ValueError):
                        pass  # connection dropped → reconnect below
                    except Exception:  # noqa: BLE001 - a handler bug must not be read as a disconnect, and naming the handlers' exceptions would be naming every one a future handler might raise
                        # A handler/parse bug must not masquerade as a
                        # disconnect (that would spin reconnects); log and
                        # keep consuming.
                        logger.exception("a2a_outbound_loop_error")
            except asyncio.CancelledError:
                raise
            # Same as the responder's: a dropped connection is reconnected
            # however it dropped.
            except Exception as e:  # noqa: BLE001 - see above
                if self._closing:
                    break
                logger.debug(
                    "a2a_outbound_disconnected err=%s retry=%.1fs",
                    e,
                    _T.timeouts.pacing.mesh_retry_delay,
                )
            finally:
                self._connected = False
                self._outbound = None
            if self._collision:
                await self.disconnect()  # also takes the responder down
                return
            if not self._closing:
                try:
                    await asyncio.sleep(_T.timeouts.pacing.mesh_retry_delay)
                except asyncio.CancelledError:
                    raise

    # ── Inbound routing ─────────────────────────────────────────────────

    def _handle_message(self, msg) -> None:
        try:
            category = msg.topic.value.split("/")[2]
        except IndexError:
            return
        if category == "discovery":
            self._handle_discovery(msg)
        elif category == "reply":
            self._handle_reply(msg)
        elif category == "event":
            self._handle_event(msg)

    def _handle_event(self, msg) -> None:
        """Fire-and-forget broadcast event (no reply, no task_id).

        The payload carries ``{"sender": …, "text": …}`` so receivers can
        attribute a broadcast whose topic's final segment is the shared
        ``broadcast`` id (not the publisher's address).
        """
        data = _decode_payload(msg)
        if data is None:
            return  # a broadcast we don't recognise is dropped
        sender = data.get("sender", "")
        text = data.get("text", "")
        if sender == self.agent_name:
            return  # our own broadcast echo
        if not text:
            return
        self.on_broadcast_event(sender or "unknown", text)

    def _handle_discovery(self, msg) -> None:
        topic = msg.topic.value
        try:
            agent_id = topic.rsplit("/", 1)[1]
        except IndexError:
            return
        if agent_id == self.agent_name:
            # A card on OUR OWN topic: either our own (the SDK responder
            # publishes it AFTER subscribing its request topic, so an ONLINE
            # sighting of it is the deterministic "inbound subscription live"
            # signal connect() gates on) or a second instance holding our
            # name.  Never cached/announced as a peer either way.
            claim = self._foreign_claim(msg)
            if claim is not None:
                self._note_collision(claim)
                return
            # Only a real card announces: a deleted one (empty payload) is how
            # a publisher retires its card, and its absent status property
            # would otherwise read as "online" — arming the readiness gate on
            # the absence of a card.
            if msg.payload and _presence_status(msg.properties) == "online":
                self._responder_ready.set()
            return
        if not msg.payload:
            # An EMPTY retained publish is how a publisher retires its card
            # (MQTT has no "card removed" event, so the deletion arrives as an
            # ordinary message).  It carries no `a2a-status` property, and the
            # absent-property default is "online" — so without this the
            # deletion would RESURRECT the peer it just retired.  Dropping the
            # card means dropping the peer: a session that watched one vanish
            # must not keep it in the roster.
            prev = self._peers.pop(agent_id, None)
            if prev is not None and prev.status == "online":
                self.on_agent_change(
                    AgentCard(agent_name=AgentName(agent_id), status="offline"),
                    "offline",
                )
            logger.debug("a2a_presence_card_deleted agent=%s", agent_id)
            return
        status = _presence_status(msg.properties)
        prev = self._peers.get(agent_id)
        if prev is not None and prev.status == status:
            return  # no transition → no event
        self._peers[agent_id] = _PresencePeer(agent_id, status)
        if prev is None and status == "offline":
            # A cold retained offline card — the peer was already gone BEFORE
            # we subscribed (mosquitto re-sends every retained card on
            # subscribe) — is not a transition: cache it so the roster knows,
            # but don't announce a fake "went offline" (dead cards from past
            # sessions would otherwise fire endless ✗ offline lines).  The
            # peer's first ONLINE card IS a transition and will announce.
            logger.debug("a2a_presence_cold_offline agent=%s", agent_id)
            return
        event = "online" if status == "online" else "offline"
        self.on_agent_change(
            AgentCard(agent_name=AgentName(agent_id), status=status),
            event,
        )

    def _handle_reply(self, msg) -> None:
        corr = get_correlation_data(msg)
        if not corr:
            return
        task_id = self._corr_to_task.get(corr)
        if task_id is None:
            # A reply for an unknown/expired send — nothing to route.
            logger.debug("a2a_reply_unknown_corr corr=%s", corr)
            return
        send = self._sends.get(task_id)
        if send is not None:
            send.delivered = True  # any classified reply confirms delivery
        data = _decode_payload(msg)
        if data is None:
            return
        kind, content = classify_reply(data)
        if kind in (REPLY_SUBMITTED, REPLY_TEXT):
            return
        if kind == REPLY_ARTIFACT:
            # The final artifact precedes the terminal status event; hold its
            # text so the terminal (which carries no message) has a result.
            # FIFOCache evicts the oldest past the cap (see `__init__`).
            self._pending[task_id] = content
            return
        if kind not in TERMINAL_KINDS:
            return
        result = self._pending.pop(task_id, "") or content
        state = _reply_state(data)
        cancelled = state == "cancelled"
        if send is not None and not send.record:
            # MESSAGE conversation: no task record was created — push the
            # reply as a message, not a task completion.
            self.on_task_completion(
                task_id,
                result,
                cancelled,
                send.agent,
                "message",
            )
        else:
            # TASK_REQUEST: record the terminal state on the task store.
            record = get_store().get(task_id)
            if record is None:
                return
            if state == "cancelled":
                get_store().record_cancel(task_id)
            elif state == "completed":
                get_store().record_result(task_id)
            else:
                get_store().record_error(task_id)
            self.on_task_completion(
                task_id,
                result,
                cancelled,
                record.agent_name,
                "task",
            )
        # Terminal reply → the send is finished; stop tracking its delivery and
        # drop EVERY routing entry for this task — the primary correlation and
        # every retry correlation the peer may have answered on (the terminal
        # can ride any of them, see :meth:`_deliver`) — so the table cannot
        # accumulate stale routes across a session.
        self._sends.pop(task_id, None)
        for c in [c for c, tid in self._corr_to_task.items() if tid == task_id]:
            self._corr_to_task.pop(c, None)

    # ── Outbound operations (LLM-facing, via the plugin) ────────────────

    async def _publish_props(
        self,
        topic: str,
        payload: str,
        props,
        *,
        qos: int = 1,
    ) -> None:
        """Publish with MQTT v5 ResponseTopic + CorrelationData properties —
        required: the receiving ``validate_a2a_request`` rejects requests
        without them."""
        client = self._outbound
        if client is None or not self._connected:
            raise RuntimeError("A2A mesh is not connected.")
        await client.publish(topic, payload, qos=qos, properties=props)

    async def _publish_attempt(self, send: _OutboundSend, corr: str) -> None:
        """One requester-profile attempt — fresh Correlation Data, same
        payload + session response topic."""
        # FIFOCache evicts the oldest routing entry past the cap (see
        # `__init__`) — a send whose reply may never come, or one already
        # evicted from the capped `_sends` cache, no longer grows the table.
        self._corr_to_task[corr] = send.task_id
        props = make_properties(
            response_topic=self._topics.reply(self.agent_name, send.session),
            correlation_data=corr,
        )
        await self._publish_props(
            self._topics.request(send.agent),
            send.payload,
            props,
        )

    async def _deliver(self, send: _OutboundSend) -> None:
        """Requester retry profile for one outbound task.

        Re-publish the same payload (same Task.id + context_id) with a fresh
        Correlation Data until a first reply arrives or the attempts are
        exhausted.  A peer that already received (and deduped) the request
        answers a retry with a ``working`` replay — which counts as the first
        reply, confirming delivery while the task keeps running.
        """
        try:
            try:
                await asyncio.sleep(_T.timeouts.deliver.reply_first)
            except asyncio.CancelledError:
                raise
            while not (self._closing or send.delivered):
                if send.attempts >= _MAX_ATTEMPTS:
                    logger.warning(
                        "a2a_delivery_exhausted task=%s attempts=%d",
                        send.task_id,
                        send.attempts,
                    )
                    return
                await asyncio.sleep(_backoff_delay(send.attempts - 1))
                if self._closing or send.delivered:
                    return
                send.attempts += 1
                try:
                    await self._publish_attempt(send, uuid.uuid4().hex[:16])
                except Exception as e:  # noqa: BLE001 - a failed attempt is retried, not raised: the budget below is what decides, and every client error means the same thing here
                    logger.debug(
                        "a2a_delivery_retry_failed task=%s err=%s",
                        send.task_id,
                        e,
                    )
                    continue  # still under the attempts budget — try again
                logger.info(
                    "a2a_delivery_retry task=%s attempt=%d",
                    send.task_id,
                    send.attempts,
                )
                try:
                    await asyncio.sleep(_T.timeouts.deliver.reply_first)
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            raise
        finally:
            # Routing entries are deliberately LEFT in place here.  The peer
            # answers each retry on the correlation it first saw — usually a
            # retry correlation, not the primary one — so the terminal reply
            # must still route after `delivered` flips.  Dropping retry
            # correlations on `delivered` would lose the result
            # (a2a_reply_unknown_corr).  The map stays bounded by
            # _MAX_CORR_ENTRIES, and every entry for a task is removed together
            # when its terminal reply routes (:meth:`_handle_reply`).
            # Bookkeeping must also survive GC of an unawaited coroutine when
            # the loop is already closed: asyncio.current_task() then raises
            # RuntimeError ("no running event loop"), which would turn the
            # GeneratorExit into an unraisable-exception warning.
            try:
                task = asyncio.current_task()
            except RuntimeError:
                task = None
            if task is not None:
                send.loops.discard(task)

    async def send_message(
        self,
        agent: str,
        message: str,
        *,
        message_type: str = "task_request",
    ) -> str:
        """Standard ``SendMessage`` — returns the task_id immediately.

        ``message_type`` rides the request's ``metadata.variables`` so a peer
        can classify the message: only ``"task_request"`` creates a task
        (recorded + result pushed); ``"message"`` is a bare conversation (not
        recorded, replies pushed as messages); ``"task_response"`` is unused
        here (completed via :meth:`complete_task`).  Delivery follows the
        requester retry profile; the result is pushed back later
        (:meth:`_handle_reply` terminal → :attr:`on_task_completion`).
        """
        task_id = uuid.uuid4().hex
        session = uuid.uuid4().hex[:12]
        request = A2ARequest(
            text=message,
            request_id=task_id,
            task_id=task_id,
            sender=self.agent_name,
            variables={"message_type": message_type},
        )
        if message_type == "message":
            # A plain message is the ONLY reply an orphaned task can still
            # get (see :meth:`_unresolvable`) — answering the peer settles
            # whatever inbound work this process' predecessor died holding,
            # so the orphan notice drops it.
            self._inbound.clear_stale_peer(agent)
        record = message_type == "task_request"
        if record:
            get_store().record_send(task_id, agent)
        send = _OutboundSend(
            task_id,
            agent,
            session,
            request.to_json(),
            record=record,
        )
        self._sends[task_id] = send  # FIFOCache evicts the oldest past the cap
        loop = asyncio.create_task(self._deliver(send))
        send.loops.add(loop)
        # First attempt — correlation == the public task_id.
        await self._publish_attempt(send, task_id)
        return task_id

    async def cancel_task(self, agent: str, task_id: str) -> str:
        """Standard ``CancelTask``; returns the resulting status string."""
        record = get_store().get(task_id)
        if record is None:
            return "not_found"
        if record.status in ("completed", "failed", "cancelled"):
            return record.status
        session = uuid.uuid4().hex[:12]
        props = make_properties(
            response_topic=self._topics.reply(self.agent_name, session),
            correlation_data=task_id,
        )
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": task_id,
                "method": "CancelTask",
                "params": {"id": task_id},
            }
        )
        await self._publish_props(self._topics.request(agent), payload, props)
        return "cancelled"

    async def broadcast(self, event: str) -> None:
        """Publish a fire-and-forget event (QoS 0) on the unit's event topic.

        Subscribers receive it as an inbound ``[A2A:…]`` event without a
        task_id — no reply is expected, and the payload's ``sender`` lets a
        receiver filter its own echo.
        """
        payload = json.dumps(
            {"sender": self.agent_name, "text": event},
            ensure_ascii=False,
        )
        await self._publish_plain(self._topics.event("broadcast"), payload, qos=0)

    async def _publish_plain(
        self,
        topic: str,
        payload: str,
        *,
        qos: int = 0,
    ) -> None:
        """Publish without MQTT v5 request/response properties."""
        client = self._outbound
        if client is None or not self._connected:
            raise RuntimeError("A2A mesh is not connected.")
        await client.publish(topic, payload, qos=qos)

    def list_agents(self) -> list[AgentCard]:
        """Own card first, then each discovered peer (by name).  Pure cache
        read — never side-effects a connection (health-probe requirement)."""
        cards = [
            AgentCard(
                agent_name=AgentName(self.agent_name),
                status=self.status,
            )
        ]
        for peer in sorted(self._peers.values(), key=lambda p: p.name):
            cards.append(
                AgentCard(
                    agent_name=AgentName(peer.name),
                    status=peer.status,
                )
            )
        return cards

    def complete_task(
        self,
        task_id: str,
        result: str = "",
        cancelled: bool = False,
    ) -> str:
        """Resolve an inbound task's bridge (backs the ``task_response`` send)."""
        return self._responder.resolve(task_id, result, cancelled)

    # ── Inbound task bookkeeping ────────────────────────────────────────

    def _note_inbound(self, task_id: str, peer: str) -> None:
        """An inbound task is now in flight in this process."""
        self._inbound.add(task_id, peer)

    def _note_withdrawn(self, task_id: str, peer: str) -> None:
        """The peer withdrew this task — remember who, for the late attempt.

        The agent learns of the withdrawal only as a delivered message, so the
        model may still try a ``task_response`` for the id it was handed;
        :meth:`_unresolvable` uses this note to answer truthfully instead of
        blaming the marker.
        """
        self._withdrawn[task_id] = peer

    def _drop_inbound(self, task_id: str) -> None:
        """The task left the responder (answered, cancelled, or the SDK
        cancelled it) — it is no longer awaiting anything from us."""
        self._inbound.drop(task_id)

    def _unresolvable(self, task_id: str) -> str:
        """Why a completion attempt found no bridge to resolve.

        An id this process never registered is one of three very different
        things, and they need different answers.  A *typo* means re-read the
        marker.  A task **orphaned by a restart** can never be completed by
        anyone: its bridge died with the process that held it, and so did the
        peer's reply topic — which is read off the inbound request's MQTT
        properties and is not reconstructible.  The only reply that peer can
        still receive is a plain message, so say exactly that instead of
        leaving the model to find the fallback by trial and error.  A task
        **withdrawn by its sender** is the third: the id is real and the model
        was handed it, so "check the marker" would send it re-reading a marker
        that is correct.
        """
        orphan = self._inbound.stale_entry(task_id)
        if orphan is not None:
            return (
                f"Error: task_id {task_id!r} arrived before this process "
                f"started — its reply path died with the previous one, so no "
                f"result can complete it now. Answer {orphan.peer} with "
                f"message_type='message' instead (drop this task_id)."
            )
        withdrawn_by = self._withdrawn.get(task_id)
        if withdrawn_by is not None:
            return (
                f"Error: task_id {task_id!r} was withdrawn by {withdrawn_by} "
                f"— its completion bridge is gone, so a task_response can no "
                f"longer complete it."
            )
        return (
            f"Error: unknown task_id {task_id!r} — no inbound task with that "
            f"id is awaiting a result. Check the [A2A:...] marker's task_id."
        )

    def stale_inbound(self) -> list[dict]:
        """Inbound tasks orphaned by a restart, oldest first — announced to the
        model until their peers are answered."""
        return [t.as_dict() for t in self._inbound.stale()]
