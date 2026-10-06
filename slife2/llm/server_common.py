"""Scaffolding the two LLM MCP servers share.

Both servers do the same job — expose one `stream_chat` tool, push provider
output down the progress channel, return the assembled message — and differ only
in which SDK they call.  This module holds everything except that difference.

It is also where **tool-call fragments are reassembled**, and that placement is
deliberate.  Every provider streams tool arguments as JSON text split at
arbitrary boundaries, and each indexes those fragments differently (OpenAI
repeats `tool_calls[].index`; Anthropic uses a content-block index).  Doing the
fold here means it happens once for both providers, and — more importantly —
that nothing downstream of this hop ever sees a fragment.  The agent loop
receives complete `ToolCall` objects and needs no accumulator of its own.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Context, FastMCP

from slife2.config import ServerSettings
from slife2.llm.base import Chunk, Finish, Streamer, ToolCallDelta
from slife2.llm.wire import encode_chunk
from slife2.messages import Message, StreamChatResult, ToolCall, ToolSpec, Usage

logger = logging.getLogger(__name__)


@dataclass
class _PartialCall:
    """A tool call being assembled from fragments."""

    id: str = ""
    name: str = ""
    arguments: str = ""


class ToolCallAccumulator:
    """Folds tool-call fragments into complete calls.

    Fragments arrive out of order relative to each other and interleaved with
    text, so they are keyed by index and only assembled at the end.
    """

    def __init__(self) -> None:
        self._parts: dict[int, _PartialCall] = {}

    def add(self, delta: ToolCallDelta) -> None:
        """Absorb one fragment.

        `id` and `name` are typically sent only on the first fragment for an
        index, so they are kept rather than overwritten — a later fragment
        carrying neither must not erase them.  A provider that repeats them
        every fragment is harmless: the value is the same.
        """
        part = self._parts.setdefault(delta.index, _PartialCall())
        if delta.id:
            part.id = delta.id
        if delta.name:
            part.name = delta.name
        part.arguments += delta.arguments_delta

    def complete(self) -> list[ToolCall]:
        """Assemble the calls, ordered by index.

        A call whose arguments did not parse becomes an empty argument dict
        rather than an error: the tool then reports the problem to the model,
        which is a feedback loop the model can act on, where an exception here
        would just end the turn.
        """
        calls = []
        for index in sorted(self._parts):
            part = self._parts[index]
            calls.append(
                ToolCall(
                    # The id must exist: the tool result message refers back to
                    # it, and a provider that never sent one leaves nothing to
                    # refer to.  A synthetic id keeps the pairing valid.
                    id=part.id or f"call_{index}",
                    name=part.name,
                    arguments=_parse_arguments(part.arguments),
                )
            )
        return calls


def _parse_arguments(raw: str) -> dict[str, Any]:
    """Parse a reassembled arguments string, degrading to empty on bad JSON."""
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("tool call arguments did not parse as JSON: %.200s", raw)
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass
class _TurnBuilder:
    """Accumulates provider events into the complete assistant message."""

    text_parts: list[str] = field(default_factory=list)
    calls: ToolCallAccumulator = field(default_factory=ToolCallAccumulator)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""

    def absorb(self, event: Chunk | Finish) -> Chunk | None:
        """Fold one event in, returning the chunk to forward (or None).

        A `Finish` is bookkeeping and is swallowed here — it never reaches the
        progress stream, because the agent loop learns the stop reason from the
        returned `StreamChatResult` instead.
        """
        if isinstance(event, Finish):
            self.stop_reason = event.stop_reason
            return None

        if event.text:
            self.text_parts.append(event.text)
        for delta in event.tool_call_deltas:
            self.calls.add(delta)
        if event.usage is not None:
            self.usage = self.usage + event.usage
        return event

    def build(self) -> StreamChatResult:
        return StreamChatResult(
            text="".join(self.text_parts),
            tool_calls=tuple(self.calls.complete()),
            usage=self.usage,
            stop_reason=self.stop_reason,
        )


async def stream_chat_impl(
    streamer: Streamer,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
    ctx: Context,
) -> dict[str, Any]:
    """Run one streamed model call and return the assembled result."""
    builder = _TurnBuilder()
    reported = 0

    async for event in streamer(
        [Message.from_wire(m) for m in messages],
        [ToolSpec.from_wire(t) for t in tools],
        model,
    ):
        chunk = builder.absorb(event)
        if chunk is None:
            continue
        reported += 1
        # A no-op when the client sent no progress token, which is the intended
        # behaviour: a caller that does not want a stream should not pay for
        # one, and gets the same result at the end.  See `slife2.events`.
        await ctx.report_progress(reported, None, encode_chunk(chunk))

    return builder.build().to_wire()


def build_llm_server(*, name: str, streamer: Streamer) -> FastMCP:
    """Build the FastMCP server exposing `stream_chat` over `streamer`.

    The streamer is injected rather than built here so tests can drive the whole
    server through the in-memory transport with a scripted provider — no API
    key, no network, no mocking library.
    """
    mcp: FastMCP = FastMCP(
        name,
        # Provider errors (a rejected key, a model that does not exist) are the
        # single most likely thing a user has to debug here, and FastMCP's
        # default is to replace them with a generic message.  These servers
        # listen on loopback and serve their own operator, so the detail is
        # worth more than the tidiness.
        mask_error_details=False,
    )

    @mcp.tool
    async def stream_chat(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Stream one chat completion.

        Provider output arrives as `notifications/progress` on the calling
        request, one notification per chunk.  The return value is the complete
        assistant message and is authoritative: a client that ignores the
        progress stream still gets the whole answer.

        Args:
            messages: OpenAI-shaped chat messages.
            tools: OpenAI-shaped tool definitions; empty for no tools.
            model: The provider's model id.
        """
        return await stream_chat_impl(streamer, messages, tools, model, ctx)

    return mcp


def parse_serve_args(argv: list[str] | None, description: str) -> argparse.Namespace:
    """The flags every server here accepts."""
    parser = argparse.ArgumentParser(prog=description, description=description)
    parser.add_argument("--config", default=None, help="path to slife2.yaml")
    parser.add_argument("--host", default=None, help="override the listen address")
    parser.add_argument("--port", default=None, type=int, help="override the port")
    parser.add_argument(
        "--provider",
        default=None,
        help=(
            "which configured provider this model server serves. Required by "
            "the model servers: each holds exactly one provider's credentials."
        ),
    )
    return parser.parse_args(argv)


def serve(mcp: FastMCP, server: ServerSettings, args: argparse.Namespace) -> None:
    """Run a server over streamable HTTP.

    Two options are pinned explicitly even where they match the default, because
    flipping either is *silent* and a future refactor could do it without
    noticing:

    * ``json_response=False`` — with it True the response body is buffered and
      returned as one JSON document.  Progress notifications cannot be
      interleaved into a buffered body, so every stream in this system would
      stop streaming while still returning correct results.
    * ``stateless_http=True`` — every server here is stateless by design.  The
      agent's conversation memory lives in the caller, so nothing depends on a
      session surviving between requests.  See `slife2.server.server`.
    """
    mcp.run(
        transport="http",
        host=args.host or server.host,
        port=args.port or server.port,
        path=server.path,
        json_response=False,
        stateless_http=True,
    )


def configure_logging() -> None:
    """Log to stderr, never stdout.

    stdout is not free here: on a stdio transport it *is* the protocol channel,
    and even over HTTP the house rule is that model output must never be
    printed to a console whose codepage may not be UTF-8 — a `UnicodeEncodeError`
    mid-turn is a worse failure than a log line nobody reads.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
