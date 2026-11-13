# Versioning

`skj` uses semantic versioning: `MAJOR.MINOR.PATCH`.

## Where the version lives

One version number, written in every place below. They must all match; `tests/test_release_docs.py` fails when the table and
those places disagree.

| Place | Today | Notes |
|---|---|---|
| `pyproject.toml` | `0.1.0` | The package version. A development version carries `.dev0`; the release drops it. |
| `src/skj/__init__.py` | `0.1.0` | What `skj --version` prints. Always the same text as `pyproject.toml`. |
| `extension.yml` | `0.1.0` | The Spec Kit extension. |
| `workflows/jira-lifecycle/workflow.yml` | `0.1.0` | The workflow. |
| `catalog/extensions.json` and `catalog/workflows.json` | `0.1.0` | The catalog entries. |
| `bundle/bundle.yml` | `0.1.0` | The bundle, and the versions it pins for the extension and the workflow. |
| `CHANGELOG.md` | `0.1.0` | The section heading, with the release date (`Unreleased` before a release). |
| `tests/test_package.py` | `0.1.0` | The test pins the exact `skj --version` output, so it changes with `pyproject.toml`. |
| `README.md`, `docs/quick-start.md` and `SECURITY.md` | `0.1.0` | Text that names the current version for readers. |

The extension, the workflow, the bundle and the catalog entries carry the version without the `.dev0` part: the installed
Spec Kit 1.2.4.dev0 accepts only `MAJOR.MINOR.PATCH` as a workflow version, and only semantic versions in a bundle. Spec Kit
would read `0.1.0.dev0` in `extension.yml` (it parses an extension version under PEP 440), but the bundle pins the extension by
its version, and every version a bundle pins must be a semantic version, so `extension.yml` carries no `.dev0` either.

The `skj` tool requirement in `extension.yml` and `catalog/extensions.json` is `>=0.1.0` since the 0.1.0 release. Before a
release it is the development version (for example `>=0.1.0.dev0`), so a `.dev0` package meets its own requirement (under PEP 440,
`0.1.0.dev0` sorts before `0.1.0`); the release drops `.dev0` from the package and the requirement in the same commit. `tests/test_release_docs.py` checks that the package
version is at least the requirement.

The steps that change all of these together are in the [release checklist](docs/release-checklist.md), section 4.

## Before 1.0

While the major version is 0, any minor release (0.1 to 0.2) may change commands, flags, settings or file formats. Each change
that can break a working setup is listed in `CHANGELOG.md` with what to do. A patch release (0.1.0 to 0.1.1) only fixes bugs.

## Spec Kit compatibility

The oldest Spec Kit version `skj` supports is stated in one form, `>=1.2.1`, in `extension.yml` (`requires.speckit_version`), in
the workflow and in both catalog entries. Raising it is a minor release and is named in `CHANGELOG.md`. Spec Kit 1.2.1
and the latest release pass the contract tests on the hosted contract job; locally only 1.2.4.dev0 is installed.

## Removing a flag or a setting

A command flag or a `skj.config.json` setting is not removed without warning. It is first marked deprecated in `CHANGELOG.md` and,
where it can be, `skj` says so when it is used. It is removed in a later minor release at the earliest. Settings that guard trust
(who approves, how, and the pinned site) never change meaning silently: a changed meaning is a new setting.
