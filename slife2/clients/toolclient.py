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
A handful of harness calls — `list_tools`, `call_tool`, `tool_exists`,
`tool_load` — one payload shape, and one rule about failure:

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
* `tool_exists` → the three-value catalogue answer plus, when the row is
  present, its advertised definition.  `acquire_tool` is the one reader that
  uses the definition: a call outside the request's list runs only if its
  arguments fit the schema the row carries *now*, and the tool is loaded
  (`tool_load`) on the same pass so the next request advertises it.
* Anything that *is* broken about the hub — unreachable, or answering with a
  shape this build does not know — raises.  That is the same line every other
  hop in this system draws: a peer that is gone fails the turn, and a peer that
  answered and said no does not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jsonschema import validate
from jsonschema.exceptions import ValidationError

import slife2.helpers.timeouts as _T
from slife2.libs.plugin.audience import client_meta
from slife2.libs.plugin.protocol import tool_payload, unreadable
from slife2.libs.turn.messages import ToolSpec
from slife2.libs.turn.tools import (
    CATALOGUE_MISSING,
    CATALOGUE_PRESENT,
    CATALOGUE_STARTING,
    CatalogueAnswer,
    Tool,
    ToolFailed,
)
from slife2.libs.turn.tools import (
    FUNC_TOOL_UNLOAD as _FUNC_TOOL_UNLOAD,
)

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
    the answer has been given, and this is bookkeeping that runs after it.  But
    *how* to say that is the caller's, not this helper's: the agent server logs
    a warning (its next request re-lists the tools and fails loudly on a hub
    that is really gone), while the agent-task server logs an error (a task runs
    one turn and has no next request).  So a call that could not be made — a hub
    from an older build that does not know the tool, or one that is gone —
    raises here and each caller reports it at its own level.

    Returns:
        The hub's payload: `unloaded` (the names that moved), `refused` and
        `not_loaded`, and `text` — the same thing in a sentence.  **An answer
        this build cannot read comes back as the empty mapping**, not as one of
        these: the caller reads it with `trim_answer`, which is where the
        refusal belongs.
    """
    return tool_payload(
        await client.call_tool(FUNC_TOOL_UNLOAD, {}, timeout=_T.timeouts.work.hub)
    )


TOOL_EXISTS = "tool_exists"
TOOL_LOAD = "tool_load"


@dataclass(frozen=True)
class CatalogueLookup:
    """What the catalogue says about a name, and the row it says it with.

    **The three-value answer and the definition are one ask.**  The harness
    needs the first to phrase a hint and the second to decide whether a call
    outside the request's list can run as it stands; asking twice would let the
    two answers describe different moments.
    """

    answer: CatalogueAnswer
    #: The advertised definition, when the row is present and its schema is
    #: readable.  `None` for an absent row and for an older hub that answers the
    #: three values alone; the caller then cannot check a call against it.
    tool: UpstreamTool | None = None


async def tool_lookup(
    client: Client, name: str, client_id: tuple[str, str] | None = None
) -> CatalogueLookup:
    """What the hub's catalogue says about this name.

    **The harness's error path**, for a model call that named a tool outside the
    request's list: a catalogued name was unloaded (`func_tool_load` puts it
    back), one with no row is a capability this machine does not have
    (`tool_search` finds what it can do), and one whose row is still being
    written is a name to ask for again — see `CatalogueAnswer`.  The registry
    cannot tell which from its own list, and the three next steps are different.

    **The two booleans are one answer here.**  `exists` is the row; `starting`
    is the catalogue still being written, and it is only meaningful when the row
    is absent.  An older hub answers `exists` alone, and its `false` is its
    whole answer — reading an absent `starting` as `false` keeps that hint
    working; a value of the wrong type is damage and raises.

    `client_id` is the conversation this question is for; the hub's catalogue is
    one file per home, and the identity is what makes it answer for the right
    one.  A hub that cannot be read raises `UnreadableAnswer`; the registry
    catches it, because the call has already failed and its explanation may not
    turn that into a turn failure.
    """
    answer = tool_payload(
        await client.call_tool(
            TOOL_EXISTS,
            {"name": name},
            meta=client_meta(*client_id) if client_id else None,
            timeout=_T.timeouts.work.hub,
        )
    )
    exists = answer.get("exists")
    if not isinstance(exists, bool):
        raise unreadable("the toolhub", "say whether it holds a tool", answer)
    if exists:
        definition = _definition(answer.get("tool"))
        if definition is not None and definition.name != name:
            raise unreadable("the toolhub", "return this tool's definition", answer)
        return CatalogueLookup(CATALOGUE_PRESENT, definition)
    starting = answer.get("starting")
    if starting is None:
        return CatalogueLookup(CATALOGUE_MISSING)
    if not isinstance(starting, bool):
        raise unreadable("the toolhub", "say whether it is still starting", answer)
    return CatalogueLookup(CATALOGUE_STARTING if starting else CATALOGUE_MISSING)


def _definition(raw: Any) -> UpstreamTool | None:
    """The row's advertised definition, or `None` when the hub did not send one.

    Absent is the older hub (the three values were its whole answer), and it is
    also the hub's answer for a row whose stored schema it cannot read: either
    way there is nothing to check a call against, and the caller falls back to
    the hint.  A *present* value with the wrong shape is damage, not a tool —
    and the arguments field in particular, because `from_wire` tolerates a
    non-mapping as `{}` and `{}` here means "no schema to check", which would
    turn a broken answer into a call that runs unchecked.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict) or not str(raw.get("name") or ""):
        raise unreadable("the toolhub", "return a tool's definition", raw)
    parameters = raw.get("parameters")
    if parameters is not None and not isinstance(parameters, dict):
        raise unreadable("the toolhub", "return a tool's parameters", raw)
    return UpstreamTool.from_wire(raw)


async def load_tool(
    client: Client, name: str, client_id: tuple[str, str] | None = None
) -> bool:
    """Load one tool into the model's list, and say whether it landed.

    **The harness's load**, for a call that fit a catalogued tool the model had
    not loaded: the tool goes into the list the next request carries, and the
    call itself runs in this turn instead of costing the model one of its own.
    A name the hub refused — a server that is down, a disabled entry, a playbook
    — answers `False`, and the caller falls back to the three-case sentence.

    `client_id` names the conversation, and with it the home whose catalogue is
    written: a load is a write, and one aimed at the wrong home is a model
    shown a tool its own request will not carry.
    """
    answer = tool_payload(
        await client.call_tool(
            TOOL_LOAD,
            {"name": name},
            meta=client_meta(*client_id) if client_id else None,
            timeout=_T.timeouts.work.hub,
        )
    )
    loaded = answer.get("loaded")
    refused = answer.get("refused")
    if not isinstance(loaded, list) or not isinstance(refused, list):
        raise unreadable("the toolhub", "say whether it loaded a tool", answer)
    return name in [str(one) for one in loaded]


async def acquire_tool(
    client: Client,
    name: str,
    arguments: dict[str, Any],
    client_id: tuple[str, str] | None = None,
) -> Tool | None:
    """A catalogued tool this call may run, or `None` when it may not.

    **The row's schema, not the conversation's memory, is the authority.**  A
    name outside the request's list can be one the model called before, with
    arguments copied from the history — but the tool may have changed since,
    and a call that no longer fits its current schema must go back through the
    load hint, where the model reads the schema that is true now.  Only an
    empty stored schema (the catalogue's spelling of "nothing to declare") is
    taken as "any arguments fit".

    Loading is the second half and deliberately behind the check: a call that
    does not fit must not change the model's list.  The returned `Tool` runs
    through the same proxy every listed tool does, so the registry has no
    second execution path.
    """
    found = await tool_lookup(client, name, client_id)
    if found.answer != CATALOGUE_PRESENT or found.tool is None:
        return None
    if not arguments_fit(name, found.tool.parameters, arguments):
        return None
    if not await load_tool(client, name, client_id):
        return None
    return Tool(spec=found.tool.spec(), run=_proxy(client, name, client_id))


def arguments_fit(
    name: str, parameters: dict[str, Any], arguments: dict[str, Any]
) -> bool:
    """Whether these arguments fit a tool's declared schema.

    **A schema this build cannot read or resolve is a no.**  The caller's next
    step is the load hint, and the tool's own server still validates the call
    once the model has seen the schema — failing closed here only costs the
    model the shortcut, never a call it was told would work.
    """
    if not parameters:
        return True
    try:
        validate(instance=arguments, schema=parameters)
    except ValidationError:
        return False
    except Exception as exc:  # noqa: BLE001 - an unreadable schema is a no
        logger.info("the stored schema for %r could not be checked: %s", name, exc)
        return False
    return True


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
    "TOOL_EXISTS",
    "TOOL_LOAD",
    "CatalogueLookup",
    "ToolCounts",
    "ToolListing",
    "UpstreamTool",
    "acquire_tool",
    "arguments_fit",
    "load_tool",
    "remote_catalogue",
    "remote_tools",
    "tool_lookup",
    "trim_answer",
    "unload_tools",
]
