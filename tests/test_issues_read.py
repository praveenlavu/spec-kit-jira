"""R1: issue reads and search, driven through skj.client.Client with the S9 fake as the opener.

Fictional data only. No test touches a socket (conftest blocks them).
"""
import json
import urllib.parse

import pytest

from fakejira import FakeJira, SITE
from skj import client as cl
from skj import issues, keys


class Rec:
    """Wraps the fake as an opener and keeps each request's method, path, query and JSON body (the fake's log
    keeps no bodies)."""

    def __init__(self, fake):
        self.fake, self.reqs = fake, []

    def __call__(self, method, url, headers, body, timeout):
        p = urllib.parse.urlsplit(url)
        self.reqs.append((method, p.path[len("/rest/api/3"):], dict(urllib.parse.parse_qsl(p.query)),
                          json.loads(body) if body else None))
        return self.fake(method, url, headers, body, timeout)


def make(**kw):
    fake = FakeJira(**kw)
    rec = Rec(fake)
    return fake, rec, cl.Client(fake.site, fake.email, fake.token, opener=rec, sleep=fake.sleeps.append)


def seed_story(fake, n=2):
    story = fake.add_issue("fictional story")
    return story, [fake.add_issue("fictional task %d" % i, type="Subtask", parent=story) for i in range(n)]


BAD_IDS = ["../x", "1 ", "0x1", "01", "", 5, None, "1\n", "\u0661"]


# --- get -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["spk-1", " SPK-1", "SPK-0", "SPK-01", "SPK-1;touch x", "SPK-١", "", None, 5,
                                 "SPK-1\n", "../x"])
def test_get_validates_key_first(bad):
    fake, rec, c = make()
    fake.add_issue("fictional")
    with pytest.raises(ValueError):
        issues.get(c, bad)
    assert fake.log == [] and rec.reqs == []  # R1-AC1: nothing was sent


def test_get_returns_issue_with_id_and_explicit_fields():
    fake, rec, c = make()
    key = fake.add_issue("fictional story", labels=["speckit"])
    got = issues.get(c, key, ["summary", "labels"])
    assert got["key"] == key and got["id"] == fake.issue(key)["id"]
    assert got["fields"]["summary"] == "fictional story" and got["fields"]["labels"] == ["speckit"]
    assert rec.reqs == [("GET", "/issue/" + key, {"fields": "summary,labels"}, None)]


@pytest.mark.parametrize("fields", [[], "summary", ["summary", 3], ["sum mary"], ["a,b"], [""], ["x" * 65],
                                    ["-status"], [["x"]]])
def test_get_rejects_bad_fields_before_any_request(fields):
    fake, rec, c = make()
    key = fake.add_issue("fictional")
    with pytest.raises(ValueError):
        issues.get(c, key, fields)
    assert rec.reqs == []


def test_get_key_mismatch_raises():
    fake, rec, c = make()
    old = fake.add_issue("fictional")
    fake.move_issue(old, "OTH-7")  # the old key now answers with the new one (Review Focus 2)
    with pytest.raises(issues.KeyMismatch) as e:
        issues.get(c, old)
    assert isinstance(e.value, cl.ClientError)
    assert str(e.value) == "Jira answered %s for %s; refused" % ("OTH-7", old)


def test_key_mismatch_with_an_unusable_returned_key_names_neither_server_value():
    class C:
        def request(self, *a, **k):
            return cl.Response(200, {}, {"id": "1", "key": "evil\nkey; rm -rf"})
    with pytest.raises(issues.KeyMismatch) as e:
        issues.get(C(), "SPK-1")
    assert "evil" not in str(e.value) and "SPK-1" in str(e.value)


@pytest.mark.parametrize("data", [None, [], "x", {"id": "1"}, {"id": "1", "key": 5}, {"key": "SPK-1"},
                                  {"id": 1, "key": "SPK-1"}])
def test_get_malformed_response_is_an_error(data):
    class C:
        def request(self, *a, **k):
            return cl.Response(200, {}, data)
    with pytest.raises(issues.ReadError):
        issues.get(C(), "SPK-1")


@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_get_id_must_be_decimal(bad_id):
    class C:
        def request(self, *a, **k):
            return cl.Response(200, {}, {"id": bad_id, "key": "SPK-1"})
    with pytest.raises(issues.ReadError):
        issues.get(C(), "SPK-1")


def test_get_missing_issue_is_notfound():
    _, _, c = make()
    with pytest.raises(cl.NotFound):
        issues.get(c, "SPK-99")


# --- children --------------------------------------------------------------------------------------------

def test_children_from_subtasks_field():
    fake, rec, c = make()
    story, subs = seed_story(fake)
    got = issues.children(c, story)
    assert [i["key"] for i in got] == subs
    assert all(i["id"] == fake.issue(i["key"])["id"] for i in got)
    assert rec.reqs == [("GET", "/issue/" + story, {"fields": "subtasks"}, None)]  # no search needed


def test_children_empty_subtasks_is_empty_not_a_fallback():
    fake, rec, c = make()
    story = fake.add_issue("fictional")
    assert issues.children(c, story) == []
    assert [r[1] for r in rec.reqs] == ["/issue/" + story]


def test_children_falls_back_to_search_when_subtasks_is_missing():
    fake, rec, c = make()
    story, subs = seed_story(fake, 3)
    inner = rec.fake

    def no_subtasks(method, url, headers, body, timeout):  # a site that does not return the field
        st, h, blob = inner(method, url, headers, body, timeout)
        if url.endswith("fields=subtasks"):
            d = json.loads(blob)
            d["fields"] = {}
            blob = json.dumps(d).encode()
        return st, h, blob

    rec2 = Rec(fake)
    rec2.fake = no_subtasks
    c2 = cl.Client(fake.site, fake.email, fake.token, opener=rec2, sleep=fake.sleeps.append)
    got = issues.children(c2, story)
    assert [i["key"] for i in got] == subs
    method, path, _, body = rec2.reqs[1]
    assert (method, path) == ("POST", "/search/jql") and body["jql"] == keys.jql_children(story)


@pytest.mark.parametrize("subtasks", ["x", {"a": 1}, [1], [{"id": "1", "key": "bad key"}], [{"id": "1"}]])
def test_children_malformed_subtasks_is_an_error(subtasks):
    class C:
        def request(self, *a, **k):
            return cl.Response(200, {}, {"id": "1", "key": "SPK-1", "fields": {"subtasks": subtasks}})
    with pytest.raises(issues.ReadError):
        issues.children(C(), "SPK-1")


def test_children_validates_key_first():
    fake, rec, c = make()
    with pytest.raises(ValueError):
        issues.children(c, "nope")
    assert rec.reqs == []


# --- search_jql ------------------------------------------------------------------------------------------

def test_search_follows_next_page_token():
    fake, rec, c = make(page_size=2)
    story, subs = seed_story(fake, 5)
    got = issues.search_jql(c, keys.jql_children(story), ["summary"])
    assert [i["key"] for i in got] == subs
    posts = [r for r in rec.reqs if r[:2] == ("POST", "/search/jql")]
    assert len(posts) == 3
    assert "nextPageToken" not in posts[0][3] and posts[1][3]["nextPageToken"] and posts[2][3]["nextPageToken"]
    assert all(r[3]["jql"] == keys.jql_children(story) for r in posts)


def test_search_repeated_token_stops():
    fake, rec, c = make(page_size=1, search_quirk="repeat_token")
    story, _ = seed_story(fake, 3)
    with pytest.raises(issues.ReadError, match="repeated"):
        issues.search_jql(c, keys.jql_children(story), ["summary"])
    assert len(rec.reqs) == 2  # the repeat is caught on the second answer, not at the page cap


def test_search_page_cap():
    fake, rec, c = make(page_size=1, search_quirk="never_end")
    story, _ = seed_story(fake, 3)
    with pytest.raises(issues.ReadError, match="page cap"):
        issues.search_jql(c, keys.jql_children(story), ["summary"], page_cap=4)
    assert len(rec.reqs) == 4
    with pytest.raises(issues.ReadError):
        issues.search_jql(c, keys.jql_children(story), ["summary"])  # default cap 20
    assert len(rec.reqs) == 4 + 20


def test_search_isLast_false_without_token_errors():
    fake, rec, c = make(search_quirk="no_token")
    story, _ = seed_story(fake)
    with pytest.raises(issues.ReadError, match="no nextPageToken"):
        issues.search_jql(c, keys.jql_children(story), ["summary"])


def test_search_every_request_carries_explicit_fields():
    fake, rec, c = make(page_size=1)
    story, subs = seed_story(fake, 2)
    issues.get(c, story)
    issues.get(c, story, ["labels"])
    issues.children(c, story)
    issues.search_jql(c, keys.jql_by_label("SPK", "speckit"), ["summary", "status"])
    issues.search_jql(c, keys.jql_keys(subs), ["summary"])
    reads = [r for r in rec.reqs if r[1].startswith(("/issue/", "/search"))]
    assert len(reads) >= 6
    for method, path, query, body in reads:
        carrier = body if method == "POST" else query
        assert carrier.get("fields"), (method, path)
        assert carrier["fields"] not in ("", [])


@pytest.mark.parametrize("jql", [
    "", "ORDER BY key", 'project = "SPK" OR project = "OTH"', "project = SPK", 'parent = "SPK-1"; DROP',
    'parent = "SPK-1" AND summary ~ "x"', 'labels = "a" ORDER BY created', 'parent = "SPK-1" ORDER BY key\n',
    'key in ()', 'parent = "SPK-1" AND', None, 5, ["x"],
    'parent = "SPK-1" AND project = "SPK" AND labels = "a" AND parent = "SPK-2" ORDER BY key extra',
])
def test_search_unbounded_or_foreign_jql_is_refused_before_any_request(jql):
    fake, rec, c = make()
    with pytest.raises(ValueError):
        issues.search_jql(c, jql, ["summary"])
    assert rec.reqs == []  # R1-AC3


def test_search_accepts_every_keys_builder_output():
    fake, rec, c = make()
    story, subs = seed_story(fake)
    for jql in (keys.jql_children(story), keys.jql_by_label("SPK", "a.b_c-d"), keys.jql_keys(subs),
                keys.jql_keys(["SPK-%d" % i for i in range(1, keys.MAX_JQL_KEYS + 1)])):
        issues.search_jql(c, jql, ["summary"])


@pytest.mark.parametrize("fields", [None, [], "summary", ("summary",), ["summary", 3], ["bad name"], ["a,b"], [""]])
def test_search_fields_must_be_a_nonempty_list_of_field_names(fields):
    fake, rec, c = make()
    story, _ = seed_story(fake)
    with pytest.raises(ValueError):
        issues.search_jql(c, keys.jql_children(story), fields)
    assert rec.reqs == []


@pytest.mark.parametrize("cap", [0, -1, True, 2.0, "3", 101])
def test_search_page_cap_must_be_a_sane_int(cap):
    fake, rec, c = make()
    story, _ = seed_story(fake)
    with pytest.raises(ValueError):
        issues.search_jql(c, keys.jql_children(story), ["summary"], page_cap=cap)
    assert rec.reqs == []


def _scripted(pages):
    class C:
        def __init__(self):
            self.n = 0

        def request(self, *a, **k):
            d = pages[self.n]
            self.n += 1
            return cl.Response(200, {}, d)
    return C()


@pytest.mark.parametrize("page", [
    None, [], "x", {}, {"issues": None}, {"issues": {}}, {"issues": "x"}, {"issues": [1]}, {"issues": [None]},
    {"issues": [{"id": "1", "key": 5}]}, {"issues": [{"id": "1", "key": "bad key"}]}, {"issues": [{}]},
    {"issues": [{"id": 1, "key": "SPK-1"}]}, {"issues": [], "nextPageToken": 5, "isLast": False},
    {"issues": [], "isLast": "no"}, {"issues": [], "nextPageToken": "", "isLast": False},
    {"issues": [], "nextPageToken": "t", "isLast": True},
])
def test_search_malformed_page_is_an_error(page):
    with pytest.raises(issues.ReadError):
        issues.search_jql(_scripted([page]), keys.jql_children("SPK-1"), ["summary"])


@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_search_and_children_id_must_be_decimal(bad_id):
    page = {"issues": [{"id": bad_id, "key": "SPK-2"}], "isLast": True}
    with pytest.raises(issues.ReadError):
        issues.search_jql(_scripted([page]), keys.jql_children("SPK-1"), ["summary"])

    class C:
        def request(self, *a, **k):
            return cl.Response(200, {}, {"id": "1", "key": "SPK-1", "fields": {"subtasks": page["issues"]}})
    with pytest.raises(issues.ReadError):
        issues.children(C(), "SPK-1")


def test_search_two_pages_without_isLast_reads_this_pages_token():
    pages = [{"issues": [{"id": "1", "key": "SPK-2"}], "nextPageToken": "t1"},  # token, no isLast: more
             {"issues": [{"id": "2", "key": "SPK-3"}]}]  # neither: the last page
    c = _scripted(pages)
    got = issues.search_jql(c, keys.jql_children("SPK-1"), ["summary"])
    assert [i["key"] for i in got] == ["SPK-2", "SPK-3"] and c.n == 2


def test_search_explicit_isLast_contradicting_the_token_is_still_an_error():
    for page in ({"issues": [], "isLast": False}, {"issues": [], "isLast": True, "nextPageToken": "t"}):
        with pytest.raises(issues.ReadError):
            issues.search_jql(_scripted([page]), keys.jql_children("SPK-1"), ["summary"])


def test_search_page_without_isLast_and_token_is_the_last_page():
    assert issues.search_jql(_scripted([{"issues": []}]), keys.jql_children("SPK-1"), ["summary"]) == []


def test_search_result_is_flat_list_of_dicts_in_order():
    pages = [{"issues": [{"id": "1", "key": "SPK-2"}], "nextPageToken": "t1", "isLast": False},
             {"issues": [{"id": "2", "key": "SPK-3"}], "isLast": True}]
    got = issues.search_jql(_scripted(pages), keys.jql_children("SPK-1"), ["summary"])
    assert got == [{"id": "1", "key": "SPK-2"}, {"id": "2", "key": "SPK-3"}]


# --- the old /search path --------------------------------------------------------------------------------

def test_never_calls_old_search_path():
    fake, rec, c = make(page_size=1)
    story, subs = seed_story(fake, 3)
    issues.get(c, story)
    issues.children(c, story)
    issues.search_jql(c, keys.jql_children(story), ["summary"])
    issues.me(c)
    issues.permissions(c, "SPK")
    paths = [e["path"] for e in fake.log] + [r[1] for r in rec.reqs]
    assert paths and "/search" not in paths  # R1-AC5: only /search/jql
    assert any(p == "/search/jql" for p in paths)


def test_search_removed_410_propagates_clearly():
    class C:
        def request(self, *a, **k):
            raise cl.SearchRemoved(410, ["gone"])
    with pytest.raises(cl.SearchRemoved) as e:
        issues.search_jql(C(), keys.jql_children("SPK-1"), ["summary"])
    assert "use /search/jql" in str(e.value)


# --- me / permissions ------------------------------------------------------------------------------------

def test_me_returns_only_the_three_safe_fields():
    fake, rec, c = make()
    got = issues.me(c)
    assert set(got) == {"accountId", "accountType", "active"}
    assert got["accountType"] == "atlassian" and got["active"] is True and got["accountId"]
    blob = json.dumps(got)
    me = fake._me()
    assert me["displayName"] not in blob and me["emailAddress"] not in blob


@pytest.mark.parametrize("kind", ["app", "customer", "ATLASSIAN", "", "atlassian "])
def test_me_requires_atlassian_account_type(kind):
    fake, rec, c = make(account_type=kind)
    with pytest.raises(issues.ReadError) as e:
        issues.me(c)
    assert kind not in str(e.value) or kind == ""  # R1-AC4; the server value is not echoed


@pytest.mark.parametrize("data", [None, [], {}, {"accountType": "atlassian"},
                                  {"accountType": "atlassian", "accountId": 5, "active": True},
                                  {"accountType": "atlassian", "accountId": "a b", "active": True},
                                  {"accountType": "atlassian", "accountId": "abc", "active": "yes"}])
def test_me_malformed_is_an_error(data):
    class C:
        def request(self, *a, **k):
            return cl.Response(200, {}, data)
    with pytest.raises(issues.ReadError):
        issues.me(C())


def test_permissions_returns_bool_dict():
    fake, rec, c = make(permissions={"EDIT_ALL_COMMENTS": False})
    got = issues.permissions(c, "SPK")
    assert got["EDIT_ALL_COMMENTS"] is False and got["DELETE_ALL_COMMENTS"] is True
    assert set(got) == set(issues.PERMISSIONS) and all(type(v) is bool for v in got.values())
    method, path, query, _ = rec.reqs[0]
    assert (method, path) == ("GET", "/mypermissions")
    assert query == {"projectKey": "SPK", "permissions": ",".join(issues.PERMISSIONS)}


def test_permissions_missing_entry_is_false_not_true():
    class C:
        def request(self, *a, **k):
            return cl.Response(200, {}, {"permissions": {"EDIT_ALL_COMMENTS": {"havePermission": True}}})
    got = issues.permissions(C(), "SPK")
    assert got["EDIT_ALL_COMMENTS"] is True and got["DELETE_ALL_COMMENTS"] is False


@pytest.mark.parametrize("project", ["spk", "S", "SPK-1", "SPK,ADMINISTER", "SPK\n", "", None, "A" * 11])
def test_permissions_validates_project_first(project):
    fake, rec, c = make()
    with pytest.raises(ValueError):
        issues.permissions(c, project)
    assert rec.reqs == []


@pytest.mark.parametrize("data", [None, [], {}, {"permissions": []}, {"permissions": {"EDIT_ALL_COMMENTS": True}},
                                  {"permissions": {"EDIT_ALL_COMMENTS": {"havePermission": "yes"}}}])
def test_permissions_malformed_is_an_error(data):
    class C:
        def request(self, *a, **k):
            return cl.Response(200, {}, data)
    with pytest.raises(issues.ReadError):
        issues.permissions(C(), "SPK")


def test_permissions_names_and_omitted_strict():  # A5: doctor's `edit-all` asks for three names; an omitted one cannot tell
    names = ("EDIT_ALL_COMMENTS", "DELETE_ALL_COMMENTS", "DELETE_OWN_COMMENTS")
    fake, rec, c = make(permissions={"DELETE_OWN_COMMENTS": False})
    assert issues.permissions(c, "SPK", names=names, omitted_false=False) == {
        "EDIT_ALL_COMMENTS": True, "DELETE_ALL_COMMENTS": True, "DELETE_OWN_COMMENTS": False}
    assert rec.reqs[0][2] == {"projectKey": "SPK", "permissions": ",".join(names)}

    class C:
        def request(self, *a, **k):
            return cl.Response(200, {}, {"permissions": {"EDIT_ALL_COMMENTS": {"havePermission": False}}})
    assert issues.permissions(C(), "SPK", names=names)["DELETE_OWN_COMMENTS"] is False  # default: omitted = False
    with pytest.raises(issues.ReadError):
        issues.permissions(C(), "SPK", names=names, omitted_false=False)
    for bad in ((), ("SYSTEM_ADMIN",), ("EDIT_ALL_COMMENTS", "X")):
        with pytest.raises(ValueError):
            issues.permissions(C(), "SPK", names=bad)
