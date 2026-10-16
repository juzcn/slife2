"""slife2-subagent — the local network, which is the same mesh one section over.

An agent reaches two kinds of agent, and they are different in kind rather than
in namespace (sdd §3.5): its **remote** peers, which may be on another machine
and on a broker somebody else runs, and the **subagents** it made, which are
directories under its own.  Both are served by `slife2.plugins.a2a.server` — the
protocol, the mesh, the envelopes and the four tools are one implementation —
and what this module is is that server told *which* network to be: its own
section (`subagent:`), its own names (`subagent_*`), its own channel and its own
envelope word.

**That the local network is A2A is a technical fact and not a meaning.**  Nothing
a person or a model sees says so: there is no `a2a` in its section, its server
name, its tools or its channel.  What it *is* is an agent and the agents it made,
meeting where they can be reached — and this build reaches them with the
machinery that was already here, which is the whole reason it is a second
`main` rather than a second implementation.
"""

from __future__ import annotations

from slife2.libs.host.config import LOCAL
from slife2.plugins.a2a.server import main as a2a_main

#: The MCP name this process answers as, read off the row `PLUGINS` files this
#: module under (`Net.server_name` is the same call).  **Declared even though
#: nothing here reads it**, because it is half of what makes this module a
#: plugin: `slife2.plugins.a2a.server` takes it from the `net` it is handed, and
#: a reader — or a test — arriving at the module the launcher starts should find
#: the name it answers as without having to follow that argument.
SERVER_NAME = LOCAL.server_name


def main(argv: list[str] | None = None) -> int:
    """Serve the local network.  `slife2.libs.host.launcher` starts this."""
    return a2a_main(argv, net=LOCAL)


if __name__ == "__main__":
    raise SystemExit(main())
