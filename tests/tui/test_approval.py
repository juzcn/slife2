"""The approval row, and the handshake that answers it.

`_approve` is the one meta-parameter that stops a turn: the loop parks on a
future, the window draws a row, and a person presses a key.  So there are two
things to pin — that the row resolves that future **once** and in the right
direction for each key, and that the window sends the answer back on the call
that exists for it (`answer_approval`) rather than dropping it.

The tests pin: `y` approves, `n` and `Esc` deny, and a second keypress is
ignored.

**The fourth rule is the opposite.** Denying on losing focus would follow the
argument that the loop is waiting and a row nobody answers holds the turn open —
and that argument is wrong here, because three things take focus away on
purpose: every printable key other than `y`/`n` focuses the row
(`redirect_printable`), `Ctrl+S` opens the model picker, and a click anywhere
does it.  So a person starting their
next thought refused the call the model was parked on, silently, and the row's
own docstring could not be told from a deliberate `n`.  What is pinned now is the
opposite: a blur leaves the row up and the answer owed, and only a decision
resolves the future.
"""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeAgentClient, FakeWatchClient
from textual.app import App

from slife2.libs.turn.events import ToolApprovalRequested
from slife2.tui.app import SlifeApp
from slife2.tui.watch import WatchScreen
from slife2.tui.widgets import (
    WINDOW_CHANNEL,
    ApprovalPrompt,
    ConversationPicker,
    HistoryInput,
)

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


def make_app(respond, watch=None) -> tuple[SlifeApp, FakeAgentClient]:
    """A window with a scripted client, as `tests/tui/test_tui.py` builds one."""
    client = FakeAgentClient(respond)
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        watch_client_factory=(lambda _name: watch) if watch is not None else None,
        model_label="deepseek/deepseek-flash",
        agent="jack",
    )
    return app, client


async def wait_for(pilot, predicate, what: str) -> None:
    """Wait for the app to have done something, or fail saying what never was."""
    for _ in range(500):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError(what)


async def open_a_watch(pilot, app, client: FakeAgentClient) -> None:
    """Push the watch screen the way `Ctrl+O` does: the list, then the choice."""
    client.subagents_answer = [{"name": "scout"}]
    await pilot.press("ctrl+o")
    await wait_for(
        pilot,
        lambda: len(app.query(ConversationPicker)) == 1,
        "the subagent picker never opened",
    )
    await pilot.press("enter")
    await wait_for(
        pilot, lambda: isinstance(app.screen, WatchScreen), "the watch never opened"
    )


def asking(call_id: str = "call-1", name: str = "sensitive"):
    """A turn that drops one approval request and then answers."""

    def respond(_prompt: str, on_event) -> str:
        on_event(ToolApprovalRequested(call_id, name, {"path": "/tmp/notes.md"}))
        return "done"

    return respond


async def submit(pilot, text: str) -> None:
    """Type a prompt and run it, as `tests/tui/test_tui.py` does."""
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


async def test_the_answers_survive_the_focus_moving_to_the_prompt() -> None:
    """**A keystroke is what takes the focus away, so the keys must outlast it.**

    Every printable key moves the focus to the prompt (`redirect_printable`),
    and a row-scoped binding is then out of the chain: a person who began their
    next thought and then pressed `y` typed a `y` instead of approving.  The
    app carries the same two answers while a question is pending, so they work
    from wherever the focus has gone; with no question up they are disabled and
    the draft gets both letters.
    """
    app, client = make_app(asking())
    async with app.run_test() as pilot:
        await submit(pilot, "do it")
        await pilot.pause()
        assert app._approval is not None

        await pilot.press("a")  # the draft takes the focus
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()

        assert app._approval is None

    assert client.answers == [("call-1", True)]
    assert client.prompts == ["do it"], "and the answer went to the waiting call"

    # With nothing being asked, `y` and `n` are ordinary letters again.
    app, client = make_app(lambda prompt, on_event: "done")
    async with app.run_test() as pilot:
        await submit(pilot, "do it")
        await pilot.pause()
        prompt = app.query_one(HistoryInput)
        prompt.text = ""
        await pilot.press("y")
        await pilot.press("n")
        await pilot.pause()
        assert prompt.text == "yn"
        assert client.answers == []


async def test_escape_over_the_prompt_refuses_the_call_and_not_the_turn() -> None:
    """Esc beside a question is the answer to it, wherever the focus is.

    `Esc` is the turn interrupt everywhere else (and stays one), but while the
    model is parked on an approval it means the smaller thing: a person who
    types a letter, moves to the draft and presses Esc must not lose the turn
    around the one call they meant to refuse.
    """
    app, client = make_app(asking())
    async with app.run_test() as pilot:
        await submit(pilot, "do it")
        await pilot.pause()
        assert app._approval is not None
        await pilot.press("a")
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()

        assert app._approval is None
        assert app._interrupting is False, "the turn was not the thing Esc stopped"

    assert client.answers == [("call-1", False)]


async def test_an_approval_flushed_after_its_turn_failed_does_not_mount_a_prompt() -> (
    None
):
    """**The cancel/failure flush applies what was posted before the turn died.**

    A request the turn made just before it ended can be sitting in that queue —
    its waiting future was dropped with the turn — so mounting the row would put
    up a question nobody can answer.  The event is honoured only while a turn is
    still there to be waiting on it.
    """

    def asks_then_fails(_prompt: str, on_event) -> str:
        on_event(ToolApprovalRequested("late", "sensitive", {"path": "/tmp/x"}))
        raise RuntimeError("the model call failed")

    app, _ = make_app(asks_then_fails)
    async with app.run_test() as pilot:
        await submit(pilot, "do it")
        await pilot.pause()
        await pilot.pause()

        assert app._approval is None, "a prompt nobody could answer was mounted"
        assert not app.query(ApprovalPrompt)


async def test_an_approval_over_a_watch_screen_is_drawn_where_it_can_be_seen() -> None:
    """**The row goes on the screen in front, not the hidden window.**

    `_ask_approval` mounted the prompt in the window's own transcript.  With a
    watch screen up that transcript is behind it, so the row and its arguments
    were invisible and the turn stayed parked until the watch closed or somebody
    pressed the app-level `y`/`n` blind.  `_active_transcript` is the same answer
    the scroll keys use: the screen that is showing is the one that is read.
    """
    watch = FakeWatchClient(())
    app, client = make_app(asking(), watch=watch)
    async with app.run_test() as pilot:
        await pilot.pause()
        await open_a_watch(pilot, app, client)
        await asyncio.wait_for(watch.watching.wait(), timeout=5)
        assert isinstance(app.screen, WatchScreen)

        # The prompt is submitted from the window's turn, which is running
        # underneath the watch — the ordinary way an approval arrives while
        # somebody is looking at a subagent.
        app._submit("do it", WINDOW_CHANNEL)
        await wait_for(
            pilot, lambda: app._approval is not None, "the approval never arrived"
        )

        assert list(app.screen.query(ApprovalPrompt)), (
            "the row was mounted on the hidden transcript"
        )
        assert app._approval.pending is True

        await pilot.press("y")
        await wait_for(pilot, lambda: app._approval is None, "the answer never left")

    assert client.answers == [("call-1", True)]


async def test_escape_on_a_watch_denies_the_approval_it_shows() -> None:
    """Esc beside a question is the answer to it, on the watch screen too.

    The screen's escape binding is priority and so is the row's, and the
    priority pass runs app → screen → focused widget — so the key reaches the
    screen's close action first.  That action denies the pending row instead
    of closing over it: the watch stays up, the answer leaves, and the turn is
    not the thing Esc stopped.  A second Esc, with nothing pending, closes the
    screen.
    """
    watch = FakeWatchClient(())
    app, client = make_app(asking(), watch=watch)
    async with app.run_test() as pilot:
        await pilot.pause()
        await open_a_watch(pilot, app, client)
        await asyncio.wait_for(watch.watching.wait(), timeout=5)

        app._submit("do it", WINDOW_CHANNEL)
        await wait_for(
            pilot, lambda: app._approval is not None, "the approval never arrived"
        )
        assert list(app.screen.query(ApprovalPrompt))

        await pilot.press("escape")
        await wait_for(pilot, lambda: app._approval is None, "the answer never left")
        assert isinstance(app.screen, WatchScreen), (
            "Esc closed the watch instead of denying the row"
        )
        assert app._interrupting is False, "the turn was not the thing Esc stopped"

        # With nothing pending, the same key closes the screen.
        await pilot.press("escape")
        await wait_for(
            pilot,
            lambda: not isinstance(app.screen, WatchScreen),
            "the watch stayed after the answer",
        )

    assert client.answers == [("call-1", False)]


async def test_a_pending_approval_comes_back_when_the_watch_closes() -> None:
    """**The row is a view of the question, and a second screen is a second view.**

    A prompt mounted on the watch screen is unmounted with it.  The turn is
    still parked on the same future, so a row that vanished with the screen
    leaves the person with nothing to answer; the dismissal re-mounts it on the
    window's transcript, and the app-level answer still answers it.
    """
    watch = FakeWatchClient(())
    app, client = make_app(asking(), watch=watch)
    async with app.run_test() as pilot:
        await pilot.pause()
        await open_a_watch(pilot, app, client)
        await asyncio.wait_for(watch.watching.wait(), timeout=5)

        app._submit("do it", WINDOW_CHANNEL)
        await wait_for(
            pilot, lambda: app._approval is not None, "the approval never arrived"
        )
        assert list(app.screen.query(ApprovalPrompt))

        # The screen goes away without a key — the route the dismissal callback
        # is registered for (`WatchScreen.action_close_watch` dismisses; the
        # callback and the pop are what a close does).  The question is still
        # pending on the same future.
        assert isinstance(app.screen, WatchScreen)
        app.screen.dismiss(None)
        await wait_for(
            pilot, lambda: not isinstance(app.screen, WatchScreen), "the watch stayed"
        )
        await wait_for(
            pilot,
            lambda: bool(list(app.screen.query(ApprovalPrompt))),
            "the pending row was not re-mounted on the window",
        )
        assert app._approval is not None and app._approval.pending

        await pilot.press("y")
        await wait_for(pilot, lambda: app._approval is None, "the answer never left")

    assert client.answers == [("call-1", True)]


async def test_ctrl_o_does_not_open_a_watch_over_a_pending_approval() -> None:
    """**A watch opened over the row hides it**, which is the same defect again.

    The key is one key away after the answer, so the answer comes first; the
    guard is the one `Ctrl+O` already applies to a watch in front.
    """
    app, client = make_app(asking())
    client.subagents_answer = [{"name": "scout"}]
    async with app.run_test() as pilot:
        await submit(pilot, "do it")
        await pilot.pause()
        assert app._approval is not None

        await pilot.press("ctrl+o")
        await pilot.pause()
        assert not app.query(ConversationPicker)
        assert len(app.screen_stack) == 1, "a watch was pushed over the question"
        assert app._approval is not None and app._approval.pending, (
            "the question was answered by the key"
        )


async def test_a_cancelled_turn_denies_the_prompt_it_was_holding() -> None:
    """`Esc` refuses the call and then stops the turn — the order follows the
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
