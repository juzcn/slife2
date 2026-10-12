"""The tool registry: what the loop can call, and what happens when a call fails.

This is the loop's *vocabulary* for tools and nothing more — `Tool` is a spec
and a function, `ToolRegistry` looks one up and runs it, and both are unaware
that anything is served over a socket.  The tools themselves live where they are
served: whatever server the toolhub reaches, our plugins and somebody else's
alike.  That split is deliberate — `loop.py` imports this module, and the loop
must not import a server.

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

**And three arguments no tool declares.**  `_timeout`, `_async` and `_approve`
are read off every call and stripped before dispatch — the one part of this
module that is about the *call* rather than about the tool, and the reason it is
here rather than in the loop is that their names are vocabulary while their
rules are a contract the model reads in the prompt.  What *enforces* them lives
elsewhere: the approval gate and the clock in `slife2.loop`, the background in
`slife2.async_tasks`.  The section comment above :class:`Meta` is where the
three are argued.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
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

#: The trim, by name.  The one `_`-prefixed tool the *model* also sees, because
#: the trim is written into the conversation as a tool pair and a pair has to
#: name a tool the request declares (the module docstring's exception).
#:
#: **Spelled once, because the two ends are a hop apart.**  The agent server
#: calls it (`slife2.toolclient`) and the hub serves it (`slife2.toolhub`), and
#: each used to carry its own copy of the string: a rename on one side would
#: have been a trim that stopped working with a log line and nothing else.
FUNC_TOOL_UNLOAD = "_func_tool_unload"


def is_harness(name: str) -> bool:
    """Whether a tool name is the harness's rather than the model's.

    Takes a name and not a tool, because the mark is on the name and names are
    what the askers hold: the catalogue stores them, the TUI reads one out of a
    rebuilt message, and the loop mints one for a pair.
    """
    return name.startswith(HARNESS_PREFIX)


# ── The three per-call meta-parameters ────────────────────────────────────────
#
# **Every tool takes three arguments it does not declare.**  `_timeout` says how
# long this one call may take, `_async` says to run it in the background, and
# `_approve` says to ask the operator first.  They are a system-prompt contract
# and not schema fields, which is v1's decision and its reason: re-describing
# three parameters on every one of a hundred schemas is the largest per-request
# context tax this system could pay, and every one of those descriptions would
# be the same sentence.
#
# **They are reserved names, and they are read off every call.**  A tool that
# genuinely wanted an argument called `_timeout` cannot have one — the loop
# pops all three before dispatch, unconditionally, because a name that is
# sometimes a meta-parameter and sometimes a business argument is a name no
# reader could interpret.  The leading underscore is the same mark
# `HARNESS_PREFIX` is, for the same reason: these are the machinery's names,
# and the model only ever *writes* them.
#
# **The strip is a copy and never an edit.**  The call the model made is what
# goes back into the conversation and out to the provider on the next request
# (`Message.to_wire`), so what the *tool* is dispatched with and what the
# *history* keeps are two dicts, and the second one still carries all three.
# Removing them from history would be rewriting what the model said.

#: Seconds; how long this one call may take.  Positive only, and it overrides
#: every default — `0`, negative and missing all mean "use the default", and
#: **never** "no bound" (`bind_timeout`).
TIMEOUT_PARAM = "_timeout"

#: Truthy; run the call in the background and answer a task id instead of a
#: result (`slife2.async_tasks`).
BACKGROUND_PARAM = "_async"

#: Truthy; ask whoever is watching before running the call (`slife2.loop`).
APPROVE_PARAM = "_approve"

#: All three, for the one place that strips them.
META_PARAMS = (TIMEOUT_PARAM, BACKGROUND_PARAM, APPROVE_PARAM)

#: What the three look like **on the schema of every tool the model is handed**.
#:
#: **Schema rather than prompt, and that is the opposite of v1's choice.**  v1
#: declared them once in the system prompt, on the argument that re-describing
#: three parameters on sixty schemas is the largest per-request context tax the
#: system could pay.  v2 pays it, for what the other arrangement costs: the
#: prompt is the *operator's file* (`config.agents[x].system_prompt`), so a block
#: the harness appended there would be one more thing an operator's own prompt has
#: to be kept in step with — and a tool's contract belongs next to the tool, where
#: a model reading *that* tool's arguments finds it.  The three are optional
#: everywhere, so nothing a tool declares has to change, and the loop strips all
#: three before dispatch (`split_meta`).
META_PROPERTIES: dict[str, Any] = {
    TIMEOUT_PARAM: {
        "type": "number",
        "description": (
            "Seconds this one call may take. Overrides the tool's own default; "
            "0 or less means 'use the default', never 'no limit'."
        ),
    },
    BACKGROUND_PARAM: {
        "type": "boolean",
        "description": (
            "Run this call in the background and answer a task_id instead of a "
            "result. Its result is handed back to the conversation when it is "
            "done, wearing [AsyncToolCallResult:{...}] with the call id, tool, "
            "task_id and status; check_async reports status, cancel_async "
            "abandons it. Unbounded unless _timeout is given too."
        ),
    },
    APPROVE_PARAM: {
        "type": "boolean",
        "description": (
            "Ask the user to confirm before this call runs. Where nobody is "
            "watching, the call runs without asking."
        ),
    },
}


def advertised(spec: ToolSpec) -> ToolSpec:
    """A tool's schema as the model is given it: its own, plus the three.

    **Kept apart from `Tool.spec` rather than merged into it**, and the split is
    load-bearing: `declares_timeout` reads the tool's *own* declaration to decide
    who enforces a deadline, so a `_timeout` hiding in that view would make every
    tool look like one that declares its own bound.  One view is what the tool
    says about itself; this one is what every tool is also offered.
    """
    parameters = dict(spec.parameters)
    properties = dict(parameters.get("properties") or {})
    # The tool's own descriptions win a name collision, and there is no such
    # collision to have: the three are reserved (`META_PARAMS`), so a tool that
    # declared one would have it popped before dispatch and never see it.
    properties.update(
        {
            name: field
            for name, field in META_PROPERTIES.items()
            if name not in properties
        }
    )
    parameters["properties"] = properties
    parameters.setdefault("type", "object")
    return ToolSpec(name=spec.name, description=spec.description, parameters=parameters)


@dataclass(frozen=True)
class Meta:
    """What one call's meta-parameters asked for.

    Parsed once per call and carried as this rather than as three loose values,
    because the loop reads all three in one place and passes them on together.
    """

    #: The positive number the model asked for, or None — which means "nobody
    #: said", not "no bound" and not zero.  `bind_timeout` is what turns None
    #: into a decision, and the decision is the tool's own if it declares one.
    timeout: float | None
    #: Run it in the background.  Truthiness, not a strict boolean: v1's rule,
    #: and the safe direction — `"false"` is a string, and backgrounding a call
    #: the model meant to wait for is recoverable where the reverse is not.
    background: bool
    #: Ask before running.  Truthiness for the same reason, and it fails the
    #: same safe way: anything the model wrote that is not empty asks.
    approve: bool


def split_meta(arguments: Mapping[str, Any]) -> tuple[dict[str, Any], Meta]:
    """One call's arguments, and what its meta-parameters said.

    The returned dict is a copy without the three reserved names; `arguments`
    itself is untouched, which is what keeps the model's own history intact
    (see the section comment above).

    A malformed `_timeout` — the model writing `"5 seconds"`, which they do —
    degrades to "nobody said" rather than raising.  It is the model's own call
    that would die: an exception here escapes a whole batch of concurrent calls
    and ends the turn, which is a disproportionate answer to a bad number.
    """
    rest = {name: value for name, value in arguments.items() if name not in META_PARAMS}
    return rest, Meta(
        timeout=parse_timeout(arguments.get(TIMEOUT_PARAM)),
        background=bool(arguments.get(BACKGROUND_PARAM)),
        approve=bool(arguments.get(APPROVE_PARAM)),
    )


def parse_timeout(value: Any) -> float | None:
    """A `_timeout` as a positive number of seconds, or None.

    None means "nobody said" and covers all four ways of saying nothing: the
    argument absent, `None`, zero-or-negative, and text that is not a number.
    v1's rule, and the reason it is a rule rather than a convenience: the tool
    reading `timeout=0` into `wait_for(..., timeout=0)` fires *instantly*, so a
    pass-through zero would be the exact opposite of the "0 = no bound" the
    prompt promises.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        logger.debug("tool timeout not a number, ignored: %r", value)
        return None
    return seconds if seconds > 0 else None


def declares_timeout(spec: ToolSpec | None) -> bool:
    """Whether a tool takes its own `timeout` argument.

    A property-name check on the schema, which is v1's test.  What it decides is
    who enforces the deadline: a tool that declares one enforces it itself (it
    is the only party that knows what it is waiting for), and the loop must not
    wrap it — a second timer is a second answer to one question, and the shorter
    of the two would be the one that fired.
    """
    if spec is None:
        return False
    properties = spec.parameters.get("properties")
    return isinstance(properties, dict) and "timeout" in properties


def bind_timeout(
    arguments: dict[str, Any], spec: ToolSpec | None, asked: float | None
) -> float | None:
    """Put a per-call timeout where the tool that has to honour it will read it.

    Two shapes, and which one applies is `declares_timeout`:

    * **The tool declares `timeout`** — the number is written into its own
      argument and the tool enforces it (the media tools work this way: a render
      says how long it is willing to wait, and the plugin owns the polling).  A
      non-positive request is *removed* rather than forwarded, so the tool's own
      default governs; absence is how a tool hears "use your default".  The
      answer is None: the loop has nothing left to enforce.
    * **The tool does not** — a bare `timeout` the model appended anyway is read
      as the per-call bound and taken out of the arguments, because a tool
      server validates its parameters and would reject the whole call with
      "Unexpected keyword argument" — v1 measured that, and a turn that dies
      over one extra key is the worst possible price for a good intention.  The
      answer is the number the loop should enforce, or None when nobody asked.

    Either way `arguments` is what the tool will be dispatched with.
    """
    if declares_timeout(spec):
        if asked is not None:
            # int(), so a sub-second request truncates — and a request that
            # truncates to 0 is dropped rather than forwarded, because the
            # tools that read this argument document "0 = your default" and one
            # of them would fire instantly on it (v1's B2/B3).
            whole = int(asked)
            if whole > 0:
                arguments["timeout"] = whole
            else:
                arguments.pop("timeout", None)
        elif arguments.get("timeout") is not None:
            # A schema-valid `timeout` that is zero or negative gets the same
            # treatment as `_timeout: 0`: removed, so the tool's default governs
            # instead of a zero that means "instantly".  Text that is not a
            # number is left alone — it is schema-valid, the tool is the party
            # that will read it, and its own complaint is a better answer than
            # this module's guess (v1's rule, kept).
            try:
                bare = float(arguments["timeout"])
            except (TypeError, ValueError):
                bare = 1.0
            if bare <= 0:
                arguments.pop("timeout", None)
        return None

    if asked is None:
        asked = parse_timeout(arguments.pop("timeout", None))
    return asked


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
        #: Built once, because `specs` is read at the top of every step of every
        #: turn: augmenting sixty schemas per request would be paying the
        #: per-request tax this design already pays twice (see `META_PROPERTIES`).
        self._advertised: list[ToolSpec] = [
            advertised(t.spec) for t in self._tools.values()
        ]

    @property
    def specs(self) -> list[ToolSpec]:
        """What to advertise to the model — every tool, with the three.

        See :func:`advertised`: what a tool *declares* is `Tool.spec`, and this
        is that plus the meta-parameters every tool accepts.
        """
        return list(self._advertised)

    def spec_of(self, name: str) -> ToolSpec | None:
        """One tool's schema, or None for a name this registry does not have.

        For the caller that has to read a tool's declaration *before* it runs —
        `slife2.loop` asks whether the tool takes its own `timeout` — and who
        must not guess it from the arguments: a tool nobody declared is not a
        tool that declares nothing.
        """
        tool = self._tools.get(name)
        return tool.spec if tool is not None else None

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
