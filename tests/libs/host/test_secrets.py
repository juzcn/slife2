"""Resolved credentials are replaced, not printed.

The registry is process-local and deliberately small: its job is to make a
credential **from credstore** unprintable in every surface that leaves the
process.  A value from the environment or a literal in the config is the
operator's own; protecting those is not this layer's job.
"""

from __future__ import annotations

import pytest

from slife2.libs.host import secrets

pytestmark = pytest.mark.unit

LONG = "sk-live-1234567890"


@pytest.fixture(autouse=True)
def clean_registry():
    secrets.reset()
    yield
    secrets.reset()


def test_a_registered_value_is_replaced() -> None:
    secrets.register(LONG)
    assert secrets.redact(f"before {LONG} after") == "before [redacted] after"


def test_a_value_that_is_too_short_is_not_registered() -> None:
    secrets.register("abc")
    assert secrets.redact("abc stays") == "abc stays"


def test_the_longest_value_wins_when_one_contains_another() -> None:
    secrets.register("live-1234567890")
    secrets.register(LONG)
    assert secrets.redact(LONG) == "[redacted]"


def test_redaction_reaches_inside_mappings_and_lists() -> None:
    secrets.register(LONG)
    assert secrets.redact_obj({"text": f"x {LONG}", "rows": [LONG], "count": 2}) == {
        "text": "x [redacted]",
        "rows": ["[redacted]"],
        "count": 2,
    }


def test_resolving_a_credstore_reference_registers_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import credstore

    from slife2.libs.host.config import resolve_secret

    monkeypatch.setattr(credstore, "resolve_uri", lambda _uri: LONG)
    assert resolve_secret("keyring:slife2/test") == LONG
    assert secrets.redact(f"x {LONG}") == "x [redacted]"


def test_an_environment_value_is_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator's shell is not the credential store.

    A `${HOST}` in a URL goes through the same resolver; protecting it would
    make ordinary text unprintable.  Only credstore's values are in scope.
    """
    from slife2.libs.host.config import resolve_secret

    monkeypatch.setenv("SLIFE2_TEST_HOST", "example.test")
    assert resolve_secret("https://${SLIFE2_TEST_HOST}/v1") == (
        "https://example.test/v1"
    )
    assert secrets.redact("https://example.test/v1") == "https://example.test/v1"
