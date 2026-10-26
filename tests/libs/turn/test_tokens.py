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

from slife2.libs.turn import tokens

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


def test_a_special_token_spelling_does_not_raise(real_tokenizer) -> None:
    """A conversation may contain any text at all, including a token's own name.

    `tiktoken`'s `encode` *refuses* text spelling a special token unless it is
    told not to — measured, not assumed: `encode_ordinary` answers 18 tokens for
    the sentence below where `encode` raises `ValueError` — so a message quoting
    `<|endoftext|>` would raise out of the estimator and fail a turn.  Neither
    measure is allowed to care.

    **Both measures, and the vocabulary is the reason the test exists.**  It used
    to loop over two resolvers that both answered `None` — one patched the seam
    `no_tokenizer_download` had already pinned to the same thing, the other
    patched nothing — so both halves measured the fallback, which counts
    characters and has nothing to refuse.  The one branch `encode_ordinary` was
    chosen over `encode` for was the branch never run.
    """
    text = "a message that quotes <|endoftext|> and <|im_start|>"

    assert tokens.estimate_text_tokens(text) > 0, "the real BPE over it"

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tokens, "_openai_encoding", lambda: None)
        assert tokens.estimate_text_tokens(text) > 0, "and the script measure"


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


def test_an_image_is_charged_the_same_way_by_either_measure(real_tokenizer) -> None:
    """The one thing a tokenizer cannot answer, so the one thing not to vary.

    An attachment is not text; it is charged a flat amount because a part that
    looks free is a part every budget can afford.  Which estimator is in force
    must not change that, or the same conversation would weigh differently
    depending on whether a machine had a network.

    **Both measures are taken, because the name is the claim.**  Only the
    fallback half was measured here — inside a block that pinned the seam to the
    same `None` the fixture had already pinned — so "by either measure" was an
    assertion about one of them, and a tokenizer that charged an image
    differently (or not at all) could not have failed it.

    What is measured is the *difference* the image makes, rather than the
    message's total: the two measures disagree about the text by design (that
    disagreement is the point of the module), so the total is the one number
    that cannot be compared across them.
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
        """What the image adds to the message, under whichever measure is in force."""
        return tokens.estimate_message_tokens(
            with_image
        ) - tokens.estimate_message_tokens(text_only)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tokens, "_openai_encoding", lambda: None)
        by_script = image_costs()

    assert by_script == tokens._IMAGE_TOKENS, "the script measure"
    assert image_costs() == tokens._IMAGE_TOKENS, "and the real tokenizer, the same"


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
    monkeypatch.setattr(tokens, "_failed_at", None)

    for _ in range(3):
        assert tokens.estimate_text_tokens(ENGLISH) > 0

    assert calls == [tokens._ENCODING], "tried once inside the cooldown"


def test_a_transient_failure_is_retried_after_the_cooldown(
    monkeypatch, tokenizer_resolver
) -> None:
    """**One outage is not the process's whole life.**  The failure is remembered
    for `_RETRY_AFTER` so an offline machine does not fetch per message, but a
    machine that comes back online must get the real tokenizer again."""
    import tiktoken

    class Encoding:
        def encode_ordinary(self, text: str) -> list[int]:
            return [1, 2]

    calls: list[str] = []

    def refuse_once(name: str):
        calls.append(name)
        if len(calls) == 1:
            raise RuntimeError("no network here")
        return Encoding()

    monkeypatch.setattr(tiktoken, "get_encoding", refuse_once)
    monkeypatch.setattr(tokens, "_openai_encoding", tokenizer_resolver)
    monkeypatch.setattr(tokens, "_encoding", None)
    monkeypatch.setattr(tokens, "_failed_at", None)

    assert tokens.estimate_text_tokens(ENGLISH) > 0, "the fallback answered"
    # Move the recorded failure past the cooldown without sleeping.
    monkeypatch.setattr(
        tokens,
        "_failed_at",
        tokens.time.monotonic() - tokens._RETRY_AFTER - 1,
    )
    assert tokens.estimate_text_tokens(ENGLISH) == 2

    assert calls == [tokens._ENCODING, tokens._ENCODING], "retried after the cooldown"


def test_resolving_the_vocabulary_is_not_a_cost_of_importing() -> None:
    """Importing must not fetch megabytes, so a backend never pays for one.

    `slife2.libs.turn.tokens` is imported by `slife2-context` — and, through it, by
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
            "import slife2.libs.turn.tokens as t; print(t._encoding, t._failed_at)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "None None"
