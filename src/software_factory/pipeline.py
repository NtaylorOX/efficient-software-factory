r"""The factory's state machine. Read this file top to bottom to see everything that happens to a ticket.

    received ─► triaged ─► specified ─► planned ─► [awaiting_approval ─►] approved ─► coded ─► verified
             ─► qa_done ─► reviewed ─► published  (or `ready` when a separate publish job opens the PR)

    exits:  received/triaged ─► needs_info | declined    planned ─► rejected    build loop ─► failed
    loop:   a failed verify or QA goes back to the coder with only the failure output, a bounded number of times

Spec-driven: a spec agent writes the change as a delta spec (requirements with WHEN/THEN scenarios) before
anyone plans or codes. Code checks the spec, then checks that the plan covers every scenario, then that
every scenario is named by a test or confirmed by browser QA. The PR carries the change folder and the
updated living spec (specs.py), so the spec and the code merge together.

Inside the coder's session a Guard (hooks.py) checks every tool call and runs a stop gate before it may
finish. Claude Code runs it as PreToolUse, PostToolUse and Stop hooks; LiteLLM and the mock call it directly.

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
import time
from pathlib import Path

from software_factory import git, policy, specs, tracking
from software_factory.backends.base import Backend, BackendError, T
from software_factory.config import FactoryConfig, LaneConfig, Role, load_role
from software_factory.forge import Forge
from software_factory.hooks import Guard, GuardPolicy
from software_factory.models import (
    Change,
    CommandResult,
    GateDecision,
    Plan,
    QAReport,
    Review,
    Route,
    Spec,
    Stage,
    Ticket,
    TicketKind,
    Triage,
    Verification,
)
from software_factory.publisher import Bundle, PublishError, publish_bundle
from software_factory.qa import chromium_path, run_qa
from software_factory.sandbox import SandboxError, run, scrubbed_env
from software_factory.state import Run, Store

CONTEXT_FILE_CHARS = 6000  # per file shown to the planner; the coder reads what it needs itself
SPEC_ATTEMPTS = 2  # a spec or plan that fails its check goes back once with the problems, then the run stops


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
            Stage.TRIAGED: self.specify,
            Stage.SPECIFIED: self.plan,
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

    def base_files(self) -> list[str]:
        # Context comes from the committed base branch, never from local state such as .factory/ or .env.
        return git.git(self.root, "ls-tree", "-r", "--name-only", self.cfg.base_branch).split()

    def show(self, path: str) -> str:
        return git.git(self.root, "show", f"{self.cfg.base_branch}:{path}")

    def living_specs(self) -> dict[str, str]:
        files = [f for f in self.base_files() if fnmatch.fnmatch(f, "specs/*/spec.md")]
        return {f.split("/")[1]: self.show(f) for f in files}

    def specify(self, r: Run) -> None:
        """Spec before plan: the change as requirements and scenarios, checked by code before anyone plans."""
        ticket, triage = r.ticket(), r.load("triage", Triage)
        lane, _ = self.lane(r)
        living = self.living_specs()
        shown = "\n\n".join(f"<living-spec capability={c!r}>\n{t}\n</living-spec>" for c, t in living.items())
        prompt = (
            f"{shown or 'There are no living specs yet.'}\n\n"
            f"Triage: {triage.kind.value}, size {triage.size}. {triage.summary} {triage.repro}\n\n"
            f"<ticket author={ticket.author!r}>\n# {ticket.title}\n\n{ticket.body}\n</ticket>"
        )
        feedback = ""
        for attempt in range(1, SPEC_ATTEMPTS + 1):
            spec = self.ask(r, "spec", prompt + feedback, Spec, lane, attempt=attempt)
            problems, blocking = policy.check_spec(spec, living)
            r.save("spec", spec)
            r.event("spec_check", passed=not problems, problems=problems, blocking=blocking)
            if blocking:
                asks = "\n".join(f"- {q}" for q in blocking)
                self.forge.comment(ticket, f"Before the factory can write the spec, please answer:\n{asks}")
                r.advance(Stage.NEEDS_INFO, questions=blocking)
                return
            if not problems:
                assumed = [q.assumption for q in spec.open_questions]
                ids = [x.id for x in spec.scenarios()]
                r.advance(Stage.SPECIFIED, requirements=len(spec.requirements), scenarios=ids, assumptions=assumed)
                return
            feedback = "\n\nYour previous spec was rejected. Fix only this:\n" + "\n".join(f"- {p}" for p in problems)
        self.fail(r, "the spec did not pass its check:\n" + "\n".join(f"- {p}" for p in problems))

    def plan(self, r: Run) -> None:
        ticket, spec = r.ticket(), r.load("spec", Spec)
        lane, lane_cfg = self.lane(r)
        files = self.base_files()
        in_scope = [f for f in files if any(fnmatch.fnmatch(f, p) for p in self.cfg.workspace_scope)]
        context = "\n\n".join(
            f"### {f}\n{self.show(f)[:CONTEXT_FILE_CHARS]}" for f in in_scope if f.endswith((".py", ".html"))
        )
        rules = "Browser QA runs in this lane." if lane_cfg.qa else "This lane has no browser QA: check by test."
        if lane_cfg.require_regression_test:
            rules += " This is a bug fix: its test must fail before the fix, so include the test file."
        prompt = (
            f"Repository files:\n{chr(10).join(files)}\n\nFiles you may change, with contents:\n{context}\n\n"
            f"<spec>\n{specs.delta_md(spec)}\n</spec>\n\n{rules}\n\n"
            f"<ticket author={ticket.author!r}>\n# {ticket.title}\n\n{ticket.body}\n</ticket>"
        )
        feedback = ""
        for attempt in range(1, SPEC_ATTEMPTS + 1):
            plan = self.ask(r, "planner", prompt + feedback, Plan, lane, attempt=attempt)
            r.save("plan", plan)
            problems = policy.check_plan(spec, plan, qa_available=lane_cfg.qa)
            r.event("plan_check", passed=not problems, problems=problems)
            if not problems:
                r.advance(Stage.PLANNED, checks={c.scenario: c.by for c in plan.checks})
                return
            feedback = "\n\nYour previous plan was rejected. Fix only this:\n" + "\n".join(f"- {p}" for p in problems)
        self.fail(r, "the plan does not cover the spec:\n" + "\n".join(f"- {p}" for p in problems))

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
        plan, spec, (lane, lane_cfg) = r.load("plan", Plan), r.load("spec", Spec), self.lane(r)
        if not r.workspace.exists():
            git.add_worktree(self.root, r.workspace, r.work_branch, self.cfg.base_branch)
        guard = self.guard(r, plan)
        feedback = ""
        for attempt in range(1, lane_cfg.max_code_attempts + 1):
            self.code(r, guard, spec, plan, feedback, attempt, lane)
            verification = self.verify(r, spec, plan, lane_cfg)
            if not verification.passed:
                feedback = _verify_feedback(verification)
                continue
            r.advance(Stage.VERIFIED, attempt=attempt)
            report = self.qa(r, spec, plan, lane)
            if report.passed:
                r.advance(Stage.QA_DONE, attempt=attempt)
                return
            feedback = "QA failed in the browser:\n" + json.dumps(report.criteria, indent=1) + "\n" + _qa_trail(report)
            r.advance(Stage.APPROVED, retry="qa")
        self.fail(r, f"still failing after {lane_cfg.max_code_attempts} attempts:\n\n{feedback[-1500:]}")

    def guard(self, r: Run, plan: Plan) -> Guard:
        """The coder's rules for this run, saved outside the workspace where the agent cannot read or edit them."""
        python = [[sys.executable if t == "{python}" else t for t in c] for c in self.cfg.stop_gate_commands]
        policy_ = GuardPolicy(
            run_id=r.id, workspace=str(r.workspace), home=str(r.home), trace=str(r.dir / "trace.jsonl"),
            tools=self.role("coder").tools, protected=self.cfg.protected_paths, writable=plan.files_to_change,
            test_globs=self.cfg.test_globs, tagged_scenarios=[c.scenario for c in plan.checks if c.by == "test"],
            stop_commands=python,
        )  # fmt: skip
        return Guard(policy_, r.dir / "guard.json")

    def code(self, r: Run, guard: Guard, spec: Spec, plan: Plan, feedback: str, attempt: int, lane: str) -> None:
        task = (
            f"<spec>\n{specs.delta_md(spec)}\n</spec>\n\nPlan:\n{plan.model_dump_json(indent=1)}\n\n"
            f"You may write only: {', '.join(plan.files_to_change)}"
        ) + (f"\n\nYour previous attempt failed. Fix this and nothing else:\n{feedback}" if feedback else "")
        tier, model = self.cfg.model_for("coder", lane, attempt)
        started = time.time()
        summary, usage = self.backend.agentic(self.role("coder"), task, guard, model, r.home)
        files, diff = git.diff(r.workspace, self.cfg.base_branch)
        r.save("change", Change(agent_summary=summary, files_changed=files, diff=diff))
        calls = [e for e in r.trace() if e["kind"] == "tool" and e["t"] >= started]
        denied = sum(1 for e in calls if not e.get("allowed"))
        r.event("agent", role="coder", tier=tier, model=model, attempt=attempt, usage=usage, files=files,
                tool_calls=len(calls), denied=denied)  # fmt: skip
        r.advance(Stage.CODED, attempt=attempt)

    def verify(self, r: Run, spec: Spec, plan: Plan, lane_cfg: LaneConfig) -> Verification:
        """The orchestrator runs the checks itself. The coder's claims are not evidence."""
        change = r.load("change", Change)
        violations = policy.check_change(self.cfg, plan.files_to_change, change)
        violations += policy.check_traceability(spec, plan, change, self.cfg.test_globs)
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

    def qa(self, r: Run, spec: Spec, plan: Plan, lane: str) -> QAReport:
        scenarios = qa_scenarios(spec, plan)
        if not self.cfg.lanes[lane].qa:
            report = QAReport(passed=True, criteria={}, steps_taken=[], notes=f"skipped: {lane} lane has no browser QA")
        elif not scenarios:
            report = QAReport(passed=True, criteria={}, steps_taken=[], notes="skipped: no scenario needs a browser")
        elif not self.qa_enabled:
            report = QAReport(passed=True, criteria={}, steps_taken=[], notes="skipped: QA disabled or no Chromium")
        else:
            tier, model = self.cfg.model_for("qa", lane)
            report, usage = run_qa(self.backend, self.role("qa"), model, r.workspace, self.cfg.qa_app, scenarios, r.dir)
            r.event("agent", role="qa", tier=tier, model=model, usage=usage, passed=report.passed,
                    steps=len(report.steps_taken))  # fmt: skip
        r.save("qa", report)
        return report

    def review(self, r: Run) -> None:
        plan, change, spec = r.load("plan", Plan), r.load("change", Change), r.load("spec", Spec)
        verification, report = r.load("verify", Verification), r.load("qa", QAReport)
        lane, _ = self.lane(r)
        prompt = (
            f"<spec>\n{specs.delta_md(spec)}\n</spec>\n\nPlan:\n{plan.model_dump_json(indent=1)}\n\nChecks: "
            + ", ".join(f"{' '.join(c.command[-3:])} -> {c.exit_code}" for c in verification.results)
            + f"\nQA: {json.dumps(report.criteria)} {report.notes}\n\n<diff>\n{change.diff}\n</diff>"
        )
        review = self.ask(r, "reviewer", prompt, Review, lane)
        r.save("review", review)
        r.advance(Stage.REVIEWED, approve=review.approve, concerns=len(review.security_concerns))

    def publish(self, r: Run) -> None:
        """Hand the diff to the publisher. The agent worktree is deleted; only the diff travels on."""
        plan, review, ticket = r.load("plan", Plan), r.load("review", Review), r.ticket()
        spec_files = self.archive(r)
        _, diff = git.diff(r.workspace, self.cfg.base_branch)
        draft = not review.approve or bool(review.security_concerns)
        bundle = Bundle(
            kind="change", run_id=r.id, ticket=ticket, branch=r.branch, title=ticket.title, body=pr_body(r),
            draft=draft, allowed_files=plan.files_to_change, spec_files=spec_files, diff=diff,
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

    def archive(self, r: Run) -> list[str]:
        """Write the change folder and merge the delta into the living specs, in the worktree, by code.

        These paths are protected from agents; the factory renders them from the checked, typed spec.
        """
        spec, plan, ticket = r.load("spec", Spec), r.load("plan", Plan), r.ticket()
        report = r.load("qa", QAReport)
        qa_ok = {k.split(":", 1)[0]: v for k, v in report.criteria.items()}
        folder = f"changes/{time.strftime('%Y-%m-%d')}-{ticket.id}"
        out = specs.change_files(
            folder, ticket.title, ticket.url or ticket.id, spec, plan, r.load("verify", Verification), qa_ok
        )
        living = self.living_specs()
        for cap in sorted({x.capability for x in spec.requirements}):
            out[specs.living_path(cap)] = specs.apply_delta(cap, living.get(cap, ""), spec)
        for rel, text in out.items():
            (r.workspace / rel).parent.mkdir(parents=True, exist_ok=True)
            (r.workspace / rel).write_text(text)
        r.event("archive", folder=folder, specs=[f for f in out if f.startswith("specs/")])
        return sorted(out)

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


def qa_scenarios(spec: Spec, plan: Plan) -> list[str]:
    """The scenarios browser QA must confirm, as `S1: WHEN ... THEN ...` lines."""
    by_qa = {c.scenario for c in plan.checks if c.by == "qa"}
    return [f"{s.id}: WHEN {s.when} THEN {s.then}" for s in spec.scenarios() if s.id in by_qa]


def pr_body(r: Run) -> str:
    plan, change, review = r.load("plan", Plan), r.load("change", Change), r.load("review", Review)
    verification, report, ticket = r.load("verify", Verification), r.load("qa", QAReport), r.ticket()
    spec = r.load("spec", Spec)
    by = {c.scenario: c.by for c in plan.checks}
    qa_ok = {k.split(":", 1)[0]: v for k, v in report.criteria.items()}
    tokens, lane = r.tokens(), tracking.lane_of(r)
    regression = verification.regression
    lines = [
        f"Closes {ticket.url or ticket.id}" if ticket.id.startswith("gh") else f"Ticket: `{ticket.id}`",
        f"Lane: **{lane}**",
        "", f"**Why.** {spec.why}", "", f"**Plan.** {plan.summary}",
        "", "**Spec** (full delta in `changes/`, merged into `specs/` by this PR)", "",
        "| op | requirement | scenario | checked by | result |", "|---|---|---|---|---|",
        *[f"| {q.op.value} | {q.title} | {s.id} {s.name} | {by.get(s.id, '-')} | "
          f"{'✅' if (qa_ok.get(s.id) if by.get(s.id) == 'qa' else verification.passed) else '❌'} |"
          for q in spec.requirements for s in q.scenarios],
        *[f"| REMOVED | {q.title} | - | - | - |" for q in spec.requirements if not q.scenarios],
        *([f"\n_Assumed: {'; '.join(q.question + ' ' + q.assumption for q in spec.open_questions)}_"]
          if spec.open_questions else []),
        *([f"\n_QA: {report.notes}_"] if report.notes else []),
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
