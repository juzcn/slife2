"""The TUI, driven headlessly by Textual's pilot.

`run_test()` never writes to the console, which is what makes these safe on the
Windows CI runner — the one place console encoding differs.  Nothing here
asserts on captured stdout for the same reason; assertions are on widget state,
taken *inside* the `async with` block, because the widgets are gone once it
exits.
"""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeAgentClient
from textual.worker import WorkerState

from slife2.events import TextDelta, ToolCallFinished, ToolCallStarted, TurnFinished
from slife2.messages import Usage
from slife2.tui.app import SlifeApp
from slife2.tui.widgets import PromptInput, StatusBar, Transcript

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

SIZE = (100, 30)


def make_app(respond, *, connect_error: Exception | None = None) -> SlifeApp:
    client = FakeAgentClient(respond, connect_error=connect_error)
    return SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        model_label="test/model",
    )


def answering(text: str, *, tokens: int = 0):
    """A scripted client that streams `text` one character at a time."""

    def respond(prompt: str, on_event):
        for char in text:
            on_event(TextDelta(char))
        on_event(
            TurnFinished(
                text=text,
                usage=Usage(completion_tokens=tokens),
                steps=1,
                stop_reason="stop",
            )
        )
        return text

    return respond


def shown(app: SlifeApp) -> str:
    return app.query_one(Transcript).plain_text()


def status(app: SlifeApp) -> str:
    return str(app.query_one(StatusBar).content)


async def submit(pilot, text: str) -> None:
    pilot.app.query_one(PromptInput).text = text
    await pilot.press("enter")
    await pilot.pause()


# --- the widgets -------------------------------------------------------------


async def test_a_submitted_prompt_is_streamed_into_one_block() -> None:
    """Deltas accumulate in the trailing block rather than one block per token."""
    app = make_app(answering("hello"))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        transcript = app.query_one(Transcript)
        text = transcript.plain_text()
        # One user bubble plus one assistant block: two widgets, not six.
        assert "you> hi" in text
        assert "hello" in text
        assert len(list(transcript.query("Static"))) == 2


async def test_final_answer_replaces_what_was_streamed() -> None:
    """The tool result is authoritative; a lossy stream is corrected, not kept."""

    def respond(prompt: str, on_event):
        on_event(TextDelta("garb"))
        return "the real answer"

    app = make_app(respond)
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        text = shown(app)

    assert "the real answer" in text
    assert "garb" not in text


async def test_enter_submits_and_clears_the_box() -> None:
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hello")
        assert app.query_one(PromptInput).text == ""
        assert "you> hello" in shown(app)


async def test_shift_enter_inserts_a_newline() -> None:
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.text = "line one"
        prompt.move_cursor(prompt.document.end)
        await pilot.press("shift+enter")
        await pilot.pause()
        assert "\n" in app.query_one(PromptInput).text


async def test_empty_input_is_not_submitted() -> None:
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "   ")
        assert "you>" not in shown(app)


# --- tool activity -----------------------------------------------------------


async def test_tool_activity_renders_inline() -> None:
    def respond(prompt: str, on_event):
        on_event(ToolCallStarted(call_id="c1", name="calc"))
        on_event(
            ToolCallFinished(
                call_id="c1",
                name="calc",
                ok=True,
                result_preview="42",
                result_chars=2,
                elapsed_ms=1,
            )
        )
        return "It is 42."

    app = make_app(respond)
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "6*7?")
        text = shown(app)

    assert "calc()" in text
    assert "It is 42." in text


async def test_text_after_a_tool_row_opens_a_new_block() -> None:
    """A tool row ends the text block, not the turn — text resumes below it."""

    def respond(prompt: str, on_event):
        on_event(TextDelta("checking"))
        on_event(ToolCallStarted(call_id="c1", name="calc"))
        on_event(
            ToolCallFinished(
                call_id="c1",
                name="calc",
                ok=True,
                result_preview="42",
                result_chars=2,
                elapsed_ms=1,
            )
        )
        on_event(TextDelta("it is 42"))
        return "checkingit is 42"

    app = make_app(respond)
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "6*7?")
        text = shown(app)

    assert text.count("checking") == 2  # streamed, then the corrected block
    assert "it is 42" in text


async def test_a_failed_tool_is_shown_as_failed() -> None:
    def respond(prompt: str, on_event):
        on_event(ToolCallStarted(call_id="c1", name="wether"))
        on_event(
            ToolCallFinished(
                call_id="c1",
                name="wether",
                ok=False,
                result_preview="unknown tool",
                result_chars=12,
                elapsed_ms=0,
            )
        )
        return "No such tool."

    app = make_app(respond)
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "weather?")
        text = shown(app)

    assert "!!" in text
    assert "unknown tool" in text


# --- connection states -------------------------------------------------------


async def test_a_dead_server_does_not_kill_the_app() -> None:
    """Recoverable, not fatal: the status bar says why and the app stays up."""
    app = make_app(
        answering("ok"), connect_error=ConnectionError("http://test/mcp: refused")
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert "disconnected" in status(app)
        # ...and the prompt still works, reporting the problem.
        await submit(pilot, "hi")
        assert "not connected" in shown(app)


async def test_a_failing_turn_is_reported_in_the_transcript() -> None:
    def respond(prompt: str, on_event):
        raise RuntimeError("the model exploded")

    app = make_app(respond)
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        assert "the model exploded" in shown(app)


# --- status and cancellation -------------------------------------------------


async def test_status_bar_runs_up_the_token_count() -> None:
    app = make_app(answering("hello", tokens=42))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        assert "42 tokens" in status(app)


async def test_status_bar_shows_the_model() -> None:
    app = make_app(answering("hello"))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert "test/model" in status(app)


async def test_ctrl_c_cancels_a_running_turn() -> None:
    """A terminal where you cannot stop a runaway turn is not usable."""
    started = asyncio.Event()

    async def never_finishes(prompt, on_event):
        started.set()
        await asyncio.sleep(30)
        return "never"

    app = make_app(answering("unused"))
    client = app._client_factory()
    client.run_turn = never_finishes  # type: ignore[method-assign]

    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        await asyncio.wait_for(started.wait(), timeout=2)
        worker = app._turn_worker
        assert worker is not None and worker.state is WorkerState.RUNNING

        await pilot.press("ctrl+c")
        await pilot.pause()

        assert worker.state is WorkerState.CANCELLED
        assert "[cancelled]" in shown(app)
        # And the app is still alive to be used.
        assert app.is_running


async def test_ctrl_c_quits_when_no_turn_is_running() -> None:
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app._turn_worker is None
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not app.is_running
