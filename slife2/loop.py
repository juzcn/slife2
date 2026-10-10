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
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

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
from slife2.messages import Message, ToolCall, Usage
from slife2.tools import HARNESS_PREFIX, ToolRegistry

logger = logging.getLogger(__name__)


def harness_call(name: str) -> ToolCall:
    """A call the harness makes on the model's behalf, marked as one.

    **One builder, because there are two writers of these pairs and an id is a
    detail they would otherwise each invent.**  `trim_tools` writes the trim's
    pair at a turn boundary and the loop writes the cut-in's at a step boundary;
    two id formats would be two conventions for one thing, and two
    `time_ns`-suffixed ids built in two places are two chances to collide.

    The `_harness_` prefix with the tool's own mark stripped is v1's, and it is
    what makes the pair recognisable as the machinery's rather than the model's —
    `slife2/tui/restore.py` reads it, and so does anyone reading a transcript.
    """
    return ToolCall(
        id=f"_harness_{name.removeprefix(HARNESS_PREFIX)}_{time.time_ns():x}",
        name=name,
        arguments={},
    )


@dataclass(frozen=True)
class TurnResult:
    """What a turn produced."""

    text: str
    usage: Usage
    steps: int
    stop_reason: str
    #: The **last** model call's own usage: the conversation as that call saw
    #: it, which is the size the next request re-sends.  Not derivable from
    #: `usage`, which is a sum over calls and so cannot say how big any one of
    #: them was.  Last, and with no default, so a four-argument construction
    #: fails loudly rather than binding something else.
    #:
    #: A turn cut off at the step limit is the one place this is not quite the
    #: next request's size: the final batch of tool results was appended after
    #: that call, so those tokens are not in it.
    last_usage: Usage

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
        refresh: Callable[[], Awaitable[ToolRegistry]] | None = None,
    ) -> None:
        self._backend = backend
        self._tools = tools
        #: Where the tool list comes from, asked again before every model call.
        #:
        #: Once per turn is the arrangement this replaced, and the difference
        #: shows up in a turn that takes several steps: a tool a server grew, or
        #: a server that finished starting, is in the *next* call's list rather
        #: than the next turn's.  The loop still does not know what a tool server
        #: is — this is a coroutine that returns a registry, and it is the server
        #: that hands one over.
        self._refresh = refresh
        #: Model calls allowed in one turn before it is cut off.  A model that
        #: keeps calling tools would otherwise burn tokens indefinitely; the
        #: cap produces an ordinary result, not an exception.
        self._max_steps = max_steps

    async def _retool(self) -> None:
        """Ask where the tools come from, before a model call.

        A refresh that fails is deliberately *not* swallowed: the tool list is
        what the call is made with, and calling a model with last step's list
        because the hub is unreachable would be a silently wrong request rather
        than a failed turn.  `slife2.mcp_server.open_server` is where that rule
        is argued.
        """
        if self._refresh is not None:
            self._tools = await self._refresh()

    async def run_turn(
        self,
        messages: list[Message],
        user: str | list[dict[str, Any]],
        observer: TurnObserver = NULL_OBSERVER,
        *,
        auto: Callable[[], str | None] | None = None,
    ) -> TurnResult:
        """Run one turn, appending to `messages` as it goes.

        `messages` is mutated in place: the loop appends the user message, every
        assistant message, and every tool result.  Whoever owns the list owns the
        conversation's lifetime and bounds — which is the agent server now, and
        was the TUI before it.  Either way the loop is not the owner: it is
        handed a list and asked to advance it.

        `user` is what the user said — text, or content parts when something came
        with it.  It is appended here rather than by the caller because the loop
        is what owns the shape of a turn, and a caller that appended its own user
        message would be a second place that knows it.

        **The auto-invoke.**  `auto` is asked at the top of every step for the
        name of a tool the *harness* wants called — v1's cut-in — and the call is
        then made the way the model's own calls are made: through `self._tools`,
        the registry whose `specs` went out with the last request.  So the name
        routes the same way, the caller's identity rides the same way, and the
        text written into the pair is **the tool's own answer** rather than
        something written beside it.  `trim_tools` is the other half of this and
        does the same thing at a turn boundary; the two are the same mechanism
        and differ only in when the boundary is.

        What lands in the list is a **harness tool pair**, not a user message,
        and that is the whole of why it is safe: the model reads it as something
        that arrived mid-turn, and the exchange stays a legal assistant/tool
        sequence for every provider.  A bare user message in the middle of one is
        a 400 from several of them.

        `auto` returns a name or `None`, which is v1's split: the caller decides
        *whether* something arrived — cheaply, without a call — and this decides
        nothing about tools at all.  The registry is what knows a name.
        """
        messages.append(Message(role="user", content=user))

        total_usage = Usage()
        last_text = ""
        last_usage = Usage()

        for step in range(1, self._max_steps + 1):
            # **The call is made before either half is written**, because the
            # pair's text *is* the tool's answer — there is nothing to write
            # until it has answered.  Putting the one `await` in front of both
            # appends rather than between them is what keeps the pair
            # unsplittable: an interruption in the gap would leave an assistant
            # message whose call is never answered, the one history state every
            # provider rejects.  This way a cancellation during the call leaves
            # no pair at all, and whatever the call consumed is still in hand to
            # be run as its own turn.
            #
            # v1 writes its pair the other way round, because its tools can
            # raise and a failure had to be *recorded* as the result of the call
            # it was written for.  Here the registry returns a failure as text
            # (`ToolRegistry.execute` never raises), so there is no such case:
            # everything that can come back is an answer.
            if auto is not None and (name := auto()) is not None:
                call = harness_call(name)
                text, _ok = await self._tools.execute(call)
                messages.append(
                    Message(role="assistant", content=None, tool_calls=[call])
                )
                messages.append(
                    Message(role="tool", content=text, tool_call_id=call.id)
                )
            # The tool list goes with the call, so it is asked for with the
            # call.  `specs` is read after this and never cached across steps.
            await self._retool()
            stream = self._backend.stream(messages, self._tools.specs)

            # Phase A: exhaust the stream before doing anything with it.  A tool
            # must not run inside this loop -- that would hold the provider's
            # response stream open across the tool call, risking its read
            # deadline and pinning a socket for no reason.
            await self._drain(stream.chunks, observer)

            result = await stream.result
            total_usage = total_usage + result.usage
            last_text = result.text
            last_usage = result.usage
            messages.append(
                Message(
                    role="assistant",
                    content=result.text or None,
                    # Kept on the message rather than only streamed at the
                    # reader: it is part of what the model said, and the turn
                    # log is where a conversation is read back from.  It does
                    # not go to the model again — `Message.to_wire` leaves it
                    # out, which is the whole of that decision.
                    thinking=result.thinking,
                    tool_calls=list(result.tool_calls),
                )
            )

            if not result.tool_calls:
                await self._emit(
                    observer,
                    TurnFinished(
                        text=result.text,
                        usage=total_usage,
                        last_usage=last_usage,
                        steps=step,
                        stop_reason=result.stop_reason or "stop",
                    ),
                )
                return TurnResult(
                    text=result.text,
                    usage=total_usage,
                    last_usage=last_usage,
                    steps=step,
                    stop_reason=result.stop_reason or "stop",
                )

            # Phase B: run the batch and feed every result back.
            for call in result.tool_calls:
                await self._emit(
                    observer, ToolCallStarted(call.id, call.name, call.arguments)
                )
                # Timed here because this is the only place that knows when the
                # call ran: the event carried a duration from the beginning —
                # short key, codec, two tests — and the producer filled it with
                # a zero, so the transcript could not have shown one whatever it
                # did with it.
                started = time.monotonic()
                text, ok = await self._tools.execute(call)
                await self._emit(
                    observer,
                    ToolCallFinished(
                        call_id=call.id,
                        name=call.name,
                        ok=ok,
                        result_preview=preview(text),
                        result_chars=len(text),
                        elapsed_ms=int((time.monotonic() - started) * 1000),
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
                last_usage=last_usage,
                steps=self._max_steps,
                stop_reason="max_steps",
            ),
        )
        return TurnResult(
            text=last_text,
            usage=total_usage,
            last_usage=last_usage,
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
