"""Handing a message to a conversation the caller is not inside.

**This is `slife2-tui`'s `submit` with the policy around it, and the policy is
the whole of the module.**  §1.2 makes `submit` this system's one path for
"something outside the conversation became a turn in it" — a WeChat message, an
agent task's answer, a scheduled run coming due — and the design of that path is
one sentence: **the window first, the agent server as the fallback, and three
answers rather than two about whether it worked.**

The window is first because that is where a message becomes a turn *and* becomes
something the person sees, in their own queue.  The fallback exists because a
window that is not showing that agent is **not** a lost result: the agent server
runs the turn anyway, records it, and a window that opens later restores it.  So
"nobody is looking" and "nobody is there" are different situations and only the
second one is a failure.

**Three answers, and the middle one is why this is not a boolean.**  Everything a
tool handler raises arrives at this end as a `ToolError` (`serve` pins
`mask_error_details=False`, which is what keeps the text), so the exception
*type* cannot tell a refusal from a dropped stream — `slife2.tui.control.refused`
has to be asked, and it is the module that writes the refusal that answers.  A
refusal is the one failure that proves nothing was written, because the terminal
looks its session up before it posts anything; everything else means the words
may already be down the stream.  Repeating *those* is how one message arrives
twice, which is worse than one arriving late — so the caller is told "unknown"
and does not retry.

Both callers keep that rule rather than restating it: `slife2-agent-task` marks
its answer undelivered and does not hand it anywhere else, and `slife2-schedule`
leaves the run `pending` rather than calling it failed.  Neither one repeats a
hand-over it could not confirm, and that is a property of this return value.
"""

from __future__ import annotations

import contextlib
import logging

from fastmcp import Client

import slife2.timeouts as _T
from slife2.config import AGENT_SERVER_NAME, TUI_SERVER_NAME, Config
from slife2.mcp_server import open_server
from slife2.tui import control

logger = logging.getLogger(__name__)

#: How long to wait on a peer that is *handed* something rather than asked.  The
#: window is quick — `submit` returns when the words are down the stream, not
#: when the turn they started ends — and the agent server is not, because the
#: fallback runs a whole turn before it answers.  **Two numbers for two different
#: promises**, and using the window's for the fallback would cut off every
#: hand-over that had to go the long way round.


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


async def to_window(
    config: Config, agent: str, text: str, channel: str, *, client: Client | None = None
) -> bool | None:
    """Hand a message to the window showing this agent's conversation.

    Opened per call and closed after it, the way `slife2-wechat` hands one over:
    a hand-over happens once per answer or once per fire rather than once per
    turn, and a long-lived connection to a plugin that may restart is a
    connection to re-establish.  An injected client is used as it is and left
    open — it belongs to whoever handed it in.
    """
    if client is not None:
        return await submit_to(client, agent, text, channel)
    try:
        window = await open_server(
            config.server("tui").url,
            name=TUI_SERVER_NAME,
            fallback_tool="submit",
            timeout=_T.timeouts.transport.window,
        )
    except Exception as exc:  # noqa: BLE001 - nothing written, because nothing reached
        logger.info("no window for %s could be reached: %s", agent, exc)
        return False
    try:
        return await submit_to(window, agent, text, channel)
    finally:
        with contextlib.suppress(Exception):
            await window.__aexit__(None, None, None)


async def to_agent(
    config: Config,
    agent: str,
    text: str,
    channel: str,
    *,
    client: Client | None = None,
) -> None:
    """Continue the agent's own conversation, because no window is showing it.

    §3's "a caller which is not displaying the conversation can still continue
    one", which is what that sentence is for.  **Raises** rather than answering
    with a boolean: reaching the agent server and failing is the last thing the
    caller can try, and there is no third place to go — so the failure belongs to
    whoever knows what it was handing over.
    """
    if client is not None:
        await client.call_tool(
            "send_message", {"agent": agent, "prompt": text, "channel": channel}
        )
        return
    server = await open_server(
        config.server("agent").url,
        name=AGENT_SERVER_NAME,
        fallback_tool="send_message",
        timeout=_T.timeouts.transport.agent,
    )
    try:
        await server.call_tool(
            "send_message",
            {"agent": agent, "prompt": text, "channel": channel},
            timeout=_T.timeouts.transport.agent,
        )
    finally:
        with contextlib.suppress(Exception):
            await server.__aexit__(None, None, None)


async def hand_over(
    config: Config,
    agent: str,
    text: str,
    channel: str,
    *,
    what: str,
    window_client: Client | None = None,
    agent_client: Client | None = None,
) -> bool | None:
    """Put a message into an agent's conversation, wherever it can be reached.

    `what` is the caller's own name for the thing being handed over — "agent task
    research", "the run due at 09:00" — and it is here rather than assembled by
    the caller because the two lines this function logs are the only place it
    appears, and a caller that had to write them would be a caller writing this
    function again.

    Returns **`True`** when the words reached a conversation, **`False`** when
    they provably reached none, and **`None`** when this cannot tell.  What each
    caller does with the last two is its own decision and is the reason this is
    not a boolean; what neither may do is try a second time on `None`.
    """
    handed = await to_window(config, agent, text, channel, client=window_client)
    if handed is None:
        # **Not repeated anywhere else, and logged rather than swallowed.**  The
        # words may already be in the conversation — a call that did not come
        # back says nothing either way — so a second copy is the one outcome
        # worse than silence.  The count is logged rather than the words, because
        # the words are the conversation's business and the log's line is only
        # "this much of it went nowhere we can prove".
        logger.error(
            "%s: the hand-over to %s did not come back, so it (%d characters) "
            "was not handed anywhere else",
            what,
            agent,
            len(text),
        )
        return None
    if handed is True:
        return True
    try:
        await to_agent(config, agent, text, channel, client=agent_client)
    except Exception:  # noqa: BLE001 - nothing left to try, and it must be said
        logger.exception("%s: could not be handed over at all", what)
        return False
    return True
