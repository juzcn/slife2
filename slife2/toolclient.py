"""The toolhub hop, from the agent's side: what the hub advertises, and how a
listed tool becomes one the loop can run.

The agent's tools come from one place — `slife2-toolhub` — and this is the half
of that hop the agent speaks.  It is deliberately thin: the hub has already
decided what the tools are, so all that happens here is that each listed tool
becomes a :class:`~slife2.tools.Tool` whose body is a call back through the same
connection.

Why this is its own module
--------------------------
The wire shape lives here rather than in `slife2.toolhub` because the hub must
not be importable from the agent.  `slife2.config` already states the rule for
server modules — reading a name means importing a module, and importing a server
drags a server into a process that must not have one — and the agent server is
the process that rule exists for.  So this is the leaf both halves sit on, the
way `slife2.events` is the leaf the loop and the agent server share.

"Client" is the same word `slife2.llm.client` uses for the same position: the
near side of a protocol boundary, in the process that asks.

The contract
------------
Three tool names, one payload shape, and one rule about failure:

* `list_tools` → `{"tools": [UpstreamTool, ...]}`, the whole set in one call.
  One call rather than one per server, because the only caller wants all of
  them: the agent builds a registry, and a registry with half the tools in it is
  not a smaller answer, it is a wrong one.
* `call_tool` → `{"text": str, "ok": bool}`.  **A failure is a value, not an
  exception**: an upstream that refused the call is one caller's bad data, which
  the model gets to read and act on.  `ok` is carried separately so the
  transcript can still show the call as failed — see `slife2.tools.ToolFailed`.
* Anything that *is* broken about the hub — unreachable, or answering with a
  shape this build does not know — raises.  That is the same line every other
  hop in this system draws: a peer that is gone fails the turn, and a peer that
  answered and said no does not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from slife2.messages import ToolSpec
from slife2.tools import Tool, ToolFailed

if TYPE_CHECKING:  # the agent never imports a server, and this is a client one
    from fastmcp import Client

#: The hub's tools, named once and used by both halves.  They are called by this
#: process and never advertised to the model — the model sees the *proxied*
#: tools, under the names `UpstreamTool.name` carries.
LIST_TOOLS = "list_tools"
CALL_TOOL = "call_tool"
SERVERS = "servers"

#: What separates a server from a tool in a proxied name.  Double, not single:
#: FastMCP's own multi-server client prefixes with one underscore, and one
#: underscore is a character an upstream tool name can contain — `read_file`
#: would then be a name nobody could split back apart.  Two of them, and the
#: convention is v1's, which used it for the same reason.
SEPARATOR = "__"


@dataclass(frozen=True)
class UpstreamTool:
    """One tool an external server offers, as the hub advertises it.

    `name` is what the model sees and calls: the upstream's own tool name with
    its server in front, already made legal for a provider by the hub.  `tool`
    keeps the name the upstream knows, because the two are no longer the same
    string once either has been sanitised and only the hub can address the far
    end — which is why `call_tool` takes `name` and not a pair.
    """

    name: str
    server: str
    tool: str
    description: str
    parameters: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "server": self.server,
            "tool": self.tool,
            "description": self.description,
            "parameters": self.parameters,
        }

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> UpstreamTool:
        parameters = raw.get("parameters")
        return cls(
            name=str(raw.get("name") or ""),
            server=str(raw.get("server") or ""),
            tool=str(raw.get("tool") or ""),
            description=str(raw.get("description") or ""),
            parameters=parameters if isinstance(parameters, dict) else {},
        )

    def spec(self) -> ToolSpec:
        """As the model's tool list wants it."""
        return ToolSpec(
            name=self.name, description=self.description, parameters=self.parameters
        )


async def remote_tools(client: Client) -> list[Tool]:
    """Every tool the hub offers, as tools the loop can run.

    Raises:
        ConnectionError: If the hub answers with something this build cannot
            read.  Deliberately loud, and deliberately not an empty list: the
            likely cause is a hub daemon left over from a previous build
            (DESIGN.md §5), and the alternative — silently handing the model a
            short tool list — turns a version mismatch into a model that has
            quietly lost its abilities.  `slife2 down` is the answer.
    """
    payload = _object(await client.call_tool(LIST_TOOLS, {}))
    listed = payload.get("tools")
    if not isinstance(listed, list):
        raise ConnectionError(
            f"the toolhub did not return a tool list (it said {payload!r}); a "
            f"daemon from another build does this — try `slife2 down`"
        )
    return [
        Tool(spec=entry.spec(), run=_proxy(client, entry.name))
        for entry in (UpstreamTool.from_wire(raw) for raw in listed)
    ]


def _proxy(client: Client, name: str):
    """A tool body that calls `name` on the far side and reports what it said."""

    async def run(arguments: dict[str, Any]) -> str:
        payload = _object(
            await client.call_tool(CALL_TOOL, {"name": name, "arguments": arguments})
        )
        text = str(payload.get("text") or "")
        if not payload.get("ok"):
            # Raised rather than returned: the loop's contract is that a tool
            # produces text, so the only way to say "this one failed" without
            # the transcript calling it a success is to fail.
            raise ToolFailed(text or f"{name} failed without saying why")
        return text

    return run


def _object(result: Any) -> dict[str, Any]:
    """The mapping a hub tool answered with.

    Reads the structured payload the SDK has already deserialized, and falls
    back to the text block — which is the same JSON, since a structured result
    is also sent as text.  A hub reached over a transport that kept only the
    text is still readable, and one that answered with neither is the mismatch
    `remote_tools` refuses.
    """
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    if isinstance(data, str) and data.strip():
        try:
            decoded = json.loads(data)
        except json.JSONDecodeError:
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


__all__ = [
    "CALL_TOOL",
    "LIST_TOOLS",
    "SEPARATOR",
    "SERVERS",
    "UpstreamTool",
    "remote_tools",
]
