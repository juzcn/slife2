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

**One tool, and it returns when the window has taken the message, not when the
turn ends.**  That distinction is what makes handing over safe rather than racy:
a caller whose call returns knows the words are somebody else's problem, so a
connection that dropped afterwards cannot make it send the same message twice.
Whether the turn then went well is not a caller's business here — it is in the
conversation's record, and the model answers WeChat itself
(`wechat_send_message`).

**The window is part of the running system.**  It is started by the person and
its address is the `tui:` section — an address and not a server
(`slife2.config`) (`slife2.config`, `slife2.launcher`) — and a peer that is
not there is a broken system rather than a degraded one, which is the answer this
system gives for every other missing server (`slife2.mcp_server.open_server`).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import uvicorn
from fastmcp import FastMCP

from slife2.config import TUI_SERVER_NAME
from slife2.mcp_server import house_server

INSTRUCTIONS = (
    "The terminal showing this agent's conversation. `submit` hands it a message "
    "to treat as something the person typed; it answers as soon as the message is "
    "in the window's queue."
)

#: What the window does with a message it has taken: `(text, channel)`.  A seam
#: because the whole of this module is one call into the application, and a test
#: drives it without a Textual app anywhere.
Deliver = Callable[[str, str], None]


def build_server(*, deliver: Deliver) -> FastMCP:
    """The window as an MCP server: one tool, for one narrow thing."""
    mcp: FastMCP = house_server(TUI_SERVER_NAME, instructions=INSTRUCTIONS)

    @mcp.tool
    async def submit(text: str, channel: str) -> None:
        """Hand this terminal a message to treat as something the person typed.

        The words go into the window's own queue, so they are run in the order it
        has already been handed things, one turn at a time, and the answer appears
        in the transcript as any other turn's does.

        **Returning is the acknowledgement**, and that is the whole of the answer:
        a caller whose call came back knows the words are queued, which is what
        makes handing them over safe to retry. What the turn did with them is in
        the conversation's record.

        Args:
            text: The words, exactly as the model should receive them. A channel
                that annotates its messages puts its marker in this string, and
                the window neither reads nor alters it.
            channel: Where they came in from: the caller's own key, which becomes
                the turn's channel (`slife2.server`) and the bubble the line wears.
        """
        deliver(text, channel)

    return mcp


@asynccontextmanager
async def serving(mcp: FastMCP, url: str) -> AsyncIterator[None]:
    """Serve the window at *url* — the `tui:` section's address — until it ends.

    The URL is taken apart here rather than handed over in pieces, because this
    module is what owns what the window's address means: the caller has a URL and
    so does every other address in this system (`slife2.config`).

    `json_response=False` for the reason every server here sets it: the response
    stream is what a long call rides on.  Nothing waits for the port to be bound —
    whoever dials this is another process in this system, and a port that is not up
    yet is the failure `open_server`'s probe is for.
    """
    parts = urlsplit(url)
    app = mcp.http_app(
        path=parts.path or "/mcp", json_response=False, stateless_http=True
    )
    config = uvicorn.Config(
        app,
        host=parts.hostname or "127.0.0.1",
        port=parts.port or 80,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        yield
    finally:
        server.should_exit = True
        # Suppressing the cancellation as well as an ordinary failure: this runs
        # while the window is closing, and a uvicorn that will not stop is not
        # worth holding an exit for.
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
