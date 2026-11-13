"""Ported from the fork's tests/test_strict_wire.py (L5a): `strict check --base/--head`, the default base, tag shadowing, the gate in strict
mode. The fork's `pr create`, `done` and override-stamping tests wait for the verbs (P1, P4, W-phase): see task-L5a-report.md."""
import json
import subprocess

import pytest

from skj import __main__ as m
from skj import decisions, strict
from test_stack_support import commit, git, git_env  # noqa: F401  (git_env: fixture)
from test_strict import (CMD, KEY, SITE, _core_only, _d, _pr, _rules, _story, _no_credentials, _stub_detect, all_checks, real_board, real_kits, real_review, real_detect)  # noqa: F401  (fixtures)


def _wire(tmp_path, monkeypatch, git_env, mode="strict", committed=False, hook=None, **kw):
    """The `_story` repo (branch main; `trunk` = the commit before the red commit) with an untracked skj.config.json in the working tree
    (`committed`: also committed with the spec, so a run against the head reads it): returns (main module, story)."""
    cfg = {"site": SITE, "project": "SPK", "strict": {"test_command": CMD}, **({"mode": mode} if mode else {})}
    _, s = _story(tmp_path, git_env, **kw, **({"config": cfg} if committed else {}))
    if hook:
        hook(tmp_path)
    git(tmp_path, "branch", "trunk", s.base)
    (tmp_path / "skj.config.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    return m, s


# ---- base and head ----
def _full(tmp_path, rev):
    return git(tmp_path, "rev-parse", rev)


def _check(tmp_path, client, **kw):
    return strict.check(client, KEY, "pr", repo=str(tmp_path), **kw)


def test_base_range_two_old_commits_are_not_in_range(tmp_path, git_env):
    client, s = _story(tmp_path, git_env)
    assert _check(tmp_path, client, base=_full(tmp_path, s.base))["ok"] is True  # base before the red commit: both are this branch's work
    res = _check(tmp_path, client, base=_full(tmp_path, s.green))  # red and green are both already in the base
    assert _rules(res) == ["not-in-range", "not-in-range"]
    assert {f["detail"].split(" commit")[0].split(": ")[1] for f in res["findings"]} == {"the red", "the green"}
    assert _check(tmp_path, client)["ok"] is True  # no base given: no range check (strict check keeps it optional)


@pytest.mark.parametrize("base", ["no-such-branch", "", "--output=x", "-h"])
def test_unresolvable_base_is_base_unknown_and_never_overridable(tmp_path, git_env, base):
    dec = decisions.render_file(KEY) + _d(1, "skip l2", kind="override", posted=True)
    client, s = _story(tmp_path, git_env, dec=dec)
    res = _check(tmp_path, client, base=base)
    assert _rules(res) == ["base-unknown"] and res["ok"] is False
    assert set(res["findings"][0]) == {"rule", "detail", "fix"}
    assert not (tmp_path / "x").exists()


def test_check_head_sha_on_a_detached_checkout(tmp_path, git_env):
    client, s = _story(tmp_path, git_env)
    head = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "--detach")
    assert _check(tmp_path, client, head=head, base=_full(tmp_path, s.base)) == {"phase": "pr", "mode": "strict", "ok": True, "findings": [],
                                                                                "overrides_used": []}
    assert _check(tmp_path, client, head=head[:10], base=_full(tmp_path, s.base))["ok"] is True  # a short SHA is resolved to the commit
    # an older commit as the head: the record is for a newer head
    commit(tmp_path, "code change", {"src/app.py": "x = 3\n"})
    assert _check(tmp_path, client, head=git(tmp_path, "rev-parse", "HEAD"), base=_full(tmp_path, s.base))["ok"] is False


def test_cli_strict_check_takes_base_and_head(tmp_path, monkeypatch, capsys, git_env):
    mod, s = _wire(tmp_path, monkeypatch, git_env)
    head = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "--detach")
    assert mod.main(["strict", "check", KEY, "--phase", "pr", "--head", head, "--base", _full(tmp_path, s.base)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert mod.main(["strict", "check", KEY, "--phase", "pr", "--head", head, "--base", "nope"]) == 2
    cap = capsys.readouterr()  # an unknown --base under a strict PR config is refused before any check: fail closed
    assert cap.out == "" and "skj.config.json at the target branch" in cap.err


def test_gate_in_strict_blocks_on_default_checks(tmp_path, monkeypatch, capsys, git_env):
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "SPK-1001 base", {"dod/DoD_SPK-1001.md": "## 2. Brainstorm & plan\n## 3. AC\n| AC | x | t | e | s |\n|---|---|---|---|---|\n"
                                                             "| AC-1 | x | Gone.testX | | red |\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK"}))
    argv = ["gate", KEY, "--offline", "--src", str(tmp_path)]
    assert m.main(argv) == 0  # advisory, no gate.block: nothing blocks
    capsys.readouterr()
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK", "mode": "strict"}))
    assert m.main(argv) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["blocking"] is True and "ac_traceability" in out["blocked_by"]


# ---- the default base, no tag shadowing ----
def _rules_out(capsys):
    return [f["rule"] for f in json.loads(capsys.readouterr().out)["findings"]]


def test_strict_check_pr_defaults_the_base_to_origin_head(tmp_path, monkeypatch, capsys, git_env):
    mod, s = _wire(tmp_path, monkeypatch, git_env)
    argv = ["strict", "check", KEY, "--phase", "pr"]
    assert mod.main(argv) == 2 and _rules_out(capsys) == ["base-unknown"]  # no --base and no origin/HEAD: never a pass
    git(tmp_path, "update-ref", "refs/remotes/origin/trunk", s.green)  # the remote base already holds red and green
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert mod.main(argv) == 2 and _rules_out(capsys) == ["not-in-range", "not-in-range"]
    git(tmp_path, "update-ref", "refs/remotes/origin/trunk", s.base)  # a base before the red commit
    assert mod.main(argv) == 0 and json.loads(capsys.readouterr().out)["ok"] is True
    git(tmp_path, "update-ref", "refs/remotes/origin/trunk", s.green)
    assert mod.main(argv + ["--base", _full(tmp_path, s.base)]) == 0  # an explicit --base wins over origin/HEAD
    capsys.readouterr()
    assert mod.main(["strict", "check", KEY, "--phase", "done"]) == 0  # done has no base


def test_the_default_base_is_never_overridable(tmp_path, monkeypatch, capsys, git_env):
    dec = decisions.render_file(KEY) + _d(1, "skip l2", kind="override", posted=True)
    mod, s = _wire(tmp_path, monkeypatch, git_env, dec=dec)
    assert mod.main(["strict", "check", KEY, "--phase", "pr"]) == 2 and _rules_out(capsys) == ["base-unknown"]


def test_strict_check_base_resolves_remote_then_local_then_a_full_commit_id_never_a_tag(tmp_path, git_env):
    client, s = _story(tmp_path, git_env)
    base, green = _full(tmp_path, s.base), _full(tmp_path, s.green)
    git(tmp_path, "tag", "v1", base)
    git(tmp_path, "tag", "origin/x", base)
    for name in ("v1", "origin/x", s.base, "nope"):  # a tag, a tag named like a remote ref, a short commit id, nothing
        assert _rules(_check(tmp_path, client, base=name)) == ["base-unknown"], name
    git(tmp_path, "update-ref", "refs/remotes/origin/x", green)
    git(tmp_path, "branch", "x", base)
    assert _rules(_check(tmp_path, client, base="origin/x")) == ["not-in-range"] * 2  # refs/remotes/origin/x, not the tag
    assert _rules(_check(tmp_path, client, base="x")) == []  # refs/remotes/x is absent: refs/heads/x (before red), not a tag
    assert _check(tmp_path, client, base=base)["ok"] is True and _rules(_check(tmp_path, client, base=green)) == ["not-in-range"] * 2
    assert _rules(_check(tmp_path, client, base="refs/tags/v1")) == ["base-unknown"]


@pytest.mark.parametrize("phase", ["pr", "done"])
def test_strict_check_never_executes_the_test_command(tmp_path, git_env, monkeypatch, phase, real_detect):
    """(`real_detect`: in this build `done` reads git only for the stack detection; the fork's review and kit checks are stubs.)"""
    client, _ = _story(tmp_path, git_env)
    real_run, real_popen, git_calls = subprocess.run, subprocess.Popen, []

    def only_git(real):  # `subprocess.run` is built on Popen: both may start git and nothing else (the test command runs via /bin/sh)
        def wrapped(args, *a, **k):
            assert args[0] == "git", f"strict check ran {args[0]!r}"
            git_calls.append(args)
            return real(args, *a, **k)
        return wrapped

    def boom(*a, **k):
        raise AssertionError("strict check must not run the test command")
    monkeypatch.setattr(strict, "run_at", boom)
    monkeypatch.setattr(strict, "run_configured", boom)
    monkeypatch.setattr(subprocess, "run", only_git(real_run))
    monkeypatch.setattr(subprocess, "Popen", only_git(real_popen))
    res = _pr(tmp_path, client, phase)
    assert res["phase"] == phase and git_calls  # it did read git (a vacuous pass would read nothing)
    assert not any(f["rule"] in ("red-passes", "green-fails", "head-fails", "runner-error", "timeout") for f in res["findings"])
