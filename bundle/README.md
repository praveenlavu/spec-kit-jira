# Spec Kit lifecycle for Jira: the bundle

This bundle is for a developer who takes Jira stories to Done through Spec Kit, with a human sign-off before every Jira or pull request write.
One install adds both parts of the add-on, each pinned to the same version, 0.1.0.

## What it installs

- The `jira-lifecycle` extension: 13 slash commands and 2 hooks.
- The `jira-lifecycle` workflow: a story to an open pull request, with a human approval gate at each step that writes.

It also needs two programs on your machine, which a bundle cannot install: `skj` (the command-line tool the commands and the workflow call) and `git`.
Spec Kit 1.2.1 or newer is required.

## Required catalogs

The two parts are found through two catalogs that this repository keeps. Add them to your Spec Kit project before you install the bundle:

    specify extension catalog add https://raw.githubusercontent.com/praveenlavu/spec-kit-jira/main/catalog/extensions.json --name jira --install-allowed
    specify workflow catalog add https://raw.githubusercontent.com/praveenlavu/spec-kit-jira/main/catalog/workflows.json --name jira

Without them `specify bundle validate` and `specify bundle install` cannot find the two parts.

## Install

1. Install the `skj` tool:

       pipx install "git+https://github.com/praveenlavu/spec-kit-jira@v0.1.0"

2. Add the two catalogs above, in the project where you use Spec Kit.
3. Download the bundle file and install it:

       curl -L -o jira-lifecycle-bundle-0.1.0.zip https://github.com/praveenlavu/spec-kit-jira/releases/download/v0.1.0/jira-lifecycle-bundle-0.1.0.zip
       specify bundle install ./jira-lifecycle-bundle-0.1.0.zip

4. Run one story:

       specify workflow run jira-lifecycle --input issue_number=<n>

Setup, the first run and what each gate asks are in the [main README](https://github.com/praveenlavu/spec-kit-jira/blob/main/README.md).

## For maintainers: building the release files

`bundle.yml` is the manifest. Check it offline with `specify bundle validate --path bundle --offline`. `specify bundle build --path bundle --output <dir>`
zips `bundle.yml` and this README as `jira-lifecycle-<version>.zip`, the same name as the extension zip below, so build the two into different
directories. The bundle file is attached to the release under the name `jira-lifecycle-bundle-<version>.zip`.

The release zips are generated, never committed. Build them into a directory outside the repository:

    .venv/bin/python scripts/build_bundle.py --out <dir outside the repo>

That writes two zips, both built from the tracked files only (`git ls-files`; an untracked file never ships, and any symlink on a
selected path refuses the build):

- `jira-lifecycle-<version>.zip`: the extension, exactly the tracked files `.extensionignore` admits (`extension.yml`, `commands/`,
  `rules/`, `workflows/`, `LICENSE`), with `extension.yml` at the archive root.
- `jira-lifecycle-workflow.zip`: the workflow, with `workflow.yml` at the archive root (Spec Kit installs a workflow archive only
  with `workflow.yml` at its root or in exactly one top-level directory, so the repository archive cannot serve the workflow).

Entries are sorted, with fixed timestamps, mode and create_system, so two builds on the same platform and zlib give the same bytes.
(The deflate output can differ with another zlib version; the fixed create_system keeps the bytes the same on Windows and POSIX.)
`bundle/*.zip` is git-ignored in case a build lands here anyway.

The URLs of the release:

- `https://github.com/praveenlavu/spec-kit-jira`: the repository, the manifest's `repository` and `homepage`, and every URL in `catalog/extensions.json` and
  `catalog/workflows.json`.
- `v0.1.0`: the release tag.
- `https://github.com/praveenlavu/spec-kit-jira/archive/refs/tags/v0.1.0.zip`: the extension `download_url` (a repository archive installs: one top-level directory,
  `.extensionignore` applied).
- `https://github.com/praveenlavu/spec-kit-jira/releases/download/v0.1.0/jira-lifecycle-workflow.zip`: the workflow `url`, the second zip attached as a release asset.
- `https://github.com/praveenlavu/spec-kit-jira/blob/v0.1.0/CHANGELOG.md`: the extension `changelog`.

`tests/test_catalog.py` pins them in `PLACEHOLDERS` (the name the release checklist uses for them).
