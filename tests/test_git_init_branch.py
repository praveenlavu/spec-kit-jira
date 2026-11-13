"""Fix-CI2a: every `git init` in the tests names its branch. Hosted runners' git makes `master`, a laptop's often `main`; a test that
leans on the machine's default passes on one and fails on the other."""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
# `["git", "init"`, `["git", "-C", d, "init"` or a helper `git(d, "init"` / `_git4(d, "init"`, on one line.
# shortcut: a call split over lines, or an init built from a variable, is not seen; widen if one appears.
GIT_INIT = re.compile(r'\[\s*"git",\s*(?:"-C",\s*[^,\]]+,\s*)?"init"|\b_?git\w*\(\s*[^,()"]+,\s*"init"')
BRANCH = re.compile(r'"-b"|"--initial-branch')
ALLOWED = set()  # "file.py:line" of a deliberate default-branch init (none today)


def violations(name, text):
    return ["%s:%d" % (name, n) for n, line in enumerate(text.split("\n"), 1)
            if GIT_INIT.search(line) and not BRANCH.search(line) and "%s:%d" % (name, n) not in ALLOWED]


def test_every_git_init_in_the_tests_names_its_branch():
    bad = [v for p in sorted(HERE.glob("*.py")) if p.name != Path(__file__).name for v in violations(p.name, p.read_text("utf-8"))]
    assert bad == [], "pass -b main to git init: " + ", ".join(bad)


def test_the_guard_sees_each_form():
    for line in ('subprocess.run(["git", "init", "-q", str(p)])', 'git(tmp_path, "init", "-q")', '_git4(p, "init")',
                 'run(["git", "-C", str(d), "init"])'):
        assert violations("x.py", line) == ["x.py:1"], line
        assert violations("x.py", line.replace('"init"', '"init", "-b", "main"')) == [], line
    for line in ('git(repo, "commit", "-q", "-m", "init")', 'main(["stack", "init"])', 'commit(p, "init", {})',
                 'subprocess.run(["git", "init", "--initial-branch=main", p])'):
        assert violations("x.py", line) == [], line
