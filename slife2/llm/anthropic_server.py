"""slife2-llm-anthropic — the Messages API, behind MCP.

Same shape as `slife2-llm-openai`: one process holding one API key, exposing one
`stream_chat` tool.  What differs is the adapter, and Anthropic disagrees with
the neutral format in three ways that all have to be handled here:

1. **System prompts are not messages.**  They are a separate top-level
   parameter, so system turns are lifted out of the list.
2. **Tool definitions use `input_schema`,** not `parameters`, and tool calls are
   `tool_use` content blocks rather than a `tool_calls` field on the message.
3. **Roles must alternate.**  Every tool result in a step has to be folded into
   a single `user` turn, and a user text that follows it merged in — strict
   endpoints (Bedrock, Bailian) return 400 on two `user` turns in a row.  OpenAI
   accepts the naive form, so this is the one place the two backends genuinely
   diverge rather than just renaming fields.

Because all of that is translation between the neutral model and one SDK, it
belongs on this side of the wire: the agent loop stays free of it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from slife2.config import AnthropicSettings, load
from slife2.llm.base import Chunk, Finish, ProviderEvent, Streamer, ToolCallDelta
from slife2.llm.server_common import (
    build_llm_server,
    configure_logging,
    parse_serve_args,
    serve,
)
from slife2.messages import Message, ToolSpec, Usage

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-llm-anthropic"

#: This server's key in the config's `servers:` table.
CONFIG_KEY = "llm-anthropic"


def to_anthropic_messages(
    messages: list[Message],
) -> tuple[str, list[dict[str, Any]]]:
    """Convert neutral messages into `(system, messages)`.

    The two return values are separate because Anthropic takes the system prompt
    as its own parameter.  Multiple system turns are joined with a blank line
    rather than dropped — a second system message is a legitimate thing for a
    caller to send, and silently discarding it would be a quiet behaviour
    change.

    Same-role turns are merged into one.  That is what makes a batch of tool
    results followed by user text a single `user` turn instead of three, which
    strict endpoints require.
    """
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []

    def _append(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": list(blocks)})

    for message in messages:
        if message.role == "system":
            if message.content:
                system_parts.append(message.content)
            continue

        if message.role == "tool":
            _append(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id or "",
                        "content": message.content or "",
                    }
                ],
            )
            continue

        if message.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            # An assistant turn with neither text nor calls carries nothing and
            # would be rejected as an empty content list.
            _append("assistant", blocks)
            continue

        _append("user", [{"type": "text", "text": message.content or ""}])

    return "\n\n".join(system_parts), converted


def to_anthropic_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Convert neutral tool definitions to Anthropic's `input_schema` form."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }
        for tool in tools
    ]


def translate(event: Any) -> list[ProviderEvent]:
    """Turn one raw Anthropic stream event into provider events.

    Usage is split across two events and each contributes only its own half:
    `message_start` carries `input_tokens`, `message_delta` carries a
    *cumulative* `output_tokens`.  Summing both events' full usage would double
    count the output — `message_start` already reports `output_tokens: 1` for
    the message it just opened — so each half is emitted as a partial `Usage`
    and the accumulator adds them correctly.

    `thinking_delta` and `signature_delta` are deliberately not translated in
    this cut: extended thinking needs a display decision the TUI has not made
    yet.  They are dropped rather than forwarded as text, because rendering a
    model's private reasoning as its answer would be worse than not showing it.
    """
    events: list[ProviderEvent] = []
    kind = getattr(event, "type", "")

    if kind == "content_block_start":
        block = getattr(event, "content_block", None)
        if getattr(block, "type", "") == "tool_use":
            # The id and name arrive here; the arguments stream afterwards.
            events.append(
                Chunk(
                    tool_call_deltas=(
                        ToolCallDelta(
                            index=int(getattr(event, "index", 0) or 0),
                            id=getattr(block, "id", None),
                            name=getattr(block, "name", None),
                        ),
                    )
                )
            )

    elif kind == "content_block_delta":
        delta = getattr(event, "delta", None)
        delta_type = getattr(delta, "type", "")
        index = int(getattr(event, "index", 0) or 0)
        if delta_type == "text_delta":
            text = getattr(delta, "text", "") or ""
            if text:
                events.append(Chunk(text=text))
        elif delta_type == "input_json_delta":
            events.append(
                Chunk(
                    tool_call_deltas=(
                        ToolCallDelta(
                            index=index,
                            arguments_delta=getattr(delta, "partial_json", "") or "",
                        ),
                    )
                )
            )

    elif kind == "message_start":
        message = getattr(event, "message", None)
        usage = getattr(message, "usage", None)
        if usage is not None:
            events.append(Chunk(usage=Usage(prompt_tokens=_int(usage, "input_tokens"))))
        # A model that opens with text already has a content block; nothing to
        # do here, the deltas carry it.

    elif kind == "message_delta":
        usage = getattr(event, "usage", None)
        if usage is not None:
            events.append(
                Chunk(usage=Usage(completion_tokens=_int(usage, "output_tokens")))
            )
        stop_reason = getattr(getattr(event, "delta", None), "stop_reason", None)
        if stop_reason:
            events.append(Finish(stop_reason=str(stop_reason)))

    return events


def _int(source: Any, attribute: str) -> int:
    """Read an int attribute that may be absent or None."""
    return int(getattr(source, attribute, 0) or 0)


def build_streamer(settings: AnthropicSettings) -> Streamer:
    """Build the provider adapter for one configuration.

    Like the OpenAI one, the SDK client is created on first use so the API key
    is resolved only when a call actually happens.
    """
    client: Any = None

    def _client() -> Any:
        nonlocal client
        if client is None:
            key = settings.api_key
            if not key or key.startswith("${"):
                raise RuntimeError(
                    f"{SERVER_NAME}: the API key did not resolve "
                    f"(config value {settings.api_key_ref!r}). Export it, or "
                    f"store it with `credstore set <NAME>`."
                )
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=key)
        return client

    async def stream(
        messages: list[Message], tools: list[ToolSpec], model: str
    ) -> AsyncIterator[ProviderEvent]:
        system, converted = to_anthropic_messages(messages)
        request: dict[str, Any] = {
            "model": model,
            "messages": converted,
            "max_tokens": settings.max_tokens,
            "stream": True,
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = to_anthropic_tools(tools)

        response = await _client().messages.create(**request)
        async for event in response:
            for out in translate(event):
                yield out

    return stream


def build_server(settings: AnthropicSettings, *, streamer: Streamer | None = None):
    """Build the MCP server.  `streamer` is injectable for tests."""
    return build_llm_server(
        name=SERVER_NAME,
        streamer=streamer if streamer is not None else build_streamer(settings),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_serve_args(argv, SERVER_NAME)
    configure_logging()
    config = load(args.config)
    settings = config.llm_anthropic
    address = config.server(CONFIG_KEY)
    logger.info(
        "serving %s on http://%s:%d%s",
        SERVER_NAME,
        args.host or address.host,
        args.port or address.port,
        address.path,
    )
    serve(build_server(settings), address, args)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
