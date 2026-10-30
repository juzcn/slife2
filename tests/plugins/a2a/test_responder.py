"""The responder's task state machine, driven without a broker.

`A2AMesh.__init__` dials nothing, and the local responder's `dispatch` takes a
client and one MQTT message.  A recording client and a fabricated message are
therefore enough to exercise every row of the table: a new task, a retry, an
interruption, a continuation, a terminal task, a withdrawal and a GetTask.
The model side is the real `MeshResponder`, resolved through `task_response`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from a2a_over_mqtt import make_properties

from slife2.libs.host.config import A2ASettings
from slife2.plugins.a2a.mesh import A2AMesh

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
    task_id = _new_task()
    await mesh._responder.dispatch(client, _message(_send(task_id, "m1", "do it")))
    await _until(lambda: client.sent, "the submitted ack")

    await mesh._responder.dispatch(client, _cancel(task_id))

    assert _states(client).count("TASK_STATE_CANCELED") == 1
    await _until(
        lambda: mesh._inbound.get(task_id).state == "cancelled",
        "the recorded withdrawal",
    )


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
