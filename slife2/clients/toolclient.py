"""The toolhub hop, from the agent's side: what the hub advertises, and how a
listed tool becomes one the loop can run.

The agent's tools come from one place — `slife2-toolhub` — and this is the half
of that hop the agent speaks.  It is deliberately thin: the hub has already
decided what the tools are, so all that happens here is that each listed tool
becomes a :class:`~slife2.libs.turn.tools.Tool` whose body is a call back through the same
connection.

Why this is its own module
--------------------------
The wire shape lives here rather than in `slife2.plugins.toolhub.server` because the hub must
not be importable from the agent.  `slife2.libs.host.config` already states the rule for
server modules — reading a name means importing a module, and importing a server
drags a server into a process that must not have one — and the agent server is
the process that rule exists for.  So this is the leaf both halves sit on, the
way `slife2.libs.turn.events` is the leaf the loop and the agent server share.

"Client" is the same word `slife2.clients.model` uses for the same position: the
near side of a protocol boundary, in the process that asks.

The contract
------------
Three tool names, one payload shape, and one rule about failure:

* `list_tools` → `{"tools": [UpstreamTool, ...], "total": int}`, the whole set
  in one call, and the size of the catalogue it was chosen from.  One call
  rather than one per server, because the only caller wants all of them: the
  agent builds a registry, and a registry with half the tools in it is not a
  smaller answer, it is a wrong one.  `total` is the *catalogue's* count — every
  category, skills and CLI entries included, loaded or not — and not the length
  of the list beside it, which is only what the model holds now.
* `call_tool` → `{"text": str, "ok": bool}`.  **A failure is a value, not an
  exception**: an upstream that refused the call is one caller's bad data, which
  the model gets to read and act on.  `ok` is carried separately so the
  transcript can still show the call as failed — see `slife2.libs.turn.tools.ToolFailed`.
* Anything that *is* broken about the hub — unreachable, or answering with a
  shape this build does not know — raises.  That is the same line every other
  hop in this system draws: a peer that is gone fails the turn, and a peer that
  answered and said no does not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import slife2.helpers.timeouts as _T
from slife2.libs.plugin.audience import client_meta
from slife2.libs.plugin.protocol import tool_payload, unreadable
from slife2.libs.turn.messages import ToolSpec
from slife2.libs.turn.tools import FUNC_TOOL_UNLOAD as _FUNC_TOOL_UNLOAD
from slife2.libs.turn.tools import Tool, ToolFailed

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # the agent never imports a server, and this is a client one
    from fastmcp import Client

#: The hub's tools, named once and used by both halves.  They are called by this
#: process and never advertised to the model — the model sees the *proxied*
#: tools, under the names `UpstreamTool.name` carries.
LIST_TOOLS = "list_tools"
CALL_TOOL = "call_tool"

#: The harness's own trim, and the one hub tool the *agent* calls on the hub's
#: API rather than through `call_tool`.  Its leading underscore is the system's
#: mark for a tool the machinery calls rather than one a model chooses, and the
#: hub's docstring on it says why the trim is a call at all: the answer names
#: what the model lost, and the harness is the party that has to know.
#: Taken from `slife2.libs.turn.tools` rather than spelled again: the hub serves this
#: name and the agent server calls it, and two copies of a string across a
#: wire is a rename that half-works.
FUNC_TOOL_UNLOAD = _FUNC_TOOL_UNLOAD

#: What separates a server from a tool in a proxied name — which is only ever
#: somebody else's tool: ours are the model's under their own names, because the
#: server behind one is slife2 itself (`slife2.plugins.toolhub.server.model_name`).  Double,
#: not single: FastMCP's own multi-server client prefixes with one underscore,
#: and one underscore is a character an upstream tool name can contain —
#: `read_file` would then be a name nobody could split back apart.  Two of them,
#: for that same reason.
SEPARATOR = "__"


@dataclass(frozen=True)
class UpstreamTool:
    """One tool a server offers, as the hub advertises it.

    `name` is what the model sees and calls: the upstream's own tool name, with
    its server in front when the tool is somebody else's and bare when it is one
    of ours, already made legal for a provider by the hub.  `tool`
    keeps the name the upstream knows, because the two are no longer the same
    string once either has been sanitised and only the hub can address the far
    end — which is why `call_tool` takes `name` and not a pair.
    """

    name: str
    server: str
    tool: str
    description: str
    parameters: dict[str, Any]
    #: What the tool's answer holds, as the tool's own `Returns:` section says
    #: it (`slife2.libs.plugin.protocol.RETURNS`), or `""`.  Not part of the
    #: model's tool definition — providers do not take an output contract — so
    #: `spec()` drops it; the hub stores it in the catalogue, where a search or
    #: a person can read it.
    returns: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "server": self.server,
            "tool": self.tool,
            "description": self.description,
            "parameters": self.parameters,
            "returns": self.returns,
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
            returns=str(raw.get("returns") or ""),
        )

    def spec(self) -> ToolSpec:
        """As the model's tool list wants it."""
        return ToolSpec(
            name=self.name, description=self.description, parameters=self.parameters
        )


@dataclass(frozen=True)
class ToolCounts:
    """The model's tool list against the catalogue it was chosen from.

    **The two numbers a display shows**, and they are different questions:
    `loaded` is the hub's loaded set — the rows the next request carries — and
    `total` is every row the catalogue holds: skills and CLI entries included,
    and the rows of a server that is down.  One without the other cannot say
    what on-demand loading is doing: `26/358` is a working budget, `26/26` is
    everything.

    `loaded` is `None` for one case and not zero: a server that predates the
    loaded half of the answer.  A zero would be a claim about the list, and a
    display would show `0/358` for a conversation that had simply not said.
    """

    loaded: int | None
    total: int


@dataclass(frozen=True)
class ToolListing:
    """What `list_tools` answered: the model's tools, and the catalogue's size.

    **Two numbers because they answer two questions.**  `tools` is the loaded
    set — what the hub offers the next request — and `total` is every row the
    catalogue holds, which is what a status display shows.  A tool nobody has
    loaded is still a tool that exists, and a skill or a CLI entry is neither
    loadable nor callable through `call_tool` and is still a row.

    A value rather than a tuple so the two halves are named where they are read:
    the agent server hands the list to the loop and the count to a display, and
    a reader of `[0]`/`[1]` has to remember which is which.
    """

    tools: list[Tool]
    total: int


async def remote_catalogue(
    client: Client,
    client_id: tuple[str, str] | None = None,
    *,
    timeout: float | None = None,
    wait: bool = True,
) -> ToolListing:
    """What the hub offers, and how large the catalogue behind it is.

    `client_id` is the conversation these tools will be run for, and it is bound
    *here* rather than passed by the model — see `_proxy`.  Optional because a
    caller with no conversation behind it (a test, a one-off) is a caller that
    has nobody to be.

    `timeout` is the asker's own patience, resolved at call time.  A turn takes
    `work.hub`; the start-up build takes `work.catalogue`, which is longer
    because it is the cold path — see that value's comment.  `None` means
    `work.hub`, so every existing caller keeps the budget it had.

    `wait` is passed to the hub and decides whether *this* ask waits for the
    sources' declarations.  The start-up build does; a turn's list does not — it
    reads the catalogue the last ask built and lets the refresh run on, because a
    declarations refresh can outlast `work.hub` when external sources are
    connecting, and a late source costs freshness rather than the turn.

    Raises:
        ConnectionError: If the hub answers with something this build cannot
            read.  Deliberately loud, and deliberately not an empty list: the
            likely cause is a hub daemon left over from a previous build
            (SDD §12.5), and the alternative — silently handing the model a
            short tool list — turns a version mismatch into a model that has
            quietly lost its abilities.  `slife2 down` is the answer.
    """
    payload = tool_payload(
        await client.call_tool(
            LIST_TOOLS,
            {"wait": wait},
            timeout=_T.timeouts.work.hub if timeout is None else timeout,
        )
    )
    listed = payload.get("tools")
    # **The elements are checked and not only the list.**  A hub that answers
    # `{"tools": ["fake__echo"]}` has the outer shape and none of the inner one,
    # and letting it through would fail two frames down in `from_wire` with
    # `AttributeError: 'str' object has no attribute 'get'` — a traceback about
    # a dict where the deliberate message about a *build* belongs.  The check is
    # the same one for both halves of the shape because the diagnosis is.
    if not isinstance(listed, list):
        raise unreadable("the toolhub", "return a tool list", payload)
    # **And an element with no name is the same diagnosis, not a tool.**  Every
    # missing field becomes `""` in `from_wire`, which is a tolerance for text
    # and a silent hole for the field that is an identity: a hub from another
    # build answering `{"tools": [{}]}` would hand the model a nameless tool it
    # could never call.  The element that broke the shape is the evidence, so
    # the element is what is quoted (SDD Appendix C).
    for raw in listed:
        if not isinstance(raw, dict) or not str(raw.get("name") or ""):
            raise unreadable("the toolhub", "return a tool list", raw)
    total = payload.get("total")
    # **A missing count is a build this one cannot read, not zero.**  `total`
    # is what a display shows, and reading an absent key as `0` would put "no
    # tools" on the bar of a catalogue that is full — the same mistake
    # `slife2.libs.plugin.protocol.unreadable` exists to refuse.
    if not isinstance(total, int) or isinstance(total, bool):
        raise unreadable("the toolhub", "return the catalogue's size", payload)
    return ToolListing(
        tools=[
            Tool(spec=entry.spec(), run=_proxy(client, entry.name, client_id))
            for entry in (UpstreamTool.from_wire(raw) for raw in listed)
        ],
        total=total,
    )


async def remote_tools(
    client: Client, client_id: tuple[str, str] | None = None
) -> list[Tool]:
    """Every tool the hub offers, as tools the loop can run.

    The list half of :func:`remote_catalogue`, for the callers that have no
    display to feed — the agent-task server, and tests.  It is one call and one
    reader: the total is read and dropped here, and `remote_catalogue` is where
    both halves are checked.
    """
    return (await remote_catalogue(client, client_id, wait=False)).tools


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
        `not_loaded`, and `text` — the same thing in a sentence.  **An answer
        this build cannot read comes back as the empty mapping**, not as one of
        these: the caller reads it with `trim_answer`, which is where the
        refusal belongs.
    """
    empty: dict[str, Any] = {
        "unloaded": [],
        "refused": [],
        "not_loaded": [],
        "text": "",
    }
    try:
        payload = tool_payload(
            await client.call_tool(FUNC_TOOL_UNLOAD, {}, timeout=_T.timeouts.work.hub)
        )
    except Exception as exc:  # noqa: BLE001 — bookkeeping does not fail a turn
        logger.warning("the tool list was not trimmed: %s", exc, exc_info=exc)
        return empty
    # **`payload or empty` laundered the one case `trim_answer` exists for.**
    # A hub answering with a shape this build does not know (`tool_payload`'s
    # `{}`) came back as the well-formed "nothing was unloaded, no sentence"
    # above, so `trim_answer` read it as an honest no-op and nothing was said —
    # the exact confusion `trim_answer` was written to remove, and the one the
    # two servers that trim already have a test for.  `empty` is for the
    # `except` above: a call that could not be *made* is a trim that did not
    # happen, and the log line there says so.  An answer that arrived and cannot
    # be read is damage, and damage is the caller's to refuse.
    return payload


def trim_answer(found: dict[str, Any]) -> tuple[list[str], str]:
    """Read a trim's answer — the names it unloaded and its sentence — or refuse it.

    **An answer the harness cannot read is not a trim that unloaded nothing.**
    `found.get("unloaded") or []` reads a missing or null key as the same
    "nothing to unload" an honest no-op produces, so no pair is written and the
    caller goes on believing it still holds the tools that have gone;
    `found.get("text") or ""` does the same one key over, writing an empty
    sentence where the hub's own belongs.  Both halves are one answer and are
    read as strictly as `remote_tools` reads a listing: only a *readable* empty
    answer is a verdict, and a missing key is damage (SDD Appendix C — a
    failure is not a verdict).  Reading both before either half is used also
    means a refused answer can never leave the half-written pair every provider
    rejects.

    **Here rather than in the two servers that trim**, which is where it was:
    the agent server and the agent-task server each carried a byte-identical
    copy of this function, down to the two messages — which are
    `slife2.libs.plugin.protocol.unreadable`'s, now one function the four modules
    that read a peer's answer share.  The rule belongs with the call whose answer
    it reads.
    """
    names = found.get("unloaded")
    if not isinstance(names, list):
        raise unreadable("the toolhub", "return the names it unloaded", found)
    text = found.get("text")
    if not isinstance(text, str):
        raise unreadable("the toolhub", "return the trim's sentence", found)
    return [str(name) for name in names], text


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
        # **An unreadable answer here is the same diagnosis the list path makes,
        # and not a tool that refused.**  `tool_payload` hands back `{}` for a
        # result it cannot read, and treating that as the `ok` false below would
        # render a version mismatch as `ToolFailed` — "the tool said no" — which
        # is the opposite of what this half of the hop deliberately says when the
        # *list* comes back wrong.  The contract is `{"text": str, "ok": bool}`,
        # so a payload missing either is a build this one cannot read.
        if "text" not in payload or "ok" not in payload:
            raise unreadable("the toolhub", "answer a call", payload)
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
    "ToolCounts",
    "ToolListing",
    "UpstreamTool",
    "remote_catalogue",
    "remote_tools",
    "trim_answer",
    "unload_tools",
]
