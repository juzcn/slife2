"""The section loader: the uniformity every hand-written section inherits.

`slife2.libs.host.config` declares one `Field` tuple per section; the loader
that reads them lives in `slife2.libs.host.section`, and this is where its rules
are tested once — the unknown-key refusal, the typed readers, the bound, the
required sentence, and the defaults taken from the base.  A per-section test of
any of those would be a copy of this file, which is how the readers drifted
apart before the loader existed.

The stand-in settings class is deliberately not a real one: what is under test
is the loader, and a real section would make a failure here a failure there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from slife2.libs.host.section import (
    Field,
    SectionError,
    bound,
    parse_section,
    read_int,
    read_list,
    read_mapping,
    read_number,
    read_scalar,
    read_section,
    read_switch,
    read_text,
)


@dataclass(frozen=True)
class Settings:
    """What the loader fills, with defaults as the dataclass states them."""

    count: int = 7
    name: str = "kept"
    ratio: float = 0.5
    tags: tuple[str, ...] = ("a",)


FIELDS = (
    Field("count", read_int, check=bound(lambda n: n >= 1, "{value} is not a count")),
    Field("name", read_text),
    Field("ratio", read_number),
    Field("tags", read_list),
)


# --- the key list is the reader list -----------------------------------------


def test_a_key_the_fields_do_not_name_is_refused() -> None:
    """**A key nobody reads is a switch nobody throws** — the loader's rule.

    The readers this replaced each wrote their own allowed set beside their
    reads, and two forgot it.  Here the tuple is both, so the refusal cannot be
    forgotten while the section is still read.
    """
    with pytest.raises(SectionError) as refused:
        parse_section({"cout": 1}, "s", Settings(), FIELDS)

    message = str(refused.value)
    assert "unknown key(s) cout" in message
    assert "known keys are count, name, ratio, tags" in message


def test_a_duplicate_key_in_a_registry_is_a_programming_error() -> None:
    """Two fields for one key would silently drop one of the two reads."""
    with pytest.raises(RuntimeError, match="names one key twice"):
        read_section({"count": 1}, "s", (*FIELDS, Field("count", read_text)))


def test_a_section_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(SectionError, match="s: expected a mapping"):
        parse_section(["count"], "s", Settings(), FIELDS)


# --- defaults are the dataclass's --------------------------------------------


def test_an_absent_key_is_the_base_and_nothing_else() -> None:
    """**The default is written once — on the settings class.**

    `parse_section` completes the file's keys from `base`, so a key the file
    omits is whatever the dataclass says, and a partial section changes only
    what it names.
    """
    base = Settings()
    assert parse_section(None, "s", base, FIELDS) is base

    one = parse_section({"count": 9}, "s", base, FIELDS)
    assert one == Settings(count=9)
    assert one.name == base.name and one.ratio == base.ratio


def test_a_key_written_empty_is_an_absent_one() -> None:
    """`name:` with nothing after it is how a person clears a key."""
    for written in (None, "", "   "):
        assert parse_section({"name": written}, "s", Settings(), FIELDS).name == (
            "kept"
        )


def test_read_section_leaves_absent_keys_out_for_the_dataclass() -> None:
    """An entry reader has no base; what it does not find, the class defaults."""
    found = read_section({"count": 2}, "s", FIELDS)
    assert found == {"count": 2}
    assert Settings(**found).name == "kept"


# --- a reader cannot hand a wrong type on ------------------------------------


def test_a_text_field_refuses_a_number_rather_than_converting_it() -> None:
    """`str(8080)` is a host nobody dialled; the shape is the typo."""
    with pytest.raises(SectionError, match="s.name: expected a string, got int"):
        parse_section({"name": 8080}, "s", Settings(), FIELDS)


def test_a_credential_is_the_one_scalar_that_is_read_as_text() -> None:
    """A pasted key can be all digits, so `read_scalar` converts — and a
    container is still refused, because no credential is a mapping."""
    assert read_scalar(123456, "s.key") == "123456"
    assert read_scalar(True, "s.key") == "True"
    with pytest.raises(SectionError, match="expected a scalar, got dict"):
        read_scalar({"api_key": "x"}, "s.key")


def test_a_whole_number_refuses_a_bool_and_a_fraction() -> None:
    """`int(True)` is 1 and `int(2.9)` is 2 — numbers nobody wrote."""
    for written in (True, 2.9, ".inf"):
        with pytest.raises(SectionError, match="expected a whole number"):
            read_int(written, "s.count")
    # A finite, whole float is still the number it spells.
    assert read_int(2.0, "s.count") == 2


def test_a_number_refuses_a_bool_and_non_finite_values() -> None:
    for written in (False, ".nan", ".inf"):
        with pytest.raises(SectionError, match="expected a"):
            read_number(written, "s.ratio")
    assert read_number(2, "s.ratio") == 2.0


def test_a_list_reads_scalar_numbers_as_text_and_refuses_containers() -> None:
    assert read_list([8080, "x"], "s.tags") == ("8080", "x")
    with pytest.raises(SectionError, match="a list of strings"):
        read_list([{"port": 1}], "s.tags")
    # A list written empty says "none" rather than "the default".
    assert read_list(None, "s.tags") == ()


def test_a_switch_is_a_boolean_or_it_is_refused() -> None:
    assert read_switch(False, "s.enabled") is False
    for written in ("no", "off", "'false'", 1, ""):
        with pytest.raises(SectionError, match="expected `true` or `false`"):
            read_switch(written, "s.enabled")


def test_an_absent_mapping_is_empty() -> None:
    assert read_mapping(None, "s.things") == {}
    with pytest.raises(SectionError, match="s.things: expected a mapping"):
        read_mapping("x", "s.things")


# --- bounds and required keys ------------------------------------------------


def test_a_bound_refuses_the_written_value_and_names_the_field() -> None:
    """The sentence is completed as `{where}: {value} …`, once for every bound."""
    with pytest.raises(SectionError) as refused:
        parse_section({"count": 0}, "s", Settings(), FIELDS)
    assert str(refused.value) == "s.count: 0 is not a count"


def test_a_bound_does_not_run_on_the_default() -> None:
    """The base is by construction a value the build accepts; only a written
    key is judged."""
    out_of_bounds = Settings(count=0)
    assert parse_section({}, "s", out_of_bounds, FIELDS).count == 0


def test_a_required_key_is_refused_with_its_own_sentence() -> None:
    """**The sentence is declared with the field**, so a section cannot require
    a key in prose and forget to refuse its absence."""
    fields = (Field("api", read_text, required="needs `api` — the wire adapter"),)
    with pytest.raises(SectionError) as refused:
        read_section({}, "media.providers.p", fields)
    assert str(refused.value) == "media.providers.p: needs `api` — the wire adapter"


def test_a_required_key_written_empty_is_the_same_absence() -> None:
    """**A key present but empty is not a value.**  `read_text` answers `None`
    for `api: ""` and `api:` — both are how a person clears a key — and the
    required sentence is the one this loader owes them.

    Leaving it out of the dict made the entry constructors splat an incomplete
    mapping: `TypeError: ProviderSettings.__init__() missing 'api'`, a
    traceback past every `ConfigError` handler rather than the section's own
    refusal.
    """
    fields = (Field("api", read_text, required="needs `api`"),)
    for raw in ({}, None, {"api": ""}, {"api": "   "}, {"api": None}):
        with pytest.raises(SectionError, match="needs `api`"):
            read_section(raw, "providers.p", fields)


def test_a_required_key_that_was_read_is_accepted() -> None:
    fields = (Field("api", read_text, required="needs `api`"),)
    assert read_section({"api": "openai-completions"}, "p", fields) == {
        "api": "openai-completions"
    }


def test_keys_are_read_in_registry_order_not_the_files() -> None:
    """Which refusal a file with two mistakes gets is the registry's answer."""
    with pytest.raises(SectionError) as refused:
        parse_section({"name": 1, "count": 0}, "s", Settings(), FIELDS)
    assert "s.count" in str(refused.value)


def test_a_refusal_sentence_may_spell_a_reference() -> None:
    """`${VAR}` is not formatting: only `{value}` and `{value!r}` are filled."""
    check = bound(lambda n: False, "{value} is not it — see ${SOME_VAR}")
    with pytest.raises(SectionError, match=r"see \$\{SOME_VAR\}"):
        check(3, "s.count")


def test_a_bound_can_quote_the_value_with_its_repr() -> None:
    """A string value reads better quoted, and `{value!r}` is the one spelling."""
    check = bound(lambda kind: False, "not {value!r}")
    with pytest.raises(SectionError, match="not 'sound'"):
        check("sound", "s.kind")


# --- one key, several attributes ---------------------------------------------


def _host_port(raw: Any, where: str) -> tuple[Any, Any]:
    found = read_mapping(raw, where)
    return found.get("host"), found.get("port")


def test_a_nested_mapping_can_fill_several_attributes() -> None:
    """`broker:` is one key and two settings, and either half may be omitted."""
    fields = (Field("addr", _host_port, attrs=("host", "port")),)

    assert read_section({"addr": {"host": "h", "port": 1}}, "s", fields) == {
        "host": "h",
        "port": 1,
    }
    # A half not written is left to the default rather than set to `None`.
    assert read_section({"addr": {"port": 1}}, "s", fields) == {"port": 1}
