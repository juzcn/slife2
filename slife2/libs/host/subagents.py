"""What one subagent's own file says: what to run it on, and what it is for.

**A subagent's configuration is a file inside the subagent** — beside its
conversation and its work, the way a WeChat login is — and it is *found* by
looking rather than listed anywhere (`slife2.libs.host.paths.subagent_dirs`).
That is what makes the config as short-lived as the thing it configures: a
subagent made today and removed tomorrow takes its settings with it, and nothing
in `slife2.yaml` has to be kept in step with a directory that is no longer there.

**Why this is a library rather than the agent server's.**  Two servers open a
conversation: the agent server runs its turns, and an agent task runs one turn
standing in for the same conversation.  Both have to render the same prompt and
resolve the same model, so the record both read is here rather than in one of
them — the server that ran a task would otherwise have to import the server that
runs conversations, which is the import this package's plugin boundary exists to
prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from slife2.libs.host.paths import subagent_file

logger = logging.getLogger(__name__)

#: What a subagent is when the call that made it named no role, and the word the
#: shipped `subagent.j2` opens with.  A **role** is what a subagent is *for* —
#: the one part of what it is that its maker decides and not the system — and it
#: is kept in the subagent's own file, which is where everything about one
#: subagent lives (`subagent_spec`).
DEFAULT_ROLE = "subagent"


@dataclass(frozen=True)
class SubagentSpec:
    """What a subagent's own file says it is: what to run it on, and what it is.

    So this is read where it is used and never cached, which is the same rule the
    config file itself gets: a subagent that has been named twice runs on what
    the second call said, and a `model_switch` reaches one that has not started
    yet.
    """

    #: `provider/model`, or empty for the maker's own — which is not a fallback
    #: but the default the note asked for: a subagent is an agent the maker owns,
    #: so it runs where the maker runs unless it was told otherwise.
    model: str = ""
    #: What it is for, in the words its prompt uses (`DEFAULT_ROLE`).
    role: str = DEFAULT_ROLE


def subagent_spec(agent: str, subagent: str) -> SubagentSpec:
    """One conversation's own settings, or the defaults for the maker's own.

    The maker's own conversation has no such file and is not supposed to: what
    makes a directory a subagent's is the record inside it, and an agent's own
    conversation is not one (`slife2.libs.host.paths.subagent_file`).  So `""`
    answers the defaults rather than being looked up, which keeps
    `agent_dir(agent, "")` — the agent's own directory — out of a question about
    a file that lives under the subagent's own directory.
    """
    if not subagent:
        return SubagentSpec()
    record = read_subagent(agent, subagent)
    return SubagentSpec(
        model=str(record.get("model") or ""),
        role=str(record.get("role") or DEFAULT_ROLE),
    )


def read_subagent(agent: str, subagent: str) -> dict[str, Any]:
    """What a subagent was made with, or `{}` when there is none to read.

    **A record that cannot be read is read as absent, and that is a decision
    rather than a shrug.**  What makes a directory a subagent's is that this file
    *exists*, so a torn write cannot widen what `subagent_remove` deletes — the
    property that matters is carried by the file's existence, not by its
    contents.  What the contents add is the model and when it was made, and
    losing a `created_at` to a half-written file is a smaller harm than a removal
    that refuses to happen; the log line is what says a file wants looking at.

    So the shape is the one `slife2.plugins.wechat.server` keeps for its login and
    for its reason: a per-agent file whose absence is a normal state, whose
    damage is reported where a person can read it, and which never turns into a
    refusal of the thing the caller actually asked for.
    """
    path = subagent_file(agent, subagent)
    if not path.is_file():
        return {}
    try:
        loaded = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, YAMLError) as exc:
        logger.warning("the record of %s could not be read: %s", path, exc)
        return {}
    if not isinstance(loaded, dict):
        logger.warning("the record of %s is not a mapping", path)
        return {}
    return loaded


__all__ = ["DEFAULT_ROLE", "SubagentSpec", "read_subagent", "subagent_spec"]
