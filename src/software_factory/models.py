"""Typed artifacts passed between stages.

Agents never see each other's conversations. Each stage reads the artifacts it needs from the run
folder and writes one artifact back, so a handoff is a small JSON file you can open and read.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Stage(StrEnum):
    RECEIVED = "received"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    CODED = "coded"
    VERIFIED = "verified"
    QA_DONE = "qa_done"
    REVIEWED = "reviewed"
    READY = "ready"  # bundle written; a separate publisher job will open the PR
    PUBLISHED = "published"
    FAILED = "failed"
    REJECTED = "rejected"


class Ticket(BaseModel):
    """A unit of work. `body` is untrusted: anyone who can open an issue wrote it."""

    id: str
    title: str
    body: str
    author: str = "unknown"
    url: str = ""
    labels: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    summary: str
    files_to_change: list[str] = Field(description="Repo-relative paths the coder may edit.")
    steps: list[str]
    acceptance_criteria: list[str] = Field(description="Checks a QA agent can confirm in the UI or tests.")
    risk_notes: str = ""


class GateDecision(BaseModel):
    needs_human: bool
    blocked: bool = False
    reasons: list[str] = Field(default_factory=list)


class Change(BaseModel):
    """What the coder did. `diff` is computed by the orchestrator from git, never reported by the agent."""

    agent_summary: str
    files_changed: list[str]
    diff: str


class CommandResult(BaseModel):
    command: list[str]
    exit_code: int
    output_tail: str


class Verification(BaseModel):
    passed: bool
    results: list[CommandResult]
    policy_violations: list[str] = Field(default_factory=list)


class QAStep(BaseModel):
    action: str = Field(description="One of: goto, fill, click, press, expect_text, done")
    target: str = ""
    value: str = ""


class QAReport(BaseModel):
    passed: bool
    criteria: dict[str, bool]
    steps_taken: list[QAStep]
    notes: str = ""
    screenshot: str = ""


class Review(BaseModel):
    approve: bool
    comments: list[str] = Field(default_factory=list)
    security_concerns: list[str] = Field(default_factory=list)


class Lesson(BaseModel):
    role: str
    text: str = Field(max_length=300)
    evidence: str = Field(description="Run id, review comment or trace event that supports the lesson.")


class Retro(BaseModel):
    lessons: list[Lesson]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )
