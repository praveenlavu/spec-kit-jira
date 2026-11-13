# Contributing

Thank you for helping. Small, careful changes are the most welcome kind. If something here is unclear, ask: a question is
always a good first contribution.

## Set up

You need Python 3.9 or newer and git.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pytest pyyaml
```

Nothing else is installed: the tests find the code in `src/` themselves (`tests/conftest.py`), and you run the CLI from the
source with `PYTHONPATH=src .venv/bin/python -m skj --version`. An editable install needs pip 21.3 or newer: an older pip, below 21.3, such as the
pip 21.2.4 that comes with the macOS system Python 3.9.6, cannot install this project in editable mode. The second line downloads `pytest` and `pyyaml`;
it was not run while writing this guide (no network), only the first line was.

The product code under `src/skj` uses the Python standard library only. `pytest` and `pyyaml` are for the tests and are never
imported by the product code.

## Run the tests

- One file while you work: `.venv/bin/python -m pytest tests/test_push.py -q`.
- The full suite before every commit: `.venv/bin/python -m pytest -q`. It takes about 15 minutes, so run it in the background.
- Then the scrub check: `.venv/bin/python scripts/scrub_check.py`. It reads its list of words that must never appear from the
  untracked `.scrub-denylist`; never print, quote or commit that list.

The tests remove `CI` and `GITHUB_ACTIONS` from each test's environment, so they behave the same on your laptop and in CI. One
thing does read the real `CI` flag: a missing Spec Kit CLI fails the run in CI and is a skip on a laptop.
Every skip in `tests/` is listed in [tests/PORT_NOTES.md](tests/PORT_NOTES.md); a new skip without its row fails the suite.

## Rules every change keeps

- **Standard library only** in `src/skj`, and code that runs on Python 3.9.
- **Preview, human yes, apply.** Every Jira or pull request write shows a preview and does nothing without `--apply`. Its apply
  takes `--expect-plan` with the previewed plan and refuses when the plan changed; the CLI does not require the flag, but the
  agent always passes it (`AGENTS.md`), so a yes covers only the plan it was given.
- **No secrets anywhere.** No token, password or real credential in code, tests, fixtures, logs or chat. `skj` never prints the
  token. A test never reads the keychain and never uses the network (the test setup blocks sockets).
- **Trust settings come from the target branch.** Who approves, how, the unattended settings and the pinned site are read from the
  branch a pull request targets, never from the working tree.
- **Fictional data only.** Sites like `your-site.atlassian.net`, people like `you@example.com`, invented account ids, issues
  like `PROJ-1`. Never real names, emails, sites or account ids.
- **Trademarks.** Name Jira only to refer to Atlassian's products ("for Jira", "with Jira"). The README's not-affiliated notice
  stays word for word and appears only there. No logos.
- **Jira words only.** This kit is for Jira: use its words (issue, story, subtask, project), not the words of other trackers.
- **Plan first.** Write the failing test before the code, keep one change per commit, and name what the commit proves.

## Add a command or an agent prompt

1. A new `skj` verb: add it to the parser in `src/skj/__main__.py`. If it writes to Jira or opens a pull request, it is a dry run
   without `--apply` (`tests/test_x2_no_write_without_apply.py` checks every write verb), and its apply takes `--expect-plan`.
2. List it in the action table of `rules/hitl.md` with who may run it, and in `AGENTS.md` if the agent uses it.
3. Document it in the README or the page of `docs/` it belongs to. The docs tests parse every `skj` command they show.
4. A new agent prompt: add the file under `commands/`, list it in `extension.yml` (`provides.commands`), and update the command
   count in `catalog/extensions.json`. `tests/test_manifest.py` and `tests/test_catalog.py` check both.
5. A new setting: add it to `src/skj/config.py` and to the table in [docs/configuration.md](docs/configuration.md).

## Pull request checklist

- [ ] The failing test came first, and the change makes it pass.
- [ ] The full suite and the scrub check pass on Python 3.9.
- [ ] No new runtime dependency; nothing outside the standard library in `src/skj`.
- [ ] Every new write is a preview without `--apply`.
- [ ] No secret, no real data, only fictional examples.
- [ ] Docs and `CHANGELOG.md` updated for anything a user sees.

## Releases

Releases follow the [versioning rules](VERSIONING.md), the [release checklist](docs/release-checklist.md) and, for the Spec Kit
community catalog, the [catalog submission checklist](docs/catalog-submission.md). Only the owner tags a release.

## Help from AI tools

Contributions written with an AI assistant are welcome. A person must read and review every line before it is proposed, and
the same rules apply: tests first, no secrets, fictional data only.

## Where to ask

There is no public repository yet, so there is no issue tracker. Report a security problem as [SECURITY.md](SECURITY.md) says,
never in public. Please be kind and patient with each other; everyone here is learning too.
