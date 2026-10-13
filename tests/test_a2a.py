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
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

import slife2.timeouts as _T
from slife2.a2a import broker
from slife2.a2a import server as a2a_server
from slife2.a2a.inbound_store import InboundStore
from slife2.a2a.mesh import (
    A2AMesh,
    AgentCard,
    AgentName,
    FIFOCache,
    _backoff_delay,
    _card_instance,
    _decode_payload,
    _OutboundSend,
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
    JOIN,
    MAX_QUEUED,
    ORPHAN_PEER,
    ORPHAN_RETRIES,
    USED,
    WINDOW,
    Session,
    _drain,
    _wire,
    a2a_message_type,
    announce_orphans,
    build_server,
    channel_for,
    marker,
)
from slife2.a2a.task_store import TaskStore
from slife2.audience import client_meta, for_the_model
from slife2.config import A2ASettings, default_config
from slife2.paths import a2a_inbound_path, a2a_inbound_paths
from slife2.runtime import read_record
from slife2.tui.client import MCPMeshClient

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
    """v1's words, without v1's trailing `[status]`.

    v1 wrote `⚡ jill online [online]` because that line was also read by a model,
    which was handed the wire's status verbatim.  The one reader now is a window
    (`slife2.tui.control`'s `note`), and `online` twice in one line is a fact the
    first word carries.
    """
    online = AgentCard(agent_name=AgentName(PEER), status="online")
    assert format_presence_line(online, "online") == "⚡ jill online"
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


@pytest.mark.asyncio
async def test_the_artifact_and_routing_caches_are_capped_by_the_cache_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both attributes grew a hand-rolled `pop(next(iter(...)))` a few lines from
    the `FIFOCache` that already does exactly that — and that `_sends` and
    `_withdrawn` beside them use.  The class is where the cap is stated now, so
    the assertion is on the class: a plain dict here is the drift coming back.
    """
    mesh = A2AMesh(default_config().a2a, AGENT)
    assert isinstance(mesh._pending, FIFOCache)
    assert isinstance(mesh._corr_to_task, FIFOCache)

    monkeypatch.setattr(mesh._pending, "maxsize", 2)
    for index in range(3):
        mesh._pending[f"task{index}"] = str(index)
    assert list(mesh._pending) == ["task1", "task2"]  # the oldest went

    async def no_publish(topic: str, payload: str, props: Any, *, qos: int = 1) -> None:
        return None

    monkeypatch.setattr(mesh, "_publish_props", no_publish)
    monkeypatch.setattr(mesh._corr_to_task, "maxsize", 2)
    send = _OutboundSend(TASK, PEER, "session", "{}")
    for corr in ("c1", "c2", "c3"):
        await mesh._publish_attempt(send, corr)
    assert list(mesh._corr_to_task) == ["c2", "c3"]


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
        #: `ensure` records what it spawned, so a stand-in for `Popen` needs the
        #: one field that record is made of.
        pid = os.getpid()

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


@pytest.mark.parametrize("host", sorted(broker.WILDCARD_HOSTS))
def test_a_wildcard_is_not_a_local_host(host: str) -> None:
    """`0.0.0.0` is *this* machine's every interface, which is not the same
    question as "may I start an anonymous broker here" — so it is neither local
    nor remote, and it gets its own answer."""
    assert broker.is_wildcard(host) is True
    assert broker.is_local(host) is False


@pytest.mark.parametrize("host", sorted(broker.WILDCARD_HOSTS))
def test_a_wildcard_host_is_refused_rather_than_started_open(host: str) -> None:
    """A broker started for a wildcard binds every interface with
    `allow_anonymous true`, so every `$a2a/v1/...` topic would be published to
    the LAN and any host could impersonate a peer.

    That is the one edit that reads as "make it reachable" and silently makes
    the mesh reachable by everybody, so nothing is spawned: the start fails
    with the reason instead.
    """
    started: list[list[str]] = []
    with pytest.raises(broker.BrokerUnavailable, match="every interface"):
        broker.ensure(
            settings(broker_host=host),
            listening=lambda h, p: False,
            which=lambda name: "/usr/bin/mosquitto",
            start=lambda argv, url: started.append(argv),
        )
    assert started == []


def test_a_broker_that_exits_at_once_is_reported_with_its_own_log() -> None:
    """Checked on every poll, not only at the deadline: a broker that dies at
    once says so in fifty milliseconds instead of costing the whole budget."""

    class Dead:
        returncode = 1
        pid = os.getpid()

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
        pid = os.getpid()

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


# ── and stopped, which is the other half of the same record ─────────────────


def test_the_broker_a_start_brought_up_is_stopped_by_the_record_it_wrote() -> None:
    """`ensure` writes what it started, and `stop` reads exactly that.

    The record is the *permission*: it is written when — and only when — this
    machine's slife2 starts a broker, so a take-down can tell the one it owns
    from a system service at the same address, and from a pid that now belongs to
    somebody else.
    """
    killed: list[int] = []
    answers = iter([False, True])

    class Child:
        returncode = None
        #: *This* process, because the record has to be provable: `same_process`
        #: refuses a pid whose start token cannot be read, which is exactly what
        #: a made-up number is — and that refusal is the rule this test's
        #: `stop` is checking the other half of.
        pid = os.getpid()

        def poll(self) -> None:
            return None

    broker.ensure(
        settings(),
        listening=lambda host, port: next(answers),
        which=lambda name: "/usr/bin/mosquitto",
        start=lambda argv, *, url: Child(),
    )
    wrote = read_record(broker.log_url(settings()))

    said = broker.stop(settings(), kill=lambda pid: killed.append(pid) or True)

    assert wrote is not None and wrote.pid == os.getpid()
    assert wrote.name == broker.BROKER_NAME
    assert said == "stopped mosquitto on localhost:1883"
    assert killed == [os.getpid()]
    # And the record goes with it, so a second take-down has nothing to act on.
    assert read_record(broker.log_url(settings())) is None


def test_a_broker_that_was_not_ours_is_never_stopped() -> None:
    """No record, and something *is* listening: somebody else's broker.

    Which is the case this rule exists for — a system mosquitto, one an operator
    ran, one belonging to another data directory — and the answer is to say so
    and signal nothing.  The probe is stubbed because what is at that address on
    the machine running this test is none of the test's business: with the real
    one it passed on a machine that happened to have a broker up and failed on
    one that did not, which is the definition of a test that proves nothing.
    """
    killed: list[int] = []
    said = broker.stop(
        settings(),
        kill=lambda pid: killed.append(pid) or True,
        listening=lambda host, port: True,
    )

    assert said == "not started by slife2; left alone"
    assert killed == []


def test_a_machine_with_no_broker_has_nothing_to_report() -> None:
    """`""`, so the take-down says nothing about a daemon nobody asked for.

    The probe is stubbed because this machine *does* have mosquitto installed
    and a test that depended on that would be a test of the machine.
    """
    said = broker.stop(
        settings(),
        kill=lambda pid: False,
        listening=lambda host, port: False,
    )
    assert said == ""


# ── the peers, as recorders ─────────────────────────────────────────────────


@dataclass
class Window:
    """A recorder standing in for `slife2-tui`, and what it was handed.

    Two tools, because the window has two kinds of arrival (`slife2.tui.control`):
    a `submit` becomes a turn, and a `note` is drawn as a line and goes no
    further.  They are recorded apart so a test can say which one a thing was —
    which is the whole of what presence's delivery is now decided by.
    """

    client: Client | None = None
    #: Refuse, the way a terminal not showing that conversation does — the one
    #: failure that *proves* nothing was written.
    refuse: bool = False
    #: Die the way a stream that broke does — the answer that proves *nothing*,
    #: and therefore the one no caller may act on by trying again.
    broken: bool = False
    handed: list[dict[str, Any]] = field(default_factory=list)
    #: The lines drawn, in the order they arrived: `{"agent", "text"}`.
    noted: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.mcp: FastMCP = FastMCP("window")

        @self.mcp.tool
        async def submit(agent: str, text: str, channel: str) -> None:
            if self.broken:
                raise RuntimeError("the stream died")
            if self.refuse:
                raise ToolError(f"no window is showing {agent}'s conversation")
            self.handed.append({"agent": agent, "text": text, "channel": channel})

        @self.mcp.tool
        async def note(agent: str, text: str) -> None:
            if self.broken:
                raise RuntimeError("the stream died")
            if self.refuse:
                raise ToolError(f"no window is showing {agent}'s conversation")
            self.noted.append({"agent": agent, "text": text})


@dataclass
class Conversation:
    """A recorder standing in for the agent server's `send_message`."""

    client: Client | None = None
    #: A turn that failed — which on a cold start is every turn, because the hub
    #: is one wave behind (`slife2.launcher.NEEDS`) and a turn's first step is
    #: the hub.  The call arrives and the answer is a refusal, so nothing was
    #: written and the hand-over is `False` rather than `None`.
    broken: bool = False
    handed: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.mcp: FastMCP = FastMCP("agent")

        @self.mcp.tool
        async def send_message(agent: str, prompt: str, channel: str) -> None:
            if self.broken:
                raise ToolError("the turn failed: the toolhub is not answering")
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

    session = Session(agent=AGENT, mesh=A2AMesh(default_config().a2a, AGENT))
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
    deadline = (  # timeouts: the value this test is built around, not a budget
        asyncio.get_running_loop().time() + 5.0
    )  # timeouts: the value this test is built around, not a budget
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
    assert handed["text"] == f"{marker(PEER, TASK)}write me a poem"
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
async def test_a_presence_change_is_drawn_and_a_heartbeat_is_not(
    window: Window,
) -> None:
    """A note, and not a message: no turn, no channel, and nothing for the model.

    This is where presence stopped being something the conversation is told.  A
    peer coming online asks the model for nothing — the peer that wants to be
    answered sends a task, and the roster is `a2a_list_agents`' answer — so the
    line goes to the window that is showing the agent and stops there.
    """
    session = wired(window=window)
    card = AgentCard(agent_name=AgentName(PEER), status="online")
    session.mesh.on_agent_change(card, "status_change")
    session.mesh.on_agent_change(card, "online")
    await drain_until_idle(session, window=window)

    assert window.handed == [], "presence is not a turn, and wears no channel"
    # Addressed to the agent whose window it is — the peer is in the words, as
    # it is for every line: `Session.post`'s `peer` is who the line is *about*,
    # and only a message needs a channel name (`channel_for`).
    assert window.noted == [{"agent": AGENT, "text": "⚡ jill online"}]


@pytest.mark.asyncio
async def test_a_presence_line_with_no_window_to_draw_it_goes_nowhere(
    conversation: Conversation,
) -> None:
    """No fallback, unlike every message here.

    `to_agent` exists because a message that reached no window is not a lost
    message — the agent server runs the turn and a window opened later restores
    it.  A line about a moment is not that: running a model turn for one costs a
    call to tell somebody a fact they cannot act on, so it is dropped and the
    log says so (`a2a_note_no_window`).
    """
    session = wired()
    session.mesh.on_agent_change(
        AgentCard(agent_name=AgentName(PEER), status="online"), "online"
    )
    await drain_until_idle(session, conversation=conversation)

    assert conversation.handed == []


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
            await drain_until_idle(session, window=refusing, conversation=conversation)

    assert conversation.handed[-1]["agent"] == AGENT
    assert a2a_message_type(conversation.handed[-1]["prompt"]) == "task_request"


@pytest.mark.asyncio
async def test_one_bad_message_does_not_end_the_writer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    window: Window,
) -> None:
    """The writer is one task per agent and nothing watches it until shutdown,
    so a raise escaping the loop used to kill it *silently*: the mesh still
    looked connected, and every later arrival queued behind a writer that no
    longer existed.  One undeliverable message is dropped, logged, and the loop
    goes on to the next."""
    caplog.set_level(logging.ERROR, logger="slife2.a2a.server")
    real_hand_over = a2a_server.hand_over
    calls = {"n": 0}

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the mesh callback blew up")
        return await real_hand_over(*args, **kwargs)

    monkeypatch.setattr(a2a_server, "hand_over", flaky)

    session = wired()
    session.post("first", PEER, "a message from jill")
    session.post("second", PEER, "a message from jill")
    # `drain_until_idle` cancels the writer at the end, so a writer that died on
    # the first message would fail this call rather than reaching the asserts.
    await drain_until_idle(session, window=window)

    assert window.handed == [
        {"agent": AGENT, "text": "second", "channel": f"a2a:{PEER}"}
    ]
    assert any(
        "a2a_delivery_failed" in record.getMessage() for record in caplog.records
    )


# ── the orphan notice, which is nobody's but this plugin's ──────────────────
#
#  The one message here that no peer will ever re-send, and the one the plugin
#  says unprompted.  What it must not do is wait for the peers a *turn* needs at
#  the moment it is found: the hub waits for every plugin, so it is up after
#  this one, and a hand-over awaited from the lifespan would be either lost or
#  running a 600-second turn inside a 90-second readiness deadline.


async def writer_until(
    session: Session,
    done: Callable[[], bool],
    *,
    window: Window | None = None,
    conversation: Conversation | None = None,
) -> None:
    """Run the writer until *done*, and cancel it the way the lifespan does.

    `drain_until_idle` is the wrong tool for a retry: a notice that came back
    "nothing was written" leaves the queue while it waits, so an empty queue is
    not the end of the story — which is the whole point of the retry.
    """
    task = asyncio.create_task(
        _drain(
            session,
            default_config(),
            window.client if window else None,
            conversation.client if conversation else None,
        )
    )
    deadline = (  # timeouts: the value this test is built around, not a budget
        asyncio.get_running_loop().time() + 10.0
    )  # timeouts: the value this test is built around, not a budget
    while not done():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the writer never did what the test waited for")
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_the_orphan_notice_is_queued_rather_than_sent_from_the_start() -> None:
    """The ledger is read at the start; the hand-over is the writer's job.

    Asserted as the *absence* of a submission, because that is the difference
    that matters: a notice awaited where it is found is this plugin's readiness
    spent on another plugin's turn.
    """
    InboundStore.open(AGENT).add(TASK, PEER)
    session = wired()

    announce_orphans(session)

    text, peer, what, retries, note = session.queue.get_nowait()
    assert session.queue.empty(), "one notice, and nothing else on the queue"
    assert peer == ORPHAN_PEER
    assert channel_for(peer) == f"{CHANNEL}:{ORPHAN_PEER}"
    assert what == "1 orphaned inbound task(s)"
    assert TASK in text and PEER in text
    # It is a message for the model to act on, not a line for a window to draw.
    assert note is False
    # It carries the retries, and it is the only message that does: everything
    # a peer sent is posted with none.
    assert retries == ORPHAN_RETRIES


@pytest.mark.asyncio
async def test_the_orphan_notice_lands_on_the_channels_the_window_draws_alone() -> None:
    """The queue is what delivers it, and the bubble is v1's `A2A(orphans)> `."""
    InboundStore.open(AGENT).add(TASK, PEER)
    window = Window()
    async with Client(window.mcp) as client:
        window.client = client
        session = wired()
        announce_orphans(session)
        await drain_until_idle(session, window=window)

    # Once, and once only: the retries it carries are for a hand-over that came
    # back "nothing was written", and this one landed.
    assert len(window.handed) == 1
    handed = window.handed[-1]
    assert handed["channel"] == f"{CHANNEL}:{ORPHAN_PEER}"
    assert handed["agent"] == AGENT
    # The one reply an orphan can still get, and the peer it is owed to.
    assert "message" in handed["text"] and PEER in handed["text"]


@pytest.mark.asyncio
async def test_a_notice_that_reached_nothing_is_offered_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`False` is the one answer that licenses a second attempt — and this is the
    one message with no peer behind it to make that attempt itself.

    The two fakes are the cold start exactly: a window that is not showing that
    conversation (refused, so provably nothing was written) and a turn that
    fails because the hub is not up yet.
    """
    monkeypatch.setattr(_T.timeouts.pacing, "orphan_retry", 0.01)
    monkeypatch.setattr(a2a_server, "ORPHAN_RETRIES", 2)
    caplog.set_level(logging.WARNING, logger="slife2.a2a.server")

    def attempts(peer: str) -> int:
        return sum(
            1
            for record in caplog.records
            if f"peer={peer} " in record.getMessage()
            and record.getMessage().startswith("a2a_delivery_not_confirmed")
        )

    refusing, broken = Window(refuse=True), Conversation(broken=True)
    async with Client(refusing.mcp) as window_client:
        async with Client(broken.mcp) as agent_client:
            refusing.client, broken.client = window_client, agent_client
            InboundStore.open(AGENT).add(TASK, PEER)
            session = wired()
            announce_orphans(session)
            session.post("a peer's own message", PEER, "a message from jill")

            await writer_until(
                session,
                lambda: attempts(ORPHAN_PEER) == a2a_server.ORPHAN_RETRIES + 1,
                window=refusing,
                conversation=broken,
            )

    # The first attempt and every retry it was given, and no more than that.
    assert attempts(ORPHAN_PEER) == a2a_server.ORPHAN_RETRIES + 1
    # The peer's message: offered once.  A peer re-sends its own task when it
    # gets no answer, and a second copy of its words is worse than a late one.
    assert attempts(PEER) == 1
    assert broken.handed == []


@pytest.mark.asyncio
async def test_a_notice_the_writer_cannot_judge_is_not_offered_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The third answer stops it, retries or not.

    A stream that died says nothing either way about whether the words landed,
    and a notice arriving twice is how a conversation is told twice about a debt
    it may already have paid.
    """
    caplog.set_level(logging.WARNING, logger="slife2.a2a.server")
    dying = Window(broken=True)
    async with Client(dying.mcp) as client:
        dying.client = client
        InboundStore.open(AGENT).add(TASK, PEER)
        session = wired()
        announce_orphans(session)

        await writer_until(
            session,
            lambda: any(
                "handed=None" in record.getMessage() for record in caplog.records
            ),
            window=dying,
        )

    assert "handed=None" in caplog.records[-1].getMessage()
    assert session.queue.empty(), "a `None` is not put back"


@pytest.mark.asyncio
async def test_the_queue_is_bounded_so_a_peer_cannot_grow_this_process() -> None:
    session = wired()
    for index in range(MAX_QUEUED + 5):
        session.mesh.on_inbound_task(PEER, f"message {index}", "")
    assert session.queue.qsize() == MAX_QUEUED
    # The oldest went, which is the one whose moment has most likely passed.
    first = session.queue.get_nowait()
    assert first[0] != f"{marker(PEER, None, 'message')}message 0"


# ── the start, and what it must not leak ────────────────────────────────────
#
#  The start-up work runs inside the guarded region, so a start that fails
#  *after* one agent's session exists still hands it back.  What leaks otherwise
#  is a `_drain` writer running with nothing watching it — one per ledger — for
#  the life of a process somebody then restarts.


@pytest.mark.asyncio
async def test_a_start_that_fails_after_building_a_session_still_tears_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `finally` used to guard only the `yield`, so a start that raised on
    its way to it left every built session running: its writer, and — when a
    start still joined the mesh as well — a pair of MQTT connections and a
    retained *online* card nobody would retire."""
    InboundStore.open(AGENT).add(TASK, PEER)  # a ledger, and an orphan in it
    left: list[str] = []

    async def connect(self: Any) -> None:
        self._connected = True

    async def disconnect(self: Any) -> None:
        left.append(self.agent_name)

    def boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("the orphan template will not render")

    monkeypatch.setattr(A2AMesh, "connect", connect)
    monkeypatch.setattr(A2AMesh, "disconnect", disconnect)
    monkeypatch.setattr(a2a_server, "ensure_broker", lambda cfg: "already listening")
    # The failure the defect names: an orphan notice whose template will not
    # render, reached with the agent's session already built.
    monkeypatch.setattr(a2a_server, "render", boom)

    config = default_config()
    object.__setattr__(config, "a2a", settings())
    mcp = build_server(config, source=lambda: config, connecting=True)

    with pytest.raises(RuntimeError, match="will not render"):
        async with Client(mcp):
            pass

    # The session was handed back — disconnected, and its writer cancelled with
    # it.  Nothing here joined the mesh on the way (that is a window's call now),
    # so the disconnect is the teardown's own, which is the half that still has
    # to run.
    assert left == [AGENT]


# ── the plugin: what it offers, and to whom ─────────────────────────────────


def build(**overrides: Any) -> FastMCP:
    """The plugin with no broker to dial and no peers to reach."""
    config = default_config()
    if overrides.pop("on_the_mesh", False):
        object.__setattr__(config, "a2a", settings(**overrides))
    return build_server(config, source=lambda: config, connecting=False)


@pytest.mark.asyncio
async def test_the_four_mesh_tools_are_the_models_and_nothing_else_is() -> None:
    """Four, which is v1's four.  `__check` has no port — v2 has no system_health
    and what it reported is `a2a_list_agents`' answer.

    `join` is here and is deliberately not one of them: it crosses a process
    boundary for a *window* (it is held open, and holding it is what keeps the
    agent on the mesh), so it carries no audience and the hub leaves it out of
    the tool set — the arrangement `slife2.tui.control`'s `attach` has.
    """
    tools = await build().list_tools()
    by_name = {t.name: t for t in tools}
    assert sorted(by_name) == [
        A2A_BROADCAST,
        A2A_CANCEL_TASK,
        A2A_LIST_AGENTS,
        A2A_SEND_MESSAGE,
        JOIN,
    ]

    model_tools = [name for name, tool in by_name.items() if for_the_model(tool.meta)]
    assert sorted(model_tools) == [
        A2A_BROADCAST,
        A2A_CANCEL_TASK,
        A2A_LIST_AGENTS,
        A2A_SEND_MESSAGE,
    ]
    assert not for_the_model(by_name[JOIN].meta), "a window's call, not the model's"


@pytest.mark.asyncio
async def test_a_call_that_does_not_say_whose_mesh_it_is_is_refused() -> None:
    """A shared process cannot guess the agent, and the name *is* the identity."""
    async with Client(build()) as client:
        with pytest.raises(ToolError, match="did not say which conversation"):
            await client.call_tool(A2A_LIST_AGENTS, {})


@pytest.mark.asyncio
async def test_a_machine_with_no_section_is_told_so_rather_than_dialled() -> None:
    async with Client(build()) as client:
        answer = await client.call_tool(A2A_LIST_AGENTS, {}, meta=client_meta(AGENT))
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
        answer = await client.call_tool(A2A_LIST_AGENTS, {}, meta=client_meta(AGENT))
    data = answer.data
    assert data["agent_name"] == AGENT
    assert data["broker"] == "localhost:1883"
    assert data["status"] == "offline"
    assert "not connected to the broker" in data["error"]
    # Its own card is always first — v1's roster, from the cache and never by
    # dialling.
    assert data["agents"] == [{"agent_name": AGENT, "status": "offline"}]


# ── the window's call, which is what puts an agent on the mesh ──────────────


@contextlib.asynccontextmanager
async def served(mcp: FastMCP) -> AsyncIterator[str]:
    """Serve a built server on a real port, and yield its URL.

    `tests/test_tui_control.py` has its own, and this is deliberately a second
    copy rather than a shared one: what each needs it *for* is different — that
    file proves a hand-over reaches a stream, this one proves a stream that ends
    takes the mesh with it — and a helper shared between two modules for two
    reasons is the shape that grows a parameter per caller.
    """
    import socket

    import uvicorn

    app = mcp.http_app(path="/mcp", json_response=False, stateless_http=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    uv = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    serve_task = asyncio.create_task(uv.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if uv.started:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the server never came up")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uv.should_exit = True
        await asyncio.wait_for(
            serve_task,
            timeout=5,  # timeouts: the value this test is built around, not a budget
        )  # timeouts: the value this test is built around, not a budget
        sock.close()


async def until(
    predicate: Callable[[], bool], what: str, *, seconds: float = 5.0
) -> None:
    """Wait for *predicate*, or fail saying what never happened.

    `seconds` is the caller's because the budgets here are not the same kind of
    number.  Waiting for the plugin to *draw* something is waiting for a call
    this process just made, so it is prompt; waiting for it to notice a window
    that has gone is waiting for the transport to surface a failed write, which
    is the socket's timing and not the plugin's — see the one caller that asks
    for more.
    """
    deadline = (  # timeouts: the value this test is built around, not a budget
        asyncio.get_running_loop().time() + seconds
    )  # timeouts: the value this test is built around, not a budget
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(what)
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_join_puts_the_agent_on_the_mesh_and_says_nothing_about_it(
    monkeypatch: pytest.MonkeyPatch, window: Window
) -> None:
    """Joined on entry and held — and **not** announced to the agent's own window.

    Two halves of one decision.  The call *is* the membership: it returns only
    when the window goes away, so nothing in this test ends it, and the mesh
    stays up throughout.  And nobody is *told* about it: a window showing jack is
    jack, so `⚡ jack online` in it would be the harness telling somebody what
    they already are — the same reason the mesh's own discovery path has never
    announced our own card.  What a window shows is its peers.

    The real connect is replaced — a unit test has no broker — and what is under
    test is the lifetime, not MQTT.
    """
    joined, left = _a_mesh_that_records_its_ends(monkeypatch)
    config = default_config()
    object.__setattr__(config, "a2a", settings())
    mcp = build_server(
        config, source=lambda: config, window_client=window.client, connecting=False
    )

    async with Client(mcp) as client:
        holding = asyncio.create_task(client.call_tool(JOIN, {"agent": AGENT}))
        await until(lambda: joined == [AGENT], "the join never reached the mesh")
        assert left == [], "the mesh went down while the window was still holding it"
        holding.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await holding

    assert window.noted == [], "the agent's own arrival is not news to itself"
    assert window.handed == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_window_that_goes_away_takes_the_agent_off_the_mesh(
    monkeypatch: pytest.MonkeyPatch, window: Window
) -> None:
    """A departure with nothing sent: the window closes, the mesh goes down.

    This is a property of the *wire* and cannot be shown in process — the end of
    the call the window was holding is what ends the membership, and there is no
    `leave` for it to send — so it is served on a real port, like
    `tests/test_tui_control.py`'s proof for the window's own inbox.

    **And it is driven through the real client** (`MCPMeshClient`, the three
    lines `SlifeApp._hold_mesh` runs), because that is the half that has to be
    right: a hand-rolled `call_tool` was tried first and its teardown left the
    stream open, which made this test assert a departure that had not happened
    yet.  A test of a held call has to hold it the way the caller does.

    What is asserted is the effect, *while the server is still up*: the teardown
    below would disconnect every mesh anyway, so a check made after it would pass
    for the wrong reason.
    """
    joined, left = _a_mesh_that_records_its_ends(monkeypatch)
    # The cadence, not the rule: the plugin's keepalive is what makes a closed
    # stream noticeable at all (`_hold_open`), and the real twenty-five seconds
    # would be a test that waits for them.
    monkeypatch.setattr(_T.timeouts.deliver, "keepalive", 0.01)
    config = default_config()
    object.__setattr__(config, "a2a", settings())
    mcp = build_server(
        config, source=lambda: config, window_client=window.client, connecting=False
    )

    async with served(mcp) as url:
        mesh = MCPMeshClient(url, agent=AGENT)
        await mesh.connect()
        holding = asyncio.create_task(mesh.hold())
        try:
            await until(lambda: joined == [AGENT], "the join never reached the mesh")
            assert left == [], "offline before the window went anywhere"

            # The window goes away: the call it was holding is cancelled, and the
            # client that held it is closed — which is `SlifeApp.on_unmount`'s
            # two lines, and the whole of what a departure is.
            holding.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await holding
            await mesh.close()

            await until(
                lambda: left == [AGENT],
                "the agent stayed on the mesh — a window that closed was never "
                "noticed, which is the departure this test exists to prove",
                # Not five seconds: the plugin's promise is *one keepalive*
                # (mocked to nothing above, from twenty-five seconds), and the
                # slack is for the machine running the whole suite at once.
                seconds=30.0,
            )
        finally:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                holding.cancel()
                await holding
            with contextlib.suppress(Exception):
                await mesh.close()


def _a_mesh_that_records_its_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[str]]:
    """Replace the mesh's two connection ends, and report who joined and left.

    `A2AMesh`'s own lifetime is not what any of this tests: what is under test is
    *when* the plugin decides to put an agent on the mesh and take it off, which
    is the same decision with a broker and without one.

    `joined` is the only signal these tests have that a `join` call got as far as
    the mesh, and it is the honest one: nothing is *drawn* for the agent's own
    arrival (a window showing jack is jack), so there is no line to wait on.
    """
    joined: list[str] = []
    left: list[str] = []

    async def connect(self: Any) -> None:
        self._connected = True
        joined.append(self.agent_name)

    async def disconnect(self: Any) -> None:
        left.append(self.agent_name)

    monkeypatch.setattr(A2AMesh, "connect", connect)
    monkeypatch.setattr(A2AMesh, "disconnect", disconnect)
    return joined, left


def test_a_window_leaving_is_not_the_last_word_when_a_tool_is_still_waiting() -> None:
    """Two reasons to be on the mesh, and the mesh outlives either one alone.

    This is why the reasons are a set rather than a flag, and it is the rule the
    window's departure test above depends on being *wrong* about: a `join` that
    counted as having used the mesh would leave the agent online for the life of
    the process, and a `join` that ignored a tool's reason would take it offline
    while an answer was still on its way.

    `USED` is never released by anything — a task's result may come many turns
    later, and nothing inside this process knows when the last one has arrived —
    which is what makes it the caller's to add and nobody's to remove.
    """
    session = Session(
        agent=AGENT,
        mesh=A2AMesh(default_config().a2a, AGENT),  # opens the ledger
    )

    session.hold(WINDOW)
    assert session.release(WINDOW), "a window alone: its departure is the departure"

    session.hold(WINDOW)
    session.hold(USED)
    assert not session.release(WINDOW), "a tool is still waiting to be answered"
    assert session.release(USED), "the last reason is the one that ends it"
