"""The few git operations the factory needs. Plain subprocess calls, no credentials involved."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False, env=env)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def add_worktree(repo: Path, path: Path, branch: str, base: str) -> None:
    """A fresh checkout of `base` on a new branch: each run starts from clean files, never another run's."""
    git(repo, "worktree", "add", "--quiet", "-b", branch, str(path), base)


def remove_worktree(repo: Path, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path))


def diff(ws: Path, base: str) -> tuple[list[str], str]:
    """Files changed and the unified diff of the working tree against `base`, including new files."""
    git(ws, "add", "--all")
    names = [n for n in git(ws, "diff", "--cached", "--name-only", base).splitlines() if n]
    return names, git(ws, "diff", "--cached", base)


def commit(ws: Path, message: str) -> str:
    git(ws, "add", "--all")
    git(
        ws, "-c", "user.name=efficient-software-factory", "-c", "user.email=factory@users.noreply.github.com",
        "commit", "--quiet", "--no-verify", "-m", message,
    )  # fmt: skip
    return git(ws, "rev-parse", "HEAD").strip()
