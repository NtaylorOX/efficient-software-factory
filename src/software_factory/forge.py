"""Where tickets come from and where pull requests go.

`LocalForge` keeps everything in `.factory/` so the whole loop runs with no accounts. `GitHubForge` talks to
the REST API. Only the orchestrator and publisher hold a forge; agents never see one or its token.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
import yaml

from software_factory.config import FactoryConfig, split_frontmatter
from software_factory.git import git
from software_factory.models import Ticket


@dataclass(frozen=True)
class ReviewComment:
    author: str
    body: str


class Forge(Protocol):
    def open_tickets(self) -> list[Ticket]: ...
    def is_approved(self, ticket: Ticket) -> bool: ...
    def comment(self, ticket: Ticket, body: str) -> None: ...
    def open_pr(self, ticket: Ticket, branch: str, title: str, body: str, draft: bool, repo: Path) -> str: ...
    def review_comments(self, pr: str) -> list[ReviewComment]: ...
    def pr_text(self, pr: str) -> str: ...


class LocalForge:
    """Tickets are `.factory/tickets/<id>.md` with YAML frontmatter (title, author, labels, approved)."""

    def __init__(self, root: Path, cfg: FactoryConfig) -> None:
        self.tickets_dir, self.prs_dir, self.cfg = root / "tickets", root / "prs", cfg
        self.tickets_dir.mkdir(parents=True, exist_ok=True)
        self.prs_dir.mkdir(parents=True, exist_ok=True)

    def add_ticket(self, ticket_id: str, title: str, body: str, author: str, approved: bool = False) -> Ticket:
        meta = {"title": title, "author": author, "labels": [self.cfg.trigger_label], "approved": approved}
        (self.tickets_dir / f"{ticket_id}.md").write_text(f"---\n{yaml.safe_dump(meta)}---\n{body}\n")
        return self._load(self.tickets_dir / f"{ticket_id}.md")

    def _load(self, path: Path) -> Ticket:
        meta, body = split_frontmatter(path.read_text())
        return Ticket(
            id=path.stem, title=str(meta["title"]), body=body, author=str(meta.get("author", "unknown")),
            labels=[str(x) for x in meta.get("labels", []) or []],  # type: ignore[attr-defined]
            url=str(path),
        )  # fmt: skip

    def open_tickets(self) -> list[Ticket]:
        tickets = [self._load(p) for p in sorted(self.tickets_dir.glob("*.md"))]
        return [t for t in tickets if self.cfg.trigger_label in t.labels]

    def is_approved(self, ticket: Ticket) -> bool:
        meta, _ = split_frontmatter((self.tickets_dir / f"{ticket.id}.md").read_text())
        return bool(meta.get("approved"))

    def approve(self, ticket_id: str) -> None:
        path = self.tickets_dir / f"{ticket_id}.md"
        meta, body = split_frontmatter(path.read_text())
        meta["approved"] = True
        path.write_text(f"---\n{yaml.safe_dump(meta)}---\n{body}\n")

    def comment(self, ticket: Ticket, body: str) -> None:
        with (self.tickets_dir / f"{ticket.id}.comments.md").open("a") as f:
            f.write(f"\n---\n{body}\n")

    def open_pr(self, ticket: Ticket, branch: str, title: str, body: str, draft: bool, repo: Path) -> str:
        path = self.prs_dir / f"{ticket.id}.md"
        path.write_text(f"# {'[draft] ' if draft else ''}{title}\n\nbranch: `{branch}`\n\n{body}\n")
        return str(path)

    def pr_text(self, pr: str) -> str:
        return Path(pr).read_text()

    def review_comments(self, pr: str) -> list[ReviewComment]:
        path = Path(pr).with_suffix(".review.json")
        if not path.is_file():
            return []
        return [ReviewComment(author=c["author"], body=c["body"]) for c in json.loads(path.read_text())]


class GitHubForge:
    """GitHub REST. The token comes from FACTORY_GITHUB_TOKEN (falls back to GITHUB_TOKEN in Actions)."""

    def __init__(self, cfg: FactoryConfig, token: str | None = None) -> None:
        self.cfg = cfg
        self._token = token or os.environ.get("FACTORY_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        self.http = httpx.Client(
            base_url=f"https://api.github.com/repos/{cfg.repo}",
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json"},
            timeout=30,
        )

    def _ticket(self, issue: dict[str, object]) -> Ticket:
        user = issue.get("user") or {}
        labels = issue.get("labels") or []
        return Ticket(
            id=f"gh{issue['number']}", title=str(issue["title"]), body=str(issue.get("body") or ""),
            author=str(user.get("login", "unknown")) if isinstance(user, dict) else "unknown",
            labels=[str(lb["name"]) for lb in labels if isinstance(lb, dict)] if isinstance(labels, list) else [],
            url=str(issue.get("html_url", "")),
        )  # fmt: skip

    def open_tickets(self) -> list[Ticket]:
        resp = self.http.get("/issues", params={"labels": self.cfg.trigger_label, "state": "open"})
        resp.raise_for_status()
        return [self._ticket(i) for i in resp.json() if "pull_request" not in i]

    def _number(self, ticket: Ticket) -> str:
        return ticket.id.removeprefix("gh")

    def is_approved(self, ticket: Ticket) -> bool:
        resp = self.http.get(f"/issues/{self._number(ticket)}/labels")
        resp.raise_for_status()
        return self.cfg.approval_label in {lb["name"] for lb in resp.json()}

    def comment(self, ticket: Ticket, body: str) -> None:
        self.http.post(f"/issues/{self._number(ticket)}/comments", json={"body": body}).raise_for_status()

    def open_pr(self, ticket: Ticket, branch: str, title: str, body: str, draft: bool, repo: Path) -> str:
        # The token goes to git through env-based config, so it never appears in argv or .git/config.
        basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        env = {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        }
        git(repo, "push", f"https://github.com/{self.cfg.repo}.git", f"{branch}:{branch}", env=env)
        resp = self.http.post(
            "/pulls",
            json={"title": title, "head": branch, "base": self.cfg.base_branch, "body": body, "draft": draft},
        )
        resp.raise_for_status()
        return str(resp.json()["html_url"])

    def pr_text(self, pr: str) -> str:
        resp = self.http.get(f"/pulls/{pr.rstrip('/').rsplit('/', 1)[-1]}")
        resp.raise_for_status()
        return f"# {resp.json()['title']}\n\n{resp.json().get('body') or ''}"

    def merge_pr(self, pr: str) -> None:
        self.http.put(
            f"/pulls/{pr.rstrip('/').rsplit('/', 1)[-1]}/merge", json={"merge_method": "squash"}
        ).raise_for_status()

    def review_comments(self, pr: str) -> list[ReviewComment]:
        number = pr.rstrip("/").rsplit("/", 1)[-1]
        out: list[ReviewComment] = []
        for path in (f"/pulls/{number}/comments", f"/pulls/{number}/reviews", f"/issues/{number}/comments"):
            resp = self.http.get(path)
            resp.raise_for_status()
            out += [ReviewComment(author=c["user"]["login"], body=c.get("body") or "") for c in resp.json()]
        return [c for c in out if c.body.strip()]
