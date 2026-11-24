from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from software_factory.backends.base import BackendError, parse_json
from software_factory.backends.claude_code import build_command, claude_tools, credential_env
from software_factory.backends.litellm import LiteLLMBackend
from software_factory.config import load_role
from software_factory.models import Plan, Review
from software_factory.sandbox import Workspace, leaked_secret_names, scrubbed_env

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
    plan = '{"summary": "s", "files_to_change": ["demo_app/app.py"], "steps": ["a"], "acceptance_criteria": ["b"]}'
    client = FakeClient([text_msg("oops"), text_msg(plan)])
    result, usage = LiteLLMBackend(client=client).structured(load_role(ROOT, "planner"), "ticket", Plan, "m")
    assert result.files_to_change == ["demo_app/app.py"]
    assert usage.input_tokens == 20 and "Invalid" in client.sent[1]["messages"][-1]["content"]


def test_litellm_tool_loop_obeys_workspace_rules(tmp_path: Path) -> None:
    (tmp_path / "demo_app").mkdir()
    ws = Workspace(tmp_path, protected=["agents/*"], writable=["demo_app/app.py"])
    client = FakeClient([
        tool_msg("write_file", '{"path": "agents/coder.md", "content": "pwned"}'),
        tool_msg("write_file", '{"path": "demo_app/app.py", "content": "x = 1\\n"}'),
        text_msg("done"),
    ])  # fmt: skip
    summary, _ = LiteLLMBackend(client=client).agentic(load_role(ROOT, "coder"), "task", ws, "m", tmp_path)
    assert summary == "done"
    assert not (tmp_path / "agents" / "coder.md").exists()
    assert (tmp_path / "demo_app" / "app.py").read_text() == "x = 1\n"
    assert "protected" in client.sent[1]["messages"][-1]["content"]


def test_litellm_turn_budget_is_enforced(tmp_path: Path) -> None:
    role = load_role(ROOT, "coder").model_copy(update={"max_turns": 2})
    client = FakeClient([tool_msg("list_files", "{}"), tool_msg("list_files", "{}")])
    with pytest.raises(BackendError, match="turn budget"):
        LiteLLMBackend(client=client).agentic(role, "task", Workspace(tmp_path, []), "m", tmp_path)


def test_claude_code_command_is_locked_down() -> None:
    coder = load_role(ROOT, "coder")
    cmd = build_command(coder, "claude-sonnet", None, claude_tools(coder.tools))
    for flag in ("--bare", "--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in cmd
    tools = cmd[cmd.index("--tools") + 1]
    assert set(tools.split(",")) == {"Read", "Glob", "Grep", "Edit", "Write"}
    assert "Bash" not in " ".join(cmd) and "WebFetch" not in " ".join(cmd)
    planner = build_command(load_role(ROOT, "planner"), "m", Plan.model_json_schema(), [])
    assert planner[planner.index("--tools") + 1] == "" and "--json-schema" in planner


def test_claude_code_gets_only_the_renamed_credential(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-provider-key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("FACTORY_ANTHROPIC_API_KEY", "sk-virtual-key")
    env = scrubbed_env(tmp_path, credential_env())
    assert env["ANTHROPIC_API_KEY"] == "sk-virtual-key"
    assert leaked_secret_names(env, allowed=["ANTHROPIC_API_KEY"]) == []
