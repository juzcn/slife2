"""Where slife2 keeps things.

**Two roots, because there are two kinds of fact.**  A *home* is a data
directory: one config, one set of playbooks, and the agents whose data lives in
it.  **A directory may hold several agents**, and then they share its config and
its catalogue, which is the whole of what "the same `--data-dir` shares
`tools.db`" means.  What a name is *not* is a property of one: it is an identity
claimed once on the machine (`slife2.libs.host.runtime.agent_claim_path`), so
`--agent jack --data-dir D` names the agent and says where its files go, and the
directory a name used before is not remembered.  The *daemons* are one set per machine —
`slife2.libs.host.launcher` shares them by endpoint, whatever started them — so
everything describing a running server is a fact about the machine, and it gets a
root of its own rather than a copy per home.

    <data>/                     one home
      slife2.yaml   the config; absent means the built-in defaults
      skills/       <name>/SKILL.md — the playbooks the model may read
      agents/       everything that belongs to one agent, one directory each
        tools.db      the tool catalogue — one file for the home, and the
                      only thing here that is not an agent's own
        <agent>/      turns.db    the conversation, and what it runs on
                      schedule.db what this agent has scheduled, and every
                                  fire of it — the ledger `slife2-schedule`
                                  keeps, and the file it finds agents by
                      cabinet/    notes, diary, saved files, reports — as
                                  markdown and bytes a person can open, with
                                  the index over them (`.index.db`) inside
                      media/      what this agent generated
                      wechat.yaml this agent's WeChat login
                      <subagent>/ the same shape again, one level in — what an
                                  agent made with `subagent_set` owns: it
                                  belongs to its parent by being underneath it,
                                  and removing it is removing this directory

    <runtime>/                  the machine's daemons
      servers/      a record per running server: its pid, its url, its config
      logs/         what each server printed — see `tail_log`
      locks/        the per-endpoint start lock two launchers race on
      agents/       the claims: which process is running under an agent name
      clients/      who is using the shared servers, so the last one out can
                    take them down — and `home`, which names the home it is
                    working in
      homes/       every data directory this machine has run an agent in

**Which root a path comes from is the whole of the design.**  `runtime/` holds
what a *machine* has: one set of servers, so one record of them, one lock
namespace, one log each, and one claim per agent name — and a second copy of any
of those disagreeing with the first is a bug rather than redundancy.  It is also
*reconstructible*: delete it and the next start rebuilds what it needs.

`agents/` and `skills/` are a *home's*, and they are not reconstructible.
Anything that would be a disaster to lose lives there.

**And a home is what a call belongs to.**  The servers are shared, so the
process that writes an agent's turn file is not the process that was started
with `--data-dir`: it is whichever one is already up, and it resolves
`agents/<agent>/…` in *the caller's* home rather than its own — see `home`,
which is where a call's home is read, and `in_home`, which is how a server
puts one around the work it does for a call.  The daemon's own config and its
own bookkeeping stay in `data_dir()`: `slife2.yaml` is read from the home the
process was started in, and a record is written where the machine keeps them.

**Where the home is depends on what you are running.**  A checkout keeps it
beside itself, so the checked-in `slife2.yaml` is the one in use and the
databases are directories you can open; an installation keeps it at `~/.slife2`,
per-user and independent of wherever it was started from.  See `data_dir`.  The
machine's runtime is `~/.slife2/runtime` either way, because a checkout and an
installation on one machine share one set of daemons.

**The homes are listed, and that list is how a shared daemon finds them.**  No
server holds a registry of agents: the schedule plugin finds a home's ledgers by
walking its `agents/`, the WeChat plugin its logins, the mesh its inbound
ledgers — and each of those walks is per home, so a daemon serving two homes
has to be able to name them.  `register_home` is a client saying which home it
is in, `known_homes` is a daemon reading the list, and a home a process was
itself started in counts whether or not anybody registered it.

**`agents/` is one directory per agent, and the whole of one agent.**  A
conversation, a cabinet, a login and whatever it generated live under the name
they belong to, so backing one up, copying one to another machine or deleting
one is one operation — the property a cabinet already claimed for itself, one
level up.  **Nothing inside is named after the agent**: the directory says whose
it is, and a file repeating it would be a name to keep in step with a rename.

The catalogue is the exception, and it is why it sits beside the agent
directories rather than in one: `tools.db` describes the tool *set*, which is
everybody's and therefore nobody's.

`skills/` is the third kind of thing, and it is beside the config rather than
under `runtime/` for the reason the config is: it is **written by a person**,
not generated by a run.  In a checkout that means it is checked in and the
skills in front of you are the ones in use, exactly as `slife2.yaml` is.

**The cabinet is the fourth, and it is one directory per agent** rather than a
file: its markdown and a saved file's bytes are what a person opens, and its
index (`.index.db`, hidden) sits inside the directory it indexes so that a
cabinet can be copied, backed up or deleted as one thing.  That is why its path
is not derivable from the data directory alone — `slife2.plugins.cabinet.store` owns
those two names, and v1 used the same `<agent>.files/` layout, so a v1 cabinet's
documents are adopted by copying the directory in.

`SLIFE2_DATA_DIR` overrides where the home is, and is what `--data-dir` sets;
`SLIFE2_RUNTIME_DIR` overrides where the machine's runtime is.  Both exist for
the same reason — a test that wrote into a developer's real state would be a test
that passes on a clean machine and fails on a busy one — and neither is a knob an
operator is expected to turn.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Generator
from contextvars import ContextVar
from pathlib import Path

#: The environment variable naming the data directory.
DATA_ENV_VAR = "SLIFE2_DATA_DIR"

#: The environment variable naming the machine's runtime directory.  Set by
#: nothing a person runs — `--data-dir` has no counterpart flag, because the
#: runtime is not a choice an operator makes but a fact about the machine — and
#: set by the test suite, which may not write into the machine it runs on.
RUNTIME_ENV_VAR = "SLIFE2_RUNTIME_DIR"


def add_data_dir_argument(parser: argparse.ArgumentParser) -> None:
    """Add `--data-dir`, the one flag every entry point accepts.

    Here rather than written into each parser because there are two of them —
    the CLI's and the servers' — and a flag whose help text and meaning are
    copied into both is a flag that drifts.  It is also the flag that decides
    which config, which runtime state and which databases everything else reads,
    so it belongs beside :func:`data_dir` rather than in either caller.
    """
    parser.add_argument(
        "--data-dir",
        default=None,
        help=(
            "where slife2 keeps everything: slife2.yaml, the runtime state of "
            "what is running, and the turns it produced"
        ),
    )


def apply_data_dir(args: argparse.Namespace) -> None:
    """Honour `--data-dir` by putting it in the environment.

    Set in the environment rather than passed down: the servers a process starts
    must look in the same folder, and an inherited variable is harder to forget
    than an argument.  Both entry points therefore call this immediately after
    parsing, before anything reaches :func:`data_dir`.
    """
    if args.data_dir:
        os.environ[DATA_ENV_VAR] = args.data_dir


#: What a checkout of this project has at its root.  Its presence, naming this
#: project, is what "a development environment" means here.
_CHECKOUT_MARKER = "pyproject.toml"
_PROJECT_NAME = 'name = "slife2"'


def in_checkout(directory: Path | None = None) -> bool:
    """Whether this is a source checkout of slife2 rather than an installation.

    Deliberately a *specific* signal — a `pyproject.toml` that names this
    project — rather than "there is a `.git` here" or "there is a `slife2.yaml`
    here".  Both of those are true of other projects' directories and of a
    production folder somebody happened to put a config in, and being wrong
    about this decides where a database is written.

    **A marker that cannot be read is not a marker that is not there.**  Only
    "no such file" answers False; a permissions error, or a directory sitting
    where the marker should be, raises, because reading either as "not a
    checkout" would send a checkout's whole data directory to `~/.slife2` with
    nothing said (DESIGN.md §10.1).
    """
    marker = (directory or Path.cwd()) / _CHECKOUT_MARKER
    try:
        text = marker.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, NotADirectoryError):
        return False
    return _PROJECT_NAME in text


def data_dir() -> Path:
    """The one directory slife2 keeps its files in.

    **A checkout keeps everything beside itself; an installation keeps it under
    the home directory.**  Working on slife2 means the config, the runtime state
    and the databases are the ones in front of you — the checked-in
    `slife2.yaml` is read, `slife2.libs.store.db/` is a directory you can open, and deleting
    the checkout deletes the lot.  An installation has no checkout to sit in, so
    it uses `~/.slife2`, which is per-user and survives whatever directory it
    happens to be started from.

    `SLIFE2_DATA_DIR` overrides both, and `--data-dir` sets that.

    **The `~/.slife2` root is a registered fallback**: it is what an installation
    with no checkout gets, so a checkout that is not *recognised* writes its
    databases to the home directory instead of beside itself.  *Gives up:* the
    checkout's own directory; *does not cover:* an override, which wins; *failure
    goes:* nowhere visible until a file is missing where it was expected — flag
    for `DESIGN.md` §10.2.
    """
    override = os.environ.get(DATA_ENV_VAR)
    path = (
        Path(override)
        if override
        else (Path.cwd() if in_checkout() else Path.home() / ".slife2")
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_dir() -> Path:
    """Where this machine's daemons record that they are running.  Safe to delete.

    **Not under the data directory, and that is the point.**  The servers are one
    set per machine (`slife2.libs.host.launcher._reuse` shares them by endpoint), so a
    record naming a pid, the lock two launchers race on, the log a failed start
    is read from and the claim holding an agent name are all facts about *the
    machine* — and a copy of each per home is a copy that can disagree with the
    others.  What that looked like in practice: a second data directory reporting
    a running stack it could not see ("no record; not started by slife2"), two
    launchers cold-starting one port because the lock was in neither's way, and a
    client in one home stopping the servers a client in another was still using.

    `~/.slife2/runtime` rather than a per-OS state directory, because
    `~/.slife2` is already this project's home-directory root (`data_dir`) — and
    for an installation the two coincide, so nothing moves.  A checkout moves it
    out of the working tree, which is the correction: a daemon's bookkeeping is
    not a checkout's file to commit, delete, or carry to another machine.
    """
    override = os.environ.get(RUNTIME_ENV_VAR)
    path = Path(override) if override else Path.home() / ".slife2" / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


#: The data directory the files being resolved belong to, when a call named one.
#:
#: A context variable rather than an argument, because it has to reach code that
#: has no idea a call is in progress — every `agent_dir(agent)` in every plugin —
#: and because a *server* is the one place it is ever set.  `None` means "this
#: process's own", which is what a client, a launcher and a test all are.
_HOME: ContextVar[Path | None] = ContextVar("slife2_home", default=None)


@contextlib.contextmanager
def in_home(root: str | Path | None) -> Generator[None]:
    """Resolve this home's files as `root`'s for the length of the block.

    The one way the ambient home is ever set, and it is set **around a whole
    call** rather than consulted at each path: a server is shared, so the home
    is a property of the request in front of it, and everything that request
    reaches — the turn file it opens, the cabinet it writes to, the catalogue it
    reads — belongs to the caller rather than to the process.

    `None` and `""` mean "no home was named", which is not an error: it is the
    daemon's own home, which is what everything did before any of this existed.
    """
    token = _HOME.set(Path(root) if root else None)
    try:
        yield
    finally:
        _HOME.reset(token)


def home() -> Path:
    """The data directory to resolve a *home's* files in.

    **The caller's if a call named one, this process's otherwise.**  Everything
    below that resolves `slife2.yaml`, `agents/`, `skills/` or the catalogue goes
    through here; everything that resolves a record, a lock, a log or a claim
    does not, because that is the machine's (`runtime_dir`).

    This process's own is the fallback rather than an error, and it is the
    common case in production: a client, a launcher and a test are all one home
    from start to finish, and only a *server* ever serves somebody else's.
    """
    override = _HOME.get()
    return override if override is not None else data_dir()


def agents_dir() -> Path:
    """Where every agent's own files live, in the home being served.

    Not safe to delete, and made on demand here rather than when the home is
    entered: a call that touches no agent's file — a `status`, a list of tools —
    should not create the directory it does not use.
    """
    path = home() / "agents"
    path.mkdir(parents=True, exist_ok=True)
    return path


def agent_dir(agent: str, subagent: str = "") -> Path:
    """One agent's directory: its conversation, its cabinet, its logins, its work.

    **A subagent's lives inside its parent's** — `agents/jack/researcher/` — and
    the nesting *is* the relationship: an agent's everything is one directory, so
    a subagent belongs to whoever made it by being underneath it, removing a
    subagent is removing a directory, and removing an agent takes its subagents
    with it.  That is `agents_dir`'s argument one level down, and the reason
    nothing has to hold a second record of who made whom.

    It takes **both halves of the client id** because it is the one place that
    knows the layout: every per-agent path in this system is this function plus a
    file name, so the nesting is written once here rather than restated wherever
    a file happens to live.

    **Both names are sanitised here**, so that no caller has to remember to: they
    come off a command line and out of a model's tool call, and `..` is a
    directory too.  `safe_agent_name` is imported inside this function rather
    than at the top because it lives in `slife2.libs.store.db` and `db` imports this
    module — a local import is what keeps one implementation of "a name fit to
    be a file", instead of a second copy in the leaf that cannot reach the first.
    It is also what keeps a subagent *inside* its parent: the sanitiser refuses a
    name that reduces to `.` or `..`, so the second segment cannot climb out of
    the first.
    """
    from slife2.libs.store.db import safe_agent_name

    path = agents_dir() / safe_agent_name(agent)
    if subagent:
        path = path / safe_agent_name(subagent)
    path.mkdir(parents=True, exist_ok=True)
    return path


def catalogue_db() -> Path:
    """The tool catalogue: one file, beside the agents rather than in one.

    **Not safe to delete**, and not an agent's: the catalogue is the tool *set*,
    which every conversation is served from, and a per-agent copy would be a copy
    to keep in step.  It follows the home rather than the process for the same
    reason the agents do: a hub serving two homes keeps the tool set each of
    them is configured with, and sharing one file between them would be one
    home's bookkeeping written into another's.
    """
    return agents_dir() / "tools.db"


def _digest(text: str) -> str:
    """A short name for a long one, for the file that records a home.

    A path is not a file name — it has separators and a drive letter — and the
    same trick is at work in `slife2.libs.host.runtime`, which digests an agent name
    and an endpoint for exactly this reason.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def register_home(root: Path | None = None) -> Path:
    """Record that this machine has a home at `root`, and answer its path.

    **Written by a client, read by a daemon, and never removed.**  A shared
    server serves every home on the machine, and the only way it can find the
    ones it is not currently talking to is to be told: the schedule plugin walks
    the ledgers of each home, the WeChat plugin its logins and the mesh its
    inbound ledgers, and a schedule that fires while nobody is watching is the
    whole point of having one.  So an entry outlives the client that wrote it —
    a home whose directory is gone is skipped by `known_homes` — and the file's
    name is the digest of the path rather than the path, which is a directory
    tree and not a name.

    **A write that fails is raised, not logged.**  This is the one thing a client
    must do before it claims anything: a home nobody recorded is a home whose
    schedules never fire, whose logins are never resumed and whose peers are never
    answered — and every one of those looks like a feature that does not work
    rather than like a directory that could not be written.  So the failure is the
    client's, named where the fix is: the running server is the thing that loses
    the file, and it must not lose it quietly.

    **And it is written whole or not at all** (`_write_atomically`), because the
    client writing it is running while the daemons reading it are — every start
    rewrites this file, and a reader that caught the truncation would fail a
    start-up pass of its own.

    Raises:
        OSError: If the record cannot be written — an unwritable runtime
            directory, a full disk.  Nothing is degraded in its place: the
            failure is the answer.
    """
    target = (root or data_dir()).resolve()
    entry = homes_dir() / f"{_digest(str(target))}.json"
    _write_atomically(entry, json.dumps({"home": str(target)}))
    return target


def _write_atomically(path: Path, text: str) -> None:
    """Put *text* at *path* so a reader sees all of it or none of it.

    **This is written by a client on every start and read by daemons that are
    already up**, which is why it is not `write_text`.  A truncating write
    leaves the file empty for as long as it takes to write it back — the file is
    opened and emptied first, and the JSON arrives afterwards — and `known_homes`
    lets a `ValueError` out by design: a record it cannot read is a file to look
    at, not a home that is not there (`DESIGN.md` §10.1).  So a daemon walking
    the registry in that window — the context store's lifespan pass
    (`turn_dbs`), the scheduler's poll (`schedule_dbs`), the mesh's announce,
    WeChat's resume — read an empty file and its `json.loads("")` failed its
    *start-up* pass, which is a server the launcher reports as FAILED and an
    exit 2 on a first start, over a file that was never damaged, only in flight.

    Written beside the target and renamed onto it, which is what every other
    bookkeeping file in this codebase does (`slife2.libs.host.runtime._write_json_atomic`)
    — the one home this could not be folded into, because that one swallows the
    failure and `register_home`'s contract is that a write which fails is raised:
    a home nobody recorded is a home whose schedules never fire, and that has to
    be the client's problem and not a line in a log.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def known_homes() -> list[Path]:
    """Every home a shared daemon should serve on this machine.

    **This process's own, and every home a client has recorded.**  The first is
    what makes a home work with nothing registered — a server started by hand in
    a directory nothing else knows about serves that directory, exactly as it did
    before any of this — and the second is what lets one daemon serve the rest.

    A registered home whose directory has been deleted is *skipped*, and that is
    not a fallback: a home that is not there has nothing to serve, and the entry
    stays because deleting a directory is not an event this can observe.

    A record that cannot be **read** is the opposite case and raises.  Reading a
    damaged file as "no home here" would drop that home's schedules, logins and
    ledgers with nothing said — the deformation this codebase refuses everywhere
    else (DESIGN.md §10.1: a failure is not a verdict).  One unreadable file in a
    machine's registry is a file to look at, named in the error.

    Raises:
        OSError, ValueError: A record that cannot be read or is not the shape
            this wrote.
    """
    found = {home().resolve()}
    for entry in sorted(homes_dir().glob("*.json")):
        recorded = str(json.loads(entry.read_text(encoding="utf-8"))["home"])
        path = Path(recorded)
        if path.is_dir():
            found.add(path)
    return sorted(found)


def homes_dir() -> Path:
    """Where the homes this machine has seen are recorded.  Safe to delete."""
    path = runtime_dir() / "homes"
    path.mkdir(parents=True, exist_ok=True)
    return path


#: The conversation store's own file name, spelt once.  Public where the other
#: two per-agent names below are private, because `slife2.libs.store.db.database_path`
#: *builds* this one while `slife2-context` *finds* them — the name is used on
#: both sides of that split, so a second spelling is a second thing to change.
TURNS_DB = "turns.db"


def turn_dbs() -> list[Path]:
    """Every conversation store on this machine: an agent's own, and its subagents'.

    **The pattern, named — and the name is the whole point.**  This replaces a
    `glob("*/*.db")` in `slife2-context`'s startup pass, which is not a
    description of the turn files at all: `schedule.db` sits in the same
    directory and matches it, so the pass opened the *scheduler's* ledger as a
    `TurnStore` and wrote the turn schema into it — two plugins sharing one
    SQLite file, which is the one thing `DESIGN.md` §5 says the file layout is
    for.  A suffix `*.db` means "a database"; what is wanted here is "a
    conversation", and only the middle of the name says which.

    **Two levels, and they are the ones `agent_dir` lays down**: the agent's
    conversation, then a subagent's one directory in (`agents/jack/turns.db`,
    `agents/jack/researcher/turns.db`).  Enumerated rather than globbed
    recursively, because a recursion is a walk of *everything* under an agent —
    a cabinet's index, whatever a tool saved — and what is being looked for here
    is the one file whose name is `turns.db` at either depth.

    **Every home, not this process's** (`known_homes`), because the store
    reading these is shared: a context daemon serving two homes has to have
    opened the conversations of both.
    """
    found: list[Path] = []
    for root in known_homes():
        agents = root / "agents"
        if not agents.is_dir():
            continue
        for directory in sorted(agents.iterdir()):
            if not directory.is_dir():
                continue
            found.extend(sorted(directory.glob(TURNS_DB)))
            found.extend(sorted(directory.glob(f"*/{TURNS_DB}")))
    return found


#: The ledger's own file name, spelt once: `schedule_db` builds a path from it
#: and `schedule_dbs` finds every one of them with it.
_SCHEDULE_DB = "schedule.db"


def schedule_dbs() -> list[Path]:
    """Every agent's ledger on this machine.

    **This is how one shared process finds the agents it serves.**  There is no
    registry of agents anywhere — a login, a conversation and a schedule all
    come into existence because somebody used one, and the directory they landed
    in is the record.  `slife2-wechat` reads the logins it wrote for the same
    reason, and `slife2.plugins.schedule.server`'s poll reads these.

    Every home, for the reason there is a registry of *homes* and none of
    agents: a ledger is found in the home it was written in, and a shared
    scheduler serves all of them — including the ones with nobody watching,
    which is what a schedule is for.
    """
    return [
        path
        for root in known_homes()
        for path in sorted((root / "agents").glob(f"*/{_SCHEDULE_DB}"))
    ]


def schedule_db(agent: str) -> Path:
    """One agent's scheduled-task ledger: `<agent>/schedule.db`.

    Beside `turns.db` rather than in a file of its own, for §5's reason — an
    agent's state is a directory, and isolation is the filesystem rather than a
    `WHERE` clause — and **the file is also how the plugin finds agents**: it is
    one shared process serving every agent, so the poll loop walks
    `agents/*/schedule.db` exactly as `slife2-wechat` walks the logins it wrote.
    Whose ledger each one is comes from the `meta` row inside it, not from the
    directory, because a directory holds the *sanitised* agent name
    (`slife2.libs.store.db.safe_agent_name`) and the two differ for a name with a space or
    a slash in it.
    """
    return agent_dir(agent) / _SCHEDULE_DB


def a2a_inbound_paths(ledger: str) -> list[Path]:
    """Every agent's *ledger* on this machine — one network's, not both.

    **The same mechanism `schedule_dbs` is, and it is a walk for the same
    reason**: a ledger is a file per agent and there is no registry of agents to
    read instead.  What a start does with what it finds is
    `slife2.plugins.a2a.server.announce_ledgers` — a peer is still owed an answer, and
    the model is the only party who can give one — which is why the file is worth
    a walk and why it is **not** an enrollment list: an agent is on the mesh
    while a window is showing it, not because it once spoke
    (`slife2.plugins.a2a.server.join`), and every home's for the reason
    `schedule_dbs` walks every home's.

    **The name is the caller's, and it is `Net.ledger`'s** — this walk reads the
    ledgers of the network the caller is serving and never the other one's.  One
    name for both networks (which is what this was) meant the process serving
    the *remote* mesh walked up the file the *local* one was holding bridges in,
    and told the model a live task could not be completed.
    """
    return [
        path
        for root in known_homes()
        for path in sorted((root / "agents").glob(f"*/{ledger}"))
    ]


def a2a_inbound_path(agent: str, ledger: str) -> Path:
    """One agent's ledger for one network: `<agent>/<ledger>`.

    What it holds is the inbound tasks this agent's mesh is holding a completion
    bridge for, which is what lets a *restart* tell its orphans from ids it has
    never seen (`slife2/plugins/a2a/inbound_store.py`).  Beside `turns.db` and
    `wechat.yaml` for §5's reason — an agent's state is a directory — and YAML
    for v1's, which is the format this file has always had: an entry in it is a
    peer still owed a reply, so it is a document rather than one long line.

    `ledger` is `Net.ledger`, and it is required rather than defaulted because
    the default would be a second spelling of one network's name in a module
    that has no business knowing which network is calling.
    """
    return agent_dir(agent) / ledger


#: The file that makes a directory a subagent's.  Spelt once for the reason
#: `_SCHEDULE_DB` is: one function writes it and one walk finds them by it.
_SUBAGENT_YAML = "subagent.yaml"


def subagent_file(agent: str, subagent: str) -> Path:
    """The record that makes `<agent>/<subagent>/` a subagent: a file inside it.

    **The file is the record and the directory is the membership.**  There is no
    registry of subagents for the reason there is none of agents
    (`schedule_dbs`): an agent's subagents *are* the directories underneath it,
    and what says which directories those are is a file in each — the way
    `wechat.yaml` says a login and `a2a.yaml` says an answer is owed.  A
    directory without this file is not a subagent's, and that is what makes
    `subagent_remove`'s blast radius provable: the cabinet lives in a directory
    under the same parent as the subagents do, and what keeps a removal off it is
    the absence of this file rather than a list of names to avoid.

    What it holds is the whole of what the subagent *is* — the model it runs on,
    the role it was made for, whether it was made as a fork, and when it was made
    — because that is the part of an agent's identity that is neither its
    conversation (elsewhere: `turns.db` beside this) nor its work (elsewhere
    again).  **The file is deleted with the directory**, which is what makes a
    subagent's settings as short-lived as the subagent: nothing else records
    them, so nothing has to be kept in step when one is removed.
    """
    return agent_dir(agent, subagent) / _SUBAGENT_YAML


def subagent_dirs(agent: str) -> list[Path]:
    """One agent's subagents, as the directories that are theirs.

    A walk of `<agent>/*/` for the file `subagent_file` writes, which is
    `schedule_dbs`' shape and its reason: what belongs to an agent is found by
    looking, because nothing keeps a list of it.  Sorted, so a caller that
    answers a model with this — the count against `subagent.max_running`, a
    roster — says the same thing twice running.

    **One agent's, not every home's**, because the only readers are that agent's
    own tools: a maker counting its subagents and a removal proving its target.
    Every other walk here spans every home (`known_homes`) because a shared
    daemon serves them all; this one is answering a call that already named an
    agent, and an agent belongs to one home.
    """
    return [
        path.parent for path in sorted(agent_dir(agent).glob(f"*/{_SUBAGENT_YAML}"))
    ]


def skills_dir() -> Path:
    """Where the skills live — one directory per skill, each with a `SKILL.md`.

    A **skill** is a playbook: a document the model reads on demand rather than
    a tool it calls, which is why this is a folder of files beside the config
    and not a section of it.  The file that names a skill and says what it is
    for is the skill's own frontmatter, so listing the skills is reading the
    directory rather than reading a list somebody has to keep in step.

    Created on the way out, like the other two, because a caller asking where
    skills go is about to put one there — and an empty directory is the honest
    answer to "which skills are installed" on a fresh install.

    The home's rather than the process's, like `agents_dir`: the playbooks a
    call is served from are the ones beside the config the caller is running.
    """
    path = home() / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path
