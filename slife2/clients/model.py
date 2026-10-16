"""The agent loop's only backend: an MCP client pointed at an LLM server.

This is the adapter that makes the architecture work.  `stream_chat` is an
ordinary tool call — it returns once, at the end — but progress notifications
arrive *during* it, so the two have to be recombined into the async iterator the
loop expects.  A queue does that: the progress callback pushes chunks as they
arrive, and the iterator drains them until the call finishes.

Everything above this file is unchanged by the fact that the model now lives in
another process, which is the point.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastmcp import Client

import slife2.helpers.timeouts as _T
from slife2.libs.model.base import Chunk, Stream
from slife2.libs.model.wire import decode_chunk
from slife2.libs.plugin.protocol import open_server, unreadable
from slife2.libs.turn.messages import Message, StreamChatResult, ToolSpec

logger = logging.getLogger(__name__)

#: Pushed into the queue when the tool call finishes, so the drain loop knows to
#: stop.  A distinct sentinel rather than `None`, because `None` is a legal
#: queue value and using it would make "finished" and "empty chunk"
#: indistinguishable.
_DONE = object()


class StreamStallError(TimeoutError):
    """A model call produced no chunk for `_T.timeouts.work.stall`.

    A `TimeoutError` on purpose: it is the class a caller already catches for
    "this took too long", and a stall is the one way a model call takes too
    long — none of the others end the call.  The message names the silence,
    because a reader of a log needs to know it was the *provider* that stopped
    talking rather than anything this process did.
    """

    def __init__(self, seconds: float) -> None:
        super().__init__(f"LLM stream stalled — no data for {seconds:g}s")


class MCPBackend:
    """An :class:`~slife2.libs.model.base.LLMBackend` backed by an MCP server.

    Switching providers is constructing this with a different client and model —
    the loop cannot tell the difference, and no provider SDK is importable from
    this process.
    """

    def __init__(
        self,
        client: Client,
        model: str,
        *,
        provider: str = "",
        name: str = "mcp",
        timeout: float | None = None,
        key: tuple[str, str] = ("", ""),
    ) -> None:
        self.name = name
        self._client = client
        self._model = model
        #: Which configured provider to ask for.  One model server speaks one
        #: wire format for every provider that uses it, so this is what picks
        #: whose credentials and model list the call uses.
        self._provider = provider
        self._timeout = timeout
        #: Whose conversation this call is being made for — the same
        #: `(agent, subagent)` every other server in this system keys its own
        #: state by.  It is not state here and nothing is keyed on it: it travels
        #: with the call so a model server can say *whose* call it served, in its
        #: log and in anything that later wants to account for one agent's usage.
        self._key = key

    def with_key(self, agent: str, subagent: str) -> MCPBackend:
        """A view of this backend that speaks for one conversation.

        The connection is shared — one per model server, however many
        conversations are using it — so this is a shallow copy carrying a
        different key rather than a second backend.  That is what lets the agent
        server cache by model while still naming a conversation on every call.
        """
        return MCPBackend(
            self._client,
            self._model,
            provider=self._provider,
            name=self.name,
            timeout=self._timeout,
            key=(agent, subagent),
        )

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Stream:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        task = asyncio.create_task(self._call(messages, tools, queue))
        return Stream(chunks=self._drain(queue, task), result=self._resolve(task))

    async def _call(
        self, messages: list[Message], tools: list[ToolSpec], queue: asyncio.Queue[Any]
    ) -> StreamChatResult:
        """Make the call, pushing decoded chunks into `queue` as they arrive.

        The `finally` is what keeps the drain loop from hanging forever when the
        call fails: without the sentinel, an exception here would leave the
        iterator waiting on a queue nobody will ever push to again.
        """

        async def on_progress(
            progress: float, total: float | None, message: str | None
        ) -> None:
            chunk = decode_chunk(message or "")
            if chunk is not None:
                queue.put_nowait(chunk)

        try:
            result = await self._client.call_tool(
                "stream_chat",
                {
                    "provider": self._provider,
                    "model": self._model,
                    "agent": self._key[0],
                    "subagent": self._key[1],
                    "messages": [m.to_wire() for m in messages],
                    "tools": [t.to_wire() for t in tools],
                },
                progress_handler=on_progress,
                timeout=self._timeout,
            )
            # **An answer this build cannot read is refused, not read as an
            # empty one.**  `from_wire` tolerates a missing *field* (a server
            # from before that field existed sends no `thinking`, and no
            # `thinking` is the truth about a model that reasoned nothing), but
            # a missing *result* is a different fact: FastMCP leaves
            # `CallToolResult.data` as None whenever the far end sent no
            # structured content, and `from_wire(None)` builds an assistant
            # message with no text and no tool calls.  The turn then ends
            # "successfully" with an empty answer and the model's real reply is
            # discarded — the confident negative DESIGN.md §10.1 forbids, on the
            # one hop that does not go through `tool_payload`.  The likely cause
            # is a model server from another build, so the sentence says so.
            if not isinstance(result.data, dict):
                raise unreadable(
                    "the model server", "return a stream_chat result", result.data
                )
            return StreamChatResult.from_wire(result.data)
        finally:
            queue.put_nowait(_DONE)

    async def _drain(
        self, queue: asyncio.Queue[Any], task: asyncio.Task[StreamChatResult]
    ) -> AsyncIterator[Chunk]:
        """Yield chunks until the call signals it is done, watching for silence.

        The deadline sits on the *wait for one chunk* and is restarted by every
        chunk that arrives, which is what makes it a stall detector rather than
        a clock: a model that streams for an hour is fine, and a provider that
        goes quiet for `_T.timeouts.work.stall` is indistinguishable from one
        that has hung — a silent provider and a dead one look exactly alike, and
        the silence is the only symptom either shows (see `StreamStallError`).
        """
        try:
            while True:
                try:
                    async with asyncio.timeout(_T.timeouts.work.stall):
                        item = await queue.get()
                except TimeoutError:
                    raise StreamStallError(_T.timeouts.work.stall) from None
                if item is _DONE:
                    return
                yield item
        finally:
            # Reached on normal completion, on failure, or when the turn is
            # cancelled mid-stream.  Cancelling a finished task is a no-op.
            if not task.done():
                task.cancel()
            # A turn abandoned by cancellation is one nobody will await, and an
            # exception retrieved by nobody is reported later as "never
            # retrieved" against whatever code happens to be running.  Retrieving
            # it here does not stop a caller that *does* await from seeing it --
            # awaiting a task re-raises every time.
            task.add_done_callback(_consume)

    @staticmethod
    async def _resolve(task: asyncio.Task[StreamChatResult]) -> StreamChatResult:
        """The authoritative result, re-raising whatever the call raised."""
        return await task


def _consume(task: asyncio.Task[StreamChatResult]) -> None:
    """Retrieve a finished task's exception so it is not reported as unhandled."""
    if task.cancelled():
        return
    with contextlib.suppress(Exception):
        task.exception()


async def open_backend(
    url: str,
    model: str,
    *,
    provider: str = "",
    name: str = "mcp",
    server_name: str = "",
    timeout: float | None = None,
) -> tuple[Client, MCPBackend]:
    """Connect to an LLM server and return `(client, backend)`.

    The client is returned alongside so the caller owns its lifetime — the agent
    server opens it once in its lifespan and keeps it for the process, because
    reconnecting per turn would pay a handshake per step.

    `server_name` is the MCP name the server should be advertising.  Empty skips
    the identity check, which is what a caller that does not know it — a test,
    or a server reached by a URL nobody configured — wants.

    Connecting, probing and the policy on failure are all
    `slife2.libs.plugin.protocol.open_server`'s; what is left here is the LLM half, which
    is wrapping the client in the backend that speaks this wire format.

    `timeout` is None by default, which means **no clock on one model call** —
    the bound is the stall watchdog in the drain (`_T.timeouts.work.stall`), and a total
    would be the thing that cuts a slow-but-live answer.  A caller that wants one
    anyway says so here; nothing in this build does.
    """
    client = await open_server(
        url, name=server_name, fallback_tool="stream_chat", timeout=timeout
    )
    return client, MCPBackend(
        client, model, provider=provider, name=name, timeout=timeout
    )
