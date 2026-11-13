"""`skj pr create`: the pull request for a story, opened through the local `gh` binary (P1; ported from the fork's prs.py).

GitHub only. The tests never run `gh` or `git push`: `subprocess.run` is faked. What changed against the fork, on purpose:

* The body is built here from the ledger (`dod/DoD_<KEY>.md`: the AC rows of section 3, the latest gate entry and the latest review of
  section 4), the decisions file and `templates/pr-body.md` (package data), not written by an agent. Every value that comes from a
  repository file is untrusted and goes through `neutral()` first: control, format, bidi and line-separator characters dropped,
  whitespace collapsed, a length cap, e-mail addresses removed, `@` turned into the fullwidth `＠` (so GitHub never notifies anyone),
  a `[skj:` marker broken, Markdown punctuation backslash-escaped. Decision and finding TEXT is never copied: ids, kinds and counts only.
* The title is `<KEY> <text>`: the key first, uppercase, the text cleaned and cut at 200 characters (`make_title`).
* A Jira smart-commit command (`<KEY> #word`, the gate's regex) in the title or the body is refused, never sent (P2-AC3).
* `gh` gets a scrubbed environment (`gh_env`): an allow-list of what `gh` needs to find its own login and its network, never the Jira
  identity (SKJ_*) and no other secret. No argument holds a token. The body goes on stdin (`--body-file -`) as the exact bytes the plan
  showed, so a file swapped after the approval is never read. Every call has a timeout; stdin is DEVNULL where nothing is piped.
* `git push` is opt-in (`--push`) and uses an explicit `refs/heads/<head>:refs/heads/<head>` refspec.
* The fork's integer work-item id limit is gone: story keys are strings that pass `keys.KEY_RE`.

`review_summary` / `review_refusal` read the verdict and the counts from the ledger text; `review_blocks` (L5b, strict) reads the
ledger from the head commit through them and adds the commit checks (staleness, origin), with `spec_dir_in_tree` and
`only_ledger_since` (the one ledger-only comparison strict uses). Not ported: `uncommitted` (the fork's comment-approval step, A phase).
"""
import os
import posixpath
import re
import subprocess
import unicodedata
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from . import config, dod, gate, keys
from .decisions import _scrub_emails, parse_decisions
from .push import _cell, _clean

MAX_TITLE = 200  # characters of the text after the key
MAX_BODY = 60000  # characters; GitHub's limit is 65536 (UNVERIFIED), the margin is on purpose
MAX_ROWS = 100  # AC rows shown
MAX_OUT = 65536  # bytes of `gh` output ever looked at
GH_TIMEOUT = 60  # seconds for `gh pr create`
PUSH_TIMEOUT = 120  # seconds for `git push`
TEMPLATE = Path(__file__).resolve().parent / "templates" / "pr-body.md"  # package data (`templates/*.md`)
MAX_TEMPLATE = 64 * 1024

E_NO_GH = "`gh` (the GitHub CLI) is not installed or not on PATH; install it and sign in with `gh auth login`"
E_GH_TIMEOUT = ("`gh pr create` did not answer within %d s; the pull request may or may not exist: look on GitHub before running "
                "this again (a write is never retried)" % GH_TIMEOUT)
E_GH_RUN = "`gh` could not be run; nothing was created, as far as skj can tell: look on GitHub before running this again"
E_URL = "`gh` answered with something that is not a pull request URL; the PR may exist: look on GitHub before running this again"

# ---- what `gh` may see of this process's environment ----
# Allow-list, not a deny-list: `gh` finds its login in its own config directory, the OS keychain or GH_TOKEN / GITHUB_TOKEN (and the
# enterprise pair), so exactly those variables, plus the paths, locale, proxy and CA settings a network call needs. SKJ_EMAIL,
# SKJ_TOKEN and every other variable (JIRA_*, cloud keys, GIT_* selectors) are not passed. The GitHub tokens are GitHub's own
# credential for the tool that is meant to use them; the Jira credentials never reach it.
GH_ENV_ALLOW = frozenset({
    "HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TERM", "TZ",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "GH_CONFIG_DIR", "GH_HOST",
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
})
GH_ENV_FIXED = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1", "NO_COLOR": "1",
                "GH_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"}
_SECRET_NAME = re.compile(r"(?:^SKJ_|_TOKEN$|_PAT$|_SECRET$|_PASSWORD$|_CREDENTIALS$)", re.I)


def gh_env(base=None):
    """The environment of a `gh` run (see GH_ENV_ALLOW)."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if k in GH_ENV_ALLOW or k.startswith("LC_")}
    return {**env, **GH_ENV_FIXED}


_USERINFO = re.compile(r"(://)[^/@\s]*@")  # scheme://user:pass@host -> scheme://host
_GH_TOKENS = re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}")


def _redact(text):
    """`text` with credentials cut out (a tool's error text could echo one): the value of every secret-named variable of this process,
    URL userinfo, and GitHub token shapes."""
    for k, v in os.environ.items():
        if len(v) >= 8 and _SECRET_NAME.search(k):
            text = text.replace(v, "[redacted]")
    return _GH_TOKENS.sub("[redacted]", _USERINFO.sub(r"\1", text))


# ---- untrusted text ----
_MARKER = re.compile(r"\[(?=\s*skj\s*:)", re.I)
_BREAKS = str.maketrans({c: " " for c in "\t\n\v\f\r\x1c\x1d\x1e\x1f\x85\u2028\u2029"})  # line breaks become a space, not a join
_MD = str.maketrans({c: "\\" + c for c in "\\`*_[]<>|~#&$!"})


def clean(s, cap):
    """One line, at most `cap` characters: no control, format (bidi), surrogate, private-use, unassigned or separator characters
    (CR, LF, U+2028 and U+2029 included), whitespace runs collapsed. Linear: the input is cut first."""
    if not isinstance(s, str):
        return ""
    return _clean(s[:cap * 4 + 64].translate(_BREAKS))[:cap].rstrip()


_REF = re.compile(r"#(?=\w)")  # `#123`, `owner/repo#12`, `#word`: GitHub cross-references them


def neutral(s, cap=200):
    """`s` from a repository file made safe for a Markdown line: `clean`, addresses removed, a `[skj:` marker broken, a `#` before a word
    character replaced by the fullwidth `\uff03` (no issue cross-reference), Markdown punctuation escaped, and every `@` replaced by the
    fullwidth `\uff20` (GitHub notifies on an ASCII `@name`, never on this one). A Jira smart-commit command shape (`#word` with a
    letter) is refused, not neutralised: the text would be hostile. UNVERIFIED: that `\uff03` stops a cross-reference."""
    s = _MARKER.sub("(", _scrub_emails(clean(s, cap)))
    if gate._COMMAND.search(s):
        raise ValueError("refusing to open a PR: text from the ledger holds a Jira smart-commit shape (`#word`), which could move the story")
    return _REF.sub("\uff03", s).translate(_MD).replace("@", "\uff20")


# ---- title ----
def make_title(key, text=""):
    """`<KEY> <text>`: the key first (it must pass the key pattern), the text cleaned and cut at MAX_TITLE; an empty text leaves the key
    alone, a text that already starts with the key does not repeat it. A title the gate's smart-commit regex matches is refused."""
    if not keys.valid_key(key):
        raise ValueError("invalid issue key")
    t = clean(text, MAX_TITLE)
    if t == key or t.startswith(key + " "):
        t = t[len(key):].lstrip()
    t = _MARKER.sub("(", t).replace("@", "\uff20")  # as neutral(): no notification, no forged marker
    title = key + (" " + t if t else "")
    smart = gate.smart_command(title)
    title = _REF.sub("\uff03", title)  # after the check, which needs the raw `#`
    if smart:
        raise ValueError("refusing to open a PR: the title holds a Jira smart-commit shape (`%s #word`), which could move the story; "
                         "reword it" % key)
    return title


# ---- refs ----
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def check_branch(kind, name):
    """Refuse an option, a refspec trick (+x, :x, a:b), a revision expression and odd ref forms before any git or gh call."""
    if (not isinstance(name, str) or len(name) > 200 or not _BRANCH.fullmatch(name) or config._BAD_BASE.search(name) or "//" in name
            or name.endswith(("/", ".", ".lock")) or "/." in name or ".lock/" in name):
        raise ValueError("invalid %s branch name: use letters, digits, '.', '_', '-', '/' only" % kind)
    return name


# ---- ledger reading ----
_HEAD = re.compile(r"^### (Gate|Review)\b(.*)$", re.M)
_NEXT = re.compile(r"^#{1,3} ", re.M)
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_BLOCKS_PR = re.compile(r"^- \*\*Blocks PR:\*\*\s*(yes|no)\b", re.M | re.I)
_SEVERITY = re.compile(r"^- \*\*Confirmed by severity:\*\*(.{0,500})$", re.M)
_PAIR = re.compile(r"(?<![\w-])([A-Za-z][\w-]{0,15})\s+(\d{1,6})(?!\d)")
_TITLE = re.compile(r"^# DoD [—-] [A-Z][A-Z0-9_]{1,9}-[1-9][0-9]{0,9}: (.+)$")
GATE_LABELS = ("traceability", "assertion gaps", "AC drift", "missing evidence", "commits without", "open questions", "open decisions",
               "business rules", "ledger", "files skipped", "blocking")
UNCLOSED = "the ledger has an unclosed code fence or comment: sections after it are not read"


def _log(ledger_md):
    """(section 4 of the ledger outside fences and comments, or None when the ledger is unreadable that way)."""
    vis, still_open = dod._visible(ledger_md)
    return None if still_open else (dod.section(vis, 4) or "")


def _block(log, kind, live=False):
    """(heading rest, body) of the LAST `### <kind>` block of section 4 (for a review: the last not marked `(superseded)`), or None."""
    heads = [m for m in _HEAD.finditer(log) if m.group(1) == kind and not (live and m.group(2).rstrip().endswith("(superseded)"))]
    if not heads:
        return None
    h = heads[-1]
    rest = log[h.end():]
    nxt = _NEXT.search(rest)
    return h.group(2), (rest[:nxt.start()] if nxt else rest)


def gate_summary(ledger_md):
    """({'found', 'date', 'lines': [(label, value)]}) of the latest `### Gate` entry; only the known labels are read."""
    log = _log(ledger_md)
    blk = _block(log, "Gate") if log is not None else None
    if blk is None:
        return {"found": False, "unreadable": log is None, "date": "", "lines": []}
    found = {}
    for line in blk[1].splitlines():
        if line.startswith("- ") and len(line) <= 2000:
            label, sep, value = line[2:].partition(":")
            low = label.strip().lower()
            for stem in GATE_LABELS:
                if sep and stem not in found and low.startswith(stem.lower()):
                    found[stem] = value.strip()
    d = _DATE.search(blk[0])
    return {"found": True, "unreadable": False, "date": d.group(1) if d else "", "lines": [(s, found[s]) for s in GATE_LABELS if s in found]}


def review_summary(ledger_md):
    """The latest live review of section 4, read from the ledger text only: {'found', 'unreadable', 'date', 'blocks' ('yes'|'no'|None),
    'counts' {severity: int}, 'problem' (a fixed text or '')}. Staleness against the commits is `review_blocks`' job (L5b)."""
    log = _log(ledger_md)
    blk = _block(log, "Review", live=True) if log is not None else None
    out = {"found": blk is not None, "unreadable": log is None, "date": "", "blocks": None, "counts": {}, "problem": ""}
    if blk is None:
        return out
    d = _DATE.search(blk[0])
    out["date"] = d.group(1) if d else ""
    verdicts = [v.lower() for v in _BLOCKS_PR.findall(blk[1])]
    out["blocks"] = "yes" if "yes" in verdicts else ("no" if verdicts else None)
    sev = _SEVERITY.findall(blk[1])
    if len(sev) > 1:
        out["problem"] = "the latest review has more than one `Confirmed by severity:` line (ambiguous)"
        return out
    pairs = [(k.lower(), int(n)) for k, n in _PAIR.findall(sev[0])] if sev else []
    counts = dict(pairs)
    if len(counts) != len(pairs):
        out["problem"] = "the latest review repeats a severity on its `Confirmed by severity:` line (ambiguous)"
        return out
    out["counts"] = {s: counts[s] for s in config.REVIEW_SEVERITIES if s in counts}
    return out


def review_refusal(rev, block_on):
    """Why the PR must not open while `review_block_on` (a list of severities) is set, or None. Empty list: advisory, None. Missing
    information refuses (blocking is opted into). Fixed texts only."""
    if not block_on:
        return None
    if rev["unreadable"]:
        return UNCLOSED
    if not rev["found"]:
        return "no review is logged in ledger section 4: run the review first"
    if rev["problem"]:
        return rev["problem"]
    if rev["blocks"] is None:
        return "the latest review has no `Blocks PR:` line"
    if rev["blocks"] == "yes":
        return "the latest review blocks the PR (`Blocks PR: yes`): fix the findings and run the review again"
    for s in block_on:
        if s not in rev["counts"]:
            return "the latest review does not report a `%s` count on its `Confirmed by severity:` line" % s
        if rev["counts"][s]:
            return "the latest review has %d confirmed `%s` finding(s) and review_block_on includes `%s`" % (rev["counts"][s], s, s)
    return None


# ---- tasks ----
def tasks_row(parsed):
    """(unticked ids, (field, value) plan row) for a parsed tasks.md; at most 20 ids are named."""
    open_ids = [t["id"] for t in parsed if not t["done"]]
    if not open_ids:
        return [], ("tasks", "all %d ticked" % len(parsed))
    shown = ", ".join(i[:12] for i in open_ids[:20]) + (" (+%d more)" % (len(open_ids) - 20) if len(open_ids) > 20 else "")
    return open_ids, ("draft", "yes — %d unticked task(s): %s" % (len(open_ids), shown))


# ---- body ----
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")


def _template():
    try:
        if TEMPLATE.is_symlink() or TEMPLATE.stat().st_size > MAX_TEMPLATE:
            raise OSError
        return TEMPLATE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ValueError("the PR body template (templates/pr-body.md) is missing or unreadable in this installation") from None


def ledger_title(ledger_md):
    """The story summary from the ledger's first line (`# DoD — KEY: summary`), or ''."""
    first = ledger_md[:1100].split("\n", 1)[0].rstrip("\r")
    m = _TITLE.match(first)
    return clean(m.group(1), MAX_TITLE) if m else ""


def _ac_rows(ledger_md):
    rows = gate.parse_section3(ledger_md)
    out = []
    for r in rows[:MAX_ROWS]:
        tests = ", ".join(neutral(t, 100) for t in r["tests"][:5]) + (" (+%d more)" % (len(r["tests"]) - 5) if len(r["tests"]) > 5 else "")
        out.append("| %s | %s | %s |" % (r["ac"][:12], neutral(r["status"], 60) or "-", tests or "-"))
    if len(rows) > MAX_ROWS:
        out.append("| … | %d more row(s) not shown | see the ledger |" % (len(rows) - MAX_ROWS))
    return "\n".join(out) or "| - | no AC rows in ledger section 3 | - |"


def _decisions_line(md):
    if md is None:
        return "none recorded"
    p = parse_decisions(md)
    shown = [d for d in p["open"] + p["rejected"]]
    if not shown and not p["validated"] and not p["unparsed"]:
        return "none recorded"
    ids = ", ".join("%s (%s)" % (d["id"] if re.fullmatch(r"D[0-9]{1,6}", d["id"]) else "D?", d["kind"]) for d in shown[:20])
    line = "%d open, %d rejected, %d validated" % (len(p["open"]), len(p["rejected"]), len(p["validated"]))
    if ids:
        line += ": " + ids + (" (+%d more)" % (len(shown) - 20) if len(shown) > 20 else "")
    return line + (" (some lines of the decisions file could not be read)" if p["unparsed"] else "")


def _gate_lines(g, key):
    if g["unreadable"]:
        return "- " + UNCLOSED + "."
    if not g["found"]:
        return "- No gate result is logged in the ledger (section 4): run `skj gate` first."
    return "\n".join("- **%s:** %s" % (("Commits without " + key) if label == "commits without" else label.capitalize() if label != "AC drift" else label,
                                         neutral(value, 200) or "-")
                     for label, value in g["lines"]) or "- The logged gate entry has no recognised lines."


def _review_lines(r):
    if r["unreadable"]:
        return "- " + UNCLOSED + "."
    if not r["found"]:
        return "- No review is logged in the ledger (section 4)."
    counts = " · ".join("%s %d" % (s, r["counts"][s]) if s in r["counts"] else "%s not reported" % s for s in config.REVIEW_SEVERITIES)
    out = ["- **Confirmed findings:** " + counts, "- **Blocks PR:** " + (r["blocks"] or "not stated")]
    if r["problem"]:
        out.append("- " + r["problem"] + ".")
    return "\n".join(out)


def build_body(cfg, key, ledger_md, decisions_md=None):
    """The PR body for story `key`, from `templates/pr-body.md` and the ledger. Raises ValueError for a body that is over MAX_BODY,
    holds a smart-commit shape or a `[skj:` marker. Deterministic: no clock, no environment."""
    g, r = gate_summary(ledger_md), review_summary(ledger_md)
    values = {
        "summary": neutral(ledger_title(ledger_md), MAX_TITLE) or "_(the ledger has no story summary)_",
        "key": key,
        "jira_url": "https://%s/browse/%s" % (cfg.site, key),
        "ac_rows": _ac_rows(ledger_md),
        "ledger_path": "dod/DoD_%s.md" % key,
        "decisions": _decisions_line(decisions_md),
        "gate_date": " (%s)" % g["date"] if g["date"] else "",
        "gate_lines": _gate_lines(g, key),
        "review_date": " (%s)" % r["date"] if r["date"] else "",
        "review_lines": _review_lines(r),
    }

    def put(m):
        if m.group(1) not in values:
            raise ValueError("the PR body template names a value skj does not know: {{%s}}" % m.group(1)[:40])
        return values[m.group(1)]
    body = _PLACEHOLDER.sub(put, _template())  # one pass: a value is never searched for a placeholder
    check_body(body, key)
    return body


def check_body(body, key=""):
    """The final text checks: size, no `[skj:` marker, no smart-commit shape. ValueError (fixed text) otherwise."""
    if len(body) > MAX_BODY:
        raise ValueError("the PR body is larger than %d characters; nothing sent" % MAX_BODY)
    if _MARKER.search(body):
        raise ValueError("refusing to open a PR: the body holds a `[skj:` marker")
    if gate.smart_command(body):
        raise ValueError("refusing to open a PR: the body holds a Jira smart-commit shape (`%s #word`), which could move the story" % (key or "KEY"))


# ---- the plan ----
def render_plan(key, title, body, base, head, draft, tasks_row=None, head_sha=None, remote="", push=False):
    """The plan: a table of what will be sent and the exact body. Canonical: only validated values, no clock."""
    preview = " ".join(body.split())
    if len(preview) > 120:
        preview = preview[:120] + "..."
    argv = "gh pr create --title <title> --body-file - --base <base> --head <head>" + (" --draft" if draft else "")
    rows = [("host", "github (local gh)"), ("story", key), ("title", title), ("base", base), ("head", head),
            ("head commit", head_sha or "(no local branch of that name)"), ("remote", remote or "origin/%s is not known here" % head),
            ("push", "yes: git push -u origin refs/heads/%s:refs/heads/%s" % (head, head) if push else "no"),
            ("draft", "yes" if draft else "no"), tasks_row or ("tasks", "not checked"),
            ("gh", argv + " (the body on stdin)"), ("body", "%d chars: %s" % (len(body), preview))]
    lines = ["Plan for pull request", "", "| field | value |", "|---|---|"]
    lines += ["| %s | %s |" % (k, _cell(str(v))) for k, v in rows]
    return "\n".join(lines) + "\n\n## PR body (exactly as it will be sent)\n\n" + body


# ---- git and gh ----
def head_state(root, head, push, apply=False):
    """(local head commit or '', remote text). ValueError when origin/<head> is a different commit and no --push, when (on apply) origin/<head>
    is not known here and no --push (gh would open the PR on whatever GitHub holds, not on the approved commit), or --push has no local
    branch to push. No fetch is done: `origin/<head>` is as fresh as the last fetch."""
    local = config._rev(root, "refs/heads/" + head)
    remote = config._rev(root, "refs/remotes/origin/" + head)
    if push and not local:
        raise ValueError("--push needs a local branch named like --head")
    if local and remote and local != remote and not push:
        raise ValueError("origin/%s is a different commit than the local branch: push it (or pass --push), then run this again" % head)
    if apply and not remote and not push:
        raise ValueError("the head branch is not known on origin from here: push it (or pass --push), then run this again")
    return local, ("origin/%s equals the local branch" % head if local and local == remote else
                   "origin/%s will be pushed to" % head if push else
                   "origin/%s is not known here (apply refuses without --push)" % head if not remote else "")


def push_branch(root, head):
    """`git push -u origin refs/heads/<head>:refs/heads/<head>` (list form, scrubbed env). ValueError (fixed text) on any failure."""
    check_branch("head", head)
    argv = config._argv(root, ["push", "-u", "origin", "refs/heads/%s:refs/heads/%s" % (head, head)])
    try:
        r = subprocess.run(argv, check=False, capture_output=True, stdin=subprocess.DEVNULL, timeout=PUSH_TIMEOUT, env=config.git_env())
    except FileNotFoundError:
        raise ValueError("`git` is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise ValueError("`git push` did not answer within %d s; no PR was created" % PUSH_TIMEOUT) from None
    except (OSError, subprocess.SubprocessError):
        raise ValueError("`git push` could not be run; no PR was created") from None
    if r.returncode:
        raise ValueError("`git push` failed (exit code %d); no PR was created" % r.returncode)


def gh_create_args(title, base, head, draft=False):
    """The `gh` argv: every value its own item after a flag that takes it; no value can be read as an option, and none is a secret."""
    return ["gh", "pr", "create", "--title", title, "--body-file", "-", "--base", base, "--head", head] + (["--draft"] if draft else [])


_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")
_PULL = re.compile(r"/[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}/pull/[1-9][0-9]{0,9}\Z")


def pr_url(out, env=None):
    """The pull request URL in `gh`'s output (its last non-empty line), or ValueError(E_URL): https, github.com or the configured GH_HOST,
    no credentials, port, query or fragment, a /owner/repo/pull/N path."""
    env = gh_env() if env is None else env
    lines = [x.strip() for x in out.splitlines() if x.strip()]
    url = lines[-1] if lines else ""
    hosts = {"github.com"}
    if _HOST.match(env.get("GH_HOST", "")):
        hosts.add(env["GH_HOST"].lower())
    try:
        u = urlsplit(url)
        ok = (len(url) <= 300 and url.isascii() and url.isprintable() and u.scheme == "https" and u.hostname in hosts
              and "@" not in u.netloc and u.port is None and not u.query and not u.fragment and _PULL.match(u.path))
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(E_URL)
    return url


def gh_create(title, body, base, head, draft=False, cwd="."):
    """Run `gh pr create` once (never retried) and return the validated URL. ValueError with a fixed text otherwise; the exit code
    and at most one cleaned line of `gh`'s error text are shown."""
    env = gh_env()
    try:
        r = subprocess.run(gh_create_args(title, base, head, draft), input=body.encode("utf-8"), capture_output=True, timeout=GH_TIMEOUT,
                           env=env, cwd=cwd, check=False)
    except FileNotFoundError:
        raise ValueError(E_NO_GH) from None
    except subprocess.TimeoutExpired:
        raise ValueError(E_GH_TIMEOUT) from None
    except (OSError, subprocess.SubprocessError):
        raise ValueError(E_GH_RUN) from None
    if r.returncode:
        err = [x for x in (r.stderr or b"")[:MAX_OUT].decode("utf-8", "replace").splitlines() if x.strip()]
        raise ValueError("`gh pr create` failed with exit code %d%s; no retry was made" % (r.returncode, (": " + clean(_redact(err[-1]), 200)) if err else ""))
    return pr_url((r.stdout or b"")[:MAX_OUT].decode("utf-8", "replace"), env)


# ---- the review at the head commit and the ledger-only comparisons (L5b; the fork's review_blocks / spec_dir_in_tree / only_ledger_since) ----
# Every value here comes from the PR (the ledger's `Spec dir:`, the review heading's SHA, file names in a diff): git reads only, with
# config's argv and environment, `--end-of-options` before a revision, paths after `--`; failures are `_Refuse` (fixed texts), never a pass.
class _Refuse(Exception):
    """A reason the review check (or a ledger-only comparison) cannot pass."""


_SHA_HEADING = re.compile(r"@ `([^`]*)`")
_SHA_TEXT = re.compile(r"[0-9a-fA-F]{7,40}")
_RERUN = ": run speckit.jira-lifecycle.review again"
MAX_LEDGER = 1 << 20  # bytes of a committed ledger read from git
MAX_DIFF = 8 << 20  # bytes of one `git diff-tree --name-only` listing; more is a refusal, never a partial answer


def _prefix(repo):
    """The repository subdirectory `repo` is (`git rev-parse --show-prefix`, "" at the top), or _Refuse."""
    out = config._out(repo, "rev-parse", "--show-prefix")
    if out is None:
        raise _Refuse("cannot check the review: not a git repository, or git could not be run")
    return out.decode("utf-8", "replace").strip()


def _spec_rel(spec_dir, prefix):
    """The ledger's untrusted `Spec dir:` as a repo-root-relative posix path, or None: relative, no `..`, no control character, not
    `.`, not the ledger dir `dod` or inside it."""
    if not isinstance(spec_dir, str) or not spec_dir.strip() or config._has_control(spec_dir):
        return None
    p = spec_dir.strip().replace("\\", "/")
    if PurePosixPath(p).is_absolute() or ".." in PurePosixPath(p).parts:
        return None
    rel = posixpath.normpath(prefix + p)  # the prefix is "" or ends in "/"
    dod_rel = prefix + "dod"
    if rel in ("", ".") or rel == dod_rel or rel.startswith(dod_rel + "/"):
        return None
    return rel


def spec_dir_in_tree(spec_dir, sha, repo="."):
    """The ledger's `Spec dir:` (untrusted) as a repo-root-relative path when commit `sha` (40 hex) holds a regular-file (not symlink)
    spec.md or tasks.md directly in it, else None. _Refuse when git cannot answer."""
    rel = _spec_rel(spec_dir, _prefix(repo))
    if rel is None or not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha)):
        return None
    want = {rel + "/spec.md", rel + "/tasks.md"}
    out = config._out(repo, "ls-tree", "--full-tree", "-z", "--end-of-options", sha, "--", *sorted(want))
    if out is None:
        raise _Refuse("cannot check the review: git could not list the spec dir")
    for entry in filter(None, out.split(b"\0")):  # "<mode> <type> <sha>\t<path>"; tree paths never follow symlinks
        meta, _, name = entry.partition(b"\t")
        if name.decode("utf-8", "replace") in want and meta.split()[:2] in ([b"100644", b"blob"], [b"100755", b"blob"]):
            return rel
    return None


def _changed_between(repo, base_sha, head_sha):
    out = config._git_capped(repo, MAX_DIFF, "diff-tree", "-r", "-z", "--name-only", "--no-renames", "--no-commit-id", "--end-of-options",
                             base_sha, head_sha, "--")  # read to MAX_DIFF + 1 bytes at most; more is None
    if out is None:
        raise _Refuse("cannot check the review: git could not compare the commits")
    return [n for n in out.decode("utf-8", "replace").split("\0") if n]


def _outside_ledger(repo, base_sha, head_sha, spec_dir, key, whole_dirs):
    """The files changed from commit `base_sha` to `head_sha` that are not the story's ledger material. `whole_dirs` (reviews): anything
    under `dod/` or the valid spec dir. Else (verification records, fidelity verdicts) exactly the ledger, the decisions file and
    `<spec dir>/tasks.md`. The spec dir is the one valid at `base_sha`, or one that is new since (it is committed after the review)."""
    prefix = _prefix(repo)
    names = _changed_between(repo, base_sha, head_sha)
    spec = spec_dir_in_tree(spec_dir, base_sha, repo)
    if spec is None:
        new = spec_dir_in_tree(spec_dir, head_sha, repo)
        if new and config._out(repo, "ls-tree", "--full-tree", "-z", "--end-of-options", base_sha, "--", new) == b"":
            spec = new
    dod_rel = prefix + "dod"
    if whole_dirs:
        return [f for f in names if not any(f.startswith(p + "/") for p in (dod_rel, spec) if p)]
    allowed = {f"{dod_rel}/{keys.dod_name(key)}", f"{dod_rel}/Decisions_{key}.md"} | ({f"{spec}/tasks.md"} if spec else set())
    return [f for f in names if f not in allowed]


def only_ledger_since(repo, base_sha, head_sha, spec_dir, key):
    """True when everything changed from commit `base_sha` to `head_sha` is the story's ledger, its decisions file or the valid spec
    dir's tasks.md (the exact files, never a directory's worth). _Refuse when git cannot answer. The one implementation strict uses."""
    return not _outside_ledger(repo, base_sha, head_sha, spec_dir, key, whole_dirs=False)


def _head_sha(repo, head, squash, at_commit):
    """(full commit id, branch name or None when detached) of the head the review is judged at."""
    if at_commit:
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise _Refuse("cannot check the review: the head is not a full commit id")
        full = config._rev(repo, head)
        if not full:
            raise _Refuse("cannot check the review: the head commit is not in this repository")
        return full, None
    if head == "HEAD":
        r = config._git(repo, "symbolic-ref", "--quiet", "HEAD")
        if r is None:
            raise _Refuse("cannot check the review: git could not be run")
        if r.returncode == 1 and squash:  # a CI checkout of the merged commit: there is no branch to compare
            full = config._rev(repo, "HEAD")
            if not full:
                raise _Refuse("cannot check the review: HEAD does not resolve to a commit")
            return full, None
        ref = r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
        if not ref.startswith("refs/heads/"):
            raise _Refuse("cannot check the review: HEAD is not on a branch; pass a branch")
        head = ref[len("refs/heads/"):]
    if not _BRANCH.fullmatch(head) or ".." in head or "@{" in head:
        raise _Refuse("cannot check the review: the head is not a plain branch name")
    full = config._rev(repo, "refs/heads/" + head)  # explicit refs/heads/: a tag named like the branch never shadows it
    if not full:
        raise _Refuse("cannot check the review: the head branch is not a commit here")
    return full, head


def _committed_ledger(repo, sha, prefix, key):
    try:
        raw = config._blob_at(repo, sha, f"{prefix}dod/{keys.dod_name(key)}", MAX_LEDGER, "the ledger")
    except config.ConfigInvalid:
        raise _Refuse("cannot check the review: the committed ledger is not a regular file of at most 1 MiB, or git could not read it") from None
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Refuse("cannot check the review: the committed ledger is not UTF-8") from None


def review_blocks(key, block_on, head, repo=".", push=False, squash=False, at_commit=False):
    """Why the review stops the PR under `block_on` (`review_block_on` in force), or None. Empty `block_on`: None (advisory). Blocking is
    opted into, so missing information refuses. The ledger is read from the head COMMIT, never the working tree; its verdict and counts
    through `review_summary` / `review_refusal`; then (unless `squash`: `done` after a squash merge, where the reviewed commit is no
    ancestor) the commit staleness: the review's `@ \\`<sha>\\`` must be a commit (object lookup, never a ref) that is an ancestor of the
    head, and later commits may change only files under `dod/` or the spec dir. Without `push` a branch head must equal origin/<head>
    when that is known (the PR opens from the remote branch). `at_commit`: `head` is a full commit id (CI). Fixed texts only."""
    if not block_on:
        return None
    try:
        _check_review(key, block_on, head, repo, push, squash, at_commit)
    except _Refuse as e:
        return str(e)
    return None


def _check_review(key, block_on, head, repo, push, squash, at_commit):
    prefix = _prefix(repo)
    head_sha, name = _head_sha(repo, head, squash, at_commit)
    if not push and name is not None:
        remote = config._rev(repo, "refs/remotes/origin/" + name)
        if remote and remote != head_sha:
            raise _Refuse("the remote branch origin/<head> differs from the reviewed local branch: push, or review again")
    md = _committed_ledger(repo, head_sha, prefix, key)
    # deliberate deviation: the fork's `## 2.` count/fence-parity refusal is not ported; `_log` refuses any unclosed fence or comment (the ledger is PR-authored)
    if md is None:
        raise _Refuse("no review logged: the head commit has no committed ledger" + _RERUN)
    rev = review_summary(md)
    why = review_refusal(rev, block_on)
    if why:
        raise _Refuse(why)
    if squash:
        return
    sha_m = _SHA_HEADING.search(_block(_log(md), "Review", live=True)[0])
    if not sha_m or not _SHA_TEXT.fullmatch(sha_m.group(1)):
        raise _Refuse("the latest review records no commit (`@ `<sha>`` of 7 to 40 hex characters)" + _RERUN)
    full = config._commit(repo, sha_m.group(1))  # object lookup only: a branch or tag named like the short SHA never stands in
    if full is None:
        raise _Refuse("the review's commit is ambiguous or not a commit here" + _RERUN)
    anc = config._git(repo, "merge-base", "--is-ancestor", full, head_sha)
    if anc is None or anc.returncode:
        raise _Refuse("the review is stale: its commit is not an ancestor of the head" + _RERUN)
    old = None
    try:
        old = _committed_ledger(repo, full, prefix, key)
    except _Refuse:
        old = None
    spec = dod.ledger_spec_dir(old) if old else None  # the spec dir as the reviewed commit had it: a later commit cannot widen it
    outside = _outside_ledger(repo, full, head_sha, spec.strip() if spec else None, key, whole_dirs=True)
    if outside:
        raise _Refuse("the review is stale: later commits change %d file(s) outside the ledger and the spec dir%s" % (len(outside), _RERUN))
