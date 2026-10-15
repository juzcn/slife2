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

**The base measure is OpenAI's own tokenizer**, `tiktoken`'s `o200k_base` — the
vocabulary GPT-4o and later are trained on, and v1's choice.  It replaces a
script-aware character measure that was the fallback and is now only the
fallback, because counting by hand was wrong by a *direction* that mattered: the
old rule charged 4 characters per token for Latin text and 1 token per CJK
character, so an English sentence came out about 25% high and a Chinese one
about 60% high — both in the direction that spends a window it did not need to.

**And the fallback is load-bearing rather than polite.**  `tiktoken` fetches a
few megabytes of vocabulary over the network the first time it is asked for one,
so a machine that is offline, air-gapped or behind a proxy that blocks it has no
tokenizer to use — and this project's whole point is that its endpoints are
DeepSeek, Qwen, Ollama and whatever gateway the operator points at, most of
which run perfectly well with no internet at all.  So a failure to load is
**not** an error here: it is remembered once per process, logged once, and the
script measure answers from then on.  That is also why nothing resolves at
import — a backend process that never sizes a conversation never touches this.

**No vocabulary is exact for a non-OpenAI endpoint**, and this one is not
claimed to be: a Qwen or DeepSeek model has its own.  What makes `o200k_base`
the better base anyway is that it is a real BPE over the same scripts, so its
error is small and unbiased where a hand-written ratio's is structural.  The
budget it feeds is a fraction of a window, so an estimate that is 5% wrong
spends 5% of a window that was sized for it.

**What the number is for, and what it is not.**  Every caller here is choosing
what *fits*.  None of them reports it as usage, and a caller that finds itself
printing one has made a mistake this module cannot catch.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

#: The vocabulary.  Named rather than inlined so the fallback's log line can say
#: which one it could not get — `o200k_base` is what the current OpenAI models
#: use, and the name is the whole of what a reader needs to check that.
_ENCODING = "o200k_base"

#: Resolved once per process, on the first estimate.  `None` with `_ENCODING_OK`
#: false means "not tried yet"; `None` with it true means "tried and could not be
#: had", which is the state an offline machine stays in for its whole life.
_encoding: Any = None
_encoding_failed = False


def _openai_encoding() -> Any:
    """`o200k_base`, or `None` if this machine cannot get it — the seam a test
    replaces to keep the suite off the network (see `tests/conftest.py`).

    Resolved lazily and never at import: the vocabulary is a few megabytes
    `tiktoken` downloads the first time anybody asks, and a process that never
    sizes a conversation — a model backend, the TUI, most plugins — should not
    pay for one at all.  A failure is remembered as firmly as a success, so a
    machine without it pays the attempt once rather than once per turn.
    """
    global _encoding, _encoding_failed
    if _encoding is None and not _encoding_failed:
        try:
            import tiktoken

            _encoding = tiktoken.get_encoding(_ENCODING)
        except Exception as exc:  # noqa: BLE001 — any failure means "no vocabulary"
            _encoding_failed = True
            logger.warning(
                "tokenizer_unavailable encoding=%s error=%s — falling back to the "
                "script measure, which runs high on both scripts",
                _ENCODING,
                exc,
            )
    return _encoding


#: Characters per token for text that is not CJK, in the fallback measure.  Four
#: is the long-standing figure for English prose under BPE, and the one v1's own
#: docstring names as the fallback rationale.
_CHARS_PER_TOKEN = 4

#: And a CJK character is close to a token of its own — which is why one ratio
#: for the whole string would be wrong by a factor of four on a Chinese
#: conversation, and why this counts the two separately.
_WIDE_TOKENS = 1

#: What one attached image is charged.  It is not text and cannot be counted as
#: any; the figure is v1's, and what it is for is keeping an image-bearing turn
#: from looking free to a budget.  In practice the bytes never reach the db (an
#: attachment is stored as a note), so this is reached only by the live message
#: list.  **This is the one thing the real tokenizer cannot answer**, so it is
#: charged the same way whichever measure is in force.
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


def _by_script(text: str) -> int:
    """The fallback: about how many tokens *text* is, by its own two scripts.

    Used when the vocabulary cannot be had (`_openai_encoding`).  It is the
    measure this module was built on, kept because the machine that needs it is
    the one with no network — which is a supported machine, not a broken one.
    """
    wide = sum(1 for character in text if _is_wide(character))
    narrow = len(text) - wide
    return wide * _WIDE_TOKENS + (narrow + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def estimate_text_tokens(text: str) -> int:
    """About how many tokens `text` is, by the model's tokenizer or its scripts."""
    if not text:
        return 0
    encoding = _openai_encoding()
    if encoding is not None:
        # `encode_ordinary` and not `encode`: the latter *refuses* text spelling
        # a special token — `<|endoftext|>` — and a conversation may contain any
        # text at all, including that, without the estimator raising a turn.
        return len(encoding.encode_ordinary(text))
    return _by_script(text)


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

    Content, a reasoner's thinking, and — for an assistant turn that asked for
    tools — the name and the arguments of each call.

    **`thinking` counts because it goes back on the wire.**  A turn stores it and
    the `openai-completions` backend re-sends it as `reasoning_content` on every
    assistant message once thinking has been asked for (the DeepSeek reasoners
    require it), so a message the model is handed is larger than its visible
    answer by exactly this.  Leaving it out is not a rounding error: the recall
    budget is headroom below a ceiling, and a reasoner's thinking is the largest
    single thing a stored turn carries, so a budget that ignored it would admit
    the very overrun it exists to prevent.

    `arguments` is counted **as text**, not as its decoded length.  On the wire
    it is the JSON string the model emitted, and its own spelling is what the
    next request carries.
    """
    total = _content_tokens(message.get("content"))
    total += estimate_text_tokens(str(message.get("thinking") or ""))
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


def clip_to_tokens(text: str, budget: int) -> str:
    """`text`, cut to fit `budget` tokens — **and never over it**.

    The last line of defence for a tool of ours whose answer is bounded by
    `tool_result.tokens`, and it exists because "bounded" by construction is not
    the same claim as "bounded": every path through such a tool has to come out
    under the budget, including the ones nobody thought about — a refusal that
    quotes back the argument it refused, a header naming a path, a sentence
    about a pattern.  A model can put anything into an argument, so a message
    built out of one is only bounded if something measures it.

    What that failure costs is not one long answer: an answer over the budget is
    a tool result the harness would have to shorten again, and shortening *this
    tool's* answer is what a loop with our own machinery at both ends looks
    like.  So the bound is enforced once, where the answer leaves, rather than
    argued for at each return.

    The cut is **announced**, like every other shortening here, and the marker is
    measured with the text it is attached to: the point of this function is the
    number, so a marker that pushed the answer back over the budget would defeat
    it.  A budget too small to hold even the marker answers with the empty
    string, which is the only answer that fits.
    """
    if estimate_text_tokens(text) <= budget:
        return text
    marker = f"\n[... {len(text)} characters do not fit {budget} tokens]"
    room = budget - estimate_text_tokens(marker)
    if room < 1:
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text_tokens(text[:middle]) <= room:
            low = middle
        else:
            high = middle - 1
    clipped = text[:low] + marker
    # Token counts are not additive — a boundary spanning the join costs a token
    # of its own — so the answer is measured rather than trusted, and backed off
    # until it is under.  It terminates: every step shortens, and the empty
    # string is under any budget this function was called with.
    while low > 0 and estimate_text_tokens(clipped) > budget:
        low -= max(low // 16, 1)
        clipped = text[:low] + marker
    return clipped if estimate_text_tokens(clipped) <= budget else ""
