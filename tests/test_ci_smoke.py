"""F4: the local rehearsal of the CI checks (scripts/ci_smoke/), carried from the fork and renamed.

The fork's rehearsal drove six cases (a to f) of its own strict mode through a fake board client. Here the harness is carried, with ONE
case (`g0`) that needs no template and no fake Jira: the existing `skj gate` CLI run in a real git clone of a local bare origin, the way a
CI job runs it. A shallow clone (what `actions/checkout` does by default, fetch-depth 1) has no `origin/main`, and in CI the gate then fails
closed (exit 3, a fixed message); a full clone (fetch-depth 0) with `--base origin/main` gives its normal answer.

C1 added the template-driven cases a to d (the four ci/templates/ files run against the fake Jira); they are tested in
tests/test_ci_smoke_fixtures.py, and the run below shows them passing from the command line too.
Nothing here touches the network: the origin is a bare repository in a temp directory, cloned over `file://`.
"""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SMOKE = ROOT / "scripts" / "ci_smoke"


def _load():
    spec = importlib.util.spec_from_file_location("ci_smoke_run_local", SMOKE / "run_local.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def smoke():
    return _load()


def test_the_rehearsal_runs_offline_and_every_case_gives_its_expected_result():
    cp = subprocess.run([sys.executable, str(SMOKE / "run_local.py")], capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert re.findall(r"^\((\w+)\) .*: (\w+)$", cp.stdout, re.M) == [(n, "ok") for n in ("g0", "a", "b", "c", "d")], cp.stdout


def test_an_unknown_case_name_is_refused():
    cp = subprocess.run([sys.executable, str(SMOKE / "run_local.py"), "nope"], capture_output=True, text=True, timeout=60)
    assert cp.returncode == 2 and "unknown case" in cp.stderr


def test_the_shallow_clone_fails_closed_and_the_full_clone_works(smoke):
    """The case itself, read back: shallow -> exit 3 with the fetch-depth hint on stderr; full -> exit 0 and the gate's JSON."""
    case = smoke.load_cases()["g0"]
    shallow, full = case["steps"]
    assert (shallow["clone"], shallow["exit"]) == ("shallow", 3) and "fetch-depth: 0" in " ".join(shallow["stderr_has"])
    assert (full["clone"], full["exit"], full["out"]) == ("full", 0, {"blocking": False, "ledger": "missing"})
    assert smoke.run_case("g0", case) == []


@pytest.mark.parametrize("change", [
    lambda c: c["steps"][0].update(exit=0),                                   # the shallow clone is expected to pass: it does not
    lambda c: c["steps"][0].update(stderr_has=["a message the gate never prints"]),
    lambda c: c["steps"][1].update(exit=3),                                   # the full clone is expected to fail: it does not
    lambda c: c["steps"][1].update(out={"blocking": True}),                   # a wrong JSON field
    lambda c: c["clones"].update(shallow={}),                                 # no shallow clone: the fail-closed step finds origin/main
])
def test_a_changed_expectation_is_reported_as_a_difference(smoke, change):
    """The harness can fail: each change to the case makes `run_case` report a step that differs."""
    case = json.loads(json.dumps(smoke.load_cases()["g0"]))
    change(case)
    assert smoke.run_case("g0", case) != []


def test_the_rehearsal_files_hold_only_fictional_data():
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(SMOKE.rglob("*")) if p.is_file() and p.suffix != ".pyc")
    assert set(re.findall(r"[\w.-]+\.atlassian\.net", text)) == {"example-site.atlassian.net"}
    assert set(re.findall(r"[\w.+-]+@[\w.-]+", text)) <= {"smoke@example.invalid"}


def test_the_fixture_copy_keeps_modes_mtimes_and_symlinks(smoke, tmp_path):
    """H1b-AC4: `_copy` (fixture tree -> the work tree) keeps an executable bit, a modification time and a symlink as a link."""
    import os
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "bin").mkdir(parents=True)
    tool = src / "bin" / "tool.sh"
    tool.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    tool.chmod(0o755)
    os.utime(tool, (1_600_000_000, 1_600_000_000))
    (src / "bin" / "link.sh").symlink_to("tool.sh")
    dst.mkdir()
    smoke._copy(src, dst)
    got = dst / "bin" / "tool.sh"
    assert got.stat().st_mode & 0o777 == 0o755 and got.stat().st_mtime == 1_600_000_000
    assert (dst / "bin" / "link.sh").is_symlink() and os.readlink(dst / "bin" / "link.sh") == "tool.sh"


def test_the_fixture_copy_replaces_links_already_at_the_destination(smoke, tmp_path):
    """H1b fix round: an overlay (`planted` over `feature`) may meet a link already there: it is replaced, never an error, never written
    through."""
    import os
    src, dst, outside = tmp_path / "src", tmp_path / "dst", tmp_path / "outside.txt"
    src.mkdir()
    dst.mkdir()
    outside.write_text("untouched\n", encoding="utf-8")
    (src / "link.sh").symlink_to("new.sh")  # a link onto an existing link
    (dst / "link.sh").symlink_to("old.sh")
    (src / "file.txt").write_text("from src\n", encoding="utf-8")  # a file onto an existing link
    (dst / "file.txt").symlink_to(outside)
    (src / "over.sh").symlink_to("x")  # a link onto an existing file
    (dst / "over.sh").write_text("old\n", encoding="utf-8")
    smoke._copy(src, dst)
    assert os.readlink(dst / "link.sh") == "new.sh" and os.readlink(dst / "over.sh") == "x"
    assert not (dst / "file.txt").is_symlink() and (dst / "file.txt").read_text() == "from src\n" and outside.read_text() == "untouched\n"
