# What is not proven yet

`skj` 0.1 is built and tested against a simulated Jira and was checked live on one real Jira Cloud Free site (a team-managed project,
2026-11). The items below are things we have **not** been able to check for real, with the reason; a settled item keeps its row and says so. Until an item is checked, treat the behaviour as unknown. The ids are fixed: other pages refer to them.
For what the comment approval limits mean for you, see [security and trust](security-and-trust.md).

| Id | What is not proven | Why |
|---|---|---|
| U1 | Company-managed projects: their hierarchy, workflows, transition screens and a required resolution. | The test site has none; 0.1 supports team-managed projects only. |
| U2 | Comment approval by another person, and what Jira shows when someone else edits that comment. | Only one user was available; only an edit by the same user was seen. |
| U3 | Comments restricted to a role or group. | The one attempt failed on a role name; no restricted comment was made. |
| U4 | A real "too many requests" (429) or "unavailable" (503) answer with a retry time, a real timeout and a real 403 answer; whether the per-issue write limit applies to API tokens; why two rate-limit headers disagree (200 against 100). | Never provoked; the test answers are made up. |
| U5 | How the Jira editor stores a comment line that a person starts with `/`. | Settled (2026-11): an editor-typed `/approve D1` comment is stored as plain text. |
| U6 | Downloading attachments, and opening an attached ledger in the browser, on a real site. | Upload worked on a real site (`skj done` attached the ledger); downloading is not in 0.1, and the attached content was not opened in the browser. |
| U7 | Scoped API tokens and the gateway address they use. | Left for a later version; the cause of an early login failure is a guess. |
| U8 | The development panel, the GitHub app for Jira and smart-commit behaviour. | Needs an admin to install the app. |
| U9 | Behaviour on a paid plan with a restricted account for the agent, and so any tamper-proof claim. | The test site is on the Free plan. |
| U10 | `space =` instead of `project =` in Jira searches. | Not tried. |
| U11 | That a new comment's "created" and "updated" times are always equal. | Seen only once or twice. |
| U12 | How Jira rewrites formatted text beyond the recorded cases, and the largest comment it accepts. | Only one recorded example. |
| U13 | The 10-character limit on project keys, and custom key formats. | From a community source only; the pattern is one setting in the code. |
| U14 | The copy-in checks in ci/ running in a repository of their own on GitHub, the `skj` gate job on a hosted pull request, a pull request from a fork in strict mode with business rules on, the scrub job on a hosted run, and that each pinned action commit is the one its version tag names. | This repository's own tests (Python 3.9 and 3.13), `actionlint` and the contract job (Spec Kit 1.2.1 and the latest release) pass on GitHub-hosted runners; the scrub job had no denylist secret on that run, and no consumer repository or fork pull request has been tried. The runs show only that the jobs ran with the pinned actions. |
| U15 | What a member who is not an admin may do on the Free plan. | Needs a second user. |
| U16 | Keeping the token on Linux and Windows. | Only macOS was used. |
| U17 | Whether the name `skj` is free on PyPI and Homebrew, and whether Atlassian's trademark guidance covers an unaffiliated open-source project. | Not checked and not asked yet. |
| U18 | How long Jira takes to show a change just made, beyond one trial. | One trial only. |
| U19 | That a real Jira search returns each issue's key when fields are asked for. | Not checked on a real site; the simulated Jira assumes it. |
| U20 | Whether a real Jira search can leave out the "last page" flag. `skj` copes with both and stops after a fixed page limit. | Not checked on a real site. |
| U21 | Whether Jira acts on smart-commit commands in merge commits, with the key on an earlier line, in other letter cases or with other digit forms. | `skj` warns about all of them; what Jira really does is not checked. |
| U22 | How Jira returns `fields.parent` above an Epic, for deeper or company-managed hierarchies, and for a parent in another project or one the token cannot see. | Only a Story's Epic parent was seen; the deeper chains `skj rules` walks are made up for the tests. |
| U23 | The ADF of a product owner's Business rules section in a real Epic description, typed or pasted from Word. | The test descriptions are written by hand. |
| U24 | That a real Jira returns the same description for an unchanged parent issue on every read, so an unchanged Epic is no rule drift. | Only the simulated Jira was read; its descriptions are written by hand. |
| U25 | What a real Jira answers when the gate's identity cannot see the story or one of its parent issues. | `skj gate` treats any error or odd answer as `rules-unreachable` (it blocks in strict mode); the real answers are not seen. |
| U26 | That the deny rules of `docs/claude-permissions.json` with a `*` in the middle refuse what they name. | The real workflow, driven by Claude Code, ran end to end on a story (2026-11); whether each deny rule refuses what it names is not recorded. |
| U27 | Proxy settings. | `skj` ignores `HTTPS_PROXY` and the other proxy variables; behind a mandatory proxy it fails with a network error. Not tried. |
| U28 | `skj done` after a real merge, with a decision a human validated. | `skj done` closed stories on the real site, but not after a real merge with a validated decision. |
