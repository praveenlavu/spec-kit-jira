# The `jira-lifecycle` workflow

A Spec Kit workflow that takes one story from Jira to an open pull request: spec, plan, tasks, push, implement, gate, review, PR.
Every step that changes Jira or the PR is a preview, a human gate, then an apply of exactly what the gate showed. The run ends at
the open PR; the story is closed after the merge with `skj done`.

It is part of "Spec Kit lifecycle for Jira". Install, setup, trust model and the trademark notice are in the
root README of the kit's repository (`https://github.com/praveenlavu/spec-kit-jira`).

## Run it

```
specify workflow run jira-lifecycle --input issue_number=<n>
```

`<n>` is the number after the project key (12 for `SPK-12`); `skj` adds the project key from `skj.config.json`. It needs Spec Kit
`>=1.2.1`. Start the run from a shell that holds no credential variables: `skj strict verify` refuses while one is set.

An agent must never pass a `verdict_*` input: a verdict given with `--input` approves that gate with no human seeing it.

## Agent permissions (headless)

Spec Kit starts the agent for each command step without a terminal (Claude Code: `claude -p`), so a command that needs your
approval is refused, the agent stops, and without this list the run would carry on with nothing done. Give it the one tested list in
`docs/claude-permissions.json` of the kit's repository (`https://github.com/praveenlavu/spec-kit-jira/blob/v0.1.0/docs/claude-permissions.json`): it allows the read and local-write commands the steps run and denies every Jira or PR write
(those run only in the workflow's own apply steps, after a gate). Either set it for the shell you start the run from, which works at once:

```bash
export SPECKIT_INTEGRATION_CLAUDE_EXTRA_ARGS="--permission-mode acceptEdits --allowedTools 'Bash(skj get:*),Bash(skj comments:*),Bash(skj dod seed:*),Bash(skj questions:*),Bash(skj decisions:*),Bash(skj rules:*),Bash(skj gate:*),Bash(skj status:*),Bash(skj stack show:*),Bash(skj stack detect:*),Bash(skj strict check:*),Bash(skj strict mode:*),Bash(skj kits verify:*),Bash(skj calibrate:*),Bash(skj branch:*),Bash(git add:*),Bash(git commit:*),Bash(git diff:*),Bash(git log:*),Bash(git status:*),Bash(git rev-parse:*),Bash(git symbolic-ref:*),Bash(git switch:*),Bash(.specify/scripts/bash/check-prerequisites.sh:*),Bash(.specify/scripts/bash/setup-plan.sh:*),Bash(.specify/scripts/bash/setup-tasks.sh:*)' --disallowedTools 'Bash(skj questions * --apply*),Bash(skj questions --apply*),Bash(skj decisions * --post*),Bash(skj decisions --post*),Bash(skj decisions * --resolve*),Bash(skj decisions --resolve*),Bash(git push:*)'"
```

or, after you have run Claude Code in the project once and accepted its trust dialog (until then it ignores the project's allow
rules), copy that file to `.claude/settings.json`. Add your project's test command too (for example `Bash(npm test:*)`), or
`implement` cannot run the tests. If a step still did not finish, the check after it stops the run and names the step.

## Inputs

| Input | Type | What it is |
|---|---|---|
| `issue_number` | number | Required. The story number; the only value the workflow ever puts into a shell command. |
| `integration` | string | The agent integration to run the command steps with (default `auto`: the one the project was set up with). |
| `review_model` | string | The model for the review step only (default empty: the integration's default). A different model from the implementer is recommended. |
| `verdict_review_spec` | string | Verdict for gate `review-spec`, set by `skj approvals sync --resume`. Leave empty. |
| `verdict_approve_questions` | string | Verdict for gate `approve-questions`, set by `skj approvals sync --resume`. Leave empty. |
| `verdict_approve_plan_questions` | string | Verdict for gate `approve-plan-questions`, set by `skj approvals sync --resume`. Leave empty. |
| `verdict_wait_po_answers` | string | Verdict for gate `wait-po-answers`, set by `skj approvals sync --resume`. Leave empty. |
| `verdict_approve_answers_questions` | string | Verdict for gate `approve-answers-questions`, set by `skj approvals sync --resume`. Leave empty. |
| `verdict_review_plan` | string | Verdict for gate `review-plan`, set by `skj approvals sync --resume`. Leave empty. |
| `verdict_approve_push` | string | Verdict for gate `approve-push`, set by `skj approvals sync --resume`. Leave empty. |
| `verdict_approve_decisions` | string | Verdict for gate `approve-decisions`, set by `skj approvals sync --resume`. Leave empty. |
| `verdict_approve_sync` | string | Verdict for gate `approve-sync`, set by `skj approvals sync --resume`. Leave empty. |
| `verdict_review_review` | string | Verdict for gate `review-review`, set by `skj approvals sync --resume`. Leave empty. |
| `verdict_approve_pr` | string | Verdict for gate `approve-pr`, set by `skj approvals sync --resume`. Leave empty. |
| `verdict_wait_po_validation` | string | Verdict for gate `wait-po-validation`, set by `skj approvals sync --resume`. Leave empty. |

A verdict input takes `approve`, `reject` or empty. Empty means the gate asks at a terminal, or pauses when there is none
(comment approval, experimental).

## Steps

In run order. A `loop` step runs its next three steps once (preview, gate, apply); the `*-applied` step after it stops the run if
the apply refused because the plan changed after approval (start a new run then). Every gate aborts the run on reject.

| Step id | Kind | What it does |
|---|---|---|
| `pull` | command | Turns the story into a spec and the ledger `dod/DoD_<KEY>.md`, and commits both for the spec review. |
| `pull-done` | shell | Stops the run unless the `pull` step left its product, committed (the agent runs headless: a refused command ends it silently). |
| `stack-check` | shell | Strict mode: stops unless the stack profile is approved and covers every detected root. |
| `review-spec` | gate | You review the spec, the open questions and the "Needs human decision" list, as committed at HEAD. |
| `check-review-spec` | shell | Checks the `review-spec` approval (comment mode; a no-op in terminal mode). |
| `questions-loop` | loop | One pass of the next three steps. |
| `questions-preview` | shell | Writes the plan for posting the open questions to the story. |
| `approve-questions` | gate | You approve posting those questions to the story, as the plan file shows. |
| `questions-apply` | shell | Posts exactly the approved plan. |
| `questions-applied` | shell | Stops the run if the questions plan changed after approval. |
| `plan` | command | Writes the implementation plan. |
| `plan-done` | shell | Stops the run unless the `plan` step left its product, committed (the agent runs headless: a refused command ends it silently). |
| `plan-questions-loop` | loop | One pass of the next three steps. |
| `plan-questions-preview` | shell | Writes the plan for posting the questions the plan raised. |
| `approve-plan-questions` | gate | You approve posting the questions the plan raised to the story. |
| `plan-questions-apply` | shell | Posts exactly the approved plan. |
| `plan-questions-applied` | shell | Stops the run if that plan changed after approval. |
| `wait-po-answers` | gate | You wait for the product owner to answer the posted questions; approving without answers accepts the plan's stated assumptions. |
| `check-wait-po-answers` | shell | Checks the `wait-po-answers` approval (comment mode). |
| `sync-po-answers` | shell | Reads the answers from the story comments into the ledger (comment mode). |
| `plan-with-answers` | command | Revises the plan with the answers; unanswered questions are recorded as assumptions. |
| `plan-with-answers-done` | shell | Stops the run unless the `plan` step left its product, committed (the agent runs headless: a refused command ends it silently). |
| `answers-questions-loop` | loop | One pass of the next three steps. |
| `answers-questions-preview` | shell | Writes the plan for posting the questions the revised plan raised. |
| `approve-answers-questions` | gate | You approve posting the questions the revised plan raised; they stay open until answered. |
| `answers-questions-apply` | shell | Posts exactly the approved plan. |
| `answers-questions-applied` | shell | Stops the run if that plan changed after approval. |
| `review-plan` | gate | You review the plan, the spec, the open questions and the decision list, as committed at HEAD. |
| `check-review-plan` | shell | Checks the `review-plan` approval (comment mode; refused while the ledger, spec or plan has uncommitted changes: commit them first). |
| `tasks` | command | Makes sure a standard `tasks.md` exists for the push. |
| `tasks-done` | shell | Stops the run unless the `tasks` step left its product, committed (the agent runs headless: a refused command ends it silently). |
| `push-loop` | loop | One pass of the next three steps. |
| `push-preview` | shell | Writes the plan for creating one subtask per task under the story. |
| `approve-push` | gate | You approve creating those subtasks and firing the transitions the plan shows. |
| `push-apply` | shell | Creates and transitions exactly what was approved. |
| `push-applied` | shell | Stops the run if the push plan changed after approval. |
| `strict-check-implement` | shell | Strict mode: refuses to implement unless spec, plan, tasks, subtasks and answers are in order. |
| `implement` | command | Implements the tasks test first and records decisions in `dod/Decisions_<KEY>.md`. |
| `implement-done` | shell | Stops the run unless the `implement` step left its product, committed (the agent runs headless: a refused command ends it silently). |
| `decisions-loop` | loop | One pass of the next three steps. |
| `decisions-preview` | shell | Writes the plan for posting the recorded decisions as one comment. |
| `approve-decisions` | gate | You approve posting the development decisions and assumptions to the story. |
| `decisions-apply` | shell | Posts exactly the approved plan. |
| `decisions-applied` | shell | Stops the run if the decisions plan changed after approval. |
| `strict-verify` | shell | Strict mode: runs the tests at each red and green commit and records the result in the ledger. |
| `sync-loop` | loop | One pass of the next three steps. |
| `sync-preview` | shell | Writes the plan for the subtask transitions of the tasks ticked during implement. |
| `approve-sync` | gate | You approve those subtask transitions. |
| `sync-apply` | shell | Transitions exactly what was approved. |
| `sync-applied` | shell | Stops the run if the sync plan changed after approval. |
| `gate` | command | Runs `skj gate` and records the result in the ledger. |
| `gate-done` | shell | Stops the run unless the `gate` step left its product, committed (the agent runs headless: a refused command ends it silently). |
| `gate-check` | shell | Runs `skj gate` on the changed files; stops on a blocking finding (exit 1) or an invalid `skj.config.json` (exit 3). |
| `review` | command | Reviews the branch diff and logs the verified findings in the ledger (with `review_model` if set). |
| `review-done` | shell | Stops the run unless the review left a current, well-formed review block, committed (a review that did not run stops it here). |
| `review-review` | gate | You review the findings in the ledger before the PR; reject if any task is incomplete. |
| `check-review-review` | shell | Checks the `review-review` approval (comment mode). |
| `pr-loop` | loop | One pass of the next three steps. |
| `pr-preview` | shell | Writes the plan for pushing the branch and opening the pull request. |
| `approve-pr` | gate | You approve pushing the head branch and opening the pull request. |
| `pr-apply` | shell | Pushes and opens exactly what was approved. |
| `pr-applied` | shell | Stops the run if the PR plan changed after approval. |
| `wait-po-validation` | gate | You wait for the product owner to validate or reject the posted decisions; this is the last gate. |
| `sync-po-validation` | shell | Records the approvers' `/approve D<n>` and `/reject D<n>` comments (comment mode). |
| `check-wait-po-validation` | shell | Checks the `wait-po-validation` approval (comment mode). |

## Not in 0.1

The ADO kit's `refresh-context`, `pr-clear` and `pr-prepare` steps are not in 0.1: `skj comments` takes a full key and writes no context file (the review command reads `skj comments <KEY> --md` itself), and `skj pr create` builds the PR title and body from the ledger, so there are no prepared PR files to clear or prepare.

## License

MIT, see [LICENSE](LICENSE).
