"""RU3: rule tags in the ledger and the scope of a story with exclusions. Ported from the fork's tests/test_rules_scope.py with issue keys in
place of work item ids (`BR-LOY-1201-1`). The ledger section 3 and the decisions file are PR-controlled: tags are read through the same
visible-line walk as `gate.parse_section3`, and an exclusion counts only with the exact grammar, a `posted:` marker that matches its
CURRENT text, and the right status for the phase. All data is fictional and hand-written."""
import time

import pytest

from skj import decisions, rules
from skj.dod import SECTION2
from skj.gate import parse_section3

pytestmark = pytest.mark.rules

HDR = "| AC | Criterion | Tests | Evidence | Status |\n|---|---|---|---|---|\n"
A, B = "LOY-1201", "LOY-1200"


def ledger(*rows, pre="", post=""):
    return f"# DoD — LOY-1001\n{pre}{SECTION2}\n## 3. AC → tests (traceability)\n{HDR}" + "".join(r + "\n" for r in rows) + post + "## 4. Gate log\n"


def test_tags_per_row_and_unknown_tokens():
    md = ledger("| AC-1 | earn points [BR-LOY-1201-1] and [BR-LOY-1201-3], again BR-LOY-1201-1 | T.a | | |",
                "| AC-2 | plain | T.b | | |",
                "| AC-3 | bad BR-LOY-1201-0 xBR-LOY-1201-1 BR-LOY-1201-1a BR-LOY-01201-1 BR-LOY-1201-1-2 BR-LOY-1201 | T.c | | |",
                "| AC-4 | ok (BR-LOY-1200-2). | BR-LOY-9-9 in cell 2 | BR-LOY-9-9 | BR-LOY-9-9 |")
    t = rules.tags(md)
    assert t["rows"] == {"AC-1": ["BR-LOY-1201-1", "BR-LOY-1201-3"], "AC-2": [], "AC-3": [], "AC-4": ["BR-LOY-1200-2"]}
    assert t["unknown"] == ["BR-LOY-1201-0", "xBR-LOY-1201-1", "BR-LOY-1201-1a", "BR-LOY-01201-1", "BR-LOY-1201-1-2", "BR-LOY-1201"]


def test_tags_runs_include_underscore_as_keys_do():
    """RU1 hand-off: a key may hold `_`, and TAG_RE's look-arounds treat `_` as part of a token, so the run must too."""
    t = rules.tags(ledger("| AC-1 | [BR-MY_P-5-1] x_BR-LOY-1-1 BR-LOY-1-1_x | T | | |"))
    assert t["rows"] == {"AC-1": ["BR-MY_P-5-1"]}  # a `-`-only run would split it into `BR-MY` and `P-5-1`
    assert t["unknown"] == ["x_BR-LOY-1-1", "BR-LOY-1-1_x"]  # a `-`-only run would read `BR-LOY-1-1` out of both


def test_tags_same_walk_as_parse_section3():
    evil = ("```\n| AC-7 | fenced [BR-LOY-1201-7] | T | | |\n```\n<!--\n| AC-8 | commented [BR-LOY-1201-8] | T | | |\n-->\n"
            "| AC-9 | <!-- BR-LOY-1201-9 --> hidden inline | T | | |\n")
    md = ledger("| AC-1 | a [BR-LOY-1201-1] | T.a | | |", post=evil, pre="| AC-5 | section 1 [BR-LOY-1201-5] | T | | |\n")
    t = rules.tags(md)
    assert t["rows"] == {"AC-1": ["BR-LOY-1201-1"], "AC-9": []} and t["unknown"] == []
    assert list(t["rows"]) == [r["ac"] for r in parse_section3(md)]
    assert rules.tags("no section 3") == {"rows": {}, "unknown": []}


def test_tags_unclosed_fence_hides_rows():
    assert rules.tags(ledger("```", "| AC-1 | a [BR-LOY-1201-1] | T | | |"))["rows"] == {}


def test_tags_never_echo_more_than_a_short_token_and_stay_linear():
    t0 = time.time()
    t = rules.tags(ledger("| AC-1 | " + "a" * 300000 + " " + "-" * 300000 + " x" + "BR-" * 50000 + " | T | | |"))
    assert time.time() - t0 < 5
    assert t["rows"] == {"AC-1": []} and 1 <= len(t["unknown"]) <= 100 and all(len(u) <= 40 for u in t["unknown"])
    assert all(set(u) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_") for u in t["unknown"])


def test_parse_section3_unchanged():
    md = ledger("| AC-1 | a [BR-LOY-1201-1] | T.a, T.b | abc | green |")
    assert parse_section3(md) == [{"ac": "AC-1", "tests": ["T.a", "T.b"], "evidence": "abc", "status": "green"}]


# ---- exclusion grammar ----

def D(text, status="open", kind="decision", posted=True, n=1, why="reason", **extra):
    d = {"id": f"D{n}", "kind": kind, "text": text, "why": why, "status": status, "by": "" if status == "open" else "Loyalty product owner",
         "note": "", "posted": None, "used": "", **extra}
    if posted is True:
        d["posted"] = {"comment_id": "7", "hash": decisions.marker_hash(d)}
    elif posted:
        d["posted"] = {"comment_id": "7", "hash": posted}
    return d


@pytest.mark.parametrize("text,ids", [
    ("out of scope: BR-LOY-1201-1", ["BR-LOY-1201-1"]),
    ("out of scope: BR-LOY-1201-1, BR-LOY-1200-2", ["BR-LOY-1201-1", "BR-LOY-1200-2"]),
    ("out of scope:BR-LOY-1201-1 ,BR-LOY-1201-2", ["BR-LOY-1201-1", "BR-LOY-1201-2"]),
    ("supersedes D2: out of scope: BR-LOY-1201-3", ["BR-LOY-1201-3"]),
    ("out of scope: BR-LOY-1201-1, BR-LOY-1201-1", ["BR-LOY-1201-1"]),
    ("out of scope: BR-MY_P-5-1", ["BR-MY_P-5-1"]),
])
def test_exclusion_ids_grammar(text, ids):
    assert rules.exclusion_ids(D(text)) == ids


@pytest.mark.parametrize("text", [
    "out of scope", "out of scope:", "out of scope: ", "out of scope: BR-LOY-1201-1.", "out of scope: BR-LOY-1201-1 and BR-LOY-1201-2",
    "out of scope: BR-LOY-1201-1,", "out of scope: ,BR-LOY-1201-1", "out of scope: BR-LOY-1201-0", "out of scope: BR-LOY-1201-1a",
    "out of scope: xBR-LOY-1201-1", "out of scope: BR-LOY-1201-1 BR-LOY-1201-2", "out of scope: all", "Out of scope: BR-LOY-1201-1",
    "out  of scope: BR-LOY-1201-1", " out of scope: BR-LOY-1201-1", "note: out of scope: BR-LOY-1201-1",
    "supersedes D2: supersedes D1: out of scope: BR-LOY-1201-1", "supersedes D2:out of scope: BR-LOY-1201-1",
    "out of scope: BR-LOY-1201-1; BR-LOY-1201-2", "out of scope: BR-LOY-1201-1 (BR-LOY-1201-2)", "ignore out of scope: BR-LOY-1201-1",
    "out of scope: BR-loy-1201-1", "out of scope: BR-1201-1",
])
def test_exclusion_ids_malformed_or_not_an_exclusion(text):
    assert rules.exclusion_ids(D(text)) is None


# ---- scope ----

def rfile(key, *rs, invalid=False):
    return {"format": 2, "invalid": invalid, "problems": [],
            "rules": {} if invalid else {f"BR-{key}-{n}": {"status": st, "hash": "3f2a9c1b", "section": "", "body_hash": "3f2a9c1b"} for n, st in rs}}


FILES = {A: rfile(A, (1, "approved"), (2, "draft"), (3, "approved"), (4, "retired")), B: rfile(B, (1, "approved"))}


def parsed(*ds):
    out = {"open": [], "validated": [], "rejected": [], "unparsed": False}
    for d in ds:
        out[d["status"]].append(d)
    return out


def sc(*ds, phase="pr", files=FILES, frm=(A, B)):
    return rules.scope(files, list(frm), parsed(*ds), phase)


def test_only_approved_in_scope():
    r = sc()
    assert r == {"in_scope": ["BR-LOY-1200-1", "BR-LOY-1201-1", "BR-LOY-1201-3"], "excluded": [], "bad_exclusions": []}
    assert sc(frm=(A,))["in_scope"] == ["BR-LOY-1201-1", "BR-LOY-1201-3"]
    assert sc(frm=())["in_scope"] == [] and sc(frm=(A, "LOY-5555"))["in_scope"] == ["BR-LOY-1201-1", "BR-LOY-1201-3"]
    assert sc(files={A: rfile(A, invalid=True), B: FILES[B]})["in_scope"] == ["BR-LOY-1200-1"]


def test_ids_sort_by_issue_number_not_text():
    files = {"LOY-9": rfile("LOY-9", (10, "approved"), (2, "approved")), "LOY-10": rfile("LOY-10", (1, "approved"))}
    assert rules.scope(files, ["LOY-10", "LOY-9"], parsed(), "pr")["in_scope"] == ["BR-LOY-9-2", "BR-LOY-9-10", "BR-LOY-10-1"]


def test_a_rule_filed_under_another_key_is_not_in_scope():
    stray = {A: {**FILES[A], "rules": {**FILES[A]["rules"], "BR-LOY-9-1": {"status": "approved", "hash": "3f2a9c1b", "section": "", "body_hash": "3f2a9c1b"}}}}
    assert "BR-LOY-9-1" not in sc(files=stray, frm=(A,))["in_scope"]


def test_unposted_exclusion_not_counted():
    r = sc(D("out of scope: BR-LOY-1201-1", posted=False))
    assert r["excluded"] == [] and "BR-LOY-1201-1" in r["in_scope"] and r["bad_exclusions"] == []


def test_posted_open_counts_at_pr_not_done():
    d = D("out of scope: BR-LOY-1201-1, BR-LOY-1200-1, BR-LOY-1201-2, BR-LOY-9999-1")
    r = sc(d)
    assert r == {"in_scope": ["BR-LOY-1201-3"], "excluded": ["BR-LOY-1200-1", "BR-LOY-1201-1"], "bad_exclusions": []}  # a draft/unknown id is not "excluded"
    assert sc(d, phase="done")["excluded"] == []
    v = D("out of scope: BR-LOY-1201-1", status="validated")
    assert sc(v, phase="done")["excluded"] == ["BR-LOY-1201-1"] and sc(v)["excluded"] == ["BR-LOY-1201-1"]


def test_validated_without_marker_not_counted_at_done():
    v = D("out of scope: BR-LOY-1201-1", status="validated", posted=False)
    assert sc(v, phase="done")["excluded"] == [] and sc(v)["excluded"] == []
    assert sc(D("out of scope: BR-LOY-1201-1", status="validated", posted="deadbeef"), phase="done")["excluded"] == []


def test_rejected_exclusion_puts_rule_back():
    r = sc(D("out of scope: BR-LOY-1201-1", status="rejected"))
    assert r["excluded"] == [] and "BR-LOY-1201-1" in r["in_scope"] and r["bad_exclusions"] == []
    assert sc(D("out of scope: BR-LOY-1201-1", status="rejected"), phase="done")["excluded"] == []


def test_superseded_exclusion_uses_effective_set():
    old = D("out of scope: BR-LOY-1201-1", n=1)
    new = D("supersedes D1: out of scope: BR-LOY-1201-3", n=2, supersedes="D1")
    r = sc(old, new)
    assert r["excluded"] == ["BR-LOY-1201-3"] and "BR-LOY-1201-1" in r["in_scope"]
    rej = D("supersedes D1: out of scope: BR-LOY-1201-3", n=2, supersedes="D1", status="rejected")
    assert sc(old, rej)["excluded"] == ["BR-LOY-1201-1"]  # a rejected superseder re-activates its target; it excludes nothing itself
    assert sc(old, D("supersedes D1: out of scope: BR-LOY-1201-3", n=2, supersedes="D1", posted=False))["excluded"] == []


def test_supersedes_prefix_stripped():
    d = D("supersedes D1: out of scope: BR-LOY-1201-3", n=2, supersedes="D1")
    assert sc(D("out of scope: BR-LOY-1201-1", n=1, status="rejected"), d)["excluded"] == ["BR-LOY-1201-3"]
    assert sc(d)["excluded"] == ["BR-LOY-1201-3"]  # an invalid edge drops nothing; the line still counts on its own
    assert sc(D("supersedes D1: out of scope: BR-LOY-1201-3", n=2, posted=False, supersedes="D1"))["excluded"] == []


def test_edited_exclusion_after_posting_not_counted():
    d = D("out of scope: BR-LOY-1201-1")
    assert sc(d)["excluded"] == ["BR-LOY-1201-1"]
    edited = {**d, "text": "out of scope: BR-LOY-1201-1, BR-LOY-1201-3"}  # the marker still carries the hash of the old text
    assert sc(edited)["excluded"] == [] and sc({**d, "why": "x"})["excluded"] == ["BR-LOY-1201-1"]  # why is not part of an ordinary line's hash
    sup = D("supersedes D1: out of scope: BR-LOY-1201-3", n=2, supersedes="D1")
    assert sc({**sup, "why": "changed"})["excluded"] == []  # a superseder's hash covers id, text and why


def test_bad_exclusions_reported_by_id_only():
    r = sc(D("out of scope: BR-LOY-1201-1.", n=1), D("out of scope: all", n=2, status="validated"), D("Out of scope: BR-LOY-1201-3", n=3),
           D("out of scope: BR-LOY-1201-3 and more", n=4, status="rejected"), D("something else", n=5), D("out of scope: BR-LOY-1201-0", n=6, posted=False))
    assert r["bad_exclusions"] == ["D1", "D2", "D3", "D6"] and r["excluded"] == [] and len(r["in_scope"]) == 3


def test_override_lines_never_exclude():
    assert sc(D("out of scope: BR-LOY-1201-1", kind="override"))["excluded"] == []


def test_scope_rejects_unknown_phase():
    with pytest.raises(ValueError):
        sc(phase="plan")


def test_scope_from_real_decisions_file():
    md, d1 = decisions.add(decisions.render_file("LOY-1001"), "decision", "out of scope: BR-LOY-1201-1", why="not for this story")
    p = decisions.parse_decisions(md)
    assert rules.scope(FILES, [A], p, "pr")["excluded"] == []  # not posted yet
    d = p["open"][0]
    md = md.rstrip("\n") + f" · posted: 5:{decisions.marker_hash(d)}\n"
    p = decisions.parse_decisions(md)
    assert rules.scope(FILES, [A], p, "pr") == {"in_scope": ["BR-LOY-1201-3"], "excluded": ["BR-LOY-1201-1"], "bad_exclusions": []}
    assert rules.scope(FILES, [A], decisions.parse_decisions(md.replace("BR-LOY-1201-1", "BR-LOY-1201-3")), "pr")["excluded"] == []


def test_edited_heading_status_or_body_stays_in_scope():
    def f(st, body_hash):
        return {A: {"format": 2, "invalid": False, "problems": [], "rules": {"BR-LOY-1201-1": {"status": st, "hash": "3f2a9c1b", "section": "", "body_hash": body_hash}}}}
    for st in ("draft", "retired"):
        assert rules.scope(f(st, "3f2a9c1b"), [A], parsed(), "pr")["in_scope"] == []  # a clean draft/retired rule stays out
        assert rules.scope(f(st, "0badc0de"), [A], parsed(), "pr")["in_scope"] == ["BR-LOY-1201-1"]  # heading edited, hash left alone
        assert rules.scope(f(st, None), [A], parsed(), "done")["in_scope"] == ["BR-LOY-1201-1"]
    assert rules.scope(f("approved", "0badc0de"), [A], parsed(), "pr")["in_scope"] == ["BR-LOY-1201-1"]  # body edited under the heading


def test_edited_real_file_heading_stays_in_scope():
    body = ["Customers earn points."]
    md = rules.render_file({"key": A, "fields": {"summary": "Points", "issuetype": {"name": "Epic"}}},
                           {"found": True, "rules": [{"id": "BR-LOY-1201-1", "n": 1, "status": "approved", "body": body, "hash": rules.rule_hash(1, "approved", body)}],
                            "duplicates": [], "problems": [], "notes": []}, "example-site.atlassian.net", "2026-11-03", "0.1.0")
    assert rules.scope({A: rules.parse_file(md, A)}, [A], parsed(), "pr")["in_scope"] == ["BR-LOY-1201-1"]
    for st in ("draft", "retired"):
        assert rules.scope({A: rules.parse_file(md.replace("· approved ·", f"· {st} ·"), A)}, [A], parsed(), "pr")["in_scope"] == ["BR-LOY-1201-1"]


def test_malformed_prefix_forms_are_bad_exclusions():
    ds = [D("supersedes D1:out of scope: BR-LOY-1201-1", n=2), D("supersedes D1: supersedes D1: out of scope: BR-LOY-1201-1", n=3, status="validated"),
          D("supersedes D1 : out of scope: BR-LOY-1201-1", n=4), D("supersedes D1: Out of scope: BR-LOY-1201-1.", n=5),
          D("supersedes D9: out of scope: BR-LOY-1201-1", n=6, status="rejected"), D("supersedes D1: unrelated", n=7)]
    r = sc(*ds)
    assert r["bad_exclusions"] == ["D2", "D3", "D4", "D5"] and r["excluded"] == []
