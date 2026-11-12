"""The one retry policy both channels run, and the one reset rule.

The mesh's requester ladder and the WeChat poll used to keep their own backoff
and clear it on different events.  What is worth testing is the rule that
replaced both: a run starts at `base`, grows to `cap`, and is cleared by nothing
but an attempt that did not fail.
"""

from __future__ import annotations

import pytest

from slife2.helpers.timeouts import Backoff, Retry, Timeouts, validate

pytestmark = pytest.mark.unit


def test_a_run_starts_at_base_grows_and_stops_at_the_cap() -> None:
    policy = Retry(base=2.0, factor=3.0, cap=20.0)
    assert [policy.delay(n) for n in (1, 2, 3, 4)] == [2.0, 6.0, 18.0, 20.0]


def test_jitter_is_a_fraction_of_the_step() -> None:
    policy = Retry(base=10.0, factor=2.0, cap=100.0, jitter=0.2)
    for _ in range(100):
        assert 8.0 <= policy.delay(1) <= 12.0
        assert 16.0 <= policy.delay(2) <= 24.0


def test_a_count_of_zero_failures_is_not_a_retry() -> None:
    with pytest.raises(ValueError, match="from 1"):
        Retry(base=1.0, factor=1.0, cap=1.0).delay(0)


def test_a_run_is_cleared_only_by_a_successful_attempt() -> None:
    backoff = Backoff(Retry(base=1.0, factor=2.0, cap=8.0))
    assert [backoff.failed() for _ in range(4)] == [1.0, 2.0, 4.0, 8.0]
    backoff.succeeded()
    assert backoff.failures == 0
    assert backoff.failed() == 1.0


def test_an_unusable_policy_is_a_validation_error() -> None:
    for mutate in (
        lambda p: setattr(p, "base", 0.0),
        lambda p: setattr(p, "factor", 0.5),
        lambda p: setattr(p, "cap", 0.5),
        lambda p: setattr(p, "jitter", 1.0),
    ):
        ts = Timeouts()
        mutate(ts.pacing.wechat_retry)
        errors = validate(ts)
        assert any("pacing.wechat_retry" in e for e in errors), (
            "a bad retry policy passed validation"
        )
