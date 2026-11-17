"""A2A — agent-to-agent traffic over MQTT, on the published profile.

The mesh is a package for the reason `media` and `file_tunnel` are ones: there
are several subjects in here and they are worth separating.  `mesh.py` is the
driver — the responder's harness bridge, a persistent client of our own for the
outbound half, presence, and the identity check that keeps two processes from
taking one name over.  `responder.py` is the inbound lifecycle itself, derived
from the SDK's responder because the profile's interrupted-task continuation
needs a state machine the SDK does not have.  `inbound_store.py` is the
per-agent ledger of tasks whose bridge or pause this process holds.
`task_store.py` is the in-memory record of what we sent — and, with the ledger,
what `task_list` reports.  `server.py` is the plugin slife2 starts: the tools,
one mesh per agent, and the hand-over that puts an inbound message into a
conversation.

Where the config section's types live is `slife2/libs/host/config.py` with every other
section's (`A2ASettings`): this project has one config file and one writer for
it.

**The vocabulary is the SDK's; the responder lifecycle is ours.**  The topics,
the retained agent card, the JSON-RPC 2.0 envelopes and their MQTT v5
properties, and the protocol's classification helpers are the
`a2a-over-mqtt` SDK's.  The responder's state machine is a local derivation
(`responder.py`, Apache-2.0): the profile's `input-required`/`auth-required`
continuation, terminal-task rejection and `GetTask` have no counterpart in SDK
0.1.0, and upstream `main` is identical.  The requester's retry ladder and the
two stores are ours.  The split is deliberate: interop follows the published
profile, and a self-built binding would be a second protocol wearing the name.

**`broker.py` probes, owns, and stops the broker with the stack** (SDD §9.2).
A config that names the mesh has said it wants a broker, so a local one that
is absent is started — mosquitto, on the concrete loopback address the section
names — and recorded, so the take-down stops exactly what this machine started
and leaves somebody else's alone.  A broker that still will not serve fails the
start loudly rather than attaching a tool surface to nothing; a wildcard
address is refused by name, and a broker on another machine is the operator's
to run.

**`identity.py`, the *inbox* vocabulary, is deliberately absent.**  This build
carries a channel as the string a turn records (`peer_agent:<peer>` on this
network, `agent:`/`subagent:` on the local one), drawn by
`slife2.tui.widgets.channel_prefix`, so there is no `AgentName`/`Channel`/
`AgentMessage` module for a message to wear.
"""
