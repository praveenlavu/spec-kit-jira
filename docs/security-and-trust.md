# Security and trust

What `skj` protects, how, and where the limits are. Plain words first; the exact rules for an AI agent are in
[rules/hitl.md](../rules/hitl.md).

## What your token can do depends on your plan

`skj` acts with your own API token, so it can do whatever your account can do. That depends on your Jira plan.

**Free plan** (the only plan `skj` has been tested against), as Atlassian's Free plan page describes it:

- permissions cannot be customised, so you cannot make a smaller account just for `skj`;
- everyone is an admin of every project;
- there is no audit log, so you cannot look up afterwards who changed what;
- so the token can edit and delete anyone's comments, its own included.

**Paid plans** let an admin customise permissions. A separate account for the agent, without the right to edit or delete
comments, could make comment approval tamper-proof. That needs a paid plan, and it is UNVERIFIED: no paid site has been
tried (see U9 and U15 in [unverified](unverified.md)).

`skj doctor` asks Jira what the token may do and shows it in the `edit-all` row.

## Preview, a person's yes, then apply

- Every command that changes Jira or opens a pull request is a preview unless you add `--apply`.
- To bind the apply to exactly what you read, save the preview with `--plan-out .skj/plan-push.md`, then apply with
  `--apply --expect-plan .skj/plan-push.md`. If the change is no longer the same, `skj` refuses, writes nothing and saves the
  new plan beside the old one as `.new`.
- A write is never retried. After a timeout `skj` reads again and looks for its own marker, so a second run does not post twice.
- `skj` never deletes or closes a subtask you removed from `tasks.md`; it only reports it.

## Trust settings come from the target branch

`approvers`, `approval` and `unattended` are read only from the branch a pull request merges into, never from your working
folder. `site` is pinned from there too: if the site in use differs, `skj` refuses to send your token. A strict target branch
judges a pull request with its own gate settings. See [configuration](configuration.md).

On your own computer the target branch is the one your clone's `origin/HEAD` names, and that is a local setting: one
`git remote set-head` changes it. So `skj` compares it with one home branch: `origin/main` when your clone has it, else
`origin/master` (never either one). When that home branch is in strict mode or comment approval, `origin/HEAD` must point at
exactly it. Another branch, even one with the same `approvers`, `approval`, `unattended`, `mode`
and `site`, could carry weaker gate settings, a weaker `strict.test_command`, stack profile or kit lock file, and a local branch
is never accepted; every command that reads the trust settings then refuses with `target-moved`. The fix is
`git remote set-head origin --auto`, which asks the remote. When `origin/HEAD` is not set, the home branch is used in its place, the same with or without `CI` set, so deleting `origin/HEAD` gains nothing. Outside CI a `--base` that cannot
be read falls back to that branch too, and a revision expression as `--base` (`main~1`, `HEAD~1`, `main^`) is refused beside it, as is a full commit id that is
not the tip of a remote branch when it would make the run advisory (an old `main`'s commit would be judged by its own weaker
config); the exact id of a remote branch tip, or any id whose run stays strict, still works.

What this does not cover: creating, rewriting or deleting any `refs/remotes/origin/*` ref in the local clone (for example with
`git update-ref`, which needs no push) is not detected: it changes what the local `skj` reads. That covers mistakes and drift on
an honest developer's computer, where a local clone is as trusted as the computer it is on. It does not cover someone (a person or
an agent) who can run git commands in your clone: they can make the home branch say anything locally. CI is where it counts: CI
fetches the real remote refs, so a pull request is judged there by what the remote holds. A project whose
default branch has another name (not `main` or `master`) gets no cross-check, and one whose default branch has another name but
which also has an `origin/main` in strict mode or comment approval is refused with `target-moved` (a known limit of
0.1).

In CI: a checkout with no `origin/main` and no `origin/master` (`actions/checkout` with its default `fetch-depth: 1`) is unchanged:
it passes `--base`, reads the target branch fresh and fails closed when it cannot read it. The `ci/templates` fetch with
`fetch-depth: 0`, so `origin/main` is there; when it is in strict mode or comment approval it is used where `origin/HEAD` would
be (the target when no `--base` is given, and the stricter side that `skj done` and `skj pr create` compare `--base` with), so a
pull request into another branch is held to it as well. In CI, `origin/HEAD` naming another branch that exists (the templates point
it at the pull request's target) is not refused: the trust settings come from the guarded home branch, and `skj gate`, `skj strict`
`skj stack show`, `skj status`, `skj done` and `skj pr create` take the target from `--base origin/<target>`, or without `--base`
from the branch `origin/HEAD` names, and are judged by the home branch's config when it is strict and the target's is not, else by the
target's config when the target is strict, and by the pull request's own config when it is not. When both the target branch and `origin/main` are strict, the target branch's gate settings apply (only the mode is compared). A pull request into `develop` beside a
strict `origin/main` is judged strict. Outside CI an `origin/HEAD` that drifted is still
refused with `target-moved`. `skj` knows it is in CI only from the `CI` or `GITHUB_ACTIONS` variable, so `CI=1` set on your own
computer looks the same: the check protects against drift and mistakes, not against someone who controls the clone and its
environment.

## Story and comment text is data

Story descriptions and comments are written by people, and anyone who can comment can write anything. `skj` never runs that
text and never obeys it. Where the story title is used in a pull request title or a branch name, it is cleaned first and
passed as a single argument, never through a shell. `skj comments --md`
prints it only when you ask, as untrusted text. Your agent is told the same in [AGENTS.md](../AGENTS.md).

The business rules in a parent issue's description are the same kind of text. `skj rules` copies them into
`dod/Rules_<KEY>.md` escaped, so they never become structure; the checks report rule ids and counts, never rule text. A pull
request cannot switch the rules checks off for itself: with a strict target branch, `rules.enabled` is read from there. The
gate reads the rules in Jira again (GETs only) to find drift; in strict mode a Jira it cannot read blocks. Re-confirming a
changed rule (`skj rules <ref> --confirm`) needs a terminal and a typed code: a guard against an agent doing it by accident,
not a security boundary, and the record says a person read the rule, it does not prove it. A pull request that removes or
changes that record shows up as a note when the gate is given the target branch.

## Comment approval is experimental

By default you approve at your own terminal. Terminal approval remains the recommended mode.

With `"approval": "comment"` on the target branch, the people listed in `approvers` can approve by a comment: a decision
with `/approve D3`, or a step with the `/approve <step> <hash>` line that `skj` prints when it is waiting for one.
A comment counts only if all of these hold:

- its author is a listed approver and a real person, not an app;
- its author is not the token's own account, and not the author of any `skj` comment;
- it was never edited;
- it is not restricted to a group or role;
- the command is its first line, and not quoted;
- it holds no unknown formatting.

When `skj` records an approval it pins the
comment's id and a hash of its text. `skj done` reads the comment again and refuses if it changed or is gone.

The limit, in one sentence (`skj doctor` and strict mode print the same words):

On this site the token can edit or delete comments, including its own, so an approval can be shown to have been tampered with only if it was edited; a deleted comment leaves no trace; comment approval is experimental and cannot be made tamper-proof.

What that means in practice:

- An edited approval is caught. A deleted one simply disappears, and Jira keeps no trace of it.
- The agent's token can delete its own newer comment, for example a re-posted list of decisions, so that an approval
  appears to cover a text the approver never saw. A deleted `skj` posting cannot be detected.
- The apply of `skj done` can never be approved by comment: a person runs it.
- In 0.1 only `skj done` reads the approvals again. `skj strict check`, `skj strict verify` and the CI checks do not yet.
- There is no approval-request comment yet: you tell approvers yourself what to approve.

## Unattended settings

`unattended.assume_after_hours` and `unattended.approval_requests` are accepted, checked and read only from the target branch,
but nothing in 0.1 uses them. Nothing runs on its own without a person: every write still waits for a yes. If a later release
uses them, it stays tamper-evident only, for the reasons above.

## Your token and other secrets

- **macOS:** the token lives in your keychain. You store it yourself with `security add-generic-password ... -w`, which asks for it;
  `skj` only reads it and never writes it.
- **Linux:** `skj setup` asks for it (hidden) and keeps it in `~/.skj/<your site>.json`, readable only by you, in a folder only you
  can open. `skj` refuses the file if anyone else could read it.
- **CI:** the secrets `SKJ_EMAIL` and `SKJ_TOKEN`. When both are set they win over the other two.
- The token is never put on a command line, in a log, in the repository or in chat. Error messages never contain it.
- `skj strict verify` runs your project's tests, so it refuses to run while a login-like variable such as `SKJ_TOKEN` is set.
  A run that `skj approvals sync --resume` resumes keeps the poller's AI agent keys (such as `ANTHROPIC_API_KEY`), so its verify
  step can refuse with `credentials-in-env`. Run the verify in a separate job without credentials, or unset them first (`env -u NAME`).

## Which CI jobs may hold the secrets

Only the job that links a merged pull request, and the optional Jira step of the gate, get `SKJ_EMAIL` and `SKJ_TOKEN`.
The final strict check never does, because it runs the pull request's own test code. Details: [checks for GitHub](ci.md).

## Reporting a problem

See [SECURITY.md](../SECURITY.md). If a token is exposed, revoke it in your Atlassian account first.
