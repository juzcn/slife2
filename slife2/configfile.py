"""Writing `slife2.yaml` — the one place that edits the operator's file.

**A library, and not a plugin, by the test `DESIGN.md` §1.1 states**: it holds a
file and nothing else, and a file needs no process — "one writer is what a SQLite
lock already is", which is the same reason `slife2.db` is imported rather than
asked.  What imports it is the four plugins that own a section: `mcp:` by
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
`mcp:` is a key added to the mapping that is already there, and every comment
around it stays where the operator put it.

**A write is validated by the reader before it counts.**  After the swap, the
whole file goes back through `slife2.config.load`, and a file it refuses is
rolled back to what was there and the caller gets the error.  The alternative —
trusting the edit — fails one process later, at the next start, in a process
that has no idea an edit happened; and the entry that produced it (a `mcp:`
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
from typing import Any, cast

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError
from ruamel.yaml.tokens import CommentToken

import slife2.timeouts as _T
from slife2.config import Config, ConfigError, find_config_path, load
from slife2.runtime import config_key, exclusive

logger = logging.getLogger(__name__)


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
    # `rt` mode builds every mapping as a CommentedMap, so the shape check above
    # has proved the concrete type too — which a plain `isinstance(…, dict)`
    # widens back out of.
    return cast(CommentedMap, document)


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
    document = _read(target)
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
        return cast(CommentedMap, current)
    if not create:
        return None
    current = CommentedMap()
    document[section] = current
    return current


def _edit(
    path: Path,
    mutate: Callable[[CommentedMap], None],
    check: Callable[[Config], None] | None = None,
) -> None:
    """One locked, validated read-modify-write of the config file.

    Everything that writes goes through here, so the four properties a config
    edit needs — held across processes, comment-preserving, atomic, and refused
    by the loader if it produced nonsense — are stated once.

    `check` is the caller's own judge, run on the parsed config *inside the
    lock* and before the write counts.  `load` alone accepts things that are
    only wrong in the light of a section's meaning — a `default:` naming no
    model is a valid string — so a caller that knows its value passes a
    predicate here, and it must raise `ConfigError` to refuse, which rolls the
    file back exactly as the loader's own refusal does.
    """
    with exclusive(config_key(path), timeout=_T.timeouts.storage.filelock):
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
            config = load(path)
            if check is not None:
                check(config)
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
    """Write *entry* under *section* → *name*, replacing what is there.

    **Replace and not merge**, so a `set` says what the whole entry *is*: a
    field the caller leaves out is a field the entry does not have.  That is
    what makes switching a `mcp:` entry from `url` to `command` possible at
    all — a merge would keep the stale `url`, and the loader would then refuse
    the entry for naming two transports.  Flipping one field and leaving the
    rest alone is a *different* operation, and the one that has to read first:
    `set_enabled`.

    The entry's key order and comments are the caller's now — an entry written
    fresh is rendered in the order it was built.  What survives from the entry
    being replaced is its comment in the *section*, the explanation a person
    put above it, because that belongs to the section and not to the fields.

    `enabled: true` **removes** the key rather than writing it, because
    `enabled` is the default and the file's own convention is that only
    `enabled: false` is written down — see `slife2.config._tool_server`, which
    reads a missing key as true.
    """
    target = path or config_path()

    def mutate(document: CommentedMap) -> None:
        values = _without_empties(entry)
        if values.get("enabled") is True:
            values.pop("enabled")
        current = _section(document, section)
        if current is None:  # unreachable while `create=True`; kept honest
            return
        # A fresh mapping rather than an update of the one that is there: what
        # is written is exactly what was handed in, and nothing the caller did
        # not name survives from whatever the entry used to say.
        current[name] = CommentedMap(values)

    _edit(target, mutate)
    logger.info("config_upsert section=%s name=%s", section, name)


def _empty_section(document: CommentedMap, section: str) -> None:
    """Leave an emptied section in the file as `section: {}`, comment and all.

    **A section is not dropped when its last entry goes**, and getting that wrong
    is not cosmetic: `slife2.yaml` explains each section in a comment block above
    its key, so a `mcp:` that vanished would take a paragraph of the file's
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


def _tail_of(container: CommentedMap, name: str) -> tuple[Any, str]:
    """The `(mapping, key)` *trailing* slot that carries the block introducing
    the entry after `name`.

    **ruamel does not store "the comment above `k2`" on `k2`.**  It stores it as
    the comment that follows the last value of `k1` — or, when the commented
    entry is the mapping's first key, as the mapping's own pending comment
    (`ca.comment[1]`).  So a block a person reads as *introducing* an entry is,
    to the parser, trailing whatever came before it, and the slot it lives in is
    the deepest last position of that entry's value: a nested mapping's last
    key, not the mapping itself.  Everything below is the consequence of that
    one fact, which is not documented anywhere ruamel points a reader at.
    """
    holder: Any = container
    key = name
    node = container[name]
    while isinstance(node, dict) and node:
        inner = list(node)[-1]
        holder, key, node = node, inner, node[inner]
    return holder, key


def _take_intro(container: CommentedMap, name: str) -> Any:
    """Lift off the comment block that introduces the entry after `name`."""
    holder, key = _tail_of(container, name)
    slot = holder.ca.items.get(key)
    if slot is None or len(slot) < 3 or slot[2] is None:
        return None
    taken = slot[2]
    slot[2] = None
    return taken


def _put_intro(container: CommentedMap, name: str, block: Any) -> None:
    """Hang *block* off `name`, where the entry that follows it will read it."""
    holder, key = _tail_of(container, name)
    slot = holder.ca.items.setdefault(key, [None, None, None, None])
    while len(slot) < 4:
        slot.append(None)
    slot[2] = block


def _as_pending(block: Any) -> Any:
    """*block*, in the shape a mapping's pending slot holds.

    **The two slots do not hold the same shape**, and the difference is
    invisible until it is wrong.  What ruamel keeps *after* an entry's value
    begins with the newline and indentation that put the comment on its own line
    (`"\\n  # a\\n  # b\\n"`); what it keeps *above the first key* does not
    (`"# a\\n  # b\\n"`) — there the renderer supplies the first line's indent
    itself and writes every later line verbatim.  Moved across unchanged, a
    comment renders as a blank line or lands at column zero.
    """
    value = str(block.value)
    if value.startswith("\n"):
        value = value[1:]
        indent = len(value) - len(value.lstrip(" "))
        value = value[indent:]
    return CommentToken(value, block.start_mark, block.end_mark, block.column)


def _put_first(
    document: CommentedMap, section: str, current: CommentedMap, block: Any
) -> None:
    """Hang *block* above the first key of `section`, where ruamel reads it.

    **Two slots, and both have to be written.**  A comment above a mapping's
    first key is held twice — as the mapping's own pending comment
    (`ca.comment[1]`) and as the parent's entry for that key
    (`ca.items[section][3]`) — and the renderer prefers the parent's.
    `_empty_section` found the same slot from the other direction, clearing it
    so a deleted entry's comment would not print inside an emptied section; this
    one writes it.  Setting only one leaves the comment that was already there,
    which is the bug this is here to fix.
    """
    token = None if block is None else _as_pending(block)
    current.ca.comment = [None, [token] if token is not None else None]
    slot = document.ca.items.setdefault(section, [None, None, None, None])
    while len(slot) < 4:
        slot.append(None)
    slot[3] = [token] if token is not None else None


def _drop_entry(
    current: CommentedMap, document: CommentedMap, section: str, name: str
) -> None:
    """Delete `section → name`, and leave every *other* entry's comment on it.

    **The half of a removal that is about the file rather than the entry**, and
    the reason it is more than `del`.  Deleting a key from a `CommentedMap`
    moves two comment blocks, and the second move is wrong:

    * the block that *introduced* the entry goes with it, which is right;
    * the block that introduces the entry *after* it does too, because that is
      where ruamel was keeping it (see `_tail_of`) — so a comment describing the
      survivor is destroyed, and the deleted entry's own comment slides up over
      the survivor in its place.

    What is left is a comment about something that no longer exists, attached to
    the one entry it is least true of: the failure `_empty_section` names for a
    section's last entry, here for a middle one.  So the survivor's block is
    lifted off the doomed entry *first* and re-hung where its entry now begins —
    above the first key when the deleted entry was first, and otherwise off the
    entry that is now before it.
    """
    names = list(current)
    index = names.index(name)
    survivor = _take_intro(current, name)
    del current[name]
    if not current:
        _empty_section(document, section)
        return
    if index == 0:
        _put_first(document, section, current, survivor)
    else:
        _put_intro(current, names[index - 1], survivor)


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
        _drop_entry(current, document, section, name)
        found = True

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


def update_entry(
    section: str,
    name: str,
    change: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    *,
    path: Path | None = None,
) -> bool:
    """Read-modify-write one entry, inside the file's lock.

    **Why this exists beside `upsert`.**  A `providers:` entry holds a *list* of
    models, and `upsert` writes exactly what it is handed — so "write the new
    model" would delete every sibling.  Reading the entry and calling `upsert`
    would be a read-modify-write with the lock only on the write, which is the
    interleave the kernel lock exists to prevent and whose cost is silent: two
    callers add a model to one provider, one model is gone.

    `change` is handed the entry **as the file has it** — a plain dict, `{}` when
    the name is not there yet — and answers the mapping to write, or `None` to
    delete the entry (an emptied section is left as `section: {}` with its
    comment, the same as `remove`).  Returns whether the entry existed before.
    """
    target = path or config_path()
    existed = False

    def mutate(document: CommentedMap) -> None:
        nonlocal existed
        values = _section(document, section)
        if values is None:  # unreachable while `create=True`; kept honest
            return
        existed = name in values
        raw = values.get(name)
        current = dict(raw) if isinstance(raw, dict) else {}
        result = change(current)
        if result is None:
            if name in values:
                # Through `_drop_entry`, not `pop`: a plain deletion takes the
                # survivor's comment with it and leaves the deleted entry's own
                # comment above a stranger (`configfile._drop_entry`).
                _drop_entry(values, document, section, name)
        else:
            values[name] = CommentedMap(_without_empties(result))

    _edit(target, mutate)
    logger.info("config_update section=%s name=%s existed=%s", section, name, existed)
    return existed


def set_scalar(
    name: str,
    value: Any,
    *,
    check: Callable[[Config], None] | None = None,
    path: Path | None = None,
) -> None:
    """Write a top-level scalar — `name: value` — replacing what is there.

    **The one thing a section writer cannot do.**  `upsert` and the rest edit
    `section → name → mapping`, and `default:` is neither a section nor a
    mapping: it is the `provider/model` the agent starts a conversation on.  So
    this is the narrow writer for a top-level scalar, and the document edit is
    one assignment, which is what keeps every comment around it.

    `check` is the judge for a value `load` alone would accept — `default:` is a
    plain string to the loader and a reference only to `Config.resolve`, so a
    caller that knows its value passes `lambda c: c.resolve(ref)` and an edit
    naming no model is rolled back rather than left for the next conversation to
    trip over.
    """
    target = path or config_path()

    def mutate(document: CommentedMap) -> None:
        document[name] = value

    _edit(target, mutate, check=check)
    logger.info("config_set_scalar name=%s", name)


def read_scalar(name: str, *, default: str = "", path: Path | None = None) -> str:
    """A top-level scalar as the file has it, or *default* when it is absent.

    A read like `read_section`, and safe for the same reason: nothing here is
    resolved, and the one scalar anybody reads — `default:` — is a model
    reference rather than a secret.
    """
    target = path or config_path()
    value = _read(target).get(name)
    return default if value is None else str(value)


def _read(target: Path) -> dict[str, Any]:
    """The file, as plain data — *refused* rather than defaulted when unreadable.

    Both readers below the writers need exactly this, and each spelled it out:
    read the text, parse it safely, refuse a non-mapping.  One copy, because the
    three failure messages are what a person sees and three copies is how they
    come to disagree about which file they are describing.

    Safe and not the round-trip loader on purpose — nothing here is written
    back, and the round-trip document would carry comments into a value a tool
    returns over a wire.
    """
    text = target.read_text(encoding="utf-8")
    try:
        document = YAML(typ="safe").load(text) or {}
    except YAMLError as exc:
        raise ConfigError(f"{target} is not YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"{target} is not a mapping, so it has no sections")
    return document
