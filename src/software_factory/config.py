"""`factory.yaml` (what the factory may do) and role files in `agents/` (who does it)."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Mode(StrEnum):
    STRUCTURED = "structured"  # one call, JSON out, no tools
    AGENTIC = "agentic"  # tool loop inside the workspace


class FactoryConfig(BaseModel):
    repo: str = Field(description="owner/name on GitHub; empty for local-only runs")
    base_branch: str = "main"
    trigger_label: str = "factory"
    approval_label: str = "factory-approved"
    trusted_authors: list[str] = Field(description="Tickets from anyone else wait for the approval label.")
    trusted_reviewers: list[str] = Field(description="Only their review comments feed the learning loop.")
    workspace_scope: list[str] = Field(description="Globs the coder may write. Everything else is read-only.")
    protected_paths: list[str] = Field(description="Never written by an agent, even with approval.")
    gate_paths: list[str] = Field(default_factory=list, description="Writable, but a human approves first.")
    gate_keywords: list[str] = Field(default_factory=list)
    verify_commands: list[list[str]]
    max_code_attempts: int = 2
    max_diff_lines: int = 400
    backend: str = "mock"
    models: dict[str, str] = Field(default_factory=dict, description="role -> model alias; key 'default'.")
    qa_app: str = "demo_app.app:app"
    auto_merge_lessons: bool = False

    def model_for(self, role: str) -> str:
        return self.models.get(role, self.models.get("default", "claude-sonnet"))


class Role(BaseModel):
    """An agent: a prompt, a mode, a tool allowlist and a budget. Lessons are appended at load time."""

    name: str
    description: str
    mode: Mode
    tools: list[str] = Field(default_factory=list)
    max_turns: int = 1
    max_output_tokens: int = 2000
    prompt: str
    lessons: list[str] = Field(default_factory=list)

    def system_prompt(self) -> str:
        if not self.lessons:
            return self.prompt
        learned = "\n".join(f"- {lesson}" for lesson in self.lessons)
        return f"{self.prompt}\n\n## Lessons from earlier runs\n{learned}"

    def card(self) -> dict[str, object]:
        """An A2A-style agent card: enough to describe the agent to another system, nothing secret."""
        return {
            "name": self.name,
            "description": self.description,
            "capabilities": {"mode": self.mode.value, "tools": self.tools},
            "limits": {"max_turns": self.max_turns, "max_output_tokens": self.max_output_tokens},
        }


def load_config(root: Path) -> FactoryConfig:
    cfg = FactoryConfig.model_validate(yaml.safe_load((root / "factory.yaml").read_text()))
    if repo := os.environ.get("FACTORY_REPO"):  # set by the workflows to ${{ github.repository }}
        cfg.repo = repo
    return cfg


def split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        raise ValueError("role file must start with YAML frontmatter")
    _, front, body = text.split("---\n", 2)
    meta = yaml.safe_load(front)
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    return meta, body.strip()


def load_lessons(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line[2:].strip() for line in path.read_text().splitlines() if line.startswith("- ")]


def load_role(root: Path, name: str) -> Role:
    meta, body = split_frontmatter((root / "agents" / f"{name}.md").read_text())
    lessons = load_lessons(root / "agents" / "lessons" / f"{name}.md")
    return Role.model_validate({**meta, "prompt": body, "lessons": lessons})
