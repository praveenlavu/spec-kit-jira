"""L5a (new; registry updated in L5b, A3 and RU3): every strict check whose module is not built yet FAILS CLOSED (L5a-AC3: since RU3 none
is left; the approvals cross-check is real, tests/test_trust_crosscheck.py, and the business rules, tests/test_rules_strict.py), and the
binding notes of the L3/L4 reviews: `skj gate` / `skj stack show --check` read trust from the TARGET branch (note 1, 6), strict fails
closed on a missing ledger (2), `gate.block` accepts the rule checks now they fire (3, RU3), the smart-commit check is in the strict
floor (4), and an unresolvable base in CI fails closed under a strict config (5)."""
import json
import os

import pytest

from skj import __main__ as m
from skj import config, decisions, strict
from skj.config import Config, ConfigInvalid
from test_stack_support import approve, commit, git, profile, git_env  # noqa: F401  (git_env: fixture)
from test_strict import (CMD, KEY, SITE, _d, _project, _rules, _story, _no_credentials, _stub_detect)  # noqa: F401  (fixture)

STUB_TASKS = {}  # L5b replaced the board, kits and review stubs; A3 the cross-check stub; RU3 the rules stub



def test_fm_ac3_the_module_docstring_does_not_describe_an_empty_stub_registry():  # Fix-Minor (final review minor)
    assert not strict.STUBS and "STUBS" not in strict.__doc__ and "unavailable" not in strict.__doc__


# ---- L5a-AC3 (as of L5b): the stub registry, and each stub that is left blocks ----
def test_the_registry_names_each_stub_and_the_task_that_replaces_it():
    assert set(strict.STUBS) == set(STUB_TASKS)
    for rule, task in STUB_TASKS.items():
        what, owner = strict.STUBS[rule]
        assert owner.startswith(task) and what
        f = strict._unavailable(rule)
        assert f["rule"] == rule and "not available in this build" in f["detail"] and task in f["fix"]
    assert strict.DEGRADED == {}
    src = open(strict.__file__, encoding="utf-8").read()
    for gone in ("board-unavailable", "kits-unavailable", "review-unavailable", "xcheck-unavailable", "rules-unavailable", "S3b (kits)",
                 "P1 (prs)", "W-phase (push)", "A3 (approvals)", "RU3 (rules)"):
        assert gone not in src, gone


def test_no_stub_is_overridable():
    covered = {r for rs in strict._COVERS.values() for r in rs}
    assert not set(strict.STUBS) & covered and not set(strict.STUBS) & set(strict._COVERS)


@pytest.mark.parametrize("phase", ["pr", "done"])
def test_no_stub_blocks_a_clean_story_any_more(tmp_path, git_env, phase):
    client, _ = _story(tmp_path, git_env)  # PHASES as shipped (this module does not use test_strict's `_core_only`)
    res = strict.check(client, KEY, phase, repo=str(tmp_path))
    assert res == {"phase": phase, "mode": "strict", "ok": True, "findings": [], "overrides_used": []}


def test_the_real_rules_check_blocks_even_with_an_override_of_every_overridable_rule(tmp_path, git_env):
    dec = "".join(_d(n + 1, f"skip {r}", kind="override", posted=True) for n, r in enumerate(strict._COVERS))
    client, _ = _story(tmp_path, git_env, dec=decisions.render_file(KEY) + dec)
    client.rules["enabled"] = True  # the story's ledger has no `Rules from:` line: the real check fails closed on the header
    for phase in ("pr", "done"):
        res = strict.check(client, KEY, phase, repo=str(tmp_path))
        assert "rules-header" in _rules(res) and "rules-unavailable" not in _rules(res) and res["ok"] is False


def test_the_real_rules_check_judges_a_config_with_rules_on(tmp_path):
    cfg = _project(tmp_path)
    assert strict._rules(strict._Ctx(cfg, KEY, str(tmp_path), "pr")) == []  # off: nothing to judge
    on = Config._from_data({"site": SITE, "project": "SPK", "mode": "strict", "rules": {"enabled": True}}, "x")
    assert [f["rule"] for f in strict._rules(strict._Ctx(on, KEY, str(tmp_path), "pr"))] == ["rules-header"]


def test_the_xcheck_stub_is_gone_and_the_real_check_fails_closed():
    """A3: `xcheck_run` is the real cross-check; it never answers `unavailable`, and a read it cannot make is a finding, not a pass."""
    def down():
        raise OSError("no identity")
    md = decisions.render_file(KEY) + _d(1, "pick the v2 api", posted=True)
    assert [f["rule"] for f in strict.xcheck_run(down, None, KEY, md, ["5b10ac8d82e05b22cc7d4ef5"], "comment")] == ["xcheck-unreachable"]
    assert strict.xcheck_run(down, None, KEY, md, ["5b10ac8d82e05b22cc7d4ef5"], "terminal") == []  # nothing pinned: nothing read


def test_the_cli_strict_check_passes_a_clean_story(tmp_path, monkeypatch, capsys, git_env):
    _story(tmp_path, git_env)
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK", "mode": "strict", "strict": {"test_command": CMD}}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    assert m.main(["strict", "check", KEY, "--phase", "done"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_the_stubs_import_no_unbuilt_module():
    import ast
    tree = ast.parse(open(strict.__file__, encoding="utf-8").read())
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not names & {"xcheck"}  # approvals and comments are built since A1/A2 (the cross-check reads them), rules since RU3


# ---- binding note 1: `skj gate` and `strict` read gate.block, mode and gate.tests from the TARGET branch, also without --base ----
LEDGER_MD = "## 2. Brainstorm & plan\n## 3. AC\n| AC | x | tests | ev | status |\n|---|---|---|---|---|\n| AC-1 | x | Gone.testX | | red |\n"


def _pr_repo(tmp_path, monkeypatch, target, head, origin_head=True, ledger=LEDGER_MD):
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "SPK-1001 base", {"README.md": "x\n", **({"skj.config.json": json.dumps(target)} if target is not None else {})})
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    if origin_head:
        git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    files = {**({"dod/DoD_SPK-1001.md": ledger} if ledger is not None else {}), **({"skj.config.json": json.dumps(head)} if head is not None else {})}
    commit(tmp_path, "SPK-1001 feature", files)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    for k in ("CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(k, raising=False)


def _gate(capsys, *extra):
    rc = m.main(["gate", KEY, "--offline", "--src", ".", *extra])
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


ORG = {"site": SITE, "project": "SPK"}
STRICT = {**ORG, "mode": "strict", "gate": {"block": ["open_decisions"]}}


@pytest.mark.parametrize("head", [ORG, {**ORG, "gate": {"block": []}, "mode": "advisory"}, None], ids=["advisory", "lowered", "deleted"])
def test_note1_gate_without_base_reads_the_target_at_origin_head_and_the_pr_cannot_lower_it(tmp_path, monkeypatch, capsys, git_env, head):
    _pr_repo(tmp_path, monkeypatch, STRICT, head)
    rc, out = _gate(capsys)
    assert rc == 1 and out["config"] == "target branch (strict)" and "ac_traceability" in out["blocked_by"]
    assert set(config.STRICT_GATE_BLOCK) <= set(Config._from_data(STRICT, "x").effective_gate_block())


def test_note1_mutation_guard_the_working_tree_alone_would_pass(tmp_path, monkeypatch, capsys, git_env):
    _pr_repo(tmp_path, monkeypatch, STRICT, ORG, origin_head=False)  # no origin/HEAD and no --base: nothing to read but the PR's file
    assert _gate(capsys)[1]["config"] == "target branch (strict)"  # DF3 M2: an unset origin/HEAD: judged by the strict origin/main
    git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")  # no remote branch at all
    rc, out = _gate(capsys)
    assert rc == 0 and "config" not in out  # the advisory head (the fork's v0.2): exactly what the target read above prevents


def test_note1_strict_mode_and_check_read_the_target_without_base(tmp_path, monkeypatch, capsys, git_env):
    _pr_repo(tmp_path, monkeypatch, STRICT, ORG)
    assert m.main(["strict", "mode"]) == 0 and json.loads(capsys.readouterr().out) == {"mode": "strict", "source": "target branch"}
    assert m.main(["strict", "check", KEY, "--phase", "done"]) == 2
    assert json.loads(capsys.readouterr().out)["mode"] == "strict"


def test_note1_a_revision_expression_base_is_never_read(tmp_path, monkeypatch, capsys, git_env):
    _pr_repo(tmp_path, monkeypatch, STRICT, STRICT)
    seen, real = [], Config.load_from_ref
    monkeypatch.setattr(Config, "load_from_ref", classmethod(lambda cls, ref, repo=".": seen.append(ref) or real(ref, repo)))
    for base in ("origin/main~1", "origin/main@{1}", "origin/main^"):
        assert _gate(capsys, "--base", base)[0] == 3  # the strict head cannot use it either: fail closed
    assert all(r == "refs/remotes/origin/main" for r in seen)  # only the default branch is read (DF3), never the revision expression


# ---- binding note 2: strict fails closed when the ledger is missing, unreadable, a symlink or over 1 MiB ----
@pytest.mark.parametrize("kind", ["missing", "symlink", "oversize", "not-utf8", "directory"])
def test_note2_strict_gate_blocks_without_a_readable_ledger(tmp_path, monkeypatch, capsys, git_env, kind):
    _pr_repo(tmp_path, monkeypatch, STRICT, ORG, ledger=None)
    p = tmp_path / "dod" / "DoD_SPK-1001.md"
    p.parent.mkdir(exist_ok=True)
    if kind == "symlink":
        (tmp_path / "elsewhere.md").write_text(LEDGER_MD)
        os.symlink(tmp_path / "elsewhere.md", p)
    elif kind == "oversize":
        p.write_text(LEDGER_MD + "x" * (1 << 20))
    elif kind == "not-utf8":
        p.write_bytes(b"\xff\xfe" + LEDGER_MD.encode())
    elif kind == "directory":
        p.mkdir()
    summary = tmp_path / "out" / "gate.md"
    rc, out = _gate(capsys, "--summary-out", str(summary))
    assert rc == 1 and out["blocking"] is True and out["blocked_by"] == ["ledger"] and "strict mode blocks" in out["note"]
    assert "**Blocking - this check fails the PR:**" in summary.read_text()
    git(tmp_path, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    assert _gate(capsys)[1]["config"] == "target branch (strict)"  # DF3 M2: an unset origin/HEAD: judged by the strict origin/main
    git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")  # the advisory head alone: the old note, never blocking
    rc, out = _gate(capsys)
    assert rc == 0 and out["blocking"] is False and "strict" not in out["note"]


def test_note2_strict_check_fails_closed_on_a_missing_ledger(tmp_path):
    cfg = _project(tmp_path, ledger=False)
    for phase in ("implement", "pr", "done"):
        assert "ledger-missing" in _rules(strict.check(cfg, KEY, phase, repo=str(tmp_path)))
    assert "ledger-missing" in _rules(strict.verify(cfg, KEY, repo=str(tmp_path)))


# ---- binding note 3: gate.block refused the rule checks while they could not fire; since RU3 they fire and are accepted ----
@pytest.mark.parametrize("name", ["rule_coverage", "rule_drift"])
def test_note3_gate_block_accepts_the_rule_checks_now_they_fire(name):
    assert Config._from_data({**ORG, "gate": {"block": ["ac_drift", name]}}, "x").gate["block"] == ["ac_drift", name]


# ---- binding note 4: the smart-commit check is in the strict floor ----
def test_note4_smart_commit_commands_is_in_the_strict_floor(tmp_path, monkeypatch, capsys, git_env):
    assert "smart_commit_commands" in config.STRICT_GATE_BLOCK
    assert "smart_commit_commands" in Config._from_data({**ORG, "mode": "strict"}, "x").effective_gate_block()
    assert "smart_commit_commands" not in Config._from_data(ORG, "x").effective_gate_block()
    _pr_repo(tmp_path, monkeypatch, STRICT, ORG)
    commit(tmp_path, "SPK-1001 #done finish it", {"x.txt": "x\n"})
    rc, out = _gate(capsys)
    assert rc == 1 and "smart_commit_commands" in out["blocked_by"] and len(out["smart_commit_commands"]) == 1


# ---- binding note 5: a base that cannot be resolved is a note locally and fails closed in CI under a strict config ----
def test_note5_strict_gate_without_a_resolvable_base_fails_closed_in_ci(tmp_path, monkeypatch, capsys, git_env):
    _pr_repo(tmp_path, monkeypatch, None, {**ORG, "mode": "strict"}, origin_head=False)  # a strict PR config, no target, no base
    rc, out = _gate(capsys)
    assert rc == 1 and "commits not checked" in out["commits_note"]  # locally: a note (the floor still blocks on traceability)
    monkeypatch.setenv("CI", "true")
    assert _gate(capsys)[0] == 3
    (tmp_path / "skj.config.json").write_text(json.dumps(ORG))  # fix round 1: an advisory PR config fails closed in CI too
    assert _gate(capsys)[0] == 3
    monkeypatch.delenv("CI")
    rc, out = _gate(capsys)  # outside CI the advisory PR config keeps the v0.2 note
    assert rc == 0 and "commits not checked" in out["commits_note"]


def test_note5_strict_gate_judges_the_resolved_base_not_a_tag(tmp_path, monkeypatch, capsys, git_env):
    _pr_repo(tmp_path, monkeypatch, STRICT, ORG)
    commit(tmp_path, "no story reference", {"y.txt": "y\n"})
    git(tmp_path, "tag", "origin/main", "HEAD")  # rev-parse would prefer this tag over refs/remotes/origin/main
    rc, out = _gate(capsys, "--base", "origin/main")
    assert rc == 1 and len(out["commits_without_story"]) == 1 and "commits_without_story" in out["blocked_by"]


# ---- binding note 6: `stack show --check` compares against the TARGET branch's profile ----
def _stack(tmp_path, monkeypatch, target_cfg, head_cfg, target_profile=None, head_profile=None):
    git(tmp_path, "init", "-q", "-b", "main")
    files = {"README.md": "x\n", **({"skj.config.json": json.dumps(target_cfg)} if target_cfg else {}),
             **({"skj.stack.json": json.dumps(target_profile)} if target_profile else {})}
    commit(tmp_path, "base", files)
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    for name, data in (("skj.config.json", head_cfg), ("skj.stack.json", head_profile)):
        if data is None:
            git(tmp_path, "rm", "-q", "--ignore-unmatch", name)
            (tmp_path / name).unlink() if (tmp_path / name).exists() else None
        else:
            (tmp_path / name).write_text(json.dumps(data))
    commit(tmp_path, "pr")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)


STACK_STRICT = {**ORG, "mode": "strict", "strict": {"test_command": "PYTHONPATH=src python3 -m pytest -q"}}


@pytest.mark.parametrize("head_cfg", [ORG, None], ids=["advisory", "deleted"])
def test_note6_stack_show_check_is_judged_by_the_strict_target(tmp_path, monkeypatch, capsys, git_env, head_cfg):
    _stack(tmp_path, monkeypatch, STACK_STRICT, head_cfg, target_profile=profile(), head_profile=approve(profile()))
    assert m.main(["stack", "show", "--check"]) == 2  # the target's draft judges: the PR's self-approved copy does not pass it
    out = json.loads(capsys.readouterr().out)
    assert any("at the target branch is draft" in n for n in out["notes"]) and out["trust_note"] == strict.TRUST_NOTE
    git(tmp_path, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    assert m.main(["stack", "show", "--check"]) == 2  # DF3 M2: an unset origin/HEAD: the strict origin/main's draft still judges
    capsys.readouterr()
    git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")  # mutation guard: the working tree alone says advisory
    assert m.main(["stack", "show", "--check"]) == 0
    capsys.readouterr()


def test_note6_stack_show_check_passes_an_approved_target_and_flags_a_differing_head(tmp_path, monkeypatch, capsys, git_env):
    _stack(tmp_path, monkeypatch, STACK_STRICT, ORG, target_profile=approve(profile()), head_profile=approve(profile()))
    assert m.main(["stack", "show", "--check"]) == 0
    assert "trust_note" not in json.loads(capsys.readouterr().out)
    (tmp_path / "skj.stack.json").write_text(json.dumps(approve({**profile(), "version": 3})))
    assert m.main(["stack", "show", "--check"]) == 0
    assert json.loads(capsys.readouterr().out)["trust_note"] == strict.TRUST_NOTE  # review it as code


def test_note6_an_invalid_target_profile_fails_closed(tmp_path, monkeypatch, capsys, git_env):
    _stack(tmp_path, monkeypatch, STACK_STRICT, ORG, target_profile={"schema": "nope"}, head_profile=approve(profile()))
    assert m.main(["stack", "show", "--check"]) == 2
    assert "skj.stack.json at the target branch" in capsys.readouterr().err


# ---- fix round 1, IMPORTANT 1: in CI an unresolvable (or absent) base never lets the PR's own config pick the mode ----
EVIL = {"site": "evil.atlassian.net", "project": "SPK", "mode": "advisory", "gate": {"tests": {"python": "."}, "block": []}}


@pytest.mark.parametrize("how", ["missing-ref", "no-base-no-origin-head", "revision-expression"])
def test_ci_unresolvable_base_fails_closed_for_an_advisory_pr_config(tmp_path, monkeypatch, capsys, git_env, how):
    _pr_repo(tmp_path, monkeypatch, STRICT, EVIL, origin_head=False)
    git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")  # a shallow checkout: no origin/main, no origin/HEAD
    extra = {"missing-ref": ["--base", "origin/nonexistent"], "no-base-no-origin-head": [], "revision-expression": ["--base", "main~1"]}[how]
    monkeypatch.setenv("CI", "1")
    assert _gate(capsys, *extra)[0] == 3
    assert m.main(["strict", "mode", *extra]) == 3
    err = capsys.readouterr().err
    assert "in CI the target branch must be readable" in err and "evil" not in err
    assert m.main(["strict", "check", KEY, "--phase", "done", *extra]) == 2
    assert m.main(["stack", "show", "--check", *extra]) == 2  # stack's refusals are exit 2 (cmd_stack), as every stack verb's
    capsys.readouterr()
    monkeypatch.delenv("CI")  # outside CI: today's behaviour (the PR's advisory file, a note)
    rc, out = _gate(capsys, *extra)
    assert rc == 0 and out["blocking"] is False
    assert m.main(["strict", "mode", *extra]) == 0 and json.loads(capsys.readouterr().out) == {"mode": "advisory", "source": "pull request"}


def test_ci_with_a_resolvable_base_still_judges_normally(tmp_path, monkeypatch, capsys, git_env):
    _pr_repo(tmp_path, monkeypatch, STRICT, EVIL)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    rc, out = _gate(capsys, "--base", "origin/main")
    assert rc == 1 and out["config"] == "target branch (strict)"
    assert m.main(["strict", "mode"]) == 0 and json.loads(capsys.readouterr().out)["source"] == "target branch"


# ---- fix round 1, IMPORTANT 2: verify refuses (runs nothing) while this process's environment holds a credential ----
@pytest.mark.parametrize("name", ["SKJ_TOKEN", "SKJ_EMAIL", "GITHUB_TOKEN", "DEPLOY_KEY", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK"])
def test_verify_refuses_with_a_credential_in_the_environment_and_starts_nothing(tmp_path, git_env, monkeypatch, name):
    client, _ = _story(tmp_path, git_env, record=False)
    monkeypatch.setenv(name, "ZZ-SECRET-VALUE")
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("no process may be started"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no process may be started"))
    monkeypatch.setattr(strict, "run_at", lambda *a, **k: pytest.fail("no test may run"))
    for record in (True, False):
        res = strict.verify(client, KEY, record=record, repo=str(tmp_path))
        assert res["ok"] is False and _rules(res) == ["credentials-in-env"] and res["recorded"] is False
        assert name in res["findings"][0]["detail"] and "ZZ-SECRET-VALUE" not in json.dumps(res)
        assert "without" in res["findings"][0]["fix"] or "no SKJ_TOKEN" in res["findings"][0]["fix"]
    dec = "".join(_d(n + 1, f"skip {r}", kind="override", posted=True) for n, r in enumerate(strict._COVERS))
    (tmp_path / "dod" / f"Decisions_{KEY}.md").write_text(decisions.render_file(KEY) + dec)
    assert _rules(strict.verify(client, KEY, record=False, repo=str(tmp_path), allow_override=True)) == ["credentials-in-env"]  # not overridable


def test_verify_runs_in_a_clean_environment_and_the_refusal_shares_the_runner_rule(tmp_path, git_env):
    client, _ = _story(tmp_path, git_env, record=False)
    assert strict.verify(client, KEY, record=False, repo=str(tmp_path))["ok"] is True  # the autouse fixture cleared every credential
    for k in ("SKJ_X", "MY_PAT", "A_PASSWORD", "GH_TOKEN", "PATH", "HOME", "LANG"):
        assert strict._denied(k) == (k not in ("PATH", "HOME", "LANG"))
        assert (k in strict.scrubbed_env({k: "/v"})) == (not strict._denied(k) and k in strict._ENV_ALLOW + ("HOME",))
    assert strict._credentials_in_env({"BAD NAME_TOKEN": "x"})[0]["detail"].count("?") == 1  # an odd name is never echoed


def test_the_gate_block_refusal_lists_only_the_checks_it_accepts():
    with pytest.raises(ConfigInvalid) as e:
        Config._from_data({**ORG, "gate": {"block": ["nope"]}}, "x")
    assert "rule_coverage" in str(e.value) and "rule_drift" in str(e.value) and "smart_commit_commands" in str(e.value)
