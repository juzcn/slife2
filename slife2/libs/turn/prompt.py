"""Rendering the text this system puts in front of a model.

Three of them, and two entry points: the **system prompt**, which the config
names and which is rendered when a conversation starts; the **recall
instruction**, which `slife2.libs.turn.context` renders per turn; and the
**subagent's prompt** (`subagent.j2`), which is shipped rather than configured
because what a subagent has to be told — what it is, whose it is, and where its
work comes from — is not something an operator's prompt for their own agent
says.  The last two ship beside the default prompt in
`slife2/libs/turn/templates/`.  They are all prompts with holes in them, so
they are all templates here rather than one template and one f-string.

**The two entry points are the difference between the prompt and an
instruction.**  `render_system_prompt` puts the home's **`AGENTS.md`** — the
guide to the model's tools, a document a person writes beside the config — into
the context as `agents_md`, so the shipped `system.j2` opens with it and a
prompt of one's own can put it where it likes.  `render_instruction` is for
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
`AGENTS.md` is read at that same moment, which is what makes an edit to it reach
the next conversation rather than the next restart.

Jinja2 caches compiled templates, so rendering per turn costs a dict lookup and
a format operation, not a parse.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateError, select_autoescape

from slife2.libs.host.config import ConfigError
from slife2.libs.host.paths import AGENTS_DOC, agents_doc

#: Where the templates this distribution ships live.
TEMPLATE_DIR = Path(__file__).parent / "templates"

#: The template used when the config names none.
DEFAULT_TEMPLATE = "system.j2"


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

    The callers are the recall instruction (`slife2.libs.turn.context`), the
    compact instruction (`slife2.libs.harness.dumps`) and a scheduled run's
    trigger.  A bare name is looked up among the shipped templates; a path is
    looked up beside itself.

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
    startup because it is a document a person edits — an edit reaches the next
    conversation, as an edit to the config reaches the next turn.

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
            "seeds one; edit that, or write your own there."
        ) from None
    except OSError as exc:
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
