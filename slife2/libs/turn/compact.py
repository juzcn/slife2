"""The live view a turn's model sees, and the selection that shrinks it.

**A compaction never touches the conversation.**  The live list is the record's
source and the store's accounting (`covered`, `turn_ids`, `carried`): a message
removed from it would be a message the next rebuild puts back, and on a failed
turn a tail read from the wrong end.  So a compaction is a *projection* —
:func:`view` builds the list the model is sent, from the list the conversation
holds minus what earlier selections hid.  The record keeps everything, the
turn's saved row is unchanged, and the next rebuild starts from the full list.
**A `_context_rebuild` resets the view**: hiding is per-turn, and a history edit
starts it over (`loop.hidden` is cleared).

**The selection is the model's, and so is legality.**  The selector is asked
which live messages this turn no longer needs; the prompt says a tool exchange
goes as a whole, and the harness applies the answer as it came.  A model that
splits a call from its result writes a history the next call refuses — the turn
fails, the record is intact, and the next turn runs on the full list.  That is
the trust the harness already gives the model's own tool calls: being wrong
costs one turn, not the conversation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from slife2.libs.turn.messages import Message


@dataclass(frozen=True)
class Compacted:
    """What one live compaction did: what it saw, what it dropped, and why not."""

    #: How many messages the selector read.
    seen: int
    #: How many it dropped.
    dropped: int
    #: `""` for a decision, otherwise why there was none (`failed`/`unparsed`).
    decision: str = ""


def opening(messages: Sequence[Message]) -> int | None:
    """The index of the message that opened the turn running, or `None`.

    **The last user message, because that is what a turn starts with.**  A
    message that arrives mid-turn is a harness tool pair and never a second user
    message (`slife2.libs.turn.loop`), so the last user message is the current
    turn's opening — the one message a compaction may not hide.
    """
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            return index
    return None


def view(messages: Sequence[Message], hidden: Sequence[Message]) -> list[Message]:
    """The list the model is sent: the conversation minus what was hidden."""
    hidden_ids = {id(message) for message in hidden}
    return [message for message in messages if id(message) not in hidden_ids]


def selectable(view: Sequence[Message], opening: Message) -> list[int]:
    """The view's ids a selector may drop: everything but the fixed two.

    The system prompt and the turn's opening message are not candidates.  The
    first is the conversation's instructions rather than its history; the second
    is what the turn *is* — hiding it would leave the model answering nothing.
    Every other message, historical or current, is the model's to drop.  A
    historical one hidden here is a view edit for this turn only; the
    conversation's durable shape is `_context_rebuild`'s.
    """
    return [
        index
        for index, message in enumerate(view)
        if message.role != "system" and message is not opening
    ]


def report(result: Compacted, *, recorded: bool = True) -> str:
    """What the tool tells the model, in the transcript and the row.

    **The record is named where there is one.**  An agent server's turn is
    written down, so the model that hid something should know it is still there
    and that the next turn starts from the full conversation.  A task records no
    turn (`slife2.plugins.agent_task.server`), so its report says only what left
    the view — claiming a log it does not have would be a false fact.
    """
    subject = "turn" if recorded else "task"
    if result.decision:
        return (
            f"No compaction: the selector did not answer ({result.decision}). "
            f"The {subject} continues on the full context."
        )
    if not result.dropped:
        return f"Nothing dropped: the {subject} keeps all {result.seen} messages."
    if not recorded:
        return f"Dropped {result.dropped} message(s) from this task's model context."
    return (
        f"Dropped {result.dropped} message(s) from this turn's model context; "
        "the turn log keeps every one of them, and the next turn starts from "
        "the full conversation."
    )
