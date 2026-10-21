"""Resolved credentials, and the placeholder that keeps them out of output.

The credential chain in `slife2.libs.host.config` turns a ``keyring:`` URI into a
plain string.  From that moment the only thing keeping the value out of a tool
result or a log line is every call site remembering not to print it — and a call
site that forgets is silent.

This module is the one place that remembers.  :func:`register` is called where a
**credstore** value resolves; :func:`redact` is applied where text leaves a
process: a tool result, a tool description, and the log formatter.

**Only credstore values are registered.**  A ``keyring:`` URI, or a ``${VAR}``
that the credential store answered, is in scope.  An environment variable or a
literal in the config is the operator's own, and protecting it is not this
layer's job.  The registry is per process, the same scope as the credential.

**Short values are not registered.**  Redacting a three-character value would
mangle every line it appeared in, and a credential that short cannot be
protected by replacement in any useful way.  Real provider keys are far longer;
the floor is a guard against a sentinel like ``local``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The shortest value that is redacted.  See the module docstring.
MIN_LENGTH = 8

#: What a secret becomes wherever it would have been printed.
PLACEHOLDER = "[redacted]"

#: Resolved values this process holds.  Per process, like the credential.
_SECRETS: set[str] = set()


def register(value: str) -> None:
    """Remember one credential resolved from credstore, to replace it later."""
    if len(value) >= MIN_LENGTH:
        _SECRETS.add(value)


def reset() -> None:
    """Forget every registered value.  For tests, not for a running process."""
    _SECRETS.clear()


def registered() -> tuple[str, ...]:
    """The registered values, longest first, so one containing another wins."""
    return tuple(sorted(_SECRETS, key=len, reverse=True))


def redact(text: str) -> str:
    """*text* with every registered credential replaced by the placeholder."""
    if not text or not _SECRETS:
        return text
    for secret in registered():
        if secret in text:
            text = text.replace(secret, PLACEHOLDER)
    return text


def redact_obj(value: Any) -> Any:
    """*value* with every string inside it redacted.

    A tool answer is usually a mapping or a list; redacting only its top-level
    string would miss the field that carries the credential.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {redact_obj(key): redact_obj(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_obj(item) for item in value)
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    return value
