"""The token estimator: the real tokenizer when it can be had, the fallback when not.

Two branches, and both have to be right — the whole reason the fallback exists is
that a machine without the vocabulary is supported rather than broken, so neither
branch is the "real" one and the other a courtesy.

`tests/conftest.py` pins the resolver to "no vocabulary" for the suite, so the
tests that are *about* the tokenizer ask for the `real_tokenizer` fixture; the
rest exercise the fallback, which is what a test run uses.

What is asserted here is deliberately **not** a table of expected numbers.  The
numbers belong to a vocabulary that is versioned upstream and fetched at runtime,
so pinning them would pin somebody else's release — and would say nothing about
the property that matters, which is that a real BPE over the same scripts is
smaller and unbiased where a hand-written ratio is structurally high.
"""

from __future__ import annotations

import pytest

from slife2 import tokens

pytestmark = pytest.mark.unit

#: A sentence and its Chinese counterpart: the same shape of test through both
#: scripts, because one ratio for the whole string is exactly what the fallback
#: refuses to do.
ENGLISH = "the quick brown fox jumps over the lazy dog, and then does it again"
CHINESE = "敏捷的棕色狐狸跳过了那只懒狗，然后又跳了一次"


# --- the fallback: the measure this module was built on -----------------------


def test_the_fallback_counts_the_two_scripts_separately() -> None:
    """One ratio for the whole string is wrong by a factor of four on Chinese.

    Which is the reason this measure exists at all rather than a single
    `len(text) / 4`, and the reason it survived the move to a real tokenizer:
    it is what answers when there is no vocabulary to ask.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tokens, "_openai_encoding", lambda: None)

        assert tokens.estimate_text_tokens("") == 0
        # Latin: four characters to the token, rounded up.
        assert tokens.estimate_text_tokens("abcd") == 1
        assert tokens.estimate_text_tokens("abcde") == 2
        # CJK: a token of its own, so the same character count is four times
        # the estimate — which is the whole point.
        assert tokens.estimate_text_tokens("你好世界") == 4
        assert tokens._by_script(CHINESE) > tokens._by_script(ENGLISH) / 2


def test_a_special_token_spelling_does_not_raise(monkeypatch) -> None:
    """A conversation may contain any text at all, including a token's own name.

    `tiktoken`'s `encode` *refuses* text spelling a special token unless it is
    told not to, so a message quoting `<|endoftext|>` would raise out of the
    estimator and fail a turn.  Neither measure is allowed to care.
    """
    text = "a message that quotes <|endoftext|> and <|im_start|>"
    for resolver in (lambda: None, None):
        with pytest.MonkeyPatch.context() as patch:
            if resolver is not None:
                patch.setattr(tokens, "_openai_encoding", resolver)
            assert tokens.estimate_text_tokens(text) > 0


# --- the real tokenizer -------------------------------------------------------


def test_the_real_tokenizer_is_smaller_on_both_scripts(real_tokenizer) -> None:
    """The reason for the change, stated as a property rather than a number.

    The old measure rounded up at four characters per token for Latin text and
    charged a whole token per CJK character; a real BPE is smaller than both, so
    every budget that was spending a window on an inflated count now spends
    less.  Asserting the direction is what can be asserted without pinning a
    vocabulary's release.
    """
    for text in (ENGLISH, CHINESE):
        assert tokens.estimate_text_tokens(text) < tokens._by_script(text), text


def test_the_real_tokenizer_answers_the_same_way_twice(real_tokenizer) -> None:
    """And it is a pure function of the text — `encode_ordinary` is stateless."""
    assert tokens.estimate_text_tokens(ENGLISH) == tokens.estimate_text_tokens(ENGLISH)


# --- what both branches share -------------------------------------------------


def test_an_image_is_charged_the_same_way_by_either_measure(monkeypatch) -> None:
    """The one thing a tokenizer cannot answer, so the one thing not to vary.

    An attachment is not text; it is charged a flat amount because a part that
    looks free is a part every budget can afford.  Which estimator is in force
    must not change that, or the same conversation would weigh differently
    depending on whether a machine had a network.
    """
    message = {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:,"}}],
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tokens, "_openai_encoding", lambda: None)
        without = tokens.estimate_message_tokens(message)

    assert without == tokens._IMAGE_TOKENS


def test_a_turn_that_weighs_nothing_is_still_charged_one() -> None:
    """The floor is load-bearing: a free turn is a turn every budget can afford.

    The recall would fill a window with empty turns and report success, which is
    why the floor is here rather than at each caller.
    """
    assert tokens.estimate_turn_tokens([]) == 1
    assert tokens.estimate_turn_tokens([{"role": "user", "content": ""}]) == 1


def test_a_failed_load_is_paid_for_once(monkeypatch, tokenizer_resolver) -> None:
    """A machine without the vocabulary must not retry on every turn.

    `tiktoken` raises when it cannot fetch the file — no network, a proxy that
    blocks it, an air-gapped host — and the estimator is called per message, so
    an un-remembered failure would be one failed fetch per message of every
    conversation.  The attempt is made once and its answer kept, like a success.
    """
    import tiktoken

    calls: list[str] = []

    def refuse(name: str):
        calls.append(name)
        raise RuntimeError("no network here")

    monkeypatch.setattr(tiktoken, "get_encoding", refuse)
    monkeypatch.setattr(tokens, "_openai_encoding", tokenizer_resolver)
    monkeypatch.setattr(tokens, "_encoding", None)
    monkeypatch.setattr(tokens, "_encoding_failed", False)

    for _ in range(3):
        assert tokens.estimate_text_tokens(ENGLISH) > 0

    assert calls == [tokens._ENCODING], "tried once, remembered as unavailable"


def test_resolving_the_vocabulary_is_not_a_cost_of_importing() -> None:
    """Importing must not fetch megabytes, so a backend never pays for one.

    `slife2.tokens` is imported by `slife2-context` — and, through it, by
    anything that imports the plugin — so a resolution at module scope would put
    a network fetch in the start-up of a process that may never size anything.
    Asserted in a fresh interpreter, because this one has already estimated.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import slife2.tokens as t; print(t._encoding, t._encoding_failed)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "None False"
