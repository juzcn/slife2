"""slife2-wechat — WeChat, as the plugin that holds every agent's login.

WeChat opens no API to a program, only the hosted bridge `slife2.ilink` speaks.
What this module adds is the part that makes it a *plugin* here: a session per
agent, the poll loop that watches each one, the delivery that turns an incoming
message into a turn, and the four tools the model uses to log in and reply.

**One process, one login per agent** — and that sentence is the whole of what
changed in the port.  v1 ran one wechat child per agent, so its module globals
*were* that agent's: `_client`, `_pending`, `_seen_keys` were one login, one
queue and one dedup window because there was exactly one agent in the process.
slife2's plugins are shared (`slife2.mcp_server`), so those four things live in
a :class:`Session` keyed by the calling agent — the first half of the client id
every call already carries (`slife2.audience`).  A second agent that scans its
own QR gets its own session file, its own poll loop and its own conversation,
and neither can see the other's.

**The plugin hands each message to the window.**  v1's harness polled the
plugin for messages (`__wechat_drain_incoming`) and injected them into the inbox
itself, so the plugin's job ended at "here is what arrived" and a second process
decided what that meant.  Here the plugin delivers: the window showing this
agent's conversation takes the message into the same queue the person's own
typing goes into, and runs it as the same kind of turn — so a message sent from a
phone is *visible in the terminal*, wears the channel it came in on, and is
answered there, with the one inbox this system has (see `slife2.tui.control`).
That is why v1's drain tool has no port either: it *was* the seam, and the seam is
gone because the terminal is where the message lands.

**The reply is the model's to send**, unchanged from v1. Nothing in this process
auto-dispatches an answer — the model reads who wrote (the marker below), calls
`wechat_send_message`, and a turn that says nothing sends nothing.

The one thing this process does on its own is the typing indicator: it goes up
when a message is handed to the agent and comes down when the reply goes out, so
the person holding the phone can see that something is happening.

## A marker, because a conversation has to know who it is with

An incoming message arrives as model input, and the model has to be able to
answer it: that takes the peer's id and the conversation's `context_token`, and
neither is anything the turn's own record carries (`channel` says `wechat` and
nothing more).  So the prompt is prefixed —

    [Wechat:{"peer_wechat_id": "…", "context_token": "…"}] 你好

— with the JSON keys *named after the arguments of `wechat_send_message`*, so
that replying is copying two values across rather than translating between two
vocabularies.  v1 kept the same marker in `slife/agent/message_history.py`, case
and all, because a session file and a habit both survive a port.

**And the case is the tell — by convention, which is all it can be.**  A
conversation carries two markers, and whether the name is in capitals says which
kind of thing it is: `[TURN:{…}]` is the harness's own annotation, derived every
time a message list is built and never written down, while `[Wechat:{…}]` is
**content** — it goes into the turn's row along with the message and comes back
out of it verbatim.  So capitals read as "this is not the conversation, it is
what this process knows about it", and a name that reads as a word reads as a
marker that is part of what the model has in memory.  Nothing enforces it and
nothing parses either marker: it is a spelling the two writers agreed on for the
sake of a reader, and the readers are the model and whoever is looking at a
transcript.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from ruamel.yaml import YAML

from slife2 import ilink
from slife2.audience import FOR_THE_MODEL, request_client
from slife2.config import TUI_SERVER_NAME, Config
from slife2.ilink import Http, IlinkClient
from slife2.mcp_server import house_server, open_server, serve_plugin
from slife2.paths import agent_dir, agents_dir

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-wechat"

#: This server's key in the config's `servers:` table, and the name the launcher
#: starts it under and the hub connects to it by.  Also the `wechat:` section
#: it owns, because the two coincide here the way they do for `context`.
CONFIG_KEY = "wechat"

INSTRUCTIONS = (
    "WeChat, over the iLink bridge: a login per agent, the messages that arrive "
    "while it is up, and the tools to log in, reply and check the link. An "
    "incoming message becomes a turn of this conversation with `channel=wechat`, "
    "and answering it is this end's job — nothing here replies for you."
)

# ── Cadence, and v1's numbers ───────────────────────────────────────────
# v1 kept these in a global timeouts registry (`slife.timeouts`) that every
# plugin read.  There is no such registry here — a number is a constant beside
# the loop it paces, and a module about WeChat does not need to be reachable
# from a module about anything else.  The values are v1's, because each was
# arrived at against the live bridge and nothing about the port changes the
# bridge.

#: How long to wait between polls.  The call itself blocks until the bridge has
#: something to say, so this is the pause *after* an answer, not the latency.
POLL_SECONDS = 3.0
#: The ceiling a failing link backs off to, and the factor it grows by.  A link
#: that is down must not be re-polled every three seconds forever.
BACKOFF_FACTOR = 1.5
BACKOFF_MAX = 30.0
#: How long a message that has already been seen is read as the bridge
#: re-serving it rather than as somebody typing the same thing twice.
DEDUP_SECONDS = 30.0
#: How many messages one person's burst may have in flight before this process
#: stops handing them over.  A bound rather than a queue: `send_message` blocks
#: until its turn is done, so these are turns, and twenty of them is somebody
#: flooding a conversation rather than talking to it.
MAX_INFLIGHT = 16
#: How long to wait for the window when handing a message over.  The call it
#: makes answers as soon as the words are written down the stream that window is
#: holding (`slife2.tui.control`), so this bounds a window that is not answering
#: at all rather than a slow terminal.
WINDOW_TIMEOUT_SECONDS = 10.0
#: A login's lifetime, in seconds — 23 hours, from v1, which measured it against
#: the bridge's own ~24h expiry and left an hour of margin.  Past this a saved
#: token is not offered to the far end at all.
SESSION_MAX_AGE = 82_800.0

#: The login's filename inside one agent's directory.  **Not `wechat_<agent>`**:
#: the directory is the agent, and a filename repeating it is a name to keep in
#: step with a rename.
SESSION_NAME = "wechat.yaml"
#: The QR handshake: how often its status is asked, how long the whole thing may
#: take, and how many times an expired code is replaced before giving up.
QR_POLL_SECONDS = 2.0
QR_DEADLINE = 600.0
QR_MAX_ATTEMPTS = 3
#: The typing indicator expires at the far end after ~10-20s, so it is refreshed
#: well inside that; `TYPING_MAX_LIFETIME` bounds the keep-alive so a turn that
#: never ends cannot leave one refreshing forever.
TYPING_REFRESH = 8.0
TYPING_MAX_LIFETIME = 300.0


#: What the marker opens with.  A constant because the marker is *read* as well
#: as written: stored text carries it into a transcript, and the window takes it
#: back off by this string (:func:`strip_marker`) rather than by a second copy of
#: the spelling that could drift from this one.
MARKER_PREFIX = "[Wechat:"


def marker(peer_wechat_id: str, context_token: str | None = None) -> str:
    """The prefix an incoming message carries, naming who it is from.

    See the module docstring: the keys mirror `wechat_send_message`'s arguments
    so that a model replying to this does not have to translate anything.

    **The whole envelope, and nothing else** — `[Wechat:{…}]`, the payload hard
    against the colon.  The name is not in capitals, which is the half of the
    convention that says *content*: this marker goes into the turn's row and is
    in what the model remembers, where `slife2.context.TURN_PREFIX`'s capitals
    read as the harness's own annotation.  The space that sets a marker off from
    what it sits beside is outside the bracket, and it is the joiner's to add —
    `_hand_over` here, `slife2.context.with_note` there.
    """
    payload: dict[str, str] = {"peer_wechat_id": peer_wechat_id}
    if context_token is not None:
        payload["context_token"] = context_token
    return f"{MARKER_PREFIX}{json.dumps(payload, ensure_ascii=False)}]"


def strip_marker(content: str) -> str:
    """*content* as a person reads it: the marker taken back off.

    The marker is machine-facing — it is how the model learns the peer and the
    conversation's token, and it is *stored*, because the model reads it back out
    of the row (`annotate_turn`'s footnote is derived, this is content).  A screen
    has the channel already, as the bubble's own prefix, so leaving it in prints
    two ids at somebody who can do nothing with them.  v1's rule, from
    `unwrap_info_envelope`, minus its legacy branch — this build has no old prose
    prefix to strip, because nothing here ever wrote one.

    **A leader, and a leader that closes.**  A marker is a prefix by construction
    (`_hand_over`), so text that merely *mentions* one is left alone — and so is
    text whose bracket never closes, which is a torn row or a hand-edited file: a
    display that eats the rest of a message is worse than one that shows a marker
    it could not read.

    Callers strip only what arrived from another channel.  The words a person
    typed are theirs, marker-shaped or not, and a turn of this window's own is
    the one place a marker cannot legitimately be.
    """
    if not content.startswith(MARKER_PREFIX):
        return content
    end = content.find("]", len(MARKER_PREFIX))
    if end == -1:
        return content
    return content[end + 1 :].lstrip()


def render_qr(content: str) -> str:
    """A QR payload as something scannable off a terminal.

    Half-block characters (`█▀▄`), two QR rows per printed row, which is what
    makes a code fit on a screen without a window to resize.  The model copies
    this into its reply, and the person scans the screen they are reading.

    **Without `qrcode` the content is returned as it came**, which for this
    bridge is a URL — a person can still open it, and a plugin that refuses to
    start its login over a missing rendering library would be trading the whole
    feature for the convenience of one.  v1's fallback, and v1's reason.
    """
    if not content:
        return ""
    try:
        import qrcode
    except ImportError:
        logger.warning("qrcode is not installed; handing over the URL instead")
        return content
    code = qrcode.QRCode(border=1, box_size=1)
    code.add_data(content)
    code.make(fit=True)
    matrix = code.get_matrix()
    size = len(matrix)
    lines: list[str] = []
    for y in range(0, size, 2):
        row: list[str] = []
        for x in range(size):
            top = matrix[y][x]
            bottom = matrix[y + 1][x] if y + 1 < size else False
            if top and bottom:
                row.append("█")
            elif top:
                row.append("▀")
            elif bottom:
                row.append("▄")
            else:
                row.append(" ")
        lines.append("".join(row))
    return "\n".join(lines)


class Window:
    """Message keys seen recently, and forgotten after a while.

    The bridge re-serves recent messages until the ack cursor advances, so the
    same message arrives more than once and only a *re-delivery* may be dropped
    — somebody sending "收到" twice a minute apart is two messages.  A window is
    what tells those apart, and the window is why the answer is not a set.

    v1 wrote this with `cachetools.TTLCache`; what that does is a dict and a
    cut-off, and this project does not declare a dependency for a dict and a
    cut-off.
    """

    def __init__(self, window: float, limit: int) -> None:
        self._window = window
        self._limit = limit
        self._seen: dict[str, float] = {}

    def has(self, key: str, *, now: float) -> bool:
        """Whether *key* was recorded inside the window."""
        stamp = self._seen.get(key)
        return stamp is not None and now - stamp <= self._window

    def add(self, key: str, *, now: float) -> None:
        """Record *key*, dropping what has aged out and what will not fit."""
        self._seen[key] = now
        for stale in [k for k, at in self._seen.items() if now - at > self._window]:
            del self._seen[stale]
        # Insertion-ordered, so the oldest go first — which is the same order
        # they would have aged out in.
        while len(self._seen) > self._limit:
            self._seen.pop(next(iter(self._seen)))


class Session:
    """One agent's WeChat: its login, its cursor, its loop, its queue.

    Everything v1 held in module globals, keyed by the agent it belongs to.
    """

    def __init__(self, agent: str, *, http: Http) -> None:
        self.agent = agent
        self.client = IlinkClient(http=http)
        #: Message keys seen inside the dedup window — the bridge re-serves
        #: recent messages, so this is what tells a re-delivery from somebody
        #: typing the same thing twice.
        self.seen = Window(DEDUP_SECONDS, MAX_INFLIGHT * 3)
        #: When the login in hand was saved.  Kept here because the client's own
        #: `get_session_dict` stamps `saved_at` at read time — right for the next
        #: start's age check, wrong for "how much of this login is left", and
        #: wrong in the worst way if it were reused for both: the cursor is
        #: written every few seconds, so a refreshed `saved_at` would mean a
        #: login that never expires.
        self.saved_at: float = 0.0
        #: What the last restore attempt found, in the words a person needs.
        #: `""` after a success, and the reason a status read gives when the
        #: login it expected is not there.
        self.restore_note: str = ""
        #: Whether `last_contact` is somebody who *wrote* or the account the
        #: login belongs to.  Both are a peer a reply can go to and only one of
        #: them is a reply, so a status answer that called the second one "the
        #: last person to write" would be telling a model it is answering
        #: somebody who has not said anything.
        self.heard_from: bool = False
        #: The poll loop, one per session, started when a login is in hand.
        self.poll: asyncio.Task[None] | None = None
        #: One typing keep-alive per conversation, so a second message from the
        #: same person does not start a second refresher.
        self.typing: dict[str, asyncio.Task[None]] = {}
        #: Turns this session has handed to the agent and is waiting on.
        self.inflight: set[asyncio.Task[None]] = set()
        #: The last cursor written to the file, so it is written on change only.
        self.persisted: str = ""
        #: A QR login in progress, and v1's four words for where it is.
        self.qr_state: str = ""  # "" | waiting | scanned | confirmed | expired | error
        self.qr_error: str = ""
        self.qr_task: asyncio.Task[None] | None = None

    @property
    def polling(self) -> bool:
        return self.poll is not None and not self.poll.done()


# ── The session file ────────────────────────────────────────────────────
# One `wechat_<agent>.yaml` per login, in the data directory — v1's name, v1's
# keys and v1's short-lived-token reasoning, so a session saved by v1 is adopted
# by this plugin without anybody scanning anything again.  The token lives in
# plain text on purpose: it expires in about a day, and v1's note says why that
# is not worth a credential store.


def _yaml() -> YAML:
    yaml = YAML()
    yaml.default_flow_style = False
    return yaml


def session_path(agent: str) -> Path:
    """One agent's login: `agents/<agent>/wechat.yaml`.

    The agent is the *directory* now, so the file does not repeat its name —
    which is also what lets the startup scan find the agent in the path it
    already has instead of parsing it back out of a filename.
    """
    return agent_dir(agent) / SESSION_NAME


def load_session(agent: str) -> dict[str, Any]:
    """A saved login, or `{}` — for a file that is missing or unreadable.

    Unreadable reads as absent rather than raising: the file is a cache of a
    login, the login can be had again by scanning, and a torn write must not be
    able to stop the plugin from starting.
    """
    path = session_path(agent)
    try:
        raw = _yaml().load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 — a corrupt cache is a cache miss
        logger.warning("wechat session %s could not be read; ignoring it", path)
        return {}
    return raw if isinstance(raw, dict) else {}


def save_session(agent: str, session: Mapping[str, Any]) -> Path:
    """Write a login down, atomically.

    A temp file in the same directory and `os.replace`, so a reader sees the old
    file or the new one and never half of either — the same guarantee
    `slife2.runtime` gives its records, for the same reason: the poll loop
    rewrites this file every time the cursor moves.
    """
    path = session_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as opened:
            _yaml().dump(dict(session), opened)
            opened.flush()
            os.fsync(opened.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def clear_session(agent: str) -> bool:
    """Forget a login — the ack cursor goes with it, as it is only meaningful
    alongside a live session."""
    path = session_path(agent)
    if not path.exists():
        return False
    with contextlib.suppress(OSError):
        path.unlink()
        return True
    return False


#: How this plugin hands a message to the agent: `(agent, prompt)`, and it is a
#: seam because a test drives the whole loop without an agent server anywhere.
Deliver = Callable[[str, str], Awaitable[None]]


class Wechat:
    """Every agent's login, and the loop that watches each one."""

    def __init__(
        self,
        config: Config,
        *,
        http: Http = ilink.request,
        deliver: Deliver | None = None,
        directory: Path | None = None,
    ) -> None:
        self._config = config
        self._http = http
        self._deliver = deliver or self._ask_agent
        self._agents = directory or agents_dir()
        self._sessions: dict[str, Session] = {}

    def session(self, agent: str) -> Session:
        """One agent's session, started the first time it is asked for.

        **Created empty, and restored by :meth:`ready`** — a tool call must not
        be the thing that waits for a network round trip to a bridge that may
        be down, so what a fresh session is given here is its own state and
        nothing else.
        """
        found = self._sessions.get(agent)
        if found is None:
            found = Session(agent, http=self._http)
            self._sessions[agent] = found
        return found

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Resume every login this machine has, in the background.

        **The session files are the set of agents that have a login** — a shared
        process cannot be told which agents exist by anybody, and the files are
        already the record of who scanned a QR here.  v1 got the same effect
        from its harness calling `wechat_check_status` once at startup; the
        files are the better answer, because they survive the harness.

        Never awaited in the path that serves: a bridge that cannot be reached
        must not stop the plugin from coming up, and every one of these is
        re-triggerable by any tool call.
        """
        for path in sorted(self._agents.glob(f"*/{SESSION_NAME}")):
            # The agent is the directory's name, read off the path already in
            # hand rather than parsed back out of a filename.
            agent = path.parent.name
            if not agent:
                continue
            self._spawn(self._resume(self.session(agent)))

    async def close(self) -> None:
        """Take down every loop, keep-alive and in-flight turn."""
        for session in self._sessions.values():
            self._cancel(session.poll)
            session.poll = None
            for task in session.typing.values():
                self._cancel(task)
            session.typing.clear()
            self._cancel(session.qr_task)
            session.qr_task = None
            for task in list(session.inflight):
                self._cancel(task)
            session.inflight.clear()

    @staticmethod
    def _cancel(task: asyncio.Task[Any] | None) -> None:
        if task is not None and not task.done():
            task.cancel()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """A background task nobody awaits, held so it cannot be collected."""
        task = asyncio.create_task(coro)
        task.add_done_callback(self._finished)
        return task

    @staticmethod
    def _finished(task: asyncio.Task[Any]) -> None:
        """Report what a background task died of, so it is never silent."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("wechat background task failed: %r", error)

    # ── Restoring a login ────────────────────────────────────────────────

    async def ready(self, session: Session) -> None:
        """Adopt this agent's saved login, once per call at most.

        **A status read is one of the things that restores**, which is v1's
        behaviour and its reason: nothing else knows the agent exists until it
        asks, and a login that is saved but not live is the state a person is
        most likely to be looking at.  Every path is cheap when there is
        nothing to do — no file means no call, and a live session returns at the
        first line.
        """
        if session.client.is_logged_in:
            return
        saved = load_session(session.agent)
        if saved.get("bot_token"):
            await self._adopt(session, saved)

    async def _resume(self, session: Session) -> None:
        """The startup path: restore if there is anything to restore."""
        with contextlib.suppress(Exception):
            await self.ready(session)

    async def _adopt(self, session: Session, saved: Mapping[str, Any]) -> bool:
        """Take a saved login up, and start watching it.

        Three outcomes and they are told apart, because the sentence a person
        needs is different for each: too old (scan again), refused (the file is
        gone, scan again), or unreachable right now (keep the file, keep
        retrying — the token is probably fine and the network is not).  Each
        writes its own `restore_note`, which is what a status read repeats
        rather than re-deriving from the file.
        """
        client = session.client
        if not await client.try_restore_session(saved, max_age=SESSION_MAX_AGE):
            session.restore_note = (
                "the saved login is older than one lasts, so it was not offered "
                "to the server"
            )
            logger.info("wechat %s: saved login is past its age", session.agent)
            return False
        if not await client.validate_session():
            if client.auth_failed:
                # The far end said no.  Keeping the token would make every later
                # status read a phantom `connected`, which is the failure this
                # whole check exists to prevent.
                client.reject_session()
                clear_session(session.agent)
                session.restore_note = (
                    "the server rejected the saved login, so it has been forgotten"
                )
                logger.warning("wechat %s: the saved login was rejected", session.agent)
            else:
                # **The token is let go, and the file is kept.**  This is the one
                # outcome of the three that has to be *reversible*:
                # `try_restore_session` adopted the token before anything
                # confirmed it, so leaving it in place would make
                # `is_logged_in` true — and `ready` short-circuits on exactly
                # that, so no later read would ever try again and the poll loop
                # would never start.  A bridge that was unreachable for one
                # moment at boot would cost the process every message it runs
                # for.  Dropping the in-memory token (not the file) is what makes
                # "keep the file, keep retrying" true rather than a promise.
                reason = client.last_error or "no answer"
                client.reject_session()
                session.restore_note = (
                    f"the saved login could not be confirmed just now ({reason})"
                )
            return False
        client.clear_session_faults()
        session.restore_note = ""
        session.saved_at = float(saved.get("saved_at") or time.time())
        # Seed the cursor, so the first poll resumes ack'ing rather than
        # re-ingesting the replay window the previous process already had.
        seed = str(saved.get("get_updates_buf") or "")
        if seed:
            client.updates_buf = seed
        session.persisted = seed
        # And the peer, so a model that only reads status still knows who to
        # answer — v1 seeded this from the login for the same reason.
        owner = str(saved.get("ilink_user_id") or "")
        if owner:
            client.last_contact = {"from_id": owner, "context_token": ""}
        session.heard_from = False
        self.watch(session)
        logger.info("wechat %s: login resumed", session.agent)
        return True

    # ── Polling ──────────────────────────────────────────────────────────

    def watch(self, session: Session) -> None:
        """Start this session's poll loop, if it is not already running."""
        if session.poll is None or session.poll.done():
            session.poll = self._spawn(self._poll(session))

    async def _poll(self, session: Session) -> None:
        """Watch one login for messages, until it stops being a login.

        The loop ends when the session does — logged out, or a token the far
        end refused — and never on a *transient* failure, which is what the
        backoff is for.  `poll_updates` records its own failures rather than
        raising, so what is left here is the cadence and the queue.
        """
        client = session.client
        backoff = POLL_SECONDS
        logger.info("wechat %s: polling every %.1fs", session.agent, POLL_SECONDS)
        while client.is_logged_in and not client.auth_failed:
            try:
                messages = await client.poll_updates()
                # **A batch, not a message at a time**, because "seen" means
                # "seen before this batch": two identical texts arriving in one
                # answer are somebody sending "ok" twice.  See `_queued`.
                batch: set[str] = set()
                for message in messages:
                    self._queued(session, message, batch)
                self._persist(session)
            except Exception as exc:  # noqa: BLE001 — the loop's job is to keep going
                client.last_error = str(exc)
                logger.debug("wechat %s: poll failed: %s", session.agent, exc)
            backoff = (
                min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)
                if client.last_error
                else POLL_SECONDS
            )
            await asyncio.sleep(backoff)
        logger.info("wechat %s: polling stopped", session.agent)

    def _queued(
        self, session: Session, message: Mapping[str, Any], batch: set[str]
    ) -> None:
        """One message off the wire: deduped, remembered, handed over.

        **Text only.**  Anything that is not a text item is skipped *before* the
        dedup key is recorded, because an image or a voice note would otherwise
        burn the key for the conversation and take the next real message with
        it — v1 found that one, and the ordering is the fix.
        """
        items = message.get("item_list")
        first = items[0] if isinstance(items, list) and items else {}
        item = first.get("text_item") if isinstance(first, Mapping) else {}
        text = str((item or {}).get("text") or "")
        if not text.strip():
            return

        now = time.time()
        peer = str(message.get("from_user_id") or "")
        token = str(message.get("context_token") or "")
        key = f"{peer}::{token}::{text}"
        # **The window is asked, and then the batch is**, and the pair is what
        # keeps re-delivery and repetition apart.  A key the window held
        # *before* this answer arrived is the bridge re-serving a message the
        # previous poll already had, so it is dropped.  A key this batch already
        # delivered is somebody having sent the same words twice, so it is not —
        # which is the case v1 shipped a version without, and the version lost
        # the second "ok".
        #
        # The cost is stated rather than hidden: a repeat of a text seen inside
        # the last `DEDUP_SECONDS` that arrives *alongside its own replay* is
        # indistinguishable from the replay, and is dropped with it.
        if session.seen.has(key, now=now) and key not in batch:
            return
        batch.add(key)
        session.seen.add(key, now=now)

        session.client.last_contact = {"from_id": peer, "context_token": token}
        session.heard_from = True
        self._hand_over(session, peer, token, text)

    def _hand_over(self, session: Session, peer: str, token: str, text: str) -> None:
        """Give one message to the agent as a turn of its own.

        **A task, not an await** — and that is the difference between polling
        and not.  `send_message` returns when its turn is *finished*, and a turn
        is a model thinking: awaiting it here would stop watching the login for
        the whole of every reply, so a second message sent mid-turn would sit at
        the bridge instead of being folded into the turn that is already
        running.  The agent server serialises one key's turns itself, so
        handing them all over at once is exactly what it expects.
        """
        if len(session.inflight) >= MAX_INFLIGHT:
            # Said rather than dropped quietly: this is somebody's message, and
            # the reason it is not being answered is a fact they cannot see.
            logger.warning(
                "wechat %s: %d messages in flight already; dropping one from %s",
                session.agent,
                len(session.inflight),
                peer,
            )
            return
        # The space that sets the marker off from the message is added *here*:
        # outside the bracket is the joiner's side of the seam, which is where
        # `slife2.context.with_note` puts the one before a footnote.
        prompt = f"{marker(peer, token)} {text}"
        task = self._spawn(self._answer(session, peer, token, prompt))
        session.inflight.add(task)
        task.add_done_callback(session.inflight.discard)

    async def _answer(
        self, session: Session, peer: str, token: str, prompt: str
    ) -> None:
        """Hand over one prompt, with the typing indicator up for its duration.

        The keep-alive goes down here *and* in `wechat_send_message`, and the
        two are not redundant: a model that replies and then keeps working has
        stopped needing to look busy at the reply, and a turn that ends without
        a reply must not leave an indicator refreshing until its own bound.
        """
        self._typing_on(session, peer, token)
        try:
            await self._deliver(session.agent, prompt)
        except Exception as exc:  # noqa: BLE001 — one message is not the loop
            # The text is not logged: it is a private message, and its length is
            # the part that matters to whoever is reading the log.
            logger.warning(
                "wechat %s: could not hand over a %d-character message from %s: %s",
                session.agent,
                len(prompt),
                peer,
                exc,
            )
        finally:
            self._typing_off(session, peer)

    def _persist(self, session: Session) -> None:
        """Write the ack cursor, when it has moved.

        **On change only**, and the file's own `saved_at` is carried across
        rather than re-stamped.  Both halves are the same concern: this runs
        after every poll, and a cursor written every three seconds would be a
        file the poll loop spends its life touching — while a re-stamped
        `saved_at` would be worse than wasteful, because that field is the
        login's age and refreshing it every few seconds is a login that never
        expires.  v1 merged the cursor into the file for the same reason.
        """
        cursor = session.client.updates_buf
        if not cursor or cursor == session.persisted or not session.client.is_logged_in:
            return
        written = session.client.get_session_dict()
        written["saved_at"] = session.saved_at or written["saved_at"]
        written["get_updates_buf"] = cursor
        try:
            save_session(session.agent, written)
        except OSError as exc:
            logger.debug("wechat %s: cursor not saved: %s", session.agent, exc)
            return
        session.persisted = cursor

    # ── The typing indicator ─────────────────────────────────────────────

    def _typing_on(self, session: Session, peer: str, token: str) -> None:
        """Show "typing…", and keep showing it while the turn runs."""
        if peer in session.typing and not session.typing[peer].done():
            return
        session.typing[peer] = self._spawn(self._keep_typing(session, peer, token))

    def _typing_off(self, session: Session, peer: str) -> None:
        task = session.typing.pop(peer, None)
        self._cancel(task)

    async def _keep_typing(self, session: Session, peer: str, token: str) -> None:
        """Refresh the indicator inside its own expiry, and not forever.

        The bound is the point: the ordinary stop is the reply going out, and a
        turn that dies without one — a crash, a cancel — must not leave this
        refreshing until the process ends.

        **A failed refresh is swallowed and a *cancellation* is not**, and the
        difference is the whole of why this is not a `suppress`.  Cancelling is
        how this stops, so a `CancelledError` caught here would be caught by the
        loop that is being cancelled: the task would go on sleeping and
        refreshing, unkillable, holding the event loop open.  `except Exception`
        does not catch it, which is exactly right — a courtesy that failed is
        not a reason to keep going, and a cancellation is not a failure.
        """
        deadline = time.monotonic() + TYPING_MAX_LIFETIME
        while time.monotonic() < deadline:
            try:
                await session.client.send_typing(peer, token, status=1)
            except Exception as exc:  # noqa: BLE001 — a courtesy, not a contract
                logger.debug("wechat %s: typing not shown: %s", session.agent, exc)
            await asyncio.sleep(TYPING_REFRESH)

    # ── Handing over, for real ───────────────────────────────────────────

    async def _ask_agent(self, agent: str, prompt: str) -> None:
        """Hand one message over to the window showing this agent's conversation.

        **The window and not the agent server**, and that is the whole of what
        makes a message sent from a phone visible in the terminal: it goes into
        the same queue the person's own typing goes into and is run as the same
        kind of turn, so the line, the answer and the record are the ones that
        window already draws (`slife2.tui.app`).  What the terminal does with it —
        one turn at a time, in arrival order — is the one inbox everything this
        system takes goes into.

        **The agent is named, and that is what makes this work with more than one
        window open.**  It used to be implicit in the address, which is one per
        machine, so a second terminal could not be reached at all; now the plugin
        keeps a session per agent and this says which one (`slife2.tui.control`).

        **The call returns when the words are on their way to that window, not
        when the turn ends**, and that is what makes the retry below safe: a
        connection that died *after* they were written cannot make them run twice.

        The connection is per delivery rather than held: this process polls for
        hours and speaks only when somebody writes, so a handshake on the message
        that needs it costs one loopback round trip and keeps nothing open in
        between.
        """
        for attempt in (1, 2):
            try:
                await self._to_window(agent, prompt)
                return
            except Exception:
                if attempt == 2:
                    raise

    async def _to_window(self, agent: str, prompt: str) -> None:
        """One delivery: the window takes it, or this raises.

        **Not `_hand_over`, which is already this class's word for something
        else** — the poll loop's "give this message to the agent as a turn of its
        own" (`_hand_over` above, which spawns the task).  One of these names came
        first and the other is a call into another process, and having the second
        silently replace the first is a mistake worth a sentence rather than a
        memory.
        """
        window = await open_server(
            # The plugin's address, and the same one every agent shares — what
            # picks out *this* agent is the argument below, not the endpoint.
            self._config.server("tui").url,
            name=TUI_SERVER_NAME,
            fallback_tool="submit",
            timeout=WINDOW_TIMEOUT_SECONDS,
        )
        try:
            # **The call returning is the acknowledgement** — the words are on
            # their way down the stream that agent's window is holding by then
            # (`slife2.tui.control`), which is why a transport failure after this
            # point must not be retried into a second turn.
            await window.call_tool(
                "submit",
                {"agent": agent, "text": prompt, "channel": CONFIG_KEY},
                timeout=WINDOW_TIMEOUT_SECONDS,
            )
        finally:
            with contextlib.suppress(Exception):
                await window.__aexit__(None, None, None)

    # ── The QR login ─────────────────────────────────────────────────────

    async def login(self, session: Session) -> str:
        """Start a QR login, and answer with what to show.

        The QR is fetched and then watched *in the background*: a scan is a
        person finding their phone, and a tool call that waited for one would
        hold a turn open for a minute.  What this hands back is the code and the
        name of the tool that reports how it is going, which is v1's shape.
        """
        # **Before the "already connected" question is asked**, because a login
        # that is saved but not yet resumed looks exactly like no login at all —
        # so without this a model that calls `wechat_login` first would be told
        # to scan a code it does not need.
        await self.ready(session)
        client = session.client
        if client.is_logged_in and not client.auth_failed:
            return (
                "WeChat is already connected. Call `wechat_logout` first to "
                "switch to another account."
            )
        if client.auth_failed:
            # A token the far end refused is not a session to build on; drop it
            # and let the scan replace it, rather than refusing the login.
            client.reject_session()
            clear_session(session.agent)

        session.qr_state = "waiting"
        session.qr_error = ""
        try:
            data = await client.fetch_qrcode(ilink.BASE_URL)
        except Exception as exc:  # noqa: BLE001 — reported to the model as text
            session.qr_state = ""
            logger.warning("wechat %s: QR fetch failed: %s", session.agent, exc)
            return f"The login QR could not be fetched: {exc}"
        qrcode = str(data.get("qrcode") or "")
        if not qrcode:
            session.qr_state = ""
            return "The server answered without a login QR; try again in a moment."
        content = str(data.get("qrcode_img_content") or qrcode)

        self._cancel(session.qr_task)
        session.qr_task = self._spawn(
            self._qr_watch(session, qrcode, ilink.BASE_URL, attempts=0)
        )
        rendered = render_qr(content)
        return (
            "Show this in your reply, so it can be scanned from the screen the "
            "person is looking at:\n\n"
            f"{rendered}\n\n"
            f"If it cannot be scanned: {content}\n\n"
            "`wechat_check_status` reports how the scan is going. It expires "
            "after about ten minutes."
        )

    async def _qr_watch(
        self, session: Session, qrcode: str, base_url: str, *, attempts: int
    ) -> None:
        """Follow one QR until it is confirmed, replaced, or out of time."""
        deadline = time.monotonic() + QR_DEADLINE
        while time.monotonic() < deadline:
            await asyncio.sleep(QR_POLL_SECONDS)
            status = await session.client.poll_login_status(qrcode, base_url)

            if redirect := status.get("redirect_base"):
                # Scanned on another node.  Without this the loop keeps asking
                # the original host for a confirmation that will never come.
                base_url = str(redirect)
                continue
            if token := status.get("bot_token"):
                await self._confirmed(session, status, str(token), base_url)
                return
            if status.get("expired"):
                if attempts >= QR_MAX_ATTEMPTS:
                    session.qr_state = "expired"
                    session.qr_error = "The QR expired more than three times."
                    return
                refreshed = await session.client.fetch_qrcode(base_url)
                fresh = str(refreshed.get("qrcode") or "")
                if not fresh:
                    session.qr_state = "error"
                    session.qr_error = (
                        "The QR expired and a new one could not be fetched."
                    )
                    return
                session.qr_state = "waiting"
                session.qr_task = self._spawn(
                    self._qr_watch(session, fresh, base_url, attempts=attempts + 1)
                )
                return
            if status.get("scanned"):
                session.qr_state = "scanned"
            if status.get("verify_code_blocked"):
                session.qr_state = "error"
                session.qr_error = "The server blocked the verify code."
                return
            if status.get("need_verifycode"):
                # **Two states the poll can answer that this loop has to say
                # something about**, and both used to fall through to the next
                # poll — so the login sat here until the ten-minute deadline and
                # then blamed the person for not scanning a code they had
                # already scanned.
                #
                # This one wants a code typed into WeChat.  `poll_login_status`
                # takes one; nothing in this build supplies it, so the honest
                # answer is that the login cannot finish here rather than a wait
                # that ends in the wrong sentence.
                session.qr_state = "error"
                session.qr_error = (
                    "WeChat asked for a verification code, which this build has "
                    "no way to send. Pair the account another way, or scan again "
                    "and choose the option that does not ask for one."
                )
                return
            if status.get("already_connected"):
                session.qr_state = "error"
                session.qr_error = (
                    "This account is already bound to another WeChat login."
                )
                return
        session.qr_state = "error"
        session.qr_error = "Nobody scanned the code in ten minutes."

    async def _confirmed(
        self,
        session: Session,
        status: Mapping[str, Any],
        token: str,
        base_url: str,
    ) -> None:
        """The scan worked: adopt the token, save it, and start watching."""
        owner = str(status.get("ilink_user_id") or "")
        await session.client.start(
            token,
            str(status.get("baseurl") or base_url),
            ilink_user_id=owner,
            ilink_bot_id=str(status.get("ilink_bot_id") or ""),
        )
        if owner:
            session.client.last_contact = {"from_id": owner, "context_token": ""}
        session.client.clear_session_faults()
        saved = session.client.get_session_dict()
        save_session(session.agent, saved)
        session.saved_at = float(saved["saved_at"])
        session.persisted = ""
        session.heard_from = False
        # The note a failed restore left is stale the moment a login works.
        session.restore_note = ""
        session.qr_state = "confirmed"
        self.watch(session)
        logger.info("wechat %s: logged in", session.agent)

    # ── The tools ────────────────────────────────────────────────────────
    # Four, all of them for the model, and all four need to know *which*
    # conversation is asking: a shared process cannot tell whose phone a scan
    # belongs to, and the client id every call carries is the answer.

    def who(self, ctx: Context) -> str:
        """The agent a call is on behalf of, or `""` if the call does not say.

        The identity is the caller's, never this process's — one wechat plugin
        serves every agent, so "whose login" is a fact only the caller has.

        **The agent and not the conversation**, so a worker shares its agent's
        WeChat: a login belongs to the account that scanned the code, and a
        subagent is a means to an end inside one of that agent's turns rather
        than a second phone.  A worker asking about WeChat is asking about its
        parent's, which is the only answer that could be true.
        """
        found = request_client(ctx)
        return found[0] if found else ""

    def unaddressed(self) -> str:
        """What a tool says when a call arrived without a conversation on it."""
        return (
            "This call did not say which conversation it is for, so there is no "
            "WeChat login to act on — a plugin serves every agent, and the login "
            "is the agent's."
        )

    async def status(self, session: Session) -> str:
        """`wechat_check_status`, as sentences.

        Restores first, so a saved login is live by the time the answer is
        written — see :meth:`ready`.  Then the states a session can be in, told
        apart because the sentence a person needs is different for each: a QR
        nobody has scanned, no login at all, a link that is up but not
        answering, and a session that works.
        """
        await self.ready(session)
        client = session.client

        # A token the far end refused — most often discovered by a *send*, which
        # is the one call that finds out without a poll.  It is dropped here for
        # the reason `_adopt` drops it: a session that cannot work must not be
        # reported as one that does.
        if client.auth_failed:
            client.reject_session()
            clear_session(session.agent)
            session.restore_note = (
                "the server rejected the login, so it has been forgotten"
            )

        if session.qr_state and not client.is_logged_in:
            return self._qr_status(session)

        if not client.is_logged_in:
            if session.restore_note:
                return (
                    f"WeChat is not connected: {session.restore_note}. Call "
                    f"`wechat_login` to scan a new code."
                )
            return (
                "WeChat is not connected, and no login is saved. Call "
                "`wechat_login` to put a QR code in front of whoever will scan it."
            )

        left = max(0.0, SESSION_MAX_AGE - (time.time() - session.saved_at))
        head = (
            f"WeChat is connected — about {left / 3600:.1f} hours left on this "
            f"login — and "
            + ("watching for messages." if session.polling else "not polling.")
        )
        if client.last_error:
            # Up but not answering, which is not the same as down: the token is
            # still good, so the advice is to wait rather than to re-scan.
            return (
                f"WeChat is connected but not answering: {client.last_error}.\n"
                f"Messages will not arrive until that clears; nothing needs doing. "
                f"{self._contact(session)}"
            )
        return f"{head}\n{self._contact(session)}"

    def _qr_status(self, session: Session) -> str:
        """What a status read says while a QR login is in progress."""
        return {
            "waiting": "A login QR is waiting to be scanned.",
            "scanned": "The QR has been scanned — confirm it on the phone.",
            "confirmed": (
                "The scan was confirmed but no session came back; call "
                "`wechat_login` again."
            ),
            "expired": f"The login QR expired ({session.qr_error}). `wechat_login` "
            f"starts a new one.",
            "error": f"The login did not work: {session.qr_error} `wechat_login` "
            f"starts over.",
        }.get(session.qr_state, "A login is in progress.")

    def _contact(self, session: Session) -> str:
        """Who a reply goes to, and the call that sends it.

        The one thing a model cannot work out for itself: the peer and the
        conversation's token arrive with a message, and a model that has lost
        them — a new turn, a trimmed context — has no way to ask a person.
        """
        contact = session.client.last_contact
        peer = str(contact.get("from_id") or "")
        token = str(contact.get("context_token") or "")
        if not peer:
            return (
                "Nobody has written yet — messages start arriving once somebody does."
            )
        if not session.heard_from:
            # v1 seeded the login's own account as a contact for this reason:
            # until somebody writes there is still one peer a message can reach,
            # and it is whoever scanned the code.  Said as what it is rather than
            # as "the last person to write", which would have a model believe it
            # is answering somebody who has said nothing.
            return (
                f"Nobody has written yet. This login belongs to {peer}, so that is "
                f"who a message can reach: {self._reply_call(peer, token)}"
            )
        return (
            f"The last person to write is {peer}; reply with "
            f"{self._reply_call(peer, token)}"
        )

    @staticmethod
    def _reply_call(peer: str, token: str) -> str:
        """The call that answers *peer*, written out so a model can copy it."""
        if token:
            return (
                f'`wechat_send_message(peer_wechat_id="{peer}", '
                f'context_token="{token}")`'
            )
        return f'`wechat_send_message(peer_wechat_id="{peer}")`'

    async def send(self, session: Session, peer: str, token: str, text: str) -> str:
        """`wechat_send_message`: one text, to one conversation."""
        await self.ready(session)
        client = session.client
        if not client.is_logged_in:
            return "WeChat is not connected. Call `wechat_login` first."
        if not peer.strip() or not text.strip():
            return (
                "Both `peer_wechat_id` and `text` are needed, and neither may be empty."
            )

        # The reply is going out, so the indicator comes down now rather than
        # when the turn ends — the model may have more work to do, and a person
        # who has been answered should stop being told otherwise.
        self._typing_off(session, peer)
        try:
            result = await client.send_message(peer, token, text)
        except Exception as exc:  # noqa: BLE001 — reported to the model as text
            logger.warning("wechat %s: send to %s failed: %s", session.agent, peer, exc)
            return f"The message could not be sent: {exc}"

        # A rejected token comes back as a 200 carrying an error envelope, and
        # a send that reads that as success is a reply the model believes it
        # delivered.  v1 lost this half and found it.
        if ilink.error_envelope(result):
            client.auth_failed = True
            client.last_error = ilink.envelope_error(result) or "send rejected"
            return (
                f"The server rejected the send: {client.last_error}. The login "
                f"has been marked as no longer working — `wechat_check_status` "
                f"says what to do, and `wechat_login` is usually it."
            )
        with contextlib.suppress(Exception):
            await client.send_typing(peer, token, status=2)
        logger.debug(
            "wechat %s: sent %d characters to %s", session.agent, len(text), peer
        )
        return f"Sent to {peer}."

    async def logout(self, session: Session) -> str:
        """`wechat_logout`: stop watching, forget the login, forget its QR."""
        self._cancel(session.poll)
        session.poll = None
        for typed in list(session.typing):
            self._typing_off(session, typed)
        self._cancel(session.qr_task)
        session.qr_task = None
        # The QR state goes with the session, and this is a fix rather than
        # tidiness: `wechat_check_status` reads it first, so a leftover
        # "confirmed" from the login just ended reported a scan still pending —
        # and told the model to wait for one that had already happened.
        session.qr_state = ""
        session.qr_error = ""
        # A fresh client rather than a cleared one, so nothing of the old
        # session survives in a field somebody forgot to reset — which is what
        # v1 did, and why.
        session.client = IlinkClient(http=self._http)
        session.persisted = ""
        session.saved_at = 0.0
        session.restore_note = ""
        clear_session(session.agent)
        logger.info("wechat %s: logged out", session.agent)
        return (
            "Logged out of WeChat, and the saved login is gone. Call "
            "`wechat_login` to connect again."
        )


def build_server(
    config: Config,
    *,
    http: Http = ilink.request,
    deliver: Deliver | None = None,
) -> FastMCP:
    """The server slife2 starts, wired to one :class:`Wechat`.

    `http` and `deliver` are the two seams, and they are the two things that
    reach outside this process: the bridge, and the agent server.  Both have a
    real default and both are replaceable, so a test drives the whole login,
    poll, delivery and reply path with neither a socket nor an agent.
    """
    plugin = Wechat(config, http=http, deliver=deliver)

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        await plugin.start()
        try:
            yield {}
        finally:
            await plugin.close()

    mcp: FastMCP = house_server(
        SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan
    )

    @mcp.tool(name="wechat_login", meta=FOR_THE_MODEL)
    async def wechat_login(ctx: Context) -> str:
        """Get a QR code that logs this agent into WeChat.

        Put the code in your reply for somebody to scan with the WeChat app:
        the account that scans is the account this conversation speaks as, and
        other people reach this conversation by messaging it. The code expires
        in about ten minutes, `wechat_check_status` reports the scan, and the
        login lasts about a day.
        """
        agent = plugin.who(ctx)
        if not agent:
            return plugin.unaddressed()
        return await plugin.login(plugin.session(agent))

    @mcp.tool(name="wechat_check_status", meta=FOR_THE_MODEL)
    async def wechat_check_status(ctx: Context) -> str:
        """How the WeChat link is: connected, waiting for a scan, or down.

        Also the one place that says who to reply to: the last person to write,
        with the `context_token` their conversation needs.
        """
        agent = plugin.who(ctx)
        if not agent:
            return plugin.unaddressed()
        return await plugin.status(plugin.session(agent))

    @mcp.tool(name="wechat_send_message", meta=FOR_THE_MODEL)
    async def wechat_send_message(
        ctx: Context,
        peer_wechat_id: str,
        text: str,
        context_token: str = "",
    ) -> str:
        """Send a WeChat message to somebody who has written to you.

        An incoming message carries its peer and context token in a
        `[Wechat:{...}]` prefix: pass those back here and the reply lands in the
        conversation it belongs to. `wechat_check_status` reports the same two
        values. A turn that sends nothing reaches nobody.

        Args:
            peer_wechat_id: The id to send to, from the message's marker or
                `wechat_check_status`.
            text: The message body.
            context_token: The conversation's token, from the same place. Empty
                starts a new thread, which a first message wants.
        """
        agent = plugin.who(ctx)
        if not agent:
            return plugin.unaddressed()
        return await plugin.send(
            plugin.session(agent), peer_wechat_id, context_token, text
        )

    @mcp.tool(name="wechat_logout", meta=FOR_THE_MODEL)
    async def wechat_logout(ctx: Context) -> str:
        """Disconnect WeChat and forget the saved login.

        The account stays logged in on the phone; what goes is this
        conversation's token and its place in the message stream. Messages
        arrive again after a new scan, which is how accounts are switched.
        """
        agent = plugin.who(ctx)
        if not agent:
            return plugin.unaddressed()
        return await plugin.logout(plugin.session(agent))

    return mcp


def main(argv: list[str] | None = None) -> int:
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        build=build_server,
        logger=logger,
        # What this process has, said where every server says what it is doing:
        # there is no section to describe, and the *number of saved logins* is
        # the one fact about a start that a person cannot get anywhere else —
        # the files are the only list of who this process is about to serve.
        note=lambda _config: _saved_note(),
    )


def _saved_note() -> str:
    """The start-up line's parenthesis: how many logins are on disk.

    Read off the data directory rather than out of a built `Wechat`, because
    `serve_plugin` asks for this before it builds the server — which is the
    point of it being text about a *start* rather than about the process.
    """
    count = len(list(agents_dir().glob(f"*/{SESSION_NAME}")))
    return f"{count} saved login(s)" if count else "no logins saved"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
