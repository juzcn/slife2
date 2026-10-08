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

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from slife2.audience import client_meta
from slife2.mcp_server import tool_payload
from slife2.messages import ToolSpec
from slife2.tools import Tool, ToolFailed

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # the agent never imports a server, and this is a client one
    from fastmcp import Client

#: The hub's tools, named once and used by both halves.  They are called by this
#: process and never advertised to the model — the model sees the *proxied*
#: tools, under the names `UpstreamTool.name` carries.
LIST_TOOLS = "list_tools"
CALL_TOOL = "call_tool"
SERVERS = "servers"

#: The harness's own trim, and the one hub tool the *agent* calls on the hub's
#: API rather than through `call_tool`.  Its leading underscore is the system's
#: mark for a tool the machinery calls rather than one a model chooses, and the
#: hub's docstring on it says why the trim is a call at all: the answer names
#: what the model lost, and the harness is the party that has to know.
FUNC_TOOL_UNLOAD = "_func_tool_unload"

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


async def remote_tools(
    client: Client, client_id: tuple[str, str] | None = None
) -> list[Tool]:
    """Every tool the hub offers, as tools the loop can run.

    `client_id` is the conversation these tools will be run for, and it is bound
    *here* rather than passed by the model — see `_proxy`.  Optional because a
    caller with no conversation behind it (a test, a one-off) is a caller that
    has nobody to be.

    Raises:
        ConnectionError: If the hub answers with something this build cannot
            read.  Deliberately loud, and deliberately not an empty list: the
            likely cause is a hub daemon left over from a previous build
            (DESIGN.md §5), and the alternative — silently handing the model a
            short tool list — turns a version mismatch into a model that has
            quietly lost its abilities.  `slife2 down` is the answer.
    """
    payload = tool_payload(await client.call_tool(LIST_TOOLS, {}))
    listed = payload.get("tools")
    if not isinstance(listed, list):
        raise ConnectionError(
            f"the toolhub did not return a tool list (it said {payload!r}); a "
            f"daemon from another build does this — try `slife2 down`"
        )
    return [
        Tool(spec=entry.spec(), run=_proxy(client, entry.name, client_id))
        for entry in (UpstreamTool.from_wire(raw) for raw in listed)
    ]


async def unload_tools(client: Client) -> dict[str, Any]:
    """Trim the model's tool list to its budget, and say what was trimmed.

    **Called by the harness at a turn boundary, never by a model.**  The tools
    the model has loaded are what its next request carries, so a list that grew
    over a turn is trimmed before the turn is saved — and the names come back,
    because the caller is the party that has to know what the model just lost.
    The hub's `_func_tool_unload` docstring is where that is argued.

    A trim that cannot happen is **not** a turn that failed: the turn is over,
    the answer has been given, and this is bookkeeping that runs after it.  So a
    hub which does not know the tool — a daemon from a previous build, which
    answers with a refusal rather than a payload — leaves the list as it was and
    says so in the log, where the next `list_tools` will make a missing hub
    impossible to miss.

    Returns:
        The hub's payload: `unloaded` (the names that moved), `refused` and
        `not_loaded`, and `text` — the same thing in a sentence.
    """
    empty: dict[str, Any] = {
        "unloaded": [],
        "refused": [],
        "not_loaded": [],
        "text": "",
    }
    try:
        payload = tool_payload(await client.call_tool(FUNC_TOOL_UNLOAD, {}))
    except Exception as exc:  # noqa: BLE001 — bookkeeping does not fail a turn
        logger.warning("the tool list was not trimmed: %s", exc)
        return empty
    return payload or empty


def _proxy(client: Client, name: str, client_id: tuple[str, str] | None = None):
    """A tool body that calls `name` on the far side and reports what it said.

    **The conversation rides in `_meta`, not in the arguments.**  A tool that
    reads the db runs on behalf of one conversation, and which conversation is a
    fact this process holds and the model does not get to assert.  Passing
    `agent` as an argument would turn "read my history" into "read anybody's",
    and make the system prompt's `You are jack` load-bearing in a way nothing
    checks: a model that wrote another name would simply be believed.  So it
    goes beside the call rather than in it — off the schema the model reads, and
    out of reach of a prompt that asks for somebody else's turns.
    """
    meta = client_meta(*client_id) if client_id else None

    async def run(arguments: dict[str, Any]) -> str:
        payload = tool_payload(
            await client.call_tool(
                CALL_TOOL, {"name": name, "arguments": arguments}, meta=meta
            )
        )
        text = str(payload.get("text") or "")
        if not payload.get("ok"):
            # Raised rather than returned: the loop's contract is that a tool
            # produces text, so the only way to say "this one failed" without
            # the transcript calling it a success is to fail.
            raise ToolFailed(text or f"{name} failed without saying why")
        return text

    return run


__all__ = [
    "CALL_TOOL",
    "FUNC_TOOL_UNLOAD",
    "LIST_TOOLS",
    "SEPARATOR",
    "SERVERS",
    "UpstreamTool",
    "remote_tools",
    "unload_tools",
]
