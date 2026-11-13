"""The DoD gate: AC to test traceability, assertion-free tests, AC drift against the story in Jira, open questions and
decisions, and the commits of the pull request (the story key, and Jira smart-commit commands).

Ported from the fork source's gate.py. A pull request controls everything read here (the ledger, the decisions file, the
test files, the commit messages, gate.tests in its config), so every reader is confined (`_read`) and linear in its input;
the few super-linear corners are capped and fail closed (MAX_TEST_IDS, MAX_TEST_ID, SCAN_BUDGET, MAX_COMMITS, MAX_MESSAGE).
"""
import ast
import errno
import functools
import os
import re
import stat
import threading
from pathlib import Path

from . import adf, config, issues, keys
from .client import ClientError
from .config import ConfigInvalid
from .decisions import effective, has_decision_lines, parse_decisions, supersede_problem
from .dod import (MissingAC, _visible, ac_hash, ac_lines, ac_section, ledger_ac_hash, ledger_rules_from, ledger_rules_reviewed, section,
                  unclosed)
from .questions import UNPARSED_NOTE, parse_questions
from .secretstore import SecretError

_PY = r"\bassert\b|pytest\.raises|self\.assert"
_JS = r"\bexpect\(|\bassert\b"
_JVM = r"\bassert|\bverify\("
_CS = r"\bAssert\.|Should\("
_GO = r"\bt\.(Error|Fatal)|\bassert\.|\brequire\."
_APEX = r"System\.assert|\bAssert\."
DEFAULT_TESTS = {
    "**/test_*.py": _PY, "**/*_test.py": _PY,
    "**/*.test.[jt]s": _JS, "**/*.spec.[jt]s": _JS, "**/*.test.[jt]sx": _JS,
    "**/*Test.java": _JVM, "**/*Test.kt": _JVM,
    "**/*Tests.cs": _CS, "**/*Test.cs": _CS,
    "**/*_test.go": _GO,
    "**/*Test.cls": _APEX, "**/*_Test.cls": _APEX,
}
_AC_RE = re.compile(r"^AC-\d+$")


def _cell(cells, i):
    c = cells[i] if len(cells) > i else ""
    return "" if "_(" in c else c  # untouched template placeholder -> empty


def parse_section3(md):
    """Section 3 rows tagged AC-<n> -> [{'ac','tests','evidence','status'}]. Graceful: missing/placeholder -> empty."""
    rows = []
    for line in _visible(section(md, 3) or "")[0].splitlines():  # a table in a fence or comment is an example
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not _AC_RE.match(cells[0]):
            continue
        rows.append({"ac": cells[0],
                     "tests": [t.strip() for t in _cell(cells, 2).split(",") if t.strip()],
                     "evidence": _cell(cells, 3), "status": _cell(cells, 4)})
    return rows


MAX_READ = 1 << 20  # bytes; a larger test file is skipped, not read
SKIPPED_NOTE = "{} file(s) skipped: symlink, outside --src, not a regular file, or over 1 MiB"


def _read(path, root, skipped):
    """Text of a small regular file under `root`, else None (also used for the gate's ledger). The PR controls the tree and gate.tests, so a read is
    a content oracle: a symlink (even one pointing inside root), a path whose real path leaves root, a FIFO or
    device, or a file over MAX_READ is never read; it is counted into the `skipped` set, never named.
    Unreadable or non-UTF-8 files return None uncounted. shortcut: only the final component is O_NOFOLLOW; the
    realpath re-check after open narrows, but cannot close, a swap of a parent directory by a concurrent writer
    (the checkout is static while the gate runs); upgrade to dir-fd openat if the gate ever scans a live tree."""
    try:
        real, top = os.path.realpath(path), os.path.realpath(root)  # resolve and confine BEFORE open: nothing outside is ever opened,
        if not os.path.islink(path) and os.path.commonpath([real, top]) == top:  # and a missing target there counts like an existing one
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)  # NONBLOCK: a FIFO must not hang open()
            with os.fdopen(os.open(path, flags), "rb") as f:
                st = os.fstat(f.fileno())
                if (os.path.realpath(path) == real and stat.S_ISREG(st.st_mode)
                        and st.st_size <= MAX_READ and os.path.samestat(st, os.stat(real))):
                    data = f.read(MAX_READ + 1)
                    if len(data) <= MAX_READ:
                        return data.decode("utf-8")
        skipped.add(os.path.abspath(path))  # a set (deduped); only its size is ever reported
    except OSError as e:
        if e.errno == errno.ELOOP:  # O_NOFOLLOW met a symlink (swapped in after the islink check)
            skipped.add(os.path.abspath(path))
    except (UnicodeDecodeError, ValueError):  # ValueError: commonpath across drives (Windows)
        pass
    return None


_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "dist", "build", ".specify"}
_GLOB_ERROR = "invalid glob in gate.tests"  # the pattern is config text, never echoed


def _component_rx(seg):
    """One path component's glob as a compiled regex: `*` any run, `?` one character, `[...]`/`[!...]` a class. Each chunk
    after a `*` is matched atomically at its leftmost place (the stdlib fnmatch technique), which is exact inside one
    component and keeps `*a*a*a…b` linear instead of exponential."""
    chunks, cur, i = [], "", 0
    while i < len(seg):
        c = seg[i]
        if c == "*":
            chunks.append(cur)
            cur = ""
            while i < len(seg) and seg[i] == "*":
                i += 1
            continue
        if c == "?":
            cur += "."
        elif c == "[":
            j = seg.find("]", i + 2)
            if j == -1:
                raise ValueError(_GLOB_ERROR)  # unbalanced class
            cls = seg[i + 1:j]
            cur, i = cur + "[" + ("^" + cls[1:] if cls[0] == "!" else cls) + "]", j
        else:
            cur += re.escape(c)
        i += 1
    chunks.append(cur)
    rx = chunks[0] if len(chunks) == 1 else (
        chunks[0] + "".join("(?=(?P<g%d>.*?%s))(?P=g%d)" % (n, c, n) for n, c in enumerate(chunks[1:-1])) + ".*" + chunks[-1])
    try:
        return re.compile("(?s:%s)\\Z" % rx)
    except re.error:
        raise ValueError(_GLOB_ERROR) from None


_ANY_COMPONENT = re.compile(r"(?s:.*)\Z")


@functools.lru_cache(maxsize=256)
def _glob_tokens(pattern):
    """The glob as a tuple: None = zero or more whole components (a `**` segment, and the implicit leading one that makes
    the match right-anchored like PurePath.match), else one component's regex. A trailing `**` is one component or more.
    `**` inside a segment (`a**`, `src/**x.py`) and `/` inside a class cannot be judged per component: ValueError."""
    segs = pattern.split("/")
    toks = [None]
    for n, seg in enumerate(segs):
        if seg == "**":
            if n == len(segs) - 1:
                toks += [_ANY_COMPONENT, None]
            elif toks[-1] is not None:  # `**/**/` is `**/`: consecutive ones would only multiply the work
                toks.append(None)
        elif "**" in seg:
            raise ValueError(_GLOB_ERROR)
        else:
            toks.append(_component_rx(seg))
    return tuple(toks)


def _closure(reach, toks):
    out = set(reach)
    for i in sorted(reach):
        while i < len(toks) and toks[i] is None:  # a None token may match zero components
            out.add(i + 1)
            i += 1
    return out


def _glob_match(path, pattern):
    """Does POSIX `path` match glob `pattern`? `**/` = zero or more directories, trailing `/**` = anything below,
    right-anchored (leading directories tolerated) so absolute and cwd-relative paths work. Component by component: the
    work is O(components x tokens), never a backtracking regex over the whole path."""
    toks = _glob_tokens(pattern)
    comps = path.replace("\\", "/").split("/")
    if toks[-1] is not None and not toks[-1].match(comps[-1]):
        return False  # the file name decides most paths in one regex call
    reach = _closure({0}, toks)
    for comp in comps:
        reach = _closure({i if toks[i] is None else i + 1 for i in reach
                          if i < len(toks) and (toks[i] is None or toks[i].match(comp))}, toks)
        if not reach:
            return False
    return len(toks) in reach


def valid_glob(pattern):
    """A gate.tests key the matcher accepts: 1-200 chars, no NUL/newline, classes balanced and well-formed, `**` only as a
    whole segment, no `/` inside a class."""
    if not pattern or len(pattern) > 200 or "\0" in pattern or "\n" in pattern:
        return False
    try:
        _glob_match("", pattern)
    except ValueError:
        return False
    return True


def discover_tests(src, tests, skipped=None):
    """{path: text} of files under src matching any test glob; vendored dirs pruned, unreadable files skipped,
    symlinks / non-regular / oversize files skipped and counted into `skipped` (see _read)."""
    skipped = set() if skipped is None else skipped
    found = {}
    for g in tests:
        _glob_match("", g)  # fail fast on a malformed glob, even when no files exist
    for root, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            p = Path(root, name)
            full = Path(os.path.abspath(p)).as_posix()
            if any(_glob_match(full, g) for g in tests):
                t = _read(p, src, skipped)
                if t is not None:
                    found[str(p)] = t
    return found


def _path_match(fname, path):
    f = fname.replace("\\", "/")
    return f == path or f.endswith("/" + path)


# ---- whole-identifier search, linear ----
MAX_TEST_IDS = 200  # distinct test ids judged per ledger; the rest count as not found (fail closed, with a note)
MAX_TEST_ID = 300  # characters; a longer test id is not found
SCAN_BUDGET = 256 << 20  # characters the text searches of one run may scan; past it a segment counts as not found
TOO_MANY_IDS = f"more than {MAX_TEST_IDS} distinct test ids: the rest count as not found"
SCAN_EXHAUSTED = "too much test text to search: the remaining test ids count as not found"
_IDCHARS = "[A-Za-z0-9_]"
_IDENT = re.compile(_IDCHARS + "+")
_MARK = re.compile("(?<!%s)" % _IDCHARS)
_ID_AFTER = "(?!%s)" % _IDCHARS


def _marked(text):
    """`text` with a NUL before every position that starts a whole-identifier match (the start, and after each character
    outside [A-Za-z0-9_]). A segment marked the same way is then a plain literal: the regex engine finds it with its
    linear prefix search, overlapping occurrences included, where `(?<!ID)seg(?!ID)` is O(n * len(seg))."""
    return _MARK.sub("\0", text.replace("\0", "�"))


def _present(seg, marked_text):
    """`seg` occurs in the text `marked_text` was made from, not as part of a longer [A-Za-z0-9_] run."""
    return re.search(re.escape(_marked(seg)) + _ID_AFTER, marked_text) is not None


class _Scan:
    """One run's view of the sources: identifier sets and marked texts built once per file, a memo per (segment, file),
    and SCAN_BUDGET for the text searches (only segments that are not a plain identifier need one)."""

    def __init__(self, sources):
        self.sources, self.budget, self.exhausted = sources, SCAN_BUDGET, False
        self._ids, self._marks, self._memo = {}, {}, {}

    def has(self, seg, fname, with_name):
        k = (seg, fname, with_name)
        if k not in self._memo:
            text = fname + "\n" + self.sources[fname] if with_name else self.sources[fname]
            if _IDENT.fullmatch(seg):
                if (fname, with_name) not in self._ids:
                    self._ids[(fname, with_name)] = set(_IDENT.findall(text))
                self._memo[k] = seg in self._ids[(fname, with_name)]
            else:
                self.budget -= len(text)
                if self.budget < 0:
                    self.exhausted = True
                    return False  # not memoised: the answer is "not judged", not "absent"
                if (fname, with_name) not in self._marks:
                    self._marks[(fname, with_name)] = _marked(text)
                self._memo[k] = _present(seg, self._marks[(fname, with_name)])
        return self._memo[k]


def _segments(test_id):
    """(path, [segments]) of a test id: `path::rest` pins the file by path suffix; segments split on `::`, ` > ` and `.`."""
    path, sep, rest = test_id.partition("::")
    if not sep:
        path, rest = "", test_id
    return path.replace("\\", "/").removeprefix("./"), sep, [s.strip() for part in re.split(r"::| > ", rest)
                                                             for s in part.split(".") if s.strip()]


def test_present(test_id, sources, scan=None):
    """Every segment of the ID must occur in the same file, as a whole identifier. `path::rest` pins the file by path suffix."""
    if len(test_id) > MAX_TEST_ID:
        return False
    path, sep, segs = _segments(test_id)
    if not segs:
        return False  # nothing to look for (empty id, or `path::` with no name)
    if sep and not path:
        return False  # `./::name` names no file
    scan = scan or _Scan(sources)
    # whole identifiers only: `test_add` must not be found in `test_add_many`; without a path the file name counts too
    return any((not path or _path_match(fname, path)) and all(scan.has(s, fname, not path) for s in segs) for fname in sources)


test_present.__test__ = False  # not a pytest test, despite the name


def _judged(rows):
    """The first MAX_TEST_IDS distinct test ids of the rows, in order; the rest are not judged (count as not found)."""
    out = {}
    for t in (t for r in rows for t in r["tests"]):
        if t not in out and len(out) < MAX_TEST_IDS:
            out[t] = None
    return out


def check_traceability(rows, sources, ac_count=0, scan=None):
    """ac_count = AC lines in ledger section 1; AC-1..AC-n with no section 3 row are unmapped."""
    have = {r["ac"] for r in rows}
    absent = [f"AC-{n}" for n in range(1, ac_count + 1) if f"AC-{n}" not in have]
    if not rows and not ac_count:
        return {"evaluated": False, "mapped": [], "unmapped": [],
                "note": "no AC-tagged rows in section 3 — insufficient to evaluate traceability"}
    scan, judged = scan or _Scan(sources), _judged(rows)
    found = {t: test_present(t, sources, scan) for t in judged}
    mapped, unmapped = [], []
    for r in rows:
        (mapped if any(found.get(t) for t in r["tests"]) else unmapped).append(r["ac"])
    unmapped += absent
    note = "all AC mapped" if not unmapped else f"{len(unmapped)} AC without a known test"
    if absent:
        note += f" ({', '.join(absent)}: no section 3 row)"
    if len({t for r in rows for t in r["tests"]}) > len(judged):
        note += "; " + TOO_MANY_IDS
    if scan.exhausted:
        note += "; " + SCAN_EXHAUSTED
    return {"evaluated": True, "mapped": mapped, "unmapped": unmapped, "note": note}


def scan_assertions(changed, tests):
    """Changed test files (by glob) with no assertion match. File-level, not method-level. shortcut: a gate.tests regex
    is run as written, so a catastrophic one from the PR's config is the PR's own slow check; the defaults are linear."""
    gaps = []
    for fname, text in changed.items():
        regexes = [rx for g, rx in tests.items() if _glob_match(fname, g)]
        if regexes and not any(re.search(rx, text) for rx in regexes):
            gaps.append({"file": fname, "issue": "no assertions"})
    return gaps


_ASSERT_CALLS = {"raises", "warns", "fail", "deprecated_call"}


def _callee(call):
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _has_assertion(fn, helpers=frozenset()):
    """A def whose body (nested statements included) asserts: `assert`, `raise AssertionError`, a call to
    `assert*`/raises/warns/fail (bare or attribute, any import alias of the module), or to a same-module helper that does."""
    for n in ast.walk(fn):
        if isinstance(n, (ast.Assert, ast.Raise)) and (isinstance(n, ast.Assert) or _exc_name(n.exc) == "AssertionError"):
            return True
        if isinstance(n, ast.Call):
            c = _callee(n)
            if c and (c.startswith("assert") or c in _ASSERT_CALLS or c in helpers):
                return True
    return False


def _exc_name(e):
    e = e.func if isinstance(e, ast.Call) else e
    return e.id if isinstance(e, ast.Name) else ""


PARSE_STACK = 512 << 20  # bytes of thread stack for ast.parse: the C parser recurses per nesting level
PARSE_TIMEOUT = 60  # seconds per file
UNANALYSED = "could not be analysed"  # the assertion finding for a Python test file the gate could not parse


def _parse(text):
    """ast.parse(text) in a worker thread with PARSE_STACK of stack, else None (a syntax error, RecursionError, MemoryError,
    PARSE_TIMEOUT, or a platform without threading.stack_size). On 3.9 a deep expression (`x = a` + `.a` * 300000, `-` * n,
    `not ` * n, `lambda:` * n ...) overflows the default C stack and kills the process (SIGSEGV): `except RecursionError`
    cannot catch that, so the parse never runs on the caller's stack. shortcut: a timed-out worker is left running (a daemon
    thread cannot be killed); the file is reported, the gate goes on."""
    box = {}
    text = text[1:] if text.startswith("\ufeff") else text  # a UTF-8 BOM (`_read` keeps it): ast.parse refuses the str with it

    def work():
        try:
            box["tree"] = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            pass
    try:
        old = threading.stack_size(PARSE_STACK)
    except (ValueError, RuntimeError):
        return None  # no big stack here: do not risk the caller's
    try:
        worker = threading.Thread(target=work, name="skj-gate-parse", daemon=True)
        worker.start()
    finally:
        threading.stack_size(old)
    worker.join(PARSE_TIMEOUT)
    return None if worker.is_alive() else box.get("tree")


def _py_file(text):
    """(defs, helpers) of a Python test file, once per file: defs = {name: [(class name or None, def)]} for the functions at
    module level or in a class body, helpers = the names of defs that assert directly (one level). None when it does not parse."""
    tree = _parse(text)
    if tree is None:
        return None
    nodes = list(ast.walk(tree))  # iterative: no recursion over a deep tree
    defs = {}
    for c, body in [(None, tree.body)] + [(n.name, n.body) for n in nodes if isinstance(n, ast.ClassDef)]:
        for f in body:
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault(f.name, []).append((c, f))
    return defs, frozenset(n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _has_assertion(n))


def check_test_assertions(rows, sources, scan=None):
    """Per mapped test: Python tests are parsed with ast and flagged when their own body asserts nothing.
    Returns (gaps, notes); other languages stay file-level (scan_assertions) and get one note."""
    gaps, notes, files, other = [], [], {}, False
    scan, nonpy = scan or _Scan(sources), {f: t for f, t in sources.items() if not f.endswith(".py")}
    # A bare test id (no `path::`) is looked up in every Python test file that mentions its name as a whole identifier, so an
    # unparseable file that only mentions it (a fixture) still adds its "could not be analysed" finding; a pinned
    # `path::name` reads that one file only.
    for tid in _judged(rows):
        if len(tid) > MAX_TEST_ID:
            continue
        path, _, segs = _segments(tid)
        if not segs:
            continue
        name, cls = segs[-1], (segs[-2] if len(segs) > 1 else None)
        verdicts, unparsed = [], False
        for f in sources:
            if (not f.endswith(".py") or (path and not _path_match(f, path)) or not _IDENT.fullmatch(name)
                    or not scan.has(name, f, False)):
                continue
            if f not in files:
                files[f] = _py_file(sources[f])
                if files[f] is None:  # fail closed: an assertion finding (blocks under `assertion`), never a silent pass
                    notes.append(f"could not parse {f}; per-test assertion check skipped for it")
                    gaps.append({"file": f, "issue": UNANALYSED})
            if files[f] is None:
                unparsed = True
                continue
            defs, helpers = files[f]  # functions called `name` at module level or in a class body (that class `cls` when given)
            verdicts += [_has_assertion(d, helpers) for c, d in defs.get(name, ()) if cls is None or c == cls]
        if verdicts and not any(verdicts):
            gaps.append({"test": tid, "issue": "no assertions"})
        elif not verdicts and not unparsed:
            other = other or test_present(tid, nonpy, scan)
    if other:
        notes.append("per-test assertion checking covers Python only; other languages are checked per file")
    return gaps, notes


UNREACHABLE = "Jira not reachable or no identity — drift not checked"
OFFLINE = "offline — drift not checked"
AC_UNREADABLE = "the story's acceptance criteria section is missing or unreadable in Jira — counted as drift"


def check_drift(ledger_md, issue, reason=UNREACHABLE, heading="Acceptance criteria"):
    """`issue`: the story as `issues.get` returns it, or None when it was not read (`reason` says why). The AC hash of its
    description's `heading` section is compared with the ledger's. A section that is gone, doubled, empty or unreadable
    since seeding (`dod.MissingAC`, an ADF error) is drift: fail closed, never a crash and never `no drift`."""
    recorded = ledger_ac_hash(ledger_md)
    if recorded is None:
        return {"evaluated": False, "drift": False, "note": "no AC hash in the ledger — drift not checked"}
    if issue is None:
        return {"evaluated": False, "drift": False, "note": reason}
    f = issue.get("fields") if isinstance(issue, dict) and isinstance(issue.get("fields"), dict) else {}
    try:
        now = ac_hash(ac_section(f.get("description"), heading))
    except (MissingAC, adf.AdfError):
        return {"evaluated": True, "drift": True, "note": AC_UNREADABLE}
    return {"evaluated": True, "drift": now != recorded}


def missing_evidence(rows):
    return [r["ac"] for r in rows if "green" in r["status"].lower() and not r["evidence"]]


# ---- commits in <base>..HEAD ----
COMMITS_NOT_CHECKED = "commits not checked"
MAX_COMMITS = 500  # commits judged per run; more add one `?` to each commit finding (fail closed)
MAX_MESSAGE = 64 * 1024  # characters per message scanned; a longer message is not scanned and is flagged by both checks
TOO_MANY_COMMITS = f"more than {MAX_COMMITS} commits in the range: only the newest {MAX_COMMITS} were read"
UNSCANNED = "%d commit message(s) over the scan cap or malformed were not scanned and are flagged"
# The story reference: KEY_RE's shape, bounded by anything that is not a Unicode word character, so `SPK-2٣`, `SPK-2_`
# or `éSPK-2` is never read as SPK-2 (keys.KEY_IN_TEXT_RE stops only at ASCII). Compared with the story key exactly.
_KEY_REF = re.compile(r"(?<!\w)[A-Z][A-Z0-9_]{1,9}-[1-9][0-9]{0,9}(?!\w)")


def _commit_log(repo, base, merges=False):
    """([(short sha, message or None)], note) for the commits in <base>..HEAD (merge commits only with `merges`), newest first; message None = over
    MAX_MESSAGE or a malformed record (flagged). `base` is a ref (CI passes origin/<target>); default is the origin/HEAD branch.
    A base that is not a plain ref name or cannot be resolved checks nothing and says so in the note (missing information
    never blocks). git runs with config.git_env(): no SKJ_* variable and nothing named like a credential reaches it."""
    def git(*args):  # stdout; None for "not found" (exit 1, no stderr); OSError for a real failure
        r = config._git(repo, *args)
        if r is None:
            raise OSError("git did not run")
        err = r.stderr.decode("utf-8", "replace").strip()
        if r.returncode == 1 and not err:
            return None
        if r.returncode:
            raise OSError(err[:200])
        return r.stdout.decode("utf-8", "replace").strip()

    try:
        if base is None:
            ref = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
            sha = ref and git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
            label = "no default branch (origin/HEAD)"
        elif not base or config._BAD_BASE.search(base):  # an option, a range, a revision expression or whitespace
            return [], f"base {base!r} is not a ref — {COMMITS_NOT_CHECKED}"
        else:
            sha = git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{base}^{{commit}}")
            label = f"base {base} not found"
        if not sha:
            return [], f"{label} — {COMMITS_NOT_CHECKED}"
        log = git("log", *(() if merges else ("--no-merges",)), "-z", f"--max-count={MAX_COMMITS + 1}", "--format=%h%x1f%B", f"{sha}..HEAD")
    except OSError as e:
        return [], f"git failed ({' '.join(str(e).split())[:100] or type(e).__name__}) — {COMMITS_NOT_CHECKED}"
    out = []
    for rec in filter(None, (log or "").split("\0")):
        sha, sep, msg = rec.partition("\x1f")
        sha = sha.strip()
        if not (sep and re.fullmatch(r"[0-9a-f]{4,40}", sha)):  # a NUL inside a message split the record: fail closed
            out.append(("?", None))
        else:
            out.append((sha, msg if len(msg) <= MAX_MESSAGE else None))
    notes = [UNSCANNED % n for n in [sum(msg is None for _, msg in out[:MAX_COMMITS])] if n]
    if len(out) > MAX_COMMITS:
        out, notes = out[:MAX_COMMITS] + [("?", None)], [TOO_MANY_COMMITS] + notes
    return out, "; ".join(notes) or None


def _has_key(msg, key):
    return any(m.group(0) == key for m in _KEY_REF.finditer(msg))


def check_commits(repo, key, base=None):
    """(short shas in <base>..HEAD whose message lacks the story `key`, note). Merge commits are ignored. A foreign
    project's key, or a lowercase, padded or Unicode-digit lookalike, is not the reference."""
    if not keys.valid_key(key):
        raise ValueError("invalid issue key")
    log, note = _commit_log(repo, base)
    return [sha for sha, msg in log if msg is None or not _has_key(msg, key)], note


# Q6 (DOCUMENTED): the kit moves an issue only through its own gated, previewed transitions (`done --apply` after the
# gate), never as a side effect of a push. A Jira smart-commit command in a commit message (`SPK-1 #done`, `#close`,
# `#time 2h`, `#comment x`, any `#<transition>`) would move or edit the issue the moment the commit reaches the linked
# repository, outside every gate, so the gate reports each one. The shape is matched wider than Jira's documented one on
# purpose (fail closed): any key-shaped token, lowercase too (Jira's case rule here is UNVERIFIED), followed anywhere later
# in the message, on its line or a later one, by `#` and a word holding a letter in any case (`#done`, `#1st-review`);
# NBSP or other Unicode spaces and CRLF are just characters between them. A project prefix of any length counts
# (`ABCDEFGHIJK-1`). `#123` (a pull-request reference) and a `#word` before every key are not commands.
# UNVERIFIED non-matches, left alone on purpose (Jira's parser is most likely ASCII-only): a key in fullwidth letters or
# digits (`ＳＰＫ-１`), with a Unicode hyphen (U+2010, U+2011, U+2212) or with non-ASCII digits (`SPK-١`).
_ANY_KEY = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]*-[0-9]")  # one start per token: linear
_COMMAND = re.compile(r"#[0-9]*[^\W\d_]")


def smart_command(msg):
    """True when `msg` holds a key-shaped token followed later by a `#<word with a letter>`: two regex scans, linear."""
    k = _ANY_KEY.search(msg)
    return k is not None and _COMMAND.search(msg, k.end()) is not None


def check_smart_commits(repo, base=None):
    """(short shas in <base>..HEAD whose message holds a Jira smart-commit command, note); same range and notes as
    check_commits, but merge commits are read too (a merge message is pushed like any other). A message over MAX_MESSAGE
    is flagged unscanned."""
    log, note = _commit_log(repo, base, merges=True)
    return [sha for sha, msg in log if msg is None or smart_command(msg)], note


LEDGER_UNCLOSED_NOTE = "the ledger has an unclosed code fence or comment — sections after it are not evaluated"

# Every check run_gate can report. config.py rejects gate.block names outside this set (tests/test_gate.py pins it to
# run_gate's finding keys; rule_coverage and rule_drift are reported only with rules enabled).
GATE_CHECKS = ("ac_traceability", "assertion", "ac_drift", "open_questions", "commits_without_story", "smart_commit_commands",
               "open_decisions", "rule_coverage", "rule_drift")


def rule_findings(cov):
    """What `rule_coverage` reports for a `rules.coverage` result: the uncovered, unknown, unconfirmed and changed ids, then one `?` for
    each of problems / missing / format / header that is not empty. Empty is a pass."""
    return [*cov["uncovered"], *cov["unknown"], *cov["unconfirmed"], *cov["changed"],
            *("?" for k in ("problems", "missing", "format", "header") if cov[k])]


def drift_findings(rl):
    """What `rule_drift` reports for a `rules` result: the drifted rule ids, `chain` / `problems`, and one `?` when the live rules were
    not read (Jira unreachable, or `--offline`). Empty is a pass."""
    return [*rl.get("drift", []), *(["?"] if "drift_note" in rl else [])]


def run_gate(ledger_md, sources, changed, issue=None, block=(), tests=None, reason=UNREACHABLE, commits=None,
             skipped=0, decisions_md=None, decisions_unreadable=False, rules_ctx=None, smart_commits=None,
             ac_heading="Acceptance criteria"):
    """`issue`: the story from `issues.get` (None: not read, `reason` says why). `commits`: check_commits' result;
    `smart_commits`: check_smart_commits' result. `decisions_md`: text of Decisions_<KEY>.md, or None when there is no
    such file; `decisions_unreadable`: the file exists but could not be read (symlink, outside --src, oversize, not UTF-8):
    counted as unresolved, never skipped. `rules_ctx`: None when business rules are off (no `rules` key, no `rule_coverage` question: the
    baseline output exactly); else `{"files": {issue key: rules.parse_file result}, "drift": rules.drift(...) result}` for the files
    `Rules from:` names that could be read (the GETs of the drift check are made by the caller; a context without `drift` counts as not
    checked), and the result gains `rules` = `rules.coverage(...)` at phase `pr` plus `drift`, `unreachable` and, when the live rules were
    not read, `drift_note`; with `reconfirmed` in the context (a base was given: a list from `rules.reconfirmed`, or a fixed text from
    `rules.RECONFIRM_NOTES`) also `reconfirmed` or `reconfirmed_note`, a note that never blocks."""
    rows = parse_section3(ledger_md)
    scan = _Scan(sources)
    trace = check_traceability(rows, sources, len(ac_lines(ledger_md)), scan)
    gaps = scan_assertions(changed, DEFAULT_TESTS if tests is None else tests)
    test_gaps, notes = check_test_assertions(rows, sources, scan)
    gaps += test_gaps
    drift = check_drift(ledger_md, issue, reason, ac_heading)
    # An AC row with no test, or none that exists, fails; no rows at all stays "not evaluated".
    qs = parse_questions(ledger_md)
    open_q = [q["id"] for q in qs["open"]]
    ds = parse_decisions(decisions_md or "")  # open and rejected are both unresolved (a decision a live `supersedes` answers is not); reported only when the file has D lines
    eff = effective(ds)
    rejected = eff["rejected"]
    if rules_ctx is not None:  # business rules on: a rejected `out of scope:` line is carried out by rule_coverage (the rule is back in scope)
        from . import rules  # lazy: rules imports this module lazily too
        rejected = [d for d in rejected if not rules.rejected_exclusion(d)]
    open_d = [d["id"] for d in sorted(eff["open"] + rejected, key=lambda d: int(d["id"][1:]))]
    open_d += ["?"] * (ds["unparsed"] or decisions_unreadable or supersede_problem(ds))  # fail closed: a malformed D line, an unclosed fence or comment, an unreadable file, a bad supersedes
    no_story, commits_note = commits or ([], None)
    smart, smart_note = smart_commits or ([], None)
    open_ledger = unclosed(ledger_md)  # fail closed: counts as an open-questions finding, so `gate.block` can stop on it
    found = {"ac_traceability": trace["unmapped"], "assertion": gaps, "ac_drift": drift["drift"],
             "open_questions": open_q or open_ledger, "commits_without_story": no_story, "smart_commit_commands": smart,
             "open_decisions": open_d}
    cov = None
    if rules_ctx is not None:
        cov = rules.coverage(ledger_md, sources, rules_ctx["files"], ledger_rules_from(ledger_md), ledger_rules_reviewed(ledger_md), ds, "pr", trace)
        found["rule_coverage"] = rule_findings(cov)
        d = rules_ctx.get("drift") or rules.DRIFT_OFFLINE
        cov["drift"], cov["unreachable"] = list(d["drift"]), bool(d["unreachable"])
        if not d["evaluated"]:  # a fixed text, whatever the caller passed
            cov["drift_note"] = rules.DRIFT_UNREACHABLE if cov["unreachable"] else rules.DRIFT_OFFLINE_NOTE
        found["rule_drift"] = drift_findings(cov)
        if "reconfirmed" in rules_ctx:  # a base was given: the target's `Rules reviewed:` against this one; a note, never in `found`
            if not isinstance(rules_ctx["reconfirmed"], list):  # not comparable: one of the fixed texts, never [] (that reads as a pass)
                cov["reconfirmed_note"] = rules_ctx["reconfirmed"] if rules_ctx["reconfirmed"] in rules.RECONFIRM_NOTES else rules.RECONFIRM_UNREADABLE
            else:
                cov["reconfirmed"] = list(rules_ctx["reconfirmed"])
    blocked_by = [k for k in found if k in block and found[k]]
    return {"traceability": trace, "assertion_gaps": gaps, "assertion_notes": notes, "drift": drift,
            "missing_evidence": missing_evidence(rows), "open_questions": open_q,
            "commits_without_story": no_story, "smart_commit_commands": smart,
            **({"open_decisions": open_d} if open_d or has_decision_lines(decisions_md or "") else {}),
            **({"rules": cov} if cov is not None else {}),
            **({"commits_note": commits_note} if commits_note else {}),
            **({"smart_commits_note": smart_note} if smart_note and smart_note != commits_note else {}),
            **({"skipped_note": SKIPPED_NOTE.format(skipped)} if skipped else {}),
            **({"open_questions_note": UNPARSED_NOTE} if qs["unparsed"] else {}),
            **({"ledger_unclosed": LEDGER_UNCLOSED_NOTE} if open_ledger else {}), "blocked_by": blocked_by, "blocking": bool(blocked_by)}


def _no_ledger(cfg, state, note):
    """No ledger to judge: advisory mode notes it; strict mode fails closed (nothing was checked, so nothing may pass: no commit,
    smart-commit or traceability check ran)."""
    if cfg.mode == "strict":
        return {"ledger": state, "blocking": True, "blocked_by": ["ledger"], "note": note.replace(" — advisory only", "") + " — strict mode blocks"}
    return {"ledger": state, "blocking": False, "note": note}


def evaluate(cfg, key, src=".", changed=(), base=None, offline=False, client_factory=None, target=None):
    """The gate for story `key` of the configured project over the checkout `src` (the fork's CLI dispatch). The ledger
    and the decisions file are `<src>/dod/DoD_<KEY>.md` and `Decisions_<KEY>.md`, read as confined as any file under src.
    The Jira requests are GETs only, none when `offline`: the story's description, and with `rules.enabled` the parent walk of
    `rules.drift` from `key` (never a key read from the ledger or a file). A missing identity or an unreachable Jira leaves AC drift
    unchecked (missing information never blocks) and makes rule drift `unreachable` (a `?` that blocks only where `rule_drift` is in
    gate.block, as in strict mode). `target`: the `--base` given on the command line; with rules enabled the target branch's
    `Rules reviewed:` is compared with this one (`rules.reconfirmed`, a note)."""
    if not keys.valid_key(key) or key.rsplit("-", 1)[0] != cfg.project:
        raise ValueError("not a key of project %s" % cfg.project)
    path = Path(src) / "dod" / keys.dod_name(key)
    shown = "dod/" + path.name  # the ledger's place is named; a symlink's target never
    if not os.path.lexists(path):
        return _no_ledger(cfg, "missing", f"no ledger at {shown} — run `skj dod seed {key}` to enable tracking")
    ledger_md = _read(path, src, set())
    if ledger_md is None:
        return _no_ledger(cfg, "unreadable", f"ledger at {shown} exists but is unreadable (a symbolic link, outside --src, not a regular file, over 1 MiB or "
                                         "not UTF-8) — advisory only")
    dpath = path.with_name("Decisions_%s.md" % key)
    decisions_md = _read(dpath, src, set()) if os.path.lexists(dpath) else None
    tests = {**DEFAULT_TESTS, **cfg.gate["tests"]}
    issue, reason, client = None, OFFLINE if offline else UNREACHABLE, None
    if not offline:
        try:
            client = client_factory()
            issue = issues.get(client, key, ["description"])
        except (ClientError, SecretError):
            pass  # no identity, Jira unreachable, a moved key: drift is simply not checked
    rules_ctx = None
    if cfg.rules["enabled"]:  # rules off: nothing is read, the baseline output exactly
        from . import rules, strict  # lazy: both import this module
        rules_ctx = {"files": rules.local_files(ledger_md, src)}
        rules_ctx["drift"] = rules.DRIFT_OFFLINE if offline else rules.drift(
            client, key, cfg, ledger_md, rules_ctx["files"], parse_decisions(decisions_md or ""))  # client None: unreachable
        if target is not None:
            rules_ctx["reconfirmed"] = strict.reconfirmed_vs_base(src, target, key, ledger_rules_reviewed(ledger_md)["hashes"])
    skipped = set()
    sources = discover_tests(src, tests, skipped)
    read = {}
    for f in changed:
        t = _read(f, src, skipped)
        if t is not None:
            read[f] = t
    return run_gate(ledger_md, sources, read, issue, skipped=len(skipped), block=cfg.effective_gate_block(), tests=tests,
                    reason=reason, commits=check_commits(src, key, base), smart_commits=check_smart_commits(src, base),
                    decisions_md=decisions_md, decisions_unreadable=os.path.lexists(dpath) and decisions_md is None,
                    ac_heading=cfg.ac_heading, rules_ctx=rules_ctx)


def _code(s, cap=200):
    """Untrusted text as one inline-code span: no backticks or newlines, length-capped."""
    return "`" + (" ".join(str(s).replace("`", "'").split())[:cap] or "?") + "`"


def _ids(values, cap=20):
    more = [f"…and {len(values) - cap} more"] if len(values) > cap else []
    return ", ".join([*map(_code, values[:cap]), *more])


def _rules_line(rl):
    """The one summary line for a `rule_coverage` result: rule ids, issue keys and counts, with fixed words (never file or ledger text)."""
    unknown = [x for x in rl["unknown"] if x != "?"]
    parts = [(rl["uncovered"], lambda v: "not cited by an AC row whose test exists: " + _ids(v)),
             (unknown, lambda v: "cited but in no rules file: " + _ids(v)),
             (["?"] * (len(rl["unknown"]) - len(unknown)), lambda v: "%d malformed `BR-` tag(s) in section 3" % len(v)),
             (rl["unconfirmed"], lambda v: "not confirmed yet (read the rule, then run `skj rules <story key> --confirm` yourself, no terminal "
                                           "needed): " + _ids(v)),
             (rl["changed"], lambda v: ("cannot be judged while the `Rules reviewed:` line is malformed (fix it by hand; then a human "
                                        "re-confirms with `skj rules <story key> --confirm` in their own terminal): "
                                        if "rules-reviewed" in rl["header"] else
                                        "changed since confirmed, or its text no longer matches its hash (re-pull with `skj rules <story key>` "
                                        "if the file was edited by hand; a human re-confirms with `skj rules <story key> --confirm` in their "
                                        "own terminal after reading the change): ") + _ids(v)),
             (rl["problems"], lambda v: "error problem(s) in a rules file: " + _ids(v)),
             (rl["missing"], lambda v: "no usable rules file for issue " + _ids(v) + " (`skj rules <story key>`)"),
             (rl["format"], lambda v: "a rules file in another format for issue " + _ids(v) + " (re-pull with `skj rules <story key>`)"),
             (["rules-from"] if "rules-from" in rl["header"] else [],
              lambda v: "the ledger has no valid `Rules from:` line (`skj dod refresh <story key>`)"),
             (["rules-reviewed"] if "rules-reviewed" in rl["header"] else [], lambda v: "the ledger's `Rules reviewed:` line is malformed (fix it by hand)")]
    return "- Business rules: " + "; ".join(f(v) for v, f in parts if v)


def _drift_line(rl):
    """The one summary line for a `rule_drift` result: rule ids and fixed words (never rule text)."""
    ids = [x for x in rl["drift"] if x not in ("chain", "problems")]
    parts = [(ids, lambda v: "the committed rules differ from the rules live in Jira: " + _ids(v) + " (re-pull with `skj rules <story key>`)"),
             (["chain"] if "chain" in rl["drift"] else [], lambda v: "`Rules from:` is not the parent chain in Jira (`skj dod refresh <story key>`)"),
             (["problems"] if "problems" in rl["drift"] else [], lambda v: "the Problems lines of a rules file differ from Jira (re-pull with `skj rules <story key>`)"),
             (["x"] if rl.get("unreachable") else [], lambda v: "rules-unreachable: the rules in Jira could not be read, so drift is not checked "
                                                              "(the gate's identity must be able to read the story and its parent issues)"),
             (["x"] if "drift_note" in rl and not rl.get("unreachable") else [], lambda v: "drift not checked (offline, or the live check was not run)")]
    return "- Rule drift: " + "; ".join(f(v) for v, f in parts if v)


def render_summary(result):
    """Markdown PR comment for a gate result (what `--summary-out` writes)."""
    head = "### skj gate\n\n"
    if "traceability" not in result:  # no / unreadable ledger: a note, blocking in strict mode
        return head + ("**Blocking - this check fails the PR:** " if result.get("blocking") else "Advisory: ") + f"{_code(result.get('note', ''))}\n"
    trace, drift = result["traceability"], result["drift"]
    drift_line = (drift.get("note") or "AC changed in Jira since the ledger was seeded" if drift["drift"]
                  else "no drift" if drift["evaluated"] else drift.get("note", "drift not checked"))
    lists = [("ac_traceability", "AC without a known test", trace["unmapped"]),
             ("assertion", "Test files or mapped tests with no assertions",
              [g.get("file") or g["test"] for g in result["assertion_gaps"]]),
             ("evidence", "Green AC without evidence", result["missing_evidence"]),
             ("open_questions", "Open questions in ledger section 2", result.get("open_questions", [])),
             ("open_decisions", "Open decisions in the decisions file", result.get("open_decisions", []))]
    found = {k: f"- {label}: {', '.join(map(_code, v))}" for k, label, v in lists if v}
    if result.get("commits_without_story"):
        found["commits_without_story"] = "- Commits without the story key: " + _ids(result["commits_without_story"])
    if result.get("smart_commit_commands"):
        found["smart_commit_commands"] = ("- Commits with a Jira smart-commit command (`<KEY> #<command>` moves or edits the issue "
                                          "outside the gate; reword them): " + _ids(result["smart_commit_commands"]))
    rl = result.get("rules") if isinstance(result.get("rules"), dict) else None
    if rl and rule_findings(rl):
        found["rule_coverage"] = _rules_line(rl)
    if rl and drift_findings(rl):
        found["rule_drift"] = _drift_line(rl)
    if rl and (rl.get("reconfirmed") or "reconfirmed_note" in rl):
        from . import rules  # lazy: rules imports this module lazily too
        rn = rl.get("reconfirmed_note")
        found["rules_reconfirmed"] = ("- " + (rn if rn in rules.RECONFIRM_NOTES else rules.RECONFIRM_UNREADABLE) if "reconfirmed_note" in rl else
                                      "- rules reconfirmed: %s; %s" % (rules.reconfirmed_text(rl["reconfirmed"]), rules.RECONFIRM_NOTE))
    note = result.get("ledger_unclosed")
    if note:  # stands in for the open-questions line when that list is empty
        found["open_questions" if "open_questions" not in found else "ledger_unclosed"] = f"- {_code(note)}"
    if not trace["evaluated"]:
        found["trace_note"] = "- AC traceability not evaluated (no AC-tagged rows in section 3)"
    blocked = [found.pop(k, f"- {drift_line}") for k in result.get("blocked_by", [])]
    out = []
    if blocked:
        out += ["**Blocking - this check fails the PR:**", *blocked, ""]
    if found:
        out += ["**Advisory:**", *found.values(), ""]
    if not blocked and not found:
        out += ["No findings.", ""]
    tail = [f"AC drift: {drift_line}"]
    if result.get("open_decisions") == []:
        tail.append("Open decisions: none")
    if any(n in trace.get("note", "") for n in (TOO_MANY_IDS, SCAN_EXHAUSTED)):
        tail.append("Traceability: " + _code(trace["note"], 400))
    for label, k in (("Open questions", "open_questions_note"), ("Commit check", "commits_note"),
                     ("Smart-commit check", "smart_commits_note"), ("Files skipped", "skipped_note")):
        if result.get(k):
            tail.append(f"{label}: " + _code(result[k]))
    if result.get("assertion_notes"):
        tail.append("Assertion check: " + "; ".join(map(_code, result["assertion_notes"])))  # informational, not a finding
    return head + "\n".join(out + tail) + "\n"


def render_error(reason):
    """Markdown body for a gate that could not run (or, for an invalid config, FAILED). The values of $SKJ_TOKEN and
    $SKJ_EMAIL are redacted before capping."""
    invalid = isinstance(reason, ConfigInvalid)
    reason = " ".join(str(reason).split())
    for name in ("SKJ_TOKEN", "SKJ_EMAIL"):
        secret = os.environ.get(name, "").strip()
        if secret:
            reason = reason.replace(secret, "***")
    if invalid:
        return (f"### skj gate\n\n**skj gate FAILED: skj.config.json is invalid:** {_code(reason, 500)}\n\n"
                "Fix the config; an invalid config fails this check.\n")
    return f"### skj gate\n\nThe gate could not run: {_code(reason, 500)}\n\nThis check is advisory and does not fail the PR.\n"
