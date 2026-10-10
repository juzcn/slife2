"""The tool registry: what the loop can call, and what happens when a call fails.

This is the loop's *vocabulary* for tools and nothing more — `Tool` is a spec
and a function, `ToolRegistry` looks one up and runs it, and both are unaware
that anything is served over a socket.  The tools themselves live where they are
served: `slife2.builtins` for the ones slife2 ships, and whatever server the
toolhub reaches for everything else.  That split is deliberate — `loop.py`
imports this module, and the loop must not import a server.

The registry's one interesting property is that :meth:`ToolRegistry.execute`
**never raises**.  A tool that fails produces error *text*, which is handed back
to the model as the tool's result.  That is deliberate: the model can read
"unknown tool 'wether'" and correct itself, where an exception would end the
turn and lose the conversation's thread.  The error path is a feedback channel,
not a failure mode.

One kind of tool, and four questions
------------------------------------
**There is one kind of tool here** — a name, a schema and a body — and one thing
that varies about it, which is who calls it:

* a **model tool** is one the model chooses: it reads the spec and calls it;
* a **harness tool** is LLM-visible and **auto-invoked** — v1's definition, and
  both halves are load-bearing.  *Auto-invoked* is what makes it one: the
  machinery calls it on the model's behalf, at a moment the machinery decides,
  and it would be called whether or not the model ever chose it.  *LLM-visible*
  is why there are so few: the call is written into the conversation as a pair,
  and a pair has to name a tool the request declares.  **There are two**,
  `_func_tool_unload` and `_check_new_input`, and that is the whole class.

**And the guard has to be there, because there is nowhere else for it to be.**  A
pair has to name a declared tool, so a harness tool is in the model's list, where
the model sees it — and nothing can stop it being called: a provider hands us a
tool call and the only refusal this system has is a tool that answers.  So the
class is not protected by forbidding the call, and cannot be.
**A harness tool has to be harmless when the model calls it, or it cannot be
one.**

That is a condition, not an invitation.  Each of the two is the *machinery's*
move, made at a moment the machinery knows is the right one, and a call from the
model is not wanted — the boundary is how a message is meant to arrive.  What the
design guarantees is only that the unwanted call is not a damaging one:
`_func_tool_unload` refuses the names the system works by and can otherwise only
shorten the model's own list, and `_check_new_input` hands over a message the next
boundary would have handed over anyway, so a call from the model is the same thing
sooner rather than something new.  A tool that *would* do damage there is not
protected from being one; it is simply not one — it stays undeclared, which is
what the audience gate is for.

A leading `_` is the mark on those two names (:data:`HARNESS_PREFIX`) and, more
widely, on any name that is the machinery's rather than the model's — it is a
convention about names, not a definition of the class.  It is on the *name*
because names are what the askers hold: the catalogue holds one in a query, the
TUI reads one out of a rebuilt message, and the loop mints one for a pair.

A tool the *harness* calls and the model never sees is not one of these — it is
an API, like `send_message` for the TUI or `restore` and `rebuild` for the agent
server.  It needs no mark, no place in the model's list and no pair, because
nothing about it is written into a conversation.

**Four facts hang off a tool, and each is asked in exactly one place.**  The
first is the one the mark answers; the mistake to avoid is reading it as an
answer to the other three:

* **who calls it** — :data:`HARNESS_PREFIX`;
* **may the model be given it** — `slife2.audience` for ours, and the operator's
  config for everybody else's;
* **may the model unload it** — `slife2.toolhub.ALWAYS_LOADED`;
* **does a screen draw it** — `slife2.tui.restore`.

The middle two do line up with the mark for the tools the harness writes *pairs*
with — a pair has to name a tool the request declares, so those names are in the
model's list and are protected from being unloaded — but that is a consequence
of the pair rule and not the meaning of the mark.  `skill_use` is not a harness
tool and is protected the same way; `_check_new_input` is one, and is drawn by
nothing at all today, for a reason that has nothing to do with who calls it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from slife2.messages import ToolCall, ToolSpec

logger = logging.getLogger(__name__)

#: How a tool is implemented: arguments in, result text out.  Returning text
#: rather than raising is the convention — see the module docstring.
ToolFunc = Callable[[dict[str, Any]], Awaitable[str]]

#: The mark of a harness tool — a name the machinery calls rather than one the
#: model chooses.  Spelled once because four modules ask about it, and the
#: module docstring is where the four questions it does *not* answer are listed.
HARNESS_PREFIX = "_"


def is_harness(name: str) -> bool:
    """Whether a tool name is the harness's rather than the model's.

    Takes a name and not a tool, because the mark is on the name and names are
    what the askers hold: the catalogue stores them, the TUI reads one out of a
    rebuilt message, and the loop mints one for a pair.
    """
    return name.startswith(HARNESS_PREFIX)


class ToolFailed(Exception):
    """A tool that failed *without* anything going wrong inside it.

    The distinction is what the exception's name would have carried.  When a
    tool raises by itself, the class is the useful part of the message —
    `KeyError: 'e'` tells the model which argument it got wrong, and
    `ZeroDivisionError` tells it what to avoid.  But a tool that proxies another
    process has already been *told* it failed, and its own message is the whole
    story: naming this class in front of it would add a word that was never in
    the error and that no model can act on.

    So this is the one exception `ToolRegistry.execute` renders bare, and it
    exists so that "the call failed" can reach the transcript as a failure.  A
    hub-backed tool that returned error text as an ordinary result would show up
    as a success with a discouraging message under it, which is worse than
    either.
    """


@dataclass(frozen=True)
class Tool:
    """A spec the model sees, and the function behind it."""

    spec: ToolSpec
    run: ToolFunc


class ToolRegistry:
    """The loop's view of what it can call."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {t.spec.name: t for t in tools or []}

    @property
    def specs(self) -> list[ToolSpec]:
        """What to advertise to the model."""
        return [t.spec for t in self._tools.values()]

    async def execute(self, call: ToolCall) -> tuple[str, bool]:
        """Run a call, returning `(result_text, ok)`.

        Never raises.  An unknown name, bad arguments, or a tool that throws all
        come back as `ok=False` with a message a model can act on.
        """
        tool = self._tools.get(call.name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "(none)"
            return f"Error: unknown tool {call.name!r}. Available tools: {known}", False

        try:
            return await tool.run(call.arguments), True
        except ToolFailed as exc:
            logger.warning("tool %s failed: %s", call.name, exc)
            return f"Error: {exc}", False
        except Exception as exc:  # noqa: BLE001 - a tool failure is a message, never an exception
            # Includes the exception type: "KeyError: 'e'" tells the model which
            # argument it got wrong, where "tool failed" does not.
            logger.warning("tool %s raised: %s", call.name, exc)
            return f"Error: {type(exc).__name__}: {exc}", False
