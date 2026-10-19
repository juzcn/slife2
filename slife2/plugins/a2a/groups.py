"""Named groups on the mesh: what a member knows, and what a wire event means.

A group is a **roster with an owner**, not a third network: its members are
the same agent names the mesh already addresses, and it rides the same
``event`` topic category.  What is new is that a group event is *directed* —
published to ``event/{org}/{unit}/member:<name>``, one topic per recipient —
so a non-member never receives the group's traffic and a member that missed an
earlier control event still has a fixed subscription to learn from the next
message.

**The state is the member's own.**  Each member persists a ``groups.yaml``
under its directory (``agents/<agent>/groups.yaml`` or the subagent's), and
every group event it receives is applied to that copy.  There is no shared
group database: a member learns the roster from an owner's ``group_set`` or
from the first message that names it.  QoS 0 means a missed control is
ordinary, so the rules below are written to converge from messages, not to
assume every control arrived.

**A gid names the group instance.**  Without it a delayed event could
resurrect a group a member left or the owner disbanded, and reusing a name
would be ambiguous.  A tombstone remembers ``(name, gid)`` for exactly the
transition where this member stopped belonging; only an owner's ``group_set``
(the re-invitation) clears it.

**What each event may change.**  Owner traffic is authoritative for the
roster and the owner; a plain member's message can never change membership.
A sender that is not in the local roster is dropped — that is the guard
against a member removed by a roster replacement continuing to inject into a
group it no longer belongs to.

The module deliberately knows nothing about FastMCP, the mesh or the server:
the mesh turns bytes into payloads (``parse_event``), the server applies them
to a :class:`GroupStore` and renders the accepted messages.  The policy lives
here, in one place, because it is the part that is hard to get right.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from slife2.libs.host import statefile
from slife2.libs.host.paths import agent_dir
from slife2.plugins.a2a.inbound_store import iso_now

if TYPE_CHECKING:
    from slife2.libs.host.config import Net

logger = logging.getLogger(__name__)

#: The per-member file, under that member's own directory.
GROUPS_FILE = "groups.yaml"

#: The document's shape.  A version this build does not know is treated like
#: a corrupt file rather than guessed at.
SCHEMA_VERSION = 1

#: Wire kinds carried in an event payload's ``kind`` field.  ``group_message``
#: is also the marker's ``type``; the control kinds never reach a conversation.
GROUP_MESSAGE = "group_message"
KIND_SET = "group_set"
KIND_LEAVE = "group_leave"
KIND_DISBANDED = "group_disbanded"
GROUP_KINDS = (KIND_SET, KIND_LEAVE, KIND_DISBANDED)
WIRE_KINDS = (GROUP_MESSAGE, *GROUP_KINDS)

#: Bounds.  A group is a conversation among agents, not a directory: the cap
#: is far past any roster a person would read and keeps one fan-out bounded.
MAX_GROUP_NAME = 64
MAX_MEMBER_NAME = 128
MAX_MEMBERS = 64
MAX_TOMBSTONES = 256
MAX_GID = 64

#: Characters a topic segment cannot carry (the member topic embeds the name)
#: and a channel cannot carry (the channel is ``<group_channel>:<group>:<sender>``).
#: Group names additionally refuse ``:``; member names are allowed to have one
#: because the channel parser only splits the group off.
_TOPIC_FORBIDDEN = frozenset("/+#")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class GroupError(ValueError):
    """A group operation the caller asked for that cannot be done."""


def groups_path(agent: str, subagent: str = "") -> Path:
    """The file one member keeps its groups in: its own directory."""
    return agent_dir(agent, subagent) / GROUPS_FILE


def valid_member_name(name: object) -> bool:
    """Whether *name* can be an MQTT topic segment and a group's member.

    A member name is used verbatim in ``event("member:<name>")``, so ``/``,
    ``+``, ``#`` and NUL cannot appear.  Control characters are refused for the
    same reason every other wire-derived name is scrubbed: the name reaches a
    channel, a marker and a log.
    """
    return _valid_name(name, MAX_MEMBER_NAME, extra="")


def valid_group_name(name: object) -> bool:
    """Whether *name* can be a group name.

    ``:`` is additionally refused: a group channel is
    ``<group_channel>:<group>:<sender>``, and a colon in the group would make
    the reader's split ambiguous.  Group names are not topic segments, so the
    MQTT wildcard characters are refused only because they would be confusing
    in a name rather than because a broker would reject them.
    """
    return _valid_name(name, MAX_GROUP_NAME, extra=":")


def _valid_name(name: object, limit: int, *, extra: str) -> bool:
    if not isinstance(name, str):
        return False
    if not name or name != name.strip() or len(name) > limit:
        return False
    if _CONTROL_RE.search(name):
        return False
    if any(character in name for character in _TOPIC_FORBIDDEN):
        return False
    return not any(character in name for character in extra)


def clean_members(owner: str, members: Iterable[str]) -> tuple[str, ...]:
    """The roster a ``group_set`` asks for, validated and in caller order.

    The owner is a member whether or not it was named, and it is first: the
    roster's order is meaningful (it is how an owner's departure chooses the
    next owner), so the caller's order is kept for everyone else rather than
    sorted.
    """
    if not valid_member_name(owner):
        raise GroupError(f"{owner!r} is not a member name this network can address.")
    found: list[str] = [owner]
    seen = {owner}
    for raw in members:
        if not isinstance(raw, str):
            raise GroupError(f"{raw!r} is not a member name.")
        name = raw.strip()
        if not valid_member_name(name):
            raise GroupError(f"{name!r} is not a member name this network can address.")
        if name in seen:
            continue
        seen.add(name)
        found.append(name)
    if len(found) > MAX_MEMBERS:
        raise GroupError(
            f"a group holds at most {MAX_MEMBERS} members; this one names {len(found)}."
        )
    return tuple(found)


# ── The vocabulary a group message wears ───────────────────────────────


def group_marker(
    net: Net,
    sender: str,
    group: str,
    owner: str,
    members: Sequence[str],
) -> str:
    """The envelope a group message opens with, in *net*'s group vocabulary.

    ``[PeerAgentGroup:{"from": "alice", "group": "builders", "owner": "alice",
    "members": ["alice", "bob"], "type": "group_message"}] `` or the local
    network's ``[SubagentGroup:…]``.  **The roster and the owner travel with
    every message**: a reader that wants to answer, mention someone, or know
    whose group this is has the names in front of it, rather than having to ask
    `group_list` before it can use them.  The wire payload carries the same
    fields (`mesh.group_fanout`); this is the subset a conversation sees.

    ``from`` is the sender, the same key every other marker uses, and never
    ``agent_name``, which is the receiver's own identity.
    """
    payload = {
        "from": sender,
        "group": group,
        "owner": owner,
        "members": list(members),
        "type": GROUP_MESSAGE,
    }
    return f"{net.group_marker}{json.dumps(payload, ensure_ascii=False)}] "


def group_marker_payload(text: str, net: Net) -> dict[str, Any] | None:
    """The payload of a group marker, or ``None``.

    ``raw_decode`` rather than a search for the first ``]``: a group name is
    free text and may itself contain a bracket, which JSON escaping keeps
    exact and a naive scan would truncate.
    """
    if not text.startswith(net.group_marker):
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(text, len(net.group_marker))
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def group_channel_for(net: Net, group: str, sender: str) -> str:
    """The channel a group turn arrived on: ``<group_channel>:<group>:<sender>``."""
    return f"{net.group_channel}:{group}:{sender}"


# ── The event, and what it is allowed to change ─────────────────────────


@dataclass(frozen=True)
class Group:
    """One group as this member knows it."""

    name: str
    owner: str
    #: Roster order is meaningful: an owner's departure hands the group to
    #: the first remaining member in this order.
    members: tuple[str, ...]
    gid: str
    updated: str = ""

    def is_member(self, name: str) -> bool:
        return name in self.members


@dataclass(frozen=True)
class GroupEvent:
    """One validated group wire event."""

    kind: str
    sender: str
    group: str
    owner: str
    gid: str
    members: tuple[str, ...]
    text: str = ""


@dataclass(frozen=True)
class EventResult:
    """What applying one event means for its receiver.

    ``deliver`` is true only for an accepted ``group_message`` that belongs in
    the conversation.  ``changed`` is true when the persisted store must be
    rewritten.
    """

    deliver: bool = False
    changed: bool = False


def parse_event(data: Mapping[str, Any]) -> GroupEvent | None:
    """Validate one raw event payload, or answer ``None``.

    Every field that reaches a store, a channel or a marker is checked here,
    before anything else reads it.  The shape differences between the kinds
    are load-bearing: a ``group_set`` must come from the owner, a
    ``group_leave``'s sender is no longer in the roster, and a
    ``group_message`` must come from a member and carry text.
    """
    kind = data.get("kind")
    if kind not in WIRE_KINDS:
        return None
    sender = data.get("sender")
    group = data.get("group")
    owner = data.get("owner")
    gid = data.get("gid")
    raw_members = data.get("members")
    text = data.get("text", "")
    if not isinstance(sender, str) or not valid_member_name(sender):
        return None
    if not isinstance(group, str) or not valid_group_name(group):
        return None
    if not isinstance(owner, str) or not valid_member_name(owner):
        return None
    if not isinstance(gid, str) or not gid or len(gid) > MAX_GID:
        return None
    if not isinstance(raw_members, list) or not raw_members:
        return None
    if len(raw_members) > MAX_MEMBERS:
        return None
    members: list[str] = []
    for raw in raw_members:
        if not isinstance(raw, str) or not valid_member_name(raw):
            return None
        if raw not in members:
            members.append(raw)
    if owner not in members:
        return None
    if kind == GROUP_MESSAGE:
        if not isinstance(text, str) or not text:
            return None
        if sender not in members:
            return None
    elif kind == KIND_SET:
        if sender != owner:
            return None
    elif kind == KIND_LEAVE:
        if sender in members:
            return None
    elif kind == KIND_DISBANDED:
        if sender != owner:
            return None
        text = ""
    else:  # pragma: no cover - WIRE_KINDS is exhaustive above
        return None
    if not isinstance(text, str):
        return None
    return GroupEvent(
        kind=kind,
        sender=sender,
        group=group,
        owner=owner,
        gid=gid,
        members=tuple(members),
        text=text,
    )


def apply_event(store: GroupStore, event: GroupEvent, me: str) -> EventResult:
    """Apply one event to *store*, with *me* as the receiving member.

    The rules are in the module docstring; the shape here is one branch per
    kind, and each branch answers two questions: does this member still belong,
    and does the roster/owner need to change?
    """
    existing = store.groups.get(event.group)
    if event.kind == GROUP_MESSAGE:
        return _apply_message(store, existing, event, me)
    if event.kind == KIND_SET:
        return _apply_set(store, existing, event, me)
    if event.kind == KIND_LEAVE:
        return _apply_leave(store, existing, event, me)
    return _apply_disbanded(store, existing, event, me)


def _apply_message(
    store: GroupStore,
    existing: Group | None,
    event: GroupEvent,
    me: str,
) -> EventResult:
    if existing is None:
        if store.is_tombstoned(event.group, event.gid):
            return EventResult()
        if me not in event.members:
            return EventResult()
        store.learn(event)
        return EventResult(deliver=True, changed=True)

    if event.sender == event.owner:
        # The sender claims to be the owner.  That is authoritative only when
        # it is the owner we know, or when the owner we know has left the
        # roster this message carries (a transfer this receiver missed).
        if existing.owner != event.sender and existing.owner in event.members:
            return EventResult()
        if event.gid != existing.gid and existing.owner in event.members:
            return EventResult()
    else:
        # A plain member may speak, but may change nothing.
        if event.gid != existing.gid or event.owner != existing.owner:
            return EventResult()
        if event.sender not in existing.members:
            return EventResult()

    if me not in event.members:
        store.forget(existing)
        return EventResult(changed=True)

    changed = (
        event.gid != existing.gid
        or event.owner != existing.owner
        or event.members != existing.members
    )
    if changed:
        store.learn(event)
    return EventResult(deliver=True, changed=changed)


def _apply_set(
    store: GroupStore,
    existing: Group | None,
    event: GroupEvent,
    me: str,
) -> EventResult:
    if existing is None:
        if me not in event.members:
            return EventResult()
        # **A `group_set` is the one event that may clear a tombstone.**  The
        # sender is the owner (parse enforces it), so this is either a new
        # group or a re-invitation of a member who left; `learn` clears the
        # `(name, gid)` that a plain message is not allowed to bypass.
        store.learn(event)
        return EventResult(changed=True)

    if event.sender != existing.owner and existing.owner in event.members:
        # A member cannot seize a group while the owner it knows is still in
        # it.  The one exception is catch-up after a lost transfer: the owner
        # this receiver recorded is absent from the roster the sender names.
        return EventResult()

    if me not in event.members:
        store.forget(existing)
        return EventResult(changed=True)

    changed = (
        event.gid != existing.gid
        or event.owner != existing.owner
        or event.members != existing.members
    )
    if changed:
        store.learn(event)
    return EventResult(changed=changed)


def _apply_leave(
    store: GroupStore,
    existing: Group | None,
    event: GroupEvent,
    me: str,
) -> EventResult:
    if existing is None or event.gid != existing.gid:
        return EventResult()
    if event.sender not in existing.members:
        return EventResult()

    if event.sender == existing.owner:
        # The departing owner computed the successor; require it to name one.
        if event.owner == event.sender or event.owner not in event.members:
            return EventResult()
        owner = event.owner
    else:
        # A plain member's leave removes only that member.  The owner change
        # is accepted only as catch-up, when the recorded owner is gone from
        # the roster the event carries.
        if event.owner != existing.owner and existing.owner in event.members:
            return EventResult()
        owner = event.owner

    remaining = tuple(member for member in existing.members if member != event.sender)
    if not remaining:
        store.forget(existing)
        return EventResult(changed=True)
    if me not in remaining:
        store.forget(existing)
        return EventResult(changed=True)
    if owner not in remaining:
        # A roster that does not contain the owner it names is not one this
        # member can persist; a stale transfer is the ordinary cause.
        return EventResult()

    # The sender leaving always changes the roster; a transfer changes the
    # owner with it.  Both go through one write.
    store.set_roster(existing.name, owner, remaining)
    return EventResult(changed=True)


def _apply_disbanded(
    store: GroupStore,
    existing: Group | None,
    event: GroupEvent,
    me: str,
) -> EventResult:
    if existing is None or event.gid != existing.gid:
        return EventResult()
    if event.sender != existing.owner:
        return EventResult()
    store.forget(existing)
    return EventResult(changed=True)


# ── The file ────────────────────────────────────────────────────────────


@dataclass
class GroupStore:
    """One member's groups, and the file they are written to.

    ``path`` is ``None`` for the in-memory store a test or a directly built
    :class:`~slife2.plugins.a2a.server.Session` gets; :meth:`save` then does
    nothing.  The plugin's real session loads the member's file.
    """

    path: Path | None = None
    groups: dict[str, Group] = field(default_factory=dict)
    #: ``(name, gid)`` this member has left, been removed from, or seen
    #: disbanded.  Bounded insertion-ordered list; the oldest goes first.
    tombstones: list[tuple[str, str]] = field(default_factory=list)
    #: Why the file could not be read and could not be quarantined.  While
    #: this is set, mutations refuse: overwriting a file we could not
    #: understand is the one way to lose what it held.
    load_error: str = ""

    @classmethod
    def load(cls, path: Path | None) -> GroupStore:
        """Read *path*, or answer an empty store with the reason it is empty."""
        store = cls(path=path)
        if path is None or not path.is_file():
            return store
        try:
            raw = statefile.reader().load(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 - any unreadable file is one answer
            return store._quarantine(f"{type(e).__name__}: {e}")
        if not isinstance(raw, Mapping) or raw.get("version") != SCHEMA_VERSION:
            return store._quarantine("not a groups document this build understands")
        groups = raw.get("groups")
        if groups is None:
            groups = {}
        if not isinstance(groups, Mapping):
            return store._quarantine("the groups entry is not a mapping")
        for name, entry in groups.items():
            group = _group_from_yaml(name, entry)
            if group is None:
                logger.warning(
                    "a2a_group_entry_invalid path=%s group=%r — skipped", path, name
                )
                continue
            store.groups[group.name] = group
        store._load_tombstones(raw.get("tombstones"))
        return store

    def _quarantine(self, reason: str) -> GroupStore:
        """Keep the bytes of a file this build cannot read, and start empty."""
        assert self.path is not None
        logger.warning(
            "a2a_groups_unreadable path=%s reason=%s — starting empty",
            self.path,
            reason,
        )
        try:
            self.path.replace(self.path.with_name(self.path.name + ".corrupt"))
        except OSError as e:
            self.load_error = (
                f"{self.path} could not be read ({reason}) and could not be moved "
                f"aside ({e}); fix or remove it before changing groups."
            )
            logger.error("a2a_groups_unrecoverable path=%s: %s", self.path, e)
        return self

    def _load_tombstones(self, raw: Any) -> None:
        if raw is None:
            return
        if not isinstance(raw, list):
            logger.warning("a2a_groups_tombstones_invalid path=%s — ignored", self.path)
            return
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name")
            gid = entry.get("gid")
            if isinstance(name, str) and isinstance(gid, str) and name and gid:
                self.tombstones.append((name, gid))
        del self.tombstones[:-MAX_TOMBSTONES]

    def save(self) -> None:
        """Write the whole store, atomically.  A store with no path is memory."""
        if self.path is None:
            return
        if self.load_error:
            raise GroupError(self.load_error)
        statefile.write(self.path, self._payload())

    def _payload(self) -> dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "groups": {
                name: {
                    "gid": group.gid,
                    "owner": group.owner,
                    "members": list(group.members),
                    "updated": group.updated,
                }
                for name, group in sorted(self.groups.items())
            },
            "tombstones": [{"name": name, "gid": gid} for name, gid in self.tombstones],
        }

    def get(self, name: str) -> Group | None:
        return self.groups.get(name)

    def learn(self, event: GroupEvent) -> None:
        """Record the roster an event carries, clearing its tombstone."""
        self.groups[event.group] = Group(
            name=event.group,
            owner=event.owner,
            members=event.members,
            gid=event.gid,
            updated=iso_now(),
        )
        self.clear_tombstone(event.group, event.gid)

    def set_roster(self, name: str, owner: str, members: Iterable[str]) -> Group:
        """Replace a group's owner and roster, keeping its gid.

        The gid names the instance, not the roster: an owner's departure hands
        the group to a successor without making it a different group.
        """
        current = self.groups[name]
        self.groups[name] = Group(
            name=name,
            owner=owner,
            members=tuple(members),
            gid=current.gid,
            updated=iso_now(),
        )
        return self.groups[name]

    def forget(self, group: Group) -> None:
        """Drop a group this member no longer belongs to, and remember its gid."""
        self.groups.pop(group.name, None)
        self.add_tombstone(group.name, group.gid)

    def add_tombstone(self, name: str, gid: str) -> None:
        if (name, gid) in self.tombstones:
            return
        self.tombstones.append((name, gid))
        del self.tombstones[:-MAX_TOMBSTONES]

    def is_tombstoned(self, name: str, gid: str) -> bool:
        return (name, gid) in self.tombstones

    def clear_tombstone(self, name: str, gid: str) -> None:
        if (name, gid) in self.tombstones:
            self.tombstones.remove((name, gid))

    def new_gid(self) -> str:
        return uuid.uuid4().hex


def _group_from_yaml(name: object, entry: object) -> Group | None:
    """One stored group, or ``None`` when the entry is not usable."""
    if not isinstance(name, str) or not valid_group_name(name):
        return None
    if not isinstance(entry, Mapping):
        return None
    owner = entry.get("owner")
    gid = entry.get("gid")
    raw_members = entry.get("members")
    updated = entry.get("updated", "")
    if not isinstance(owner, str) or not valid_member_name(owner):
        return None
    if not isinstance(gid, str) or not gid or len(gid) > MAX_GID:
        return None
    if not isinstance(raw_members, list) or not raw_members:
        return None
    members: list[str] = []
    for raw in raw_members:
        if not isinstance(raw, str) or not valid_member_name(raw):
            return None
        if raw not in members:
            members.append(raw)
    if owner not in members or len(members) > MAX_MEMBERS:
        return None
    if not isinstance(updated, str):
        updated = ""
    return Group(
        name=name,
        owner=owner,
        members=tuple(members),
        gid=gid,
        updated=updated,
    )
