"""The window over a tool result that does not fit, and the record of it.

**What this is for.**  A tool can return more text than a conversation can
carry, and that text is then carried by *every* request the conversation makes
afterwards.  Nothing here decides whether an output is the right length — that
is the tool designer's business — so the whole of it is kept (a `dump`, in the
cabinet) and what the conversation gets is a **window** over it: lines of the
original, chosen by a discriminator under the conversation in hand, within
`tool_result.tokens`.

**Three things here are decisions rather than mechanisms.**

* **The reply is lines, not text.**  The model is asked which lines of the
  output belong in this conversation and is never asked to write any of it — so
  what arrives is the tool's own words or it is nothing, and a reply that is
  wrong costs a re-read rather than a fact no tool said.  That is DESIGN.md §2.4's
  property one discriminator over: there the answer is turn ids, here it is line
  ranges, and the harness does the cutting both times.
* **An unreadable reply is not a decision to do nothing.**  `Rejected` against
  "none of it bears on this conversation", the same split the rebuild makes: one
  is a failure and the other is an answer, and they are logged differently.  Both
  end in the same fallback — the reader's first page, which is what a model would
  have got by asking — because every way this can go wrong ends in the
  conversation carrying *something* it can read past, never in a retry.
* **The address is the tool call id**, which is the one identifier both sides of
  the arrangement already carry: the conversation's own tool message holds it and
  the cabinet's dump is keyed by it, so nothing has to be invented, matched up or
  kept in step — and, the constraint that decided it, nothing has to exist before
  the turn is saved, which a turn id does not.

**What is deliberately not here.**  The window is a *bound on tokens*, so the
call that chooses it has an input that can itself be enormous — the result, the
conversation, and the template.  That is parked as a known small-probability
gap rather than half-solved: the honest fix needs the model's own window in
hand, which is a fact about a provider rather than about this module.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from slife2.libs.turn.messages import Message, ToolCall
from slife2.libs.turn.prompt import render
from slife2.libs.turn.tokens import (
    clip_to_tokens,
    estimate_lines_tokens,
    estimate_text_tokens,
)

#: How the harness is asked to fit one tool result into a conversation: the call
#: it came from, the text the tool answered with, and **the conversation in hand
#: at that moment** — in return for the text the conversation carries.  The
#: messages are handed over because they are the condition the choice is made
#: under: which lines of an output this conversation needs is a fact about the
#: conversation, and the loop is the only party holding it.
#:
#: **A callback rather than a method**, for the reason `approve` and `deliver`
#: are: the loop does not know what the conversation is about.  A loop with no
#: callback passes the result through unchanged, which is its answer to "nobody
#: said otherwise".
Compress = Callable[[ToolCall, str, Sequence[Message]], Awaitable[str]]

#: The discriminator's instruction, shipped as a template beside `recall.j2` and
#: for the reason that file gives: it is text with holes in it.
INSTRUCTION_TEMPLATE = "compact.j2"

#: What the model is asked to reply with.  Rendered into the instruction rather
#: than described in prose, because a field's *wording* is what a model copies
#: and a description is what it paraphrases (`RECALL_REPLY` makes the same
#: choice for the same reason).
COMPACT_REPLY: dict[str, Any] = {
    "lines": (
        "The lines of this output to keep in the conversation, as [first, last] "
        "pairs, 1-based and inclusive, best first. [] when none of it bears on "
        'this conversation, or "binary" when the output is not text at all.'
    ),
}

#: The harness's own annotation.  **A name in capitals is the harness's**, by
#: the convention DESIGN.md §2.4 states and `[TURN:{…}]` wears — nothing reads the case,
#: and what the capitals are for is a reader telling an annotation from
#: something somebody said.  Nothing goes inside the brackets but the payload,
#: because the payload is the part a model copies out.
MARKER = "[DUMP:"
MARKER_END = "]"

#: What the model may say in place of a list of lines.  Two words, and both are
#: answers: `[]`'s is "none of this bears on the conversation".
NOT_TEXT = "binary"


class Rejected:
    """The answer "this reply is not the object that was asked for".

    A sentinel rather than `None`, for `slife2.libs.turn.context`'s reason: `None`
    is a *value* in this field — it is what "no lines" is not but "binary" also
    is not — and collapsing a malformed reply into an empty selection would make
    a failure indistinguishable from an answer.
    """


REJECTED = Rejected()

#: What a reply may be read out of.  A model answers with text; a caller
#: testing this module hands it the object.
Reply = str | Mapping[str, Any]


@dataclass(frozen=True)
class Decision:
    """What one discriminator reply decided about one output's lines.

    `lines` is what the conversation should carry, **best first** — the order is
    used, because a selection that still goes over the window is trimmed from its
    end — and an empty list is the answer "none of this bears on the
    conversation".  `binary` says the model judged the output not to be text at
    all, which is the one case with nothing to select.
    """

    lines: list[tuple[int, int]]
    binary: bool = False

    @property
    def keeps_nothing(self) -> bool:
        """Whether this reply puts no lines in the conversation."""
        return not self.lines


def read_reply(reply: Reply) -> Decision | Rejected:
    """What one reply decided, or `REJECTED` for one this build cannot read.

    **A key this build does not know is dropped rather than refused**: the reply
    surface is written into the instruction, and a model that added a field of
    its own has still answered the question that was asked.  A *value* of the
    wrong type is different — that is a reply whose selection cannot be read, and
    acting on half of it would be worse than the fallback.

    `bool` is excluded from the numbers on the way through, because it is a
    subclass of `int` and `true` would otherwise become line 1.
    """
    if isinstance(reply, str):
        try:
            decoded = json.loads(reply)
        except (ValueError, TypeError):
            return REJECTED
    else:
        decoded = reply
    if not isinstance(decoded, Mapping):
        return REJECTED
    raw = decoded.get("lines")
    if raw is None:
        # Absent is not "keep nothing": a reply that answered nothing at all is
        # a reply this build does not act on, and the fallback is cheaper than
        # an empty conversation window.
        return REJECTED
    if isinstance(raw, str):
        if raw.strip() == NOT_TEXT:
            return Decision(lines=[], binary=True)
        return REJECTED
    if not isinstance(raw, Sequence):
        return REJECTED
    ranges: list[tuple[int, int]] = []
    for pair in raw:
        if (
            not isinstance(pair, Sequence)
            or isinstance(pair, str)
            or len(pair) != 2
            or not all(
                isinstance(value, int) and not isinstance(value, bool) for value in pair
            )
        ):
            return REJECTED
        first, last = int(pair[0]), int(pair[1])
        if first < 1 or last < first:
            return REJECTED
        ranges.append((first, last))
    return Decision(lines=ranges)


def lines_of(body: str) -> list[str]:
    """One output, as the lines every address in this arrangement counts.

    Spelt once because two renderings read it: the instruction the discriminator
    is shown and the window that goes into the conversation.  **A number is a
    line of the tool's output**, so the two have to agree — a model that names
    line 1204 and a reader asked for line 1204 must mean the same characters.
    """
    return body.splitlines()


def numbered(lines: Sequence[str], *, start: int = 1) -> str:
    """Lines as a model reads them: the number, then the line.

    Right-aligned to the widest number shown, so a column of addresses is
    readable rather than merely present.  Two spaces rather than a mark, because
    this spelling is *not* a grep's: the instruction's own arms are the only
    thing that says which lines matched.
    """
    if not lines:
        return ""
    width = len(str(start + len(lines) - 1))
    return "\n".join(
        f"{number:>{width}}  {text}" for number, text in enumerate(lines, start)
    )


def cut(lines: Sequence[str], decision: Decision, budget: int) -> list[tuple[int, str]]:
    """The lines a decision keeps, in the order it named them, within `budget`.

    **The order is used, exactly as it is for a keep-list**: a selection that
    still goes over the window is trimmed from its *end*, so a model's ranking
    survives a window it misjudged.  Ranges are clipped to the output — a line
    number past the end is not an error, it is a model that counted wrong — and
    a line named twice is kept once, in its first position, because the address
    is what it is.

    The budget is spent on the lines alone; the sentence and the marker that go
    round them are the caller's, and `window` is where the whole answer is
    measured.
    """
    total = len(lines)
    kept: list[tuple[int, str]] = []
    seen: set[int] = set()
    for first, last in decision.lines:
        for number in range(max(first, 1), min(last, total) + 1):
            if number in seen:
                continue
            seen.add(number)
            kept.append((number, lines[number - 1]))
            if estimate_lines_tokens(kept) > budget:
                kept.pop()
                return kept
    return kept


def window(
    *,
    call: str,
    lines: Sequence[str],
    kept: Sequence[tuple[int, str]],
    chosen: bool,
    binary: bool = False,
    budget: int,
) -> str:
    """The text a conversation carries in place of a whole tool output.

    A marker naming the call and how long the output is, one sentence saying what
    this is, and then the lines themselves — numbered, because those numbers are
    how the rest is asked for.  **The whole answer is measured against the
    budget and clipped if it has to be**, for the reason `clip_to_tokens` gives:
    an answer over the window would be a tool result the harness has to shorten
    again, and shortening this one is the loop the arrangement exists to
    prevent.

    **And the address is not the part that may be shed.**  A clip answers with a
    shorter *answer*, so it may take the lines and the sentence — but the
    `[DUMP:…]` marker is how a model names the output to read the rest back, and
    a window whose marker was cut is a conversation that has given up the bound
    *and* broken the address while looking like success.  A budget too small to
    hold it is therefore a failure and not a shorter window: nothing here can
    decide to spend fewer tokens than the address costs, so it says so
    (`tool_result.tokens` is validated as `>= 1` and nothing else, so an operator
    can configure this; see the raise below).
    """
    total = len(lines)
    head = f"{MARKER}{json.dumps({'call': call, 'total': total}, ensure_ascii=False)}{MARKER_END}"
    if binary:
        said = (
            "this output is not text, so there is nothing here to read in it. "
            "The whole of it is kept, and `dump_read` will not open it."
        )
    elif not kept and not chosen:
        said = (
            "nothing in this output bears on this conversation. The whole of it "
            "is kept, and `dump_read` opens it by a word or by a line number."
        )
    elif not kept:
        # **Chosen and still empty**, which is not the same answer: the lines the
        # model named are each longer than the whole window, so none of them fits
        # on its own. Saying "nothing here bears on this conversation" would be a
        # different and false claim, and the model reading it would stop looking.
        said = (
            "the lines this conversation needs are each longer than the window "
            "on their own. The whole of it is kept, and `dump_read` opens it by "
            "a word or by a line number."
        )
    else:
        where = ", ".join(_ranges(kept))
        said = (
            f"{where} of {total}, chosen for this conversation. The rest is "
            f"kept, and `dump_read` opens it by a word or by a line number."
        )
    body = numbered_lines(kept)
    answer = f"{head} {said}" + (f"\n\n{body}" if body else "")
    # **The clip must leave the address standing, or this is not a window.**
    # `clip_to_tokens` shortens from the end and protects nothing: at a small
    # enough budget it cut the marker mid-JSON (the model could not name the dump
    # to ask for the rest) and at a smaller one still it answered with the empty
    # string — a tool result that says nothing at all, in a conversation that
    # carries it forever.  The state is the one SDD Appendix C puts on the side of the
    # steps that must succeed: an output nobody can read back is worse than a
    # turn that failed, because it looks like an answer.
    clipped = clip_to_tokens(answer, budget)
    if not clipped.startswith(head):
        raise ValueError(
            f"a `tool_result.tokens` window of {budget} cannot hold the window's "
            f"own address ({estimate_text_tokens(head)} tokens), so the output "
            f"could not be read back — raise it"
        )
    return clipped


def numbered_lines(kept: Sequence[tuple[int, str]]) -> str:
    """Kept lines as a model reads them, carrying their **original** numbers.

    The number is the one the reader will be asked for, so it is the output's
    line number and not the window's: a window that renumbered from one would
    give a model two addresses for one line, and the one it copied out of the
    conversation would reach the wrong text.
    """
    if not kept:
        return ""
    width = max(len(str(number)) for number, _text in kept)
    return "\n".join(f"{number:>{width}}  {text}" for number, text in kept)


def _ranges(kept: Sequence[tuple[int, str]]) -> list[str]:
    """The kept lines as runs, so a sentence about them is short."""
    runs: list[list[int]] = []
    for number, _text in kept:
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return [str(first) if first == last else f"{first}-{last}" for first, last in runs]


def instruction(
    *,
    tool: str,
    body: str,
    budget: int,
    conversation: str,
    user_input: str,
) -> str:
    """The discriminator's instruction for one result, rendered from the template.

    The result is handed over **numbered**, which is the whole of how a reply can
    be an address rather than a copy, and its size is handed over as the measured
    number rather than as a shape a model has to guess at.  `conversation` is the
    turns before this result, rendered as text: the template judges the result
    under it, and the newest turn's tool calls cannot be sent as history because
    their results do not exist yet.
    """
    lines = lines_of(body)
    return render(
        INSTRUCTION_TEMPLATE,
        tool_name=tool or "a tool",
        result=numbered(lines),
        result_tokens=estimate_text_tokens(body),
        budget=budget,
        conversation=conversation or "(no earlier turns)",
        user_input=user_input,
        compact_reply=json.dumps(COMPACT_REPLY, indent=2, ensure_ascii=False),
    )
