# Catalog submission checklist

What must be true, and what to prepare, before this extension, its workflow and its bundle are offered to the Spec Kit
community catalogs. There are three filings: the extension, the workflow and the bundle. This is a checklist, not a submission.

This repository has NOT been submitted to the Spec Kit community catalog.

## Where the community catalog lives

Read from the installed Spec Kit 1.2.4.dev0 source: the CLI reads the community catalogs from the Spec Kit repository's
`extensions/catalog.community.json`, `workflows/catalog.community.json` and `bundles/catalog.community.json`, on its main branch. It registers all three as
discovery only: a listing there lets people find the kit with `specify extension search` and `specify extension info`, but
`specify extension add jira-lifecycle` and `specify workflow add jira-lifecycle` both refuse to install from a discovery-only
catalog. To install, a user either runs
`specify extension add jira-lifecycle --from <archive-url>` (for the workflow,
`specify workflow add jira-lifecycle --from <workflow-url>`) or adds a catalog they control, with install allowed, in
`.specify/extension-catalogs.yml` (`.specify/workflow-catalogs.yml` for the workflow). Whether Spec Kit 1.2.1 behaves the same is
UNVERIFIED.

## What must be true first

- [ ] Every step of the [release checklist](release-checklist.md) is done: the live checks, the publication decision, real URLs
      in place of every placeholder, hosted CI green, and the owner's tag.
- [ ] The repository is public and holds the README, the MIT `LICENSE` and the tagged release with its workflow zip.
- [ ] The version is the same everywhere it lives (see [the versioning rules](../VERSIONING.md)).
- [ ] The extension description is under 100 characters (`tests/test_manifest.py` checks this).
- [ ] The README states what is not proven, and names Jira only to refer to the product.

## What to prepare

- [ ] The extension entry: a copy of the `jira-lifecycle` entry of `catalog/extensions.json`, with the real URLs, and a `sha256`
      if the release checklist decided to add one.
- [ ] The workflow entry: a copy of the `jira-lifecycle` entry of `catalog/workflows.json`, with the real release asset URL.
- [ ] The bundle entry: there is no local copy. The installed Spec Kit reads `id`, `name`, `version`, `role`, `description`,
      `author`, `license`, `download_url`, `requires.speckit_version`, `provides`, `repository`, `tags`, `verified` and an
      optional `sha256`. Take the values from `bundle/bundle.yml`; `download_url` is the tagged `bundle/bundle.yml` or the zip
      `specify bundle build` makes. The bundle installs the extension and the workflow through their own entries, so those two
      are filed first. `specify bundle validate --path bundle --offline` must pass.
- [ ] The README link, the license name (MIT), the version (0.1.0) and the tag.
- [ ] The pull request is opened from the owner's personal GitHub account, and only by the owner.

## What is not known yet

The steps below cannot be checked offline, so each is UNVERIFIED until someone reads the current Spec Kit contributing rules:

- UNVERIFIED: whether a community entry is added by a pull request that edits those three files, or in another way.
- UNVERIFIED: which fields a community entry must carry, and whether the catalog keeps `verified`, `downloads` and `stars` as
  this kit's local entries do.
- UNVERIFIED: whether the extension, the workflow and the bundle are submitted together or one at a time.
- UNVERIFIED: whether the entry must point at a tag, a release asset or a repository archive.
- UNVERIFIED: whether the name `skj` is free on the package indexes, and whether the trademark guidance covers an unaffiliated
  open-source project (U17 in [what is not proven yet](unverified.md)).
