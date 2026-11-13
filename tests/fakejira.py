"""A fake Jira Cloud (REST v3, one team-managed project) for the test-suite: the only network substitute.

Spec: a live check on one Free site (2026-11-08), rows 1 to 8, each row pinned by the `test_row<n>_*` tests in
tests/test_fakejira.py (the check's own notes are not part of this repository). Every response text marked "verified" below is
copied from that run; shapes follow its evidence. All data is
fictional (site example-site.atlassian.net, invented accountIds). Behaviour that run never observed is
labelled at its line:
  SIMULATED   modelled from the documentation, never seen live (foreign edits: U2; moved keys)
  SYNTHETIC   invented for tests (429/503 and Retry-After: U4)
  U6          attachment upload ran live once (LV2, 2026-11-12); download never; the rest is our reading of [OAS]
  UNVERIFIED  an error text or rule that run did not record; the fake's own wording, kept short
  fake:       a message that only the fake gives (unsupported input; a real site would answer differently)

Plug-in (the skj.client opener contract):
    fake = FakeJira()
    client = Client(fake.site, fake.email, fake.token, opener=fake)   # the instance is the opener
    client = fake.client()                                           # same, sleep recorded in fake.sleeps
    fake.opener(method, url, headers, body, timeout) -> (status, headers dict, bytes)  (b"" when empty)
It never raises for an HTTP status; injected transport failures raise OSError (TimeoutError by default).
A request to another host is a test bug: AssertionError.

Constructor: FakeJira(*, site, email, token, project="SPK", account_id=ME_ID, account_type="atlassian",
    permissions=None (dict merged over the Free-owner defaults), page_size=50 (caps search maxResults),
    search_quirk=None | "repeat_token" | "never_end" | "no_token", clock=None (callable -> aware datetime)).
Seeding (not logged): add_issue, add_comment, edit_comment (SIMULATED foreign edit), move_issue, user.
Injectors: inject(status, retry_after=, headers=, body=) queues an HTTP answer served before routing and never
applied; fail_transport(exc=None, on=None, applied=False) raises on request number `on` (1-based over all
requests; default the next one), after applying it when applied=True (a write that happened but timed out).
State: issues {id: dict}, comments {id: dict}, transitions (list of dicts; tests may edit it), log, sleeps.
Log entry: {"method", "path" (relative to /rest/api/3), "query" (dict), "write", "auth", "status"}; write is
every non-GET except POST /search/jql and POST /search/approximate-count; auth is "Basic [redacted]",
"[redacted]" or None; status is None for a transport failure. No header, body, email or token is logged.
"""
import base64
import datetime
import email.parser
import email.policy
import json
import re
import urllib.parse

SITE = "example-site.atlassian.net"
EMAIL = "dev.person@example.com"
TOKEN = "FAKE-tok-9f3a7c1e5b2d-NOTREAL"
ME_ID = "5f0c1a2b3c4d5e6f7a8b9c0d"  # invented
OTHER_ID = "5e9d8c7b6a5f4e3d2c1b0a99"  # invented
API = "/rest/api/3"
JSON = "application/json;charset=UTF-8"

# --- verified texts (row number) -------------------------------------------------------------------------
T_NO_AUTH = b"Client must be authenticated to access this resource."  # row 1, a text body
T_NO_PROJECT = "No project could be found with key '%s'."  # row 1
T_NO_ISSUE = "Issue does not exist or you do not have permission to see it."  # row 7, missing key or project
T_FIELD = "Field '%s' cannot be set. It is not on the appropriate screen, or unknown."  # row 3 (status)
T_BAD_TRANSITION = "Transition id '%s' is not valid for this issue."  # row 3
T_NOT_ADF = "Operation value must be an Atlassian Document (see the Atlassian Document Format)"  # row 4
T_COMMENT_INVALID = "Comment body is not valid!"  # row 4, a plain string body
T_COMMENT_EMPTY = "Comment body can not be empty!"  # row 4, an empty text node
T_INVALID_INPUT = "INVALID_INPUT"  # row 4, an unknown node
T_NO_COMMENT = "Can not find a comment for the id: %s."  # row 5
T_UNBOUNDED = "Unbounded JQL queries are not allowed here. Please add a search restriction to your query."  # row 7
T_QUOTE = "Error in the JQL Query: The quoted string '%s' has not been completed. (line 1, character %d)"  # row 7
T_GONE = ("The requested API has been removed. Please migrate to the /rest/api/3/search/jql API. "
          "A full migration guideline is available at https://developer.atlassian.com/changelog/#CHANGE-2046")  # row 7

# --- the team-managed project (row 2, row 3) -------------------------------------------------------------
TYPES = {"Epic": ("10001", False, 1), "Subtask": ("10002", True, -1), "Task": ("10003", False, 0),
         "Story": ("10004", False, 0)}  # name: (id, subtask, hierarchyLevel); Task's id is invented
CATEGORIES = {"new": (2, "blue-gray", "To Do"), "indeterminate": (4, "yellow", "In Progress"),
              "done": (3, "green", "Done")}
STATUSES = {"To Do": ("10000", "new"), "In Progress": ("3", "indeterminate"),
            "In Review": ("10001", "indeterminate"), "Done": ("10002", "done")}
TRANSITIONS = (("11", "To Do"), ("21", "In Progress"), ("31", "In Review"), ("41", "Done"))
FREE_OWNER = dict.fromkeys(  # row 1: mypermissions of the Free site owner; SYSTEM_ADMIN is false
    "BROWSE_PROJECTS CREATE_ISSUES EDIT_ISSUES TRANSITION_ISSUES DELETE_ISSUES LINK_ISSUES ADD_COMMENTS "
    "EDIT_ALL_COMMENTS EDIT_OWN_COMMENTS DELETE_ALL_COMMENTS DELETE_OWN_COMMENTS CREATE_ATTACHMENTS "
    "ADMINISTER_PROJECTS MODIFY_REPORTER ADMINISTER".split(), True)
FREE_OWNER["SYSTEM_ADMIN"] = False

# ADF node types from Atlassian's ADF structure page; any other type is INVALID_INPUT (row 4)
ADF_NODES = frozenset(
    "doc blockquote bodiedSyncBlock bulletList codeBlock expand heading mediaGroup mediaSingle orderedList panel "
    "paragraph rule syncBlock table multiBodiedExtension listItem tableRow tableCell tableHeader nestedExpand media "
    "date emoji hardBreak inlineCard mention status text mediaInline".split())

_READ_POSTS = ("/search/jql", "/search/approximate-count")
_STAMP = "%Y-%m-%dT%H:%M:%S.%f%z"
_START = datetime.datetime(2026, 1, 5, 9, 0, 0, tzinfo=datetime.timezone.utc)


def _err(status, messages=(), errors=None):
    return status, {"errorMessages": list(messages), "errors": dict(errors or {})}


def _is_doc(v):
    return isinstance(v, dict) and v.get("type") == "doc" and v.get("version") == 1 and isinstance(v.get("content"), list)


def _walk(node):
    yield node
    for child in node.get("content") or [] if isinstance(node, dict) else []:
        yield from _walk(child)


def _text(adf):
    return "".join(n.get("text", "") for n in _walk(adf) if isinstance(n, dict) and n.get("type") == "text")


def _body_error(body):
    """Row 4 comment-body validation (create and edit)."""
    if body is None:
        return _err(400, errors={"comment": T_COMMENT_EMPTY})  # UNVERIFIED: a missing body was not sent live
    if not _is_doc(body):
        return _err(400, errors={"comment": T_COMMENT_INVALID})
    nodes = list(_walk(body))
    if any(not isinstance(n, dict) or n.get("type") not in ADF_NODES for n in nodes):
        return _err(400, [T_INVALID_INPUT], {"comment": T_INVALID_INPUT})
    if not body["content"] or any(n["type"] == "text" and not n.get("text") for n in nodes):
        return _err(400, errors={"comment": T_COMMENT_EMPTY})
    return None


class _Req:
    __slots__ = ("method", "path", "query", "headers", "raw")

    def __init__(self, method, path, query, headers, raw):
        self.method, self.path, self.query, self.headers, self.raw = method, path, query, headers, raw

    def json(self):
        try:
            return json.loads(self.raw.decode("utf-8")) if self.raw else None
        except ValueError:
            raise _Answer(*_err(400, ["fake: request body is not JSON"])) from None


class _Answer(Exception):
    def __init__(self, status, body=None, headers=None):
        super().__init__(status)
        self.status, self.body, self.headers = status, body, headers or {}


# --- JQL subset -----------------------------------------------------------------------------------------
# Enough for the queries skj.keys builds: `parent = "K" ORDER BY key`, `project = "P" AND labels = "x" ORDER BY key`,
# `key in ("A", "B") ORDER BY key`; clauses joined by AND, values quoted or bare, ORDER BY key [ASC|DESC].
_TOKEN_RE = re.compile(r'\s*(?:(")|(\()|(\))|(,)|(=)|([A-Za-z0-9_.-]+)|(\S))')
_FIELDS = {"parent": "parent", "project": "project", "space": "project", "labels": "labels", "key": "key",
           "issuekey": "key"}


def _lex(jql):
    out, i = [], 0
    while True:
        m = _TOKEN_RE.match(jql, i)
        if not m:
            return out
        if m.group(1):  # a quoted string; backslash escapes the next character
            start, j, val = m.start(1), m.end(1), []
            while j < len(jql) and jql[j] != '"':
                if jql[j] == "\\" and j + 1 < len(jql):
                    j += 1
                val.append(jql[j])
                j += 1
            if j >= len(jql):
                raise _Answer(*_err(400, [T_QUOTE % (jql[start + 1:], start + 1)]))
            out.append(("str", "".join(val)))
            i = j + 1
        elif m.group(7):  # an operator the fake does not support (~, !=, <): lexed, refused by the parser
            out.append(("other", m.group(7)))
            i = m.end()
        else:
            kind = ("(", ")", ",", "=", "word")[[g is not None for g in m.groups()[1:6]].index(True)]
            out.append((kind, m.group(m.lastindex)))
            i = m.end()


def _parse_jql(jql):
    """-> (clauses [(field, [values])], descending). Raises _Answer 400 like Jira does."""
    toks, pos, clauses = _lex(jql), [0], []

    def peek(k=0):
        return toks[pos[0] + k] if pos[0] + k < len(toks) else (None, None)

    def take(kind=None):
        tok = peek()
        if tok[0] is None or (kind and tok[0] != kind):
            raise _Answer(*_err(400, ["Error in the JQL Query: fake: unsupported query near %r" % (tok[1],)]))
        pos[0] += 1
        return tok

    def word(w):
        return peek()[0] == "word" and peek()[1].lower() == w

    def value():
        tok = peek()
        if tok[0] not in ("str", "word"):
            take("str")
        return take()[1]

    while peek()[0] is not None and not (word("order") and peek(1)[1] and peek(1)[1].lower() == "by"):
        if clauses:
            if not word("and"):
                take("and")
            take()
        name = take("word")[1]
        field = _FIELDS.get(name.lower())
        if field is None:  # UNVERIFIED wording
            raise _Answer(*_err(400, ["Field '%s' does not exist or you do not have permission to view it." % name]))
        if word("in"):
            take()
            take("(")
            vals = [value()]
            while peek()[0] == ",":
                take()
                vals.append(value())
            take(")")
        else:
            take("=")
            vals = [value()]
        clauses.append((field, vals))
    desc = False
    if peek()[0] is not None:
        take(), take()  # ORDER BY
        if take("word")[1].lower() not in ("key", "issuekey"):
            raise _Answer(*_err(400, ["Error in the JQL Query: fake: only ORDER BY key is supported"]))
        if word("asc") or word("desc"):
            desc = take()[1].lower() == "desc"
        take("end") if peek()[0] is not None else None
    if not clauses:
        raise _Answer(*_err(400, [T_UNBOUNDED]))
    return clauses, desc


class FakeJira:
    def __init__(self, *, site=SITE, email=EMAIL, token=TOKEN, project="SPK", account_id=ME_ID,
                 account_type="atlassian", permissions=None, page_size=50, search_quirk=None, clock=None):
        if search_quirk not in (None, "repeat_token", "never_end", "no_token"):
            raise ValueError("unknown search_quirk")
        self.site, self.email, self.token, self.project = site, email, token, project
        self.account_id, self.account_type = account_id, account_type
        self.permissions = dict(FREE_OWNER, **(permissions or {}))
        self.page_size, self.search_quirk = page_size, search_quirk
        self._clock, self._tick = clock, 0
        self.issues, self.comments, self.transitions = {}, {}, [
            {"id": i, "name": n, "to": n, "hasScreen": False, "isConditional": False, "fields": {}}
            for i, n in TRANSITIONS]
        self.log, self.sleeps = [], []
        self._keys, self._ids, self._n = {}, {}, 0  # key -> issue id (incl. moved aliases); counters
        self._answers, self._failures = [], {}

    # --- plumbing ------------------------------------------------------------------------------------------

    def client(self, **kw):
        from skj.client import Client
        kw.setdefault("sleep", self.sleeps.append)
        return Client(self.site, self.email, self.token, opener=self, **kw)

    def _next_id(self, kind):
        self._ids[kind] = self._ids.get(kind, 9999) + 1
        return self._ids[kind]

    def _now(self, after=None):
        """A Jira timestamp; with `after` (a stamp), strictly later than it even under a constant clock."""
        if self._clock:
            t = self._clock()
        else:
            self._tick += 1
            t = _START + datetime.timedelta(seconds=self._tick)
        t = t.replace(microsecond=t.microsecond // 1000 * 1000)
        if after is not None:
            prev = datetime.datetime.strptime(after, _STAMP)
            if t <= prev:  # row 5 (verified): an edit always moves `updated`
                t = prev + datetime.timedelta(milliseconds=1)
        return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03d" % (t.microsecond // 1000) + t.strftime("%z")

    def _url(self, path):
        return "https://%s%s%s" % (self.site, API, path)

    def user(self, account_id, account_type="atlassian"):
        """A user object as Jira renders it (names and emails invented)."""
        me = account_id == self.account_id
        return {"self": self._url("/user?accountId=" + account_id), "accountId": account_id,
                "emailAddress": self.email if me else "other.person@example.com",
                "avatarUrls": {}, "displayName": "Fictional Me" if me else "Fictional Other", "active": True,
                "timeZone": "Etc/UTC", "accountType": self.account_type if me else account_type}

    def _me(self):
        return self.user(self.account_id)

    # --- injectors -----------------------------------------------------------------------------------------

    def inject(self, status, *, retry_after=None, headers=None, body=None):
        """SYNTHETIC (U4): queue one HTTP answer (e.g. 429/503), served FIFO before routing, never applied."""
        h = {"Content-Type": JSON, "X-RateLimit-Limit": "200", "X-RateLimit-Remaining": "0"}
        if status == 429:
            h["RateLimit-Reason"] = "jira-burst-based"
        if retry_after is not None:
            h["Retry-After"] = str(retry_after)
        h.update(headers or {})
        self._answers.append((status, h, body if body is not None else {
            "errorMessages": ["fake: synthetic %d" % status], "errors": {}}))

    def fail_transport(self, exc=None, *, on=None, applied=False):
        """Raise `exc` (default TimeoutError) on request number `on` (default: the next request)."""
        self._failures[on or self._n + 1] = (exc or TimeoutError("timed out"), applied)

    # --- seeding (not logged) -------------------------------------------------------------------------------

    def add_issue(self, summary, *, type="Story", parent=None, labels=(), status="To Do", description=None):
        if type not in TYPES or status not in STATUSES:
            raise ValueError("unknown type or status")
        pid = self._find(parent)["id"] if parent else None
        iid = str(self._next_id("issue"))
        key = "%s-%d" % (self.project, self._next_id("number") - 9999)
        self.issues[iid] = {"id": iid, "key": key, "type": type, "summary": summary, "status": status,
                            "parent": pid, "labels": list(labels), "description": description,
                            "attachments": [], "links": [], "changelog": [], "comments": []}
        self._keys[key] = iid
        return key

    def issue(self, ref):
        return self._find(ref)

    def add_comment(self, ref, body, *, author=None, visibility=None):
        """Seed a comment; `author` (a user() dict) other than me is SIMULATED (U2)."""
        issue = self._find(ref)
        cid, now, who = str(self._next_id("comment")), self._now(), author or self._me()
        self.comments[cid] = {"id": cid, "issue": issue["id"], "author": who, "updateAuthor": who, "body": body,
                              "created": now, "updated": now, "visibility": visibility, "properties": {}}
        issue["comments"].append(cid)
        return cid

    def edit_comment(self, cid, body, *, editor):
        """SIMULATED (U2), not observed: someone (`editor`, a user() dict) edits a comment; like an own edit it
        changes body, `updated` and `updateAuthor` only and leaves no changelog entry (row 5)."""
        c = self.comments[cid]
        c.update(body=body, updated=self._now(after=c["updated"]), updateAuthor=editor)

    def move_issue(self, ref, new_key):
        """SIMULATED from [OAS] ("check for moved issues"): the old key keeps answering with the new one."""
        issue = self._find(ref)
        issue["key"] = new_key
        self._keys[new_key] = issue["id"]

    def writes(self):
        return [e for e in self.log if e["write"]]

    # --- the opener -----------------------------------------------------------------------------------------

    def opener(self, method, url, headers, body, timeout):
        parts = urllib.parse.urlsplit(url)
        assert parts.scheme == "https" and parts.netloc == self.site, "fake: request to an unexpected host"
        assert parts.path.startswith(API + "/"), "fake: not a REST v3 path"
        path = parts.path[len(API):]
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parts.query, keep_blank_values=True).items()}
        h = {str(k).lower(): str(v) for k, v in headers.items()}
        auth = h.get("authorization")
        entry = {"method": method, "path": path, "query": query,
                 "write": method != "GET" and not (method == "POST" and path in _READ_POSTS),
                 "auth": None if auth is None else "Basic [redacted]" if auth.startswith("Basic ") else "[redacted]",
                 "status": None}
        self.log.append(entry)
        self._n += 1
        fail = self._failures.pop(self._n, None)
        if fail and not fail[1]:
            raise fail[0]
        status, rh, out = self._serve(_Req(method, path, query, h, body or b""))
        if fail:
            raise fail[0]
        entry["status"] = status
        if isinstance(out, (dict, list)):
            out = json.dumps(out).encode("utf-8")
        return status, rh, out if out is not None else b""

    __call__ = opener

    def _serve(self, req):
        if self._answers:
            return self._answers.pop(0)
        login = self._login(req.headers.get("authorization"))
        if login is not None:
            m = re.fullmatch(r"/project/([^/]+)", req.path)
            if login == "missing" and req.method == "GET" and m:  # row 1: anonymous project read, no leak
                return 404, {"Content-Type": JSON}, _err(404, [T_NO_PROJECT % m.group(1)])[1]
            rh = {"Content-Type": JSON, "WWW-Authenticate": 'OAuth realm="https%%3A%%2F%%2F%s"' % self.site}
            if login == "bad":  # row 1 auth incident; the body of a bad-credential 401 is UNVERIFIED
                rh["X-Seraph-LoginReason"] = "AUTHENTICATED_FAILED"
            return 401, rh, T_NO_AUTH
        rh = {"Content-Type": JSON, "Ratelimit-Policy": '"jira-burst-based";q=%d;w=1' % (
            50 if req.method in ("PUT", "DELETE") else 100), "X-Ratelimit-Limit": "200", "X-Ratelimit-Remaining": "199"}
        try:
            status, body = self._route(req)
        except _Answer as a:
            status, body = a.status, a.body
        return status, rh, body

    def _login(self, auth):
        """None when the Basic credential matches; else "missing" or "bad"."""
        if auth is None:
            return "missing"
        if not auth.startswith("Basic "):
            return "bad"
        try:
            user, sep, token = base64.b64decode(auth[6:], validate=True).decode("utf-8").partition(":")
        except ValueError:  # binascii.Error and UnicodeDecodeError are ValueErrors
            return "bad"
        return None if sep and (user, token) == (self.email, self.token) else "bad"

    _ROUTES = [(m, re.compile(p + r"\Z"), h) for m, p, h in (
        ("GET", "/myself", "myself"), ("GET", "/mypermissions", "mypermissions"),
        ("GET", "/project/([^/]+)", "project"),
        ("POST", "/issue", "create_issue"), ("GET", "/issue/([^/]+)", "get_issue"),
        ("PUT", "/issue/([^/]+)", "edit_issue"),
        ("GET", "/issue/([^/]+)/transitions", "get_transitions"), ("POST", "/issue/([^/]+)/transitions", "transition"),
        ("GET", "/issue/([^/]+)/comment", "list_comments"), ("POST", "/issue/([^/]+)/comment", "add_comment_rest"),
        ("GET", "/issue/([^/]+)/comment/([^/]+)", "get_comment"),
        ("PUT", "/issue/([^/]+)/comment/([^/]+)", "edit_comment_rest"),
        ("DELETE", "/issue/([^/]+)/comment/([^/]+)", "delete_comment"),
        ("PUT", "/comment/([^/]+)/properties/([^/]+)", "set_property"),
        ("GET", "/comment/([^/]+)/properties/([^/]+)", "get_property"),
        ("GET", "/issue/([^/]+)/changelog", "changelog"),
        ("GET", "/issue/([^/]+)/remotelink", "list_links"), ("POST", "/issue/([^/]+)/remotelink", "add_link"),
        ("POST", "/issue/([^/]+)/attachments", "attach"),
        ("GET", "/search/jql", "search"), ("POST", "/search/jql", "search"),
        ("POST", "/search/approximate-count", "count"),
        ("GET", "/search", "old_search"), ("POST", "/search", "old_search"),
    )]

    def _route(self, req):
        for method, rx, name in self._ROUTES:
            m = rx.match(req.path)
            if m and method == req.method:
                return getattr(self, "_h_" + name)(req, *m.groups())
        return _err(404, ["fake: no route for %s %s" % (req.method, req.path)])

    # --- lookups and rendering ------------------------------------------------------------------------------

    def _find(self, ref):
        """By numeric id, or by key case-insensitively (row 2); 404 shape of row 7 otherwise."""
        ref = str(ref)
        iid = ref if ref in self.issues else self._keys.get(ref.upper())
        if iid is None:
            raise _Answer(*_err(404, [T_NO_ISSUE]))
        return self.issues[iid]

    def _comment(self, issue, cid):
        c = self.comments.get(cid)
        if c is None or c["issue"] != issue["id"]:
            raise _Answer(*_err(404, [T_NO_COMMENT % cid]))
        return c

    def _status(self, name):
        sid, cat = STATUSES[name]
        cid, color, cname = CATEGORIES[cat]
        return {"self": self._url("/status/" + sid), "description": "", "iconUrl": "", "name": name, "id": sid,
                "statusCategory": {"self": self._url("/statuscategory/%d" % cid), "id": cid, "key": cat,
                                   "colorName": color, "name": cname}}

    def _issuetype(self, name):
        tid, sub, level = TYPES[name]
        return {"self": self._url("/issuetype/" + tid), "id": tid, "name": name, "subtask": sub,
                "hierarchyLevel": level}

    def _ref(self, issue):
        """Row 2: the shape of fields.parent (and of each subtask)."""
        return {"id": issue["id"], "key": issue["key"], "self": self._url("/issue/" + issue["id"]),
                "fields": {"summary": issue["summary"], "status": self._status(issue["status"]),
                           "priority": {"self": self._url("/priority/3"), "name": "Medium", "id": "3"},
                           "issuetype": self._issuetype(issue["type"])}}

    def _render_comment(self, c):
        issue = self.issues[c["issue"]]
        out = {"self": self._url("/issue/%s/comment/%s" % (issue["id"], c["id"])), "id": c["id"],
               "author": c["author"], "body": c["body"], "updateAuthor": c["updateAuthor"],
               "created": c["created"], "updated": c["updated"], "jsdPublic": True}
        if c["visibility"] is not None:
            out["visibility"] = c["visibility"]
        return out

    def _fields(self, issue, names):
        everything = {
            "summary": lambda: issue["summary"], "issuetype": lambda: self._issuetype(issue["type"]),
            "status": lambda: self._status(issue["status"]),
            "resolution": lambda: None,  # row 3: always null in team-managed, even after Done
            "parent": lambda: self._ref(self.issues[issue["parent"]]),
            "subtasks": lambda: [self._ref(i) for i in self._sorted(self.issues.values()) if i["parent"] == issue["id"]
                                 and TYPES[i["type"]][1]],
            "labels": lambda: list(issue["labels"]), "description": lambda: issue["description"],
            "project": lambda: {"self": self._url("/project/10000"), "id": "10000", "key": self.project},
            "priority": lambda: {"self": self._url("/priority/3"), "name": "Medium", "id": "3"},
            "attachment": lambda: list(issue["attachments"]),
            "comment": lambda: self._comment_page(issue, 0, 100),
        }
        out = {}
        for n in (names if names is not None else everything):
            if n in everything and not (n == "parent" and issue["parent"] is None):
                out[n] = everything[n]()
        return out

    def _render_issue(self, issue, names):
        out = {"expand": "renderedFields,names,schema,operations,editmeta,changelog,versionedRepresentations",
               "id": issue["id"], "self": self._url("/issue/" + issue["id"]), "key": issue["key"]}
        fields = self._fields(issue, names)
        if names is None or fields:
            out["fields"] = fields
        return out

    @staticmethod
    def _sorted(issues, desc=False):
        return sorted(issues, key=lambda i: (i["key"].rsplit("-", 1)[0], int(i["key"].rsplit("-", 1)[1])),
                      reverse=desc)

    def _log_change(self, issue, items):
        issue["changelog"].append({"id": str(self._next_id("change")), "author": self._me(),
                                   "created": self._now(), "items": items})

    def _comment_page(self, issue, start, size):
        ids = issue["comments"]
        return {"startAt": start, "maxResults": size, "total": len(ids),
                "comments": [self._render_comment(self.comments[c]) for c in ids[start:start + size]]}

    # --- handlers: identity and project (row 1) ---------------------------------------------------------------

    def _h_myself(self, req):
        return 200, dict(self._me(), locale="en_US", groups={"size": 0, "items": []},
                         applicationRoles={"size": 1, "items": []}, expand="groups,applicationRoles")

    def _h_mypermissions(self, req):
        want = [p for p in req.query.get("permissions", "").split(",") if p] or list(self.permissions)
        return 200, {"permissions": {p: {"key": p, "name": p.replace("_", " ").title(), "type": "PROJECT",
                                         "havePermission": bool(self.permissions.get(p, False))} for p in want}}

    def _h_project(self, req, key):
        if key.upper() != self.project and key != "10000":
            return _err(404, [T_NO_PROJECT % key])
        return 200, {"self": self._url("/project/10000"), "id": "10000", "key": self.project, "style": "next-gen",
                     "simplified": True, "projectTypeKey": "software", "isPrivate": False,
                     "issueTypes": [self._issuetype(n) for n in TYPES]}

    # --- handlers: issues (rows 2 to 4) -----------------------------------------------------------------------

    def _h_create_issue(self, req):
        body = req.json()
        f = body.get("fields") if isinstance(body, dict) else None
        if not isinstance(f, dict):
            return _err(400, ["fake: body needs a fields object"])
        bad = {}  # every text here except description is UNVERIFIED wording
        proj = f.get("project") or {}
        if not (isinstance(proj, dict) and (proj.get("key") == self.project or proj.get("id") == "10000")):
            bad["project"] = "Specify a valid project ID or key"
        it = f.get("issuetype") or {}
        itype = next((n for n, (tid, _, _) in TYPES.items() if isinstance(it, dict) and it.get("name") == n
                      or isinstance(it, dict) and it.get("id") == tid), None)
        if itype is None:
            bad["issuetype"] = "Specify an issue type"
        if not (isinstance(f.get("summary"), str) and f["summary"].strip()):
            bad["summary"] = "You must specify a summary of the issue."
        if "description" in f and f["description"] is not None and not _is_doc(f["description"]):
            bad["description"] = T_NOT_ADF
        parent = f.get("parent")
        pid = None
        if parent is not None:
            ref = parent.get("key") or parent.get("id") if isinstance(parent, dict) else None
            try:
                pid = self._find(ref)["id"] if ref else None
            except _Answer:
                pid = None
            if pid is None:
                bad["parent"] = "Could not find issue by id or key."
        elif itype and TYPES[itype][1]:
            bad["parent"] = "Given parent work item does not belong to appropriate hierarchy."
        labels = f.get("labels", [])
        if not (isinstance(labels, list) and all(isinstance(x, str) for x in labels)):
            bad["labels"] = "fake: labels must be a list of strings"
        if bad:
            return _err(400, errors=bad)
        key = self.add_issue(f["summary"], type=itype, parent=pid, labels=labels, description=f.get("description"))
        issue = self._find(key)
        return 201, {"id": issue["id"], "key": key, "self": self._url("/issue/" + issue["id"])}

    def _h_get_issue(self, req, ref):
        issue = self._find(ref)  # the stored key comes back, whatever case was asked (row 2)
        names = req.query.get("fields")
        return 200, self._render_issue(issue, None if names is None else [n for n in names.split(",") if n])

    def _h_edit_issue(self, req, ref):
        issue = self._find(ref)
        body = req.json()
        f = body.get("fields") if isinstance(body, dict) else None
        if not isinstance(f, dict) or set(body) - {"fields"}:
            return _err(400, ["fake: only a fields object is supported"])
        bad = {n: T_FIELD % n for n in f if n not in ("summary", "description", "labels")}  # row 3 for status
        if "description" in f and f["description"] is not None and not _is_doc(f["description"]):
            bad["description"] = T_NOT_ADF  # row 4
        if bad:
            return _err(400, errors=bad)
        issue.update(f)  # shortcut: no changelog entry for field edits (not probed live), add one when a test needs it
        return 204, None

    def _h_get_transitions(self, req, ref):
        self._find(ref)
        expand = "transitions.fields" in req.query.get("expand", "")
        out = []
        for t in self.transitions:  # the list includes the current state (row 3)
            r = {"id": t["id"], "name": t["name"], "to": self._status(t["to"]), "hasScreen": t["hasScreen"],
                 "isGlobal": True, "isInitial": False, "isAvailable": True, "isConditional": t["isConditional"],
                 "isLooped": False}
            if expand:
                r["fields"] = t["fields"]
            out.append(r)
        return 200, {"expand": "transitions", "transitions": out}

    def _h_transition(self, req, ref):
        issue = self._find(ref)
        body = req.json()
        t = body.get("transition") if isinstance(body, dict) else None
        tid = str(t.get("id")) if isinstance(t, dict) else None
        match = next((x for x in self.transitions if x["id"] == tid), None)
        if match is None:
            return _err(400, [T_BAD_TRANSITION % tid])
        old, new = issue["status"], match["to"]
        issue["status"] = new
        self._log_change(issue, [{"field": "status", "fieldtype": "jira", "fieldId": "status",
                                  "from": STATUSES[old][0], "fromString": old,
                                  "to": STATUSES[new][0], "toString": new}])
        return 204, None

    def _h_changelog(self, req, ref):
        values = self._find(ref)["changelog"]
        return 200, {"self": self._url("/issue/%s/changelog" % ref), "maxResults": 100, "startAt": 0,
                     "total": len(values), "isLast": True, "values": values}

    # --- handlers: comments (rows 4, 5) -----------------------------------------------------------------------

    def _h_list_comments(self, req, ref):
        issue = self._find(ref)
        try:
            start, size = int(req.query.get("startAt", 0)), int(req.query.get("maxResults", 100))
        except ValueError:
            return _err(400, ["fake: startAt and maxResults must be integers"])
        return 200, self._comment_page(issue, start, size)

    def _h_add_comment_rest(self, req, ref):
        issue = self._find(ref)
        body = req.json()
        body = body if isinstance(body, dict) else {}
        err = _body_error(body.get("body"))
        if err:
            return err
        vis = body.get("visibility")
        if vis is not None and not (isinstance(vis, dict) and vis.get("type") in ("group", "role")
                                    and isinstance(vis.get("value"), str) and vis["value"]):
            return _err(400, ["fake: malformed visibility"])  # role-name checks are not modelled (row 5: inconclusive)
        cid = self.add_comment(issue["id"], body["body"], visibility=vis)
        return 201, self._render_comment(self.comments[cid])

    def _h_get_comment(self, req, ref, cid):
        return 200, self._render_comment(self._comment(self._find(ref), cid))

    def _h_edit_comment_rest(self, req, ref, cid):
        c = self._comment(self._find(ref), cid)
        body = req.json()
        err = _body_error(body.get("body") if isinstance(body, dict) else None)
        if err:
            return err
        self.edit_comment(cid, body["body"], editor=self._me())  # row 5: only updated and updateAuthor move
        return 200, self._render_comment(c)

    def _h_delete_comment(self, req, ref, cid):
        issue = self._find(ref)
        c = self._comment(issue, cid)
        del self.comments[cid]
        issue["comments"].remove(cid)
        self._log_change(issue, [{"field": "Comment", "fieldtype": "jira", "fieldId": "comment", "from": None,
                                  "fromString": _text(c["body"]), "to": None, "toString": None}])
        # row 5 verified only that a delete adds an item with field "Comment"; fieldId, from/to and fromString
        # (the old text here) are UNVERIFIED: the probe kept field names only. Never build on the text.
        return 204, None

    def _h_set_property(self, req, cid, key):
        c = self.comments.get(cid)
        if c is None:
            return _err(404, [T_NO_COMMENT % cid])
        new = key not in c["properties"]
        c["properties"][key] = req.json()  # row 5: `updated` does not move
        return (201 if new else 200), None  # 201 verified; 200 on overwrite is UNVERIFIED

    def _h_get_property(self, req, cid, key):
        c = self.comments.get(cid)
        if c is None or key not in c["properties"]:
            return _err(404, ["fake: no property %s" % key])
        return 200, {"key": key, "value": c["properties"][key]}

    # --- handlers: remote links (row 6) and attachments (U6) ----------------------------------------------------

    def _h_list_links(self, req, ref):
        issue = self._find(ref)
        return 200, [dict(link, self=self._url("/issue/%s/remotelink/%d" % (issue["key"], link["id"])))
                     for link in issue["links"]]

    def _h_add_link(self, req, ref):
        issue = self._find(ref)
        body = req.json()
        if not (isinstance(body, dict) and isinstance(body.get("object"), dict)):
            return _err(400, ["fake: a remote link needs an object"])
        gid = body.get("globalId")
        old = next((x for x in issue["links"] if gid is not None and x.get("globalId") == gid), None)
        if old is not None:  # same globalId: updated in place, 200 with the same id (row 6)
            lid = old["id"]
            old.clear()
            old.update(body, id=lid)
            status = 200
        else:
            lid = self._next_id("link")
            issue["links"].append(dict(body, id=lid))
            status = 201
        return status, {"id": lid, "self": self._url("/issue/%s/remotelink/%d" % (issue["key"], lid))}

    def _h_attach(self, req, ref):
        """Upload ran live once (LV2); otherwise our reading of [OAS]: multipart, part `file`, X-Atlassian-Token."""
        issue = self._find(ref)
        if req.headers.get("x-atlassian-token") != "no-check":
            return _err(403, ["fake: XSRF check failed (UNVERIFIED wording)"])
        ctype = req.headers.get("content-type", "")
        if not ctype.startswith("multipart/form-data"):
            return _err(415, ["fake: attachments need multipart/form-data"])
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + ctype.encode("latin-1") + b"\r\n\r\n" + req.raw)
        files = [p for p in msg.iter_parts() if p.get_param("name", header="content-disposition") == "file"]
        if not files:
            return _err(400, ["fake: no part named file"])
        out = []
        for part in files:
            data = part.get_payload(decode=True) or b""
            aid = str(self._next_id("attachment"))
            att = {"self": self._url("/attachment/" + aid), "id": aid, "filename": part.get_filename(),
                   "author": self._me(), "created": self._now(), "size": len(data),
                   "mimeType": part.get_content_type(), "content": self._url("/attachment/content/" + aid)}
            issue["attachments"].append(att)
            out.append(att)
        return 200, out

    # --- handlers: search (rows 7, 8) -------------------------------------------------------------------------

    def _select(self, jql):
        clauses, desc = _parse_jql(jql)

        def ok(issue):
            for field, vals in clauses:
                if field == "project":
                    hit = any(v.upper() == self.project for v in vals)
                elif field == "labels":
                    hit = any(v in issue["labels"] for v in vals)
                else:  # parent / key: a value that names no issue matches nothing (real Jira may 400: UNVERIFIED)
                    ids = {self._keys.get(v.upper(), v) for v in vals}
                    hit = (issue["parent"] if field == "parent" else issue["id"]) in ids
                if not hit:
                    return False
            return True

        return [i for i in self._sorted(self.issues.values(), desc) if ok(i)]

    def _h_search(self, req):
        if req.method == "POST":
            body = req.json()
            q = body if isinstance(body, dict) else {}
            names = q.get("fields")
        else:
            q = req.query
            names = q["fields"].split(",") if "fields" in q else None
        jql = q.get("jql", "")
        hits = self._select(jql)
        try:
            size = min(int(q.get("maxResults", 50)), self.page_size)
            token = q.get("nextPageToken")
            start = json.loads(base64.urlsafe_b64decode(token.encode()).decode())["o"] if token else 0
        except (ValueError, KeyError, TypeError, AttributeError):
            return _err(400, ["fake: invalid maxResults or nextPageToken"])
        names = [n for n in (names or []) if n not in ("key", "id")]  # /search/jql returns id only by default
        page = {"issues": [self._render_issue(i, names) for i in hits[start:start + size]]}
        more = start + size < len(hits)
        quirk = self.search_quirk
        if quirk == "no_token":
            page["isLast"] = False
        elif quirk == "repeat_token" or quirk == "never_end" or more:
            nxt = 0 if quirk == "repeat_token" else start + size
            page["nextPageToken"] = base64.urlsafe_b64encode(json.dumps({"o": nxt}).encode()).decode()
            page["isLast"] = False
        else:
            page["isLast"] = True
        return 200, page

    def _h_count(self, req):
        body = req.json()
        return 200, {"count": len(self._select(body.get("jql", "") if isinstance(body, dict) else ""))}

    def _h_old_search(self, req):
        return _err(410, [T_GONE])  # row 7 (GET verified; POST /search assumed the same)
