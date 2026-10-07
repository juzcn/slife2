"""Streaming types and the backend protocol the agent loop talks to.

The loop knows exactly one method: :meth:`LLMBackend.stream`.  It has no idea
whether a provider SDK, a subprocess, or an HTTP hop to another machine is
behind it — which is the point of putting the backends behind MCP.

Two things travel back from a model call, and they are not the same thing:

* **chunks**, streamed for display — text fragments and tool-call fragments, in
  the order the provider produced them;
* **the result**, a complete assistant message assembled by whoever talked to
  the provider.

:class:`Stream` carries both, and the separation is load-bearing: the chunks can
be dropped, coalesced, or delayed by a transport without the conversation ever
noticing, because the loop builds its message from the result.  See
`slife2.messages.StreamChatResult`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import Protocol

from slife2.messages import Message, StreamChatResult, ToolSpec, Usage


@dataclass(frozen=True)
class ToolCallDelta:
    """One provider fragment of a tool call.

    Every provider streams tool arguments as JSON *text* split at arbitrary
    boundaries, so a fragment is only meaningful once reassembled.  Providers
    disagree on how they index those fragments — OpenAI repeats
    ``tool_calls[].index``, Anthropic uses a content-block index — so both are
    normalised into this one shape by the LLM server, and the reassembly happens
    there.  Nothing downstream of that hop sees a fragment.
    """

    index: int
    id: str | None = None
    name: str | None = None
    #: A slice of the JSON arguments string, not necessarily valid JSON alone.
    arguments_delta: str = ""


@dataclass(frozen=True)
class Chunk:
    """One increment of a model response, for display."""

    text: str = ""
    #: The model's reasoning, for the models that report it.  A separate field
    #: rather than folded into `text` because the two are displayed differently
    #: — reasoning is collapsed by default — and because a caller that does not
    #: want to pay for thinking must be able to ignore it wholesale.
    thinking: str = ""
    tool_call_deltas: tuple[ToolCallDelta, ...] = ()
    usage: Usage | None = None


@dataclass(frozen=True)
class Finish:
    """The provider signalled the end of its response.

    Kept out of :class:`Chunk` on purpose.  A chunk is *content* and crosses the
    progress wire to the TUI; a finish reason is provider bookkeeping that only
    the LLM server needs, and it travels onward inside
    :class:`~slife2.messages.StreamChatResult`.  Folding it into `Chunk` would
    mean a field that the wire deliberately drops, which is exactly the
    round-trip asymmetry that causes bugs later.
    """

    stop_reason: str = ""


#: What a provider adapter yields: content, interleaved with a final marker.
ProviderEvent = Chunk | Finish


class Streamer(Protocol):
    """A provider adapter: neutral messages in, provider events out.

    Implemented once per wire protocol — in `openai_server`,
    `openai_responses_server` and `anthropic_server`.  It is a plain callable
    returning an async iterator, so a test can substitute a scripted one
    without any of the SDK being involved.
    """

    def __call__(
        self,
        provider: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
    ) -> AsyncIterator[ProviderEvent]: ...


@dataclass
class Stream:
    """An in-flight model call: iterate the chunks, then await the result.

    `result` is an awaitable rather than a plain attribute because the value
    does not exist until the call finishes.  Awaiting it before iteration ends
    is allowed and simply waits; the loop does it after the `async for`, which
    is the natural order.

    Both fields are required.  A Stream with no backend behind it has no useful
    meaning, so there is no empty default to construct by accident.
    """

    #: Text and tool-call fragments, in provider order.
    chunks: AsyncIterator[Chunk]
    #: The complete assistant message.  Authoritative over `chunks`.
    result: Awaitable[StreamChatResult]


class LLMBackend(Protocol):
    """A way to call a model.

    `stream` is a plain method returning a :class:`Stream`, not an `async def`
    returning an async iterator.  Two reasons: it matches what the provider SDKs
    naturally offer (a sync call that hands back a stream, with the first await
    happening on iteration), and it lets the caller reach `result` before the
    stream is exhausted.
    """

    name: str

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Stream: ...
