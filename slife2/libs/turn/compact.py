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
which live messages this turn no longer needs; its schema says a tool exchange
goes as a whole, and the harness applies the answer as it came.  A model that
splits a call from its result writes a history the next call refuses — the turn
fails, the record is intact, and the next turn runs on the full list.  That is
the trust the harness already gives the model's own tool calls: being wrong
costs one turn, not the conversation.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace

from slife2.libs.turn.context import with_note
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
    #: The live ids the drop named, in the order they were written.  Kept so the
    #: report can say *which* messages left rather than only how many: the id is
    #: the one thing the model and the transcript can both address a message by.
    ids: tuple[str, ...] = ()


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


def _id_note(mid: str) -> str:
    """The annotation a live-id-bearing message gains in the view."""
    return f'[MESSAGE:{{"id": "{mid}"}}]'


def view(messages: Sequence[Message], hidden: Collection[str]) -> list[Message]:
    """The list the model is sent: the conversation minus what was hidden.

    **Every addressable message carries its live id in the view**, like the
    `[TURN:]` footnote a rebuilt turn carries: `{turn_id}.{index}` for a
    historical message (set by `messages_from_turns`), `cur.{index}` for one of
    the turn running, counted from its opening.  The annotation is a copy and
    never reaches the record; `hidden` holds the same ids, so hiding a message
    does not renumber the ones around it.  The system prompt is not addressable
    and passes through untouched.
    """
    at = opening(messages)
    hidden_ids = set(hidden)
    out: list[Message] = []
    for index, message in enumerate(messages):
        if message.role == "system":
            out.append(message)
            continue
        mid = message.mid or (
            f"cur.{index - at}" if at is not None and index >= at else ""
        )
        if mid and mid in hidden_ids:
            continue
        if not mid:
            out.append(message)
            continue
        out.append(
            replace(
                message,
                content=with_note(message.content, _id_note(mid)),
                mid=mid,
            )
        )
    return out


def selectable(view: Sequence[Message], opening_mid: str) -> list[str]:
    """The view's ids a selector may drop: everything but the fixed two.

    The system prompt and the turn's opening message are not candidates.  The
    first is the conversation's instructions rather than its history; the second
    is what the turn *is* — hiding it would leave the model answering nothing.
    Every other message, historical or current, is the model's to drop.  A
    historical one hidden here is a view edit for this turn only; the
    conversation's durable shape is `_context_rebuild`'s.
    """
    return [
        message.mid
        for message in view
        if message.mid and message.role != "system" and message.mid != opening_mid
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
    named = f": {', '.join(result.ids)}" if result.ids else ""
    if not recorded:
        return (
            f"Dropped {result.dropped} message(s){named} from this task's "
            "model context."
        )
    return (
        f"Dropped {result.dropped} message(s){named} from this turn's model "
        "context; the turn log keeps every one of them, and the next turn "
        "starts from the full conversation."
    )
