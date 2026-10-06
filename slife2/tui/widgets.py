"""The widgets the TUI is made of.

The design is slife v1's, ported: a low-chrome dark transcript, borderless
messages marked by a bold amber prefix, tool calls as collapsible bordered
panels, and a single dim status row.  What follows is what is load-bearing about
it, because most of these choices look arbitrary until you change one.

**Nothing the model produces is parsed as markup.**  Every string that came from
a model, a tool or a user goes through `Text(...)`, which does not interpret
`[bold]`-style tags.  Rich's markup would raise on a stray `[` — and model output
is full of them — so the distinction between "our label" and "their text" is
kept by construction rather than by escaping.

**Text is styled in pieces, not by string templates.**  A prefix is a separate
stylable span rather than part of a format string, which is why slife's messages
can carry a coloured `You>` and slife2's can too, without either of them
inventing an escaping scheme.
"""

from __future__ import annotations

from datetime import datetime
from typing import TypeVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message as TextualMessage
from textual.widgets import Static, TextArea

from slife2.tui.theme import GLYPHS, PALETTE

#: So `_add` can hand back the widget type it was given.
WidgetT = TypeVar("WidgetT", bound=Static)

#: How much of a tool's primary argument the header row shows.  The point of
#: the row is to be readable at a glance, so it is a glance's worth.
PRIMARY_ARG_CHARS = 72

#: How much reasoning an expanded thinking block shows.  Reasoning runs long —
#: it is the model talking to itself — and past a few hundred characters nobody
#: is reading it, they are skimming for the shape of it.
THINKING_PREVIEW_CHARS = 500

#: How many result lines the expanded panel renders before summarising.  The
#: panel also caps at 60% of the viewport in CSS and scrolls; this is the guard
#: for pathological output, which would otherwise be parsed and laid out in
#: full before anything could stop it.
MAX_RESULT_LINES = 500


def _timestamp(moment: datetime | None = None) -> str:
    """`HH:MM`, or a longer form once the day is no longer today."""
    now = datetime.now()
    moment = moment or now
    if moment.date() == now.date():
        return moment.strftime("%H:%M")
    if moment.year == now.year:
        return moment.strftime("%m-%d %H:%M")
    return moment.strftime("%Y-%m-%d %H:%M")


def plain(text: str, style: str = "") -> Text:
    """Model, tool or user text as a renderable it cannot style itself with."""
    return Text(text, style=style)


def _primary_argument(arguments: dict) -> str:
    """The first non-empty string argument, for the header row.

    A tool call's arguments are the interesting part of it — "read:
    config.yaml" says something, "read" does not — so the first one is shown
    even though which key it is varies by tool.
    """
    for value in arguments.values():
        if isinstance(value, str) and value.strip():
            return value
    return ""


class ChatView(VerticalScroll):
    """The scrolling transcript.

    Streaming means mutating the trailing block on every delta, which is why
    this holds widgets rather than being a `RichLog`: `RichLog` appends whole
    lines, so writing one line per delta would render a separate block per
    token.
    """

    can_focus = True

    def __init__(self, agent: str = "") -> None:
        super().__init__(id="chat-view")
        #: Used to sign the assistant's messages, so a conversation reads
        #: `You> ...` / `jack> ...` rather than only marking one side of it.
        self._agent = agent
        self._streaming: AssistantMessage | None = None
        self._last_assistant: AssistantMessage | None = None
        self._streaming_text = ""
        self._turn_open = False
        #: Tool panels by call id, so a result can find the row it belongs to.
        self._tools: dict[str, ToolCallWidget] = {}
        #: Whether the view is pinned to the bottom.  Cleared when the user
        #: scrolls up, so streaming does not yank the page out from under
        #: someone reading back.
        self._at_tail = True

    # --- following the tail --------------------------------------------------

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        self._at_tail = self.is_vertical_scroll_end

    def follow_tail(self) -> None:
        if self._at_tail:
            self.scroll_end(animate=False)

    def jump_to_tail(self) -> None:
        self._at_tail = True
        self.scroll_end(animate=False)

    def _add(self, widget: WidgetT, classes: str = "") -> WidgetT:
        if classes:
            widget.add_class(classes)
        self.mount(widget)
        self.follow_tail()
        return widget

    def _close_block(self) -> None:
        self._streaming = None
        self._streaming_text = ""

    # --- messages ------------------------------------------------------------

    def add_user(self, text: str) -> None:
        """`[HH:MM] You> text`, the timestamp dim and the prefix bold amber."""
        self._turn_open = False
        self._close_block()
        line = Text.assemble(
            (f"[{_timestamp()}] ", f"dim {PALETTE['dim']}"),
            ("You> ", f"bold {PALETTE['amber-bold']}"),
            (text, PALETTE["text"]),
        )
        self._add(Static(line), "user-message")

    def add_note(self, text: str) -> None:
        """A line from the harness rather than from anyone in the conversation."""
        self._close_block()
        self._add(Static(plain(text, PALETTE["dim"])), "system-message")

    def add_error(self, text: str) -> None:
        self._turn_open = False
        self._close_block()
        line = Text.assemble(
            (f"{GLYPHS['failed']} ", f"bold {PALETTE['red']}"),
            (text, PALETTE["red"]),
        )
        self._add(Static(line), "system-message")

    # --- the assistant's turn ------------------------------------------------

    def begin_assistant(self) -> None:
        """Open a turn: from here until `finish_assistant`, deltas land."""
        self._close_block()
        self._turn_open = True
        self._streaming = self._open_block()

    def _open_block(self) -> AssistantMessage:
        widget = self._add(AssistantMessage(self._agent), "assistant-message")
        self._last_assistant = widget
        return widget

    def append_text(self, delta: str) -> None:
        """Add a delta to the current block, if a turn is still open.

        Deltas that arrive after the turn's result are **dropped**, and that is
        load-bearing rather than defensive.  Progress events reach the UI as
        queued messages while the result comes back on the awaited call, so a
        delta can be handled after `finish_assistant` has already run.  Letting
        it reopen a block would append a stale fragment under the final answer.
        """
        if not self._turn_open:
            return
        if self._streaming is None:
            # Text resumed after a tool panel: a new block, same turn.
            self._streaming_text = ""
            self._streaming = self._open_block()
        self._streaming_text += delta
        self._streaming.set_text(self._streaming_text)
        self.follow_tail()

    def append_thinking(self, delta: str) -> None:
        """Add reasoning to the current block, if a turn is still open.

        Dropped after the turn's result for the same reason deltas are: a late
        fragment appended under a finished answer is worse than a missing one.
        """
        if not self._turn_open:
            return
        if self._streaming is None:
            self._streaming = self._open_block()
        self._streaming.append_thinking(delta)
        self.follow_tail()

    def finish_assistant(self, text: str) -> None:
        """Close the turn with the authoritative answer.

        Not an optimisation — a correction.  The deltas are a display channel
        and may be incomplete or late; the return value of the tool call is the
        answer, and this is where the two are reconciled.
        """
        self._turn_open = False
        if self._streaming is None:
            self._streaming = self._open_block()
        self._streaming_text = text
        self._streaming.set_text(text)
        self._close_block()
        self.jump_to_tail()

    def set_usage(self, tokens: int) -> None:
        """Put a token count under the answer it belongs to.

        Aimed at the *last* block rather than the streaming one: by the time
        usage arrives the turn has usually been closed, and the count belongs
        to the message the user is looking at either way.
        """
        target = self._streaming or self._last_assistant
        if target is not None:
            target.set_usage(tokens)

    # --- tools ---------------------------------------------------------------

    def add_tool_start(
        self, call_id: str, name: str, arguments: dict | None = None
    ) -> None:
        # Closes the current text block but not the turn: text may resume after.
        #
        # The block it closes was a step on the way somewhere rather than the
        # answer, so its reasoning folds away — the final answer keeps its own
        # open.  This is the only place that distinction is known.
        if self._streaming is not None:
            self._streaming.collapse_thinking()
        self._close_block()
        widget = ToolCallWidget(call_id=call_id, name=name, arguments=arguments or {})
        self._tools[call_id] = widget
        self.mount(widget)
        self.follow_tail()

    def add_tool_end(
        self, call_id: str, ok: bool, result_preview: str, result_chars: int
    ) -> None:
        widget = self._tools.get(call_id)
        if widget is None:  # pragma: no cover - a result without a start
            return
        # `ok` is the positive phrasing and `set_complete` wants the negative
        # one.  Naming both ends after the same thing is how these get swapped;
        # a test that asserts a success says "done" is what keeps them honest.
        widget.set_complete(result_preview, is_error=not ok, result_chars=result_chars)
        self.follow_tail()

    def clear_all(self) -> None:
        """Empty the transcript.  Pairs with starting a new conversation."""
        self._turn_open = False
        self._close_block()
        self._tools.clear()
        for widget in list(self.query(Static)) + list(self.query(ToolCallWidget)):
            widget.remove()

    # --- for tests -----------------------------------------------------------

    def plain_text(self) -> str:
        """Everything shown, as plain text.

        Reads the widgets rather than a private mirror of what was appended, so
        a test asserting on this is asserting on what the user sees.
        """
        lines = []
        for widget in self.query(
            ".user-message, .assistant-message, .system-message, .tool-call"
        ):
            if isinstance(widget, ToolCallWidget):
                lines.append(widget.plain_text())
            elif isinstance(widget, Static):
                content = widget.content
                lines.append(
                    content.plain if isinstance(content, Text) else str(content)
                )
        return "\n".join(lines)


class AssistantMessage(Static):
    """What the model said, streamed in and then corrected.

    The text is rebuilt wholesale on each delta rather than appended to, which
    is what lets the authoritative answer replace it exactly — and what makes
    the optional usage line land underneath rather than being appended to the
    streamed text and then duplicated.
    """

    can_focus = True

    BINDINGS = [
        Binding("enter,space", "toggle_thinking", "Toggle thinking", show=False),
    ]

    def __init__(self, agent: str = "") -> None:
        super().__init__()
        self._agent = agent
        self._text = ""
        self._thinking = ""
        #: Shown by default, folded away only for a step that is not the answer.
        #:
        #: Reasoning on the *final* answer is the interesting case — it is how
        #: you tell whether the model understood the question — so it is shown.
        #: A step that ended in a tool call is machinery on the way somewhere,
        #: and reads better as one line; `collapse_thinking` is what a tool call
        #: does to the block it closes.
        self._thinking_open = True
        self._tokens = 0

    @property
    def thinking(self) -> str:
        return self._thinking

    @property
    def text(self) -> str:
        """The answer, without the reasoning that may sit above it."""
        return self._text

    @property
    def thinking_expanded(self) -> bool:
        return self._thinking_open

    def append_thinking(self, delta: str) -> None:
        self._thinking += delta
        self._refresh()

    def collapse_thinking(self) -> None:
        """Fold the reasoning away, for a step that is not the answer."""
        if self._thinking:
            self._thinking_open = False
            self._refresh()

    def action_toggle_thinking(self) -> None:
        self._thinking_open = not self._thinking_open
        self._refresh()

    def on_click(self) -> None:
        """A click expands, and never collapses — a click is also how text gets
        selected, and collapsing would destroy the selection."""
        if not self._thinking_open and self._thinking:
            self._thinking_open = True
            self._refresh()

    def _thinking_block(self) -> Text:
        """The reasoning, as one line or as a block."""
        label = f"{GLYPHS['thinking']} Thinking"
        if not self._thinking_open:
            line = Text(
                f"{label} ({len(self._thinking):,} chars) {GLYPHS['collapsed']}",
                style=f"italic {PALETTE['dim']}",
            )
            return line

        shown = self._thinking
        if len(shown) > THINKING_PREVIEW_CHARS:
            shown = shown[:THINKING_PREVIEW_CHARS] + GLYPHS["ellipsis"]
        line = Text(f"{label}{GLYPHS['ellipsis']}\n", style=f"italic {PALETTE['dim']}")
        line.append(shown, style=PALETTE["dim"])
        return line

    def set_text(self, text: str) -> None:
        self._text = text
        self._refresh()

    def set_usage(self, tokens: int) -> None:
        self._tokens = tokens
        self._refresh()

    def _refresh(self) -> None:
        line = Text()
        if self._thinking:
            line.append_text(self._thinking_block())
            line.append("\n")
        if self._agent:
            # The same treatment as the user's `You> `: a signature in bold
            # amber, so the two sides of a conversation are marked the same way
            # and the agent's name is visible where it is being used.
            line.append(f"{self._agent}> ", f"bold {PALETTE['amber-bold']}")
        if not self._text:
            if not self._agent:
                # Nothing at all yet: a dim ellipsis is the only affordance that
                # says "working", and dim so it never reads as content.
                line.append(GLYPHS["ellipsis"], PALETTE["dimmest"])
        else:
            line.append(self._text, PALETTE["text"])
        if self._tokens:
            line.append(f"\n{GLYPHS['up']} {self._tokens:,} tokens", PALETTE["dim"])
        self.update(line)


class ToolCallWidget(VerticalScroll):
    """One tool call, as a collapsible bordered panel.

    Collapsed it is a single header row — which is the state it will spend
    almost all its life in, since a collapsed one-liner is the whole value of
    showing a tool call at all.  Expanded it shows the arguments and the result.
    """

    can_focus = True

    BINDINGS = [
        Binding("enter,space", "toggle_detail", "Toggle detail", show=False),
    ]

    def __init__(self, *, call_id: str, name: str, arguments: dict) -> None:
        super().__init__()
        self.call_id = call_id
        self.tool_name = name
        self.arguments = arguments
        self._collapsed = True
        self._result = ""
        self._result_chars = 0
        self._is_error = False
        self._done = False
        self.add_class("tool-call")
        self._body = Static(classes="tool-content")
        self._refresh_display()

    def compose(self):
        yield self._body

    def _refresh_display(self) -> None:
        self._body.update(self._content())

    def _status(self) -> tuple[str, str, str]:
        """`(glyph, colour, word)` for the header row."""
        if not self._done:
            return GLYPHS["running"], PALETTE["amber"], "running"
        if self._is_error:
            return GLYPHS["error"], PALETTE["red"], "error"
        return GLYPHS["done"], PALETTE["green"], "done"

    def _label(self) -> str:
        """`read_file` reads better as `Read file`."""
        return self.tool_name.replace("_", " ").capitalize()

    def _header(self) -> Text:
        glyph, colour, word = self._status()
        indicator = GLYPHS["collapsed"] if self._collapsed else GLYPHS["expanded"]
        line = Text.assemble(
            (f"{indicator} ", PALETTE["text"]),
            (f"{glyph} ", colour),
            (self._label(), f"bold {PALETTE['amber']}"),
        )
        primary = _primary_argument(self.arguments)
        if primary:
            short = primary[:PRIMARY_ARG_CHARS]
            if len(primary) > PRIMARY_ARG_CHARS:
                short += GLYPHS["ellipsis"]
            line.append(": ", PALETTE["text"])
            line.append(short, PALETTE["muted"])
        line.append("  ")
        line.append(word, colour)
        return line

    def _content(self) -> Text:
        line = self._header()
        if self._collapsed:
            return line

        line.append("\n")
        line.append("Arguments", f"bold {PALETTE['muted']}")
        if self.arguments:
            for key, value in self.arguments.items():
                shown = str(value)
                if len(shown) > 500:
                    shown = shown[:500] + GLYPHS["ellipsis"]
                line.append(f"\n  {key} = ", PALETTE["muted"])
                line.append(shown, PALETTE["text-secondary"])
        else:
            line.append("\n  (no arguments)", PALETTE["muted"])

        if self._done:
            if self._is_error:
                line.append("\n\nError", f"bold {PALETTE['red']}")
                line.append("\n" + self._result, PALETTE["red"])
            else:
                line.append("\n\nResult", f"bold {PALETTE['muted']}")
                rows = self._result.split("\n")
                if len(rows) > MAX_RESULT_LINES:
                    line.append(
                        "\n" + "\n".join(rows[:MAX_RESULT_LINES]),
                        PALETTE["text-secondary"],
                    )
                    line.append(
                        f"\n{GLYPHS['ellipsis']} {len(rows) - MAX_RESULT_LINES} more lines "
                        f"of {self._result_chars:,} characters",
                        PALETTE["dimmest"],
                    )
                else:
                    line.append("\n" + self._result, PALETTE["text-secondary"])
        return line

    def set_complete(self, result: str, is_error: bool, result_chars: int) -> None:
        self._result = result
        self._is_error = is_error
        self._result_chars = result_chars
        self._done = True
        self._refresh_display()

    @property
    def collapsed(self) -> bool:
        """Whether the panel is showing only its header row."""
        return self._collapsed

    def action_toggle_detail(self) -> None:
        self._collapsed = not self._collapsed
        self._refresh_display()

    def on_click(self) -> None:
        """A click expands, and never collapses.

        Asymmetric on purpose: a click is also how text gets selected, and
        collapsing on click would destroy the selection the user was making.
        Collapsing is Enter or Space, which cannot be a mis-aimed drag.
        """
        if self._collapsed:
            self._collapsed = False
            self._refresh_display()

    def plain_text(self) -> str:
        content = self._body.content
        return content.plain if isinstance(content, Text) else str(content)


class HistoryInput(TextArea):
    """The prompt: Enter sends, Shift+Enter breaks the line.

    `TextArea` rather than `Input` because a pasted stack trace or a paragraph
    is a normal thing to send, and `Input` silently truncates at the first
    newline.  It grows from three rows to twelve as the draft does.
    """

    #: How many previous prompts Up/Down can reach.
    MAX_HISTORY = 256

    BINDINGS = [
        Binding("shift+enter", "newline", "Newline", show=False, priority=True),
        Binding("up", "history_previous", "Previous prompt", show=False),
        Binding("down", "history_next", "Next prompt", show=False),
    ]

    class Submitted(TextualMessage):
        """Enter was pressed with something in the box."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._history: list[str] = []
        self._index = -1
        self._draft = ""

    async def _on_key(self, event) -> None:
        """Enter submits, wherever the cursor happens to be.

        Handled here rather than as a binding because a binding would need to
        win against TextArea's own newline insertion at every cursor position;
        `prevent_default` stops that insertion, and stopping the event stops it
        reaching anything else.
        """
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text.strip()
            if text:
                # The message carries the text, so the box can be emptied here
                # rather than by the handler — which also means a slow handler
                # cannot leave the sent prompt sitting in the input, looking
                # like it is still about to be sent.
                self.post_message(self.Submitted(text))
                self.clear()
            return
        await super()._on_key(event)

    def action_newline(self) -> None:
        self.insert("\n")

    def remember(self, text: str) -> None:
        """Add a submitted prompt to the history, skipping an immediate repeat."""
        text = text.strip()
        if not text or (self._history and self._history[-1] == text):
            return
        self._history.append(text)
        del self._history[: -self.MAX_HISTORY]
        self._index = -1

    def action_history_previous(self) -> None:
        """Walk back through history, but only from the first line.

        Inside a multi-line draft, Up is a cursor movement — reaching for
        history there would make a long prompt unusable.
        """
        if self.cursor_location[0] > 0 or not self._history:
            self.action_cursor_up()
            return
        if self._index == -1:
            self._draft = self.text
        if self._index < len(self._history) - 1:
            self._index += 1
            self._show(self._history[-(self._index + 1)])

    def action_history_next(self) -> None:
        if not self._history or self._index == -1:
            self.action_cursor_down()
            return
        self._index -= 1
        self._show(
            self._draft if self._index == -1 else self._history[-(self._index + 1)]
        )

    def _show(self, text: str) -> None:
        self.text = text
        self.move_cursor(self.document.end)


class StatusBar(Static):
    """One dim row: who you are, what is happening, and what the keys do."""

    def update_status(
        self,
        *,
        connection: str,
        agent: str = "",
        model: str = "",
        busy: bool = False,
        tokens: int = 0,
        steps: int = 0,
        connected: bool = True,
    ) -> None:
        parts: list[str] = []
        if agent:
            # `[` is escaped so a name from a config cannot inject markup.
            parts.append(f"[{PALETTE['muted']}]{agent.replace('[', '[[')}[/]")
        if not connected:
            parts.append(f"[{PALETTE['red']}]{connection}[/]")
        elif busy:
            parts.append(f"[{PALETTE['amber']}]working{GLYPHS['ellipsis']}[/]")
        if model:
            parts.append(f"[{PALETTE['dim']}]{model.replace('[', '[[')}[/]")
        if tokens:
            parts.append(f"[{PALETTE['dim']}]{GLYPHS['up']} {tokens:,} tokens[/]")
        if steps:
            parts.append(
                f"[{PALETTE['dim']}]{steps} step{'s' if steps != 1 else ''}[/]"
            )
        hints = "Ctrl+C cancel  Ctrl+N new  Ctrl+Q quit"
        parts.append(f"[{PALETTE['dimmest']}]{GLYPHS['separator']} {hints}[/]")
        self.update("  ".join(parts))
