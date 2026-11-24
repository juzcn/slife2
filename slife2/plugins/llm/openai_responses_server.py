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
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from slife2.libs.host.config import ModelSettings, plugin
from slife2.libs.model.base import Chunk, Finish, ProviderEvent, Streamer, ToolCallDelta
from slife2.libs.turn.messages import Message, ToolSpec, Usage
from slife2.plugins.llm.server_common import (
    Translate,
    WireParts,
    build_backend_server,
    content_parts,
    forced_tool,
    read_int,
    render_tool_result,
    sdk_client_factory,
    sdk_streamer,
    serve_backend,
    system_text,
)

logger = logging.getLogger(__name__)


#: The wire protocol this process speaks — and the key its address lives under.
API = "openai-responses"

SERVER_NAME = plugin(API).name

#: The image detail level sent for an inline image.  The field is part of the
#: documented shape and `auto` is the documented default, so this says out loud
#: what the provider would have assumed — which is worth doing for a field whose
#: omission some compatible gateways reject.
DEFAULT_IMAGE_DETAIL = "auto"


def _image_fields(url: str) -> dict[str, Any]:
    """An accepted image URL as the fields of an `input_image` part.

    **Both forms go on, and neither is fetched here.**  An inline `data:` URL and
    a remote `http(s)` one are both valid `input_image.image_url` values, and the
    API downloads the remote one on its own side — so passing it through *is* what
    "not fetching" looks like, and dropping it silently would lose the attachment
    the caller sent with nothing anywhere reporting the loss.  That case is
    exactly what the file tunnel exists to serve (`slife2.libs.turn.attachments`
    passes an `http(s)` URL through untouched for the provider to fetch).

    `detail` is part of the documented shape and `auto` is the documented
    default, so this says out loud what the provider would have assumed — worth
    doing for a field whose omission some compatible gateways reject.
    """
    return {"image_url": url, "detail": DEFAULT_IMAGE_DETAIL}


#: This wire's part names, and the one part field that is its own: an
#: `input_image` part carries the whole URL, not Anthropic's split source.
_WIRE = WireParts(
    server="openai-responses",
    text="input_text",
    image="input_image",
    image_fields=_image_fields,
)


def to_responses_content(content: Any) -> Any:
    """One message's content, in the shape this API takes.

    A plain string stays a plain string.  That is not merely an optimisation:
    the common case is text, both the API's message item and its content list
    accept it, and a converter that wrapped every string in a one-element list
    would make every request in every log harder to read for nothing.
    """
    if isinstance(content, str):
        return content if content else None
    return content_parts(content, _WIRE) or None


def reasoning_items(payload: dict[str, Any] | None, model: str) -> list[dict[str, Any]]:
    """The reasoning items this wire may replay from a message's opaque payload.

    **Only for the model that produced them**, on `Message.reasoning`'s rule;
    empty `model` means this request did not ask the model to reason and must
    not replay another one's items.

    Items go back **exactly as received** — that is the API's own instruction,
    and they are opaque to this build.  A `store: false` request asks for
    `encrypted_content` beside them (`build_request`), because a stateless
    server has nothing else to resolve the item against.
    """
    if not payload or not model or payload.get("model") != model:
        return []
    return [
        item
        for item in payload.get("blocks") or []
        if isinstance(item, dict) and item.get("type") == "reasoning"
    ]


def to_responses_input(
    messages: list[Message], *, model: str = ""
) -> tuple[str, list[dict[str, Any]]]:
    """Convert neutral messages into `(instructions, input items)`.

    The two halves are separate because this API takes the system prompt as its
    own parameter.  Several system turns are joined with a blank line rather
    than dropped, which is what the Anthropic adapter does for the same reason:
    a second system message is a legitimate thing to send, and discarding it
    would be a quiet behaviour change.

    An assistant turn becomes a text item followed by one `function_call` per
    call, in that order — which is the order the API itself emits them in, so a
    history that came from a previous response round-trips unchanged.

    **Its reasoning items go first.**  A reasoning model's second call in a
    tool loop is a 400 without them when the response was not stored, and the
    API emitted them before the message and calls it reasons about.
    """
    instructions: list[str] = []
    items: list[dict[str, Any]] = []

    for message in messages:
        if message.role == "system":
            # `instructions` is a string parameter, so a system turn's parts are
            # taken for the text in them and the rest is named — `system_text`
            # is the one rule both wires that hoist the prompt follow, and it
            # says why the text is taken rather than dropped.
            text = system_text(message.content, _WIRE, "`instructions` parameter")
            if text:
                instructions.append(text)
            continue

        if message.role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id or "",
                    # A tool result is text, but a caller can legitimately send
                    # something else, so a non-string is *rendered* rather than
                    # replaced with "": answering "" for a result nobody wrote
                    # drops the tool's whole output from the request and leaves
                    # nothing anywhere saying it went.
                    "output": render_tool_result(message.content),
                }
            )
            continue

        if message.role == "assistant":
            items.extend(reasoning_items(message.reasoning, model))
            content = to_responses_content(message.content)
            if content is not None:
                items.append({"role": "assistant", "content": content})
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        # A JSON string, because that is what the wire requires
                        # — the awkwardness is theirs, and this is the one
                        # place it is dealt with.  **A damaged call's original
                        # text goes back as it came** (`raw_arguments`), the
                        # rule `ToolCall.to_wire` states for the chat wire:
                        # re-serialising its empty dict would send a call the
                        # model never made, and the repair round (and every
                        # later replay) would lose the answer being corrected.
                        "arguments": (
                            call.raw_arguments
                            if call.raw_arguments is not None
                            else json.dumps(call.arguments, ensure_ascii=True)
                        ),
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
    the opposite one.  See `slife2/libs/host/config.py` for why the field is tri-state.
    """
    return settings.store


def build_request(
    messages: list[Message],
    tools: list[ToolSpec],
    settings: ModelSettings,
    *,
    tool_choice: str = "",
    thinking: str = "",
    temperature: float | None = None,
) -> dict[str, Any]:
    """The request body, containing only what the config asked for.

    `tool_choice` names the one tool the call must use and `thinking` overrides
    the model's configured value for this call; the forced-choice guard is
    `server_common.forced_tool`.
    """
    if thinking and settings.reasoning:
        settings = replace(settings, thinking=thinking)
    if temperature is not None:
        settings = replace(settings, temperature=temperature)
    forced_tool(tools, tool_choice)
    reasoning = reasoning_parameter(settings)
    # A model that did not ask for reasoning is a model to which another one's
    # items are not ours to send; the empty model makes `reasoning_items` drop
    # them.
    instructions, items = to_responses_input(
        messages, model=settings.model if reasoning is not None else ""
    )

    request: dict[str, Any] = {
        "model": settings.model,
        "input": items,
        "stream": True,
    }
    if instructions:
        request["instructions"] = instructions
    if tools:
        request["tools"] = to_responses_tools(tools)
        if tool_choice:
            request["tool_choice"] = {"type": "function", "name": tool_choice}

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

    if reasoning is not None:
        request["reasoning"] = reasoning

    store = store_parameter(settings)
    if store is not None:
        request["store"] = store
    # **A stateless request must ask for the reasoning it will have to replay.**
    # With `store: false` the server keeps nothing, so a reasoning item comes
    # back without its `encrypted_content` unless this asks for it — and the
    # next call of a tool loop cannot send back what it never received.  Only
    # when reasoning was asked for: an endpoint that has no reasoning has no
    # use for the field, and a partial gateway may refuse the ones it does not
    # implement.
    if reasoning is not None and store is False:
        request["include"] = ["reasoning.encrypted_content"]
    return request


def translate(event: Any, *, streamed: set[int] | None = None) -> list[ProviderEvent]:
    """Turn one Responses stream event into provider events.

    Takes `Any` rather than the SDK's event union on purpose: it reads only the
    handful of attributes it needs, which keeps it testable against hand-built
    objects while still working with the real ones.  The event's own `type`
    string is the discriminant, so an event this adapter has never heard of
    produces no events rather than an exception — the API's event list grows,
    and a new `response.*` event is not a reason to fail a turn.

    `streamed` is the caller's memory of which **output indexes this stream has
    already delivered content for** — the function-call arguments, the answer
    text and the reasoning, whether it arrived as deltas or whole.  It is
    written here and not at the call site, because this is the only place that
    knows an event's `output_index`: a `Chunk` carries the index of a *tool
    call* and nothing at all about which text item it belongs to.  The terminal
    event reads it to back-fill only what never streamed
    (`_arguments_that_never_streamed`, `_text_that_never_streamed`,
    `_thinking_that_never_streamed`), and the `.done` events read it to avoid
    repeating content their deltas already delivered.

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
            _note_streamed(streamed, event)
            events.append(Chunk(text=text))

    elif kind == "response.output_text.done":
        # **The text half of the back-fill below, per part.**  An endpoint that
        # assembles the answer and sends it whole has only this event to send it
        # in: the per-token deltas never came, so `text_parts` stays empty while
        # a `Finish` — the event the cut-stream check reads
        # (`server_common.stream_chat_impl`) — arrives from `response.completed`
        # anyway, and the turn's *authoritative* answer is recorded as `""`.
        # The text this event carries is that answer, so an index that never
        # streamed is filled in from it, on `_arguments_that_never_streamed`'s
        # rule and for its reason.
        text = getattr(event, "text", "") or ""
        if text and read_int(event, "output_index") not in (streamed or set()):
            _note_streamed(streamed, event)
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
            _note_streamed(streamed, event)
            events.append(Chunk(thinking=thinking))

    elif kind in (
        "response.reasoning_summary_text.done",
        "response.reasoning_text.done",
    ):
        # **The reasoning half of the text back-fill below, per part.**  An
        # endpoint that assembles the summary and sends it whole has only this
        # event to send it in: the deltas never came, so `thinking_parts` stays
        # empty while the `Finish` arrives from `response.completed` anyway,
        # and the model's working is lost with nothing anywhere saying so — the
        # answer is right and the thinking line is simply absent.  An index
        # that never streamed is filled in from it, on
        # `_text_that_never_streamed`'s rule and for its reason.
        thinking = getattr(event, "text", "") or ""
        if thinking and read_int(event, "output_index") not in (streamed or set()):
            _note_streamed(streamed, event)
            events.append(Chunk(thinking=thinking))

    elif kind == "response.refusal.delta":
        # **A refusal is the answer, not a missing one.**  This protocol has a
        # channel of its own for it (`response.refusal.delta` / `.done`, and a
        # `refusal` content part on the assembled message), and the answer text
        # is null beside it.  Ignoring the channel made a refusal into an empty
        # answer and failed the turn.
        text = getattr(event, "delta", "") or ""
        if text:
            _note_streamed(streamed, event)
            events.append(Chunk(text=text))

    elif kind == "response.refusal.done":
        text = getattr(event, "refusal", "") or ""
        if text and read_int(event, "output_index") not in (streamed or set()):
            _note_streamed(streamed, event)
            events.append(Chunk(text=text))

    elif kind == "response.output_item.added":
        # A function call opens as its own output item, which is where its
        # `call_id` and name are announced; the arguments stream afterwards.
        item = getattr(event, "item", None)
        if getattr(item, "type", "") == "function_call":
            events.append(
                Chunk(
                    tool_call_deltas=(
                        ToolCallDelta(
                            index=read_int(event, "output_index"),
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
        arguments = getattr(event, "delta", "") or ""
        # **An empty delta is not content**, and remembering it as if it were
        # would skip `_arguments_that_never_streamed` for an index whose
        # arguments are only in the terminal response — which is exactly the
        # endpoint that back-fill exists for.
        if arguments:
            _note_streamed(streamed, event)
        events.append(
            Chunk(
                tool_call_deltas=(
                    ToolCallDelta(
                        index=read_int(event, "output_index"),
                        arguments_delta=arguments,
                    ),
                )
            )
        )

    elif kind == "response.completed":
        response = getattr(event, "response", None)
        _raise_on_error(response)
        events.extend(_arguments_that_never_streamed(response, streamed))
        events.extend(_text_that_never_streamed(response, streamed))
        events.extend(_thinking_that_never_streamed(response, streamed))
        events.extend(_reasoning_items(response))
        events.extend(_usage_event(response))
        events.append(Finish(stop_reason=_stop_reason(response)))

    elif kind == "response.incomplete":
        # Not an error: a response cut off at the output cap and a response
        # stopped by a content filter are both ordinary outcomes, and the
        # reason is carried through to the caller as the stop reason.
        response = getattr(event, "response", None)
        _raise_on_error(response)
        events.extend(_arguments_that_never_streamed(response, streamed))
        events.extend(_text_that_never_streamed(response, streamed))
        events.extend(_thinking_that_never_streamed(response, streamed))
        events.extend(_reasoning_items(response))
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


def _note_streamed(streamed: set[int] | None, event: Any) -> None:
    """Remember that this event's output index has delivered its content now.

    One line, and it is a function because it is written wherever an event
    delivers content, for one rule: an *output index* is the position of an
    item in `response.output`, and each kind of item — a function call, a
    message, a reasoning item — owns exactly one index (see `translate`'s
    `streamed`).  A stream that never had a memory handed to it is not an
    error: `translate` is callable without one, and a caller that passes none
    is a caller with one event and no terminal to back-fill.
    """
    if streamed is not None:
        streamed.add(read_int(event, "output_index"))


def _text_that_never_streamed(
    response: Any, streamed: set[int] | None
) -> list[ProviderEvent]:
    """The answer text of a response whose deltas never came.

    **The text half of `_arguments_that_never_streamed`**: the turn's answer is
    assembled from `response.output_text.delta` — or, for an endpoint that sends
    text whole, from `response.output_text.done`, which `translate` treats as
    the same content — so a response that sends neither leaves `text_parts`
    empty.  An endpoint that sends the text whole is a gateway that implements
    the Responses API partially, which this module already treats as real for
    `store` (`store_parameter`).  A `Finish` arrives
    regardless, so the cut-stream check passes, and the turn is stored and shown
    with the answer `""`: a blank reply recorded as the model's whole output,
    with nothing anywhere saying a thing was lost.

    The message items in the completed response carry the text, so every output
    index whose text never streamed is read from here.  One `Chunk` per item
    rather than one per content part, because that is what the deltas would have
    been: a string the accumulator appends to the same answer, in order.
    """
    seen = streamed or set()
    events: list[ProviderEvent] = []
    for index, item in enumerate(getattr(response, "output", None) or []):
        if getattr(item, "type", "") != "message" or index in seen:
            continue
        # **A refusal part is answer text too.**  The assembled message can
        # carry `{"type": "refusal", "refusal": …}` where a normal answer has
        # `output_text`; reading only `text` dropped the model's whole output
        # from the back-fill and recorded an empty answer.
        text = "".join(
            str(getattr(part, "text", None) or getattr(part, "refusal", None) or "")
            for part in getattr(item, "content", None) or []
            if getattr(part, "type", "") in ("output_text", "refusal")
        )
        if text:
            events.append(Chunk(text=text))
    return events


def _thinking_that_never_streamed(
    response: Any, streamed: set[int] | None
) -> list[ProviderEvent]:
    """The reasoning of a response whose deltas never came.

    **The reasoning half of the same back-fill**: the thinking parts are
    assembled from `response.reasoning_summary_text.delta` and
    `response.reasoning_text.delta` — or, for an endpoint that sends the
    summary whole, from their `.done` events, which `translate` treats as the
    same content — so a response that sends neither leaves `thinking_parts`
    empty while the `Finish` arrives anyway, and the model's working is lost
    with nothing anywhere saying so.

    The reasoning items in the completed response carry the text, so every
    output index whose reasoning never streamed is read from here: the
    `summary` parts and any `content` parts, the same two kinds the deltas
    arrive as.  One `Chunk` per item rather than one per part, on
    `_text_that_never_streamed`'s rule — a string the accumulator appends to
    the same thinking, in order.
    """
    seen = streamed or set()
    events: list[ProviderEvent] = []
    for index, item in enumerate(getattr(response, "output", None) or []):
        if getattr(item, "type", "") != "reasoning" or index in seen:
            continue
        text = "".join(
            str(getattr(part, "text", "") or "")
            for part in (getattr(item, "summary", None) or [])
            + (getattr(item, "content", None) or [])
            if getattr(part, "type", "") in ("summary_text", "reasoning_text")
        )
        if text:
            events.append(Chunk(thinking=text))
    return events


def _reasoning_items(response: Any) -> list[ProviderEvent]:
    """The reasoning items of a completed response, exactly as received.

    **The replay half.**  A reasoning model's next call in a tool loop must be
    sent the items this one produced — that is the API's documented contract
    for tool use, and with `store: false` the items carry the encrypted content
    the request asked for (`build_request`).  They are opaque here: serialised
    by the SDK's own model dump and replayed by `to_responses_input` untouched.

    Emitted regardless of whether their summary streamed: the readable text and
    the replayable item are different facts, and the display channel dropping a
    delta must not cost the conversation the item.
    """
    items: list[dict[str, Any]] = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", "") != "reasoning":
            continue
        wire = _plain(item)
        if isinstance(wire, dict):
            items.append(wire)
    return [Chunk(reasoning=tuple(items))] if items else []


def _plain(value: Any) -> Any:
    """An SDK object as the plain mapping its own encoder would write.

    The SDK's types are Pydantic models and `model_dump` is their wire form;
    a mapping or a list built by a test is walked the same way.  Anything else
    is left alone — dropping the item would lose the only replayable copy the
    conversation will ever have.
    """
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True, exclude_unset=True)
        except TypeError:  # a non-Pydantic double with a same-named method
            return dump()
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _arguments_that_never_streamed(
    response: Any, streamed: set[int] | None
) -> list[ProviderEvent]:
    """The arguments of a call whose deltas never came.

    The accumulator is fed by `response.function_call_arguments.delta` and by
    nothing else, so an endpoint that assembles the call and sends it whole
    leaves the model calling a tool with `{}` — the call runs or errors on
    empty input, and nothing anywhere says a thing was lost.  The item in the
    completed response carries the whole string, so every index that never
    streamed a delta is filled in from here.

    The index is the item's **position in `output`**, which is how
    `output_index` on the delta events is assigned; `streamed` is what the
    caller has seen, and an index that did stream is left alone — appending the
    whole string after its own deltas would concatenate it twice.
    """
    seen = streamed or set()
    events: list[ProviderEvent] = []
    for index, item in enumerate(getattr(response, "output", None) or []):
        if getattr(item, "type", "") != "function_call" or index in seen:
            continue
        arguments = getattr(item, "arguments", "") or ""
        if arguments:
            events.append(
                Chunk(
                    tool_call_deltas=(
                        ToolCallDelta(
                            index=index,
                            # **The whole call, not only its arguments.**  An
                            # endpoint that assembles the call sends no
                            # `output_item.added`, so this is the only place
                            # the accumulator can learn the call's id and name;
                            # without them the model's legitimate call arrives
                            # as `name=""` and is answered "unknown tool".
                            id=getattr(item, "call_id", None),
                            name=getattr(item, "name", None),
                            arguments_delta=str(arguments),
                        ),
                    )
                )
            )
    return events


def _usage_event(response: Any) -> list[ProviderEvent]:
    """Usage as its own chunk, when the response reported any.

    This API reports token counts once, on the terminal event, unlike the
    chat-completions stream where they trail in a chunk of their own — so this
    is the only place they can be read from.
    """
    raw = getattr(response, "usage", None)
    if raw is None:
        return []
    # The cached prefix is a *detail* on this wire, and the two fields are the
    # same breakdown the chat-completions adapter reads — parts of the input,
    # not additions to it (`slife2.libs.turn.messages.Usage` states the rule).
    details = getattr(raw, "input_tokens_details", None)
    return [
        Chunk(
            usage=Usage(
                prompt_tokens=read_int(raw, "input_tokens"),
                completion_tokens=read_int(raw, "output_tokens"),
                cache_read=read_int(details, "cached_tokens"),
                cache_creation=read_int(details, "cache_write_tokens"),
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


#: This protocol's reasons for stopping short, in the vocabulary the rest of the
#: system speaks.  `max_output_tokens` is the one that overlaps the other two
#: backends' `length`, and the whole point of the shared vocabulary is that the
#: same config on any backend tells a caller the same story: chat-completions
#: passes its raw finish reason through (`length`) and Anthropic maps its
#: `max_tokens` to `length` (`normalised_stop`), so a caller asking "was this
#: answer truncated?" with `stop_reason == "length"` must get the same answer
#: here.  A reason this build has not heard of is passed through rather than
#: folded into something familiar — it is a fact about the model.
_INCOMPLETE_REASONS = {
    "max_output_tokens": "length",
}


def _incomplete_reason(response: Any) -> str:
    """Why the response stopped short, as the stop reason."""
    details = getattr(response, "incomplete_details", None)
    reason = str(getattr(details, "reason", "") or "incomplete")
    return _INCOMPLETE_REASONS.get(reason, reason)


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


def _make_streamer() -> Streamer:
    """The adapter for one process — every provider that speaks this wire.

    The SDK client is created on first use, not here: the key is resolved at
    that moment, so a provider nobody calls never opens the OS keyring, and
    constructing a client outside a running event loop is not always safe.
    """

    def make_translator() -> Translate:
        """One stream's translation, remembering which output items streamed.

        `streamed` is what a *later* event needs to know about an earlier one —
        which is why it lives across the stream rather than inside `translate`,
        whose only input beyond the event itself is this set
        (`_arguments_that_never_streamed`, `_text_that_never_streamed`,
        `_thinking_that_never_streamed`).
        """
        streamed: set[int] = set()

        def one(event: Any) -> Iterable[ProviderEvent]:
            return iter(translate(event, streamed=streamed))

        return one

    return sdk_streamer(
        api=API,
        server_name=SERVER_NAME,
        sdk_client=sdk_client_factory("openai", "AsyncOpenAI"),
        build_request=build_request,
        start=lambda client, request: client.responses.create(**request),
        translator=make_translator,
    )


def build_server(*, streamer: Streamer | None = None):
    """Build the MCP server.  `streamer` is injectable for tests."""
    return build_backend_server(
        name=SERVER_NAME, streamer=streamer, make=_make_streamer
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `slife2-llm-openai-responses` console script."""
    return serve_backend(
        argv, api=API, server_name=SERVER_NAME, build=build_server, logger=logger
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
