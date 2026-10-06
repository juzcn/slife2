"""The three widgets the TUI is made of.

`Transcript` is the interesting one.  Streaming means rewriting the *last* block
on every delta, which is why it is a `VerticalScroll` holding widgets rather
than a `RichLog`: `RichLog` appends whole lines, so writing one line per delta
would render a separate block per token.  Its auto-scroll is the only thing it
would have given us, and that is one call.
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message as TextualMessage
from textual.widgets import Static, TextArea


class Transcript(VerticalScroll):
    """The scrolling conversation.

    Text is handed to Rich as `Text` objects rather than markup strings: model
    output routinely contains square brackets, and a string would have them
    parsed as style tags — at best mangling the answer, at worst raising.
    """

    def __init__(self) -> None:
        super().__init__(id="transcript")
        #: The block currently accepting deltas, or None between blocks.
        #: None happens mid-turn too — a tool row interrupts the text, and the
        #: next delta opens a fresh block.
        self._streaming: Static | None = None
        self._streaming_text = ""
        #: Whether a turn is still accepting deltas at all.  Tracked separately
        #: from `_streaming` because the two answer different questions, and
        #: conflating them is a bug: see `append_text`.
        self._turn_open = False

    def _add(self, block: Text, classes: str = "") -> Static:
        widget = Static(block, classes=classes)
        self.mount(widget)
        self.scroll_end(animate=False)
        return widget

    def _close_block(self) -> None:
        self._streaming = None
        self._streaming_text = ""

    def add_user(self, text: str) -> None:
        self._turn_open = False
        self._close_block()
        self._add(Text.assemble(("you> ", "bold cyan"), (text, "")))

    def begin_assistant(self) -> None:
        """Open a turn: from here until :meth:`finish_assistant`, deltas land."""
        self._close_block()
        self._turn_open = True
        self._streaming = self._add(Text(""))

    def append_text(self, delta: str) -> None:
        """Add a delta to the current block, if a turn is still open.

        Deltas that arrive after the turn's result are **dropped**, and that is
        load-bearing rather than defensive.  Progress events reach the UI as
        queued messages while the result comes back on the awaited call, so a
        delta can be handled after `finish_assistant` has already run.  Letting
        it reopen a block would append a stale fragment after the final answer —
        which is exactly the failure the "the result is authoritative" rule
        exists to prevent.
        """
        if not self._turn_open:
            return
        if self._streaming is None:
            # Text resumed after a tool row: a new block, same turn.
            self._streaming_text = ""
            self._streaming = self._add(Text(""))
        self._streaming_text += delta
        self._streaming.update(Text(self._streaming_text))
        self.scroll_end(animate=False)

    def finish_assistant(self, text: str) -> None:
        """Close the turn with the authoritative answer.

        Not an optimisation — a correction.  The deltas are a display channel
        and may be incomplete or late; the return value of the tool call is the
        answer, and this is where the two are reconciled.
        """
        self._turn_open = False
        if self._streaming is None:
            self._streaming = self._add(Text(""))
        self._streaming_text = text
        self._streaming.update(Text(text))
        self._close_block()
        self.scroll_end(animate=False)

    def add_tool_start(self, name: str) -> None:
        # Closes the current text block but not the turn: text may resume after.
        self._close_block()
        self._add(Text(f"  -> {name}()", style="dim"))

    def add_tool_end(self, ok: bool, result_preview: str) -> None:
        marker = "<-" if ok else "!!"
        style = "dim" if ok else "dim red"
        self._add(Text(f"  {marker} {result_preview}", style=style))

    def clear_all(self) -> None:
        """Empty the transcript.  Pairs with starting a new conversation."""
        self._turn_open = False
        self._close_block()
        for widget in list(self.query(Static)):
            widget.remove()

    def add_note(self, text: str) -> None:
        self._close_block()
        self._add(Text(text, style="yellow"))

    def add_error(self, text: str) -> None:
        self._turn_open = False
        self._close_block()
        self._add(Text(f"error: {text}", style="bold red"))

    def plain_text(self) -> str:
        """Everything shown, as plain text.

        Reads the widgets rather than a private mirror of what was appended, so
        a test asserting on this is asserting on what the user sees.
        """
        return "\n".join(
            widget.content.plain
            if isinstance(widget.content, Text)
            else str(widget.content)
            for widget in self.query(Static)
        )


class PromptInput(TextArea):
    """A multiline prompt box where Enter sends and Shift+Enter breaks the line.

    `TextArea` rather than `Input` because a pasted stack trace or a paragraph
    is a normal thing to send, and a single-line box makes it unusable.
    """

    BINDINGS = [
        # `priority=True` is what makes these win over TextArea's own key
        # handling, which would otherwise insert a newline on Enter.
        Binding("enter", "submit", "Send", show=False, priority=True),
        Binding("shift+enter", "newline", "Newline", show=False, priority=True),
    ]

    class Submitted(TextualMessage):
        """Enter was pressed with something in the box."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    def action_submit(self) -> None:
        text = self.text.strip()
        if not text:
            return
        self.post_message(self.Submitted(text))
        self.clear()

    def action_newline(self) -> None:
        self.insert("\n")


class StatusBar(Static):
    """One line: connection, model, and what the last turn cost."""

    def update_status(
        self,
        *,
        connection: str,
        model: str = "",
        busy: bool = False,
        tokens: int = 0,
        steps: int = 0,
    ) -> None:
        parts = ["[bold]slife2[/bold]", connection]
        if model:
            parts.append(model)
        if busy:
            parts.append("working...")
        if steps:
            parts.append(f"{steps} step{'s' if steps != 1 else ''}")
        if tokens:
            parts.append(f"{tokens} tokens")
        self.update("  |  ".join(parts))
