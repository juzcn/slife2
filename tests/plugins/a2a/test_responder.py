"""The responder's task state machine, driven without a broker.

`A2AMesh.__init__` dials nothing, and the local responder's `dispatch` takes a
client and one MQTT message.  A recording client and a fabricated message are
therefore enough to exercise every row of the table: a new task, a retry, an
interruption, a continuation, a terminal task, a withdrawal and a GetTask.
The model side is the real `MeshResponder`, resolved through `task_response`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from a2a_over_mqtt import make_properties

from slife2.libs.host.config import A2ASettings
from slife2.plugins.a2a.mesh import A2AMesh, MeshResponder
from slife2.plugins.a2a.responder import Responder

pytestmark = pytest.mark.unit


class RecordingClient:
    """The publish sink the responder talks to."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def publish(
        self,
        topic: str,
        payload: str | None = None,
        qos: int = 0,
        retain: bool = False,
        properties: Any = None,
    ) -> None:
        if payload is not None:
            self.sent.append(payload)


def _message(data: dict, corr: str = "corr-1") -> SimpleNamespace:
    return SimpleNamespace(
        payload=json.dumps(data).encode(),
        properties=make_properties(
            response_topic="a2a/v1/reply/jack/session",
            correlation_data=corr,
        ),
        topic=SimpleNamespace(value="a2a/v1/request/jack"),
    )


def _send(
    task_id: str,
    message_id: str,
    text: str,
    *,
    context_id: str = "ctx-1",
    message_type: str = "task_request",
    request_id: str = "rpc-1",
) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": message_id,
                "role": "ROLE_USER",
                "parts": [{"text": text}],
                "taskId": task_id,
                "contextId": context_id,
            },
            "metadata": {
                "sender": "alice",
                "variables": {"message_type": message_type},
            },
        },
    }


def _without_id(data: dict) -> dict:
    """The same SendMessage with no messageId — the peer the register's
    last-text fallback is for (SDD Appendix C)."""
    del data["params"]["message"]["messageId"]
    return data


def _cancel(task_id: str, corr: str = "cancel-1") -> SimpleNamespace:
    return _message(
        {
            "jsonrpc": "2.0",
            "id": "rpc-cancel",
            "method": "CancelTask",
            "params": {"id": task_id},
        },
        corr,
    )


def _get(task_id: str, corr: str = "get-1") -> SimpleNamespace:
    return _message(
        {
            "jsonrpc": "2.0",
            "id": "rpc-get",
            "method": "GetTask",
            "params": {"id": task_id},
        },
        corr,
    )


def _states(client: RecordingClient) -> list[str]:
    """What each published payload says, in order."""
    out: list[str] = []
    for payload in client.sent:
        data = json.loads(payload)
        result = data.get("result") or {}
        if "statusUpdate" in result:
            out.append(result["statusUpdate"]["status"]["state"])
        elif "artifactUpdate" in result:
            text = result["artifactUpdate"]["artifact"]["parts"][0]["text"]
            out.append(f"artifact:{text}")
        elif "task" in result:
            out.append(result["task"]["status"]["state"])
        elif "error" in data:
            out.append(f"error:{data['error']['code']}")
    return out


def _status_message(client: RecordingClient) -> str:
    """The text of the last status update's message."""
    for payload in reversed(client.sent):
        data = json.loads(payload)
        status = (data.get("result") or {}).get("statusUpdate", {}).get("status", {})
        if status:
            message = status.get("message") or {}
            parts = message.get("parts") or []
            return parts[0].get("text", "") if parts else ""
    return ""


async def _until(predicate, what: str) -> None:
    for _ in range(400):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def mesh() -> A2AMesh:
    return A2AMesh(A2ASettings(configured=True), "jack")


@pytest.fixture
def client() -> RecordingClient:
    return RecordingClient()


async def _settle(mesh: A2AMesh, task_id: str, outcome: str, text: str = "") -> None:
    # The submitted ack is published before `on_request` runs; the completion
    # bridge is what a `task_response` needs.
    await _until(lambda: task_id in mesh._responder._completion, "the round's bridge")
    assert mesh.task_response(task_id, outcome, text) == "ok"
    await asyncio.sleep(0.01)


def _new_task() -> str:
    return uuid.uuid4().hex


def test_the_engine_is_a_plain_base_with_one_implementation() -> None:
    """**The engine is not an ABC ceremony.**  `MeshResponder` is its only
    subclass, and the abstract machinery bought an instantiation check no
    caller ran; the base is a plain class whose `on_request` hook the one
    implementation overrides.
    """
    assert not inspect.isabstract(Responder)
    assert MeshResponder.__mro__[1] is Responder


@pytest.mark.asyncio
async def test_a_response_typed_frame_on_the_request_path_fails_the_round(
    mesh: A2AMesh,
) -> None:
    """**A `task_response` cannot start a round, and must not pretend to finish one.**

    `Responder._on_send_message` answers a response-typed frame before a record
    or a round exists, so this path is closed there.  The old guard answered an
    empty `completed` if a caller ever reached it — a terminal for a task nobody
    answered.  The round fails instead.
    """
    request = SimpleNamespace(
        task_id="task-response-on-the-request-topic",
        sender="alice",
        variables={"message_type": "task_response"},
        text="",
    )

    async def stream(_text: str) -> None:
        return None

    with pytest.raises(RuntimeError, match="task_response frame reached"):
        await mesh._responder.on_request(request, stream)


@pytest.mark.asyncio
async def test_a_round_publishes_submitted_then_the_artifact_and_completed(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    task_id = _new_task()
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "do it")))
    await _until(lambda: client.sent, "the submitted ack")
    assert mesh._inbound.get(task_id).state == "working"

    await _settle(mesh, task_id, "completed", "done")

    assert _states(client) == [
        "TASK_STATE_SUBMITTED",
        "artifact:done",
        "TASK_STATE_COMPLETED",
    ]
    assert mesh._inbound.get(task_id).state == "completed"


@pytest.mark.asyncio
async def test_an_interrupted_round_publishes_no_terminal_and_keeps_the_task(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    task_id = _new_task()
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "翻译")))
    await _until(lambda: client.sent, "the submitted ack")

    await _settle(mesh, task_id, "input_required", "简体还是繁体?")

    assert _states(client) == [
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_INPUT_REQUIRED",
    ]
    record = mesh._inbound.get(task_id)
    assert record is not None
    assert (record.state, record.interrupt, record.question) == (
        "interrupted",
        "input_required",
        "简体还是繁体?",
    )
    assert _status_message(client) == "简体还是繁体?"


@pytest.mark.asyncio
async def test_a_continuation_is_a_new_round_on_the_same_task(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    task_id = _new_task()
    handed: list[tuple[str, str, str, str]] = []
    mesh.on_inbound_task = lambda source, content, tid, kind="task": handed.append(
        (source, content, tid, kind)
    )
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "翻译")))
    await _settle(mesh, task_id, "input_required", "简体还是繁体?")

    await mesh._responder.dispatch(
        client,
        _message(
            _send(task_id, "m2", "简体", message_type="task_input"),
            corr="corr-2",
        ),
    )
    await _until(
        lambda: len(handed) == 2 and mesh._inbound.get(task_id).round == 2,
        "the continuation's hand-over",
    )

    assert handed[-1][3] == "task_input"
    assert handed[-1][2] == task_id
    assert mesh._inbound.get(task_id).round == 2

    await _settle(mesh, task_id, "completed", "译文")
    assert _states(client)[-2:] == ["artifact:译文", "TASK_STATE_COMPLETED"]


@pytest.mark.asyncio
async def test_a_stray_task_response_does_not_publish_a_terminal_for_a_working_task(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    """**A response-typed frame is not the task's answer**, and it must not end
    the task either.

    The responder acknowledged one empty as `completed` whatever the id's own
    state was.  For an id it was still working on, that `completed` went back to
    the sender — and a requester that took it settles its send and forgets every
    routing entry for the task, so the real artifact, when it came, had nowhere
    to go.  A known id is answered with its own state now; only an id this
    process never held gets the empty acknowledgment.
    """
    task_id = _new_task()
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "do it")))
    await _until(lambda: client.sent, "the submitted ack")
    assert mesh._inbound.get(task_id).state == "working"

    before = len(client.sent)
    await mesh._responder.dispatch(
        client,
        _message(
            _send(task_id, "m2", "a stray answer", message_type="task_response"),
            corr="corr-2",
        ),
    )

    assert _states(client)[before:] == ["TASK_STATE_WORKING"], (
        "a stray response-typed frame published a terminal for a working task"
    )
    assert mesh._inbound.get(task_id).state == "working"

    # And the real round still settles, which is what the bogus terminal broke.
    await _settle(mesh, task_id, "completed", "the real answer")
    assert _states(client)[-2:] == [
        "artifact:the real answer",
        "TASK_STATE_COMPLETED",
    ]


@pytest.mark.asyncio
async def test_a_retried_task_response_does_not_settle_the_next_round(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    """**A client-level retry is the same call re-sent**, not a new answer.

    Round one ends `input_required`; the peer continues the task and round two's
    bridge is waiting.  A retried `task_response` — same task id, same outcome,
    same words — matches what settled round one exactly, and settling round two
    with it would end a task the peer is still working through with the previous
    round's answer.  The bridge is still open afterwards, so the model can give
    the continuation its own answer.
    """
    task_id = _new_task()
    handed: list[tuple[str, str, str, str]] = []
    mesh.on_inbound_task = lambda source, content, tid, kind="task": handed.append(
        (source, content, tid, kind)
    )
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "翻译")))
    await _settle(mesh, task_id, "input_required", "简体还是繁体?")

    await mesh._responder.dispatch(
        client,
        _message(
            _send(task_id, "m2", "简体", message_type="task_input"),
            corr="corr-2",
        ),
    )
    await _until(
        lambda: (
            len(handed) == 2
            and mesh._responder._completion.get(task_id) is not None
            and mesh._responder._completion[task_id].serial == 2
        ),
        "the continuation's bridge",
    )

    refused = mesh.task_response(task_id, "input_required", "简体还是繁体?")
    assert "earlier round" in refused
    assert mesh._responder._completion[task_id].outcome is None, (
        "the retried answer settled the continuation's round"
    )

    assert mesh.task_response(task_id, "completed", "译文") == "ok"
    await asyncio.sleep(0.01)
    assert _states(client)[-2:] == ["artifact:译文", "TASK_STATE_COMPLETED"]


@pytest.mark.asyncio
async def test_a_retry_with_the_same_message_id_replays_working(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    task_id = _new_task()
    handed: list[tuple[str, str, str, str]] = []
    mesh.on_inbound_task = lambda source, content, tid, kind="task": handed.append(
        (source, content, tid, kind)
    )
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "do it")))
    await _until(lambda: handed, "the first hand-over")

    await mesh._responder.dispatch(
        client, _message(_send(task_id, "m1", "do it"), corr="corr-2")
    )
    await _until(lambda: _states(client)[-1] == "TASK_STATE_WORKING", "the replay")

    assert len(handed) == 1, "a retry is not a second turn"


@pytest.mark.asyncio
async def test_a_retry_after_an_evicted_record_owns_the_new_round(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    """**The round that was cancelled must not settle the record that replaced it.**

    The store prunes past its cap, so a live round can lose its record; a retry
    of the same task id then builds a fresh one and cancels the old round.  The
    old round's cancellation used to read "the task with this id" — the *new*
    record — mark it `cancelled` and publish that terminal, after which the new
    round finished into a state that was already terminal and dropped its
    answer.  Identity is what makes the ending the old round's own.
    """
    task_id = _new_task()
    handed: list[tuple[str, str, str, str]] = []
    mesh.on_inbound_task = lambda source, content, tid, kind="task": handed.append(
        (source, content, tid, kind)
    )
    # A live link is what makes a *real* cancellation look like a withdrawal;
    # the replacement must not be reported as one even then.
    mesh._connected = True
    withdrawals: list[tuple[str, str]] = []
    mesh.on_peer_cancel = lambda tid, peer: withdrawals.append((tid, peer))
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "do it")))
    await _until(lambda: handed, "the first hand-over")

    # The cap evicted the live round's record, and the peer retried the same
    # message, which builds a fresh record and cancels the old round.
    mesh._inbound.drop(task_id)
    await mesh._responder.dispatch(
        client, _message(_send(task_id, "m1", "do it"), corr="corr-2")
    )
    await _until(lambda: len(handed) == 2, "the retry's hand-over")
    await asyncio.sleep(0.01)  # the old round's cancellation runs

    record = mesh._inbound.get(task_id)
    assert record is not None and record.state == "working", (
        "the evicted round's cancellation settled the retry's record"
    )
    assert "TASK_STATE_CANCELED" not in _states(client)
    assert withdrawals == [], (
        "the replaced round told the agent its peer had withdrawn the task"
    )

    await _settle(mesh, task_id, "completed", "done")

    assert "artifact:done" in _states(client), "the retry's answer was dropped"
    assert _states(client)[-1] == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
async def test_a_new_message_while_working_is_refused(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    task_id = _new_task()
    handed: list[tuple[str, str, str, str]] = []
    mesh.on_inbound_task = lambda source, content, tid, kind="task": handed.append(
        (source, content, tid, kind)
    )
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "do it")))
    await _until(lambda: handed, "the first hand-over")

    await mesh._responder.dispatch(
        client, _message(_send(task_id, "m2", "something else"), corr="corr-2")
    )
    await _until(lambda: _states(client)[-1].startswith("error:"), "the refusal")

    assert _states(client)[-1] == "error:-32602"
    assert len(handed) == 1


@pytest.mark.asyncio
async def test_a_no_id_retry_of_the_last_text_replays_the_pause(
    mesh: A2AMesh, client: RecordingClient, caplog: pytest.LogCaptureFixture
) -> None:
    """**No messageId: the task's LAST text is the retry's fingerprint**
    (SDD Appendix C).  A peer that sends no id and repeats the request that
    produced the question is a retry — the pause is replayed and no new round
    runs — and the fallback says it fired: a debug line naming the peer and
    the task, the trace of the distinction it cannot make.
    """
    task_id = _new_task()
    handed: list[tuple[str, str, str, str]] = []
    mesh.on_inbound_task = lambda source, content, tid, kind="task": handed.append(
        (source, content, tid, kind)
    )
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "翻译")))
    await _settle(mesh, task_id, "input_required", "简体还是繁体?")

    with caplog.at_level(logging.DEBUG, logger="slife2.plugins.a2a.responder"):
        await mesh._responder.dispatch(
            client,
            _message(_without_id(_send(task_id, "m-unused", "翻译")), corr="corr-2"),
        )

    assert _states(client)[-1] == "TASK_STATE_INPUT_REQUIRED"
    assert _status_message(client) == "简体还是繁体?"
    assert len(handed) == 1, "a retry is not a second round"
    assert mesh._inbound.get(task_id).round == 1
    assert any(
        "a2a_retry_by_text" in record.getMessage()
        and task_id in record.getMessage()
        and "alice" in record.getMessage()
        for record in caplog.records
    ), "the fallback left no trace naming the peer and the task"


@pytest.mark.asyncio
async def test_a_continuation_repeating_an_older_rounds_text_is_a_new_round(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    """**The last text, not any remembered one.**  Round one asked 翻译, round
    two asked 简体; a no-id message repeating round one's words is a
    continuation of the paused task, not a retry of a round two rememberings
    ago.  Matching any of the last 32 texts swallowed it as a retry and
    replayed the pause — the peer's new input never reached the model.
    """
    task_id = _new_task()
    handed: list[tuple[str, str, str, str]] = []
    mesh.on_inbound_task = lambda source, content, tid, kind="task": handed.append(
        (source, content, tid, kind)
    )
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "翻译")))
    await _settle(mesh, task_id, "input_required", "简体还是繁体?")
    await mesh._responder.dispatch(
        client,
        _message(
            _send(task_id, "m2", "简体", message_type="task_input"), corr="corr-2"
        ),
    )
    await _settle(mesh, task_id, "input_required", "用哪个术语?")

    await mesh._responder.dispatch(
        client,
        _message(
            _without_id(_send(task_id, "m3", "翻译", message_type="task_input")),
            corr="corr-3",
        ),
    )
    await _until(lambda: len(handed) == 3, "the continuation's hand-over")

    assert handed[-1][1] == "翻译"
    assert handed[-1][3] == "task_input"
    assert mesh._inbound.get(task_id).round == 3

    await _settle(mesh, task_id, "completed", "译文")
    assert _states(client)[-2:] == ["artifact:译文", "TASK_STATE_COMPLETED"]


@pytest.mark.asyncio
async def test_a_working_task_replays_a_retry_and_refuses_a_repeated_new_message(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    """**The text fallback is the no-id case** (SDD Appendix C).  A peer that
    sends no id and repeats the working request is a retry and gets `working`
    replayed; a new messageId with the same text is a new message, and the
    answer is the refusal any other new message gets — this build's requester
    always stamps an id and its ladder republishes the identical payload, so
    a retry never needs the text to be recognised.
    """
    task_id = _new_task()
    handed: list[tuple[str, str, str, str]] = []
    mesh.on_inbound_task = lambda source, content, tid, kind="task": handed.append(
        (source, content, tid, kind)
    )
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "do it")))
    await _until(lambda: handed, "the first hand-over")

    # No id, same text: the register's retry, replayed as working.
    await mesh._responder.dispatch(
        client,
        _message(_without_id(_send(task_id, "m-unused", "do it")), corr="corr-2"),
    )
    await _until(lambda: _states(client)[-1] == "TASK_STATE_WORKING", "the replay")
    assert len(handed) == 1

    # A new id with the same text is a new message, not a retry.
    await mesh._responder.dispatch(
        client, _message(_send(task_id, "m2", "do it"), corr="corr-3")
    )
    await _until(lambda: _states(client)[-1].startswith("error:"), "the refusal")
    assert _states(client)[-1] == "error:-32602"
    assert len(handed) == 1


@pytest.mark.asyncio
async def test_a_terminal_task_replays_a_retry_and_refuses_a_new_message(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    task_id = _new_task()
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "do it")))
    await _until(lambda: client.sent, "the submitted ack")
    await _settle(mesh, task_id, "completed", "done")

    await mesh._responder.dispatch(
        client, _message(_send(task_id, "m1", "do it"), corr="corr-2")
    )
    await _until(lambda: _states(client)[-1] == "TASK_STATE_COMPLETED", "the replay")
    assert _states(client)[-2:] == ["artifact:done", "TASK_STATE_COMPLETED"]

    await mesh._responder.dispatch(
        client, _message(_send(task_id, "m2", "again"), corr="corr-3")
    )
    await _until(lambda: _states(client)[-1].startswith("error:"), "the refusal")
    assert _states(client)[-1] == "error:-32602"


@pytest.mark.asyncio
async def test_a_context_mismatch_is_refused(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    task_id = _new_task()
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "do it")))
    await _until(lambda: client.sent, "the submitted ack")

    await mesh._responder.dispatch(
        client,
        _message(
            _send(task_id, "m2", "more", context_id="another"),
            corr="corr-2",
        ),
    )
    await _until(lambda: _states(client)[-1].startswith("error:"), "the refusal")
    assert _states(client)[-1] == "error:-32602"


@pytest.mark.asyncio
async def test_cancel_of_an_interrupted_task_publishes_one_canceled(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    task_id = _new_task()
    mesh._connected = True
    withdrawals: list[tuple[str, str]] = []
    mesh.on_peer_cancel = lambda tid, peer: withdrawals.append((tid, peer))
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "翻译")))
    await _until(lambda: client.sent, "the submitted ack")
    await _settle(mesh, task_id, "input_required", "简体还是繁体?")

    await mesh._responder.dispatch(client, _cancel(task_id))

    assert _states(client).count("TASK_STATE_CANCELED") == 1
    assert mesh._inbound.get(task_id).state == "cancelled"
    assert withdrawals == [(task_id, "alice")]


@pytest.mark.asyncio
async def test_cancel_of_a_working_task_publishes_one_canceled(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    """**A working task's withdrawal reaches the agent, and only once.**

    The responder cancels the round, and the round is what tells the agent the
    task is withdrawn (`on_peer_cancel`).  The check that keeps a *replacement*
    round from being read as a withdrawal (`on_request`) keys on the cancelled
    round still being the registered one — so `_on_cancel` must not remove the
    entry before it cancels.  It did, and the guard read the peer's own
    withdrawal as machinery's ending: the work was never told to stop, and the
    maker's `subagent_remove` then found a turn that outlived every request to
    end it.
    """
    task_id = _new_task()
    mesh._connected = True
    withdrawals: list[tuple[str, str]] = []
    mesh.on_peer_cancel = lambda tid, peer: withdrawals.append((tid, peer))
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "do it")))
    await _until(lambda: client.sent, "the submitted ack")

    await mesh._responder.dispatch(client, _cancel(task_id))

    assert _states(client).count("TASK_STATE_CANCELED") == 1
    await _until(
        lambda: mesh._inbound.get(task_id).state == "cancelled",
        "the recorded withdrawal",
    )
    await _until(lambda: withdrawals, "the agent being told to stop")
    assert withdrawals == [(task_id, "alice")]


@pytest.mark.asyncio
async def test_get_task_reports_the_interrupted_state_and_question(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    task_id = _new_task()
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "翻译")))
    await _until(lambda: client.sent, "the submitted ack")
    await _settle(mesh, task_id, "input_required", "简体还是繁体?")

    await mesh._responder.dispatch(client, _get(task_id))
    await _until(lambda: _states(client)[-1].startswith("TASK_STATE_"), "the task")

    assert _states(client)[-1] == "TASK_STATE_INPUT_REQUIRED"
    data = json.loads(client.sent[-1])
    task = data["result"]["task"]
    assert task["id"] == task_id
    assert task["contextId"] == "ctx-1"
    assert task["status"]["message"]["parts"][0]["text"] == "简体还是繁体?"


@pytest.mark.asyncio
async def test_get_task_for_an_unknown_task_is_task_not_found(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    await mesh._responder.dispatch(client, _get(_new_task()))
    await _until(lambda: client.sent, "the refusal")
    assert _states(client)[-1] == "error:-32001"


@pytest.mark.asyncio
async def test_a_non_object_payload_is_a_parse_error(
    mesh: A2AMesh, client: RecordingClient
) -> None:
    message = _message([])  # a JSON array, not an object
    await mesh._responder.dispatch(client, message)
    assert _states(client) == ["error:-32700"]


def test_teardown_drops_working_rounds_and_keeps_interrupted_ones(
    mesh: A2AMesh,
) -> None:
    working = _new_task()
    interrupted = _new_task()
    mesh._inbound.add(working, "alice")
    mesh._inbound.add(interrupted, "alice")
    mesh._inbound.interrupt(interrupted, "input_required", "more?")

    mesh._inbound.forget_working()

    assert mesh._inbound.get(working) is None
    assert mesh._inbound.get(interrupted).state == "interrupted"
