"""The Textual front end — an MCP client, and nothing else.

The TUI holds no conversation state, no provider knowledge, and no API key.  It
sends a prompt and renders what comes back, which is the only role the MCP split
leaves it.

**"And nothing else" is true again, and it was not for a while.**  The window
used to *serve* an MCP server of its own — on a `tui:` section that nothing
started — so it was a client at one end and a server at the other.  That server
is now `slife2.tui.control`, a plugin like every other one, and the window is
only ever its client.  The module stays in this package for the reason
`slife2.media.server` lives in `slife2.media`: it is the one that belongs to this
subsystem, and the client beside it names its tools and speaks its wire format
without importing anything that renders.
"""
