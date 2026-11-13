"""P1: `skj pr create` (src/skj/prs.py, the verb in __main__.py). Ported from the fork's tests/test_prs.py, GitHub cases only.

Nothing here runs `gh` or `git push`, and no test touches the network: `subprocess.run` is wrapped by `Fake`, which answers `gh` and
`git push` itself and lets every other (local, read-only) git call through to the temp repository. No Jira client is ever built (the
client factory fails the test). Everything is fictional: the site is example-site.atlassian.net, the owner example-owner.

Dropped with the second host's code: its create/comment/link cases, the 4000-character description cap, the work-item reference in the
title, the integer id limit, and the `uncommitted` cases (comment approval: A phase). `review_blocks`, `spec_dir_in_tree` and
`only_ledger_since` (L5b) are tested through strict in test_strict_checks.py.
"""
import json
import os
import re
import signal
import subprocess
import time

import pytest

import skj.__main__ as m
from skj import config, dod, gate, prs

KEY = "SPK-1"
URL = "https://github.com/example-owner/example-repo/pull/5"
SECRET = "ATA" + "TT3xFfGF0fictional-secret-0123456789"  # split so the secret scanner does not flag the fixture


# ---- the world: a git repo with a config, a ledger, a decisions file, a tasks.md and a feature branch --------------------------------

def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


def commit(root, msg, files=None):
    for rel, text in (files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)
    sha = git(root, "rev-parse", "HEAD")
    if subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", "refs/remotes/origin/feat/x"], capture_output=True).returncode == 0:
        git(root, "update-ref", "refs/remotes/origin/feat/x", sha)  # the simulated remote follows the branch (it was pushed)
    return sha


def ledger(rows="| AC-1 | does a | tests/test_a.py::test_a | abc1234 | green |\n", gate_block=None, review_block=None, title="Add the fictional widget",
           spec="specs/001-x"):
    gate_block = GATE if gate_block is None else gate_block
    review_block = REVIEW if review_block is None else review_block
    return ("# DoD \u2014 %s: %s\n\n- **Key:** %s\n- **Spec dir:** %s\n\n## 1. Seeded from Jira\n\n### Acceptance Criteria\n- a thing\n\n%s\n\n"
            "## 3. AC \u2192 tests (traceability)\n\n| AC | acceptance criterion | tests | commit/PR | status |\n|---|---|---|---|---|\n%s\n"
            "## 4. Gate & review log\n%s\n%s\n## 5. Definition of Done\n- [ ] x\n"
            % (KEY, title, KEY, spec, dod.SECTION2, rows, gate_block, review_block))


GATE = ("### Gate \u2014 2026-10-28 \u2014 `main...HEAD` @ `abc1234`\n- traceability: all AC mapped\n- assertion gaps: none\n"
        "- AC drift: not checked \u2014 offline\n- missing evidence: none\n- commits without SPK-1: none\n- open questions: none\n- blocking: no\n")


def review(blocker=0, major=1, blocks="no", tail=""):
    return ("### Review \u2014 2026-10-28 \u2014 `main...feat/x` @ `abc1234`%s\n\n- **Confirmed by severity:** blocker %d \u00b7 major %d \u00b7 minor 0\n"
            "- **Blocks PR:** %s\n" % (tail, blocker, major, blocks))


REVIEW = review()


@pytest.fixture
def git_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv("GIT_%s_NAME" % k, "Test")
        monkeypatch.setenv("GIT_%s_EMAIL" % k, "test@example.invalid")


class Fake:
    """Replaces subprocess.run: `gh` and `git push` are answered (and recorded), every other call runs for real."""

    def __init__(self, real, rc=0, out=URL + "\n", err=b"", push_rc=0, exc=None):
        self.real, self.rc, self.out, self.err, self.push_rc, self.exc = real, rc, out, err, push_rc, exc
        self.gh, self.push, self.kw = [], [], []

    def __call__(self, argv, *a, **kw):
        if argv[0] == "gh":
            self.gh.append(list(argv))
            self.kw.append(kw)
            if self.exc:
                raise self.exc
            return subprocess.CompletedProcess(argv, self.rc, stdout=self.out.encode(), stderr=self.err)
        if argv[0] == "git" and "push" in argv[:12]:
            self.push.append(list(argv))
            self.kw.append(kw)
            return subprocess.CompletedProcess(argv, self.push_rc, stdout=b"", stderr=b"")
        return self.real(argv, *a, **kw)


@pytest.fixture
def world(tmp_path, monkeypatch, git_env):
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    monkeypatch.delenv("CI", raising=False)
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "init", {
        "skj.config.json": json.dumps({"site": "example-site.atlassian.net", "project": "SPK"}),
        "dod/DoD_SPK-1.md": ledger(), "specs/001-x/tasks.md": "- [x] T001 One\n- [x] T002 Two\n"})
    git(tmp_path, "switch", "-q", "-c", "feat/x")
    commit(tmp_path, "SPK-1 work", {"f.txt": "one\n"})
    git(tmp_path, "update-ref", "refs/remotes/origin/feat/x", "HEAD")  # the branch is on origin, as the last fetch saw it
    monkeypatch.chdir(tmp_path)
    fake = Fake(subprocess.run)
    monkeypatch.setattr(subprocess, "run", fake)
    w = type("World", (type(tmp_path),), {})(tmp_path)  # a Path that also carries the fake
    w.fake = fake
    return w


def no_jira():
    raise AssertionError("pr create must not build a Jira client")


def run(*argv):
    return m.main(["pr", "create", KEY, "--base", "main", "--head", "feat/x", *argv], client_factory=no_jira)


def set_ledger(root, text, name="dod/DoD_SPK-1.md"):
    (root / name).write_text(text, encoding="utf-8")


def last_err(capsys):
    """The last stderr line (the one before it is the plan's sha256)."""
    return capsys.readouterr().err.splitlines()[-1]


def body_of(text):
    return text.split("## PR body (exactly as it will be sent)\n\n", 1)[1]


# ---- P1-AC1: the title starts with the key ----------------------------------------------------------------------------------------

def test_title_starts_with_key(world, capsys):
    """P1-AC1: the title is `SPK-1 <title>`: the key first, uppercase, then the text; the apply sends exactly that as one argv item."""
    assert run("--title", "Add x") == 0
    assert "| title | SPK-1 Add x |" in capsys.readouterr().out and world.fake.gh == []
    assert run("--title", "Add x", "--apply") == 0
    argv = world.fake.gh[0]
    title = argv[argv.index("--title") + 1]
    assert title == "SPK-1 Add x" and title.startswith(KEY) and KEY == KEY.upper()
    assert prs.make_title(KEY, "Add x") == "SPK-1 Add x" and prs.make_title(KEY, "") == KEY
    assert prs.make_title(KEY, "SPK-1 Add x") == "SPK-1 Add x" and prs.make_title(KEY, "SPK-1") == KEY  # the key is not repeated
    for bad in ("spk-1", " SPK-1", "SPK-0", "SPK-01", "SPK-1;x", "SPK-1\n", "", None):
        with pytest.raises(ValueError, match="invalid issue key"):
            prs.make_title(bad, "t")


def test_a_title_can_never_be_an_option_and_is_cleaned():
    for text in ("--force", "-x", "--title=y", "\u202e--evil", "\x00--evil"):
        t = prs.make_title(KEY, text)
        assert t.startswith(KEY + " ") and not t.startswith("-")
    t = prs.make_title(KEY, "a\r\nb\u2028c\u202ed\x1b[2Je\u200bf\t g")
    assert t == "SPK-1 a b cd[2Jef g"  # line breaks become a space; control, bidi and format characters are dropped
    assert not re.search(r"[\x00-\x1f\u2028\u2029\u200b\u202e]", t)
    long = prs.make_title(KEY, "x" * 5000)
    assert len(long) == len(KEY) + 1 + prs.MAX_TITLE


HOSTILE = 'x $(touch pwned) `touch pwned2` "q" \'s\' ; rm -rf ~\nline2'


def test_a_hostile_title_goes_to_gh_as_one_argv_item_and_runs_nothing(world):
    assert run("--title", HOSTILE, "--apply") == 0
    argv = world.fake.gh[0]
    assert argv[argv.index("--title") + 1] == "SPK-1 " + " ".join(HOSTILE.split())
    assert all(not kw.get("shell") for kw in world.fake.kw) and not (world / "pwned").exists() and not (world / "pwned2").exists()


def test_the_default_title_is_the_story_summary_of_the_ledger(world, capsys):
    assert run() == 0
    assert "| title | SPK-1 Add the fictional widget |" in capsys.readouterr().out
    set_ledger(world, "no title line here\n")
    assert prs.ledger_title("no title line here\n") == "" and prs.ledger_title(ledger(title="x" * 5000)) == "x" * prs.MAX_TITLE


def test_a_smart_commit_shape_in_the_title_is_refused(world, capsys):
    """P2-AC3: no title holds `KEY #word`."""
    for text in ("fix #done", "x #close it", "#time 2h"):
        with pytest.raises(ValueError, match="smart-commit"):
            prs.make_title(KEY, text)
    for text in ("fix #done", "x #close it", "#time 2h", "a\n#comment b", "see #1st"):
        assert run("--title", text, "--apply") == 1, text
        assert "smart-commit" in capsys.readouterr().err
    assert world.fake.gh == []
    assert run("--title", "Fix #12 and C# parsing", "--apply") == 0  # a pull request number and a lone `#` are not commands


# ---- P1-AC2: a dry run unless --apply ---------------------------------------------------------------------------------------------

def test_dry_run_runs_no_gh_and_no_push_and_writes_nothing(world, capsys):
    before = sorted(p.name for p in world.iterdir())
    assert run("--push") == 0
    out = capsys.readouterr().out
    assert world.fake.gh == [] and world.fake.push == [] and "dry run: nothing created" in out
    assert "gh pr create --title <title> --body-file - --base <base> --head <head>" in out and "## PR body (exactly as it will be sent)" in out
    assert sorted(p.name for p in world.iterdir()) == before


def test_the_plan_shows_the_exact_body_that_apply_sends(world, capsys):
    assert run("--plan-out", "out/plan.md") == 0
    capsys.readouterr()
    plan = (world / "out" / "plan.md").read_text(encoding="utf-8")
    assert run("--apply") == 0
    assert world.fake.kw[0]["input"].decode("utf-8") == body_of(plan) and world.fake.gh[0][world.fake.gh[0].index("--body-file") + 1] == "-"
    assert "| head commit | %s |" % git(world, "rev-parse", "refs/heads/feat/x") in plan


def test_apply_runs_exactly_one_gh_with_title_and_body_from_stdin(world, capsys):
    assert run("--apply") == 0
    assert world.fake.gh == [["gh", "pr", "create", "--title", "SPK-1 Add the fictional widget", "--body-file", "-", "--base", "main",
                              "--head", "feat/x"]]
    assert world.fake.push == [] and "created: " + URL in capsys.readouterr().out
    kw = world.fake.kw[0]
    assert kw["check"] is False and kw["timeout"] == prs.GH_TIMEOUT and not kw.get("shell")
    assert os.path.realpath(kw["cwd"]) == os.path.realpath(str(world)) and isinstance(kw["input"], bytes)


def test_the_body_comes_from_the_ledger_gate_and_review(world, capsys):
    set_ledger(world, ledger(rows="| AC-1 | a | tests/test_a.py::test_a, tests/test_b.py::test_b | abc | green |\n| AC-2 | b | | | red |\n"))
    commit(world, "ledger", {})
    assert run("--plan-out", "p.md") == 0
    body = body_of((world / "p.md").read_text(encoding="utf-8"))
    for needle in ("Add the fictional widget", "Story: SPK-1 (https://example-site.atlassian.net/browse/SPK-1)",
                   "| AC-1 | green | tests/test\\_a.py::test\\_a, tests/test\\_b.py::test\\_b |", "| AC-2 | red | - |",
                   "`dod/DoD_SPK-1.md`", "## Gate (2026-10-28)", "- **Traceability:** all AC mapped", "- **AC drift:** not checked \u2014 offline",
                   "- **Commits without SPK-1:** none", "- **Blocking:** no", "## Review (2026-10-28)",
                   "- **Confirmed findings:** blocker 0 \u00b7 major 1 \u00b7 minor 0", "- **Blocks PR:** no", "Decisions and assumptions: none recorded"):
        assert needle in body, needle
    assert "{{" not in body


def test_a_missing_gate_or_review_is_said_not_invented(world):
    body = prs.build_body(config.Config("example-site.atlassian.net", "SPK"), KEY, ledger(gate_block="", review_block=""))
    assert "No gate result is logged" in body and "No review is logged" in body
    assert "no AC rows" not in body
    body = prs.build_body(config.Config("example-site.atlassian.net", "SPK"), KEY, ledger(rows=""))
    assert "| - | no AC rows in ledger section 3 | - |" in body


def test_an_unclosed_fence_hides_section_four_and_says_so(world):
    body = prs.build_body(config.Config("example-site.atlassian.net", "SPK"), KEY, ledger().replace("## 4.", "```\n## 4.", 1))
    assert prs.UNCLOSED in body and "- **Traceability:**" not in body


def test_decisions_are_listed_by_id_and_kind_never_by_text(world):
    md = ("# Decisions SPK-1\n\n- [ ] D1: @octocat SPK-1 #done secret text \u2192 _(open)_ \u00b7 kind: decision \u00b7 why: because\n"
          "- [x] D2: another \u2192 rejected \u00b7 kind: assumption \u00b7 by: PO\n- [x] D3: fine \u2192 validated \u00b7 kind: decision \u00b7 by: PO\n")
    line = prs._decisions_line(md)
    assert line == "1 open, 1 rejected, 1 validated: D1 (decision), D2 (assumption)"
    body = prs.build_body(config.Config("example-site.atlassian.net", "SPK"), KEY, ledger(), md)
    assert "secret text" not in body and "octocat" not in body
    assert prs._decisions_line("# nothing\n") == "none recorded" and prs._decisions_line(None) == "none recorded"
    assert "could not be read" in prs._decisions_line(md + "- [ ] D9 broken line\n")


# ---- P1-AC3: no token in argv, and the environment is scrubbed --------------------------------------------------------------------

def test_gh_env_is_an_allow_list_and_never_holds_the_jira_identity():
    base = {"HOME": "/h", "PATH": "/bin", "LC_TIME": "C", "GH_TOKEN": "gh-own", "GH_HOST": "ghe.example.com", "XDG_CONFIG_HOME": "/x",
            "SKJ_TOKEN": SECRET, "SKJ_EMAIL": "a@example.com", "SKJ_SITE": "s", "JIRA_API_TOKEN": SECRET, "DEPLOY_KEY": "k",
            "AWS_SECRET_ACCESS_KEY": "k", "GIT_DIR": "/elsewhere", "GIT_CONFIG_COUNT": "1", "SSH_AUTH_SOCK": "/s", "RANDOM_VAR": "v"}
    env = prs.gh_env(base)
    assert {"HOME", "PATH", "LC_TIME", "GH_TOKEN", "GH_HOST", "XDG_CONFIG_HOME"} <= set(env)
    assert not [k for k in env if k.startswith(("SKJ_", "GIT_DIR", "GIT_CONFIG")) or k in ("JIRA_API_TOKEN", "DEPLOY_KEY", "AWS_SECRET_ACCESS_KEY",
                                                                                         "SSH_AUTH_SOCK", "RANDOM_VAR")]
    assert env["GH_PROMPT_DISABLED"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0" and SECRET not in "".join(env.values())


def test_no_token_in_argv_env_or_body(world, monkeypatch):
    """P1-AC3: the Jira identity and a Jira-looking secret in the environment reach neither argv, nor stdin, nor the environment of
    gh or git push; gh's own GH_TOKEN is the one credential allowed through."""
    for k, v in (("SKJ_TOKEN", SECRET), ("SKJ_EMAIL", "dev@example.com"), ("JIRA_API_TOKEN", SECRET), ("MY_SECRET", SECRET),
                 ("GH_TOKEN", "gh-own-login-token-0001")):
        monkeypatch.setenv(k, v)
    assert run("--push", "--apply") == 0
    for call, kw in zip(world.fake.push + world.fake.gh, world.fake.kw):
        assert not any(SECRET in a or "dev@example.com" in a for a in call)
        env = kw["env"]
        assert not [k for k in env if k.startswith("SKJ_")] and SECRET not in "".join(env.values()) and "MY_SECRET" not in env
        assert kw["stdin"] == subprocess.DEVNULL if call[0] == "git" else SECRET.encode() not in kw["input"]
    assert world.fake.kw[-1]["env"]["GH_TOKEN"] == "gh-own-login-token-0001"  # gh keeps its own login
    assert "GH_TOKEN" not in world.fake.kw[0]["env"] and "gh-own-login-token-0001" not in "".join(world.fake.kw[0]["env"].values())  # git does not


def test_a_secret_named_variable_value_is_cut_from_the_error_text(world, monkeypatch, capsys):
    monkeypatch.setenv("GH_TOKEN", "gh-own-login-token-0001")
    world.fake.rc, world.fake.err = 1, b"HTTP 401 for token gh-own-login-token-0001\n"
    assert run("--apply") == 1
    err = capsys.readouterr().err
    assert "gh-own-login-token-0001" not in err and "exit code 1" in err and "[redacted]" in err


@pytest.mark.parametrize("leak", [
    "ghp_" + "A1b2C3d4E5" * 4, "gho_" + "x" * 30, "ghs_" + "Z9" * 15, "github_pat_" + "11AB_cd" * 6,
    "https://user:s3cretpw@github.com/example-owner/example-repo.git", "https://tok3n@github.com/o/r"])
def test_gh_error_text_is_stripped_of_credentials(world, capsys, leak):
    world.fake.rc, world.fake.err = 1, ("fatal: could not read from %s now\n" % leak).encode()
    assert run("--apply") == 1
    err = capsys.readouterr().err
    secret = leak.split("@")[0].split("//")[-1] if "@" in leak else leak
    assert secret not in err and "exit code 1" in err and "could not read from" in err
    assert prs._redact("x https://u:p@h.example/y z") == "x https://h.example/y z" and prs._redact("a") == "a"


def test_issue_references_are_neutralised(world):
    set_ledger(world, ledger(gate_block=GATE.replace("all AC mapped", "see #123 and acme/widgets#12 and #_x"), title="Fix #7 for C#"))
    commit(world, "ledger", {})
    assert run("--apply") == 0
    sent = world.fake.kw[0]["input"].decode("utf-8")
    assert "\uff03123" in sent and "widgets\uff0312" in sent and "\uff03\\_x" in sent and "Fix \uff037 for C\\#" in sent
    assert not re.search(r"#\d|\\#\w|[a-z]#\d", sent) and "## Summary" in sent  # template headings keep working
    title = world.fake.gh[0][world.fake.gh[0].index("--title") + 1]
    assert title == "SPK-1 Fix \uff037 for C#"
    assert prs.neutral("#12 a#1 _#") == "\uff0312 a\uff031 \\_\\#"


# ---- unticked tasks ---------------------------------------------------------------------------------------------------------------

OPEN_TASKS = "- [x] T001 Done\n- [ ] T002 Open\n- [ ] T003 Open too\n"


@pytest.mark.parametrize("apply", [False, True])
def test_unticked_tasks_refuse_and_write_nothing(world, capsys, apply):
    (world / "specs/001-x/tasks.md").write_text(OPEN_TASKS)
    assert run("--plan-out", "out/plan.md", *(["--apply"] if apply else [])) == 1
    err = capsys.readouterr().err
    assert "T002, T003" in err and "T001" not in err and "--allow-incomplete" in err
    assert world.fake.gh == [] and world.fake.push == [] and not (world / "out").exists()


def test_allow_incomplete_opens_a_draft(world, capsys):
    (world / "specs/001-x/tasks.md").write_text(OPEN_TASKS)
    assert run("--allow-incomplete", "--plan-out", "plan.md") == 0
    assert "| draft | yes \u2014 2 unticked task(s): T002, T003 |" in (world / "plan.md").read_text(encoding="utf-8")
    assert run("--allow-incomplete", "--apply") == 0
    assert world.fake.gh[0][-1] == "--draft" and world.fake.gh[0].count("--draft") == 1


def test_all_ticked_is_not_a_draft(world):
    assert run("--apply") == 0
    assert "--draft" not in world.fake.gh[0]


@pytest.mark.parametrize("how", ["missing", "empty", "no-tasks", "symlink", "no-spec-dir", "huge"])
def test_an_unreadable_or_empty_tasks_md_refuses_unless_allowed(world, capsys, how):
    t = world / "specs/001-x/tasks.md"
    if how == "missing":
        t.unlink()
    elif how == "empty":
        t.write_text("")
    elif how == "no-tasks":
        t.write_text("# Tasks\n\nnothing parseable\n")
    elif how == "symlink":
        t.unlink()
        (world / "real.md").write_text("- [x] T001 a\n")
        t.symlink_to("../../real.md")
    elif how == "no-spec-dir":
        set_ledger(world, ledger(spec="_(set by speckit.jira-lifecycle.pull)_"))
    else:
        t.write_text("- [x] T001 a\n" + "x" * (2 << 20))
    assert run("--apply") == 1
    err = capsys.readouterr().err
    assert err.startswith("skj: refusing to open a PR: ") and "--allow-incomplete" in err and "Traceback" not in err and world.fake.gh == []
    assert run("--allow-incomplete", "--plan-out", "p.md") == 0
    assert "| draft | yes \u2014 tasks.md could not be checked" in (world / "p.md").read_text(encoding="utf-8") or how in ("empty", "no-tasks")


def test_too_many_or_odd_tasks_are_refused(world, capsys):
    (world / "specs/001-x/tasks.md").write_text("".join("- [x] T%03d a\n" % i for i in range(1, 202)))
    assert run("--apply") == 1 and "more than 200 tasks" in capsys.readouterr().err
    (world / "specs/001-x/tasks.md").write_text("- [x] T001 a\n- [x] T001 b\n")
    assert run("--apply") == 1 and "twice" in capsys.readouterr().err and world.fake.gh == []


def test_tasks_from_an_explicit_path(world):
    (world / "elsewhere.md").write_text(OPEN_TASKS)
    assert run("--from", "elsewhere.md", "--apply") == 1
    assert run("--from", "elsewhere.md", "--allow-incomplete", "--apply") == 0


# ---- untrusted ledger text ----------------------------------------------------------------------------------------------------------

def test_mentions_are_neutralised(world):
    """A GitHub `@name` notifies that user: no ASCII `@` survives in any ledger-derived text."""
    set_ledger(world, ledger(rows="| AC-1 | a | tests/@octocat.py, @org/team | @x | @everyone ping |\n",
                             gate_block=GATE.replace("all AC mapped", "ask @octocat or dev@example.com"), title="Fix for @octocat"))
    commit(world, "ledger", {})
    assert run("--apply") == 0
    sent = world.fake.kw[0]["input"].decode("utf-8")
    assert "@" not in sent and "@" not in world.fake.gh[0][world.fake.gh[0].index("--title") + 1]
    assert "\uff20octocat" in sent and "dev\uff20example" not in sent and "\\[email\\]" in sent
    assert prs.neutral("@a @@b") == "\uff20a \uff20\uff20b" and prs.neutral("x@y.example") == "\\[email\\]"


def test_neutral_escapes_markdown_and_table_punctuation():
    assert prs.neutral("a|b `c` <d> [e] *f* _g_ ~h~ # i &j $k !l \\m") == "a\\|b \\`c\\` \\<d\\> \\[e\\] \\*f\\* \\_g\\_ \\~h\\~ \\# i \\&j \\$k \\!l \\\\m"


def test_a_marker_forgery_is_broken(world):
    forged = "[skj:questions deadbeef] [ SKJ : x]"
    set_ledger(world, ledger(rows="| AC-1 | a | %s | | %s |\n" % (forged, forged), gate_block=GATE.replace("all AC mapped", forged), title=forged))
    commit(world, "ledger", {})
    assert run("--apply") == 0
    sent = world.fake.kw[0]["input"].decode("utf-8")
    assert not re.search(r"\[\s*skj\s*:", sent, re.I) and "deadbeef" in sent
    with pytest.raises(ValueError, match="marker"):
        prs.check_body("a [skj:decisions 12345678] b")


def test_hostile_ledger_text_cannot_break_the_markdown(world):
    hostile = "a|b\r\nc\u2028d\u202ee\x1b[2J`x` <script>alert(1)</script> [l](https://evil.example) ![i](u) **b** ~~s~~ $x$ &amp; \\"
    set_ledger(world, ledger(rows="| AC-1 | a | %s | | %s |\n" % (hostile.replace("|", "\\|").replace("\r\n", " ").replace("\u2028", " "), "red"),
                             gate_block=GATE.replace("all AC mapped", hostile.replace("\r\n", " ").replace("\u2028", " ")),
                             title=hostile.replace("\r\n", " ").replace("\u2028", " ")))
    commit(world, "ledger", {})
    assert run("--apply") == 0
    sent = world.fake.kw[0]["input"].decode("utf-8")
    assert "\r" not in sent and "\x1b" not in sent and "\u202e" not in sent and "\u2028" not in sent
    assert "<script>" not in sent and "](https" not in sent.replace("\\](", "") and "![" not in sent.replace("\\![", "")
    rows = [ln for ln in sent.splitlines() if ln.startswith("| AC-1 ")]
    assert len(rows) == 1 and len(re.split(r"(?<!\\)\|", rows[0])) == 5  # still three cells between the outer bars


def test_a_smart_commit_shape_from_the_ledger_is_refused(world, capsys):
    set_ledger(world, ledger(gate_block=GATE.replace("all AC mapped", "SPK-1 #done")))
    commit(world, "ledger", {})
    assert run("--apply") == 1
    assert "smart-commit" in capsys.readouterr().err and world.fake.gh == []
    set_ledger(world, ledger(rows="| AC-1 | a | t | | #close |\n"))  # `#word` after the body's own key mention
    commit(world, "ledger2", {})
    assert run("--apply") == 1 and world.fake.gh == []
    set_ledger(world, ledger(gate_block=GATE.replace("all AC mapped", "mentions SPK-9 and SPK-1 only")))  # key mentions alone are fine
    commit(world, "ledger3", {})
    assert run("--apply") == 0


def test_an_oversize_body_is_refused(world, monkeypatch, capsys):
    monkeypatch.setattr(prs, "MAX_BODY", 300)
    assert run("--apply") == 1
    assert "larger than 300 characters" in capsys.readouterr().err and world.fake.gh == []
    monkeypatch.undo()
    assert len(prs.build_body(config.Config("example-site.atlassian.net", "SPK"), KEY, ledger(rows="| AC-1 | a | t | | g |\n" * 5000))) < prs.MAX_BODY


# ---- gh: missing, failing, timing out, answering oddly ----------------------------------------------------------------------------

def test_gh_missing_is_a_fixed_message(world, capsys):
    world.fake.exc = FileNotFoundError("gh")
    assert run("--apply") == 1
    assert last_err(capsys) == "skj: " + prs.E_NO_GH


def test_gh_failure_shows_the_code_and_one_clean_line(world, capsys):
    world.fake.rc, world.fake.err = 1, "first\nHTTP 422: no commits between main and feat/x\x1b[2J\u202e\n".encode("utf-8")
    assert run("--apply") == 1
    assert last_err(capsys) == "skj: `gh pr create` failed with exit code 1: HTTP 422: no commits between main and feat/x[2J; no retry was made"
    world.fake.err = b"x" * 100000
    assert run("--apply") == 1 and len(last_err(capsys)) < 400 and len(world.fake.gh) == 2


@pytest.mark.parametrize("exc,text", [(subprocess.TimeoutExpired(["gh"], 60), prs.E_GH_TIMEOUT), (PermissionError("x"), prs.E_GH_RUN)])
def test_gh_timeout_or_cannot_run(world, capsys, exc, text):
    world.fake.exc = exc
    assert run("--apply") == 1
    assert last_err(capsys) == "skj: " + text and len(world.fake.gh) == 1  # never retried


@pytest.mark.parametrize("out", [
    "http://github.com/example-owner/example-repo/pull/5", "https://evil.example/example-owner/example-repo/pull/5",
    "https://user:pw@github.com/example-owner/example-repo/pull/5", "https://github.com:8443/example-owner/example-repo/pull/5",
    "https://github.com/example-owner/example-repo/pull/5?x=1", "https://github.com/example-owner/example-repo/pull/5#c",
    "https://github.com/example-owner/example-repo/issues/5", "https://github.com/example-owner/example-repo/pull/0",
    "https://github.com.evil.example/o/r/pull/5", "https://github.com/o/r/pull/5/../../x", "javascript:alert(1)", "",
    "https://github.com/o/r/pull/5\u202e", "https://github.com/o/r/pull/" + "9" * 11, "not a url at all"])
def test_a_bad_pr_url_is_refused(world, capsys, out):
    world.fake.out = out + "\n"
    assert run("--apply") == 1
    cap = capsys.readouterr()
    assert cap.err.splitlines()[-1] == "skj: " + prs.E_URL and "created:" not in cap.out and (not out or out not in cap.out)


def test_a_good_pr_url_is_taken_from_the_last_line_and_gh_host_is_allowed(world, monkeypatch, capsys):
    world.fake.out = "Creating pull request for feat/x into main\n\n" + URL + "\n"
    assert run("--apply") == 0 and "created: " + URL in capsys.readouterr().out
    monkeypatch.setenv("GH_HOST", "ghe.example.com")
    world.fake.out = "https://ghe.example.com/o/r/pull/12\n"
    assert run("--apply") == 0
    monkeypatch.setenv("GH_HOST", "bad host/..")
    assert run("--apply") == 1


# ---- refs, push, remote -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["+main", ":main", "a:b", "--force", "-x", "x..y", "a//b", "a/", "a.lock", "a@{u}", "a b", "", "~1", "a^", "a\nb",
                                 "a/.hidden", "x" * 300])
@pytest.mark.parametrize("flag", ["--base", "--head"])
def test_branch_names_are_strict(world, capsys, bad, flag):
    argv = ["pr", "create", KEY, "--base", "main", "--head", "feat/x", "--apply"]
    argv[argv.index(flag) + 1] = bad
    try:
        assert m.main(argv, client_factory=no_jira) == 1
    except SystemExit as e:  # argparse itself refuses an option-looking value
        assert e.code == 2
    assert world.fake.gh == [] and world.fake.push == []


def test_feature_branch_names_are_accepted(world):
    git(world, "branch", "release/1.0")
    assert m.main(["pr", "create", KEY, "--base", "release/1.0", "--head", "feat/x", "--apply"], client_factory=no_jira) == 0


def test_base_and_head_default_to_origin_head_and_the_current_branch(world, capsys):
    git(world, "update-ref", "refs/remotes/origin/main", "main")
    git(world, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    assert m.main(["pr", "create", KEY, "--apply"], client_factory=no_jira) == 0
    assert world.fake.gh[0][world.fake.gh[0].index("--base") + 1] == "main" and world.fake.gh[0][world.fake.gh[0].index("--head") + 1] == "feat/x"
    git(world, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    assert m.main(["pr", "create", KEY], client_factory=no_jira) == 1 and "pass --base" in capsys.readouterr().err


def test_push_is_opt_in_list_form_and_only_with_apply(world, capsys):
    assert run("--push") == 0 and world.fake.push == []
    assert run("--push", "--apply") == 0
    push = world.fake.push[0]
    assert push[0] == "git" and push[-4:] == ["push", "-u", "origin", "refs/heads/feat/x:refs/heads/feat/x"] and not world.fake.kw[0].get("shell")
    assert world.fake.gh and "pushed: origin/feat/x" in capsys.readouterr().out
    assert run("--apply") == 0 and len(world.fake.push) == 1  # no --push: no push


def test_a_failed_push_stops_before_the_pr(world, capsys):
    world.fake.push_rc = 1
    assert run("--push", "--apply") == 1
    assert world.fake.gh == [] and "`git push` failed (exit code 1); no PR was created" in capsys.readouterr().err


def test_push_needs_a_local_branch(world, capsys):
    assert m.main(["pr", "create", KEY, "--base", "main", "--head", "nope/branch", "--push", "--apply"], client_factory=no_jira) == 1
    assert world.fake.push == [] and world.fake.gh == [] and "needs a local branch" in capsys.readouterr().err


def test_a_remote_branch_that_differs_is_refused_without_push(world, capsys):
    git(world, "update-ref", "refs/remotes/origin/feat/x", "main")
    assert run("--apply") == 1
    assert "different commit" in capsys.readouterr().err and world.fake.gh == []
    assert run("--push", "--apply") == 0  # --push brings it up to date


def test_an_unknown_origin_ref_refuses_the_apply_unless_pushing(world, capsys):
    """Minor 4: without --push the PR opens on whatever GitHub holds; with no origin/<head> here that is not the approved commit."""
    git(world, "update-ref", "-d", "refs/remotes/origin/feat/x")
    assert run("--plan-out", "p.md") == 0  # the preview still works and says so
    assert "| remote | origin/feat/x is not known here (apply refuses without --push) |" in (world / "p.md").read_text(encoding="utf-8")
    capsys.readouterr()
    assert run("--apply") == 1
    assert "not known on origin from here" in capsys.readouterr().err and world.fake.gh == [] and world.fake.push == []
    assert run("--push", "--apply") == 0 and len(world.fake.push) == 1 and len(world.fake.gh) == 1


def test_an_origin_ref_on_the_same_commit_applies(world):
    assert run("--apply") == 0 and len(world.fake.gh) == 1 and world.fake.push == []


# ---- X2: --expect-plan binds the apply to the approved preview ------------------------------------------------------------------

def test_apply_refuses_when_the_body_changed_after_approval(world, capsys):
    assert run("--plan-out", "plan.md") == 0
    set_ledger(world, ledger(rows="| AC-1 | a | t | | red |\n"))
    capsys.readouterr()
    assert run("--apply", "--expect-plan", "plan.md") == 2
    assert "plan changed since approval" in capsys.readouterr().err and world.fake.gh == [] and world.fake.push == []
    assert "| red |" in (world / "plan.md.new").read_text(encoding="utf-8")


def test_apply_proceeds_when_the_plan_matches(world):
    assert run("--plan-out", "plan.md") == 0
    assert run("--apply", "--expect-plan", "plan.md") == 0 and len(world.fake.gh) == 1


def test_a_commit_added_after_approval_refuses_the_apply(world, capsys):
    assert run("--push", "--plan-out", "plan.md") == 0
    commit(world, "SPK-1 added while paused at approval", {"f.txt": "two\n"})
    assert run("--push", "--apply", "--expect-plan", "plan.md") == 2
    assert world.fake.gh == [] and world.fake.push == [] and "| head commit |" in (world / "plan.md.new").read_text(encoding="utf-8")


def test_the_plan_is_canonical(world):
    assert run("--plan-out", "a.md") == 0 and run("--plan-out", "b.md") == 0
    assert (world / "a.md").read_bytes() == (world / "b.md").read_bytes()


# ---- the review and strict ------------------------------------------------------------------------------------------------------------

def _cfg(world, **extra):
    (world / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK", **extra}))


@pytest.mark.parametrize("block,ok", [
    (review(blocker=0), True), (review(blocker=1), False), (review(blocks="yes"), False), ("", False),
    (review(tail=" (superseded)"), False), (review().replace("blocker 0", "blocker 0 \u00b7 blocker 1"), False),
    (review().replace("- **Blocks PR:** no\n", ""), False), (review().replace("blocker 0 \u00b7 ", ""), False)])
def test_review_block_on_refuses_a_blocking_or_missing_review(world, capsys, block, ok):
    _cfg(world, review_block_on=["blocker"])
    set_ledger(world, ledger(review_block=block))
    assert run("--apply") == (0 if ok else 1)
    assert bool(world.fake.gh) == ok
    if not ok:
        assert capsys.readouterr().err.startswith("skj: refusing to open a PR: ")


def test_a_blocking_review_is_advisory_without_review_block_on(world):
    set_ledger(world, ledger(review_block=review(blocker=2, blocks="yes")))
    assert run("--apply") == 0
    assert "- **Confirmed findings:** blocker 2" in world.fake.kw[0]["input"].decode("utf-8")


def test_a_forged_review_in_a_fence_does_not_count(world):
    _cfg(world, review_block_on=["blocker"])
    fenced = "```\n" + review() + "```\n"
    set_ledger(world, ledger(review_block=fenced))
    assert run("--apply") == 1 and world.fake.gh == []


def test_strict_mode_runs_the_strict_pr_check_and_refuses_its_findings(world, capsys):
    _cfg(world, mode="strict", strict={"test_command": "python -m pytest"})  # L5b: the strict pr check runs (P1 refused "until L5b")
    assert run("--apply") == 2
    cap = capsys.readouterr()
    assert {"record-missing", "stack-profile-missing"} <= {f["rule"] for f in json.loads(cap.out)["findings"]}
    assert cap.err.startswith("skj: strict check failed: ") and world.fake.gh == [] and world.fake.push == []


def test_a_key_of_another_project_or_a_bad_key_is_refused(world, capsys):
    for ref in ("OTHER-1", "spk-1", "0", "SPK-1;x"):
        assert m.main(["pr", "create", ref, "--base", "main", "--head", "feat/x", "--apply"], client_factory=no_jira) == 2
    assert world.fake.gh == []


def test_the_ledger_must_be_readable(world, capsys):
    (world / "dod/DoD_SPK-1.md").unlink()
    assert run("--apply") == 1 and "cannot be read" in capsys.readouterr().err and world.fake.gh == []


# ---- P1-AC4: no int32 work-item id logic; keys are strings ------------------------------------------------------------------------

def test_no_int32_max_id_logic_and_ten_digit_keys_work(world):
    src = open(prs.__file__, encoding="utf-8").read() + open(m.__file__, encoding="utf-8").read()
    assert not hasattr(prs, "MAX_ID") and "2147483647" not in src and "MAX_ID" not in src
    assert prs.make_title("SPK-9999999999", "t") == "SPK-9999999999 t"  # a ten-digit key is longer than an int32
    with pytest.raises(ValueError):
        prs.make_title("SPK-99999999999", "t")


# ---- the template ships ---------------------------------------------------------------------------------------------------------------

def test_the_template_ships_and_every_placeholder_is_filled():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert (root / "src/skj/templates/pr-body.md").is_file() and prs.TEMPLATE == root / "src/skj/templates/pr-body.md"
    assert re.search(r"\[tool\.setuptools\.package-data\]\s*\n\s*skj\s*=\s*\[[^\]]*\"templates/\*\.md\"", (root / "pyproject.toml").read_text())
    names = set(re.findall(r"\{\{([a-z_]+)\}\}", prs._template()))
    assert names == {"summary", "key", "jira_url", "ac_rows", "ledger_path", "decisions", "gate_date", "gate_lines", "review_date", "review_lines"}
    text = prs._template()
    assert "Atlassian" not in text and "affiliated" not in text and "<!--" not in text  # no trademark claim and no hidden text in the template


def test_a_template_with_an_unknown_placeholder_or_a_value_that_looks_like_one(monkeypatch, tmp_path):
    t = tmp_path / "t.md"
    t.write_text("{{key}} {{nope}}")
    monkeypatch.setattr(prs, "TEMPLATE", t)
    with pytest.raises(ValueError, match="names a value skj does not know"):
        prs.build_body(config.Config("example-site.atlassian.net", "SPK"), KEY, ledger())
    t.write_text("{{summary}}")  # a value is never searched for placeholders
    assert prs.build_body(config.Config("example-site.atlassian.net", "SPK"), KEY, ledger(title="{{key}} {{x}}")) == "{{key}} {{x}}"
    monkeypatch.setattr(prs, "TEMPLATE", tmp_path / "missing.md")
    with pytest.raises(ValueError, match="template"):
        prs.build_body(config.Config("example-site.atlassian.net", "SPK"), KEY, ledger())


# ---- linear time (the ledger, the decisions file and tasks.md are untrusted) --------------------------------------------------------

_MIB = 1 << 20
_BOUND = 2.0


class _TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise _TooSlow()


def _timed(fn, *args):
    # CPU time, not the wall clock (H1b): load slows the clock, not the work; the timed calls run no subprocess and never block
    old = signal.signal(signal.SIGPROF, _alarm)
    signal.setitimer(signal.ITIMER_PROF, _BOUND)
    t0 = time.process_time()
    try:
        fn(*args)
    except _TooSlow:
        return _BOUND
    except ValueError:
        pass
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, old)
    return time.process_time() - t0


needs_timer = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="no setitimer on this platform")
CFG = config.Config("example-site.atlassian.net", "SPK")
SHAPES = {
    "1mib-one-line": "# DoD \u2014 SPK-1: " + "| \\\u202e  @" * (_MIB // 6),
    "1mib-in-section-4": ledger(gate_block="### Gate\n- traceability: " + "[skj: @\\|\u202e " * (_MIB // 14) + "\n"),
    "1mib-review-line": ledger(review_block="### Review\n- **Confirmed by severity:** " + "blocker " * (_MIB // 8) + "\n"),
    "500x1990": ledger(rows="".join("| AC-%d | %s | %s | | %s |\n" % (i, "x", ("|\\ \t\u200b@" * 400)[:1980], "y" * 1000) for i in range(1, 501))),
    "20000-short": ledger(rows="| AC-1 | a | t | | g |\n" * 20000, gate_block="### Gate\n" * 20000, review_block="### Review\n" * 20000),
    "20000-headings": "# DoD\n" + dod.SECTION2 + "\n## 4. Gate & review log\n" + "### Review (superseded)\n" * 20000,
}


@needs_timer
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_body_building_is_linear(shape):
    assert _timed(prs.build_body, CFG, KEY, SHAPES[shape]) < _BOUND
    assert _timed(prs.review_summary, SHAPES[shape]) < _BOUND


@needs_timer
@pytest.mark.parametrize("text", [" " * _MIB, "\u202e" * _MIB, "x" * _MIB, "@" * _MIB, "[skj:" * (_MIB // 5), "SPK-1 " + "#" * _MIB, "SPK-1 " * (_MIB // 6)],
                         ids=["blanks", "bidi", "long", "ats", "markers", "hashes", "keys"])
def test_text_helpers_are_linear(text):
    assert _timed(prs.neutral, text) < _BOUND
    assert _timed(prs.clean, text, 200) < _BOUND
    assert _timed(prs.check_body, text[:prs.MAX_BODY], KEY) < _BOUND
    assert _timed(prs.make_title, KEY, text) < _BOUND
    assert _timed(prs.pr_url, text) < _BOUND


# ---- A4: comment mode needs the approver's `/approve approve-pr <hash of this plan>` before the push and `gh` --------------------------

APPROVER = "5b10ac8d82e05b22cc7d4ef5"  # invented accountId


def _comment_mode_on_main(root):
    """The TARGET branch `main` sets approval "comment" with one approver (committed there; the feature branch keeps its own config)."""
    git(root, "switch", "-q", "main")
    (root / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK", "approvers": [APPROVER],
                                                      "approval": "comment"}))
    git(root, "commit", "-qam", "comment mode")
    git(root, "switch", "-q", "feat/x")


def test_a4_pr_create_in_comment_mode_needs_the_approval_of_this_plan(world, capsys):
    from fakejira import ME_ID, FakeJira
    from skj import adf, approvals
    _comment_mode_on_main(world)
    fake = FakeJira()
    fake.add_issue("A fictional story")

    def pr(*argv):
        return m.main(["pr", "create", KEY, "--base", "main", "--head", "feat/x", *argv], client_factory=fake.client)
    assert pr("--plan-out", "p.md") == 0 and fake.log == []  # the preview never needs Jira
    h = approvals.gate_hash("approve-pr", KEY, (world / "p.md").read_bytes())
    assert pr("--apply", "--expect-plan", "p.md") == 2 and world.fake.gh == []  # no approval: nothing sent, no gh
    assert capsys.readouterr().err.splitlines()[-1].startswith("skj: comment approval: no valid approval for this plan")
    assert all(e["method"] == "GET" for e in fake.log)
    fake.add_comment(KEY, adf.md_to_adf("/approve approve-pr %s" % h), author=fake.user(ME_ID))  # the agent's own: never counts
    assert pr("--apply", "--expect-plan", "p.md") == 2 and world.fake.gh == []
    fake.add_comment(KEY, adf.md_to_adf("/approve approve-pr %s" % approvals.gate_hash("approve-pr", KEY, b"another plan\n")),
                     author=fake.user(APPROVER))  # an approval of another plan
    assert pr("--apply", "--expect-plan", "p.md") == 2 and world.fake.gh == []
    fake.add_comment(KEY, adf.md_to_adf("/approve approve-pr %s" % h), author=fake.user(APPROVER))
    capsys.readouterr()
    assert pr("--apply", "--expect-plan", "p.md") == 0 and "created: " in capsys.readouterr().out
    assert fake.writes() == [] and len(world.fake.gh) == 1


def test_a4_pr_create_in_terminal_mode_builds_no_jira_client(world, capsys):
    assert run("--apply") == 0 and "created: " in capsys.readouterr().out  # `run` passes a client factory that refuses


def test_a4_pr_create_comment_mode_on_origin_head_cannot_be_dropped_by_base(world, capsys):
    """Fix round 1: the default branch (origin/HEAD) sets approval "comment"; `--base release` names a branch whose config is terminal:
    the stricter side still demands the approval (exit 2, no gh call)."""
    from fakejira import FakeJira
    git(world, "branch", "release", "main")  # before main turns comment mode on: release keeps the terminal config
    _comment_mode_on_main(world)
    git(world, "update-ref", "refs/remotes/origin/main", "main")
    git(world, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    fake = FakeJira()
    fake.add_issue("A fictional story")
    assert m.main(["pr", "create", KEY, "--base", "release", "--head", "feat/x", "--apply"], client_factory=fake.client) == 2
    assert capsys.readouterr().err.splitlines()[-1].startswith("skj: comment approval: no valid approval for this plan")
    assert world.fake.gh == [] and fake.writes() == []


def test_a4_pr_create_apply_with_an_unresolvable_base_fails_closed(world, capsys):
    """Fix round 1 (intentional behaviour change): `--apply` reads the trust of the target side (`_stricter_trust`) also in terminal
    mode, so a --base that is no commit here refuses (exit 1) before the push and gh, as L5a's 'unresolved base fails closed'."""
    assert m.main(["pr", "create", KEY, "--base", "nosuch", "--head", "feat/x", "--apply"], client_factory=no_jira) == 1
    assert "not a commit" in capsys.readouterr().err.splitlines()[-1] and world.fake.gh == [] and world.fake.push == []
