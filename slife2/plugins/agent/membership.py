"""Subagent membership: making, listing and unmaking an agent's subagents.

A subagent is a directory with a `subagent.yaml` in it, and that record is the
whole of what it is: its own conversation, its own store and its own place on
the subagent network.  This module owns the two verbs that make and unmake one
(SDD §3.5) and the read a window's conversation switcher offers; the
conversation a subagent runs is the core's (`AgentCore`), and the removal claim
it takes is the core's loops table's — a name being deleted must not start a
conversation, and that refusal lives on the path that creates one rather than
in each tool that might.

**The removal claim is one invariant, taken here and refused by the table.**
`Conversations.removal` marks the name synchronously, before the first await;
`AgentCore.conversation` and `reset` ask the same table.  Every race the old
spelling had to remember — a message arriving during the network notification,
a reset landing between the record check and the store clear — is one check in
one place (`Conversations`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.helpers.clock import now
from slife2.libs.host import statefile
from slife2.libs.host.config import LOCAL, ConfigError
from slife2.libs.host.paths import (
    SUBAGENT_RECORD,
    agent_dir,
    subagent_dirs,
    subagent_file,
    subagent_path,
)
from slife2.libs.host.subagents import (
    DEFAULT_ROLE,
    SubagentSpec,
    read_subagent,
    subagent_spec,
)
from slife2.libs.plugin.audience import FOR_THE_MODEL, conversation_key
from slife2.libs.plugin.protocol import (
    close_server,
    conversation_reader,
    describe,
    open_server,
)
from slife2.libs.plugin.vocabulary import FORGET

from .core import SUBAGENT_CREATE, SUBAGENT_REMOVE, AgentCore, opening_messages

logger = logging.getLogger(__name__)


async def leave_the_mesh(
    core: AgentCore, agent: str, subagent: str, *, client: Client | None = None
) -> None:
    """Tell the subagent network that this member is gone.

    **A removal is one directory and three processes hold something of it.**
    This server holds the conversation (`loops`), the context store holds the
    list it would restore from, and the subagent network holds the member's
    *place*: an MQTT connection subscribed to its topic, answering whoever
    sends to it, with a card on the broker saying it is here.  A directory
    that has been deleted cannot unsubscribe any of that, so the network is
    told — the same act the other two get, in the one vocabulary this system
    has for it (`forget`, which is what `slife2-context`'s tool of the same
    name means and what `slife2.plugins.a2a.server`'s does).

    **Called before anything is taken away, and that is the order.**  A peer
    that is not there is a broken system rather than a degraded one
    (`slife2.libs.plugin.protocol.open_server`), this one is dialled on every
    removal, and a notification that cannot be made must leave the subagent
    exactly as it was — which it does, because by then nothing has been
    deleted.  A machine that configures that section has its plugin running
    (`slife2.libs.host.launcher` starts every one of them), so the only way
    this cannot be reached is the whole stack being wrong.

    A machine with no `subagent:` section has no such network and nothing to
    tell: `configured` is that section's presence, and it is the one case this
    returns without a call.

    `client` is the seam for a test — a recorder answers what the plugin would
    — and an injected client is used as it is and left open.

    **The call's patience is `work.management`**, the registry's number for a
    management call answered out of a plugin's memory: this one reads a table
    and publishes an offline card, and it must not be the call that keeps a
    removal waiting.
    """
    if not core.current().subagent.network.configured:
        return
    opened = client is None
    try:
        peer = client or await open_server(
            core.current().server(LOCAL.key).url,
            name=LOCAL.server_name,
            fallback_tool=FORGET,
            timeout=None,
        )
    except Exception as exc:  # noqa: BLE001 - every way this peer can be missing means the same thing here, and the caller is a removal that has not started
        raise ToolError(
            f"{describe((agent, subagent))} was not removed: the subagent "
            f"network could not be reached, so its place on it could not be "
            f"given up ({exc}).  Nothing has been deleted."
        ) from exc
    try:
        await peer.call_tool(
            FORGET,
            {"agent": agent, "subagent": subagent},
            timeout=_T.timeouts.work.management,
        )
    finally:
        if opened:
            # `shield`, for `slife2.libs.harness.handover._peer`'s reason: a
            # cancellation arriving while the close is awaiting would pass
            # straight through it and leave the connection open.
            await asyncio.shield(close_server(peer))


def register_tools(
    mcp: FastMCP, core: AgentCore, *, mesh_client: Client | None = None
) -> None:
    """Serve the membership surface: create, remove, and the list a window reads."""

    maker_of = conversation_reader("agent to make a subagent for", "a subagent")
    remover_of = conversation_reader("agent to remove a subagent of", "a subagent")

    @mcp.tool
    async def subagents(agent: str) -> dict[str, Any]:
        """The subagents an agent has made, for a screen that offers them.

        **The filesystem is the list**, by the rule the subagent directory
        states: what makes a directory a subagent's is its `subagent.yaml`, and
        nothing keeps a registry to be kept in step with a directory that is
        deleted without notice (`slife2.libs.host.paths.subagent_dirs`).  A watch
        list that asked the *network* instead would be a different question:
        `subagent_list` is the model's roster, it connects and wakes members to
        answer, and a screen that merely offers names must not do either.

        A damaged record reads as the defaults, as it does for every read path
        (`read_subagent`): losing a role to a half-written file is smaller than
        refusing to list the conversation that exists.  A conversation that
        would run on the answer is the other case and is strict — it refuses
        rather than silently running the subagent as the maker's.

        Args:
            agent: Whose subagents.

        Returns:
            `subagents`: one entry per directory, sorted by name, each with
                `name`, `model` (empty for the maker's own) and `role`.
                Empty is the honest answer for an agent that has made none.
        """
        found: list[dict[str, Any]] = []
        for directory in sorted(subagent_dirs(agent), key=lambda one: one.name):
            spec = subagent_spec(agent, directory.name)
            found.append(
                {
                    "name": directory.name,
                    "model": spec.model,
                    "role": spec.role,
                }
            )
        return {"subagents": found}

    @mcp.tool(name=SUBAGENT_CREATE, meta=FOR_THE_MODEL)
    async def subagent_create(
        ctx: Context,
        name: str,
        model: str = "",
        role: str = DEFAULT_ROLE,
    ) -> dict[str, Any]:
        """Make a subagent: an agent of your own, named, with its own memory.

        A subagent is an agent that belongs to you — the same tools, the same way
        of working, its own conversation and its own memory. It has no window
        and no process: it runs when something sends it a message, and from then
        on you address it by name. It is *made* here and *unmade* by
        `subagent_remove`; everything else it does, it does with the tools you
        have.

        **A subagent you already have is the same subagent** — naming one twice
        makes no second one, so it is safe. What a second call changes is its
        model and its role.

        Args:
            name: What to call it. A name is one subagent, so it is also the
                address you send to.
            model: Which model it runs on, as `provider/model`.  Empty — the
                default — is *your* model, read when the subagent first runs
                rather than now, so a `model_switch` you make in between reaches
                it the way it reaches your own next turn.
            role: What it is for, as a word: it goes into the prompt the subagent
                starts with, which opens `You are <name>, one of <agent>'s
                subagents — your role is <role>`.  The default is the plain
                one — `subagent` — and anything else is worth saying only when
                it changes what the agent would do, the way "code reviewer"
                does.

        Returns:
            `name`, `model`, `role`, and `created`: whether this call was the
            one that made it.
        """
        maker, mine = maker_of(ctx)
        if mine:
            # The tree, and it stops at one level — not because a second level is
            # unthinkable, but because nothing would hold a place for it: the
            # local network's membership is the maker's *own* subagents
            # (`subagent_dirs`, under the agent's own `subagents/`), and a place is
            # what makes a member
            # reachable at all (`slife2.plugins.a2a.server.place_for`).  SDD §4.5.3
            # holds the rule this depends on.
            raise ToolError(
                "a subagent cannot make a subagent: the local network holds a "
                "place for one level in — the subagents of the agent you belong "
                "to — so a subagent of yours would have nothing listening for "
                "it. Ask your own maker for what you need."
            )
        directory = subagent_path(maker, name)
        key = conversation_key(maker, directory.name)
        # **A name being removed is not a name to make.**  The check is before
        # the first filesystem read and there is no await between it and the
        # record write below, so a removal cannot claim the name in between:
        # this call either makes it first (and the removal then deletes it) or
        # is refused and leaves the delete alone.
        core.conversations.refuse_removal(key, subject=repr(directory.name))
        held = subagent_dirs(maker)
        created = directory not in held
        if created and directory.exists():
            raise ToolError(
                f"{directory.name!r} is already a directory inside your "
                f"`subagents/` that is not a subagent — a half-removed one, or one "
                f"made by hand. Pick a name that is free, or remove that "
                f"directory yourself."
            )
        limit = core.current().subagent.max_running
        if created and len(held) >= limit:
            raise ToolError(
                f"{maker} already has {len(held)} subagents (the `subagent: "
                f"max_running` cap); remove one with {SUBAGENT_REMOVE}, or raise "
                f"it in the config."
            )
        made_at = now()
        if created:
            agent_dir(maker, directory.name)
        record = read_subagent(maker, directory.name)
        # When it was made is not a question a later call is answering; a second
        # call says what this subagent runs on and what it is for.
        _write_subagent(
            maker,
            directory.name,
            {
                "name": directory.name,
                "model": model,
                "role": role or DEFAULT_ROLE,
                "created_at": record.get("created_at") or made_at,
            },
        )
        if not created:
            # **A live conversation takes the new settings now.**  `subagent_spec`
            # is read when a loop is built, so without this a second
            # `subagent_create` rewrote the file and answered `model: B` while the
            # running conversation kept model A and its old role — the list and
            # the conversation disagreeing about the same subagent.  The lock is
            # the turn's, so the update lands between turns; a turn that will not
            # yield is left for the idle reap (which rebuilds from the file) and
            # is named in the log rather than blocking this call for ever.
            live_loop = core.conversations.get(key)
            if live_loop is not None:
                spec = SubagentSpec(model=model, role=role or DEFAULT_ROLE)
                try:
                    async with asyncio.timeout(_T.timeouts.grace.turn):
                        await live_loop.lock.acquire()
                except TimeoutError:
                    logger.info(
                        "subagent %s is mid-turn; its new settings apply from "
                        "the next conversation",
                        describe((maker, directory.name)),
                    )
                else:
                    try:
                        try:
                            name, provider, settings = core.current().resolve(
                                model or spec.model
                            )
                        except ConfigError as exc:
                            # The record is written and this conversation cannot
                            # move to it yet; saying so where the operator looks
                            # keeps a successful edit from reading as a failed
                            # call, and the next conversation starts from the
                            # file (where the same reference fails its turn).
                            logger.warning(
                                "subagent %s: the new model was not applied: %s",
                                describe((maker, directory.name)),
                                exc,
                            )
                        else:
                            live_loop.model = f"{name}/{settings.model}"
                            live_loop.provider = name
                            live_loop.api = provider.api
                            live_loop.model_id = settings.model
                            live_loop.accepts_images = settings.accepts_images
                            live_loop.context_window = settings.context_window
                            head = opening_messages(
                                maker, directory.name, core.current(), spec
                            )
                            if head and live_loop.messages:
                                live_loop.messages[0] = head[0]
                    finally:
                        live_loop.lock.release()
        logger.info(
            "subagent %s %s (model %s, role %s)",
            describe((maker, directory.name)),
            "created" if created else "updated",
            model or "the maker's",
            role or DEFAULT_ROLE,
        )
        return {
            "name": directory.name,
            "model": model,
            "role": role or DEFAULT_ROLE,
            "created": created,
        }

    @mcp.tool(name=SUBAGENT_REMOVE, meta=FOR_THE_MODEL)
    async def subagent_remove(ctx: Context, name: str) -> dict[str, Any]:
        """Unmake a subagent, and take everything it wrote with it.

        Its conversation is forgotten and everything it wrote — turns, cabinet,
        ledger — is deleted. Removing a subagent that is not there is not an
        error: the caller wanted it gone, and it is gone. A name that exists but
        is not one of your subagents is refused rather than deleted. A turn it
        is running is interrupted first, so removal works mid-turn.

        Args:
            name: Which of your subagents.  The same name you made it with.

        Returns:
            `name`, and `removed`: whether there was one here to remove.
        """
        who = remover_of(ctx)
        maker, mine = who
        if mine:
            raise ToolError(
                f"a subagent cannot remove a subagent: {describe(who)} owns no "
                f"subagents — its maker does. Ask your own maker to remove it."
            )
        directory = subagent_path(maker, name)
        if directory not in subagent_dirs(maker):
            if directory.exists():
                raise ToolError(
                    f"{directory.name!r} is not a subagent of {maker}'s — it is "
                    f"not yours to remove, and this call will not delete it."
                )
            return {"name": directory.name, "removed": False}
        # **The network is told first, and that is what interrupts a turn.**  A
        # member's turn runs in the subagent-network process (a subagent has no
        # window), and giving up its place drops the session, which interrupts
        # that turn at the caller — the same caller-abandoned ending a window's
        # Escape gives its own turn.  Waiting for the lock without that was the
        # old order, and a mid-turn removal could only refuse: nothing else in
        # this process can interrupt a turn, so the maker had no lever at all
        # (SDD §4.5.4 says this is a forced cleanup, idle or mid-turn).
        #
        # **And no `forget` against the store, which is the difference from
        # `reset`.**  What `reset` clears is the *stored* live-context list, and
        # that list is a table in `turns.db` — the file this is about to delete.
        # Asking the store first would be asking it to clear a file that is on
        # its way out, and on Windows the asking is not free: opening a store
        # that does not exist *creates* it, and the handle that created it can
        # hold the file against the `rmtree` that follows.
        #
        # **The loop is marked removed before it is dropped**, so a message
        # already queued behind the lock cannot run and save a turn into the
        # directory after the delete, recreating it without a record.
        key = conversation_key(maker, directory.name)
        # **The claim is synchronous and comes before the first await.**  A
        # message that arrives during the network notification or the delete
        # checks this claim on the loop-creating path, so it cannot start a turn
        # whose save recreates the directory.  One removal at a time per name:
        # a second concurrent removal would walk a tree the first is deleting
        # and report its vanished files as held open, and the first's `finally`
        # would release the claim while the second still deletes.
        with core.conversations.removal(key, subject=repr(directory.name)):
            loop = core.conversations.get(key)
            if loop is None:
                # **The network before the files, for a subagent with no loop.**  A
                # place on the subagent network outlives the directory it was held
                # for: the plugin has a responder subscribed to this name, so a
                # removal that only deleted the files would leave something still
                # answering for a subagent that no longer exists
                # (`leave_the_mesh`).
                await leave_the_mesh(core, maker, directory.name, client=mesh_client)
            else:
                if core.current().subagent.network.configured:
                    # A turn in flight is interrupted by this call; an idle one has
                    # nothing to interrupt, and the later call below still gives up
                    # the place (a new session may have been created in between).
                    await leave_the_mesh(
                        core, maker, directory.name, client=mesh_client
                    )
                try:
                    async with asyncio.timeout(_T.timeouts.grace.turn):
                        await loop.lock.acquire()
                except TimeoutError:
                    # **A turn the clean interrupt did not reach is interrupted
                    # here.**  The network's cancel is a caller abandoning the call,
                    # which the agent server honours at its next checkpoint; a turn
                    # wedged somewhere that is not one is ended by cancelling its
                    # own handler.  It is the last step because it reaches into a
                    # request the server does not own, and it is bounded so a turn
                    # that cannot be interrupted cannot hold the removal for ever.
                    task = loop.turn_task
                    if task is not None:
                        task.cancel()
                        try:
                            async with asyncio.timeout(_T.timeouts.grace.drain):
                                await loop.lock.acquire()
                        except TimeoutError:
                            raise ToolError(
                                f"{directory.name!r} is still running a turn that "
                                f"will not stop; nothing was deleted — try again."
                            ) from None
                    else:
                        raise ToolError(
                            f"{directory.name!r} is still running a turn; wait for "
                            f"it, then remove it again — removing it now would let "
                            f"that turn write the directory back."
                        ) from None
                try:
                    if core.conversations.get(key) is not loop:
                        raise ToolError(
                            f"{directory.name!r} changed while it was being removed; "
                            f"try again."
                        )
                    pending = loop.record_pending
                    if pending is not None:
                        # **The detached write is not under the lock.**  It is a
                        # cancelled turn's record already on its way to the context
                        # store; letting the tree go before it lands recreates the
                        # directory.  A refused write has nothing more to wait for.
                        try:
                            async with asyncio.timeout(_T.timeouts.grace.drain):
                                await asyncio.shield(pending.written)
                        except TimeoutError:
                            raise ToolError(
                                f"{directory.name!r} is still recording its last "
                                f"turn; try again in a moment."
                            ) from None
                        except Exception:  # noqa: BLE001 — a refused write writes nothing
                            pass
                    # **The place is given up again under the lock.**  The call
                    # above was what stopped a turn; this one is what makes the
                    # state true at the moment of the delete — a message that
                    # arrived while the lock was being taken may have placed a
                    # fresh session.  A failure here leaves the files alone: the
                    # loop is untouched, because `leave_the_mesh` raises before the
                    # record is dropped.
                    await leave_the_mesh(
                        core, maker, directory.name, client=mesh_client
                    )
                    loop.removed = True
                    core.conversations.pop(key)
                finally:
                    loop.lock.release()
            # The retry loop sleeps up to `storage.sqlite_busy` between attempts;
            # on the event loop that stalls every turn, stream and approval in this
            # shared server, so the whole removal runs on a thread.
            await asyncio.to_thread(_remove_tree, directory)
            # **A watcher is told, not left on a stream that will never write
            # again**, and it is told after the directory is gone: a delete that
            # failed raises out of `_remove_tree` with the record still there (it is
            # removed last), so the conversation is still a subagent — and a watcher
            # told it was gone would have been told something untrue.  A stream that
            # simply stopped, on the other
            # hand, would be indistinguishable from an idle conversation, so the
            # last frame says why and the call returns after it (`WatchHub.close`).
            core.watching.close(key, f"{directory.name} was removed")
            core.watching.drop(key)
            logger.info("subagent %s removed", describe((maker, directory.name)))
            return {"name": directory.name, "removed": True}


def _delete_file(path: Path) -> None:
    """Remove one file a removal owns; the seam a test holds open.

    Windows refuses this for a file another process holds, and the retry loop
    around `_remove_tree` is what waits that out.  A function rather than
    `path.unlink` inline so the held-file test can raise the OS's own failure
    at the exact call the removal makes.
    """
    path.unlink(missing_ok=True)


def _remove_tree(directory: Path) -> None:
    """Take a subagent's whole directory away, waiting out a file still in use.

    **Windows will not delete a file that is open**, and the one thing in there
    that can be is the subagent's own conversation store: `Recorder.remember_turn`
    detaches the record of a turn, so a write can outlive the answer that produced it, and
    a write genuinely in flight is not something the caller can wait for — it
    belongs to a turn that has already been answered, in another process.

    The wait is `storage.sqlite_busy`, which is the registry's number for exactly
    this ("how long a writer waits for the writer holding the database"): a
    holder that is a write in flight lets go in milliseconds, and one that
    outlasts this is not a write.

    A recursive delete cannot be atomic, so the *record* is what is made last:
    a failed removal leaves the remaining files and `subagent.yaml` — still a
    subagent, and one a second `subagent_remove` can finish.  Previous versions
    removed the record in `rmtree`'s own order, so a locked `turns.db` could
    leave a directory no caller could own.

    Raises:
        ToolError: With the file named and nothing else.  A `WinError 32`, in the
            OS's own words and in the OS's own *language*, is not an answer a
            model can act on; "it is open, try again" is.
    """
    # **The record goes last.**  A recursive delete is not atomic anywhere, and
    # `rmtree` walks in its own order: a Windows `PermissionError` on the open
    # `turns.db` could leave the directory with `subagent.yaml` already gone —
    # neither a subagent nor a directory nobody can create, refused by both
    # `subagent_create` and `subagent_remove` until somebody cleans the tree by
    # hand.  Removing the record at the end makes a failed delete leave a
    # *subagent* (one still removable), not a locked name.
    record = directory / SUBAGENT_RECORD
    deadline = time.monotonic() + _T.timeouts.storage.sqlite_busy
    while True:
        if not directory.exists():
            # Another removal finished the tree between the caller's check and
            # this hop, or between two attempts; gone is the outcome asked for.
            # Checked every attempt: a walk that meets a directory another
            # removal just deleted raises `FileNotFoundError`, and retrying that
            # to the deadline would report a vanished file as held open.
            return
        try:
            for root, dirs, files in os.walk(directory, topdown=False):
                base = Path(root)
                for name in files:
                    path = base / name
                    if path != record:
                        _delete_file(path)
                for name in dirs:
                    try:
                        (base / name).rmdir()
                    except OSError:
                        # A file inside is still held; the parent rmdir below
                        # reports the one failure this loop retries.
                        pass
            # The record is the last thing to go; the retrying seam is for the
            # conversation's bulky files, which are the ones a writer holds.
            record.unlink(missing_ok=True)
            directory.rmdir()
            return
        except OSError as e:
            if time.monotonic() >= deadline:
                raise ToolError(
                    f"{directory.name!r} could not be removed: {e.filename} is "
                    f"open in another process — its conversation may still be "
                    f"being written. Ask again in a moment."
                ) from e
            time.sleep(_T.timeouts.pacing.file_release)


def _write_subagent(agent: str, subagent: str, record: dict[str, Any]) -> None:
    """Write a subagent's record — the file that makes its directory one.

    It holds the whole of what a subagent *is*, and it is the whole of that: the
    name it answers to, what it runs on, the role it was made for, and when it
    was made (`SubagentSpec` reads those back).  A second place holding any of
    it — a section in `slife2.yaml`, a table in a db — would be a thing to keep
    in step with a directory that is deleted without notice, and this file is
    deleted with it.

    Written whole every time, because it is one small mapping and a merge would
    be a second rule about which keys survive a `set`.  Block style, like
    `wechat.yaml` and `groups.yaml`: somebody reading one of them should not have
    to read a second format to see the next fact.

    **A write is a rename**, through `statefile.write`, so a process that dies
    mid-write leaves the old record or the new one and never half of either.
    """
    statefile.write(subagent_file(agent, subagent), record)
