# Checks for GitHub

`skj` comes with four ready-made GitHub Actions checks. They are **copy-in templates**: they live in `ci/templates/` and
never run in this repository. You copy the ones you want into your own `.github/workflows/` folder.

- `check-branch-name.yml`: the branch name carries a story key of your project.
- `check-pull-request.yml`: runs `skj gate` on the pull request (with no Jira login unless you turn it on).
- `final-check.yml`: strict mode only; re-runs each criterion's test at its red and green commit. It never gets a Jira login.
- `link-pull-request.yml`: after a merge, runs `skj pr link` once to add a link and a comment to the story.

The install line installs `skj` from a full commit of the release, not a tag (a tag can be moved). The actions are
already pinned to full commits (`actions/checkout` v4.2.2, `actions/setup-python` v5.6.0); not yet checked on GitHub.
Only the link job, and the optional Jira step of the gate, use the `SKJ_EMAIL` and `SKJ_TOKEN` secrets.

How to copy them, which secrets and variables to add, and why each one is built the way it is: [ci/README.md](../ci/README.md).

A strict project with business rules on (`rules.enabled`) must set the repository variable `SKJ_GATE_ONLINE` to `true` for the copy-in pull request check. The check runs the gate `--offline` by default, so rule drift reads "drift not checked" and strict mode blocks every pull request.

In CI, a pull request into another branch (`develop`, `release/1.x`) beside a `origin/main` in strict mode or comment approval is
judged by `origin/main`'s config when `origin/main` is strict and the target is not, else by the target's config when the
target is strict, and by the pull request's own config when it is not. When both the target branch and `origin/main` are strict, the target branch's gate settings apply (only the mode is compared). The target is `--base`, or without `--base` the branch `origin/HEAD`
names. Its trust settings and site come from `origin/main` (`skj done` and `skj pr create` also check the target's approvers);
the templates' `origin/HEAD` pointing at the target is not refused there (on your own computer it is, with `target-moved`). `skj`
tells CI apart only by the `CI` or `GITHUB_ACTIONS` variable, so `CI=1` set on a computer looks the same: the check guards against
drift and mistakes, not against whoever controls the clone and its environment. Details: [ci/README.md](../ci/README.md) and
[security and trust](security-and-trust.md#trust-settings-come-from-the-target-branch).

Pull requests from forks get no secrets from GitHub: the link step skips the link and the Jira part of the gate skips the comparison. A workflow runs as it is written in the pull request's own branch, so protect `.github/` with a CODEOWNERS file and "require review from code owners" (details in [ci/README.md](../ci/README.md)).

This repository's own tests run with `.github/workflows/tests.yml` and `.github/workflows/speckit-contract.yml`. You do not need
those files. `tests.yml` runs the tests on Python 3.9 and 3.13 (on `ubuntu-24.04`), builds the wheel from a clean copy and checks
that it carries its data files, scans for secrets (on a push to `main` also the whole history, with the `SCRUB_DENYLIST` secret),
and lints the workflows and templates with `actionlint` 1.7.7. The tests on Python 3.9 and 3.13, `actionlint` and the contract job on Spec Kit 1.2.1 and the latest release pass on GitHub-hosted runners; the scrub job has not passed yet, and the copy-in checks in `ci/` have not run in a repository of their own (see U14 in [unverified](unverified.md)).
