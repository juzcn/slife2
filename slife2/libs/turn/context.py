"""What a conversation's context *is* — the decision, as a pure function.

The context plugin's whole judgement lives here, and none of it does I/O: a
reply is parsed into a :class:`Decision`, a decision becomes a list of turn ids,
a list of ids becomes a list of messages.  `slife2.plugins.context.server` is the thin
half around it — the store, the embedder, the model call and the wire — and the
split is `slife2.libs.store.db`'s, for `slife2.libs.store.db`'s reason: the part that can be tested
without a process is the part worth stating on its own.

Two ideas, and the implementation is deliberately faithful to them because each
one is load-bearing:

**The context is chosen, not accumulated.**  At each turn's first boundary the
agent says what to keep of the turns in hand and what to *recall* from the turn
log, and the turn runs on the two together.  One model call decides it — the
*decision model* — and it is the single most expensive thing here, which is why
a failure ends in "keep the context" rather than in a retry.  The exception is
an answer this build cannot read: the refusal names the field
(`parse_with_reason`) and the model is asked once more with that reason, because
correcting an answer already in hand is not the same bet as asking a silent
provider again.

**The union is what makes "keep this and add that" expressible.**  Kept and
recalled are joined by id and never reconciled: a turn both kept and recalled is
the same turn, so there is no incumbent to defend.  An empty recall is therefore
*harmless* — a union with nothing is the base — where an overriding selection
would have discarded the context it replaced, so a query that merely failed to
match would empty it and the turn would be answered from the system prompt
alone.  Clearing is only ever the explicit clear.

**The live selection is the same decision, one question later.**  `_turn_compact`
asks which of the running turn's messages it no longer needs; `parse_live` reads
that answer into message positions, and `LIVE_REPLY` is its schema.  The same
call, repair and failure rules apply (`slife2.plugins.context.server`; SDD §5.3).
Its answer edits a view, never the history.

**Two annotations, one for a turn that ran and one for the turn running.**
`turn_note` is the `[TURN:{…}]` footnote rebuilt turns carry: it is derived from
a row every time a list is built and never stored.  `turn_prompt_note` is
`[TURN-PROMPT:{…}]`, the clock and the conversation's plan at the start of the
turn about to run: it is added to the user's message when the turn opens and
removed before the turn is saved, so it never reaches the row or the index — a
list rebuilt from the row does not carry it, and the turn a rebuild carries
verbatim does.  The second is the only place a per-turn fact can live without
becoming a fact about the turn for ever — and its payload is a mapping for
exactly that reason: what a turn needs to know can grow without giving the past
a copy of it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from slife2.helpers.clock import now
from slife2.helpers.timeutil import BOUND_GRAMMAR, can_place
from slife2.libs.harness.markers import wrap
from slife2.libs.turn.prompt import render_instruction
from slife2.libs.turn.tools import INTERRUPTED

#: The decision model's instruction, shipped as a template for the reason the
#: system prompt is: it is text with holes in it, and a template says where they
#: are.  Beside `system.j2` so both are found the same way.
INSTRUCTION_TEMPLATE = "recall.j2"

#: The live compaction selector's instruction — the same kind of template, and
#: the same entry point, as `INSTRUCTION_TEMPLATE`.
LIVE_TEMPLATE = "live.j2"

#: The two decision tools, and the one line each tool says about itself.  The
#: instruction travels as a system message appended to the live conversation;
#: the tool description says only what the call records, and the reply schema is
#: the tool's `parameters`.  Neither name is ever advertised in a turn — only in
#: the decision request whose `tool_choice` names it.
DECISION_TOOL = "decide_context"
SELECTION_TOOL = "select_messages"
DECISION_TOOL_TEXT = "Record the context decision described by the system instruction."
SELECTION_TOOL_TEXT = (
    "Record the live-message selection described by the system instruction."
)

#: The live selector's tool schema: the decision call's `parameters`, so the
#: live message ids are a typed argument rather than a sentence (`RECALL_REPLY`
#: states the same for the decision model).  **The rules are here and not in
#: `live.j2`** — the fixed two, the tool-exchange legality and the empty answer
#: are what the model fills, so the schema says them once (SDD §2.3).
LIVE_REPLY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "compact": {
            "description": (
                "The live message ids whose content this turn no longer needs "
                "— any numbered message except the system prompt and the "
                "turn's opening message, historical or current. Omit for none. "
                "**Drop a tool exchange as a whole**: an assistant message "
                "that asked for tools goes together with all of its results, "
                "and a result goes together with the assistant message that "
                "asked for it — a call without its answer, or an answer "
                "without its call, is a history no provider accepts."
            ),
            "type": "array",
            "items": {"type": "integer"},
        }
    },
}

#: The decision model's tool schema: the types, the allowed values, and the
#: rules that couple them.  The decision call is a forced tool call (SDD §2.3,
#: "Schema over prompt"): the schema is the tool's `parameters`, so the field's
#: wording is what the model fills, not a sentence it copies — which is why the
#: running turn's exclusion and a follow-up's sentence are here rather than in
#: `recall.j2`.
RECALL_REPLY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "context": {
            "description": (
                "The turns in hand this decision is answered from — never the "
                "turn running, which has no turn id and is carried verbatim. "
                "Omit for all of them. A follow-up keeps the turn it continues, "
                "even when the input brings a new word and does not name it. "
                '"clear" only when the input stands on its own: a turn dropped '
                "here cannot be recalled back — the recall searches only the "
                "turn log outside the turns in hand, not merely outside the "
                "turns kept."
            ),
            "oneOf": [
                {"const": "keep", "title": "all of them"},
                {"const": "clear", "title": "none of them"},
                {
                    "type": "array",
                    "items": {"type": "integer"},
                    "title": "these turn_ids",
                },
            ],
        },
        "recall": {
            "description": (
                "One condition: a period, a question, or both. Omit for no recall."
            ),
            "type": "object",
            "anyOf": [
                {"required": ["sentences"]},
                {"required": ["query"]},
                {"required": ["since"]},
                {"required": ["until"]},
                {"required": ["anchor"]},
            ],
            "properties": {
                "sentences": {
                    "description": (
                        "What to recall from the turn log, in your own words, "
                        "one sentence per idea. Read by meaning. Several "
                        "sentences are several questions: another question is "
                        "another chance to be found, and cannot cost a turn an "
                        "earlier one found. A follow-up whose subject is no "
                        "longer in hand carries it into the sentence."
                    ),
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "query": {
                    "description": (
                        "Words the keyword leg should find — names, paths, "
                        "exact strings. The fallback when no sentence names the "
                        "subject; without a sentence it is also read by meaning."
                    ),
                    "type": "string",
                    "minLength": 1,
                },
                "since": {
                    "description": f"Start of a period. Takes {BOUND_GRAMMAR}.",
                    "type": "string",
                },
                "until": {
                    "description": ('End of a period, in the same grammar as "since".'),
                    "type": "string",
                },
                "anchor": {
                    "description": (
                        "Which end of a period to recall from; omit for the "
                        "newest end. A period recall may truncate as the context "
                        "is bounded, and the anchor chooses which end survives; a "
                        "query recall is bounded by a narrower period instead. "
                        "Alone, it reads the whole log from that end."
                    ),
                    "enum": ["newest", "oldest"],
                },
            },
        },
    },
}

#: The two ends a period can be read from.  Anything else is a reply this build
#: does not understand, and an unread reply is one it does not act on.
ANCHORS = ("newest", "oldest")

#: The fields of a recall condition, in the order they are read.
RECALL_KEYS = (
    "query",
    "sentences",
    "since",
    "until",
    "anchor",
)

#: What the reply says to keep *all* of the turns in hand, and *none* of them.
#: Two spellings for keeping everything, because a model that says `"keep"` has
#: answered the question and refusing it would spend a turn to learn nothing.
KEEP_ALL = "keep"
KEEP_NONE = "clear"

#: The prefix and suffix a turn's footnote is wrapped in, and the marker is what
#: makes the footnote *findable* rather than merely present: a reader looking for
#: one has a string to look for.
#:
#: **The harness's own marker**, beside `slife2.plugins.wechat.server.marker`'s
#: `[Wechat:{…}]` on a turn somebody sent over WeChat.  This envelope holds one
#: thing — the footnote, on the user message a turn opened with.  A trim note
#: saying how many old turns had just been cut would be
#: a second occupant, but the trim is deferred here (SDD §14.2), which leaves
#: this envelope with one occupant — and an envelope with one occupant can say
#: what it holds.  `TURN` is what it holds: the turn this message opened.
#:
#: **A name in capitals, a colon, and the payload hard against it.**  These
#: markers have this shape, so the case is what tells them apart — by convention,
#: and convention is the whole of it: no code reads the case, and they are
#: never compared.  What the capitals are meant to say is where the marker lives.
#: This one is the harness's own annotation, derived every time a message list is
#: built and never stored, while `[Wechat:{…}]`'s name reads as a word because
#: that marker is content — it is in the row, and therefore in what the model
#: remembers.  A model reading the conversation is the reader both are written
#: for.  Nothing goes inside the brackets but the payload, because the payload is
#: the part a model copies out — one more character in it is one more character
#: to copy wrong.  The space that sets a marker off from the text beside it is
#: outside the bracket, and it belongs to whoever joins the two.
TURN_PREFIX = "[TURN:"
#: The per-turn prompt marker: the clock and the plan at the start of the turn
#: the model is about to answer.  **A second capitals marker, and the one that is
#: not derived from a row**: `[TURN:]` is a record of a turn that ran, while this
#: is a prompt for the turn about to run — added to the user's message before the
#: turn, and removed before the turn is saved.  It is therefore never stored and
#: never reconstructed from a row; its readers are the model answering and the
#: decision that reads the live conversation, and the running turn a rebuild
#: carries verbatim is the only place it survives one.  It is never seen by a
#: screen.  **No context figure**: the previous call's is stale before the
#: rebuild, and `_context_rebuild`'s report carries the turn's own estimate.
TURN_PROMPT_PREFIX = "[TURN-PROMPT:"
#: The harness's record of a turn that died, as a message of its own.  **The
#: one capitals marker that is a message rather than a footnote**: the failure
#: belongs to the turn, but a footnote is derived from the row and a failure is
#: not a column — so it follows the turn as an assistant message the harness
#: wrote, which is stored, rebuilt, and still there after a restart.
TURN_FAILED_PREFIX = "[TURN-FAILED:"
TURN_SUFFIX = "]"


@dataclass(frozen=True)
class _Refused:
    """Why a reply is not the object that was asked for — one sentence, for the model.

    A refusal rather than `None`, because `None` is a *value* in both fields: it
    is what "keep everything" and "recall nothing" parse to.  Collapsing the two
    would make a malformed reply indistinguishable from a decision to do
    nothing, and those want opposite handling — one is asked to correct itself,
    the other is the ordinary case.

    `reason` names the field and the shape it takes, because its reader is the
    decision model: `parse_with_reason` hands it back for the one repair round,
    and a reason the model cannot act on would be a retry with no point.
    """

    reason: str


@dataclass(frozen=True)
class Decision:
    """What one decision reply decided.

    `keep` is `None` for all the turns in hand, a list of ids for some of them,
    and an empty list for none.  `recall` is `None` for nothing to add, or a
    condition — see `RECALL_KEYS`.
    """

    keep: list[int] | None = None
    recall: dict[str, str | list[str] | bool | None] | None = None

    @property
    def asks_for_nothing(self) -> bool:
        """Whether this reply leaves the context exactly as it found it.

        The common case by a wide margin, and worth being able to name: the
        caller skips the whole rebuild — the fetch, the re-render, the write —
        rather than re-deriving a list that is already in hand.
        """
        return self.keep is None and self.recall is None


def normalise_ids(values: Iterable[Any]) -> list[int]:
    """An id list with duplicates collapsed to their **first** position.

    First and not last, which is the only choice that keeps
    the list's meaning: the order *is* the content, so a later mention of an id
    already in the list is a repeat rather than a correction — nothing in a
    second mention says where in time it belongs.

    **The rule lives here and not in the store**, which has the same need and
    used to carry a byte-identical copy of this: a turn's context is what this
    module decides, and the list the decision produces *is* a context, so the
    two answers to "which ids, in what order" being one answer is the point
    rather than a convenience.  `slife2.libs.store.db` imports it.

    The ints are coerced rather than trusted, because the values arrive from two
    directions that neither type-check the other: a model's reply, which is JSON
    of whatever the encoder wrote, and a list read back out of the store, where
    a stringly `"7"` reaching a `rowid IN (…)` query matches nothing — and the
    failure would be a silently short context rather than an error.  `_is_id` is
    the other half of that rule — what *counts* as one id — and a caller that
    filters a reply before handing it here reads it, so the two cannot disagree
    about what a coerced id is (`_keep` did, and the cost was a whole reply).
    """
    seen: dict[int, None] = {}
    for value in values:
        seen.setdefault(int(value), None)
    return list(seen)


def _is_id(value: Any) -> bool:
    """Whether one entry of a `context` list is a turn id.

    **The ints are *coerced* rather than trusted**, which is `normalise_ids`'s
    rule and therefore this function's: a reply is JSON, so `"7"` and `7` are
    the same id written by two encoders, and the model is asked for turn ids —
    the ones it read in a `[TURN:…]` footnote — not for a particular JSON type.

    Two values that look like ids are not:

    * `bool` is a subclass of `int`, and `true` would otherwise become turn 1;
    * a float, because a turn id is a whole count and the same rule
      `slife2.libs.turn.tools.read_timeout` states for a bound applies here:
      `1e999` is valid JSON, arrives as `inf`, and `int(inf)` raises
      `OverflowError` out of a function whose whole contract is that a malformed
      reply is refused rather than raised.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        try:
            int(value)
        except ValueError:
            return False
        return True
    return False


def _keep(raw: Any) -> list[int] | _Refused | None:
    """The `context` field, as a keep-list.

    Three answers and one refusal: absent or `"keep"` is every turn in hand,
    `"clear"` is none of them, a list of ids is those, and anything else is a
    reply this build does not act on.  `bool` is excluded from the ids on the
    way through, because it is a subclass of `int` and `true` would otherwise
    become turn 1.

    **The filter asks `_is_id` rather than `isinstance(item, int)`**, and that
    was a bug of the silent kind: a model that wrote `["7", "9"]` — JSON of
    whatever the encoder wrote, which is the source `normalise_ids`'s docstring
    names — made *every* id resolve to a refusal, so the parse answered no
    decision and dropped the other half of the reply with it.  A valid `recall` beside the
    keep-list was thrown away and the model's instruction was ignored, with only
    `recall_decision_unparsed` in the log to say so.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        if raw.strip() == KEEP_ALL:
            return None
        if raw.strip() == KEEP_NONE:
            return []
        return _Refused('"context" is omitted, "keep", "clear", or a list of turn ids')
    if isinstance(raw, list):
        if not all(_is_id(item) for item in raw):
            return _Refused('"context" holds turn ids only — whole numbers')
        return normalise_ids(raw)
    return _Refused('"context" is omitted, "keep", "clear", or a list of turn ids')


def _recall(raw: Any) -> dict[str, str | list[str] | bool | None] | _Refused | None:
    """The `recall` field, as one condition.

    **One condition, never several.**  A period and a question are the two halves
    of a single request — "about X, last month" — so they arrive together in one
    object rather than as a list of searches.  The question has two spellings:
    `sentences` are read by meaning (each one a separate question, and one that
    cannot cost a turn an earlier one found), while `query` is the words the
    keyword leg asks for — names, paths, exact strings — and the meaning
    fallback when there is no sentence, which is why a recalled turn may not
    carry them.  There is no separate input: the decision model reads the
    conversation the instruction is appended to.

    A key this build does not know is dropped rather than refused: the reply
    surface is written into the instruction, and a model that added a field of
    its own has still answered the question that was asked.  A *value* of the
    wrong type is different — that is a reply whose condition cannot be read, and
    acting on half of it would be worse than keeping the context.  Each refusal
    names the field, because the reason is fed back to the model for the one
    repair round.

    A value of the right type in a grammar this build does not speak is the same
    thing as a wrong type, and refused the same way.  `anchor` has always been
    checked that way; `since` and `until` were not, and were handed to the store
    to raise on (see the note at the check below).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return _Refused('"recall" is one object holding the condition')
    sentences = raw.get("sentences")
    if sentences is not None:
        if not isinstance(sentences, list) or not all(
            isinstance(sentence, str) for sentence in sentences
        ):
            return _Refused(
                '"recall.sentences" is a list of strings, one sentence per idea'
            )
        sentences = [sentence for sentence in sentences if sentence.strip()]
    for key in RECALL_KEYS:
        if key == "sentences":
            continue
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            return _Refused(f'"recall.{key}" is a string')
    anchor = raw.get("anchor")
    if anchor is not None and anchor not in ANCHORS:
        return _Refused('"recall.anchor" is "newest" or "oldest"')
    # **A bound the store cannot place is the third kind of unreadable value.**
    # `since` and `until` are checked as *strings* above and for grammar here,
    # because the two are different questions: `2026-13-99` is the right type
    # and in no grammar this build speaks.  Left to the store it would raise
    # `InvalidTimeBound` from inside a query, and the exception would fail the
    # whole turn — the decision model's answer is not a caller the harness can
    # hand a refusal back to.  `slife2.helpers.timeutil.can_place` is the store's own
    # question asked while the answer is still "drop this reply".
    for key in ("since", "until"):
        bound = raw.get(key)
        if bound and not can_place(bound, role=key):
            return _Refused(
                f'"recall.{key}" is not a time I can place — expected {BOUND_GRAMMAR}'
            )
    condition = {key: raw.get(key) for key in RECALL_KEYS}
    # The filtered list, not the raw one: a list of blank sentences is a field
    # present and empty, and the `asked` below has to read it as nothing asked.
    condition["sentences"] = sentences
    # Nothing filled in is the same answer as no `recall` field at all, and it
    # has to be normalised here: a store asked for nothing at all cannot answer,
    # and "asked and given nothing" must not become an error downstream.
    asked = any(value for value in condition.values())
    return condition if asked else None


def parse_with_reason(text: str) -> tuple[Decision | None, str]:
    """The decision in the model's reply, and why there is none if there isn't.

    **The reason is for the model, not the log.**  A refusal names the field and
    the shape it takes (`_Refused`), which is what the caller hands back with the
    reply it refused so the model can write the object it was asked for.  `""`
    with a decision, and no decision with a reason — never both, never neither.

    The outer braces are taken rather than the whole text parsed, because a
    model that wraps its JSON in a sentence has still written the JSON; `find`
    and `rfind` are what make that tolerant without accepting a reply with two
    objects in it.  Every other malformation is refused, including a top-level
    key that is neither of the two fields — a reply that says something this
    build does not understand is not a reply it should half-obey.
    """
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None, "the reply is not one JSON object"
    try:
        raw = json.loads(text[start : end + 1])
    except ValueError:
        return None, "the reply is not one JSON object"
    if not isinstance(raw, dict):
        return None, "the reply is not one JSON object"
    unknown = set(raw) - {"context", "recall"}
    if unknown:
        names = ", ".join(
            sorted(json.dumps(name, ensure_ascii=False) for name in unknown)
        )
        return (
            None,
            f'only "context" and "recall" are read, and the reply also held {names}',
        )
    keep = _keep(raw.get("context"))
    if isinstance(keep, _Refused):
        return None, keep.reason
    recall = _recall(raw.get("recall"))
    if isinstance(recall, _Refused):
        return None, recall.reason
    return Decision(keep=keep, recall=recall), ""


def repair_note(reason: str, reply: str = "") -> str:
    """What a refused reply is answered with, before the one correction round.

    **One round, not a loop.**  The schema the correction refers to is the
    instruction already in the conversation, so the note does not repeat it; the
    reply that was refused is there too, sent beside it.  A second refusal is
    where the repair stops and the turn keeps the context it has.

    **The text is repeated in the result as well.**  The assistant call beside
    this note is the model's own reply on the wires that can replay one, but
    not every wire can carry a malformed one: Anthropic's `tool_use.input` is
    an object by protocol, so a reply that was not an object arrives there as
    `{}`.  A tool result is text on every wire, and it is the one channel that
    shows the model what it actually wrote.
    """
    shown = f" It was: {reply}" if reply else ""
    return (
        f"That reply could not be read: {reason}.{shown} "
        "Reply again with ONE JSON object holding the fields asked for, and "
        "nothing else."
    )


def instruction() -> str:
    """What the decision model is told: the task, the criterion, one guard.

    **No input is passed beside the messages.**  The rebuild runs where the
    turn's own message is already in the list (or, mid-turn, the cut-in the loop
    delivered), so the instruction is the same every time and the decision is
    made from the conversation the call is made over.  A separate prompt would
    be the previous design's — it existed only because the rebuild ran before the
    message was appended.

    **The answer's shape is not restated here.**  Every field, allowed value and
    coupling rule is `RECALL_REPLY`'s; the cases that used to demonstrate
    combinations were a second spelling of the `oneOf`/`anyOf` the tool already
    sends.

    **The bound grammar is the module's one spelling.**  It is inside the tool
    schema's `since`/`until` descriptions (`RECALL_REPLY`), so the template
    carries no copy of it: the schema is the tool's `parameters` and the code
    that decides reads it, and a copy in the template had fallen behind it — no
    `tomorrow`, `now`, `quarter` or datetime.
    """
    return render_instruction(INSTRUCTION_TEMPLATE)


def with_message_id(message: Mapping[str, Any], index: int) -> dict[str, Any]:
    """One message of the live selector's copy, prefixed with its live id.

    **A copy, and the one place a live id is written.**  The list a turn runs on
    is the record; stamping it would put a number into the row, the index and
    every later request, and the number means nothing after this call.  A
    part-list gains a text part rather than having its first part rewritten,
    for `with_note`'s reason: replacing it would drop an attachment's note.

    **The marker carries the number the model is asked to write back**, in the
    turn footnote's shape: `[MESSAGE:{"id": 4}]`, not a label like `[M4]` that
    hides the integer behind a letter.  The schema names integers, and a model
    shown `M4` wrote `M4` back — a reply the parser refused for a reason that
    said nothing about the wrong channel.
    """
    prefixed = dict(message)
    prefix = f'[MESSAGE:{{"id": {index}}}] '
    content = prefixed.get("content")
    if isinstance(content, list):
        prefixed["content"] = [{"type": "text", "text": prefix}, *content]
    else:
        prefixed["content"] = prefix + (content or "")
    return prefixed


def live_instruction(usage: str) -> str:
    """What the live selector's tool says: the task, the figure and the marker.

    **The ids are on the messages, not in the instruction.**  `with_message_id`
    prefixes every message of the one request with its copy number, and the
    template only points at that marker: a list of the candidates here would be
    a second spelling of the same set, growing with the view on the call with
    the least room to spare.  Every rule that fits the tool's parameters — the
    fixed two, the tool-exchange legality, the empty answer — is `LIVE_REPLY`'s.

    `usage` is the same figure the turn prompt and the status bar show, measured
    against the view the selector is deciding about — the model needs to know
    how full the window is to know how much it has to give up.  The legality
    rule is the schema's rather than the harness's: a model that keeps calls
    and results together writes a history every provider accepts, and a system
    that repaired its answer would be making a decision the model was asked to
    make.
    """
    return render_instruction(LIVE_TEMPLATE, usage=usage)


def parse_live(text: str) -> tuple[list[int] | None, str]:
    """One live selector reply, as the message ids to drop — or why not.

    `[]` is a decision to drop nothing and `None` is no readable decision at
    all: the first is the model saying the turn needs everything, the second is
    an answer asked to correct itself.  An empty object and an absent `compact`
    are both "drop nothing" — the ordinary answer, and the one a model with
    nothing to drop writes.
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"the reply is not JSON ({exc})"
    if not isinstance(raw, dict):
        return None, "the reply is not a JSON object"
    unknown = set(raw) - {"compact"}
    if unknown:
        names = ", ".join(
            sorted(json.dumps(name, ensure_ascii=False) for name in unknown)
        )
        return None, f'only "compact" is read, and the reply also held {names}'
    drop = raw.get("compact")
    if drop is None:
        return [], ""
    if not isinstance(drop, list):
        return None, 'the "compact" field is not a JSON array of message ids'
    ids: list[int] = []
    for value in drop:
        if not _is_id(value):
            return None, f"the compact list holds {value!r}, which is not a message id"
        ids.append(int(value))
    return normalise_ids(ids), ""


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
    """An ISO timestamp as `YYYY-MM-DD HH:MM`; anything else, unchanged.

    Truncated to the minute because the footnote is read by a model deciding
    whether a turn is the one it wants, and seconds are noise at that decision.

    **A stamp that will not parse comes back as it was written**, not as `""`.
    The two are different claims — an empty field says "there is no time here",
    and the raw text says what the column actually holds, which is the answer
    somebody looking at a hand-edited or restored file needs.  It is safe to
    pass on because the footnote is a `json.dumps` of its payload: whatever the
    stamp turns out to be, it is escaped before a model ever reads it.
    """
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    return moment.strftime("%Y-%m-%d %H:%M")


def turn_footnote(
    turn_id: int,
    created_at: str,
    completed_at: str | None,
    channel: str = "",
) -> str:
    """The footnote's *payload* — the part of it that is not the envelope.

    The same string the model reads inside `[TURN:{…}]`, and it is one function
    `turn_note` calls rather than a copy, because the two readers must not
    disagree: a window showing a turn's id beside a message is showing the id a
    keep-list will be written with, and a second spelling of it would be a
    second thing to get wrong.

    `channel` is where the turn came from — `tui` for a window, and the key of a
    worker for a turn one of the agent's own subagents ran.  It is in the
    footnote because an id alone does not say whose turn it is, and the model
    reading a conversation is exactly the reader that cannot tell: two turns
    addressed the same way and answered by different parties look identical
    without it.

    The end collapses to a time when it shares the begin's day, which is the
    ordinary case and the one where repeating the date costs a token to say
    nothing.
    """
    begin = _format_moment(created_at)
    end = _format_moment(completed_at or "")
    day = begin[:10]
    if day and end.startswith(day):
        end = end[11:]
    payload: dict[str, Any] = {"turn_id": int(turn_id)}
    if channel:
        payload["channel"] = channel
    if begin:
        payload["begin"] = begin
    if end:
        payload["end"] = end
    return json.dumps(payload, ensure_ascii=False)


def context_usage(tokens: int, window: int) -> tuple[str, float]:
    """The status bar's context figure, as text and as a percentage.

    **One formatter, because the bar, the selector and the rebuild's answer read
    the same number.**  The TUI's status bar, the live selector's instruction and
    `_context_rebuild`'s report all answer "how full is this conversation", and
    two `f"{tokens:,} ({used:.1f}%)"` spellings would be two chances to round
    differently.  `(text, percentage)` because the bar colours the figure by the
    percentage and the report only prints it.

    A model with no declared window answers the count alone: a percentage of a
    window nobody declared is a number that means nothing (`StatusBar` says the
    same in its own branch).  `""` for zero tokens and no window: there is no
    figure to show.
    """
    if window > 0:
        used = tokens / window * 100
        return f"{tokens:,} ({used:.1f}%)", used
    return (f"{tokens:,}" if tokens else ""), 0.0


def turn_prompt_note(facts: Mapping[str, Any]) -> str:
    """The prompt a turn opens with, built from the facts its caller has.

    **A prompt for the turn about to run, not a record of one.**  It is added to
    the user's message when the turn opens — after the message is appended and
    before the turn's first model call — and removed before the turn is saved,
    so it never enters the row or the index; a list rebuilt from the row does
    not carry it, while the running turn a rebuild carries verbatim does.  The
    `[TURN:]` footnote is what a later reader sees.

    `facts` is the whole payload, and a mapping rather than a fixed argument
    list because this is where a per-turn fact belongs as it appears: today
    `now` and `plan`, tomorrow whatever the turn needs and no later turn
    should remember.  The caller omits what it does not know; a missing key is
    silence, not a zero.
    """
    return wrap(TURN_PROMPT_PREFIX, facts)


def turn_prompt_lead(
    plan: Sequence[Mapping[str, Any]] = (),
) -> str:
    """The `[TURN-PROMPT:{…}]` lead a turn opens with.

    **Two facts, one rule.**  The clock is sampled where the turn starts.  The
    context figure is **not** here: the last measured call is the previous
    turn's, and a rebuild can change the list before this turn's first call, so
    a figure drawn here would describe a context the turn does not run on.  The
    turn's own estimate comes from `_context_rebuild`'s report instead, which is
    computed after the rebuild (`rebuild_report`).

    **The plan is replayed here because a rebuild cannot decide about it.**  It
    is the model's current list (`slife2.libs.turn.plan`), read from the
    conversation's own file before every turn; the rebuild chooses *turns*, and
    a plan kept only as a turn could be dropped.  It rides the same marker for
    the marker's own reason — it is a fact for the turn about to run, not a
    record of it — and because it changes often enough that putting it in the
    system prompt would invalidate the cached prefix on every update.  An empty
    plan is omitted, like every fact the caller does not have.

    The two servers that run turns built this block each; it is here because
    what a turn is told at its start is prompt content, and this module already
    owns the marker's vocabulary.
    """
    facts: dict[str, Any] = {"now": now()}
    if plan:
        facts["plan"] = [dict(item) for item in plan]
    return turn_prompt_note(facts)


def rebuild_report(
    *,
    changed: bool,
    kept: int,
    in_hand: int,
    outside: int,
    recalled: int,
    rerank: str,
    decision: str,
    tokens: str,
) -> str:
    """What `_context_rebuild` answers the model, in one line.

    **The estimate is always there**, because this tool is the model's only
    context figure since the turn prompt stopped carrying one.  `tokens` is
    `context_usage`'s text for the rebuilt list — an estimate of the messages,
    not the provider's measured count; the caller prefixes the `~` that says so.

    **The two "of N" figures answer the two sides of the decision.**  `in_hand`
    is what the caller held; `kept 0 of 5 turn(s) in hand` says a clear, where
    `kept 0` alone reads as an empty history.  `outside` is the whole log beyond
    the context — the set a recall condition filters — and `recalled 0 of 90
    outside` says the search had 90 turns to draw from.  The no-op and failed
    branches already have their own sentences, so they do not need the figures.

    `decision` non-empty names why no decision was made at all: the turn keeps
    the context it had, and `recalled 0` would be the count of a search nobody
    made.  `rerank` non-empty means the recall ran but lost its cross-encoder;
    the fused order stands and the report says so.  `changed` false is the
    ordinary no-op: the decision asked for exactly what was in hand.
    """
    if decision:
        return (
            f"No rebuild: the decision model did not answer ({decision}); "
            f"the turn continues on the context it had; context ~{tokens}."
        )
    # The rerank status is the recall's and belongs to both answers: a recall
    # that ran and then chose nothing is still one whose cross-encoder was
    # lost, and "unchanged" alone would hide the stage that said so.
    tail = f" (rerank {rerank})" if rerank else ""
    if not changed:
        return f"No rebuild: the context is unchanged{tail}; context ~{tokens}."
    return (
        f"kept {kept} of {in_hand} turn(s) in hand, "
        f"recalled {recalled} of {outside} outside{tail}; context ~{tokens}."
    )


def turn_note(
    turn_id: int,
    created_at: str,
    completed_at: str | None,
    channel: str = "",
) -> str:
    """The footnote a rebuilt turn carries, naming its id, its channel and span.

    **This is how a turn id reaches the model at all**, and therefore how a
    keep-list is expressible: the ids a model writes back are the ids it read
    here.  It is added when a message list is *built* and never stored — the turn
    row holds the user's own words — so a footnote can never be persisted twice
    or drift from the row it describes.  `turn_footnote` is the same footnote
    without the marker, for the reader that is a screen.
    """
    return wrap(TURN_PREFIX, turn_footnote(turn_id, created_at, completed_at, channel))


def turn_failed_note(error: str) -> str:
    """The message a turn ends with when it could not go on, and why.

    **An assistant message the harness wrote, and the marker is what says so.**
    Capitals are the harness's (`[TURN:…]`, `[DUMP:…]`), so a model reading its
    own history does not take this for something it said; `by` names the side
    that failed, because a tool's failure is never this — it is text in the
    result the model was given, and the turn carries on.  What reaches here is
    the turn itself stopping, which is the harness's to report.

    **A message rather than a footnote**, and the difference is a design one:
    the turn's own `[TURN:…]` footnote is derived from its row, and a failure is
    not a column — so it rides its own message, which is stored, rebuilt, and
    still there after a restart.  `turn_failed_error` is the screen's reader.
    """
    return wrap(TURN_FAILED_PREFIX, {"by": "harness", "error": error})


def turn_failed_error(content: Any) -> str | None:
    """The error a `[TURN-FAILED:…]` message carries, or `None`.

    The reader a screen uses to draw the failure as the harness's line rather
    than as an answer — the same payload, one spelling.  A content that is not
    a string, or a string that is not that marker, is `None`: a message of any
    other shape is somebody else's.
    """
    if not isinstance(content, str) or not content.startswith(TURN_FAILED_PREFIX):
        return None
    payload = content[len(TURN_FAILED_PREFIX) :]
    if not payload.endswith(TURN_SUFFIX):
        return None
    try:
        found = json.loads(payload[: -len(TURN_SUFFIX)])
    except json.JSONDecodeError:
        return None
    error = found.get("error") if isinstance(found, dict) else None
    return error if isinstance(error, str) else None


def with_note(content: Any, note: str) -> Any:
    """A message's content with the footnote appended.

    Two shapes, because content is a string or a list of parts: text is appended
    to text, and a part-list gains a text part.  Appending to a list would be a
    type error and replacing it would drop an attachment's note, which is the one
    thing the stored turn still says about what was attached.

    Public because there are two places a message gains its footnote and they
    have to agree to the character: this module, when a list is *built*, and the
    agent server, on the turn it has just run and saved (`annotate_turn`).  The
    second is why it is a function rather than three lines inline — a rebuilt
    turn and the same turn in memory differ by a prompt-cache miss when their
    footnotes are not byte-identical.
    """
    if isinstance(content, str):
        # **The person's text is not trimmed, and only the empty case is.**
        # `f"{content} {note}".strip()` stripped the *whole* string, so the
        # one thing it could ever reach was the message's own leading
        # whitespace: a turn that opened with an indented block or a blank line
        # was stored as it was written and read back by the model with that
        # whitespace gone — the row in the transcript and the message in the
        # request were not the same message.
        return f"{content} {note}" if content.strip() else note
    if isinstance(content, list):
        return [*content, {"type": "text", "text": note}]
    return note


def with_lead(content: Any, note: str) -> Any:
    """A message's content with a leading note — the inverse of `without_lead`.

    Two shapes for `with_note`'s reason, and one rule of its own: the note is
    joined by a single space that `without_lead` takes back, so the person's own
    leading whitespace survives the round trip.  A prefix rather than a suffix
    because the turn's prompt is read before the user's words, while the
    `[TURN:]` footnote is a record read after them.
    """
    if isinstance(content, str):
        return f"{note} {content}" if content.strip() else note
    if isinstance(content, list):
        return [{"type": "text", "text": note}, *content]
    return note


def without_lead(content: Any, note: str) -> Any:
    """`with_lead`'s inverse, and the reason it is a pair rather than a strip.

    **Exactly the note and its one joining space**, not `lstrip`: the person's
    text is not trimmed (`with_note`'s rule), and `markers.strip` is a display
    function that strips the whitespace after a marker.  A content that does not
    start with the note is returned unchanged — a turn may have been folded or
    repaired before this ran, and removing something that is not there is the
    one way this could eat a message.
    """
    if isinstance(content, str):
        if content == note:
            return ""
        lead = f"{note} "
        return content[len(lead) :] if content.startswith(lead) else content
    if isinstance(content, list):
        if not content:
            return content
        first = content[0]
        if not isinstance(first, dict) or first.get("type") != "text":
            return content
        text = first.get("text")
        if not isinstance(text, str):
            return content
        if text == note:
            return content[1:]
        # **A lead that was merged into the first part is still the lead.**  A
        # string content becomes the person's text joined to the note, and a
        # mid-turn `attach_image` turns that string into parts — so the first
        # part is `"<note> <prompt>"`, not the note alone.  Requiring an exact
        # match left the marker in the row, the index and every rebuild, which
        # is the one place SDD §5.5 says it never reaches.
        joined = f"{note} "
        if not text.startswith(joined):
            return content
        rest = text[len(joined) :]
        if not rest:
            return content[1:]
        trimmed = dict(first)
        trimmed["text"] = rest
        return [trimmed, *content[1:]]
    return content


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
            str(turn.get("channel") or ""),
        )
        first = dict(messages[0])
        if note:
            first["content"] = with_note(first.get("content"), note)
        built.append(first)
        built.extend(messages[1:])
    return built


def consistent(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Repair orphaned tool calls, so the list is one a provider will accept.

    **The one history state every provider rejects** is an assistant message
    whose tool calls are only partly answered, and a rebuild is a place it can
    appear from nothing: a selection is a subset of turns, and a subset's last
    turn is only well formed by accident.  The repair is idempotent — running it
    on a list that is already correct changes nothing — which is what makes it
    safe to run unconditionally here rather than only where a problem is
    suspected.

    The placeholder is `INTERRUPTED`, and it has two producers: this function,
    and the loop completing a batch an interruption cut short
    (`slife2.libs.turn.loop`, which keeps the batch rather than dropping it).  They
    share the one constant because both write the same sentence into a
    conversation, and a model reading one must not see two descriptions of one
    interruption.

    **The one invariant deliberately absent** is a history ending on `user` or
    `tool` getting a closing assistant line.  That would exist for a history
    that is *sent* as it stands; here a rebuilt list is handed straight
    to a loop that appends the user's message to it before anything is sent, so a
    closing line would be an assistant turn the user's message interrupts.

    **A result answers the call before it, and no other.**  The set of ids is
    therefore collected with the positions they occur at, and a call is answered
    only by a result that appears *after* it.  The old walk collected every
    `tool_call_id` in the list first, so a result in an earlier turn answered a
    call with the same id in a later one — and the later turn's missing answer
    was left unrepaired, the exact state this function exists to fix.  Duplicate
    ids are not this build's writing (an id is one response's, and nothing
    checks uniqueness across turns), but a rebuild is handed stored rows it did
    not write.
    """
    # Collected with positions in one pass first: a result always follows the
    # call it answers, so the call has to be compared against results that come
    # after it, not against the presence of its id somewhere in the list.
    answered: dict[str, list[int]] = {}
    for index, message in enumerate(messages):
        if message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "")
        if call_id:
            answered.setdefault(call_id, []).append(index)
    repaired: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        repaired.append(dict(message))
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, Mapping):
                continue
            call_id = str(call.get("id") or "")
            if not call_id:
                continue
            if any(position > index for position in answered.get(call_id, ())):
                continue
            repaired.append(
                {
                    "role": "tool",
                    "content": INTERRUPTED,
                    "tool_call_id": call_id,
                }
            )
    return repaired
