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

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from itertools import count
from typing import Any

from slife2.libs.harness.async_tasks import Deliver, schedule, started
from slife2.libs.harness.dumps import Compress
from slife2.libs.model.base import Chunk, LLMBackend
from slife2.libs.turn.events import (
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
from slife2.libs.turn.messages import Message, ToolCall, Usage
from slife2.libs.turn.tools import (
    HARNESS_PREFIX,
    ToolRegistry,
    bind_timeout,
    split_meta,
)

logger = logging.getLogger(__name__)

#: What the model reads when its own request to be checked was refused.  v1's
#: sentence, and it starts with `Error` because that is what makes the call read
#: as a failure in the transcript rather than as a result that happens to be
#: discouraging (`slife2.libs.turn.tools` states the same rule for a tool's own failures).
DENIED = "Error: Tool execution was denied by user."

#: What the model reads when it asks for a background call in a loop that has
#: nowhere to hand the result back to — today that is an agent task's own loop
#: (`slife2.plugins.agent_task.server`), which runs one turn and has no second one for an
#: answer to arrive in.  **Actionable rather than merely true**: the model can fix
#: this in one step, and a mode that quietly ran the call here instead would have
#: taught it that `_async` does nothing.
BACKGROUND_UNAVAILABLE = (
    "Error: a background call is not available here. "
    "Call the tool again without `_async`."
)


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

    **And the shape is a wire constraint rather than only a label.**  A pair is
    written once and every later request carries it, so a provider that refuses
    the string refuses a *request* rather than a call — on the replay, far from
    here.  Messages states the rule, on both fields the id appears in
    (`tool_use.id`, `tool_result.tool_use_id`: `^[a-zA-Z0-9_-]+$`, and no length
    bound), and this shape sits inside it by construction.  The letters are the
    load-bearing part: a separator outside that class is one that would have to
    be sanitised on the way out, which is the thing a single builder exists to
    make unnecessary.  `DESIGN.md` §8.5 carries the measurement.
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
        max_steps: int = 0,
        refresh: Callable[[], Awaitable[ToolRegistry]] | None = None,
        approve: Callable[[ToolCall], Awaitable[bool]] | None = None,
        deliver: Deliver | None = None,
        compress: Compress | None = None,
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
        #: How the harness is asked to confirm one call, or None when nobody can
        #: be.  **The loop does not know whether anyone is watching** (the module
        #: docstring's third promise), so it cannot answer "is there an operator"
        #: itself — it asks the thing that does, and a caller that has nobody to
        #: ask passes None.  None means the model's `_approve` is granted rather
        #: than refused, which is v1's rule for the same situation and the safe
        #: direction: refusing would turn "nobody is here to say yes" into a tool
        #: the model can never use.
        self._approve = approve
        #: Where a background call's result goes when it is ready, or None for a
        #: loop nobody's conversation is behind.  **It is handed in for the same
        #: reason `approve` is**: the loop is a pure function over a message list
        #: and does not know which conversation it is running — the *server*
        #: does, and only the server can put a message back into one
        #: (`slife2.libs.harness.handover`).  A background call that nobody is behind keeps its
        #: result in the registry instead, for `check_async` to hand over.
        self._deliver = deliver
        #: How a tool result that does not fit is fitted, or None for one that
        #: goes in as it came.  **Handed in for the third time and the same
        #: reason**: the loop does not know what the conversation is about, and
        #: the party that chooses which lines of a long output this conversation
        #: needs is the party that can see it (`slife2.libs.harness.dumps`).
        #: None means the result is passed through, which is the loop's answer to
        #: "nobody said otherwise" — and the cost of that answer is a turn that
        #: carries whatever the tool returned, which is a fact about the tool and
        #: not about this decision.
        self._compress = compress
        #: Model calls allowed in one turn before it is cut off, or **0 for no
        #: cap at all**.  Zero is the config's spelling for "nobody asked for
        #: one" (`slife2.libs.host.config.AgentSettings.max_steps`, where an absent key is
        #: the same 0), and it is the default here for the same reason: a cap is
        #: something somebody asks for, and a turn that runs until the model
        #: stops asking for tools is what a loop is when nobody has said
        #: otherwise.  So there is one rule rather than two — a cap exists when
        #: the number is positive — and where one exists it produces an ordinary
        #: result, not an exception.
        self._max_steps = max_steps

    async def _retool(self) -> None:
        """Ask where the tools come from, before a model call.

        A refresh that fails is deliberately *not* swallowed: the tool list is
        what the call is made with, and calling a model with last step's list
        because the hub is unreachable would be a silently wrong request rather
        than a failed turn.  `slife2.libs.plugin.protocol.open_server` is where that rule
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

        **Three arguments the model may add to any call.**  `_timeout`, `_async`
        and `_approve` are read off every call and stripped before dispatch, so
        no tool ever sees them (`slife2.libs.turn.tools` names them and argues the strip).
        What the loop does with each:

        * `_timeout` — how long this one call may take.  **It is the only clock
          the loop ever holds**: there is no default and no per-turn budget,
          because the loop does not know what any tool is doing, and a number
          that applies to every tool is a number that is wrong for most of them.
          A bound exists exactly when somebody who knows asked for one — the
          model in its arguments, or the tool in its own schema, in which case
          the tool enforces it and the loop stays out of the way.  Enforcement is
          therefore **exactly one timer per call**, and never two.
        * `_async` — the call is handed to the background and the model is
          answered with a task id (`slife2.libs.harness.async_tasks`).  It still passes
          through the approval gate first, and it is still announced as started.
          **A loop with nowhere to hand the result back is the one that refuses
          it** (`self._deliver is None`): an agent task's loop is that case
          today, and it says so in a sentence the model can act on rather than
          running the call quietly.
        * `_approve` — the harness is asked before the call runs, through
          `self._approve`.  A caller with nobody to ask passes None, and then the
          call simply runs: refusing would be answering a request for a check
          with a tool the model can never use.
        """
        messages.append(Message(role="user", content=user))

        total_usage = Usage()
        last_text = ""
        last_usage = Usage()

        # The iterator is the whole of what "no cap" changes here: a positive
        # number bounds the steps, and zero counts forever — the turn still
        # ends the way it always did, when the model stops asking for tools.
        steps = range(1, self._max_steps + 1) if self._max_steps > 0 else count(1)
        for step in steps:
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
            answer = Message(
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

            if not result.tool_calls:
                messages.append(answer)
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

            # Phase B: run the whole batch, and write it down only once every
            # call of it has an answer.
            #
            # **The assistant message and its answers are appended together,
            # with no await between them** — the same rule the harness pair
            # above follows, for the same reason.  An assistant message whose
            # calls are only partly answered is the one history state every
            # provider rejects, and appending the message before fitting its
            # results left exactly that whenever a fit raised: `_fit` is the
            # caller's own callback, so it is the step that can fail (a cabinet
            # that will not open, a discriminator that stalled), and nothing
            # downstream repairs a failure — a failed turn keeps its messages,
            # so the conversation failed every later turn until it was rebuilt.
            #
            # What `_fit` is handed is therefore the conversation *before* this
            # step's answer, which is also the honest list to judge a result
            # against: it no longer contains a half-written batch.
            answers: list[Message] = []
            for call in result.tool_calls:
                text, _ok = await self._one(call, observer, step)
                answers.append(
                    Message(
                        role="tool",
                        content=await self._fit(call, text, messages),
                        tool_call_id=call.id,
                    )
                )
            messages.append(answer)
            messages.extend(answers)

        # Only a capped turn ever gets here: with no cap the iterator does not
        # end, so the only way out of the loop is the return above.
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

    async def _one(
        self, call: ToolCall, observer: TurnObserver, step: int
    ) -> tuple[str, bool]:
        """Run one call the way its own meta-parameters asked, and report it.

        The whole of one call sits here — the strip, the gate, the clock, the
        two events — because each of those is a fact about *this* call and the
        order between them is the thing that is easy to get wrong:

        `step` is the loop's own counter, and it is here for the same reason:
        the event that opens the call carries it so the panel can say which
        step of the turn this is (v1's `(3/30)`), and this is the only place
        that knows both the call and the step it was made in.

        * **Nothing is announced before the gate answers.**  A call the operator
          refused never happened as far as the transcript is concerned: v1's
          rule, and the reason is the display — a panel that appeared and then
          said "denied" would have told a reader the tool ran.
        * **The clock starts after the announcement**, so a call's duration is
          the call and not the time somebody spent deciding.
        * **The call is announced with the arguments the model wrote**, including
          the meta-parameters, while the dispatch carries the stripped copy:
          what a reader sees is what the model said, and what the tool sees is
          what the tool takes.
        """
        arguments, meta = split_meta(call.arguments)
        # The tool's own schema decides who enforces the deadline, so the number
        # is put where it will be read and what is left for this method is
        # whatever nobody else took (`slife2.libs.turn.tools.bind_timeout`).
        bound = bind_timeout(arguments, self._tools.spec_of(call.name), meta.timeout)
        dispatch = replace(call, arguments=arguments)

        if meta.background and self._deliver is None:
            # **A background call needs somewhere for its result to go**, and a
            # loop with no way back has none: the call would run, the model would
            # be handed a task id, and the answer would sit in a registry nobody
            # can ask — `check_async` is not in a registry whose loop has no
            # `deliver` at all, because the transport that builds one builds both
            # (`slife2.plugins.agent.server.registry`).
            #
            # So the mode is refused, and *before* the approval gate: asking an
            # operator to confirm a call that is not going to run would be
            # spending somebody's attention on nothing.
            logger.info("tool_background_unsupported name=%s", call.name)
            return BACKGROUND_UNAVAILABLE, False

        if meta.approve and self._approve is not None and not await self._approve(call):
            logger.info("tool_denied name=%s", call.name)
            return DENIED, False

        await self._emit(
            observer,
            ToolCallStarted(
                call.id,
                call.name,
                call.arguments,
                step=step,
                # A whole number for a turn that has a cap, and 0 for one that
                # does not — the panel's two spellings of the counter come from
                # this, so it is sent rather than left for the reader to guess.
                max_steps=self._max_steps,
            ),
        )
        # Timed here because this is the only place that knows when the call ran:
        # the event carried a duration from the beginning — short key, codec, two
        # tests — and the producer filled it with a zero, so the transcript could
        # not have shown one whatever it did with it.
        started = time.monotonic()
        if meta.background:
            text, ok = await self._background(dispatch, bound)
        else:
            text, ok = await self._foreground(dispatch, bound)
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
        return text, ok

    async def _fit(self, call: ToolCall, text: str, messages: Sequence[Message]) -> str:
        """What the conversation carries for one result: the result, or a window.

        **The harness's move and not the tool's**, which is why it is a callback
        the caller injects rather than anything this module decides: fitting a
        result is choosing what *this conversation* needs from it, and the loop
        knows neither the conversation nor the model's budget — so it hands over
        the call, the text and the list, and takes back what to append.  A loop
        with no callback hands the result on unchanged: the same
        None-means-nobody-said rule `approve` and `deliver` follow, and the cost
        of it is a turn whose size is whatever the tool returned.

        It happens **after the call is announced and before the result is
        appended**, which is the one moment that is both: the panel's account of
        the call is the tool's own answer (`ToolCallFinished` carries its length
        and its head), and what enters the conversation is what the model will
        read back later.
        """
        if self._compress is None:
            return text
        return await self._compress(call, text, messages)

    async def _foreground(
        self, dispatch: ToolCall, bound: float | None
    ) -> tuple[str, bool]:
        """Run a call this turn is waiting on, or say it took too long.

        `bound` is None for every call nobody asked about, and that is the
        ordinary case rather than a gap: a tool is a real action somewhere else,
        and cutting one off is a thing that has to be *asked for*.  Cancellation
        is what a bound is here — the await is cancelled where it stands, and
        what the far end does about a cancelled call is its own business
        (`slife2.libs.turn.tools` keeps the result a string, so the sentence below is this
        method's to write and cannot be a tool's).
        """
        if bound is None:
            return await self._tools.execute(dispatch)
        try:
            async with asyncio.timeout(bound):
                return await self._tools.execute(dispatch)
        except TimeoutError:
            # v1's sentence, verbatim.
            logger.warning("tool_timeout name=%s timeout=%s", dispatch.name, bound)
            return f"Error: Tool '{dispatch.name}' timed out ({bound}s).", False

    async def _background(
        self, dispatch: ToolCall, bound: float | None
    ) -> tuple[str, bool]:
        """Hand a call to the background, and answer the id the model polls.

        **The bound applies only when the model asked for one.**  Backgrounding
        exists to escape the bounds a foreground call is subject to, so applying
        the foreground's anyway would undo the thing that was asked for.  A tool
        with its own `timeout` argument has already been handed the number; a
        tool without one is wrapped when the model named a number and runs bare
        when nobody did — v1's mapping exactly, and the same mapping the
        foreground path uses, which is why this method takes `bound` rather than
        deciding anything.
        """
        coro = self._result(dispatch)
        if bound is not None:
            coro = asyncio.wait_for(coro, timeout=bound)
        # **The call's own id and name go with it**, because nothing else can
        # supply them later: `check_async` is given a task id and nothing else —
        # a call the model makes a step or a turn afterwards is carrying none of
        # this — and a result that cannot say which call it answers is one the
        # model has to match up from memory.
        task_id = schedule(
            coro,
            call_id=dispatch.id,
            tool=dispatch.name,
            deliver=self._deliver,
        )
        # v1's sentence, under the marker the registry wrote
        # (`slife2.libs.harness.async_tasks.started`): the sentence is what the model acts on,
        # and the marker is what a screen and a reader of the transcript pair
        # this call's two halves by.
        return started(task_id, dispatch.id, dispatch.name), True

    async def _result(self, dispatch: ToolCall) -> tuple[str, bool]:
        """One call's answer, as its text *and* whether it worked.

        **The pair travels now, and the old argument for dropping it is the one
        DESIGN.md §10.1 forbids.**  `_ok` used to be thrown away on the grounds
        that it was not lost — a failure is text by the time it reaches here
        (`ToolRegistry.execute` renders one) so the `Error` the string starts
        with was said to *be* the fact, and `slife2.libs.harness.async_tasks` read the prefix.
        A fact spelled as a prefix is a fact nothing guarantees: an answer that
        legitimately begins "Error" reads as a failure, and a tool that failed
        without that prefix (the registry's `ok=False` path has more than one
        spelling) reads as a success.  `execute` already answers the pair.
        """
        return await self._tools.execute(dispatch)

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
