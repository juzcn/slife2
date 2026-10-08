"""The Textual application.

Four things here are worth knowing before changing them:

1. **The MCP client is created in `on_mount`, never in `__init__`.**  Its anyio
   task group has to live in the app's event loop, and `__init__` runs before
   there is one.
2. **Submissions queue, and only one turn is in flight at a time.**  The loop on
   the server would accept them concurrently and run them in order, but the
   *transcript* could not: a turn's last delta and its result travel on one
   response while the next turn's first delta travels on another, and nothing
   orders those two arrivals.  Sending one at a time makes the transcript's
   order a property of the code rather than of a race — and it also means the
   client's timeout measures a turn rather than a wait, which is what a timeout
   should measure.  What the server's own inbox is for is the *other* caller:
   any peer that sends to a busy loop is queued there and never loses its turn.
3. **Every event is stamped with the turn it belongs to.**  Textual delivers
   events as queued messages, so a delta from the turn that just finished can be
   handled after the *next* turn's block is already open — and land in it.  The
   ticket is what makes that a dropped fragment instead of a wrong transcript.
4. **`ctrl+c` cancels the running turn, never the queue.**  The message the user
   is still waiting on must not go with the one they gave up on.  With no turn
   running it quits, because a user cannot stop a runaway turn otherwise.  It is
   also the copy key — a selection in the prompt or in the transcript is copied
   and nothing is cancelled — and that first refusal is why `escape` is bound to
   the cancel alone: the interrupt that always fires, and the one that cannot
   close the window.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message as TextualMessage
from textual.worker import Worker

from slife2.config import DEFAULT_AGENT
from slife2.events import (
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnEvent,
    TurnFinished,
)
from slife2.tui import attachments
from slife2.tui.client import AgentClient, MCPAgentClient
from slife2.tui.theme import css_variables
from slife2.tui.widgets import ChatView, HistoryInput, StatusBar

logger = logging.getLogger(__name__)


class TurnEventMessage(TextualMessage):
    """A turn event, carried from the client callback into Textual's handling."""

    def __init__(self, ticket: int, event: TurnEvent) -> None:
        self.ticket = ticket
        self.event = event
        super().__init__()


class SlifeApp(App[None]):
    """The conversation window."""

    CSS_PATH = "app.tcss"

    BINDINGS = [
        # Deliberately *not* priority.  The priority pass runs before the
        # focused widget, so binding ctrl+c here with `priority=True` takes the
        # key from the two copy actions that want it first: the focused
        # `TextArea`'s ("copy the selection") and the screen's ("copy the mouse
        # selection").  Both skip themselves when there is nothing selected,
        # and that skip is what lands the key here — so ctrl+c copies when
        # there is a selection and interrupts when there is not.
        Binding("ctrl+c", "interrupt", "Cancel or quit", show=False),
        # Escape is the cancel and only the cancel; see `action_cancel`.  The
        # two keys are separate actions rather than one because escape must not
        # be able to close the window — it is the interrupt to reach for while
        # something is selected, and that has to be safe to press.
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+n", "new_conversation", "New conversation"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        url: str,
        *,
        agent: str = DEFAULT_AGENT,
        model: str = "",
        model_label: str = "",
        context_window: int = 0,
        thinking: bool = False,
        vision: bool = False,
        client_factory: Callable[[], AgentClient] | None = None,
    ) -> None:
        # `App.__init__` takes no title, so it is assigned after the fact; the
        # attribute is reactive and the header picks it up.
        super().__init__()
        # The agent label is the window title.  It changes nothing else: there is
        # no per-agent port, process or config, because the servers are shared
        # infrastructure and an identity is not a piece of topology.
        self.title = f"slife2 - {agent}"
        self._url = url
        self._agent = agent
        self._model = model
        self._model_label = model_label or model
        #: The model's budget, from its config, for the percentage in the status
        #: bar.  Zero when the config does not say, and then no percentage is
        #: shown rather than one against a number somebody invented.
        self._context_window = context_window
        #: The model's own two capabilities, also from its config: it reasons
        #: natively, and it can be shown a picture.  Both are facts about the
        #: model rather than about a turn, which is why they are read once here
        #: instead of being reported by the server per turn — the TUI is what
        #: decides `@image` is worth offering, and the server is what refuses one
        #: a model cannot read.
        self._thinking = thinking
        self._vision = vision
        #: Injectable so the TUI can be driven by a scripted client, with no
        #: server and no network.
        self._client_factory = client_factory or (
            lambda: MCPAgentClient(url, agent=agent, model=model)
        )
        self._client: AgentClient | None = None
        self._connected = False
        self._connection_text = "connecting"
        #: Prompts sent but not started, oldest first.  The user's message is
        #: added to the transcript when its turn starts rather than on submit,
        #: so that the transcript stays a faithful record of the turns that
        #: actually happened, in the order they happened.
        self._queue: list[str] = []
        self._worker: Worker[None] | None = None
        self._draining = False
        #: The running turn, as its own task so that cancelling one leaves the
        #: queue alone.  Cancelling the worker would take the queue with it.
        #: It resolves to the turn's answer, which the queue applies before the
        #: next turn opens a block.
        self._turn: asyncio.Task[str] | None = None
        #: Whether the cancellation in flight was ours.  Without it there is no
        #: way to tell "the user stopped this turn" from "the app is closing",
        #: and they want opposite handling.
        self._interrupting = False
        #: Which turn's events the transcript is currently accepting.
        self._ticket = 0
        self._context_tokens = 0
        self._steps = 0

    def compose(self) -> ComposeResult:
        # The transcript signs its messages with the agent name, so a
        # conversation reads `You> ...` / `jack> ...` on both sides.
        yield ChatView(self._agent)
        yield HistoryInput(
            # The agent's name, not the program's: the prompt is addressed to
            # whoever this instance is, and it is the one place the name is
            # read before anything has been said.
            placeholder=f"Message {self._agent}…",
            id="user-input",
            # `focus` rather than `indent`: Tab in a prompt should reach the
            # rest of the app, not insert whitespace nobody can see.
            tab_behavior="focus",
        )
        yield StatusBar(id="status")

    def get_css_variables(self) -> dict[str, str]:
        """Publish the palette to the stylesheet.

        This is what keeps the design in one place: `app.tcss` says
        `$slife-amber`, the widgets say `PALETTE["amber"]`, and neither owns a
        hex value.  slife v1 duplicates them, which is how a stylesheet and the
        code that renders into it drift apart.
        """
        return {**super().get_css_variables(), **css_variables()}

    # --- lifecycle -----------------------------------------------------------

    async def on_mount(self) -> None:
        self._client = self._client_factory()
        self.query_one(HistoryInput).focus()
        self._refresh_status()
        # Connecting can take a moment; a worker keeps the first paint prompt.
        self.run_worker(self._connect(), group="connect", exit_on_error=False)

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def _connect(self) -> bool:
        """Connect, recording the outcome.  Never raises.

        A server that is not running is an operational problem, not a fatal one:
        the app stays usable, the status bar says why, and the next prompt tries
        again.
        """
        assert self._client is not None
        try:
            await self._client.connect()
        except Exception as exc:
            self._connected = False
            self._connection_text = f"disconnected: {exc}"
            logger.warning("agent server connection failed: %s", exc)
        else:
            self._connected = True
            self._connection_text = "connected"
        self._refresh_status()
        return self._connected

    # --- the queue of turns --------------------------------------------------

    @property
    def _transcript(self) -> ChatView:
        return self.query_one(ChatView)

    async def on_history_input_submitted(self, message: HistoryInput.Submitted) -> None:
        self.query_one(HistoryInput).remember(message.text)
        self._queue.append(message.text)
        self._start_draining()
        self._refresh_status()

    def _start_draining(self) -> None:
        """Be the one worker that runs queued turns, if there is not one already.

        The flag rather than the worker's state: `run_worker` returns before the
        worker has started, so a second submission in the same tick would see a
        worker that is set and not yet running, and start a second one.
        """
        if self._draining:
            return
        self._draining = True
        self._worker = self.run_worker(self._drain(), group="turn", exit_on_error=False)

    async def _drain(self) -> None:
        """Run queued turns, one at a time, until the queue is empty."""
        try:
            while self._queue:
                # Taken off the queue *before* it runs, so the count in the
                # status bar is what is still waiting rather than including the
                # turn the user is already watching.
                prompt = self._queue.pop(0)
                self._ticket += 1
                ticket = self._ticket
                self._transcript.add_user(prompt)
                self._transcript.begin_assistant()
                self._steps = 0
                self._refresh_status()

                self._turn = asyncio.create_task(self._send(prompt, ticket))
                try:
                    final = await self._turn
                except asyncio.CancelledError:
                    if not self._interrupting:
                        raise
                    self._transcript.add_note("[cancelled]")
                except Exception as exc:
                    logger.exception("turn failed")
                    self._transcript.add_error(str(exc))
                else:
                    self._transcript.finish_assistant(final)
                finally:
                    self._interrupting = False
                    self._turn = None
                    self._refresh_status()
        finally:
            self._draining = False

    async def _send(self, prompt: str, ticket: int) -> str:
        """Run one turn and return its authoritative answer.

        The answer is returned rather than posted so the queue can apply it
        before the next turn opens a block.  Ordering against the deltas is the
        ticket's job, not the message queue's.
        """
        assert self._client is not None
        # The `@path` markers stay in the prompt: the transcript should show
        # what was sent, and taking them out would leave a sentence with a
        # hole where the attachment was named.
        images, complaints = attachments.extract(prompt)
        for complaint in complaints:
            # Complaints do not stop the prompt.  Losing what somebody typed
            # because one attachment was wrong is a worse outcome than
            # sending it without.
            self._transcript.add_note(complaint)

        if not self._connected:
            # Lazy retry: the server may have started since we launched.
            await self._connect()
        if not self._connected:
            raise ConnectionError(
                f"not connected to the agent server ({self._connection_text})"
            )

        return await self._client.run_turn(
            prompt, lambda event: self._on_event(ticket, event), images=images
        )

    def _on_event(self, ticket: int, event: TurnEvent) -> None:
        """Called by the client for every progress event.  Does not touch widgets."""
        self.post_message(TurnEventMessage(ticket, event))

    # --- message handlers (the only places widgets are mutated) --------------

    def on_turn_event_message(self, message: TurnEventMessage) -> None:
        # Late fragments of a turn that is over.  Dropping them is what keeps
        # one turn's tail out of the next turn's block — see the module
        # docstring.
        if message.ticket != self._ticket:
            return
        event = message.event
        match event:
            case TextDelta(text=text):
                self._transcript.append_text(text)
            case ThinkingDelta(text=text):
                self._transcript.append_thinking(text)
            case ToolCallStarted(call_id=call_id, name=name, arguments=arguments):
                self._transcript.add_tool_start(call_id, name, arguments)
            case ToolCallFinished(
                call_id=call_id,
                ok=ok,
                result_preview=preview_text,
                result_chars=chars,
            ):
                self._transcript.add_tool_end(call_id, ok, preview_text, chars)
            case TurnFinished(usage=usage, last_usage=last_usage, steps=steps):
                # Two numbers, two questions.  `last_usage` is how large the
                # conversation had become by the end — the only thing a context
                # percentage can be a percentage of.  `usage` is the turn's bill
                # across every model call it took, which is what the line under
                # the answer reports.  Taking the bill for the size reads as a
                # context two or three times fuller than it is on any turn that
                # called a tool, and it was what this did.
                self._context_tokens = last_usage.total_tokens
                self._steps = steps
                self._transcript.set_usage(usage.total_tokens)
                self._refresh_status()

    # --- keyboard ------------------------------------------------------------

    def _cancel_turn(self) -> bool:
        """Cancel the turn in flight, reporting whether there was one.

        The flag is set before the cancel so the drain can tell this
        cancellation from the one an exit or a reset causes; see
        `_interrupting`.
        """
        if self._turn is None or self._turn.done():
            return False
        self._interrupting = True
        self._turn.cancel()
        return True

    def action_cancel(self) -> None:
        """Stop the running turn — and nothing else, which is the point.

        Slife's rule, kept: escape is never a way out of the app, so pressing it
        by reflex at the wrong moment costs a cancelled turn at worst and never
        the window.  That is what makes it the interrupt to offer while
        something is selected, where ctrl+c is busy deciding whether to copy.
        """
        self._cancel_turn()

    def action_interrupt(self) -> None:
        """Stop the running turn, or quit when there is nothing to stop.

        Bound to ctrl+c, where the copy actions get first refusal: this runs
        only when there was no selection to copy.  Quitting is what is left
        here rather than in `action_cancel` because a user with no turn running
        and nothing selected is trying to leave.
        """
        if not self._cancel_turn():
            self.exit()

    async def action_new_conversation(self) -> None:
        """Start over: a new loop, an empty queue, an empty window.

        The queue is dropped rather than carried over — those messages were
        meant for the conversation being abandoned — and the loop is replaced,
        which is also what forgets the history: the server ends a name's old
        loop when it opens a new one for that name.

        The turn is cancelled without `_cancel_turn`, so `_interrupting` stays
        clear and the drain adds no `[cancelled]` note — this window is about to
        be emptied, and a note about the turn nobody is watching is not worth
        saying.
        """
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
        self._queue.clear()
        if self._client is not None:
            await self._client.reset()
        self._transcript.clear_all()
        self._context_tokens = 0
        self._steps = 0
        self._refresh_status()

    # --- status --------------------------------------------------------------

    def _refresh_status(self) -> None:
        """Derive the bar from what is left to do, not from a worker's state.

        Asking the worker whether it is running is wrong at exactly the moment
        the last refresh happens: it is the final statement of the worker's own
        body, so it sees `RUNNING` for a turn that has already finished, and
        nothing afterwards corrects it — the bar says working until the next
        turn, and then until the one after that.  A queue and a task are the two
        things that actually describe the situation.
        """
        self.query_one(StatusBar).update_status(
            connection=self._connection_text,
            connected=self._connected,
            agent=self._agent,
            model=self._model_label,
            busy=self._turn is not None or bool(self._queue),
            queued=len(self._queue),
            context_tokens=self._context_tokens,
            context_window=self._context_window,
            thinking=self._thinking,
            vision=self._vision,
            steps=self._steps,
        )
