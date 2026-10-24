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
over text, and the fold that an iteration ending in a tool call gets, both come
along without being restated here.  A rendering plan would be a second
vocabulary to keep in step with the transcript's own; driving the rebuild with
the live calls is what replaces it.

**A tool's failure is the one thing a rebuild cannot show.**  It is a fact the
loop knows only while it is running (`ToolCallFinished.ok`); what a turn stores
is the text the tool answered with, and for most tools a failure is a perfectly
ordinary sentence.  So a panel rebuilt from a record reads as done — an absence
in the record rather than a mistake made here, and worth closing in the record
rather than guessed at from the text.

Reasoning *is* stored, which is the other half of that point.  It rides the
message (`Message.thinking`) because the turn log stores messages, and each
provider adapter decides at its own edge what to do with it — so a restored
iteration has the reasoning block a live one had, expanded or folded by the same
rule.
"""

from __future__ import annotations

import logging
from typing import Any

from slife2.libs.turn.attachments import IMAGE_PREFIX
from slife2.libs.turn.context import turn_footnote
from slife2.libs.turn.events import preview
from slife2.libs.turn.messages import ToolCall
from slife2.libs.turn.tools import CHECK_NEW_INPUT, is_harness
from slife2.tui import notes
from slife2.tui.i18n import t
from slife2.tui.widgets import WINDOW_CHANNEL, ChatView

logger = logging.getLogger(__name__)


def restore(chat: ChatView, turns: list[dict[str, Any]]) -> None:
    """Draw *turns* — the exit-time context, oldest first — into *chat*.

    A conversation with nothing to restore draws nothing: not an empty note, and
    not a heading.  A name that is genuinely new has no history, and saying
    "restored 0 turns" would be inventing an event out of one.

    **A row the screen cannot read is dropped, not fatal — and said twice.**
    `restore` is awaited before a submission's turn is drawn
    (`slife2.tui.app._drain`), so an exception out of here does not merely lose
    the history — it kills the worker that runs turns, leaves the window saying
    "working" with the message still counted as queued, and never runs or shows
    it.  The record is not this module's to trust: `_draw_turn` reads a stored
    row field by field (an id it parses, a tool call it decodes), and a row
    written by an older build or a half-finished one can carry a shape none of
    that expects.  So the failure is contained to the one turn it came from,
    said in the log where the store it contradicts can be looked at, *and* drawn
    as an error line — because the note below counts what was drawn, and a drop
    nobody is told about would make `[restored N turns]` read as the whole
    history having come back.
    """
    if not turns:
        return

    drawn = 0
    # Suppressed for the length of the rebuild.  Every mounted widget follows
    # the tail on its own, and following during a rebuild means a scroll per
    # widget towards an end that is still moving — which is what made a restored
    # transcript jitter instead of appearing.
    chat._autoscroll = False
    with chat.app.batch_update():
        for turn in turns:
            try:
                _draw_turn(chat, turn)
            except Exception:  # noqa: BLE001 - a stored row is not ours to trust
                # Named on the terminal, not only in the log: the count below is
                # what came back, and the row that did not is the difference a
                # reader has to be able to account for.  The row itself stays in
                # the log line, where the store it contradicts can be looked at.
                chat.add_error(t("turn_unreadable"))
                logger.warning(
                    "a stored turn could not be drawn: %r", turn, exc_info=True
                )
            else:
                drawn += 1

    # One scroll, at the end, and following live again.  `jump_to_tail` rather
    # than `scroll_end` for the reason it exists: the re-arm is stated now, so a
    # token streamed into the refresh the scroll waits on does not find
    # following still off.  Both run however many rows were dropped: the history
    # that *is* drawable has still come back — and the note counts that, not the
    # rows asked for, so a drop does not inflate it.
    chat._autoscroll = True
    chat.add_note(notes.restored(drawn))
    chat.jump_to_tail()


def _draw_turn(chat: ChatView, turn: dict[str, Any]) -> None:
    """One stored turn: the question, the answers, and the work between them."""
    messages = turn.get("messages") or []
    # The results are collected first because they arrive *after* the call that
    # asked for them — a tool panel is opened by an assistant message and
    # completed by a tool message further down — so one walk cannot both open a
    # panel and know what to put in it.  Each result carries its message's
    # channel because a cut-in's line wears the bubble the message arrived on.
    results: dict[str, tuple[str, str]] = {}
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id"):
            results[str(message["tool_call_id"])] = (
                _text(message.get("content")),
                str(message.get("channel") or ""),
            )

    # When the question was asked, and what every line of this turn is stamped
    # with.  The completed side is only the assistant's, and slife2's assistant
    # blocks carry no timestamp live — so a rebuilt one does not either, and
    # looking like the live transcript is the rule that settles it.
    #
    # Passed through as stored, an empty or damaged value included: `or None`
    # here turned one into "now" at the last iteration, which is the stamp
    # `_timestamp`
    # refuses to invent (SDD §5.5).  A row that says nothing about when it
    # ran draws no moment at all.
    asked = turn.get("created_at") or ""
    # The turn's id, channel and span, shown beside the words that opened it.  It
    # is the *same* footnote `messages_from_turns` wraps in `[TURN:{…}]` for the
    # model — one function, two readers — and that is the point of showing it at
    # all: the ids a keep-list is written with are then visible to the person
    # watching, instead of being a number only the model ever sees.
    #
    # No `[TURN:]` envelope here: that is the machine's marker, and a screen
    # renders the payload (`turn_footnote`).
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
            # The channel rides along because the screen needs two things from
            # it that are not the footnote's business: the bubble this message
            # wears (`Wechat> ` for a turn somebody sent over WeChat) and the
            # marker the widget takes back off the text.  One value, so the
            # bubble and the stripping cannot come from different turns.
            chat.add_user(
                _text(message.get("content")),
                moment=asked,
                footnote=footnote if index == 0 else "",
                channel=str(turn.get("channel") or ""),
            )
        elif role == "assistant":
            _draw_answer(chat, message, results)


def _draw_answer(
    chat: ChatView, message: dict[str, Any], results: dict[str, tuple[str, str]]
) -> None:
    """One assistant message — its reasoning, its answer, its tool panels."""
    calls = [ToolCall.from_wire(raw) for raw in message.get("tool_calls") or []]
    answer = _text(message.get("content"))
    thinking = _text(message.get("thinking"))

    # Opened only when there is something to open it *for*.  An iteration that
    # says
    # nothing and thought nothing is a message the live transcript could not have
    # shown either: nothing streamed into it, so no block was ever opened — and
    # opening one here would leave a row holding a lone `…`.
    #
    # The order is the live turn's, and that is what carries the fold: a tool
    # call collapses the reasoning of the block it closes, so
    # `add_tool_start` below folds this iteration's thinking by itself, and the
    # last
    # iteration's stays open because no call follows it.  Nothing restates that
    # rule.
    if thinking or answer:
        chat.begin_assistant()
        if thinking:
            chat.append_thinking(thinking)
        if answer:
            chat.append_text(answer)

    for call in calls:
        text, channel = results.get(call.id, ("", ""))
        if call.name == CHECK_NEW_INPUT:
            # **The one harness call a rebuild draws.**  The pair is how a
            # message that arrived mid-turn entered the conversation, and the
            # tool's answer is the message's own words.  `add_cut_in` keeps the
            # turn open, exactly as the live event does, so the answer that
            # follows this pair streams under the line rather than replacing it.
            # The channel is the one `Message.to_record` kept: a record from
            # before that field reads as this window's own, which is the only
            # speaker a missing channel can honestly be.
            chat.add_cut_in(text, channel=channel or WINDOW_CHANNEL)
        elif not is_harness(call.name):
            _draw_tool(chat, call, text)


def _draw_tool(chat: ChatView, call: ToolCall, result: str) -> None:
    """A tool panel, opened and completed — the two halves of a live call.

    The result is shown through `preview`, exactly as a live one is: the wire
    caps what a panel displays (`slife2/events.PREVIEW_CHARS`) so that a tool
    returning 100 KB is not duplicated into every widget and log line, and the
    count carried beside it is the *whole* result's.  Passing the stored text
    through unshaped would make a restored panel the one place a tool's entire
    output is rendered — and make it unlike the same call seen live.
    """
    # No iteration: the turn log keeps no such column, and a live panel's
    # counter comes from the event.  Zero is the widget's "nobody said", which
    # leaves the counter off rather than claiming `(0)` — the same rule the
    # duration below follows.
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


def _text(content: Any) -> str:
    """A message's text, from either spelling of it.

    `content` is a string or a list of content parts — the OpenAI shape, which
    is what a message carrying an image is — so the parts are read rather than
    the list being rendered.  Images themselves are not drawn: the transcript
    shows what was *said*, and the turn's own words are where a source is
    named.

    **The `[Image:…]` reference is machinery, not speech.**  It is the address
    a kept picture leaves in the row, and the model — not the person — is its
    reader; showing its JSON in a restored bubble would be the same mistake as
    drawing `[TURN:…]`.
    """
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and not str(part.get("text") or "").startswith(IMAGE_PREFIX)
        )
    return str(content) if content else ""
