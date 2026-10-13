"""A plugin that holds somebody else's MCP servers, and declares them.

**This is where a connection lives now.**  Until this module existed, the toolhub
opened one link per configured entry, merged what each offered, and was therefore
the only process that could reach a server.  The hub is the tool *set's* owner —
which tools exist, what the model is holding, what the budget takes back — and
holding twenty connections is not that job; it is the job of the process that
owns the config section they were written in.  So `mcp:` and `rest-api:` each
have a plugin, and this is the half of both that is the same code.

**A family holds, and the hub records.**  What this class does is connect to each
entry, name its tools the way the model will call them, and answer `list_sources`
with the whole of what it holds — and `call_source` when the hub wants one run.
It does not touch the catalogue: the merge, the injectable gate, the budget and
the naming rule are the hub's, and a family that wrote rows would be a second
writer of the tool table.

**The two facts a source has are its own switch and its own health.**  `enabled`
is the operator's (`enabled: false` in the section); `up` is whether the link is
answering.  They are different, and a source that is one and not the other is
still declared — with no rows, because a plugin cannot list what it cannot reach,
and the hub marks the state rather than merging an empty list that would purge
what the previous run left behind.

**A declaration is a statement about a listing already taken**, which is why this
keeps each entry's rows: the hub asks at a moment of its choosing and may not be
made to wait for a cold `npx`, so the family answers with what it last saw and
re-lists on its own time.  That is the one copy of a tool list outside the
catalogue, and it exists because the question "what do you hold" has to have an
answer that does not block.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

from fastmcp import Context, FastMCP

import slife2.timeouts as _T
from slife2 import configfile
from slife2.audience import forwarded_client, request_meta
from slife2.config import Config, ConfigError, ToolServerSettings
from slife2.gateway import ClientFactory, Connection, mcp_config, proxied_name
from slife2.mcp_server import (
    CALL_SOURCE,
    CLI_SOURCE,
    LIST_SOURCES,
    SKILLS_SOURCE,
    house_server,
)
from slife2.paths import data_dir

logger = logging.getLogger(__name__)

#: How many of one server's tools a `*_list_tools` prints.  v1's number and v1's
#: reason: a published server can carry four figures of tools (github: 1239),
#: and printing them all spends the model's context on names it never asked for.
#: The read is uncapped — the catalogue needs every tool.
TOOL_LIST_LIMIT = 20


class Held:
    """One configured entry: its link, and the rows it last offered.

    The rows are built from the listing the gateway hands over — the entry's name
    in front of each tool's own (`proxied_name`) and the schema the far end
    declared — because that is what the hub merges, and building them here is
    what lets a family answer "what do you hold" without asking anybody.
    """

    def __init__(
        self,
        settings: ToolServerSettings,
        *,
        directory: str,
        category: str,
        transport: Callable[[ToolServerSettings], Any] | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.settings = settings
        self.category = category
        #: `None` until it has answered once, and again while it is re-listing:
        #: "no rows" and "no rows *yet*" are the same thing to the hub, which is
        #: told `up: false` either way and marks the source rather than merging.
        self.rows: list[dict[str, Any]] | None = None
        self.connection = Connection(
            settings,
            transport=transport or (lambda one: mcp_config(one, cwd=directory)),
            client_factory=client_factory,
            on_listed=self._listed,
            on_failed=self._failed,
        )

    async def _listed(self, tools: list[Any]) -> None:
        self.rows = [self._row(tool) for tool in tools]
        logger.info("%s: %d tool(s) held", self.settings.name, len(self.rows))

    def _failed(self, exc: Exception) -> None:
        # Nothing to write: a family holds no catalogue, and the hub learns this
        # from the next declaration.  It is logged because a server that will not
        # start is otherwise only visible as tools that are not there.
        logger.warning("%s is not reachable: %s", self.settings.name, exc)

    def _row(self, tool: Any) -> dict[str, Any]:
        """One tool of this entry, as the catalogue's row for it.

        **No `status`, which is the whole of how this family differs from the two
        documents.**  An entry here has a *connection*, so whether its tools are
        usable is the runtime's answer and the hub writes it: a source that
        answers puts back what an older verdict left `error`, and that is the one
        path the merge has for re-enabling a row (`ToolStore._plan`'s
        `reconnected`).  A row stating a verdict of its own would take that
        decision away from the runtime and turn it into an "update" — which is
        what marking every row `error` at boot and then writing it straight back
        looked like from the log.  Neither document family can do this: a skill
        and a `cli:` entry have no connection whose state a verdict could come
        from, which is why they, and only they, carry one.
        """
        schema = getattr(tool, "input_schema", None)
        return {
            "name": proxied_name(self.settings.name, str(getattr(tool, "name", ""))),
            "description": str(getattr(tool, "description", "") or ""),
            # What the far end calls it, which is not what the model calls it:
            # the advertised name is sanitised and carries the server in front.
            "remote_name": str(getattr(tool, "name", "")),
            "schema": json.dumps(schema, ensure_ascii=False) if schema else "",
        }

    def declaration(self) -> dict[str, Any]:
        """This entry, as the hub is told about it."""
        enabled = self.settings.enabled
        # **Start the attempt and do not wait for it**, on every ask: a server
        # that is starting is one this cannot answer for, and an ask is the only
        # moment there is to ask again.
        if enabled:
            self.connection.connecting()
        up = enabled and self.connection.usable
        return {
            "name": self.settings.name,
            "category": self.category,
            "enabled": enabled,
            "up": up,
            "error": self.connection.error,
            "autoload": self.settings.autoload,
            "description": self.settings.description,
            "transport": self.settings.transport,
            # No rows unless it is answering: see the module docstring.
            "rows": self.rows if up else None,
        }


async def settle_attempts(attempts: Iterable[asyncio.Task[None] | None]) -> None:
    """Let the connect attempts already in flight finish, briefly.

    **Wait, never cancel.**  The obvious spelling of this at a call site is
    `asyncio.gather(*pending)` under `asyncio.timeout`, and it is wrong in a way
    that took a live run to see: on timeout the timeout cancels *this* task, the
    cancellation propagates into the gather, and the connect being waited for
    dies with it — leaving a source `idle` with no error and no tools, which is
    the one state that describes nothing at all.  A caller's patience has nothing
    to do with whether a connection should continue.

    **Both processes that hold a pool of links settle this way**, and that is why
    it is one function: `slife2.toolhub`'s `settle` over the plugins it is
    connected to, and `Family.settle` over the entries of one config section
    (`settled` is the single-entry case of the same wait).  The bound is the same
    one — `slife2.timeouts.timeouts.ready.list_settle` — because the two answers
    are the same answer: "not answering yet" and "absent" must read alike, so a
    caller is told which of them it got instead of being held open.

    Nothing is waited for when nothing is in flight, and a source nobody has asked
    about yet has no attempt at all: it is `idle`, which is a state rather than a
    delay.
    """
    pending = [attempt for attempt in attempts if attempt is not None]
    if pending:
        await asyncio.wait(pending, timeout=_T.timeouts.ready.list_settle)


class Family:
    """Every entry of one config section, held and declared."""

    def __init__(
        self,
        config: Config,  # noqa: ARG002 - the house signature; the entries are passed in
        *,
        category: str,
        entries: Iterable[ToolServerSettings],
        transports: Mapping[str, Callable[[ToolServerSettings], Any]] | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.category = category
        self._directory = str(data_dir())
        self._transports = dict(transports or {})
        self._client_factory = client_factory
        self._held = {entry.name: self._hold(entry) for entry in entries}

    def _hold(self, settings: ToolServerSettings) -> Held:
        """A `Held` for one entry, wired the way this process wires its own."""
        return Held(
            settings,
            directory=self._directory,
            category=self.category,
            transport=self._transports.get(settings.name),
            client_factory=self._client_factory,
        )

    # --- what a management tool changes --------------------------------------

    async def put(self, settings: ToolServerSettings) -> Held:
        """Hold *settings*, replacing whatever was held under that name.

        **The new entry is in the map before anything is awaited.**  A caller
        that took the old one out and only then awaited its close would leave a
        window in which this family declares *nothing* for a name it still
        holds — and the hub reads a source that has stopped being declared as an
        entry taken out of the section, so it would delete that server's rows
        and re-embed every one of them when the next declaration put them back.
        So the swap is synchronous and the teardown follows it.

        The replacement is a *new* connection rather than a mutated one: a
        connection is what it was built from, and the settings it was built from
        are frozen.

        **An unchanged entry is left alone**, which is what makes this an upsert
        a caller can repeat: tearing the link down and rebuilding it would cost a
        fresh `npx` start and a new handshake for a call that changed nothing.
        The comparison is on the settings, and both sides came from the config
        layer, so a `${VAR}` reference is compared resolved-against-resolved —
        v1 compared the raw argument against the resolved pool and therefore
        never matched, reconnecting on every identical `mcp_set`.
        """
        previous = self._held.get(settings.name)
        if previous is not None and previous.settings == settings:
            return previous
        previous = self._held.pop(settings.name, None)
        one = self._hold(settings)
        self._held[settings.name] = one
        if settings.enabled:
            one.connection.connecting()
        if previous is not None:
            await previous.connection.close()
            logger.info("%s: replaced", settings.name)
        return one

    async def remove(self, name: str) -> Held | None:
        """Stop holding *name*, dropping its link.  `None` if it was not held.

        The pop is what the declaration sees, so it happens first for the reason
        `put` states — except that here the disappearance is the point, and the
        hub deleting the rows is the outcome being asked for.
        """
        one = self._held.pop(name, None)
        if one is not None:
            await one.connection.close()
        return one

    async def set_enabled(self, name: str, enabled: bool) -> Held | None:
        """Switch one held entry on or off.  `None` if it is not held.

        A rebuild rather than a flag on the live object: an enabled entry has a
        link and a disabled one does not, so this is `remove` and `put` and
        there is no third state where the settings say one thing and the
        connection is doing another.
        """
        one = self._held.get(name)
        if one is None or one.settings.enabled == enabled:
            return one
        return await self.put(replace(one.settings, enabled=enabled))

    def held(self, name: str) -> Held | None:
        """One held entry, for a tool answering about it."""
        return self._held.get(name)

    def connecting(self) -> None:
        """Start a connect attempt for every entry that is switched on.

        Called at startup and on every ask: a source that is not answering is one
        this has to ask about again, and the only moment there is to ask is a
        `list_sources`.
        """
        for one in self._held.values():
            one.declaration()

    async def settle(self) -> None:
        """Let attempts already in flight finish, briefly.

        The bound, the "wait, never cancel" and the reason for both are
        `settle_attempts`', which is the one place they are stated — the hub's
        own settle over its plugin links is the same wait over a different pool.
        """
        await settle_attempts(one.connection.attempt for one in self._held.values())

    async def declare(self) -> dict[str, Any]:
        """The `list_sources` payload: everything this family holds.

        **The wait is bounded and only for attempts already begun**, which is
        what keeps this from being the thing that holds a turn open: a server
        that started with this process has had the whole of its own start-up to
        answer by now, and one that has not is declared as not up yet rather than
        waited for.
        """
        self.connecting()
        await self.settle()
        return {"sources": [one.declaration() for one in self._held.values()]}

    async def call(
        self,
        source: str,
        tool: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Run one tool of one held entry.  `(text, ok)`, never raises.

        **A switched-off entry refuses, and this is the one place that has to
        say so.**  A disabled source's rows stay in the catalogue so a search
        still finds them (`slife2.toolhub`), which means the model can call one
        — and the hub routes by row without reading a status, so `Connection`
        would happily connect and spawn the process the operator had switched
        off.  The guard is on `call` rather than only on `settled` because this
        is the path a *call* takes, and the whole of what `enabled: false`
        promises is that a call is not a thing that starts it.
        """
        one = self._held.get(source)
        if one is None:
            # The hub routes by what it was told, so this is a hub that has a row
            # this family did not declare — an entry removed in the last moment,
            # or a name it invented.  Said as what it is.
            return f"{source!r} is not a server this plugin holds any more", False
        if not one.settings.enabled:
            return (
                f"{source!r} is switched off, so nothing was run. Its "
                f"`set_enabled` tool turns it on."
            ), False
        return await one.connection.call(tool, arguments, meta)

    def start(self) -> None:
        """Begin connecting to every entry that is switched on.

        **At startup, and not at the first ask.**  The asks that follow have a
        bounded wait in them, so a server that started with this process has had
        its whole start-up to answer by the time anybody asks — which is the head
        start it had when the hub held the link itself.
        """
        self.connecting()

    async def close(self) -> None:
        """Drop every link this family holds, once.

        **The mapping is snapshotted, and not for tidiness.**  This awaits inside
        the loop (`Connection.close` waits on a cancelled attempt and then on
        `__aexit__`), so a management tool landing between two iterations —
        `mcp_set`, `mcp_remove`, `*_set_enabled`, all reached through this same
        server — would mutate `_held` underneath the iteration and raise
        "dictionary changed size during iteration" out of the lifespan teardown,
        leaving every link after the one that was being closed still open.  It is
        the same precaution the two readers take: `settle` hands `settle_attempts`
        a generator, which is drained into a list before anything is awaited, and
        `connecting` never suspends at all.  The two writers (`put`, `remove`)
        already swap the mapping synchronously before they await anything, for
        the reason their own docstrings give.
        """
        for one in list(self._held.values()):
            await one.connection.close()


async def settled(one: Held) -> Held:
    """Wait briefly for *one* entry's connect attempt, and hand it back.

    **Bounded, and the bound is the point**: an answer about a server that is
    still starting is the same answer a broken one gives — "not answering yet" —
    and telling those two apart is the whole job of the tools that ask.  So it
    starts the attempt if nobody has (a call arriving before anything tried is
    one that tries) and waits for the one in flight, for no longer than the hub
    waits for its own links.  A cold `npx` that this process started has had its
    own start-up to answer by now; one that needs longer is reported as
    connecting, truthfully, rather than held open.

    **A switched-off entry is never started**, which is the one thing this must
    not do: asking what a disabled server offers would otherwise spawn it, and
    the operator's `enabled: false` would be undone by a *question*.  The guard
    is here rather than left to the callers who remember to check.
    """
    if not one.settings.enabled:
        return one
    one.connection.connecting()
    await settle_attempts([one.connection.attempt])
    return one


async def apply_and_report(
    family: Family, settings: ToolServerSettings, *, words: FamilyWords
) -> str:
    """Hold *settings* and describe what happened, in one sentence.

    **The three outcomes are the three things that can be true**, and the point
    is that they are distinguishable: a server that connected has a tool count,
    a disabled one has nothing to connect, and one that did not answer says so
    rather than reporting a count of zero — which is what a server with no tools
    would also report.  Whether it answers *later* is not a claim this can make;
    what it can say is that the catalogue learns at the next search, because the
    hub re-reads every declaration before each one.

    **The sentence itself is the family's**, not this function's: `words` carries
    every word that varies (`slife2.toolfamily.FamilyWords`), and the two answers
    this shares with `switch_entry` are methods on it rather than text written
    out here.  They were written out here, and the copy had `connected`,
    `tool(s)`, `connecting` and `` `set_enabled` `` hardcoded into it — so the
    REST API family answered in the MCP family's vocabulary, naming a tool it
    does not have.
    """
    one = await family.put(settings)
    if not settings.enabled:
        return (
            f"'{settings.name}' is written into `{words.section}:` and switched "
            f"off, so this process does not connect it. Its `{words.enable_tool}` "
            f"tool turns it on."
        )
    await settled(one)
    if one.connection.usable:
        count = len(one.rows or [])
        # **The count is the cost, and only the tool can say it before it is
        # paid.**  Nothing here writes the catalogue: the hub merges this
        # source's whole list before the next search, and that merge embeds every
        # row — one request, every text of every tool.  So a server with four
        # figures of tools makes the model's *next* search the expensive call,
        # which is one turn too late to be discovering it.  A count at or under
        # the listing cap is ordinary and is left unsaid.
        expensive = (
            " That is a large server: the next search writes and embeds a row for "
            "each of them, and may take a while."
            if count > TOOL_LIST_LIMIT
            else ""
        )
        return words.offers(settings.name, count, note=expensive)
    reason = one.connection.error or "it has not answered yet"
    return words.coming_up(
        settings.name, reason, because=f"written into `{words.section}:`"
    )


def tools_as_text(one: Held, name: str, limit: int, *, noun: str = "tool") -> str:
    """One server's tools, capped, with the count it was capped from.

    The cap is announced rather than applied quietly: a trimmed list that reads
    as the whole list is a model concluding a server cannot do something it can.

    `noun` is the family's own word for what it lists — a REST API exposes
    *operations*, and the tool that asked for them said so.  The same answer
    under two names would make a reader wonder whether they are two things.
    """
    rows = list(one.rows or [])
    if not one.connection.usable or not rows:
        reason = one.connection.error or "it is not answering yet"
        return (
            f"'{name}' has no tool list to show — {reason}. The `_list` tool "
            f"reports its state."
        )
    shown = rows[:limit]
    lines = [f"{name} — {len(rows)} {noun}(s), showing {len(shown)}:"]
    for row in shown:
        text = str(row.get("description") or "").strip().splitlines()
        summary = text[0] if text else ""
        lines.append(
            f"- {row.get('remote_name', '?')}" + (f" — {summary}" if summary else "")
        )
    if len(shown) < len(rows):
        lines.append(
            f"\n{len(rows) - len(shown)} more — use tool_search to find the one "
            f"you need."
        )
    return "\n".join(lines)


def refusal(config: Config, name: str) -> str | None:
    """Why *name* cannot become a source of this family, or `None`.

    **The collision it prevents is silent, which is the whole reason it is
    checked here.**  The hub routes by source name and refuses to know two owners
    for one: a `mcp:` entry called `agent` would be declared, dropped by
    `refresh_declared` as a name this hub is already connected under, and
    reported to the model as added — with no tools, and nothing anywhere saying
    why.  The same from the other side for `skills` and `cli`, which are the
    names those two families file their own rows under; a source's verdict is
    written across every row it owns, so sharing one would mark somebody else's
    playbooks broken.

    Two names and not every reserved word: the rest of what a source may not be
    called — the plugin's own name, a category the hub owns — the hub refuses
    loudly at declaration time (`Upstream._record`), and duplicating that list
    here would be a second copy to keep in step.
    """
    if name in config.plugins():
        return (
            f"[refused] '{name}' is a server slife2 itself runs, so its tools "
            f"would be dropped as a name the hub is already connected under. "
            f"Choose another name."
        )
    if name in (SKILLS_SOURCE, CLI_SOURCE):
        return (
            f"[refused] '{name}' is the name the `{name}:` section's rows are "
            f"filed under. Choose another name."
        )
    return None


# ── The management tools' shared half ───────────────────────────────────
# `mcp_tools` and `restapi_tools` serve the same four tools over one mechanism
# and answer in the same sentences; the words they differ by live in a
# `FamilyWords`, and the sentences that consume it live here.  What stays in
# each family is the part that is genuinely its own: `*_set`'s argument list
# (the schema the model reads — `mcp_set` takes a command, `rest_api_set` a
# spec), and `*_list`'s `describe`, because an MCP entry is a command or a URL
# and a REST entry is a spec and a base URL.


@dataclass(frozen=True)
class FamilyWords:
    """One family's vocabulary — the tokens its four sentences vary by.

    The two connection families manage the same shape of entry through the
    same `Family` and answer in the same sentences: "is not in `…:` — `…_set`
    adds it"; "is switched on and …ing".  Every word that changes between them
    is a field here, so the sentences can be written once.  Two copies of a
    sentence is two chances for a model to learn two vocabularies for one
    mechanism.
    """

    section: str
    """The config section this family owns: `mcp` or `rest-api`."""

    plural: str
    """Its entries, plural, as the empty listing names them."""

    entry: str
    """One entry, singular with its article: `a server` or `an API`."""

    unit: str
    """What one exposed item is: `tool` or `operation` (pluralised `{unit}(s)`)."""

    list_tool: str
    """The listing tool's name, so another sentence can point at it."""

    set_tool: str
    """The upsert tool's name, for the same reason."""

    enable_tool: str
    """The `*_set_enabled` tool's name.  It is *not* `set_tool`: one writes an
    entry and the other flips a switch on one that is already written, and a
    sentence that told a model to reach for the wrong one is the failure this
    whole class exists to make impossible — it was hardcoded to `set_enabled`
    for both families before this field existed."""

    remove_log: str
    """The `logger.info` event a removal writes."""

    present: str
    """A live link, present tense: `connected` or `serving`."""

    past: str
    """A live link said of the next start: `connected` or `served`."""

    starting: str
    """A link still coming up: `connecting` or `starting`."""

    def offers(self, name: str, count: int, *, note: str = "") -> str:
        """A live link, and what it offers — the answer **both** entry tools give.

        **One sentence, because there is one state.**  `mcp_set` reaches a
        connected server by writing its entry and `mcp_set_enabled` by flipping a
        switch on one already written, and a model that got two different accounts
        of the same connection would be reading two vocabularies for one
        mechanism — which is what happened: this sentence was written out twice,
        once here and once hardcoded, and the hardcoded copy said "connected" and
        "tool(s)" for *every* family, so `rest_api_set` described an API as
        connected with three tools.  The words that legitimately differ are fields
        of this class (`present`, `unit`), which is the whole of what it is for.

        `note` is the one thing a caller may add: a server whose tool count
        crosses the listing cap is worth warning about at the moment it is
        connected, and only the caller that has just merged it knows the count.
        """
        return (
            f"'{name}' is {self.present} and offers {count} {self.unit}(s); "
            f"they are in your tool list from the next search.{note}"
        )

    def coming_up(self, name: str, reason: str, *, because: str) -> str:
        """A link that has not answered yet — the other answer both tools give.

        `because` is the hole, and it is the operation's own account of itself:
        one caller *wrote* the entry into the file, the other *switched on* one
        that was already there, and those are genuinely two facts.  Everything
        after the reason is not, which is why it is here rather than in each.
        """
        return (
            f"'{name}' is {because} and {self.starting} — {reason}. Its "
            f"{self.unit}s appear once it answers, and the hub retries before "
            f"every search."
        )


def state_word(raw: Mapping[str, Any], one: Held | None) -> str:
    """The one word a listing puts in `[brackets]` for one entry.

    **Read off the file before the link**, which is the whole of the rule: a
    switched-off entry was never asked, so its link has nothing to say and its
    state is the operator's `off` — not the `idle` of a connection nobody
    started.  `not held` is an entry the file has and this process does not.

    The word for a live link is `Connection.state`'s own (`ready`, `connecting`,
    `failed`, `idle`), because it is the same question the hub answers and a
    second vocabulary for it would be a second thing to learn.

    Only the families that hold a *link* use this.  A `cli:` entry and a skill
    have no connection whose state a verdict could come from, so they say
    `on`/`off` from their own rule and are not folded in here.
    """
    if raw.get("enabled") is False:
        return "off"
    if one is None:
        return "not held"
    return one.connection.state


def prepare_entry(
    config: Config,
    name: str,
    build: Callable[[], tuple[ToolServerSettings, Mapping[str, Any]]],
    *,
    section: str,
) -> tuple[ToolServerSettings | None, str]:
    """Refuse, validate and write one entry, or answer with why it cannot be.

    `(settings, "")` means it was written; `(None, refusal)` is a sentence for
    the caller to return.  Both refusals happen **before** anything is written:
    a name the hub is already using (`refusal`), and an entry the config layer
    will not parse.

    `build` is the family's own half — it constructs the entry and runs it
    through the parser a start uses (`_tool_server`, `_rest_api`), returning
    the settings and the mapping to write.  A callable rather than a mapping
    because the REST family's two URL checks raise `ConfigError` *while
    building the entry*, and giving them a `try` of their own would be the
    contortion that keeping them inside this one avoids.  Anything `build`
    raises as a `ConfigError`, and anything the writer raises, is refused in
    the loader's own words — which is what makes what a model may write and
    what a start accepts one question.
    """
    if why := refusal(config, name):
        return None, why
    try:
        settings, entry = build()
        configfile.upsert(section, name, entry)
    except ConfigError as exc:
        return None, f"[refused] {exc}"
    return settings, ""


async def list_entries(
    family: Family,
    *,
    words: FamilyWords,
    describe: Callable[[Held | None, str, Mapping[str, Any]], str],
) -> str:
    """The `*_list` answer: every entry as the file has it, with its state.

    Settles first, for the reason the hub's `servers()` does: this is the
    answer to "why is my tool missing", and a server that started a moment ago
    reads as one still starting — two different problems this tool exists to
    tell apart.  The description is the family's, because an MCP entry is a
    command or a URL and a REST entry is a spec and a base URL.
    """
    family.connecting()
    await family.settle()
    entries = configfile.read_section(words.section)
    if not entries:
        return (
            f"No {words.plural} are configured under `{words.section}:` yet. "
            f"`{words.set_tool}` adds one."
        )
    return "\n".join(
        describe(family.held(name), name, raw) for name, raw in entries.items()
    )


async def remove_entry(
    family: Family,
    name: str,
    *,
    words: FamilyWords,
    logger: logging.Logger,
) -> str:
    """The `*_remove` answer: stop holding *name*, drop its entry.

    The file is consulted as well as the process, because a name this process
    never held but the operator wrote is still one to delete — and one in
    neither is answered as what it is rather than silently succeeding.
    """
    if family.held(name) is None and name not in configfile.read_section(words.section):
        return (
            f"'{name}' is not {words.entry} under `{words.section}:` — "
            f"see `{words.list_tool}`."
        )
    removed = configfile.remove(words.section, name)
    stopped = await family.remove(name)
    if not removed:
        # **Two ways to be here, and the old sentence was true of one.**  The
        # entry can be held and no longer in the file — another instance removed
        # it, or a person edited it out — and then a link *was* stopped, so
        # "nothing was removed" is the opposite of what happened.
        if stopped is not None:
            return (
                f"'{name}' was not written in `{words.section}:` any more, so "
                f"there was no entry to delete — but this process was still "
                f"holding it, and that link is stopped."
            )
        return (
            f"'{name}' was not written in `{words.section}:`, so nothing was removed."
        )
    logger.info("%s name=%s", words.remove_log, name)
    return f"'{name}' is stopped and its entry is gone from `{words.section}:`."


async def switch_entry(
    family: Family, name: str, enabled: bool, *, words: FamilyWords
) -> str:
    """The `*_set_enabled` answer: flip the switch and say what followed.

    The switch is the file's, and the link follows it — a rebuild rather than a
    flag, so there is no state where the two disagree (`Family.set_enabled`).
    """
    if name not in configfile.read_section(words.section):
        return (
            f"'{name}' is not in `{words.section}:` — `{words.set_tool}` adds it, "
            f"`{words.list_tool}` shows what is there."
        )
    configfile.set_enabled(words.section, name, enabled)
    one = await family.set_enabled(name, enabled)
    if one is None:
        # Written but not held: this process did not have it — an entry another
        # instance added since this one read the config.  The file is right;
        # the next start holds it.
        return (
            f"'{name}' is now {'enabled' if enabled else 'disabled'} in "
            f"`{words.section}:`. It was not one of the servers this process is "
            f"holding, so it is {words.past} from the next start."
        )
    if not enabled:
        return f"'{name}' is switched off; its entry stays in `{words.section}:`."
    await settled(one)
    if one.connection.usable:
        return words.offers(name, len(one.rows or []))
    reason = one.connection.error or "it has not answered yet"
    return words.coming_up(name, reason, because="switched on")


async def list_entry_tools(
    family: Family, name: str, limit: int, *, words: FamilyWords
) -> str:
    """The `*_list_tools` answer: one entry's exposed items, capped."""
    one = family.held(name)
    if one is None:
        return (
            f"'{name}' is not {words.entry} this process holds — "
            f"see `{words.list_tool}`."
        )
    # Waits for the link, for the reason `list_entries` does: what this reports
    # is the items the entry *offers*, and "no list" is the answer for both a
    # broken entry and one still starting.
    await settled(one)
    return tools_as_text(
        one, name, limit if limit > 0 else TOOL_LIST_LIMIT, noun=words.unit
    )


@dataclass
class Documents:
    """A family whose rows are documents: one config section, no connection.

    `skills:` and `cli:` are the two (DESIGN.md §8.2 is the argument for giving
    them servers at all).  What they share is not a *mechanism* — one writes
    files into a folder and the other validates a command line against `PATH` —
    but a shape, and the shape was written out twice:

    * **one source declared to the hub from a mirror of the section**, held here
      so that a tool's edit is visible to the next declaration without a restart;
    * **a switch mirrored into that mirror**, which is the one write both
      families make and neither can skip.

    `declare` builds the whole `list_sources` answer rather than the family
    assembling the envelope itself, because every key in it is a claim to the
    hub — `up`, `transport`, `category` — and a second copy of the envelope is a
    second place one of them can come out wrong.
    """

    source: str
    category: str
    section: str
    entries: dict[str, Any]

    def declare(self, description: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """This server's whole holding, as `list_sources` answers it.

        **One source, and it is always up.**  There is no connection to be down,
        so `up` is not a claim anybody has to check; a row that cannot be read is
        a *row* carrying `error`, which is a different thing and is said where it
        belongs.  `transport` is empty for the same reason, and that is why these
        sources are absent from `servers()` — a source with no connection is not
        a server.
        """
        return {
            "sources": [
                {
                    "name": self.source,
                    "category": self.category,
                    "enabled": True,
                    "up": True,
                    "description": description,
                    "transport": "",
                    "rows": rows,
                }
            ]
        }

    def switched(
        self, name: str, enabled: bool, make: Callable[[], Any] | None = None
    ) -> None:
        """Mirror one switch into the held entries.

        `make` is what to put there when the mirror has no entry of that name —
        a second instance wrote one, or the operator did — and a family that
        should leave it alone passes nothing.  The switch has already been
        written to the file by the caller: this is the in-memory half.
        """
        current = self.entries.get(name)
        if current is not None:
            self.entries[name] = replace(current, enabled=enabled)
        elif make is not None:
            self.entries[name] = make()

    def forgotten(self, name: str) -> None:
        """Drop one entry from the mirror, after the file has been changed."""
        self.entries.pop(name, None)


def build_document_server(
    *,
    name: str,
    instructions: str,
    source: str,
    category: str,
    section: str,
    description: str,
    entries: Mapping[str, Any],
    rows: Callable[[Mapping[str, Any]], list[dict[str, Any]]],
    management: Callable[[FastMCP, Documents], None],
) -> FastMCP:
    """One document family, as the MCP server slife2 starts.

    The sibling of `build_family_server`, for the families that hold no
    connection: one tool towards the hub (`list_sources`, unmarked and invisible
    to the model) and `management` for what the model does get.

    `management` is a callback for the reason the other builder's is: a tool's
    parameter list *is* the schema the model reads, so `cli_set(name, command,
    description, …)` and `skill_set(name, files)` belong with the section each
    describes rather than in the code two sections share.  What *is* shared is
    the mirror those tools mutate — so an edit is in front of the next search
    without a restart — and the declaration built from it.

    `entries` is the config's own mapping for that section, copied here; `rows`
    turns the mirror into catalogue rows, which is the one thing a family cannot
    share because a playbook's row *is* its document and a command's is its
    invocation.
    """
    family = Documents(
        source=source,
        category=category,
        section=section,
        entries=dict(entries),
    )

    mcp: FastMCP = house_server(name, instructions=instructions)

    @mcp.tool(name=LIST_SOURCES)
    def list_sources() -> dict[str, Any]:
        """Every source this plugin holds — one, with everything it declares.

        **Not a tool for the model**, and so not marked as one: the hub asks for
        this and records the answer, and the model never sees the name.  The
        whole list rather than a difference, because that is what makes a
        deleted document stop being a hit — a merge reads an absent name as a row
        the source no longer has.
        """
        return family.declare(description, rows(family.entries))

    management(mcp, family)
    return mcp


def build_family_server(
    config: Config,
    *,
    name: str,
    instructions: str,
    category: str,
    entries: Iterable[ToolServerSettings],
    transports: Mapping[str, Callable[[ToolServerSettings], Any]] | None = None,
    client_factory: ClientFactory | None = None,
    management: Callable[[FastMCP, Family], None] | None = None,
) -> FastMCP:
    """One family, as the MCP server slife2 starts.

    Two tools are the whole of the plugin contract this kind of plugin has
    *towards the hub*: what it holds, and running one.  Neither is marked for the
    model, and neither should be — the model is given the tools of the *entries*,
    merged by the hub, and never these.  What the model gets from this process is
    what `management` registers, which is a different surface with a different
    reader (see `slife2.toolfamily`'s own note on `refusal`, and DESIGN.md §8).

    `transports` and `client_factory` are the same two seams the hub has and for
    the same reason: a test drives the whole thing over in-memory servers, with
    no process and no port, and a test that needs a client which misbehaves needs
    to say so from outside this module.

    `management` registers the family's own tool set — the tools a model uses to
    add, remove and switch entries — and it is a callback rather than something
    this function writes because those tools are *the family's vocabulary*:
    `mcp_set` takes a command or a URL and `rest_api_set` takes a spec and a base
    URL, and the parameter list of a tool is the schema the model reads, so it
    belongs with the section it describes rather than in the code the two
    sections share.  What is shared — the live `_held` the tools mutate, so that
    an entry written to the file is connected in the same breath — is here.
    """
    family = Family(
        config,
        category=category,
        entries=entries,
        transports=transports,
        client_factory=client_factory,
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        """Hold the links while the server is up, and drop them on the way out.

        Not per client, for the reason the hub's is not: this process's pool is
        the process's, and a client disconnecting is not a reason to restart
        every stdio server behind it.
        """
        family.start()
        try:
            yield {}
        finally:
            await family.close()

    mcp: FastMCP = house_server(name, instructions=instructions, lifespan=lifespan)

    @mcp.tool(name=LIST_SOURCES)
    async def list_sources() -> dict[str, Any]:
        """Every source this plugin holds, and everything about each of them.

        **Not a tool for the model**, and so not marked as one: the hub asks for
        this and records the answer.  A source is a name, its category, whether
        the operator switched it off, whether it is answering now, what it is
        for, how it is reached — and its rows, which are absent unless it is
        answering, because a plugin cannot list what it cannot reach and an empty
        list would be read as "it has no tools any more".

        **Async, and not for the I/O**: a connect attempt is a task on the
        running loop, and FastMCP runs a sync tool in a worker thread, where
        there is no loop to start one on.

        Returns:
            `sources`: one entry per configured server, in file order.
        """
        return await family.declare()

    @mcp.tool(name=CALL_SOURCE)
    async def call_source(
        ctx: Context, source: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Run one tool of one held source.

        `tool` is **the far end's own name**, which the hub read off the row it
        merged: this plugin is not the one that named the tool, so it is not the
        one that can guess it back.  A refusal comes back as `ok` false with the
        text, which is the contract every call in this system keeps.

        The caller's identity rides in `_meta` and is forwarded on unchanged —
        the conversation a call is for is a fact only the far end can act on, and
        this process is a second hop on the way there rather than a reader.
        """
        text, ok = await family.call(
            source, tool, arguments, forwarded_client(request_meta(ctx))
        )
        return {"text": text, "ok": ok}

    if management is not None:
        management(mcp, family)

    return mcp
