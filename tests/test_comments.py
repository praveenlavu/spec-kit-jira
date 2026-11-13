"""R2: comment reads (`skj.comments.fetch`) and the `skj comments` CLI verb, driven through skj.client.Client
with the S9 fake as the opener. Fictional data only; no test touches a socket (conftest blocks them).
"""
import json
import urllib.parse

import pytest

from fakejira import EMAIL, ME_ID, OTHER_ID, FakeJira
from skj import adf
from skj import client as cl
from skj import comments
from skj.__main__ import main
from skj.issues import ReadError

FIELDS = ("id", "author", "author_type", "update_author", "created", "updated", "visibility", "md",
          "unknown_nodes", "digest")
SECRETS = ("Fictional Me", "Fictional Other", EMAIL, "other.person@example.com", "avatarUrls", "https://",
           "Hidden Name")


class Rec:
    def __init__(self, fake):
        self.fake, self.reqs = fake, []

    def __call__(self, method, url, headers, body, timeout):
        p = urllib.parse.urlsplit(url)
        self.reqs.append((method, p.path[len("/rest/api/3"):], dict(urllib.parse.parse_qsl(p.query))))
        return self.fake(method, url, headers, body, timeout)


def make(**kw):
    fake = FakeJira(**kw)
    rec = Rec(fake)
    return fake, rec, cl.Client(fake.site, fake.email, fake.token, opener=rec, sleep=fake.sleeps.append)


class Canned:
    """An opener that answers each request with the next canned page (the last one repeats)."""

    def __init__(self, pages):
        self.pages, self.n = list(pages), 0

    def __call__(self, method, url, headers, body, timeout):
        page = self.pages[min(self.n, len(self.pages) - 1)]
        self.n += 1
        return 200, {"Content-Type": "application/json"}, json.dumps(page).encode()


def canned(*pages):
    op = Canned(pages)
    return op, cl.Client("example-site.atlassian.net", EMAIL, "FAKE-tok", opener=op, sleep=lambda s: None)


def para(*texts):
    return {"version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": t} for t in texts]}]}


def user(aid=ME_ID, atype="atlassian"):
    return {"accountId": aid, "accountType": atype, "displayName": "Hidden Name", "emailAddress": "x@example.com"}


def raw(cid="10000", **over):
    c = {"id": cid, "author": user(), "updateAuthor": user(), "body": para("hello"),
         "created": "2026-01-05T09:00:00.000+0000", "updated": "2026-01-05T09:00:00.000+0000", "jsdPublic": True}
    c.update(over)
    return c


def page(items, start=0, total=None):
    return {"startAt": start, "maxResults": 100, "total": len(items) if total is None else total, "comments": items}


# --- R2-AC1: the Row contract ------------------------------------------------------------------------------

def test_row_has_only_listed_fields():
    fake, rec, c = make()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, para("/approve"))
    rows = comments.fetch(c, key)
    assert comments.Row._fields == FIELDS
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, comments.Row) and len(r) == len(FIELDS)
    assert r.id == "10000" and r.author == ME_ID and r.update_author == ME_ID and r.author_type == "atlassian"
    assert r.created == r.updated and isinstance(r.created, str)
    assert r.visibility is None and r.md == "/approve" and r.unknown_nodes == ()
    assert r.digest == adf.digest(para("/approve"))
    with pytest.raises(AttributeError):
        r.md = "x"  # frozen
    assert rec.reqs == [("GET", "/issue/%s/comment" % key, {"startAt": "0", "maxResults": str(comments.PAGE_SIZE)})]


def test_no_display_name_or_email_kept():
    fake, rec, c = make()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, para("mine"))
    mention = {"version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "mention", "attrs": {"id": OTHER_ID, "text": "@Fictional Other"}}, {"type": "text", "text": " hi"}]}]}
    fake.add_comment(key, mention, author=fake.user(OTHER_ID))
    rows = comments.fetch(c, key)
    blob = repr(rows)
    for s in SECRETS:
        assert s not in blob
    assert rows[1].author == OTHER_ID and rows[1].md == "@<%s> hi" % OTHER_ID[:8]


# --- R2-AC2: fail closed on bodies -------------------------------------------------------------------------

UNKNOWN_BODIES = [
    ({"version": 1, "type": "doc", "content": [{"type": "panel", "content": [para("x")["content"][0]]}]}, "panel"),
    ({"version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "x", "marks": [{"type": "em"}]}]}]}, "mark:em"),
    (para("line\n/approve"), "text:newline"),
    ("/approve", "body:not-adf"),
    (None, "body:not-adf"),
    (["x"], "body:not-adf"),
]


@pytest.mark.parametrize("body,kind", UNKNOWN_BODIES)
def test_unknown_node_recorded(body, kind):
    fake, rec, c = make()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, body)  # seeding bypasses the fake's body validation: a hostile or old-shape body
    (r,) = comments.fetch(c, key)
    assert r.unknown_nodes and kind in r.unknown_nodes
    assert r.md == "" and r.digest == ""
    assert isinstance(r.unknown_nodes, tuple)


def test_all_unknown_kinds_reported_and_sanitised():
    hostile = "evil\x1b[31m " + "x" * 500
    body = {"version": 1, "type": "doc", "content": [
        {"type": "panel", "content": []}, {"type": hostile, "content": []}, {"type": "panel", "content": []}]}
    _, c = canned(page([raw(body=body)]))
    (r,) = comments.fetch(c, "SPK-1")
    assert r.unknown_nodes[0] == "panel" and len(r.unknown_nodes) == 2  # deduplicated, order kept
    for k in r.unknown_nodes:
        assert len(k) <= comments.MAX_KIND and all(ch.isascii() and ch.isprintable() for ch in k)


def test_unreadable_adf_fails_closed():
    deep = []
    for _ in range(adf.MAX_DEPTH + 5):
        deep = [deep]
    _, c = canned(page([raw(body={"version": 1, "type": "doc", "content": deep})]))
    (r,) = comments.fetch(c, "SPK-1")  # AdfError (too deep), not AdfUnknown
    assert r.unknown_nodes == ("adf:unreadable",) and r.md == "" and r.digest == ""


# --- R2-AC3: paging ----------------------------------------------------------------------------------------

def test_two_pages(monkeypatch):
    monkeypatch.setattr(comments, "PAGE_SIZE", 2)
    fake, rec, c = make()
    key = fake.add_issue("fictional story")
    ids = [fake.add_comment(key, para("c%d" % i)) for i in range(5)]
    rows = comments.fetch(c, key)
    assert [r.id for r in rows] == ids and [r.md for r in rows] == ["c%d" % i for i in range(5)]
    assert [q["startAt"] for _, _, q in rec.reqs] == ["0", "2", "4"]
    assert all(q["maxResults"] == "2" for _, _, q in rec.reqs)


def test_no_comments():
    fake, rec, c = make()
    key = fake.add_issue("fictional story")
    assert comments.fetch(c, key) == []
    assert len(rec.reqs) == 1


def test_duplicate_id_across_pages_refused():
    _, c = canned(page([raw("10000")], 0, 2), page([raw("10000")], 1, 2))
    with pytest.raises(ReadError, match="duplicate"):
        comments.fetch(c, "SPK-1")


def test_duplicate_id_on_one_page_refused():
    _, c = canned(page([raw("10000"), raw("10000")]))
    with pytest.raises(ReadError, match="duplicate"):
        comments.fetch(c, "SPK-1")


def test_empty_page_while_total_says_more_stops():
    _, c = canned(page([raw("10000")], 0, 5), page([], 1, 5))
    with pytest.raises(ReadError, match="no progress"):
        comments.fetch(c, "SPK-1")


def test_total_changing_between_pages_refused():
    _, c = canned(page([raw("10000")], 0, 3), page([raw("10001")], 1, 2))
    with pytest.raises(ReadError, match="changed"):
        comments.fetch(c, "SPK-1")


def test_page_cap(monkeypatch):
    monkeypatch.setattr(comments, "MAX_PAGES", 3)
    pages = [page([raw(str(10000 + i))], i, 10 ** 6) for i in range(5)]
    op, c = canned(*pages)
    with pytest.raises(ReadError, match="page cap"):
        comments.fetch(c, "SPK-1")
    assert op.n == 3


HOSTILE_PAGES = [
    [],
    "x",
    None,
    {"startAt": 0, "total": 1},
    {"startAt": 0, "total": 1, "comments": "x"},
    {"startAt": 0, "total": 1, "comments": {}},
    {"startAt": 0, "comments": []},
    {"startAt": 0, "total": True, "comments": []},
    {"startAt": 0, "total": -1, "comments": []},
    {"startAt": 0, "total": "1", "comments": []},
    {"startAt": 0, "total": 1.0, "comments": []},
    {"startAt": 5, "total": 1, "comments": [raw()]},
    {"startAt": "0", "total": 1, "comments": [raw()]},
    {"startAt": False, "total": 1, "comments": [raw()]},
    {"startAt": 0, "total": 1, "comments": ["x"]},
    {"startAt": 0, "total": 1, "comments": [None]},
]


@pytest.mark.parametrize("bad", HOSTILE_PAGES)
def test_hostile_page_shapes_refused(bad):
    _, c = canned(bad)
    with pytest.raises(ReadError):
        comments.fetch(c, "SPK-1")


BAD_COMMENTS = [
    raw("01"), raw("0"), raw("../x"), raw(""), raw(10000), raw(None), raw("1\n"), raw("١"), raw("1" * 20),
    raw(created=None), raw(created="yesterday"), raw(created="2026-01-05T09:00:00.000+0000\n"),
    raw(updated=5), raw(updated="2026-01-05 09:00:00"), raw(created="٢026-01-05T09:00:00.000+0000"),
]


@pytest.mark.parametrize("bad", BAD_COMMENTS)
def test_malformed_comment_refuses_the_whole_read(bad):
    _, c = canned(page([raw("10001"), bad]))
    with pytest.raises(ReadError) as e:
        comments.fetch(c, "SPK-1")
    assert "Hidden" not in str(e.value) and "hello" not in str(e.value)


def test_missing_id_key_refused():
    bad = raw()
    del bad["id"]
    _, c = canned(page([bad]))
    with pytest.raises(ReadError):
        comments.fetch(c, "SPK-1")


@pytest.mark.parametrize("bad", ["spk-1", "SPK-0", "SPK-01", "SPK-1;touch x", "", None, 5, "SPK-1\n", "../x"])
def test_key_validated_before_any_request(bad):
    op, c = canned(page([]))
    with pytest.raises(ValueError):
        comments.fetch(c, bad)
    assert op.n == 0


def test_missing_issue_raises_not_found():
    fake, rec, c = make()
    with pytest.raises(cl.NotFound):
        comments.fetch(c, "SPK-99")


# --- R2-AC4: oversize --------------------------------------------------------------------------------------

def test_oversize_refused():
    fake, rec, c = make()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, para("ok"))
    big = fake.add_comment(key, para("SECRETBODY" + "x" * adf.MAX_BODY))
    with pytest.raises(comments.CommentTooLarge) as e:
        comments.fetch(c, key)
    assert isinstance(e.value, ReadError)
    assert str(e.value) == "comment %s is larger than the 1 MiB local cap; the comments were not read" % big
    assert "SECRETBODY" not in str(e.value)


def test_oversize_string_body_refused():
    _, c = canned(page([raw(body="y" * (adf.MAX_BODY + 1))]))
    with pytest.raises(comments.CommentTooLarge):
        comments.fetch(c, "SPK-1")


def test_body_just_under_cap_is_read():
    body = para("")
    body["content"][0]["content"][0]["text"] = "x" * (adf.MAX_BODY - len(json.dumps(para(""), separators=(",", ":"))))
    _, c = canned(page([raw(body=body)]))
    (r,) = comments.fetch(c, "SPK-1")
    assert r.unknown_nodes == () and len(r.md) > 1000


# --- authors -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("author", [
    None, "x", [], {}, {"accountType": "atlassian"}, {"accountId": "has space", "accountType": "atlassian"},
    {"accountId": "a" * 129, "accountType": "atlassian"}, {"accountId": 5, "accountType": "atlassian"},
    {"accountId": "", "accountType": "atlassian"}, {"accountId": ME_ID + "\n", "accountType": "atlassian"},
])
def test_author_without_valid_account_id_is_none(author):
    _, c = canned(page([raw(author=author, updateAuthor=author)]))
    (r,) = comments.fetch(c, "SPK-1")
    assert r.author is None and r.update_author is None
    assert "Hidden" not in repr(r)


def test_missing_author_keys():
    item = raw()
    del item["author"], item["updateAuthor"]
    _, c = canned(page([item]))
    (r,) = comments.fetch(c, "SPK-1")
    assert (r.author, r.author_type, r.update_author) == (None, None, None)


@pytest.mark.parametrize("atype,want", [("app", "app"), ("customer", "customer"), ("atlassian", "atlassian"),
                                        (None, None), (5, None), ("", None), ("x\n", None), ("a" * 40, None),
                                        ("At lassian", None)])
def test_author_type_recorded_or_none(atype, want):
    _, c = canned(page([raw(author=user(atype=atype))]))
    (r,) = comments.fetch(c, "SPK-1")
    assert r.author == ME_ID and r.author_type == want


def test_app_author_keeps_its_id_and_type():
    fake, rec, c = make()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, para("/approve"), author=fake.user("557058:app-fictional", "app"))
    (r,) = comments.fetch(c, key)
    assert (r.author, r.author_type) == ("557058:app-fictional", "app")


# --- visibility --------------------------------------------------------------------------------------------

def test_visibility_present():
    fake, rec, c = make()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, para("x"), visibility={"type": "role", "value": "Administrators"})
    (r,) = comments.fetch(c, key)
    assert r.visibility == ("role", "Administrators")


@pytest.mark.parametrize("vis", [{}, "x", 5, [], True, {"type": "role"}, {"value": "x"}, {"type": "x", "value": "y"},
                                 {"type": "group", "value": 5}, {"type": "group", "value": ""},
                                 {"type": "group", "value": "a\x1bb"}, {"type": "group", "value": "v" * 300}])
def test_odd_visibility_never_none(vis):
    _, c = canned(page([raw(visibility=vis)]))
    (r,) = comments.fetch(c, "SPK-1")
    assert r.visibility is not None and r.visibility == comments.VIS_ODD


@pytest.mark.parametrize("jsd", [False, None, "true", 0])
def test_internal_or_odd_jsd_flag_is_a_restriction(jsd):
    _, c = canned(page([raw(jsdPublic=jsd)]))
    (r,) = comments.fetch(c, "SPK-1")
    assert r.visibility == comments.VIS_ODD


def test_null_visibility_and_absent_jsd_flag_are_none():
    item = raw(visibility=None)
    del item["jsdPublic"]
    _, c = canned(page([item]))
    (r,) = comments.fetch(c, "SPK-1")
    assert r.visibility is None


# --- edits and digests -------------------------------------------------------------------------------------

def test_foreign_edit_shows():
    fake, rec, c = make()
    key = fake.add_issue("fictional story")
    cid = fake.add_comment(key, para("/approve"), author=fake.user(OTHER_ID))
    (before,) = comments.fetch(c, key)
    fake.edit_comment(cid, para("/approve edited"), editor=fake.user(ME_ID))  # SIMULATED (U2)
    (after,) = comments.fetch(c, key)
    assert before.created == before.updated and before.update_author == OTHER_ID
    assert after.created == before.created and after.updated != after.created
    assert after.author == OTHER_ID and after.update_author == ME_ID
    assert after.digest != before.digest


def test_own_edit_through_rest_shows():
    fake, rec, c = make()
    key = fake.add_issue("fictional story")
    cid = fake.add_comment(key, para("first"))
    (before,) = comments.fetch(c, key)
    c.request("PUT", "/issue/%s/comment/%s" % (key, cid), body={"body": para("second")})
    (after,) = comments.fetch(c, key)
    assert after.updated != after.created and after.update_author == ME_ID == after.author
    assert after.digest != before.digest and after.md == "second"


def test_digest_stable_across_reads():
    fake, rec, c = make()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, para("/approve"))
    assert comments.fetch(c, key) == comments.fetch(c, key)


def test_quoted_and_unquoted_digests_differ():
    quoted = {"version": 1, "type": "doc", "content": [{"type": "blockquote", "content": para("/approve")["content"]}]}
    _, c = canned(page([raw("10000", body=para("/approve")), raw("10001", body=quoted)]))
    plain, q = comments.fetch(c, "SPK-1")
    assert plain.digest and q.digest and plain.digest != q.digest
    assert plain.md == "/approve" and q.md == "> /approve"


# --- short_id and the CLI ----------------------------------------------------------------------------------

def test_short_id():
    assert comments.short_id(ME_ID) == ME_ID[:8] + "..."
    assert comments.short_id("abc") == "abc"
    assert comments.short_id(None) == "?"


@pytest.fixture(autouse=True)
def _spk_project(tmp_path, monkeypatch):
    """The CLI verbs read the configured project (D-021): every test runs in a folder configured for SPK."""
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)


def cli(argv, fake):
    return main(argv, client_factory=lambda: fake.client())


def test_cli_comments_output_cuts_account_ids(capsys):
    fake = FakeJira()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, para("BODYTEXT one"))
    cid = fake.add_comment(key, para("BODYTEXT two"), author=fake.user(OTHER_ID),
                           visibility={"type": "group", "value": "fictional-group"})
    fake.edit_comment(cid, para("BODYTEXT two edited"), editor=fake.user(OTHER_ID))
    assert cli(["comments", key], fake) == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert len(lines) == 2 and err == ""
    assert ME_ID[:8] + "..." in lines[0] and OTHER_ID[:8] + "..." in lines[1]
    for s in (ME_ID, OTHER_ID, "BODYTEXT", "fictional-group") + SECRETS:
        assert s not in out
    assert "edited=no" in lines[0] and "visibility=no" in lines[0] and "unknown=0" in lines[0]
    assert "edited=yes" in lines[1] and "visibility=yes" in lines[1]
    assert lines[0].startswith("10000 ")


def test_cli_md_flag_prints_bodies_only_when_asked(capsys):
    fake = FakeJira()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, para("BODYTEXT \x1b[31mred"))
    fake.add_comment(key, {"version": 1, "type": "doc", "content": [{"type": "panel", "content": []}]})
    assert cli(["comments", key, "--md"], fake) == 0
    out = capsys.readouterr().out
    assert "    BODYTEXT ?[31mred" in out and "\x1b" not in out
    assert "unknown=1" in out
    assert ME_ID not in out


def test_cli_refuses_bad_key_before_building_a_client(capsys):
    called = []
    assert main(["comments", "spk-1"], client_factory=lambda: called.append(1)) == 2
    assert not called and "not an issue number or a key of project SPK" in capsys.readouterr().err


def test_cli_reports_read_errors(capsys):
    fake = FakeJira()
    assert cli(["comments", "SPK-404"], fake) == 1
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("skj: ") and fake.token not in err and EMAIL not in err


def test_cli_reports_oversize(capsys):
    fake = FakeJira()
    key = fake.add_issue("fictional story")
    cid = fake.add_comment(key, para("x" * (adf.MAX_BODY + 1)))
    assert cli(["comments", key], fake) == 1
    assert capsys.readouterr().err == "skj: comment %s is larger than the 1 MiB local cap; the comments were not read\n" % cid


def test_cli_without_command_still_succeeds():
    assert main([]) == 0


def test_h1a_ac3_md_output_replaces_bidi_and_invisible_characters(capsys):
    fake = FakeJira()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, para("ok‮evil‬ zero​width ⁦iso⁩"))
    assert cli(["comments", key, "--md"], fake) == 0
    out = capsys.readouterr().out
    assert "    ok�evil� zero�width �iso�" in out
    assert not any(c in out for c in "‮‬​⁦⁩")


# ---- DF1 (live runs A2-09, A3-12, A3-13: D-006, D-008) ----
def test_cli_md_names_what_it_cannot_show_instead_of_printing_nothing(capsys):
    """A3-12/A3-13: a comment with a table, or a code block whose text holds ```, printed `unknown=1` and no body at all. Hand-written
    from the live shapes (comments 10112 and 10113 of the evidence)."""
    fake = FakeJira()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, {"version": 1, "type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "LV comment with a table"}]},
        {"type": "table", "attrs": {"isNumberColumnEnabled": False, "layout": "default"}, "content": [{"type": "tableRow", "content": [
            {"type": "tableCell", "attrs": {}, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "a1"}]}]}]}]}]})
    fake.add_comment(key, {"version": 1, "type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "see"}]},
        {"type": "codeBlock", "attrs": {"language": "text"}, "content": [{"type": "text", "text": "a\n```\nb \x1b[31m"}]}]})
    assert cli(["comments", key, "--md"], fake) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    shown = [x for x in lines if x.startswith("    [skj: body not shown")]
    assert len(shown) == 2 and "table" in shown[0] and "codeBlock:fence" in shown[1]
    assert "LV comment" not in out and "\x1b" not in out


def test_cli_md_says_in_band_that_the_bodies_are_untrusted(capsys):
    fake = FakeJira()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, para("BODYTEXT"))
    assert cli(["comments", key, "--md"], fake) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == comments.UNTRUSTED_LINE and "untrusted" in comments.UNTRUSTED_LINE
    assert cli(["comments", key], fake) == 0
    assert comments.UNTRUSTED_LINE not in capsys.readouterr().out  # without bodies there is nothing to warn about


# ---- DF2 (live run A12-X1, D-021): every verb that takes an issue ref keeps the configured project's boundary ----
def _ref_verbs():
    """Every (sub)command path whose positional is an issue ref, with dummy values for its required options."""
    from skj.__main__ import build_parser

    def walk(parser, path):
        subs = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
        for s in subs:
            for name, p in s.choices.items():
                yield from walk(p, path + [name])
        pos = [a.dest for a in parser._actions if not a.option_strings and a.__class__.__name__ != "_SubParsersAction"]
        if pos and pos[0] in ("ref", "key"):
            req = [x for a in parser._actions if a.option_strings and a.required
                   for x in (a.option_strings[0], {"--gate": "review-spec"}.get(a.option_strings[0], sorted(a.choices)[0] if a.choices else "x"))]
            yield path, req
    return list(walk(build_parser(), []))


def test_every_ref_verb_refuses_another_projects_key_before_any_request(capsys):
    verbs = _ref_verbs()
    assert ["comments"] in [p for p, _ in verbs] and len(verbs) >= 17
    for path, req in verbs:
        called = []
        assert main([*path, "ABC-1", *req], client_factory=lambda: called.append(1)) == 2, path
        err = capsys.readouterr().err
        assert not called and "not an issue number or a key of project SPK" in err, (path, err)


def test_comments_takes_the_issue_number_form(capsys):
    fake = FakeJira()
    key = fake.add_issue("fictional story")
    fake.add_comment(key, para("BODYTEXT"))
    assert cli(["comments", key.split("-")[1]], fake) == 0
    assert capsys.readouterr().out.startswith("10000 ")
