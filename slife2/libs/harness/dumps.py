"""The truncation of a tool result that does not fit, and the address of its file.

**What this is for.**  A tool can return more text than a conversation can
carry, and that text is then carried by *every* request the conversation makes
afterwards.  Nothing here decides whether an output is the right length — that
is the tool designer's business — so the whole of it is kept (a `dump`, in the
cabinet) and what the conversation gets is the **head** of it, cut to
`tool_result.tokens`, and an address: the `[DUMP:…]` marker names the call, the
file's size and its path, and the model reads the rest with `dump_read` or
finds a part of it with `dump_grep`.

**Why the head and not a selection.**  Choosing the *interesting* lines is what
a discriminator model call used to do here.  It was expensive — every shortened
result paid a second model call carrying the result and the conversation — and
it was unreliable in the way that matters: its input could itself be the whole
output, so the step meant to protect the context could overflow it.  A
truncation is a fixed rule with a fixed cost, and the model can undo it by
reading the file.  The head is also what tells a model what kind of output it
is: text, JSON, a log, or bytes that are not text at all.  A model that sees
the latter has no reason to read on, and if it does, the readers are bounded.

**The address is the tool call id**, which both sides already carry: the
conversation's own tool message holds it and the cabinet's dump is keyed by it.
Nothing has to be invented or kept in step.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from slife2.libs.turn.messages import ToolCall
from slife2.libs.turn.tokens import estimate_text_tokens

#: How the harness fits one tool result into a conversation: the call it came
#: from and the text the tool answered with.  **A callback rather than a
#: method**, for the reason `approve` and `deliver` are: the loop does not know
#: which cabinet holds the dump.  A loop with no callback passes the result
#: through unchanged, which is its answer to "nobody said otherwise".
Compress = Callable[[ToolCall, str], Awaitable[str]]

#: The harness's own annotation.  **A name in capitals is the harness's**, by
#: the convention SDD §5.5 states and `[TURN:{…}]` wears — nothing reads the
#: case, and what the capitals are for is a reader telling an annotation from
#: something somebody said.  Nothing goes inside the brackets but the payload,
#: because the payload is the part a model copies out.
MARKER = "[DUMP:"
MARKER_END = "]"


#: The first head size the fit tries, in characters.  A budget's worth of
#: text is usually between this and a few doublings of it; starting here keeps
#: the search from slicing a multi-megabyte output in half just to find the
#: few thousand characters that fit.
_HEAD_PROBE = 1024


def window(*, call: str, text: str, where: str, size: int, budget: int) -> str:
    """The head of one output, with the path of the whole.

    The marker is `[DUMP:{"call": …, "bytes": …, "file": …}]` and the sentence
    after it says how much was cut and which tools read the rest.  The path is
    the file the cabinet wrote, **relative to the agent's data directory** (the
    working directory its own tools run in), so a marker stays true when the
    home is moved, and a reader can open it or pass it to another tool.
    **The whole answer is measured against the budget and the head is what
    gives way**: the marker and the sentence are the address, and an answer
    whose marker was clipped is a conversation that has given up the bound
    *and* broken the address while looking like success.  A budget too small to
    hold the address is therefore a failure, not a shorter window.

    **Nothing in the marker is counted.**  `bytes` is the size of the file the
    cabinet wrote, read with one `stat`; a character count would be O(1) but is
    not needed to read the file, and a line count would mean scanning the whole
    output — work the marker does not need.  So the marker carries no counts:
    the head length says where this window stops, and `dump_read` answers how
    many lines and characters there are when a model asks to read them.
    """
    total_chars = len(text)
    head = (
        f"{MARKER}"
        f"{json.dumps({'call': call, 'bytes': size, 'file': where}, ensure_ascii=False, separators=(',', ':'))}"
        f"{MARKER_END}"
    )

    def render(shown: int) -> str:
        said = (
            f" Too large for the conversation: the first {shown} characters of "
            "the output text follow. The whole output is saved at the file path "
            "in the marker; read it with `dump_read` (mode line or char) or "
            "find text with `dump_grep`."
        )
        body = text[:shown]
        return f"{head}{said}" + (f"\n\n{body}" if body else "")

    if estimate_text_tokens(render(0)) > budget:
        raise ValueError(
            f"a `tool_result.tokens` window of {budget} cannot hold the "
            f"window's own address ({estimate_text_tokens(render(0))} tokens), "
            f"so the output could not be read back — raise it"
        )
    # **Grow to the fit, do not halve the output.**  A probe costs a slice of
    # what it measures, and starting at `len(text)` would copy half of a huge
    # output on the first try.  Doubling from `_HEAD_PROBE` reaches a budget's
    # worth of characters in a few steps and never touches the rest.
    low, high = 0, min(total_chars, _HEAD_PROBE)
    while True:
        fits = estimate_text_tokens(render(high)) <= budget
        if fits and high < total_chars:
            low = high
            high = min(high * 2, total_chars)
            continue
        break
    if fits:
        shown = high
    else:
        while low < high:
            middle = (low + high + 1) // 2
            if estimate_text_tokens(render(middle)) <= budget:
                low = middle
            else:
                high = middle - 1
        shown = low
    # Token counts are not additive — a boundary spanning the join costs a token
    # of its own — so the answer is measured rather than trusted, and backed off
    # until it is under.  It terminates: every step shortens, and zero is under
    # any budget that got past the guard above.
    answer = render(shown)
    while shown > 0 and estimate_text_tokens(answer) > budget:
        shown -= max(shown // 16, 1)
        answer = render(shown)
    return answer
