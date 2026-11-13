"""CI trust (ported from the fork's tests/test_ci_trust.py, L5a). With a base (`--base REF`, else origin/HEAD), `gate`, `strict check`,
`strict verify`, `strict mode` and `stack show` use the config of the PR's target branch when it is strict there: a PR cannot weaken (or
re-point) its own strictness. Real temp git repos; the target branch is refs/remotes/origin/main. The fork's kit-lock cases are
ported at the end (L5b), its rules case in RU3 (`test_pr_disabling_rules_still_judged_enabled`); its `branch --id-from` (C1) cases are
listed in task-L5a-report.md / task-L5b-report.md."""
import hashlib
import json
import os
import shutil
import subprocess

import pytest

from skj import __main__ as m
from skj import config, decisions, kits, strict
from skj.__main__ import main
from skj.dod import SECTION2
from test_stack_support import _IDENTITY, approve, commit, git, profile, git_env  # noqa: F401  (git_env: fixture)
from test_strict import (CMD, KEY, LEDGER, MARK, RED_NOTE, SITE, STACK_CMD, _core_only, _d, _ledger_rows, _pair, _project, _repo,  # noqa: F401
                         _no_credentials, _stub_detect, all_checks, real_board, real_kits, real_review, real_detect)
from test_strict_wire import _full, _wire

ORG = {"site": SITE, "project": "SPK"}
STRICT = {**ORG, "mode": "strict"}
LINE = "config: target branch (strict)"


def _cfg_text(cfg):
    return cfg if isinstance(cfg, str) else json.dumps(cfg)


# ---- gate ----
def _gate_pr(tmp_path, monkeypatch, git_env, target, head, decisions_md=None, target_files=None, head_files=None):
    """main holds `target` (a dict, raw text or None) = refs/remotes/origin/main; the branch `feature` (checked out) holds `head` and
    a ledger with one AC whose test does not exist (AC traceability finds it) and, optionally, a decisions file."""
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "SPK-1001 base", {"README.md": "x\n", **({"skj.config.json": _cfg_text(target)} if target is not None else {}),
                                      **(target_files or {})})
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    files = {"dod/DoD_SPK-1001.md": f"{SECTION2}\n## 3. AC\n| AC | x | tests | ev | status |\n|---|---|---|---|---|\n| AC-1 | x | Gone.testX | | red |\n",
             **(head_files or {})}
    if head is not None:
        files["skj.config.json"] = _cfg_text(head)
    if decisions_md is not None:
        files["dod/Decisions_SPK-1001.md"] = decisions_md
    commit(tmp_path, "SPK-1001 feature", files)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    monkeypatch.delenv("SKJ_TOKEN", raising=False)
    return str(tmp_path / "out" / "gate.md")


def _gate(capsys, summary, *extra):
    rc = main(["gate", "SPK-1001", "--offline", "--src", ".", "--summary-out", summary, *extra])
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


def _md(summary):
    return open(summary, encoding="utf-8").read()


def test_target_strict_overrides_head_advisory(tmp_path, monkeypatch, capsys, git_env):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)  # the PR says advisory
    rc, out = _gate(capsys, summary, "--base", "origin/main")
    assert rc == 1 and out["blocking"] is True and "ac_traceability" in out["blocked_by"]  # judged strict: the floor blocks
    assert _gate(capsys, summary)[1]["config"] == "target branch (strict)"  # DF3 M2: no origin/HEAD: judged by the strict origin/main
    git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")  # D-023: v0.2 needs no strict origin/main beside an unset origin/HEAD
    assert _gate(capsys, summary)[0] == 0  # the head config alone (no --base): advisory, v0.2


def test_head_cannot_lower_gate_block(tmp_path, monkeypatch, capsys, git_env):
    dec = decisions.render_file(KEY) + _d(1, "use the v2 api")  # an open decision
    target = {**STRICT, "gate": {"block": ["open_decisions"]}}  # a project-chosen entry beyond the strict floor
    head = {**ORG, "gate": {"block": []}, "review_block_on": []}
    summary = _gate_pr(tmp_path, monkeypatch, git_env, target, head, dec)
    rc, out = _gate(capsys, summary, "--base", "origin/main")
    assert rc == 1 and {"open_decisions", "ac_traceability"} <= set(out["blocked_by"])
    assert _gate(capsys, summary)[1]["config"] == "target branch (strict)"  # DF3 M2: no origin/HEAD: judged by the strict origin/main
    git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")  # D-023: v0.2 needs no strict origin/main beside an unset origin/HEAD
    rc, out = _gate(capsys, summary)  # without --base the PR's own lower list applies (v0.2)
    assert rc == 0 and out["blocked_by"] == []


@pytest.mark.rules
def test_pr_disabling_rules_still_judged_enabled(tmp_path, monkeypatch, capsys, git_env):
    """RU3 (the fork's R§13 AC 2): the strict target enables rules; the PR's own config switches them off (or drops the key). The target's
    config judges the run: the `rules` result is there and `rule_coverage` is in the floor, so the missing `Rules from:` line blocks."""
    target = {**STRICT, "rules": {"enabled": True}}
    for n, head in enumerate(({**ORG, "rules": {"enabled": False}}, ORG)):
        repo = tmp_path / f"r{n}"
        repo.mkdir()
        summary = _gate_pr(repo, monkeypatch, git_env, target, head)
        rc, out = _gate(capsys, summary, "--base", "origin/main")
        assert rc == 1 and out["config"] == "target branch (strict)", head
        assert out["rules"]["header"] == ["rules-from"] and "rule_coverage" in out["blocked_by"]
        assert "Business rules" in _md(summary) and "skj dod refresh" in _md(summary)
        assert _gate(capsys, summary)[1]["config"] == "target branch (strict)"  # DF3 M2: no origin/HEAD: judged by the strict origin/main
        git(repo, "update-ref", "-d", "refs/remotes/origin/main")  # D-023: v0.2 needs no strict origin/main beside an unset origin/HEAD
        rc, out = _gate(capsys, summary)  # no --base: origin/HEAD is not set here, so the PR's own config: rules disabled, the baseline
        assert rc == 0 and "rules" not in out
    repo = tmp_path / "advisory-target"  # a strict target that says disabled: the PR cannot enable rules for itself
    repo.mkdir()
    summary = _gate_pr(repo, monkeypatch, git_env, {**STRICT, "rules": {"enabled": False}}, {**ORG, "rules": {"enabled": True}})
    rc, out = _gate(capsys, summary, "--base", "origin/main")
    assert "rules" not in out


def test_target_missing_or_advisory_uses_head(tmp_path, monkeypatch, capsys, git_env):
    head = {**ORG, "gate": {"block": ["ac_traceability"]}}
    for target in (None, ORG, {**ORG, "mode": "advisory", "gate": {"block": []}}):
        repo = tmp_path / f"r{len(os.listdir(tmp_path))}"
        repo.mkdir()
        summary = _gate_pr(repo, monkeypatch, git_env, target, head)
        rc, out = _gate(capsys, summary, "--base", "origin/main")
        assert rc == 1 and out["blocked_by"] == ["ac_traceability"], target  # the head's own list, as in v0.2
        assert "config" not in out and LINE not in _md(summary) and "config:" not in _md(summary)
    repo = tmp_path / "strict-head"  # a head that adds strictness is honoured when the target has none
    repo.mkdir()
    summary = _gate_pr(repo, monkeypatch, git_env, None, STRICT)
    rc, out = _gate(capsys, summary, "--base", "origin/main")
    assert rc == 1 and "ac_traceability" in out["blocked_by"] and out["config"] == "pull request"  # judged by the PR's own strict file


@pytest.mark.parametrize("target", ['{"site": "contoso"}', "{not json", "[]", '{"site": "example-site.atlassian.net", "project": "SPK", "mode": "lax"}'])
def test_target_invalid_exit_3(tmp_path, monkeypatch, capsys, git_env, target):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, target, ORG)  # a valid head config does not rescue it
    rc, out = _gate(capsys, summary, "--base", "origin/main")
    assert rc == 3 and out is None
    md = _md(summary)
    assert "skj.config.json is invalid" in md and str(tmp_path) not in md and "origin/main" not in md.replace("target branch", "")


def test_git_failure_exit_3(tmp_path, monkeypatch, capsys, git_env):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, STRICT)  # git cannot run: the base cannot be resolved, the PR's config is strict
    monkeypatch.setenv("PATH", "")  # git cannot be run at all
    assert _gate(capsys, summary, "--base", "origin/main")[0] == 3
    assert "skj.config.json is invalid" in _md(summary)
    monkeypatch.undo()
    monkeypatch.chdir(tmp_path)
    blob = git(tmp_path, "rev-parse", "origin/main:skj.config.json")  # the object is gone: git fails, the file is not "absent"
    os.remove(tmp_path / ".git" / "objects" / blob[:2] / blob[2:])
    assert _gate(capsys, summary, "--base", "origin/main")[0] == 3
    assert "skj.config.json is invalid" in _md(summary)


@pytest.mark.parametrize("base", ["origin/nope", "nope", "", "--output=x", "origin/main:skj.config.json", "abc"])
def test_unresolvable_base_fails_closed(tmp_path, monkeypatch, capsys, git_env, base):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, STRICT)  # an advisory PR config keeps v0.2: see the next tests
    # DF3 M2: a plain unknown name falls back to the strict origin/main (blocks); a revision expression or an option is refused (exit 3)
    assert _gate(capsys, summary, f"--base={base}")[0] == (3 if config._BAD_BASE.search(base) else 1)
    assert not (tmp_path / "x").exists()


def test_tag_named_like_the_base_cannot_shadow(tmp_path, monkeypatch, capsys, git_env):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)
    commit(tmp_path, "no story reference", {"extra.txt": "x\n"})
    git(tmp_path, "tag", "origin/main", "HEAD")  # a tag on the PR's own (lax) commit, named like the remote-tracking branch
    git(tmp_path, "tag", "main", "HEAD")
    rc, out = _gate(capsys, summary, "--base", "origin/main")
    assert rc == 1 and "ac_traceability" in out["blocked_by"] and out["config"] == "target branch (strict)"
    assert len(out["commits_without_story"]) == 1 and "commits_without_story" in out["blocked_by"]  # the range is origin/main's, not the tag's
    assert LINE in _md(summary)


def test_advisory_head_unresolvable_base_keeps_v02(tmp_path, monkeypatch, capsys, git_env):
    head = {**ORG, "gate": {"block": ["ac_traceability"]}}
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, head)  # the target is strict, but the base given cannot be read here
    short = git(tmp_path, "rev-parse", "--short", "HEAD")
    rc, out = _gate(capsys, summary, "--base", "origin/nosuch")
    assert rc == 1 and out["config"] == "target branch (strict)"  # DF3 M2: the fallback is the strict origin/main
    capsys.readouterr()
    git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")  # D-023: v0.2 needs no strict origin/main beside an unset origin/HEAD
    for base in ("origin/nosuch", "HEAD~5", short, "nosuch"):
        rc, out = _gate(capsys, summary, "--base", base)
        assert rc == 1 and out["blocked_by"] == ["ac_traceability"], base  # blocks on the PR's own list, as v0.2
        assert out["commits_without_story"] == [] and "config" not in out and "config:" not in _md(summary), base
        assert capsys.readouterr().err == ""
    # (origin/main was deleted above) the lifecycle workflow's fallback: origin/main in a repo with no origin
    rc, out = _gate(capsys, summary, "--base", "origin/main")
    assert rc == 1 and "config" not in out
    mod = m  # strict check with an advisory head and an unknown base: v0.2 answers (advisory: ok)
    for phase in ("pr", "done"):
        rc, out = _cli(mod, capsys, "strict", "check", "SPK-1001", "--phase", phase, "--base", "origin/nosuch")
        assert rc == 0 and out["mode"] == "advisory"


def test_strict_head_unresolvable_base_exit_3(tmp_path, monkeypatch, capsys, git_env):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, STRICT)
    git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")  # DF3 M2: else the unknown base falls back to the strict origin/main
    assert _gate(capsys, summary, "--base", "origin/nosuch")[0] == 3
    assert "skj.config.json is invalid" in _md(summary)
    for argv in (["strict", "check", "SPK-1001", "--phase", "pr"], ["strict", "verify", "SPK-1001", "--no-record"]):
        rc, out = _cli(m, capsys, *argv, "--base", "origin/nosuch")
        assert rc == 2 and out is None  # an error: line on stderr, no JSON


def test_a_resolvable_base_with_an_unreadable_target_fails_closed_even_for_an_advisory_head(tmp_path, monkeypatch, capsys, git_env):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, "{not json", ORG)
    assert _gate(capsys, summary, "--base", "origin/main")[0] == 3  # the base is fine: the target's file is the problem


def test_summary_says_pull_request_when_the_prs_own_strict_config_judged(tmp_path, monkeypatch, capsys, git_env):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, None, STRICT)  # the first PR that introduces strict mode
    rc, out = _gate(capsys, summary, "--base", "origin/main")
    assert out["config"] == "pull request" and _md(summary).splitlines().count("config: pull request") == 1
    assert _gate(capsys, summary)[1].get("config") is None  # no --base: nothing added

def test_summary_names_the_config_source(tmp_path, monkeypatch, capsys, git_env):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)
    _gate(capsys, summary, "--base", "origin/main")
    assert _md(summary).splitlines().count(LINE) == 1  # exactly one extra line
    (tmp_path / "dod" / "DoD_SPK-1001.md").unlink()  # the ledger-missing note shape names it too
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "SPK-1001 drop")
    _gate(capsys, summary, "--base", "origin/main")
    assert _md(summary).splitlines().count(LINE) == 1
    other = tmp_path / "advisory"
    other.mkdir()
    s2 = _gate_pr(other, monkeypatch, git_env, None, ORG)
    _gate(capsys, s2, "--base", "origin/main")
    assert "config:" not in _md(s2)  # the head's own config: nothing extra


def test_no_base_flag_unchanged(tmp_path, monkeypatch, capsys, git_env):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)  # a strict target exists, but no --base is given
    assert _gate(capsys, summary)[1]["config"] == "target branch (strict)"  # DF3 M2: no origin/HEAD: judged by the strict origin/main
    git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")  # D-023: v0.2 needs no strict origin/main beside an unset origin/HEAD
    monkeypatch.setattr(config.Config, "load_from_ref", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no --base: no ref is read")))
    rc, out = _gate(capsys, summary)
    assert rc == 0 and "config" not in out and out["blocked_by"] == []  # the PR's own advisory config, as in v0.2
    assert "config:" not in _md(summary)


def _target(repo, cfg, parent, ref="refs/remotes/origin/trunk", files=None):
    """A commit with `skj.config.json` = cfg (and `files`: path -> text, or (mode, text); None removes it) on top of `parent` (not on the
    checked-out branch), set as `ref`. cfg None and no files: the parent as it is."""
    files = {**({"skj.config.json": _cfg_text(cfg)} if cfg is not None else {}), **(files or {})}
    if not files:
        git(repo, "update-ref", ref, parent)
        return
    env = {**_IDENTITY, **os.environ, "GIT_INDEX_FILE": str(repo / ".git" / "trust-index")}  # commit-tree needs an identity (a hosted runner has none)

    def run(*a, inp=None):
        return subprocess.run(["git", "-C", str(repo), *a], env=env, input=inp, capture_output=True, text=True, check=True).stdout.strip()
    run("read-tree", parent)
    for path, text in files.items():
        if text is None:
            run("update-index", "--force-remove", path)
            continue
        mode, text = text if isinstance(text, tuple) else ("100644", text)
        blob = run("hash-object", "-w", "--stdin", inp=text)
        run("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}")
    sha = run("commit-tree", run("write-tree"), "-p", parent, "-m", "target")
    git(repo, "update-ref", ref, sha)
    os.remove(repo / ".git" / "trust-index")


def _cli(mod, capsys, *argv):
    rc = mod.main(list(argv))
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


def test_strict_check_base_uses_the_target_config(tmp_path, monkeypatch, capsys, git_env):
    mod, s = _wire(tmp_path, monkeypatch, git_env, mode=None, committed=True)  # the PR's config (committed, with the profile) says advisory
    _target(tmp_path, {**STRICT, "strict": {"test_command": CMD}}, _full(tmp_path, s.base))
    argv = ["strict", "check", "SPK-1001", "--phase", "pr"]
    rc, out = _cli(mod, capsys, *argv, "--base", "origin/trunk")
    assert (rc, out["mode"], out["ok"], out["findings"]) == (0, "strict", True, [])
    rc, out = _cli(mod, capsys, *argv, "--base", _full(tmp_path, s.base))  # a commit id: no ref, no target config here
    assert out["mode"] == "advisory"
    rc, out = _cli(mod, capsys, "strict", "check", "SPK-1001", "--phase", "done")  # no --base: nothing changes
    assert out["mode"] == "advisory"


def test_strict_check_with_a_bad_base_fails_closed(tmp_path, monkeypatch, capsys, git_env):
    mod, s = _wire(tmp_path, monkeypatch, git_env)  # the PR's own config is strict
    assert mod.main(["strict", "check", "SPK-1001", "--phase", "pr", "--base", "nope"]) == 2  # no head config can wave an unknown target through


def test_head_cannot_repoint_test_command(tmp_path, monkeypatch, capsys, git_env):
    lax = "grep -q 'x = 1' src/app.py"  # passes at the red commit: the PR's own command would call red a pass
    mod, s = _wire(tmp_path, monkeypatch, git_env)
    (tmp_path / "skj.config.json").write_text(json.dumps({**STRICT, "strict": {"test_command": lax}}))
    _target(tmp_path, {**STRICT, "strict": {"test_command": CMD}}, _full(tmp_path, s.base))
    rc, out = _cli(mod, capsys, "strict", "verify", "SPK-1001")  # the PR's config: its own command, red passes
    assert rc == 2 and "red-passes" in [f["rule"] for f in out["findings"]]
    rc, out = _cli(mod, capsys, "strict", "verify", "SPK-1001", "--base", "origin/trunk")
    assert rc == 0 and out["recorded"] is True
    assert f"command-sha256: {hashlib.sha256(CMD.encode()).hexdigest()[:12]}" in LEDGER(tmp_path).read_text()


def test_head_gate_tests_glob_cannot_make_every_file_a_test(tmp_path, monkeypatch, capsys, git_env):
    repo = _repo(tmp_path, git_env)
    base = git(repo, "rev-parse", "HEAD")
    red, green = _pair(repo, red_files={"tests/test_a.py": "def test_a():\n    assert 0\n", "src/other.py": "y = 1\n"})  # red touches code
    client = _project(tmp_path, ledger=_ledger_rows(("AC-1", RED_NOTE.format(red, green))), test_command=CMD)
    head = {**STRICT, "strict": {"test_command": CMD}, "gate": {"tests": {"**": "."}}}  # every file is a test
    (tmp_path / "skj.config.json").write_text(json.dumps(head))
    _target(tmp_path, {**STRICT, "strict": {"test_command": CMD}}, base)  # the target keeps the default globs
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    argv = ["strict", "check", "SPK-1001", "--phase", "pr"]
    _, out = _cli(m, capsys, *argv, "--base", _full(tmp_path, base))  # a commit id: no target config, the PR's own glob
    assert "red-touches-code" not in [f["rule"] for f in out["findings"]]
    rc, out = _cli(m, capsys, *argv, "--base", "origin/trunk")
    assert rc == 2 and "red-touches-code" in [f["rule"] for f in out["findings"]]


def test_strict_verify_base_cannot_be_shadowed_by_a_tag(tmp_path, monkeypatch, capsys, git_env):
    mod, s = _wire(tmp_path, monkeypatch, git_env)
    git(tmp_path, "update-ref", "refs/remotes/origin/trunk", s.green)  # the real base already holds red and green
    git(tmp_path, "tag", "origin/trunk", s.base)  # a tag named like it, before them
    rc, out = _cli(mod, capsys, "strict", "verify", "SPK-1001", "--no-record", "--base", "origin/trunk")
    assert rc == 2 and [f["rule"] for f in out["findings"]] == ["not-in-range", "not-in-range"]
    git(tmp_path, "update-ref", "refs/remotes/origin/trunk", s.base)
    assert _cli(mod, capsys, "strict", "verify", "SPK-1001", "--no-record", "--base", "origin/trunk")[0] == 0
    for base in ("v-none", "", "refs/tags/origin/trunk"):  # nothing that is a tag or unknown stands in for the base
        assert _cli(mod, capsys, "strict", "verify", "SPK-1001", "--no-record", "--base", base)[0] == 2


# ---- V3-14a: `skj strict mode [--base REF]`: the mode CI would judge a PR by (read-only, no token, no network) ----
def _mode(monkeypatch, capsys, *extra):
    rc = main(["strict", "mode", *extra])
    cap = capsys.readouterr()
    return rc, (json.loads(cap.out) if cap.out.strip() else None), cap.err


TARGET, PULL = "target branch", "pull request"


@pytest.mark.parametrize("target,head,mode,source", [
    (STRICT, ORG, "strict", TARGET),  # the PR says advisory: the target's strict config wins
    (STRICT, None, "strict", TARGET),  # a PR that deletes its config
    (STRICT, {**ORG, "mode": "advisory"}, "strict", TARGET),
    (None, ORG, "advisory", PULL),
    (ORG, ORG, "advisory", PULL),
    ({**ORG, "mode": "advisory", "gate": {"block": ["ac_traceability"]}}, ORG, "advisory", PULL),
    (None, STRICT, "strict", PULL),  # the first PR that introduces strict mode: its own config
    (ORG, STRICT, "strict", PULL),  # a PR that adds strictness to an advisory target
])
def test_strict_mode_says_which_config_judges_the_pr(tmp_path, monkeypatch, capsys, git_env, target, head, mode, source):
    _gate_pr(tmp_path, monkeypatch, git_env, target, head)
    rc, out, err = _mode(monkeypatch, capsys, "--base", "origin/main")
    assert (rc, out, err) == (0, {"mode": mode, "source": source}, "")


def test_strict_mode_without_base_is_the_pr_config_and_reads_no_ref(tmp_path, monkeypatch, capsys, git_env):
    _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)
    assert _mode(monkeypatch, capsys) == (0, {"mode": "strict", "source": TARGET}, "")  # DF3 M2: no origin/HEAD: the strict origin/main
    git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")  # D-023: v0.2 needs no strict origin/main beside an unset origin/HEAD
    monkeypatch.setattr(config.Config, "load_from_ref", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no --base: no ref is read")))
    assert _mode(monkeypatch, capsys)[:2] == (0, {"mode": "advisory", "source": PULL})


@pytest.mark.parametrize("target", ['{"site": "contoso"}', "{not json", "[]", '{"site": "example-site.atlassian.net", "project": "SPK", "mode": "lax"}'])
def test_strict_mode_with_an_invalid_target_exit_3(tmp_path, monkeypatch, capsys, git_env, target):
    _gate_pr(tmp_path, monkeypatch, git_env, target, ORG)  # a valid head config does not rescue it
    rc, out, err = _mode(monkeypatch, capsys, "--base", "origin/main")
    assert rc == 3 and out is None and err.startswith("skj: ") and err.count("\n") == 1
    assert str(tmp_path) not in err and "origin/main" not in err.replace("target branch", "")  # fixed text: no path, no ref


def test_strict_mode_with_an_invalid_head_config_exit_3(tmp_path, monkeypatch, capsys, git_env):
    _gate_pr(tmp_path, monkeypatch, git_env, None, '{"site": SITE, "project": "SPK", "gate": {"block": ["nope"]}}')
    assert _mode(monkeypatch, capsys, "--base", "origin/main")[0] == 3
    assert _mode(monkeypatch, capsys)[0] == 3


@pytest.mark.parametrize("base", ["origin/nosuch", "nosuch", "abc", "--output=x", "origin/main:skj.config.json"])
def test_strict_mode_unresolvable_base_under_a_strict_pr_config_fails_closed(tmp_path, monkeypatch, capsys, git_env, base):
    _gate_pr(tmp_path, monkeypatch, git_env, STRICT, STRICT)
    rc, out, err = _mode(monkeypatch, capsys, f"--base={base}")
    if config._BAD_BASE.search(base):
        assert rc == 3 and out is None and err.startswith("skj: ") and not (tmp_path / "x").exists()
    else:  # DF3 M2: a plain unknown name falls back to the strict origin/main
        assert (rc, out) == (0, {"mode": "strict", "source": TARGET})


@pytest.mark.parametrize("base", ["origin/nosuch", "HEAD~5", "nosuch", "--output=x"])
def test_strict_mode_unresolvable_base_with_an_advisory_pr_config_keeps_advisory(tmp_path, monkeypatch, capsys, git_env, base):
    _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)  # the target is strict, but this base cannot be read here: the v0.2 path
    if base not in ("HEAD~5", "--output=x"):  # a revision expression or an option never falls back to origin/HEAD (D-016)
        assert _mode(monkeypatch, capsys, f"--base={base}") == (0, {"mode": "strict", "source": TARGET}, ""), base  # DF3 M2: origin/main
    git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")  # D-023: v0.2 needs no strict origin/main beside an unset origin/HEAD
    assert _mode(monkeypatch, capsys, f"--base={base}") == (0, {"mode": "advisory", "source": PULL}, "")


def test_strict_mode_without_any_config_is_a_plain_error_not_fail_closed(tmp_path, monkeypatch, capsys, git_env):
    _gate_pr(tmp_path, monkeypatch, git_env, None, None)  # no config anywhere: the templates treat this as advisory
    rc, out, err = _mode(monkeypatch, capsys, "--base", "origin/main")
    assert rc == 2 and out is None and err.startswith("skj: ")


def test_strict_mode_a_tag_named_like_the_base_cannot_shadow_it(tmp_path, monkeypatch, capsys, git_env):
    _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)
    git(tmp_path, "tag", "origin/main", "HEAD")  # a tag on the PR's own commit
    assert _mode(monkeypatch, capsys, "--base", "origin/main")[:2] == (0, {"mode": "strict", "source": TARGET})


def test_strict_mode_needs_no_token_and_makes_no_request(tmp_path, monkeypatch, capsys, git_env):
    _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)
    monkeypatch.delenv("SKJ_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no keychain, no stored token
    assert _mode(monkeypatch, capsys, "--base", "origin/main")[0] == 0


# ---- P2-5b: the stack profile and the kit lockfile come from the TARGET branch ----
NOTE = "trust-introducing PR: review skj.stack.json as code"
STACK_STRICT = {**STRICT, "strict": {"test_command": STACK_CMD}}
OVERSIZE = "{" + " " * (256 * 1024 + 1) + "}"
TOKEN = "gh" + "p_" + "a" * 36  # built here so this file holds no secret-looking literal
BAD_PROFILES = {
    "parse": "{not json " + MARK,
    "duplicate-key": '{"schema": 1, "schema": 1, "x": "' + MARK + '"}',
    "schema": json.dumps({**profile(), "version": MARK}),
    "unknown-key": json.dumps({**profile(), MARK: 1}),
    "secret": json.dumps({**profile(), "sources": {"S1": {**profile()["sources"]["S1"], "evidence": TOKEN + MARK}, "S2": profile()["sources"]["S2"]}}),
    "oversize": OVERSIZE,
}
BAD_LOCKS = {
    "parse": "{not json " + MARK,
    "duplicate-key": '{"schema": 1, "schema": 1, "kits": {}}',
    "schema": json.dumps({"schema": 2, "kits": {}, "x": MARK}),
    "not-object": "[]",
    "kit-id": json.dumps({"schema": 1, "kits": {"Bad Id " + MARK: {}}}),
    "secret": json.dumps({"schema": 1, "kits": {"sf": {"source": TOKEN + MARK}}}),
    "oversize": OVERSIZE,
}


def _all_output(capsys, summary=None):
    cap = capsys.readouterr()
    return cap.out + cap.err + (_md(summary) if summary and os.path.exists(summary) else "")


@pytest.mark.parametrize("raw", BAD_PROFILES.values(), ids=BAD_PROFILES)
def test_invalid_target_profile_gate_exit_3_check_exit_2(tmp_path, monkeypatch, capsys, git_env, raw):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG, target_files={"skj.stack.json": raw})
    assert _gate(capsys, summary, "--base", "origin/main")[0] == 3
    md = _md(summary)
    assert "skj.stack.json at the target branch" in md and MARK not in md and TOKEN not in md and "origin/main" not in md.replace("target branch", "")
    capsys.readouterr()
    rc, out = _cli(m, capsys, "strict", "check", "SPK-1001", "--phase", "implement", "--base", "origin/main")
    err = _all_output(capsys)
    assert (rc, out) == (2, None)  # an error: line on stderr, no JSON
    assert _cli(m, capsys, "strict", "verify", "SPK-1001", "--no-record", "--base", "origin/main")[0] == 2
    assert _cli(m, capsys, "strict", "mode", "--base", "origin/main")[0] == 3
    assert MARK not in err and TOKEN not in err


@pytest.mark.parametrize("raw", BAD_LOCKS.values(), ids=BAD_LOCKS)
def test_invalid_target_lock_gate_exit_3(tmp_path, monkeypatch, capsys, git_env, raw):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG, target_files={"skj.kits.lock.json": raw})
    assert _gate(capsys, summary, "--base", "origin/main")[0] == 3
    md = _md(summary)
    assert "skj.kits.lock.json at the target branch" in md and MARK not in md and TOKEN not in md
    assert _cli(m, capsys, "strict", "mode", "--base", "origin/main")[0] == 3
    assert MARK not in _all_output(capsys)


def test_valid_target_profile_and_lock_do_not_fail_the_config(tmp_path, monkeypatch, capsys, git_env):
    files = {"skj.stack.json": json.dumps(approve(profile())), "skj.kits.lock.json": json.dumps({"schema": 1, "kits": {"sf": {"version": "1.0.0"}}})}
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG, target_files=files)
    rc, out = _gate(capsys, summary, "--base", "origin/main")
    assert rc == 1 and out["config"] == "target branch (strict)"  # judged strict by the target (AC traceability blocks), not exit 3
    assert _cli(m, capsys, "strict", "mode", "--base", "origin/main")[1] == {"mode": "strict", "source": "target branch"}


def test_target_blobs_are_read_through_blob_at_with_the_cap_and_the_labels(tmp_path, monkeypatch, capsys, git_env):
    _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG, target_files={"skj.stack.json": json.dumps(approve(profile()))})
    calls, real = [], config._blob_at
    monkeypatch.setattr(config, "_blob_at", lambda *a, **k: calls.append(a[2:]) or real(*a, **k))
    assert _cli(m, capsys, "strict", "mode", "--base", "origin/main")[0] == 0
    assert calls == [("skj.config.json", 1024 * 1024, "skj.config.json at the target branch"),
                     ("skj.stack.json", 256 * 1024, "skj.stack.json at the target branch"),
                     ("skj.kits.lock.json", 256 * 1024, "skj.kits.lock.json at the target branch")]


def test_advisory_or_absent_target_config_reads_no_stack_files(tmp_path, monkeypatch, capsys, git_env):
    for n, target in enumerate((None, ORG)):
        repo = tmp_path / f"r{n}"
        repo.mkdir()
        _gate_pr(repo, monkeypatch, git_env, target, ORG, target_files={"skj.stack.json": "{not json", "skj.kits.lock.json": "{not json"})
        seen = []
        real = config._blob_at
        monkeypatch.setattr(config, "_blob_at", lambda *a, **k: seen.append(a[2]) or real(*a, **k))
        assert _cli(m, capsys, "strict", "mode", "--base", "origin/main")[0] == 0
        assert "skj.stack.json" not in seen and "skj.kits.lock.json" not in seen  # v0.2: nothing new is read
        monkeypatch.undo()
        monkeypatch.chdir(repo)


# -- strict check: the target's profile judges --
def _ci(tmp_path, monkeypatch, git_env, target_files, head_files=None, target_cfg=STACK_STRICT, head_cfg=STACK_STRICT, base_files=None):
    """`origin/trunk` = base + the target's config and `target_files`; HEAD = base + the spec/ledger files, the PR's own config and
    `head_files`. Returns a runner for `strict check 1001 --phase implement --base origin/trunk` -> (rc, JSON, holder of the client)."""
    repo = _repo(tmp_path, git_env)
    if base_files:  # tracked on the target, the PR and everything between (e.g. an installed kit)
        commit(repo, "base files", base_files)
    base = git(repo, "rev-parse", "HEAD")
    _project(tmp_path, prof=None)
    _target(repo, target_cfg, base, files=target_files)
    mine = {k: v for k, v in (head_files or {}).items() if v is not None}
    inherited = {k: v for k, v in (target_files or {}).items() if isinstance(v, str) and k not in mine}  # the PR branched from the target
    commit(repo, "SPK-1001 pr", {"skj.config.json": _cfg_text(head_cfg), **inherited, **mine})
    for gone in (k for k, v in (head_files or {}).items() if v is None):  # a head file of None: the PR deletes it
        git(repo, "rm", "-q", gone)
        git(repo, "commit", "-q", "-m", "SPK-1001 delete")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    monkeypatch.setenv("SKJ_TOKEN", "t")
    holder, real = {}, strict.check

    def spy(cfg, *a, **k):  # the fork held the FakeClient around the config; the core needs none, so the config is held
        holder["c"] = cfg
        return real(cfg, *a, **k)
    monkeypatch.setattr(strict, "check", spy)
    return holder


def _run(capsys, *extra, phase="implement"):
    rc = main(["strict", "check", "SPK-1001", "--phase", phase, "--base", "origin/trunk", *extra])
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


def _rules_of(out):
    return [f["rule"] for f in out["findings"]]


def test_target_profile_used_when_target_strict(tmp_path, monkeypatch, capsys, git_env, real_detect):
    holder = _ci(tmp_path, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(profile()))})
    rc, out = _run(capsys)  # the PR touches neither file: the target's approved profile judges, no note
    assert (rc, out["ok"], out["findings"]) == (0, True, []) and "warnings" not in out and holder["c"].from_target is True
    d = tmp_path / "rev"
    d.mkdir()
    _ci(d, monkeypatch, git_env, {"skj.stack.json": json.dumps(profile())}, head_files={"skj.stack.json": json.dumps(approve(profile()))})
    rc, out = _run(capsys)  # the PR approves its own profile: the target's draft is what counts (and the head copy is consistent)
    assert rc == 2 and _rules_of(out) == ["stack-profile-draft"] and "at the head commit" not in out["findings"][0]["detail"]


def test_head_draft_over_an_approved_target_is_refused_at_the_head_commit(tmp_path, monkeypatch, capsys, git_env, real_detect):
    """Fix round 1 (this replaces the P2-5b brief's 'head draft + target approved -> pass'): the target's verdict stays, and a head copy
    that would lock every later PR once merged is refused too."""
    _ci(tmp_path, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(profile()))}, head_files={"skj.stack.json": json.dumps(profile())})
    rc, out = _run(capsys)
    assert rc == 2 and _rules_of(out) == ["stack-profile-draft"] and "at the head commit" in out["findings"][0]["detail"]
    assert out["warnings"] == [NOTE]


HEAD_BAD = {"draft": (lambda: profile(), "stack-profile-draft"),
            "changed": (lambda: approve(profile(), sha256="1" * 64), "stack-profile-changed"),
            "parse": (lambda: "{not json " + MARK, "stack-profile-invalid"),
            "unknown-key": (lambda: json.dumps({**profile(), MARK: 1}), "stack-profile-invalid")}


@pytest.mark.parametrize("kind", HEAD_BAD)
@pytest.mark.parametrize("bootstrap", [False, True], ids=["target-has-profile", "bootstrap"])
def test_head_profile_that_would_lock_strict_after_merge_is_refused(tmp_path, monkeypatch, capsys, git_env, real_detect, kind, bootstrap):
    make, rule = HEAD_BAD[kind]
    raw = make()
    _ci(tmp_path, monkeypatch, git_env, None if bootstrap else {"skj.stack.json": json.dumps(approve(profile()))},
        head_files={"skj.stack.json": raw if isinstance(raw, str) else json.dumps(raw)})
    rc, out = _run(capsys)
    assert rc == 2 and _rules_of(out) == [rule] and "at the head commit" in out["findings"][0]["detail"] and out["warnings"] == [NOTE]
    assert MARK not in json.dumps(out)


def test_head_hand_edited_test_command_is_refused(tmp_path, monkeypatch, capsys, git_env, real_detect):
    _ci(tmp_path, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(profile()))}, head_cfg={**STRICT, "strict": {"test_command": "true"}})
    rc, out = _run(capsys)  # the head's profile is unchanged but its config no longer matches it: merged, every later PR would drift
    assert rc == 2 and _rules_of(out) == ["stack-profile-drift"] and "at the head commit" in out["findings"][0]["detail"] and out["warnings"] == [NOTE]
    d = tmp_path / "unset"
    d.mkdir()
    _ci(d, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(profile()))}, head_cfg=STRICT)  # the PR drops the command
    rc, out = _run(capsys)
    assert rc == 2 and _rules_of(out) == ["stack-profile-drift"]


def test_head_deleting_the_profile_is_refused_and_a_null_test_is_consistent(tmp_path, monkeypatch, capsys, git_env, real_detect):
    _ci(tmp_path, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(profile()))}, head_files={"skj.stack.json": None})
    rc, out = _run(capsys)
    assert rc == 2 and _rules_of(out) == ["stack-profile-missing"] and "at the head commit" in out["findings"][0]["detail"]
    null = profile()
    null["stacks"][0]["commands"]["test"] = {"run": None, "why": "tests run on the platform"}
    for n, boot in enumerate((False, True)):  # no command anywhere is consistent with a null test, in both flows
        d = tmp_path / f"n{n}"
        d.mkdir()
        _ci(d, monkeypatch, git_env, None if boot else {"skj.stack.json": json.dumps(approve(null))}, target_cfg=STRICT, head_cfg=STRICT,
            head_files={"skj.stack.json": json.dumps(approve({**null, "version": 3}))})
        rc, out = _run(capsys)
        assert _rules_of(out) == ["test-command"] and out["warnings"] == [NOTE], boot  # only the unset command (an l2 waiver covers it)


def test_the_note_fires_on_any_head_edit_of_the_trust_files(tmp_path, monkeypatch, capsys, git_env, real_detect):
    approved = json.dumps(approve(profile()))
    cases = {"lockfile": dict(head_files={"skj.kits.lock.json": json.dumps({"schema": 1, "kits": {}})}),
             "profile": dict(head_files={"skj.stack.json": json.dumps(approve({**profile(), "version": 3}))}),
             "command": dict(head_cfg={**STRICT, "strict": {"test_command": "true"}}, head_files={"skj.stack.json": json.dumps(
                 approve({**profile(), "stacks": [{**profile()["stacks"][0], "commands": {"test": {"run": "true", "source": "S1"}}}]}))})}
    for n, (name, kw) in enumerate(cases.items()):
        d = tmp_path / f"c{n}"
        d.mkdir()
        _ci(d, monkeypatch, git_env, {"skj.stack.json": approved}, **kw)
        rc, out = _run(capsys)
        # consistent edits pass, flagged for review as code; the first lockfile's own note (KITS_TRUST_NOTE) is S3b's, so it adds none here
        assert (rc, out["ok"], out.get("warnings")) == (0, True, None if name == "lockfile" else [NOTE]), name
    d = tmp_path / "none"
    d.mkdir()
    _ci(d, monkeypatch, git_env, {"skj.stack.json": approved})
    assert "warnings" not in _run(capsys)[1]


def test_head_edit_to_test_command_ignored_in_ci(tmp_path, monkeypatch, capsys, git_env, real_detect):
    other = profile()
    other["stacks"][0]["commands"]["test"]["run"] = "true"
    holder = _ci(tmp_path, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(profile()))},
                 head_files={"skj.stack.json": json.dumps(approve(other))},
                 head_cfg={**STRICT, "strict": {"test_command": "true"}})  # the PR re-points both, consistently
    rc, out = _run(capsys)
    assert (rc, out["ok"], out["findings"]) == (0, True, [])
    assert holder["c"].strict["test_command"] == STACK_CMD  # the command in force is the target's
    d = tmp_path / "drift"
    d.mkdir()
    _ci(d, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(profile()))}, target_cfg={**STRICT, "strict": {"test_command": "pytest -q"}},
        head_cfg={**STRICT, "strict": {"test_command": STACK_CMD}})  # the target's config drifted from its profile; the PR's own copy agrees
    rc, out = _run(capsys)
    assert rc == 2 and _rules_of(out) == ["stack-profile-drift"]


def test_bootstrap_no_target_profile_is_note_not_block(tmp_path, monkeypatch, capsys, git_env, real_detect):
    _ci(tmp_path, monkeypatch, git_env, None)
    rc, out = _run(capsys)
    assert (rc, out["ok"], out["findings"]) == (0, True, []) and "warnings" not in out  # no profile anywhere: no finding, no note
    d = tmp_path / "ok"
    d.mkdir()
    _ci(d, monkeypatch, git_env, None, head_files={"skj.stack.json": json.dumps(approve(profile()))})
    rc, out = _run(capsys)  # the first profile: approved and consistent with the head's strict.test_command
    assert (rc, out["ok"], out["findings"], out["warnings"]) == (0, True, [], [NOTE])
    d = tmp_path / "drift"
    d.mkdir()
    _ci(d, monkeypatch, git_env, None, head_files={"skj.stack.json": json.dumps(approve(profile()))}, head_cfg={**STRICT, "strict": {"test_command": "true"}})
    rc, out = _run(capsys)  # approved but the head's command is not the composed one
    assert rc == 2 and _rules_of(out) == ["stack-profile-drift"] and out["warnings"] == [NOTE]


@pytest.mark.parametrize("raw", BAD_PROFILES.values(), ids=BAD_PROFILES)
def test_bootstrap_invalid_head_profile_refused(tmp_path, monkeypatch, capsys, git_env, real_detect, raw):
    _ci(tmp_path, monkeypatch, git_env, None, head_files={"skj.stack.json": raw})
    rc, out = _run(capsys)
    assert rc == 2 and _rules_of(out) == ["stack-profile-invalid"] and out["warnings"] == [NOTE]
    assert MARK not in json.dumps(out) and TOKEN not in json.dumps(out)


def test_bootstrap_head_symlink_profile_refused(tmp_path, monkeypatch, capsys, git_env, real_detect):
    _ci(tmp_path, monkeypatch, git_env, None, head_files={"elsewhere.json": json.dumps(approve(profile()))})
    os.symlink("elsewhere.json", tmp_path / "skj.stack.json")
    commit(tmp_path, "SPK-1001 link")
    rc, out = _run(capsys)
    assert rc == 2 and _rules_of(out) == ["stack-profile-invalid"]


def test_bootstrap_applies_only_when_the_target_lacks_a_profile(tmp_path, monkeypatch, capsys, git_env, real_detect):
    _ci(tmp_path, monkeypatch, git_env, {"skj.stack.json": json.dumps(profile())}, head_files={"skj.stack.json": json.dumps(approve(profile()))})
    rc, out = _run(capsys)  # the target has a (draft) profile: it binds, and the head's approved copy is just flagged for review
    assert rc == 2 and _rules_of(out) == ["stack-profile-draft"] and out["warnings"] == [NOTE] and "at the head commit" not in out["findings"][0]["detail"]
    d = tmp_path / "local"
    d.mkdir()
    _ci(d, monkeypatch, git_env, None, target_cfg=None, head_cfg=STACK_STRICT)  # no target config: the PR's own strict config, a local one
    rc, out = _run(capsys)  # (P2-17a: no strict target config, so this PR turns strict mode on: the stack note applies)
    assert rc == 2 and _rules_of(out) == ["stack-profile-missing"] and out["warnings"] == [NOTE]
    d = tmp_path / "plain"
    d.mkdir()
    _ci(d, monkeypatch, git_env, None, head_files={"skj.stack.json": json.dumps(approve(profile()))})
    rc, out = _run(capsys)  # and a target without a profile gives a note even when the head's profile is approved and complete
    assert rc == 0 and out["warnings"] == [NOTE]


def test_new_root_after_bootstrap_is_stack_unprofiled(tmp_path, monkeypatch, capsys, git_env, real_detect):
    _ci(tmp_path, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(profile()))}, head_files={"web/package.json": "{}\n"})
    rc, out = _run(capsys)
    assert rc == 2 and _rules_of(out) == ["stack-unprofiled"] and "web" in out["findings"][0]["detail"]
    d = tmp_path / "covered"
    d.mkdir()
    web = profile()
    web["stacks"].append({"id": "web", "root": "web", "commands": {"test": {"run": None, "why": "none"}}})
    web = approve(web)
    _ci(d, monkeypatch, git_env, {"skj.stack.json": json.dumps(web)}, head_files={"web/package.json": "{}\n"})
    rc, out = _run(capsys)  # the profile on the target covers the root (the two-PR flow: profile first, then the root)
    assert "stack-unprofiled" not in _rules_of(out)
    e = tmp_path / "edit"
    e.mkdir()
    _ci(e, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(profile()))},
        head_files={"web/package.json": "{}\n", "skj.stack.json": json.dumps(web)})  # the PR adds the root AND its own profile edit: still refused
    rc, out = _run(capsys)
    assert rc == 2 and "stack-unprofiled" in _rules_of(out)


def test_a_new_stack_at_a_covered_root_is_unprofiled_by_family(tmp_path, monkeypatch, capsys, git_env, real_detect):
    _ci(tmp_path, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(profile()))},
        head_files={"package.json": "{}\n", "tsconfig.json": "{}\n"})  # a python profile at "."; the head adds a TypeScript project at the same root
    rc, out = _run(capsys)
    assert rc == 2 and _rules_of(out) == ["stack-unprofiled"] and "(typescript)" in out["findings"][0]["detail"]
    d = tmp_path / "fam"
    d.mkdir()
    ts = profile()
    ts["stacks"][0]["id"] = "typescript"  # the seed's id for a root detection calls node or typescript: one family
    _ci(d, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(ts))}, head_files={"package.json": "{}\n"})
    assert _rules_of(_run(capsys)[1]) == []
    e = tmp_path / "custom"
    e.mkdir()
    custom = profile()
    custom["stacks"][0]["id"] = "web-app"  # a free-form id covers its root, whatever is detected there
    _ci(e, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(custom))}, head_files={"package.json": "{}\n"})
    assert _rules_of(_run(capsys)[1]) == []


def test_stack_rules_bind_in_pr_and_done_phases_from_the_target(tmp_path, monkeypatch, capsys, git_env, real_detect):
    _ci(tmp_path, monkeypatch, git_env, {"skj.stack.json": json.dumps(profile())}, head_files={"skj.stack.json": json.dumps(approve(profile()))})
    for phase in ("pr", "done"):
        rc, out = _run(capsys, phase=phase)
        assert rc == 2 and "stack-profile-draft" in _rules_of(out), phase


# -- gate: the PR comment carries the note --
def test_gate_comment_notes_a_trust_introducing_pr(tmp_path, monkeypatch, capsys, git_env):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG, head_files={"skj.stack.json": json.dumps(profile())})
    rc, out = _gate(capsys, summary, "--base", "origin/main")
    assert out["trust_note"] == NOTE and _md(summary).splitlines().count(NOTE) == 1
    cases = {"target has a profile": dict(target_files={"skj.stack.json": json.dumps(profile())}, head_files={"skj.stack.json": json.dumps(profile())}),
             "head adds none": {}, "advisory target": dict(head_files={"skj.stack.json": json.dumps(profile())})}
    for n, (name, kw) in enumerate(cases.items()):
        repo = tmp_path / f"c{n}"
        repo.mkdir()
        s = _gate_pr(repo, monkeypatch, git_env, ORG if name == "advisory target" else STRICT, ORG, **kw)
        rc, out = _gate(capsys, s, "--base", "origin/main")
        assert "trust_note" not in out and NOTE not in _md(s), name
    other = tmp_path / "nobase"
    other.mkdir()
    s = _gate_pr(other, monkeypatch, git_env, STRICT, ORG, head_files={"skj.stack.json": json.dumps(profile())})
    git(other, "update-ref", "-d", "refs/remotes/origin/main")  # D-023: v0.2 needs no strict origin/main beside an unset origin/HEAD
    assert "trust_note" not in _gate(capsys, s)[1] and NOTE not in _md(s)  # no --base: nothing is judged from a target


def test_gate_comment_notes_any_head_edit_of_the_trust_files(tmp_path, monkeypatch, capsys, git_env):
    cases = {"lockfile": dict(head=ORG, head_files={"skj.kits.lock.json": json.dumps({"schema": 1, "kits": {}})}),
             "profile": dict(head=ORG, target_files={"skj.stack.json": json.dumps(approve(profile()))},
                             head_files={"skj.stack.json": json.dumps(approve({**profile(), "version": 3}))}),
             "command": dict(head={**ORG, "strict": {"test_command": "true"}})}
    for n, (name, kw) in enumerate(cases.items()):
        repo = tmp_path / f"e{n}"
        repo.mkdir()
        kw = {"head": ORG, **kw}
        summary = _gate_pr(repo, monkeypatch, git_env, STRICT, kw.pop("head"), **kw)
        rc, out = _gate(capsys, summary, "--base", "origin/main")
        if name == "lockfile":  # a first lockfile has its own note (KITS_TRUST_NOTE), which is S3b's: no stack note either
            assert "trust_note" not in out and "kits_trust_note" not in out and NOTE not in _md(summary)
            continue
        assert out["trust_note"] == NOTE and _md(summary).splitlines().count(NOTE) == 1, name


def test_gate_comment_for_an_invalid_trust_file_does_not_blame_the_config(tmp_path, monkeypatch, capsys, git_env):
    for name, raw in (("skj.stack.json", "{not json"), ("skj.kits.lock.json", "[]")):
        repo = tmp_path / name
        repo.mkdir()
        summary = _gate_pr(repo, monkeypatch, git_env, STRICT, ORG, target_files={name: raw})
        assert _gate(capsys, summary, "--base", "origin/main")[0] == 3
        md = _md(summary)
        assert f"{name} at the target branch" in md  # (the fork's own wording "target branch trust files are invalid" is C-phase's summary text)
    other = tmp_path / "config"
    other.mkdir()
    summary = _gate_pr(other, monkeypatch, git_env, "{not json", ORG)  # a broken config file keeps its old wording
    assert _gate(capsys, summary, "--base", "origin/main")[0] == 3 and "skj.config.json is invalid" in _md(summary)



def _sub(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


# a tab or control character in the command, invalid JSON, a mode that is neither value: the validator refuses each
BAD_HEAD_CONFIGS = {"tab": json.dumps({**STRICT, "strict": {"test_command": "echo SECRET-MARK\tx"}}),
                    "control": json.dumps({**STRICT, "strict": {"test_command": "echo SECRET-MARK\x01x"}}),
                    "invalid-json": '{"mode": "strict", SECRET-MARK',
                    "bad-mode": json.dumps({**ORG, "mode": "SECRET-MARK"})}


@pytest.mark.parametrize("name", BAD_HEAD_CONFIGS)
def test_ci_head_config_present_but_invalid_is_config_invalid(tmp_path, monkeypatch, capsys, git_env, real_detect, name):
    _ci(tmp_path, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(profile()))}, head_cfg=BAD_HEAD_CONFIGS[name])
    for phase in ("implement", "pr", "done"):
        rc, out = _run(capsys, phase=phase)
        got = [f for f in out["findings"] if f["rule"] == "config-invalid"]
        assert rc == 2 and out["ok"] is False and len(got) == 1, (name, phase)
        assert set(got[0]) == {"rule", "detail", "fix"} and "SECRET-MARK" not in json.dumps(out), (name, phase)  # a fixed text: nothing echoed
        assert got[0]["detail"] == strict._CONFIG_BAD[0] and got[0]["fix"] == strict._CONFIG_BAD[1]
    assert "SECRET-MARK" not in capsys.readouterr().err


def test_ci_head_config_invalid_is_also_found_at_bootstrap_and_not_overridable(tmp_path, monkeypatch, capsys, git_env, real_detect):
    _ci(tmp_path, monkeypatch, git_env, {}, head_cfg=BAD_HEAD_CONFIGS["tab"])  # the target has no profile yet: only the head copies are judged
    rc, out = _run(capsys)
    assert rc == 2 and "config-invalid" in _rules_of(out)
    assert "config-invalid" not in strict._COVERS and all("config-invalid" not in rules for rules in strict._COVERS.values())


def test_ci_head_config_absent_or_valid_is_no_config_invalid(tmp_path, monkeypatch, capsys, git_env, real_detect):
    _ci(tmp_path, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(profile()))}, head_files={"skj.config.json": None})  # the PR deletes it
    assert "config-invalid" not in _rules_of(_run(capsys)[1])
    d = _sub(tmp_path, "valid")
    _ci(d, monkeypatch, git_env, {"skj.stack.json": json.dumps(approve(profile()))})
    rc, out = _run(capsys)
    assert (rc, out["ok"]) == (0, True)


# ---- the config-trust half of the fork's tests/test_trust_minors.py (M-4, m-3, SEC-1); its approvals/sync/post half is A1/A2/W4/W5's ----
from test_config import A1, TRUST_BASE, _trust_repo  # noqa: E402


def _nonroot(repo, data, monkeypatch, name="config/skj.json"):
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    monkeypatch.setenv("SKJ_CONFIG", str(p))
    return p


@pytest.mark.parametrize("key,val", [("approvers", [A1]), ("approval", "comment"), ("unattended", {"assume_after_hours": 24})])
def test_a_non_root_config_with_a_trust_key_is_invalid(tmp_path, git_env, monkeypatch, key, val):
    repo = _trust_repo(tmp_path, {**TRUST_BASE, "approvers": [A1], "approval": "comment"})
    _nonroot(repo, {**TRUST_BASE, key: val}, monkeypatch)
    with pytest.raises(config.ConfigInvalid) as e:
        config.trusted(str(repo))
    assert str(e.value) == config.E_NONROOT_TRUST and A1 not in str(e.value)
    with pytest.raises(config.ConfigInvalid):
        config.trusted(str(repo), base="origin/main")


def test_a_non_root_config_without_trust_keys_and_the_root_file_keep_working(tmp_path, git_env, monkeypatch):
    repo = _trust_repo(tmp_path, {**TRUST_BASE, "approvers": [A1], "approval": "comment"})
    _nonroot(repo, {**TRUST_BASE, "labels": ["x"]}, monkeypatch)
    assert config.trusted(str(repo)).approvers == [A1]  # unchanged: the target's keys, the non-root file is only the rest of the config
    monkeypatch.setenv("SKJ_CONFIG", str(repo / "skj.config.json"))  # the root file itself may carry trust keys (it is read from the target)
    (repo / "skj.config.json").write_text(json.dumps({**TRUST_BASE, "approvers": [A1], "approval": "comment"}))
    assert config.trusted(str(repo)).approvers == [A1]
    monkeypatch.setenv("SKJ_CONFIG", str(repo / "config" / ".." / "skj.config.json"))  # the same file by another spelling
    assert config.trusted(str(repo)).approval == "comment"


def test_a_non_root_config_with_a_trust_key_is_invalid_with_no_origin_head_too(tmp_path, git_env, monkeypatch):
    repo = _trust_repo(tmp_path, TRUST_BASE, origin=False)
    _nonroot(repo, {**TRUST_BASE, "approvers": [A1]}, monkeypatch)
    with pytest.raises(config.ConfigInvalid) as e:
        config.trusted(str(repo))
    assert str(e.value) == config.E_NONROOT_TRUST


def test_the_root_file_is_recognised_however_it_is_reached(tmp_path, git_env, monkeypatch):
    repo = _trust_repo(tmp_path, {**TRUST_BASE, "approvers": [A1], "approval": "comment"})
    (repo / "svc").mkdir()
    monkeypatch.setenv("SKJ_CONFIG", str(repo / "skj.config.json"))  # the root file by an absolute path, run from a subdirectory
    assert config.trusted(str(repo / "svc")).approvers == [A1]
    upper = repo / "SKJ.CONFIG.JSON"
    if upper.exists():  # a case-insensitive disk (the macOS default) reaches the root file in any letter case
        monkeypatch.setenv("SKJ_CONFIG", str(upper))
        assert config.trusted(str(repo)).approvers == [A1]


def test_a_subproject_config_with_trust_keys_is_refused_without_skj_config_too(tmp_path, git_env, monkeypatch):
    """`svc/skj.config.json` read by `Config.load` from `svc/` is not the file the target is read from."""
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    repo = _trust_repo(tmp_path, TRUST_BASE)
    svc = repo / "svc"
    svc.mkdir()
    (svc / "skj.config.json").write_text(json.dumps({**TRUST_BASE, "approvers": [A1], "approval": "comment"}))
    for base in (None, "origin/main"):
        with pytest.raises(config.ConfigInvalid) as e:
            config.trusted(str(svc), base=base)
        assert str(e.value) == config.E_NONROOT_TRUST
    (svc / "skj.config.json").write_text(json.dumps(TRUST_BASE))  # without trust keys it works as before
    assert config.trusted(str(svc)).approvers == [] and config.trusted(str(svc), base="origin/main").approval == "terminal"


def test_src_has_no_raw_format_characters():
    import unicodedata
    from pathlib import Path
    found = [f"{p}:{n}" for p in sorted(Path(__file__).resolve().parent.parent.joinpath("src").rglob("*.py"))
             for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if any(unicodedata.category(c) == "Cf" for c in line)]
    assert not found, f"raw Cf (format/bidi) characters in: {found}"


@pytest.mark.rules
def test_the_invisible_character_rule_matches_exactly_the_documented_code_points():  # fork test_trust_minors (SEC-1), Fix-RU
    from skj import rules
    expected = ({0xAD, 0x61C, 0x180E, 0xFEFF} | set(range(0x200B, 0x2010)) | set(range(0x202A, 0x202F))
                | set(range(0x2060, 0x2065)) | set(range(0x2066, 0x2070)))
    assert {cp for cp in range(0x10000) if rules._INVISIBLE.fullmatch(chr(cp))} == expected and len(expected) == 29


# ---- L5a (new): the target config also judges without --base (origin/HEAD), as config.trusted() finds the base ----
def test_no_base_flag_with_origin_head_reads_the_target(tmp_path, monkeypatch, capsys, git_env):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    rc, out = _gate(capsys, summary)
    assert rc == 1 and out["config"] == "target branch (strict)" and "ac_traceability" in out["blocked_by"]
    assert _md(summary).splitlines().count(LINE) == 1


# ---- the kit rules in CI (ported from the fork, L5b): the TARGET branch's lockfile judges the files git tracks at the PR head ----
KIT = ".specify/extensions/sf"
KIT_FILES = {f"{KIT}/extension.yml": "id: sf\n", f"{KIT}/commands/specify.md": "specify\n"}
KNOTE = "trust-introducing PR: review skj.kits.lock.json as code"
UNTRACKED = "kit not in the repository: checked locally only"
APPROVED = json.dumps(approve(profile()))


def _blob(text):  # the git blob id of a SHA-1 repository
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _lock(files=None, **more):
    """A lockfile pinning `sf` to the digest of `files` (default KIT_FILES); `more`: extra kits as {id: entry}."""
    prefix = KIT + "/"
    digest = kits._digest([(_blob(t), p[len(prefix):]) for p, t in (files or KIT_FILES).items() if p.startswith(prefix)])
    entry = {"version": "1.0.0", "source": None, "catalog_sha256": None, "dir_digest": digest, "agents_sha256": "a" * 64}
    return json.dumps({"schema": 1, "kits": {"sf": entry, **more}})


def _kci(tmp_path, monkeypatch, git_env, lock="pinned", head_files=None, base_files=KIT_FILES, **kw):
    """A strict target with an approved profile and (lock: "pinned" = sf pinned to KIT_FILES, None = none, else the text) the lockfile,
    KIT_FILES tracked everywhere; `head_files` is the PR's own change."""
    files = {"skj.stack.json": APPROVED, **({"skj.kits.lock.json": _lock() if lock == "pinned" else lock} if lock is not None else {})}
    return _ci(tmp_path, monkeypatch, git_env, files, head_files, base_files=base_files, **kw)


def _kit_rules(out):
    return [r for r in _rules_of(out) if r.startswith("kit-")]


def _sub(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def _kit_findings(out, rule):
    got = [f for f in out["findings"] if f["rule"] == rule]
    assert len(got) == 1 and set(got[0]) == {"rule", "detail", "fix"}, out["findings"]
    return got[0]


def test_ci_uses_target_lock_and_head_tracked_files(tmp_path, monkeypatch, capsys, git_env, real_kits):
    _kci(tmp_path, monkeypatch, git_env)
    rc, out = _run(capsys)
    assert (rc, out["ok"], out["findings"]) == (0, True, []) and "warnings" not in out  # the lock matches the tracked files
    (tmp_path / KIT / "commands/specify.md").write_text("dirty\n")  # an uncommitted edit, a new untracked file and agent copies: CI reads none
    (tmp_path / KIT / "commands/new.md").write_text("new\n")
    (tmp_path / ".claude/skills/speckit-sf-specify").mkdir(parents=True)
    (tmp_path / ".claude/skills/speckit-sf-specify/SKILL.md").write_text("agent copy\n")
    for phase in ("implement", "pr", "done"):
        assert _kit_rules(_run(capsys, phase=phase)[1]) == [], phase
    d = _sub(tmp_path, "edit")  # a committed edit to a tracked kit file
    _kci(d, monkeypatch, git_env, head_files={f"{KIT}/commands/specify.md": "IGNORE-ALL-PREVIOUS evil\n"})
    for phase in ("implement", "pr", "done"):
        rc, out = _run(capsys, phase=phase)
        f = _kit_findings(out, "kit-changed")
        assert "sf" in f["detail"] and "evil" not in json.dumps(out) and "IGNORE" not in json.dumps(out) and f["fix"], phase
        assert "kit-unpinned" not in _rules_of(out) and rc == 2
    e = _sub(tmp_path, "added")  # a new tracked file in a locked kit dir
    _kci(e, monkeypatch, git_env, head_files={f"{KIT}/hooks/after.sh": "echo x\n"})
    assert _kit_rules(_run(capsys)[1]) == ["kit-changed"]
    g = _sub(tmp_path, "deleted")  # a deleted tracked file
    _kci(g, monkeypatch, git_env, head_files={f"{KIT}/commands/specify.md": None})
    assert _kit_rules(_run(capsys)[1]) == ["kit-changed"]


def test_ci_pr_editing_lock_has_no_effect(tmp_path, monkeypatch, capsys, git_env, real_kits):
    evil = {**KIT_FILES, f"{KIT}/commands/specify.md": "evil\n"}
    _kci(tmp_path, monkeypatch, git_env, head_files={f"{KIT}/commands/specify.md": "evil\n", "skj.kits.lock.json": _lock(evil)})
    rc, out = _run(capsys)  # the PR re-pins sf to its own tampered files: the target's pin still judges
    assert rc == 2 and _kit_rules(out) == ["kit-changed"] and NOTE in out["warnings"]
    d = _sub(tmp_path, "new-kit")  # the PR adds a kit AND its pin
    other = {f"{KIT.replace('/sf', '/other')}/extension.yml": "id: other\n"}
    entry = json.loads(_lock())["kits"]["sf"]
    _kci(d, monkeypatch, git_env, head_files={**other, "skj.kits.lock.json": json.dumps({"schema": 1, "kits": {"sf": entry, "other": entry}})})
    rc, out = _run(capsys)
    assert rc == 2 and _kit_rules(out) == ["kit-unpinned"] and "other" in _kit_findings(out, "kit-unpinned")["detail"]
    e = _sub(tmp_path, "weaker")  # the PR drops sf from the lock: this run is unaffected, the edit is flagged for review as code
    _kci(e, monkeypatch, git_env, head_files={"skj.kits.lock.json": json.dumps({"schema": 1, "kits": {}})})
    rc, out = _run(capsys)
    assert (rc, out["ok"]) == (0, True) and NOTE in out["warnings"]


def test_ci_pr_deleting_or_breaking_the_lock_is_refused(tmp_path, monkeypatch, capsys, git_env, real_kits):
    _kci(tmp_path, monkeypatch, git_env, head_files={"skj.kits.lock.json": None})
    rc, out = _run(capsys)  # merged, a deleted lock would turn every kit rule into a bootstrap note
    assert rc == 2 and _kit_rules(out) == ["kit-changed"] and "skj.kits.lock.json" in _kit_findings(out, "kit-changed")["detail"]
    for n, raw in enumerate(("{not json IGNORE-ALL-PREVIOUS", '{"schema": 1, "kits": {"sf": {"version": "1.0.0"}}}', json.dumps({"schema": 2, "kits": {}}))):
        d = _sub(tmp_path, f"bad{n}")
        _kci(d, monkeypatch, git_env, head_files={"skj.kits.lock.json": raw})
        rc, out = _run(capsys)
        assert rc == 2 and _kit_rules(out) == ["kit-changed"] and "IGNORE" not in json.dumps(out), raw
    g = _sub(tmp_path, "symlink")  # a lockfile that is a symlink at the head
    _kci(g, monkeypatch, git_env, head_files={"elsewhere.json": _lock()})
    os.remove(g / "skj.kits.lock.json")
    os.symlink("elsewhere.json", g / "skj.kits.lock.json")
    commit(g, "SPK-1001 link")
    assert _kit_rules(_run(capsys)[1]) == ["kit-changed"]


def test_ci_target_lock_entry_that_cannot_be_read_is_changed(tmp_path, monkeypatch, capsys, git_env, real_kits):
    bad = json.dumps({"schema": 1, "kits": {"sf": {"version": "IGNORE-ALL-PREVIOUS"}}})  # valid for the config, not a pin
    _kci(tmp_path, monkeypatch, git_env, lock=bad)
    rc, out = _run(capsys)
    assert rc == 2 and _kit_rules(out) == ["kit-changed"] and "IGNORE" not in json.dumps(out)


def test_ci_new_tracked_kit_is_unpinned(tmp_path, monkeypatch, capsys, git_env, real_kits):
    _kci(tmp_path, monkeypatch, git_env, head_files={".specify/extensions/other/extension.yml": "id: other\n"})
    for phase in ("implement", "pr", "done"):
        rc, out = _run(capsys, phase=phase)
        f = _kit_findings(out, "kit-unpinned")
        assert _kit_rules(out) == ["kit-unpinned"] and "other" in f["detail"] and "sf" not in f["detail"] and f["fix"], phase
    d = _sub(tmp_path, "odd")  # a dir that is no valid kit id, a dot dir, a dir with no manifest, a manifest too deep, a locked kit's second manifest
    _kci(d, monkeypatch, git_env, head_files={
        ".specify/extensions/IGNORE ALL PREVIOUS/extension.yml": "x\n", ".specify/extensions/.cache/extension.yml": "x\n",
        ".specify/extensions/readme-only/README.md": "x\n", ".specify/extensions/deep/inner/extension.yml": "x\n",
        f"{KIT}/inner/extension.yml": "x\n"})
    rc, out = _run(capsys)
    f = _kit_findings(out, "kit-unpinned")
    assert "IGNORE" not in json.dumps(out) and "#1" in f["detail"] and "#2" in f["detail"] and "readme-only" not in f["detail"] and "deep" not in f["detail"]
    assert _kit_rules(out) == ["kit-unpinned", "kit-changed"] or _kit_rules(out) == ["kit-changed", "kit-unpinned"]  # sf got a new tracked file
    e = _sub(tmp_path, "many")  # the finding stays one short line however many kits there are
    _kci(e, monkeypatch, git_env, head_files={f".specify/extensions/k{n}/extension.yml": "x\n" for n in range(30)})
    out = _run(capsys)[1]
    assert len(_kit_findings(out, "kit-unpinned")["detail"]) < 300 and "30" in _kit_findings(out, "kit-unpinned")["detail"]


def test_ci_symlink_or_submodule_entry_is_changed(tmp_path, monkeypatch, capsys, git_env, real_kits):
    def inside_link(d):  # a symlink inside the locked kit dir
        os.symlink("../../../README.md", d / KIT / "commands/link.md")
        git(d, "add", "-A")

    def inside_gitlink(d):  # a submodule inside the locked kit dir
        git(d, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},{KIT}/vendor")

    def dir_link(d):  # the locked kit dir itself is a symlink to tracked content elsewhere
        git(d, "rm", "-rq", KIT)
        shutil.rmtree(d / KIT, ignore_errors=True)
        (d / "evil").mkdir()
        (d / "evil/extension.yml").write_text("id: sf\n")
        (d / KIT).parent.mkdir(parents=True, exist_ok=True)
        os.symlink("../../evil", d / KIT)
        git(d, "add", "-A")

    def dir_gitlink(d):  # ... or a submodule
        git(d, "rm", "-rq", KIT)
        shutil.rmtree(d / KIT, ignore_errors=True)
        git(d, "update-index", "--add", "--cacheinfo", f"160000,{'2' * 40},{KIT}")

    def manifest_link(d):  # the manifest itself is a symlink
        os.remove(d / KIT / "extension.yml")
        os.symlink("../../../README.md", d / KIT / "extension.yml")
        git(d, "add", "-A")

    def extensions_link(d):  # `.specify/extensions` is a symlink: the kit files would be read from elsewhere
        git(d, "rm", "-rq", ".specify")
        shutil.rmtree(d / ".specify", ignore_errors=True)
        (d / "evil").mkdir()
        (d / "evil/sf").mkdir()
        (d / "evil/sf/extension.yml").write_text("id: sf\n")
        (d / ".specify").mkdir()
        os.symlink("../evil", d / ".specify/extensions")
        git(d, "add", "-A")

    def specify_link(d):  # ... or `.specify` itself
        git(d, "rm", "-rq", ".specify")
        shutil.rmtree(d / ".specify", ignore_errors=True)
        (d / "evil").mkdir()
        os.symlink("evil", d / ".specify")
        git(d, "add", "-A")

    def extensions_gitlink(d):
        git(d, "rm", "-rq", ".specify")
        shutil.rmtree(d / ".specify", ignore_errors=True)
        git(d, "update-index", "--add", "--cacheinfo", f"160000,{'4' * 40},.specify/extensions")

    cases = {"extensions-link": extensions_link, "specify-link": specify_link, "extensions-gitlink": extensions_gitlink, "inside-link": inside_link, "inside-gitlink": inside_gitlink, "dir-link": dir_link, "dir-gitlink": dir_gitlink, "manifest-link": manifest_link}
    for name, mutate in cases.items():
        d = _sub(tmp_path, name)
        _kci(d, monkeypatch, git_env)
        mutate(d)  # stages its own change (a submodule entry exists only in the index)
        git(d, "commit", "-q", "-m", "SPK-1001 mutate")
        rc, out = _run(capsys)
        assert rc == 2 and _kit_rules(out) == ["kit-changed"], (name, out["findings"], out.get("warnings"))
        assert UNTRACKED not in json.dumps(out), name
    d = _sub(tmp_path, "unlocked-link")  # an unlocked dir that is a symlink or a submodule is an unpinned kit, not skipped
    _kci(d, monkeypatch, git_env)
    os.symlink("../../evil", d / ".specify/extensions/evil")
    git(d, "add", "-A")
    git(d, "update-index", "--add", "--cacheinfo", f"160000,{'3' * 40},.specify/extensions/vendor")
    git(d, "commit", "-q", "-m", "SPK-1001 link")
    rc, out = _run(capsys)
    assert rc == 2 and _kit_rules(out) == ["kit-unpinned"] and "evil" in out["findings"][0]["detail"] + out["findings"][-1]["detail"]


def test_ci_agent_copies_not_read(tmp_path, monkeypatch, capsys, git_env, real_kits):
    for name in ("installed", "verify", "agents_sha256", "dir_digest", "load_lock"):
        monkeypatch.setattr(kits, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("CI reads the tracked files only")))
    registry = json.dumps({"extensions": {"evil": {"enabled": True, "registered_commands": {"claude": ["speckit.evil.run"]}}}})
    _kci(tmp_path, monkeypatch, git_env, head_files={
        ".specify/extensions/.registry": registry, ".claude/commands/speckit.evil.run.md": "evil\n",
        ".claude/skills/speckit-sf-specify/SKILL.md": "tampered agent copy\n"})
    for phase in ("implement", "pr", "done"):
        rc, out = _run(capsys, phase=phase)
        assert _kit_rules(out) == [], phase
    d = _sub(tmp_path, "plain")
    _kci(d, monkeypatch, git_env)
    assert _run(capsys)[0] == 0


def test_ci_untracked_kit_is_warning(tmp_path, monkeypatch, capsys, git_env, real_kits):
    _kci(tmp_path, monkeypatch, git_env, base_files={"src/x.py": "x = 1\n"})  # sf is locked but nothing of it is tracked here
    (tmp_path / KIT / "commands").mkdir(parents=True)
    for rel, text in KIT_FILES.items():  # the kit exists on disk, untracked: CI still has no tracked file to judge
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    for phase in ("implement", "pr", "done"):
        rc, out = _run(capsys, phase=phase)
        assert _kit_rules(out) == [] and UNTRACKED in "\n".join(w for w in out["warnings"] if isinstance(w, str)), phase
    assert _run(capsys)[0] == 0
    d = _sub(tmp_path, "deleted")  # the PR deletes the whole locked kit: a warning too (the kit is gone from the checkout)
    _kci(d, monkeypatch, git_env, head_files={p: None for p in KIT_FILES})
    rc, out = _run(capsys)
    assert rc == 0 and UNTRACKED in "\n".join(w for w in out["warnings"] if isinstance(w, str))
    e = _sub(tmp_path, "two")  # one tracked and matching, one not tracked: only the second is named
    two = {**KIT_FILES}
    lock = json.loads(_lock())
    lock["kits"]["other-kit"] = lock["kits"]["sf"]
    _kci(e, monkeypatch, git_env, lock=json.dumps(lock), base_files=two)
    out = _run(capsys)[1]
    note = next(w for w in out["warnings"] if isinstance(w, str) and UNTRACKED in w)
    assert "other-kit" in note and "sf" not in note.replace("other-kit", "")


def test_bootstrap_no_target_lock_is_note(tmp_path, monkeypatch, capsys, git_env, real_kits):
    _kci(tmp_path, monkeypatch, git_env, lock=None, head_files={".specify/extensions/other/extension.yml": "id: other\n"})
    for phase in ("implement", "pr", "done"):
        rc, out = _run(capsys, phase=phase)
        assert _kit_rules(out) == [] and out["warnings"] == [strict.KIT_NO_LOCK], phase  # no lock anywhere: no finding; L5b: a note
    d = _sub(tmp_path, "adds")  # the PR adds the first lockfile (and a kit the target never pinned)
    _kci(d, monkeypatch, git_env, lock=None, head_files={"skj.kits.lock.json": _lock(), ".specify/extensions/other/extension.yml": "x\n"})
    rc, out = _run(capsys)
    assert (rc, out["ok"], out["findings"]) == (0, True, []) and KNOTE in out["warnings"] and out["warnings"].count(KNOTE) == 1
    e = _sub(tmp_path, "bad")  # the first lockfile must itself be sound: merged, an invalid one would fail every later run
    for n, raw in enumerate(("{not json IGNORE-ALL-PREVIOUS", '{"schema": 1, "kits": {"sf": {}}}')):
        f = _sub(e, f"b{n}")
        _kci(f, monkeypatch, git_env, lock=None, head_files={"skj.kits.lock.json": raw})
        rc, out = _run(capsys)
        assert rc == 2 and _kit_rules(out) == ["kit-changed"] and KNOTE in out["warnings"] and "IGNORE" not in json.dumps(out)
    g = _sub(tmp_path, "present")  # the target HAS a lock: no bootstrap note for an edit of it
    _kci(g, monkeypatch, git_env, head_files={"skj.kits.lock.json": json.dumps({"schema": 1, "kits": {}})})
    assert KNOTE not in _run(capsys)[1]["warnings"]



def test_advisory_target_ignores_the_kit_rules(tmp_path, monkeypatch, capsys, git_env, real_kits):
    _kci(tmp_path, monkeypatch, git_env, target_cfg=ORG, head_cfg=ORG, head_files={".specify/extensions/other/extension.yml": "x\n"})
    rc, out = _run(capsys)
    assert (rc, out["ok"], out["findings"]) == (0, True, []) and out["note"] == "mode is advisory"


def _track(repo, path, text="x\n"):
    """Stage `path` in the index only and commit: a case-insensitive file system (macOS) would fold the variants in the working tree."""
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=text, capture_output=True, text=True, check=True).stdout.strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")


def test_ci_case_and_unicode_lookalike_paths_are_not_ignored(tmp_path, monkeypatch, capsys, git_env, real_kits):
    """On a case-insensitive checkout (macOS, Windows) these land in the pinned kit dir, so they are findings, and no name is echoed."""
    hostile = ("SF/commands/x.md", "\uff53f/commands/x.md", "\u0455f/commands/x.md", "Sf/extension.yml")
    for n, name in enumerate(hostile):
        d = _sub(tmp_path, f"dir{n}")
        _kci(d, monkeypatch, git_env)
        _track(d, f".specify/extensions/{name}")
        git(d, "commit", "-q", "-m", "SPK-1001 variant")
        rc, out = _run(capsys)
        f = _kit_findings(out, "kit-unpinned")
        assert rc == 2 and _kit_rules(out) == ["kit-unpinned"] and "#1" in f["detail"], name
        assert name.split("/")[0] not in json.dumps(out, ensure_ascii=False) and "SF" not in json.dumps(out), name
    for n, path in enumerate((".Specify/extensions/sf/x.md", ".specify/Extensions/sf/x.md", ".SPECIFY/extensions/sf/commands/specify.md",
                              "\uff0especify/extensions/sf/x.md")):
        d = _sub(tmp_path, f"top{n}")
        _kci(d, monkeypatch, git_env)
        _track(d, path)
        git(d, "commit", "-q", "-m", "SPK-1001 variant")
        rc, out = _run(capsys)
        assert rc == 2 and "kit-changed" in _kit_rules(out), path
        dump = json.dumps(out, ensure_ascii=False)
        assert not any(t in dump for t in (".Specify", "Extensions", "SPECIFY", "\uff0e")), path
    d = _sub(tmp_path, "plain-file")  # a plain file and a dot dir beside the kits are not kit dirs
    _kci(d, monkeypatch, git_env, head_files={".specify/extensions/README.md": "x\n", ".specify/extensions/.cache/c.json": "{}\n"})
    assert _kit_rules(_run(capsys)[1]) == []


def test_ci_symlinked_kit_config_file_is_changed(tmp_path, monkeypatch, capsys, git_env, real_kits):
    files = {**KIT_FILES, f"{KIT}/sf-config.yml": "org: example\n"}  # the kit's own settings file is excluded from the digest ...
    _kci(tmp_path, monkeypatch, git_env, base_files=files)
    assert _run(capsys)[0] == 0
    d = _sub(tmp_path, "link")  # ... unless it is a symlink
    _kci(d, monkeypatch, git_env, base_files=files)
    os.remove(d / KIT / "sf-config.yml")
    os.symlink("../../../README.md", d / KIT / "sf-config.yml")
    commit(d, "SPK-1001 link")
    assert _kit_rules(_run(capsys)[1]) == ["kit-changed"]


# ---- a kit dir with no manifest is a note ----
NOMANIFEST = "kit dir without extension.yml"


def test_ci_kit_dir_without_manifest_is_a_note_never_a_finding(tmp_path, monkeypatch, capsys, git_env, real_kits):
    _kci(tmp_path, monkeypatch, git_env, head_files={".specify/extensions/notes/README.md": "x\n", ".specify/extensions/other/commands/a.md": "x\n"})
    for phase in ("implement", "pr", "done"):
        rc, out = _run(capsys, phase=phase)
        assert _kit_rules(out) == [] and out["warnings"] == [f"{NOMANIFEST} (notes, other)"], phase
        if phase == "implement":
            assert (rc, out["ok"]) == (0, True)
    d = _sub(tmp_path, "locked")  # a locked kit is judged by its digest (here: unchanged), a dir with a manifest is judged as before, dot dirs are no kits
    _kci(d, monkeypatch, git_env, head_files={".specify/extensions/.cache/x.json": "{}\n"})
    out = _run(capsys)[1]
    assert out["ok"] is True and "warnings" not in out
    e = _sub(tmp_path, "invalid-id")  # a dir that is no valid kit id is the older `kit-unpinned` rule's business (#n), not this note
    _kci(e, monkeypatch, git_env, head_files={".specify/extensions/BAD NAME/README.md": "x\n"})
    out = _run(capsys)[1]
    assert not any(NOMANIFEST in w for w in out.get("warnings", [])) and "BAD" not in json.dumps(out)


# ---- L5b fix round 1 ----
def test_bootstrap_without_kit_dirs_has_no_note_and_with_one_has_a_note_not_a_block(tmp_path, monkeypatch, capsys, git_env, real_kits):
    _kci(tmp_path, monkeypatch, git_env, lock=None, base_files={"src/x.py": "x = 1\n"})
    rc, out = _run(capsys)
    assert (rc, out["ok"]) == (0, True) and "warnings" not in out  # no kit dir, no lockfile: nothing to say
    d = _sub(tmp_path, "kit")
    _kci(d, monkeypatch, git_env, lock=None, head_files={".specify/extensions/other/extension.yml": "id: other\n"})
    rc, out = _run(capsys)
    assert (rc, out["ok"]) == (0, True) and out["warnings"] == [strict.KIT_NO_LOCK]


def test_ci_case_variant_manifest_makes_a_tracked_dir_a_kit(tmp_path, monkeypatch, capsys, git_env, real_kits):
    """`EXTENSION.yml` is the manifest on a case-insensitive checkout: the dir is an unpinned kit, not a manifest-less note."""
    for n, name in enumerate(("EXTENSION.yml", "Extension.yml", "extension.YML")):
        d = _sub(tmp_path, f"m{n}")
        _kci(d, monkeypatch, git_env)
        _track(d, f".specify/extensions/other/{name}", "id: other\n")
        git(d, "commit", "-q", "-m", "SPK-1001 variant")
        rc, out = _run(capsys)
        assert rc == 2 and _kit_rules(out) == ["kit-unpinned"] and "other" in _kit_findings(out, "kit-unpinned")["detail"], name
        assert not any(strict.KIT_NO_MANIFEST in w for w in out.get("warnings", []) if isinstance(w, str)), name


# ---- DF1 (live run A7-23, D-016): a mistyped --base outside CI must not drop origin/HEAD's strict mode ----
def test_an_unknown_base_falls_back_to_origin_head_and_reports_base_unknown(tmp_path, monkeypatch, capsys, git_env):
    _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)  # the target (origin/main) is strict; the PR's own config is advisory
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    rc, out = _cli(m, capsys, "strict", "check", "SPK-1001", "--phase", "pr", "--base", "origin/nope")
    assert rc == 2 and out["mode"] == "strict" and "base-unknown" in [f["rule"] for f in out["findings"]]
    assert _mode(monkeypatch, capsys, "--base", "origin/nope")[1] == {"mode": "strict", "source": TARGET}
    rc, out = _gate(capsys, str(tmp_path / "out" / "gate.md"), "--base", "origin/nope")
    assert out["config"] == "target branch (strict)"  # judged by origin/HEAD's strict config, not the PR's advisory one


# ---- DF2 (live run A12-X7, D-023): moving origin/HEAD to a weaker branch must not turn strict mode off ----
def test_d023_strict_mode_is_not_lowered_by_a_moved_origin_head(tmp_path, monkeypatch, capsys, git_env):
    _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)  # origin/main is strict; the feature branch says advisory
    git(tmp_path, "update-ref", "refs/remotes/origin/lv-x7", "HEAD")
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/lv-x7")  # `git remote set-head origin lv-x7`
    rc, out, err = _mode(monkeypatch, capsys)
    assert rc != 0 and out is None and "target-moved" in err and "lv-x7" not in err
    rc, out = _gate(capsys, str(tmp_path / "out" / "gate.md"))
    assert rc == 3  # the gate fails closed (exit 3), as for any target config it cannot trust
    rc, out = _cli(m, capsys, "strict", "check", "SPK-1001", "--phase", "pr")
    assert rc != 0
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")  # set back: judged by main's strict config
    assert _mode(monkeypatch, capsys)[1] == {"mode": "strict", "source": TARGET}


# ---- DF3 (R1 retest, D-016 variant): a revision-expression --base outside CI must not drop a strict default branch ----
@pytest.mark.parametrize("base", ["main~1", "origin/main~1", "HEAD~1", "main^"])
def test_a_revision_expression_base_is_refused_beside_a_strict_default_branch(tmp_path, monkeypatch, capsys, git_env, base):
    _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)  # origin/main strict, the PR's own config advisory
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    rc, out, err = _mode(monkeypatch, capsys, "--base", base)
    assert rc != 0 and out is None and "not a plain branch or ref name" in err
    rc, out = _cli(m, capsys, "strict", "check", "SPK-1001", "--phase", "pr", "--base", base)
    assert rc != 0 and not (out or {}).get("ok")
    assert _gate(capsys, str(tmp_path / "out" / "gate.md"), "--base", base)[0] == 3


def test_a_revision_expression_base_beside_an_advisory_default_branch_is_unchanged(tmp_path, monkeypatch, capsys, git_env):
    _gate_pr(tmp_path, monkeypatch, git_env, ORG, ORG)  # nothing strict anywhere: `--base HEAD~1` as a diff range stays usable
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    assert _mode(monkeypatch, capsys, "--base", "HEAD~1")[1] == {"mode": "advisory", "source": PULL}


# ---- DF4 (re-review N6): a PR into `develop` in CI is judged by the stricter of origin/develop and a guarded origin/main ----
@pytest.mark.parametrize("develop", [ORG, {**STRICT, "labels": ["develop"]}])
@pytest.mark.parametrize("set_head", [False, True])
def test_df4_ci_pr_into_develop_is_judged_by_the_stricter_side(tmp_path, monkeypatch, capsys, git_env, develop, set_head):
    """origin/main strict; origin/develop advisory (weaker) or strict; the PR advisory. The check-pull-request template passes
    `--base origin/develop` and, with the online gate, points origin/HEAD at it."""
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG, head_files={"GoneTest.cls": "class Gone { testX }"})
    git(tmp_path, "checkout", "-q", "-b", "develop", "main")
    commit(tmp_path, "SPK-1001 develop", {"skj.config.json": _cfg_text(develop)})
    git(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    git(tmp_path, "checkout", "-q", "feature")
    if set_head:
        git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
    monkeypatch.setenv("CI", "true")
    rc, out = _gate(capsys, summary, "--base", "origin/develop")
    assert out["config"] == "target branch (strict)", (develop, set_head)  # never the PR's advisory file
    assert rc == 0 and out["blocked_by"] == [], json.dumps(out)  # judged strict, and it passes: the AC's test exists
    git(tmp_path, "checkout", "-q", "-b", "broken", "feature")
    commit(tmp_path, "SPK-1001 drop the test", {"GoneTest.cls": "class Gone { }"})
    rc, out = _gate(capsys, summary, "--base", "origin/develop")
    assert rc == 1 and "ac_traceability" in out["blocked_by"]  # strict main's floor blocks it
    if develop == ORG and not set_head:  # outside CI a --base is read as given (read-only verbs): the weaker develop, so the PR's own file
        monkeypatch.setenv("CI", "")
        assert "config" not in _gate(capsys, summary, "--base", "origin/develop")[1]


# ---- DF4 (re-review N5): a bare 40-hex --base outside CI is trusted only when it is the tip of a remote branch ----
def _old_commit(repo, cfg):
    """A commit with `cfg` that no branch points at any more (an old main, say)."""
    git(repo, "checkout", "-q", "-b", "old", "main")
    commit(repo, "SPK-1001 old", {"skj.config.json": _cfg_text(cfg)})
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "feature")
    git(repo, "branch", "-qD", "old")
    return sha


def test_df4_a_commit_id_base_is_refused_beside_a_strict_default_branch(tmp_path, monkeypatch, capsys, git_env):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    old = _old_commit(tmp_path, ORG)  # advisory at that commit
    rc, out, err = _mode(monkeypatch, capsys, "--base", old)
    assert rc != 0 and out is None and "commit id" in err and old not in err
    assert _gate(capsys, summary, "--base", old)[0] == 3
    tip = git(tmp_path, "rev-parse", "refs/remotes/origin/main")  # the exact id of a remote branch tip stays usable
    assert _mode(monkeypatch, capsys, "--base", tip)[1] == {"mode": "strict", "source": TARGET}
    strict_old = _old_commit(tmp_path, {**STRICT, "labels": ["old"]})  # an old commit that is just as strict stays usable
    assert _mode(monkeypatch, capsys, "--base", strict_old)[1] == {"mode": "strict", "source": TARGET}
    monkeypatch.setenv("CI", "true")  # in CI it is not refused: judged by the stricter side, the strict main (DF4 N6)
    assert _mode(monkeypatch, capsys, "--base", old)[1] == {"mode": "strict", "source": TARGET}
    monkeypatch.setenv("CI", "")
    git(tmp_path, "update-ref", "refs/remotes/origin/develop", old)  # now the tip of a remote branch: read as given (advisory)
    assert _mode(monkeypatch, capsys, "--base", old)[1] == {"mode": "advisory", "source": PULL}


def test_df4_a_commit_id_base_beside_an_advisory_default_branch_is_unchanged(tmp_path, monkeypatch, capsys, git_env):
    _gate_pr(tmp_path, monkeypatch, git_env, ORG, ORG)
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    assert _mode(monkeypatch, capsys, "--base", _old_commit(tmp_path, STRICT))[1] == {"mode": "strict", "source": TARGET}


# ---- DF5 (re-review D1): in CI with no --base, origin/HEAD at a strict PR target beside a main guarded by comment approval only ----
def _df5_d1(tmp_path, monkeypatch, git_env):
    """origin/main advisory but guarded (comment approval, an approver), origin/develop strict, the PR advisory; origin/HEAD -> develop
    (what the ci/ templates do); CI=true; no --base anywhere."""
    guarded = {**ORG, "approvers": ["557058:aaaa"], "approval": "comment"}
    summary = _gate_pr(tmp_path, monkeypatch, git_env, guarded, ORG)
    git(tmp_path, "checkout", "-q", "-b", "develop", "main")
    commit(tmp_path, "SPK-1001 develop", {"skj.config.json": _cfg_text({**STRICT, "approvers": ["557058:aaaa"]})})
    git(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    git(tmp_path, "checkout", "-q", "feature")
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
    monkeypatch.setenv("CI", "true")
    return summary


def test_df5_ci_no_base_strict_mode_and_gate_use_the_strict_target(tmp_path, monkeypatch, capsys, git_env):
    summary = _df5_d1(tmp_path, monkeypatch, git_env)
    assert _mode(monkeypatch, capsys)[1] == {"mode": "strict", "source": TARGET}
    rc, out = _gate(capsys, summary)
    assert rc == 1 and out["config"] == "target branch (strict)" and "ac_traceability" in out["blocked_by"]


def test_df5_ci_no_base_done_and_pr_create_are_judged_strict(tmp_path, monkeypatch, capsys, git_env):
    _df5_d1(tmp_path, monkeypatch, git_env)
    assert m._write_judge(None).mode == "strict"  # the judge of `done` and `pr create` without --base
    assert main(["done", "SPK-1001"]) == 2 and "strict check failed" in capsys.readouterr().err  # strict findings, nothing read from Jira
    assert main(["pr", "create", "SPK-1001"]) == 2 and "strict check failed" in capsys.readouterr().err
    monkeypatch.setenv("CI", "")  # outside CI origin/HEAD -> develop beside a guarded main is still refused (target-moved)
    assert _mode(monkeypatch, capsys)[0] == 3


def test_df5_ci_no_base_pr_create_targets_the_branch_origin_head_names(tmp_path, monkeypatch, capsys, git_env):
    """D1: `skj pr create` without --base in CI opens the PR against origin/HEAD's branch (the PR's target), not the home branch."""
    _gate_pr(tmp_path, monkeypatch, git_env, {**ORG, "approvers": ["557058:aaaa"], "approval": "comment"}, ORG)
    git(tmp_path, "checkout", "-q", "-b", "develop", "main")
    commit(tmp_path, "SPK-1001 develop", {"skj.config.json": _cfg_text(ORG)})
    git(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    git(tmp_path, "checkout", "-q", "feature")
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
    monkeypatch.setenv("CI", "true")
    assert main(["pr", "create", "SPK-1001", "--allow-incomplete"]) == 0  # a dry run: nothing sent
    assert "| base | develop |" in capsys.readouterr().out


# ---- DF6 (re-review R2): each of the D1 call sites (`gate`, `strict`, `done`) is pinned by a test of its own ----
def _df6_layout(tmp_path, monkeypatch, git_env, home, develop):
    """origin/main holds `home`, origin/develop (one commit later) holds `develop`, the PR advisory; origin/HEAD -> develop; CI=true."""
    summary = _gate_pr(tmp_path, monkeypatch, git_env, home, ORG)
    git(tmp_path, "checkout", "-q", "-b", "develop", "main")
    commit(tmp_path, "SPK-1001 develop", {"skj.config.json": _cfg_text(develop)})
    git(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    git(tmp_path, "checkout", "-q", "feature")
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
    monkeypatch.setenv("CI", "true")
    return summary


def _spy(monkeypatch, mod, name):
    seen = []
    real = getattr(mod, name)
    monkeypatch.setattr(mod, name, lambda *a, **kw: seen.append(kw) or real(*a, **kw))
    return seen


def _unreachable():
    raise ConnectionError("no Jira in this test")


def test_df6_ci_no_base_gate_walks_the_range_from_the_target_branch(tmp_path, monkeypatch, capsys, git_env):
    from skj import gate
    summary = _df5_d1(tmp_path, monkeypatch, git_env)
    seen = _spy(monkeypatch, gate, "evaluate")
    _gate(capsys, summary)
    assert seen[-1]["base"] == git(tmp_path, "rev-parse", "refs/remotes/origin/develop") and seen[-1]["target"] == "origin/develop"


def test_df6_ci_no_base_strict_check_takes_the_target_branch_as_its_range_base(tmp_path, monkeypatch, capsys, git_env):
    _df5_d1(tmp_path, monkeypatch, git_env)
    seen = _spy(monkeypatch, strict, "check")
    main(["strict", "check", "SPK-1001", "--phase", "pr"])
    assert seen[-1]["base"] == "origin/develop" and seen[-1]["target"] == "origin/develop"


def test_df6_ci_no_base_done_reads_the_approvers_only_the_target_branch_names(tmp_path, monkeypatch, capsys, git_env):
    """main strict with no approvers, develop advisory naming one: the cross-check runs with develop's approver, never skipped because
    main names nobody (the strict findings then stop `done`, exit 2, before any Jira read)."""
    _df6_layout(tmp_path, monkeypatch, git_env, STRICT, {**ORG, "approvers": ["557058:aaaa"]})
    seen = []
    monkeypatch.setattr(strict, "xcheck_run", lambda f, cfg, key, md, approvers, approval: seen.append(approvers) or [])
    assert main(["done", "SPK-1001"], client_factory=_unreachable) == 2
    assert seen == [["557058:aaaa"]]


# ---- DF6 (re-review R3): CI, no --base, home and target name different approvers: the refusal names the real fix ----
def test_df6_ci_no_base_done_with_two_approver_lists_names_the_fix_not_a_flag(tmp_path, monkeypatch, capsys, git_env):
    _df6_layout(tmp_path, monkeypatch, git_env, {**ORG, "approvers": ["557058:aaaa"], "approval": "comment"},
                {**ORG, "approvers": ["557058:aaaa", "557058:bbbb"], "approval": "comment"})
    assert main(["done", "SPK-1001"], client_factory=_unreachable) == 1
    err = capsys.readouterr().err
    assert "origin/develop" in err and "origin/main" in err and "same" in err and "without --base" not in err


# ---- DF5 (re-review D5): only a refs/remotes/origin/* tip counts as a branch tip for a commit-id --base ----
def test_df5_a_commit_id_that_is_only_a_tip_outside_origin_is_still_refused(tmp_path, monkeypatch, capsys, git_env):
    summary = _gate_pr(tmp_path, monkeypatch, git_env, STRICT, ORG)
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    old = _old_commit(tmp_path, ORG)
    git(tmp_path, "update-ref", "refs/remotes/zz/old", old)  # a tip, but not of an origin branch
    rc, out, err = _mode(monkeypatch, capsys, "--base", old)
    assert rc != 0 and out is None and "commit id" in err
    assert _gate(capsys, summary, "--base", old)[0] == 3
