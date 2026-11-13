"""A3: the done-time cross-check (EXPERIMENTAL comment approval, tamper-evident only; owner decision O5). Before a story is closed
(`skj done`) the kit re-reads every pinned approval (`via: <comment id>:<hash8>`) from Jira and refuses when it is no longer what was
synced. The scenario is the owner's: the agent's token can edit (and delete) every comment (EDIT_ALL_COMMENTS). Against the fake Jira
only (no socket); all ids and accounts are invented."""
import json
import signal
import time

import pytest

import skj.__main__ as m
from skj import adf, approvals, comments, decisions, strict
from skj.config import Config
from fakejira import ME_ID, FakeJira

SITE = "example-site.atlassian.net"
KEY = "SPK-1"
APPROVER = "5b10ac8d82e05b22cc7d4ef5"  # invented accountIds
OTHER = "5c20bd9e93f16c33dd8e5fa6"
CFG = Config(SITE, "SPK")
LINES = ("- [ ] D1: Use cursor paging → _(open)_ · kind: decision · why: the story names no page size\n"
         "- [ ] D2: Cap at 50 rows → _(open)_ · kind: assumption · why: the story is silent\n")


def _fake(key=KEY):
    f = FakeJira()
    if key != KEY:
        f._ids["number"] = int(key.split("-")[1]) - 1
    assert f.add_issue("A fictional story", status="In Progress") == key
    return f


def _say(f, text, who=APPROVER, key=KEY):
    return f.add_comment(key, adf.md_to_adf(text), author=f.user(who))


def _synced(f, votes=("/approve D1", "/reject D2: too small"), key=KEY, lines=LINES, approvers=(APPROVER,)):
    """(the decisions file after the kit posted it and `decisions --sync` ran, the posting id, the vote ids). The log is cleared."""
    md = decisions.render_file(key) + lines
    todo = decisions.unposted(decisions.parse_decisions(md))
    post = f.add_comment(key, decisions.body(todo)[1])
    md = decisions.stamp_posted(md, {d["id"]: decisions.marker_hash(d) for d in todo}, post)
    ids = [_say(f, t, key=key) for t in votes]
    c = f.client()
    rows = comments.fetch(c, key)
    md, rep = decisions.sync(md, rows, list(approvers), approvals.excluded(c, rows, me=ME_ID), ME_ID)
    assert len(rep["applied"]) == len({v.split()[1].rstrip(":") for v in votes}), rep  # one verdict per target
    f.log.clear()
    return md, post, ids


def run(f, md, approvers=(APPROVER,), approval="comment", key=KEY):
    return strict.xcheck_run(f.client, CFG, key, md, list(approvers), approval)


def rules(findings):
    return [x["rule"] for x in findings]


def details(findings):
    return [x["detail"] for x in findings]


def _put(f, cid, text, key=KEY):
    """The token user edits a comment through the REST PUT (the agent holds EDIT_ALL_COMMENTS)."""
    f.client().request("PUT", "/issue/%s/comment/%s" % (key, cid), body={"body": adf.md_to_adf(text)})


def _delete(f, cid, key=KEY):
    f.client().request("DELETE", "/issue/%s/comment/%s" % (key, cid))


def _pin(f, cid):
    return "%s:%s" % (cid, adf.digest(f.comments[cid]["body"])[:8])


def _only_gets(f):
    assert f.log and all(e["method"] == "GET" for e in f.log) and f.writes() == []


def _clean_text(findings, f):
    text = json.dumps(findings)
    for s in (APPROVER, OTHER, ME_ID, f.email, f.token, APPROVER[:8], "Fictional", "cursor paging", "too small"):
        assert s not in text, s


# ---- the five required tests (A3-AC1 .. A3-AC5) ------------------------------------------------------------------------------

def test_unedited_passes():
    """A3-AC1: same hash and `updated == created`: nothing found; GETs only."""
    f = _fake()
    md, _, ids = _synced(f)
    assert run(f, md) == []
    _only_gets(f)
    assert decisions.parse_decisions(md)["validated"][0]["via"] == _pin(f, ids[0])


def test_edited_refused():
    """A3-AC2: an approval edited after the sync (the fake's PUT, by the token user) refuses with exactly the fixed text."""
    f = _fake()
    md, _, ids = _synced(f)
    _put(f, ids[0], "/approve D1")  # the same words: still an edit
    f.log.clear()
    out = run(f, md)
    assert rules(out) == ["xcheck-edited"] and details(out) == ["approval comment %s was edited since sync" % ids[0]]
    _only_gets(f)
    _clean_text(out, f)
    _put(f, ids[1], "/reject D2: something else")  # other words, pin not rewritten: still `edited` (checked before the pin)
    assert details(run(f, md))[1] == "approval comment %s was edited since sync" % ids[1]


def test_deleted_refused():
    """A3-AC3: a deleted approval is missing (a comment the complete list does not show)."""
    f = _fake()
    md, _, ids = _synced(f)
    _delete(f, ids[1])
    f.log.clear()
    out = run(f, md)
    assert rules(out) == ["xcheck-missing"] and details(out) == ["approval comment %s is missing" % ids[1]]
    _only_gets(f)


def test_wrong_pin_refused():
    """A3-AC4: a pin whose hash8 is not the comment body's refuses; every one of the 8 hex counts; an id-only `via` has no hash."""
    f = _fake()
    md, _, ids = _synced(f)
    pin = _pin(f, ids[0])
    h8 = pin.split(":")[1]
    for bad in ("0" * 8 if h8 != "0" * 8 else "1" * 8, h8[:7] + ("0" if h8[7] != "0" else "1"), ("0" if h8[0] != "0" else "1") + h8[1:]):
        out = run(f, md.replace(pin, "%s:%s" % (ids[0], bad)))
        assert details(out) == ["approval comment %s does not match its pin" % ids[0]] and rules(out) == ["xcheck-pin"], bad
    assert rules(run(f, md.replace(pin, ids[0]))) == ["xcheck-pin"]  # the L1 id-only form: re-sync writes the pin


def test_forged_pin_does_not_rescue_edited_comment():
    """A3-AC5: the agent edits the approval AND re-pins the working-tree line to the edited body: still refused, because
    `updated != created` is checked first (the pin is a second check, not the first)."""
    f = _fake()
    md, _, ids = _synced(f, votes=("/reject D1: no", "/reject D2: too small"))
    old = _pin(f, ids[0])
    _put(f, ids[0], "/approve D1")
    forged = md.replace(old, _pin(f, ids[0])).replace("D1: Use cursor paging → rejected · by: approver · note: no",
                                                      "D1: Use cursor paging → validated · by: approver")
    assert forged != md and decisions.parse_decisions(forged)["validated"][0]["via"] == _pin(f, ids[0])
    out = run(f, forged)
    assert details(out) == ["approval comment %s was edited since sync" % ids[0]]


# ---- MUST-ENFORCE (b): the kit's posting is still there, the token user's, unedited, and shows the ledger line NOW -------------

def test_posting_edited_after_the_approval_refuses():
    f = _fake()
    md, post, _ = _synced(f)
    f.edit_comment(post, f.comments[post]["body"], editor=f._me())  # an own edit, even to the same body
    out = run(f, md)
    assert details(out) == ["D1: the kit's posting no longer backs the verdict (posting-edited)",
                            "D2: the kit's posting no longer backs the verdict (posting-edited)"]
    assert set(rules(out)) == {"xcheck-posting"}


def test_posting_deleted_is_missing():
    """No posting left: `posting-missing`. Residual (owner escalation, A2): the agent deleting its OWN NEWER re-posting while the
    old one stays cannot be seen: Jira keeps no trace of a deleted comment."""
    f = _fake()
    md, post, _ = _synced(f)
    _delete(f, post)
    assert details(run(f, md)) == ["D1: the kit's posting no longer backs the verdict (posting-missing)",
                                   "D2: the kit's posting no longer backs the verdict (posting-missing)"]


def test_posting_line_must_equal_the_ledger_line_now():
    """A1 must-enforce 3: the pin binds the approval comment only; the D1 text is bound by the posting, re-checked at done."""
    f = _fake()
    md, _, _ = _synced(f)
    reworded = md.replace("Use cursor paging", "Use offset paging")  # the marker hash no longer matches the line
    assert details(run(f, reworded))[0] == "D1: the kit's posting no longer backs the verdict (edited)"
    d = decisions.parse_decisions(reworded)["validated"][0]
    forged = reworded.replace(":" + d["posted"]["hash"], ":" + decisions.marker_hash(d))  # the agent recomputes the marker
    assert details(run(f, forged)) == ["D1: the kit's posting no longer backs the verdict (posting-mismatch)"]


def test_a_newer_reworded_posting_supersedes_the_approval():
    f = _fake()
    md, post, _ = _synced(f)
    d1 = decisions.parse_decisions(md.replace("Use cursor paging", "Use offset paging"))["validated"][0]
    f.add_comment(KEY, decisions.body([dict(d1, status="open")])[1])  # the kit posts other words for D1 later
    assert details(run(f, md)) == ["D1: the kit's posting no longer backs the verdict (posting-superseded)"]


# ---- (a): the approval still passes the guard on the TARGET branch's approvers ----------------------------------------------

def test_approver_removed_from_the_target_approvers_refuses():
    f = _fake()
    md, _, ids = _synced(f)
    out = run(f, md, approvers=(OTHER,))
    assert details(out) == ["approval comment %s no longer counts (not-approver)" % i for i in ids]
    assert set(rules(out)) == {"xcheck-not-valid"}


def test_approval_now_restricted_refuses():
    f = _fake()
    md, _, ids = _synced(f)
    f.comments[ids[0]]["visibility"] = {"type": "role", "value": "Developers"}  # stamps unchanged: a restriction alone
    assert details(run(f, md)) == ["approval comment %s no longer counts (restricted)" % ids[0]]


def test_token_user_in_approvers_never_backs_a_line():
    """The agent's own `/approve D1` with its accountId listed in approvers, and a hand-made pin to it: `excluded-author`."""
    f = _fake()
    md, _, ids = _synced(f)
    own = f.add_comment(KEY, adf.md_to_adf("/approve D2"))
    forged = md.replace(_pin(f, ids[1]), _pin(f, own)).replace("rejected · by: approver · note: too small", "validated · by: approver")
    out = run(f, forged, approvers=(APPROVER, ME_ID))
    assert details(out) == ["approval comment %s no longer counts (excluded-author)" % own]


def test_a_pin_to_another_targets_approval_is_stale():
    f = _fake()
    md, _, ids = _synced(f)
    swapped = md.replace(_pin(f, ids[1]), _pin(f, ids[0])).replace("rejected · by: approver · note: too small", "validated · by: approver")
    assert details(run(f, swapped)) == ["D2: the verdict is not what --sync would write now (not-newest)"]


# ---- (c): the newer-tampered rule and newer valid commands ------------------------------------------------------------------

def test_a_newer_tampered_reject_refuses():
    f = _fake()
    md, _, ids = _synced(f)
    late = _say(f, "/reject D1: changed my mind")
    _put(f, late, "/approve D1")  # the agent turns the approver's newer reject into something else
    # A2 fix round 1: an approver comment edited by ANOTHER account and newer than the winner blocks every target
    assert details(run(f, md)) == ["D%d: the verdict is not what --sync would write now (newer-tampered)" % n for n in (1, 2)]
    f2 = _fake()
    md2, _, _ = _synced(f2)
    late = _say(f2, "/reject D1: changed my mind")
    f2.comments[late]["visibility"] = {"type": "role", "value": "Developers"}  # restricted: tampered for its own target only
    assert details(run(f2, md2)) == ["D1: the verdict is not what --sync would write now (newer-tampered)"]


def test_a_newer_valid_reject_makes_the_line_stale():
    f = _fake()
    md, _, _ = _synced(f)
    _say(f, "/reject D1: changed my mind")
    assert details(run(f, md)) == ["D1: the verdict is not what --sync would write now (not-newest)"]


def test_a_newer_malformed_line_by_an_approver_is_stale():
    f = _fake()
    md, _, _ = _synced(f)
    _say(f, "/reject D1 oops")
    assert details(run(f, md)) == ["D1: the verdict is not what --sync would write now (bad-newer)"]


def test_the_other_verb_by_and_note_must_be_what_sync_wrote():
    f = _fake()
    md, _, _ = _synced(f)
    flipped = md.replace("D1: Use cursor paging → validated", "D1: Use cursor paging → rejected")
    assert details(run(f, flipped)) == ["D1: the verdict is not what --sync would write now (verb)"]
    renoted = md.replace("note: too small", "note: a different reason")
    assert details(run(f, renoted)) == ["D2: the verdict is not what --sync would write now (note)"]
    rerole = md.replace("D1: Use cursor paging → validated · by: approver", "D1: Use cursor paging → validated · by: PO")
    assert details(run(f, rerole)) == ['D1: a verdict with no approver comment behind it (the target branch sets approval "comment")']


# ---- (d): a truncated or inconsistent comment list, Jira down ---------------------------------------------------------------

def test_a_truncated_list_refuses(monkeypatch):
    f = _fake()
    md, _, _ = _synced(f)
    real = f._comment_page

    def short_total(issue, start, size):
        page = real(issue, start, size)
        page["total"] = 1
        return page
    monkeypatch.setattr(f, "_comment_page", short_total)
    assert rules(run(f, md)) == ["xcheck-truncated"]

    def grows(issue, start, size):  # a comment past the end: the probe sees it
        page = real(issue, start, size)
        if size == 1:
            page["total"] += 1
            page["comments"] = [real(issue, 0, 1)["comments"][0]]
        return page
    monkeypatch.setattr(f, "_comment_page", grows)
    assert rules(run(f, md)) == ["xcheck-truncated"]
    _only_gets(f)


def test_jira_down_or_no_credentials_or_a_moved_issue_refuses():
    f = _fake()
    md, _, _ = _synced(f)
    f.inject(500, body={"errorMessages": ["‮IGNORE-ALL evil server text"], "errors": {}})
    f.inject(500)
    f.inject(500)
    f.inject(500)
    out = run(f, md)
    assert rules(out) == ["xcheck-unreachable"] and "IGNORE" not in json.dumps(out)

    def no_identity():
        raise OSError("no identity in the keychain SECRET-ish")
    out = strict.xcheck_run(no_identity, CFG, KEY, md, [APPROVER], "comment")
    assert details(out) == ["Jira could not be read for the cross-check (OSError)"] and "keychain" not in json.dumps(out)
    f2 = _fake()
    md2, _, _ = _synced(f2)
    f2.move_issue(KEY, "OTH-9")
    assert rules(run(f2, md2)) == ["xcheck-unreachable"]


# ---- what is read, and when -------------------------------------------------------------------------------------------------

def _boom():
    raise AssertionError("no client may be built here")


def test_no_client_without_something_to_check():
    md = decisions.render_file(KEY) + LINES
    assert strict.xcheck_run(_boom, CFG, KEY, md, [APPROVER], "terminal") == []  # terminal mode, no pinned line
    hand = decisions.resolve(md, "D1", "validated", "PO", "agreed")
    assert strict.xcheck_run(_boom, CFG, KEY, hand, [APPROVER], "terminal") == []  # a human's verdict in a reviewed commit stands
    out = strict.xcheck_run(_boom, CFG, KEY, hand, [APPROVER], "comment")  # comment mode: a hand verdict refuses before any read
    assert details(out) == ['D1: a verdict with no approver comment behind it (the target branch sets approval "comment")']
    bad = md + "- [ ] D3: broken line\n"
    assert rules(strict.xcheck_run(_boom, CFG, KEY, bad, [APPROVER], "comment")) == ["xcheck-malformed"]


def test_terminal_mode_still_checks_a_pinned_line():
    f = _fake()
    md, _, ids = _synced(f)
    assert run(f, md, approval="terminal") == []
    _put(f, ids[0], "/approve D1")
    assert rules(run(f, md, approval="terminal")) == ["xcheck-edited"]


def test_comment_mode_reads_even_without_pins_and_finds_a_dropped_line():
    f = _fake()
    md, _, _ = _synced(f, votes=("/approve D1",))
    assert run(f, md) == []
    gone = "".join(ln for ln in md.splitlines(keepends=True) if not ln.startswith("- [ ] D2"))
    assert gone != md and details(run(f, gone)) == ["D2: a decision the kit posted is missing from the decisions file"]
    assert rules(run(f, "")) == ["xcheck-dropped", "xcheck-dropped"]  # the whole file deleted


def test_the_pin_cap_is_a_finding_and_reads_nothing():
    f = _fake()
    md, _, ids = _synced(f, votes=("/approve D1",))
    line = next(ln for ln in md.splitlines() if ln.startswith("- [x] D1"))
    extra = "".join(line.replace("D1:", "D%d:" % n) + "\n" for n in range(3, 3 + strict.MAX_PINS))
    out = strict.xcheck_run(_boom, CFG, KEY, md + extra, [APPROVER], "comment")
    assert rules(out) == ["xcheck-too-many"] and str(strict.MAX_PINS) in out[0]["detail"]
    under = "".join(line.replace("D1:", "D%d:" % n) + "\n" for n in range(3, 2 + strict.MAX_PINS))
    assert "xcheck-too-many" not in rules(run(f, md + under))


def test_findings_are_fixed_texts_with_ids_only_in_a_stable_order():
    f = _fake()
    md, post, ids = _synced(f)
    _put(f, ids[1], "/reject D2: too small")
    _delete(f, ids[0])
    f.log.clear()
    out = run(f, md)
    assert details(out) == ["approval comment %s is missing" % ids[0], "approval comment %s was edited since sync" % ids[1]]
    assert run(f, md) == out
    for x in out:
        assert set(x) == {"rule", "detail", "fix"} and x["rule"] in strict.XC_RULES
    _clean_text(out, f)


# ---- (e): approvers and approval come from the TARGET branch; `skj done` end to end ------------------------------------------

def _git(repo, *a):
    import os
    import subprocess
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env).stdout


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A repo whose ledger is ticked; `set(target, head)` commits `target` as origin/HEAD's config and writes `head` to the working tree."""
    from test_done import READY
    for k in ("SKJ_CONFIG", "CI", "GITHUB_ACTIONS", "SKJ_EMAIL", "SKJ_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "dod").mkdir()
    (tmp_path / "dod" / "DoD_SPK-1.md").write_text(READY, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def set_(target, head):
        (tmp_path / "skj.config.json").write_text(json.dumps(target))
        _git(tmp_path, "add", "skj.config.json")
        _git(tmp_path, "commit", "-q", "-m", "target")
        _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
        _git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        (tmp_path / "skj.config.json").write_text(json.dumps(head))
    return tmp_path, set_


BASE = {"site": SITE, "project": "SPK"}
COMMENT = {**BASE, "approvers": [APPROVER], "approval": "comment"}
TERMINAL = {**BASE, "approvers": [APPROVER], "approval": "terminal"}


def _dec(root, md):
    (root / "dod" / "Decisions_SPK-1.md").write_text(md, encoding="utf-8")


def test_working_tree_approval_and_approvers_are_ignored(world):
    root, set_ = world
    f = _fake()
    md, _, _ = _synced(f)
    hand = md.replace("D1: Use cursor paging → validated · by: approver", "D1: Use cursor paging → validated · by: PO")
    _dec(root, hand)
    set_(COMMENT, TERMINAL)  # the target says comment, the working tree terminal: the hand verdict refuses
    xf, _ = m._xcheck(Config.load(), None, root, KEY, f.client)
    assert details(xf) == ['D1: a verdict with no approver comment behind it (the target branch sets approval "comment")']
    set_(TERMINAL, COMMENT)  # and the reverse: the hand verdict stands
    xf, _ = m._xcheck(Config.load(), None, root, KEY, f.client)
    assert xf == []
    set_(BASE, COMMENT)  # the working tree names approvers, the target none: no cross-check at all
    assert m._xcheck(Config.load(), None, root, KEY, _boom) == ([], False)
    set_({**COMMENT, "approvers": [OTHER]}, COMMENT)  # the working tree cannot add back an approver the target removed
    _dec(root, md)
    xf, _ = m._xcheck(Config.load(), None, root, KEY, f.client)
    assert set(rules(xf)) == {"xcheck-not-valid"}


def test_a_base_with_approval_comment_is_the_stricter_mode(world):
    """origin/HEAD and `--base weak` name the same approvers; only weak sets approval "comment": the hand verdict refuses."""
    root, set_ = world
    f = _fake()
    md, _, _ = _synced(f)
    _dec(root, md.replace("D1: Use cursor paging → validated · by: approver", "D1: Use cursor paging → validated · by: PO"))
    set_(TERMINAL, BASE)
    _git(root, "checkout", "-q", "-b", "weak")
    (root / "skj.config.json").write_text(json.dumps(COMMENT))
    _git(root, "commit", "-q", "-am", "weak")
    _git(root, "checkout", "-q", "main")
    (root / "skj.config.json").write_text(json.dumps(BASE))
    assert m._xcheck(Config.load(), None, root, KEY, f.client)[0] == []  # origin/HEAD alone: terminal, the hand verdict stands
    xf, _ = m._xcheck(Config.load(), "weak", root, KEY, f.client)
    assert details(xf) == ['D1: a verdict with no approver comment behind it (the target branch sets approval "comment")']


def test_done_end_to_end_passes_to_the_close_plan_and_refuses_with_no_write(world, capsys):
    root, set_ = world
    f = _fake()
    md, _, ids = _synced(f, votes=("/approve D1", "/approve D2"))
    _dec(root, md)
    set_({**COMMENT, "mode": "strict"}, {**BASE, "mode": "strict"})
    # the cross-check passes; the strict `done` check (a story without spec or review here) then refuses on its own
    rc = m.main(["done", KEY], client_factory=f.client)
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and not any(x["rule"].startswith("xcheck") for x in out["findings"])
    set_(COMMENT, BASE)  # advisory everywhere: the cross-check passes and the close plan is shown
    f.log.clear()
    assert m.main(["done", KEY], client_factory=f.client) == 0
    cap = capsys.readouterr()
    assert "xcheck" not in cap.err and "skj: warning" not in cap.err
    _only_gets(f)
    # the approval is edited: strict anywhere -> exit 2 before any further request (comment mode alone: the fix-round-1 tests below)
    f2 = _fake()
    md2, _, ids2 = _synced(f2, votes=("/approve D1", "/approve D2"))
    _dec(root, md2)
    _put(f2, ids2[0], "/approve D1")
    set_({**COMMENT, "mode": "strict"}, BASE)
    for extra in ([], ["--apply"]):
        f2.log.clear()
        assert m.main(["done", KEY, *extra], client_factory=f2.client) == 2
        cap = capsys.readouterr()
        assert rules(json.loads(cap.out)["findings"]) == ["xcheck-edited"] and "xcheck-edited" in cap.err
        assert f2.writes() == [] and all(e["method"] == "GET" for e in f2.log)


def test_done_apply_closes_when_the_cross_check_passes(world, capsys):
    root, set_ = world
    f = _fake()
    md, _, _ = _synced(f, votes=("/approve D1", "/approve D2"))
    _dec(root, md)
    set_(COMMENT, BASE)
    rc = m.main(["done", KEY, "--apply"], client_factory=f.client)
    capsys.readouterr()
    assert rc == 0 and any(e["method"] == "POST" and e["path"].endswith("/transitions") for e in f.log)


# ---- fix round 1: approval "comment" blocks in ANY mode; only approval "terminal" with no strict side merely warns ------------

def _no_transition(f):
    assert not any(e["method"] == "POST" and e["path"].endswith("/transitions") for e in f.log) and f.writes() == []


def test_comment_mode_advisory_never_closes_on_an_edited_approval(world, capsys):
    """The attack: approval "comment" on the target, no mode anywhere (advisory); the agent PUTs `/approve D1` over the approval."""
    root, set_ = world
    f = _fake()
    md, _, ids = _synced(f)
    _dec(root, md)
    set_(COMMENT, BASE)
    _put(f, ids[0], "/approve D1")
    for extra in ([], ["--apply"]):
        f.log.clear()
        rc = m.main(["done", KEY, *extra], client_factory=f.client)
        cap = capsys.readouterr()
        assert rc == 2 and rules(json.loads(cap.out)["findings"]) == ["xcheck-edited"] and "warning" not in cap.err
        _no_transition(f)


def test_comment_mode_advisory_blocks_when_jira_is_down_or_the_list_is_truncated(world, capsys, monkeypatch):
    root, set_ = world
    f = _fake()
    md, _, _ = _synced(f)
    _dec(root, md)
    set_(COMMENT, BASE)
    for _ in range(4):
        f.inject(503)
    assert m.main(["done", KEY, "--apply"], client_factory=f.client) == 2
    assert rules(json.loads(capsys.readouterr().out)["findings"]) == ["xcheck-unreachable"]
    _no_transition(f)
    real = f._comment_page

    def short_total(issue, start, size):
        page = real(issue, start, size)
        page["total"] = 1
        return page
    monkeypatch.setattr(f, "_comment_page", short_total)
    f._answers.clear()  # any injected answer the retries did not use
    f.log.clear()
    assert m.main(["done", KEY, "--apply"], client_factory=f.client) == 2
    assert rules(json.loads(capsys.readouterr().out)["findings"]) == ["xcheck-truncated"]
    _no_transition(f)


def test_terminal_mode_advisory_only_warns_and_strict_blocks(world, capsys):
    root, set_ = world
    f = _fake()
    md, _, ids = _synced(f)
    _dec(root, md)
    _put(f, ids[0], "/approve D1")
    set_(TERMINAL, BASE)  # approval terminal, no strict side: the finding is a warning, the close goes on
    assert m.main(["done", KEY], client_factory=f.client) == 0
    assert "skj: warning: xcheck-edited: approval comment %s was edited since sync" % ids[0] in capsys.readouterr().err
    assert m._xcheck(Config.load(), None, root, KEY, f.client)[1] is False
    set_({**TERMINAL, "mode": "strict"}, BASE)
    f.log.clear()
    assert m.main(["done", KEY, "--apply"], client_factory=f.client) == 2
    assert rules(json.loads(capsys.readouterr().out)["findings"]) == ["xcheck-edited"]
    _no_transition(f)


# ---- placement: the fork runs the cross-check in `done` only (and its CI close step); `strict check` / `verify` do not ---------

def test_strict_check_and_verify_never_call_the_cross_check(monkeypatch):
    import inspect
    src = inspect.getsource(strict.check) + inspect.getsource(strict.verify)
    assert "xcheck" not in src and not any("xcheck" in r.__name__ for p in strict.PHASES.values() for r in p)
    assert "xcheck-unavailable" not in strict.STUBS and "rules-unavailable" not in strict.STUBS  # RU3 built the rules check


# ---- linear time on adversarial input ---------------------------------------------------------------------------------------
needs_timer = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="signal.setitimer is not available on this platform")


@needs_timer
def test_the_cross_check_is_linear_on_many_pins_and_comments():
    from test_stack_linear import _timed
    f = _fake()
    n = strict.MAX_PINS
    lines = "".join("- [ ] D%d: item %d → _(open)_ · kind: decision · why: w\n" % (i, i) for i in range(1, n + 1))
    md, _, ids = _synced(f, votes=tuple("/approve D%d" % i for i in range(1, n + 1)), lines=lines)
    for i in range(3000):
        _say(f, "/approve D%d" % (i % n + 1) if i % 2 else "noise %d" % i, who=OTHER)
    c = f.client()
    rows = comments.fetch(c, KEY)
    excl = approvals.excluded(c, rows, me=ME_ID)
    t = _timed(lambda: strict.xcheck(md, rows, [APPROVER], excl, ME_ID, "comment"))
    assert t < 2.0 and strict.xcheck(md, rows, [APPROVER], excl, ME_ID, "comment") == []


@needs_timer
def test_the_local_part_is_linear_on_a_hostile_12000_line_file():
    from test_stack_linear import _timed
    line = "- [x] D%d: item → validated · by: approver · via: 10001:abcdef12 · why: w\n"
    md = decisions.render_file(KEY) + "".join(line % i for i in range(1, 12001))
    assert len(md.encode()) < 1 << 20
    t = _timed(lambda: strict.xcheck_run(_boom, CFG, KEY, md, [APPROVER], "comment"))
    assert t < 2.0 and rules(strict.xcheck_run(_boom, CFG, KEY, md, [APPROVER], "comment")) == ["xcheck-too-many"]
    hand = md.replace("by: approver", "by: PO")
    t = _timed(lambda: strict.xcheck_run(_boom, CFG, KEY, hand, [APPROVER], "comment"))
    assert t < 2.0 and len(strict.xcheck_run(_boom, CFG, KEY, hand, [APPROVER], "comment")) == 12000
