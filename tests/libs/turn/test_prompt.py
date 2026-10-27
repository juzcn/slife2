"""The guide is the home's, and only the system prompt reads it.

`AGENTS.md` — the model's guide to its own tools — is a document a person writes
beside the configuration, not a template shipped inside the package.  These
tests hold that boundary: the system prompt carries the home's text, a prompt of
one's own can place it, an instruction template reads nothing from the home, and
a home without the guide is refused by name rather than rendered with a blank
section.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slife2.libs.host.config import ConfigError
from slife2.libs.turn.prompt import render_instruction, render_system_prompt

pytestmark = pytest.mark.unit


def test_the_system_prompt_carries_the_homes_agents_md(isolated_runtime: Path) -> None:
    """The shipped prompt opens with the home document, read from the home.

    `isolated_runtime` writes a stub guide; replacing it here is the whole
    assertion that the text in the prompt is *this home's* file rather than
    anything packaged with the build.
    """
    (isolated_runtime / "AGENTS.md").write_text("THE HOME'S GUIDE\n", encoding="utf-8")

    prompt = render_system_prompt(None, agent_name="jack")

    assert "THE HOME'S GUIDE" in prompt
    assert prompt.count("THE HOME'S GUIDE") == 1


def test_a_prompt_of_ones_own_can_place_the_guide(
    tmp_path: Path, isolated_runtime: Path
) -> None:
    """`agents_md` is in the context, so a config-named prompt decides where."""
    (isolated_runtime / "AGENTS.md").write_text("GUIDE\n", encoding="utf-8")
    template = tmp_path / "mine.j2"
    template.write_text("before\n{{ agents_md }}after", encoding="utf-8")

    assert render_system_prompt(template) == "before\nGUIDE\nafter"


def test_a_home_without_its_guide_is_refused_by_name(
    isolated_runtime: Path,
) -> None:
    """A missing guide is an error, not a prompt with a blank section.

    The guide is required as the configuration is: a home without one is named
    where the prompt is rendered, and the sentence says where the file goes.
    """
    (isolated_runtime / "AGENTS.md").unlink()

    with pytest.raises(ConfigError, match="AGENTS.md") as failed:
        render_system_prompt(None, agent_name="jack")

    assert str(isolated_runtime) in str(failed.value)


def test_a_guide_that_cannot_be_read_is_not_a_missing_one(
    isolated_runtime: Path,
) -> None:
    """**A failure is not a verdict** (SDD Appendix C): a directory sitting
    where the guide should be is named as unreadable, not as a home without
    one — the two send a person to different fixes."""
    guide = isolated_runtime / "AGENTS.md"
    guide.unlink()
    guide.mkdir()

    with pytest.raises(ConfigError, match="cannot read"):
        render_system_prompt(None, agent_name="jack")


def test_an_instruction_template_reads_nothing_from_the_home(
    tmp_path: Path, isolated_runtime: Path
) -> None:
    """`render_instruction` is one turn's text, and a turn is the same in every home.

    The home's guide is deleted before the render to prove the instruction path
    never looked for it: `recall.j2` and its siblings must not acquire a home
    dependency the system prompt alone is supposed to have.
    """
    (isolated_runtime / "AGENTS.md").unlink()
    template = tmp_path / "instruction.j2"
    template.write_text("one turn's text", encoding="utf-8")

    assert render_instruction(template) == "one turn's text"


def test_an_instruction_template_that_asks_for_the_guide_is_refused(
    tmp_path: Path,
) -> None:
    """The two entry points are not interchangeable.

    A template rendered through `render_instruction` that asks for `agents_md` is
    a system prompt rendered the wrong way; an empty string would drop the
    section it asked for without a word, so the value raises instead.
    """
    template = tmp_path / "instruction.j2"
    template.write_text("{{ agents_md }}", encoding="utf-8")

    with pytest.raises(ConfigError, match="render_system_prompt"):
        render_instruction(template)
