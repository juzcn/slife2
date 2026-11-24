"""The two pickers, and the machinery they share.

`ModelPicker` and `ConversationPicker` are one question in two lists: the same
four keys, the same cursor, the same one-shot future.  `tests/tui/test_tui.py`
drives them through the window and pins what each choice *does*; what is left
to pin here is the machinery both inherit — **a blur cancels**, and a decided
picker ignores the blur that follows it.  Those two rules are what keep the key
that opened a picker from latching forever on a question nobody answered.

The rows are built in `compose`, not handed in, for `tests/tui/test_approval.py`'s
reason: a `Static` measures itself against the app's console when it is given
content, so constructing one outside a running app raises `NoActiveAppError`.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App

from slife2.tui.widgets import (
    ConversationPicker,
    ModelChoice,
    ModelPicker,
    WatchChoice,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class Host(App):
    """A window whose whole content is the picker the test asks for."""

    def __init__(self, build) -> None:
        super().__init__()
        self._build = build

    def compose(self):
        self.picker = self._build()
        yield self.picker


def model_picker(future: asyncio.Future) -> ModelPicker:
    return ModelPicker(
        [ModelChoice("local/big"), ModelChoice("local/small")], "local/big", future
    )


def conversation_picker(future: asyncio.Future) -> ConversationPicker:
    return ConversationPicker([WatchChoice("subagent", "scout")], future)


@pytest.mark.parametrize("build", [model_picker, conversation_picker])
async def test_a_picker_blurred_without_a_decision_resolves_to_nothing(build) -> None:
    """A question abandoned is a question answered with nothing.

    The picker is dismissed by a click on the transcript as easily as by `Esc`,
    and one left undecided holds the future the key that opened it is waiting
    on: the latch would never clear, so every later press of the key would do
    nothing.  The blur is what answers it, and the answer is no choice.
    """
    future: asyncio.Future = asyncio.Future()
    host = Host(lambda: build(future))
    async with host.run_test() as pilot:
        host.picker.focus()
        await pilot.pause()
        host.picker.blur()
        await pilot.pause()

        assert future.done(), "the blur left the question unresolved"
        assert future.result() is None


@pytest.mark.parametrize("build", [model_picker, conversation_picker])
async def test_a_decided_picker_ignores_the_blur_that_follows(build) -> None:
    """The future is set once; the blur that follows a decision is not a second one.

    The app removes a decided picker, and removing the focused widget delivers
    a blur.  Reading that as a second decision would call `set_result` on a
    resolved future, which raises — out of the message pump, as a crash on the
    ordinary path through the picker.
    """
    future: asyncio.Future = asyncio.Future()
    host = Host(lambda: build(future))
    async with host.run_test() as pilot:
        host.picker.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert future.done(), "Enter chose nothing"
        answer = future.result()
        assert answer is not None

        # What the removal's blur delivers, before the widget is removed.
        host.picker.on_blur()
        await pilot.pause()

        assert future.result() is answer, "the blur decided a second time"
