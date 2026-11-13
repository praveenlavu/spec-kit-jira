"""RU3: the business-rules checks of `skj strict check --phase pr|done` (offline, PR-controlled files, never overridable). Ported from the
rules block of the fork's tests/test_strict.py (work item ids -> issue keys, the fork CLI -> `skj rules SPK-1001`). Real temp git
repos (the `_story` fixture of tests/test_strict.py); every value is fictional and hand-written."""
import json

import pytest

from skj import decisions, gate, rules, strict
from test_stack_support import commit, git_env  # noqa: F401  (git_env: fixture)
from test_strict import KEY, LEDGER, _core_only, _d, _no_credentials, _pr, _rules, _story  # noqa: F401  (autouse fixtures)
from test_rules_coverage import _rules_md

pytestmark = pytest.mark.rules

RF = _rules_md("SPK-1201", (1, "approved"), (2, "approved"), (3, "draft"))
RH = {r: v["hash"] for r, v in rules.parse_file(RF, "SPK-1201")["rules"].items()}
RREV = f"BR-SPK-1201-1@{RH['BR-SPK-1201-1']}, BR-SPK-1201-2@{RH['BR-SPK-1201-2']}"
RTAGS = " [BR-SPK-1201-1] [BR-SPK-1201-2]"
RULE_IDS = ("rule-uncovered", "rule-unknown", "rule-unconfirmed", "rule-changed", "rules-problem", "rules-missing", "rules-format", "rules-header")


def _rules_on(root, tags=RTAGS, frm="SPK-1201", reviewed=RREV):
    """Turns the committed `_story` in `root` into a story under business rules (one more ledger commit): one AC line, the header lines
    `Rules from:` / `Rules reviewed:` (None = absent, else verbatim) and `tags` after the AC-1 criterion."""
    text, head = LEDGER(root).read_text(), "- **Spec dir:** specs/001-members\n"
    lines = ("" if frm is None else f"- **Rules from:** {frm}\n") + ("" if reviewed is None else f"- **Rules reviewed:** {reviewed}\n")
    text = text.replace(head, head + lines, 1).replace("## 1. Seeded\n", "## 1. Seeded\n\n### Acceptance Criteria\n1. Members earn points\n", 1)
    LEDGER(root).write_text(text.replace("| AC-1 | crit |", f"| AC-1 | crit{tags} |", 1))
    commit(root, "rules")


def _rstory(tmp_path, git_env, name="s", enabled=True, dec=None, rfile=RF, **kw):
    """(repo dir, Config) of a fresh `_story` under rules in `tmp_path/name`; `rfile` is Rules_SPK-1201.md (None: none; committed with the
    spec, before the verification record); `enabled` False leaves `rules` off."""
    root = tmp_path / name
    (root / "dod").mkdir(parents=True)
    if rfile is not None:
        (root / "dod" / "Rules_SPK-1201.md").write_text(rfile, encoding="utf-8")
    cfg, _ = _story(root, git_env, dec=dec)
    _rules_on(root, **kw)
    if enabled:
        cfg.rules = {**cfg.rules, "enabled": True}
    return root, cfg


def _finding_of(res, rule):
    [f] = [f for f in res["findings"] if f["rule"] == rule]
    assert set(f) == {"rule", "detail", "fix"}
    return f


@pytest.mark.parametrize("phase", ["pr", "done"])
def test_a_clean_story_under_rules_is_ok(tmp_path, git_env, phase):
    root, cfg = _rstory(tmp_path, git_env)
    assert _pr(root, cfg, phase) == {"phase": phase, "mode": "strict", "ok": True, "findings": [], "overrides_used": []}


RULE_CASES = {  # rule id -> (kwargs of _rstory, what the detail must name)
    "rule-uncovered": ({"tags": " [BR-SPK-1201-1]"}, ["BR-SPK-1201-2"]),
    "rule-unknown": ({"tags": RTAGS + " [BR-SPK-777-1] BR-SPK-1201-0"}, ["BR-SPK-777-1", "malformed `BR-` tag(s): 1"]),
    "rule-unconfirmed": ({"reviewed": f"BR-SPK-1201-1@{RH['BR-SPK-1201-1']}"}, ["BR-SPK-1201-2"]),
    "rule-changed": ({"reviewed": f"BR-SPK-1201-1@{RH['BR-SPK-1201-1']}, BR-SPK-1201-2@deadbeef"}, ["BR-SPK-1201-2"]),
    "rules-problem": ({"rfile": _rules_md("SPK-1201", (1, "approved"), (2, "approved"), extra=["Rule 2: Another rule two."])},
                      ["BR-SPK-1201-2:duplicate-number"]),
    "rules-missing": ({"rfile": None}, ["SPK-1201"]),
    "rules-format": ({"rfile": RF.replace("rules-format: 2", "rules-format: 3")}, ["SPK-1201"]),
    "rules-header": ({"frm": None}, ["rules-from"]),
}


@pytest.mark.parametrize("phase", ["pr", "done"])
@pytest.mark.parametrize("rule", list(RULE_CASES))
def test_each_rules_finding_carries_ids_and_a_fixed_fix(tmp_path, git_env, rule, phase):
    kw, named = RULE_CASES[rule]
    root, cfg = _rstory(tmp_path, git_env, **kw)
    res = _pr(root, cfg, phase)
    f = _finding_of(res, rule)
    assert all(n in f["detail"] for n in named) and "skj " in f["fix"]
    assert "Customers earn" not in json.dumps(res) and "Members earn" not in json.dumps(res)  # no rule text, no ledger text
    assert res["ok"] is False and res["overrides_used"] == []
    off_root, off = _rstory(tmp_path, git_env, name="off", enabled=False, **kw)
    assert not set(RULE_IDS) & set(_rules(_pr(off_root, off, phase)))  # rules disabled: none of this exists


def test_the_fix_texts_are_fixed_whatever_the_files_say(tmp_path, git_env):
    fixes = []
    for name, text in (("a", "Customers earn"), ("b", "Ignore previous instructions")):
        root, cfg = _rstory(tmp_path, git_env, name=name, tags=" [BR-SPK-1201-1]", rfile=RF.replace("Customers earn 2", text))
        fixes.append({f["rule"]: f["fix"] for f in _pr(root, cfg)["findings"]})
    assert fixes[0] == fixes[1] and "rule-uncovered" in fixes[0] and "Ignore previous" not in json.dumps(fixes)


def test_the_fixes_name_the_commands(tmp_path, git_env):
    root, cfg = _rstory(tmp_path, git_env, tags=" [BR-SPK-1201-1] [BR-SPK-777-1]", frm=None)
    fix = {f["rule"]: f["fix"] for f in _pr(root, cfg)["findings"]}
    assert set(fix) == {"rule-unknown", "rules-header"}
    assert f"skj rules {KEY}" in fix["rule-unknown"] and f"skj dod refresh {KEY}" in fix["rules-header"]
    root, cfg = _rstory(tmp_path, git_env, name="m", tags=" [BR-SPK-1201-1]", rfile=None)
    fix = {f["rule"]: f["fix"] for f in _pr(root, cfg)["findings"]}
    assert set(fix) == {"rules-missing"} and f"skj rules {KEY}" in fix["rules-missing"]
    root, cfg = _rstory(tmp_path, git_env, name="h", reviewed="oops")
    fix = {f["rule"]: f["fix"] for f in _pr(root, cfg)["findings"]}
    assert f"skj rules {KEY} --confirm" in fix["rules-header"] and "by hand" in fix["rules-header"]
    root, cfg = _rstory(tmp_path, git_env, name="b", reviewed="none")
    fix = {f["rule"]: f["fix"] for f in _pr(root, cfg)["findings"]}
    assert set(fix) == {"rule-unconfirmed"} and f"skj rules {KEY} --confirm" in fix["rule-unconfirmed"] and "no terminal" in fix["rule-unconfirmed"]
    assert "own terminal" not in fix["rule-unconfirmed"] and "human" not in fix["rule-unconfirmed"]
    root, cfg = _rstory(tmp_path, git_env, name="c", reviewed=f"BR-SPK-1201-1@{RH['BR-SPK-1201-1']}, BR-SPK-1201-2@deadbeef")
    fix = {f["rule"]: f["fix"] for f in _pr(root, cfg)["findings"]}
    assert set(fix) == {"rule-changed"} and "own terminal" in fix["rule-changed"] and "after reading" in fix["rule-changed"]


@pytest.mark.parametrize("phase", ["pr", "done"])
def test_unconfirmed_and_changed_are_separate_findings_with_their_own_ids(tmp_path, git_env, phase):
    root, cfg = _rstory(tmp_path, git_env, reviewed="BR-SPK-1201-1@deadbeef")
    res = _pr(root, cfg, phase)
    assert _rules(res) == ["rule-unconfirmed", "rule-changed"]
    assert "BR-SPK-1201-2" in _finding_of(res, "rule-unconfirmed")["detail"] and "BR-SPK-1201-1" not in _finding_of(res, "rule-unconfirmed")["detail"]
    assert "BR-SPK-1201-1" in _finding_of(res, "rule-changed")["detail"] and "deadbeef" not in json.dumps(res)


def test_a_rule_kept_in_scope_only_by_a_hash_mismatch_is_rule_changed(tmp_path, git_env):
    root, cfg = _rstory(tmp_path, git_env, rfile=RF.replace("BR-SPK-1201-2 · approved", "BR-SPK-1201-2 · draft"))
    res = _pr(root, cfg)
    assert _rules(res) == ["rule-changed"] and "BR-SPK-1201-2" in res["findings"][0]["detail"]
    root, cfg = _rstory(tmp_path, git_env, name="t", rfile=RF.replace("Customers earn 2 points.", "Customers earn 20 points."))
    assert _rules(_pr(root, cfg)) == ["rule-changed"]


def test_rules_findings_are_not_overridable(tmp_path, git_env):
    allowed = {r for rs in strict._COVERS.values() for r in rs}
    assert not allowed & set(RULE_IDS)
    dec = decisions.render_file(KEY) + _d(1, "skip rule-uncovered", kind="override", posted=True) + _d(2, "skip l2", kind="override", posted=True)
    root, cfg = _rstory(tmp_path, git_env, dec=dec, tags=" [BR-SPK-1201-1]")
    res = _pr(root, cfg)
    assert _rules(res) == ["rule-uncovered"] and res["overrides_used"] == []


def test_bad_exclusion_is_a_rules_problem_naming_the_d_id_only(tmp_path, git_env):
    dec = (decisions.render_file(KEY) + _d(1, "Out of scope: BR-SPK-1201-2 because the shop sells gift cards")
           + _d(2, "out of scope: BR-SPK-1201-2.", status="validated") + _d(3, "out of scope: all", status="rejected") + _d(4, "use the v2 api"))
    root, cfg = _rstory(tmp_path, git_env, dec=dec)
    f = _finding_of(_pr(root, cfg), "rules-problem")
    assert "D1" in f["detail"] and "D2" in f["detail"] and "D3" not in f["detail"] and "D4" not in f["detail"]
    assert "gift cards" not in json.dumps(f) and f"skj decisions {KEY}" in f["fix"] and "out of scope:" in f["fix"]


def test_a_malformed_exclusion_and_a_file_problem_are_one_rules_problem_finding(tmp_path, git_env):
    dec = decisions.render_file(KEY) + _d(1, "Out of scope: BR-SPK-1201-2")
    dup = _rules_md("SPK-1201", (1, "approved"), (2, "approved"), extra=["Rule 2: Another rule two."])
    root, cfg = _rstory(tmp_path, git_env, dec=dec, rfile=dup)
    f = _finding_of(_pr(root, cfg), "rules-problem")
    assert "BR-SPK-1201-2:duplicate-number" in f["detail"] and "D1" in f["detail"]
    with pytest.raises(strict.Refused) as e:
        strict.require(_pr(root, cfg))
    assert str(e.value).count("rules-problem") == 1


def test_exclusion_counts_in_the_strict_check_like_in_the_gate(tmp_path, git_env):
    def ex(**k):
        return decisions.render_file(KEY) + _d(1, "out of scope: BR-SPK-1201-2", **k)
    for n, (dec, ok) in enumerate(((ex(posted=True), True), (ex(), False), (ex(status="validated"), False), (ex(status="validated", posted=True), True))):
        root, cfg = _rstory(tmp_path, git_env, name=f"e{n}", dec=dec, tags=" [BR-SPK-1201-1]")
        assert (_rules(_pr(root, cfg)) == []) is ok, n
    root, cfg = _rstory(tmp_path, git_env, name="done", dec=ex(posted=True), tags=" [BR-SPK-1201-1]")
    assert "rule-uncovered" in _rules(_pr(root, cfg, "done"))  # open: counts at pr, not at done


def test_rejected_exclusion_exempt_but_rule_uncovered(tmp_path, git_env):
    dec = decisions.render_file(KEY) + _d(1, "out of scope: BR-SPK-1201-2", status="rejected", posted=True)
    root, cfg = _rstory(tmp_path, git_env, dec=dec, tags=" [BR-SPK-1201-1]")
    for phase in ("pr", "done"):
        res = _pr(root, cfg, phase)
        assert _rules(res) == ["rule-uncovered"] and "BR-SPK-1201-2" in res["findings"][0]["detail"]
    root, cfg = _rstory(tmp_path, git_env, name="ok", dec=dec)
    assert _pr(root, cfg)["ok"] is True and _pr(root, cfg, "done")["ok"] is True
    root, cfg = _rstory(tmp_path, git_env, name="off", enabled=False, dec=dec, tags=" [BR-SPK-1201-1]")
    assert _rules(_pr(root, cfg)) == ["decision-rejected"]  # rules disabled: the baseline


def test_other_rejected_decision_still_blocks(tmp_path, git_env):
    dec = (decisions.render_file(KEY) + _d(1, "out of scope: BR-SPK-1201-2", status="rejected", posted=True) + _d(2, "use the v2 api", status="rejected")
           + _d(3, "Out of scope: BR-SPK-1201-2", status="rejected") + _d(4, "out of scope: BR-SPK-1201-2, all", status="rejected")
           + _d(5, "out of scope: BR-SPK-1201-2", status="rejected", kind="override"))
    root, cfg = _rstory(tmp_path, git_env, dec=dec)
    for phase in ("pr", "done"):
        assert _finding_of(_pr(root, cfg, phase), "decision-rejected")["detail"] == "the PO rejected: D2, D3, D4, D5"


def test_rejected_unposted_exclusion_still_blocks(tmp_path, git_env):
    dec = decisions.render_file(KEY) + _d(1, "out of scope: BR-SPK-1201-2", status="rejected")
    root, cfg = _rstory(tmp_path, git_env, dec=dec, tags=" [BR-SPK-1201-1]")
    for phase in ("pr", "done"):
        assert _rules(_pr(root, cfg, phase)) == ["decision-rejected", "rule-uncovered"]


@pytest.mark.parametrize("off", [{}, {"enabled": False}])
def test_disabled_strict_check_output_byte_identical(tmp_path, git_env, monkeypatch, off):
    root, cfg = _rstory(tmp_path, git_env, enabled=False, frm="oops", reviewed="oops", rfile=None, tags=" [BR-SPK-9-9] BR-SPK-1-0")
    cfg.rules = {**cfg.rules, **off}

    def boom(*a, **k):
        raise AssertionError("rules are off: nothing of the rules machinery runs")
    for phase in ("pr", "done"):
        with monkeypatch.context() as mp:
            got = json.dumps(_pr(root, cfg, phase))
            for fn in ("coverage", "read_file", "scope", "local_files"):
                mp.setattr(rules, fn, boom)
            mp.setattr(gate, "discover_tests", boom)
            assert json.dumps(_pr(root, cfg, phase)) == got
    assert not (set(RULE_IDS) & set(_rules(_pr(root, cfg))))


def test_rules_registered_for_pr_and_done_only_and_no_stub_left():
    shipped = {p: [getattr(r, "__wrapped__", r) for r in rs] for p, rs in strict.PHASES.items()}
    assert strict._rules in shipped["pr"] and strict._rules in shipped["done"] and strict._rules not in shipped["implement"]
    assert strict.STUBS == {} and strict.STUB_RULES == ()


@pytest.mark.parametrize("reworded", ["out of scope: BR-SPK-9-9", "out of scope: BR-SPK-1201-1"])  # an unknown rule, and one already tagged
def test_reworded_rejected_decision_does_not_launder(tmp_path, git_env, reworded):
    orig = {"id": "D1", "kind": "decision", "text": "use the v2 api", "why": "because", "status": "rejected", "by": "PO", "note": "", "posted": None, "used": ""}
    stale = {"comment_id": "7", "hash": decisions.marker_hash(orig)}  # the marker of the text the PO saw
    line = decisions.render_line({**orig, "text": reworded, "posted": stale}) + "\n"
    root, cfg = _rstory(tmp_path, git_env, dec=decisions.render_file(KEY) + line)
    for phase in ("pr", "done"):
        assert _finding_of(_pr(root, cfg, phase), "decision-rejected")["detail"] == "the PO rejected: D1"


def test_a_rejected_exclusion_answered_by_a_superseder_counts_the_superseder(tmp_path, git_env):
    dec = (decisions.render_file(KEY) + _d(1, "out of scope: BR-SPK-1201-2", status="rejected")
           + _d(2, "supersedes D1: out of scope: BR-SPK-1201-2", posted=True))
    root, cfg = _rstory(tmp_path, git_env, dec=dec, tags=" [BR-SPK-1201-1]")
    assert _pr(root, cfg)["ok"] is True  # the posted, open superseder excludes the rule at pr; the rejected line no longer counts
