"""A2A-over-MQTT responder with interrupted-task continuation.

Derived from ``a2a_over_mqtt.responder`` 0.1.0 (Apache-2.0, EMQX;
https://github.com/emqx/mqtt-for-ai).  The licence text is beside this file
(``LICENSE.a2a-over-mqtt``).  The upstream lifecycle is kept where it still
fits: the retained card and LWT, validation, the request loop, and the
per-request stream.  The divergences are the ones the A2A-over-MQTT profile
needs and upstream does not have:

1. A task may end a round ``input-required`` or ``auth-required``.  That is
   stream-final but **not terminal**: no artifact and no terminal status are
   published, and the state stays ``interrupted``.
2. A new ``SendMessage`` with the same ``Task.id`` and ``context_id`` continues
   an interrupted task.  A retry (the same ``messageId``, or the same text when
   a peer does not send one) replays the current state instead of running the
   handler again.  The upstream ``Task.id``-only dedup cannot tell the two
   apart, which is why it is replaced.
3. A terminal task accepts no new message: a new ``messageId`` is refused with
   ``-32602``; a retry replays the stored artifact + terminal status.
4. ``CancelTask`` on an interrupted task is a withdrawal (there is no in-flight
   round to cancel), and a working task publishes exactly one ``canceled``.
5. ``GetTask`` answers with the current task object; upstream fell through to
   SendMessage validation and answered ``-32005``.
6. The socket payload is validated as an object before ``.get`` is called.

The state table is :class:`InboundStore`, owned by the mesh, so the harness and
the responder read one answer to "what is this task now".
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import aiomqtt
from a2a_over_mqtt.mqtt import (
    MqttConfig,
    get_correlation_data,
    get_response_topic,
    make_properties,
)
from a2a_over_mqtt.protocol import (
    A2A_INVALID_PARAMS,
    A2A_PARSE_ERROR,
    A2A_TRANSPORT_PROTOCOL_ERROR,
    A2ARequest,
    A2AResponse,
    ValidationError,
    make_a2a_error,
    make_artifact_event,
    make_status_event,
    validate_a2a_request,
)
from a2a_over_mqtt.topics import TopicSpace
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

import slife2.helpers.timeouts as _T
from slife2.plugins.a2a.inbound_store import InboundStore, InboundTask
from slife2.plugins.a2a.states import (
    CANCELLED,
    COMPLETED,
    FAILED,
    INTERRUPTED,
    REJECTED,
    TERMINAL_STATUSES,
    WORKING,
    user_property,
)

log = logging.getLogger("slife2.plugins.a2a.responder")

#: The two interrupted states, as the task store/display spell them.  The wire
#: spells them ``input-required``/``auth-required``; `_WIRE_STATE` is the one
#: translation table.
INPUT_REQUIRED = "input_required"
AUTH_REQUIRED = "auth_required"

#: A JSON-RPC method this responder does not serve.  The SDK defines the
#: transport's -32003..-32005; `-32601` is the core JSON-RPC "method not found".
A2A_METHOD_NOT_FOUND = -32601

#: A2A core's TaskNotFoundError.  The profile does not override it.
A2A_TASK_NOT_FOUND = -32001

#: Internal outcome -> the value `make_status_event` accepts.  The store's
#: `cancelled` has two l's; the wire's `canceled` has one.
_WIRE_STATE = {
    WORKING: "working",
    COMPLETED: "completed",
    FAILED: "failed",
    CANCELLED: "canceled",
    REJECTED: "rejected",
    INPUT_REQUIRED: "input-required",
    AUTH_REQUIRED: "auth-required",
}

#: Internal outcome -> the proto enum name `GetTask` reports.
_PROTO_STATE = {
    WORKING: "TASK_STATE_WORKING",
    COMPLETED: "TASK_STATE_COMPLETED",
    FAILED: "TASK_STATE_FAILED",
    CANCELLED: "TASK_STATE_CANCELED",
    REJECTED: "TASK_STATE_REJECTED",
    INPUT_REQUIRED: "TASK_STATE_INPUT_REQUIRED",
    AUTH_REQUIRED: "TASK_STATE_AUTH_REQUIRED",
}


@dataclass(frozen=True)
class RoundOutcome:
    """What one round of a task ended as.

    ``state`` is one of the module's outcome words; ``text`` is the artifact
    for `completed`, the reason for `failed`/`rejected`, and the question for
    `input_required`/`auth_required`.
    """

    state: str
    text: str = ""


class StreamCallback(Protocol):
    """Progress frames for the round's current correlation."""

    async def __call__(self, message: str, *, type: str = "text") -> None: ...


def _raw_message(data: dict) -> tuple[str, str, str]:
    """``(messageId, text, contextId)`` from a raw SendMessage payload.

    `A2ARequest.from_json` deliberately discards ``messageId`` (the profile's
    SDK does not need it), but the retry/continuation decision does: a retry
    republishes the same serialized payload, so its id is stable, while a
    continuation is a new Message with a new id.
    """
    params = data.get("params")
    if not isinstance(params, dict):
        params = {}
    message = params.get("message")
    if not isinstance(message, dict):
        message = {}
    message_id = message.get("messageId")
    context_id = message.get("contextId")
    parts = message.get("parts")
    text = ""
    if isinstance(parts, list) and parts and isinstance(parts[0], dict):
        value = parts[0].get("text")
        text = value if isinstance(value, str) else ""
    return (
        message_id if isinstance(message_id, str) else "",
        text,
        context_id if isinstance(context_id, str) else "",
    )


def _variables(data: dict) -> dict:
    params = data.get("params")
    if not isinstance(params, dict):
        return {}
    metadata = params.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    variables = metadata.get("variables")
    return variables if isinstance(variables, dict) else {}


def _request_id(data: dict) -> str:
    value = data.get("id")
    return value if isinstance(value, str) else ""


def _status_message(text: str) -> dict:
    return {
        "messageId": uuid.uuid4().hex,
        "role": "ROLE_AGENT",
        "parts": [{"text": text}],
    }


def _wire_outcome(record: InboundTask) -> str:
    """The outcome word for a record: its interrupt subtype while interrupted."""
    return record.interrupt if record.state == INTERRUPTED else record.state


class Responder(abc.ABC):
    """Base class for the plugin's agents.

    Subclass and implement :meth:`on_request`; call :meth:`run`.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        mqtt: MqttConfig,
        topics: TopicSpace,
        card: dict,
        inbound: InboundStore,
        max_concurrent: int = 4,
    ):
        self._agent_id = agent_id
        self._mqtt = mqtt
        self._topics = topics
        self._card = card
        self._inbound = inbound
        self._semaphore = asyncio.Semaphore(max_concurrent)
        #: One in-flight round per task.  An interrupted task has no entry:
        #: its state lives in the store, not in a coroutine.
        self._rounds: dict[str, asyncio.Task] = {}
        self._stopping = False

    @abc.abstractmethod
    async def on_request(
        self,
        request: A2ARequest,
        stream: StreamCallback,
        *,
        continuation: bool = False,
    ) -> RoundOutcome:
        """Handle one round.  ``continuation`` is true for a resumed task."""

    async def on_cancel(self, task_id: str, peer: str) -> None:  # noqa: B027 - a no-op default hook, not an abstract obligation
        """A peer withdrew an interrupted task; the harness may want to know."""

    async def withdraw_inbound(self, task_id: str) -> bool:
        """Settle an inbound task whose sender no longer exists.

        **The receiver's half of a removal, and the half a session drop cannot
        reach.**  The tasks a removed member *sent* are filed here, in the
        inbound tables of the members that survived it.  Its reply topic is
        gone, so no answer can ever be published for them, and a live round
        keeps a responder slot waiting on a completion nobody can send.  The
        task ends `cancelled`, and the work is told through `on_cancel` — the
        same hook a peer's withdrawal uses, because from this side that is what
        happened.

        A bare message is not a task: its delivery settles it, so it is left
        alone.  There is no status to publish — there is nobody left to read
        one — which is the whole of the difference from `CancelTask`.

        Returns:
            Whether there was an open task to settle.
        """
        record = self._inbound.get(task_id)
        if record is None or record.state in TERMINAL_STATUSES:
            return False
        if record.kind != "task":
            return False
        self._inbound.terminal(task_id, CANCELLED, "")
        round_task = self._rounds.get(task_id)
        if round_task is not None:
            round_task.cancel()
        else:
            await self.on_cancel(task_id, record.peer)
        return True

    # ── Connection ───────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect, publish the card, and serve requests until cancelled."""
        self._stopping = False
        card_json = json.dumps(self._card)

        lwt_props = Properties(PacketTypes.WILLMESSAGE)
        lwt_props.UserProperty = [
            ("a2a-status", "offline"),
            ("a2a-status-source", "lwt"),
        ]
        will = aiomqtt.Will(
            topic=self._topics.discovery(self._agent_id),
            payload=card_json,
            qos=1,
            retain=True,
            properties=lwt_props,
        )

        request_topic = self._topics.request(self._agent_id)
        async with aiomqtt.Client(
            **self._mqtt.client_kwargs(
                identifier=f"{self._topics.org}/{self._topics.unit}/{self._agent_id}",
                will=will,
                # The same bound the probe and the outbound loop carry: a TCP
                # connection that never sends CONNACK would hold the responder
                # for ever, and the member would never resubscribe.
                timeout=_T.timeouts.deliver.mqtt_connect,
            ),
        ) as client:
            try:
                await client.subscribe(request_topic, qos=1)
                online_props = Properties(PacketTypes.PUBLISH)
                online_props.UserProperty = [
                    ("a2a-status", "online"),
                    ("a2a-status-source", "agent"),
                ]
                await client.publish(
                    self._topics.discovery(self._agent_id),
                    card_json,
                    qos=1,
                    retain=True,
                    properties=online_props,
                )
                log.info("Listening on %s", request_topic)
                async for mqtt_msg in client.messages:
                    try:
                        await self.dispatch(client, mqtt_msg)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - one bad frame must not end the connection; the loop is the recovery
                        log.exception("A2A responder dispatch failed")
            finally:
                self._stopping = True
                rounds = list(self._rounds.values())
                for task in rounds:
                    task.cancel()
                if rounds:
                    await asyncio.gather(*rounds, return_exceptions=True)
                self._rounds.clear()
                # A working round's bridge died with the connection.  An
                # interrupted or terminal task keeps its state: the peer can
                # still continue it, and a retry replays the last answer.
                self._inbound.forget_working()
                try:
                    offline_props = Properties(PacketTypes.PUBLISH)
                    offline_props.UserProperty = [
                        ("a2a-status", "offline"),
                        ("a2a-status-source", "agent"),
                    ]
                    await client.publish(
                        self._topics.discovery(self._agent_id),
                        card_json,
                        qos=1,
                        retain=True,
                        properties=offline_props,
                    )
                except Exception:  # noqa: BLE001 - a departing card is best effort; the LWT is the backstop
                    pass

    # ── Dispatch ─────────────────────────────────────────────────────────

    async def dispatch(self, client, mqtt_msg) -> None:
        """One inbound MQTT message.  Pure enough to drive with a fake client."""
        self._inbound.prune(_T.timeouts.pacing.a2a_task_ttl)
        try:
            payload = mqtt_msg.payload.decode() if mqtt_msg.payload else ""
        except (UnicodeDecodeError, AttributeError):
            payload = ""
        if not payload:
            return

        response_topic = get_response_topic(mqtt_msg) or ""
        correlation = get_correlation_data(mqtt_msg) or ""

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            if response_topic and correlation:
                await self._send_error(
                    client,
                    response_topic,
                    correlation,
                    A2A_PARSE_ERROR,
                    f"Parse error: {exc}",
                )
            return
        if not isinstance(data, dict):
            if response_topic and correlation:
                await self._send_error(
                    client,
                    response_topic,
                    correlation,
                    A2A_PARSE_ERROR,
                    "Parse error: payload is not an object",
                )
            return

        method = data.get("method", "")
        if method == "CancelTask":
            await self._on_cancel(client, data, response_topic, correlation)
            return
        if method == "GetTask":
            await self._on_get_task(client, data, response_topic, correlation)
            return
        if method != "SendMessage":
            if response_topic and correlation:
                await self._send_error(
                    client,
                    response_topic,
                    correlation,
                    A2A_METHOD_NOT_FOUND,
                    f"Unknown method: {method!r}",
                )
            return
        await self._on_send_message(client, data, response_topic, correlation, mqtt_msg)

    async def _on_send_message(
        self,
        client,
        data: dict,
        response_topic: str,
        correlation: str,
        mqtt_msg,
    ) -> None:
        request_id = _request_id(data)
        if not response_topic or not correlation:
            log.warning("SendMessage missing MQTT v5 properties, ignoring")
            return

        result = validate_a2a_request(data, response_topic, correlation)
        if isinstance(result, ValidationError):
            await self._send_error(
                client, response_topic, correlation, result.code, result.message
            )
            return
        req = result
        message_id, text, context_id = _raw_message(data)
        message_type = str(_variables(data).get("message_type") or "task_request")

        # The profile's binding property is advisory for transport visibility;
        # when both the property and the JSON are present they must agree.
        prop_context = user_property(
            getattr(mqtt_msg, "properties", None), "a2a-context-id"
        )
        if prop_context and context_id and prop_context != context_id:
            await self._send_error(
                client,
                response_topic,
                correlation,
                A2A_TRANSPORT_PROTOCOL_ERROR,
                "a2a-context-id does not match Message.context_id",
            )
            return

        # `validate_a2a_request` refuses an empty Task.id; the `or ""` is for
        # the type, not for a fall-through.
        task_id = req.task_id or ""
        peer = req.sender or "unknown"

        if message_type == "task_response":
            # **A response-typed frame on the request topic is not a new task.**
            # An id this process never held is acknowledged empty, as upstream
            # does, and remembered so a duplicate is answered the same way.  An
            # id it *does* hold is answered with that task's own state and never
            # with `completed`: the frame is not the task's answer, and a
            # terminal published for a task that is still `working` settles the
            # requester's send — which forgets every routing entry for the task,
            # so the real artifact, when it comes, has nowhere to go.
            record = self._inbound.get(task_id)
            if record is None:
                self._inbound.add(
                    task_id,
                    peer,
                    context_id=context_id,
                    kind="message",
                    message_id=message_id,
                    text=text,
                )
                self._inbound.terminal(task_id, COMPLETED, "")
                await self._publish_status(
                    client,
                    response_topic,
                    correlation,
                    request_id,
                    task_id,
                    COMPLETED,
                    "",
                    context_id,
                )
                return
            await self._replay_status(
                client, response_topic, correlation, request_id, record
            )
            return

        record = self._inbound.get(task_id)
        if record is None:
            if message_type == "task_input":
                await self._send_error(
                    client,
                    response_topic,
                    correlation,
                    A2A_TASK_NOT_FOUND,
                    f"Task not found: {task_id}",
                )
                return
            record = self._inbound.add(
                task_id,
                peer,
                context_id=context_id,
                kind="task" if message_type != "message" else "message",
                message_id=message_id,
                text=text,
            )
            self._start_round(
                client,
                req,
                response_topic,
                correlation,
                record,
                continuation=False,
            )
            return

        if record.context_id and context_id and record.context_id != context_id:
            await self._send_error(
                client,
                response_topic,
                correlation,
                A2A_INVALID_PARAMS,
                "context_id does not match the task's stored context",
            )
            return

        duplicate = bool(message_id and record.seen(message_id))
        if not message_id:
            # **No id: the task's LAST text is the retry's fingerprint**
            # (SDD Appendix C).  Any of the remembered texts would swallow a
            # continuation that repeats an older round's words as a retry of
            # that round; only the last one is what a re-send of the current
            # request repeats.  The comparison cannot tell a re-minted retry
            # from a continuation that repeats the text — the distinction the
            # register gives up — so the fallback says it fired: a debug line
            # naming the peer and the task.
            duplicate = bool(text) and text == record.last_text()
            if duplicate:
                log.debug("a2a_retry_by_text task=%s peer=%s", task_id, peer)

        if record.state in TERMINAL_STATUSES:
            if duplicate:
                await self._replay(
                    client, response_topic, correlation, request_id, record
                )
            else:
                await self._send_error(
                    client,
                    response_topic,
                    correlation,
                    A2A_INVALID_PARAMS,
                    f"Task already ended ({record.state}) and accepts no "
                    f"further messages",
                )
            return

        if record.state == WORKING:
            # **A retry replays working; a new message is refused.**  The
            # retry is the same `messageId` — this build's requester always
            # stamps one and its ladder republishes the identical payload —
            # or, for a peer that sends none, the last-text comparison above
            # (SDD Appendix C).  A new id with repeated text is a new message,
            # not a retry: the register's text fallback is the no-id case.
            if duplicate:
                await self._publish_status(
                    client,
                    response_topic,
                    correlation,
                    request_id,
                    task_id,
                    WORKING,
                    "",
                    record.context_id,
                )
            else:
                await self._send_error(
                    client,
                    response_topic,
                    correlation,
                    A2A_INVALID_PARAMS,
                    "Task is still in progress",
                )
            return

        # Interrupted.  A duplicate of the request that produced the question
        # replays it (the requester missed the interruption); a new message is
        # the continuation.
        if duplicate:
            await self._publish_status(
                client,
                response_topic,
                correlation,
                request_id,
                task_id,
                record.interrupt,
                record.question,
                record.context_id,
            )
            return

        self._inbound.begin_round(task_id, message_id, text)
        record = self._inbound.get(task_id)
        if record is not None:
            self._start_round(
                client,
                req,
                response_topic,
                correlation,
                record,
                continuation=True,
            )

    # ── One round ────────────────────────────────────────────────────────

    def _start_round(
        self,
        client,
        req: A2ARequest,
        response_topic: str,
        correlation: str,
        record: InboundTask,
        *,
        continuation: bool,
    ) -> None:
        task_id = record.task_id
        old = self._rounds.pop(task_id, None)
        if old is not None:
            old.cancel()
        task = asyncio.create_task(
            self._round(
                client,
                req,
                response_topic,
                correlation,
                record,
                continuation,
            )
        )
        self._rounds[task_id] = task

        def _done(t: asyncio.Task, tid: str = task_id) -> None:
            if self._rounds.get(tid) is t:
                self._rounds.pop(tid, None)

        task.add_done_callback(_done)

    async def _round(
        self,
        client,
        req: A2ARequest,
        reply_topic: str,
        correlation: str,
        record: InboundTask,
        continuation: bool,
    ) -> None:
        request_id = req.request_id
        task_id = record.task_id
        context_id = record.context_id

        def ours() -> InboundTask | None:
            """This round's record, while the store still holds it.

            **A record can be evicted and replaced while its round runs.**  The
            store prunes past its cap, and a profile retry of the same task id
            then builds a fresh record and cancels the old round — so an ending
            recorded against "the task with this id" would land on the *new*
            record, publish its terminal state, and the new round would finish
            into a state that is already terminal and drop its answer.  Identity
            is what makes an ending this round's own.
            """
            current = self._inbound.get(task_id)
            return current if current is record else None

        async def stream(message: str, *, type: str = "text") -> None:
            await self._publish_status(
                client,
                reply_topic,
                correlation,
                request_id,
                task_id,
                WORKING,
                message,
                context_id,
                metadata={"type": type},
            )

        try:
            await self._publish_status(
                client,
                reply_topic,
                correlation,
                request_id,
                task_id,
                WORKING,
                "",
                context_id,
                submitted=True,
            )
            async with self._semaphore:
                outcome = await self.on_request(req, stream, continuation=continuation)
        except asyncio.CancelledError:
            # **A cancellation by a replacement round is not a withdrawal.**
            # `_start_round` pops the old round and registers the new one before
            # cancelling the old, so a round that is no longer the registered
            # one was cancelled by its successor — not by the peer.  Recording
            # `cancelled` here would end the task the successor is working on:
            # its answer is then dropped as terminal, and the requester gets a
            # spurious cancel instead of its continuation's result.
            me = asyncio.current_task()
            if self._rounds.get(task_id) is not me:
                raise
            current = ours()
            if (
                current is not None
                and current.state not in TERMINAL_STATUSES
                and not self._stopping
            ):
                self._inbound.terminal(task_id, CANCELLED, "")
                await self._publish_status(
                    client,
                    reply_topic,
                    correlation,
                    request_id,
                    task_id,
                    CANCELLED,
                    "Task canceled",
                    context_id,
                )
            raise
        except Exception:  # noqa: BLE001 - a handler failure is a task failure, and the peer is owed the state
            log.exception("A2A round failed for task %s", task_id)
            current = ours()
            if current is None:
                return  # this round's record was evicted; its retry owns the task
            if current.state in TERMINAL_STATUSES:
                return  # a withdrawal already ended it
            self._inbound.terminal(task_id, FAILED, "Internal error")
            await self._publish_status(
                client,
                reply_topic,
                correlation,
                request_id,
                task_id,
                FAILED,
                "Internal error",
                context_id,
            )
            return

        # A withdrawal may have landed while the model was still working; the
        # cancel path already recorded the terminal state and published it.  A
        # record that is gone or replaced is the other ending: this round no
        # longer speaks for the task, and its answer must not settle somebody
        # else's round.
        current = ours()
        if current is None or current.state in TERMINAL_STATUSES:
            return

        if outcome.state == COMPLETED:
            self._inbound.terminal(task_id, COMPLETED, outcome.text)
            if outcome.text:
                artifact = make_artifact_event(
                    request_id,
                    task_id,
                    outcome.text,
                    context_id,
                )
                await self._publish(client, reply_topic, correlation, artifact)
            await self._publish_status(
                client,
                reply_topic,
                correlation,
                request_id,
                task_id,
                COMPLETED,
                "",
                context_id,
            )
            return
        if outcome.state in (INPUT_REQUIRED, AUTH_REQUIRED):
            self._inbound.interrupt(task_id, outcome.state, outcome.text)
            await self._publish_status(
                client,
                reply_topic,
                correlation,
                request_id,
                task_id,
                outcome.state,
                outcome.text,
                context_id,
            )
            return
        if outcome.state in (FAILED, REJECTED, CANCELLED):
            self._inbound.terminal(task_id, outcome.state, outcome.text)
            await self._publish_status(
                client,
                reply_topic,
                correlation,
                request_id,
                task_id,
                outcome.state,
                outcome.text,
                context_id,
            )
            return
        # An unknown outcome is a handler bug; end the task rather than leave
        # the peer waiting forever.
        self._inbound.terminal(task_id, FAILED, "Internal error")
        await self._publish_status(
            client,
            reply_topic,
            correlation,
            request_id,
            task_id,
            FAILED,
            "Internal error",
            context_id,
        )

    # ── Cancel / GetTask ────────────────────────────────────────────────

    async def _on_cancel(
        self,
        client,
        data: dict,
        response_topic: str,
        correlation: str,
    ) -> None:
        if not response_topic or not correlation:
            log.warning("CancelTask missing MQTT v5 properties, ignoring")
            return
        params = data.get("params")
        if not isinstance(params, dict):
            params = {}
        task_id = params.get("id")
        task_id = task_id if isinstance(task_id, str) else ""
        request_id = _request_id(data)
        record = self._inbound.get(task_id)
        if record is None:
            await self._send_error(
                client,
                response_topic,
                correlation,
                A2A_TASK_NOT_FOUND,
                f"Task not found: {task_id}",
            )
            return
        if record.state in TERMINAL_STATUSES:
            await self._replay_status(
                client,
                response_topic,
                correlation,
                request_id,
                record,
            )
            return
        self._inbound.terminal(task_id, CANCELLED, "")
        # **The round stays registered while it is stopped.**  The handler that
        # unwinds it asks this table whether the round it is cancelling is still
        # the one registered — that is how a peer's withdrawal is told from a
        # retry's replacement (`MeshResponder.on_request`) — and popping the id
        # here made the peer path answer "no" about itself, so a working task's
        # withdrawal was never handed to the agent.  The done callback is what
        # removes the entry, once the round has actually finished.
        round_task = self._rounds.get(task_id)
        if round_task is not None:
            round_task.cancel()
        else:
            await self.on_cancel(task_id, record.peer)
        await self._publish_status(
            client,
            response_topic,
            correlation,
            request_id,
            task_id,
            CANCELLED,
            "Task canceled",
            record.context_id,
        )

    async def _on_get_task(
        self,
        client,
        data: dict,
        response_topic: str,
        correlation: str,
    ) -> None:
        if not response_topic or not correlation:
            log.warning("GetTask missing MQTT v5 properties, ignoring")
            return
        params = data.get("params")
        if not isinstance(params, dict):
            params = {}
        task_id = params.get("id")
        task_id = task_id if isinstance(task_id, str) else ""
        request_id = _request_id(data)
        record = self._inbound.get(task_id)
        if record is None:
            await self._send_error(
                client,
                response_topic,
                correlation,
                A2A_TASK_NOT_FOUND,
                f"Task not found: {task_id}",
            )
            return
        task = self._task_object(record)
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": {"task": task}}
        )
        await self._publish(client, response_topic, correlation, payload)

    def _task_object(self, record: InboundTask) -> dict:
        state = _wire_outcome(record)
        status: dict = {
            "state": _PROTO_STATE.get(state, "TASK_STATE_UNSPECIFIED"),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        text = (
            record.question
            if state in (INPUT_REQUIRED, AUTH_REQUIRED)
            else record.result
        )
        if text:
            status["message"] = _status_message(text)
        task: dict = {
            "id": record.task_id,
            "contextId": record.context_id,
            "status": status,
        }
        if state == COMPLETED and record.result:
            task["artifacts"] = [
                {
                    "artifactId": uuid.uuid4().hex[:12],
                    "parts": [{"text": record.result}],
                }
            ]
        return task

    # ── Publishing helpers ──────────────────────────────────────────────

    async def _publish(
        self, client, topic: str, correlation: str, payload: str
    ) -> None:
        props = make_properties(correlation_data=correlation)
        await client.publish(topic, payload, qos=1, properties=props)

    async def _publish_status(
        self,
        client,
        topic: str,
        correlation: str,
        request_id: str,
        task_id: str,
        state: str,
        message: str = "",
        context_id: str = "",
        *,
        metadata: dict | None = None,
        submitted: bool = False,
    ) -> None:
        wire_state = "submitted" if submitted else _WIRE_STATE[state]
        payload = make_status_event(
            request_id,
            task_id,
            wire_state,
            message,
            context_id,
            metadata=metadata,
        )
        await self._publish(client, topic, correlation, payload)

    async def _send_error(
        self,
        client,
        response_topic: str,
        correlation: str,
        code: int,
        message: str,
    ) -> None:
        response = A2AResponse(id=correlation, error=make_a2a_error(code, message))
        await self._publish(client, response_topic, correlation, response.to_json())

    async def _replay(
        self,
        client,
        response_topic: str,
        correlation: str,
        request_id: str,
        record: InboundTask,
    ) -> None:
        if record.state == COMPLETED and record.result:
            artifact = make_artifact_event(
                request_id,
                record.task_id,
                record.result,
                record.context_id,
            )
            await self._publish(client, response_topic, correlation, artifact)
        await self._replay_status(
            client, response_topic, correlation, request_id, record
        )

    async def _replay_status(
        self,
        client,
        response_topic: str,
        correlation: str,
        request_id: str,
        record: InboundTask,
    ) -> None:
        state = _wire_outcome(record)
        text = (
            record.question
            if state in (INPUT_REQUIRED, AUTH_REQUIRED)
            else record.result
        )
        await self._publish_status(
            client,
            response_topic,
            correlation,
            request_id,
            record.task_id,
            state,
            text,
            record.context_id,
        )
