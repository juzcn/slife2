"""What the TUI puts on the wire.

`MCPAgentClient` is the one client with nothing else watching it — every TUI
test injects a stand-in — and that blind spot has now cost two parameters:
`--model` reached the window title and nothing else, and an image was read,
turned into a `data:` URL, handed to this class, and dropped.  Both were
invisible from either end, because the client honoured its own signature and the
server honoured its own schema.

So this is a test of the *payload*: which tool is called, which keys go out, and
what is in them.  The transport underneath is stubbed, because the wire is not
the part that was wrong.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from slife2.libs.plugin import protocol as protocol_module
from slife2.plugins.tui.server import ATTACH, Handover, encode
from slife2.tui.client import MCPAgentClient, MCPWindowClient

pytestmark = pytest.mark.unit


class FakeTransport:
    """The MCP client `MCPAgentClient` would have built, with the wire stubbed."""

    def __init__(
        self, tools: tuple[str, ...] = ("send_message",), *, name: str | None = None
    ) -> None:
        self.tools = tools
        #: What the handshake reported this server calls itself.  `None` is a
        #: server that reported no name — the case `identifies` falls back to
        #: `tools` for — so the default exercises that path.
        self.server_info = SimpleNamespace(name=name) if name else None
        #: Every (tool, arguments) pair handed to `call_tool`, in order.
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> FakeTransport:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_tools(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=name) for name in self.tools]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> SimpleNamespace:
        # Copied, because a real transport serialises the payload and a fake
        # that kept the reference would let a later mutation show up in a
        # recorded call.
        self.sent.append((name, copy.deepcopy(arguments)))
        if name == "model_switch":
            # A string rather than a mapping, which is what the tool returns
            # and what FastMCP hands back wrapped in `data` — the shape the
            # client reads first.
            return SimpleNamespace(data=f"`default:` is now `{arguments['ref']}`.")
        return SimpleNamespace(data={"text": "ok", "model": "fake/model"})


def install(monkeypatch, transport: FakeTransport | FakeWindowTransport) -> None:
    """Point the client at a stubbed transport instead of a URL.

    Patched in `slife2.libs.plugin.protocol` rather than here: the client no longer builds
    its own `Client` — `open_server` does, for every plugin in this system —
    so that module is where the construction happens and therefore where a test
    has to stand in for it.  The name is `Peer` because that is what
    `open_server` builds: a `Client` that says which home each call is for.
    """
    monkeypatch.setattr(protocol_module, "Peer", lambda *a, **k: transport)


def sent(transport: FakeTransport, tool: str) -> list[dict[str, Any]]:
    return [arguments for name, arguments in transport.sent if name == tool]


@pytest.mark.asyncio
async def test_a_turn_names_the_client_it_speaks_for(monkeypatch) -> None:
    """The client id *is* the conversation, so it goes out with every message.

    There is no second call to make first: the server starts a conversation
    under a key it has not seen, so naming the key is the whole of addressing
    one.  That is what removed the handle — and with it the "your conversation
    is gone" path every caller used to need.
    """
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient(
        "http://test/mcp", agent="jack", model="deepseek/deepseek-flash"
    )

    await client.connect()
    await client.run_turn("hi", lambda event: None, images=["data:image/png;base64,AA"])

    (arguments,) = sent(transport, "send_message")
    assert arguments == {
        "agent": "jack",
        "subagent": "",
        "prompt": "hi",
        "channel": "tui",
        "model": "deepseek/deepseek-flash",
        "images": ["data:image/png;base64,AA"],
        # And that this caller can be asked a question mid-turn: a window draws
        # the `_approve` prompt and answers it, so its turns wait for a person
        # rather than running unasked (`slife2.tui.app`, DESIGN.md §8.8).
        "watched": True,
    }
    assert client.client_id == ("jack", "")


@pytest.mark.asyncio
async def test_a_payload_says_nothing_it_does_not_have_to(monkeypatch) -> None:
    """An absent key and an empty one mean the same thing to the server — it
    chooses — so the payload sends neither rather than both spellings."""
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp", agent="jack")

    await client.connect()
    await client.run_turn("hi", lambda event: None)

    (arguments,) = sent(transport, "send_message")
    assert "model" not in arguments
    assert "images" not in arguments
    # The channel is not optional: this client exists to serve one window, and
    # that is a fact about the caller rather than something it may lack.
    assert arguments["channel"] == "tui"


@pytest.mark.asyncio
async def test_a_subagent_name_is_carried_as_part_of_the_id(monkeypatch) -> None:
    """The second half of the id is what makes a worker a separate conversation.

    A TUI is never a worker, so it does not pass one — but the id it sends has
    the same shape either way, which is what lets every server key on one thing.
    """
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp", agent="jack", subagent="helper")

    await client.connect()
    await client.run_turn("hi", lambda event: None)

    assert client.client_id == ("jack", "helper")
    assert sent(transport, "send_message")[0]["subagent"] == "helper"


@pytest.mark.asyncio
async def test_switching_names_the_model_and_adopts_it(monkeypatch) -> None:
    """The tool the model calls, and the reference every later turn carries.

    **Adopting it is what makes a switch outlive the conversation it was made
    in.**  The model goes out with every message and the server reads it when a
    conversation *starts*, so a client that kept the model it was built with
    would bring the old one back the moment that conversation was started again
    — after the server let an idle one go, or in the next window.
    """
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp", agent="jack", model="local/big")
    await client.connect()

    answer = await client.switch_model("local/small")

    assert sent(transport, "model_switch") == [{"ref": "local/small"}]
    assert "local/small" in answer, "the server's own sentence, not a paraphrase"
    await client.run_turn("hi", lambda event: None)
    assert sent(transport, "send_message")[-1]["model"] == "local/small"


@pytest.mark.asyncio
async def test_a_refused_switch_is_not_adopted(monkeypatch) -> None:
    """A refusal is an answer rather than an error — and it is not a switch.

    `model_switch` says no in a sentence the model reads like any other answer,
    so nothing raises and the caller has to read it.  Carrying the reference
    anyway would pin the *next* conversation to a model this client has just
    been told does not exist.
    """
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp", agent="jack", model="local/big")
    await client.connect()

    async def refuse(name, arguments, **kwargs):
        if name == "model_switch":
            return SimpleNamespace(data="[refused] `local/nope` is not a model.")
        return await FakeTransport.call_tool(transport, name, arguments, **kwargs)

    monkeypatch.setattr(transport, "call_tool", refuse)

    answer = await client.switch_model("local/nope")

    assert answer.startswith("[refused]")
    await client.run_turn("hi", lambda event: None)
    assert sent(transport, "send_message")[-1]["model"] == "local/big"


@pytest.mark.asyncio
async def test_a_failed_turn_keeps_the_connection(monkeypatch) -> None:
    """A `ToolError` is the server answering, not the transport dying.

    Dropping the connection here would turn a bad API key into a reconnect storm
    on every retry.
    """
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp", agent="jack")
    await client.connect()

    async def explode(name, arguments, **kwargs):
        if name == "send_message":
            raise ToolError("the API key did not resolve")
        return await FakeTransport.call_tool(transport, name, arguments, **kwargs)

    monkeypatch.setattr(transport, "call_tool", explode)

    with pytest.raises(ToolError, match="API key"):
        await client.run_turn("hi", lambda event: None)
    assert client.client_id == ("jack", "")


@pytest.mark.asyncio
async def test_an_answer_without_text_is_refused_rather_than_drawn_empty(
    monkeypatch,
) -> None:
    """A turn whose answer is not `text` is a failure, not a finished one.

    `send_message`'s answer is a contract — the window draws `text` as the
    answer — and a payload without it was read as `""`: a question with no answer
    and no error, which the window presents as a completed turn (DESIGN.md
    §10.1: a value that cannot be read is not a value that is absent).  The
    shape fails loudly, quoting what did come back, and the connection stays
    because the server answered.
    """
    transport = FakeTransport()
    install(monkeypatch, transport)

    async def no_text(name, arguments, **kwargs):
        if name == "send_message":
            return SimpleNamespace(data={"model": "fake/model"})
        return await FakeTransport.call_tool(transport, name, arguments, **kwargs)

    monkeypatch.setattr(transport, "call_tool", no_text)
    client = MCPAgentClient("http://test/mcp", agent="jack")
    await client.connect()

    with pytest.raises(ConnectionError) as raised:
        await client.run_turn("hi", lambda event: None)

    assert "fake/model" in str(raised.value), "the answer is quoted, not summarised"
    assert client.connected, "the server answered; the connection stays"


@pytest.mark.asyncio
async def test_a_transcript_without_turns_is_not_an_empty_history(monkeypatch) -> None:
    """A blank history and a conversation that never ran are different answers.

    A name with no history is answered with a real, empty `turns`; a reply whose
    `turns` cannot be read is damage, and reading it as `[]` left the window
    blank with `_restore` marked done for the rest of the session.  It raises
    instead, so the retry path runs (DESIGN.md §10.1: a failure is not a
    verdict).
    """
    transport = FakeTransport()
    install(monkeypatch, transport)

    async def no_turns(name, arguments, **kwargs):
        if name == "transcript":
            return SimpleNamespace(data={"messages": []})
        return await FakeTransport.call_tool(transport, name, arguments, **kwargs)

    monkeypatch.setattr(transport, "call_tool", no_turns)
    client = MCPAgentClient("http://test/mcp", agent="jack")
    await client.connect()

    with pytest.raises(ConnectionError) as raised:
        await client.transcript()

    assert "messages" in str(raised.value), "the answer is quoted, not summarised"
    assert client.connected, "the server answered; the connection stays"


@pytest.mark.asyncio
async def test_an_unreadable_switch_answer_is_not_a_successful_switch(
    monkeypatch,
) -> None:
    """`""` is not a refusal, so a shape this build cannot read is raised.

    `switch_model` adopts the reference unless the answer starts with the
    refusal prefix — so an unreadable answer read as `""` read as a *successful*
    switch: the client moved its ref and the bar described a model that is not
    the one answering.  The unknown shape fails, and the model in hand is kept
    (DESIGN.md §10.1: a failure is not a verdict).
    """
    transport = FakeTransport()
    install(monkeypatch, transport)

    async def odd_shape(name, arguments, **kwargs):
        if name == "model_switch":
            return SimpleNamespace(data={"switched": True})
        return await FakeTransport.call_tool(transport, name, arguments, **kwargs)

    monkeypatch.setattr(transport, "call_tool", odd_shape)
    client = MCPAgentClient("http://test/mcp", agent="jack", model="local/big")
    await client.connect()

    with pytest.raises(ConnectionError):
        await client.switch_model("local/small")

    await client.run_turn("hi", lambda event: None)
    assert sent(transport, "send_message")[-1]["model"] == "local/big", (
        "a ref was adopted from an answer nobody could read"
    )


@pytest.mark.asyncio
async def test_a_management_call_drops_the_link_only_when_the_transport_does(
    monkeypatch,
) -> None:
    """One statement of the rule `model_switch`, `transcript` and `answer_approval` share.

    All three are ordinary asks of our own daemon, and each used to spell the
    same `try`/`except` out for itself — three copies of one rule, each a place
    the next caller could get it subtly wrong.  So the rule is pinned through one
    of them (`_call_tool`): a `ToolError` is the server *answering* and the link
    stays; anything else is the transport itself, and dropping the client is what
    lets the app's lazy retry reconnect instead of believing it is connected to a
    server nothing is talking to.
    """
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp", agent="jack")
    await client.connect()

    async def refuse(name, arguments, **kwargs):
        if name == "transcript":
            raise ToolError("the store is busy")
        return await FakeTransport.call_tool(transport, name, arguments, **kwargs)

    monkeypatch.setattr(transport, "call_tool", refuse)
    with pytest.raises(ToolError, match="busy"):
        await client.transcript()
    assert client.connected, "the server answered; the connection stays"

    async def die(name, arguments, **kwargs):
        raise ConnectionError("the socket went away")

    monkeypatch.setattr(transport, "call_tool", die)
    with pytest.raises(ConnectionError, match="socket"):
        await client.transcript()
    assert not client.connected, "the transport is gone; so is the client"


@pytest.mark.asyncio
async def test_a_server_that_is_not_ours_is_refused(monkeypatch) -> None:
    """The URL may point at some other MCP server, and saying so beats a turn
    that fails later with a message about an unknown tool.

    This transport reports no name, so the check falls back to the tool list —
    which is the weaker check, and the one that has to keep working.
    """
    install(monkeypatch, FakeTransport(tools=("stream_chat",)))
    client = MCPAgentClient("http://test/mcp")

    with pytest.raises(ConnectionError, match="not slife2-agent"):
        await client.connect()


@pytest.mark.asyncio
async def test_a_server_that_says_it_is_something_else_is_refused(monkeypatch) -> None:
    """The name the handshake reports decides on its own.

    It is not a hint to combine with the tool list: a server that calls itself
    something else is not ours even if it happens to expose a tool by that name,
    which is exactly what a *stale* build of one of our own servers looks like.
    """
    install(monkeypatch, FakeTransport(name="someone-elses-server"))
    client = MCPAgentClient("http://test/mcp")

    with pytest.raises(ConnectionError, match="not slife2-agent"):
        await client.connect()


@pytest.mark.asyncio
async def test_a_named_server_is_accepted_without_listing_tools(monkeypatch) -> None:
    """The common case: the handshake already answered, so nothing more is asked.

    `tools` is deliberately wrong here — if the name matches, the tool list is
    never consulted, and asserting that is how the round trip stays saved.
    """
    transport = FakeTransport(tools=("nothing_like_it",), name="slife2-agent")
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp")

    await client.connect()
    await client.run_turn("hi", lambda event: None)
    assert sent(transport, "send_message")[0]["prompt"] == "hi"


# --- and the other direction: the window's own inbox --------------------------


class FakeWindowTransport:
    """The MCP client `MCPWindowClient` would have built, with the wire stubbed.

    It keeps the *options* as well as the payload, because the two facts that
    matter down here are not in the arguments at all: that a progress handler was
    supplied, and that the call is not on a clock.
    """

    def __init__(
        self, tools: tuple[str, ...] = (ATTACH,), *, name: str | None = None
    ) -> None:
        self.tools = tools
        self.server_info = SimpleNamespace(name=name) if name else None
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.options: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeWindowTransport:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_tools(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=name) for name in self.tools]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> SimpleNamespace:
        self.sent.append((name, copy.deepcopy(arguments)))
        self.options.append(kwargs)
        return SimpleNamespace(data=None)

    async def push(self, handover: Handover) -> None:
        """What the plugin does to a window: one progress notification."""
        handler = self.options[-1]["progress_handler"]
        await handler(handover.ticket, None, encode(handover))


@pytest.mark.asyncio
async def test_attaching_names_the_agent_and_asks_for_the_stream(monkeypatch) -> None:
    """Three things go out, and each is load-bearing.

    The **agent**, because that is what picks the session — the address is one
    for every window now.  The **progress handler**, because the plugin's push is
    a silent no-op without one: a window that omitted it would be handed nothing
    while every `submit` still succeeded.  And **no timeout**, because the call
    lasts as long as the window does, so the default would detach a terminal that
    had merely been open a while.
    """
    transport = FakeWindowTransport()
    install(monkeypatch, transport)
    client = MCPWindowClient("http://test/mcp", agent="jack")

    await client.connect()
    await client.attach(lambda handover: None)

    assert transport.sent == [(ATTACH, {"agent": "jack"})]
    (options,) = transport.options
    assert options["progress_handler"] is not None
    assert options["timeout"] is None


@pytest.mark.asyncio
async def test_a_pushed_message_arrives_as_a_handover(monkeypatch) -> None:
    """The words and their channel, decoded once and handed straight on."""
    transport = FakeWindowTransport()
    install(monkeypatch, transport)
    client = MCPWindowClient("http://test/mcp", agent="jack")
    seen: list[Handover] = []

    await client.connect()
    await client.attach(seen.append)
    await transport.push(Handover(ticket=4, text="hi", channel="wechat"))

    assert seen == [Handover(ticket=4, text="hi", channel="wechat")]


@pytest.mark.asyncio
async def test_a_line_that_is_not_a_handover_is_dropped(monkeypatch) -> None:
    """The stream is somebody else's too, in principle.

    A notification this build has no word for is a line not shown rather than a
    window that dies on it — `slife2.libs.turn.events.decode`'s rule, for its reason.
    """
    transport = FakeWindowTransport()
    install(monkeypatch, transport)
    client = MCPWindowClient("http://test/mcp", agent="jack")
    seen: list[Handover] = []

    await client.connect()
    await client.attach(seen.append)
    handler = transport.options[-1]["progress_handler"]
    await handler(1, None, '{"t":"text","d":"someone elses"}')
    await handler(2, None, None)

    assert seen == []


@pytest.mark.asyncio
async def test_an_inbox_that_is_not_ours_is_refused(monkeypatch) -> None:
    """A `tui` address pointed at anything else fails here, by name."""
    install(monkeypatch, FakeWindowTransport(tools=("stream_chat",)))
    client = MCPWindowClient("http://test/mcp", agent="jack")

    with pytest.raises(ConnectionError, match="not slife2-tui"):
        await client.connect()
