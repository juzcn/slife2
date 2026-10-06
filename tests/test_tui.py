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

from slife2.events import (
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)
from slife2.messages import Usage
from slife2.tui.app import SlifeApp
from slife2.tui.theme import GLYPHS, PALETTE
from slife2.tui.widgets import (
    AssistantMessage,
    ChatView,
    HistoryInput,
    StatusBar,
    ToolCallWidget,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

SIZE = (100, 30)


def make_app(respond, *, connect_error: Exception | None = None) -> SlifeApp:
    client = FakeAgentClient(respond, connect_error=connect_error)
    return SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        model_label="deepseek/deepseek-flash",
        agent="jack",
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


def tool_turn(*, ok: bool = True, result: str = "42"):
    """A scripted client that runs one tool and then answers."""

    def respond(prompt: str, on_event):
        on_event(ThinkingDelta("I should call the tool"))
        on_event(TextDelta("checking"))
        on_event(ToolCallStarted(call_id="c1", name="calc", arguments={"e": "6*7"}))
        on_event(
            ToolCallFinished(
                call_id="c1",
                name="calc",
                ok=ok,
                result_preview="42" if ok else "unknown tool",
                result_chars=len(result),
                elapsed_ms=3,
            )
        )
        on_event(TextDelta("it is 42"))
        return "checkingit is 42"

    return respond


def shown(app: SlifeApp) -> str:
    return app.query_one(ChatView).plain_text()


def status(app: SlifeApp) -> str:
    return str(app.query_one(StatusBar).content)


async def submit(pilot, text: str) -> None:
    pilot.app.query_one(HistoryInput).text = text
    await pilot.press("enter")
    await pilot.pause()


# --- the transcript ----------------------------------------------------------


async def test_a_submitted_prompt_is_streamed_into_one_block() -> None:
    """Deltas accumulate in the trailing block rather than one per token."""
    app = make_app(answering("hello"))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        text = shown(app)
        assert "hello" in text
        # One user bubble plus one assistant block: two widgets, not six.
        assert (
            len(
                list(app.query_one(ChatView).query(".user-message, .assistant-message"))
            )
            == 2
        )


async def test_the_user_message_carries_a_timestamp_and_prefix() -> None:
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hello")
        assert "You> hello" in shown(app)


async def test_both_sides_of_the_conversation_are_signed() -> None:
    """`You> ...` and `jack> ...`, so neither side is the unmarked one."""
    app = make_app(answering("hello"))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        text = shown(app)

    assert "You> hi" in text
    assert "jack> hello" in text


async def test_the_answer_carries_its_token_count() -> None:
    app = make_app(answering("hello", tokens=830))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        assert "830 tokens" in shown(app)


def reasoning_turn():
    """A scripted client that thinks before it answers."""

    def respond(prompt: str, on_event):
        on_event(ThinkingDelta("let me think about this"))
        on_event(TextDelta("the answer"))
        on_event(
            TurnFinished(text="the answer", usage=Usage(), steps=1, stop_reason="stop")
        )
        return "the answer"

    return respond


async def test_reasoning_on_the_answer_is_shown() -> None:
    """The final answer's reasoning is shown, not folded away.

    It is the interesting case — it is how you tell whether the model understood
    the question — so it is the one that stays open.  Collapsing is the
    *intermediate* step's treatment, not the default.
    """
    app = make_app(reasoning_turn())
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        widget = app.query_one(AssistantMessage)
        text = shown(app)

    assert widget.thinking == "let me think about this"
    assert widget.thinking_expanded is True
    assert "Thinking" in text
    assert "let me think about this" in text
    assert "the answer" in text


async def test_reasoning_can_be_folded_away() -> None:
    app = make_app(reasoning_turn())
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        widget = app.query_one(AssistantMessage)
        widget.action_toggle_thinking()
        await pilot.pause()

        assert widget.thinking_expanded is False
        text = shown(app)
        assert "let me think about this" not in text
        assert f"({len('let me think about this')} chars)" in text
        # ...and the answer is untouched by folding the reasoning.
        assert "the answer" in text


async def test_an_intermediate_step_folds_its_reasoning() -> None:
    """A step that ends in a tool call is machinery, and reads better as a line.

    The distinction is only knowable here: a tool call following a block is what
    makes that block intermediate, so it is the tool call that folds it.
    """
    app = make_app(tool_turn())
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "6*7?")
        blocks = list(app.query(AssistantMessage))

    assert len(blocks) == 2
    assert blocks[0].thinking_expanded is False, "the pre-tool step should fold"
    assert blocks[1].thinking_expanded is True, "the answer should not"


async def test_reasoning_is_never_mixed_into_the_answer() -> None:
    """The two are displayed differently, so they must arrive separately.

    Asserted on the fields rather than on the rendered transcript: reasoning is
    *visible* now, so its presence in the output proves nothing.  What matters
    is that it is the widget's reasoning and not part of its answer — an adapter
    that let it through as text would put the model's private working into the
    middle of its reply, which is hard to notice and impossible to undo.
    """
    app = make_app(reasoning_turn())
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        widget = app.query_one(AssistantMessage)

    assert widget.text == "the answer"
    assert widget.thinking == "let me think about this"
    assert "let me think" not in widget.text


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


async def test_model_output_is_not_parsed_as_markup() -> None:
    """`[bold]` from a model is text, not an instruction to the renderer.

    Rich's markup would raise on a stray `[`, and model output is full of them,
    so the distinction is kept by construction: every string that came from a
    model goes through `Text(...)`, which does not interpret tags.
    """
    hostile = "use [bold red]this[/] and [unclosed"
    app = make_app(answering(hostile))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        assert hostile in shown(app)


async def test_the_prompt_is_addressed_to_this_agent() -> None:
    """The placeholder names the agent, not the program.

    It is the only place the name is read before anything has been said, which
    is exactly why a hard-coded one is easy to miss: it looks right for the
    default and is wrong for every other name.
    """
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.query_one(HistoryInput).placeholder == "Message jack…"


async def test_enter_submits_and_clears_the_box() -> None:
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hello")
        assert app.query_one(HistoryInput).text == ""
        assert "You> hello" in shown(app)


async def test_shift_enter_inserts_a_newline() -> None:
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        prompt = app.query_one(HistoryInput)
        prompt.text = "line one"
        prompt.move_cursor(prompt.document.end)
        await pilot.press("shift+enter")
        await pilot.pause()
        assert "\n" in app.query_one(HistoryInput).text


async def test_empty_input_is_not_submitted() -> None:
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "   ")
        assert "You>" not in shown(app)


async def test_up_walks_back_through_previous_prompts() -> None:
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "first")
        await submit(pilot, "second")
        prompt = app.query_one(HistoryInput)
        assert prompt.text == ""

        await pilot.press("up")
        await pilot.pause()
        assert prompt.text == "second"
        await pilot.press("up")
        await pilot.pause()
        assert prompt.text == "first"
        await pilot.press("down")
        await pilot.pause()
        assert prompt.text == "second"


# --- tool calls --------------------------------------------------------------


async def test_a_tool_call_is_a_panel_showing_what_it_ran() -> None:
    """The header says `calc: 6*7`, not just `calc`.

    Which tool ran is the least interesting part; what it was asked is the part
    worth a row.
    """
    app = make_app(tool_turn())
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "6*7?")
        panel = app.query_one(ToolCallWidget)
        text = panel.plain_text()

    assert "Calc" in text
    assert "6*7" in text
    assert "done" in text


async def test_a_tool_panel_starts_collapsed() -> None:
    app = make_app(tool_turn())
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "6*7?")
        panel = app.query_one(ToolCallWidget)
        assert panel.collapsed is True
        assert "Arguments" not in panel.plain_text()


async def test_toggling_a_tool_panel_expands_it() -> None:
    app = make_app(tool_turn())
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "6*7?")
        panel = app.query_one(ToolCallWidget)
        panel.action_toggle_detail()
        await pilot.pause()

        text = panel.plain_text()
        assert panel.collapsed is False
        assert "Arguments" in text
        assert "Result" in text
        assert "e = 6*7" in text
        assert "42" in text


async def test_a_failed_tool_says_error() -> None:
    app = make_app(tool_turn(ok=False))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "weather?")
        panel = app.query_one(ToolCallWidget)
        row = panel.plain_text()
        panel.action_toggle_detail()
        expanded = panel.plain_text()

    assert "error" in row
    assert "Error" in expanded


async def test_a_running_tool_says_running() -> None:
    """Before the result lands the row has to say something is happening."""

    def respond(prompt: str, on_event):
        on_event(ToolCallStarted(call_id="c1", name="calc", arguments={"e": "1"}))
        return ""

    app = make_app(respond)
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "go")
        assert "running" in app.query_one(ToolCallWidget).plain_text()


async def test_text_after_a_tool_panel_opens_a_new_block() -> None:
    """A tool panel ends the text block, not the turn — text resumes below it."""
    app = make_app(tool_turn())
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "6*7?")
        assert "it is 42" in shown(app)
        blocks = app.query_one(ChatView).query(".assistant-message")
        assert len(list(blocks)) == 2, "text after a tool panel is its own block"


# --- the palette -------------------------------------------------------------


async def test_the_stylesheet_reads_the_palette() -> None:
    """Colours live in one place: the app publishes them, the CSS reads them."""
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE):
        variables = app.get_css_variables()

    assert variables["slife-bg"] == PALETTE["bg"]
    assert variables["slife-amber"] == PALETTE["amber"]
    # And the app did not lose Textual's own variables doing it.
    assert "$primary" in str(variables) or "primary" in variables


async def test_the_screen_is_painted_with_the_palette() -> None:
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        # Textual normalises a hex colour to a Color; compare the channels.
        assert app.screen.styles.background.hex.lower() == PALETTE["bg"]


# --- connection states -------------------------------------------------------


async def test_a_dead_server_does_not_kill_the_app() -> None:
    """Recoverable, not fatal: the status bar says why and the app stays up."""
    app = make_app(
        answering("ok"), connect_error=ConnectionError("http://test/mcp: refused")
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert "disconnected" in status(app)
        await submit(pilot, "hi")
        assert "not connected" in shown(app)


async def test_a_failing_turn_is_reported() -> None:
    def respond(prompt: str, on_event):
        raise RuntimeError("the model exploded")

    app = make_app(respond)
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        assert "the model exploded" in shown(app)
        assert GLYPHS["failed"] in shown(app)


# --- status and cancellation -------------------------------------------------


async def test_status_bar_shows_the_agent_the_model_and_the_context() -> None:
    app = make_app(answering("hello", tokens=42))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        bar = status(app)

    assert "jack" in bar
    assert "deepseek/deepseek-flash" in bar
    # No window configured for this model, so a count and no percentage.
    assert "42" in bar
    assert "%" not in bar


async def test_the_context_is_shown_as_a_percentage_of_the_window() -> None:
    """When the config says how big the model's window is, show how full it is."""
    client = FakeAgentClient(answering("hello", tokens=25_000))
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        model="deepseek/deepseek-flash",
        context_window=100_000,
    )
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        assert "25,000 (25.0%)" in status(app)


async def test_the_context_is_the_latest_turn_not_a_running_total() -> None:
    """A sum over every turn only ever grows, so it cannot be a context.

    What the percentage is a percentage *of* is the conversation as it stands,
    which is the last turn's count and not the total of all of them.
    """
    client = FakeAgentClient(answering("hello", tokens=30))
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        model="m",
        context_window=100,
    )
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "one")
        await submit(pilot, "two")
        # 30 twice would be 60%, which is what accumulating would show.
        assert "(30.0%)" in status(app)


async def test_an_at_path_is_read_and_sent_with_the_prompt(tmp_path) -> None:
    """The client gets a `data:` URL, and the transcript keeps the marker.

    Keeping it matters: the record should show what was sent, and removing the
    marker would leave a sentence with a hole where the attachment was named.
    """
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"pretend png bytes")
    client = FakeAgentClient(answering("I see it"))
    app = SlifeApp("http://test/mcp", client_factory=lambda: client, agent="jack")

    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, f"what is this? @{shot.as_posix()}")
        assert f"@{shot.as_posix()}" in shown(app)

    assert len(client.images) == 1
    assert client.images[0][0].startswith("data:image/png;base64,")


async def test_a_missing_attachment_is_reported_and_the_prompt_still_goes() -> None:
    """Losing what somebody typed because an attachment was wrong is worse.

    So the complaint is written to the transcript and the prompt is sent
    anyway — with no images, rather than not at all.
    """
    client = FakeAgentClient(answering("ok"))
    app = SlifeApp("http://test/mcp", client_factory=lambda: client, agent="jack")

    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "look at @nope.png")
        assert "nope.png" in shown(app)

    assert client.prompts == ["look at @nope.png"]
    assert client.images == [[]]


@pytest.mark.asyncio
async def test_ctrl_c_cancels_a_running_turn() -> None:
    """A terminal where you cannot stop a runaway turn is not usable."""
    started = asyncio.Event()

    async def never_finishes(prompt, on_event, *, images=None):
        # `images` is keyword-only on the real client, so a stand-in that
        # omitted it would fail on every call rather than never finishing —
        # which is a different test.
        started.set()
        await asyncio.sleep(30)
        return "never"

    app = make_app(answering("unused"))
    app._client_factory().run_turn = never_finishes  # type: ignore[method-assign]

    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        await asyncio.wait_for(started.wait(), timeout=2)
        worker = app._turn_worker
        assert worker is not None and worker.state is WorkerState.RUNNING

        await pilot.press("ctrl+c")
        await pilot.pause()

        assert worker.state is WorkerState.CANCELLED
        assert "[cancelled]" in shown(app)
        assert app.is_running


async def test_ctrl_c_quits_when_no_turn_is_running() -> None:
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app._turn_worker is None
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not app.is_running
