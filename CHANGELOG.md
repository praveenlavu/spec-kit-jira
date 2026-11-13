# Changelog

All notable changes to `skj` are listed here. The format follows Keep a Changelog, and versions follow
[the versioning rules](VERSIONING.md).

## [0.1.0] - 2026-11-13

The first version. Everything below is built and tested against a simulated Jira and was checked live on a real
Jira Cloud Free site (see Known limits).

### Added

Setup, health check and reading

- `skj setup` writes `skj.config.json` and says how to store your Jira identity. On macOS it never touches the token: it prints
  the keychain line for you to run yourself. Elsewhere it writes a private file only you can read. It asks its questions only in a
  terminal, and a malformed `--site` or `--project` is a usage error (exit 2).
- `skj doctor` checks the setup in one read-only pass: git, Spec Kit, the config and the target branch's config, the site pin,
  your identity, the project type, the approvers and whether comments can be edited by others.
- `skj get` summarises one issue; `skj comments` lists an issue's comments (an issue number or a key of the configured project, as
  every other verb); `skj status` shows where a story stands.
- The site in the config of the target branch is pinned: if the site in use differs, `skj` refuses to send your credentials.

The checklist for each story

- `skj dod` seeds and refreshes one checklist per story, `dod/DoD_<KEY>.md`, from the story's acceptance criteria (read from a
  description section under a heading you can configure). Control, bidi and zero-width characters from Jira are removed before
  anything is written, and a refresh needs an existing checklist of at most 1 MiB. A `--spec-dir` that is not a plain relative path in the repository is a
  usage error (exit 2).
- `skj branch` names the local branch for a story (a dry run unless you add `--apply`).

Decisions and questions

- `skj decisions` records, resolves and lists decisions in `dod/Decisions_<KEY>.md`; `--post` sends the unposted ones to the story
  as one new comment.
- `skj questions` lists open and answered questions; `--post` sends the open ones to the story as one new comment.

Tasks to subtasks

- `skj push` mirrors the tasks of `tasks.md` into subtasks of the story and moves ticked ones to done. A task you remove is reported
  in the preview and its subtask is left alone. An @mention or an email address in a task title is replaced (`@user`, `[email]`)
  before it reaches Jira, as in the decisions and questions comments.

The gate

- `skj gate` checks the work before merge: each acceptance criterion traced to a test, assertions, drift of the criteria,
  evidence and commits. It also warns about Jira smart-commit commands in commit messages.

Business rules (`rules.enabled`)

- `skj rules <ref>` pulls the business rules of the story's parent issues (an Epic by default, at most 3 levels up, GETs only) into
  `dod/Rules_<KEY>.md`, each rule tagged `BR-<KEY>-<n>`; `skj dod` records where they came from in the ledger. A second rule label
  inside a rule (`... PO Rule two: ...`, `... and BR-2: ...`) makes that rule a draft with the error `bad-rule-line`; prose shaped like
  a label (`Why: GDPR rules apply: always`) fails closed the same way (see [configuration](docs/configuration.md)).
- `skj rules <ref> --confirm` records in the ledger the rules a person reviewed. A new rule is recorded without a terminal; a
  changed or dropped one needs your own terminal and a typed code.
- `skj gate` reports `rule_coverage` (every approved rule cited by a tested criterion, or excluded by a posted decision, and
  confirmed) and `rule_drift` (the committed rules against the rules in Jira now), and notes a confirmation that changed against
  the target branch. `skj strict check` checks the rules at `pr` and `done`, and `skj status` shows a `rules` stage.

Strict mode

- `skj strict check` checks the strict-mode rules of a phase, `skj strict mode` says which mode CI judges a pull request by, and
  `skj strict verify` runs the configured test command at each criterion's red and green commit. An override is a hand-written
  decision line of kind `override`.

Pull requests

- `skj pr create` opens the pull request through the local `gh` tool, with a title and body built from the checklist.
- `skj pr link` links the merged pull request to the story: one remote link and one comment.

Closing a story

- `skj done` closes a story whose checklist is fully ticked: it attaches the checklist and the decisions file, then moves the story
  to done. Done means the status category "done"; the resolution field is never read. An item marked not applicable in any
  spelling, or a nested item that is not a ticked box and does not start with a letter or digit, counts as not done.

Approvals (experimental)

- Comment approval: named people can approve a decision or a step by a comment on the story. It is experimental and can only show
  tampering, not prevent it (see [security and trust](docs/security-and-trust.md)). Terminal approval stays the default and the
  recommended mode.
- `skj approvals check` says whether a gate is met; `skj approvals sync` reads the verdict for a paused workflow run. A review gate
  refuses while a file it shows has uncommitted changes, so an approval never covers a stale ledger.

Kits, stack and calibration

- `skj stack` detects the stacks in the repository and keeps the stack profile `skj.stack.json`, with three seed profiles
  (Node with TypeScript, Python with pytest, Salesforce).
- `skj kits` checks installed Spec Kit extensions against the lockfile `skj.kits.lock.json`.
- `skj calibrate` writes a calibration prompt for the review and scores the answer against golden examples.

Spec Kit extension and workflow

- The Spec Kit extension `jira-lifecycle`: 13 agent commands, two hooks, and the agent's rules (`rules/hitl.md`, `rules/routing.md`).
- The `jira-lifecycle` workflow: from a story to an open pull request, with a pause for you at every gate. Its only required input is the
  issue number (`issue_number`); the optional ones are `integration`, `review_model` and twelve gate verdict inputs, which an agent
  never passes. The agent runs headless there, so a check after each agent step (`skj dod check`, read-only)
  stops the run when the step left nothing or left it uncommitted, and a review that did not run stops it before the review gate.
- `docs/claude-permissions.json`: the one tested allow and deny list for Claude Code's headless agent, usable as
  `SPECKIT_INTEGRATION_CLAUDE_EXTRA_ARGS` at once or as `.claude/settings.json` after the one-time trust accept. It allows no Jira or
  pull request write.
- Local catalog entries in `catalog/` and `scripts/build_bundle.py`, which builds the two release zips. Every URL in them is still
  a placeholder.

Checks for GitHub

- Four copy-in GitHub checks in `ci/templates/`: branch name, pull request, final check, and the link after a merge.
- The kit's own test and Spec Kit contract workflows in `.github/workflows/`. Both have run on GitHub-hosted runners (see Known
  limits).

Documentation

- The README, a quick start, every setting, a glossary, security and trust, checks for GitHub, compatibility, stack and kits,
  and the list of what is not proven yet.
- The agent's runbook `AGENTS.md`, `SECURITY.md`, and these release documents: this changelog, the
  [versioning rules](VERSIONING.md), the [contributing guide](CONTRIBUTING.md), the [release checklist](docs/release-checklist.md)
  and the [catalog submission checklist](docs/catalog-submission.md).

### Known limits

- Tested live on a real Jira Cloud Free site, on a team-managed project (2026-11). About 600 live checks covered setup and doctor, get and comments (including more than 100 comments and pagination), the branch dry run, dod seed and refresh with acceptance-criteria drift, decisions, questions (posted once, never duplicated), push of subtasks, the gate, strict mode (its refusals and its success path, up to `done` closing a story), business rules on a real Epic parent chain, pr link, done with the ledger attachment, the Spec Kit add-on and its 13 commands, the bundle and a security sweep; the 23 defects they found are fixed and re-tested. The `jira-lifecycle` workflow, driven by Claude Code, ran end to end on a story, up to a pull request on a private GitHub repository; a rejected gate left Jira unchanged, and a second and third run added no duplicate comment or subtask. The final pass ran 143 of 143 checks green. Running the workflow found six problems (among them the headless permissions, a missing check after each agent step and a stale ledger at the review approval gates), all fixed. A `/approve D1` line typed in the Jira editor is stored as plain text. Not yet run live: comment approval by a second user and an edit by someone else (comment approval stays experimental), company-managed projects, real 429, 503, timeout and 403 answers, the token store on Linux and Windows, the copy-in GitHub checks in a repository of their own and a pull request from a fork, and `skj done` after a real merge with a decision a human validated. What is still unproven is listed in
  [docs/unverified.md](docs/unverified.md).
- Team-managed projects only. `skj doctor` warns about company-managed projects in the `project-type` row.
- Jira Cloud only, and GitHub is the only pull request host.
- The tests on Python 3.9 and 3.13, the Spec Kit contract job on 1.2.1 and the latest release, and `actionlint` pass on
  GitHub-hosted runners (`ubuntu-24.04`, 2026-11-12). Locally the suite runs on Python 3.9.6 with Spec Kit 1.2.4.dev0.
- The copy-in checks in `ci/` have not run in a repository of their own, nor on a pull request from a fork (U14).
- The `unattended` setting is accepted but not used yet.
- A done transition for a subtask created in the same push waits for the next push.
- An invalid `origin/HEAD` setting makes some commands exit 3, also in projects that are only advisory.
- On your own computer the target branch is the one your clone's `origin/HEAD` names. When the home branch (`origin/main`, or
  `origin/master` when there is no `origin/main`) is in strict mode or comment approval, `origin/HEAD` must be exactly that branch (else
  `target-moved`), and when it is not set that branch is used, with or without `CI` set; creating, rewriting or deleting any
  `refs/remotes/origin/*` ref in the local clone is not detected (a local clone is as trusted as the computer). In a CI checkout that
  fetched `origin/main` (the templates use `fetch-depth: 0`) an `origin/HEAD` naming the pull request's target is not refused: trust
  comes from the guarded home branch, and the target (`--base`, or without it the branch `origin/HEAD` names) is judged by the home
  branch's config when that is strict and the target's is not, else by the target's when it is strict and by the pull request's own
  when it is not; when both are strict the target branch's gate settings apply (only the mode is compared); `CI=1` set
  locally looks the same. One
  without `origin/main` and `origin/master` reads `--base` fresh as before.
- Comment approval is experimental: on most sites it can only show tampering, not prevent it.

### Not in 0.1

- Comment approval hardening: the strict cross-check at the done phase in CI, and the approval-request comment with its re-check
  at `skj pr create`, are deferred to 0.2.
- A `skj strict override` command is not in 0.1; a hand-written decision line of kind `override` is honoured instead.
- Single-item write commands (create, update, comment, link or attach one item) and a raw `query` command.
- Attachment download (upload works).
- Company-managed projects, scoped API tokens and Jira Data Center.
- A `removed` state for tasks taken out of `tasks.md`: they are reported, never deleted or moved.
- Run awareness in `skj status` (paused, failed or running workflow runs) and a catch-up option.
- A due date for questions in `--sync`, and the `unattended` settings.
- Pull request hosts other than GitHub.
