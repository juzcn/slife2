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

**The selection is the model's; the harness checks it against the view.**  The
selector is asked which live messages this turn no longer needs, and its schema
says a tool exchange goes as a whole.  The harness checks both rules before
applying a selection: an id the view did not offer, and a split tool exchange,
are refused — a tool error for the model's own call, a report for the automatic
one — so the turn continues on its full view instead of failing the next
request.  Being wrong costs a call, not a turn.

**A selection is read by one parser.**  The model's own `_turn_compact` call
writes the same `compact` list the selector writes, so it is read by
`slife2.libs.turn.context.parse_live` and checked here — the shape and the ids
answer to one rule, whichever party wrote the list.
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
    #: Why the selection was refused before it was applied (a split tool
    #: exchange, an unreadable selection, or an id this view did not offer), or
    #: `""`.  The selection could not be carried by the next request as it was
    #: written, so nothing was hidden.
    refused: str = ""
    #: The live ids the drop named, in the order they were written.  Kept for
    #: the automatic call's pair — the arguments the harness writes into it —
    #: not for the report: the report is the count, and the pair is the call.
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


def unknown_ids(
    selectable: Collection[str],
    ids: Collection[str],
    hidden: Collection[str] = (),
) -> list[str]:
    """The selected ids this view does not offer, in selection order.

    **`selectable` is `selectable()`'s answer** — every addressable message but
    the fixed two — and `hidden` is what this turn has already dropped.  Any id
    outside both is a message the model cannot name: the system prompt, the
    opening message, or an id no build wrote.  An id it already hid is not an
    error: a model that repeats a cumulative list is asking for the same view.

    The caller refuses the whole selection rather than applying the part of it
    that resolves, because the rest would be a decision the model did not make
    (`slife2.libs.turn.runtime.compact_turn`).
    """
    offered = set(selectable) | set(hidden)
    return [mid for mid in ids if mid not in offered]


def split_exchange(view: Sequence[Message], ids: Collection[str]) -> str | None:
    """Why a selection would split a tool exchange, or `None` if it does not.

    **The provider's rule, checked before the next call rather than after it.**
    An assistant message with `tool_calls` must be followed by a tool message
    for every call, so a selection is all-or-none over such an exchange.  The
    schema states the rule to the model; the harness is the party that knows
    the view, and a split applied here is a 400 on the next request — a failed
    turn for a mistake the tool call can refuse instead.
    """
    selected = set(ids)
    for index, message in enumerate(view):
        if message.role != "assistant" or not message.tool_calls:
            continue
        calls = {call.id for call in message.tool_calls}
        results: list[Message] = []
        for other in view[index + 1 :]:
            if other.role != "tool" or other.tool_call_id not in calls:
                break
            results.append(other)
        group = {
            mid for mid in [message.mid, *(result.mid for result in results)] if mid
        }
        picked = group & selected
        if picked and picked != group:
            return (
                "the selection would split a tool exchange — name the assistant "
                "call and all of its results together"
            )
    return None


def report(result: Compacted, *, recorded: bool = True) -> str:
    """What the tool tells the model, in the transcript and the row.

    **The record is named where there is one.**  An agent server's turn is
    written down, so the model that hid something should know it is still there
    and that the next turn starts from the full conversation.  A task records no
    turn (`slife2.plugins.agent_task.server`), so its report says only what left
    the view — claiming a log it does not have would be a false fact.
    """
    subject = "turn" if recorded else "task"
    if result.refused:
        return (
            f"No compaction: {result.refused}. The {subject} continues on its "
            "full view."
        )
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
        f"Dropped {result.dropped} message(s) from this turn's model "
        "context; the turn log keeps every one of them, and the next turn "
        "starts from the full conversation."
    )
