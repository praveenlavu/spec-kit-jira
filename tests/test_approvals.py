"""A1: the comment-approval trust guard (seven conditions) and the command grammar (EXPERIMENTAL, tamper-evident only).

Rows are built through the REAL `comments.fetch` from fake-Jira JSON (canned pages or the S9 FakeJira), so the guard is judged on
the Row the product produces; a few hand-built Rows probe the guard's own type checks. Fictional data only: every accountId is
invented. No socket is opened (conftest blocks them).

The grammar cases are the fork's `test_approvals.py` grammar and quote cases with the HTML replaced by ADF (the comment body is
ADF, rendered by `adf.adf_to_md(strict=True)` into `Row.md`). Cases that only exist for an HTML reader (mail-client quote
classes, raw-text elements, bogus close tags, `<!-- -->` parsing) have no ADF counterpart: ADF is a tree, so a quote has no
textual end a parser could misread. `newest`, `require`, `check`, `sync` and the poller cases belong to A2-A4.
"""
import json
import signal
import time

import pytest

from fakejira import ME_ID, OTHER_ID, FakeJira
from skj import adf, approvals, comments
from skj import client as cl

APPROVER = "5b10ac8d82e05b22cc7d4ef5"  # invented
APPROVER2 = "5b10ac8d82e05b22cc7d4ef6"  # invented: a second approver, also a kit posting identity in one test
APP = "5b10ac8d82e05b22cc7d4e00"  # invented: an app account
CI = "5c20bd9e93f16c33dd8e5f06"  # invented: the CI identity
H64 = "ab" * 32
T = "2026-01-05T09:00:00.000+0000"
T2 = "2026-01-05T09:00:01.000+0000"


# --- ADF builders ---------------------------------------------------------------------------------------------------------

def tx(s, *marks):
    n = {"type": "text", "text": s}
    if marks:
        n["marks"] = [{"type": m} for m in marks]
    return n


def br():
    return {"type": "hardBreak"}


def mention(aid, name="Fictional Approver"):
    return {"type": "mention", "attrs": {"id": aid, "text": "@" + name}}


def p(*inl):
    return {"type": "paragraph", "content": [tx(x) if isinstance(x, str) else x for x in inl]}


def quote(*blocks):
    return {"type": "blockquote", "content": list(blocks)}


def bullets(*items):
    return {"type": "bulletList", "content": [{"type": "listItem", "content": [p(i)]} for i in items]}


def ordered(*items):
    return {"type": "orderedList", "content": [{"type": "listItem", "content": [p(i)]} for i in items]}


def heading(s, level=2):
    return {"type": "heading", "attrs": {"level": level}, "content": [tx(s)]}


def code(s, lang="text"):
    return {"type": "codeBlock", "attrs": {"language": lang}, "content": [tx(s)]}


def doc(*blocks):
    return {"version": 1, "type": "doc", "content": [p(b) if isinstance(b, str) else b for b in blocks]}


MEDIA = {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "f00d", "type": "file", "collection": "x"}}]}


def md_of(x):
    return adf.adf_to_md(x, strict=True) if isinstance(x, dict) else x


def _p(x):
    return approvals.parse_command(md_of(x))


def _bad(x):
    return approvals._parse(md_of(x))[1]


def C(verb, target, h=None, text=""):
    return {"verb": verb, "target": target, "hash": h, "text": text}


# --- rows through the real comments.fetch -------------------------------------------------------------------------------------

def user(aid, atype="atlassian"):
    return {"accountId": aid, "accountType": atype, "displayName": "Fictional Approver", "emailAddress": "appr@example.com",
            "avatarUrls": {}, "active": True}


_UNSET = object()


def raw(cid="10001", body=None, author=APPROVER, atype="atlassian", upd=_UNSET, created=T, updated=_UNSET, **over):
    c = {"id": cid, "author": user(author, atype) if author is not None else None,
         "updateAuthor": user(author if upd is _UNSET else upd, atype) if (author if upd is _UNSET else upd) is not None else None,
         "body": doc("/approve D3") if body is None else body, "created": created,
         "updated": created if updated is _UNSET else updated, "jsdPublic": True}
    c.update(over)
    return c


class Canned:
    def __init__(self, pages):
        self.pages, self.n = list(pages), 0

    def __call__(self, method, url, headers, body, timeout):
        page = self.pages[min(self.n, len(self.pages) - 1)]
        self.n += 1
        return 200, {"Content-Type": "application/json"}, json.dumps(page).encode()


def rows(*raws):
    page = {"startAt": 0, "maxResults": 100, "total": len(raws), "comments": list(raws)}
    c = cl.Client("example-site.atlassian.net", "dev.person@example.com", "FAKE-tok", opener=Canned([page]), sleep=lambda s: None)
    return comments.fetch(c, "SPK-1")


def row(**kw):
    return rows(raw(**kw))[0]


def g(r, approvers=(APPROVER,), excl=(ME_ID,)):
    return approvals.guard(r, list(approvers), set(excl))


def good():
    return row()


# --- the clean case -----------------------------------------------------------------------------------------------------------

def test_guard_passes_a_clean_approval_from_real_rows():
    r = good()
    assert g(r) is None and _p(r.md) == C("approve", "D3")
    assert approvals.guard(r, (APPROVER,), frozenset((ME_ID,))) is None  # tuple and frozenset are accepted too
    assert approvals.guard(r, {APPROVER, APPROVER2}, [ME_ID, CI]) is None


def test_reason_codes_are_the_plans_seven():
    assert set(approvals.REASONS) == {"not-approver", "app-account", "excluded-author", "edited", "restricted", "quoted", "unknown-node"}
    assert len(approvals.REASONS) == 7


# --- A1-AC1: one failing-case test per condition ---------------------------------------------------------------------------------

def test_guard_not_approver():
    assert g(row(author=OTHER_ID)) == "not-approver"
    assert g(good(), approvers=()) == "not-approver"  # an EMPTY approvers list means nobody approves
    assert g(good(), approvers=[APPROVER.upper()]) == "not-approver"  # accountIds are case-sensitive: exact match only
    assert g(good(), approvers=[APPROVER[:-1]]) == "not-approver"
    assert g(good(), approvers=[APPROVER + "0"]) == "not-approver"
    # an anonymous or odd author is None in the Row and never an approver, whatever update_author holds
    for author in (None, {"accountType": "atlassian"}, {"accountId": "bad id", "accountType": "atlassian"}, "x", 5):
        r = rows({**raw(), "author": author, "updateAuthor": author})[0]
        assert r.author is None and g(r) == "not-approver", author


def test_guard_author_none_with_update_author_none_is_rejected():
    # R2 note (a): None == None must not read as "unedited by the author"
    r = rows({**raw(), "author": {"accountType": "atlassian"}, "updateAuthor": {"accountType": "atlassian"}})[0]
    assert (r.author, r.update_author, r.author_type) == (None, None, "atlassian")
    assert g(r) == "not-approver"
    hand = good()._replace(author=None, update_author=None)
    assert g(hand) == "not-approver"


def test_guard_app_account():
    assert g(row(author=APP, atype="app"), approvers=[APP]) == "app-account"
    assert g(row(atype="customer")) == "app-account"
    r = rows({**raw(), "author": {"accountId": APPROVER}})[0]  # no accountType at all
    assert r.author_type is None and g(r) == "app-account"
    assert g(good()._replace(author_type="Atlassian")) == "app-account"


def test_guard_excluded_token_user():
    # A1-AC3: the agent's own comment never counts, even when its accountId IS in approvers: the exclusion wins
    mine = row(author=ME_ID)
    assert g(mine, approvers=[ME_ID, APPROVER], excl=[ME_ID]) == "excluded-author"
    assert g(mine, approvers=[APPROVER], excl=[ME_ID]) == "excluded-author"
    # and through FakeJira: excluded() reads /myself, so the token user is excluded without being told
    fake = FakeJira()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, doc("/approve D3"))  # by the token user
    found = comments.fetch(fake.client(), key)
    skip = approvals.excluded(fake.client(), found)
    assert skip == {ME_ID}
    assert approvals.guard(found[0], [ME_ID], skip) == "excluded-author"
    # `doctor` failing this configuration (approvers holding the token user) is A5's row; deferred, see the A1 report


def test_guard_excluded_kit_comment_author():
    kit = raw("10001", body=doc("[skj:questions 0a1b2c3d]", "Open questions for the product owner:"), author=APPROVER2)
    appr = raw("10002", author=APPROVER2)
    found = rows(kit, appr)
    fake = FakeJira()
    skip = approvals.excluded(fake.client(), found)
    assert skip == {ME_ID, APPROVER2}
    assert approvals.guard(found[1], [APPROVER, APPROVER2], skip) == "excluded-author"
    # the CI identity is excluded when given
    assert approvals.excluded(fake.client(), found, ci_identity=CI) == {ME_ID, APPROVER2, CI}
    assert approvals.guard(row(author=CI), [CI], approvals.excluded(fake.client(), [], ci_identity=CI)) == "excluded-author"


def test_excluded_marker_only_at_the_start_and_needs_a_clean_identity():
    found = rows(raw("10001", body=doc("see [skj:questions] above"), author=APPROVER2),
                 raw("10002", body=doc(quote(p("[skj:questions 0a1b2c3d]"))), author=APPROVER2),
                 raw("10003", body=doc("[skj:x]"), author=None))
    assert approvals.excluded(FakeJira().client(), found) == {ME_ID}  # a quoted or mid-text marker is not a kit comment
    for bad in ("", "bad id", 5, None, b"5c20bd9e", "x" * 129):
        if bad is None:
            continue
        with pytest.raises(ValueError):
            approvals.excluded(FakeJira().client(), [], ci_identity=bad)
    with pytest.raises(ValueError):
        approvals.excluded(FakeJira().client(), [{"author": APPROVER, "md": "[skj:x]"}])  # not Rows: refused, never skipped
    with pytest.raises(ValueError):
        approvals.excluded(FakeJira().client(), "10001")


def test_excluded_refuses_when_the_token_user_cannot_be_read():
    fake = FakeJira(account_type="app")  # /myself answers an app account: issues.me refuses it
    with pytest.raises(ValueError) as e:
        approvals.excluded(fake.client(), [])
    assert "nothing counted" in str(e.value)
    fake = FakeJira()
    fake.inject(401)
    with pytest.raises(ValueError):
        approvals.excluded(fake.client(), [])


def test_guard_edited_updated_differs():
    assert g(row(updated=T2)) == "edited"
    # the same instant written another way is still `edited`: created/updated are compared as RAW strings, never parsed
    for other in ("2026-01-05T09:00:00.000Z", "2026-01-05T10:00:00.000+0100", "2026-01-05T09:00:00.000+00:00", "2026-01-05T09:00:00+0000",
                  "2026-01-05T09:00:00.0000+0000"):
        r = row(updated=other)
        assert r.created != r.updated and g(r) == "edited", other
    assert g(good()._replace(created="", updated="")) == "edited"  # empty stamps are not a stamp
    assert g(good()._replace(created=None, updated=None)) == "edited"


def test_guard_edited_update_author_differs():
    assert g(row(upd=OTHER_ID)) == "edited"
    assert g(row(upd=ME_ID)) == "edited"
    r = rows({**raw(), "updateAuthor": None})[0]  # an absent updateAuthor fails closed
    assert r.update_author is None and g(r) == "edited"
    r = rows({k: v for k, v in raw().items() if k != "updateAuthor"})[0]
    assert r.update_author is None and g(r) == "edited"
    assert g(good()._replace(update_author=APPROVER.upper())) == "edited"


def test_guard_restricted_visibility():
    assert g(row(visibility={"type": "role", "value": "Developers"})) == "restricted"
    assert g(row(visibility={"type": "group", "value": "jira-software-users"})) == "restricted"
    for odd in ({"type": "x", "value": "y"}, {}, [], "role", 1, {"type": "role"}, {"type": "role", "value": ""}):
        r = row(visibility=odd)
        assert r.visibility is not None and g(r) == "restricted", odd
    for jsd in (False, None, 0, "true", "false"):  # jsdPublic present and not True: an internal note (unverified live)
        r = row(jsdPublic=jsd)
        assert g(r) == "restricted", jsd
    assert g(good()._replace(visibility="x")) == "restricted"
    assert g(good()._replace(visibility=())) == "restricted"


def test_guard_quoted_command():
    for body in (doc(quote(p("/approve D3"))), doc(quote(quote(p("/approve D3")))), doc(bullets("/approve D3")), doc(ordered("/approve D3")),
                 doc(heading("/approve D3")), doc(code("/approve D3")), doc("Looks good", "/approve D3"), doc(p("Thanks!", br(), "/approve D3")),
                 doc(p(mention(APPROVER), " /approve D3")), doc(p("> /approve D3")), doc(quote(p("x")), "/approve D3"),
                 doc(bullets("x"), "/approve D3"), doc("just chatting"), doc()):
        r = row(body=body)
        assert r.unknown_nodes == () and g(r) == "quoted", r.md
        assert _p(r.md) is None, r.md


def test_guard_unknown_node():
    r = row(body=doc("/approve D3", MEDIA))
    assert r.unknown_nodes and r.md == "" and g(r) == "unknown-node"
    r = row(body={"version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [tx("/approve D3", "underline")]}]})
    assert r.unknown_nodes and g(r) == "unknown-node"
    r = row(body={"version": 1, "type": "doc", "content": [p("/approve D3 /reject D3")]})  # a line break only strict mode refuses
    assert r.unknown_nodes and g(r) == "unknown-node"
    r = row(body="/approve D3")  # not ADF at all
    assert r.unknown_nodes == ("body:not-adf",) and g(r) == "unknown-node"
    # hand-built Rows: a `[?` placeholder, unknown kinds, or a digest that is not one fail closed
    assert g(good()._replace(md="/approve D3 [?mediaSingle]")) == "unknown-node"
    assert g(good()._replace(unknown_nodes=("x",))) == "unknown-node"
    assert g(good()._replace(unknown_nodes=["x"])) == "unknown-node"
    assert g(good()._replace(unknown_nodes=[])) == "unknown-node"  # not the Row's own () shape
    assert g(good()._replace(digest="")) == "unknown-node"
    assert g(good()._replace(digest="AB" * 32)) == "unknown-node"
    assert g(good()._replace(md=b"/approve D3")) == "unknown-node"


# --- A1-AC2: text naming an approver -------------------------------------------------------------------------------------------

def test_text_naming_approver_does_not_count():
    body = doc(p("/approve D3"), p("on behalf of ", mention(APPROVER), " (", APPROVER, ", [~accountid:" + APPROVER + "], Fictional Approver)"))
    r = row(author=OTHER_ID, body=body)
    assert r.author == OTHER_ID and APPROVER in r.md  # the text does name the approver
    assert g(r) == "not-approver"
    assert g(r, approvers=[APPROVER, APPROVER2]) == "not-approver"
    # end to end: another person's comment on FakeJira that names the approver everywhere still does not count
    fake = FakeJira()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, body, author=fake.user(OTHER_ID))
    found = comments.fetch(fake.client(), key)
    assert approvals.guard(found[0], [APPROVER], approvals.excluded(fake.client(), found)) == "not-approver"


# --- order: one deterministic code for a comment failing several conditions ------------------------------------------------------

def test_guard_reports_the_first_failing_condition_in_a_fixed_order():
    worst = good()._replace(author=ME_ID, author_type="app", update_author=OTHER_ID, updated=T2, visibility=("role", "x"),
                            md="> /approve D3 [?x]", unknown_nodes=("x",), digest="")
    # fix one failing condition at a time: the code moves through REASONS in order, one step per fix, then None
    fixes = [("author", OTHER_ID), ("author", APPROVER), ("author_type", "atlassian"), ("update_author", APPROVER), ("updated", T),
             ("visibility", None), ("unknown_nodes", ()), ("digest", good().digest), ("md", "> /approve D3"), ("md", "/approve D3")]
    seen, r = [], worst
    for i in range(len(fixes) + 1):
        seen.append(approvals.guard(r, [ME_ID, APPROVER], {ME_ID}))
        if i < len(fixes):
            r = r._replace(**{fixes[i][0]: fixes[i][1]})
    assert seen == ["excluded-author", "not-approver", "app-account", "edited", "edited", "restricted", "unknown-node", "unknown-node",
                    "unknown-node", "quoted", None]
    assert [c for k, c in enumerate(seen) if c and (k == 0 or seen[k - 1] != c)] == list(approvals.REASONS)
    # the same comment always gets the same code
    assert all(approvals.guard(worst, [ME_ID], {ME_ID}) == "excluded-author" for _ in range(3))
    # not an approver beats every later condition; excluded beats not-approver
    assert g(worst._replace(author=OTHER_ID)) == "not-approver"
    assert g(worst._replace(author=ME_ID), approvers=[APPROVER]) == "excluded-author"


# --- the guard's own inputs are untrusted -----------------------------------------------------------------------------------------

def test_guard_refuses_approvers_or_excluded_it_cannot_trust():
    r = good()
    for bad in (APPROVER, APPROVER.encode(), None, {APPROVER: 1}, 5, iter([APPROVER]), [APPROVER, "bad id"], [APPROVER, ""], [APPROVER, None],
                [APPROVER, 5], [APPROVER, b"x"], [APPROVER, ["x"]], ["x" * 129], [APPROVER + "\n"]):
        with pytest.raises(ValueError):
            approvals.guard(r, bad, {ME_ID})
        with pytest.raises(ValueError):
            approvals.guard(r, [APPROVER], bad)

    class Sly(str):
        def __eq__(self, other):
            return True

        __hash__ = str.__hash__

    with pytest.raises(ValueError):
        approvals.guard(r, [Sly("anything")], {ME_ID})
    for empty in ((), [], set(), frozenset()):  # the token user must always be excluded: an empty set is a caller error
        with pytest.raises(ValueError):
            approvals.guard(r, [APPROVER], empty)


def test_guard_never_crashes_and_never_passes_on_odd_rows():
    base = good()
    junk = (None, 5, 1.5, True, [], ["x"], {}, {"a": 1}, (), ("x",), b"x", "", object())
    for field in comments.Row._fields:
        for v in junk:
            if v == getattr(base, field) and type(v) is type(getattr(base, field)):
                continue  # visibility None and unknown_nodes () are the clean Row's own values
            r = base._replace(**{field: v})
            got = g(r)
            assert got in approvals.REASONS, (field, v, got)
    for not_a_row in (None, {}, dict(base._asdict()), tuple(base), [base], "row", 5):
        assert g(not_a_row) == "not-approver"

    class Sneaky(str):
        def __eq__(self, other):
            return True

        __hash__ = str.__hash__

    assert g(base._replace(author=Sneaky("zzz"))) == "not-approver"
    assert g(base._replace(author_type=Sneaky("x"))) == "app-account"
    assert g(base._replace(update_author=Sneaky("x"))) == "edited"
    assert g(base._replace(updated=Sneaky("x"))) == "edited"


# --- end to end with FakeJira ------------------------------------------------------------------------------------------------------

def _story(body=None, **kw):
    fake = FakeJira()
    key = fake.add_issue("fictional story")
    cid = fake.add_comment(key, doc("/approve D3") if body is None else body, author=fake.user(APPROVER), **kw)
    return fake, key, cid


def _judge(fake, key):
    found = comments.fetch(fake.client(), key)
    return approvals.guard(found[0], [APPROVER], approvals.excluded(fake.client(), found)), found[0]


def test_e2e_clean_approval_counts_and_pins():
    fake, key, cid = _story()
    why, r = _judge(fake, key)
    assert why is None and _p(r.md) == C("approve", "D3")
    assert approvals.pin(r) == "%s:%s" % (cid, adf.digest(doc("/approve D3"))[:8])


def test_e2e_comment_put_edited_by_the_agent_is_edited():
    fake, key, cid = _story()
    resp = fake.client().request("PUT", "/issue/%s/comment/%s" % (key, cid), body={"body": doc("/approve D3: rewritten")})
    assert resp.status == 200
    why, r = _judge(fake, key)
    assert r.update_author == ME_ID and r.updated != r.created and why == "edited"


def test_e2e_comment_edited_by_its_author_is_edited():
    fake, key, cid = _story()
    fake.edit_comment(cid, doc("/approve D3"), editor=fake.user(APPROVER))  # same text, same author: `updated` still moves
    why, r = _judge(fake, key)
    assert r.update_author == APPROVER and why == "edited"


def test_e2e_restricted_quoted_and_unknown_node():
    assert _judge(*_story(visibility={"type": "role", "value": "Developers"})[:2])[0] == "restricted"
    assert _judge(*_story(doc(quote(p("/approve D3")), p("I agree")))[:2])[0] == "quoted"
    assert _judge(*_story(doc("/approve D3", MEDIA))[:2])[0] == "unknown-node"
    fake = FakeJira()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, doc("/approve D3"), author=fake.user(APP, account_type="app"))
    found = comments.fetch(fake.client(), key)
    assert approvals.guard(found[0], [APP], approvals.excluded(fake.client(), found)) == "app-account"


# --- the pin -----------------------------------------------------------------------------------------------------------------------

def test_pin_is_id_and_first_8_hex_of_the_digest():
    r = good()
    assert approvals.pin(r) == "10001:" + r.digest[:8] and approvals.PIN_RE.fullmatch(approvals.pin(r))
    for bad in (r._replace(digest=""), r._replace(digest="AB" * 32), r._replace(id="010"), r._replace(id=10001), r._replace(id="1 0"), None, {}):
        with pytest.raises(ValueError):
            approvals.pin(bad)
    for s in ("10001:abcd1234", "1:00000000"):
        assert approvals.PIN_RE.fullmatch(s)
    for s in ("10001:ABCD1234", "010:abcd1234", "10001:abcd123", "10001:abcd12345", " 10001:abcd1234", "10001:abcd1234\n", "x:abcd1234"):
        assert not approvals.PIN_RE.fullmatch(s), s


# --- A1-AC4: the grammar (the fork's cases, HTML replaced by ADF) ---------------------------------------------------------------------

@pytest.mark.parametrize("body,want", [
    ("/approve D3", C("approve", "D3")),
    (doc("/approve D3"), C("approve", "D3")),
    (doc("/reject D12: too vague"), C("reject", "D12", text="too vague")),
    (doc("/reject D3 :  spaced  note "), C("reject", "D3", text="spaced note")),
    (doc("/answer Q2: Use the blue one"), C("answer", "Q2", text="Use the blue one")),
    (doc("/approve review-spec " + H64), C("approve", "review-spec", H64)),
    (doc("/reject approve-push %s: not yet" % H64), C("reject", "approve-push", H64, "not yet")),
    (doc(p(), p(br()), p(" "), "/approve D3", "thanks"), C("approve", "D3")),  # empty blocks before the command are skipped
    (doc(p("/approve D3", br(), "thanks")), C("approve", "D3")),  # the command is the first LINE; lines after it are fine
    (doc(p("/approve D3"), quote(p("/reject D3"))), C("approve", "D3")),  # a quote AFTER the command line changes nothing
    (doc(p("/reject D3: /approve D3")), C("reject", "D3", text="/approve D3")),  # a command inside the reason is reason text
    (doc(p("/approve D3"), p("/reject D3")), C("approve", "D3")),  # duplicate commands: the first line wins
    (doc(p("/approve ", tx("D3"))), C("approve", "D3")),  # how text is split into nodes does not matter
])
def test_parse_command_accepts_the_grammar(body, want):
    assert _p(body) == want


@pytest.mark.parametrize("md", [
    "", None, 5, b"/approve D3", "approve D3", "/Approve D3", "/approve", "/approve d3", "/approve D", "/approve D3x", "/approve D03", "/approve D0",
    "/answer Q02: x", "/approve D3 extra", "/approve D3:", "/approve D3:   ", "/answer Q2", "/answer Q2:", "/answer D3: yes", "/approve Q2",
    "/reject Q2: no", "/answer review-spec: x", "/approve review-spec", "/approve ab", "/approve a" + "b" * 41, "/approve D3 " + H64,
    "/answer Q2 " + H64 + ": x", "/approve D3", "/approve D3 /approve D4 x", "x/approve D3", "​/approve D3", "/approvals D3",
    "/approve-all D3", "<!-- /approve D3 -->", "/approve review-spec " + H64 + " extra", "/approve D1234567890",
    "／approve D3", "/approve D３", "/approve D٣", "/аpprove D3", "‏/approve D3", "‮/approve D3", "/approve D3\r",
    "/approve D3 /reject D3", "/approve D3", "﻿/approve D3",
])
def test_parse_command_rejects_everything_else(md):
    assert approvals.parse_command(md) is None


@pytest.mark.parametrize("body", [
    doc(p("> /approve D3")), doc(bullets("/approve D3")), doc(ordered("/approve D3")), doc(heading("/approve D3")), doc(code("/approve D3")),
    doc(quote(p("/approve D3"))), doc(p("hello", br(), "/approve D3")), doc(p(mention(APPROVER), " /approve D3")),
    doc(p(tx("/approve D3", "strong"))), doc(p(tx("/approve D3", "code"))),
])
def test_parse_command_rejects_structure_around_the_command(body):
    assert _p(body) is None


def test_hash_must_be_64_hex():
    for bad in (H64[:-1], H64 + "a", H64.upper(), "g" * 64, H64[:-1] + "G", " " + H64, "0x" + H64[2:], H64[:-1] + "ａ"):
        assert _p("/approve review-spec " + bad) is None, bad
    assert _p("/approve review-spec " + H64)["hash"] == H64
    assert _p("/approve review-spec " + "0" * 64)["hash"] == "0" * 64
    assert _p(doc("/approve review-spec " + H64 + ": looks right"))["text"] == "looks right"


def test_notes_and_answers_are_capped_and_scrubbed():
    assert _p("/reject D3: " + "n" * 300)["text"] == "n" * 300
    assert _p("/reject D3: " + "n" * 301)["text"] == "n" * 300  # capped, never refused: a long reject must stay a reject
    assert _p("/reject D3: " + "n" * 5000)["text"] == "n" * 300
    assert _p("/answer Q2: " + "a" * 500)["text"] == "a" * 500
    assert _p("/answer Q2: " + "a" * 501)["text"] == "a" * 500
    body = doc(p("/answer Q2: mail dana@contoso.example or ping ", mention(APPROVER, "Dana Fictional"), " or [~accountid:" + APPROVER + "]"
                 " or @dana \x1b[31mred\x1b[0m <!-- x --> ```fence``` <!<!---->-- y -->"))
    t = _p(body)["text"]
    assert "contoso" not in t and "Dana" not in t and "\x1b" not in t and "<!--" not in t and "-->" not in t and "```" not in t
    assert APPROVER[:8] not in t and "accountid" not in t and "dana" not in t
    assert "[email]" in t and "@user" in t and "\n" not in t
    assert _p("/reject D3: da​na@contoso.example")["text"] == "[email]"  # an invisible character cannot shield an address
    assert _p("/reject D3: dana@con<!---->toso.example")["text"] == "[email]"  # nor removed markup
    assert _p("/answer Q2: ​​") is None  # nothing left after the scrub
    assert _bad("/answer Q2: ​​") == "Q2"
    assert _p("/reject D3: ``````") is None


def test_quoted_approve_ignored():
    assert _p(doc(quote(p("/approve D3")))) is None
    assert _p(doc(quote(p("/approve D3"), quote(p("/approve D3")), p("/approve D3")))) is None  # nested
    assert _p(doc(quote(quote(p("x")), p("/approve D3")))) is None
    assert _p(doc(p("> /approve D3"))) is None  # typed `>`: escaped by the reader, still not a command
    # a human's own first line outside the quote counts; a quote AFTER it changes nothing
    assert _p(doc(p("/approve D3"), quote(p("/reject D3"))))["verb"] == "approve"
    # CHANGED from the fork (which stripped quotes first, so a command after a leading quote counted): here the command must be
    # the FIRST non-empty block (plan rule 6), so a reply under a quoted request does not count
    assert _p(doc(quote(p("/reject D3")), p("/approve D3"))) is None
    assert _p(doc(quote(p("[skj:approval-request] review-spec")), "/approve D3")) is None
    assert row(body=doc(quote(p("/reject D3")), p("/approve D3"))).md.startswith(">")


def test_second_line_command_ignored():
    for body in (doc("Looks good", "/approve D3"), doc(p("Thanks!", br(), "/approve D3")), doc(p("ok"), p("/approve D3")),
                 doc(bullets("x"), "/approve D3"), doc("/approve D3 and also"), "hello\n/approve D3"):
        assert _p(body) is None, body
    assert _p(doc("/reject D3: no", "/approve D3"))["verb"] == "reject"  # the FIRST command wins: only the first line counts


def test_bad_lines_reported_not_obeyed():
    secret = "SEKRET-TEXT-" + "z" * 5
    assert approvals._parse("/approve D3 " + secret) == (None, "D3")
    assert approvals._parse("/reject D3 %s no colon" % secret) == (None, "D3")
    assert approvals._parse("just chatting") == (None, None)  # chatting is not a bad line
    assert approvals._parse("/approve review-spec " + secret) == (None, "review-spec")
    assert approvals._parse("/approve D4") == (C("approve", "D4"), None)  # a valid line is not a bad line
    assert approvals._parse("/approve") == (None, "")
    assert approvals._parse("/approve D3: ​") == (None, "D3")


def test_oversized_comment_is_a_bad_line_not_a_hang():
    assert approvals._parse("/approve D3" + " " * approvals.MAX_MD) == (None, "")
    assert approvals._parse("/approve D3" + " " * (approvals.MAX_MD - 11)) == (C("approve", "D3"), None)
    assert approvals._parse("/approve D3: " + "n" * (approvals.MAX_MD - 13))[0]["text"] == "n" * 300


def test_a_first_line_starting_with_a_quote_marker_is_not_skipped():
    for body in (doc(p("> x", br(), "/approve D3")), doc(p("> agent text"), p("/approve D3")), "> agent said this\n/approve D3",
                 doc(p(">"), p("/approve D3")), doc(p("> /reject D3: no"), p("/approve D3")), doc(quote(p("x")), p("/approve D3"))):
        assert _p(body) is None, body


def test_look_alike_verbs_are_bad_lines():
    for line in ("/REJECT D3", "/Approve D3", "/approved D3", "/approve-all D3", "／approve D3", "​/approve D3", "/ＡＰＰＲＯＶＥ D3",
                 "/answers Q2: x", "/reject D3 reason", "‏/approve D3", "⁦/approve D3"):
        assert _p(line) is None and _bad(line) is not None, line
        r = row(body=doc(line))
        assert _p(r.md) is None and _bad(r.md) is not None, line
    for line in ("/path/to/file", "/ approve D3", "approve D3", "/app D3", "// approve", "/аpprove D3"):
        assert _p(line) is None and _bad(line) is None, line


def test_mixed_case_verbs_are_not_commands_but_are_reported():
    for line in ("/Approve D3", "/APPROVE D3", "/aPPROVE D3", "/Reject D3: no", "/Answer Q1: yes"):
        assert approvals._parse(line)[0] is None and approvals._parse(line)[1] is not None, line


def test_blanks_between_verb_and_target_are_a_bad_line_not_a_command():
    for sep in (" ", "　", " ", " ", "​", "\u0085", "\x0b", "  ", " \t"):
        line = "/approve%sD3" % sep
        if sep in ("  ", " \t"):
            assert _p(line) == C("approve", "D3"), repr(sep)  # ASCII blanks may repeat, as in the fork's grammar
            continue
        assert _p(line) is None and _bad(line) is not None, repr(sep)
    assert _p(doc("/approve", "D3")) is None and _bad(doc("/approve", "D3")) == ""
    for sep in (" ", "　", " "):  # and not between target and colon either
        assert _p("/reject D3%s: x" % sep) is None, repr(sep)


def test_a_first_line_that_only_looks_empty_is_a_bad_line():
    for body in ("​", doc(p("​"), p("/approve D3")), "﻿/approve D3\n", doc(p("​​"), "/approve D3"),
                 doc(p("⁠"), "/approve D3"), doc(p(" "), "/approve D3")):
        assert _p(body) is None and _bad(body) is not None, repr(body)
    for body in ("", doc(), doc(p()), doc(p(" ")), doc(p(br(), br())), "\n\n \t\n"):
        assert _p(body) is None and _bad(body) is None, repr(body)


def test_crlf_and_unicode_line_breaks_are_never_split_on():
    # Row.md never holds \r or U+2028 (strict ADF refuses them); a caller's Markdown that does is not obeyed, only reported
    for md in ("/approve D3\r\nthanks", "/approve D3\r", "/approve D3 thanks", "/approve D3 ", "/approve D3\x85",
               "\r/approve D3", "x\r/approve D3", "x /approve D3"):
        assert approvals.parse_command(md) is None, repr(md)
    assert _bad("/approve D3\r\nthanks") == "D3"
    assert _bad("x /approve D3") is None  # not split: the line is "x...", which is not a command
    assert approvals.parse_command("/approve D3\nthanks") == C("approve", "D3")


def test_elsewhere_tells_which_malformed_lines_may_be_about_a_target():
    for line in ("/reject D3.", "/reject D3, not ok", "/reject (D3)", '/reject "D3"', "/reject D3!", "/reject D03", "/reject D３", "/reject D3;",
                 "/reject D-3", "/reject d3", "/reject D3 reason", "/reject", "/reject  ", "/reject D3: ", "/reject <D3>", "/reject D 3",
                 "/reject D3…", "/REJECT D3", "/reject d3: x", "/reject D3 changed my mind", "/reject D4.x"):
        bad = _bad(line)
        assert bad is not None and not approvals._elsewhere(bad, "D3"), line
    for line in ("/reject D4 because", "/reject Q2 x", "/reject review-spec", "/answer Q2", "/reject D4 changed my mind", "/reject review-spec x"):
        bad = _bad(line)
        assert bad is not None and approvals._elsewhere(bad, "D3"), line
    for line in ("/reject D4", "/reject D4: no", "/approve D10"):
        assert _bad(line) is None and _p(line) is not None, line
    assert not approvals._elsewhere("review-spec", "review-spec") and approvals._elsewhere("D3", "review-spec")
    assert not approvals._elsewhere("D3:x", "D3") and approvals._elsewhere("D4:x", "D3")


def test_a_long_reject_is_a_reject_not_a_malformed_line():
    got = _p(doc("/reject D3: " + "n" * 5000))
    assert got["verb"] == "reject" and len(got["text"]) == 300
    assert _bad(doc("/reject D3: " + "n" * 5000)) is None


def test_parse_does_not_echo_or_mutate():
    r = row(body=doc("/approve D3: dana@contoso.example"))
    snap = repr(r)
    got = _p(r.md)
    assert repr(r) == snap and "contoso" not in repr(got) and set(got) == {"verb", "target", "hash", "text"}


# --- linear time: every reader at 1 MiB, 500 x 1990-character lines and 20000 short lines -------------------------------------

_MIB = 1 << 20
_BOUND = 2.0


class _TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise _TooSlow()


def _timed(fn, *args):
    # CPU time, not the wall clock (H1b): load slows the clock, not the work; the timed calls run no subprocess and never block
    old = signal.signal(signal.SIGPROF, _alarm)
    signal.setitimer(signal.ITIMER_PROF, _BOUND)
    t0 = time.process_time()
    try:
        fn(*args)
    except _TooSlow:
        return _BOUND
    except ValueError:
        pass
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, old)
    return time.process_time() - t0


_UNITS = {"space": " ", "tab": "\t", "nl": "\n", "nl-space": "\n ", "lt": "<", "gt": ">", "slash": "/", "colon": ":", "nbsp": " ",
          "zw": "​", "rtl": "‏", "ideo": "　", "at": "@", "at-lt": "@<", "wiki": "[~", "email": "a@b.", "email2": "a.a@",
          "markup": "<!-", "markup2": "<!--", "close": "-->", "fence": "``", "tilde": "~~", "nest": "<!<!--", "bq": "> ", "quote-mark": "[?",
          "verb": "/approve ", "verbD": "/approve D3 ", "hex": "a", "dq": "D3", "word": "x", "fullwidth": "／", "cr": "\r", "ls": " ",
          "digit": "1", "dash": "-", "colon-sp": ": "}
_PREFIXES = ("", "/approve D3: ", "/reject D3 ", "/answer Q1: ", "/approve review-spec ", "/approve ")


def _shapes(size):
    for name, unit in _UNITS.items():
        for pre in _PREFIXES:
            yield "%s|%s|%d" % (name, pre, size), pre + unit * ((size - len(pre)) // len(unit))
        n = size // 2000
        yield "%s|%dx1990" % (name, n), "\n".join([("/approve D3: " + unit * 1990)[:1990]] * n)
        yield name + "|20000-short", "\n".join([unit * 3] * 20000) + "\n/approve D3"
    m = size // 8  # markup that a removal brings together again: one layer per pass for a remove-until-none-left loop
    for name, s in (("nested-open", "<!" * m + "--" * m), ("nested-close", "-" * (2 * m) + ">" * m), ("nested-fence", "``~~" * m + "~`" * m)):
        for pre in ("", "/reject D3: "):
            yield "%s|%s|%d" % (name, pre, size), (pre + s)[:size]


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs signal.setitimer")
def test_every_reader_is_linear_on_adversarial_text():
    # at MAX_MD every grammar path runs (a longer Markdown stops at the length check); at 1 MiB the guard and the word checks;
    # `_note` gets the raw shapes too (the prefixes only matter to `_parse`)
    slow = []
    base = good()
    checks = []
    for name, s in _shapes(approvals.MAX_MD):
        checks += [(name, "parse", approvals._parse, s)]
        if "|" + "|" in name or "x1990" in name or "short" in name:
            checks += [(name, "note", approvals._note, s)]
    for name, s in _shapes(_MIB):
        checks += [(name, "guard", lambda r: approvals.guard(r, [APPROVER], {ME_ID}), base._replace(md=s)),
                   (name, "parse", approvals._parse, s), (name, "elsewhere", lambda w: approvals._elsewhere(w, "D3"), s),
                   (name, "looks", approvals._looks_like_command, s)]
    for name, label, fn, arg in checks:
        took = _timed(fn, arg)
        if took >= 1.0:
            slow.append((name, label, round(took, 2)))
    assert not slow, slow
    assert len(checks) > 500


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs signal.setitimer")
def test_excluded_and_rows_are_linear_in_the_comment_count():
    many = [good()._replace(id=str(10000 + i), md="[skj:x]" if i % 2 else "/approve D3") for i in range(20000)]

    class One:
        def request(self, method, path, **kw):
            return cl.Response(200, {}, {"accountId": ME_ID, "accountType": "atlassian", "active": True})

    assert _timed(approvals.excluded, One(), many) < 1.0
    assert _timed(lambda: [approvals.guard(r, [APPROVER] * 1, {ME_ID}) for r in many]) < 2.0


# ==== A2: the time order, the newest valid command per target, the bad_newer tally ==============================================
# Ported from the fork's `newest` / `_when` / `_order` cases (tests/test_approvals.py, the parts A1 deferred). Rows are real Rows.

def test_when_parses_jira_stamps_as_instants_never_as_strings():
    W = approvals._when
    utc = W("2026-01-05T09:00:00.000+0000")
    assert utc is not None and utc.utcoffset().total_seconds() == 0
    for same in ("2026-01-05T09:00:00Z", "2026-01-05T09:00:00.000+00:00", "2026-01-05T11:00:00.000+0200",
                 "2026-01-05T08:30:00.000-0030", "2026-01-05T09:00:00.000000000Z", "2026-01-05T04:00:00-05:00"):
        assert W(same) == utc, same
    assert W("2026-01-05T09:00:00.5Z") > W("2026-01-05T09:00:00.25Z")  # a fraction is a fraction, not a string
    assert W("2026-01-05T09:30:00.000-0100") > W("2026-01-05T10:00:00.000+0000")  # a raw string compare says the opposite
    for bad in (None, 5, "", "2026-01-05", "2026-13-05T09:00:00Z", "2026-02-30T09:00:00Z", "2026-01-05T24:00:00Z",
                "2026-01-05T09:00:60Z", "2026-01-05T09:00:00+2400", "2026-01-05T09:00:00+0060", "2026-01-05T09:00:00",
                "2026-01-05 09:00:00Z", "２026-01-05T09:00:00Z", "2026-01-05T09:00:00.0000000000Z", "0001-01-01T00:00:00+0100",
                "2026-01-05T09:00:00Z\n"):
        assert W(bad) is None, bad


def test_order_is_time_then_numeric_id_without_int():
    a = good()._replace(id="9")
    b = good()._replace(id="10")
    assert approvals._order(a) < approvals._order(b)  # same instant: the numeric id breaks the tie ("9" < "10")
    later = good()._replace(id="9", created="2026-01-05T09:00:00.001+0000", updated="2026-01-05T09:00:00.001+0000")
    assert approvals._order(later) > approvals._order(b)
    assert approvals._order(good()._replace(id="09")) is None and approvals._order(good()._replace(created="bad")) is None
    assert approvals._order("x") is None


def _board(rs, approvers=(APPROVER,), excl=(ME_ID,)):
    return approvals.board(list(rs), list(approvers), set(excl))


def _at(cid, when, body, **kw):
    return raw(cid=cid, created=when, body=doc(body) if isinstance(body, str) else body, **kw)


T0, T1, T3 = "2026-01-05T09:00:00.000+0000", "2026-01-05T09:00:01.000+0000", "2026-01-05T09:00:03.000+0000"


def test_newest_valid_command_wins_and_a_later_valid_reject_overrides():
    b = _board(rows(_at("10", T0, "/approve D1: ok"), _at("11", T1, "/reject D1: no")))
    w = approvals.newest(b, "D1")
    assert (w["verb"], w["id"], w["text"], w["bad_newer"]) == ("reject", "11", "no", 0)
    assert w["pin"] == approvals.pin(rows(_at("11", T1, "/reject D1: no"))[0]) and approvals.PIN_RE.match(w["pin"])
    assert approvals.newest(b, "D2") is None


def test_a_later_invalid_command_never_cancels_an_earlier_valid_one_and_is_reported():
    rs = rows(_at("10", T0, "/approve D1"), _at("11", T1, "/reject D1", updated=T3),  # edited
              _at("12", T1, "/reject D1", visibility={"type": "role", "value": "Developers"}),
              _at("13", T3, "/reject D1", author=ME_ID), _at("14", T3, "/reject D1", author=OTHER_ID),
              _at("15", T3, doc(quote(p("/reject D1")))))
    tally, ignored = {}, {}
    w = approvals.newest(_board(rs), "D1", tally=tally, ignored=ignored)
    assert w["id"] == "10" and w["verb"] == "approve"
    assert ignored == {"11": "edited", "12": "restricted", "13": "excluded-author", "14": "not-approver"}  # 15 is no command at all
    assert tally == {"edited": 1, "restricted": 1, "excluded-author": 1, "not-approver": 1}


def test_ties_on_the_same_millisecond_are_broken_by_the_numeric_id():
    rs = rows(_at("100", T0, "/reject D1"), _at("99", T0, "/approve D1"))
    assert approvals.newest(_board(rs), "D1")["id"] == "100"  # "100" > "99" as numbers, though not as strings
    rs = rows(_at("99", T0, "/reject D1"), _at("100", "2026-01-05T11:00:00.000+0200", "/approve D1"))  # the same instant, other zone
    assert approvals.newest(_board(rs), "D1")["verb"] == "approve"


def test_bad_newer_counts_malformed_and_look_alike_lines_by_approvers_newer_than_the_winner():
    rs = rows(_at("10", T0, "/approve D1"), _at("11", T1, "/reject D1."), _at("12", T1, "/Reject D1"),
              _at("13", T1, "/аpprove D1"),  # a Cyrillic а: A1 must-enforce 6
              _at("14", T1, "/reject D2"),  # a well-formed command for another target: not a bad line
              _at("15", T1, "/reject D9 extra"),  # malformed, but names another well-formed target
              _at("16", T1, "/reject D1 junk", author=OTHER_ID),  # not an approver: not counted
              _at("17", T1, "/reject D1 junk", author=ME_ID),  # excluded: not counted
              _at("9", "2026-01-05T08:00:00.000+0000", "/reject D1 junk"))  # older than the winner
    tally = {}
    w = approvals.newest(_board(rs), "D1", tally=tally)
    assert w["id"] == "10" and w["bad_newer"] == 3 and tally["bad_newer"] == 3
    t9 = {}
    assert approvals.newest(_board(rs), "D9", tally=t9) is None and t9["bad_newer"] == 3  # no winner: every line that may be about D9
    for lookalike in ("/аpprove D1", "/rеject D1", "/аnswer Q1: x", "∕approve D1", "/ΑPPROVE D1", "/apprоve D1"):
        assert approvals._parse(lookalike) == (None, None) and approvals._lookalike(lookalike), lookalike
    for plain in ("/path/x", "approve D1", "hello", "/ approve D1"):
        assert not approvals._lookalike(plain), plain


def test_board_refuses_what_it_cannot_trust():
    with pytest.raises(ValueError):
        approvals.board([good()], "5b10ac8d82e05b22cc7d4ef5", {ME_ID})
    with pytest.raises(ValueError):
        approvals.board([good()], [APPROVER], set())
    with pytest.raises(ValueError):
        approvals.board([good()._asdict()], [APPROVER], {ME_ID})
    with pytest.raises(ValueError):
        approvals.board(iter([good()]), [APPROVER], {ME_ID})


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs signal.setitimer")
def test_newest_is_linear_over_many_comments_and_targets():
    base = good()
    many = [base._replace(id=str(10000 + i), md="/approve D%d" % (i % 5000 + 1) if i % 3 else "/reject D%d junk" % (i % 7))
            for i in range(20000)]
    b = []
    assert _timed(lambda: b.append(_board(many))) < 2.0
    assert _timed(lambda: [approvals.newest(b[0], "D%d" % n) for n in range(1, 20001)]) < 2.0


# ==== A4: gate hashes, `require` at the write verbs, `approvals check`, the poller (EXPERIMENTAL) ============================
# Against the fake Jira only; `specify` is never run (the one subprocess seam, `approvals._spawn`, is faked, and the real `_spawn` is
# exercised with a local python child only). Git runs against temp repos.

import hashlib  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

import skj.__main__ as m  # noqa: E402
from skj import config, decisions, questions  # noqa: E402
from skj.config import Config  # noqa: E402

KEY4 = "SPK-1"
SITE4 = "example-site.atlassian.net"
CFG4 = Config(SITE4, "SPK")
PLAN = b"## plan\n- create T001\n"
GATES12 = ("review-spec", "approve-questions", "approve-plan-questions", "wait-po-answers", "approve-answers-questions", "review-plan",
           "approve-push", "approve-decisions", "approve-sync", "review-review", "approve-pr", "wait-po-validation")


def _tr(approval="comment", approvers=(APPROVER,)):
    return config.Trust(list(approvers), approval, {}, SITE4, True)


def _fk():
    f = FakeJira()
    assert f.add_issue("A fictional story") == KEY4
    return f


def _say(f, text, who=APPROVER, atype="atlassian", **kw):
    return f.add_comment(KEY4, adf.md_to_adf(text) if isinstance(text, str) else text, author=f.user(who, atype), **kw)


def _h(gate, data=PLAN, key=KEY4):
    return approvals.gate_hash(gate, key, data)


def _req(f, verb="push", plan=PLAN, trust=None):
    return approvals.require(f.client(), trust or _tr(), CFG4, KEY4, verb, plan)


def _missing(f, **kw):
    with pytest.raises(approvals.ApprovalMissing) as e:
        _req(f, **kw)
    return str(e.value)


def _gets_only(f):
    assert f.log and all(e["method"] == "GET" for e in f.log) and f.writes() == []


def _no_ids(text, f=None):
    for s in (APPROVER, APPROVER2, OTHER_ID, ME_ID, APPROVER[:8], "Fictional", "dev.person@example.com", "FAKE-tok"):
        assert s not in text, s


# ---- gate_hash and the fixed lists ------------------------------------------------------------------------------------------

def test_gate_hash_is_pinned_and_covers_the_gate_the_story_key_and_the_bytes():
    assert _h("approve-push") == hashlib.sha256(b"spec-kit-jira/1\napprove-push\nSPK-1\n" + PLAN).hexdigest()
    assert len({_h("approve-push"), _h("approve-sync"), _h("approve-push", key="SPK-2"), _h("approve-push", PLAN + b" "),
                _h("approve-push", key="SPK-11")}) == 5
    for bad in (("approve-push", 1, PLAN), ("approve-push", "spk-1", PLAN), ("approve-push", "SPK-1\n", PLAN), ("nope", KEY4, PLAN),
                ("approve-push", KEY4, PLAN.decode()), (None, KEY4, PLAN)):
        with pytest.raises(ValueError):
            approvals.gate_hash(*bad)


def test_gates_are_the_twelve_of_the_workflow_and_the_verbs_gates_are_its_apply_gates():
    assert approvals.GATES == GATES12
    assert set(approvals.REVIEW_GATES) == {"review-spec", "review-plan", "review-review"}
    assert set(approvals.WAIT_GATES) == {"wait-po-answers", "wait-po-validation"}
    apply = set(GATES12) - set(approvals.REVIEW_GATES) - set(approvals.WAIT_GATES)
    assert set(approvals.APPLY_FILES) == apply == {g for gs in approvals.VERB_GATES.values() for g in gs}
    assert approvals.VERB_GATES == {"questions-post": ("approve-answers-questions", "approve-plan-questions", "approve-questions"),
                                    "push": ("approve-push", "approve-sync"), "decisions-post": ("approve-decisions",),
                                    "pr-create": ("approve-pr",)}
    assert all(p.startswith(".skj/plan-") and p.endswith(".md") for p in approvals.APPLY_FILES.values())
    assert len(set(approvals.APPLY_FILES.values())) == len(apply)


# ---- require: the gate in front of a write in comment mode -------------------------------------------------------------------

def test_require_is_a_noop_in_terminal_mode_with_no_client_and_no_request():
    assert approvals.require(None, _tr("terminal"), CFG4, KEY4, "push", PLAN) is None
    assert approvals.require(None, _tr("terminal", ()), CFG4, KEY4, "pr-create", PLAN) is None


def test_require_refuses_without_an_approval_and_names_the_hashes():
    f = _fk()
    text = _missing(f)
    assert text == approvals.E_MISSING.format(key=KEY4, gates="approve-push %s, approve-sync %s" % (_h("approve-push"), _h("approve-sync")),
                                              bad="")
    _gets_only(f)
    _no_ids(text)


def test_require_passes_with_a_valid_approval_of_this_plan_for_any_gate_of_the_verb():
    for verb, gates in approvals.VERB_GATES.items():
        for gate in gates:
            f = _fk()
            _say(f, "/approve %s %s" % (gate, _h(gate)))
            f.log.clear()
            assert _req(f, verb=verb) is None, (verb, gate)
            _gets_only(f)


def test_require_refuses_another_plan_another_story_and_a_gate_outside_the_verb():
    f = _fk()
    _say(f, "/approve approve-push %s" % _h("approve-push"))
    assert "no valid approval for this plan" in _missing(f, plan=PLAN + b"x")  # the plan changed after the approval
    for cmd in ("/approve approve-pr %s" % _h("approve-pr"),  # the right plan, another verb's gate
                "/approve approve-push %s" % _h("approve-push", key="SPK-2"),  # another story's hash
                "/approve approve-push %s" % _h("approve-sync")):  # the sibling gate's hash under this gate's name
        f = _fk()
        _say(f, cmd)
        _missing(f)


def test_require_who_and_what_never_approves():
    """Rule 1 (excluded-author: the token user, a kit-marker author), not-approver, app account, edited, restricted, quoted, second line."""
    cmd = "/approve approve-push %s" % _h("approve-push")
    cases = {
        "token user": lambda f: _say(f, cmd, who=ME_ID),
        "kit author": lambda f: (_say(f, "[skj:questions 0badc0de]\n\nhello", who=APPROVER2), _say(f, cmd, who=APPROVER2)),
        "stranger": lambda f: _say(f, cmd, who=OTHER_ID),
        "app": lambda f: _say(f, cmd, atype="app"),
        "edited": lambda f: f.edit_comment(_say(f, cmd), adf.md_to_adf(cmd), editor=f.user(APPROVER)),
        "restricted": lambda f: _say(f, cmd, visibility={"type": "role", "value": "Developers"}),
        "quoted": lambda f: _say(f, doc(quote(p(cmd)))),
        "second line": lambda f: _say(f, "looks good\n\n" + cmd),
    }
    for name, seed in cases.items():
        f = _fk()
        seed(f)
        _missing(f, trust=_tr(approvers=(APPROVER, APPROVER2, ME_ID, OTHER_ID) if name != "stranger" else (APPROVER,))), name


def test_require_newest_valid_command_over_the_verbs_gates_wins():
    a, r = "/approve approve-push %s" % _h("approve-push"), "/reject approve-push %s" % _h("approve-push")
    for seq, ok in (((a, r), False), ((r, a), True), ((a, "/approve approve-push %s" % _h("approve-push", PLAN + b"y")), False),
                    ((a, "/reject approve-sync %s" % _h("approve-sync")), False), (("/reject approve-sync %s" % _h("approve-sync"), a), True),
                    ((a, "/approve D1"), True)):
        f = _fk()
        for t in seq:
            _say(f, t)
        if ok:
            assert _req(f) is None, seq
        else:
            _missing(f)
    f = _fk()  # a later INVALID comment (a stranger's reject) never cancels a valid approval
    _say(f, a)
    _say(f, r, who=OTHER_ID)
    assert _req(f) is None


def test_require_a_newer_malformed_or_tampered_approver_comment_refuses():
    a = "/approve approve-push %s" % _h("approve-push")
    f = _fk()
    _say(f, "/reject approve-push")  # older malformed line: harmless
    _say(f, a)
    assert _req(f) is None
    _say(f, "/reject approve-push")  # newer malformed line about this gate
    text = _missing(f)
    assert text.endswith(" 1 newer malformed command line(s) by an approver for this plan: check the thread.")
    f = _fk()
    _say(f, a)
    _say(f, "/rеject approve-push %s" % _h("approve-push"))  # a Cyrillic look-alike retraction counts as a bad line
    _missing(f)
    f = _fk()
    _say(f, a)
    rej = _say(f, "/reject approve-push %s" % _h("approve-push"))
    f.edit_comment(rej, adf.md_to_adf(a), editor=f._me())  # the agent turns the newer reject into an approve
    text = _missing(f)
    assert approvals.E_TAMPERED in text
    f = _fk()
    _say(f, a)
    _say(f, "/reject approve-push %s" % _h("approve-push"), visibility={"type": "role", "value": "Developers"})  # hidden retraction
    _missing(f)
    f = _fk()
    _say(f, a)
    rej = _say(f, "/reject approve-push %s" % _h("approve-push"))
    f.comments[rej]["created"] = f.comments[rej]["updated"] = "2026-02-30T09:00:00.000+0000"  # a time that cannot be read: pending
    assert approvals._order(comments.fetch(f.client(), KEY4)[-1]) is None
    _missing(f)


def test_require_refuses_when_the_comments_cannot_be_read_or_attributed():
    f = _fk()
    _say(f, "/approve approve-push %s" % _h("approve-push"))
    with pytest.raises(approvals.ApprovalMissing) as e:
        _req(f, trust=_tr(approvers=()))
    assert str(e.value) == approvals.E_NO_APPROVERS and f.log == []
    f.move_issue(KEY4, "SPK-9")  # the issue answers with another key
    assert _missing(f) == approvals.E_UNREADABLE
    f = _fk()
    _say(f, "/approve approve-push %s" % _h("approve-push"))
    f.inject(404)
    assert _missing(f) == approvals.E_UNREADABLE
    f = FakeJira(account_type="app")
    f.add_issue("A fictional story")
    assert _missing(f) == approvals.E_IDENTITY


def test_require_refuses_when_a_comment_lies_past_the_end_of_the_list(monkeypatch):
    f = _fk()
    _say(f, "/approve approve-push %s" % _h("approve-push"))
    real = comments.fetch
    monkeypatch.setattr(comments, "fetch", lambda c, k: real(c, k)[:0])  # a page that drops the last comment: the probe sees it
    assert _missing(f) == approvals.E_UNREADABLE


# ---- `approvals check`: review gates by the hash of the committed artifacts, wait gates by what Jira shows --------------------

LEDGER4 = ("# DoD \u2014 SPK-1: A fictional story\n\n- **Key:** SPK-1\n- **Spec dir:** specs/001-x\n\n## 1. Seeded from Jira\n\n- a thing\n\n"
           + "## 2. Brainstorm & plan\n\n- [ ] Q1: Are contractors included? \u2192 _(open)_ \u00b7 owner: PO\n\n## 3. AC\n\n"
           + "## 4. Gate & review log\n\n### Review 2026-01-05\n- Blocks PR: no\n\n## 5. Definition of Done\n- [ ] x\n")
SPEC4, PLANMD4 = "# Spec\n\nThe fictional feature.\n", "# Plan\n\nDo the fictional thing.\n"
_GIT_ID = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
           "GIT_COMMITTER_EMAIL": "test@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git4(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env={**os.environ, **_GIT_ID}).stdout.strip()


def _commit4(repo, files, msg="x"):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    _git4(repo, "add", "-A")
    _git4(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return _git4(repo, "rev-parse", "HEAD")


def _target4(repo, cfg):
    """Commit `cfg` as skj.config.json and point origin/HEAD at that commit (the TARGET branch)."""
    _commit4(repo, {"skj.config.json": json.dumps(cfg)}, "target")
    _git4(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git4(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


BASE4 = {"site": SITE4, "project": "SPK"}
COMMENT4 = {**BASE4, "approvers": [APPROVER], "approval": "comment"}


@pytest.fixture
def repo4(tmp_path, monkeypatch):
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    for k in ("SKJ_EMAIL", "SKJ_TOKEN", "CI"):
        monkeypatch.delenv(k, raising=False)
    _git4(tmp_path, "init", "-q", "-b", "main")
    _commit4(tmp_path, {"dod/DoD_SPK-1.md": LEDGER4, "specs/001-x/spec.md": SPEC4, "specs/001-x/plan.md": PLANMD4}, "story")
    _target4(tmp_path, COMMENT4)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _check(f, gate, repo, trust=None):
    return approvals.check(f.client if f is not None else None, trust or _tr(), CFG4, KEY4, gate, repo)


def _cli(f, *argv):
    def boom():
        raise AssertionError("a client was built")
    return m.main(list(argv), client_factory=f.client if f is not None else boom)


def test_check_noop_in_terminal_mode(repo4, capsys):
    """A4-AC1: approval "terminal" on the TARGET branch: every gate exits 0 with no client built and no request, even when the working
    tree says "comment"; `approval` is never read from the working tree."""
    _target4(repo4, {**BASE4, "approvers": [APPROVER], "approval": "terminal"})
    (repo4 / "skj.config.json").write_text(json.dumps(COMMENT4))  # the PR's own word is not trusted
    for gate in GATES12:
        assert _cli(None, "approvals", "check", KEY4, "--gate", gate) == 0, gate
        assert json.loads(capsys.readouterr().out) == {"key": KEY4, "gate": gate, "mode": "terminal", "met": True}
    assert approvals.check(None, _tr("terminal"), CFG4, KEY4, "review-spec", repo4)["mode"] == "terminal"
    assert _cli(None, "approvals", "check", KEY4, "--gate", "nope") == 2  # an unknown gate is refused first, in every mode
    assert capsys.readouterr().err.strip() == "skj: " + approvals.E_GATE


def test_check_reads_the_target_branch_not_the_working_tree(repo4, capsys):
    (repo4 / "skj.config.json").write_text(json.dumps({**BASE4, "approval": "terminal"}))  # the PR tries to turn it off
    f = _fk()
    assert _cli(f, "approvals", "check", KEY4, "--gate", "review-spec") == 2
    _gets_only(f)


def test_check_review_gate_is_met_only_by_the_hash_of_the_committed_artifacts(repo4, capsys):
    """A4-AC1: read-only (GETs only); met only by `/approve review-spec <hash of the artifacts at HEAD>` from an approver."""
    h = approvals.gate_hash("review-spec", KEY4, approvals.artifacts("review-spec", KEY4, repo4))
    f = _fk()
    assert _cli(f, "approvals", "check", KEY4, "--gate", "review-spec") == 2
    err = capsys.readouterr().err
    assert err.strip() == "skj: " + approvals.E_REVIEW.format(gate="review-spec", key=KEY4, hash=h, bad="") and h in err
    _no_ids(err)
    _say(f, "/approve review-spec %s" % h, who=ME_ID)  # the agent's own approval never counts
    assert _cli(f, "approvals", "check", KEY4, "--gate", "review-spec") == 2
    _say(f, "/approve review-spec %s" % h)
    f.log.clear()
    assert _cli(f, "approvals", "check", KEY4, "--gate", "review-spec") == 0
    _gets_only(f)
    assert json.loads(capsys.readouterr().out) == {"key": KEY4, "gate": "review-spec", "mode": "comment", "met": True}
    (repo4 / "specs/001-x/spec.md").write_text("# Spec\n\nSomething else.\n")  # DF7 D-027: an uncommitted change is refused, never hashed
    assert _cli(f, "approvals", "check", KEY4, "--gate", "review-spec") == 2
    assert capsys.readouterr().err.strip() == "skj: " + approvals.E_UNCOMMITTED
    _commit4(repo4, {}, "spec changed")  # committed: the approval no longer covers the artifacts
    assert _cli(f, "approvals", "check", KEY4, "--gate", "review-spec") == 2
    assert _cli(f, "approvals", "check", KEY4, "--gate", "review-plan") == 2  # another gate needs its own approval


@pytest.mark.parametrize("gate,path", [("review-spec", "specs/001-x/spec.md"), ("review-spec", "dod/DoD_SPK-1.md"),
                                       ("review-plan", "specs/001-x/plan.md"), ("review-plan", "dod/DoD_SPK-1.md"),
                                       ("review-review", "dod/DoD_SPK-1.md")])
def test_a_review_gate_refuses_while_a_file_it_shows_is_uncommitted(repo4, capsys, gate, path):
    """DF7 D-027 (the owner session): after the workflow's question applies, the ledger holds an uncommitted `Questions posted:` line
    when gate review-plan asks; a hash of HEAD would be approved over a stale ledger. Comment mode refuses (check and the poller's hash)
    until the file is committed; terminal mode is unchanged (a no-op)."""
    (repo4 / path).write_text((repo4 / path).read_text(encoding="utf-8") + "- **Questions posted:** 10001:abcdef12\n", encoding="utf-8")
    with pytest.raises(approvals.ApprovalMissing) as e:
        approvals.artifacts(gate, KEY4, repo4)
    assert str(e.value) == approvals.E_UNCOMMITTED
    f = _fk()
    assert _cli(f, "approvals", "check", KEY4, "--gate", gate) == 2
    assert capsys.readouterr().err.strip() == "skj: " + approvals.E_UNCOMMITTED and f.log == []  # refused before any request
    assert approvals.check(None, _tr("terminal"), CFG4, KEY4, gate, repo4)["mode"] == "terminal"
    if path != "specs/001-x/plan.md" or gate == "review-plan":
        _commit4(repo4, {}, "commit it")
        approvals.artifacts(gate, KEY4, repo4)  # committed: hashed again


def test_an_unrelated_uncommitted_file_is_no_refusal(repo4):
    (repo4 / "notes.txt").write_text("x\n")
    (repo4 / "specs/001-x/plan.md").write_text("changed\n")  # review-spec does not show plan.md
    approvals.artifacts("review-spec", KEY4, repo4)


def test_artifacts_frame_what_each_review_gate_shows(repo4):
    from skj import dod
    sec2, sec4 = dod.section(LEDGER4, 2), dod.section(LEDGER4, 4)
    assert "Q1: Are contractors included?" in sec2 and "### Review 2026-01-05" in sec4 and "## 3." not in sec2 + sec4

    def frame(label, text):
        b = text.encode("utf-8")
        return b"%s %d\n%s\n" % (label.encode(), len(b), b)
    assert approvals.artifacts("review-spec", KEY4, repo4) == frame("spec.md", SPEC4) + frame("ledger-2", sec2)
    assert approvals.artifacts("review-plan", KEY4, repo4) == frame("plan.md", PLANMD4) + frame("spec.md", SPEC4) + frame("ledger-2", sec2)
    head = _git4(repo4, "rev-parse", "HEAD")
    assert approvals.artifacts("review-review", KEY4, repo4) == frame("ledger-4", sec4) + frame("head", head)
    _commit4(repo4, {}, "empty")  # a new commit: review-review no longer matches (it names HEAD), review-spec still does
    assert head.encode() not in approvals.artifacts("review-review", KEY4, repo4)
    with pytest.raises(approvals.ApprovalMissing):
        approvals.artifacts("approve-push", KEY4, repo4)


def test_artifacts_refuse_what_they_cannot_read(repo4):
    def refused(files=None, remove=(), link=None):
        for r in remove:
            _git4(repo4, "rm", "-q", r)
        if link:
            os.remove(str(repo4 / link[0]))
            os.symlink(link[1], str(repo4 / link[0]))
        _commit4(repo4, files or {})
        with pytest.raises(approvals.ApprovalMissing) as e:
            approvals.artifacts("review-plan", KEY4, repo4)
        assert str(e.value) == approvals.E_ARTIFACT
    refused({"specs/001-x/plan.md": b"\xff\xfe not utf-8"})
    refused({"specs/001-x/plan.md": "x" * ((1 << 20) + 1)})
    refused(link=("specs/001-x/plan.md", "spec.md"))
    refused(remove=["specs/001-x/plan.md"])
    for spec_dir in ("../outside", "/abs", "dod", "_(set by speckit.jira-lifecycle.pull)_"):
        refused({"dod/DoD_SPK-1.md": LEDGER4.replace("specs/001-x", spec_dir)})
    refused({"dod/DoD_SPK-1.md": LEDGER4.replace("## 3. AC", "```\n## 3. AC")})  # an unclosed fence hides the rest
    refused(remove=["dod/DoD_SPK-1.md"])


def _qpost(f, ledger=LEDGER4):
    return f.add_comment(KEY4, questions.body(questions.parse_questions(ledger)["open"])[1])  # the kit (token user) posts Q1


def test_wait_po_answers_is_met_from_jira_not_from_the_ledger(repo4):
    f = _fk()
    assert _check(f, "wait-po-answers", repo4)["met"] is True  # nothing posted: nothing to wait for
    _qpost(f)
    with pytest.raises(approvals.ApprovalMissing) as e:
        _check(f, "wait-po-answers", repo4)
    assert str(e.value) == approvals.E_WAIT.format(gate="wait-po-answers", key=KEY4, n=1, ids="Q1", bad="")
    (repo4 / "dod/DoD_SPK-1.md").write_text(LEDGER4.replace("_(open)_", "yes"))  # the ledger never meets a wait gate
    _say(f, "/answer Q1: yes", who=ME_ID)  # nor does the agent's own answer
    with pytest.raises(approvals.ApprovalMissing):
        _check(f, "wait-po-answers", repo4)
    _say(f, "/answer Q1: yes, contractors too")
    f.log.clear()
    assert _check(f, "wait-po-answers", repo4)["met"] is True
    _gets_only(f)
    _qpost(f, LEDGER4.replace("included?", "included at all?"))  # a newer posting of Q1: the answer is older than it
    with pytest.raises(approvals.ApprovalMissing):
        _check(f, "wait-po-answers", repo4)


def test_wait_po_answers_stays_open_on_a_tampered_or_unreadable_posting(repo4):
    f = _fk()
    post = _qpost(f)
    _say(f, "/answer Q1: yes")
    f.edit_comment(post, f.comments[post]["body"], editor=f._me())  # the kit's posting edited after the answer
    with pytest.raises(approvals.ApprovalMissing):
        _check(f, "wait-po-answers", repo4)
    f = _fk()
    _qpost(f)
    _say(f, "/answer Q1: yes")
    f.add_comment(KEY4, doc(MEDIA))  # an own comment the strict reader cannot read: it could be a newer posting
    with pytest.raises(approvals.ApprovalMissing) as e:
        _check(f, "wait-po-answers", repo4)
    assert str(e.value) == approvals.E_POSTINGS
    f = _fk()
    _qpost(f)
    _say(f, "/answer Q1: yes")
    _say(f, "/answer Q1")  # a newer malformed answer line by the approver
    with pytest.raises(approvals.ApprovalMissing) as e:
        _check(f, "wait-po-answers", repo4)
    assert "1 newer malformed command line(s) by an approver on these" in str(e.value)


LINES4 = ("- [ ] D1: Use cursor paging \u2192 _(open)_ \u00b7 kind: decision \u00b7 why: the story names no page size\n"
          "- [ ] D2: Cap at 50 rows \u2192 _(open)_ \u00b7 kind: assumption \u00b7 why: the story is silent\n")


def _dpost(f, repo, lines=LINES4):
    md = decisions.render_file(KEY4) + lines
    todo = decisions.unposted(decisions.parse_decisions(md))
    cid = f.add_comment(KEY4, decisions.body(todo)[1])
    md = decisions.stamp_posted(md, {d["id"]: decisions.marker_hash(d) for d in todo}, cid)
    (repo / "dod" / "Decisions_SPK-1.md").write_text(md, encoding="utf-8")
    return cid


def test_wait_po_validation_is_met_by_approver_verdicts_newer_than_the_posting(repo4):
    f = _fk()
    assert _check(f, "wait-po-validation", repo4)["met"] is True
    post = _dpost(f, repo4)
    _say(f, "/approve D1")
    with pytest.raises(approvals.ApprovalMissing) as e:
        _check(f, "wait-po-validation", repo4)
    assert str(e.value) == approvals.E_WAIT.format(gate="wait-po-validation", key=KEY4, n=1, ids="D2", bad="")
    _say(f, "/reject D2: too small")
    f.log.clear()
    assert _check(f, "wait-po-validation", repo4)["met"] is True
    _gets_only(f)
    path = repo4 / "dod" / "Decisions_SPK-1.md"
    path.write_text(path.read_text().replace("Cap at 50 rows", "Cap at 500 rows"))  # the file now says other words: kept open
    with pytest.raises(approvals.ApprovalMissing):
        _check(f, "wait-po-validation", repo4)
    os.remove(str(path))
    os.symlink("/etc/hosts", str(path))
    with pytest.raises(approvals.ApprovalMissing) as e:
        _check(f, "wait-po-validation", repo4)
    assert str(e.value) == approvals.E_DECISIONS_FILE
    os.remove(str(path))
    f.edit_comment(post, f.comments[post]["body"], editor=f._me())  # the posting edited: nothing it shows is met
    with pytest.raises(approvals.ApprovalMissing):
        _check(f, "wait-po-validation", repo4)


def test_a_decision_the_kit_cut_stays_open(repo4):
    f = _fk()
    _dpost(f, repo4, "- [ ] D1: %s \u2192 _(open)_ \u00b7 kind: decision \u00b7 why: long\n" % ("word " * 150).strip())
    _say(f, "/approve D1")
    with pytest.raises(approvals.ApprovalMissing):
        _check(f, "wait-po-validation", repo4)


def test_check_refuses_apply_gates_and_comment_mode_without_approvers(repo4):
    with pytest.raises(approvals.ApprovalMissing) as e:
        _check(None, "approve-push", repo4)
    assert str(e.value) == approvals.E_APPLY_GATE.format(gate="approve-push")
    with pytest.raises(approvals.ApprovalMissing) as e:
        _check(None, "review-spec", repo4, trust=_tr(approvers=()))
    assert str(e.value) == approvals.E_NO_APPROVERS
    for odd in ("Comment", None, ""):  # anything but "terminal" is comment mode: fail closed
        with pytest.raises(approvals.ApprovalMissing):
            _check(_fk(), "review-spec", repo4, trust=_tr(odd))


# ---- the poller: `approvals sync RUN_ID [--resume]` ------------------------------------------------------------------------------

def _state(repo, run_id="run-1", status="paused", step="review-spec", n=1, raw=None, **over):
    d = {"run_id": run_id, "workflow_id": "jira-lifecycle", "status": status, "current_step_index": 3, "current_step_id": step,
         "step_results": {}, "workflow_dir": None, "created_at": "2026-01-05T09:00:00.000000+00:00",
         "updated_at": "2026-01-05T09:00:01.123456+00:00", "error": None, "inputs": {"issue_number": n, "integration": "auto"}}
    d.update(over)
    path = repo / ".specify" / "workflows" / "runs" / run_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "state.json").write_text(json.dumps(d) if raw is None else raw, encoding="utf-8")
    return path / "state.json"


HOSTILE_IDS = ("../x", "-x", "a b", "\u00e9t\u00e9", "a\x00b", "x" * 10240, "", "a/b", "a\\b", ".", "..", "a\n", "run-\u0661", "_x",
               "a;b", "$(x)", "`x`", "a\tb", "x" * 129)


def test_valid_run_id_is_a_strict_ascii_allow_list():
    for good_id in ("run-1", "a", "A9_b-c", "x" * 128, "0abc"):
        assert approvals.valid_run_id(good_id), good_id
    for bad in HOSTILE_IDS + (None, 1, b"run-1", ["run-1"]):
        assert not approvals.valid_run_id(bad), bad


def test_run_id_is_validated_before_anything_else(repo4, capsys, monkeypatch):
    monkeypatch.setattr(config, "trusted", lambda *a, **k: (_ for _ in ()).throw(AssertionError("config read")))
    for bad in ("../x", "-x", "a b", "x" * 10240):
        assert _cli(None, "approvals", "sync", "--", bad) == 2, bad
        err = capsys.readouterr().err
        assert err.strip() == "skj: " + approvals.E_RUN_ID and bad[:20] not in err.replace("skj: ", "")
    with pytest.raises(approvals.ApprovalMissing):
        approvals.read_run(repo4, "../x")


def test_read_run_reads_the_spec_kit_state_file(repo4):
    _state(repo4)
    assert approvals.read_run(repo4, "run-1") == {"run_id": "run-1", "status": "paused", "step": "review-spec", "issue_number": 1}
    _state(repo4, status="created", step=None)
    assert approvals.read_run(repo4, "run-1")["step"] == ""
    _state(repo4, step="push-loop:approve-push:2")
    assert approvals.read_run(repo4, "run-1")["step"] == "push-loop:approve-push:2"


def test_state_json_is_untrusted(repo4, tmp_path_factory):
    bad = [dict(workflow_id="other-lifecycle"), dict(run_id="run-2"), dict(status="weird"), dict(status=1), dict(inputs={"issue_number": True}),
           dict(inputs={"issue_number": 1.0}), dict(inputs={"issue_number": "1"}), dict(inputs={"issue_number": 0}),
           dict(inputs={"issue_number": 10 ** 10}), dict(inputs={}), dict(inputs=[]), dict(updated_at="yesterday"),
           dict(current_step_id="../x"), dict(current_step_id="a:b"), dict(current_step_id=7), dict(current_step_id=None)]
    for over in bad:
        _state(repo4, **over)
        with pytest.raises(approvals.ApprovalMissing) as e:
            approvals.read_run(repo4, "run-1")
        assert str(e.value) == approvals.E_RUN, over
    for raw in ("{ not json", "[" * 100000 + "]" * 100000, '{"a": %s}' % ("9" * 5000), "\ufeff{}", "[]", "null", "x" * ((1 << 20) + 1)):
        _state(repo4, raw=raw)
        with pytest.raises(approvals.ApprovalMissing):
            approvals.read_run(repo4, "run-1")
    path = _state(repo4)
    real = path.with_name("real.json")
    os.rename(str(path), str(real))
    os.symlink(str(real), str(path))  # a symlinked state file
    with pytest.raises(approvals.ApprovalMissing):
        approvals.read_run(repo4, "run-1")
    os.remove(str(path))
    os.mkfifo(str(path))  # a FIFO is never waited on
    with pytest.raises(approvals.ApprovalMissing):
        approvals.read_run(repo4, "run-1")
    os.remove(str(path))
    os.mkdir(str(path))
    with pytest.raises(approvals.ApprovalMissing):
        approvals.read_run(repo4, "run-1")
    outside = tmp_path_factory.mktemp("outside")
    _state(outside, run_id="run-3")
    os.symlink(str(outside / ".specify/workflows/runs/run-3"), str(repo4 / ".specify/workflows/runs/run-3"))  # a symlinked run dir
    with pytest.raises(approvals.ApprovalMissing):
        approvals.read_run(repo4, "run-3")
    inside = _state(repo4 / "elsewhere", run_id="run-4").parent  # a valid run-4 state file inside the repository
    os.symlink(str(inside), str(repo4 / ".specify/workflows/runs/run-4"))  # a link to a directory inside the repository: refused too
    with pytest.raises(approvals.ApprovalMissing):
        approvals.read_run(repo4, "run-4")
    (repo4 / ".specify/workflows/runs/run-4").unlink()
    (repo4 / ".specify/workflows/runs/run-4").mkdir()
    (repo4 / ".specify/workflows/runs/run-4/state.json").write_text((inside / "state.json").read_text())
    assert approvals.read_run(repo4, "run-4")["run_id"] == "run-4"  # the same file in a real directory is read
    with pytest.raises(approvals.ApprovalMissing):
        approvals.read_run(repo4, "run-9")  # missing


def test_resume_argv_is_a_list():
    """A4-AC2: a list of plain str tokens, no shell: the fixed words, the validated run id, then one `--input` per verdict pair (this
    gate's verdict, every other gate's input reset to empty, so a stale verdict cannot pass a later gate)."""
    argv = approvals.resume_argv("run-1", "approve-push", "approve")
    assert type(argv) is list and all(type(x) is str for x in argv)
    assert argv[:4] == ["specify", "workflow", "resume", "run-1"]
    assert argv[4::2] == ["--input"] * 12
    assert argv[5] == "verdict_approve_push=approve"
    assert sorted(argv[7::2]) == sorted("verdict_%s=" % g.replace("-", "_") for g in GATES12 if g != "approve-push")
    for x in argv:
        assert not any(c in x for c in " \t\n\r;&|$`'\"\\<>(){}*?!#~\x00"), x
    assert approvals.resume_argv("run-1", "review-spec", "reject")[5] == "verdict_review_spec=reject"
    for args in [(bad, "approve-push", "approve") for bad in HOSTILE_IDS] + [("run-1", "nope", "approve"), ("run-1", "approve-push", "yes"),
                                                                          ("run-1", "approve-push", "approve; rm -rf x"), ("run-1", None, "approve")]:
        with pytest.raises(ValueError):
            approvals.resume_argv(*args)


class Spawn:
    def __init__(self, code=0, exc=None):
        self.code, self.exc, self.calls = code, exc, []

    def __call__(self, argv, cwd, env, timeout):
        self.calls.append({"argv": argv, "cwd": cwd, "env": env, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.code


@pytest.fixture
def poller(repo4, tmp_path_factory, monkeypatch):
    """A safe PATH with stand-ins for specify, skj and git ahead of the real one (the stand-ins are never run: `_spawn` is faked)."""
    bin_ = tmp_path_factory.mktemp("bin")
    for name in ("specify", "skj", "git"):
        exe = bin_ / name
        exe.write_text("#!/bin/sh\nexit 99\n")
        exe.chmod(0o755)
    real_git = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True).stdout.strip()
    os.remove(str(bin_ / "git"))
    os.symlink(real_git, str(bin_ / "git"))  # git itself is used by the tests and the checks
    monkeypatch.setenv("PATH", "%s%s%s" % (bin_, os.pathsep, os.environ.get("PATH", "")))
    spawn = Spawn()
    monkeypatch.setattr(approvals, "_spawn", spawn)
    w = type("Poller", (), {})()
    w.repo, w.bin, w.spawn = repo4, bin_, spawn
    return w


def _sync(f, w, resume=False, dry_run=False, run_id="run-1", trust=None):
    return approvals.sync(f.client, trust or _tr(), CFG4, run_id, w.repo, resume=resume, dry_run=dry_run)


def test_poller_waits_then_resumes_with_the_verdict(poller, monkeypatch):
    w = poller
    _state(w.repo)
    f = _fk()
    h = approvals.gate_hash("review-spec", KEY4, approvals.artifacts("review-spec", KEY4, w.repo))
    assert _sync(f, w, resume=True) == {"run": "run-1", "key": KEY4, "gate": "review-spec", "verdict": "waiting", "resumed": False,
                                        "hashes": {"review-spec": h}}
    assert w.spawn.calls == []
    _gets_only(f)
    _say(f, "/approve review-spec %s" % h)
    monkeypatch.setenv("SKJ_TOKEN", "FAKE-tok-should-not-pass")
    monkeypatch.setenv("SKJ_EMAIL", "dev.person@example.com")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "FAKE-other")
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE-agent-key")  # H1a fix round 1: the resumed agent steps keep their own credential
    f.log.clear()
    assert _sync(f, w, resume=True) == {"run": "run-1", "key": KEY4, "gate": "review-spec", "verdict": "approve", "resumed": True}
    _gets_only(f)
    (call,) = w.spawn.calls
    assert call["argv"] == [str(w.bin / "specify")] + approvals.resume_argv("run-1", "review-spec", "approve")[1:]
    assert type(call["argv"]) is list and call["cwd"] == str(w.repo) and call["timeout"] == approvals.RESUME_TIMEOUT
    env = call["env"]
    assert not any(k.startswith("SKJ_") or k.endswith("_TOKEN") or k == "GIT_DIR" for k in env)
    assert env["ANTHROPIC_API_KEY"] == "FAKE-agent-key"
    assert env["PATH"].split(os.pathsep)[0] == str(w.bin) and all(os.path.isabs(d) for d in env["PATH"].split(os.pathsep))
    _say(f, "/reject review-spec %s: not yet" % h)
    assert _sync(f, w, resume=True)["verdict"] == "reject" and w.spawn.calls[-1]["argv"][5] == "verdict_review_spec=reject"


def test_dry_run_and_a_run_that_is_not_paused_resume_nothing(poller):
    w = poller
    f = _fk()
    h = approvals.gate_hash("review-spec", KEY4, approvals.artifacts("review-spec", KEY4, w.repo))
    _say(f, "/approve review-spec %s" % h)
    _state(w.repo)
    assert _sync(f, w, resume=True, dry_run=True)["resumed"] is False
    for status in ("running", "completed", "failed", "created", "aborted"):
        _state(w.repo, status=status, step="review-spec" if status != "created" else None)
        assert _sync(f, w, resume=True) == {"run": "run-1", "gate": None, "verdict": "waiting", "paused": False}
    assert w.spawn.calls == []


def test_resume_needs_the_very_same_step_when_read_again(poller, monkeypatch):
    w = poller
    f = _fk()
    h = approvals.gate_hash("review-spec", KEY4, approvals.artifacts("review-spec", KEY4, w.repo))
    _say(f, "/approve review-spec %s" % h)
    _state(w.repo)
    real = approvals.read_run
    seen = []

    def moving(repo, run_id):
        seen.append(1)
        out = real(repo, run_id)
        return dict(out, step="review-plan") if len(seen) > 1 else out
    monkeypatch.setattr(approvals, "read_run", moving)
    assert _sync(f, w, resume=True)["resumed"] is False and w.spawn.calls == []


def test_paused_at_a_step_that_is_no_gate_is_refused(poller):
    w = poller
    for step in ("pull", "questions-loop", "x:approve-push", "approve-push:x:1", "loop:nope:1"):
        _state(w.repo, step=step)
        with pytest.raises(approvals.ApprovalMissing) as e:
            _sync(_fk(), w)
        assert str(e.value) in (approvals.E_SYNC_GATE, approvals.E_RUN), step


def test_the_poller_identity_never_counts_as_an_approver(poller):
    """A4-AC3: the poller reads with the token user, which is excluded (rule `excluded-author`) even when listed in approvers; so is
    every author of a `[skj:` kit comment. Its `/approve` of the right hash never resumes the run."""
    w = poller
    _state(w.repo)
    h = approvals.gate_hash("review-spec", KEY4, approvals.artifacts("review-spec", KEY4, w.repo))
    f = _fk()
    _say(f, "/approve review-spec %s" % h, who=ME_ID)
    _say(f, "[skj:approval-request 0badc0de]\n\nplease", who=APPROVER2)
    _say(f, "/approve review-spec %s" % h, who=APPROVER2)
    assert _sync(f, w, resume=True, trust=_tr(approvers=(APPROVER, APPROVER2, ME_ID)))["verdict"] == "waiting"
    assert w.spawn.calls == []
    rows_ = comments.fetch(f.client(), KEY4)
    skip = approvals.excluded(f.client(), rows_)
    assert [approvals.guard(r, [APPROVER, APPROVER2, ME_ID], skip) for r in rows_] == ["excluded-author"] * 3


def test_apply_gates_hash_the_plan_file_as_require_does(poller):
    w = poller
    _state(w.repo, step="push-loop:approve-push:1")
    f = _fk()
    with pytest.raises(approvals.ApprovalMissing) as e:
        _sync(f, w)
    assert str(e.value) == approvals.E_ARTIFACT  # no plan file yet
    (w.repo / ".skj").mkdir()
    (w.repo / ".skj" / "plan-push.md").write_bytes(PLAN)
    out = _sync(f, w)
    assert out["verdict"] == "waiting" and out["hashes"] == {"approve-push": _h("approve-push"), "approve-sync": _h("approve-sync")}
    _say(f, "/approve approve-sync %s" % _h("approve-sync"))  # any gate of the verb: what `push --apply` will accept
    assert _sync(f, w, resume=True)["verdict"] == "approve"
    assert approvals.require(f.client(), _tr(), CFG4, KEY4, "push", PLAN) is None
    assert w.spawn.calls[-1]["argv"][5] == "verdict_approve_push=approve"
    os.remove(str(w.repo / ".skj" / "plan-push.md"))
    os.symlink(str(w.repo / "specs/001-x/spec.md"), str(w.repo / ".skj" / "plan-push.md"))
    with pytest.raises(approvals.ApprovalMissing):
        _sync(f, w)


def test_wait_gate_resumes_on_jira_state(poller):
    w = poller
    _state(w.repo, step="wait-po-answers")
    f = _fk()
    _qpost(f)
    assert _sync(f, w, resume=True) == {"run": "run-1", "key": KEY4, "gate": "wait-po-answers", "verdict": "waiting", "resumed": False,
                                        "open": ["Q1"]}
    _say(f, "/answer Q1: yes")
    assert _sync(f, w, resume=True)["verdict"] == "approve" and len(w.spawn.calls) == 1


def test_resume_failures_are_fixed_texts(poller, monkeypatch):
    w = poller
    f = _fk()
    h = approvals.gate_hash("review-spec", KEY4, approvals.artifacts("review-spec", KEY4, w.repo))
    _say(f, "/approve review-spec %s" % h)
    _state(w.repo)
    for spawn, text in ((Spawn(code=3), approvals.E_RESUME.format(code=3)),
                        (Spawn(exc=subprocess.TimeoutExpired(["specify"], 1)), approvals.E_RESUME_TIMEOUT),
                        (Spawn(exc=OSError("boom /secret/path")), approvals.E_RESUME_MISSING)):
        monkeypatch.setattr(approvals, "_spawn", spawn)
        with pytest.raises(approvals.ResumeFailed) as e:
            _sync(f, w, resume=True)
        assert str(e.value) == text
    monkeypatch.setattr(approvals, "_spawn", w.spawn)
    os.remove(str(w.bin / "skj"))
    monkeypatch.setenv("PATH", "%s%s%s" % (w.bin, os.pathsep, "/nonexistent-dir"))
    with pytest.raises(approvals.ResumeFailed) as e:
        _sync(f, w, resume=True)
    assert str(e.value) == approvals.E_RESUME_TOOLS
    assert w.spawn.calls == []


def test_specify_is_found_only_on_a_safe_path(poller, monkeypatch):
    w = poller
    f = _fk()
    h = approvals.gate_hash("review-spec", KEY4, approvals.artifacts("review-spec", KEY4, w.repo))
    _say(f, "/approve review-spec %s" % h)
    _state(w.repo)
    planted = w.repo / "tools"
    planted.mkdir()
    for name in ("specify", "skj"):
        (planted / name).write_text("#!/bin/sh\nexit 0\n")
        (planted / name).chmod(0o755)
    os.remove(str(w.bin / "specify"))
    for path in (str(planted), "tools", "", ".", str(w.repo) + "/./tools"):  # inside the project, relative or empty: ignored
        monkeypatch.setenv("PATH", os.pathsep.join([path, str(w.bin)]))
        with pytest.raises(approvals.ResumeFailed) as e:
            _sync(f, w, resume=True)
        assert str(e.value) == approvals.E_RESUME_MISSING, path
    assert approvals._safe_path(w.repo).split(os.pathsep) == [str(w.bin)]
    assert w.spawn.calls == []


def test_the_poller_refuses_in_terminal_mode_with_no_request(poller, capsys):
    w = poller
    _state(w.repo)
    _target4(w.repo, {**BASE4, "approvers": [APPROVER], "approval": "terminal"})
    assert _cli(None, "approvals", "sync", "run-1", "--resume") == 2
    assert capsys.readouterr().err.strip() == "skj: " + approvals.E_POLL_TERMINAL
    with pytest.raises(approvals.ApprovalMissing):
        approvals.sync(None, _tr("terminal"), CFG4, "run-1", w.repo, resume=True)
    assert w.spawn.calls == []


def test_cli_sync_prints_the_verdict_and_exit_codes(poller, capsys, monkeypatch):
    w = poller
    _state(w.repo)
    f = _fk()
    assert _cli(f, "approvals", "sync", "run-1") == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "waiting"
    h = approvals.gate_hash("review-spec", KEY4, approvals.artifacts("review-spec", KEY4, w.repo))
    _say(f, "/approve review-spec %s" % h)
    assert _cli(f, "approvals", "sync", "run-1", "--resume", "--dry-run") == 0 and w.spawn.calls == []
    assert _cli(f, "approvals", "sync", "run-1", "--resume") == 0 and len(w.spawn.calls) == 1
    monkeypatch.setattr(approvals, "_spawn", Spawn(code=1))
    assert _cli(f, "approvals", "sync", "run-1", "--resume") == 1
    assert capsys.readouterr().err.strip().endswith(approvals.E_RESUME.format(code=1))
    _state(w.repo, n=2, step="wait-po-answers")  # a story the fake does not have
    assert _cli(f, "approvals", "sync", "run-1") == 2
    assert capsys.readouterr().err.strip() == "skj: " + approvals.E_UNREADABLE


def test_real_spawn_kills_the_whole_process_group_on_timeout(tmp_path):
    """The one subprocess seam, run for real with a local python child (never `specify`): no shell, stdin closed, output discarded, and
    on timeout the whole process group is killed (a grandchild too)."""
    pidfile = tmp_path / "pid"
    child = ("import subprocess, sys, time\n"
             "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
             "open(%r, 'w').write(str(p.pid))\n"
             "time.sleep(60)\n" % str(pidfile))
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        approvals._spawn([sys.executable, "-c", child], cwd=str(tmp_path), env={"PATH": os.environ.get("PATH", "")}, timeout=1.5)
    assert time.monotonic() - t0 < 10
    gpid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(gpid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the grandchild survived the timeout")
    assert approvals._spawn([sys.executable, "-c", "import sys; print('x' * 100000); sys.exit(4)"], cwd=str(tmp_path),
                            env={"PATH": os.environ.get("PATH", "")}, timeout=30) == 4


# ---- linear time on adversarial run files and comment lists ----------------------------------------------------------------------

def _secs(fn):
    # CPU time, not the wall clock (H1b): load slows the clock, not the work; the timed calls run no subprocess and never block
    old = signal.signal(signal.SIGPROF, _alarm)
    signal.setitimer(signal.ITIMER_PROF, _BOUND)
    t0 = time.process_time()
    try:
        fn()
    except _TooSlow:
        return _BOUND
    except (approvals.ApprovalMissing, ValueError):
        pass
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, old)
    return time.process_time() - t0


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs signal.setitimer")
def test_read_run_is_linear_on_adversarial_state_files(repo4):
    near = (1 << 20) - 64
    for raw in ("[" * near, "{\"a\":" * (near // 5), " " * near, "\"" + "\\u0000" * (near // 6) + "\"", "1" * near,
                json.dumps({"run_id": "run-1", "pad": "x" * near})[:near], "[" + "1," * (near // 2 - 1) + "1]"):
        _state(repo4, raw=raw)
        assert _secs(lambda: approvals.read_run(repo4, "run-1")) < 1.0


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs signal.setitimer")
def test_the_verdict_is_linear_over_many_comments():
    base = good()
    h = _h("approve-push")
    many = [base._replace(id=str(10000 + i), created=T, updated=T,
                          md=("/approve approve-push %s" % h) if i % 3 else ("/reject approve-%d" % (i % 7)))
            for i in range(20000)]
    b = approvals.board(many, [APPROVER], {ME_ID})
    hashes = {"approve-push": h, "approve-sync": _h("approve-sync")}
    assert _secs(lambda: approvals._verdict(b, hashes)) < 1.0


def test_in_ci_a_missing_target_branch_never_makes_check_or_sync_a_noop(tmp_path, monkeypatch, capsys):
    """Fix round 1: in CI with no origin/HEAD (the trust is the defaults, `base_known` False) `check` and `sync` refuse (exit 2, no
    client); outside CI the defaults (terminal) stand, unchanged."""
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    for k in ("SKJ_EMAIL", "SKJ_TOKEN", "CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(k, raising=False)
    _git4(tmp_path, "init", "-q", "-b", "main")
    _commit4(tmp_path, {"skj.config.json": json.dumps(BASE4)}, "no trust keys, no origin/HEAD")
    monkeypatch.chdir(tmp_path)
    assert _cli(None, "approvals", "check", KEY4, "--gate", "review-spec") == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "terminal"
    monkeypatch.setenv("CI", "true")
    assert _cli(None, "approvals", "check", KEY4, "--gate", "review-spec") == 2
    assert capsys.readouterr().err.strip() == "skj: " + approvals.E_CI_NO_TARGET
    assert _cli(None, "approvals", "sync", "run-1") == 2
    assert capsys.readouterr().err.strip() == "skj: " + approvals.E_CI_NO_TARGET
    unknown = config.Trust([], "terminal", {}, None, False)
    for call in (lambda: approvals.check(None, unknown, CFG4, KEY4, "review-spec", tmp_path),
                 lambda: approvals.sync(None, unknown, CFG4, "run-1", tmp_path)):
        with pytest.raises(approvals.ApprovalMissing) as e:
            call()
        assert str(e.value) == approvals.E_CI_NO_TARGET
    assert approvals.check(None, _tr("terminal"), CFG4, KEY4, "review-spec", tmp_path)["met"] is True  # a known terminal target: no-op


# ---- H1a fix round 1: the resume environment is git_env plus a named list of agent credentials, never a Jira or GitHub one --------

_AGENT = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
          "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "AWS_PROFILE")
_NEVER = ("SKJ_TOKEN", "SKJ_EMAIL", "SKJ_SITE", "JIRA_API_TOKEN", "JIRA_EMAIL", "ATLASSIAN_API_KEY", "ATLASSIAN_EMAIL", "GH_TOKEN",
          "GITHUB_TOKEN", "AWS_SESSION_TOKEN", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_EXEC_PATH", "GIT_DIR",
          "GIT_ASKPASS", "GIT_SSH_COMMAND", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "BASH_ENV", "RANDOM_VAR")


def test_h1a_resume_env_is_git_env_plus_the_named_agent_credentials():
    base = {"PATH": "/usr/bin", "HOME": "/h", **{k: "v-" + k for k in _AGENT + _NEVER}}
    env = approvals.resume_env("/safe/bin", base)
    assert {k: env[k] for k in _AGENT} == {k: "v-" + k for k in _AGENT}
    assert not set(env) & set(_NEVER)
    assert env["PATH"] == "/safe/bin" and env["HOME"] == "/h" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert not set(approvals.RESUME_AGENT_ENV) & set(_NEVER)
    assert not any(k.startswith(("SKJ_", "JIRA_", "ATLASSIAN_", "GH_", "GITHUB_", "GIT_")) for k in approvals.RESUME_AGENT_ENV)


# ---- DF1 (live run A10-27, D-019): the token's own account as the only approver can never approve: say so ----
def test_check_says_why_when_every_approver_is_the_token_user(repo4):
    h = approvals.gate_hash("review-spec", KEY4, approvals.artifacts("review-spec", KEY4, repo4))
    f = _fk()
    _say(f, "/approve review-spec %s" % h, who=ME_ID)  # the live run had the token user's own approval on the story (A10-26)
    with pytest.raises(approvals.ApprovalMissing) as e:
        _check(f, "review-spec", repo4, trust=_tr(approvers=(ME_ID,)))
    assert str(e.value) == approvals.E_SELF_ONLY and "never" in str(e.value) and ME_ID not in str(e.value)
    with pytest.raises(approvals.ApprovalMissing) as e:  # the apply gates too
        approvals.require(f.client(), _tr(approvers=(ME_ID,)), CFG4, KEY4, "push", b"plan\n")
    assert str(e.value) == approvals.E_SELF_ONLY
    with pytest.raises(approvals.ApprovalMissing) as e:  # another approver beside it: the ordinary waiting text
        _check(f, "review-spec", repo4, trust=_tr(approvers=(ME_ID, APPROVER)))
    assert str(e.value) != approvals.E_SELF_ONLY


def test_d023_origin_head_moved_to_a_weaker_branch_refuses_the_comment_mode_apply(repo4, capsys):
    """DF2 (live run A12-X7, D-023): the feature branch turns comment approval off, is pushed as its own branch and origin/HEAD is moved
    to it (`git remote set-head origin lv-x7`). The apply must not go through with no approval: it is refused before any write."""
    _git4(repo4, "checkout", "-q", "-b", "feature/SPK-1-x")
    _commit4(repo4, {"skj.config.json": json.dumps({**BASE4, "approval": "terminal"})}, "weaken")
    _git4(repo4, "update-ref", "refs/remotes/origin/lv-x7", "HEAD")
    _git4(repo4, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/lv-x7")
    f = _fk()
    plan = str(repo4 / "plan-q.md")
    assert _cli(f, "questions", KEY4, "--post", "--plan-out", plan) == 0
    capsys.readouterr()
    assert _cli(f, "questions", KEY4, "--post", "--apply", "--expect-plan", plan) != 0
    err = capsys.readouterr().err
    assert "target-moved" in err and "lv-x7" not in err
    assert f.writes() == []
    assert _cli(None, "approvals", "check", KEY4, "--gate", "review-spec") != 0  # read before any client is built
    assert "target-moved" in capsys.readouterr().err


def test_df3_note_removes_control_characters_through_the_shared_cleaner():
    """Review m4: `adf.safe_text` runs first in `_note` (D-011), so a control character is removed, never blanked; tab and line breaks
    are blanks. Without the call `ok\\x07ok` became `ok ok`."""
    assert approvals._note("ok\x07ok") == "okok" and approvals._note("ok\x1bok\x7fok") == "okokok"
    assert approvals._note("ok\tok\nok") == "ok ok ok" and approvals._note("a‮b​c") == "abc"
