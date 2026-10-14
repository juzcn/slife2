"""Review gate: no hardcoded time values outside the central registry.

Scans ``slife2/**/*.py`` and ``tests/**/*.py`` and fails on:
  * a numeric literal in a time-style keyword argument of any call
    (``timeout=``, ``interval=``, ``keepalive=`` …),
  * a numeric literal as the timeout argument of ``wait_for`` /
    ``asyncio.timeout`` / ``httpx2.Timeout`` (the cancel-on-timeout
    primitives),
  * a numeric literal as the delay of ``sleep`` (a cadence) — in ``slife2/``,
    where a sleep is a duration someone chose; a test's sleep is a
    synchronisation primitive and is left alone,
  * a module-level (or class-level, or annotated) numeric constant assigned to
    a *time-style name — budget names (TIMEOUT/DEADLINE/WAIT/STALL/GRACE/DELAY/
    INTERVAL/… ) and cadence names (BACKOFF/LIFETIME/TTL/POLL/PERIOD/KEEPALIVE/
    HEARTBEAT/REFRESH/AGE) alike, with simple arithmetic folded so ``24 * 60``
    counts as a literal,
  * ``deadline — X = <expr> + <numeric literal>`` assignments,
  * a numeric default on a timeout/deadline-named *function argument* (the
    def-time-default pattern, which captures the registry's value at import and
    ignores a patch),
  * **a registry lookup as a function-argument default at all** — `timeout:
    float = _T.timeouts.grace.lock`.  This is the same capture as the numeric
    case and slips past it by being *spelled* as a lookup: the value is read
    once, at import, so `monkeypatch.setattr(timeouts.grace, "lock", …)` — the
    documented way to tune a value, and what the registry convention below
    promises — has no effect on any call that took the default.  Three of these
    were live (`runtime.exclusive`, `runtime.start_lock`, `a2a.broker.ensure`),
    each next to a docstring stating the rule it broke.

Anything that must stay literal is either covered by the allowlist below or
carries a ``# timeouts: …`` comment on the same line saying why.

**What it does not ask of a test, and one thing it does.**  The literal rules —
1, 2, 3 and 7 — apply to ``slife2/`` only.  A *magnitude* in a test is a fixture:
``await asyncio.wait_for(loop.stop(), timeout=1)`` and ``sleep(3600)`` mean "long
enough to be a hang" and "until cancelled", and routing either through a
registry keyed on the daemon's real budgets would be a number about nothing.  Two
rules survive into ``tests/`` because they are about *names in a second seat*
rather than magnitudes, and a second seat is a second seat wherever it sits:
rule 4 (a module constant with a time-style name) and rule 8 (a registry read in
a signature — see below).  A test helper's ``timeout: float = 5.0`` is a fixture;
a test helper's ``timeout: float = _T.timeouts.ready.probe`` is the capture bug
in a file that is about to assert the registry works.

Model rules behind this gate (see ``slife2/helpers/timeouts.py`` and DESIGN.md §4.7):
  * values live in ``slife2/helpers/timeouts.py`` — consumers read them at call time
    via ``slife2.helpers.timeouts.timeouts.<role>.<key>``;
  * budgets (``work``/``ready``/``grace``/``transport``/``storage``/
    ``deliver``) bound an await; cadences (``pacing``) set how often something
    runs.  Both are registry-owned — a cadence left as a module constant is a
    second seat for a value nobody can find;
  * ``slife2/helpers/timeouts.py`` itself is the registry — exempt.

This is v1's gate, ported: the same rules, because the failure it prevents is
the same one — a number nobody can find, and an outer bound nobody checked the
inner bounds against.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = (ROOT / "slife2", ROOT / "tests")
EXEMPT = {"slife2/helpers/timeouts.py", "tests/test_no_magic_timeouts.py"}

#: relpath → symbols that intentionally stay local.  An entry here is a claim
#: that the value is not a time budget OR a cadence — a count, a status code, a
#: profile shape — and the comment says which.
ALLOWLIST: dict[str, frozenset[str]] = {
    "slife2/plugins/wechat/server.py": frozenset({"BACKOFF_FACTOR"}),
    "slife2/libs/host/runtime.py": frozenset({"WAIT_TIMEOUT"}),
    "slife2/libs/store/db.py": frozenset({"MAX_AGENT_NAME"}),
}

#: Name tokens that mark a number as time-valued.  Budget words and cadence
#: words are both here.
_NAME_BANNED = re.compile(
    r"(TIMEOUT|_DELAY|_STALL|_GRACE|_WAIT|_KEEPALIVE|_S$|DEADLINE"
    r"|INTERVAL|CADENCE|BACKOFF|LIFETIME|_TTL|_POLL|PERIOD|HEARTBEAT|REFRESH"
    r"|_AGE|MINUTE|_HOUR)"
)

#: Argument names that mark a numeric default as a time value.
_ARG_BANNED = re.compile(
    r"(TIMEOUT|DEADLINE|DELAY|INTERVAL|CADENCE|BACKOFF|LIFETIME|_WAIT|GRACE"
    r"|STALL|KEEPALIVE|_S$|POLL|PERIOD|REFRESH|_AGE)"
)


def _source_line(path: Path, node: ast.AST) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""


#: The exemption marker.  v1 spelled it as a `noqa` for timeouts; newer ruff
#: reads that shape as a malformed directive and warns on every run, so the same
#: claim is made as a plain note instead.  It says *why* the literal stays, and
#: this gate is what reads it.
MARKER = "# timeouts:"


def _has_noqa(line: str) -> bool:
    return MARKER in line


def _numeric(v: ast.expr | None) -> bool:
    return (
        isinstance(v, ast.Constant)
        and isinstance(v.value, (int, float))
        and not isinstance(v.value, bool)
    )


#: A read of the registry.  Matched on *source*, because the rule is about the
#: shape of the expression rather than its value — and the shape is the whole
#: problem: a lookup in a signature looks like the convention it breaks.
_REGISTRY_READ = re.compile(
    r"\btimeouts\s*\.\s*(work|ready|grace|transport|stream|storage|deliver|pacing)"
    r"\s*\.\s*[a-z0-9_]+"
)


def _registry_default(value: ast.expr | None, src: str) -> str | None:
    """The registry read in *value*, if that is what the default is."""
    if value is None:
        return None
    segment = ast.get_source_segment(src, value)
    return segment if segment and _REGISTRY_READ.search(segment) else None


def _fold(v: ast.expr | None) -> float | None:
    """Constant-fold simple arithmetic — ``24 * 60`` is as hardcoded as 1440."""
    if _numeric(v):
        return v.value  # type: ignore[union-attr]
    if isinstance(v, ast.BinOp) and isinstance(
        v.op, (ast.Mult, ast.Add, ast.Sub, ast.Div)
    ):
        left, right = _fold(v.left), _fold(v.right)
        if left is None or right is None:
            return None
        try:
            return {
                ast.Mult: left * right,
                ast.Add: left + right,
                ast.Sub: left - right,
                ast.Div: left / right,
            }[type(v.op)]
        except ZeroDivisionError:
            return None
    return None


def _collect(path: Path) -> list[str]:
    """Return the list of violations in one file (empty = clean)."""
    rel = path.relative_to(ROOT).as_posix()
    if rel in EXEMPT:
        return []
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    findings: list[str] = []
    allowed = ALLOWLIST.get(rel, frozenset())
    is_prod = rel.startswith("slife2/")

    def note(node: ast.AST, msg: str) -> None:
        if not _has_noqa(_source_line(path, node)):
            findings.append(f"{rel}:{node.lineno} {msg}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and is_prod:
            # 1) a numeric literal on a time-style keyword argument of any call.
            for kw in node.keywords:
                if not kw.arg or not _numeric(kw.value):
                    continue
                if _ARG_BANNED.search(kw.arg.upper()):
                    note(
                        kw.value,
                        f"{kw.arg}={kw.value.value!r} — use "
                        "timeouts.<role>.<key> or a # timeouts: note",
                    )
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            # 2) timeout(N) / Timeout(N) / wait_for(..., N)
            if name in ("timeout", "Timeout"):
                pos = node.args
            elif name == "wait_for":
                pos = node.args[1:]
            elif name == "sleep":
                # 3) sleep(N) — a bare cadence.  `sleep(0)` is a scheduling
                #    yield, not a duration.
                pos = node.args
                if pos and _numeric(pos[0]) and pos[0].value == 0:
                    pos = ()
            else:
                pos = ()
            for a in pos:
                if _numeric(a):
                    note(a, f"{name}({a.value!r}) — use timeouts.<role>.<key>")
        elif isinstance(node, ast.Assign):
            # 4) numeric constant assigned to a time-style name
            for tgt in node.targets:
                tgt_name = (
                    tgt.id
                    if isinstance(tgt, ast.Name)
                    else tgt.attr
                    if isinstance(tgt, ast.Attribute)
                    else None
                )
                if not tgt_name or tgt_name in allowed:
                    continue
                value = _fold(node.value)
                if value is not None and _NAME_BANNED.search(tgt_name):
                    note(
                        node,
                        f"{tgt_name} = {value!r} — time constant; must be a "
                        "call-time registry lookup",
                    )
            # 5) deadline-style: name = <expr> + literal
            if (
                len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and "deadline" in node.targets[0].id.lower()
                and isinstance(node.value, ast.BinOp)
                and isinstance(node.value.op, ast.Add)
                and _fold(node.value.right) is not None
            ):
                note(
                    node,
                    f"{node.targets[0].id} + literal — a resource deadline must "
                    "read timeouts.<role>.<key>",
                )
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            # 6) annotated constant / dataclass field with a time-style name
            if isinstance(node.target, ast.Name) and node.target.id not in allowed:
                value = _fold(node.value)
                if value is not None and _NAME_BANNED.search(node.target.id):
                    note(
                        node,
                        f"{node.target.id}: ... = {value!r} — time constant; must "
                        "be a call-time registry lookup",
                    )
        # 7) numeric default on a timeout/deadline-named function argument.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            pos_defaults = [None] * (len(args.args) - len(args.defaults)) + list(
                args.defaults
            )
            arg_defs = list(zip(args.args, pos_defaults, strict=False)) + list(
                zip(args.kwonlyargs, args.kw_defaults, strict=False)
            )
            for arg, default in arg_defs:
                if default is None:
                    continue
                if (
                    is_prod
                    and _fold(default) is not None
                    and _ARG_BANNED.search(arg.arg.upper())
                ):
                    note(
                        default,
                        f"def {node.name}(... {arg.arg}={_fold(default)!r}) — "
                        "numeric time default; use None + a call-time registry "
                        "lookup",
                    )
                # 8) a registry lookup captured at def time.  Same defect as the
                #    numeric case and invisible to it, because the expression
                #    reads as the convention rather than as a literal.  **Not
                #    waived for tests**, unlike 7: a numeric default in a test is
                #    a fixture's magnitude, but a registry read in one is the
                #    capture bug wherever it lives — and a test that reaches into
                #    the registry is a test about the registry.
                elif (read := _registry_default(default, src)) is not None:
                    note(
                        default,
                        f"def {node.name}(... {arg.arg}={read}) — a def-time "
                        "default captures the registry at import, so patching it "
                        "has no effect; use None + a call-time lookup",
                    )
    return findings


def _scan_all() -> list[str]:
    violations: list[str] = []
    for base in SCAN_DIRS:
        for path in sorted(base.rglob("*.py")):
            try:
                violations.extend(_collect(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
    return violations


def test_no_magic_timeouts():
    violations = _scan_all()
    assert not violations, (
        "hardcoded time values — route every value through slife2/helpers/timeouts.py:\n"
        + "\n".join(violations)
    )


def _scan_source(src: str) -> bool:
    """Run the literal rules against a snippet (for the neg tests)."""
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg and _ARG_BANNED.search(kw.arg.upper()) and _numeric(kw.value):
                    return True
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            if (
                name in ("timeout", "Timeout", "sleep")
                and node.args
                and _numeric(node.args[0])
            ):
                return True
            if name == "wait_for" and len(node.args) > 1 and _numeric(node.args[1]):
                return True
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and _fold(node.value) is not None
            and _NAME_BANNED.search(node.targets[0].id)
        ):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            pos_defaults = [None] * (len(args.args) - len(args.defaults)) + list(
                args.defaults
            )
            arg_defs = list(zip(args.args, pos_defaults, strict=False)) + list(
                zip(args.kwonlyargs, args.kw_defaults, strict=False)
            )
            for arg, default in arg_defs:
                if default is None:
                    continue
                if _ARG_BANNED.search(arg.arg.upper()) and _fold(default) is not None:
                    return True
                if _registry_default(default, src) is not None:
                    return True
    return False


def test_planted_violation_is_caught():
    """Sanity check that the scanning logic actually fires on a literal."""
    assert _scan_source("async def f():\n    await asyncio.wait_for(g(), timeout=1.2)")
    assert _scan_source(
        "async def f():\n    async with asyncio.timeout(9):\n        pass"
    )
    assert _scan_source("c = httpx2.Timeout(30.0)")
    assert _scan_source("async def f():\n    await asyncio.sleep(5)")
    # cadence names and folded arithmetic are time values too
    assert _scan_source("_POLL_INTERVAL = 15.0")
    assert _scan_source("HEARTBEAT_INTERVAL = 1800")
    assert _scan_source("SESSION_MAX_AGE = 23 * 3600")
    assert _scan_source("MAX_WAIT_MINUTES = 24 * 60")
    assert _scan_source("def f(*, deadline_s: float = 1200.0) -> None: ...")
    # A registry *lookup* is the same capture as a literal, and the one that got
    # past this gate: it reads as the convention rather than as a number.
    assert _scan_source(
        "def f(*, timeout: float = _T.timeouts.grace.lock) -> None: ..."
    )
    assert _scan_source("def f(*, timeout: float = timeouts.ready.broker) -> None: ...")
    assert not _scan_source(
        "async def f():\n    await asyncio.wait_for(g(), timeout=_T.timeouts.ready.probe)"
    )
    assert not _scan_source("_CONNECT_RETRY_ATTEMPTS = 20")  # a count, not a time
    assert not _scan_source("_STDERR_BUFFER_LIMIT = 500")  # a count, not a time


# ── Companion: every declared registry key is consumed ─────────────────


REGISTRY_KEYS = re.compile(
    r"timeouts\.(work|ready|grace|transport|stream|storage|deliver|pacing)"
    r"\.([a-z0-9_]+)"
)
_ROLE_NAMES = (
    "work",
    "ready",
    "grace",
    "transport",
    "stream",
    "storage",
    "deliver",
    "pacing",
)


def test_every_registry_key_is_consumed():
    """No dead keys: each declared key must be referenced by at least one site.

    The failure it catches is the one a registry invites: a value declared,
    tuned, documented — and read by nothing, so the number that matters is
    still the literal somewhere else.
    """
    from dataclasses import fields

    from slife2.helpers.timeouts import Timeouts

    mentioned: set[tuple[str, str]] = set()
    for base in SCAN_DIRS:
        for path in base.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            mentioned.update(REGISTRY_KEYS.findall(text))

    declared: set[tuple[str, str]] = set()
    for role, obj in Timeouts().__dict__.items():
        if role in _ROLE_NAMES:
            for f in fields(obj):
                declared.add((role, f.name))

    missing = declared - mentioned
    assert not missing, "unreferenced timeout keys — consume or remove: " + ", ".join(
        f"{r}.{k}" for r, k in sorted(missing)
    )
