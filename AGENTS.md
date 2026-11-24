# Agent guide

Instructions for AI coding agents working on this repo (not for the factory's own agents: those are in `agents/`).
`CLAUDE.md` imports this file, so keep the rules here.

## What this is
A small software factory: GitHub issues in, reviewed pull requests out. The point is the infrastructure around
the agents: isolation, deterministic policy, state, and a learning loop. Read `docs/architecture.md` first.

## Rules that must hold
- Agents never hold a GitHub token. Only `publisher.py` writes to the forge, and it runs no model.
- Policy decisions are code in `policy.py`, never a model call.
- Every agent process gets `sandbox.scrubbed_env`. Never pass `os.environ` to a child process that runs a model.
- The learning loop may only write `agents/lessons/*.md`. Anything about tools, paths or approval is a human PR.
- Say what was run against a real model and what was only tested with the mock. Never invent results.

## Code conventions
- Python 3.11+, `uv`, `src/` layout, package `software_factory`. Type hints everywhere; `mypy --strict` passes.
- Tests run offline: the mock backend and LocalForge. Mark tests that call a real model `api`.
- Commits use the owner's git identity. No AI attribution lines in commits, PRs or code.

## Commands
```
make install   # uv sync, with Playwright
make check     # ruff, ruff format, mypy --strict, pytest
make demo      # offline end-to-end demo
```
