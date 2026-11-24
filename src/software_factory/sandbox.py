"""The boundary between agents and the machine.

Three rules live here, so they can be read in one place:
1. Child processes get a scrubbed environment: an allowlist of variables, never the parent's secrets.
2. File access is confined to the run's workspace, and protected paths cannot be written.
3. Commands are matched against an allowlist; nothing goes through a shell.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

# Variables a child process may inherit. Everything else, including every *_KEY and *_TOKEN, is dropped.
SAFE_ENV_VARS = ("PATH", "LANG", "LC_ALL", "TERM", "TZ", "UV_CACHE_DIR", "UV_PYTHON", "UV_NATIVE_TLS")
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
# Files no agent may read, even inside its workspace.
UNREADABLE = (".env*", "*/.env*", ".git", ".git/*", "*.pem", "*.key", ".factory/*")


class SandboxError(Exception):
    pass


def scrubbed_env(home: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """An environment with only allowlisted variables plus a private HOME.

    `extra` is for values the caller hands over deliberately (for example a scoped proxy key for the
    claude-code backend); they are passed through as given and listed in the trace by name only.
    """
    env = {k: v for k, v in os.environ.items() if k in SAFE_ENV_VARS}
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env.update(extra or {})
    return env


def leaked_secret_names(env: Mapping[str, str], allowed: Sequence[str] = ()) -> list[str]:
    """Names in `env` that look like secrets and were not explicitly allowed. Used as a tripwire."""
    return sorted(k for k in env if any(m in k.upper() for m in SECRET_MARKERS) and k not in allowed)


class Workspace:
    """A directory an agent may touch, with write protection for sensitive paths."""

    def __init__(self, root: Path, protected: Sequence[str], writable: Sequence[str] = ("*",)) -> None:
        self.root = root.resolve()
        self.protected = list(protected)
        self.writable = list(writable)

    def resolve(self, rel: str) -> Path:
        """Resolve a repo-relative path, refusing anything that escapes the workspace (.., symlinks, abs)."""
        if not rel or Path(rel).is_absolute():
            raise SandboxError(f"path must be relative to the workspace: {rel!r}")
        path = (self.root / rel).resolve()
        if path != self.root and self.root not in path.parents:
            raise SandboxError(f"path escapes the workspace: {rel!r}")
        return path

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def is_protected(self, rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, pat) or rel.startswith(pat.rstrip("*")) for pat in self.protected)

    def check_writable(self, rel: str) -> Path:
        path = self.resolve(rel)
        norm = self.rel(path)
        if self.is_protected(norm):
            raise SandboxError(f"path is protected and needs a human change: {norm}")
        if not any(fnmatch.fnmatch(norm, pat) for pat in self.writable):
            raise SandboxError(f"path is outside the plan's files: {norm}")
        return path

    def check_readable(self, rel: str) -> Path:
        path = self.resolve(rel)
        norm = self.rel(path) if path != self.root else "."
        if any(fnmatch.fnmatch(norm, pat) for pat in UNREADABLE):
            raise SandboxError(f"path may not be read by an agent: {norm}")
        return path

    def read(self, rel: str, max_chars: int = 20_000) -> str:
        path = self.check_readable(rel)
        if not path.is_file():
            raise SandboxError(f"not a file: {rel}")
        text = path.read_text(errors="replace")
        return text if len(text) <= max_chars else text[:max_chars] + f"\n... [truncated {len(text)} chars]"

    def write(self, rel: str, content: str) -> None:
        path = self.check_writable(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def list_files(self, sub: str = ".") -> list[str]:
        base = self.resolve(sub) if sub != "." else self.root
        skip = {"__pycache__", "node_modules"}
        return sorted(
            self.rel(p)
            for p in base.rglob("*")
            if p.is_file() and not any(x.startswith(".") or x in skip for x in p.relative_to(self.root).parts)
        )


def command_allowed(cmd: Sequence[str], allowlist: Sequence[Sequence[str]]) -> bool:
    """A command is allowed if it starts with one of the allowlisted prefixes, token for token."""
    return any(list(cmd[: len(prefix)]) == list(prefix) for prefix in allowlist)


def run(
    cmd: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    allowlist: Sequence[Sequence[str]],
    timeout: int = 300,
    tail: int = 4000,
) -> tuple[int, str]:
    if not command_allowed(cmd, allowlist):
        raise SandboxError(f"command not in allowlist: {list(cmd)}")
    try:
        proc = subprocess.run(
            list(cmd), cwd=cwd, env=dict(env), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        return 124, f"timed out after {timeout}s: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out[-tail:]
