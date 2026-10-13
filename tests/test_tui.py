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
from fakes import (
    FakeAgentClient,
    FakeMeshClient,
    FakeWindowClient,
    RefusingMeshClient,
)

import slife2.timeouts as _T
from slife2.events import (
    ContextChosen,
    TextDelta,
    ThinkingDelta,
    ToolApprovalRequested,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)
from slife2.messages import Message, ToolCall, Usage
from slife2.toolclient import FUNC_TOOL_UNLOAD
from slife2.tui.app import SlifeApp, TurnEventMessage
from slife2.tui.client import MCPAgentClient
from slife2.tui.theme import GLYPHS, PALETTE
from slife2.tui.widgets import (
    AssistantMessage,
    ChatView,
    HistoryInput,
    ModelChoice,
    ModelPicker,
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


def answering(text: str, *, tokens: int = 0, context: int | None = None):
    """A scripted client that streams `text` one character at a time.

    `tokens` is what the turn cost and `context` is the size it left the
    conversation at.  They are the same number unless a test says otherwise,
    which is the truth for a turn that took one model call — and a test that
    cares about the difference is the only way to tell a display that reads the
    right one from a display that reads the other.
    """

    def respond(prompt: str, on_event):
        for char in text:
            on_event(TextDelta(char))
        on_event(
            TurnFinished(
                text=text,
                usage=Usage(completion_tokens=tokens),
                last_usage=Usage(
                    completion_tokens=tokens if context is None else context
                ),
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


def visible_lines(app: SlifeApp) -> list[str]:
    """The text the terminal is showing — the compositor's own output.

    Not `scroll_y`: the offset was always right, and every assertion that read
    it passed while the screen stood still.  What the reader sees is the
    compositor's strips, so a repaint is only testable here.
    """
    strips = app.screen._compositor.render_strips()
    return ["".join(segment.text for segment in strip) for strip in strips]


async def repainted(app: SlifeApp, pilot, previous: list[str], tries: int = 20):
    """The screen once it differs from `previous` — a repaint, waited for."""
    lines = visible_lines(app)
    for _ in range(tries):
        if lines != previous:
            break
        await pilot.pause()
        lines = visible_lines(app)
    return lines


async def settled(pilot, condition, *, tries: int = 20) -> bool:
    """`condition` once it holds — polled, because a scroll lands a turn late.

    `scroll_to` and `scroll_end` do not move the offset when they are called:
    both defer, and the deferred scroll runs once the widget is next idle — a
    turn *after* the `pilot.pause` written to cover it.  The read right after
    that pause therefore compared the old position with itself.
    """
    for _ in range(tries):
        if condition():
            return True
        await pilot.pause()
    return bool(condition())


def wheel_over(widget, *, up: bool):
    """A wheel event over *widget*, as the driver would deliver it."""
    from textual import events

    x, y = widget.region.x + 1, widget.region.y + 1
    cls = events.MouseScrollUp if up else events.MouseScrollDown
    return cls(
        widget=None,
        x=x,
        y=y,
        delta_x=0,
        delta_y=0,
        button=0,
        shift=False,
        meta=False,
        ctrl=False,
        screen_x=x,
        screen_y=y,
        style=None,
    )


async def settle(pilot, app: SlifeApp, *, tries: int = 200) -> None:
    """Let every queued turn finish, however many there are.

    A single `pause()` is not enough once the app owns a queue: each turn ends
    by handing control back to the drain, which starts the next one, and the
    whole chain has to be pumped.  Polling the flag rather than sleeping a fixed
    time keeps this from being a race that passes on a fast machine.
    """
    for _ in range(tries):
        await pilot.pause()
        if not app._draining:
            return
    raise AssertionError("the queue never drained")


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


async def test_a_turn_with_nothing_to_say_leaves_nothing_behind() -> None:
    """No empty block, and no bill hung on the previous turn's answer.

    `begin_assistant` promises a turn that says nothing leaves nothing behind,
    and the block is opened by the *answer* — so an empty one has to open
    nothing.  It used to open an `AssistantMessage` regardless, whose dim `…` is
    the widget's "still working" affordance: a turn that had finished, saying it
    was working, forever.

    The bill is the other half of the same turn.  `TurnFinished` arrives before
    the answer does, so the count was aimed at a block that did not exist and
    fell back to `_last_assistant` — the *previous* turn's — printing the silent
    turn's tokens under somebody else's words.
    """

    def respond(prompt: str, on_event):
        if prompt == "first":
            on_event(TextDelta("hello"))
            on_event(
                TurnFinished(
                    text="hello",
                    usage=Usage(completion_tokens=3),
                    last_usage=Usage(completion_tokens=3),
                    steps=1,
                    stop_reason="stop",
                )
            )
            return "hello"
        # A turn that hit the step cap having said nothing: no deltas, no text.
        on_event(
            TurnFinished(
                text="",
                usage=Usage(completion_tokens=99),
                last_usage=Usage(completion_tokens=99),
                steps=8,
                stop_reason="max_steps",
            )
        )
        return ""

    app = make_app(respond)
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "first")
        await settle(pilot, app)
        await submit(pilot, "second")
        await settle(pilot, app)
        text = shown(app)
        blocks = list(app.query(AssistantMessage))

    assert len(blocks) == 1, "the silent turn opened a block of its own"
    assert text.count("3 tokens") == 1, "the first turn still has its own count"
    assert "99 tokens" not in text, "the silent turn's bill landed somewhere"
    assert [row for row in text.splitlines() if row.strip() == "…"] == [], (
        "a finished turn left the working ellipsis behind"
    )


def reasoning_turn():
    """A scripted client that thinks before it answers."""

    def respond(prompt: str, on_event):
        on_event(ThinkingDelta("let me think about this"))
        on_event(TextDelta("the answer"))
        on_event(
            TurnFinished(
                text="the answer",
                usage=Usage(),
                last_usage=Usage(),
                steps=1,
                stop_reason="stop",
            )
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


async def test_a_block_with_only_reasoning_is_not_signed() -> None:
    """The signature signs an *answer*, so a reasoning-only step has none.

    A block is opened by whatever has something to put in it — the first delta,
    a resumed stream after a tool panel, the final answer — so a signature
    painted at construction left a bare `jack>` row in the transcript for every
    step that had only reasoning to show.  On a reasoning model that
    is one per tool call, and it reads as the prompt being printed over and over
    down the conversation.
    """

    def reasons_then_calls(prompt, on_event):
        on_event(ThinkingDelta("first, what is six times seven"))
        on_event(ToolCallStarted(call_id="c1", name="calc", arguments={"e": "6*7"}))
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
        on_event(ThinkingDelta("now say it"))
        on_event(TextDelta("42"))
        return "42"

    app = make_app(reasons_then_calls)
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "6*7?")
        rows = shown(app).splitlines()

    assert [row for row in rows if row.strip() == "jack>"] == [], (
        "a signature with nothing under it"
    )
    assert "jack> 42" in rows, "and the answer is still signed"


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


async def test_a_collapsed_tool_says_how_long_it_took() -> None:
    """The event has carried a duration from the beginning.

    Short key, codec, and two tests — and the loop filled it with a zero, so
    the header could not have shown one whatever it did with it.  A tool that
    hung is worth seeing without expanding anything: it is the one thing about
    a finished call that the result text does not say.
    """
    app = make_app(tool_turn())
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "6*7?")
        panel = app.query_one(ToolCallWidget)

        collapsed = panel.plain_text()

    assert panel.collapsed is True, "the header is the whole point of collapsing"
    assert "3ms" in collapsed, "the duration is on the row that is always visible"


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


async def test_tool_panels_do_not_pile_up_across_turns() -> None:
    """Every panel stays on screen; only the current turn's calls stay addressable.

    The map the transcript keeps is what a result finds its panel through, and it
    was never pruned: one widget handle per call, for the life of the window, so
    a day of agent work — thousands of calls, each with its own id — retained
    thousands of handles that nothing would ever look up again.  It is bounded by
    the turn now, which is also what settles what a reused id means: a call id is
    unique only within the turn that used it (`add_user` clears the map as each
    turn opens), so a later turn registering `c1` cannot complete the earlier
    turn's panel of the same name.

    Distinct ids per turn are the point of the count — one id reused five times
    overwrites itself and would read as bounded even while leaking.
    """

    def respond(prompt: str, on_event):
        call = f"call-{prompt}"
        on_event(ToolCallStarted(call_id=call, name="calc", arguments={"e": "1"}))
        on_event(
            ToolCallFinished(
                call_id=call,
                name="calc",
                ok=True,
                result_preview="1",
                result_chars=1,
                elapsed_ms=1,
            )
        )
        return "1"

    app = make_app(respond)
    async with app.run_test(size=SIZE) as pilot:
        for index in range(5):
            await submit(pilot, f"q{index}")
            await settle(pilot, app)
        chat = app.query_one(ChatView)
        panels = list(app.query(ToolCallWidget))

    assert len(panels) == 5, "the transcript is still the whole conversation"
    assert len(chat._tools) == 1, "but only the last turn's call is still addressable"


# --- scrolling the transcript -------------------------------------------------


#: An answer taller than the 30-row test screen, so the transcript really does
#: have somewhere to scroll to.
SCROLLABLE = "\n".join(f"line {n}" for n in range(60))


async def overflowing_transcript(pilot, app: SlifeApp) -> ChatView:
    """Submit a prompt whose answer overflows the screen, and return the view.

    Every test here starts from "the reader is at the tail", so the start is
    asserted rather than assumed: a `scroll_end` is deferred, and a test that
    began somewhere else would pass or fail for the wrong reason.
    """
    await submit(pilot, "hi")
    transcript = app.query_one(ChatView)
    await settle(pilot, app)
    assert await settled(pilot, lambda: transcript.max_scroll_y > 0), (
        "the answer has to overflow, or none of this section means anything"
    )
    assert await settled(
        pilot, lambda: transcript.scroll_y == transcript.max_scroll_y
    ), "and it has to come to rest at its tail"
    return transcript


async def test_page_up_and_page_down_move_the_transcript() -> None:
    """PageUp/PageDown page the transcript, not the three-row draft.

    `TextArea` claims both keys to page through its *own* text, and the prompt
    is what always has focus — so without a binding here to take them back, the
    transcript's only ways to move were the wheel and a scrollbar the stylesheet
    hides.  The keys were simply dead.
    """
    app = make_app(answering(SCROLLABLE))
    async with app.run_test(size=SIZE) as pilot:
        transcript = await overflowing_transcript(pilot, app)

        await pilot.press("pageup")
        assert await settled(
            pilot, lambda: transcript.scroll_y < transcript.max_scroll_y
        )
        paged_up = transcript.scroll_y

        await pilot.press("pagedown")
        assert await settled(pilot, lambda: transcript.scroll_y > paged_up)


async def test_home_and_end_jump_to_the_ends() -> None:
    """Home/End go to the transcript even though the prompt has focus.

    `priority=True` on the app's bindings is what does it: the priority pass
    runs *before* the focused widget, and the focused widget is the prompt,
    whose `TextArea` binds both keys to the ends of the line the cursor is on.
    What the draft keeps is the Emacs spelling of each, which is the bargain
    slife v1 struck.
    """
    app = make_app(answering(SCROLLABLE))
    async with app.run_test(size=SIZE) as pilot:
        transcript = await overflowing_transcript(pilot, app)

        await pilot.press("home")
        assert await settled(pilot, lambda: transcript.scroll_y == 0)
        reading = transcript._at_tail

        await pilot.press("end")
        assert await settled(
            pilot, lambda: transcript.scroll_y == transcript.max_scroll_y
        )
        following = transcript._at_tail

    assert reading is False, "scrolling up into history stopped the following"
    assert following is True, "and End resumed it"


async def test_a_scroll_repaints_what_is_on_screen() -> None:
    """A scroll must repaint, not just move the offset.

    `ChatView.watch_scroll_y` overrides Textual's watcher, and the override has
    to delegate: the base one is what repaints the widget at the new offset.
    Dropping the delegation moved the offset and left the screen exactly as it
    was — which is why this section reads the compositor rather than `scroll_y`,
    and why every assertion on the offset stayed green through the bug.
    """
    app = make_app(answering(SCROLLABLE))
    async with app.run_test(size=SIZE) as pilot:
        transcript = await overflowing_transcript(pilot, app)
        at_tail = visible_lines(app)

        transcript.scroll_to(y=0, animate=False)
        moved = await repainted(app, pilot, at_tail)

    assert moved != at_tail, "the offset moved and the screen stood still"
    assert moved[0] != at_tail[0], "the top row is a different row now"


async def test_the_wheel_over_the_prompt_moves_the_transcript() -> None:
    """A tick aimed at the bottom of the screen still moves the reading.

    Textual delivers a wheel tick to the widget under the pointer, and that
    widget is the prompt whenever the pointer sits low on the screen — where it
    lands after typing.  The draft has nothing to scroll and the base handler
    neither moved it nor stopped the tick, so the tick bubbled to a Screen with
    no scroll to give it and the transcript never moved.
    """
    app = make_app(answering(SCROLLABLE))
    async with app.run_test(size=SIZE) as pilot:
        transcript = await overflowing_transcript(pilot, app)
        prompt = app.query_one(HistoryInput)
        prompt.focus()
        await pilot.pause()

        before = transcript.scroll_y
        app.screen._forward_event(wheel_over(prompt, up=True))
        assert await settled(pilot, lambda: transcript.scroll_y < before)
        focused = app.focused

    assert isinstance(focused, HistoryInput), "the wheel must not move focus"


async def test_a_streaming_turn_does_not_drag_a_reader_back_to_the_tail() -> None:
    """The other half of the bug: tokens undid every page the reader turned.

    Every token and every mounted widget landed on an unconditional
    `scroll_end`, so the wheel and the keys both looked dead while the model was
    working and worked again once it was idle.  Following is sticky — it holds
    while the reader asks for the end, and stops the moment they ask for
    anything else.
    """
    arrived = asyncio.Event()
    release = asyncio.Event()

    async def streams_in_two(prompt, on_event, *, channel="tui", images=None):
        on_event(TextDelta(SCROLLABLE))
        arrived.set()
        await release.wait()
        on_event(TextDelta("\nand more"))
        return SCROLLABLE + "\nand more"

    app = make_app(answering("unused"))
    app._client_factory().run_turn = streams_in_two

    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        await asyncio.wait_for(
            arrived.wait(),
            timeout=2,  # timeouts: the value this test is built around, not a budget
        )  # timeouts: the value this test is built around, not a budget
        transcript = app.query_one(ChatView)
        assert await settled(pilot, lambda: transcript.max_scroll_y > 0)

        await pilot.press("pageup")
        assert await settled(
            pilot, lambda: transcript.scroll_y < transcript.max_scroll_y
        )
        held = transcript.scroll_y

        release.set()
        await settle(pilot, app)
        after = transcript.scroll_y

    assert after == held, "a streamed token dragged the reader back to the tail"


async def test_sending_a_message_returns_to_the_tail() -> None:
    """The reader's own send goes to the end, however far up they had read.

    Sticky following is what keeps a streaming turn from pulling the page out
    from under a reader in history; the other half of it is that the reader's
    own message is not held back with the stream.  Without the re-arm the
    message is written below the fold and the answer under that, so a reader who
    had scrolled up sees nothing at all happen.
    """
    app = make_app(answering(SCROLLABLE))
    async with app.run_test(size=SIZE) as pilot:
        transcript = await overflowing_transcript(pilot, app)

        await pilot.press("pageup")
        assert await settled(
            pilot, lambda: transcript.scroll_y < transcript.max_scroll_y
        )

        await submit(pilot, "another")
        await settle(pilot, app)
        assert await settled(
            pilot, lambda: transcript.scroll_y == transcript.max_scroll_y
        )


# --- the keyboard, once the transcript has it ---------------------------------


async def test_typing_after_focusing_a_message_still_reaches_the_prompt() -> None:
    """A click lands focus on a message; typing has to go on working.

    The transcript's widgets are focusable — that is what gives a message Enter
    and Space to unfold its reasoning, and a tool panel the same to open — so a
    click is enough to take the keyboard off the prompt, and the next letter
    typed would go nowhere.
    """
    app = make_app(answering("hello"))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        app.query_one(AssistantMessage).focus()
        await pilot.pause()

        await pilot.press("o", "k")
        await pilot.pause()
        typed = app.query_one(HistoryInput).text

    assert typed == "ok"


async def test_space_on_a_message_unfolds_its_reasoning() -> None:
    """Space is the message's, not the redirect's.

    It is printable, so a redirect that took every printable key would type a
    space into the prompt instead of running the binding the message was made
    focusable for.
    """
    app = make_app(reasoning_turn())
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        block = app.query_one(AssistantMessage)
        block.focus()
        await pilot.pause()

        await pilot.press("space")
        await pilot.pause()
        expanded = block.thinking_expanded
        typed = app.query_one(HistoryInput).text

    assert expanded is False, "space did not reach the message's own binding"
    assert typed == "", "and it did not fall through into the prompt"


# --- a window opened after a restart ------------------------------------------


def stored(
    *messages: Message,
    turn_id: int = 1,
    asked: str = "",
    context: int = 0,
    channel: str = "tui",
) -> dict:
    """One stored turn, as the server hands it back.

    Built from the real message model rather than by writing the dicts out by
    hand: the tool-call half of the shape (`arguments` as a JSON *string*,
    nested under `function`) is exactly the kind of thing a fixture that spells
    it itself gets subtly wrong and then proves itself right about.

    `context` is how large the conversation had become by the end of the turn —
    `TurnRecord.context_tokens`, which `to_wire` always sends and the bar reads.

    `channel` is where the turn came in on: this window's own (`tui`), or the key
    a plugin runs under for a turn somebody else's message started.
    """
    return {
        "turn_id": turn_id,
        "messages": [message.to_wire() for message in messages],
        "created_at": asked,
        "completed_at": None,
        "channel": channel,
        "what_model": "fake",
        "token_count": context,
        "context_tokens": context,
    }


def answering_with(*turns: dict, text: str = "ok"):
    """A client that has a previous conversation and answers plainly."""
    client = FakeAgentClient(answering(text))
    client.turns = list(turns)
    return client


def window(client: FakeAgentClient, **kwargs) -> SlifeApp:
    return SlifeApp(
        "http://test/mcp", client_factory=lambda: client, agent="jack", **kwargs
    )


CONVERSATION = [
    stored(
        Message(role="user", content="what is 6*7?"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="calc", arguments={"e": "6*7"})],
        ),
        Message(role="tool", content="42", tool_call_id="c1"),
        Message(role="assistant", content="it is 42"),
        turn_id=1,
    ),
    stored(
        Message(role="user", content="and times two?"),
        Message(role="assistant", content="84"),
        turn_id=2,
    ),
]


async def restored(pilot, app: SlifeApp) -> None:
    """Wait for the window's one read of the history to have landed."""
    assert await settled(pilot, lambda: app._restored), "the history was never read"


async def test_a_restarted_window_shows_the_conversation_it_left() -> None:
    """The whole point: nothing about the screen should say "restart".

    The server has restored the *context* the moment it builds a loop, so a
    window that drew nothing was the only part of the system acting as though
    the conversation were over — blank, while the model went on answering from a
    history the user could not see.
    """
    client = answering_with(*CONVERSATION)
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        text = shown(app)
        panels = list(app.query(ToolCallWidget))

    assert "You> what is 6*7?" in text
    assert "jack> it is 42" in text
    assert "You> and times two?" in text
    assert "jack> 84" in text
    # ...and the work between the question and the answer, which a rebuild that
    # only replayed the text would leave out.
    assert len(panels) == 1
    assert "Calc" in panels[0].plain_text()


async def test_a_name_that_has_never_run_shows_nothing() -> None:
    """No empty heading, and no "restored 0 turns": that is inventing an event."""
    app = window(answering_with())
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        assert shown(app) == ""


def context_of(kept: int, recalled: int):
    """A scripted client whose turn reports the discriminator's answer first."""

    def respond(prompt: str, on_event):
        on_event(ContextChosen(kept=kept, recalled=recalled))
        on_event(TextDelta("ok"))
        on_event(
            TurnFinished(
                text="ok",
                usage=Usage(),
                last_usage=Usage(),
                steps=1,
                stop_reason="stop",
            )
        )
        return "ok"

    return respond


async def test_the_discriminators_decision_is_reported_under_the_prompt() -> None:
    """One model call decides the context, and this is the only sight of it.

    The note is `add_note`'s, so it carries the class the restored history's line
    carries — the same shape and the same dim styling, which is the point: both
    say what the harness did, and a reader has one kind of line to learn.  It
    lands between the prompt and the answer because that is what it is about:
    the context the answer was written from, not a comment on how it went.
    """
    app = window(FakeAgentClient(context_of(12, 3)))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        blocks = [widget.classes for widget in app.query_one(ChatView).children]
        text = shown(app)

    assert "[kept 12 turns, recalled 3]" in text
    assert "user-message" in blocks[0]
    assert "system-message" in blocks[1], "the line the restore writes is this line"
    assert "assistant-message" in blocks[2]


async def test_the_context_note_is_spelled_the_way_the_restored_one_is() -> None:
    """`1 turn`, not `1 turns` — one phrase, written in one place.

    A reader who has learned to read `[restored 14 turns]` should not meet
    `1 turns` in the note that sits beside it.
    """
    app = window(FakeAgentClient(context_of(1, 0)))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        assert "[kept 1 turn, recalled 0]" in shown(app)


async def test_the_restored_history_says_how_much_came_back() -> None:
    app = window(answering_with(*CONVERSATION))
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        assert "[restored 2 turns]" in shown(app)


async def test_a_restored_conversation_shows_how_full_the_window_is() -> None:
    """The bar was the one part of the window that did not know it had a history.

    Measured on a restart: the transcript said `[restored 14 turns]` and the bar
    directly under it said `0 (0.0%)`, because `_context_tokens` is set from a
    live turn's `last_usage` and a window that has only restored has run no turn.
    `context_of` reads it from the last restored turn instead — the same
    quantity, since a turn records how large the conversation had become by the
    end of it, which is what a percentage of the window is a percentage of.

    The newest turn carries the larger number on purpose: a bar filled from the
    first restored turn, or from a sum over all of them, would read differently.
    """
    app = window(
        answering_with(
            stored(Message(role="user", content="a"), turn_id=1, context=1_000),
            stored(Message(role="user", content="b"), turn_id=2, context=65_514),
        ),
        model="m",
        context_window=100_000,
    )
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        assert "65,514 (65.5%)" in status(app)


async def test_a_restored_message_is_stamped_when_it_was_sent() -> None:
    """Not when the window opened.

    A conversation from March that says every line arrived this second is worse
    than one with no timestamps at all — it is the transcript making a claim
    about the record that the record contradicts.
    """
    client = answering_with(
        stored(Message(role="user", content="hello"), asked="2020-03-04T09:05:00+08:00")
    )
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        assert "[2020-03-04 09:05] You> hello" in shown(app)


async def test_a_restored_message_from_another_channel_wears_it() -> None:
    """A WeChat turn is not the person's own line, and it does not print its ids.

    The marker is for the model — it is how a reply learns which peer and which
    thread — and it is *stored*, so a rebuild reads it back out of the row.  On
    the screen both halves of that belong to the channel: the bubble says
    `Wechat> ` and the marker comes off, because the two ids in it are of no use
    to somebody reading over the model's shoulder.  v1's split, kept: the channel
    picks the prefix, and the widget unwraps the text.
    """
    client = answering_with(
        stored(
            Message(
                role="user",
                content='[Wechat:{"peer_wechat_id": "peer-1"}] 你好',
            ),
            asked="2020-03-04T09:05:00+08:00",
            channel="wechat",
        )
    )
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        assert "[2020-03-04 09:05] Wechat> 你好" in shown(app)
        assert "peer-1" not in shown(app), "the marker reached the screen"


async def test_the_persons_own_words_are_never_unwrapped() -> None:
    """Typing a marker-shaped message is not a way to lose the front of it.

    Unwrapping is for a turn that arrived over *another* channel.  This window's
    own turns are the person's words — a question about the marker, a paste, a
    line of a log — and they are shown as typed, which is the one place the guard
    can be checked from: through the window, on its own channel.
    """
    typed = '[Wechat:{"peer_wechat_id": "peer-1"}] hello'
    client = answering_with(stored(Message(role="user", content=typed)))
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        # Restored, on this window's channel: shown verbatim.
        await restored(pilot, app)
        assert typed in shown(app)
        # And typed live, which is the same claim through the other path.
        await submit(pilot, typed)
        assert f"You> {typed}" in shown(app)


async def test_a_row_with_no_channel_keeps_the_persons_words_whole() -> None:
    """An absent channel is this window's own, and the strip must agree.

    The bubble is decided by `channel_prefix`, which reads `""` as `You> ` —
    the only rows without a channel are rows this window wrote before there was
    a column for one (`DEFAULT ''`), and it is also what a caller that names no
    channel sends.  The marker-strip guard had its own test for the same
    question (`channel != "tui"`), so an absent channel *was* unwrapped: a
    person's own message that happened to start with a marker lost its front on
    the way back — the one outcome the guard beside this one exists to prevent.
    """
    typed = '[Wechat:{"peer_wechat_id": "peer-1"}] hello'
    client = answering_with(stored(Message(role="user", content=typed), channel=""))
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        assert f"You> {typed}" in shown(app)
        assert "peer-1" in shown(app), "their own words, marker and all"


async def test_a_restored_message_says_which_turn_it_opened() -> None:
    """The id the model addresses turns by, shown to the person watching.

    It is the *same* footnote the model reads inside `[TURN:{…}]` — one function,
    two readers — and showing it is what makes a keep-list legible from the
    outside: a model that says it is keeping turn 12 is naming a number its
    reader can point at, and a channel besides.  What a screen shows is the
    payload; `[TURN:` is the machine's marker and stays behind.
    """
    client = answering_with(
        stored(
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi"),
            turn_id=12,
            asked="2026-08-10T14:03:00+08:00",
        )
    )
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        text = shown(app)

    assert '{"turn_id": 12, "channel": "tui", "begin": "2026-08-10 14:03"}' in text
    assert "[TURN:" not in text, "the envelope is the model's, not the reader's"


async def test_every_restored_turn_says_which_one_it_was() -> None:
    """One footnote per turn, on the message that opened it."""
    client = answering_with(*CONVERSATION)
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        text = shown(app)

    assert text.count('"turn_id": 1') == 1
    assert text.count('"turn_id": 2') == 1


async def test_a_live_message_carries_no_turn_footnote() -> None:
    """A turn has no id until it is stored, and the bubble is drawn before that.

    v1's arrangement, kept: the footnote is metadata about a *stored* turn, so
    it appears in a rebuilt transcript and never beside something being typed.
    """
    app = make_app(answering("hi"))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hello")
        text = shown(app)

    assert "You> hello" in text
    assert "turn_id" not in text


async def test_a_restored_tool_panel_opens_and_shows_what_it_returned() -> None:
    client = answering_with(*CONVERSATION)
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        panel = app.query_one(ToolCallWidget)
        collapsed = panel.collapsed
        panel.action_toggle_detail()
        await pilot.pause()
        expanded = panel.plain_text()

    assert collapsed is True, "a panel is born collapsed, restored or not"
    assert "Result" in expanded
    assert "42" in expanded
    assert "e = 6*7" in expanded


async def test_a_restored_result_reads_like_a_live_one() -> None:
    """A preview and a true count, not the whole thing.

    The wire caps what a panel displays, so 100 KB of tool output is not
    duplicated into every widget and log line.  A rebuild that passed the stored
    text through unshaped would make the restored panel the one place a tool's
    entire output is rendered — and the one place a call looks different from
    the same call seen live.
    """
    long = "x" * 5000
    client = answering_with(
        stored(
            Message(role="user", content="read it"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c1", name="read", arguments={"path": "big"})],
            ),
            Message(role="tool", content=long, tool_call_id="c1"),
            Message(role="assistant", content="read"),
        )
    )
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        panel = app.query_one(ToolCallWidget)
        panel.action_toggle_detail()
        await pilot.pause()
        expanded = panel.plain_text()

    assert long not in expanded, "the whole result was rendered"
    assert f"{len(long):,} characters in all" in expanded


async def test_a_restored_step_that_only_called_a_tool_gets_no_empty_block() -> None:
    """The live transcript could not have shown one, so a rebuild must not.

    An assistant message with tool calls and no text exists in the record for
    the model's benefit.  Opening a block for it would put a stray `…` — or,
    worse, a bare signature — between the question and the work.
    """
    client = answering_with(*CONVERSATION)
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        rows = shown(app).splitlines()
        # One block per turn: the callee's "it is 42", and "84".  Not three.
        assert len(list(app.query(AssistantMessage))) == 2

    assert [row for row in rows if row.strip() in ("jack>", "…")] == []


async def test_a_restored_step_shows_the_reasoning_it_had() -> None:
    """The reasoning is part of what happened, and the record is where it lives.

    It is not on the API wire — a model is never sent its own earlier reasoning
    — so a store written as `to_wire` output loses it, and the transcript comes
    back with a hole exactly where the reader was looking.  It rides the
    message, which is what the turn log stores.
    """
    client = answering_with(
        stored(
            Message(role="user", content="6*7?"),
            Message(
                role="assistant",
                content="42",
                thinking="six sevens are forty-two",
            ),
        )
    )
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        block = app.query_one(AssistantMessage)
        text = shown(app)

    assert block.thinking == "six sevens are forty-two"
    assert "Thinking" in text
    assert "six sevens are forty-two" in text
    # The final answer's reasoning stays open, as it does live.
    assert block.thinking_expanded is True


async def test_a_restored_step_that_ended_in_a_tool_call_folds_its_reasoning() -> None:
    """And the rule is not restated here: the tool call is what folds it.

    A step on the way somewhere is machinery and reads better as one line; the
    answer keeps its own reasoning open.  Restore drives the transcript with the
    live calls, so `add_tool_start` does that by itself — a rebuild that set the
    fold explicitly would be a second place that knows the rule.
    """
    client = answering_with(
        stored(
            Message(role="user", content="6*7?"),
            Message(
                role="assistant",
                content="",
                thinking="first, what is six times seven",
                tool_calls=[ToolCall(id="c1", name="calc", arguments={"e": "6*7"})],
            ),
            Message(role="tool", content="42", tool_call_id="c1"),
            Message(role="assistant", content="it is 42", thinking="now say it"),
        )
    )
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        blocks = list(app.query(AssistantMessage))

    assert len(blocks) == 2
    assert blocks[0].thinking_expanded is False, "the pre-tool step should fold"
    assert blocks[1].thinking_expanded is True, "the answer should not"


async def test_a_harness_tool_call_is_not_drawn() -> None:
    """`_func_tool_unload` is the harness talking to itself, and always was.

    It is in the record because the model's tool list carries it, and the live
    transcript does not show it either.
    """
    client = answering_with(
        stored(
            Message(role="user", content="trim please"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="c1", name=FUNC_TOOL_UNLOAD, arguments={}),
                    ToolCall(id="c2", name="calc", arguments={"e": "1"}),
                ],
            ),
            Message(role="tool", content="dropped 3", tool_call_id="c1"),
            Message(role="tool", content="1", tool_call_id="c2"),
            Message(role="assistant", content="done"),
        )
    )
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        names = [panel.tool_name for panel in app.query(ToolCallWidget)]

    assert names == ["calc"]


async def test_a_restored_history_ends_at_its_tail() -> None:
    """The reader is put where a live session would have left them."""
    many = [
        stored(
            Message(role="user", content=f"question {n}"),
            Message(role="assistant", content="a long answer\n" * 20),
            turn_id=n,
        )
        for n in range(1, 6)
    ]
    client = answering_with(*many)
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        transcript = app.query_one(ChatView)
        assert await settled(
            pilot, lambda: transcript.scroll_y == transcript.max_scroll_y
        )
        assert transcript.max_scroll_y > 0


async def test_a_history_that_cannot_be_read_does_not_close_the_window() -> None:
    """Losing the sight of the conversation is not losing the conversation."""
    client = FakeAgentClient(answering("ok"))

    async def refuse() -> list[dict]:
        raise ConnectionError("the transcript call died")

    client.transcript = refuse  # type: ignore[method-assign]
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.pause()
        running = app.is_running
        bar = status(app)

    assert running is True
    assert "disconnected" in bar


async def test_one_unreadable_stored_row_does_not_kill_the_drain() -> None:
    """A row the screen cannot parse costs that row, not the turn behind it.

    `_restore` is awaited at the top of `_drain`, before the queue is popped
    from — so an exception out of the rebuild does not merely lose the history.
    It escapes the loop, the worker dies, `_draining` goes False, and the
    message the person is waiting on is never run and never shown: the window
    sits there saying "working" with `1 queued` in the bar.  A record written by
    an older build can carry a shape a field-by-field read does not expect — a
    `turn_id` that is not a number is one, and a tool call that is not a mapping
    is another — and neither is this module's to trust.
    """
    bad = stored(
        Message(role="user", content="a question nobody can read"),
        Message(role="assistant", content="an answer"),
    )
    bad["turn_id"] = "not-a-number"
    client = answering_with(bad)
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        # The row was dropped, so the count is still honest — the rebuild ran.
        assert "[restored 1 turn]" in shown(app)
        await submit(pilot, "hi")
        await settle(pilot, app)
        text = shown(app)

    assert "You> hi" in text, "the queued message never ran"
    assert "not-a-number" not in text


async def test_the_history_is_read_once() -> None:
    """Not once per turn: a second read would draw a second copy of it."""
    client = answering_with(*CONVERSATION)
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await restored(pilot, app)
        await submit(pilot, "hi")
        await settle(pilot, app)
        text = shown(app)

    assert client.reads == 1
    assert text.count("You> what is 6*7?") == 1


async def test_a_turn_that_begins_during_the_rebuild_waits_for_it() -> None:
    """The history cannot land under a turn that started while it was read.

    `_drain` asks `_ensure_connected` before it mounts anything, and that call
    used to stop at "connected" — but the connect sets the connected flag
    *before* it reads the history, so a prompt typed in that window mounted its
    line and its answer first and the whole restored conversation arrived
    underneath them.  The read is held open here the way a large one really
    takes a moment, and the prompt is typed into it: the transcript may not show
    a single line of the new turn until the history is up.
    """
    client = answering_with(*CONVERSATION, text="new answer")
    reading = asyncio.Event()
    release = asyncio.Event()
    original = client.transcript

    async def held_open() -> list[dict]:
        reading.set()
        await release.wait()
        return await original()

    client.transcript = held_open  # type: ignore[method-assign]
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await asyncio.wait_for(
            reading.wait(),
            timeout=2,  # timeouts: the value this test is built around, not a budget
        )  # timeouts: the value this test is built around, not a budget
        await submit(pilot, "hi")
        # The drain is parked on the read that is still in flight, so nothing of
        # the new turn may be on screen yet.
        assert "You> hi" not in shown(app)

        release.set()
        await settle(pilot, app)
        text = shown(app)

    assert text.index("You> what is 6*7?") < text.index("You> hi"), (
        "the restored history landed under a turn that started during the read"
    )
    assert text.index("[restored 2 turns]") < text.index("You> hi")
    assert "jack> new answer" in text


async def test_a_prompt_typed_before_the_servers_were_up_still_restores() -> None:
    """The cold-start case, and the reason the read sits on `_connect`.

    A window opened first and the daemons started after is ordinary.  The lazy
    reconnect inside the first turn is the first moment there is anything to
    ask, and it is also the last moment it may happen — the turn opens its
    block next.
    """
    client = answering_with(*CONVERSATION, text="new answer")
    attempts = {"n": 0}
    connect = client.connect

    async def flaky() -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("nothing is listening yet")
        await connect()

    client.connect = flaky  # type: ignore[method-assign]
    app = window(client)
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        await settle(pilot, app)
        text = shown(app)

    assert attempts["n"] > 1, "the prompt never retried the connection"
    assert "You> what is 6*7?" in text
    assert "jack> new answer" in text


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


async def test_a_dropped_connection_is_retried_by_the_next_prompt() -> None:
    """One lost stream must not cost the session.

    A turn that fails at the transport drops the connection inside the client,
    so the window has to ask the client rather than trust a flag of its own:
    with a latch it stays "connected" to a server nothing is talking to and
    every later turn fails the same way.
    """
    calls = {"n": 0}

    def respond(prompt: str, on_event):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("the stream dropped")
        on_event(TextDelta("second"))
        return "second"

    app = make_app(respond)
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "one")
        assert "the stream dropped" in shown(app)

        await submit(pilot, "two")
        assert "second" in shown(app), "the window never reconnected"


async def test_ctrl_s_survives_a_server_that_has_died() -> None:
    """A key binding that raises takes the whole app down with it.

    Textual's message pump turns an exception out of an action into a fatal
    error, so Ctrl+S against a server that is not answering would close the
    window — the one outcome a window built to survive a dead server must not
    have.  It is also the key somebody presses *because* something is broken,
    which is the worst moment to lose the window they are working in.
    """
    client = FakeAgentClient(answering("ok"))

    async def dead(ref: str) -> str:
        raise ConnectionError("the server died")

    client.switch_model = dead  # type: ignore[method-assign]
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        model_label="deepseek/deepseek-flash",
        agent="jack",
        model="deepseek/deepseek-flash",
        models=[ModelChoice(ref="deepseek/deepseek-flash")],
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert app.is_running, "the window closed on a failed switch"
        assert "was not switched" in shown(app), "and it says so"
        assert isinstance(app.focused, HistoryInput), "and the prompt is back"


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


async def test_the_context_is_the_last_call_not_the_turn_s_bill() -> None:
    """What a turn *cost* and how big it *is* are two numbers, and only one of
    them belongs in the status bar.

    A turn that called a tool twice made three model calls: the bill is the sum
    of all three, while the conversation is the size of the last one.  Showing
    the bill reads as a context near three times fuller than it is — and it is
    the tempting mistake, because `usage` is the number that is right there.
    """
    client = FakeAgentClient(answering("hello", tokens=245, context=135))
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        model="m",
        context_window=1000,
    )
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        assert "135 (13.5%)" in status(app)
        # ...while the line under the answer still reports what the turn cost.
        assert "245 tokens" in shown(app)


async def test_a_named_file_is_left_to_the_model(tmp_path) -> None:
    """The terminal sends the prompt as written and attaches nothing itself.

    **`@` is gone, and this is what replaced it**: a path in a sentence is text,
    and the model attaching it is the one call that `attach_image` is for.  A
    terminal that resolved paths itself would be a second implementation of one
    rule, and the place the two would disagree is which file a person meant.
    """
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"pretend png bytes")
    client = FakeAgentClient(answering("I see it"))
    app = SlifeApp("http://test/mcp", client_factory=lambda: client, agent="jack")

    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, f"what is this? {shot.as_posix()}")
        assert shot.name in shown(app)

    assert client.prompts == [f"what is this? {shot.as_posix()}"]
    assert client.images == [[]], "nothing is read here — the model attaches it"


async def test_only_one_connection_is_opened_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`connect` has an await between its check and its use.

    A prompt typed while the mount-time connect worker is still handshaking
    overlaps in that gap — the ordinary case on a slow start — and both callers
    used to enter a client, leaving whichever finished last to be the only one
    that `close` could reach.
    """
    from slife2.tui import client as client_module

    opened = 0

    async def slow_open(*args, **kwargs):
        nonlocal opened
        opened += 1
        await asyncio.sleep(0.05)
        return _Opened()

    monkeypatch.setattr(client_module, "open_server", slow_open)
    client = MCPAgentClient("http://test/mcp", agent="jack")

    await asyncio.gather(client.connect(), client.connect(), client.connect())

    assert opened == 1, "three callers, three connections"
    await client.close()


class _Opened:
    """The smallest thing `MCPAgentClient` treats as a connection."""

    async def __aenter__(self) -> _Opened:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_the_status_bar_stops_saying_working_when_the_turn_ends() -> None:
    """The bar has to be told about the transition, not asked during it.

    The drain's `finally` refreshes the status, and that line runs while the
    worker is still `RUNNING` — it is the last statement of the worker's own
    body.  So it reports "working" for the turn that has just finished, and
    nothing afterwards corrects it: the bar says working until the next turn,
    and then until the one after that.
    """
    app = make_app(answering("hello"))
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        worker = app._worker
        assert worker is not None
        await worker.wait()
        await pilot.pause()
        assert "working" not in status(app)


async def test_ctrl_c_cancels_a_running_turn() -> None:
    """A terminal where you cannot stop a runaway turn is not usable."""
    started = asyncio.Event()

    async def never_finishes(prompt, on_event, *, channel="tui", images=None):
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
        await asyncio.wait_for(
            started.wait(),
            timeout=2,  # timeouts: the value this test is built around, not a budget
        )  # timeouts: the value this test is built around, not a budget
        assert app._turn is not None and not app._turn.done()

        await pilot.press("ctrl+c")
        await pilot.pause()

        assert "[cancelled]" in shown(app)
        assert app.is_running


async def test_ctrl_c_quits_when_no_turn_is_running() -> None:
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app._turn is None
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not app.is_running


async def test_a_cancelled_turn_accepts_nothing_more() -> None:
    """The tail of a cancelled turn is dropped, ticket and turn alike.

    A delta can be handled after the turn it belongs to is over — the module
    docstring says so about `finish_assistant`, and the cancel is the same seam
    from the other side: an event already in Textual's pump, or one the client's
    reader task posted as it unwound.  Leaving the ticket on the cancelled turn
    accepted them, and `[cancelled]` is drawn by `add_note`, which does not close
    a turn — so a late delta opened a fresh block under the cancel line and a late
    tool call mounted a panel there, a transcript reading as though the turn had
    gone on after the person stopped it.
    """
    started = asyncio.Event()

    async def streams_then_hangs(prompt, on_event, *, channel="tui", images=None):
        on_event(TextDelta("partial"))
        started.set()
        await asyncio.sleep(30)
        return "never"

    app = make_app(answering("unused"))
    app._client_factory().run_turn = streams_then_hangs  # type: ignore[method-assign]

    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        await asyncio.wait_for(
            started.wait(),
            timeout=2,  # timeouts: the value this test is built around, not a budget
        )  # timeouts: the value this test is built around, not a budget
        await pilot.pause()
        ticket = app._ticket

        await pilot.press("escape")
        await pilot.pause()
        assert "[cancelled]" in shown(app)
        assert app.query_one(ChatView)._turn_open is False

        # Both halves of the seam, on the ticket the turn really used.
        app.post_message(TurnEventMessage(ticket, TextDelta("LATE-FRAGMENT")))
        app.post_message(TurnEventMessage(ticket, ToolCallStarted("late", "sneak", {})))
        await pilot.pause()
        text = shown(app)
        names = [panel.tool_name for panel in app.query(ToolCallWidget)]

    assert "LATE-FRAGMENT" not in text, "a late delta reopened the cancelled turn"
    assert names == [], "a late tool call mounted a panel under the cancel"
    assert text.index("partial") < text.index("[cancelled]")


async def test_escape_cancels_and_stops_there() -> None:
    """The cancel key, and the only thing it does.

    Slife's rule.  Escape is pressed by reflex — to back out of a half-typed
    thought, to dismiss what is not there — and a terminal where that reflex
    closes the window is one nobody dares press it in.  Ctrl+C is the way out;
    with no turn running this leaves the app up.
    """
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app._turn is None
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running


# --- the approval row ---------------------------------------------------------


def asking(call_id: str = "call-1", name: str = "sensitive"):
    """A scripted turn that puts one tool call to the person, then answers.

    The request arrives as a progress event and the answer cannot travel back on
    it, so the window draws a row and answers it on its own call.  What is under
    test here is the row, not the handshake — `tests/test_approval.py` holds that
    half.
    """

    def respond(prompt: str, on_event):
        on_event(ToolApprovalRequested(call_id, name, {"path": "/tmp/notes.md"}))
        return "done"

    return respond


async def test_a_keystroke_while_a_call_awaits_an_answer_does_not_refuse_it() -> None:
    """Typing your next thought is not an answer, and must not be read as one.

    `ApprovalPrompt` treated losing focus as a denial, and two things take focus
    on purpose: every printable key (`redirect_printable` lands it in the
    prompt) and `Ctrl+S`.  So an `a`, or a paste, silently refused the call the
    model was parked on — the person's next keystroke deciding a question they
    had not been asked to answer.  A blur now leaves the row up and pending; the
    keys `y`/`n`/`Esc` and a cancelled turn are the answers.

    The row is also the one thing the transcript's own `plain_text` used not to
    read — its class was drawn by nothing and selected by nothing — so the last
    assertion is that the question is part of what the screen reports.
    """
    client = FakeAgentClient(asking())
    app = SlifeApp("http://test/mcp", client_factory=lambda: client, agent="jack")
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "do it")
        await pilot.pause()
        assert app._approval is not None, "the row was drawn"
        assert "requests approval" in shown(app), "and the transcript reads it"

        await pilot.press("a")
        await pilot.pause()

        assert app._approval is not None, "a keystroke refused the call"
        assert app._approval.pending is True
        typed = app.query_one(HistoryInput).text

    assert client.answers == [], "an answer was sent for a call nobody decided"
    assert typed == "a", "the keystroke went to the draft, which is where it was aimed"


# --- the model switch ---------------------------------------------------------

#: Two models, so "switch" has somewhere to switch to, and with different
#: capabilities, so a bar that kept the old model's facts is visible.
MODELS = [
    ModelChoice(ref="local/big", label="Big", context_window=100_000, thinking=True),
    ModelChoice(ref="local/small", context_window=8_000, vision=True),
]


def model_app(respond, **kwargs) -> SlifeApp:
    """A window whose Ctrl+S has something to offer."""
    return SlifeApp(
        "http://test/mcp",
        client_factory=lambda: FakeAgentClient(respond),
        agent="jack",
        model="local/big",
        models=MODELS,
        **kwargs,
    )


async def test_ctrl_s_lists_the_models_and_opens_on_the_one_in_use() -> None:
    """The cursor starts on the model already answering, so Enter is a no-op.

    That is the difference between a list and a menu with a default: the row
    under the cursor is the one in front of you, and pressing Enter on it is
    how somebody who opened the picker to *look* gets out again without
    changing anything.
    """
    client = FakeAgentClient(answering("ok"))
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        agent="jack",
        model="local/big",
        models=MODELS,
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        listing = shown(app)
        assert "local/big" in listing and "local/small" in listing
        assert "ctx 100,000" in listing, "what the model is, beside its name"
        assert "Big" in listing and "thinking" in listing

        # The cursor is on that row, so a bare Enter chooses the model already
        # in use — which is a switch the server has nothing to do about.
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert client.switches == ["local/big"]
        assert "local/big" in status(app)


async def test_choosing_a_model_switches_it_and_the_bar_follows() -> None:
    """**The whole point of the key**: pick a model, and it is the one in use.

    What the bar says about a model is a fact about the *model* — its window,
    whether it reads pictures — so a bar that kept the old window's percentage
    and the old model's capability marks would be describing something that is
    not answering any more.
    """
    client = FakeAgentClient(answering("ok"))
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        agent="jack",
        model="local/big",
        models=MODELS,
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert client.switches == ["local/small"]
        assert "Switched to local/small" in shown(app)
        # The server's own sentence, kept as it was written: it is the only
        # thing that says what a switch does to a conversation already running.
        assert client.switch_answer in shown(app)
        bar = status(app)
        assert "local/small" in bar and GLYPHS["vision"] in bar
        assert "local/big" not in bar and GLYPHS["thinking"] not in bar
        assert isinstance(app.focused, HistoryInput)


async def test_escape_offers_nothing_and_changes_nothing() -> None:
    """A picker is a question, and a question can be left unanswered.

    Nothing is switched, the conversation keeps its model, and the prompt has
    the keys back — the picker is not a mode that has to be exited from.
    """
    client = FakeAgentClient(answering("ok"))
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        agent="jack",
        model="local/big",
        models=MODELS,
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()

        assert client.switches == []
        assert "Cancelled" in shown(app)
        assert "local/big" in status(app)
        assert isinstance(app.focused, HistoryInput)
        assert app.is_running


async def test_a_second_ctrl_s_does_not_stack_a_picker() -> None:
    """The re-entry guard, which v1 had for the same reason.

    A second picker would leave the first one's future unresolved, and the
    window would then be waiting on a decision nobody can make — every later
    Ctrl+S doing nothing, which reads as a broken key.
    """
    app = model_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert len(app.query(ModelPicker)) == 1

        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app._switching, "and a later Ctrl+S opens one again"


async def test_ctrl_s_over_an_approval_does_not_refuse_the_call() -> None:
    """The other focus-stealer: the switch, opened while a call waits.

    `action_switch_model` focuses the picker it mounts, and that blur used to
    deny the call — which sent a refocus back to the prompt, which blurred *the
    picker*, which cancelled the question ctrl+s had just opened.  So the one
    key refused a call the model was parked on and then threw away its own
    question; both are read as nothing here.
    """
    client = FakeAgentClient(asking())
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        agent="jack",
        model="local/big",
        models=MODELS,
    )
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "do it")
        await pilot.pause()
        assert app._approval is not None, "the row was drawn"

        await pilot.press("ctrl+s")
        await pilot.pause()

        assert len(app.query(ModelPicker)) == 1, "the picker survives its own key"
        assert app._approval is not None, "and the call is still being asked about"
        assert app._approval.pending is True

        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()

    assert client.answers == [], "ctrl+s refused a call nobody decided"
    assert client.switches == [], "and nothing was switched either"


async def test_a_refused_switch_moves_nothing_and_says_why() -> None:
    """A refusal is an answer, and the one thing it must not do is move the bar.

    The server writes nothing when it refuses, so a window that showed the new
    model anyway would be the one place in the system claiming a switch that
    did not happen.
    """
    client = FakeAgentClient(answering("ok"))
    client.switch_answer = "[refused] `local/nope` is not a model"
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        agent="jack",
        model="local/big",
        models=MODELS,
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert "is not a model" in shown(app)
        assert "local/big" in status(app), "the bar still names the model in use"


async def test_ctrl_s_says_so_when_there_is_nothing_to_switch_to() -> None:
    """An empty picker is a key that looks broken; the fix is a tool with a name."""
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: FakeAgentClient(answering("ok")),
        agent="jack",
        model_label="deepseek/deepseek-flash",
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert "no models are configured" in shown(app)
        assert len(app.query(ModelPicker)) == 0


async def test_ctrl_c_copies_the_selection_in_the_prompt() -> None:
    """Ctrl+C is the copy key, and the prompt is where a person copies from.

    Which is why the app's binding is not priority: the priority pass runs
    *before* the focused widget, so a priority ctrl+c here takes the key from
    `TextArea`'s own copy, and from the screen's, which is what copies a mouse
    selection out of the transcript.  Both copy actions skip themselves when
    there is nothing selected, and that skip is the only reason the key ever
    reaches the app.
    """
    app = make_app(answering("ok"))
    async with app.run_test(size=SIZE) as pilot:
        prompt = app.query_one(HistoryInput)
        prompt.text = "copy me"
        prompt.select_all()
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert app.clipboard == "copy me"
        # Copying is not cancelling, and it is not a way to lose the draft.
        assert app.is_running
        assert prompt.text == "copy me"


async def test_a_selection_keeps_ctrl_c_off_a_running_turn() -> None:
    """The price of the key being copy first: the turn keeps running.

    Escape is the way out of exactly this state — nothing claims it, and it
    never quits — unlike ctrl+c, which the focused widget takes whenever it has
    something to copy.  A terminal that cannot copy is worse than one that asks
    for a second key to stop a runaway turn; the second key is checked here
    rather than in a test of its own because *this* is the state that needs it.
    """
    started = asyncio.Event()

    async def never_finishes(prompt, on_event, *, channel="tui", images=None):
        started.set()
        await asyncio.sleep(30)
        return "never"

    app = make_app(answering("unused"))
    app._client_factory().run_turn = never_finishes  # type: ignore[method-assign]

    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "hi")
        await asyncio.wait_for(
            started.wait(),
            timeout=2,  # timeouts: the value this test is built around, not a budget
        )  # timeouts: the value this test is built around, not a budget
        prompt = app.query_one(HistoryInput)
        prompt.text = "still typing"
        prompt.select_all()

        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.clipboard == "still typing"
        assert app._turn is not None and not app._turn.done()

        await pilot.press("escape")
        await pilot.pause()
        assert "[cancelled]" in shown(app)


async def test_a_second_prompt_waits_and_both_are_answered() -> None:
    """The behaviour this whole change exists for, seen from the window.

    What it replaced: a second Enter ran `exclusive=True`, which cancelled the
    turn already running and threw it away — its text stayed on screen, and
    neither it nor the user's message ever reached the conversation.  Now the
    second prompt queues, and the order the turns happened in is the order they
    are drawn in.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_then_fast(prompt, on_event, *, channel="tui", images=None):
        if prompt == "alpha":
            started.set()
            await release.wait()
        answer = f"answer to {prompt}"
        on_event(TextDelta(answer))
        return answer

    app = make_app(answering("unused"))
    app._client_factory().run_turn = slow_then_fast  # type: ignore[method-assign]

    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "alpha")
        await asyncio.wait_for(
            started.wait(),
            timeout=2,  # timeouts: the value this test is built around, not a budget
        )  # timeouts: the value this test is built around, not a budget
        await submit(pilot, "beta")
        await pilot.pause()

        # Waiting, and said so rather than shown in the wrong place: a queued
        # message drawn above the answer still being written would report the
        # turns out of order.
        assert "1 queued" in status(app)
        assert "You> beta" not in shown(app)

        release.set()
        await settle(pilot, app)

        text = shown(app)
        assert (
            text.index("You> alpha")
            < text.index("answer to alpha")
            < text.index("You> beta")
            < text.index("answer to beta")
        )
        assert "queued" not in status(app)


async def test_a_cancelled_turn_does_not_take_the_queue_with_it() -> None:
    """Ctrl+C stops one turn.  The messages behind it are still wanted."""
    started = asyncio.Event()

    async def first_hangs(prompt, on_event, *, channel="tui", images=None):
        if prompt == "alpha":
            started.set()
            await asyncio.sleep(30)
        answer = f"answer to {prompt}"
        on_event(TextDelta(answer))
        return answer

    app = make_app(answering("unused"))
    app._client_factory().run_turn = first_hangs  # type: ignore[method-assign]

    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, "alpha")
        await asyncio.wait_for(
            started.wait(),
            timeout=2,  # timeouts: the value this test is built around, not a budget
        )  # timeouts: the value this test is built around, not a budget
        await submit(pilot, "beta")
        await pilot.pause()

        await pilot.press("ctrl+c")
        await settle(pilot, app)

        # The turn the user gave up on stopped; the one they are still waiting
        # for ran.
        assert "[cancelled]" in shown(app)
        assert "answer to beta" in shown(app)
        assert "answer to alpha" not in shown(app)


async def test_the_model_s_capabilities_are_shown_beside_its_name() -> None:
    """Reasoning and vision, from the model's config, before anything is typed.

    Both are facts about the model rather than about a turn, so they are there
    from the first frame.  Vision especially: `@picture.png` is either going to
    work or be refused, and a person should know which before they type it.
    """
    client = FakeAgentClient(answering("hi"))
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        model="deepseek/deepseek-flash",
        thinking=True,
        vision=True,
    )
    async with app.run_test(size=SIZE):
        bar = status(app)

    assert f"{GLYPHS['thinking']} thinking" in bar
    assert f"{GLYPHS['vision']} vision" in bar


async def test_a_text_only_model_claims_no_capabilities() -> None:
    """No badge rather than a crossed-out one: the bar says what is true."""
    app = make_app(answering("hi"))
    async with app.run_test(size=SIZE):
        bar = status(app)

    assert "thinking" not in bar
    assert "vision" not in bar


async def test_the_context_indicator_is_there_before_the_first_turn() -> None:
    """From zero, because the window is a fact about the model.

    An indicator that only appears once it has something to say is one nobody
    knows is there — which is what "our TUI doesn't show the context" looks like
    from the outside.
    """
    client = FakeAgentClient(answering("hi"))
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        model="m",
        context_window=1000,
    )
    async with app.run_test(size=SIZE):
        assert f"{GLYPHS['up']} 0 (0.0%)" in status(app)


async def test_a_message_handed_over_by_another_channel_runs_as_if_typed() -> None:
    """The pass-through, and the whole of it: words in this window's queue.

    `deliver` is what the window's inbox calls.  What it does is what the person
    typing does — nothing more — so the line, the turn and the record are the ones
    this window already draws, and the channel rides along for the two things that
    depend on it: the bubble and where the turn came from.
    """
    client = FakeAgentClient(answering("你好"))
    app = SlifeApp("http://test/mcp", client_factory=lambda: client, agent="jack")
    async with app.run_test(size=SIZE) as pilot:
        app.deliver('[Wechat:{"peer_wechat_id": "p1"}] hi', "wechat")
        await settle(pilot, app)
        text = shown(app)
        channels = client.channels

    assert "Wechat> hi" in text
    # The marker is the model's and not the reader's, exactly as for a turn read
    # back out of the record.
    assert "peer_wechat_id" not in text
    assert "jack> 你好" in text
    assert channels == ["wechat"]


async def test_the_window_attaches_to_its_inbox_and_a_push_reaches_the_queue() -> None:
    """The half above `deliver`: the window holds a session open, and is pushed to.

    A window no longer *serves* anything — it attaches to `slife2-tui` on mount
    and keeps that call open for its life, which is what makes a message able to
    find one window out of several.  The plugin is faked here because the real
    one is a socket and a second process; `tests/test_tui_control.py` runs that
    end to end over a port.

    The assertion is the same one as above, deliberately: what arrives on the
    push is the *person's own typing* by the time it is done, or the seam is in
    the wrong place.
    """
    client = FakeAgentClient(answering("你好"))
    window = FakeWindowClient()
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        agent="jack",
        control_url="http://test/inbox",
        window_factory=lambda: window,
    )
    async with app.run_test(size=SIZE) as pilot:
        assert await settled(pilot, window.attaching.is_set), "never attached"
        window.push("hello")
        await settle(pilot, app)
        text = shown(app)
        channels = client.channels

    assert "Wechat> hello" in text
    assert channels == ["wechat"]


async def test_a_window_with_no_inbox_attaches_to_nothing() -> None:
    """`control_url` empty is a window that serves no purpose for other channels.

    That is what a unit test wants and what a window started without a config
    address gets, and it has to mean *nothing happens* rather than a worker
    retrying against an empty string forever.
    """
    client = FakeAgentClient(answering("ok"))
    window = FakeWindowClient()
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        agent="jack",
        window_factory=lambda: window,
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()

    assert not window.attaching.is_set()
    assert not window.connected


async def test_a_pushed_note_is_drawn_and_does_not_wait_for_a_turn() -> None:
    """A note is a line, not a message: drawn now, run never.

    Both kinds arrive on the same held call (`slife2.tui.control`), and this is
    the branch between them.  `push` here is deliberately *not* followed by a
    turn: a presence line that waited for the queue would be telling the person
    about a peer who has since gone.
    """
    client = FakeAgentClient(answering("ok"))
    window = FakeWindowClient()
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        agent="jack",
        control_url="http://test/inbox",
        window_factory=lambda: window,
    )
    async with app.run_test(size=SIZE) as pilot:
        assert await settled(pilot, window.attaching.is_set), "never attached"
        window.push_note("⚡ jill online")
        await settled(pilot, lambda: "jill online" in shown(app))
        text = shown(app)

    # The harness's own shape, applied by the widget and not by the plugin.
    assert "[⚡ jill online]" in text
    assert client.prompts == [], "a note is not a turn"
    assert "A2A(jill)>" not in text


async def test_a_window_holds_its_agent_on_the_mesh_and_lets_go_with_it() -> None:
    """The window is the enrollment: joining is the call, leaving is its cancel.

    **Nothing is drawn for it**, here or at the other end: a window showing jack
    is jack, so `⚡ jack online` in it would be the harness telling somebody what
    they already are.  What this asserts is the lifetime — the call is held while
    the window is up, and its cancellation is the departure.
    """
    client = FakeAgentClient(answering("ok"))
    window = FakeWindowClient()
    mesh = FakeMeshClient()
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        agent="jack",
        control_url="http://test/inbox",
        mesh_url="http://test/mesh",
        window_factory=lambda: window,
        mesh_factory=lambda: mesh,
    )
    async with app.run_test(size=SIZE) as pilot:
        assert await settled(pilot, mesh.holding.is_set), "never joined the mesh"
        assert shown(app) == "", "arriving on the mesh is not news to yourself"

    # The window is gone, so the call it held is gone with it — which is what
    # publishes the offline card, and why there is no `leave` to send.
    assert not mesh.connected


async def test_a_refused_mesh_says_so_once_and_keeps_trying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A taken name is worth one line, not one every two seconds.

    The retry itself is `_attach_inbox`'s rule and is right here too: a plugin
    that is not answering must not make the window unusable.  What must not be
    repeated is the *sentence* — it is the same sentence each time, and a
    terminal left open beside a broker holding the name would otherwise fill the
    transcript with it.
    """
    # The cadence, not the rule: the retry is what is under test, and the real
    # two seconds would be a test that waits for them.
    monkeypatch.setattr(_T.timeouts.pacing, "reconnect", 0.01)
    client = FakeAgentClient(answering("ok"))
    mesh = RefusingMeshClient()
    app = SlifeApp(
        "http://test/mcp",
        client_factory=lambda: client,
        agent="jack",
        mesh_url="http://test/mesh",
        mesh_factory=lambda: mesh,
    )
    async with app.run_test(size=SIZE) as pilot:
        assert await settled(pilot, lambda: mesh.attempts >= 2), "never retried"
        await pilot.pause()
        text = shown(app)

    assert text.count("not on the mesh") == 1
    assert "already in use" in text, "the reason, verbatim"
