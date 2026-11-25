"""The token estimator: one measure, and a damaged install fails loudly.

There is no script-measure fallback.  The vocabulary ships with the package, so
a missing or hash-mismatched bundle is an installation that needs repairing
rather than a machine running with less; the resolver raises
`TokenizerUnavailable` naming the bundle and the repair.  `tests/conftest.py`
pins the resolver to a deterministic fake for the suite's speed, and the tests
that are *about* the tokenizer ask for the `real_tokenizer` fixture.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from slife2.libs.turn import tokens

pytestmark = pytest.mark.unit

#: A sentence and its Chinese counterpart: the same shape of test through both
#: scripts.
ENGLISH = "the quick brown fox jumps over the lazy dog, and then does it again"
CHINESE = "敏捷的棕色狐狸跳过了那只懒狗，然后又跳了一次"


# --- a broken install is a hard failure ---------------------------------------


@pytest.mark.parametrize("damage", ["missing", "mismatch"])
def test_a_damaged_bundle_raises_naming_the_path_and_the_repair(
    monkeypatch, tmp_path: Path, tokenizer_resolver, damage: str
) -> None:
    """**Damage is not a second measure.**  A missing or hash-mismatched bundle
    is an installation that needs repairing, so the caller that needed a count
    fails with the bundle's path and the repair, not with a permanent estimate.
    """
    import tiktoken

    if damage == "mismatch":
        (tmp_path / tokens._CACHE_KEY).write_bytes(b"not the vocabulary")

    calls: list[str] = []

    def refuse(name: str):
        calls.append(name)
        raise AssertionError("get_encoding was asked with no readable bundle")

    monkeypatch.setattr(tiktoken, "get_encoding", refuse)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(tokens, "_openai_encoding", tokenizer_resolver)
    monkeypatch.setattr(tokens, "_encoding", None)
    monkeypatch.setattr(tokens, "_failure", None)

    with pytest.raises(tokens.TokenizerUnavailable) as raised:
        tokens.estimate_text_tokens(ENGLISH)

    message = str(raised.value)
    assert str(tmp_path) in message or str(tokens._BUNDLED_CACHE) in message
    assert "reinstall" in message and "restart" in message
    assert calls == [], "the local check refused before tiktoken could fetch"
    assert tokens._failure is raised.value, "the damage is kept for later calls"


def test_a_kept_failure_is_not_reopened(
    monkeypatch, tmp_path: Path, tokenizer_resolver
) -> None:
    """A second estimate raises the kept failure without touching the file."""
    import shutil

    import tiktoken

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(tiktoken, "get_encoding", lambda name: None)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(cache))
    monkeypatch.setattr(tokens, "_openai_encoding", tokenizer_resolver)
    monkeypatch.setattr(tokens, "_encoding", None)
    monkeypatch.setattr(tokens, "_failure", None)

    with pytest.raises(tokens.TokenizerUnavailable):
        tokens.estimate_text_tokens(ENGLISH)
    kept = tokens._failure

    # Remove the directory: a resolver that retried would answer "missing" with
    # a different path; the kept failure is the same object and sentence.
    shutil.rmtree(cache)
    with pytest.raises(tokens.TokenizerUnavailable) as raised:
        tokens.estimate_text_tokens(ENGLISH)
    assert raised.value is kept


def test_a_failure_is_not_retried_on_the_next_message(
    monkeypatch, tmp_path: Path, tokenizer_resolver
) -> None:
    """One failed resolve is not one failed open per message of every turn."""
    import tiktoken

    calls: list[str] = []

    def refuse(name: str):
        calls.append(name)
        raise RuntimeError("no vocabulary here")

    monkeypatch.setattr(tiktoken, "get_encoding", refuse)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(tokens, "_openai_encoding", tokenizer_resolver)
    monkeypatch.setattr(tokens, "_encoding", None)
    monkeypatch.setattr(tokens, "_failure", None)

    for _ in range(3):
        with pytest.raises(tokens.TokenizerUnavailable):
            tokens.estimate_text_tokens(ENGLISH)

    assert calls == [], "the local file check refused before get_encoding"


# --- the real tokenizer -------------------------------------------------------


def test_a_special_token_spelling_does_not_raise(real_tokenizer) -> None:
    """A conversation may contain any text at all, including a token's own name.

    `tiktoken`'s `encode` *refuses* text spelling a special token unless it is
    told not to, so the estimator was chosen to use `encode_ordinary`; a message
    quoting `<|endoftext|>` must not fail a turn.
    """
    text = "a message that quotes <|endoftext|> and <|im_start|>"
    assert tokens.estimate_text_tokens(text) > 0


def test_the_real_tokenizer_is_smaller_than_the_characters(real_tokenizer) -> None:
    """A real BPE over both scripts packs more than one character per token.

    Asserting the direction is what can be asserted without pinning a
    vocabulary's release.
    """
    for text in (ENGLISH, CHINESE):
        assert tokens.estimate_text_tokens(text) < len(text), text


def test_the_real_tokenizer_answers_the_same_way_twice(real_tokenizer) -> None:
    """And it is a pure function of the text — `encode_ordinary` is stateless."""
    assert tokens.estimate_text_tokens(ENGLISH) == tokens.estimate_text_tokens(ENGLISH)


# --- what both resolvers share ------------------------------------------------


def test_an_image_is_charged_the_same_way_by_either_resolver(real_tokenizer) -> None:
    """The one thing a tokenizer cannot answer, so the one thing not to vary.

    An attachment is not text; it is charged a flat amount because a part that
    looks free is a part every budget can afford.  The fake the suite uses and
    the real vocabulary must charge it the same way.
    """
    with_image = {
        "role": "user",
        "content": [
            {"type": "text", "text": ENGLISH},
            {"type": "image_url", "image_url": {"url": "data:,"}},
        ],
    }
    text_only = {
        "role": "user",
        "content": [{"type": "text", "text": ENGLISH}],
    }

    def image_costs() -> int:
        return tokens.estimate_message_tokens(
            with_image
        ) - tokens.estimate_message_tokens(text_only)

    assert image_costs() == tokens._IMAGE_TOKENS, "the real tokenizer"


def test_a_turn_that_weighs_nothing_is_still_charged_one() -> None:
    """The floor is load-bearing: a free turn is a turn every budget can afford.

    The recall would fill a window with empty turns and report success, which is
    why the floor is here rather than at each caller.
    """
    assert tokens.estimate_turn_tokens([]) == 1
    assert tokens.estimate_turn_tokens([{"role": "user", "content": ""}]) == 1


def _fresh_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run *code* in a fresh, isolated interpreter — **UTF-8 on both ends**.

    `-X utf8` is the child's half: a piped interpreter on a Chinese Windows
    encodes stderr in the ANSI code page, so a log line with an em dash arrives
    as GBK bytes.  `encoding="utf-8"` is the parent's half.
    """
    return subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_a_missing_bundle_never_touches_the_network() -> None:
    """The bundle branch itself, in a fresh process: no cache directory
    configured and no bundled file, so the resolver raises before `requests.get`
    could be reached.  Both are poisoned to fail the test rather than hang it.
    """
    code = """
import os
import tempfile
from pathlib import Path

os.environ.pop("TIKTOKEN_CACHE_DIR", None)

import requests

def refuse(*args, **kwargs):
    raise AssertionError("the estimator touched the network")

requests.get = refuse

import tiktoken

def never(name):
    raise AssertionError("get_encoding was asked with no readable bundle")

tiktoken.get_encoding = never

from slife2.libs.turn import tokens

tokens._BUNDLED_CACHE = Path(tempfile.mkdtemp())
try:
    tokens._openai_encoding()
except tokens.TokenizerUnavailable as exc:
    assert "missing" in str(exc)
    assert "reinstall" in str(exc)
else:
    raise AssertionError("a missing bundle must raise")
"""
    result = _fresh_python(code)
    assert result.returncode == 0, result.stderr


def test_resolving_the_vocabulary_is_not_a_cost_of_importing() -> None:
    """Importing must not parse megabytes, so a backend never pays for one.

    `slife2.libs.turn.tokens` is imported by `slife2-context` — and, through it, by
    anything that imports the plugin — so a resolution at module scope would put
    a vocabulary parse in the start-up of a process that may never size anything.
    Asserted in a fresh interpreter, because this one has already estimated.
    """
    result = _fresh_python(
        "import slife2.libs.turn.tokens as t; print(t._encoding, t._failure)"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "None None"


def test_the_bundled_vocabulary_is_present_and_whole() -> None:
    """The cache file is package data, and it is the file tiktoken expects.

    The name is tiktoken's cache key (the SHA-1 of the URL) and the content is
    verified against the hash the resolver checks before the load.
    """
    path = tokens._BUNDLED_CACHE / tokens._CACHE_KEY

    assert path.is_file(), f"the bundled vocabulary is missing: {path}"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == tokens._BUNDLED_SHA256


def test_the_bundled_vocabulary_loads_without_a_network() -> None:
    """**The point of shipping it.**  A cold process resolves the encoding with
    `requests.get` poisoned, so a cache-key drift or a missing data file fails
    the suite instead of quietly turning the first recall into a download.
    """
    code = """
import requests

def refuse(*args, **kwargs):
    raise AssertionError("the estimator touched the network")

requests.get = refuse

from slife2.libs.turn import tokens

encoding = tokens._openai_encoding()
assert encoding is not None, "the bundled vocabulary did not load"
assert tokens.estimate_text_tokens("hello") > 0
"""
    result = _fresh_python(code)
    assert result.returncode == 0, result.stderr
