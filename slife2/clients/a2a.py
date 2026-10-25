"""The requester half of the peer network: one task, one answer.

**A probe, not a channel.**  It asks a headless agent one task and waits for the
model's own `task_response`.  Nothing here reads a turn store or speaks to the
TUI: a window's progress frames are its own business, and the artifact its
terminal status closes is the answer.  That is the whole of what a one-shot
driver needs.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import aiomqtt
from a2a_over_mqtt import (
    REPLY_ARTIFACT,
    REPLY_ERROR,
    REPLY_FAILED,
    REPLY_INPUT_REQUIRED,
    REPLY_TERMINAL,
    REPLY_TIMEOUT,
    A2ARequest,
    MqttConfig,
    Requester,
    TopicSpace,
)

from slife2.helpers import timeouts as _T
from slife2.libs.host.config import Config

#: The requester's name in the request metadata.  It is a label for the target's
#: marker, not an agent: the SDK gives every run its own session-suffixed MQTT
#: client id, so two probes do not contend for a name.
REQUESTER = "slife2-probe"


class ProbeError(RuntimeError):
    """The probe did not get a completed task back."""


@dataclass(frozen=True)
class ProbeResult:
    """What a probe got: the answer, and the task it answered."""

    text: str
    task_id: str


async def probe(
    config: Config,
    agent: str,
    prompt: str,
    *,
    timeout: float | None = None,
    requester: str = REQUESTER,
) -> ProbeResult:
    """Ask *agent* one task, and answer with the reply its terminal status closes.

    **The retry ladder is the readiness wait.**  A window joins the mesh a
    moment after it starts, and a request published before it did finds no
    responder.  The SDK repeats the publish — a fresh correlation, the same task
    id, which the responder dedups — so the first attempt that lands after the
    window is online is the one that runs.  Progress frames are ignored; the
    artifact the terminal status closes is the result.

    A broker that is not up yet is the same kind of gap, one layer down: the
    stack starts it, and the probe starts at once.  A refused connection is
    retried until *timeout*, when the ladder has had nothing to land on.

    Raises:
        ProbeError: this machine has no `peer_agent:` section, the target
            answered with a failed/cancelled/input-required task, or *timeout*
            passed with no terminal frame.
    """
    settings = config.peer_agent
    if not settings.configured:
        raise ProbeError(
            "this machine is not on the peer network: there is no `peer_agent:` section"
        )
    if timeout is None:
        timeout = _T.timeouts.deliver.probe
    first_reply = _T.timeouts.deliver.reply_first
    task_id = uuid.uuid4().hex
    request = A2ARequest(
        text=prompt,
        request_id=task_id,
        task_id=task_id,
        sender=requester,
        variables={"message_type": "task_request"},
    )
    client = Requester(
        MqttConfig(host=settings.broker_host, port=settings.broker_port),
        TopicSpace(org=settings.org, unit=settings.unit),
        requester_id=requester,
        first_reply_timeout=first_reply,
        stream_idle_timeout=_T.timeouts.deliver.stream_idle,
        max_attempts=max(3, int(timeout / first_reply)),
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ProbeError(f"no answer from {agent!r} within {timeout:.0f}s")
            async with asyncio.timeout(remaining):
                answer = ""
                async for kind, content in client.stream(
                    agent, request.to_json(), task_id
                ):
                    # **The artifact is the answer; the terminal is the full
                    # stop.**  The responder publishes the result as an artifact
                    # update and then a `completed` status with no message, so a
                    # probe that read only the terminal would print an empty
                    # answer.
                    if kind == REPLY_ARTIFACT:
                        answer = str(content)
                        continue
                    if kind == REPLY_TERMINAL:
                        return ProbeResult(text=str(content) or answer, task_id=task_id)
                    if kind in (REPLY_FAILED, REPLY_ERROR, REPLY_INPUT_REQUIRED):
                        raise ProbeError(f"{kind}: {content}" if content else kind)
                    if kind == REPLY_TIMEOUT:
                        raise ProbeError(f"no answer from {agent!r}")
            raise ProbeError(
                f"the reply stream from {agent!r} ended without a terminal frame"
            )
        except aiomqtt.MqttError as exc:
            if loop.time() >= deadline:
                raise ProbeError(
                    f"no answer from {agent!r} within {timeout:.0f}s: {exc}"
                ) from exc
            await asyncio.sleep(_T.timeouts.pacing.mesh_retry_delay)
        except TimeoutError as exc:
            raise ProbeError(f"no answer from {agent!r} within {timeout:.0f}s") from exc
