"""The window over a tool result: the reply, the cut, and the text it becomes.

Everything here is a pure function, which is the point of the split — the call
that produces a reply and the store that keeps the whole are elsewhere, and what
is under test is the part that decides what a conversation carries.
"""

from __future__ import annotations

import pytest

from slife2.libs.harness import dumps
from slife2.libs.host.config import DEFAULT_TOOL_RESULT_TOKENS
from slife2.libs.turn.tokens import estimate_lines_tokens, estimate_text_tokens

#: The window the shipped config states, used here as the one a conversation
#: would really be bounded by.
WINDOW = DEFAULT_TOOL_RESULT_TOKENS

#: A smaller one, so a test's numbers are about the rule and not about the size.
BUDGET = 400


def body(lines: int = 300) -> str:
    return "\n".join(
        f"line {number} of a tool output" + (" ERROR" if number == 100 else "")
        for number in range(1, lines + 1)
    )


# --- the reply, as an address rather than a text ------------------------------


def test_a_reply_is_line_ranges_in_the_order_it_named_them() -> None:
    """**The order is used**, so a window that still overflows trims from its end."""
    decision = dumps.read_reply('{"lines": [[100, 102], [1, 3]]}')

    assert decision == dumps.Decision(lines=[(100, 102), (1, 3)])
    kept = dumps.cut(dumps.lines_of(body()), decision, BUDGET)
    assert [number for number, _text in kept] == [100, 101, 102, 1, 2, 3]


def test_a_line_is_never_quoted_and_never_rewritten() -> None:
    """What arrives is the tool's own words, because the harness does the cutting.

    The reply is a pair of numbers and the text comes out of the original, so a
    model cannot put a sentence of its own into the conversation by mis-typing
    one it read — which is the property the whole arrangement rests on.
    """
    original = "the tool said this\nand this"
    decision = dumps.read_reply('{"lines": [[2, 2]]}')

    kept = dumps.cut(dumps.lines_of(original), decision, BUDGET)

    assert kept == [(2, "and this")]


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        "{}",  # answered nothing: not "keep nothing"
        '{"lines": 3}',  # wrong type
        '{"lines": true}',  # a bool is an int, and not a line
        '{"lines": [[3]]}',  # not a pair
        '{"lines": [[0, 2]]}',  # line numbers start at one
        '{"lines": [[5, 4]]}',  # backwards
        '{"lines": [["a", "b"]]}',
        '{"lines": "words"}',  # a word that is not the one word
        "[]",
    ],
)
def test_a_reply_this_build_cannot_read_is_refused_rather_than_read_as_empty(
    reply: str,
) -> None:
    """**An unreadable reply is not a decision to do nothing** (§5.1's split).

    They are different answers with different consequences — one is a failure
    the caller falls back from, the other is a model saying the output has
    nothing to do with this conversation — and collapsing them would make the
    fallback unreachable.
    """
    assert dumps.read_reply(reply) is dumps.REJECTED


def test_the_two_answers_that_are_not_selections() -> None:
    """`[]` and `"binary"` are answers, and they are different ones."""
    assert dumps.read_reply('{"lines": []}') == dumps.Decision(lines=[])
    binary = dumps.read_reply('{"lines": "binary"}')

    assert binary == dumps.Decision(lines=[], binary=True)
    assert binary.binary and binary.keeps_nothing


def test_a_key_this_build_does_not_know_is_dropped_rather_than_refused() -> None:
    """A model that added a field of its own has still answered the question."""
    decision = dumps.read_reply('{"lines": [[5, 9]], "why": "it is the error"}')

    assert decision == dumps.Decision(lines=[(5, 9)])


# --- the cut ------------------------------------------------------------------


def test_a_range_past_the_end_is_clipped_rather_than_refused() -> None:
    """A model that counted wrong is not an error; it is a model that counted wrong."""
    decision = dumps.read_reply('{"lines": [[298, 9999]]}')

    kept = dumps.cut(dumps.lines_of(body(300)), decision, BUDGET)

    assert [number for number, _text in kept] == [298, 299, 300]


def test_a_line_named_twice_is_kept_once_in_its_first_position() -> None:
    """The address is what it is: a second mention is a repeat, not a correction."""
    decision = dumps.read_reply('{"lines": [[7, 8], [8, 9]]}')

    kept = dumps.cut(dumps.lines_of(body()), decision, BUDGET)

    assert [number for number, _text in kept] == [7, 8, 9]


def test_a_selection_over_the_window_is_trimmed_from_its_end() -> None:
    """The model's ranking survives a window it misjudged."""
    decision = dumps.read_reply('{"lines": [[1, 3000]]}')
    lines = dumps.lines_of(body(3000))

    kept = dumps.cut(lines, decision, BUDGET)

    assert kept, "something is always kept when a line fits"
    assert kept[0][0] == 1, "the first is the one the model wanted most"
    assert estimate_lines_tokens(kept) <= BUDGET
    assert kept[-1][0] < 3000


# --- the text a conversation carries ------------------------------------------


def test_a_window_names_the_call_and_says_what_it_is() -> None:
    """The marker is the address, and every answer here is measured."""
    lines = dumps.lines_of(body())
    decision = dumps.read_reply('{"lines": [[100, 101]]}')

    answer = dumps.window(
        call="call_abc",
        lines=lines,
        kept=dumps.cut(lines, decision, WINDOW),
        chosen=True,
        budget=WINDOW,
    )

    assert answer.startswith('[DUMP:{"call": "call_abc", "total": 300}]')
    assert "line 100 of a tool output ERROR" in answer
    assert "100-101" in answer, "the sentence says which lines these are"
    assert "dump_read" in answer, "and how to get the rest"
    assert estimate_text_tokens(answer) <= WINDOW


def test_the_whole_answer_is_measured_including_the_sentence() -> None:
    """**The bound is on what a model is handed**, header and sentence included.

    Which is why the budget is spent on the *lines* first and the rendered
    answer is clipped at the end: a window that went over would be a tool result
    the harness has to shorten again, and shortening this one is the loop.
    """
    lines = dumps.lines_of(body(5000))
    decision = dumps.read_reply('{"lines": [[1, 5000]]}')

    answer = dumps.window(
        call="call_big",
        lines=lines,
        kept=dumps.cut(lines, decision, 50),
        chosen=True,
        budget=50,
    )

    assert estimate_text_tokens(answer) <= 50


def test_a_window_too_small_for_its_own_address_fails_rather_than_lies() -> None:
    """**A state that cannot be read back must fail, not look like success.**

    `clip_to_tokens` bounds the answer and protects nothing, so a
    `tool_result.tokens` small enough cut the `[DUMP:…]` marker mid-JSON — the
    model could not name the dump to ask for the rest — and a smaller one still
    answered with the empty string: a tool result that says nothing at all, in a
    conversation that carries it for ever.  The address is what makes the bound
    survivable, so a budget that cannot hold it is a failure rather than a
    shorter window (`slife2.libs.host.config` validates `tokens >= 1` and nothing
    else, so this is reachable by a config edit alone).
    """
    lines = dumps.lines_of(body())
    decision = dumps.read_reply('{"lines": [[1, 3]]}')

    # Each of these is smaller than the marker's own cost under either measure,
    # so the guard is what has to answer — not the size of a window.
    for budget in (1, 5, 15):
        with pytest.raises(ValueError, match="address"):
            dumps.window(
                call="call_abc123",
                lines=lines,
                kept=dumps.cut(lines, decision, budget),
                chosen=True,
                budget=budget,
            )

    # And a budget with room for the address still answers with it whole.
    answer = dumps.window(
        call="call_abc123",
        lines=lines,
        kept=dumps.cut(lines, decision, WINDOW),
        chosen=True,
        budget=WINDOW,
    )

    assert answer.startswith(dumps.MARKER)
    assert '"call": "call_abc123"' in answer


def test_the_three_answers_that_are_not_a_window_read_differently() -> None:
    """Nothing bears on it; the lines are too long to fit; it is not text.

    Three sentences, because a model acts differently on each: it stops looking,
    it asks for the lines by name, or it knows there is nothing to read.
    """
    lines = dumps.lines_of(body())
    nothing = dumps.window(call="c", lines=lines, kept=[], chosen=False, budget=WINDOW)
    too_long = dumps.window(call="c", lines=lines, kept=[], chosen=True, budget=WINDOW)
    binary = dumps.window(
        call="c",
        lines=["AAAA"],
        kept=[],
        chosen=False,
        binary=True,
        budget=WINDOW,
    )

    assert "nothing in this output bears on this conversation" in nothing
    assert "longer than the window" in too_long
    assert "not text" in binary and "will not open it" in binary


# --- the instruction the discriminator is shown -------------------------------


def test_the_instruction_hands_over_a_numbered_result_and_the_measured_size() -> None:
    """Numbered, because that is how the reply can be an address."""
    rendered = dumps.instruction(
        tool="grep", body=body(12), budget=WINDOW, user_input="why?"
    )

    assert "grep" in rendered
    assert " 1  line 1 of a tool output" in rendered
    assert "12  line 12 of a tool output" in rendered
    assert str(estimate_text_tokens(body(12))) in rendered
    assert "why?" in rendered
    # The reply surface is rendered rather than described: a field's wording is
    # what a model copies.
    assert dumps.COMPACT_REPLY["lines"][:40] in rendered
