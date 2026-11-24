"""Specs in the repo: living specs, change folders and the archive step. Plain Markdown, rendered by code.

    specs/<capability>/spec.md        the living spec: what the system does now (source of truth)
    changes/<date>-<ticket>/          one folder per factory change, added in the same PR as the code
      proposal.md                     why, scope, recorded assumptions
      spec.md                         the delta: ADDED / MODIFIED / REMOVED requirements with scenarios
      tasks.md                        the plan's tasks and how each scenario is checked

The format follows OpenSpec: `### Requirement: <title>` with a SHALL sentence, then `#### Scenario:` blocks
with WHEN and THEN. Agents return a typed `Spec`; this module turns it into Markdown and merges it into the
living spec. Merging the PR is what makes the new requirements true: git is the state machine.
"""

from __future__ import annotations

import re

from software_factory.models import DeltaOp, Plan, Requirement, Scenario, Spec, Verification

REQ_HEADING = "### Requirement: "
_REQ_SPLIT = re.compile(r"^### Requirement: ", re.MULTILINE)


def living_path(capability: str) -> str:
    return f"specs/{capability}/spec.md"


def scenario_md(s: Scenario, with_id: bool) -> str:
    name = f"{s.id} {s.name}" if with_id else s.name
    return f"#### Scenario: {name}\n- **WHEN** {s.when}\n- **THEN** {s.then}\n"


def requirement_md(r: Requirement, with_ids: bool = False) -> str:
    scenarios = "\n".join(scenario_md(s, with_ids) for s in r.scenarios)
    return f"{REQ_HEADING}{r.title}\n{r.text}\n\n{scenarios}".rstrip() + "\n"


def parse_living(text: str) -> tuple[str, dict[str, str]]:
    """Split a living spec into its preamble and an ordered {title: block} of requirements."""
    parts = _REQ_SPLIT.split(text)
    blocks: dict[str, str] = {}
    for part in parts[1:]:
        title, _, _ = part.partition("\n")
        blocks[title.strip()] = REQ_HEADING + part.rstrip() + "\n"
    return parts[0], blocks


def apply_delta(capability: str, living: str, spec: Spec) -> str:
    """Archive step: merge one capability's ADDED, MODIFIED and REMOVED requirements into its living spec."""
    preamble, blocks = parse_living(living) if living else (f"# {capability}\n\n## Requirements\n\n", {})
    for r in (r for r in spec.requirements if r.capability == capability):
        if r.op == DeltaOp.REMOVED:
            blocks.pop(r.title, None)
        else:
            blocks[r.title] = requirement_md(r)
    return preamble.rstrip() + "\n\n" + "\n".join(blocks.values())


def delta_md(spec: Spec) -> str:
    out = []
    for op in DeltaOp:
        reqs = [r for r in spec.requirements if r.op == op]
        if reqs:
            out.append(f"## {op.value} Requirements\n")
            out += [f"<!-- capability: {r.capability} -->\n{requirement_md(r, with_ids=True)}" for r in reqs]
    return "\n".join(out)


def proposal_md(ticket_title: str, ticket_ref: str, spec: Spec) -> str:
    caps = sorted({r.capability for r in spec.requirements})
    lines = [f"# {ticket_title}", "", f"Ticket: {ticket_ref}", "", "## Why", spec.why, "",
             "## What changes", *[f"- {r.op.value.lower()}: {r.title} ({r.capability})" for r in spec.requirements],
             "", "## Capabilities", *[f"- `{living_path(c)}`" for c in caps]]  # fmt: skip
    if spec.out_of_scope:
        lines += ["", "## Out of scope", *[f"- {x}" for x in spec.out_of_scope]]
    if spec.open_questions:
        lines += ["", "## Assumptions", *[f"- {q.question} Assumed: {q.assumption}" for q in spec.open_questions]]
    return "\n".join(lines) + "\n"


def tasks_md(plan: Plan, spec: Spec, verification: Verification | None, qa: dict[str, bool]) -> str:
    done = verification is not None and verification.passed
    names = {s.id: s.name for s in spec.scenarios()}
    lines = ["# Tasks", "", *[f"- [{'x' if done else ' '}] {t}" for t in plan.tasks], "", "## Scenario checks"]
    for c in plan.checks:
        ok = done if c.by == "test" else qa.get(c.scenario, False)
        lines.append(f"- [{'x' if ok else ' '}] {c.scenario} {names.get(c.scenario, '?')}: {c.by}")
    return "\n".join(lines) + "\n"


def change_files(
    folder: str, ticket_title: str, ticket_ref: str, spec: Spec, plan: Plan, v: Verification, qa: dict[str, bool]
) -> dict[str, str]:
    return {
        f"{folder}/proposal.md": proposal_md(ticket_title, ticket_ref, spec),
        f"{folder}/spec.md": delta_md(spec),
        f"{folder}/tasks.md": tasks_md(plan, spec, v, qa),
    }
