"""Talking to a model, across a process boundary.

Nothing in this package holds a provider SDK except the two server modules.
The agent loop's only backend is :mod:`slife2.llm.client`, which speaks MCP to
whichever server the config points at — so the loop can be run against a
provider it has no code for, and a provider's credentials never enter its
process.

Layers, in dependency order::

    base.py           Chunk, ToolCallDelta, Stream, LLMBackend  (no I/O)
    wire.py           Chunk <-> progress payload                (no I/O)
    client.py         MCPBackend: LLMBackend over MCP           (client side)
    server_common.py  shared scaffolding for the two servers
    openai_server.py  slife2-llm-openai     <- imports openai
    anthropic_server.py  slife2-llm-anthropic  <- imports anthropic

Nothing is re-exported here on purpose: importing `slife2.llm` should not pull
in either SDK, and a wildcard export would eventually do exactly that.
"""
