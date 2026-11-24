"""Deterministic policy. No model is consulted here: these are the decisions a human signs off.

Routing:
- `route` turns the triage agent's classification into a lane. The model labels; code decides.

Spec checks (spec-driven development: spec, then plan, then code):
- `check_spec` before planning: well-formed requirements and scenarios, consistent with the living specs, and
  no open question left without an assumption.
- `check_plan` before the gate: every scenario has a way to be shown true (a test, or browser QA).
- `check_traceability` after the coder: every scenario checked "by test" is named by an added test.

Two gates:
- `gate_plan` before any code is written: is the plan allowed, and does a human need to approve it?
- `check_change` after the coder: did the diff stay inside the plan and the rules, with no secrets?
"""

from __future__ import annotations

import fnmatch
import re

from software_factory.config import FactoryConfig
from software_factory.models import Change, DeltaOp, GateDecision, Plan, Route, Spec, Ticket, TicketKind, Triage
from software_factory.specs import parse_living
from software_factory.state import find_secrets


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


SIZES = "SML"


def route(cfg: FactoryConfig, triage: Triage) -> Route:
    if triage.kind in (TicketKind.QUESTION, TicketKind.OUT_OF_SCOPE):
        return Route(lane=None, reasons=[f"{triage.kind.value}: no code change needed"])
    if triage.missing_info:
        return Route(lane=None, reasons=["needs info: " + "; ".join(triage.missing_info)])
    small = SIZES.index(triage.size) <= SIZES.index(cfg.max_fast_size)
    if triage.kind.value in cfg.fast_kinds and small:
        return Route(lane="fast", reasons=[f"{triage.kind.value}, size {triage.size}"])
    why = f"size {triage.size} is above {cfg.max_fast_size}" if not small else f"{triage.kind.value} work"
    return Route(lane="complex", reasons=[why])


CAPABILITY = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
SCENARIO_ID = re.compile(r"^S\d{1,3}$")
SCENARIO_TAG = re.compile(r"#\s*scenario:\s*(S\d{1,3}(?:\s*,\s*S\d{1,3})*)")


def check_spec(spec: Spec, living: dict[str, str]) -> tuple[list[str], list[str]]:
    """(problems, blocking questions). `living` maps capability -> current spec text from the base branch."""
    problems = [] if spec.requirements else ["the spec has no requirements"]
    ids = [s.id for s in spec.scenarios()]
    problems += [f"duplicate scenario id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    problems += [f"scenario id {i!r} must look like S1" for i in ids if not SCENARIO_ID.match(i)]
    for r in spec.requirements:
        where = f"{r.op.value} {r.title!r}"
        if not CAPABILITY.match(r.capability):
            problems.append(f"{where}: capability {r.capability!r} must be a lowercase slug")
            continue
        existing = parse_living(living.get(r.capability, ""))[1]
        if r.op == DeltaOp.ADDED and r.title in existing:
            problems.append(f"{where}: already in specs/{r.capability}/spec.md, use MODIFIED")
        if r.op != DeltaOp.ADDED and r.title not in existing:
            problems.append(f"{where}: no such requirement in specs/{r.capability}/spec.md")
        if r.op != DeltaOp.REMOVED and "SHALL" not in r.text:
            problems.append(f"{where}: the requirement text must use SHALL")
        if r.op != DeltaOp.REMOVED and not r.scenarios:
            problems.append(f"{where}: needs at least one scenario")
    blocking = [q.question for q in spec.open_questions if not q.assumption.strip()]
    return problems, blocking


def check_plan(spec: Spec, plan: Plan, qa_available: bool) -> list[str]:
    ids = {s.id for s in spec.scenarios()}
    checked = {c.scenario for c in plan.checks}
    problems = [f"scenario {i} has no check in the plan" for i in sorted(ids - checked)]
    problems += [f"plan checks unknown scenario {i}" for i in sorted(checked - ids)]
    if not qa_available:
        problems += [f"scenario {c.scenario}: this lane has no browser QA, check it by test" for c in plan.checks
                     if c.by == "qa"]  # fmt: skip
    return problems


def tagged_scenarios(text: str) -> set[str]:
    return {i.strip() for m in SCENARIO_TAG.finditer(text) for i in m.group(1).split(",")}


def check_traceability(spec: Spec, plan: Plan, change: Change, test_globs: list[str]) -> list[str]:
    """Every scenario the plan checks by test must be named by a `# scenario: Sn` tag added in a test file."""
    added, current = [], ""
    for line in change.diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++") and _matches(current, test_globs):
            added.append(line[1:])
    tagged = tagged_scenarios("\n".join(added))
    return [
        f"scenario {c.scenario} is checked by test, but no added test has a '# scenario: {c.scenario}' tag"
        for c in plan.checks
        if c.by == "test" and c.scenario not in tagged
    ]


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
    text = " ".join([ticket.title, ticket.body, plan.summary, plan.risk_notes, *plan.tasks]).lower()
    reasons += [f"keyword: {k}" for k in cfg.gate_keywords if k in text]
    if not plan.files_to_change:
        reasons.append("plan names no files")
    return GateDecision(needs_human=bool(reasons) and not approved, reasons=reasons)


SPEC_FILES = ["specs/*/spec.md", "changes/*/proposal.md", "changes/*/spec.md", "changes/*/tasks.md"]


def check_change(
    cfg: FactoryConfig, allowed_files: list[str], change: Change, spec_files: list[str] | None = None
) -> list[str]:
    """`spec_files` are the change folder and living specs the factory rendered itself at publish time. They are
    protected from agents, so they are let through here only by name and only in the spec layout."""
    spec_files = spec_files or []
    problems = [f"not a spec file: {f}" for f in spec_files if not _matches(f, SPEC_FILES)]
    allowed = set(allowed_files) | set(spec_files)
    problems += [f"file not in plan: {f}" for f in change.files_changed if f not in allowed]
    problems += [
        f"protected path changed: {f}"
        for f in change.files_changed
        if _matches(f, cfg.protected_paths) and not (f in spec_files and _matches(f, SPEC_FILES))
    ]
    added = [line[1:] for line in change.diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    problems += [f"possible secret added (pattern {p})" for p in find_secrets("\n".join(added))]
    n, current = 0, ""
    for line in change.diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line[:1] in "+-" and line[:3] not in ("+++", "---") and current not in spec_files:
            n += 1
    if n > cfg.max_diff_lines:
        problems.append(f"diff too large: {n} lines > {cfg.max_diff_lines}")
    if not change.files_changed:
        problems.append("no files changed")
    return problems
