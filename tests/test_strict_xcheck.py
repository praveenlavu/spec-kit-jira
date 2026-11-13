"""A3: the fork's tests/test_strict_xcheck.py (P4-11, `decisions-xcheck-*`), rebased on Jira: ADF comments read by `comments.fetch`
(the fake Jira), the `via: <id>:<hash8>` pin, updated/updateAuthor instead of the fork's work item revisions, and the skj rule names (strict.XC_RULES).
Only SYNCED lines (`via` + `by: approver`) are re-read; with approval "comment" a hand-written verdict and a posted decision missing
from the file are findings too. Dropped/changed fork cases are listed in the A3 report. Fictional data only; no socket."""
import pytest

import skj.__main__ as m
from skj import adf, approvals, comments, decisions, strict
from fakejira import ME_ID
from test_trust_crosscheck import (APPROVER, KEY, LINES, OTHER, _boom, _delete, _fake, _pin, _put, _say, _synced, details, rules,
                                   world)  # noqa: F401  (world: fixture)


def _x(f, md, approvers=(APPROVER,), approval="comment"):
    c = f.client()
    rows = comments.fetch(c, KEY)
    return strict.xcheck(md, rows, list(approvers), approvals.excluded(c, rows, me=ME_ID), ME_ID, approval)


def _posted(f, lines=LINES):
    md = decisions.render_file(KEY) + lines
    todo = decisions.unposted(decisions.parse_decisions(md))
    post = f.add_comment(KEY, decisions.body(todo)[1])
    return decisions.stamp_posted(md, {d["id"]: decisions.marker_hash(d) for d in todo}, post), post


def _forge(md, d_id, status, via, note=""):
    """The agent hand-writes a synced-looking line."""
    return decisions.resolve(md, d_id, status, decisions.SYNC_BY, note, False, via)


def test_clean_story_passes():
    f = _fake()
    md, _, _ = _synced(f)
    assert _x(f, md) == [] and _x(f, md) == []  # idempotent
    f2 = _fake()
    open_md, _ = _posted(f2)
    assert _x(f2, open_md) == []  # posted and still open: nothing to cross-check yet (the strict floor blocks open lines)
    assert _x(_fake(), "") == []  # no decisions at all


def test_a_later_reject_and_a_reposted_line_stay_clean():
    f = _fake()
    md, _, _ = _synced(f, votes=("/approve D1", "/reject D1: changed my mind", "/approve D2"))
    assert "D1: Use cursor paging → rejected" in md and _x(f, md) == []
    f2 = _fake()
    md2, _ = _posted(f2)
    _say(f2, "/approve D1")
    reworded = md2.replace("Use cursor paging", "Use offset paging")
    d1 = decisions.parse_decisions(reworded)["open"][0]
    post2 = f2.add_comment(KEY, decisions.body([d1])[1])  # the kit re-posts the new words; the approver answers them
    reworded = decisions.stamp_posted(reworded, {"D1": decisions.marker_hash(d1)}, post2)
    _say(f2, "/approve D1")
    c = f2.client()
    rows = comments.fetch(c, KEY)
    synced, rep = decisions.sync(reworded, rows, [APPROVER], approvals.excluded(c, rows, me=ME_ID), ME_ID)
    assert rep["applied"] == ["D1"] and _x(f2, synced) == []


def test_forged_posting_marker():
    """A synced line whose `posted:` names no comment, or a comment that is not the kit's posting."""
    f = _fake()
    md, post, ids = _synced(f)
    h = decisions.parse_decisions(md)["validated"][0]["posted"]["hash"]
    assert details(_x(f, md.replace("posted: %s:%s" % (post, h), "posted: 99:%s" % h, 1))) == [
        "D1: the kit's posting no longer backs the verdict (posting-missing)"]
    assert details(_x(f, md.replace("posted: %s:%s" % (post, h), "posted: %s:%s" % (ids[1], h), 1))) == [
        "D1: the kit's posting no longer backs the verdict (posting-mismatch)"]  # an approver's comment is no posting
    assert details(_x(f, md.replace("posted: %s:%s" % (post, h), "posted: %s:%s" % (post, "0" * 8 if h != "0" * 8 else "1" * 8), 1))) == [
        "D1: the kit's posting no longer backs the verdict (edited)"]


def test_deleted_line_and_deleted_file():
    f = _fake()
    md, _ = _posted(f)
    gone = "".join(ln for ln in md.splitlines(keepends=True) if not ln.startswith("- [ ] D2"))
    out = _x(f, gone)
    assert details(out) == ["D2: a decision the kit posted is missing from the decisions file"]
    assert rules(_x(f, "")) == ["xcheck-dropped", "xcheck-dropped"]
    assert _x(f, gone, approval="terminal") == []  # terminal mode: a reviewed commit is the gate, no dropped check


def test_hand_written_verdict_with_or_without_via():
    f = _fake()
    md, post = _posted(f)
    vote = _say(f, "/approve D1")
    assert details(_x(f, decisions.resolve(md, "D1", "validated", "PO", "agreed"))) == [
        'D1: a verdict with no approver comment behind it (the target branch sets approval "comment")']
    assert details(_x(f, _forge(md, "D1", "validated", "777:%s" % _pin(f, vote).split(":")[1]))) == ["approval comment 777 is missing"]
    assert details(_x(f, _forge(md, "D1", "validated", _pin(f, post)))) == [
        "approval comment %s no longer counts (excluded-author)" % post]  # the kit's own posting
    assert _x(f, _forge(md, "D1", "validated", _pin(f, vote))) == []  # a real approver comment behind it passes
    unposted = decisions.render_file(KEY) + LINES
    assert details(_x(f, _forge(unposted, "D1", "validated", _pin(f, vote)))) == [
        "D1: the kit's posting no longer backs the verdict (not-posted)"]  # never shown to the PO


def test_via_pointing_at_the_agents_own_or_a_strangers_or_a_quoted_approve():
    f = _fake()
    md, _ = _posted(f)
    for who, code in ((ME_ID, "excluded-author"), (OTHER, "not-approver")):
        cid = _say(f, "/approve D1", who=who)
        assert details(_x(f, _forge(md, "D1", "validated", _pin(f, cid)))) == ["approval comment %s no longer counts (%s)" % (cid, code)]
    quoted = _say(f, "> /approve D1")
    assert details(_x(f, _forge(md, "D1", "validated", _pin(f, quoted)))) == ["approval comment %s no longer counts (quoted)" % quoted]
    app = f.add_comment(KEY, adf.md_to_adf("/approve D1"), author=f.user(APPROVER, account_type="app"))
    assert details(_x(f, _forge(md, "D1", "validated", _pin(f, app)))) == ["approval comment %s no longer counts (app-account)" % app]


def test_verdict_older_than_its_posting():
    f = _fake()
    early = _say(f, "/approve D1")
    md, _ = _posted(f)
    assert details(_x(f, _forge(md, "D1", "validated", _pin(f, early)))) == [
        "D1: the verdict is not what --sync would write now (older-than-posting)"]


def test_verdict_must_be_the_newest_valid_command_with_the_matching_verb():
    f = _fake()
    md, _, _ = _synced(f)
    _say(f, "/reject D1", who=ME_ID)
    assert _x(f, md) == []  # the agent's later reject does not count
    _say(f, "/reject D1")
    assert rules(_x(f, md)) == ["xcheck-stale"]


def test_a_via_taken_off_a_synced_line_is_a_hand_verdict():
    """CHANGED from the fork (`dropped` + `verdict`): without `via` the line is a human's; with approval "comment" it refuses."""
    f = _fake()
    md, _, ids = _synced(f)
    stripped = md.replace(" · via: " + _pin(f, ids[0]), "")
    assert stripped != md and details(_x(f, stripped)) == [
        'D1: a verdict with no approver comment behind it (the target branch sets approval "comment")']


def test_a_line_set_back_to_an_older_posting():
    f = _fake()
    md, post, _ = _synced(f)
    d1 = decisions.parse_decisions(md.replace("Use cursor paging", "Use offset paging"))["validated"][0]
    f.add_comment(KEY, decisions.body([dict(d1, status="open")])[1])
    assert details(_x(f, md)) == ["D1: the kit's posting no longer backs the verdict (posting-superseded)"]


def test_posting_comment_edited_or_lossy():
    f = _fake()
    md, post, _ = _synced(f)
    f.edit_comment(post, f.comments[post]["body"], editor=f.user(OTHER))
    assert set(details(_x(f, md))) == {"D%d: the kit's posting no longer backs the verdict (posting-edited)" % n for n in (1, 2)}
    f2 = _fake()
    lossy = "- [ ] D1: Mail dana@example.com about paging → _(open)_ · kind: decision · why: w\n"
    md2, _ = _posted(f2, lossy)
    vote = _say(f2, "/approve D1")
    assert details(_x(f2, _forge(md2, "D1", "validated", _pin(f2, vote)))) == [
        "D1: the kit's posting no longer backs the verdict (posting-lossy)"]


def test_an_unreadable_own_comment_newer_than_the_posting_supersedes_it():
    f = _fake()
    md, _, _ = _synced(f)
    f.add_comment(KEY, {"type": "doc", "version": 1, "content": [{"type": "mediaSingle", "content": []}]})  # an own unreadable comment
    assert set(details(_x(f, md))) == {"D%d: the kit's posting no longer backs the verdict (posting-superseded)" % n for n in (1, 2)}


def test_a_file_that_cannot_be_read_line_by_line_is_malformed():
    f = _fake()
    md, _, _ = _synced(f)
    for bad in (md + LINES, md + "- [ ] D3: broken line\n", md + "<!-- never closed\n",
                md + "- [ ] D٣: x → _(open)_ · kind: decision · why: w\n", None):
        assert rules(_x(f, bad)) == ["xcheck-malformed"], bad


def test_done_not_run_without_approvers(world, capsys, monkeypatch):
    """No approvers on the target branch: the cross-check is never called and no comment is read."""
    root, set_ = world
    monkeypatch.setattr(strict, "xcheck_run", lambda *a, **k: pytest.fail("the cross-check ran without approvers"))
    f = _fake()
    md, _, _ = _synced(f)
    (root / "dod" / "Decisions_SPK-1.md").write_text(md, encoding="utf-8")
    set_({"site": "example-site.atlassian.net", "project": "SPK"}, {"site": "example-site.atlassian.net", "project": "SPK"})
    f.log.clear()
    assert m.main(["done", KEY], client_factory=f.client) == 0
    assert not any("/comment" in e["path"] or "myself" in e["path"] for e in f.log)
    capsys.readouterr()


def test_done_strict_unreachable_fails_closed(world, capsys):
    import json
    root, set_ = world
    f = _fake()
    md, _, _ = _synced(f)
    (root / "dod" / "Decisions_SPK-1.md").write_text(md, encoding="utf-8")
    base = {"site": "example-site.atlassian.net", "project": "SPK"}
    set_({**base, "approvers": [APPROVER], "approval": "comment", "mode": "strict"}, base)
    for _ in range(4):
        f.inject(503, body={"errorMessages": ["SEKRET-sentinel"], "errors": {}})
    assert m.main(["done", KEY, "--apply"], client_factory=f.client) == 2
    cap = capsys.readouterr()
    assert rules(json.loads(cap.out)["findings"]) == ["xcheck-unreachable"] and "SEKRET" not in cap.out + cap.err and f.writes() == []
