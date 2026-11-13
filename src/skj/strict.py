"""`skj strict check`: the rules of strict SDD+TDD mode. Read-only: it never writes and never runs tests (`run_at` below is
the L2 test runner, called only by `verify`, which writes the verification record `check` validates).

Ported from the fork source's strict.py (task L5a: the core). Each phase is its own list of rule functions in PHASES; a rule
takes a _Ctx and returns findings `{"rule", "detail", "fix"}`. The ledger and the spec dir come from the PR in CI, so every
file is read through gate._read (regular, <= 1 MiB, confined to the repo, never a symlink), and a `detail` carries only ids,
file names and counts: never ledger, spec or Jira text.

The board (`push`), kits (`kits`) and review (`prs.review_blocks`) checks are real
since L5b; the board check is the only phase rule that reads Jira (the story's subtasks). The business-rules check (`_rules`, RU3) is
offline: the live drift check is the gate's. The done-time cross-check
of pinned comment approvals (`xcheck_run`, A3) is called by `skj done` only.

`verify` runs the PR's test command. On Linux that code can read the environment of its parent `skj` process through
/proc/<ppid>/environ (same uid; scrubbing the child's env does not hide the parent's), so `verify` refuses (the blocking finding
`credentials-in-env`, variable NAMES only) and runs nothing while this process's environment holds SKJ_TOKEN, SKJ_EMAIL or any
name the runner's deny rule (`_denied`) drops. CI runs `verify` in a step or job without credentials."""
import hashlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

from . import approvals, config, decisions, dod, gate, keys, kits, prs, push, questions, rules, stack, tasks
from .config import Config, ConfigInvalid, _has_control

_PLACEHOLDER = re.compile(r"_\(\s*fill\s+in", re.I)
_BULLETS = (("Approach", "Approach"), ("Assumptions", "Assumptions"), ("Use cases & edge cases", "Use cases"))
_LABEL = re.compile(r"^\s{0,3}[-*+]\s+\*\*[^*]*\*\*")
_NEXT = re.compile(r"[-*+]\s|#")  # a column-0 bullet or heading ends the bullet being read
_BULLET_AT = {label: re.compile(r"\s{0,3}[-*+]\s+\*\*" + re.escape(label)) for _, label in _BULLETS}
PROFILE_LABEL = "skj.stack.json at the target branch"
LOCK_FILE = "skj.kits.lock.json"
LOCK_LABEL = LOCK_FILE + " at the target branch"
HEAD_LABEL = "skj.stack.json at the head commit"
TARGET_CONFIG_NOTE = "target branch (strict)"  # `result["config"]` when CI judged the PR with the target branch's config
PR_CONFIG_NOTE = "pull request"  # ... or with the PR's own strict config, when a base was given
TRUST_NOTE = "trust-introducing PR: review skj.stack.json as code"  # this PR edits the profile, the lockfile or strict.test_command
KITS_TRUST_NOTE = "trust-introducing PR: review skj.kits.lock.json as code"  # this PR adds the first kits lockfile (the target has none)


def _finding(rule, detail, fix):
    return {"rule": rule, "detail": detail, "fix": fix}


# ---- the fail-closed stubs (L5a-AC3): a check whose module is not built yet is a blocking finding, named per check ----
# rule -> (what the check judges, the later task that replaces the stub). Never overridable (not in _COVERS), never skipped.
STUBS = {}  # A3 replaced the cross-check stub (`xcheck_run`), RU3 the business-rules stub (`_rules`)
DEGRADED = {}  # internals that fail closed without a finding of their own (L5a: prs.spec_dir_in_tree, real since L5b)


def _unavailable(rule):
    what, task = STUBS[rule]
    return _finding(rule, f"{what} is not available in this build", f"it arrives with {task}; until then strict mode cannot pass it")


class _NoClient(Exception):
    """The board check was given no client (`check(client_factory=None)`)."""


_BOARD_FIX = "check access with `skj doctor`, then re-run: strict mode needs the board to be readable"


def _board_ops(c, parsed):
    """`push`'s ops for tasks.md against the story's subtasks (reads only: the story's subtask list and each listed subtask; no
    transition or issue-type read, so a ticked task's transition is not looked up). _NoClient when this run was given no client."""
    if c.client_factory is None:
        raise _NoClient
    return push.build_ops(parsed, push.read_children(c.client_factory(), c.id), lambda ch: None)


def _board(c):
    """`board-task-missing`: a tasks.md task with no subtask on the story (`skj push` would create it); `board-unchecked`: the board
    could not be read (no client, Jira down, a malformed answer: the exception's class name only), or tasks.md cannot be pushed."""
    text = c.file("tasks.md")
    if text is None:
        return []  # already reported as tasks-missing
    parsed = tasks.parse_tasks(text)
    if not parsed:
        return [_finding("tasks-missing", "tasks.md lists no tasks", "run speckit.jira-lifecycle.tasks")]
    ids = [t["id"] for t in parsed]
    if len(set(ids)) != len(ids):
        return [_finding("board-unchecked", "tasks.md has duplicate task ids, so the board was not checked",
                         "make every task id in tasks.md unique, then re-run")]
    try:
        ops = _board_ops(c, parsed)
    except _NoClient:
        return [_finding("board-unchecked", "no Jira client was given to this run, so the board was not read", _BOARD_FIX)]
    except Exception as e:  # noqa: BLE001 - anything that stops the read is a finding, never a pass
        return [_finding("board-unchecked", f"the board could not be read ({e.__class__.__name__})", _BOARD_FIX)]
    n = sum(op["action"] == "create" for op in ops)
    if not n:
        return []
    return [_finding("board-task-missing", f"{n} task(s) in tasks.md have no subtask on the story", f"skj push {c.id} --apply")]


_REVIEW_FIX = "run speckit.jira-lifecycle.review, fix what it blocks on, commit the ledger"


def _review_why(c):
    return prs.review_blocks(c.id, c.cfg.effective_review_block_on(), c.review_head, c.repo, push=c.push, squash=c.phase == "done",
                             at_commit=c.at_commit)


def _review(c):
    """`review-missing` / `review-blocks` from prs.review_blocks with the strict review list (the project's plus `blocker`), read at the
    head commit. The reason it returns is a fixed text, but the finding carries its own: `skj pr create` names the exact one."""
    if c.ledger is None:
        return []  # reported by _ledger
    why = _review_why(c)
    if why is None:
        return []
    if why.startswith("no review"):
        return [_finding("review-missing", "no review is logged in ledger section 4 of the head commit", _REVIEW_FIX)]
    return [_finding("review-blocks", "the latest review blocks the PR, is stale, or could not be checked (git or ledger problem)", _REVIEW_FIX)]


# ---- business rules (RU3): `rules.coverage` over the PR's own files, offline; none of these can be overridden (not in _COVERS) ----
_SAFE = re.compile(r"[A-Za-z0-9:?_-]{1,60}")  # rule ids, issue keys and problem keys only; anything else is shown as `?`
_MAX_IDS = 20


def _ids(items):
    shown = [str(i) if _SAFE.fullmatch(str(i)) else "?" for i in items[:_MAX_IDS]]
    return ", ".join(shown) + (" and %d more" % (len(items) - _MAX_IDS) if len(items) > _MAX_IDS else "")


def _rules_fixes(key):
    """The fixed fix text of each rules finding: the commands to run, never any text from a file."""
    return {
        "rule-uncovered": ("tag the AC row that implements the rule (`[BR-<KEY>-<n>]` in its criterion cell, with a test that exists); if no AC "
                           "reflects it the story's AC are extended in Jira, then `skj dod refresh %s`; or the PO validates a posted "
                           "`out of scope: BR-<KEY>-<n>` decision (`skj decisions %s --add`, then `--post`); never overridable" % (key, key)),
        "rule-unknown": ("correct or remove the tag in the AC row; if the rule exists in Jira, pull it with `skj rules %s` and commit the file; "
                         "a tag is exactly `BR-<issue key>-<number>`" % key),
        "rule-unconfirmed": ("read the rule in its rules file, then run `skj rules %s --confirm` yourself (the first confirm of a new rule is "
                             "free: no terminal needed)" % key),
        "rule-changed": ("STOP: a human re-confirms it with `skj rules %s --confirm` in their own terminal after reading the change in the rules "
                         "file (re-pull with `skj rules %s` first if the file was edited by hand); re-confirming a changed or dropped hash is "
                         "never the agent's" % (key, key)),
        "rules-problem": "fix the Business rules section of the parent issue in Jira, then `skj rules %s` and commit the file" % key,
        "rules-bad-exclusion": ("add a correct line, exactly `out of scope: BR-<KEY>-<n>[, BR-<KEY>-<n>]`, with `skj decisions %s --add` and post "
                                "it with `--post` (`skj decisions %s` shows the lines; never edit a line the PO answered); a malformed line "
                                "excludes nothing" % (key, key)),
        "rules-missing": "pull the rules with `skj rules %s` and commit the file(s)" % key,
        "rules-format": "re-pull the file with `skj rules %s` (it is in another rules format) and commit it" % key,
        "rules-from": "`skj dod refresh %s` writes the missing or malformed `Rules from:` line" % key,
        "rules-reviewed": "repair the malformed `Rules reviewed:` line by hand, then a human runs `skj rules %s --confirm` in their own terminal" % key,
    }


def _rules(c):
    """The offline business-rules findings of `rules.coverage`, only with `rules.enabled` (the target branch's config in CI, like every strict
    rule): `rule-uncovered`, `rule-unknown`, `rule-unconfirmed` (no recorded hash yet: free to confirm), `rule-changed`, `rules-problem` (an
    error problem of a file, or a malformed `out of scope:` line named by its D id), `rules-missing`, `rules-format`, `rules-header`. Inputs
    are the ones `skj gate` uses. A detail carries ids, issue keys and counts only, never rule or ledger text."""
    if not c.cfg.rules["enabled"] or c.ledger is None:  # a missing ledger is reported by _ledger
        return []
    files = rules.local_files(c.ledger, c.repo)
    sources = gate.discover_tests(c.repo, {**gate.DEFAULT_TESTS, **c.cfg.gate["tests"]}, set())
    cov = rules.coverage(c.ledger, sources, files, dod.ledger_rules_from(c.ledger), dod.ledger_rules_reviewed(c.ledger), c.decisions, c.phase)
    fix, out = _rules_fixes(c.id), []
    if cov["uncovered"]:
        out.append(_finding("rule-uncovered", "rule(s) that bind the story are cited by no AC row with a test that exists, and no counted "
                            "exclusion lifts them: " + _ids(cov["uncovered"]), fix["rule-uncovered"]))
    named = [r for r in cov["unknown"] if r != "?"]
    if cov["unknown"]:
        out.append(_finding("rule-unknown", "cited rule id(s) found in no rules file: %s; malformed `BR-` tag(s): %d"
                            % (_ids(named) or "none", len(cov["unknown"]) - len(named)), fix["rule-unknown"]))
    if cov["unconfirmed"]:
        out.append(_finding("rule-unconfirmed", "rule(s) in scope not confirmed yet: " + _ids(cov["unconfirmed"]), fix["rule-unconfirmed"]))
    if cov["changed"]:
        out.append(_finding("rule-changed", "rule(s) changed since confirmed, or whose text no longer matches the hash in their heading: "
                            + _ids(cov["changed"]), fix["rule-changed"]))
    bad = rules.scope({}, [], c.decisions, c.phase)["bad_exclusions"]
    if cov["problems"] or bad:  # one finding, so the id shows once in the refusal text
        out.append(_finding("rules-problem", "; ".join(
            (["error problem(s) in a rules file (<id>:<key>): " + _ids(cov["problems"])] if cov["problems"] else []) +
            (["malformed `out of scope:` decision line(s), which exclude nothing: " + _ids(bad)] if bad else [])),
            " / ".join(([fix["rules-problem"]] if cov["problems"] else []) + ([fix["rules-bad-exclusion"]] if bad else []))))
    if cov["missing"]:
        out.append(_finding("rules-missing", "no usable rules file for issue(s): " + _ids(cov["missing"]), fix["rules-missing"]))
    if cov["format"]:
        out.append(_finding("rules-format", "rules file(s) in another rules-format for issue(s): " + _ids(cov["format"]), fix["rules-format"]))
    if cov["header"]:
        out.append(_finding("rules-header", "malformed or missing ledger header line(s): " + _ids(cov["header"]),
                            "; ".join(fix[h] for h in cov["header"] if h in fix)))
    return out


# ---- the done-time cross-check (A3; EXPERIMENTAL comment approval, tamper-evident only: O5). `skj done` only, as the fork (its CI
# close step runs `done`); `strict check` and `verify` never read comments (`verify` must run without credentials, L5a). ----
MAX_PINS = 200  # pinned lines judged per run; more is `xcheck-too-many` before any request (a hostile ledger cannot cost thousands)
XC_TRUNC = "the comment list of the story looks truncated or changed while it was read; nothing closed (run again)"
_XC_REPOST = "re-post with `skj decisions <KEY> --post`, let an approver answer the new comment, then `skj decisions <KEY> --sync`"
_XC_FIX = {
    "xcheck-malformed": "repair the decisions file (`skj decisions <KEY>` shows what parses); the cross-check needs every line",
    "xcheck-too-many": "a human closes the story in Jira; the cross-check does not judge more pinned approvals than this",
    "xcheck-hand-resolved": "an approver comments `/approve D<n>` or `/reject D<n>`, then `skj decisions <KEY> --sync`; with approval "
                            "\"comment\" a hand-written verdict is not accepted",
    "xcheck-missing": "an approver comments the verdict again, then `skj decisions <KEY> --sync`",
    "xcheck-edited": "an approver comments the verdict again (a new comment, never an edit), then `skj decisions <KEY> --sync`",
    "xcheck-pin": "restore the line from git history, or run `skj decisions <KEY> --sync`; never edit a `via:` pin by hand",
    "xcheck-not-valid": "an approver named on the target branch comments the verdict again, then `skj decisions <KEY> --sync`",
    "xcheck-posting": _XC_REPOST,
    "xcheck-stale": "run `skj decisions <KEY> --sync` and check the thread: the line must be what the newest approver comment says",
    "xcheck-dropped": "restore the line from git history; never delete a posted decision, record a change as a new decision",
    "xcheck-truncated": "run it again; the cross-check needs the whole comment list",
    "xcheck-unreachable": "check access with `skj doctor`, then run it again: with approvers on the target branch, done needs the comments",
}
XC_RULES = tuple(_XC_FIX)


def _xc(rule, detail):
    return _finding(rule, detail, _XC_FIX[rule])


def _xc_lines(md, approval):
    """(parsed file or None, the findings a local read alone gives, the synced lines to re-read). No request. Synced = `via` AND
    `by: approver` (what `decisions --sync` writes). Another resolved line is a human's: it stands in terminal mode (a reviewed commit),
    with approval "comment" it is `xcheck-hand-resolved` (the fork's `verdict`)."""
    try:
        if type(md) is not str:  # the caller could not read it (a symlink, outside the repository, over 1 MiB, not UTF-8)
            raise ValueError
        parsed = decisions.check_sync_file(md)
    except ValueError:
        return None, [_xc("xcheck-malformed", "the decisions file cannot be read (a symlink, outside the repository, over 1 MiB or not "
                                              "UTF-8) or not line by line (a line that breaks the grammar, an unclosed fence or "
                                              "comment, or a repeated or non-ASCII id)")], []
    resolved = sorted(parsed["validated"] + parsed["rejected"], key=lambda d: (len(d["id"]), d["id"]))
    synced = [bool(d.get("via")) and d["by"] == decisions.SYNC_BY for d in resolved]
    pinned = [d for d, s in zip(resolved, synced) if s]
    out = [_xc("xcheck-hand-resolved", '%s: a verdict with no approver comment behind it (the target branch sets approval "comment")'
               % d["id"]) for d, s in zip(resolved, synced) if approval == "comment" and not s]
    if len(pinned) > MAX_PINS:
        out.append(_xc("xcheck-too-many", "more than %d pinned approvals: they are not checked one by one" % MAX_PINS))
    return parsed, out, pinned


def _xc_stale(d, win, pkey):
    """Why synced line `d` is not what `--sync` would write now (the fork's `verdict` rule), else None."""
    cid = d["via"].split(":")[0]
    if win is None or win["id"] != cid:
        return "not-newest"
    if decisions._VERDICT.get(win["verb"]) != d["status"]:
        return "verb"
    if win["tampered"]:
        return "newer-tampered"
    if win["bad_newer"]:
        return "bad-newer"
    if win["key"] <= pkey:
        return "older-than-posting"
    if d["note"] not in (decisions._sync_note(win["text"]), ""):
        return "note"
    return None


def xcheck(md, rows, approvers, excluded, me, approval):
    """Findings (XC_RULES, fixed texts with D ids, comment ids and fixed codes only, in file order) for the decisions file `md` against
    the story's comments `rows` (read COMPLETELY: the caller ran the probe past the end), the TARGET branch's `approvers` and `approval`,
    `excluded` (`approvals.excluded`) and the token user `me`. Per synced line, in this order (the first failing check only):
    `missing` (the pinned comment is not in the list: deleted or hidden), `edited` (created != updated or updateAuthor != author: checked
    BEFORE the pin, so a pin rewritten to an edited body still refuses), `pin` (the hash8 is not the first 8 hex of the body's digest, or
    the old id-only form), `not-valid` (`approvals.guard` fails now, e.g. the author left `approvers`), `posting` (the newest-posting rule
    of `--sync` fails now: the kit's posting is missing, edited, superseded, or no longer shows the ledger line), `stale` (the line is not
    what `--sync` would write now: another newest valid command, the other verb, a newer tampered or malformed approver comment, older
    than the posting, another note). With approval "comment": `dropped` for a D<n> the kit's newest postings list that the file lacks.
    A local finding (`_xc_lines`) is returned alone. ValueError propagates (the caller fails closed). Linear in rows and lines."""
    from . import approvals
    parsed, out, pinned = _xc_lines(md, approval)
    if out:
        return out
    b = approvals.board(rows, approvers, excluded)
    index = decisions.posting_index(rows, me)
    by_id = {r.id: r for r in rows}
    for d in pinned:
        cid, _, h8 = d["via"].partition(":")
        r = by_id.get(cid)
        if r is None:
            out.append(_xc("xcheck-missing", "approval comment %s is missing" % cid))
            continue
        if not approvals.unedited(r):
            out.append(_xc("xcheck-edited", "approval comment %s was edited since sync" % cid))
            continue
        if not h8 or type(r.digest) is not str or r.digest[:8] != h8:
            out.append(_xc("xcheck-pin", "approval comment %s does not match its pin" % cid))
            continue
        why = approvals.guard(r, approvers, excluded)
        if why:
            out.append(_xc("xcheck-not-valid", "approval comment %s no longer counts (%s)" % (cid, why)))
            continue
        rule, pkey = decisions._posting_rule(d, by_id, me, index)
        if rule:
            out.append(_xc("xcheck-posting", "%s: the kit's posting no longer backs the verdict (%s)" % (d["id"], rule)))
            continue
        stale = _xc_stale(d, approvals.newest(b, d["id"]), pkey)
        if stale:
            out.append(_xc("xcheck-stale", "%s: the verdict is not what --sync would write now (%s)" % (d["id"], stale)))
    if approval == "comment":
        have = decisions._ids(parsed)
        for i in sorted((i for i in index.best if i not in have), key=lambda x: (len(x), x)):
            out.append(_xc("xcheck-dropped", "%s: a decision the kit posted is missing from the decisions file" % i))
    return out


def xcheck_run(client_factory, cfg, key, md, approvers, approval):
    """`xcheck` over a fresh read, GETs only: the issue (a moved issue or another project refuses), `/myself`, every comment and the
    probe past the end (`questions._story`). The client is built only when there is something to check: a synced line, or approval
    "comment" (whose posted decisions must all still be in the file). Anything that stops the read is a finding, never a pass:
    `xcheck-truncated` for an incomplete list, else `xcheck-unreachable` with the exception's class name only."""
    from . import approvals, issues
    _, out, pinned = _xc_lines(md, approval)
    if out or not (pinned or approval == "comment"):
        return out
    try:
        c = client_factory()
        me, rows = questions._story(c, cfg, key, "nothing closed", XC_TRUNC)
        excluded = approvals.excluded(c, rows, me=me)
        return xcheck(md, rows, approvers, excluded, me, approval)
    except issues.ReadError as e:
        if str(e) == XC_TRUNC:
            return [_xc("xcheck-truncated", "not every comment of the story was read, so an edit or a missing approval cannot be ruled out")]
        return [_xc("xcheck-unreachable", "Jira could not be read for the cross-check (%s)" % e.__class__.__name__)]
    except Exception as e:  # noqa: BLE001 - as the board check: anything that stops the read is a finding, never a pass
        return [_xc("xcheck-unreachable", "Jira could not be read for the cross-check (%s)" % e.__class__.__name__)]


class Refused(ValueError):
    """A strict check that found something: a write verb exits 2 with these findings (`.check`) before it writes or sends."""

    def __init__(self, check):
        self.check = check
        super().__init__("strict check failed: " + "; ".join(f"{f['rule']} ({f['fix']})" for f in check["findings"]))


def require(check):
    """The check result when it is ok, else raises Refused."""
    if not check["ok"]:
        raise Refused(check)
    return check


# git: list argv, no shell, config.git_env() (no SKJ_* or token-named variable, no selector, replace refs off)
_rev, _out, _commit, _resolve_base, default_base = config._rev, config._out, config._commit, config._resolve_base, config.default_base


def _blob_at(repo, commit, path, cap, label):
    return config._blob_at(repo, commit, path, cap, label)  # looked up at call time: one reader for the config, the profile and the lock


class _Ctx:
    def __init__(self, cfg, key, repo, phase=None, head="HEAD", base=None, push=False, client_factory=None):
        if not keys.valid_key(key) or key.rsplit("-", 1)[0] != cfg.project:
            raise ValueError("not a key of project %s" % cfg.project)
        self.id, self.repo, self.cfg, self.phase, self.head = key, repo, cfg, phase, head
        self.push, self.base, self.client_factory = push, base, client_factory
        self.base_full = _resolve_base(repo, base) if base is not None else ""  # "" = no base given, or one that is no commit here
        # `head` is HEAD, a branch or a commit id (CI): review_head is what prs.review_blocks is given, rev the commit L1 and the
        # record are judged at. A hex id that is no branch becomes the full SHA (resolved by object id, never by a tag).
        self.review_head, self.rev, self.at_commit = head, head, False
        if head != "HEAD":
            full = _rev(repo, f"refs/heads/{head}")
            if not full and re.fullmatch(_HEX, head):
                full = _commit(repo, head) or ""
                self.review_head, self.at_commit = full or head, bool(full)
            self.rev = full or head
        self.ledger = gate._read(Path(repo) / "dod" / keys.dod_name(key), repo, set())
        spec_dir = dod.ledger_spec_dir(self.ledger) if self.ledger is not None else None
        self.spec_dir = spec_dir.strip() or None if spec_dir else None  # a CRLF ledger leaves `\r` on the header value
        self._files, self._stack, self._detect, self._kits = {}, None, None, None
        path = Path(repo) / "dod" / f"Decisions_{key}.md"
        md = gate._read(path, repo, set()) if os.path.lexists(path) else None  # confined like the ledger: no symlink, <= 1 MiB
        self.decisions_md = md or ""
        self.decisions = decisions.parse_decisions(self.decisions_md)
        self.decisions_ok = md is not None or not os.path.lexists(path)  # a missing file is no decisions; an unreadable one is a finding

    def file(self, name):
        """Text of <spec dir>/<name>, or None (no spec dir, missing, symlink, outside the repo, too big, not UTF-8)."""
        if self.spec_dir is None:
            return None
        if name not in self._files:
            self._files[name] = gate._read(Path(self.repo) / self.spec_dir / name, self.repo, set())
        return self._files[name]


def _ledger(c):
    if c.ledger is None:
        return [_finding("ledger-missing", "the ledger is missing or unreadable", f"skj dod seed {c.id}")]
    return []


_SPEC_FILES = (("spec.md", "spec-missing", "run speckit.specify (or the stack kit's specify command)"),
               ("plan.md", "plan-missing", "run speckit.jira-lifecycle.plan"),
               ("tasks.md", "tasks-missing", "run speckit.jira-lifecycle.tasks"))


def _spec_files(c):
    if c.ledger is None:
        return []
    out = []
    for name, rule, fix in _SPEC_FILES:
        if c.spec_dir is None:
            out.append(_finding(rule, f"the ledger records no Spec dir, so {name} cannot be found",
                                f"set the `Spec dir:` line in the ledger header, then {fix}"))
        elif c.file(name) is None:
            out.append(_finding(rule, f"{name} is missing or unreadable in the spec dir", fix))
    return out


def _bullet_problem(lines, label):
    """True when the section 2 bullet is absent, empty (nothing but its label) or still holds `_(fill in`."""
    i = next((i for i, line in enumerate(lines) if _BULLET_AT[label].match(line)), None)
    if i is None:
        return True
    text = [_LABEL.sub("", lines[i])]
    for line in lines[i + 1:]:
        if _NEXT.match(line):
            break
        text.append(line)
    body = "\n".join(text)
    return not body.strip() or _PLACEHOLDER.search(body) is not None


def _placeholders(c):
    if c.ledger is None:
        return []
    lines = dod._visible(dod.section(c.ledger, 2) or "")[0].splitlines()  # fences and comments render as nothing
    bad = [name for name, label in _BULLETS if _bullet_problem(lines, label)]
    if not bad:
        return []
    return [_finding("placeholder", f"ledger section 2 is missing, empty or still a placeholder for: {', '.join(bad)}",
                     "fill them in with speckit.jira-lifecycle.plan; no `_(fill in` may be left in Approach, Assumptions or "
                     "Use cases & edge cases")]


def _open_questions(c):
    if c.ledger is None:
        return []
    n = len(questions.parse_questions(c.ledger)["open"])  # an `assumed:` answer is closed; an unclosed fence counts as open
    if not n:
        return []
    return [_finding("open-question", f"{n} open question(s) in ledger section 2",
                     f"get them answered (skj questions {c.id} --post), or record the answer as `assumed: ...` and tick the line")]


def _override_l2(c):
    # 0.1 has no override command: `overrides` honours a hand-written line of kind override, posted like any decision
    return (f"a human adds the line `- [ ] D<n>: skip l2 → _(open)_ · kind: override · why: <why>` to dod/Decisions_{c.id}.md "
            f"(n: the next free number) and posts it (`skj decisions {c.id} --post`)")


_APPROVE = "a human runs `skj stack approve --apply` in their own terminal window (`skj stack approve` previews it)"


def _test_command_fix(c):
    """Where a profile exists, strict.test_command is the profile's (a hand edit is drift): approval sets it, or a human waives the rule."""
    res = stack.profile_for(c.cfg, c.repo)
    if res["status"] == "none":
        return "set strict.test_command in skj.config.json"
    try:
        sets = res["status"] == "approved" and stack.compose_test_command(res["profile"]) is not None
    except ValueError:
        sets = False
    return (f"{_APPROVE} to set it from the profile, or " if sets else "") + _override_l2(c)


def _test_command(c):
    if c.cfg.strict.get("test_command"):
        return []
    return [_finding("test-command", "strict.test_command is not set", _test_command_fix(c))]


# ---- stack profile rules: `skj.stack.json` must be approved, unchanged since, and agree with strict.test_command ----
# In CI (cfg.from_target) the profile is the TARGET branch's blob (read once by read_target_trust), never the PR's file; a local config
# reads the working tree. Not overridable (not in _COVERS): an unknown or unapproved stack stops the run. Process enforcement, not a sandbox.
def read_target_trust(cfg, repo):
    """For a strict config taken from the target branch (`cfg.from_target`): reads `skj.stack.json` and `skj.kits.lock.json` at
    `cfg.source_commit` through `_blob_at` (256 KiB each) into `cfg.target_profile_blob` / `cfg.target_lock_blob` (None when absent) and
    raises ConfigInvalid, with fixed texts that carry none of the file, when either is unreadable or invalid. A draft or changed profile
    is valid here (the rule refuses it); the lockfile is judged by `stack.lock_error`."""
    raw = _blob_at(repo, cfg.source_commit, stack.PROFILE_FILE, stack.MAX_PROFILE, PROFILE_LABEL)
    if raw is not None:
        res = stack.read_profile(blob=raw)
        if res["status"] == "invalid":
            raise ConfigInvalid(f"{PROFILE_LABEL}: the profile is invalid ({len(res['errors'])} error(s)); fix it on the target branch")
    cfg.target_profile_blob = raw
    raw = _blob_at(repo, cfg.source_commit, LOCK_FILE, stack.MAX_PROFILE, LOCK_LABEL)
    why = stack.lock_error(raw) if raw is not None else None
    if why:
        raise ConfigInvalid(f"{LOCK_LABEL}: the lockfile {why}")
    cfg.target_lock_blob = raw


_UNREADABLE = object()  # a head file that cannot be read (symlink, directory, oversize, git failure): never equal to the target's bytes


def _head_blob(repo, head, path, label):
    try:
        return _blob_at(repo, head, path, stack.MAX_PROFILE, label) if head else _UNREADABLE
    except ConfigInvalid:
        return _UNREADABLE


def _head_trust(cfg, repo, head):
    """For a strict target config: what the head commit holds of the trust files, against the target's. `{"profile": read_profile-like dict,
    "differs": the profile or strict.test_command differs from the target's, "changed": that or the lockfile differs, "lock": the head's lockfile
    (bytes, None when absent, _UNREADABLE), "cmd": the head config's strict.test_command, "config_invalid": the head's skj.config.json
    exists but cannot be read or fails validation}`. An absent head config counts as no command; an unreadable file as different."""
    raw = _head_blob(repo, head, stack.PROFILE_FILE, HEAD_LABEL)
    lock = _head_blob(repo, head, LOCK_FILE, LOCK_FILE + " at the head commit")
    try:
        hc, bad = (Config.load_from_ref(head, repo) if head else None), False
    except ConfigInvalid:  # present but unreadable or invalid (the text is never kept): `config-invalid`, not "no command"
        hc, bad = None, True
    cmd = hc.strict.get("test_command") if hc is not None else None
    if raw is _UNREADABLE:
        res = {"status": "unreadable", "profile": None, "errors": []}
    else:
        res = stack.read_profile(blob=raw) if raw is not None else {"status": "none", "profile": None, "errors": []}
    differs = raw != cfg.target_profile_blob or cmd != cfg.strict.get("test_command")
    return {"profile": res, "lock": lock, "differs": differs, "changed": differs or lock != cfg.target_lock_blob, "cmd": cmd,
            "config_invalid": bad}


def _stack_note(cfg, trust):
    """Whether the stack note applies to `_head_trust` result `trust`: the head differs from the target in the profile, `strict.test_command` or
    the lockfile, except that the first lockfile (the target has none) carries its own note instead (KITS_TRUST_NOTE, `_kits_ci`)."""
    return trust["differs"] or (trust["changed"] and not (cfg.target_lock_blob is None and trust["lock"] is not None))


def edits_trust_files(cfg, repo, commit="HEAD"):
    """True when `commit` (the PR head) differs from the target branch in `skj.stack.json`, `skj.kits.lock.json` or `strict.test_command`
    (only for a config taken from the target): the PR comment and `warnings` say to review it as code."""
    full = _rev(repo, commit) if cfg.from_target else ""
    return bool(full) and _stack_note(cfg, _head_trust(cfg, repo, full))


def adoption_note(cfg, repo, base, commit="HEAD"):
    """True for a run under the PR's OWN strict config (`cfg.from_target` False) when `base` (the PR's target ref) holds no strict config:
    this PR turns strict mode on, so its `skj.stack.json` and `strict.test_command` are trust files nobody has reviewed as code. False for a
    target-judged run, an advisory run, no `base` or one that is no commit here. A target config that cannot be read counts as no strict
    config (a note too many, never one too few). (The fork's kits half of this note is not ported: see task-L5b-report.md.)"""
    full = _resolve_base(repo, base) if base and not cfg.from_target and cfg.mode == "strict" else ""
    if not (full and _rev(repo, commit)):
        return False
    try:
        target = Config.load_from_ref(full, repo)
    except ConfigInvalid:
        target = None
    return not (target is not None and target.mode == "strict")  # a strict target judges; `edits_trust_files` covers its PRs


def _stack_state(c):
    """The profile that judges this run, cached: `{"bootstrap", "status", "profile", "errors"}` (the target's blob in CI, else the working
    tree) plus, in CI, `"head"`: `_head_trust` of the head commit."""
    if c._stack is None:
        cfg = c.cfg
        c._stack = {"bootstrap": bool(cfg.from_target and cfg.target_profile_blob is None), **stack.profile_for(cfg, c.repo), "head": None}
        if cfg.from_target:
            c._stack["head"] = _head_trust(cfg, c.repo, _rev(c.repo, c.rev))
    return c._stack


def _no_test_fix(c):
    return f"remove strict.test_command from skj.config.json ({_override_l2(c)} waives the unset command), or add a test command to the profile; then {_APPROVE}"


_DRAFT_IT = ("draft it for every detected stack: run `skj stack init --seed <a> --seed <b>` (one --seed per stack), "
             "or edit a draft and use `--from` (or run speckit.jira-lifecycle.stack)")
_APPROVE_IT = f"review the draft; then {_APPROVE}"  # a draft exists: it needs approval, not another draft
_CONFIG_BAD = ("skj.config.json at the head commit exists but cannot be read, or is invalid: merged, it would fail every later run",
               "fix skj.config.json in the PR; any `skj` command run on the PR branch names the problem")


def _head_findings(c, s):
    """`config-invalid` when the head's skj.config.json is present but invalid (its text is never echoed), then `_head_profile_findings`."""
    h = s["head"]
    bad = [_finding("config-invalid", *_CONFIG_BAD)] if h and h["config_invalid"] else []
    return bad + _head_profile_findings(c, s)


def _drift_findings(cmd, composed, too_long, at, c):
    """The `stack-profile-drift` finding of `cmd` (strict.test_command) against the command the profile `at` composes, or []."""
    if too_long:
        return [_finding("stack-profile-drift", f"strict.test_command{at[1]} differs from the command {at[0]} composes (which is over 500 characters)"
                         if at[1] else "the command the approved profile composes is longer than 500 characters",
                         f"{_APPROVE} to set it from the profile; do not hand-edit it" if at[1]
                         else "point `test` at a repo script (kind `repo`) in the profile, then re-approve it")]
    if (cmd or None) == composed:  # no test command in the profile: any command in the config is a hand edit
        return []
    if composed is None:
        return [_finding("stack-profile-drift", f"strict.test_command{at[1]} is set, but {at[0]} {stack.N_NO_TEST}", _no_test_fix(c))]
    return [_finding("stack-profile-drift", f"strict.test_command{at[1]} differs from the command {at[0]} composes",
                     f"{_APPROVE} to set it from the profile; do not hand-edit it")]


def _composed(profile):
    try:
        return stack.compose_test_command(profile), False
    except ValueError:
        return None, True


def _head_profile_findings(c, s):
    """The head commit's copy, which becomes the target's on merge: when it differs from the target's (or the head config's
    strict.test_command does), it must itself be a valid, approved profile that agrees with the head's strict.test_command. Only adds
    findings; the target's verdict is never replaced. A head without a profile is fine at bootstrap (nothing to lock)."""
    h = s["head"]
    if h is None or not h["differs"]:
        return []
    st, at = h["profile"]["status"], HEAD_LABEL
    n = len(h["profile"]["errors"])
    if st in ("unreadable", "invalid"):
        return [_finding("stack-profile-invalid", f"{at} cannot be read, or is invalid" + ("" if st == "unreadable" else f" ({n} error(s))"),
                         "run `skj stack show` for the error positions and fix the file: once merged it would fail every later PR")]
    if st == "none":
        return [] if s["bootstrap"] else [_finding("stack-profile-missing", f"{at} is missing: the PR deletes the profile",
                                                  f"restore it, or {_DRAFT_IT}; then {_APPROVE}")]
    if st == "draft":
        return [_finding("stack-profile-draft", f"{at} is a draft: once merged it would fail every later PR", _APPROVE_IT)]
    if st == "changed-since-approval":
        return [_finding("stack-profile-changed", f"{at} changed after it was approved (the recorded hash no longer matches)",
                         f"review the change; then {_APPROVE} to approve it again")]
    return _drift_findings(h["cmd"], *_composed(h["profile"]["profile"]), (at, " at the head commit"), c)


def _stack_profile(c):
    s = _stack_state(c)
    if s["bootstrap"]:
        return _head_findings(c, s)  # no profile on the target yet: the first one is judged for being sound, not against a target
    status, n = s["status"], len(s["errors"])
    if status == "none":
        out = [_finding("stack-profile-missing", "skj.stack.json is missing", f"{_DRAFT_IT}; then {_APPROVE}")]
    elif status == "invalid":
        out = [_finding("stack-profile-invalid", f"skj.stack.json is invalid ({n} error(s))",
                        "run `skj stack show` for the error positions and fix the file")]
    elif status == "draft":
        out = [_finding("stack-profile-draft", "skj.stack.json is a draft: nobody has approved it", _APPROVE_IT)]
    elif status == "changed-since-approval":
        out = [_finding("stack-profile-changed", "skj.stack.json changed after it was approved (the recorded hash no longer matches)",
                        f"review the change; then {_APPROVE} to approve it again")]
    else:
        out = _drift_findings(c.cfg.strict.get("test_command"), *_composed(s["profile"]), ("the approved profile", ""), c)
    return out + _head_findings(c, s)


def _detected(repo, commit):
    return stack.detect(repo, commit)


def _detect_state(c):
    """`stack.detect` at the commit being judged, once per run; False when git cannot answer."""
    if c._detect is None:
        try:
            c._detect = _detected(c.repo, c.rev)
        except ValueError:
            c._detect = False
    return c._detect


def ignored_roots_note(n):
    """The one fixed warning for `n` detected roots a profile cannot name (a count, never a path)."""
    return f"{n} detected root(s) cannot be named in a profile and are not covered by it: review them as code"


def _stack_unprofiled(c):
    """A high-confidence (root, stack) at the head commit that no profile stack covers: compared by root and id family (stack.uncovered); a
    medium-confidence root is no finding. Printed only through `stack.safe_path`."""
    s = _stack_state(c)
    if s["bootstrap"] or s["profile"] is None:
        return []  # bootstrap: nothing to compare to; no profile: stack-profile-* already says so
    fix = (f"two PRs: first add the root to skj.stack.json ({_DRAFT_IT}; then {_APPROVE}) and merge it, then add the code")
    found = _detect_state(c)
    if found is False:
        return [_finding("stack-unprofiled", "the stacks at the head commit could not be detected (git could not answer)",
                         "run in a full clone of the repository (fetch-depth: 0), then re-run")]
    new = stack.uncovered(s["profile"], found)
    if not new:
        return []
    shown = ", ".join(f"{stack.safe_path(r, n)} ({i})" for n, (r, i) in enumerate(new[:5], 1))
    return [_finding("stack-unprofiled", f"{len(new)} high-confidence stack root(s) have no stack in the approved profile: {shown}"
                     + (", ..." if len(new) > 5 else ""), fix)]



# ---- kit pins: skj.kits.lock.json against the installed kits (ported from the fork, L5b) ----
# Local config: `kits.verify` over the registry, the kit dirs and the agent copies of the working tree against `kits.lock_for` (the
# working-tree lockfile). CI (cfg.from_target): the TARGET branch's lockfile (`kits.lock_for`: read once by read_target_trust, never the
# working tree) judges the files git TRACKS at the PR head; the registry, the agent copies and untracked files are not read there. No
# target lockfile (bootstrap): no finding, a note. Findings are fixed texts: kit ids match [a-z0-9-]{1,40} or are named by position
# (#n); no kit, lock or path text is echoed. Never overridable (not in _COVERS).
_KIT_PIN = "review the kit, then a human runs `skj kits pin <id> --apply` in their own terminal window (`skj kits pin <id>` previews it)"
_KIT_REPIN = "review what changed, then a human re-pins it with `skj kits pin <id> --apply` in their own terminal window"
_KIT_CI_ORDER = "; in CI merge the pin in a PR of its own before the kit change, because the lockfile that judges is the target branch's"
KIT_UNTRACKED = "kit not in the repository: checked locally only"
KIT_NO_LOCK = "kit directories are present but the target branch has no lockfile: nothing pins them (a note, not a block)"
KIT_NO_MANIFEST = "kit dir without extension.yml"  # a note, never a finding: Spec Kit cannot load it, so nothing runs from it
_KIT_LOCK_BAD = "skj.kits.lock.json at the head commit cannot be read, or is not a lockfile this version can trust: merged, it would fail every later run"
_KIT_LOCK_GONE = "the PR deletes skj.kits.lock.json: merged, it would turn these rules into a note (write an empty `kits` object to unpin everything)"
_KIT_LOCK_ENTRY = "skj.kits.lock.json at the target branch cannot be read as a set of pins"
_KIT_REPLACED = "`.specify` or `.specify/extensions` is a symlink, a submodule or a file at the head commit: the kits cannot be judged"
_KIT_LOOKALIKE = "a tracked path differs from `.specify` or `.specify/extensions` only in case or Unicode form: on a case-insensitive checkout it lands in the kit dirs"
_KIT_GIT = "the kit files of the head commit cannot be read (git could not answer, or the listing is too large)"
MAX_KITS = 50  # locked kits judged against git (one tree listing each); more is one fixed finding, never a partial answer
_KIT_TOO_MANY = f"skj.kits.lock.json at the target branch pins more than {MAX_KITS} kits: they are not judged one by one"


def _kit_ids(labels):
    return ", ".join(labels[:5]) + (f", ... ({len(labels)} in all)" if len(labels) > 5 else "")


def _kit_result(unpinned, changed, problems=(), warnings=(), ci=False):
    """`{"findings", "warnings"}`: one kit-unpinned and one kit-changed finding at most, however many kits (ids, counts and fixed words only)."""
    out = []
    if unpinned:
        out.append(_finding("kit-unpinned", f"{len(unpinned)} kit(s) not pinned in skj.kits.lock.json: {_kit_ids(unpinned)}",
                            _KIT_PIN + (_KIT_CI_ORDER if ci else "")))
    if changed or problems:
        out.append(_finding("kit-changed", "; ".join(([f"{len(changed)} kit(s) differ from their pin in skj.kits.lock.json: {_kit_ids(changed)}"]
                                                      if changed else []) + list(problems)), _KIT_REPIN + (_KIT_CI_ORDER if ci else "")))
    return {"findings": out, "warnings": list(warnings)}


def _kits_local(c):
    try:
        res = kits.verify(c.repo, kits.lock_for(c.cfg, c.repo))
    except (kits.KitError, ConfigInvalid):  # an untrustworthy registry, kit file or lockfile is a finding, never a pass
        return _kit_result([], [], ["the kit registry, a kit file or skj.kits.lock.json cannot be trusted: run `skj kits verify`"])
    return _kit_result([f["kit"] for f in res["findings"] if f["rule"] == "kit-unpinned"],
                       [f["kit"] for f in res["findings"] if f["rule"] == "kit-changed"])


def _lock_problem(raw):
    """True when the head's lockfile bytes (or the unreadable sentinel) are not a lockfile this version can read."""
    if raw is _UNREADABLE:
        return True
    try:
        kits.read_lock(raw)
    except ConfigInvalid:
        return True
    return False


def _lookalikes(repo, head, args, want):
    """True when the tree listing `args` (git ls-tree names) holds an entry that equals `want` only after case and Unicode folding (a
    case-insensitive checkout would put it in the same place); None when git cannot answer."""
    out = kits._git(repo, "ls-tree", "-z", "--name-only", "--full-tree", "--end-of-options", head, *args)
    if out is None:
        return None
    names = (rec.decode("utf-8", "replace").rsplit("/", 1)[-1] for rec in out.split(b"\0") if rec)
    return any(n != want and kits._fold(n) == want for n in names)


def _tracked_kits(repo, head):
    """From git's tree of the 40-hex `head`: (names with a tracked file below `.specify/extensions/`, names with a tracked
    `<name>/extension.yml`, {name: mode of an entry that is itself the name: a file, symlink or submodule where a kit dir would be}, whether
    `.specify` or `extensions` has a case/Unicode lookalike), or "replaced" when `.specify` or `.specify/extensions` is itself a symlink,
    submodule or file (the kits would live elsewhere), or None when git cannot answer (or a listing is over kits.MAX_LISTING). Names are
    decoded leniently (an odd one never equals a kit id). A parent-level case twin (`.specify/extensions/SF/` beside `sf/`) and a symlinked
    kit dir are seen here, which `kits.dir_digest_at` (one kit's own prefix) cannot."""
    for path in (".specify", ".specify/extensions"):  # one pathspec per call: two make git list the children of `.specify` too
        top = kits._git(repo, "ls-tree", "-z", "--full-tree", "--end-of-options", head, "--", path)
        if top is None:
            return None
        for rec in (r for r in top.split(b"\0") if r):
            meta, _, name = rec.partition(b"\t")
            if name != path.encode():  # git answered about something else: not an answer
                return None
            if meta.split(b" ")[0] != b"040000":
                return "replaced"
    roots, inner = _lookalikes(repo, head, (), ".specify"), _lookalikes(repo, head, ("--", ".specify/"), "extensions")
    if roots is None or inner is None:
        return None
    out = kits._git(repo, "ls-tree", "-r", "-z", "--full-tree", "--end-of-options", head, "--", ".specify/extensions/")
    if out is None:
        return None
    present, manifests, entries = set(), set(), {}
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, _, raw = rec.partition(b"\t")
        parts = raw.decode("utf-8", "replace").split("/")
        if len(parts) < 3 or parts[:2] != [".specify", "extensions"]:
            return None
        if len(parts) == 3:
            entries[parts[2]] = meta.split(b" ")[0]
            continue
        present.add(parts[2])
        if len(parts) == 4 and kits._fold(parts[3]) == "extension.yml":  # `Extension.yml` too: a case-insensitive checkout loads it
            manifests.add(parts[2])
    return present, manifests, entries, roots or inner


def _kits_ci(c):
    cfg, head = c.cfg, _rev(c.repo, c.rev)
    hl = _stack_state(c)["head"]["lock"]
    try:
        lock = kits.lock_for(cfg, c.repo)  # the target's blob; None = the target has none (bootstrap). Never the working tree.
    except (ConfigInvalid, kits.KitError):
        return _kit_result([], [], [_KIT_LOCK_ENTRY], ci=True)
    if lock is None:  # bootstrap: nothing to judge against, so a note; the first lockfile must still be sound
        if hl is None:
            tracked = _tracked_kits(c.repo, head) if head else None
            kit_dirs = isinstance(tracked, tuple) and any(not n.startswith(".") and n != kits.OWN_EXTENSION for n in tracked[0])
            return _kit_result([], [], (), [KIT_NO_LOCK] if kit_dirs else [])
        return _kit_result([], [], [_KIT_LOCK_BAD] if _lock_problem(hl) else [], [KITS_TRUST_NOTE], ci=True)
    problems = []
    if hl != cfg.target_lock_blob:  # the PR's copy becomes the target's on merge
        if hl is None:
            problems.append(_KIT_LOCK_GONE)
        elif _lock_problem(hl):
            problems.append(_KIT_LOCK_BAD)
    lock = lock["kits"]
    if len(lock) > MAX_KITS:
        return _kit_result([], [], problems + [_KIT_TOO_MANY], ci=True)
    tracked = _tracked_kits(c.repo, head) if head else None
    if tracked is None:
        return _kit_result([], [], problems + [_KIT_GIT], ci=True)
    if tracked == "replaced":
        return _kit_result([], [], problems + [_KIT_REPLACED], ci=True)
    present, manifests, entries, lookalike = tracked
    if lookalike:
        problems.append(_KIT_LOOKALIKE)
    own = kits.OWN_EXTENSION  # the toolkit's own extension is reviewed as code, not pinned (a case variant is no id and stays reported)
    present, manifests = present - {own}, manifests - {own}
    odd = {n for n, mode in entries.items() if mode in (b"120000", b"160000") and not n.startswith(".") and n != own}  # symlinked or submodule dirs
    # a tracked dir whose name is no kit id (`SF`, a fullwidth or Cyrillic lookalike) would land in a pinned kit dir on a case-insensitive
    # checkout, with or without a manifest; dot names (.cache) and plain files at the top are not kit dirs
    odd |= {n for n in present if not n.startswith(".") and not stack._ID.fullmatch(n)}
    names = sorted((manifests | odd) - set(lock))
    unpinned = [n if stack._ID.fullmatch(n) else f"#{i}" for i, n in enumerate(names, 1)]
    changed, untracked = [], []
    for kit_id, pin in lock.items():
        if kit_id in entries:  # a file, symlink or submodule where the locked kit dir was
            changed.append(kit_id)
        elif kit_id not in present:
            untracked.append(kit_id)
        else:
            try:
                digest = kits.dir_digest_at(c.repo, head, kit_id)
            except kits.KitError:
                digest = "changed"
            if digest is None:
                untracked.append(kit_id)
            elif digest != pin["dir_digest"]:  # includes "changed": a symlink or submodule inside the kit dir, an odd mode or path
                changed.append(kit_id)
    warn = [f"{KIT_UNTRACKED} ({_kit_ids(untracked)})"] if untracked else []
    bare = sorted(n for n in present if stack._ID.fullmatch(n) and n not in manifests and n not in lock)  # locked ones are judged by their digest
    warn += [f"{KIT_NO_MANIFEST} ({_kit_ids(bare)})"] if bare else []
    return _kit_result(unpinned, changed, problems, warn, ci=True)


def _kits_state(c):
    if c._kits is None:
        c._kits = _kits_ci(c) if c.cfg.from_target else _kits_local(c)
    return c._kits


def _kits(c):
    return _kits_state(c)["findings"]


# ---- L1 order evidence: red <sha> → green <sha> per AC, checked against git ----
_HEX = r"[0-9a-fA-F]{7,40}"
_EVIDENCE = re.compile(rf"(?<![0-9A-Za-z])red[\s:]+({_HEX})(?![0-9A-Za-z])\s*(?:→|->)\s*"
                       rf"green[\s:]+({_HEX})(?![0-9A-Za-z])", re.I)
_TAGGED = re.compile(r"(?<![0-9A-Za-z])(?:red|green)[\s:]+[0-9a-f]{4,}", re.I)  # a second pair makes the cell ambiguous
_MARKUP_CHARS = re.compile(r"[`*_]")


def parse_evidence(status_cell):
    """`red <sha> → green <sha>` (or `->`; backticks, bold and trailing words tolerated) -> {"red","green"}, else None.
    The cell is PR text: a SHA is 7-40 hex characters or the cell is not evidence, so nothing else reaches git."""
    text = _MARKUP_CHARS.sub("", status_cell or "")
    m = _EVIDENCE.search(text)
    return {"red": m[1], "green": m[2]} if m and len(_TAGGED.findall(text)) == 2 else None


def _is_merge(repo, full):
    """True for a merge commit, and when its parents cannot be read (fail closed)."""
    out = _out(repo, "rev-list", "--no-walk", "--parents", full)
    return out is None or len(out.split()) > 2


def _is_ancestor(repo, older, newer):
    r = config._git(repo, "merge-base", "--is-ancestor", older, newer)
    return r is not None and r.returncode == 0  # 1 = no, anything else = error: both count as no


def _changed(repo, full, diff_filter=None):
    """Paths the commit changes, NUL-delimited (names are untrusted), or None when git failed."""
    out = _out(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root", "--no-renames",
               *([f"--diff-filter={diff_filter}"] if diff_filter else []), full, "--")
    return [n for n in out.decode("utf-8", "replace").split("\0") if n] if out is not None else None


def _dod_files(key):
    """`dod/DoD_<KEY>.md` and `dod/Decisions_<KEY>.md` (K8: the ledger dir is fixed)."""
    return [f"dod/DoD_{key}.md", f"dod/Decisions_{key}.md"]


def _spec_tasks(repo, spec_dir, head_full):
    """`<spec dir>/tasks.md` (repo-root-relative) when the ledger's untrusted `Spec dir:` is valid in HEAD's tree (prs.spec_dir_in_tree),
    else None (git failure included: no allowance)."""
    try:
        spec = prs.spec_dir_in_tree(spec_dir, head_full, str(repo)) if head_full else None
    except prs._Refuse:
        spec = None
    return f"{spec}/tasks.md" if spec else None


def _l1_finding(rule, ac, detail, fix):
    return _finding(rule, f"{ac}: {detail}", fix)


_EVIDENCE_FIX = ("record the commits in the ledger section 3 status cell as `red <sha> → green <sha>`: a red commit with "
                 "only tests, then a green commit with the code")


MAX_ROWS = 100  # section 3 rows L1 judges against git (each costs several git runs, ~30 ms); more is `evidence-too-many`, never a partial answer


def _too_many(rows):
    return [_finding("evidence-too-many", f"ledger section 3 has {len(rows)} AC rows; at most {MAX_ROWS} are judged against git",
                     f"split the story, or merge rows: one row per AC, at most {MAX_ROWS}")] if len(rows) > MAX_ROWS else []


def check_l1(rows, repo, head, tests, spec_dir, key, ac_count=0):
    """L1 for each section 3 row (and each AC-1..AC-<ac_count> that has no row). A `detail` names the AC, the rule and
    a count; it echoes at most 12 hex characters of a SHA and never a file name (PR text and git output are untrusted).
    A red commit may change test files, the story's ledger and decisions file and the valid spec dir's tasks.md tick; never a
    directory's worth of files."""
    if len(rows) > MAX_ROWS:
        return _too_many(rows)
    out = []
    head_full = _rev(repo, head)
    prefix = (_out(repo, "rev-parse", "--show-prefix") or b"").decode("utf-8", "replace").strip()  # repo below the toplevel
    allowed = {prefix + f for f in _dod_files(key)} | {_spec_tasks(repo, spec_dir, head_full)} - {None}
    is_test = lambda f: any(gate._glob_match(f, g) for g in tests)  # noqa: E731
    for row in rows:
        ac, ev = row["ac"], parse_evidence(row["status"])
        if ev is None:
            out.append(_l1_finding("evidence-missing", ac, "the status cell has no `red <sha> → green <sha>` pair", _EVIDENCE_FIX))
            continue
        full = {k: _commit(repo, ev[k]) for k in ("red", "green")}
        bad = [k for k in full if full[k] is None]
        for k in bad:
            out.append(_l1_finding("sha-unknown", ac, f"the {k} commit {ev[k][:12].lower()} is not a commit in this repository "
                                   "(or git could not be run)", "use the SHAs of the real red and green commits (`git log`)"))
        if bad:
            continue
        red, green = full["red"], full["green"]
        merges = [k for k in full if _is_merge(repo, full[k])]
        for k in merges:
            out.append(_l1_finding("merge-commit", ac, f"the {k} commit is a merge commit (or its parents could not be read)",
                                   "record plain commits: tests only for red, code for green"))
        if merges:
            continue
        if red == green or not _is_ancestor(repo, red, green):
            why = "red and green are the same commit" if red == green else "the red commit is not an ancestor of the green commit"
            out.append(_l1_finding("red-not-ancestor", ac, why, "commit the failing tests first (red), then the code (green), on one branch"))
        else:
            names, added = _changed(repo, red), _changed(repo, red, "AMT")
            if names is None or added is None:
                out.append(_l1_finding("sha-unknown", ac, "the files of the red commit could not be read", "re-run in a full clone"))
            else:
                code = [f for f in names if not is_test(f) and f not in allowed]
                if code:
                    out.append(_l1_finding("red-touches-code", ac, f"the red commit changes {len(code)} non-test file(s) besides the "
                                           "ledger, the decisions file and tasks.md", "move the code into the green commit; red holds tests "
                                           "only (and the ledger, decisions file and tasks.md tick)"))
                if not any(is_test(f) for f in added):
                    out.append(_l1_finding("red-no-test", ac, "the red commit adds or changes no test file",
                                           "the red commit must add the failing test"))
        if not (head_full and _is_ancestor(repo, green, head_full)):
            out.append(_l1_finding("green-not-on-branch", ac, "the green commit is not an ancestor of HEAD" if head_full
                                   else "HEAD does not resolve to a commit", "record a green commit that is on this branch"))
    return out + _rows_missing(rows, ac_count)


def _rows_missing(rows, ac_count):
    have = {r["ac"] for r in rows}
    return [_l1_finding("evidence-missing", f"AC-{n}", "no section 3 row", _EVIDENCE_FIX)
            for n in range(1, ac_count + 1) if f"AC-{n}" not in have]


def base_ref(repo, base):
    """The full ref the PR range starts at: `refs/remotes/origin/<base>` when it exists (a CI checkout has no local branch), else
    `refs/heads/<base>`, else `<base>` unchanged (then unresolvable). Never a short name, so a tag cannot stand in for it."""
    return next((ref for ref in (f"refs/remotes/origin/{base}", f"refs/heads/{base}") if _rev(repo, ref)), base)


def head_is_checkout(repo, head):
    """True when the local branch `head` is the commit HEAD is at: strict mode judges the working-tree ledger and decisions file."""
    full = _rev(repo, f"refs/heads/{head}")
    return bool(full) and full == _rev(repo, "HEAD")


def _base(c):
    """`base-unknown`: a base was asked for (`--base`, the default origin/HEAD of `pr`) and is no commit here. Never overridable, never a pass."""
    if c.base is None or c.base_full:
        return []
    return [_finding("base-unknown", "the base ref is not a commit in this repository (or git could not be run), so the range of the "
                     "branch cannot be told", "pass --base <ref> (CI: origin/<target branch>), or fetch the base branch (a full clone, "
                     "fetch-depth: 0; `git remote set-head origin -a` sets origin/HEAD)")]


def _l1(c):
    if c.ledger is None:
        return []  # reported by _ledger
    tests = {**gate.DEFAULT_TESTS, **c.cfg.gate["tests"]}
    rows = gate.parse_section3(c.ledger)
    return check_l1(rows, c.repo, c.rev, tests, c.spec_dir, c.id, len(dod.ac_lines(c.ledger))) \
        + (_range_findings(rows, c.repo, c.base_full) if c.base_full and len(rows) <= MAX_ROWS else [])  # red and green must lie in base..head


def _evidence(c):
    """The git-free part of L1, for `done` (after a squash merge the red and green commits may be gone): every AC has a row
    whose status cell is a `red <sha> → green <sha>` pair. The record check validates those SHAs against the record."""
    if c.ledger is None:
        return []  # reported by _ledger
    rows = gate.parse_section3(c.ledger)
    return [_l1_finding("evidence-missing", r["ac"], "the status cell has no `red <sha> → green <sha>` pair", _EVIDENCE_FIX)
            for r in rows if parse_evidence(r["status"]) is None] + _rows_missing(rows, len(dod.ac_lines(c.ledger)))


# ---- verification record: written by `verify`, only validated by `check` ----
_V_HEADING = re.compile(r"^### Verification\b[^\r\n]*", re.M)
_V_HEAD = re.compile(r"### Verification — (\d{4}-\d{2}-\d{2}) @ ([0-9a-f]{7,40})")
_V_AC = re.compile(r"- (AC-\d+): red ([0-9a-f]{12}) exit (\d{1,3}) · green ([0-9a-f]{12}) exit (\d{1,3}) · L1 ok")
_V_COMMAND = re.compile(r"- command-sha256: ([0-9a-fA-F]{12})")
_V_HEADLINE = re.compile(r"- head: ([0-9a-f]{12}) exit (\d{1,3})")
_V_NEXT = re.compile(r"^#{1,3} ", re.M)
_SUPERSEDED = " (superseded)"
_PR_HEAD = re.compile(rf"(?<![0-9A-Za-z])PR head[\s:]+({_HEX})(?![0-9A-Za-z])", re.I)


def _range_findings(rows, repo, base):
    """`not-in-range` when `base` is no commit here, or a red or green commit is already in `base` (not this branch's work)."""
    if len(rows) > MAX_ROWS:
        return _too_many(rows)
    base_full = _rev(repo, base) if base.strip() else ""
    if not base_full:
        return [_finding("not-in-range", "the base ref is not a commit in this repository (or git could not be run)",
                         "pass an existing branch, tag or commit as --base")]
    out = []
    for row in rows:
        for k, sha in (parse_evidence(row["status"]) or {}).items():
            full = _commit(repo, sha)
            if full and _is_ancestor(repo, full, base_full):
                out.append(_l1_finding("not-in-range", row["ac"], f"the {k} commit is already in the base",
                                       "record red and green commits made after the base: this branch's own work"))
    return out


def _same_sha(cell_sha, recorded12):
    n = min(len(cell_sha), 12)
    return cell_sha[:n].lower() == recorded12[:n]


def _record_stale(repo, head, recorded, key, spec_dir=None):
    """Why the record (made at the commit `recorded`) no longer covers `head`, or None when it does: head is that commit
    or a descendant whose later commits touch only the ledger, the decisions file and the valid spec dir's tasks.md
    (prs.only_ledger_since, the one implementation; `spec_dir` None: no tasks.md allowance)."""
    head_full = _rev(repo, head)
    rec_full = _commit(repo, recorded)
    if not head_full:
        return "HEAD does not resolve to a commit"
    if rec_full is None:
        return "the commit the record was made at is not in this repository (or git could not be run)"
    if rec_full == head_full:
        return None
    if not _is_ancestor(repo, rec_full, head_full):
        return "the commit the record was made at is not an ancestor of HEAD"
    try:
        only = prs.only_ledger_since(str(repo), rec_full, head_full, spec_dir, key)
    except prs._Refuse:
        return "git could not compare the record's commit with HEAD"
    return None if only else "later commits change files other than the ledger, the decisions file and tasks.md"


def validate_record(ledger_md, rows, repo, head, spec_dir, key, base=None, squash_ok=False, command=False):
    """Findings for the latest live `### Verification` block of ledger section 4 (a block in a fence or comment is not
    one): `record-missing`, `record-stale`, `record-incomplete`, `record-failed`, and `not-in-range` with `base`. It runs
    no test and reads only the ledger and git. `squash_ok` (the `done` phase, after a squash merge the red and green
    commits are no ancestors of HEAD): no staleness or range check, only completeness, SHAs against the status cells and exit codes."""
    redo = f"run `skj strict verify {key}` and commit the ledger"
    log = dod._visible(dod.section(ledger_md, 4) or "")[0]
    live = [m for m in _V_HEADING.finditer(log) if not m.group(0).rstrip().endswith(_SUPERSEDED.strip())]
    head_m = _V_HEAD.fullmatch(live[-1].group(0).rstrip()) if live else None
    if head_m is None:
        return [_finding("record-missing", "ledger section 4 has no verification record (a `### Verification — <date> @ <sha>` block)", redo)]
    rest = log[live[-1].end():]
    nxt = _V_NEXT.search(rest)
    found, heads, shas = {}, [], []
    for line in (rest[:nxt.start()] if nxt else rest).splitlines():
        line = line.strip()
        m = _V_COMMAND.fullmatch(line)
        if m:
            shas.append(m[1].lower())
        m = _V_AC.fullmatch(line)
        if m:
            found.setdefault(m[1], []).append(m.groups()[1:])
        m = _V_HEADLINE.fullmatch(line)
        if m:
            heads.append(m.groups())
    out = []
    if command is not False and shas != [_command_hash(command)]:  # `command`: the test command in force (None = unset); False = not judged
        out.append(_finding("record-command-mismatch", "the verification record was not made with the test command in force (the "
                            "target branch's in CI): a weaker command could have passed it", redo))
    if not squash_ok:
        why = _record_stale(repo, head, head_m[2], key, spec_dir)
        if why:
            return [_finding("record-stale", f"the verification record is stale: {why}", redo)]
        if base is not None:
            out += _range_findings(rows, repo, base)
    if squash_ok:  # a `PR head <sha>` in a section 3 commit/PR cell names the commit the PR was made at: the record must be for it
        pr_heads = [h for row in rows for h in _PR_HEAD.findall(_MARKUP_CHARS.sub("", row["evidence"]))]
        if any(not _same_sha(h, head_m[2][:12]) for h in pr_heads):
            out.append(_finding("record-incomplete", "the verification record is for a different commit than the PR head recorded in "
                                "ledger section 3", redo))
    if len(heads) != 1 or not _same_sha(head_m[2], heads[0][0]):
        out.append(_finding("record-incomplete", "the verification record has no single `- head:` line naming the recorded head", redo))
    elif int(heads[0][1]) != 0:
        out.append(_finding("record-failed", "the record shows the tests failing at the recorded HEAD",
                            "fix the code so the tests pass at HEAD, then " + redo))
    for row in rows:
        ac, ev, got = row["ac"], parse_evidence(row["status"]), found.get(row["ac"], [])
        if len(got) != 1:
            out.append(_l1_finding("record-incomplete", ac, "the verification record has no line for it" if not got
                                   else "the verification record has more than one line for it", redo))
        elif ev is None or not (_same_sha(ev["red"], got[0][0]) and _same_sha(ev["green"], got[0][2])):
            out.append(_l1_finding("record-incomplete", ac, "the recorded red and green SHAs differ from the section 3 status cell", redo))
        elif int(got[0][1]) == 0 or int(got[0][3]) != 0:
            out.append(_l1_finding("record-failed", ac, "the record shows a red run that passed or a green run that failed",
                                   "make the red tests fail and the green tests pass, then " + redo))
    return out


def _command_hash(command):
    """The 12 hex a record's `- command-sha256:` line holds; unset (None) has none."""
    return hashlib.sha256(command.encode()).hexdigest()[:12] if command else None


def _record(c):
    if c.ledger is None:
        return []  # reported by _ledger
    return validate_record(c.ledger, gate.parse_section3(c.ledger), c.repo, c.rev, c.spec_dir, c.id,
                           command=c.cfg.strict.get("test_command"))


def _record_done(c):
    if c.ledger is None:
        return []
    return validate_record(c.ledger, gate.parse_section3(c.ledger), c.repo, "HEAD", c.spec_dir, c.id, squash_ok=True,
                           command=c.cfg.strict.get("test_command"))


# ---- decisions: the rules `pr` and `done` add ----
_D_TOKEN = re.compile(r"\bD\d+\b")
_STATUS_ARROW = re.compile(r"(?:→|->)\s*(?:_\(open\)_|validated|rejected)")  # both searches are linear, whatever the line
SUP_HIDDEN = re.compile(r"\bD\d+(?!\d)\s*:\s*supersedes\s+D\d")  # the supersedes words on a line that is not a decision line


def _hidden_lines(md, key):
    """True when a decision line could be read differently by a person than by `parse_decisions`: a duplicate `D` id (the first
    wins, so a `validated` copy can hide a `rejected` one) or a visible `D<n> … → status` line that is not a decision line
    (`1. `, `> ` or other prefix). The format note `render_file` writes is exempt."""
    note = set(decisions.render_file(key).splitlines())
    lines, ids, odd = dod._visible(md)[0].splitlines(), [], False
    for ln in lines:
        m = decisions._CAND.match(ln)
        if m:
            ids.append(m.group(2))
        elif ln not in note:
            d = _D_TOKEN.search(ln)
            odd = odd or SUP_HIDDEN.search(ln) is not None or (  # a supersedes line behind `1. ` or `> ` is not read
                ("→" in ln or "->" in ln) and d is not None and _STATUS_ARROW.search(ln, d.end()) is not None)
    return odd or len(ids) != len(set(ids))


def _decisions_ok(c):
    if (c.decisions_ok and not c.decisions["unparsed"] and not _hidden_lines(c.decisions_md, c.id)
            and not decisions.supersede_problem(c.decisions)):
        return []
    return [_finding("decisions-invalid", "the decisions file is unreadable (a symlink, outside the repository, over 1 MiB or not UTF-8), "
                     "a D line breaks the grammar or repeats an id, a line that looks like a decision is not one, a `supersedes` line "
                     "names no decision, an override or a higher id, or a code fence or comment is left open in it, so its lines cannot all be judged",
                     f"repair `dod/Decisions_{c.id}.md` (`skj decisions {c.id}` shows what parses)")]


def _ids_detail(ds):
    return ", ".join(d["id"] for d in ds)


def _decisions_pr(c):
    """`decision-rejected` (any rejected line) and `override-unposted` (an override line without a `posted:` marker that
    still matches its text: the PO must have seen it). Ids only, never decision text."""
    out = _decisions_ok(c)
    rejected = decisions.effective(c.decisions)["rejected"]  # a rejected line a live `supersedes` answers no longer blocks
    if c.cfg.rules["enabled"]:  # a rejected `out of scope:` line is carried out by `_rules` (the rule is back in scope), not a finding of its own
        rejected = [d for d in rejected if not rules.rejected_exclusion(d)]
    if rejected:
        out.append(_finding("decision-rejected", f"the PO rejected: {_ids_detail(rejected)}",
                            "change the work to match the PO's answer, or record a new decision; never re-word or delete a rejected line"))
    unposted = [d for d in decisions.unposted(c.decisions) if d["kind"] == "override"]
    if unposted:
        out.append(_finding("override-unposted", f"override(s) not posted to the story: {_ids_detail(unposted)}",
                            f"post them with `skj decisions {c.id} --post` (preview, yes, then --apply)"))
    answers = decisions.supersede_unposted(c.decisions)  # every superseder, whatever its status, and every open line it retires
    if answers:
        out.append(_finding("supersedes-unposted", f"decision(s) in a supersedes relation not posted to the story (no `posted:` marker that "
                            f"matches the line): {_ids_detail(answers)}",
                            f"post an open line with `skj decisions {c.id} --post` (preview, yes, then --apply) before it is resolved; a "
                            "resolved line that was never posted is recorded again as a new open decision"))
    return out


def _decisions_done(c):
    still = decisions.effective(c.decisions)["open"]
    if not still:
        return []
    return [_finding("decision-open", f"decision(s) the PO has not validated or rejected yet: {_ids_detail(still)}",
                     "wait for the PO's reply (`skj comments`); a human records it with `skj decisions "
                     f"{c.id} --resolve D<n> --validated|--rejected`")]


# ---- L3 fidelity verdicts: written by the review plane `AC fidelity`, only read by `check` ----
_F_HEADING = re.compile(r"^### AC fidelity\b[^\r\n]*", re.M)
_F_HEAD = re.compile(r"### AC fidelity — (\d{4}-\d{2}-\d{2}) @ ([0-9a-f]{7,40})")
_F_LINE = re.compile(r"- (AC-[0-9]{1,6}): (asserts|partial|does not assert) — (\S(?:.{0,198}\S)?)")  # reason: 1 to 200 characters
_F_NEXT = re.compile(r"#{1,3} ")
_FIDELITY_FIX = "run speckit.jira-lifecycle.review (its AC fidelity plane writes the block) and commit the ledger"


def parse_fidelity(ledger_md):
    """The latest live `### AC fidelity — <date> @ <sha>` block of ledger section 4 (older ones end in ` (superseded)`; a block in
    a fence, comment, quote or list is not one). Returns `{"block": {"date","sha"} or None, "verdicts": {"AC-1": {"verdict",
    "reason"}}, "duplicates": [ids repeated], "unparsed": bool}`. `unparsed` (fail closed): an unclosed fence or comment, a live
    heading that is not exactly the form above, or any block line that is not exactly `- AC-<n>: asserts|partial|does not
    assert — <reason, 1 to 200 characters>` (a list or quote prefix, another dash, other prose, a fence or comment inside it)."""
    out = {"block": None, "verdicts": {}, "duplicates": [], "unparsed": False}
    sec = dod.section(ledger_md, 4)
    if dod.unclosed(ledger_md):
        out["unparsed"] = True
    if out["unparsed"] or sec is None:
        return out
    raw, log = sec.splitlines(), dod._visible(sec)[0].splitlines()  # 1:1 lines; a fence or comment is blank in `log`
    live = [i for i, line in enumerate(log) if _F_HEADING.match(line) and not line.rstrip().endswith(_SUPERSEDED.strip())]
    if not live:
        return out
    head = _F_HEAD.fullmatch(log[live[-1]].rstrip())
    if head is None:
        out["unparsed"] = True
        return out
    out["block"] = {"date": head[1], "sha": head[2]}
    seen = {}
    for i in range(live[-1] + 1, len(log)):
        if _F_NEXT.match(log[i]):
            break
        if raw[i].strip() != log[i].strip():  # something in the block is hidden by a fence or comment
            out["unparsed"] = True
        m = _F_LINE.fullmatch(log[i].rstrip())
        if m:
            seen[m[1]] = seen.get(m[1], 0) + 1
            out["verdicts"].setdefault(m[1], {"verdict": m[2], "reason": m[3]})
        elif log[i].strip():
            out["unparsed"] = True
    out["duplicates"] = sorted((k for k, n in seen.items() if n > 1), key=lambda k: int(k[3:]))
    return out


def _fidelity_acs(c):
    """The ACs that need a verdict: every section 3 row and AC-1..AC-<n> for the n AC lines of section 1."""
    rows = {r["ac"] for r in gate.parse_section3(c.ledger)} | {f"AC-{n}" for n in range(1, len(dod.ac_lines(c.ledger)) + 1)}
    return sorted(rows, key=lambda a: (len(a), a))  # section 3 ids come from gate (any length): no int(), shown cut below


def _fidelity(c, stale):
    if c.ledger is None:
        return []  # reported by _ledger
    f = parse_fidelity(c.ledger)
    if f["unparsed"] or f["duplicates"]:  # never overridable and never read as "first wins"
        return [_finding("fidelity-invalid", "the AC fidelity block in ledger section 4 is malformed, repeats an AC "
                         f"({len(f['duplicates'])}), or hides lines in a code fence or comment", "re-run speckit.jira-lifecycle.review: it "
                         "rewrites the block (one `- AC-<n>: asserts|partial|does not assert — <reason>` line per AC, nothing else)")]
    if f["block"] is None:
        return [_finding("fidelity-missing", "ledger section 4 has no AC fidelity block (a `### AC fidelity — <date> @ <sha>` block)",
                         _FIDELITY_FIX)]
    out = []
    missing = [a for a in _fidelity_acs(c) if a not in f["verdicts"]]
    if missing:
        out.append(_finding("fidelity-missing", f"{len(missing)} AC(s) have no fidelity verdict: {', '.join(a[:12] for a in missing[:20])}", _FIDELITY_FIX))
    fails = [a for a, v in f["verdicts"].items() if v["verdict"] == "does not assert"]
    if fails:
        out.append(_finding("fidelity-fails", f"{len(fails)} AC(s) whose test does not assert the stated behaviour: "
                            f"{', '.join(sorted(fails, key=lambda a: int(a[3:])))}",
                            "make the test assert the AC's stated behaviour (a new red then green), then " + _FIDELITY_FIX))
    why = _record_stale(c.repo, c.rev, f["block"]["sha"], c.id, c.spec_dir) if stale else None
    if why:
        out.append(_finding("fidelity-stale", f"the AC fidelity verdicts are stale: {why}", _FIDELITY_FIX))
    return out


def _fidelity_pr(c):
    return _fidelity(c, True)


def _fidelity_done(c):  # after a squash merge the reviewed commit is no ancestor of HEAD: no staleness, no git
    return _fidelity(c, False)


def _fidelity_warnings(c):
    """`partial` is allowed: one warning per such AC, not a finding."""
    f = parse_fidelity(c.ledger) if c.ledger is not None else None
    if f is None or f["unparsed"] or f["duplicates"]:
        return []
    return [{"rule": "fidelity-partial", "ac": a} for a, v in sorted(f["verdicts"].items(), key=lambda kv: int(kv[0][3:]))
            if v["verdict"] == "partial"]


PHASES = {
    "implement": [_ledger, _spec_files, _placeholders, _open_questions, _board, _test_command, _stack_profile, _stack_unprofiled, _kits],
    "pr": [_ledger, _base, _l1, _record, _decisions_pr, _rules, _review, _fidelity_pr, _stack_profile, _stack_unprofiled, _kits],
    "done": [_ledger, _evidence, _record_done, _decisions_pr, _decisions_done, _rules, _review, _fidelity_done, _stack_profile,
             _stack_unprofiled, _kits],
}
STUB_RULES = ()  # the rule functions that carry a STUBS finding (none since RU3)

# ---- override: `skip <rule>` lines of kind override; only these findings, never L1, a stub or a missing spec/plan/tasks ----
_COVERS = {"test-command": ("test-command",),
           "l2": ("test-command", "record-missing", "record-stale", "record-incomplete", "record-failed", "head-fails", "green-fails", "red-passes",
                  "timeout", "runner-error", "record-command-mismatch"),
           "open-question": ("open-question",),
           "placeholder": ("placeholder",)}


def overrides(c):
    """{finding rule: id} of the override lines in force: kind override, text exactly `skip <rule>` for an overridable rule, any
    status but rejected; the lowest id wins per rule. Covers every check of the story until it is done."""
    out = {}
    for d in sorted(c.decisions["open"] + c.decisions["validated"], key=lambda d: int(d["id"][1:]), reverse=True):
        rule = d["text"][5:] if d["text"].startswith("skip ") else None
        if d["kind"] == "override" and rule in _COVERS:
            out.update({f: d["id"] for f in _COVERS[rule]})
    return out


def _used(ids):
    return sorted(ids, key=lambda i: int(i[1:]))


# ---- `rules.reconfirmed` (RU3): the ledger's `Rules reviewed:` against the target branch's ledger and the PR's earlier versions; a note ----
_TARGET_LEDGER_LABEL = "the story ledger at the target branch"
MAX_HISTORY = 200  # ledger versions of `base..head` that are compared; more is the fixed RECONFIRM_TOO_LONG note, never a partial answer


def _reviewed_at(repo, commit, rel):
    """`(ledger exists, {rule id: hash8})` of the ledger `rel` at the resolved `commit` (`_blob_at`: 1 MiB, regular file; `{}` for no line or
    an untrusted header). ConfigInvalid / UnicodeDecodeError when it cannot be read."""
    raw = _blob_at(repo, commit, rel, gate.MAX_READ, _TARGET_LEDGER_LABEL)
    return (False, {}) if raw is None else (True, dod.ledger_rules_reviewed(raw.decode("utf-8"))["hashes"])


def target_reviewed(repo, base, key):
    """`{rule id: hash8}` of the `Rules reviewed:` line in the story's ledger on the PR's target `base` (resolved as `_resolve_base` does,
    never a tag). `{}` when the target has no ledger, no such line or an untrusted header. None when it cannot be read (git failing, a
    symlink, oversize, not UTF-8, a base that is no commit here): the caller reports that, it is never a pass."""
    commit = _resolve_base(repo, base) if not config._BAD_BASE.search(base) else ""
    if not commit:
        return None
    try:
        return _reviewed_at(repo, commit, "dod/" + keys.dod_name(key))[1]
    except (ConfigInvalid, UnicodeDecodeError):
        return None


def reconfirmed_vs_base(repo, base, key, new_hashes, head="HEAD"):
    """What `rules.reconfirmed` is for the PR range `base..head`: the list `rules.reconfirmed(old, new_hashes)`, or a FIXED note text (str)
    when that cannot be answered. `old` is, per rule id, the first record that differs from the head's across the target branch's ledger
    and every version of the ledger a commit of `git log --full-history --no-merges base..head -- <ledger>` made (a first PR has no ledger on
    the target: the branch's own history is the only record). Notes: RECONFIRM_UNREADABLE (the target ledger, or a base or head that is no
    commit), RECONFIRM_BRANCH_UNREADABLE (the log, or a version in the range), RECONFIRM_TOO_LONG (more than MAX_HISTORY commits touch the
    ledger) and RECONFIRM_FIRST_PR (no ledger on the target, no earlier record in the range, and the head records a rule)."""
    commit = _resolve_base(repo, base) if not config._BAD_BASE.search(base) else ""
    tip, rel = _rev(repo, head), "dod/" + keys.dod_name(key)
    if not (commit and tip):
        return rules.RECONFIRM_UNREADABLE
    # --no-merges: a merge commit (CI checks out the PR's merge) repeats the head's record and is nobody's earlier record
    listed = _out(repo, "log", "--full-history", "--no-merges", "--topo-order", "--reverse", "--format=%H", "--max-count=%d" % (MAX_HISTORY + 1),
                  "--end-of-options", "%s..%s" % (commit, tip), "--", rel)
    shas = listed.decode("ascii", "replace").split() if listed is not None else None
    if shas is None or any(not re.fullmatch(r"[0-9a-f]{40}", x) for x in shas):
        return rules.RECONFIRM_BRANCH_UNREADABLE
    if len(shas) > MAX_HISTORY:
        return rules.RECONFIRM_TOO_LONG
    try:
        target_has_ledger, recorded = _reviewed_at(repo, commit, rel)
    except (ConfigInvalid, UnicodeDecodeError):
        return rules.RECONFIRM_UNREADABLE
    try:
        versions = [recorded] + [_reviewed_at(repo, sha, rel)[1] for sha in shas]  # oldest first
    except (ConfigInvalid, UnicodeDecodeError):
        return rules.RECONFIRM_BRANCH_UNREADABLE
    old = {}  # an id is listed when ANY earlier version records it with another hash than the head (sibling commits have no order)
    for v in versions:
        for rid, h in v.items():
            if h != new_hashes.get(rid):
                old.setdefault(rid, h)
    if not target_has_ledger:  # the head's own commit is no earlier record of it: with no other recorded version there is nothing to compare
        own = 1 if new_hashes and any(v == new_hashes for v in versions[1:]) else 0
        if sum(1 for v in versions if v) - own <= 0:
            return rules.RECONFIRM_FIRST_PR if new_hashes else []  # worth a note only when the head records a rule
    return rules.reconfirmed(old, new_hashes)


def _reconfirmed_warnings(c, target):
    """For `strict check --phase pr|done --base REF` with rules enabled: one warning naming each rule id an earlier ledger records that this
    one lacks or records with another hash, or the fixed note text. A note, never a finding. [] otherwise: the output is the baseline."""
    if target is None or not c.cfg.rules["enabled"] or c.ledger is None or c.phase not in ("pr", "done"):
        return []
    res = reconfirmed_vs_base(c.repo, target, c.id, dod.ledger_rules_reviewed(c.ledger)["hashes"], c.rev)
    if isinstance(res, str):
        return [res]
    return ["rules reconfirmed: %s; %s" % (rules.reconfirmed_text(res), rules.RECONFIRM_NOTE)] if res else []


def _comment_approval(cfg, repo, target):
    """Whether the TARGET branch sets approval "comment" (X4): the judging config when it was taken from the target, else
    `config.trusted(repo, target)`; a target that cannot be read errs toward the note (True)."""
    if getattr(cfg, "from_target", False):
        return cfg.approval == "comment"
    try:
        return config.trusted(str(repo), target).approval == "comment"
    except config.ConfigError:
        return True


def check(cfg, key, phase, repo=".", head="HEAD", base=None, push=False, target=None, client_factory=None):
    """Read-only. `overrides_used`: the ids of the override lines that let a finding through (the apply paths stamp them used).
    `head`: HEAD, a branch or a commit id; `base` (pr): the ref the PR targets, red and green must lie in base..head; `push`: as
    a later `pr create --push`; `target`: the CLI's `--base REF` (CI), which adds the adoption note of `adoption_note` to `warnings`;
    `client_factory`: builds the Jira client the board check (implement) reads with, called only when it runs (None: board-unchecked)."""
    if phase not in PHASES:
        raise ValueError(f"unknown phase (valid: {', '.join(PHASES)})")
    mode = cfg.mode
    if mode != "strict":
        return {"phase": phase, "mode": mode, "ok": True, "findings": [], "note": "mode is advisory"}
    ctx = _Ctx(cfg, key, repo, phase, head, base, push, client_factory)
    cover = overrides(ctx)
    findings, used = [], set()
    for f in (f for rule in PHASES[phase] for f in rule(ctx)):
        if f["rule"] in cover:
            used.add(cover[f["rule"]])
        else:
            findings.append(f)
    out = {"phase": phase, "mode": mode, "ok": not findings, "findings": findings, "overrides_used": _used(used)}
    warnings = _fidelity_warnings(ctx) if phase in ("pr", "done") else []
    trust = _stack_state(ctx)["head"]
    if adoption_note(cfg, repo, target, ctx.rev) or (trust and _stack_note(cfg, trust)):  # review the trust files as code
        warnings.append(TRUST_NOTE)
    warnings += _kits_state(ctx)["warnings"]
    warnings += _reconfirmed_warnings(ctx, target)
    found = _detect_state(ctx)
    n = found.get("ignored_roots", {}).get("count", 0) if found else 0
    if n:  # roots no profile can name: outside the stack rules
        warnings.append(ignored_roots_note(n))
    if _comment_approval(cfg, repo, target):  # a standing note (A5-AC3): no Jira client on this path, so the permissions are not read
        warnings.append(approvals.TAMPER_SENTENCE)
    return {**out, "warnings": warnings} if warnings else out


# ---- L2 test runner: the project's test command at one commit, in a throwaway checkout ----
# The command is code from the project under test. It runs in a throwaway clone outside the repo with an allow-listed
# environment, stdin closed, a timeout and its own process group; nothing it prints is returned or logged (counts only).
# Toolchain locations only, never a credential directory: CARGO_HOME (credentials.toml), GRADLE_USER_HOME (gradle.properties)
# and PUB_CACHE (older Dart kept credentials.json there) are deliberately absent. Profiles cannot add names.
_ENV_ALLOW = ("PATH", "LANG", "TERM", "TMPDIR", "VIRTUAL_ENV", "JAVA_HOME", "GOPATH", "NODE_PATH", "GOROOT", "FLUTTER_ROOT",
              "DOTNET_ROOT", "ANDROID_HOME", "ANDROID_SDK_ROOT", "RUSTUP_HOME")
_ENV_LC = frozenset(("LC_ADDRESS", "LC_ALL", "LC_COLLATE", "LC_CTYPE", "LC_IDENTIFICATION", "LC_MEASUREMENT", "LC_MESSAGES",
                     "LC_MONETARY", "LC_NAME", "LC_NUMERIC", "LC_PAPER", "LC_TELEPHONE", "LC_TIME"))  # not any LC_* (LC_SFDX_AUTH_URL leaked)
_ENV_SDK_PATHS = ("GOROOT", "FLUTTER_ROOT", "DOTNET_ROOT", "ANDROID_HOME", "ANDROID_SDK_ROOT", "RUSTUP_HOME")  # value must be an absolute path
_ENV_DENY_NAMES = {"SKJ_TOKEN", "SKJ_EMAIL", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"}
_ENV_DENY_SUFFIX = ("_TOKEN", "_PAT", "_SECRET", "_KEY", "_PASSWORD", "_CREDENTIALS")


def _denied(name):
    """The runner's deny rule, one source of truth for `scrubbed_env` and `verify`'s refusal: SKJ_*, AWS_*, the named secrets and
    anything named like a token, key, password, secret or credential."""
    k = name.upper()
    return k in _ENV_DENY_NAMES or k.startswith(("AWS_", "SKJ_")) or k.endswith(_ENV_DENY_SUFFIX)


_NAME = re.compile(r"[A-Za-z0-9_]{1,64}")


def _credentials_in_env(env):
    """`credentials-in-env` when `env` holds a name `_denied` drops, else []: names only (an odd one is `?`), never a value."""
    names = sorted(k for k in env if _denied(k))
    if not names:
        return []
    shown = ", ".join(n if _NAME.fullmatch(n) else "?" for n in names[:20]) + (f" and {len(names) - 20} more" if len(names) > 20 else "")
    unset = " ".join("-u " + n for n in names if _NAME.fullmatch(n))
    odd = "; unset the one(s) shown as ? too" if any(not _NAME.fullmatch(n) for n in names) else ""
    return [_finding("credentials-in-env", f"this process's environment holds credential variable(s) {shown}: the test command would run "
                     "as its child and could read them (on Linux from /proc/<parent>/environ), so no test was run",
                     "run `skj strict verify` in a CI step or job whose environment has no SKJ_TOKEN, SKJ_EMAIL or token-named "
                     "variable (and no other credential), e.g. a job of its own that runs last"
                     + (f"; locally: `env {unset} skj strict verify <ref>`" if unset else "") + odd)]


def scrubbed_env(env, home="/nonexistent"):
    """The environment a test run gets: only the allow-list (PATH, LANG, the standard LC_ locale names, TERM, TMPDIR and the toolchain variables
    that are present), then a deny-list pass for secret-looking names (defence in depth: SKJ_*, AWS_*, anything token-named). HOME is
    `home`, never the real one."""
    out = {k: v for k, v in env.items() if k in _ENV_ALLOW or k in _ENV_LC}
    out = {k: v for k, v in out.items() if k not in _ENV_SDK_PATHS or (os.path.isabs(v) and not _has_control(v))}
    out = {k: v for k, v in out.items() if not _denied(k)}
    return {**out, "HOME": home, "PYTHONDONTWRITEBYTECODE": "1"}


def _git_env(env, home="/nonexistent"):
    """`scrubbed_env` for the runner's git calls and for the test command: no GIT_* from the parent (GIT_INDEX_FILE, GIT_DIR,
    GIT_ASKPASS, GIT_SSH*, GIT_CONFIG_*), no system or global git config, no prompts, no replace refs."""
    return {**scrubbed_env(env, home), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1"}


def _git(repo, *args, env, timeout):
    """A finished git run of the runner (bytes) with its own scrubbed `env`, or None when git is missing, cannot start or runs past `timeout`."""
    try:
        return subprocess.run(config._argv(repo, args), check=False, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return None


def _kill_group(p):
    # shortcut: after a normal exit the pid is already reaped; its reuse as a group id within microseconds is ignored
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _count_lines(fd, counts, i):
    """Read `fd` in 64 KiB chunks until EOF, keeping only the number of lines (memory stays constant); owns and closes `fd`."""
    n, last = 0, b"\n"
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            n, last = n + chunk.count(b"\n"), chunk[-1:]
            counts[i] = n + (last != b"\n")
    except OSError:
        pass
    finally:
        os.close(fd)


def _run_group(command, cwd, env, timeout):
    """(exit or None, timed_out, stdout lines, stderr lines, seconds to the process exit); the whole process group dies on a timeout, an interrupt or exit.
    shortcut: a descendant that calls setsid escapes the group; if it also holds the output pipes the drain gives up after
    10 s (its later output is not counted) and the thread stays until it exits."""
    (r_out, w_out), (r_err, w_err), counts, t0 = os.pipe(), os.pipe(), [0, 0], time.monotonic()
    try:
        try:
            p = subprocess.Popen(["/bin/sh", "-c", command], cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=w_out,
                                 stderr=w_err, start_new_session=True)
        finally:
            os.close(w_out)
            os.close(w_err)
    except BaseException:
        os.close(r_out)
        os.close(r_err)
        raise
    readers = [threading.Thread(target=_count_lines, args=(fd, counts, i), daemon=True) for i, fd in enumerate((r_out, r_err))]
    for t in readers:
        t.start()
    timed_out = False
    with p:
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(p)
            p.wait()
        except BaseException:
            _kill_group(p)
            raise
        _kill_group(p)  # background processes the command left behind
    elapsed = time.monotonic() - t0  # not the drain wait below
    end = time.monotonic() + 10
    for t in readers:
        t.join(max(0.0, end - time.monotonic()))
    return (None if timed_out else p.returncode), timed_out, counts[0], counts[1], elapsed


def _rmtree(path):
    """Remove a tree including read-only and mode-000 directories (Go's module cache is 555); iterative, so a deep tree cannot overflow."""
    todo, dirs = [path], []
    while todo:
        d = todo.pop()
        dirs.append(d)
        try:
            os.chmod(d, 0o700)
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            entries = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    todo.append(e.path)
                else:
                    os.unlink(e.path)
            except OSError:
                pass
    for d in reversed(dirs):
        try:
            os.rmdir(d)
        except OSError:
            pass


def run_at(repo, sha, command, timeout):
    """Run `command` (a shell string from the project's config, never built from anything else) at commit `sha` in a
    throwaway clone (`git clone --shared --no-checkout`, origin removed, never fetched) that shares no config, hooks or index
    with the user's repo. Every git call and the command get a scrubbed environment. Returns `{"sha", "exit", "timed_out",
    "seconds", "stdout_lines", "stderr_lines"}`; the output itself is dropped so test output cannot leak a secret. Raises
    ValueError (fixed text) when `sha` is not 7-40 hex characters naming a commit here, or the checkout fails. The temp dir
    is always removed.
    Known limits, not a sandbox (C1 requirement: CI runs `verify` last, or in its own job, without credentials): the `--shared` clone
    borrows the repo's objects through alternates, so the test code can write objects into the source repository; and a `setsid`
    daemon it starts leaves the process group and outlives the group kill."""
    if not (isinstance(sha, str) and re.fullmatch(_HEX, sha)):
        raise ValueError("sha must be 7 to 40 hex characters")
    if not (isinstance(command, str) and command.strip()) or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ValueError("a command and a timeout of at least 1 second are required")
    genv = _git_env(os.environ)
    full = _commit(repo, sha)
    top = _out(repo, "rev-parse", "--show-toplevel")
    if full is None or top is None:
        raise ValueError("the sha is not a commit in this repository (a shallow clone may lack it, or git could not be run)")
    top = os.path.realpath(top.decode("utf-8", "replace").strip())
    tmp = tempfile.mkdtemp(prefix="skj-strict-")
    wt, home = os.path.join(tmp, "wt"), os.path.join(tmp, "home")
    try:
        if os.path.commonpath([os.path.realpath(tmp), top]) == top:
            raise ValueError("the temporary directory must be outside the repository")
        os.mkdir(home)
        no_hooks = ["-c", f"core.hooksPath={os.devnull}"]  # a post-checkout hook would run with the environment
        steps = [(top, "clone", "--shared", "--no-checkout", "--quiet", "--end-of-options", top, wt),
                 (wt, "remote", "remove", "origin"),
                 (wt, "checkout", "--quiet", "--detach", full)]  # `full` is git's own 40-hex output; checkout --detach rejects --end-of-options before git 2.4x
        for where, *args in steps:
            r = _git(where, *no_hooks, *args, env=genv, timeout=120)
            if r is None or r.returncode != 0:
                raise ValueError("could not create a temporary checkout of the commit")
        code, timed_out, n_out, n_err, secs = _run_group(command, wt, _git_env(os.environ, home), timeout)
        return {"sha": full, "exit": code, "timed_out": timed_out, "seconds": round(secs, 2),
                "stdout_lines": n_out, "stderr_lines": n_err}
    finally:
        try:
            _rmtree(tmp)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)


def run_configured(cfg, repo, sha):
    """run_at with `strict.test_command` and `strict.test_timeout` from the (already validated) config."""
    return run_at(repo, sha, cfg.strict.get("test_command"), cfg.strict.get("test_timeout"))


# ---- `skj strict verify`: L1, then the test command at every red and green commit, then the record ----
def _with_record(md, heading, lines):
    """`md` with the earlier live `### Verification` headings of section 4 marked superseded and the new block appended at
    the end of section 4 (before `## 5.`), in the ledger's own line ending. Nothing else changes."""
    span = dod.section_span(md, 4)
    if span is None or dod.unclosed(md):
        raise ValueError("the ledger has no section 4 (`## 4.`), or an unclosed code fence or comment; nothing written")
    start, end = span
    mask = dod._mask(md)  # same length as md: offsets found here slice the original
    cuts = [m.end() for m in _V_HEADING.finditer(mask, start, end) if not m.group(0).rstrip().endswith(_SUPERSEDED.strip())]
    parts, last = [], 0
    for at in cuts:  # one join, not one copy of the ledger per heading (20000 headings took 2.2 s)
        parts += [md[last:at], _SUPERSEDED]
        last = at
    md, end = "".join(parts) + md[last:], end + len(_SUPERSEDED) * len(cuts)
    eol = "\r\n" if "\r\n" in md else "\n"
    before = md[:end]
    pre = "" if before.endswith(("\n\n", "\r\n\r\n")) else eol if before.endswith("\n") else eol * 2
    return before + pre + eol.join([heading, *lines, ""]) + eol + md[end:]


def _write_record(path, repo, old, heading, lines):
    """Append the record to the ledger at `path` through a temp file + os.replace; refuses (nothing written) when the
    ledger changed while the tests ran."""
    new = _with_record(old, heading, lines)
    if gate._read(path, repo, set()) != old:
        raise ValueError("the ledger changed while the tests ran; nothing written, run it again")
    try:
        decisions._atomic_write(Path(path), new)
    except (OSError, UnicodeError) as e:
        raise ValueError(f"cannot write the ledger ({e.__class__.__name__}); nothing written")


def verify(cfg, key, record=True, base=None, repo=".", allow_override=False):
    """L1 for every AC, then the test command at each AC's red commit (must exit non-zero) and green commit (must exit 0),
    once per distinct commit. Strict mode only. With `record` and a pass, appends the verification record to ledger
    section 4. With `record` False (CI) an override in force is the finding `override-in-force` unless `allow_override`. It applies no stack
    or kit rule: those bind at `check`. Returns `{"mode","ok","findings","recorded",...}`; nothing is returned or logged from the tests' output."""
    mode = cfg.mode
    if mode != "strict":
        return {"mode": mode, "ok": True, "findings": [], "note": "mode is advisory"}
    leaked = _credentials_in_env(os.environ)  # before anything is read or run: never overridable
    if leaked:
        return {"mode": mode, "ok": False, "findings": leaked, "recorded": False, "overrides_used": []}
    c = _Ctx(cfg, key, repo)
    rows = gate.parse_section3(c.ledger) if c.ledger is not None else []
    findings = _ledger(c) + _l1(c) + _test_command(c)
    if c.ledger is not None and not rows and not any(f["rule"] == "evidence-missing" for f in findings):
        findings.append(_finding("evidence-missing", "ledger section 3 has no AC row", _EVIDENCE_FIX))
    if base is not None and len(rows) <= MAX_ROWS:  # (over the cap check_l1 already said so)
        findings += _range_findings(rows, repo, _resolve_base(repo, base))  # the same resolver as `check`: never a tag
    cover, used = overrides(c), set()  # like `check`: the findings an override covers are dropped and the entry reported
    findings = [f for f in findings if f["rule"] not in cover or used.add(cover[f["rule"]])]
    skipped = "test-command" in cover or "record-missing" in cover  # an `l2` / `test-command` override: no run, and no record to fake
    if skipped and not findings:
        used |= {cover[r] for r in ("test-command", "record-missing") if r in cover}
    if used and not record and not allow_override:  # the CI re-run: an override would skip the very run it exists for
        findings.append(_finding("override-in-force", f"the override(s) {', '.join(_used(used))} would let this re-run pass without it",
                                 "remove the override line, or pass --allow-override to `skj strict verify` to accept it"))
    if findings:
        return {"mode": mode, "ok": False, "findings": findings, "recorded": False, "overrides_used": _used(used)}
    if skipped:
        return {"mode": mode, "ok": True, "findings": [], "recorded": False, "overrides_used": _used(used),
                "note": "verification skipped: overridden"}
    head = _rev(repo, "HEAD")
    if not head:
        raise ValueError("cannot resolve HEAD to a commit; nothing written")
    runs, timeout = {}, cfg.strict.get("test_timeout")  # a failed run is cached too: one failure, not one per AC

    def run(full):
        if full not in runs:
            try:
                runs[full] = run_configured(cfg, repo, full)
            except ValueError:
                runs[full] = None
        return runs[full]
    lines = []
    for row in rows:
        ac, ev, shown = row["ac"], parse_evidence(row["status"]), []
        for kind in ("red", "green"):
            full = _commit(repo, ev[kind])
            r = run(full)
            if r is None:
                findings.append(_l1_finding("runner-error", ac, f"the {kind} run could not be started: the checkout of the commit failed "
                                            "or git could not be run", "use a full clone (fetch-depth: 0) with a working git, then re-run"))
            elif r["timed_out"]:
                findings.append(_l1_finding("timeout", ac, f"the {kind} run timed out after {timeout} s",
                                            "make the tests faster, or raise strict.test_timeout in skj.config.json"))
            elif kind == "red" and r["exit"] == 0:
                findings.append(_l1_finding("red-passes", ac, f"the tests pass at the red commit {full[:12]}: a red commit must fail",
                                            "make the red commit's test fail without the code (assert the new behaviour), "
                                            "or correct the red SHA or strict.test_command"))
            elif kind == "green" and r["exit"] != 0:
                findings.append(_l1_finding("green-fails", ac, f"the tests fail at the green commit {full[:12]} (exit {r['exit']})",
                                            "make the green commit pass the test command, or correct the green SHA"))
            else:
                shown.append(f"{kind} {full[:12]} exit {r['exit']}")
        if len(shown) == 2:
            lines.append(f"- {ac}: {' · '.join(shown)} · L1 ok")
    r = run(head)  # the record says `@ <head>`: the tests must pass there too (cached when HEAD is a recorded green)
    if head not in {_commit(repo, parse_evidence(row["status"])["green"]) for row in rows}:  # a green HEAD was judged above
        if r is None:
            findings.append(_finding("runner-error", "the HEAD run could not be started: the checkout of the commit failed or git "
                                     "could not be run", "use a full clone (fetch-depth: 0) with a working git, then re-run"))
        elif r["timed_out"]:
            findings.append(_finding("timeout", f"the HEAD run timed out after {timeout} s",
                                     "make the tests faster, or raise strict.test_timeout in skj.config.json"))
        elif r["exit"] != 0:
            findings.append(_finding("head-fails", f"the tests fail at HEAD {head[:12]} (exit {r['exit']}): a later commit broke them",
                                     "fix the code on this branch so the test command passes at HEAD, then re-run verify"))
    seconds = round(sum(r["seconds"] for r in runs.values() if r), 2)
    out = {"mode": mode, "ok": not findings, "findings": findings, "recorded": False, "runs": len(runs), "seconds": seconds,
           "overrides_used": _used(used)}
    if findings or not record:
        return out
    cmd = _command_hash(cfg.strict["test_command"])
    _write_record(Path(repo) / "dod" / keys.dod_name(key), repo, c.ledger,
                  f"### Verification — {date.today().isoformat()} @ {head[:12]}",
                  [*lines, f"- head: {head[:12]} exit {runs[head]['exit']}", f"- command-sha256: {cmd}", f"- seconds: {seconds}"])
    return {**out, "recorded": True}
