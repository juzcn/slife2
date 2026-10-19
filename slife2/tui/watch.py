"""The read-only screen that shows one conversation the window is not running.

**A screen rather than a second transcript in the window**, because the thing
being watched is somebody else's conversation and the window underneath is still
*this* one: the main transcript keeps receiving its turn's events while this is
up, and closing the screen returns to it with nothing lost.  A view swapped into
the one transcript would have to stop drawing the window's turn to do that.

**The same widgets the window draws with.**  The history goes through
`slife2.tui.restore.restore`, the live events through `slife2.tui.draw.draw` — the
function the window itself calls — so a watched turn and a typed turn cannot
come to look different.  What is missing is the half that is the window's: there
is no prompt (a subagent has no window, and this is not one), no approval row
(a watched turn runs unattended by design), and no status bar (the bar describes
the window's conversation, not this one).

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

from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message as TextualMessage
from textual.screen import Screen
from textual.widgets import Static
from textual.worker import Worker

from slife2.libs.turn.events import (
    ContextChosen,
    MessageCutIn,
    TextDelta,
    ThinkingDelta,
    ToolApprovalRequested,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)
from slife2.libs.turn.watch import Closed, Snapshot, TurnStarted, WatchItem
from slife2.tui.client import WatchClient
from slife2.tui.draw import draw
from slife2.tui.i18n import t
from slife2.tui.restore import restore
from slife2.tui.widgets import ChatView

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
    """One subagent's conversation, live, with no way to talk to it.

    That last part is the design rather than a missing feature: a subagent is
    reached by its maker, and a person looking over its shoulder is a reader.
    Typing into a watched conversation would make this a window in the sense the
    architecture refuses (`SDD.md` §4.5), and it would also be the one input on
    this screen whose destination is not the thing it appears to be.
    """

    BINDINGS = [
        Binding("escape", "close_watch", "Close", priority=True),
    ]

    def __init__(
        self,
        subagent: str,
        client_factory: Callable[[], WatchClient],
    ) -> None:
        super().__init__()
        self._subagent = subagent
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
        # The transcript signs the assistant's messages with the subagent's
        # name, so the screen reads `scout> ...` under the message that woke it,
        # which wears its maker's bubble, `Agent(jack)>` — and under a sibling's
        # message, which wears `Subagent(jill)>`.  The sender's kind is in the
        # channel, so this screen does not decide it (`channel_prefix`).
        yield Static(t("watch_header", subagent=self._subagent), id="watch-header")
        yield ChatView(self._subagent)

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
            logger.warning("watching %s failed: %s", self._subagent, exc)
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
        if not self._snapshot_seen:
            self._snapshot_seen = True
            if not isinstance(item, Snapshot):
                # The protocol's first frame is the history.  One that is not
                # is a build this screen cannot read, and the live half drawn
                # alone would look like the whole conversation.
                chat.add_error(t("watch_snapshot_missing"))

        match item:
            case Snapshot(turns=turns):
                restore(chat, turns)
            case TurnStarted(text=text, channel=channel):
                # **The message the turn is answering**, which its caller sent
                # and a watcher never saw.  `add_user` also closes any turn left
                # open by a previous one that failed without a `TurnFinished`;
                # `begin_assistant` opens this one for the deltas below.
                chat.add_user(text, channel=channel)
                chat.begin_assistant()
            case Closed(reason=reason):
                chat.add_note(
                    t("watch_closed", reason=reason)
                    if reason
                    else t("watch_closed_generic"),
                    ends_turn=True,
                )
            case TurnFinished(text=text):
                # The answer is authoritative and replaces what the deltas
                # built — the reconciliation `finish_assistant` documents.
                chat.finish_assistant(text)
            case ToolApprovalRequested():
                # A watched turn runs unattended (`watched=False`), so the loop
                # emits none.  Drawing nothing is the right answer for one that
                # arrived anyway: the person watching cannot answer it.
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

    def action_close_watch(self) -> None:
        """Leave the screen.  The conversation keeps running without us."""
        self.dismiss(None)
