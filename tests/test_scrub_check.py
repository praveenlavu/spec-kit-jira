"""T5: behaviour of scripts/scrub_check.py, run as the CLI in a throwaway git repository (fictional terms only).

Every secret sample is built by concatenation, so this file never matches its own patterns when the script scans the repo."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from skj import stack

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "scrub_check.py"
TERM = "Zorblax" + "Widgets"  # a fictional denylisted name


def _module():
    spec = importlib.util.spec_from_file_location("scrub_check", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _env(denylist=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "SCRUB_"))}
    if denylist is not None:
        env["SCRUB_DENYLIST"] = denylist
    return env


def git(repo, *args, **kw):
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
                   cwd=str(repo), check=True, capture_output=True, env={**_env(), "GIT_CEILING_DIRECTORIES": str(repo.parent)}, **kw)


def scrub(repo, *args, denylist=None):
    env = {**_env(denylist), "GIT_CEILING_DIRECTORIES": str(repo.parent)}
    p = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(repo), capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    return r


def commit(repo, files, msg="c"):
    for name, text in files.items():
        (repo / name).write_text(text)
    git(repo, "add", *files)
    git(repo, "commit", "-q", "-m", msg)


def test_denylist_is_a_case_insensitive_substring_match(repo):
    commit(repo, {"a.txt": "ok\nsee the X" + TERM.upper() + "Y docs\n"})
    code, out, _ = scrub(repo, denylist=TERM.lower())
    assert code == 1 and out.splitlines() == ["a.txt:2:denylist#1"]


def test_a_hit_never_prints_the_term(repo):
    commit(repo, {"a.txt": TERM + "\n"}, msg="mentions " + TERM)
    code, out, err = scrub(repo, "--history", denylist=TERM)
    assert code == 1 and out
    assert TERM.lower() not in (out + err).lower()


@pytest.mark.parametrize("label,sample", [
    ("github-token", "ghp_" + "A1b2" * 9),
    ("github-pat", "github_pat_" + "B" * 22),
    ("atlassian-token", "ATATT" + "x" * 20),
    ("aws-key", "AKIA" + "Q" * 16),
    ("private-key", "-----BEGIN RSA " + "PRIVATE KEY-----"),
])
def test_each_secret_pattern_is_found(repo, label, sample):
    commit(repo, {"s.txt": "x = '" + sample + "'\n"})
    code, out, _ = scrub(repo, denylist="")
    assert code == 1 and out.splitlines() == ["s.txt:1:secret:" + label]
    assert sample not in out


def test_the_env_denylist_wins_over_the_file(repo):
    (repo / ".scrub-denylist").write_text("# comment\nfileonlyterm\n")
    commit(repo, {"a.txt": "fileonlyterm\n" + TERM + "\n"})
    git(repo, "add", ".scrub-denylist")  # staged, so the scan would see it: it must still be skipped
    assert scrub(repo, denylist=TERM)[:2] == (1, "a.txt:2:denylist#1\n")
    assert scrub(repo)[:2] == (1, "a.txt:1:denylist#1\n")  # without the env, the file is used (and never scanned itself)


def test_history_finds_a_term_removed_from_the_tree(repo):
    commit(repo, {"a.txt": TERM + "\n"})
    commit(repo, {"a.txt": "clean\n"})
    assert scrub(repo, denylist=TERM)[:2] == (0, "")
    code, out, _ = scrub(repo, "--history", denylist=TERM)
    assert code == 1 and out and all(line.startswith("history:") for line in out.splitlines())


def test_require_denylist_exits_2_when_the_list_is_empty(repo):
    commit(repo, {"a.txt": "ok\n"})
    code, out, err = scrub(repo, "--require-denylist")
    assert (code, out) == (2, "") and "denylist is empty" in err
    (repo / ".scrub-denylist").write_text("# only a comment\n\n")
    assert scrub(repo, "--require-denylist", denylist="")[0] == 2
    assert scrub(repo, "--require-denylist", denylist=TERM)[0] == 0


def test_the_staged_blob_is_scanned_not_the_working_copy(repo):
    commit(repo, {"a.txt": "clean\n"})
    (repo / "a.txt").write_text(TERM + "\n")  # unstaged edit: not what a commit would hold
    assert scrub(repo, denylist=TERM)[:2] == (0, "")
    git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("clean again\n")  # staged term, clean working copy
    assert scrub(repo, denylist=TERM)[:2] == (1, "a.txt:1:denylist#1\n")


def test_a_git_failure_exits_3_without_the_term(tmp_path):
    plain = tmp_path / "notarepo"
    plain.mkdir()
    (plain / "a.txt").write_text(TERM + "\n")
    code, out, err = scrub(plain, "--history", denylist=TERM)
    assert (code, out) == (3, "") and err.startswith("scrub_check: git ")
    assert TERM.lower() not in err.lower()


def test_history_scans_author_email_and_message(repo):
    commit(repo, {"a.txt": "clean\n"})
    git(repo, "commit", "-q", "--allow-empty", "--author", "Some One <dev@" + TERM.lower() + ".example.com>", "-m", "plain")
    git(repo, "commit", "-q", "--allow-empty", "-m", "fix for " + TERM)
    code, out, _ = scrub(repo, "--history", denylist=TERM)
    assert code == 1 and len(out.splitlines()) == 2  # once in the author line, once in the message (the tree is clean)


def test_the_secret_lists_are_equal_wherever_duplicated():
    assert [(n, p.pattern) for n, p in stack.SECRETS] == [(n, p.pattern) for n, p in _module().SECRETS]


def test_no_denylisted_name_in_any_tracked_file():  # T10: the internal files that held one are out of the tree, so no exemption
    """B1: the denylist (the client handle among its names) is read, never spelled here; hits print path:line:kind only.
    The working copy of every tracked file is read, so an unstaged edit is caught too."""
    mod = _module()
    deny = mod.load_denylist(os.environ, ROOT / mod.DENYLIST_FILE)
    if not deny:
        pytest.skip("no denylist here: the tracked-name check reads SCRUB_DENYLIST or the untracked .scrub-denylist")
    paths = subprocess.run(["git", "ls-files", "-z"], cwd=str(ROOT), capture_output=True, check=True).stdout.decode().split("\0")
    hits = []
    for rel in paths:
        if not rel or rel == mod.DENYLIST_FILE or not (ROOT / rel).is_file():
            continue
        data = (ROOT / rel).read_bytes()
        if b"\0" not in data:
            hits += [h for h in mod.scan_text(rel, data.decode(errors="replace"), deny) if h[2].startswith("denylist")]
    assert not hits, ["%s:%d:%s" % h for h in hits]
