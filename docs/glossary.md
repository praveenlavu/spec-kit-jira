# Glossary

Plain meanings of the words used in these docs.

Atlassian has renamed some words: a Jira project is now also called a space, and an issue a work item. These docs use the older words, project and issue (a story is one kind of issue).

| Word | What it means |
|---|---|
| **Issue** | One item in a Jira project. Stories and subtasks are kinds of issue. |
| **Story** | One piece of work, such as "Let users log in". `PROJ-123` is its key. |
| **Key** | The issue's short name: the project letters, a dash and a number, like `PROJ-123`. Always capital letters. |
| **Subtask** | A small piece of a story. `skj push` creates one per task in your `tasks.md`. |
| **Acceptance criteria** | The lines in a story that say what "done" means. `skj` reads them from the section titled "Acceptance criteria". |
| **Spec Kit** | A tool that guides an AI agent from an idea to a spec, a plan and a task list. `skj` plugs into it. |
| **Definition of Done (DoD)** | What must be true before a story counts as finished. |
| **Checklist (the "ledger")** | The file `dod/DoD_PROJ-123.md`, one per story. It lists each criterion, the test that proves it, and the evidence. |
| **Decisions file** | `dod/Decisions_PROJ-123.md`: the decisions and assumptions made on the way, each a line `D1`, `D2` and so on. |
| **Gate** | An automatic check before merge that says what is missing, such as a criterion with no test. `skj gate` runs it. |
| **Human approval gate** | A point where the work stops until a person says yes. Every change to Jira passes one. |
| **Preview, then apply** | A command that would change Jira first shows the change and does nothing. You run it again with `--apply` to make the change. |
| **Plan file** | A preview saved with `--plan-out`. Applying with `--expect-plan` and that file refuses if the change is no longer the one you read. |
| **Strict mode** | An optional mode (`"mode": "strict"`): every criterion needs a test that failed first and then passed, and the checks block instead of warn. |
| **Target branch** | The branch a pull request merges into, usually `main`. Trust settings are read from there. |
| **Trust keys** | The settings that decide who may approve and where the token goes. A branch cannot change them. |
| **Approver** | A person allowed to approve a step by a Jira comment, in the experimental comment approval (see [security and trust](security-and-trust.md)). |
| **Tamper-evident** | A change can be noticed afterwards. Weaker than tamper-proof, where a change cannot be made at all. |
| **Marker** | A tag like `[skj:pr ab12cd34]` at the start of the comments `skj` writes, so it can find them again and never posts twice. |
| **Smart commit** | A Jira feature where a commit message such as `PROJ-123 #done` changes a story. `skj` never writes these. |
| **Business rule** | A rule a product owner writes in a parent issue's description (an Epic by default), as `Rule 1: ...` under a `Business rules` heading, with `Status:`, `Why:`, `Example:` and `Owner:` fields ([how to write them](configuration.md#how-to-write-business-rules)). `skj rules` copies them into `dod/Rules_PROJ-12.md`, each tagged `BR-PROJ-12-1`; with `rules.enabled` the gate checks that every approved one is cited by a tested criterion and still matches Jira. |
| **Stack profile** | `skj.stack.json`: the languages, test command and conventions of your repository, approved by a person. |
| **Kit** | A Spec Kit add-on. `skj.kits.lock.json` records the ones a person reviewed. |
| **Workflow** | A Spec Kit run, `jira-lifecycle`, that goes through every step and pauses at each human approval gate. |
| **Team-managed project** | A Jira project set up by its own team rather than by a site admin. The only kind tested so far. |
| **API token** | A long password for tools. You create it in your Atlassian account and can revoke it at any time. |
| **Keychain** | The password store on a Mac. On macOS `skj` reads your token from there. |
| **CI** | Checks that run by themselves on every pull request, such as GitHub Actions. |
