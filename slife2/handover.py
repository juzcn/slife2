"""Handing a message to a conversation the caller is not inside.

**This is `slife2-tui`'s `submit` with the policy around it, and the policy is
the whole of the module.**  §1.2 makes `submit` this system's one path for
"something outside the conversation became a turn in it" — a WeChat message, an
agent task's answer, a scheduled run coming due — and the design of that path is
one sentence: **the window, and only the window.**

**The window is the conversation, because the window is what runs it.**  An agent
is served by a window — a terminal or `--headless`, and both attach — so a
conversation with no window is not a conversation with fewer readers; it is one
nobody is running.  There used to be a second road for that case (the agent server
ran the turn anyway, and a window that opened later restored it), and it was
wrong in the way every fallback is: it made "the window is gone" a *state to
carry on in*, so work nobody could see went ahead anyway — spending the model,
advancing a conversation no one was reading, and answering a peer that believed
it had been heard.  A delivery that cannot be made is now a **hard failure**
(`DeliveryFailed`), and each caller says what that means for the work it was
trying to hand over.  None of them carries on as though it had landed.

**Retrying is for the gap, and the gap is seconds.**  An agent's window attaches
a moment after its stack comes up, and a window that loses its attach offers to
take it again within `pacing.reconnect` — so a first refusal is usually "the
terminal is starting", which is worth another try.  That retry lives *here*,
because it is a fact about the window rather than about the message: every caller
gets the same patience, and none of them has to know why the window was away.

**Three answers, and the middle one is why this is not a boolean.**  Everything a
tool handler raises arrives at this end as a `ToolError` (`serve` pins
`mask_error_details=False`, which is what keeps the text), so the exception
*type* cannot tell a refusal from a dropped stream — `slife2.tui.control.refused`
has to be asked, and it is the module that writes the refusal that answers.  A
refusal is the one failure that proves nothing was written, because the terminal
looks its session up before it posts anything; everything else means the words
may already be down the stream.  That distinction decides *what a retry is worth*
— a refusal may be tried again, and a call that did not come back may not, since
repeating it is how one message becomes two turns — and it is spent here rather
than passed on: what the callers get is one failure, whose reason is in it.

**And one thing this module hands over is not a message at all.**  A *note*
(`note_to_window`) is a line the harness has to say and nobody has to answer, so
it goes to the window and stops there: a line no window took is a line nobody
needs, and it is never retried — a note is about the moment it describes, and a
`✗ offline` that arrives after the window has moved on is worth less than silence.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastmcp import Client

import slife2.timeouts as _T
from slife2.config import TUI_SERVER_NAME, Config
from slife2.mcp_server import close_server, open_server
from slife2.tui import control
from slife2.tui.control import NOTE

logger = logging.getLogger(__name__)

#: How many times a hand-over is tried before it is a failure.  Three, because
#: what the retry is for is the window's own gap — an agent's window attaches a
#: moment after its stack comes up — and because the wait between the attempts is
#: the terminal's own `pacing.reconnect`, the pause before it offers to attach
#: again.
_ATTEMPTS = 3


async def submit_to(window: Client, agent: str, text: str, channel: str) -> bool | None:
    """One `submit`, classified: written, refused, or unknown.

    See the module docstring for why the last two are different answers.  The
    refusal comes back as `False` — the one result a caller may safely act on by
    trying somewhere else.
    """
    try:
        await window.call_tool(
            "submit",
            {"agent": agent, "text": text, "channel": channel},
            timeout=_T.timeouts.transport.window,
        )
    except Exception as exc:  # noqa: BLE001 - three answers, and this is both
        if control.refused(exc):
            logger.info("no window is showing %s: %s", agent, exc)
            return False
        logger.warning("the hand-over to %s did not come back: %s", agent, exc)
        return None
    return True


@contextlib.asynccontextmanager
async def _peer(
    url: str,
    *,
    name: str,
    fallback_tool: str,
    timeout: float,
    client: Client | None,
) -> AsyncIterator[Client]:
    """Yield a client for one of our servers, closing it if we opened it.

    **The close belongs to whoever opened the connection, and that is the whole
    of what this decides.**  A client handed in is used as it is and left open —
    it belongs to the caller, which is how a test drives a hand-over with no
    network at all — so the close is conditional on `client is None`, and both
    hand-over paths answer that one question here rather than each spelling out
    the open, the `finally` and the close for itself.

    **And the close survives the cancellation it exists for.**  This `finally`
    runs on exactly the path where the caller was cancelled, which is why a
    close belongs there — but `CancelledError` is not an `Exception`, so a
    cancellation delivered while `close_server`'s `__aexit__` is awaiting would
    pass straight through its `contextlib.suppress(Exception)` and leave the
    connection open, the one outcome the close was there to prevent.  `shield`
    runs the close in a task of its own, out of this frame's reach, so it
    finishes either way; the cancellation still propagates from here once it
    has, so the caller's task is cancelled as it expected.
    """
    if client is not None:
        yield client
        return
    opened = await open_server(
        url, name=name, fallback_tool=fallback_tool, timeout=timeout
    )
    try:
        yield opened
    finally:
        await asyncio.shield(close_server(opened))


async def to_window(
    config: Config, agent: str, text: str, channel: str, *, client: Client | None = None
) -> bool | None:
    """Hand a message to the window showing this agent's conversation.

    Opened per call and closed after it, the way `slife2-wechat` hands one over:
    a hand-over happens once per answer or once per fire rather than once per
    turn, and a long-lived connection to a plugin that may restart is a
    connection to re-establish.  An injected client is used as it is and left
    open — it belongs to whoever handed it in.

    `_peer` owns the open and the close; what is here is the window's address and
    the one answer a failure to reach it produces.
    """
    try:
        async with _peer(
            config.server("tui").url,
            name=TUI_SERVER_NAME,
            fallback_tool="submit",
            timeout=_T.timeouts.transport.window,
            client=client,
        ) as window:
            return await submit_to(window, agent, text, channel)
    except Exception as exc:  # noqa: BLE001 - nothing written, because nothing reached
        logger.info("no window for %s could be reached: %s", agent, exc)
        return False


async def note_to_window(
    config: Config,
    agent: str,
    text: str,
    *,
    client: Client | None = None,
) -> bool | None:
    """Draw one harness line in the window showing this agent, and nowhere else.

    **A line is not a message, and the difference is what happens when no window
    takes it.**  A message that reached no conversation is a hard failure
    (`hand_over`), because a message *is* work for the conversation; a note is a
    line about the moment it describes (`⚡ jill online`), it is addressed to
    nobody, and running a turn for one would spend a model call on a fact nobody
    has to act on.  So a note no window took is simply dropped, and the caller is
    told `False` rather than sent looking for a second place to put it.

    Answers like `submit_to` — `True` written, `False` provably not, `None` this
    cannot tell — because it takes the same road, and a caller reading one of
    these and then the other should not have to learn a second vocabulary.  What
    no caller may do with a note is retry it: everything a note says is about a
    moment that has passed.
    """
    try:
        async with _peer(
            config.server("tui").url,
            name=TUI_SERVER_NAME,
            fallback_tool=NOTE,
            timeout=_T.timeouts.transport.window,
            client=client,
        ) as window:
            try:
                await window.call_tool(
                    NOTE,
                    {"agent": agent, "text": text},
                    timeout=_T.timeouts.transport.window,
                )
            except Exception as exc:  # noqa: BLE001 - three answers, and this is both
                if control.refused(exc):
                    logger.debug("no window is showing %s: %s", agent, exc)
                    return False
                logger.debug("the note to %s did not come back: %s", agent, exc)
                return None
            return True
    except Exception as exc:  # noqa: BLE001 - nothing written, because nothing reached
        logger.debug("no window for %s could be reached: %s", agent, exc)
        return False


class DeliveryFailed(RuntimeError):
    """A message did not reach the conversation it was addressed to.

    **Hard on purpose.**  The window is what runs an agent, so a delivery that
    cannot be made is not a degraded success — it is the one thing a caller must
    not carry on past: the work has not happened, and every caller says so in its
    own terms (a run ends `failed`, an answer is `undelivered`, a peer's message
    is not answered).  An exception rather than a fourth return value because a
    caller that forgot to look at a value would go on as though the hand-over had
    happened, which is exactly the failure this module was rebuilt to stop.

    It carries the agent and the reason, since those are what a caller logs or
    records — not the words, which are the conversation's business.
    """

    def __init__(self, agent: str, reason: str) -> None:
        super().__init__(f"{agent}'s conversation could not be reached: {reason}")
        self.agent = agent
        self.reason = reason


async def hand_over(
    config: Config,
    agent: str,
    text: str,
    channel: str,
    *,
    what: str,
    window_client: Client | None = None,
) -> None:
    """Put a message into the agent's conversation, or fail.

    **A failure is raised, and it is the last word on the message**: nothing is
    handed anywhere else, because the only road is the window (`slife2.handover`'s
    module docstring has the argument), and nothing here retries a call that may
    already have landed.

    `what` is the caller's own name for the thing being handed over — "agent task
    research", "the run due at 09:00" — and it is here because the lines this
    function logs are the only place it appears; a caller that had to write them
    would be a caller writing this function again.

    **The retry below is for the window's own gap**: an agent's window attaches a
    moment after its stack comes up, and one that lost its attach takes it again
    within `pacing.reconnect`.  So a refusal is tried again at that pace — the
    wait the terminal itself uses — and a call that did not come back is not,
    since its words may be in the conversation already.
    """
    for attempt in range(1, _ATTEMPTS + 1):
        handed = await to_window(config, agent, text, channel, client=window_client)
        if handed is True:
            return
        if handed is None:
            # The words may already be in the conversation — a call that did not
            # come back says nothing either way — so this is not repeated, and
            # the count is logged rather than the words: the words are the
            # conversation's business and the log's line is only "this much of it
            # went nowhere we can prove".
            raise DeliveryFailed(
                agent,
                f"the hand-over did not come back, so it ({len(text)} characters) "
                f"was not repeated",
            )
        if attempt < _ATTEMPTS:
            await asyncio.sleep(_T.timeouts.pacing.reconnect)
    raise DeliveryFailed(
        agent,
        f"no window is showing it after {_ATTEMPTS} attempts ({what})",
    )
