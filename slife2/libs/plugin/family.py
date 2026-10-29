"""A family plugin's shared half: connections for the families that hold them,
declarations for the families that do not.

**This is where a connection lives.**  The hub is the tool *set's* owner — which
tools exist, what the model is holding, what the budget takes back — and holding
twenty connections is not that job; it is the job of the process that owns the
config section they were written in.  So `mcp:` and `rest-api:` each have a
plugin, and this is the half of both that is the same code.

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
from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.clients.gateway import (
    ClientFactory,
    Connection,
    proxied_name,
    transport_for,
)
from slife2.libs.host import configfile
from slife2.libs.host.config import (
    Config,
    ConfigError,
    ToolServerSettings,
    for_the_call,
)
from slife2.libs.host.paths import home_key
from slife2.libs.plugin.audience import forwarded_client, request_meta
from slife2.libs.plugin.protocol import (
    CALL_SOURCE,
    CLI_SOURCE,
    JOB_SOURCE,
    LIST_SOURCES,
    RETURNS,
    SKILLS_SOURCE,
    house_server,
)

logger = logging.getLogger(__name__)

#: How many of one server's tools a `*_list_tools` hands over in one page.  The
#: cap follows from what a published server can carry — four figures of tools
#: (github: 1239) — because handing them all over spends the model's context on
#: names it never asked for.  The read is uncapped — the catalogue needs every tool.
TOOL_LIST_LIMIT = 20

#: How many entries one `*_list` hands over in one page.
#:
#: **A cap rather than a preference**, and the same one `TOOL_LIST_LIMIT` is: a
#: `limit` comes from a model, and a section an operator has let grow to a
#: hundred entries is a listing that would spend a context on names.  Both
#: listings answer `total`, which is the *whole* count and not the page's, so a
#: page says what it left behind and `offset` reaches it — hiding rows with
#: nothing saying they were hidden is the one thing a browse must not do.
LIST_PAGE = 50


def page_of[T](
    rows: list[T], limit: int, offset: int, *, cap: int
) -> tuple[list[T], int, int]:
    """One page of a listing: the rows, and the `limit`/`offset` that describe it.

    **The answer's `limit` is the page's own and never the one that was asked
    for**, which is `slife2.libs.store.index.page_limit`'s rule and its reason:
    a caller pages by `offset`, so an answer echoing a size it did not apply —
    1000, say, for a 50-row cap — makes the caller's own arithmetic skip every
    row in between.  A `limit` of zero or less means "the cap", so a caller that
    names no size gets a page without naming `LIST_PAGE` itself.

    `offset` is floored at zero for the same reason it is echoed back: it is the
    number the caller will add its page to, and a negative one would make the
    next page overlap this one.
    """
    size = cap if limit <= 0 else min(int(limit), cap)
    start = max(0, int(offset))
    return rows[start : start + size], size, start


def listed[T](
    rows: list[T], key: str, limit: int, offset: int, *, cap: int
) -> dict[str, Any]:
    """A listing's answer: `key`'s page, what there is in all, and where this is.

    One builder for the four listings that answer rows (`mcp_list`,
    `rest_api_list`, `cli_list`, `skill_list`) and the two that answer tools,
    because they are one shape with one fact under it:
    `offset + len(key) < total` is how a model knows it has not seen everything.
    """
    page, size, start = page_of(rows, limit, offset, cap=cap)
    return {key: page, "total": len(rows), "limit": size, "offset": start}


class Link:
    """One external server this process is running, and the rows it last offered.

    **One per name, however many homes name it.**  That is the designer's rule and
    the whole of why this class exists: *whether it is the same MCP server is
    decided by the name.*  An `mcp:` entry is a process — or somebody else's
    endpoint — and a machine is where processes live, so two data directories that
    configure `serper` are two homes naming one server rather than two servers.
    Without this, a second home starts all of them again: `slife2-jack`'s
    section is byte-identical to the checkout's, and that is twenty `npx`/`uvx`
    processes for the same twenty servers.

    **The rows are here and not on a binding**, because what the far end offers is
    the *server's* listing: two homes reading it are reading one answer, and a
    copy per home would be a second thing to keep in step.  What an entry says
    that a server does not — the switch and the description — stays on `Held`,
    which is what lets one home switch an entry off without switching it off for
    anybody else.

    `settings` is what this link was **started** from, which is the first home's.
    A second home naming the same entry with a different command gets this link and
    a warning rather than a second process (`Links.acquire`), and an *edit* through
    a management tool reconfigures it in place (`Links.reconfigure`) — one server,
    one configuration, whichever home wrote it.
    """

    def __init__(
        self,
        settings: ToolServerSettings,
        *,
        home: str,
        directory: str,
        category: str,
        transport: Callable[[ToolServerSettings], Any] | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.category = category
        self.settings = settings
        #: The home that started it, named when a second one disagrees about what
        #: this entry is: a directory is what a reader needs to go and look at.
        self.home = home
        #: The homes holding it, so that the last one out stops the process.  A
        #: home with the entry switched *off* is not in here at all (`Held`) — its
        #: switch is not a reason for somebody else's server to keep running.
        self.holders: set[str] = set()
        #: `None` until it has answered once, and again while it is re-listing:
        #: "no rows" and "no rows *yet*" are the same thing to the hub, which is
        #: told `up: false` either way and marks the source rather than merging.
        self.rows: list[dict[str, Any]] | None = None
        #: **One edit or teardown at a time.**  Two management calls for one
        #: entry are two conversations on a shared plugin — an `mcp_set` from a
        #: turn and one from a task — and `reconfigure` is a read (the current
        #: connection), an await (`close`), and a write (the new one).  Two that
        #: interleave both read the same old connection and both replace it, so
        #: one of the two replacements is dropped without its `close` ever being
        #: called.  The lock covers the swap and the close, not the connect
        #: attempt, because the attempt belongs to `Connection` and is awaited
        #: by the caller that asked.
        self._lock = asyncio.Lock()
        self.connection = self._connect(settings, directory, transport, client_factory)

    def _connect(
        self,
        settings: ToolServerSettings,
        directory: str,
        transport: Callable[[ToolServerSettings], Any] | None,
        client_factory: ClientFactory | None,
    ) -> Connection:
        """A connection wired the way this process wires its own.

        `directory` is the home that first asked for the entry, and it is the one
        thing a *shared* process cannot have per home: an entry that cares where it
        runs names its own `cwd`, and one that does not gets the first home's
        rather than a directory that would move under a running process.
        """
        return Connection(
            settings,
            transport=transport or (lambda one: transport_for(one, cwd=directory)),
            client_factory=client_factory,
            on_listed=self._listed,
            on_failed=self._failed,
        )

    async def reconfigure(
        self,
        settings: ToolServerSettings,
        *,
        directory: str,
        transport: Callable[[ToolServerSettings], Any] | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        """Restart this server on *settings*, for every home that holds it.

        **What a management tool means by an edit.**  The name is the identity, so
        `mcp_set` on an entry two homes name is one server being reconfigured
        rather than a second one appearing beside it — and the homes holding it go
        on holding the entry they named, whose rows the restart replaces.

        The new connection is in place *before* the old one is awaited, which is
        `Family.put`'s rule one level down: a call landing in between must not find
        this link without a connection to make it on.

        **And the swap is one step.**  Two homes can hold one entry, so two
        management calls can reach this link together; both would read the same
        `previous`, both would replace it, and one of the two replacements would
        be dropped without its `close` ever being called.
        """
        async with self._lock:
            previous = self.connection
            self.settings = settings
            self.rows = None
            self.connection = self._connect(
                settings, directory, transport, client_factory
            )
            await previous.close()

    async def close(self) -> None:
        """Close this link's connection, serialised with a reconfigure.

        The other half of the same lock: a home letting go of the last hold
        while another reconfigures the entry must not close a connection the
        reconfigure has just built (`Links.release`).
        """
        async with self._lock:
            await self.connection.close()

    async def _listed(self, tools: list[Any]) -> None:
        self.rows = [self._row(tool) for tool in tools]
        logger.info("%s: %d tool(s) held", self.settings.name, len(self.rows))

    def _failed(self, exc: Exception) -> None:
        # Nothing to write: a family holds no catalogue, and the hub learns this
        # from the next declaration.  It is logged because a server that will not
        # start is otherwise only visible as tools that are not there.
        #
        # **The target is on the line, and this is the only WARNING for the
        # event.**  `settings.url or command` is what a reader goes to look at;
        # the full cause chain stays at DEBUG in `Connection.fail`, where
        # somebody who asked for detail gets it.
        logger.warning(
            "%s (%s) is not reachable: %s: %s",
            self.settings.name,
            self.settings.url or self.settings.command,
            type(exc).__name__,
            exc,
        )

    def _row(self, tool: Any) -> dict[str, Any]:
        """One tool of this entry, as the catalogue's row for it.

        **No `status`, which is the whole of how this family differs from the two
        documents.**  An entry here has a *connection*, so whether its tools are
        usable is the runtime's answer and the hub writes it: a source that
        answers puts back what an older verdict left `error`, and that is the one
        path the merge has for re-enabling a row (`ToolStore._plan`'s
        `reconnected`).  A row stating a verdict of its own would take that
        decision away from the runtime and turn it into an "update" — what
        marking every row `error` at boot and writing it straight back would look
        like in the log.  Neither document family can do this: a skill
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
            # Somebody else's server may say what its answer holds, in the same
            # `_meta` key ours does.  It is kept when it is there and is empty
            # when it is not — absence is the empty string in the catalogue.
            "returns": str((getattr(tool, "meta", None) or {}).get(RETURNS) or ""),
        }


class Links:
    """The external servers one plugin process runs, keyed by what names them.

    **The name, and nothing else**, is what makes two configured entries the same
    server — so a home that writes `serper` and a home that writes `serper` get
    one process, and the second one's command is reported rather than started.
    That is the decision this class exists to hold in one place, and it is a
    deliberate simplification: two homes *can* disagree about what `serper` runs,
    and the answer to that is a warning naming both (`acquire`) rather than two
    processes nobody can tell apart — the tool names in the catalogue are the
    entry's, so a second one would collide with the first anyway.

    **One of these belongs to one server process** (`build_family_server`), like
    `Homes` and for the same reason: a module-level pool would be shared by two
    servers built in one test, and a family that inherited the previous one's
    process would be a test passing for a reason that is not its own.

    The lifetime is the holders': a link is built by the first home that switches
    the entry on and closed when the last one lets go, so the process exists while
    somebody wants it and not after — which is what makes the last-instance-out
    take-down still take everything down (`slife2 down` stops these plugins, and
    their links are the plugin's children).
    """

    def __init__(self) -> None:
        self._links: dict[tuple[str, str], Link] = {}

    def acquire(
        self,
        home: str,
        settings: ToolServerSettings,
        *,
        directory: str,
        category: str,
        transport: Callable[[ToolServerSettings], Any] | None = None,
        client_factory: ClientFactory | None = None,
    ) -> Link:
        """The link for *settings*' name, started if nobody has started it yet.

        A second home naming the same server with different settings is **warned
        about and given the running one**: the name is what makes it the same
        server, so starting a second would be two processes whose tools the
        catalogue could not tell apart.  A home already holding it is not warned
        at — that is the same home re-holding the entry, which is what an edit
        looks like from here (`Family.put` reconfigures it afterwards).
        """
        found = self._links.get((category, settings.name))
        if found is None:
            found = Link(
                settings,
                home=home,
                directory=directory,
                category=category,
                transport=transport,
                client_factory=client_factory,
            )
            self._links[(category, settings.name)] = found
        elif found.settings != settings and home not in found.holders:
            logger.warning(
                "%s: %r is already running for %s, configured differently — the "
                "running one is used, because the name is what makes it the same "
                "server. Give it another name to run both",
                home,
                settings.name,
                found.home,
            )
        found.holders.add(home)
        return found

    async def release(self, home: str, link: Link) -> None:
        """One home lets go.  The last one out stops the process."""
        link.holders.discard(home)
        if link.holders:
            return
        self._links.pop((link.category, link.settings.name), None)
        await link.close()


class Held:
    """**One home's** binding to one configured entry: its switch, and its link.

    What a home owns is what its own section says — `enabled`, `description`, and
    therefore the whole of what it declares — and what the machine owns is the
    process and the listing (`Link`).  That split is the answer to "what may two
    homes' sections disagree about": everything here, and nothing there.

    **Which of its tools start loaded is not here.**  That is
    `tool_load.autoload` in the home's own config, and the hub reads it by tool
    name when it opens that home's catalogue — a declaration carries what the
    *server* offers, and the load list is about the model's list.

    **A switched-off entry has no link.**  That is what `enabled: false` has always
    meant — the operator's promise that a *call* is not a thing that starts it —
    and it is also what keeps one home's switch from holding up a server that
    another home is running.
    """

    def __init__(
        self,
        settings: ToolServerSettings,
        *,
        home: str,
        links: Links,
        directory: str,
        category: str,
        transport: Callable[[ToolServerSettings], Any] | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.settings = settings
        self.category = category
        self._home = home
        self._links = links
        self.link: Link | None = (
            links.acquire(
                home,
                settings,
                directory=directory,
                category=category,
                transport=transport,
                client_factory=client_factory,
            )
            if settings.enabled
            else None
        )

    @property
    def rows(self) -> list[dict[str, Any]] | None:
        """What the server last offered, or `None` if this home holds no link."""
        return self.link.rows if self.link is not None else None

    @property
    def connection(self) -> Connection:
        """This entry's connection, for a caller that needs the object itself.

        **Only for a home that is running it.**  A switched-off entry has no link,
        and a question asked of one — is it up, what did it say, what is it doing —
        is answered by `usable`, `error` and `state`, which say `off` rather than
        raising.  Reaching *here* for a linkless entry is a caller that skipped the
        switch it was supposed to read first, which is why this is an assertion
        rather than a third accessor returning `None`.
        """
        assert self.link is not None
        return self.link.connection

    @property
    def attempt(self) -> asyncio.Task[None] | None:
        """The connect attempt in flight, for a caller that waits on it."""
        return self.connection.attempt if self.link is not None else None

    @property
    def usable(self) -> bool:
        """Whether this entry's server has answered.

        `False` for a home that has the entry switched off, which is not the same
        statement as "it is down" and is not read as one: every caller of this has
        already asked about `enabled` (`settled`, `apply_and_report`).
        """
        return self.link is not None and self.link.connection.usable

    @property
    def error(self) -> str:
        """What the server said the last time it failed, or `""`."""
        return self.link.connection.error if self.link is not None else ""

    @property
    def state(self) -> str:
        """One word for a report — `off` when this home runs nothing for it.

        The hub's own word for a source nobody is connecting
        (`slife2.plugins.toolhub.server._server_row`), used here for the same
        state: `idle` would read as "it will try again", which is exactly what
        `enabled: false` promises it will not do.
        """
        return self.link.connection.state if self.link is not None else "off"

    def connecting(self) -> None:
        """Start a connect attempt, unless this home has the entry switched off.

        Called on every ask: a server that is starting is one this cannot answer
        for, and an ask is the only moment there is to ask again.
        """
        if self.link is not None:
            self.link.connection.connecting()

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Run one tool of this entry's server.  `(text, ok)`, never raises.

        The caller has already refused a switched-off entry (`Family.call`), which
        is the only way there is no link to run it on.
        """
        assert self.link is not None
        return await self.link.connection.call(tool, arguments, meta)

    async def release(self) -> None:
        """Let go of this entry's server, closing it if this home was the last.

        Idempotent, because both callers can reach a link the other has dropped:
        a management tool replacing or removing an entry, and the lifespan letting
        go of every one of them on the way out.
        """
        if self.link is None:
            return
        link, self.link = self.link, None
        await self._links.release(self._home, link)

    def declaration(self) -> dict[str, Any]:
        """This entry, as the hub is told about it.

        **Per home, and the rows are not.**  What this home says is its own switch,
        its own description and its own category; what the model may call is the
        server's listing, which is one answer for every home naming it — and `None`
        unless it is answering, for the module docstring's reason.
        """
        enabled = self.settings.enabled
        # **Start the attempt and do not wait for it**, on every ask: a server
        # that is starting is one this cannot answer for, and an ask is the only
        # moment there is to ask again.
        self.connecting()
        up = enabled and self.link is not None and self.link.connection.usable
        return {
            "name": self.settings.name,
            "category": self.category,
            "enabled": enabled,
            "up": up,
            "error": self.link.connection.error if self.link is not None else "",
            "description": self.settings.description,
            "transport": self.settings.transport,
            # No rows unless it is answering: see the module docstring.
            "rows": self.rows if up else None,
        }


async def settle_attempts(attempts: Iterable[asyncio.Task[None] | None]) -> None:
    """Let the connect attempts already in flight finish, briefly.

    **Wait, never cancel.**  The obvious spelling of this at a call site is
    `asyncio.gather(*pending)` under `asyncio.timeout`, and it is wrong: on
    timeout the timeout cancels *this* task, the cancellation propagates into
    the gather, and the connect being waited for dies with it — leaving a source
    `idle` with no error and no tools, which is the one state that describes
    nothing at all.  A caller's patience has nothing to do with whether a
    connection should continue.

    **Both processes that hold a pool of links settle this way**, and that is why
    it is one function: `slife2.plugins.toolhub.server`'s `settle` over the plugins it is
    connected to, and `Family.settle` over the entries of one config section
    (`settled` is the single-entry case of the same wait).  The bound is the same
    one — `slife2.helpers.timeouts.timeouts.ready.list_settle` — because the two answers
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
        *,
        category: str,
        entries: Iterable[ToolServerSettings],
        links: Links,
        transports: Mapping[str, Callable[[ToolServerSettings], Any]] | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.category = category
        #: This home, which is two things at once and both of them here: **who
        #: holds this family's links** (`Links`), and the working directory an
        #: entry's process gets when its own `cwd` is left out.  One string,
        #: because a family is built inside the call that first needed it
        #: (`Homes`), so the ambient home is already the right one.
        self._home = home_key()
        self._directory = self._home
        self._links = links
        self._transports = dict(transports or {})
        self._client_factory = client_factory
        self._held = {entry.name: self._hold(entry) for entry in entries}
        #: **One management call at a time for this home.**  The map is the
        #: family's own state, and every write to it is an upsert or a
        #: replace — `put` reads the entry it is replacing, so two `mcp_set`
        #: calls can both see the same previous entry and release one link the
        #: other just took (`Family.put` and `Held.release`).  One home, one
        #: lock; two homes editing one entry are serialised one level down, on
        #: the link.
        self._manage = asyncio.Lock()

    def _hold(self, settings: ToolServerSettings) -> Held:
        """A `Held` for one entry: this home's settings over the machine's link."""
        return Held(
            settings,
            home=self._home,
            links=self._links,
            directory=self._directory,
            category=self.category,
            transport=self._transports.get(settings.name),
            client_factory=self._client_factory,
        )

    # --- what a management tool changes --------------------------------------

    async def put(self, settings: ToolServerSettings) -> Held:
        """Hold *settings*, replacing whatever was held under that name — serialised.

        Two conversations on one shared plugin can call a management tool at the
        same time; `_put` is the read-and-swap that must not overlap with itself
        (its docstring says why), and this is the lock around it.  `set_enabled`
        calls `_put` under the same lock rather than this method, because
        `asyncio.Lock` is not reentrant.
        """
        async with self._manage:
            return await self._put(settings)

    async def _put(self, settings: ToolServerSettings) -> Held:
        """Hold *settings*, replacing whatever was held under that name.

        **The new entry is in the map before anything is awaited, and in the
        place the old one held.**  A caller that took the old one out and only
        then awaited its close would leave a window in which this family declares
        *nothing* for a name it still holds — and the hub reads a source that has
        stopped being declared as an entry taken out of the section, so it would
        delete that server's rows and re-embed every one of them when the next
        declaration put them back.  So the swap is synchronous and the teardown
        follows it.

        **And the place matters as much as the moment**: `_held` is in file order,
        which is the order this family declares its sources in and the order the
        hub keeps them in (`build_family_server`'s `list_sources`), so an edit
        that moved its entry to the end would re-order the status view against
        the file `mcp_list`/`rest_api_list` read.  The name is assigned over, and
        never popped and re-inserted: an assignment to an existing key is the one
        operation that changes the value without changing the order.

        The replacement is a *new* connection rather than a mutated one: a
        connection is what it was built from, and the settings it was built from
        are frozen.

        **An unchanged entry is left alone**, which is what makes this an upsert
        a caller can repeat: tearing the link down and rebuilding it would cost a
        fresh `npx` start and a new handshake for a call that changed nothing.
        The comparison is on the settings, and both sides came from the config
        layer, so a `${VAR}` reference is compared resolved-against-resolved.
        Comparing the raw argument against the resolved pool would never match,
        reconnecting on every identical `mcp_set`.
        """
        previous = self._held.get(settings.name)
        if previous is not None and previous.settings == settings:
            return previous
        one = self._hold(settings)
        self._held[settings.name] = one
        # **An edit to an entry that is already running restarts the server**, for
        # every home that holds it: the name is the identity, so there is one
        # server here and what changed is its configuration — not a second one
        # appearing beside it.  This is the half that keeps `mcp_set` working: a
        # link joined without being reconfigured would ignore the edit in silence.
        shared = (
            previous is not None
            and previous.link is not None
            and previous.link is one.link
        )
        if shared:
            assert one.link is not None  # `previous.link is one.link` is not None
            await one.link.reconfigure(
                settings,
                directory=self._directory,
                transport=self._transports.get(settings.name),
                client_factory=self._client_factory,
            )
        one.connecting()
        # And a link the new entry *does* hold is not let go of: the holders are
        # keyed by home, and both `Held`s here are this home's, so releasing the
        # old one would drop the hold the new one just took.
        if previous is not None and not shared:
            await previous.release()
        if previous is not None:
            logger.info("%s: replaced", settings.name)
        return one

    async def remove(self, name: str) -> Held | None:
        """Stop holding *name*, dropping its link.  `None` if it was not held.

        The pop is what the declaration sees, so it happens first for the reason
        `put` states — except that here the disappearance is the point, and the
        hub deleting the rows is the outcome being asked for.  The *server* goes
        only if this was the last home holding it (`Links.release`).

        Under the family's lock, for the reason `put` states: the pop and the
        release are two halves of one edit, and a `put` landing between them
        would leave a held entry pointing at a link this call has just closed.
        """
        async with self._manage:
            one = self._held.pop(name, None)
            if one is not None:
                await one.release()
            return one

    async def set_enabled(self, name: str, enabled: bool) -> Held | None:
        """Switch one held entry on or off.  `None` if it is not held.

        A rebuild rather than a flag on the live object: an enabled entry has a
        link and a disabled one does not, so this is `remove` and `put` and
        there is no third state where the settings say one thing and the
        connection is doing another.

        Takes the family's lock and calls `_put` directly, because `put` would
        try to take the same non-reentrant lock.
        """
        async with self._manage:
            one = self._held.get(name)
            if one is None or one.settings.enabled == enabled:
                return one
            return await self._put(replace(one.settings, enabled=enabled))

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
        await settle_attempts(one.attempt for one in self._held.values())

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
        *,
        words: FamilyWords,
    ) -> tuple[str, bool]:
        """Run one tool of one held entry.  `(text, ok)`, never raises.

        **A switched-off entry refuses, and this is the one place that has to
        say so.**  A disabled source's rows stay in the catalogue so a search
        still finds them (`slife2.plugins.toolhub.server`), which means the model can call one
        — and the hub routes by row without reading a status, so `Connection`
        would happily connect and spawn the process the operator had switched
        off.  The guard is on `call` rather than only on `settled` because this
        is the path a *call* takes, and the whole of what `enabled: false`
        promises is that a call is not a thing that starts it.

        **The refusal ends by naming the tool that switches the entry back on,
        so `words` comes in with the call.**  That name is the family's
        (`FamilyWords.enable_tool`, `mcp_set_enabled` / `rest_api_set_enabled`),
        and this class does not hold a vocabulary.  Taking it here rather than
        in the constructor is the same arrangement the payload builders use
        (`apply_and_report`, `switch_entry`): the answer is built once and the
        words come from the caller that knows them.

        **A sentence, and not JSON like the tools' own answers**, because this
        is not one of ours: it is the *result* of somebody else's tool, and the
        hub hands it to the model as the `text` of a call it made.  The
        contract that carries it is `call_source`'s (`{"text", "ok"}`), which the
        model never sees and which every call in this system keeps.
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
                f"`{words.enable_tool}` tool turns it on."
            ), False
        return await one.call(tool, arguments, meta)

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

        **And what it drops is this family's *hold*, not the server**: a link
        another home is still using outlives this one's letting go of it
        (`Links.release`), which is the rule everywhere else here.
        """
        for one in list(self._held.values()):
            await one.release()


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
    one.connecting()
    await settle_attempts([one.attempt])
    return one


def entry_report(one: Held, name: str, *, words: FamilyWords) -> dict[str, Any]:
    """One entry's state, and what it offers when it answered.

    **The three outcomes are the three things that can be true**, and the point
    is that they are distinguishable: a server that connected has a count, a
    disabled one has `off` and nothing to connect, and one that did not answer
    says so with the reason it gave rather than reporting a count of zero —
    which is what a server with no tools would also report.  Whether it answers
    *later* is not a claim this can make; what it can say is that the hub
    re-reads every declaration before each search.

    **The state is the one word `state_word` answers with**, and not a word of
    this family's own.  `connected` and `serving` were two vocabularies for one
    question the hub already answers the same way (`ready`, `connecting`,
    `failed`, `idle`, `off`), so a listing and a `set` could describe one link
    differently and a model held two things to learn for one fact.  `Held.state`
    is that word and reads the switch first, which is why a switched-off entry
    says `off` rather than the `idle` of a connection nobody started.

    **The count is the one number the tool can give before the work is paid
    for.**  Nothing here writes the catalogue: the hub merges this source's whole
    list before the next search.  The count is therefore what a caller reads to
    know how much a server brings with it.
    """
    if not one.settings.enabled:
        return {"name": name, "enabled": False, "state": "off"}
    report: dict[str, Any] = {
        "name": name,
        "enabled": True,
        "state": one.state,
    }
    if one.usable:
        # The count key is the unit's plural, so the number here and the rows a
        # `*_list_tools` hands over are named the same thing.
        report[f"{words.unit}s"] = len(one.rows or [])
    else:
        # What it said the last time it failed, or `""` for one that has not
        # answered yet — a different fact from a server that answered with
        # nothing, which is `usable` with a count of zero.
        report["error"] = one.error
    return report


async def apply_and_report(
    family: Family, settings: ToolServerSettings, *, words: FamilyWords
) -> dict[str, Any]:
    """Hold *settings* and answer with what happened to it.

    The upsert's own half of `switch_entry`'s answer: an entry that was just
    written and connected is the same entry as one that was switched on, so the
    payload is built once (`entry_report`) and both tools answer with it.

    A disabled entry is deliberately **not waited for**: there is no link to wait
    on, and `settled` is the one thing that must never start one.
    """
    one = await family.put(settings)
    if settings.enabled:
        await settled(one)
    return entry_report(one, settings.name, words=words)


def tools_payload(
    one: Held, name: str, limit: int, offset: int, *, words: FamilyWords
) -> dict[str, Any]:
    """One server's tools, as one page, with the state it was read in.

    **Three ways to have no tools, and the state says which.**  A switched-off
    entry, one that is not answering and one that answered with nothing all have
    an empty page, and folding them together would make this plugin tell a model
    two things about one entry — the model is the party that cannot check, so it
    is the party the disagreement costs.  The state is the answer, in the same
    words the listing and the hub use, so there is nothing to go and ask.

    The cap is announced rather than applied quietly (`total`), because a
    trimmed list that reads as the whole list is a model concluding a server
    cannot do something it can.

    `name` is the name in the model's own tool list — what a call to one of
    these is made with — and the far end's name for it is deliberately not
    repeated: it is what the plugin routes by, and a model that used it would be
    naming a tool that has no such spelling in its list.
    """
    rows = list(one.rows or [])
    page = [
        {
            "name": str(row.get("name") or ""),
            "description": str(row.get("description") or ""),
        }
        for row in rows
    ]
    answer: dict[str, Any] = {
        "name": name,
        "state": one.state,
        **listed(page, f"{words.unit}s", limit, offset, cap=TOOL_LIST_LIMIT),
    }
    if one.error:
        answer["error"] = one.error
    return answer


def refusal(config: Config, name: str) -> str | None:
    """Why *name* cannot become a source of this family, or `None`.

    **The collision it prevents is silent, which is the whole reason it is
    checked here.**  The hub routes by source name and refuses to know two owners
    for one: a `mcp:` entry called `agent` would be declared, dropped by
    `refresh_declared` as a name this hub is already connected under, and
    reported to the model as added — with no tools, and nothing anywhere saying
    why.  The same from the other side for `skills`, `cli` and `job`, which are
    the names those families file their own rows under; a source's verdict is
    written across every row it owns, so sharing one would mark somebody else's
    playbooks broken.

    Three names and not every reserved word: the rest of what a source may not be
    called — the plugin's own name, a category the hub owns — the hub refuses
    loudly at declaration time (`Upstream._record`), and duplicating that list
    here would be a second copy to keep in step.

    **One sentence, with no marker in front of it.**  `prepare_entry` raises it,
    so the failure is the call's status and the sentence is only the reason.
    """
    if name in config.plugins():
        return (
            f"'{name}' is a server slife2 itself runs, so its tools "
            f"would be dropped as a name the hub is already connected under. "
            f"Choose another name."
        )
    if name in (SKILLS_SOURCE, CLI_SOURCE):
        return (
            f"'{name}' is the name the `{name}:` section's rows are "
            f"filed under. Choose another name."
        )
    if name == JOB_SOURCE:
        return (
            f"'{name}' is the name the `jobs/` folder's rows are filed under. "
            f"Choose another name."
        )
    return None


# ── The management tools' shared half ───────────────────────────────────
# `mcp_tools` and `restapi_tools` serve the same four tools over one mechanism
# and answer with the same payloads; the words they differ by live in a
# `FamilyWords`, and the builders that consume it live here.  What stays in each
# family is the part that is genuinely its own: `*_set`'s argument list (the
# schema the model reads — `mcp_set` takes a command, `rest_api_set` a spec),
# and `*_list`'s `describe`, because an MCP entry is a command or a URL and a
# REST entry is a spec and a base URL.


@dataclass(frozen=True)
class FamilyWords:
    """One family's vocabulary — the words its tools' payloads vary by.

    The two connection families manage the same shape of entry through the same
    `Family` and answer with the same keys: a listing under a plural noun, an
    upsert reporting one entry's state, a removal reporting what it stopped.
    Every word that changes between them is a field here, so the answers can be
    built once.  Two builders is two chances for a model to learn two
    vocabularies for one mechanism.

    **What is *not* here is as deliberate as what is.**  A link's state is
    `state_word`'s one vocabulary, because the hub answers the same question the
    same way; the words here are the ones that name *things*: what a row is
    called, what a tool is called, which tool to reach for next.
    """

    section: str
    """The config section this family owns: `mcp` or `rest-api`."""

    collection: str
    """Its entries, plural, as the key a listing's rows sit under: `servers`."""

    entry: str
    """One entry, singular with its article: `a server` or `an API`."""

    unit: str
    """What one exposed item is: `tool` or `operation`.  It names the key a
    `*_list_tools` page sits under, and the count an entry report carries."""

    list_tool: str
    """The listing tool's name, so a refusal can point at it."""

    set_tool: str
    """The upsert tool's name, for the same reason."""

    enable_tool: str
    """The `*_set_enabled` tool's name.  It is *not* `set_tool`: one writes an
    entry and the other flips a switch on one that is already written, and a
    sentence that told a model to reach for the wrong one is the failure this
    whole class exists to make impossible."""

    remove_log: str
    """The `logger.info` event a removal writes."""


def state_word(raw: Mapping[str, Any], one: Held | None) -> str:
    """The one word a listing row's `state` carries for one entry.

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
    return one.state


def prepare_entry(
    config: Config,
    name: str,
    build: Callable[[], tuple[ToolServerSettings, Mapping[str, Any]]],
    *,
    section: str,
) -> ToolServerSettings:
    """Write one entry, or raise `ToolError` saying why it cannot be.

    Both refusals happen **before** anything is written: a name the hub is
    already using (`refusal`), and an entry the config layer will not parse.

    **Raised, and not returned.**  A refusal is this system's word for a call
    that failed — `slife2.clients.gateway` splits a peer's refusal from a
    transport fault on exactly that, and `slife2.plugins.toolhub.server` reads a
    raised `ToolError` as a value with `ok` false — while a sentence *returned*
    arrives with `ok` true and reads as a success, so the model reading it cannot
    tell that nothing was written.

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
        raise ToolError(why)
    try:
        settings, entry = build()
        configfile.upsert(section, name, entry)
    except ConfigError as exc:
        raise ToolError(str(exc)) from exc
    return settings


async def list_entries(
    family: Family,
    limit: int,
    offset: int,
    *,
    words: FamilyWords,
    describe: Callable[[Held | None, str, Mapping[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """The `*_list` answer: every entry as the file has it, with its state.

    Settles first, for the reason the hub's `servers()` does: this is the
    answer to "why is my tool missing", and a server that started a moment ago
    reads as one still starting — two different problems this tool exists to
    tell apart.  The row is the family's own, because an MCP entry is a command
    or a URL and a REST entry is a spec and a base URL.

    **An empty section is an empty page and not a sentence**: an answer naming
    another tool in prose is a cross-reference to keep in step, where `total`
    says the section is empty and the tool that writes an entry is one the model
    can read in its own list.
    """
    family.connecting()
    await family.settle()
    entries = configfile.read_section(words.section)
    rows = [describe(family.held(name), name, raw) for name, raw in entries.items()]
    return listed(rows, words.collection, limit, offset, cap=LIST_PAGE)


async def remove_entry(
    family: Family,
    name: str,
    *,
    words: FamilyWords,
    logger: logging.Logger,
) -> dict[str, Any]:
    """The `*_remove` answer: stop holding *name*, drop its entry.

    The file is consulted as well as the process, because a name this process
    never held but the operator wrote is still one to delete — and one in
    neither is refused rather than silently succeeding: nothing was written, so
    the call is a failure the model can see (`prepare_entry`'s rule).

    **The answer is one fact, because that is the one the caller acts on.**  The
    entry can be held and no longer in the file — another instance removed it,
    or a person edited it out — and then no entry was deleted but a link *was*
    stopped; both roads end with the name gone, which is what the caller asked
    for and all it can act on.  The two internals are still told apart in the
    refusal below, where the difference is whether there was anything to do.
    """
    if family.held(name) is None and name not in configfile.read_section(words.section):
        raise ToolError(
            f"'{name}' is not {words.entry} under `{words.section}:` — "
            f"see `{words.list_tool}`."
        )
    removed = configfile.remove(words.section, name)
    stopped = await family.remove(name)
    if removed:
        logger.info("%s name=%s", words.remove_log, name)
    return {"name": name, "removed": removed or stopped is not None}


async def switch_entry(
    family: Family, name: str, enabled: bool, *, words: FamilyWords
) -> dict[str, Any]:
    """The `*_set_enabled` answer: flip the switch and report what followed.

    The switch is the file's, and the link follows it — a rebuild rather than a
    flag, so there is no state where the two disagree (`Family.set_enabled`).

    A name the file does not have is refused rather than answered as a switch
    that was set: nothing was written, so the call failed.

    **Written but not held is a state of its own** (`not held`), and it is not a
    failure: this process did not have the entry — another instance added it
    since this one read the config — and the file is right, so the next start
    holds it.
    """
    if name not in configfile.read_section(words.section):
        raise ToolError(
            f"'{name}' is not in `{words.section}:` — `{words.set_tool}` adds it, "
            f"`{words.list_tool}` shows what is there."
        )
    configfile.set_enabled(words.section, name, enabled)
    one = await family.set_enabled(name, enabled)
    if one is None:
        return {"name": name, "enabled": enabled, "state": "not held"}
    if enabled:
        await settled(one)
    return entry_report(one, name, words=words)


async def list_entry_tools(
    family: Family, name: str, limit: int, offset: int, *, words: FamilyWords
) -> dict[str, Any]:
    """The `*_list_tools` answer: one entry's exposed items, as one page.

    A name this process does not hold is refused rather than answered with an
    empty page: the entry exists nowhere this can look, and `total: 0` would read
    as a server that offers nothing.
    """
    one = family.held(name)
    if one is None:
        raise ToolError(
            f"'{name}' is not {words.entry} this process holds — "
            f"see `{words.list_tool}`."
        )
    # Waits for the link, for the reason `list_entries` does: what this reports
    # is the items the entry *offers*, and a page that is empty because the link
    # is still coming up is a different answer from one that answered with
    # nothing — and `state` is which of them it is.
    await settled(one)
    return tools_payload(one, name, limit, offset, words=words)


@dataclass
class Documents:
    """A family whose rows are documents: one config section, no connection.

    `skills:` and `cli:` are the two (SDD §7.1 is the argument for giving
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


class Homes[T]:
    """One of `T` per home, built the first time that home asks.

    **A plugin holds a family per home, and this is what makes sharing it
    honest.**  What a family holds is its section of `slife2.yaml` — the entries,
    their switches, and what it declares — and that section belongs to a home
    (`slife2.libs.host.paths.home`).  One family for the process would serve every
    home the entries of whichever data directory started it, which is the leak
    this closes.  **The connections are not one of those per-home things**: they
    are the machine's and pooled by name (`Links`), so two homes naming one entry
    run one server between them.

    **Built by the first ask from that home**, for the reason the tool catalogue
    is (`slife2.plugins.toolhub.server.Catalogue.store`): a home is a data
    directory this process may not hear about until somebody in it asks
    something, and building for one nobody is using is work plus connections
    nobody asked for.  **A start in a home counts as that ask**, and is the
    commonest one there is — the client makes it before it opens a window
    (`slife2._open_catalogue`) — so a home an agent has been started in is built
    then rather than at its first turn.

    `build` is what to make, and **`source` decides which config it is made
    from**: the home's own file in production, the one a caller injected in a
    test (`slife2.libs.host.config.for_the_call`).  A holder that always read the
    file would be one that ignores a config handed to `build_server`, and every
    test in this suite builds its server that way.  `injected` is therefore
    required and `source` is what makes it lose: neither is optional, because
    "which config" is the whole question this class answers.

    **`current()` and not `__getattr__`.**  Forwarding attribute access to
    whichever family is in front of us reads well at a call site and is a patch:
    it hides, at every use, the fact that the answer depends on the call, and it
    makes an object that is only *sometimes* a `Family` look like one.  The four
    plugins that register management tools say `homes.current()` once per tool
    body instead, which is one line and no surprise.

    **`on_build` is the one thing the two kinds of family do differently.**  A
    `Family` begins connecting as it is held (`Family.start`: the asks that
    follow have a bounded wait in them, so a server that came up with this
    process has had its start-up to answer by then), and a `Documents` mirror has
    nothing to start at all.  Said here as a parameter rather than guessed with a
    `getattr` at the call: which of the two this is, is known where it is built.
    """

    def __init__(
        self,
        build: Callable[[Config], T],
        source: Callable[[], Config] | None,
        injected: Config,
        on_build: Callable[[T], None] | None = None,
    ) -> None:
        self._build = build
        self._source = source
        self._injected = injected
        self._on_build = on_build
        self._held: dict[str, T] = {}

    def current(self) -> T:
        """This home's, built and started on the way out of the first ask."""
        key = home_key()
        held = self._held.get(key)
        if held is None:
            held = self._build(for_the_call(self._source, self._injected))
            self._held[key] = held
            if self._on_build is not None:
                self._on_build(held)
        return held

    def every(self) -> list[T]:
        """Every home's, for the two ends of this process's life."""
        return list(self._held.values())


def build_document_server(
    config: Config,
    *,
    name: str,
    instructions: str,
    catalogue_source: str,
    category: str,
    section: str,
    description: str,
    entries: Callable[[Config], Mapping[str, Any]],
    rows: Callable[[Mapping[str, Any]], list[dict[str, Any]]],
    management: Callable[[FastMCP, Homes[Documents]], None],
    source: Callable[[], Config] | None = None,
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

    `entries` is the config's own mapping for that section, taken from the
    config of the call that first needed it (a function, for the reason
    `build_family_server` gives: the section belongs to a home), and copied into
    a mirror **one per home** (`Homes`); `rows` turns a mirror into catalogue
    rows, which is the one thing a family cannot share because a playbook's row
    *is* its document and a command's is its invocation.

    A document family has nothing to start and nothing to close — the mirror is
    a dictionary — so a home's copy is made when it is first asked for and
    nothing else has to happen at either end of the process's life.

    `catalogue_source` and not `source` is the name these rows carry, because
    `source` here is the other thing — the live config reader this builder shares
    with `build_family_server`.
    """
    family: Homes[Documents] = Homes(
        lambda theirs: Documents(
            source=catalogue_source,
            category=category,
            section=section,
            entries=dict(entries(theirs)),
        ),
        source,
        config,
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
        held = family.current()
        return held.declare(description, rows(held.entries))

    management(mcp, family)
    return mcp


def build_family_server(
    config: Config,
    *,
    name: str,
    instructions: str,
    category: str,
    entries: Callable[[Config], Iterable[ToolServerSettings]],
    words: FamilyWords,
    source: Callable[[], Config] | None = None,
    transports: Mapping[str, Callable[[ToolServerSettings], Any]] | None = None,
    client_factory: ClientFactory | None = None,
    management: Callable[[FastMCP, Homes[Family]], None] | None = None,
) -> FastMCP:
    """One family, as the MCP server slife2 starts.

    Two tools are the whole of the plugin contract this kind of plugin has
    *towards the hub*: what it holds, and running one.  Neither is marked for the
    model, and neither should be — the model is given the tools of the *entries*,
    merged by the hub, and never these.  What the model gets from this process is
    what `management` registers, which is a different surface with a different
    reader (see `slife2.libs.plugin.family`'s own note on `refusal`, and SDD §7.6).

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
    an entry written to the file is connected in the same breath — is here, and
    it is **one per home** (`Homes`): the section a family holds is the home's,
    so the entries are read from the config of the call that first needed them
    rather than from the one this process started with.  `entries` is therefore
    a function of the config — `lambda theirs: theirs.mcp.values()` — and the one
    config it is *not* a function of is an injected one, which wins
    (`slife2.libs.host.config.for_the_call`).

    `source` is `load_cached` in production: it is what makes a section edited by
    hand, or by one of these management tools, reach the next home that asks.

    **`words` is here rather than only in `management`** because one sentence is
    the *family's* own and is written without a management tool asking for it:
    `Family.call`'s refusal of a switched-off entry, which names the tool that
    switches it back on.  That name differs between the two families
    (`FamilyWords.enable_tool`), and the call that writes the sentence takes the
    vocabulary with it — a second copy of the name here would be the drift this
    class exists to prevent.
    """
    #: **One pool for the process, handed to every home's family.**  The links are
    #: the machine's (`Links`), so this is what makes two homes naming one entry
    #: share one process — and it is built here rather than at module level so
    #: that two servers in one process (which is what a test builds) do not share
    #: a pool, and a family cannot inherit a process from the previous test.
    links = Links()
    family: Homes[Family] = Homes(
        lambda theirs: Family(
            category=category,
            entries=entries(theirs),
            links=links,
            transports=transports,
            client_factory=client_factory,
        ),
        source,
        config,
        on_build=Family.start,
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        """Hold the links while the server is up, and drop them on the way out.

        Not per client, for the reason the hub's is not: this process's pool is
        the process's, and a client disconnecting is not a reason to restart
        every stdio server behind it.
        """
        for one in family.every():
            one.start()
        try:
            yield {}
        finally:
            for one in family.every():
                await one.close()

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
        return await family.current().declare()

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
        text, ok = await family.current().call(
            source, tool, arguments, forwarded_client(request_meta(ctx)), words=words
        )
        return {"text": text, "ok": ok}

    if management is not None:
        management(mcp, family)

    return mcp
