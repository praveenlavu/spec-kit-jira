# Compatibility

What `skj` 0.1 runs on, and what has really been tried.

| Part | Supported | Tried here |
|---|---|---|
| Python | 3.9 or newer, standard library only | 3.9.6 locally; the tests pass on Python 3.9 and 3.13 on GitHub-hosted runners (`ubuntu-24.04`). |
| git | 2.24 or newer | yes |
| Spec Kit | 1.2.1 or newer | 1.2.4.dev0 locally; the contract tests pass on 1.2.1 and the latest release on the hosted contract job. |
| Jira | Jira Cloud only | a simulated Jira, and one real Jira Cloud Free site (about 600 live checks and one workflow run end to end, 2026-11) |
| Jira project type | team-managed projects | a simulated team-managed project and one real one; company-managed not tried (U1) |
| Pull requests | GitHub, through the `gh` tool | the `gh` tool is used only by `skj pr create` |

## Works with Jira Cloud only

`skj` talks to Jira Cloud sites, whose address ends in `.atlassian.net`. Jira Data Center and Server are not supported. Scoped API tokens are not
supported in 0.1; use a classic API token (U7).

## Team-managed projects only

0.1 is built and tested for team-managed projects. Company-managed projects are not tested and not claimed (U1):
`skj doctor` warns about them in the `project-type` row. The code does not guess: a transition with a screen, a condition or
required fields is refused, and you name the transition in `transitions.done` (see [configuration](configuration.md)).

## Where the token is kept

| System | Store | Notes |
|---|---|---|
| macOS | the login keychain, item `skj:<site>` | You store it with `security add-generic-password ... -w`; `skj` only reads it. The exact `security` output is checked strictly and was read from a real keychain item once (one macOS version). |
| Linux | `~/.skj/<site>.json`: a file only you can read, in a folder only you can open | `skj setup` writes it. Not tried on a real Linux machine (U16). |
| Windows | no native support | The file store checks Unix file owners and modes, so it refuses on native Windows. Use WSL, which counts as Linux (U16). |
| CI | `SKJ_EMAIL` and `SKJ_TOKEN` | When both are set they win over the stores above. |

`skj` writes the file in one step (never half-written) and refuses a file that is a link or that others can read, and a folder that is a link, that someone else owns or that others can open.
