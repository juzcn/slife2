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
    Translate,
    build_backend_server,
    read_int,
    render_tool_result,
    sdk_client_factory,
    sdk_streamer,
    serve_backend,
)

logger = logging.getLogger(__name__)


#: The wire protocol this process speaks — and the key its address lives under.
API = "anthropic-messages"

SERVER_NAME = plugin(API).name

#: Output cap when a model entry does not set one.  Anthropic requires the
#: field, so "absent" cannot mean "omit" here the way it does for sampling.
DEFAULT_MAX_TOKENS = 4096

#: How much of the output budget a thinking model may spend reasoning.  Half,
#: because the other half is what the answer is made of.
THINKING_BUDGET_SHARE = 0.5

#: The smallest `budget_tokens` the Messages API accepts.  A floor of the
#: protocol rather than a preference: a request below it is a 400, so a
#: `max_tokens` that cannot hold it leaves no valid budget at all.
MIN_THINKING_BUDGET = 1024


def to_anthropic_blocks(content: Any) -> list[dict[str, Any]]:
    """Neutral content — a string or OpenAI content parts — as Anthropic blocks.

    The two APIs disagree about images in the usual way: OpenAI nests a data URL
    under `image_url`, Anthropic wants the media type and the base64 payload as
    separate fields.  A part this function does not recognise is dropped rather
    than passed through, because passing an unknown block to the Messages API is
    a 400 on every call that contains one — and it is *named* when it is dropped,
    because an attachment or a newer part type that disappears without a word is
    the failure this module cannot see from inside.  A part that is not a mapping
    at all is dropped and named the same way, for the same reason.
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []

    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            logger.warning(
                "anthropic: dropping a content part that is %r, not a mapping "
                "— the Messages API takes block mappings only, so it cannot be "
                "passed through, but the caller's part is now missing",
                type(part).__name__,
            )
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
            case other:
                logger.warning(
                    "anthropic: dropping a content part of type %r — it is not "
                    "a block the Messages API takes, so it cannot be passed "
                    "through, but the caller's attachment is now missing",
                    other,
                )
    return blocks


def _image_source(raw: Any) -> dict[str, Any] | None:
    """An `image_url` as Anthropic's source: inline data, or a URL.

    **Two forms and neither one is fetched here.**  A `data:` URL becomes a
    base64 source; a remote `http(s)` URL becomes a `url` source, which Anthropic
    downloads on its own side — v1 did exactly this, and it is why the capability
    does not belong to this process: no request leaves this one, and there is no
    address a prompt could aim at this machine.

    (This function used to refuse a remote URL, on the argument that fetching one
    was a capability this plugin should not have.  The premise was wrong for this
    form of it: the provider fetches.)

    A url in neither form — a `file:` URL, a bare word — is dropped, because a
    source the Messages API cannot read is a 400 on every call that carries one.
    So is a `data:` URL that names a type and carries no payload: there are no
    bytes to read.  Both are *named* when they are dropped — one by its scheme,
    the other by the type it claimed — for the reason the unrecognised part type
    one function up is named: an attachment that vanishes without a word is the
    failure this module cannot see from inside (DESIGN.md §11.1).
    """
    url = (raw or {}).get("url") if isinstance(raw, dict) else None
    if not isinstance(url, str) or not url:
        return None
    if url.startswith(("http://", "https://")):
        return {"type": "url", "url": url}
    if not url.startswith("data:"):
        scheme, _, _ = url.partition(":")
        logger.warning(
            "anthropic: dropping an image from a %r source — it is neither a "
            "`data:` URL nor an http(s) one, so the Messages API has no source "
            "block to read it as, and the caller's attachment goes with it",
            scheme,
        )
        return None
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
            #
            # **Taken, and not dropped**: reading only `str` content meant a
            # system turn carrying blocks contributed nothing at all, with no
            # word anywhere — a prompt the caller wrote left the request
            # (DESIGN.md §11.1).  The blocks are built by the same converter the
            # other roles use, so a `str` is one text block and a list is what
            # it says it is; a block that is not text is dropped with a warning,
            # because the API has no field to put it in.
            blocks = to_anthropic_blocks(message.content)
            text = "".join(
                str(block.get("text") or "")
                for block in blocks
                if block.get("type") == "text"
            )
            other = [
                str(block.get("type"))
                for block in blocks
                if block.get("type") != "text"
            ]
            if other:
                logger.warning(
                    "anthropic: a system turn carries %s, which the system "
                    "prompt has no field for — the text beside it is kept and "
                    "these are left out",
                    ", ".join(other),
                )
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

    **A cap too small to hold a budget is refused, not quietly downgraded.**  A
    model the config says reasons, with a `max_tokens` that cannot leave a
    legal `budget_tokens`, is a configuration that cannot be satisfied — turning
    thinking off with a warning is a weaker path standing in for the capability
    that was asked for (DESIGN.md §11.1), and the caller is told nothing either way.  So
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

    thinking = thinking_parameter(settings, max_tokens)
    if thinking is not None:
        request["thinking"] = thinking
        # A thinking budget must leave room for an answer, and sampling is what
        # this endpoint rejects alongside extended thinking — so it is dropped
        # where it is collected rather than sent and refused.
        sampling = {}
    if sampling:
        request["extra_body"] = sampling
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
        """One stream's translation, carrying its running output total."""
        produced = 0

        def one(event: Any) -> Iterable[ProviderEvent]:
            nonlocal produced
            for out in translate(event):
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
