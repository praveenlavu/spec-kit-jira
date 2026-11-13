"""RU3: `rules.reconfirmed`: the ledger's `Rules reviewed:` against the TARGET branch's copy of the ledger and the PR's earlier versions (real
temp git repos, as tests/test_ci_trust.py). Ported from the fork's tests/test_rules_reconfirmed.py (work item ids -> issue keys; the
ledger path is always `dod/DoD_<KEY>.md`, so the fork's `dod_path` cases are dropped, see tests/PORT_NOTES.md). A new id may be confirmed
without a terminal, so an agent could delete the line and confirm again: the target's copy shows it. All data is fictional."""
import json
import subprocess

import pytest

from skj import gate, rules, strict
from skj.dod import SECTION2
from test_ci_trust import ORG, STRICT, _gate, _gate_pr, _md, _target
from test_rules_strict import RREV, _rstory
from test_stack_support import commit, git, git_env  # noqa: F401  (git_env: fixture)
from test_strict import KEY, LEDGER, _core_only, _no_credentials, _pr  # noqa: F401  (autouse fixtures)

pytestmark = pytest.mark.rules

TARGET_CFG = {**STRICT, "rules": {"enabled": True}}
A, B, C = "aaaaaaaa", "bbbbbbbb", "cccccccc"
SECRET = "SECRET-MARK"
LED = "dod/DoD_SPK-1001.md"


def _ledger(reviewed=None, frm="none", extra=""):
    """A ledger: header (`Rules from:`, and `Rules reviewed:` when `reviewed` is a string), then `extra` text, then section 2 onwards."""
    lines = f"- **Rules from:** {frm}\n" + ("" if reviewed is None else f"- **Rules reviewed:** {reviewed}\n")
    return f"# DoD — SPK-1001\n\n{lines}{extra}\n{SECTION2}\n\n## 3. AC\n| AC | x | tests | ev | status |\n|---|---|---|---|---|\n| AC-1 | x | Gone.testX | | red |\n"


def _setup(tmp_path, monkeypatch, git_env, target, head, cfg=TARGET_CFG, head_cfg=ORG):
    """origin/main = a commit holding the ledger `target` (None: no ledger there) and `cfg`; the checked-out branch holds the ledger `head`."""
    return _gate_pr(tmp_path, monkeypatch, git_env, cfg, head_cfg, target_files={LED: target} if target is not None else None, head_files={LED: head})


def _run(capsys, summary, *extra):
    return _gate(capsys, summary, "--base", "origin/main", *extra)


def test_deleted_reviewed_line_is_reconfirmed_all_ids(tmp_path, monkeypatch, capsys, git_env):
    summary = _setup(tmp_path, monkeypatch, git_env, _ledger(f"BR-SPK-1201-2@{B}, BR-SPK-1201-10@{C}, BR-SPK-1201-1@{A}"), _ledger())
    rc, out = _run(capsys, summary)
    assert out["rules"]["reconfirmed"] == [{"id": "BR-SPK-1201-1", "old": A, "new": None}, {"id": "BR-SPK-1201-2", "old": B, "new": None},
                                           {"id": "BR-SPK-1201-10", "old": C, "new": None}]  # (issue, number) order
    assert "rules reconfirmed: BR-SPK-1201-1 (aaaaaaaa -> none), BR-SPK-1201-2 (bbbbbbbb -> none)" in _md(summary)


def test_changed_hash_is_reconfirmed_with_old_and_new(tmp_path, monkeypatch, capsys, git_env):
    summary = _setup(tmp_path, monkeypatch, git_env, _ledger(f"BR-SPK-1201-1@{A}, BR-SPK-1201-2@{B}"), _ledger(f"BR-SPK-1201-1@{C}, BR-SPK-1201-2@{B}"))
    rc, out = _run(capsys, summary)
    assert out["rules"]["reconfirmed"] == [{"id": "BR-SPK-1201-1", "old": A, "new": C}]
    assert "rules reconfirmed: BR-SPK-1201-1 (aaaaaaaa -> cccccccc)" in _md(summary) and "BR-SPK-1201-2" not in _md(summary).split("reconfirmed")[1]


def test_new_id_is_not_reconfirmed(tmp_path, monkeypatch, capsys, git_env):
    summary = _setup(tmp_path, monkeypatch, git_env, _ledger(f"BR-SPK-1201-1@{A}"), _ledger(f"BR-SPK-1201-1@{A}, BR-SPK-1201-2@{B}"))
    rc, out = _run(capsys, summary)
    assert out["rules"]["reconfirmed"] == [] and "reconfirmed" not in _md(summary)


@pytest.mark.parametrize("target", [_ledger(), _ledger("none")], ids=["no-line", "none"])
def test_target_without_line_is_empty(tmp_path, monkeypatch, capsys, git_env, target):
    summary = _setup(tmp_path, monkeypatch, git_env, target, _ledger(f"BR-SPK-1201-1@{A}"))
    rc, out = _run(capsys, summary)
    assert out["rules"]["reconfirmed"] == [] and "reconfirmed_note" not in out["rules"]


UNTRUSTED = {
    "comment-hides-the-first-heading": "# DoD\n\n- **Rules reviewed:** BR-SPK-1201-1@aaaaaaaa\n\n<!--\n## 1. Story\n-->\n" + SECTION2 + "\n",
    "open-fence-in-the-header": "# DoD\n\n```\n- **Rules reviewed:** BR-SPK-1201-1@aaaaaaaa\n\n## 1. Story\n",
    "duplicate-id": "# DoD\n\n- **Rules reviewed:** BR-SPK-1201-1@aaaaaaaa, BR-SPK-1201-1@bbbbbbbb\n\n" + SECTION2 + "\n",
    "second-line": "# DoD\n\n- **Rules reviewed:** BR-SPK-1201-1@aaaaaaaa\n- **Rules reviewed:** BR-SPK-1201-2@bbbbbbbb\n\n" + SECTION2 + "\n",
    "hostile-value": f"# DoD\n\n- **Rules reviewed:** BR-SPK-1201-1@aaaaaaaa, {SECRET}\n\n" + SECTION2 + "\n",
}


@pytest.mark.parametrize("name", UNTRUSTED)
def test_untrusted_target_header_is_empty_not_a_pass_and_not_a_crash(tmp_path, monkeypatch, capsys, git_env, name):
    summary = _setup(tmp_path, monkeypatch, git_env, UNTRUSTED[name], _ledger(f"BR-SPK-1201-1@{C}"))
    rc, out = _run(capsys, summary)
    assert rc in (0, 1) and out["rules"]["reconfirmed"] == [], name
    assert SECRET not in json.dumps(out) + _md(summary)


def _unreadable(capsys, summary):
    rc, out = _run(capsys, summary)
    assert rc in (0, 1) and "reconfirmed" not in out["rules"]  # omitted: neither [] (a pass) nor a guess
    assert out["rules"]["reconfirmed_note"] == rules.RECONFIRM_UNREADABLE == "rules reconfirmed: target ledger unreadable"
    assert rules.RECONFIRM_UNREADABLE in _md(summary)  # never silent


def test_unreadable_target_ledger_warns(tmp_path, monkeypatch, capsys, git_env):
    summary = _setup(tmp_path, monkeypatch, git_env, _ledger(f"BR-SPK-1201-1@{A}"), _ledger())
    parent = git(tmp_path, "rev-parse", "main")
    for files in ({LED: ("120000", "elsewhere")}, {LED: "x" * ((1 << 20) + 1)}):  # a symlink, over 1 MiB
        _target(tmp_path, None, parent, ref="refs/remotes/origin/main", files={"skj.config.json": json.dumps(TARGET_CFG), **files})
        _unreadable(capsys, summary)
    real = strict._blob_at

    def patched(repo, commit_, path, cap, label, bad=b"\xff\xfe# not utf-8"):  # only the ledger read is broken: the config read still works
        if path.endswith("DoD_SPK-1001.md"):
            if bad is None:
                raise strict.ConfigInvalid("the story ledger at the target branch: git could not read the tree")
            return bad
        return real(repo, commit_, path, cap, label)
    monkeypatch.setattr(strict, "_blob_at", patched)
    _unreadable(capsys, summary)
    monkeypatch.setattr(strict, "_blob_at", lambda *a: patched(*a, bad=None))
    _unreadable(capsys, summary)
    monkeypatch.undo()
    for bad in ("origin/nope", "main~1", "main@{1}", "-x"):  # a base that is no commit here, or a revision expression
        assert strict.target_reviewed(str(tmp_path), bad, KEY) is None, bad
    assert strict.reconfirmed_vs_base(str(tmp_path), "main~1", KEY, {}) == rules.RECONFIRM_UNREADABLE


def test_no_base_means_no_key(tmp_path, monkeypatch, capsys, git_env):
    summary = _setup(tmp_path, monkeypatch, git_env, _ledger(f"BR-SPK-1201-1@{A}"), _ledger(), head_cfg={**ORG, "rules": {"enabled": True}})
    assert _gate(capsys, summary)[1]["config"] == "target branch (strict)"  # DF3 M2: no origin/HEAD: the strict origin/main judges
    git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")  # no remote default branch: the PR's own config, as before
    monkeypatch.setattr(strict, "reconfirmed_vs_base", lambda *a, **k: pytest.fail("the target is read only with --base"))
    rc, out = _gate(capsys, summary)  # rules on (the PR's own config), no --base
    assert "rules" in out and "reconfirmed" not in out["rules"] and "reconfirmed_note" not in out["rules"]
    assert "reconfirmed" not in _md(summary)


def test_off_is_byte_identical(tmp_path, monkeypatch, capsys, git_env):
    """Rules absent or disabled: with --base the output is the same whatever the target ledger holds, and the target ledger is never read."""
    for n, cfg in enumerate(({**STRICT}, {**STRICT, "rules": {"enabled": False}})):
        repo = tmp_path / f"r{n}"
        repo.mkdir()
        summary = _setup(repo, monkeypatch, git_env, _ledger(f"BR-SPK-1201-1@{A}"), _ledger(), cfg=cfg)
        monkeypatch.setattr(strict, "reconfirmed_vs_base", lambda *a, **k: pytest.fail("rules are off: the target ledger is not read"))
        rc1, out1 = _run(capsys, summary)
        md1 = _md(summary)
        _target(repo, None, git(repo, "rev-parse", "main"), ref="refs/remotes/origin/main",
                files={"skj.config.json": json.dumps(cfg), LED: _ledger(f"BR-SPK-1201-1@{B}")})
        rc2, out2 = _run(capsys, summary)
        assert (rc1, out1, md1) == (rc2, out2, _md(summary)) and "rules" not in out1 and "reconfirmed" not in md1 + json.dumps(out1)
        monkeypatch.undo()


def test_warning_not_finding_exit_code_unchanged(tmp_path, monkeypatch, capsys, git_env):
    summary = _setup(tmp_path, monkeypatch, git_env, _ledger(f"BR-SPK-1201-1@{A}"), _ledger(f"BR-SPK-1201-1@{A}"))
    rc, out = _run(capsys, summary)
    repo2 = tmp_path / "again"
    repo2.mkdir()
    summary2 = _setup(repo2, monkeypatch, git_env, _ledger(f"BR-SPK-1201-1@{A}"), _ledger())  # the same, but the PR deleted the line
    rc2, out2 = _run(capsys, summary2)
    assert out["rules"]["reconfirmed"] == [] and len(out2["rules"]["reconfirmed"]) == 1
    assert (rc, out["blocked_by"], out["blocking"]) == (rc2, out2["blocked_by"], out2["blocking"])  # a note: no new block
    assert "rules_reconfirmed" not in out2["blocked_by"] and "rules_reconfirmed" not in gate.GATE_CHECKS and "**Advisory:**" in _md(summary2)


def _first_pr(tmp_path, monkeypatch, git_env, versions):
    """A story's FIRST PR: the target has no ledger; the branch commits the ledger versions `versions` (the first in `_setup`'s commit)."""
    summary = _setup(tmp_path, monkeypatch, git_env, None, versions[0])
    for n, v in enumerate(versions[1:], 2):
        commit(tmp_path, f"SPK-1001 step {n}", {LED: v})
    return summary


def test_first_pr_delete_and_reconfirm_is_reconfirmed_from_branch_history(tmp_path, monkeypatch, capsys, git_env):
    summary = _first_pr(tmp_path, monkeypatch, git_env, [_ledger(f"BR-SPK-1201-1@{A}, BR-SPK-1201-2@{B}"), _ledger(), _ledger(f"BR-SPK-1201-1@{C}, BR-SPK-1201-2@{B}")])
    rc, out = _run(capsys, summary)
    assert out["rules"]["reconfirmed"] == [{"id": "BR-SPK-1201-1", "old": A, "new": C}] and "reconfirmed_note" not in out["rules"]
    repo2 = tmp_path / "again"
    repo2.mkdir()
    summary2 = _first_pr(repo2, monkeypatch, git_env, [_ledger(f"BR-SPK-1201-2@{B}, BR-SPK-1201-1@{A}"), _ledger(f"BR-SPK-1201-1@{C}"), _ledger()])
    rc, out = _run(capsys, summary2)
    assert out["rules"]["reconfirmed"] == [{"id": "BR-SPK-1201-1", "old": A, "new": None}, {"id": "BR-SPK-1201-2", "old": B, "new": None}]


def test_first_pr_with_no_earlier_record_gives_note_not_empty_list(tmp_path, monkeypatch, capsys, git_env):
    summary = _first_pr(tmp_path, monkeypatch, git_env, [_ledger(f"BR-SPK-1201-1@{A}")])
    rc, out = _run(capsys, summary)
    assert "reconfirmed" not in out["rules"] and out["rules"]["reconfirmed_note"] == rules.RECONFIRM_FIRST_PR
    assert rules.RECONFIRM_FIRST_PR in _md(summary)
    for n, versions in enumerate(([_ledger(), _ledger(f"BR-SPK-1201-1@{A}")], [_ledger("none"), _ledger(f"BR-SPK-1201-1@{A}")])):
        repo = tmp_path / f"v{n}"
        repo.mkdir()
        rc, out = _run(capsys, _first_pr(repo, monkeypatch, git_env, versions))
        assert out["rules"]["reconfirmed_note"] == rules.RECONFIRM_FIRST_PR and "reconfirmed" not in out["rules"]


def test_first_pr_note_only_when_the_head_records_a_rule(tmp_path, monkeypatch, capsys, git_env):
    for n, head in enumerate((_ledger(), _ledger("none"))):
        repo = tmp_path / f"n{n}"
        repo.mkdir()
        rc, out = _run(capsys, _first_pr(repo, monkeypatch, git_env, [head]))
        assert out["rules"]["reconfirmed"] == [] and "reconfirmed_note" not in out["rules"], n


def test_target_ledger_present_nothing_differs_is_empty(tmp_path, monkeypatch, capsys, git_env):
    summary = _setup(tmp_path, monkeypatch, git_env, _ledger(f"BR-SPK-1201-1@{A}"), _ledger(f"BR-SPK-1201-1@{A}"))
    commit(tmp_path, "SPK-1001 touch", {LED: _ledger(f"BR-SPK-1201-1@{A}", extra="more\n")})
    rc, out = _run(capsys, summary)
    assert out["rules"]["reconfirmed"] == [] and "reconfirmed_note" not in out["rules"] and "reconfirmed" not in _md(summary)
    repo2 = tmp_path / "two"
    repo2.mkdir()
    rc, out = _run(capsys, _first_pr(repo2, monkeypatch, git_env, [_ledger(f"BR-SPK-1201-1@{A}"), _ledger(f"BR-SPK-1201-1@{A}", extra="more\n")]))
    assert out["rules"]["reconfirmed"] == []  # the branch's own earlier record counts too: A -> A is no change


def test_history_over_cap_fixed_text(tmp_path, monkeypatch, capsys, git_env):
    monkeypatch.setattr(strict, "MAX_HISTORY", 2)
    summary = _first_pr(tmp_path, monkeypatch, git_env, [_ledger(f"BR-SPK-1201-1@{A}"), _ledger(), _ledger(f"BR-SPK-1201-1@{C}")])
    rc, out = _run(capsys, summary)
    assert out["rules"]["reconfirmed_note"] == rules.RECONFIRM_TOO_LONG and "reconfirmed" not in out["rules"] and rules.RECONFIRM_TOO_LONG in _md(summary)
    monkeypatch.setattr(strict, "MAX_HISTORY", 3)  # exactly at the cap: compared
    assert _run(capsys, summary)[1]["rules"]["reconfirmed"] == [{"id": "BR-SPK-1201-1", "old": A, "new": C}]


def _merge_checkout(repo, advance):
    """What CI judges: the PR's MERGE commit checked out, the base (possibly moved on) as origin/main. Returns the head commit's sha."""
    head = git(repo, "rev-parse", "feature")
    git(repo, "checkout", "-q", "--detach", "main")
    if advance:
        commit(repo, "SPK-1001 main moves", {"m.txt": "m"})
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "merge", "-q", "--no-ff", "--no-edit", head)
    return head


@pytest.mark.parametrize("advance", [False, True], ids=["base-stays", "base-moves"])
def test_head_is_a_merge_commit_same_as_head_commit(tmp_path, monkeypatch, capsys, git_env, advance):
    versions = {"one-commit": [_ledger(f"BR-SPK-1201-1@{C}")], "two-commits": [_ledger(f"BR-SPK-1201-1@{A}"), _ledger(f"BR-SPK-1201-1@{C}")]}
    for name, vs in versions.items():
        repo = tmp_path / f"{name}-{advance}"
        repo.mkdir()
        summary = _first_pr(repo, monkeypatch, git_env, vs)
        head = _merge_checkout(repo, advance)
        want = rules.RECONFIRM_FIRST_PR if name == "one-commit" else [{"id": "BR-SPK-1201-1", "old": A, "new": C}]
        rc, out = _run(capsys, summary)  # HEAD = the merge commit
        assert out["rules"].get("reconfirmed_note" if name == "one-commit" else "reconfirmed") == want, (name, advance)
        new = {"BR-SPK-1201-1": C}
        assert strict.reconfirmed_vs_base(str(repo), "origin/main", KEY, new, head=head) == want
        assert strict.reconfirmed_vs_base(str(repo), "origin/main", KEY, new, head="HEAD") == want


def test_record_on_a_merged_in_side_branch_is_found_whatever_the_log_order(tmp_path, monkeypatch, capsys, git_env):
    for n, (side, own) in enumerate(((A, C), (C, A))):
        repo = tmp_path / f"s{n}"
        repo.mkdir()
        summary = _setup(repo, monkeypatch, git_env, None, _ledger(f"BR-SPK-1201-1@{side}"))
        git(repo, "checkout", "-q", "-b", "other", "main")
        commit(repo, "SPK-1001 other", {LED: _ledger(f"BR-SPK-1201-1@{own}")})
        git(repo, "merge", "-q", "-s", "ours", "--no-edit", "feature")  # the merged tree keeps `own`
        rc, out = _run(capsys, summary)
        assert out["rules"]["reconfirmed"] == [{"id": "BR-SPK-1201-1", "old": side, "new": own}], n


def test_branch_version_unreadable_has_its_own_note(tmp_path, monkeypatch, capsys, git_env):
    summary = _first_pr(tmp_path, monkeypatch, git_env, [_ledger(f"BR-SPK-1201-1@{A}"), _ledger(f"BR-SPK-1201-1@{C}")])
    real, target = strict._blob_at, git(tmp_path, "rev-parse", "origin/main")

    def patched(repo, commit_, path, cap, label):
        return b"\xff\xfe" if path.endswith("DoD_SPK-1001.md") and commit_ != target else real(repo, commit_, path, cap, label)
    monkeypatch.setattr(strict, "_blob_at", patched)
    rc, out = _run(capsys, summary)
    assert out["rules"]["reconfirmed_note"] == rules.RECONFIRM_BRANCH_UNREADABLE == "rules reconfirmed: a ledger version in the branch history is unreadable"
    assert "reconfirmed" not in out["rules"] and rules.RECONFIRM_BRANCH_UNREADABLE in _md(summary)


def test_the_log_failing_is_the_branch_note(tmp_path, monkeypatch, capsys, git_env):
    summary = _first_pr(tmp_path, monkeypatch, git_env, [_ledger(f"BR-SPK-1201-1@{A}")])
    real = strict._out
    monkeypatch.setattr(strict, "_out", lambda repo, *a: None if a and a[0] == "log" else real(repo, *a))
    assert _run(capsys, summary)[1]["rules"]["reconfirmed_note"] == rules.RECONFIRM_BRANCH_UNREADABLE


def test_no_rule_text_in_output(tmp_path, monkeypatch, capsys, git_env):
    body = f"- **Spec dir:** {SECRET}-spec\nThe rule text {SECRET} is here\n"
    summary = _setup(tmp_path, monkeypatch, git_env, _ledger(f"BR-SPK-1201-1@{A}", extra=body), _ledger(f"BR-SPK-1201-1@{C}", extra=body))
    rc, out = _run(capsys, summary)
    cap = capsys.readouterr()
    assert out["rules"]["reconfirmed"] == [{"id": "BR-SPK-1201-1", "old": A, "new": C}]
    assert SECRET not in json.dumps(out) + _md(summary) + cap.out + cap.err


def test_the_pure_comparison():
    old = {"BR-SPK-1201-1": A, "BR-SPK-1201-2": B, "BR-SPK-9-1": C}
    assert rules.reconfirmed(old, {"BR-SPK-1201-1": A, "BR-SPK-1201-2": C, "BR-SPK-1201-3": B}) == [
        {"id": "BR-SPK-9-1", "old": C, "new": None}, {"id": "BR-SPK-1201-2", "old": B, "new": C}]
    assert rules.reconfirmed({}, {"BR-SPK-1201-1": A}) == [] and rules.reconfirmed(old, old) == []
    many = {f"BR-SPK-5-{n}": A for n in range(1, 31)}
    text = rules.reconfirmed_text(rules.reconfirmed(many, {}))
    assert text.count(" -> none") == 20 and text.endswith("and 10 more")


# ---- strict check: a warning, never a finding ----
def _tgt(root, ledger, ref="refs/remotes/origin/trunk"):
    _target(root, None, git(root, "rev-parse", "HEAD"), ref=ref, files={LED: ledger})


def _check(cfg, root, phase, target="origin/trunk"):
    """strict.check against `target` with only the `rules reconfirmed` warnings kept (the other notes are not this test's)."""
    res = strict.check(cfg, KEY, phase, repo=str(root), target=target)
    warnings = [w for w in res.get("warnings", []) if w.startswith("rules reconfirmed:")]
    return {**{k: v for k, v in res.items() if k != "warnings"}, **({"warnings": warnings} if warnings else {})}


@pytest.mark.parametrize("phase", ["pr", "done"])
def test_strict_check_warns_with_the_same_ids_and_changes_nothing_else(tmp_path, git_env, phase):
    root, cfg = _rstory(tmp_path, git_env)
    base = _pr(root, cfg, phase)
    assert base == {"phase": phase, "mode": "strict", "ok": True, "findings": [], "overrides_used": []}
    head = LEDGER(root).read_text(encoding="utf-8")
    _tgt(root, head.replace(RREV, f"BR-SPK-1201-1@{A}, BR-SPK-1201-2@{B}"))  # the target recorded other hashes
    res = _check(cfg, root, phase)
    assert {k: v for k, v in res.items() if k != "warnings"} == base  # a warning: ok, findings and overrides are unchanged
    [w] = res["warnings"]
    assert f"BR-SPK-1201-1 ({A} -> " in w and f"BR-SPK-1201-2 ({B} -> " in w and w.endswith(rules.RECONFIRM_NOTE)
    _tgt(root, head)  # the same record: nothing
    assert _check(cfg, root, phase) == base
    _tgt(root, head.replace(RREV, f"BR-SPK-1201-1@{A}"))  # BR-SPK-1201-2 is new at the head
    assert "BR-SPK-1201-2" not in " ".join(_check(cfg, root, phase).get("warnings", []))


def test_strict_check_first_pr_history(tmp_path, git_env):
    root, cfg = _rstory(tmp_path, git_env)
    first = git(root, "rev-list", "--max-parents=0", "HEAD")
    assert subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{first}:{LED}"], capture_output=True).returncode != 0  # no ledger at the base
    git(root, "update-ref", "refs/remotes/origin/trunk", first)
    assert _check(cfg, root, "pr")["warnings"] == [rules.RECONFIRM_FIRST_PR]  # the ledger came in one commit: nothing to compare
    head = LEDGER(root).read_text(encoding="utf-8")
    LEDGER(root).write_text(head.replace(RREV, f"BR-SPK-1201-1@{A}, BR-SPK-1201-2@{B}"), encoding="utf-8")
    commit(root, "SPK-1001 re-record")
    [w] = _check(cfg, root, "pr")["warnings"]
    assert w.startswith("rules reconfirmed: BR-SPK-1201-1 (") and f"-> {A})" in w and f"-> {B})" in w and w.endswith(rules.RECONFIRM_NOTE)
    git(root, "update-ref", "refs/remotes/origin/trunk", "HEAD")  # a target at HEAD that has the ledger: nothing differs
    assert "warnings" not in _check(cfg, root, "pr")


def test_strict_check_unreadable_target_ledger_warns_never_finds(tmp_path, git_env):
    root, cfg = _rstory(tmp_path, git_env)
    _tgt(root, ("120000", "elsewhere"))
    res = _check(cfg, root, "pr")
    assert res["ok"] is True and res["findings"] == [] and res["warnings"] == [rules.RECONFIRM_UNREADABLE]


def test_strict_check_rules_off_or_no_target_is_the_baseline(tmp_path, git_env, monkeypatch):
    root, cfg = _rstory(tmp_path, git_env, enabled=False)
    _tgt(root, LEDGER(root).read_text(encoding="utf-8").replace(RREV, f"BR-SPK-1201-1@{A}"))
    monkeypatch.setattr(strict, "reconfirmed_vs_base", lambda *a, **k: pytest.fail("not read"))
    assert _check(cfg, root, "pr") == {"phase": "pr", "mode": "strict", "ok": True, "findings": [], "overrides_used": []}
    root2, cfg2 = _rstory(tmp_path, git_env, name="t")
    assert strict.check(cfg2, KEY, "pr", repo=str(root2)) == {"phase": "pr", "mode": "strict", "ok": True, "findings": [], "overrides_used": []}


def test_strict_check_no_ledger_anywhere_is_the_first_pr_note(tmp_path, git_env):
    root, cfg = _rstory(tmp_path, git_env)
    _tgt(root, None)  # a target past HEAD without the ledger
    assert _check(cfg, root, "pr")["warnings"] == [rules.RECONFIRM_FIRST_PR]
