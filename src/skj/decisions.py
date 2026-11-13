"""The decisions file `dod/Decisions_<KEY>.md`: every assumption and decision of a story, one `D<n>` line each (local verbs only).

Port of the fork source's `decisions.py`: the `D<n>` grammar, `--add`, `--supersedes`, `--resolve`, `--post` (W5) and the `posted:` /
`used:` / `via:` markers. Marker ids are Jira comment id STRINGS (digits, no leading zero), never ints. `--post` (W5) and `--sync` (A2)
make requests (`--sync` GETs only: it writes this local file, never Jira). The file is a repository file the PR controls, so it is read
with a size cap, written through a temp file, and never followed through a symlink.
"""
import hashlib
import os
import re
import stat
import tempfile
import time
import unicodedata
from pathlib import Path

from . import client as _client, dod, keys, push
from .dod import _visible

KINDS = ("decision", "assumption", "override")
_OPEN = "_(open)_"
# `- [ ] D1: <text> → _(open)_ · kind: <k> · why: <reason>`; `- [x] D1: <text> → validated|rejected · by: <role> · note: <text>`
# (either also takes ` · posted: <comment id>:<hash8>` and ` · used: <date>`; a resolved one ` · via: <comment id>:<hash8>`, the approver's comment
# `--sync` took the verdict from and the first 8 hex of its `adf.digest` (A2); the id-only `via: <comment id>` of L1 still reads); lenient: spacing, `->`, `*`/`+` bullet.
# Linear on a blank run: the text after `D<n>` is taken whole and trimmed with str.strip; the status arrow and the `·` separators are
# found with str.find/re.finditer on the literal and checked by ONE anchored match at that position (never an unanchored `\s*X` scan,
# which backtracks quadratically over a long run of blanks).
_CAND = re.compile(r"^\s*(?:[-*+]\s+)?\[([ xX])\]\s*D(\d+)(.*)$")
_ARROW = re.compile("→|->")
_AFTER_ARROW = re.compile(r"\s*(_\(open\)_|validated|rejected)(?=\s*(?:·|$))")  # what a status arrow is followed by
_KEYS = ("kind", "why", "by", "note", "posted", "used", "via")
_KEYED = re.compile(r"\s*(?:" + "|".join(_KEYS) + r")\s*:")  # what a separating `·` is followed by
MAX_LINE = 2000  # a longer D line is malformed (defence in depth: the per-line work is linear, but 1 MiB of such lines is not worth reading)
_CID = r"[1-9][0-9]{0,11}"  # a Jira comment id: ASCII digits only, no leading zero (a string, never an int)
_POSTED = re.compile(r"(%s):([0-9a-f]{8})" % _CID)
_VIA = re.compile(_CID + r"(?::[0-9a-f]{8})?")  # A2 writes `<id>:<hash8>` (approvals.pin); an id alone (L1) is still read
_SUP = re.compile(r"supersedes D([0-9]{1,6}): ")  # the only place a supersedes relation is read from: the start of the text
_MAX_FILE = 1 << 20


def decisions_file(root, key):
    """`<root>/dod/Decisions_<KEY>.md`, beside the DoD ledger. ValueError for an invalid key; `dod.UnsafePath` (a ValueError) when
    dod/ or the file is a symbolic link or the wrong kind of file."""
    path = dod.ledger_path(root, key).with_name("Decisions_%s.md" % key)
    if os.path.islink(str(path)):
        raise dod.UnsafePath("dod/%s is a symbolic link; refused (the decisions file is never written through a link)" % path.name)
    if os.path.lexists(str(path)) and not path.is_file():
        raise dod.UnsafePath("dod/%s is not a regular file; refused" % path.name)
    return path


def render_file(key):
    """A new decisions file: a title and a format note. The note is a code span inside a paragraph, so it does not
    parse as a decision line (`parse_decisions` only reads lines that start with a checkbox)."""
    return (f"# Decisions — {key}\n\n"
            "Format, one line each: `- [ ] D1: <text> → _(open)_ · kind: decision · why: <reason>`; "
            "a human records the outcome.\n\n")


def line_hash(text):
    """First 8 hex of sha256 of the text with whitespace collapsed: an edited line no longer matches its `posted:` marker."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:8]


def marker_hash(d):
    """The hash a `posted:` marker carries for the parsed decision `d`: the text alone, but for an override or a superseder the
    id, the text and the reason (what the PO read): an edited reason or a marker copied from another line no longer matches."""
    return line_hash(f"{d['id']}|{d['text']}|{d['why']}" if d["kind"] == "override" or d.get("supersedes") else d["text"])


_STATES = ("open", "validated", "rejected")


def _answers(parsed):
    """(entries by id, {target: [superseder ids]} over the valid edges, a bad edge was seen, {id: stands}). An edge is bad when its
    target is unknown or an override, or its superseder id is not higher (that covers self-reference and cycles); a bad edge
    changes nothing here. A decision `stands` unless it is rejected and no decision that stands supersedes it: the head of a chain
    answers everything below it."""
    ents = {d["id"]: d for k in _STATES for d in parsed[k]}
    by, bad = {}, False
    for d in ents.values():
        t = d.get("supersedes")
        if t is None:
            continue
        if t not in ents or ents[t]["kind"] == "override" or int(t[1:]) >= int(d["id"][1:]):
            bad = True
        else:
            by.setdefault(t, []).append(d["id"])
    stands = {}
    for i in sorted(ents, key=lambda i: -int(i[1:])):  # a superseder has the higher id, so it is decided first
        stands[i] = ents[i]["status"] != "rejected" or any(stands[s] for s in by.get(i, ()))
    return ents, by, bad, stands


def supersede_problem(parsed):
    """True when a `supersedes D<n>: ` line is invalid: unknown target, an override as target, a superseder that is not
    higher than its target (self-reference, a cycle)."""
    return _answers(parsed)[2]


def _stale(d):
    return not d["posted"] or d["posted"]["hash"] != marker_hash(d)


def unposted(parsed):
    """The lines the PO has not seen as they now read (the fork's predicate): the open lines, then the override lines that are validated
    (a validated override still has to reach the thread), each without a `posted:` marker or with one that no longer matches
    `marker_hash`. An open line a superseder answers stays in: the PO must see it before it is retired. The marker is a local claim
    and it is trusted here (fork-faithful): a line whose `posted:` hash matches is left out WITHOUT checking Jira, so a deleted or
    never-posted comment is not posted again (strict's decision-open and A2's posting-missing must catch that). For a plain decision
    `marker_hash` covers the text only, so a changed why or kind is not posted again either (fork-faithful). Only for the lines that
    ARE posted does `--post` check Jira itself, so that a comment already there is not duplicated."""
    lines = parsed["open"] + [d for d in parsed["validated"] if d["kind"] == "override"]
    return [d for d in lines if _stale(d)]


def supersede_unposted(parsed):
    """The decisions in a supersedes relation the PO has not seen as they read, by id order: every line that supersedes another
    (whatever its status or kind) and every open line an effective superseder drops, each without a matching `posted:` marker."""
    kept = {d["id"] for d in effective(parsed)["open"]}
    out = [d for k in _STATES for d in parsed[k] if _stale(d) and (d.get("supersedes") or (k == "open" and d["id"] not in kept))]
    return sorted(out, key=lambda d: int(d["id"][1:]))


def masked_by(parsed, d_id):
    """The id of the decision that stands as superseder of `d_id` (so its own verdict is not counted now), else None."""
    _, by, _, stands = _answers(parsed)
    return next((s for s in by.get(d_id, ()) if stands[s]), None)


def effective(parsed):
    """`parsed` without the decisions a live superseder answers (a superseded line stays in the file, only the set the gates
    count changes). A superseder is live when it is not rejected, or when it is itself answered by a live one; a rejected
    superseder with nothing live above it re-activates its target. Invalid edges drop nothing."""
    _, by, _, stands = _answers(parsed)
    gone = {t for t, ss in by.items() if any(stands[s] for s in ss)}
    return {**{k: [d for d in parsed[k] if d["id"] not in gone] for k in _STATES}, "unparsed": parsed["unparsed"]}


def _split_keyed(tail):
    """`tail` cut at every `·` that is followed by a key and its colon (the pieces keep their blanks; every caller strips)."""
    cuts, j = [-1], tail.find("·")
    while j >= 0:
        if _KEYED.match(tail, j + 1):
            cuts.append(j)
        j = tail.find("·", j + 1)
    return [tail[i + 1:j] for i, j in zip(cuts, cuts[1:] + [len(tail)])]


def _line(raw):
    """One file line -> the decision dict, or None when it is a `D` line that breaks the grammar."""
    m = _CAND.match(raw)
    rest = m.group(3).lstrip() if m else ""
    if not rest.startswith(":") or not keys.line_number(m.group(2)):
        return None
    done = m.group(1) in "xX"
    rest = rest[1:].strip()
    for a in reversed(list(_ARROW.finditer(rest))):  # the LAST status arrow: a `→` inside the text must not split it
        s = _AFTER_ARROW.match(rest, a.end())
        if s:
            break
    else:
        return None
    if (s.group(1) == _OPEN) == done:
        return None
    text, tail = rest[:a.start()].strip(), rest[s.end():]
    first, *parts = _split_keyed(tail)
    kv = {}
    for p in parts:
        k, _, v = p.partition(":")
        if k.strip() in kv:
            return None
        kv[k.strip()] = v.strip()
    kind = kv.get("kind", "decision")
    posted = _POSTED.fullmatch(kv["posted"]) if "posted" in kv else None
    bad = (first.strip() or not text or kind not in KINDS or ("posted" in kv and not posted)
           or (not done and not (kv.get("kind") and kv.get("why")))
           or (done and not kv.get("by")) or (not done and ("by" in kv or "note" in kv or "via" in kv))
           or ("via" in kv and not _VIA.fullmatch(kv["via"])))
    if bad:
        return None
    sup = _SUP.match(text)
    return {"id": f"D{m.group(2)}", "kind": kind, "text": text, "why": kv.get("why", ""),
            "status": "open" if not done else s.group(1), "by": kv.get("by", ""), "note": kv.get("note", ""),
            "posted": {"comment_id": posted.group(1), "hash": posted.group(2)} if posted else None,
            "used": kv.get("used", ""), **({"supersedes": f"D{sup.group(1)}"} if sup else {}), **({"via": kv["via"]} if "via" in kv else {})}


def has_decision_lines(md):
    """True when the decisions file has a `D<n>` checkbox line outside fences and comments, well-formed or not."""
    return any(_CAND.match(ln) for ln in _visible(md)[0].splitlines())


def parse_decisions(md):
    """The decisions file -> {'open': [...], 'validated': [...], 'rejected': [...], 'unparsed': bool}. Lines inside code
    fences or <!-- --> comments are ignored; the first line of a duplicate id wins. `unparsed` is true when a `D` line
    breaks the grammar (it is left out) or a fence or comment is left open. A text that starts with `supersedes D<n>: `
    adds `supersedes: "D<n>"` to its entry (no other entry has the key). Empty text -> all empty."""
    out = {"open": [], "validated": [], "rejected": []}
    body, out["unparsed"] = _visible(md)
    seen = set()
    for raw in body.splitlines():
        if not _CAND.match(raw):  # linear: only the `- [ ] D<n>` prefix is matched here
            continue
        d = _line(raw) if len(raw) <= MAX_LINE else None
        if d is None:
            out["unparsed"] = True
        elif d["id"] not in seen:
            seen.add(d["id"])
            out[d["status"]].append(d)
    return out


def render_line(d):
    """The exact file line for a decision; `parse_decisions` reads it back unchanged."""
    if d["status"] == "open":
        parts = [f"- [ ] {d['id']}: {d['text']} → {_OPEN}", f"kind: {d['kind']}", f"why: {d['why']}"]
    else:
        parts = [f"- [x] {d['id']}: {d['text']} → {d['status']}", f"by: {d['by']}", f"note: {d['note']}".rstrip()]
        if d.get("via"):
            parts.append(f"via: {d['via']}")
        if d["kind"] != "decision":  # resolving must not lose that a line was an override or assumption
            parts.append(f"kind: {d['kind']}")
        if d["why"]:
            parts.append(f"why: {d['why']}")
    if d["posted"]:
        parts.append(f"posted: {d['posted']['comment_id']}:{d['posted']['hash']}")
    if d["used"]:
        parts.append(f"used: {d['used']}")
    return " · ".join(parts).rstrip()


# ---- scrubbing: what `--add` writes is later posted as a comment, so addresses and mentions never enter the file ----
_AT = re.compile(r"(?<![\w@])@\w[\w.-]*")  # a typed `@name`
_MENTION = re.compile(r"\[~[^\]\s]{1,200}\]")  # a Jira wiki mention `[~accountid:...]`: the accountId must not travel
_CTRL = re.compile("[\x00-\x1f\x7f-\x9f  ]")
# plain-text addresses; shortcut: ASCII local part/host only, widen if IDN addresses matter
_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_LOCAL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+'-")  # the local-part class of _EMAIL


def _scrub_emails(s):
    """`_EMAIL.sub("[email]", s)` in linear time: `_EMAIL` itself is quadratic on a long run of address characters, so it is matched once per
    `@`, anchored where the local part starts (at most 64 characters back) and ending at most 255 after.
    shortcut: an address longer than the RFC limits is cut there and its first characters stay; widen if that matters."""
    out, i = [], 0
    for at in re.finditer("@", s):
        p = at.start()
        if p < i or p == 0 or s[p - 1] not in _LOCAL or s.find(".", p, p + 256) < 0:
            continue  # cheap rejects: no address character before, no dot in the host window
        lo = p
        while lo > i and p - lo < 64 and s[lo - 1] in _LOCAL:
            lo -= 1
        m = _EMAIL.match(s, lo, min(len(s), p + 256))
        if m and m.end() > p:
            out.append(s[i:lo])
            out.append("[email]")
            i = m.end()
    out.append(s[i:])
    return "".join(out)


E_FORMAT_WHY = "such text can read differently from what it says (Trojan Source); retype it without them"


def has_format(s):
    """True when `s` holds a Unicode format character (category Cf: bidi overrides and isolates, zero-width characters)."""
    return any(unicodedata.category(ch) == "Cf" for ch in s)


def _scrub(text):
    """Wiki mentions and typed `@name` -> `@user`, addresses -> `[email]`. An address or @mention typed in a decision is replaced,
    never refused. Free-text names cannot be detected."""
    return _AT.sub("@user", _scrub_emails(_MENTION.sub("@user", text)))


_BY = re.compile(r"[A-Za-z0-9 ]{1,40}")
MAX_NOTE = 300
_MARKUP = re.compile(r"<!--|-->|`{3}|~{3}")  # would open or close a comment or fence in the decisions file


def _ids(parsed):
    return {d["id"]: d["status"] for k in _STATES for d in parsed[k]}


def _read(path):
    """The file's text, newlines untouched; ValueError over 1 MiB (bytes) (OSError/UnicodeError as the open and decode raise them)."""
    with path.open("rb") as f:
        raw = f.read(_MAX_FILE + 1)
    if len(raw) > _MAX_FILE:
        raise ValueError("the decisions file is larger than 1 MiB; nothing written")
    return raw.decode("utf-8")  # bytes keep every newline as it is, as newline="" did


def _atomic_write(path, text):
    """Write `text` over `path` through a uniquely named temp file in the same directory (mode kept, 0644 for a new
    file), then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def resolve(md, d_id, status, by, note, force=False, via=None):
    """The file text with the first `d_id` line rewritten as validated|rejected; its kind, why, posted and used
    markers and its line ending are kept, every other byte is unchanged. `via` (a comment id string, digits only, optionally `:<hash8>`;
    `--sync` passes the approver's comment pin) is written as ` · via: <via>`; without it an old `via` is dropped, since the new verdict no longer comes from that comment. A line that is already resolved needs `force`.
    ValueError for a bad status, role or note, an unknown id, a line holding an inline comment, or a result that
    `parse_decisions` would not read back the same over the whole file."""
    by, note = by.strip(), note.strip()
    if via is not None and not (isinstance(via, str) and _VIA.fullmatch(via)):
        raise ValueError("via must be a comment id (digits only), optionally with :<hash8>")
    if status not in ("validated", "rejected"):
        raise ValueError("the status must be validated or rejected")
    if not _BY.fullmatch(by):
        raise keys.UsageError("--by must be 1-40 letters, digits or spaces (a role, never a name)")
    if len(note) > MAX_NOTE or _CTRL.search(note) or _MARKUP.search(note):
        raise keys.UsageError(f"--note must be at most {MAX_NOTE} characters with no control characters, line breaks, "
                         "`<!--`, `-->` or code fences")
    out, found = [], False
    for raw, v in zip(md.splitlines(keepends=True), _visible(md)[0].split("\n")):
        d = _line(v) if not found and _CAND.match(v) and len(v) <= MAX_LINE else None
        if d and d["id"] == d_id:
            found = True
            if v != raw.splitlines()[0]:
                raise ValueError(f"{d_id} holds an inline <!-- --> comment or fence that a rewrite would drop; edit it by hand")
            if d["status"] != "open" and not force:
                raise ValueError(f"{d_id} is already {d['status']} (by {d['by']}); pass --force to overwrite that verdict")
            new = {**{k: v for k, v in d.items() if k != "via"}, "status": status, "by": by, "note": note, **({"via": via} if via else {})}
            line = render_line(new)
            if len(line) > MAX_LINE or _line(line) != new:  # over-long, or the note collides with the grammar
                raise ValueError(f"the resolved line would be longer than {MAX_LINE} characters or would not read back "
                                 "(a note must not contain ` · kind:`-style markers or `→ validated`)")
            raw = line + raw[len(raw.splitlines()[0]):]  # splitlines also ends a line at U+2028, \x0c, \x85, \x1c
        out.append(raw)
    if not found:
        raise ValueError(f"no decision {d_id} in the decisions file (see `skj decisions <KEY>`)")
    new_md = "".join(out)
    a, b = md.splitlines(keepends=True), new_md.splitlines(keepends=True)
    before, after = parse_decisions(md), parse_decisions(new_md)
    if (len(a) != len(b) or sum(x != y for x, y in zip(a, b)) != 1 or after["unparsed"] != before["unparsed"]
            or _ids(after) != {**_ids(before), d_id: status}):
        raise ValueError(f"the rewrite of {d_id} would change more than that line or break the file; nothing written")
    return new_md


def apply_resolve(file_path, d_id, status, by, note, force=False):
    """Rewrite one line of the decisions file in place (unique temp file + os.replace). Refuses when the file changed
    between the read and the write."""
    path = Path(file_path)
    try:
        md = _read(path)
        new = resolve(md, d_id, status, by, note, force)
        if _read(path) != md:
            raise ValueError("the decisions file changed while resolving; nothing written, run it again")
        _atomic_write(path, new)
    except (OSError, UnicodeError) as e:
        raise ValueError(f"cannot update the decisions file {path} ({e.__class__.__name__}: {e}); nothing written")


MAX_TEXT = 1500
_ADD_KINDS = ("decision", "assumption")  # an `override` line is written by a human by hand (0.1 has no override command)


def _next_number(digits):
    """max(int(n) for n in digits, default 0) + 1 as a string, without int(): its cost is quadratic in the digits (a huge id in a
    file the PR controls). The ids are normalised to ASCII digits without leading zeros and compared by (length, text)."""
    top = ""
    for n in digits:
        n = (n if n.isascii() else "".join(str(unicodedata.decimal(c)) for c in n)).lstrip("0")
        top = max(top, n, key=lambda x: (len(x), x))
    head = top.rstrip("9")
    return "1" + "0" * len(top) if not head else head[:-1] + str(int(head[-1]) + 1) + "0" * (len(top) - len(head))


def _supersedes_target(md, target):
    """Refuse (ValueError, fixed texts) unless `target` is a decision of `md` the developer may answer: it exists, is not an
    override, is rejected or open (not validated), and no live decision answers it yet."""
    if not re.fullmatch(r"D[0-9]{1,6}", target if isinstance(target, str) else ""):
        raise keys.UsageError("--supersedes must be D<n>: the letter D and one to six digits")
    ents, by, _, stands = _answers(parse_decisions(md))
    if target not in ents:
        raise ValueError("--supersedes names a decision that is not in the decisions file")
    if ents[target]["kind"] == "override":
        raise ValueError("--supersedes cannot answer an override")
    if ents[target]["status"] == "validated":
        raise ValueError("--supersedes can only answer a rejected or open decision, not a validated one")
    if ents[target]["status"] == "open" and _stale(ents[target]):
        raise ValueError(f"post {target} first (`skj decisions <KEY> --post`): the PO has not seen it, so it cannot be superseded")
    live = [s for s in by.get(target, ()) if stands[s]]
    if live:
        raise ValueError(f"{target} is already superseded by {live[0]}: supersede {live[0]} instead, or have the PO reject it first")


def add(md, kind, text, why="", _kinds=_ADD_KINDS, _min_why=0, supersedes=None):
    """(the file text with one new open `D<n>` line appended, "D<n>"). n = highest id in the file + 1 (a malformed or
    duplicate line still counts), grammar-exact. @mentions and addresses are replaced as in a posted comment.
    ValueError (never echoing the text) for a bad kind, a text of 0 or over MAX_TEXT characters, a why over MAX_NOTE,
    control characters, line breaks, `<!--`, `-->`, code fences, or a line that would not read back (so a file with an
    unclosed fence or comment, which would hide the new line, is refused too). `supersedes` ("D<n>"): the line's text then
    starts with `supersedes D<n>: ` (see `_supersedes_target` for what is refused); the prefix cannot be typed by hand."""
    if kind not in _kinds:
        raise keys.UsageError("--kind must be decision or assumption")
    for name, v in (("the text", text), ("--why", why)):  # before strip(), which would drop a trailing line break
        if _CTRL.search(v) or _MARKUP.search(v):
            raise keys.UsageError(f"{name} must not contain control characters, line breaks, `<!--`, `-->` or code fences")
        if has_format(v):
            raise keys.UsageError(f"{name} holds a bidi control or an invisible (format) character; nothing written: {E_FORMAT_WHY}")
    text, why = text.strip(), why.strip()
    if kind != "override":  # an override reason is refused for markup, never rewritten
        text, why = _scrub(text), _scrub(why)
    why = why or "unspecified"
    if not 1 <= len(text) <= MAX_TEXT:
        raise keys.UsageError(f"the text must be 1 to {MAX_TEXT} characters")
    if not _min_why <= len(why) <= MAX_NOTE:
        raise keys.UsageError(f"--why must be {f'{_min_why} to ' if _min_why else 'at most '}{MAX_NOTE} characters")
    if _SUP.match(text):
        raise keys.UsageError("the text must not start with `supersedes D<n>: `; use --supersedes D<n>")
    if supersedes is not None:
        _supersedes_target(md, supersedes)
        text = f"supersedes {supersedes}: {text}"
    d_id = "D" + _next_number(re.findall(r"(?m)^[ \t]*(?:<!--[ \t]*)?(?:[-*+][ \t]+)?\[[ xX]\][ \t]*D(\d+)", md))  # raw text: an id inside a comment or fence is taken too
    new = {"id": d_id, "kind": kind, "text": text, "why": why, "status": "open", "by": "", "note": "",
           "posted": None, "used": "", **({"supersedes": supersedes} if supersedes is not None else {})}
    line = render_line(new)
    eol = "\r\n" if "\r\n" in md else "\n"
    new_md = md + ("" if not md or md.endswith(("\n", "\r")) else eol) + line + eol
    before, after = parse_decisions(md), parse_decisions(new_md)
    if len(line) > MAX_LINE or _line(line) != new or _ids(after) != {**_ids(before), d_id: "open"} \
            or after["unparsed"] != before["unparsed"]:
        raise ValueError("the line would not read back from the decisions file (a text or --why holding ` · kind:`-style "
                         "markers or `→ validated`, or an unclosed code fence or comment in the file); nothing written")
    return new_md, d_id


def _apply(file_path, key, build):
    # shortcut: two concurrent writers can lose a line (no lock); fine for a local human/agent command, add a lock if parallel agents write
    """Write the text `build(md) -> (new md, id)` makes, starting from `render_file` when the file is missing (unique
    temp file + os.replace). Refuses when the file changed or appeared between the read and the write, and when the file
    on disk does not read back as exactly the new text (the old text is then put back). Returns the id."""
    path = Path(file_path)
    try:
        exists = path.exists()
        md = _read(path) if exists else render_file(key)
        new, d_id = build(md)
        if path.exists() != exists or (exists and _read(path) != md):
            raise ValueError("the decisions file changed while adding; nothing written, run it again")
        _atomic_write(path, new)
        try:
            back = _read(path)
        except (OSError, UnicodeError, ValueError):  # unreadable after the write: put the old text back as for a mismatch
            back = None
        if back != new:
            if exists:
                _atomic_write(path, md)
            raise ValueError("the decisions file did not read back as written; nothing recorded")
    except (OSError, UnicodeError) as e:
        raise ValueError(f"cannot update the decisions file {path} ({e.__class__.__name__}: {e}); nothing written")
    return d_id


def apply_add(file_path, key, kind, text, why="", supersedes=None):
    return _apply(file_path, key, lambda md: add(md, kind, text, why, supersedes=supersedes))


_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def stamp_used(md, ids, date):
    """The file text with ` · used: <date>` appended to the lines of the decisions in `ids`, once: a line that already
    has `used` keeps its date. Pure: the caller writes it (`_atomic_write`). Every other byte, line endings included,
    is unchanged. ValueError (nothing returned) for a bad date, an id that is not a line of the file, or a line holding
    an inline comment or fence that a rewrite would drop."""
    if not isinstance(date, str) or not _DATE.fullmatch(date):
        raise ValueError("the date must be YYYY-MM-DD")
    out, todo = [], set(ids)
    for raw, v in zip(md.splitlines(keepends=True), _visible(md)[0].split("\n")):
        text = raw.splitlines()[0]  # the terminator may be U+2028 etc.: keep it as it is
        d = _line(v) if _CAND.match(v) and len(v) <= MAX_LINE else None
        if d and d["id"] in todo:
            todo.discard(d["id"])
            if v != text:
                raise ValueError(f"{d['id']} holds an inline <!-- --> comment or fence that a rewrite would drop; edit it by hand")
            if not d["used"]:
                raw = f"{text} · used: {date}{raw[len(text):]}"
        out.append(raw)
    if todo:
        raise ValueError(f"no such decision in the decisions file: {', '.join(sorted(todo))}")
    return "".join(out)


def apply_stamp(file_path, key, ids):
    """Stamp `used: <today>` on the lines in `ids` (an apply path that relied on them, just before it sends). Same write as
    `apply_add`: temp file + os.replace, whole-file read-back, ValueError (nothing written) on any failure."""
    from datetime import date
    return _apply(file_path, key, lambda md: (stamp_used(md, ids, date.today().isoformat()), None))


# ---- `skj decisions <ref> --post` (W5): plan (reads only), then one POST behind the X2 gate, then the `posted:` stamps ----
# The comment is W4's (questions._compose): a marker paragraph `[skj:decisions <hash8>]` (hash8 = sha256 of the canonical Markdown after
# it), the fork's heading, and one plain-text bullet per unposted line `D<n> (<kind>): <text> — why: <why>` (the fork's comment line: no
# status, so A2 can match a line that was resolved after posting). Append-only: a changed line goes out in a NEW comment; the line's one
# `posted:` marker (the grammar holds one) then names the newer comment, the older comment stays on Jira untouched.
HEAD = "Decisions and assumptions made during development; please validate or reject:"
MAX_COMMENT_LINE = 500
MAX_POST = 32 << 10  # characters of the raw lines before the scrub, and bytes of the ADF JSON (questions.MAX_POST); Jira's is UNVERIFIED
_MARKER_RE = re.compile(r"\[skj:decisions ([0-9a-f]{8})\]\Z")  # the WHOLE first line of a comment's canonical Markdown
_CID_RE = re.compile(_CID + r"\Z")
E_SIZE = "the decisions make a comment larger than the 32 KiB local cap; nothing posted (shorten some)"
E_UNPARSED = ("a decision line breaks the grammar, or the decisions file has an unclosed code fence or comment; nothing posted "
              "(fix the file first: a hidden line would never reach the PO)")
E_TOO_MANY = ("more than 20 own comments on the story cannot be read by the kit; nothing posted (one of them could be its "
              "decisions comment: posting again could duplicate it)")
E_UNREADABLE = ("an own comment the kit cannot read (%s) may carry a decisions marker or could not be checked; nothing posted "
                "(a second one could be a duplicate): run the command again, and if it persists remove or fix that comment in Jira")
E_STAMP_SIZE = "the file would exceed 1 MiB with the posted: stamps; nothing stamped"
def _q():
    from . import questions  # here, not at the top: questions imports this module (W4's helpers are reused, not copied)
    return questions


def _raw_line(d):
    return f"{d['id']} ({d['kind']}): {d['text']}" + (f" — why: {d['why']}" if d["why"] else "")


def _comment_line(d):
    """(the comment line of `d` made inert by W4's `_safe` on the WHOLE line, then capped at MAX_COMMENT_LINE with …, was it cut).
    Scrub first: a cut through a mention or an address would leave a fragment the scrub cannot see."""
    text = _q()._safe(_raw_line(d))
    return (text[:MAX_COMMENT_LINE - 1] + "…", True) if len(text) > MAX_COMMENT_LINE else (text, False)


def cut(lines):
    """Ids of the decisions whose comment line is cut to MAX_COMMENT_LINE."""
    return [d["id"] for d in lines if _comment_line(d)[1]]


def body(lines):
    """(Markdown, ADF doc, hash8) of the comment for the decisions `lines` (`unposted`): the marker, HEAD, one item each. ValueError (fixed
    text) when empty, over MAX_POST (checked on the raw lines first, so the scrub's cost is bounded), or when the ADF would not read back
    exactly as the previewed Markdown in the exact layout (W4's `_compose`)."""
    if not lines:
        raise ValueError("no unposted decisions; nothing to post")
    if sum(len(_raw_line(d)) for d in lines) > MAX_POST:
        raise ValueError(E_SIZE)
    return _q()._compose("decisions", [(HEAD, [_comment_line(d)[0] for d in lines])], E_SIZE)


def stamp_posted(md, hashes, cid):
    """The file text with ` · posted: <cid>:<hash8>` on the first line of each id in `hashes` ({id: marker_hash}); a stale marker on that
    line is replaced (the grammar holds one), marker-like words in the text stay text. Each line is re-rendered (`render_line`) and must
    read back as exactly the planned decision plus its marker; every other byte, line endings included, is unchanged. ValueError (nothing
    returned) for an id that is not a string comment id, a planned line that is missing or whose hash changed since the plan, a line
    holding an inline comment or fence, or a result that would not parse back the same."""
    if not (isinstance(cid, str) and _CID_RE.match(cid)):
        raise ValueError("not a comment id string; nothing stamped")
    out, todo = [], dict(hashes)
    for raw, v in zip(md.splitlines(keepends=True), _visible(md)[0].split("\n")):
        text = raw.splitlines()[0]  # the terminator may be U+2028 etc.: keep it as it is
        d = _line(v) if todo and _CAND.match(v) and len(v) <= MAX_LINE else None
        if d and d["id"] in todo:
            if marker_hash(d) != todo.pop(d["id"]):
                raise ValueError(f"the decisions file changed since the plan ({d['id']}); nothing stamped")
            if v != text:
                raise ValueError(f"{d['id']} holds an inline <!-- --> comment or fence that a rewrite would drop; edit it by hand")
            new = {**d, "posted": {"comment_id": cid, "hash": marker_hash(d)}}
            line = render_line(new)
            if len(line) > MAX_LINE or _line(line) != new:
                raise ValueError(f"{d['id']} would not read back with its posted: marker; nothing stamped")
            raw = line + raw[len(text):]
        out.append(raw)
    if todo:
        raise ValueError(f"the decisions file changed since the plan ({', '.join(sorted(todo))}); nothing stamped")
    new_md = "".join(out)
    before, after = parse_decisions(md), parse_decisions(new_md)
    want = {k: [({**d, "posted": {"comment_id": cid, "hash": hashes[d["id"]]}} if d["id"] in hashes else d) for d in before[k]]
            for k in _STATES}
    if ({k: after[k] for k in _STATES} != want or after["unparsed"] != before["unparsed"]
            or len(new_md.splitlines()) != len(md.splitlines())):
        raise ValueError("the posted: stamps would not read back from the decisions file; nothing stamped")
    if len(new_md.encode("utf-8", "surrogatepass")) > _MAX_FILE:  # the kit could not read the file back
        raise ValueError(E_STAMP_SIZE)
    return new_md


def plan_post(c, cfg, key, parsed, missing=False):
    """Reads only: {"key", "action": none|post|noop, "md", "doc", "hash", "found", "hashes", "cut", "report", "me", "missing"}. Nothing
    unposted (or `missing`: no file, `--if-any`): `none`, no request (`c` may be None). An unparsed file refuses first. Otherwise W4's
    reads (`questions._own_comment`: the issue and its project, the identity, every comment and the probe past the end, the inspection
    of own unreadable comments): `noop` when the kit's own comment already shows exactly this text, else `post`. `hashes` is what the
    lines get stamped with afterwards."""
    if not (keys.valid_key(key) and key.rsplit("-", 1)[0] == cfg.project):
        raise ValueError("not an issue key of project %s" % cfg.project)
    if parsed["unparsed"]:
        raise ValueError(E_UNPARSED)
    todo = unposted(parsed)
    if any(has_format(d["text"] + d["why"]) for d in todo):
        raise ValueError("a decision to post holds a bidi control or an invisible (format) character; nothing posted: " + E_FORMAT_WHY)
    p = {"key": key, "action": "none", "md": "", "doc": None, "hash": None, "found": None, "cut": cut(todo), "me": None,
         "hashes": {d["id"]: marker_hash(d) for d in todo}, "report": _q()._report(), "missing": missing}
    if not todo:
        return p
    p["md"], p["doc"], p["hash"] = body(todo)
    p["me"], p["found"], p["report"] = _q()._own_comment(c, cfg, key, p["md"], _MARKER_RE, "[skj:decisions", E_TOO_MANY, E_UNREADABLE)
    p["action"] = "noop" if p["found"] else "post"
    return p


_REPORTS = (("foreign", "marker comment(s) by another account ignored (a marker never counts unless the kit's own account wrote it)"),
            ("restricted", "own marker comment(s) with a visibility restriction ignored"),
            ("mismatch", "own marker comment(s) whose body no longer matches the marker (edited) ignored"),
            ("unreadable", "own comment(s) the kit cannot read (no decisions marker) ignored"))


def render_plan(p):
    """The canonical plan text: deterministic, no people, no account ids; the body is the exact ADF-as-Markdown that is sent."""
    lines = ["Plan for %s: post the unposted decisions as one comment" % p["key"]]
    if p["action"] == "none":
        why = "no decisions file" if p["missing"] else "no unposted decisions"
        return "\n".join(lines + ["action: none (%s; nothing to post)" % why]) + "\n"
    ids = ", ".join(p["hashes"])
    if p["action"] == "post":
        lines += ["action: post ONE new comment [skj:decisions %s] (an earlier comment is never edited or deleted)" % p["hash"],
                  "decisions file: then stamp %s with ` · posted: <new comment id>:<hash>`" % ids]
    else:
        lines += ["action: none (comment %s already shows these decisions)" % p["found"],
                  "decisions file: stamp %s with ` · posted: %s:<hash>`" % (ids, p["found"])]
    if p["cut"]:
        lines.append("note: %s cut to %d characters (…)" % (", ".join(p["cut"]), MAX_COMMENT_LINE))
    for k, what in _REPORTS:
        if p["report"][k]:
            lines.append("note: %d %s: %s" % (len(p["report"][k]), what, ", ".join(p["report"][k])))
    if p["action"] == "post":
        lines += ["", "Comment body (ADF as Markdown, exactly what is sent):", ""]
        lines += ["> " + x if x else ">" for x in p["md"].split("\n")]
    return "\n".join(lines) + "\n"


def apply_post(c, p, root, clock=time.monotonic, sleep=time.sleep):
    """The plan's one write, then the stamps: {"lines", "ok"}. The planned lines are checked against the file first (a changed line
    posts nothing, as in the fork). POST /issue/{key}/comment with {"body": <adf>} only (no visibility, no properties), paced (push's
    pacer), never retried. A timeout, a 5xx, a 2xx/3xx it cannot read or a lost connection is uncertain: one fresh re-read decides what
    is printed, nothing is posted again, and `ok` is False. A 4xx: not posted (status only). The lines are stamped after a post, a
    found-on-re-read, or a noop (temp file + os.replace, path re-checked: no symlink or FIFO, 1 MiB cap, read back)."""
    lines, ok, cid = [], True, p["found"]
    if p["action"] == "none":
        return {"lines": lines, "ok": ok}
    q = _q()
    try:
        path = decisions_file(root, p["key"])
        stamp_posted(_read(path), p["hashes"], "9" * 12)  # refuses before any write, as the fork does (the longest id: a size bound)
    except (OSError, UnicodeError, ValueError) as e:
        why = str(e) if isinstance(e, ValueError) else "cannot read the decisions file (%s)" % e.__class__.__name__
        return {"lines": ["failed: %s; nothing posted" % why.split("; nothing")[0]], "ok": False}
    if p["action"] == "post":
        push._Pacer(clock, sleep).wait()
        try:
            d = c.request("POST", "/issue/%s/comment" % p["key"], body={"body": p["doc"]}).data
            cid = d.get("id") if isinstance(d, dict) else None
            if not (isinstance(cid, str) and _CID_RE.match(cid)):
                raise _client.WriteUncertain("POST", "a malformed answer from Jira")
            lines.append("posted comment %s [skj:decisions %s]" % (cid, p["hash"]))
        except _client.JiraError as e:
            if type(e.status) is int and 400 <= e.status < 500:
                return {"lines": ["failed: %s; nothing posted" % _client.why(e)], "ok": False}
            ok = False
            cid, line = q._recheck(c, p, _MARKER_RE)
            lines.append(line)
        except _client.ClientError:  # WriteUncertain and any other failure after the request may have left
            ok = False
            cid, line = q._recheck(c, p, _MARKER_RE)
            lines.append(line)
    elif p["action"] == "noop":
        lines.append("nothing posted: comment %s already shows these decisions" % cid)
    if cid is None:
        return {"lines": lines, "ok": ok}
    try:
        _apply(decisions_file(root, p["key"]), p["key"], lambda m: (stamp_posted(m, p["hashes"], cid), None))
        lines.append("decisions file: stamped %s with posted: %s" % (", ".join(p["hashes"]), cid))
    except ValueError as e:
        pairs = "; ".join("%s: · posted: %s:%s" % (i, cid, h) for i, h in p["hashes"].items())
        lines.append("decisions file: not stamped (%s); append to the lines by hand: %s" % (e, pairs))
        ok = False
    return {"lines": lines, "ok": ok}



# ---- `skj decisions <ref> --sync` (A2): an approver's `/approve D<n>` or `/reject D<n>` comment becomes the verdict (a LOCAL write) ----
# EXPERIMENTAL, tamper-evident only (owner decision O5/K5). The ONE newest-posting rule (`posting_index`, `superseded`, `_posting_rule`):
# a verdict on D<n> counts only when the line's `posted:` marker names the token user's own `[skj:decisions <hash8>]` comment (W5), that
# comment is UNEDITED and unrestricted, shows the D<n> line exactly as the file holds it now, is the NEWEST own posting that lists D<n>
# (no own unreadable comment newer than it), and the approval is newer than it (parsed instants, then the numeric id; a tie loses).
SYNC_RULES = ("not-posted", "edited", "posting-lossy", "posting-missing", "posting-mismatch", "posting-markup", "posting-edited",
              "posting-restricted", "posting-superseded", "resolved-locally", "local-override", "no-verdict", "older-than-posting",
              "newer-tampered", "bad-comment-id", "up-to-date", "unwritable")  # why a line was left alone; fixed names, never Jira text
SYNC_BY = "approver"  # the role written on a synced line (never a name)
_VERDICT = {"approve": "validated", "reject": "rejected"}
POSTED_LINE = re.compile(r"(D[1-9][0-9]{0,8}) \((?:decision|assumption|override)\): ")  # a decision item of a posting: the id, ASCII digits
_SYNC_ID = re.compile(r"D[0-9]{1,9}")
E_SYNC_MALFORMED = ("the decisions file is malformed (a line that breaks the grammar, an unclosed fence or comment, or a repeated or "
                    "non-ASCII id); nothing written")
E_SYNC_BLOCKED = ("an approver's newer verdict cannot be written to the decisions file for %s, so the earlier verdict would stand; fix the "
                  "line and run it again; nothing written")


def comment_line_is_lossy(d):
    """Does the posted comment line differ from the file's words (cut at MAX_COMMENT_LINE, or `_safe` changed something)? Then a match
    between the file and the comment proves less than it seems: a changed tail or a swapped address would still match."""
    raw = _raw_line(d)
    return _comment_line(d)[1] or _q()._safe(raw) != raw


def _posted_items(rest):
    """[(D id, "", item text)] of a decisions posting's Markdown after the marker, or None unless it is exactly the W5 layout: HEAD, then
    one bullet list (`- ` items). Items that are no decision line are skipped."""
    parts = rest.split("\n\n")
    if len(parts) != 2 or parts[0] != HEAD:
        return None
    out = []
    for ln in parts[1].split("\n"):
        if not ln.startswith("- "):
            return None
        m = POSTED_LINE.match(ln, 2)
        if m:
            out.append((m.group(1), "", ln[2:]))
    return out


def posting_index(rows, me):
    """The kit's own decisions postings (`approvals.postings`, one pass): the ONE reading of "newest posting" that sync (A2), and the
    wait gate (A4) and the done-time cross-check (A3), share."""
    from . import approvals
    return approvals.postings(rows, me, _MARKER_RE, "[skj:decisions", _posted_items)


def superseded(d, index):
    """Does a posted line `d` (its `posted:` marker is the claim; a line without one is not checked) point at anything but the NEWEST own
    posting of its id, or differ from the line that posting shows, or is an own unreadable comment newer than that posting? Then an
    approval of the newest posting is not an approval of this text."""
    best = index.best.get(d["id"])
    if not d["posted"] or best is None:
        return False
    return (d["posted"]["comment_id"] != best[1].id or ("", _comment_line(d)[0]) not in best[2]
            or (index.opaque is not None and index.opaque > best[0]))


def _posting_rule(d, by_id, me, index):
    """(rule, the posting's order key): why the verdict on `d` cannot be read yet, else (None, key). The `posted:` marker is a claim in a
    PR-controlled file: the comment it names must exist with a readable time, be the token user's own `[skj:decisions]` comment in the
    W5 layout (`posting-mismatch`, `posting-markup`), show `d`'s line as it reads NOW (`posting-mismatch`), be unedited (`posting-edited`:
    the agent is the token user and can edit its own comment) and unrestricted, and be the newest own posting of `d` (`superseded`)."""
    from . import approvals
    if not d["posted"]:
        return "not-posted", None
    if d["posted"]["hash"] != marker_hash(d):
        return "edited", None
    if comment_line_is_lossy(d):
        return "posting-lossy", None
    r = by_id.get(d["posted"]["comment_id"])
    key = approvals._order(r) if r is not None else None
    if key is None:
        return "posting-missing", None
    if r.id not in index.items or r.author != me:
        return "posting-mismatch", None
    items = index.items[r.id]
    if items is None:
        return "posting-markup", None
    if (d["id"], "", _comment_line(d)[0]) not in items:
        return "posting-mismatch", None
    if not approvals.unedited(r):
        return "posting-edited", None
    if r.visibility is not None:
        return "posting-restricted", None
    return ("posting-superseded", None) if superseded(d, index) else (None, key)


def _sync_note(text):
    """A comment's note (already made inert by `approvals._note`) as a safe `note:` value: the grammar's own separators (`·`, `→`, `->`)
    turned into dashes, addresses and mentions replaced, control characters, `<!--`, `-->` and fences blanked, blanks collapsed, cut to
    MAX_NOTE. Never refused: the verdict must land with or without it."""
    t = _scrub(text.replace("·", "-").replace("→", "-").replace("->", "-"))
    return " ".join(_MARKUP.sub(" ", _CTRL.sub(" ", t)).split())[:MAX_NOTE]


def check_sync_file(md):
    """`parse_decisions(md)` for a sync, or ValueError (E_SYNC_MALFORMED): a line that breaks the grammar, an unclosed fence or comment,
    a repeated id (no first-wins here), or an id that is not `D` + 1-9 ASCII digits (ids are printed)."""
    parsed = parse_decisions(md)
    ids = [m.group(2) for ln in _visible(md)[0].splitlines() for m in (_CAND.match(ln),) if m]
    if parsed["unparsed"] or len(ids) != len(set(ids)) or not all(_SYNC_ID.fullmatch("D" + i) for i in ids):
        raise ValueError(E_SYNC_MALFORMED)
    return parsed


def _rewrite(md, changes):
    """(the text with each id of `changes` ({id: (status, [notes to try], via)}) resolved by `--sync`, the ids written, the ids that could
    not be). One pass (linear): a line is rewritten with the first note that reads back exactly (`_line(render_line(new)) == new`, at most
    MAX_LINE); a line holding an inline comment or fence, or none that reads back, is left as it is and listed."""
    out, done, bad = [], [], []
    for raw, v in zip(md.splitlines(keepends=True), _visible(md)[0].split("\n")):
        d = _line(v) if _CAND.match(v) and len(v) <= MAX_LINE else None
        if d and d["id"] in changes and d["id"] not in done and d["id"] not in bad:
            status, notes, via = changes[d["id"]]
            text = raw.splitlines()[0]  # the terminator may be U+2028 etc.: keep it as it is
            for note in notes:
                new = dict({k: x for k, x in d.items() if k != "via"}, status=status, by=SYNC_BY, note=note, via=via)
                line = render_line(new)
                if v == text and len(line) <= MAX_LINE and _line(line) == new:
                    raw = line + raw[len(text):]
                    done.append(d["id"])
                    break
            else:
                bad.append(d["id"])
        out.append(raw)
    return "".join(out), done, bad


def sync(md, rows, approvers, excluded, me):
    """(the new decisions file text, report). `rows` is `comments.fetch` output read completely (the caller ran the probe past the end);
    `approvers` the TARGET branch's, `excluded` from `approvals.excluded`, `me` the token user. For every `D<n>` line the newest VALID
    `/approve|/reject D<n>` (`approvals.newest`: the seven guard rules and the grammar) is taken when the newest-posting rule holds
    (`_posting_rule`) and the verdict comment is NEWER than the posting (parsed instants, then the numeric id; a tie loses), and the line is
    open or was itself written by a sync (`via` AND `by: approver`): a verdict a human wrote is never overwritten (`resolved-locally`;
    a hand edit that kept a `via` under another role is `local-override`), an approver comment NEWER than the winner that was edited,
    restricted or made unreadable stops the line (`newer-tampered`: nothing applied, listed in `tampered`), and a later
    valid `/reject` overrides an earlier synced approve (and back). The resolved line gets ` · via: <comment id>:<hash8>` (`approvals.pin`).
    Report: {"applied": [ids], "skipped": {id: SYNC_RULES code}, "status": {id: validated|rejected}, "bad_newer": {id: n}, "masked":
    {id: superseder}, "skipped_comments": {comment id: guard code}, "foreign_postings": [comment ids], "no_longer_valid": [ids] (only when
    some)}: ids and codes only. Sync never undoes a verdict: a `via` line neither renewed nor still backed by its comment is left and listed
    in `no_longer_valid`. ValueError (nothing returned) for a malformed file (`check_sync_file`), an approver's newer verdict that cannot be
    written, or a result that changes more than the target lines. Pure: no request."""
    from . import approvals
    parsed = check_sync_file(md)
    b = approvals.board(rows, approvers, excluded)
    index = posting_index(rows, me)
    by_id = {r.id: r for r in rows}
    changes, skipped, bad_newer, blocked, lost, ignored, tampered = {}, {}, {}, [], [], {}, []
    for d in sorted((d for k in _STATES for d in parsed[k]), key=lambda d: (len(d["id"]), d["id"])):
        i = d["id"]
        synced = bool(d.get("via")) and d["by"] == SYNC_BY  # written by a sync; a `via` under another role is a human's hand edit
        rule, pkey = _posting_rule(d, by_id, me, index)
        if rule is None and d["status"] != "open" and not synced:
            rule = "local-override" if d.get("via") else "resolved-locally"
        tally = {}
        win = approvals.newest(b, i, tally=tally, ignored=ignored)
        bad_newer[i] = tally.get("bad_newer", 0)
        if rule:
            skipped[i] = rule
            if synced:
                lost.append(i)  # the posting no longer backs this synced verdict: the file keeps it, the report says so
                if win is not None and _VERDICT.get(win["verb"]) not in (None, d["status"]):
                    blocked.append(i)  # its evidence can no longer be read: a newer opposite command must not be dropped
            continue
        if win is None or win["verb"] not in _VERDICT:
            skipped[i] = "no-verdict"
            if synced:
                lost.append(i)
            continue
        if win["tampered"]:
            skipped[i] = "newer-tampered"  # an approver's newer comment was edited/restricted/made unreadable: apply nothing
            tampered.append(i)
            if synced:
                lost.append(i)
            continue
        if win["key"] <= pkey:
            skipped[i] = "older-than-posting"
        elif not _VIA.fullmatch(win["pin"]):
            skipped[i] = "bad-comment-id"
        elif d["status"] == _VERDICT[win["verb"]] and d.get("via") == win["pin"]:
            skipped[i] = "up-to-date"
        else:
            changes[i] = (_VERDICT[win["verb"]], [_sync_note(win["text"]), ""], win["pin"])
        if synced and i not in changes and skipped.get(i) != "up-to-date":
            lost.append(i)
    out, applied, unwritable = _rewrite(md, changes)
    for i in unwritable:
        skipped[i] = "unwritable"
    blocked += unwritable  # an approver's verdict that cannot land is an error, never a quiet skip
    if blocked:
        raise ValueError(E_SYNC_BLOCKED % ", ".join(sorted(blocked, key=lambda x: (len(x), x))))
    a, c = md.splitlines(keepends=True), out.splitlines(keepends=True)
    after = parse_decisions(out)
    if (len(a) != len(c) or sum(x != y for x, y in zip(a, c)) != len(applied) or after["unparsed"]
            or _ids(after) != {**_ids(parsed), **{i: changes[i][0] for i in applied}}):
        raise ValueError("the sync would change more than the target lines; nothing written")
    applied.sort(key=lambda x: (len(x), x))
    _, by, _, stands = _answers(after)  # once: `masked_by` per id would be quadratic
    masked = {i: m for i in applied for m in (next((x for x in by.get(i, ()) if stands[x]), None),) if m}
    report = {"applied": applied, "skipped": skipped, "status": {i: changes[i][0] for i in applied}, "bad_newer": bad_newer,
              "masked": masked, "skipped_comments": ignored,
              "foreign_postings": list(index.foreign)}
    if lost:
        report["no_longer_valid"] = lost
    if tampered:
        report["tampered"] = tampered
    return out, report


def apply_sync(path, md, rows, approvers, excluded, me):
    """Run `sync` over `md` (the decisions file text the caller read before any request) and write the result in place only when something
    changed: the path is re-checked (no symlink, a regular file), the file must still be exactly `md`, then temp file + os.replace, read
    back (the old text is put back when it does not read back). ValueError (nothing written) otherwise. Returns the `sync` report."""
    new, report = sync(md, rows, approvers, excluded, me)
    if new == md:
        return report
    path = Path(path)
    try:
        if os.path.islink(str(path)) or not path.is_file():
            raise ValueError("the decisions file is a symbolic link or not a regular file; nothing written")
        if _read(path) != md:
            raise ValueError("the decisions file changed while syncing; nothing written, run it again")
        _atomic_write(path, new)
        try:
            back = _read(path)
        except (OSError, UnicodeError, ValueError):
            back = None
        if back != new:
            _atomic_write(path, md)
            raise ValueError("the decisions file did not read back as written; nothing recorded")
    except (OSError, UnicodeError) as e:
        raise ValueError("cannot update the decisions file (%s); nothing written" % e.__class__.__name__) from None
    return report
