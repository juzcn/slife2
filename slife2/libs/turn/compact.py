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

**An annotation belongs to a message the model did not write.**  The live id is
a marginal note on what the model is *reading* — a person's message, a tool's
result, and, in the selector's silent view, the assistant message of a tool
exchange.  An assistant message's content is what the model said, and a marker
written there is a sentence it never wrote that it will copy into the next
answer, so the turn's own view never carries it.  A tool exchange is dropped by
naming both members (see :func:`split_exchanges`).

**The selection is the model's; the harness checks it against the view.**  The
selector is asked which live messages this turn no longer needs.  The harness
checks the id against the view; a tool exchange named in part is *completed* to
the whole exchange, because a call without its answer — or an answer without its
call — is a history no provider accepts.  An id the view did not offer is
refused whole — a tool error for the model's own call, a report for the
automatic one — so the turn continues on its full view instead of failing the
next request.  Being wrong costs a call, not a turn.

**A selection is read by one parser.**  The model's own `_turn_compact` call
writes the same `compact` list the selector writes, so it is read by
`slife2.libs.turn.context.parse_live` and checked here — the shape and the ids
answer to one rule, whichever party wrote the list.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from typing import Any

from slife2.libs.turn.context import with_note
from slife2.libs.turn.messages import Message


@dataclass(frozen=True)
class Compacted:
    """What one live compaction did: what it saw, what it dropped, and why not."""

    #: How many messages the selector read.
    seen: int
    #: How many it dropped.
    dropped: int
    #: `""` for a decision, otherwise why there was none — the provider's
    #: failure text (`failure_text`) or the parser's refusal.
    decision: str = ""
    #: Why the selection was refused before it was applied (an unreadable
    #: selection or an id this view did not offer), or `""`.  The selection
    #: could not be carried by the next request as it was written, so nothing
    #: was hidden.
    refused: str = ""


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


def _has_text(content: Any) -> bool:
    """Whether a message carries anything to write an annotation into."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return bool(content)
    return False


def _annotated(
    message: Message, index: int, at: int | None, annotate_assistant: bool
) -> bool:
    """Whether the view writes this message's live id into its content.

    **A message the model did not write, and one with room for the note.**  The
    annotation is a marginal note on what the model is *reading*: a person's
    message or a tool's result.  An assistant message is what the model said,
    and a marker written into its content is a sentence it never wrote — the
    model copies it into the next answer, once per call.  The selector's view is
    the exception: it annotates the assistant message of a tool exchange too,
    because an exchange is dropped by naming both of its members, and the
    selector's call is silent (the turn's own view is built without this flag).
    The turn's opening is fixed and carries no id either.
    """
    if message.role not in ("user", "tool") and not (
        annotate_assistant and message.role == "assistant" and message.tool_calls
    ):
        return False
    if index == at:
        return False
    if message.role == "assistant":
        # A tool-call-only message has no text; `with_note` writes the note
        # alone, so the selector can read the id it must name.
        return True
    return _has_text(message.content)


def view(
    messages: Sequence[Message],
    hidden: Collection[str],
    annotate_assistant: bool = False,
) -> list[Message]:
    """The list the model is sent: the conversation minus what was hidden.

    **Every addressable message gains its live id here**, like the `[TURN:]`
    footnote a rebuilt turn carries: `{turn_id}.{index}` for a historical
    message (set by `messages_from_turns`), `cur.{index}` for one of the turn
    running, counted from its opening.  The *annotation* — the
    `[MESSAGE:{"id": …}]` text — goes on the messages the model did not write,
    and the id itself is kept on all of them for the exchange rule.  Both are
    copies and never reach the record; `hidden` holds the same ids, so hiding a
    message does not renumber the ones around it.  The system prompt is not
    addressable and passes through untouched.  `annotate_assistant` is the
    selector's view (`projection`): a tool exchange is named by both of its
    members, so the assistant message's id has to be readable there.
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
        if not _annotated(message, index, at, annotate_assistant):
            out.append(replace(message, mid=mid))
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
    """The view's ids a selector may drop: the annotated messages but the opening.

    **An exchange's assistant message is a candidate too**, because a tool
    exchange is dropped by naming both members.  The person's messages and tool
    results carry an annotation; the assistant message of an exchange carries
    one in the selector's view (`view`'s `annotate_assistant`), and its id is
    accepted in the turn's own view as well.  The opening message is fixed —
    hiding it would leave the model answering nothing — and the system prompt
    is the conversation's instructions rather than its history.  A historical
    message hidden here is a view edit for this turn only; the conversation's
    durable shape is `_context_rebuild`'s.
    """
    return [
        message.mid
        for message in view
        if message.mid
        and message.mid != opening_mid
        and (
            (message.role in ("user", "tool") and _has_text(message.content))
            or (message.role == "assistant" and message.tool_calls)
        )
    ]


def projection(
    messages: Sequence[Message], hidden: Collection[str]
) -> tuple[list[Message], list[str]]:
    """The view a selector reads and the ids it may drop, or two empty lists.

    **The one answer to "what would a compaction be asked about."**  The view is
    what the selector is sent (`view` with the assistant annotations), and the
    ids are the ones a selection may name (`selectable`) — the system prompt and
    the turn's opening message are the fixed two.  Two empty lists mean the turn
    has no opening, or nothing left to drop; both callers read that as "not due"
    and "nothing to ask".
    """
    at = opening(messages)
    if at is None:
        return [], []
    projected = view(messages, hidden, annotate_assistant=True)
    at_view = opening(projected)
    opening_mid = projected[at_view].mid if at_view is not None else ""
    return projected, selectable(projected, opening_mid)


def due(
    messages: Sequence[Message],
    hidden: Collection[str],
    *,
    last_tokens: int,
    prompt_window: int,
    compact_at: float,
    held: bool,
) -> tuple[list[Message], list[str]] | None:
    """The view a due compaction asks over, or `None` when none is due.

    **One rule for the agent, its subagents and an agent task.**  The
    measurement is the last completed call's own, and the fraction is
    `agent.compact_at` of the prompt budget the conversation started on.  A turn
    that began near its ceiling crosses it after one large batch; the
    just-appended batch is not in the measurement yet, and the fraction is below
    the window by enough to cover one.

    **Re-asked whenever the threshold is crossed again.**  A compaction that
    dropped something leaves the pass live, so a turn that grows back over the
    fraction is asked again at the next boundary.  A compaction that kept
    everything or failed is a decision about the whole context and stops the
    automatic pass for the turn (`held`); the model can still call the tool
    itself, and a later successful call resumes the pass.

    The answer is the projection the caller then asks its selector over, so the
    view and the candidate set are built once.
    """
    if compact_at <= 0 or held:
        return None
    if last_tokens < prompt_window * compact_at:
        return None
    projected, candidates = projection(messages, hidden)
    return (projected, candidates) if candidates else None


def unknown_ids(
    selectable: Collection[str],
    ids: Collection[str],
    hidden: Collection[str] = (),
) -> list[str]:
    """The selected ids this view does not offer, in selection order.

    **`selectable` is `selectable()`'s answer** — the annotated messages but the
    opening — and `hidden` is what this turn has already dropped.  Any id outside
    both is a message the model cannot name: the system prompt, the opening
    message, or an id no build wrote.  An id it already hid is not an error: a
    model that repeats a cumulative list is asking for the same view.

    The caller refuses the whole selection rather than applying the part of it
    that resolves, because the rest would be a decision the model did not make
    (`slife2.libs.turn.runtime.compact_turn`).
    """
    offered = set(selectable) | set(hidden)
    return [mid for mid in ids if mid not in offered]


def split_exchanges(view: Sequence[Message], ids: Collection[str]) -> list[list[str]]:
    """The tool exchanges the selection named only in part.

    **A pair moves whole or not at all.**  An assistant message with tool_calls
    and every result of those calls are one exchange.  To drop it, the model
    names both members — the assistant message and each result; a selection
    that names one without the others is refused, and the exchange stays in the
    view.  The harness does **not** complete a half-named exchange: the safe
    direction is to keep the pair, and naming both is the decision the model
    can make because the selector's view annotates the assistant message too
    (`projection`).
    """
    chosen = set(ids)
    split: list[list[str]] = []
    for index, message in enumerate(view):
        if message.role != "assistant" or not message.tool_calls or not message.mid:
            continue
        calls = {call.id for call in message.tool_calls}
        group = {message.mid}
        for other in view[index + 1 :]:
            if other.role == "assistant":
                break
            if other.role == "tool" and other.tool_call_id in calls and other.mid:
                group.add(other.mid)
        named = group & chosen
        if named and named != group:
            split.append(sorted(group))
    return split


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
