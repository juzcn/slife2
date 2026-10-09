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

**Following the tail is decided where a scroll is ASKED FOR**, not from the
offset — see :meth:`ChatView._scroll_to`.  Textual re-clamps `scroll_y` by
itself whenever a size change leaves it past the end, and reading that
correction as intent re-arms following under a reader who is up in the history:
the next streamed token then pulls the page out from under them.

**And it is re-armed when the content's height changes** — see
:meth:`ChatView._size_updated`.  The end of the content is settled a frame after
the text that moved it, so the follow a burst of deltas asks for is aimed at a
height that is already out of date by the time it lands.

**The agent's `name>` is painted only where there is text under it.**  A block
is opened by whatever has something to put in it — the first delta, a resumed
stream after a tool panel, the final answer — so a signature painted
unconditionally leaves a bare `slife2>` row in the transcript for every step
that has only reasoning to show.  slife v1 signs the message, not the widget.
"""

from __future__ import annotations

from datetime import datetime
from typing import TypeVar

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.geometry import Size
from textual.message import Message as TextualMessage
from textual.widget import Widget
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


def _timestamp(when: datetime | str | None = None) -> str:
    """`HH:MM`, or a longer form once the day is no longer today.

    Accepts the ISO-8601 string a turn records its own time as, so a restored
    message can carry the moment it was sent rather than the moment the window
    was opened — which is the whole difference between a transcript that reads
    as a conversation and one that reads as a wall of text stamped now.
    """
    now = datetime.now()
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            when = None
    moment = when or now
    if moment.date() == now.date():
        return moment.strftime("%H:%M")
    if moment.year == now.year:
        return moment.strftime("%m-%d %H:%M")
    return moment.strftime("%Y-%m-%d %H:%M")


def _duration(ms: int) -> str:
    """How long a tool took, in the unit a person reads it in.

    `ms` under a second and seconds above it, because the number that matters
    changes with the magnitude: 800ms is a fact worth having, 12.4s is, and
    12,400ms is the same fact spelled in a way nobody converts in their head.
    """
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


def plain(text: str, style: str = "") -> Text:
    """Model, tool or user text as a renderable it cannot style itself with."""
    return Text(text, style=style)


async def redirect_printable(widget: Widget, event: events.Key) -> bool:
    """Forward a printable key to the prompt; `True` when it was taken.

    The transcript's widgets are focusable — that is what gives a message Enter
    and Space to unfold its reasoning, and a tool panel the same to open — but a
    click lands focus on one of them, and after that typing has to reach the
    prompt anyway.  Letters and punctuation are what a person types at; the keys
    those widgets were made focusable for are theirs.
    """
    # Space is the one printable key that is *also* a binding, and the widget's
    # own `_on_key` runs before the binding chain is consulted — so taking it
    # here would type a space into the prompt instead of unfolding the reasoning
    # the message was made focusable for.
    if not event.is_printable or event.key == "space":
        return False
    prompt = widget.screen.query_one_optional(HistoryInput)
    if prompt is None or prompt.has_focus:
        return False
    prompt.focus()
    await prompt._on_key(event)
    event.stop()
    return True


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
        #: Whether mounting a widget follows the tail.  On for a live turn;
        #: restore turns it off, mounts the whole history and scrolls exactly
        #: once at the end — a scroll per widget is what made a rebuild jitter.
        self._autoscroll = True

    # --- following the tail --------------------------------------------------

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Repaint at the new offset — and deliberately nothing else.

        This overrides `Widget.watch_scroll_y`, which is what repaints the
        widget at the new offset: dropping the delegation left the reader
        scrolling an image that never moved.

        Following is **not** decided here, and that is the whole point of the
        method being this short.  The offset is not the reader's alone: Textual
        re-clamps `scroll_y` whenever a size change leaves it past the end, and
        that assignment arrives through this watcher exactly like a keypress
        does.  Reading it as intent re-armed following under a reader who was
        reading history — the prompt box growing a line was enough, and the next
        streamed token then pulled them to the bottom.  The decision is made
        where the request is made; see :meth:`_scroll_to`.
        """
        super().watch_scroll_y(old_value, new_value)

    def _scroll_to(self, x=None, y=None, **kwargs) -> bool:
        """Following is decided here, from the REQUEST — the one place.

        Every scroll Textual can make funnels through this method: the arrow
        keys, PageUp/PageDown, Home/End, the wheel, the scrollbar, and our own
        `scroll_end`.  So one comparison covers every mover, including one added
        later — which is what a per-method override could never promise, since
        each new way to move the view would have to remember to state the
        intent, and the one that forgot re-armed following under the reader.

        `scroll_target_y` is the clamped request — the value the offset will
        land on — so aiming at or past the end means "follow from here" and
        anything else means the reader is reading history.  A move nothing
        requested never reaches this method, which is the point.

        *x* is the horizontal half of the framework signature: this widget
        scrolls one axis, and a request without a *y* says nothing about
        following.
        """
        result = super()._scroll_to(x, y, **kwargs)
        if y is not None:
            self._at_tail = self.scroll_target_y >= self.max_scroll_y
        return result

    def _size_updated(
        self,
        size: Size,
        virtual_size: Size,
        container_size: Size,
        layout: bool = True,
    ) -> bool:
        """The content grew or shrank: follow it, if the reader is at the tail.

        Following is armed by whatever *changes* the content, but the tail is a
        property of the content's *height*, and the height is settled a frame
        later — in the layout pass this method is part of.  A burst of deltas
        that arrives inside one frame therefore asks to follow the height the
        content had a frame ago; every one of those requests lands short, and
        once the layout has moved past them nothing asks again.  The end of the
        answer then sits below the fold until something else scrolls the view.

        Following on the size is what closes that gap, and it is also the
        cheapest place to do it: this is called on every layout pass, so the
        guard is the change itself rather than the call.
        """
        before = self.virtual_size
        resized = super()._size_updated(size, virtual_size, container_size, layout)
        if self.virtual_size != before:
            self.follow_tail()
        return resized

    def follow_tail(self) -> None:
        """Follow new content, unless following is off or the reader has gone
        back to reading.

        Every streamed token and every mounted widget lands here, so the guards
        have to be here too: without the second, each token scrolls back to the
        end and paging up during a turn is impossible; without the first, a
        rebuild scrolls once per widget.
        """
        if self._autoscroll and self._at_tail:
            self.scroll_end(animate=False)

    def jump_to_tail(self) -> None:
        """Return to the tail and follow from there — the reader's own send.

        The re-arm is stated here as well as by the request, and that is not
        redundant: `scroll_end` makes its request a refresh later, and a token
        streamed inside that window would find following still off.  The intent
        is known now, so it is recorded now.
        """
        self._at_tail = True
        self.scroll_end(animate=False)

    async def _on_key(self, event: events.Key) -> None:
        """Redirect printable keys to the prompt — see `redirect_printable`."""
        if not await redirect_printable(self, event):
            await super()._on_key(event)

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

    def add_user(
        self,
        text: str,
        moment: datetime | str | None = None,
        footnote: str = "",
    ) -> None:
        """`[HH:MM] You> text`, the timestamp dim and the prefix bold amber.

        `moment` is when the message was *sent*, and defaults to now.  Restore
        is the only caller that passes one, and it is what keeps a rebuilt
        transcript honest: a conversation from yesterday that says every line
        arrived at the second the window opened is worse than no timestamps.

        `footnote` is the turn's id, channel and span, and restore is its only
        caller too — a live message has no turn yet to name.  It is drawn as its
        own span rather than found in the text and restyled, which is what keeps
        this widget free of the escaping rules a marker needs: the text a person
        typed is one piece and the metadata is another, and neither has to be
        parsed back out of a string.  v1 reaches the same picture from the other
        side, wrapping the footnote in `[INFO: …]` for the model and unstyling it
        again for the screen.
        """
        self._turn_open = False
        self._close_block()
        line = Text.assemble(
            (f"[{_timestamp(moment)}] ", f"dim {PALETTE['dim']}"),
            ("You> ", f"bold {PALETTE['amber-bold']}"),
            (text, PALETTE["text"]),
        )
        if footnote:
            # Inline and after the words, in the style reasoning gets: dim and
            # italic, so it reads as a note about the message rather than as
            # part of it — the distinction the styling carries, not a bracket.
            line.append(f" {footnote}", f"italic {PALETTE['dim']}")
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
        """Open a turn: from here until `finish_assistant`, deltas land.

        **No block yet, because nothing has been said.**  One used to be opened
        here — "before anything is known about it" — and *anything* that wrote a
        line first closed it again: `add_note` closes the block it is given, so a
        note arriving before the first delta left an empty assistant block in the
        transcript, a blank gap between the prompt and the answer.  The
        discriminator's note is such a line, and so is `[cancelled]` on a turn
        that streamed nothing before it was interrupted.

        Text, reasoning and the final answer each open the block they need —
        which `append_text` was already doing for the case after a tool panel —
        so a turn with nothing to say leaves nothing behind.
        """
        self._close_block()
        self._turn_open = True

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
        # `follow_tail`, not `jump_to_tail`: ending a turn is not a reason to
        # yank the viewport off somebody who scrolled up to read — which is the
        # whole case the `_at_tail` machinery exists for.  It follows when the
        # view is at the tail, and leaves it alone when it is not.
        self.follow_tail()

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
        self,
        call_id: str,
        ok: bool,
        result_preview: str,
        result_chars: int,
        elapsed_ms: int,
    ) -> None:
        widget = self._tools.get(call_id)
        if widget is None:  # pragma: no cover - a result without a start
            return
        # `ok` is the positive phrasing and `set_complete` wants the negative
        # one.  Naming both ends after the same thing is how these get swapped;
        # a test that asserts a success says "done" is what keeps them honest.
        widget.set_complete(
            result_preview,
            is_error=not ok,
            result_chars=result_chars,
            elapsed_ms=elapsed_ms,
        )
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
        # Painted once at construction, so an empty block shows the agent's
        # signature while the first delta is still in flight.  Without this the
        # block renders nothing at all until text arrives, and the signature
        # that says who is answering is the only affordance there is.
        self._refresh()

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
        if self._text:
            if self._thinking:
                line.append("\n")
            # The same treatment as the user's `You> `: a signature in bold
            # amber, so the two sides of a conversation are marked the same way
            # and the agent's name is visible where it is being used.
            #
            # Signed only where there is text to sign.  A block is opened before
            # anything is known about it — at `begin_assistant`, and again after
            # each tool panel when reasoning or text resumes — so a signature
            # painted at construction leaves a bare `slife2>` row in the
            # transcript for every step that has only reasoning to show: one per
            # tool call, on a reasoning model.  slife v1 signs the message, not
            # the widget.
            if self._agent:
                line.append(f"{self._agent}> ", f"bold {PALETTE['amber-bold']}")
            line.append(self._text, PALETTE["text"])
        elif not self._thinking:
            # Nothing at all yet: a dim ellipsis is the only affordance that
            # says "working", and dim so it never reads as content.
            line.append(GLYPHS["ellipsis"], PALETTE["dimmest"])
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
        #: Wall time the call took, from the event.  Zero while it is running,
        #: and zero for a call whose duration nobody reported.
        self._elapsed_ms = 0
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
        if self._elapsed_ms:
            # How long it took, in the collapsed header rather than in the
            # detail: a tool that hung for four seconds is worth seeing without
            # expanding anything, and the duration is the one thing about a
            # finished call that the result text does not say.
            line.append(
                f" {GLYPHS['ellipsis']} {_duration(self._elapsed_ms)}",
                PALETTE["dimmest"],
            )
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
                line.append("\n" + self._result, PALETTE["text-secondary"])
                # What the tool actually returned, when that is more than the
                # preview shows.  A line-count guard used to stand here,
                # trimming to `MAX_RESULT_LINES` and saying how many were left
                # — and it could never fire: the result on the wire is capped at
                # `events.PREVIEW_CHARS` (two hundred), which cannot make five
                # hundred lines however it is spelled.  What is worth saying is
                # the size, which is the number carried beside the preview for
                # exactly this and was read by nothing else.
                if self._result_chars > len(self._result):
                    line.append(
                        f"\n{GLYPHS['ellipsis']} {self._result_chars:,} characters "
                        f"in all",
                        PALETTE["dimmest"],
                    )
        return line

    def set_complete(
        self, result: str, is_error: bool, result_chars: int, elapsed_ms: int
    ) -> None:
        self._result = result
        self._is_error = is_error
        self._result_chars = result_chars
        self._elapsed_ms = elapsed_ms
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
        # TextArea claims PageUp/PageDown to page through its *own* text, and
        # for a chat prompt that is the wrong target: the draft is three rows
        # while the transcript above is what the reader is paging.  The binding
        # is what takes the key back — a binding here beats the one inherited
        # from TextArea — and the action forwards it to the transcript.
        Binding("pageup", "transcript_page_up", show=False),
        Binding("pagedown", "transcript_page_down", show=False),
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

    # --- paging the transcript, which the prompt sits under ------------------

    def _transcript(self) -> ChatView | None:
        """The chat view, or None when there is no transcript to page.

        `HistoryInput` is a `TextArea` and can be mounted on its own, where the
        keys above have nothing to forward to.
        """
        return self.screen.query_one_optional(ChatView)

    def action_transcript_page_up(self) -> None:
        """Page the transcript up — the key belongs to it, not to the draft."""
        transcript = self._transcript()
        if transcript is not None:
            transcript.scroll_page_up(animate=False)

    def action_transcript_page_down(self) -> None:
        """The mirror of :meth:`action_transcript_page_up`."""
        transcript = self._transcript()
        if transcript is not None:
            transcript.scroll_page_down(animate=False)

    # The wheel over this box is the same case as PageUp/PageDown above, and it
    # is how the transcript is normally read: Textual delivers a tick to
    # whatever is under the pointer, and the pointer sits here after typing.
    # The draft has nothing to scroll — `overflow` is `hidden` on the prompt, so
    # it follows the cursor rather than a wheel — and the base handler neither
    # moved it nor stopped the tick, leaving the tick to bubble to a Screen with
    # no scroll to give it.  `_scroll_*_for_pointer` is still asked first, so a
    # stylesheet that ever made the draft scrollable would not have this
    # override steal every tick from it.

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        """Forward a wheel tick the draft cannot use to the transcript."""
        if event.ctrl or event.shift:
            super()._on_mouse_scroll_up(event)  # horizontal — not ours
            return
        if not self._scroll_up_for_pointer(animate=False):
            self._wheel_transcript(up=True)
        event.stop()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        """The mirror of :meth:`_on_mouse_scroll_up`."""
        if event.ctrl or event.shift:
            super()._on_mouse_scroll_down(event)
            return
        if not self._scroll_down_for_pointer(animate=False):
            self._wheel_transcript(up=False)
        event.stop()

    def _wheel_transcript(self, *, up: bool) -> None:
        """Step the transcript by one wheel notch.

        Measured from the offset, not from `scroll_target_y`: a token's follow
        is aimed at the end and may still be on its way there, so a notch taken
        from the target would jump the reader to the end minus one line instead
        of stepping from where they are looking.  `immediate=True` is the same
        point — the notch lands now, and stating the intent now is what stops
        the next token from following it back down.
        """
        transcript = self._transcript()
        if transcript is None:
            return  # nothing to scroll
        step = self.app.scroll_sensitivity_y
        transcript.scroll_to(
            y=transcript.scroll_y - step if up else transcript.scroll_y + step,
            animate=False,
            immediate=True,
        )

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
        queued: int = 0,
        context_tokens: int = 0,
        context_window: int = 0,
        thinking: bool = False,
        vision: bool = False,
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
        # Messages the user has sent that have not had their turn yet.  Shown
        # because they are deliberately *not* in the transcript until they run:
        # a queued message appearing above the answer still being written would
        # misreport the order the turns actually happened in.
        if queued:
            parts.append(f"[{PALETTE['dim']}]{queued} queued[/]")
        if model:
            parts.append(f"[{PALETTE['dim']}]{model.replace('[', '[[')}[/]")

        # What the model *is*, as opposed to what it is doing — which is why
        # these are dim words beside its name rather than coloured states:
        # `amber` means a turn is running, and a capability that was amber
        # whenever the model happened to reason would read as an activity.
        #
        # Both are worth the space.  Reasoning explains the pause before an
        # answer and the block above it, and vision is the one thing a person
        # has to know *before* typing — `@picture.png` is either going to work
        # or be refused, and the config already knows which.
        if thinking:
            parts.append(f"[{PALETTE['dim']}]{GLYPHS['thinking']} thinking[/]")
        if vision:
            parts.append(f"[{PALETTE['dim']}]{GLYPHS['vision']} vision[/]")

        # The *last* turn's tokens, not a running total: what a context
        # percentage is a percentage of is the conversation as it now stands,
        # and a sum over every turn is a number that only ever grows.
        #
        # Shown from zero when the model has a window, rather than appearing
        # with the first answer: an indicator that is absent until it has
        # something to say is one nobody knows is there, and the window is a
        # fact about the model rather than about the turn.
        if context_window:
            used = context_tokens / context_window * 100
            colour = PALETTE["amber"] if used >= 80 else PALETTE["dim"]
            parts.append(
                f"[{colour}]{GLYPHS['up']} {context_tokens:,} ({used:.1f}%)[/]"
            )
        elif context_tokens:
            # No window is configured for this model, and inventing one would
            # render a percentage that means nothing.
            parts.append(
                f"[{PALETTE['dim']}]{GLYPHS['up']} {context_tokens:,} tokens[/]"
            )
        if steps:
            parts.append(
                f"[{PALETTE['dim']}]{steps} step{'s' if steps != 1 else ''}[/]"
            )
        # Both keys, because they are not interchangeable: the copy actions get
        # first refusal on ctrl+c, and escape is the one that stops the turn and
        # cannot do anything else — nothing else claims it, and it never quits.
        hints = "Esc/Ctrl+C cancel  Ctrl+N new  Ctrl+Q quit"
        parts.append(f"[{PALETTE['dimmest']}]{GLYPHS['separator']} {hints}[/]")
        self.update("  ".join(parts))
