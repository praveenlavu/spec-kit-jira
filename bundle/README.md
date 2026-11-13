# bundle/

`bundle.yml` is the Spec Kit bundle `jira-lifecycle`: installing it adds the `jira-lifecycle` extension and the `jira-lifecycle`
workflow, each pinned to this release's version. It needs the `skj` CLI and git. Check it offline with
`specify bundle validate --path bundle --offline`. `specify bundle build --path bundle --output <dir>` zips `bundle.yml` and this
README as `jira-lifecycle-<version>.zip`, the same name as the extension zip below, so build the two into different directories.

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
