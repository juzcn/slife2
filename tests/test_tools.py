"""The builtin tools and the registry's failure contract.

`calc` evaluates a string a language model produced, which makes the model an
attacker with knowledge of the prompt.  Most of this file is about what it
refuses.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from slife2.messages import ToolCall
from slife2.tools import ToolRegistry, builtin_tools, evaluate

pytestmark = pytest.mark.unit


# --- registry ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_returns_the_result_and_ok() -> None:
    registry = ToolRegistry(builtin_tools())
    text, ok = await registry.execute(
        ToolCall(id="c1", name="calc", arguments={"e": "1+1"})
    )
    assert (text, ok) == ("2", True)


@pytest.mark.asyncio
async def test_unknown_tool_is_a_result_not_an_exception() -> None:
    """The error is a message the model can act on, not a dead turn."""
    registry = ToolRegistry(builtin_tools())
    text, ok = await registry.execute(ToolCall(id="c1", name="wether", arguments={}))
    assert ok is False
    assert "wether" in text
    assert "calc" in text  # names the alternatives


@pytest.mark.asyncio
async def test_a_raising_tool_becomes_an_error_result() -> None:
    registry = ToolRegistry(builtin_tools())
    # `calc` with no expression raises ValueError inside the tool.
    text, ok = await registry.execute(ToolCall(id="c1", name="calc", arguments={}))
    assert ok is False
    assert "ValueError" in text


def test_specs_are_advertised() -> None:
    assert {s.name for s in ToolRegistry(builtin_tools()).specs} == {"now", "calc"}


# --- now ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_now_is_utc_iso8601() -> None:
    registry = ToolRegistry(builtin_tools())
    text, ok = await registry.execute(ToolCall(id="c1", name="now", arguments={}))
    assert ok is True
    parsed = datetime.fromisoformat(text)
    offset = parsed.utcoffset()
    assert offset is not None, "the timestamp must carry a timezone"
    assert offset.total_seconds() == 0, "and it must be UTC"


# --- calc: what it evaluates -------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2 + 2", 4),
        ("2 + 2 * 3", 8),  # precedence, not left-to-right
        ("(2 + 2) * 3", 12),
        ("7 // 2", 3),
        ("7 % 2", 1),
        ("7 / 2", 3.5),
        ("2 ** 10", 1024),
        ("-5 + 3", -2),
        ("+5", 5),
        ("1.5 * 2", 3.0),
    ],
)
def test_calc_evaluates_arithmetic(expression: str, expected: float) -> None:
    assert evaluate(expression) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo hi')",
        "open('/etc/passwd')",
        "print(1)",
        "lambda: 1",
        "[1, 2][0]",
        "{'a': 1}",
        "1 if True else 2",
        "x",
        "x + 1",
        "2 .__class__",
        "True",
        "1 < 2",
        "'a' * 3",
        "f'{1}'",
        "1; 2",
        "[x for x in range(3)]",
    ],
)
def test_calc_refuses_anything_that_is_not_arithmetic(expression: str) -> None:
    """A model that can talk its way past this gets code execution.

    The list is deliberately long: each entry is a distinct syntactic route to
    something the AST walker must not accept.
    """
    with pytest.raises(ValueError):
        evaluate(expression)


def test_calc_bounds_a_runaway_exponent() -> None:
    """`9**9**9` is a few keystrokes that would hang the process."""
    with pytest.raises(ValueError, match="exponent"):
        evaluate("9 ** 9 ** 9")


def test_calc_reports_division_by_zero_to_the_model() -> None:
    """The model gets to see the mistake and correct it."""
    with pytest.raises(ZeroDivisionError):
        evaluate("1 / 0")


@pytest.mark.asyncio
async def test_calc_tool_reports_a_syntax_error_as_text() -> None:
    registry = ToolRegistry(builtin_tools())
    text, ok = await registry.execute(
        ToolCall(id="c1", name="calc", arguments={"e": "2 +"})
    )
    assert ok is False
    assert "Error" in text


@pytest.mark.asyncio
async def test_calc_accepts_the_long_form_argument_name() -> None:
    """`e` is terse; a model that writes `expression` should not be punished."""
    registry = ToolRegistry(builtin_tools())
    text, ok = await registry.execute(
        ToolCall(id="c1", name="calc", arguments={"expression": "6*7"})
    )
    assert (text, ok) == ("42", True)
