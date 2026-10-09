"""How large a piece of a conversation is, when nobody has measured it.

**An estimate is not a measurement, and this module only ever produces the
first.**  What slife2 knows for certain about a conversation's size is
`context_tokens` — the last model call's own prompt plus completion, which the
db stores on every turn and which the provider reports rather than guesses.  That
number drives the ceiling check and the status bar, and nothing here may stand
in for it.

What no measurement can answer is how large a turn *would* be: a candidate a
recall is considering, or a turn a selection is about to spend its budget on.
That is the whole of this module's job, and the reason it is one module rather
than three functions in three places is v1's: counting, the trim's stop
condition and the recall's token budget were the same function there, so the
three could not come to disagree about how big something is.

**Why not the real tokenizer.**  v1 counts with `tiktoken`'s `o200k_base` and
provisions the vocabulary at install time.  That is the right answer for a system
whose models are OpenAI's, and the wrong one here: slife2's endpoints are
DeepSeek, Qwen, Ollama and whatever gateway the operator points at, and
`o200k_base` is not one of their vocabularies either — so the dependency would
buy a number that is exact about a tokenizer nobody is using, at the price of a
multi-megabyte vocabulary fetched over the network at first use.  What is left is
a script-aware character measure, and it is honest about being one.  The budget
it feeds is a fraction of a window, so an estimate that is 10% wrong spends 10%
of a window that was sized for it.

**What the number is for, and what it is not.**  Every caller here is choosing
what *fits*.  None of them reports it as usage, and a caller that finds itself
printing one has made a mistake this module cannot catch.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

#: Characters per token for text that is not CJK.  Four is the long-standing
#: figure for English prose under BPE, and the one v1's own docstring names as
#: the fallback rationale.
_CHARS_PER_TOKEN = 4

#: And a CJK character is close to a token of its own — which is why one ratio
#: for the whole string would be wrong by a factor of four on a Chinese
#: conversation, and why this counts the two separately.
_WIDE_TOKENS = 1

#: What one attached image is charged.  It is not text and cannot be counted as
#: any; the figure is v1's, and what it is for is keeping an image-bearing turn
#: from looking free to a budget.  In practice the bytes never reach the db (an
#: attachment is stored as a note), so this is reached only by the live message
#: list.
_IMAGE_TOKENS = 200


def _is_wide(character: str) -> bool:
    """Whether a character is one a tokenizer spends about one token on.

    The ranges are the ones `wcwidth` calls double-width, which is the closest
    thing to a standard answer to this question.  Latin, Cyrillic, Greek and
    Arabic are not in them and are counted at `_CHARS_PER_TOKEN`; Han, Kana,
    Hangul and the fullwidth forms are.
    """
    code = ord(character)
    return (
        0x1100 <= code <= 0x115F  # Hangul Jamo
        or 0x2E80 <= code <= 0x303E  # CJK radicals and punctuation
        or 0x3041 <= code <= 0x33FF  # Kana, Hangul Compatibility Jamo, CJK
        or 0x3400 <= code <= 0x4DBF  # CJK Unified Ideographs Extension A
        or 0x4E00 <= code <= 0x9FFF  # CJK Unified Ideographs
        or 0xA000 <= code <= 0xA4CF  # Yi
        or 0xAC00 <= code <= 0xD7A3  # Hangul syllables
        or 0xF900 <= code <= 0xFAFF  # CJK compatibility ideographs
        or 0xFE30 <= code <= 0xFE6F  # CJK compatibility forms
        or 0xFF00 <= code <= 0xFF60  # fullwidth forms
        or 0xFFE0 <= code <= 0xFFE6  # fullwidth signs
        or 0x20000 <= code <= 0x3FFFD  # the supplementary ideographic planes
    )


def estimate_text_tokens(text: str) -> int:
    """About how many tokens `text` is, by its own two scripts."""
    if not text:
        return 0
    wide = sum(1 for character in text if _is_wide(character))
    narrow = len(text) - wide
    return wide * _WIDE_TOKENS + (narrow + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _content_tokens(content: Any) -> int:
    """One message's content, whether it is text or content parts."""
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "text":
            total += estimate_text_tokens(str(part.get("text") or ""))
        else:
            # An image, or a part of some future kind.  Charged as one image
            # rather than as nothing: what a part this build does not know how
            # to count must not be a part that looks free.
            total += _IMAGE_TOKENS
    return total


def estimate_message_tokens(message: Mapping[str, Any]) -> int:
    """About how many tokens one message costs in a request.

    The three things a message can carry, which is what v1 counts too: its
    content, and — for an assistant turn that asked for tools — the name and the
    arguments of each call.

    `arguments` is counted **as text**, not as its decoded length.  On the wire
    it is the JSON string the model emitted, and its own spelling is what the
    next request carries.
    """
    total = _content_tokens(message.get("content"))
    for call in message.get("tool_calls") or ():
        if not isinstance(call, Mapping):
            continue
        function = call.get("function")
        if not isinstance(function, Mapping):
            continue
        total += estimate_text_tokens(str(function.get("name") or ""))
        arguments = function.get("arguments")
        total += estimate_text_tokens(
            arguments
            if isinstance(arguments, str)
            else json.dumps(arguments or {}, ensure_ascii=False)
        )
    return total


def estimate_turn_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """About how many tokens one turn costs, floored at one.

    The floor is v1's and it is load-bearing in the direction people get wrong:
    a turn that weighs nothing is a turn every budget can afford, so a recall
    would fill its window with empty turns and report success.  One is a lie, but
    it is a lie that bounds the count.
    """
    return max(sum(estimate_message_tokens(message) for message in messages), 1)


def count_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """About how many tokens a whole conversation is, floored at one.

    The same floor as `estimate_turn_tokens` and for the same reason, one level
    up: a conversation that weighs nothing is a conversation no ceiling holds.
    """
    return max(sum(estimate_message_tokens(message) for message in messages), 1)
