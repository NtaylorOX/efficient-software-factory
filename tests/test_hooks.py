"""The guard and the Claude Code hook protocol, without a model: hooks are plain JSON in, JSON out."""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from software_factory.hooks import Guard, GuardPolicy, handle, main


def make_guard(tmp_path: Path, **over: object) -> Guard:
    ws = tmp_path / "ws"
    (ws / "demo_app" / "tests").mkdir(parents=True)
    (ws / ".env").write_text("LITELLM_API_KEY=sk-should-never-be-read\n")
    policy = GuardPolicy(
        run_id="r1", workspace=str(ws), home=str(tmp_path / "home"), trace=str(tmp_path / "trace.jsonl"),
        tools=["read_file", "list_files", "search", "write_file"], protected=["agents/*", "specs/*"],
        writable=["demo_app/app.py", "demo_app/tests/test_app.py"], test_globs=["demo_app/tests/*"],
        tagged_scenarios=["S1"], stop_commands=[],
    )  # fmt: skip
    return Guard(policy.model_copy(update=over), tmp_path / "guard.json")


def pre(guard: Guard, tool: str, **tool_input: str) -> str | None:
    out = handle("pre-tool-use", guard, {"tool_name": tool, "tool_input": tool_input})
    return out["hookSpecificOutput"]["permissionDecisionReason"] if out else None


def test_pre_tool_use_applies_the_coder_rules(tmp_path: Path) -> None:
    g = make_guard(tmp_path)
    root = g.ws.root
    assert pre(g, "Read", file_path=str(root / "demo_app" / "app.py")) is None  # absolute path inside: fine
    assert pre(g, "Write", file_path=str(root / "demo_app" / "app.py")) is None
    assert "outside the plan" in str(pre(g, "Write", file_path=str(root / "demo_app" / "other.py")))
    assert "protected" in str(pre(g, "Edit", file_path=str(root / "specs" / "notes" / "spec.md")))
    assert "may not be read" in str(pre(g, "Read", file_path=str(root / ".env")))
    assert "relative to the workspace" in str(pre(g, "Read", file_path="/proc/self/environ"))
    assert "escapes" in str(pre(g, "Read", file_path="demo_app/../../guard.json"))
    assert "not available" in str(pre(g, "Bash", command="env"))
    assert "leave the workspace" in str(pre(g, "Glob", pattern="../../**"))
    denied = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert len(denied) == 7 and not any(e["allowed"] for e in denied)


def test_stop_gate_sends_back_once_then_lets_verify_decide(tmp_path: Path) -> None:
    g = make_guard(tmp_path)
    first = handle("stop", g, {"stop_hook_active": False})
    assert first["decision"] == "block" and "scenario: S1" in first["reason"]
    assert handle("stop", g, {"stop_hook_active": True}) == {}  # no loop: the second stop goes through
    (g.ws.root / "demo_app" / "tests" / "test_app.py").write_text("# scenario: S1\ndef test_x() -> None: ...\n")
    assert handle("stop", g, {"stop_hook_active": False}) == {}


def test_stop_gate_runs_lint_with_a_scrubbed_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-secret")  # what the hook process inherits
    probe = [sys.executable, "-c", "import os,sys; sys.exit(3 if 'CLAUDE_CODE_OAUTH_TOKEN' in os.environ else 1)"]
    g = make_guard(tmp_path, tagged_scenarios=[], stop_commands=[probe])
    problems = g.stop_problems()
    assert len(problems) == 1 and "exit 1" in problems[0]  # ran, and did not see the token


def test_hook_entry_point_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert main(["hooks", "pre-tool-use", str(tmp_path / "missing.json")]) == 2


def test_hook_runs_as_claude_code_would_call_it(tmp_path: Path) -> None:
    g = make_guard(tmp_path)
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(g.ws.root / "agents" / "x.md")}})
    proc = subprocess.run(
        [sys.executable, "-m", "software_factory.hooks", "pre-tool-use", str(g.path)],
        input=payload, capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
