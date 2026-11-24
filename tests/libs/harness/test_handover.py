"""The hand-over's three answers, classified in one place.

`submit_to`, `note_to_window` and `window_attached` each answer `True`, `False`
or `None`, and the split between the last two is the module's whole policy: a
refusal proves nothing was written, and anything else may already have put the
words in the conversation.  The classification is one helper (`_classify`), so
these tests drive it through each call site with an injected window — the
refusal is the vocabulary's own sentence, not a copy of it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from slife2.libs.harness import handover
from slife2.libs.host.config import default_config
from slife2.libs.plugin.vocabulary import NO_WINDOW

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

#: The refusal the window writes, built from the one constant a caller matches.
REFUSAL = ToolError(f"{NO_WINDOW} jack's conversation; start it and hand it over again")


class Window:
    """A window plugin that raises what the test chose, or answers it."""

    def __init__(self, *, raises: Exception | None = None, answer: Any = None) -> None:
        self.raises = raises
        self.answer = answer

    async def call_tool(self, name: str, arguments: dict, **kwargs: Any) -> Any:
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(data=self.answer)


async def test_submit_to_classifies_the_three_answers() -> None:
    assert await handover.submit_to(Window(), "jack", "hi", "wechat") is True
    assert (
        await handover.submit_to(Window(raises=REFUSAL), "jack", "hi", "wechat")
        is False
    )
    assert (
        await handover.submit_to(
            Window(raises=RuntimeError("the stream dropped")), "jack", "hi", "wechat"
        )
        is None
    )


async def test_note_to_window_answers_the_same_vocabulary() -> None:
    config = default_config()

    assert (
        await handover.note_to_window(config, "jack", "⚡ jill online", client=Window())
        is True
    )
    assert (
        await handover.note_to_window(
            config, "jack", "⚡ jill online", client=Window(raises=REFUSAL)
        )
        is False
    )
    assert (
        await handover.note_to_window(
            config,
            "jack",
            "⚡ jill online",
            client=Window(raises=RuntimeError("dropped")),
        )
        is None
    )


async def test_window_attached_reads_a_refusal_as_no_window() -> None:
    """The question takes the same classification: a refusal proves no window is
    showing, and a call that did not come back says nothing either way."""
    config = default_config()

    assert (
        await handover.window_attached(
            config, "jack", client=Window(answer={"attached": True})
        )
        is True
    )
    assert (
        await handover.window_attached(config, "jack", client=Window(raises=REFUSAL))
        is False
    )
    assert (
        await handover.window_attached(
            config, "jack", client=Window(raises=RuntimeError("the stream dropped"))
        )
        is None
    )


async def test_window_attached_reads_an_unreadable_shape_as_unknown() -> None:
    """A shape this build cannot read is not a `False`: saying "no window" would
    turn a broken peer into a missed run."""
    config = default_config()

    assert (
        await handover.window_attached(
            config, "jack", client=Window(answer={"attached": "yes"})
        )
        is None
    )
    assert (
        await handover.window_attached(config, "jack", client=Window(answer={})) is None
    )
