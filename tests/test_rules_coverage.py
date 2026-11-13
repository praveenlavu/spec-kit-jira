"""RU3: the local `rule_coverage` check (`rules.coverage`, from committed files) and how `skj gate` reports it. Ported from the rules block of
the fork's tests/test_gate.py (work item ids -> issue keys, HTML -> ADF, the fork CLI's refresh -> `skj dod refresh <KEY>`). The JSON
key `rules` exists only when rules are enabled. All data is fictional and hand-written (LOY-1201 an Epic, LOY-1200 its parent)."""
import shutil

import pytest

from skj import decisions, dod, gate, rules
from skj.config import Config
from skj.decisions import render_file
from skj.dod import ac_section, render
from skj.gate import render_summary, run_gate

pytestmark = pytest.mark.rules

SITE = "example-site.atlassian.net"
KEY = "LOY-7"
APEX = "@IsTest private class FooTest { @IsTest static void testX(){ Assert.isTrue(true); } }"
SOURCES = {"FooTest.cls": APEX}
BASE_KEYS = {"traceability", "assertion_gaps", "assertion_notes", "drift", "missing_evidence", "open_questions",
             "commits_without_story", "smart_commit_commands", "blocked_by", "blocking"}
_ROW0 = "| AC-1 | _(what it must do)_ | _(test IDs, …)_ | _(commit / PR)_ | _(red→green)_ |"
_D_REJ = "- [x] D3: Skip audit log → rejected · by: PO · note: not acceptable"
_D_OPEN10 = "- [ ] D10: Retry twice → _(open)_ · kind: assumption · why: unspecified"


def para(s):
    return {"type": "paragraph", "content": [{"type": "text", "text": s}]}


def _story():
    items = [{"type": "listItem", "content": [para(x)]} for x in ("earn points", "gift cards earn nothing", "show the balance")]
    desc = {"type": "doc", "version": 1, "content": [{"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Acceptance criteria"}]},
                                                    {"type": "bulletList", "content": items}]}
    return {"key": KEY, "id": "10007", "fields": {"summary": "T", "issuetype": {"name": "Story"}, "status": {"name": "To Do"}, "description": desc}}


def _rules_md(key, *specs, extra=()):
    """A rules file as `skj rules` writes it: (n, status) per rule, every label present (no warnings)."""
    lines = ["Business rules"]
    for n, s in specs:
        lines += ["Rule %d: Customers earn %d points." % (n, n), "Why: w", "Example: e", "Owner: Loyalty product owner", "Status: " + s]
    doc = {"type": "doc", "version": 1, "content": [para(x) for x in lines + list(extra)]}
    plain, probs = rules.normalise(doc)
    return rules.render_file({"key": key, "fields": {"summary": "Points", "issuetype": {"name": "Epic"}}},
                             rules.parse(plain, key, probs), SITE, "2026-11-03", "0.1.0")


F1201 = _rules_md("LOY-1201", (1, "approved"), (2, "approved"), (3, "draft"), (4, "retired"))
F1200 = _rules_md("LOY-1200", (1, "approved"))
H = {r: v["hash"] for md, k in ((F1201, "LOY-1201"), (F1200, "LOY-1200")) for r, v in rules.parse_file(md, k)["rules"].items()}
ALL = f"BR-LOY-1200-1@{H['BR-LOY-1200-1']}, BR-LOY-1201-1@{H['BR-LOY-1201-1']}, BR-LOY-1201-2@{H['BR-LOY-1201-2']}"
CLEAN_REV = f"BR-LOY-1201-1@{H['BR-LOY-1201-1']}, BR-LOY-1201-2@{H['BR-LOY-1201-2']}"


def _files(**md):
    """`f1201=<text>` -> {"LOY-1201": parse_file(text)}."""
    return {"LOY-" + k[1:]: rules.parse_file(v, "LOY-" + k[1:]) for k, v in md.items() if v is not None}


def _gl(*rows, frm=("LOY-1201",), reviewed=""):
    """A ledger: three AC lines, the given section 3 rows, the header lines `Rules from:` (None: absent) and `Rules reviewed:`."""
    s = _story()
    md = render(s, ac_section(s["fields"]["description"], "Acceptance criteria"), SITE, synced="2026-07-14",
                rules_from=None if frm is None else list(frm), rules_reviewed=reviewed)
    assert _ROW0 in md
    return md.replace(_ROW0, "\n".join(rows))


ROWS = ("| AC-1 | earn points [BR-LOY-1201-1] | FooTest.testX | abc | green |",
        "| AC-2 | gift cards earn nothing [BR-LOY-1201-2] | FooTest.testX | | green |",
        "| AC-3 | show the balance | FooTest.testX | | green |")


def _cov(ledger, files=None, decisions_md="", phase="pr", sources=SOURCES):
    return rules.coverage(ledger, sources, _files(f1201=F1201, f1200=F1200) if files is None else files,
                          dod.ledger_rules_from(ledger), dod.ledger_rules_reviewed(ledger), decisions.parse_decisions(decisions_md), phase)


def _clean():
    return _gl(*ROWS, reviewed=CLEAN_REV)


def test_coverage_keys_and_a_clean_story():
    c = _cov(_clean())
    assert list(c) == ["in_scope", "covered", "excluded", "uncovered", "unknown", "unconfirmed", "changed", "problems", "missing", "format", "header"]
    assert c["in_scope"] == c["covered"] == ["BR-LOY-1201-1", "BR-LOY-1201-2"]  # the draft and the retired rule are never in scope
    assert not any(c[k] for k in ("excluded", "uncovered", "unknown", "unconfirmed", "changed", "problems", "missing", "format", "header"))


def test_uncovered_rule_untagged_or_tagged_on_an_ac_with_no_present_test():
    c = _cov(_gl(ROWS[0], "| AC-2 | gift cards earn nothing | FooTest.testX | | |", ROWS[2], reviewed=CLEAN_REV))
    assert c["covered"] == ["BR-LOY-1201-1"] and c["uncovered"] == ["BR-LOY-1201-2"]
    gone = _cov(_gl(ROWS[0], "| AC-2 | gift cards [BR-LOY-1201-2] | Gone.test | | |", ROWS[2], reviewed=CLEAN_REV))
    assert gone["uncovered"] == ["BR-LOY-1201-2"]
    empty = _cov(_gl(ROWS[0], "| AC-2 | gift cards [BR-LOY-1201-2] |  | | |", ROWS[2]))
    assert "BR-LOY-1201-2" in empty["uncovered"]


def test_extra_ac_row_does_not_cover():
    c = _cov(_gl(ROWS[0], "| AC-99 | extra [BR-LOY-1201-2] | FooTest.testX | | |", ROWS[2], "| AC-0 | zero [BR-LOY-1201-2] | FooTest.testX | | |",
                 "| AC-4 | beyond the three [BR-LOY-1201-2] | FooTest.testX | | |", "| AC-01 | not AC-1 [BR-LOY-1201-2] | FooTest.testX | | |"))
    assert c["covered"] == ["BR-LOY-1201-1"] and c["uncovered"] == ["BR-LOY-1201-2"]
    assert not c["unknown"]  # a known id cited on a row that does not count is not an unknown id


def test_row_with_absent_test_does_not_cover_even_if_other_row_mapped():
    c = _cov(_gl(ROWS[0], "| AC-2 | y | FooTest.testX | | |", "| AC-3 | z [BR-LOY-1201-2] | Gone.test | | |"))
    assert c["uncovered"] == ["BR-LOY-1201-2"]
    dup = _cov(_gl("| AC-1 | a | FooTest.testX | | |", "| AC-1 | a [BR-LOY-1201-1] [BR-LOY-1201-2] | Gone.test | | |", "| AC-2 | b | FooTest.testX | | |"))
    assert dup["uncovered"] == ["BR-LOY-1201-1", "BR-LOY-1201-2"]  # any absent duplicate row disqualifies
    ok = _cov(_gl("| AC-1 | a [BR-LOY-1201-1] | Gone.test, FooTest.testX | | |", "| AC-2 | b [BR-LOY-1201-2] | FooTest.testX | | |"))
    assert ok["covered"] == ["BR-LOY-1201-1", "BR-LOY-1201-2"]  # one of several tests present is enough for a row


def test_rule_in_a_fence_or_comment_row_is_not_cited():
    led = _gl(ROWS[0], "```", "| AC-2 | hidden [BR-LOY-1201-2] | FooTest.testX | | |", "```", "<!--", "| AC-3 | c [BR-LOY-1201-2] | FooTest.testX | | |", "-->")
    assert _cov(led)["uncovered"] == ["BR-LOY-1201-2"]


def test_unknown_ids_are_ids_or_a_question_mark_never_the_token_text():
    c = _cov(_gl(ROWS[0], "| AC-2 | b [BR-LOY-1201-2] [BR-LOY-1201-9] [BR-LOY-777-1] [BR-LOY-1201-0] xBR-LOY-1-1y | FooTest.testX | | |", ROWS[2]))
    assert c["unknown"] == ["BR-LOY-777-1", "BR-LOY-1201-9", "?", "?"]  # ids by (issue, number), then one `?` per malformed token
    assert "BR-LOY-1201-0" not in repr(c) and "xBR" not in repr(c)
    d = _cov(_gl(ROWS[0], ROWS[1], "| AC-3 | c [BR-LOY-1201-3] [BR-LOY-1201-4] | FooTest.testX | | |"))
    assert d["unknown"] == [] and d["in_scope"] == ["BR-LOY-1201-1", "BR-LOY-1201-2"]  # citing a draft or retired rule is neither


def test_first_pull_is_all_unconfirmed_none_changed():
    for led in (_gl(*ROWS), _gl(*ROWS, reviewed="none")):
        c = _cov(led)
        assert c["unconfirmed"] == ["BR-LOY-1201-1", "BR-LOY-1201-2"] and c["changed"] == []


def test_a_recorded_different_hash_is_changed_only():
    c = _cov(_gl(*ROWS, reviewed=f"BR-LOY-1201-1@{H['BR-LOY-1201-1']}, BR-LOY-1201-2@deadbeef"))
    assert c["changed"] == ["BR-LOY-1201-2"] and c["unconfirmed"] == []


def test_a_rule_with_no_recorded_hash_is_unconfirmed_while_the_rest_is_confirmed():
    c = _cov(_gl(*ROWS, reviewed=f"BR-LOY-1201-1@{H['BR-LOY-1201-1']}"))
    assert c["unconfirmed"] == ["BR-LOY-1201-2"] and c["changed"] == []


def test_a_mix_of_both_keeps_the_two_lists_separate():
    c = _cov(_gl(*ROWS, frm=("LOY-1201", "LOY-1200"), reviewed="BR-LOY-1201-1@deadbeef"))
    assert c["changed"] == ["BR-LOY-1201-1"] and c["unconfirmed"] == ["BR-LOY-1200-1", "BR-LOY-1201-2"]


def test_an_unrecorded_rule_whose_text_no_longer_matches_its_hash_is_changed_not_unconfirmed():
    edited = F1201.replace("- Rule: Customers earn 1 points.", "- Rule: Customers earn nothing.")
    assert edited != F1201
    c = _cov(_gl(*ROWS, reviewed=f"BR-LOY-1201-2@{H['BR-LOY-1201-2']}"), files=_files(f1201=edited))
    assert c["changed"] == ["BR-LOY-1201-1"] and c["unconfirmed"] == []
    down = F1201.replace(f"### BR-LOY-1201-2 · approved · {H['BR-LOY-1201-2']}", f"### BR-LOY-1201-2 · draft · {H['BR-LOY-1201-2']}")
    d = _cov(_gl(*ROWS), files=_files(f1201=down))
    assert d["changed"] == ["BR-LOY-1201-2"] and d["unconfirmed"] == ["BR-LOY-1201-1"]


def test_a_malformed_record_makes_nothing_free_to_confirm():
    bad = _gl(*ROWS, reviewed=ALL).replace("@" + H["BR-LOY-1201-1"], "@XYZ")
    c = _cov(bad)
    assert c["header"] == ["rules-reviewed"] and c["unconfirmed"] == [] and c["changed"] == ["BR-LOY-1201-1", "BR-LOY-1201-2"]


def test_changed_summary_words_follow_the_cause():
    def line(led):
        return next(x for x in render_summary(_rg(led, block=["rule_coverage"])).splitlines() if x.startswith("- Business rules:"))
    mismatch = line(_gl(*ROWS, reviewed=f"BR-LOY-1201-1@deadbeef, BR-LOY-1201-2@{H['BR-LOY-1201-2']}"))
    assert "changed since confirmed" in mismatch and "re-pull with `skj rules <story key>` if the file was edited by hand" in mismatch
    assert "`BR-LOY-1201-1`" in mismatch and "malformed" not in mismatch and "BR-LOY-1201-2" not in mismatch
    bad = line(_gl(*ROWS, reviewed=ALL).replace("@" + H["BR-LOY-1201-1"], "@XYZ"))
    assert "changed since confirmed" not in bad and "cannot be judged while the `Rules reviewed:` line is malformed" in bad
    assert "`BR-LOY-1201-1`" in bad and "`BR-LOY-1201-2`" in bad and "XYZ" not in bad


def _excl(*ids, status="open", posted=True, n=3):
    d = {"id": f"D{n}", "kind": "decision", "text": "out of scope: " + ", ".join(ids), "why": "the Loyalty product owner agreed", "status": status,
         "by": "" if status == "open" else "Loyalty product owner", "note": "", "posted": None, "used": ""}
    if posted:
        d["posted"] = {"comment_id": "7", "hash": decisions.marker_hash(d)}
    return decisions.render_line(d)


def _dfile(*lines):
    return render_file(KEY) + "\n".join(lines) + "\n"


def test_an_excluded_or_draft_rule_without_a_record_is_neither_list():
    c = _cov(_gl(*ROWS, reviewed=f"BR-LOY-1201-1@{H['BR-LOY-1201-1']}"), decisions_md=_dfile(_excl("BR-LOY-1201-2")))
    assert c["unconfirmed"] == [] and c["changed"] == []
    assert "BR-LOY-1201-3" not in _cov(_gl(*ROWS))["unconfirmed"]


def test_changed_judges_every_recorded_id_that_is_still_approved_in_scope_or_not():
    ex = _dfile(_excl("BR-LOY-1201-2"))
    c = _cov(_gl(*ROWS, reviewed=f"BR-LOY-1201-1@{H['BR-LOY-1201-1']}, BR-LOY-1201-2@deadbeef"), decisions_md=ex)
    assert c["excluded"] == ["BR-LOY-1201-2"] and "BR-LOY-1201-2" not in c["in_scope"] and c["changed"] == ["BR-LOY-1201-2"]
    assert _cov(_gl(*ROWS, reviewed=CLEAN_REV), decisions_md=ex)["changed"] == []
    drafted = _cov(_gl(*ROWS, reviewed=f"BR-LOY-1201-1@{H['BR-LOY-1201-1']}, BR-LOY-1201-3@deadbeef"))
    assert "BR-LOY-1201-3" not in drafted["changed"]  # a recorded id whose rule is a draft now is the live drift check's


def test_exclusions_count_only_when_posted_and_not_rejected_and_phase_fits():
    led = _gl(ROWS[0], ROWS[2], reviewed=CLEAN_REV)
    posted = _cov(led, decisions_md=_dfile(_excl("BR-LOY-1201-2")))
    assert posted["excluded"] == ["BR-LOY-1201-2"] and posted["uncovered"] == [] and posted["in_scope"] == ["BR-LOY-1201-1"]
    assert _cov(led, decisions_md=_dfile(_excl("BR-LOY-1201-2", posted=False)))["uncovered"] == ["BR-LOY-1201-2"]
    assert _cov(led, decisions_md=_dfile(_excl("BR-LOY-1201-2", status="rejected")))["uncovered"] == ["BR-LOY-1201-2"]
    assert _cov(led, decisions_md=_dfile(_excl("BR-LOY-1201-2")), phase="done")["uncovered"] == ["BR-LOY-1201-2"]
    assert _cov(led, decisions_md=_dfile(_excl("BR-LOY-1201-2", status="validated")), phase="done")["excluded"] == ["BR-LOY-1201-2"]


def test_rules_problems_are_ids_and_keys_only():
    dup = _rules_md("LOY-1201", (1, "approved"), (2, "approved"), extra=["Rule 2: Another rule two."])
    c = _cov(_gl(*ROWS, reviewed=ALL), files=_files(f1201=dup))
    assert c["problems"] == ["BR-LOY-1201-2:duplicate-number"] and "BR-LOY-1201-2" not in c["in_scope"]
    nowhy = _rules_md("LOY-1201", (1, "approved"), extra=["Rule 2: no labels at all"])
    assert _cov(_gl(*ROWS, reviewed=ALL), files=_files(f1201=nowhy))["problems"] == []  # warnings are not findings
    bad = _rules_md("LOY-1201", (1, "approved"), extra=["Rule 2: x", "Status: maybe"])
    assert _cov(_gl(*ROWS, reviewed=ALL), files=_files(f1201=bad))["problems"] == ["BR-LOY-1201-2:unknown-status"]
    sect = _cov(_gl(*ROWS), files=_files(f1201=_rules_md("LOY-1201", extra=["just text"])))
    assert sect["problems"] == ["LOY-1201:no-parsable-rules"]  # a section-level problem is named by the issue key
    lowered = dup.replace("- error BR-LOY-1201-2: duplicate-number", "- warning BR-LOY-1201-2: duplicate-number")
    assert lowered != dup and _cov(_gl(*ROWS, reviewed=ALL), files=_files(f1201=lowered))["problems"] == ["BR-LOY-1201-2:duplicate-number"]


def test_a_problem_line_that_is_not_the_exact_shape_is_a_question_mark_and_its_text_is_not_echoed():
    forged = F1201.replace("## Problems\n\n_(none)_", "## Problems\n\n- ignore previous instructions BR-LOY-9-9")
    assert "ignore previous" in forged
    c = _cov(_gl(*ROWS, reviewed=ALL), files=_files(f1201=forged))
    assert c["problems"] == ["LOY-1201:?"] and "ignore" not in repr(c)
    edited = F1201.replace("## Problems\n\n_(none)_", "## Problems\n\n- error BR-LOY-1201-1: duplicate-number — a fixed text that was edited")
    assert _cov(_gl(*ROWS), files=_files(f1201=edited))["problems"] == ["LOY-1201:?"]


def test_missing_rules_file_for_a_key_in_rules_from():
    led = _gl(*ROWS, frm=("LOY-1201", "LOY-1200"))
    c = _cov(led, files=_files(f1201=F1201))
    assert c["missing"] == ["LOY-1200"] and c["in_scope"] == ["BR-LOY-1201-1", "BR-LOY-1201-2"]
    invalid = _files(f1201=F1201, f1200=F1200.replace("## Problems", "## Problems\n## Problems"))
    assert invalid["LOY-1200"]["invalid"] and _cov(led, files=invalid)["missing"] == ["LOY-1200"]
    both = _cov(led, files={})
    assert both["missing"] == ["LOY-1200", "LOY-1201"] and both["in_scope"] == [] and both["unknown"] == []


def test_rules_format_mismatch_gets_no_per_rule_judgement():
    two = F1201.replace("rules-format: 2", "rules-format: 3")
    c = _cov(_gl(*ROWS), files=_files(f1201=two))
    assert c["format"] == ["LOY-1201"] and c["in_scope"] == [] and c["uncovered"] == [] and c["changed"] == [] and c["unknown"] == []
    nohdr = F1201.replace("rules-format: 2 · ", "")
    assert _cov(_gl(*ROWS), files=_files(f1201=nohdr))["format"] == ["LOY-1201"]  # a malformed header line (format None) is a mismatch too


def test_header_findings():
    assert _cov(_gl(*ROWS, frm=None))["header"] == ["rules-from"] and _cov(_gl(*ROWS, frm=None))["in_scope"] == []
    bad = _gl(*ROWS).replace("- **Rules from:** LOY-1201", "- **Rules from:** LOY-1201,LOY-1200")
    assert dod.ledger_rules_from(bad)["valid"] is False and _cov(bad)["header"] == ["rules-from"]
    assert _cov(_gl(*ROWS, frm=()))["header"] == []  # `none` is valid: the story has no ancestors with rules
    assert _cov(_gl(*ROWS, reviewed=ALL).replace("@" + H["BR-LOY-1201-1"], "@XYZ"))["header"] == ["rules-reviewed"]
    both = _gl(*ROWS, frm=None, reviewed=ALL).replace("- **Rules reviewed:** ", "- **Rules reviewed:**  ")
    assert _cov(both)["header"] == ["rules-from", "rules-reviewed"]


def test_hostile_files_fail_closed_not_open():
    edited = F1201.replace("- Rule: Customers earn 1 points.", "- Rule: Customers earn nothing.")
    c = _cov(_gl(*ROWS, reviewed=ALL), files=_files(f1201=edited))
    assert c["changed"] == ["BR-LOY-1201-1"] and "BR-LOY-1201-1" in c["in_scope"]
    down = F1201.replace(f"### BR-LOY-1201-2 · approved · {H['BR-LOY-1201-2']}", f"### BR-LOY-1201-2 · draft · {H['BR-LOY-1201-2']}")
    d = _cov(_gl(*ROWS, reviewed=ALL), files=_files(f1201=down))
    assert d["changed"] == ["BR-LOY-1201-2"] and "BR-LOY-1201-2" in d["in_scope"]


# ---- run_gate: the JSON key, the finding list, the summary line ----
def _rg(ledger, files=None, block=(), decisions_md=None, **kw):
    return run_gate(ledger, SOURCES, {}, None, block=block, decisions_md=decisions_md,
                    rules_ctx={"files": _files(f1201=F1201, f1200=F1200) if files is None else files, **kw})


def test_rules_key_and_findings_only_with_a_rules_ctx():
    led = _clean()
    off = run_gate(led, SOURCES, {}, None)
    assert set(off) == BASE_KEYS and "rules" not in off
    on = _rg(led)
    assert set(on) == BASE_KEYS | {"rules"} and {k: on["rules"][k] for k in _cov(led)} == _cov(led) and on["blocking"] is False
    assert on["rules"]["covered"] == ["BR-LOY-1201-1", "BR-LOY-1201-2"]


def test_disabled_gate_asks_nothing_and_enabled_asks_both_rule_checks():
    led = _gl(*ROWS, frm=None)
    asked = []

    class _Block:
        def __contains__(self, key):
            asked.append(key)
            return False
    off = run_gate(led, SOURCES, {}, None, block=_Block())
    assert set(off) == BASE_KEYS and "rule_coverage" not in asked and "rule_drift" not in asked
    assert "Business rules" not in render_summary(off)
    run_gate(led, SOURCES, {}, None, block=_Block(), rules_ctx={"files": {}})
    assert "rule_coverage" in asked and "rule_drift" in asked


def test_findings_list_blocks_only_when_named_and_marks_each_class_with_a_question_mark():
    uncovered = _gl(ROWS[0], ROWS[2], reviewed=ALL)
    assert _rg(uncovered)["blocking"] is False
    r = _rg(uncovered, block=["rule_coverage"])
    assert r["blocked_by"] == ["rule_coverage"] and r["blocking"] is True
    for led, files in ((_gl(*ROWS, frm=None), None), (_gl(*ROWS), None), (_gl(*ROWS, frm=("LOY-1201", "LOY-1200"), reviewed=ALL), _files(f1201=F1201)),
                       (_gl(*ROWS, reviewed=ALL), _files(f1201=F1201.replace("rules-format: 2", "rules-format: 3")))):
        assert _rg(led, files, block=["rule_coverage"])["blocking"] is True
    assert _rg(_clean(), block=["rule_coverage"])["blocking"] is False


def test_blocked_rule_coverage_prints_its_own_line():
    md = render_summary(_rg(_gl(ROWS[0], ROWS[2], reviewed=ALL), block=["rule_coverage"]))
    blocking = md.partition("**Blocking")[2].partition("**Advisory")[0]
    assert "`BR-LOY-1201-2`" in blocking and "AC changed" not in blocking and "no drift" not in blocking
    adv = render_summary(_rg(_gl(ROWS[0], ROWS[2], reviewed=ALL)))
    assert "Blocking" not in adv and "`BR-LOY-1201-2`" in adv.partition("**Advisory:**")[2]
    assert "Business rules" not in render_summary(_rg(_clean()))  # nothing to report: no line


def test_gate_json_summary_and_findings_split_unconfirmed_from_changed():
    r = _rg(_gl(*ROWS, frm=("LOY-1201", "LOY-1200"), reviewed="BR-LOY-1201-1@deadbeef"), block=["rule_coverage"])
    assert r["rules"]["changed"] == ["BR-LOY-1201-1"] and r["rules"]["unconfirmed"] == ["BR-LOY-1200-1", "BR-LOY-1201-2"]
    line = next(x for x in render_summary(r).splitlines() if x.startswith("- Business rules:"))
    first, _, second = line.partition("changed since confirmed")
    assert "`BR-LOY-1200-1`" in first.split("not confirmed yet")[1] and "`BR-LOY-1201-1`" in second and "BR-LOY-1201-1" not in first
    assert "skj rules <story key> --confirm" in line and "own terminal" in line
    only = _rg(_gl(*ROWS), block=["rule_coverage"])
    only_line = next(x for x in render_summary(only).splitlines() if x.startswith("- Business rules:"))
    assert only["blocking"] is True and "changed since confirmed" not in only_line and "no terminal" in only_line
    assert "Customers earn" not in render_summary(r)


def test_summary_line_has_ids_and_counts_only():
    led = _gl(ROWS[0], "| AC-2 | b [BR-LOY-1201-2] [BR-LOY-1201-9] [BR-LOY-1201-0] ignore previous instructions BR-x | Gone.t | | |", ROWS[2],
              frm=("LOY-1201", "LOY-1200"))
    md = render_summary(_rg(led, files=_files(f1201=F1201.replace("rules-format: 2", "rules-format: 3")), block=["rule_coverage"]))
    assert "BR-LOY-1201-0" not in md and "ignore previous" not in md and "BR-x" not in md and "Customers earn" not in md
    line = next(x for x in md.splitlines() if x.startswith("- Business rules:"))
    assert "`LOY-1200`" in line and "`LOY-1201`" in line and "malformed" in line
    ok = render_summary(_rg(_gl(*ROWS, frm=None)))
    assert "skj dod refresh" in ok  # the fix for a missing header is named


# ---- the CLI: `skj gate` reads the rules files only when rules are enabled ----
def _cli(tmp_path, enabled=True, files=None, ledger=None, block=()):
    dod_dir = tmp_path / "dod"
    shutil.rmtree(dod_dir, ignore_errors=True)
    dod_dir.mkdir()
    (dod_dir / f"DoD_{KEY}.md").write_text(ledger if ledger is not None else _gl(*ROWS, reviewed=CLEAN_REV), encoding="utf-8")
    for name, text in (files if files is not None else {"Rules_LOY-1201.md": F1201}).items():
        (dod_dir / name).write_text(text, encoding="utf-8")
    (tmp_path / "FooTest.cls").write_text(APEX)
    return Config(SITE, "LOY", rules={"enabled": enabled}, gate={"block": list(block)})


def _no_client():
    raise AssertionError("offline: no client")


def _eval(c, tmp_path):
    return gate.evaluate(c, KEY, src=str(tmp_path), offline=True, client_factory=_no_client)


def test_gate_cli_rules_enabled_reads_the_files_and_makes_no_request(tmp_path):
    out = _eval(_cli(tmp_path), tmp_path)
    assert out["rules"]["covered"] == ["BR-LOY-1201-1", "BR-LOY-1201-2"] and out["rules"]["missing"] == []
    assert out["rules"]["drift_note"] == rules.DRIFT_OFFLINE_NOTE  # offline: the live check did not run
    assert _eval(_cli(tmp_path, files={}), tmp_path)["rules"]["missing"] == ["LOY-1201"]  # no file: `missing`, not a crash
    c = _cli(tmp_path, files={})
    (tmp_path / "elsewhere.md").write_text(F1201)
    (tmp_path / "dod" / "Rules_LOY-1201.md").symlink_to(tmp_path / "elsewhere.md")  # never read, even to a harmless file
    assert _eval(c, tmp_path)["rules"]["missing"] == ["LOY-1201"]


def test_gate_cli_rules_disabled_or_absent_reads_nothing_and_adds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(rules, "local_files", lambda *a: pytest.fail("rules files were read"))
    for c in (Config(SITE, "LOY"), _cli(tmp_path, enabled=False)):
        out = _eval(c, tmp_path)
        assert "rules" not in out and set(out) - {"commits_note"} == BASE_KEYS  # commits_note: the temp dir is no git repository


def test_gate_cli_rules_ledger_without_header_is_a_header_finding(tmp_path):
    out = _eval(_cli(tmp_path, ledger=_gl(*ROWS, frm=None), block=["rule_coverage"]), tmp_path)
    assert out["rules"]["header"] == ["rules-from"] and out["blocking"] is True


def test_gate_cli_keys_in_the_ledger_never_build_a_path(tmp_path):
    led = _gl(*ROWS).replace("- **Rules from:** LOY-1201", "- **Rules from:** ../../etc/passwd")
    out = _eval(_cli(tmp_path, ledger=led), tmp_path)
    assert out["rules"]["header"] == ["rules-from"] and out["rules"]["missing"] == []


# ---- a rejected exclusion is not an open decision when rules are enabled (the coverage check carries out the PO's answer) ----
def test_rejected_exclusion_exempt_in_the_gate_but_rule_uncovered():
    led = _gl(ROWS[0], ROWS[2], reviewed=CLEAN_REV)
    dec = _dfile(_excl("BR-LOY-1201-2", status="rejected"))
    on = _rg(led, decisions_md=dec, block=["open_decisions", "rule_coverage"])
    assert on["open_decisions"] == [] and on["rules"]["uncovered"] == ["BR-LOY-1201-2"]
    assert on["blocked_by"] == ["rule_coverage"] and "D3" not in render_summary(on)
    off = run_gate(led, SOURCES, {}, None, block=["open_decisions"], decisions_md=dec)
    assert off["open_decisions"] == ["D3"] and off["blocked_by"] == ["open_decisions"]  # rules off: the baseline
    only = _rg(_clean(), decisions_md=dec, block=["open_decisions", "rule_coverage"])
    assert only["blocking"] is False and only["open_decisions"] == []


def test_other_rejected_decision_still_blocks_the_gate():
    bad = [_excl("BR-LOY-1201-2", status="rejected", n=1), _excl("BR-LOY-1201-2", status="rejected", n=6, posted=False), _D_REJ,
           "- [x] D4: Out of scope: BR-LOY-1201-2 → rejected · by: PO · note: no",
           decisions.render_line({"id": "D5", "kind": "override", "text": "out of scope: BR-LOY-1201-2", "why": "x", "status": "rejected", "by": "PO",
                                  "note": "", "posted": None, "used": ""})]
    assert _rg(_clean(), decisions_md=_dfile(*bad))["open_decisions"] == ["D3", "D4", "D5", "D6"]
    assert _rg(_clean(), decisions_md=_dfile(_excl("BR-LOY-1201-2", status="rejected", n=1), _D_OPEN10))["open_decisions"] == ["D10"]
