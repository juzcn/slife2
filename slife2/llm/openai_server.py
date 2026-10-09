"""slife2-llm-openai — every OpenAI-compatible provider, behind MCP.

**One process per wire protocol, not per provider.**  This process serves every
provider in the config whose `api` is `openai-completions`, and
`stream_chat(provider=...)` says whose credentials and model list a given call
uses.  A process per provider would be more processes buying nothing: they all
speak one format, and this way a provider nobody calls never has its key
resolved at all.  It holds no state between calls.

"OpenAI-compatible" is doing real work in that sentence.  The same code serves
OpenAI, DeepSeek, Ollama, vLLM, scnet, and most gateways, because they all speak
the chat-completions wire format — which is exactly why that format was chosen
as the neutral one in `slife2.messages`.

**One field is translated rather than passed on.**  The neutral message carries
the model's own reasoning, because a turn log stores messages and a conversation
read back with the reasoning stripped has a hole exactly where the reader was
looking — but the chat-completions wire has no such field.  `reasoning_content`
is DeepSeek's name for it, and `to_provider_messages` is where it is renamed:
that function is the whole of the difference between what we carry and what a
provider is sent.  The other two protocols build their requests field by field
and so drop it, which is the arrangement v1 arrives at from the other side.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from slife2.config import ModelSettings, ProviderSettings
from slife2.llm.base import Chunk, Finish, ProviderEvent, Streamer, ToolCallDelta
from slife2.llm.server_common import (
    ProviderClients,
    build_llm_server,
    serve_backend,
)
from slife2.messages import Message, ToolSpec, Usage

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-llm-openai"

#: The wire protocol this process speaks — and the key its address lives under.
API = "openai-completions"

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


def to_provider_messages(
    messages: list[Message], *, reasoning: bool
) -> list[dict[str, Any]]:
    """The messages as this wire wants them — the one place the two differ.

    `thinking` is carried on the neutral message for the reason it exists: a
    turn log stores messages, and a conversation read back with the reasoning
    stripped has a hole in it.  This wire has no such field, and DeepSeek's name
    for the same thing is `reasoning_content` — so here it is renamed, and this
    function is the whole of the difference between what we carry and what a
    provider is sent.  The other two adapters build their requests field by
    field and so drop it, which is v1's arrangement exactly.

    **The empty string is the load-bearing part.**  When its reasoners are asked
    to think, the DeepSeek API requires `reasoning_content` on *every* assistant
    message in the history and answers 400 when one is missing — and a harness
    pair, or an assistant message whose model reported nothing, is exactly such
    a message.  So "no reasoning" and "no field" have to be spelled differently,
    and only one of the two is accepted.

    `reasoning` is the model's own `reasoning: true`, which is v1's rule: the
    flag that says a model thinks is the flag that says its thinking comes back
    to it.  A model without it gets neither the field nor the empty filler,
    because an endpoint that never reports reasoning is an endpoint whose
    acceptance of the key is unknown.
    """
    converted: list[dict[str, Any]] = []
    for message in messages:
        wire = message.to_wire()
        thinking = wire.pop("thinking", "")
        if thinking:
            wire["reasoning_content"] = thinking
        elif reasoning and message.role == "assistant":
            wire["reasoning_content"] = ""
        converted.append(wire)
    return converted


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
        "messages": to_provider_messages(messages, reasoning=settings.reasoning),
        "stream": True,
    }
    if tools:
        request["tools"] = [t.to_wire() for t in tools]
    if stream_usage and settings.stream_usage != "omit":
        # An OpenAI extension.  Some compatible servers reject it with a 400,
        # and `compat.stream_usage: omit` is the switch for those — per model,
        # because whether the field is accepted is a fact about the endpoint and
        # not about this process.  Sending it stays the default: without it the
        # provider reports no token counts at all, which is what the status bar
        # and the turn's own record are reading.
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


def _make_streamer(
    providers: dict[str, ProviderSettings], *, stream_usage: bool = True
) -> Streamer:
    """The adapter for one process — every provider that speaks this wire.

    The SDK client is created on first use, not here: the API key is resolved at
    that moment, so a server that never receives a call never opens the OS
    keyring, and constructing a client outside a running event loop is not
    always safe.
    """

    def sdk_client(provider: ProviderSettings, key: str) -> Any:
        """The one per-protocol fact: which SDK class, and with what.

        Imported here rather than at the top of the file for the reason this
        whole arrangement exists — the module is importable, and a process that
        serves nothing from it never loads the SDK.
        """
        from openai import AsyncOpenAI

        return AsyncOpenAI(base_url=provider.base_url, api_key=key)

    clients = ProviderClients(providers, SERVER_NAME, sdk_client)

    async def stream(
        provider: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
    ) -> AsyncIterator[ProviderEvent]:
        settings = clients.provider(provider).model(model)
        request = build_request(messages, tools, settings, stream_usage=stream_usage)
        response = await clients.client(provider).chat.completions.create(**request)
        async for event in response:
            for out in translate(event):
                yield out

    return stream


def build_streamer_for(
    providers: dict[str, ProviderSettings], *, stream_usage: bool = True
) -> Streamer:
    """The adapter for every provider this process serves."""
    return _make_streamer(providers, stream_usage=stream_usage)


def build_server(
    providers: dict[str, ProviderSettings],
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
            else build_streamer_for(providers, stream_usage=stream_usage)
        ),
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `slife2-llm-openai` console script."""
    return serve_backend(
        argv, api=API, server_name=SERVER_NAME, build=build_server, logger=logger
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
