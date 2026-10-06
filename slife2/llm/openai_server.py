"""slife2-llm-openai — one provider's chat completions, behind MCP.

One process per provider, because a process can only hold one base_url and one
key.  It is started as `slife2-llm-openai --provider deepseek` and reads that
provider's credentials and models from the config; it holds no state between
calls and knows nothing about the others.

"OpenAI-compatible" is doing real work in that sentence.  The same code serves
OpenAI, DeepSeek, Ollama, vLLM, scnet, and most gateways, because they all speak
the chat-completions wire format — which is exactly why that format was chosen
as the neutral one in `slife2.messages`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from slife2.config import (
    ConfigError,
    ModelSettings,
    ProviderSettings,
    find_config_path,
    load,
)
from slife2.llm.base import Chunk, Finish, ProviderEvent, Streamer, ToolCallDelta
from slife2.llm.server_common import (
    build_llm_server,
    configure_logging,
    parse_serve_args,
    serve,
)
from slife2.messages import Message, ToolSpec, Usage

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-llm-openai"

#: How much of the output budget a thinking model may spend reasoning, when
#: `compat.thinking: enabled` asks for it.  Half, because the other half is what
#: the answer is made of and a model that spends everything thinking returns
#: nothing.
THINKING_BUDGET_SHARE = 0.5


def thinking_parameter(settings: ModelSettings) -> dict[str, Any] | None:
    """The `thinking` field to send, or None to send none.

    The OpenAI-compatible wire has no standard thinking parameter, so this is
    conservative on purpose: **an absent `compat.thinking` means send nothing**.
    Gateways differ on whether they accept the field and what shape they want,
    and a 400 on an unknown parameter is a much worse failure than not asking a
    model to think — especially as the ones that reason natively (DeepSeek's
    reasoners among them) do it unasked, and report it in `reasoning_content`,
    which this adapter reads either way.

    Set `compat.thinking: enabled` to ask explicitly, `disabled` to forbid it,
    and `omit` to say so out loud.
    """
    match settings.thinking:
        case "enabled":
            budget = int((settings.max_tokens or 8192) * THINKING_BUDGET_SHARE)
            return {"type": "enabled", "budget_tokens": budget}
        case "disabled":
            return {"type": "disabled"}
        case _:
            return None


def build_request(
    messages: list[Message],
    tools: list[ToolSpec],
    settings: ModelSettings,
    *,
    stream_usage: bool,
) -> dict[str, Any]:
    """The request body, containing only what the config asked for.

    Separated from the call so the whole parameter surface can be asserted on
    without a network: which fields are present matters as much as their values,
    and "absent" is a real choice here rather than an oversight.
    """
    request: dict[str, Any] = {
        "model": settings.model,
        "messages": [m.to_wire() for m in messages],
        "stream": True,
    }
    if tools:
        request["tools"] = [t.to_wire() for t in tools]
    if stream_usage:
        # An OpenAI extension.  Some compatible servers reject it with a 400;
        # the config has a switch for those.
        request["stream_options"] = {"include_usage": True}

    # Only what was configured.  A gateway that rejects a temperature it did not
    # ask for is a real thing, so `None` means "say nothing", not "use 0.7".
    if settings.temperature is not None:
        request["temperature"] = settings.temperature
    if settings.top_p is not None:
        request["top_p"] = settings.top_p
    if settings.max_tokens is not None:
        request["max_tokens"] = settings.max_tokens

    thinking = thinking_parameter(settings)
    if thinking is not None:
        request["thinking"] = thinking
    return request


def translate(event: Any) -> list[ProviderEvent]:
    """Turn one SDK stream chunk into provider events.

    Pure, and takes `Any` rather than the SDK's chunk type on purpose: it reads
    only the handful of attributes it needs, which keeps it testable against
    hand-built objects while still working with the real ones.

    Returns a *list* because one chunk can carry several things at once: the
    last content chunk usually arrives together with the finish reason, and
    usage arrives on a chunk of its own.
    """
    events: list[ProviderEvent] = []

    # Usage is read *before* the choices branch, and the ordering is the point.
    # OpenAI sends token counts on a trailing chunk whose `choices` list is
    # empty; DeepSeek attaches them to the final chunk that still carries a
    # choice.  Reading usage only in the empty-choices case silently reports zero
    # tokens against a real DeepSeek endpoint, which is what happened here until
    # a live call caught it.
    usage = _usage(getattr(event, "usage", None))
    if usage is not None:
        events.append(Chunk(usage=usage))

    choices = getattr(event, "choices", None) or []
    if not choices:
        return events

    choice = choices[0]
    delta = getattr(choice, "delta", None)

    thinking = _reasoning(delta)
    if thinking:
        events.append(Chunk(thinking=thinking))

    deltas: list[ToolCallDelta] = []
    for call in getattr(delta, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        deltas.append(
            ToolCallDelta(
                index=getattr(call, "index", 0) or 0,
                # Present only on the first fragment for an index; the
                # accumulator keeps the first non-empty value it sees.
                id=getattr(call, "id", None),
                name=getattr(function, "name", None),
                arguments_delta=getattr(function, "arguments", None) or "",
            )
        )

    text = getattr(delta, "content", None) or ""
    if text or deltas:
        events.append(Chunk(text=text, tool_call_deltas=tuple(deltas)))

    finish = getattr(choice, "finish_reason", None)
    if finish:
        events.append(Finish(stop_reason=str(finish)))

    return events


#: Field names a gateway may put reasoning in.  There is no standard one —
#: `reasoning_content` is DeepSeek's and the most common, but the others are in
#: use, and a gateway whose spelling is not on this list produces reasoning the
#: transcript silently never shows.  Silent is the problem: nothing errors, the
#: answer is correct, and the only symptom is a line that should have been
#: there.
_REASONING_FIELDS = ("reasoning_content", "reasoning", "thinking")


def _reasoning(delta: Any) -> str:
    """Read whatever reasoning field this gateway used, if any.

    Also looks in the model's `extra` fields, because an SDK that does not know
    a name puts it there rather than dropping it — which is how a field that IS
    present ends up invisible to `getattr`.
    """
    for field in _REASONING_FIELDS:
        value = getattr(delta, field, None)
        if isinstance(value, str) and value:
            return value

    extra = getattr(delta, "model_extra", None)
    if isinstance(extra, dict):
        for field in _REASONING_FIELDS:
            value = extra.get(field)
            if isinstance(value, str) and value:
                return value
    return ""


def _usage(raw: Any) -> Usage | None:
    """Read token counts off a usage object, or None when there is not one."""
    if raw is None:
        return None
    return Usage(
        prompt_tokens=int(getattr(raw, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
    )


def build_streamer(
    provider: ProviderSettings, *, stream_usage: bool = True
) -> Streamer:
    """The adapter for one provider.

    The SDK client is created on first use, not here: the API key is resolved at
    that moment, so a server that never receives a call never opens the OS
    keyring, and constructing a client outside a running event loop is not
    always safe.
    """
    client: Any = None

    def _client() -> Any:
        nonlocal client
        if client is None:
            key = provider.api_key
            if not key or key.startswith("${"):
                raise RuntimeError(
                    f"{SERVER_NAME}: the API key for provider "
                    f"{provider.base_url!r} did not resolve "
                    f"(config value {provider.api_key_ref!r}). Export it, or "
                    f"store it with `credstore set <NAME>`."
                )
            from openai import AsyncOpenAI

            client = AsyncOpenAI(base_url=provider.base_url, api_key=key)
        return client

    async def stream(
        messages: list[Message], tools: list[ToolSpec], model: str
    ) -> AsyncIterator[ProviderEvent]:
        settings = provider.model(model)
        request = build_request(messages, tools, settings, stream_usage=stream_usage)
        response = await _client().chat.completions.create(**request)
        async for event in response:
            for out in translate(event):
                yield out

    return stream


def build_server(
    provider: ProviderSettings,
    *,
    streamer: Streamer | None = None,
    stream_usage: bool = True,
):
    """Build the MCP server.  `streamer` is injectable for tests."""
    return build_llm_server(
        name=SERVER_NAME,
        streamer=(
            streamer
            if streamer is not None
            else build_streamer(provider, stream_usage=stream_usage)
        ),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_serve_args(argv, SERVER_NAME)
    configure_logging()
    config_path = find_config_path(args.config)
    config = load(args.config)
    try:
        provider_name, provider = _select(config, args.provider)
    except ConfigError as exc:
        print(f"{SERVER_NAME}: {exc}")
        return 2

    address = provider.server
    logger.info(
        "serving %s for provider %s on http://%s:%d%s",
        SERVER_NAME,
        provider_name,
        args.host or address.host,
        args.port or address.port,
        address.path,
    )
    serve(
        build_server(provider),
        address,
        args,
        name=f"{SERVER_NAME}:{provider_name}",
        config_path=config_path,
    )
    return 0


def _select(config, name: str | None) -> tuple[str, ProviderSettings]:
    """The provider this process serves.

    A model server without one is a config error rather than something to guess
    at: the whole point of the process boundary is that this one holds exactly
    one provider's credentials.
    """
    if not name:
        raise ConfigError(
            "started without --provider; this server serves one provider and "
            f"needs to know which (configured: {', '.join(sorted(config.providers))})"
        )
    provider = config.provider(name)
    if provider.module != "slife2.llm.openai_server":
        raise ConfigError(
            f"provider {name!r} speaks {provider.api!r}, not openai-completions"
        )
    return name, provider


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
