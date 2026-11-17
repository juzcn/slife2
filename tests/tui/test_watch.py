"""The read-only screen that watches one subagent's conversation.

The screen is driven by a scripted `WatchClient`, which is the seam the real one
crosses: a `Snapshot`, then `TurnStarted` and the turn events, exactly as the
agent server pushes them (`slife2.libs.turn.watch`).  Nothing here needs a
server, so these belong in the fast lane.
"""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeWatchClient
from textual.app import App
from textual.widgets import Static

from slife2.libs.turn.events import (
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    UsageReported,
)
from slife2.libs.turn.messages import Message, Usage
from slife2.libs.turn.watch import Closed, Snapshot, TaskBrief, TurnStarted
from slife2.tui.watch import WatchScreen
from slife2.tui.widgets import ChatView, HistoryInput, PlanView

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

SIZE = (100, 30)


def stored_turn(
    asked: str = "count to three",
    answered: str = "one two three",
    *,
    channel: str = "agent:jack",
) -> dict:
    """One stored turn, in the shape `restore` draws."""
    return {
        "turn_id": 1,
        "messages": [
            Message(role="user", content=asked).to_record(),
            Message(role="assistant", content=answered).to_record(),
        ],
        "created_at": "",
        "completed_at": None,
        "channel": channel,
        "what_model": "fake",
        "token_count": 0,
        "context_tokens": 0,
    }


def finished(text: str) -> TurnFinished:
    return TurnFinished(
        text=text,
        usage=Usage(),
        last_usage=Usage(),
        iterations=1,
        stop_reason="stop",
    )


@pytest.mark.asyncio
async def test_the_screen_draws_the_history_the_message_and_the_live_answer() -> None:
    """Three layers, in the order a watcher receives them.

    The snapshot is the conversation the store holds; `TurnStarted` is the
    message its caller sent and a watcher never saw; the deltas and the final
    answer are the turn as it happens.  The bubble on that message is the
    channel's, so the maker is named by the same function every other surface
    uses — and it is `Agent(jack)>`, not `Subagent`: the sender is the agent
    that made this subagent.
    """
    watch = FakeWatchClient(
        (
            Snapshot([stored_turn()]),
            TurnStarted("what is 6*7?", "agent:jack"),
            TextDelta("It is "),
            # The bar's per-call report crosses the watch stream too; this
            # screen has no bar, and drawing it would put a figure in the
            # transcript nobody typed (see `draw`'s module docstring).
            UsageReported(last_usage=Usage(completion_tokens=9)),
            TextDelta("42."),
            finished("It is 42."),
        )
    )
    app: App[None] = App()
    async with app.run_test(size=SIZE) as pilot:
        app.push_screen(WatchScreen("scout", lambda: watch))
        await asyncio.wait_for(watch.watching.wait(), timeout=5)
        await pilot.pause()

        chat = app.screen.query_one(ChatView)
        text = chat.plain_text()
        assert "count to three" in text, "the stored question came back"
        assert "one two three" in text, "and its answer"
        assert "what is 6*7?" in text, "the turn's own message"
        assert "Agent(jack)>" in text, "the maker's bubble, not its own word"
        assert "It is 42." in text, "and the answer as it arrived"
        assert text.count("It is 42.") == 1, "deltas and the result are one block"
        assert "9 tokens" not in text, "the bar's figure is not the watcher's line"

        # A subagent has no window, and this screen is not one: there is no
        # prompt to type into.
        assert not app.screen.query(HistoryInput)

        # Escape leaves, and the call with it.
        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()
        assert len(app.screen_stack) == 1, "back to the window"
        assert watch.closed is True, "the watch call was cancelled"


@pytest.mark.asyncio
async def test_a_task_watch_draws_the_brief_and_signs_with_the_task() -> None:
    """A task's description is a block, not a user message.

    Nobody in the conversation said it, so it does not wear `You>` or any
    sender's bubble; the assistant's words wear the task's own bubble, the same
    `AsyncTask(name)>` the window uses for its answer.  The description is model
    text and is drawn as `Text`, so brackets in it are not markup.
    """
    watch = FakeWatchClient(
        (
            Snapshot([]),
            TaskBrief("research", "Find [the] retry policy.", "running"),
            TextDelta("It is in retry.py."),
            finished("It is in retry.py."),
            Closed("the task finished"),
        )
    )
    app: App[None] = App()
    async with app.run_test(size=SIZE) as pilot:
        app.push_screen(WatchScreen("research", lambda: watch, task=True))
        await asyncio.wait_for(watch.watching.wait(), timeout=5)
        await pilot.pause()

        header = str(app.screen.query_one("#watch-header", Static).content)
        assert "watching task research" in header
        brief = app.screen.query_one("#watch-brief", Static)
        assert brief.display is True, "the brief is shown only when it arrives"
        brief_text = str(brief.content)
        assert "task research — running" in brief_text
        assert "Find [the] retry policy." in brief_text, "markup in model text is text"

        text = app.screen.query_one(ChatView).plain_text()
        assert "AsyncTask(research)>" in text
        assert "It is in retry.py." in text

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_the_watched_plan_is_drawn_and_updated_by_a_write() -> None:
    """The strip is the watcher's too: the snapshot carries the plan, and a
    plan write in the watched turn reaches it through the same rule the window
    uses (`PlanView.observe`)."""
    watch = FakeWatchClient(
        (
            Snapshot(
                [],
                [{"text": "read the config", "status": "active"}],
            ),
        )
    )
    app: App[None] = App()
    async with app.run_test(size=SIZE) as pilot:
        app.push_screen(WatchScreen("scout", lambda: watch))
        await asyncio.wait_for(watch.watching.wait(), timeout=5)
        await pilot.pause()

        view = app.screen.query_one(PlanView)
        assert "plan 0/1" in view.plain_text()
        assert "read the config" in view.plain_text()

        watch.send(
            ToolCallStarted(
                call_id="p1",
                name="plan_write",
                arguments={"items": [{"text": "write the tests", "status": "done"}]},
            )
        )
        watch.send(
            ToolCallFinished(
                call_id="p1",
                name="plan_write",
                ok=True,
                result_preview='{"done": 1, "total": 1}',
                result_chars=24,
                elapsed_ms=1,
            )
        )
        await pilot.pause()

        assert "plan 1/1" in view.plain_text()
        assert "write the tests" in view.plain_text()

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_a_closed_turn_leaves_no_parked_plan_write() -> None:
    """The watcher's settle: a write whose finish never came is parked no
    longer than the turn that started it.

    `Closed` is the watched turn's ending (cancelled, failed, link dropped),
    and it settles the strip's parked writes as the window's endings do — a
    later frame reusing the id finds nothing parked, and the stale items are
    not drawn as a committed plan.
    """
    watch = FakeWatchClient(
        (
            Snapshot([]),
            TurnStarted("plan it", "agent:jack"),
            ToolCallStarted(
                call_id="p1",
                name="plan_write",
                arguments={
                    "items": [{"text": "a plan never committed", "status": "active"}]
                },
            ),
            Closed("the turn was cancelled"),
        )
    )
    app: App[None] = App()
    async with app.run_test(size=SIZE) as pilot:
        app.push_screen(WatchScreen("scout", lambda: watch))
        await asyncio.wait_for(watch.watching.wait(), timeout=5)
        await pilot.pause()

        view = app.screen.query_one(PlanView)
        assert not view._started, "the closed turn's write leaked"
        assert not view.display, "a write never committed draws no plan"

        # A later turn reuses the id and finishes it without a start of its
        # own: nothing is parked for it, so the stale items are not drawn.
        watch.send(TurnStarted("again", "agent:jack"))
        watch.send(
            ToolCallFinished(
                call_id="p1",
                name="plan_write",
                ok=True,
                result_preview="{}",
                result_chars=2,
                elapsed_ms=1,
            )
        )
        await pilot.pause()
        text = view.plain_text()

    assert "a plan never committed" not in text, (
        "a reused call id popped the closed turn's stale write"
    )


@pytest.mark.asyncio
async def test_a_closed_conversation_says_why_and_keeps_the_screen() -> None:
    """`Closed` is the last frame, not the end of the view.

    The server ends the stream when a subagent is removed; the screen stays up
    with the news, because the history it is showing is still worth reading and
    a view that vanished would leave a person unsure what they had been looking
    at.
    """
    watch = FakeWatchClient(
        (Snapshot([]), Closed("scout was removed")),
        finish=True,
    )
    app: App[None] = App()
    async with app.run_test(size=SIZE) as pilot:
        app.push_screen(WatchScreen("scout", lambda: watch))
        await asyncio.wait_for(watch.watching.wait(), timeout=5)
        await pilot.pause()
        await pilot.pause()

        text = app.screen.query_one(ChatView).plain_text()
        assert "watching ended: scout was removed" in text
        assert len(app.screen_stack) == 2, "the screen stayed; only the call ended"

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_a_watch_that_cannot_open_says_so() -> None:
    """A connection that fails is a line on the screen, not a blank one."""
    watch = FakeWatchClient(connect_error=ConnectionError("no agent server"))
    app: App[None] = App()
    async with app.run_test(size=SIZE) as pilot:
        app.push_screen(WatchScreen("scout", lambda: watch))
        await pilot.pause()
        await pilot.pause()

        text = app.screen.query_one(ChatView).plain_text()
        assert "could not be watched" in text
        assert "no agent server" in text

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_a_stream_that_does_not_open_with_history_is_said_so() -> None:
    """The first frame is the snapshot by contract; one that is not is damage.

    Drawn as though the live half were the whole conversation, a stream that
    lost its history would read as an agent that had never spoken before — a
    failure presented as a fact (`SDD Appendix C).
    """
    watch = FakeWatchClient((TextDelta("orphan"),))
    app: App[None] = App()
    async with app.run_test(size=SIZE) as pilot:
        app.push_screen(WatchScreen("scout", lambda: watch))
        await asyncio.wait_for(watch.watching.wait(), timeout=5)
        await pilot.pause()

        text = app.screen.query_one(ChatView).plain_text()
        assert "did not open with the conversation's history" in text

        await pilot.press("escape")
        await pilot.pause()
