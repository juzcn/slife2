"""The truncation of a tool result that does not fit, and the address of its file.

**What this is for.**  A tool can return more text than a conversation can
carry, and that text is then carried by *every* request the conversation makes
afterwards.  Nothing here decides whether an output is the right length — that
is the tool designer's business — so the whole of it is kept (a `dump`, in the
cabinet) and what the conversation gets is the **head** of it, cut to
`tool_result.bytes`, and an address: the `[DUMP:…]` marker names the call, the
file's size and its path, and the model reads the rest with `dump_read` or
finds a part of it with `dump_grep`.

**The bound is bytes, not tokens.**  This is a size cap on one result, not a
token accounting of the conversation; the recall's `ceiling` is the token guard.
Bytes need no vocabulary and cost one expression to measure, and a UTF-8 byte
tracks text volume across scripts better than a character does.

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

**A loop with no cabinet gets `cut` instead.**  An agent task has no cabinet
client and its messages are not recorded, so a result over the budget is cut to
the same bound with a marker that promises no file; the turn continues and the
tail is gone.  The bound is the guarantee in both cases; the dump is the
recovery convenience.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from slife2.libs.store.index import storable
from slife2.libs.turn.messages import ToolCall

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


def text_size(text: str) -> int:
    """`text`'s size as the model receives it, in UTF-8 bytes.

    `backslashreplace` is the same rule `as_tool_text` applies: a lone surrogate
    cannot be encoded at all, and it travels as the `\\uXXXX` escape it would
    have been.  The measure and the message therefore count the same bytes.
    """
    return len(text.encode("utf-8", "backslashreplace"))


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
    # **The head is measured in the units the file holds.**  The cabinet writes
    # `storable(body)`, and a surrogate pair is two code units in the raw string
    # but one character in the file — so slicing before the normalization made
    # the announced "first N characters" name a different offset than
    # `dump_read(mode="char")` uses, and an odd cut could leave half a pair.
    text = storable(text)
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

    if text_size(render(0)) > budget:
        raise ValueError(
            f"a `tool_result.bytes` window of {budget} cannot hold the "
            f"window's own address ({text_size(render(0))} bytes), so the "
            f"output could not be read back — raise it"
        )
    # A byte budget bounds the character count too (every character is at least
    # one byte), so `budget` is a safe upper bound for the search; every probe
    # is a slice of the head, never the whole output.
    low, high = 0, min(total_chars, budget)
    while low < high:
        middle = (low + high + 1) // 2
        if text_size(render(middle)) <= budget:
            low = middle
        else:
            high = middle - 1
    return render(low)


def cut(text: str, budget: int) -> str:
    """The head of an output that could not be kept: no file, no address.

    The difference from `window` is the promise.  A `[DUMP:…]` marker names a
    file that exists; this marker does not claim one, so a loop with no cabinet
    (an agent task) can still hold the bound and end the turn honestly.  The
    tail is gone; a model that needs it calls the tool again.

    **A budget that cannot show anything is a failure, as it is for `window`.**
    An empty answer with no truncation marker is a confident negative — the
    model reads a tool that produced nothing — so the two fitters share one
    policy: raise, and the turn fails visibly rather than recording an empty
    result as the tool's own.
    """
    marker = "\n[... the rest of this output was not shown]"
    room = budget - text_size(marker)
    if room < 1:
        raise ValueError(
            f"a `tool_result.bytes` window of {budget} cannot hold the cut's "
            f"own marker ({text_size(marker)} bytes) — raise it"
        )
    body = text.encode("utf-8", "backslashreplace")[:room].decode("utf-8", "ignore")
    if not body:
        if text:
            # The marker fits but the output's first character does not: a
            # multi-byte character cut mid-sequence decodes to nothing, and an
            # empty head beside the marker is the same confident negative.
            raise ValueError(
                f"a `tool_result.bytes` window of {budget} leaves no room for "
                f"even one character of the output beside the cut's marker — "
                f"raise it"
            )
        return ""
    return f"{body}{marker}"


def cut_for(budget: int) -> Compress:
    """A `Compress` that cuts rather than dumps, for a loop with no cabinet.

    The async shape is the loop's (`Compress`); the cut itself is the same one
    an address-less result gets.  A result that fits goes in as it came.
    """

    async def fit(_call: ToolCall, text: str) -> str:
        return text if text_size(text) <= budget else cut(text, budget)

    return fit
