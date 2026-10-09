"""What a conversation's context *is* — the decision, as a pure function.

The context plugin's whole judgement lives here, and none of it does I/O: a
reply is parsed into a :class:`Decision`, a decision becomes a list of turn ids,
a list of ids becomes a list of messages.  `slife2.context_server` is the thin
half around it — the store, the embedder, the model call and the wire — and the
split is `slife2.db`'s, for `slife2.db`'s reason: the part that can be tested
without a process is the part worth stating on its own.

Two ideas, both v1's, and the port is deliberately faithful to them because each
one is load-bearing:

**The context is chosen, not accumulated.**  Before every turn the agent says
what to keep of the turns in hand and what to *recall* from the turn log, and the
turn runs on the two together.  One model call decides it — the *discriminator* —
and it is the single most expensive thing here, which is why every way it can go
wrong ends in "keep the context" rather than in a retry.

**The union is what makes "keep this and add that" expressible.**  Kept and
recalled are joined by id and never reconciled: a turn both kept and recalled is
the same turn, so there is no incumbent to defend and no need to exclude turns
already in context.  An empty recall is therefore *harmless* — a union with
nothing is the base — where an overriding selection would have discarded the
context it replaced, so a query that merely failed to match would empty it and
the turn would be answered from the system prompt alone.  Clearing is only ever
the explicit clear.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from slife2.prompt import render

#: The discriminator's instruction, shipped as a template for the reason the
#: system prompt is: it is text with holes in it, and a template says where they
#: are.  Beside `system.j2` so both are found the same way.
INSTRUCTION_TEMPLATE = "recall.j2"

#: What the model is asked to reply with, and what each field means.  Rendered
#: into the instruction rather than described in prose, because a field's
#: *wording* is what a model copies and a description is what it paraphrases.
RECALL_REPLY: dict[str, Any] = {
    "context": (
        "Which of the turns in hand to keep: all of them (omit or \"keep\"), "
        "none (\"clear\"), or the turn_ids to keep, e.g. [12, 15]."
    ),
    "recall": {
        "query": "Search text. Omit for no search.",
        "since": "Start of a period, in the grammar the turn headers use.",
        "until": "End of a period.",
        "anchor": (
            "Which end of a period to recall from, \"newest\" or \"oldest\". "
            "Omit for the newest end."
        ),
    },
}

#: The two ends a period can be read from.  Anything else is a reply this build
#: does not understand, and an unread reply is one it does not act on.
ANCHORS = ("newest", "oldest")

#: The fields of a recall condition, in the order they are read.
RECALL_KEYS = ("query", "since", "until", "anchor")

#: What the reply says to keep *all* of the turns in hand, and *none* of them.
#: Two spellings for keeping everything, because a model that says `"keep"` has
#: answered the question and refusing it would spend a turn to learn nothing.
KEEP_ALL = "keep"
KEEP_NONE = "clear"

#: The message a repaired history gets where a tool result should have been.
#: ASCII, and the same words v1 uses, because it is a sentence in a transcript
#: that a person also reads.
INTERRUPTED = "(Tool execution interrupted)"

#: The prefix and suffix a turn's footnote is wrapped in.  The prefix is v1's,
#: and it is what makes the footnote *findable* rather than merely present: a
#: reader looking for one has a string to look for.
INFO_PREFIX = "[INFO: "
INFO_SUFFIX = "]"


class _Rejected:
    """The answer "this reply is not the object that was asked for".

    A sentinel rather than `None`, because `None` is a *value* in both fields:
    it is what "keep everything" and "recall nothing" parse to.  Collapsing the
    two would make a malformed reply indistinguishable from a decision to do
    nothing, and those want opposite handling — one is logged as a failure, the
    other is the ordinary case.
    """


REJECTED = _Rejected()


@dataclass(frozen=True)
class Decision:
    """What one discriminator reply decided.

    `keep` is `None` for all the turns in hand, a list of ids for some of them,
    and an empty list for none.  `recall` is `None` for nothing to add, or a
    condition — see `RECALL_KEYS`.
    """

    keep: list[int] | None = None
    recall: dict[str, str | None] | None = None

    @property
    def asks_for_nothing(self) -> bool:
        """Whether this reply leaves the context exactly as it found it.

        The common case by a wide margin, and worth being able to name: the
        caller skips the whole rebuild — the fetch, the re-render, the write —
        rather than re-deriving a list that is already in hand.
        """
        return self.keep is None and self.recall is None


def _dedupe(values: Sequence[int]) -> list[int]:
    """Those ids, duplicates collapsed to their first position.

    v1's rule and v1's reason: a keep-list is read in the order it was written,
    and a repeat says nothing about where the turn belongs in time.
    """
    seen: dict[int, None] = {}
    for value in values:
        seen.setdefault(int(value), None)
    return list(seen)


def _keep(raw: Any) -> list[int] | None | _Rejected:
    """The `context` field, as a keep-list.

    Three answers and one rejection: absent or `"keep"` is every turn in hand,
    `"clear"` is none of them, a list of ids is those, and anything else is a
    reply this build does not act on.  `bool` is excluded from the ids on the
    way through, because it is a subclass of `int` and `true` would otherwise
    become turn 1.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        if raw.strip() == KEEP_ALL:
            return None
        if raw.strip() == KEEP_NONE:
            return []
        return REJECTED
    if isinstance(raw, list):
        if not all(isinstance(item, int) and not isinstance(item, bool) for item in raw):
            return REJECTED
        return _dedupe(raw)
    return REJECTED


def _recall(raw: Any) -> dict[str, str | None] | None | _Rejected:
    """The `recall` field, as one condition.

    **One condition, never several.**  A period and a query are the two halves of
    a single request — "about X, last month" — so they arrive together in one
    object rather than as a list of searches, and the three shapes a store can
    answer (a period, a query, a query within a period) are the three ways of
    filling it in.

    A key this build does not know is dropped rather than refused: the reply
    surface is written into the instruction, and a model that added a field of
    its own has still answered the question that was asked.  A *value* of the
    wrong type is different — that is a reply whose condition cannot be read, and
    acting on half of it would be worse than keeping the context.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return REJECTED
    for key in RECALL_KEYS:
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            return REJECTED
    anchor = raw.get("anchor")
    if anchor is not None and anchor not in ANCHORS:
        return REJECTED
    condition = {key: raw.get(key) for key in RECALL_KEYS}
    # Nothing filled in is the same answer as no `recall` field at all, and it
    # has to be normalised here: a store asked for nothing at all cannot answer,
    # and "asked and given nothing" must not become an error downstream.
    return condition if any(condition.values()) else None


def parse(text: str) -> Decision | None:
    """The decision in a discriminator's reply, or `None` if there is not one.

    **Degrading is the whole point.**  A timeout, a provider failure and a reply
    that is not the requested object all arrive here as "nothing usable", and the
    caller's fallback — keep the context — is perfectly good, so this never
    retries and never raises.  Retrying would double the pre-turn latency of a
    call whose answer nobody needed.

    The outer braces are taken rather than the whole text parsed, because a
    model that wraps its JSON in a sentence has still written the JSON; `find`
    and `rfind` are what make that tolerant without accepting a reply with two
    objects in it.  Every other malformation is refused, including a top-level
    key that is neither of the two fields — a reply that says something this
    build does not understand is not a reply it should half-obey.
    """
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        raw = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    if not set(raw) <= {"context", "recall"}:
        return None
    keep = _keep(raw.get("context"))
    if isinstance(keep, _Rejected):
        return None
    recall = _recall(raw.get("recall"))
    if isinstance(recall, _Rejected):
        return None
    return Decision(keep=keep, recall=recall)


def instruction(user_input: str) -> str:
    """What the discriminator is asked, with the input it is being asked about.

    The *current input* travels inside the instruction rather than as a message
    of its own, and that is not a shortcut: the turn being recalled is usually a
    follow-up, so a query written from the input alone drops the subject the
    conversation in hand is carrying.  The model reads the conversation — the
    instruction is appended to it — and this is what tells it which input the
    decision is about.
    """
    return render(
        INSTRUCTION_TEMPLATE,
        user_input=user_input,
        recall_reply=json.dumps(RECALL_REPLY, indent=2, ensure_ascii=False),
    )


def union(base: Sequence[int], recalled: Sequence[int]) -> list[int]:
    """Kept and recalled, joined by id, in time order.

    Sorted and not concatenated: membership is the union and *order* is time,
    because the render order is chronological in every case — that is the
    restore contract and not a choice.  A set rather than a merge, so that a turn
    both kept and recalled appears once without anything having to notice.
    """
    return sorted(set(base) | set(recalled))


def gate(
    ranked: Sequence[tuple[int, float | None]], minimum: float
) -> list[tuple[int, float | None]]:
    """Drop the ranked candidates whose *measured* similarity is below a floor.

    **A candidate with no number is exempt**, and that is the half worth stating:
    a fused score is a function of rank position and carries no magnitude to
    threshold, so a floor can only gate a similarity the store actually
    measured — and a turn found by the keyword leg alone has not been measured
    against anything.  An exact match is a stronger signal than a cosine
    neighbourhood, and "no number" is not evidence against it.
    """
    return [
        (turn_id, similarity)
        for turn_id, similarity in ranked
        if similarity is None or similarity >= minimum
    ]


def fit_budget(
    ranked: Sequence[int], costs: Mapping[int, int], budget: int
) -> list[int]:
    """Spend a token budget down a relevance ranking; answer chronologically.

    **Rank order decides membership**, which is the rule for a query: the axis
    the candidates are ordered by is relevance, so the cut falls at the relevance
    tail.  A candidate too large to fit is *skipped* rather than treated as the
    end of the list — a single enormous turn must not stop the ones after it, or
    one bad row would make a recall useless for the rest of the session.
    """
    kept: list[int] = []
    spent = 0
    for turn_id in ranked:
        cost = costs.get(turn_id, 0)
        if spent + cost > budget:
            continue
        kept.append(turn_id)
        spent += cost
    return sorted(kept)


def fit_window(
    ranked: Sequence[int], costs: Mapping[int, int], budget: int
) -> list[int]:
    """Spend a token budget from the head of a time-ordered list; answer the same.

    **The first candidate that does not fit ends the selection.**  With no query
    the axis is time, so the list is contiguous and the cut has to be too: a
    period read from its end is "the last N days", and skipping a large turn in
    the middle to reach a small one further back would answer a question nobody
    asked.  That is the whole of the difference from `fit_budget`, and it is why
    there are two functions rather than one with a flag.
    """
    kept: list[int] = []
    spent = 0
    for turn_id in ranked:
        cost = costs.get(turn_id, 0)
        if spent + cost > budget:
            break
        kept.append(turn_id)
        spent += cost
    return sorted(kept)


def _format_moment(value: str) -> str:
    """An ISO timestamp as `YYYY-MM-DD HH:MM`, or `""` if it will not parse.

    Truncated to the minute because the footnote is read by a model deciding
    whether a turn is the one it wants, and seconds are noise at that decision.
    """
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    return moment.strftime("%Y-%m-%d %H:%M")


def turn_note(turn_id: int, created_at: str, completed_at: str | None) -> str:
    """The footnote a rebuilt turn carries, naming its id and its span.

    **This is how a turn id reaches the model at all**, and therefore how a
    keep-list is expressible: the ids a model writes back are the ids it read
    here.  It is added when a message list is *built* and never stored — the turn
    row holds the user's own words — so a footnote can never be persisted twice
    or drift from the row it describes.

    The end collapses to a time when it shares the begin's day, which is the
    ordinary case and the one where repeating the date costs a token to say
    nothing.  A turn with no id and no timestamps gets no footnote, because an
    empty one would tell the model that something is being withheld.
    """
    begin = _format_moment(created_at)
    end = _format_moment(completed_at or "")
    day = begin[:10]
    if day and end.startswith(day):
        end = end[11:]
    payload: dict[str, Any] = {"turn_id": int(turn_id)}
    if begin:
        payload["begin"] = begin
    if end:
        payload["end"] = end
    return f"{INFO_PREFIX}{json.dumps(payload, ensure_ascii=False)}{INFO_SUFFIX}"


def _with_note(content: Any, note: str) -> Any:
    """A message's content with the footnote appended.

    Two shapes, because content is a string or a list of parts: text is appended
    to text, and a part-list gains a text part.  Appending to a list would be a
    type error and replacing it would drop an attachment's note, which is the one
    thing the stored turn still says about what was attached.
    """
    if isinstance(content, str):
        return f"{content} {note}".strip()
    if isinstance(content, list):
        return [*content, {"type": "text", "text": note}]
    return note


def messages_from_turns(
    turns: Sequence[Mapping[str, Any]], *, head: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The **one** turn→messages builder, shared by restore and the rebuild.

    Shared and not written twice, because a restored turn and a rebuilt one have
    to render identically: they are the same conversation seen at two moments,
    and two builders is how a footnote drifts from what the keep-list addresses.

    `head` is the caller's system message, kept as the first element and never
    re-rendered — the prompt is a property of the conversation, and re-rendering
    it would be this module deciding something it was not asked about.

    Each turn contributes a footnote-carrying copy of its first message followed
    by the rest of the row verbatim.  **The first message is copied and not
    mutated**, because the row a caller handed in is the row as stored: a builder
    that wrote a footnote into it would corrupt the very thing it is reading, and
    the second turn of a rebuild would carry two.
    """
    built: list[dict[str, Any]] = [dict(head)] if head else []
    for turn in turns:
        messages = list(turn.get("messages") or [])
        if not messages:
            continue
        note = turn_note(
            int(turn.get("turn_id") or 0),
            str(turn.get("created_at") or ""),
            turn.get("completed_at"),
        )
        first = dict(messages[0])
        if note:
            first["content"] = _with_note(first.get("content"), note)
        built.append(first)
        built.extend(messages[1:])
    return built


def consistent(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Repair orphaned tool calls, so the list is one a provider will accept.

    **The one history state every provider rejects** is an assistant message
    whose tool calls are only partly answered, and a rebuild is a place it can
    appear from nothing: a selection is a subset of turns, and a subset's last
    turn is only well formed by accident.  v1 runs the same repair at its save,
    its restore and a subagent's clone, and calls it idempotent — running it on a
    list that is already correct changes nothing, which is what makes it safe to
    run unconditionally here rather than only where a problem is suspected.

    The placeholder is the same sentence the agent server's own cancellation
    repair leaves, because a model reading back a conversation should not be able
    to tell which of the two interrupted a call.

    **What is deliberately not ported is v1's second invariant** — a history
    ending on `user` or `tool` gets a closing assistant line.  That exists for a
    history that is *sent* as it stands; here a rebuilt list is handed straight
    to a loop that appends the user's message to it before anything is sent, so a
    closing line would be an assistant turn the user's message interrupts.
    """
    # Answered anywhere, in one pass first: a result always follows the call, so
    # a single walk would decide each call before seeing whether it was answered
    # and repair the whole of every healthy exchange.
    answered = {
        str(message.get("tool_call_id") or "")
        for message in messages
        if message.get("role") == "tool"
    }
    repaired: list[dict[str, Any]] = []
    for message in messages:
        repaired.append(dict(message))
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            call_id = str((call or {}).get("id") or "")
            if call_id and call_id not in answered:
                answered.add(call_id)
                repaired.append(
                    {
                        "role": "tool",
                        "content": INTERRUPTED,
                        "tool_call_id": call_id,
                    }
                )
    return repaired
