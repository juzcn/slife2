"""Skills — the playbooks in `<data>/skills/`, and what it takes to read one.

A skill is **not a tool**.  It is a document, and the difference is the whole
design: a tool is called with arguments the model invents, while a skill is
*read* and then followed — a procedure, the conventions around it, and the
command lines it expects.  The two families are kept apart, and the shape of the
folder follows from that:

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

**A manifest that does not parse is not a manifest with no header.**  Damage and
absence are different facts: a `SKILL.md` with no frontmatter is a prose skill
and is read, while one whose header cannot be read is refused — the same
`SkillHeaderError` at install (`install` writes nothing) and at load (`scan`
keeps the skill visible as an `error` row and `skill_use` refuses it).  Reading
a damaged header as an empty one would drop its declared name, description and
requirements while looking like a skill that simply declared none.

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
`slife2.libs.host.config`'s, and injecting what it resolved is the business of whatever
runs a skill's scripts (`SDD` §14.2 — nothing does yet).

Authoring one
-------------
**A workflow becomes a playbook through a draft, and the draft writes nothing.**
`draft` renders the `SKILL.md` a procedure needs and hands back the
`files` argument `install` or `edit` takes — so the name's rules, the header's
parser and the folder stay in one place, and a model sees the document before it
exists.

Why this is a module rather than part of the server
---------------------------------------------------
It is everything that knows what a skill is — the folder, the manifest, its
reading and its writing — and nothing else: no FastMCP, no transport, no `_meta`,
no idea that a tool list exists.  `slife2.plugins.skills.server` turns what is here into
a tool and into catalogue rows — this file could be used by a `slife2 skills`
listing tomorrow without a line changing, and a server is free to be wrong about
skills without the folder being wrong about itself.  The split is the same one
every server here keeps between what a call *does* and how it is served: nothing
in this file would change if the tool were reached some other way.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from slife2.libs.host.paths import skills_dir

logger = logging.getLogger(__name__)

#: The file that makes a directory a skill.  Upper case, a name every skill
#: in the wild already uses for this file.
MANIFEST = "SKILL.md"

#: The frontmatter's delimiter, and the fence a skill's own header sits between.
_FENCE = "---"

#: What the model calls to read one.  Named `skill_use` because that name
#: already appears in prompts and habits; renaming it would be a change
#: somebody has to relearn for nothing.
#:
#: The name lives here and the *tool* is served by `slife2.plugins.skills.server`, which
#: takes its own name from this constant — and `slife2.plugins.toolhub.server` spells it too,
#: because the hub will not let the model unload it.  A name that crosses a
#: process boundary is spelled on both sides; `tests/test_config.py` holds the
#: two spellings together.
USE_TOOL = "skill_use"


#: A value that is still a `${...}` reference after resolution — what
#: `slife2.libs.host.config.resolve_secret` leaves behind when neither the environment nor
#: the keyring had the name.  Deliberately not an error there: it fails where
#: the value is used, with its own name intact.  This is that failure seen from
#: the other side, and the reason a configured key and a configured *reference*
#: are not the same thing.
_REFERENCE = re.compile(r"\$\{[^}]*\}")


def unresolved(value: str) -> bool:
    """Whether a value meant to be a secret is still a reference to one."""
    return bool(_REFERENCE.fullmatch(value.strip())) or value.startswith("keyring:")


class SkillHeaderError(ValueError):
    """A `SKILL.md` whose header is present but cannot be read.

    A plain `ValueError` because `skills.install` already translates a
    `ValueError` into the caller's refusal, and because a header this build
    cannot read is a bad value rather than a missing file (`OSError`).
    """


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
    #: Why this skill's manifest was refused, when it was.  Empty for a skill
    #: this build can read; `scan` keeps a damaged one visible (see the module
    #: docstring) while `document` refuses to serve it.
    error: str = ""

    @property
    def manifest(self) -> Path:
        return self.directory / MANIFEST


def _read(manifest: Path) -> str:
    """A skill file's text, with a UTF-8 BOM tolerated.

    **A `SKILL.md` from a Windows editor or an upstream download may begin with a
    byte-order mark, and the fence check is a `strip()`** — `'\\ufeff---'.strip()`
    is still `'\\ufeff---'`, and `'\\ufeff'.isspace()` is false — so a file that
    plainly has frontmatter would read as one that has none.  The skill loses
    its declared name, its description and every requirement it stated, and the
    raw `---`/`name:`/`metadata:` header is handed to the model as the top of the
    playbook.  `utf-8-sig` drops the mark when it is there and is plain UTF-8
    when it is not, which is why every read of a skill file goes through here:
    `scan` and `document` are the two, and a third reader would be a third place
    where the mark silently wins.
    """
    try:
        return manifest.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        # **A file that is not UTF-8 is a file that could not be read**, which is
        # the failure every caller already answers: `scan` keeps the skill with
        # an empty header, `use` refuses it by name, and the catalogue gives it a
        # row that says `error`.  Translated here rather than caught as a decode
        # error at each of them, because "this file cannot be read" is one fact
        # and this is the only place that reads one — and a `UnicodeDecodeError`
        # is a `ValueError`, so every `except OSError` guard around this call
        # missed it and the raw codec error reached the hub instead, failing the
        # whole `list_sources` for every home.  The person is the one the
        # docstring above is about, one step further: an editor that saved the
        # file in the machine's own codepage rather than as UTF-8.
        raise OSError(
            f"not UTF-8 text ({exc.reason} at byte {exc.start}) — save the file "
            f"as UTF-8"
        ) from exc


def split_frontmatter(content: str) -> tuple[str, str]:
    """A `SKILL.md` split into its header and its body.

    The header is returned as *text* rather than as a mapping, because "this
    file has no frontmatter" and "this file has frontmatter that does not
    parse" are two different facts; `frontmatter` is where the second is
    refused.

    **An unterminated fence is refused, not read as no frontmatter.**  A file
    that opened a header and never closed it is damaged: whatever follows the
    opening `---` was meant to be a header, and handing it to the model as the
    top of the playbook hides the damage while looking like a prose skill.
    """
    lines = content.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return "", content
    for end in range(1, len(lines)):
        if lines[end].strip() == _FENCE:
            return "\n".join(lines[1:end]), "\n".join(lines[end + 1 :]).strip()
    raise SkillHeaderError(
        "the header's opening `---` has no closing `---`; a `SKILL.md` with a "
        "header must close it"
    )


def _validate_header(header: Mapping[str, Any]) -> None:
    """Refuse a header whose declaration this build cannot read.

    **A malformed declaration is damage, and damage is refused.**  `metadata`
    is the block a publisher writes requirements in; skipping a malformed one
    would load the skill without the prerequisite it meant to state, which is
    exactly the silence `requirements` exists to remove.  The checks are shaped
    as the reader reads: `metadata` is a mapping of host → block, `requires` is
    a mapping, and `env`/`bins` are a string or a list of strings.
    """
    for key in ("name", "description"):
        value = header.get(key)
        if value is not None and not isinstance(value, str):
            raise SkillHeaderError(f"`{key}` is not a string")
    metadata = header.get("metadata")
    if metadata is None:
        return
    if not isinstance(metadata, Mapping):
        raise SkillHeaderError("`metadata` is not a mapping")
    for host, block in metadata.items():
        if not isinstance(block, Mapping):
            raise SkillHeaderError(f"`metadata.{host}` is not a mapping")
        requires = block.get("requires")
        if requires is None:
            continue
        if not isinstance(requires, Mapping):
            raise SkillHeaderError(f"`metadata.{host}.requires` is not a mapping")
        for key in ("env", "bins"):
            value = requires.get(key)
            if value is None or isinstance(value, str):
                continue
            if isinstance(value, list) and all(isinstance(one, str) for one in value):
                continue
            raise SkillHeaderError(
                f"`metadata.{host}.requires.{key}` is not a string or a list of strings"
            )


def frontmatter(content: str) -> dict[str, Any]:
    """A `SKILL.md`'s header, as a mapping.  Raises `SkillHeaderError` on damage.

    **Parsed as YAML, not as `key: value` lines.**  Reading it line by line is
    enough right up until a description contains a colon and has to be quoted —
    and then the quotes arrive in the model's context as part of the
    description.  ruamel is already here for `slife2.yaml`, so the correct
    reading costs nothing.

    **Absent and damaged are different answers.**  A file with no frontmatter
    is a prose skill and answers `{}`; a file whose header is not YAML, is not a
    mapping, or declares a malformed `metadata`/`requires` block raises.  The
    same function is the install gate (`install` calls it before writing) and
    the load gate (`scan` calls it for every manifest on disk), so the same file
    is refused for the same reason in both places — SDD Appendix A's "one rule,
    one place".
    """
    header, _ = split_frontmatter(content)
    if not header.strip():
        return {}
    try:
        parsed = YAML(typ="safe").load(header)
    except YAMLError as exc:
        raise SkillHeaderError(f"the header is not YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SkillHeaderError("the header is not a mapping")
    _validate_header(parsed)
    return parsed


def requirements(header: Mapping[str, Any]) -> Requirements:
    """What a skill declares it needs, read out of its header's `metadata`.

    **Any namespace counts.**  `metadata` is keyed by the host a skill was
    published for — `openclaw` in the ones this project ships with — and the
    same skill is meant to say the same thing to every host that reads it.  A
    reader that insisted on one name would be blind to a skill that renamed its
    client, and blindness here is silent: the skill loads, reads perfectly, and
    dies on a missing key three steps later.

    **A malformed `metadata` block does not reach this function.**  The header
    parser (`_validate_header`) refuses one before a `Skill` is built, because a
    declaration that cannot be read is damage and reading it as
    "declares nothing" is how a skill loads without the prerequisite it meant to
    state.  The tolerant branches below are the reader's own shape check, kept
    for callers that hand `requirements` a mapping built somewhere else.
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

    **A dot-directory is not a skill.**  `install` stages into `.name.installing`
    and moves the old copy to `.name.replacing`, and a hard kill in that window
    leaves one behind — a directory with a real `SKILL.md` in it, which would
    otherwise be listed, could win `find(name)` (its frontmatter name wins, and
    it sorts first), and would become the catalogue row for that name.  The
    promise the swap makes is that an interrupted install does not exist; this is
    the half of it that survives a `SIGKILL`.
    """
    root = skills_dir() if directory is None else directory
    if not root.is_dir():
        return []

    found: list[Skill] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        manifest = entry / MANIFEST
        if not manifest.is_file():
            continue
        error = ""
        try:
            content = _read(manifest)
        except OSError as exc:
            # Reported and *kept*: see the module docstring.  Empty content
            # takes the same path as a manifest with no header.
            logger.warning("skill %s: cannot read %s: %s", entry.name, MANIFEST, exc)
            content = ""
            error = str(exc)
        try:
            header = frontmatter(content)
        except SkillHeaderError as exc:
            # **Kept and refused.**  The folder says a skill is installed, so
            # hiding it would send a reader looking for a file the operator
            # believes is there; serving it as a headerless skill would drop
            # every declaration it wrote.  The error travels on the `Skill`, so
            # the catalogue row says `error` and `skill_use` refuses it.
            logger.warning("skill %s: %s: %s", entry.name, MANIFEST, exc)
            header = {}
            error = str(exc)
        found.append(
            Skill(
                name=str(header.get("name") or entry.name),
                description=str(header.get("description") or ""),
                directory=entry,
                requires=requirements(header),
                error=error,
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


def entry_for(skill: Skill, entries: Mapping[str, Any] | None) -> Any | None:
    """The `skills:` entry that supplies *skill*, or `None`.

    **A skill answers to two names and `skills:` may be keyed by either**, so
    "which entry is this one given" is a single question — and it had two
    answers.  The reader merged both entries with the directory winning per key,
    while the catalogue row and the switch check took the frontmatter name
    first; an operator who wrote both, with a different value under each, then
    read that the same skill's requirements were met in one place and unmet in
    another.  This is that question, asked once and answered the same way
    everywhere — `use`, `catalogue` and `_settings` all go through it.

    **The skill's own name wins when the two disagree**, exactly as it does in
    :func:`find`: `name:` is the skill's declaration about itself where the
    directory is only where somebody happened to put it, and the declaration is
    the more deliberate of the two.  It is also the name the rest of the family
    already treats as the skill's own — `skill_list` prints it, and
    `skills_server._key_for` files a new entry under it — so a `skills:` entry
    under the declared name is the operator answering the skill rather than the
    folder it landed in.

    `None` for an entry that is not there, and for *any* entry whose value is
    `None`; an entry that is present but empty is returned as it is, because an
    empty entry is a decision the operator wrote down and not an absence to
    second-guess.
    """
    if not entries:
        return None
    found = entries.get(skill.name)
    return found if found is not None else entries.get(skill.directory.name)


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

    missing_env = [name for name in needs.env if not _present(name, supplied)]
    missing_bins = [name for name in needs.bins if shutil.which(name) is None]
    missing = [
        *(f"${name}" for name in missing_env),
        *(f"`{name}` on PATH" for name in missing_bins),
    ]
    if not missing:
        return f"This skill's requirements are met ({_declared(needs)})."

    # **One sentence per kind of need, because the two have different fixes.**
    # A missing name and a missing program are not the same problem: `skills:`
    # carries `env:` and nothing that can put anything on `PATH`, so telling a
    # reader to supply a missing `ffmpeg` there sends it to a fix that cannot
    # work — and telling it to `credstore set` a key while the thing it lacks is
    # a binary is the same mistake from the other side.  Each sentence is
    # emitted only for the kind that is actually short, so the advice names the
    # thing the reader has to do and nothing it has already done.
    hints: list[str] = []
    if missing_env:
        # The publisher's `primaryEnv` is the name to point at when it is one of
        # the missing ones — the operator's `skills:` entry is expected to
        # reference exactly that name, so `credstore set` is the whole fix.
        # Otherwise the entry itself is what has to be written, which is what
        # the second sentence says; naming a *satisfied* primary here would send
        # the reader to a key it already has.
        if needs.primary in missing_env:
            hints.append(f"Set ${needs.primary} with `credstore set {needs.primary}`")
        else:
            hints.append("Supply it under `skills:` in slife2.yaml")
    if missing_bins:
        programs = ", ".join(f"`{name}`" for name in missing_bins)
        hints.append(f"Install {programs} so it is on PATH")
    return (
        f"**This skill needs {' and '.join(missing)}, which is not configured — "
        f"the commands below will fail until it is.** {' '.join(hints)}."
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
    """A skill's `SKILL.md`, preceded by the directory it lives in.

    The shape has a reason: a skill's body names its own files by relative path
    (`scripts/search.py`), and a model reading it has no way to know what those
    are relative *to*.  One line in front of the document turns a path it cannot
    use into one it can.

    **The base is the skill's own directory, not the `skills/` root.**  A
    playbook's files sit beside its `SKILL.md`, so `scripts/search.py` means
    `<skill directory>/scripts/search.py`; printing the root instead sent the
    model one level too high, to a path that does not exist.

    `note` rides in the same block rather than at the end, because a warning
    after a long playbook is a warning the reader has already stopped reading
    for.

    Raises:
        OSError: If the manifest cannot be read, or its header was refused
            when the folder was scanned — the caller's to report, because only
            the caller knows whether it is answering a model or a person.
    """
    if skill.error:
        raise OSError(skill.error)
    text = _read(skill.manifest)
    directory = skill.directory.resolve()
    preamble = [
        f"> **Skill directory:** `{directory}`",
        "> Every path this playbook names is relative to it.",
    ]
    if note:
        preamble.append(f"> {note}")
    return "\n".join([*preamble, "", text])


def _within(base: Path, name: str) -> Path:
    """`base / name`, refused unless it lands inside *base*.

    **A model writes these names, so this is the boundary.**  `skill_set` takes
    a list of `{path, content}` pairs and `skill_remove` takes a name, and in
    both the path becomes a filesystem path on the operator's machine: `..\\..`
    or an absolute path in one of them is how a playbook would rewrite the
    config that describes it, or the turn log beside it.

    Resolved *before* the comparison, because `scripts/../../..` is only outside
    once it has been resolved — a check on the string would pass it, and the
    resolution is what the filesystem will actually open.
    """
    root = base.resolve()
    target = (root / name).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"{name!r} is outside {base.name}/, and a skill is not")
    return target


def _location(name: str, root: Path | None = None) -> Path:
    """The one directory *name* installs into; every other spelling is refused.

    **The name is one directory directly under `skills/`, and it is that
    directory's own.**  Everything an install or an edit does is keyed on the
    name, and two spellings of it are two different things to every reader
    afterwards: `scan` lists the *direct* children of `skills/`, so a name
    carrying a separator installs where nothing looks, and a name that is not the
    directory it lands in (`one/`) answers about a skill `skill_use` cannot find
    — both of which reported success.  `a/./b` and `a/b` are one refusal, and
    the location decides it rather than the string.

    **The dot rule is about where a name lands, not about how it is spelled.**
    `_write` stages into `.{name}.installing` and moves the old copy to
    `.{name}.replacing`, so the leading dot is reserved for the swap's own
    directories — and `scan` refuses to list one so that a `SIGKILL` in that
    window cannot become a catalogue row for a name nobody installed.  A skill
    that lands in one would therefore install, report success and be unfindable:
    no row, no `skill_list` line, and `skill_use` answering that no such skill
    exists.  Checked on the resolved path, because `a/../.notes` and
    `sub/./../.notes` reach the same `.notes` a plain name does.

    **A refusal leaves nothing behind**, because it happens before anything is
    created, where the name is still the caller's to change.  `draft` asks this
    same question so a draft cannot hand `install` a name it will refuse.

    `root` is the folder the name lands in — `skills_dir()` in production, and a
    test's folder when one calls the module directly.
    """
    base = skills_dir() if root is None else root
    final = _within(base, name)
    if final == base.resolve():
        raise ValueError("a skill needs a name")
    inside = final.relative_to(base.resolve())
    if any(part.startswith(".") for part in inside.parts):
        raise ValueError(
            f"{name!r} installs into {inside.as_posix()!r}, and a dot-directory "
            f"is reserved for the staging an install swaps through — `scan` "
            f"never lists one, so this would be a skill nothing could find. Name "
            f"it without a dot."
        )
    if len(inside.parts) != 1 or inside.as_posix() != name:
        raise ValueError(
            f"{name!r} does not name one skill directory: a skill lives at "
            f"`skills/<name>/`, and `scan` lists only the direct children — so "
            f"this would install where nothing looks for it, or answer about a "
            f"name nothing matches. Name it as the one directory it is."
        )
    return final


def _write(
    name: str,
    files: Sequence[Mapping[str, Any]],
    *,
    base: Path | None = None,
) -> Path:
    """Write *files* as a skill and return where; *base* is what it starts from.

    **Whole or not at all, and never outside its own directory.**  The files are
    written into a staging directory beside the real one and the real one is
    swapped in afterwards, so a skill that was interrupted half-written never
    exists: a model reading a playbook that stops in the middle of a command is
    worse off than one told the skill is not installed.  A file whose path leaves
    the skill's directory is refused before anything is created.

    **`base` is the difference between creating and editing.**  `None` writes a
    new skill from exactly *files*; a directory copies that skill first, so an
    edit lands over the files it names and leaves every other one — the scripts
    beside the manifest are not silently deleted by a change to the prose.  The
    copy is staged and parsed before the swap for the same reason a create is:
    an edit that would leave a `SKILL.md` the loader refuses must not reach the
    live folder.

    **The manifest is parsed before the swap.**  `scan` will refuse the same
    header at load, and this gate is where the caller can still change it;
    writing first and failing later would leave a skill on disk that looks
    installed and cannot be used.  The parser is the one loader `scan` uses —
    one rule, one place (SDD Appendix A).
    """
    root = skills_dir()
    final = _location(name, root)
    manifest = next(
        (one for one in files if str(one.get("path") or "").strip() == MANIFEST),
        None,
    )
    if base is None:
        # **A create needs its manifest in the argument, before anything is
        # created**, because the folder's rule is that the manifest is what makes
        # a directory a skill (`scan`): writing three files and no `SKILL.md`
        # would be an install that reports success and installs nothing anything
        # can find.
        if manifest is None:
            raise ValueError(f"a skill is a directory with a {MANIFEST} in it")
        try:
            frontmatter(str(manifest.get("content") or ""))
        except SkillHeaderError as exc:
            raise ValueError(f"{MANIFEST}: {exc}") from exc
    # **The swap's own directories are named from the resolved location**, not
    # from the string the caller gave: the name has to be the directory it lands
    # in (the check above), and deriving them from `final.name` is what keeps
    # them beside the skill they belong to rather than nested under a spelling of
    # it — a stage built as `.{name}.installing` from `one/` was `skills/.one`,
    # and the `finally` below removed the stage while leaving that behind.
    staging = root / f".{final.name}.installing"
    previous = root / f".{final.name}.replacing"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        if base is not None:
            # **Symlinks are copied as links, not followed.**  A skill's folder
            # is the operator's (a copied-in playbook may have one pointing
            # somewhere else); resolving it here would copy the target into a
            # skill that then holds a document it never declared.
            shutil.copytree(base, staging, symlinks=True)
        for one in files:
            relative = str(one.get("path") or "").strip()
            if not relative:
                raise ValueError("every file needs a `path`")
            target = _within(staging, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(one.get("content") or ""), encoding="utf-8")
        if not (staging / MANIFEST).is_file():
            raise ValueError(f"a skill is a directory with a {MANIFEST} in it")
        if base is not None:
            # **The effective manifest is the staged one**, because an edit may
            # have replaced it or may have left the skill's damaged one in
            # place.  `_read` is the loader's own read, so a BOM or a non-UTF-8
            # file is judged exactly where `scan` judges it.
            try:
                frontmatter(_read(staging / MANIFEST))
            except OSError as exc:
                raise ValueError(f"{MANIFEST}: {exc}") from exc
            except SkillHeaderError as exc:
                raise ValueError(f"{MANIFEST}: {exc}") from exc
        # The swap: the live one moves aside, the new one takes its place, and a
        # failure puts the old one back.  `os.replace` cannot do this for a
        # non-empty directory — a rename onto one is refused rather than merged.
        shutil.rmtree(previous, ignore_errors=True)
        if final.exists():
            os.rename(final, previous)
        try:
            os.rename(staging, final)
        except OSError:
            if previous.exists() and not final.exists():
                os.rename(previous, final)
            raise
        shutil.rmtree(previous, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return final


def install(name: str, files: Sequence[Mapping[str, Any]]) -> Path:
    """Write a skill — its `SKILL.md` and anything beside it — and return where.

    The create half of `_write`: *files* are the whole skill, so anything the
    folder held before is gone after the swap.  `edit` is the other half — a
    change that keeps what it does not name.
    """
    return _write(name, files)


def edit(name: str, files: Sequence[Mapping[str, Any]]) -> Path:
    """Change part of an installed skill, keeping every file *files* does not name.

    The edit half of `_write`, and the reason it is a different function from
    `install`: replacement is right for a draft and wrong for a one-line fix — a
    model that read a playbook with `skill_use`, changed a step and installed it
    again would delete the scripts the playbook tells it to run, and nothing in
    the answer would say so.

    **A name that is not installed is refused**, because an edit that created a
    skill would be an install wearing the wrong verb: the answer would say a
    change happened where there was nothing to change.

    `SKILL.md` is not required in *files*, because an edit to a script is a real
    edit; the manifest the swap would leave behind is parsed either way, so a
    skill whose header is damaged cannot be half-edited into looking healthy.
    """
    skill = find(name)
    if skill is None:
        raise ValueError(
            f"{name!r} is not installed — `install` creates a skill and `edit` "
            f"changes one; see `scan`."
        )
    if not files:
        raise ValueError("an edit needs at least one file")
    return _write(skill.directory.name, files, base=skill.directory)


#: The host namespace a drafted playbook declares its needs under.  The reader
#: takes any namespace (`requirements`), so this is not a protocol name; it is
#: the honest one for a skill written by this system for this system.
_NAMESPACE = "slife2"


def _yaml_scalar(value: str) -> str:
    """A string as YAML reads it back unchanged.

    JSON is YAML's scalar syntax, so `json.dumps` is the quoting rule: a
    description with a colon, a quote or a newline is a plain string to the
    parser rather than a header that stops being a mapping.  `ensure_ascii`
    stays off so a Chinese description is the same text in the file a person
    opens.
    """
    return json.dumps(value, ensure_ascii=False)


def _requirement_names(raw: Any, key: str) -> list[str]:
    """One `requires` list: blanks dropped, the wrong shape refused.

    The shape is `_validate_header`'s — a string is one name and a list holds
    names — so a draft cannot render a declaration the load gate would refuse.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(one, str) for one in raw):
        raise ValueError(f"`requires.{key}` is not a string or a list of strings")
    return [one.strip() for one in raw if one.strip()]


def _declared_requirements(
    requires: Mapping[str, Any] | None,
) -> dict[str, list[str]]:
    """`requires` as the header's declaration, or `{}` for none.

    **A key this build does not read is refused, not dropped.**  A typo such as
    `bin` for `bins` would otherwise render a playbook that declares nothing
    about the program its steps run — the silence `Requirements` exists to
    remove, introduced by the tool that was supposed to prevent it.
    """
    if not requires:
        return {}
    if not isinstance(requires, Mapping):
        raise ValueError("`requires` is a mapping of `env` and `bins`")
    unknown = sorted(set(requires) - {"env", "bins"})
    if unknown:
        raise ValueError(
            f"`requires` has {unknown[0]!r}, which no reader knows — a playbook "
            f"declares `env` names and `bins` programs"
        )
    declared: dict[str, list[str]] = {}
    for key in ("env", "bins"):
        names = _requirement_names(requires.get(key), key)
        if names:
            declared[key] = names
    return declared


def _item(prefix: str, text: str, indent: str) -> list[str]:
    """One Markdown list item, with its continuation lines kept inside it.

    A step may be a command with a fenced block under it.  A continuation stays
    in the item only when it is indented to the marker's content, so every line
    after the first is indented — a step whose second line is a command is then
    still that step rather than a paragraph that ended the list.
    """
    first, *rest = text.splitlines()
    return [
        f"{prefix}{first}",
        *(f"{indent}{line}" if line else "" for line in rest),
    ]


def _render(
    name: str,
    description: str,
    steps: Sequence[str],
    notes: Sequence[str],
    requires: Mapping[str, Sequence[str]],
) -> str:
    """The `SKILL.md` a workflow becomes."""
    lines = [
        _FENCE,
        f"name: {_yaml_scalar(name)}",
        f"description: {_yaml_scalar(description)}",
    ]
    if requires:
        block = {key: list(value) for key, value in requires.items()}
        metadata = {_NAMESPACE: {"requires": block}}
        lines.append(f"metadata: {json.dumps(metadata, ensure_ascii=False)}")
    lines += [
        _FENCE,
        "",
        f"# {name}",
        "",
        description,
        "",
        "## Steps",
    ]
    for number, step in enumerate(steps, start=1):
        lines += _item(f"{number}. ", step, "   ")
    if notes:
        lines += ["", "## Notes"]
        for note in notes:
            lines += _item("- ", note, "  ")
    return "\n".join(lines).rstrip() + "\n"


def draft(
    name: str,
    description: str,
    steps: Sequence[str],
    notes: Sequence[str] = (),
    requires: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A workflow as a playbook: the `files` argument `install` takes.

    **The answer is a draft, and this writes nothing.**  The writer is `_write`,
    reached through `install`/`edit` and their tools; a second route to the
    folder would be a second place where the name's rules and the header's
    parser could disagree.  What this adds is the half that is not a file
    operation: the manifest a model would otherwise have to spell from memory —
    the fence, a YAML-safe description, the `requires` declaration — rendered
    from fields the tool asks for by name.

    **The fields are the ones a reader will need.**  `description` is what a
    search ranks and `skill_list` prints, so it is required: a playbook with
    none is one nothing looks for.  `steps` is the workflow itself.  `notes` is
    the half a procedure loses when it is written down — the order that is easy
    to get wrong, the check that says it worked.  `requires` is what the steps
    assume is already present, rendered into the header where `readiness` finds
    it.

    **The rendered text goes through the install gate's own parser**
    (`frontmatter`), so a draft is either a `SKILL.md` `install` would accept or
    a refusal here — never a file that looks installed and cannot be read.

    Raises:
        ValueError: If the name would install where nothing looks, the
            description or a step is empty, or `requires` is not a shape a
            header can declare.
    """
    name = name.strip()
    # The name's rules are `_location`'s, not a second copy: a draft that
    # accepted a name the install would refuse is a round trip wasted at the
    # second call.
    _location(name)
    description = description.strip()
    if not description:
        raise ValueError(
            "a skill needs a description — it is the line a search finds the "
            "playbook by, and a `SKILL.md` without one is one nothing looks for"
        )
    procedure = [str(step).strip() for step in steps]
    if not procedure:
        raise ValueError("a workflow needs at least one step")
    if any(not step for step in procedure):
        raise ValueError("every step must say something")
    remarks = [str(note).strip() for note in notes if str(note).strip()]
    declared = _declared_requirements(requires)
    text = _render(name, description, procedure, remarks, declared)
    # The install gate's parser, before the caller can hand this to `install`:
    # a renderer bug is then a failed draft, not a skill that scans as damaged.
    frontmatter(text)
    return {"name": name, "files": [{"path": MANIFEST, "content": text}]}


def remove(name: str) -> bool:
    """Delete one skill's directory; True if it was there.

    The whole directory, and not only its manifest: a skill's scripts live
    beside it (`SKILL.md`'s own paths are relative to it), so a removal that
    left them behind would leave a folder that is no longer a skill and files
    nobody can place.
    """
    root = skills_dir()
    target = _within(root, name)
    if target == root.resolve() or not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


async def use(
    name: str,
    *,
    environments: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[str, bool]:
    """`skill_use` — read one skill.  Returns `(text, ok)`; never raises.

    The body of the tool, here rather than in `slife2.plugins.skills.server` because
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
        names = [one.name for one in scan()]
        if not names:
            return f"no skill called {name!r}. (none installed)", False
        # **A typo and an absence are two answers**, and the reader needs to
        # know which one this is: a suggestion is the correction, an explicit
        # "nothing close" is the cue to look elsewhere.
        close = difflib.get_close_matches(name, names, n=3, cutoff=0.6)
        if close:
            return (
                f"no skill called {name!r} — did you mean "
                f"{', '.join(repr(one) for one in close)}? "
                f"Installed: {', '.join(names)}",
                False,
            )
        return (
            f"no skill called {name!r}, and nothing close is installed. "
            f"Installed: {', '.join(names)}",
            False,
        )

    # Which `skills:` entry supplies this skill is a single question with a
    # single answer (`entry_for`), shared with the catalogue row and the switch
    # check: asked three ways, one skill could be told its requirements were met
    # by one caller and unmet by another, which is the disagreement that function
    # exists to end.
    supplied = entry_for(skill, environments)

    try:
        return document(skill, readiness(skill, supplied)), True
    except OSError as exc:
        return f"the skill {name!r} is on disk but cannot be read: {exc}", False


__all__ = [
    "MANIFEST",
    "USE_TOOL",
    "Requirements",
    "Skill",
    "SkillHeaderError",
    "document",
    "draft",
    "edit",
    "entry_for",
    "find",
    "frontmatter",
    "install",
    "readiness",
    "remove",
    "requirements",
    "scan",
    "split_frontmatter",
    "unresolved",
    "use",
]
