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

**A vocabulary that cannot be read is a damaged installation, and it fails
loudly.**  The bundle ships with the package, so a missing or hash-mismatched
file is not a machine that is running with less — it is an installation that
needs repair.  There is no script-measure fallback: a permanent second measure
would make an overestimate the normal answer for that process, and nothing would
ever say a reinstall was owed.  The resolver raises `TokenizerUnavailable` with
the bundle's path and the repair, and the call that needed a token count fails
rather than guessing.  A repaired installation is picked up by a restart.

**No vocabulary is exact for a non-OpenAI endpoint**, and this one is not
claimed to be: a Qwen or DeepSeek model has its own.  What makes `o200k_base`
the better base anyway is that it is a real BPE over the same scripts, so its
error is small and unbiased where a hand-written ratio's is structural.  The
budget it feeds is a fraction of a window, so an estimate that is 5% wrong
spends 5% of a window that was sized for it.

**Nothing resolves at import**: a process that never sizes a conversation never
touches this.  A failed read is kept for the process's life, because it is a
repair-and-restart fact rather than something a retry can heal.

**What the number is for.**  The recall chooses what fits; none of it reports
usage, and a caller that finds itself printing one has made a mistake this
module cannot catch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
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
#: yet"; `_failure` is the damage a failed resolve found, kept so every later
#: estimate raises the same sentence rather than opening the file again.
_encoding: Any = None
_failure: TokenizerUnavailable | None = None


class TokenizerUnavailable(RuntimeError):
    """The bundled vocabulary cannot be read, so no token count can be made.

    **Damage, not a machine running with less.**  The bundle ships with the
    package; a missing or hash-mismatched file is an installation that needs
    repairing, and the caller that needed a count fails with the path and the
    repair rather than using a permanent second measure.
    """


def _openai_encoding() -> Any:
    """`o200k_base` — the seam a test replaces to keep the suite off the vocabulary.

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
    timeout*, so a broken install on a black-holed network would hang the turn.
    The file tiktoken will read is therefore checked here first — present, and
    the content hash tiktoken pins — and a broken install raises
    `TokenizerUnavailable` naming the bundle and the repair.
    """
    global _encoding, _failure
    if _encoding is None:
        if _failure is not None:
            raise _failure
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
        except Exception as exc:  # noqa: BLE001 — any failure is damaged install data
            _failure = TokenizerUnavailable(
                f"the tokenizer vocabulary ({_ENCODING}) cannot be read: {exc}. "
                f"The bundle is {_BUNDLED_CACHE / _CACHE_KEY}; reinstall or "
                f"repair the slife2 installation and restart."
            )
            logger.error("%s", _failure)
            raise _failure from exc
    return _encoding


#: What one attached image is charged.  It is not text and cannot be counted as
#: any; the figure is a fixed charge, and what it is for is keeping an
#: image-bearing turn from looking free to a budget.  In practice the bytes never
#: reach the db (an attachment is stored as a note), so this is reached only by
#: the live message list.
_IMAGE_TOKENS = 200


def estimate_text_tokens(text: str) -> int:
    """How many tokens `text` is, by the model's tokenizer.

    **A vocabulary that cannot be read fails the caller** (`TokenizerUnavailable`)
    rather than being estimated by a second measure: a permanent fallback would
    make the degraded count the normal answer and nothing would say the install
    needs repairing.
    """
    if not text:
        return 0
    encoding = _openai_encoding()
    # `encode_ordinary` and not `encode`: the latter *refuses* text spelling a
    # special token — `<|endoftext|>` — and a conversation may contain any text
    # at all, including that, without the estimator raising a turn.
    return len(encoding.encode_ordinary(text))


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
    """About how many tokens one turn costs: the sum of its messages.

    **No floor.**  A turn with nothing in it costs nothing, and saying so is
    true; every writer here records the turn's opening message, so a zero
    weight means a list this build did not write.  The budget that reads this
    walks a bounded candidate list, so a free turn cannot make it loop.
    """
    return sum(estimate_message_tokens(message) for message in messages)
