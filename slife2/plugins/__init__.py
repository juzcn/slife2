"""The processes: one directory per plugin, and the `server.py` inside it
is the one `slife2` starts.  A plugin directory also holds whatever
only that plugin uses — `a2a/mesh.py`, `media/adapters.py` — because a
capability goes where the plugin that owns it is.
"""
