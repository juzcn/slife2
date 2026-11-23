"""The in-memory state one agent server holds: a conversation and its turns.

`Loop` is one conversation and everything that makes it a thing; `Pending` is a
message waiting to become a turn of it; `Outcome` is what a turn did; and
`PendingRecord` is a turn's write when the caller who asked for it could not
wait.  They are the vocabulary the rest of this plugin shares — the core that
runs the turn, the recorder that writes it, and the membership that removes a
subagent's whole directory — so they live here rather than in any one of them.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from slife2.libs.turn.loop import AgentLoop, TurnResult
from slife2.libs.turn.messages import Message, Usage


@dataclass(eq=False)
class Pending:
    """A message submitted to a loop, waiting for its own turn — or for one.

    It is held here, and *not* appended to the loop's messages as a user message,
    until its turn begins.  What may happen to it in between is the running
    turn **folding it in** (`take_waiting` -> `folded_message`): the message goes to the model at that
    turn's next iteration boundary as a harness tool pair, and this entry's `result`
    is set to that turn's answer.  It is never appended as a user message
    mid-turn, which is what would be steering nobody asked for — the model reads
    a folded message as something that arrived, which is what it is.

    `eq=False` because the inbox is a queue of *submissions* and not of values.
    `deque.remove` compares by equality, so the generated `__eq__` — which
    compares `(prompt, attachments, channel)` — would make two callers who sent
    the same text one entry: the first turn takes its own message out of the queue,
    and its closing `remove` then takes the *other* caller's place in it, since
    that entry is equal.  The count `MAX_QUEUED` guards would then fall below
    the number of callers actually waiting, which is the bound's whole job.
    """

    prompt: str
    #: What this message arrived with, as **sources** rather than as encoded
    #: bytes: a path on this machine, an `http(s)` URL, or a `data:` URI.  They
    #: are turned into content parts by `slife2.libs.turn.attachments` — the same encoder
    #: `attach_image` uses, so a caller that holds a path and a model that is
    #: given one cannot come to different conclusions about the same file.
    images: list[str]
    channel: str
    #: Whether this caller can be asked a question mid-turn and will answer it.
    #:
    #: **It is the caller's to say, and nobody else can.**  "Is a handler
    #: installed" would answer a different question: every turn arrives at the
    #: same shared server, and whether anybody is watching is a fact about
    #: *this* call rather than about the process — the TUI is watched, a WeChat
    #: message is not, and the two can address one conversation an hour apart.
    #: False is the safe default: a caller that cannot answer must never be
    #: asked, or the model's request for a check becomes a turn that hangs.
    watched: bool = False
    #: Set by the turn that folded this message into itself, and **only when that
    #: turn produced a result**.  `None` and `delivered` together are what say
    #: whether this caller owes itself a turn: an answer that exists, or a fold
    #: that was recorded, means it does not.
    #:
    #: A turn that produced no result — cancelled *or* failed — leaves this alone
    #: on purpose: the message was not answered.  Whether it was nonetheless
    #: *delivered* is `delivered`'s to say, and restoring it to the inbox would
    #: race a caller that had been cancelled in the meantime and leave an orphan
    #: the next turn would fold in with nobody waiting for it.
    result: TurnResult | None = None
    #: Whether a running turn took this message into its conversation — the fold
    #: itself, which is delivery even where there is no answer.
    #:
    #: **Set when the pair is written, not when the message is taken**
    #: (`folded_message`): between the take and the append there is one `_fit`,
    #: and a turn that dies in it leaves the message in no record at all.  Marked
    #: at take time, that window answered its sender "injected, empty" about a
    #: message the conversation never held.
    #:
    #: **A recorded cut-in is delivered even when the absorbing turn had no
    #: result.**  The cancellation repair keeps the pair and the turn's row
    #: records it, so the message is in the conversation; a parked caller that
    #: ran its own turn because `result` was `None` would put the same prompt in
    #: there twice and have the model answer it twice — the outcome the receipt
    #: mechanism exists to prevent.  It is cleared when that turn's record is
    #: refused and its messages are rolled back (`unanswer_folded`): the message
    #: is gone from the conversation then, and its own turn is the honest answer.
    delivered: bool = False
    #: The model that answered this message, written **with** `result` and only
    #: by the turn that wrote it.  Kept on the entry rather than read off the
    #: loop when the caller is finally answered, because the two can differ: the
    #: reply names what answered, and a `model_switch` arriving while that turn
    #: was running moves the loop's reference without moving the answer
    #: (`slife2.plugins.agent.core._reply` is where that is argued).  Nothing
    #: reads this unless `result` is set, so its emptiness is not a branch —
    #: `result` is still the one field that says whether anything folded this in.
    model: str = ""
    #: The submitting client's own id for this message, echoed on the
    #: `MessageCutIn` event when a running turn takes it.
    #:
    #: **Opaque here and load-bearing there.**  A window that submits a message
    #: while a turn is running has to know whether that message was folded in or
    #: ran its own turn — its call produces no events in the first case — and
    #: the receipt is what makes that an identity rather than a guess at the
    #: words.  The server never reads it.
    receipt: str = ""


@dataclass
class Outcome:
    """What one turn did, filled in even when the turn was cancelled.

    `messages` is a copy taken after any repair, so it is the slice this turn is
    answerable for: the whole exchange, or as much of it as the turn had
    completed when it was interrupted.
    """

    messages: list[Message] = field(default_factory=list)
    result: TurnResult | None = None
    #: What the turn spent, filled in however it ended — including the
    #: cancellation path, where a result is never produced.  The loop keeps the
    #: running total while it runs (`AgentLoop.usage`), so a turn cut off after
    #: two completed model calls reports those two rather than zero; a turn
    #: interrupted inside a call reports the calls that came back before it.
    #: `last_usage` is the last completed call's own, which is what
    #: `context_tokens` means.
    usage: Usage = field(default_factory=Usage)
    last_usage: Usage = field(default_factory=Usage)
    #: Sources this turn was handed that could not be attached, in the caller's
    #: words.  On the outcome rather than on the loop, because it is a fact about
    #: *this* turn — the next one attaches nothing of the sort — and it is what
    #: the caller is told, since a call whose pictures were fewer than it asked
    #: for is a call that cannot tell.
    refused: list[str] = field(default_factory=list)
    #: The model this turn *ran on*, taken when it started.  Held because the
    #: loop's own reference can move while a turn is in flight — `model_switch`
    #: moves every conversation it is holding — and what that turn's record must
    #: name is the model that answered it, which is the one it began on.
    model: str = ""
    started_at: str = ""
    completed_at: str = ""
    #: The turn's own wall time, on the monotonic clock.  Kept on the outcome
    #: rather than derived from `started_at`/`completed_at` because the two are
    #: ISO strings written for the row, and a duration read back out of them
    #: would be a second parsing of the store's format.  It is what the one
    #: `turn_done` line reports, and it is filled in even when the turn was
    #: cancelled — the clock ran whether or not the turn finished.
    elapsed: float = 0.0
    #: The inbox entries this turn folded in, kept so a record that is refused
    #: can **un-answer** them (`unanswer_folded`): a caller whose message is
    #: rolled back with the turn must run its own turn, not be told an answer
    #: that is no longer in the conversation.
    folded: list[Pending] = field(default_factory=list)


@dataclass
class PendingRecord:
    """A turn's detached write, and the turn its id will make addressable.

    **The id is not knowable when the caller needs it.**  The write is detached
    — `AgentCore.detach` says why — when the turn was interrupted (the caller is
    gone and cannot wait) or when a caller was cancelled during the save of a
    turn that had already finished.  What it answers with arrives later, and the
    turn is not addressable until `Recorder.settle_record` waits for it.  The outcome
    and the item ride with the task because they are what the footnote is made
    of: the timestamps the row was written with and the channel it carries.
    """

    written: asyncio.Task[int | None]
    outcome: Outcome
    item: Pending


@dataclass
class Loop:
    """One conversation, and everything that makes it a thing.

    `subagent` is part of the identity rather than a label on it: two keys differ
    exactly when their pairs differ, and the pair is what the lock, the inbox and
    the write to the db are each scoped to.
    """

    agent: str
    subagent: str
    model: str
    #: The model's token window, for the automatic compaction's threshold and
    #: `_context_rebuild`'s estimate.  Required: a conversation is only
    #: ever started on a model whose config entry states one.  Held with the rest
    #: of how this conversation reaches its model (see `accepts_images`): a model
    #: switch moves it, and a conversation whose model left the config keeps the
    #: window it started on.
    context_window: int
    #: **The iteration cap this conversation started on**, read where the model is
    #: read — at the start, from the config as it was then — and not per turn.
    #: `AgentCore.loop_for` runs on every turn, so taking it from the live config
    #: would let an operator's edit change the terms of a turn already in flight.
    max_iterations: int
    #: The fraction of the window at which this conversation's turns compact
    #: themselves, or 0 for manual-only.  Read when the conversation starts —
    #: the same moment and the same reason as `max_iterations`: an operator's
    #: edit must not change the terms of a conversation already running.
    compact_at: float
    messages: list[Message]
    lock: asyncio.Lock
    last_used: float
    inbox: deque[Pending] = field(default_factory=deque)
    #: Whether this conversation has been put back on the context it had when it
    #: last stopped.  A property of the *loop* rather than of the call that
    #: created it: two callers can arrive on one brand-new key at the same time,
    #: and only one of them is the caller that created it — so a per-call flag
    #: would leave the other running a turn on the empty list the restore was
    #: about to replace.  Set under the lock, and only
    #: once the restore has actually answered, so a store that failed is tried
    #: again by the next turn rather than leaving the conversation blank for the
    #: life of the process.
    restored: bool = False
    #: The entries this turn has folded in, in the order it took them.  Held for
    #: exactly one turn: it is how the turn's own end reaches them (their `result`
    #: is set there), and it is cleared by the same `finally`, so a later turn can
    #: never answer for one this one took.
    injected: list[Pending] = field(default_factory=list)
    #: The live ids this turn's compaction has hidden from its own later model
    #: calls.  **Turn-scoped, and never part of the record**: the loop's list,
    #: the saved row and the store's `covered` are the full conversation; this
    #: is only the projection the model runs on (`slife2.libs.turn.compact`).
    #: Ids rather than message objects, so hiding one does not renumber the rest
    #: and a rebuilt list addresses the same messages.  Cleared when a turn
    #: starts, so a hidden message cannot outlive the turn that hid it.
    hidden: set[str] = field(default_factory=set)
    #: Whether this turn's automatic compaction has stopped asking.  Set when
    #: the selector keeps everything (`dropped == 0`) or fails: that is a
    #: decision about the whole context, not something to re-ask on every
    #: iteration.  A later successful call — the model's own — clears it, and so
    #: does a new turn.  A compaction that drops something leaves it false, so
    #: the next crossing of `compact_at` asks again.
    compact_hold: bool = False
    #: Which turns of the log `messages` is made of, in order — the agent's copy
    #: of the live-context list the store persists.  Held here rather than read
    #: per turn because it *is* the in-memory state: `rebuild` is handed it and
    #: answers with its replacement, and the two must be the same list or a
    #: keep-list would name turns the messages do not have.
    turn_ids: list[int] = field(default_factory=list)
    #: How many of `messages` those ids account for — the system prompt, then
    #: each turn in the list, in order.  What follows is the **carried tail**:
    #: messages this conversation holds that no turn in the list covers, which a
    #: rebuild takes back verbatim rather than reconstructing.
    #:
    #: **The ids have to describe a *prefix*, and that is the invariant the whole
    #: count rests on.**  `_context_rebuild`'s body hands `len(messages) - covered`
    #: to the store as "how many messages at the end no turn accounts for", and
    #: the store re-appends `messages[-carried:]` — so an unbacked run that is not
    #: at the end would be read off the wrong end of the list entirely.
    #:
    #: A turn is unbacked only while its write is *pending*: it was interrupted
    #: (the caller is gone) or its caller was cancelled during the save of a turn
    #: that had already finished.  `Recorder.settle_record` takes the id up on the next
    #: turn and the messages become addressable.  **A write that is refused is a
    #: hard failure**: the turn's messages are rolled back to this prefix, so a
    #: silent unaddressable run cannot be left behind.  A count is enough here
    #: because of that rule, and it is what lets a rebuild replace the turns
    #: without destroying a message that has no turn to be replaced by.
    covered: int = 0
    #: The write of a turn whose caller could not wait for it, kept until the
    #: turn that follows can take up the id it answered with — see
    #: `Recorder.settle_record`.
    #:
    #: **The id is what makes the run backed, and the write is where it is.**  The
    #: write is detached because an interrupted caller cannot await and a
    #: cancellation would cancel an awaited one at its next checkpoint, so by the
    #: time the turn ends it has not answered.  Left unclaimed for good, the
    #: turn's messages would stay in the carried run — and once one turn has left
    #: a run there, the guard below (`covered == before`) is never true again,
    #: because a rebuild re-appends the run at the end of a list `covered` does
    #: not count.  Every later turn would then be unaddressable *and*
    #: recallable: its id never joins `turn_ids`, so `annotate_turn` never writes
    #: a footnote the model can name it by, while the row is in the log for a
    #: query recall to find — and the store would render that turn from its row
    #: and re-append the same messages verbatim, so the exchange appears twice in
    #: the list the turn runs on.
    #:
    #: Set only when the turn began on a wholly backed list (`covered == began`),
    #: because that is what makes its id describe the whole of what follows that
    #: prefix.  A refused write does not leave the run: `Recorder.settle_record` rolls it
    #: back (a hard failure).
    #:
    #: **The outcome and item travel with the task** so that `Recorder.settle_record` can
    #: write the turn's footnote in the same step that claims its id
    #: (`PendingRecord` says why the two cannot be separated).
    record_pending: PendingRecord | None = None
    #: **Set when this conversation has been removed.**  A message already
    #: queued on the loop is waiting for the lock; when it gets in, this is what
    #: tells it the conversation no longer exists.  Without it that turn would
    #: run and its save would recreate the directory `subagent_remove` had just
    #: deleted — and the recreated directory has no `subagent.yaml`, so the name
    #: is blocked for `subagent_create` and untouchable for `subagent_remove` for
    #: ever.
    removed: bool = False
    #: The task running this conversation's turn, or None when it is idle — the
    #: handle an **interrupt** cancels.  `subagent_remove` asks the subagent
    #: network to interrupt the member turn first (a clean cancellation the
    #: agent server sees as a caller abandoning the call); one that outlasts
    #: that is interrupted here, because removal is a forced cleanup whether it
    #: is idle or mid-turn (SDD §4.5.4).  One turn runs at a time, so one slot
    #: is the whole of it.  Set once the message is running its own turn,
    #: cleared on the way out by the task that set it.
    turn_task: asyncio.Task | None = None
    #: **The token of the turn running on this loop**, or empty when it is idle.
    #:
    #: A loop is a *turn's* working state, not a property of the conversation's
    #: id.  One id can have two live turns in two processes — the agent server's
    #: own and an agent task standing in for the same conversation — and the
    #: token is the distinguishing value every live-conversation tool needs: it is
    #: minted where the turn starts, carried on every hop, and compared against
    #: this field before a tool touches the list or the inbox.
    #:
    #: Set under the lock and cleared on the way out by the turn that set it (a
    #: value guard, because a queued turn may have replaced it first).
    turn_token: str = ""
    #: The loop running this turn, or None when the conversation is idle.  The
    #: `_turn_compact` handler reads its last measured call — the figure the
    #: status bar is showing — and the turn that set it clears it on the way out
    #: under the same value guard `turn_token` uses.
    live: AgentLoop | None = None
    #: The rest of how this conversation reaches its model, resolved once when it
    #: started and kept beside the reference.  Held rather than re-resolved so a
    #: conversation whose model is later edited out of the config still runs: the
    #: backend it talks to is already open, and only a *new* conversation reads
    #: the file.
    provider: str = ""
    api: str = ""
    model_id: str = ""
    accepts_images: bool = True
    #: The index in `messages` where this turn's opening sits.  **The record's
    #: slice, and the prefix rule's boundary**, and the reason it is a field
    #: rather than the local it used to be: `_context_rebuild` can replace the
    #: historical prefix under a running turn, which moves the opening.  The
    #: tool body updates this; `run_turn_into` slices the record from it.
    turn_opening: int = 0
    #: Whether this turn's automatic `_context_rebuild` has been tried.  It is
    #: set at the first boundary *before* the harness's call, because a hub hop
    #: that fails never reaches the tool's body; the attempt is the turn's one
    #: automatic rebuild, whether or not it ran, and after it the tool is the
    #: model's to call.  Reset with `hidden` when a turn starts.
    rebuilt: bool = False
    #: What the last `_context_rebuild`/`_turn_compact` body decided, as the
    #: tool arguments that express it.  The automatic call's pair is written by
    #: `AgentLoop.run_turn` *after* the tool answered, and this is how the
    #: decision's arguments reach that pair; the model's own call carries its
    #: arguments already.  **Cleared when a turn starts and before each
    #: automatic call**, so a body a failed hop never reached writes an empty
    #: call rather than the previous decision.
    last_decision: dict[str, Any] = field(default_factory=dict)
    #: The last turn's last model call, in tokens — the figure the status bar
    #: shows.  Set from the restored last turn and after every turn; 0 for a
    #: conversation that has not run one.  Not estimated from `messages`: what
    #: the bar shows is the provider's count, and an estimate of the same list
    #: would be a second figure that disagrees with it.  **The model's figure is
    #: `_context_rebuild`'s report**, computed from the rebuilt list after the
    #: selection — this one is the previous call's and is not.
    context_tokens: int = 0
    #: The `_approve` calls this conversation is holding for an answer, by call
    #: id.  On the loop because a turn is what fills and empties it, and the
    #: answer arrives on a *different* call (`answer_approval`) that must not
    #: wait for the lock the waiting turn is holding — so this is the one piece
    #: of a loop's state that is read and written while its lock is held, and
    #: that is why it is a dict of futures rather than a queue.
    approvals: dict[str, asyncio.Future[bool]] = field(default_factory=dict)
