"""Test doubles.

Five seams get faked, and each one exists because something real and slow sits
behind it: the model (an HTTP call to a provider), the embedding model (another
one), the agent server (a socket), the window's own inbox (another socket, and
one the window *holds open* rather than calls), and the observer (nothing — but a
recorder is how a test reads what happened).

Plus one that is not a seam but a *shape*: the set of plugins a toolhub will
ask for tools.  It is here because three test modules need it and none of them
owns it — see `plugin_transports`.
"""

from __future__ import annotations

import ast
import asyncio
import operator
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

from slife2.libs.host.config import Config
from slife2.libs.model.base import Chunk, Stream
from slife2.libs.turn.events import TurnEvent
from slife2.libs.turn.messages import Message, StreamChatResult, ToolSpec
from slife2.libs.turn.watch import WatchItem
from slife2.plugins.tui.server import NOTE, Handover
from slife2.tui.client import TurnReply
from slife2.tui.widgets import WINDOW_CHANNEL


@dataclass
class ListObserver:
    """Records every event it is handed, in order.

    The assertion surface for loop tests: the loop's contract is the sequence of
    events it emits, and comparing against a list is how that contract is stated
    without a mock library.
    """

    events: list[TurnEvent] = field(default_factory=list)

    async def on_event(self, event: TurnEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        """Event type names, for asserting a shape without the payloads."""
        return [type(e).__name__ for e in self.events]


@dataclass
class RaisingObserver:
    """An observer that fails on every event.

    Exists to prove the loop survives a broken UI: a turn that returns the right
    answer while nobody is watching is the behaviour under test.
    """

    async def on_event(self, event: TurnEvent) -> None:
        raise RuntimeError("observer exploded")


@dataclass
class ScriptedTurn:
    """One scripted model response: what streams, and what the result is.

    `chunks` and `result` are separate because they are separate on the real
    wire — and keeping them separate in tests means a script *can* disagree with
    itself, which is how the "the result is authoritative" rule gets tested.
    """

    result: StreamChatResult
    chunks: list[Chunk] = field(default_factory=list)
    #: Seconds to sleep before yielding each chunk, for cancellation tests.
    delay: float = 0.0


class FakeBackend:
    """An :class:`~slife2.libs.model.base.LLMBackend` that replays a script.

    Each call to :meth:`stream` consumes the next scripted turn.  Running off
    the end raises rather than looping or returning empty: a test that makes
    more model calls than it scripted has a bug, and a silent empty response
    would turn that bug into a confusing assertion failure somewhere else.
    """

    def __init__(self, *turns: ScriptedTurn, name: str = "fake") -> None:
        self.name = name
        self._turns = list(turns)
        #: Every (messages, tools) pair the loop passed, for asserting what the
        #: loop actually sent — history handling is invisible otherwise.
        self.calls: list[tuple[list[Message], list[ToolSpec]]] = []

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Stream:
        self.calls.append((list(messages), list(tools)))
        if not self._turns:
            raise AssertionError(
                f"FakeBackend ran out of scripted turns after {len(self.calls)} call(s)"
            )
        turn = self._turns.pop(0)
        return Stream(chunks=self._chunks(turn), result=self._result(turn))

    async def _chunks(self, turn: ScriptedTurn) -> AsyncIterator[Chunk]:
        for chunk in turn.chunks:
            if turn.delay:
                await asyncio.sleep(turn.delay)
            yield chunk

    async def _result(self, turn: ScriptedTurn) -> StreamChatResult:
        # Awaited after the chunk loop by the real caller; the sleep here is
        # what lets a cancellation test interrupt a turn that is still going.
        if turn.delay:
            await asyncio.sleep(turn.delay)
        return turn.result


def text_turn(
    text: str,
    *,
    chunks: list[str] | None = None,
    delay: float = 0.0,  # timeouts: the value this test is built around, not a budget
):  # timeouts: the value this test is built around, not a budget
    """A scripted turn that answers with text and calls no tools.

    `chunks` defaults to splitting the text one character at a time, which is
    the worst case for the streaming path and therefore the useful default.
    """
    pieces = chunks if chunks is not None else list(text)
    return ScriptedTurn(
        result=StreamChatResult(text=text, stop_reason="stop"),
        chunks=[Chunk(text=p) for p in pieces],
        delay=delay,
    )


class FakeAgentClient:
    """An :class:`~slife2.tui.client.AgentClient` that replays turn events.

    Takes a callable rather than a fixed list so a test can script a different
    response per prompt, and can raise to exercise the TUI's error path.
    """

    def __init__(
        self,
        respond: Callable[[str, Callable[[TurnEvent], None]], str],
        *,
        connect_error: Exception | None = None,
    ) -> None:
        self._respond = respond
        self._connect_error = connect_error
        self.connected = False
        self.prompts: list[str] = []
        #: The channel each prompt was sent on, so a test can prove a message
        #: handed over by another channel kept its own.
        self.channels: list[str] = []
        #: The images sent with each prompt, so a test can assert the attachment
        #: reached the client rather than only that the file was read.
        self.images: list[list[str]] = []
        #: The receipt each prompt was sent with, so a test can prove a window
        #: that draws a cut-in asks the server to echo the id it will match.
        self.receipts: list[str] = []
        #: What `switch_model` was asked for, in order, so a test can prove the
        #: picker's choice reached the server rather than only that the picker
        #: closed.
        self.switches: list[str] = []
        #: What `switch_model` answers with — the agent server's facts, which is
        #: what the real tool returns (`default`, `previous`, `moved`).
        self.switch_answer: dict[str, Any] = {
            "default": "local/small",
            "previous": "local/big",
            "moved": 0,
        }
        #: What `switch_model` raises instead, when a test wants the other half.
        #: **A refusal is a raised `ToolError`**, which is the contract every
        #: tool in this system keeps, and the one path that must not move the
        #: bar.
        self.switch_error: Exception | None = None
        #: What `transcript` answers with — the previous conversation, as the
        #: window opened after a restart sees it.  Empty is the real answer for
        #: a name that has never run, and so is the default.
        self.turns: list[dict[str, Any]] = []
        #: How many times the history was read, so a test can prove a window
        #: reads it once rather than once per connection.
        self.reads = 0
        #: What `answer_approval` was called with, in order — the half of the
        #: `_approve` handshake that leaves the window (`slife2.tui.app`).
        self.answers: list[tuple[str, bool]] = []
        #: What `subagents` answers with — the directories the agent server
        #: would list.  Empty is the real answer for an agent that made none.
        self.subagents_answer: list[dict[str, Any]] = []

    async def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    async def transcript(self) -> list[dict[str, Any]]:
        if not self.connected:
            raise ConnectionError("not connected")
        self.reads += 1
        return list(self.turns)

    async def close(self) -> None:
        self.connected = False

    async def switch_model(self, ref: str) -> dict[str, Any]:
        """Answer the way the server does: the facts, or a raised refusal.

        It needs a connection for the same reason `run_turn` does — the real one
        reaches for a client it may no longer have, and that is the failure the
        app's lazy retry is built on.
        """
        if not self.connected:
            raise ConnectionError("not connected")
        self.switches.append(ref)
        if self.switch_error is not None:
            raise self.switch_error
        return dict(self.switch_answer)

    async def answer_approval(self, call_id: str, approved: bool) -> str:
        """Answer the way the server does: the sentence is empty when taken.

        Empty means a waiting turn took it; a sentence means nothing was
        waiting — the turn ended first, which is a race and not an error.
        """
        if not self.connected:
            raise ConnectionError("not connected")
        self.answers.append((call_id, approved))
        return ""

    async def subagents(self) -> list[dict[str, Any]]:
        """The list the agent server would answer with, scripted by the test."""
        if not self.connected:
            raise ConnectionError("not connected")
        return [dict(one) for one in self.subagents_answer]

    async def run_turn(
        self,
        prompt: str,
        on_event: Callable[[TurnEvent], None],
        *,
        channel: str = WINDOW_CHANNEL,
        images: list[str] | None = None,
        receipt: str = "",
    ) -> TurnReply:
        """Run one scripted turn, keeping the real client's contract.

        Two halves of that contract matter to the TUI and neither is obvious, so
        both are modelled here: a turn needs a connection (the real one would
        reach for a client it no longer has), and a turn that fails for any
        reason other than a refusal drops it — which is what `run_turn`'s own
        `except` does, and what the app's retry is built on.

        The reply is a `TurnReply`, because the app draws on whether a running
        turn folded this message in.  A scripted turn runs its own, so the flag
        is false; a test that wants the other half injects the cut-in event and
        returns `TurnReply(answer, injected=True)`.
        """
        if not self.connected:
            raise ConnectionError("not connected")
        self.prompts.append(prompt)
        self.channels.append(channel)
        self.images.append(images or [])
        self.receipts.append(receipt)
        try:
            return TurnReply(self._respond(prompt, on_event))
        except ToolError:
            # The server answered and refused *this* turn; the link is fine.
            raise
        except Exception:
            self.connected = False
            raise


class FakeWatchClient:
    """A :class:`~slife2.tui.client.WatchClient` that plays a scripted stream.

    `watch` hands every scripted item to the callback and then **holds the call
    open**, like the real one — a fake that returned would let the screen's
    worker finish while the view was still up and hide exactly the lifetime
    this class exists to exercise.  `finish` is for the other ending: a server
    that closed the conversation, whose call does return.  A test that wants to
    push something later calls `send`.
    """

    def __init__(
        self,
        items: tuple[WatchItem, ...] = (),
        *,
        finish: bool = False,
        connect_error: Exception | None = None,
    ) -> None:
        self.items = list(items)
        self.finish = finish
        self.connect_error = connect_error
        self.connected = False
        self.closed = False
        #: Set once `watch` has been entered, so a test can wait for the screen's
        #: worker rather than sleep on it.
        self.watching = asyncio.Event()
        #: The handler `watch` was handed, so a test can push live items.
        self.on_item: Callable[[WatchItem], None] | None = None

    async def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def close(self) -> None:
        self.connected = False
        self.closed = True

    async def watch(self, on_item: Callable[[WatchItem], None]) -> None:
        if not self.connected:
            raise ConnectionError("not connected")
        self.on_item = on_item
        self.watching.set()
        for item in self.items:
            on_item(item)
        if self.finish:
            return
        # Held rather than returned, for `FakeWindowClient.attach`'s reason:
        # the real call is the view's lifetime.
        await asyncio.Event().wait()

    def send(self, item: WatchItem) -> None:
        """Push one item, as the server's progress stream does."""
        assert self.on_item is not None, "nothing has started watching"
        self.on_item(item)


class FakeWindowClient:
    """A :class:`~slife2.tui.client.WindowClient` that pushes on demand.

    The *other* direction from `FakeAgentClient`: this one is how a test plays
    the plugin, handing the window a message over the call it holds open.  What
    it records is what the window asked for, because "which agent am I" is the
    only thing a window says to its inbox.
    """

    def __init__(self, *, connect_error: Exception | None = None) -> None:
        self._connect_error = connect_error
        self.connected = False
        #: The handler `attach` was handed, so a test can push a message — or
        #: several — down the session the way the plugin does.
        self.on_handover: Callable[[Handover], None] | None = None
        #: Set once `attach` has been entered, which is what a test waits on
        #: before pushing: the real call blocks, so "has it started" is a fact
        #: the fake has to say by hand.
        self.attaching = asyncio.Event()

    async def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def attach(self, on_handover: Callable[[Handover], None]) -> None:
        if not self.connected:
            raise ConnectionError("not connected")
        self.on_handover = on_handover
        self.attaching.set()
        # Held rather than returned, like the real one: a fake that came back
        # would let a test pass while the app's worker spun on a dead session.
        await asyncio.Event().wait()

    def push(self, text: str, channel: str = "wechat") -> None:
        """Hand the window one message, as the plugin's push does."""
        assert self.on_handover is not None, "nothing has attached"
        self.on_handover(Handover(ticket=1, text=text, channel=channel))

    def push_note(self, text: str) -> None:
        """Draw one harness line in the window, as the plugin's `note` does.

        The same road as `push` and no channel, which is what a note is: it is
        the harness talking, not somebody on a channel.
        """
        assert self.on_handover is not None, "nothing has attached"
        self.on_handover(Handover(ticket=2, text=text, channel="", kind=NOTE))


class FakeMeshClient:
    """A :class:`~slife2.tui.client.MeshClient` that joins on demand.

    The third direction again: this one is how a test plays the A2A mesh, holding
    the window's agent online for as long as the window holds the call.  What it
    records is what went wrong, because a successful join is a call that does not
    come back — so `holding` is the whole of what a test can wait on.
    """

    def __init__(self, *, connect_error: Exception | None = None) -> None:
        self._connect_error = connect_error
        self.connected = False
        #: Set once `hold` has been entered: the real call blocks until the
        #: window goes away, so "am I on the mesh" is a fact the fake says by
        #: hand, exactly as `FakeWindowClient.attaching` does for the inbox.
        self.holding = asyncio.Event()
        #: How many times the window tried, which is what says whether a refusal
        #: was retried rather than given up on.
        self.attempts = 0

    async def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def hold(self) -> None:
        self.attempts += 1
        if not self.connected:
            raise ConnectionError("not connected")
        self.holding.set()
        # Held rather than returned, for the reason `attach` is: a fake that came
        # back would let a test pass while the app's worker spun on a dead mesh.
        await asyncio.Event().wait()


class RefusingMeshClient(FakeMeshClient):
    """A mesh that answers, and says no — a name another agent is holding.

    The one failure the window draws a line for, and the one it must not draw
    once per attempt: the reason does not change between them.
    """

    async def hold(self) -> None:
        self.attempts += 1
        raise ToolError("agent name 'jack' is already in use on default/default")


@dataclass
class StubEmbedder:
    """A deterministic stand-in for an embedding model.

    A bag of words over a fixed vocabulary — one slot per word, and one more
    slot for "none of them" — so that text sharing no word with the query is
    *orthogonal* to it rather than merely small.  That distinction is the whole
    point of the double: a nearly-zero vector points in almost the same
    direction as any single-word vector, so a stub that fell back to one ranks
    nonsense first and a test built on it passes for the wrong reason.

    `identity` and `dimension` are the two facts a store reads before it can
    build an index, and `calls` records every request so a test can prove a
    rebuild re-embedded rather than only re-recorded.
    """

    vocab: tuple[str, ...] = ("工具", "trump", "计算", "测试")
    identity: str = "stub:one"
    max_chars: int = 8000
    calls: list[list[str]] = field(default_factory=list)

    @property
    def dimension(self) -> int:
        """One slot per word, plus the one that means "none of them"."""
        return len(self.vocab) + 1

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            counts = [float(lowered.count(word)) for word in self.vocab]
            nowhere = not any(counts)
            counts.append(1.0 if nowhere else 0.0)
            vectors.append(counts)
        return vectors


async def keep_the_context(
    agent: str, subagent: str, model: str, messages: list[Any], prompt: str
) -> str:
    """A discriminator that answers "keep what is in hand" — `{}`.

    **The context plugin's other real seam, and the one that is easy to leave
    open.**  `build_server` takes `ask`, and an un-injected one is a *real model
    call*; these tests take their addresses from the config, so a plugin built
    without it reaches whatever is listening on the model server's port — which,
    on a machine with the daemons up, is a real provider.  What the rebuild then
    does is up to that model, and `{"context": "clear"}` empties the list: the
    next turn runs on its own message and nothing else, so a test asserting that
    the context survives fails for a reason that has nothing to do with the
    server under test.  Intermittent, because the answer is.

    `{}` and not a failure: "a decision that asks for exactly what is in hand
    rebuilds nothing at all" is the behaviour those tests are written against,
    and it is the same default `Replies` falls back to in
    `tests/test_context_server.py`, which varies the *answer* rather than the
    seam.
    """
    return "{}"


@dataclass
class FailingEmbedder:
    """An embedding endpoint that is down.

    What it exists to prove is the save path's contract: a save that raises has
    to be a save that stored nothing.
    """

    identity: str = "stub:broken"
    dimension: int = 5
    max_chars: int = 8000
    message: str = "the embedding endpoint is down"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError(self.message)


def upstream_server() -> FastMCP:
    """A stand-in for somebody else's MCP server.

    **Here rather than in the module that used to own it.**  `test_toolhub.py`
    defined it and `tests/plugins/agent/test_server.py` reached for it with a
    bare `from test_toolhub import …` — an import that only resolves once that
    file's *directory* has been put on `sys.path`, which pytest does when it
    collects the module.  In a full run it always had been; in a targeted run
    (`pytest tests/plugins/agent`) it had not, and the test died with
    `ModuleNotFoundError`.  A double two modules need belongs where the other
    shared ones are.
    """
    server = FastMCP("upstream")

    @server.tool
    def echo(text: str) -> str:
        """Echo it back."""
        return text

    @server.tool
    def boom() -> str:
        """Always fails, the way a real tool refuses a request."""
        raise ValueError("the peer said no")

    # A name a provider rejects, which is what the sanitiser exists for.  A real
    # one arrives this way: nothing in MCP stops an upstream from calling a tool
    # whatever it likes.
    server.add_tool(
        Tool.from_function(lambda: "ok", name="read.file", description="Dotted.")
    )
    return server


#: The `slife2.yaml` every test home starts with.  **`providers:` is the one
#: section a file does not inherit** (SDD §4.1), so a home needs at least this;
#: everything else is left out and takes its built-in default.  The provider is
#: `default_config()`'s, so a test that reads this file gets the configuration
#: the suite had before the file was required.
A_HOME_CONFIG = """\
providers:
  deepseek:
    api: openai-completions
    base_url: https://api.deepseek.com
    api_key: ${DEEPSEEK_API_KEY}
    models:
      - model: deepseek-flash
        name: DeepSeek Flash
        reasoning: true
        input: [text, image]
        context_window: 1000000
        max_tokens: 384000
        temperature: 0.7
        top_p: 1.0
"""


def home_config(data_dir: Path) -> Path:
    """Write the config a home must have, and answer its path."""
    return config_file(data_dir, A_HOME_CONFIG)


def config_file(data_dir: Path, text: str) -> Path:
    """Write one config file where the loader will look for it.

    **One copy, because four modules had one each.**  Four test modules write a
    `slife2.yaml` into the isolated data directory before building a server over
    it, and each had grown its own four-line function to do it (three called
    `write_config`, one `config_on_disk`).  What each of those really owns is its
    own `CONFIG` text, so each keeps one line — a `partial` binding that default
    — and the writing happens here.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "slife2.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def plugin_transports(
    config: Config,
    overrides: Mapping[str, Any] | None = None,
    client_factory: Any = None,
) -> dict[str, Any]:
    """What a toolhub built from `config` needs to reach its own plugins.

    **A hub with no plugins is a hub that lists nothing.**  It asks every
    server slife2 starts — that is where its tools come from, alongside the
    entries under `mcp:` — and it refuses to hand out a list when one of them
    does not answer, because a plugin that is not there is a system that has
    come apart rather than a model with fewer tools.

    So a test that builds a hub stands each one up, the way the launcher does.
    These are in-memory, and apart from the six below they offer the model
    nothing, which is what most plugins are: asking them is how "nothing for
    you" becomes a fact rather than an assumption.

    **Five are the real servers**, because the hub is not merely a client of
    them — each is the only process that knows something the hub has to ask
    for.  `context` holds the turn log, on the deterministic `StubEmbedder`, so
    a test gets the real merge, the real search and the real budget with no
    embedding endpoint behind them.  `skills-server` and `cli-server` are where
    the rows in a default config come from, and `mcp-tools` and `restapi-tools`
    are where the entries under `mcp:` and `rest-api:` are reached and
    declared — a `blank_plugin` in any of their places would leave the model
    with an empty list and a search with nothing to find, which is a hub under
    test only if what is being tested is a hub with no plugins.

    `overrides` replaces or adds a transport by name — a plugin the test
    wants to misbehave, or an entry under `mcp:` it wants wired.
    """
    from slife2.plugins.cli.server import build_server as build_cli
    from slife2.plugins.context.server import build_server as build_context
    from slife2.plugins.mcp_tools.server import build_server as build_mcp_tools
    from slife2.plugins.restapi.server import build_server as build_restapi_tools
    from slife2.plugins.skills.server import build_server as build_skills

    def for_plugin(name: str) -> Any:
        if name == "context":
            return lambda settings: build_context(
                config, embedder=StubEmbedder(), ask=keep_the_context
            )
        if name == "skills-server":
            return lambda settings: build_skills(config)
        if name == "cli-server":
            return lambda settings: build_cli(config)
        if name == "mcp-tools":
            # **The entries under `mcp:` are reached through this one now**,
            # so a test's fake upstreams are handed to it rather than to the hub
            # — which is why `overrides` is keyed by entry name and lands here.
            return lambda settings: build_mcp_tools(
                config, transports=overrides, client_factory=client_factory
            )
        if name == "restapi-tools":
            return lambda settings: build_restapi_tools(
                config, transports=overrides, client_factory=client_factory
            )
        return lambda settings: blank_plugin()

    transports: dict[str, Any] = {
        name: for_plugin(name) for name in config.plugins() if name != "toolhub"
    }
    transports.update(overrides or {})
    return transports


def answering(server: Any) -> Any:
    """A client factory that wires *every* entry to one in-memory server.

    **A transport a test did not wire still has to be reachable**, and that is
    the case the family plugins create: their management tools *add* entries,
    and an added entry names a command nothing in the test connected — so left
    to the real factory, `command: in-memory` would be spawned as a process and
    `uvx mcp-openapi-proxy` would be fetched.  Answering everything with the
    same in-memory server is what keeps those tests about the editing; the
    commands an entry names are never run.
    """

    def factory(_transport: Any, _handler: Any) -> Any:
        from fastmcp import Client

        return Client(server)

    return factory


def blank_plugin() -> Any:
    """One of our own servers, answering, with nothing for the model.

    The ordinary case, and the one worth being able to state: a plugin whose
    tools belong to its own code is asked for a list like every other, and the
    answer is empty rather than absent.
    """
    from fastmcp import FastMCP

    return FastMCP("plugin")


_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def evaluate(expression: str) -> int | float:
    """The four operations, for tests that want *a* tool and not a calculator.

    `calc` used to be served by `slife2.builtins`, and two test modules used its
    evaluator to build a local tool whose result they can assert (`2+2` is `4`)
    and whose failure they can provoke (`1/0` raises `ZeroDivisionError`).  The
    plugin is gone and the evaluator went with it; this is the little of it those
    tests need — no security bounds, because the only expression it ever sees is
    a test's own.
    """

    def walk(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp):
            left, right = walk(node.left), walk(node.right)
            operate = _OPERATORS[type(node.op)]
            return operate(left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        raise ValueError(f"unsupported expression: {ast.dump(node)}")

    return walk(ast.parse(expression, mode="eval"))
