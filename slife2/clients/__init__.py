"""The client half of a hop: one module per peer this side dials.

`slife2` is one client of many servers, and every hop it makes has two
halves.  The server halves are under `plugins/`; these are the others —
`toolclient` for the hub, `model` for a model server, `embedder` for the
embeddings endpoint, and `gateway` for a server somebody else runs.
"""
