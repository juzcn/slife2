"""How the TUI talks to the servers it needs: the agent, its inbox, and the mesh.

Three clients, because a window holds three conversations with the outside: it
*asks* the agent server for turns (:class:`AgentClient`), it *is asked* by the
plugin that hands it messages from other channels (:class:`WindowClient`), and it
*holds a place* for its agent on the A2A mesh (:class:`MeshClient`).  The last two
point the other way round from the first, which is why both are calls held open
rather than anything polled — and why the window's own liveness is what they
report: a window that goes away drops both, and a peer sees the agent leave the
mesh at the same moment the inbox closes.

All three are Protocols, so the TUI never imports FastMCP at the point of use and
tests can inject scripted clients.  That seam is the whole reason the widgets can
be built and verified without a server running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from fastmcp import Client
from fastmcp.exceptions import ToolError

import slife2.timeouts as _T
from slife2.a2a.server import JOIN
from slife2.config import (
    A2A_SERVER_NAME,
    AGENT_SERVER_NAME,
    DEFAULT_AGENT,
    TUI_SERVER_NAME,
)
from slife2.events import TurnEvent, decode
from slife2.mcp_server import close_server, open_server
from slife2.tui.control import ATTACH, Handover
from slife2.tui.control import decode as decode_handover
from slife2.tui.widgets import WINDOW_CHANNEL

#: How every tool in this system says no: a sentence beginning with this, which
#: a model reads as an answer and a person reads as a refusal.  Spelled once
#: here because two callers now ask the same question of one answer — the app,
#: which shows it as a failure, and this client, which must not adopt a model
#: the server just refused.
REFUSAL = "[refused]"


def refused(answer: str) -> bool:
    """Whether a tool's answer is a refusal rather than a result."""
    return answer.startswith(REFUSAL)


def _unreadable(what: str, answer: Any) -> ConnectionError:
    """The loud failure a tool's answer that is not its shape raises, worded once.

    Three answers here are contracts rather than data — a turn's `text`, the
    transcript's `turns`, and the sentence `model_switch` replies with — and each
    has an obvious empty that reads as a legitimate one: an answer with no words,
    a conversation that never ran, a switch that worked.  So each quotes the
    payload rather than summarising it, because a shape this build cannot read is
    the only evidence there is that the far end is a different build (DESIGN.md
    §10.1: a value that cannot be read is not a value that is absent).
    """
    return ConnectionError(f"the agent server did not {what} (it said {answer!r})")


def answer_of(result: Any) -> str:
    """The words a tool answered with, however the transport carried them.

    `model_switch` returns a plain string, which FastMCP wraps and hands back as
    `data` — so that is the first shape read, and the content-block half is the
    fallback for a server that answered the older way.

    **A reply that is neither is refused, not answered as empty.**  The refusal
    this client acts on (`refused`) is a *prefix*, so an unreadable answer read
    as `""` is read as a successful switch: the client adopts the ref and the bar
    describes a model that is not the one answering.  An unknown shape raises
    naming what came back (DESIGN.md §10.1: a failure is not a verdict).
    """
    data = getattr(result, "data", None)
    if isinstance(data, str):
        return data
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        return data["text"]
    for block in getattr(result, "content", None) or ():
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    raise _unreadable(
        "answer with words",
        data if data is not None else getattr(result, "content", None),
    )


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

    async def switch_model(self, ref: str) -> str:
        """Make `provider/model` what this machine runs on, and say what happened.

        **The answer is the agent server's own sentence**, because it is the
        only place that says what a switch does to a conversation already
        running — and a client that paraphrased it would be a second story about
        the same fact.  A refusal arrives as an answer too (`refused`), not as
        an exception: the server answered, the connection is fine, and the
        sentence is written for a person to read.

        **It needs the agent server and not the model**, which is the whole
        reason the key exists: switching away from a model that is down is the
        one thing to do about it, and it has to work precisely then.  Nothing
        here calls the model.
        """
        ...

    async def transcript(self) -> list[dict[str, Any]]:
        """The turns this conversation is made of, for a window to draw.

        Empty for a conversation that has never run, which is the honest answer
        for a new name and not a failure.  Read once, when the window opens: the
        history is the server's, so a window that failed to read it has lost
        nothing but the sight of it.

        **A read that is missing `turns` is not that empty.**  A name with no
        history is answered with a real turn list that happens to be empty
        (`slife2.server.server.transcript`, over `restore`'s own `{"turns": []}`);
        a payload whose `turns` cannot be read is damage, and reading it as `[]`
        is a window that shows a blank history and marks itself restored
        (DESIGN.md §10.1: a value that cannot be read is not a value that is
        absent).
        """
        ...

    async def run_turn(
        self,
        prompt: str,
        on_event: Callable[[TurnEvent], None],
        *,
        channel: str = WINDOW_CHANNEL,
        images: list[str] | None = None,
    ) -> str:
        """Run a turn, calling `on_event` as it goes.

        Returns the final assistant text, which is authoritative — `on_event`
        is a display channel and its events may be dropped or delayed.  A reply
        without it is not an answer, and it raises rather than returning `""`,
        which the window would draw as a question with no answer and no error.

        `channel` is where the message came in on.  The window passes its own for
        what the person typed, and something else when it is running a message
        another channel handed it (`slife2.tui.app.deliver`) — the turn is
        recorded with it and the line wears it as a bubble.

        `images` are `data:` URLs to send with the prompt.
        """
        ...

    async def answer_approval(self, call_id: str, approved: bool) -> str:
        """Answer the `_approve` request a turn is waiting on, and say what happened.

        **It is a call of its own because the turn's channel runs one way**: the
        request arrives as a progress notification (`ToolApprovalRequested`), and
        a notification cannot carry an answer back.  So the window answers here,
        on the same connection, while the turn that asked is still parked inside
        `send_message` — which is why this must not wait for anything the server
        holds per conversation: the waiting turn holds that.

        The returned sentence is the server's, and it is empty when a waiting
        turn took the answer.  **Losing the race is not an error**: the turn may
        have ended (the person pressed Esc a moment before pressing `y`), and a
        client that raised over it would call a prompt that arrived too late a
        failure of the connection.
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
        timeout: float | None = None,
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
        #: a conversation *starts*, so sending it every turn is idempotent rather
        #: than wrong.  What changes a model is `model_switch`, and it is why
        #: `switch_model` moves this too: the next conversation to start — after
        #: an idle conversation is let go, or a window reopened — must start on
        #: the model that was just chosen, and not on the one this client was
        #: built with.
        self._model = model
        # None means "the registry's management patience", resolved here
        # rather than in the signature: a def-time default captures the value at
        # import (`slife2.timeouts` states the rule).
        self._timeout = _T.timeouts.work.management if timeout is None else timeout
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
                # **No clock on the connection**, because a connection-level
                # deadline is a deadline on *every* call that crosses it — turns
                # included (see `slife2.timeouts.work.management`).  Each call states
                # its own patience below, which is where a number that applies to
                # one thing belongs.
                timeout=None,
            )

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await close_server(client)

    async def _call_tool(self, name: str, payload: dict[str, Any]) -> Any:
        """Call one management tool, stating the transport rule in one place.

        **A transport failure drops the connection; a `ToolError` does not.**
        That is the whole of what `model_switch`, `transcript` and
        `answer_approval` have in common, and each of them spelled it out — three
        copies of one `try`/`except`, differing only in a tool name and a payload,
        which is exactly the shape a fourth caller copies *wrongly*.  Written once
        here, the sentence the class docstring repeats three times is a statement
        about this method rather than a rule the next one has to remember.

        `ToolError` is re-raised untouched because it is the server *answering* —
        a refusal it wrote for a person to read — and the connection is fine, so
        dropping it would turn a bad request into a reconnect storm on every
        retry.  Anything else is the transport itself, and dropping the client is
        what lets the app's lazy retry reconnect instead of believing it is
        connected to a server nothing is talking to (see `run_turn`).
        """
        if self._client is None:
            raise ConnectionError("not connected")
        try:
            return await self._client.call_tool(name, payload, timeout=self._timeout)
        except ToolError:
            raise
        except Exception:
            await self.close()
            raise

    async def switch_model(self, ref: str) -> str:
        result = await self._call_tool("model_switch", {"ref": ref})
        answer = answer_of(result)
        # Adopted only when the server said it happened.  A refused switch wrote
        # nothing, so carrying the reference here would pin the next conversation
        # to a model this client was just told does not exist.
        if not refused(answer):
            self._model = ref
        return answer

    async def transcript(self) -> list[dict[str, Any]]:
        """Read the conversation back, for the window that has just opened.

        The transport rule is `_call_tool`'s, for `_call_tool`'s reason: a
        failure that is not the server *answering* means the connection is gone,
        and a client that kept it would look connected to a server nothing is
        talking to for the rest of the session.

        A `turns` that is not a list raises rather than answering `[]`: an empty
        read is a name with no history, and a damaged one read as empty is a
        window that shows nothing and calls itself restored for the session.
        The check is outside `_call_tool`, so the connection stays — the server
        answered, and it is the answer this build cannot read.
        """
        result = await self._call_tool(
            "transcript", {"agent": self._agent, "subagent": self._subagent}
        )
        payload = result.data
        turns = payload.get("turns") if isinstance(payload, dict) else None
        if not isinstance(turns, list):
            raise _unreadable("return a turn list", payload)
        return list(turns)

    async def answer_approval(self, call_id: str, approved: bool) -> str:
        """Answer one `_approve` request.  See `AgentClient.answer_approval`."""
        result = await self._call_tool(
            "answer_approval",
            {
                "agent": self._agent,
                "subagent": self._subagent,
                "call_id": call_id,
                "approved": approved,
            },
        )
        return str((result.data or {}).get("text") or "")

    async def run_turn(
        self,
        prompt: str,
        on_event: Callable[[TurnEvent], None],
        *,
        channel: str = WINDOW_CHANNEL,
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
            # Where this turn came in on, as the caller knows it.  For a
            # message the person typed that is this window (`tui` names the
            # terminal and not the person: `human` was the first spelling and it
            # says less, since a channel is what tells one caller's turns from
            # another's); for one another channel handed the window, it is that
            # channel's key, and the window is only the thing running it.
            "channel": channel,
            # **This client can be asked a question and will answer it.**  A
            # window draws the `_approve` prompt and answers it with
            # `answer_approval` below, so a turn whose model asks to be checked
            # waits for a person here rather than running unasked — which is the
            # only reason the meta-parameter exists.  Every other caller in the
            # system leaves this False, and their turns grant the request.
            "watched": True,
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
                # **And no deadline on the call**, said explicitly rather than
                # left to the default: this is the one call whose length is the
                # answer's own business (`slife2.timeouts.work.management`).
                timeout=None,
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

        # **The turn's answer is `text`, and a payload without it is refused.**
        # This used to read as `""`, which the window draws as a question with no
        # answer and no error — a failed turn presented as a finished one
        # (DESIGN.md §10.1: a value that cannot be read is not a value that is
        # absent).  Raised here, after the `try`, so the connection stays: the
        # server answered, and it is the answer this build cannot read.
        payload = result.data
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise _unreadable("answer the turn", payload)
        return text


class WindowClient(Protocol):
    """The window's view of its own inbox, from the receiving end."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def attach(self, on_handover: Callable[[Handover], None]) -> None:
        """Announce this window and be handed messages until it goes away.

        **Does not return while the window is up**, and that is the mechanism
        rather than an oversight: the session *is* this call, so a message handed
        over by a channel arrives as a progress notification on it and is passed
        straight to `on_handover` — which is the app's `deliver`, the same method
        the person's own typing goes through.

        Returns, or raises, only when the window is done: a cancelled call is the
        detach (`slife2.tui.control`).
        """
        ...


class MeshClient(Protocol):
    """The window's view of the A2A mesh, from the end that keeps it up."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def hold(self) -> None:
        """Put this window's agent on the mesh, and keep it there.

        **Does not return while the window is up**, and that is the mechanism
        rather than an oversight: `slife2.a2a.server`'s `join` is the other end of
        it, so the mesh connection lives exactly as long as this call and
        cancelling it is the departure.  A window that quits, is killed, or loses
        the stream therefore takes its agent *offline* with nothing to send, which
        is what makes `✗ offline` true rather than noticed late — the same
        lifetime `attach` has on the window's own inbox.

        Returns, or raises, only when the window is done — or when the mesh
        refused the agent, which arrives as a `ToolError` carrying the reason.
        """
        ...


class MCPWindowClient:
    """A :class:`WindowClient` backed by an MCP connection.

    Holds an identity and nothing else — which window this is — because
    everything a message does afterwards belongs to the app: this class never
    sees the queue, the turn or the transcript.
    """

    def __init__(self, url: str, *, agent: str = DEFAULT_AGENT):
        self._url = url
        #: Whose conversation this window is showing, which is the whole of what
        #: it tells the plugin: one session per agent, and this is the key.
        self._agent = agent
        self._client: Client | None = None

    async def connect(self) -> None:
        """Open the connection, or raise with a message worth showing.

        The named-peer check is the point: a `tui:` address pointed at anything
        else fails here, by name, rather than as a window that silently receives
        nothing (`slife2.mcp_server.identifies`).
        """
        if self._client is not None:
            return
        self._client = await open_server(
            self._url, name=TUI_SERVER_NAME, fallback_tool=ATTACH
        )

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await close_server(client)

    async def attach(self, on_handover: Callable[[Handover], None]) -> None:
        if self._client is None:
            raise ConnectionError("not connected")

        async def on_progress(
            progress: float, total: float | None, message: str | None
        ) -> None:
            handover = decode_handover(message or "")
            if handover is not None:
                on_handover(handover)

        try:
            await self._client.call_tool(
                ATTACH,
                {"agent": self._agent},
                # Passing a progress handler is what makes the SDK attach a
                # progress token, and the plugin's push is a *silent no-op*
                # without one — a window that omitted this would be handed
                # nothing while every `submit` still succeeded.  See
                # `slife2.events` for the same seam on a turn's stream.
                progress_handler=on_progress,
                # A session lasts as long as the window, so this call is bounded
                # by nothing: the default timeout would detach a window that had
                # merely been open for a while.
                timeout=None,
            )
        except ToolError:
            # The plugin answered and refused — an agent it cannot place, a
            # version mismatch.  The connection is fine, so keep it.
            raise
        except Exception:
            # The transport is gone.  Dropping the client is what lets the app's
            # retry reconnect, for `run_turn`'s reason.
            await self.close()
            raise


class MCPMeshClient:
    """A :class:`MeshClient` backed by an MCP connection to the A2A plugin.

    **Held rather than opened per call**, which is the opposite of
    `slife2.handover`'s way and deliberately so: a hand-over happens once per
    message and a connection that dropped costs one retry, where being *on* the
    mesh is a state other people can see — an agent that reconnected per message
    would be watched coming and going by every peer on it.

    What the mesh does with the agent name is the a2a plugin's business, and none
    of it is here: this class knows where the door is and how long to hold it.
    """

    def __init__(self, url: str, *, agent: str = DEFAULT_AGENT):
        self._url = url
        #: Whose mesh this window is responsible for.  The name *is* the mesh
        #: identity (`slife2.a2a.mesh`), so this is not a label: it is the MQTT
        #: client id and the last segment of the request topic a peer will use.
        self._agent = agent
        self._client: Client | None = None

    async def connect(self) -> None:
        """Open the connection, or raise with a message worth showing.

        The named-peer check is the point here too: an `a2a:` address pointed at
        something else — a stale port, another plugin — fails by name rather than
        as a window whose agent silently never appears on the mesh
        (`slife2.mcp_server.identifies`).
        """
        if self._client is not None:
            return
        self._client = await open_server(
            self._url, name=A2A_SERVER_NAME, fallback_tool=JOIN
        )

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await close_server(client)

    async def hold(self) -> None:
        if self._client is None:
            raise ConnectionError("not connected")

        async def on_progress(
            progress: float, total: float | None, message: str | None
        ) -> None:
            # **Nothing to do, and that is not the same as nothing to have.**
            # What arrives here is the plugin's keepalive — the write that makes
            # a dead window notice-able at the other end
            # (`slife2.a2a.server._hold_open`) — and receiving it means the mesh
            # still counts this window as here.  A caller that sent *no* progress
            # token would get no keepalive at all, because `report_progress` is
            # then a silent no-op, and its agent would stay online until
            # something else noticed; sending this handler is what makes the
            # departure a fact rather than a hope.
            return

        try:
            await self._client.call_tool(
                JOIN,
                {"agent": self._agent},
                progress_handler=on_progress,
                # A mesh membership lasts as long as the window, so this call is
                # bounded by nothing: the default timeout would take an agent off
                # the mesh that had merely been on it for a while.
                timeout=None,
            )
        except ToolError:
            # The mesh answered and refused — the name is taken, or the broker is
            # not there.  The connection is fine, so keep it: the caller decides
            # whether to try again, and the sentence it carries is the reason.
            raise
        except Exception:
            # The transport is gone.  Dropping the client is what lets the app's
            # retry reconnect, for `attach`'s reason.
            await self.close()
            raise
