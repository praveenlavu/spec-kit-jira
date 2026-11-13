"""`skj push`: mirror the `T###` tasks of tasks.md into subtasks of a story (W3; ported from the fork's push.py).

Plan (reads only): the story's children come from `GET /issue/{key}?fields=subtasks`, then one `GET /issue/{child}` each
(summary, labels, status, parent), never from a search right after a create (J7). A child is ours when its summary starts with
`T###` (3 to 9 digits, then a space or the end) AND it carries the label `skj`; anything else is ignored. Per task:

  create      no subtask yet: POST /issue (summary `T001 title`, label skj, parent {key}). A ticked task's done transition is
              planned by the NEXT push, once the subtask exists and its own transitions can be read (never guessed, K3).
  skip        the subtask exists and nothing is to be done (unticked tasks are never moved: no backwards transition).
  transition  ticked, the subtask's status category is not `done`: the transition `transitions.choose_done` picked (id and name).
  no-op       ticked and already in a `done` category status.
  refused     ticked, but the chooser refused (code shown; nothing is fired for it).
  orphan      a subtask of ours whose T### tasks.md no longer lists: reported, never deleted or transitioned (K10).
  duplicate   a second subtask of ours with the same T### (the lowest id is the one used): reported, never touched; a ticked task
              with duplicates gets no transition either (`refused: duplicate`).
A T###-prefixed subtask WITHOUT the label is not ours: it is ignored, and the plan names its key (`unlabelled T###-like subtask
ignored`). An unticked task whose subtask is already done shows as a `skip (Jira: done)` row.

Apply stops at the first write that fails or is uncertain (`WriteUncertain`: a timeout, a 5xx or a malformed answer) and says
what was done and what was not; writes already made stay made, and the next run reconciles. After an uncertain create it
re-reads the story's children once and reports whether the subtask exists; it never creates it again in that run. Writes are
paced: at most PACE_WRITES in any PACE_WINDOW seconds (all writes, so also per issue; Jira's real limit for token traffic is
UNVERIFIED, U4). There is no PUT at all: a status changes only through a transition.

Untrusted text: task titles come from tasks.md (agent/PR-controlled) and are cleaned (control, format, bidi, separator
characters dropped, whitespace collapsed, cut to 255); the only server text in the plan is the transition name, cleaned by the
chooser. Every table cell is escaped. Errors carry fixed text and an HTTP status at most, never server text. The DoD ledger is not
touched (the fork's push does not write it either).

The summary is capped at 255 characters AND 255 UTF-16 code units (an emoji counts 2): UNVERIFIED which one Jira counts, so the
cap is the conservative one; the cut is on a code-point boundary and drops a base character whose combining marks were cut off.

Known limit (J7): a subtask renamed by a human between a lost create response and the re-read may be duplicated on the next run.
"""
import re
import time
import unicodedata

from . import issues, keys, transitions
from .client import ClientError, JiraError, WriteUncertain, why

LABEL = "skj"
MAX_TASKS = 200
MAX_CHILDREN = 500
MAX_SUMMARY = issues.MAX_SUMMARY
PACE_WRITES, PACE_WINDOW = 20, 2.0  # U4: documented burst for one issue, UNVERIFIED for token traffic
ACTIONS = ("create", "skip", "transition", "no-op", "refused", "orphan", "duplicate")
_ID_RE = re.compile(r"^T[0-9]{3,9}\Z")
_PREFIX_RE = re.compile(r"T[0-9]{3,9}(?= |\Z)")  # anchored by .match: constant work whatever follows
_STATUS_ID_RE = re.compile(r"^[1-9][0-9]{0,9}\Z")
_CATEGORIES = frozenset({"new", "indeterminate", "done", "undefined"})
_DEFERRED = "done: on the next push, after the create"
_REFUSALS = dict(transitions.MESSAGES, duplicate="Two subtasks carry this task id. Nothing is fired until one is removed or relabelled.",
                 summary="The summary built for this task would not carry its task id. Nothing is created or fired for it.")


def _clean(s):
    """No control, format (bidi), surrogate, private-use, unassigned or line/paragraph separator characters; whitespace runs to one
    space. Linear."""
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C" and unicodedata.category(ch) not in ("Zl", "Zp"))
    return " ".join(s.split())


def _units(s):
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def _mark(ch):
    return unicodedata.category(ch) in ("Mn", "Mc", "Me")


class SummaryRefused(ValueError):
    """The summary built for a task would not be recognised as that task later (or breaks a cap): the task is refused, not created."""


def _checked(tid, s, cut):
    """Hard invariant (not `assert`, which -O strips): the summary names `tid` by its prefix and fits both caps."""
    if not (task_id_of(s) == tid and len(s) <= MAX_SUMMARY and _units(s) <= MAX_SUMMARY):
        raise SummaryRefused("the summary built for a task would not carry its task id; that task is refused")
    return s, cut


def summary(task):
    """(summary, cut): `T001 title`, @mentions and addresses replaced as in a posted decision (`decisions._scrub`), cleaned, at most MAX_SUMMARY characters and UTF-16 units (a longer one ends in `…`, the same
    every run). The cut never reaches into the `T### ` prefix: when nothing but the id is left, the summary is the id alone. A
    regional-indicator pair (a flag) can be cut in half at the boundary: accepted, cosmetic only. SummaryRefused when the result
    breaks the invariant (`_checked`). Linear."""
    from . import decisions  # lazy: decisions imports this module
    tid = task["id"]
    s = _clean("%s %s" % (tid, decisions._scrub(_clean(task["title"]))))  # cleaned first: `a<ZWSP>@x.com`; D-014: no @mention or address reaches Jira (AGENTS.md rule 8)
    if len(s) <= MAX_SUMMARY and _units(s) <= MAX_SUMMARY:
        return _checked(tid, s, False)
    floor = len(tid) + 1  # `T### `: never popped
    out, n = [], 0
    for ch in s:
        w = 2 if ord(ch) > 0xFFFF else 1
        if n + w > MAX_SUMMARY - 1:  # room for the one-unit `…`
            break
        out.append(ch)
        n += w
    if _mark(s[len(out)]):  # the cut split a base from its marks: drop the whole cluster
        while len(out) > floor and _mark(out[-1]):
            out.pop()
        if len(out) > floor:
            out.pop()
    body = "".join(out).rstrip()
    return _checked(tid, tid if body == tid else body + "…", True)


def task_id_of(text):
    """The `T###` a subtask summary starts with, or None."""
    m = _PREFIX_RE.match(text) if isinstance(text, str) else None
    return m.group(0) if m else None


def check_tasks(task_list):
    """Refuse (ValueError, fixed text) before any request: too many tasks, an id too long, an id twice."""
    if len(task_list) > MAX_TASKS:
        raise ValueError("tasks.md lists more than %d tasks; nothing read or written" % MAX_TASKS)
    seen = set()
    for t in task_list:
        if not _ID_RE.fullmatch(t["id"]):
            raise ValueError("tasks.md has a task id that is not ASCII T followed by 3 to 9 digits; nothing read or written")
        if t["id"] in seen:
            raise ValueError("tasks.md lists a task id twice: %s; nothing read or written" % t["id"])
        seen.add(t["id"])


def _child(c, story, ref):
    """{"tid", "key", "id", "status_id", "category"} of one listed subtask when it is ours, else None."""
    ref = issues._issue(ref)
    f = ref.get("fields")
    if isinstance(f, dict) and isinstance(f.get("summary"), str) and task_id_of(f["summary"]) is None:
        return None  # the listing already says it is not ours: no read
    d = issues.get(c, ref["key"], ["summary", "labels", "status", "parent"])
    f = d.get("fields") if isinstance(d.get("fields"), dict) else {}
    parent = f.get("parent")
    if not (isinstance(parent, dict) and parent.get("key") == story):
        raise issues.ReadError("a subtask of %s in Jira's answer has another parent; refused" % story)
    labels, tid = f.get("labels"), task_id_of(f.get("summary"))
    if not (isinstance(labels, list) and all(isinstance(x, str) for x in labels)):
        raise issues.ReadError("malformed labels in Jira's answer; refused")
    if tid is None:
        return None
    st = f.get("status")
    cat = st.get("statusCategory") if isinstance(st, dict) else None
    if not (isinstance(st.get("id") if isinstance(st, dict) else None, str) and _STATUS_ID_RE.fullmatch(st["id"])
            and isinstance(cat, dict) and cat.get("key") in _CATEGORIES):
        raise issues.ReadError("malformed status in Jira's answer; refused")
    return {"tid": tid, "key": d["key"], "id": d["id"], "status_id": st["id"], "category": cat["key"], "labelled": LABEL in labels}


def read_children(c, story):
    """The T###-prefixed subtasks of `story` by numeric id (the lowest id of a T### is the one used); only `labelled` ones are ours."""
    data = issues.get(c, story, ["subtasks"])
    subs = data.get("fields", {}).get("subtasks") if isinstance(data.get("fields"), dict) else None
    if not isinstance(subs, list):
        raise issues.ReadError("malformed subtasks in Jira's answer; refused")
    if len(subs) > MAX_CHILDREN:
        raise issues.ReadError("the story has more than %d subtasks; refused" % MAX_CHILDREN)
    out = [x for x in (_child(c, story, s) for s in subs) if x is not None]
    return sorted(out, key=lambda x: int(x["id"]))


def _op(action, task, child=None, **kw):
    return dict({"action": action, "task": task, "subtask": child["key"] if child else None}, **kw)


def build_ops(task_list, children, transition_for):
    """The ops in tasks.md order, then orphans and duplicates by id. `transition_for(child)` -> a `choose` result or raises
    TransitionRefused; it is called only for a ticked task whose subtask is not in a done category."""
    mine, extra = {}, []
    for ch in (x for x in children if x["labelled"]):  # sorted by id: the first of a T### is the one used
        if ch["tid"] in mine:
            extra.append(ch)
        else:
            mine[ch["tid"]] = ch
    ops, listed = [], set()
    for t in task_list:
        listed.add(t["id"])
        ch = mine.get(t["id"])
        try:
            s, cut = summary(t)
        except SummaryRefused:
            ops.append(_op("refused", t["id"], ch, code="summary"))
            continue
        if ch is None:
            ops.append(_op("create", t["id"], summary=s, cut=cut, deferred=t["done"]))
        elif not t["done"]:
            ops.append(_op("skip", t["id"], ch, summary=s, cut=cut, jira_done=ch["category"] == "done"))
        elif ch["category"] == "done":
            ops.append(_op("no-op", t["id"], ch, summary=s, cut=cut))
        elif any(x["tid"] == t["id"] for x in extra):
            ops.append(_op("refused", t["id"], ch, summary=s, cut=cut, code="duplicate"))
        else:
            try:
                ops.append(_op("transition", t["id"], ch, summary=s, cut=cut, transition=transition_for(ch)))
            except transitions.TransitionRefused as e:
                ops.append(_op("refused", t["id"], ch, summary=s, cut=cut, code=e.code))
    ops += [_op("orphan", tid, ch) for tid, ch in sorted(mine.items(), key=lambda x: int(x[1]["id"])) if tid not in listed]
    ops += [_op("duplicate", ch["tid"], ch) for ch in extra]
    return ops


def plan(c, cfg, story, task_list):
    """{"key", "type", "ops"}: reads only. `type` ({"id", "name"} of the subtask issue type) is looked up only when a create is
    planned."""
    check_tasks(task_list)
    if not (keys.valid_key(story) and story.rsplit("-", 1)[0] == cfg.project):
        raise ValueError("not an issue key of project %s" % cfg.project)
    children = read_children(c, story)
    ops = build_ops(task_list, children, lambda ch: transitions.choose_done(  # a configured name must land in done (H1a)
        issues.transitions(c, ch["key"]), ch["status_id"], cfg.transitions))
    typ = issues.subtask_type(c, cfg.project, cfg.subtask_type) if any(o["action"] == "create" for o in ops) else None
    return {"key": story, "project": cfg.project, "type": typ, "ops": ops,
            "unlabelled": [(ch["tid"], ch["key"]) for ch in children if not ch["labelled"]]}


# --- the plan text ----------------------------------------------------------------------------------------------------------------

def _cell(text):
    return _clean(text).replace("\\", "\\\\").replace("|", "\\|")


def _what(o):
    a = o["action"]
    if a == "create":
        return _DEFERRED if o.get("deferred") else "-"
    if a == "transition":
        return "%s %s" % (o["transition"]["id"], _cell(o["transition"]["name"]))
    return {"no-op": "already done", "refused": "refused: %s" % o.get("code"), "skip": "-",
            "orphan": "never deleted or transitioned (K10)", "duplicate": "not touched"}[a]


def render(p):
    """The canonical plan text (deterministic; no timestamps, no people, no server text but cleaned transition names). Rows that
    write or need attention come first; existing subtasks with nothing to do are one wrapped line, so the gate shows the writes."""
    import textwrap
    ops = p["ops"]
    counts = {a: sum(o["action"] == a for o in ops) for a in ACTIONS}
    lines = ["Plan for %s: push tasks.md to subtasks (label %s)" % (p["key"], LABEL),
             " · ".join("%s %d" % (a, n) for a, n in counts.items())]
    if p.get("type"):
        lines.append("subtask type: %s (id %s)" % (_cell(p["type"]["name"]), p["type"]["id"]))
    lines.append("")
    if counts["orphan"] and not any(counts[a] for a in ("create", "skip", "transition", "no-op", "refused")):
        lines += ["⚠ tasks.md lists none of the %d subtask(s) already on the story: was it emptied or rewritten? Check it before "
                  "approving." % counts["orphan"], ""]
    lines += ["| action | task | subtask | summary | transition |", "|---|---|---|---|---|"]
    for o in ops:
        if o["action"] != "skip" or o.get("jira_done"):
            s = (_cell(o["summary"]) + (" (cut to %d)" % MAX_SUMMARY if o.get("cut") else "")) if "summary" in o else "-"
            act = "skip (Jira: done)" if o["action"] == "skip" else o["action"]
            lines.append("| %s | %s | %s | %s | %s |" % (act, o["task"], o["subtask"] or "-", s, _what(o)))
    same = [o["task"] for o in ops if o["action"] == "skip" and not o.get("jira_done")]
    if same:
        lines += [""] + textwrap.wrap("skip: %s (%d)" % (", ".join(same), len(same)), width=100, break_long_words=False,
                                      break_on_hyphens=False)
    codes = sorted({o["code"] for o in ops if o["action"] == "refused"})
    if codes:
        lines += [""] + ["refused %s: %s" % (code, _REFUSALS[code]) for code in codes]
    if p.get("unlabelled"):
        lines += [""] + ["unlabelled %s-like subtask ignored: %s" % (tid, key) for tid, key in p["unlabelled"]]
    return "\n".join(lines) + "\n"


# --- apply ------------------------------------------------------------------------------------------------------------------------

class _Pacer:
    """At most `n` writes in any `window` seconds, by the injected clock; sleeps (injected) until the oldest leaves the window."""

    def __init__(self, clock, sleep, n=PACE_WRITES, window=PACE_WINDOW):
        self.clock, self.sleep, self.n, self.window, self.stamps = clock, sleep, n, window, []

    def wait(self):
        if len(self.stamps) >= self.n:
            wait = self.stamps[-self.n] + self.window - self.clock()
            if wait > 0:
                self.sleep(wait)
        self.stamps.append(self.clock())


def _label(o):
    return "%s %s" % (o["action"], o["task"]) + (" %s" % o["subtask"] if o["subtask"] else "")


def _recheck(c, story, tid):
    """After an uncertain create: one re-read of the children (GET, consistent: J7), never a search."""
    try:
        found = [ch for ch in read_children(c, story) if ch["tid"] == tid and ch["labelled"]]
    except ClientError:
        return "write uncertain: %s: the re-read failed; run push again to see whether it exists" % tid
    if found:
        return "write uncertain: %s: found %s on re-read (created)" % (tid, found[0]["key"])
    return "write uncertain: %s: not found on re-read; not created again in this run" % tid


def apply(c, p, clock=time.monotonic, sleep=time.sleep):
    """Perform the plan's writes in order: {"lines", "stopped"}. `stopped` is None when every write was made."""
    pace, lines, stopped = _Pacer(clock, sleep), [], None
    todo = [o for o in p["ops"] if o["action"] in ("create", "transition")]
    for i, o in enumerate(todo):
        pace.wait()
        try:
            if o["action"] == "create":
                r = issues.create_subtask(c, p["project"], p["key"], p["type"]["id"], o["summary"], (LABEL,))
                lines.append("created %s %s" % (o["task"], r["key"]))
            else:
                issues.do_transition(c, o["subtask"], o["transition"]["id"])
                lines.append("transitioned %s %s to %s (id %s)" % (o["task"], o["subtask"], _cell(o["transition"]["to_name"]),
                                                                   o["transition"]["id"]))
        except WriteUncertain:
            stopped = _recheck(c, p["key"], o["task"]) if o["action"] == "create" else (
                "write uncertain: %s: the transition may have happened; run push again to see" % _label(o))
        except JiraError as e:
            stopped = "failed: %s: %s; stopped" % (_label(o), why(e))
        except ClientError:
            stopped = "failed: %s: the request did not complete; stopped" % _label(o)
        if stopped:
            lines.append(stopped)
            lines += ["not done: %s" % _label(x) for x in todo[i + 1:]]
            break
    return {"lines": lines, "stopped": stopped}
