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
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from slife2.libs.host.config import ModelSettings, plugin
from slife2.libs.model.base import Chunk, Finish, ProviderEvent, Streamer, ToolCallDelta
from slife2.libs.turn.messages import Message, ToolSpec, Usage
from slife2.plugins.llm.server_common import (
    THINKING_BUDGET_SHARE,
    Translate,
    WireParts,
    build_backend_server,
    content_parts,
    read_int,
    render_tool_result,
    sdk_client_factory,
    sdk_streamer,
    serve_backend,
    system_text,
)

logger = logging.getLogger(__name__)


#: The wire protocol this process speaks — and the key its address lives under.
API = "anthropic-messages"

SERVER_NAME = plugin(API).name

#: Output cap when a model entry does not set one.  Anthropic requires the
#: field, so "absent" cannot mean "omit" here the way it does for sampling.
DEFAULT_MAX_TOKENS = 4096

#: The smallest `budget_tokens` the Messages API accepts.  A floor of the
#: protocol rather than a preference: a request below it is a 400, so a
#: `max_tokens` that cannot hold it leaves no valid budget at all.
MIN_THINKING_BUDGET = 1024

#: The cache TTL spellings this config takes, and the TTL each sends.
#: `ephemeral` with no TTL is the API's own default (five minutes), so it is
#: not listed here.
_CACHE_TTLS = {"5m": "5m", "1h": "1h"}


def to_anthropic_blocks(content: Any) -> list[dict[str, Any]]:
    """Neutral content — a string or OpenAI content parts — as Anthropic blocks.

    The walk — drop-and-name, and refusing a source the API cannot read — is
    `server_common.content_parts`.  What is Anthropic's here is only what a
    block is called and what an image carries: the media type and the base64
    payload as separate fields rather than one nested data URL.
    """
    return content_parts(content, _WIRE)


def _image_fields(url: str) -> dict[str, Any] | None:
    """An accepted image URL as the fields of an `image` block's `source`.

    **Two forms and neither one is fetched here.**  A `data:` URL becomes a
    base64 source; a remote `http(s)` URL becomes a `url` source, which Anthropic
    downloads on its own side — and that is why the capability does not belong
    to this process: no request leaves this one, and there is no address a
    prompt could aim at this machine.

    (This function used to refuse a remote URL, on the argument that fetching one
    was a capability this plugin should not have.  The premise was wrong for this
    form of it: the provider fetches.)

    A `data:` URL that names a type and carries no payload has no bytes to read
    as a base64 source, so it is dropped — and *named*, by the type it claimed,
    for the reason `content_parts` names everything it drops: an attachment
    that vanishes without a word is the failure this module cannot see from
    inside (SDD Appendix C).
    """
    if url.startswith(("http://", "https://")):
        return {"source": {"type": "url", "url": url}}
    header, _, data = url.partition(",")
    media_type = header[5:].split(";", 1)[0] or "application/octet-stream"
    if not data:
        logger.warning(
            "anthropic: dropping an image whose `data:` source claims to be %s "
            "but carries no payload — there are no bytes to read as a base64 "
            "source, and the caller's attachment goes with it",
            media_type,
        )
        return None
    return {"source": {"type": "base64", "media_type": media_type, "data": data}}


#: This wire's part names, and the one part field that is its own: what an
#: `image` block carries beside its type.
_WIRE = WireParts(
    server="anthropic", text="text", image="image", image_fields=_image_fields
)


def reasoning_blocks(
    payload: dict[str, Any] | None, model: str
) -> list[dict[str, Any]]:
    """The thinking blocks this wire may replay from a message's opaque payload.

    **Only for the model that produced them.**  A signed thinking block is
    bound to its model; replaying one to another is a 400, so a conversation
    moved with `model_switch` keeps its old blocks in the record and sends none
    of them to the new model.  Empty `model` is the caller saying it does not
    replay reasoning at all (this model did not ask for thinking).

    Blocks are rebuilt from the fields this wire knows rather than passed
    through, so an envelope from another build cannot smuggle a key into the
    request; a block whose shape this build cannot send is named and dropped,
    because a malformed block is a 400 as surely as a missing one.
    """
    if not payload or not model or payload.get("model") != model:
        return []
    blocks: list[dict[str, Any]] = []
    for block in payload.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "thinking":
            text = block.get("thinking")
            signature = block.get("signature")
            if isinstance(text, str) and isinstance(signature, str) and signature:
                blocks.append(
                    {"type": "thinking", "thinking": text, "signature": signature}
                )
                continue
        elif kind == "redacted_thinking":
            data = block.get("data")
            if isinstance(data, str) and data:
                blocks.append({"type": "redacted_thinking", "data": data})
                continue
        logger.warning(
            "anthropic: dropping a reasoning block this build cannot replay (%r)",
            kind,
        )
    return blocks


def to_anthropic_messages(
    messages: list[Message], *, model: str = ""
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

    **`model` is what decides whether a turn's thinking blocks go back.**  The
    API requires them returned unmodified beside the tool results when thinking
    is on — without them the second call of every tool loop is a 400 — and it
    rejects blocks signed by another model, so this replays exactly the blocks
    of the model being called (`reasoning_blocks`).
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
            # The system prompt is a string parameter here, so a system turn's
            # parts are taken for the text in them and the rest is named —
            # `system_text` is the one rule both wires that hoist the prompt
            # follow, and it says why the text is taken rather than dropped.
            text = system_text(message.content, _WIRE, "system prompt")
            if text:
                system_parts.append(text)
            continue

        if message.role == "tool":
            _append(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id or "",
                        # A tool result is text, but a caller can legitimately
                        # send something else — `tool_result.content` is a block
                        # list here — so a non-string is *rendered* rather than
                        # replaced with "": answering "" for a result nobody
                        # wrote drops the tool's whole output from the request
                        # and leaves nothing anywhere saying it went.
                        "content": render_tool_result(message.content),
                    }
                ],
            )
            continue

        if message.role == "assistant":
            # **Thinking blocks first**, which is the order the API emitted them
            # in and the order it requires them back in.
            blocks = [
                *reasoning_blocks(message.reasoning, model),
                *to_anthropic_blocks(message.content),
            ]
            for call in message.tool_calls:
                # **A damaged call's original text has no place here.**  This
                # wire's `input` is an object by protocol, so a call whose
                # arguments were not one replays as `{}` — the regression the
                # chat and Responses wires avoid by sending `raw_arguments`.
                # The decision tools' repair round repeats the refused text in
                # its tool result (`slife2.libs.turn.context.repair_note`),
                # which is the channel this wire does carry as text.
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

    **A cap too small to hold a budget is refused, not quietly downgraded.**  A
    model the config says reasons, with a `max_tokens` that cannot leave a
    legal `budget_tokens`, is a configuration that cannot be satisfied — turning
    thinking off with a warning is a weaker path standing in for the capability
    that was asked for (SDD Appendix C), and the caller is told nothing either way.  So
    it raises, naming the model and the two numbers.
    """
    if not settings.reasoning or settings.thinking in ("omit", "disabled"):
        return None
    # **Both bounds, because the API has both.**  `budget_tokens` must be at
    # least `MIN_THINKING_BUDGET` *and* smaller than `max_tokens`, so the two
    # constraints leave a valid budget only when the cap is above the floor.
    # The clamp used to guard the second alone — `max(1024, …)` then `min(…,
    # max_tokens - 1)` — so a model with `max_tokens: 1024`, the very value the
    # comment here names, came out at 1023 and 400'd on every call.
    budget = min(
        max(MIN_THINKING_BUDGET, int(max_tokens * THINKING_BUDGET_SHARE)),
        max_tokens - 1,
    )
    if budget < MIN_THINKING_BUDGET:
        raise ValueError(
            f"{settings.model}: max_tokens={max_tokens} leaves no room for the "
            f"{MIN_THINKING_BUDGET}-token minimum thinking budget that "
            f"`reasoning: true` asks for; raise max_tokens above "
            f"{MIN_THINKING_BUDGET}, or set this model's `compat.thinking` to "
            f"`omit` to say thinking is not wanted."
        )
    return {"type": "enabled", "budget_tokens": budget}


def cache_control_parameter(settings: ModelSettings) -> dict[str, Any] | None:
    """The top-level `cache_control` to send, or None to send none.

    Anthropic's automatic prompt cache: the API marks the last cacheable block
    in the request and serves that prefix from its cache on the next call.  A
    conversation here re-sends its history on every turn, so without the field
    the same prefix is paid for again on each one — the numbers in
    `server_common`'s usage line are what makes that visible.

    **Opt-in per model.**  A gateway that speaks this wire is not obliged to
    implement the field, and one that does not answers a request carrying it
    with a 400.  `ephemeral` asks for the API's default five-minute TTL; `5m`
    and `1h` say a TTL explicitly; `omit` (or an absent field) sends nothing.

    An unrecognised value is refused, naming it and the spellings this adapter
    knows — a typo read as "send nothing" would leave the cache off with no
    word anywhere, the deformation SDD Appendix C names.
    """
    match settings.cache_control:
        case "" | "omit":
            return None
        case "ephemeral":
            return {"type": "ephemeral"}
        case ttl if ttl in _CACHE_TTLS:
            return {"type": "ephemeral", "ttl": _CACHE_TTLS[ttl]}
        case other:
            raise ValueError(
                f"{settings.model}: compat.cache_control {other!r} is not a "
                "spelling this adapter knows — write `ephemeral`, `5m`, `1h` or "
                "`omit`, or leave the field absent to send nothing."
            )


def build_request(
    messages: list[Message],
    tools: list[ToolSpec],
    settings: ModelSettings,
    *,
    tool_choice: str = "",
    thinking: str = "",
) -> dict[str, Any]:
    """The request body, containing only what the config asked for.

    `tool_choice` names the one tool the call must use and `thinking` overrides
    the model's configured value for this call — `openai_server.build_request`
    states the same two options, and a forced choice with no tool list is
    refused here for the same reason.
    """
    if thinking and settings.reasoning:
        settings = replace(settings, thinking=thinking)
    if tool_choice and not tools:
        raise ValueError(
            f"tool_choice {tool_choice!r} names a tool but the call sends no tools"
        )
    max_tokens = settings.max_tokens or DEFAULT_MAX_TOKENS
    thinking_block = thinking_parameter(settings, max_tokens)
    # A model that did not ask for thinking is a model to which a signed block
    # is not ours to send; the empty model makes `reasoning_blocks` drop them.
    system, converted = to_anthropic_messages(
        messages, model=settings.model if thinking_block is not None else ""
    )

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
        if tool_choice:
            request["tool_choice"] = {"type": "tool", "name": tool_choice}

    # Only what was configured: `None` means say nothing, not "use a default".
    #
    # **Sampling rides in `extra_body`, and that is not a style choice.**  The
    # SDK's typed surface dropped `temperature` and `top_p` when the first-party
    # API stopped accepting them — `anthropic` 1.11's `messages.create` has
    # neither — so passing one as an argument is a `TypeError` raised *here*,
    # before anything is sent, on every call of every conversation, with a
    # message about a keyword rather than about the model.  Through `extra_body`
    # the configured value reaches the wire, which is the whole of what this
    # function promises: a provider that refuses sampling answers in its own
    # words, naming the field, and one that accepts it — every gateway a
    # `anthropic-messages` entry in this config can point at — behaves exactly as
    # it did before.  Measured against `api.deepseek.com/anthropic`: the typed
    # form raises before the request is built, the `extra_body` form answers.
    sampling = {
        field: value
        for field, value in (
            ("temperature", settings.temperature),
            ("top_p", settings.top_p),
        )
        if value is not None
    }

    if thinking_block is not None:
        request["thinking"] = thinking_block
        # A thinking budget must leave room for an answer, and sampling is what
        # this endpoint rejects alongside extended thinking — so it is dropped
        # where it is collected rather than sent and refused.
        sampling = {}

    cache_control = cache_control_parameter(settings)
    if cache_control is not None:
        request["cache_control"] = cache_control

    if sampling:
        request["extra_body"] = sampling
    return request


class ThinkingBlocks:
    """One stream's thinking blocks, assembled across its events.

    **A thinking block cannot be built from one event.**  Its text arrives as
    `thinking_delta`s and its signature arrives afterwards, in a
    `signature_delta`, and the API accepts the block back only whole — text and
    signature together, unmodified.  So the pieces are kept here, one entry per
    content-block index, and the block is emitted at `content_block_stop`.

    A block with no signature is not emitted: a gateway that speaks this wire
    without signing its thinking cannot have its block replayed, and inventing
    an unsigned one would be a 400 from the endpoints that check.  Those
    gateways do not require the replay anyway, so dropping it is exactly the
    old behaviour rather than a new loss.
    """

    def __init__(self) -> None:
        self._text: dict[int, list[str]] = {}
        self._signature: dict[int, list[str]] = {}

    def open(self, index: int, block: Any) -> None:
        """Start a block that `close` will finish."""
        self._text[index] = [getattr(block, "thinking", "") or ""]
        signature = getattr(block, "signature", "") or ""
        self._signature[index] = [signature] if signature else []

    def add_text(self, index: int, thinking: str) -> None:
        self._text.setdefault(index, []).append(thinking)

    def add_signature(self, index: int, signature: str) -> None:
        self._signature.setdefault(index, []).append(signature)

    def close(self, index: int) -> dict[str, Any] | None:
        """The finished block, or None when this index is not a signed one."""
        text = "".join(self._text.pop(index, []))
        signature = "".join(self._signature.pop(index, []))
        if not signature:
            return None
        return {"type": "thinking", "thinking": text, "signature": signature}


def translate(
    event: Any, reasoning: ThinkingBlocks | None = None
) -> list[ProviderEvent]:
    """Turn one raw Anthropic stream event into provider events.

    Usage is split across two events and each contributes only its own half:
    `message_start` carries `input_tokens`, `message_delta` carries a
    *cumulative* `output_tokens`.  Summing both events' full usage would double
    count the output — `message_start` already reports `output_tokens: 1` for
    the message it just opened — so each half is emitted as a partial `Usage`
    and the accumulator adds them correctly.

    `reasoning` is the per-stream state that assembles thinking blocks
    (`ThinkingBlocks`); without one — a caller translating a single event — the
    readable `thinking` still streams and no opaque block is built, which is
    the old behaviour rather than a silent reading of the wrong thing.
    """
    events: list[ProviderEvent] = []
    kind = getattr(event, "type", "")

    if kind == "content_block_start":
        block = getattr(event, "content_block", None)
        index = int(getattr(event, "index", 0) or 0)
        block_type = getattr(block, "type", "")
        if block_type == "thinking" and reasoning is not None:
            reasoning.open(index, block)
        elif block_type == "redacted_thinking":
            # Complete at start: there is no text and no signature, only the
            # provider's encrypted blob, which goes back exactly as it came.
            data = getattr(block, "data", "") or ""
            if data:
                events.append(
                    Chunk(reasoning=({"type": "redacted_thinking", "data": data},))
                )
        if block_type == "tool_use":
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
                if reasoning is not None:
                    reasoning.add_text(index, thinking)
                events.append(Chunk(thinking=thinking))
        elif delta_type == "signature_delta":
            # The signature that makes the block replayable; it belongs to the
            # block, not to the display, so it produces no event of its own.
            signature = getattr(delta, "signature", "") or ""
            if signature and reasoning is not None:
                reasoning.add_signature(index, signature)
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

    elif kind == "content_block_stop":
        if reasoning is not None:
            block = reasoning.close(int(getattr(event, "index", 0) or 0))
            if block is not None:
                events.append(Chunk(reasoning=(block,)))

    elif kind == "message_start":
        message = getattr(event, "message", None)
        usage = getattr(message, "usage", None)
        if usage is not None:
            # `input_tokens` is the *uncached* part of the prompt: the cached
            # prefix is reported in two fields of its own, and a gateway with
            # prompt caching on would otherwise undercount the prompt by the
            # whole of it — silently, because the number still looks plausible.
            # The two fields keep their own counts as well, because the whole
            # point of reading this gateway's usage is to see how much of that
            # prompt the cache served — and `Usage`'s rule is that the cache
            # fields are parts of `prompt_tokens`, not additions to it.
            cache_creation = read_int(usage, "cache_creation_input_tokens")
            cache_read = read_int(usage, "cache_read_input_tokens")
            events.append(
                Chunk(
                    usage=Usage(
                        prompt_tokens=read_int(usage, "input_tokens")
                        + cache_creation
                        + cache_read,
                        cache_read=cache_read,
                        cache_creation=cache_creation,
                    )
                )
            )

    elif kind == "message_delta":
        usage = getattr(event, "usage", None)
        if usage is not None:
            events.append(
                Chunk(usage=Usage(completion_tokens=read_int(usage, "output_tokens")))
            )
        stop_reason = getattr(getattr(event, "delta", None), "stop_reason", None)
        if stop_reason:
            events.append(Finish(stop_reason=normalised_stop(str(stop_reason))))

    return events


#: This protocol's stop reasons, in the vocabulary the rest of the system
#: speaks.  `slife2.plugins.llm.openai_responses_server` normalises for the same
#: reason: `TurnResult.stop_reason` reaches the TUI's status line and
#: `send_message`'s result, and a caller comparing two backends must not be
#: told a different story by one of them.
_STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
}


def normalised_stop(reason: str) -> str:
    """One of this protocol's reasons, said the way every backend says it.

    A reason this build has not heard of is passed through rather than mapped
    to something familiar: it is a fact about the model, and folding it into
    "stop" would hide exactly the case worth seeing.
    """
    return _STOP_REASONS.get(reason, reason)


def as_delta(chunk: Chunk, seen: int) -> tuple[Chunk, int]:
    """Turn a *cumulative* output count into the difference, and remember it.

    Anthropic reports `output_tokens` in `message_delta` as the total for the
    message so far, and the shared accumulator adds every `Usage` it is handed
    (`slife2.plugins.llm.server_common`).  That is right for exactly one delta, which is
    what the API sends today — and wrong for a gateway that emits several, each
    repeating the running total, where the turn would report two or three times
    the tokens it produced.  The number is not decorative: it is what the status
    bar shows, what the turn's row records as the bill, and what the budget's
    arithmetic reads.

    So the running total is turned into a difference at the stream, where the
    previous value is known, and the accumulator goes on being a sum.  Returns
    the chunk to yield and the total to carry.
    """
    if chunk.usage is None:
        return chunk, seen
    total = chunk.usage.completion_tokens
    if total <= seen:
        # A repeated or decreasing total, which no correct stream produces —
        # nothing to add, and the running total is left where it was.
        return replace(chunk, usage=None), seen
    return replace(chunk, usage=Usage(completion_tokens=total - seen)), total


def _make_streamer() -> Streamer:
    """The adapter for one process — every provider that speaks this wire.

    The SDK client is created on first use, not here: the key is resolved at
    that moment, so a provider nobody calls never opens the OS keyring, and
    constructing a client outside a running event loop is not always safe.
    """

    def make_translator() -> Translate:
        """One stream's translation, carrying its output total and its blocks."""
        produced = 0
        reasoning = ThinkingBlocks()

        def one(event: Any) -> Iterable[ProviderEvent]:
            nonlocal produced
            for out in translate(event, reasoning):
                # `isinstance`, because one raw event produces *both* kinds:
                # `message_delta` yields a usage chunk and a `Finish`, and the
                # second has no `usage` at all — reading it on the union is an
                # AttributeError in the middle of a stream, on every turn.
                if (
                    isinstance(out, Chunk)
                    and out.usage is not None
                    and out.usage.completion_tokens
                ):
                    out, produced = as_delta(out, produced)
                yield out

        return one

    return sdk_streamer(
        api=API,
        server_name=SERVER_NAME,
        sdk_client=sdk_client_factory("anthropic", "AsyncAnthropic"),
        build_request=build_request,
        start=lambda client, request: client.messages.create(**request),
        translator=make_translator,
    )


def build_server(*, streamer: Streamer | None = None):
    """Build the MCP server.  `streamer` is injectable for tests."""
    return build_backend_server(
        name=SERVER_NAME, streamer=streamer, make=_make_streamer
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `slife2-llm-anthropic` console script."""
    return serve_backend(
        argv, api=API, server_name=SERVER_NAME, build=build_server, logger=logger
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
