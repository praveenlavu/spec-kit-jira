"""The open questions of a story: `Q<n>` lines in section 2 of the DoD ledger `dod/DoD_<KEY>.md`, and `--post` (W4).

Port of the fork source's `questions.py`: the `Q<n>` grammar and the post path. There is no separate questions file: the ledger is
the file. Every id is a STRING: a question id `Q<n>`, an account id, a Jira comment id; the fork's integer ids and GUIDs are gone.

`--post` (W4) differs from the fork by design: Jira comments are append-only here (no PUT or DELETE of a comment in 0.1), so a
changed open set gets a NEW comment and an older one stays as history. The comment is Markdown -> ADF (`adf.md_to_adf`): a marker
paragraph `[skj:questions <hash8>]` (hash8 = sha256 of the canonical Markdown after it), then as in the fork the open questions
(heading paragraph, bullet list) and the answers the team assumed (heading paragraph, bullet list), plain text nodes only; item
text is made inert first (`_safe`, on the whole line before the cut) and the ADF must read back to exactly the previewed Markdown.
Answers a human gave (not `assumed...`) are not posted; neither is an "all answered" comment (0.1). A2 note: the fork edited one
comment in place, 0.1 appends, and the open part of a posted comment ends at the assumed heading. The kit's own comment is found on Jira only (`_scan`: own author,
user account, no restriction, byte-equal body); the ledger's `Questions posted:` record is written after a post but never trusted
for the decision.

`--sync` (A2, EXPERIMENTAL comment approval, tamper-evident only): an approver's `/answer Q<n>: text` becomes the PO's answer on that
open question's line (a LOCAL write of the ledger, GETs only), under the newest-posting rule: the kit's newest own `[skj:questions]`
comment that lists Q<n> must show it OPEN exactly as the ledger reads now, be unedited and unrestricted, and be older than the answer.
"""
import hashlib
import json
import os
import re
import time

from . import adf, client as _client, comments, decisions, dod, issues, keys, push
from .dod import _visible, section
from .push import _clean

UNPARSED_NOTE = ("§2 has an unclosed code fence or comment, a question line over 2000 characters, or a Q number that is not plain "
                 "digits without a leading zero; questions could not be read")
_OPEN = "_(open)_"
# `- [ ] Q1: <question> → <answer | _(open)_> · owner: <role>`; lenient: spacing, `->`, `*`/`+` bullet, no owner.
# Linear on a blank run: the text after `Q<n>:` is taken whole and trimmed with str.strip, and the owner and arrow splits are
# str.rfind/find plus anchored patterns, never an unanchored `\s*X` scan (that backtracks quadratically over a long run of blanks).
_LINE = re.compile(r"^\s*(?:[-*+]\s+)?\[([ xX])\]\s*Q(\d+)\s*:(.*)$", re.S)
_EMPTY_ASSUMED = re.compile(r"assumed\s*:\s*$", re.I)
_OWNER_TAIL = re.compile(r"\s*owner\s*:(.*)$", re.I | re.S)  # what follows the LAST `·` or `|`
# a candidate question line: checked first and linear
_CAND = re.compile(r"\s*(?:[-*+]\s+)?\[[ xX]\]\s*Q\d")
MAX_LEDGER_LINE = 2000  # a longer question line is malformed: it is flagged unparsed and no regex below runs on it
MAX_LEDGER = 1 << 20  # bytes: the ledger is a repository file the PR controls


def _parts(rest):
    """(question, answer, owner) of the text after `Q<n>:`; owner is `?` when the line names none."""
    owner = "?"
    i = max(rest.rfind("·"), rest.rfind("|"))  # the owner separator is the last one: the owner text holds neither
    o = _OWNER_TAIL.match(rest, i + 1) if i >= 0 else None
    if o:
        rest, owner = rest[:i], o.group(1).strip() or "?"
    # the LAST `→` (a `->` in the question or the answer must not split it); only without one, the FIRST `->`
    i, n = rest.rfind("→"), 1
    if i < 0:
        i, n = rest.find("->"), 2
    question, answer = (rest[:i], rest[i + n:]) if i >= 0 else (rest, "")
    return question.strip(), answer.strip(), owner


def parse_questions(ledger_md):
    """Ledger section 2 -> {'open': [...], 'answered': [...], 'unparsed': bool}. Open: box `[ ]` or answer `_(open)_`/empty.
    Lines inside code fences or <!-- --> comments are ignored; `unparsed` is true when a fence or comment is
    left open in section 2 (later questions are then unreadable). No section 2 -> both empty."""
    out = {"open": [], "answered": []}
    body, out["unparsed"] = _visible(section(ledger_md, 2) or "")
    for line in body.splitlines():
        if not _CAND.match(line):
            continue
        if len(line) > MAX_LEDGER_LINE:
            out["unparsed"] = True
            continue
        m = _LINE.match(line)
        if not m:
            continue
        if not keys.line_number(m.group(2)):  # `Q٣`, `Q01`: never a question number, and never silently skipped
            out["unparsed"] = True
            continue
        question, answer, owner = _parts(m.group(3).strip())
        # a bare `assumed:` (nothing after it) records no assumption, so the question stays open
        done = m.group(1) in "xX" and answer not in ("", _OPEN) and not _EMPTY_ASSUMED.match(answer)
        item = {"id": "Q" + m.group(2), "question": question, "owner": owner}
        out["answered" if done else "open"].append({**item, "answer": answer} if done else item)
    first = {}
    for q in out["open"]:
        first.setdefault(q["id"], q)  # duplicate ids are listed once, the first wins
    out["open"] = list(first.values())
    if out["unparsed"]:
        out["open"].append({"id": "?", "question": UNPARSED_NOTE, "owner": "?"})
    return out


def read_ledger(path):
    """The ledger's text; ValueError over 1 MiB (counted in bytes) or unreadable (OSError/UnicodeError never leak their text)."""
    try:
        with path.open("rb") as f:
            raw = f.read(MAX_LEDGER + 1)
        if len(raw) > MAX_LEDGER:
            raise ValueError("the DoD ledger is larger than 1 MiB; refused")
        return raw.decode("utf-8")
    except (OSError, UnicodeError) as e:
        raise ValueError("cannot read the DoD ledger (%s)" % e.__class__.__name__)


# ---- the comment (W4): Markdown -> ADF by adf.md_to_adf: paragraphs, bullet lists and plain text nodes only ----
HEAD = "Open questions for the product owner:"
ASSUMED_HEAD = "The team assumed these; reply if one is wrong:"
MAX_LINE = 500
MAX_POST = 32 << 10  # bytes of the ADF JSON (and characters of the raw item lines before escaping): the kit's cap, Jira's is UNVERIFIED
_MARKER_RE = re.compile(r"\[skj:questions ([0-9a-f]{8})\]\Z")  # the WHOLE first line of a comment's canonical Markdown
_CID = re.compile(r"[1-9][0-9]{0,11}\Z")  # a comment id the ledger records: ASCII digits, no leading zero, a string
_SENT_NODES = frozenset({"doc", "paragraph", "bulletList", "listItem", "text"})
E_SIZE = "the questions make a comment larger than the 32 KiB local cap; nothing posted (answer or shorten some)"
E_SHAPE = "the comment would not read back exactly as previewed; nothing posted"
E_UNPARSED = ("ledger section 2 has an unclosed code fence or comment, a question line over 2000 characters, or a Q number that is "
              "not plain digits without a leading zero; nothing posted")
E_TRUNC = "the comment list of the story looks truncated or changed while it was read; nothing posted (run again)"
MAX_INSPECT = 20  # own comments the strict reader rejects that one run reads again one by one; more refuses (fail closed)
E_TOO_MANY = ("more than 20 own comments on the story cannot be read by the kit; nothing posted (one of them could be its "
              "questions comment: posting again could duplicate it)")
E_UNREADABLE = ("an own comment the kit cannot read (%s) may carry a questions marker or could not be checked; nothing posted "
                "(a second one could be a duplicate): run the command again, and if it persists remove or fix that comment in Jira")


def assumed(answered):
    """Answered questions whose answer is an `assumed: …` (accepted by the operator without the product owner)."""
    return [q for q in answered if q.get("answer", "").lower().startswith("assumed")]


def postable(parsed):
    """(open, assumed) questions the comment lists, in ledger order: open ones without the '?' note; assumed answers once per id
    (the first wins, as for open ones) and never for an id that is also open (it stays OPEN only). Other answers are not posted."""
    qs = [q for q in parsed["open"] if q["id"] != "?"]
    seen, out = {q["id"] for q in qs}, []
    for q in assumed(parsed["answered"]):
        if q["id"] not in seen:
            seen.add(q["id"])
            out.append(q)
    return qs, out


def _raw(q):
    return "%s: %s" % (q["id"], q["question"]) + (" → %s" % q["answer"] if "answer" in q else "")


def _safe(text):
    """Ledger text as inert comment text, linear: control, format (bidi), line-separator characters dropped and blanks collapsed;
    wiki mentions, `@name` and addresses replaced (decisions' scrub: no accountId travels, nobody is notified); then the three inline
    Markdown forms md_to_adf knows (`code`, **strong**, [text](url)) broken, so every item becomes ONE plain text node. Not
    scrubbed: `@@x` and a full-width `＠x` (whether Jira turns typed text into a mention at all is UNVERIFIED)."""
    return decisions._scrub(_clean(text)).replace("`", "'").replace("*", "\u2217").replace("](", "] (")


def _line(q):
    """(the item text: `Q<n>: <question>[ → <answer>]` made inert by `_safe` on the WHOLE line, then capped at MAX_LINE chars with a
    trailing …, was it cut). Scrub first: a cut through a mention or an address would leave a fragment the scrub cannot see."""
    text = _safe(_raw(q))
    return (text[:MAX_LINE - 1] + "…", True) if len(text) > MAX_LINE else (text, False)


def cut(questions_):
    """Ids of the questions whose comment line is cut to MAX_LINE."""
    return [q["id"] for q in questions_ if q["id"] != "?" and _line(q)[1]]


def _hash8(md):
    return hashlib.sha256(md.encode("utf-8", "surrogatepass")).hexdigest()[:8]


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.get("content", ()))


def body(open_questions, assumed_questions=()):
    """(Markdown, ADF doc, hash8) of the comment, as in the fork: the marker paragraph `[skj:questions <hash8>]`, then (open questions)
    HEAD and a `- Q<n>: <question>` list, then (assumed answers) ASSUMED_HEAD and a `- Q<n>: <question> → <answer>` list (the '?' note is
    never an item). hash8 = first 8 hex of sha256 over the canonical Markdown of everything after the marker, so a change of either set
    is a new comment. ValueError (fixed text) when both are empty, over MAX_POST, or when the ADF is not exactly that sequence of
    paragraphs and bullet lists of plain text nodes reading back (`adf.adf_to_md`) to the same Markdown: the preview is what is sent."""
    groups = [(head, [q for q in qs if q["id"] != "?"]) for head, qs in ((HEAD, open_questions), (ASSUMED_HEAD, assumed_questions))]
    groups = [(head, qs) for head, qs in groups if qs]
    if not groups:
        raise ValueError("no open questions or assumed answers; nothing to post")
    if sum(len(_raw(q)) for _, qs in groups for q in qs) > MAX_POST:  # before the scrub: its cost is bounded by the cap
        raise ValueError(E_SIZE)
    return _compose("questions", [(head, [_line(q)[0] for q in qs]) for head, qs in groups], E_SIZE)


def _compose(tag, groups, e_size):
    """(Markdown, ADF doc, hash8) of a kit comment: the marker paragraph `[skj:<tag> <hash8>]`, then per group its heading paragraph and a
    bullet list of the (already inert) item texts. Shared with `decisions --post` (W5). ValueError (E_SHAPE, `e_size`) unless the ADF
    is exactly that layout of plain text nodes, reads back to the same Markdown, and is at most MAX_POST bytes."""
    rest = "\n\n".join(head + "\n\n" + "\n".join("- " + x for x in items) for head, items in groups)
    h = _hash8(rest)
    md = "[skj:%s %s]\n\n%s" % (tag, h, rest)
    try:
        doc = adf.md_to_adf(md)
        same = adf.adf_to_md(doc, strict=True) == md
    except adf.AdfError:
        raise ValueError(E_SHAPE) from None
    layout = [n["type"] for n in doc["content"]] == ["paragraph"] + ["paragraph", "bulletList"] * len(groups)
    if (not same or not layout or any(n["type"] not in _SENT_NODES or "marks" in n for n in _walk(doc))
            or doc["content"][0]["content"][0]["text"] != md.split("\n", 1)[0]):
        raise ValueError(E_SHAPE)
    if len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_POST:
        raise ValueError(e_size)
    return md, doc, h


def _rows(c, key, e_trunc=E_TRUNC):
    """Every comment of `key` (comments.fetch), then one probe past the end: a total that is not exactly what was read, or a comment
    there, refuses (fail closed: a later page could hold the kit's own comment, and posting again would duplicate it; for a sync, a
    newer retraction could be there)."""
    rows = comments.fetch(c, key)
    page = c.request("GET", "/issue/%s/comment" % key, params={"startAt": len(rows), "maxResults": 1}).data
    if not (isinstance(page, dict) and type(page.get("total")) is int and page["total"] == len(rows) and page.get("comments") == []):
        raise issues.ReadError(e_trunc)
    return rows


def _report():
    return {"foreign": [], "restricted": [], "mismatch": [], "unreadable": []}


def _scan(rows, me, md, marker=_MARKER_RE):
    """(the id of the oldest comment that IS the planned comment, report). A comment counts only when its canonical Markdown's first
    line is a marker, its author is `me` (accountType atlassian), it has no visibility restriction and no unknown node, its body
    hashes to its own marker, and it equals the planned Markdown byte for byte (the hash is a label, never the proof: 32 bits). An
    own comment edited back to exactly the planned text counts (nothing is lost by not posting it again). Marker comments that do
    not count are reported by id: `foreign` (another author: a forged marker never suppresses a post), `restricted`, `mismatch`
    (its body no longer matches its own marker: edited), `unreadable` (an own comment the strict reader rejects: md is "")."""
    found, report = None, _report()
    for r in rows:
        if r.unknown_nodes and r.author is not None and r.author == me and r.author_type == "atlassian":
            report["unreadable"].append(r.id)
            continue
        m = marker.match(r.md.split("\n", 1)[0])
        if not m:
            continue
        if r.author is None or r.author != me or r.author_type != "atlassian":
            report["foreign"].append(r.id)
        elif r.visibility is not None:
            report["restricted"].append(r.id)
        elif _hash8(r.md.split("\n\n", 1)[1] if "\n\n" in r.md else "") != m.group(1):
            report["mismatch"].append(r.id)
        elif r.md == md and found is None:
            found = r.id
    return found, report


# ---- the ledger record: one `- **Questions posted:** <comment id>:<hash8>` line in section 2 (decisions' `posted:` grammar) ----
RECORD = "- **Questions posted:** "
_RECORD_RE = re.compile(r"- \*\*Questions posted:\*\* ([1-9][0-9]{0,11}):([0-9a-f]{8})\Z")


def _record_at(md):
    """(span of section 2, its raw lines, index of the record line or None, the record (id, hash8) or None). ValueError when there
    is no section 2, two record lines, a malformed or over-long one, or one holding an inline comment (a rewrite would drop it).
    Only visible lines count: a record inside a fence or comment is text. A question line never starts like a record."""
    span = dod.section_span(md, 2)
    if span is None:
        raise ValueError("the DoD ledger has no section 2; the comment id cannot be recorded")
    raws = md[span[0]:span[1]].splitlines(keepends=True)
    hits = [(n, v) for n, v in enumerate(_visible(md[span[0]:span[1]])[0].split("\n")[:len(raws)]) if v.startswith(RECORD.rstrip())]
    if len(hits) > 1:
        raise ValueError("the DoD ledger has more than one `Questions posted:` line; fix it by hand")
    if not hits:
        return span, raws, None, None
    n, v = hits[0]
    m = _RECORD_RE.match(v) if len(v) <= MAX_LEDGER_LINE else None
    if not m or v != raws[n].splitlines()[0]:
        raise ValueError("the DoD ledger's `Questions posted:` line is malformed; fix it by hand (`- **Questions posted:** <id>:<hash8>`)")
    return span, raws, n, (m.group(1), m.group(2))


def recorded(md):
    """The (comment id, hash8) strings the ledger records as posted, or None. Informational: the post decision never trusts it
    (the ledger is PR-controlled); only Jira's own comment, by its author and body, decides."""
    return _record_at(md)[3]


def record_text(md, cid, h):
    """`md` with the record set to `cid`:`h` (the one line rewritten, or added after the last non-blank line of section 2); every other
    byte unchanged. ValueError unless it parses back: the same questions, this record, one line changed or added, at most 1 MiB."""
    if not (isinstance(cid, str) and _CID.match(cid) and isinstance(h, str) and re.fullmatch(r"[0-9a-f]{8}", h)):
        raise ValueError("not a comment id string and hash8; nothing recorded")
    (a, b), raws, n, _ = _record_at(md)
    line = RECORD + cid + ":" + h
    if n is not None:
        raws[n] = line + raws[n][len(raws[n].splitlines()[0]):]
    else:
        k = max([i for i, x in enumerate(raws) if x.strip()] or [0])
        eol = "\r\n" if raws[k].endswith("\r\n") else "\n"
        if not raws[k].endswith(eol):
            raws[k] += eol  # the section ends without a line break (end of file); another terminator fails the guard below
        raws.insert(k + 1, line + eol)
    new = md[:a] + "".join(raws) + md[b:]
    old_l, new_l = md.splitlines(), new.splitlines()
    i = next((j for j, (x, y) in enumerate(zip(old_l, new_l)) if x != y), len(old_l))
    one = (new_l[:i] + new_l[i + 1:] == old_l[:i] + old_l[i + 1:]) if len(new_l) == len(old_l) else new_l[:i] + new_l[i + 1:] == old_l
    if (not one or recorded(new) != (cid, h) or parse_questions(new) != parse_questions(md)
            or len(new.encode("utf-8", "surrogatepass")) > MAX_LEDGER):
        raise ValueError("the comment id would not read back from the ledger; nothing recorded")
    return new


def apply_record(root, key, md, cid, h):
    """Write the record into `dod/DoD_<KEY>.md` (path re-checked: no symlink, a regular file), only when the file is still exactly `md`
    (decisions' temp file + os.replace, read back, the old text put back when it does not read back). ValueError (fixed text) otherwise."""
    path = dod.ledger_path(root, key)
    new = record_text(md, cid, h)
    try:
        if read_ledger(path) != md:
            raise ValueError("the DoD ledger changed since it was read; the comment id was not recorded (run again to record it)")
        decisions._atomic_write(path, new)
        if read_ledger(path) != new:
            decisions._atomic_write(path, md)
            raise ValueError("the DoD ledger did not read back as written; nothing recorded")
    except (OSError, UnicodeError) as e:
        raise ValueError("cannot update the DoD ledger (%s); nothing recorded" % e.__class__.__name__) from None


def _me(c, tail="nothing posted"):
    """The authenticated account id (a string), for the post and sync paths to tell their own comments from others'. Unresolvable ->
    ValueError with a fixed text: never guess whose comment it is, never echo the server's words."""
    try:
        return issues.me(c)["accountId"]
    except _client.ClientError:  # `issues.ReadError` is one
        raise ValueError("cannot tell which Jira comments are yours (the identity could not be read); %s" % tail) from None


# ---- `skj questions <ref> --post` (W4): plan (reads only), then one POST behind the X2 gate ----

def plan_post(c, cfg, key, parsed, ledger_md):
    """Reads only: {"key", "action": none|post|noop, "md", "doc", "hash", "found", "record", "cut", "report", "me"}. No open question
    and no assumed answer (`postable`): `none`, and no request at all (`c` may be None); an "all answered" comment is not posted in
    0.1. Otherwise `issues.get` first (a moved issue is KeyMismatch; the project must be the configured one), then the identity,
    then every comment (`_rows`): `noop` when the kit's own comment already shows exactly this text (`_scan`), else `post`. An own
    comment the strict reader rejects is read once more on its own (GET, at most MAX_INSPECT, stopping at the first) and, unless
    it is definitively not a questions comment (`_marked`), refuses the post (E_UNREADABLE; the live shape of such a comment is
    UNVERIFIED, LV2-AC1). `record`: a found comment the ledger
    does not record yet. An unreadable section 2 refuses before anything else."""
    if not (keys.valid_key(key) and key.rsplit("-", 1)[0] == cfg.project):
        raise ValueError("not an issue key of project %s" % cfg.project)
    if parsed["unparsed"]:
        raise ValueError(E_UNPARSED)
    rec = recorded(ledger_md) if ledger_md is not None else None  # a malformed record refuses before any request
    qs, assumed_qs = postable(parsed)
    if any(decisions.has_format(q["question"] + q.get("answer", "")) for q in qs + assumed_qs):
        raise ValueError("a question to post holds a bidi control or an invisible (format) character; nothing posted: "
                         + decisions.E_FORMAT_WHY)
    p = {"key": key, "action": "none", "md": "", "doc": None, "hash": None, "found": None, "record": False,
         "cut": cut(qs + assumed_qs), "report": _report(), "me": None}
    if not (qs or assumed_qs):
        return p
    p["md"], p["doc"], p["hash"] = body(qs, assumed_qs)
    p["me"], p["found"], p["report"] = _own_comment(c, cfg, key, p["md"])
    p["action"] = "noop" if p["found"] else "post"
    p["record"] = p["found"] is not None and rec != (p["found"], p["hash"])
    return p


def _own_comment(c, cfg, key, md, marker=_MARKER_RE, prefix="[skj:questions", e_too_many=E_TOO_MANY, e_unreadable=E_UNREADABLE):
    """(me, the id of the kit's own comment that shows exactly `md` or None, report): the reads of a post plan, in this order and shared
    with `decisions --post` (W5): `issues.get` (a moved issue is KeyMismatch; the project must be the configured one), the identity,
    every comment plus the probe past the end (`_rows`), `_scan` for `marker`; then each own comment the strict reader rejects is
    inspected (at most MAX_INSPECT, stopping at the first that refuses: `_marked` with `prefix`), refusing with the fixed texts."""
    me, rows = _story(c, cfg, key)
    found, report = _scan(rows, me, md, marker)
    unreadable = report["unreadable"]
    if len(unreadable) > MAX_INSPECT:
        raise ValueError(e_too_many)
    marked = next((cid for cid in unreadable if _marked(c, key, cid, prefix)), None)  # stops at the first one that refuses
    if marked:
        raise ValueError(e_unreadable % marked)
    return me, found, report


def _story(c, cfg, key, tail="nothing posted", e_trunc=E_TRUNC):
    """(me, every comment of `key`): the reads a post plan and a sync share, in this order: `issues.get` (a moved issue is KeyMismatch;
    the project must be the configured one), the identity, every comment plus the probe past the end (`_rows`). Fixed texts end in `tail`."""
    f = issues.get(c, key, ["project"]).get("fields")
    proj = f.get("project") if isinstance(f, dict) else None
    if not (isinstance(proj, dict) and proj.get("key") == cfg.project):
        raise issues.ReadError("the issue is not in project %s; %s" % (cfg.project, tail))
    me = _me(c, tail)
    return me, _rows(c, key, e_trunc)


def _marked(c, key, cid, prefix="[skj:questions"):
    """Whether the own comment `cid` (one the strict reader rejected) must be taken as a questions comment: fail closed. It is NOT
    marked only when a fresh GET answers that very comment with an ADF object whose non-strict Markdown is non-empty, readable from its
    first character (no `[?` placeholder there: an unknown node before the marker hides it) and does not start with
    `[skj:questions`. A failed or odd answer, another id, a missing or non-object body, or a render error counts as marked."""
    try:
        d = c.request("GET", "/issue/%s/comment/%s" % (key, cid)).data
    except _client.ClientError:
        return True
    body_ = d.get("body") if isinstance(d, dict) and d.get("id") == cid else None
    if not isinstance(body_, dict):
        return True
    try:
        md = adf.adf_to_md(body_)
    except (adf.AdfError, RecursionError):
        return True
    return md == "" or md.startswith("[?") or md.startswith(prefix)  # empty contradicts the unknown nodes just seen


_REPORTS = (("foreign", "marker comment(s) by another account ignored (a marker never counts unless the kit's own account wrote it)"),
            ("restricted", "own marker comment(s) with a visibility restriction ignored"),
            ("mismatch", "own marker comment(s) whose body no longer matches the marker (edited) ignored"),
            ("unreadable", "own comment(s) the kit cannot read (no questions marker) ignored"))


def render_plan(p):
    """The canonical plan text: deterministic, no people, no account ids; the body is the exact ADF-as-Markdown that is sent."""
    lines = ["Plan for %s: post the open questions as one comment" % p["key"]]
    if p["action"] == "none":
        return "\n".join(lines + ["action: none (no open questions; nothing to post)"]) + "\n"
    rec = "%s%s:%s" % (RECORD, p["found"] or "<new comment id>", p["hash"])
    if p["action"] == "post":
        lines += ["action: post ONE new comment [skj:questions %s] (an earlier comment is never edited or deleted)" % p["hash"],
                  "ledger: then record `%s` in section 2" % rec]
    else:
        lines += ["action: none (comment %s already shows these questions)" % p["found"],
                  "ledger: record `%s` in section 2" % rec if p["record"] else "ledger: already recorded"]
    if p["cut"]:
        lines.append("note: %s cut to %d characters (…)" % (", ".join(p["cut"]), MAX_LINE))
    for k, what in _REPORTS:
        if p["report"][k]:
            lines.append("note: %d %s: %s" % (len(p["report"][k]), what, ", ".join(p["report"][k])))
    if p["action"] == "post":
        lines += ["", "Comment body (ADF as Markdown, exactly what is sent):", ""]
        lines += ["> " + x if x else ">" for x in p["md"].split("\n")]
    return "\n".join(lines) + "\n"


def _recheck(c, p, marker=_MARKER_RE):
    """After an uncertain post: a fresh read of every comment (no cache); (the id found, the line to print). Never posts again."""
    try:
        found, _ = _scan(_rows(c, p["key"]), p["me"], p["md"], marker)
    except _client.ClientError:
        return None, "write uncertain: the re-read failed; not posted again in this run (the next run's re-read decides)"
    if found:
        return found, "posted (found on re-read): comment %s" % found
    return None, "write uncertain: not found on re-read; not posted again in this run (the next run's re-read decides)"


def apply_post(c, p, root, ledger_md, clock=time.monotonic, sleep=time.sleep):
    """The plan's one write, then the ledger record: {"lines", "ok"}. POST /issue/{key}/comment with {"body": <adf>} only (no
    visibility, no properties), paced (push's pacer), never retried. A timeout, a 5xx, a 2xx/3xx it cannot read or a lost
    connection is uncertain: one re-read decides what is printed, nothing is posted again, and `ok` is False. A 4xx: not posted
    (status only). The comment id (a string) is recorded in the ledger after a post, a found-on-re-read, or a noop it lacks."""
    lines, ok, cid = [], True, p["found"]
    if p["action"] == "none":
        return {"lines": lines, "ok": ok}
    if p["action"] == "post":
        push._Pacer(clock, sleep).wait()
        try:
            d = c.request("POST", "/issue/%s/comment" % p["key"], body={"body": p["doc"]}).data
            cid = d.get("id") if isinstance(d, dict) else None
            if not (isinstance(cid, str) and _CID.match(cid)):
                raise _client.WriteUncertain("POST", "a malformed answer from Jira")
            lines.append("posted comment %s [skj:questions %s]" % (cid, p["hash"]))
        except _client.JiraError as e:
            if type(e.status) is int and 400 <= e.status < 500:
                return {"lines": ["failed: %s; nothing posted" % _client.why(e)], "ok": False}
            ok = False
            cid, line = _recheck(c, p)
            lines.append(line)
        except _client.ClientError:  # WriteUncertain and any other failure after the request may have left
            ok = False
            cid, line = _recheck(c, p)
            lines.append(line)
    if cid is not None and (p["action"] == "post" or p["record"]):
        if not _CID.match(cid):
            return {"lines": lines + ["ledger: Jira's comment id is not a valid comment id; not recorded"], "ok": False}
        try:
            apply_record(root, p["key"], ledger_md, cid, p["hash"])
            lines.append("ledger: recorded %s%s:%s" % (RECORD, cid, p["hash"]))
        except ValueError as e:
            lines.append("ledger: %s" % e)
            ok = False
    elif p["action"] == "noop":
        lines.append("nothing posted: comment %s already shows these questions" % cid)
    return {"lines": lines, "ok": ok}



# ---- `skj questions <ref> --sync` (A2): an approver's `/answer Q<n>: text` becomes the PO's answer (a LOCAL write of the ledger) ----
SYNC_RULES = ("has-answer", "posting-lossy", "not-posted", "posting-edited", "posting-restricted", "posting-superseded", "no-answer",
              "older-than-posting", "newer-tampered", "bad-comment-id")  # why an open question was left alone; fixed names
MAX_ANSWER = 500
E_MALFORMED = ("the ledger's section 2 is malformed (a missing section, an unclosed fence or comment, a repeated or non-ASCII question id, "
               "or a question line over %d characters); nothing written" % MAX_LEDGER_LINE)
E_SYNC_TRUNC = "the comment list of the story looks truncated or changed while it was read; nothing written (run again)"
_ID = re.compile(r"Q[0-9]{1,9}")
_SYNCED = re.compile(r"\(PO comment ([0-9]+)\)$")  # the provenance a sync writes at the end of an answer
_PO_COMMENT = re.compile(r"\bPO\s+comment\b", re.I)
_SEPARATORS = re.compile(r"->|[·|→]")
_POSTED_Q = re.compile(r"(Q[1-9][0-9]{0,8}): ")


def check_ledger(md):
    """`parse_questions(md)` for a sync, or ValueError (E_MALFORMED): no section 2, an unclosed fence or comment, a question line over
    MAX_LEDGER_LINE (checked BEFORE a regex runs on it), a repeated question id (open or answered: no first-wins), or an id that is not
    `Q` + 1-9 ASCII digits."""
    span = dod.section_span(md, 2)
    if span is None:
        raise ValueError(E_MALFORMED)
    cands = [v for v in _visible(md[span[0]:span[1]])[0].split("\n") if _CAND.match(v)]
    if any(len(v) > MAX_LEDGER_LINE for v in cands):
        raise ValueError(E_MALFORMED)
    ids = ["Q" + m.group(2) for v in cands for m in (_LINE.match(v),) if m]
    parsed = parse_questions(md)
    if parsed["unparsed"] or len(ids) != len(set(ids)) or not all(_ID.fullmatch(i) for i in ids):
        raise ValueError(E_MALFORMED)
    return parsed


def _posted_qs(rest):
    """[(Q id, "open"|"assumed", item text)] of a questions posting's Markdown after the marker, or None unless it is exactly the W4
    layout: HEAD and a bullet list, then ASSUMED_HEAD and a bullet list, either or both, in that order."""
    parts = rest.split("\n\n")
    heads = parts[0::2]
    if len(parts) not in (2, 4) or heads not in ([HEAD], [ASSUMED_HEAD], [HEAD, ASSUMED_HEAD]):
        return None
    out = []
    for head, block in zip(heads, parts[1::2]):
        for ln in block.split("\n"):
            if not ln.startswith("- "):
                return None
            m = _POSTED_Q.match(ln, 2)
            if m:
                out.append((m.group(1), "open" if head == HEAD else "assumed", ln[2:]))
    return out


def _answer_text(text):
    """An approver's answer (already made inert by `approvals._note`) as ledger text: the grammar's own separators (`→`, `->`, `·`, `|`)
    turned into `-`, then `_safe` (control, bidi and separator characters dropped, mentions and addresses replaced), `<!--`, `-->` and
    fences blanked, the words `PO comment` neutralised (only the sync writes `(PO comment <id>)`), an answer that would read as the team's
    assumption (`assumed...`) prefixed so it never does, cut to MAX_ANSWER. May be empty."""
    t = _safe(_SEPARATORS.sub("-", text))
    t = " ".join(decisions._MARKUP.sub(" ", t).split())
    t = _PO_COMMENT.sub("PO-comment", t)
    if t.lower().startswith("assumed"):
        t = "PO answer: " + t
    return t[:MAX_ANSWER].strip()


def _q_rule(q, index):
    """(rule, the posting's order key) for an open question `q` under the newest-posting rule: the newest own questions posting that lists
    q's id must list it OPEN with the line the ledger holds now (`not-posted`), be unedited and unrestricted, with no own unreadable comment
    newer than it (`posting-superseded`)."""
    from . import approvals
    text, cut_ = _line(q)
    if cut_ or _safe(_raw(q)) != _raw(q):
        return "posting-lossy", None
    best = index.best.get(q["id"])
    if best is None:
        return "not-posted", None
    key, r, shown = best
    if index.opaque is not None and index.opaque > key:
        return "posting-superseded", None
    if not approvals.unedited(r):
        return "posting-edited", None
    if r.visibility is not None:
        return "posting-restricted", None
    if ("open", text) not in shown:
        return "not-posted", None
    return None, key


def sync(md, rows, approvers, excluded, me):
    """(the new ledger text, {"answered": [ids], "open": [ids still open], "skipped": {id: rule}, "bad_newer": {id: n}, "ignored": [ids],
    "skipped_comments": {comment id: guard code}, "foreign_postings": [comment ids]}): ids and fixed codes only, never question or answer
    text. For every OPEN question whose line has no answer text yet (`has-answer`: never overwritten), the newest VALID `/answer Q<n>:
    text` (`approvals.newest`; `no-answer`) is taken when the newest-posting rule holds (`_q_rule`) and the answer is NEWER than that
    posting (`older-than-posting`: parsed instants, then the numeric id; a tie loses). The line is rewritten to
    `- [x] Q<n>: <question> → <answer> (PO comment <id>) · owner: PO`; every other byte is unchanged. `ignored`: ANSWERED questions on
    which an approver's newest valid `/answer` is not the comment the line names (never applied, only reported). ValueError (nothing
    written) for a malformed ledger (`check_ledger`), an approver's valid answer that cannot be written, or a result that does not read
    back with exactly those lines changed. Pure: no request."""
    from . import approvals
    parsed = check_ledger(md)
    b = approvals.board(rows, approvers, excluded)
    index = approvals.postings(rows, me, _MARKER_RE, "[skj:questions", _posted_qs)
    a, z = dod.section_span(md, 2)
    raws = md[a:z].splitlines(keepends=True)
    lines = _visible(md[a:z])[0].split("\n")
    at = {}
    for n, v in enumerate(lines[:len(raws)]):
        m = _LINE.match(v) if _CAND.match(v) else None
        if m:
            at["Q" + m.group(2)] = (n, m)
    written, skipped, bad_newer, blocked, still, ignored = {}, {}, {}, [], [], {}
    for q in sorted(parsed["open"], key=lambda q: (len(q["id"]), q["id"])):
        i = q["id"]
        n, m = at[i]
        _, answer, _ = _parts(m.group(3).strip())
        tally = {}
        win = approvals.newest(b, i, tally=tally, ignored=ignored)
        bad_newer[i] = tally.get("bad_newer", 0)
        rule, pkey = (("has-answer", None) if answer not in ("", _OPEN) and not _EMPTY_ASSUMED.match(answer) else _q_rule(q, index))
        if rule is None:
            if win is None or win["verb"] != "answer":
                rule = "no-answer"
            elif win["tampered"]:
                rule = "newer-tampered"  # an approver's newer comment was edited/restricted/made unreadable: apply nothing
            elif win["key"] <= pkey:
                rule = "older-than-posting"
            elif not _CID.match(win["id"]):
                rule = "bad-comment-id"
        if rule:
            skipped[i] = rule
            still.append(i)
            continue
        text = _answer_text(win["text"])
        v, raw = lines[n], raws[n]
        eol = raw[len(raw.splitlines()[0]):]  # splitlines also ends a line at U+2028, \x0c, \x85, \x1c: keep that terminator
        new = "%sx%s %s → %s (PO comment %s) · owner: PO" % (v[:m.start(1)], v[m.end(1):m.start(3)], q["question"], text, win["id"])
        if not text or v != raw.splitlines()[0] or len(new) > MAX_LEDGER_LINE:
            blocked.append(i)
            continue
        raws[n] = new + eol
        written[i] = {"id": i, "question": q["question"], "owner": "PO", "answer": "%s (PO comment %s)" % (text, win["id"])}
    answered_ignored = []
    for q in sorted(parsed["answered"], key=lambda q: (len(q["id"]), q["id"])):
        win = approvals.newest(b, q["id"])
        named = _SYNCED.search(q["answer"])
        if win is not None and win["verb"] == "answer" and (named is None or named.group(1) != win["id"]):
            answered_ignored.append(q["id"])
    if blocked:
        raise ValueError("an approver's answer cannot be written to the ledger for %s (an inline comment on the line, an empty answer or a "
                         "line over %d characters); fix the line and run it again; nothing written" % (", ".join(blocked), MAX_LEDGER_LINE))
    out = md[:a] + "".join(raws) + md[z:]
    old_l, new_l = md.splitlines(keepends=True), out.splitlines(keepends=True)
    after = parse_questions(out)

    def by_id(ls):
        return sorted(ls, key=lambda x: (len(x["id"]), x["id"]))
    if (len(old_l) != len(new_l) or sum(x != y for x, y in zip(old_l, new_l)) != len(written) or after["unparsed"]
            or after["open"] != [q for q in parsed["open"] if q["id"] not in written]
            or by_id(after["answered"]) != by_id(parsed["answered"] + list(written.values()))):
        raise ValueError("the sync would change more than the answered lines, or they do not read back; nothing written")
    rep = {"answered": list(written), "open": still, "skipped": skipped, "bad_newer": bad_newer, "ignored": answered_ignored,
           "skipped_comments": ignored, "foreign_postings": list(index.foreign)}
    tampered = [i for i, r in skipped.items() if r == "newer-tampered"]
    if tampered:
        rep["tampered"] = tampered
    return out, rep


def apply_sync(root, key, md, rows, approvers, excluded, me):
    """Run `sync` over `md` (the ledger text the caller read before any request) and write the result over `dod/DoD_<KEY>.md` only when
    something changed: the path is re-checked (`dod.ledger_path`: no symlink, a regular file), the file must still be exactly `md`, then
    temp file + os.replace and a read-back (the old text is put back when it does not read back). ValueError (nothing written) otherwise."""
    new, report = sync(md, rows, approvers, excluded, me)
    if new == md:
        return report
    path = dod.ledger_path(root, key)
    try:
        if os.path.islink(str(path)) or not path.is_file():
            raise ValueError("the DoD ledger is a symbolic link or not a regular file; nothing written")
        if read_ledger(path) != md:
            raise ValueError("the DoD ledger changed while syncing; nothing written, run it again")
        decisions._atomic_write(path, new)
        try:
            back = read_ledger(path)
        except ValueError:
            back = None
        if back != new:
            decisions._atomic_write(path, md)
            raise ValueError("the DoD ledger did not read back as written; nothing recorded")
    except (OSError, UnicodeError) as e:
        raise ValueError("cannot update the DoD ledger (%s); nothing written" % e.__class__.__name__) from None
    return report
