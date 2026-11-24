"""The thread hop carries blocking calls, and refuses a coroutine function.

`off_loop` is where a store's signature meets the worker thread, and its
`ParamSpec` makes a wrong argument a type error at the call site.  The one
wrong argument a signature cannot name is an `async def`: handed to a thread
it returns an un-awaited coroutine, which is truthy where the answer should
be and leaves only a `RuntimeWarning` when it is collected.
"""

from __future__ import annotations

import pytest

from slife2.helpers.threads import off_loop

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_a_blocking_call_returns_its_answer() -> None:
    assert await off_loop(sum, [1, 2, 3]) == 6


@pytest.mark.asyncio
async def test_an_async_function_is_refused_rather_than_returned_unawaited() -> None:
    async def answered() -> int:
        return 1

    with pytest.raises(TypeError, match="is async"):
        await off_loop(answered)
