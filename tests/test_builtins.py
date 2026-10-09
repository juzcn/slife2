"""slife2-builtins: the tools slife2 ships, served like anybody else's.

Two things are being tested here and they are not the same.  One is what the
tools *do* — and most of that is `calc`'s refusals, because `calc` evaluates a
string a language model produced, which makes the model an attacker with
knowledge of the prompt.  The other is that they arrive the way every other tool
in this system arrives: over MCP, from a server with a name, a tool list and
schemas — not as a function call with a shortcut past the hub.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from slife2.builtins import SERVER_NAME, build_server, evaluate
from slife2.config import default_config

pytestmark = pytest.mark.unit


async def call(name: str, **arguments) -> str:
    """One tool call, over the in-memory transport."""
    async with Client(build_server(default_config())) as client:
        result = await client.call_tool(name, arguments)
    return result.data


# --- served over MCP ----------------------------------------------------------


def test_the_server_is_named_where_a_client_can_find_it() -> None:
    assert SERVER_NAME == "slife2-builtins"


@pytest.mark.asyncio
async def test_the_tool_list_is_read_off_a_signature_not_hand_written() -> None:
    """The schema the model sees is derived, which is why it cannot drift.

    `echo` is the shape to copy when adding the next one: a typed argument, a
    docstring, and nothing else to keep in step.
    """
    async with Client(build_server(default_config())) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    assert set(listed) == {"echo", "now", "calc"}
    assert listed["echo"].input_schema["properties"]["text"]["type"] == "string"
    assert listed["echo"].input_schema["required"] == ["text"]
    assert listed["now"].input_schema["properties"] == {}
    assert "expression" in listed["calc"].input_schema["properties"]


@pytest.mark.asyncio
async def test_echo_says_it_back() -> None:
    assert await call("echo", text="你好") == "你好"


@pytest.mark.asyncio
async def test_now_is_utc_iso8601() -> None:
    parsed = datetime.fromisoformat(await call("now"))
    offset = parsed.utcoffset()
    assert offset is not None, "the timestamp must carry a timezone"
    assert offset.total_seconds() == 0, "and it must be UTC"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2 + 2", "4"),
        ("2 + 2 * 3", "8"),
        ("(2 + 2) * 3", "12"),
        ("7 / 2", "3.5"),
    ],
)
async def test_calc_answers_over_the_wire(expression: str, expected: str) -> None:
    assert await call("calc", e=expression) == expected


@pytest.mark.asyncio
async def test_calc_accepts_the_long_form_argument_name() -> None:
    """`e` is terse; a model that writes `expression` should not be punished.

    Declared in the signature rather than tolerated at runtime, because FastMCP
    validates the arguments against the schema — an undeclared name would be
    rejected before the function ever ran.
    """
    assert await call("calc", expression="6*7") == "42"


@pytest.mark.asyncio
async def test_a_syntax_error_is_an_error_the_call_reports() -> None:
    with pytest.raises(ToolError):
        await call("calc", e="2 +")


# --- calc: what it evaluates --------------------------------------------------


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


@pytest.mark.parametrize(
    "expression",
    [
        "(10 ** 1000) ** 1000",
        "((10 ** 1000) ** 1000) ** 10",
    ],
)
def test_calc_bounds_a_runaway_result_too(expression: str) -> None:
    """The exponent check bounds one operation, and the base is a value.

    Every `**` above passes an exponent limit of 1000 on its own while asking
    for an integer of millions of bits: the first is 3.3M bits in 0.12s, the
    second 33M in 6.6s, and nesting once more does not finish in 20s.  `calc` is
    always in the model's list, so what has to be bounded is the answer — and
    the error has to be about the answer, since the exponent it was written with
    is one `calc` allows.
    """
    with pytest.raises(ValueError, match="bits"):
        evaluate(expression)


def test_calc_still_does_the_arithmetic_it_is_for() -> None:
    """The bound must not be so eager that it refuses ordinary sums."""
    assert evaluate("2 ** 10") == 1024
    assert evaluate("10 ** 100") == 10**100


def test_calc_reports_division_by_zero_to_the_model() -> None:
    """The model gets to see the mistake and correct it."""
    with pytest.raises(ZeroDivisionError):
        evaluate("1 / 0")
