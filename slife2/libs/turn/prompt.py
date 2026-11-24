"""Rendering the text this system puts in front of a model.

Four templates, and two entry points: the **system prompt**, which the config
names and which is rendered when a conversation starts; the **recall
instruction**, which `slife2.libs.turn.context` renders per turn; the
**agent-task opening** (`slife2.plugins.agent_task.server`); and the
**subagent's prompt** (`subagent.j2`), which is shipped rather than configured
because what a subagent has to be told — what it is, whose it is, and where its
work comes from — is not something an operator's prompt for their own agent
says.  All four ship in `slife2/libs/turn/templates/`.  They are all prompts
with holes in them, so they are all templates here rather than one template and
one f-string.

**The two entry points are the difference between the prompt and an
instruction.**  `render_system_prompt` puts the home's **`AGENTS.md`** — the
system-delivered guide to the model's tools, beside the config — into the
context as `agents_md`, so the shipped `system.j2` opens with it and a prompt of
one's own can put it where it likes.  `render_instruction` is for
the instruction templates, which are one turn's text and the same in every
home; it reads nothing from the home, and a template it renders may not ask
for the guide.

The prompt is a Jinja2 template rather than a config string, so the parts of it
that depend on *who is asking* can be written as such: a prompt is text with
holes in it, and a template is the thing that says where the holes are.

**It is rendered when a conversation starts, not once at startup.**  The agent
name is a property of the conversation — `send_message(agent, ...)` names it —
because the server is shared, so two instances on one server are two names asking
the same process.  Rendering once would make the first caller's name everybody's.
Rendering it per *turn* would be the same mistake one size smaller: a
conversation's agent cannot change, so there would be nothing to re-render for.
`AGENTS.md` is read at that same moment, which is what makes a replacement reach
the next conversation rather than the next restart.

Jinja2 caches compiled templates, so rendering per turn costs a dict lookup and
a format operation, not a parse.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateError, select_autoescape

from slife2.libs.host.config import Config, ConfigError
from slife2.libs.host.paths import AGENTS_DOC, agents_doc
from slife2.libs.host.runtime import read_claim
from slife2.libs.host.subagents import SubagentSpec
from slife2.libs.turn.messages import Message

#: Where the templates this distribution ships live.
TEMPLATE_DIR = Path(__file__).parent / "templates"

#: The template used when the config names none.
DEFAULT_TEMPLATE = "system.j2"

#: The system prompt a subagent's conversation opens with.  **Not the template
#: an agent's own conversation uses** (`agent.system_prompt`): the two have
#: different things to say — this one says what a subagent is, whose it is, and
#: where its work comes from — and an operator's prompt for their own agent
#: cannot say any of that.  A bare name, so it is looked up among the templates
#: shipped beside `system.j2`.
#:
#: **One template for every role, for now.**  `role` reaches it as a word ("You
#: are scout, one of jill's subagents — your role is code reviewer") and not yet
#: as a choice of *file*: a per-role template is the step this design leaves open,
#: and it is left open on purpose rather than guessed at — a role that selects
#: `prompts/<role>.j2` needs an answer for a role with no file, and that answer
#: is a design decision.
SUBAGENT_PROMPT = "subagent.j2"


class _NoGuide:
    """What `render_instruction` puts where the guide would be: a mistake if printed.

    An instruction template has no home document in its context, and one that
    asks for `agents_md` anyway is a system prompt rendered through the wrong
    entry point.  An empty string would be a prompt silently missing the section
    it asked for — the hidden degrade this codebase refuses — so the value
    raises when Jinja prints it, which is the same `ConfigError` a missing
    `AGENTS.md` gives.
    """

    def __str__(self) -> str:
        raise ConfigError(
            "agents_md is in a system prompt's context only; render this template "
            "with render_system_prompt"
        )


def _environment(search: Path) -> Environment:
    """A loader for one directory.

    Autoescaping is off.  These templates render into a model's context, not
    into HTML, and escaping would put `&amp;` in front of a model that has no
    idea what that means.  The `select_autoescape` call is kept for the
    extension check it would do if a template ever *were* HTML — it is a no-op
    here and says so out loud rather than leaving the default to be guessed.
    """
    return Environment(
        loader=FileSystemLoader(str(search)),
        autoescape=select_autoescape(enabled_extensions=()),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


#: One environment per directory, built on demand and kept: constructing an
#: `Environment` is the expensive part, and rendering happens every turn.
_ENVIRONMENTS: dict[Path, Environment] = {}


def _renderer(directory: Path) -> Environment:
    environment = _ENVIRONMENTS.get(directory)
    if environment is None:
        environment = _environment(directory)
        _ENVIRONMENTS[directory] = environment
    return environment


def render_instruction(template: str | Path, **context: object) -> str:
    """Render an instruction template: one turn's text, from the package.

    The callers are the recall instruction (`slife2.libs.turn.context`) and
    the agent-task opening (`slife2.plugins.agent_task.server`).  A bare name is
    looked up among the shipped templates; a path is looked up beside itself.

    **Nothing from the home is in its context**, and `agents_md` is a value
    that raises if asked for (`_NoGuide`): these templates say what to do with
    one turn, which is the same in every home, and the guide belongs to the
    system prompt rather than to a second kind of prompt.

    Raises:
        ConfigError: If the template is missing or does not render.
    """
    return _render(template, {"agents_md": _NoGuide(), **context})


def render_system_prompt(template: str | Path | None, **context: object) -> str:
    """Render the system prompt for one conversation.

    `template` is what the config said: a path, or empty for the shipped
    default.  A relative path is resolved against the config file's own
    directory by :func:`slife2.libs.host.config.load`, so by the time it arrives
    here it is either absolute or a bare name for the packaged templates.

    **The home's `AGENTS.md` is in the context as `agents_md`**, and it is
    required: a home has one beside `slife2.yaml`, exactly as it has the config,
    and a missing one is a `ConfigError` naming the file rather than a prompt
    that silently opens without the guide.  It is read here rather than once at
    startup because it is the installer's to replace — a reinstalled guide
    reaches the next conversation, as an edit to the config reaches the next
    turn.

    Raises:
        ConfigError: If the template or `AGENTS.md` is missing, unreadable, or
            does not render.
    """
    return _render(template, {"agents_md": _agents_md(), **context})


def _agents_md() -> str:
    """The home's guide to the model's tools, or the `ConfigError` saying why not.

    **A missing file and an unreadable one are different answers**, the rule the
    config loader states (`slife2.libs.host.config._config_file`, SDD Appendix
    C): only "no such file" is missing; a directory where the document should be,
    or a read the filesystem refused, is named as that rather than read as a home
    without a guide.
    """
    path = agents_doc()
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(
            f"no {AGENTS_DOC} in {path.parent}: a home has one beside "
            "slife2.yaml — the model's guide to its tools, which the system "
            "prompt opens with — and slife2 has none built in. The installer "
            "writes it; run the installer again to restore it."
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        # `UnicodeDecodeError` is a `ValueError`; without it here a guide saved
        # in another encoding escaped the named refusal and failed the turn with
        # the codec's message about a byte.
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def _render(template: str | Path | None, context: dict[str, object]) -> str:
    """Render one template with the context its caller supplied."""
    if not template:
        return _renderer(TEMPLATE_DIR).get_template(DEFAULT_TEMPLATE).render(**context)

    path = Path(template)
    if path.parent == Path(""):
        directory, name = TEMPLATE_DIR, path.name
    else:
        directory, name = path.parent, path.name

    try:
        return _renderer(directory).get_template(name).render(**context)
    except TemplateError as exc:
        raise ConfigError(f"cannot render {path}: {exc}") from exc


def opening_messages(
    agent: str, subagent: str, live: Config, spec: SubagentSpec
) -> list[Message]:
    """What a conversation starts with: its system prompt, and nothing else.

    Rendered when the conversation starts rather than once at startup,
    because the agent name is a property of the *key* — the server is shared,
    so two agents are two names asking one process — and it is rendered from
    the config a conversation *starts* from, so a prompt edited since boot
    reaches the next one.

    **A subagent is told its own name, its agent's, and where its work comes
    from.**  Each follows from what it is: the name in the prompt is the one
    it answers to (its agent's alone would be a model told it is somebody
    else), the agent's name is whose subagent it is — and with it the member
    it answers — and a subagent has no window at all, not a terminal and not
    a headless one, so what reaches it comes from another member rather than
    from a person at its own keyboard.

    **It is told which tools carry a reply, not their schema.**  What the
    call takes — `task_response`, `message_type`, the task's id — is what
    the tool's own description says; a prompt that repeated the arguments
    would be a second schema kept in step by hand.  The prompt's job is the
    part no tool can state: what this conversation is.

    **And it is told what it is, from its own file** (`subagent_spec`): the
    `role` the agent named it with is the one thing from that file the prompt
    uses.  Its prompt is `subagent.j2` and not the config's
    (`SUBAGENT_PROMPT`), because the two say different things.

    **And an agent's own prompt is rendered with whether anybody is
    watching**, read off the claim the client under that name wrote
    (`AgentClaim.headless`).  A prompt is where a model is told who it is
    talking to, and an unattended agent has nobody: a tool call put up for
    approval is granted without being read, so a model that asks is spending
    the turn it was given.  **The unattended sentence names the tools a
    reply leaves by** — the peer network's and the channel's own send tool —
    because nobody is there to read prose.  Two ways for nobody to be there,
    and they are one answer: the instance holding the name has no terminal
    (`slife2 --headless`), or **no instance is holding it at all** — a peer
    that asks something of an agent nobody is running is told so rather than
    served (`slife2.libs.harness.handover`), and a turn that runs here
    anyway is unattended for the same reason.  A subagent is not one of
    those cases: it is always unattended, so its prompt has no claim to read.
    """
    if subagent:
        system = render_system_prompt(
            SUBAGENT_PROMPT,
            agent_name=subagent,
            agent=agent,
            role=spec.role,
        )
    else:
        claim = read_claim(agent)
        system = render_system_prompt(
            live.agent.system_prompt,
            agent_name=agent,
            headless=claim is None or claim.headless,
        )
    return [Message(role="system", content=system)] if system else []
