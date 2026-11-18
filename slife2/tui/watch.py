"""The read-only screen that shows one conversation or task the window is not running.

**A screen rather than a second transcript in the window**, because the thing
being watched is somebody else's conversation and the window underneath is still
*this* one: the main transcript keeps receiving its turn's events while this is
up, and closing the screen returns to it with nothing lost.  A view swapped into
the one transcript would have to stop drawing the window's turn to do that.

**The same widgets the window draws with.**  The history goes through
`slife2.tui.restore.restore`, the live events through `slife2.tui.draw.draw` — the
function the window itself calls — so a watched turn and a typed turn cannot
come to look different.  What is missing is the half that is the window's: there
is no prompt (a subagent has no window, and this is not one) and no status bar
(the bar describes the window's conversation, not this one).  An approval row
can appear here, because the *window's* turn can ask while this screen is up:
the app mounts the question on whichever screen is in front
(`slife2.tui.app._ask_approval`), and a watched turn itself runs unattended and
emits none.  An agent task adds a brief block
above the turn: its description is the whole of what it was given, and nobody
in the conversation said it.

**The watch call is the view.**  It is opened when the screen mounts and
cancelled when the screen goes away — the same lifetime an inbox attachment has
(`slife2.plugins.tui.server`) — so closing the screen is the detach and there is
nothing to send.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import assert_never

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message as TextualMessage
from textual.screen import Screen
from textual.widgets import Static
from textual.worker import Worker

from slife2.libs.harness.i18n import t
from slife2.libs.turn.events import (
    ContextChosen,
    MessageCutIn,
    TextDelta,
    ThinkingDelta,
    ToolApprovalRequested,
    ToolCallFinished,
    ToolCallStarted,
    ToolsCounted,
    TurnFinished,
    UsageReported,
)
from slife2.libs.turn.watch import (
    Closed,
    Snapshot,
    TaskBrief,
    TurnStarted,
    WatchItem,
)
from slife2.tui.client import WatchClient
from slife2.tui.draw import draw
from slife2.tui.restore import restore
from slife2.tui.widgets import ApprovalPrompt, ChatView, PlanView, task_bubble

logger = logging.getLogger(__name__)


class WatchItemMessage(TextualMessage):
    """One item from the watch stream, carried into Textual's handling."""

    def __init__(self, item: WatchItem) -> None:
        self.item = item
        super().__init__()


class WatchFailed(TextualMessage):
    """The watch did not open, or ended at the transport rather than at `Closed`."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__()


class WatchScreen(Screen[None]):
    """One conversation or task the window is not running, live, read-only.

    **Read-only is the design rather than a missing feature**: a subagent is
    reached by its maker and a task by its own start call, and a person looking
    over either's shoulder is a reader.  Typing into a watched conversation
    would make this a window in the sense the architecture refuses (`SDD.md`
    §4.5), and it would also be the one input on this screen whose destination
    is not the thing it appears to be.

    **Two kinds, one screen.**  A subagent's conversation and an agent task's
    one turn are both watched through the same stream and drawn by the same
    `draw`; `task` changes only the heading, the assistant's signature and the
    brief block a task's stream carries.
    """

    BINDINGS = [
        Binding("escape", "close_watch", "Close", priority=True),
    ]

    def __init__(
        self,
        name: str,
        client_factory: Callable[[], WatchClient],
        *,
        task: bool = False,
    ) -> None:
        super().__init__()
        self._name = name
        #: Whether this is an agent task's one turn rather than a subagent's
        #: conversation.  It decides the heading and the signature; the stream
        #: vocabulary is the same (`WatchItem`).  Not `_task`: `Screen` keeps a
        #: `_task` of its own and the screen's own worker is not this flag.
        self._is_task = task
        #: A factory rather than a client, so the connection is opened in the
        #: worker that will use it (and on the screen's own event loop) — the
        #: same reason `SlifeApp` creates its client in `on_mount`.
        self._client_factory = client_factory
        self._worker: Worker[None] | None = None
        #: Whether the first frame has been seen, which is how a stream that
        #: did not open with its history is said rather than drawn as if the
        #: live half were the whole conversation.
        self._snapshot_seen = False

    def compose(self) -> ComposeResult:
        # The transcript signs the assistant's messages with the watched
        # entity's name, so a subagent screen reads `scout> ...` under the
        # message that woke it, which wears its maker's bubble, `Agent(jack)>`
        # — and under a sibling's message, which wears `Subagent(jill)>`.  A
        # task's words wear the task's bubble (`task_bubble`), the same word
        # the window uses for its answer.
        header = (
            t("watch_task_header", task=self._name)
            if self._is_task
            else t("watch_header", subagent=self._name)
        )
        # `Text`, not a string: a task's name is model-chosen, and Rich markup
        # would read a `[...]` in it as a style — a name like `review [/]`
        # raised out of layout, and a benign `fix [draft]` silently lost its
        # brackets (SDD §10.2: model output is plain text).
        yield Static(Text(header), id="watch-header")
        # The task's description, as the block the person is watching.  Hidden
        # until a `TaskBrief` arrives; a subagent watch never shows it.
        yield Static("", id="watch-brief")
        # The strip the window has, under the header: the watched conversation's
        # plan is part of what somebody looking over its shoulder can see, and
        # the snapshot carries it for the same reason it carries the history.
        yield PlanView()
        yield ChatView(task_bubble(self._name) if self._is_task else self._name)

    def on_mount(self) -> None:
        self.query_one(ChatView).focus()
        self._worker = self.run_worker(self._run(), group="watch", exit_on_error=False)

    def on_unmount(self) -> None:
        if self._worker is not None:
            worker, self._worker = self._worker, None
            # Cancelling the worker drops the `watch` call, and the server sees
            # that as the detach — there is no `unwatch` to send.
            worker.cancel()

    async def _run(self) -> None:
        """Hold the watch open for as long as this screen is up."""
        client = self._client_factory()
        try:
            await client.connect()
            await client.watch(self._take)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a screen is not worth losing
            logger.warning("watching %s failed: %s", self._name, exc)
            self.post_message(WatchFailed(str(exc)))
        finally:
            # `shield`, for `slife2.plugins.a2a.server.run_turn`'s reason: a
            # cancellation arriving while the close is awaiting would pass
            # straight through it and leave the connection open.  The
            # cancellation still propagates once the close has finished.
            await asyncio.shield(client.close())

    def _take(self, item: WatchItem) -> None:
        """Take one item from the stream, from the client's callback.

        A message rather than a widget mutation: the callback runs on the MCP
        client's task, and every widget write in this project goes through the
        message pump (`SlifeApp._on_event` is the same seam).
        """
        self.post_message(WatchItemMessage(item))

    def on_watch_item_message(self, message: WatchItemMessage) -> None:
        # A frame posted before the screen went away may be handled after it.
        # There is no view left to draw into, and asking for one would raise
        # from the message pump rather than from anything the person did.
        if self.is_mounted:
            self._apply(message.item)

    def on_watch_failed(self, message: WatchFailed) -> None:
        if self.is_mounted:
            self.query_one(ChatView).add_error(t("watch_failed", err=message.reason))

    def _apply(self, item: WatchItem) -> None:
        """Draw one watch item, in the order the server sent it."""
        chat = self.query_one(ChatView)
        # The plan's live half.  The snapshot below sets the plan the watcher was
        # not there to see; a `plan_write` in the watched turn reaches the strip
        # through the same rule the window uses (`PlanView.observe`).
        self.query_one(PlanView).observe(item)
        if not self._snapshot_seen:
            self._snapshot_seen = True
            if not isinstance(item, Snapshot):
                # The protocol's first frame is the history.  One that is not
                # is a build this screen cannot read, and the live half drawn
                # alone would look like the whole conversation.
                chat.add_error(t("watch_snapshot_missing"))

        match item:
            case Snapshot(turns=turns, plan=items):
                restore(chat, turns)
                self.query_one(PlanView).set_plan(items)
            case TaskBrief(name=name, description=description, status=status):
                # **The description is the task's whole input**, and this is
                # the one screen where it is visible while the work runs.  A
                # block rather than a user bubble: nobody in the conversation
                # said it, so signing it with a sender would be a lie about who.
                self._show_brief(name, description, status)
                # **A task's start frame is its brief**, where a subagent's is
                # `TurnStarted`; without this the turn was never opened and every
                # live delta was dropped (`append_text` is gated on it).  The
                # screen showed nothing until `TurnFinished` drew the whole
                # answer, and a cancelled task showed nothing at all.
                chat.begin_assistant()
            case TurnStarted(text=text, channel=channel):
                # **The message the turn is answering**, which its caller sent
                # and a watcher never saw.  `add_user` also closes any turn left
                # open by a previous one that failed without a `TurnFinished`;
                # `begin_assistant` opens this one for the deltas below.  Settle
                # first: a new turn proves the prior one ended, and `add_user`
                # clears the tool map without closing the panels it held.  The
                # plan strip's parked writes settle at the same seam.
                chat.settle_tools()
                self.query_one(PlanView).settle()
                chat.add_user(text, channel=channel)
                chat.begin_assistant()
            case Closed(reason=reason):
                # The watched turn is gone (cancelled, failed, link dropped);
                # whatever it left running has no ending coming — panels and
                # parked plan writes alike.
                chat.settle_tools()
                self.query_one(PlanView).settle()
                chat.add_note(
                    t("watch_closed", reason=reason)
                    if reason
                    else t("watch_closed_generic"),
                    ends_turn=True,
                )
            case TurnFinished(text=text):
                # The answer is authoritative and replaces what the deltas
                # built — the reconciliation `finish_assistant` documents.
                # Every ending settles, as the main window does; on a success
                # the panels are already complete and this is a no-op.  A
                # successful plan write was popped by its own finish; what is
                # left parked is a write this turn never finished.
                chat.settle_tools()
                self.query_one(PlanView).settle()
                chat.finish_assistant(text)
            case ToolApprovalRequested():
                # A watched turn runs unattended (`watched=False`), so the loop
                # emits none.  Drawing nothing is the right answer for one that
                # arrived anyway: the person watching cannot answer it.
                return
            case ToolsCounted() | UsageReported():
                # The window's status bar is the only reader of these, and this
                # screen has none (`slife2.tui.draw` says the same on its side).
                return
            case (
                ContextChosen()
                | TextDelta()
                | ThinkingDelta()
                | ToolCallStarted()
                | ToolCallFinished()
                | MessageCutIn()
            ):
                draw(chat, item)
            case _:
                assert_never(item)

    def _show_brief(self, name: str, description: str, status: str) -> None:
        """Put the task's description above its turn.

        **Model text, drawn as `Text` rather than markup**, for `PlanView`'s
        reason: a description is whatever the model wrote, and Textual's markup
        would read a pair of brackets in it as a style.
        """
        brief = self.query_one("#watch-brief", Static)
        body = Text()
        body.append(t("task_brief_title", name=name, status=status), style="bold")
        if description:
            body.append("\n")
            body.append(description)
        brief.update(body)
        brief.add_class("visible")

    def action_close_watch(self) -> None:
        """Leave the screen — or refuse the question on it, which is what Esc
        means beside one.  The conversation keeps running without us either way.

        **A pending approval owns escape here, as it does in the window.**  The
        row's own escape binding is priority, but so is this screen's, and the
        priority pass runs app → screen → focused widget, so the key arrives
        here first.  Denying is the smaller answer and the one the row
        advertises (`n/Esc`); closing over a parked question would leave the
        turn waiting on a row that went away with the screen — the re-home
        (`slife2.tui.app._rehome_approval`) keeps the question, but the key
        meant to answer it, not to move it.
        """
        for prompt in self.query(ApprovalPrompt):
            if prompt.pending:
                prompt.action_deny()
                return
        self.dismiss(None)
