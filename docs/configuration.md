# Configuration

`skj.config.json` sits at the top of your repository. `skj setup` writes it; you edit it by hand after that.
It holds names and settings only, never a secret. Commit it to your main branch.

A minimal file:

```json
{
  "site": "your-site.atlassian.net",
  "project": "PROJ"
}
```

Every other key is optional. A key `skj` does not know (at any level, the top level included), a wrong type or an out-of-range
value makes `skj` refuse the file with a message that names the key (never its value; a key `skj` does not know is named by its
position, such as `top level entry #3`, followed by the valid keys); `skj doctor` shows it in the `config` row.

## Which copy is read

Most keys are read from the file in your working folder. Some are **trust keys**: they decide who may approve, where your
token is sent, or how strictly a pull request is judged. Those are read from the **target branch** (the branch a pull
request merges into: `origin/HEAD` on your computer, `--base` in CI), never from your working folder. A branch that edits
them changes nothing until the edit is reviewed and merged. On your computer `origin/HEAD` is a local setting; when
`origin/main` (or `origin/master`) is in strict mode or comment approval, `skj` refuses (`target-moved`) unless
`origin/HEAD` is exactly that branch, and uses that branch when `origin/HEAD` is not set. See
[security and trust](security-and-trust.md#trust-settings-come-from-the-target-branch) for what this does and does not cover.

- `approvers`, `approval` and `unattended` are always read from the target branch.
- `site` is pinned from the target branch: if the two differ, `skj` refuses to send your token.
- When the target branch's file says `"mode": "strict"`, that file judges the run of `skj gate`, `skj strict` and `skj done`,
  including its `gate` and `strict` settings, so a pull request cannot lower its own bar.

"Who may change it" below says who should make the change. The tool cannot see who edited a file: your code review can.

## Every key

| Key | Meaning | Default | Who may change it |
|---|---|---|---|
| `site` | Your Jira Cloud site, such as `your-site.atlassian.net`. Lowercase, no `https://`. | (required) | A maintainer, on the main branch first (pinned) |
| `project` | The project key, such as `PROJ`: a capital letter, then 1 to 9 capital letters, digits or `_`. One project per repository. | (required) | A maintainer |
| `story_type` | The name of the story issue type. In 0.1 it is only checked against `rules.types`. | `"Story"` | Anyone, through review |
| `subtask_type` | The name of the subtask issue type `skj push` creates. | `"Subtask"` | Anyone, through review |
| `ac_heading` | The heading of the description section that holds the acceptance criteria. | `"Acceptance criteria"` | Anyone, through review |
| `transitions.active` | The name of the transition that starts work. Accepted, but nothing uses it in 0.1. | `null` | Anyone, through review |
| `transitions.done` | The name of the one transition that closes a story. `null`: `skj` picks the only transition into a done status, and refuses when there are none, several, or one with a screen, a condition or required fields. | `null` | Anyone, through review |
| `branch_prefix` | The start of branch names: `feature/` gives `feature/PROJ-123-add-login`. Letters, digits, `.`, `_`, `-` and `/`; a prefix that starts with `refs/`, `heads/`, `tags/` or `remotes/` is refused, because git would confuse the branch with another ref. | `"feature/"` | Anyone, through review |
| `labels` | A list of up to 20 labels. Accepted, but nothing uses it in 0.1 (subtasks always get the label `skj`). | `[]` | Anyone, through review |
| `gate.tests` | Extra test-file patterns: a map from a file pattern to a regular expression that counts as an assertion, for example `{"tests/**/*.py": "assert "}`. | built-in patterns | Anyone, through review |
| `gate.block` | Gate checks that fail the gate instead of only warning: `ac_traceability`, `assertion`, `ac_drift`, `open_questions`, `commits_without_story`, `smart_commit_commands`, `open_decisions`, and with `rules.enabled` `rule_coverage`, `rule_drift`. | `[]` | Anyone, through review (strict mode adds a fixed minimum) |
| `review_block_on` | Review finding levels that stop `skj pr create`: `blocker`, `major`, `minor`. | `[]` | Anyone, through review (strict mode adds `blocker`) |
| `stack.kit` | The Spec Kit add-on your stack uses, or empty. Read by the agent's commands. | `""` | Anyone, through review |
| `stack.phases` | Which command runs each phase (specify, plan, tasks, implement). Read by the agent's commands. | `{}` | Anyone, through review |
| `mode` | `"advisory"` (the gate reports) or `"strict"` (tests first, every check enforced; see the [glossary](glossary.md)). | `"advisory"` | A maintainer, on the main branch |
| `strict.test_command` | The command strict mode runs to prove each criterion's test failed first and then passed. Up to 500 characters. | `null` | A person, through `skj stack approve --apply` |
| `strict.test_timeout` | Seconds that test command may run, 1 to 3600. | `300` | Anyone, through review |
| `rules.enabled` | Business rules. `skj rules <ref>` pulls the rules into `dod/Rules_<KEY>.md` whatever this says. `true` makes `skj dod` write the `Rules from:` line, `skj gate` report `rule_coverage` (local: every approved rule cited by an AC row with a test, confirmed with `skj rules <ref> --confirm`) and `rule_drift` (GETs of the parent issues; not checked with `--offline`), `skj status` show the `rules` stage, and strict mode check the rules at `pr` and `done`; in strict mode both gate checks are in the floor, so an unreachable Jira blocks, and so does `--offline`: the copy-in pull request check runs the gate offline unless the repository variable `SKJ_GATE_ONLINE` is `true` ([ci/README.md](../ci/README.md)), so a strict project with rules on needs it. Read from the target branch's config when that one is strict. | `false` | Anyone, through review |
| `rules.types` | The issue types that carry business rules: 1 to 10 names, never the story type. `skj rules` follows the story's parent at most 3 levels up and reads the description of these types only. | `["Epic"]` | Anyone, through review |
| `approvers` | Up to 20 Jira accountIds of the people whose comments may approve. Never the token's own account. | `[]` | A maintainer, on the main branch only (trust key) |
| `approval` | `"terminal"` (you approve at your own terminal: recommended) or `"comment"` (experimental; see [security and trust](security-and-trust.md)). | `"terminal"` | A maintainer, on the main branch only (trust key) |
| `unattended.assume_after_hours` | Hours, 1 to 720. Accepted and checked, but nothing uses it in 0.1. | `null` | A maintainer, on the main branch only (trust key) |
| `unattended.approval_requests` | `true` or `false`. Accepted and checked, but nothing uses it in 0.1 (the approval-request comment is not built). | `false` | A maintainer, on the main branch only (trust key) |

The keys an AI agent must never change without you are listed in [the agent's rules](../rules/hitl.md).

## How to write business rules

With `rules.enabled`, a product owner writes the rules in the description of the parent issue (an Epic by default), under a
heading or a line that says exactly `Business rules`. Each rule starts with `Rule <n>:` (a whole number from 1 to 9999, each
number once), then its statement. Its fields follow, each `Label: value`:

```text
Business rules
Rule 1: A draft older than 30 days is archived.
Status: approved
Why: storage
Example: day 31
Owner: PO
Rule 2: Drafts are private. Status: approved. Why: privacy. Example: another user sees nothing. Owner: PO
```

- A field may start its own line (rule 1), or follow on the same line after a sentence end (`.`, `;`, `!` or `?`, as in rule 2).
  Both are read the same way.
- `Status:` is `approved`, `draft` or `retired`. Only an approved rule binds: the gate checks that an acceptance criterion with a
  test cites it. A rule with no Status is a draft (a warning); a Status `skj` cannot read, such as one with no sentence end before
  it (`Rule 3: text Status: approved`), is an error in the `Problems` of `dod/Rules_<KEY>.md`, and the rule stays a draft.
- Each rule starts its own line or paragraph. A second rule label anywhere after a rule's `Rule <n>:` (on the same line or a later
  line of that rule, after any character that is not a letter or digit) is the error `bad-rule-line`, and the rule it is in stays a
  draft. A label is `Rule <n>`, `Rules <n>`, `Rule no. <n>`, `Rule #<n>`, `Rule (<x>)`, `Rule <word>` (`Rule two`, `Rule II`,
  `Rule A`), `R<n>`, `BR-<n>` or `BR-<KEY>-<n>`, maybe after `Business`, followed by a colon or a dash (`–`, `—`, or ` - `);
  case, fullwidth letters, look-alike letters and zero-width characters do not hide it. This fails closed on prose of the same shape:
  `Why: GDPR rules apply: always`, `Example: see BR-LOY-1-2: privacy`, `Example: rule A: B`, `Why: Q4 R2: launch`. The fix: put each
  rule on its own line, or reword the text after `Why:` (or `Example:`, `Owner:`) so it does not look like a second rule label
  (`Why: GDPR applies here, always`). A line that starts with such a label is the error `unparsed-rule-text` instead.
- `Why:`, `Example:` and `Owner:` are expected; a missing one is a warning.
