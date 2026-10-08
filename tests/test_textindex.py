"""How a turn's text and a query are normalized, and what a query becomes.

The parts worth testing are the ones that are *not* obvious and that were
measured rather than assumed: a CJK run has to keep its characters adjacent
while a Latin word does not, the two scripts have to be cut apart where they
touch, and a query carrying no term has to be refused rather than handed to
FTS5 — whose empty pattern matches every row.
"""

from __future__ import annotations

import pytest

from slife2.textindex import (
    RULES_VERSION,
    EmptyQuery,
    match_expression,
    normalize,
    terms,
)

pytestmark = pytest.mark.unit


# --- normalize ----------------------------------------------------------------


def test_every_cjk_character_gets_a_space() -> None:
    """What makes a character a token: `unicode61` sees a run as one otherwise."""
    assert normalize("工具") == "工 具"


def test_a_mixed_string_keeps_the_latin_words_whole() -> None:
    assert normalize("google搜索一下近期trump的活动") == (
        "google 搜 索 一 下 近 期 trump 的 活 动"
    )


def test_normalize_folds_case() -> None:
    assert normalize("TRUMP") == "trump"


def test_normalize_collapses_whitespace() -> None:
    """So a caller's formatting cannot change what a phrase matches."""
    assert normalize("  工具 \n\t 测试  ") == "工 具 测 试"


def test_cjk_punctuation_survives_normalize() -> None:
    """It is a separator to the tokenizer, not something to be deleted here."""
    assert normalize("工具，测试。") == "工 具 ， 测 试 。"


def test_an_underscore_stays_inside_a_term() -> None:
    """It is a token character to `unicode61`, so it is not a term boundary."""
    assert normalize("ABC_DEF 工具") == "abc_def 工 具"


# --- terms --------------------------------------------------------------------


def test_a_two_character_word_is_one_term() -> None:
    """Which is what the phrase below then holds adjacent."""
    assert terms("工具") == ["工 具"]


def test_a_single_character_is_a_term_too() -> None:
    """A bigram tokenizer could not match this; splitting into characters can."""
    assert terms("工") == ["工"]


def test_a_longer_chinese_word_stays_one_term() -> None:
    assert terms("搜索一下") == ["搜 索 一 下"]


def test_words_separated_by_a_space_are_separate_terms() -> None:
    assert terms("工具 类别") == ["工 具", "类 别"]


def test_latin_words_are_independent_terms() -> None:
    assert terms("turn list") == ["turn", "list"]


def test_the_two_scripts_are_cut_apart_where_they_touch() -> None:
    """A query that glued them asks for two words, not for their adjacency."""
    assert terms("google搜索") == ["google", "搜 索"]


def test_the_cut_is_symmetric() -> None:
    assert terms("trump的活动") == ["trump", "的 活 动"]


def test_punctuation_separates_terms() -> None:
    assert terms("工具，测试。calc/792") == ["工 具", "测 试", "calc", "792"]


def test_a_repeated_term_is_asked_for_once() -> None:
    assert terms("工具 工具") == ["工 具"]


def test_terms_keep_the_order_they_were_written_in() -> None:
    assert terms("trump 活动 计算") == ["trump", "活 动", "计 算"]


# --- match_expression ---------------------------------------------------------


def test_a_term_is_quoted() -> None:
    assert match_expression("工具") == '"工 具"'


def test_terms_are_anded() -> None:
    assert match_expression("trump 活动") == '"trump" AND "活 动"'


def test_fts_operator_words_are_ordinary_words() -> None:
    """Nothing here is interpolated, so a model cannot reach the parser."""
    assert match_expression("工具 AND 计算") == '"工 具" AND "and" AND "计 算"'


def test_an_operator_word_is_a_word_and_not_an_operator() -> None:
    """Quoted whole, so `NEAR` asks for the word rather than for a proximity."""
    assert match_expression("NEAR") == '"near"'


@pytest.mark.parametrize("query", ["", "   ", "*", "((", "，。", "---", "***"])
def test_a_query_with_no_term_is_refused(query: str) -> None:
    """Refused, not answered: an empty MATCH pattern matches every row."""
    with pytest.raises(EmptyQuery):
        match_expression(query)


def test_the_refusal_names_what_was_asked() -> None:
    with pytest.raises(EmptyQuery, match=r"\*\*\*"):
        match_expression("***")


# --- the rule set's identity --------------------------------------------------


def test_the_rules_are_versioned() -> None:
    """An index records this, so a rule change is what makes it rebuild."""
    assert isinstance(RULES_VERSION, str) and RULES_VERSION
