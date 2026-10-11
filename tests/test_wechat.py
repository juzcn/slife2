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
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fastmcp import Client, FastMCP

from slife2 import ilink, wechat
from slife2.audience import client_meta, for_the_model
from slife2.config import Config, load
from slife2.paths import data_dir
from slife2.wechat import (
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
        #: Whether `sendmessage` answers with an envelope of its own.
        self.reject_send = False
        self.qr: dict[str, Any] = {
            "qrcode": "QR-1",
            "qrcode_img_content": "https://example.test/scan",
        }
        #: What `get_qrcode_status` says — `{}` is "still waiting".
        self.scan: dict[str, Any] = {}
        #: Every path asked for, in order, so a test can count the polls.
        self.paths: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.typing: list[int] = []
        self.ticket = "ticket-1"

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
        return ilink.Answer(status=200, text=json.dumps(self._answer(path, body or {})))

    def _answer(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if path == "ilink/bot/get_bot_qrcode":
            return self.qr
        if path == "ilink/bot/get_qrcode_status":
            return self.scan
        if path == "ilink/bot/getupdates":
            if self.envelope:
                return self.envelope
            return {"msgs": list(self.messages), "get_updates_buf": self.cursor}
        if path == "ilink/bot/sendmessage":
            self.sent.append(dict(body))
            if self.reject_send:
                return {"errno": 1, "error": "token revoked"}
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


@dataclass
class Rig:
    """One plugin, its bridge and its agent."""

    plugin: Wechat
    bridge: Bridge
    delivery: Delivery

    async def close(self) -> None:
        await self.plugin.close()


def rig(monkeypatch: pytest.MonkeyPatch, *, bridge: Bridge | None = None) -> Rig:
    """A plugin over a fresh bridge, with every loop hurried along."""
    monkeypatch.setattr(wechat, "POLL_SECONDS", QUICK)
    monkeypatch.setattr(wechat, "QR_POLL_SECONDS", QUICK)
    monkeypatch.setattr(wechat, "TYPING_REFRESH", QUICK)
    made = Bridge() if bridge is None else bridge
    delivery = Delivery()
    return Rig(
        plugin=Wechat(load(), http=made, deliver=delivery),
        bridge=made,
        delivery=delivery,
    )


def signed_in(
    agent: str = "jack", *, saved_at: float | None = None, cursor: str = ""
) -> Path:
    """A session file, as a login that already happened would leave one."""
    return save_session(
        data_dir(),
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


async def until(predicate: Any, *, timeout: float = 3.0) -> None:
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
    async with Client(served(http=bridge, deliver=made.delivery)) as client:
        shown = await ask(client, "wechat_login")
        await until(lambda: session_path(data_dir(), "jack").exists())
        after = await ask(client, "wechat_check_status")

    saved = load_session(data_dir(), "jack")
    assert "Show this in your reply" in shown
    assert saved["bot_token"] == "bot-token-9"
    assert saved["ilink_user_id"] == "bot-user-9"
    assert "connected" in after
    # The account that scanned is a peer a message can reach, and it is said as
    # that rather than as somebody who has written — nobody has yet.
    assert "Nobody has written yet" in after
    assert "bot-user-9" in after


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
    async with Client(served(http=made.bridge, deliver=made.delivery)) as client:
        answer = await ask(client, "wechat_login")

    assert "already connected" in answer
    assert made.bridge.paths.count("ilink/bot/get_bot_qrcode") == 0


def test_the_qr_is_drawn_as_something_that_can_be_scanned() -> None:
    """Half-block characters, which is what fits a terminal and a reply."""
    drawn = render_qr("https://example.test/scan")

    assert len(drawn.splitlines()) > 5
    assert set(drawn) & {"█", "▀", "▄"}
    assert render_qr("") == ""


# --- messages become turns ----------------------------------------------------


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
    marker = '[Wechat:{"peer_wechat_id": "peer-1", "context_token": "ctx-1"}] '
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
        f"{marker}ok",
        f"{marker}收到",
        f"{marker}收到",
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
    before = load_session(data_dir(), "jack")["saved_at"]
    made = rig(monkeypatch)
    made.bridge.cursor = "cursor-1"
    await made.plugin.start()
    try:
        await until(
            lambda: (
                load_session(data_dir(), "jack").get("get_updates_buf") == "cursor-1"
            )
        )
        started = made.plugin.session("jack").saved_at
    finally:
        await made.close()

    assert load_session(data_dir(), "jack")["saved_at"] == before
    assert abs(started - before) < 1.0, "the session's age is the file's, not now"


@pytest.mark.asyncio
async def test_an_aged_login_is_never_offered_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token past its life is not worth a call, and the answer says why.

    v1 checked the age before using a session and this keeps that: an expired
    login is a sentence about scanning again rather than a rejected request that
    reads like a network fault.
    """
    signed_in("jack", saved_at=time.time() - (wechat.SESSION_MAX_AGE + 60))
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
    assert not session_path(data_dir(), "jack").exists(), "a dead token is not kept"


# --- sending ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reply_carries_the_peer_and_the_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the model hands back is what the bridge is given, unchanged."""
    signed_in("jack")
    made = rig(monkeypatch)
    async with Client(served(http=made.bridge, deliver=made.delivery)) as client:
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
async def test_a_rejected_send_is_not_reported_as_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the envelope problem, on the path with no poll in it.

    `sendmessage` answers a revoked token with a `200` too, so a send that reads
    only its status code tells the model a reply landed when it did not — and
    the model has no way to find out.
    """
    signed_in("jack")
    made = rig(monkeypatch)
    made.bridge.reject_send = True
    async with Client(served(http=made.bridge, deliver=made.delivery)) as client:
        answer = await ask(
            client, "wechat_send_message", peer_wechat_id="peer-1", text="hi"
        )
        after = await ask(client, "wechat_check_status")

    assert "rejected" in answer
    assert "Sent to" not in answer
    assert "rejected" in after
    assert not session_path(data_dir(), "jack").exists()


@pytest.mark.asyncio
async def test_nothing_is_sent_to_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty peer or an empty body is refused before anything is sent."""
    signed_in("jack")
    made = rig(monkeypatch)
    async with Client(served(http=made.bridge, deliver=made.delivery)) as client:
        answer = await ask(client, "wechat_send_message", peer_wechat_id="", text="")

    assert "neither may be empty" in answer
    assert made.bridge.sent == []


@pytest.mark.asyncio
async def test_the_agent_is_told_the_channel_the_turn_came_in_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`channel` is the one thing that says a turn came from a phone.

    Nothing here parses it — it is recorded with the turn and shows in the
    footnote, which is how a model reading a conversation can tell a person at
    the terminal from somebody on WeChat.
    """
    seen: list[dict[str, Any]] = []

    class Agent:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            seen.append({"name": name, **arguments})
            return None

    made = rig(monkeypatch)
    # The one hop, stubbed at its narrowest point: everything else about the
    # delivery is the poll loop's, and that is tested above.
    made.plugin._peer = cast(Any, Agent())
    await made.plugin._ask_agent("jack", "hello")

    assert seen == [
        {
            "name": "send_message",
            "agent": "jack",
            "prompt": "hello",
            "channel": "wechat",
        }
    ]


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
        forgotten = load_session(data_dir(), "jack")
    finally:
        await made.close()

    assert "Logged out" in answer
    assert session.qr_state == ""
    assert forgotten == {}, "the file goes with the login"
    assert not session_path(data_dir(), "jack").exists()


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
        still_saved = session_path(data_dir(), "jack").exists()
        jill_saved = session_path(data_dir(), "jill").exists()
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
    (data_dir() / "wechat_jack.yaml").write_text(
        "bot_token: d834a0dbf318@im.bot:0600\n"
        f"base_url: {ilink.BASE_URL}\n"
        "saved_at: 1790833171.72\n"
        "ilink_user_id: o9cq80@im.wechat\n"
        "ilink_bot_id: d834a0dbf318@im.bot\n"
        "get_updates_buf: ChAICBC7\n",
        encoding="utf-8",
    )

    saved = load_session(data_dir(), "jack")

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
    (data_dir() / "wechat_jack.yaml").write_text(
        "bot_token: [unclosed\n", encoding="utf-8"
    )

    assert load_session(data_dir(), "jack") == {}
