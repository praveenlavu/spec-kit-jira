"""REL1: scripts/pin_release_sha.py writes the release commit into the skj install line, in place of `<COMMIT-SHA>`.

It takes only a full 40-character lowercase hex commit (never a tag, a branch or a short sha) and changes exactly the tracked files
whose install line `git+<REPO_URL>@<COMMIT-SHA>` it finds; the tests read either state (test_speckit_contract.install_ref)."""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from test_catalog import REPO_URL
from test_speckit_contract import install_ref

ROOT = Path(__file__).resolve().parent.parent
SHA = "0123456789abcdef0123456789abcdef01234567"
LINE = "git+%s@%s" % (REPO_URL, "<COMMIT-SHA>")
GIT_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
BAD = ["main", "v0.1.0", "<TAG>", "0123456", SHA.upper(), SHA + "8", SHA[:-1], SHA + "\n", " " + SHA, ""]


def _mod():
    spec = importlib.util.spec_from_file_location("pin_release_sha", str(ROOT / "scripts" / "pin_release_sha.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _repo(root):
    files = {"ci/a.yml": "run: pip install \"%s\"\n" % LINE, "notes.md": "no install line, just `<COMMIT-SHA>` in prose\n",
             "s.py": "ARGS = [%r, %r]\n" % (LINE, LINE)}
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], env=GIT_ENV, check=True)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / "untracked.yml").write_text(LINE)
    subprocess.run(["git", "-C", str(root), "add", "ci", "notes.md", "s.py"], env=GIT_ENV, check=True)
    return root, files


def test_pin_replaces_the_install_line_in_exactly_the_tracked_files_that_hold_it(tmp_path):
    root, files = _repo(tmp_path / "r")
    mod = _mod()
    assert mod.pin(root, SHA) == ["ci/a.yml", "s.py"]
    pinned = "git+%s@%s" % (REPO_URL, SHA)
    assert (root / "ci/a.yml").read_text() == files["ci/a.yml"].replace(LINE, pinned)
    assert (root / "s.py").read_text() == files["s.py"].replace(LINE, pinned) and LINE not in (root / "s.py").read_text()
    assert (root / "notes.md").read_text() == files["notes.md"] and (root / "untracked.yml").read_text() == LINE
    with pytest.raises(mod.PinError, match="no install line"):  # a second run finds nothing to pin: it says so, never silent
        mod.pin(root, SHA)


@pytest.mark.parametrize("bad", BAD)
def test_pin_refuses_anything_but_a_full_lowercase_commit_and_changes_nothing(tmp_path, bad, capsys):
    root, files = _repo(tmp_path / "r")
    mod = _mod()
    with pytest.raises(mod.PinError, match="40 lowercase hex"):
        mod.pin(root, bad)
    assert mod.main([bad]) == 2 and "40 lowercase hex" in capsys.readouterr().err
    assert {rel: (root / rel).read_text() for rel in files} == files


def test_the_repository_holds_the_install_line_in_the_three_templates_and_the_rehearsal_only():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout: the tracked files cannot be listed")
    assert _mod().REPO_URL == REPO_URL
    pinned = install_ref((ROOT / "ci/templates/check-pull-request.yml").read_text(encoding="utf-8")) != "<COMMIT-SHA>"
    assert _mod().targets(ROOT) == ([] if pinned else ["ci/templates/check-pull-request.yml", "ci/templates/final-check.yml",
                                                       "ci/templates/link-pull-request.yml", "scripts/ci_smoke/run_local.py"])
