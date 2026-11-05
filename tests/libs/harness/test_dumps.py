"""The head of a tool result that does not fit, and the address of its file.

Everything here is a pure function, which is the point of the split — the
cabinet writes the file, the agent server calls this, and what is under test is
what a conversation carries in place of the whole output.
"""

from __future__ import annotations

import json

import pytest

from slife2.libs.harness import dumps
from slife2.libs.host.config import DEFAULT_TOOL_RESULT_TOKENS
from slife2.libs.turn.tokens import estimate_text_tokens

#: The window the shipped config states.
WINDOW = DEFAULT_TOOL_RESULT_TOKENS

#: The smallest budget the config allows, so the address and a readable head
#: both have to fit inside it.
BUDGET = 512

#: The path the cabinet answers with: relative to the agent's data directory.
WHERE = "agents/jack/cabinet/dumps/call_0123456789abcdef0123456789abcdef.md"


def body(lines: int = 3000) -> str:
    return "\n".join(
        f"line {number} of a tool output" for number in range(1, lines + 1)
    )


def marker(answer: str) -> dict:
    """The marker's payload, as a model reads it."""
    started = answer.index(dumps.MARKER) + len(dumps.MARKER)
    ended = answer.index(dumps.MARKER_END, started)
    return json.loads(answer[started:ended])


def test_a_window_names_the_call_the_size_and_the_path() -> None:
    """The marker is the address, and it carries the file path the agent opens.

    **No counts.**  The size is the file's, read by the cabinet without looking
    at the text; a character or line count would be work the reader can do if
    and when it is asked for.
    """
    text = body()

    answer = dumps.window(
        call="call_abc", text=text, where=WHERE, size=len(text) + 1, budget=WINDOW
    )

    payload = marker(answer)
    assert payload == {"call": "call_abc", "bytes": len(text) + 1, "file": WHERE}
    assert "dump_read" in answer and "dump_grep" in answer
    assert str(len(text) + 1) in answer, "the file's size is shown"


def test_the_head_is_the_outputs_own_words_from_the_start() -> None:
    """Nothing is rewritten: the head is a prefix of what the tool returned."""
    text = body()

    answer = dumps.window(
        call="c", text=text, where=WHERE, size=len(text) + 1, budget=WINDOW
    )
    head = answer.split("\n\n", 1)[1]

    assert text.startswith(head)
    assert head, "a head is what tells a model what kind of output this is"


def test_the_sentence_says_exactly_how_much_was_shown() -> None:
    """The head length is the character offset the next read continues from."""
    text = body()

    answer = dumps.window(
        call="c", text=text, where=WHERE, size=len(text) + 1, budget=WINDOW
    )
    head = answer.split("\n\n", 1)[1]

    assert f"the first {len(head)} characters of the output text follow" in answer


def test_a_window_never_exceeds_its_budget() -> None:
    """**The property the arrangement rests on**, address and sentence included."""
    text = body()
    for budget in (BUDGET, 800, 4000):
        answer = dumps.window(
            call="call_abc", text=text, where=WHERE, size=len(text) + 1, budget=budget
        )

        assert estimate_text_tokens(answer) <= budget, budget


def test_the_address_gives_way_last() -> None:
    """**A state that cannot be read back must fail, not look like success.**

    The marker names the file the rest is in; a window cut before the end of the
    marker is a conversation that has given up the bound and broken the address.
    """
    for budget in (1, 5, 15, 50):
        with pytest.raises(ValueError, match="address"):
            dumps.window(
                call="call_abc123", text=body(), where=WHERE, size=1, budget=budget
            )


def test_a_single_line_longer_than_the_window_still_gets_a_head() -> None:
    """The head is a character prefix, so a file with no useful lines is still
    identified — and char mode can read the rest from where it stopped."""
    text = "x" * 60_000

    answer = dumps.window(
        call="c", text=text, where=WHERE, size=len(text) + 1, budget=BUDGET
    )

    head = answer.split("\n\n", 1)[1]
    assert head == "x" * len(head)
    assert len(head) > 0
    assert estimate_text_tokens(answer) <= BUDGET


def test_a_huge_output_is_not_sliced_in_half_to_find_a_small_head(monkeypatch) -> None:
    """**The search grows to the fit; it does not start from the output's size.**

    Binary-searching the whole string would copy half of a multi-megabyte output
    on the first probe.  With the fallback measure (one token per character), a
    1,000,000-character output needs at most a few thousand characters shown;
    the largest slice the renderer ever asks for must stay far below the file.
    """
    monkeypatch.setattr(dumps, "estimate_text_tokens", lambda value: len(value))

    class CountingText(str):
        deepest = 0

        def __getitem__(self, item):
            if isinstance(item, slice) and item.stop is not None:
                type(self).deepest = max(type(self).deepest, item.stop)
            return super().__getitem__(item)

    text = CountingText("x" * 1_000_000)

    answer = dumps.window(call="c", text=text, where=WHERE, size=1_000_001, budget=512)

    assert answer
    assert CountingText.deepest <= 4096, (
        f"the search sliced {CountingText.deepest} characters of a 1,000,000-"
        f"character output; it should never need more than a small multiple of "
        f"the head"
    )
