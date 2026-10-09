"""Skills — the playbooks in `<data>/skills/`, and what it takes to read one.

A skill is **not a tool**.  It is a document, and the difference is the whole
design: a tool is called with arguments the model invents, while a skill is
*read* and then followed — a procedure, the conventions around it, and the
command lines it expects.  v1 put it best by keeping the two families apart, and
what slife2 keeps from that is the shape of the folder:

    <data>/skills/
      browser-harness/
        SKILL.md          frontmatter: name, description.  The body is the skill.
        scripts/…         whatever the body tells the model to run
        references/…       whatever it tells the model to read

**The manifest names the skill.**  `name` and `description` are the skill's own
frontmatter rather than a list in the config, so the answer to "which skills are
installed" is reading the directory instead of reading a second file that has to
be kept in step with it — the same rule that keeps a tool server's tools out of
`slife2.yaml`.  A directory is a skill when it has a `SKILL.md` and not
otherwise; a stray folder or a half-finished download is then invisible rather
than a skill that fails when it is read.

**One unreadable manifest hides nothing.**  A `SKILL.md` that cannot be read
still yields its skill, named after its directory and with no description,
because the alternative is a folder that exists on disk and cannot be seen from
inside the system at all — which is a worse answer than a skill whose
description is missing.  Only a *directory* with no manifest is not a skill.

A skill declares what it needs
------------------------------
**A skill has no process, and it can still have a credential.**  A playbook that
says *run `python3 scripts/search.py`* is worth nothing if that script dies on a
missing `BAIDU_API_KEY` — and the model that just read the skill is the last one
to be told, because it is about to act on instructions that cannot work.  So the
skill's own header says what it needs, in the same block the publisher already
writes for other hosts:

    metadata: {"openclaw": {"requires": {"env": ["BAIDU_API_KEY"],
                                         "bins": ["python3"]},
                            "primaryEnv": "BAIDU_API_KEY"}}

and the host is expected to supply it.  The block is namespaced by the host a
skill was published *for*, so the reader takes any namespace that carries a
`requires` mapping: the same skill is meant to say the same thing to every host
that reads it, and reading only one namespace would make this system silently
blind to a skill that renamed its client.

**Where the value comes from is `slife2.yaml`, not here.**  A `skills:` entry
resolves its `env:` through the same chain as a provider key — shell, then
credstore — which is what makes a key that lives in the OS keyring reach a
script whose process was started days later by a daemon that never saw it.  This
module only reads the *declaration* and reports it; the resolution is
`slife2.config`'s, and injecting what it resolved is the business of whatever
runs a skill's scripts (`DESIGN.md` §9 — nothing does yet).

Why this is a module rather than part of the server
---------------------------------------------------
It is the reading half and nothing else: no FastMCP, no transport, no `_meta`,
no idea that a tool list exists.  `slife2.skills_server` turns what is here into
a tool and into catalogue rows — this file could be used by a `slife2 skills`
listing tomorrow without a line changing, and a server is free to be wrong about
skills without the folder being wrong about itself.  The split is the same one
every server here keeps between what a call *does* and how it is served: nothing
in this file would change if the tool were reached some other way.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from slife2.paths import skills_dir

logger = logging.getLogger(__name__)

#: The file that makes a directory a skill.  Upper case, and v1's name: it is
#: what every skill in the wild already calls this file.
MANIFEST = "SKILL.md"

#: The frontmatter's delimiter, and the fence a skill's own header sits between.
_FENCE = "---"

#: What the model calls to read one.  Named `skill_use` because v1's name is the
#: one that already appears in prompts and habits; a port that renamed it would
#: be a port somebody has to relearn for nothing.
#:
#: The name lives here and the *tool* is served by `slife2.skills_server`, which
#: takes its own name from this constant — and `slife2.toolhub` spells it too,
#: because the hub will not let the model unload it.  A name that crosses a
#: process boundary is spelled on both sides; `tests/test_config.py` holds the
#: two spellings together.
USE_TOOL = "skill_use"


#: A value that is still a `${...}` reference after resolution — what
#: `slife2.config.resolve_secret` leaves behind when neither the environment nor
#: the keyring had the name.  Deliberately not an error there: it fails where
#: the value is used, with its own name intact.  This is that failure seen from
#: the other side, and the reason a configured key and a configured *reference*
#: are not the same thing.
_REFERENCE = re.compile(r"\$\{[^}]*\}")


def unresolved(value: str) -> bool:
    """Whether a value meant to be a secret is still a reference to one."""
    return bool(_REFERENCE.fullmatch(value.strip())) or value.startswith("keyring:")


@dataclass(frozen=True)
class Requirements:
    """What a skill's own header says it needs before its instructions work.

    `env` is the part this system can act on: a name here with nothing behind it
    is a script that will die on its first line.  `bins` is the same fact about
    a program — the skill's own command line was written assuming it is on
    `PATH` — and `primary` is the one the publisher says matters most, which is
    what a person is told to set first.

    Empty is the common case, and it is not a failure: a playbook that is only
    prose needs nothing but a reader.
    """

    env: tuple[str, ...] = ()
    bins: tuple[str, ...] = ()
    primary: str = ""

    @property
    def declared(self) -> bool:
        return bool(self.env or self.bins)


@dataclass(frozen=True)
class Skill:
    """One skill on disk: what it is called, and where it lives.

    `name` is the frontmatter's if it has one and the directory's otherwise,
    because a skill that never says its name is still a skill and the directory
    is the only other thing that could name it.
    """

    name: str
    description: str
    directory: Path
    requires: Requirements = field(default_factory=Requirements)

    @property
    def manifest(self) -> Path:
        return self.directory / MANIFEST


def split_frontmatter(content: str) -> tuple[str, str]:
    """A `SKILL.md` split into its header and its body.

    The header is returned as *text* rather than as a mapping, because "this
    file has no frontmatter" and "this file has frontmatter that does not
    parse" are two different facts and only the caller can decide what to do
    with the second.  An unterminated fence counts as no frontmatter at all —
    v1's rule, and the safe half: whatever follows an unclosed `---` is body,
    and a body is never wrong to show.
    """
    lines = content.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return "", content
    for end in range(1, len(lines)):
        if lines[end].strip() == _FENCE:
            return "\n".join(lines[1:end]), "\n".join(lines[end + 1 :]).strip()
    return "", content


def frontmatter(content: str) -> dict[str, Any]:
    """A `SKILL.md`'s header, as a mapping.  Never raises.

    **Parsed as YAML, not as `key: value` lines.**  v1 read it line by line,
    which was enough right up until a description contained a colon and had to
    be quoted — and then the quotes arrived in the model's context as part of
    the description.  ruamel is already here for `slife2.yaml`, so the correct
    reading costs nothing to adopt.

    A header that does not parse degrades to no header rather than to an
    exception: the skill still exists, and `scan` names it after its directory.
    """
    header, _ = split_frontmatter(content)
    if not header.strip():
        return {}
    try:
        parsed = YAML(typ="safe").load(header)
    except YAMLError as exc:
        logger.warning("a SKILL.md header is not readable: %s", exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def requirements(header: Mapping[str, Any]) -> Requirements:
    """What a skill declares it needs, read out of its header's `metadata`.

    **Any namespace counts.**  `metadata` is keyed by the host a skill was
    published for — `openclaw` in the ones this project ships with — and the
    same skill is meant to say the same thing to every host that reads it.  A
    reader that insisted on one name would be blind to a skill that renamed its
    client, and blindness here is silent: the skill loads, reads perfectly, and
    dies on a missing key three steps later.

    Malformed blocks are skipped rather than fatal, for the same reason a
    malformed header is: the skill is still a skill.
    """
    env: list[str] = []
    bins: list[str] = []
    primary = ""

    metadata = header.get("metadata")
    for block in metadata.values() if isinstance(metadata, Mapping) else ():
        if not isinstance(block, Mapping):
            continue
        declares = block.get("requires")
        if isinstance(declares, Mapping):
            env += _names(declares.get("env"))
            bins += _names(declares.get("bins"))
        if not primary and block.get("primaryEnv"):
            primary = str(block["primaryEnv"])

    # Ordered-unique: a skill that declares the same key under two namespaces
    # should not read as needing it twice.
    return Requirements(
        env=tuple(dict.fromkeys(env)),
        bins=tuple(dict.fromkeys(bins)),
        primary=primary,
    )


def _names(raw: Any) -> list[str]:
    """A `requires:` list, as names.  A bare string is one name, not eight."""
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(one) for one in raw]
    return []


def scan(directory: Path | None = None) -> list[Skill]:
    """Every skill under `directory` — defaulting to `<data>/skills/`.

    Sorted by directory name, so the order a person sees does not depend on the
    order the filesystem happens to hand things back in.  A missing directory is
    an empty list rather than an error: a fresh install has no skills, and that
    is the honest answer to what is installed.
    """
    root = skills_dir() if directory is None else directory
    if not root.is_dir():
        return []

    found: list[Skill] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / MANIFEST
        if not manifest.is_file():
            continue
        try:
            content = manifest.read_text(encoding="utf-8")
        except OSError as exc:
            # Reported and *kept*: see the module docstring.  Empty content
            # takes the same path as a manifest with no header.
            logger.warning("skill %s: cannot read %s: %s", entry.name, MANIFEST, exc)
            content = ""
        header = frontmatter(content)
        found.append(
            Skill(
                name=str(header.get("name") or entry.name),
                description=str(header.get("description") or ""),
                directory=entry,
                requires=requirements(header),
            )
        )
    return found


def find(name: str, directory: Path | None = None) -> Skill | None:
    """The skill called `name`, by its frontmatter name first.

    Both namings are accepted because both are real: a skill that declares
    `name:` wants to be addressed by it, and one that does not is addressed by
    the directory somebody put it in.  The frontmatter wins when they disagree,
    which is the case that only arises when a skill was renamed after it was
    written — and the declaration is the more deliberate of the two.
    """
    fallback: Skill | None = None
    for skill in scan(directory):
        if skill.name == name:
            return skill
        if fallback is None and skill.directory.name == name:
            fallback = skill
    return fallback


def readiness(skill: Skill, supplied: Mapping[str, str] | None = None) -> str:
    """What the reader is told about a skill's declared needs, or `""`.

    **Read at the moment the playbook is handed over, and read from this side of
    it.**  The alternative is the model finding out the way the script does —
    an exception on a missing key, after it has already committed to following
    instructions that cannot work — and a model that has read a skill is exactly
    the reader who cannot tell a broken prerequisite from a broken instruction.

    `supplied` is what the host resolved from `skills:` in the config, which is
    the environment *plus* the keyring.  Both halves matter and they are not the
    same test: a name in `os.environ` is a value, and a config entry still
    holding its `${VAR}` reference is a name with nothing behind it —
    `resolve_secret` leaves the reference on purpose, so this has to check.

    Silence when a skill declares nothing, because there is nothing to say, and
    a line on every `skill_use` is a line the model learns to skip.
    """
    needs = skill.requires
    if not needs.declared:
        return ""

    missing = [
        *(f"${name}" for name in needs.env if not _present(name, supplied)),
        *(f"`{name}` on PATH" for name in needs.bins if shutil.which(name) is None),
    ]
    if not missing:
        return f"This skill's requirements are met ({_declared(needs)})."

    hint = (
        f" Set it with `credstore set {needs.primary}`"
        if needs.primary
        else " Supply it under `skills:` in slife2.yaml"
    )
    return (
        f"**This skill needs {' and '.join(missing)}, which is not configured — "
        f"the commands below will fail until it is.**{hint}."
    )


def _present(name: str, supplied: Mapping[str, str] | None) -> bool:
    """Whether one declared variable has a value behind it.

    The environment first, because that is what a script will actually see,
    then what the config resolved.  An unresolved reference counts as absent:
    the name is there and the value is not, which is the distinction this whole
    function exists to make.
    """
    found = os.environ.get(name) or (supplied or {}).get(name)
    return bool(found) and not unresolved(found)


def _declared(needs: Requirements) -> str:
    names = [*needs.env, *needs.bins]
    return ", ".join(names)


def document(skill: Skill, note: str = "") -> str:
    """A skill's `SKILL.md`, preceded by the root it lives under.

    v1's shape, kept for v1's reason: a skill's body names its own files by
    relative path (`scripts/search.py`), and a model reading it has no way to
    know what those are relative *to*.  One line in front of the document turns
    a path it cannot use into one it can.

    `note` rides in the same block rather than at the end, because a warning
    after a long playbook is a warning the reader has already stopped reading
    for.

    Raises:
        OSError: If the manifest cannot be read — the one failure this module
            has, and the caller's to report, because only the caller knows
            whether it is answering a model or a person.
    """
    text = skill.manifest.read_text(encoding="utf-8")
    root = skill.directory.parent.resolve()
    preamble = [
        f"> **Skills root:** `{root}`",
        "> Every path a skill names is relative to it.",
    ]
    if note:
        preamble.append(f"> {note}")
    return "\n".join([*preamble, "", text])


async def use(
    name: str,
    *,
    environments: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[str, bool]:
    """`skill_use` — read one skill.  Returns `(text, ok)`; never raises.

    The body of the tool, here rather than in `slife2.skills_server` because
    this is the only part of it that knows what a skill is.  A server knows how
    to *advertise* a tool and how to answer a call; what the call does is this
    file's business, the same way an upstream's behaviour is its own server's.

    `environments` is the `skills:` section's resolved `env:` per skill, handed
    in rather than read here: what a skill *needs* is the skill's own business,
    and what it is *given* is the operator's.  This function is the place the
    two meet, and it is the only place that needs both.

    A `(text, ok)` pair rather than a raise, because a skill that is not
    installed and a `SKILL.md` that cannot be read are both answers a model
    should read — the caller decides what a `False` means for its protocol.
    """
    name = name.strip()
    if not name:
        return "skill_use needs the name of a skill to read.", False

    skill = find(name)
    if skill is None:
        installed = ", ".join(one.name for one in scan()) or "(none installed)"
        return f"no skill called {name!r}. Installed: {installed}", False

    # Both names, because a skill is addressed by either and the operator
    # writing `skills:` has only one of them in front of them.
    given = environments or {}
    supplied = {**given.get(skill.name, {}), **given.get(skill.directory.name, {})}

    try:
        return document(skill, readiness(skill, supplied)), True
    except OSError as exc:
        return f"the skill {name!r} is on disk but cannot be read: {exc}", False


__all__ = [
    "MANIFEST",
    "USE_TOOL",
    "Requirements",
    "Skill",
    "document",
    "find",
    "frontmatter",
    "readiness",
    "requirements",
    "scan",
    "split_frontmatter",
    "unresolved",
    "use",
]
