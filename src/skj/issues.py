"""Issue reads and search, and the two writes of `push` (W3): `create_subtask` and `do_transition`.

Every read passes `fields` explicitly: the real `/search/jql` returns only `id` by default, so a call
without `fields` could pass against the fake and fail live. `/search` (removed, 410) is never called.

JQL guard: `search_jql` takes a plain string but accepts only the shapes `keys.py` builds (one or more
AND-joined `parent|project|labels = "<value>"` or `key in ("<value>", ...)` clauses, then an optional
`ORDER BY key`), each value double-quoted from a conservative charset. So the JQL is always restricted
(never unbounded) and free text cannot enter it, even from a caller that bypassed `keys.py`.

Server data is never echoed into error text except keys that passed `keys.valid_key`.

Writes are never retried (the client's rule, S7): a timeout, a 5xx or a malformed 2xx answer raises `WriteUncertain`, and the
caller re-reads and looks for its marker before acting again. The status is never written with `PUT fields.status` (it gives
400 on Jira, row 3 of the live check in tests/test_fakejira.py): a state changes only through `POST /issue/{key}/transitions`.
"""
import re

from . import keys
from .client import ClientError, WriteUncertain

DEFAULT_FIELDS = ["summary", "status", "issuetype", "parent", "labels", "description", "subtasks"]
PERMISSIONS = ("BROWSE_PROJECTS", "CREATE_ISSUES", "EDIT_ISSUES", "TRANSITION_ISSUES", "LINK_ISSUES", "ADD_COMMENTS",
               "EDIT_ALL_COMMENTS", "EDIT_OWN_COMMENTS", "DELETE_ALL_COMMENTS", "DELETE_OWN_COMMENTS",
               "ADMINISTER_PROJECTS")
MAX_PAGE_CAP = 100
PAGE_SIZE = 100

_ID_RE = re.compile(r"^[1-9][0-9]{0,18}\Z")  # the numeric id is stored and may reach a path
_TRANSITION_ID_RE = re.compile(r"^[1-9][0-9]{0,9}\Z")
MAX_SUMMARY = 255  # Jira's summary limit; UNVERIFIED whether it counts characters or UTF-16 units: both are capped (conservative)
_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,63}\Z")
_VAL = r'"[A-Za-z0-9_.-]{1,100}"'
_CLAUSE = r"(?:(?:parent|project|labels) = %s|key in \(%s(?:, %s){0,99}\))" % (_VAL, _VAL, _VAL)
_JQL_RE = re.compile(r"^%s(?: AND %s)*(?: ORDER BY key)?\Z" % (_CLAUSE, _CLAUSE))


class ReadError(ClientError):
    """Jira's answer was not usable (wrong shape, bad pagination, wrong account type). Fixed text."""


class KeyMismatch(ReadError):
    """A read answered with a different issue key than the one requested (a moved issue)."""


def _fields(fields):
    if fields is None:
        return list(DEFAULT_FIELDS)
    if not (isinstance(fields, list) and fields and all(isinstance(f, str) and _FIELD_RE.fullmatch(f) for f in fields)):
        raise ValueError("fields must be a non-empty list of field names")
    return list(fields)


def _id_ok(i):
    return isinstance(i, str) and _ID_RE.fullmatch(i) is not None


def _issue(d, need_key=True):
    """An issue dict from the server: a str `id` and a valid `key` (the key is optional for search only)."""
    ok = isinstance(d, dict) and _id_ok(d.get("id"))
    if ok and (need_key or "key" in d):
        ok = keys.valid_key(d.get("key"))
    if not ok:
        raise ReadError("malformed issue in Jira's answer; refused")
    return d


def get(c, key, fields=None):
    """The issue as Jira returns it (`id`, `key`, `fields`). KeyMismatch when the key differs from `key`."""
    if not keys.valid_key(key):
        raise ValueError("invalid issue key")
    f = _fields(fields)
    data = c.request("GET", "/issue/" + key, params={"fields": ",".join(f)}).data
    if not (isinstance(data, dict) and _id_ok(data.get("id")) and isinstance(data.get("key"), str)):
        raise ReadError("malformed issue in Jira's answer; refused")
    if data["key"] != key:
        shown = data["key"] if keys.valid_key(data["key"]) else "a different key"
        raise KeyMismatch("Jira answered %s for %s; refused" % (shown, key))
    return data


def children(c, key):
    """The subtasks of `key`, from its `subtasks` field; a search by parent only if that field is absent."""
    data = get(c, key, ["subtasks"])
    subs = (data.get("fields") or {}).get("subtasks") if isinstance(data.get("fields"), dict) else None
    if subs is None:
        return [_issue(i) for i in search_jql(c, keys.jql_children(key), ["summary", "status", "issuetype"])]
    if not isinstance(subs, list):
        raise ReadError("malformed subtasks in Jira's answer; refused")
    return [_issue(i) for i in subs]


def search_jql(c, jql, fields, page_cap=20):
    """All issues matching `jql` (see the module docstring for the accepted shapes), following nextPageToken."""
    if not (isinstance(jql, str) and _JQL_RE.fullmatch(jql)):
        raise ValueError("jql is not one of the bounded shapes built by keys.py")
    if fields is None:
        raise ValueError("fields must be a non-empty list of field names")  # no default: always explicit
    f = _fields(fields)
    if type(page_cap) is not int or not 1 <= page_cap <= MAX_PAGE_CAP:
        raise ValueError("page_cap must be an int from 1 to %d" % MAX_PAGE_CAP)
    out, seen, token = [], set(), None
    for _ in range(page_cap):
        body = {"jql": jql, "fields": f, "maxResults": PAGE_SIZE}
        if token:
            body["nextPageToken"] = token
        page = c.request("POST", "/search/jql", body=body, retry_safe=True).data
        if not (isinstance(page, dict) and isinstance(page.get("issues"), list)):
            raise ReadError("malformed search answer from Jira; refused")
        out += [_issue(i, need_key=False) for i in page["issues"]]
        token = page.get("nextPageToken")  # this page's token, read before isLast defaults from it
        last = page.get("isLast", token is None)
        if type(last) is not bool or (token is not None and not (isinstance(token, str) and token)):
            raise ReadError("malformed search answer from Jira; refused")
        if last != (token is None):
            raise ReadError("search answered isLast=%s with%s nextPageToken; stopped" % (last, "" if token else " no"))
        if token is None:
            return out
        if token in seen:
            raise ReadError("search repeated a nextPageToken; stopped")
        seen.add(token)
    raise ReadError("search exceeded the page cap of %d; stopped" % page_cap)


def me(c):
    """Only accountId, accountType and active; displayName and emailAddress are dropped on purpose (X3)."""
    d = c.request("GET", "/myself").data
    if not (isinstance(d, dict) and isinstance(d.get("accountId"), str) and keys.ACCOUNT_RE.fullmatch(d["accountId"])
            and type(d.get("active")) is bool and isinstance(d.get("accountType"), str)):
        raise ReadError("malformed /myself answer from Jira; refused")
    if d["accountType"] != "atlassian":
        raise ReadError("the account is not an Atlassian user account (accountType); refused")
    return {"accountId": d["accountId"], "accountType": "atlassian", "active": d["active"]}


def permissions(c, project, names=PERMISSIONS, omitted_false=True):
    """{permission name: bool} for the token's user on `project`, asking for `names` only (each one of PERMISSIONS); a name Jira
    omits counts as False, or, with `omitted_false=False` (doctor: a probe that cannot tell never says ok), is a ReadError."""
    if not keys._ok(keys.PROJECT_RE, project):
        raise ValueError("invalid project")
    if not names or any(n not in PERMISSIONS for n in names):
        raise ValueError("unknown permission name")
    d = c.request("GET", "/mypermissions", params={"projectKey": project, "permissions": ",".join(names)}).data
    perms = d.get("permissions") if isinstance(d, dict) else None
    if not isinstance(perms, dict):
        raise ReadError("malformed /mypermissions answer from Jira; refused")
    out = {}
    for name in names:
        entry = perms.get(name, {"havePermission": False} if omitted_false else None)
        if not (isinstance(entry, dict) and type(entry.get("havePermission")) is bool):
            raise ReadError("malformed /mypermissions answer from Jira; refused")
        out[name] = entry["havePermission"]
    return out


def subtask_type(c, project, name):
    """{"id", "name"} of the one issue type of `project` named `name` that is a subtask type (`subtask: true`, level -1), from
    `GET /project/{key}` (`issueTypes`, live-check row 2 in tests/test_fakejira.py). None or several: ReadError (fixed text)."""
    if not keys._ok(keys.PROJECT_RE, project):
        raise ValueError("invalid project")
    d = c.request("GET", "/project/" + project).data
    types = d.get("issueTypes") if isinstance(d, dict) and d.get("key") == project else None
    if not isinstance(types, list):
        raise ReadError("malformed project answer from Jira; refused")
    hits = [t for t in types if isinstance(t, dict) and t.get("name") == name and t.get("subtask") is True
            and t.get("hierarchyLevel", -1) == -1 and _id_ok(t.get("id"))]
    if len(hits) != 1:
        raise ReadError("no single subtask issue type named by subtask_type in the project; set subtask_type in skj.config.json")
    return {"id": hits[0]["id"], "name": name}


def create_subtask(c, project, parent, type_id, summary, labels=("skj",)):
    """POST /issue: a subtask of `parent` (by key). Returns {"id", "key"} (validated). Never retried; a malformed answer is
    WriteUncertain (the issue may exist)."""
    if not (keys._ok(keys.PROJECT_RE, project) and keys.valid_key(parent) and parent.rsplit("-", 1)[0] == project
            and _id_ok(type_id)):
        raise ValueError("invalid project, parent key or issue type id")
    if not (isinstance(summary, str) and summary.strip() and len(summary) <= MAX_SUMMARY and summary.isprintable()
            and len(summary.encode("utf-16-le", "surrogatepass")) // 2 <= MAX_SUMMARY):
        raise ValueError("the summary must be printable text of 1 to %d characters" % MAX_SUMMARY)
    if not all(keys._ok(keys.LABEL_RE, x) for x in labels):
        raise ValueError("invalid label")
    body = {"fields": {"project": {"key": project}, "parent": {"key": parent}, "issuetype": {"id": type_id},
                       "summary": summary, "labels": list(labels)}}
    d = c.request("POST", "/issue", body=body).data
    if not (isinstance(d, dict) and _id_ok(d.get("id")) and keys.valid_key(d.get("key"))
            and d["key"].rsplit("-", 1)[0] == project):
        raise WriteUncertain("POST", "a malformed answer from Jira")
    return {"id": d["id"], "key": d["key"]}


def transitions(c, key):
    """The `transitions` list of `key` (untrusted: `transitions.choose` validates every entry). Always with
    `expand=transitions.fields`, so the chooser sees required fields."""
    if not keys.valid_key(key):
        raise ValueError("invalid issue key")
    d = c.request("GET", "/issue/%s/transitions" % key, params={"expand": "transitions.fields"}).data
    if not (isinstance(d, dict) and isinstance(d.get("transitions"), list)):
        raise ReadError("malformed transitions answer from Jira; refused")
    return d["transitions"]


def do_transition(c, key, transition_id):
    """POST /issue/{key}/transitions with one transition id (chosen by `transitions.choose`). Never retried."""
    if not (keys.valid_key(key) and isinstance(transition_id, str) and _TRANSITION_ID_RE.fullmatch(transition_id)):
        raise ValueError("invalid issue key or transition id")
    c.request("POST", "/issue/%s/transitions" % key, body={"transition": {"id": transition_id}})
