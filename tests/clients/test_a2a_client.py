"""The requester half of A2A: one task out, its terminal reply back.

The SDK owns the wire, the retry ladder and the reply classification; what is
pinned here is what this client puts on that wire and what it does with the
frames that come back: a `task_request`, the progress frames ignored, the
terminal frame returned.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from a2a_over_mqtt import (
    REPLY_ARTIFACT,
    REPLY_FAILED,
    REPLY_SUBMITTED,
    REPLY_TERMINAL,
    REPLY_TEXT,
    REPLY_TIMEOUT,
)

from slife2.clients import a2a
from slife2.libs.host.config import A2ASettings, Config, default_config

pytestmark = pytest.mark.unit


def a_peer_config() -> Config:
    return replace(
        default_config(),
        peer_agent=A2ASettings(
            configured=True, broker_host="broker.test", broker_port=1884
        ),
    )


class FakeRequester:
    """The SDK's requester, cut down to the one call the probe makes."""

    replies: list[tuple[str, str]] = []
    call: dict[str, object] = {}

    def __init__(self, mqtt, topics, **kwargs) -> None:
        FakeRequester.call = {"mqtt": mqtt, "topics": topics, **kwargs}

    async def stream(self, agent, payload, correlation_id):
        FakeRequester.call.update(
            agent=agent, payload=payload, correlation_id=correlation_id
        )
        for kind, content in FakeRequester.replies:
            yield kind, content


@pytest.mark.asyncio
async def test_the_probe_sends_one_task_and_returns_the_terminal_text(
    monkeypatch,
) -> None:
    """**A task, not a turn.**  The request is what a peer sends, and the
    answer is the artifact the responder publishes before its terminal status —
    the two progress frames around it are the responder's business."""
    FakeRequester.replies = [
        (REPLY_SUBMITTED, "the task id"),
        (REPLY_TEXT, "working"),
        (REPLY_ARTIFACT, "the answer"),
        (REPLY_TERMINAL, ""),
    ]
    monkeypatch.setattr(a2a, "Requester", FakeRequester)

    result = await a2a.probe(a_peer_config(), "tb-1", "do it", timeout=30)

    assert result.text == "the answer"
    assert FakeRequester.call["agent"] == "tb-1"
    assert FakeRequester.call["correlation_id"] == result.task_id
    assert FakeRequester.call["mqtt"].host == "broker.test"
    request = json.loads(str(FakeRequester.call["payload"]))
    assert request["params"]["metadata"]["variables"] == {
        "message_type": "task_request"
    }
    assert request["params"]["message"]["parts"][0]["text"] == "do it"
    assert request["params"]["message"]["taskId"] == result.task_id


@pytest.mark.asyncio
async def test_a_failed_task_is_a_probe_failure(monkeypatch) -> None:
    FakeRequester.replies = [(REPLY_FAILED, "cancelled")]
    monkeypatch.setattr(a2a, "Requester", FakeRequester)

    with pytest.raises(a2a.ProbeError, match="cancelled"):
        await a2a.probe(a_peer_config(), "tb-1", "do it", timeout=30)


@pytest.mark.asyncio
async def test_an_exhausted_requester_is_a_probe_failure(monkeypatch) -> None:
    FakeRequester.replies = [(REPLY_TIMEOUT, "")]
    monkeypatch.setattr(a2a, "Requester", FakeRequester)

    with pytest.raises(a2a.ProbeError, match="no answer"):
        await a2a.probe(a_peer_config(), "tb-1", "do it", timeout=30)


@pytest.mark.asyncio
async def test_a_machine_without_the_peer_network_is_refused() -> None:
    with pytest.raises(a2a.ProbeError, match="peer_agent"):
        await a2a.probe(default_config(), "slife2", "do it")


@pytest.mark.asyncio
async def test_the_overall_timeout_stops_a_silent_stream(monkeypatch) -> None:
    class Silent(FakeRequester):
        async def stream(self, agent, payload, correlation_id):
            yield REPLY_SUBMITTED, "the task id"
            await asyncio.sleep(60)

    monkeypatch.setattr(a2a, "Requester", Silent)

    with pytest.raises(a2a.ProbeError, match="within"):
        await a2a.probe(a_peer_config(), "tb-1", "do it", timeout=0.01)
