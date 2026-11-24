r"""The factory's state machine. Read this file top to bottom to see everything that happens to a ticket.

    received ─► triaged ─► planned ─► [awaiting_approval ─►] approved ─► coded ─► verified ─► qa_done
             ─► reviewed ─► published  (or `ready` when a separate publish job opens the PR)

    exits:  received ─► needs_info | declined    planned ─► rejected    build loop ─► failed
    loop:   a failed verify or QA goes back to the coder with only the failure output, a bounded number of times

Two lanes share this machine. The triage agent labels a ticket and `policy.route` (code) picks the lane:
  fast     small bugs and chores: cheap models, the fix must ship a test that fails on the base branch
  complex  features and anything large: frontier planner, browser QA, coder escalates a tier on retry
Lanes, tiers and caps live in factory.yaml; this file only reads them.

Each step loads the artifacts it needs from the run folder and saves one back, then advances the ledger.
`advance()` can be called again at any time: it resumes from the stored stage, so a crash, a human
approval or a re-triggered workflow all continue the same run instead of starting over.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import sys
from pathlib import Path

from software_factory import git, policy, tracking
from software_factory.backends.base import Backend, BackendError, T
from software_factory.config import FactoryConfig, LaneConfig, Role, load_role
from software_factory.forge import Forge
from software_factory.models import (
    Change,
    CommandResult,
    GateDecision,
    Plan,
    QAReport,
    Review,
    Route,
    Stage,
    Ticket,
    TicketKind,
    Triage,
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

    def lane(self, r: Run) -> tuple[str, LaneConfig]:
        name = r.load("route", Route).lane
        if name is None:
            raise RuntimeError(f"run {r.id} has no lane; triage stopped it before planning")
        return name, self.cfg.lanes[name]

    # ---- entry points -------------------------------------------------------------------------------

    def handle(self, ticket: Ticket) -> Run:
        """Start or resume the ticket's run and drive it as far as it can go without a human."""
        run_ = self.store.active_run(ticket.id) or self.store.new_run(ticket)
        self.advance(run_)
        return run_

    def advance(self, r: Run) -> None:
        steps = {
            Stage.RECEIVED: self.triage,
            Stage.TRIAGED: self.plan,
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
                break
        self.forge.upsert_status(r.ticket(), tracking.timeline(r))

    # ---- agent calls: every one is routed to a tier and traced with its model and tokens --------------

    def ask(self, r: Run, role: str, prompt: str, schema: type[T], lane: str | None, **trace: object) -> T:
        tier, model = self.cfg.model_for(role, lane)
        result, usage = self.backend.structured(self.role(role), prompt, schema, model)
        r.event("agent", role=role, tier=tier, model=model, usage=usage, **trace)
        return result

    # ---- stages -------------------------------------------------------------------------------------

    def triage(self, r: Run) -> None:
        ticket = r.ticket()
        prompt = f"<ticket author={ticket.author!r}>\n# {ticket.title}\n\n{ticket.body}\n</ticket>"
        triage = self.ask(r, "triage", prompt, Triage, lane=None)
        route = policy.route(self.cfg, triage)
        r.save("triage", triage)
        r.save("route", route)
        labels = [f"kind:{triage.kind.value}", f"size:{triage.size}"] + ([f"lane:{route.lane}"] if route.lane else [])
        self.forge.add_labels(ticket, labels)
        if route.lane is not None:
            r.advance(Stage.TRIAGED, lane=route.lane, ticket_kind=triage.kind.value, reasons=route.reasons)
        elif triage.kind in (TicketKind.QUESTION, TicketKind.OUT_OF_SCOPE):
            self.forge.comment(ticket, f"The factory will not change code for this ticket: {route.reasons[0]}.")
            r.advance(Stage.DECLINED, ticket_kind=triage.kind.value)
        else:
            asks = "\n".join(f"- {q}" for q in triage.missing_info)
            self.forge.comment(ticket, f"Before the factory can start, please answer:\n{asks}")
            r.advance(Stage.NEEDS_INFO, questions=triage.missing_info)

    def plan(self, r: Run) -> None:
        ticket, triage = r.ticket(), r.load("triage", Triage)
        lane, _ = self.lane(r)
        # Context comes from the committed base branch, never from local state such as .factory/ or .env.
        files = git.git(self.root, "ls-tree", "-r", "--name-only", self.cfg.base_branch).split()
        in_scope = [f for f in files if any(fnmatch.fnmatch(f, p) for p in self.cfg.workspace_scope)]
        context = "\n\n".join(
            f"### {f}\n{git.git(self.root, 'show', f'{self.cfg.base_branch}:{f}')[:CONTEXT_FILE_CHARS]}"
            for f in in_scope
            if f.endswith((".py", ".html"))
        )
        bug_rule = (
            "\nThis is a bug fix: include a test file in files_to_change, with a test that fails before the fix."
            if self.cfg.lanes[lane].require_regression_test
            else ""
        )
        prompt = (
            f"Repository files:\n{chr(10).join(files)}\n\nFiles you may change, with contents:\n{context}\n\n"
            f"Triage: {triage.kind.value}, size {triage.size}. {triage.summary} {triage.repro}{bug_rule}\n\n"
            f"<ticket author={ticket.author!r}>\n# {ticket.title}\n\n{ticket.body}\n</ticket>"
        )
        plan = self.ask(r, "planner", prompt, Plan, lane)
        r.save("plan", plan)
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
        """Code, verify and (complex lane) QA in a loop, each failure fed back to a fresh coder call."""
        plan, (lane, lane_cfg) = r.load("plan", Plan), self.lane(r)
        if not r.workspace.exists():
            git.add_worktree(self.root, r.workspace, r.work_branch, self.cfg.base_branch)
        ws = Workspace(r.workspace, self.cfg.protected_paths, writable=plan.files_to_change)
        feedback = ""
        for attempt in range(1, lane_cfg.max_code_attempts + 1):
            self.code(r, ws, plan, feedback, attempt, lane)
            verification = self.verify(r, plan, lane_cfg)
            if not verification.passed:
                feedback = _verify_feedback(verification)
                continue
            r.advance(Stage.VERIFIED, attempt=attempt)
            report = self.qa(r, plan, lane)
            if report.passed:
                r.advance(Stage.QA_DONE, attempt=attempt)
                return
            feedback = "QA failed in the browser:\n" + json.dumps(report.criteria, indent=1) + "\n" + _qa_trail(report)
            r.advance(Stage.APPROVED, retry="qa")
        self.fail(r, f"still failing after {lane_cfg.max_code_attempts} attempts:\n\n{feedback[-1500:]}")

    def code(self, r: Run, ws: Workspace, plan: Plan, feedback: str, attempt: int, lane: str) -> None:
        task = f"Plan:\n{plan.model_dump_json(indent=1)}\n\nYou may write only: {', '.join(plan.files_to_change)}" + (
            f"\n\nYour previous attempt failed. Fix this and nothing else:\n{feedback}" if feedback else ""
        )
        tier, model = self.cfg.model_for("coder", lane, attempt)
        summary, usage = self.backend.agentic(self.role("coder"), task, ws, model, r.home)
        files, diff = git.diff(r.workspace, self.cfg.base_branch)
        r.save("change", Change(agent_summary=summary, files_changed=files, diff=diff))
        r.event("agent", role="coder", tier=tier, model=model, attempt=attempt, usage=usage, files=files)
        r.advance(Stage.CODED, attempt=attempt)

    def verify(self, r: Run, plan: Plan, lane_cfg: LaneConfig) -> Verification:
        """The orchestrator runs the checks itself. The coder's claims are not evidence."""
        change = r.load("change", Change)
        violations = policy.check_change(self.cfg, plan.files_to_change, change)
        results, regression = [], None
        if not violations:
            env = scrubbed_env(r.home)
            cmds = [[sys.executable if t == "{python}" else t for t in c] for c in self.cfg.verify_commands]
            for shown, cmd in zip(self.cfg.verify_commands, cmds, strict=True):
                code, out = run(cmd, r.workspace, env, allowlist=cmds)
                shown = [t.strip("{}") for t in shown]  # "{python}" reads as "python" in PRs and traces
                results.append(CommandResult(command=shown, exit_code=code, output_tail=out[-1500:]))
            if lane_cfg.require_regression_test and all(x.exit_code == 0 for x in results):
                regression, problem = self.regression_check(r, change)
                violations += problem
        v = Verification(
            passed=not violations and all(x.exit_code == 0 for x in results),
            results=results,
            policy_violations=violations,
            regression=regression,
        )
        r.save("verify", v)
        r.event("verify", passed=v.passed, violations=violations, exit_codes=[x.exit_code for x in results])
        return v

    def regression_check(self, r: Run, change: Change) -> tuple[CommandResult | None, list[str]]:
        """Fail-before, pass-after: the new tests must fail on the untouched base branch.

        A bug fix whose test also passes without the fix has not shown that it reproduces the bug.
        """
        tests = [f for f in change.files_changed if any(fnmatch.fnmatch(f, g) for g in self.cfg.test_globs)]
        if not tests:
            return None, [f"bug fix must add or change a test matching {self.cfg.test_globs}"]
        base = r.dir / "base-check"
        git.git(self.root, "worktree", "add", "--quiet", "--detach", str(base), self.cfg.base_branch)
        try:
            for t in tests:
                (base / t).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(r.workspace / t, base / t)
            cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests]
            code, out = run(cmd, base, scrubbed_env(r.home), allowlist=[cmd[:3]])  # pytest on named test files only
        finally:
            git.remove_worktree(self.root, base)
        result = CommandResult(
            command=["python", "-m", "pytest", *tests, "# on base"], exit_code=code, output_tail=out[-800:]
        )
        if code == 0:
            return result, ["the new test passes on the base branch too, so it does not reproduce the bug"]
        return result, []

    def qa(self, r: Run, plan: Plan, lane: str) -> QAReport:
        if not self.cfg.lanes[lane].qa:
            report = QAReport(passed=True, criteria={}, steps_taken=[], notes=f"skipped: {lane} lane has no browser QA")
        elif not self.qa_enabled:
            report = QAReport(passed=True, criteria={}, steps_taken=[], notes="skipped: QA disabled or no Chromium")
        else:
            tier, model = self.cfg.model_for("qa", lane)
            report, usage = run_qa(
                self.backend, self.role("qa"), model, r.workspace, self.cfg.qa_app, plan.acceptance_criteria, r.dir
            )
            r.event("agent", role="qa", tier=tier, model=model, usage=usage, passed=report.passed,
                    steps=len(report.steps_taken))  # fmt: skip
        r.save("qa", report)
        return report

    def review(self, r: Run) -> None:
        plan, change = r.load("plan", Plan), r.load("change", Change)
        verification, report = r.load("verify", Verification), r.load("qa", QAReport)
        lane, _ = self.lane(r)
        prompt = (
            f"Plan:\n{plan.model_dump_json(indent=1)}\n\nChecks: "
            + ", ".join(f"{' '.join(c.command[-3:])} -> {c.exit_code}" for c in verification.results)
            + f"\nQA: {json.dumps(report.criteria)} {report.notes}\n\n<diff>\n{change.diff}\n</diff>"
        )
        review = self.ask(r, "reviewer", prompt, Review, lane)
        r.save("review", review)
        r.advance(Stage.REVIEWED, approve=review.approve, concerns=len(review.security_concerns))

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
    tokens, lane = r.tokens(), tracking.lane_of(r)
    regression = verification.regression
    lines = [
        f"Closes {ticket.url or ticket.id}" if ticket.id.startswith("gh") else f"Ticket: `{ticket.id}`",
        f"Lane: **{lane}**",
        "", f"**Plan.** {plan.summary}", "", "**Acceptance criteria (browser QA)**",
        *[f"- [{'x' if report.criteria.get(c) else ' '}] {c}" for c in plan.acceptance_criteria],
        *([f"\n_{report.notes}_"] if report.notes else []),
        "", "**Checks run by the factory**",
        *[f"- `{' '.join(c.command)}` exit {c.exit_code}" for c in verification.results],
        *([f"- regression test fails on the base branch (exit {regression.exit_code}), passes with the fix"]
          if regression else []),
        "", f"**Agent review:** {'approve' if review.approve else 'changes requested'}",
        *[f"- {c}" for c in review.comments],
        *[f"- :warning: security: {c}" for c in review.security_concerns],
        "", f"**Coder summary (unverified):** {change.agent_summary}",
        "", f"Run `{r.id}` · tokens in/out {tokens.input_tokens}/{tokens.output_tokens} · "
        "this PR was produced by an automated factory and needs human review before merge.",
        "", "<details><summary>Run timeline</summary>", "", tracking.timeline(r), "", "</details>",
    ]  # fmt: skip
    return "\n".join(lines)
