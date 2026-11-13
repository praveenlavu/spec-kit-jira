# Release checklist

The steps to turn this repository into the 0.1.0 release, in order. Tick each box only with evidence (a command and its last
line of output). Today the repository is ready to tag but not released.

## Where things stand

- The tests on Python 3.9 and 3.13, the Spec Kit contract job on 1.2.1 and the latest release, and `actionlint` pass on
  GitHub-hosted runners (`ubuntu-24.04`, 2026-11-12).
- Hosted CI green (X7) is not complete: the scrub job has not passed yet (its `SCRUB_DENYLIST` secret was stored after the run),
  and the checks in ci/ have not run in a repository of their own, nor on a pull request from a fork.
- O2 is decided: the public repository is `https://github.com/praveenlavu/spec-kit-jira`, the release tag `v0.1.0`. Every placeholder but `<COMMIT-SHA>` is
  replaced (REL1).
- The full suite passes locally on Python 3.9.6, and on both Pythons on the hosted runners (above).
- The live checks LV1 to LV4 ran on a real Jira Cloud Free site (2026-11), on a team-managed project. The comment times of LV3
  (U11) stay open. The README says what was and was not run live, on purpose.

## 1. Before anything else

- [ ] Scrub with the owner's denylist (task Z1): `python3 scripts/scrub_check.py --history --require-denylist`, with the denylist
      supplied by the owner in the `SCRUB_DENYLIST` variable or the untracked `.scrub-denylist` file. It must exit 0. Exit 2 means no denylist was given: the scrub is then not done, not passed.
- [ ] The full suite on Python 3.9: `.venv/bin/python -m pytest -q` (about 15 minutes). Record the last line.
- [x] The full suite on Python 3.13: it passes on the hosted runners (above and section 5).
- [x] Decided by the owner (T10): the internal build files are out of the tree (the spike notes and scripts and the build
      plan; they stay on the owner's disk only). `CLAUDE.md` and `tests/PORT_NOTES.md` stay on purpose. The early local commits
      still hold internal files, the client handle and local paths, so publication is from a clean export of one commit (the first published repository is private), and the local development
      history stays local and is never pushed. Every `--history` scrub in this checklist runs on what will be pushed: run
      `python3 scripts/scrub_check.py --history --require-denylist` in that export, before the first push. It must exit 0.
- [ ] Every skip is listed in [tests/PORT_NOTES.md](../tests/PORT_NOTES.md) section 4 (the suite checks this).

## 2. Live checks, by the owner only

Each needs the owner's own terminal, keychain and consent; an agent never runs them.

- [x] LV1: the read-only commands against a scratch story (2026-11-12: `doctor`, `get`, `comments` and the `branch` dry run
      worked; the token and the email never reached the output).
- [x] LV2: the writes (push, questions, pull request link, done with an attachment) on a scratch story (2026-11-12: each
      wrote once and a second apply wrote nothing).
- [x] LV3: how the editor stores a comment line that starts with `/`, and whether a new comment's times match (2026-11: an
      editor-typed `/approve D1` comment is stored as plain text; the times stay open, U11).
- [x] LV4: one real workflow run, from the story to an open pull request (2026-11: it ran end to end with Claude Code, a rejected
      gate left Jira unchanged, and a second and third run added no duplicate comment or subtask).
- [x] Only after LV3 and LV4 pass: change the README's live-check sentence and its "not yet run" line and `NOT_LIVE` in
      `tests/test_docs.py` together, and update [what is not proven yet](unverified.md) (done 2026-11).

## 3. The publication decision and the placeholders

- [x] The owner decides O2 and confirms the real repository URL (2026-11-13: `https://github.com/praveenlavu/spec-kit-jira`, tag `v0.1.0`).
- [ ] When the repository goes public, the owner turns on GitHub private vulnerability reporting in its security settings, so
      the channel `SECURITY.md` names works. Check it: the Security tab offers Report a vulnerability.
- [ ] Move `SCRUB_DENYLIST` to an environment secret limited to `main` when the repository goes public: the history scan gets
      its own job with that environment, so a pushed branch with an edited `tests.yml` cannot read the denylist.
- [x] Done in REL1 (`tests/test_manifest.py` and `tests/test_catalog.py` pass): `extension.yml` held `<REPO-URL>` as its repository and homepage, like the catalog, and both source manifests
      (`extension.yml`, `workflows/jira-lifecycle/workflow.yml`) name the owner's account as the author. `tests/test_manifest.py`
      pins both. Decide the real values, then change the
      manifests, their pins in `tests/test_manifest.py`, the catalog entries, `tests/test_catalog.py` and the README placeholders
      together, in one commit.
- [x] Decide whether `_placeholder_manifest` in `scripts/build_bundle.py` stays (it wrote `<REPO-URL>` into the bundled
      `extension.yml`): removed in REL1, as it would have undone the real URL. `tests/test_catalog.py` checks that the bundled
      `extension.yml` is the source file byte for byte.
- [ ] Replace every placeholder below. Done in REL1 for all but `<COMMIT-SHA>` (`test_rel1_no_release_placeholder_is_left` in
      `tests/test_release_docs.py`); pin that one with `scripts/pin_release_sha.py` once the release commit exists. The lists the tests read are `PLACEHOLDERS` in `tests/test_catalog.py`, `PLACEHOLDERS` and
      `SECURITY_PLACEHOLDERS` in `tests/test_docs.py`, and `INSTALL_PLACEHOLDERS` in `tests/test_speckit_contract.py`.

| Placeholder | Where it is | What replaces it |
|---|---|---|
| `<REPO-URL>` | `catalog/extensions.json`, `catalog/workflows.json`, `extension.yml` (source and bundled), README.md, `docs/quick-start.md`, `bundle/README.md`, `SECURITY.md`, the install line of `ci/templates/`, `ci/README.md`, `docs/ci.md`, `workflows/jira-lifecycle/README.md` | the real repository URL from the O2 decision |
| `<TAG>` | the catalog URLs, README.md, `docs/quick-start.md`, `bundle/README.md`, `workflows/jira-lifecycle/README.md` | the release tag, for example `v0.1.0` |
| `<REPO-URL>/archive/refs/tags/<TAG>.zip` | the extension `download_url` in `catalog/extensions.json` | the repository archive of the tag (or a release asset of the extension zip, see section 4) |
| `<REPO-URL>/releases/download/<TAG>/jira-lifecycle-workflow.zip` | the workflow `url` in `catalog/workflows.json` | the release asset built by `scripts/build_bundle.py` |
| `<REPO-URL>/blob/<TAG>/CHANGELOG.md` | the extension `changelog` in `catalog/extensions.json` | the tagged `CHANGELOG.md` in the real repository |
| `<REPO-URL>/security/advisories/new` | the reporting channel in `SECURITY.md` | the real repository's private vulnerability reporting page |
| `<COMMIT-SHA>` | the install line of `ci/templates/`, `ci/README.md`, `docs/ci.md` | the full 40-character commit of the release: `python3 scripts/pin_release_sha.py <commit>` writes it into every install line and prints the files it changed |

The action pins are already full commits (`ACTION_PINS` in `tests/test_speckit_contract.py`): `actions/checkout` (v4.2.2) and
`actions/setup-python` (v5.6.0). They could not be checked against GitHub offline (UNVERIFIED): before the release confirm that
each commit is the one its version tag points to, and keep the version comment after each commit.

- [x] Drop the "Notes for the maintainer (remove before release)" section of `ci/README.md` (REL1).
- [ ] Keep the rules line in `ci/README.md` step 3 and say it in the release notes: A strict project with business rules on
      (`rules.enabled`) must set the repository variable `SKJ_GATE_ONLINE` to `true` for the copy-in pull request check. The
      template runs the gate `--offline` by default, so rule drift reads "drift not checked" and strict mode blocks every pull request.

## 4. Build the bundle and prove it installs

- [x] The release commit, in one commit, so the suite stays green (see [the versioning rules](../VERSIONING.md)):
  - drop `.dev0` from `pyproject.toml` and `src/skj/__init__.py`, and from the pinned `skj --version` output in
    `tests/test_package.py`;
  - raise the `skj` requirement in `extension.yml` and `catalog/extensions.json` from `>=0.1.0.dev0` to `>=0.1.0` (and its pin
    in `tests/test_manifest.py`);
  - change `Unreleased` in `CHANGELOG.md` to the release date, and `CHANGELOG_DATE` in `tests/test_release_docs.py` with it;
  - update the version text in `README.md` (its status section), `docs/quick-start.md` (the example output) and `SECURITY.md`;
  - update the `Today` column of `VERSIONING.md` and its `Unreleased` note to the released version;
  - remove the no-tag sentence at the top of this page and set `TAG_CREATED` in `tests/test_release_docs.py` to true, so a checkout
    that has the owner's tag stays green.
- [x] Build: `.venv/bin/python scripts/build_bundle.py --out <dir outside the repo>` (a directory inside the repository is refused). It writes `jira-lifecycle-0.1.0.zip` and
      `jira-lifecycle-workflow.zip`.
- [x] Record the sha256 of each zip (`shasum -a 256 <zip>`). Recommended: add it to the catalog entry as `sha256`, only once the release asset exists (the entries hold no `sha256` field
      today: a placeholder digest would make Spec Kit refuse the archive, and `tests/test_catalog.py` pins the field list). The installed
      Spec Kit 1.2.4.dev0 reads that field for both entries and refuses an archive whose digest differs. Whether Spec Kit 1.2.1
      accepts the field is UNVERIFIED. The digest must be of the exact bytes the URL serves: whether a generated repository
      archive keeps the same bytes over time is UNVERIFIED, so for the extension either check it after the tag or serve the built
      extension zip as a release asset instead.
- [x] Offline install proof in a scratch project with a private home folder: `specify workflow add <local workflow zip>`, and for
      the extension unzip the built extension zip and run `specify extension add --dev <unzipped dir>`. Record the output. An
      install through the catalog can only be tried after the tag, when its URLs exist.
- [x] The bundle: `specify bundle validate --path bundle --offline` passes, and `bundle/bundle.yml` pins the release version
      (`tests/test_catalog.py` checks it). `specify bundle build --path bundle --output <dir>` names its zip like the extension
      zip, so build it into another directory.

## 5. Hosted CI

What ran (2026-11-12, GitHub-hosted runners, `ubuntu-24.04`):

- [x] `tests.yml`: the tests on Python 3.9 and on Python 3.13 pass, and the `actionlint` job passes.
- [x] `speckit-contract.yml` (started by hand): Spec Kit 1.2.1 (installed from the pinned commit) and the latest release, each on
      Python 3.9 and 3.13, pass.

What is left:

- [ ] The scrub job ("Scan for secrets and private words") on its next run, now that the `SCRUB_DENYLIST` secret is stored. It
      failed before the secret existed; it has not passed yet.
- [ ] The copy-in checks in `ci/templates/` in a separate repository of their own, including the `skj` gate job on a pull request.
- [ ] A pull request from a fork, in strict mode with business rules on.
- [ ] The action pins (section 3) match their version tags on GitHub. The runs only show the jobs ran with them.
- [ ] The Python 3.9 job is slow on the hosted runner (most of the job time is spread over all tests; no single slow test). Its
      `timeout-minutes` in `tests.yml` stays at 90 for now; bring it down to about 60 once more runs agree.
- [ ] When all of the above are green: remove the X7 sentence at the top of this page, update the hosted CI sentence in the
      README status, narrow U14 in [what is not proven yet](unverified.md), and set `HOSTED_CI_GREEN` in
      `tests/test_release_docs.py` to true, in one commit.

## 6. Tag, catalog, announcement

- [ ] The owner creates the tag. No agent and no script creates it.
- [ ] The owner publishes the release with `jira-lifecycle-workflow.zip` as an asset.
- [ ] The three catalog filings (extension, workflow, bundle), following the
      [catalog submission checklist](catalog-submission.md).
- [ ] Announce with no claim beyond the README: no "tested on a real site" beyond what the README lists, no "production ready", no "tamper-proof".
