"""S9: the fake Jira. One test per row of the 2026-11-08 live check (rows 1 to 8; the row
number is in the test name), the request log (S9-AC2) and the only-transport scan (S9-AC3).

Most tests drive the fake THROUGH skj.client.Client (the real transport code path, the fake as opener).
Behaviour marked SIMULATED or SYNTHETIC in tests/fakejira.py was never observed live (U2, U4; attachment upload ran live once, LV2).
"""
import ast
import base64
import datetime
import json
from pathlib import Path

import pytest

from fakejira import EMAIL, OTHER_ID, SITE, TOKEN, FakeJira
from skj import client as cl
from skj import keys

TESTS = Path(__file__).resolve().parent
API = "https://%s/rest/api/3" % SITE
B64 = base64.b64encode(("%s:%s" % (EMAIL, TOKEN)).encode()).decode()


def doc(text):
    return {"version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


@pytest.fixture
def fake():
    return FakeJira()


@pytest.fixture
def c(fake):
    return fake.client()


def raw(fake, method, path, *, auth="good", body=None, headers=None):
    """Call the opener directly (no Client): for cases the Client never sends."""
    h = dict(headers or {})
    if auth == "good":
        h["Authorization"] = "Basic " + B64
    elif auth is not None:
        h["Authorization"] = auth
    status, rh, stream = fake.opener(method, API + path, h, body, 30)
    assert isinstance(stream, bytes)
    return status, rh, stream


def errors_of(exc):
    return exc.value.status, exc.value.messages


# --- row 1: auth and identity --------------------------------------------------------------------


def test_row1_myself_200_atlassian_own_email_visible(c, fake):
    r = c.request("GET", "/myself")
    assert r.status == 200
    me = r.data
    assert {"accountId", "accountType", "active", "displayName", "emailAddress", "self"} <= set(me)
    assert me["accountType"] == "atlassian" and me["accountId"] == fake.account_id
    assert me["emailAddress"] == EMAIL  # own email is visible (row 1)


def test_row1_myself_account_type_configurable():
    f = FakeJira(account_type="app")
    assert f.client().request("GET", "/myself").data["accountType"] == "app"


def test_row1_no_credentials_401_text_without_login_reason(fake):
    status, h, body = raw(fake, "GET", "/myself", auth=None)
    assert status == 401
    assert body == b"Client must be authenticated to access this resource."
    assert "WWW-Authenticate" in h and "X-Seraph-LoginReason" not in h


def test_row1_bad_credentials_401_authenticated_failed(fake):
    bad = cl.Client(SITE, EMAIL, "FAKE-wrong-token-NOTREAL", opener=fake, sleep=lambda s: None)
    with pytest.raises(cl.AuthError) as e:
        bad.request("GET", "/myself")
    assert e.value.status == 401 and e.value.login_reason == "AUTHENTICATED_FAILED"
    assert len(fake.log) == 1  # never retried


@pytest.mark.parametrize("auth", [
    "Bearer " + B64,
    "Basic !!!not-base64!!!",
    "Basic " + base64.b64encode(b"no-colon-here").decode(),
    "Basic " + base64.b64encode(("other@example.com:" + TOKEN).encode()).decode(),
    "Basic " + base64.b64encode(b"\xff\xfe:\xff").decode(),
])
def test_row1_basic_header_is_decoded_and_checked(fake, auth):
    status, h, _ = raw(fake, "GET", "/myself", auth=auth)
    assert status == 401 and h["X-Seraph-LoginReason"] == "AUTHENTICATED_FAILED"


def test_row1_anonymous_project_is_404_no_existence_leak(fake):
    status, _, body = raw(fake, "GET", "/project/SPK", auth=None)
    assert status == 404
    assert json.loads(body) == {"errorMessages": ["No project could be found with key 'SPK'."], "errors": {}}


def test_row1_project_is_team_managed(c):
    p = c.request("GET", "/project/SPK").data
    assert (p["key"], p["style"], p["simplified"], p["projectTypeKey"]) == ("SPK", "next-gen", True, "software")


def test_row1_mypermissions_free_owner_defaults_and_configurable(c):
    want = "EDIT_ALL_COMMENTS,DELETE_ALL_COMMENTS,TRANSITION_ISSUES,LINK_ISSUES,ADMINISTER,SYSTEM_ADMIN"
    perms = c.request("GET", "/mypermissions", params={"projectKey": "SPK", "permissions": want}).data["permissions"]
    assert set(perms) == set(want.split(","))
    assert {k: v["havePermission"] for k, v in perms.items()} == {
        "EDIT_ALL_COMMENTS": True, "DELETE_ALL_COMMENTS": True, "TRANSITION_ISSUES": True,
        "LINK_ISSUES": True, "ADMINISTER": True, "SYSTEM_ADMIN": False}
    f = FakeJira(permissions={"EDIT_ALL_COMMENTS": False, "DELETE_ALL_COMMENTS": False})
    p2 = f.client().request("GET", "/mypermissions", params={"permissions": "EDIT_ALL_COMMENTS,DELETE_ALL_COMMENTS"})
    assert [v["havePermission"] for v in p2.data["permissions"].values()] == [False, False]


# --- row 2: issue model ----------------------------------------------------------------------------


def _create(c, summary, itype, parent=None, **fields):
    f = {"project": {"key": "SPK"}, "issuetype": {"name": itype}, "summary": summary, **fields}
    if parent:
        f["parent"] = {"key": parent}
    r = c.request("POST", "/issue", body={"fields": f})
    assert r.status == 201 and set(r.data) == {"id", "key", "self"}
    return r.data


def test_row2_hierarchy_levels_of_the_four_types(c):
    levels = {}
    for t in ("Epic", "Story", "Task"):
        k = _create(c, "fictional %s" % t, t)["key"]
        it = c.request("GET", "/issue/%s" % k, params={"fields": "issuetype"}).data["fields"]["issuetype"]
        levels[t] = (it["hierarchyLevel"], it["subtask"])
    st = _create(c, "fictional subtask", "Subtask", parent="SPK-2")["key"]
    it = c.request("GET", "/issue/%s" % st, params={"fields": "issuetype"}).data["fields"]["issuetype"]
    levels["Subtask"] = (it["hierarchyLevel"], it["subtask"])
    assert levels == {"Epic": (1, False), "Story": (0, False), "Task": (0, False), "Subtask": (-1, True)}


def test_row2_story_under_epic_reads_back_parent_shape(c):
    epic = _create(c, "fictional epic", "Epic")
    story = _create(c, "fictional story", "Story", parent=epic["key"])
    parent = c.request("GET", "/issue/%s" % story["key"], params={"fields": "parent"}).data["fields"]["parent"]
    assert (parent["id"], parent["key"]) == (epic["id"], epic["key"])
    assert parent["self"] == "%s/issue/%s" % (API, epic["id"])
    assert set(parent["fields"]) == {"summary", "status", "priority", "issuetype"}


def test_row2_subtask_under_story_is_listed_in_subtasks(c):
    story = _create(c, "fictional story", "Story")
    sub = _create(c, "T001 fictional task", "Subtask", parent=story["key"], labels=["speckit"])
    subs = c.request("GET", "/issue/%s" % story["key"], params={"fields": "subtasks"}).data["fields"]["subtasks"]
    assert [(s["id"], s["key"]) for s in subs] == [(sub["id"], sub["key"])]
    assert subs[0]["fields"]["summary"] == "T001 fictional task"


def test_row2_subtask_without_parent_is_400(c):
    with pytest.raises(cl.JiraError) as e:
        c.request("POST", "/issue", body={"fields": {"project": {"key": "SPK"}, "issuetype": {"name": "Subtask"},
                                                     "summary": "orphan"}})
    assert e.value.status == 400


def test_row2_case_insensitive_key_returns_stored_key(c, fake):
    key = fake.add_issue("fictional story")
    r = c.request("GET", "/issue/%s" % key.lower())
    assert r.status == 200 and r.data["key"] == key == "SPK-1"
    by_id = c.request("GET", "/issue/%s" % r.data["id"])
    assert by_id.data["key"] == key  # numeric id read


def test_row2_moved_issue_answers_with_its_new_key_simulated(c, fake):
    """SIMULATED from [OAS] "check for moved issues"; never observed live."""
    key = fake.add_issue("fictional story")
    fake.move_issue(key, "SPK-40")
    assert c.request("GET", "/issue/%s" % key).data["key"] == "SPK-40"


# --- row 3: workflow ---------------------------------------------------------------------------------


def test_row3_four_transitions_listed_including_current(c, fake):
    key = fake.add_issue("fictional story")
    ts = c.request("GET", "/issue/%s/transitions" % key, params={"expand": "transitions.fields"}).data["transitions"]
    got = [(t["id"], t["name"], t["to"]["statusCategory"]["key"], t["hasScreen"], t["isConditional"], t["fields"])
           for t in ts]
    assert got == [("11", "To Do", "new", False, False, {}), ("21", "In Progress", "indeterminate", False, False, {}),
                   ("31", "In Review", "indeterminate", False, False, {}), ("41", "Done", "done", False, False, {})]
    current = c.request("GET", "/issue/%s" % key, params={"fields": "status"}).data["fields"]["status"]["name"]
    assert current == "To Do" and current in [t["name"] for t in ts]
    plain = c.request("GET", "/issue/%s/transitions" % key).data["transitions"]
    assert all("fields" not in t for t in plain)  # fields only with expand=transitions.fields


def test_row3_status_put_is_400(c, fake):
    key = fake.add_issue("fictional story")
    with pytest.raises(cl.JiraError) as e:
        c.request("PUT", "/issue/%s" % key, body={"fields": {"status": {"name": "Done"}}})
    assert errors_of(e) == (400, ["status: Field 'status' cannot be set. It is not on the appropriate screen, or unknown."])
    assert fake.issue(key)["status"] == "To Do"


def test_row3_bogus_transition_id_is_400(c, fake):
    key = fake.add_issue("fictional story")
    with pytest.raises(cl.JiraError) as e:
        c.request("POST", "/issue/%s/transitions" % key, body={"transition": {"id": "999999"}})
    assert errors_of(e) == (400, ["Transition id '999999' is not valid for this issue."])


def test_row3_transition_204_then_category_done_resolution_null(c, fake):
    key = fake.add_issue("fictional story")
    for tid, name, cat in (("21", "In Progress", "indeterminate"), ("41", "Done", "done")):
        r = c.request("POST", "/issue/%s/transitions" % key, body={"transition": {"id": tid}})
        assert (r.status, r.data) == (204, None)
        f = c.request("GET", "/issue/%s" % key, params={"fields": "status,resolution"}).data["fields"]
        assert (f["status"]["name"], f["status"]["statusCategory"]["key"], f["resolution"]) == (name, cat, None)
    items = [i for v in c.request("GET", "/issue/%s/changelog" % key).data["values"] for i in v["items"]]
    assert [(i["field"], i["fromString"], i["toString"]) for i in items] == [
        ("status", "To Do", "In Progress"), ("status", "In Progress", "Done")]


# --- row 4: rich text ----------------------------------------------------------------------------------


def test_row4_description_round_trips(c):
    desc = {"version": 1, "type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Acceptance criteria"}]},
        {"type": "bulletList", "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "AC one: "}, {"type": "text", "text": "0", "marks": [{"type": "code"}]}]}]}]}]}
    story = _create(c, "fictional story", "Story", description=desc)
    assert c.request("GET", "/issue/%s" % story["key"], params={"fields": "description"}).data["fields"]["description"] == desc


def test_row4_description_plain_string_is_400(c, fake):
    key = fake.add_issue("fictional story")
    with pytest.raises(cl.JiraError) as e:
        c.request("PUT", "/issue/%s" % key, body={"fields": {"description": "plain string, not ADF"}})
    assert errors_of(e) == (400, ["description: Operation value must be an Atlassian Document (see the Atlassian Document Format)"])


def test_row4_comment_body_stored_equals_sent(c, fake):
    key = fake.add_issue("fictional story")
    body = doc("[skj:questions] fictional")
    r = c.request("POST", "/issue/%s/comment" % key, body={"body": body})
    assert r.status == 201
    assert c.request("GET", "/issue/%s/comment/%s" % (key, r.data["id"])).data["body"] == body


@pytest.mark.parametrize("body, messages", [
    ("a plain string body", ["comment: Comment body is not valid!"]),
    (doc(""), ["comment: Comment body can not be empty!"]),
    ({"version": 1, "type": "doc", "content": [{"type": "nosuchnode"}]}, ["INVALID_INPUT", "comment: INVALID_INPUT"]),
])
def test_row4_malformed_comment_bodies_are_400(c, fake, body, messages):
    key = fake.add_issue("fictional story")
    with pytest.raises(cl.JiraError) as e:
        c.request("POST", "/issue/%s/comment" % key, body={"body": body})
    assert errors_of(e) == (400, messages)
    with pytest.raises(cl.JiraError) as e2:  # an edit validates the same way
        cid = fake.add_comment(key, doc("fine"))
        c.request("PUT", "/issue/%s/comment/%s" % (key, cid), body={"body": body})
    assert errors_of(e2) == (400, messages)


# --- row 5: comments and trust -------------------------------------------------------------------------


def _post_comment(c, key, text="fictional comment", **extra):
    return c.request("POST", "/issue/%s/comment" % key, body={"body": doc(text), **extra}).data


def test_row5_new_comment_created_equals_updated_author_is_me(c, fake):
    key = fake.add_issue("fictional story")
    cm = _post_comment(c, key)
    assert {"id", "self", "author", "updateAuthor", "body", "created", "updated", "jsdPublic"} <= set(cm)
    assert cm["created"] == cm["updated"]
    assert cm["author"]["accountId"] == cm["updateAuthor"]["accountId"] == fake.account_id
    assert cm["author"]["accountType"] == "atlassian"
    assert cm["self"] == "%s/issue/%s/comment/%s" % (API, fake.issue(key)["id"], cm["id"])


def test_row5_edit_changes_only_updated_and_update_author(c, fake):
    key = fake.add_issue("fictional story")
    before = _post_comment(c, key)
    r = c.request("PUT", "/issue/%s/comment/%s" % (key, before["id"]), params={"notifyUsers": "false"},
                  body={"body": doc("fictional comment, edited")})
    assert r.status == 200
    after = c.request("GET", "/issue/%s/comment/%s" % (key, before["id"])).data
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed == {"updated", "body"}  # body is what the edit sent; updateAuthor stays me (own edit)
    assert after["created"] == before["created"] and after["updated"] != after["created"]
    assert after["updateAuthor"]["accountId"] == fake.account_id
    items = [i["field"] for v in c.request("GET", "/issue/%s/changelog" % key).data["values"] for i in v["items"]]
    assert "Comment" not in items  # an edit leaves no changelog entry


def test_row5_foreign_edit_changes_update_author_simulated(c, fake):
    """SIMULATED (U2): a second human's edit was never observed; the fake models the documented field."""
    key = fake.add_issue("fictional story")
    other = fake.user(OTHER_ID)
    cid = fake.add_comment(key, doc("/approve D1 fictional"), author=other)
    fresh = c.request("GET", "/issue/%s/comment/%s" % (key, cid)).data
    assert fresh["author"]["accountId"] == fresh["updateAuthor"]["accountId"] == OTHER_ID
    assert fresh["created"] == fresh["updated"]
    fake.edit_comment(cid, doc("/approve D1 rewritten"), editor=fake.user(fake.account_id))
    edited = c.request("GET", "/issue/%s/comment/%s" % (key, cid)).data
    assert edited["author"]["accountId"] == OTHER_ID and edited["updateAuthor"]["accountId"] == fake.account_id
    assert edited["created"] == fresh["created"] and edited["updated"] != fresh["updated"]


def test_row5_property_write_does_not_change_updated(c, fake):
    key = fake.add_issue("fictional story")
    cm = _post_comment(c, key)
    r = c.request("PUT", "/comment/%s/properties/speckit" % cm["id"], body={"sha256": "0" * 64})
    assert r.status == 201
    assert c.request("GET", "/comment/%s/properties/speckit" % cm["id"]).data["value"] == {"sha256": "0" * 64}
    assert c.request("GET", "/issue/%s/comment/%s" % (key, cm["id"])).data["updated"] == cm["updated"]


def test_row5_delete_then_404(c, fake):
    key = fake.add_issue("fictional story")
    cm = _post_comment(c, key, "fictional comment to delete")
    r = c.request("DELETE", "/issue/%s/comment/%s" % (key, cm["id"]))
    assert (r.status, r.data) == (204, None)
    with pytest.raises(cl.NotFound) as e:
        c.request("GET", "/issue/%s/comment/%s" % (key, cm["id"]))
    assert e.value.messages == ["Can not find a comment for the id: %s." % cm["id"]]
    items = [i for v in c.request("GET", "/issue/%s/changelog" % key).data["values"] for i in v["items"]]
    assert [i["field"] for i in items] == ["Comment"]  # row 5 verified the field name only
    assert c.request("GET", "/issue/%s/comment" % key).data["total"] == 0


def test_unverified_delete_changelog_entry_content(c, fake):
    """UNVERIFIED: the probe kept field names only; fromString/toString/fieldId are the fake's guess.
    No product code may rely on the text of a delete entry."""
    key = fake.add_issue("fictional story")
    cm = _post_comment(c, key, "fictional comment to delete")
    c.request("DELETE", "/issue/%s/comment/%s" % (key, cm["id"]))
    (item,) = [i for v in c.request("GET", "/issue/%s/changelog" % key).data["values"] for i in v["items"]]
    assert (item["field"], item["fromString"], item["toString"]) == ("Comment", "fictional comment to delete", None)


def test_row5_edit_moves_updated_even_with_a_constant_clock():
    t0 = datetime.datetime(2026, 3, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    f = FakeJira(clock=lambda: t0)
    c = f.client()
    key = f.add_issue("fictional story")
    cm = c.request("POST", "/issue/%s/comment" % key, body={"body": doc("x")}).data
    assert cm["created"] == cm["updated"]  # a fresh comment stays unedited
    stamps = [cm["updated"]]
    for text in ("y", "z"):
        c.request("PUT", "/issue/%s/comment/%s" % (key, cm["id"]), body={"body": doc(text)})
        stamps.append(c.request("GET", "/issue/%s/comment/%s" % (key, cm["id"])).data["updated"])
    f.edit_comment(cm["id"], doc("w"), editor=f.user(OTHER_ID))  # the SIMULATED path too
    stamps.append(f.comments[cm["id"]]["updated"])
    assert stamps == sorted(set(stamps))  # strictly increasing: every edit moves updated past created
    assert stamps[1] == "2026-03-01T12:00:00.001+0000"


def test_row5_visibility_stored_when_given(c, fake):
    key = fake.add_issue("fictional story")
    vis = {"type": "role", "value": "Developers"}
    cm = _post_comment(c, key, visibility=vis)
    assert c.request("GET", "/issue/%s/comment/%s" % (key, cm["id"])).data["visibility"] == vis
    assert "visibility" not in _post_comment(c, key)


def test_row5_comment_page_shape(c, fake):
    key = fake.add_issue("fictional story")
    ids = [_post_comment(c, key, "c%d" % i)["id"] for i in range(3)]
    page = c.request("GET", "/issue/%s/comment" % key, params={"maxResults": 2, "startAt": 1}).data
    assert set(page) == {"comments", "maxResults", "startAt", "total"}
    assert (page["startAt"], page["maxResults"], page["total"]) == (1, 2, 3)
    assert [x["id"] for x in page["comments"]] == ids[1:]


# --- row 6: linking code ------------------------------------------------------------------------------


def test_row6_remote_link_idempotent(c, fake):
    key = fake.add_issue("fictional story")
    link = {"globalId": "skj:pr:fictional-1", "relationship": "pull request",
            "object": {"url": "https://example.com/fictional/pull/1", "title": "%s fictional PR" % key}}
    r1 = c.request("POST", "/issue/%s/remotelink" % key, body=link)
    r2 = c.request("POST", "/issue/%s/remotelink" % key, body=dict(link, object={"url": link["object"]["url"], "title": "renamed"}))
    assert (r1.status, r2.status) == (201, 200) and r1.data["id"] == r2.data["id"] and isinstance(r1.data["id"], int)
    links = c.request("GET", "/issue/%s/remotelink" % key).data
    assert len(links) == 1 and links[0]["object"]["title"] == "renamed"
    r3 = c.request("POST", "/issue/%s/remotelink" % key, body={"object": link["object"]})
    assert r3.status == 201 and r3.data["id"] != r1.data["id"]  # no globalId: always a new link


# --- row 7: search, errors, rate limits ------------------------------------------------------------------


def _seed(fake, n, **kw):
    return [fake.add_issue("fictional %d" % i, **kw) for i in range(n)]


def _all_pages(c, jql, method="GET", max_pages=10):
    keys_, token, pages = [], None, 0
    while True:
        pages += 1
        assert pages <= max_pages
        if method == "GET":
            params = {"jql": jql, "fields": "key"}
            if token:
                params["nextPageToken"] = token
            data = c.request("GET", "/search/jql", params=params).data
        else:
            body = {"jql": jql, "fields": ["key"]}
            if token:
                body["nextPageToken"] = token
            data = c.request("POST", "/search/jql", body=body, retry_safe=True).data
        keys_ += [i["key"] for i in data["issues"]]
        if data["isLast"]:
            assert "nextPageToken" not in data
            return keys_, pages
        token = data["nextPageToken"]


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_row7_pagination_token(method):
    fake = FakeJira(page_size=2)
    want = _seed(fake, 5)
    got, pages = _all_pages(fake.client(), 'project = "SPK" ORDER BY key', method)
    assert got == want and pages == 3


def test_row7_page_shape_and_fields(c, fake):
    _seed(fake, 2)
    page = c.request("GET", "/search/jql", params={"jql": "project = SPK ORDER BY key ASC", "maxResults": 1,
                                                    "fields": "key"}).data
    assert set(page) == {"issues", "nextPageToken", "isLast"} and page["isLast"] is False
    assert set(page["issues"][0]) == {"expand", "id", "self", "key"}  # fields=key alone adds no "fields"
    with_status = c.request("GET", "/search/jql", params={"jql": "project = SPK", "fields": "key,status"}).data
    assert with_status["issues"][0]["fields"]["status"]["statusCategory"]["key"] == "new"
    assert set(with_status) == {"issues", "isLast"}


def test_row7_the_jql_keys_py_builds(c, fake):
    story = fake.add_issue("fictional story")
    a = fake.add_issue("T002 b", type="Subtask", parent=story, labels=["speckit"])
    b = fake.add_issue("T001 a", type="Subtask", parent=story)
    fake.add_issue("unrelated", labels=["speckit"])
    other = fake.add_issue("another")

    def search(jql):
        return [i["key"] for i in c.request("POST", "/search/jql", body={"jql": jql}, retry_safe=True).data["issues"]]

    assert search(keys.jql_children(story)) == [a, b]
    assert search(keys.jql_by_label("SPK", "speckit")) == [a, "SPK-4"]
    assert search(keys.jql_keys([other, story])) == [story, other]
    assert search('parent = "%s" AND labels = "speckit"' % story) == [a]
    assert search("key in (%s, %s) ORDER BY key DESC" % (story, other)) == [other, story]


def test_row7_unbounded_jql_is_400(c, fake):
    _seed(fake, 1)
    for jql in ("ORDER BY key", ""):
        with pytest.raises(cl.JiraError) as e:
            c.request("GET", "/search/jql", params={"jql": jql})
        assert errors_of(e) == (400, ["Unbounded JQL queries are not allowed here. Please add a search restriction to your query."])


def test_row7_bad_jql_is_400_with_position(c):
    jql = 'project = "SPK" AND summary ~ "\\"unterminated'  # the spike's own query and answer (row 7)
    with pytest.raises(cl.JiraError) as e:
        c.request("GET", "/search/jql", params={"jql": jql})
    assert errors_of(e) == (400, ["Error in the JQL Query: The quoted string '\\\"unterminated' has not been "
                                  "completed. (line 1, character 31)"])
    with pytest.raises(cl.JiraError) as e2:
        c.request("GET", "/search/jql", params={"jql": "project = SPK AND summary ~ x"})
    assert e2.value.status == 400


def test_row7_old_search_410(c):
    with pytest.raises(cl.SearchRemoved) as e:
        c.request("GET", "/search", params={"jql": "project = SPK"})
    assert e.value.status == 410
    assert e.value.messages == ["The requested API has been removed. Please migrate to the /rest/api/3/search/jql API. "
                                "A full migration guideline is available at https://developer.atlassian.com/changelog/#CHANGE-2046"]


def test_row7_approximate_count(c, fake):
    _seed(fake, 4)
    r = c.request("POST", "/search/approximate-count", body={"jql": "project = SPK"})
    assert (r.status, r.data) == (200, {"count": 4})


@pytest.mark.parametrize("key", ["SPK-999999999", "NOSUCHPROJ-1"])
def test_row7_404_shape_same_for_missing_key_and_project(fake, key):
    status, _, body = raw(fake, "GET", "/issue/%s" % key)
    assert status == 404
    assert json.loads(body) == {"errorMessages": ["Issue does not exist or you do not have permission to see it."], "errors": {}}


def test_row7_rate_limit_headers_on_every_answer(c):
    h = c.request("GET", "/myself").headers
    assert h["ratelimit-policy"] == '"jira-burst-based";q=100;w=1'
    assert h["x-ratelimit-limit"].isdigit() and h["x-ratelimit-remaining"].isdigit()


def test_row7_injected_429_with_retry_after_is_retried_synthetic(fake):
    """SYNTHETIC (U4): a real 429 was never provoked."""
    c = fake.client()
    fake.inject(429, retry_after=2)
    assert c.request("GET", "/myself").status == 200
    assert [e["status"] for e in fake.log] == [429, 200] and len(fake.sleeps) == 1 and fake.sleeps[0] >= 2


def test_row7_injected_503_without_retry_after_raises_and_429_on_write_not_retried(fake):
    c = fake.client()
    fake.inject(503)
    with pytest.raises(cl.JiraError) as e:
        c.request("GET", "/myself")
    assert e.value.status == 503
    fake.inject(429, retry_after=1, headers={"RateLimit-Reason": "jira-per-issue-on-write"})
    with pytest.raises(cl.JiraError) as e2:
        c.request("POST", "/issue", body={"fields": {}})
    assert e2.value.status == 429 and e2.value.headers["ratelimit-reason"] == "jira-per-issue-on-write"
    assert fake.issues == {} and fake.sleeps == []  # an injected error is never applied


@pytest.mark.parametrize("quirk", ["repeat_token", "never_end", "no_token"])
def test_row7_search_quirks_for_r1(quirk):
    fake = FakeJira(page_size=1, search_quirk=quirk)
    _seed(fake, 3)
    c = fake.client()
    pages = [c.request("GET", "/search/jql", params={"jql": "project = SPK"}).data]
    if quirk == "no_token":
        assert pages[0]["isLast"] is False and "nextPageToken" not in pages[0]
        return
    for _ in range(5):
        pages.append(c.request("GET", "/search/jql", params={"jql": "project = SPK",
                                                            "nextPageToken": pages[-1]["nextPageToken"]}).data)
    assert all(p["isLast"] is False for p in pages)
    tokens = [p["nextPageToken"] for p in pages]
    if quirk == "repeat_token":
        assert len(set(tokens)) == 1
    else:
        assert len(set(tokens)) == len(tokens)


# --- row 8: read after write ---------------------------------------------------------------------------


def test_row8_new_subtask_visible_in_parent_search_immediately(c, fake):
    """One live trial only (U18): the fake has no lag."""
    story = fake.add_issue("fictional story")
    sub = _create(c, "T001 fictional", "Subtask", parent=story)
    jql = keys.jql_children(story)
    got = c.request("GET", "/search/jql", params={"jql": jql, "reconcileIssues": sub["id"]}).data["issues"]
    assert [i["key"] for i in got] == [sub["key"]]


# --- attachments (upload ran live once, LV2; otherwise this is our reading of [OAS]) --------------------


def _multipart(name="file", filename="DoD_SPK-1.md", data=b"# fictional ledger\n"):
    boundary = "fakeboundary7c1e"
    body = ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
            "Content-Type: text/markdown\r\n\r\n" % (boundary, name, filename)).encode() + data + (
        "\r\n--%s--\r\n" % boundary).encode()
    return {"Content-Type": "multipart/form-data; boundary=%s" % boundary}, body


def test_attachment_upload_needs_no_check_header_and_file_part(fake):
    key = fake.add_issue("fictional story")
    h, body = _multipart()
    status, _, _ = raw(fake, "POST", "/issue/%s/attachments" % key, body=body, headers=h)
    assert status == 403
    status, _, out = raw(fake, "POST", "/issue/%s/attachments" % key, body=body,
                         headers=dict(h, **{"X-Atlassian-Token": "no-check"}))
    assert status == 200
    att = json.loads(out)
    assert [(a["filename"], a["size"]) for a in att] == [("DoD_SPK-1.md", 19)]
    got = fake.client().request("GET", "/issue/%s" % key, params={"fields": "attachment"}).data["fields"]["attachment"]
    assert [(a["id"], a["filename"], a["size"]) for a in got] == [(att[0]["id"], "DoD_SPK-1.md", 19)]
    h2, body2 = _multipart(name="other")
    status, _, _ = raw(fake, "POST", "/issue/%s/attachments" % key, body=body2,
                       headers=dict(h2, **{"X-Atlassian-Token": "no-check"}))
    assert status == 400


# --- injectors: transport failures, clock, ids --------------------------------------------------------------


def test_transport_failure_on_nth_request(fake):
    c = fake.client()
    fake.fail_transport(on=2)
    c.request("GET", "/myself")
    with pytest.raises(cl.ClientError) as e:
        c.request("GET", "/myself")
    assert not isinstance(e.value, cl.WriteUncertain)
    assert c.request("GET", "/myself").status == 200
    assert [x["status"] for x in fake.log] == [200, None, 200]


def test_transport_failure_on_write_after_apply_leaves_the_write_done(fake):
    c = fake.client()
    story = fake.add_issue("fictional story")
    fake.fail_transport(applied=True)
    with pytest.raises(cl.WriteUncertain):
        c.request("POST", "/issue/%s/comment" % story, body={"body": doc("[skj:marker] fictional")})
    assert c.request("GET", "/issue/%s/comment" % story).data["total"] == 1  # the re-read finds it
    fake.fail_transport(OSError("reset"))
    with pytest.raises(cl.WriteUncertain):
        c.request("POST", "/issue/%s/comment" % story, body={"body": doc("second")})
    assert c.request("GET", "/issue/%s/comment" % story).data["total"] == 1  # not applied


def test_deterministic_ids_and_injectable_clock():
    t0 = datetime.datetime(2026, 3, 1, 12, 0, 0, 250000, tzinfo=datetime.timezone.utc)
    f = FakeJira(clock=lambda: t0)
    key = f.add_issue("fictional")
    assert (key, f.issue(key)["id"]) == ("SPK-1", "10000")
    cm = f.client().request("POST", "/issue/%s/comment" % key, body={"body": doc("x")}).data
    assert (cm["id"], cm["created"]) == ("10000", "2026-03-01T12:00:00.250+0000")
    g = FakeJira()  # default clock: fixed start, one second per stamp
    k = g.add_issue("fictional")
    a = g.add_comment(k, doc("a"))
    b = g.add_comment(k, doc("b"))
    assert g.comments[a]["created"] < g.comments[b]["created"]
    assert FakeJira().add_issue("x") == "SPK-1"


def test_empty_bodies_are_bytes_and_unknown_routes_are_fake_404(fake):
    key = fake.add_issue("fictional story")
    status, _, body = raw(fake, "POST", "/issue/%s/transitions" % key, body=b'{"transition": {"id": "21"}}')
    assert (status, body) == (204, b"")
    status, _, body = raw(fake, "GET", "/nothing/here")
    assert status == 404 and json.loads(body)["errorMessages"][0].startswith("fake:")


def test_wrong_host_is_a_test_bug(fake):
    with pytest.raises(AssertionError):
        fake.opener("GET", "https://other-site.atlassian.net/rest/api/3/myself", {}, None, 30)


# --- S9-AC2: the request log ---------------------------------------------------------------------------------


def test_request_log_records_method_path_write_and_redacts(fake):
    c = fake.client()
    key = fake.add_issue("fictional story")
    c.request("GET", "/issue/%s" % key, params={"fields": "status"})
    c.request("POST", "/search/jql", body={"jql": "project = SPK"}, retry_safe=True)
    c.request("POST", "/search/approximate-count", body={"jql": "project = SPK"})
    cid = c.request("POST", "/issue/%s/comment" % key, body={"body": doc("x")}).data["id"]
    c.request("PUT", "/issue/%s/comment/%s" % (key, cid), body={"body": doc("y")})
    c.request("DELETE", "/issue/%s/comment/%s" % (key, cid))
    raw(fake, "GET", "/myself", auth=None)
    got = [(e["method"], e["path"], e["write"], e["auth"]) for e in fake.log]
    assert got == [
        ("GET", "/issue/SPK-1", False, "Basic [redacted]"),
        ("POST", "/search/jql", False, "Basic [redacted]"),
        ("POST", "/search/approximate-count", False, "Basic [redacted]"),
        ("POST", "/issue/SPK-1/comment", True, "Basic [redacted]"),
        ("PUT", "/issue/SPK-1/comment/%s" % cid, True, "Basic [redacted]"),
        ("DELETE", "/issue/SPK-1/comment/%s" % cid, True, "Basic [redacted]"),
        ("GET", "/myself", False, None),
    ]
    assert fake.log[0]["query"] == {"fields": "status"} and fake.log[0]["status"] == 200
    assert [e["path"] for e in fake.writes()] == [p for (_, p, w, _) in got if w]
    dump = json.dumps(fake.log)
    for secret in (TOKEN, EMAIL, B64):
        assert secret not in dump


# --- S9-AC3: the fake is the only transport --------------------------------------------------------------------

# The default opener's real network path, reachable only through these names; test_client.py exercises it with
# in-memory handlers (no socket), and test_conftest_guard.py opens raw sockets to prove conftest blocks them.
_NET_NAMES = {"urlopen", "create_connection", "HTTPConnection", "HTTPSConnection", "_urllib_opener", "_OPENER",
              "_build_opener", "build_opener", "OpenerDirector"}
_ALLOWED = {"test_client.py": {"net", "client-default"}, "test_conftest_guard.py": {"socket"}}


def _violations(path):
    tree, out = ast.parse(path.read_text()), []
    allowed = _ALLOWED.get(path.name, set())
    for node in ast.walk(tree):
        name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
        if name in _NET_NAMES and "net" not in allowed:
            out.append("%s:%d uses %s" % (path.name, node.lineno, name))
        if isinstance(node, ast.Call):
            fn = node.func
            fname = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
            owner = fn.value.id if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) else None
            if fname == "socket" and owner == "socket" and "socket" not in allowed:
                out.append("%s:%d opens a socket" % (path.name, node.lineno))
            kws = {k.arg for k in node.keywords}
            if fname == "Client" and "opener" not in kws and None not in kws and "client-default" not in allowed:
                out.append("%s:%d builds a Client without opener=" % (path.name, node.lineno))
    return out


def test_fake_is_the_only_transport():
    files = sorted(TESTS.glob("*.py"))
    assert len(files) > 5
    bad = [v for f in files for v in _violations(f)]
    assert bad == []


def test_scanner_catches_network_use(tmp_path):
    bad = tmp_path / "test_bad.py"
    bad.write_text("import socket, urllib.request\nurllib.request.urlopen('https://x')\n"
                   "socket.socket()\nfrom skj.client import Client\nClient('s', 'e', 't')\n")
    assert len(_violations(bad)) == 3


def test_fakejira_imports_no_network_stack():
    tree = ast.parse((TESTS / "fakejira.py").read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module)
    assert mods <= {"base64", "datetime", "email.parser", "email.policy", "json", "re", "urllib.parse", "skj.client"}
