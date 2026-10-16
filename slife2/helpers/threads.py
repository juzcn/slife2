"""The thread hop: how a synchronous call is kept off the event loop.

`asyncio.to_thread` is the mechanism and this is the name for it, because the
rule needs saying rather than the call needing wrapping.

**Being `async` is not what keeps the loop free.**  SQLite here is blocking: a
statement issued on the loop holds it for as long as the disk takes.  Every
server in this system answers *every* agent instance from one loop — that is what
a shared server is — so a blocking call on it does not slow one conversation
down, it stalls all of them: a turn with nothing to do with the cabinet waits for
the cabinet's write, and a hub's catalogue read stalls the conversations that are
not searching anything.

**The hop is safe because nothing is shared across it.**  Every store in this
tree opens a connection per call and closes it before returning, which is what
makes handing the call to a worker thread sound — a connection is not shareable
across threads, so there is none to share.  A store that kept one would need more
than a hop.

Not every `asyncio.to_thread` in the tree goes through this: a one-off hop inside
a function that already explains itself does not need a second name.  It is the
places where *every* call of a store is hopped — the turn store, the cabinet, the
context store, the hub — that want the rule stated once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def off_loop[**P, T](
    function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run one blocking call on a worker thread, and await its answer.

    `ParamSpec` rather than `*args: Any`, so the call sites are checked the same
    way a direct call would be: this is where a store's signature meets the
    thread hop, and a runner that accepted anything would be the one place in the
    tree where a wrong argument is not an error.
    """
    return await asyncio.to_thread(function, *args, **kwargs)
