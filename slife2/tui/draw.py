"""Turning one turn event into one line on a transcript.

**One vocabulary, two readers.**  The window that *runs* a turn and the screen
that *watches* somebody else's both draw through this function, so a watched
turn and a turn the person typed cannot come to look different: every widget
call here is the one the live transcript has always made
(`slife2.tui.restore` is the same argument for the history half).

Three events are deliberately not drawn here, because they are not display in
the same sense:

* **`ToolApprovalRequested`** is a question, and the only party who can answer it
  is the turn's own caller (`Pending.watched`).  The window draws the prompt and
  answers it; a watcher is never asked, because the turn it is watching runs
  unattended by design (`slife2.plugins.a2a.server.run_turn`).
* **`TurnFinished`** carries the authoritative answer and the turn's bill.  The
  window reconciles the first with what it streamed and puts the second in the
  status bar; a watched screen closes its turn with the same text and has no bar.
* **`ToolsCounted`** is the model's list against the catalogue's size, and
  **`UsageReported`** is how full the context is after one model call.  Both are
  the window's status bar; a watched screen has no bar, and drawing either would
  put a fact about the window's chrome in somebody's transcript.

`False` is the whole of what says "this one is yours"; anything left unmatched
is a vocabulary this build was not taught, and `assert_never` turns that into a
static error at the next variant rather than a line nobody draws.
"""

from __future__ import annotations

from typing import assert_never

from slife2.libs.harness import notes
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
    UsageReported,
)
from slife2.libs.turn.tools import CHECK_NEW_INPUT, is_harness, is_harness_call
from slife2.tui.widgets import ChatView


def draw(chat: ChatView, event: TurnEvent) -> bool:
    """Draw the display half of *event*; answer whether it was drawn.

    `False` means the event belongs to the view that owns the turn — see the
    module docstring.  A caller that ignores the answer is a caller that has
    decided it does not own any of them, which is exactly what a watcher is.
    """
    match event:
        case ToolCallFinished(call_id=call_id, name=name, result_preview=text) if (
            is_harness(name)
        ):
            # **A pair the machinery wrote is a line the person can see.**  The
            # machinery's own sentence is a note — the compaction's count, the
            # trim's list of what left.  A model's own call to the same tool
            # keeps its own call id and is not a note: the answer is the
            # model's.  `_check_new_input`'s note is the `MessageCutIn` line —
            # the message it took — rather than this finish.
            if is_harness_call(call_id) and name != CHECK_NEW_INPUT and text:
                chat.add_note(text)
            return True
        case ToolCallStarted(name=name) if is_harness(name):
            # **The harness's own pairs are not tool panels.**  The answer is
            # the line (above), and a panel would be a second one for a call the
            # model did not make.
            return True
        case MessageCutIn(text=text):
            # **A cut-in is a harness note like every other pair's answer.**  The
            # person's words are the `_check_new_input` tool's own answer, so the
            # line is the note rather than a bubble; the channel stays in the
            # record and in the event (the receipt's reader is the window that
            # submitted it).
            chat.add_note(text, new_call=True)
        case ContextChosen(
            kept=kept, recalled=recalled, rerank=rerank, discriminator=discriminator
        ):
            # The discriminator's decision, which is the only sight of it: the
            # model call it took is one the conversation never shows.  The
            # rerank status rides the same event because it is the same
            # decision's last stage; the view says it once per change.  A
            # discriminator that decided nothing turns the recall count into
            # the word — `recalled 0` is the count of a search nobody made.
            chat.add_note(notes.kept(kept, recalled, failed=bool(discriminator)))
            chat.note_rerank(rerank)
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
        case (
            ToolApprovalRequested() | ToolsCounted() | UsageReported() | TurnFinished()
        ):
            return False
        case _:
            assert_never(event)
    return True
