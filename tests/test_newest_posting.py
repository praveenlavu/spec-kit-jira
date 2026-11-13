"""A2: the ONE "newest posting" rule `decisions --sync` reads (rebased from the fork's tests/test_newest_posting.py).

An approval counts only for the line of the NEWEST kit posting that lists that D id (the token user's own `[skj:decisions <hash8>]`
comment, W5), only when that posting is unedited and shows the line as the file holds it now, and only when the approval is newer than
it. The fork ran each case through three readers (sync, the `wait-po-validation` gate, the done-time cross-check); the gate is A4 and
the cross-check is A3, so only the sync column is kept here (A3/A4 add theirs over the same `decisions.posting_index`). The fork's
"modified_by" edits are Jira's `updated` / `updateAuthor`; its "versions" do not exist (Jira comments here are append-only).
All data is fictional; the fake Jira only.
"""
import signal
import time

import pytest

from fakejira import ME_ID, OTHER_ID, FakeJira
from skj import adf, comments, decisions

KEY = "SPK-1"
APPROVER = "5b10ac8d82e05b22cc7d4ef5"  # invented
X = "- [ ] D1: Use cursor paging → _(open)_ · kind: decision · why: the story names no page size"
Y = X.replace("cursor paging", "offset paging")
LONG = "- [ ] D1: " + "x" * 600 + " → _(open)_ · kind: decision · why: w"


def _file(body):
    return decisions.render_file(KEY) + body + "\n"


@pytest.fixture
def fake():
    f = FakeJira()
    assert f.add_issue("A fictional story") == KEY
    return f


def _post(fake, line, author=None):
    """The kit posts `line`; (the file text stamped with that posting, the comment id)."""
    md = _file(line)
    todo = decisions.unposted(decisions.parse_decisions(md))
    cid = fake.add_comment(KEY, decisions.body(todo)[1], author=author)
    return decisions.stamp_posted(md, {d["id"]: decisions.marker_hash(d) for d in todo}, cid), cid


def _vote(fake, text="/approve D1", who=APPROVER):
    return fake.add_comment(KEY, adf.md_to_adf(text), author=fake.user(who))


def _rows(fake):
    return comments.fetch(fake.client(), KEY)


def _sync(fake, md):
    return decisions.sync(md, _rows(fake), [APPROVER], {ME_ID}, ME_ID)


def _verdict(fake, md):
    new, rep = _sync(fake, md)
    got = rep["skipped"].get("D1", "applied")
    assert (got == "applied") == (new != md)  # a skip writes nothing
    return got


def _edit(fake, cid, who=None):
    fake.edit_comment(cid, fake.comments[cid]["body"], editor=who or fake._me())


# ---- the fork's matrix, sync column ------------------------------------------------------------------------------------------

def test_unchanged(fake):
    md, _ = _post(fake, X)
    _vote(fake)
    assert _verdict(fake, md) == "applied"


def test_reworded_repost_then_approve_the_new_text(fake):
    _post(fake, X)
    md, _ = _post(fake, Y)
    _vote(fake)
    assert _verdict(fake, md) == "applied"


def test_reworded_repost_then_revert_is_not_validated(fake):
    md, _ = _post(fake, X)
    _post(fake, Y)
    _vote(fake)
    new, rep = _sync(fake, md)
    assert rep["applied"] == [] and rep["skipped"] == {"D1": "posting-superseded"} and new == md
    assert "posting-superseded" in decisions.SYNC_RULES


def test_old_text_under_the_newest_marker(fake):
    md, c10 = _post(fake, X)
    _, c20 = _post(fake, Y)
    _vote(fake)
    assert _verdict(fake, md.replace("posted: %s:" % c10, "posted: %s:" % c20)) == "posting-mismatch"


def test_edited_posting(fake):
    md, c = _post(fake, X)
    _edit(fake, c, fake.user(OTHER_ID))
    _vote(fake)
    assert _verdict(fake, md) == "posting-edited"
    md, c = _post(fake, X)
    _edit(fake, c)  # an own edit counts the same: the agent is the token user
    _vote(fake)
    assert _verdict(fake, md) == "posting-edited"


def test_repost_edited_then_revert(fake):
    """An edited newer posting still counts as the newest: it supersedes the older one."""
    md, _ = _post(fake, X)
    _, c20 = _post(fake, Y)
    _edit(fake, c20)
    _vote(fake)
    assert _verdict(fake, md) == "posting-superseded"


def test_cut_line(fake):
    md, _ = _post(fake, LONG)
    _vote(fake)
    assert _verdict(fake, md) == "posting-lossy"


def test_missing_posting(fake):
    md, c = _post(fake, X)
    _vote(fake)
    assert _verdict(fake, md.replace("posted: %s:" % c, "posted: 99999:")) == "posting-missing"


# ---- the A1 must-enforce cases and the Jira-only ones ---------------------------------------------------------------------------

def test_an_approval_older_than_the_newest_posting_does_not_count(fake):
    _vote(fake)
    md, _ = _post(fake, X)
    assert _verdict(fake, md) == "older-than-posting"


def test_a_posting_by_another_account_is_not_a_posting(fake):
    """A marker comment by another author is ignored (listed), neither as the posting nor as a newer one."""
    md, c = _post(fake, X, author=fake.user(OTHER_ID))
    _vote(fake)
    new, rep = _sync(fake, md)
    assert rep["skipped"] == {"D1": "posting-mismatch"} and rep["foreign_postings"] == [c]
    md, _ = _post(fake, X)
    _post(fake, Y, author=fake.user(OTHER_ID))  # a forged newer posting of another text does not supersede the kit's
    _vote(fake)
    assert _verdict(fake, md) == "applied"


def test_a_restricted_posting_does_not_count(fake):
    md, c = _post(fake, X)
    fake.comments[c]["visibility"] = {"type": "role", "value": "Developers"}
    _vote(fake)
    assert _verdict(fake, md) == "posting-restricted"


def test_an_own_comment_the_kit_cannot_read_newer_than_the_posting_supersedes_it(fake):
    """Fail closed: an own unreadable comment could be a newer posting of another text (it is never assumed to be harmless)."""
    md, _ = _post(fake, X)
    fake.add_comment(KEY, {"version": 1, "type": "doc", "content": [{"type": "mysteryNode"}]})
    _vote(fake)
    assert _verdict(fake, md) == "posting-superseded"


def test_an_own_marker_comment_that_is_not_the_kit_layout_supersedes_too(fake):
    md, _ = _post(fake, X)
    for body in ("[skj:decisions 00000000]\n\nD1 (decision): Use offset paging",  # the hash is not its own
                 None):
        if body is None:  # the right hash, the wrong layout
            rest = "Something else:\n\n- D1 (decision): Use offset paging"
            body = "[skj:decisions %s]\n\n%s" % (decisions._q()._hash8(rest), rest)
        f2 = FakeJira()
        f2.add_issue("A fictional story")
        md2, _ = _post(f2, X)
        f2.add_comment(KEY, adf.md_to_adf(body))
        _vote(f2)
        assert _verdict(f2, md2) == "posting-superseded", body
    _vote(fake)
    assert _verdict(fake, md) == "applied"  # a kit comment of another kind (questions) is not a decisions posting
    fake.add_comment(KEY, adf.md_to_adf("[skj:questions 00000000]\n\nx"))
    _vote(fake)
    assert _verdict(fake, md) == "applied"


def test_the_named_posting_must_be_the_kit_layout(fake):
    rest = "Something else:\n\n- D1 (decision): Use cursor paging — why: the story names no page size"
    c = fake.add_comment(KEY, adf.md_to_adf("[skj:decisions %s]\n\n%s" % (decisions._q()._hash8(rest), rest)))
    md = decisions.stamp_posted(_file(X), {"D1": decisions.marker_hash(decisions.parse_decisions(_file(X))["open"][0])}, c)
    _vote(fake)
    assert _verdict(fake, md) == "posting-markup"


# ---- the index ---------------------------------------------------------------------------------------------------------------

def _reference_superseded(d, rows, me):
    """The per-line reading the index replaces (the fork's first version): re-parse every own marked comment for this one id."""
    from skj import approvals
    best, key, shown = None, None, ()
    for r in rows:
        k = approvals._order(r)
        if k is None or r.author != me or (key is not None and k <= key) or not r.md.startswith("[skj:decisions "):
            continue
        lines = tuple(ln[2:] for ln in r.md.split("\n") if ln.startswith("- %s (" % d["id"]))
        if lines:
            best, key, shown = r, k, lines
    return bool(d["posted"] and best is not None and (d["posted"]["comment_id"] != best.id or decisions._comment_line(d)[0] not in shown))


def test_index_gives_the_same_answers_as_the_per_line_reading(fake):
    z = X.replace("cursor paging", "page cursors")
    two = "- [ ] D2: Cap at 50 rows → _(open)_ · kind: assumption · why: the story is silent"
    m10, _ = _post(fake, X)
    m20, _ = _post(fake, Y)
    m25, _ = _post(fake, X + "\n" + two)
    m30, c30 = _post(fake, z)
    _edit(fake, c30)
    _vote(fake)
    _post(fake, Y, author=fake.user(OTHER_ID))
    rows = _rows(fake)
    index = decisions.posting_index(rows, ME_ID)
    for md in (m10, m20, m25, m30, m10.replace("posted: 10", "posted: 99"), _file(X)):
        for k in ("open", "validated", "rejected"):
            for d in decisions.parse_decisions(md)[k]:
                assert decisions.superseded(d, index) == _reference_superseded(d, rows, ME_ID), (md, d["id"])


def test_cost_is_linear_in_comment_bytes_not_decisions_times_comments(fake, monkeypatch):
    calls, real = [], decisions._posted_items
    monkeypatch.setattr(decisions, "_posted_items", lambda *a, **k: calls.append(1) or real(*a, **k))
    lines = ["- [ ] D%d: Item %d → _(open)_ · kind: decision · why: w" % (n, n) for n in range(1, 31)]
    md, _ = _post(fake, "\n".join(lines))
    junk = "[skj:decisions 00000000]\n\n" + "\n".join("not a decision line %d " % i + "x" * 90 for i in range(2000))
    for _ in range(20):
        fake.add_comment(KEY, adf.md_to_adf(junk), author=fake.user(APPROVER))
    rows = _rows(fake)
    t = time.monotonic()
    new, rep = decisions.sync(md, rows, [APPROVER], {ME_ID}, ME_ID)
    assert rep["applied"] == [] and len(calls) < 10 * len(rows), (len(calls), len(rows))
    assert time.monotonic() - t < 60  # last-resort sanity bound only; the call count is the real check


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs signal.setitimer")
def test_the_index_is_linear_on_a_huge_posting():
    f = FakeJira()
    f.add_issue("A fictional story")
    ds = [{"id": "D%d" % i, "kind": "decision", "text": "t%d" % i, "why": "w", "status": "open", "by": "", "note": "", "posted": None,
           "used": ""} for i in range(1, 200)]
    for _ in range(30):
        f.add_comment(KEY, decisions.body(ds)[1])
    rows = [comments._row(f._render_comment(c)) for c in f.comments.values()]

    class _Slow(Exception):
        pass

    def alarm(*a):
        raise _Slow()
    # CPU time, not the wall clock (H1b): load slows the clock, not the work; the timed calls run no subprocess and never block
    old = signal.signal(signal.SIGPROF, alarm)
    signal.setitimer(signal.ITIMER_PROF, 2.0)
    try:
        index = decisions.posting_index(rows, ME_ID)
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, old)
    assert len(index.best) == 199


def test_a_named_posting_whose_marker_hash_is_not_its_own_is_not_the_kit_layout(fake):
    """The exact W5 layout under a marker whose hash8 does not match the body: not a kit-composed comment (`posting-markup`)."""
    md = _file(X)
    d = decisions.parse_decisions(md)["open"][0]
    real_md = decisions.body([d])[0]
    c = fake.add_comment(KEY, adf.md_to_adf("[skj:decisions 00000000]" + real_md[len("[skj:decisions 00000000]"):]))
    assert adf.adf_to_md(fake.comments[c]["body"], strict=True).split("\n\n", 1)[1] == real_md.split("\n\n", 1)[1]
    md = decisions.stamp_posted(md, {"D1": decisions.marker_hash(d)}, c)
    _vote(fake)
    assert _verdict(fake, md) == "posting-markup"


def test_an_own_comment_with_an_odd_account_type_is_opaque_not_foreign(fake):
    """MINOR 4: the token user's id with an accountType other than atlassian cannot be read as the kit: fail closed (opaque)."""
    md, _ = _post(fake, X)
    odd = fake.add_comment(KEY, decisions.body(decisions.parse_decisions(_file(Y))["open"])[1])
    fake.comments[odd]["author"] = dict(fake.comments[odd]["author"], accountType="app")
    fake.comments[odd]["updateAuthor"] = fake.comments[odd]["author"]
    _vote(fake)
    new, rep = _sync(fake, md)
    assert rep["skipped"]["D1"] == "posting-superseded" and rep["foreign_postings"] == []


def test_a_named_own_posting_with_an_odd_account_type_never_counts(fake):
    md, c = _post(fake, X)
    fake.comments[c]["author"] = dict(fake.comments[c]["author"], accountType="app")
    fake.comments[c]["updateAuthor"] = fake.comments[c]["author"]
    _vote(fake)
    assert _verdict(fake, md) == "posting-mismatch"
