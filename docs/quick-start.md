# Quick start

From nothing to your first story in five steps. Nothing in these steps changes your Jira site.

> The output below is an **example**: your text may differ a little. Names such as `your-site.atlassian.net`,
> `PROJ`, `PROJ-123` and `you@example.com` are made up. Use your own.

## Before you start

- Python 3.9 or newer (`python3 --version` shows it) and `git` 2.24 or newer.
- A Jira Cloud account and a **team-managed** project you can see (the only kind tested so far).
- A story in that project whose description has a section titled "Acceptance criteria".
- For the AI agent part: [Spec Kit](https://github.com/github/spec-kit) 1.2.1 or newer (the `specify` command).
- For the workflow with Claude Code: the agent runs headless, so it needs the permission list below (see "The workflow runs the agent headless").

## Step 1. Install

```bash
pipx install "git+https://github.com/praveenlavu/spec-kit-jira@v0.1.0"
skj --version
```

Example output:

```text
skj 0.1.0
```

No `pipx`? Run `python3 -m pip install --user pipx`, then `python3 -m pipx ensurepath`, and open a new terminal.
From a local copy of the repository, `python3 -m pip install -e .` works too (it needs pip 22 or newer).

## Step 2. Make an API token and store it

An API token is a long password made for tools like this one. You can revoke it at any time.

1. Sign in to your Atlassian account settings, open **Security**, then **API tokens**, and create a token named `skj`.
2. Copy it. You only see it once.

*These pages may look different on your account.*

**On macOS**, store it in the keychain. Run this in your own terminal, with your email and your site:

```bash
security add-generic-password -a 'you@example.com' -s 'skj:your-site.atlassian.net' -w
```

The `-w` at the end, with nothing after it, makes the computer ask for the token and hide what you type.
That keeps the token out of your shell history. `skj` never writes to the keychain itself.

**On Linux:** skip this. In step 3, `skj setup` asks for your email and token (the token is hidden as you type) and keeps them in
`~/.skj/your-site.atlassian.net.json`, a file only you can read. Windows is covered in [compatibility](compatibility.md).

## Step 3. Connect your repository

Inside your code repository:

```bash
skj setup --site your-site.atlassian.net --project PROJ
```

Example output on macOS:

```text
Wrote skj.config.json for your-site.atlassian.net, project PROJ. Commit it to the default branch: the site is pinned from there.
Store your Jira identity in the Keychain: run this, with your Atlassian email in place of <your-email>; it prompts for the API token (https://id.atlassian.com/manage-profile/security/api-tokens):
  security add-generic-password -a <your-email> -s skj:your-site.atlassian.net -w
Then run `skj doctor`.
```

If you did step 2 already, ignore the keychain lines. `skj setup` never overwrites an existing `skj.config.json`.
The file holds names only, never a secret. Commit it to your main branch: `skj` reads the site from there, so a branch
cannot quietly point your token at another site. Every setting is explained in [configuration](configuration.md).

## Step 4. Check the setup

```bash
skj doctor
```

Example output (one line per check):

```text
ok      git
ok      specify
ok      config
warn    config-target  the target branch has no skj.config.json yet, so CI judges a PR by its own: merge the setup
ok      site-pin       the target branch has no skj.config.json yet, so there is no site to pin: merge the setup there
ok      token
ok      identity       accountId 5b10ac8d...
ok      project-type
ok      approvers      not used: approval is "terminal" on the target branch
warn    edit-all       the token has EDIT_ALL_COMMENTS, ... on this project; it does not matter while approval is "terminal", ...
```

`ok` is good. `warn` is worth a look but does not block. `fail` needs fixing, and the line says how. `unknown` means the check
could not tell. The `config-target` warning goes away once `skj.config.json` is on your main branch. The `edit-all` warning is
normal on a Free plan: see [security and trust](security-and-trust.md). Doctor makes three read-only requests to Jira.
If `config-target` fails with `target-moved`, your clone's `origin/HEAD` does not point at the main branch: run
`git remote set-head origin --auto` (it asks the remote) and run `skj doctor` again.

## Step 5. Try your first story

```bash
skj get PROJ-123
skj dod seed PROJ-123 --spec-dir specs/001-my-feature
skj status PROJ-123
```

The first command prints a short summary (key, status, type, parent, number of subtasks). The second writes
`dod/DoD_PROJ-123.md`, the checklist, with one row per acceptance criterion; `--spec-dir` is the Spec Kit folder of this
feature, so later commands find its `tasks.md` (a relative path inside the repository: letters, digits and `. _ / -` only). The third prints the stage the story is at and the one command that moves it on.

Once `tasks.md` exists, preview the subtasks it would create. This changes nothing:

```bash
skj push PROJ-123
```

Read the preview. If it is right, run the same command with `--apply` added, and the subtasks appear in Jira.

## Optional: the AI agent add-on

The repository also holds a Spec Kit add-on (13 commands named `speckit.jira-lifecycle.*`) and a workflow, `jira-lifecycle`.
Until there is a release, install the add-on from a local copy of the repository:

```bash
specify extension add --dev <path-to-your-copy>
```

Your agent then follows [AGENTS.md](../AGENTS.md). It shows you each preview and waits for your yes; only you start a workflow run.

### The workflow runs the agent headless

Spec Kit starts the agent for each command step without a terminal (Claude Code: `claude -p`), so a command that needs your
approval is refused, the agent stops, and without this list the run would carry on with nothing done. Give it the one tested list in
[`claude-permissions.json`](claude-permissions.json): it allows the read and local-write commands the steps run and denies every Jira or PR write
(those run only in the workflow's own apply steps, after a gate). Either set it for the shell you start the run from, which works at once:

```bash
export SPECKIT_INTEGRATION_CLAUDE_EXTRA_ARGS="--permission-mode acceptEdits --allowedTools 'Bash(skj get:*),Bash(skj comments:*),Bash(skj dod seed:*),Bash(skj questions:*),Bash(skj decisions:*),Bash(skj rules:*),Bash(skj gate:*),Bash(skj status:*),Bash(skj stack show:*),Bash(skj stack detect:*),Bash(skj strict check:*),Bash(skj strict mode:*),Bash(skj kits verify:*),Bash(skj calibrate:*),Bash(skj branch:*),Bash(git add:*),Bash(git commit:*),Bash(git diff:*),Bash(git log:*),Bash(git status:*),Bash(git rev-parse:*),Bash(git symbolic-ref:*),Bash(git switch:*),Bash(.specify/scripts/bash/check-prerequisites.sh:*),Bash(.specify/scripts/bash/setup-plan.sh:*),Bash(.specify/scripts/bash/setup-tasks.sh:*)' --disallowedTools 'Bash(skj questions * --apply*),Bash(skj questions --apply*),Bash(skj decisions * --post*),Bash(skj decisions --post*),Bash(skj decisions * --resolve*),Bash(skj decisions --resolve*),Bash(git push:*)'"
```

or, after you have run Claude Code in the project once and accepted its trust dialog (until then it ignores the project's allow
rules), copy that file to `.claude/settings.json`. Add your project's test command too (for example `Bash(npm test:*)`), or
`implement` cannot run the tests. If a step still did not finish, the check after it (a step named `<step>-done`) stops the run. Spec Kit
prints only "Shell command exited with code 1" for it, so read the reason with `skj dod check 12 --after pull` (your story number and the step's name), or in the run's `.specify/workflows/runs/<run id>/state.json`.

## If something goes wrong

Run `skj doctor` first. Each line names a check and how to fix it.

- **`config` row: "no skj.config.json found"**: run `skj setup` inside your repository.
- **`token` row: "no Jira identity for this site"**: store the token (step 2) with the same site name.
- **`token` row: "Jira did not accept the credentials (HTTP 401)"**: wrong email or token. Doctor does not try again on purpose: repeated failed logins can lock you out until you sign in once in a browser. Other commands say `Jira did not accept the credentials (HTTP 404, authentication failed ...)`: Jira Cloud answers a failed login on most paths with a 404, and `skj` reads its login-failed header so it is never shown as "not found".
- **`project-type` row: "project PROJ not found"**: check `project` in `skj.config.json`, and that your account can see that project.
- **`site-pin` row: "the site in skj.config.json differs from the site pinned on the target branch"**: a safety check. Change the site on your main branch first.
- **`config-target` row: "the target branch has no skj.config.json yet"**: normal until you merge `skj.config.json` to your main branch.
- **`project-type` row: "company-managed (classic) project"**: a warning: this version is not tested on that kind of project.

## Next

- Look up a word: [glossary](glossary.md).
- See every command: `skj --help`.
- Add automatic checks to your pull requests: [checks for GitHub](ci.md).
