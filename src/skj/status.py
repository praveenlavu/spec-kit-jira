"""`skj status KEY`: the first unmet stage of a story and the one command that moves it on. Read-only: local files, git and a few Jira
GETs (the story's status, its remote links, its comments, its subtasks); nothing is written anywhere, no local file and no Jira request
other than a GET.

`STAGES` is data: `(id, needs_board, met(ctx), next(ctx))`, in lifecycle order. A board stage needs Jira: offline, with no usable client, or
after one failed read it is NOT CHECKED (listed in `not_checked`), never met. Every text here is fixed: stage ids, counts, the story key (a
validated key), an HTTP status number and the one pull request URL that passed `prlink.check_url`; no ledger, spec, decision, comment or
Jira text and no error message is ever printed. A file that cannot be read (a symlink, outside the working tree, over 1 MiB, not UTF-8)
leaves its stage unmet. Process help, not a security boundary: the ledger, the spec dir and the decisions file come from the branch.

Done is the story's status CATEGORY (`fields.status.statusCategory.key == "done"`, K3); `resolution` is never requested or read.

A pull request link is INFORMATION: remote links are writable by anyone with the Link-issues permission, so the URL of one that passes the
P2 validator is shown ("a pull request link is attached: URL") and moves the displayed stage on, but it never grants anything (the gate and
strict decide merges). `skj status` runs no strict-mode check (the approvals cross-check runs in `skj done`, the rest in `skj strict check`): a note says so.

Order after the review: `merged` (local: git only) comes BEFORE `pr-link` (the remote link `skj pr link` adds after the merge). Without GitHub
an open PR cannot be seen, so while the work has not landed the answer is `skj pr create` only if the branch was never pushed
(`refs/remotes/origin/<branch>` is missing), else `git fetch origin`; a link alone never moves the answer past `merged`.

The `review` and `gate` stages read the WORKING-TREE ledger (the fork read the review from the ledger at HEAD): informational only.

Run awareness (the Spec Kit run state) and `--catch-up` are not in this build: they depend on the `jira-lifecycle` workflow (C1)."""
import os
import re

from . import comments, config, decisions, dod, done, gate as gate_mod, issues, keys, prlink, prs, push, questions, rules, stack, strict, tasks
from .client import AuthError, ClientError, JiraError
from .config import ConfigError, ConfigInvalid

_GATE_HEADING = re.compile(r"^### Gate\b[^\r\n]*", re.M)
_BLOCKING = re.compile(r"^- blocking:\s*(yes|no)\s*$", re.M | re.I)
_HEADING_SHA = re.compile(r"@ `([^`]*)`")
_SHA_TEXT = re.compile(r"[0-9a-fA-F]{7,40}")
BOARD_NOT_CHECKED = "board-not-checked"  # the answer when every stage that could be read is met but a board stage could not be
STRICT_ONLY = ("stack-profile", "verified", "decisions-closed")
# What the agent-run Spec Kit commands are called (K8). They are prompts, not CLI verbs: F2 ships them, tests/test_status.py pins the names.
EXTENSION_COMMANDS = ("plan", "tasks", "implement", "gate", "review", "done", "stack")
_SHAPE_ERRORS = (ValueError, KeyError, TypeError, AttributeError)  # an answer that is not what the reader expects
DOWN_TEXT = {"offline": "offline: --offline was given", "no-client": "no usable Jira client (identity or site pin): run `skj doctor`",
             "error": "Jira could not be read"}
BY_HAND = "fix the ledger by hand (no command does it), then run `skj status` again"
REPORT_LINE = "status reports; skj gate and CI decide"
DOWN_LINE = "Jira not reachable: showing local state only (%s)"
NOT_READ_NOTE = "board not checked: no Jira request was needed for the stages up to this one, so none was sent"
STRICT_NOTE = ("Strict mode: when the target branch names approvers, `skj done` runs the decisions cross-check (approvals): it re-reads "
               "the story's comments from Jira (GETs only) and refuses on any finding, such as a recorded approval that is missing, edited "
               "or no longer valid, or a Jira that cannot be read; `skj status` does not run it; `skj strict check` judges the rest; this "
               "answer shows the ledger stages only")
PR_NOTE = ("a pull request link is attached: %s (information only: anyone who may link issues can add a link, and the gate and strict "
           "decide merges)")


class _Down(Exception):
    """A board read failed (or the board is not read at all): the stage is not checked."""


class Ctx:
    def __init__(self, factory, key, repo, offline, cfg_loader):
        self.id, self.repo, self.factory, self.client, self.reason, self._memo = key, repo, factory, None, "", {}
        self.down, self.http = ("offline" if offline else None), None  # why the board is not read: offline | no-client | error
        self.auth = False  # the error was a refused login (D-003)
        self.read = False  # a board read answered
        self.config_state = "ok"
        try:
            self.cfg = (cfg_loader or (lambda r: config.Config.load(r)))(repo)
            if self.cfg.project != key.rsplit("-", 1)[0]:
                self.cfg, self.config_state = None, "project"
        except ConfigInvalid:
            self.cfg, self.config_state = None, "invalid"
        except ConfigError:
            self.cfg, self.config_state = None, "missing"
        self.strict_mode = self.cfg is not None and self.cfg.mode == "strict"

    @property
    def s(self):
        """The strict checks' own context: confined reads of the ledger, the spec dir files and the decisions file."""
        if "s" not in self._memo:
            self._memo["s"] = strict._Ctx(self.cfg, self.id, self.repo)
        return self._memo["s"]

    def memo(self, key, fn):
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    def board(self, fn):
        """fn(client) for a board read. A Jira failure ends all board reads for this run; an answer of the wrong shape (or a refusal of
        the reader) leaves only this stage unchecked."""
        if self.down:
            raise _Down
        if self.client is None:
            try:
                self.client = self.factory() if self.factory else None
            except Exception:  # noqa: BLE001 - no identity, a refused site pin, a bad config: the text is never shown
                self.client = None
            if self.client is None:
                self.down = "no-client"
                raise _Down
        try:
            return fn(_Sent(self.client, self))  # D-005: board_checked needs a request that was sent and answered, not only a reader that returned
        except ClientError as e:
            self.down, self.http = "error", (e.status if isinstance(e, JiraError) and type(e.status) is int else None)
            self.auth = isinstance(e, AuthError)
            raise _Down from None
        except _SHAPE_ERRORS:
            raise _Down from None

    def story(self):
        """The story's `fields.status` (the only field read; `resolution` is never asked for), once."""
        return self.memo("story", lambda: self.board(lambda cl: done._status(issues.get(cl, self.id, ["status"]))))

    def tasks(self):
        text = self.s.file("tasks.md")
        return None if text is None else tasks.parse_tasks(text)

    def ops(self):
        """The dry-run plan of `skj push` (GETs only), once."""
        return self.memo("ops", lambda: self.board(lambda cl: push.plan(cl, self.cfg, self.id, self.tasks())["ops"]))

    def pr_urls(self):
        """The pull request URLs among the story's remote links (see `_link_url`), once."""
        return self.memo("pr_urls", lambda: self.board(lambda cl: _pr_urls(cl, self.id)))


class _Sent:
    """The client, but a request that answers marks the run as having read the board (DF3, D-005: a reader may return with no request)."""

    def __init__(self, client, ctx):
        self._client, self._ctx = client, ctx

    def __getattr__(self, name):
        return getattr(self._client, name)

    def request(self, *a, **kw):
        out = self._client.request(*a, **kw)
        self._ctx.read = True
        return out


def _n(count, what):
    return "%d %s" % (count, what)


def _link_url(link):
    """The PR URL of one remote link, else None. `globalId` and `object.url`, whichever are present, must all pass the P2 validator
    (`prlink.check_url`) and be the same string, in a link whose `object` is an object: a hostile link (another scheme or host, a look-alike, two different values) is ignored."""
    obj = link.get("object")
    if not isinstance(obj, dict):  # Jira always sends one (P2's own reader refuses a list without it)
        return None
    vals = [v for v in (link.get("globalId"), obj.get("url")) if v is not None]
    try:
        urls = {prlink.check_url(v) for v in vals}
    except ValueError:
        return None
    return urls.pop() if len(urls) == 1 else None


def _pr_urls(client, key):
    data = client.request("GET", "/issue/%s/remotelink" % key).data
    if not (isinstance(data, list) and len(data) <= prlink.MAX_LINKS
            and all(isinstance(x, dict) and prlink._link_id(x.get("id")) for x in data)):
        raise ValueError(prlink.E_LINKS)  # bad data: only this stage is left unchecked, Jira itself answered
    return [u for u in map(_link_url, data) if u]


# ---- met(ctx): True, or False with ctx.reason set to a fixed text (else the stage's default in _WHY) ----
def _config(c):
    c.reason = {"invalid": "skj.config.json is invalid", "project": "the story key is not of the configured project"}.get(
        c.config_state, "no skj.config.json in this directory")
    return c.cfg is not None


def _config_next(c):
    return "skj doctor" if c.config_state in ("invalid", "project") else "skj setup"


def _ledger(c):
    if c.s.ledger is None:
        return False
    if dod.unclosed(c.s.ledger):
        c.reason = ("the ledger has an unclosed code fence or comment in section 1, so the sections after it cannot be read"
                    if dod._repair_at(c.s.ledger) != -1 else
                    "the ledger has an unclosed code fence or comment outside section 1: close it by hand (a refresh refuses it)")
        return False
    return True


def _ledger_next(c):
    if c.s.ledger is None:  # a file that is there but cannot be read is fixed by hand: a seed refuses to replace it
        return BY_HAND if _ledger_exists(c) else "skj dod seed %s" % c.id
    return "skj dod refresh %s" % c.id if dod._repair_at(c.s.ledger) != -1 else BY_HAND


def _ledger_exists(c):
    return os.path.lexists(os.path.join(c.repo, "dod", keys.dod_name(c.id)))


def _stack_profile(c):
    result, _, findings = stack.show(c.repo, "strict", c.cfg.strict["test_command"])
    c.memo("stack_status", lambda: result["status"])
    return findings == 0


def _stack_profile_next(c):
    return "skj stack approve" if c.memo("stack_status", lambda: "none") in ("draft", "changed-since-approval") \
        else "speckit.jira-lifecycle.stack %s" % c.id


def _rules(c):
    """The rules stage (rules.enabled only): `rules.coverage` over the committed files, offline, at phase `pr`; ids and counts only."""
    led = c.s.ledger
    cov = rules.coverage(led, {}, rules.local_files(led, c.repo), dod.ledger_rules_from(led), dod.ledger_rules_reviewed(led), c.s.decisions, "pr")
    c.memo("rules_cov", lambda: cov)
    if "rules-from" in cov["header"]:
        c.reason = "the ledger has no valid `Rules from:` line"
    elif cov["missing"] or cov["format"]:
        c.reason = _n(len(cov["missing"]) + len(cov["format"]), "rules file(s) named by `Rules from:` are missing, unusable or in another format")
    elif cov["header"]:  # the fork's table let this case fall into the count above ("0 rules file(s)"): named here
        c.reason = "`Rules reviewed:` is malformed: a human repairs the ledger header by hand, then confirms"
    elif cov["changed"]:
        c.reason = _n(len(cov["changed"]), "rule(s) differ from the hash recorded in `Rules reviewed:`: a human re-confirms them in their own terminal")
    else:
        c.reason = _n(len(cov["unconfirmed"]), "in-scope rule(s) are not confirmed yet: run the confirm yourself, no terminal is needed")
    return not (cov["header"] or cov["missing"] or cov["format"] or cov["unconfirmed"] or cov["changed"])


def _rules_next(c):
    cov = c._memo["rules_cov"]
    if "rules-from" in cov["header"]:
        return "skj dod refresh %s" % c.id
    if cov["missing"] or cov["format"]:
        return "skj rules %s" % c.id
    return BY_HAND if cov["header"] else "skj rules %s --confirm" % c.id


def _questions_posted(c):
    parsed = questions.parse_questions(c.s.ledger)
    if parsed["unparsed"]:
        c.reason = "ledger section 2 has an unclosed code fence or comment, or a line over 2000 characters, so its questions cannot be read"
        return False
    plan = c.board(lambda cl: questions.plan_post(cl, c.cfg, c.id, parsed, c.s.ledger))
    return plan["action"] in ("none", "noop")


def _questions_posted_next(c):
    return BY_HAND if questions.parse_questions(c.s.ledger)["unparsed"] else \
        "skj questions %s --post --plan-out .skj/plan-questions.md" % c.id


def _questions_answered(c):
    opened = questions.parse_questions(c.s.ledger)["open"]
    if any(q["id"] == "?" for q in opened):
        c.reason = "ledger section 2 has an unclosed code fence or comment, so its questions cannot be read"
    else:
        c.reason = _n(len(opened), "open question(s) in ledger section 2 wait for an answer")
    return not opened


def _plan(c):
    if c.s.spec_dir is None:
        c.reason = "the ledger records no Spec dir, so plan.md cannot be found"
    return c.s.file("plan.md") is not None


def _tasks(c):
    return bool(c.tasks())


def _board_tasks(c):
    ids = [t["id"] for t in c.tasks()]
    if len(set(ids)) != len(ids):
        c.reason = "tasks.md repeats a task id, so the board cannot be compared"
        return False
    n = sum(op["action"] == "create" for op in c.ops())
    c.reason = _n(n, "task(s) in tasks.md have no subtask on the story")
    return n == 0


def _tasks_ticked(c):
    n = sum(not t["done"] for t in c.tasks())
    c.reason = _n(n, "unticked task(s) in tasks.md")
    return n == 0


def _board_sync(c):
    n = sum(op["action"] in ("transition", "refused") for op in c.ops())
    c.reason = _n(n, "task(s) differ between tasks.md and the board")
    return n == 0


def _decisions_posted(c):
    s = c.s
    if not s.decisions_ok or s.decisions["unparsed"]:
        c.reason = "the decisions file cannot be read in full (unreadable, a line breaks the grammar, or a fence or comment is open)"
        return False
    todo = decisions.unposted(s.decisions)
    if todo:
        c.reason = _n(len(todo), "decision line(s) not posted to the story")
        return False
    lines = s.decisions["open"] + [d for d in s.decisions["validated"] if d["kind"] == "override"]
    wanted = {d["posted"]["comment_id"] for d in lines if d["posted"]}
    if not wanted:
        return True
    # a `posted:` marker is a local claim: it counts only when a decisions comment with that id is on the story (not proof of who wrote it)
    found = c.board(lambda cl: {r.id for r in comments.fetch(cl, c.id) if decisions._MARKER_RE.match(r.md.split("\n", 1)[0])})
    c.reason = _n(len(wanted - found), "posted marker(s) name a decisions comment that is not on the story")
    return wanted <= found


def _verified(c):
    found = strict._record_done(c.s) if _landed(c) else strict._record(c.s)
    rule = found[0]["rule"] if found else ""
    c.reason = {"record-missing": "ledger section 4 has no verification record",
                "record-stale": "the verification record is stale: the code changed after it"}.get(
                    rule, "the verification record is incomplete, failing or made with another test command")
    return not found


def _headings(ledger_md, kind):
    """The `### <kind>` heading lines of ledger section 4 (outside fences and comments), a trailing ` (superseded)` removed."""
    log = dod._visible(dod.section(ledger_md, 4) or "")[0]
    return [re.sub(r" \(superseded\)$", "", m.group(0).rstrip()) for m in re.finditer(r"^### %s\b[^\r\n]*" % kind, log, re.M)]


def _landed(c):
    """The work is on the default branch `origin/HEAD` points at: the ledger as committed there (`git show`, read through
    `config._blob_at`) holds the latest review (else gate) heading of this ledger. A commit that merely names the story does not count.
    After a squash or rebase merge the recorded SHAs are no ancestors of HEAD, so the staleness of the gate, review and verification
    record is not judged then (as `skj done` would not)."""
    def go():
        base = strict.default_base(c.repo)
        commit = strict._resolve_base(c.repo, base) if base else ""
        mine = (_headings(c.s.ledger, "Review") or _headings(c.s.ledger, "Gate"))[-1:]
        if not (commit and mine):
            return False
        try:
            raw = strict._blob_at(c.repo, commit, "dod/" + keys.dod_name(c.id), gate_mod.MAX_READ, "the story ledger at the default branch")
            there = _headings(raw.decode("utf-8"), "Review") + _headings(raw.decode("utf-8"), "Gate") if raw is not None else []
        except (ConfigError, ValueError):  # ConfigInvalid is a ConfigError; UnicodeDecodeError is a ValueError
            return False
        return mine[0] in there
    return c.memo("landed", go)


def _logged_sha(heading):
    sha = _HEADING_SHA.search(heading)
    return sha.group(1) if sha and _SHA_TEXT.fullmatch(sha.group(1)) else None


GATE_HINT = "; run `skj gate %s` for the result, then log it in section 4 (the gate prompt does this)"


def _gate(c):
    ok = _gate_met(c)
    if not ok:
        c.reason += GATE_HINT % c.id
    return ok


def _gate_met(c):
    log = dod._visible(dod.section(c.s.ledger, 4) or "")[0]
    live = list(_GATE_HEADING.finditer(log))
    if not live:
        c.reason = "ledger section 4 has no gate result"
        return False
    rest = log[live[-1].end():]
    nxt = re.search(r"^#{1,3} ", rest, re.M)
    verdicts = _BLOCKING.findall(rest[:nxt.start()] if nxt else rest)
    sha = _logged_sha(live[-1].group(0))
    if sha is None or len(verdicts) != 1:
        c.reason = "the latest gate result has no commit or no single `blocking:` line"
        return False
    if verdicts[0].lower() == "yes":
        c.reason = "the latest gate result is blocking"
        return False
    if not _landed(c) and strict._record_stale(c.repo, "HEAD", sha, c.id):
        c.reason = "the gate result is stale: the code changed after it"
        return False
    return True


def _review(c):
    """The latest live review of ledger section 4 through the readers P1 offers (`prs.review_summary`, `prs.review_refusal`): its verdict
    and counts, with `review_block_on` or else `blocker`; then, unless the work has landed, its commit against HEAD
    (`strict._record_stale`: later commits may touch only the ledger and the decisions file, stricter than the fork's review check)."""
    rev = prs.review_summary(c.s.ledger)
    why = prs.review_refusal(rev, c.cfg.effective_review_block_on() or ["blocker"])
    if why is not None:
        c.reason = ("no review is logged in ledger section 4" if not rev["found"] else
                    "the latest review blocks the PR" if rev["blocks"] == "yes" else why)
        return False
    if _landed(c):
        return True
    log = dod._visible(dod.section(c.s.ledger, 4) or "")[0]
    heads = [m for m in re.finditer(r"^### Review\b[^\r\n]*", log, re.M) if not m.group(0).rstrip().endswith("(superseded)")]
    sha = _logged_sha(heads[-1].group(0)) if heads else None
    if sha is None:
        c.reason = "the latest review records no commit"
        return False
    if strict._record_stale(c.repo, "HEAD", sha, c.id):
        c.reason = "the review is stale: the code changed after it"
        return False
    return True


def _pr_link(c):
    urls = c.pr_urls()
    if urls:
        c.memo("pr_url", lambda: urls[0])
    return bool(urls)


def _pr_link_next(c):
    return "skj pr link %s --url <PR-URL> --title=<title>" % c.id


def _on_origin(c):
    """True when `refs/remotes/origin/<current branch>` exists (the branch was pushed: a PR may exist). A detached HEAD, a name that is
    not a plain ref name or any git failure is False (never pushed, as far as git can tell). Local git only, scrubbed environment."""
    ref = (config._out(c.repo, "symbolic-ref", "--quiet", "HEAD") or b"").decode("utf-8", "replace").strip()
    name = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""
    if not name or config._BAD_BASE.search(name):
        return False
    return bool(config._rev(c.repo, "refs/remotes/origin/" + name))


def _merged(c):
    """The work has landed on the default branch `origin/HEAD` points at (see `_landed`). Local only. Unmet: a branch never pushed has no
    PR (open it); a pushed one may have one that git cannot see (wait for the merge, then fetch). A link is shown if one is attached."""
    if not strict.default_base(c.repo):
        c.reason = "origin/HEAD is not set, so the default branch cannot be compared"
        return False
    if _landed(c):
        return True
    pushed = c.memo("pushed", lambda: _on_origin(c))
    c.reason = ("open the pull request: the branch is not on origin yet" if not pushed else
                "the PR is open or not created yet (status cannot see GitHub): open it with skj pr create %s if needed, wait for review and "
                "merge, then fetch" % c.id)
    if not c.down:
        try:  # information only: a link never moves the answer past this stage
            urls = c.pr_urls()
        except _Down:
            urls = []
        if urls:
            c.memo("pr_url", lambda: urls[0])
    return False


def _merged_next(c):
    if not strict.default_base(c.repo):
        return "git remote set-head origin --auto"
    return "git fetch origin" if c.memo("pushed", lambda: _on_origin(c)) else "skj pr create %s" % c.id


def _done_state(c):
    if c.story()["category"] == "done":  # K3: the status category; `resolution` is never read
        return True
    c.reason = ("the story is not in a done status category: `skj done %s` shows the plan (the done prompt ticks section 5 with evidence, "
                "then runs it)" % c.id)
    return False


def _done_state_next(c):
    # `skj done` refuses in strict mode in this build, and while a decision is open: the command that closes them comes first
    return "skj decisions %s" % c.id if c.strict_mode and _open_decisions(c) else "speckit.jira-lifecycle.done %s" % c.id


def _open_decisions(c):
    s = c.s
    if not s.decisions_ok or s.decisions["unparsed"]:
        return -1
    eff = decisions.effective(s.decisions)
    return len(eff["open"]) + len(eff["rejected"])


def _decisions_closed(c):
    n = _open_decisions(c)
    c.reason = ("the decisions file cannot be read in full" if n < 0 else _n(n, "decision(s) are open or rejected"))
    return n == 0


_WHY = {
    "config": "no valid skj.config.json", "ledger": "no readable ledger for this story (start here: missing, a symlink, outside the working tree, over 1 MiB or not UTF-8)",
    "stack-profile": "the stack profile is not approved, drifts from strict.test_command, or leaves a detected root uncovered",
    "rules": "the business rules are not pulled, or not confirmed in the ledger",
    "questions-posted": "the open questions are not (or no longer) posted to the story",
    "questions-answered": "questions wait for an answer: the product owner replies on the story, or close them by hand as assumed",
    "plan": "no plan.md in the spec dir", "tasks": "no tasks.md with tasks in the spec dir",
    "board-tasks": "tasks are missing on the board", "tasks-ticked": "tasks are unticked", "board-sync": "board states differ from tasks.md",
    "decisions-posted": "decisions are not posted to the story", "verified": "no current verification record",
    "gate": "no current, non-blocking gate result in the ledger", "review": "no current, non-blocking review in the ledger",
    "pr-link": "the story has no pull request link (CI's link job adds it after the merge; run it by hand only if that job did not)",
    "merged": "the pull request is not merged",
    "done-state": "the story is not in a done status category", "decisions-closed": "decisions are open or rejected",
}
STAGES = [
    ("config", False, _config, _config_next),
    ("ledger", False, _ledger, _ledger_next),
    ("stack-profile", False, _stack_profile, _stack_profile_next),
    ("rules", False, _rules, _rules_next),
    ("questions-posted", True, _questions_posted, _questions_posted_next),
    ("questions-answered", False, _questions_answered, lambda c: "speckit.jira-lifecycle.plan %s" % c.id),
    ("plan", False, _plan, lambda c: "speckit.jira-lifecycle.plan %s" % c.id),
    ("tasks", False, _tasks, lambda c: "speckit.jira-lifecycle.tasks %s" % c.id),
    ("board-tasks", True, _board_tasks, lambda c: "skj push %s --plan-out .skj/plan-push.md" % c.id),
    ("tasks-ticked", False, _tasks_ticked, lambda c: "speckit.jira-lifecycle.implement %s" % c.id),
    ("board-sync", True, _board_sync, lambda c: "skj push %s --plan-out .skj/plan-sync.md" % c.id),
    ("decisions-posted", True, _decisions_posted, lambda c: "skj decisions %s --post --if-any --plan-out .skj/plan-decisions.md" % c.id),
    ("verified", False, _verified, lambda c: "skj strict verify %s" % c.id),
    ("gate", False, _gate, lambda c: "speckit.jira-lifecycle.gate %s" % c.id),
    ("review", False, _review, lambda c: "speckit.jira-lifecycle.review %s" % c.id),
    ("merged", False, _merged, _merged_next),
    ("pr-link", True, _pr_link, _pr_link_next),
    ("done-state", True, _done_state, _done_state_next),
    ("decisions-closed", False, _decisions_closed, lambda c: "skj decisions %s" % c.id),
    ("done", False, lambda c: True, lambda c: "none"),
]
_STAGE = {row[0]: row for row in STAGES}


def applicable(cfg):
    """The stage ids that apply to a config: the strict-only stages in strict mode, `rules` when `rules.enabled`."""
    return [sid for sid, *_ in STAGES if (sid not in STRICT_ONLY or cfg.mode == "strict") and (sid != "rules" or cfg.rules["enabled"])]


def _judge(c, sid):
    """`(ok, reason)` of one stage, once per Ctx: ok is True (met), False (unmet) or None (a board stage that could not be read)."""
    judged = c.memo("judged", dict)
    if sid not in judged:
        _, needs_board, met, _ = _STAGE[sid]
        c.reason = ""
        try:
            if needs_board and c.down:
                raise _Down
            judged[sid] = (bool(met(c)), c.reason)
        except _Down:
            judged[sid] = (None, "")
        except Exception as e:  # noqa: BLE001 - a stage that cannot be judged is unmet, with the exception CLASS only
            judged[sid] = (False, "this stage could not be checked (%s)" % type(e).__name__)
    return judged[sid]


def _follow(c, sid):
    try:
        return _STAGE[sid][3](c)
    except Exception:  # noqa: BLE001
        return "skj doctor"


def _walk(c):
    """(stage id or None, its next command, why, skipped board stage ids): the first unmet stage in order; a board stage that cannot be
    read is skipped."""
    keep = set(applicable(c.cfg)) if c.cfg else set(sid for sid, *_ in STAGES)
    skipped = []
    for sid, *_ in STAGES:
        if sid not in keep:
            continue
        ok, why = _judge(c, sid)
        if ok is None:
            skipped.append(sid)
        elif not ok:
            return sid, _follow(c, sid), why or _WHY[sid], skipped
    return None, "", "", skipped


def evaluate(client_factory, key, repo=".", offline=False, cfg_loader=None):
    """`{"stage", "next", "why", "board_checked", "not_checked", "board", "notes"}`. `stage` is the first stage that is unmet; a board stage
    that could not be read is skipped and listed in `not_checked`; when nothing readable is unmet but a board stage was skipped the stage
    is `board-not-checked`, never `done`. `board` is why Jira was not read (`offline`, `no-client` or `error`) or None. `client_factory`
    (a callable returning a Client, called at the first board read; None: no client) is never called with `offline`. `cfg_loader(repo)`:
    the config that judges (default `Config.load`; the CLI passes the target-branch aware `_judging_config`). `notes` are fixed lines."""
    if not keys.valid_key(key):
        raise ValueError("the story key is not a valid issue key")
    c = Ctx(client_factory, key, repo, offline, cfg_loader)
    sid, follow, why, skipped = c.memo("walk", lambda: _walk(c))
    if sid is None and skipped:
        sid = BOARD_NOT_CHECKED
        follow = "skj status %s" % key if c.down == "offline" else "skj doctor"
        why = ("Jira was not read (offline), so the stages that need it are not checked" if c.down == "offline" else
               "Jira could not be read, so the stages that need it are not checked")
    elif sid is None:
        sid, follow, why = "done", "none", "every stage is met"
    notes = [PR_NOTE % c._memo["pr_url"]] if "pr_url" in c._memo else []
    if c.strict_mode and sid not in ("done", "config"):
        notes.append(STRICT_NOTE)
    detail = DOWN_TEXT.get(c.down, "")
    if c.down == "error" and c.http is not None:
        detail += " (HTTP %d%s)" % (c.http, ", authentication failed" if c.auth else "")
    if c.down is None and not c.read:
        notes.append(NOT_READ_NOTE)
    return {"stage": sid, "next": follow, "why": why, "board_checked": c.down is None and not skipped and c.read, "not_checked": skipped,
            "board": c.down, "notes": ([DOWN_LINE % detail] if c.down else []) + notes}


def render(res, key):
    stage = "board not checked" if res["stage"] == BOARD_NOT_CHECKED else res["stage"]
    lines = ["Story %s: %s. Next: %s" % (key, stage, res["next"]), "Why: %s" % res["why"]]
    if res["not_checked"] and res["stage"] != BOARD_NOT_CHECKED:
        lines.append("Not checked: %s" % ", ".join(res["not_checked"]))
    return "\n".join(lines + res["notes"] + [REPORT_LINE])


# ---- `skj dod check <ref> --after <step>` (DF7, D-025/D-028): did an agent `command:` step of the workflow leave its product? ----------
# `claude -p` exits 0 when the agent stops on a refused tool call, so the engine carries on; the workflow runs this after each agent step.
AFTER_STEPS = ("pull", "plan", "tasks", "implement", "gate", "review")
_SPEC_FILE = {"pull": "spec.md", "plan": "plan.md", "tasks": "tasks.md"}
HEADLESS_DOC = "docs/quick-start.md#the-workflow-runs-the-agent-headless"


def _logged(s, repo, key, kind):
    """Why the latest live `### <kind>` block of section 4 is not a current result of a run, or None. Fixed texts only."""
    log = dod._visible(dod.section(s.ledger, 4) or "")[0]
    heads = [m.group(0).rstrip() for m in re.finditer(r"^### %s\b[^\r\n]*" % kind, log, re.M) if not m.group(0).rstrip().endswith("(superseded)")]
    what = kind.lower()
    if not heads:
        return "ledger section 4 has no %s" % ("gate result" if kind == "Gate" else "review")
    if re.search(r"\bnot run\b", heads[-1], re.I):  # D-028: a block that says it did not run is no review
        return "the latest %s says it did not run" % what
    if kind == "Review":
        rev = prs.review_summary(s.ledger)
        if rev["problem"] or rev["blocks"] is None:
            return "the latest review is not well formed (no single `Confirmed by severity:` and `Blocks PR:` line)"
    sha = _logged_sha(heads[-1])
    if sha is None:
        return "the latest %s names no commit" % what
    if strict._record_stale(repo, "HEAD", sha, key):
        return "the latest %s is stale: the code changed after it" % what
    return None


def after_step(cfg, key, repo, step):
    """Why the product of agent step `step` (AFTER_STEPS) is missing, or None. Local files and git only: the ledger (pull: Spec dir and
    spec.md; plan: plan.md and a filled section 2; tasks: a task in tasks.md; implement: section 3 rows; gate and review: a current
    block in section 4), all committed (the ledger, the decisions file, the spec dir: each command ends with that commit)."""
    s = strict._Ctx(cfg, key, repo)
    if s.ledger is None:
        return "no readable ledger dod/%s" % keys.dod_name(key)
    rel = prs._spec_rel(s.spec_dir, "") if s.spec_dir and re.fullmatch(r"[A-Za-z0-9._/-]+", s.spec_dir) else None  # D-022's shape
    if step in _SPEC_FILE:
        if rel is None:
            return "the ledger records no usable Spec dir"
        if s.file(_SPEC_FILE[step]) is None:
            return "%s is missing or unreadable in the spec dir" % _SPEC_FILE[step]
    if step == "plan" and strict._placeholders(s):
        return "ledger section 2 is missing, empty or still a placeholder (Approach, Assumptions, Use cases)"
    if step == "tasks" and not tasks.parse_tasks(s.file("tasks.md")):
        return "tasks.md has no task line"
    if step == "implement" and "_(what it must do)_" in dod._visible(dod.section(s.ledger, 3) or "")[0]:
        return "ledger section 3 still has the placeholder row (one row per AC)"
    if step in ("gate", "review"):
        why = _logged(s, repo, key, step.capitalize())
        if why:
            return why
    paths = ["dod/" + keys.dod_name(key), "dod/Decisions_%s.md" % key] + ([rel] if rel else [])
    out = config._out(repo, "status", "--porcelain", "--untracked-files=all", "--", *paths)
    if out is None:
        return "git could not tell whether the files are committed"
    if out.strip():
        return "the ledger, the decisions file or the spec dir has uncommitted changes (the step ends with a commit)"
    return None


def after_step_line(step, why):
    return ("the %s step did not finish: %s. If the agent was refused a command, give it the headless permissions (%s); then run the "
            "step's command by hand and resume, or start a new run" % (step, why, HEADLESS_DOC))
