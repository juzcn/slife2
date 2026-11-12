"""Central registry for every time value in slife2.

All time values live HERE as typed dataclass defaults — this module is the single
source of truth.  There is no config section for them and no second seat: a
developer tunes a value by editing the dataclass default and its comment, and a
structurally invalid edit fails loudly at import (:func:`validate`).

Two kinds of value share the file, because one file answering "what is this
number and who owns it" beats two:

- **budgets** (``work`` / ``ready`` / ``grace`` / ``transport`` / ``storage`` /
  ``deliver``) bound a single await — **the owner-of-await rule**: the layer that
  awaits owns the bound, and a callee never sets a total for its caller.
- **cadences** (``pacing``) are how OFTEN something runs — a poll period, a
  heartbeat, a backoff step, a session lifetime.  A cadence never bounds an await
  and a budget never sets a cadence; the role keeps that visible.

**A retry policy is one shape too.**  :class:`Retry` is the schedule a run of
failed attempts earns; :class:`Backoff` is one loop's run under it.  The rule
both channels share is :meth:`Backoff.succeeded`: **an attempt that did not
fail is the only reset** — not a reconnect, not a new message, not a clock.
Two private backoffs that reset on different events pace one failing loop
twice as fast as the other and hide how long either has been failing.

CONVENTION (enforced by ``tests/test_no_magic_timeouts.py``):

- consumers do **call-time lookups** — ``_T.timeouts.<role>.<key>`` — and NEVER
  import-capture values into module constants or def-time default args, so tests
  can monkeypatch ``slife2.helpers.timeouts.timeouts.<role>.<key>`` at runtime;
- a value that has to stay a literal carries ``# timeouts:`` on the line, and
  the gate's docstring says which literals are exempt outright.

**What is deliberately absent, and why.**
There is **no tool-call budget here**, on the argument that a single number
applied to every tool is a number wrong for most of them — a search and a
twenty-minute render are both tools.  The bound a call has is the one an
interested party stated: the model's ``_timeout``, or the tool's own declared
parameter, and otherwise none (``slife2.libs.turn.loop``).

**Every value here is a tuned default, and its comment says what it is for.**
``timeouts.py`` intentionally imports nothing from ``slife2.*``
(``config.py`` imports us; a cycle would break both).  stdlib only.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, fields


class TimeoutConfigError(ValueError):
    """A registry value violates a type / non-negativity / invariant rule."""


@dataclass
class Retry:
    """One retry policy: the pause a run of failed attempts earns.

    ``base`` is the pause before the first retry, ``factor`` grows it per
    consecutive failure, and ``cap`` is the ceiling; ``jitter`` is the fraction
    of the step that may be added or subtracted (0.2 = ±20 %).

    The numbers are registry values like every other cadence — a policy is the
    *shape* that keeps two channels' schedules comparable, not a second seat for
    either one's values.
    """

    base: float
    factor: float
    cap: float
    jitter: float = 0.0

    def delay(self, failures: int) -> float:
        """The pause after *failures* consecutive failed attempts.

        ``failures`` counts from 1: the first retry waits ``base``, the second
        ``base * factor``, and so on up to ``cap``.  A count of 0 is not "no
        failures yet" at a caller that means to retry — it is a caller that did
        not read this contract, and it fails rather than waiting a fraction of
        the base step that nobody chose.
        """
        if failures < 1:
            raise ValueError("failures counts consecutive failed attempts from 1")
        step = min(self.base * self.factor ** (failures - 1), self.cap)
        if self.jitter:
            step += step * self.jitter * (2 * random.random() - 1)
        return step


class Backoff:
    """One loop's run of failures under a :class:`Retry` policy.

    **An attempt that did not fail is the only thing that resets the run.**  Not
    a reconnect, not a new message, not a clock: a run cleared by anything else
    paces a failing loop at its base cadence again and makes a long failure look
    like a fresh one.  So an attempt has exactly two answers —
    :meth:`failed` (counter, pause) and :meth:`succeeded` (clear) — and their
    effect on the schedule is stated here, once, for every caller.
    """

    def __init__(self, policy: Retry) -> None:
        self.policy = policy
        self.failures = 0

    def failed(self) -> float:
        """Count one failed attempt and answer the pause before the next."""
        self.failures += 1
        return self.policy.delay(self.failures)

    def succeeded(self) -> None:
        """Clear the run — the attempt this loop retries has succeeded."""
        self.failures = 0


# ── Role dataclasses — the values themselves (single source of truth) ──


@dataclass
class Work:
    """One await a layer does on its own behalf."""

    #: Inactivity watchdog on a streamed model answer: seconds of silence before
    #: the call is declared stalled.  **Not a total** — reset by every chunk, so
    #: a slow-but-live generation is never cut and a provider that answered 200
    #: and went quiet is (`slife2.clients.model`).
    stall: float = 120.0
    #: One call to the context plugin.  Long enough for `restore` (a whole
    #: context replayed) and `rebuild` (a recall that may embed a query), and
    #: for `remember` to embed and write a long turn: a save that outlasts it
    #: rolls the whole turn back as recorded-nowhere.
    context: float = 300.0
    #: One *ask* of the toolhub by this process — the tool list, and the trim at
    #: a turn boundary.  **Not a tool call**: that is the loop's, and nothing
    #: here bounds it (see the module docstring).  Must outlast the hub's own
    #: settle wait (`validate`).
    hub: float = 20.0
    #: One *catalogue build* at start-up: the first ask that makes a home's
    #: `agents/tools.db` (`slife2.libs.host.launcher.open_catalogue`).  **Longer
    #: than `hub` because it is the cold path**: a hub that has just started
    #: must reach every plugin, ask each family for its declaration, and embed
    #: the rows that are new — on a machine that may still be starting nineteen
    #: servers, and with external sources that can take minutes to declare.  A
    #: turn's ask stays `hub`; this budget is spent once, and a start-up that
    #: gives up on it leaves the window with no catalogue at all.  `validate`
    #: keeps it >= `hub`.
    catalogue: float = 360.0
    #: One management call from the terminal to the agent server: `model_switch`,
    #: `reset`, `transcript`.  A question answered from memory or from the store.
    #: **Not the turn** — a turn's length is the answer's own business, and the
    #: person watching has Esc (`slife2.tui.client`).
    management: float = 30.0
    #: The pre-turn recall discriminator: how long the model it asks may take
    #: before the turn runs on the context it already has.  `config.py` reads
    #: this as the default of `context.timeout` — the one seat for the number,
    #: with the config as an override rather than a second copy.
    recall_discriminator: float = 20.0
    #: The per-request clock on the *model client* a plugin makes its own calls
    #: with (the context plugin's discriminator).  Larger than the bound its
    #: caller puts around it, deliberately: the caller's is the operative one,
    #: and this exists so a hung provider cannot park the plugin.
    model_call: float = 600.0


@dataclass
class Ready:
    """Establishing a peer, and one-off probes of one."""

    #: How long a plugin may take to come up.  Generous: the launcher refuses to
    #: start at all if one will not, so a short bound here would be a system that
    #: refuses to boot on a slow machine — a cold `schedule` (the last server up,
    #: after the hub's catalogue) took ~90 s in a fresh task container, and its
    #: port did answer, so that is a cold start and not a hang.
    plugin_start: float = 300.0
    #: How long one readiness probe may take — a TCP connect, a `ps`, or a
    #: server's own identity answer.  Short: the probe answers, it does not work.
    probe: float = 2.0
    #: How long a second `slife2` waits on the claim file before deciding the
    #: running process is not using it.  Short: it is a mutex, not a wait.
    agent_claim: float = 0.2
    #: Waiting on a child that has been asked to stop.
    reap: float = 5.0
    #: One of *our own* hops connecting.  Loopback to a daemon this system
    #: started, so this bounds a port that listens and never answers.
    connect_ours: float = 10.0
    #: A `mcp:` entry connecting.  Somebody else's process, possibly an `npx`
    #: fetching itself, so it gets far more patience than our own hops.  **180 s,
    #: not 60**: a cold `@aryanbv/pdf-toolkit-mcp` install took 138 s, and a
    #: shorter bound kills it mid-install, leaving a half-written npx cache whose
    #: missing files then fail every later start.  A connect that is killed while
    #: somebody else's package manager is writing is worse than one given time to
    #: finish.
    connect_tools: float = 180.0
    #: How long a listing waits for connects it started, before answering with
    #: what is in hand.  "Not answering yet" and "absent" are the same answer.
    list_settle: float = 5.0
    #: The a2a broker coming up.
    broker: float = 10.0
    #: One probe of an a2a peer, local and remote.
    probe_local: float = 0.25
    probe_remote: float = 5.0
    #: One probe of a tunnel child's own metrics endpoint.
    probe_endpoint: float = 5.0
    #: A file tunnel publishing, and one read of the URL it published.
    tunnel_start: float = 45.0
    tunnel_read_url: float = 30.0
    #: How long a published tunnel may stay unreachable from the edge before the
    #: monitor respawns it.  **Must outlast `tunnel_read_url`** (`validate`): a
    #: child gets at least the patience a fresh start gets, or the monitor would
    #: respawn one that was never given time to heal.
    tunnel_heal: float = 180.0
    #: The WeChat login's QR flow, from code shown to code scanned.
    qr_deadline: float = 600.0


@dataclass
class Grace:
    """Asking something to stop, and then making it."""

    #: After a stop request, how long a process has to go quietly before the
    #: force is used.  Blunt on purpose: these are our own daemons, they have no
    #: console to catch a signal and no state worth flushing.
    kill: float = 5.0
    #: How long a daemon's exit waits for work it detached — a write that is
    #: already in flight.  Bounded, because a store that has stopped answering
    #: must not hold up a shutdown.
    drain: float = 5.0
    #: How long one attempt at a lock waits before giving up.
    lock: float = 30.0
    #: How long `subagent_remove` waits for the target conversation's turn to
    #: release its lock before refusing.  Short: a turn parked on an approval
    #: is one to come back to, not to wait out inside a model's tool call.
    turn: float = 2.0


@dataclass
class Transport:
    """Per-request clocks on somebody else's HTTP API."""

    #: One media request, from connect to last byte.  Generous: a synchronous
    #: render is a long POST.
    media_request: float = 180.0
    media_connect: float = 30.0
    #: Downloading the artifact a provider hands back.
    media_download: float = 300.0
    #: How long an asynchronous media job may take before the *wait* gives up.
    #: The job keeps running on the provider's side — the message says so.
    media_deadline: float = 1200.0
    #: One embedding request, and the same one through the API server.
    embed: float = 60.0
    embed_api: float = 30.0
    #: One cross-encoder rerank request.  A single synchronous scoring pass over
    #: the candidates, so it is shorter than an embedding batch and longer than
    #: a chat stream's first byte.
    rerank: float = 30.0
    #: One request to the WeChat backend.
    wechat_request: float = 120.0
    #: One of the WeChat calls that is not the long poll: `getconfig` and
    #: `sendtyping` answer at once, so a hung one must not hold a turn's typing
    #: keep-alive for the poll's whole budget.
    wechat_config: float = 10.0
    #: Handing a message to a window: it returns when the words are down the
    #: stream, not when the turn they started ends.
    window: float = 10.0
    #: One call to the schedule plugin.
    tasks: float = 30.0
    #: Fetching one URL into the cabinet (`file_save`), connect to last byte.
    #: The time half of the fetch's bound; `MAX_FETCH_BYTES` is the byte half.
    file_fetch: float = 30.0


@dataclass
class Storage:
    """Waiting on a local store rather than on a peer."""

    #: SQLite's own busy timeout: how long a writer waits for the writer holding
    #: the database (seconds here; the stores convert to the PRAGMA's ms).
    sqlite_busy: float = 5.0
    #: Waiting for a config file's write lock.
    filelock: float = 30.0


@dataclass
class Stream:
    """Retries, which are counts and not durations — here for the same reason
    as the durations: one file answers "what is this number", whatever it is."""

    #: The embeddings server's SDK-level retries.  Zero: an embedding that
    #: failed is a caller's problem to retry, not the server's to repeat — a
    #: request that reached the provider may have been billed, and a 429 is the
    #: caller's to pace.  A connection that never opened is the one exception,
    #: and the embeddings server tries that again itself
    #: (`slife2.plugins.llm.embeddings_server.EmbeddingClient.embed`).
    retries: int = 0
    #: Handing a message to a window (`slife2.libs.harness.handover`): how many
    #: times it is offered before the offer is a failure.  Three, because what
    #: the retry is for is the window's own gap — an agent's window attaches a
    #: moment after its stack comes up — and the pause between attempts is
    #: `pacing.reconnect`, which is the terminal's and not this count's.
    #:
    #: **A count, so it is here rather than in `pacing`**: the roles are the
    #: registry's own rule, and a number that says "how many" is not a cadence
    #: however close to one it sits.
    handover: int = 3


@dataclass
class Deliver:
    """Handing something to a peer over a channel that can drop it."""

    #: The a2a mesh: connecting to the broker, and the pieces of its own wire
    #: contract — a peer's card is read in this long, a first reply is waited
    #: for this long, and silence past this long means the link is dead.
    mqtt_connect: float = 15.0
    name_probe: float = 1.0
    reply_first: float = 15.0
    keepalive: float = 25.0
    #: The gap allowed between two frames of one task reply.  The profile's
    #: default is 30 s; the responder's keepalive is 25 s, so a live task still
    #: answers inside this window.  Past it the requester asks ``GetTask``
    #: rather than re-publishing the request.
    stream_idle: float = 30.0
    #: One probe's whole task budget: a headless window may run the task for as
    #: long as its turns take, and the probe waits for its `task_response`.
    probe: float = 3600.0


@dataclass
class Pacing:
    """Cadences — how often something runs, never how long an await may block.

    Every value here is a period between two events (a poll, a heartbeat, a
    backoff step) or a lifetime after which a cached thing is stale.
    """

    #: The probe's poll of an agent task's state, and then of the conversation
    #: the task's answer is delivered to.
    probe_poll: float = 0.5
    #: The WeChat poll's retry policy.  `base` is the pause between polls — the
    #: call itself blocks until the bridge has something to say, so it is the
    #: pause *after* an answer, not the latency — and a link that keeps failing
    #: grows by `factor` to `cap`: it must not be re-polled every three seconds
    #: for ever.  A poll that answers clears the run, so a recovered link is
    #: back on the base cadence at once.
    wechat_retry: Retry = field(
        default_factory=lambda: Retry(base=3.0, factor=1.5, cap=30.0)
    )
    #: How long one WeChat session may live before it is logged in again.
    wechat_session_max_age: float = 82800.0  # 23 h
    #: The QR status check while a login waits for a scan.
    qr_poll: float = 2.0
    #: The typing indicator: how often it is refreshed, and how long the
    #: keepalive may run before it gives up on the agent ever answering.  Five
    #: seconds is the official client's keep-alive cadence, and the indicator
    #: expires sooner than a longer refresh would notice.
    typing_refresh: float = 5.0
    typing_max_lifetime: float = 300.0
    #: How long one peer's `typing_ticket` is reused before `getconfig` mints
    #: another.  The ticket lives about a day; refreshing inside that keeps a
    #: process that outlives one login from presenting a retired one.
    typing_ticket_ttl: float = 43200.0  # 12 h
    #: A message that repeats within this is the same message — the WeChat
    #: backend redelivers, and answering twice is worse than answering once.
    wechat_dedup: float = 30.0
    #: The due-task poll.  Cron's unit is a minute, so this fires within half of
    #: one of the due time.
    schedule_poll: float = 30.0
    #: A fire whose due time is within this of "now" is freshly due; older, it
    #: was missed while slife2 was down.
    miss_grace: float = 120.0
    #: How often a tunnel child is checked for life, and the delay before a
    #: spawn that failed is tried again.
    tunnel_health: float = 30.0
    tunnel_retry_delay: float = 2.0
    #: The media plugin's poll of an asynchronous provider job.  The providers'
    #: own examples use fifteen seconds.
    media_poll: float = 15.0
    #: Re-checking a file another process still holds open, while something waits
    #: for it to let go (`slife2.plugins.agent.server._remove_tree`, where a
    #: subagent's directory is deleted).  Short, because the holder is a write
    #: that has been committed and not yet flushed — milliseconds — and the wait
    #: around this is `storage.sqlite_busy`, which is the budget for a file
    #: somebody else has.
    file_release: float = 0.2
    #: The a2a mesh's own poll and its retry delay, and the broker's startup
    #: poll.
    broker_poll: float = 0.1
    mesh_retry_delay: float = 1.0
    #: The a2a requester ladder's retry policy — the profile's standard
    #: defaults: 1 s, 2 s, 4 s between attempts, ±20 % jitter.  The attempt
    #: count is the profile's own (`plugins/a2a/mesh.py`), and a first reply
    #: ends the ladder rather than resetting it: every rung it runs was earned
    #: by a failed attempt.
    requester_retry: Retry = field(
        default_factory=lambda: Retry(base=1.0, factor=2.0, cap=4.0, jitter=0.2)
    )
    #: How long an agent may stay `online` after its window's `join` ended.  A
    #: join ends for two reasons — the process is exiting, or the stream dropped
    #: and the window retries it — and only the first is a departure.  Longer
    #: than `reconnect`, because the retry also has to reconnect and re-join
    #: before it holds again.
    mesh_departure: float = 5.0
    #: How long a terminal inbound task is kept for retry dedup, and how long
    #: an interrupted one is kept waiting for a continuation.  The profile's
    #: SDK uses 300 s for completed dedup; an interrupted task can wait on a
    #: model or a person, so the one window covers both and the store's cap is
    #: the real memory bound.
    a2a_task_ttl: float = 1800.0
    #: The terminal's pause before offering to attach again.
    reconnect: float = 2.0
    #: Between two attempts at a local lock.
    lock_poll: float = 0.1
    #: How long a tool server's listing is trusted without being read again.  A
    #: policy number rather than a measurement: a session is hours long and a
    #: server's tool set changes about as often as it is restarted.
    relist_after: float = 300.0
    #: How long a conversation survives with nothing asked of it, before the
    #: agent server lets its live list go.  Housekeeping, not a lifetime a caller
    #: can observe: the next message under the same key starts it again.
    loop_idle: float = 1800.0


@dataclass
class Timeouts:
    work: Work = field(default_factory=Work)
    ready: Ready = field(default_factory=Ready)
    grace: Grace = field(default_factory=Grace)
    transport: Transport = field(default_factory=Transport)
    storage: Storage = field(default_factory=Storage)
    stream: Stream = field(default_factory=Stream)
    deliver: Deliver = field(default_factory=Deliver)
    pacing: Pacing = field(default_factory=Pacing)


# ── Validation ──────────────────────────────────────────────────────────


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _retry_errors(path: str, policy: Retry) -> list[str]:
    """The violations in one retry policy, named by their registry path."""

    def usable(v: float | int) -> bool:
        return _is_number(v) and math.isfinite(v) and v > 0

    errs: list[str] = []
    for name in ("base", "factor", "cap"):
        value = getattr(policy, name)
        if not usable(value):
            errs.append(f"{path}.{name} must be finite and > 0, got {value!r}")
    jitter = policy.jitter
    if not (_is_number(jitter) and math.isfinite(jitter) and 0.0 <= jitter < 1.0):
        errs.append(f"{path}.jitter must be in [0, 1), got {jitter!r}")
    if usable(policy.base) and usable(policy.factor) and usable(policy.cap):
        if policy.factor < 1.0:
            errs.append(
                f"{path}.factor must be >= 1 (a backoff that cannot grow is not one)"
            )
        if policy.cap < policy.base:
            errs.append(f"{path}.cap must be >= {path}.base")
    return errs


def validate(ts: Timeouts) -> list[str]:
    """Return a list of human-readable violations (empty when valid).

    **The invariants are the point of this function**, and each one is a claim
    the code makes about two clocks rather than about one.  A value nobody
    relates to another is checked for being a sane number and left at that: what
    the registry can assert is the relations, because those are the ones that go
    wrong silently — an outer bound shorter than an inner one is an inner bound
    that never fires, and a timer that fires while the thing it bounds is still
    legitimately running.
    """
    errs: list[str] = []
    for role, obj in (
        ("work", ts.work),
        ("ready", ts.ready),
        ("grace", ts.grace),
        ("transport", ts.transport),
        ("storage", ts.storage),
        ("stream", ts.stream),
        ("deliver", ts.deliver),
        ("pacing", ts.pacing),
    ):
        for f in fields(obj):
            v = getattr(obj, f.name)
            if isinstance(v, Retry):
                errs.extend(_retry_errors(f"timeouts.{role}.{f.name}", v))
                continue
            if f.name == "retries":
                if not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
                    errs.append(
                        f"timeouts.{role}.{f.name} must be a non-negative int, got {v!r}"
                    )
                continue
            if not _is_number(v):
                errs.append(
                    f"timeouts.{role}.{f.name} must be a number, got {type(v).__name__}"
                )
                continue
            if v <= 0 or not math.isfinite(v):
                errs.append(
                    f"timeouts.{role}.{f.name} must be finite and > 0, got {v!r}"
                )
    if ts.work.hub < ts.ready.list_settle:
        # The agent's patience for the hub has to outlast the hub's own brief
        # wait for tool servers that are still connecting, or "waiting for a
        # server that is starting" arrives as a failed turn.
        errs.append(
            "invariant: work.hub >= ready.list_settle (a listing's wait is "
            "inside the asker's patience)"
        )
    if ts.work.catalogue < ts.work.hub:
        # The cold build is the same ask as a turn's, with the starting and
        # embedding on top; a shorter budget would fail the start first.
        errs.append(
            "invariant: work.catalogue >= work.hub (a catalogue build is a "
            "hub ask with the cold work in it)"
        )
    if ts.ready.tunnel_heal < ts.ready.tunnel_read_url:
        # A running child gets at least the patience a fresh start gets, or the
        # monitor would respawn one that was never given time to heal.
        errs.append("invariant: ready.tunnel_heal >= ready.tunnel_read_url")
    if ts.ready.plugin_start < ts.ready.probe:
        errs.append(
            "invariant: ready.plugin_start >= ready.probe (a start is longer "
            "than a look)"
        )
    if ts.ready.connect_tools < ts.ready.connect_ours:
        errs.append(
            "invariant: ready.connect_tools >= ready.connect_ours (somebody "
            "else's process gets at least what ours gets)"
        )
    if ts.transport.media_request > ts.transport.media_deadline:
        # The *wait* for an asynchronous job is longer than one synchronous
        # request by design — this guards the reverse of that claim.
        errs.append("invariant: transport.media_request <= transport.media_deadline")
    if ts.pacing.typing_max_lifetime < ts.pacing.typing_refresh:
        errs.append("invariant: pacing.typing_max_lifetime >= pacing.typing_refresh")
    return errs


def checked(ts: Timeouts) -> Timeouts:
    """Validate *ts* and raise :class:`TimeoutConfigError` on any violation.

    The raising counterpart of :func:`validate` — used at import for the
    singleton, and by tests asserting that a bad edit fails loudly.
    """
    errs = validate(ts)
    if errs:
        raise TimeoutConfigError("; ".join(errs))
    return ts


#: The values every caller reads.  A structurally invalid edit fails at import,
#: which is the only moment a wrong number can be caught before it is used.
timeouts = checked(Timeouts())
