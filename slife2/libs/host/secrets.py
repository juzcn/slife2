"""Resolved credentials, and the masked form that keeps them out of output.

The credential chain in `slife2.libs.host.config` turns a ``keyring:`` URI into a
plain string.  From that moment the only thing keeping the value out of a tool
result or a log line is every call site remembering not to print it — and a call
site that forgets is silent.

This module is the one place that remembers.  :func:`register` is called where a
**credstore** value resolves; :func:`masked` is applied where text leaves a
process: a tool result, a tool listing, and the log formatter.  The mask is
the conventional one — the first four and last four characters — but only when
at least as many characters stay hidden as are shown.  A value too short for
that, or one carrying control characters, becomes ``***``: a mask that reveals
most of the secret, or that copies a newline or an escape sequence into a log
line, protects nothing and can forge output.

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

from collections.abc import Mapping
from typing import Any

#: The shortest value that is registered.  See the module docstring.
MIN_LENGTH = 8

#: What a value too short to show a prefix and suffix of becomes.
SHORT = "***"

#: Characters shown at each end of a masked value, and the minimum that must
#: stay hidden.  A value shorter than ``2 * _SHOWN + _HIDDEN`` becomes
#: :data:`SHORT` instead: four and four out of nine characters is not a mask.
_SHOWN = 4
_HIDDEN = 8

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


def mask(value: str) -> str:
    """The conventional masked form of one credential.

    The same shape as ``credstore``'s terminal mask: four characters at each
    end.  A value with fewer than :data:`_HIDDEN` characters left over becomes
    :data:`SHORT` instead, and so does one carrying a control character — the
    visible ends are copied into model output and log lines, where a newline or
    an ANSI escape would be more than cosmetic.
    """
    if not value:
        return "(empty)"
    if len(value) < 2 * _SHOWN + _HIDDEN or not value.isprintable():
        return SHORT
    return f"{value[:_SHOWN]}{_SEPARATOR}{value[-_SHOWN:]}"


def _spans(text: str) -> list[tuple[int, int]]:
    """Every occurrence of every registered value, overlaps merged.

    **One pass per value rather than one alternation**, because a regex matches
    leftmost and then resumes after the match: two credentials that overlap in
    the output leave the tail of the second one behind.  Merging the ranges
    first means the whole covered region is replaced, and no fragment of any
    registered value survives.
    """
    found: list[tuple[int, int]] = []
    for secret in _SECRETS:
        start = text.find(secret)
        while start != -1:
            found.append((start, start + len(secret)))
            start = text.find(secret, start + 1)
    if not found:
        return []
    found.sort()
    merged = [found[0]]
    for start, end in found[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def masked(text: str) -> str:
    """*text* with every registered credential in its masked form."""
    if not text or not _SECRETS:
        return text
    spans = _spans(text)
    if not spans:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        parts.append(mask(text[start:end]))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


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
    if isinstance(value, (set, frozenset)):
        # Tool tags are a set, and `_masked_tool` names them as a surface.
        return type(value)(masked_obj(item) for item in value)
    return value
