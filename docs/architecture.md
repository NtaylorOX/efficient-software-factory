# Architecture

## One ticket, start to finish

| Stage | Who | Reads | Writes | Notes |
|---|---|---|---|---|
| `received` | factory | issue | `ticket.json` | issue text is marked untrusted from here on |
| `planned` | planner agent (structured, no tools) | ticket, file list and in-scope files from the **committed** base branch | `plan.json` | never sees `.factory/`, `.env` or uncommitted files |
| gate | `policy.gate_plan` (code) | plan, ticket, approval label | `gate.json` | protected path → `rejected`; untrusted author, gated path or keyword → `awaiting_approval` |
| `approved` | coder agent (file tools only) | plan, previous failure output | worktree edits | worktree on `factory-work/<ticket>-<run>`; writes limited to the plan's files |
| `coded` | factory | `git diff` | `change.json` | the diff comes from git, not from what the agent says it did |
| `verified` | factory | worktree | `verify.json` | `policy.check_change`, then `verify_commands` with a scrubbed env and a command allowlist |
| `qa_done` | QA agent + Playwright | acceptance criteria, accessibility snapshot | `qa.json`, `qa.png` | browser can reach only the app on localhost; a pass needs at least one successful `expect` step |
| `reviewed` | reviewer agent | plan, diff, check results, QA | `review.json` | security concerns turn the PR into a draft |
| `ready` / `published` | publisher (code) | `bundle.json` | branch `factory/<ticket>-<run>`, PR | re-applies the diff to a clean checkout and re-checks it |

Verify and QA failures go back to the coder with only the failure output, up to `max_code_attempts`. Then the run
stops and comments on the ticket.

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
| `claude-code` | `claude -p --json-schema` in an empty directory | `claude -p --tools Read,Glob,Grep,Edit,Write` in the worktree | `FACTORY_ANTHROPIC_API_KEY`, passed as the child's only secret |
| `mock` | scripted JSON | scripted file writes, through the same workspace rules | none |
