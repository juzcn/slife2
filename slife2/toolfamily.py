"""A plugin that holds somebody else's MCP servers, and declares them.

**This is where a connection lives now.**  Until this module existed, the toolhub
opened one link per configured entry, merged what each offered, and was therefore
the only process that could reach a server.  The hub is the tool *set's* owner —
which tools exist, what the model is holding, what the budget takes back — and
holding twenty connections is not that job; it is the job of the process that
owns the config section they were written in.  So `tools:` and `rest-api:` each
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
from typing import Any

from fastmcp import Context, FastMCP

from slife2.audience import forwarded_client, request_meta
from slife2.config import Config, ToolServerSettings
from slife2.gateway import ClientFactory, Connection, mcp_config, proxied_name
from slife2.mcp_server import CALL_SOURCE, LIST_SOURCES, house_server
from slife2.paths import data_dir

logger = logging.getLogger(__name__)

#: How long one `list_sources` waits for the connects it started, and it is the
#: same compromise the hub makes about its own links.  Answering instantly means
#: a source that is still starting reads as one that is down, and the first tool
#: list of a session is missing every external tool; waiting without a bound
#: means a cold `npx` holds a turn open for a minute.  So: wait, but briefly, and
#: only for attempts that have already begun.
LIST_SETTLE_SECONDS = 5.0


class Held:
    """One configured entry: its link, and the rows it last offered.

    The rows are built from the listing the gateway hands over — the entry's name
    in front of each tool's own (`proxied_name`) and the schema the far end
    published — because that is what the hub merges, and building them here is
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
        """One tool of this entry, as the catalogue's row for it."""
        schema = getattr(tool, "input_schema", None)
        return {
            "name": proxied_name(self.settings.name, str(getattr(tool, "name", ""))),
            "description": str(getattr(tool, "description", "") or ""),
            # What the far end calls it, which is not what the model calls it:
            # the advertised name is sanitised and carries the server in front.
            "remote_name": str(getattr(tool, "name", "")),
            "schema": json.dumps(schema, ensure_ascii=False) if schema else "",
            "status": "enabled",
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
        directory = str(data_dir())
        wired = transports or {}
        self._held = {
            entry.name: Held(
                entry,
                directory=directory,
                category=category,
                transport=wired.get(entry.name),
                client_factory=client_factory,
            )
            for entry in entries
        }

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

        **Wait, never cancel** — the same rule the hub's own `settle` states, and
        for the same reason: giving up on *waiting* for a connection is not a
        reason to stop making it.
        """
        pending = [
            attempt
            for one in self._held.values()
            if (attempt := one.connection.attempt) is not None
        ]
        if pending:
            await asyncio.wait(pending, timeout=LIST_SETTLE_SECONDS)

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
        """Run one tool of one held entry.  `(text, ok)`, never raises."""
        one = self._held.get(source)
        if one is None:
            # The hub routes by what it was told, so this is a hub that has a row
            # this family did not declare — an entry removed in the last moment,
            # or a name it invented.  Said as what it is.
            return f"{source!r} is not a server this plugin holds any more", False
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
        for one in self._held.values():
            await one.connection.close()


def build_family_server(
    config: Config,
    *,
    name: str,
    instructions: str,
    category: str,
    entries: Iterable[ToolServerSettings],
    transports: Mapping[str, Callable[[ToolServerSettings], Any]] | None = None,
    client_factory: ClientFactory | None = None,
) -> FastMCP:
    """One family, as the MCP server slife2 starts.

    The two tools are the whole of the plugin contract this kind of plugin has:
    what it holds, and running one.  Neither is marked for the model — the model
    is given the tools of the *entries*, merged by the hub, and never these.

    `transports` and `client_factory` are the same two seams the hub has and for
    the same reason: a test drives the whole thing over in-memory servers, with
    no process and no port, and a test that needs a client which misbehaves needs
    to say so from outside this module.
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

    return mcp
