"""The approval row, and the handshake that answers it.

`_approve` is the one meta-parameter that stops a turn: the loop parks on a
future, the window draws a row, and a person presses a key.  So there are two
things to pin — that the row resolves that future **once** and in the right
direction for each key, and that the window sends the answer back on the call
that exists for it (`answer_approval`) rather than dropping it.

v1's tests, ported: `y` approves, `n` and `Esc` deny, and a second keypress is
ignored.

**The fourth one is no longer v1's.** v1 denied on losing focus, on the argument
that the loop is waiting and a row nobody answers holds the turn open — and that
argument is wrong here, because three things take focus away on purpose: every
printable key other than `y`/`n` focuses the row (`redirect_printable`), `Ctrl+S`
opens the model picker, and a click anywhere does it.  So a person starting their
next thought refused the call the model was parked on, silently, and the row's
own docstring could not be told from a deliberate `n`.  What is pinned now is the
opposite: a blur leaves the row up and the answer owed, and only a decision
resolves the future.
"""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeAgentClient
from textual.app import App

from slife2.events import ToolApprovalRequested
from slife2.tui.app import SlifeApp
from slife2.tui.widgets import ApprovalPrompt, HistoryInput

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class Harness(App):
    """A window whose whole content is the row under test.

    **The row is built in `compose`, not handed in**, and that is not style: a
    `Static` measures itself against the app's console when it is given content,
    so constructing one outside a running app raises `NoActiveAppError`.
    `ModelPicker` never meets that because its caller is a key action, which runs
    inside the app; a test is the one caller that would build one first.
    """

    def __init__(
        self, name: str, arguments: dict, future: asyncio.Future[bool]
    ) -> None:
        super().__init__()
        self._name = name
        self._arguments = arguments
        self._future = future

    def compose(self):
        yield ApprovalPrompt(self._name, self._arguments, self._future)

    @property
    def prompt(self) -> ApprovalPrompt:
        return self.query_one(ApprovalPrompt)


def make_row(
    name: str = "sensitive", arguments: dict | None = None
) -> tuple[Harness, asyncio.Future[bool]]:
    future: asyncio.Future[bool] = asyncio.Future()
    return Harness(name, arguments or {"path": "/tmp/notes.md"}, future), future


def make_app(respond) -> tuple[SlifeApp, FakeAgentClient]:
    """A window with a scripted client, as `tests/test_tui.py` builds one."""
    client = FakeAgentClient(respond)
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        model_label="deepseek/deepseek-flash",
        agent="jack",
    )
    return app, client


def asking(call_id: str = "call-1", name: str = "sensitive"):
    """A turn that drops one approval request and then answers."""

    def respond(_prompt: str, on_event) -> str:
        on_event(ToolApprovalRequested(call_id, name, {"path": "/tmp/notes.md"}))
        return "done"

    return respond


async def submit(pilot, text: str) -> None:
    """Type a prompt and run it, as `tests/test_tui.py` does."""
    pilot.app.query_one(HistoryInput).text = text
    await pilot.press("enter")
    await pilot.pause()


# ── the row ───────────────────────────────────────────────────────────────────


async def test_y_approves_and_a_second_key_is_ignored() -> None:
    """The future is resolved once: a resolved future raises rather than shrugs."""
    harness, future = make_row()
    async with harness.run_test() as pilot:
        harness.prompt.focus()
        await pilot.press("y")
        await pilot.pause()
        assert future.result() is True
        await pilot.press("n")
        await pilot.pause()
        assert future.result() is True, "the first answer is the answer"


async def test_escape_denies() -> None:
    """`Esc` is a denial, not a cancel: the row is what has the focus."""
    harness, future = make_row()
    async with harness.run_test() as pilot:
        harness.prompt.focus()
        await pilot.press("escape")
        await pilot.pause()
        assert future.result() is False


async def test_losing_focus_without_a_decision_leaves_the_row_waiting() -> None:
    """A blur is not an answer, and reading it as one refused calls by accident.

    The row exists because the loop is waiting on this future — which is the
    argument for treating a blur as "no" — but "nobody is looking at it right
    now" and "no" are not close, and the paths that blur it are the ordinary ones
    (any printable keystroke, `Ctrl+S`, a click).  So the answer stays owed.
    """
    harness, future = make_row()
    async with harness.run_test() as pilot:
        harness.prompt.focus()
        harness.prompt.blur()
        await pilot.pause()
        assert harness.prompt.pending is True
        assert not future.done(), "the answer is still owed, not given"


async def test_the_decided_row_reads_as_its_decision() -> None:
    """The panel for the call is mounted after an approval and *never* for a
    refusal, so this line is the only place a refused call is ever seen.
    """
    harness, _ = make_row()
    async with harness.run_test() as pilot:
        harness.prompt.focus()
        assert harness.prompt.pending is True
        assert "requests approval" in harness.prompt.plain_text()
        await pilot.press("n")
        await pilot.pause()
        shown = harness.prompt.plain_text()
        assert harness.prompt.pending is False
        assert "denied" in shown
        assert "sensitive" in shown


async def test_the_row_shows_what_it_is_asking_about() -> None:
    """The question names the tool and what it would be called with."""
    harness, _ = make_row("skill_set", {"path": "/tmp/skill.md", "body": "x" * 400})
    async with harness.run_test():
        harness.prompt.focus()
        shown = harness.prompt.plain_text()
        assert "skill_set" in shown
        assert "path = /tmp/skill.md" in shown
        assert "…" in shown, "a long argument is cut rather than wrapping the row"


# ── the handshake ─────────────────────────────────────────────────────────────


async def test_a_keypress_reaches_the_server_as_an_answer() -> None:
    """The window draws the request and answers it with `answer_approval`.

    One call of its own, because the turn's progress stream runs one way — the
    request arrives on it and the answer cannot.
    """
    app, client = make_app(asking())
    async with app.run_test() as pilot:
        await submit(pilot, "do it")
        await pilot.pause()
        assert app._approval is not None, "the row was drawn"
        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()

        assert app._approval is None

    assert client.answers == [("call-1", True)]


async def test_a_cancelled_turn_denies_the_prompt_it_was_holding() -> None:
    """`Esc` refuses the call and then stops the turn — v1's order, and its
    reason: a row left undecided is an answer nobody will ever give.
    """
    app, client = make_app(asking())
    async with app.run_test() as pilot:
        await submit(pilot, "do it")
        await pilot.pause()
        assert app._approval is not None
        app.action_cancel()
        await pilot.pause()
        await pilot.pause()

        assert app._approval is None

    assert client.answers == [("call-1", False)]
