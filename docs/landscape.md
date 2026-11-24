# What else is out there, and what this repo borrows

A short survey made while designing this repo (November 2026). Summaries come from the linked pages; where only part
of a page or an abstract was read, it says so.

| Project | What it is | What we borrowed | Where we differ |
|---|---|---|---|
| [Software factory (definition)](https://github.com/mattpocock/dictionary-of-ai-coding/blob/main/dictionary/Software%20factory.md) | Sessions started by triggers rather than people; the design question is which decisions stay human | Issue-label trigger; humans only at the gates that matter | - |
| [GitHub Agentic Workflows](https://github.blog/security/vulnerability-research/under-the-hood-security-architecture-of-github-agentic-workflows/) and its [safe outputs](https://github.github.com/gh-aw/reference/safe-outputs/) | Agents in Actions with read-only tokens; writes are buffered as structured requests and run by separate, narrowly-permissioned jobs with caps and repo scoping | The outbox: the agent job queues comments, labels and status; the publish job replays them under `safe_outputs` caps and label allowlists; `allowed_repos` | gh-aw also runs the agent in a chroot behind an egress firewall and keeps model keys in a gateway. We do not yet (see security.md, known gaps) |
| [Microsoft on the Claude Code action](https://microsoft.com/en-us/security/blog/2026/06/05/securing-ci-cd-in-agentic-world-claude-code-github-action-case) | Post-mortem: the Read tool could read `/proc/self/environ` and leak the API key | "Rule of Two"; scrubbed env; `claude --bare --restricted` | - |
| [LaunchDarkly, a factory small enough to understand](https://launchdarkly.com/blog/build-a-software-factory-small-enough-to-understand/) | Plan → approve → execute → human merge, with per-stage tool scoping | Per-role tool allowlists; the factory re-runs tests itself; protected paths (`.github/`, agent files) | Our human gate is policy-driven, not on every plan |
| [Cloudflare, Astro issue triage](https://blog.cloudflare.com/astro-issue-triage/) (partly read) | Reproduce → diagnose → verify → fix as isolated sub-agents; label-driven state | Bug lane must reproduce first: a regression test that fails on the base branch | We keep state in a ledger, and mirror it to labels |
| [swe-af](https://github.com/Agent-Field/swe-af) (partly read) | Plan-first, worktree per issue, bounded retry loops, no test-gaming | Worktree per run; bounded attempts; coder may not touch tests outside its plan | We escalate the model tier instead of replanning |
| [OpenHands GitHub Action](https://docs.openhands.dev/openhands/usage/run-openhands/github-action) | `fix-me` label or `@openhands-agent` mention triggers a resolver that opens a PR; iteration cap in repo variables | Label trigger; backend and budget as repo variables | One general agent; little about secret scoping. We split roles and permissions |
| [Orkes, router strategy for triage](https://orkes.io/blog/split-the-brain-build-an-issue-triage-bot-with-the-router-strategy) | Cheap classifier picks a label, a specialist does the work; persist the routing decision | Triage on the cheapest tier; routing persisted in `route.json` | Our router is code (`policy.route`), so the lane is reproducible from the triage |
| [Accumulated behavioural rules](https://arxiv.org/abs/2607.13091) (abstract only) | Accepted review comments become version-controlled rules; reported 0% recurrence of ruled-against errors in one deployment | Lessons in `agents/lessons/*.md`, under version control | We validate and cap lessons, and only trusted reviewers count |
| [Unblocked, code-review memory](https://getunblocked.com/blog/code-review-memory/index.md) | Rules scoped to repo/folder/file type, capped, expiring; security findings cannot be silenced | Cap per role (oldest drop off); lessons cannot touch security controls | Not yet: per-path scoping and expiry by usefulness (next step) |

## What this repo adds

Most open projects are either one general agent behind a label (OpenHands, the Claude Code action) or a hosted
product. This repo is a small, readable reference for the infrastructure in between:

1. **Two lanes chosen by code.** A cheap triage model labels the ticket, and a deterministic router picks the lane. The
   fast lane must prove a bug with a test that fails on the base branch. The complex lane gets a frontier planner and
   browser QA.
2. **Model routing as configuration.** Roles name a tier, not a model. Lanes map roles to tiers, and the coder
   escalates a tier after a failed attempt. Every agent call is traced with its tier, model and tokens.
3. **Safe outputs without a platform.** The same read-only-agent and capped-writer split as gh-aw, in about 100
   lines of Python you can read.
4. **A learning loop that cannot loosen its own controls**, with lessons, validation and trusted reviewers only.
5. **Readable tracking.** One timeline renders to the terminal, the Actions job summary and a single issue comment
   that updates as the run moves.

## Next ideas from the survey

- Agents in a container with egress only to the model proxy (gh-aw's firewall plus our command allowlist).
- Lessons scoped to path globs, which load only when the plan touches matching files, and which expire if unused.
- An `@factory` mention on a PR to address review comments on the same branch (OpenHands' follow-up loop).
- Replay recent tickets with and without a lesson, and keep it only if outcomes or tokens improve.
