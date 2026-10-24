"""A2A — agent-to-agent traffic over MQTT, on the published profile.

The mesh is a package for the reason `media` and `file_tunnel` are ones: there
are three subjects in here and they are worth separating.  `mesh.py` is the
driver — the SDK's `Responder` for the inbound lifecycle, a persistent client of
our own for the outbound half, presence, and the identity check that keeps two
processes from taking one name over.  `inbound_store.py` is the per-agent ledger
of tasks whose completion bridge this process holds.  `task_store.py` is the
in-memory record of what we sent — and, with the ledger, what `task_list`
reports.  `server.py` is the plugin slife2 starts: the tools, one mesh per
agent, and the hand-over that puts an inbound message into a conversation.

Where the config section's types live is `slife2/libs/host/config.py` with every other
section's (`A2ASettings`): this project has one config file and one writer for
it.

**The wire is not ours.**  The topics, the retained agent card, the JSON-RPC 2.0
envelopes and their MQTT v5 properties, the task lifecycle and its per-task
dedup, and the requester's retry ladder are all the `a2a-over-mqtt` SDK's.  What
this package writes is the harness glue around it, and the split is deliberate:
interop with the rest of the ecosystem follows from the wire being somebody
else's, and a self-built binding would be a second protocol wearing the name.

**Two things are deliberately absent.**  `broker.py` — a TCP probe that decides
whether to start the plugin at all — has no counterpart, because
`slife2.libs.host.runtime.tcp_listening` already is that and there is no spawn to gate: a
plugin the config names is always started (SDD §4.2), and what the section decides is
whether the plugin opens a mesh.  And `identity.py`, the *inbox* vocabulary
(`AgentName`, `Channel`, `AgentMessage`), is not here at all: this build
carries a channel as the string a turn records (`peer_agent:<peer>` on this
network, `agent:`/`subagent:` on the local one), drawn by
`slife2.tui.widgets.channel_prefix`.
"""
