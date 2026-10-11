"""slife2-media — generation: images, video, speech and transcripts.

v1's `media` plugin, as a package because it is a *family* of endpoints rather
than one: `server.py` is the plugin slife2 starts, and `adapters.py` is the
translation layer between one contract and each vendor's HTTP.  Where the config
section's types live is `slife2/config.py` with every other section's — the
plugin owns the section and reads it, but this project keeps the parsing in one
place, which is the one thing v1's own layout (`plugins/media/config.py`) did
differently.
"""
