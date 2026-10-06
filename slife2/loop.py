"""The agent loop.

One turn is: send the conversation to a model, stream whatever comes back,
run any tools it asked for, put the results back into the conversation, and
repeat until it stops asking for tools.

The loop is a pure function over a message list.  It does not own the
conversation, does not know what MCP is, does not know which provider answered,
and does not know whether anyone is watching — the observer is optional and its
failures cannot end a turn.  Each of those was a thing v1's loop did, and each
one made it harder to test than it needed to be.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from slife2.events import (
    NULL_OBSERVER,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnEvent,
    TurnFinished,
    TurnObserver,
    preview,
)
from slife2.llm.base import Chunk, LLMBackend
from slife2.messages import Message, Usage
from slife2.tools import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnResult:
    """What a turn produced."""

    text: str
    usage: Usage
    steps: int
    stop_reason: str

    @property
    def hit_step_limit(self) -> bool:
        return self.stop_reason == "max_steps"


class AgentLoop:
    """Runs turns against a backend and a tool registry."""

    def __init__(
        self,
        backend: LLMBackend,
        tools: ToolRegistry,
        *,
        max_steps: int = 16,
    ) -> None:
        self._backend = backend
        self._tools = tools
        #: Model calls allowed in one turn before it is cut off.  A model that
        #: keeps calling tools would otherwise burn tokens indefinitely; the
        #: cap produces an ordinary result, not an exception.
        self._max_steps = max_steps

    async def run_turn(
        self,
        messages: list[Message],
        user_text: str,
        observer: TurnObserver = NULL_OBSERVER,
    ) -> TurnResult:
        """Run one turn, appending to `messages` as it goes.

        `messages` is mutated in place: the loop appends the user message, every
        assistant message, and every tool result.  The caller owns the list and
        therefore owns the conversation's lifetime and bounds.
        """
        messages.append(Message(role="user", content=user_text))

        specs = self._tools.specs
        total_usage = Usage()
        last_text = ""

        for step in range(1, self._max_steps + 1):
            stream = self._backend.stream(messages, specs)

            # Phase A: exhaust the stream before doing anything with it.  A tool
            # must not run inside this loop -- that would hold the provider's
            # response stream open across the tool call, risking its read
            # deadline and pinning a socket for no reason.
            await self._drain(stream.chunks, observer)

            result = await stream.result
            total_usage = total_usage + result.usage
            last_text = result.text
            messages.append(
                Message(
                    role="assistant",
                    content=result.text or None,
                    tool_calls=list(result.tool_calls),
                )
            )

            if not result.tool_calls:
                await self._emit(
                    observer,
                    TurnFinished(
                        text=result.text,
                        usage=total_usage,
                        steps=step,
                        stop_reason=result.stop_reason or "stop",
                    ),
                )
                return TurnResult(
                    text=result.text,
                    usage=total_usage,
                    steps=step,
                    stop_reason=result.stop_reason or "stop",
                )

            # Phase B: run the batch and feed every result back.
            for call in result.tool_calls:
                await self._emit(
                    observer, ToolCallStarted(call.id, call.name, call.arguments)
                )
                text, ok = await self._tools.execute(call)
                await self._emit(
                    observer,
                    ToolCallFinished(
                        call_id=call.id,
                        name=call.name,
                        ok=ok,
                        result_preview=preview(text),
                        result_chars=len(text),
                        elapsed_ms=0,
                    ),
                )
                messages.append(
                    Message(role="tool", content=text, tool_call_id=call.id)
                )

        logger.warning("turn hit the %d-step limit", self._max_steps)
        await self._emit(
            observer,
            TurnFinished(
                text=last_text,
                usage=total_usage,
                steps=self._max_steps,
                stop_reason="max_steps",
            ),
        )
        return TurnResult(
            text=last_text,
            usage=total_usage,
            steps=self._max_steps,
            stop_reason="max_steps",
        )

    async def _drain(
        self, chunks: AsyncIterator[Chunk], observer: TurnObserver
    ) -> None:
        """Forward a stream's chunks to the observer, closing it either way.

        The `finally` matters on cancellation: an abandoned async generator
        keeps whatever the provider left open.  Closing is best-effort, since a
        generator that is already finished raises on a second close.
        """
        try:
            async for chunk in chunks:
                # Reasoning is forwarded as its own kind, never merged into the
                # answer: it is the model talking to itself, and a transcript
                # that mixes the two is unreadable in a way that is hard to
                # notice and impossible to undo.
                if chunk.thinking:
                    await self._emit(observer, ThinkingDelta(chunk.thinking))
                if chunk.text:
                    await self._emit(observer, TextDelta(chunk.text))
        finally:
            aclose = getattr(chunks, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 - closing must not mask a cancel
                    logger.debug("closing the chunk stream failed", exc_info=True)

    @staticmethod
    async def _emit(observer: TurnObserver, event: TurnEvent) -> None:
        """Hand an event to the observer, swallowing its failures.

        A UI that throws must not end a turn — the answer is still correct and
        still returned.  `CancelledError` is deliberately not caught: it derives
        from BaseException precisely so that cancellation cannot be swallowed,
        and the loop depends on that.
        """
        try:
            await observer.on_event(event)
        except Exception:  # noqa: BLE001
            logger.warning("observer failed on %s", type(event).__name__, exc_info=True)
