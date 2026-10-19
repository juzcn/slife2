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

from slife2.libs.turn.events import TextDelta, TurnFinished
from slife2.libs.turn.messages import Message, Usage
from slife2.libs.turn.watch import Closed, Snapshot, TurnStarted
from slife2.tui.watch import WatchScreen
from slife2.tui.widgets import ChatView, HistoryInput

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
    failure presented as a fact (`DESIGN.md` §10.1).
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
