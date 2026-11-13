# Why skj

Once the spec and the plan are agreed, an AI agent can write the code and the tests. What it should not do on its own is change
the story your team reads. `skj` keeps those two apart: your agent does the grunt work, and every change to Jira waits for you.

## The problem

Give an agent a Jira token and it can create subtasks, post comments and close a story the moment it decides to. You find out
afterwards, by reading the story. The questions that needed a person (is this criterion right, is this assumption sound,
is this story really done) were answered by the agent, or not asked at all.

## What skj does instead

- **It plans, then waits.** Every command that writes to Jira prints a plan and stops. Nothing is written until you add
  `--apply`. Save the plan with `--plan-out` and apply with `--expect-plan`, and the apply refuses (exit 2, nothing written) if
  the plan changed since you read it.
- **It keeps a checklist per story.** `skj dod seed` copies the story's acceptance criteria, word for word (hidden and control
  characters removed), into `dod/DoD_<KEY>.md`. Each criterion gets the test that proves it, and `skj gate` reports any criterion without one. The
  [glossary](glossary.md) explains the words.
- **It asks before it builds.** `skj questions --post` and `skj decisions --post` put the open questions and the decisions on the
  story, after your yes, so the team can answer them before the code is written.
- **It stops when in doubt.** A changed plan, a missing comment approval or settings it cannot trust end the command with nothing
  written; in CI, settings that cannot be trusted fail the check (exit 3).
- **It never closes a story on its own.** The workflow ends at the open pull request. The story is closed by `skj done`, after the
  merge, with the checklist attached, and only after your yes.
- **It needs nothing but Python.** Python 3.9 or newer, standard library only, so there is no third-party package to install
  or audit.

## How it compares

These are kinds of tools, not named products, and the point is the shape of the work, not a score.

| Instead of | What changes with `skj` |
|---|---|
| Doing it by hand | The subtasks, the pull request link and the finished checklist go on the story for you, each after a preview. |
| An agent that writes to Jira with no stop | You see each change before it is made, and the apply is bound to the plan you read. |
| A general Jira command line tool, or an MCP server (a set of tools an agent can call) | Those do what they are asked, when asked. `skj` makes a plan for one step of a story and waits for a person. |
| Its sibling kit for another tracker | The same shape and the same rules, for Jira Cloud. Each kit is a separate, independent project. |

## What it is not, yet

`skj` 0.1 supports team-managed projects on Jira Cloud only. It is built and tested against a simulated Jira and was checked live on
one real Jira Cloud Free site, on a team-managed project (2026-11). The full list of what is not in 0.1 is in the README's Status section, and
what is still unproven is in [unverified](unverified.md). The trust model, with its limits, is in
[security and trust](security-and-trust.md).
