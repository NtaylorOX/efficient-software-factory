"""Run state: a SQLite ledger for "where is each ticket", and one folder per run for everything else.

.factory/
  ledger.db                      runs table: id, ticket, stage, attempts, timestamps
  runs/<run_id>/
    ticket.json plan.json gate.json change.json verify.json qa.json review.json   artifacts, one per stage
    trace.jsonl                  one line per event, with token usage; secrets redacted on write
    workspace/                   git worktree on branch factory/<ticket>-<run>; deleted after publish
    home/                        throwaway HOME for agent processes

A new run for the same ticket gets a new folder and a fresh worktree, so no context carries over.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from software_factory.models import Stage, Ticket, Usage

T = TypeVar("T", bound=BaseModel)

# Patterns for credentials that must never land in a trace or a PR body.
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
]


def redact(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def find_secrets(text: str) -> list[str]:
    return [pat.pattern for pat in SECRET_PATTERNS if pat.search(text)]


class Run:
    def __init__(self, store: Store, run_id: str, ticket_id: str) -> None:
        self.store, self.id, self.ticket_id = store, run_id, ticket_id
        self.dir = store.root / "runs" / run_id
        self.workspace = self.dir / "workspace"
        self.home = self.dir / "home"

    @property
    def work_branch(self) -> str:
        """Where the coder works. Never pushed."""
        return f"factory-work/{self.ticket_id}-{self.id}"

    @property
    def branch(self) -> str:
        """What the publisher pushes, rebuilt from the diff on a clean checkout."""
        return f"factory/{self.ticket_id}-{self.id}"

    @property
    def stage(self) -> Stage:
        return self.store.stage(self.id)

    def save(self, name: str, artifact: BaseModel) -> None:
        (self.dir / f"{name}.json").write_text(redact(artifact.model_dump_json(indent=2)))

    def load(self, name: str, schema: type[T]) -> T:
        return schema.model_validate_json((self.dir / f"{name}.json").read_text())

    def has(self, name: str) -> bool:
        return (self.dir / f"{name}.json").is_file()

    def ticket(self) -> Ticket:
        return self.load("ticket", Ticket)

    def event(self, kind: str, usage: Usage | None = None, **data: Any) -> None:
        record = {"t": round(time.time(), 3), "run": self.id, "kind": kind, **data}
        if usage is not None:
            record["usage"] = usage.model_dump()
        with (self.dir / "trace.jsonl").open("a") as f:
            f.write(redact(json.dumps(record, default=str)) + "\n")

    def trace(self) -> list[dict[str, Any]]:
        path = self.dir / "trace.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []

    def tokens(self) -> Usage:
        total = Usage()
        for ev in self.trace():
            if "usage" in ev:
                total = total + Usage.model_validate(ev["usage"])
        return total

    def advance(self, stage: Stage, **data: Any) -> None:
        self.store.set_stage(self.id, stage)
        self.event("stage", stage=stage.value, **data)


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()  # git worktree paths are interpreted relative to the repo, so never relative
        (self.root / "runs").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "ledger.db")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, ticket TEXT, stage TEXT, created REAL, updated REAL)"
        )
        self.db.commit()

    def new_run(self, ticket: Ticket) -> Run:
        run_id = uuid.uuid4().hex[:8]
        now = time.time()
        self.db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?)", (run_id, ticket.id, Stage.RECEIVED, now, now))
        self.db.commit()
        run = Run(self, run_id, ticket.id)
        run.dir.mkdir(parents=True)
        run.save("ticket", ticket)
        run.event("stage", stage=Stage.RECEIVED.value, ticket=ticket.id)
        return run

    def get(self, run_id: str) -> Run:
        row = self.db.execute("SELECT ticket FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"no run {run_id}")
        return Run(self, run_id, row[0])

    def stage(self, run_id: str) -> Stage:
        row = self.db.execute("SELECT stage FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Stage(row[0])

    def set_stage(self, run_id: str, stage: Stage) -> None:
        self.db.execute("UPDATE runs SET stage = ?, updated = ? WHERE id = ?", (stage, time.time(), run_id))
        self.db.commit()

    def runs(self, ticket_id: str | None = None) -> list[tuple[str, str, str]]:
        q, args = "SELECT id, ticket, stage FROM runs", tuple[str, ...]()
        if ticket_id is not None:
            q, args = q + " WHERE ticket = ?", (ticket_id,)
        return [(str(a), str(b), str(c)) for a, b, c in self.db.execute(q + " ORDER BY created", args).fetchall()]

    def active_run(self, ticket_id: str) -> Run | None:
        """The ticket's unfinished run, so a re-trigger resumes it instead of starting a second one."""
        done = {Stage.PUBLISHED, Stage.FAILED, Stage.REJECTED, Stage.READY, Stage.NEEDS_INFO, Stage.DECLINED}
        for run_id, _, stage in reversed(self.runs(ticket_id)):
            if Stage(stage) not in done:
                return self.get(run_id)
        return None
