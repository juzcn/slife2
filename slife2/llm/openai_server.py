"""slife2-llm-openai — chat completions, behind MCP.

One process, one job: hold an OpenAI-compatible API key and expose a single
`stream_chat` tool over streamable HTTP.  It is the only kind of process in this
system that touches a provider SDK.

"OpenAI-compatible" is doing real work in that sentence.  The same code serves
OpenAI, DeepSeek, Ollama, vLLM, and most gateways, because they all speak the
chat-completions wire format — which is exactly why that format was chosen as
the neutral one in `slife2.messages`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from slife2.config import OpenAISettings, load
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


def translate(event: Any) -> list[ProviderEvent]:
    """Turn one SDK stream chunk into provider events.

    Pure, and takes `Any` rather than the SDK's chunk type on purpose: it reads
    only the handful of attributes it needs, which keeps this testable against
    hand-built objects while still working with the real ones.  (Tests use real
    SDK objects built with `model_validate` anyway — duck typing is for the
    call sites, not an excuse to skip the real shapes.)

    Returns a *list* because one chunk can carry several things at once: the
    last content chunk usually arrives together with the finish reason, and
    OpenAI reports usage on a separate chunk whose `choices` list is empty.
    """
    events: list[ProviderEvent] = []

    # Usage is read *before* the choices branch, and this ordering is the whole
    # point of the method being structured this way.
    #
    # OpenAI sends token counts on their own trailing chunk whose `choices` list
    # is empty.  DeepSeek -- and, empirically, several other compatible servers
    # -- attach them to the final chunk that still carries a choice.  Reading
    # usage only in the empty-choices case therefore silently reports zero
    # tokens against a real DeepSeek endpoint, which is exactly what happened
    # here until a live call caught it.
    usage = _usage(getattr(event, "usage", None))
    if usage is not None:
        events.append(Chunk(usage=usage))

    choices = getattr(event, "choices", None) or []
    if not choices:
        return events

    choice = choices[0]
    delta = getattr(choice, "delta", None)

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


def _usage(raw: Any) -> Usage | None:
    """Read token counts off a usage object, or None when there is not one."""
    if raw is None:
        return None
    return Usage(
        prompt_tokens=int(getattr(raw, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
    )


def build_streamer(settings: OpenAISettings) -> Streamer:
    """Build the provider adapter for one configuration.

    The SDK client is created on first use, not here.  Two reasons: the API key
    is resolved at that moment (see `slife2.config`), so a server that never
    receives a call never opens the OS keyring; and constructing a client
    outside a running event loop is not always safe.
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
            from openai import AsyncOpenAI

            client = AsyncOpenAI(base_url=settings.base_url, api_key=key)
        return client

    async def stream(
        messages: list[Message], tools: list[ToolSpec], model: str
    ) -> AsyncIterator[ProviderEvent]:
        request: dict[str, Any] = {
            "model": model,
            "messages": [m.to_wire() for m in messages],
            "stream": True,
        }
        if tools:
            request["tools"] = [t.to_wire() for t in tools]
        if settings.stream_usage:
            # `stream_options` is an OpenAI extension: DeepSeek honours it,
            # some compatible servers reject it with a 400.  See the config
            # field for how to turn it off.
            request["stream_options"] = {"include_usage": True}

        response = await _client().chat.completions.create(**request)
        async for event in response:
            for out in translate(event):
                yield out

    return stream


def build_server(settings: OpenAISettings, *, streamer: Streamer | None = None):
    """Build the MCP server.  `streamer` is injectable for tests."""
    return build_llm_server(
        name=SERVER_NAME,
        streamer=streamer if streamer is not None else build_streamer(settings),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_serve_args(argv, SERVER_NAME)
    configure_logging()
    settings = load(args.config).llm_openai
    logger.info(
        "serving %s on http://%s:%d%s",
        SERVER_NAME,
        args.host or settings.server.host,
        args.port or settings.server.port,
        settings.server.path,
    )
    serve(build_server(settings), settings.server, args)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
