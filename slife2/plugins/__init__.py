"""The processes: one directory per plugin, and the `server.py` inside it
is the one `slife2` starts.  A plugin directory also holds whatever
only that plugin uses — `a2a/mesh.py`, `media/adapters.py` — because a
capability goes where the plugin that owns it is.

**Nothing else lives here**, and that is the rule this package is: a
module at this level would be a file with no `main` and no
`SERVER_NAME` in a directory whose every other entry is a process.
What every plugin *sits on* — `protocol`, `audience`, `family` — is a
library, and lives in `slife2.libs.plugin`.
"""
