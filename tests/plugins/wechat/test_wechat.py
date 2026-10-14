"""`slife2-wechat`: the login, the loop, and the four tools the model uses.

The bridge is faked and the agent is faked, and both are faked *as state*
rather than as a script: what these tests vary is a message waiting, a token the
far end refuses, a scan that has happened — and a state is a field on one
object, not a queue of canned answers.  That is what lets the cases worth
writing be written at all, because they are the ones where the same call is made
twice and the answer changes.

Four things here are the port's own, and each has a test that a faithful copy of
v1 would fail: the login is **per agent** rather than per process, a message is
**delivered by the plugin** rather than by a harness, the cursor does not
**restamp the login's age**, and a revoked token is read out of a `200` rather
than out of a status code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

import slife2.helpers.timeouts as _T
from slife2.libs.harness import handover, markers
from slife2.libs.harness.handover import DeliveryFailed
from slife2.libs.host.config import TUI_SERVER_NAME, Config, load
from slife2.libs.host.paths import agent_dir
from slife2.plugins.audience import client_meta, for_the_model
from slife2.plugins.wechat import ilink
from slife2.plugins.wechat import server as wechat
from slife2.plugins.wechat.server import (
    Wechat,
    build_server,
    load_session,
    render_qr,
    save_session,
    session_path,
)

pytestmark = pytest.mark.unit

#: Less than a real poll and less than a real QR poll.  Every wait in these
#: tests belongs to a background loop, and this is what the loops are hurried
#: with — through the module constant, which is where each loop reads it.
QUICK = 0.01


class Bridge:
    """The iLink endpoints, as one object a test steers.

    Only the six paths the protocol uses, and the answers have the shapes the
    real ones have — including the one that makes this bridge worth writing at
    all: `getupdates` answers a *revoked token* with an error envelope rather
    than with a status, so a test that wants a revoked token sets one field.
    """

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.cursor = ""
        #: A `getupdates` failure envelope, when a test wants one.
        self.envelope: dict[str, Any] = {}
        #: Whether `sendmessage` answers with an envelope of its own, and which
        #: one — the bridge has two spellings for "no" and a test says which.
        self.reject_send = False
        self.send_envelope: dict[str, Any] = {}
        self.qr: dict[str, Any] = {
            "qrcode": "QR-1",
            "qrcode_img_content": "https://example.test/scan",
        }
        #: What `get_qrcode_status` says — `{}` is "still waiting".
        self.scan: dict[str, Any] = {}
        #: And that one endpoint failing, which is the poll a watch is made of.
        self.scan_fails = False
        #: Every path asked for, in order, so a test can count the polls.
        self.paths: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.typing: list[int] = []
        self.ticket = "ticket-1"
        #: An unreachable bridge: every call raises, which is what a machine
        #: that has just booted and whose proxy is not up yet looks like.
        self.down = False
        #: A `200` whose body is not this protocol's JSON — a proxy's error
        #: page, a captive portal.  Something answered, and it is not the bridge.
        self.raw: str | None = None

    async def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
        timeout: float,
    ) -> ilink.Answer:
        path = url[url.index("ilink/bot/") :].split("?", 1)[0]
        self.paths.append(path)
        if self.down:
            raise ConnectionError("the bridge is unreachable")
        if self.raw is not None:
            return ilink.Answer(status=200, text=self.raw)
        return ilink.Answer(status=200, text=json.dumps(self._answer(path, body or {})))

    def _answer(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if path == "ilink/bot/get_bot_qrcode":
            return self.qr
        if path == "ilink/bot/get_qrcode_status":
            if self.scan_fails:
                raise ConnectionError("the bridge dropped the connection")
            return self.scan
        if path == "ilink/bot/getupdates":
            if self.envelope:
                return self.envelope
            return {"msgs": list(self.messages), "get_updates_buf": self.cursor}
        if path == "ilink/bot/sendmessage":
            self.sent.append(dict(body))
            if self.reject_send:
                return self.send_envelope or {"errno": 1, "error": "token revoked"}
            return {"message_id": "m-1"}
        if path == "ilink/bot/sendtyping":
            self.typing.append(int(body.get("status") or 0))
            return {}
        if path == "ilink/bot/getconfig":
            return {"typing_ticket": self.ticket}
        return {}

    def polls(self) -> int:
        return self.paths.count("ilink/bot/getupdates")


class Delivery:
    """What the plugin says to the agent, recorded rather than run.

    The other seam, and the one that makes this testable as a *plugin*: what is
    asserted is what would have become a turn, with no agent server, no
    conversation and no model anywhere.
    """

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []
        #: A test that wants a turn still running when it looks sets this.
        self.gate: asyncio.Event | None = None

    async def __call__(self, agent: str, prompt: str) -> None:
        self.prompts.append((agent, prompt))
        if self.gate is not None:
            await self.gate.wait()


class Notes:
    """What the plugin drew in a window, recorded rather than rendered.

    The third seam (`note=`), and the one that keeps "a line in a transcript" a
    fact this process can assert: what is recorded is what a window would have
    put under `ChatView.add_note`, brackets and all being the widget's.

    **What the call answers is not read** (`slife2.plugins.wechat.server.Note`): a line is said
    once, and whether a window took it is the window's business rather than
    something this plugin acts on.
    """

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    async def __call__(self, agent: str, text: str) -> None:
        self.lines.append((agent, text))


@dataclass
class Rig:
    """One plugin, its bridge, its agent and its window."""

    plugin: Wechat
    bridge: Bridge
    delivery: Delivery
    notes: Notes

    async def close(self) -> None:
        await self.plugin.close()


def rig(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bridge: Bridge | None = None,
) -> Rig:
    """A plugin over a fresh bridge, with every loop hurried along."""
    monkeypatch.setattr(_T.timeouts.pacing, "wechat_poll", QUICK)
    monkeypatch.setattr(_T.timeouts.pacing, "qr_poll", QUICK)
    monkeypatch.setattr(_T.timeouts.pacing, "typing_refresh", QUICK)
    made = Bridge() if bridge is None else bridge
    delivery = Delivery()
    notes = Notes()
    return Rig(
        plugin=Wechat(load(), http=made, deliver=delivery, note=notes),
        bridge=made,
        delivery=delivery,
        notes=notes,
    )


def signed_in(
    agent: str = "jack", *, saved_at: float | None = None, cursor: str = ""
) -> Path:
    """A session file, as a login that already happened would leave one."""
    return save_session(
        agent,
        {
            "bot_token": "bot-token-1",
            "base_url": ilink.BASE_URL,
            "saved_at": time.time() if saved_at is None else saved_at,
            "ilink_user_id": "bot-user-1",
            "ilink_bot_id": "bot-1",
            "get_updates_buf": cursor,
        },
    )


def message(text: str, *, peer: str = "peer-1", token: str = "ctx-1") -> dict[str, Any]:
    """One incoming message, shaped the way `getupdates` shapes them."""
    return {
        "from_user_id": peer,
        "context_token": token,
        "message_type": 1,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


async def until(
    predicate: Any,
    *,
    timeout: float = 3.0,  # timeouts: the value this test is built around, not a budget
) -> None:  # timeouts: the value this test is built around, not a budget
    """Wait for a background loop to get somewhere, or say it did not."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the background loop did not get there in time")


async def ask(client: Client, tool: str, agent: str = "jack", **arguments: Any) -> str:
    """One tool call, on behalf of a conversation, as the model reads it."""
    result = await client.call_tool(
        tool, arguments, meta=client_meta(agent) if agent else None
    )
    return "".join(getattr(block, "text", "") for block in result.content or [])


# --- the surface the model is given -------------------------------------------


def served(config: Config | None = None, **seams: Any) -> FastMCP:
    return build_server(config or load(), **seams)


@pytest.mark.asyncio
async def test_the_four_tools_are_the_models_and_nothing_else_is() -> None:
    """Four names, all marked, and no `list_sources` at all.

    **The absence is the claim.**  Every other plugin here holds *sources* and
    is asked for them by the hub; this one holds a login.  A `list_sources`
    answering "nothing" would be a tool the hub calls and a sentence that means
    nothing, so there is none — which is the whole difference between this
    plugin and a family.
    """
    async with Client(served()) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    assert set(listed) == {
        "wechat_login",
        "wechat_check_status",
        "wechat_send_message",
        "wechat_logout",
    }
    assert all(for_the_model(tool.meta) for tool in listed.values())
    assert "list_sources" not in listed


@pytest.mark.asyncio
async def test_a_call_that_does_not_say_whose_it_is_gets_nowhere() -> None:
    """**A shared process cannot guess the conversation, so it refuses.**

    v1 knew without being told: its plugin was one agent's, so the agent *was*
    the process.  Here the login is the agent's and the caller is the only party
    that says which — so a call with no client id is answered with why rather
    than with somebody else's WeChat.
    """
    async with Client(served()) as client:
        anonymous = await ask(client, "wechat_check_status", agent="")
        named = await ask(client, "wechat_check_status", agent="jack")

    assert "did not say which conversation" in anonymous
    assert "not connected" in named


# --- logging in ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_scan_becomes_a_saved_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """The QR goes out, the far end confirms, and the file is the record.

    The scan is watched in the background and the file is written when it is
    confirmed — which is what a restart reads back, and what the next start
    polls from.  So this asserts the *file*, because the file is the part that
    has to survive.
    """
    bridge = Bridge()
    bridge.scan = {
        "status": "confirmed",
        "bot_token": "bot-token-9",
        "ilink_user_id": "bot-user-9",
        "ilink_bot_id": "bot-9",
    }
    made = rig(monkeypatch, bridge=bridge)
    async with Client(
        served(http=bridge, deliver=made.delivery, note=made.notes)
    ) as client:
        shown = await ask(client, "wechat_login")
        await until(lambda: session_path("jack").exists())
        after = await ask(client, "wechat_check_status")

    saved = load_session("jack")
    assert "Show this in your reply" in shown
    assert saved["bot_token"] == "bot-token-9"
    assert saved["ilink_user_id"] == "bot-user-9"
    assert "connected" in after
    # The account that scanned is a peer a message can reach, and it is said as
    # that rather than as somebody who has written — nobody has yet.
    assert "Nobody has written yet" in after
    assert "bot-user-9" in after
    # **And the window is told, because this is a link coming up while somebody
    # is watching** — the one arrival that is news, as against a saved login
    # picked up at start-up (`test_a_login_resumed_at_start_up_says_nothing`).
    assert made.notes.lines == [("jack", "⚡ Wechat online")]


@pytest.mark.asyncio
async def test_a_watch_that_cannot_poll_ends_the_login_rather_than_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Every exit from the watch is a state, and the raising one was not.**

    A poll that failed killed the spawned task: `qr_state` stayed `waiting` and
    `wechat_check_status` went on telling the model that a login QR was waiting
    to be scanned — by a watch that had already died, so it could wait for ever
    and the person would be told to scan again and again.  The login ends
    instead, naming what happened.
    """
    bridge = Bridge()
    bridge.scan_fails = True
    made = rig(monkeypatch, bridge=bridge)
    async with Client(
        served(http=bridge, deliver=made.delivery, note=made.notes)
    ) as client:
        await ask(client, "wechat_login")
        # The watch fails on its first poll, and the wait is this loop rather
        # than `until` because what is being waited for is the *tool's* answer
        # and `until` takes a predicate that cannot await one.
        after = ""
        for _ in range(400):
            after = await ask(client, "wechat_check_status")
            if "did not work" in after:
                break
            await asyncio.sleep(0.005)

    assert "The login did not work" in after
    assert "could not be followed" in after
    assert "waiting to be scanned" not in after


@pytest.mark.asyncio
async def test_a_login_that_exists_is_not_asked_to_scan_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved-but-unresumed login must not read as no login at all.

    The startup resume is a background task, so a model that calls `wechat_login`
    first would otherwise be handed a QR code for an account that is already
    connected — and somebody would scan it, which is a *second* login replacing a
    working one.  So the restore is tried before the question is answered.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    async with Client(
        served(http=made.bridge, deliver=made.delivery, note=made.notes)
    ) as client:
        answer = await ask(client, "wechat_login")

    assert "already connected" in answer
    assert made.bridge.paths.count("ilink/bot/get_bot_qrcode") == 0


def test_the_qr_is_drawn_as_something_that_can_be_scanned() -> None:
    """Half-block characters, which is what fits a terminal and a reply."""
    drawn = render_qr("https://example.test/scan")

    assert len(drawn.splitlines()) > 5
    assert set(drawn) & {"█", "▀", "▄"}
    assert render_qr("") == ""


def test_a_missing_qr_renderer_refuses_rather_than_calling_a_url_a_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A URL cannot be scanned, so it must not be handed back as one.**

    Without `qrcode` the payload was returned as it came — a link the login tool
    then told the model to show "so it can be scanned", which is false
    (DESIGN.md §10.1: a weaker path must not be selected silently).  The
    renderer refuses now, and the caller says what actually happened.
    """
    monkeypatch.setitem(sys.modules, "qrcode", None)

    with pytest.raises(ImportError):
        render_qr("https://example.test/scan")


@pytest.mark.asyncio
async def test_a_login_without_the_qr_renderer_tells_the_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sentence the model reads matches what it is being handed."""
    monkeypatch.setitem(sys.modules, "qrcode", None)
    made = rig(monkeypatch)
    async with Client(
        served(http=made.bridge, deliver=made.delivery, note=made.notes)
    ) as client:
        answer = await ask(client, "wechat_login")

    assert "so it can be scanned" not in answer, "a link is not scannable"
    assert "`qrcode` package is missing" in answer
    assert "https://example.test/scan" in answer, "the link is still handed over"


# --- messages become turns ----------------------------------------------------


def test_the_marker_a_person_is_shown_is_not_the_one_the_model_reads() -> None:
    """Build and strip are one pair, and this is what holds them together.

    The marker is *content*: it goes into the row, and the window reads it back
    out of there — so the string that goes on has to be the string that comes
    off, character for character.  A second spelling on the display side is the
    copy that drifts into printing two ids at somebody.
    """
    written = wechat.marker("peer-1", "ctx-1")
    assert written.startswith(wechat.MARKER_PREFIX)
    assert markers.strip(f"{written} 你好", wechat.MARKER_PREFIX) == "你好"
    # Without a token, which is the shape a first reply's marker has.
    assert markers.strip(f"{wechat.marker('peer-1')} hi", wechat.MARKER_PREFIX) == "hi"


def test_unwrapping_takes_a_leader_and_leaves_everything_else() -> None:
    """A prefix by construction, and only that.

    Three kinds of text that must survive: a message that *mentions* a marker
    rather than starting with one, a marker-shaped leader whose bracket never
    closes (a torn row, a hand-edited file), and text with no marker at all.  A
    display that ate any of them would be losing somebody's words to make a
    point about punctuation.
    """
    inside = 'we discussed [Wechat:{"peer_wechat_id": "p"}] and moved on'
    assert markers.strip(inside, wechat.MARKER_PREFIX) == inside
    torn = '[Wechat:{"peer_wechat_id": "p"'
    assert markers.strip(torn, wechat.MARKER_PREFIX) == torn
    assert markers.strip("plain words", wechat.MARKER_PREFIX) == "plain words"
    assert markers.strip("", wechat.MARKER_PREFIX) == ""


@pytest.mark.asyncio
async def test_a_message_becomes_a_turn_with_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The plugin delivers its own turns**, which is what replaced the drain.

    The prompt carries the peer and the conversation's token in a marker whose
    JSON keys are `wechat_send_message`'s argument names, so that replying is
    copying two values rather than translating between two vocabularies.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    made.bridge.messages = [message("你好")]
    await made.plugin.start()
    try:
        await until(lambda: made.delivery.prompts)
    finally:
        await made.close()

    assert made.delivery.prompts == [
        ("jack", '[Wechat:{"peer_wechat_id": "peer-1", "context_token": "ctx-1"}] 你好')
    ]


@pytest.mark.asyncio
async def test_a_redelivery_is_dropped_and_a_real_repeat_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window, and the one thing it must not do.

    The bridge re-serves recent messages until the cursor advances, so the same
    message arrives twice and only the second may be dropped.  But two identical
    texts in **one** answer are somebody sending "ok" twice, so the window is
    consulted per *batch* — which is the distinction v1's first version got
    wrong in the direction that loses a message.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    made.bridge.messages = [message("ok")]
    # The envelope alone: the space that separates it from the message is the
    # joiner's, so it is written here the way `_hand_over` writes it.
    marker = '[Wechat:{"peer_wechat_id": "peer-1", "context_token": "ctx-1"}]'
    try:
        await made.plugin.start()
        await until(lambda: made.delivery.prompts)
        # The same message, served again by later polls: the window holds it.
        await until(lambda: made.bridge.polls() >= 4)
        assert len(made.delivery.prompts) == 1

        # And now two *new* identical texts together, which are two messages.
        made.bridge.messages = []
        await until(lambda: made.bridge.polls() >= 5)
        made.bridge.messages = [message("收到"), message("收到")]
        await until(lambda: len(made.delivery.prompts) == 3)
    finally:
        await made.close()

    assert [prompt for _, prompt in made.delivery.prompts] == [
        f"{marker} ok",
        f"{marker} 收到",
        f"{marker} 收到",
    ]


@pytest.mark.asyncio
async def test_a_photo_is_skipped_without_burning_the_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-text item must not take the next real message with it.

    The dedup key is derived from the text, so an image — whose text is empty —
    would key every later message in that conversation to the same value and
    drop them.  The skip happens *before* the key is recorded, which is the
    ordering fix v1 shipped.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    made.bridge.messages = [
        {
            "from_user_id": "peer-1",
            "context_token": "ctx-1",
            "item_list": [{"type": 3}],
        },
        message("still here"),
    ]
    await made.plugin.start()
    try:
        await until(lambda: made.delivery.prompts)
    finally:
        await made.close()

    assert len(made.delivery.prompts) == 1
    assert made.delivery.prompts[0][1].endswith("still here")


@pytest.mark.asyncio
async def test_the_typing_hint_is_up_while_the_turn_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Somebody holding the phone is told that something is happening.

    The keep-alive belongs to the *turn* rather than to the message: it goes up
    when the message is handed over, and its task is gone when the turn is —
    which is what stops it refreshing for a turn that has already ended.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    made.delivery.gate = asyncio.Event()
    made.bridge.messages = [message("在吗")]
    await made.plugin.start()
    try:
        await until(lambda: 1 in made.bridge.typing)
        session = made.plugin.session("jack")
        assert session.typing, "a keep-alive is running for the conversation"
        made.delivery.gate.set()
        await until(lambda: not session.typing)
        await until(lambda: not session.inflight)
    finally:
        await made.close()


# --- the session file ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cursor_write_does_not_restamp_the_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug this would have been, and why the age is kept apart.

    The cursor is written whenever it moves — every few seconds — and the login
    expires on `saved_at`.  A writer that stamped `saved_at` as it wrote would
    be a login that never expires, and a status line that always says a full day
    is left.  So the field is carried across, and this is the test that says so.
    """
    signed_in("jack", saved_at=time.time() - 3600)
    before = load_session("jack")["saved_at"]
    made = rig(monkeypatch)
    made.bridge.cursor = "cursor-1"
    await made.plugin.start()
    try:
        await until(lambda: load_session("jack").get("get_updates_buf") == "cursor-1")
        started = made.plugin.session("jack").saved_at
    finally:
        await made.close()

    assert load_session("jack")["saved_at"] == before
    assert abs(started - before) < 1.0, "the session's age is the file's, not now"


@pytest.mark.asyncio
async def test_a_cursor_that_could_not_be_written_is_said_out_loud(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**A cursor that did not land is a replay window answered twice.**

    The file is the only thing that tells a restart where the stream had got to,
    so a write that failed leaves the bridge's whole replay window to be
    re-ingested and answered as fresh turns.  That was a `debug` line — the one
    level an operator never reads — and it is a warning naming the file now,
    with `persisted` left where it was so the next poll tries again.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    made.bridge.cursor = "cursor-1"

    def refuse(agent: str, session: Mapping[str, Any]) -> Path:
        raise OSError("the disk is full")

    monkeypatch.setattr(wechat, "save_session", refuse)
    with caplog.at_level(logging.WARNING, logger="slife2.plugins.wechat.server"):
        await made.plugin.start()
        try:
            session = made.plugin.session("jack")
            await until(lambda: made.bridge.polls() >= 1)
        finally:
            await made.close()

    assert session.persisted == "", "a cursor that did not land is not believed saved"
    assert load_session("jack").get("get_updates_buf") == "", "the file kept its place"
    assert any(
        str(session_path("jack")) in record.getMessage() for record in caplog.records
    ), "a cursor that could not be saved left nothing to find"


@pytest.mark.asyncio
async def test_a_failed_poll_is_a_warning_naming_the_session(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**A poll that failed is findable, not merely inferable.**

    A poll failure was a `debug` line, so the only way to see that the loop had
    stopped collecting was `last_error` inside a later `wechat_check_status` —
    the fact was in the log all along, at the one level nobody reads.  The
    warning names the session, and the loop still keeps going.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    await made.plugin.start()
    try:
        session = made.plugin.session("jack")
        await until(lambda: session.polling)

        async def refuse() -> list[dict[str, Any]]:
            raise RuntimeError("the bridge answered with something unreadable")

        monkeypatch.setattr(session.client, "poll_updates", refuse)
        with caplog.at_level(logging.WARNING, logger="slife2.plugins.wechat.server"):
            await until(
                lambda: any(
                    "poll failed" in record.getMessage() for record in caplog.records
                )
            )
        assert session.polling, "the loop keeps going after a failure"
    finally:
        await made.close()

    assert any(
        "jack" in record.getMessage() and "poll failed" in record.getMessage()
        for record in caplog.records
    ), "the warning does not name the session it belongs to"


@pytest.mark.asyncio
async def test_an_aged_login_is_never_offered_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token past its life is not worth a call, and the answer says why.

    v1 checked the age before using a session and this keeps that: an expired
    login is a sentence about scanning again rather than a rejected request that
    reads like a network fault.
    """
    signed_in(
        "jack", saved_at=time.time() - (_T.timeouts.pacing.wechat_session_max_age + 60)
    )
    made = rig(monkeypatch)
    await made.plugin.start()
    try:
        answer = await made.plugin.status(made.plugin.session("jack"))
    finally:
        await made.close()

    assert made.bridge.polls() == 0, "an aged token is not offered"
    assert "older than one lasts" in answer
    assert "wechat_login" in answer


@pytest.mark.asyncio
async def test_a_rejected_login_is_forgotten_rather_than_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `200` carrying an error envelope is a *rejection*, not silence.

    This is the failure v1 shipped and then fixed: read as "no messages", a
    revoked token leaves a poll loop spinning and a status tool answering
    `connected` while nothing arrives.  Here the envelope stops the loop and the
    file goes, because a dead token left in place makes every later read a
    phantom login.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    await made.plugin.start()
    try:
        session = made.plugin.session("jack")
        await until(lambda: session.polling)
        made.bridge.envelope = {"errno": 11000, "error": "token revoked"}
        await until(lambda: session.poll is not None and session.poll.done())
        answer = await made.plugin.status(session)
    finally:
        await made.close()

    assert "rejected" in answer
    assert not session_path("jack").exists(), "a dead token is not kept"


# --- sending ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reply_carries_the_peer_and_the_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the model hands back is what the bridge is given, unchanged."""
    signed_in("jack")
    made = rig(monkeypatch)
    async with Client(
        served(http=made.bridge, deliver=made.delivery, note=made.notes)
    ) as client:
        answer = await ask(
            client,
            "wechat_send_message",
            peer_wechat_id="peer-1",
            text="在的",
            context_token="ctx-1",
        )

    assert answer == "Sent to peer-1."
    sent = made.bridge.sent[0]["msg"]
    assert (sent["to_user_id"], sent["context_token"]) == ("peer-1", "ctx-1")
    assert sent["item_list"][0]["text_item"]["text"] == "在的"
    # The indicator comes down with the reply rather than with the turn: a model
    # that has answered and keeps working should stop looking busy.
    assert 2 in made.bridge.typing


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "envelope",
    [
        {"errno": 1, "error": "token revoked"},
        {"errcode": -14, "errmsg": "session timeout"},
    ],
    ids=["revoked", "retired"],
)
async def test_a_rejected_send_is_not_reported_as_sent(
    monkeypatch: pytest.MonkeyPatch, envelope: dict[str, Any]
) -> None:
    """The other half of the envelope problem, on the path with no poll in it.

    `sendmessage` answers a token the far end will not serve with a `200` too, so
    a send that reads only its status code tells the model a reply landed when it
    did not — and the model has no way to find out.  Both spellings of that
    envelope are here because the retired one is the harder half: the message a
    *retired* login sends is not refused loudly by the bridge, it is dropped, and
    "Sent to peer-1." over a dropped message is the most expensive lie this
    plugin can tell.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    made.bridge.reject_send = True
    made.bridge.send_envelope = envelope
    async with Client(
        served(http=made.bridge, deliver=made.delivery, note=made.notes)
    ) as client:
        answer = await ask(
            client, "wechat_send_message", peer_wechat_id="peer-1", text="hi"
        )
        after = await ask(client, "wechat_check_status")

    assert "rejected" in answer
    assert "Sent to" not in answer
    assert "rejected" in after
    assert not session_path("jack").exists()


@pytest.mark.asyncio
async def test_nothing_is_sent_to_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty peer or an empty body is refused before anything is sent."""
    signed_in("jack")
    made = rig(monkeypatch)
    async with Client(
        served(http=made.bridge, deliver=made.delivery, note=made.notes)
    ) as client:
        answer = await ask(client, "wechat_send_message", peer_wechat_id="", text="")

    assert "neither may be empty" in answer
    assert made.bridge.sent == []


@pytest.mark.asyncio
async def test_the_message_is_handed_to_the_window_with_its_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delivery goes to the terminal, and says which channel it came in on.

    Both halves matter and they are the same fact: the window runs these words as
    if the person typed them, so the turn's channel is what tells the model — and
    the bubble — that these came off a phone.  Nothing here parses it; it is
    recorded with the turn and shows in the footnote.
    """
    seen: dict[str, Any] = {}

    class Window:
        async def call_tool(
            self, name: str, arguments: dict[str, Any], **kwargs: Any
        ) -> Any:
            seen.update({"name": name, **arguments})

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def open_window(url: str, **kwargs: Any):
        seen.update({"url": url, "advertised": kwargs.get("name")})
        return Window()

    monkeypatch.setattr(handover, "open_server", open_window)
    made = rig(monkeypatch)
    await made.plugin._deliver_to_window("jack", "hello")

    # Where it went, and what it says: the address is the `tui` plugin's — one
    # for every agent — and the name is what a caller proves before handing
    # anything over.
    assert seen.pop("url") == made.plugin._config.server("tui").url
    assert seen.pop("advertised") == TUI_SERVER_NAME
    assert seen == {
        "name": "submit",
        # **Which agent**, and it is the whole reason the address can be shared:
        # the plugin keeps a session per agent and this picks the window.
        "agent": "jack",
        "text": "hello",
        "channel": "wechat",
    }


@pytest.mark.asyncio
async def test_a_window_that_is_not_there_fails_the_hand_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tried while the window may be attaching, then a hard failure.

    A refusal is the one answer that proves nothing was written, so it is worth
    another attempt — but the attempts are `slife2.libs.harness.handover`'s rather than a
    second copy here, and when they are spent the message is *not delivered*
    rather than quietly dropped or carried somewhere else.
    """
    attempts = 0

    async def open_window(url: str, **kwargs: Any):
        nonlocal attempts
        attempts += 1
        raise ConnectionError(f"nothing is listening on {url}")

    monkeypatch.setattr(handover, "open_server", open_window)
    made = rig(monkeypatch)

    with pytest.raises(DeliveryFailed, match="could not be reached"):
        await made.plugin._deliver_to_window("jack", "hello")

    assert attempts == handover._ATTEMPTS


# --- logging out, and two logins at once --------------------------------------


@pytest.mark.asyncio
async def test_logging_out_forgets_the_login_and_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And the QR state goes with it, which is a fix rather than tidiness.

    A status read asks about a pending scan *first*, so a `confirmed` left over
    from the login just ended reported a scan still waiting — and told the model
    to wait for one that had already happened.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    await made.plugin.start()
    try:
        session = made.plugin.session("jack")
        await until(lambda: session.polling)
        session.qr_state = "confirmed"
        answer = await made.plugin.logout(session)
        forgotten = load_session("jack")
    finally:
        await made.close()

    assert "Logged out" in answer
    assert session.qr_state == ""
    assert forgotten == {}, "the file goes with the login"
    assert not session_path("jack").exists()


@pytest.mark.asyncio
async def test_a_login_that_could_not_be_removed_is_not_reported_as_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**"The saved login is gone" is a promise the next start keeps for you.**

    `clear_session` suppressed the unlink failure and answered `False`, which
    every caller threw away — so the tool said the login was gone over a file
    still on disk, and the next start adopted the account the logout was asked
    to end.  The answer now says what actually happened, with the path to remove.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    await made.plugin.start()
    original = Path.unlink

    def refuse(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == session_path("jack"):
            raise PermissionError("the file is open in another program")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    try:
        session = made.plugin.session("jack")
        await until(lambda: session.polling)
        answer = await made.plugin.logout(session)
    finally:
        await made.close()

    assert session_path("jack").exists(), "the file this test is about"
    assert "could not be removed" in answer
    assert "saved login is gone" not in answer, "a file still there is not gone"
    assert str(session_path("jack")) in answer, "the answer names what to remove"


# --- the link, as a line in the window ----------------------------------------


@pytest.mark.asyncio
async def test_a_login_resumed_at_start_up_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved login coming back is this *process* starting, not the link moving.

    The plugin is up before the terminal is, so a line drawn here is drawn into
    nobody — and what a window shows is what happened *while somebody was
    watching*.  Without this rule every start of the stack would put
    `⚡ Wechat online` in every window of every agent that has a saved login,
    which is a line about the process rather than about the link.  The state is
    marked known instead (`_adopt`), so the poll loop says nothing either.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    await made.plugin.start()
    try:
        session = made.plugin.session("jack")
        # Several polls, with the link up throughout.
        await until(lambda: made.bridge.paths.count("ilink/bot/getupdates") >= 3)
    finally:
        await made.close()

    # Nothing, however many polls pass under it.
    assert made.notes.lines == []
    assert session.announced == wechat.LINK_ONLINE


@pytest.mark.asyncio
async def test_a_resume_that_could_not_run_at_all_is_said(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """**A boot that failed is not a boot that stopped.**

    `_resume` swallows everything, and it runs from a spawned task whose only
    reporter is `_spawn`'s callback — so a fourth failure, past the three
    `_adopt` tells apart (too old, refused, unreachable), was invisible: no
    note, no line, and a status read that says "not connected" about a login
    nobody tried to take up.  The swallow stays (a boot must not take the
    plugin down); the silence does not.
    """
    signed_in("jack")
    made = rig(monkeypatch)

    async def refuse(session) -> None:
        raise RuntimeError("the session file has no bot token")

    monkeypatch.setattr(made.plugin, "ready", refuse)
    with caplog.at_level("WARNING"):
        await made.plugin.start()
        await until(lambda: "was not resumed" in caplog.text)
    await made.close()

    assert "the session file has no bot token" in caplog.text


@pytest.mark.asyncio
async def test_a_login_that_did_not_come_back_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And the other half of a start-up: a login that is there and is not working.

    Nothing is drawn for it either, for `..._says_nothing`'s reason — nobody is
    watching yet.  What is *not* silent is why: `restore_note` is what
    `wechat_check_status` repeats, so a model asked about WeChat says the saved
    login could not be confirmed rather than that nothing was ever scanned.
    """
    signed_in("jack")
    bridge = Bridge()
    bridge.down = True  # the bridge is unreachable: nothing to come back to
    made = rig(monkeypatch, bridge=bridge)
    await made.plugin.start()
    try:
        session = made.plugin.session("jack")
        await until(lambda: session.restore_note)
        assert session.poll is None, "no link, so there is nothing to poll"
        answer = await made.plugin.status(session)
    finally:
        await made.close()

    assert made.notes.lines == []
    assert "could not be confirmed" in answer


@pytest.mark.asyncio
async def test_logging_out_takes_the_line_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one departure the plugin chooses, so it is the one it says itself.

    `logout` cancels the poll loop where it stands, which is why the announcement
    cannot ride that loop's exit — see `Wechat._announce`.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    await made.plugin.start()
    try:
        session = made.plugin.session("jack")
        await until(lambda: made.bridge.paths.count("ilink/bot/getupdates") >= 2)
        await made.plugin.logout(session)
    finally:
        await made.close()

    # The departure only: the login was resumed at start-up, and a resume says
    # nothing (`test_a_login_resumed_at_start_up_says_nothing`).
    assert made.notes.lines == [("jack", "✗ Wechat offline")]


@pytest.mark.asyncio
async def test_a_token_the_far_end_refuses_takes_the_line_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link that goes down on its own is the case nobody is told about otherwise.

    The loop ends because the state did, so the last thing it does is say so —
    and a window that was open when the token was revoked finds out without
    anybody asking `wechat_check_status`.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    await made.plugin.start()
    try:
        session = made.plugin.session("jack")
        await until(lambda: made.bridge.paths.count("ilink/bot/getupdates") >= 2)
        made.bridge.envelope = {"errno": 11000, "error": "token revoked"}
        await until(lambda: session.poll is not None and session.poll.done())
    finally:
        await made.close()

    # The departure this test is about, and nothing for the resumed login that
    # came before it (`test_a_login_resumed_at_start_up_says_nothing`).
    assert made.notes.lines == [("jack", "✗ Wechat offline")]


@pytest.mark.asyncio
async def test_a_second_login_of_the_same_account_retires_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same "no" in the other spelling, and the case it is the answer to.

    **iLink keeps one live session per WeChat account.**  Scanning a code for a
    second agent with the same phone retires the older login, and the retired one
    is answered `{"errcode": -14, "errmsg": "session timeout"}` — instantly, to
    its polls and to its sends.  That is a rejection wearing a `200`, which is
    what this module already has a name for; what it did not have was `errcode`.
    Read as an empty poll the retired session kept its loop, kept a window
    saying `online`, and reported sends the bridge had thrown away, which is
    exactly the sound of a link that is fine and a channel that is not.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    await made.plugin.start()
    try:
        session = made.plugin.session("jack")
        await until(lambda: made.bridge.polls() >= 2)
        # Another login of the same account takes the session over.
        made.bridge.envelope = {"errcode": -14, "errmsg": "session timeout"}
        await until(lambda: session.poll is not None and session.poll.done())
        answer = await made.plugin.status(session)
    finally:
        await made.close()

    assert made.notes.lines == [("jack", "✗ Wechat offline")]
    # And the reason is the bridge's own words, so the model can say which of
    # the two things happened rather than sending somebody to scan blindly.
    assert "session timeout" in answer
    assert not session_path("jack").exists(), "a retired token is not kept"


@pytest.mark.asyncio
async def test_a_link_that_never_came_up_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold offline is not a transition, and is not drawn.

    An agent that has never scanned a code must not open a window with a
    complaint about a channel nothing has happened to.  This is the rule the mesh
    states about a cold card, and it is the same rule said about a link — a
    start-up is not something a window watched, whether the login came back or
    not (`test_a_login_that_did_not_come_back_says_nothing`).
    """
    made = rig(monkeypatch)  # no `signed_in`: there is no login to resume
    await made.plugin.start()
    try:
        session = made.plugin.session("jack")
        await made.plugin.logout(session)
    finally:
        await made.close()

    assert made.notes.lines == []
    assert session.announced == wechat.LINK_OFFLINE, "known, and not worth saying"


@pytest.mark.asyncio
async def test_two_agents_are_two_logins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sharing the process does not mean sharing the account.

    Each agent scans its own code and gets its own file, its own client and its
    own loop — and one ending does not touch the other's, which is a property a
    per-process port gets for free and a shared one has to be built for.
    """
    signed_in("jack")
    signed_in("jill")
    made = rig(monkeypatch)
    await made.plugin.start()
    try:
        await until(lambda: made.plugin.session("jack").polling)
        await until(lambda: made.plugin.session("jill").polling)
        await made.plugin.logout(made.plugin.session("jill"))
        jack = made.plugin.session("jack")
        still_running = jack.polling
        still_saved = session_path("jack").exists()
        jill_saved = session_path("jill").exists()
    finally:
        await made.close()

    assert still_running, "jill's logout does not stop jack's loop"
    assert still_saved
    assert not jill_saved


# --- and the file v1 left behind ----------------------------------------------


def test_a_login_file_from_v1_is_adopted_as_it_stands() -> None:
    """The port reads v1's file, so nobody has to scan again.

    Same name, same keys, same meaning: a session saved by
    `slife/plugins/wechat` is a session this plugin resumes, which is the only
    reason the format was kept rather than improved.
    """
    (agent_dir("jack") / "wechat.yaml").write_text(
        "bot_token: d834a0dbf318@im.bot:0600\n"
        f"base_url: {ilink.BASE_URL}\n"
        "saved_at: 1790833171.72\n"
        "ilink_user_id: o9cq80@im.wechat\n"
        "ilink_bot_id: d834a0dbf318@im.bot\n"
        "get_updates_buf: ChAICBC7\n",
        encoding="utf-8",
    )

    saved = load_session("jack")

    assert saved["bot_token"] == "d834a0dbf318@im.bot:0600"
    assert saved["get_updates_buf"] == "ChAICBC7"
    assert set(saved) == {
        "bot_token",
        "base_url",
        "saved_at",
        "ilink_user_id",
        "ilink_bot_id",
        "get_updates_buf",
    }


def test_a_torn_session_file_is_a_cache_miss_rather_than_a_crash() -> None:
    """A file that cannot be read must not be able to stop the plugin starting."""
    (agent_dir("jack") / "wechat.yaml").write_text(
        "bot_token: [unclosed\n", encoding="utf-8"
    )

    assert load_session("jack") == {}


@pytest.mark.asyncio
async def test_a_damaged_login_file_is_named_rather_than_read_as_no_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A file that will not parse is damage, not the absence of a login.**

    A torn cache answered `{}`, and the status sentence built from that said
    "no login is saved" over a file that was right there — telling somebody who
    had already scanned to scan a fresh code (DESIGN.md §10.1).  The outcome is
    kept, because a login is still had by scanning, but the file and its damage
    are named in what the model reads, through `restore_note`.
    """
    path = session_path("jack")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("bot_token: [unclosed\n", encoding="utf-8")
    made = rig(monkeypatch)
    async with Client(
        served(http=made.bridge, deliver=made.delivery, note=made.notes)
    ) as client:
        answer = await ask(client, "wechat_check_status")

    assert "no login is saved" not in answer, "the file is there, and it is damaged"
    assert "could not be read" in answer
    assert "wechat.yaml" in answer, "the file at fault is not named"


@pytest.mark.asyncio
async def test_a_bridge_that_is_unreachable_at_boot_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A transient failure must not wedge the session for the process's life.**

    The launcher starts this plugin at boot, which is exactly when a network can
    be unreachable — and `try_restore_session` adopts the token *before* anything
    confirms it.  So a validation that fails on the network used to leave a
    client that reported itself logged in, and `ready` short-circuits on that:
    nothing ever retried and the poll loop never started, while the status answer
    said the link was merely quiet.  The token is let go instead (the *file* is
    not touched), so the next read re-reads it — which is what "keep retrying"
    has to mean to be worth saying.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    made.bridge.down = True
    await made.plugin.start()
    try:
        session = made.plugin.session("jack")
        await until(lambda: session.restore_note)
        assert not session.polling, "no loop can start against a dead bridge"
        assert not session.client.is_logged_in, "the unconfirmed token is let go"
        assert session_path("jack").exists(), "and the file is kept: it is not at fault"

        made.bridge.down = False
        answer = await made.plugin.status(session)
        await until(lambda: session.polling)
    finally:
        await made.close()

    assert "connected" in answer


@pytest.mark.asyncio
async def test_a_message_off_the_wire_is_handed_to_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole path, and the one that was broken: the poll loop's own delivery.

    The tests above drive the delivery *seam* (`deliver=`), which is the poll
    loop's edge — and the window is reached by a method of this class *after* that
    edge, so both halves were "tested" while disagreeing about the call.  That is
    how a method whose name was already taken in this class replaced the poll
    loop's own hand-over, and it left sending working (that path is a tool) while
    receiving was dead (this one is not).

    So this goes the whole way and nothing less: a message off the wire →
    `_queued` → the task `_answer` awaits → the window's `submit`.
    """
    seen: list[tuple[str, dict[str, Any]]] = []

    class Window:
        async def call_tool(
            self, name: str, arguments: dict[str, Any], **kwargs: Any
        ) -> Any:
            seen.append((name, arguments))

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def open_window(url: str, **kwargs: Any):
        return Window()

    monkeypatch.setattr(handover, "open_server", open_window)
    signed_in("jack")
    # The real delivery — no `deliver=` seam — which is the point of the test.
    plugin = Wechat(load(), http=Bridge())
    session = plugin.session("jack")

    plugin._queued(session, message("hi"), set())
    await until(lambda: not session.inflight)

    assert seen == [
        (
            "submit",
            {
                # Whose conversation it belongs to, so that one shared plugin
                # can find the one window showing it.
                "agent": "jack",
                # The marker is the plugin's, and it is what the model answers
                # with: it goes through the window untouched.
                "text": (
                    '[Wechat:{"peer_wechat_id": "peer-1", "context_token": "ctx-1"}] hi'
                ),
                "channel": "wechat",
            },
        )
    ]


# --- a failure is classified before it is retried -----------------------------


@pytest.mark.asyncio
async def test_a_hand_over_that_does_not_come_back_is_not_repeated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A lost reply must not become two turns.**

    The window takes the message and the response is lost — a timeout inside
    `submit`, which is what `transport.window` bounds — so the words may already
    be down the stream.  Retrying on *any* exception is how one WeChat message
    runs as two turns and draws two replies, the failure DESIGN §1.2/§1.3 call
    worse than a late answer.  `slife2.libs.harness.handover` classifies this as
    *unknown* rather than a refusal, so no retry is taken.
    """
    attempts = 0

    class Window:
        async def call_tool(
            self, name: str, arguments: dict[str, Any], **kwargs: Any
        ) -> Any:
            nonlocal attempts
            attempts += 1
            raise TimeoutError("the submit never came back")

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def open_window(url: str, **kwargs: Any) -> Window:
        return Window()

    monkeypatch.setattr(handover, "open_server", open_window)
    made = rig(monkeypatch)

    with pytest.raises(DeliveryFailed, match="was not repeated"):
        await made.plugin._deliver_to_window("jack", "hello")

    assert attempts == 1, "an unclassified failure is not retried"


# --- the login is keyed by the name the file states ---------------------------


@pytest.mark.asyncio
async def test_a_login_is_keyed_by_the_name_the_file_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The directory holds the sanitised name, so a login keyed by it was a
    second key for one login.**

    `a/b` saves to `agents/a_b/wechat.yaml`, but every other entry point — the
    tools, off the call's client id — uses `a/b`.  Keyed by the directory,
    `start()` created `Session("a_b")` and the first tool call created a second
    `Session("a/b")` on the same token: two poll loops on one login, so every
    message was delivered twice, one copy to a window that was not showing.
    """
    signed_in("a/b")
    assert session_path("a/b").parent.name != "a/b", "the directory is sanitised"
    assert load_session("a/b")["agent"] == "a/b", "and the file states the real name"
    made = rig(monkeypatch)

    await made.plugin.start()
    try:
        assert list(made.plugin._sessions) == ["a/b"], (
            "the session is keyed by the name in the file, not the directory"
        )
    finally:
        await made.close()


# --- the one endpoint both readers share --------------------------------------


@pytest.mark.asyncio
async def test_the_two_getupdates_readers_agree_about_a_rejection() -> None:
    """**`validate_session` and `poll_updates` are one `getupdates` call read two
    ways**, and the envelope handling is shared so the two cannot come to disagree
    about what a rejection is — the failure this module's docstring was written
    about."""
    # Both spellings of "no", because the second is how this came back after
    # being fixed once: a retired login answers `errcode` / `errmsg`, and a
    # reader that knew only `errno` read that as a healthy empty poll.
    for envelope in (
        {},
        {"errno": 11000, "error": "token revoked"},
        {"errcode": -14, "errmsg": "session timeout"},
    ):
        bridge = Bridge()
        bridge.envelope = envelope
        client = ilink.IlinkClient(http=bridge)

        valid = await client.validate_session()
        rejected = client.auth_failed

        # The other reader, on the same body: same call, same verdict.
        client.auth_failed = False
        messages = await client.poll_updates()

        assert valid is (not envelope)
        assert rejected is bool(envelope)
        assert client.auth_failed is bool(envelope)
        assert messages == []


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_is_a_broken_link_not_an_empty_poll() -> None:
    """**The third way to fail**, which used to read as the second.

    A proxy's error page and a captive portal arrive as a `200` carrying HTML.
    Decoded to `{}` that was the same answer as an empty poll — no envelope, so
    `last_error` was cleared and the session validated — and the model was told
    about a connected bridge by whatever had answered instead of it.
    """
    bridge = Bridge()
    bridge.raw = "<html><body>502 Bad Gateway</body></html>"
    client = ilink.IlinkClient(http=bridge)

    assert await client.validate_session() is False
    assert client.auth_failed is False, "not the far end saying no — something else"
    assert "not JSON" in (client.last_error or "")

    # And the other reader of the same call agrees, rather than answering the
    # empty list a healthy poll answers.
    client.last_error = None
    assert await client.poll_updates() == []
    assert client.last_error, "a poll that read nothing did not read an empty inbox"
