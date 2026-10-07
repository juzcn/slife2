"""Test doubles.

Three seams get faked, and each one exists because something real and slow sits
behind it: the model (an HTTP call to a provider), the agent server (a socket),
and the observer (nothing — but a recorder is how a test reads what happened).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

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

    async def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

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
        self.prompts.append(prompt)
        self.images.append(images or [])
        return self._respond(prompt, on_event)
