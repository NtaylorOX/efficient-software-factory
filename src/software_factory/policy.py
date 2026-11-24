"""Deterministic security policy. No model is consulted here: these are the decisions a human signs off.

Two checks:
- `gate_plan` before any code is written: is the plan allowed, and does a human need to approve it?
- `check_change` after the coder: did the diff stay inside the plan and the rules, with no secrets?
"""

from __future__ import annotations

import fnmatch

from software_factory.config import FactoryConfig
from software_factory.models import Change, GateDecision, Plan, Ticket
from software_factory.state import find_secrets


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def gate_plan(cfg: FactoryConfig, ticket: Ticket, plan: Plan, approved: bool) -> GateDecision:
    blocked = [f"protected path: {f}" for f in plan.files_to_change if _matches(f, cfg.protected_paths)]
    blocked += [
        f"outside workspace scope: {f}"
        for f in plan.files_to_change
        if not _matches(f, cfg.workspace_scope) and not _matches(f, cfg.protected_paths)
    ]
    if blocked:
        return GateDecision(needs_human=True, blocked=True, reasons=blocked)

    reasons = [f"gated path: {f}" for f in plan.files_to_change if _matches(f, cfg.gate_paths)]
    if ticket.author not in cfg.trusted_authors:
        reasons.append(f"ticket author {ticket.author!r} is not trusted")
    text = " ".join([ticket.title, ticket.body, plan.summary, plan.risk_notes, *plan.steps]).lower()
    reasons += [f"keyword: {k}" for k in cfg.gate_keywords if k in text]
    if not plan.files_to_change:
        reasons.append("plan names no files")
    return GateDecision(needs_human=bool(reasons) and not approved, reasons=reasons)


def check_change(cfg: FactoryConfig, allowed_files: list[str], change: Change) -> list[str]:
    problems = [f"file not in plan: {f}" for f in change.files_changed if f not in allowed_files]
    problems += [f"protected path changed: {f}" for f in change.files_changed if _matches(f, cfg.protected_paths)]
    added = [line[1:] for line in change.diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    problems += [f"possible secret added (pattern {p})" for p in find_secrets("\n".join(added))]
    n = sum(1 for line in change.diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---"))
    if n > cfg.max_diff_lines:
        problems.append(f"diff too large: {n} lines > {cfg.max_diff_lines}")
    if not change.files_changed:
        problems.append("no files changed")
    return problems
