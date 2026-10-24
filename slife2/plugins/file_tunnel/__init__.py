"""slife2-file-tunnel — a public HTTPS URL for a local file.

It is a package for the same reason `media` is one: there are two subjects in
here and they are worth separating.  `providers.py` is "how this machine's port
becomes a public URL" — ngrok, localhost.run, a Cloudflare Quick Tunnel — and
`server.py` is the plugin slife2 starts: the registry of what has been exposed,
the `GET /share/{token}` route that serves the bytes, and the tools.

Where the config section's types live is `slife2/libs/host/config.py` with every
other section's: a `plugins/sharefile/config.py` plus a standalone
`sharefile.yaml` would be a second config file and a second writer, and this
project has one of each.
"""
