"""Test doubles.

Four seams get faked, and each one exists because something real and slow sits
behind it: the model (an HTTP call to a provider), the embedding model (another
one), the agent server (a socket), and the observer (nothing — but a recorder is
how a test reads what happened).

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
from typing import Any

from fastmcp.exceptions import ToolError

from slife2.config import Config
from slife2.events import TurnEvent
from slife2.llm.base import Chunk, Stream
from slife2.messages import Message, StreamChatResult, ToolSpec


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
    """An :class:`~slife2.llm.base.LLMBackend` that replays a script.

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


def text_turn(text: str, *, chunks: list[str] | None = None, delay: float = 0.0):
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
        #: The images sent with each prompt, so a test can assert the attachment
        #: reached the client rather than only that the file was read.
        self.images: list[list[str]] = []
        self.resets = 0
        #: What `transcript` answers with — the previous conversation, as the
        #: window opened after a restart sees it.  Empty is the real answer for
        #: a name that has never run, and so is the default.
        self.turns: list[dict[str, Any]] = []
        #: How many times the history was read, so a test can prove a window
        #: reads it once rather than once per connection.
        self.reads = 0

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

    async def reset(self) -> None:
        self.resets += 1

    async def run_turn(
        self,
        prompt: str,
        on_event: Callable[[TurnEvent], None],
        *,
        images: list[str] | None = None,
    ) -> str:
        """Run one scripted turn, keeping the real client's contract.

        Two halves of that contract matter to the TUI and neither is obvious, so
        both are modelled here: a turn needs a connection (the real one would
        reach for a client it no longer has), and a turn that fails for any
        reason other than a refusal drops it — which is what `run_turn`'s own
        `except` does, and what the app's retry is built on.
        """
        if not self.connected:
            raise ConnectionError("not connected")
        self.prompts.append(prompt)
        self.images.append(images or [])
        try:
            return self._respond(prompt, on_event)
        except ToolError:
            # The server answered and refused *this* turn; the link is fine.
            raise
        except Exception:
            self.connected = False
            raise


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


def plugin_transports(
    config: Config,
    overrides: Mapping[str, Any] | None = None,
    client_factory: Any = None,
) -> dict[str, Any]:
    """What a toolhub built from `config` needs to reach its own plugins.

    **A hub with no plugins is a hub that lists nothing.**  It asks every
    server slife2 starts — that is where its tools come from, alongside the
    entries under `tools:` — and it refuses to hand out a list when one of them
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
    are where the entries under `tools:` and `rest-api:` are reached and
    declared — a `blank_plugin` in any of their places would leave the model
    with an empty list and a search with nothing to find, which is a hub under
    test only if what is being tested is a hub with no plugins.

    `overrides` replaces or adds a transport by name — a plugin the test
    wants to misbehave, or an entry under `tools:` it wants wired.
    """
    from slife2.cli_server import build_server as build_cli
    from slife2.context_server import build_server as build_context
    from slife2.mcp_tools import build_server as build_mcp_tools
    from slife2.restapi_tools import build_server as build_restapi_tools
    from slife2.skills_server import build_server as build_skills

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
            # **The entries under `tools:` are reached through this one now**,
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
