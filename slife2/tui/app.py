"""The Textual application.

Three things here are worth knowing before changing them:

1. **The MCP client is created in `on_mount`, never in `__init__`.**  Its anyio
   task group has to live in the app's event loop, and `__init__` runs before
   there is one.
2. **Every widget mutation happens in a message handler.**  The progress
   callback, the turn's result, and its failure are all posted as messages
   rather than applied where they happen.  That is not just tidiness: the
   result must be applied *after* the deltas that preceded it, and posting it
   puts it in the same queue, in order.  Applying it directly from the worker
   would let it overtake deltas that had been posted but not yet handled — the
   final answer would land first and the fragments would appear beneath it.
   Routing through messages also means widget updates happen inside Textual's
   handling context, which keeps `refresh()` batching deterministic and stops
   pilot-driven tests racing, and it keeps `client.py` free of UI concerns.
3. **`ctrl+c` cancels a running turn, and quits when there is none.**  Leaving
   it as quit-only would mean a user cannot stop a runaway turn, which is the
   most common thing anyone wants to do.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message as TextualMessage
from textual.worker import Worker, WorkerState

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

    def __init__(self, event: TurnEvent) -> None:
        self.event = event
        super().__init__()


class TurnDoneMessage(TextualMessage):
    """The turn's authoritative answer.

    Posted rather than applied, so it cannot overtake the deltas that came
    before it — see the module docstring.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()


class TurnFailedMessage(TextualMessage):
    """The turn raised.  Posted for the same ordering reason."""

    def __init__(self, error: str) -> None:
        self.error = error
        super().__init__()


class SlifeApp(App[None]):
    """The conversation window."""

    CSS_PATH = "app.tcss"

    BINDINGS = [
        # priority: TextArea binds ctrl+c to *copy*, and a terminal that cannot
        # stop a runaway turn is worse than one that cannot copy from the input.
        Binding("ctrl+c", "interrupt", "Cancel or quit", priority=True, show=False),
        Binding("escape", "interrupt", "Cancel", show=False),
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
        #: Injectable so the TUI can be driven by a scripted client, with no
        #: server and no network.
        self._client_factory = client_factory or (
            lambda: MCPAgentClient(url, agent=agent, model=model)
        )
        self._client: AgentClient | None = None
        self._connected = False
        self._connection_text = "connecting"
        self._turn_worker: Worker[None] | None = None
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

    # --- running a turn ------------------------------------------------------

    @property
    def _transcript(self) -> ChatView:
        return self.query_one(ChatView)

    async def on_history_input_submitted(self, message: HistoryInput.Submitted) -> None:
        self.query_one(HistoryInput).remember(message.text)
        self._transcript.add_user(message.text)
        self._transcript.begin_assistant()
        self._steps = 0
        self._refresh_status(busy=True)
        # exclusive=True: a second submit cancels the first, which is what a
        # user pressing Enter again means.
        self._turn_worker = self.run_worker(
            self._run_turn(message.text),
            group="turn",
            exclusive=True,
            exit_on_error=False,
        )

    async def _run_turn(self, prompt: str) -> None:
        assert self._client is not None
        try:
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
                self.post_message(
                    TurnFailedMessage(
                        f"not connected to the agent server ({self._connection_text})"
                    )
                )
                return

            final = await self._client.run_turn(prompt, self._on_event, images=images)
            self.post_message(TurnDoneMessage(final))
        except asyncio.CancelledError:
            # `action_interrupt` has already noted it in the transcript; all
            # this needs to do is not turn a deliberate cancellation into an
            # error message.
            raise
        except Exception as exc:
            logger.exception("turn failed")
            self.post_message(TurnFailedMessage(str(exc)))
        finally:
            self._refresh_status()

    def _on_event(self, event: TurnEvent) -> None:
        """Called by the client for every progress event.  Does not touch widgets."""
        self.post_message(TurnEventMessage(event))

    # --- message handlers (the only places widgets are mutated) --------------

    def on_turn_event_message(self, message: TurnEventMessage) -> None:
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
            case TurnFinished(usage=usage, steps=steps):
                # Replaced, not accumulated: this is how big the conversation
                # is now, which is the only thing a context percentage can be a
                # percentage of.
                self._context_tokens = usage.total_tokens
                self._steps = steps
                self._transcript.set_usage(usage.total_tokens)
                self._refresh_status()

    def on_turn_done_message(self, message: TurnDoneMessage) -> None:
        self._transcript.finish_assistant(message.text)

    def on_turn_failed_message(self, message: TurnFailedMessage) -> None:
        self._transcript.add_error(message.error)

    # --- keyboard ------------------------------------------------------------

    def action_interrupt(self) -> None:
        """Stop a running turn, or quit when there is nothing to stop."""
        worker = self._turn_worker
        if worker is not None and worker.state is WorkerState.RUNNING:
            worker.cancel()
            self._transcript.add_note("[cancelled]")
            self._refresh_status()
        else:
            self.exit()

    def action_new_conversation(self) -> None:
        """Start over: forget the history and empty the window.

        Cheap, because the server keeps no conversation to clear — see
        `slife2.tui.client.MCPAgentClient`.
        """
        if (
            self._turn_worker is not None
            and self._turn_worker.state is WorkerState.RUNNING
        ):
            self._turn_worker.cancel()
        if self._client is not None:
            self._client.reset()
        self._transcript.clear_all()
        self._context_tokens = 0
        self._steps = 0
        self._refresh_status()

    # --- status --------------------------------------------------------------

    def _refresh_status(self, *, busy: bool = False) -> None:
        worker = self._turn_worker
        if worker is not None and worker.state is WorkerState.RUNNING:
            busy = True
        self.query_one(StatusBar).update_status(
            connection=self._connection_text,
            connected=self._connected,
            agent=self._agent,
            model=self._model_label,
            busy=busy,
            context_tokens=self._context_tokens,
            context_window=self._context_window,
            steps=self._steps,
        )
