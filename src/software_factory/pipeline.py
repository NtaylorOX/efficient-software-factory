r"""The factory's state machine. Read this file top to bottom to see everything that happens to a ticket.

    received -> planned -> [awaiting_approval ->] approved -> coded -> verified -> qa_done -> reviewed -> published
                        \-> rejected (plan touches protected paths)          \-> failed (attempts used up)

Each step loads the artifacts it needs from the run folder and saves one back, then advances the ledger.
`advance()` can be called again at any time: it resumes from the stored stage, so a crash, a human
approval or a re-triggered workflow all continue the same run instead of starting over.
"""

from __future__ import annotations

import fnmatch
import json
import sys
from pathlib import Path

from software_factory import git, policy
from software_factory.backends.base import Backend, BackendError
from software_factory.config import FactoryConfig, Role, load_role
from software_factory.forge import Forge
from software_factory.models import (
    Change,
    CommandResult,
    GateDecision,
    Plan,
    QAReport,
    Review,
    Stage,
    Ticket,
    Verification,
)
from software_factory.publisher import Bundle, PublishError, publish_bundle
from software_factory.qa import chromium_path, run_qa
from software_factory.sandbox import SandboxError, Workspace, run, scrubbed_env
from software_factory.state import Run, Store

CONTEXT_FILE_CHARS = 6000  # per file shown to the planner; the coder reads what it needs itself


class Factory:
    def __init__(
        self,
        root: Path,
        cfg: FactoryConfig,
        store: Store,
        forge: Forge,
        backend: Backend,
        qa: bool = True,
        defer_publish: bool = False,
    ) -> None:
        self.root, self.cfg, self.store, self.forge, self.backend = root, cfg, store, forge, backend
        self.defer_publish = defer_publish  # True in Actions: the publish job, not this process, opens the PR
        self.qa_enabled = qa and chromium_path() is not None

    def role(self, name: str) -> Role:
        return load_role(self.root, name)  # re-read every time, so new lessons apply to the next call

    # ---- entry points -------------------------------------------------------------------------------

    def handle(self, ticket: Ticket) -> Run:
        """Start or resume the ticket's run and drive it as far as it can go without a human."""
        run_ = self.store.active_run(ticket.id) or self.store.new_run(ticket)
        self.advance(run_)
        return run_

    def advance(self, r: Run) -> None:
        steps = {
            Stage.RECEIVED: self.plan,
            Stage.PLANNED: self.gate,
            Stage.AWAITING_APPROVAL: self.check_approval,
            Stage.APPROVED: self.build,
            Stage.CODED: self.build,  # resuming after a crash mid-build restarts the loop in the same worktree
            Stage.VERIFIED: self.build,
            Stage.QA_DONE: self.review,
            Stage.REVIEWED: self.publish,
        }
        while (step := steps.get(r.stage)) is not None:
            before = r.stage
            try:
                step(r)
            except (BackendError, SandboxError) as exc:  # a refused write from the mock surfaces here
                r.event("error", stage=before.value, error=str(exc))
                self.fail(r, f"agent error during {before.value}: {exc}")
            if r.stage == before:  # waiting on a human
                return

    # ---- stages -------------------------------------------------------------------------------------

    def plan(self, r: Run) -> None:
        ticket, role = r.ticket(), self.role("planner")
        # Context comes from the committed base branch, never from local state such as .factory/ or .env.
        files = git.git(self.root, "ls-tree", "-r", "--name-only", self.cfg.base_branch).split()
        in_scope = [f for f in files if any(fnmatch.fnmatch(f, p) for p in self.cfg.workspace_scope)]
        context = "\n\n".join(
            f"### {f}\n{git.git(self.root, 'show', f'{self.cfg.base_branch}:{f}')[:CONTEXT_FILE_CHARS]}"
            for f in in_scope
            if f.endswith((".py", ".html"))
        )
        prompt = (
            f"Repository files:\n{chr(10).join(files)}\n\nFiles you may change, with contents:\n{context}\n\n"
            f"<ticket author={ticket.author!r}>\n# {ticket.title}\n\n{ticket.body}\n</ticket>"
        )
        plan, usage = self.backend.structured(role, prompt, Plan, self.cfg.model_for("planner"))
        r.save("plan", plan)
        r.event("agent", role="planner", usage=usage, files=plan.files_to_change)
        r.advance(Stage.PLANNED)

    def gate(self, r: Run) -> None:
        decision = policy.gate_plan(self.cfg, r.ticket(), r.load("plan", Plan), self.forge.is_approved(r.ticket()))
        r.save("gate", decision)
        if decision.blocked:
            self.forge.comment(r.ticket(), _gate_comment(decision, self.cfg, blocked=True))
            r.advance(Stage.REJECTED, reasons=decision.reasons)
        elif decision.needs_human:
            self.forge.comment(r.ticket(), _gate_comment(decision, self.cfg, blocked=False))
            r.advance(Stage.AWAITING_APPROVAL, reasons=decision.reasons)
        else:
            r.advance(Stage.APPROVED, reasons=decision.reasons)

    def check_approval(self, r: Run) -> None:
        if self.forge.is_approved(r.ticket()):
            r.advance(Stage.APPROVED, by="human")

    def build(self, r: Run) -> None:
        """Code, verify and QA in a loop, with each failure fed back to a fresh coder call."""
        plan = r.load("plan", Plan)
        if not r.workspace.exists():
            git.add_worktree(self.root, r.workspace, r.work_branch, self.cfg.base_branch)
        ws = Workspace(r.workspace, self.cfg.protected_paths, writable=plan.files_to_change)
        feedback = ""
        for attempt in range(1, self.cfg.max_code_attempts + 1):
            self.code(r, ws, plan, feedback, attempt)
            verification = self.verify(r, plan)
            if not verification.passed:
                feedback = _verify_feedback(verification)
                continue
            r.advance(Stage.VERIFIED, attempt=attempt)
            report = self.qa(r, plan)
            if report.passed:
                r.advance(Stage.QA_DONE, attempt=attempt)
                return
            feedback = "QA failed in the browser:\n" + json.dumps(report.criteria, indent=1) + "\n" + _qa_trail(report)
            r.advance(Stage.APPROVED, retry="qa")
        self.fail(r, f"still failing after {self.cfg.max_code_attempts} attempts:\n\n{feedback[-1500:]}")

    def code(self, r: Run, ws: Workspace, plan: Plan, feedback: str, attempt: int) -> None:
        task = f"Plan:\n{plan.model_dump_json(indent=1)}\n\nYou may write only: {', '.join(plan.files_to_change)}" + (
            f"\n\nYour previous attempt failed. Fix this and nothing else:\n{feedback}" if feedback else ""
        )
        summary, usage = self.backend.agentic(self.role("coder"), task, ws, self.cfg.model_for("coder"), r.home)
        files, diff = git.diff(r.workspace, self.cfg.base_branch)
        r.save("change", Change(agent_summary=summary, files_changed=files, diff=diff))
        r.event("agent", role="coder", attempt=attempt, usage=usage, files=files)
        r.advance(Stage.CODED, attempt=attempt)

    def verify(self, r: Run, plan: Plan) -> Verification:
        """The orchestrator runs the checks itself. The coder's claims are not evidence."""
        change = r.load("change", Change)
        violations = policy.check_change(self.cfg, plan.files_to_change, change)
        results = []
        if not violations:
            env = scrubbed_env(r.home)
            cmds = [[sys.executable if t == "{python}" else t for t in c] for c in self.cfg.verify_commands]
            for shown, cmd in zip(self.cfg.verify_commands, cmds, strict=True):
                code, out = run(cmd, r.workspace, env, allowlist=cmds)
                shown = [t.strip("{}") for t in shown]  # "{python}" reads as "python" in PRs and traces
                results.append(CommandResult(command=shown, exit_code=code, output_tail=out[-1500:]))
        v = Verification(
            passed=not violations and all(x.exit_code == 0 for x in results),
            results=results,
            policy_violations=violations,
        )
        r.save("verify", v)
        r.event("verify", passed=v.passed, violations=violations, exit_codes=[x.exit_code for x in results])
        return v

    def qa(self, r: Run, plan: Plan) -> QAReport:
        if not self.qa_enabled:
            report = QAReport(passed=True, criteria={}, steps_taken=[], notes="skipped: QA disabled or no Chromium")
        else:
            role = self.role("qa")
            report, usage = run_qa(
                self.backend, role, self.cfg.model_for("qa"), r.workspace, self.cfg.qa_app,
                plan.acceptance_criteria, r.dir,
            )  # fmt: skip
            r.event("agent", role="qa", usage=usage, passed=report.passed, steps=len(report.steps_taken))
        r.save("qa", report)
        return report

    def review(self, r: Run) -> None:
        plan, change = r.load("plan", Plan), r.load("change", Change)
        verification, report = r.load("verify", Verification), r.load("qa", QAReport)
        prompt = (
            f"Plan:\n{plan.model_dump_json(indent=1)}\n\nChecks: "
            + ", ".join(f"{' '.join(c.command[-3:])} -> {c.exit_code}" for c in verification.results)
            + f"\nQA: {json.dumps(report.criteria)} {report.notes}\n\n<diff>\n{change.diff}\n</diff>"
        )
        review, usage = self.backend.structured(self.role("reviewer"), prompt, Review, self.cfg.model_for("reviewer"))
        r.save("review", review)
        r.event(
            "agent",
            role="reviewer",
            usage=usage,
            approve=review.approve,
            concerns=len(review.security_concerns),
        )
        r.advance(Stage.REVIEWED)

    def publish(self, r: Run) -> None:
        """Hand the diff to the publisher. The agent worktree is deleted; only the diff travels on."""
        plan, review, ticket = r.load("plan", Plan), r.load("review", Review), r.ticket()
        _, diff = git.diff(r.workspace, self.cfg.base_branch)
        draft = not review.approve or bool(review.security_concerns)
        bundle = Bundle(
            kind="change", run_id=r.id, ticket=ticket, branch=r.branch, title=ticket.title, body=pr_body(r),
            draft=draft, allowed_files=plan.files_to_change, diff=diff,
        )  # fmt: skip
        r.save("bundle", bundle)
        git.remove_worktree(self.root, r.workspace)
        if self.defer_publish:
            r.advance(Stage.READY)
            return
        try:
            url = publish_bundle(self.root, self.cfg, self.forge, bundle)
        except PublishError as exc:
            self.fail(r, f"publish check failed: {exc}")
            return
        r.advance(Stage.PUBLISHED, pr=url, draft=draft)
        self.forge.comment(ticket, f"Opened {'a draft ' if draft else ''}pull request: {url}")

    def fail(self, r: Run, reason: str) -> None:
        r.advance(Stage.FAILED, reason=reason[:500])
        self.forge.comment(r.ticket(), f"The factory stopped on this ticket (run `{r.id}`).\n\n{reason}")


# ---- text the factory writes ------------------------------------------------------------------------


def _gate_comment(decision: GateDecision, cfg: FactoryConfig, blocked: bool) -> str:
    reasons = "\n".join(f"- {x}" for x in decision.reasons)
    if blocked:
        return f"The factory will not work on this ticket. A human needs to make these changes:\n{reasons}"
    return (
        f"This ticket needs a human decision before any code is written:\n{reasons}\n\n"
        f"Add the `{cfg.approval_label}` label to go ahead."
    )


def _verify_feedback(v: Verification) -> str:
    parts = [f"Policy: {p}" for p in v.policy_violations]
    parts += [f"$ {' '.join(c.command)} (exit {c.exit_code})\n{c.output_tail}" for c in v.results if c.exit_code]
    return "\n\n".join(parts)


def _qa_trail(report: QAReport) -> str:
    return "\n".join(f"{s.action} {s.target!r} {s.value!r}" for s in report.steps_taken[-8:])


def pr_body(r: Run) -> str:
    plan, change, review = r.load("plan", Plan), r.load("change", Change), r.load("review", Review)
    verification, report, ticket = r.load("verify", Verification), r.load("qa", QAReport), r.ticket()
    tokens = r.tokens()
    lines = [
        f"Closes {ticket.url or ticket.id}" if ticket.id.startswith("gh") else f"Ticket: `{ticket.id}`",
        "", f"**Plan.** {plan.summary}", "", "**Acceptance criteria (browser QA)**",
        *[f"- [{'x' if report.criteria.get(c) else ' '}] {c}" for c in plan.acceptance_criteria],
        *([f"\n_{report.notes}_"] if report.notes else []),
        "", "**Checks run by the factory**",
        *[f"- `{' '.join(c.command)}` exit {c.exit_code}" for c in verification.results],
        "", f"**Agent review:** {'approve' if review.approve else 'changes requested'}",
        *[f"- {c}" for c in review.comments],
        *[f"- :warning: security: {c}" for c in review.security_concerns],
        "", f"**Coder summary (unverified):** {change.agent_summary}",
        "", f"Run `{r.id}` · tokens in/out {tokens.input_tokens}/{tokens.output_tokens} · "
        "this PR was produced by an automated factory and needs human review before merge.",
    ]  # fmt: skip
    return "\n".join(lines)
