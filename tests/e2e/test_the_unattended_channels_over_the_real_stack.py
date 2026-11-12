"""The four ways work arrives without a person typing, each over the real stack.

**What the fast lane cannot prove, and this file exists for.**  Every channel
here has a test in the fast lane that drives the plugin's own `build_server`
over the in-memory transport: WeChat's poll loop, the A2A responder, the
schedule's clock, and a subagent's watched turn.  What none of those has is a
*process boundary* — the plugin is the test's own object, the window is a
recorder, and the transport is a function call.

Here each of the four runs where it really runs: the channel's plugin is a
daemon the launcher started, the message crosses a socket, the turn is run by
the agent server process, and the answer is written by the context process into
a store under this test's data directory.  The far ends that are not slife2 are
the three doubles in `tests/e2e/edges.py`: the model provider, WeChat's iLink
bridge, and the MQTT broker both agent networks meet on.

**The four are one module and not four**, because they share the fixture that
starts nineteen daemons and the terminal that receives what each channel hands
over; splitting them would be four copies of that arrangement.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Callable

import pytest
from a2a_over_mqtt import REPLY_ARTIFACT, REPLY_SUBMITTED, REPLY_TERMINAL, A2ARequest
from fastmcp import Client

from slife2.libs.plugin.audience import conversation_meta
from slife2.libs.turn.events import TurnFinished
from slife2.libs.turn.watch import Snapshot, TurnStarted
from slife2.tui.client import MCPAgentClient, MCPMeshClient
from tests.e2e.edges import WECHAT_AGENT, ChatAnswer, Peer, ToolAsk, Window

pytestmark = pytest.mark.e2e


async def _until(found: Callable[[], object], *, timeout: float, what: str):
    """Wait for *found* to answer something truthy, and name what did not come."""
    deadline = time.monotonic() + timeout
    while True:
        value = found()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no {what} within {timeout:.0f}s")
        await asyncio.sleep(0.1)


async def _until_async(found: Callable[[], object], *, timeout: float, what: str):
    """`_until` for a question that has to ask a server for its answer."""
    deadline = time.monotonic() + timeout
    while True:
        value = await found()  # type: ignore[misc]
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no {what} within {timeout:.0f}s")
        await asyncio.sleep(0.2)


# ── WeChat ──────────────────────────────────────────────────────────────────

PEER = "wx-peer"
TOKEN = "e2e-context-token"


@pytest.mark.asyncio
async def test_a_wechat_message_becomes_a_turn_and_the_reply_goes_back(stack) -> None:
    """**Inbound:** a message off a phone reaches a window and runs as its turn.

    Every hop is real except the bridge: the wechat plugin's process polls the
    stub over HTTP, hands the message to the terminal plugin's session over MCP,
    and the terminal (this test) submits it to the agent server with the
    channel and marker it arrived with.  The model then answers *on the
    channel*, calling `wechat_send_message`; what the bridge receives is the
    proof that the reply path crossed every one of those boundaries too.
    """
    config = stack["config"]
    provider = stack["provider"]
    ilink = stack["ilink"]
    agent = WECHAT_AGENT
    reply = "The answer is 42, sent from the terminal."

    window = Window(config, agent)
    await window.start()
    try:
        provider.script = [
            # The context store's discriminator, before the turn runs.
            ChatAnswer(text="{}"),
            ChatAnswer(
                tool=ToolAsk(
                    id="call_e2e_wechat",
                    name="wechat_send_message",
                    arguments=json.dumps(
                        {
                            "peer_wechat_id": PEER,
                            "context_token": TOKEN,
                            "text": reply,
                        }
                    ),
                )
            ),
            ChatAnswer(text="Answered the phone."),
        ]
        ilink.enqueue("what is the number?", peer=PEER, token=TOKEN)

        handover = await window.wait_handover(channel="wechat", timeout=120)
        # The marker is content, and it is what the model replies from: the
        # peer and the conversation token travel with the message because the
        # turn's own record cannot carry them.
        assert handover.text.startswith('[Wechat:{"peer_wechat_id": "wx-peer"'), (
            handover.text[:120]
        )
        assert TOKEN in handover.text
        assert "what is the number?" in handover.text

        sent = await ilink.wait_for_send(timeout=120)
        assert sent == {"to": PEER, "token": TOKEN, "text": reply}, sent
        await window.wait_reply(timeout=60)
    finally:
        await window.stop()

    # Three calls: the discriminator, the turn's tool request, and the turn
    # after the tool answered.  Step by step, so a different count says which.
    assert provider.chats == 3, [len(call) for call in provider.calls]
    assert "wechat_send_message" in provider.tool_names[1], provider.tool_names[1]
    # The turn's own request carries the marker: the message off the phone is
    # prompt content, and the prompt marker the harness adds sits in front of
    # it — so this reads the body of that first user message rather than its
    # first character.
    turn = provider.calls[1][-1]["content"]
    assert "[Wechat:" in turn and TOKEN in turn, turn[:200]
    assert "what is the number?" in turn, turn[:200]

    # And the turn is the conversation's, recorded with the channel it came in
    # on — written by the agent server process, read back through a new
    # connection.
    client = MCPAgentClient(config.agent.server.url, agent=agent)
    await client.connect()
    try:
        turns = await client.transcript()
    finally:
        await client.close()
    assert len(turns) == 1, turns
    assert turns[0]["channel"] == "wechat", turns[0]
    assert "what is the number?" in json.dumps(turns[0]["messages"])


# ── A2A ─────────────────────────────────────────────────────────────────────

REMOTE_PEER = "e2e-peer"
RECEIVER = "e2e-receiver"


@pytest.mark.asyncio
async def test_a_peer_task_runs_a_turn_and_the_answer_returns_over_the_mesh(
    stack,
) -> None:
    """**Inbound:** a task published to the mesh's request topic, answered.

    The peer is outside the stack, speaking the SDK's own requester — its
    retry ladder is what a real peer does when it publishes before the
    responder has subscribed.  The agent's window holds `join`, exactly as the
    terminal does, so the agent's retained card is on the broker and its
    responder is reading the request topic.  The model answers with the
    channel's own tool, and what the peer receives is the artifact and terminal
    the responder published, correlated back to its task id.
    """
    config = stack["config"]
    provider = stack["provider"]
    agent = RECEIVER

    window = Window(config, agent)
    await window.start()
    mesh = MCPMeshClient(config.server("peer-agent").url, agent=agent)
    await mesh.connect()
    holding = asyncio.create_task(mesh.hold())
    try:
        task_id = uuid.uuid4().hex
        answer = "The number is 42."
        provider.script = [
            ChatAnswer(text="{}"),
            ChatAnswer(
                tool=ToolAsk(
                    id="call_e2e_task_response",
                    name="peer_agent_task_response",
                    arguments=json.dumps(
                        {
                            "agent": REMOTE_PEER,
                            "task_id": task_id,
                            "outcome": "completed",
                            "message": answer,
                        }
                    ),
                )
            ),
            ChatAnswer(text="Settled the peer's task."),
        ]
        request = A2ARequest(
            text="what is the number?",
            request_id=task_id,
            task_id=task_id,
            sender=REMOTE_PEER,
            variables={"message_type": "task_request"},
        )
        peer = Peer(
            "127.0.0.1",
            stack["broker"].port,
            requester_id=REMOTE_PEER,
            # Short first-reply waits, because the ladder's first rungs are
            # the readiness wait: the window's `join` is still subscribing.
            first_reply_timeout=5.0,
            max_attempts=8,
        )
        seen = await peer.ask(agent, request.to_json(), task_id)
        assert (REPLY_SUBMITTED, task_id) in seen, seen
        assert (REPLY_ARTIFACT, answer) in seen, seen
        assert seen[-1][0] == REPLY_TERMINAL, seen

        handover = await window.wait_handover(
            channel=f"peer_agent:{REMOTE_PEER}", timeout=120
        )
        assert f'"task_id": "{task_id}"' in handover.text, handover.text[:200]
        assert "what is the number?" in handover.text
        await window.wait_reply(timeout=60)
    finally:
        holding.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await holding
        await mesh.close()
        await window.stop()

    assert provider.chats == 3, [len(call) for call in provider.calls]
    assert "peer_agent_task_response" in provider.tool_names[1], provider.tool_names[1]

    client = MCPAgentClient(config.agent.server.url, agent=agent)
    await client.connect()
    try:
        turns = await client.transcript()
    finally:
        await client.close()
    assert len(turns) == 1, turns
    assert turns[0]["channel"] == f"peer_agent:{REMOTE_PEER}", turns[0]
    assert answer in json.dumps(turns[0]["messages"])


# ── the scheduled fire ─────────────────────────────────────────────────────

SCHEDULED_AGENT = "e2e-scheduled"
TASK = "daily"
TASK_ANSWER = "The day's entry is written."


@pytest.mark.asyncio
async def test_a_scheduled_fire_runs_the_task_and_the_answer_reaches_the_window(
    stack,
) -> None:
    """**Inbound:** the clock, not a caller, starts the work.

    The task is set through the real tool on the real plugin; the fire is the
    daemon's own poll, gated on this test's window being attached, and the work
    is an ordinary agent task in the agent-task process.  What the window
    receives is that task's answer on the `agent-task:<name>` channel, which is
    the delivery the ledger calls `ran`.  No `schedule_run_now` anywhere: the
    cron and the poll are the subject.
    """
    config = stack["config"]
    provider = stack["provider"]
    agent = SCHEDULED_AGENT

    window = Window(config, agent)
    await window.start()
    try:
        provider.script = [
            # The agent task's rebuild, then its answer.
            ChatAnswer(text="{}"),
            ChatAnswer(text=TASK_ANSWER),
            # The window's turn on the delivered answer, then its reply.
            ChatAnswer(text="{}"),
            ChatAnswer(text="Noted in the conversation."),
        ]
        async with Client(config.server("schedule").url) as schedule:
            await schedule.call_tool(
                "schedule_set",
                {
                    "name": TASK,
                    "description": "Write the day's entry.",
                    "cron": "* * * * *",
                },
                meta=conversation_meta(agent),
            )

        # A minute's cron and a 30 s poll: the fire is at most a poll and a
        # minute away, and the ledger closes on the poll after the task ends.
        handover = await window.wait_handover(channel=f"agent-task:{TASK}", timeout=210)
        assert TASK_ANSWER in handover.text, handover.text

        async def settled() -> object:
            async with Client(config.server("schedule").url) as schedule:
                answer = await schedule.call_tool(
                    "schedule_runs",
                    {"name": TASK},
                    meta=conversation_meta(agent),
                )
            rows = (answer.data or {}).get("runs") or []
            # A run row is written before the work starts, so `pending` is the
            # ordinary row for a while.  Wait for an ending, and then say which
            # one it was rather than asking for the ending that would pass.
            if rows and all(row["status"] != "pending" for row in rows):
                return rows
            return None

        runs = await _until_async(settled, timeout=120, what="settled run")
        assert runs[0]["status"] == "ran", runs
        # The fire is named by the moment it was due, which is what makes it a
        # moment rather than a wall-clock guess.
        assert runs[0]["due_at"], runs
    finally:
        await window.stop()

    # Two turns, two discriminator calls: the task's, and the window's on the
    # answer it was handed.
    assert provider.chats == 4, [len(call) for call in provider.calls]
    assert window.replies, "the delivered answer ran a turn of its own"
    assert window.replies[0][1].text == "Noted in the conversation."


# ── the watched subagent ──────────────────────────────────────────────────

MAKER = "e2e-maker"
SCOUT = "scout"
SCOUT_REPORT = "The subagent looked, and this is what it found."


@pytest.mark.asyncio
async def test_a_watched_subagent_runs_unattended_and_the_watch_sees_it(
    stack,
) -> None:
    """**Inbound:** a message on the local network wakes a member, and a read
    watches it work.

    A subagent has no window, so the a2a plugin runs its turn through the agent
    server — that is the road the fast lane drives with a recorder.  Here the
    message is published over a broker by one MCP process and received by
    another, the turn is run by the agent server process with `watched=false`,
    and a *third* connection — the watch — sees the turn from its `TurnStarted`
    through its `TurnFinished`.  The member's answer comes back to the maker as
    a message in the maker's own conversation, which is the terminal this test
    attached.
    """
    config = stack["config"]
    provider = stack["provider"]

    window = Window(config, MAKER, run=False)
    await window.start()
    try:
        async with Client(config.agent.server.url) as server:
            await server.call_tool(
                "subagent_set",
                {"name": SCOUT, "role": "researcher"},
                meta=conversation_meta(MAKER),
            )

        watcher = MCPAgentClient(config.agent.server.url, agent=MAKER, subagent=SCOUT)
        await watcher.connect()
        items: list[object] = []
        watching = asyncio.create_task(watcher.watch(items.append))
        try:
            await _until(
                lambda: next((one for one in items if isinstance(one, Snapshot)), None),
                timeout=30,
                what="watch snapshot",
            )
            provider.script = [
                ChatAnswer(text="{}"),
                ChatAnswer(
                    tool=ToolAsk(
                        id="call_e2e_subagent",
                        name="subagent_send_message",
                        arguments=json.dumps(
                            {
                                "agent": MAKER,
                                "message": SCOUT_REPORT,
                                "message_type": "message",
                            }
                        ),
                    )
                ),
                ChatAnswer(text="Reported to the maker."),
            ]
            async with Client(config.server("subagent").url) as network:
                await network.call_tool(
                    "subagent_send_message",
                    {
                        "agent": SCOUT,
                        "message": "look into it",
                        "message_type": "message",
                    },
                    meta=conversation_meta(MAKER),
                )

            await _until(
                lambda: next(
                    (one for one in items if isinstance(one, TurnFinished)), None
                ),
                timeout=120,
                what="watched turn",
            )
            started = next(one for one in items if isinstance(one, TurnStarted))
            # The member's turn carries the maker's envelope — the sender is
            # content the model reads, not a property of the transport.
            assert started.text.startswith("[Agent:"), started
            assert '"from": "e2e-maker"' in started.text, started
            assert started.text.endswith("look into it"), started
            # The sender was the maker, so the member's turn wears the maker's
            # word rather than a sibling's.
            assert started.channel == f"agent:{MAKER}", started

            handover = await window.wait_handover(
                channel=f"subagent:{SCOUT}", timeout=120
            )
            assert SCOUT_REPORT in handover.text, handover.text
        finally:
            watching.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watching
            await watcher.close()
    finally:
        await window.stop()

    assert provider.chats == 3, [len(call) for call in provider.calls]
    assert "subagent_send_message" in provider.tool_names[1], provider.tool_names[1]
    assert not window.replies, (
        "a subagent's message is not a turn in the maker's window"
    )
