# Architecture

## One ticket, start to finish

| Stage | Who | Reads | Writes | Notes |
|---|---|---|---|---|
| `received` | factory | issue | `ticket.json` | issue text is marked untrusted from here on |
| `triaged` | triage agent (fast tier), then `policy.route` (code) | ticket | `triage.json`, `route.json`, labels | vague → `needs_info` with questions; question or out of scope → `declined` |
| `specified` | spec agent (tier set by lane), then `policy.check_spec` (code) | ticket, triage, living specs from `specs/` on the base branch | `spec.json` | a delta spec: ADDED / MODIFIED / REMOVED requirements with SHALL and WHEN/THEN scenarios. A malformed spec goes back once with the problems. An open question without an assumption → `needs_info` |
| `planned` | planner agent, then `policy.check_plan` (code) | spec, file list and in-scope files from the **committed** base branch | `plan.json` | every scenario needs a check: `test` or `qa` (QA only in lanes that run it). Never sees `.factory/`, `.env` or uncommitted files |
| gate | `policy.gate_plan` (code) | plan, ticket, approval label | `gate.json` | protected path → `rejected`; untrusted author, gated path or keyword → `awaiting_approval` |
| `approved` | coder agent (file tools only), under the guard | spec, plan, previous failure output | worktree edits | worktree on `factory-work/<ticket>-<run>`; every tool call is checked and traced; the stop gate (lint, scenario tags) runs before it may finish |
| `coded` | factory | `git diff` | `change.json` | the diff comes from git, not from what the agent says it did |
| `verified` | factory | worktree | `verify.json` | `policy.check_change` and `policy.check_traceability` (every scenario checked by test has a `# scenario: Sn` tag in an added test), then `verify_commands` with a scrubbed env and a command allowlist; fast lane: the changed tests are run on a clean base checkout and must fail there |
| `qa_done` | QA agent + Playwright (complex lane) | the scenarios the plan gave to QA, accessibility snapshot | `qa.json`, `qa.png` | browser can reach only the app on localhost; a pass needs at least one successful `expect` step |
| `reviewed` | reviewer agent | spec, plan, diff, check results, QA | `review.json` | security concerns turn the PR into a draft |
| `ready` / `published` | factory renders the change folder and merges the delta into `specs/`; publisher (code) opens the PR | `bundle.json` | branch `factory/<ticket>-<run>`, PR | re-applies the diff to a clean checkout and re-checks it; spec files are allowed only by name and in the spec layout |

Verify and QA failures go back to the coder with only the failure output, up to the lane's `max_code_attempts`.
With `escalate_on_retry`, each retry runs the coder one tier higher. When attempts run out, the run stops and
comments on the ticket.

## Specs: spec before plan before code

The design borrows from [OpenSpec](https://github.com/Fission-AI/OpenSpec) (delta specs, a living source of truth,
archive on completion) and [sdd](https://github.com/LiorCohen/sdd) (open questions block work, every acceptance
criterion traced to a test, git as the state machine).

```
specs/<capability>/spec.md        living spec: what the app does now
changes/<date>-<ticket>/          added by the factory's PR, alongside the code
  proposal.md  spec.md  tasks.md  why and assumptions; the delta; tasks and how each scenario was checked
```

1. The spec agent returns a typed `Spec`. Code checks it against the living specs on the base branch: ADDED titles
   must be new, MODIFIED and REMOVED titles must exist, text uses SHALL, every requirement has a scenario, scenario
   ids are unique. Open questions need an assumption, or the author is asked.
2. The planner must give every scenario a check. Code refuses a plan that leaves one out.
3. The coder tags each test with `# scenario: S1`. The stop gate checks the tags before the coder finishes, and
   verify checks them again in the diff. QA gets only the scenarios marked `qa`, worded as WHEN/THEN.
4. At publish, code (not an agent) writes the change folder and merges the delta into the living spec. Agents can
   never write `specs/` or `changes/`: both are protected paths. Merging the PR is what makes a requirement true,
   and a rejected PR leaves the specs untouched.

## Hooks: one guard for every backend

`hooks.Guard` holds the coder's rules for one run, saved to `<run>/guard.json`, outside the workspace.

| When | Claude Code hook | LiteLLM / mock | What it does |
|---|---|---|---|
| before a tool call | `PreToolUse` | `tools.call_tool` | deny unless the tool is in the role's list, the path stays in the worktree, writes are in the plan and not protected, and reads avoid `.env`, `.git` and keys |
| after a tool call | `PostToolUse` | `tools.call_tool` | one `tool` event in `trace.jsonl` (denied calls show in the timeline) |
| when the agent stops | `Stop` | end of the tool loop | run `stop_gate_commands` (lint) and check scenario tags; if anything fails, send the agent back once with the output |

Claude Code gets the hooks through `--settings` under `--restricted`, which ignores user, project and local
settings, so a repo cannot add hooks of its own. `--bare` and `--safe-mode` are not used, because both switch hooks
off. A hook that crashes exits 2, which blocks the call (fail closed). The stop gate catches cheap mistakes inside
the coder's session, before the factory spends a full verify run and a retry on a higher tier.

## Lanes and model routing

| | fast lane | complex lane |
|---|---|---|
| for | bugs and chores sized S or M | features, and anything sized L |
| spec | fast tier | frontier tier |
| planner | fast tier | standard tier |
| coder | standard, frontier on retry | standard, frontier on retry |
| proof | tagged tests, and a new test that fails on the base branch and passes with the fix | tagged tests, and browser QA for the scenarios that need a page |
| reviewer | fast tier | standard tier |
| attempts | 2 | 3 |

The triage model only labels the ticket (kind, size, missing info). `policy.route` turns the label into a lane, so the
same triage always gives the same lane. A fooled triage cannot widen what the run may touch either, because the plan
gate still applies (see demo ticket 4). Tiers map to LiteLLM aliases in `tiers`, or to Claude Code's own aliases in
`backend_tiers`.

## Tracking

`tracking.timeline(run)` renders the trace as a Markdown table with one row per stage change, agent call (with tier,
model, tokens and tool-call count), spec and plan check, denied tool call, stop gate and verify result, plus timing. The factory posts it after every `advance()`: in Actions it goes in
the job summary, and on the issue as one comment (marked `<!-- factory-status -->`) that is edited in place, so the
issue shows live progress without spamming notifications. `factory trace --json` gives a roll-up per run for
dashboards.

## Context and token discipline

- **No shared conversations.** Every agent call starts from its role prompt plus one compact artifact, such as the
  plan or a failure tail. Nothing is carried between stages or tickets.
- **Fresh per ticket.** A new run means a new worktree from the base branch, a new `HOME`, and (for Claude Code)
  `--no-session-persistence`.
- **Budgets live in the role file.** `max_turns` and `max_output_tokens` sit in each `agents/*.md` frontmatter.
  Snapshots for QA are capped at 3,000 characters, and failure output at 1,500.
- **Tokens are traced.** Every agent event in `trace.jsonl` carries `usage`. `factory status` and the PR footer show
  totals.

## State

`.factory/ledger.db` records where each run is. The run folder holds everything else. `Factory.advance` is
resumable: it reads the stage and continues. That is how human approval works. Re-triggering the workflow or
running `factory run` again picks up the same run.

In GitHub Actions each job is a fresh machine, so the `.factory/` folder travels between the agents and publish jobs
as an artifact. A run that waits for approval is re-planned when the approval label triggers the workflow again.
This is simple, but it costs one extra planner call. Caching `.factory/` per issue would remove that.

## Learning loop

```
published run ──► trace digest ─┐
                                ├─► retro agent ─► validate ─► agents/lessons/<role>.md ─► next prompt
PR review comments ─► trusted? ─┘                    │
                                                     └─► rejected: about tools/paths/approval/secrets, duplicate, wrong role
```

- Lessons are one imperative sentence each, at most three per run and twelve per role (oldest drop off).
- Locally they apply immediately. In Actions they arrive as a PR, which merges itself only if `auto_merge_lessons` is
  on. The publisher re-validates every added line before opening it.
- `agents/lessons/log.jsonl` records which run each lesson came from.
- Not done yet: measuring whether a lesson helps. The next step is to replay recent tickets with and without a
  lesson using the mock-free backends, and keep the lesson only if outcomes or tokens improve.

## Backends

| Backend | Structured calls | Agentic calls | Credential it holds |
|---|---|---|---|
| `litellm` | chat completion with JSON-schema instructions and one repair retry | OpenAI tool loop over `tools.py` (read, list, search, write) | `LITELLM_API_KEY` in the factory process; models have no tool that can read env |
| `claude-code` | `claude -p --json-schema` in an empty directory, hooks off (no tools to guard) | `claude -p --restricted --tools Read,Glob,Grep,Edit,Write --settings <hooks>` in the worktree | `CLAUDE_CODE_OAUTH_TOKEN` (subscription) or `FACTORY_ANTHROPIC_API_KEY`, passed as the child's only secret |
| `mock` | scripted JSON | scripted file writes through the same guard, including the stop gate | none |
