"""The agent loop.

One turn is: send the conversation to a model, stream whatever comes back,
run any tools it asked for, put the results back into the conversation, and
repeat until it stops asking for tools.

The loop is a pure function over a message list.  It does not own the
conversation, does not know what MCP is, does not know which provider answered,
and does not know whether anyone is watching — the observer is optional and its
failures cannot end a turn.  Each of those is deliberately out of the loop,
which is what keeps it testable.
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
    MessageCutIn,
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
    INTERRUPTED,
    ToolRegistry,
    bind_timeout,
    split_meta,
)

logger = logging.getLogger(__name__)

#: What the model reads when its own request to be checked was refused.  It
#: starts with `Error` because that is what makes the call read as a failure in
#: the transcript rather than as a result that happens to be discouraging
#: (`slife2.libs.turn.tools` states the same rule for a tool's own failures).
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


def timed_out(name: str, bound: float) -> str:
    """What a call that ran past the bound somebody set is answered with.

    **One spelling, because two paths write it.**  A call runs under a clock
    only when the model asked for one, and the same bound firing has to read the
    same way whichever path the call took — the sentence names the tool and the
    bound, and both are what the model acts on (retry with a longer one, or
    without one at all).  A bare `TimeoutError` in the background path would say
    nothing: `slife2.libs.harness.async_tasks._runner` renders whatever escapes a
    task as `Error: {type}: {exc}`, so the hand-back would read
    `Error: TimeoutError: ` with neither fact in it.
    """
    return f"Error: Tool '{name}' timed out ({bound}s)."


def harness_call(name: str) -> ToolCall:
    """A call the harness makes on the model's behalf, marked as one.

    **One builder, because there are two writers of these pairs and an id is a
    detail they would otherwise each invent.**  `trim_tools` writes the trim's
    pair at a turn boundary and the loop writes the cut-in's at an iteration
    boundary; two id formats would be two conventions for one thing, and two
    `time_ns`-suffixed ids built in two places are two chances to collide.

    The `_harness_` prefix with the tool's own mark stripped is what makes the
    pair recognisable as the machinery's rather than the model's —
    `slife2/tui/restore.py` reads it, and so does anyone reading a transcript.

    **And the shape is a wire constraint rather than only a label.**  A pair is
    written once and every later request carries it, so a provider that refuses
    the string refuses a *request* rather than a call — on the replay, far from
    here.  Messages states the rule, on both fields the id appears in
    (`tool_use.id`, `tool_result.tool_use_id`: `^[a-zA-Z0-9_-]+$`, and no length
    bound), and this shape sits inside it by construction.  The letters are the
    load-bearing part: a separator outside that class is one that would have to
    be sanitised on the way out, which is the thing a single builder exists to
    make unnecessary.  `SDD` §5.3 carries the measurement.
    """
    return ToolCall(
        id=f"_harness_{name.removeprefix(HARNESS_PREFIX)}_{time.time_ns():x}",
        name=name,
        arguments={},
    )


def interrupted_results(
    calls: Sequence[ToolCall], returned: str | None = None
) -> list[Message]:
    """The tool messages that complete a batch an interruption cut short.

    **One answer per call, because the wire requires it**: an assistant message
    whose tool calls are only partly answered is the one history state every
    provider rejects.  A call that had already returned keeps its own text —
    including the first of a cut-off tail, which is why `returned` exists: an
    interruption that lands in the fit after the call ran has the tool's answer
    in hand, and saying it was interrupted would be false.  It goes in
    **unfitted**, because a fit is an await and the interruption is in flight;
    the long result is the true one, and `_fit` is a size decision rather than a
    truth one.  Every call that produced nothing says `INTERRUPTED`, the same
    sentence a rebuilt list uses for an orphan
    (`slife2.libs.turn.context.consistent`).

    `returned` belongs to `calls[0]`, because the loop hands over the tail that
    starts at the call it was working on.

    Written by the loop synchronously on its way out: the interruption is in
    flight, and an await here would be cut at its next checkpoint before the
    cleanup finished (`slife2.libs.harness.async_tasks` measured the same thing
    for detached writes).
    """
    answers: list[Message] = []
    for index, call in enumerate(calls):
        content = returned if index == 0 and returned is not None else INTERRUPTED
        answers.append(Message(role="tool", content=content, tool_call_id=call.id))
    return answers


@dataclass(frozen=True)
class AutoCall:
    """A tool the harness wants called at an iteration boundary, and its receipt.

    **The one thing `auto` returns**, so that the caller decides *whether*
    something arrived (cheaply, without a call) and the loop decides how it is
    written down.  `name` is the tool; the loop runs it out of the registry the
    last request advertised, so the answer in the pair is the tool's own.

    `channel` and `receipt` describe the *message* the call delivers rather than
    the tool: the loop writes the channel onto the pair's tool message (display
    metadata a rebuild reads, never a wire field) and echoes both on the
    `MessageCutIn` event.  They are empty for a call that delivers no message,
    which today is no call at all — `auto` is the cut-in's way into the loop.
    """

    name: str
    channel: str = ""
    receipt: str = ""


@dataclass(frozen=True)
class TurnResult:
    """What a turn produced."""

    text: str
    usage: Usage
    iterations: int
    stop_reason: str
    #: The **last** model call's own usage: the conversation as that call saw
    #: it, which is the size the next request re-sends.  Not derivable from
    #: `usage`, which is a sum over calls and so cannot say how big any one of
    #: them was.  Last, and with no default, so a four-argument construction
    #: fails loudly rather than binding something else.
    #:
    #: A turn cut off at the iteration limit is the one place this is not quite
    #: the next request's size: the final batch of tool results was appended
    #: after that call, so those tokens are not in it.
    last_usage: Usage

    @property
    def hit_iteration_limit(self) -> bool:
        return self.stop_reason == "max_iterations"


def _not_run(call: ToolCall, why: str) -> ToolCall:
    """The call, marked with the sentence its result will be.

    The tool's name is in the sentence because a turn carries several calls and
    an opaque `tool_call_id` is not what the model recognises its own mistake
    by.  `ToolRegistry.execute` is what turns the mark into `Error: …` and
    stops the dispatch; this only says which call and why.
    """
    return replace(call, error=f"tool call {call.name!r} was not run: {why}")


class AgentLoop:
    """Runs turns against a backend and a tool registry."""

    def __init__(
        self,
        backend: LLMBackend,
        tools: ToolRegistry,
        *,
        max_iterations: int = 0,
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
        #: shows up in a turn that takes several iterations: a tool a server
        #: grew, or a server that finished starting, is in the *next* call's
        #: list rather than the next turn's.  The loop still does not know what a
        #: tool server is — this is a coroutine that returns a registry, and it
        #: is the server that hands one over.
        self._refresh = refresh
        #: How the harness is asked to confirm one call, or None when nobody can
        #: be.  **The loop does not know whether anyone is watching** (the module
        #: docstring's third promise), so it cannot answer "is there an operator"
        #: itself — it asks the thing that does, and a caller that has nobody to
        #: ask passes None.  None means the model's `_approve` is granted rather
        #: than refused, which is the safe direction: refusing would turn "nobody
        #: is here to say yes" into a tool the model can never use.
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
        #: cap at all**.  A cap is something somebody asks for: an absent key is
        #: the same 0 (`config.AgentSettings.max_iterations`), and a turn runs
        #: until the model stops asking for tools.  Where a cap exists it
        #: produces an ordinary result, not an exception.
        self._max_iterations = max_iterations
        #: The bill for the turn in flight, and the last completed call's own.
        #: Kept on the loop because **a cancellation has no return value to
        #: carry them**: `run_turn` updates these after each model call and
        #: resets them when a turn begins, so the owner of a cancelled turn can
        #: still say what it spent (`slife2.plugins.agent.server.Outcome.usage`).
        #: On the loop rather than in a callback because the loop is the party
        #: that saw the calls, and a second accounting path would be a second
        #: place to forget one.
        self.usage = Usage()
        self.last_usage = Usage()

    async def _retool(self) -> None:
        """Ask where the tools come from, before a model call.

        A refresh that fails is deliberately *not* swallowed: the tool list is
        what the call is made with, and calling a model with the last
        iteration's list because the hub is unreachable would be a silently
        wrong request rather than a failed turn.
        `slife2.libs.plugin.protocol.open_server` is where that rule is argued.
        """
        if self._refresh is not None:
            self._tools = await self._refresh()

    async def run_turn(
        self,
        messages: list[Message],
        user: str | list[dict[str, Any]],
        observer: TurnObserver = NULL_OBSERVER,
        *,
        auto: Callable[[], AutoCall | None] | None = None,
        folded: Callable[[], tuple[str, str] | None] | None = None,
        injected: Callable[[], int] | None = None,
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

        **The auto-invoke.**  `auto` is asked at the top of every iteration for
        an `AutoCall` — the name of a tool the *harness* wants called, the
        cut-in — and the call is then made the way the model's own calls are
        made: through `self._tools`, the registry whose `specs` went out with the
        last request.  So the name
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

        `auto` returns an `AutoCall` or `None`, and the split is deliberate: the
        caller decides *whether* something arrived — cheaply, without a call —
        and this decides nothing about tools at all.  The registry is what knows
        a name.  The call's channel is written onto the pair as display
        metadata and both halves are echoed on `MessageCutIn`, so there is one
        answer to "what arrived, and for whom" whether a reader looks at the
        live stream or at the turn log.

        **The model can make the same call itself**, because the tool is
        declared and a provider hands the loop whatever the model asked for.
        That path must not be a second, poorer account of a cut-in: `injected`
        answers how many messages this turn has taken, `folded` answers the
        channel and receipt of the one just taken, and a count that grew across
        the call writes the channel onto the tool message and emits the same
        `MessageCutIn`.  Without that pair a message the model took early lost
        its channel in the record and was drawn again by the window when its
        submission's own turn began — one message, two lines, the second below
        the answer.  Both are optional: a loop with no inbox behind it has
        nothing to fold.

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
        # **A new bill, before anything can be spent on it.**  Left over from
        # the previous turn, these would bill this one for a cancellation that
        # lands before its first call returns — the exact case they exist for.
        self.usage = Usage()
        self.last_usage = Usage()
        messages.append(Message(role="user", content=user))

        total_usage = Usage()
        last_text = ""
        last_usage = Usage()

        # The iterator is the whole of what "no cap" changes here: a positive
        # number bounds the iterations, and zero counts forever — the turn still
        # ends the way it always did, when the model stops asking for tools.
        iterations = (
            range(1, self._max_iterations + 1) if self._max_iterations > 0 else count(1)
        )
        for iteration in iterations:
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
            # The registry returns a failure as text (`ToolRegistry.execute`
            # never raises), so there is no case where a failure has to be
            # *recorded* with the call it was written for: everything that can
            # come back is an answer.
            if auto is not None and (wanted := auto()) is not None:
                call = harness_call(wanted.name)
                text, _ok = await self._tools.execute(call)
                messages.append(
                    Message(role="assistant", content=None, tool_calls=[call])
                )
                messages.append(
                    Message(
                        role="tool",
                        content=text,
                        tool_call_id=call.id,
                        # Display metadata, persisted with the turn and never
                        # sent to a provider (`Message.to_record`): the line a
                        # rebuilt transcript draws for this pair wears the
                        # bubble of the channel the message came in on.
                        channel=wanted.channel,
                    )
                )
                # **Said after the pair is written**, so a screen is told about
                # a message the conversation already holds.  It carries the
                # tool's own answer, and the receipt the submitting window
                # matches back to the request it made — no screen may infer
                # "it was folded in" from an event it could lose.
                await self._emit(
                    observer,
                    MessageCutIn(
                        text=text,
                        channel=wanted.channel,
                        receipt=wanted.receipt,
                    ),
                )
            # The tool list goes with the call, so it is asked for with the
            # call.  `specs` is read after this and never cached across
            # iterations.
            await self._retool()
            stream = self._backend.stream(messages, self._tools.specs)

            # Phase A: exhaust the stream before doing anything with it.  A tool
            # must not run inside this loop -- that would hold the provider's
            # response stream open across the tool call, risking its read
            # deadline and pinning a socket for no reason.
            await self._drain(stream.chunks, observer)

            result = await stream.result
            total_usage = total_usage + result.usage
            # Published before the batch below can be interrupted, so a
            # cancellation after this call still reports what the call cost.
            self.usage = total_usage
            self.last_usage = result.usage
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
                        iterations=iteration,
                        stop_reason=result.stop_reason or "stop",
                    ),
                )
                return TurnResult(
                    text=result.text,
                    usage=total_usage,
                    last_usage=last_usage,
                    iterations=iteration,
                    stop_reason=result.stop_reason or "stop",
                )

            # Phase B: run the whole batch, and write it down only once every
            # call of it has an answer.
            #
            # **The assistant message and its answers are appended together,
            # with no await between them.**  An assistant message whose calls are
            # only partly answered is the one history state every provider
            # rejects.  `_fit` is the caller's own callback, so it is the
            # iteration that can fail (a cabinet that will not open, a
            # discriminator that stalled), and a failed turn keeps its messages:
            # appending the message before its answers would fail every later
            # turn until the conversation was rebuilt.
            #
            # **An interruption is completed rather than dropped**, a different
            # rule from the failure above: nothing downstream could reconstruct
            # the answers the batch had already produced, and a turn the person
            # asks to continue cannot resume from a question alone.  The
            # completed batch is the one thing that survives the turn, and the
            # agent server keeps it (`send_message`'s cancellation repair).
            #
            # What `_fit` is handed is therefore the conversation *before* this
            # iteration's answer, which is also the honest list to judge a result
            # against: it contains no half-written batch.
            answers: list[Message] = []
            cutins: list[tuple[str, str, str]] = []
            for index, call in enumerate(result.tool_calls):
                text: str | None = None
                before = injected() if injected is not None else 0
                try:
                    text, _ok = await self._one(call, observer, iteration, messages)
                    fitted = await self._fit(call, text, messages)
                except asyncio.CancelledError:
                    # **The batch is completed on the way out, not dropped.**
                    # Calls that already ran keep their answers, a call whose fit
                    # was cut keeps the text it had returned, and every call that
                    # produced nothing says so.  The appends have no await
                    # between them: the interruption is in flight, and the next
                    # checkpoint would cancel this cleanup before it was written.
                    answers.extend(interrupted_results(result.tool_calls[index:], text))
                    messages.append(answer)
                    messages.extend(answers)
                    raise
                channel = ""
                receipt = ""
                took = False
                if folded is not None and injected is not None and injected() > before:
                    found = folded()
                    if found is not None:
                        channel, receipt = found
                    took = True
                answers.append(
                    Message(
                        role="tool",
                        content=fitted,
                        tool_call_id=call.id,
                        channel=channel,
                    )
                )
                if took:
                    cutins.append((text or "", channel, receipt))
            messages.append(answer)
            messages.extend(answers)
            # After the batch is in the conversation, as the auto path's event
            # is after its pair: a screen is told about a message the
            # conversation already holds, and a rebuilt transcript can draw the
            # same line from the channel on the tool message.
            for cutin_text, cutin_channel, cutin_receipt in cutins:
                await self._emit(
                    observer,
                    MessageCutIn(
                        text=cutin_text,
                        channel=cutin_channel,
                        receipt=cutin_receipt,
                    ),
                )

        # Only a capped turn ever gets here: with no cap the iterator does not
        # end, so the only way out of the loop is the return above.
        logger.warning("turn hit the %d-iteration limit", self._max_iterations)
        await self._emit(
            observer,
            TurnFinished(
                text=last_text,
                usage=total_usage,
                last_usage=last_usage,
                iterations=self._max_iterations,
                stop_reason="max_iterations",
            ),
        )
        return TurnResult(
            text=last_text,
            usage=total_usage,
            last_usage=last_usage,
            iterations=self._max_iterations,
            stop_reason="max_iterations",
        )

    async def _one(
        self,
        call: ToolCall,
        observer: TurnObserver,
        iteration: int,
        messages: Sequence[Message],
    ) -> tuple[str, bool]:
        """Run one call the way its own meta-parameters asked, and report it.

        The whole of one call sits here — the strip, the gate, the clock, the
        two events — because each of those is a fact about *this* call and the
        order between them is the thing that is easy to get wrong:

        `iteration` is the loop's own counter, and it is here for the same
        reason: the event that opens the call carries it so the panel can say
        which iteration of the turn this is (shown as `(3/30)` in the panel),
        and this is the only place that knows both the call and the iteration it
        was made in.

        `messages` travels no further than a background call needs it, and it
        is passed rather than read off an attribute because the loop does not
        own the conversation (the module docstring's "pure function over a
        message list"): it is handed one, and a call that outlives the turn it
        was made in is the one case where the *later* state of that list is what
        has to be reachable — see `_background`.

        * **Nothing is announced before the gate answers.**  A call the operator
          refused never happened as far as the transcript is concerned, and the
          reason is the display — a panel that appeared and then said "denied"
          would have told a reader the tool ran.
        * **A call that cannot be run is answered before it is announced.**  Its
          arguments could not be read, or its `_timeout` is not a positive,
          finite number; the tool is never reached, and the error is the call's
          result (`ToolRegistry.execute`).
        * **The clock starts after the announcement**, so a call's duration is
          the call and not the time somebody spent deciding.
        * **The call is announced with the arguments the model wrote**, including
          the meta-parameters, while the dispatch carries the stripped copy:
          what a reader sees is what the model said, and what the tool sees is
          what the tool takes.
        """
        # **A call this system cannot run is answered, not announced.**  Its
        # arguments could not be read at the hop that decoded it, or its
        # `_timeout` is not a positive, finite number; either way the tool is
        # never reached and the sentence is the call's result (SDD Appendix C).
        # `execute` is what spells the refusal, so there is one copy of it.
        if call.error is not None:
            return await self._tools.execute(call)

        arguments, meta = split_meta(call.arguments)
        if meta.timeout_error is not None:
            return await self._tools.execute(_not_run(call, meta.timeout_error))
        # The tool's own schema decides who enforces the deadline, so the number
        # is put where it will be read and what is left for this method is
        # whatever nobody else took (`slife2.libs.turn.tools.bind_timeout`).
        bound, timeout_error = bind_timeout(
            arguments, self._tools.spec_of(call.name), meta.timeout
        )
        if timeout_error is not None:
            return await self._tools.execute(_not_run(call, timeout_error))
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
                iteration=iteration,
                # A whole number for a turn that has a cap, and 0 for one that
                # does not — the panel's two spellings of the counter come from
                # this, so it is sent rather than left for the reader to guess.
                max_iterations=self._max_iterations,
            ),
        )
        # Timed here because this is the only place that knows when the call ran:
        # the event carried a duration from the beginning — short key, codec, two
        # tests — and the producer filled it with a zero, so the transcript could
        # not have shown one whatever it did with it.
        started = time.monotonic()
        if meta.background:
            text, ok = await self._background(dispatch, bound, messages)
        else:
            text, ok = await self._foreground(dispatch, bound)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        await self._emit(
            observer,
            ToolCallFinished(
                call_id=call.id,
                name=call.name,
                ok=ok,
                result_preview=preview(text),
                result_chars=len(text),
                elapsed_ms=elapsed_ms,
            ),
        )
        # **One line per *foreground* tool call.**  A background call has not run
        # yet when its id is returned, and `slife2.libs.harness.async_tasks` logs
        # its own schedule and finish.  Arguments are deliberately absent — they
        # can hold a path, a message or a credential, and the name and duration
        # are what a trace needs.
        if not meta.background:
            logger.info(
                "tool_call name=%s ok=%s elapsed_ms=%d result_chars=%d",
                call.name,
                ok,
                elapsed_ms,
                len(text),
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

        **Two callers, and one window.**  The ordinary one is the iteration loop
        below, where the fit happens after the call is announced and before the
        result is appended — the one moment that is both, so the panel's account
        of the call is the tool's own answer (`ToolCallFinished` carries its
        length and its head) while what enters the conversation is what the model
        will read back later.  The other is a **background** call's hand-back
        (`_background`), which is the same result arriving by a different road:
        it becomes a message in a later turn rather than a tool message in this
        one, and it used to skip this iteration entirely for exactly that
        reason.
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
            logger.warning("tool_timeout name=%s timeout=%s", dispatch.name, bound)
            return timed_out(dispatch.name, bound), False

    async def _background(
        self,
        dispatch: ToolCall,
        bound: float | None,
        messages: Sequence[Message],
    ) -> tuple[str, bool]:
        """Hand a call to the background, and answer the id the model polls.

        **The bound applies only when the model asked for one.**  Backgrounding
        exists to escape the bounds a foreground call is subject to, so applying
        the foreground's anyway would undo the thing that was asked for.  A tool
        with its own `timeout` argument has already been handed the number; a
        tool without one is wrapped when the model named a number and runs bare
        when nobody did — the same mapping the foreground path uses, which is
        why this method takes `bound` rather than deciding anything.

        **The window reaches this road too.**  A background call's result arrives
        as a *message* — a turn of its own, several turns later — which is the
        one road a tool result takes that the iteration loop's fit does not
        cover, so
        the whole output used to enter the conversation and its turn log and be
        carried by every later request: one call's answer was bounded or
        unbounded depending only on whether the background delivery landed.
        The fit here is the same `_fit`, over the same result, so what enters the
        conversation is bounded the same way — and it is applied to the *body*
        only, at the far end (`slife2.libs.harness.async_tasks.hand_back`), because
        the marker and the sentence that name the call this answers are not
        something a window may take away.

        `messages` is handed over as the list it is and not as a copy: the
        hand-back can arrive long after this turn ended — after a rebuild has
        replaced the conversation, even — and the honest thing for the
        discriminator to be shown is the conversation as it stands *then*, which
        only a reference to the live list can be.
        """

        async def fit(body: str) -> str:
            """What the conversation carries for this result, decided when it lands."""
            return await self._fit(dispatch, body, messages)

        # **The call's own id and name go with it**, because nothing else can
        # supply them later: `check_async` is given a task id and nothing else —
        # a call the model makes an iteration or a turn afterwards is carrying
        # none of
        # this — and a result that cannot say which call it answers is one the
        # model has to match up from memory.
        task_id = schedule(
            self._timed(dispatch, bound),
            call_id=dispatch.id,
            tool=dispatch.name,
            deliver=self._deliver,
            fit=fit,
        )
        # The sentence goes under the marker the registry wrote
        # (`slife2.libs.harness.async_tasks.started`): the sentence is what the model acts on,
        # and the marker is what a screen and a reader of the transcript pair
        # this call's two halves by.
        return started(task_id, dispatch.id, dispatch.name), True

    async def _timed(self, dispatch: ToolCall, bound: float | None) -> tuple[str, bool]:
        """One background call's answer, under the model's own bound if it set one.

        Separate from `_background` only because the clock has to run *inside*
        the scheduled task: the bound fires long after `_background` has returned
        the task id, so a `wait_for` around the schedule would be a bound on
        nothing.  That is also why the timeout is caught here rather than left to
        `slife2.libs.harness.async_tasks._runner`: what escapes a task is rendered as
        `Error: {type}: {exc}`, and a bare `TimeoutError` carries no message — so
        the conversation used to read `Error: TimeoutError: ` where the
        foreground path says which tool broke which bound.  **The same sentence,
        from the same builder** (`timed_out`), because a bound that fired is one
        fact and it must not read two ways.
        """
        if bound is None:
            return await self._result(dispatch)
        try:
            return await asyncio.wait_for(self._result(dispatch), timeout=bound)
        except TimeoutError:
            logger.warning("tool_timeout name=%s timeout=%s", dispatch.name, bound)
            return timed_out(dispatch.name, bound), False

    async def _result(self, dispatch: ToolCall) -> tuple[str, bool]:
        """One call's answer, as its text *and* whether it worked.

        **The pair travels now, and the old argument for dropping it is the one
        SDD Appendix C forbids.**  `_ok` used to be thrown away on the grounds
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
