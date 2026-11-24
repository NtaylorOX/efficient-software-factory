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
| planner, coder, QA, reviewer, retro (agents) | yes | **no**: no GitHub token; at most a budget-limited model key | model proxy only |
| policy, verify (code) | reads the diff | no | no |
| publisher (code, no model) | the diff, re-checked | **yes**: GitHub write | GitHub only |

In Actions this split is enforced by GitHub rather than only by this code. The `agents` job has
`contents: read, issues: read`, and the `publish` job has write permissions but no model secrets.

## Threats and what stops them

| Threat | Mitigation | Where |
|---|---|---|
| Issue says "ignore your rules, edit CI" | plan touching `protected_paths` is rejected before coding; coder cannot write protected or unplanned paths | `policy.gate_plan`, `sandbox.Workspace` |
| Agent reads secrets from env or `/proc` | child processes get an allowlisted env with a throwaway `HOME`; Claude Code runs `--bare --restricted` (no Bash, file tools confined to the worktree) | `sandbox.scrubbed_env`, `backends/claude_code.py` |
| Agent escapes the workspace (`..`, symlinks, absolute paths) | every path is resolved and must stay under the worktree | `Workspace.resolve` |
| Agent claims tests pass | the factory runs `verify_commands` itself; QA needs a real `expect` step to pass | `pipeline.verify`, `qa.run_qa` |
| Secret committed in a diff | added lines are scanned before verify and again at publish | `policy.check_change`, `state.SECRET_PATTERNS` |
| Compromised agent job tampers with the bundle | publisher re-applies the diff to a clean base and re-runs `check_change` | `publisher.publish_bundle` |
| Browser QA phones home | Playwright aborts every request that is not the app on localhost | `qa.run_qa` |
| Random commenter poisons the learning loop | only `trusted_reviewers` reach the retro agent; lessons about tools, paths, approval or secrets are rejected | `learning.learn`, `learning.validate` |
| Secrets in traces or PR bodies | artifacts and traces are redacted on write | `state.redact` |
| Untrusted author files a ticket | it waits for the `factory-approved` label, which only people with triage rights can add | `policy.gate_plan` |
| Script injection in Actions | issue text never reaches a shell; only the issue number is passed, through env | `.github/workflows/factory.yml` |

## Known gaps (honest list)

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
