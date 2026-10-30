"""The outbound continuation: pause, keep, resume, settle.

The mesh's `_handle_reply` takes one fabricated MQTT message, and the ladder's
first publish goes through `_publish_props`.  Patching that one method is
enough to drive a task from its send through an interruption, a continuation,
a stale round and a terminal — without a broker.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from a2a_over_mqtt import make_properties

import slife2.helpers.timeouts as _T
from slife2.libs.host.config import A2ASettings
from slife2.plugins.a2a.mesh import (
    AUTH_REQUIRED,
    COMPLETED,
    INPUT_REQUIRED,
    REJECTED,
    A2AMesh,
)
from slife2.plugins.a2a.task_store import TaskStore, get_store

pytestmark = pytest.mark.unit


def _message(data: dict, corr: str) -> SimpleNamespace:
    return SimpleNamespace(
        payload=json.dumps(data).encode(),
        properties=make_properties(
            response_topic="a2a/v1/reply/jack/session",
            correlation_data=corr,
        ),
        topic=SimpleNamespace(value="a2a/v1/reply/jack/session"),
    )


def _working(corr: str, text: str = "") -> SimpleNamespace:
    return _message(
        {
            "result": {
                "statusUpdate": {
                    "status": {
                        "state": "TASK_STATE_WORKING",
                        "message": {"parts": [{"text": text}]} if text else {},
                    }
                }
            }
        },
        corr,
    )


def _interrupted(corr: str, text: str) -> SimpleNamespace:
    return _message(
        {
            "result": {
                "statusUpdate": {
                    "status": {
                        "state": "TASK_STATE_INPUT_REQUIRED",
                        "message": {"parts": [{"text": text}]},
                    }
                }
            }
        },
        corr,
    )


def _completed(corr: str, text: str) -> tuple[SimpleNamespace, SimpleNamespace]:
    artifact = _message(
        {
            "result": {
                "artifactUpdate": {
                    "artifact": {"parts": [{"text": text}]},
                }
            }
        },
        corr,
    )
    terminal = _message(
        {"result": {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}}},
        corr,
    )
    return artifact, terminal


def _rejected(corr: str, text: str) -> SimpleNamespace:
    return _message(
        {
            "result": {
                "statusUpdate": {
                    "status": {
                        "state": "TASK_STATE_REJECTED",
                        "message": {"parts": [{"text": text}]},
                    }
                }
            }
        },
        corr,
    )


def _direct_message(corr: str, text: str) -> SimpleNamespace:
    return _message(
        {
            "result": {
                "message": {
                    "role": "ROLE_AGENT",
                    "parts": [{"text": text}],
                }
            }
        },
        corr,
    )


def _error(corr: str, code: int, text: str) -> SimpleNamespace:
    return _message(
        {"jsonrpc": "2.0", "id": "rpc", "error": {"code": code, "message": text}},
        corr,
    )


def _corr(mesh: A2AMesh, task_id: str, round_no: int) -> str:
    for corr, route in mesh._corr_to_task.items():
        if route == (task_id, round_no):
            return corr
    raise AssertionError(f"no correlation for {task_id} round {round_no}")


@pytest_asyncio.fixture
async def mesh(monkeypatch: pytest.MonkeyPatch):
    m = A2AMesh(A2ASettings(configured=True), "jack")
    sent: list[tuple[str, str, Any]] = []

    async def publish(topic: str, payload: str, props: Any, *, qos: int = 1) -> None:
        sent.append((topic, payload, props))

    done: list[tuple[Any, ...]] = []
    monkeypatch.setattr(m, "_publish_props", publish)
    monkeypatch.setattr(m, "on_task_completion", lambda *args: done.append(args))
    yield m, sent, done
    for send in list(m._sends.values()):
        for loop in list(send.loops):
            loop.cancel()
        if send.watchdog is not None:
            send.watchdog.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_an_interrupted_reply_keeps_the_send_and_the_question(
    mesh,
) -> None:
    m, _sent, done = mesh
    task_id = await m.send_message("bob", "翻译 report.pdf")
    send = m._sends[task_id]
    corr = _corr(m, task_id, 1)

    m._handle_reply(_interrupted(corr, "简体还是繁体?"))

    assert get_store().get(task_id).status == "interrupted"
    assert task_id in m._sends, "the send is the continuation's context"
    assert (send.interrupt, send.question) == (INPUT_REQUIRED, "简体还是繁体?")
    assert done[-1][0] == task_id and done[-1][2] == INPUT_REQUIRED
    assert corr not in m._corr_to_task, "the interrupted round's route is spent"


@pytest.mark.asyncio
async def test_a_continuation_reuses_the_task_and_context_with_a_new_message(
    mesh,
) -> None:
    m, _sent, _done = mesh
    task_id = await m.send_message("bob", "翻译 report.pdf")
    send = m._sends[task_id]
    corr1 = _corr(m, task_id, 1)
    m._handle_reply(_interrupted(corr1, "简体还是繁体?"))

    first = json.loads(send.payload)["params"]["message"]
    assert await m.continue_task("bob", task_id, "简体") == "ok"

    second = json.loads(send.payload)["params"]["message"]
    assert second["taskId"] == task_id
    assert second["contextId"] == first["contextId"]
    assert second["messageId"] != first["messageId"], "a continuation is a new Message"
    variables = json.loads(send.payload)["params"]["metadata"]["variables"]
    assert variables["message_type"] == "task_input"
    assert (send.round, send.attempts, send.interrupt) == (2, 1, "")
    assert get_store().get(task_id).status == "pending"
    assert _corr(m, task_id, 2) in m._corr_to_task
    assert corr1 not in m._corr_to_task


@pytest.mark.asyncio
async def test_a_late_terminal_from_the_interrupted_round_is_dropped(mesh) -> None:
    m, _sent, _done = mesh
    task_id = await m.send_message("bob", "翻译 report.pdf")
    corr1 = _corr(m, task_id, 1)
    m._handle_reply(_interrupted(corr1, "简体还是繁体?"))
    assert await m.continue_task("bob", task_id, "简体") == "ok"
    corr2 = _corr(m, task_id, 2)

    old_artifact, old_terminal = _completed(corr1, "stale")
    m._handle_reply(old_artifact)
    m._handle_reply(old_terminal)

    assert get_store().get(task_id).status == "pending"
    assert task_id in m._sends

    artifact, terminal = _completed(corr2, "译文")
    m._handle_reply(artifact)
    m._handle_reply(terminal)

    assert get_store().get(task_id).status == "completed"
    assert task_id not in m._sends


@pytest.mark.asyncio
async def test_a_continuation_that_never_lands_restores_the_pause(
    mesh, monkeypatch: pytest.MonkeyPatch
) -> None:
    m, _sent, _done = mesh
    task_id = await m.send_message("bob", "翻译 report.pdf")
    send = m._sends[task_id]
    m._handle_reply(_interrupted(_corr(m, task_id, 1), "简体还是繁体?"))

    async def a_link_that_is_down(send: Any, corr: str) -> None:
        raise RuntimeError("A2A mesh is not connected.")

    monkeypatch.setattr(m, "_publish_attempt", a_link_that_is_down)
    with pytest.raises(RuntimeError):
        await m.continue_task("bob", task_id, "简体")

    assert get_store().get(task_id).status == "interrupted"
    assert (send.interrupt, send.question) == (INPUT_REQUIRED, "简体还是繁体?")


@pytest.mark.asyncio
async def test_an_error_reply_names_its_retry_guidance(mesh) -> None:
    m, _sent, done = mesh
    task_id = await m.send_message("bob", "do it")

    m._handle_reply(_error(_corr(m, task_id, 1), -32004, "unavailable"))

    assert get_store().get(task_id).status == "failed"
    text = done[-1][1]
    assert "unavailable" in text
    assert "may be sent again" in text
    assert task_id not in m._sends


@pytest.mark.asyncio
async def test_a_direct_message_reply_settles_a_task_request(mesh) -> None:
    m, _sent, done = mesh
    task_id = await m.send_message("bob", "do it")

    m._handle_reply(_direct_message(_corr(m, task_id, 1), "hello"))

    assert get_store().get(task_id).status == "completed"
    assert done[-1][1] == "hello"
    assert done[-1][2] == COMPLETED
    assert done[-1][4] == "task"
    assert task_id not in m._sends


@pytest.mark.asyncio
async def test_a_direct_message_reply_settles_a_message_send(mesh) -> None:
    m, _sent, done = mesh
    task_id = await m.send_message("bob", "hi", message_type="message")

    m._handle_reply(_direct_message(_corr(m, task_id, 1), "hello"))

    assert get_store().get(task_id) is None, "a message send has no task row"
    assert done[-1][1] == "hello"
    assert done[-1][4] == "message"
    assert task_id not in m._sends


@pytest.mark.asyncio
async def test_a_rejected_reply_is_terminal_and_cancel_reports_it(mesh) -> None:
    m, _sent, done = mesh
    task_id = await m.send_message("bob", "do it")

    m._handle_reply(_rejected(_corr(m, task_id, 1), "not doing that"))

    assert get_store().get(task_id).status == "rejected"
    assert done[-1][2] == REJECTED
    assert task_id not in m._sends
    assert await m.cancel_task("bob", task_id) == "rejected"


@pytest.mark.asyncio
async def test_the_task_list_reports_an_interrupted_send_with_its_question(
    mesh,
) -> None:
    m, _sent, _done = mesh
    task_id = await m.send_message("bob", "翻译 report.pdf")
    m._handle_reply(_interrupted(_corr(m, task_id, 1), "简体还是繁体?"))

    row = next(row for row in m.open_tasks() if row["task_id"] == task_id)

    assert row["status"] == "interrupted"
    assert row["question"] == "简体还是繁体?"
    assert row["interrupt"] == INPUT_REQUIRED
    assert row["member"] == "bob"


@pytest.mark.asyncio
async def test_the_task_list_reports_an_interrupted_inbound_task(mesh) -> None:
    m, _sent, _done = mesh
    task_id = uuid.uuid4().hex
    m._inbound.add(task_id, "alice", context_id="ctx")
    m._inbound.interrupt(task_id, AUTH_REQUIRED, "token?")

    row = next(row for row in m.open_tasks() if row["task_id"] == task_id)

    assert row["direction"] == "received"
    assert row["status"] == "interrupted"
    assert row["interrupt"] == AUTH_REQUIRED
    assert row["question"] == "token?"


@pytest.mark.asyncio
async def test_cancel_uses_a_fresh_correlation(mesh) -> None:
    m, sent, _done = mesh
    task_id = await m.send_message("bob", "do it")
    first_corr = _corr(m, task_id, 1)

    assert await m.cancel_task("bob", task_id) == "cancelled"

    _topic, _payload, props = sent[-1]
    cancel_corr = bytes(props.CorrelationData).decode()
    assert cancel_corr != first_corr
    assert get_store().get(task_id).status == "cancelled"


@pytest.mark.asyncio
async def test_a_stalled_stream_asks_get_task_once(mesh, monkeypatch) -> None:
    m, sent, _done = mesh
    monkeypatch.setattr(_T.timeouts.deliver, "stream_idle", 0.01)
    task_id = await m.send_message("bob", "do it")

    m._handle_reply(_working(_corr(m, task_id, 1), "still working"))
    await asyncio.sleep(0.05)

    methods = []
    for _topic, payload, _props in sent:
        data = json.loads(payload)
        if "method" in data:
            methods.append(data["method"])
    assert methods.count("GetTask") == 1


@pytest.mark.asyncio
async def test_a_name_outside_the_profile_identifier_alphabet_is_refused() -> None:
    mesh = A2AMesh(A2ASettings(configured=True), "bad name")
    with pytest.raises(RuntimeError, match="not a valid profile identifier"):
        await mesh.connect()


def test_a_terminal_status_never_flips() -> None:
    store = TaskStore()
    store.record_send("task", "bob")
    store.record_reject("task")

    store.record_result("task")
    store.record_error("task")
    store.record_cancel("task")

    assert store.get("task").status == "rejected"
