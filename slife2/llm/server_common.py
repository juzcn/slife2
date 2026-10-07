"""What the two LLM MCP servers share.

Both servers do the same job — expose one `stream_chat` tool, push provider
output down the progress channel, return the assembled message — and differ only
in which SDK they call.  This module holds everything except that difference.

Serving a server at all — the flags, the HTTP transport, the record that says a
daemon is here — is *not* here, because it is not an LLM concern: it lives in
`slife2.mcp_server`, alongside the other things every server in this system
agrees about.

It is also where **tool-call fragments are reassembled**, and that placement is
deliberate.  Every provider streams tool arguments as JSON text split at
arbitrary boundaries, and each indexes those fragments differently (OpenAI
repeats `tool_calls[].index`; Anthropic uses a content-block index).  Doing the
fold here means it happens once for both providers, and — more importantly —
that nothing downstream of this hop ever sees a fragment.  The agent loop
receives complete `ToolCall` objects and needs no accumulator of its own.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Context, FastMCP

from slife2.config import ProviderSettings, find_config_path, load
from slife2.llm.base import Chunk, Finish, Streamer, ToolCallDelta
from slife2.llm.wire import encode_chunk
from slife2.mcp_server import (
    configure_logging,
    house_server,
    parse_serve_args,
    serve,
)
from slife2.messages import Message, StreamChatResult, ToolCall, ToolSpec, Usage

logger = logging.getLogger(__name__)

#: What a model backend tells a caller that reads its instructions.
#:
#: The two things a caller cannot work out from the tool signature are what
#: `provider` selects and that the result — not the notifications — is
#: authoritative.  Nothing here restates a tool description; that is the spec's
#: guidance for this field, and it is also just true.
INSTRUCTIONS = (
    "A model backend. Call `stream_chat` with a configured provider and a model "
    "id; provider output arrives as progress notifications on the same request, "
    "and the result carries the complete assistant message. The result is "
    "authoritative — a caller that ignores the progress stream still gets the "
    "whole answer. This server keeps no state between calls."
)


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
    provider: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
    ctx: Context,
) -> dict[str, Any]:
    """Run one streamed model call and return the assembled result."""
    builder = _TurnBuilder()
    reported = 0

    async for event in streamer(
        provider,
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
    mcp = house_server(name, instructions=INSTRUCTIONS)

    @mcp.tool
    async def stream_chat(
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        ctx: Context,
    ) -> dict[str, Any]:
        """Stream one chat completion.

        Provider output arrives as `notifications/progress` on the calling
        request, one notification per chunk.  The return value is the complete
        assistant message and is authoritative: a client that ignores the
        progress stream still gets the whole answer.

        Args:
            provider: Which configured provider to call.  One process serves
                every provider that speaks its wire format, and this says whose
                credentials and model list to use.
            model: The model id, as that provider names it.
            messages: OpenAI-shaped chat messages.
            tools: OpenAI-shaped tool definitions; empty for no tools.
        """
        return await stream_chat_impl(streamer, provider, messages, tools, model, ctx)

    return mcp


def serve_backend(
    argv: list[str] | None,
    *,
    api: str,
    server_name: str,
    build: Callable[[dict[str, ProviderSettings]], FastMCP],
    logger: logging.Logger,
) -> int:
    """The `main` both model servers share.

    One process per wire protocol, so the only thing that genuinely differs
    between the two is which protocol they serve and which SDK builds the
    streamer.  Everything else here was byte-identical in both, which is the
    kind of duplication that drifts one branch at a time.

    A config with no provider for this protocol stops here, with a one-line
    message and exit code 2, rather than starting a server that can answer
    nothing.

    `logger` is the caller's, so a line still lands under the name of the server
    that wrote it rather than under this module's.
    """
    args = parse_serve_args(argv, server_name)
    configure_logging()
    config_path = find_config_path()
    config = load()

    providers = {
        name: provider
        for name, provider in config.providers.items()
        if provider.api == api
    }
    if not providers:
        print(f"{server_name}: this config has no {api} provider")
        return 2

    address = config.server(api)
    logger.info(
        "serving %s for %s on http://%s:%d%s (providers: %s)",
        server_name,
        api,
        args.host or address.host,
        args.port or address.port,
        address.path,
        ", ".join(sorted(providers)),
    )
    serve(build(providers), address, args, name=server_name, config_path=config_path)
    return 0
