"""slife2-wechat — WeChat, as the plugin that holds every agent's login.

WeChat opens no API to a program, only the hosted bridge `slife2.plugins.wechat.ilink` speaks.
What this module adds is the part that makes it a *plugin* here: a session per
agent, the poll loop that watches each one, the delivery that turns an incoming
message into a turn, and the four tools the model uses to log in and reply.

**One process, one login per agent** — and that sentence is the whole of what
changed in the port.  v1 ran one wechat child per agent, so its module globals
*were* that agent's: `_client`, `_pending`, `_seen_keys` were one login, one
queue and one dedup window because there was exactly one agent in the process.
slife2's plugins are shared (`slife2.libs.plugin.protocol`), so those four things live in
a :class:`Session` keyed by the calling agent — the first half of the client id
every call already carries (`slife2.libs.plugin.audience`).  A second agent that scans its
own QR gets its own session file, its own poll loop and its own conversation,
and neither can see the other's.

**The plugin hands each message to the window.**  v1's harness polled the
plugin for messages (`__wechat_drain_incoming`) and injected them into the inbox
itself, so the plugin's job ended at "here is what arrived" and a second process
decided what that meant.  Here the plugin delivers: the window showing this
agent's conversation takes the message into the same queue the person's own
typing goes into, and runs it as the same kind of turn — so a message sent from a
phone is *visible in the terminal*, wears the channel it came in on, and is
answered there, with the one inbox this system has (see `slife2.plugins.tui.server`).
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
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.libs.harness.handover import hand_over, note_to_window
from slife2.libs.host import statefile
from slife2.libs.host.config import WECHAT_SERVER_NAME, Config
from slife2.libs.host.paths import agent_dir, in_home, known_homes
from slife2.libs.host.paths import home as home_dir
from slife2.libs.plugin.audience import FOR_THE_MODEL, unaddressed
from slife2.libs.plugin.protocol import agent_or_blank, house_server, serve_plugin
from slife2.plugins.wechat import ilink
from slife2.plugins.wechat.ilink import Http, IlinkClient
from slife2.tui import notes

logger = logging.getLogger(__name__)

SERVER_NAME = WECHAT_SERVER_NAME

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
#: The ceiling a failing link backs off to, and the factor it grows by.  A link
#: that is down must not be re-polled every three seconds forever.
BACKOFF_FACTOR = 1.5
#: How long a message that has already been seen is read as the bridge
#: re-serving it rather than as somebody typing the same thing twice.
#: How many messages one person's burst may have in flight before this process
#: stops handing them over.  A bound rather than a queue: `send_message` blocks
#: until its turn is done, so these are turns, and twenty of them is somebody
#: flooding a conversation rather than talking to it.
MAX_INFLIGHT = 16

#: The login's filename inside one agent's directory.  **Not `wechat_<agent>`**:
#: the directory is the agent, and a filename repeating it is a name to keep in
#: step with a rename.
SESSION_NAME = "wechat.yaml"
#: The key a saved login carries its **raw** agent name under.  The directory
#: cannot: it holds the *sanitised* name (`a/b` lands in `a_b/`, `slife2.libs.store.db`),
#: which is not the name a call arrives under — so a session resumed from the
#: directory would be a second key for one login, and a tool call from the raw
#: name would adopt the same token and start a second poll loop.  This is the
#: same row `slife2-schedule` keeps in its ledger's `meta` table, and for the
#: same reason: the shared process has nothing but the file to go on.
AGENT_KEY = "agent"
#: The QR handshake: how often its status is asked, how long the whole thing may
#: take, and how many times an expired code is replaced before giving up.
QR_MAX_ATTEMPTS = 3


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
    in what the model remembers, where `slife2.libs.turn.context.TURN_PREFIX`'s capitals
    read as the harness's own annotation.  The space that sets a marker off from
    what it sits beside is outside the bracket, and it is the joiner's to add —
    `_hand_over` here, `slife2.libs.turn.context.with_note` there.
    """
    payload: dict[str, str] = {"peer_wechat_id": peer_wechat_id}
    if context_token is not None:
        payload["context_token"] = context_token
    return f"{MARKER_PREFIX}{json.dumps(payload, ensure_ascii=False)}]"


#  Taking the marker back off is `slife2.libs.harness.markers.strip`, which is the shape and
#  not the name: the window hands in every marker it knows about and this
#  module's is one of them, exactly as `slife2.plugins.agent_task.server`'s is.  It
#  lives there rather than here because there are two writers now, and a
#  function per marker would be two things to keep in step.


def render_qr(content: str) -> str:
    """A QR payload as something scannable off a terminal.

    Half-block characters (`█▀▄`), two QR rows per printed row, which is what
    makes a code fit on a screen without a window to resize.  The model copies
    this into its reply, and the person scans the screen they are reading.

    **Without `qrcode` this refuses rather than handing back the payload.**  It
    used to return the content as it came — a URL the caller then told the model
    to show "so it can be scanned", which is not something a URL can be, so a
    weaker path was selected silently (DESIGN.md §11.1).  The `ImportError`
    goes to the caller, which says what actually happened instead of reading a
    link as a code.
    """
    if not content:
        return ""
    # The one import of this package, inside the one function that draws it
    # (pyproject's note): a build without it still runs everything else.
    import qrcode

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

    def __init__(self, agent: str, *, http: Http, home: str = "") -> None:
        self.agent = agent
        #: The data directory this login lives in, **kept rather than looked
        #: up**.  The poll loop outlives the call that started it, so a path
        #: resolved from the ambient home inside it would be answered by
        #: whatever directory the *daemon* is in — and the cursor would be
        #: written into another home's login file (`slife2.libs.host.paths.home`).
        self.home = home
        self.client = IlinkClient(http=http)
        #: Message keys seen inside the dedup window — the bridge re-serves
        #: recent messages, so this is what tells a re-delivery from somebody
        #: typing the same thing twice.
        self.seen = Window(_T.timeouts.pacing.wechat_dedup, MAX_INFLIGHT * 3)
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
        #: The code in hand and what it encodes.  **The session's, not the
        #: login call's**: a QR that expires is replaced in the background, and
        #: a code returned only by `wechat_login` would be a code nobody could
        #: scan while the status still said `waiting`.
        self.qr: str = ""
        self.qr_url: str = ""
        self.qr_task: asyncio.Task[None] | None = None
        #: The link state this session has already said, so a transition is
        #: announced once and a state that has not moved is announced not at
        #: all (`_announce`).  `""` is "nothing has been said yet", which is also
        #: what says a link that is already down is not news — see `_announce`.
        self.announced: str = ""

    @property
    def polling(self) -> bool:
        return self.poll is not None and not self.poll.done()


# ── The session file ────────────────────────────────────────────────────
# One `wechat.yaml` per login, inside the agent's own directory.  v1 kept
# `wechat_<agent>.yaml` beside the data, and the *keys* and the short-lived-token
# reasoning come over from it unchanged, so a session v1 saved is adopted here
# without anybody scanning anything again; the path does not, because the
# directory already names the agent and a filename repeating it is a second name
# to keep in step with a rename.
#
# What the path cannot carry is the agent's **raw** name, which is why the file
# states it under `AGENT_KEY`: the directory holds the *sanitised* one (`a/b`
# lands in `a_b/`), and a session resumed under the directory name would be a
# second key for one login — a tool call from the raw name would adopt the same
# token and start a second poll loop.  The token itself lives in plain text on
# purpose: it expires in about a day, and v1's note says why that is not worth a
# credential store.


def session_path(agent: str) -> Path:
    """One agent's login: `agents/<agent>/wechat.yaml`.

    The agent is the *directory* now, so the file does not repeat its name —
    which is also what lets the startup scan find the agent in the path it
    already has instead of parsing it back out of a filename.
    """
    return agent_dir(agent) / SESSION_NAME


def _read_session(path: Path) -> tuple[dict[str, Any], str]:
    """One login file as a mapping, and — when it could not be read — why.

    The path-shaped half, because the startup scan has the path already and must
    read the name out of the file rather than the directory it sits in.

    **A file that will not parse is named, not read as no file at all**
    (DESIGN.md §11.1).  The outcome stays a cache miss — a login can be had
    again by scanning, and refusing to start over one would trade the whole
    feature for it — but an empty mapping on its own is indistinguishable from
    a machine that never scanned, so the status answer built from it told
    somebody with a damaged file to scan a *first* code while the file naming
    their account sat on disk.  The damage rides back beside the mapping, and
    `ready` puts it where the status answer reads it (`restore_note`): the file,
    and what went wrong with it.
    """
    try:
        raw = statefile.reader().load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, ""
    except Exception as e:  # noqa: BLE001 — a corrupt cache is a cache miss
        logger.warning("wechat session %s could not be read: %s; ignoring it", path, e)
        return {}, f"the saved login at {path} could not be read ({e})"
    return (raw if isinstance(raw, dict) else {}), ""


def save_session(agent: str, session: Mapping[str, Any]) -> Path:
    """Write a login down, atomically.

    The atomicity is `slife2.libs.host.statefile.write`'s, and it matters here for
    the reason it gives: the poll loop rewrites this file every time the cursor
    moves, so a reader is always a moment away from catching it.

    **The agent is stamped here**, beside the token, and the one writer is why
    every file this build writes can say whose login it is — see :data:`AGENT_KEY`
    for what the directory cannot carry.  Written rather than left to each caller
    because there are two callers already and a third would be the one to forget.
    """
    path = session_path(agent)
    statefile.write(path, {**session, AGENT_KEY: agent})
    return path


def clear_session(agent: str) -> bool:
    """Forget a login — the ack cursor goes with it, as it is only meaningful
    alongside a live session.

    ``True`` when no saved login is left afterwards, which is the state every
    caller is after: a login that was never there is already that state.  The
    old answer said ``False`` for it, which made "nothing to forget"
    indistinguishable from "the file would not go" — so `logout`'s answer, "the
    saved login is gone", stood over a file that was still on disk, and the next
    start adopted the account again.  A removal that fails is now a ``False``
    **and an error naming the file**, because the caller that reports the login
    gone has read the wrong answer otherwise.  Callers that only want the
    session tidied (`_adopt`, `login`, `status`) may ignore the return; the fact
    is left in the log for them.
    """
    path = session_path(agent)
    if not path.exists():
        return True
    try:
        path.unlink()
    except OSError as exc:
        logger.error(
            "wechat %s: the saved login at %s could not be removed: %s — the "
            "next start will adopt this account again",
            agent,
            path,
            exc,
        )
        return False
    return True


#: How this plugin hands a message to the agent: `(agent, prompt)`, and it is a
#: seam because a test drives the whole loop without an agent server anywhere.
Deliver = Callable[[str, str], Awaitable[None]]

#: How this plugin tells a window about its own link: `(agent, line)`, drawing a
#: harness line rather than running a turn (`slife2.plugins.tui.server`'s `note`).  A
#: seam for `Deliver`'s reason — the whole point of these tests is that no window
#: and no server is anywhere — and it is a second seam rather than a use of the
#: first because the two are different things: a message the person reads as
#: their own, and a line the harness has to say.
#:
#: **What the call answers is not read.**  A line is said once, where a window
#: is.  An agent's window is what runs it — a terminal or `--headless`, both of
#: which attach — so "no window" is the seconds around that agent's own start
#: and not a state, and a line the gap swallows is not worth a second attempt
#: (`handover`'s own rule for a note: the moment it describes has passed).
Note = Callable[[str, str], Awaitable[None]]


#: The two states a login is in, as a window is told them.  Derived from the
#: client rather than stored (`Wechat._state`), so there is one answer to "is the
#: link up" and a tool call cannot leave a stale one behind.
LINK_ONLINE = "online"
LINK_OFFLINE = "offline"


def format_link_line(state: str) -> str:
    """The one line a link transition is worth, in the harness's vocabulary.

    **`Wechat` is the name here**, where a peer's line carries the peer's own —
    a channel coming up is the same news as an agent coming up, so
    `slife2.tui.notes` spells both and this cannot drift from `⚡ jill online`.
    The capital is the bubble's (`slife2.tui.widgets.channel_prefix`), not the
    config key's: `Wechat> ` is what this channel is called on screen, and a
    line about the same channel should call it the same thing.

    The state is one of :data:`LINK_ONLINE` / :data:`LINK_OFFLINE` and nothing
    finer.  A window is not where "waiting for a scan", "no login saved" and
    "connected but not answering" are told apart: those are three different
    pieces of advice, and `wechat_check_status` is where a model asks for them.
    """
    if state == LINK_ONLINE:
        return notes.online("Wechat")
    return notes.offline("Wechat")


def _home_key(root: str | Path) -> str:
    """One home's canonical spelling — the first half of a session's key.

    **A key is an identity, so it is the directory and not the string somebody
    wrote it with.**  `Wechat.session` is asked for a login by two callers who
    spell one home differently.  A tool call carries the client's own
    `data_dir`, which is a spelling and not a directory: `client_meta` sends the
    home "exactly as written", and `_for_the_wire` leaves an absolute path
    alone on purpose (a symlinked home is "a spelling the operator chose"), so
    what arrives in the ambient home (`ServeTheCallersHome` → `in_home`) is the
    client's string.  `_resume_login` keys by the directory `known_homes`
    answered with, which is `Path.resolve()`d.  Both read the *same file* —
    `agent_dir` resolves the two to one directory — so the mismatch is invisible
    everywhere except in the key, and what it bought was a second `Session` for
    one login: a second `ready()` → `_adopt` → `try_restore_session` on the same
    token and then a second `watch()`, so two poll loops, each with its own
    cursor and its own `seen` window, handed every inbound message to the
    conversation twice.  Any home whose spelling is not canonical splits them —
    `--data-dir d:\\DEV\\…` under a directory whose real name is `D:\\Dev\\…`, or
    a symlinked or junctioned one — which is the class of name `AGENT_KEY`
    exists for, one level up.

    Resolved, because that is the spelling one of the two ends already answers
    with and only one of them can move.
    """
    return str(Path(root).resolve())


class Wechat:
    """Every agent's login, and the loop that watches each one."""

    def __init__(
        self,
        config: Config,
        *,
        http: Http = ilink.request,
        deliver: Deliver | None = None,
        note: Note | None = None,
        directory: Path | None = None,
    ) -> None:
        self._config = config
        self._http = http
        self._deliver = deliver or self._deliver_to_window
        self._note = note or self._note_window
        #: The homes whose logins this process watches.  `None` means every
        #: home on the machine (`slife2.libs.host.paths.known_homes`), which is the
        #: real answer: this is one process serving every agent, so it resumes
        #: the logins of every home rather than of whichever directory it was
        #: started in.  A caller that hands in a directory — a test — gets that
        #: directory's home and nothing else.
        self._homes: list[Path] | None = (
            None if directory is None else [Path(directory).parent]
        )
        self._sessions: dict[tuple[str, str], Session] = {}

    def session(self, agent: str, home: str = "") -> Session:
        """One agent's session, started the first time it is asked for.

        **Created empty, and restored by :meth:`ready`** — a tool call must not
        be the thing that waits for a network round trip to a bridge that may
        be down, so what a fresh session is given here is its own state and
        nothing else.

        **The home is one key however it is spelled** (`_home_key`): a call
        arrives with the client's own `data_dir` string and `_resume_login`
        arrives with a resolved directory, and the same home spelled twice was
        two sessions — and so two poll loops — on one login.
        """
        where = _home_key(home or home_dir())
        found = self._sessions.get((where, agent))
        if found is None:
            found = Session(agent, http=self._http, home=where)
            self._sessions[(where, agent)] = found
        return found

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Resume every login this machine has, in the background.

        **The session files are the set of agents that have a login** — a shared
        process cannot be told which agents exist by anybody, and the files are
        already the record of who scanned a QR here.  v1 got the same effect
        from its harness calling `wechat_check_status` once at startup; the
        files are the better answer, because they survive the harness.

        **Whose login a file is comes from the file, not the directory.**  The
        directory holds the *sanitised* agent name (`a/b` lands in `a_b/`), so a
        session keyed by it would be a second key for one login: a tool call from
        `a/b` — which is the name every other entry point uses — would create a
        session under the raw name, adopt the same saved token, and start a
        *second* poll loop on it, delivering every message twice, one copy to a
        window that is not showing.  The name is written into the file beside the
        token (`AGENT_KEY`, :func:`save_session`) and read back here, exactly as
        `slife2-schedule` reads its ledger's owner out of the ledger.

        A file too old to carry one — v1's, or one written before this key — is
        **skipped and named in the log**, which is what `slife2-schedule` and
        `slife2-a2a` do with a ledger whose owner is not written down.  The
        fallback that used to be here (the directory name) looked right for the
        names where the two are equal — every name without a reserved character
        — and was the second-session bug for the ones where they are not, which
        are the only names the key exists for.

        Never awaited in the path that serves: a bridge that cannot be reached
        must not stop the plugin from coming up, and every one of these is
        re-triggerable by any tool call.
        """
        for root in self._homes or known_homes():
            for path in sorted((root / "agents").glob(f"*/{SESSION_NAME}")):
                self._resume_login(path, root)

    def _resume_login(self, path: Path, root: Path) -> None:
        """One login file, resumed in the home it was found in.

        **The home travels with the session rather than being looked up when it
        is needed.**  The poll loop this starts outlives the call that started
        it, so a path resolved inside it would be answered by whatever directory
        the *daemon* is in — and the cursor of one home's login would be written
        into another's file.  So the directory the file was found in is kept
        (`Session.home`) and re-entered around the work
        (`slife2.libs.host.paths.in_home`).
        """
        saved, _ = _read_session(path)
        # **The raw name, or nothing.**  The directory is a *sanitised*
        # name (`a/b` lives in `a_b/`), so resuming under it is a second key
        # for one login: a tool call from the raw name then builds a second
        # `Session`, adopts the same token, and starts a second poll loop —
        # every message delivered twice, which is the bug this key exists to
        # prevent.  The fallback that used to be here was "exactly right
        # whenever the raw name and the sanitised one are the same, which is
        # every name without a reserved character in it" — and it was wrong
        # for exactly the names it mattered for, silently.  A login file
        # from before this key existed is *skipped* and named, like the
        # ownerless ledgers `slife2-schedule` and `slife2-a2a` refuse
        # (`ScheduleStore.adopt`, `InboundStore`): the file is not lost, and
        # nobody is told a session is being watched that is not.
        agent = str(saved.get(AGENT_KEY) or "")
        if not agent:
            logger.warning(
                "wechat %s: no agent name in it, so this login is not "
                "resumed; sign in again to rewrite it",
                path,
            )
            return
        self._spawn_in_home(root, self._resume(self.session(agent, _home_key(root))))

    async def close(self) -> None:
        """Take down every loop, keep-alive and in-flight turn."""
        cancelled: list[asyncio.Task[Any]] = []
        for session in self._sessions.values():
            cancelled.extend(
                task
                for task in (
                    session.poll,
                    *session.typing.values(),
                    session.qr_task,
                    *session.inflight,
                )
                if task is not None
            )
            session.poll = None
            session.typing.clear()
            session.qr_task = None
            session.inflight.clear()
        for task in cancelled:
            self._cancel(task)
        # **Await them, rather than only cancelling them.**  A cancelled task
        # the loop never runs again ends with its coroutine un-awaited, and the
        # garbage collector reports one at shutdown — the `Wechat._poll`
        # warning a TUI test used to collect.  `return_exceptions` keeps one
        # task's failure from hiding the others.
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

    @staticmethod
    def _cancel(task: asyncio.Task[Any] | None) -> None:
        if task is not None and not task.done():
            task.cancel()

    def _spawn_in_home(
        self, root: Path, work: Coroutine[Any, Any, None]
    ) -> asyncio.Task[None]:
        """Spawn *work* with *root* as the home around it.

        **A loop outlives the call that started it**, so by the time it resolves
        a path the ambient home is the daemon's own — and a daemon serving two
        homes would write one home's cursor into the other's login file.  Every
        task that is started from outside a call and touches a login goes through
        here, which is what `Session.home` is kept for.
        """

        async def run() -> None:
            with in_home(root):
                await work

        return self._spawn(run())

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
        saved, damage = _read_session(session_path(session.agent))
        if damage:
            # **The file is named, not read as no login at all** (DESIGN.md
            # DESIGN.md §11.1).  The status answer repeats `restore_note` verbatim, so a
            # damaged cache now says which file it is and what was wrong with it
            # rather than telling somebody who already scanned to scan again.
            session.restore_note = damage
        if saved.get("bot_token"):
            await self._adopt(session, saved)

    async def _resume(self, session: Session) -> None:
        """The startup path: restore if there is anything to restore.

        **Nothing is said to the window from here.**  A login picked up at
        start-up is not a change to the link, and this process comes up before
        the terminal does — the line belongs to what happens while a window is
        there (`_announce`, and `_adopt` for why the state is marked known
        rather than announced).

        **And nothing is said by *raising* either.**  This runs from a spawned
        task whose only reporter is `_spawn`'s callback, and the three outcomes
        `_adopt` tells apart each write a `restore_note` of their own; a fourth
        failure — a bug in `ready`, a file that cannot be read — used to be
        swallowed by `contextlib.suppress(Exception)` with no line anywhere at
        all.  It is logged now: silent about a login is the one thing this must
        not be, and a note is not a reason to say the file is fine either.
        """
        try:
            await self.ready(session)
        except Exception as exc:  # noqa: BLE001 — a boot that failed is not a boot that stopped
            logger.warning(
                "wechat %s: the saved login was not resumed: %s", session.agent, exc
            )

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
        if not await client.try_restore_session(
            saved, max_age=_T.timeouts.pacing.wechat_session_max_age
        ):
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
                #
                # **The bridge's own words go into the note**, read before the
                # rejection clears them: `session timeout` is the answer to a
                # login another agent of the same WeChat account has taken over,
                # and it is the difference between "scan again because somebody
                # else did" and "scan again, unexplained".
                reason = client.last_error or "no reason given"
                client.reject_session()
                clear_session(session.agent)
                session.restore_note = (
                    f"the server rejected the saved login ({reason}), so it has "
                    f"been forgotten"
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
        # **Known, and not worth saying.**  A saved login coming back is this
        # *process* starting, not the link changing, and the poll loop it is
        # about to start would otherwise announce it in every window of every
        # agent that has one (`_announce`).  Marking it here is what makes the
        # difference between "it came up while you were watching" and "it was
        # already up when you looked" — the second is not news.
        session.announced = LINK_ONLINE
        self.watch(session)
        logger.info("wechat %s: login resumed", session.agent)
        return True

    # ── Polling ──────────────────────────────────────────────────────────

    async def _note_window(self, agent: str, line: str) -> None:
        """Draw *line* in the window showing *agent*, and nowhere else.

        The default behind the `note` seam, and the exact call the A2A plugin
        makes for a peer's arrival: the window, and nowhere else — a line about a
        moment has nowhere else to go, and running a turn for one would spend a
        model call telling somebody a fact they cannot act on
        (`slife2.libs.harness.handover.note_to_window`).

        What that call answers — written, refused, unknown — is not read here: a
        line no window took is a line nobody needs, and it is not said again
        (`Note`).
        """
        await note_to_window(self._config, agent, line)

    @staticmethod
    def _state(session: Session) -> str:
        """This session's link, as one of the two words — **the client's**.

        Derived rather than stored, so there is one answer to "is the link up"
        and a tool call cannot leave a stale one behind.
        """
        client = session.client
        return (
            LINK_ONLINE
            if client.is_logged_in and not client.auth_failed
            else LINK_OFFLINE
        )

    async def _announce(self, session: Session) -> None:
        """Tell the window showing this agent what its link state is.

        **Only a change, and never a change into *offline* from nothing.**  Both
        halves matter: a link that is up produces one line however many polls
        pass under it, and an agent that never scanned a code does not open a
        window with a complaint about a channel nothing has happened to.  That is
        the same rule the mesh states about a cold card
        (`slife2.plugins.a2a.mesh._handle_discovery`), and for the same reason: a line
        about an absence is a line about an event that did not happen.

        **And a login picked up at start-up is not an event either** — `_adopt`
        marks that state known, so what a window shows is what happened *while
        somebody was watching*: a scan landing, a token the far end refuses, an
        account expiring, a logout.  Without that, every start of the stack would
        put `⚡ Wechat online` in every window of every agent that has a saved
        login, which is a line about the process rather than about the link.

        **Said once, and not owed.**  An agent's window is what runs that agent —
        a terminal or `--headless`, and both attach — so there is no window only
        in the seconds around that agent's own start, and a line the gap swallows
        is not offered again (`Note`).  This used to hold the line *owed* and
        redial for it, which is machinery for a case the deployment does not
        have: the line is for somebody watching, and in that gap nobody was.

        Called from the poll loop and from `logout`, so the state is derived here
        rather than set at a call site — there is one answer to "is the link up",
        and it is the client's.
        """
        state = self._state(session)
        # **Claimed before the await.**  The poll loop and `logout` are the two
        # drivers of this, and a cancelled poll loop may already be inside a draw
        # of its own, so setting it here is what keeps one transition from being
        # said twice.
        was, session.announced = session.announced, state
        if state == was or (not was and state == LINK_OFFLINE):
            return
        try:
            await self._note(session.agent, format_link_line(state))
        except Exception as exc:  # noqa: BLE001 - a line is not worth a loop
            # A window that cannot be drawn in is not a reason to stop watching
            # messages, and the poll loop is where this is usually called from.
            logger.debug(
                "wechat %s: the link line was not drawn: %s", session.agent, exc
            )

    def watch(self, session: Session) -> None:
        """Start this session's poll loop, if it is not already running."""
        if session.poll is None or session.poll.done():
            session.poll = self._spawn_in_home(Path(session.home), self._poll(session))

    async def _poll(self, session: Session) -> None:
        """Watch one login for messages, until it stops being a login.

        The loop ends when the session does — logged out, or a token the far
        end refused — and never on a *transient* failure, which is what the
        backoff is for.  `poll_updates` records its own failures rather than
        raising, so what is left here is the cadence and the queue.
        """
        client = session.client
        backoff = _T.timeouts.pacing.wechat_poll
        logger.info(
            "wechat %s: polling every %.1fs",
            session.agent,
            _T.timeouts.pacing.wechat_poll,
        )
        while client.is_logged_in and not client.auth_failed:
            # **First, before the poll blocks on the bridge**: this is where a
            # link that has just come up says so, and `poll_updates` waits for
            # the far end to have something to say — which, on a quiet account,
            # is the whole of the wait.
            await self._announce(session)
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
                logger.warning("wechat %s: poll failed: %s", session.agent, exc)
            backoff = (
                min(backoff * BACKOFF_FACTOR, _T.timeouts.pacing.wechat_backoff_max)
                if client.last_error
                else _T.timeouts.pacing.wechat_poll
            )
            await asyncio.sleep(backoff)
        # The loop ended because the link did — a token the far end refused, or
        # an account that expired — and a link that dies on its own is the one
        # transition nobody asked about, so it is said here.  A *deliberate* end
        # (`logout`, which cancels this task) never reaches here and says so
        # itself.
        await self._announce(session)
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
        # the last `_T.timeouts.pacing.wechat_dedup` that arrives *alongside its own replay* is
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
        # `slife2.libs.turn.context.with_note` puts the one before a footnote.
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
            # **An error rather than a warning**, because this is the one outcome
            # a person can see and the plugin cannot make right: their message is
            # not answered, and nothing else will happen to it (`hand_over`'s
            # failure is the last word on a message).  The text is not logged: it
            # is a private message, and its length is the part that matters to
            # whoever is reading the log.
            logger.error(
                "wechat %s: a %d-character message from %s was not delivered to "
                "its conversation, so nothing will answer it: %s",
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

        **A cursor that did not land is not a cursor that moved, and it is said
        out loud.**  This was a `debug` line, which made the one consequence it
        has invisible: the file is the only thing that tells the next start
        where the stream had got to, so a write that failed leaves the bridge's
        whole replay window to be re-ingested and answered again as fresh turns
        — the same messages said twice, by a process that believed it had
        already read them.  The warning names the file, and `persisted` is left
        where it was so the next poll tries again.
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
            logger.warning(
                "wechat %s: the ack cursor could not be written to %s: %s — "
                "after a restart this stream is re-read from its old place, so "
                "those messages are answered again",
                session.agent,
                session_path(session.agent),
                exc,
            )
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
        deadline = time.monotonic() + _T.timeouts.pacing.typing_max_lifetime
        while time.monotonic() < deadline:
            try:
                await session.client.send_typing(peer, token, status=1)
            except Exception as exc:  # noqa: BLE001 — a courtesy, not a contract
                logger.debug("wechat %s: typing not shown: %s", session.agent, exc)
            await asyncio.sleep(_T.timeouts.pacing.typing_refresh)

    # ── Handing over, for real ───────────────────────────────────────────

    async def _deliver_to_window(self, agent: str, prompt: str) -> None:
        """Hand one message over to the window showing this agent's conversation.

        **The window and not the agent server**, and that is the whole of what
        makes a message sent from a phone visible in the terminal: the window
        submits it exactly as it submits the person's own typing, so the line,
        the answer and the record are the ones that window already draws
        (`slife2.tui.app`).  A message that arrives while the window is drawing
        a turn cuts into that turn, like any other source; the agent server's
        inbox is the one queue, and it orders the turns.

        **The agent is named, and that is what makes this work with more than one
        window open.**  It used to be implicit in the address, which is one per
        machine, so a second terminal could not be reached at all; now the plugin
        keeps a session per agent and this says which one (`slife2.plugins.tui.server`).

        **The retry and the classification are `slife2.libs.harness.handover.hand_over`'s** —
        one copy of "the window may be attaching right now" for every caller, and
        one copy of the rule that a call which did not come back may not be
        repeated.  What is left here is the naming: a message off a phone is
        handed over as `wechat`, which is the channel its turn is recorded under
        and the bubble it wears.

        **And a failure is a failure**: this used to fall back on the agent
        server, which is a turn nobody is watching — the one thing a channel
        whose whole point is the terminal must not do (`slife2.libs.harness.handover`).
        """
        await hand_over(
            self._config, agent, prompt, CONFIG_KEY, what="a WeChat message"
        )

    # ── The QR login ─────────────────────────────────────────────────────

    async def login(self, session: Session) -> dict[str, Any]:
        """Start a QR login, and answer the code to show and where it stands.

        The QR is fetched and then watched *in the background*: a scan is a
        person finding their phone, and a tool call that waited for one would
        hold a turn open for a minute.

        **A QR that could not be fetched is a failure and not a state**, which
        is what a caller getting nothing scannable would otherwise be handed: a
        login nothing is watching is not a login in progress, so the two are
        raised rather than answered with a `state` that reads as an ordinary
        wait.
        """
        # **Before the "already connected" question is asked**, because a login
        # that is saved but not yet resumed looks exactly like no login at all —
        # so without this a model that calls `wechat_login` first would be told
        # to scan a code it does not need.
        await self.ready(session)
        client = session.client
        if client.is_logged_in and not client.auth_failed:
            return {"state": "connected", "qr": "", "qr_url": "", "seconds_left": 0}
        if client.auth_failed:
            # A token the far end refused is not a session to build on; drop it
            # and let the scan replace it, rather than refusing the login.
            client.reject_session()
            clear_session(session.agent)

        session.qr_state = "waiting"
        session.qr_error = ""
        try:
            data = await client.fetch_qrcode(ilink.BASE_URL)
        except Exception as exc:  # noqa: BLE001 — a failure the model reads
            session.qr_state = ""
            logger.warning("wechat %s: QR fetch failed: %s", session.agent, exc)
            raise ToolError(
                f"the login QR could not be fetched ({exc}); "
                f"call `wechat_login` again in a moment."
            ) from exc
        qrcode = str(data.get("qrcode") or "")
        if not qrcode:
            session.qr_state = ""
            raise ToolError(
                "the server answered without a login QR; "
                "call `wechat_login` again in a moment."
            )
        content = str(data.get("qrcode_img_content") or qrcode)

        try:
            rendered = render_qr(content)
        except ImportError:
            # **A URL is not a code, so the answer says which one it is**
            # (DESIGN.md §11.1).  Without the rendering library there is nothing
            # scannable to put in a reply, and handing `qr` the URL would be a
            # false instruction — the person would be told to scan a link.  So
            # `qr` is empty and the URL rides in `qr_url`, where the tool's own
            # `Returns:` says it is what to send instead.
            rendered = ""
        session.qr = rendered
        session.qr_url = content
        self._cancel(session.qr_task)
        session.qr_task = self._spawn(
            self._qr_watch(session, qrcode, ilink.BASE_URL, attempts=0)
        )
        return {
            "state": "waiting",
            "qr": rendered,
            "qr_url": content,
            "seconds_left": int(_T.timeouts.ready.qr_deadline),
        }

    async def _qr_watch(
        self, session: Session, qrcode: str, base_url: str, *, attempts: int
    ) -> None:
        """Follow one QR, and leave the session in a state whatever happens.

        **Every exit from the loop below is a `qr_state`**, and the one that
        raised was the exception to it: the task died, `_spawn`'s callback put
        the traceback in the log, and `qr_state` stayed `waiting` for ever — so
        `wechat_check_status` went on telling the model that a login QR was
        waiting to be scanned, about a watch that no longer existed.  The guard
        is here and not around the call that raised it first
        (`poll_login_status`, on a body nothing could read) because the next one
        would be a different line.
        """
        try:
            await self._qr_follow(session, qrcode, base_url, attempts=attempts)
        except Exception as exc:  # noqa: BLE001 — a dead watch is a state, not a traceback
            session.qr_state = "error"
            session.qr_error = f"The login could not be followed: {exc}"
            logger.warning("wechat %s: the QR watch ended: %s", session.agent, exc)

    async def _qr_follow(
        self, session: Session, qrcode: str, base_url: str, *, attempts: int
    ) -> None:
        """Follow one QR until it is confirmed, replaced, or out of time."""
        deadline = time.monotonic() + _T.timeouts.ready.qr_deadline
        while time.monotonic() < deadline:
            await asyncio.sleep(_T.timeouts.pacing.qr_poll)
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
                content = str(refreshed.get("qrcode_img_content") or fresh)
                try:
                    session.qr = render_qr(content)
                except ImportError:
                    # The URL is still the code's address; `qr_url` carries it.
                    session.qr = ""
                session.qr_url = content
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

    #: The agent a call is on behalf of, or `""` if the call does not say.  The
    #: identity is the caller's, never this process's — one wechat plugin serves
    #: every agent, so "whose login" is a fact only the caller has.
    #:
    #: **The agent and not the conversation**, so a worker shares its agent's
    #: WeChat: a login belongs to the account that scanned the code, and a
    #: subagent is a means to an end inside one of that agent's turns rather than
    #: a second phone.  A worker asking about WeChat is asking about its parent's,
    #: which is the only answer that could be true.
    #:
    #: `staticmethod` because the reading is `slife2.libs.plugin.protocol`'s and
    #: not this class's — assigning the function bare would make it a *method*,
    #: and `plugin.who(ctx)` would hand it this instance as the call.
    who = staticmethod(agent_or_blank)

    def unaddressed(self) -> str:
        """The sentence a tool is *refused* with, for a call that arrived with
        no conversation on it.

        A sentence and not a raise, because what belongs to this class is which
        words the refusal uses; raising it is the tool's, and it is the tool
        that knows a refusal is a `ToolError` rather than an answer.
        """
        return unaddressed("WeChat login to act on", "the login")

    async def status(self, session: Session) -> dict[str, Any]:
        """`wechat_check_status`: the link, why it is not working when it is
        not, and who a reply goes to.

        Restores first, so a saved login is live by the time the answer is
        written — see :meth:`ready`.  Then the states a session can be in, told
        apart by *values* rather than by shape: a QR nobody has scanned, no
        login at all, a link that is up but not answering, and a session that
        works all answer the same keys (`_facts`), so a model that has read one
        answer has read the schema for the next.
        """
        await self.ready(session)
        client = session.client

        # A token the far end refused — most often discovered by a *send*, which
        # is the one call that finds out without a poll.  It is dropped here for
        # the reason `_adopt` drops it: a session that cannot work must not be
        # reported as one that does.
        if client.auth_failed:
            # The bridge's words, for `_adopt`'s reason: a login taken over by
            # another agent of the same account answers `session timeout`, and
            # that is what tells the model which of the two things to say to
            # whoever is reading.
            reason = client.last_error or "no reason given"
            client.reject_session()
            clear_session(session.agent)
            session.restore_note = (
                f"the server rejected the login ({reason}), so it has been forgotten"
            )

        if session.qr_state and not client.is_logged_in:
            return self._facts(
                session, qr_state=session.qr_state, error=session.qr_error
            )

        if not client.is_logged_in:
            # **`error` is the reason and not the state.**  A `restore_note` that
            # is empty is a login this machine has never had rather than one
            # that failed, which is the difference between "scan a first code"
            # and "scan again" — and it is the *note* that says which, because
            # that is the field a damaged file and a refused token both fill in.
            return self._facts(session, error=session.restore_note)

        left = max(
            0.0,
            _T.timeouts.pacing.wechat_session_max_age
            - (time.time() - session.saved_at),
        )
        peer, token, heard = self._peer(session)
        # `last_error` and not the state: up but not answering is not down — the
        # token is still good, so what a reader is told is the bridge's own words
        # and the advice is to wait rather than to re-scan.
        return self._facts(
            session,
            connected=True,
            hours_left=left / 3600,
            peer=peer,
            token=token,
            heard_from=heard,
            error=str(client.last_error or ""),
        )

    def _facts(
        self,
        session: Session,
        *,
        connected: bool = False,
        hours_left: float = 0.0,
        peer: str = "",
        token: str = "",
        heard_from: bool = False,
        qr_state: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        """One status answer, whatever state it is answering about.

        **Every key, always**, because the three states are three readings of
        one session rather than three tools: a model that met a missing key
        would have to guess whether it was absent or empty, and the same
        question answered twice would be two shapes to keep in step.  What
        differs between them is which values are filled in — and the docstring
        the model reads is where each one is told what its empty value means.
        """
        return {
            "connected": connected,
            # The loop, and not the token: a link that is up with nothing polling
            # it is a link no message will arrive on, and that is the one thing
            # about "connected" a model cannot see from anywhere else.
            "polling": session.polling,
            "hours_left": round(hours_left, 1),
            "peer_wechat_id": peer,
            "context_token": token,
            # **Whether that peer wrote, or is this login's own account.**  Both
            # are a peer a message can reach and only the first is a reply, so an
            # answer that left it out would have a model believe it is answering
            # somebody who has said nothing.
            "heard_from": heard_from,
            # The QR login, in the session's own words: waiting, scanned,
            # confirmed, expired or error, and `""` when no login is in progress.
            "qr_state": qr_state,
            # **The code itself while it can still be scanned.**  A refreshed
            # code is the session's now, so a status answer can hand it over
            # instead of saying `waiting` about a code nobody holds.
            "qr": session.qr if qr_state in ("waiting", "scanned") else "",
            "qr_url": (session.qr_url if qr_state in ("waiting", "scanned") else ""),
            "error": error,
        }

    def _peer(self, session: Session) -> tuple[str, str, bool]:
        """Who a reply goes to — the peer, the conversation's token, and whether
        they wrote.

        The one thing a model cannot work out for itself: the peer and the token
        arrive with a message, and a model that has lost them — a new turn, a
        trimmed context — has no way to ask a person.  v1 seeded the login's own
        account as a contact for this reason: until somebody writes there is
        still one peer a message can reach, and it is whoever scanned the code.
        """
        contact = session.client.last_contact
        return (
            str(contact.get("from_id") or ""),
            str(contact.get("context_token") or ""),
            session.heard_from,
        )

    async def send(
        self, session: Session, peer: str, token: str, text: str
    ) -> dict[str, Any]:
        """`wechat_send_message`: one text, to one conversation.

        **Every way this can fail raises**, which is the whole of what makes the
        answer trustworthy: the envelope below is a `200` that says no, and a
        caller that had to read "sent" out of a returned sentence would be
        reading a reply the bridge had thrown away as one that landed.
        """
        await self.ready(session)
        client = session.client
        if not client.is_logged_in:
            raise ToolError("WeChat is not connected — call `wechat_login` first.")
        if not peer.strip() or not text.strip():
            raise ToolError(
                "both `peer_wechat_id` and `text` are needed, and neither may be empty."
            )

        # The reply is going out, so the indicator comes down now rather than
        # when the turn ends — the model may have more work to do, and a person
        # who has been answered should stop being told otherwise.
        self._typing_off(session, peer)
        try:
            result = await client.send_message(peer, token, text)
        except Exception as exc:  # noqa: BLE001 — a failure the model reads
            logger.warning("wechat %s: send to %s failed: %s", session.agent, peer, exc)
            raise ToolError(f"the message to {peer} could not be sent: {exc}") from exc

        # A rejected token comes back as a 200 carrying an error envelope, and
        # a send that reads that as success is a reply the model believes it
        # delivered.  v1 lost this half and found it.
        if ilink.error_envelope(result):
            client.auth_failed = True
            client.last_error = ilink.envelope_error(result) or "send rejected"
            raise ToolError(
                f"the server rejected the send: {client.last_error} — the login "
                f"is marked as no longer working, and `wechat_login` is usually "
                f"the fix."
            )
        with contextlib.suppress(Exception):
            await client.send_typing(peer, token, status=2)
        logger.debug(
            "wechat %s: sent %d characters to %s", session.agent, len(text), peer
        )
        return {"sent": True, "peer_wechat_id": peer, "context_token": token}

    async def logout(self, session: Session) -> dict[str, Any]:
        """`wechat_logout`: stop watching, forget the login, forget its QR.

        **The login is only gone if the file is.**  `clear_session` used to
        swallow the unlink failure and answer `False`, which every caller threw
        away — so this tool said "the saved login is gone" over a file that was
        still there, and the next start adopted the same account the logout was
        asked to end.  The teardown still happens (the loop stops, the client is
        replaced, the link is announced down), and what changes is the answer:
        `login_removed` is the file's own verdict and `login_file` names it, so
        a model can say the one thing that is true and a person can remove the
        file by hand.
        """
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
        forgotten = clear_session(session.agent)
        # The one departure nobody has to discover: the model asked for it.  Said
        # from here rather than from the loop's exit, because `logout` cancels
        # that loop where it stands (`_poll`).
        #
        # **Before the link is forgotten, and the order is the whole of it.**
        # Told afterwards, the session would read as one that never had a link,
        # and a logout somebody asked for would be silent (`_announce`).
        await self._announce(session)
        logger.info("wechat %s: logged out", session.agent)
        # **The path is answered either way.**  It is what a person removes by
        # hand when the unlink failed, and it is also where a login would be
        # written next time — a fact about the session rather than about this
        # call's outcome, which is what `login_removed` is for.
        return {
            "logged_out": True,
            "login_removed": forgotten,
            "login_file": str(session_path(session.agent)),
        }


def build_server(
    config: Config,
    *,
    http: Http = ilink.request,
    deliver: Deliver | None = None,
    note: Note | None = None,
) -> FastMCP:
    """The server slife2 starts, wired to one :class:`Wechat`.

    `http`, `deliver` and `note` are the three seams, and they are the three
    things that reach outside this process: the bridge, the agent server, and the
    window showing this conversation.  All have a real default and all are
    replaceable, so a test drives the whole login, poll, delivery, reply and
    link-announcement path with neither a socket, an agent nor a terminal.
    """
    plugin = Wechat(config, http=http, deliver=deliver, note=note)

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
    async def wechat_login(ctx: Context) -> dict[str, Any]:
        """Get a QR code that logs this agent into WeChat.

        Put `qr` in your reply for somebody to scan with the WeChat app: the
        account that scans is the account this conversation speaks as, and other
        people reach this conversation by messaging it. The code expires in
        about ten minutes, and the login lasts about a day.

        Returns:
            `state` — `waiting` for a code to be scanned, or `connected` when
            there was already a login, in which case call `wechat_logout` first
            to switch accounts — `qr`, the code to put in your reply, `qr_url`,
            what the code encodes (send it as a link instead when `qr` is empty,
            which is a machine that cannot draw one), and `seconds_left`, how
            long the code lasts.
        """
        agent = plugin.who(ctx)
        if not agent:
            raise ToolError(plugin.unaddressed())
        return await plugin.login(plugin.session(agent))

    @mcp.tool(name="wechat_check_status", meta=FOR_THE_MODEL)
    async def wechat_check_status(ctx: Context) -> dict[str, Any]:
        """How the WeChat link is, and who a reply goes to.

        The one place that says where a reply goes: the peer and the
        `context_token` their conversation needs.

        Returns:
            `connected` — whether a message can go out — `polling`, whether
            messages are being watched for, and `hours_left` of the login's day.
            `qr_state` is a login in progress: `waiting` for the code to be
            scanned, `scanned` for one to be confirmed on the phone, `confirmed`
            or `expired` or `error`, and then call `wechat_login` again. `qr` is
            that code drawn, and `qr_url` what it encodes — both empty unless a
            login is waiting or scanned; a code refreshed after an expiry is
            here rather than in the original login answer. `error`
            says what is wrong whenever anything is — empty means nothing is —
            and `peer_wechat_id` and `context_token` are who a reply goes to, and
            `heard_from` is whether they wrote to you — false means nobody has,
            and that peer is the account this login belongs to.
        """
        agent = plugin.who(ctx)
        if not agent:
            raise ToolError(plugin.unaddressed())
        return await plugin.status(plugin.session(agent))

    @mcp.tool(name="wechat_send_message", meta=FOR_THE_MODEL)
    async def wechat_send_message(
        ctx: Context,
        peer_wechat_id: str,
        text: str,
        context_token: str = "",
    ) -> dict[str, Any]:
        """Send a WeChat message to somebody who has written to you.

        An incoming message carries its peer and context token in a
        `[Wechat:{...}]` prefix: pass those back here and the reply lands in the
        conversation it belongs to. A turn that sends nothing reaches nobody.

        Args:
            peer_wechat_id: The id to send to, from the message's marker or
                `wechat_check_status`.
            text: The message body.
            context_token: The conversation's token, from the same place. Empty
                starts a new thread, which a first message wants.

        Returns:
            `sent` — true, because a send that did not land raises — with the
            `peer_wechat_id` and `context_token` it went to.
        """
        agent = plugin.who(ctx)
        if not agent:
            raise ToolError(plugin.unaddressed())
        return await plugin.send(
            plugin.session(agent), peer_wechat_id, context_token, text
        )

    @mcp.tool(name="wechat_logout", meta=FOR_THE_MODEL)
    async def wechat_logout(ctx: Context) -> dict[str, Any]:
        """Disconnect WeChat and forget the saved login.

        The account stays logged in on the phone; what goes is this
        conversation's token and its place in the message stream. Messages
        arrive again after a new scan, which is how accounts are switched.

        Returns:
            `logged_out` — true — `login_removed`, whether the saved login file
            is gone (the next start would adopt that account again if it is
            not), and `login_file`, the path it was at.
        """
        agent = plugin.who(ctx)
        if not agent:
            raise ToolError(plugin.unaddressed())
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

    Read off the data directories rather than out of a built `Wechat`, because
    `serve_plugin` asks for this before it builds the server — which is the
    point of it being text about a *start* rather than about the process — and
    every home's, because this process serves every home on the machine and
    the line is a count of what it is about to watch.
    """
    count = sum(
        len(list((root / "agents").glob(f"*/{SESSION_NAME}"))) for root in known_homes()
    )
    return f"{count} saved login(s)" if count else "no logins saved"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
