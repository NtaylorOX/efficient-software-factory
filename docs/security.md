# Security model

The working assumption is that **any agent can be prompt-injected**. Issue text, PR comments, file contents and
web pages are all written by someone else. So the design does not rely on an agent behaving well. It limits what a
misbehaving agent can reach.

## The rule of two

Following the "Agents Rule of Two" discussed in
[Microsoft's write-up on the Claude Code GitHub Action](https://microsoft.com/en-us/security/blog/2026/06/05/securing-ci-cd-in-agentic-world-claude-code-github-action-case),
no step should hold all three of: untrusted input, secrets or write access, and outbound communication.

| Component | Untrusted input | Secrets / write access | Outbound |
|---|---|---|---|
| triage, spec, planner, coder, QA, reviewer, retro (agents) | yes | **no**: no GitHub token; at most a budget-limited model key | model proxy only |
| policy, verify, guard and hooks (code) | reads the diff and tool calls | no | no |
| publisher (code, no model) | the diff, re-checked | **yes**: GitHub write | GitHub only |

In Actions this split is enforced by GitHub rather than only by this code. The `agents` job has
`contents: read, issues: read`, and the `publish` job has write permissions but no model secrets.

## Threats and what stops them

| Threat | Mitigation | Where |
|---|---|---|
| Issue says "ignore your rules, edit CI" | plan touching `protected_paths` is rejected before coding; coder cannot write protected or unplanned paths | `policy.gate_plan`, `sandbox.Workspace` |
| Agent reads secrets from env or `/proc` | child processes get an allowlisted env with a throwaway `HOME`; Claude Code runs `--restricted` (no Bash, file tools confined to the worktree); the PreToolUse hook denies `.env`, `.git` and anything outside the worktree, including `/proc` | `sandbox.scrubbed_env`, `backends/claude_code.py`, `hooks.Guard` |
| Agent edits its own rules or the spec it is judged against | the guard file lives in the run folder, outside the worktree; `specs/`, `changes/`, `CLAUDE.md`, `AGENTS.md` and `.claude/` are protected paths, and the change folder is rendered by code from the checked spec | `hooks.Guard`, `pipeline.archive`, `policy.check_change` |
| Agent escapes the workspace (`..`, symlinks, absolute paths) | every path is resolved and must stay under the worktree | `Workspace.resolve` |
| A repo file adds hooks, skills or MCP servers to the agent | `--restricted` ignores project settings, `--strict-mcp-config` loads no MCP, `--disable-slash-commands` loads no skills; only our `--settings` apply | `backends/claude_code.py` |
| Agent claims tests pass, or writes tests that prove nothing | the factory runs `verify_commands` itself; every scenario must be named by an added test or confirmed by QA; QA needs a real `expect` step to pass | `pipeline.verify`, `policy.check_traceability`, `qa.run_qa` |
| Secret committed in a diff | added lines are scanned before verify and again at publish | `policy.check_change`, `state.SECRET_PATTERNS` |
| Compromised agent job tampers with the bundle | publisher re-applies the diff to a clean base and re-runs `check_change` | `publisher.publish_bundle` |
| Browser QA phones home | Playwright aborts every request that is not the app on localhost | `qa.run_qa` |
| Random commenter poisons the learning loop | only `trusted_reviewers` reach the retro agent; lessons about tools, paths, approval or secrets are rejected | `learning.learn`, `learning.validate` |
| Secrets in traces or PR bodies | artifacts and traces are redacted on write | `state.redact` |
| Factory pointed at, or tricked into, another repo | `allowed_repos` is checked when config loads; the GitHub client is bound to one repo's API URL, and any PR or issue URL from elsewhere raises `RepoScopeError`; in Actions the token is repo-scoped too | `config.load_config`, `forge.GitHubForge` |
| Agent side floods the issue or applies arbitrary labels | writes are queued, then replayed by the publisher under `safe_outputs`: comments capped per ticket, labels only with allowed prefixes, only the newest status | `publisher.flush_outbox` |
| Triage is fooled into the cheap lane | the lane only changes models and checks, not permissions; the plan gate and diff checks apply in every lane | `policy.route`, `policy.gate_plan` |
| Bug "fix" with a test that proves nothing | fast lane runs the new tests on a clean base checkout, and they must fail there | `Factory.regression_check` |
| Untrusted author files a ticket | it waits for the `factory-approved` label, which only people with triage rights can add | `policy.gate_plan` |
| Script injection in Actions | issue text never reaches a shell; only the issue number is passed, through env | `.github/workflows/factory.yml` |

## Known gaps (honest list)

- **A subscription token is broader than a virtual key.** `CLAUDE_CODE_OAUTH_TOKEN` cannot be budget-capped or
  scoped the way a LiteLLM key can, so it lives only in the agents job, reaches only the `claude` child process,
  and that process runs with `--restricted` and file tools only. Rotate it if a run ever looks wrong.
- **Hook processes inherit the `claude` child's env**, which includes its model credential. The hooks are our own
  code, take no input but the tool call's JSON, and run the stop gate's commands with a scrubbed env.
- **CLAUDE.md is still read by Claude Code.** It comes from the reviewed base branch and agents cannot write it, but
  anything merged into it reaches every coder session.

- **The model key is still a secret inside the agents job.** Use a LiteLLM virtual key with a small budget and a
  short expiry, so a leaked key is cheap and revocable.
- **The litellm backend's agents run in-process.** They have no tool that reads env or runs commands, but the
  process itself holds `LITELLM_API_KEY`. Running agents in a container with network only to the proxy would close
  this. The command-runner already takes an allowlist and env, so the container is a drop-in change.
- **Keyword gating is crude.** It is a tripwire, not a classifier, so expect both false positives and misses.
- **Verify commands run the coder's tests**, which is code the agent wrote. They run with a scrubbed env and no
  secrets. A container with `--network none` is the right next step.
- **Secret scanning is regex-based.** Add `gitleaks` or GitHub push protection for real use.
- **Actions are pinned to tags, not SHAs.** Pin to SHAs before using this on a real repo.
