"""Rendering the system prompt.

The prompt is a Jinja2 template rather than a config string, so the parts of it
that depend on *who is asking* can be written as such.  slife v1 does the same,
and for the same reason: a prompt is text with holes in it, and a template is
the thing that says where the holes are.

**It is rendered per turn, not once at startup.**  The agent name arrives with
the request — `run_turn(messages, prompt, agent=...)` — because the server is
stateless and shared, so two instances on one server are two names asking the
same process.  Rendering once would make the first caller's name everybody's.

Jinja2 caches compiled templates, so rendering per turn costs a dict lookup and
a format operation, not a parse.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateError, select_autoescape

from slife2.config import ConfigError

#: Where the templates this distribution ships live.
TEMPLATE_DIR = Path(__file__).parent / "templates"

#: The template used when the config names none.
DEFAULT_TEMPLATE = "system.j2"


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


def render(template: str | Path | None, **context: object) -> str:
    """Render the system prompt for one turn.

    `template` is what the config said: a path, or empty for the shipped
    default.  A relative path is resolved against the config file's own
    directory by :func:`slife2.config.load`, so by the time it arrives here it
    is either absolute or a bare name for the packaged templates.

    Raises:
        ConfigError: If the template is missing or does not render.  Both are
            config mistakes, and both are worth a message naming the file
            rather than a traceback from inside Jinja.
    """
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
