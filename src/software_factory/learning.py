"""The learning loop: finished runs and human review comments become lessons in the role files.

    trace.jsonl + review comments (trusted reviewers only) -> retro agent -> validate -> agents/lessons/<role>.md

Lessons are the only thing the loop can change, and they change automatically. They are appended to a
role's prompt at load time, so the next run uses them. Anything about tools, permissions, paths or
approval is rejected here, because those live in factory.yaml and role frontmatter, which only a human
PR can change. Review comments from untrusted accounts never reach the retro agent: otherwise anyone who
can comment on a PR could write instructions into the agents.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from software_factory.backends.base import Backend
from software_factory.config import FactoryConfig, load_lessons, load_role
from software_factory.forge import ReviewComment
from software_factory.models import Lesson, Retro, Usage
from software_factory.state import Run, find_secrets

LEARNABLE_ROLES = ("planner", "coder", "qa", "reviewer")
MAX_LESSONS_PER_ROLE = 12  # oldest drop off: lessons cost tokens on every call

# A lesson that matches any of these is about the factory's controls, not about doing the work well.
FORBIDDEN = re.compile(
    r"(?i)\b(ignore|disregard|override)\b.*\b(instruction|rule|polic)|"
    r"\b(permission|allowlist|allow-list|protected|approval|approve label|sandbox|credential|token|secret|"
    r"api[ _-]?key|password|\.github|factory\.yaml|curl|wget|http[s]?://|base64|exfiltrat)"
)


def run_digest(r: Run) -> str:
    """A compact view of one run for the retro agent: outcomes and failure reasons, not full transcripts."""
    lines, usage = [], Usage()
    for ev in r.trace():
        if "usage" in ev:
            usage = usage + Usage.model_validate(ev["usage"])
        if ev["kind"] == "stage":
            extra = {k: v for k, v in ev.items() if k not in ("t", "run", "kind", "stage")}
            lines.append(f"stage {ev['stage']} {json.dumps(extra)[:300] if extra else ''}".strip())
        elif ev["kind"] == "verify" and not ev["passed"]:
            lines.append(f"verify failed: violations={ev['violations']} exit_codes={ev['exit_codes']}")
        elif ev["kind"] == "error":
            lines.append(f"agent error: {ev['error'][:300]}")
    if r.has("verify"):
        failing = [c for c in json.loads((r.dir / "verify.json").read_text())["results"] if c["exit_code"]]
        lines += [f"last failing check output:\n{c['output_tail'][-600:]}" for c in failing]
    lines.append(f"tokens in/out: {usage.input_tokens}/{usage.output_tokens}")
    return "\n".join(lines)


def validate(lesson: Lesson, existing: list[str]) -> str | None:
    """Why a lesson is rejected, or None if it may be applied."""
    if lesson.role not in LEARNABLE_ROLES:
        return f"role {lesson.role!r} cannot learn"
    if FORBIDDEN.search(lesson.text):
        return "lesson touches the factory's controls"
    if find_secrets(lesson.text):
        return "lesson contains something that looks like a secret"
    if lesson.text.strip().lower() in {e.lower() for e in existing}:
        return "duplicate"
    return None


def apply_lessons(root: Path, lessons: list[Lesson], run_id: str) -> tuple[list[Lesson], list[tuple[Lesson, str]]]:
    applied, rejected = [], []
    for lesson in lessons:
        path = root / "agents" / "lessons" / f"{lesson.role}.md"
        existing = load_lessons(path)
        reason = validate(lesson, existing)
        if reason:
            rejected.append((lesson, reason))
            continue
        kept = [*existing, lesson.text.strip()][-MAX_LESSONS_PER_ROLE:]
        path.parent.mkdir(parents=True, exist_ok=True)
        header = f"# Lessons for the {lesson.role} agent\n\nWritten by the learning loop; edit or delete freely.\n\n"
        path.write_text(header + "\n".join(f"- {t}" for t in kept) + "\n")
        applied.append(lesson)
    (root / "agents" / "lessons").mkdir(parents=True, exist_ok=True)
    with (root / "agents" / "lessons" / "log.jsonl").open("a") as f:
        for lesson in applied:
            f.write(json.dumps({"run": run_id, **lesson.model_dump()}) + "\n")
    return applied, rejected


def learn(
    root: Path,
    cfg: FactoryConfig,
    backend: Backend,
    comments: list[ReviewComment],
    run: Run | None = None,
    pr_text: str = "",
) -> tuple[list[Lesson], list[tuple[Lesson, str]]]:
    """Derive lessons from a run's trace when we have it (local), or from the PR text (Actions, where the
    ledger of the earlier job is gone). The PR body carries the plan, checks, QA and review results."""
    trusted = [c for c in comments if c.author in cfg.trusted_reviewers]
    dropped = dict(Counter(c.author for c in comments if c.author not in cfg.trusted_reviewers))
    review = "\n".join(f"- {c.author}: {c.body[:500]}" for c in trusted) or "(no human review comments)"
    if run is not None:
        run.event("learn", trusted_comments=len(trusted), dropped_comments=dropped)
        context = f"Run {run.id}, ticket {run.ticket().title!r}\n\n{run_digest(run)}"
    else:
        context = f"Pull request:\n{pr_text[:4000]}"
    prompt = f"{context}\n\nHuman review comments:\n{review}"
    retro, usage = backend.structured(load_role(root, "retro"), prompt, Retro, cfg.model_for("retro"))
    applied, rejected = apply_lessons(root, retro.lessons[:3], run.id if run else "pr")
    if run is not None:
        run.event(
            "agent", role="retro", usage=usage, applied=[x.text for x in applied],
            rejected=[(x.text, why) for x, why in rejected],
        )  # fmt: skip
    return applied, rejected
