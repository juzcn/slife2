"""How the TUI talks to the agent server.

:class:`AgentClient` is a Protocol, so the TUI never imports FastMCP and tests
can inject a scripted client.  That seam is the whole reason the widgets can be
built and verified without a server running.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Protocol

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

    @property
    def connected(self) -> bool:
        """Whether a live connection is held right now.

        Asked rather than remembered by the caller.  A turn that fails at the
        transport drops the connection here, and a window keeping its own latch
        would go on believing it was connected — every later turn failing the
        same way, against a server nothing is talking to.
        """
        ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def reset(self) -> None:
        """Forget the conversation so the next turn starts fresh."""
        ...

    async def transcript(self) -> list[dict[str, Any]]:
        """The turns this conversation is made of, for a window to draw.

        Empty for a conversation that has never run, which is the honest answer
        for a new name and not a failure.  Read once, when the window opens: the
        history is the server's, so a window that failed to read it has lost
        nothing but the sight of it.
        """
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
        #: its own history whose turns are not written to the db.  A TUI is
        #: always the former, which is why it does not expose this as a flag.
        self._subagent = subagent
        #: Which model to ask for, as `provider/model`.  The server reads it when
        #: the conversation starts and keeps it, so sending it every turn is
        #: idempotent rather than wrong — a conversation's model is a property of
        #: the conversation, and changing it is what `reset` is for.
        self._model = model
        self._timeout = timeout
        self._client: Client | None = None
        #: Serialises `connect`, which has an await between its check and its
        #: use.  See the comment there.
        self._connecting = asyncio.Lock()

    @property
    def client_id(self) -> tuple[str, str]:
        """Who this client is, as every server in this system spells it."""
        return (self._agent, self._subagent)

    @property
    def connected(self) -> bool:
        """Whether a live connection is held.  See `AgentClient.connected`."""
        return self._client is not None

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
        # Under a lock, because the check above and the connection below are
        # separated by an await and two callers overlap in that gap: a prompt
        # typed while `on_mount`'s connect worker is still handshaking is the
        # ordinary case on a slow start.  Without this both calls enter a client
        # and `self._client` keeps whichever finished last — the other is
        # unreachable, so `close` never closes it and its task group and
        # connection live on for the session.
        async with self._connecting:
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

    async def transcript(self) -> list[dict[str, Any]]:
        """Read the conversation back, for the window that has just opened.

        The transport rule is `run_turn`'s, for `run_turn`'s reason: a failure
        that is not the server *answering* means the connection is gone, and a
        client that kept it would look connected to a server nothing is talking
        to for the rest of the session.
        """
        if self._client is None:
            raise ConnectionError("not connected")
        try:
            result = await self._client.call_tool(
                "transcript",
                {"agent": self._agent, "subagent": self._subagent},
                timeout=self._timeout,
            )
        except ToolError:
            raise
        except Exception:
            await self.close()
            raise
        turns = (result.data or {}).get("turns")
        return list(turns) if isinstance(turns, list) else []

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
            # Every turn this client sends came from somebody typing at *this*
            # window, and that is the whole of what the channel records — which
            # is why it names the window and not the person: `human` was the
            # first spelling and it says less, since a turn's channel is what
            # tells one caller's turns from another's, and "a human" does not
            # distinguish the terminal from anything else a person might type
            # into.  A second kind of caller gets a second client rather than a
            # flag on this one.
            "channel": "tui",
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
