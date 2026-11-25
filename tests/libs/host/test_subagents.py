"""A subagent's own record: what it runs on, and what it is for.

One file answers two questions, and the answers are read in two places.  A
list or a transcript may show a directory whose record is damaged — it exists,
and refusing to show it would fail a read over a half-written file.  A
conversation that would *run* on the answer must not silently become the
maker's model and the plain role: that is a differently configured subagent,
which is the damage Appendix C refuses.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from slife2.libs.host import subagents

pytestmark = pytest.mark.unit


@contextmanager
def a_record(text: str) -> Iterator[Path]:
    """A temporary `subagent.yaml`, patched in as the one this module reads."""
    directory = tempfile.TemporaryDirectory()
    path = Path(directory.name) / "subagent.yaml"
    path.write_text(text, encoding="utf-8")
    original = subagents.subagent_file
    subagents.subagent_file = lambda agent, subagent: path
    try:
        yield path
    finally:
        subagents.subagent_file = original
        directory.cleanup()


def test_the_makers_own_conversation_needs_no_record() -> None:
    assert subagents.subagent_spec("jack", "") == subagents.SubagentSpec()


def test_a_damaged_record_is_flagged_for_a_list_not_read_as_defaults() -> None:
    """**Damage is not "the maker's model and the plain role".**

    `read_subagent` still answers `{}` for the removal path, where the file's
    existence is the membership and the contents are not needed.  The spec a
    list or a transcript shows carries `unreadable` and the error, so a reader
    cannot mistake the empty settings for what the subagent was configured with.
    """
    with a_record(":: not yaml ::"):
        assert subagents.read_subagent("jack", "helper") == {}
        spec = subagents.subagent_spec("jack", "helper")
        assert spec.unreadable is True
        assert "could not be read" in spec.error
        assert spec.model == "" and spec.role == subagents.DEFAULT_ROLE


def test_a_damaged_record_refuses_a_conversation_that_would_run_on_it() -> None:
    with a_record(":: not yaml ::"):
        with pytest.raises(ValueError, match="could not be read"):
            subagents.subagent_spec("jack", "helper", strict=True)


def test_a_record_that_is_not_a_mapping_is_refused_strictly() -> None:
    with a_record("- one\n- two\n"):
        with pytest.raises(ValueError, match="not a mapping"):
            subagents.read_subagent("jack", "helper", strict=True)
