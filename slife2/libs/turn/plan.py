"""A conversation's plan: the model's own list of steps, and nothing else.

**The plan is not a turn and not a transcript.**  It is a small, current list a
long-horizon conversation keeps in front of itself: each item is one line of the
model's words and one of four statuses.  It lives beside the conversation rather
than in the turn log, because a rebuild chooses *turns* and a plan is not one —
the whole point is that a decision about what to keep cannot drop it.

**The model writes the whole list every time.**  There are no ids and no
add/complete verbs: a write is a snapshot, the last successful write is the
plan, and an empty list clears it.  That is what makes the file, the prompt
marker and the window's strip agree without a reducer, and what keeps the
harness from deciding what any status means.

**The harness stores, replays and draws; it never edits.**  :func:`write`
validates the shape (four statuses, a cap) and writes atomically, :func:`read`
answers what the file says, the agent server puts the list into
``[TURN-PROMPT:…]`` before every turn so a rebuild cannot lose it, and the window
draws it from the tool-call events and the opening read.  A status is the
model's claim, not a measurement: the harness has no way to know which item a
tool call belongs to, and must not invent one.

**A missing file is no plan.**  A file that exists and cannot be read is a
:class:`PlanError` naming the path — a plan the conversation silently lost is
worse than a turn that stops and says where to look.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from slife2.libs.host import statefile
from slife2.libs.host.paths import agent_path

#: The tool the model writes its plan with.  Spelled here because the agent
#: server serves it and the window recognises the call by it.
PLAN_WRITE = "plan_write"

#: The file, in the conversation's own directory.  A fixed name beside
#: `subagent.yaml` and `turns.db`, so a person can open it and a removal takes it.
PLAN_FILE = "plan.yaml"

#: The four states of one item.  **The model's words, not the harness's** — the
#: tool panel's `running`/`done`/`error` are observations, and reusing one of
#: them here would make a claim look like a measurement.
PENDING = "pending"
ACTIVE = "active"
DONE = "done"
BLOCKED = "blocked"
STATUSES: tuple[str, ...] = (PENDING, ACTIVE, DONE, BLOCKED)

#: How large a plan may be.  The list is replayed into the prompt at the start
#: of every turn, so this is a token budget as much as a shape: a plan that does
#: not fit one screen is not a plan for the work in front of the model.
MAX_ITEMS = 24
MAX_TEXT = 200

#: One item.  A plain mapping rather than a model class because the same shape
#: crosses the tool schema, the YAML file and the watch frame.
PlanItem = dict[str, str]


class PlanError(ValueError):
    """A plan this build will not store or read, naming what is wrong."""


def from_wire(raw: Any, *, where: str = "the plan") -> list[PlanItem]:
    """*raw* as the plan this build accepts, or a `PlanError` saying why not.

    **Structure only.**  Four statuses, one line of text each, the cap — the
    things a reader cannot repair for the model.  Whether an item is *really*
    done, or whether the model forgot to mark one, is not this function's to
    judge; that is why there are no transition rules here.

    `where` is the reader's own name for what it read — the tool says "the
    plan", a file says its path — so a refusal points at the thing to look at.
    """
    if not isinstance(raw, list):
        raise PlanError(f"{where}: the plan must be a list of items")
    if len(raw) > MAX_ITEMS:
        raise PlanError(
            f"{where}: {len(raw)} items is over the limit of {MAX_ITEMS}; keep "
            "the plan to the work in front of you"
        )
    items: list[PlanItem] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise PlanError(f"{where}: item {index} is not an object")
        # A list rather than a set: a hand-edited file can carry a non-string
        # key, and sorting mixed types would raise something other than the
        # `PlanError` this function promises.
        extra = [name for name in entry if name not in ("text", "status")]
        if extra:
            names = ", ".join(repr(name) for name in extra)
            raise PlanError(
                f"{where}: item {index} has {names}, which is not part of an "
                f"item — an item is `text` and `status`"
            )
        text = entry.get("text")
        status = entry.get("status")
        if not isinstance(text, str) or not text.strip():
            raise PlanError(f"{where}: item {index} has no `text`")
        if "\n" in text or "\r" in text:
            raise PlanError(
                f"{where}: item {index}'s text has a line break; an item is one line"
            )
        if len(text) > MAX_TEXT:
            raise PlanError(
                f"{where}: item {index}'s text is {len(text)} characters; the "
                f"limit is {MAX_TEXT}"
            )
        if status not in STATUSES:
            raise PlanError(
                f"{where}: item {index}'s status is {status!r}; it must be one of "
                f"{', '.join(STATUSES)}"
            )
        items.append({"text": text, "status": status})
    return items


def plan_path(agent: str, subagent: str = "") -> Path:
    """Where one conversation's plan lives — **without making the directory**.

    The conversation's own directory, for `agent_path`'s reason: a read must not
    create the thing it is asking about.
    """
    return agent_path(agent, subagent) / PLAN_FILE


def read(agent: str, subagent: str = "") -> list[PlanItem]:
    """The conversation's plan, or `[]` when it has none.

    **Only "no file" is no plan.**  A file that exists and cannot be parsed, or
    one whose `items` is not a plan, raises: reading damage as an empty plan is
    the silent loss this module exists to prevent (SDD Appendix C).
    """
    path = plan_path(agent, subagent)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise PlanError(f"{path}: {exc}") from exc
    try:
        document = statefile.reader().load(text)
    except Exception as exc:  # noqa: BLE001 - the file's own failure, named below
        raise PlanError(f"{path}: not a plan file ({exc})") from exc
    if not isinstance(document, Mapping) or "items" not in document:
        raise PlanError(f"{path}: not a plan file (no `items`)")
    return from_wire(document["items"], where=str(path))


def write(agent: str, subagent: str, items: Any) -> list[PlanItem]:
    """Replace a conversation's plan, atomically, and answer what was written.

    **Validation is before the write**, so a refused plan leaves the previous
    one exactly as it was — the file is the state, and a failed tool call must
    not change it.  `statefile.write` renames, so a process that dies mid-write
    leaves the old plan or the new one and never half of either.
    """
    validated = from_wire(items)
    statefile.write(plan_path(agent, subagent), {"items": validated})
    return validated


def clear(agent: str, subagent: str = "") -> bool:
    """Remove a conversation's plan; True when there was one.

    **Only "not there" is a no-op.**  A file that exists and cannot be removed
    raises, because a `reset` that silently left the plan behind would be a
    reset the next turn could resume.
    """
    try:
        plan_path(agent, subagent).unlink()
    except FileNotFoundError:
        return False
    return True
