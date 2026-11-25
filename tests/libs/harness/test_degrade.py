"""The degrade line: once per condition, in the window, and in the log.

A fallback that continues is a fact the person is owed.  The tool paths put
their damage in the answer the model reads; this is the route for the ones that
happen between turns.  The line is drawn once per `(agent, code)` per process —
a tokenizer that cannot be read would otherwise write one per estimate — and a
window that is gone does not turn the report into another failure.
"""

from __future__ import annotations

import logging

import pytest

from slife2.libs.harness import degrade, notes

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def fresh_reports() -> None:
    degrade.reset()


@pytest.mark.asyncio
async def test_a_degrade_is_drawn_once_and_logged(monkeypatch, caplog) -> None:
    drawn: list[tuple[str, str]] = []

    async def note(config, agent, text, *, client=None):
        drawn.append((agent, text))
        return True

    monkeypatch.setattr(degrade, "note_to_window", note)
    caplog.set_level(logging.WARNING, logger=degrade.logger.name)

    first = await degrade.report(
        None, "jack", "schedule_ledger", "scheduled tasks are not firing", "the ledger is broken"
    )
    second = await degrade.report(
        None, "jack", "schedule_ledger", "scheduled tasks are not firing", "the ledger is broken"
    )

    assert first is True and second is False
    assert drawn == [
        ("jack", notes.degraded("scheduled tasks are not firing", "the ledger is broken"))
    ]
    assert caplog.text.count("degrade code=schedule_ledger") == 1, (
        "one line and one log per persistent condition, not one per call"
    )


@pytest.mark.asyncio
async def test_a_window_that_is_gone_does_not_raise(monkeypatch) -> None:
    async def gone(config, agent, text, *, client=None):
        raise ConnectionError("no window")

    monkeypatch.setattr(degrade, "note_to_window", gone)
    assert (
        await degrade.report(None, "jack", "tokenizer", "counts are approximate", "broken")
        is False
    )


@pytest.mark.asyncio
async def test_two_codes_for_one_agent_are_two_lines(monkeypatch) -> None:
    drawn: list[str] = []

    async def note(config, agent, text, *, client=None):
        drawn.append(text)
        return True

    monkeypatch.setattr(degrade, "note_to_window", note)
    await degrade.report(None, "jack", "one", "effect one", "reason one")
    await degrade.report(None, "jack", "two", "effect two", "reason two")

    assert len(drawn) == 2
