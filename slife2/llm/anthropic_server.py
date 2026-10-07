"""slife2-llm-anthropic — every Anthropic Messages provider, behind MCP.

**One process per wire protocol, not per provider.**  This process serves every
provider in the config whose `api` is `anthropic-messages`, and
`stream_chat(provider=...)` says whose credentials and model list a given call
uses.

What differs from the OpenAI-compatible adapter is the wire format, and the
Messages API disagrees with the neutral model in three ways that all have to be
handled here:

1. **System prompts are not messages.**  They are a separate top-level
   parameter, so system turns are lifted out of the list.
2. **Tool definitions use `input_schema`,** not `parameters`, and tool calls are
   `tool_use` content blocks rather than a `tool_calls` field on the message.
3. **Roles must alternate.**  Every tool result in a step has to be folded into
   a single `user` turn, and a user text that follows it merged in — strict
   endpoints (Bedrock, Bailian) return 400 on two `user` turns in a row.

Because all of that is translation between the neutral model and one SDK, it
belongs on this side of the wire: the agent loop stays free of it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from slife2.config import ModelSettings, ProviderSettings
from slife2.llm.base import Chunk, Finish, ProviderEvent, Streamer, ToolCallDelta
from slife2.llm.server_common import build_llm_server, serve_backend
from slife2.messages import Message, ToolSpec, Usage

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-llm-anthropic"

#: The wire protocol this process speaks — and the key its address lives under.
API = "anthropic-messages"

#: Output cap when a model entry does not set one.  Anthropic requires the
#: field, so "absent" cannot mean "omit" here the way it does for sampling.
DEFAULT_MAX_TOKENS = 4096

#: How much of the output budget a thinking model may spend reasoning.  Half,
#: because the other half is what the answer is made of.
THINKING_BUDGET_SHARE = 0.5


def to_anthropic_blocks(content: Any) -> list[dict[str, Any]]:
    """Neutral content — a string or OpenAI content parts — as Anthropic blocks.

    The two APIs disagree about images in the usual way: OpenAI nests a data URL
    under `image_url`, Anthropic wants the media type and the base64 payload as
    separate fields.  A part this function does not recognise is dropped rather
    than passed through, because passing an unknown block to the Messages API is
    a 400 on every call that contains one.
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []

    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        match part.get("type"):
            case "text":
                text = part.get("text") or ""
                if text:
                    blocks.append({"type": "text", "text": text})
            case "image_url":
                source = _image_source(part.get("image_url"))
                if source is not None:
                    blocks.append({"type": "image", "source": source})
    return blocks


def _image_source(raw: Any) -> dict[str, Any] | None:
    """A `data:` URL as Anthropic's base64 source, or None if it is not one.

    Only inline data is accepted.  A remote URL would have to be fetched, and
    fetching a URL a model or a user named is a capability this component has no
    business having — so it is refused rather than quietly turned into a request
    somebody did not make.
    """
    url = (raw or {}).get("url") if isinstance(raw, dict) else None
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, _, data = url.partition(",")
    media_type = header[5:].split(";", 1)[0] or "application/octet-stream"
    if not data:
        return None
    return {"type": "base64", "media_type": media_type, "data": data}


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
            # The system prompt is a string parameter here, and a system message
            # carrying content parts is not a thing either API supports — so a
            # list is taken for the text in it and nothing else.
            if isinstance(message.content, str) and message.content:
                system_parts.append(message.content)
            continue

        if message.role == "tool":
            _append(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id or "",
                        # Tool results are text; anything else would have to be
                        # rendered, and a result that is not a string is a bug
                        # upstream rather than something to guess at here.
                        "content": message.content
                        if isinstance(message.content, str)
                        else "",
                    }
                ],
            )
            continue

        if message.role == "assistant":
            blocks = to_anthropic_blocks(message.content)
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

        _append("user", to_anthropic_blocks(message.content))

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


def thinking_parameter(
    settings: ModelSettings, max_tokens: int
) -> dict[str, Any] | None:
    """The `thinking` block to send, or None to send none.

    Unlike the OpenAI-compatible wire, this parameter is part of the protocol, so
    `reasoning: true` is enough to ask for it — a model that reasons natively is
    not going to do it unless told, and there is no ambiguity about the shape.

    `compat.thinking: omit` or `disabled` suppress it, for a gateway that
    rejects the field while reasoning anyway.
    """
    if not settings.reasoning or settings.thinking in ("omit", "disabled"):
        return None
    budget = max(1024, int(max_tokens * THINKING_BUDGET_SHARE))
    return {"type": "enabled", "budget_tokens": budget}


def build_request(
    messages: list[Message],
    tools: list[ToolSpec],
    settings: ModelSettings,
) -> dict[str, Any]:
    """The request body, containing only what the config asked for."""
    system, converted = to_anthropic_messages(messages)
    max_tokens = settings.max_tokens or DEFAULT_MAX_TOKENS

    request: dict[str, Any] = {
        "model": settings.model,
        "messages": converted,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if system:
        request["system"] = system
    if tools:
        request["tools"] = to_anthropic_tools(tools)

    # Only what was configured: `None` means say nothing, not "use a default".
    if settings.temperature is not None:
        request["temperature"] = settings.temperature
    if settings.top_p is not None:
        request["top_p"] = settings.top_p

    thinking = thinking_parameter(settings, max_tokens)
    if thinking is not None:
        request["thinking"] = thinking
        # A thinking budget must leave room for an answer, and this endpoint
        # rejects temperature and top_p alongside extended thinking.
        request.pop("temperature", None)
        request.pop("top_p", None)
    return request


def translate(event: Any) -> list[ProviderEvent]:
    """Turn one raw Anthropic stream event into provider events.

    Usage is split across two events and each contributes only its own half:
    `message_start` carries `input_tokens`, `message_delta` carries a
    *cumulative* `output_tokens`.  Summing both events' full usage would double
    count the output — `message_start` already reports `output_tokens: 1` for
    the message it just opened — so each half is emitted as a partial `Usage`
    and the accumulator adds them correctly.
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
        elif delta_type == "thinking_delta":
            # Reasoning, reported as its own kind rather than folded into the
            # answer — the two are displayed differently and must not mix.
            thinking = getattr(delta, "thinking", "") or ""
            if thinking:
                events.append(Chunk(thinking=thinking))
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


def _make_streamer(providers: dict[str, ProviderSettings]) -> Streamer:
    """The adapter for one process — every provider that speaks this wire.

    The SDK client is created on first use, not here: the key is resolved at
    that moment, so a provider nobody calls never opens the OS keyring, and
    constructing a client outside a running event loop is not always safe.
    """
    clients: dict[str, Any] = {}

    def _provider(name: str) -> ProviderSettings:
        try:
            return providers[name]
        except KeyError:
            known = ", ".join(sorted(providers)) or "(none)"
            raise RuntimeError(
                f"{SERVER_NAME}: no provider {name!r} in this config (known: {known})"
            ) from None

    def _client(name: str) -> Any:
        """The SDK client for one provider, each with its own key."""
        client = clients.get(name)
        if client is not None:
            return client

        provider = _provider(name)
        key = provider.api_key
        if not key or key.startswith("${"):
            raise RuntimeError(
                f"{SERVER_NAME}: the API key for provider {name!r} "
                f"({provider.base_url!r}) did not resolve "
                f"(config value {provider.api_key_ref!r}). Export it, or "
                f"store it with `credstore set <NAME>`."
            )
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=key, base_url=provider.base_url)
        clients[name] = client
        return client

    async def stream(
        provider: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
    ) -> AsyncIterator[ProviderEvent]:
        settings = _provider(provider).model(model)
        request = build_request(messages, tools, settings)
        response = await _client(provider).messages.create(**request)
        async for event in response:
            for out in translate(event):
                yield out

    return stream


def build_server(
    providers: dict[str, ProviderSettings], *, streamer: Streamer | None = None
):
    """Build the MCP server.  `streamer` is injectable for tests."""
    return build_llm_server(
        name=SERVER_NAME,
        streamer=streamer if streamer is not None else _make_streamer(providers),
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `slife2-llm-anthropic` console script."""
    return serve_backend(
        argv, api=API, server_name=SERVER_NAME, build=build_server, logger=logger
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
