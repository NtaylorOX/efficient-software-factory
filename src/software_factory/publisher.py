"""The publisher: the only code that writes to the forge. No model runs here.

The agent side of the factory ends by writing a Bundle: a diff plus the PR text. The publisher applies that
diff to a fresh checkout of the base branch, re-checks it against policy, commits, pushes and opens the PR.
It never uses the agent's worktree, so anything the agent left outside the diff cannot reach the PR.

In GitHub Actions the two sides are separate jobs: the agent job has a model key and a read-only token, the
publish job has a write token and no model key. Comments the agent side wants to post wait in an outbox.
"""

from __future__ import annotations

import fnmatch
import json
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from software_factory import git
from software_factory.config import FactoryConfig, SafeOutputs
from software_factory.forge import Forge, ReviewComment
from software_factory.learning import validate
from software_factory.models import Change, Lesson, Ticket
from software_factory.policy import check_change

LESSON_FILES = "agents/lessons/*"


class Bundle(BaseModel):
    kind: Literal["change", "lessons"]
    run_id: str
    ticket: Ticket
    branch: str
    title: str
    body: str
    draft: bool
    allowed_files: list[str]
    spec_files: list[str] = []
    diff: str


class PublishError(Exception):
    pass


def check_bundle(cfg: FactoryConfig, bundle: Bundle, files: list[str], diff: str) -> list[str]:
    if bundle.kind == "change":
        change = Change(agent_summary="", files_changed=files, diff=diff)
        return check_change(cfg, bundle.allowed_files, change, bundle.spec_files)
    # Lessons live under agents/, which agents can never write. Only lesson files may change, and every added
    # line is re-validated here, so a tampered bundle cannot smuggle in other edits.
    problems = [f"not a lesson file: {f}" for f in files if not fnmatch.fnmatch(f, LESSON_FILES)]
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+- ") and current.endswith(".md"):
            role = Path(current).stem
            if why := validate(Lesson(role=role, text=line[3:][:300], evidence="bundle"), []):
                problems.append(f"lesson rejected ({why}): {line[3:80]}")
    return problems


def publish_bundle(root: Path, cfg: FactoryConfig, forge: Forge, bundle: Bundle) -> str:
    with tempfile.TemporaryDirectory(prefix="factory-publish-") as tmp:
        wt, patch = Path(tmp) / "wt", Path(tmp) / "change.patch"
        git.add_worktree(root, wt, bundle.branch, cfg.base_branch)
        try:
            patch.write_text(bundle.diff)
            try:
                git.git(wt, "apply", "--index", "--whitespace=nowarn", str(patch))
            except git.GitError as exc:
                raise PublishError(f"bundle diff does not apply to {cfg.base_branch}: {exc}") from exc
            files, diff = git.diff(wt, cfg.base_branch)
            if problems := check_bundle(cfg, bundle, files, diff):
                raise PublishError("; ".join(problems))
            git.commit(wt, f"{bundle.title}\n\nFactory run {bundle.run_id} for ticket {bundle.ticket.id}.")
            return forge.open_pr(bundle.ticket, bundle.branch, bundle.title, bundle.body, bundle.draft, repo=wt)
        finally:
            git.remove_worktree(root, wt)


class Outbox:
    """Wraps a forge for the agent side: reads go through, every write is queued as a "safe output".

    The publisher replays the queue under caps from factory.yaml (`safe_outputs`): a bounded number of
    comments per ticket, labels only with allowed prefixes, and only the latest status per ticket.
    """

    def __init__(self, inner: Forge, path: Path) -> None:
        self.inner, self.path = inner, path

    def open_tickets(self) -> list[Ticket]:
        return self.inner.open_tickets()

    def is_approved(self, ticket: Ticket) -> bool:
        return self.inner.is_approved(ticket)

    def review_comments(self, pr: str) -> list[ReviewComment]:
        return self.inner.review_comments(pr)

    def pr_text(self, pr: str) -> str:
        return self.inner.pr_text(pr)

    def _queue(self, kind: str, ticket: Ticket, payload: object) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps({"kind": kind, "ticket": ticket.model_dump(), "payload": payload}) + "\n")

    def comment(self, ticket: Ticket, body: str) -> None:
        self._queue("comment", ticket, body)

    def add_labels(self, ticket: Ticket, labels: list[str]) -> None:
        self._queue("labels", ticket, labels)

    def upsert_status(self, ticket: Ticket, body: str) -> None:
        self._queue("status", ticket, body)

    def open_pr(self, ticket: Ticket, branch: str, title: str, body: str, draft: bool, repo: Path) -> str:
        raise PublishError("the agent side cannot open pull requests; write a bundle instead")


def flush_outbox(forge: Forge, path: Path, caps: SafeOutputs) -> dict[str, int]:
    """Replay queued writes under the caps. Returns counts of what was done and what was dropped."""
    done = {"comment": 0, "labels": 0, "status": 0, "dropped": 0}
    if not path.is_file():
        return done
    items = [json.loads(line) for line in path.read_text().splitlines()]
    latest_status = {item["ticket"]["id"]: i for i, item in enumerate(items) if item["kind"] == "status"}
    per_ticket: dict[str, int] = {}
    for i, item in enumerate(items):
        ticket, kind, payload = Ticket.model_validate(item["ticket"]), item["kind"], item["payload"]
        if kind == "comment":
            per_ticket[ticket.id] = per_ticket.get(ticket.id, 0) + 1
            if per_ticket[ticket.id] > caps.max_comments_per_ticket:
                done["dropped"] += 1
                continue
            forge.comment(ticket, str(payload))
        elif kind == "labels":
            allowed = [lb for lb in payload if any(str(lb).startswith(p) for p in caps.label_prefixes)]
            done["dropped"] += len(payload) - len(allowed)
            if not allowed:
                continue
            forge.add_labels(ticket, [str(lb) for lb in allowed])
        elif kind == "status":
            if latest_status[ticket.id] != i:
                continue  # only the newest status per ticket is worth an API call
            forge.upsert_status(ticket, str(payload))
        else:
            done["dropped"] += 1
            continue
        done[kind] += 1
    path.unlink()
    return done
