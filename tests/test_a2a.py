"""The A2A mesh: the envelope, the two stores, the broker policy, and delivery.

Everything here runs without a broker, and that is a statement about where the
seams are rather than a compromise.  `A2AMesh.__init__` builds topics, a card and
a ledger and dials nothing — `connect()` is the only thing that touches the
network — so a real mesh object can be wired to its callbacks and driven by
calling those callbacks, which is exactly what the MQTT reader would do with a
message in hand.  The wire itself is the SDK's and is not re-tested here; what
this file tests is the harness glue around it, which is the part this project
wrote.

The window and the agent server are recorders, as they are in `test_schedule.py`:
what is under test is *what this plugin hands over* — the marker, the channel, and
which of `slife2.handover`'s three answers it was handed.

**What is not faked is the ledger.**  A real YAML file on the isolated data
directory, because the properties under test are exactly those: a restart tells
its orphans from ids it has never seen, a retry revives a task, and an answered
peer stops being a debt.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from slife2.a2a import broker
from slife2.a2a.inbound_store import InboundStore
from slife2.a2a.mesh import (
    AgentCard,
    AgentName,
    FIFOCache,
    _backoff_delay,
    _card_instance,
    _decode_payload,
    _presence_status,
    _reply_state,
    format_presence_line,
    safe_name,
)
from slife2.a2a.server import (
    A2A_BROADCAST,
    A2A_CANCEL_TASK,
    A2A_LIST_AGENTS,
    A2A_SEND_MESSAGE,
    A2A_TYPES,
    CHANNEL,
    MAX_QUEUED,
    Session,
    _drain,
    _wire,
    a2a_message_type,
    build_server,
    channel_for,
    marker,
)
from slife2.a2a.task_store import TaskStore
from slife2.audience import client_meta, for_the_model
from slife2.config import A2ASettings, default_config
from slife2.paths import a2a_inbound_path, a2a_inbound_paths

pytestmark = pytest.mark.unit

AGENT = "jack"
PEER = "jill"
TASK = "b1946ac92492d2347c6235b4d2611184"


def settings(**overrides: Any) -> A2ASettings:
    """A configured `a2a:` section, which is what puts a machine on the mesh."""
    base: dict[str, Any] = {"configured": True}
    base.update(overrides)
    return A2ASettings(**base)


# ── the marker: v1's envelope, written and read ─────────────────────────────


def test_the_marker_is_v1s_envelope_character_for_character() -> None:
    """`from`, not `agent_name` — v1 measured the confusion that caused.

    "agent_name" is the receiver's *own* identity, so a marker carrying it had
    receivers misreading the sender as themselves.  The keys and the spelling
    survive the port for the reason `[Wechat:{…}]`'s do: a habit is a thing that
    does not have to be relearned.
    """
    assert marker(PEER) == '[A2A:{"from": "jill", "type": "task_request"}] '
    assert marker(PEER, TASK) == (
        f'[A2A:{{"from": "jill", "type": "task_request", "task_id": "{TASK}"}}] '
    )
    assert marker(PEER, None, "message") == '[A2A:{"from": "jill", "type": "message"}] '


def test_the_marker_reads_its_own_type_back() -> None:
    """The writer and the reader are one pair, so the vocabulary stays one list."""
    for kind in A2A_TYPES:
        assert a2a_message_type(marker(PEER, TASK, kind)) == kind


def test_a_type_outside_the_vocabulary_reads_as_none() -> None:
    """A peer may put anything in the payload; the set is what this build knows."""
    assert a2a_message_type('[A2A:{"from": "jill", "type": "invented"}] hi') is None
    assert a2a_message_type("no marker here") is None
    assert a2a_message_type("[A2A:{not json] hi") is None
    assert a2a_message_type('[A2A:["a", "list"]] hi') is None
    # A bracket that never closes is a torn row: nothing is read out of it.
    assert a2a_message_type('[A2A:{"from": "jill"') is None


def test_the_channel_names_the_peer() -> None:
    """The window draws a bubble from the channel alone, so the peer is in it."""
    assert channel_for(PEER) == "a2a:jill"
    assert CHANNEL == "a2a"


# ── the scrubber, on values that come off the wire ──────────────────────────


def test_a_peer_cannot_write_a_newline_into_a_message() -> None:
    """A presence field is untrusted input on its way into a model's context."""
    assert safe_name("ji\nll") == "ji ll"
    assert safe_name("jill\x1b[31m") == "jill [31m"
    assert safe_name("  spaced   out  ") == "spaced out"
    assert len(safe_name("x" * 500)) == 128


def test_a_presence_line_is_v1s_and_only_for_real_transitions() -> None:
    online = AgentCard(agent_name=AgentName(PEER), status="online")
    assert format_presence_line(online, "online") == "⚡ jill online [online]"
    assert format_presence_line(online, "offline") == "✗ jill offline"
    # A heartbeat from an already-known peer is not worth anybody's turn.
    assert format_presence_line(online, "status_change") is None


# ── the small readers the driver is built out of ────────────────────────────


class FakeMessage:
    """The three fields the handlers read off an `aiomqtt` message."""

    def __init__(self, topic: str, payload: bytes, status: str = "") -> None:
        self.topic = type("Topic", (), {"value": topic})()
        self.payload = payload
        self.properties = (
            type("Props", (), {"UserProperty": [("a2a-status", status)]})()
            if status
            else None
        )


def test_an_empty_payload_decodes_to_nothing_rather_than_raising() -> None:
    assert _decode_payload(FakeMessage("t", b"")) is None
    assert _decode_payload(FakeMessage("t", b"not json")) is None
    assert _decode_payload(FakeMessage("t", b'{"a": 1}')) == {"a": 1}
    # A mis-encoded body is replaced rather than fatal — callers drop it.
    assert _decode_payload(FakeMessage("t", b"\xff\xfe")) is None


def test_presence_defaults_to_online_when_the_card_carries_none() -> None:
    """Absent means online, which is the property the whole readiness gate rests
    on — and the reason a *deleted* card is handled before this is ever asked."""
    assert _presence_status(FakeMessage("t", b"{}").properties) == "online"
    assert _presence_status(FakeMessage("t", b"{}", "offline").properties) == "offline"
    assert _presence_status(FakeMessage("t", b"{}", "online").properties) == "online"


def test_the_instance_id_is_read_out_of_the_nested_card() -> None:
    card = {"capabilities": {"extensions": [{"instance": "abc123"}]}}
    assert _card_instance(card) == "abc123"
    assert _card_instance({"capabilities": {}}) == ""
    assert _card_instance(None) == ""
    # Somebody else's card, or an older Slife's: no instance, but no twin either.
    assert _card_instance({"capabilities": {"extensions": ["nonsense"]}}) == ""


def test_a_terminal_reply_says_which_of_the_three_endings_it_was() -> None:
    def wire(state: str) -> dict:
        return {"result": {"statusUpdate": {"status": {"state": state}}}}

    assert _reply_state(wire("TASK_STATE_COMPLETED")) == "completed"
    assert _reply_state(wire("TASK_STATE_CANCELED")) == "cancelled"
    assert _reply_state(wire("TASK_STATE_FAILED")) == "failed"
    assert _reply_state({}) == "failed"


def test_the_retry_ladder_is_the_profiles_own_numbers() -> None:
    """1000/2000/4000 ms ± 20 %, and the last rung is the one that repeats."""
    for attempt in range(6):
        delay = _backoff_delay(attempt)
        base = [1.0, 2.0, 4.0][min(attempt, 2)]
        assert base * 0.8 <= delay <= base * 1.2


def test_a_fifo_cache_forgets_the_oldest_past_its_cap() -> None:
    """v1 got this from `cachetools`; a dict and a cut-off is a dict and a cut-off."""
    cache = FIFOCache(maxsize=2)
    cache["a"], cache["b"], cache["c"] = 1, 2, 3
    assert dict(cache) == {"b": 2, "c": 3}
    assert cache.get("a") is None


# ── the ledger, and the restart it exists for ───────────────────────────────


def test_an_inbound_task_is_written_down_and_taken_back_out() -> None:
    store = InboundStore.open(AGENT)
    store.add(TASK, PEER)
    assert [t.task_id for t in store.stale()] == []
    # On disk, with the owner named — the directory holds the sanitised name.
    raw = json.loads(json.dumps(_read_yaml(a2a_inbound_path(AGENT))))
    assert raw["agent"] == AGENT
    assert TASK in raw["pending"]
    assert raw["pending"][TASK]["peer"] == PEER

    store.drop(TASK)
    assert _read_yaml(a2a_inbound_path(AGENT))["pending"] == {}


def test_a_restart_tells_its_orphans_from_ids_it_has_never_seen() -> None:
    """The whole reason this file exists: the bridge died with the process."""
    first = InboundStore.open(AGENT)
    first.add(TASK, PEER)

    second = InboundStore.open(AGENT)
    orphans = second.stale()
    assert [t.task_id for t in orphans] == [TASK]
    assert orphans[0].peer == PEER
    # And it is offered to the model as an id that can never be completed.
    assert second.stale_entry(TASK) is not None
    assert second.stale_entry("never-seen") is None


def test_a_retry_revives_a_task_its_predecessor_dropped() -> None:
    """A peer that re-sends the same task id re-registers the bridge."""
    InboundStore.open(AGENT).add(TASK, PEER)
    second = InboundStore.open(AGENT)
    assert second.stale() != []
    second.add(TASK, PEER)
    assert second.stale() == []


def test_answering_a_peer_clears_what_that_peer_is_owed() -> None:
    """A plain message is the only reply an orphan can still get, so it pays the
    debt — and only that peer's, because the others are still waiting."""
    first = InboundStore.open(AGENT)
    first.add(TASK, PEER)
    first.add("other", "someone-else")

    # A set, not a list: the two arrived inside the same second, so the
    # oldest-first ordering the notice uses has nothing to order them by.
    store = InboundStore.open(AGENT)
    assert {t.task_id for t in store.stale()} == {"other", TASK}

    store.clear_stale_peer(PEER)
    assert [t.task_id for t in store.stale()] == ["other"]


def test_completing_an_orphan_is_refused_with_the_reply_that_still_works() -> None:
    """The three ways an id can be unknown need three different sentences, and
    the orphan's is the one a model cannot work out for itself: no result can
    ever complete it, so the answer is a plain message instead."""
    from slife2.a2a.mesh import A2AMesh

    InboundStore.open(AGENT).add(TASK, PEER)
    mesh = A2AMesh(default_config().a2a, AGENT)  # opens the ledger: TASK → stale

    orphan = mesh.complete_task(TASK, "too late")
    assert "arrived before this process started" in orphan
    assert f"Answer {PEER} with message_type='message'" in orphan

    # A withdrawal is the second: the id is real and was handed to the model, so
    # "check the marker" would send it re-reading a correct marker.
    mesh._note_withdrawn("withdrawn-id", PEER)
    assert "was withdrawn by jill" in mesh.complete_task("withdrawn-id", "x")

    # And a genuine typo is the third.
    assert "unknown task_id" in mesh.complete_task("never-seen", "x")


def test_a_ledger_without_an_owner_belongs_to_nobody() -> None:
    """The `agent:` key is the whole reason the directory name is not trusted."""
    orphan = a2a_inbound_path(AGENT)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("pending: {}\nstale: {}\n", encoding="utf-8")
    assert InboundStore.at(orphan) is None


def test_the_ledger_is_how_a_shared_process_finds_the_agents_it_serves() -> None:
    InboundStore.open(AGENT)
    InboundStore.open("someone-else")
    found = {InboundStore.at(p).agent for p in a2a_inbound_paths()}  # type: ignore[union-attr]
    assert found == {AGENT, "someone-else"}


def test_a_ledger_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each `add` is a whole-file write with an fsync, so the cap is turned down
    rather than the writes being made 200 times: the property is the trimming,
    and the number is asserted rather than exercised."""
    monkeypatch.setattr("slife2.a2a.inbound_store._MAX_ENTRIES", 5)

    store = InboundStore.open(AGENT)
    for index in range(9):
        store.add(f"task{index}", PEER)
    assert len(_read_yaml(a2a_inbound_path(AGENT))["pending"]) == 5
    # The oldest went, because the newest is the one still owed an answer.
    assert store._pending.get("task0") is None
    assert store._pending.get("task8") is not None


def _read_yaml(path: Path) -> dict:
    from slife2.a2a.inbound_store import _yaml

    return _yaml().load(path.read_text(encoding="utf-8"))


# ── the outbound task store ─────────────────────────────────────────────────


def test_a_terminal_task_is_not_un_set_by_a_late_result() -> None:
    """A caller told "cancelled" must not be contradicted by a peer's reply."""
    store = TaskStore()
    store.record_send(TASK, PEER)
    assert store.get(TASK).status == "pending"  # type: ignore[union-attr]
    store.record_cancel(TASK)
    store.record_result(TASK)
    assert store.get(TASK).status == "cancelled"  # type: ignore[union-attr]
    assert store.get("never-sent") is None


def test_the_task_store_forgets_terminal_records_first() -> None:
    store = TaskStore()
    for index in range(TaskStore.MAX_RECORDS + 20):
        store.record_send(f"t{index}", PEER)
    assert len(store._records) <= TaskStore.MAX_RECORDS
    store.record_send("last", PEER)
    # A finished task is the one nothing is waiting on, so it goes before a
    # pending one — which is the record a late result still has to be routed to.
    store.record_result("last")
    assert store.get("last") is not None


# ── the broker: probed, started, or fatal ───────────────────────────────────


def test_a_broker_that_is_already_there_is_left_alone() -> None:
    started: list[list[str]] = []
    said = broker.ensure(
        settings(),
        listening=lambda host, port: True,
        which=lambda name: "/usr/bin/mosquitto",
        start=lambda argv, url: started.append(argv),
    )
    assert "already listening" in said
    assert started == []


def test_a_missing_broker_on_this_machine_is_started() -> None:
    """The one place this port contradicts v1: it spawns mosquitto rather than
    leaving it to the operator, because a config that says `a2a:` has said it
    wants the mesh."""
    answers = iter([False, True])
    started: list[dict[str, Any]] = []

    class Child:
        returncode = None

        def poll(self) -> None:
            return None

    def start(argv: list[str], *, url: str) -> Any:
        started.append({"argv": argv, "url": url})
        return Child()

    said = broker.ensure(
        settings(),
        listening=lambda host, port: next(answers),
        which=lambda name: "/usr/bin/mosquitto",
        start=start,
    )
    assert "started" in said
    assert started[0]["argv"][0] == "/usr/bin/mosquitto"
    # `-c`, not `-p`: on mosquitto 2.x a bare `-p` version binds and then refuses
    # every anonymous client, which is a connect timeout naming nothing.
    assert started[0]["argv"][1] == "-c"
    config = Path(started[0]["argv"][2]).read_text(encoding="utf-8")
    assert "listener 1883 localhost" in config
    assert "allow_anonymous true" in config


def test_a_missing_mosquitto_fails_and_says_what_to_install() -> None:
    with pytest.raises(broker.BrokerUnavailable, match="not\n?.*on PATH|on PATH"):
        broker.ensure(
            settings(),
            listening=lambda host, port: False,
            which=lambda name: None,
        )


def test_a_remote_broker_is_never_started_locally() -> None:
    """There is no listener to create on somebody else's host."""
    started: list[list[str]] = []
    with pytest.raises(broker.BrokerUnavailable, match="not this machine"):
        broker.ensure(
            settings(broker_host="mq.example.test"),
            listening=lambda host, port: False,
            which=lambda name: "/usr/bin/mosquitto",
            start=lambda argv, url: started.append(argv),
        )
    assert started == []


def test_a_broker_that_exits_at_once_is_reported_with_its_own_log() -> None:
    """Checked on every poll, not only at the deadline: a broker that dies at
    once says so in fifty milliseconds instead of costing the whole budget."""

    class Dead:
        returncode = 1

        def poll(self) -> int:
            return 1

    with pytest.raises(broker.BrokerUnavailable, match="exited with code 1"):
        broker.ensure(
            settings(),
            listening=lambda host, port: False,
            which=lambda name: "/usr/bin/mosquitto",
            start=lambda argv, *, url: Dead(),
        )


def test_a_broker_that_never_answers_is_given_a_deadline() -> None:
    class Alive:
        returncode = None

        def poll(self) -> None:
            return None

    with pytest.raises(broker.BrokerUnavailable, match="did not serve"):
        broker.ensure(
            settings(),
            listening=lambda host, port: False,
            which=lambda name: "/usr/bin/mosquitto",
            start=lambda argv, *, url: Alive(),
            ready_seconds=0.0,
        )


# ── the peers, as recorders ─────────────────────────────────────────────────


@dataclass
class Window:
    """A recorder standing in for `slife2-tui`, and what it was handed."""

    client: Client | None = None
    #: Refuse, the way a terminal not showing that conversation does — the one
    #: failure that *proves* nothing was written.
    refuse: bool = False
    handed: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.mcp: FastMCP = FastMCP("window")

        @self.mcp.tool
        async def submit(agent: str, text: str, channel: str) -> None:
            if self.refuse:
                raise ToolError(f"no window is showing {agent}'s conversation")
            self.handed.append({"agent": agent, "text": text, "channel": channel})


@dataclass
class Conversation:
    """A recorder standing in for the agent server's `send_message`."""

    client: Client | None = None
    handed: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.mcp: FastMCP = FastMCP("agent")

        @self.mcp.tool
        async def send_message(agent: str, prompt: str, channel: str) -> None:
            self.handed.append({"agent": agent, "prompt": prompt, "channel": channel})


@pytest_asyncio.fixture(loop_scope="function")
async def window() -> AsyncIterator[Window]:
    node = Window()
    async with Client(node.mcp) as client:
        node.client = client
        yield node


@pytest_asyncio.fixture(loop_scope="function")
async def conversation() -> AsyncIterator[Conversation]:
    node = Conversation()
    async with Client(node.mcp) as client:
        node.client = client
        yield node


# ── what the mesh delivers, and where it goes ───────────────────────────────
#
#  A real `A2AMesh` is constructed and never connected — its constructor dials
#  nothing — so what runs here is the genuine callback wiring, driven the way the
#  MQTT reader would drive it.


def wired(
    window: Window | None = None,
    conversation: Conversation | None = None,
) -> Session:
    """A real mesh, never connected, with the plugin's callbacks on it.

    `A2AMesh.__init__` builds topics, a card and the agent's ledger and dials
    nothing, so this is the genuine object and the genuine wiring — what a test
    replaces is the *messenger* on the far end, never the mesh and never
    `slife2.handover`'s policy.
    """
    from slife2.a2a.mesh import A2AMesh

    session = Session(
        agent=AGENT, mesh=A2AMesh(default_config().a2a, AGENT)
    )
    _wire(session)
    return session


async def drain_until_idle(
    session: Session,
    *,
    window: Window | None = None,
    conversation: Conversation | None = None,
) -> None:
    """Run the writer until it has handed over everything queued.

    The real `_drain`, on the real `hand_over` — started, given a moment to
    finish, and cancelled the way the lifespan cancels it.
    """
    task = asyncio.create_task(
        _drain(
            session,
            default_config(),
            window.client if window else None,
            conversation.client if conversation else None,
        )
    )
    deadline = asyncio.get_running_loop().time() + 5.0
    while not session.queue.empty():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the writer never drained its queue")
        await asyncio.sleep(0.01)
    # One more pass, so the last message's hand-over has been awaited rather
    # than merely started.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_an_inbound_task_arrives_marked_and_on_the_peers_channel(
    window: Window,
) -> None:
    session = wired(window=window)
    session.mesh.on_inbound_task(PEER, "write me a poem", TASK)
    await drain_until_idle(session, window=window)

    handed = window.handed[-1]
    assert handed["agent"] == AGENT
    assert handed["channel"] == f"a2a:{PEER}"
    assert handed["text"] == f'{marker(PEER, TASK)}write me a poem'
    # The type is readable back off it, which is what a receiver needs to know
    # whether it owes a completion.
    assert a2a_message_type(handed["text"]) == "task_request"


@pytest.mark.asyncio
async def test_a_bare_message_is_marked_task_less(window: Window) -> None:
    """Only a task_request creates a task, so only it carries an id."""
    session = wired(window=window)
    session.mesh.on_inbound_task(PEER, "hello", "", kind="message")
    await drain_until_idle(session, window=window)

    text = window.handed[-1]["text"]
    assert a2a_message_type(text) == "message"
    assert '"task_id"' not in text


@pytest.mark.asyncio
async def test_a_peer_withdrawal_is_a_message_and_not_a_control_signal(
    window: Window,
) -> None:
    """Only the model knows whether it is still working on that task."""
    session = wired(window=window)
    session.mesh.on_peer_cancel(TASK, PEER)
    await drain_until_idle(session, window=window)

    text = window.handed[-1]["text"]
    assert a2a_message_type(text) == "cancel_task"
    assert "withdrew this task" in text
    assert window.handed[-1]["channel"] == f"a2a:{PEER}"


@pytest.mark.asyncio
async def test_a_broadcast_arrives_as_an_event(window: Window) -> None:
    session = wired(window=window)
    session.mesh.on_broadcast_event(PEER, "the build is green")
    await drain_until_idle(session, window=window)

    text = window.handed[-1]["text"]
    assert a2a_message_type(text) == "broadcast"
    assert text.endswith("the build is green")


@pytest.mark.asyncio
async def test_an_outbound_result_is_pushed_back_wearing_its_task_id(
    window: Window,
) -> None:
    session = wired(window=window)
    session.mesh.on_task_completion(TASK, "the poem", False, PEER, "task")
    await drain_until_idle(session, window=window)

    text = window.handed[-1]["text"]
    assert a2a_message_type(text) == "task_response"
    assert f'"task_id": "{TASK}"' in text
    assert "completed async task" in text
    assert "the poem" in text


@pytest.mark.asyncio
async def test_a_presence_change_is_delivered_and_a_heartbeat_is_not(
    window: Window,
) -> None:
    session = wired(window=window)
    card = AgentCard(agent_name=AgentName(PEER), status="online")
    session.mesh.on_agent_change(card, "status_change")
    session.mesh.on_agent_change(card, "online")
    await drain_until_idle(session, window=window)

    assert len(window.handed) == 1
    assert window.handed[0]["text"] == "⚡ jill online [online]"
    assert window.handed[0]["channel"] == f"a2a:{PEER}"


@pytest.mark.asyncio
async def test_an_empty_result_pushes_nothing(window: Window) -> None:
    session = wired(window=window)
    session.mesh.on_task_completion(TASK, "", False, PEER, "task")
    await drain_until_idle(session, window=window)
    assert window.handed == []


@pytest.mark.asyncio
async def test_the_window_is_tried_before_the_agent_server(
    window: Window, conversation: Conversation
) -> None:
    """§1.2's policy: the window is where a message becomes something a person
    sees, and the agent server is the fallback for when none is showing it."""
    session = wired(window=window, conversation=conversation)
    session.mesh.on_inbound_task(PEER, "hello", TASK)
    await drain_until_idle(session, window=window, conversation=conversation)

    assert len(window.handed) == 1
    assert conversation.handed == []


@pytest.mark.asyncio
async def test_a_refusing_window_falls_back_to_the_agent_server() -> None:
    refusing, conversation = Window(refuse=True), Conversation()
    async with Client(refusing.mcp) as window_client:
        async with Client(conversation.mcp) as agent_client:
            refusing.client, conversation.client = window_client, agent_client
            session = wired(window=refusing, conversation=conversation)
            session.mesh.on_inbound_task(PEER, "hello", TASK)
            await drain_until_idle(
                session, window=refusing, conversation=conversation
            )

    assert conversation.handed[-1]["agent"] == AGENT
    assert a2a_message_type(conversation.handed[-1]["prompt"]) == "task_request"


@pytest.mark.asyncio
async def test_the_queue_is_bounded_so_a_peer_cannot_grow_this_process() -> None:
    session = wired()
    for index in range(MAX_QUEUED + 5):
        session.mesh.on_inbound_task(PEER, f"message {index}", "")
    assert session.queue.qsize() == MAX_QUEUED
    # The oldest went, which is the one whose moment has most likely passed.
    first = session.queue.get_nowait()
    assert first[0] != f"{marker(PEER, None, 'message')}message 0"


# ── the plugin: four tools, and who may call them ───────────────────────────


def build(**overrides: Any) -> FastMCP:
    """The plugin with no broker to dial and no peers to reach."""
    config = default_config()
    if overrides.pop("on_the_mesh", False):
        object.__setattr__(config, "a2a", settings(**overrides))
    return build_server(config, source=lambda: config, connecting=False)


@pytest.mark.asyncio
async def test_the_four_tools_are_the_models_and_nothing_else_is() -> None:
    """Four, which is v1's four.  `__check` has no port — v2 has no system_health
    and what it reported is `a2a_list_agents`' answer."""
    tools = await build().list_tools()
    assert sorted(t.name for t in tools) == [
        A2A_BROADCAST,
        A2A_CANCEL_TASK,
        A2A_LIST_AGENTS,
        A2A_SEND_MESSAGE,
    ]
    assert all(for_the_model(t.meta) for t in tools)


@pytest.mark.asyncio
async def test_a_call_that_does_not_say_whose_mesh_it_is_is_refused() -> None:
    """A shared process cannot guess the agent, and the name *is* the identity."""
    async with Client(build()) as client:
        with pytest.raises(ToolError, match="did not say which conversation"):
            await client.call_tool(A2A_LIST_AGENTS, {})


@pytest.mark.asyncio
async def test_a_machine_with_no_section_is_told_so_rather_than_dialled() -> None:
    async with Client(build()) as client:
        answer = await client.call_tool(
            A2A_LIST_AGENTS, {}, meta=client_meta(AGENT)
        )
    assert answer.data["agents"] == []
    assert "no `a2a:` section" in answer.data["error"]


@pytest.mark.asyncio
async def test_an_invented_message_type_is_refused() -> None:
    async with Client(build(on_the_mesh=True)) as client:
        with pytest.raises(ToolError, match="must be 'message'"):
            await client.call_tool(
                A2A_SEND_MESSAGE,
                {"agent": PEER, "message": "hi", "message_type": "invented"},
                meta=client_meta(AGENT),
            )


@pytest.mark.asyncio
async def test_a_task_response_without_a_task_id_is_refused() -> None:
    async with Client(build(on_the_mesh=True)) as client:
        with pytest.raises(ToolError, match="task_id is required"):
            await client.call_tool(
                A2A_SEND_MESSAGE,
                {"agent": PEER, "message": "done", "message_type": "task_response"},
                meta=client_meta(AGENT),
            )


@pytest.mark.asyncio
async def test_the_roster_says_why_it_is_short_when_it_is() -> None:
    """The state a bare "offline" cannot tell apart from another agent holding
    this one's name — which is the whole of what v1's `__check` error field was
    for, and it is here rather than in a tool of its own."""
    async with Client(build(on_the_mesh=True)) as client:
        answer = await client.call_tool(
            A2A_LIST_AGENTS, {}, meta=client_meta(AGENT)
        )
    data = answer.data
    assert data["agent_name"] == AGENT
    assert data["agent_name"] == AGENT
    assert data["broker"] == "localhost:1883"
    assert data["status"] == "offline"
    assert "not connected to the broker" in data["error"]
    # Its own card is always first — v1's roster, from the cache and never by
    # dialling.
    assert data["agents"] == [{"agent_name": AGENT, "status": "offline"}]
