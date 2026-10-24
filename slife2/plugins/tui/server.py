"""The window, as somewhere another process can put a message.

**Why this exists at all.**  A message that arrives on another channel — WeChat,
another agent — has to become a turn of this conversation *and* be visible in the
terminal showing it.  The conversation is reachable from anywhere (`slife2.plugins.agent`
addresses it by name), but the *sight* of it is a client's: a turn's events reach
the client that asked for that turn, so a message delivered by a plugin runs and
is answered while the window that is watching shows nothing.

**Two things reach a window, and they are two kinds of hand-over.**  The first is
a *message*: it goes *to the window* and becomes the one thing a window already
knows how to handle — something the person typed.  It is submitted to the
conversation the moment the window takes it, exactly as the person's own typing
is (`slife2.tui.app`), and it is drawn either as its own turn or — when it
arrives while a turn is running — as a line inside that turn.  The second is a
*note*: a line the harness has to say and nobody has to answer — a peer came
online, a turn was cancelled — so it is drawn where every such line is drawn
(`ChatView.add_note`, `slife2.tui.notes`) and the conversation never learns of
it.  Same stream, same acknowledgement, one branch at the far end.

**This is a plugin, and the window is a client of it.**  It used to be the one
server here that was not: the window served it in-process, on a `tui:` section
of its own, and `slife2-wechat` dialled that address.  That made the address one
per *machine*, so a call could not say which window it meant — with two windows
open, the second one silently failed to bind and could never be reached.  Now it
is started and shared by the launcher like every other plugin (`slife2.libs.host.launcher`,
`Config.plugins`), and what it holds is **a session per agent**: the agent is the
key, exactly as a WeChat login is keyed by agent (`slife2.plugins.wechat.server`), so two
windows are two sessions and each is addressed by name.

**A session is an open call, and that is what keeps the message off a queue.**  A
window calls `attach` when it mounts and holds it for its life; the plugin pushes
each handed-over message down that call as a progress notification, and the
window's handler hands it to `deliver` — the same method its own typing goes
through.  There is deliberately **no inbox to poll**: v1 polled the plugin
(`__wechat_drain_incoming`) and DESIGN.md §6.2 records why that seam went, since
a message that becomes a turn in the window is worth more than one that becomes
a row somewhere else.  Holding the call open is also what makes a session
self-cleaning — a window that dies takes its stream with it, which cancels the
handler behind it, which is the detach.

**Three tools, and none of them is the model's.**  `submit` is a channel's,
`note` is the harness's, `attach` is a window's; all three are addressed by an
agent name.  Nothing here carries `slife2.libs.plugin.audience.FOR_THE_MODEL`, so the hub
leaves all of it out of the tool set, and the plugin serves no `list_sources` —
it declares no rows, and is therefore asked for none (`slife2.libs.plugin.protocol`).

**Returning is the acknowledgement, and it is the write.**  A caller whose
`submit` came back knows the words are on their way down a stream a window is
holding, which is what makes handing them over safe to retry: a connection that
dropped after the words were written cannot make them run twice.  That is one
notch weaker than "the window has queued it" and it is stated rather than
assumed — a window that is gone has no stream, so the write fails and `submit`
fails with it, and a hand-over whose caller gave up before the write is
withdrawn rather than delivered late.  What is *not* done is a window taking a
message and then acknowledging it: that variant buys a truer sentence at the
cost of a new way to run the same words twice, since an acknowledgement that
lands after a `submit` gave up leaves a message that still runs.  Duplicating a
turn is the failure the acknowledgement exists to prevent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.libs.host.config import TUI_SERVER_NAME, Config
from slife2.libs.plugin.audience import client_key
from slife2.libs.plugin.protocol import house_server, serve_plugin

logger = logging.getLogger(__name__)

SERVER_NAME = TUI_SERVER_NAME
#: The `servers:` key this plugin's address is filed under, like every other one
#: that owns no section: an address is the whole of what is written down about it.
CONFIG_KEY = "tui"

#: The tool names, spelled once because each crosses a process boundary: the
#: window's client calls `attach` (`slife2.tui.client`), and a channel calls
#: `submit` — or `note`, when it has a line for the person rather than a message
#: for the conversation.
ATTACH = "attach"
SUBMIT = "submit"
NOTE = "note"

#: What a hand-over asks the window to do with the words, and the one branch
#: between them: `TURN` is run as a turn of the conversation, `NOTE` is drawn as
#: a harness line and nothing else.  `NOTE` is its own tool name because the two
#: coincide; `TURN` is not `SUBMIT` because what the window does with a submitted
#: message is *run it*, which is the fact the window branches on.
TURN = "turn"
KINDS: tuple[str, ...] = (TURN, NOTE)

#: What `submit` says when there is nothing to hand a message to.  A constant
#: because a *caller* has to be able to recognise it: see `refused`.
NO_WINDOW = "no window is showing"


def refused(error: Exception) -> bool:
    """Whether a `submit` failed because no window is showing that agent.

    **The one failure a caller may safely repeat**, and the reason this is a
    function rather than a `ToolError` check at the call site.  Everything a tool
    handler raises arrives at the far end as a `ToolError` (`serve` pins
    `mask_error_details=False`, which is what keeps the text) — so the *type*
    cannot tell a refusal from a stream that dropped, and a caller that read it
    as one would hand over words that had already been written.

    What makes this one decidable is that `submit` looks the session up **before
    it posts anything**: nothing was written, so trying somewhere else cannot
    duplicate anything.  The module that writes the message is the one that
    answers what it looks like — the same arrangement `MARKER_PREFIX` makes for a
    marker, and `tests/test_agent_task.py` holds the pair together.
    """
    return isinstance(error, ToolError) and NO_WINDOW in str(error)


INSTRUCTIONS = (
    "The terminal showing an agent's conversation. `attach` is how a window "
    "announces itself and is handed messages; `submit` hands it one, from a "
    "channel that is not the person at that terminal."
)


@dataclass(frozen=True)
class Handover:
    """One hand-over on its way to a window: the words, and where they came in.

    `ticket` exists so that a caller and the push that carries it can agree on
    *which* hand-over is being acknowledged — a number rather than the words,
    because the same words twice is an ordinary thing for somebody to send.

    `kind` is what the window does with it, and there are two: a **turn** is put
    in the queue the person's own typing goes into and run as one (`submit`), and
    a **note** is drawn as a line and nothing else (`note`).  A note needs no
    channel — it is not on any channel, it is the harness talking — and carries
    `""` in its place so that one dataclass and one wire shape serve both.
    """

    ticket: int
    text: str
    channel: str
    kind: str = TURN


def encode(handover: Handover) -> str:
    """Encode a hand-over for an MCP progress notification's `message` field.

    Same convention as a turn's events (`slife2.libs.turn.events.encode`), and the same
    reasons: the field is a plain string, so the payload crosses as compact JSON,
    and `ensure_ascii=True` keeps it representable on a console whose codepage is
    not UTF-8.  The encode and the decode live beside each other so one
    round-trip test covers the contract.

    It is deliberately **not** one of `slife2.libs.turn.events`' variants: those are the
    events a *turn* produces, and this is a control message that causes one — or
    a line drawn without one.
    """
    return json.dumps(
        {
            "t": "handover",
            "id": handover.ticket,
            "d": handover.text,
            "c": handover.channel,
            "k": handover.kind,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def decode(message: str) -> Handover | None:
    """Decode a progress notification's `message`, or None if it is not ours.

    None rather than an exception for `slife2.libs.turn.events.decode`'s reason: a
    notification this build has no word for is a line not read, and the caller
    is a display that should stay up rather than a turn that should die.

    **And a `k` that is not one of `KINDS` is such a line.**  An absent or
    unknown kind used to read as `TURN`, argued from the older-plugin direction
    — a hand-over before there were two kinds was always a turn.  But the
    direction that costs something is the other one: a *newer* plugin's third
    kind would be executed as a conversation turn rather than drawn as a line,
    and a line run as a turn is a model call nobody asked for.  So the safer
    branch is the refusal (DESIGN.md §11.1: a weaker path is not a substitute
    for a missing capability).
    """
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(payload, dict) or payload.get("t") != "handover":
        return None
    text = payload.get("d")
    channel = payload.get("c")
    if not isinstance(text, str) or not isinstance(channel, str):
        return None
    kind = payload.get("k")
    if kind not in KINDS:
        return None
    # **Checked like the two fields above it**, because this is the reader for
    # the stream a window holds open: `int("x")` and `int([1])` raise, and a
    # raise here ends the progress callback that delivers every hand-over for
    # that window — one malformed push from a peer on another build, or a torn
    # write, and the delivery path is gone for the life of the attachment.  A
    # `bool` is not a ticket either, and `True` used to read as ticket 1.
    ticket = payload.get("id")
    if not isinstance(ticket, int) or isinstance(ticket, bool):
        return None
    return Handover(
        ticket=ticket,
        text=text,
        channel=channel,
        kind=kind,
    )


class WindowSession:
    """One agent's session: the stream a window is holding, and what waits on it.

    Created when a window attaches (`Windows.attach`), and it lives exactly as
    long as that call does.  What it holds is a queue of hand-overs *plus* the
    caller parked on each one — the second half is what makes this a pipe rather
    than an inbox, since nothing here outlives the wait for a window to take it.

    No cap on the queue, and it needs none: every entry has a caller blocked on
    its delivery, so how many may be in flight is decided by how many callers
    there are (`slife2.plugins.wechat.server.MAX_INFLIGHT` bounds its own).
    """

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.handovers: asyncio.Queue[Handover] = asyncio.Queue()
        self.pending: dict[int, asyncio.Future[None]] = {}
        self._next_ticket = 0
        #: Whether the window is still holding the call.  Cleared by the push
        #: loop on its way out, so a `submit` that arrives during the gap is
        #: refused rather than queued behind a window that has gone.
        self.attached = True

    def post(
        self, text: str, channel: str, kind: str = TURN
    ) -> tuple[Handover, asyncio.Future[None]]:
        """Queue a hand-over, and answer with it and the caller that will wait."""
        self._next_ticket += 1
        handover = Handover(self._next_ticket, text, channel, kind)
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.pending[handover.ticket] = waiter
        self.handovers.put_nowait(handover)
        return handover, waiter

    def claim(self, ticket: int) -> asyncio.Future[None] | None:
        """Take a hand-over's caller, or None when that caller has given up.

        The pop *is* the claim, and it is synchronous on purpose: caller and push
        run in this one event loop, so whoever takes the ticket out of `pending`
        first has decided the hand-over's fate, and there is no window between
        the check and the write for the two to disagree in.
        """
        return self.pending.pop(ticket, None)

    def abandon(self, ticket: int) -> bool:
        """Give up on a hand-over.  True when this caller got there first.

        False means the push had already claimed it, so the words are being
        written — a caller that gave up in that instant is a caller that cannot
        be told either way, which is the ordinary asymmetry of cancelling a
        request that has already been served.
        """
        return self.pending.pop(ticket, None) is not None

    def settle(self, waiter: asyncio.Future[None], failure: str = "") -> None:
        """Answer the caller parked on one hand-over, once and only once."""
        if waiter.done():
            return
        if failure:
            waiter.set_exception(ToolError(failure))
        else:
            waiter.set_result(None)

    def fail_pending(self, reason: str) -> None:
        """Tell every caller still waiting that this window is not taking them."""
        for waiter in self.pending.values():
            self.settle(waiter, reason)
        self.pending.clear()


class Windows:
    """Every conversation's session, started the first time one is named.

    The shape is `slife2.plugins.wechat.server.Wechat`'s, deliberately: one process shared by
    every agent, and a `dict` keyed by the caller rather than a process per
    agent.

    **The key is `slife2.libs.plugin.audience.client_key`'s triple**, because the
    process is shared by every *home* as well: two homes may each be running an
    agent called `jack`, and a table keyed by the name alone would hand the
    second window the first one's stream — its hand-overs typed into somebody
    else's conversation.
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str, str], WindowSession] = {}

    def attach(self, agent: str) -> WindowSession:
        """A window announces itself, replacing whatever session was there.

        Replaced rather than refused, because the case it covers is a window that
        died without its stream closing — a hard kill — and the second attach is
        the moment the stale session is discovered.  Two *live* windows cannot
        collide here: an agent name is exclusive (`slife2.libs.host.launcher.claim_agent`).
        """
        key = client_key(agent)
        previous = self._sessions.get(key)
        if previous is not None:
            previous.attached = False
            previous.fail_pending(f"another window took over {agent}'s conversation")
        session = WindowSession(agent)
        self._sessions[key] = session
        return session

    def detach(self, agent: str, session: WindowSession) -> None:
        """Forget a session — but only if it is still the current one.

        The identity check is the point: a replaced session's push loop ends
        later than the replacement started, and a detach that removed by name
        would delete the window that is actually there.
        """
        session.attached = False
        key = client_key(agent)
        if self._sessions.get(key) is session:
            del self._sessions[key]

    def showing(self, agent: str) -> WindowSession | None:
        """The session a message for *agent* should go to, if a window holds one.

        A peer that is not there is this system's broken-system answer rather than
        a degraded one (`slife2.libs.plugin.protocol.open_server`), which for this plugin
        means the same thing it has always meant: nobody is showing that
        conversation, so there is nowhere for the words to be typed.
        """
        session = self._sessions.get(client_key(agent))
        return session if session is not None and session.attached else None


async def _push(ctx: Context, session: WindowSession) -> None:
    """Hand over what arrives, down the call the window is holding.

    **The loop lives in the attach handler's own task, and that is load-bearing.**
    `Context.report_progress` resolves its progress token from the request that
    is current *when it is called*, so a context stashed in server state and used
    from `submit`'s task would push into the submitter's own stream.  Reported
    from inside this handler, the token is this call's, which is the same thing
    `ProgressObserver` does for a turn (`slife2.plugins.agent.server`).
    """
    in_flight: tuple[Handover, asyncio.Future[None]] | None = None
    try:
        while True:
            handover = await session.handovers.get()
            waiter = session.claim(handover.ticket)
            if waiter is None:
                # Its caller gave up before we woke: nothing to write for, and
                # this is the whole of what "withdrawn" means.
                continue
            in_flight = (handover, waiter)
            try:
                await asyncio.wait_for(
                    ctx.report_progress(handover.ticket, None, encode(handover)),
                    _T.timeouts.transport.window,
                )
            except Exception as exc:  # noqa: BLE001 - the caller is told, not this loop
                session.settle(waiter, f"the window did not take the message: {exc}")
            else:
                session.settle(waiter)
            in_flight = None
    finally:
        # Reached by cancellation, which is what a closed stream does to the
        # handler behind it — so this *is* the detach, and it has to leave nobody
        # waiting: a claimed hand-over has no one left to write to, and every
        # caller still parked is told rather than left parked.
        if in_flight is not None:
            session.settle(
                in_flight[1], "the window went away before it took the message"
            )
        session.fail_pending(f"the window showing {session.agent} went away")


def build_server(_config: Config | None = None) -> FastMCP:
    """The window, as an MCP server: a session per agent, and no state of its own.

    The config is accepted and unread, because `slife2.libs.plugin.protocol.serve_plugin`
    hands every plugin the config it read and this one reads no section: the
    address it is reached at is the launcher's business rather than its own.
    """
    windows = Windows()
    mcp: FastMCP = house_server(SERVER_NAME, instructions=INSTRUCTIONS)

    async def _hand_over(agent: str, text: str, channel: str, kind: str) -> None:
        """Put one hand-over down the stream the window showing *agent* holds.

        The two tools differ in the kind and in nothing else, so they meet here —
        and the part that must not be written twice is the `finally`: a
        cancellation landing between the push answering and this task resuming
        leaves a failure on the future with nobody to read it, and `asyncio`
        reports that at collection time as a warning about a turn that failed for
        a reason the log line cannot name.
        """
        session = windows.showing(agent)
        if session is None:
            # **A refusal is a state, not a server error.**  FastMCP logs every
            # `ToolError` at its `log_level` before answering the caller, and
            # the default is ERROR — so a readiness probe that asks "is a window
            # there yet?" wrote an ERROR traceback-shaped line every two
            # seconds until one was, turning a normal cold start's log into
            # something that reads like a fault.  The caller is told exactly
            # what it was told before; only the server's own copy is demoted,
            # and the caller's INFO line is where this fact is recorded.
            raise ToolError(
                f"{NO_WINDOW} {agent}'s conversation; "
                f"start `slife2 --agent {agent}` and hand it over again",
                log_level=logging.DEBUG,
            )

        handover, waiter = session.post(text, channel, kind)
        try:
            await waiter
        finally:
            # A caller that gave up — a cancelled call, a dropped client — takes
            # its message back out with it, so that a retry is a retry and not a
            # second turn.  False means the push got there first and the words are
            # already written.
            if session.abandon(handover.ticket):
                logger.debug(
                    "handover %d for %s withdrawn: its caller gave up",
                    handover.ticket,
                    agent,
                )
            # **Collected even though the `await` above already raised it.**  See
            # the docstring.
            if waiter.done() and not waiter.cancelled():
                waiter.exception()

    @mcp.tool(name=ATTACH)
    async def attach(agent: str, ctx: Context) -> None:
        """Attach this window to *agent*'s session, and be handed messages on it.

        **Hold this call open for as long as the window is showing the
        conversation.** It does not return: a message for this agent arrives as a
        progress notification on this call, and the caller must have supplied a
        progress handler, since `report_progress` is a silent no-op without one
        and a window that sent none would be handed nothing while every `submit`
        still succeeded (`slife2.libs.turn.events`).

        Ending the call detaches — a window that quits, crashes or is killed takes
        its stream with it, and this server notices by the handler being
        cancelled. There is no `detach` to call.

        Args:
            agent: Whose conversation this window is showing. A second attach for
                one agent replaces the first, which is how a stale session from a
                killed window is cleared.
        """
        session = windows.attach(agent)
        try:
            await _push(ctx, session)
        finally:
            windows.detach(agent, session)

    @mcp.tool(name=SUBMIT)
    async def submit(agent: str, text: str, channel: str) -> None:
        """Hand an agent's window a message to treat as something the person typed.

        The window submits it to the conversation immediately — not behind a
        queue of its own — so a message that arrives while a turn is running is
        folded into that turn at its next iteration boundary, and its answer
        appears in the transcript as any other turn's does.

        **Returning is the acknowledgement**, and it is the *write*: when this
        returns, the words are on their way down the stream that window is
        holding, and this server will not hand them to anybody else. What the
        turn then did with them is in the conversation's record.

        Args:
            agent: Whose conversation this belongs to — the session the message
                goes to, and the reason a call can be addressed to one window
                rather than to whichever terminal happens to be running.
            text: The words, exactly as the model should receive them. A channel
                that annotates its messages puts its marker in this string, and
                the window neither reads nor alters it.
            channel: Where they came in from: the caller's own key, which becomes
                the turn's channel (`slife2.plugins.agent`) and the bubble the line wears.

        Raises:
            ToolError: If no window is showing that agent — which is the same
                thing this system says for any missing peer, arriving here as
                "nobody is looking at that conversation".
        """
        await _hand_over(agent, text, channel, TURN)

    @mcp.tool(name=NOTE)
    async def note(agent: str, text: str) -> None:
        """Hand an agent's window a line to draw — no turn, no model, no record.

        **A note is the harness talking, not a channel.**  It reaches the window
        as `add_note` does (`slife2.tui.notes`), in the same bracket every other
        harness line wears, and the conversation never learns of it: nothing is
        queued, nothing runs, and nothing is stored — so a peer coming online
        costs no model call and leaves no trace in the turn log, which is exactly
        what it is worth.

        It is acknowledged like a `submit` and refused like one, because it takes
        the same road: the window's own stream, in arrival order with the turns.
        What is *not* here is the fallback a message has — see
        `slife2.libs.harness.handover.note_to_window` — since a line for whoever is looking
        has nowhere else to go.

        Args:
            agent: Whose window is showing the conversation this belongs to.
            text: The words inside the brackets, e.g. `⚡ jill online`. The
                window adds the brackets; a caller that supplied them would be
                spelling the harness's format a second time.

        Raises:
            ToolError: If no window is showing that agent.
        """
        await _hand_over(agent, text, "", NOTE)

    return mcp


def main(argv: list[str] | None = None) -> int:
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        build=build_server,
        logger=logger,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
