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
from software_factory.config import FactoryConfig
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
    diff: str


class PublishError(Exception):
    pass


def check_bundle(cfg: FactoryConfig, bundle: Bundle, files: list[str], diff: str) -> list[str]:
    if bundle.kind == "change":
        return check_change(cfg, bundle.allowed_files, Change(agent_summary="", files_changed=files, diff=diff))
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
    """Wraps a forge for the agent side: reads go through, comments are queued for the publisher."""

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

    def comment(self, ticket: Ticket, body: str) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps({"ticket": ticket.model_dump(), "body": body}) + "\n")

    def open_pr(self, ticket: Ticket, branch: str, title: str, body: str, draft: bool, repo: Path) -> str:
        raise PublishError("the agent side cannot open pull requests; write a bundle instead")


def flush_outbox(forge: Forge, path: Path) -> int:
    if not path.is_file():
        return 0
    lines = path.read_text().splitlines()
    for line in lines:
        item = json.loads(line)
        forge.comment(Ticket.model_validate(item["ticket"]), str(item["body"]))
    path.unlink()
    return len(lines)
