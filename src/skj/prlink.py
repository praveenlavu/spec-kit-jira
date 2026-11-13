"""`skj pr link KEY --url URL [--title T] [--apply]`: link on merge without any app (P2).

Two writes, in this order, each paced and never retried: `POST /issue/{key}/remotelink` with `{"globalId": <PR URL>, "object": {"url":
<PR URL>, "title": "<KEY>: <title>"}}` (Jira creates the link, or updates it in place when the globalId exists: 201 then 200 with the
same id, live-check row 6 in tests/test_fakejira.py), then ONE kit comment `[skj:pr <hash8>]` (W4's machinery in questions.py, imported: `_compose`, `_own_comment`,
`_recheck`; the comment counts only when the kit's own account wrote exactly this body). The fork's link step is specific to its own tracker (artifact
links on the work item); only its shape is kept. No network for the title: it is passed (`--title`, in CI the PR event's title) and is
PR-author controlled, so it is made inert (`questions._safe`, `[skj:` broken, cut at 200) and a Jira smart-commit shape refuses (P2-AC3).
The URL must be a GitHub pull request URL (https, github.com or GH_HOST, lowercase host, /owner/repo/pull/N), kept byte for byte.
"""
import json
import re
import time
from urllib.parse import urlsplit

from . import client as _client, gate, issues, keys, prs, push, questions

MAX_URL = 255  # characters: the URL is the globalId (Jira's limit is UNVERIFIED)
MAX_TITLE_IN = 1024  # characters of --title looked at; more refuses (GitHub caps a PR title at 256, UNVERIFIED)
MAX_TITLE = 200  # characters of the cleaned title
MAX_LINKS = 1000  # remote links of one issue read; more refuses
HEAD = "Pull request linked to this story:"
_MARKER_RE = re.compile(r"\[skj:pr ([0-9a-f]{8})\]\Z")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
_NUMBER = re.compile(r"[1-9][0-9]{0,8}\Z")

E_URL = "not a GitHub pull request URL (https://github.com/<owner>/<repo>/pull/<n>, or the GH_HOST host); nothing written"
E_TITLE = "the PR title is longer than 1024 characters; nothing written"
E_SMART = ("the PR title holds a Jira smart-commit shape (`KEY #word`), which could move the story; nothing written (reword the title)")
E_LINKS = "the remote links of the story could not be read safely (malformed, ambiguous or too many); nothing written"
E_SIZE = "the link comment would be larger than the 32 KiB local cap; nothing written"
E_TOO_MANY = ("more than 20 own comments on the story cannot be read by the kit; nothing written (one of them could be its "
              "link comment: posting again could duplicate it)")
E_UNREADABLE = ("an own comment the kit cannot read (%s) may carry a link marker or could not be checked; nothing written "
                "(a second one could be a duplicate): run the command again, and if it persists remove or fix that comment in Jira")


def check_url(url, env=None):
    """`url` unchanged, or ValueError(E_URL): printable ASCII without blanks, at most MAX_URL, exactly `https://<host><path>` with a
    lowercase host that is github.com or GH_HOST (`prs.pr_url`: no credentials, port, query or fragment), path /owner/repo/pull/N with
    GitHub's owner characters, a repo name that is not `.` or `..`, and N of 1 to 9 digits without a leading zero."""
    if not (isinstance(url, str) and len(url) <= MAX_URL and url.isascii() and url.isprintable() and " " not in url):
        raise keys.UsageError(E_URL)
    try:
        prs.pr_url(url, env)  # env None: this process's environment, as `gh` would see it (GH_HOST)
        u = urlsplit(url)
        parts = u.path.split("/")
        ok = (url == "https://%s%s" % (u.netloc, u.path) and u.netloc == u.hostname and len(parts) == 5
              and _OWNER.match(parts[1]) and _REPO.match(parts[2]) and parts[2] not in (".", "..") and _NUMBER.match(parts[4]))
    except ValueError:
        ok = False
    if not ok:
        raise keys.UsageError(E_URL)
    return url


def link_title(key, title):
    """The link's title `<KEY>: <text>` (the key alone without a text): the text one line, made inert (`questions._safe`: control and bidi
    dropped, mentions and addresses scrubbed, inline Markdown broken; a `[skj:` marker broken), a leading key not repeated, cut at
    MAX_TITLE after the scrub. ValueError (fixed text) over MAX_TITLE_IN, or when the raw or the cleaned title, after the key, holds a
    smart-commit shape (the gate's regex)."""
    title = title or ""
    if len(title) > MAX_TITLE_IN:
        raise keys.UsageError(E_TITLE)
    if gate.smart_command(key + " " + title):  # the raw text too: a shape the cleaning or the cut would hide still refuses
        raise keys.UsageError(E_SMART)
    t = prs._MARKER.sub("(", questions._safe(prs.clean(title, MAX_TITLE_IN)))
    if t == key or t.startswith((key + " ", key + ":")):
        t = t[len(key):].lstrip(" :")
    if len(t) > MAX_TITLE:
        t = t[:MAX_TITLE - 1] + "…"
    out = key + (": " + t if t else "")
    if gate.smart_command(out):
        raise keys.UsageError(E_SMART)
    return out


def _link_id(v):
    return type(v) is int and 0 < v < 1 << 63


def _find_link(c, key, url):
    """(id, its object) of the remote link whose globalId is `url`, or (None, None). ReadError(E_LINKS) on a list that is not a list of
    link objects with integer ids, holds more than MAX_LINKS, or two links with this globalId."""
    d = c.request("GET", "/issue/%s/remotelink" % key).data
    if not (isinstance(d, list) and len(d) <= MAX_LINKS and all(isinstance(x, dict) and _link_id(x.get("id"))
                                                                 and isinstance(x.get("object"), dict) for x in d)):
        raise issues.ReadError(E_LINKS)
    hits = [x for x in d if x.get("globalId") == url]
    if len(hits) > 1:
        raise issues.ReadError(E_LINKS)
    return (hits[0]["id"], hits[0]["object"]) if hits else (None, None)


def plan_link(c, cfg, key, url, title):
    """Reads only: {"key", "url", "link_body", "link": post|none, "link_id", "action": post|noop (the comment), "md", "doc", "hash",
    "found", "report", "me"}. Local checks first (key, URL, title, the comment), then W4's reads (`questions._own_comment`: the issue
    and its project, the identity, every comment and the probe past the end, own unreadable comments inspected), then the links."""
    if not (keys.valid_key(key) and key.rsplit("-", 1)[0] == cfg.project):
        raise ValueError("not an issue key of project %s" % cfg.project)
    check_url(url)
    lt = link_title(key, title)
    body = {"globalId": url, "object": {"url": url, "title": lt}}
    md, doc, h = questions._compose("pr", [(HEAD, [lt, url])], E_SIZE)
    if gate.smart_command(md) or gate.smart_command(json.dumps(body, ensure_ascii=False)):
        raise keys.UsageError(E_SMART)
    p = {"key": key, "url": url, "link_body": body, "md": md, "doc": doc, "hash": h}
    p["me"], p["found"], p["report"] = questions._own_comment(c, cfg, key, md, _MARKER_RE, "[skj:pr", E_TOO_MANY, E_UNREADABLE)
    p["action"] = "noop" if p["found"] else "post"
    p["link_id"], obj = _find_link(c, key, url)
    p["link"] = "none" if obj is not None and obj.get("url") == url and obj.get("title") == lt else "post"
    return p


def _canon(body):
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_REPORTS = tuple((k, what.replace("no questions marker", "no link marker")) for k, what in questions._REPORTS)


def render_plan(p):
    """The canonical plan: deterministic, no people, no account ids; both bodies exactly as sent."""
    lines = ["Plan for %s: link the pull request (a remote link, then one comment)" % p["key"], "url: %s" % p["url"]]
    if p["link"] == "none":
        lines.append("remote link: none (link %d already shows this pull request)" % p["link_id"])
    elif p["link_id"] is not None:
        lines.append("remote link: post (updates link %d: its url or title differs)" % p["link_id"])
    else:
        lines.append("remote link: post (no link with this globalId yet)")
    if p["action"] == "noop":
        lines.append("comment: none (comment %s already shows this link)" % p["found"])
    else:
        lines.append("comment: post ONE new comment [skj:pr %s] (an earlier comment is never edited or deleted)" % p["hash"])
    for k, what in _REPORTS:
        if p["report"][k]:
            lines.append("note: %d %s: %s" % (len(p["report"][k]), what, ", ".join(p["report"][k])))
    if p["link"] == "post":
        lines += ["", "Remote link body (JSON, exactly what is sent):", "", "> " + _canon(p["link_body"])]
    if p["action"] == "post":
        lines += ["", "Comment body (ADF as Markdown, exactly what is sent):", ""]
        lines += ["> " + x if x else ">" for x in p["md"].split("\n")]
    return "\n".join(lines) + "\n"


def _recheck_link(c, p):
    try:
        lid, obj = _find_link(c, p["key"], p["url"])
    except _client.ClientError:
        return "write uncertain: the re-read of the remote links failed; not sent again in this run (the next run's re-read decides)"
    if obj is not None and obj.get("url") == p["url"] and obj.get("title") == p["link_body"]["object"]["title"]:
        return "linked (found on re-read): remote link %d" % lid
    return "write uncertain: the remote link was not found on re-read; not sent again in this run (the next run's re-read decides)"


def apply_link(c, p, clock=time.monotonic, sleep=time.sleep):
    """The plan's writes: {"lines", "ok"}. The link POST (when planned), then the comment POST (when planned), each paced, neither
    retried. A 4xx: that write did not happen, the rest is skipped (status only). An uncertain link write (timeout, 5xx, an answer it
    cannot read): one re-read of the links decides what is printed, and the comment waits for the next run. An uncertain comment: W4's
    fresh re-read of every comment; never posted again in this run."""
    lines, pacer = [], push._Pacer(clock, sleep)
    if p["link"] == "post":
        pacer.wait()
        try:
            d = c.request("POST", "/issue/%s/remotelink" % p["key"], body=p["link_body"]).data
            lid = d.get("id") if isinstance(d, dict) else None
            if not _link_id(lid):
                raise _client.WriteUncertain("POST", "a malformed answer from Jira")
            lines.append("linked: remote link %d" % lid)
        except _client.ClientError as e:
            if isinstance(e, _client.JiraError) and type(e.status) is int and 400 <= e.status < 500:
                return {"lines": ["failed: %s; no remote link written, no comment posted" % _client.why(e)], "ok": False}
            return {"lines": [_recheck_link(c, p), "comment: not posted in this run (the link write was uncertain; run again)"],
                    "ok": False}
    else:
        lines.append("nothing linked: remote link %d already shows this pull request" % p["link_id"])
    if p["action"] == "noop":
        return {"lines": lines + ["nothing posted: comment %s already shows this link" % p["found"]], "ok": True}
    pacer.wait()
    try:
        d = c.request("POST", "/issue/%s/comment" % p["key"], body={"body": p["doc"]}).data
        cid = d.get("id") if isinstance(d, dict) else None
        if not (isinstance(cid, str) and questions._CID.match(cid)):
            raise _client.WriteUncertain("POST", "a malformed answer from Jira")
        lines.append("posted comment %s [skj:pr %s]" % (cid, p["hash"]))
    except _client.ClientError as e:
        if isinstance(e, _client.JiraError) and type(e.status) is int and 400 <= e.status < 500:
            return {"lines": lines + ["failed: %s; no comment posted" % _client.why(e)], "ok": False}
        return {"lines": lines + [questions._recheck(c, p, _MARKER_RE)[1]], "ok": False}
    return {"lines": lines, "ok": True}
