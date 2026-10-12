"""The window, as somewhere another process can put a message.

**Why this exists at all.**  A message that arrives on another channel — WeChat,
another agent — has to become a turn of this conversation *and* be visible in the
terminal showing it.  The conversation is reachable from anywhere (`slife2.server`
addresses it by name), but the *sight* of it is a client's: a turn's events reach
the client that asked for that turn, so a message delivered by a plugin runs and
is answered while the window that is watching shows nothing.

So the message goes *to the window*, and becomes the one thing a window already
knows how to handle: something the person typed.  It lands in the same queue
(`slife2.tui.app`), is run as the same kind of turn, and wears the same bubble —
which is the whole of the design, and the reason there is no second display path
anywhere in this system.

**This is a plugin, and the window is a client of it.**  It used to be the one
server here that was not: the window served it in-process, on a `tui:` section
of its own, and `slife2-wechat` dialled that address.  That made the address one
per *machine*, so a call could not say which window it meant — with two windows
open, the second one silently failed to bind and could never be reached.  Now it
is started and shared by the launcher like every other plugin (`slife2.launcher`,
`Config.plugins`), and what it holds is **a session per agent**: the agent is the
key, exactly as a WeChat login is keyed by agent (`slife2.wechat`), so two
windows are two sessions and each is addressed by name.

**A session is an open call, and that is what keeps the message off a queue.**  A
window calls `attach` when it mounts and holds it for its life; the plugin pushes
each handed-over message down that call as a progress notification, and the
window's handler hands it to `deliver` — the same method its own typing goes
through.  There is deliberately **no inbox to poll**: v1 polled the plugin
(`__wechat_drain_incoming`) and DESIGN.md §1.2 records why that seam went, since
a message that becomes a turn in the window is worth more than one that becomes
a row somewhere else.  Holding the call open is also what makes a session
self-cleaning — a window that dies takes its stream with it, which cancels the
handler behind it, which is the detach.

**Two tools, and neither is the model's.**  `submit` is a channel's, `attach` is
a window's; both are addressed by an agent name.  Nothing here carries
`slife2.audience.FOR_THE_MODEL`, so the hub leaves all of it out of the tool set,
and the plugin serves no `list_sources` — it declares no rows, and is therefore
asked for none (`slife2.mcp_server`).

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

import slife2.timeouts as _T
from slife2.config import TUI_SERVER_NAME, Config
from slife2.mcp_server import house_server, serve_plugin

logger = logging.getLogger(__name__)

SERVER_NAME = TUI_SERVER_NAME
#: The `servers:` key this plugin's address is filed under, like every other one
#: that owns no section: an address is the whole of what is written down about it.
CONFIG_KEY = "tui"

#: The two tool names, spelled once because each crosses a process boundary: the
#: window's client calls `attach` (`slife2.tui.client`) and a channel calls
#: `submit` (`slife2.wechat`).
ATTACH = "attach"
SUBMIT = "submit"

#: How long one push may take before it is a broken window rather than a slow
#: one.  Generous for a loopback write, short enough to be a deadline: this
#: bounds the *write*, not the turn the window will run afterwards.

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
    """One message on its way to a window: the words, and where they came in.

    `ticket` exists so that a caller and the push that carries it can agree on
    *which* message is being acknowledged — a number rather than the words,
    because the same words twice is an ordinary thing for somebody to send.
    """

    ticket: int
    text: str
    channel: str


def encode(handover: Handover) -> str:
    """Encode a hand-over for an MCP progress notification's `message` field.

    Same convention as a turn's events (`slife2.events.encode`), and the same
    reasons: the field is a plain string, so the payload crosses as compact JSON,
    and `ensure_ascii=True` keeps it representable on a console whose codepage is
    not UTF-8.  The encode and the decode live beside each other so one
    round-trip test covers the contract.

    It is deliberately **not** one of `slife2.events`' variants: those are the
    events a *turn* produces, and this is a control message that causes one.
    """
    return json.dumps(
        {
            "t": "handover",
            "id": handover.ticket,
            "d": handover.text,
            "c": handover.channel,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def decode(message: str) -> Handover | None:
    """Decode a progress notification's `message`, or None if it is not ours.

    None rather than an exception for `slife2.events.decode`'s reason: a
    notification this build has no word for is a line not read, and the caller
    is a display that should stay up rather than a turn that should die.
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
    return Handover(ticket=int(payload.get("id") or 0), text=text, channel=channel)


class WindowSession:
    """One agent's session: the stream a window is holding, and what waits on it.

    Created when a window attaches (`Windows.attach`), and it lives exactly as
    long as that call does.  What it holds is a queue of hand-overs *plus* the
    caller parked on each one — the second half is what makes this a pipe rather
    than an inbox, since nothing here outlives the wait for a window to take it.

    No cap on the queue, and it needs none: every entry has a caller blocked on
    its delivery, so how many may be in flight is decided by how many callers
    there are (`slife2.wechat.MAX_INFLIGHT` bounds its own).
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

    def post(self, text: str, channel: str) -> tuple[Handover, asyncio.Future[None]]:
        """Queue a hand-over, and answer with it and the caller that will wait."""
        self._next_ticket += 1
        handover = Handover(self._next_ticket, text, channel)
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
    """Every agent's session, started the first time that agent is named.

    The shape is `slife2.wechat.Wechat`'s, deliberately: one process shared by
    every agent, and a `dict` keyed by the agent rather than a process per agent.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, WindowSession] = {}

    def attach(self, agent: str) -> WindowSession:
        """A window announces itself, replacing whatever session was there.

        Replaced rather than refused, because the case it covers is a window that
        died without its stream closing — a hard kill — and the second attach is
        the moment the stale session is discovered.  Two *live* windows cannot
        collide here: an agent name is exclusive (`slife2.launcher.claim_agent`).
        """
        previous = self._sessions.get(agent)
        if previous is not None:
            previous.attached = False
            previous.fail_pending(f"another window took over {agent}'s conversation")
        session = WindowSession(agent)
        self._sessions[agent] = session
        return session

    def detach(self, agent: str, session: WindowSession) -> None:
        """Forget a session — but only if it is still the current one.

        The identity check is the point: a replaced session's push loop ends
        later than the replacement started, and a detach that removed by name
        would delete the window that is actually there.
        """
        session.attached = False
        if self._sessions.get(agent) is session:
            del self._sessions[agent]

    def showing(self, agent: str) -> WindowSession | None:
        """The session a message for *agent* should go to, if a window holds one.

        A peer that is not there is this system's broken-system answer rather than
        a degraded one (`slife2.mcp_server.open_server`), which for this plugin
        means the same thing it has always meant: nobody is showing that
        conversation, so there is nowhere for the words to be typed.
        """
        session = self._sessions.get(agent)
        return session if session is not None and session.attached else None


async def _push(ctx: Context, session: WindowSession) -> None:
    """Hand over what arrives, down the call the window is holding.

    **The loop lives in the attach handler's own task, and that is load-bearing.**
    `Context.report_progress` resolves its progress token from the request that
    is current *when it is called*, so a context stashed in server state and used
    from `submit`'s task would push into the submitter's own stream.  Reported
    from inside this handler, the token is this call's, which is the same thing
    `ProgressObserver` does for a turn (`slife2.server.server`).
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

    The config is accepted and unread, because `slife2.mcp_server.serve_plugin`
    hands every plugin the config it read and this one reads no section: the
    address it is reached at is the launcher's business rather than its own.
    """
    windows = Windows()
    mcp: FastMCP = house_server(SERVER_NAME, instructions=INSTRUCTIONS)

    @mcp.tool
    async def attach(agent: str, ctx: Context) -> None:
        """Attach this window to *agent*'s session, and be handed messages on it.

        **Hold this call open for as long as the window is showing the
        conversation.** It does not return: a message for this agent arrives as a
        progress notification on this call, and the caller must have supplied a
        progress handler, since `report_progress` is a silent no-op without one
        and a window that sent none would be handed nothing while every `submit`
        still succeeded (`slife2.events`).

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

    @mcp.tool
    async def submit(agent: str, text: str, channel: str) -> None:
        """Hand an agent's window a message to treat as something the person typed.

        The words go into the window's own queue, so they are run in the order it
        has already been handed things, one turn at a time, and the answer appears
        in the transcript as any other turn's does.

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
                the turn's channel (`slife2.server`) and the bubble the line wears.

        Raises:
            ToolError: If no window is showing that agent — which is the same
                thing this system says for any missing peer, arriving here as
                "nobody is looking at that conversation".
        """
        session = windows.showing(agent)
        if session is None:
            raise ToolError(
                f"{NO_WINDOW} {agent}'s conversation; "
                f"start `slife2 --agent {agent}` and hand it over again"
            )

        handover, waiter = session.post(text, channel)
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
            # **Collected even though the `await` above already raised it.**  A
            # cancellation landing between the push answering and this task
            # resuming leaves the failure on the future with nobody to read it,
            # and `asyncio` reports that at collection time as a warning about a
            # turn that failed for a reason the log line cannot name.
            if waiter.done() and not waiter.cancelled():
                waiter.exception()

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
