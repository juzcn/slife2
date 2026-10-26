"""Turning one turn event into one line on a transcript.

**One vocabulary, two readers.**  The window that *runs* a turn and the screen
that *watches* somebody else's both draw through this function, so a watched
turn and a turn the person typed cannot come to look different: every widget
call here is the one the live transcript has always made
(`slife2.tui.restore` is the same argument for the history half).

Two events are deliberately not drawn here, because they are not display in the
same sense:

* **`ToolApprovalRequested`** is a question, and the only party who can answer it
  is the turn's own caller (`Pending.watched`).  The window draws the prompt and
  answers it; a watcher is never asked, because the turn it is watching runs
  unattended by design (`slife2.plugins.a2a.server.run_turn`).
* **`TurnFinished`** carries the authoritative answer and the turn's bill.  The
  window reconciles the first with what it streamed and puts the second in the
  status bar; a watched screen closes its turn with the same text and has no bar.
* **`ToolsCounted`** is the model's list against the catalogue's size, which is
  the same kind of fact: the window's status bar shows it, and a watched screen
  has no bar to put it in.  Drawing it would put a line about the tool set in
  somebody's transcript.

`False` is the whole of what says "this one is yours"; anything left unmatched
is a vocabulary this build was not taught, and `assert_never` turns that into a
static error at the next variant rather than a line nobody draws.
"""

from __future__ import annotations

from typing import assert_never

from slife2.libs.turn.events import (
    ContextChosen,
    MessageCutIn,
    TextDelta,
    ThinkingDelta,
    ToolApprovalRequested,
    ToolCallFinished,
    ToolCallStarted,
    ToolsCounted,
    TurnEvent,
    TurnFinished,
)
from slife2.libs.turn.tools import is_harness
from slife2.tui import notes
from slife2.tui.widgets import ChatView


def draw(chat: ChatView, event: TurnEvent) -> bool:
    """Draw the display half of *event*; answer whether it was drawn.

    `False` means the event belongs to the view that owns the turn — see the
    module docstring.  A caller that ignores the answer is a caller that has
    decided it does not own any of them, which is exactly what a watcher is.
    """
    match event:
        case ToolCallStarted(name=name) | ToolCallFinished(name=name) if is_harness(
            name
        ):
            # **The harness's own pairs are not tool panels.**  `_check_new_input`
            # is drawn as the cut-in its `MessageCutIn` event carries, and
            # `_func_tool_unload` is the budget talking to itself; a panel for
            # either would be a second line for a call the model did not make.
            # A rebuilt transcript has always skipped them (`restore._draw_answer`);
            # this is the live half of the same rule.
            return True
        case MessageCutIn(text=text, channel=channel):
            # A message that jumped the queue, drawn inside the turn that took
            # it: the line belongs here and the turn stays open.
            chat.add_cut_in(text, channel=channel)
        case ContextChosen(kept=kept, recalled=recalled):
            # The discriminator's decision, which is the only sight of it: the
            # model call it took is one the conversation never shows.
            chat.add_note(notes.kept(kept, recalled))
        case TextDelta(text=text):
            chat.append_text(text)
        case ThinkingDelta(text=text):
            chat.append_thinking(text)
        case ToolCallStarted(
            call_id=call_id,
            name=name,
            arguments=arguments,
            iteration=iteration,
            max_iterations=max_iterations,
        ):
            chat.add_tool_start(
                call_id,
                name,
                arguments,
                iteration=iteration,
                max_iterations=max_iterations,
            )
        case ToolCallFinished(
            call_id=call_id,
            ok=ok,
            result_preview=preview_text,
            result_chars=chars,
            elapsed_ms=elapsed,
        ):
            chat.add_tool_end(call_id, ok, preview_text, chars, elapsed)
        case ToolApprovalRequested() | ToolsCounted() | TurnFinished():
            return False
        case _:
            assert_never(event)
    return True
