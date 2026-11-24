import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from software_factory.backends.base import BackendError, parse_json
from software_factory.backends.claude_code import NO_HOOKS, build_command, claude_tools, credential_env
from software_factory.backends.litellm import LiteLLMBackend
from software_factory.config import load_role
from software_factory.hooks import Guard, GuardPolicy, claude_settings
from software_factory.models import Plan, Review
from software_factory.sandbox import leaked_secret_names, scrubbed_env

ROOT = Path(__file__).parents[1]


def test_parse_json_tolerates_fences_and_prose() -> None:
    text = 'Sure!\n```json\n{"approve": true, "comments": []}\n```'
    assert parse_json(text, Review).approve is True
    with pytest.raises(BackendError):
        parse_json("no json here", Review)
    with pytest.raises(BackendError, match="does not match"):
        parse_json('{"approve": "maybe-not-a-bool-and-not-coercible"}', Review)


class FakeClient:
    """Stands in for openai.OpenAI: returns queued messages and records what was sent."""

    def __init__(self, replies: list[Any]) -> None:
        self.replies, self.sent = replies, list[dict[str, Any]]()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.sent.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot: the list is mutated later
        msg = self.replies.pop(0)
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def text_msg(content: str) -> Any:
    return SimpleNamespace(
        content=content, tool_calls=None, model_dump=lambda **_: {"role": "assistant", "content": content}
    )


def tool_msg(name: str, args: str) -> Any:
    call = SimpleNamespace(id="c1", function=SimpleNamespace(name=name, arguments=args))
    dump = {
        "role": "assistant",
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": args}}],
    }
    return SimpleNamespace(content=None, tool_calls=[call], model_dump=lambda **_: dump)


def test_litellm_structured_repairs_once_then_succeeds() -> None:
    plan = '{"summary": "s", "files_to_change": ["demo_app/app.py"], "tasks": ["a"], "checks": []}'
    client = FakeClient([text_msg("oops"), text_msg(plan)])
    result, usage = LiteLLMBackend(client=client).structured(load_role(ROOT, "planner"), "ticket", Plan, "m")
    assert result.files_to_change == ["demo_app/app.py"]
    assert usage.input_tokens == 20 and "Invalid" in client.sent[1]["messages"][-1]["content"]


def guard(tmp_path: Path, **over: Any) -> Guard:
    (tmp_path / "ws").mkdir(exist_ok=True)
    policy = GuardPolicy(
        run_id="r1", workspace=str(tmp_path / "ws"), home=str(tmp_path / "home"), trace=str(tmp_path / "trace.jsonl"),
        tools=["read_file", "list_files", "search", "write_file"], protected=["agents/*"],
        writable=["demo_app/app.py"], test_globs=["demo_app/tests/*"], tagged_scenarios=[], stop_commands=[],
    )  # fmt: skip
    return Guard(policy.model_copy(update=over), tmp_path / "guard.json")


def test_litellm_tool_loop_obeys_the_guard(tmp_path: Path) -> None:
    g = guard(tmp_path)
    (g.ws.root / "demo_app").mkdir()
    client = FakeClient([
        tool_msg("write_file", '{"path": "agents/coder.md", "content": "pwned"}'),
        tool_msg("write_file", '{"path": "demo_app/app.py", "content": "x = 1\\n"}'),
        text_msg("done"),
    ])  # fmt: skip
    summary, _ = LiteLLMBackend(client=client).agentic(load_role(ROOT, "coder"), "task", g, "m", tmp_path)
    assert summary == "done"
    assert not (g.ws.root / "agents" / "coder.md").exists()
    assert (g.ws.root / "demo_app" / "app.py").read_text() == "x = 1\n"
    assert "denied" in client.sent[1]["messages"][-1]["content"]
    trace = (tmp_path / "trace.jsonl").read_text()
    assert '"allowed": false' in trace and '"allowed": true' in trace  # both calls traced, as the hooks would


def test_litellm_stop_gate_sends_the_agent_back_once(tmp_path: Path) -> None:
    g = guard(tmp_path, tagged_scenarios=["S1"], writable=["demo_app/tests/test_app.py"])
    client = FakeClient([text_msg("done"), text_msg("still done")])
    summary, _ = LiteLLMBackend(client=client).agentic(load_role(ROOT, "coder"), "task", g, "m", tmp_path)
    assert summary == "still done"  # sent back once, then allowed to stop; verify will catch it
    assert "scenario: S1" in client.sent[1]["messages"][-1]["content"]


def test_litellm_turn_budget_is_enforced(tmp_path: Path) -> None:
    role = load_role(ROOT, "coder").model_copy(update={"max_turns": 2})
    client = FakeClient([tool_msg("list_files", "{}"), tool_msg("list_files", "{}")])
    with pytest.raises(BackendError, match="turn budget"):
        LiteLLMBackend(client=client).agentic(role, "task", guard(tmp_path), "m", tmp_path)


def test_claude_code_command_is_locked_down(tmp_path: Path) -> None:
    coder = load_role(ROOT, "coder")
    cmd = build_command(coder, "claude-sonnet", None, claude_tools(coder.tools), claude_settings(tmp_path / "g.json"))
    flags = ("--restricted", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence")
    assert all(f in cmd for f in flags)
    assert "--bare" not in cmd and "--safe-mode" not in cmd  # both would switch our hooks off
    hooks = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]
    assert set(hooks) == {"PreToolUse", "PostToolUse", "Stop"}
    assert "software_factory.hooks pre-tool-use" in hooks["PreToolUse"][0]["hooks"][0]["command"]
    tools = cmd[cmd.index("--tools") + 1]
    assert set(tools.split(",")) == {"Read", "Glob", "Grep", "Edit", "Write"}
    assert "Bash" not in cmd[cmd.index("--tools") + 1]
    planner = build_command(load_role(ROOT, "planner"), "m", Plan.model_json_schema(), [], NO_HOOKS)
    assert planner[planner.index("--tools") + 1] == "" and "--json-schema" in planner


def test_subscription_token_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FACTORY_ANTHROPIC_API_KEY", "sk-virtual-key")
    monkeypatch.setenv("FACTORY_CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-subscription")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    creds = credential_env()
    assert creds == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-subscription"}  # never both
    env = scrubbed_env(tmp_path, creds)
    assert leaked_secret_names(env, allowed=["CLAUDE_CODE_OAUTH_TOKEN"]) == []


def test_claude_code_gets_only_the_renamed_credential(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-provider-key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("FACTORY_ANTHROPIC_API_KEY", "sk-virtual-key")
    env = scrubbed_env(tmp_path, credential_env())
    assert env["ANTHROPIC_API_KEY"] == "sk-virtual-key"
    assert leaked_secret_names(env, allowed=["ANTHROPIC_API_KEY"]) == []
