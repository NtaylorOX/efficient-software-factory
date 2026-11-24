"""One guard for every agentic backend, and the Claude Code hooks that run it.

The coder's rules are enforced at three points of its session, whatever model or backend runs it:

    before each tool call   allow or deny: role tool allowlist, workspace confinement, plan-only writes,
                            protected paths, unreadable secrets (.env, .git, keys)
    after each tool call    one line in the run's trace.jsonl
    when it wants to stop   the stop gate: lint must pass and every scenario checked "by test" must be
                            tagged in a test. If not, the agent is sent back once with the problems.

LiteLLM and the mock call `Guard` directly. Claude Code gets the same checks as hooks: `claude_settings()`
registers `python -m software_factory.hooks <event> <guard.json>` for PreToolUse, PostToolUse and Stop, and
`main()` below answers them. The guard file sits in the run folder, outside the workspace, so the agent can
neither read nor edit its own rules. A hook that crashes denies (fail closed).
"""

from __future__ import annotations

import fnmatch
import json
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from software_factory.policy import tagged_scenarios
from software_factory.sandbox import SandboxError, Workspace, run, scrubbed_env
from software_factory.state import redact

WRITE_TOOLS = {"write_file"}
# Claude Code tool name -> (our tool name, the input field holding the path)
CLAUDE_TOOLS = {
    "Read": ("read_file", "file_path"),
    "Write": ("write_file", "file_path"),
    "Edit": ("write_file", "file_path"),
    "MultiEdit": ("write_file", "file_path"),
    "Glob": ("list_files", "path"),
    "Grep": ("search", "path"),
}


class GuardPolicy(BaseModel):
    """Everything the guard needs, written by the pipeline to <run>/guard.json before the coder starts."""

    run_id: str
    workspace: str
    home: str
    trace: str
    tools: list[str]
    protected: list[str]
    writable: list[str]
    test_globs: list[str]
    tagged_scenarios: list[str]  # scenario ids the plan checks by test
    stop_commands: list[list[str]]  # e.g. ruff; run by the guard itself, never by the agent


class Guard:
    def __init__(self, policy: GuardPolicy, path: Path | None = None) -> None:
        self.policy, self.path = policy, path
        self.ws = Workspace(Path(policy.workspace), policy.protected, policy.writable)
        if path is not None:
            path.write_text(policy.model_dump_json(indent=1))

    @classmethod
    def load(cls, path: Path) -> Guard:
        return cls(GuardPolicy.model_validate_json(path.read_text()))

    # ---- before a tool call --------------------------------------------------------------------------

    def check(self, tool: str, path: str | None) -> str | None:
        """None if the call may go ahead, else the reason it is denied."""
        if tool not in self.policy.tools:
            return f"tool {tool!r} is not available to this agent"
        rel = self._relative(path) if path else "."
        try:
            if tool in WRITE_TOOLS:
                self.ws.check_writable(rel)
            else:
                self.ws.check_readable(rel)
        except SandboxError as exc:
            return str(exc)
        return None

    def _relative(self, path: str) -> str:
        """Claude Code sends absolute paths. Anything outside the workspace stays absolute and is refused."""
        p = Path(path)
        if p.is_absolute() and (p == self.ws.root or self.ws.root in p.parents):
            return p.relative_to(self.ws.root).as_posix() or "."
        return path

    # ---- after a tool call, and at the stop gate --------------------------------------------------------

    def record(self, kind: str, **data: Any) -> None:
        line = {"t": round(time.time(), 3), "run": self.policy.run_id, "kind": kind, **data}
        with Path(self.policy.trace).open("a") as f:
            f.write(redact(json.dumps(line, default=str)) + "\n")

    def stop_problems(self) -> list[str]:
        """What must be true before the agent may finish. The factory checks it all again in verify."""
        tests = "\n".join(
            (self.ws.root / f).read_text()
            for f in self.policy.writable
            if any(fnmatch.fnmatch(f, g) for g in self.policy.test_globs) and (self.ws.root / f).is_file()
        )
        found = tagged_scenarios(tests)
        problems = [f"no test is tagged '# scenario: {s}'" for s in self.policy.tagged_scenarios if s not in found]
        env = scrubbed_env(Path(self.policy.home))  # the hook's own env holds the model credential: drop it
        for cmd in self.policy.stop_commands:
            code, out = run(cmd, self.ws.root, env, allowlist=[cmd])
            if code:
                problems.append(f"$ {' '.join(cmd[2:] if cmd[1:2] == ['-m'] else cmd)} (exit {code})\n{out[-800:]}")
        return problems

    def stop(self, already_sent_back: bool) -> str | None:
        """The stop gate. Returns the reason to keep working, or None to let the agent finish.

        An agent is sent back at most once per session, so a gate that cannot be satisfied cannot loop.
        """
        problems = self.stop_problems()
        blocked = bool(problems) and not already_sent_back
        self.record("stop_gate", blocked=blocked, problems=[p.splitlines()[0] for p in problems])
        if not blocked:
            return None
        return "Before you finish, fix these. The factory will run the same checks again:\n" + "\n".join(problems)


def claude_settings(guard_file: Path) -> dict[str, Any]:
    """Settings passed with `--settings`. Under `--restricted`, user, project and local settings are ignored,
    so these hooks and permissions are the only ones that apply."""

    def hook(event: str) -> list[dict[str, Any]]:
        cmd = f"{sys.executable} -m software_factory.hooks {event} {guard_file}"
        return [{"matcher": "*", "hooks": [{"type": "command", "command": cmd, "timeout": 120}]}]

    return {
        "hooks": {"PreToolUse": hook("pre-tool-use"), "PostToolUse": hook("post-tool-use"), "Stop": hook("stop")},
        "permissions": {"deny": ["Bash", "WebFetch", "WebSearch", "Task", "Read(./.env*)", "Read(./.git/**)"]},
        "disableAllHooks": False,
    }


def handle(event: str, guard: Guard, payload: dict[str, Any]) -> dict[str, Any]:
    """Answer one Claude Code hook call. The returned dict is printed as the hook's JSON output."""
    if event == "pre-tool-use":
        name = str(payload.get("tool_name", ""))
        tool, field = CLAUDE_TOOLS.get(name, (name, ""))
        args = payload.get("tool_input") or {}
        path = args.get(field) if field else None
        reason = guard.check(tool, str(path) if path else None)
        if name == "Glob" and ".." in str(args.get("pattern", "")):
            reason = "glob patterns may not leave the workspace"
        if reason is None:
            return {}
        guard.record("tool", tool=name, target=path, allowed=False, reason=reason)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    if event == "post-tool-use":
        args = payload.get("tool_input") or {}
        target = next((args[k] for k in ("file_path", "path", "pattern") if k in args), None)
        guard.record("tool", tool=payload.get("tool_name"), target=target, allowed=True)
        return {}
    if event == "stop":
        reason = guard.stop(bool(payload.get("stop_hook_active")))
        return {"decision": "block", "reason": reason} if reason else {}
    raise ValueError(f"unknown hook event {event!r}")


def main(argv: list[str]) -> int:
    event, guard_file = argv[1], Path(argv[2])
    try:
        out = handle(event, Guard.load(guard_file), json.loads(sys.stdin.read() or "{}"))
    except Exception as exc:  # fail closed: exit code 2 blocks the tool call and shows the reason to the agent
        print(f"factory guard error, refusing: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
