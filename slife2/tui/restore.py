"""Putting a previous conversation back on the screen.

Nothing here reads a store or decides a context.  A conversation that has just
been built is restored to its context inside `send_message` — that is the agent
server's half and it has already happened by the time a window asks — so what is
missing when a terminal starts is only the *sight* of it.  This module walks the
turns that context was built from and drives the transcript with them.

**It drives it with the live calls**, which is the whole design.  `add_user`,
`begin_assistant`, `append_thinking`, `append_text`, `add_tool_start` and
`add_tool_end` are what a turn calls as it happens, and calling them in the same
order makes a restored conversation and a live one the same thing rather than
two renderings that happen to agree today: the signature that is painted only
over text, and the fold that a step ending in a tool call gets, both come along
without being restated here.  v1 drew its rebuild from a rendering plan for the
same reason; the plan is gone and what replaced it is the transcript's own
vocabulary.

**A tool's failure is the one thing a rebuild cannot show.**  It is a fact the
loop knows only while it is running (`ToolCallFinished.ok`); what a turn stores
is the text the tool answered with, and for most tools a failure is a perfectly
ordinary sentence.  So a panel rebuilt from a record reads as done — an absence
in the record rather than a mistake made here, and worth closing in the record
rather than guessed at from the text.

Reasoning *is* stored, which is the other half of that point.  It rides the
message (`Message.thinking`) because the turn log stores messages, and each
provider adapter decides at its own edge what to do with it — so a restored step
has the reasoning block a live one had, expanded or folded by the same rule.
"""

from __future__ import annotations

import logging
from typing import Any

from slife2.context import turn_footnote
from slife2.events import preview
from slife2.messages import ToolCall
from slife2.tools import is_harness
from slife2.tui.widgets import ChatView

logger = logging.getLogger(__name__)


def restore(chat: ChatView, turns: list[dict[str, Any]]) -> None:
    """Draw *turns* — the exit-time context, oldest first — into *chat*.

    A conversation with nothing to restore draws nothing: not an empty note, and
    not a heading.  A name that is genuinely new has no history, and saying
    "restored 0 turns" would be inventing an event out of one.
    """
    if not turns:
        return

    # Suppressed for the length of the rebuild.  Every mounted widget follows
    # the tail on its own, and following during a rebuild means a scroll per
    # widget towards an end that is still moving — which is what made a restored
    # transcript jitter instead of appearing.
    chat._autoscroll = False
    with chat.app.batch_update():
        for turn in turns:
            _draw_turn(chat, turn)

    # One scroll, at the end, and following live again.  `jump_to_tail` rather
    # than `scroll_end` for the reason it exists: the re-arm is stated now, so a
    # token streamed into the refresh the scroll waits on does not find
    # following still off.
    chat._autoscroll = True
    chat.add_note(f"[restored {as_turns(len(turns))}]")
    chat.jump_to_tail()


def as_turns(count: int) -> str:
    """`3 turns`, or `1 turn` — the phrase both notes are written in.

    Shared rather than spelled twice: the restored-history note and
    the discriminator's note are the same kind of line, and a reader
    who has learned to read one should not meet `1 turns` in the
    other.
    """
    return f"{count} turn" if count == 1 else f"{count} turns"


def _draw_turn(chat: ChatView, turn: dict[str, Any]) -> None:
    """One stored turn: the question, the answers, and the work between them."""
    messages = turn.get("messages") or []
    # The results are collected first because they arrive *after* the call that
    # asked for them — a tool panel is opened by an assistant message and
    # completed by a tool message further down — so one walk cannot both open a
    # panel and know what to put in it.
    results: dict[str, str] = {}
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id"):
            results[str(message["tool_call_id"])] = _text(message.get("content"))

    # When the question was asked, and what every line of this turn is stamped
    # with.  The completed side is only the assistant's, and slife2's assistant
    # blocks carry no timestamp live — so a rebuilt one does not either, and
    # looking like the live transcript is the rule that settles it.
    asked = turn.get("created_at") or None
    # The turn's id, channel and span, shown beside the words that opened it.  It
    # is the *same* footnote `messages_from_turns` wraps in `[TURN: …]` for the
    # model — one function, two readers — and that is the point of showing it at
    # all: the ids a keep-list is written with are then visible to the person
    # watching, instead of being a number only the model ever sees.
    #
    # No `[TURN: ]` envelope here: that is the machine's marker, and a screen
    # renders the payload (`turn_footnote`), the way v1's does.
    footnote = turn_footnote(
        int(turn.get("turn_id") or 0),
        str(turn.get("created_at") or ""),
        turn.get("completed_at"),
        str(turn.get("channel") or ""),
    )

    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "user":
            if message.get("content") is None:
                continue
            # The footnote rides `messages[0]` — the message that *opens* the
            # turn — which is the same message `messages_from_turns` appends it
            # to.  Keyed by position rather than by role so the two builders
            # cannot disagree about which message it belongs to.
            chat.add_user(
                _text(message.get("content")),
                moment=asked,
                footnote=footnote if index == 0 else "",
            )
        elif role == "assistant":
            _draw_answer(chat, message, results)


def _draw_answer(
    chat: ChatView, message: dict[str, Any], results: dict[str, str]
) -> None:
    """One assistant message — its reasoning, its answer, its tool panels."""
    calls = [
        ToolCall.from_wire(raw)
        for raw in message.get("tool_calls") or []
        if _drawn(raw)
    ]
    answer = _text(message.get("content"))
    thinking = _text(message.get("thinking"))

    # Opened only when there is something to open it *for*.  A step that says
    # nothing and thought nothing is a message the live transcript could not have
    # shown either: nothing streamed into it, so no block was ever opened — and
    # opening one here would leave a row holding a lone `…`.
    #
    # The order is the live turn's, and that is what carries the fold: a tool
    # call collapses the reasoning of the block it closes, so
    # `add_tool_start` below folds this step's thinking by itself, and the last
    # step's stays open because no call follows it.  Nothing restates that rule.
    if thinking or answer:
        chat.begin_assistant()
        if thinking:
            chat.append_thinking(thinking)
        if answer:
            chat.append_text(answer)

    for call in calls:
        _draw_tool(chat, call, results.get(call.id, ""))


def _draw_tool(chat: ChatView, call: ToolCall, result: str) -> None:
    """A tool panel, opened and completed — the two halves of a live call.

    The result is shown through `preview`, exactly as a live one is: the wire
    caps what a panel displays (`slife2/events.PREVIEW_CHARS`) so that a tool
    returning 100 KB is not duplicated into every widget and log line, and the
    count carried beside it is the *whole* result's.  Passing the stored text
    through unshaped would make a restored panel the one place a tool's entire
    output is rendered — and make it unlike the same call seen live.
    """
    chat.add_tool_start(call.id, call.name, call.arguments)
    chat.add_tool_end(
        call.id,
        # See the module docstring: a failure is not in the record, so a rebuilt
        # panel cannot know it was one.
        True,
        preview(result),
        len(result),
        # And neither is how long it took.  Zero is the widget's "nobody said",
        # which leaves the duration off the header rather than claiming none.
        0,
    )


def _drawn(raw: dict[str, Any]) -> bool:
    """Whether a stored call is one the transcript draws.

    **Today: only the model's own calls.**  The harness tools
    (`slife2.tools`'s word, and the mark is `is_harness`) are in the record — the
    model's list carries them, which is what makes the pair legal — and nothing
    draws them, live or rebuilt.  Live because the auto-invoke raises no event;
    here because of this filter.

    **The two are hidden for different reasons, and only one of them is a
    decision.**  `_func_tool_unload` is bookkeeping: its result is a sentence
    about the model's own tool list, and a panel for it would be noise.
    `_check_new_input` carries something a *person* said — DESIGN.md §9 defers
    drawing it.  So what is missing there is not a line to delete but a widget: a
    line that shows a user message **without closing the open turn**, since
    `ChatView.add_user` sets `_turn_open = False` and would drop the rest of the
    answer being streamed.  Deleting this filter alone would draw it as a tool
    panel, which is the one thing it is not.
    """
    function = raw.get("function") or {}
    return not is_harness(str(function.get("name") or ""))


def _text(content: Any) -> str:
    """A message's text, from either spelling of it.

    `content` is a string or a list of content parts — the OpenAI shape, which
    is what a message carrying an image is — so the parts are read rather than
    the list being rendered.  Images themselves are not drawn: the transcript
    shows what was *said*, and `@path` in the prompt is where an attachment is
    named.
    """
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content) if content else ""
