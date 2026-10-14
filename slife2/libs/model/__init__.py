"""The model, as a wire: `Chunk`, `Stream` and `LLMBackend`, and the
progress encoding that carries one across a process boundary.  No I/O,
and no SDK — the client is `slife2.clients.model` and the servers are
under `plugins/llm/`.
"""
