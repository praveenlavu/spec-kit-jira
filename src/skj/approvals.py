"""Comment approvals, part A1: the trust guard and the command grammar. EXPERIMENTAL and tamper-evident only (owner decision K5/O5:
there is no second human; terminal approval stays the default). Pure functions: no I/O except `excluded`, which reads `/myself`.

The trust rule. A comment (a `comments.Row`) counts as an approver's only when `guard` returns None. Otherwise `guard` returns the
FIRST failing condition, always checked in this order (REASONS), so a comment failing several gets one deterministic code:
  1. `excluded-author`: the author is excluded (the token user from `/myself`, the CI identity, the author of any comment whose
     canonical Markdown starts with a `[skj:` kit marker: `excluded`). First, so the agent's own comment never counts even when its
     accountId is listed in `approvers` (A1-AC3).
  2. `not-approver`: the author is not an accountId string listed in `approvers` (exact, case-sensitive). An empty `approvers` means
     nobody approves. A None author (anonymous, odd user object) never counts. A value that is not a well-formed `comments.Row`
     (a decimal-string id) also gets this code: who wrote it cannot be told.
  3. `app-account`: `author_type` is not exactly "atlassian" (an app or customer account, or none).
  4. `edited`: `created` and `updated` are not two equal well-formed stamps, compared as RAW strings (the same instant written another
     way is `edited`: a safe false positive), or `update_author` is not exactly the author (absent counts as edited).
  5. `restricted`: any `visibility` at all (`comments` maps an odd shape, and a `jsdPublic` that is present and not True, to VIS_ODD).
  6. `unknown-node`: the body failed strict ADF reading (`unknown_nodes` not the empty tuple, `md` not a str, a `[?` placeholder in
     `md`, or a digest that is not 64 lowercase hex). Before rule 7: an unreadable body has no first line to judge.
  7. `quoted`: the first non-empty line of `md` does not start like a command: a command inside a quote, a list, a code block or a
     heading, after other text or a mention, or no command at all. A command anywhere but the first line is never obeyed.
`approvers` and `excluded` must be a list, tuple, set or frozenset of accountId strings (`keys.ACCOUNT_RE`); anything else raises
ValueError (never filtered into an empty list, never read as "anyone"); `excluded` may not be empty (the token user is always in it).
Callers take `approvers` from `config.trusted()` with NO argument (the target branch, X4; `_stricter_trust` for `--base`), never from
the working tree, and run `issues.get` before `comments.fetch` (the comment endpoint cannot tell a moved issue).
Only `guard(...) is None` AND a command from `parse_command(row.md)` make an approval; A2 chooses the newest one per target.

The grammar (the fork's, unchanged): ONLY the first non-empty line of the Markdown counts and it must be the whole command, ASCII only:
`/approve|/reject D<n>[: note]`, `/answer Q<n>: text`, `/approve|/reject <gate-id> <64 lowercase hex>[: note]` (a gate needs the hash,
a D or Q target must not carry one: the hash binds a gate approval to the exact bytes the gate shows, A4's `gate_hash`; a decision's
text is bound by the posting it answers, A2). Lines are split on "\\n" only. A note is cut to 300 characters, an answer to 500
(capped, never refused: a long `/reject` stays a reject), after the scrub (`_note`). A first line that starts like a command in any
disguise (case, full-width forms, invisible characters, `/approved`) but breaks the grammar is a malformed line (`_parse` reports the
word it names, `_elsewhere` tells whether that word is a different well-formed target): reported, never obeyed.
Every reader here is linear in its input. Comment ids are strings; nothing here calls int() on one.

A2 adds the time order and the choice of the newest valid command per target (`board`, `newest`), the kit's own postings
(`postings`: the comments the newest-posting rule of `decisions --sync` / `questions --sync` reads) and the fixed texts of `--sync`.
Times are compared as PARSED instants (`_when`), never as strings; a tie on the same instant is broken by the numeric comment id
(`_order`: compared by length, then text, never int()). A stamp that cannot be parsed is never compared: that comment never counts.
"""
import bisect
import datetime
import hashlib
import os
import re
import shutil
import signal
import subprocess
import unicodedata

from . import adf, comments, decisions, issues, keys
from .client import ClientError
from .stack import _clean

# The one plain-words sentence (A5-AC3; owner decision O5 and the A2 escalation): `doctor`'s `edit-all` row, the strict output and the
# README carry exactly this text, by name, never a copy. Edits are detected (`edited`); a deleted comment is not.
TAMPER_SENTENCE = "On this site the token can edit or delete comments, including its own, so an approval can be shown to have been tampered with only if it was edited; a deleted comment leaves no trace; comment approval is experimental and cannot be made tamper-proof."  # noqa: E501  one unwrapped line (README: verbatim)
KIT_MARKER = "[skj:"  # every comment the kit posts starts with one (questions, decisions, pr, ...)
REASONS = ("excluded-author", "not-approver", "app-account", "edited", "restricted", "unknown-node", "quoted")
MAX_MD = 256 << 10  # characters: a longer Markdown is never read for a command, it is a malformed line (`(None, "")`)
NOTE_CAP, ANSWER_CAP = 300, 500
PIN_RE = re.compile(r"[1-9][0-9]{0,18}:[0-9a-f]{8}\Z")  # `via: <comment id>:<hash8>`, hash8 = first 8 hex of `adf.digest`

_CMD = re.compile(r"/(approve|reject|answer)[ \t]+(D[1-9][0-9]{0,8}|Q[1-9][0-9]{0,8}|[a-z][a-z0-9-]{2,40})(?:[ \t]+([0-9a-f]{64}))?"
                  r"(?:[ \t]*:[ \t]*(.+))?")
_DQ = re.compile(r"[DQ][0-9]+")
_WELL_FORMED = re.compile(r"[DQ][1-9][0-9]*|[a-z][a-z0-9-]{2,40}")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MD_MENTION = re.compile(r"@<[^>\n]{0,8}>")  # how adf_to_md renders a mention node: a cut accountId
_MARKUP = ("<!--", "-->", "```", "~~~")  # would open or close a comment or a fence where the text is written later
_BLANKISH = ("Cf", "Zs", "Zl", "Zp", "Cc")


# ---- exclusion ------------------------------------------------------------------------------------------------------------------

def _account(v, what):
    if type(v) is not str or not keys.ACCOUNT_RE.fullmatch(v):
        raise ValueError("%s is not a Jira accountId; nothing counted" % what)
    return v


def excluded(client, rows, ci_identity=None, me=None):
    """AccountIds that can never count as an approver: the token user (`/myself`; unreadable or not an Atlassian user account raises
    ValueError, never a guess), `ci_identity` when given (not an accountId string: ValueError, never skipped), and the author of every
    Row whose canonical Markdown STARTS with the `[skj:` marker (the kit's posting identities; a quoted or mid-text marker is not a kit
    comment). `rows` is `comments.fetch` output; anything else raises ValueError. A Row whose body cannot be read (unknown nodes,
    md "") shows no marker, so its author is not added from it: the token user and the CI identity are excluded regardless.
    `me` (A2): the token user the caller already read, so one run reads `/myself` once and cannot see two answers."""
    if me is None:
        try:
            me = issues.me(client)["accountId"]
        except ClientError:
            raise ValueError("cannot tell which Jira comments are yours (the identity could not be read); nothing counted") from None
    out = {_account(me, "the token user")}
    if ci_identity is not None:
        out.add(_account(ci_identity, "the CI identity"))
    if not isinstance(rows, (list, tuple)):
        raise ValueError("the comments are not a list of rows; nothing counted")
    for r in rows:
        if type(r) is not comments.Row:
            raise ValueError("the comments are not a list of rows; nothing counted")
        if type(r.md) is str and r.md.startswith(KIT_MARKER) and type(r.author) is str and keys.ACCOUNT_RE.fullmatch(r.author):
            out.add(r.author)
    return out


# ---- the guard --------------------------------------------------------------------------------------------------------------------

def _ids(v, what):
    if type(v) not in (list, tuple, set, frozenset):
        raise ValueError("%s must be a list of Jira accountIds; nothing counted" % what)
    return {_account(a, "an entry of " + what) for a in v}


def guard(row, approvers, excluded):
    """None when comment `row` passes all seven conditions, else the first failing reason code (module docstring, REASONS order).
    ValueError when `approvers` or `excluded` cannot be trusted (see the module docstring). Linear; never raises on an odd row."""
    ok, skip = _ids(approvers, "approvers"), _ids(excluded, "excluded")
    if not skip:
        raise ValueError("excluded is empty: the token user is always excluded; nothing counted")
    if type(row) is not comments.Row or not issues._id_ok(row.id) or type(row.id) is not str:
        return "not-approver"
    author = row.author if type(row.author) is str and keys.ACCOUNT_RE.fullmatch(row.author) else None
    if author in skip:
        return "excluded-author"
    if author is None or author not in ok:
        return "not-approver"
    if type(row.author_type) is not str or row.author_type != "atlassian":
        return "app-account"
    stamps = row.created, row.updated
    if not (all(type(s) is str and comments._STAMP_RE.fullmatch(s) for s in stamps) and row.created == row.updated
            and type(row.update_author) is str and row.update_author == author):
        return "edited"
    if row.visibility is not None:
        return "restricted"
    md = row.md
    if (type(row.unknown_nodes) is not tuple or row.unknown_nodes or type(md) is not str or "[?" in md
            or type(row.digest) is not str or not _HEX64.fullmatch(row.digest)):
        return "unknown-node"
    line = _first_line(md)
    if line is None or not _looks_like_command(line):
        return "quoted"
    return None


def pin(row):
    """`<comment id>:<hash8>` for a Row (hash8 = the first 8 hex of its `adf.digest`); ValueError for a Row without a valid id or digest."""
    if type(row) is not comments.Row or type(row.id) is not str or not issues._id_ok(row.id) or type(row.digest) is not str \
            or not _HEX64.fullmatch(row.digest):
        raise ValueError("this comment cannot be pinned (no valid id or digest)")
    return "%s:%s" % (row.id, row.digest[:8])


# ---- the grammar ------------------------------------------------------------------------------------------------------------------

def _first_line(md):
    """The first line of `md` (split on "\\n" only) that is not empty once ASCII blanks are stripped, stripped; None when there is none."""
    i, n = 0, len(md)
    while i <= n:
        j = md.find("\n", i)
        j = n if j < 0 else j
        line = md[i:j].strip(" \t")
        if line:
            return line
        i = j + 1
    return None


def _looks_like_command(line):
    """Does the line start like a command, however disguised: any case, full-width forms, invisible characters before it, `/approved`?"""
    head = "".join(c for c in line[:40] if unicodedata.category(c) != "Cf")
    return unicodedata.normalize("NFKC", head).lstrip().casefold().startswith(("/approve", "/reject", "/answer"))


def _unmark(s):
    """`s` without `<!--`, `-->`, ``` and ~~~, also those a removal brings together, in one pass (a stack: the kept text never ends in one)."""
    if not any(m in s for m in _MARKUP):  # a removal only ever starts at a whole one
        return s
    out = []
    for ch in s:
        out.append(ch)
        for m in _MARKUP:
            if ch == m[-1] and len(out) >= len(m) and "".join(out[-len(m):]) == m:
                del out[-len(m):]
                break
    return "".join(out)


def _note(raw):
    """Note or answer text made inert, linear: control characters (but tab and line breaks) and invisible ones are removed and
    separator characters become blanks FIRST (so they cannot shield an address from the scrub), then comment and fence markup, then
    mentions (`@<id>`, `[~accountid:...]`, `@name`) become `@user` and addresses `[email]`, then blank runs become one space."""
    t = _unmark(_clean(adf.safe_text(raw), cap=len(raw), markup=False))  # the shared cleaner first (D-011)
    return " ".join(decisions._scrub(_MD_MENTION.sub("@user", t)).split())


def _parse(md):
    """(command or None, bad): `bad` is None, or for a first line that looks like a command (or a Markdown too long to read) but breaks
    the grammar, the word it names as its target, as typed ("" when there is none): the caller decides whether that line is about its
    target (`_elsewhere`). A first line that only LOOKS empty (invisible or odd blank characters) is a malformed line too."""
    if type(md) is not str:
        return None, None
    if len(md) > MAX_MD:
        return None, ""
    line = _first_line(md)
    if line is None:
        return None, None
    if all(unicodedata.category(c) in _BLANKISH for c in line):
        return None, ""
    if not _looks_like_command(line):
        return None, None
    words = line.split(None, 2)
    named = words[1].split(":")[0] if len(words) > 1 else ""
    m = _CMD.fullmatch(line)
    if not m:
        return None, named
    verb, target, hash_, note = m.groups()
    if verb == "answer":
        ok = target[0] == "Q" and hash_ is None and note is not None
    else:
        ok = (target[0] == "D" and hash_ is None) if _DQ.fullmatch(target) else hash_ is not None
    if not ok:
        return None, named
    text = "" if note is None else _note(note)[:ANSWER_CAP if verb == "answer" else NOTE_CAP]
    if note is not None and not text:
        return None, named
    return {"verb": verb, "target": target, "hash": hash_, "text": text}, None


def parse_command(md):
    """The command in a comment's canonical Markdown (`Row.md`), or None: a dict `{"verb", "target", "hash", "text"}` (module docstring).
    No error is ever raised for hostile input."""
    return _parse(md)[0]


def _elsewhere(word, target):
    """Is the target word of a malformed line (up to a colon) a DIFFERENT well-formed target? Anything else (punctuation: `D3.`, `(D3)`,
    `D3,`; `D03`; full-width digits; a typo; nothing) counts as possibly about `target`: a retraction typed wrong must be reported, never
    assumed to be elsewhere."""
    w = word.split(":")[0]
    return w != target and _WELL_FORMED.fullmatch(w) is not None


# ---- A2: time order, the newest valid command per target, the kit's own postings ------------------------------------------------

E_NOT_COMMENT_MODE = 'terminal mode (`approval` is not "comment" on the target branch): nothing to sync'
E_NO_DECISIONS_FILE = "there is no decisions file for this story: nothing to sync"
E_SYNC_TERMINAL = ('--sync is refused: the target branch does not set approval "comment", so a verdict or an answer is recorded by a '
                   "human: `skj decisions <KEY> --resolve` when the target branch names no approvers, else by editing the line in a "
                   "reviewed commit (or set approval \"comment\" there); --sync --if-comment-mode is the no-op; nothing was read or sent")
E_SYNC_NO_APPROVERS = ('--sync is refused: the target branch sets approval "comment" but names no approvers, so no comment can count; '
                       "nothing was read or sent")

_WHEN = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?"
                   r"(?:(Z)|([+-])([0-9]{2}):?([0-9]{2}))\Z")
_UTC = datetime.timezone.utc
TOP = (datetime.datetime.max.replace(tzinfo=_UTC), 99, "")  # later than every `_order` key: an own comment whose time cannot be read


def _when(s):
    """A comment stamp as an aware UTC datetime, else None (never compared: fail closed). The shapes `comments._STAMP_RE` lets through:
    Jira's `2026-01-02T03:04:05.678+0000`, `+00:00`, `Z`, 0 to 9 fraction digits. shortcut: fraction digits past the 6th are cut (two
    stamps that differ below a microsecond tie, and the comment id decides; Jira writes milliseconds), widen if that ever matters."""
    m = _WHEN.fullmatch(s) if type(s) is str else None
    if not m or int(m.group(11) or 0) > 59:
        return None
    y, mo, d, h, mi, sec = (int(x) for x in m.groups()[:6])
    frac = int((m.group(7) or "0")[:6].ljust(6, "0"))
    try:
        off = datetime.timedelta(0) if m.group(8) else datetime.timedelta(hours=int(m.group(10)), minutes=int(m.group(11)))
        return datetime.datetime(y, mo, d, h, mi, sec, frac, tzinfo=datetime.timezone(-off if m.group(9) == "-" else off)).astimezone(_UTC)
    except (ValueError, OverflowError):
        return None


def _order(row):
    """(created instant, id length, id) of a Row: the time, then the numeric id (no leading zero, so length then text is the number's
    order, without int()), or None when the time or the id cannot be read."""
    if type(row) is not comments.Row or type(row.id) is not str or not issues._id_ok(row.id):
        return None
    when = _when(row.created)
    return None if when is None else (when, len(row.id), row.id)


def unedited(row):
    """created == updated (raw strings) and updateAuthor == author (not None): the `edited` rule of `guard`, for a posting."""
    return (type(row.created) is str and row.created == row.updated and type(row.author) is str and row.update_author == row.author)


_LOOKALIKE = str.maketrans({"а": "a", "ɑ": "a", "α": "a", "р": "p", "ρ": "p", "о": "o", "ο": "o", "ս": "u", "е": "e", "ε": "e",
                            "ѵ": "v", "ν": "v", "г": "r", "ј": "j", "с": "c", "ϲ": "c", "т": "t", "τ": "t", "п": "n", "ո": "n",
                            "ѕ": "s", "ԝ": "w", "ω": "w", "і": "i", "ι": "i", "∕": "/", "⁄": "/", "⧸": "/", "╱": "/"})


def _lookalike(md):
    """Does the first line start like a command once common Cyrillic/Greek look-alike letters and slashes are read as ASCII (`/аpprove`)?
    Such a line is never a command (`_parse`) but, by an approver, must be counted in `bad_newer` (A1 must-enforce 6).
    shortcut: a small fixed table, not the Unicode confusables data; widen it if another script is seen."""
    line = _first_line(md) if type(md) is str else None
    if line is None:
        return False
    head = "".join(c for c in line[:40] if unicodedata.category(c) != "Cf")
    return unicodedata.normalize("NFKC", head).casefold().translate(_LOOKALIKE).lstrip().startswith(("/approve", "/reject", "/answer"))


class Board:
    """Every comment parsed ONCE for a run (the fork's `Rows`/`cached`): commands by target, newest first, and the malformed lines by
    approvers. Built by `board`; read by `newest`."""

    def __init__(self, ok, skip):
        self.ok, self.skip = frozenset(ok), frozenset(skip)
        self.cmds, self.undated = {}, {}  # target -> [(key, cmd, row)] newest first; target -> [row] whose time cannot be read
        self.bad_any, self.bad_at = [], {}  # sorted keys of bad lines about any target / about one well-formed target
        self.bad_any_undated, self.bad_at_undated = 0, {}
        self.tamper_any, self.tamper_at = None, {}  # newest key of an approver comment that may have been tampered with (`board`)


def board(rows, approvers, excluded):
    """The Board of `rows` (`comments.fetch` output: a list or tuple of Rows; anything else raises ValueError, never filtered). A bad line
    (`_parse`'s `bad`, or a look-alike command) counts only when its author is an approver and not excluded; it is filed under the word
    it names when that word is a well-formed target (`_elsewhere`: it is about that target only), else under every target."""
    ok, skip = _ids(approvers, "approvers"), _ids(excluded, "excluded")
    if not skip:
        raise ValueError("excluded is empty: the token user is always excluded; nothing counted")
    if not isinstance(rows, (list, tuple)):
        raise ValueError("the comments are not a list of rows; nothing counted")
    b = Board(ok, skip)
    for r in rows:
        if type(r) is not comments.Row:
            raise ValueError("the comments are not a list of rows; nothing counted")
        cmd, bad = _parse(r.md)
        if cmd is None and bad is None and _lookalike(r.md):
            bad = ""
        key = _order(r)
        if bad is not None and type(r.author) is str and r.author in b.ok and r.author not in b.skip:
            word = bad.split(":")[0]
            if _WELL_FORMED.fullmatch(word):
                if key is None:
                    b.bad_at_undated[word] = b.bad_at_undated.get(word, 0) + 1
                else:
                    b.bad_at.setdefault(word, []).append(key)
            elif key is None:
                b.bad_any_undated += 1
            else:
                b.bad_any.append(key)
        if type(r.author) is str and r.author in b.ok and r.author not in b.skip:
            # Fix round 1: what someone ELSE can do to an approver's comment (edit it, restrict it, make it unreadable) must not
            # silently bring back an older verdict: edited by another account, or edited/unreadable so its command cannot be read,
            # counts for every target; an edited or restricted command for a target counts for that target. An undated one is newest.
            edited = not unedited(r)
            k = key or TOP
            if edited and (r.update_author != r.author or r.unknown_nodes or not r.md) or (r.visibility is not None and r.unknown_nodes):
                b.tamper_any = max(b.tamper_any or k, k)
            elif cmd is not None and (edited or r.visibility is not None):
                b.tamper_at[cmd["target"]] = max(b.tamper_at.get(cmd["target"], k), k)
        if cmd is not None:
            if key is None:
                b.undated.setdefault(cmd["target"], []).append(r)
            else:
                b.cmds.setdefault(cmd["target"], []).append((key, cmd, r))
    for v in b.cmds.values():
        v.sort(key=lambda t: t[0], reverse=True)
    b.bad_any.sort()
    for v in b.bad_at.values():
        v.sort()
    return b


def newest(b, target, tally=None, ignored=None):
    """The newest VALID command for `target` in Board `b`, by (created instant, numeric id), as `{"verb", "target", "hash", "text",
    "id", "key", "pin", "bad_newer"}`, else None. Valid = the grammar holds and `guard` returns None. An invalid comment is skipped, so a
    later invalid one (the agent's, an edited, restricted or quoted one) never cancels an earlier valid one, while a later VALID `/reject`
    is the newest and does. Every command for `target` that was skipped on the way (newer than the winner, or all of them without one)
    gets its reason in `ignored` ({comment id: code}; `date` for a time that cannot be read) and is counted in `tally`.
    `bad_newer`: the bad lines by approvers about `target` newer than the winner (all of them without one, and any whose time cannot be
    read); the caller must show it. `tampered` (Fix round 1): an approver's comment NEWER than the winner was edited, restricted or made
    unreadable in a way that could hide a retraction (`board`); the caller must then apply nothing for `target` (`newer-tampered`).
    Counts and ids only: no comment text, no accountId."""
    tally = {} if tally is None else tally
    ignored = {} if ignored is None else ignored

    def count(k, n=1):
        tally[k] = tally.get(k, 0) + n
    for r in b.undated.get(target, ()):
        count("date")
        ignored[r.id] = "date"
    out = None
    for key, cmd, r in b.cmds.get(target, ()):
        why = guard(r, b.ok, b.skip)
        if why is None:
            out = dict(cmd, id=r.id, key=key, pin=pin(r))
            break
        count(why)
        ignored[r.id] = why
    newer = b.bad_any_undated + b.bad_at_undated.get(target, 0)
    for keys_ in (b.bad_any, b.bad_at.get(target, ())):
        newer += len(keys_) - (bisect.bisect_right(keys_, out["key"]) if out else 0)
    if newer:
        count("bad_newer", newer)
    if out is not None:
        out["bad_newer"] = newer
        out["tampered"] = any(t is not None and t > out["key"] for t in (b.tamper_any, b.tamper_at.get(target)))
    return out


class Postings:
    """The kit's own postings of one kind (`postings`): `best` {target: (key, row, ((part, text), ...))} the newest own posting that lists
    each target; `items` {comment id: ((target, part, text), ...) or None (not the kit layout)} for every own marker comment; `opaque`
    the newest key of an own comment that could be a posting but cannot be read (TOP when its time cannot be read), or None; `foreign`
    the ids of marker comments by another account (ignored: a marker never counts unless the kit's own account wrote it)."""

    def __init__(self):
        self.best, self.items, self.opaque, self.foreign = {}, {}, None, []


def postings(rows, me, marker, prefix, read_items):
    """The `Postings` of `rows` (Rows) for the token user `me`: a comment is the kit's when its author is exactly `me` (an Atlassian
    account) and the first line of its canonical Markdown is `marker` (`[skj:<kind> <hash8>]`) whose hash8 is that of the Markdown after
    it (`questions._hash8`), and `read_items(rest)` reads the layout ([(target, part, text)] or None). Edited and restricted own postings
    are indexed like any other (the caller refuses them as THE posting; as a newer one they still supersede). An own comment that starts
    with `prefix` but is not that, an own comment the strict reader rejected (unknown nodes: it may hide a marker), or one whose time
    cannot be read, is `opaque`: fail closed, it could be a newer posting of another text. One pass, each comment read once."""
    from .questions import _hash8  # here: questions imports this module's callers
    out = Postings()
    if type(me) is not str or not keys.ACCOUNT_RE.fullmatch(me):
        raise ValueError("the token user is not a Jira accountId; nothing counted")
    for r in rows:
        if type(r) is not comments.Row:
            raise ValueError("the comments are not a list of rows; nothing counted")
        md = r.md if type(r.md) is str else ""
        first = md.split("\n", 1)[0]
        m = marker.match(first)
        if r.author != me:
            if m:
                out.foreign.append(r.id)
            continue
        key = _order(r)
        if r.author_type != "atlassian" or r.unknown_nodes or not md:  # the token user's id with an odd account type: fail closed
            out.opaque = max(out.opaque or key or TOP, key or TOP)
            continue
        if not first.startswith(prefix):
            continue
        rest = md.split("\n\n", 1)[1] if "\n\n" in md else None
        items = read_items(rest) if m and rest is not None and _hash8(rest) == m.group(1) else None
        out.items[r.id] = None if items is None else tuple(items)
        if items is None or key is None:
            out.opaque = max(out.opaque or key or TOP, key or TOP)
            continue
        shown = {}
        for target, part, text in items:
            shown.setdefault(target, []).append((part, text))
        for target, lines in shown.items():
            if target not in out.best or key > out.best[target][0]:
                out.best[target] = (key, r, tuple(lines))
    return out


# ---- A4: gate hashes, `require` at the write verbs, `approvals check`, the poller --------------------------------------------------
# Comment approval stays EXPERIMENTAL and tamper-evident only (O5). Terminal mode (`approval: "terminal"` on the TARGET branch, the
# default) makes `require` and `check` no-ops that read nothing from Jira and build no client; anything else is comment mode (fail closed).
# The trust keys always come from the target branch (`config.trusted()` with no argument, or `_stricter_trust` for `pr create`).

GATES = ("review-spec", "approve-questions", "approve-plan-questions", "wait-po-answers", "approve-answers-questions", "review-plan",
         "approve-push", "approve-decisions", "approve-sync", "review-review", "approve-pr", "wait-po-validation")  # the fork's 12, fixed
REVIEW_GATES = ("review-spec", "review-plan", "review-review")  # approved by the hash of their artifacts at HEAD (`artifacts`)
WAIT_GATES = ("wait-po-answers", "wait-po-validation")  # met by what Jira shows, no hash (`wait_state`)
VERB_GATES = {"questions-post": ("approve-answers-questions", "approve-plan-questions", "approve-questions"),
              "push": ("approve-push", "approve-sync"), "decisions-post": ("approve-decisions",),
              "pr-create": ("approve-pr",)}  # the ONLY applies a comment can authorise; each verb's gates are ONE target
APPLY_FILES = {"approve-questions": ".skj/plan-questions.md", "approve-plan-questions": ".skj/plan-questions-after-plan.md",
               "approve-answers-questions": ".skj/plan-questions-after-answers.md", "approve-push": ".skj/plan-push.md",
               "approve-decisions": ".skj/plan-decisions.md", "approve-sync": ".skj/plan-sync.md",
               "approve-pr": ".skj/plan-pr.md"}  # the `--plan-out` / `show_file` of each apply gate; F1's workflow must use these
GATE_VERB = {g: v for v, gs in VERB_GATES.items() for g in gs}
_DOMAIN = b"spec-kit-jira/1\n"

E_MISSING = ("comment approval: no valid approval for this plan on story {key}. A trusted approver must comment `/approve <gate> <hash>` "
             "(first line, unedited) with one of: {gates}.{bad}")
E_UNREADABLE = ("comment approval: the story's comments could not be read completely (a Jira error, a moved issue, another project, or a "
                "list that is not what was read), so nothing is approved.")
E_NO_APPROVERS = 'comment approval: the target branch does not set approval "terminal" and names no approvers, so nothing can be approved.'
E_SELF_ONLY = ("comment approval: every approver on the target branch is the token's own account (or the author of the kit's own "
               "comments), whose comments never count, so this can never be approved: name another person's accountId in `approvers` on "
               "the target branch (`skj doctor` shows it in the approvers row).")  # D-019: not the waiting text
E_IDENTITY = "comment approval: the token user cannot be established, so the comments cannot be attributed and nothing is approved."
E_TAMPERED = "an approver's newer comment was edited, restricted or made unreadable: check the thread."
E_GATE = "unknown gate: --gate must be one of the 12 gates of the jira-lifecycle workflow (%s)" % ", ".join(GATES)
E_APPLY_GATE = "gate {gate} is not checked here: the apply it guards (`--apply`) requires the approver comment itself"
E_ARTIFACT = ("comment approval: an artifact this gate shows cannot be read at HEAD (missing, a symlink, over 1 MiB, not UTF-8, a spec "
              "dir outside the repository, an unreadable ledger, or no commit), so there is nothing to approve")
E_UNCOMMITTED = ("comment approval: a file this gate shows (the ledger, spec.md or plan.md) has uncommitted changes, and approvals judge "
                 "the files as committed at HEAD: commit them first, then the approval is asked for the hash of what was committed")
E_REVIEW = ("comment approval: no valid approval for gate {gate} on story {key}. A trusted approver must comment `/approve {gate} {hash}` "
            "(first line, unedited).{bad}")
E_WAIT = "comment approval: gate {gate} on story {key} is not met: {n} open ({ids}).{bad}"
E_POSTINGS = ("comment approval: an own comment of the kit on the story cannot be read (it could be a newer posting), so no wait gate "
              "is met")
E_DECISIONS_FILE = ("comment approval: the story's decisions file exists but cannot be read (a symlink, over 1 MiB, outside the "
                    "repository or not UTF-8), so nothing is met")


class ApprovalMissing(Exception):
    """A comment-approval refusal with a fixed text (never Jira text, never an accountId): exit 2, nothing written. Not a ValueError, so
    no verb's own `except ValueError` turns it into something else."""


def gate_hash(gate_id, key, data):
    """What an approver's `/approve <gate> <hash>` names: sha256 over `spec-kit-jira/1`, the gate id, the story KEY (a string, never the
    numeric id) and the exact bytes, newline-separated (64 lowercase hex). The gate id and the key cannot hold a newline, so the framing
    is unambiguous. ValueError for a gate outside GATES, a key that is not a valid issue key, or data that is not bytes."""
    if gate_id not in GATES or type(key) is not str or not keys.valid_key(key) or type(data) is not bytes:
        raise ValueError("gate_hash needs a gate of the workflow, an issue key and bytes")
    return hashlib.sha256(_DOMAIN + gate_id.encode("ascii") + b"\n" + key.encode("ascii") + b"\n" + data).hexdigest()


def _bad(n, what="for this plan"):
    return " %d newer malformed command line(s) by an approver %s: check the thread." % (n, what) if n else ""


def _terminal(trust):
    return getattr(trust, "approval", None) == "terminal"


E_CI_NO_TARGET = ("comment approval: this is CI and no target branch could be read (origin/HEAD is not set), so whether approval is "
                  "terminal cannot be told; nothing is met or resumed: set origin/HEAD (fetch-depth: 0)")


def _ci_target_known(trust):
    """In CI, a trust read from no target branch (`base_known` False: the defaults) must not make a gate a no-op (fail closed, as the
    site pin's `refused-ci-no-base`); locally the defaults stand, unchanged."""
    from . import config
    if config.in_ci() and not getattr(trust, "base_known", False):
        raise ApprovalMissing(E_CI_NO_TARGET)


def _approvers(trust):
    if not getattr(trust, "approvers", None):
        raise ApprovalMissing(E_NO_APPROVERS)
    return list(trust.approvers)


def _reads(c, cfg, key):
    """(me, every comment, excluded): `questions._story` (the issue and its project, the identity, the comments and the probe past the
    end), then `excluded` (the token user, every `[skj:` author). GETs only. ApprovalMissing with a fixed text when anything fails."""
    from . import questions
    try:
        me, rows = questions._story(c, cfg, key, "nothing approved", E_UNREADABLE)
    except ClientError:
        raise ApprovalMissing(E_UNREADABLE) from None
    except ValueError:  # `questions._me`: the identity
        raise ApprovalMissing(E_IDENTITY) from None
    try:
        return me, rows, excluded(c, rows, me=me)
    except ValueError:
        raise ApprovalMissing(E_IDENTITY) from None


def _newer(keys_, cut):
    return len(keys_) - (bisect.bisect_right(keys_, cut) if cut is not None else 0)


def _verdict(b, hashes):
    """(verdict or None, bad, tampered) of the gates `hashes` ({gate: hash}) in Board `b`, ONE target: the newest valid command over all
    of them (`newest` per gate, then the latest by (instant, id)) must be `/approve` or `/reject` with THAT gate's hash (a command naming
    another hash still counts as the newest command, so another plan's approval or a later reject cancels). Then nothing newer may be
    pending on any gate of the set: no malformed or look-alike approver line, no command by an approver whose time cannot be read, and no
    approver comment edited, restricted or made unreadable (`newer-tampered`). `bad` counts the pending lines. Pure, linear."""
    wins = [w for w in (newest(b, g) for g in hashes) if w is not None]
    best = max(wins, key=lambda w: w["key"]) if wins else None
    cut = best["key"] if best else None
    bad = _newer(b.bad_any, cut) + b.bad_any_undated
    tampered = cut is not None and b.tamper_any is not None and b.tamper_any > cut
    for g in hashes:
        bad += _newer(b.bad_at.get(g, ()), cut) + b.bad_at_undated.get(g, 0)
        bad += sum(1 for r in b.undated.get(g, ()) if r.author in b.ok and r.author not in b.skip)
        t = b.tamper_at.get(g)
        tampered = tampered or (cut is not None and t is not None and t > cut)
    ok = best is not None and best["hash"] == hashes[best["target"]] and not bad and not tampered
    return (best["verb"] if ok else None), bad, tampered


def _approve(c, trust, cfg, key, hashes, message):
    me, rows, skip = _reads(c, cfg, key)
    if all(a in skip for a in trust.approvers):  # D-019: nobody left who could ever approve
        raise ApprovalMissing(E_SELF_ONLY)
    verdict, bad, tampered = _verdict(board(rows, trust.approvers, skip), hashes)
    if verdict != "approve":
        raise ApprovalMissing(message(bad) + (" " + E_TAMPERED if tampered else ""))


def require(c, trust, cfg, key, verb, plan_bytes):
    """The gate in front of a Jira or PR write (`--apply`) in comment mode, after the X2 gate and before the first write; a no-op that
    reads nothing in terminal mode. The verb's gates (`VERB_GATES[verb]`) are ONE target for this plan: a trusted approver's newest valid
    command over them must be `/approve <gate> <gate_hash(gate, key, plan_bytes)>` (`_verdict`). `plan_bytes` are the canonical plan
    bytes the X2 gate writes to --plan-out and compares with --expect-plan. ApprovalMissing (fixed text, hashes and counts only)
    otherwise; GETs only. `trust` is the TARGET branch's."""
    if _terminal(trust):
        return None
    _approvers(trust)
    hashes = {g: gate_hash(g, key, plan_bytes) for g in VERB_GATES[verb]}
    _approve(c, trust, cfg, key, hashes, lambda bad: E_MISSING.format(
        key=key, gates=", ".join("%s %s" % (g, h) for g, h in hashes.items()), bad=_bad(bad)))


# ---- the artifacts a review gate shows, read at HEAD --------------------------------------------------------------------------

MAX_ARTIFACT = 1 << 20


def _blob(root, head, path):
    from . import config
    try:
        raw = config._blob_at(str(root), head, path, MAX_ARTIFACT, "an artifact")
        if raw is None:
            raise ApprovalMissing(E_ARTIFACT)
        return raw.decode("utf-8")
    except (config.ConfigInvalid, UnicodeDecodeError, ValueError):
        raise ApprovalMissing(E_ARTIFACT) from None


def artifacts(gate_id, key, root):
    """The bytes a review gate shows its approver, for `gate_hash`: the listed texts read from the COMMIT at HEAD (`config._blob_at`: a
    regular file of at most 1 MiB, never a symlink, never the working tree), each framed `<label> <byte count>\\n<bytes>\\n` (so no
    content can pass for a frame). `review-spec` = spec.md + ledger section 2; `review-plan` = plan.md + spec.md + ledger section 2;
    `review-review` = ledger section 4 (the whole log: every review and fidelity block) + HEAD's commit id. spec.md and plan.md sit in
    the ledger's `Spec dir` (`prs._spec_rel`: relative, no `..`, not dod/). ApprovalMissing(E_ARTIFACT) for anything unreadable,
    E_UNCOMMITTED while the working tree holds an uncommitted change to a file it shows (DF7 D-027)."""
    from . import config, dod, prs
    if gate_id not in REVIEW_GATES:
        raise ApprovalMissing(E_ARTIFACT)
    head = config._rev(str(root), "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ApprovalMissing(E_ARTIFACT)
    ledger = _blob(root, head, "dod/" + keys.dod_name(key))
    if dod.unclosed(ledger):
        raise ApprovalMissing(E_ARTIFACT)

    def section(n):
        text = dod.section(ledger, n)
        if text is None:
            raise ApprovalMissing(E_ARTIFACT)
        return text

    def spec(name):
        rel = prs._spec_rel(dod.ledger_spec_dir(ledger), "")
        if rel is None:
            raise ApprovalMissing(E_ARTIFACT)
        return _blob(root, head, rel + "/" + name)
    if gate_id == "review-review":
        parts = [("ledger-4", section(4)), ("head", head)]
    else:
        parts = ([("plan.md", spec("plan.md"))] if gate_id == "review-plan" else []) + [("spec.md", spec("spec.md")), ("ledger-2", section(2))]
    # DF7 D-027: the workflow's own applies write the ledger after the agent's commit; a hash of HEAD would approve a stale file
    shown = ["dod/" + keys.dod_name(key)] + [prs._spec_rel(dod.ledger_spec_dir(ledger), "") + "/" + n for n, _ in parts if n.endswith(".md")]
    dirty = config._out(str(root), "status", "--porcelain", "--untracked-files=no", "--", *shown)
    if dirty is None:
        raise ApprovalMissing(E_ARTIFACT)
    if dirty.strip():
        raise ApprovalMissing(E_UNCOMMITTED)
    out = []
    for label, text in parts:
        b = text.encode("utf-8")
        out.append(b"%s %d\n%s\n" % (label.encode("ascii"), len(b), b))
    return b"".join(out)


# ---- the wait gates: met by what Jira shows -------------------------------------------------------------------------------------

def _cut_item(text, cap):
    return len(text) >= cap and text.endswith("…")


def wait_state(rows, me, gate_id, approvers, skip, decisions_md=None):
    """{"met", "open": [ids], "bad_newer": n} of a wait gate, from Jira's comments only (the ledger and the decisions file never MEET a
    gate: the agent edits them). The ids are those the kit's OWN postings (`postings`, token user, marker hash) showed: for
    `wait-po-answers` every Q<n> any own questions posting listed OPEN (a later posting that shows it assumed, or not at all, does not
    close it); for `wait-po-validation` every D<n> of an own decisions posting. Each stays open unless the newest own posting that lists
    it is unedited, unrestricted and not cut, and the approver's newest valid command (`/answer Q<n>` resp. `/approve|/reject D<n>`) is
    NEWER than it, with no newer malformed line and nothing tampered. An own comment that cannot be read refuses (E_POSTINGS: it could be
    a newer posting). `decisions_md` (the working tree's decisions file, optional) can only keep a decision open: a posted line that no
    longer is what its newest posting shows (`decisions.superseded`). Pure; linear."""
    from . import decisions, questions
    if gate_id == "wait-po-answers":
        index = postings(rows, me, questions._MARKER_RE, "[skj:questions", questions._posted_qs)
        ids = {t for items in index.items.values() if items for t, part, _ in items if part == "open"}
        cap, verbs = questions.MAX_LINE, ("answer",)
    else:
        index = decisions.posting_index(rows, me)
        ids = set(index.best)
        cap, verbs = decisions.MAX_COMMENT_LINE, ("approve", "reject")
    if index.opaque is not None:
        raise ApprovalMissing(E_POSTINGS)
    b = board(rows, approvers, skip)
    still, bad = [], 0
    for i in sorted(ids, key=lambda x: (len(x), x)):
        pkey, r, shown = index.best[i]
        tally = {}
        win = newest(b, i, tally=tally)
        if (not unedited(r) or r.visibility is not None or any(_cut_item(t, cap) for _, t in shown)
                or not (win and win["verb"] in verbs and not win["bad_newer"] and not win["tampered"] and win["key"] > pkey)):
            still.append(i)
            bad += tally.get("bad_newer", 0)
    if decisions_md is not None and gate_id == "wait-po-validation":
        for d in (d for k in ("open", "validated", "rejected") for d in decisions.parse_decisions(decisions_md)[k]):
            if d["id"] in index.best and d["id"] not in still and decisions.superseded(d, index):
                still.append(d["id"])
        still.sort(key=lambda x: (len(x), x))
    return {"met": not still, "open": still, "bad_newer": bad}


def _decisions_md(root, key):
    """The working tree's decisions file (confined as the gate reads it), None when there is none; unreadable: ApprovalMissing."""
    from . import decisions, gate
    try:
        path = str(decisions.decisions_file(root, key))
    except ValueError:  # dod/ or the file is a link or the wrong kind of file
        raise ApprovalMissing(E_DECISIONS_FILE) from None
    if not os.path.lexists(path):
        return None
    md = gate._read(path, str(root), set())
    if md is None:
        raise ApprovalMissing(E_DECISIONS_FILE)
    return md


def _wait(client_factory, trust, cfg, key, gate_id, root):
    md = _decisions_md(root, key) if gate_id == "wait-po-validation" else None  # local, before any client or request
    me, rows, skip = _reads(client_factory(), cfg, key)
    return wait_state(rows, me, gate_id, trust.approvers, skip, md)


def check(client_factory, trust, cfg, key, gate_id, root):
    """`skj approvals check KEY --gate G`: {"key", "gate", "mode", "met": True} when met, else ApprovalMissing (fixed text). An unknown
    gate is refused first. Terminal mode: a no-op, no client is built and nothing is read from Jira. Comment mode: approvers must be
    named; a review gate needs the newest valid approver command to be `/approve <gate> <hash of artifacts() at HEAD>` (the artifacts
    are read before any client is built); a wait gate needs `wait_state` met; the seven apply gates are checked by their own `--apply`
    (`require`) and refused here. Read-only: GETs only, nothing written anywhere."""
    if gate_id not in GATES:
        raise ApprovalMissing(E_GATE)
    _ci_target_known(trust)
    if _terminal(trust):
        return {"key": key, "gate": gate_id, "mode": "terminal", "met": True}
    if gate_id not in REVIEW_GATES + WAIT_GATES:
        raise ApprovalMissing(E_APPLY_GATE.format(gate=gate_id))
    _approvers(trust)
    if gate_id in WAIT_GATES:
        st = _wait(client_factory, trust, cfg, key, gate_id, root)
        if not st["met"]:
            raise ApprovalMissing(E_WAIT.format(gate=gate_id, key=key, n=len(st["open"]), ids=", ".join(st["open"]),
                                                bad=_bad(st["bad_newer"], "on these")))
    else:
        h = gate_hash(gate_id, key, artifacts(gate_id, key, root))
        _approve(client_factory(), trust, cfg, key, {gate_id: h},
                 lambda bad: E_REVIEW.format(gate=gate_id, key=key, hash=h, bad=_bad(bad)))
    return {"key": key, "gate": gate_id, "mode": "comment", "met": True}


# ---- the poller: `skj approvals sync RUN_ID [--resume] [--dry-run]` ----------------------------------------------------------------
# Spec Kit 1.2.4.dev0 (read from the installed source, `workflows/engine.py` `RunState`): a run lives in
# `<project>/.specify/workflows/runs/<run_id>/state.json` (`RunState.save`, atomic temp + os.replace), with `run_id`, `workflow_id`,
# `status` (created|running|paused|completed|failed|aborted, `base.RunStatus`), `current_step_index`, `current_step_id` (a plain step id,
# or `<loop>:<step>:<n>` for a later loop iteration, `_execution.qualified_id`), `step_results`, `created_at`/`updated_at` (UTC isoformat)
# and `inputs`. Spec Kit's own run-id pattern is `^[a-zA-Z0-9][a-zA-Z0-9_-]*$` (no length cap); `specify workflow resume <run_id>
# --input k=v` (repeatable; `_parse_input_values` splits at the first `=` and strips) merges inputs and re-validates them, and a gate
# with `verdict_input` takes a non-empty input as its choice. The poller never writes Jira (no approval-request comment in 0.1).

RUN_WORKFLOW, RUN_INPUT = "jira-lifecycle", "issue_number"  # K4: the workflow input is the issue NUMBER; the key is <project>-<n>
RUN_STATUSES = ("created", "running", "paused", "completed", "failed", "aborted")
MAX_RUN_ID = 128
RESUME_TIMEOUT = 6 * 3600  # seconds: a resume runs the workflow to its next pause, agent steps included
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_STEP = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_ITER = re.compile(r"[1-9][0-9]{0,5}\Z")
_NUMBER = re.compile(r"[1-9][0-9]{0,9}\Z")

E_POLL_TERMINAL = ('approvals sync works in comment mode only (approval "comment" on the target branch): in terminal mode a person '
                   "answers the gates in a terminal; nothing was read or resumed")
E_RUN_ID = "the run id is not a Spec Kit run id (ASCII letters, digits, `-` and `_`, starting with a letter or digit, at most 128 characters)"
E_RUN = ("the run's state.json cannot be read or is not a run of the jira-lifecycle workflow (missing, a symlink, over 1 MiB, not JSON, "
         "another workflow or run, an unknown status or step, or no issue number), so nothing is done")
E_SYNC_GATE = "the run is paused at a step that is not one of the 12 gates of the jira-lifecycle workflow, so it is not resumed here"
E_RESUME = "`specify workflow resume` failed (exit status {code}); see `specify workflow status`"
E_RESUME_TIMEOUT = "`specify workflow resume` did not finish within the poller's time limit and was stopped; see `specify workflow status`"
E_RESUME_TOOLS = "`skj` or `git` was not found on a safe PATH (absolute entries outside the project), so the resumed steps would fail; the run is not resumed"
E_RESUME_MISSING = ("the `specify` command was not found on a safe PATH (absolute entries outside the project; an empty or relative entry "
                    "and one inside the project are ignored) or could not be started")


class ResumeFailed(ValueError):
    """`specify workflow resume` failed, timed out or could not start: exit 1, fixed text (the run's own output is never read)."""


def valid_run_id(run_id):
    return type(run_id) is str and _RUN_ID.match(run_id) is not None


def _step_ok(step):
    if type(step) is not str:
        return False
    parts = step.split(":")
    if len(parts) == 1:
        return _STEP.match(step) is not None
    return len(parts) == 3 and _STEP.match(parts[0]) is not None and _STEP.match(parts[1]) is not None and _ITER.match(parts[2]) is not None


def read_run(root, run_id):
    """{"run_id", "status", "step", "issue_number"} of one run, from `<root>/.specify/workflows/runs/<run id>/state.json`, which is
    UNTRUSTED (the agent can edit it): the run id is checked BEFORE any path is built (E_RUN_ID); no directory on the way may be a link;
    the file is read as `gate._read` reads (O_NOFOLLOW, O_NONBLOCK, fstat regular, confined to the root, 1 MiB, UTF-8) and parsed with the
    config's guard (digit cap, nesting); workflow id and run id must match, the status be known, the step a well-formed step id (or none
    while created), `inputs.issue_number` an int 1..9999999999 (never a bool or a float) and `updated_at` a time. ApprovalMissing(E_RUN)."""
    from . import config, gate
    if not valid_run_id(run_id):
        raise ApprovalMissing(E_RUN_ID)
    base = os.path.realpath(str(root))
    runs = os.path.join(base, ".specify", "workflows", "runs")
    path = os.path.join(runs, run_id)
    if os.path.realpath(runs) != runs or os.path.realpath(path) != path or os.path.islink(path):
        raise ApprovalMissing(E_RUN)
    text = gate._read(os.path.join(path, "state.json"), base, set())
    try:
        data = config._parse(text.encode("utf-8"), "state.json") if text is not None else None
    except config.ConfigInvalid:
        data = None
    d = data if isinstance(data, dict) else {}
    inp = d.get("inputs")
    n = inp.get(RUN_INPUT) if isinstance(inp, dict) else None
    status, step = d.get("status"), d.get("current_step_id")
    if not (d and d.get("workflow_id") == RUN_WORKFLOW and d.get("run_id") == run_id and type(n) is int and _NUMBER.match(str(n))
            and type(status) is str and status in RUN_STATUSES and _when(d.get("updated_at")) is not None
            and (_step_ok(step) or (step is None and status == "created"))):
        raise ApprovalMissing(E_RUN)
    return {"run_id": run_id, "status": status, "step": step or "", "issue_number": n}


def _paused_gate(step):
    """The gate a paused run waits at (`<gate>`, or the middle part of `<loop>:<gate>:<n>`): one of GATES, else E_SYNC_GATE."""
    gate_id = step.split(":")[1] if ":" in step else step
    if gate_id not in GATES:
        raise ApprovalMissing(E_SYNC_GATE)
    return gate_id


def _input(gate_id):
    return "verdict_" + gate_id.replace("-", "_")  # the workflow's input names are the underscore forms


def resume_argv(run_id, gate_id, verdict):
    """The argument LIST of the resume (never a shell string): `specify workflow resume <run id>`, then one `--input` per pair: this gate's
    verdict, and every OTHER gate's verdict input reset to empty (a verdict persists across resumes; a stale one would pass a later gate).
    Every token is built from the validated run id, a gate id from GATES and "approve" / "reject". ValueError otherwise."""
    if not valid_run_id(run_id):
        raise ValueError(E_RUN_ID)
    if gate_id not in GATES or verdict not in ("approve", "reject"):
        raise ValueError(E_SYNC_GATE)
    pairs = ["%s=%s" % (_input(gate_id), verdict)] + ["%s=" % _input(o) for o in GATES if o != gate_id]
    return ["specify", "workflow", "resume", run_id] + [x for pair in pairs for x in ("--input", pair)]


def _inside(path, tops):
    """Is `path`, or one of its ancestors, the same directory as one of the `os.stat` results `tops`?"""
    while True:
        try:
            st = os.stat(path)
            if any(os.path.samestat(st, t) for t in tops):
                return True
        except OSError:
            pass  # a directory that does not exist holds nothing; its ancestors are still looked at
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


def _safe_path(root):
    """PATH for the resume: absolute entries only, none inside the project or the current directory (an empty or relative entry means
    the current directory, where the project could plant a `specify`, `skj` or `git`). Symlinks are resolved first; `samestat` catches a
    differently-cased spelling is caught on a case-insensitive file system too."""
    tops = [os.stat(t) for t in {os.path.realpath(str(root)), os.path.realpath(os.getcwd())} if os.path.isdir(t)]
    keep = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and os.path.isabs(d) and not _inside(os.path.realpath(d), tops)]
    return os.pathsep.join(keep)


def _spawn(argv, cwd, env, timeout):
    """The poller's one subprocess (tests replace it): an argument list, no shell, stdin closed, stdout and stderr discarded (model and
    project text is never read or echoed), a new session so that on timeout the whole process group is killed. Returns the exit code."""
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            shell=False, close_fds=True, start_new_session=True)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        raise


# The agent credentials a resumed run's agent steps (`integration: claude|copilot|gemini`) may need, the ones the pre-H1a deny rule
# let through (AWS_SESSION_TOKEN never passed: `_TOKEN`). Never a Jira, skj or GitHub name: the agent must not hold the kit's identity.
RESUME_AGENT_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "AWS_PROFILE")


def resume_env(path, base=None):
    """The environment of a resumed run: `config.git_env(base)` (the git allow-list: no SKJ_*, Jira or GitHub credential, no git
    selector, config injection or helper command), then the RESUME_AGENT_ENV names that are set, then PATH = `path`."""
    from . import config
    base = os.environ if base is None else base
    return {**config.git_env(base), **{k: base[k] for k in RESUME_AGENT_ENV if k in base}, "PATH": path}


def _resume(root, run_id, gate_id, verdict):
    """Runs the resume: `specify` is the name looked up on the safe PATH (`_safe_path`) and nothing else, refused when absent; `skj` and
    `git` must be there too (the resumed steps run them). The child gets `resume_env` (the git allow-list plus the named agent
    credentials, never a Jira or GitHub one) with that PATH, cwd = the repository root, a time limit."""
    argv = resume_argv(run_id, gate_id, verdict)
    path = _safe_path(root)
    exe = shutil.which("specify", path=path)
    if not exe:
        raise ResumeFailed(E_RESUME_MISSING)
    if not all(shutil.which(tool, path=path) for tool in ("skj", "git")):
        raise ResumeFailed(E_RESUME_TOOLS)
    try:
        code = _spawn([exe] + argv[1:], cwd=str(root), env=resume_env(path), timeout=RESUME_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ResumeFailed(E_RESUME_TIMEOUT) from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ResumeFailed(E_RESUME_MISSING) from None
    if code != 0:
        raise ResumeFailed(E_RESUME.format(code=int(code)))


def _apply_plan(gate_id, root):
    """The bytes of an apply gate's plan file (`APPLY_FILES`), read confined (`gate._read`); exactly what `require` hashes."""
    from . import gate
    text = gate._read(os.path.join(str(root), APPLY_FILES[gate_id]), str(root), set())
    if text is None:
        raise ApprovalMissing(E_ARTIFACT)
    return text.encode("utf-8")


def sync(client_factory, trust, cfg, run_id, root, resume=False, dry_run=False):
    """The poller: {"run", "key", "gate", "verdict": approve|reject|waiting, ...} for the gate a run is paused at ({"run", "gate": None,
    "verdict": "waiting", "paused": False} for a run that is not paused). Order: the run id, then terminal mode (refused before anything
    is read), then state.json (untrusted, `read_run`) and the gate against GATES, then the local hashes (artifacts at HEAD, or the apply
    gate's plan file), all before any request; then the comments (GETs only). The poller reads with the token user, which is excluded
    like every `[skj:` author: its own comments never count. A review or apply gate is decided as `require`/`check` decide (`_verdict`),
    a wait gate by `wait_state` (met = approve). Waiting: `hashes` or `open` say what is missing. With `resume`, a verdict and not
    `dry_run`, the run is read again (still paused at the very same step) and resumed (`resume_argv`). It never writes to Jira."""
    if not valid_run_id(run_id):
        raise ApprovalMissing(E_RUN_ID)
    _ci_target_known(trust)
    if _terminal(trust):
        raise ApprovalMissing(E_POLL_TERMINAL)
    run = read_run(root, run_id)
    if run["status"] != "paused":
        return {"run": run_id, "gate": None, "verdict": "waiting", "paused": False}
    gate_id = _paused_gate(run["step"])
    key = "%s-%d" % (cfg.project, run["issue_number"])
    if not keys.valid_key(key):
        raise ApprovalMissing(E_RUN)
    _approvers(trust)
    out = {"run": run_id, "key": key, "gate": gate_id}
    if gate_id in WAIT_GATES:
        st = _wait(client_factory, trust, cfg, key, gate_id, root)
        verdict = "approve" if st["met"] else "waiting"
        extra = {} if st["met"] else {"open": st["open"]}
        bad = st["bad_newer"]
    else:
        if gate_id in REVIEW_GATES:
            hashes = {gate_id: gate_hash(gate_id, key, artifacts(gate_id, key, root))}
        else:
            data = _apply_plan(gate_id, root)
            hashes = {g: gate_hash(g, key, data) for g in VERB_GATES[GATE_VERB[gate_id]]}
        me, rows, skip = _reads(client_factory(), cfg, key)
        verdict, bad, _ = _verdict(board(rows, trust.approvers, skip), hashes)
        verdict = verdict or "waiting"
        extra = {"hashes": hashes} if verdict == "waiting" else {}
    out["verdict"] = verdict
    if bad:
        out["bad_newer"] = bad
    if resume:
        go = verdict != "waiting" and not dry_run
        if go:
            again = read_run(root, run_id)
            go = again["status"] == "paused" and again["step"] == run["step"]
        if go:
            _resume(root, run_id, gate_id, verdict)
        out["resumed"] = go
    out.update(extra)
    return out
