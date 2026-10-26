"""The neutral message model — the one vocabulary every plugin shares.

Why OpenAI-shaped
-----------------
This is deliberately the *OpenAI* chat-completions shape, not a bespoke format
that each provider translates into.  The OpenAI wire format is the intersection
everything speaks: DeepSeek, Ollama, vLLM, OpenAI itself, and most gateways.  A
bespoke middle format would mean two translations per provider (theirs and ours)
instead of one, buying nothing but symmetry.

It also means this module *is* the JSON that crosses `stream_chat`: the agent
loop hands a list of :meth:`Message.to_wire` dicts to the LLM MCP server, and an
Anthropic-backed server — living in a different process, possibly on a different
machine — is the only place that has to care that Anthropic disagrees.

Dataclasses rather than dicts
-----------------------------
Raw dicts would make every consumer remember whether a tool call is
``{"id", "function": {"name", "arguments"}}`` or something flatter, and whether
``arguments`` is a dict or a JSON string.  Dataclasses answer those questions
once, in :meth:`ToolCall.to_wire`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)

#: The four roles the loop and both providers agree on.  `tool` carries a tool's
#: result back to the model, addressed by `tool_call_id`.
Role = Literal["system", "user", "assistant", "tool"]


def read_arguments(name: str, raw: str) -> tuple[dict[str, Any], str | None]:
    """The arguments *raw* carries, or why the call must not be dispatched.

    **An empty string is a call with no arguments; anything else must be a JSON
    object.**  `[]`, `null` and `5` parse and are still not arguments — the
    protocol says a call's arguments are an object — so they are refused rather
    than read as `{}`.  An empty dict is not a neutral stand-in: it runs a tool
    on defaults the model never chose, and the tool's own complaint then blames
    the wrong mistake (a missing required argument, from a call the model got
    wrong in a different way).

    The sentence names the tool and carries the first 200 characters of what
    arrived, because the reader is the model and that is what it needs to redo
    the call.  The call is not dispatched; the error is its answer.
    """
    if not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, (
            f"tool call {name!r} was not run: its arguments are not JSON "
            f"({exc}): {raw[:200]!r}"
        )
    if not isinstance(parsed, dict):
        return {}, (
            f"tool call {name!r} was not run: its arguments are not a JSON "
            f"object but {type(parsed).__name__}: {raw[:200]!r}"
        )
    return parsed, None


@dataclass
class ToolCall:
    """A complete tool call the model asked for.

    `arguments` is always a parsed dict.  Providers stream it as a JSON *string*
    in fragments and the fragments are reassembled by the LLM server, so nothing
    downstream of that hop ever sees a half-parsed call or a raw string.

    **A call whose arguments are not a JSON object carries `error` and is never
    dispatched.**  `arguments` is then empty and `raw_arguments` holds what the
    wire said, because a reader needs both facts: the tool must not run on
    arguments the model never sent, and the record must show what it did send.
    `ToolRegistry.execute` answers the error text with `ok=False` and does not
    invoke the tool.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    #: Why this call cannot be dispatched, or `None`.  Set only when the wire's
    #: arguments are neither empty nor a JSON object — see `read_arguments`.
    error: str | None = None
    #: The arguments exactly as the wire carried them, kept only beside `error`;
    #: `to_wire` sends these back rather than an empty object, so a stored turn
    #: records what the model actually wrote.
    raw_arguments: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render as the OpenAI tool-call object.

        `arguments` is re-serialised to a JSON string here because that is what
        the wire format requires — the awkwardness is theirs, and this method is
        the single place it is dealt with.

        `ensure_ascii=True` keeps the payload pure ASCII.  It crosses a UTF-8
        JSON-RPC boundary either way, but the same string can also land in a
        server log on a console whose codepage is not UTF-8, and a mojibake log
        line is a worse failure than an escaped one.
        """
        rendered = (
            self.raw_arguments
            if self.raw_arguments is not None
            else json.dumps(self.arguments, ensure_ascii=True)
        )
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": rendered},
        }

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> ToolCall:
        """Rebuild from the OpenAI tool-call object.

        **A call whose arguments are not a JSON object is kept, marked, and
        not dispatched.**  Invalid JSON and valid JSON that is not an object are
        the same damage: the call is still a call — its id is what a result
        answers, and its name is what the model needs to hear — so it is decoded
        with `error` set and the tool never runs.  `to_wire` sends the original
        text back, so the record keeps what the model wrote rather than an empty
        object it never sent.

        The same reading is in `read_arguments`, and it is the same sentence the
        LLM server uses when it assembles a damaged provider stream: one rule,
        whichever hop the call came through.
        """
        function = raw.get("function") or {}
        call_id = str(raw.get("id") or "")
        name = str(function.get("name") or "")
        raw_args = function.get("arguments")

        if isinstance(raw_args, dict):
            return cls(id=call_id, name=name, arguments=raw_args)
        if raw_args is None:
            # Absent is a call with no arguments — a real value, not damage.
            return cls(id=call_id, name=name)
        if isinstance(raw_args, str):
            arguments, error = read_arguments(name, raw_args)
            return cls(
                id=call_id,
                name=name,
                arguments=arguments,
                error=error,
                raw_arguments=raw_args if error is not None else None,
            )

        # Neither a string nor an object is not a shape this build writes; it
        # is still damage, and it is named rather than dropped.
        rendered = json.dumps(raw_args, ensure_ascii=True, default=str)
        return cls(
            id=call_id,
            name=name,
            error=(
                f"tool call {name!r} was not run: its arguments are not a JSON "
                f"object but {type(raw_args).__name__}: {rendered[:200]!r}"
            ),
            raw_arguments=rendered,
        )


@dataclass
class Message:
    """One entry in a conversation.

    `content` is optional because an assistant turn that only calls tools has no
    text, and a tool result's payload is also carried on `content`.

    It is a string *or* a list of content parts — the OpenAI shape, where a
    message carrying an image is a list of `{"type": "text"}` and
    `{"type": "image_url"}` blocks.  Keeping that as the neutral form means a
    plain message stays plain (the common case, and the one that has to stay
    readable), and only a message with an attachment pays for the structure.
    """

    role: Role
    content: str | list[dict[str, Any]] | None = None
    #: Assistant turns only — the model's own reasoning, when it reported any.
    #:
    #: **Not one of the OpenAI wire's fields**, and it travels anyway: this is
    #: the shape *we* pass a conversation around in, and each provider adapter
    #: builds its own request from it — so a field the provider has no name for
    #: can ride here and be renamed or dropped at the edge.  That is what makes
    #: the two things reasoning has to do possible at once: `openai_server`
    #: re-sends it as DeepSeek's `reasoning_content`, and `anthropic_server` and
    #: the responses server drop it, because those two have nowhere to put it.
    #:
    #: Kept on the message rather than only streamed at the reader, because the
    #: turn log stores messages: a conversation read back after a restart that
    #: has lost the reasoning has a hole in it, and the hole is exactly where
    #: the reader was looking.
    thinking: str = ""
    #: Assistant turns only — the calls this turn is asking for.
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: Tool turns only — which call this is the result of.
    tool_call_id: str | None = None
    #: Where the message came in on, for the turns the harness wrote rather than
    #: the model.
    #:
    #: **Display metadata, and the one field here that a provider never sees.**
    #: A message that cut into a running turn is written as a tool pair, and the
    #: tool result carries the words but not the channel they arrived on; the
    #: line a rebuilt transcript draws for that pair needs the channel to wear
    #: the right bubble, so it rides the message.  `to_wire` deliberately omits
    #: it — the model's request has no such field — and `to_record` is what
    #: keeps it in the turn log.  Empty for every message that did not cut in,
    #: which is every message a model or a person wrote.
    channel: str = ""

    def to_wire(self) -> dict[str, Any]:
        """Render as the message our own hop carries.

        The OpenAI chat message, plus `thinking` where there is any — the one
        key on this dict a provider may not know (see the field).  Whoever
        builds a provider's request is what turns this into that provider's
        shape, and `slife2.plugins.llm.openai_server` is where the one rename lives.

        Keys that do not apply to the role are omitted rather than sent as null:
        several OpenAI-compatible servers (vLLM, some gateways) reject a `tool`
        message carrying a `tool_calls` key, and an assistant message with
        `tool_call_id: null` is a 400 on others.  `thinking` is omitted on the
        same terms: an absent key and an empty one mean the same thing, and the
        absent spelling is what a turn stored before this field existed has.
        """
        payload: dict[str, Any] = {"role": self.role}
        # `content` is sent as an empty string rather than null when there are
        # tool calls: OpenAI accepts null there, but Ollama and a few gateways
        # do not, and an empty string is accepted by all of them.
        if self.content is not None:
            payload["content"] = self.content
        elif self.tool_calls:
            payload["content"] = ""

        if self.thinking:
            payload["thinking"] = self.thinking
        if self.tool_calls:
            payload["tool_calls"] = [c.to_wire() for c in self.tool_calls]
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        return payload

    def to_record(self) -> dict[str, Any]:
        """The stored shape: the wire's, plus the metadata only a reader needs.

        **One method rather than a second writer in the turn log.**  The store
        is handed messages as dicts (`slife2.plugins.agent.server.record`), and
        a display field added at that call site would be a second answer to what
        a message is — on the path that has to agree with the live list exactly.
        So the wire stays the model's shape and this adds what the record keeps
        beyond it; a field that is not display metadata belongs on neither.
        """
        payload = self.to_wire()
        if self.channel:
            payload["channel"] = self.channel
        return payload

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> Message:
        """Rebuild from the message our own hop carries.

        Normalises the one asymmetry `to_wire` introduces: it sends `""` as the
        content of a tool-calling assistant turn, where this stores `None`.  Both
        mean "no text", and letting two spellings of that circulate is how a
        later comparison surprises someone.
        """
        tool_calls = [ToolCall.from_wire(c) for c in raw.get("tool_calls") or []]
        content = raw.get("content")
        if content == "" and tool_calls:
            content = None

        role = raw.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            # **A message with no readable role is damage, and "user" is the
            # worst default to give it silently**: it puts words in the
            # person's mouth.  The shape still says which arm it belongs to —
            # a tool result addresses a call, an assistant turn makes one — so
            # the message keeps its place, and the warning says what was
            # inferred (SDD Appendix C).
            role = (
                "tool"
                if raw.get("tool_call_id")
                else "assistant"
                if tool_calls
                else "user"
            )
            logger.warning(
                "a message carried no readable role (%r); reading it as %r",
                raw.get("role"),
                role,
            )

        return cls(
            role=cast(Role, role),
            content=content,
            thinking=str(raw.get("thinking") or ""),
            tool_calls=tool_calls,
            tool_call_id=raw.get("tool_call_id"),
            channel=str(raw.get("channel") or ""),
        )


@dataclass(frozen=True)
class ToolSpec:
    """A tool advertised to the model: what it is called and how to call it.

    `parameters` is JSON Schema.  It is authored once, in :mod:`slife2.libs.turn.tools`,
    and both providers derive their own encoding from it.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        """Render as the OpenAI tool object (the shape `stream_chat` accepts)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> ToolSpec:
        """Rebuild from the OpenAI tool object."""
        function = raw.get("function") or {}
        return cls(
            name=str(function.get("name") or ""),
            description=str(function.get("description") or ""),
            parameters=function.get("parameters") or {},
        )


@dataclass(frozen=True)
class Usage:
    """Token counts for one model call.

    Frozen and addable so the loop can accumulate a turn's usage with `+=`
    without a mutable accumulator threaded through the streaming code.

    **`cache_read` and `cache_creation` are parts of `prompt_tokens`, not
    additions to it.**  Every provider that reports them counts the cached
    prefix inside its own prompt count — Anthropic's `input_tokens` is the
    *uncached* remainder, so its adapter adds the two cache fields back in;
    OpenAI-compatible wires already include them — and this class keeps that
    same rule so a total is the whole prompt either way.  `total_tokens`
    therefore stays prompt + completion, and the two cache fields are the
    breakdown a cache-hit ratio is computed from.  A provider that reports
    neither leaves both zero.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Prompt tokens served from the provider's prefix cache.
    cache_read: int = 0
    #: Prompt tokens the provider wrote to its prefix cache on this call.
    cache_creation: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cache_read=self.cache_read + other.cache_read,
            cache_creation=self.cache_creation + other.cache_creation,
        )

    def to_wire(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read": self.cache_read,
            "cache_creation": self.cache_creation,
        }

    @classmethod
    def from_wire(cls, raw: dict[str, Any] | None) -> Usage:
        raw = raw or {}
        return cls(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
            cache_read=int(raw.get("cache_read") or 0),
            cache_creation=int(raw.get("cache_creation") or 0),
        )


@dataclass(frozen=True)
class StreamChatResult:
    """The complete assistant message — the authoritative result of `stream_chat`.

    This, not the accumulated progress deltas, is what the agent loop appends to
    the conversation.  Progress notifications are a *display* channel: they can
    be dropped, delayed, or coalesced by the transport without the conversation
    ever noticing, which is exactly the property that makes streaming over MCP
    safe to build on.
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    #: The model's reasoning, for the models that reported any.
    #:
    #: Here as well as on the progress stream, and for the same reason `text` is:
    #: the chunks are how reasoning is *watched* and this is what the turn
    #: *keeps*.  A caller that accumulated it from the deltas would be trusting
    #: the display channel for a fact about the record — which is the mistake
    #: this class exists to prevent, one field over.
    thinking: str = ""
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [c.to_wire() for c in self.tool_calls],
            "thinking": self.thinking,
            "usage": self.usage.to_wire(),
            "stop_reason": self.stop_reason,
        }

    @classmethod
    def from_wire(cls, raw: dict[str, Any] | None) -> StreamChatResult:
        raw = raw or {}
        return cls(
            text=str(raw.get("text") or ""),
            tool_calls=tuple(
                ToolCall.from_wire(c) for c in raw.get("tool_calls") or []
            ),
            # An absent key reads as empty, which is what a server from before
            # this field existed sends — and is the truth about a model that
            # reported no reasoning.
            thinking=str(raw.get("thinking") or ""),
            usage=Usage.from_wire(raw.get("usage")),
            stop_reason=str(raw.get("stop_reason") or ""),
        )
