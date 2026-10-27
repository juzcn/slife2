"""The harness's background tasks: `_async: true` on one tool call.

A call the model marks `_async` runs in the background and the model is handed a
task id instead of a result.  It may ask where that call stands (`check_async`)
and may abandon it (`cancel_async`) — but it does not have to ask for the answer:
**the result is handed back into the conversation when it arrives**, which is what
makes backgrounding a thing a model can use rather than a thing it must babysit.

The rules:

* **The registry is the process's**, keyed by task id alone.  It is not keyed by
  conversation: the task belongs to the *call*, not to the conversation that
  happened to make it.  What each task does know is **where to hand its result
  back to** (`Task.deliver`, bound when the call was made), because that is a fact
  about the call and nowhere else.
* **Nothing bounds a background task.**  Not a lifetime, not any default: the
  point of backgrounding is to leave the in-turn bounds behind, so a bound that
  applied anyway would undo the thing that was asked for.  The only bound a
  backgrounded call ever has is the model's own `_timeout`, plus the registry
  cap below, which is about memory rather than time.
* **A turn being cancelled does not cancel the task.**  The turn's cancellation
  reaches the calls it is waiting on; this one it is no longer waiting on.
* **Nothing survives the process**, and nothing runs on the way out to make
  anything survive.  A task is memory, and memory is what a process exit takes.

What a background call's result *is*
------------------------------------
Every piece of news about a call is written as `[AgentToolResult:{…}]` — the
marker shape `slife2.libs.harness.markers` describes — carrying the tool call's id, the tool's
name, the task id and the status.  Three readers, and each wants a different
half:

* **the model**, which learns the task id from the first one and, from the last,
  which of its calls an answer belongs to.  That pairing is the whole reason the
  id is in there: the answer arrives as a message of its own, in a later turn
  possibly, and without the id the two are related only by the model's memory;
* **a screen**, which draws a call that has not finished as running rather than
  as a fast one that returned a paragraph (the window registers this prefix and
  takes it off before drawing);
* **anyone reading a transcript**, for whom the sentence after the marker says
  the same thing in words.

**Finished, and pushed.**  A call that completes hands its answer to the
conversation it was made in, on `channel_for(tool)` — `async-tool:<tool>`, so
the bubble it draws names the call — by the road every other piece of
background work in this system takes (`slife2.libs.harness.handover`: the window, and a
failure if it is not there).  A call that is *cancelled* is handed back too —
a cancellation the conversation never hears about would leave a panel on screen
that is running forever.

So **`check_async` answers where the call is**, not what it said: running,
finished, failed, cancelled.  The one thing it still carries is a result that
**could not be handed back** — no window was showing that conversation — because
the alternative to returning it there is losing it, and an answer nobody can ask
for a second time is worse than one that arrives a turn late.

Why a module rather than a corner of `slife2.libs.turn.tools`
---------------------------------------------------
`slife2.libs.turn.tools` is the loop's *vocabulary* — names, a spec, and the one rule that
a failure is text.  This is machinery with state, and it has two more callers
besides the loop: the agent server serves the two tools, and it is the one
process that can hand a result back.  One module, three callers, and
`slife2.libs.turn.tools` stays a leaf that a task registry would have made it
stop being.

**The two tools are model tools and are managed like every other one.**  The
agent server declares them as MCP tools, so the toolhub catalogues them, the
operator's `tool_load.autoload` keeps them loaded, and the budget may not take
them away (`REQUIRED_AUTOLOAD` / `ALWAYS_LOADED`).  They are not a private
registry the model cannot see: their bodies live here because the task table is
this process's memory, not because their names are outside the catalogue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from slife2.libs.turn.tools import ToolFailed

logger = logging.getLogger(__name__)

#: The two tools the model polls and cancels with.  Bare names, no leading
#: underscore: they are the model's tools, not the machinery's — the agent
#: server serves them and the harness never calls them
#: (`slife2.libs.turn.tools.is_harness` is about names the *machinery* calls, and
#: this is the opposite case).
CHECK_ASYNC = "check_async"
CANCEL_ASYNC = "cancel_async"

#: The channel a handed-back result arrives on, and the first half of what it is
#: keyed by: `async-tool:pdf_to_md`.  The same job `slife2.plugins.wechat.server`'s and
#: `slife2.plugins.agent_task.server`'s channel names do — with the tool's name in it for
#: the reason theirs carry theirs: a hand-over carries the channel and the words
#: and nothing else, and the window draws the bubble from the channel alone
#: (`slife2.tui.widgets.channel_prefix`), so `AsyncTool(pdf_to_md)> ` is a fact
#: the channel has to carry rather than one the window could guess.
CHANNEL = "async-tool"

#: What one background call wears in every result it produces.  The prefix is
#: this module's, the way every writer names its own marker (`slife2.libs.harness.markers`
#: holds the shape and no name).
#:
#: **`AgentToolResult` rather than a name built from this process's word for
#: backgrounding.**  The model is the reader, and what it has to recognise in a
#: later turn is the *result of one of its own tool calls* — so the name says
#: that, and the `tool` and `call_id` inside say which.  It reads beside
#: `slife2.plugins.agent_task.server`'s `[AgentTaskResult:{…}]`: two results, one from a
#: tool call and one from a task, named so that neither can be taken for the
#: other.
MARKER_PREFIX = "[AgentToolResult:"

#: The four statuses, spelled once: the model reads them, a screen matches on
#: them, and two spellings of "finished" would be a screen drawing a finished
#: call as one still running.
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
CANCELLED = "cancelled"

#: How one result reaches its conversation: the text and the channel it goes in
#: on, "did it land" out.  `True` means a conversation took it; `False` and
#: `None` both mean "not provably delivered", which is what makes `check_async`
#: offer the text instead (`slife2.libs.harness.handover` is where the three answers are
#: argued).
#:
#: **The channel travels with the words** rather than being worked out at the far
#: end, because the far end is a server that knows which *conversation* this is
#: and nothing about the call: the tool's name is here (`Task.tool`), and a
#: channel is a fact about the message rather than about the transport.
Deliver = Callable[[str, str], Awaitable[bool]]

#: How a result is **fitted** before it is handed back: the tool's own text in,
#: what the conversation carries out — or `None` for a caller that has nothing to
#: fit with, in which case the result goes in as it came.
#:
#: **Here rather than in the hand-back itself**, because fitting a result is
#: choosing what *that conversation* needs from it and this registry knows
#: nothing about any conversation (`slife2.libs.harness.dumps` is where the
#: choice is made).  It is a second callback beside `Deliver` rather than part of
#: it because the two answer different questions and are supplied by different
#: parties: the loop has the window, and only the server has the conversation —
#: and a delivery that is *not* fitted is still a delivery worth making.
Fit = Callable[[str], Awaitable[str]]


@dataclass
class Task:
    """One background call: the work, what it *was*, and where it goes.

    The call id and the tool name are kept because nothing else can supply them
    later: `check_async` is given a task id and nothing else — a call the model
    makes a step or a turn afterwards is carrying none of the arguments of the
    one it is asking about — and news that cannot say which call it is about is
    news somebody has to match up from memory.
    """

    handle: asyncio.Task[str]
    #: The tool call this task is running, as the transcript names it.
    call_id: str
    tool: str
    status: str = RUNNING
    #: Where the result goes when it is ready.  Bound when the call was made,
    #: because that is the one moment the conversation is known —
    #: `slife2.plugins.agent.server` closes over its own loop and hands this in.  None
    #: for a caller with no conversation to reach (a test, a worker), and then
    #: the result stays here for `check_async` to hand over.
    deliver: Deliver | None = None
    #: How the result is fitted before it goes into the conversation, or None for
    #: a caller with no window to apply.  Bound when the call was made, beside
    #: `deliver` and for the same reason: it is a fact about the *call* — the
    #: loop's own `_fit`, closed over the call and the conversation it was made
    #: in — and nowhere else.
    fit: Fit | None = None
    #: Whether it got there, so `check_async` can either confirm it or offer the
    #: text before it is dropped.
    delivered: bool = False
    #: The result itself, held only until it is handed back — dropping it is the
    #: point of handing it back, and a task that keeps its answer is a task that
    #: keeps everything the answer held.
    result: str = ""


#: The running and finished tasks, by id.  Process-wide and insertion-ordered, so
#: the prune below walks oldest-first without a second structure to keep in step
#: with it.
_tasks: dict[str, Task] = {}

#: Hand-backs in flight that nobody awaits.  Held for the reason
#: `slife2.plugins.toolhub.server`'s verdicts are: a task nothing references can be collected
#: before it runs, and one that raises into nobody is an exception reported
#: against whatever code happens to be running.
_handbacks: set[asyncio.Task[None]] = set()

#: Bound on `_tasks` — an `_async: true` call the model never polls would
#: otherwise keep its task in the dict (holding whatever the tool holds) for the
#: whole session.  Only *finished* tasks are ever dropped: a running one is
#: work, and forgetting it would be the one way to lose it.
MAX_ASYNC_TASKS = 100


def marker(status: str, *, call_id: str = "", tool: str = "", task_id: str = "") -> str:
    """One line of machine-readable state, as every marker in this system is written.

    `ensure_ascii` off, unlike the turn-event codec: this is content in a
    conversation rather than a payload on a progress channel, and a tool name
    that is not ASCII should read as itself in a transcript.
    """
    fields = {"call_id": call_id, "tool": tool, "task_id": task_id, "status": status}
    return f"{MARKER_PREFIX}{json.dumps(fields, ensure_ascii=False)}]"


def channel_for(tool: str) -> str:
    """The channel a background call's result arrives on: `async-tool:<tool>`.

    The name is in it because the window draws the bubble from the channel and
    nothing else (`slife2.tui.widgets.channel_prefix`), exactly as an agent
    task's answer carries its task's name — and what a person watching needs is
    the *tool*, since one conversation can have several calls in the background
    at once.
    """
    return f"{CHANNEL}:{tool}"


def started(task_id: str, call_id: str, tool: str) -> str:
    """What the model reads when a call has been handed to the background.

    The sentence under the marker is what the model acts on (it names the two
    calls that work on the id), and the marker is what everything else acts on.
    It does not say "poll for the result": the result comes back on its own.
    """
    return (
        f"{marker(RUNNING, call_id=call_id, tool=tool, task_id=task_id)}\n"
        f"✓ Async task started.\n"
        f"  task_id: {task_id}\n"
        f"  tool: {tool}\n"
        f"  Its result will be handed back to this conversation when it is done.\n"
        f'  Ask where it stands with {CHECK_ASYNC}(task_id="{task_id}").\n'
        f'  Abandon it with {CANCEL_ASYNC}(task_id="{task_id}").'
    )


def handed_back(task: Task, task_id: str, status: str, body: str) -> str:
    """What the conversation is given when a background call ends.

    Written for a model that is reading a *later* turn: it names the call the
    news is about (the marker and the sentence), and then the result itself.  The
    task id stays in it even though the work is over — a model that wants to
    check or cancel it anyway is a model this line should not lie to.
    """
    head = marker(status, call_id=task.call_id, tool=task.tool, task_id=task_id)
    if status == CANCELLED:
        return (
            f"{head}\nThe background call to {task.tool} was cancelled, so it "
            f"has no result."
        )
    what = "failed" if status == FAILED else "finished"
    return f"{head}\nThe background call to {task.tool} {what}. Its result:\n\n{body}"


def _prune_finished() -> None:
    """Drop the oldest finished tasks while over the cap; running ones stay."""
    if len(_tasks) <= MAX_ASYNC_TASKS:
        return
    for task_id in list(_tasks):  # insertion order = oldest first
        if len(_tasks) <= MAX_ASYNC_TASKS:
            break
        if _tasks[task_id].handle.done():
            _tasks.pop(task_id, None)


def schedule(
    coro: Awaitable[tuple[str, bool]],
    *,
    call_id: str,
    tool: str,
    deliver: Deliver | None = None,
    fit: Fit | None = None,
) -> str:
    """Start `coro` in the background and answer the id the model polls.

    `asyncio.create_task`, so the task is not a child of the turn that started
    it: cancellation reaches what the loop is awaiting, and the loop has stopped
    awaiting this.

    **What it answers with is a pair**, `(text, ok)` — the same pair
    `ToolRegistry.execute` answers a foreground call with.  A background task
    stores text for the model to read, but whether that text is an answer or a
    failure is not something a reader of the string can decide, and this
    registry is the thing that has to decide it (see `_runner`).

    `fit` is kept with the task rather than applied here, because the result it
    is for does not exist yet — see `Fit` and `hand_back`.
    """
    task_id = uuid.uuid4().hex[:8]
    _tasks[task_id] = Task(
        handle=asyncio.create_task(_runner(coro, task_id)),
        call_id=call_id,
        tool=tool,
        deliver=deliver,
        fit=fit,
    )
    _prune_finished()
    logger.info("async_task_scheduled id=%s tool=%s", task_id, tool)
    return task_id


async def _runner(coro: Awaitable[tuple[str, bool]], task_id: str) -> str:
    """Await one background call, then hand its result back.

    **The hand-back is the delivery, not a favour to `check_async`.**  A call
    that finished while nobody was looking is a call whose answer is one message
    away from the conversation that asked for it; waiting to be asked leaves it
    one poll away instead, and a model that has moved on never asks.

    **A failure is a result here**, exactly as it is everywhere else in this
    system (`slife2.libs.turn.tools`): the text goes back, the marker says `failed`, and
    the model reads it as a call it can retry — where an exception raised into
    the conversation would have no reader at all.

    **The status is the caller's answer, not a reading of its prose.**  The
    coroutine answers `(text, ok)` — the pair a foreground call already comes
    back as — and `ok` is what `check_async` reports.  This used to ask whether
    the text began `"Error"`, which made a verdict out of a spelling: an answer
    that legitimately begins "Error" was recorded `failed`, and a tool the
    registry itself marked `ok=False` without that prefix was recorded
    `finished` (SDD Appendix C, "a failure is not a verdict").  A coroutine
    that *raises* is the one case left, and a raise is a failure because it is
    the only thing it can be.
    """
    try:
        result, ok = await coro
    except Exception as exc:  # noqa: BLE001 — the conversation reads this, so it is text
        result, ok = f"Error: {type(exc).__name__}: {exc}", False
    task = _tasks.get(task_id)
    if task is not None:
        task.status = FINISHED if ok else FAILED
        task.result = result
    logger.info(
        "async_task_done id=%s status=%s len=%d",
        task_id,
        task.status if task is not None else "?",
        len(result),
    )
    if task is not None and task.deliver is not None:
        await hand_back(task, task_id, task.status, result)
    return result


async def hand_back(task: Task, task_id: str, status: str, body: str) -> None:
    """Put one piece of news about a call into the conversation it came from.

    **Not raised when it cannot land.**  `deliver` answers whether it did, the
    task records that, and `check_async` offers the text to whoever asks next —
    so a hand-back that failed is a result the model can still get, rather than
    an error with no reader.  The failure is still *said*, at `warning` and
    naming the task: `delivered=False` is the fact, but a fact with no reason
    beside it is a delivery nobody can explain.

    A result that *did* land is dropped here: holding it would be keeping an
    answer nobody can ask for twice, which is the mistake in the other direction
    (`slife2.plugins.agent_task.server` drops its task's text for the same reason).

    The channel goes with the words (`channel_for`): a cancelled call, a failed
    one and a finished one all come back the same way, and the bubble a person
    sees has to say which tool it was about.

    **And the window is applied here, to the body alone.**  A background call's
    result reaches the conversation as a *message* — a turn of its own, later —
    which is the one road a tool result takes that the loop's own fit does not
    cover: `slife2.libs.turn.loop._fit` runs where a result is appended to a turn,
    and this one arrives in a turn of its own, so without this the whole output
    entered the conversation and its log and was carried by every later request.
    The fit is over the tool's own words and not over the marker and sentence
    above them: the model has to be able to name the call this answers, and only
    the output is what a window is a window over.  A fit that raises is an
    ordinary delivery failure, for the reason the `except` is here at all — the
    result stays readable to whoever asks next.
    """
    if task.deliver is None:
        return
    try:
        if task.fit is not None:
            body = await task.fit(body)
        task.delivered = bool(
            await task.deliver(
                handed_back(task, task_id, status, body), channel_for(task.tool)
            )
        )
    except Exception as exc:  # noqa: BLE001 — it is recorded, not raised
        logger.warning(
            "async task %s: the answer could not be handed back: %s", task_id, exc
        )
    if task.delivered:
        task.result = ""


def _get_task(task_id: str) -> Task | None:
    return _tasks.get(task_id)


def _pop_task(task_id: str) -> Task | None:
    return _tasks.pop(task_id, None)


def _not_found(task_id: str) -> str:
    """The one sentence both tools refuse an id they do not hold with.

    **A miss is a refusal and not an answer**, which is the half of these two
    tools that was wrong: the sentence was *returned*, so a call naming a task
    nobody ever made came back as a call that succeeded — an `ok` with "Error:"
    in its text — and a transcript that reads a failure as a success is the one
    thing this system's tool contract exists to prevent.
    """
    return (
        f"no async task {task_id!r} here: either it was never started, or it "
        f"finished and has been read already — the task_id comes from the call "
        f"that started it."
    )


def _answer(task: Task, task_id: str) -> dict[str, Any]:
    """One task, as the object both tools answer with.

    Four facts, and the answer itself only when the conversation has not been
    given it: the id, the status, the tool and call the task is about, and —
    **only when there is one to hand over** — the result.  Whether the hand-back
    landed is this process's bookkeeping, not a fact the model can act on: a
    result that is present is one nobody has seen, and its absence is the whole
    of "there is nothing to hand over".

    **A result in flight is not a result to hand over.**  `_runner` records the
    ending before it awaits `hand_back`, so there is a window in which the call
    is over and its delivery has no verdict yet — the work is not still running,
    and the hand-back has not returned, which is not the same as having failed.
    Offering the text in that window would hand the model the same answer twice
    (once here, once as the message it is about to become), and popping the task
    there would throw the only copy away if that delivery then failed.  So
    `result` appears only once `handle` is done, and its *absence* is what
    "there is nothing to hand over" looks like — whichever of the several ways
    that happens to be true.
    """
    answer: dict[str, Any] = {
        "task_id": task_id,
        "status": task.status,
        "tool": task.tool,
        "call_id": task.call_id,
    }
    if task.result and not task.delivered and task.handle.done():
        answer["result"] = task.result
    return answer


async def check_async(arguments: dict[str, Any]) -> dict[str, Any]:
    """Answer where one background call stands.

    **Status, not a result**, because the result is handed back on its own —
    the answer here is only where the call stands.  What is still carried here
    is the *undelivered* result: a call whose hand-back did not provably reach
    a conversation stays readable until somebody reads it, and then it goes.

    Reading is what removes a finished task from the registry:
    a task read twice would otherwise be a task kept for the whole session.
    """
    task_id = str(arguments.get("task_id") or "")
    task = _get_task(task_id)
    if task is None:
        raise ToolFailed(_not_found(task_id))
    # **Final status is not the same moment as a finished delivery.**  `_runner`
    # records the ending before it awaits `hand_back`, so between the two the
    # call is over and its answer is still on its way — and this asked
    # `task.handle.done()` *instead of* the status, so that window fell into the
    # finished branch: the model was told "its result could not be handed back"
    # a moment before the message landed (reading the same result twice, once as
    # a tool answer and once as a turn), the task was popped, and the next poll
    # answered "not found".  What decides here is the status; the handle decides
    # whether the *delivery* has an outcome yet, and popping before it has one
    # would drop the only copy of the result if it fails.
    if task.status == RUNNING or not task.handle.done():
        return _answer(task, task_id)
    _pop_task(task_id)
    return _answer(task, task_id)


async def cancel_async(arguments: dict[str, Any]) -> dict[str, Any]:
    """Abandon one running background call, and tell the conversation it is gone.

    **The cancellation is handed back too.**  A person watching saw the call
    start, and a cancellation nobody hears about leaves a panel on screen that is
    running forever; the model, in a later turn, reads the same line and knows
    the id it holds is done with.

    **A call that already finished is *not* cancelled, and its answer is not
    thrown away.**  There is nothing to cancel — the work is over — but a task
    whose hand-back never provably landed is still holding the one copy of its
    result (`Task.delivered`/`result`), and popping it while answering only
    "nothing to cancel" would lose that copy for good.  SDD §7.7 says the
    alternative to returning a result is losing it, and losing it in silence is
    the worse of the two, so this case is answered with exactly what
    `check_async` would have said.  What tells the two apart is `status`, and
    nothing extra has to: `cancelled` is the word that means *this* call stopped
    it, and the call's own ending means it was already over when the call
    arrived.

    The hand-back is detached rather than awaited: this runs *inside* the turn
    that called it, and `slife2.libs.harness.handover` reaching the agent server for that same
    conversation means a `send_message` that queues behind the very turn these
    words are blocking — waiting for it here would wait for ourselves.
    """
    task_id = str(arguments.get("task_id") or "")
    task = _get_task(task_id)
    if task is None:
        raise ToolFailed(_not_found(task_id))
    if task.status != RUNNING:
        # **Nothing to cancel, and nothing to throw away.**  The work is over —
        # and when its handle is still running, what is running is the hand-back
        # of a result that already exists: `task.handle.cancel()` there aborted
        # the delivery mid-flight and this answered "was cancelled, so it has no
        # result" about a call that had completed, which is the case this
        # function's own docstring says cannot happen.  The task is kept while
        # that delivery is in flight, for `check_async`'s reason.
        if not task.handle.done():
            return _answer(task, task_id)
        _pop_task(task_id)
        return _answer(task, task_id)
    task.handle.cancel()
    _pop_task(task_id)
    # **Recorded as well as reported.**  The field is the one thing that says
    # which of the two tools ended this call, and it is left behind *before* the
    # detached hand-back reads the task — `handed_back` writes the marker with a
    # status of its own, and a task whose field still said `running` would be a
    # task two readers describe differently.
    task.status = CANCELLED
    logger.info("async_task_cancelled id=%s", task_id)
    if task.deliver is not None:
        handback = asyncio.create_task(hand_back(task, task_id, CANCELLED, ""))
        _handbacks.add(handback)
        handback.add_done_callback(_handbacks.discard)
    return _answer(task, task_id)


__all__ = [
    "CANCEL_ASYNC",
    "CHANNEL",
    "CHECK_ASYNC",
    "MARKER_PREFIX",
    "MAX_ASYNC_TASKS",
    "cancel_async",
    "channel_for",
    "check_async",
    "marker",
    "schedule",
    "started",
]
