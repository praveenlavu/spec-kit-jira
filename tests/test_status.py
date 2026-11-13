"""`skj status` (R6): the first unmet stage of a story and the one command that moves it on.

Ported from the fork's tests/test_status.py. Every scenario is a real temporary git repository with a ledger, a spec dir and commits (the gate,
review and verification record are judged against git) and the fake Jira (tests/fakejira.py, plugged in through `Client(opener=...)`: no
socket) that answers the few Jira reads status makes. The fake's request log proves the verb only ever sends GETs. All data is fictional.

Rebased on the Jira model: the story is the key SPK-1 (the fork's `1001`), the board is the story's subtasks and comments, a "done" story is
a status CATEGORY `done` (K3, `resolution` is never read), a pull request is a REMOTE LINK that passes P2's URL validator (the fork reads
artifact links). Changed, dropped and new tests are listed in task-R6-report.md."""
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

import skj.__main__ as m
from fakejira import FakeJira
from skj import adf, client as cl, decisions, dod, questions, status
from skj.client import Client
from skj.config import Config
from test_stack_support import approve, commit, git, profile, git_env  # noqa: F401  (git_env: fixture)

KEY = "SPK-1"
SITE = "example-site.atlassian.net"
UNTRUSTED = "ZZ-JIRA-TEXT"
PR_URL = "https://github.com/example-org/example-repo/pull/12"
OTHER_URL = "https://github.com/example-org/example-repo/pull/13"
TASKS = {"ticked": "- [x] T001 Do it\n", "open": "- [ ] T001 Do it\n"}
COMMAND = "PYTHONPATH=src python3 -m pytest -q"  # what the approved test profile composes
NEXT = {  # the exact next command of every stage
    "ledger": "skj dod seed %s" % KEY, "questions-posted": "skj questions %s --post --plan-out .skj/plan-questions.md" % KEY,
    "questions-answered": "speckit.jira-lifecycle.plan %s" % KEY, "plan": "speckit.jira-lifecycle.plan %s" % KEY,
    "tasks": "speckit.jira-lifecycle.tasks %s" % KEY,
    "board-tasks": "skj push %s --plan-out .skj/plan-push.md" % KEY, "tasks-ticked": "speckit.jira-lifecycle.implement %s" % KEY,
    "board-sync": "skj push %s --plan-out .skj/plan-sync.md" % KEY,
    "decisions-posted": "skj decisions %s --post --if-any --plan-out .skj/plan-decisions.md" % KEY,
    "verified": "skj strict verify %s" % KEY, "gate": "speckit.jira-lifecycle.gate %s" % KEY, "review": "speckit.jira-lifecycle.review %s" % KEY,
    "merged": "skj pr create %s" % KEY, "pr-link": "skj pr link %s --url <PR-URL> --title=<title>" % KEY,
    "done-state": "speckit.jira-lifecycle.done %s" % KEY, "decisions-closed": "skj decisions %s" % KEY,
}
LINK_NEXT = NEXT["pr-link"]
FETCH = "git fetch origin"
# the 13 prompts the extension ships (F2; the names are the fork's command files) and the ones status points at
EXTENSION_PROMPTS = ("constitution", "done", "gate", "implement", "plan", "pr", "pull", "push", "review", "setup", "stack", "status", "tasks")
GIT_COMMANDS = ("git fetch origin", "git remote set-head origin --auto")


# ---- the Jira side -----------------------------------------------------------------------------------------------------------------

def add_link(fake, url, **over):
    """A remote link as Jira lists it: an integer id, a globalId and an object (P2 writes both with the PR URL)."""
    link = {"id": fake._next_id("link"), "globalId": url, "object": {"url": url, "title": "SPK-1: a fictional pull request"}}
    link.update(over)
    fake.issue(KEY)["links"].append(link)
    return link


def jira(state="Done", links=(PR_URL,), children=(("T001", "Done"),), summary="A fictional story"):
    """The fake with the story SPK-1 (default: complete: Done, one valid PR link, T001 done) and its subtasks."""
    f = FakeJira()
    assert f.add_issue(summary, status=state) == KEY
    for tid, st in children:
        f.add_issue("%s Do it" % tid, type="Subtask", parent=KEY, labels=("skj",), status=st)
    for url in links:
        add_link(f, url)
    return f


def posted_questions(fake, *texts):
    """A questions comment by the kit's own account, as W4 posts it."""
    qs = [{"id": "Q%d" % i, "question": t, "owner": "PO"} for i, t in enumerate(texts, 1)]
    return fake.add_comment(KEY, questions.body(qs)[1])


def posted_decisions(fake, line):
    cid = fake.add_comment(KEY, decisions.body([line])[1])
    return cid


class Tamper:
    """An opener that serves the fake's answer with `mutate(path, data)` applied to the JSON body: what a hostile or odd server might say."""

    def __init__(self, fake, mutate):
        self.fake, self.mutate = fake, mutate

    def __call__(self, method, url, headers, body, timeout):
        status_, h, out = self.fake(method, url, headers, body, timeout)
        path = url.split("/rest/api/3", 1)[1].split("?")[0]
        if status_ == 200 and out:
            data = json.loads(out)
            data = self.mutate(path, data)
            out = json.dumps(data).encode("utf-8")
        return status_, h, out

    def client(self):
        return Client(self.fake.site, self.fake.email, self.fake.token, opener=self)


# ---- the repository side -----------------------------------------------------------------------------------------------------------

def review_md(sha, blocks="no", superseded=False, blocker=0, major=0):
    tail = " (superseded)" if superseded else ""
    verdict = "yes — `review_block_on` = [blocker] and 1 such finding(s) remain" if blocks == "yes" else "no"
    return ("### Review — 2026-10-28 — `main...feat` @ `%s`%s\n\n- **Confirmed by severity:** blocker %d · major %d · minor 0\n"
            "- **Blocks PR:** %s\n\n" % (sha, tail, blocker, major, verdict))


def ledger_text(sha, *, spec_dir="specs/001-x", qlines="- [x] Q1: what? → yes · owner: PO\n", gate="ok", review="ok", record="", header="",
                red="aaaaaaa", green="bbbbbbb", old="ccccccc"):
    blocks = ""
    if gate:
        blocks += ("### Gate — 2026-10-28 — `main...HEAD` @ `%s`\n- traceability: ok\n- blocking: %s\n\n"
                   % (old if gate == "stale" else sha, "yes" if gate == "blocking" else "no"))
    if review:
        blocks += review_md(old if review == "stale" else sha, blocks="yes" if review == "blocks" else "no")
    blocks += record
    return ("# DoD — %s: title\n\n- **Spec dir:** %s\n%s\n## 1. Seeded from Jira\n\n### Acceptance Criteria\nit works\n\n"
            "%s\n- **Approach:** a\n- **Open questions → answers:**\n%s\n"
            "## 3. AC → tests\n\n| AC | acceptance criterion | tests | commit/PR | status |\n|---|---|---|---|---|\n"
            "| AC-1 | it works | tests/test_a.py::test_a | %s | red %s → green %s |\n\n"
            "## 4. Gate & review log\n%s\n## 5. Definition of Done\n- [x] a\n"
            % (KEY, spec_dir, header, dod.SECTION2, qlines, green, red, green, blocks))


def record_block(head, red, green, command):
    return ("### Verification — 2026-10-30 @ %s\n- AC-1: red %s exit 1 · green %s exit 0 · L1 ok\n- head: %s exit 0\n"
            "- command-sha256: %s\n- seconds: 1.0\n\n"
            % (head[:12], red[:12], green[:12], head[:12], hashlib.sha256(command.encode()).hexdigest()[:12]))


@pytest.fixture
def env(tmp_path, monkeypatch, git_env):
    for k in ("SKJ_CONFIG", "CI", "GITHUB_ACTIONS", "GH_HOST", "SKJ_EMAIL", "SKJ_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("skj.stack.detect", lambda root: {"outcome": "unknown", "stacks": [], "domain_hints": []})
    return tmp_path


def world(repo, *, mode="advisory", cfg=None, questions="answered", plan=True, tasks="ticked", spec=True, decisions_md=None, gate="ok", review="ok",
          record=False, merged=True, ledger=True, header="", stack_profile="approved", early_ledger=False):
    """A complete story by default (every stage met); each keyword breaks one thing."""
    git(repo, "init", "-q", "-b", "main")
    init = commit(repo, "chore: init", {"README.md": "x\n"})
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    if early_ledger:  # the ledger was pulled on the default branch before the work started
        commit(repo, "docs: pull SPK-1", {"dod/DoD_%s.md" % KEY: ledger_text("abc1234", gate=None, review=None)})
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-q", "-b", "feat/spk-1-x")
    data = {"site": SITE, "project": "SPK", **({"mode": "strict", "strict": {"test_command": COMMAND}} if mode == "strict" else {}), **(cfg or {})}
    files = {"skj.config.json": json.dumps(data)}
    if spec:
        files["specs/001-x/spec.md"] = "# spec\n"
        if plan:
            files["specs/001-x/plan.md"] = "# plan\n"
        if tasks:
            files["specs/001-x/tasks.md"] = TASKS[tasks]
    if decisions_md is not None:
        files["dod/Decisions_%s.md" % KEY] = decisions_md
    if mode == "strict" and stack_profile != "none":
        files["skj.stack.json"] = json.dumps(approve(profile()) if stack_profile == "approved" else profile())
    red = commit(repo, "test: red SPK-1", {"tests/test_a.py": "def test_a():\n    assert 0\n", **files})
    green = commit(repo, "feat: green SPK-1", {"src/app.py": "x = 1\n"})
    head = git(repo, "rev-parse", "HEAD")
    if ledger:
        rec = record_block(head, git(repo, "rev-parse", red), git(repo, "rev-parse", green), COMMAND) if record else ""
        qlines = {"answered": "- [x] Q1: what? → yes · owner: PO\n", "open": "- [ ] Q1: what? → _(open)_ · owner: PO\n", "none": ""}[questions]
        commit(repo, "docs: ledger SPK-1", {"dod/DoD_%s.md" % KEY: ledger_text(green, qlines=qlines, gate=gate, review=review, record=rec,
                                                                           header=header, red=red, green=green, old=init)})
    if merged:
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return Config.load(str(repo))


def world_again(env, **kw):
    """A second world in the same directory: start over."""
    for child in env.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    return world(env, **kw)


def valid_command(cmd):
    """Every command status shows is a real invocation: a `skj` line parses against the argparse tree (a verb that exists, its flags, no
    --apply: a suggestion is always a preview), an extension prompt is one the extension ships, a git line one of two fixed texts."""
    if cmd in ("none", status.BY_HAND):
        return
    words = shlex.split(cmd)
    assert "--apply" not in words, cmd
    if words[0] == "skj":
        try:
            m.build_parser().parse_args(words[1:])
        except SystemExit:
            pytest.fail("not a valid skj invocation: " + cmd)
    elif words[0].startswith("speckit.jira-lifecycle."):
        assert words[0].split(".", 2)[2] in EXTENSION_PROMPTS and words[1:] == [KEY], cmd
    else:
        assert cmd in GIT_COMMANDS, cmd


def check(repo, fake=None, stage=None, why=None, nxt=None, **kw):
    res = status.evaluate(fake.client if fake is not None else None, KEY, str(repo), **kw)
    valid_command(res["next"])
    if stage is not None:
        assert res["stage"] == stage, res
        if nxt or stage in NEXT:
            assert res["next"] == (nxt or NEXT[stage]), res
    if why is not None:
        assert why in res["why"], res
    return res


# ---- the table ---------------------------------------------------------------------------------------------------------------------

def test_a_complete_story_is_done(env):
    world(env)
    f = jira()
    res = check(env, f, "done")
    assert res["next"] == "none" and res["board_checked"] is True and res["not_checked"] == [] and res["board"] is None
    assert res["notes"] == ["a pull request link is attached: %s (information only: anyone who may link issues can add a link, and the gate "
                            "and strict decide merges)" % PR_URL]


def test_stages_are_data_in_the_documented_order():
    ids = [s[0] for s in status.STAGES]
    assert ids == ["config", "ledger", "stack-profile", "rules", "questions-posted", "questions-answered", "plan", "tasks", "board-tasks",
                   "tasks-ticked", "board-sync", "decisions-posted", "verified", "gate", "review", "merged", "pr-link", "done-state",
                   "decisions-closed", "done"]  # the fork's table (`rules` back after `stack-profile` since RU3)
    assert all(len(s) == 4 and isinstance(s[1], bool) and callable(s[2]) and callable(s[3]) for s in status.STAGES)
    assert {s[0] for s in status.STAGES if s[1]} == {"questions-posted", "board-tasks", "board-sync", "decisions-posted", "pr-link", "done-state"}


def test_every_fixed_next_command_is_valid():
    for cmd in [*NEXT.values(), LINK_NEXT, "skj doctor", "skj setup", "skj dod refresh %s" % KEY, "skj status %s" % KEY, "skj stack approve",
                "git remote set-head origin --auto", "none"]:
        valid_command(cmd)


def test_the_command_check_itself_rejects_a_bad_command():
    for bad in ("skj puch %s" % KEY, "skj push %s --apply" % KEY, "skj push %s --plan-out" % KEY, "skj pr link", "speckit.jira-lifecycle.nope %s" % KEY,
                "git push", "speckit.other.plan 1"):
        with pytest.raises((AssertionError, pytest.fail.Exception)):
            valid_command(bad)


# ---- one test per stage ----
def test_stage_config(env):
    check(env, None, "config", "no skj.config.json")
    assert check(env)["next"] == "skj setup"
    (env / "skj.config.json").write_text("{not json")
    res = check(env, None, "config", "invalid")
    assert res["next"] == "skj doctor"
    (env / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "OTHER"}))
    res = check(env, None, "config", "not of the configured project")
    assert res["next"] == "skj doctor"


def test_stage_ledger(env):
    world(env, ledger=False)
    res = check(env, None, "ledger")
    assert "start here" in res["why"]
    # a symlinked ledger is never read: fail closed, and a seed would refuse to replace it, so it is by hand
    (env / "dod").mkdir(exist_ok=True)
    (env / "outside.md").write_text(ledger_text("abc1234"))
    os.symlink(env / "outside.md", env / "dod" / ("DoD_%s.md" % KEY))
    check(env, None, "ledger", nxt=status.BY_HAND)


def test_stage_stack_profile(env):
    world(env, mode="strict", stack_profile="none")
    res = check(env, jira(), "stack-profile")
    assert res["next"] == "speckit.jira-lifecycle.stack %s" % KEY
    world_again(env, mode="strict", stack_profile="draft")
    assert check(env, jira(), "stack-profile")["next"] == "skj stack approve"


def test_a_complete_strict_story_is_done(env):
    world(env, mode="strict", record=True)
    res = check(env, jira(), "done")
    assert STRICT_NOTE_PART not in " ".join(res["notes"])  # nothing left to refuse


STRICT_NOTE_PART = "Strict mode:"


def test_fru_ac3_strict_note_states_what_done_checks():
    """Fix-RU (task-RU3-review.md finding 3): since A3 `skj done` runs the approvals cross-check (`_xcheck` -> `strict.xcheck_run`,
    GETs only, only when the target branch names approvers); `skj status` itself never runs it."""
    import inspect
    note = status.STRICT_NOTE
    assert "not available" not in note
    assert note.startswith("Strict mode: when the target branch names approvers, `skj done` runs the decisions cross-check (approvals)")
    assert "GETs only" in note and "`skj status` does not run it" in note and "`skj strict check` judges the rest" in note
    assert "_xcheck(" in inspect.getsource(m.cmd_done) and "if not trust.approvers:" in inspect.getsource(m._xcheck)
    assert "_xcheck" not in inspect.getsource(status)


def _rules_file():
    """Rules_SPK-900.md as `skj rules` writes it: one approved rule (hand-written, fictional)."""
    from skj import rules
    doc = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": x}]} for x in (
        "Business rules", "Rule 1: Members earn points.", "Why: w", "Example: e", "Owner: Loyalty product owner", "Status: approved")]}
    lines, probs = rules.normalise(doc)
    text = rules.render_file({"key": "SPK-900", "fields": {"summary": "Points", "issuetype": {"name": "Epic"}}}, rules.parse(lines, "SPK-900", probs),
                             SITE, "2026-11-03", "0.1.0")
    return text, rules.parse_file(text, "SPK-900")["rules"]["BR-SPK-900-1"]["hash"]


@pytest.mark.rules
def test_stage_rules(env):
    """RU3 (the fork's rules stage): with rules on, the stage after `stack-profile` names the one command that moves it on."""
    rules_file, rule_hash = _rules_file()
    world(env, cfg={"rules": {"enabled": True}})  # a ledger with no `Rules from:` line
    res = check(env, jira(), "rules", nxt="skj dod refresh %s" % KEY)
    p = env / "dod" / ("DoD_%s.md" % KEY)
    p.write_text(p.read_text().replace("- **Spec dir:** specs/001-x\n", "- **Spec dir:** specs/001-x\n- **Rules from:** SPK-900\n"))
    assert check(env, jira(), "rules", nxt="skj rules %s" % KEY)["why"].startswith("1 rules file(s)")  # Rules_SPK-900.md is not there
    (env / "dod" / "Rules_SPK-900.md").write_text(rules_file)
    res = check(env, jira(), "rules", nxt="skj rules %s --confirm" % KEY)  # first confirm: no hash recorded yet, free
    assert res["why"] == "1 in-scope rule(s) are not confirmed yet: run the confirm yourself, no terminal is needed"
    p.write_text(p.read_text().replace("- **Rules from:** SPK-900\n", "- **Rules from:** SPK-900\n- **Rules reviewed:** BR-SPK-900-1@%s\n" % rule_hash))
    assert check(env, jira())["stage"] != "rules"
    p.write_text(p.read_text().replace(rule_hash, "deadbeef"))
    res = check(env, jira(), "rules", nxt="skj rules %s --confirm" % KEY)  # a recorded hash that is not the rule's: a human re-confirms
    assert res["why"] == "1 rule(s) differ from the hash recorded in `Rules reviewed:`: a human re-confirms them in their own terminal"
    p.write_text(p.read_text().replace("@deadbeef", "@XYZ"))  # a malformed record: by hand, never a confirm (it refuses one)
    res = check(env, jira(), "rules", nxt=status.BY_HAND)
    assert "malformed" in res["why"] and "XYZ" not in json.dumps(res)


@pytest.mark.rules
def test_rules_stage_only_when_enabled(env):
    world(env)  # rules off: no Rules lines, and the stage is not even looked at
    assert check(env, jira(), "done")["stage"] == "done"
    assert "rules" not in status.applicable(Config(SITE, "SPK", mode="strict"))
    assert "rules" in status.applicable(Config(SITE, "SPK", rules={"enabled": True}))


def test_strict_only_stages_skipped_in_advisory(env):
    world(env, gate="ok")  # advisory: no profile, no verification record, rejected decision lines
    (env / "dod" / ("Decisions_%s.md" % KEY)).write_text("- [x] D1: a → rejected · by: PO · note: no\n")
    assert check(env, jira(), "done")["stage"] == "done"
    ids = status.applicable(Config(SITE, "SPK"))
    assert not {"stack-profile", "verified", "decisions-closed"} & set(ids)
    assert {"stack-profile", "verified", "decisions-closed"} <= set(status.applicable(Config(SITE, "SPK", mode="strict")))


def test_stage_questions_posted(env):
    world(env, questions="open")
    check(env, jira(), "questions-posted")
    # a posted comment that no longer matches section 2 is out of date too
    f = jira()
    posted_questions(f, "something else")
    check(env, f, "questions-posted")


def test_stage_questions_answered(env):
    world(env, questions="open")
    f = jira()
    posted_questions(f, "what?")
    res = check(env, f, "questions-answered", "1 open question")
    assert res["board_checked"] is True


def test_stage_plan(env):
    world(env, plan=False)
    check(env, jira(), "plan")


def test_stage_tasks(env):
    world(env, tasks=None)
    check(env, jira(), "tasks")
    (env / "specs/001-x/tasks.md").write_text("no tasks here\n")
    check(env, jira(), "tasks")
    (env / "specs/001-x/tasks.md").unlink()  # a symlinked tasks.md is never read
    (env / "elsewhere.md").write_text(TASKS["ticked"])
    os.symlink(env / "elsewhere.md", env / "specs/001-x/tasks.md")
    check(env, jira(), "tasks")


def test_stage_board_tasks(env):
    world(env)
    check(env, jira(children=()), "board-tasks", "1 task")
    (env / "specs/001-x/tasks.md").write_text("- [x] T001 a\n- [x] T001 b\n")
    check(env, jira(), "board-tasks", "repeats")


def test_stage_tasks_ticked(env):
    world(env, tasks="open")
    check(env, jira(children=(("T001", "To Do"),)), "tasks-ticked", "1 unticked")


def test_stage_board_sync(env):
    world(env)  # ticked in tasks.md, still To Do on the board
    check(env, jira(children=(("T001", "To Do"),)), "board-sync", "1 task")


DECISION = "- [ ] D1: use sqlite → _(open)_ · kind: decision · why: simple"
D_LINE = {"id": "D1", "kind": "decision", "text": "use sqlite", "why": "simple"}


def posted_line(cid):
    return "%s · posted: %s:%s\n" % (DECISION, cid, decisions.line_hash("use sqlite"))


def test_stage_decisions_posted(env):
    world(env, decisions_md=DECISION + "\n")
    check(env, jira(), "decisions-posted", "1 decision")
    # a marker the file carries must match a decisions comment that is really on the story
    (env / "dod" / ("Decisions_%s.md" % KEY)).write_text(posted_line("9"))
    check(env, jira(), "decisions-posted", "not on the story")
    f = jira()
    other = f.add_comment(KEY, adf.md_to_adf("an unrelated comment"))
    (env / "dod" / ("Decisions_%s.md" % KEY)).write_text(posted_line(other))
    check(env, f, "decisions-posted", "not on the story")
    f = jira()
    cid = posted_decisions(f, decisions.parse_decisions(DECISION + "\n")["open"][0])
    (env / "dod" / ("Decisions_%s.md" % KEY)).write_text(posted_line(cid))
    assert check(env, f)["stage"] != "decisions-posted"


def test_stage_verified(env):
    world(env, mode="strict", record=False)
    res = check(env, jira(), "verified", "no verification record")
    assert res["next"] == "skj strict verify %s" % KEY


def test_stage_verified_stale(env):
    world(env, mode="strict", record=True, merged=False)
    commit(env, "feat: more SPK-1", {"src/more.py": "y = 2\n"})  # code after the record
    check(env, jira(), "verified", "stale")


def test_stage_gate(env):
    world(env, gate=None)
    res = check(env, jira(), "gate", "no gate result")
    # `skj gate` writes nothing to the ledger: the prompt logs it, so the prompt is the next command (no dead loop) and the CLI is the hint
    assert res["next"] == "speckit.jira-lifecycle.gate %s" % KEY
    assert "run `skj gate %s` for the result, then log it in section 4 (the gate prompt does this)" % KEY in res["why"]


def test_stage_gate_stale(env):
    world(env, gate="stale", merged=False)
    check(env, jira(), "gate", "stale")


def test_stage_gate_blocking(env):
    world(env, gate="blocking")
    check(env, jira(), "gate", "blocking")


def test_stage_review_missing(env):
    world(env, review=None)
    check(env, jira(), "review", "no review")


def test_stage_review_blocks(env):
    world(env, review="blocks")
    check(env, jira(), "review", "blocks")


def test_stage_review_stale(env):
    world(env, review="stale", merged=False)
    check(env, jira(), "review", "stale")


def test_stage_pr_link(env):
    world(env)  # merged and fetched: the link is what is missing
    res = check(env, jira(links=()), "pr-link", "CI's link job")
    assert res["notes"] == []
    check(env, jira(links=("https://github.com/example-org/example-repo/tree/main",)), "pr-link")  # a branch is not a PR


def test_pr_from_remote_link(env):
    """R6-AC1: the story's PR is a remote link whose globalId (and object.url) is a GitHub pull request URL that passes P2's validator."""
    world(env)
    f = jira(links=())
    check(env, f, "pr-link", nxt=LINK_NEXT)
    add_link(f, PR_URL)
    res = check(env, f, "done")
    assert res["notes"] == [status.PR_NOTE % PR_URL]  # shown as information
    # a link written without the object's url, and one with several links around it, count too
    f = jira(links=("https://github.com/example-org/example-repo/commit/abc",))
    add_link(f, OTHER_URL, object={"title": "t"})
    assert check(env, f)["stage"] == "done"
    # it is read with one GET of /remotelink, and the verdict is the validator's, not the link's word
    assert ("GET", "/issue/%s/remotelink" % KEY) in [(e["method"], e["path"]) for e in f.log]


HOSTILE = [
    "javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "ftp://github.com/o/r/pull/1",
    "http://github.com/example-org/example-repo/pull/12", "https://evil.example/example-org/example-repo/pull/12",
    "https://github.com.evil.example/o/r/pull/12", "https://GitHub.com/o/r/pull/12", "https://github.com:443/o/r/pull/12",
    "https://user@github.com/o/r/pull/12", "https://github.com/o/r/pull/12?x=1", "https://github.com/o/r/pull/12#c1",
    "https://github.com/o/r/pull/12/", "https://github.com/o/r/pull/12/files", "https://github.com/o/r/pull/0", "https://github.com/o/r/pull/012",
    "https://github.com/o/r/pull/%31", "https://github.com/o/r/issues/12", "https://github.com/o/r/pull/12\nhttps://x", " " + PR_URL,
    PR_URL + " ", PR_URL + "‮", "https://github.com/o/r/pull/1" + "2" * 300, "https://github.com/o/../pull/12", "",
]


@pytest.mark.parametrize("url", HOSTILE)
def test_a_hostile_remote_link_is_ignored(env, url):
    world(env)
    f = jira(links=())
    add_link(f, url)
    res = check(env, f, "pr-link", nxt=LINK_NEXT)
    assert res["notes"] == [] and res["board"] is None


def test_a_link_whose_two_urls_differ_or_one_is_hostile_is_ignored(env):
    world(env)
    for over in ({"object": {"url": OTHER_URL}}, {"object": {"url": "javascript:alert(1)"}}, {"globalId": "javascript:alert(1)"},
                 {"globalId": 5}, {"globalId": ["x"]}, {"object": "text"}, {"object": None, "globalId": None}, {"globalId": {"url": PR_URL}}):
        f = jira(links=())
        add_link(f, PR_URL, **over)
        check(env, f, "pr-link", nxt=LINK_NEXT)
    f = jira(links=())
    add_link(f, "javascript:alert(1)")
    add_link(f, PR_URL)  # one honest link among hostile ones is still found
    assert check(env, f, "done")["notes"] == [status.PR_NOTE % PR_URL]


BAD_LISTS = ([{"id": True, "globalId": PR_URL}], [{"id": "7", "globalId": PR_URL}], [{"id": 0}], [{"id": -1}], ["x"], [None], {"id": 1},
             [{"id": 1}] * 1001, "text")


def test_a_remote_link_list_of_the_wrong_shape_leaves_only_pr_link_unchecked(env):
    """M1: Jira answered, the data is bad: only `pr-link` is not checked; the other Jira stages still run and no 'Jira not reachable' is shown."""
    world(env)
    for bad in BAD_LISTS:
        f = jira(links=())
        t = Tamper(f, lambda path, data, bad=bad: bad if path.endswith("/remotelink") else data)
        res = status.evaluate(t.client, KEY, str(env))
        assert res["stage"] == "board-not-checked" and res["board"] is None and res["not_checked"] == ["pr-link"], bad
        assert res["board_checked"] is False and not any("Jira not reachable" in n for n in res["notes"]) and PR_URL not in json.dumps(res)
        # the stage after it is still judged
        f = jira(state="In Progress", links=())
        t = Tamper(f, lambda path, data, bad=bad: bad if path.endswith("/remotelink") else data)
        res = status.evaluate(t.client, KEY, str(env))
        assert res["stage"] == "done-state" and res["not_checked"] == ["pr-link"] and res["board"] is None, bad


def test_stage_merged(env):
    world(env, merged=False)
    check(env, jira(), "merged", nxt=NEXT["merged"])  # the branch was never pushed: open the PR
    git(env, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")  # nothing to compare with: not met, never guessed
    check(env, jira(), "merged", "origin/HEAD", nxt="git remote set-head origin --auto")


def test_merged_next_depends_on_whether_the_branch_is_on_origin(env):
    """I1: no network, so an open PR is invisible: a branch that was never pushed cannot have one (`skj pr create`); a pushed one may
    (`git fetch origin` after the merge), and `skj pr create` is then never suggested (gh refuses a duplicate)."""
    world(env, merged=False)
    res = check(env, jira(), "merged", "open the pull request", nxt="skj pr create %s" % KEY)
    git(env, "update-ref", "refs/remotes/origin/feat/spk-1-x", "HEAD")  # pushed
    res = check(env, jira(), "merged", "status cannot see GitHub", nxt=FETCH)
    assert "pr create" in res["why"] and res["next"] == FETCH
    # a detached HEAD or a branch git cannot place on origin counts as never pushed
    git(env, "checkout", "-q", "--detach")
    check(env, jira(), "merged", nxt="skj pr create %s" % KEY)
    git(env, "checkout", "-q", "-b", "feat/odd;branch")
    check(env, jira(), "merged", nxt="skj pr create %s" % KEY)
    assert not (env / "branch").exists()


def test_merged_but_not_fetched_says_fetch(env):
    world(env, merged=False)
    git(env, "update-ref", "refs/remotes/origin/feat/spk-1-x", "HEAD")
    git(env, "checkout", "-q", "main")
    git(env, "merge", "-q", "--ff-only", "feat/spk-1-x")  # merged on main here, origin/main not fetched yet
    res = check(env, jira(), "merged", nxt=FETCH)
    assert "pr create" in res["why"]
    git(env, "update-ref", "refs/remotes/origin/main", "HEAD")  # fetched
    check(env, jira(links=()), "pr-link", nxt=LINK_NEXT)
    check(env, jira(), "done")


def test_a_link_alone_never_moves_the_answer_past_merged(env):
    """I1: an open PR with a (forged or real) remote link stays at `merged`; the link is shown as information only."""
    world(env, merged=False)
    for pushed in (False, True):
        if pushed:
            git(env, "update-ref", "refs/remotes/origin/feat/spk-1-x", "HEAD")
        res = check(env, jira(links=(PR_URL,)), "merged", nxt=NEXT["merged"] if not pushed else FETCH)
        assert res["notes"] == [status.PR_NOTE % PR_URL]
        assert check(env, jira(links=("javascript:alert(1)",)), "merged", nxt=NEXT["merged"] if not pushed else FETCH)["notes"] == []  # a hostile one is not even shown


def test_merged_unmet_still_answers_when_jira_is_down(env):
    world(env, merged=False)
    f = jira()
    f.fail_transport(OSError("x"), on=1)
    res = status.evaluate(f.client, KEY, str(env))
    assert res["stage"] == "merged" and res["next"] == NEXT["merged"] and res["board"] == "error"
    assert res["notes"][0].startswith("Jira not reachable: showing local state only")


def test_pr_link_next_is_the_link_command_once_the_work_has_landed(env):
    world(env)  # on the default branch, no link yet: the link is added on merge
    res = check(env, jira(links=()), "pr-link", "run it by hand only if that job did not", nxt=LINK_NEXT)
    assert res["next"].startswith("skj pr link %s --url " % KEY)


def test_an_earlier_commit_naming_the_story_is_not_a_merge(env):
    # the ledger was pulled on main (a commit naming the story) and the PR is not merged: the default branch lacks the review/gate headings
    world(env, merged=False, early_ledger=True)
    assert "SPK-1" in git(env, "log", "--format=%s", "origin/main")
    check(env, jira(), "merged")
    # a sibling commit on main naming a longer key changes nothing either
    git(env, "update-ref", "refs/remotes/origin/main", git(env, "commit-tree", "-m", "feat: other SPK-12", "-p", "origin/main", "origin/main^{tree}"))
    check(env, jira(), "merged")


@pytest.mark.parametrize("how", ["ff", "merge", "squash"])
@pytest.mark.parametrize("mode", ["advisory", "strict"])
def test_merged_story_is_done_on_main_however_it_was_merged(env, how, mode):
    # after a merge the user checks out main; a squash or rebase leaves the recorded SHAs unreachable, which must not re-open gate, review, verified
    world(env, mode=mode, record=mode == "strict", merged=False)
    git(env, "checkout", "-q", "main")
    if how == "squash":
        git(env, "merge", "--squash", "feat/spk-1-x")
        git(env, "commit", "-q", "-m", "Add feature SPK-1 (#5)")
    else:
        git(env, "merge", "-q", *(["--ff-only"] if how == "ff" else ["--no-ff", "-m", "Merge pull request SPK-1"]), "feat/spk-1-x")
    git(env, "update-ref", "refs/remotes/origin/main", "HEAD")
    check(env, jira(), "done")


def test_squash_merge_without_a_fetch_is_not_merged_yet(env):
    world(env, merged=False)
    git(env, "checkout", "-q", "main")
    git(env, "merge", "--squash", "feat/spk-1-x")
    git(env, "commit", "-q", "-m", "Add feature SPK-1 (#5)")  # origin/main still at the first commit
    res = check(env, jira())
    assert res["stage"] == "gate", res  # not on the default branch as far as git knows, so the old SHAs are judged as they are


def test_stage_done_state(env):
    world(env)
    res = check(env, jira(state="In Progress"), "done-state")
    assert res["next"] == "speckit.jira-lifecycle.done %s" % KEY and "`skj done %s` shows the plan" % KEY in res["why"]


def test_every_prompt_status_names_is_in_the_extension_list():
    used = {shlex.split(c)[0].split(".", 2)[2] for c in NEXT.values() if c.startswith("speckit.")}
    assert used <= set(status.EXTENSION_COMMANDS) and {"gate", "done"} <= used


def test_the_text_always_ends_with_the_report_line(env, capsys):
    world(env)
    assert run(jira()) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "status reports; skj gate and CI decide"


def test_done_by_status_category_not_resolution(env):
    """R6-AC2: done is `fields.status.statusCategory.key == "done"` and nothing else. `resolution` is null on team-managed projects, so a
    story whose category is done and whose resolution is null is Done; a resolution on a story whose category is not done does not make it so."""
    world(env)

    def serve(category, resolution="absent", name=None):
        def mutate(path, data):
            if re.fullmatch(r"/issue/SPK-1", path) and isinstance(data.get("fields"), dict):  # the story, not its subtasks
                st = data["fields"].get("status")
                if st:
                    st["statusCategory"]["key"] = category
                    if name:
                        st["name"] = name
                if resolution != "absent":  # a hostile or odd server may send it whatever was asked for
                    data["fields"]["resolution"] = resolution
            return data
        return Tamper(jira(state="In Progress" if category != "done" else "Done"), mutate)

    # category done, resolution null (the live team-managed shape): Done, whatever the status is called
    for kw in ({"resolution": None}, {"resolution": None, "name": "Finished"}, {"resolution": None, "name": "To Do"}):
        t = serve("done", **kw)
        assert status.evaluate(t.client, KEY, str(env))["stage"] == "done", kw
    # category not done, resolution set: not Done, even for a status that is called Done
    for category in ("new", "indeterminate", "undefined"):
        for kw in ({"resolution": {"name": "Done"}}, {"resolution": {"name": "Done"}, "name": "Done"}, {"resolution": "Fixed", "name": "Closed"}):
            t = serve(category, **kw)
            res = status.evaluate(t.client, KEY, str(env))
            assert res["stage"] == "done-state" and res["next"] == NEXT["done-state"], (category, kw)
    # an unknown category is a malformed status: not checked, never done
    res = status.evaluate(serve("fancy", resolution=None).client, KEY, str(env))
    assert res["stage"] == "board-not-checked" and res["board"] == "error"
    # the source never names the field, and no request asks for it
    src = Path(status.__file__).read_text(encoding="utf-8")
    assert not re.search(r"""["']resolution["']|\.resolution\b|resolution\s*=""", src)
    f = jira()
    status.evaluate(f.client, KEY, str(env))
    asked = [e["query"].get("fields", "") for e in f.log]
    assert all("resolution" not in x for x in asked) and any(x == "status" for x in asked)


def test_story_is_always_read_with_explicit_fields(env):
    world(env)
    f = jira()
    status.evaluate(f.client, KEY, str(env))
    story_reads = [e for e in f.log if e["path"] == "/issue/%s" % KEY]
    assert story_reads and all(e["query"].get("fields") for e in story_reads)  # U19: a read without `fields` can pass the fake and fail live
    assert any(e["query"]["fields"] == "status" for e in story_reads)  # the done check asks for the status and nothing else


def test_stage_decisions_closed(env):
    world(env, mode="strict", record=True, decisions_md=None)
    f = jira()
    cid = posted_decisions(f, decisions.parse_decisions(DECISION + "\n")["open"][0])
    (env / "dod" / ("Decisions_%s.md" % KEY)).write_text(posted_line(cid))
    git(env, "add", "-A")
    res = check(env, f, "decisions-closed")
    assert "1 decision" in res["why"]


def test_done_state_next_in_strict_points_at_open_decisions(env):
    # `skj done` refuses while a decision is open, so the next command is the one that closes them
    world(env, mode="strict", record=True)
    f = jira(state="In Progress")
    cid = posted_decisions(f, decisions.parse_decisions(DECISION + "\n")["open"][0])
    (env / "dod" / ("Decisions_%s.md" % KEY)).write_text(posted_line(cid))
    check(env, f, "done-state", nxt="skj decisions %s" % KEY)


def test_stage_done(env):
    world(env)
    res = check(env, jira())
    assert res["stage"] == "done" and res["next"] == "none"


# ---- strict mode: the stubs are told, not called ----
def test_strict_note_is_shown_and_no_strict_phase_runs(env, monkeypatch):
    world(env, mode="strict", record=True)
    for name in ("check", "verify"):
        monkeypatch.setattr("skj.strict." + name, lambda *a, **k: pytest.fail("status must not run a strict phase"))
    res = check(env, jira(state="In Progress"), "done-state")
    assert status.STRICT_NOTE in res["notes"]  # Fix-RU: the note says what `skj done` checks (test_fru_ac3_strict_note_states_what_done_checks)
    assert not any("business-rules" in n for n in res["notes"])  # RU3: the rules check is built
    text = status.render(res, KEY)
    assert "`skj done` runs the decisions cross-check (approvals)" in text
    # advisory: no such note
    world_again(env)
    assert not any("Strict mode" in n for n in check(env, jira(state="In Progress"), "done-state")["notes"])


# ---- offline, Jira errors, no writes, no Jira text ----
def test_offline_skips_board_stages_says_not_checked(env):
    world(env, questions="open")
    called = []
    res = status.evaluate(lambda: called.append(1), KEY, str(env), offline=True)
    valid_command(res["next"])
    assert res["stage"] == "questions-answered" and called == []
    assert res["board_checked"] is False and res["not_checked"][0] == "questions-posted" and res["board"] == "offline"
    assert res["notes"][0] == "Jira not reachable: showing local state only (offline: --offline was given)"


def test_offline_with_everything_local_met_is_never_done(env):
    world(env)
    called = []
    res = status.evaluate(lambda: called.append(1), KEY, str(env), offline=True)
    assert res["stage"] == "board-not-checked" and res["board_checked"] is False and called == []
    assert res["next"] == "skj status %s" % KEY
    assert set(res["not_checked"]) == {"questions-posted", "board-tasks", "board-sync", "decisions-posted", "pr-link", "done-state"}


def inject_http(f, code):
    f.inject(code, body={"errorMessages": [UNTRUSTED]})


@pytest.mark.parametrize("how", ["401", "404", "500", "403", "transport", "no-client"])
def test_jira_down_degrades_to_the_local_stage(env, how):
    world(env)
    f = jira()
    factory = f.client
    if how == "transport":
        f.fail_transport(OSError("down"), on=1)
    elif how == "no-client":
        def factory():
            raise ValueError(UNTRUSTED)
    else:
        inject_http(f, int(how))
    res = status.evaluate(factory, KEY, str(env))
    valid_command(res["next"])
    assert res["stage"] == "board-not-checked" and res["board_checked"] is False and res["next"] == "skj doctor"
    assert "board-tasks" in res["not_checked"]
    assert res["notes"][0].startswith("Jira not reachable: showing local state only (")
    assert res["board"] == ("no-client" if how == "no-client" else "error")
    if how.isdigit():
        assert "(HTTP %s" % how in res["notes"][0]  # a 401 adds ", authentication failed" (DF1 D-003)
    assert UNTRUSTED not in json.dumps(res)
    if how != "no-client":
        assert len(f.log) == 1  # one failure ends the reads; the other board stages are not tried


def test_jira_down_still_reports_an_earlier_local_stage(env):
    world(env, plan=False, questions="none")
    f = jira()
    f.fail_transport(OSError("down"), on=1)
    res = status.evaluate(f.client, KEY, str(env))
    assert res["stage"] == "plan" and res["next"] == "speckit.jira-lifecycle.plan %s" % KEY  # the local stage before any board read


def test_a_refusal_of_a_reader_leaves_only_that_stage_unchecked(env, monkeypatch):
    # questions.plan_post refuses with a fixed text (an own comment it cannot read, a truncated list ...): that stage is not checked,
    # the board stays usable for the stages after it, and the text is never shown
    world(env, questions="open", plan=False)

    def refuse(*a, **k):
        raise ValueError(UNTRUSTED)
    monkeypatch.setattr("skj.questions.plan_post", refuse)
    f = jira()
    res = status.evaluate(f.client, KEY, str(env))
    assert res["stage"] == "questions-answered" and res["board"] is None and res["not_checked"] == ["questions-posted"]
    assert UNTRUSTED not in json.dumps(res)
    monkeypatch.undo()
    world_again(env)
    assert status.evaluate(f.client, KEY, str(env))["stage"] == "done"  # without the refusal nothing is skipped


def test_no_factory_is_not_checked(env):
    world(env)
    res = status.evaluate(None, KEY, str(env))
    assert res["stage"] == "board-not-checked" and res["board"] == "no-client"


def test_a_factory_that_raises_is_called_once_and_only_when_a_board_stage_needs_it(env):
    world(env, plan=False, questions="none")  # stops at `plan`, before any board stage after questions-posted... which needs no read
    calls = []

    def factory():
        calls.append(1)
        raise RuntimeError(UNTRUSTED)
    res = status.evaluate(factory, KEY, str(env))
    assert res["stage"] == "plan" and len(calls) <= 1 and UNTRUSTED not in json.dumps(res)


def test_status_never_writes(env):
    world(env, questions="open", tasks="open")
    f = jira(children=(("T001", "To Do"),))
    for kw in ({}, {"offline": True}):
        status.evaluate(f.client, KEY, str(env), **kw)
    assert f.writes() == [] and {e["method"] for e in f.log} == {"GET"}


def tree_digest(root):
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob("*")):
        rel = p.relative_to(root).as_posix()
        h.update(rel.encode() + b"\0")
        if p.is_symlink():
            h.update(b"L" + os.readlink(p).encode())
        elif p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def test_status_is_read_only(env, capsys):
    """R6-AC3: the verb sends nothing but GETs (the fake's request log), has no write flag, and leaves every file of the repository (and the
    git directory) byte for byte as it was, whichever stage it reports."""
    ap = m.build_parser()
    (sub,) = [a for a in ap._actions if isinstance(a, argparse._SubParsersAction)]
    opts = {s for a in sub.choices["status"]._actions for s in a.option_strings}
    assert opts == {"-h", "--help", "--json", "--offline"}  # no --apply, --plan-out, --expect-plan, --force ...
    assert "status" not in " ".join(m.WRITE_VERBS) and "status" not in m.LOCAL_APPLY
    scenarios = [dict(), dict(questions="open", tasks="open"), dict(mode="strict", record=True), dict(ledger=False), dict(gate="stale", merged=False),
                 dict(decisions_md=DECISION + "\n"), dict(plan=False)]
    for kw in scenarios:
        world_again(env, **kw)
        before = tree_digest(env)
        for flags in ([], ["--json"], ["--offline"]):
            for f in (jira(state="In Progress", children=(("T001", "To Do"),), links=()), jira()):  # a story midway, and one with every read
                assert m.main(["status", KEY, *flags], client_factory=f.client) == 0
                capsys.readouterr()
                assert f.writes() == [] and {e["method"] for e in f.log} <= {"GET"}, (kw, flags, f.log)
                assert tree_digest(env) == before, (kw, flags)


def test_no_jira_text_in_output(env, capsys):
    # the story summary, a comment, a subtask title and a failing request's message all carry a marker; so do a hostile link title and a question
    world(env, questions="open")
    p = env / "dod" / ("DoD_%s.md" % KEY)
    p.write_text(p.read_text().replace("what?", "%s-question?" % UNTRUSTED))
    f = jira(summary="A story %s" % UNTRUSTED, children=(("T001", "To Do"),))
    f.issue(KEY)["links"][0]["object"]["title"] = UNTRUSTED
    f.add_comment(KEY, adf.md_to_adf("%s comment" % UNTRUSTED))
    posted_questions(f, "%s-question?" % UNTRUSTED)
    bad = jira()
    inject_http(bad, 500)
    for fake in (f, bad):
        for flags in ([], ["--json"], ["--offline"]):
            m.main(["status", KEY, *flags], client_factory=fake.client)
            cap = capsys.readouterr()
            assert UNTRUSTED not in cap.out + cap.err, flags
    # past the stages that read Jira text: the PR link and the done check
    world_again(env)
    f = jira(summary="A story %s" % UNTRUSTED)
    f.issue(KEY)["links"][0]["object"]["title"] = UNTRUSTED
    m.main(["status", KEY], client_factory=f.client)
    cap = capsys.readouterr()
    assert UNTRUSTED not in cap.out + cap.err and PR_URL in cap.out
    for secret in (f.email, f.token, f.account_id):
        assert secret not in cap.out + cap.err


# ---- the verb ----
def run(f, *flags, ref=KEY):
    return m.main(["status", ref, *flags], client_factory=f.client if f is not None else None)


def test_verb_prints_the_one_line_answer(env, capsys):
    world(env, plan=False)
    assert run(jira()) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Story %s: plan. Next: speckit.jira-lifecycle.plan %s" % (KEY, KEY)


def test_verb_json(env, capsys):
    world(env, plan=False)
    assert run(jira(), "--json") == 0
    res = json.loads(capsys.readouterr().out)
    assert res["stage"] == "plan" and res["next"] == "speckit.jira-lifecycle.plan %s" % KEY
    assert res["board_checked"] is False and res["notes"] == [status.NOT_READ_NOTE]  # DF3 (D-005): no request was needed up to `plan`
    assert set(res) == {"stage", "next", "why", "board_checked", "not_checked", "board", "notes"}


def test_verb_offline_builds_no_client_and_prints_what_was_not_checked(env, capsys):
    world(env, plan=False, questions="open")

    def factory():
        pytest.fail("a client was built")
    assert m.main(["status", KEY, "--offline"], client_factory=factory) == 0
    out = capsys.readouterr().out
    assert out.startswith("Story %s: questions-answered. Next: speckit.jira-lifecycle.plan %s\n" % (KEY, KEY))
    assert "Not checked: questions-posted" in out and "Jira not reachable: showing local state only" in out


def test_verb_without_a_config_says_setup(env, capsys):
    assert run(None, ref=KEY) == 0
    assert capsys.readouterr().out.splitlines()[0] == "Story %s: config. Next: skj setup" % KEY
    assert run(None, ref="1") == 2  # a number needs the project of a config
    assert "give the full issue key" in capsys.readouterr().err


def test_verb_with_an_invalid_config_says_doctor(env, capsys):
    (env / "skj.config.json").write_text("{not json")
    assert run(None) == 0
    assert capsys.readouterr().out.splitlines()[0] == "Story %s: config. Next: skj doctor" % KEY


def test_verb_all_met_text(env, capsys):
    world(env)
    assert run(jira()) == 0
    assert capsys.readouterr().out.splitlines()[0] == "Story %s: done. Next: none" % KEY


def test_verb_accepts_the_number_of_the_project(env, capsys):
    world(env)
    assert run(jira(), ref="1") == 0
    assert capsys.readouterr().out.splitlines()[0] == "Story %s: done. Next: none" % KEY


def test_verb_board_not_checked_text(env, capsys):
    world(env)
    f = jira()
    f.fail_transport(OSError("x"), on=1)
    assert run(f) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Story %s: board not checked. Next: skj doctor" % KEY
    assert "Jira not reachable: showing local state only" in out


@pytest.mark.parametrize("bad", ["0", "-3", "abc", "spk-1", "SPK-0", "SPK-01", "OTHER-1", "SPK-1;touch x", " SPK-1", "2147483648123", "1.5"])
def test_verb_rejects_a_bad_ref(env, capsys, bad):
    world(env)
    assert run(jira(), ref=bad) == 2
    assert "skj: not an issue number or a key of project SPK" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["status"], ["status", KEY, "--apply"], ["status", KEY, "--plan-out", "x"], ["status", KEY, "--off"],
                                  ["status", KEY, "--js"], ["status", KEY, "--catch-up"], ["status", KEY, "extra"]])
def test_usage_errors_exit_2_and_no_abbreviation(env, capsys, argv):
    world(env)
    with pytest.raises(SystemExit) as e:
        m.main(argv, client_factory=jira().client)
    assert e.value.code == 2
    assert not (env / "x").exists()


def test_every_parser_refuses_abbreviations_including_status():
    ap = m.build_parser()
    (sub,) = [a for a in ap._actions if isinstance(a, argparse._SubParsersAction)]
    assert sub.choices["status"].allow_abbrev is False


def test_board_checked_is_false_offline_even_for_a_local_stage(env):
    world(env, ledger=False)
    res = check(env, None, "ledger", offline=True)
    assert res["board"] == "offline" and res["board_checked"] is False
    res = check(env, jira(), "ledger")
    assert res["board"] is None and res["board_checked"] is False  # DF1 D-005: the walk stopped before any board read


def test_unclosed_fence_next_is_a_refresh_only_when_it_can_repair(env):
    world(env)
    p = env / "dod" / ("DoD_%s.md" % KEY)
    good = p.read_text()
    p.write_text(good.replace("it works\n", "```\nit works\n", 1))  # opened in section 1: `skj dod refresh` splices from section 2
    check(env, jira(), "ledger", "section 1", nxt="skj dod refresh %s" % KEY)
    p.write_text(good + "\n```\n")  # opened after section 2: a refresh refuses, so it is by hand
    check(env, jira(), "ledger", "by hand", nxt=status.BY_HAND)


def test_unparsed_questions_and_unreadable_files_are_stages_not_crashes(env):
    world(env, questions="open")
    p = env / "dod" / ("DoD_%s.md" % KEY)
    p.write_text(p.read_text().replace("- [ ] Q1: what?", "- [ ] Q1: " + "x" * 2100))  # a question line over the cap cannot be read
    res = check(env, jira(), "questions-posted", "cannot be read", nxt=status.BY_HAND)
    assert "xxxx" not in json.dumps(res)
    world_again(env, decisions_md=DECISION + "\n")
    (env / "dod" / ("Decisions_%s.md" % KEY)).write_text("```\n" + DECISION + "\n")  # an open fence hides the rest
    check(env, jira(), "decisions-posted", "cannot be read in full")
    world_again(env, tasks="open")
    (env / "specs/001-x/tasks.md").write_bytes(b"\xff\xfe- [ ] T001\n")  # not UTF-8
    check(env, jira(), "tasks")


# ---- linear on adversarial files (CPU-time SIGPROF bound) ----
needs_timer = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="signal.setitimer is not available on this platform")
BOUND = 4.0  # seconds for a whole evaluate (a few git runs and the fake included); the linear code takes well under 1 s
MIB, W = 1 << 20, 1990


class _TooSlow(Exception):
    pass


def _timed(fn, *args):
    def alarm(signum, frame):
        raise _TooSlow()
    # CPU time, not the wall clock: load slows the clock, not the work; a wall-clock backstop at 10x still cuts off a call blocked on a FIFO or a child
    old, old_real = signal.signal(signal.SIGPROF, alarm), signal.signal(signal.SIGALRM, alarm)
    signal.setitimer(signal.ITIMER_PROF, BOUND)
    signal.setitimer(signal.ITIMER_REAL, 10 * BOUND)
    t0 = time.process_time()
    try:
        return fn(*args), time.process_time() - t0
    except _TooSlow:
        return None, BOUND
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGPROF, old)
        signal.signal(signal.SIGALRM, old_real)


SHAPES = {"1MiB-one-line": lambda: "x" * MIB, "1MiB-blanks": lambda: " " * MIB, "500x1990": lambda: ("a " * (W // 2) + "\n") * 500,
          "20000-lines": lambda: "  - [ ] x\n" * 20000, "500x1990-fill": lambda: ("_(" + " " * W + "\n") * 500,
          "20000-d-lines": lambda: "- [ ] D1: a → _(open)_\n" * 20000, "20000-t-lines": lambda: "- [ ] T001 a\n" * 20000}


@needs_timer
@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("where", ["ledger-section2", "ledger-section4", "ledger-tail", "tasks", "decisions"])
def test_evaluate_is_linear_on_adversarial_files(env, shape, where):
    world(env, mode="strict", record=True)
    junk = SHAPES[shape]()
    if where == "tasks":
        (env / "specs/001-x/tasks.md").write_text(junk[:MIB])
    elif where == "decisions":
        (env / "dod" / ("Decisions_%s.md" % KEY)).write_text(junk[:MIB])
    else:
        p = env / "dod" / ("DoD_%s.md" % KEY)
        text = p.read_text()
        marker = {"ledger-section2": "## 3. AC", "ledger-section4": "## 5. Definition", "ledger-tail": None}[where]
        p.write_text(text + junk[:MIB // 2] if marker is None else text.replace(marker, junk[:MIB // 2] + "\n" + marker, 1))
    f = jira()
    res, took = _timed(lambda: status.evaluate(f.client, KEY, str(env)))
    assert res is not None and took < BOUND, (shape, where, took)
    valid_command(res["next"])
    assert f.writes() == []


# ---- the command table against the argparse tree ----
def test_every_skj_command_status_can_show_names_a_verb_that_exists():
    seen = {w for cmd in [*NEXT.values(), LINK_NEXT] for w in shlex.split(cmd)[:1] if w == "skj"}
    assert seen == {"skj"}
    verbs = {"dod", "questions", "push", "decisions", "strict", "gate", "pr", "done", "stack", "doctor", "setup", "status"}
    ap = m.build_parser()
    (sub,) = [a for a in ap._actions if isinstance(a, argparse._SubParsersAction)]
    assert verbs <= set(sub.choices)


def test_a_refused_login_is_named_in_the_note(env):
    """DF1 (D-003): Jira Cloud answers a failed login with 404 + X-Seraph-LoginReason (live run A2-28; hand-written shape)."""
    world(env)
    f = jira()
    f.inject(404, headers={"X-Seraph-LoginReason": "AUTHENTICATED_FAILED"},
             body={"errorMessages": ["Issue does not exist or you do not have permission to see it."], "errors": {}})
    res = status.evaluate(f.client, KEY, str(env))
    assert res["board"] == "error" and "(HTTP 404, authentication failed" in res["notes"][0] and res["next"] == "skj doctor"


def test_board_checked_is_false_when_the_walk_stopped_before_any_board_read(env):
    """DF1 (live run A2-26, D-005): with no network the walk stopped at the local `ledger` stage and still said board_checked: true."""
    world(env, ledger=False)
    called = []

    def unreachable():
        called.append(1)
        raise AssertionError("no board read is needed before the ledger stage")
    res = check(env, None, "ledger")
    assert res["board_checked"] is False and res["board"] is None and res["not_checked"] == []
    res = status.evaluate(unreachable, KEY, str(env))
    assert res["stage"] == "ledger" and res["board_checked"] is False and called == []


def test_df3_board_checked_needs_a_request_that_was_sent(env, capsys):
    """R1 retest (D-005 variant, SPK-14): a ledger with no open question stops at `plan`; questions-posted needs no request, so none
    is sent and board_checked must not say the board was checked. The reason is a note."""
    world(env, plan=False)
    f = jira()
    assert run(f, "--json") == 0
    res = json.loads(capsys.readouterr().out)
    assert res["stage"] == "plan" and f.log == []
    assert res["board_checked"] is False and res["board"] is None and res["notes"] == [status.NOT_READ_NOTE]
