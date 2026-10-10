"""Writing `slife2.yaml` — the one place that edits the operator's file.

**A library, and not a plugin, by the test `DESIGN.md` §1.1 states**: it holds a
file and nothing else, and a file needs no process — "one writer is what a SQLite
lock already is", which is the same reason `slife2.db` is imported rather than
asked.  What imports it is the four plugins that own a section: `tools:` by
`slife2-mcp-tools`, `rest-api:` by `slife2-restapi-tools`, `cli:` by
`slife2-cli`, `skills:` by `slife2-skills`.  Each one edits the section it
already reads, and none of them knows anything about the others' — which is why
this module takes a *section name* rather than knowing what a tool server is.
The validating is `slife2.config`'s (`_tool_server`, `_cli_tool`), and it stays
there: a library that knew the sections would be a second place that knows the
config's shape.

**The comment preservation is not decoration.**  `pyproject.toml` claims ruamel
for exactly this — "so a rewrite does not eat the file's explanation of itself"
— and until this module existed that claim had never been tested, because
nothing in slife2 had ever written the file: `slife2.config` opens it with
`YAML(typ="safe")`, which drops comments on the floor.  So this one loads the
document in round-trip mode and edits it *as a document*: a key added under
`tools:` is a key added to the mapping that is already there, and every comment
around it stays where the operator put it.

**A write is validated by the reader before it counts.**  After the swap, the
whole file goes back through `slife2.config.load`, and a file it refuses is
rolled back to what was there and the caller gets the error.  The alternative —
trusting the edit — fails one process later, at the next start, in a process
that has no idea an edit happened; and the entry that produced it (a `tools:`
entry with neither `command` nor `url`, say) would be one the tool reported as
written.

**The lock is the kernel's**, from `slife2.runtime.exclusive`: the four plugins
are four processes and two of them can write at once, so a read-modify-write
window has to be held across processes.  Nothing hand-rolled here decides
whether a lock is stale — see `exclusive`'s own docstring for why that matters.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from slife2.config import ConfigError, find_config_path, load
from slife2.runtime import config_key, exclusive

logger = logging.getLogger(__name__)

#: How long a section edit waits for another process's edit to land.  A config
#: write is a lock, a read, a rewrite and a validating parse — milliseconds —
#: so anything near this bound means a writer that died holding the lock, and
#: `exclusive` releases that in the kernel rather than after a timeout.
WRITE_TIMEOUT_SECONDS = 30.0


def _yaml() -> YAML:
    """The round-trip loader, set up to edit `slife2.yaml` without reformatting it.

    `typ="rt"` is what keeps comments, quote styles and key order.  Each of the
    four settings below undoes a ruamel default that is wrong for this file, and
    v1's `_yaml_doc.new_yaml` is where they were arrived at — measured against
    its own configs, which are 65–77% comments and just as Chinese:

    - **preserve_quotes** — the file's own quote style survives a rewrite.
    - **allow_unicode** — without it non-ASCII is dumped as `\\uXXXX`, so every
      Chinese description in `slife2.yaml` would change character on the first
      write that touched anything near it.
    - **width** — ruamel wraps at 80 columns and will break a long scalar inside
      the string.  The value survives; the file churns on every write.
    - **indent** — the style the file already uses: two-space mappings, sequence
      dashes indented two under their own key.  This is also what a *new* key is
      emitted with, which is why it has to agree with the file rather than with
      ruamel.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.allow_unicode = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _document(text: str, path: Path) -> CommentedMap:
    """Parse the file into an editable document.

    A mapping is required and nothing else is tolerated: everything below is
    "reach into this section by name", and a file that is a list, or empty, has
    no section to reach into — said here rather than at the first `KeyError`.
    """
    if not text.strip():
        return CommentedMap()
    try:
        document = _yaml().load(text)
    except YAMLError as exc:
        raise ConfigError(f"{path} is not YAML: {exc}") from exc
    if document is None:
        return CommentedMap()
    if not isinstance(document, dict):
        raise ConfigError(f"{path} is not a mapping, so it has no sections")
    return document


def _render(document: CommentedMap) -> str:
    stream = StringIO()
    _yaml().dump(document, stream)
    return stream.getvalue()


def _swap(path: Path, text: str) -> None:
    """Put *text* at *path*, atomically, keeping the file's mode.

    A temp file in the same directory and `os.replace`, so a reader sees the old
    file or the new one and never a half-written one.  The mode is copied from
    the file being replaced because `mkstemp` creates `0600`, which would
    silently tighten a config somebody had made group-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            try:
                os.chmod(tmp, path.stat().st_mode & 0o7777)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_section(
    section: str, *, path: Path | None = None
) -> dict[str, dict[str, Any]]:
    """One section as the *file* has it, as plain Python, secrets unresolved.

    **The listing tools read this rather than the settings they hold**, and the
    difference is a leak: `ToolServerSettings.env` has been through
    `resolve_secret`, so a server configured with `SERPER_API_KEY: ${SERPER_API_KEY}`
    is held with the live key in it — and an answer built from that would print
    the operator's credential into the conversation and the transcript.  What a
    person wrote is `${SERPER_API_KEY}`, and that is what a listing shows.

    Plain Python and not a round-trip document because this is a *read*: nothing
    here is written back, and a caller returning it from a tool needs something
    FastMCP can serialise.
    """
    target = path or config_path()
    text = target.read_text(encoding="utf-8")
    try:
        document = YAML(typ="safe").load(text) or {}
    except YAMLError as exc:
        raise ConfigError(f"{target} is not YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"{target} is not a mapping, so it has no sections")
    values = document.get(section)
    if not isinstance(values, dict):
        return {}
    return {
        str(name): dict(entry)
        for name, entry in values.items()
        if isinstance(entry, dict)
    }


def config_path() -> Path:
    """`slife2.yaml`, or the reason there is nothing to edit.

    **A missing file is refused rather than created**, and that is the whole of
    what this function is for.  slife2 runs with no config at all — it falls back
    to `slife2.config.default_config`, one built-in OpenAI-compatible provider —
    so writing a fresh file holding only the section being edited would replace
    that fallback with a file that has no providers in it.  The next start would
    then have no models, over an edit that reported success.
    """
    found = find_config_path()
    if found is None:
        raise ConfigError(
            "there is no slife2.yaml to write to — slife2 is running on its "
            "built-in default config, and a section written into a new file "
            "would take the place of the whole of it. Create the file first."
        )
    return found


def _section(
    document: CommentedMap, section: str, *, create: bool = True
) -> CommentedMap | None:
    """The top-level mapping *section*, created when it is missing.

    A malformed value — the operator wrote a string where a mapping goes — is
    replaced by an empty one rather than raising, because every caller has
    already decided to write here and the alternative is a traceback out of a
    tool call.  That the value was malformed is the reader's to complain about,
    and it does: `slife2.config` refuses a section that is not a mapping.
    """
    current = document.get(section)
    if isinstance(current, dict):
        return current
    if not create:
        return None
    current = CommentedMap()
    document[section] = current
    return current


def _edit(path: Path, mutate: Callable[[CommentedMap], None]) -> None:
    """One locked, validated read-modify-write of the config file.

    Everything that writes goes through here, so the four properties a config
    edit needs — held across processes, comment-preserving, atomic, and refused
    by the loader if it produced nonsense — are stated once.
    """
    with exclusive(config_key(path), timeout=WRITE_TIMEOUT_SECONDS):
        existed = path.exists()
        before = path.read_text(encoding="utf-8") if existed else ""
        document = _document(before, path)
        mutate(document)
        text = _render(document)
        if text == before:
            # An edit that found nothing to change — a `remove` of an entry that
            # was not there — is not a write.  Rewriting the same bytes would
            # only move the file's mtime and make a reader think something
            # happened.
            return
        _swap(path, text)
        try:
            # The reader is the judge: an entry this refuses is one the next
            # `slife2` would fail to start on, so it must not be left on disk.
            load(path)
        except ConfigError as exc:
            if existed:
                _swap(path, before)
            else:
                path.unlink(missing_ok=True)
            raise ConfigError(f"refused, so nothing was written: {exc}") from exc


def _without_empties(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the fields that say nothing.

    `None` (the caller did not supply it) and the empty string, list and mapping
    are four spellings of "no value", and an entry that keeps them is noise in a
    file people read and hand-edit — `url: ''` also reads as a claim that there
    *is* a URL.  What survives is what the entry actually says.
    """
    return {
        key: value
        for key, value in entry.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def upsert(
    section: str, name: str, entry: Mapping[str, Any], *, path: Path | None = None
) -> None:
    """Write *entry* under *section* → *name*, merging over what is there.

    **Merge and not replace**, which is what makes this usable as an "update":
    a caller passes the fields it is setting and leaves the rest out, and the
    ones it left out keep their value.  `remove` is the way to drop a field.

    An entry that already exists keeps its key order and its comments; only the
    fields handed in move.  `enabled: true` **removes** the key rather than
    writing it, because `enabled` is the default and the file's own convention
    is that only `enabled: false` is written down — see
    `slife2.config._tool_server`, which reads a missing key as true.
    """
    target = path or config_path()

    def mutate(document: CommentedMap) -> None:
        values = _without_empties(entry)
        if values.get("enabled") is True:
            values.pop("enabled")
        current = _section(document, section)
        if current is None:  # unreachable while `create=True`; kept honest
            return
        existing = current.get(name)
        merged = (
            CommentedMap(existing) if isinstance(existing, dict) else CommentedMap()
        )
        merged.update(values)
        current[name] = merged

    _edit(target, mutate)
    logger.info("config_upsert section=%s name=%s", section, name)


def _empty_section(document: CommentedMap, section: str) -> None:
    """Leave an emptied section in the file as `section: {}`, comment and all.

    **A section is not dropped when its last entry goes**, and getting that wrong
    is not cosmetic: `slife2.yaml` explains each section in a comment block above
    its key, so a `tools:` that vanished would take a paragraph of the file's
    documentation with it and put the section back at the bottom of the file the
    next time somebody added an entry.

    Two things have to happen, and both are ruamel's doing rather than ours:

    * **The emptied mapping is replaced, not left hollowed.**  Deleting the last
      key of a block mapping leaves a `CommentedMap` with no keys and a comment
      still attached to it, which ruamel renders as the key, that comment, then
      `{}` at column zero — invalid YAML, so the write was refused and rolled
      back by `_edit`'s own check, every time a section's last entry went and
      that section had a comment inside it.
    * **The deleted entry's comment is cleared off the section's key.**  ruamel
      does not drop it with the entry: it re-homes it onto the enclosing map's
      entry for *that key*, in the slot that renders after the value.  Left
      there it would print inside the empty section, under a key that has no
      entries — a comment describing something that no longer exists, which is
      the one thing a comment must not do.

    What survives is exactly what should: the section's own explanation, still
    attached to the section, and nothing about the entry that went.
    """
    document[section] = CommentedMap()
    comments = document.ca.items.get(section)
    if comments and len(comments) > 3:
        comments[3] = None


def remove(section: str, name: str, *, path: Path | None = None) -> bool:
    """Delete *section* → *name*; True if it was there.

    Nothing is written when it was not — see `_edit`, which does not touch a file
    it would only rewrite byte for byte — so "remove something that is not there"
    costs a read and leaves the file's mtime alone.
    """
    target = path or config_path()
    found = False

    def mutate(document: CommentedMap) -> None:
        nonlocal found
        current = _section(document, section, create=False)
        if current is None or name not in current:
            return
        del current[name]
        found = True
        if not current:
            _empty_section(document, section)

    _edit(target, mutate)
    logger.info("config_remove section=%s name=%s found=%s", section, name, found)
    return found


def set_enabled(
    section: str, name: str, enabled: bool, *, path: Path | None = None
) -> bool:
    """Write the `enabled` switch for *section* → *name*; True if it was there.

    `False` writes the key; `True` removes it, for the reason `upsert` gives.
    """
    target = path or config_path()
    found = False

    def mutate(document: CommentedMap) -> None:
        nonlocal found
        current = _section(document, section, create=False)
        if current is None or name not in current:
            return
        entry = current[name]
        if not isinstance(entry, dict):
            entry = CommentedMap()
            current[name] = entry
        if enabled:
            entry.pop("enabled", None)
        else:
            entry["enabled"] = False
        found = True

    _edit(target, mutate)
    logger.info(
        "config_set_enabled section=%s name=%s enabled=%s", section, name, enabled
    )
    return found
