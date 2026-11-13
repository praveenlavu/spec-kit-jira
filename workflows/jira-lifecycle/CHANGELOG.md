# Changelog: the `jira-lifecycle` workflow

The format follows Keep a Changelog. Versions are the `workflow.version` of `workflow.yml`.

## [0.1.0] - 2026-11-13

### Added

- The `jira-lifecycle` workflow: a story to an open pull request (pull, spec review, questions, plan, product owner answers,
  plan review, tasks, push, implement, decisions, strict verify, sync, gate, review, PR, product owner validation).
- Input `issue_number` (type number): the only value put into a shell command; `skj` builds the key from the project in
  `skj.config.json`.
- Inputs `integration` and `review_model`, and one `verdict_<gate>` input for each of the 12 gates (meant to be set
  only by `skj approvals sync --resume`: a verdict given with `--input` approves its gate with no human seeing it).
- Every Jira or PR change as a one-pass preview, gate and apply; the apply refuses unless the plan is exactly what the gate showed.
- Comment approval, experimental: `skj approvals check` after each review and wait gate. Terminal approval stays the default.
- Requires Spec Kit `>=1.2.1`. Team-managed Jira projects only.

### Not included

- No `refresh-context`, `pr-clear` or `pr-prepare` steps (see the README's "Not in 0.1").
