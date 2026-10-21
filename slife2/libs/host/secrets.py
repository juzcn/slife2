"""Resolved credentials, and the masked form that keeps them out of output.

The credential chain in `slife2.libs.host.config` turns a ``keyring:`` URI into a
plain string.  From that moment the only thing keeping the value out of a tool
result or a log line is every call site remembering not to print it — and a call
site that forgets is silent.

This module is the one place that remembers.  :func:`register` is called where a
**credstore** value resolves; :func:`masked` is applied where text leaves a
process: a tool result, a tool description, and the log formatter.  The mask is
the conventional one — the first four and last four characters, or ``***`` when
the value is short enough that four characters would be most of it.

**Only credstore values are registered.**  A ``keyring:`` URI, or a ``${VAR}``
that the credential store answered, is in scope.  An environment variable or a
literal in the config is the operator's own, and protecting it is not this
layer's job.  The registry is per process, the same scope as the credential.

**Short values are not registered.**  Masking a three-character value would
mangle every line it appeared in, and a credential that short cannot be
protected by replacement in any useful way.  Real provider keys are far longer;
the floor is a guard against a sentinel like ``local``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: The shortest value that is masked.  See the module docstring.
MIN_LENGTH = 8

#: What a value too short to show a prefix and suffix of becomes.
SHORT = "***"

#: Between the prefix and the suffix, the same glyph credstore's terminal mask
#: uses.
_SEPARATOR = "…"

#: Resolved values this process holds.  Per process, like the credential.
_SECRETS: set[str] = set()


def register(value: str) -> None:
    """Remember one credential resolved from credstore, to mask it later."""
    if len(value) >= MIN_LENGTH:
        _SECRETS.add(value)


def reset() -> None:
    """Forget every registered value.  For tests, not for a running process."""
    _SECRETS.clear()


def registered() -> tuple[str, ...]:
    """The registered values, longest first, so one containing another wins."""
    return tuple(sorted(_SECRETS, key=len, reverse=True))


def mask(value: str) -> str:
    """The conventional masked form of one credential.

    The same shape as ``credstore``'s terminal mask: four characters at each
    end, or nothing at all when the value is too short to hide anything.
    """
    if not value:
        return "(empty)"
    if len(value) <= MIN_LENGTH:
        return SHORT
    return f"{value[:4]}{_SEPARATOR}{value[-4:]}"


def masked(text: str) -> str:
    """*text* with every registered credential in its masked form."""
    if not text or not _SECRETS:
        return text
    pattern = "|".join(re.escape(secret) for secret in registered())
    return re.sub(pattern, lambda found: mask(found.group(0)), text)


def masked_obj(value: Any) -> Any:
    """*value* with every string inside it masked.

    A tool answer is usually a mapping or a list; masking only its top-level
    string would miss the field that carries the credential.
    """
    if isinstance(value, str):
        return masked(value)
    if isinstance(value, Mapping):
        return {masked_obj(key): masked_obj(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(masked_obj(item) for item in value)
    if isinstance(value, list):
        return [masked_obj(item) for item in value]
    return value
