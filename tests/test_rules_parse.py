"""Business-rules normaliser and parser (RU1). The input is an untrusted Jira description in ADF: nothing may be lost silently and
no text may become structure. Ported from the fork's parser tests with ADF input in place of HTML; the HTML-only steps (comments,
style/script/xml elements, entities, stray `<`) have no ADF counterpart and are listed in tests/PORT_NOTES.md. Fixture descriptions
are hand-written (see test_rules_fixtures.py). All data is fictional."""
import json
import signal
import time
from pathlib import Path

import pytest

from skj import rules

pytestmark = pytest.mark.rules

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "rules"
KEY = "LOY-1201"


class _TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise _TooSlow()


def _cpu_bounded(bound, fn, label=""):
    """fn() under a CPU-time limit (HD2-AC3, the pattern of tests/test_strict_linear.py); returns fn's result, fails when cut off."""
    # CPU time, not the wall clock: load slows the clock, not the work; a wall-clock backstop at 10x still ends a stuck call
    old, old_real = signal.signal(signal.SIGPROF, _alarm), signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_PROF, bound)
    signal.setitimer(signal.ITIMER_REAL, 10 * bound)
    t0 = time.process_time()
    try:
        out = fn()
    except _TooSlow:
        pytest.fail("%s: cut off at %.1fs (CPU or 10x wall clock)" % (label, bound))
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGPROF, old)
        signal.signal(signal.SIGALRM, old_real)
    elapsed = time.process_time() - t0
    assert elapsed < bound, "%s: %.2fs CPU" % (label, elapsed)
    return out


def fixture(name):
    return json.loads((FIX / (name + ".json")).read_text(encoding="utf-8"))


def t(s, *marks):
    n = {"type": "text", "text": s}
    if marks:
        n["marks"] = [{"type": m} for m in marks]
    return n


def para(*parts):
    return {"type": "paragraph", "content": [t(x) if isinstance(x, str) else x for x in parts]}


def doc(*blocks):
    return {"type": "doc", "version": 1, "content": list(blocks)}


def ul(*items, kind="bulletList"):
    return {"type": kind, "content": [{"type": "listItem", "content": [i if isinstance(i, dict) else para(i)]} for i in items]}


def table(*rows):
    return {"type": "table", "content": [{"type": "tableRow", "content": [
        {"type": "tableCell", "content": [para(c)]} for c in row]} for row in rows]}


BR = {"type": "hardBreak"}


def norm(d):
    return rules.normalise(d)[0]


def text_doc(s):
    """One paragraph holding `s` as one text node (an empty string gives an empty paragraph)."""
    return doc(para(*([s] if s else [])))


def parsed(d, key=KEY):
    lines, probs = rules.normalise(d)
    return rules.parse(lines, key, probs)


def plain(text, key=KEY):
    """Parse typed plain text: one paragraph per line (the way the Jira editor stores typed lines)."""
    return parsed(doc(*(para(*([line] if line else [])) for line in text.splitlines())), key)


def issues(p, level=None):
    return sorted(x["issue"] for x in p["problems"] if level in (None, x["level"]))


def by_n(p):
    return {r["n"]: r for r in p["rules"]}


# ---- fixtures ----

def test_po_typed_fixture():
    p = parsed(fixture("feature_po_typed"), "LOY-1201")
    assert p["found"] and p["notes"] == []
    rs = by_n(p)
    assert sorted(rs) == [1, 2, 3]
    assert [rs[n]["status"] for n in (1, 2, 3)] == ["approved", "draft", "approved"]  # `Status : Approved` is Rule 3's
    assert rs[3]["body"] == ["Points can't pay for delivery.", "Status : Approved"]  # the unnumbered Rule: below is not absorbed
    assert rs[1]["body"][0] == "Customers earn 1 point for every whole euro they spend."
    assert [x["issue"] for x in p["problems"] if x["rule"] is None] == ["no-number"]
    assert not [x for x in p["problems"] if x["level"] == "error" and x["rule"]]
    assert {"BR-LOY-1201-1"}.isdisjoint({x["rule"] for x in p["problems"]})  # Rule 1 has Why, Example, Owner and Status


def test_epic_typed_fixture():
    p = parsed(fixture("epic_po_typed"), "LOY-1200")
    assert [(r["id"], r["status"]) for r in p["rules"]] == [("BR-LOY-1200-1", "approved")] and issues(p) == ["no-example", "no-owner", "no-why"]
    assert p["rules"][0]["body"] == ["Members must be 18 or older.", "Status: approved"]


def test_word_paste_fixture():
    lines, probs = rules.normalise(fixture("feature_word_paste"))
    assert not probs and "\u200b" not in "".join(lines) and "\u00a0" not in "".join(lines)
    p = rules.parse(lines, "LOY-1201", probs)
    rs = by_n(p)
    assert sorted(rs) == [1, 2, 4] and [rs[n]["status"] for n in (1, 2, 4)] == ["approved", "draft", "approved"]
    assert rs[2]["body"][1:3] == ["When the basket holds only a gift card, then no points are earned.",
                                  "When a gift card is bought with a normal item, then only the normal item earns points."]  # the `·` bullets
    assert "24.80 euros" in " ".join(rs[1]["body"])  # the no-break space became a space
    assert [(x["rule"], x["issue"]) for x in p["problems"] if x["level"] == "error"] == [(None, "bad-rule-line")]  # `Rule 3 -` is loud


# ---- normalisation, one test per step ----

def test_marks_are_ignored_and_split_text_nodes_join():
    assert norm(doc(para(t("Rule 1:", "strong"), t(" Members "), t("must", "em"), t(" be 18.")))) == ["Rule 1: Members must be 18."]
    assert norm(doc(para(t("Ru"), t("le", "strong"), t(" 1: x")))) == ["Rule 1: x"]


def test_every_block_starts_a_line_and_hard_break_splits():
    assert norm(doc(para("a", BR, "b"), {"type": "heading", "attrs": {"level": 2}, "content": [t("c")]}, para("d"))) == ["a", "b", "c", "d"]
    assert norm(doc({"type": "codeBlock", "content": [t("Rule 1: a\nStatus: draft\r\nWhy: b")]})) == ["Rule 1: a", "Status: draft", "Why: b"]


def test_nested_containers_are_read():
    panel = {"type": "panel", "attrs": {"panelType": "info"}, "content": [para("in a panel")]}
    quote = {"type": "blockquote", "content": [para("quoted")]}
    expand = {"type": "expand", "attrs": {"title": "More"}, "content": [para("hidden until opened")]}
    layout = {"type": "layoutSection", "content": [{"type": "layoutColumn", "attrs": {"width": 50}, "content": [para("col")]}]}
    tasks = {"type": "taskList", "content": [{"type": "taskItem", "attrs": {"state": "TODO"}, "content": [t("a task")]}]}
    assert norm(doc(panel, quote, expand, layout, tasks)) == ["in a panel", "quoted", "More", "hidden until opened", "col", "a task"]
    assert rules.normalise(doc(panel, quote, expand, layout, tasks))[1] == []


def test_mention_and_email_scrubbed():
    mention = {"type": "mention", "attrs": {"id": "712020:00000000-0000-0000-0000-000000000000", "text": "@Jane Doe"}}
    assert norm(doc(para("Ask ", mention, " or jane.doe@example.com or [~accountid:712020:abc].", ))) == ["Ask @user or [email] or @user."]
    assert norm(doc(para(t("a link", "link"), " and a@b"))) == ["a link and a@b"]


@pytest.mark.parametrize("parts", [["jane.doe@example.com"], [t("jane.doe", "em"), "@example.com"], ["jane.doe@", t("example.com", "strong")],
                                   ["jane\u200b.doe@example.com"], ["JANE@EXAMPLE.COM"]])
def test_split_and_marked_emails_are_scrubbed(parts):
    assert norm(doc(para("Ask ", *parts, " now"))) == ["Ask [email] now"]


def test_typed_at_name_is_left_as_typed():
    assert norm(text_doc("Ask @Jane Doe now")) == ["Ask @Jane Doe now"]  # not a mention node: nothing marks it as a person


def test_inline_nodes_render_their_visible_text():
    nodes = [{"type": "emoji", "attrs": {"shortName": ":smile:", "text": "\U0001f604"}}, " ",
             {"type": "status", "attrs": {"text": "IN REVIEW", "color": "blue"}}, " ",
             {"type": "inlineCard", "attrs": {"url": "https://example.com/x"}}, " ",
             {"type": "date", "attrs": {"timestamp": "1767225600000"}}, " ", {"type": "placeholder", "attrs": {"text": "Type here"}}]
    assert norm(doc(para(*nodes))) == ["\U0001f604 IN REVIEW https://example.com/x 2026-01-01"]
    for ts in ("999999999999999", "x", "-1", "", "1" * 400):  # out of range or not digits: no date, never an error
        assert rules.normalise(doc(para("on ", {"type": "date", "attrs": {"timestamp": ts}}))) == (["on"], [])
    started = time.perf_counter()  # the length is checked before int(): a huge digit run costs nothing (no int-limit on 3.9)
    assert rules.normalise(doc(para("on ", {"type": "date", "attrs": {"timestamp": "9" * 1000000}}))) == (["on"], [])
    assert time.perf_counter() - started < 1.0
    media = {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "x", "type": "file", "collection": "c"}}]}
    assert rules.normalise(doc(media, para("after"), {"type": "rule"})) == (["after"], [])


def test_text_cannot_become_structure():
    """Markup typed as text stays text."""
    assert norm(text_doc("<b>Rule 1: x</b>")) == ["<b>Rule 1: x</b>"]
    assert norm(text_doc("if a < b then Rule 1: x")) == ["if a < b then Rule 1: x"]
    assert norm(text_doc("<!-- Rule 9: x -->")) == ["<!-- Rule 9: x -->"]


def test_line_breaks_inside_text_split():
    assert norm(text_doc("Rule 1: a\r\nStatus: draft\rWhy: b\nOwner: c")) == ["Rule 1: a", "Status: draft", "Why: b", "Owner: c"]
    assert norm(text_doc("a\u2028b\x0bc\x85d")) == ["a", "b", "c", "d"]


def test_space_separators_become_spaces_and_zero_width_removed():
    assert norm(text_doc("a\u2003b\u202fc\u3000d\u1680e\u00a0f")) == ["a b c d e f"]
    assert norm(text_doc("Ru\u200bl\u2060e\ufeff 1\u200d:\u00ad x")) == ["Rule 1: x"]
    assert norm(text_doc("a\x00b\x1fc")) == ["a b c"]


def test_bidi_override_is_stripped_from_rule_text():
    """RU1 review: U+202E (right-to-left override) and its partners never reach a stored rule line."""
    b = parsed(doc(para("Business rules"), para("Rule 1: Members earn \u202e01\u202c points"), para("Why: \u202a\u2066loyalty\u2069")))
    body = b["rules"][0]["body"]
    assert body == ["Members earn 01 points", "Why: loyalty"]
    assert not any(c in "".join(body) for c in "\u202a\u202c\u202e\u2066\u2069")


@pytest.mark.parametrize("bullet", list(rules.BULLETS))
def test_each_bullet_is_stripped(bullet):
    assert norm(text_doc(f"{bullet}  Rule 1: x")) == ["Rule 1: x"]
    assert norm(text_doc(f"{bullet}\u00a0{bullet} Rule 1: x")) == ["Rule 1: x"]
    assert norm(text_doc(f"{bullet}Rule 1: x")) == ["Rule 1: x"]


def test_lone_o_and_list_numbers():
    assert norm(text_doc("o   Rule 1: x")) == ["Rule 1: x"]
    assert norm(doc(para("o"), para("ok then"), para("one more"))) == ["ok then", "one more"]  # `o` only when alone
    assert norm(doc(para("1. Rule 1: x"), para("2) Rule 2: y"), para("3.Rule"))) == ["Rule 1: x", "Rule 2: y", "3.Rule"]
    assert norm(doc(para("24.80 euros"), para("2026. Plan"))) == ["24.80 euros", "2026. Plan"]
    assert norm(doc(ul("Rule 1: x", "Rule 2: y", kind="orderedList"))) == ["Rule 1: x", "Rule 2: y"]  # the list's numbers are not text
    assert norm(doc(para("· 1. Rule 1: x"), para("1. 2. y"))) == ["Rule 1: x", "2. y"]  # one run, then one number


def test_sign_glued_to_a_digit_is_not_a_bullet():
    assert norm(doc(para("-10 points"), para("- 10 points"), para("*5 off"), para("-x"))) == ["-10 points", "10 points", "*5 off", "x"]


def test_euro_sign_not_stripped():
    assert norm(doc(para("€5 off"), para("$ 5 off"), para("+1 point"), para("(a) b"))) == ["€5 off", "$ 5 off", "+1 point", "(a) b"]


def test_list_items_and_empty_lines():
    assert norm(doc(ul(para(t("Rule 1:", "strong"), " x ")))) == ["Rule 1: x"]
    assert norm(doc(para(), para("  "), para("\u200b"), para("-"), para("·"), para("a\t \t b"), para(BR, BR))) == ["a b"]
    assert norm(doc()) == [] and norm(None) == [] and norm(doc(para())) == []
    assert norm(doc(ul(ul("nested")))) == ["nested"]


def test_table_rows_render_with_pipes():
    assert norm(doc(table(["Tier", "Points"], ["Gold", "2"]))) == ["Tier | Points", "Gold | 2"]
    cell = {"type": "tableRow", "content": [{"type": "tableHeader", "content": [para("a", BR, "b"), para("c")]}, {"type": "tableCell", "content": []}]}
    assert norm(doc({"type": "table", "content": [cell]})) == ["a b c |"]


def test_oversize_input_is_an_error_not_a_parse():
    lines, probs = rules.normalise(doc(*[para("a" * 1000)] * 1100))
    assert lines == [] and [x["issue"] for x in probs] == ["section-too-big"]
    assert rules.normalise(doc(*[para("a" * 1000)] * 1000))[1] == []


def _nest(depth):
    d = para("deep")
    for _ in range(depth):
        d = {"type": "blockquote", "content": [d]}
    return doc(d)


def test_too_deep_is_unreadable_and_nothing_is_read():
    assert rules.normalise(_nest(40)) == (["deep"], [])
    lines, probs = rules.normalise(_nest(60))
    assert lines == [] and probs == [{"rule": None, "level": "error", "issue": "unreadable-content"}]


@pytest.mark.parametrize("node", [{"type": "extension", "attrs": {"extensionKey": "x"}}, {"type": "inlineExtension", "attrs": {}},
                                  {"type": "bodiedExtension", "content": [para("Rule 2: inside a macro")]}, {"type": "mysteryNode", "content": [para("kept")]},
                                  {"no": "type"}, "a string", {"type": "text", "text": 5}, {"type": "paragraph", "content": "x"}])
def test_unknown_malformed_or_macro_content_is_one_error(node):
    lines, probs = rules.normalise(doc(para("Business rules"), node if node != {"type": "inlineExtension", "attrs": {}} else para(node), para("after")))
    assert probs == [{"rule": None, "level": "error", "issue": "unreadable-content"}]
    assert lines[0] == "Business rules" and lines[-1] == "after"  # the readable text around it stays
    if isinstance(node, dict) and node.get("content") and isinstance(node["content"], list):
        assert len(lines) == 3  # the text inside an unknown node or a macro body is read too (and the error says to look)


def test_not_a_doc_is_unreadable():
    assert rules.normalise({"type": "paragraph", "content": [t("x")]})[1] == [{"rule": None, "level": "error", "issue": "unreadable-content"}]
    assert rules.normalise("plain")[1] == [{"rule": None, "level": "error", "issue": "unreadable-content"}]


# ---- parsing ----

def test_heading_variants_and_no_rules_before_it():
    for h in ("Business rules", "business RULES", "Business rules:", "**Business rules**".strip("*")):
        assert plain(f"{h}\nRule 1: x\nStatus: approved")["rules"][0]["status"] == "approved"
    assert parsed(doc({"type": "heading", "attrs": {"level": 2}, "content": [t("Business rules")]}, para("Rule 1: x")))["rules"][0]["n"] == 1
    assert not plain("Rule 1: x")["rules"]  # no heading: nothing becomes a rule
    assert not plain("Business rules (v2)\nRule 1: x")["rules"]
    p = plain("Business rulesX\nRule 1: x")
    assert p["found"] and issues(p) == ["no-section-heading"] and p["rules"] == []
    assert not plain("Intro text only")["found"] and plain("Intro text only")["problems"] == []
    p = plain("Rule 1: before\nBusiness rules\nRule 2: after\nStatus: draft")
    assert issues(p, "error") == ["no-section-heading"] and [r["n"] for r in p["rules"]] == [2]  # a rule above the heading is not silently dropped


def test_text_before_rule_1_is_a_note():
    p = plain("Business rules\nSome intro the PO wrote.\nRule 1: x\nStatus: draft")
    assert p["notes"] == [rules.NOTE_INTRO] and issues(p, "error") == [] and [r["n"] for r in p["rules"]] == [1]
    assert plain("Business rules\nRule 1: x")["notes"] == []


@pytest.mark.parametrize("line", ["Status: approved", "STATUS: APPROVED", "status:Approved", "Status : Approved", "Status:   approved.", "sTaTuS :\u00a0approved."])
def test_status_case_spacing_and_trailing_period(line):
    p = plain(f"Business rules\nRule 1: x\n{line}")
    assert p["rules"][0]["status"] == "approved" and "no-status" not in issues(p)


def test_leading_zeros_same_number():
    p = plain("Business rules\nRule 01: x\nRule 002 : y")
    assert [(r["n"], r["id"]) for r in p["rules"]] == [(1, "BR-LOY-1201-1"), (2, "BR-LOY-1201-2")]
    p = plain("Business rules\nRule 1: x\nRule 001: y")  # the same number once zeros are dropped
    assert issues(p, "error") == ["duplicate-number"]


def test_ids_stable_when_rule_inserted_above():
    a = plain("Business rules\nRule 2: b\nStatus: approved\nRule 5: e\nStatus: draft")
    b = plain("Business rules\nRule 1: new\nStatus: approved\nRule 2: b\nStatus: approved\nRule 5: e\nStatus: draft")
    assert [(r["id"], r["hash"]) for r in a["rules"]] == [(r["id"], r["hash"]) for r in b["rules"] if r["n"] != 1]


def test_same_rule_different_markup_same_hash():
    a = parsed(doc(para(t("Business rules", "strong")), para(t("Rule 1:", "strong"), " Members  must be 18."), para("Status: approved")))
    b = parsed(doc(para("Business rules"), ul(para("Rule\u00a01: Members must ", t("be", "em"), " 18.")), para("•\u200b Status: approved")))
    c = plain("Business rules\nRule 1: Members must be 18.\nStatus: approved")
    d = parsed(doc(para("Business rules", BR, "Rule 1: Members must be 18.", BR, "Status: approved")))
    assert a["rules"][0]["hash"] == b["rules"][0]["hash"] == c["rules"][0]["hash"] == d["rules"][0]["hash"] and len(a["rules"][0]["hash"]) == 8


def test_hash_changes_with_status_text_and_number():
    base = rules.rule_hash(1, "approved", ["x", "Status: approved"])
    assert len({base, rules.rule_hash(1, "draft", ["x", "Status: approved"]), rules.rule_hash(2, "approved", ["x", "Status: approved"]),
                rules.rule_hash(1, "approved", ["y", "Status: approved"])}) == 4
    from skj import decisions
    assert base == decisions.line_hash("1|approved|x")  # the Status line is not hashed: the value is in the prefix
    a = plain("Business rules\nRule 1: x\nStatus: approved")["rules"][0]
    b = plain("Business rules\nRule 1: x\nStatus: draft")["rules"][0]
    assert a["hash"] != b["hash"]
    assert plain("Business rules\nRule 1: x\nStatus: approved")["rules"][0]["hash"] != plain("Business rules\nRule 1: y\nStatus: approved")["rules"][0]["hash"]


def test_unknown_label_stays_in_body():
    p = plain("Business rules\nRule 1: x\nNote: keep me\nSee also the appendix\nStatus: approved\nRule-based pricing applies here\nStatus quo applies")
    r = p["rules"][0]
    assert r["body"] == ["x", "Note: keep me", "See also the appendix", "Status: approved", "Rule-based pricing applies here", "Status quo applies"]
    assert issues(p, "error") == []


def test_wrapped_sentences_bullets_and_table_rows_belong_to_the_rule():
    p = plain("Business rules\nRule 1: Customers earn points\nwhen they pay.\n· When a, then b.\no When c, then d.\nTier | Points\nGold | 2\nStatus: approved")
    assert p["rules"][0]["body"] == ["Customers earn points", "when they pay.", "When a, then b.", "When c, then d.", "Tier | Points", "Gold | 2", "Status: approved"]
    q = parsed(doc(para("Business rules"), para("Rule 1: Customers earn points"), table(["Tier", "Points"], ["Gold", "2"]), para("Status: approved")))
    assert q["rules"][0]["body"] == ["Customers earn points", "Tier | Points", "Gold | 2", "Status: approved"]


# one test per problem key

def test_no_number():
    p = plain("Business rules\nRule: x\nStatus: approved\nRule 2: y\nRule :z")
    assert issues(p, "error") == ["no-number", "no-number"] and [r["n"] for r in p["rules"]] == [2]
    assert by_n(p)[2]["status"] == "draft"  # the unnumbered rule's Status did not leak into Rule 2 or the unnumbered rule's neighbour


@pytest.mark.parametrize("line", ["Rule 1a: x", "Rule 1.1: x", "Rule 0: x", "Rule 10000: x", "Rule 10000 : x", "Rule #1: x", "Rule \u0661: x",
                                  "Rule " + "9" * 5000 + ": x", "Rule 1" + "0" * 5000 + ": x"])
def test_bad_number(line):
    p = plain(f"Business rules\nRule 1: ok\n{line}\nStatus: approved")
    assert issues(p, "error") == ["bad-number"] and [r["n"] for r in p["rules"]] == [1]
    assert by_n(p)[1]["status"] == "draft"  # the bad rule's Status line is not Rule 1's


def test_max_number_accepted():
    p = plain("Business rules\nRule 9999: x\nRule 0008 : y")
    assert [r["id"] for r in p["rules"]] == ["BR-LOY-1201-9999", "BR-LOY-1201-8"]


@pytest.mark.parametrize("line", ["Rule 3 - x", "Rule 3 \u2013 x", "Rule 3 \u2014 x", "Rule 3 x", "Rule3: x", "Rule-3: x", "Rule - 3: x", "Rule. 3 x", "Rule 3\uff1a x",
                                  "RULE 12 words: x"])
def test_bad_rule_line(line):
    p = plain(f"Business rules\nRule 1: ok\n{line}\nStatus: approved")
    assert issues(p, "error") == ["bad-rule-line"] and [r["n"] for r in p["rules"]] == [1] and by_n(p)[1]["status"] == "draft"


@pytest.mark.parametrize("line", ["Rule-based pricing applies", "Rules: none", "Rule of thumb: be kind", "Ruler 1: x", "Rule", "Rule for gift cards: none", "A rule of thumb: be kind"])
def test_rule_words_in_body_text_are_not_errors(line):
    p = plain(f"Business rules\nRule 1: ok\n{line}")
    assert issues(p, "error") == [] and line in by_n(p)[1]["body"]


def test_no_section_heading():
    p = plain("Rule 1: x\nStatus: approved")
    assert p["found"] and issues(p) == ["no-section-heading"] and p["rules"] == []


def test_duplicate_number():
    p = plain("Business rules\nRule 1: x\nStatus: approved\nRule 2: y\nRule 1: z\nStatus: draft")
    assert [(x["rule"], x["issue"]) for x in p["problems"] if x["level"] == "error"] == [("BR-LOY-1201-1", "duplicate-number")]
    assert [r["n"] for r in p["rules"]] == [2]  # neither copy is citable
    assert [d["body"][0] for d in p["duplicates"]] == ["x", "z"]  # both copies are kept so they can be listed under Problems


def test_unknown_status():
    for line in ("Status: maybe", "Status:", "Status: approved by Bob", "Status: Approved, draft"):
        p = plain(f"Business rules\nRule 1: x\n{line}")
        assert issues(p, "error") == ["unknown-status"] and p["rules"][0]["status"] == "draft", line


@pytest.mark.parametrize("line", ["Status - Approved", "Status \u2013 approved", "Status\u2014approved", "Status = approved", "Status. approved", "Status; draft",
                                  "Status approved", "Status Draft", "Status-approved", "Status\uff1aapproved", "status retired"])
def test_bad_status_line(line):
    p = plain(f"Business rules\nRule 1: x\n{line}")
    assert issues(p, "error") == ["bad-status-line"] and p["rules"][0]["status"] == "draft" and "no-status" not in issues(p)


def test_status_quo_is_body_text():
    p = plain("Business rules\nRule 1: x\nStatus quo applies\nStatuses: none\nStatus: approved")
    assert issues(p, "error") == [] and p["rules"][0]["status"] == "approved" and "Status quo applies" in p["rules"][0]["body"]


def test_two_status():
    for second in ("Status: approved", "Status: draft", "Status: maybe"):
        p = plain(f"Business rules\nRule 1: x\nStatus: approved\n{second}")
        assert "two-status" in issues(p, "error") and p["rules"][0]["status"] == "draft", second


def test_rule_too_long():
    ok = plain("Business rules\nRule 1: " + "a" * 1980 + "\nStatus: draft")
    assert "rule-too-long" not in issues(ok)
    long = plain("Business rules\nRule 1: " + "a" * 1990 + "\nWhy: " + "b" * 20 + "\nStatus: draft")
    assert issues(long, "error") == ["rule-too-long"] and long["rules"][0]["id"] == "BR-LOY-1201-1"


def test_section_too_big_by_size_and_by_count():
    big = plain("Business rules\nRule 1: x\n" + "\n".join("w" * 1000 for _ in range(70)))
    assert big["found"] and issues(big) == ["section-too-big"] and big["rules"] == []
    many = plain("Business rules\n" + "\n".join(f"Rule {n}: x" for n in range(1, 102)))
    assert issues(many) == ["section-too-big"] and many["rules"] == []
    assert len(plain("Business rules\n" + "\n".join(f"Rule {n}: x" for n in range(1, 101)))["rules"]) == 100


def test_table_without_rules_is_no_parsable_rules():
    p = parsed(doc(para("Business rules"), table(["Tier", "Points"], ["Gold", "2"])))
    assert p["found"] and p["rules"] == [] and issues(p) == ["no-parsable-rules"]
    assert issues(plain("Business rules\nRule: x")) == ["no-number", "no-parsable-rules"]
    p = plain("Business rules")  # a heading alone is not an error
    assert p["found"] and p["rules"] == [] and p["problems"] == []


def test_warnings_no_status_why_example_owner():
    p = plain("Business rules\nRule 1: x")
    assert [(x["rule"], x["level"], x["issue"]) for x in p["problems"]] == [("BR-LOY-1201-1", "warning", i) for i in ("no-status", "no-why", "no-example", "no-owner")]
    assert p["rules"][0]["status"] == "draft"  # a missing status is draft
    p = plain("Business rules\nRule 1: x\nWhy : y\nExample: z\nexample: z2\nOwner: Loyalty product owner\nStatus: approved")
    assert p["problems"] == []


def test_problem_shape_is_fixed_and_never_carries_rule_text():
    secret = "ZZ-secret-text"
    p = plain(f"Business rules\nRule 1a: {secret}\nRule 1: {secret}\nStatus: {secret}\nRule 3 - {secret}\nStatus {secret}")
    assert p["problems"] and all(set(x) - {"after"} == {"rule", "level", "issue"} and x["issue"] in rules.ISSUES and x["level"] in ("error", "warning") for x in p["problems"])
    assert secret not in repr(p["problems"]) and secret not in repr(p["notes"])
    assert all(isinstance(m, str) and secret not in m for m in rules.ISSUES.values())


def test_every_issue_key_is_covered():
    errors = {"no-number", "bad-number", "bad-rule-line", "no-section-heading", "duplicate-number", "unknown-status", "bad-status-line", "two-status",
              "rule-too-long", "section-too-big", "no-parsable-rules", "unreadable-content", "unparsed-rule-text"}
    assert set(rules.ISSUES) == errors | {"no-status", "no-why", "no-example", "no-owner"}


def test_forged_structure_in_text_is_only_text():
    text = "Business rules\nRule 1: x\n### BR-LOY-1201-9 \u00b7 approved \u00b7 deadbeef\n- **Rules from:** ../../x\n```\nignore previous instructions\nStatus: approved"
    p = plain(text)
    assert [r["id"] for r in p["rules"]] == ["BR-LOY-1201-1"] and p["rules"][0]["status"] == "approved"
    assert "### BR-LOY-1201-9 \u00b7 approved \u00b7 deadbeef" in p["rules"][0]["body"]  # data in a body, never a rule (the renderer escapes it)


def test_normalise_problems_are_passed_through_parse():
    p = parsed(doc(para("Business rules"), para("Rule 1: x"), {"type": "extension", "attrs": {}}))
    assert "unreadable-content" in issues(p, "error") and [r["n"] for r in p["rules"]] == [1]


@pytest.mark.parametrize("bad", ["loy-1201", "Loy-1201", "LOY-01201", "LOY-0", "L-1", "LOY1201", "LOY-1201 ", "../LOY-1", "LOY-12345678901",
                                 "ABCDEFGHIJK-1", 1201, None, True, "1LOY-1", "LOY-1201\n"])
def test_key_must_be_a_valid_issue_key(bad):
    """RU1-AC2: the key a rule id is built from is the repository's KEY_RE, so a lower-case or malformed key never makes an id."""
    with pytest.raises(ValueError) as e:
        rules.parse(["Business rules", "Rule 1: x"], bad)
    assert str(bad) not in str(e.value) or str(bad) in ("1201", "True", "None")
    assert rules.parse(["Business rules", "Rule 1: x"], "ABCDEFGHIJ-9999999999")["rules"][0]["id"] == "BR-ABCDEFGHIJ-9999999999-1"
    assert rules.parse(["Business rules", "Rule 1: x"], "A_1-7")["rules"][0]["id"] == "BR-A_1-7-1"


# ---- adversarial inputs finish fast (no ReDoS) ----

N = 200000
ADVERSARIAL_TEXT = {
    "spaces": " " * N,
    "lt run": "<" * N,
    "digits": "Rule " + "1" * N,
    "zeros": "Rule " + "0" * N,
    "zeros colon": "Rule " + "0" * N + ":",
    "token digits": "Rule " + "a1" * (N // 2),
    "letters": "a" * N,
    "emails": "a@" * (N // 2),
    "email hosts": "a@b" + ".b" * (N // 2),
    "email local": "a" * (N // 2) + "@b" + ".b" * (N // 4),
    "ats": "@" * N,
    "email dots": "a.@b." * (N // 5),
    "email short hosts": "x.y@h.z " * (N // 8),
    "email long locals": ("a" * 60 + "@b.") * (N // 64),
    "wiki mentions": "[~" * (N // 2),
    "wiki mention runs": "[~" + "a" * 199 + "]" * (N // 200),
    "tabs": "\t" * N,
    "nbsp": "\u00a0" * N,
    "zero width": "\u200b" * N,
    "newlines": "\n" * N,
    "bullets": "·" * N,
    "bullets and o": "o " * (N // 2),
    "status": "Status" + " " * N,
    "ampersands": "&" * N,
    "list numbers": "1) " * (N // 3),
}
ADVERSARIAL_DOCS = {
    "many paragraphs": doc(*[para("x")] * (N // 4)),
    "many empty paragraphs": doc(*[para()] * (N // 2)),
    "many hard breaks": doc(para(*[BR] * N)),
    "many mentions": doc(para(*[{"type": "mention", "attrs": {"id": "a"}}] * (N // 4))),
    "many text nodes": doc(para(*[t("a@")] * (N // 4))),
    "many unknown nodes": doc(*[{"type": "x"}] * (N // 4)),
    "many table cells": doc(table(["c"] * (N // 8))),
    "deep at the limit": _nest(49),
    "too deep": _nest(10000),
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL_TEXT) + sorted(ADVERSARIAL_DOCS))
def test_adversarial_inputs_are_fast(name):
    d = ADVERSARIAL_DOCS[name] if name in ADVERSARIAL_DOCS else text_doc(ADVERSARIAL_TEXT[name])

    def run():
        lines, probs = rules.normalise(d)
        return rules.parse(["Business rules"] + lines + ["Rule 1: x"], KEY, probs)
    p = _cpu_bounded(1.0, run, name)
    assert isinstance(p["rules"], list)


def test_adversarial_parse_lines_are_fast():
    lines = ["Business rules", "Rule " + "1" * N + ":", "Status" + " " * N, "Rule 1 " + "x" * N, "Rule " + "a1" * (N // 2), "x" * N, "Rule 2: y"]
    tm = time.perf_counter()
    rules.parse(lines, KEY)
    assert time.perf_counter() - tm < 1.0


# ---- the fork's first fix round ----

def _h(*lines):
    return plain("Business rules\n" + "\n".join(lines))["rules"][0]["hash"]


def test_status_spelling_and_position_do_not_change_the_hash():
    base = _h("Rule 1: x", "Why: y", "Status: approved")
    for status in ("Status: Approved", "STATUS: approved", "Status : APPROVED.", "status:approved"):
        assert _h("Rule 1: x", "Why: y", status) == base, status
    assert _h("Rule 1: x", "Status: approved", "Why: y") == base  # moved within the rule
    assert _h("Rule 1: x", "Why: y", "Status: draft") != base  # a changed status value
    assert _h("Rule 1: x2", "Why: y", "Status: approved") != base  # a changed statement
    assert _h("Rule 1: x", "Why: y2", "Status: approved") != base


def test_label_spacing_and_case_do_not_change_the_hash():
    assert _h("Rule 1: x", "Why : y", "Owner:Loyalty product owner") == _h("Rule 1: x", "WHY:   y", "owner: Loyalty product owner")
    assert _h("Rule 1: x", "Example: a", "Example: b") != _h("Rule 1: x", "Example: b", "Example: a")  # order is content


def test_hash_of_stored_body_equals_the_parsed_hash():
    r = plain("Business rules\nRule 1: x\nWhy : y\nStatus : APPROVED.")["rules"][0]
    assert rules.rule_hash(r["n"], r["status"], r["body"]) == r["hash"]


@pytest.mark.parametrize("prefix", ["\u00d8", "\u00fc", "v", "q", "n", "\uf0b7", "\u25c6", "\u2192", "\u2705", ">", "#", "##", "(", '"', "a)", "i.", "1.1", "1",
                                    "1 |", "| 1 |", "\u00d8 \u00fc", "(a)", "\u201c"])
@pytest.mark.parametrize("label", ["Rule 2: b", "RULE 2 : b", "rule 2:b"])
def test_rule_behind_a_marker_is_flagged_not_absorbed(prefix, label):
    p = plain(f"Business rules\nRule 1: a\nStatus: approved\n{prefix} {label}\nStatus: draft")
    assert issues(p, "error") == ["bad-rule-line"] and [r["n"] for r in p["rules"]] == [1]
    assert by_n(p)[1]["status"] == "approved" and by_n(p)[1]["body"] == ["a", "Status: approved"]  # nothing absorbed, no status mix-up


@pytest.mark.parametrize("line", ["See Rule 2", "the rule 2 times", "Rule-based pricing", "Note - Rule 2 applies", "In Rule 2 we",
                                  "Fuel rule 2 costs", "truleX 2: x", "(see Rule 2)"])
def test_prose_mentioning_a_rule_is_body_text(line):
    p = plain(f"Business rules\nRule 1: a\n{line}")
    assert issues(p, "error") == [] and by_n(p)[1]["body"][-1] == line


@pytest.mark.parametrize("line", ["\uff32\uff55\uff4c\uff45 2: b", "Rule 2\uff1a b", "\uff32\uff55\uff4c\uff45 \uff12\uff1a b"])
def test_fullwidth_rule_is_flagged_not_silent(line):
    p = plain(f"Business rules\nRule 1: a\n{line}")
    assert issues(p, "error") == ["bad-rule-line"] and [r["n"] for r in p["rules"]] == [1]
    assert by_n(p)[1]["body"] == ["a"]  # the fullwidth line is not stored anywhere; the error is the signal


def test_two_rules_on_one_line_is_an_error():
    p = plain("Business rules\nRule 1: Members pay. Rule 2: Guests pay.\nStatus: approved")
    assert [(x["rule"], x["issue"]) for x in p["problems"] if x["level"] == "error"] == [("BR-LOY-1201-1", "bad-rule-line")]
    assert [r["n"] for r in p["rules"]] == [1]
    assert issues(plain("Business rules\nRule 1: Members pay (see Rule 2) always.\nStatus: approved"), "error") == []


@pytest.mark.parametrize("line", ["Status, approved", "Status (approved)", "Status [draft]", "Status / approved", "Status! approved"])
def test_any_punctuation_but_colon_after_status_is_bad_status_line(line):
    p = plain(f"Business rules\nRule 1: x\n{line}")
    assert issues(p, "error") == ["bad-status-line"] and p["rules"][0]["status"] == "draft"


def test_rule_word_probes_are_fast():
    for text in ("v " + "Rule 1" * 40000, "(" * 50000 + "Rule 1: x", "1." * 50000 + " Rule 2: x", "Rule 1: " + "Rule " * 40000, "Rule 1: " + "Rule 1" * 40000):
        tm = time.perf_counter()
        rules.parse(["Business rules", "Rule 1: a", text[:60000]], KEY)
        rules.normalise(text_doc(text))
        assert time.perf_counter() - tm < 1.0


def test_fullwidth_digit_after_rule_is_bad_number():
    p = plain("Business rules\nRule 1: a\nRule \uff12: b")
    assert issues(p, "error") == ["bad-number"] and [r["n"] for r in p["rules"]] == [1]


# ---- no rule is dropped silently, problems carry a locator, notes are shown ----

INTRO_RULEISH = ["Rule one: Gift cards earn no points.", "Rule A: x", "R1: x", "BR-1: x", "br-1201-1: x", "BR-LOY-1201-1: x", "br-loy-1201-1: x",
                 "Rul\u0435 one: x", "R\u03c5le A: x", "Rule \u0430: x", "Ｒｕｌｅ ｏｎｅ: x", "Ｒ１: x", "Rules: gift cards earn no points", "R 1 x",
                 "Status: approved", "Why: loyalty policy", "Example: 24 points", "Owner: Loyalty product owner",
                 "Status approved", "Status, draft", "STATUS : APPROVED"]


@pytest.mark.parametrize("line", INTRO_RULEISH)
def test_rule_like_intro_text_is_an_error_not_silent(line):
    p = plain(f"Business rules\n{line}\nStatus: approved\nRule 2: y\nStatus: approved")
    assert [r["id"] for r in p["rules"]] == ["BR-LOY-1201-2"]
    errs = [x for x in p["problems"] if x["level"] == "error"]
    assert [(x["rule"], x["issue"], x["after"]) for x in errs if x["issue"] == "unparsed-rule-text"] == [(None, "unparsed-rule-text", rules.FIRST)]
    assert "unparsed-rule-text" in rules.ISSUES and not [x for x in p["problems"] if x["rule"] == "BR-LOY-1201-2" and x["level"] == "error"]


def test_unparsed_intro_text_reaches_the_file_without_its_text():
    secret = "Gift cards earn no ZZSECRET points."
    d = doc(para("Business rules"), para(f"Rule one: {secret}"), para("Status: approved"), para("Rule 2: y"), para("Status: approved"))
    md = rules.render_file({"key": KEY, "fields": {}}, parsed(d), "example-site.atlassian.net", "2026-11-03", "0.1.0")
    assert "- error section (before the first rule): unparsed-rule-text — " + rules.ISSUES["unparsed-rule-text"] in md
    assert "ZZSECRET" not in md and "Rule one" not in md
    assert rules.parse_file(md, KEY)["invalid"] is False


def test_plain_intro_prose_is_a_note_not_an_error():
    p = plain("Business rules\nThe loyalty programme started in 2026.\nRule 1: x\nStatus: approved")
    assert issues(p, "error") == [] and p["notes"] == [rules.NOTE_INTRO]
    p = plain("Business rules\nRule one: x\nThe loyalty programme started in 2026.\nRule 1: x\nStatus: approved")  # both: an error and a note
    assert issues(p, "error") == ["unparsed-rule-text"] and p["notes"] == [rules.NOTE_INTRO]
    assert plain("Business rules\nRule one: x\nRule 1: x\nStatus: approved")["notes"] == []  # nothing but the error to say
    assert plain("Business rules")["notes"] == [rules.NOTE_EMPTY]


def test_intro_with_no_rule_at_all_still_reports_the_rule_like_line():
    p = plain("Business rules\nRule one: x\nStatus: approved")
    assert issues(p, "error") == ["no-parsable-rules", "unparsed-rule-text"]


def test_intro_notes_and_errors_in_a_word_paste():
    p = parsed(doc(para(t("Business rules", "strong")), para("R1:\u00a0Gift cards earn no points."), para("Status: approved"), para("Rule 2: y")))
    assert issues(p, "error") == ["unparsed-rule-text"] and [r["n"] for r in p["rules"]] == [2]


def _loc(p):
    return [(x["issue"], x.get("after")) for x in p["problems"] if x["rule"] is None and "after" in x]


def test_locator_before_the_first_rule():
    p = plain("Business rules\nRule 1a: x\nRule 2: y\nStatus: approved")
    assert _loc(p) == [("bad-number", rules.FIRST)]


def test_locator_after_a_middle_rule():
    p = plain("Business rules\nRule 1: a\nStatus: approved\nRule 2: b\nStatus: approved\nRule 3 - c\nRule 4: d\nStatus: approved")
    assert _loc(p) == [("bad-rule-line", "BR-LOY-1201-2")]


def test_locator_after_the_last_rule_and_for_several_lines():
    p = plain("Business rules\nRule 1: a\nRule 2: b\nRule: c\nRule 3x: d")
    assert _loc(p) == [("no-number", "BR-LOY-1201-2"), ("bad-number", "BR-LOY-1201-2")]


def test_locator_follows_a_duplicate_and_is_not_set_for_whole_section_problems():
    p = plain("Business rules\nRule 1: a\nRule 1: b\nRule: c")
    assert _loc(p) == [("no-number", "BR-LOY-1201-1")]
    p = plain("Rule 1: a")
    assert [x["issue"] for x in p["problems"]] == ["no-section-heading"] and "after" not in p["problems"][0]
    assert all("after" not in x for x in plain("Business rules\nRule 1: a")["problems"])  # a rule's own problems name the rule


def test_locator_is_an_id_or_a_fixed_phrase_never_text():
    p = plain("Business rules\nZZ intro Rule one: x\nRule 1a: ZZSECRET\nRule 2: y\nRule 3 - ZZSECRET")
    assert {x["after"] for x in p["problems"] if "after" in x} <= {rules.FIRST, "BR-LOY-1201-2"}
    assert "ZZ" not in repr(p["problems"])


def test_two_errors_in_one_bad_block_are_both_reported():
    p = plain("Business rules\nRule 1: ok\nStatus: approved\nRule 2 - x\nStatus approved\nRule 3: y\nStatus: approved")
    assert [(x["issue"], x["after"]) for x in p["problems"] if x["rule"] is None] == [("bad-rule-line", "BR-LOY-1201-1"), ("bad-status-line", "BR-LOY-1201-1")]
    assert [r["n"] for r in p["rules"]] == [1, 3] and by_n(p)[1]["status"] == "approved"  # the bad block's lines still are not Rule 1's


def test_bad_block_status_errors_all_listed():
    p = plain("Business rules\nRule: x\nStatus: maybe\nStatus: draft\nRule 2: y\nStatus: approved")
    assert [x["issue"] for x in p["problems"] if x["rule"] is None] == ["no-number", "two-status", "unknown-status"]
    p = plain("Business rules\nRule 1a: x\nStatus: approved\nRule 2: y\nStatus: approved")
    assert issues(p, "error") == ["bad-number"]  # a valid Status line under a bad block adds nothing


def test_duplicate_copies_report_their_own_errors_too():
    p = plain("Business rules\nRule 1: a\nStatus: maybe\nRule 1: b\nStatus: approved")
    assert sorted(x["issue"] for x in p["problems"] if x["level"] == "error") == ["duplicate-number", "unknown-status"]


def test_fold_does_not_make_ordinary_text_a_rule():
    assert plain("Business rules\nRule 1: ok\nRésumé of the old Rule 2 is below\nStatus: approved")["problems"][0]["issue"] != "bad-rule-line"
    assert plain("Business rules\nBranding applies\nRoutine: daily\nRule 1: x\nStatus: approved")["notes"] == [rules.NOTE_INTRO]


@pytest.mark.parametrize("line", ["Rul\u0435 2: b", "R\u03c5le 2: b", "Ru\u04cfe 2: b"])
def test_look_alike_letters_in_a_rule_label_are_flagged_below_a_rule_too(line):
    p = plain(f"Business rules\nRule 1: ok\n{line}\nStatus: approved")
    assert issues(p, "error") == ["bad-rule-line"] and by_n(p)[1]["status"] == "draft"


def test_notes_and_locators_are_linear_on_1_mib():
    n1 = 1 << 20
    for chunk in ("Rule one: x", "R1: " + "е" * 40, "Status: approved ", "BR-1: x\u200b", "Rule 1a: x\nStatus approved",
                  "Ｒｕｌｅ " + "x" * 90, "intro prose here", "R" * 100):
        d = doc(para("Business rules"), *[para(chunk)] * (n1 // (len(chunk) + 1) - 20))

        def run():
            lines, probs = rules.normalise(d)
            p = rules.parse(lines, KEY, probs)
            if len(p["rules"]) <= rules.MAX_RULES:
                rules.render_file({"key": KEY, "fields": {}}, p, "example-site.atlassian.net", "2026-11-03", "0.1.0")
        _cpu_bounded(1.5, run, chunk)

    def bad_blocks():  # many bad blocks, under the 64 KiB cap so the problems are really rendered
        p = rules.parse(["Business rules"] + ["Rule 1a: x", "Status approved"] * 2000 + ["Rule 1: y"], KEY)
        assert len(p["problems"]) >= 4000 and "section-too-big" not in issues(p)
        rules.render_file({"key": KEY, "fields": {}}, p, "example-site.atlassian.net", "2026-11-03", "0.1.0")
        body = ["Rule 1: a"] + ["\u00d8 Rule one: " + "x" * 20, "[1] R1: y", "\uff32\uff55\uff4c\uff45 " + "\u0435" * 60] * 250
        p = rules.parse(["Business rules"] + ["\u00d8 \u00d8 \u00d8 \u00d8 Rule one: x"] * 500 + body, KEY)
        assert "section-too-big" not in issues(p)
    _cpu_bounded(1.0, bad_blocks, "bad blocks")


def test_problems_of_the_new_key_and_notes_never_carry_text():
    secret = "ZZ-secret-text"
    p = plain(f"Business rules\nRule one: {secret}\nStatus: {secret}\nprose {secret}\nRule 2 - {secret}\nStatus {secret}\nRule 3: x")
    assert secret not in repr(p["problems"]) and secret not in repr(p["notes"])
    assert all(n in rules.NOTES for n in p["notes"])


MARKED = ["\u00d8 {}", "\uf0b7 {}", "v {}", "> {}", "a) {}", "(1) {}", "[1] {}", "1 - {}", "\u2022 {}", "\u00d8 \u00d8 a) {}"]
SHAPES = ["Rule one: Gift cards earn no points.", "Rule A: x", "R1: x", "BR-1: x", "BR-1201-2: x", "BR-LOY-1201-2: x", "Business rule 2: x",
          "Business Rule A: x", "Rules 2: x", "Rule No. 2: x", "Rul\u0435 one: x", "\uff32\uff55\uff4c\uff45 \uff4f\uff4e\uff45: x", "BR\u20111: x"]


@pytest.mark.parametrize("marker", MARKED)
@pytest.mark.parametrize("shape", SHAPES)
def test_marked_rule_like_intro_line_is_an_error_even_without_a_status_line(marker, shape):
    p = plain("Business rules\n" + marker.format(shape) + "\nRule 2: y\nStatus: approved")
    assert [(x["issue"], x["after"]) for x in p["problems"] if x["level"] == "error"] == [("unparsed-rule-text", rules.FIRST)], (marker, shape)


@pytest.mark.parametrize("marker", ["{}"] + MARKED)
@pytest.mark.parametrize("shape", SHAPES)
def test_rule_like_line_below_a_valid_rule_is_an_error_and_changes_nothing(marker, shape):
    base = plain("Business rules\nRule 1: ok\nStatus: approved\nRule 2: y\nStatus: approved")
    p = plain("Business rules\nRule 1: ok\nStatus: approved\n" + marker.format(shape) + "\nRule 2: y\nStatus: approved")
    errs = [(x["rule"], x["issue"], x.get("after")) for x in p["problems"] if x["level"] == "error"]
    assert errs == [(None, "unparsed-rule-text", "BR-LOY-1201-1")], (marker, shape)
    assert [r["status"] for r in p["rules"]] == ["approved", "approved"]
    assert p["rules"][1] == base["rules"][1]
    assert p["rules"][0]["body"][:2] == base["rules"][0]["body"][:2] and len(p["rules"][0]["body"]) == 3  # the line stays in Rule 1's text, nothing is merged elsewhere


def test_body_rule_like_line_with_a_status_under_it_is_one_error_not_two_status():
    p = plain("Business rules\nRule 1: ok\nStatus: approved\nRule one: x\nStatus: draft")
    assert issues(p, "error") == ["two-status", "unparsed-rule-text"] and by_n(p)[1]["status"] == "draft"  # the two Status lines are visible; so is the cause
    p = plain("Business rules\nRule 1: ok\nRule one: x\nStatus: draft")
    assert issues(p, "error") == ["unparsed-rule-text"] and by_n(p)[1]["status"] == "draft"
    assert [x["after"] for x in p["problems"] if x["issue"] == "unparsed-rule-text"] == ["BR-LOY-1201-1"]


def test_body_rule_like_line_on_a_duplicate_copy_is_reported_once():
    p = plain("Business rules\nRule 1: a\nRule one: x\nRule 1: b\nRule one: y")
    assert issues(p, "error") == ["duplicate-number", "unparsed-rule-text"]


@pytest.mark.parametrize("line", ["Rules: none", "Rule of thumb: be kind", "Rule-based pricing: yes", "Rules for gift cards apply: always",
                                  "Ruler 1: x", "Rule", "Note: see Rule 2", "Brand: Contoso", "Branch 1: x", "Bring 2: x",
                                  "Why: because a rule applies", "Owner: Loyalty product owner", "Tier | Points", "Policy 1: gift cards",
                                  "Regel 1: x", "Business: retail", "A rule of thumb: be kind", "Brie 2: x", "Brr-x: cold"])
def test_ordinary_body_text_and_labels_are_not_flagged(line):
    p = plain(f"Business rules\nRule 1: ok\n{line}\nStatus: approved")
    assert issues(p, "error") == [], line
    assert line in by_n(p)[1]["body"]


@pytest.mark.parametrize("line", ["\u0405tatus: approved", "\uff33tatus: approved", "\uff33\uff54\uff41\uff54\uff55\uff53 : approved", "\u00d8 Status: approved", "\u0405tatus approved",
                                  "Why: x", "\u00d8 Example: x"])
def test_look_alike_and_marked_label_lines_in_the_intro_are_errors(line):
    assert issues(plain(f"Business rules\n{line}\nRule 1: ok\nStatus: approved"), "error") == ["unparsed-rule-text"]


def test_a_repeated_heading_line_in_the_intro_is_only_a_note():
    p = plain("Business rules\nBusiness rules:\nRule 1: ok\nStatus: approved")
    assert issues(p, "error") == [] and p["notes"] == [rules.NOTE_INTRO]


# ---- DF1 (live runs A8-01, A8-10: D-017): a rule typed on one line ----
# Hand-written from the live E1 description (SPK-11, evidence A8/A8-01.before.SPK-11.json): one paragraph per rule.
ONE_LINE = ["Business rules",
            "Rule 1: A draft older than 30 days is archived. Status: approved. Why: storage. Example: day 31. Owner: PO",
            "Rule 2: Drafts are private. Status: approved. Why: privacy. Example: other user sees nothing. Owner: PO",
            "Rule 3: Drafts may be shared. Status: draft."]


def test_a_rule_typed_on_one_line_is_read_like_one_field_per_line():
    one = parsed(doc(*[para(x) for x in ONE_LINE]))
    assert [(r["id"], r["status"]) for r in one["rules"]] == [("BR-LOY-1201-1", "approved"), ("BR-LOY-1201-2", "approved"), ("BR-LOY-1201-3", "draft")]
    assert one["rules"][0]["body"] == ["A draft older than 30 days is archived.", "Status: approved.", "Why: storage.", "Example: day 31.",
                                       "Owner: PO"]
    assert not [p for p in one["problems"] if p["rule"] in ("BR-LOY-1201-1", "BR-LOY-1201-2")]  # no `no-status`, `no-why` ...
    split = parsed(doc(*[para(x) for x in ["Business rules", "Rule 1: A draft older than 30 days is archived.", "Status: approved.",
                                           "Why: storage.", "Example: day 31.", "Owner: PO"]]))
    assert split["rules"][0]["hash"] == one["rules"][0]["hash"]  # the same words are the same rule, however they are laid out


def test_an_inline_status_that_cannot_be_split_is_an_error_not_a_silent_draft():
    p = parsed(doc(para("Business rules"), para("Rule 1: Drafts are private Status: approved Why: privacy")))
    assert p["rules"][0]["status"] == "draft"
    assert {"rule": "BR-LOY-1201-1", "level": "error", "issue": "bad-status-line"} in p["problems"]


def test_prose_that_mentions_a_label_mid_sentence_is_not_split():
    p = parsed(doc(para("Business rules"), para("Rule 1: The record owner: the PO, signs. Status: approved")))
    assert p["rules"][0]["body"][0] == "The record owner: the PO, signs." and p["rules"][0]["status"] == "approved"


# ---- DF3 (review M3): a second rule inside a split segment is never absorbed by the rule above ----
# DF4 (re-review N2): the second marker after any separator, and the label spellings the grammar does not accept
DF4_JOINS = ["PO, Rule 2", "PO.Rule 2", '"PO." Rule 2', "PO Rule 2", "PO) Rule 2", "PO. Rule two", "PO. BR-2", "PO; rule 2",
             "PO. Business rule 2", "PO. R2", "PO, Rule2"]
M3 = "Rule 1: Drafts are archived. Why: storage. Example: day 31. Owner: PO. Rule 2: Drafts are private. Status: approved. Why: privacy."


@pytest.mark.parametrize("text", [M3, "Rule 1: Drafts are archived. Status: draft. Owner: PO. Rule 2: Drafts are private. Why: privacy.",
                                  "Rule 1: Drafts are archived.\nOwner: PO. Rule 2: Drafts are private.\nStatus: approved"]
                         + [M3.replace("Owner: PO. Rule 2", "Owner: " + x) for x in DF4_JOINS])
def test_a_second_rule_in_a_split_segment_is_an_error_and_binds_nobody(text):
    p = parsed(doc(para("Business rules"), *[para(x) for x in text.split("\n")]))
    assert {"rule": "BR-LOY-1201-1", "level": "error", "issue": "bad-rule-line"} in p["problems"]
    assert [(r["n"], r["status"]) for r in p["rules"]] == [(1, "draft")]  # never rule 2's Status


def test_the_live_e1_description_still_reads_cleanly():
    """The exact live E1 shape (SPK-11, A8/A8-01.before.SPK-11.json): an intro paragraph, the heading, one paragraph per rule."""
    p = parsed(doc(para("Fictional throwaway epic created by a live test."), *[para(x) for x in ONE_LINE]))
    assert [(r["n"], r["status"]) for r in p["rules"]] == [(1, "approved"), (2, "approved"), (3, "draft")]
    assert not [x for x in p["problems"] if x["level"] == "error"]


def test_df4_many_second_rule_markers_on_one_line_bind_nobody_in_linear_time():
    import time
    for n in (101, 400):
        text = "Rule 1: Drafts are archived. Why: storage. Example: day 31. Owner: PO" + ", Rule 2: x" * n + ". Status: approved."
        t0 = time.monotonic()
        p = parsed(doc(para("Business rules"), para(text)))
        assert time.monotonic() - t0 < 2
        assert [(r["n"], r["status"]) for r in p["rules"]] == [(1, "draft")]
        assert {"rule": "BR-LOY-1201-1", "level": "error", "issue": "bad-rule-line"} in p["problems"]


@pytest.mark.parametrize("line", ["See Rule 2: the details", "Ok Rule 2: b", "x y z w Rule 2: b", "Example: Rule one: x"])
def test_df4_a_rule_label_inside_a_line_fails_closed(line):
    """DF4 N2 (deliberate change): a `Rule <n>:`-shaped label after the start of a later line is never read as prose: draft + bad-rule-line."""
    p = plain(f"Business rules\nRule 1: ok\n{line}\nStatus: approved")
    assert "bad-rule-line" in issues(p, "error") and by_n(p)[1]["status"] == "draft"


# ---- DF5 (re-review D2) prose, DF6 (re-review R1, owner ruling: fail closed): prose shaped like a rule label is a draft with the
# reword hint, never a silent pass. Deliberate: a wrongly drafted rule is cheap, a silently merged second rule is not ----
@pytest.mark.parametrize("line", ["Why: these rules matter: privacy", "Example: the pricing rules change: on day 31", "Why: GDPR rules apply: always",
                                  "Why: the rules says: x", "Example: see BR-LOY-1-2: privacy", "Why: Q4 R2: launch",
                                  "Owner: the rules team: PO", "Example: per BR-7: day 31"])
def test_df6_prose_shaped_like_a_second_rule_label_fails_closed_with_the_reword_hint(line):
    p = plain(f"Business rules\nRule 1: Drafts are archived.\n{line}\nStatus: approved")
    assert "bad-rule-line" in issues(p, "error") and by_n(p)[1]["status"] == "draft", p["problems"]


@pytest.mark.parametrize("line", ["Why: privacy, see the rule of thumb", "Rules: none", "Example: Rule 2 above applies",
                                  "Why: the rulebook says so", "Owner: PO"])
def test_df6_prose_without_a_label_shape_stays_approved(line):
    p = plain(f"Business rules\nRule 1: Drafts are archived.\n{line}\nStatus: approved")
    assert "bad-rule-line" not in issues(p) and by_n(p)[1]["status"] == "approved", p["problems"]


# DF6 (re-review R1): the 14 joins the DF5 narrowing let through (a label after a plain space or any character), and a label in the
# text after `Rule 1:` itself
R1_JOINS = ["PO Rule two", "PO BR-2", "PO R2", "PO - Rule two", "PO \u2014 Rule two", "PO \u2013 BR-2", "PO / BR-LOY-1-2", "PO and BR-2",
            "PO & Rule two", "PO Business rule two", "PO Rule II", "PO R\u200b2", "PO Rule #2", "PO Rule (2)"]


@pytest.mark.parametrize("text", [M3.replace("Owner: PO. Rule 2", "Owner: " + x) for x in R1_JOINS]
                         + ["Rule 1: Drafts are archived. Rule two: Drafts are private. Status: approved.",
                            "Rule 1: Drafts are archived and BR-2: Drafts are private. Status: approved.",
                            "Rule 1: Drafts are archived (Rule (2): Drafts are private). Status: approved.",
                            "Rule 1: Drafts are archived \"Rule 2\" \u2014 Drafts are private. Status: approved.",
                            "Rule 1: Drafts are archived.\nWhy: storage -R2 - Drafts are private\nStatus: approved",
                            "Rule 1: Drafts are archived.\nWhy: storage Rule#2: Drafts are private\nStatus: approved",
                            "Rule 1: Drafts are archived.\nWhy: storage Rul\u0435 two: Drafts are private\nStatus: approved",
                            "Rule 1: Drafts are archived.\nWhy: storage \uff32\uff55\uff4c\uff45 two: Drafts are private\nStatus: approved"])
def test_df6_a_second_rule_label_after_any_character_is_an_error_and_binds_nobody(text):
    p = parsed(doc(para("Business rules"), *[para(x) for x in text.split("\n")]))
    assert {"rule": "BR-LOY-1201-1", "level": "error", "issue": "bad-rule-line"} in p["problems"], p["problems"]
    assert [(r["n"], r["status"]) for r in p["rules"]] == [(1, "draft")]


def test_df6_many_space_joined_word_labels_bind_nobody_in_linear_time():
    for n in (400, 500):
        text = "Rule 1: Drafts are archived. Why: storage. Example: day 31. Owner: PO" + " PO Rule two: x." * n + " Status: approved."
        t0 = time.monotonic()
        p = parsed(doc(para("Business rules"), para(text)))
        assert time.monotonic() - t0 < 2
        assert [(r["n"], r["status"]) for r in p["rules"]] == [(1, "draft")]


@pytest.mark.parametrize("line", ["Why: storage. Rule 2: x", "Why: storage; Rule two: x", "Example: rule A: B", "Why: x. BR-LOY-2: y",
                                  "Why: x. R2: y", "Owner: PO, BR-2: y", "Why: see rules 2: x", "Why: see rule no. 2: x"])
def test_df5_a_second_rule_label_inside_a_line_still_fails_closed(line):
    p = plain(f"Business rules\nRule 1: Drafts are archived.\n{line}\nStatus: approved")
    assert "bad-rule-line" in issues(p, "error") and by_n(p)[1]["status"] == "draft"


def test_df5_the_bad_rule_line_text_names_the_real_fix():
    assert "put each rule on its own line" in rules.ISSUES["bad-rule-line"]
    assert "reword the text after `Why:`" in rules.ISSUES["bad-rule-line"]


@pytest.mark.parametrize("unit", [", ", ":" + " " * 50, "br" + "-" * 50, "rule ", "rules no. ", ". r ", "; business ", "1" * 50])
def test_df5_the_second_label_search_is_linear_on_hostile_lines(unit):
    line = unit * ((1 << 20) // len(unit))
    t0 = time.monotonic()
    rules._SECOND_LABEL.search(rules._fold_all(line))
    assert time.monotonic() - t0 < 2
    p = plain("Business rules\nRule 1: Drafts are archived." + ", Rule 2: x" * 400 + "\nStatus: approved")
    assert by_n(p)[1]["status"] == "draft"
