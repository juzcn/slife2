"""slife2-llm-openai-responses — every OpenAI Responses provider, behind MCP.

**One process per wire protocol, not per provider.**  This process serves every
provider in the config whose `api` is `openai-responses`, and
`stream_chat(provider=...)` says whose credentials and model list a given call
uses.

The Responses API is a *different protocol* from chat-completions, not a flag on
it, which is why it is a third server rather than a branch in `openai_server`.
It disagrees with the neutral model in four ways, and all four are handled here
so nothing above this hop has to know:

1. **System prompts are `instructions`,** a top-level parameter rather than a
   message, so system turns are lifted out of the list.
2. **Tool definitions are flat** — `{"type": "function", "name", ...}` rather
   than nested under `function`, and the schema is `parameters` at the top.
3. **Tool calls and their results are input *items*, not fields on a message.**
   An assistant turn's calls become `function_call` items beside its text, and a
   tool result becomes a `function_call_output` item addressed by `call_id`.
   This is the part with no analogue in the other two backends.
4. **Content parts are renamed** — `text` is `input_text`, and an image nests a
   bare data URL rather than an `image_url` object.

What is *not* here is equally deliberate: the server is stateless, keeps nothing
between calls, and never sends `previous_response_id`.  The caller owns the
conversation and re-sends it, so there is nothing to resume from — which is why
`store` defaults to being left alone rather than being forced off.  See
`store_parameter`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from slife2.config import ModelSettings, ProviderSettings
from slife2.llm.base import Chunk, Finish, ProviderEvent, Streamer, ToolCallDelta
from slife2.llm.server_common import build_llm_server, serve_backend
from slife2.messages import Message, ToolSpec, Usage

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-llm-openai-responses"

#: The wire protocol this process speaks — and the key its address lives under.
API = "openai-responses"

#: The image detail level sent for an inline image.  The field is part of the
#: documented shape and `auto` is the documented default, so this says out loud
#: what the provider would have assumed — which is worth doing for a field whose
#: omission some compatible gateways reject.
DEFAULT_IMAGE_DETAIL = "auto"


def to_responses_parts(content: Any) -> list[dict[str, Any]]:
    """Neutral content parts as Responses input parts.

    A part this function does not recognise is dropped rather than passed
    through: an unknown item in the input list is a 400 on every call that
    carries one, and the neutral form has exactly two kinds because that is what
    the agent server can produce.  See `slife2.server.server._with_images`.
    """
    parts: list[dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        match part.get("type"):
            case "text":
                text = part.get("text") or ""
                if text:
                    parts.append({"type": "input_text", "text": text})
            case "image_url":
                image = to_input_image(part.get("image_url"))
                if image is not None:
                    parts.append(image)
    return parts


def to_input_image(raw: Any) -> dict[str, Any] | None:
    """A `data:` URL as an `input_image` part, or None if it is not one.

    Only inline data is accepted, and the reason is the same as the Anthropic
    adapter's: a remote URL would have to be fetched, and fetching an address a
    model or a user named is a capability this component has no business having.
    Dropping is safe here only because the agent server has already refused to
    send one.
    """
    url = (raw or {}).get("url") if isinstance(raw, dict) else None
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    return {"type": "input_image", "image_url": url, "detail": DEFAULT_IMAGE_DETAIL}


def to_responses_content(content: Any) -> Any:
    """One message's content, in the shape this API takes.

    A plain string stays a plain string.  That is not merely an optimisation:
    the common case is text, both the API's message item and its content list
    accept it, and a converter that wrapped every string in a one-element list
    would make every request in every log harder to read for nothing.
    """
    if isinstance(content, str):
        return content if content else None
    return to_responses_parts(content) or None


def to_responses_input(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Convert neutral messages into `(instructions, input items)`.

    The two halves are separate because this API takes the system prompt as its
    own parameter.  Several system turns are joined with a blank line rather
    than dropped, which is what the Anthropic adapter does for the same reason:
    a second system message is a legitimate thing to send, and discarding it
    would be a quiet behaviour change.

    An assistant turn becomes a text item followed by one `function_call` per
    call, in that order — which is the order the API itself emits them in, so a
    history that came from a previous response round-trips unchanged.
    """
    instructions: list[str] = []
    items: list[dict[str, Any]] = []

    for message in messages:
        if message.role == "system":
            # `instructions` is a string parameter, so a system message carrying
            # content parts has nothing to contribute — no API takes blocks here.
            if isinstance(message.content, str) and message.content:
                instructions.append(message.content)
            continue

        if message.role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id or "",
                    # Tool results are text.  Anything else would have to be
                    # rendered, and a result that is not a string is a bug
                    # upstream rather than something to guess at here.
                    "output": message.content
                    if isinstance(message.content, str)
                    else "",
                }
            )
            continue

        if message.role == "assistant":
            content = to_responses_content(message.content)
            if content is not None:
                items.append({"role": "assistant", "content": content})
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        # Re-serialised to a JSON string because that is what
                        # the wire requires — the awkwardness is theirs, and
                        # this is the one place it is dealt with.
                        "arguments": json.dumps(call.arguments, ensure_ascii=True),
                    }
                )
            # An assistant turn with neither text nor calls carries nothing.
            continue

        # Only `user` reaches here; the other three roles are handled above.
        content = to_responses_content(message.content)
        if content is not None:
            items.append({"role": message.role, "content": content})

    return "\n\n".join(instructions), items


def to_responses_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Convert neutral tool definitions to this API's flat function form.

    Flat is the whole difference from chat-completions: there is no `function`
    wrapper, so `name`, `description` and `parameters` sit at the top level
    beside `type`.  Sending the nested shape here is a 400 that names a field
    the config never mentioned.
    """
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        for tool in tools
    ]


def reasoning_parameter(settings: ModelSettings) -> dict[str, Any] | None:
    """The `reasoning` block to send, or None to send none.

    `summary: auto` is a request for a readable *summary* of the model's
    reasoning, which is the only form this API streams — the full reasoning text
    is not returned.  Asking for it is what makes a thinking model's working
    visible in the transcript, so it is tied to `reasoning: true`: a model the
    config does not claim reasons would be sent a parameter it has no use for,
    and possibly one it rejects.

    `compat.thinking: omit` or `disabled` suppress it, for a gateway that
    rejects the field while reasoning anyway — the same escape hatch the other
    two backends offer, because the gateways that need it are the same ones.
    """
    if not settings.reasoning or settings.thinking in ("omit", "disabled"):
        return None
    return {"summary": "auto"}


def store_parameter(settings: ModelSettings) -> bool | None:
    """The `store` flag to send, or None to say nothing.

    **Absent means send nothing**, which is the rule every other optional field
    in this config follows and is worth restating because the consequence here
    is unusual: the API's own default is to *retain* the response server-side,
    so saying nothing means leaving that on.

    That is a deliberate choice rather than an oversight.  Responses-compatible
    endpoints differ in how much of the API they implement, and `store` is one
    of the fields a partial implementation may not accept at all — so a server
    that forced it off on every call would make the whole backend unusable
    against those gateways to buy a guarantee the operator can ask for
    explicitly.  `compat.store: false` is that request; `compat.store: true` is
    the opposite one.  See `slife2/config.py` for why the field is tri-state.
    """
    return settings.store


def build_request(
    messages: list[Message],
    tools: list[ToolSpec],
    settings: ModelSettings,
) -> dict[str, Any]:
    """The request body, containing only what the config asked for."""
    instructions, items = to_responses_input(messages)

    request: dict[str, Any] = {
        "model": settings.model,
        "input": items,
        "stream": True,
    }
    if instructions:
        request["instructions"] = instructions
    if tools:
        request["tools"] = to_responses_tools(tools)

    # Only what was configured.  `None` means "say nothing and let the gateway
    # decide", which is not the same as passing a default — and unlike
    # Anthropic's `max_tokens`, this API does not require an output cap, so
    # absence is a real option here.
    if settings.temperature is not None:
        request["temperature"] = settings.temperature
    if settings.top_p is not None:
        request["top_p"] = settings.top_p
    if settings.max_tokens is not None:
        request["max_output_tokens"] = settings.max_tokens

    reasoning = reasoning_parameter(settings)
    if reasoning is not None:
        request["reasoning"] = reasoning

    store = store_parameter(settings)
    if store is not None:
        request["store"] = store
    return request


def translate(event: Any) -> list[ProviderEvent]:
    """Turn one Responses stream event into provider events.

    Pure, and takes `Any` rather than the SDK's event union on purpose: it reads
    only the handful of attributes it needs, which keeps it testable against
    hand-built objects while still working with the real ones.  The event's own
    `type` string is the discriminant, so an event this adapter has never heard
    of produces no events rather than an exception — the API's event list grows,
    and a new `response.*` event is not a reason to fail a turn.

    Two events here are errors rather than results, and both **raise**:
    `response.failed` and a bare `error`.  The alternative — returning an empty
    result with a stop reason — is indistinguishable from a successful empty
    answer: the turn would be recorded as if it had worked, and the
    only symptom would be a blank reply.  Raising is the shape the rest of the
    system is built for; `MCPBackend` re-raises and the TUI prints the message.
    """
    events: list[ProviderEvent] = []
    kind = getattr(event, "type", "")

    if kind == "response.output_text.delta":
        text = getattr(event, "delta", "") or ""
        if text:
            events.append(Chunk(text=text))

    elif kind in (
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
    ):
        # Reasoning arrives as its own kind, never folded into the answer: the
        # two are displayed differently, and a transcript that mixes them is
        # unreadable in a way that is hard to notice and impossible to undo.
        thinking = getattr(event, "delta", "") or ""
        if thinking:
            events.append(Chunk(thinking=thinking))

    elif kind == "response.output_item.added":
        # A function call opens as its own output item, which is where its
        # `call_id` and name are announced; the arguments stream afterwards.
        item = getattr(event, "item", None)
        if getattr(item, "type", "") == "function_call":
            events.append(
                Chunk(
                    tool_call_deltas=(
                        ToolCallDelta(
                            index=_int(event, "output_index"),
                            id=getattr(item, "call_id", None),
                            name=getattr(item, "name", None),
                        ),
                    )
                )
            )

    elif kind == "response.function_call_arguments.delta":
        # Keyed by `output_index`, not by the item id: each call is its own
        # output item, so the index is stable and unique for the whole
        # response — the job Anthropic's content-block index does.
        events.append(
            Chunk(
                tool_call_deltas=(
                    ToolCallDelta(
                        index=_int(event, "output_index"),
                        arguments_delta=getattr(event, "delta", "") or "",
                    ),
                )
            )
        )

    elif kind == "response.completed":
        response = getattr(event, "response", None)
        _raise_on_error(response)
        events.extend(_usage_event(response))
        events.append(Finish(stop_reason=_stop_reason(response)))

    elif kind == "response.incomplete":
        # Not an error: a response cut off at the output cap and a response
        # stopped by a content filter are both ordinary outcomes, and the
        # reason is carried through to the caller as the stop reason.
        response = getattr(event, "response", None)
        _raise_on_error(response)
        events.extend(_usage_event(response))
        events.append(Finish(stop_reason=_incomplete_reason(response)))

    elif kind == "response.failed":
        response = getattr(event, "response", None)
        raise RuntimeError(
            f"{SERVER_NAME}: the response failed: {_error_text(response)}"
        )

    elif kind == "error":
        message = getattr(event, "message", "") or "the provider reported an error"
        raise RuntimeError(f"{SERVER_NAME}: {message}")

    return events


def _int(source: Any, attribute: str) -> int:
    """Read an int attribute that may be absent or None."""
    return int(getattr(source, attribute, 0) or 0)


def _usage_event(response: Any) -> list[ProviderEvent]:
    """Usage as its own chunk, when the response reported any.

    This API reports token counts once, on the terminal event, unlike the
    chat-completions stream where they trail in a chunk of their own — so this
    is the only place they can be read from.
    """
    raw = getattr(response, "usage", None)
    if raw is None:
        return []
    return [
        Chunk(
            usage=Usage(
                prompt_tokens=_int(raw, "input_tokens"),
                completion_tokens=_int(raw, "output_tokens"),
            )
        )
    ]


def _stop_reason(response: Any) -> str:
    """`tool_calls` or `stop` — what the other two backends would have said.

    This API reports a `status`, not a finish reason, so the distinction the
    rest of the system speaks in has to be recovered from the output: a
    completed response that contains a function call is one the loop will
    continue from, and every other completed response is a final answer.
    """
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", "") == "function_call":
            return "tool_calls"
    return "stop"


def _incomplete_reason(response: Any) -> str:
    """Why the response stopped short, as the stop reason."""
    details = getattr(response, "incomplete_details", None)
    return str(getattr(details, "reason", "") or "incomplete")


def _error_text(response: Any) -> str:
    """The provider's own words for what went wrong."""
    error = getattr(response, "error", None)
    message = getattr(error, "message", None)
    code = getattr(error, "code", None)
    if message and code:
        return f"{message} ({code})"
    return str(message or code or "no detail given")


def _raise_on_error(response: Any) -> None:
    """Raise if a completed response still carries an error.

    A response can complete *and* hold an error — the API reports a failure it
    recovered from that way — and answering with whatever text arrived while
    ignoring the error is how a partial answer gets recorded as a whole one.
    """
    if getattr(response, "error", None) is not None:
        raise RuntimeError(f"{SERVER_NAME}: {_error_text(response)}")


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
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=provider.base_url, api_key=key)
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
        response = await _client(provider).responses.create(**request)
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
    """Entry point for the `slife2-llm-openai-responses` console script."""
    return serve_backend(
        argv, api=API, server_name=SERVER_NAME, build=build_server, logger=logger
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
