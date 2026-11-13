"""The one way a pydantic refusal becomes the loader's `ValueError`.

`model_config` and `tool_config` validate two sections of one file, and both
turn a `ValidationError` into the sentence a caller reads.  The rule has two
halves — the field path is always named, and only an enum or a switch echoes the
value it got — and the second exists because pydantic puts the whole parent
mapping in `input` for a missing field, which can carry a literal credential.
Two copies of that rule are two chances to leak one.
"""

from __future__ import annotations

from pydantic import ValidationError

_MISSING = object()


def validation_refusal(where: str, exc: ValidationError) -> ValueError:
    """The first validation failure, in the config's own sentence.

    **The field path is the part a model acts on**, so it is always there, and
    the offending value is appended only when pydantic kept a value that is safe
    to echo — an enum's choice, a switch's boolean, a quirk's name.  A missing
    field's `input` is the whole parent mapping, and an unknown key's value can
    be a literal API key; neither is repeated.
    """
    first = exc.errors()[0]
    path = ".".join(str(part) for part in first.get("loc", ()))
    got = first.get("input", _MISSING)
    shown = first.get("type") in ("literal_error", "bool_type") and got is not _MISSING
    suffix = f" (got {got!r})" if shown else ""
    where = f"{where}.{path}" if path else where
    return ValueError(f"{where}: {first['msg']}{suffix}")
