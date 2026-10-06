# slife2

**Terminal-based AI agent — slife v2, a clean-slate rebuild.**

This repository is currently a **scaffold**. It holds the uv workspace, the build
backend, the test and lint configuration, and CI — and no agent code. The v2
design is written next, and lands in a project that is already sound.

Until then, [slife](https://github.com/juzcn/slife) (v1) is the working agent. The
two coexist: v1 keeps running untouched while v2 is designed from scratch, rather
than being bent around decisions v1 already made.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13.

```bash
uv sync                    # create .venv and resolve the workspace
uv run pytest              # tests
uv run ruff check .        # lint
uv run slife2              # the console script (prints a placeholder)
uv run python -m slife2    # the same, via the module
uv build                   # build the wheel
```

Type checking uses pyright as a **uv tool**, not a project dependency:

```bash
uv tool install pyright
pyright                    # run from the repo root
```

## Layout

```
slife2/
├─ pyproject.toml     # project, workspace, pytest, ruff and pyright config
├─ slife2/            # the package — the repo root *is* the distribution
│  ├─ __init__.py
│  └─ __main__.py
├─ tests/
└─ .github/workflows/ci.yml
```

The root `pyproject.toml` declares `[tool.uv.workspace]` so that a sibling package
(`credstore`, `local-embed`, …) can join by adding its directory and one entry to
`members`. The root is a workspace member in its own right and is deliberately not
listed there.

## License

MIT
