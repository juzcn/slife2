"""slife2-file-tunnel — a public HTTPS URL for a local file.

v1's `sharefile` plugin, as a package for the same reason `media` is one: there
are two subjects in here and they are worth separating.  `providers.py` is "how
this machine's port becomes a public URL" — ngrok, localhost.run, a Cloudflare
Quick Tunnel — and `server.py` is the plugin slife2 starts: the registry of what
has been exposed, the `GET /share/{token}` route that serves the bytes, and the
tools.

Where the config section's types live is `slife2/config.py` with every other
section's, which is the one thing v1's layout (`plugins/sharefile/config.py`,
plus a standalone `sharefile.yaml`) did differently — this project has one
config file and one writer for it.
"""
