"""Headless Claude Code (`claude -p`) as a backend.

Hardening, all from flags so it is visible here:
- `--bare`: no CLAUDE.md discovery, hooks, plugins, memory or keychain; auth is ANTHROPIC_API_KEY only.
- `--restricted`: no Bash or WebFetch, settings files ignored, file tools confined to the working directory.
- `--tools` lists the exact built-in tools for the role; `--strict-mcp-config` with no config means no MCP.
- `--no-session-persistence`: nothing from one ticket is resumable by the next.
- The process gets a scrubbed env with a throwaway HOME. The key it does get should be a LiteLLM virtual
  key (ANTHROPIC_BASE_URL pointed at the proxy), so the model never holds a provider key.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from software_factory.backends.base import BackendError, T, parse_json
from software_factory.config import Role
from software_factory.models import Usage
from software_factory.sandbox import Workspace, scrubbed_env

TOOL_MAP = {
    "read_file": ["Read"],
    "list_files": ["Glob"],
    "search": ["Grep"],
    "write_file": ["Edit", "Write"],
}


def claude_tools(role_tools: list[str]) -> list[str]:
    return sorted({t for name in role_tools for t in TOOL_MAP[name]})


def credential_env() -> dict[str, str]:
    """The only credential handed to the child, renamed from FACTORY_* so it is never inherited by accident."""
    env = {}
    if key := os.environ.get("FACTORY_ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = key
    if url := os.environ.get("FACTORY_ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = url
    return env


def build_command(role: Role, model: str, schema: dict[str, Any] | None, tools: list[str]) -> list[str]:
    cmd = [
        "claude", "-p", "--bare", "--restricted", "--strict-mcp-config", "--no-session-persistence",
        "--output-format", "json", "--model", model, "--max-turns", str(role.max_turns),
        "--system-prompt", role.system_prompt(), "--tools", ",".join(tools) if tools else "",
    ]  # fmt: skip
    if tools:
        cmd += ["--allowedTools", ",".join(tools), "--permission-mode", "acceptEdits"]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema)]
    return cmd


class ClaudeCodeBackend:
    name = "claude-code"

    def __init__(self, timeout: int = 900) -> None:
        self.timeout = timeout

    def _run(self, cmd: list[str], prompt: str, cwd: Path, home: Path) -> dict[str, Any]:
        home.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            cmd, input=prompt, cwd=cwd, env=scrubbed_env(home, credential_env()),
            capture_output=True, text=True, timeout=self.timeout, check=False,
        )  # fmt: skip
        if proc.returncode != 0:
            raise BackendError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[-800:]}")
        try:
            out: dict[str, Any] = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise BackendError(f"claude did not return JSON: {proc.stdout[-400:]!r}") from exc
        if out.get("is_error"):
            raise BackendError(f"claude reported an error: {out.get('result')!r}")
        return out

    @staticmethod
    def _usage(out: dict[str, Any]) -> Usage:
        u = out.get("usage") or {}
        cached = int(u.get("cache_read_input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0))
        return Usage(input_tokens=int(u.get("input_tokens", 0)) + cached, output_tokens=int(u.get("output_tokens", 0)))

    def structured(self, role: Role, user: str, schema: type[T], model: str) -> tuple[T, Usage]:
        cwd = Path.cwd() / ".factory" / "tmp" / role.name
        cwd.mkdir(parents=True, exist_ok=True)  # empty dir: a structured call reads nothing from disk
        out = self._run(build_command(role, model, schema.model_json_schema(), []), user, cwd, cwd / "home")
        payload = out.get("structured_output")
        result = (
            schema.model_validate(payload) if payload is not None else parse_json(str(out.get("result", "")), schema)
        )
        return result, self._usage(out)

    def agentic(self, role: Role, task: str, ws: Workspace, model: str, home: Path) -> tuple[str, Usage]:
        out = self._run(build_command(role, model, None, claude_tools(role.tools)), task, ws.root, home)
        return str(out.get("result", "")), self._usage(out)
