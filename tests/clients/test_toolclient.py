"""The agent's half of the toolhub hop: a listed tool becomes a runnable one.

Two contracts meet here — the payload the hub sends and the shape the loop
expects — so this file tests both ends of the same round trip: mostly against a
client that answers the way the hub does, and once against the real hub, which
is what keeps the two halves from drifting apart.
"""

from __future__ import annotations

import json
import types
from dataclasses import replace
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from slife2.clients.toolclient import (
    CALL_TOOL,
    FUNC_TOOL_UNLOAD,
    LIST_TOOLS,
    UpstreamTool,
    remote_tools,
    trim_answer,
    unload_tools,
)
from slife2.libs.host.config import Config, ToolServerSettings, default_config
from slife2.libs.host.paths import DATA_ENV_VAR
from slife2.libs.turn.messages import ToolCall
from slife2.libs.turn.tools import ToolRegistry
from slife2.plugins.context.server import build_server as build_context
from slife2.plugins.toolhub.server import build_server as build_hub
from tests.fakes import StubEmbedder, plugin_transports

pytestmark = pytest.mark.unit


def answer(payload: Any) -> Any:
    """What `Client.call_tool` hands back for a tool that returned a mapping."""
    return types.SimpleNamespace(data=payload, content=[], is_error=False)


class FakeHub:
    """A client that answers the way the hub does, and records what it was asked.

    The client half only ever calls `call_tool`, so this is the whole of the
    surface it needs — and it lets one case be expressed exactly: a hub that
    answers with something this build cannot read.
    """

    def __init__(self, *, tools: list[dict[str, Any]] | None = None, junk: Any = None):
        self.tools = (
            tools
            if tools is not None
            else [
                UpstreamTool(
                    name="fake__echo",
                    server="fake",
                    tool="echo",
                    description="[fake] Echo it back.",
                    parameters={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                ).to_wire()
            ]
        )
        self.junk = junk
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: Any = None, **_: Any) -> Any:
        self.calls.append((name, arguments or {}))
        if name == LIST_TOOLS:
            return answer(self.junk if self.junk is not None else {"tools": self.tools})
        if name == CALL_TOOL:
            return answer({"text": "answered", "ok": True})
        raise AssertionError(f"the agent asked for {name!r}")


# --- the list becomes tools ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_listed_tool_becomes_a_tool_the_loop_can_run() -> None:
    hub = FakeHub()
    tools = await remote_tools(hub)

    assert [tool.spec.name for tool in tools] == ["fake__echo"]
    assert tools[0].spec.description == "[fake] Echo it back."
    assert "text" in tools[0].spec.parameters["properties"]
    assert hub.calls == [(LIST_TOOLS, {})]


@pytest.mark.asyncio
async def test_running_one_calls_back_through_the_same_connection() -> None:
    hub = FakeHub()
    registry = ToolRegistry(await remote_tools(hub))

    text, ok = await registry.execute(
        ToolCall(id="c1", name="fake__echo", arguments={"text": "hi"})
    )

    assert (text, ok) == ("answered", True)
    assert hub.calls[-1] == (
        CALL_TOOL,
        {"name": "fake__echo", "arguments": {"text": "hi"}},
    )


@pytest.mark.asyncio
async def test_a_failed_call_is_a_failure_not_a_success_with_bad_news() -> None:
    """The distinction the `ok` flag carries, all the way to the transcript.

    A tool that returned "Error: ..." as an ordinary result would render as a
    call that worked, with a discouraging message under it.
    """

    class Refusing(FakeHub):
        async def call_tool(self, name: str, arguments: Any = None, **_: Any) -> Any:
            if name == CALL_TOOL:
                return answer({"text": "the peer said no", "ok": False})
            return await super().call_tool(name, arguments)

    registry = ToolRegistry(await remote_tools(Refusing()))
    text, ok = await registry.execute(ToolCall(id="c1", name="fake__echo"))

    # Rendered bare — no exception class in front of a message that is already
    # the whole story.  See `slife2.libs.turn.tools.ToolFailed`.
    assert (text, ok) == ("Error: the peer said no", False)


@pytest.mark.asyncio
async def test_a_refusal_with_no_text_still_says_which_tool() -> None:
    class Silent(FakeHub):
        async def call_tool(self, name: str, arguments: Any = None, **_: Any) -> Any:
            if name == CALL_TOOL:
                return answer({"text": "", "ok": False})
            return await super().call_tool(name, arguments)

    registry = ToolRegistry(await remote_tools(Silent()))
    text, ok = await registry.execute(ToolCall(id="c1", name="fake__echo"))

    assert ok is False
    assert "fake__echo" in text


# --- a hub this build cannot read ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "junk",
    [
        {},
        {"tools": "not a list"},
        {"something": "else"},
        # **The shape inside the list, and not only the list.**  A hub from
        # another build may answer with a list of the right length and the wrong
        # contents, and letting one through fails two frames down in `from_wire`
        # — `AttributeError: 'str' object has no attribute 'get'` — which is a
        # traceback about a dict where the deliberate message about a *build*
        # belongs.
        {"tools": ["fake__echo"]},
        {"tools": [None]},
    ],
)
async def test_a_payload_this_build_cannot_read_is_loud(junk: Any) -> None:
    """Never an empty tool list.

    The likely cause is a hub daemon left over from a previous build, and
    quietly handing the model a short list turns a version mismatch into a model
    that has lost its abilities without anyone being told.
    """
    with pytest.raises(ConnectionError, match="slife2 down"):
        await remote_tools(FakeHub(junk=junk))


def test_a_listed_tool_tolerates_what_it_does_not_understand() -> None:
    """`parameters` is the one field with a shape; the rest is text.

    `name` is the exception, and it is not tested here because it is not
    `from_wire`'s to enforce: a nameless element is refused where the list is
    (`remote_tools`), the way a wrongly-typed element is.
    """
    tool = UpstreamTool.from_wire({"name": "a__b", "parameters": "nonsense"})
    assert tool.parameters == {}
    assert (tool.server, tool.tool) == ("", "")


@pytest.mark.asyncio
async def test_a_listed_tool_without_a_name_fails_the_list() -> None:
    """**A nameless tool is one the model can never call, so the list is refused.**

    `UpstreamTool.from_wire` fills every missing field with `""` or `{}`, which
    is a tolerance for text and a silent hole for the one field that is an
    identity: a hub daemon from another build answering `{"tools": [{}]}` used to
    arrive as a tool whose name was the empty string.  The element that broke the
    shape is the evidence of the wrong build, so it is the element that is quoted
    (DESIGN.md §10.1).
    """
    with pytest.raises(ConnectionError, match="slife2 down"):
        await remote_tools(FakeHub(junk={"tools": [{}]}))
    with pytest.raises(ConnectionError, match="slife2 down"):
        await remote_tools(FakeHub(junk={"tools": [{"description": "no name here"}]}))


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", [{}, {"text": "answered"}])
async def test_a_call_this_build_cannot_read_is_a_version_mismatch(junk: Any) -> None:
    """**The call path says what the list path says, and not "the tool said no".**

    An answer `tool_payload` cannot read comes back as an empty mapping, and
    treating that as a refusal renders a hub from another build as a tool that
    failed — a diagnosis pointing at the tool, when the hub is what is wrong.
    The registry never raises, so the model reads the difference in the text:
    the same sentence `remote_tools` raises, naming the build and `slife2 down`,
    rather than "fake__echo failed without saying why".
    """

    class Unreadable(FakeHub):
        async def call_tool(self, name: str, arguments: Any = None, **_: Any) -> Any:
            if name == CALL_TOOL:
                return answer(junk)
            return await super().call_tool(name, arguments)

    tool = (await remote_tools(Unreadable()))[0]
    with pytest.raises(ConnectionError, match="slife2 down"):
        await tool.run({})

    text, ok = await ToolRegistry(await remote_tools(Unreadable())).execute(
        ToolCall(id="c1", name="fake__echo")
    )
    assert ok is False
    assert "slife2 down" in text


# --- the two halves, against each other --------------------------------------


def hub_with_upstream() -> FastMCP:
    """A real hub over a real (in-memory) upstream server."""
    upstream = FastMCP("upstream")

    @upstream.tool
    def echo(text: str) -> str:
        """Echo it back."""
        return text

    config: Config = replace(
        default_config(),
        # `autoload` because this is a test about the hop, not about the load
        # gate: the tool servers here are wanted in the list from the start.
        mcp={
            "fake": ToolServerSettings(name="fake", command="in-memory", autoload=True)
        },
    )
    return build_hub(
        config,
        transports=plugin_transports(config, {"fake": lambda settings: upstream}),
        embedder=StubEmbedder(),
    )


@pytest.mark.asyncio
async def test_the_two_halves_agree_over_a_real_hop() -> None:
    """One round trip, both contracts: the hub's payload and the loop's tools.

    The names, the schemas and the dispatch are all crossing MCP here rather
    than being read off a dict this file wrote itself.
    """
    async with Client(hub_with_upstream()) as hub:
        registry = ToolRegistry(await remote_tools(hub))
        # In the catalogue's order, which is the name's.  **Ours are bare** —
        # a plugin's tools read the same as the hub's own, because there is one
        # set of tools here — and `fake__echo`, an entry under `mcp:`, is the
        # one name that says whose it is.  The hub's four cross this hop like
        # everything else, `_func_tool_unload` included: it is the one
        # `_`-prefixed name a model sees, and the reason is in its own test.
        assert [spec.name for spec in registry.specs] == [
            "_func_tool_unload",
            # `cli-server` records programs already on this machine, and offers
            # the model the tools that edit that record.
            "cli_list",
            "cli_remove",
            "cli_set",
            "cli_set_enabled",
            "fake__echo",
            "func_tool_load",
            # The `mcp_*` and `rest_api_*` fives are here too: each family
            # plugin holds somebody else's servers *and* offers the model the
            # tools that edit its own section, and both arrive by the same road.
            "mcp_list",
            "mcp_list_tools",
            "mcp_remove",
            "mcp_set",
            "mcp_set_enabled",
            "rest_api_list",
            "rest_api_list_tools",
            "rest_api_remove",
            "rest_api_set",
            "rest_api_set_enabled",
            # And `skills-server`: `skill_use` was always the model's, and the
            # four that install, remove and switch a playbook have joined it.
            "skill_list",
            "skill_remove",
            "skill_set",
            "skill_set_enabled",
            "skill_use",
            "tool_search",
            "turn_list",
            "turn_read",
        ]
        text, ok = await registry.execute(
            ToolCall(id="c1", name="fake__echo", arguments={"text": "through"})
        )
        assert (text, ok) == ("through", True)

        # One of ours, through the hub, through the plugin that serves it: two
        # hops and the same answer, which is what makes ours not a special case.
        text, ok = await registry.execute(
            ToolCall(id="c2", name="skill_list", arguments={})
        )
        assert ok is True and json.loads(text)["skills"] == []


@pytest.mark.asyncio
async def test_a_hub_that_is_the_wrong_build_says_so() -> None:
    """The same loudness, reached through a real connection rather than a fake."""
    hub = FastMCP("not the hub")

    @hub.tool
    def list_tools() -> str:
        """Answers, but not with what the agent expects."""
        return "hello"

    async with Client(hub) as client:
        with pytest.raises(ConnectionError, match="slife2 down"):
            await remote_tools(client)


@pytest.mark.asyncio
async def test_the_identity_reaches_the_db_through_the_hub(
    tmp_path, monkeypatch
) -> None:
    """Two hops, and the conversation survives both.

    This is the whole point of the arrangement: the model is handed
    `turn_list` with no `agent` argument to fill in, the loop binds the
    conversation it was built for to the call, the hub forwards it without
    reading it, and the db server answers with *that* conversation's turns.
    Every hop would be individually plausible with the identity dropped — the
    call would still succeed — and the result would be a model reading a
    history that is not its own, or none at all.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    store = build_context(default_config(), embedder=StubEmbedder())

    async with Client(store) as seed:
        for agent, said in (("jack", "jack asked"), ("jill", "jill asked")):
            await seed.call_tool(
                "remember",
                {
                    "agent": agent,
                    "messages": [
                        {"role": "user", "content": said},
                        {"role": "assistant", "content": "…"},
                    ],
                },
            )

    config = default_config()
    hub = build_hub(
        config,
        transports=plugin_transports(config, {"context": lambda settings: store}),
        embedder=StubEmbedder(),
    )

    async with Client(hub) as hub_client:
        offered = await remote_tools(hub_client, ("jack", ""))
        by_name = {tool.spec.name: tool for tool in offered}
        # What the db has that the model may not call never left the hub, and
        # what it may call arrived under the name the db's own code gives it.
        assert "turn_list" in by_name
        assert "turn_read" in by_name
        assert "remember" not in by_name

        payload = json.loads(await by_name["turn_list"].run({}))

    assert [entry["user_message"] for entry in payload["entries"]] == ["jack asked"]


@pytest.mark.asyncio
async def test_the_trim_is_one_tool_with_two_callers() -> None:
    """`_func_tool_unload` is on the hub's API *and* in the model's list.

    Both halves are deliberate.  The agent server calls it before it saves a
    turn — on the API, not through `call_tool`, so a trim never depends on the
    catalogue or the routing being in some particular state — and what it gets
    back is the point: the names of the tools the model has just lost.

    The model has it too, and that is what makes the trim *recordable*: the pair
    the harness writes into the conversation names this tool, and a name the
    request does not declare is a 400 from the Responses and Messages backends.
    So this is the one `_`-prefixed name in the list this hop returns, and the
    model can call it with the names it is done with.
    """
    async with Client(hub_with_upstream()) as hub:
        registry = ToolRegistry(await remote_tools(hub))
        assert "_func_tool_unload" in [spec.name for spec in registry.specs]

        trimmed = await unload_tools(hub)

        # The model's own call, through the registry, like any other tool.
        text, ok = await registry.execute(
            ToolCall(id="c1", name="_func_tool_unload", arguments={})
        )

    assert ok is True, "the budget is within its limit, so nothing was refused"
    assert json.loads(text)["unloaded"] == [], "said, not silent"

    assert trimmed["unloaded"] == [], "a hundred is room for everything here"
    assert json.loads(trimmed["text"])["unloaded"] == []


@pytest.mark.asyncio
async def test_a_trim_that_cannot_happen_does_not_fail_the_turn() -> None:
    """Bookkeeping runs after the answer, so it cannot be what fails it.

    A hub from an older build has no such tool and answers with a refusal; a hub
    that is gone cannot answer at all.  Either way the turn is over — and the
    list is asked for again before the next model call, which is where a missing
    hub becomes impossible to miss.
    """

    class Refuses:
        async def call_tool(self, *_: object, **__: object) -> object:
            raise ToolError("Unknown tool: '_func_tool_unload'")

    assert await unload_tools(Refuses()) == {
        "unloaded": [],
        "refused": [],
        "not_loaded": [],
        "text": "",
    }


@pytest.mark.asyncio
async def test_an_unreadable_trim_answer_is_not_a_trim_that_took_nothing() -> None:
    """**A hub that answered, and unreadably, is not a hub that unloaded none.**

    `return payload or empty` turned the one answer this pair of functions is
    built to refuse — `tool_payload`'s `{}`, which is what a shape this build
    does not know reads as — into the well-formed "nothing was unloaded, no
    sentence" the `except` above is for.  `trim_answer` then saw an honest
    no-op, wrote no pair, and the caller logged nothing: the model went on
    believing it held the tools that had gone, which is the confusion
    `trim_answer` was written to remove and the sibling paths pin with a test.
    A call that could not be *made* is still the empty answer; an answer that
    arrived and cannot be *read* is damage, and damage is the caller's to
    refuse.
    """

    class Unreadable(FakeHub):
        async def call_tool(self, name: str, arguments: Any = None, **_: Any) -> Any:
            if name == FUNC_TOOL_UNLOAD:
                # A hub from another build answering in prose: no payload this
                # build can read out of it, which is the empty mapping.
                return answer("the tool list is within its budget")
            return await super().call_tool(name, arguments)

    found = await unload_tools(Unreadable())

    assert found == {}, "the unreadable answer is passed on as it is"
    with pytest.raises(ConnectionError, match="names it unloaded"):
        trim_answer(found)


def test_the_hub_reaches_the_store_and_nothing_below_it() -> None:
    """各司其职, as a property of the import graph rather than a promise.

    This guard used to be its own opposite — "the hub holds no database", when
    the catalogue was served by a plugin and the hub asked for it over MCP.  The
    catalogue is a *file this process opens* now, so the hub holds one, and what
    is worth guarding is where the line moved to rather than that it moved:
    `slife2.libs.store.catalogue` is a store, and the hub imports the store and not the
    machinery under it.  A `sqlite3` import here would be the hub writing SQL of
    its own, which is the overlap this test exists to catch — the same failure
    it always caught, one level down.

    **Read off the module, not off its text**, which is what the guard used to
    do: "no sqlite3" read off a source file is as true of a comment as of an
    import, and "the store, not the module" was a claim about a spelling rather
    than about what the hub reaches.  An import is a name bound in the module's
    own namespace and a class shared with the rest of the build, so those are
    what is asserted — and a `ToolStore` that stopped being the catalogue's own
    would be a second catalogue, which is the drift this is really about.
    """
    import slife2.libs.store.catalogue as catalogue
    import slife2.plugins.toolhub.server as hub

    assert "sqlite3" not in vars(hub), "no SQL of the hub's own"
    assert "slife2" not in vars(hub), "the store, and not the package around it"
    assert hub.ToolStore is catalogue.ToolStore, "the one store, and this process's"
