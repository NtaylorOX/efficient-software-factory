"""Headless Claude Code (`claude -p`) as a backend.

Two ways to authenticate, chosen by which secret is set:
- Subscription (preferred): FACTORY_CLAUDE_CODE_OAUTH_TOKEN, a long-lived token from `claude setup-token`, passed
  to the child as CLAUDE_CODE_OAUTH_TOKEN. Usage counts against your Claude plan, not API billing.
- API key: FACTORY_ANTHROPIC_API_KEY (ideally a LiteLLM virtual key, with FACTORY_ANTHROPIC_BASE_URL at the proxy).

Hardening, all from flags so it is visible here:
- `--restricted`: no Bash or WebFetch, user/project/local settings files ignored, file tools confined to the
  working directory. Only the settings we pass with `--settings` apply.
- `--settings`: the factory's hooks (hooks.py). PreToolUse runs the Guard on every tool call, PostToolUse
  traces it, and Stop runs the stop gate (lint, scenario tags) before the coder may finish. Not `--bare` or
  `--safe-mode`: both switch hooks off.
- `--tools` lists the exact built-in tools for the role; `--strict-mcp-config` with no config means no MCP;
  `--disable-slash-commands` means no skills or commands from the repo.
- `--no-session-persistence`: nothing from one ticket is resumable by the next.
- The process gets a scrubbed env with a throwaway HOME. The key it does get should be a LiteLLM virtual
  key (ANTHROPIC_BASE_URL pointed at the proxy), so the model never holds a provider key.

The repo's CLAUDE.md is still read as context. It comes from the reviewed base branch, and agents can never
write it (protected path).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from software_factory.backends.base import BackendError, T, parse_json
from software_factory.config import Role
from software_factory.hooks import Guard, claude_settings
from software_factory.models import Usage
from software_factory.sandbox import scrubbed_env

TOOL_MAP = {
    "read_file": ["Read"],
    "list_files": ["Glob"],
    "search": ["Grep"],
    "write_file": ["Edit", "Write"],
}


def claude_tools(role_tools: list[str]) -> list[str]:
    return sorted({t for name in role_tools for t in TOOL_MAP[name]})


def credential_env() -> dict[str, str]:
    """The only credential handed to the child, renamed from FACTORY_* so it is never inherited by accident.

    A subscription token wins over an API key, and the child never gets both.
    """
    if token := os.environ.get("FACTORY_CLAUDE_CODE_OAUTH_TOKEN"):
        return {"CLAUDE_CODE_OAUTH_TOKEN": token}
    env = {}
    if key := os.environ.get("FACTORY_ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = key
    if url := os.environ.get("FACTORY_ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = url
    return env


NO_HOOKS: dict[str, Any] = {"disableAllHooks": True}  # structured calls use no tools, so nothing to guard


def build_command(
    role: Role, model: str, schema: dict[str, Any] | None, tools: list[str], settings: dict[str, Any]
) -> list[str]:
    cmd = [
        "claude", "-p", "--restricted", "--settings", json.dumps(settings), "--strict-mcp-config",
        "--disable-slash-commands", "--no-session-persistence",
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
        creds = credential_env()
        if not creds:
            raise BackendError("claude-code backend needs FACTORY_CLAUDE_CODE_OAUTH_TOKEN or FACTORY_ANTHROPIC_API_KEY")
        proc = subprocess.run(
            cmd, input=prompt, cwd=cwd, env=scrubbed_env(home, creds),
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
        cmd = build_command(role, model, schema.model_json_schema(), [], NO_HOOKS)
        out = self._run(cmd, user, cwd, cwd / "home")
        payload = out.get("structured_output")
        result = (
            schema.model_validate(payload) if payload is not None else parse_json(str(out.get("result", "")), schema)
        )
        return result, self._usage(out)

    def agentic(self, role: Role, task: str, guard: Guard, model: str, home: Path) -> tuple[str, Usage]:
        if guard.path is None:
            raise BackendError("claude-code needs the guard saved to a file for its hooks")
        cmd = build_command(role, model, None, claude_tools(role.tools), claude_settings(guard.path))
        out = self._run(cmd, task, guard.ws.root, home)
        return str(out.get("result", "")), self._usage(out)
