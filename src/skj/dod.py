"""The DoD ledger `dod/DoD_<KEY>.md`: seeded from the story's acceptance criteria, then authored by hand.

Section 1 is the AC: the description's section under the configured heading (`ac_section`), as canonical Markdown (S5).
`ac_hash` is taken over that canonical Markdown (J3) after `adf.safe_text` removed control, bidi and zero-width characters (D-011),
never over raw ADF and never over the escaped ledger text.
A refresh re-renders the header and section 1 and keeps everything from the `## 2.` marker on, section 5 included.

Story text (summary, AC) is untrusted. It is escaped per line so it can never open a fence or a comment that hides later
sections, or forge a `## ` heading, a header line or a section 5 box (`_esc`, `_hv`, `_clean`). Readers of the ledger
(`section`, `ac_lines`, the header values) ignore what sits inside fences and comments.

The heading match is conservative: a top-level ADF heading whose own text equals `heading` exactly after strip (case
matters; a bold paragraph is not a heading). No heading, two matching headings, or nothing under it: refused, never invented.
"""
import hashlib
import os
import re
import unicodedata
from datetime import date
from pathlib import Path

from . import adf, config, issues, keys

FIELDS = ["summary", "description", "issuetype", "status", "project"]  # explicit: no people, no comments
MAX_SUMMARY = 200
MAX_NAME = 64
SECTION2 = "## 2. Brainstorm & plan"
SPEC_DIR_UNSET = "_(set by speckit.jira-lifecycle.pull)_"
# Below this the AC cannot be decomposed into AC-1…AC-n without inventing requirements. Advisory only.
AC_THIN_CHARS = 150

NO_HEADING = ("the story's description has no '%s' heading: refused (the kit never invents acceptance criteria; "
              "add the section in Jira, or set ac_heading in skj.config.json)")
TWO_HEADINGS = "the story's description has more than one '%s' heading: refused (the kit never guesses which one holds the AC)"
EMPTY = "the story's description has nothing under its '%s' heading: refused (the kit never invents acceptance criteria)"


class MissingAC(ValueError):
    """The description holds no usable AC section. Fixed text: the configured heading only, never story text."""


class DodExists(Exception):
    """The ledger exists and this is not a refresh; or a refresh has no ledger, cannot read it, or would splice from the wrong place."""


class UnsafePath(ValueError):
    """The ledger path would leave dod/ (a symbolic link) or is not a regular file."""


# --- the AC section ------------------------------------------------------------------------------------------------------

def _heading(n):
    """(level, own text stripped) of an ADF heading made of text nodes only, else None."""
    if not (isinstance(n, dict) and n.get("type") == "heading" and isinstance(n.get("attrs"), dict)):
        return None
    level, content = n["attrs"].get("level"), n.get("content", [])
    if type(level) is not int or not isinstance(content, list):
        return None
    if not all(isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str) for c in content):
        return None
    return level, "".join(c["text"] for c in content).strip()


def ac_section(description_adf, heading):
    """Canonical Markdown of the top-level blocks under `heading`, up to the next heading of the same or a higher level.
    AdfError over 1 MiB (the whole description) or on a malformed doc; AdfUnknown on an unknown node in the section."""
    adf._check(description_adf)  # size and depth of the whole description, before anything is read from it
    if description_adf is None:
        raise MissingAC(NO_HEADING % heading)
    d = description_adf
    if not (isinstance(d, dict) and d.get("type") == "doc" and type(d.get("version")) is int and d["version"] == 1
            and isinstance(d.get("content", []), list)):
        raise adf.AdfUnknown("not-a-doc")
    content = d.get("content", [])
    want = heading.strip()
    hits = [i for i, n in enumerate(content) if (_heading(n) or (0, None))[1] == want]
    if not hits:
        raise MissingAC(NO_HEADING % heading)
    if len(hits) > 1:
        raise MissingAC(TWO_HEADINGS % heading)
    level, body = _heading(content[hits[0]])[0], []
    for n in content[hits[0] + 1:]:
        if isinstance(n, dict) and n.get("type") == "heading" and isinstance(n.get("attrs"), dict) \
                and type(n["attrs"].get("level")) is int and n["attrs"]["level"] <= level:
            break
        body.append(n)
    md = adf.safe_text(adf.canonical_md({"version": 1, "type": "doc", "content": body}))  # D-011: never a raw ESC, bidi or zero-width
    if not md.strip():
        raise MissingAC(EMPTY % heading)
    return md


def ac_hash(md):
    """sha256 hex of the AC's canonical Markdown after `adf.safe_text` (what `ac_section` returns): a story that differs only by the
    removed control, bidi or zero-width characters hashes the same, so drift is never reported for them."""
    return hashlib.sha256(md.encode("utf-8", "surrogatepass")).hexdigest()


# --- escaping ------------------------------------------------------------------------------------------------------------

# One scheme, per line: a leading `\` before a line-start ``` / ~~~ / # (after indent and one list marker), and
# `<!--` -> `&lt;!--` anywhere. `_unescape` is its inverse for `ac_lines`, and also removes the one backslash the ADF
# reader puts before literal block syntax (`\- x`, `\> x`, `\1. x`, `\---`), so an AC line reads as typed.
_CTRL = re.compile(r"^([ \t]*(?:(?:[-*+]|\d+[.)])[ \t]+)?)(?=```|~~~|#)", re.M)
_BANNER_TEXT = "**Thin acceptance criteria**"
THIN_BANNER = "> " + _BANNER_TEXT  # the one quote the kit writes itself (render); the only line ac_lines skips
# story text that would read as the banner (a quoted bold "Thin acceptance criteria") gets `\` before the `**`
_FORGED_BANNER = re.compile(r"^([^\S\n]*> )(?=%s)" % re.escape(_BANNER_TEXT), re.M)  # any leading whitespace, as strip() sees it
_ESCAPED = re.compile(r"^\\(?=```|~~~|#|>|[-*][ \t]|[0-9]{1,9}\.[ \t]|---$|%s)|&lt;!--" % re.escape(_BANNER_TEXT))


def _esc(text):
    text = "\n".join(text.splitlines())  # every str.splitlines break (U+2028, \x0c, \r ...) becomes \n first
    text = _FORGED_BANNER.sub(lambda m: m.group(1) + "\\", text)
    return _CTRL.sub(lambda m: m.group(1) + "\\", text).replace("<!--", "&lt;!--")


def _unescape(line):
    return _ESCAPED.sub(lambda m: "<!--" if m.group(0) != "\\" else "", line)


def _hv(value):
    """A header value: one line (any line break becomes a space) and escaped, so it can never open a comment."""
    return _esc(" ".join(str(value).splitlines()))


def _clean(s, cap):
    """Server text for a header line: control, format (bidi, zero-width), separator and unassigned characters become
    spaces, runs of blanks one space, at most `cap` characters (then `…`). Not a string: empty."""
    if not isinstance(s, str):
        return ""
    s = " ".join("".join(" " if unicodedata.category(c)[0] in "CZ" else c for c in s).split())
    return s[:cap] + "…" if len(s) > cap else s


def _name(f, field):
    v = f.get(field)
    return _clean(v.get("name") if isinstance(v, dict) else None, MAX_NAME)


# --- the fence/comment-aware readers -------------------------------------------------------------------------------------

_SECTION2_RE = re.compile(r"^" + re.escape(SECTION2), re.M)
_FENCE_OPEN = re.compile(r"^ {0,3}(?:(`{3,})[^`]*$|(~{3,}))")


def _scan(md, fill):
    """([md's lines with what renders as code or not at all replaced by `fill` per character],
    whether a fence or comment is still open at the end). Blanked: fenced code (CommonMark: closed
    only by the same character, at least as long, nothing but whitespace after) and <!-- --> comments."""
    out, fence, comment = [], None, False
    for line in md.splitlines():
        if fence:
            if re.fullmatch(r" {0,3}%s{%d,}[ \t]*" % (re.escape(fence[0]), len(fence)), line):
                fence = None
            out.append(fill * len(line))
            continue
        m = None if comment else _FENCE_OPEN.match(line)
        if m:
            fence = m.group(1) or m.group(2)
            out.append(fill * len(line))
            continue
        parts, i, n = [], 0, len(line)  # indexes, never `rest = rest[k:]`: re-copying the rest per comment is quadratic
        while i < n:
            if comment:
                end = line.find("-->", i)
                if end < 0:
                    parts.append(fill * (n - i))
                    break
                parts.append(fill * (end + 3 - i))
                comment, i = False, end + 3
            else:
                start = line.find("<!--", i)
                if start < 0:
                    parts.append(line[i:])
                    break
                parts.append(line[i:start] + fill * 4)
                comment, i = True, start + 4
        out.append("".join(parts))
    return out, bool(fence or comment)


def _visible(md):
    """(md with fences and comments removed; lines stay 1:1 with md.splitlines(), whether one is still open)."""
    out, still_open = _scan(md, "")
    return "\n".join(out), still_open


def _mask(md):
    """md with fences and comments overwritten by spaces: same length as md, so an offset found here
    slices the original. Every line break becomes `\\n` (CRLF is kept), as `re.M` and `_visible` see lines."""
    out, _ = _scan(md, " ")
    return "".join(k + ("\r\n" if raw.endswith("\r\n") else "\n" if raw[len(k):] else "")
                   for k, raw in zip(out, md.splitlines(keepends=True)))


def unclosed(md):
    """True when a fence or comment is left open: everything after it is hidden, so callers fail closed."""
    return _scan(md, "")[1]


def _section2_at(md):
    """Offset of the LAST line-anchored SECTION2 heading outside fences and comments, or -1. Inline
    mentions never count, and a forged heading copied into section 1 loses to the real one rendered after it."""
    last = None
    for last in _SECTION2_RE.finditer(_mask(md)):
        pass
    return last.start() if last else -1


def _repair_at(md):
    """Offset to splice from when `md` has an unclosed opener, else -1. The only splice point is the FIRST raw line-start
    SECTION2 heading, and only when the text before it is unclosed (the opener is in section 1 or the header) and the
    suffix from it is balanced and still has `## 3.`, `## 4.`, `## 5.` in order. Anything else refuses."""
    m = _SECTION2_RE.search(md)
    if not (m and unclosed(md[:m.start()]) and not unclosed(md[m.start():])):
        return -1
    vis, pos = _mask(md[m.start():]), 0
    for n in (3, 4, 5):  # in order, each searched after the last: one lazy `.*?` pattern over them is quadratic
        h = re.compile(r"^## %d\." % n, re.M).search(vis, pos)
        if not h:
            return -1
        pos = h.end()
    return m.start()


def section_span(ledger_md, n):
    """(start, end) offsets of the body of `## <n>.` (up to the next `## ` heading or the end), searched only after the
    last SECTION2 marker, so story text copied into section 1 can never stand in for it. Headings inside fences or
    comments are not headings. None if absent."""
    vis = _mask(ledger_md)
    i = _section2_at(ledger_md)
    m = re.search(r"^## %d\." % n, vis[i:], re.M) if i != -1 else None
    if not m:
        return None
    start = i + m.end()
    nxt = re.search(r"^## ", vis[start:], re.M)
    return start, (start + nxt.start() if nxt else len(ledger_md))


def section(ledger_md, n):
    """Body of `## <n>.` (see section_span; the returned text is the authored original). None if absent."""
    span = section_span(ledger_md, n)
    return ledger_md[span[0]:span[1]] if span else None


_LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])(?:\s+|$)")
_AC_NONE = re.compile(r"^_\(none[^)]*\)_$")
_QUOTE = re.compile(r"^(?:>[ \t]?)+")  # blockquote prefixes, nested ones included


def ac_lines(ledger_md):
    """The AC lines of section 1's `### Acceptance Criteria` block; AC-n is the n-th. Blank lines, the thin-AC banner (only
    as the block's first non-blank line, where render writes it) and a
    `_(none ...)_` placeholder are skipped; quote prefixes and one leading list marker are stripped. Read only before SECTION2."""
    ledger_md = ledger_md.replace("\r\n", "\n").replace("\r", "\n")
    vis, i = _mask(ledger_md), _section2_at(ledger_md)
    m = re.search(r"^### Acceptance Criteria[ \t]*$", vis[:i] if i != -1 else vis, re.M)
    if not m:
        return []
    end = i if i != -1 else len(vis)
    nxt = re.search(r"^##", vis[m.end():end], re.M)
    return _ac_line_list(ledger_md[m.end():m.end() + nxt.start() if nxt else end].splitlines())


_AC_FENCE = re.compile(r"(`{3,}|~{3,})")


def _ac_line_list(lines, banner=True):
    """`banner`: the first non-blank line may be the thin-AC banner (a ledger's AC block); False for canonical AC text. A code block
    (its fence escaped in a ledger) belongs to the criterion before it (D-012): its lines are not criteria of their own; a block with no
    criterion before it counts once, as its opening fence line. A fence-like line with no closing fence is an ordinary line."""
    out, first, close = [], banner, None
    cleaned = []
    for line in lines:
        if first and line.strip():
            first = False
            if line.startswith(THIN_BANNER):  # column 0, no strip: exactly where render writes it
                continue
        line = line.strip()
        line = _QUOTE.sub("", line)  # a quoted criterion is still a criterion: skipping it would fail open
        if line and not _AC_NONE.match(line):
            line = _unescape(_LIST_MARKER.sub("", line))
            if line:
                cleaned.append(line)
    # longest closing fence (a line of only ` or only ~) at or after each line, per character: one backward pass keeps this linear
    later, best = [None] * len(cleaned), {"`": 0, "~": 0}
    for i in range(len(cleaned) - 1, -1, -1):
        later[i] = dict(best)
        x = cleaned[i]
        if len(x) >= 3 and x[0] in best and x == x[0] * len(x):
            best[x[0]] = max(best[x[0]], len(x))
    for i, line in enumerate(cleaned):
        if close is not None:
            if len(line) >= len(close) and line == close[0] * len(line):
                close = None
            continue
        m = _AC_FENCE.match(line)
        if m and later[i][m.group(1)[0]] >= len(m.group(1)):
            close = m.group(1)
            if out:
                continue
        out.append(line)
    return out


def ledger_ac_stats(ledger_md):
    """`ac_stats` for a written ledger: the AC lines are counted once, as `ac_lines` reads them (equal to ac_stats of the
    canonical AC the ledger was rendered from)."""
    n = sum(map(len, ac_lines(ledger_md)))
    return {"ac_chars": n, "ac_thin": n < AC_THIN_CHARS}


def ac_stats(md):
    """AC length (the sum of the AC lines' lengths, markers stripped) and whether it is too thin to decompose."""
    n = sum(map(len, _ac_line_list(md.splitlines(), banner=False)))
    return {"ac_chars": n, "ac_thin": n < AC_THIN_CHARS}


def _header_value(ledger_md, label):
    """The value of `- **<label>:** v`, read only from the header block (above the first `## ` heading), outside fences
    and comments, so story text in section 1 can never stand in for it."""
    vis = _mask(ledger_md)
    h = re.search(r"^## ", vis, re.M)
    m = re.search(r"^- \*\*%s:\*\*(.*)$" % re.escape(label), vis[:h.start()] if h else vis, re.M)
    return m.group(1).strip(" \t") if m else None  # not `[ \t]*(.*?)[ \t]*$`: quadratic over a blank run inside the value


def ledger_ac_hash(ledger_md):
    return _header_value(ledger_md, "AC hash") or None


def ledger_spec_dir(ledger_md):
    v = _header_value(ledger_md, "Spec dir")
    return None if not v or v == SPEC_DIR_UNSET else v


# --- the business-rules header lines (RU1): `- **Rules from:** <KEY>, ...` and `- **Rules reviewed:** BR-<KEY>-<n>@<hash8>, ...` ---

_H2 = re.compile(r"^## ", re.M)


def _header(md):
    """(the header block as lines, whether it can be trusted). The block is what comes before the first line-start `## ` heading
    outside fences and comments; text hidden in them is gone. Untrusted when the first raw line-start `## ` is not that heading (a
    comment or fence hides it: the opener sits in the header, e.g. an old summary that was not escaped, and story text could stand in
    for header lines), or when there is no heading and a fence or comment is still open. The readers below call that invalid."""
    vis, still_open = _visible(md)
    m = _H2.search(vis)
    raw = next((i for i, line in enumerate(md.splitlines()) if line.startswith("## ")), None)
    shown = vis.count("\n", 0, m.start()) if m else None
    return (vis[:m.start()] if m else vis).splitlines(), raw == shown and (bool(m) or not still_open)


def header_block(md):
    """The text above the first `## ` heading outside fences and comments (fences and comments removed). The only place `Rules from:`
    and `Rules reviewed:` are read, so story text in section 1 can never stand in for them."""
    return "".join(line + "\n" for line in _header(md)[0])


_RULES_FROM = re.compile(r"none|{0}(?:, {0})*".format(keys.KEY_PATTERN))
MAX_RULES_FROM = 3  # the parent walk follows at most 3 links (RU2)
# a line that names the label as a header line in any spelling (blockquote prefix, list marker, emphasis, case); no two adjacent
# optional blank runs, so a long blank run cannot backtrack
_LOOK = {label: re.compile(r"\s*(?:>\s*)*(?:(?:[-*+]|[0-9]+[.)])\s*)?(?:[*_]+\s*)?" + label.replace(" ", r"\s+") + r"\b", re.I)
         for label in ("Rules from", "Rules reviewed")}


def _look(label):
    return _LOOK[label]


def _labelled(md, label):
    """(present, value) for the header line `- **<label>:** <value>`. Exact form only (one space, no trailing blanks). Value None =
    malformed: a second line, an untrusted header, or a line that names the label in any other way. A summary or `Spec dir` that
    mentions the label is not a line of that kind (it does not begin with the label)."""
    lines, trusted = _header(md)
    if not trusted:
        return True, None
    hits = [x for x in lines if _look(label).match(x)]
    if not hits:
        return False, ""
    prefix = "- **%s:** " % label
    value = hits[0][len(prefix):]
    return True, value if len(hits) == 1 and hits[0].startswith(prefix) and value and value == value.strip() else None


def _parse_from(value):
    if not _RULES_FROM.fullmatch(value or ""):
        return None
    ids = [] if value == "none" else value.split(", ")
    return ids if len(ids) <= MAX_RULES_FROM and len(set(ids)) == len(ids) else None


def _parse_reviewed(value):
    """{rule id: hash8} for `BR-<KEY>-<n>@<hash8>, ...` or `none`; None when malformed or an id repeats."""
    from .rules import TAG_RE  # rules imports this module at load time: import lazily
    if value == "none":
        return {}
    out = {}
    for tok in (value or "x").split(", "):
        rid, at, h = tok.partition("@")
        if not (at and TAG_RE.fullmatch(rid) and re.fullmatch(r"[0-9a-f]{8}", h)) or rid in out:
            return None
        out[rid] = h
    return out


def ledger_rules_from(md):
    """`{"present", "valid", "ids"}` for `- **Rules from:** LOY-1201, LOY-1200` (nearest first) or `none`, read from the header block
    only. Invalid (and `ids == []`): more than 3 keys, a repeated key, a key that is not `keys.KEY_RE`, a second line, any other spelling
    of the line. Absent is `present False, valid True`."""
    present, value = _labelled(md, "Rules from")
    ids = _parse_from(value) if present else []
    return {"present": present, "valid": ids is not None, "ids": ids or []}


def ledger_rules_reviewed(md):
    """`{"present", "valid", "hashes"}` for `- **Rules reviewed:** BR-LOY-1201-1@3f2a9c1b, ...` (or `none`), read from the header block
    only. The same rules as `ledger_rules_from`; ids match `rules.TAG_RE`, hashes are 8 lower-case hex, an id may appear once."""
    present, value = _labelled(md, "Rules reviewed")
    hashes = _parse_reviewed(value) if present else {}
    return {"present": present, "valid": hashes is not None, "hashes": hashes or {}}


def _reviewed_line(md):
    """`(present, value)` of the `Rules reviewed:` header line; value None when it cannot be trusted (an untrusted header that names
    the label anywhere)."""
    if _header(md)[1]:
        return _labelled(md, "Rules reviewed")
    return any(_look("Rules reviewed").match(x) for x in md.splitlines()), None


# --- render and write ----------------------------------------------------------------------------------------------------

def render(issue, ac_md, site, synced="", spec_dir="", rules_from=None, rules_reviewed=""):
    """The fresh ledger for `issue` (a Jira issue dict as `issues.get` returns it) whose AC is `ac_md` (from `ac_section`).
    `rules_from`: None writes no `Rules from:` line (the baseline); a list of keys writes it (`none` when empty). `rules_reviewed`: the
    value of a `Rules reviewed:` line, written when not empty. Both are checked with the readers' own rules before they reach the header."""
    if spec_dir != " ".join(spec_dir.splitlines()):
        raise ValueError("spec dir must be a single line (it is written into the ledger header)")
    rules_lines = []
    if rules_from is not None:
        if not isinstance(rules_from, (list, tuple)) or any(not isinstance(k, str) for k in rules_from):
            raise ValueError("rules_from must be a list of issue keys")
        value = ", ".join(rules_from) or "none"
        if _parse_from(value) is None:
            raise ValueError("rules_from must hold at most 3 different issue keys")
        rules_lines.append("- **Rules from:** %s" % value)
    if rules_reviewed:
        if _parse_reviewed(rules_reviewed) is None:
            raise ValueError("rules_reviewed must be BR-<KEY>-<n>@<8 hex> entries separated by ', ', each rule once")
        rules_lines.append("- **Rules reviewed:** %s" % rules_reviewed)
    key = keys._need(keys.KEY_RE, issue.get("key"), "issue key")
    f = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    n_ac = ac_stats(ac_md)["ac_chars"]  # counted before escaping: human text only
    ac_block = _esc(ac_md)
    if n_ac < AC_THIN_CHARS:
        ac_block = (THIN_BANNER + " (%d chars). Splitting this into AC-1…AC-n is judgment, not transcription — "
                    "record the assumptions and open questions it forces in section 2, and consider improving the story in "
                    "Jira first.\n\n" % n_ac) + ac_block
    parts = [
        "# DoD — %s: %s" % (key, _hv(_clean(f.get("summary"), MAX_SUMMARY))),
        "",
        "- **Jira:** %s" % _hv("https://%s/browse/%s" % (site, key)),
        "- **Key:** %s" % key,
        "- **Id:** %s" % _hv(issue.get("id")),
        "- **Type / State:** %s / %s" % (_hv(_name(f, "issuetype")), _hv(_name(f, "status"))),
        "- **Last synced:** %s" % _hv(synced),
        "- **AC hash:** %s" % ac_hash(ac_md),
        "- **Spec dir:** %s" % (_hv(spec_dir) or SPEC_DIR_UNSET),
        *rules_lines,
        "",
        "## 1. Seeded from Jira (verbatim: the description's acceptance criteria section, ADF converted to Markdown — "
        "do not edit; refresh via `skj dod refresh %s`)" % key,
        "",
        "### Acceptance Criteria",
        ac_block,
        "",
        SECTION2 + "  (fill before building — per discipline)",
        "- **Approach / chosen option + why:** _(fill in)_",
        "- **Assumptions:** _(fill in)_ Each is also recorded in Decisions_%s.md (`skj decisions %s --add`)." % (key, key),
        "- **Open questions → answers:** _(fill in as they arise)_",
        "  Format, one line each: `- [ ] Q1: <question> → _(open)_ · owner: PO`; answered: `- [x] Q1: <question> → <answer> · owner: PO`",
        "- **Use cases & edge cases:** _(fill in — each: covered-by test / N/A + why)_",
        "",
        "## 3. AC → tests (traceability)  (tag each AC as AC-1, AC-2…)",
        "",
        "Tests are free-form IDs: `Class.method`, `path::test_name` or `describe > it`.",
        "",
        "| AC | acceptance criterion | tests | commit/PR | status |",
        "|---|---|---|---|---|",
        "| AC-1 | _(what it must do)_ | _(test IDs, …)_ | _(commit / PR)_ | _(red→green)_ |",
        "",
        "## 4. Gate & review log  (appended by speckit.jira-lifecycle.gate and speckit.jira-lifecycle.review)",
        "_(none yet)_",
        "",
        "## 5. Definition of Done  (all ✅ to close)",
        "- [ ] every AC mapped to a passing real test",
        "- [ ] coverage meets the project's bar",
        "- [ ] no assertion-free tests",
        "- [ ] all assumptions / questions / edge cases resolved",
        "- [ ] gates green",
        "",
    ]
    return "\n".join(parts)


def repo_root(start="."):
    """The git top level of `start`; ValueError outside a repository (the ledger lives at the top level's dod/)."""
    top = config._out(str(start), "rev-parse", "--show-toplevel")
    if not top:
        raise ValueError("not inside a git repository; the ledger is written to dod/ at the repository's top level")
    return Path(top.decode("utf-8", "surrogateescape").strip())


def ledger_path(root, key):
    """`<root>/dod/DoD_<KEY>.md`, refused when the key is invalid, dod/ or the ledger is a symbolic link, or either is the
    wrong kind of file. The root itself is resolved first, so a symlinked checkout still writes inside the real one."""
    name = keys.dod_name(key)  # ValueError: the key regex keeps the name inside dod/
    d = Path(os.path.realpath(str(root))) / "dod"
    if os.path.islink(str(d)):
        raise UnsafePath("dod/ is a symbolic link; refused (the ledger is written only inside the repository)")
    if os.path.lexists(str(d)) and not d.is_dir():
        raise UnsafePath("dod exists and is not a directory; refused")
    path = d / name
    if os.path.islink(str(path)):
        raise UnsafePath("dod/%s is a symbolic link; refused (the ledger is never written through a link)" % name)
    if os.path.lexists(str(path)) and not path.is_file():
        raise UnsafePath("dod/%s is not a regular file; refused" % name)
    return path


E_NO_LEDGER = "no ledger to refresh at dod/%s: run `skj dod seed %s` first; nothing written"  # D-009
E_UNREADABLE = ("cannot read the DoD ledger (a symbolic link, outside the repository, not a regular file, over 1 MiB or not UTF-8); "
                "refusing to refresh; nothing written")  # D-010: the limit and the reasons of gate and done (`gate._read`)


def _exists_msg(path, key):
    return ("%s exists — run `skj dod refresh %s` to re-pull section 1 (authored sections are kept), "
            "or delete the file to start over." % (path, key))


def _write(path, issue, ac_md, site, synced, refresh, spec_dir, rules_from=None):
    key = issue["key"]
    if os.path.lexists(str(path)) and not refresh:
        raise DodExists(_exists_msg(path, key))
    if refresh and not os.path.lexists(str(path)):
        raise DodExists(E_NO_LEDGER % (path.name, key))
    old = None
    if refresh:
        from . import gate  # lazy: gate imports this module
        old = gate._read(str(path), str(path.parent.parent), set())  # the 1 MiB cap and the checks gate and done read the ledger with
        if old is None:
            raise DodExists(E_UNREADABLE)
        old = old.replace("\r\n", "\n").replace("\r", "\n")  # universal newlines, as the read_text it replaces
    rules_reviewed = ""
    if old is not None:
        spec_dir = spec_dir or ledger_spec_dir(old) or ""
        # written by a human's rules confirm: kept like Spec dir. A header that cannot be trusted (an old ledger's unescaped `<!--` in
        # its summary hid the first heading) cannot be read: with no line naming the label it is rebuilt as before; with one (maybe
        # forged from the description) the refresh refuses. `Rules from:` is never copied: the pull recomputes it (RU2).
        present, kept = _reviewed_line(old)
        if present and _parse_reviewed(kept) is None:
            raise DodExists("%s has a malformed or untrusted 'Rules reviewed:' header line — refusing to refresh (it would drop the "
                            "hashes a human confirmed, or copy a forged line). Fix the header by hand." % path)
        rules_reviewed = kept or ""
    fresh = render(issue, ac_md, site, synced, spec_dir, rules_from=rules_from, rules_reviewed=rules_reviewed)
    if old is not None:
        idx = _repair_at(old) if unclosed(old) else _section2_at(old)
        if unclosed(old) and idx == -1:
            raise DodExists("%s has an unclosed code fence or comment outside section 1 — refusing to refresh "
                            "(it would splice from the wrong place). Fix the file by hand." % path)
        if idx == -1:
            raise DodExists("%s is missing the '%s' marker — refusing to refresh (it would discard authored content). "
                            "Fix the file by hand." % (path, SECTION2))
        fresh = fresh[:_section2_at(fresh)] + old[idx:]  # fresh header and section 1, the old section 2 to the end
    path.parent.mkdir(exist_ok=True)
    path = ledger_path(path.parent.parent, key)  # checked again right before the write
    # write-then-rename so a crash or disk-full mid-write never truncates authored sections; O_EXCL never opens through a link
    tmp = path.with_name(".%s.%s.tmp" % (path.name, os.urandom(4).hex()))
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(fresh)
        os.replace(str(tmp), str(path))
    except BaseException:
        if os.path.lexists(str(tmp)):
            os.unlink(str(tmp))
        raise
    return path


def seed(c, cfg, key, refresh=False, spec_dir="", root=None, synced=None):
    """Write `dod/DoD_<KEY>.md` at the repository's top level from one GET of the story; returns its path. A refresh
    re-renders the header and section 1 and keeps section 2 onward byte for byte. Local write only: no Jira write. With `rules.enabled`
    the parent walk (`rules.walk`, more GETs) writes `Rules from:`; its errors (a cycle, a Jira error) propagate before anything is
    written."""
    path = ledger_path(repo_root() if root is None else root, key)  # refused before any request
    if os.path.lexists(str(path)) and not refresh:
        raise DodExists(_exists_msg(path, key))
    if refresh and not os.path.lexists(str(path)):  # before the request
        raise DodExists(E_NO_LEDGER % (path.name, key))
    data = issues.get(c, key, list(FIELDS))
    f = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    ac_md = ac_section(f.get("description"), cfg.ac_heading)
    from . import rules  # lazy: rules imports this module
    rules_from = [a["key"] for a in rules.walk(c, key, cfg.rules["types"])] if cfg.rules["enabled"] else None
    return _write(path, data, ac_md, cfg.site, date.today().isoformat() if synced is None else synced, refresh, spec_dir, rules_from)
