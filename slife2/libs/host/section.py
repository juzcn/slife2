"""One reader for the hand-written sections of `slife2.yaml`.

Every section is the same shape: a mapping, a set of keys this build takes, a
type each value must have, a bound it must meet, and a default an absent key
means.  Written by hand those five drift apart — two readers that should refuse
a typo do not, a number in a text field becomes its digits in one section and
is refused in another, and the missing-key refusal is a check each parser has
to remember.  A `Field` states all five in one place, and `read_section` reads
a whole section through them, so a section cannot take a key it does not read
or read one it does not take: one tuple is both the reader list and the list of
keys the section takes.

**The defaults are the dataclass's, and only its.**  `parse_section` reads a
section onto the `base` it is given and fills every key the file omits from it,
so an absent key means what the settings class already says it means.
`read_section` — the form an *entry* reader uses, where there is no base —
leaves the absent keys out and the dataclass's own field defaults apply.

The pydantic families (`model_config`, `tool_config`) are the same idea with a
validation model behind it.  These sections have no model — they are a handful
of numbers and names — and the one thing they need is that the three questions
are asked by one reader.

A refusal here is a `SectionError`; the config loader converts it to its own
`ConfigError` at the one boundary every section passes through.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, cast


class SectionError(ValueError):
    """A section says something this build refuses, in the section's own words."""


#: Turns one raw YAML value into the value a settings object holds — or `None`
#: for a key that is not written: absent, `null`, or empty.  The loader leaves
#: it to the default (the `base` in `parse_section`, the dataclass's own field
#: default where there is none), so no setting here takes `None` as a value.
Reader = Callable[[Any, str], Any]

#: A field's bound: refuses a value the reader already accepted, with the
#: sentence the section refuses it in.
Check = Callable[[Any, str], None]


@dataclass(frozen=True)
class Field:
    """One key of one section: how it is read, and what its value must satisfy.

    *read* turns the raw value into the typed one (`None` for a key written
    empty).  *check* — the bound — runs on a value the key wrote and refuses
    it; it is declared with the field, so a bound cannot be forgotten beside a
    key that is read.  *required* is the sentence the key's **absence** is
    refused with, for an entry that cannot work without it.

    *attrs* names the setting attributes the key fills, when they are not the
    key itself: `api_key:` fills `api_key_ref`, and a nested mapping such as
    `broker:` fills several (`broker_host`, `broker_port`) from one read, whose
    value is then a tuple in the same order.
    """

    key: str
    read: Reader
    check: Check | None = None
    required: str = ""
    attrs: tuple[str, ...] | str = ""

    def __post_init__(self) -> None:
        # Frozen, so the default is resolved once here rather than by every
        # reader of `attrs` falling back on its own.
        if not self.attrs:
            object.__setattr__(self, "attrs", (self.key,))
        elif isinstance(self.attrs, str):
            object.__setattr__(self, "attrs", (self.attrs,))


def refuse_unknown(raw: Mapping[Any, Any], where: str, allowed: Iterable[str]) -> None:
    """Refuse a key *raw* writes that *allowed* does not name.

    **A key nobody reads is a switch nobody throws.**  The sections were read
    key by key, so a misspelt `max_iteration` loaded cleanly and meant "no
    cap" — the most expensive reading of a value the operator wrote to be
    safe.  It is refused rather than ignored, naming the keys that do exist.
    """
    allowed = set(allowed)
    unknown = sorted(str(key) for key in set(raw) - allowed)
    if unknown:
        raise SectionError(
            f"{where}: unknown key(s) {', '.join(unknown)} — known keys are "
            f"{', '.join(sorted(allowed))}"
        )


def read_section(raw: Any, where: str, fields: Iterable[Field]) -> dict[str, Any]:
    """The values *raw* writes, read through *fields*; absent keys are left out.

    **One loader, and the key list is the reader list.**  A section's fields are
    its own statement of what it takes, so an unknown key is refused from the
    same tuple that reads the known ones — the refusal cannot be forgotten
    while the section is still parsed, which is how it went missing in the
    readers this replaced.

    Keys are read in *fields* order, not the file's, so which refusal a file
    with two mistakes gets is the registry's answer rather than a property of
    how somebody happened to order the YAML.
    """
    if raw is None:
        # A null *entry* (`p:` with nothing under it) is an empty mapping to
        # this reader, so its required keys get their sentence below rather
        # than a constructor's `TypeError`.  A whole absent *section* never
        # reaches here: `parse_section` answers its base first.
        raw = {}
    if not isinstance(raw, dict):
        raise SectionError(f"{where}: expected a mapping")
    fields = tuple(fields)
    by_key = {field.key: field for field in fields}
    if len(by_key) != len(fields):  # pragma: no cover - import-time registry bug
        raise RuntimeError(f"{where}: a field list names one key twice")
    refuse_unknown(raw, where, by_key)
    found: dict[str, Any] = {}
    for field in fields:
        if field.key not in raw:
            if field.required:
                raise SectionError(f"{where}: {field.required}")
            continue
        key_where = f"{where}.{field.key}"
        value = field.read(raw[field.key], key_where)
        if len(field.attrs) == 1:
            pairs = [(field.attrs[0], value)]
        else:
            pairs = list(zip(field.attrs, value, strict=True))
        # **A required key written empty is still an absence.**  A reader
        # answers `None` for "not written" — absent, `null`, or an empty
        # scalar — and the entry constructors then splat a dict without the
        # attribute, which is a `TypeError` rather than this section's
        # refusal.  So the same sentence absence gets is answered here, where
        # the key is being read.
        if field.required and all(one is None for _attr, one in pairs):
            raise SectionError(f"{where}: {field.required}")
        for attr, one in pairs:
            if one is None:
                continue
            if field.check is not None:
                field.check(one, key_where)
            found[attr] = one
    return found


def parse_section[T](raw: Any, where: str, base: T, fields: Iterable[Field]) -> T:
    """A whole section, read through *fields* and completed from *base*.

    **The defaults are `base`'s**, so a setting's default is written once — on
    the dataclass — and an absent key means exactly what that default says.
    A `raw` of `None` is an absent section, which is the base unchanged.
    """
    if raw is None:
        return base
    return replace(cast(Any, base), **read_section(raw, where, fields))


def read_mapping(value: Any, where: str) -> dict[str, Any]:
    """*value* as a mapping; an absent one (`None`) is empty.

    `None` is `{}` because an absent section and an empty one mean the same
    thing everywhere this is used; anything that is neither is a shape mistake
    and is refused here rather than by a `.get` failing further down.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SectionError(f"{where}: expected a mapping")
    return value


def read_text(value: Any, where: str) -> str | None:
    """A string, stripped; absent or empty is `None`.

    **A non-string is refused, not converted.**  `str(8080)` is a host nobody
    dialled and `str(["npx", "foo"])` is a program name no machine has — a
    value written in the wrong shape is a typo, and refusing it here is the
    same rule the canonical objects' `str` fields apply.  An empty scalar is
    the one other reading: it is how a person clears a key, so it means the
    default rather than the empty string.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        # A mapping given to a text field can carry a literal credential, so
        # it is named by its type rather than echoed (`validation_refusal`'s
        # rule).
        raise SectionError(f"{where}: expected a string, got {type(value).__name__}")
    return value.strip() or None


def read_scalar(value: Any, where: str) -> str | None:
    """A scalar as written — how a credential reference is read.

    **A credential is the one exception to `read_text`'s refusal of a
    non-string**: `api_key: 123456` is a value somebody pasted, not a typo,
    and `resolve_secret` documents the same reading.  Anything that is not a
    scalar is still refused, because no credential is written as a mapping.
    """
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        raise SectionError(f"{where}: expected a scalar, got {type(value).__name__}")
    return str(value).strip() or None


def read_int(value: Any, where: str) -> int | None:
    """A whole number, or absent — **and a bool is not one**.

    `int(True)` is 1 and `int(2.9)` is 2, so a bare `int()` turned
    `port: true` into port 1 and a fractional count into a different number
    nobody wrote.  `inf` is the third: `int(inf)` raises `OverflowError`, which
    is past every `except ConfigError` and reaches the user as a traceback
    (`.inf` is just how some people write "no cap").

    A written `0` is a written zero and reaches the section's bound; only an
    absent key is the default, because `value or default` read `port: 0` and
    `threshold: 0` as "not given" — the two values their bounds exist to refuse.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise SectionError(f"{where}: expected a whole number, got {value!r}")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SectionError(f"{where}: expected a whole number, got {value!r}") from exc
    if isinstance(value, float) and (not math.isfinite(value) or value != number):
        # A finite, whole float (`2.0`) is still the number it spells; a
        # fractional one is not the setting's whole number, and truncating it
        # would apply a value the file did not contain.
        raise SectionError(f"{where}: expected a whole number, got {value!r}")
    return number


def read_number(value: Any, where: str) -> float | None:
    """A finite number, or absent.  A bool is not a number either."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise SectionError(f"{where}: expected a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SectionError(f"{where}: expected a number, got {value!r}") from exc
    if not math.isfinite(number):
        raise SectionError(f"{where}: expected a finite number, got {value!r}")
    return number


def read_list(value: Any, where: str) -> tuple[str, ...]:
    """A list of strings — a scalar number is text, a container is a mistake.

    `args: [8080]` is a port written the way YAML writes numbers, and the
    command line it becomes is text either way; `args: [{port: 8080}]` is a
    mapping `str()` would render as a program argument nobody meant.  The
    canonical objects' `list[str]` draws the same line.

    A list written empty is an empty list rather than `None`: the key was
    written, and what it says is "none".
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SectionError(f"{where}: expected a list")
    items: list[str] = []
    for item in value:
        if isinstance(item, str):
            items.append(item)
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            items.append(str(item))
        else:
            raise SectionError(
                f"{where}: expected a list of strings, got {type(item).__name__}"
            )
    return tuple(items)


def read_switch(value: Any, where: str) -> bool:
    """`true` or `false`, and nothing else — an entry's `enabled:` switch.

    **`bool(raw.get("enabled", True))` read a string as a switch.**  ruamel's
    loader is YAML 1.2, so `enabled: no` and `enabled: off` are the *strings*
    `no` and `off` — not the booleans PyYAML would have made of them — and
    every non-empty string is true to `bool`.  So an entry the operator had
    switched off came back on, and the plugin dialled it at every start,
    because `settings.enabled` is the whole of what decides whether a link is
    made.  The reading is wrong in the one direction that costs something: the
    server is started and asked, rather than not.

    **The value is refused rather than guessed at.**  Coercing a string to a
    boolean is the other half of the same mistake, one spelling over — it
    replaces "the switch this says is on" with "the switch it did not say"; a
    quoted `"false"` is a hand-edit that means nothing at all, and a *value
    that cannot be read is not an absent one*.
    """
    if not isinstance(value, bool):
        # A scalar is echoed; a mapping given to a switch can carry a literal
        # credential, so it is named by its type (`validation_refusal`'s rule).
        shown = (
            repr(value)
            if isinstance(value, (str, int, float))
            else type(value).__name__
        )
        raise SectionError(
            f"{where}: expected `true` or `false`, got {shown} — YAML "
            f"spells a boolean as one, so `no`, `off` and a quoted 'false' are "
            f"strings; leave the key out for true"
        )
    return value


def bound(test: Callable[[Any], bool], refusal: str) -> Check:
    """A field's bound, and the sentence a value outside it is refused with.

    *refusal* is completed as ``{where}: …`` and its ``{value}`` (or
    ``{value!r}``, for a value whose type matters) is replaced by the value
    that failed, so every bound names its field and its value the same way.
    Nothing else is substituted — a sentence may spell a `${VAR}` reference
    without the braces being read as formatting.
    """

    def check(value: Any, where: str) -> None:
        if not test(value):
            sentence = refusal.replace("{value!r}", repr(value)).replace(
                "{value}", str(value)
            )
            raise SectionError(f"{where}: {sentence}")

    return check
