"""Talking to a model, across a process boundary.

Nothing in this package holds a provider SDK except the server modules, one per
wire protocol.  The agent loop's only backend is :mod:`slife2.llm.client`, which
speaks MCP to whichever server the config points at — so the loop can be run
against a provider it has no code for, and a provider's credentials never enter
its process.

Layers, in dependency order::

    base.py           Chunk, ToolCallDelta, Stream, LLMBackend  (no I/O)
    wire.py           Chunk <-> progress payload                (no I/O)
    client.py         MCPBackend: LLMBackend over MCP           (client side)
    server_common.py  what the model servers share: one `stream_chat` tool,
                      the progress encoding, and tool-call assembly
    openai_server.py            slife2-llm-openai            <- imports openai
    openai_responses_server.py  slife2-llm-openai-responses  <- imports openai
    anthropic_server.py         slife2-llm-anthropic         <- imports anthropic
    embeddings_server.py        slife2-llm-embeddings        <- imports openai

The two OpenAI modules are separate because they are separate *protocols*: the
Responses API takes a different input shape, names its tools differently and
streams different events, so it gets its own process like any other wire format.

The embeddings server is the one that is not a *model* backend and is still
here, because the rule the package is organized by is the wire format and not
the caller: it speaks the OpenAI-compatible API, so it is the same adapter, and
the fact that two other plugins rather than the agent loop reach it changes
nothing
about what it has to do.

Serving a server at all — the flags, the HTTP transport, the record that says a
daemon is here — is deliberately *not* in this package.  It is not an LLM
concern, and every other server needs it too, so it lives in
:mod:`slife2.mcp_server`.  Neither of those two should have to import
`slife2.llm` to be a server.

Nothing is re-exported here on purpose: importing `slife2.llm` should not pull
in either SDK, and a wildcard export would eventually do exactly that.
"""
