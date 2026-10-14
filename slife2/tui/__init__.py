"""The Textual front end — an MCP client, and nothing else.

The TUI holds no conversation state, no provider knowledge, and no API key.  It
sends a prompt and renders what comes back, which is the only role the MCP split
leaves it.

**"And nothing else" is true again, and it was not for a while.**  The window
used to *serve* an MCP server of its own — on a `tui:` section that nothing
started — so it was a client at one end and a server at the other.  That server
is now `slife2.plugins.tui.server`, a plugin like every other one and filed with
every other process under `plugins/`, and the window is only ever its client:
`slife2/tui/client.py` names its tools and speaks its wire format without
importing anything that renders.
"""
