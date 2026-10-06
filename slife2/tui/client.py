"""How the TUI talks to the agent server.

:class:`AgentClient` is a Protocol, so the TUI never imports FastMCP and tests
can inject a scripted client.  That seam is the whole reason the widgets can be
built and verified without a server running.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Protocol

from fastmcp import Client
from fastmcp.exceptions import ToolError

from slife2.config import DEFAULT_AGENT
from slife2.events import TurnEvent, decode

logger = logging.getLogger(__name__)

#: How long a whole turn may take.  A turn is several model calls plus tool
#: runs, so the default client timeout would abandon it mid-stream and look
#: like a bug in the loop rather than a timeout.
TURN_TIMEOUT_SECONDS = 900.0


class AgentClient(Protocol):
    """The TUI's view of the agent."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    def reset(self) -> None:
        """Forget the conversation so the next turn starts fresh."""
        ...

    async def run_turn(
        self,
        prompt: str,
        on_event: Callable[[TurnEvent], None],
        *,
        images: list[str] | None = None,
    ) -> str:
        """Run a turn, calling `on_event` as it goes.

        Returns the final assistant text, which is authoritative — `on_event`
        is a display channel and its events may be dropped or delayed.

        `images` are `data:` URLs to send with the prompt.
        """
        ...


class MCPAgentClient:
    """An :class:`AgentClient` backed by an MCP connection.

    **This class owns the conversation.**  The agent server is stateless, so
    whoever wants memory has to keep it, and the TUI is the only party with a
    reason to.  The messages are treated as opaque tokens: they are whatever the
    server returned last time, handed straight back, and never constructed here.
    That keeps the wire format the server's business.
    """

    def __init__(
        self,
        url: str,
        *,
        agent: str = DEFAULT_AGENT,
        model: str = "",
        timeout: float | None = TURN_TIMEOUT_SECONDS,
    ):
        self._url = url
        #: Who this client says it is.  It reaches the server's system-prompt
        #: template; see `slife2.server.server.run_turn`.
        self._agent = agent
        #: Which model to ask for, as `provider/model`.  Sent per turn rather
        #: than fixed at the server, because one agent server serves every
        #: caller and two instances may want different models.
        self._model = model
        self._timeout = timeout
        self._client: Client | None = None
        self._history: list[dict[str, object]] = []

    async def connect(self) -> None:
        """Open the connection, or raise with a message worth showing.

        The URL is in the error because "connection refused" without an address
        is the least useful thing a terminal can say, and so is a bare timeout.

        The probe is `tools/list` rather than `ping`: the 2026-07-28 revision of
        MCP made the protocol stateless and removed the protocol-level ping, so
        a conforming server answers `ping` with "Method not found".  Listing
        tools is the documented liveness check, and it also catches the case
        where the URL points at some *other* MCP server.
        """
        if self._client is not None:
            return
        client: Client = Client(self._url, timeout=self._timeout)
        try:
            await client.__aenter__()
            names = {tool.name for tool in await client.list_tools()}
            if "run_turn" not in names:
                raise ConnectionError(
                    f"not a slife2 agent server (tools: {sorted(names) or 'none'})"
                )
        except Exception as exc:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)
            raise ConnectionError(f"{self._url}: {exc}") from exc
        self._client = client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)

    def reset(self) -> None:
        """Forget the conversation.  Costs nothing — the server has no copy."""
        self._history.clear()

    async def run_turn(
        self,
        prompt: str,
        on_event: Callable[[TurnEvent], None],
        *,
        images: list[str] | None = None,
    ) -> str:
        if self._client is None:
            raise ConnectionError("not connected")

        async def on_progress(
            progress: float, total: float | None, message: str | None
        ) -> None:
            event = decode(message or "")
            if event is not None:
                on_event(event)

        try:
            result = await self._client.call_tool(
                "run_turn",
                {"messages": self._history, "prompt": prompt, "agent": self._agent},
                progress_handler=on_progress,
                # Passing a progress handler is what makes the SDK attach a
                # progress token, and `report_progress` on the server is a
                # *silent no-op* without one.  Omitting it produces a turn that
                # works perfectly and shows nothing until the very end — see
                # `slife2.events`.
                timeout=self._timeout,
            )
        except ToolError:
            # The server answered and the turn failed inside it — a bad API key,
            # a model that does not exist.  The connection is fine, so keep it.
            raise
        except Exception:
            # The transport itself is gone.  Dropping the client here is what
            # lets the app's lazy retry actually reconnect: `connect()` returns
            # early while `self._client` is set, so without this the app would
            # stay "connected" to a dead server for the rest of the session and
            # every subsequent turn would fail the same way.
            await self.close()
            raise

        data = result.data or {}
        # Extend only after the call succeeded: a cancelled or failed turn
        # leaves the history untouched, which is what makes an interrupted turn
        # safe without any repair logic on either side.
        self._history.extend(data.get("new_messages") or [])
        return str(data.get("text") or "")
