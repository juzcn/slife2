"""How large a piece of a conversation is, when nobody has measured it.

**An estimate is not a measurement, and this module only ever produces the
first.**  What slife2 knows for certain about a conversation's size is
`context_tokens` — the last model call's own prompt plus completion, which the
db stores on every turn and which the provider reports rather than guesses.  That
number drives the ceiling check and the status bar, and nothing here may stand
in for it.

**One decision in this system is genuinely in tokens: the context rebuild's
recall budget.**  A candidate a recall is considering is a turn that has not been
sent, so nothing has measured it; the budget is a fraction of a model window, and
the estimate is how the selection decides what fits.  Every other bound is a
size: a tool result is cut by bytes, a page is fitted by bytes, and
`tool_result.bytes` is the one number those answer to.  This module therefore
serves the recall and nothing else, which is why it can afford a real tokenizer.

**The base measure is OpenAI's own tokenizer**, `tiktoken`'s `o200k_base` — the
vocabulary GPT-4o and later are trained on.  **It ships with the package**:
`tiktoken_cache/` holds the file tiktoken expects, and `_openai_encoding` points
`TIKTOKEN_CACHE_DIR` at it before asking for the encoding.  Resolving it is a
local read and parse — no network, no timeout, no proxy — about 186 ms once per
process, and the recall's cost runs off the event loop.

**The fallback remains the last resort**, not the ordinary path.  If the bundled
vocabulary cannot be read — a broken install, or a tiktoken that no longer knows
the name — the script measure answers: ASCII at four characters per token, every
non-ASCII character at one.  That is deliberately conservative; a real BPE packs
common CJK pairs into one token, so this runs high there.  It is one rule and no
table of ranges, because a table is always missing the next block: the old one
omitted emoji and made the fallback *under*count them by four to five times,
which is the one direction a budget cannot afford.

**No vocabulary is exact for a non-OpenAI endpoint**, and this one is not
claimed to be: a Qwen or DeepSeek model has its own.  What makes `o200k_base`
the better base anyway is that it is a real BPE over the same scripts, so its
error is small and unbiased where a hand-written ratio's is structural.  The
budget it feeds is a fraction of a window, so an estimate that is 5% wrong
spends 5% of a window that was sized for it.

**A failed read is remembered, but not forever.**  Retrying on every message
would be one failed open per message; remembering it for the process's whole
life would turn a reinstalled file into a permanent overestimate.  The attempt
is retried after `_RETRY_AFTER`.  Nothing resolves at import: a process that
never sizes a conversation never touches this.

**What the number is for.**  The recall chooses what fits; none of it reports
usage, and a caller that finds itself printing one has made a mistake this
module cannot catch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The vocabulary.  Named rather than inlined so the fallback's log line can say
#: which one it could not get — `o200k_base` is what the current OpenAI models
#: use, and the name is the whole of what a reader needs to check that.
_ENCODING = "o200k_base"

#: Where tiktoken's loader looks for it, and the name it expects: the cache file
#: is keyed by the SHA-1 of the URL `read_file_cached` would fetch.  The name is
#: derived rather than written down, so the URL and the file name stay one
#: statement.  The data directory's README records the content hash and source.
_VOCAB_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
_CACHE_KEY = hashlib.sha1(_VOCAB_URL.encode()).hexdigest()

#: The package directory holding the cache file.  A normal wheel is unpacked, so
#: `__file__` is a real path; a zipped import is not a mode this project ships.
_BUNDLED_CACHE = Path(__file__).with_name("tiktoken_cache")

#: The content hash the installed tiktoken pins for the vocabulary file
#: (`tiktoken_ext.openai_public.o200k_base`, tiktoken 0.14.0).  Verified before
#: the load, because tiktoken answers a mismatched cache file by deleting it and
#: fetching — the network path this module promises never to take.  The bundle's
#: README records the same hash and the procedure for updating it.
_BUNDLED_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"

#: Resolved once per process, on the first estimate.  `None` means "not resolved
#: yet"; `_failed_at` says when the last attempt failed, which is what the retry
#: cooldown is measured from.
_encoding: Any = None
_failed_at: float | None = None

#: How long a failed read is remembered before the next attempt.  Long enough
#: that a broken install does not pay a file open per message; short enough that
#: a repair does not degrade this process for its whole life.
_RETRY_AFTER = 600.0


def _openai_encoding() -> Any:
    """`o200k_base`, or `None` if this machine cannot read it — the seam a test
    replaces to keep the suite off the vocabulary (see `tests/conftest.py`).

    Resolved lazily and never at import: the file is a few megabytes to parse,
    and a process that never sizes a conversation — a model backend, the TUI,
    most plugins — should not pay for one at all.

    **The bundle is a cache directory, not a format of ours.**  tiktoken's
    `read_file_cached` checks `TIKTOKEN_CACHE_DIR` before it fetches, and the
    bundled file is named with the SHA-1 of the URL it would fetch, so pointing
    the variable at the package directory is the whole of the integration.
    `setdefault` leaves an operator's own cache directory alone.

    **The read is proven local before tiktoken is asked.**  A missing or
    hash-mismatched cache file sends tiktoken 0.14.0 to `requests.get` with *no
    timeout*, so a broken install on a black-holed network would hang the turn
    rather than reach the script measure.  The file tiktoken will read is
    therefore checked here first — present, and the content hash tiktoken pins —
    and a broken install falls back immediately, through the same warning and
    cooldown a failed load always had.
    """
    global _encoding, _failed_at
    if _encoding is None:
        now = time.monotonic()
        if _failed_at is not None and now - _failed_at < _RETRY_AFTER:
            return None
        try:
            import tiktoken

            # The directory tiktoken will read: an operator's own when they set
            # one, the bundle otherwise — and the file in it is held to the
            # same check either way, so no configuration of this variable can
            # turn the load into a fetch.
            cache_dir = os.environ.get("TIKTOKEN_CACHE_DIR")
            if cache_dir is None:
                cache_dir = str(_BUNDLED_CACHE)
                os.environ.setdefault("TIKTOKEN_CACHE_DIR", cache_dir)
            bundle = Path(cache_dir) / _CACHE_KEY
            if not bundle.is_file():
                raise FileNotFoundError(
                    f"the vocabulary cache file is missing: {bundle}"
                )
            if hashlib.sha256(bundle.read_bytes()).hexdigest() != _BUNDLED_SHA256:
                raise ValueError(
                    f"the vocabulary cache file does not match the hash "
                    f"tiktoken pins: {bundle}"
                )
            _encoding = tiktoken.get_encoding(_ENCODING)
        except Exception as exc:  # noqa: BLE001 — any failure means "no vocabulary"
            _failed_at = now
            logger.warning(
                "tokenizer_unavailable encoding=%s error=%s — falling back to the "
                "script measure, which charges every non-ASCII character",
                _ENCODING,
                exc,
            )
    return _encoding


#: Characters per token for ASCII text, in the fallback measure.  Four is the
#: long-standing figure for English prose under BPE, and the rationale the
#: fallback runs on.
_CHARS_PER_TOKEN = 4

#: What one attached image is charged.  It is not text and cannot be counted as
#: any; the figure is a fixed charge, and what it is for is keeping an
#: image-bearing turn from looking free to a budget.  In practice the bytes never
#: reach the db (an attachment is stored as a note), so this is reached only by
#: the live message list.  **This is the one thing the real tokenizer cannot
#: answer**, so it is charged the same way whichever measure is in force.
_IMAGE_TOKENS = 200

#: The one rule of the fallback: ASCII text at `_CHARS_PER_TOKEN` characters per
#: token, every non-ASCII character at one.  **No range table**, because a table
#: is always missing the next block — the old one omitted emoji (U+1F300–U+1FAFF
#: is East-Asian-Wide) and charged them at a quarter of a token each, four to
#: five times under the real BPE.  One rule cannot have that hole.  The scan is a
#: compiled regex so it happens in C; a recall measuring a long turn is the cost
#: this protects.
_NON_ASCII = re.compile("[^\x00-\x7f]")


def _by_script(text: str) -> int:
    """The fallback: ASCII at four characters per token, non-ASCII at one each.

    Used when the bundled vocabulary cannot be read.  It is deliberately a
    measure that errs high on non-ASCII text — a CJK page is charged one token
    per character where the real BPE often packs two into one — because the one
    direction a budget cannot afford is an undercount.  The previous table of
    "wide" ranges charged every other non-ASCII script at a quarter of a token
    and omitted emoji entirely.
    """
    _ascii_only, non_ascii = _NON_ASCII.subn("", text)
    ascii_chars = len(text) - non_ascii
    return non_ascii + (ascii_chars + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


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

    The floor is load-bearing in the direction people get wrong:
    a turn that weighs nothing is a turn every budget can afford, so a recall
    would fill its window with empty turns and report success.  One is a lie, but
    it is a lie that bounds the count.
    """
    return max(sum(estimate_message_tokens(message) for message in messages), 1)
