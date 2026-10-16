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
which of `slife2.libs.harness.handover`'s three answers it was handed.

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
from a2a_over_mqtt import make_properties
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.libs.host.config import (
    BROADCAST,
    CANCEL_TASK,
    LIST_AGENTS,
    LOCAL,
    REMOTE,
    SEND_MESSAGE,
    A2ASettings,
    default_config,
)
from slife2.libs.host.paths import (
    a2a_inbound_path,
    a2a_inbound_paths,
    home,
    in_home,
    register_home,
)
from slife2.libs.host.runtime import read_record
from slife2.libs.host.statefile import reader as read_state_yaml
from slife2.libs.plugin.audience import client_meta, for_the_model
from slife2.plugins.a2a import broker
from slife2.plugins.a2a import server as a2a_server
from slife2.plugins.a2a.inbound_store import InboundStore
from slife2.plugins.a2a.mesh import (
    _MAX_ATTEMPTS,
    COMPLETED,
    FAILED,
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
from slife2.plugins.a2a.server import (
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
from slife2.plugins.a2a.task_store import TaskStore, get_store
from slife2.tui.client import MCPMeshClient

#: Which inbound ledger these tests use.  **A ledger is one file per network**
#: (`Net.ledger`), because what it records is the completion bridges one mesh is
#: holding — so every call that opens or names one says which.  The remote
#: network's is what these tests drive unless one says otherwise; the split
#: itself is `test_each_network_keeps_its_own_ledger`.
LEDGER = REMOTE.ledger

pytestmark = pytest.mark.unit

AGENT = "jack"
PEER = "jill"
TASK = "b1946ac92492d2347c6235b4d2611184"


def settings(**overrides: Any) -> A2ASettings:
    """A configured `a2a:` section, which is what puts a machine on the mesh."""
    base: dict[str, Any] = {"configured": True}
    base.update(overrides)
    return A2ASettings(**base)


# ── the marker: one envelope, written and read ──────────────────────────────


def test_the_marker_carries_from_and_not_agent_name() -> None:
    """`from`, not `agent_name` — v1 measured the confusion that caused.

    "agent_name" is the receiver's *own* identity, so a marker carrying it had
    receivers misreading the sender as themselves.  The *keys* survive the port
    for the reason `[Wechat:{…}]`'s do: a habit is a thing that does not have to
    be relearned.

    **The spelling does not.**  v1 wrote `[A2A:`, and capitals are this build's
    convention for the other kind of bracket — the one that says "this is what
    the process knows, not what was said" (`[TURN:{…}]`).  This marker is
    content, so it is word-shaped like every other one a row carries, and the
    case now says the same thing the marker's life does.

    **And it says `Peer`, not `Agent`.**  The name is read on the line that says
    who spoke, and "an agent" is the one thing it must not leave open: the
    receiver is an agent too, and v1's measured failure was a receiver taking the
    sender for itself — which the `from` key prevents inside the payload and the
    prefix must not undo in front of it.
    """
    assert (
        marker(REMOTE, PEER) == '[PeerAgent:{"from": "jill", "type": "task_request"}] '
    )
    assert marker(REMOTE, PEER, TASK) == (
        f'[PeerAgent:{{"from": "jill", "type": "task_request", "task_id": "{TASK}"}}] '
    )
    assert (
        marker(REMOTE, PEER, None, "message")
        == '[PeerAgent:{"from": "jill", "type": "message"}] '
    )


def test_the_marker_reads_its_own_type_back() -> None:
    """The writer and the reader are one pair, so the vocabulary stays one list."""
    for kind in A2A_TYPES:
        assert a2a_message_type(marker(REMOTE, PEER, TASK, kind)) == kind


def test_a_type_outside_the_vocabulary_reads_as_none() -> None:
    """A peer may put anything in the payload; the set is what this build knows."""
    assert (
        a2a_message_type('[PeerAgent:{"from": "jill", "type": "invented"}] hi') is None
    )
    assert a2a_message_type("no marker here") is None
    assert a2a_message_type("[PeerAgent:{not json] hi") is None
    assert a2a_message_type('[PeerAgent:["a", "list"]] hi') is None
    # A bracket that never closes is a torn row: nothing is read out of it.
    assert a2a_message_type('[PeerAgent:{"from": "jill"') is None


def test_the_channel_names_the_peer() -> None:
    """The window draws a bubble from the channel alone, so the peer is in it."""
    assert channel_for(REMOTE, PEER) == "a2a:jill"
    assert CHANNEL == "a2a"


def test_the_two_networks_say_the_same_things_in_their_own_words() -> None:
    """A vocabulary each, and the one word that differs on purpose.

    The remote network's envelope says `PeerAgent` and the local one's says
    `SubagentPeer`: both keep `Peer`, which is the word that cannot be read as
    the reader — the window's `Subagent(name)>` is the other half of that pair,
    and it *is* a role word, rightly, because the window belongs to the agent
    that made the subagent.
    """
    assert channel_for(LOCAL, PEER) == "subagent:jill"
    assert (
        marker(LOCAL, PEER)
        == '[SubagentPeer:{"from": "jill", "type": "task_request"}] '
    )
    assert LOCAL.tool("send_message") == "subagent_send_message"
    assert REMOTE.tool("send_message") == "a2a_send_message"


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
    (`slife2.plugins.tui.server`'s `note`), and `online` twice in one line is a fact the
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

    def __init__(
        self, topic: str, payload: bytes, status: str = "", corr: str = ""
    ) -> None:
        self.topic = type("Topic", (), {"value": topic})()
        self.payload = payload
        if status:
            self.properties = type(
                "Props", (), {"UserProperty": [("a2a-status", status)]}
            )()
        elif corr:
            # The routing a reply carries: the correlation data its request went
            # out with is the only way back to the send it answers.
            self.properties = make_properties(correlation_data=corr)
        else:
            self.properties = None


def test_an_empty_payload_decodes_to_nothing_rather_than_raising() -> None:
    assert _decode_payload(FakeMessage("t", b"")) is None
    assert _decode_payload(FakeMessage("t", b"not json")) is None
    assert _decode_payload(FakeMessage("t", b'{"a": 1}')) == {"a": 1}
    # A mis-encoded body is replaced rather than fatal — callers drop it.
    assert _decode_payload(FakeMessage("t", b"\xff\xfe")) is None


def test_a_corrupt_payload_is_named_where_an_empty_one_is_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**An empty body is normal; a body that is not JSON is somebody's bug.**

    Both used to answer `None` in silence, so a peer sending a body this build
    cannot read — a different profile, a different version — was dropped with
    nothing anywhere saying which topic it came off.  The empty case stays
    quiet, because a deleted card is exactly that and it is ordinary; the
    corrupt one is a warning naming the topic.
    """
    topic = "a2a/v1/event/org/unit/broadcast"
    with caplog.at_level(logging.WARNING, logger="slife2.plugins.a2a.mesh"):
        assert _decode_payload(FakeMessage(topic, b"")) is None
        assert _decode_payload(FakeMessage(topic, b"not json")) is None

    named = [
        record.getMessage()
        for record in caplog.records
        if "a2a_payload_undecodable" in record.getMessage()
    ]
    assert len(named) == 1, "an empty body is not evidence of anything"
    assert topic in named[0], "the warning does not say where it came from"


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


def test_a_reply_for_a_pruned_task_is_delivered_rather_than_dropped() -> None:
    """**The store evicts; the peer answers anyway.**

    The task store is process-local and drops the oldest *pending* record once
    it is past its cap, so a peer slow enough to answer finishes a task this
    process no longer holds.  The routing table still had the send, and with it
    the agent the completion belongs to — the reply was dropped there in
    silence, which loses a peer's result and leaves the conversation that asked
    waiting for an answer that had already arrived.
    """
    mesh = A2AMesh(default_config().a2a, AGENT, LEDGER)
    corr = "corr-pruned"
    mesh._corr_to_task[corr] = TASK
    mesh._sends[TASK] = _OutboundSend(TASK, PEER, "session", "{}")
    assert get_store().get(TASK) is None, "the state this test is about"

    done: list[tuple[Any, ...]] = []
    mesh.on_task_completion = lambda *args: done.append(args)

    mesh._handle_reply(
        FakeMessage(
            "a2a/v1/agents/jack",
            json.dumps(
                {
                    "result": {
                        "statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}
                    }
                }
            ).encode(),
            corr=corr,
        )
    )

    assert done, "the reply reached nobody"
    task_id, _result, outcome, agent, kind = done[0]
    assert (task_id, outcome, agent, kind) == (TASK, COMPLETED, PEER, "task")
    assert TASK not in mesh._sends, "and the send is finished either way"


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
    mesh = A2AMesh(default_config().a2a, AGENT, LEDGER)
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
    store = InboundStore.open(AGENT, LEDGER)
    store.add(TASK, PEER)
    assert [t.task_id for t in store.stale()] == []
    # On disk, with the owner named — the directory holds the sanitised name.
    raw = json.loads(json.dumps(_read_yaml(a2a_inbound_path(AGENT, LEDGER))))
    assert raw["agent"] == AGENT
    assert TASK in raw["pending"]
    assert raw["pending"][TASK]["peer"] == PEER

    store.drop(TASK)
    assert _read_yaml(a2a_inbound_path(AGENT, LEDGER))["pending"] == {}


def test_a_restart_tells_its_orphans_from_ids_it_has_never_seen() -> None:
    """The whole reason this file exists: the bridge died with the process."""
    first = InboundStore.open(AGENT, LEDGER)
    first.add(TASK, PEER)

    second = InboundStore.open(AGENT, LEDGER)
    orphans = second.stale()
    assert [t.task_id for t in orphans] == [TASK]
    assert orphans[0].peer == PEER
    # And it is offered to the model as an id that can never be completed.
    assert second.stale_entry(TASK) is not None
    assert second.stale_entry("never-seen") is None


def test_a_retry_revives_a_task_its_predecessor_dropped() -> None:
    """A peer that re-sends the same task id re-registers the bridge."""
    InboundStore.open(AGENT, LEDGER).add(TASK, PEER)
    second = InboundStore.open(AGENT, LEDGER)
    assert second.stale() != []
    second.add(TASK, PEER)
    assert second.stale() == []


def test_answering_a_peer_clears_what_that_peer_is_owed() -> None:
    """A plain message is the only reply an orphan can still get, so it pays the
    debt — and only that peer's, because the others are still waiting."""
    first = InboundStore.open(AGENT, LEDGER)
    first.add(TASK, PEER)
    first.add("other", "someone-else")

    # A set, not a list: the two arrived inside the same second, so the
    # oldest-first ordering the notice uses has nothing to order them by.
    store = InboundStore.open(AGENT, LEDGER)
    assert {t.task_id for t in store.stale()} == {"other", TASK}

    store.clear_stale_peer(PEER)
    assert [t.task_id for t in store.stale()] == ["other"]


def test_completing_an_orphan_is_refused_with_the_reply_that_still_works() -> None:
    """The three ways an id can be unknown need three different sentences, and
    the orphan's is the one a model cannot work out for itself: no result can
    ever complete it, so the answer is a plain message instead."""
    from slife2.plugins.a2a.mesh import A2AMesh

    InboundStore.open(AGENT, LEDGER).add(TASK, PEER)
    mesh = A2AMesh(
        default_config().a2a, AGENT, LEDGER
    )  # opens the ledger: TASK → stale

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
    orphan = a2a_inbound_path(AGENT, LEDGER)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("pending: {}\nstale: {}\n", encoding="utf-8")
    assert InboundStore.at(orphan) is None


def test_the_ledger_is_how_a_shared_process_finds_the_agents_it_serves() -> None:
    InboundStore.open(AGENT, LEDGER)
    InboundStore.open("someone-else", LEDGER)
    found = {InboundStore.at(one.path).agent for one in a2a_inbound_paths(LEDGER)}  # type: ignore[union-attr]
    assert found == {AGENT, "someone-else"}


def test_each_network_keeps_its_own_ledger() -> None:
    """**One agent, two networks, two files** — and neither reads the other's.

    Both networks are served by this one module and a machine may configure
    both, so one shared file meant the process serving the local mesh opened the
    *remote* mesh's file and read its live `pending` tasks as a dead
    predecessor's orphans — telling the model that a task whose bridge was
    answering could never be completed — and then rewrote the whole file from
    its own memory, erasing whatever the other process had added since.  That
    last part is the one fact the file exists to keep: a peer is owed an answer.

    Two files rather than a guard, because of what the file records: the
    completion bridges *this* mesh is holding, which is process-local by
    construction and invisible to the other process — no liveness check can tell
    one process's live task from another's orphan when neither can see the
    other's bridges.
    """
    InboundStore.open(AGENT, REMOTE.ledger).add(TASK, PEER)

    # The local network's process starts.  Its ledger is its own, so the remote
    # mesh's task is not an orphan it may announce as unanswerable.
    InboundStore.open(AGENT, LOCAL.ledger)
    theirs = _read_yaml(a2a_inbound_path(AGENT, LOCAL.ledger))
    assert theirs["pending"] == {} and theirs["stale"] == {}, "its own file, empty"

    # ...and that save left the remote one's task exactly where it was.
    ours = _read_yaml(a2a_inbound_path(AGENT, REMOTE.ledger))
    assert TASK in ours["pending"], "the other network's task survived"

    assert REMOTE.ledger != LOCAL.ledger
    assert {one.path.name for one in a2a_inbound_paths(LOCAL.ledger)} == {LOCAL.ledger}
    assert {one.path.name for one in a2a_inbound_paths(REMOTE.ledger)} == {
        REMOTE.ledger
    }


def test_the_ledger_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each `add` is a whole-file write with an fsync, so the cap is turned down
    rather than the writes being made 200 times: the property is the trimming,
    and the number is asserted rather than exercised."""
    monkeypatch.setattr("slife2.plugins.a2a.inbound_store._MAX_ENTRIES", 5)

    store = InboundStore.open(AGENT, LEDGER)
    for index in range(9):
        store.add(f"task{index}", PEER)
    assert len(_read_yaml(a2a_inbound_path(AGENT, LEDGER))["pending"]) == 5
    # The oldest went, because the newest is the one still owed an answer.
    assert store._pending.get("task0") is None
    assert store._pending.get("task8") is not None


def _read_yaml(path: Path) -> dict:
    """The ledger as it is on disk, read the way the plugin reads it."""
    return read_state_yaml().load(path.read_text(encoding="utf-8"))


def test_a_damaged_ledger_is_refused_rather_than_read_as_empty() -> None:
    """**The write after the read is what makes the loss permanent.**

    A corrupt file was logged and answered with an empty store, and the very
    next save wrote that empty store over it — so the evidence was consumed
    with the data.  What it held is the one thing the file exists for: the peers
    a previous process died owing an answer, which then went neither answered
    nor reported (DESIGN.md §10.1).  The read raises naming the file instead,
    and because it runs before `open`'s write, nothing is overwritten.
    """
    path = a2a_inbound_path(AGENT, LEDGER)
    path.parent.mkdir(parents=True, exist_ok=True)
    damaged = "agent: jack\npending: {\n"  # a flow mapping that never closes
    path.write_text(damaged, encoding="utf-8")

    with pytest.raises(ValueError, match="could not be read"):
        InboundStore.open(AGENT, LEDGER)

    assert path.read_text(encoding="utf-8") == damaged, "the evidence survived"


def test_a_ledger_section_that_is_not_a_mapping_is_refused_rather_than_read_as_empty() -> (
    None
):
    """**The same loss as the file-level read, one level down** (DESIGN.md §10.1).

    A section that was not a mapping read as `{}`, and the next `_save` wrote
    that empty section back — so a peer a previous process died owing an answer
    was dropped from a file whose whole purpose is to remember it.  The section
    is refused instead, naming the section and the file.
    """
    path = a2a_inbound_path(AGENT, LEDGER)
    path.parent.mkdir(parents=True, exist_ok=True)
    damaged = "agent: jack\npending:\n  - not-a-mapping\nstale: {}\n"
    path.write_text(damaged, encoding="utf-8")

    with pytest.raises(ValueError, match="pending"):
        InboundStore.open(AGENT, LEDGER)

    assert path.read_text(encoding="utf-8") == damaged, "the evidence survived"


def test_a_ledger_entry_that_is_not_a_mapping_is_refused_rather_than_dropped() -> None:
    """**An entry that cannot be read is damage, not an absent entry.**

    A non-mapping row was dropped with no word, and the next `_save` wrote the
    section without it (DESIGN.md §10.1) — the orphaned peer vanished from the
    one record of the reply it is still owed.  The row is named instead.
    """
    path = a2a_inbound_path(AGENT, LEDGER)
    path.parent.mkdir(parents=True, exist_ok=True)
    damaged = f"agent: jack\npending:\n  {TASK}: not-a-mapping\nstale: {{}}\n"
    path.write_text(damaged, encoding="utf-8")

    with pytest.raises(ValueError, match=TASK):
        InboundStore.open(AGENT, LEDGER)

    assert path.read_text(encoding="utf-8") == damaged, "the evidence survived"


def test_a_ledger_write_that_failed_is_findable_rather_than_whispered(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**This file is the only line between an orphaned task and one never seen**
    (§1.8), so a write that does not land costs exactly the fact the next
    process would have read.  It is still not the caller's failure — a turn must
    not fail over a bookkeeping write — so what changes is where it goes: an
    error naming the file, which is the only place an operator will see it.
    """
    store = InboundStore.open(AGENT, LEDGER)
    path = a2a_inbound_path(AGENT, LEDGER)
    before = path.read_text(encoding="utf-8")

    def refuse(src: Any, dst: Any) -> None:
        raise OSError("the disk is full")

    monkeypatch.setattr(os, "replace", refuse)
    caplog.set_level(logging.ERROR, logger="slife2.plugins.a2a.inbound_store")
    store.add(TASK, PEER)

    assert path.read_text(encoding="utf-8") == before, "the write did land"
    assert any(str(path) in record.getMessage() for record in caplog.records), (
        "the task that was never written down left nothing for an operator"
    )


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
    """**A finished task is the one nothing is waiting on**, so the prune spends
    it before it spends a pending one — which is the record a late result still
    has to be routed to.

    The test that claimed to check this never did.  It filled the store with
    *pending* records and marked its own record completed after the last prune,
    so the sort's terminal-first clause was never consulted — every record the
    prune could see was pending — and its final assertion was unfailable besides
    (the record it looked for was the newest, and a prune takes the oldest).
    The finished record is seeded before the prune that has to choose, and it is
    the *newest* record in the store: only the terminal-first clause can pick it.
    """
    store = TaskStore()
    for index in range(TaskStore.MAX_RECORDS - 1):
        store.record_send(f"pending{index}", PEER)
    finished = store.record_send("finished", PEER)
    store.record_result("finished")
    assert finished.status == "completed"
    assert len(store._records) == TaskStore.MAX_RECORDS, "the cap has not been reached"

    # One more send is what pushes the store over the cap, and therefore what
    # prunes it.
    store.record_send("last", PEER)

    assert len(store._records) <= TaskStore.MAX_RECORDS
    assert store.get("finished") is None, "a finished task outlived a pending one"
    assert store.get("last") is not None, "the record just written was pruned"
    # And the pending records went in age order after it: the oldest first.
    assert store.get("pending0") is None
    assert store.get("pending50") is not None


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


def test_a_broker_that_did_not_exit_keeps_the_record_that_finds_it() -> None:
    """**A record cleared too early is a live broker nothing can find again.**

    `stop` cleared the record and *then* read `kill`'s answer, so the one case
    that most needs the record — a mosquitto that would not exit — was the case
    that lost it.  The next `slife2 down` reads no record, probes the port and
    answers "not started by slife2; left alone": a sentence about somebody
    else's daemon, about a process this one started and could not stop.  The
    launcher's own stop keeps the record for exactly this reason, and the answer
    says so, because the operator reading it is the one who has to go and look.
    """
    answers = iter([False, True])

    class Child:
        returncode = None
        pid = os.getpid()

        def poll(self) -> None:
            return None

    broker.ensure(
        settings(),
        listening=lambda host, port: next(answers),
        which=lambda name: "/usr/bin/mosquitto",
        start=lambda argv, *, url: Child(),
    )
    assert read_record(broker.log_url(settings())) is not None

    said = broker.stop(settings(), kill=lambda pid: False)

    assert "did not exit" in said and "the record is kept" in said
    kept = read_record(broker.log_url(settings()))
    assert kept is not None and kept.pid == os.getpid(), (
        "the surviving pid is the one the next take-down has to signal"
    )


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

    Two tools, because the window has two kinds of arrival (`slife2.plugins.tui.server`):
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


@pytest_asyncio.fixture(loop_scope="function")
async def window() -> AsyncIterator[Window]:
    node = Window()
    async with Client(node.mcp) as client:
        node.client = client
        yield node


# ── what the mesh delivers, and where it goes ───────────────────────────────
#
#  A real `A2AMesh` is constructed and never connected — its constructor dials
#  nothing — so what runs here is the genuine callback wiring, driven the way the
#  MQTT reader would drive it.


def wired(window: Window | None = None) -> Session:
    """A real mesh, never connected, with the plugin's callbacks on it.

    `A2AMesh.__init__` builds topics, a card and the agent's ledger and dials
    nothing, so this is the genuine object and the genuine wiring — what a test
    replaces is the *messenger* on the far end, never the mesh and never
    `slife2.libs.harness.handover`'s policy.
    """
    from slife2.plugins.a2a.mesh import A2AMesh

    session = Session(
        agent=AGENT, net=REMOTE, mesh=A2AMesh(default_config().a2a, AGENT, LEDGER)
    )
    _wire(session)
    return session


async def drain_until_idle(
    session: Session,
    *,
    window: Window | None = None,
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
    assert handed["text"] == f"{marker(REMOTE, PEER, TASK)}write me a poem"
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


#: What a peer calls itself when it wants to be mistaken for a line of the
#: window's own drawing: a name that ends the bubble it is the prefix of, opens
#: one of somebody else's, and carries an ESC.
FORGED_PEER = "jill\n✗ offline\x1b[31m"


@pytest.mark.asyncio
async def test_no_wire_peer_can_forge_a_line_in_the_channel_a_message_arrives_on(
    window: Window,
) -> None:
    """**The peer's name *becomes* the channel**, and the channel is drawn.

    `safe_name`'s own docstring gives the reason — these fields come off the
    wire with nothing having validated them, and a control character in one lets
    a peer "break out of a single display line in a handed-over message, the
    TUI, or logs" — and only one of the four callbacks applied it.  The other
    three pass their peer into `session.post` untouched, and that string is the
    second half of `channel_for`: what the window renders verbatim as the bubble
    prefix (`slife2.tui.widgets.channel_prefix`), what the turn row stores, and
    what the writer's log lines name.  So a peer calling itself `jill` plus a
    newline and `✗ offline` forged an arrival line and a departure line in every
    window showing the agent's conversation.  The marker itself is safe —
    `json.dumps` escapes the control characters — which is why this asserts on
    the channel, the stored peer and the log rather than on the text.

    Three callbacks in one test because they are one rule: the fourth
    (`on_agent_change`) already scrubs, and a rule applied by three of four
    callers is the bug.
    """
    session = wired(window=window)
    session.mesh.on_inbound_task(FORGED_PEER, "hello", TASK)
    session.mesh.on_peer_cancel(TASK, FORGED_PEER)
    session.mesh.on_broadcast_event(FORGED_PEER, "the build is green")
    await drain_until_idle(session, window=window)

    assert len(window.handed) == 3, "the three arrivals, and nothing else"
    safe = safe_name(FORGED_PEER)
    assert safe != FORGED_PEER, "the fixture is not a name that needed scrubbing"
    for handed in window.handed:
        assert handed["channel"] == f"{CHANNEL}:{safe}"
        assert "\n" not in handed["channel"] and "\x1b" not in handed["channel"], (
            "a peer's name opened a line of its own in the window"
        )


@pytest.mark.asyncio
async def test_the_log_line_for_an_arrival_names_the_scrubbed_peer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The channel is not the only reader: `what` is the writer's own line.

    A delivery that reaches no conversation is logged with the peer it was for
    (`a2a_delivery_unreachable`), and that line is read in a terminal — so the
    name in it has to be the same scrubbed one the window was given, not the
    name off the wire.
    """
    monkeypatch.setattr(_T.timeouts.pacing, "reconnect", 0.01)
    caplog.set_level(logging.ERROR, logger="slife2.plugins.a2a.server")
    refusing = Window(refuse=True)
    async with Client(refusing.mcp) as refusing_client:
        refusing.client = refusing_client
        session = wired(window=refusing)
        session.mesh.on_broadcast_event(FORGED_PEER, "hello")
        await writer_until(
            session,
            lambda: any(
                "a2a_delivery_unreachable" in record.getMessage()
                for record in caplog.records
            ),
            window=refusing,
        )

    said = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("a2a_delivery_unreachable")
    ]
    assert said, "a refused delivery left no line naming the peer"
    for line in said:
        assert f"peer={safe_name(FORGED_PEER)} " in line
        assert "\n" not in line and "\x1b" not in line, "the log line was forged too"


@pytest.mark.asyncio
async def test_the_line_a_presence_note_logs_is_scrubbed_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fourth callback, and the one whose `what` is a sentence rather than a name.

    `on_agent_change` scrubbed the peer it *stores* and left the raw
    `card.agent_name` in the delivery's own description — which reaches a log
    line on the one path a presence note has when nobody is looking
    (`a2a_note_no_window … line=<what>`).  A name carrying a newline forged that
    line, which is the third reader `safe_name`'s docstring names and the same
    defect the three arrival callbacks were fixed for.

    The window is not the exposure here: `format_presence_line` renders the
    scrubbed name, so what is asserted is the log alone.
    """
    from slife2.plugins.a2a.mesh import AgentCard

    caplog.set_level(logging.DEBUG, logger="slife2.plugins.a2a.server")
    session = wired()  # no window: the note is drawn nowhere and said to the log
    session.mesh.on_agent_change(
        AgentCard(agent_name=AgentName(FORGED_PEER), status="online"), "online"
    )
    await drain_until_idle(session)

    said = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("a2a_note_no_window")
    ]
    assert said, "the note reached no window and left no line saying so"
    for line in said:
        assert f"line={safe_name(FORGED_PEER)} is online" in line
        assert "\n" not in line and "\x1b" not in line, "the log line was forged too"


@pytest.mark.asyncio
async def test_an_outbound_result_is_pushed_back_wearing_its_task_id(
    window: Window,
) -> None:
    session = wired(window=window)
    session.mesh.on_task_completion(TASK, "the poem", COMPLETED, PEER, "task")
    await drain_until_idle(session, window=window)

    text = window.handed[-1]["text"]
    assert a2a_message_type(text) == "task_response"
    assert f'"task_id": "{TASK}"' in text
    assert "completed async task" in text
    assert "the poem" in text


@pytest.mark.asyncio
async def test_a_peer_that_answered_with_a_failure_does_not_read_as_completed(
    window: Window,
) -> None:
    """**The peer's own word, and it is not the happy one.**

    `_reply_state` distinguishes three endings, and the callback carried one of
    them (`cancelled`) as a flag — so `TASK_STATE_FAILED`, which is what the SDK
    publishes when a peer's handler raises, arrived at the renderer as
    "not cancelled" and went into the conversation as "Peer X **completed**
    async task", in the same delivery that recorded `failed` on the task's row.
    The model was told the opposite of what happened, about a task it had asked
    for and would now not re-send.  The store and the sentence are one verdict
    or the sentence is a lie.
    """
    session = wired(window=window)
    session.mesh.on_task_completion(TASK, "the handler raised", FAILED, PEER, "task")
    await drain_until_idle(session, window=window)

    text = window.handed[-1]["text"]
    assert "failed async task" in text
    assert "completed" not in text
    assert "the handler raised" in text


@pytest.mark.asyncio
async def test_a_send_the_requester_ladder_gave_up_on_tells_the_conversation(
    window: Window, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The promise `a2a_send_message` makes is the one this keeps.**

    The tool answers "The result will arrive in this conversation", so a send
    that dies on the retry ladder with one `warning` leaves the model waiting
    for an arrival nothing will bring — and the task row `pending` for ever.
    The ladder's last word now takes the completion callback a terminal reply
    takes, wearing the failure's own words and marked `failed` so the envelope
    does not call it an answer.  The record is left `pending` on purpose: no
    reply arrived, so the peer's real ending is unknown, and a late terminal
    still routes and records `completed` over it (DESIGN.md §10.1).
    """
    session = wired(window=window)
    # An id of this test's own: the store is the process's singleton, and a row
    # left behind under a shared id would be another test's surprise.
    unacked = "task-nobody-acknowledged"
    send = _OutboundSend(unacked, PEER, "session", "{}")
    send.attempts = _MAX_ATTEMPTS  # the ladder is spent before it starts
    get_store().record_send(unacked, PEER)
    monkeypatch.setattr(_T.timeouts.deliver, "reply_first", 0.0)

    await session.mesh._deliver(send)
    await drain_until_idle(session, window=window)

    text = window.handed[-1]["text"]
    assert a2a_message_type(text) == "task_response"
    assert f'"task_id": "{unacked}"' in text, "the id the model was given is here"
    assert "never reached" in text
    assert "never acknowledged" in text
    assert "completed async task" not in text, "nothing may be read as an answer"
    assert get_store().get(unacked).status == "pending", "a failure is not a verdict"


@pytest.mark.asyncio
async def test_a_name_probe_that_failed_is_not_a_free_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A probe that could not run is not a probe that found nothing**
    (DESIGN.md §10.1).

    `_probe_own_name` answers the one verdict nothing else on the wire can:
    whether another agent already holds this name.  Answering `None` when its
    own connection failed read as "the name is free", and a name held in
    duplicate is exactly the case the check exists for — every task sent to it
    would be executed twice.  The failure is the connect's now, named for the
    broker the probe could not ask.
    """

    class Refusing:
        def __init__(self, **kwargs: Any) -> None:
            raise OSError("the broker refused the probe connection")

    mesh = A2AMesh(default_config().a2a, AGENT, LEDGER)
    monkeypatch.setattr("slife2.plugins.a2a.mesh.aiomqtt.Client", Refusing)

    with pytest.raises(RuntimeError, match="name probe"):
        await mesh.connect()


@pytest.mark.asyncio
async def test_a_failed_retry_publish_is_a_warning_naming_the_task_and_attempt(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**An attempt that failed is not an attempt that has not run.**

    Each failed rung of the retry ladder was a `debug` line with a `continue`,
    so the attempt's failure was invisible until the whole budget ran out — and
    the two failures tell an operator different things: one peer that never
    hears, against a broker this process cannot publish to at all.  The warning
    names the task and which rung it was on.
    """
    mesh = A2AMesh(default_config().a2a, AGENT, LEDGER)
    send = _OutboundSend("task-a-retry-that-failed", PEER, "session", "{}")

    async def lands(topic: str, payload: str, props: Any, *, qos: int = 1) -> None:
        return None

    real = mesh._publish_attempt
    rungs = 0

    async def refuse_the_first(send_: _OutboundSend, corr: str) -> None:
        """One rung that cannot be published, and then a link that works.

        Only the first, because a failed publish does not spend the ladder
        (`_deliver`): a fake that refused every publish would leave the loop
        retrying this rung for ever, which is the *point* of that fix and not
        something this test is about.
        """
        nonlocal rungs
        rungs += 1
        if rungs == 1:
            raise RuntimeError("the broker refused the publish")
        await real(send_, corr)

    monkeypatch.setattr(mesh, "_publish_props", lands)
    monkeypatch.setattr(mesh, "_publish_attempt", refuse_the_first)
    monkeypatch.setattr(_T.timeouts.deliver, "reply_first", 0.0)
    monkeypatch.setattr("slife2.plugins.a2a.mesh._backoff_delay", lambda attempt: 0.0)

    with caplog.at_level(logging.WARNING, logger="slife2.plugins.a2a.mesh"):
        await mesh._deliver(send)

    failed = [
        record.getMessage()
        for record in caplog.records
        if "a2a_delivery_retry_failed" in record.getMessage()
    ]
    assert failed, "a failed retry left no trace beyond the exhausted ladder"
    assert all(send.task_id in line for line in failed), "the task is not named"
    assert any("attempt=2" in line for line in failed), "the rung is not named"


@pytest.mark.asyncio
async def test_a_publish_that_never_left_is_not_a_rung_of_the_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The ladder counts what a peer could have seen.**

    `_deliver` incremented `send.attempts` before `_publish_attempt` and left
    the count standing when that raised — which is exactly what a mesh does
    while its outbound client is mid-reconnect (`_publish_props` answers
    `A2A mesh is not connected.`).  The first two backoffs are ~1 s and ~2 s, so
    a broker or outbound outage of three or four seconds spent the whole budget
    without a single publish leaving: the send was abandoned, the conversation
    was pushed "it was sent 3 times over the requester retry ladder and never
    acknowledged" — a count of things no peer could have received — and nothing
    retried it when the link came back, so the task row `pending` that
    `a2a_send_message` promised a result for stayed `pending` for ever.

    The link is down for four publishes and then up, and both halves of that
    are asserted: the send is still there when the link returns, and the rungs
    it then spends are its own.
    """
    mesh = A2AMesh(default_config().a2a, AGENT, LEDGER)
    send = _OutboundSend("task-the-link-was-down-for", PEER, "session", "{}")
    published: list[str] = []
    down = 4

    async def lands(topic: str, payload: str, props: Any, *, qos: int = 1) -> None:
        published.append(topic)

    real = mesh._publish_attempt

    async def a_link_that_drops(send_: _OutboundSend, corr: str) -> None:
        nonlocal down
        if down:
            down -= 1
            raise RuntimeError("A2A mesh is not connected.")
        await real(send_, corr)

    monkeypatch.setattr(mesh, "_publish_props", lands)
    monkeypatch.setattr(mesh, "_publish_attempt", a_link_that_drops)
    monkeypatch.setattr(_T.timeouts.deliver, "reply_first", 0.0)
    monkeypatch.setattr("slife2.plugins.a2a.mesh._backoff_delay", lambda attempt: 0.0)

    await mesh._deliver(send)

    # `send.attempts` starts at one: the send's own publish, which `send_message`
    # makes before `_deliver` is started.  The two retries the profile allows are
    # the two that follow, and both of them have to reach the wire.
    assert len(published) == _MAX_ATTEMPTS - 1, (
        f"the ladder was spent on publishes that never left: "
        f"{len(published)} of {_MAX_ATTEMPTS - 1} rungs reached the broker"
    )
    assert send.attempts == _MAX_ATTEMPTS, "the rungs that landed are not counted"


@pytest.mark.asyncio
async def test_a_reconnect_does_not_destroy_a_send_still_on_the_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A broker blip must not eat a message the conversation was promised.**

    A connect attempt on a mesh whose link had just dropped began with
    `disconnect()`, which cancelled every `_deliver` task and cleared the
    correlation routes — so a send that was between two rungs of its ladder
    (with both `a2a_send_message`'s "The result will arrive in this
    conversation" and the retry profile still standing) died there: no terminal
    reply could route afterwards (`a2a_reply_unknown_corr`, its correlation
    gone), no `UNREACHABLE` notice came out of the ladder, and the task row
    stayed `pending` for ever.  The teardown a *reconnect* makes is a revival —
    the mesh is being rebuilt, not abandoned — so the ladder rides over it and
    the message goes out when the link is back.

    The connect is driven to its teardown and no further: the name probe is what
    runs next, and a name held elsewhere is a `RuntimeError` with no broker to
    dial.
    """

    async def the_name_is_taken() -> str:
        return "another A2A instance holds the name"

    mesh = A2AMesh(default_config().a2a, AGENT, LEDGER)
    monkeypatch.setattr(mesh, "_probe_own_name", the_name_is_taken)
    send = _OutboundSend("task-in-flight-over-a-reconnect", PEER, "session", "{}")
    mesh._sends[send.task_id] = send

    published: list[str] = []
    down = True

    async def lands(topic: str, payload: str, props: Any, *, qos: int = 1) -> None:
        published.append(topic)

    real_attempt = mesh._publish_attempt

    async def a_link_that_is_down(send_: _OutboundSend, corr: str) -> None:
        if down:
            raise RuntimeError("A2A mesh is not connected.")
        await real_attempt(send_, corr)

    monkeypatch.setattr(mesh, "_publish_props", lands)
    monkeypatch.setattr(mesh, "_publish_attempt", a_link_that_is_down)
    monkeypatch.setattr(_T.timeouts.deliver, "reply_first", 0.0)
    monkeypatch.setattr("slife2.plugins.a2a.mesh._backoff_delay", lambda attempt: 0.0)

    loop = asyncio.create_task(mesh._deliver(send))
    send.loops.add(loop)
    await asyncio.sleep(0.05)  # it is on the ladder, and its rung cannot land

    with pytest.raises(RuntimeError, match="holds the name"):
        await mesh.connect()

    assert not loop.done(), "the connect attempt destroyed a send that was in flight"
    assert loop in send.loops and send.task_id in mesh._sends

    # The link comes back, and the rung the send was owed reaches the broker.
    down = False
    await asyncio.wait_for(loop, timeout=5.0)
    assert published, "the send was never published once the mesh was back"


@pytest.mark.asyncio
async def test_a_teardown_that_ends_a_send_tells_the_conversation(
    window: Window,
) -> None:
    """**A cancellation was the one exit that reached nobody.**

    `_deliver`'s contract is that the ladder's ending is told to the
    conversation and not only to the log — a task row stays `pending` for ever
    otherwise, over a message `a2a_send_message` promised a result for.  A
    teardown that ends a ladder takes the same callback a terminal reply takes,
    wearing `UNREACHABLE`: no peer said anything, and whether the peer has the
    message is exactly what is unknown once the mesh has gone.
    """
    session = wired(window=window)
    send = _OutboundSend(TASK, PEER, "session", "{}")
    session.mesh._sends[send.task_id] = send
    loop = asyncio.create_task(session.mesh._deliver(send))
    send.loops.add(loop)
    await asyncio.sleep(0)  # on the ladder, waiting out its first interval

    await session.mesh.disconnect()
    await drain_until_idle(session, window=window)

    text = window.handed[-1]["text"]
    assert a2a_message_type(text) == "task_response"
    assert f'"task_id": "{TASK}"' in text, "the id the model was given is here"
    assert "never reached" in text, "it must not read as a peer's answer"
    assert "mesh went down while this was still being sent" in text


class _OneMessageBroker:
    """An `aiomqtt.Client` that hands over one message and then closes.

    The outbound loop's reconnect cycle is not worth reproducing; what it needs
    is a client whose reader ends for a reason the loop can answer, which is
    `__aexit__` setting `_closing` on the mesh it belongs to.
    """

    def __init__(self, msg: Any, mesh: A2AMesh) -> None:
        self._msg = msg
        self._mesh = mesh
        self.entries = 0

    async def __aenter__(self) -> _OneMessageBroker:
        self.entries += 1
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        self._mesh._closing = True
        return False

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        return None

    @property
    def messages(self) -> AsyncIterator[Any]:
        async def reading() -> AsyncIterator[Any]:
            yield self._msg

        return reading()


@pytest.mark.asyncio
async def test_a_handler_bug_in_the_outbound_loop_is_logged_not_read_as_a_drop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**A bug in a reply handler used to look exactly like a dropped line.**

    `AttributeError` and `ValueError` were caught beside `MqttError` and thrown
    away as transport noise — which is what a handler reading a key a body no
    longer carries raises.  So the client reconnected with nobody the wiser, and
    the clause below, written to log exactly this class of failure, could never
    see one.
    """
    mesh = A2AMesh(default_config().a2a, AGENT, LEDGER)

    def bug(_msg: Any) -> None:
        raise AttributeError("a body with no 'result' at all")

    monkeypatch.setattr(mesh, "_handle_message", bug)
    broker = _OneMessageBroker(
        FakeMessage("a2a/v1/reply/org/unit/jack/s1", b"{}"), mesh
    )
    monkeypatch.setattr(
        "slife2.plugins.a2a.mesh.aiomqtt.Client", lambda **kwargs: broker
    )

    with caplog.at_level(logging.ERROR, logger="slife2.plugins.a2a.mesh"):
        await mesh._run_outbound()

    assert broker.entries == 1
    assert any(
        "a2a_outbound_loop_error" in record.getMessage() and record.exc_info is not None
        for record in caplog.records
    ), "the handler bug left no trace anywhere"


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
async def test_a_presence_line_with_no_window_to_draw_it_goes_nowhere() -> None:
    """A line is not a message, and is not retried or carried anywhere.

    A message that reached no conversation is a hard failure, because a message
    *is* work for the conversation.  A line about a moment is not: running a
    model turn for one costs a call to tell somebody a fact they cannot act on,
    so it is dropped and the log says so (`a2a_note_no_window`).
    """
    session = wired()
    session.mesh.on_agent_change(
        AgentCard(agent_name=AgentName(PEER), status="online"), "online"
    )
    await drain_until_idle(session)

    assert session.queue.empty(), "and nothing was left behind for a retry"


@pytest.mark.asyncio
async def test_an_empty_result_pushes_nothing(window: Window) -> None:
    session = wired(window=window)
    session.mesh.on_task_completion(TASK, "", COMPLETED, PEER, "task")
    await drain_until_idle(session, window=window)
    assert window.handed == []


@pytest.mark.asyncio
async def test_a_message_is_handed_to_the_window(window: Window) -> None:
    """§1.2's policy, and there is one road in it: the window is where a message
    becomes a turn somebody sees, and a conversation with no window is not a
    conversation with fewer readers — it is one nobody is running."""
    session = wired(window=window)
    session.mesh.on_inbound_task(PEER, "hello", TASK)
    await drain_until_idle(session, window=window)

    assert len(window.handed) == 1


@pytest.mark.asyncio
async def test_a_refusing_window_is_a_failure_and_nothing_else_is_tried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The fallback is gone, so a refusal is the end of that message.

    It used to be handed to the agent server — a turn nobody was watching, run
    because the window was not there — which is the one outcome a channel whose
    whole point is the terminal must not have.  What a caller gets now is a
    failure it cannot mistake for success, logged where the peer it belongs to is
    named, because the peer is the only party who can send the task again.
    """
    monkeypatch.setattr(_T.timeouts.pacing, "reconnect", 0.01)
    caplog.set_level(logging.ERROR, logger="slife2.plugins.a2a.server")
    refusing = Window(refuse=True)
    async with Client(refusing.mcp) as window_client:
        refusing.client = window_client
        session = wired(window=refusing)
        session.mesh.on_inbound_task(PEER, "hello", TASK)
        await writer_until(
            session,
            lambda: any(
                "a2a_delivery_unreachable" in record.getMessage()
                for record in caplog.records
            ),
            window=refusing,
        )

    assert refusing.handed == []
    assert session.queue.empty(), "a peer's message is not this plugin's to retry"
    assert any(
        "a2a_delivery_unreachable" in record.getMessage()
        and PEER in record.getMessage()
        for record in caplog.records
    )


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
    caplog.set_level(logging.ERROR, logger="slife2.plugins.a2a.server")
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
    InboundStore.open(AGENT, LEDGER).add(TASK, PEER)
    session = wired()

    announce_orphans(session)

    text, peer, what, retries, note = session.queue.get_nowait()
    assert session.queue.empty(), "one notice, and nothing else on the queue"
    assert peer == ORPHAN_PEER
    assert channel_for(REMOTE, peer) == f"{CHANNEL}:{ORPHAN_PEER}"
    assert what == "1 orphaned inbound task(s)"
    assert TASK in text and PEER in text
    # It is a message for the model to act on, not a line for a window to draw.
    assert note is False
    # It carries the retries, and it is the only message that does: everything
    # a peer sent is posted with none.
    assert retries == ORPHAN_RETRIES


@pytest.mark.asyncio
async def test_the_orphan_notice_lands_on_the_channels_the_window_draws_alone() -> None:
    """The queue is what delivers it, and the bubble is `PeerAgent(orphans)> `."""
    InboundStore.open(AGENT, LEDGER).add(TASK, PEER)
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
    """A refused delivery licenses a second attempt for this plugin's own
    notice, and only for that.

    The fake is the cold start exactly: a window that is not showing that
    conversation, so the hand-over is refused and provably wrote nothing.  A
    peer's message has the peer behind it — a peer that gets no answer sends the
    task again, and a second copy of its words is worse than a late one — while
    an orphan notice is a debt nobody else knows about, so it is the one thing
    here that tries again.
    """
    monkeypatch.setattr(_T.timeouts.pacing, "orphan_retry", 0.01)
    # The hand-over's own patience, which every attempt of it pays: three tries
    # at this pace is what an orphan notice waits through.
    monkeypatch.setattr(_T.timeouts.pacing, "reconnect", 0.01)
    monkeypatch.setattr(a2a_server, "ORPHAN_RETRIES", 2)
    caplog.set_level(logging.ERROR, logger="slife2.plugins.a2a.server")

    def attempts(peer: str) -> int:
        return sum(
            1
            for record in caplog.records
            if f"peer={peer} " in record.getMessage()
            and record.getMessage().startswith("a2a_delivery_unreachable")
        )

    refusing = Window(refuse=True)
    async with Client(refusing.mcp) as window_client:
        refusing.client = window_client
        InboundStore.open(AGENT, LEDGER).add(TASK, PEER)
        session = wired()
        announce_orphans(session)
        session.post("a peer's own message", PEER, "a message from jill")

        await writer_until(
            session,
            lambda: attempts(ORPHAN_PEER) == a2a_server.ORPHAN_RETRIES + 1,
            window=refusing,
        )

    # The first attempt and every retry it was given, and no more than that.
    assert attempts(ORPHAN_PEER) == a2a_server.ORPHAN_RETRIES + 1
    # The peer's message: offered once.  A peer re-sends its own task when it
    # gets no answer, and a second copy of its words is worse than a late one.
    assert attempts(PEER) == 1
    assert refusing.handed == []


@pytest.mark.asyncio
async def test_a_notice_the_writer_cannot_judge_is_not_offered_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A call that did not come back stops it, retries or not.

    A stream that died says nothing either way about whether the words landed,
    and a notice arriving twice is how a conversation is told twice about a debt
    it may already have paid.  The refusal path retries; this one may not, and
    the test is the same in both cases: the queue is empty afterwards.
    """
    caplog.set_level(logging.ERROR, logger="slife2.plugins.a2a.server")
    dying = Window(broken=True)
    async with Client(dying.mcp) as client:
        dying.client = client
        InboundStore.open(AGENT, LEDGER).add(TASK, PEER)
        session = wired()
        announce_orphans(session)

        await writer_until(
            session,
            lambda: any(
                "was not repeated" in record.getMessage() for record in caplog.records
            ),
            window=dying,
        )

    assert "was not repeated" in caplog.records[-1].getMessage()
    assert session.queue.empty(), "a call that may have landed is not put back"


@pytest.mark.asyncio
async def test_the_queue_is_bounded_so_a_peer_cannot_grow_this_process() -> None:
    session = wired()
    for index in range(MAX_QUEUED + 5):
        session.mesh.on_inbound_task(PEER, f"message {index}", "")
    assert session.queue.qsize() == MAX_QUEUED
    # The oldest went, which is the one whose moment has most likely passed.
    first = session.queue.get_nowait()
    assert first[0] != f"{marker(REMOTE, PEER, None, 'message')}message 0"


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
    InboundStore.open(AGENT, LEDGER).add(TASK, PEER)  # a ledger, and an orphan in it
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


@pytest.mark.asyncio
async def test_another_homes_orphan_is_announced_in_that_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**The walk finds every home's ledger, and the announcement has to be that
    home's too.**

    One process serves every home, and the walk that finds the ledgers spans
    them all — but the session built out of what it finds was keyed by the agent
    name alone and built in the ambient home, so a start in home A that found
    home B's `jack` ledger did four wrong things at once: its mesh opened a
    *different* file (`<A>/agents/jack/a2a.yaml`, usually absent, which is why
    the notice was empty), the notice about B's peer — who is still owed an
    answer — was handed to A's conversation, a stray empty ledger was written
    in A, and the session cached under `jack` was the one B's own window then
    joined: B's agent came online on the broker with A's ambient home behind it,
    and every inbound task was handed over in A.

    So this asserts the two halves that are visible from outside: the notice is
    delivered at all, and the home it is delivered in is the ledger's.
    """
    other = tmp_path / "other-home"
    with in_home(other):
        InboundStore.open(AGENT, LEDGER).add(TASK, PEER)
    register_home(other)

    #: What `hand_over` would have been called with, and the home it would have
    #: been called *in* — which is what decides the conversation it reaches
    #: (`client_meta` stamps `paths.home()` on the call the window receives).
    said: list[dict[str, str]] = []

    async def recorded(
        config: Any,
        agent: str,
        text: str,
        channel: str,
        *,
        what: str,
        window_client: Any = None,
    ) -> None:
        said.append(
            {"agent": agent, "channel": channel, "text": text, "home": str(home())}
        )

    monkeypatch.setattr(a2a_server, "hand_over", recorded)
    monkeypatch.setattr(a2a_server, "ensure_broker", lambda cfg: "already listening")

    config = default_config()
    object.__setattr__(config, "a2a", settings())
    mcp = build_server(config, source=lambda: config, connecting=True)

    async with Client(mcp):
        deadline = (  # timeouts: the value this test is built around, not a budget
            asyncio.get_running_loop().time() + 5.0
        )  # timeouts: the value this test is built around, not a budget
        while not said:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the orphan notice was never handed over")
            await asyncio.sleep(0.01)

    assert said[0]["home"] == str(other), "the notice was handed over in another home"
    assert said[0]["agent"] == AGENT
    assert TASK in said[0]["text"] and PEER in said[0]["text"]
    # And the daemon's own home is not where the ledger was opened: an agent
    # named in another home's ledger leaves nothing behind here.
    assert not (home() / "agents" / AGENT).exists(), (
        "a session for another home's agent wrote its ledger into this one"
    )


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
    the tool set — the arrangement `slife2.plugins.tui.server`'s `attach` has.
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
async def test_the_local_instance_serves_the_local_network() -> None:
    """One module, two networks, and the second one's vocabulary is its own.

    **This is the whole of what "two instances" means**, and the tool names are
    where it is visible: a model that has a maker's four `a2a_*` tools in front of
    it must be handed `subagent_*` for the network its own subagents are on, or
    the names it leans on to tell the two apart are the same names.

    The *descriptions* move with them, which is the half that is easy to leave
    behind: a tool called `subagent_send_message` that says it sends to "a peer
    on the A2A mesh" teaches a model the opposite of what its name says.
    """
    tools = await build_server(
        default_config(), net=LOCAL, connecting=False
    ).list_tools()
    by_name = {t.name: t for t in tools}

    assert sorted(by_name) == [
        JOIN,
        LOCAL.tool(BROADCAST),
        LOCAL.tool(CANCEL_TASK),
        LOCAL.tool(LIST_AGENTS),
        LOCAL.tool(SEND_MESSAGE),
    ]
    assert A2A_SEND_MESSAGE not in by_name, "and not the remote network's names"

    send = by_name[LOCAL.tool(SEND_MESSAGE)]
    assert send.description.startswith("Send a message to one of your subagents.")
    assert LOCAL.marker in send.description, "its own envelope, in its own text"
    assert REMOTE.marker not in send.description
    assert LOCAL.tool(SEND_MESSAGE) in by_name[LOCAL.tool(BROADCAST)].description


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
        with contextlib.suppress(asyncio.CancelledError):
            await holding

    assert window.noted == [], "the agent's own arrival is not news to itself"
    assert window.handed == []


@pytest.mark.asyncio
async def test_a_connect_that_failed_and_cannot_be_torn_down_is_named(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**A connect that failed is not a mesh that can be left half-up.**

    A responder that advertised before the outbound connect timed out is still
    on the broker, so the teardown after a failed `connect` is what retires it
    (DESIGN.md §1.8).  `suppress(Exception)` made a teardown that raised
    indistinguishable from one that worked: the agent stayed advertised with
    nobody to retire it, and nothing said why.
    """

    async def connect(self: Any) -> None:
        raise RuntimeError("the outbound connect timed out")

    async def disconnect(self: Any) -> None:
        raise RuntimeError("the responder would not let go")

    monkeypatch.setattr(A2AMesh, "connect", connect)
    monkeypatch.setattr(A2AMesh, "disconnect", disconnect)
    config = default_config()
    object.__setattr__(config, "a2a", settings())
    mcp = build_server(config, source=lambda: config, connecting=False)

    caplog.set_level(logging.ERROR, logger="slife2.plugins.a2a.server")
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="could not join the mesh"):
            await client.call_tool(
                A2A_BROADCAST, {"event": "the build is green"}, meta=client_meta(AGENT)
            )

    assert any(
        AGENT in record.getMessage() and "would not be torn down" in record.getMessage()
        for record in caplog.records
    ), "the failed teardown left no trace naming the agent it left advertised"


@pytest.mark.asyncio
async def test_a_mesh_that_will_not_disconnect_is_named_rather_than_swallowed(
    monkeypatch: pytest.MonkeyPatch, window: Window, caplog: pytest.LogCaptureFixture
) -> None:
    """**A teardown that carries a protocol fact is not silent.**

    The offline card is how a peer that watched an agent arrive watches it leave
    (DESIGN.md §1.8), so a `disconnect` that raises as the window closes leaves
    the agent advertised with nobody to retire it.  `suppress(Exception)` made
    that failure indistinguishable from success — the roster said `online` and
    nothing said why.
    """
    joined: list[str] = []

    async def connect(self: Any) -> None:
        self._connected = True
        joined.append(self.agent_name)

    async def disconnect(self: Any) -> None:
        raise RuntimeError("the broker went away mid-teardown")

    monkeypatch.setattr(A2AMesh, "connect", connect)
    monkeypatch.setattr(A2AMesh, "disconnect", disconnect)
    config = default_config()
    object.__setattr__(config, "a2a", settings())
    mcp = build_server(
        config, source=lambda: config, window_client=window.client, connecting=False
    )

    caplog.set_level(logging.ERROR, logger="slife2.plugins.a2a.server")
    async with Client(mcp) as client:
        holding = asyncio.create_task(client.call_tool(JOIN, {"agent": AGENT}))
        await until(lambda: joined == [AGENT], "the join never reached the mesh")
        holding.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await holding
        await until(
            lambda: any(
                "would not leave the broker as its window left" in record.getMessage()
                for record in caplog.records
            ),
            "a teardown that could not retire the agent left no trace",
        )

    assert any(AGENT in record.getMessage() for record in caplog.records), (
        "the failure does not name the agent it left advertised"
    )


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
            with contextlib.suppress(asyncio.CancelledError):
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
            with contextlib.suppress(asyncio.CancelledError):
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


def _joins_that_wait(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Event]:
    """Hold every `join` where its own call would, and hand back each hold.

    **A held `join` says nothing of its own** — writing nothing is what it is
    defined by, and the keepalive is the only thing that ever happens on it —
    so there is no state to poll and no line to wait for.  Being the wait is
    what makes two of them drivable in a known order: each call is at the mesh
    the moment it appears here.
    """
    holds: list[asyncio.Event] = []

    async def held(_ctx: Any) -> None:
        event = asyncio.Event()
        holds.append(event)
        await event.wait()

    monkeypatch.setattr(a2a_server, "_hold_open", held)
    return holds


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
        net=REMOTE,
        mesh=A2AMesh(default_config().a2a, AGENT, LEDGER),  # opens the ledger
    )

    session.hold(WINDOW)
    assert session.release(WINDOW), "a window alone: its departure is the departure"

    session.hold(WINDOW)
    session.hold(USED)
    assert not session.release(WINDOW), "a tool is still waiting to be answered"
    assert session.release(USED), "the last reason is the one that ends it"


@pytest.mark.asyncio
async def test_two_windows_showing_one_agent_are_two_holds(
    monkeypatch: pytest.MonkeyPatch, window: Window
) -> None:
    """**Two holders of one reason are two reasons to stay** (`Session.hold`).

    This is the case the set could not express, and the one the reason table had
    to become a count for: the *second* `join` for an agent used to be a no-op,
    so whichever of the two calls finished first was answered "that was the last
    reason" — and the mesh went down underneath a window that was still showing
    the agent.  Nothing brings it back: the survivor is parked in `_hold_open`,
    which only pings, and `join` never re-checks the connection.  The agent then
    reads `✗ offline` on the broker for the life of that window and every task a
    peer sends to it is lost.

    Two windows are the plain way in (both call `MCPMeshClient.hold`), and one
    window gets there alone — a dropped stream is retried by
    `slife2.tui.app._hold_mesh` after `pacing.reconnect`, and the stale call is
    not cancelled until its next `report_progress` fails, up to a
    `deliver.keepalive` later.

    Driven through the real tool call and the real release, because *when the
    mesh ends* is the plugin's own decision and not the fake's.
    """
    joined, left = _a_mesh_that_records_its_ends(monkeypatch)
    holds = _joins_that_wait(monkeypatch)
    config = default_config()
    object.__setattr__(config, "a2a", settings())
    mcp = build_server(
        config, source=lambda: config, window_client=window.client, connecting=False
    )

    async with Client(mcp) as client:
        first = asyncio.create_task(client.call_tool(JOIN, {"agent": AGENT}))
        await until(lambda: len(holds) == 1, "the first join never reached the mesh")
        second = asyncio.create_task(client.call_tool(JOIN, {"agent": AGENT}))
        await until(lambda: len(holds) == 2, "the second join never reached the mesh")
        assert joined == [AGENT], "the second window dialled a mesh of its own"

        # One of the two windows goes, and the other is still showing that agent.
        holds[0].set()
        await first
        assert left == [], (
            "a window leaving took the agent off the mesh while a second one "
            "was still holding it — nothing reconnects that survivor"
        )

        holds[1].set()
        await second
        # Asserted with the server still up: the teardown below acknowledges
        # every mesh anyway, so a check made after it would pass for free.
        assert left == [AGENT], "the last window to leave did not take it off"


@pytest.mark.asyncio
async def test_a_window_arriving_as_the_mesh_is_torn_down_gets_a_live_one(
    monkeypatch: pytest.MonkeyPatch, window: Window
) -> None:
    """**One lock, both halves of the decision** (`connected`, `join`).

    `disconnect` clears `_connected` only at its very end — the cancelled
    outbound task has to reach its client context first — and the release was
    awaited outside the session's lock while `connected`'s already-connected
    fast path was taken outside it as well.  So a call arriving inside that
    window was handed a mesh on its way out: it held its reason on a client
    whose reply subscription (`reply/<agent>/+`) was being dropped, so the
    peer's answer reached a topic nobody was listening on and the conversation
    waited for a result that could never arrive — the reason held for ever on a
    mesh that was already offline.

    Held under one lock the two ends cannot interleave: either the arrival takes
    its hold first and the release finds the agent still wanted, or the teardown
    finishes first and the arrival finds a mesh that is genuinely down.  This is
    the second of those, which is the one that has to dial again.
    """
    joined: list[str] = []
    left: list[str] = []
    tearing_down = asyncio.Event()

    async def connect(self: Any) -> None:
        self._connected = True
        joined.append(self.agent_name)

    async def disconnect(self: Any) -> None:
        """Tear down the way the real one does: `_connected` last."""
        left.append(self.agent_name)
        await tearing_down.wait()
        self._connected = False

    monkeypatch.setattr(A2AMesh, "connect", connect)
    monkeypatch.setattr(A2AMesh, "disconnect", disconnect)
    holds = _joins_that_wait(monkeypatch)
    config = default_config()
    object.__setattr__(config, "a2a", settings())
    mcp = build_server(
        config, source=lambda: config, window_client=window.client, connecting=False
    )

    async with Client(mcp) as client:
        leaving = asyncio.create_task(client.call_tool(JOIN, {"agent": AGENT}))
        await until(lambda: len(holds) == 1, "the first join never reached the mesh")
        holds[0].set()
        await until(lambda: left == [AGENT], "the window's departure never began")

        arriving = asyncio.create_task(client.call_tool(JOIN, {"agent": AGENT}))
        # The teardown is still in flight, so this call has to be waiting rather
        # than holding a mesh that is going away — measured here, where the
        # difference is: the fast path answered in the same tick, with
        # `_connected` still true because `disconnect` had not finished.
        await asyncio.sleep(0.05)
        assert len(holds) == 1, (
            "a window arriving mid-teardown was handed the mesh that was "
            "leaving — its reply topic is being unsubscribed under it"
        )

        tearing_down.set()
        await until(lambda: len(holds) == 2, "the arriving window never joined")
        assert joined == [AGENT, AGENT], (
            "the arriving window was not given a mesh of its own: it is riding "
            "the one that went down, and it never reconnects"
        )
        holds[1].set()
        await leaving
        await arriving
        assert left == [AGENT, AGENT], "each departure takes its own mesh down"


@pytest.mark.asyncio
async def test_a_withdrawal_is_recorded_so_a_late_answer_cannot_undo_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Cancelling published the withdrawal and told nobody's record.**

    `cancel_task` answered "cancelled" without recording it, so the row stayed
    `pending` — which made `TaskStore.record_result`'s guard ("a result arriving
    after the caller was told about a cancel must not flip the record back")
    unreachable from the tool path, and a peer that finished anyway pushed "Peer
    X completed async task" over the cancellation the model had already been
    given.
    """
    mesh = A2AMesh(default_config().a2a, AGENT, LEDGER)

    async def no_publish(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(mesh, "_publish_props", no_publish)
    get_store().record_send(TASK, PEER)

    assert await mesh.cancel_task(PEER, TASK) == "cancelled"
    assert get_store().get(TASK).status == "cancelled"

    # A late answer from the peer does not rewrite it, on either ending.
    get_store().record_result(TASK)
    assert get_store().get(TASK).status == "cancelled"
    get_store().record_error(TASK)
    assert get_store().get(TASK).status == "cancelled"
