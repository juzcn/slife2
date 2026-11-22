"""The recording machinery: a turn written to the log, and the live list kept
in step with it.

**Every turn is recorded however it ended**, cancelled ones included, and the
write maintains the *live-context list* the store persists — so this is not a
log append beside the conversation but the step that makes a turn addressable.
The caller that owns the turn hands in the write and the loop's list; this
module owns the order (images kept before the row, the id and the footnote as
one act, a refused write rolled back) so `send_message` cannot get that order
wrong by remembering it in the wrong place.

The two peers are injected as callables because they are the core's: the
context store answers `remember`, and the cabinet keeps a turn's images.  The
`backend_name` is here because an injected backend is a test's and the model
that ran genuinely is not anything the config names.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Client

import slife2.helpers.timeouts as _T
from slife2.libs.plugin.audience import conversation_meta
from slife2.libs.plugin.protocol import describe, tool_payload, unreadable
from slife2.libs.plugin.vocabulary import TURN_SUMMARIZE
from slife2.libs.turn import attachments
from slife2.libs.turn.context import turn_note, with_note
from slife2.libs.turn.messages import Usage
from slife2.libs.turn.watchhub import Tap

from .state import Loop, Outcome, Pending

logger = logging.getLogger(__name__)

Memory = Callable[[], Awaitable[Client]]
Cabinet = Callable[[], Awaitable[Client]]


def turn_annotation(
    messages: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """The annotation a rowid-less `turn_summarize` call carries.

    The model annotates the in-flight turn by calling `turn_summarize` with
    no `turn_id`; the tool answers `captured` and writes nothing.  The
    values are applied here, at the one save point, so they land on exactly
    the turn being saved — no "latest row" race, no pending state, and a
    turn whose save fails applies nothing.  A call with an explicit id is
    ignored: the tool wrote that one already.
    """
    summary: str | None = None
    tags: str | None = None
    for message in messages:
        for call in message.get("tool_calls") or ():
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            if str(function.get("name") or "").split("__")[-1] != TURN_SUMMARIZE:
                continue
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError) as exc:
                # **The annotation is a field, and the turn is the record.**
                # The tool already answered `captured`; failing the save
                # over a damaged argument would lose the turn the
                # annotation belongs to.  The damage is named instead.
                logger.warning(
                    "a %s call's arguments could not be read; "
                    "the annotation is dropped: %s",
                    TURN_SUMMARIZE,
                    exc,
                )
                continue
            if not isinstance(arguments, dict):
                continue
            if arguments.get("turn_id") is not None:
                continue
            found = arguments.get("summary")
            if isinstance(found, str) and found.strip():
                summary = found.strip()
            found = arguments.get("tags")
            if isinstance(found, str) and found.strip():
                tags = found.strip()
    return summary, tags


class Recorder:
    """Turns written to the log, and the id each one becomes addressable by."""

    def __init__(
        self,
        *,
        memory: Memory,
        cabinet: Cabinet,
        backend_name: str | None,
    ) -> None:
        self._memory = memory
        self._cabinet = cabinet
        self._backend_name = backend_name

    async def remember_turn(
        self,
        agent: str,
        subagent: str,
        messages: list[dict],
        usage: Usage,
        last_usage: Usage,
        *,
        model: str,
        channel: str,
        created_at: str,
        completed_at: str,
    ) -> int | None:
        """Persist a turn, and answer with the id it was given.

        **This is called for every turn of every conversation**, cancelled ones
        included — an agent's own and each of its subagents'.  The `usage` pair
        is taken from the loop rather than from a result, so a turn that did not
        finish still reports the model calls that did.  The rule is deliberately not
        "remember to record the cancel path": a write that is conditional on how
        a turn ended is a write somebody can forget to make, and the failure it
        produces is a conversation in the transcript that the database has never
        heard of.

        **A refusal is a failure of the turn, not a `None`.**  A store that is
        gone fails the turn (`slife2.libs.plugin.protocol.open_server` is where a
        missing peer is decided to be a broken system), and so does a store
        that answered and refused *this* request — an agent name that cannot be
        a filename, say — or answered without an id.  The caller rolls the
        unrecorded turn back and raises; there is no silent "recorded nowhere"
        path, which would leave a message the transcript shows and no later turn
        can address.

        Two token counts go over, and they are not interchangeable.  `usage` is
        the turn's total across however many model calls it took — the bill —
        and its breakdown goes with it, because a cache-hit ratio cannot be
        reconstructed from the total.  `last_usage` is the last *completed*
        call's own, which is how much conversation existed when the turn
        reached it: the number the next request would resend.  A cancelled
        turn reports both from the calls that came back — zero only when none
        did.
        """
        summary, tags = turn_annotation(messages)
        client = await self._memory()

        payload = tool_payload(
            await client.call_tool(
                "remember",
                {
                    "agent": agent,
                    "subagent": subagent,
                    "messages": messages,
                    "token_count": usage.total_tokens,
                    "context_tokens": last_usage.total_tokens,
                    "usage": usage.to_wire(),
                    "who_helped": agent,
                    # What the loop was opened on, not the reference as typed:
                    # a caller may name a bare provider or nothing at all, and
                    # neither says which model wrote the answer.
                    "what_model": model,
                    "channel": channel,
                    "summary": summary or "",
                    "tags": tags or "",
                    "created_at": created_at,
                    "completed_at": completed_at,
                },
            )
        )
        turn_id = payload.get("turn_id")
        # A bool is an int to `isinstance`, and `True == 1` — a store answering
        # `turn_id: true` would name turn 1 in the keep-list.  The file's other
        # strict readers exclude it for the same reason.
        if not isinstance(turn_id, int) or isinstance(turn_id, bool):
            raise unreadable("the context store", "return a turn id", payload)
        return turn_id

    def recorded_model(self, loop: Loop, outcome: Outcome) -> str:
        """What to write in the `what_model` column.

        The model the turn *started* on, except when a backend was injected — in
        which case the thing that ran genuinely is not anything the config
        names.  Both are the same mistake written twice, recording a model that
        never answered, which is why this is one function rather than a
        condition at each call site — and it is the loop's *record* of the turn
        rather than the loop's reference now, because `model_switch` moves the
        reference under a turn that is already running.
        """
        return (
            self._backend_name
            if self._backend_name is not None
            else outcome.model or loop.model
        )

    async def keep_images(
        self, agent: str, subagent: str, messages: list[dict[str, Any]]
    ) -> None:
        """Replace every image part in *messages* with its cabinet reference.

        **The record copies only, and new lists.**  `to_record` shares a
        message's content list with the live `Message`, so replacing a part in
        place would put the marker into the conversation the model is still
        running on.  Each changed message gets a fresh list; the live objects
        are untouched.

        **A cabinet refusal is a failure of the turn.**  It raises, and the
        caller rolls the unrecorded turn back — there is no fallback to the note
        on a turn whose image was meant to be kept.  A *provider* refusal is the
        other road: `repaired_error` has already replaced those parts with the
        note before `record` is reached.

        The pair travels on every call so a subagent's images land in the
        subagent's own cabinet, exactly as its turns land in its own store.
        """
        client: Client | None = None
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            parts: list[dict[str, Any]] = []
            changed = False
            for part in content:
                source = _image_url(part)
                if source is None:
                    parts.append(part)
                    continue
                if client is None:
                    # **Opened only when there is an image to keep.**  A turn
                    # with no pictures must not depend on the cabinet at all.
                    client = await self._cabinet()
                answer = await client.call_tool(
                    "image_save",
                    {"url": source},
                    meta=conversation_meta(agent, subagent),
                    timeout=_T.timeouts.work.hub,
                )
                kept = tool_payload(answer)
                key = kept.get("key")
                remote = kept.get("source")
                if not (isinstance(key, str) and key) and not (
                    isinstance(remote, str) and remote
                ):
                    raise unreadable("the cabinet", "return an image reference", kept)
                parts.append(
                    {
                        "type": "text",
                        "text": attachments.image_reference(
                            key=str(key or ""),
                            source=str(remote or ""),
                            mime=str(kept.get("mime") or ""),
                            size=int(kept.get("size") or 0),
                        ),
                    }
                )
                changed = True
            if changed:
                message["content"] = parts

    async def record(self, loop: Loop, item: Pending, outcome: Outcome) -> int | None:
        """Write this turn to the log if this loop has any, and answer with its id.

        **Called inside the lock.**  The write maintains the *live-context list*,
        and a rebuild reads that list: a queued turn that started before the
        write landed would be handed a context missing the turn it is following
        up on, and would drop it.  The price is one loopback before a queued turn
        starts; the thing it buys is that `turn_ids` and `messages` are never out
        of step.

        **Images are kept before the row is written.**  The record copy is
        handed to `keep_images`; a cabinet refusal raises and the caller treats
        the turn as unrecorded, because a row that names an image the cabinet
        does not hold is a marker that can never be attached again.
        """
        if not outcome.messages:
            # A caller cancelled while queued, in which case its turn never
            # started and there is nothing to record.
            return None
        messages = [message.to_record() for message in outcome.messages]
        await self.keep_images(loop.agent, loop.subagent, messages)
        return await self.remember_turn(
            loop.agent,
            loop.subagent,
            messages,
            outcome.usage,
            outcome.last_usage,
            model=self.recorded_model(loop, outcome),
            channel=item.channel,
            created_at=outcome.started_at or outcome.completed_at,
            completed_at=outcome.completed_at,
        )

    async def record_into(
        self, tap: Tap | None, loop: Loop, item: Pending, outcome: Outcome
    ) -> int | None:
        """`record`, and the tap learns the id **before** the write answers.

        **The store and the tap are two facts, and the watcher reads both.**  A
        turn's row is visible the moment the store commits it, while
        `tap.recorded_turn` used to be written by the caller after the write
        answered — so a watcher could read a snapshot that already held the turn
        while the tap still said it did not, and draw it twice.  Setting the id
        here, inside the task the watcher waits on (`Tap.writing`), makes the
        two answer one way: a write in flight is waited out, and a write that
        failed leaves the field alone and the replay is the missing half rather
        than a duplicate.

        A turn with nothing to record — a caller cancelled while queued — and a
        failed write both answer `None`, which is the same thing this leaves the
        tap saying.
        """
        turn_id = await self.record(loop, item, outcome)
        if tap is not None and isinstance(turn_id, int):
            tap.recorded_turn = turn_id
        return turn_id

    def roll_back(self, loop: Loop, outcome: Outcome) -> None:
        """Drop an unrecorded turn's messages, leaving the backed prefix.

        **The tail is the turn's own messages** — `record_pending` is set only
        when the turn began on a wholly backed list, and nothing appends between
        the turn and the settle — but the identity is checked rather than
        assumed, because deleting the wrong slice of a conversation is not a
        mistake a later rebuild can repair.
        """
        count = len(outcome.messages)
        if count == 0:
            return
        tail = loop.messages[-count:]
        if len(tail) != count or any(
            kept is not mine for kept, mine in zip(tail, outcome.messages, strict=True)
        ):
            logger.error(
                "the unrecorded turn is not the tail of %s; not rolling it back",
                describe((loop.agent, loop.subagent)),
            )
            unanswer_folded(outcome)
            return
        del loop.messages[-count:]
        unanswer_folded(outcome)

    async def settle_record(self, loop: Loop) -> None:
        """Take up the id of a turn whose write the caller could not wait for.

        **The step that turns an unbacked run back into a turn**, and it is here
        — at the start of the next turn, inside the lock — because the record of
        an interrupted turn is detached (`AgentCore.send_message`'s handler says
        why), so its id is only knowable once somebody can wait for that write.
        A loop that never takes it up is a loop whose `covered` can never advance
        again: the turn's messages stay in the carried run, and the rebuild that
        re-appends that run leaves every later turn's id unclaimed too, since
        the ids have to describe a prefix of `messages` and a carried run in
        front of them is not one.

        Nothing appends to `messages` between the turn and this, so the id
        covers the whole of what follows `covered`.  **A refused write is a hard
        failure**: the turn was not recorded, so its messages are rolled back to
        the backed prefix and any caller it had already answered is un-answered
        (`roll_back`).  That is the same rule `record`'s callers apply when they
        are still there to see the refusal.

        **And it writes the footnote the interrupted turn could not have.**  The
        turn ended before its id existed, so `annotate_turn` had nothing to
        write with; this is the first moment it does, and `address_turn` writes
        the id and the footnote together — the two are one act, and
        `PendingRecord` says why.
        """
        pending = loop.record_pending
        if pending is None:
            return
        try:
            turn_id = await pending.written
        except Exception as exc:  # noqa: BLE001 - a refused write is a hard failure
            logger.warning(
                "the turn of %s was not recorded (%s); rolling it back",
                describe((loop.agent, loop.subagent)),
                exc,
                exc_info=exc,
            )
            turn_id = None
        # **Cleared only after the await.**  A cancellation while waiting leaves
        # the field for the next turn; the task itself was created outside this
        # scope, so it still completes.
        loop.record_pending = None
        if turn_id is None:
            self.roll_back(loop, pending.outcome)
            return
        self.address_turn(loop, pending.outcome, pending.item, turn_id)
        loop.covered = len(loop.messages)

    def address_turn(
        self, loop: Loop, outcome: Outcome, item: Pending, turn_id: int
    ) -> None:
        """Make one recorded turn addressable — its id and its footnote as one act.

        **They are one act, and separating them fails in both directions.**  An
        id in `turn_ids` that no message carries is a turn the decision
        cannot name: the ids it writes in a keep-list are the ones it read, and
        it reads them in `[TURN:…]`.  A footnote whose id is not in `turn_ids`
        is worse: the keep-list contains it, and `rebuild`'s intersection drops
        it as an id it cannot see (`slife2.plugins.context.server.rebuild`).
        The success path and `settle_record` are the two moments a turn becomes
        addressable, and this is the one place both halves are written so they
        cannot drift apart.
        """
        loop.turn_ids.append(turn_id)
        annotate_turn(outcome, item, turn_id)


def unanswer_folded(outcome: Outcome) -> None:
    """Take back what a turn told the messages it folded in.

    A record that is refused rolls the turn's messages back, so an answer
    that was already handed to a parked caller would name a turn the
    conversation no longer holds.  ``None`` sends that caller on to its own
    turn, which is the only honest answer left.
    """
    for folded in outcome.folded:
        folded.result = None
        folded.delivered = False
        folded.model = ""


def annotate_turn(outcome: Outcome, item: Pending, turn_id: int) -> None:
    """Write a turn's id and span onto the message that opened it.

    **The footnote is how a turn id reaches the model at all**, and
    therefore how a keep-list is expressible: the ids a model writes back
    are the ids it read.  `messages_from_turns` puts one on every turn a
    *rebuild* builds, which covers everything that came out of the store —
    and this is the other half: the turn that has just run is in
    memory and in no rebuilt list yet, so without this the newest turns are
    the only ones the model cannot name, and a keep-list silently drops
    exactly those.

    Called **after** the save, and never written into it.  The row holds the
    user's own words and the footnote is derived from the row when a list is
    built; storing it as well would put two of them in one message on the
    next rebuild.  That is also why the timestamps are the ones the row was
    written with — the two spellings have to agree to the character.

    The message is `outcome.messages[0]`, which is the *same object* the
    loop is holding: the snapshot is a shallow copy, so there is no index to
    carry and the live list is annotated by annotating this.
    """
    opening = outcome.messages[0] if outcome.messages else None
    if opening is None or opening.role != "user":
        return
    opening.content = with_note(
        opening.content,
        turn_note(
            turn_id,
            outcome.started_at,
            outcome.completed_at,
            # The channel the row is written with, from the same field — a
            # footnote that disagreed with the record would be the one thing
            # the model has no way to check.
            item.channel,
        ),
    )


def _image_url(part: Any) -> str | None:
    """The URL of one `image_url` content part, or `None` for any other part.

    The one reader of the wire shape, for `keep_images` when it records a turn.
    (`attach_image` resolves a marker through `attach_sources`, which asks the
    cabinet for the part's key rather than reading the wire shape.)  A part that
    is not the shape is left alone rather than guessed at — an unreadable part is
    the store's note's business, not this function's.
    """
    if not isinstance(part, dict) or part.get("type") != "image_url":
        return None
    image = part.get("image_url")
    url = image.get("url") if isinstance(image, dict) else None
    return url if isinstance(url, str) and url else None
