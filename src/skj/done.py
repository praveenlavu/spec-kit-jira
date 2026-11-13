"""P4 `skj done`: close a story (ported from the fork's done.py, on the Jira model).

Rules, in order:
1. Local first, no request: the ledger `dod/DoD_<KEY>.md` (and `dod/Decisions_<KEY>.md` when it exists) is read as confined as the
   gate reads (`gate._read`: a regular file under the repository, no symbolic link, at most 1 MiB, UTF-8); anything else refuses.
2. Section 5 must be fully ticked (the fork's `check`): no unclosed fence or comment anywhere, a `## 5.` after the last `## 2.`
   marker (`dod.section`), at least one visible checkbox (fences and comments are not read: `dod._visible`), and every box ticked
   with `x` or `X`. Any other mark (`[ ]`, `[-]`, `[~]`, `[N/A]`, `N/A` as text, no box at all) is unticked: every list item of
   section 5 is an item (D-018). Deeper-indented, a list item is a free note only when its text starts with a letter or digit (DF6
   R4, after NFKC and with format characters removed); anything else (a box, `[NA]`, `☐`, `( )`, emphasis, a link) is an item. A
   note is free unless it says not-applicable (`N/A`, `N / A`, `n.a.`, `not applicable`, NFKC-folded) (DF5 D3). Not ready: no request at all, nothing written
   even with `--apply`, exit 1. There is NO override: the fork's `done` has no `--force` (P4-AC5).
3. Reads, in order: the story (`fields=status`; its status category alone decides "done", K3; no other field is read), then (only
   when not done) its transitions with `expand=transitions.fields`, chosen by `transitions.choose_done(..., cfg.transitions)`
   (a screen, a condition, required fields, none or two candidates: refused with the `transitions.done` hint), then the
   attachment plan (P3, `attachments.plan_attachments`: name AND size skip, the whole plan refused if any file is bad). The bytes
   P3 read for the upload must be the ledger that was checked.
4. Writes, behind the caller's X2 gate: the attachments FIRST, then the transition, only when every attachment is in place and
   none was uncertain (an upload found only on re-read also stops the run: the next run skips it and closes). An uncertain
   transition re-reads the status once. Nothing is retried. A status is never written with PUT: only the transition POST.
"""
import re
import time
import unicodedata

from . import attachments, decisions, dod, gate, issues, keys, transitions
from .client import ClientError, JiraError, WriteUncertain, why
from .push import _cell

# A list item checkbox (the fork's pattern): bullets, ordered items and quoted items; the mark is one character.
_BOX = re.compile(r"^[ \t>]*(?:[-*+]|\d+[.)])[ \t]+\[(.)\][ \t]*(.*)$", re.M)
# D-018: every list item of section 5 is a DoD item; one that is no one-character box (`[N/A]`, plain `N/A`, no box) is unticked
_ITEM = re.compile(r"^([ \t>]*)(?:[-*+]|\d+[.)])(?:[ \t]+(.*))?$", re.M)
_BREAK = re.compile(r"[ \t>]*([-*_])(?:[ \t]*\1){2,}[ \t]*")  # a thematic break (`* * *`, `- - -`) is no item (DF3 m2)
# DF5 D3: on the NFKC-folded line (`Ｎ/Ａ`, `N∕A` read as `N/A`), `N/A`, `N / A`, `n.a.`, `not applicable` anywhere in section 5 is never done
_NA = re.compile(r"(?<![a-z0-9])(?:n[ \t]*[/\\.][ \t]*a(?![a-z0-9])|not[ \t_-]*applicable)", re.I)
_SLASHES = str.maketrans({"\u2215": "/", "\u2044": "/", "\u29f8": "/", "\u2571": "/"})  # division, fraction, big solidus look-alikes
NA_SUFFIX = " (marked not applicable: not done)"
_STATUS_ID_RE = re.compile(r"^[1-9][0-9]{0,9}\Z")
_CATEGORIES = frozenset({"new", "indeterminate", "done", "undefined"})
MAX_SHOWN = 50  # unticked items listed in a plan
MAX_ITEM = 120  # characters of one listed item
FOUND = "(found on re-read)"  # attachments' line for an upload whose answer was lost: uncertain for `done`

UNCLOSED = "ledger has an unclosed code fence or comment"
NO_SECTION = "section 5 missing"
NO_CHECKLIST = "section 5 has no checklist"
E_LEDGER = ("cannot read the DoD ledger (missing, a symbolic link, outside the repository, not a regular file, over 1 MiB or not "
            "UTF-8); run `skj dod seed <KEY>` first; nothing read or written")
E_DECISIONS = ("the decisions file is not a regular file inside the repository (a symbolic link, outside it, a directory, over 1 MiB "
               "or not UTF-8); refusing to attach it; nothing read or written")
E_CHANGED = "the ledger changed while it was being read; nothing written: run done again"
E_STATUS = "malformed status in Jira's answer; refused; nothing written"
HINT = ("Set transitions.done in skj.config.json to the name of the one transition that closes a story (no screen, no condition, "
        "no required field); nothing is written, even with --apply.")
E_SENT_NOT_DONE = "the transition %s was sent but the story %s is not in a done status on re-read"
NOT_TRANSITIONED = "not transitioned: every attachment must be in place first; run done again"


def _fold(s):
    """The matching key of a section 5 line: format characters (zero-width, bidi, BOM) removed, NFKC, slash look-alikes read as `/`."""
    return unicodedata.normalize("NFKC", "".join(c for c in s if unicodedata.category(c) != "Cf")).translate(_SLASHES)


def _note(item):
    """DF6 R4 (allow-list): a deeper item is a free note only when its folded text starts with a letter or digit; a box glyph, `( )`,
    `\\[ ]`, emphasis, a link or an empty item is a DoD item (not done unless it is a ticked box)."""
    return _fold(item)[:1].isalnum()


def check(ledger_md):
    """{"ready", "unticked", "boxes"}: section 5 of the ledger fully ticked. Linear in the ledger."""
    if dod.unclosed(ledger_md):  # whatever follows the opener is hidden: never read it as ready
        return {"ready": False, "unticked": [UNCLOSED], "boxes": 0}
    body = dod.section(ledger_md, 5)
    if body is None:
        return {"ready": False, "unticked": [NO_SECTION], "boxes": 0}
    visible = dod._visible(body)[0]  # an example in a fence or a comment is not a box
    if not _BOX.search(visible):
        return {"ready": False, "unticked": [NO_CHECKLIST], "boxes": 0}
    items = [(len(re.sub(r">[ \t]?", "", m.group(1)).expandtabs(4)), m.group(2) or "") for m in _ITEM.finditer(visible)
             if not _BREAK.fullmatch(m.group(0))]  # indent without the quote markers
    top = min((n for n, _ in items), default=0)
    # an item at the boxes' own (least) indent is a DoD item, ticked or not (D-018); a deeper one is a note under it only when it is no
    # box and starts with a letter or digit (DF6 R4: an allow-list, so every other mark is an item)
    boxes = [(m.group(1), m.group(2)) if m else ("", item) for n, item in items for m in (_BOX.match("- " + item),)
             if m or n == top or not _note(item)]
    unticked = [t.strip() for mark, t in boxes if mark not in ("x", "X")]
    # DF4 N4 / DF5 D3: a line saying not-applicable, at any indent, is not done; one already listed is not listed twice
    for ln in visible.splitlines():
        if not _NA.search(_fold(ln)):
            continue
        x = _ITEM.sub(lambda m: m.group(2) or "", ln).strip()
        ticked = re.match(r"\[[xX]\][ \t]*", x)
        if ticked:  # DF5 D7: blocking, so never shown with its tick mark
            unticked.append(x[ticked.end():] + NA_SUFFIX)
        elif re.sub(r"^\[.\][ \t]*", "", x) not in unticked:  # a box is listed without its mark
            unticked.append(x)
    return {"ready": not unticked, "unticked": unticked, "boxes": len(boxes)}


def local(root, key):
    """{"ledger", "decisions", "check"} read from `<root>/dod/` (no request); `check` is computed once, here. ValueError (fixed text) for an unreadable ledger or decisions file."""
    import os
    try:
        path = dod.ledger_path(root, key)
    except ValueError:  # dod/ or the ledger is a symbolic link or the wrong kind of file
        raise ValueError(E_LEDGER) from None
    try:
        dec = decisions.decisions_file(root, key)
    except ValueError:
        raise ValueError(E_DECISIONS) from None
    md = gate._read(str(path), str(root), set())
    if md is None:
        raise ValueError(E_LEDGER)
    has_dec = os.path.lexists(str(dec))
    if has_dec and gate._read(str(dec), str(root), set()) is None:
        raise ValueError(E_DECISIONS)
    return {"ledger": md, "decisions": has_dec, "check": check(md)}


def _status(d):
    """{"id", "name", "category"} of the story's status, validated (the category is the only "done" signal)."""
    f = d.get("fields")
    st = f.get("status") if isinstance(f, dict) else None
    cat = st.get("statusCategory") if isinstance(st, dict) else None
    if not (isinstance(st, dict) and isinstance(st.get("id"), str) and _STATUS_ID_RE.fullmatch(st["id"])
            and isinstance(st.get("name"), str) and isinstance(cat, dict) and cat.get("key") in _CATEGORIES):
        raise issues.ReadError(E_STATUS)
    return {"id": st["id"], "name": transitions._clean(st["name"]), "category": cat["key"]}


def _reading(fn, *args):
    """A planning read: Jira's errors become fixed texts (an HTTP status at most, never server text)."""
    try:
        return fn(*args)
    except (issues.ReadError, attachments.Refused):
        raise
    except JiraError as e:
        raise ValueError("%s while reading; nothing written" % why(e)) from None
    except ClientError:
        raise ValueError("the request to Jira did not complete while reading; nothing written") from None


def plan(c, cfg, root, key, loc):
    """The close plan (reads only). Not ready: no request (`c` may be None)."""
    chk = loc["check"]  # of loc["ledger"], whose bytes are compared with the uploaded ones below
    p = {"key": key, "ready": chk["ready"], "check": chk}
    if not chk["ready"]:
        return p
    st = _status(_reading(issues.get, c, key, ["status"]))
    choice = refusal = None
    if st["category"] != "done":
        try:
            listed = _reading(issues.transitions, c, key)
            choice = transitions.choose_done(listed, st["id"], cfg.transitions)  # a configured name must close the story
        except transitions.TransitionRefused as e:
            refusal = {"code": e.code, "candidates": list(e.candidates)}
    paths = ["dod/" + keys.dod_name(key)] + (["dod/Decisions_%s.md" % key] if loc["decisions"] else [])
    att = _reading(attachments.plan_attachments, c, root, key, paths)
    if att.items[0].data != loc["ledger"].encode("utf-8"):  # P3 read the file again for the upload: it must be the checked one
        raise ValueError(E_CHANGED)
    p.update(status=st, choice=choice, refusal=refusal, att=att)
    return p


def _writes(p):
    return [it for it in p["att"].items if it.action == "upload"], p["choice"]


def render(p):
    """The canonical plan text: deterministic, no people, no secrets; server text only as cleaned, escaped names."""
    chk = p["check"]
    lines = ["Plan for closing %s (skj done)" % p["key"], ""]
    if not p["ready"]:
        shown = chk["unticked"][:MAX_SHOWN]
        lines += ["Not ready: no writes will happen, even with --apply. Ledger section 5 is not fully ticked:"]
        lines += ["- " + (_cell(u)[:MAX_ITEM] or "(a box with no text)") for u in shown]
        if len(chk["unticked"]) > MAX_SHOWN:
            lines.append("- ... and %d more" % (len(chk["unticked"]) - MAX_SHOWN))
        lines += ["", "Tick a box only when its evidence exists (a passing run, the gate, a resolved question)."]
        return "\n".join(lines) + "\n"
    st = p["status"]
    lines.append("ledger section 5: complete (%d of %d boxes ticked)" % (chk["boxes"], chk["boxes"]))
    lines.append("status: %s (category %s)" % (_cell(st["name"]) or "?", st["category"]))
    if st["category"] == "done":
        lines.append("transition: none: already done (status category done); the status is not changed")
    elif p["refusal"]:
        r = p["refusal"]
        lines.append("transition: refused: %s" % r["code"])
        lines.append("refused %s: %s" % (r["code"], transitions.MESSAGES[r["code"]]))
        if r["candidates"]:
            lines.append("candidates: " + ", ".join(_cell(n) for n in r["candidates"]))
        lines.append(HINT)
    else:
        ch = p["choice"]
        lines.append("transition: %s %s (to %s)" % (ch["id"], _cell(ch["name"]), _cell(ch["to_name"])))
    lines += ["", "attachments:"] + ["- " + line for line in p["att"].lines]
    lines.append("")
    uploads, choice = _writes(p)
    if p["refusal"]:
        lines.append("On --apply: nothing (the transition is refused).")
    elif not uploads and choice is None:
        lines.append("Nothing to do: already done and every file already attached.")
    else:
        steps = ["upload " + it.rel for it in uploads] + (["fire transition %s" % choice["id"]] if choice else [])
        lines.append("On --apply, in this order (the transition only when every attachment is in place):")
        lines += ["%d. %s" % (i, s) for i, s in enumerate(steps, 1)]
    return "\n".join(lines) + "\n"


def apply(c, p, clock=time.monotonic, sleep=time.sleep):
    """The plan's writes: {"lines", "ok"}. Attachments first; the transition only after all of them are certainly in place."""
    if not p["ready"] or p["refusal"]:
        return {"lines": ["nothing written: the plan refuses the close"], "ok": False}
    key, choice = p["key"], p["choice"]
    res = attachments.apply_attachments(c, p["att"], clock, sleep)  # <= 2 uploads + 1 transition: under push's pace window
    lines = list(res["lines"])
    if not res["ok"] or any(line.endswith(FOUND) for line in lines):
        if choice is not None:
            lines.append(NOT_TRANSITIONED)
        return {"lines": lines, "ok": False}
    if choice is None:
        return {"lines": lines + ["already done: the status is not changed"], "ok": True}
    try:
        issues.do_transition(c, key, choice["id"])
    except WriteUncertain:
        return {"lines": lines + [_recheck(c, key)], "ok": False}
    except JiraError as e:
        return {"lines": lines + ["failed: transition %s of %s: %s; not retried" % (choice["id"], key, why(e))], "ok": False}
    except ClientError:
        return {"lines": lines + ["failed: transition %s of %s: the request did not complete; not retried" % (choice["id"], key)],
                "ok": False}
    try:  # the report is the re-read status, not the POST's answer
        st = _status(issues.get(c, key, ["status"]))
    except ClientError:
        return {"lines": lines + ["the transition %s of %s was sent but the re-read failed; run done again to see" % (choice["id"], key)],
                "ok": False}
    if st["category"] != "done":
        return {"lines": lines + [E_SENT_NOT_DONE % (choice["id"], key)], "ok": False}
    return {"lines": lines + ["transitioned %s to %s (id %s)" % (key, _cell(choice["to_name"]), choice["id"])], "ok": True}


def _recheck(c, key):
    """After an uncertain transition: one re-read of the status category, never a second POST."""
    try:
        st = _status(issues.get(c, key, ["status"]))
    except ClientError:
        return "write uncertain: the transition of %s may have happened and the re-read failed; not retried (run done again)" % key
    if st["category"] == "done":
        return "transitioned %s %s" % (key, FOUND)
    return "write uncertain: %s is not done on re-read; the transition was not retried (run done again)" % key
