# Changes

One folder per factory change, named `<date>-<ticket>`, added in the same pull request as the code:

- `proposal.md`: why, what changes, recorded assumptions
- `spec.md`: the delta against `specs/`, as ADDED, MODIFIED and REMOVED requirements with scenarios
- `tasks.md`: the plan's tasks, and how each scenario was checked (by a tagged test or browser QA)

The same PR applies the delta to `specs/<capability>/spec.md`. A merged change folder is the archive.
