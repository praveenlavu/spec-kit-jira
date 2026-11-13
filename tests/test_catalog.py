"""Z2: the local catalog entries (catalog/extensions.json, catalog/workflows.json) and the bundle script (scripts/build_bundle.py).

The entry shape is what the installed Spec Kit 1.2.4.dev0 reads (checked by reading its source, offline): a catalog is a JSON object with
`schema_version` "1.0" and an `extensions` (or `workflows`) object keyed by id (`ExtensionCatalog._validate_catalog_payload`, the workflow
catalog's `_get_merged_workflows`); `specify extension info` reads name, version, id, description, author, license, requires, provides,
tags, downloads, stars, repository, homepage, documentation, download_url and verified; a workflow entry is installed from its `url`,
an archive with workflow.yml at its root or in exactly one top-level directory (so not the repository archive).
REL1: every URL is the public repository's (REPO_URL), at the release tag (RELEASE_TAG) where it names a release."""
import importlib.util
import os
import subprocess
import json
import re
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"
BUNDLE = ROOT / "bundle"
EXT = yaml.safe_load((ROOT / "extension.yml").read_text())
WF = yaml.safe_load((ROOT / "workflows" / "jira-lifecycle" / "workflow.yml").read_text())

# REL1: the one source of the public repository and the release tag (the owner's decision); the other test files import them.
REPO_URL = "https://github.com/praveenlavu/spec-kit-jira"
RELEASE_TAG = "v0.1.0"
TOKENS = (REPO_URL, RELEASE_TAG)
ARCHIVE = "%s/archive/refs/tags/%s.zip" % TOKENS  # the extension: a repository archive installs (one top dir, .extensionignore applied)
WORKFLOW_ASSET = "%s/releases/download/%s/jira-lifecycle-workflow.zip" % TOKENS  # built by scripts/build_bundle.py
CHANGELOG = "%s/blob/%s/CHANGELOG.md" % TOKENS  # T7: the extension entry's `changelog` (`specify extension info` prints it)
PLACEHOLDERS = TOKENS + (ARCHIVE, WORKFLOW_ASSET, CHANGELOG)  # the values that replaced the release placeholders (the name is the checklist's)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
URL_FIELDS = ("repository", "homepage", "documentation", "download_url", "url", "changelog")


def _load(name):
    path = CATALOG / name
    text = path.read_text()
    data = json.loads(text)
    assert text == json.dumps(data, indent=2, sort_keys=True) + "\n", f"{name}: not in canonical form (indent 2, sorted keys)"
    return data


def _entry(name, kind, entry_id):
    data = _load(name)
    assert data["schema_version"] == "1.0"
    assert isinstance(data[kind], dict) and list(data[kind]) == [entry_id]
    entry = data[kind][entry_id]
    assert entry["id"] == entry_id
    return entry


def test_extension_entry_matches_the_manifest():
    e = _entry("extensions.json", "extensions", "jira-lifecycle")
    m = EXT["extension"]
    assert set(e) == {"author", "changelog", "description", "documentation", "download_url", "downloads", "homepage", "id", "license",
                      "name", "provides", "repository", "requires", "stars", "tags", "verified", "version"}
    assert (e["id"], e["name"], e["version"], e["description"], e["license"]) == (m["id"], m["name"], m["version"], m["description"], "MIT")
    assert e["author"] == m["author"] == WF["workflow"]["author"]  # T7-AC2: the manifests are the source
    assert e["changelog"] == CHANGELOG
    assert e["requires"] == {"speckit_version": ">=1.2.1", "tools": EXT["requires"]["tools"]}
    assert e["tags"] == EXT["tags"]
    assert (e["verified"], e["downloads"], e["stars"]) == (False, 0, 0)


def test_provides_counts_are_thirteen_commands_and_the_manifest_hooks():
    e = _entry("extensions.json", "extensions", "jira-lifecycle")
    files = sorted(p.name for p in (ROOT / "commands").glob("*.md"))
    assert e["provides"] == {"commands": 13, "hooks": len(EXT["hooks"])}
    assert e["provides"]["commands"] == len(EXT["provides"]["commands"]) == len(files) == 13
    assert sorted(Path(c["file"]).name for c in EXT["provides"]["commands"]) == files


def test_workflow_entry_matches_the_workflow():
    e = _entry("workflows.json", "workflows", "jira-lifecycle")
    w = WF["workflow"]
    assert set(e) == {"author", "description", "id", "license", "name", "repository", "requires", "tags", "url", "version"}  # ADO's shape
    assert (e["repository"], e["license"]) == (REPO_URL, EXT["extension"]["license"])
    assert (e["id"], e["name"], e["version"], e["description"]) == (w["id"], w["name"], w["version"], w["description"])
    assert e["author"] == w["author"] == EXT["extension"]["author"]  # T7-AC2
    # T7-AC1: a `--version`-pinned `specify workflow add` refuses a download whose requires differ from the catalog's
    assert e["requires"] == WF["requires"] and e["requires"]["speckit_version"] == ">=1.2.1"
    assert e["tags"] == EXT["tags"]  # workflow.yml has no tags: the extension's, which `specify workflow info`/`search` read


def test_every_url_is_the_public_repository_at_the_tag():
    ext = _entry("extensions.json", "extensions", "jira-lifecycle")
    wf = _entry("workflows.json", "workflows", "jira-lifecycle")
    assert (ext["repository"], ext["homepage"], ext["documentation"], ext["download_url"]) == (REPO_URL,) * 3 + (ARCHIVE,)
    assert ext["changelog"] == CHANGELOG
    assert wf["url"] == WORKFLOW_ASSET
    for entry in (ext, wf):
        for field in URL_FIELDS:
            if field in entry:
                assert entry[field] == REPO_URL or entry[field].startswith(REPO_URL + "/"), field
                assert "<" not in entry[field], field
    files = sorted(CATALOG.glob("*")) + sorted(p for p in BUNDLE.glob("*") if p.suffix != ".zip")  # zips: git-ignored
    assert [p.name for p in files] == ["extensions.json", "workflows.json", "README.md", "bundle.yml"]
    for p in files:
        text = p.read_text().replace('"author": "%s"' % EXT["extension"]["author"], "")  # the author is the manifests' (T7)
        text = text.replace('author: "%s"' % EXT["extension"]["author"], "")  # bundle/bundle.yml (T6)
        text = text.replace(REPO_URL, "")  # the one URL allowed: the public repository's
        for bad in ("http://", "https://", "github.com", "praveenlavu", "www."):
            assert bad not in text, f"{p.name}: {bad}"
        assert not EMAIL.search(text), p.name
        assert not re.findall(r"<[A-Z][A-Z-]*>", text), p.name  # no placeholder left (REL1)
    both = (CATALOG / "extensions.json").read_text() + (CATALOG / "workflows.json").read_text()
    assert all(ph in both for ph in PLACEHOLDERS)
    readme = (BUNDLE / "README.md").read_text()
    assert all(ph in readme for ph in PLACEHOLDERS) and "jira-lifecycle-workflow.zip" in readme


def test_trademark_rule_jira_is_only_referential():
    for e in (_entry("extensions.json", "extensions", "jira-lifecycle"), _entry("workflows.json", "workflows", "jira-lifecycle")):
        for field in ("id", "name", "description", "author"):
            assert "atlassian" not in e[field].lower(), field
        assert not e["name"].lower().startswith("jira") and e["name"].endswith(" for Jira")  # "for Jira" is referential (X9)
        assert "Jira" not in e["author"]


def _bundle_module():
    spec = importlib.util.spec_from_file_location("build_bundle", str(ROOT / "scripts" / "build_bundle.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bundle_is_deterministic_and_holds_only_the_extension(tmp_path):
    mod = _bundle_module()
    first, wf1 = mod.build(ROOT, tmp_path / "a")
    second, wf2 = mod.build(ROOT, tmp_path / "b")
    assert (first.name, wf1.name) == ("jira-lifecycle-0.1.0.zip", "jira-lifecycle-workflow.zip")
    assert first.read_bytes() == second.read_bytes() and wf1.read_bytes() == wf2.read_bytes()
    with zipfile.ZipFile(wf1) as z:  # workflow.yml at the archive root, as `specify workflow add` requires
        wf_files = ("CHANGELOG.md", "LICENSE", "README.md", "workflow.yml")  # T2: the folder's docs ship with the workflow
        assert [(i.filename, i.date_time, i.create_system) for i in z.infolist()] == [(n, (1980, 1, 1, 0, 0, 0), 3) for n in wf_files]
        for n in wf_files:
            assert z.read(n) == (ROOT / "workflows" / "jira-lifecycle" / n).read_bytes(), n
    with zipfile.ZipFile(first) as z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        assert names == sorted(names)
        assert {(i.date_time, i.create_system, i.external_attr, i.compress_type) for i in infos} == {
            ((1980, 1, 1, 0, 0, 0), 3, 0o644 << 16, zipfile.ZIP_DEFLATED)}
        z_manifest = z.read("extension.yml")
        manifest = yaml.safe_load(z_manifest)
        texts = {n: z.read(n).decode() for n in names}
    commands = sorted(n for n in names if n.startswith("commands/"))
    assert commands == sorted("commands/" + Path(c["file"]).name for c in EXT["provides"]["commands"]) and len(commands) == 13
    assert {"extension.yml", "LICENSE", "rules/hitl.md", "rules/routing.md", "workflows/jira-lifecycle/workflow.yml"} <= set(names)
    assert set(names) == {"extension.yml", "LICENSE"} | set(commands) | {n for n in names if n.split("/")[0] in ("rules", "workflows")}
    for n in names:
        assert not n.startswith((".", "tests/", "src/", "docs/", "scripts/", "ci/", "catalog/", "bundle/")), n
        assert not n.endswith((".pyc", ".DS_Store")) and "__pycache__" not in n, n
    assert z_manifest == (ROOT / "extension.yml").read_bytes()  # REL1: the bundled manifest is the source one, byte for byte
    assert (manifest["extension"]["repository"], manifest["extension"]["homepage"]) == (REPO_URL, REPO_URL)
    for n, t in texts.items():  # the only repository URL that ships is the public one; commands/stack.md names the bare host prefix only
        assert set(re.findall(r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", t)) <= {REPO_URL}, n


GIT_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _repo(root, files, ignore="# c\n/*\n!/keep/\n!/top.txt\n*.pyc\nskip/\n!/.extensionignore\n"):
    """A scratch git repository: `files` written and added to the index (ls-files reads the index; no commit needed)."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], env=GIT_ENV, check=True)
    (root / ".extensionignore").write_text(ignore)  # the last rule: Spec Kit never copies the ignore file
    for rel in files:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel)
    subprocess.run(["git", "-C", str(root), "add", "-A"], env=GIT_ENV, check=True)
    return root


def test_ignore_rules_follow_gitignore_order_and_pruning(tmp_path):
    mod = _bundle_module()
    root = _repo(tmp_path / "r", ("keep/a.md", "keep/b.pyc", "keep/skip/x.md", "keep/deep/c.md", "top.txt", "drop/d.md", "other.txt"))
    assert mod.included_files(root) == ["keep/a.md", "keep/deep/c.md", "top.txt"]


def test_untracked_files_never_ship(tmp_path):
    mod = _bundle_module()
    root = _repo(tmp_path / "r", ("keep/a.md",))
    (root / "keep" / "notes-draft.md").write_text("draft")
    (root / "top.txt").write_text("untracked")
    assert mod.included_files(root) == ["keep/a.md"]


@pytest.mark.parametrize("kind", ["file", "dir", "loop"])
def test_a_tracked_symlink_refuses_the_build(tmp_path, kind):
    mod = _bundle_module()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside the repository")
    root = tmp_path / "r"
    root.mkdir()
    target = {"file": outside / "secret.txt", "dir": outside, "loop": Path("..")}[kind]
    (root / "keep").mkdir()
    os.symlink(str(target), str(root / "keep" / "link"))
    _repo(root, ("keep/a.md",))
    with pytest.raises(mod.BuildError, match="refusing symlink: keep/link"):
        mod.included_files(root)


def test_a_directory_swapped_for_a_symlink_after_git_add_refuses_the_build(tmp_path):
    mod = _bundle_module()
    root = _repo(tmp_path / "r", ("keep/sub/a.md",))
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "a.md").write_text("outside the repository")
    (root / "keep" / "sub" / "a.md").unlink()
    (root / "keep" / "sub").rmdir()
    os.symlink(str(tmp_path / "outside"), str(root / "keep" / "sub"))  # the index still lists keep/sub/a.md
    with pytest.raises(mod.BuildError, match="refusing symlink: keep/sub$"):
        mod.included_files(root)


def test_an_untracked_symlink_is_not_followed(tmp_path):
    mod = _bundle_module()
    root = _repo(tmp_path / "r", ("keep/a.md",))
    os.symlink("..", str(root / "keep" / "loop"))
    assert mod.included_files(root) == ["keep/a.md"]


def test_the_root_must_be_the_top_of_a_git_work_tree(tmp_path):
    mod = _bundle_module()
    root = _repo(tmp_path / "r", ("keep/a.md",))
    (tmp_path / "plain").mkdir()
    for bad in (root / "keep", tmp_path / "plain"):
        with pytest.raises(mod.BuildError, match="not the top of a git work tree"):
            mod.included_files(bad)



@pytest.mark.parametrize("drop", ["id", "version"])
def test_fm_ac2_a_manifest_without_a_quoted_id_or_version_is_a_build_error(tmp_path, drop):  # Fix-Minor (final review minor)
    mod = _bundle_module()
    fields = {"id": '  id: "x-kit"', "version": '  version: "0.1.0"'}
    fields[drop] = "  %s: unquoted" % drop
    (tmp_path / "extension.yml").write_text("extension:\n%s\n%s\n" % (fields["id"], fields["version"]))
    with pytest.raises(mod.BuildError, match="extension.yml has no quoted %s" % drop):
        mod.build(tmp_path, tmp_path / "out")


# ---- T6: the bundle manifest (bundle/bundle.yml), the third filing ---------------------------------------------------------------------
# Fields are those the installed Spec Kit 1.2.4.dev0 bundle model reads (specify_cli/bundles/manifest.py, BundleManifest.from_dict):
# schema_version; bundle.{id,name,version,role,description,author,license} (all required); requires.{speckit_version,tools,mcp};
# integration (absent: the bundle uses the project's integration); provides.{extensions,presets,steps,workflows,bundles}; tags.


def _bundle():
    return yaml.safe_load((BUNDLE / "bundle.yml").read_text())


def test_bundle_manifest_pins_the_manifests_and_the_catalog():
    b = _bundle()
    ext = _entry("extensions.json", "extensions", "jira-lifecycle")
    wf = _entry("workflows.json", "workflows", "jira-lifecycle")
    m, w = EXT["extension"], WF["workflow"]
    assert set(b) == {"schema_version", "bundle", "requires", "provides", "tags"} and b["schema_version"] == "1.0"
    assert b["bundle"] == {"id": "jira-lifecycle", "name": m["name"], "version": m["version"], "role": "developer",
                           "description": m["description"], "author": m["author"], "license": m["license"]}
    assert b["requires"] == {"speckit_version": EXT["requires"]["speckit_version"],
                             "tools": [t["name"] for t in EXT["requires"]["tools"] if t["required"]]} \
        == {"speckit_version": ">=1.2.1", "tools": ["skj", "git"]}
    assert b["provides"] == {"extensions": [{"id": m["id"], "version": m["version"]}], "workflows": [{"id": w["id"], "version": w["version"]}]}
    assert m["version"] == ext["version"] and w["version"] == wf["version"] and b["bundle"]["version"] == m["version"]
    assert b["tags"] == ext["tags"] == wf["tags"] == EXT["tags"]


def test_bundle_manifest_validates_offline_with_the_installed_speckit(tmp_path):
    import test_manifest as tm  # its skip site (no specify CLI) is the one listed in PORT_NOTES section 4
    exe = tm._specify()
    out = subprocess.run([exe, "bundle", "validate", "--path", str(BUNDLE), "--offline"], capture_output=True, text=True,
                         env=tm._isolated(tmp_path), cwd=str(tmp_path), stdin=subprocess.DEVNULL, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "jira-lifecycle is well-formed and valid." in " ".join(out.stdout.split())


def test_df2_out_inside_the_repository_is_refused(tmp_path, capsys):
    """DF2 (live run A11-C2, D-020): `--out` inside the repository is refused (exit 2) and nothing is written; outside works."""
    mod = _bundle_module()
    for inside in (ROOT / "nonexistent" / "distx", ROOT, ROOT / "dist"):
        with pytest.raises(SystemExit) as e:
            mod.main(["--out", str(inside)])
        assert e.value.code == 2 and "outside the repository" in capsys.readouterr().err
        assert not (ROOT / "nonexistent").exists()
    link = tmp_path / "link"
    link.symlink_to(ROOT)  # a path outside that resolves inside
    with pytest.raises(SystemExit):
        mod.main(["--out", str(link / "distx")])
    assert not (ROOT / "distx").exists()
    assert mod.main(["--out", str(tmp_path / "out")]) == 0 and len(list((tmp_path / "out").iterdir())) == 2


def test_df3_out_in_another_letter_case_is_refused_on_a_case_insensitive_disk(tmp_path, capsys):
    """Review m1: on macOS (APFS, case-insensitive) `--out <REPO IN UPPER CASE>/distx` resolved outside and wrote into the repository."""
    probe = Path(str(ROOT).swapcase())
    if not probe.exists():
        pytest.skip("the disk under the repository is case-sensitive")
    mod = _bundle_module()
    for inside in (probe / "distx", probe):
        with pytest.raises(SystemExit) as e:
            mod.main(["--out", str(inside)])
        assert e.value.code == 2 and "outside the repository" in capsys.readouterr().err
    assert not (ROOT / "distx").exists()
