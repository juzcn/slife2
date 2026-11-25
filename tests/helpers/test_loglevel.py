"""`SLIFE2_LOG_LEVEL`: one decoder for the daemons and for `slife2 status`."""

from __future__ import annotations

import logging

import pytest

import slife2.helpers.loglevel as loglevel
from slife2.libs.plugin.protocol import _log_level


def test_unset_or_empty_is_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(loglevel.ENV_VAR, raising=False)
    assert loglevel.configured_level() == (logging.INFO, "")

    monkeypatch.setenv(loglevel.ENV_VAR, "   ")
    assert loglevel.configured_level() == (logging.INFO, "")


@pytest.mark.parametrize(
    ("written", "level"),
    [
        ("debug", logging.DEBUG),
        ("WARNING", logging.WARNING),
        ("warn", logging.WARNING),
        ("critical", logging.CRITICAL),
    ],
)
def test_a_known_name_is_its_level(
    monkeypatch: pytest.MonkeyPatch, written: str, level: int
) -> None:
    monkeypatch.setenv(loglevel.ENV_VAR, written)
    assert loglevel.configured_level() == (level, "")


def test_an_unknown_name_is_info_and_comes_back_for_saying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The fallback is never silent**: the caller gets the value to name."""
    monkeypatch.setenv(loglevel.ENV_VAR, "  loud ")
    assert loglevel.configured_level() == (logging.INFO, "loud")


def test_the_daemon_and_the_status_warning_read_one_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two decoders were two rules for one lever; the daemon now asks this one."""
    monkeypatch.setenv(loglevel.ENV_VAR, "loud")
    assert _log_level() == loglevel.configured_level()
