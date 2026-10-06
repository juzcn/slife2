"""The neutral message model — the one vocabulary every component shares.

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
v1 passes raw dicts around and pays for it in every consumer that has to
remember whether a tool call is ``{"id", "function": {"name", "arguments"}}`` or
something flatter, and whether ``arguments`` is a dict or a JSON string.  Here
those questions are answered once, in :meth:`ToolCall.to_wire`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, cast

#: The four roles the loop and both providers agree on.  `tool` carries a tool's
#: result back to the model, addressed by `tool_call_id`.
Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    """A complete tool call the model asked for.

    `arguments` is always a parsed dict.  Providers stream it as a JSON *string*
    in fragments and the fragments are reassembled by the LLM server, so nothing
    downstream of that hop ever sees a half-parsed call or a raw string.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

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
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=True),
            },
        }

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> ToolCall:
        """Rebuild from the OpenAI tool-call object.

        Tolerates malformed `arguments` (invalid JSON, or already a dict) by
        degrading to an empty dict rather than raising: a model that emits
        unparseable arguments should get an error *from its own tool call*, not
        kill the turn.  The tool then reports the problem back to the model,
        which is the loop's normal feedback path.
        """
        function = raw.get("function") or {}
        raw_args = function.get("arguments")
        if isinstance(raw_args, str):
            try:
                arguments = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                arguments = {}
        elif isinstance(raw_args, dict):
            arguments = raw_args
        else:
            arguments = {}

        return cls(
            id=str(raw.get("id") or ""),
            name=str(function.get("name") or ""),
            arguments=arguments,
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
    #: Assistant turns only — the calls this turn is asking for.
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: Tool turns only — which call this is the result of.
    tool_call_id: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render as the OpenAI chat message.

        Keys that do not apply to the role are omitted rather than sent as null:
        several OpenAI-compatible servers (vLLM, some gateways) reject a `tool`
        message carrying a `tool_calls` key, and an assistant message with
        `tool_call_id: null` is a 400 on others.
        """
        payload: dict[str, Any] = {"role": self.role}
        # `content` is sent as an empty string rather than null when there are
        # tool calls: OpenAI accepts null there, but Ollama and a few gateways
        # do not, and an empty string is accepted by all of them.
        if self.content is not None:
            payload["content"] = self.content
        elif self.tool_calls:
            payload["content"] = ""

        if self.tool_calls:
            payload["tool_calls"] = [c.to_wire() for c in self.tool_calls]
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        return payload

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> Message:
        """Rebuild from the OpenAI chat message.

        Normalises the one asymmetry `to_wire` introduces: it sends `""` as the
        content of a tool-calling assistant turn, where this stores `None`.  Both
        mean "no text", and letting two spellings of that circulate is how a
        later comparison surprises someone.
        """
        tool_calls = [ToolCall.from_wire(c) for c in raw.get("tool_calls") or []]
        content = raw.get("content")
        if content == "" and tool_calls:
            content = None

        return cls(
            role=cast(Role, raw.get("role", "user")),
            content=content,
            tool_calls=tool_calls,
            tool_call_id=raw.get("tool_call_id"),
        )


@dataclass(frozen=True)
class ToolSpec:
    """A tool advertised to the model: what it is called and how to call it.

    `parameters` is JSON Schema.  It is authored once, in :mod:`slife2.tools`,
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
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )

    def to_wire(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }

    @classmethod
    def from_wire(cls, raw: dict[str, Any] | None) -> Usage:
        raw = raw or {}
        return cls(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
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
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [c.to_wire() for c in self.tool_calls],
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
            usage=Usage.from_wire(raw.get("usage")),
            stop_reason=str(raw.get("stop_reason") or ""),
        )
