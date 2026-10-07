"""How the TUI talks to the agent server.

:class:`AgentClient` is a Protocol, so the TUI never imports FastMCP and tests
can inject a scripted client.  That seam is the whole reason the widgets can be
built and verified without a server running.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from fastmcp import Client
from fastmcp.exceptions import ToolError

from slife2.config import AGENT_SERVER_NAME, DEFAULT_AGENT
from slife2.events import TurnEvent, decode
from slife2.mcp_server import close_server, open_server

logger = logging.getLogger(__name__)

#: How long a whole turn may take.  A turn is several model calls plus tool
#: runs, so the default client timeout would abandon it mid-stream and look
#: like a bug in the loop rather than a timeout.
#:
#: This is a *turn's* budget and not a wait's: the app holds its own queue of
#: submissions and sends the next one only once the previous has finished, so a
#: call is never sitting on the server waiting its turn while this clock runs.
TURN_TIMEOUT_SECONDS = 900.0


class AgentClient(Protocol):
    """The TUI's view of the agent."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def reset(self) -> None:
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

    **This class holds an identity, not a conversation.**  The server keeps the
    history — that is what makes a message sent to a busy agent wait its turn
    instead of displacing it — and it addresses that history by the client id
    this client already knows: who it is (`agent`) and which of that agent's
    conversations it is (`subagent`).

    So there is nothing here to lose.  The id this class used to be handed was a
    thing that could go stale — a daemon restart, an idle window — and every
    caller then had to carry a path for "your conversation is gone".  A name
    cannot go stale: the server starts the conversation when a message arrives
    under a key it has not seen, so "the first message of a new session" and
    "the first message after a restart" are the same code path and the same
    non-event.
    """

    def __init__(
        self,
        url: str,
        *,
        agent: str = DEFAULT_AGENT,
        subagent: str = "",
        model: str = "",
        timeout: float | None = TURN_TIMEOUT_SECONDS,
    ):
        self._url = url
        #: Who this client is.  It selects the conversation's system prompt and
        #: is the name its turns are recorded under.
        self._agent = agent
        #: Which of that agent's conversations.  Empty for the one a person is
        #: watching; a name for a worker, which is a different conversation with
        #: its own history whose turns are not written to memory.  A TUI is
        #: always the former, which is why it does not expose this as a flag.
        self._subagent = subagent
        #: Which model to ask for, as `provider/model`.  The server reads it when
        #: the conversation starts and keeps it, so sending it every turn is
        #: idempotent rather than wrong — a conversation's model is a property of
        #: the conversation, and changing it is what `reset` is for.
        self._model = model
        self._timeout = timeout
        self._client: Client | None = None

    @property
    def client_id(self) -> tuple[str, str]:
        """Who this client is, as every server in this system spells it."""
        return (self._agent, self._subagent)

    async def connect(self) -> None:
        """Open the connection, or raise with a message worth showing.

        The URL is in the error because "connection refused" without an address
        is the least useful thing a terminal can say, and so is a bare timeout.
        Both come from `slife2.mcp_server.open_server`, which is where every
        client in this system connects to a peer and where the rule about what
        to do when one is missing lives.
        """
        if self._client is not None:
            return
        self._client = await open_server(
            self._url,
            name=AGENT_SERVER_NAME,
            fallback_tool="send_message",
            timeout=self._timeout,
        )

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await close_server(client)

    async def reset(self) -> None:
        """Start a new conversation under the same id.

        One call, and no state of ours changes: the server forgets the history,
        and the next message under this id begins a new one.  Nothing follows the
        old conversation into the new one, because nothing about it was ever
        held here.
        """
        if self._client is None:
            return
        await self._client.call_tool(
            "reset",
            {"agent": self._agent, "subagent": self._subagent},
            timeout=self._timeout,
        )

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

        payload: dict[str, object] = {
            "agent": self._agent,
            "subagent": self._subagent,
            "prompt": prompt,
            # Every turn this client sends came from somebody typing, which is
            # the whole of what the channel records.  A second kind of caller
            # gets a second client rather than a flag on this one.
            "channel": "human",
        }
        # Omitted rather than sent empty: an empty string and an absent key mean
        # the same thing to the server — "you choose" — and a payload that says
        # nothing once is clearer than one that says it twice.
        if self._model:
            payload["model"] = self._model
        if images:
            payload["images"] = images

        try:
            result = await self._client.call_tool(
                "send_message",
                payload,
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

        return str((result.data or {}).get("text") or "")
