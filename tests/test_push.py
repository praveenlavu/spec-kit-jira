"""W3: `skj push` mirrors tasks.md into subtasks of a story (preview, gate, apply), against the fake Jira only.

Ported from the fork's tests/test_push.py where the behaviour carries over (create / skip / orphan / duplicate, the compact plan,
the 255 cut, partial failure then re-run). Dropped with the Jira model: title updates, the `removed` state and the generic state
PATCH (K9/K10: no `removed`; the brief has no title edit; states move only by transitions).
"""
import json
import signal
import subprocess
import time
from pathlib import Path

import pytest

import skj.__main__ as m
from skj import config, issues, push, tasks, transitions
from skj.client import Client, WriteUncertain
from fakejira import FakeJira

KEY = "SPK-1"
DONE_ID, DONE_NAME = "41", "Done"


def cfg(**kw):
    return config.Config("example-site.atlassian.net", "SPK", **kw)


def T(tid, title, done=False):
    return {"id": tid, "title": title, "done": done}


@pytest.fixture
def fake():
    f = FakeJira()
    assert f.add_issue("A fictional story") == KEY
    return f


def sub(fake, summary, status="To Do", labels=("skj",)):
    return fake.add_issue(summary, type="Subtask", parent=KEY, labels=labels, status=status)


class Recorder:
    """An opener around the fake that keeps (method, path, query, body) of every request, and a clock reading per write."""

    def __init__(self, fake, clock=None, fail=None):
        self.fake, self.calls, self.clock, self.stamps, self.fail = fake, [], clock, [], fail

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("/rest/api/3", 1)[1].split("?", 1)[0]
        self.calls.append((method, path, url, json.loads(body) if body else None))
        if method != "GET":
            self.stamps.append(self.clock.t if self.clock else None)
            if self.fail and self.fail(method, path, sum(1 for c in self.calls if c[0] == method and c[1] == path)):
                return 400, {"Content-Type": "application/json"}, b'{"errorMessages":["\\u202eevil server text"],"errors":{}}'
        return self.fake(method, url, headers, body, timeout)

    def client(self):
        return Client(self.fake.site, self.fake.email, self.fake.token, opener=self, sleep=lambda s: None)


def writes(rec):
    return [c for c in rec.calls if c[0] != "GET"]


def planned(fake, task_list, c=None, **kw):
    return push.plan(c or fake.client(), cfg(**kw), KEY, task_list)


def by_task(p):
    return {(o["action"], o["task"]): o for o in p["ops"]}


# ---- the required tests ---------------------------------------------------------------------------------------------------------------

def test_creates_subtask_with_parent_key(fake):
    rec = Recorder(fake)
    c = rec.client()
    p = push.plan(c, cfg(), KEY, [T("T001", "Create the model"), T("T002", "Wire it")])
    assert [o["action"] for o in p["ops"]] == ["create", "create"] and p["type"] == {"id": "10002", "name": "Subtask"}
    assert writes(rec) == []
    res = push.apply(c, p, clock=lambda: 0.0, sleep=lambda s: None)
    assert res["stopped"] is None
    (a, b) = writes(rec)
    assert a[:2] == ("POST", "/issue")
    assert a[3] == {"fields": {"project": {"key": "SPK"}, "parent": {"key": KEY}, "issuetype": {"id": "10002"},
                               "summary": "T001 Create the model", "labels": ["skj"]}}
    assert b[3]["fields"]["summary"] == "T002 Wire it"
    kids = issues.get(fake.client(), KEY, ["subtasks"])["fields"]["subtasks"]
    assert [k["fields"]["summary"] for k in kids] == ["T001 Create the model", "T002 Wire it"]
    assert all(fake.issue(k["key"])["labels"] == ["skj"] and fake.issue(k["key"])["parent"] == fake.issue(KEY)["id"] for k in kids)
    assert res["lines"] == ["created T001 SPK-2", "created T002 SPK-3"]


def test_preview_shows_transition_id_and_name(fake):
    sub(fake, "T001 Ticked and open")
    sub(fake, "T002 Ticked and done", status="Done")
    sub(fake, "T003 Open")
    p = planned(fake, [T("T001", "Ticked and open", True), T("T002", "Ticked and done", True), T("T003", "Open"),
                       T("T004", "New and ticked", True)])
    ops = by_task(p)
    assert ops[("transition", "T001")]["transition"] == {"id": DONE_ID, "name": DONE_NAME, "to_id": "10002", "to_name": "Done"}
    assert ("no-op", "T002") in ops and ("skip", "T003") in ops and ("create", "T004") in ops
    text = push.render(p)
    assert "| transition | T001 | SPK-2 | T001 Ticked and open | 41 Done |" in text
    assert "| no-op | T002 | SPK-3 | T002 Ticked and done | already done |" in text
    assert "| create | T004 | - | T004 New and ticked | done: on the next push, after the create |" in text
    assert "skip: T003 (1)" in text
    # the transitions read asks for the fields, so a required field is seen
    tr = [e for e in fake.log if e["path"].endswith("/transitions")]
    assert tr and all(e["query"].get("expand") == "transitions.fields" for e in tr)


def test_second_apply_creates_nothing(repo, fake, capsys):
    (repo / "tasks.md").write_text("- [ ] T001 One\n- [x] T002 Two\n")
    assert run(fake, "push", KEY, "--from", "tasks.md", "--apply") == 0
    first = [(e["method"], e["path"]) for e in fake.writes()]
    assert first == [("POST", "/issue"), ("POST", "/issue")]
    # second run: the ticked new one gets its transition, nothing is created
    assert run(fake, "push", KEY, "--from", "tasks.md", "--apply") == 0
    assert [(e["method"], e["path"]) for e in fake.writes()][2:] == [("POST", "/issue/SPK-3/transitions")]
    capsys.readouterr()
    assert run(fake, "push", KEY, "--from", "tasks.md", "--apply") == 0
    assert len(fake.writes()) == 3 and "create 0" in capsys.readouterr().out
    # J7: never a search: the children come from GET /issue/{key}?fields=subtasks, each one from its own GET
    assert not [e for e in fake.log if e["path"].startswith("/search")]
    assert any(e["path"] == "/issue/" + KEY and e["query"].get("fields") == "subtasks" for e in fake.log)


def test_timeout_then_rerun_finds_marker(repo, fake, capsys):
    """W3-AC3: the create times out but happened; the same run re-reads and finds it (and stops), the next run creates nothing."""
    (repo / "tasks.md").write_text("- [ ] T001 One\n- [ ] T002 Two\n")
    assert run(fake, "push", KEY, "--from", "tasks.md") == 0
    reads = len(fake.log)
    capsys.readouterr()
    fake.fail_transport(on=len(fake.log) + reads + 1, applied=True)  # the first create of the next run
    assert run(fake, "push", KEY, "--from", "tasks.md", "--apply") == 1
    out = capsys.readouterr().out
    assert len(fake.writes()) == 1 and len(fake.issues) == 2  # stopped: T002 not attempted
    assert "write uncertain: T001: found SPK-2 on re-read (created)" in out
    assert "not done: create T002" in out
    assert run(fake, "push", KEY, "--from", "tasks.md", "--apply") == 0
    assert [(e["method"], e["path"]) for e in fake.writes()] == [("POST", "/issue")] * 2
    assert sorted(i["summary"] for i in fake.issues.values()) == ["A fictional story", "T001 One", "T002 Two"]


def test_timeout_not_applied_is_not_created_again_in_that_run(repo, fake, capsys):
    (repo / "tasks.md").write_text("- [ ] T001 One\n")
    assert run(fake, "push", KEY, "--from", "tasks.md") == 0
    reads = len(fake.log)
    capsys.readouterr()
    fake.fail_transport(on=len(fake.log) + reads + 1, applied=False)
    assert run(fake, "push", KEY, "--from", "tasks.md", "--apply") == 1
    out = capsys.readouterr().out
    assert "write uncertain: T001: not found on re-read; not created again in this run" in out
    assert len(fake.writes()) == 1 and len(fake.issues) == 1
    assert run(fake, "push", KEY, "--from", "tasks.md", "--apply") == 0  # the next run reconciles: one create
    assert len(fake.issues) == 2


def test_pacing_with_fake_clock(fake):
    """W3-AC4: at most 20 writes in any 2 s window (UNVERIFIED for token traffic, U4); the clock and sleep are injected."""
    class Clock:
        t = 100.0
    clock, slept = Clock(), []

    def sleep(s):
        slept.append(s)
        clock.t += s

    rec = Recorder(fake, clock=clock)
    c = rec.client()
    p = push.plan(c, cfg(), KEY, [T("T%03d" % i, "task %d" % i) for i in range(1, 46)])
    res = push.apply(c, p, clock=lambda: clock.t, sleep=sleep)
    assert res["stopped"] is None and len(rec.stamps) == 45
    assert all(sum(1 for s in rec.stamps if t <= s < t + push.PACE_WINDOW) <= push.PACE_WRITES for t in rec.stamps)
    assert slept == [2.0, 2.0] and rec.stamps[19] == 100.0 and rec.stamps[20] == 102.0 and rec.stamps[40] == 104.0
    assert push.PACE_WRITES == 20 and push.PACE_WINDOW == 2.0


def test_no_status_put(repo, fake):
    """W3-AC5: done is reached only by POST /transitions; no PUT is ever sent (and no write body names `status`)."""
    sub(fake, "T001 One")
    sub(fake, "T002 Two", status="In Progress")
    (repo / "tasks.md").write_text("- [x] T001 One\n- [x] T002 Two\n- [x] T003 Three\n- [ ] T004 Four\n")
    rec = Recorder(fake)
    for _ in range(2):
        assert m.main(["push", KEY, "--from", "tasks.md", "--apply"], client_factory=rec.client) == 0
    assert [c[0] for c in rec.calls if c[0] not in ("GET", "POST")] == []
    assert all("status" not in json.dumps(c[3]) for c in writes(rec))
    assert [c[1] for c in writes(rec) if c[1].endswith("/transitions")] == ["/issue/SPK-2/transitions", "/issue/SPK-3/transitions",
                                                                           "/issue/SPK-4/transitions"]
    assert all(c[3] == {"transition": {"id": DONE_ID}} for c in writes(rec) if c[1].endswith("/transitions"))
    assert {fake.issue(k)["status"] for k in ("SPK-2", "SPK-3", "SPK-4")} == {"Done"} and fake.issue("SPK-5")["status"] == "To Do"


def test_orphan_reported_not_touched(repo, fake, capsys):
    """K10: a labelled T### subtask that tasks.md no longer lists is shown and never deleted or transitioned."""
    sub(fake, "T001 Kept")
    sub(fake, "T009 Gone", status="In Progress")
    (repo / "tasks.md").write_text("- [x] T001 Kept\n")
    assert run(fake, "push", KEY, "--from", "tasks.md", "--apply") == 0
    out = capsys.readouterr().out
    assert "| orphan | T009 | SPK-3 | - | never deleted or transitioned (K10) |" in out
    assert [(e["method"], e["path"]) for e in fake.writes()] == [("POST", "/issue/SPK-2/transitions")]
    assert fake.issue("SPK-3")["status"] == "In Progress" and "SPK-3" in [i["key"] for i in fake.issues.values()]


# ---- matching (J7: the T### prefix plus the label) -------------------------------------------------------------------------------------

def test_matching_needs_prefix_and_label_and_ignores_the_rest(fake):
    sub(fake, "T001 hand-made, no label", labels=())
    sub(fake, "Made by a human", labels=("skj",))
    sub(fake, "T0012 longer id", labels=("skj",))
    sub(fake, "T002x not an id", labels=("skj",))
    p = planned(fake, [T("T001", "A"), T("T002", "B")])
    assert [(o["action"], o["task"]) for o in p["ops"]] == [("create", "T001"), ("create", "T002"), ("orphan", "T0012")]
    text = push.render(p)
    assert text.count("unlabelled ") == 1 and "unlabelled T001-like subtask ignored: SPK-2\n" in text and "hand-made" not in text


@pytest.mark.parametrize("title", ["Fix T001 later", "x T001", " T001 leading blank"])
def test_a_task_id_inside_the_summary_is_not_adopted(fake, title):
    sub(fake, title)
    p = planned(fake, [T("T001", "A", True)])
    assert [(o["action"], o["task"]) for o in p["ops"]] == [("create", "T001")] and not p["unlabelled"]
    assert "SPK-2" not in push.render(p)


def test_duplicate_children_reported_never_applied(fake):
    sub(fake, "T001 A")
    sub(fake, "T001 A copy")
    p = planned(fake, [T("T001", "A", True)])
    assert [(o["action"], o["subtask"]) for o in p["ops"]] == [("refused", "SPK-2"), ("duplicate", "SPK-3")]
    assert p["ops"][0]["code"] == "duplicate" and not [e for e in fake.log if e["path"].endswith("/transitions")]
    text = push.render(p)
    assert "duplicate 1" in text and "| refused | T001 | SPK-2 | T001 A | refused: duplicate |" in text
    assert "refused duplicate: Two subtasks carry this task id." in text
    res = push.apply(fake.client(), p, clock=lambda: 0.0, sleep=lambda s: None)
    assert fake.writes() == [] and res["stopped"] is None
    assert [o["action"] for o in planned(fake, [T("T001", "A")])["ops"]] == ["skip", "duplicate"]  # unticked: nothing refused


def test_unticked_never_transitioned_backwards(fake):
    sub(fake, "T001 done but unticked", status="Done")
    sub(fake, "T002 in progress, unticked", status="In Progress")
    p = planned(fake, [T("T001", "done but unticked"), T("T002", "in progress, unticked")])
    assert [o["action"] for o in p["ops"]] == ["skip", "skip"]
    assert not [e for e in fake.log if e["path"].endswith("/transitions")]
    push.apply(fake.client(), p, clock=lambda: 0.0, sleep=lambda s: None)
    assert fake.writes() == []


def test_children_from_a_moved_story_are_refused(fake):
    fake.move_issue(KEY, "SPK-77")
    with pytest.raises(issues.KeyMismatch):
        planned(fake, [T("T001", "A")])
    assert fake.writes() == []


def test_a_child_whose_parent_differs_is_refused(fake):
    k = sub(fake, "T001 A")
    other = fake.add_issue("another story")
    fake.issue(k)["parent"] = fake.issue(other)["id"]
    fake._fields_orig = fake._fields

    def fields(issue, names):  # the story still lists it: an inconsistent answer
        out = fake._fields_orig(issue, names)
        if issue["key"] == KEY and "subtasks" in out:
            out["subtasks"] = [fake._ref(fake.issue(k))]
        return out
    fake._fields = fields
    with pytest.raises(issues.ReadError, match="^a subtask of SPK-1 in Jira's answer has another parent; refused$"):
        planned(fake, [T("T001", "A")])


# ---- transitions: never guessed (K3) -------------------------------------------------------------------------------------------------

def _extra_done(fake):
    fake.transitions.append({"id": "51", "name": "Closed", "to": "Done", "hasScreen": False, "isConditional": False, "fields": {}})


def test_two_done_transitions_are_refused_not_guessed(fake):
    sub(fake, "T001 A")
    _extra_done(fake)
    p = planned(fake, [T("T001", "A", True)])
    (op,) = p["ops"]
    assert op["action"] == "refused" and op["code"] == "ambiguous" and "transition" not in op
    text = push.render(p)
    assert "| refused | T001 | SPK-2 | T001 A | refused: ambiguous |" in text
    assert transitions.MESSAGES["ambiguous"] in text
    push.apply(fake.client(), p, clock=lambda: 0.0, sleep=lambda s: None)
    assert fake.writes() == []
    # the configured name picks one
    p = planned(fake, [T("T001", "A", True)], transitions={"done": "Closed"})
    assert p["ops"][0]["transition"]["id"] == "51"


def test_a_required_field_refuses_the_transition(fake):
    """The transitions GET carries expand=transitions.fields: without it the fake (like Jira) omits `fields` and the check is blind."""
    sub(fake, "T001 A")
    fake.transitions[3]["fields"] = {"resolution": {"required": True, "name": "Resolution"}}
    p = planned(fake, [T("T001", "A", True)])
    assert p["ops"][0]["action"] == "refused" and p["ops"][0]["code"] == "required-fields"


@pytest.mark.parametrize("flag,code", [("hasScreen", "screen"), ("isConditional", "conditional")])
def test_a_screen_or_condition_refuses_the_transition(fake, flag, code):
    sub(fake, "T001 A")
    fake.transitions[3][flag] = True
    assert planned(fake, [T("T001", "A", True)])["ops"][0]["code"] == code


# ---- failures mid-apply -------------------------------------------------------------------------------------------------------------

def test_partial_failure_then_rerun_creates_only_missing(fake):
    rec = Recorder(fake, fail=lambda method, path, n: (method, path, n) == ("POST", "/issue", 2))
    c = rec.client()
    tl = [T("T001", "A"), T("T002", "B"), T("T003", "C")]
    res = push.apply(c, push.plan(c, cfg(), KEY, tl), clock=lambda: 0.0, sleep=lambda s: None)
    assert res["lines"] == ["created T001 SPK-2", "failed: create T002: Jira answered HTTP 400; stopped", "not done: create T003"]
    assert res["stopped"] and "evil" not in "\n".join(res["lines"])
    p2 = planned(fake, tl)
    assert [(o["action"], o["task"]) for o in p2["ops"]] == [("skip", "T001"), ("create", "T002"), ("create", "T003")]


def test_create_answer_malformed_is_uncertain(fake):
    rec = Recorder(fake)
    c = rec.client()
    p = push.plan(c, cfg(), KEY, [T("T001", "A"), T("T002", "B")])
    fake.inject(201, body={"id": "x", "key": "nope"})
    res = push.apply(c, p, clock=lambda: 0.0, sleep=lambda s: None)
    assert res["stopped"] and res["lines"][0] == "write uncertain: T001: not found on re-read; not created again in this run"
    assert len(writes(rec)) == 1


def test_create_subtask_and_do_transition_validate_their_input(fake):
    c = fake.client()
    for bad in [dict(parent="spk-1"), dict(type_id="x"), dict(summary="a\nb"), dict(summary=""), dict(summary="x" * 256),
                dict(project="spk")]:
        kw = dict(project="SPK", parent=KEY, type_id="10002", summary="T001 a")
        kw.update(bad)
        with pytest.raises(ValueError):
            issues.create_subtask(c, kw["project"], kw["parent"], kw["type_id"], kw["summary"])
    for bad in ("0", "1;x", "", None):
        with pytest.raises(ValueError):
            issues.do_transition(c, KEY, bad)
    with pytest.raises(ValueError):
        issues.transitions(c, "SPK-1/x")
    assert fake.log == []


def test_subtask_type_must_be_unique_and_a_subtask(fake):
    with pytest.raises(issues.ReadError, match="no single subtask issue type named by subtask_type"):
        issues.subtask_type(fake.client(), "SPK", "Story")  # exists, but not a subtask type
    with pytest.raises(issues.ReadError):
        issues.subtask_type(fake.client(), "SPK", "Nope")
    assert issues.subtask_type(fake.client(), "SPK", "Subtask") == {"id": "10002", "name": "Subtask"}
    for types in ([{"id": "10005", "name": "Odd", "subtask": False}], [{"id": "10005", "name": "Odd", "subtask": True}] * 2,
                  [{"id": "10005", "name": "Odd", "subtask": True, "hierarchyLevel": 0}], [{"id": "x", "name": "Odd", "subtask": True}]):
        fake.inject(200, body={"key": "SPK", "issueTypes": types})
        with pytest.raises(issues.ReadError):
            issues.subtask_type(fake.client(), "SPK", "Odd")
    fake.inject(200, body={"key": "OTHER", "issueTypes": [{"id": "10005", "name": "Odd", "subtask": True}]})
    with pytest.raises(issues.ReadError, match="malformed project answer"):
        issues.subtask_type(fake.client(), "SPK", "Odd")
    fake.inject(200, body={"key": "SPK", "issueTypes": [{"id": "10005", "name": "Odd", "subtask": True}]})
    assert issues.subtask_type(fake.client(), "SPK", "Odd") == {"id": "10005", "name": "Odd"}
    with pytest.raises(issues.ReadError):
        planned(fake, [T("T001", "A")], subtask_type="Story")
    assert fake.writes() == []


def test_no_create_means_no_type_lookup(fake):
    sub(fake, "T001 A")
    planned(fake, [T("T001", "A")])
    assert not [e for e in fake.log if e["path"].startswith("/project")]


# ---- summaries (agent/PR-controlled text) ------------------------------------------------------------------------------------------------

def test_summary_is_sanitised():
    s, cut = push.summary(T("T001", "a\u202eb\u200bc\x07d\u2028e   f\u00a0g\x1b[31m"))
    assert (s, cut) == ("T001 abcde f g[31m", False)
    assert push.summary(T("T001", ""))[0] == "T001"


def _u16(s):
    return len(s.encode("utf-16-le")) // 2


@pytest.mark.parametrize("title,cut", [
    ("\U0001F600" * 300, True), ("\U0001F600" * 125, False), ("\U0001F600" * 125 + "x", True), ("x" * 250, False),
    ("x" * 251, True), ("e\u0301" * 200, True), ("\U0001F468\u200d\U0001F469\u200d\U0001F467" * 80, True),
    ("x" * 248 + "\U0001F600" + "y", True)],
    ids=["emoji-300", "emoji-at-limit", "emoji-one-over", "ascii-at-limit", "ascii-one-over", "combining", "zwj", "emoji-at-the-cut"])
def test_summary_cap_counts_utf16_units_and_cuts_on_a_boundary(title, cut):
    s, was_cut = push.summary(T("T001", title))
    assert was_cut is cut and len(s) <= 255 and _u16(s) <= 255 and s.startswith("T001")
    assert s == push.summary(T("T001", title))[0]  # deterministic
    if cut:
        assert s.endswith("\u2026") and "\u200d" not in s
        body = s[:-1]
        if "\u0301" in title:  # never a base without all of its marks
            assert body.endswith("\u0301") or body.endswith("\u0302") or not body.endswith("e")
    else:
        assert s == "T001 " + title


def test_a_cut_inside_a_combining_sequence_drops_the_whole_cluster():
    s, cut = push.summary(T("T001", "a" * 247 + "e\u0301\u0302\u0303"))
    assert cut and s == "T001 " + "a" * 247 + "\u2026"
    s, cut = push.summary(T("T001", "e\u0301" * 200))
    assert cut and s == "T001 " + "e\u0301" * 124 + "\u2026"
    s, cut = push.summary(T("T001", "x" * 248 + "\U0001F600y"))
    assert cut and s == "T001 " + "x" * 248 + "\u2026" and _u16(s) == 254


def test_create_subtask_refuses_over_255_utf16_units(fake):
    with pytest.raises(ValueError):
        issues.create_subtask(fake.client(), "SPK", KEY, "10002", "\U0001F600" * 128)
    assert fake.log == []


def test_long_summary_cut_to_255_and_shown(fake):
    p = planned(fake, [T("T001", "x" * 400)])
    op = p["ops"][0]
    assert len(op["summary"]) == 255 and op["summary"].endswith("x…") and op["cut"] is True
    assert "(cut to 255)" in push.render(p)
    s = "y" * (255 - len("T001 "))
    assert push.summary(T("T001", s)) == ("T001 " + s, False)


def test_render_escapes_cells_and_is_deterministic(fake):
    p = planned(fake, [T("T001", "a|b\\c")])
    text = push.render(p)
    assert "| create | T001 | - | T001 a\\|b\\\\c | - |" in text
    assert text == push.render(planned(fake, [T("T001", "a|b\\c")]))


def test_render_warns_when_every_tracked_subtask_is_an_orphan(fake):
    sub(fake, "T001 A")
    sub(fake, "T002 B")
    text = push.render(planned(fake, []))
    assert "⚠ tasks.md lists none of the 2 subtask(s) already on the story" in text
    assert "⚠" not in push.render(planned(fake, [T("T001", "A")]))


def test_render_200_tasks_writes_first_fit_the_gate():
    ops = [{"action": "skip", "task": "T%03d" % i, "subtask": "SPK-%d" % (i + 2), "summary": "T%03d t" % i, "cut": False}
           for i in range(1, 201)]
    ops[150] = dict(ops[150], action="create", subtask=None)
    text = push.render({"key": KEY, "type": {"id": "10002", "name": "Subtask"}, "ops": ops})
    lines = text.splitlines()
    assert any("| create | T151 |" in line for line in lines[:15]) and len(lines) < 40 and "(199)" in text


# ---- limits ---------------------------------------------------------------------------------------------------------------------------

def test_duplicate_task_ids_refused_before_any_call(fake):
    with pytest.raises(ValueError, match="^tasks.md lists a task id twice: T001; nothing read or written$"):
        planned(fake, [T("T001", "A"), T("T002", "B"), T("T001", "C")])
    assert fake.log == []


def test_too_many_tasks_refused_before_any_call(fake):
    with pytest.raises(ValueError, match="^tasks.md lists more than 200 tasks; nothing read or written$"):
        planned(fake, [T("T%03d" % i, "x") for i in range(1, 202)])
    assert fake.log == []
    planned(fake, [T("T%03d" % i, "x") for i in range(1, 201)])


def test_overlong_task_id_refused(fake):
    for tid in ("T0000000001", "T\uff10\uff10\uff11", "T\u0660\u0660\u0661"):
        with pytest.raises(ValueError, match="^tasks.md has a task id that is not ASCII T followed by 3 to 9 digits; nothing read or written$"):
            planned(fake, [T(tid, "x")])
    assert fake.log == []


def test_non_ascii_digit_ids_from_tasks_md_are_refused(fake):
    for line in ("- [ ] T\uff10\uff10\uff11 fullwidth\n", "- [ ] T\u0660\u0660\u0661 arabic-indic\n"):
        (t,) = tasks.parse_tasks(line)  # tasks.py accepts them (\d); push refuses them
        with pytest.raises(ValueError, match="not ASCII T followed by 3 to 9 digits"):
            push.check_tasks([t])


def test_too_many_children_refused(fake, monkeypatch):
    monkeypatch.setattr(push, "MAX_CHILDREN", 2)
    for i in range(3):
        sub(fake, "T%03d x" % (i + 1))
    with pytest.raises(issues.ReadError, match="more than 2 subtasks"):
        planned(fake, [])


# ---- the CLI --------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    for k in ("SKJ_EMAIL", "SKJ_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, stdin=subprocess.DEVNULL)
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK"}))
    (tmp_path / "dod").mkdir()
    (tmp_path / "dod" / "DoD_SPK-1.md").write_text("# DoD SPK-1\n\n- **Spec dir:** specs/001-x\n\n## 1. Acceptance criteria\n\n- AC1 a\n")
    (tmp_path / "specs" / "001-x").mkdir(parents=True)
    (tmp_path / "specs" / "001-x" / "tasks.md").write_text("- [ ] T001 From the spec dir\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(fake, *argv):
    return m.main(list(argv), client_factory=fake.client)


def test_cli_preview_writes_nothing_and_reads_the_ledger_spec_dir(repo, fake, capsys):
    assert run(fake, "push", "1") == 0
    out = capsys.readouterr()
    assert "| create | T001 | - | T001 From the spec dir | - |" in out.out and "dry run: nothing written" in out.out
    assert fake.writes() == [] and "plan sha256" in out.err


def test_cli_expect_plan_binds_the_apply(repo, fake, capsys):
    assert run(fake, "push", KEY, "--plan-out", "p.md") == 0
    (repo / "specs" / "001-x" / "tasks.md").write_text("- [ ] T001 From the spec dir\n- [ ] T002 Sneaked in\n")
    assert run(fake, "push", KEY, "--apply", "--expect-plan", "p.md") == 2
    assert fake.writes() == [] and (repo / "p.md.new").is_file()
    assert run(fake, "push", KEY, "--plan-out", "p.md") == 0
    assert run(fake, "push", KEY, "--apply", "--expect-plan", "p.md") == 0
    assert len(fake.writes()) == 2


def test_cli_refuses_a_key_of_another_project_and_bad_files(repo, fake, capsys):
    assert run(fake, "push", "OTHER-1") == 2 and fake.log == []
    (repo / "big.md").write_text("x" * ((1 << 20) + 1))
    assert run(fake, "push", KEY, "--from", "big.md") == 1
    assert capsys.readouterr().err.endswith("skj: cannot read the tasks file (missing, a symlink, outside the repository, "
                                            "not a regular file, over 1 MiB or not UTF-8); nothing read or written\n")
    (repo / "link.md").symlink_to("big.md")
    assert run(fake, "push", KEY, "--from", "link.md") == 1 and fake.log == []
    (repo / "dod" / "DoD_SPK-1.md").write_text("# DoD SPK-1\n\n## 1. Acceptance criteria\n")
    assert run(fake, "push", KEY) == 1 and fake.log == []
    err = capsys.readouterr().err
    assert "has no spec dir" in err and "`- **Spec dir:**` header line" in err
    assert "dod refresh" not in err  # a refresh resets the AC hash and hides AC drift: never the fix for a missing spec dir (F2 fix 1)


def test_no_cli_message_offers_dod_refresh_to_set_a_spec_dir():
    import skj
    src = Path(skj.__file__).resolve().parent
    for f in src.glob("*.py"):
        for line in f.read_text(encoding="utf-8").splitlines():
            assert not ("dod refresh" in line and ("--spec-dir" in line or "no spec dir" in line)), (f.name, line)


def test_cli_deferred_and_refused_notes(repo, fake, capsys):
    sub(fake, "T001 A")
    _extra_done(fake)
    (repo / "tasks.md").write_text("- [x] T001 A\n- [x] T002 B\n- [x] T003 C\n- [ ] T004 D\n")
    assert run(fake, "push", KEY, "--from", "tasks.md", "--apply") == 0
    err = capsys.readouterr().err
    assert "skj: 2 done transition(s) deferred: run push again\n" in err and "skj: 1 transition(s) refused: see the plan\n" in err
    assert run(fake, "push", KEY, "--from", "tasks.md", "--apply") == 0  # the second run fires the two deferred ones
    assert "deferred" not in capsys.readouterr().err


def test_skip_row_says_when_jira_already_has_it_done(fake):
    sub(fake, "T001 A", status="Done")
    sub(fake, "T002 B")
    text = push.render(planned(fake, [T("T001", "A"), T("T002", "B")]))
    assert "| skip (Jira: done) | T001 | SPK-2 | T001 A | - |" in text and "skip: T002 (1)" in text and "skip 2" in text


def test_cli_server_text_never_reaches_the_terminal(repo, fake, capsys):
    k = sub(fake, "T001 A")
    fake.transitions[3]["name"] = "Do\u202ene|x\x1b[2J"
    (repo / "tasks.md").write_text("- [x] T001 A\n")
    assert run(fake, "push", KEY, "--from", "tasks.md") == 0
    out = capsys.readouterr().out
    assert "\u202e" not in out and "\x1b" not in out and "| 41 Done\\|x[2J |" in out and k in out


def test_push_is_wired_through_the_framework():
    p = [x for x in m._all_parsers(m.build_parser()) if x.get_default("write_verb") == "push"]
    assert len(p) == 1 and {"--apply", "--plan-out", "--expect-plan", "--from"} <= {s for a in p[0]._actions for s in a.option_strings}


# ---- linear time (tasks.md and Jira summaries are untrusted) ---------------------------------------------------------------------------

_MIB = 1 << 20
_BOUND = 2.0


class _TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise _TooSlow()


def _timed(fn, *args):
    # CPU time, not the wall clock (H1b): load slows the clock, not the work; the timed calls run no subprocess and never block
    old = signal.signal(signal.SIGPROF, _alarm)
    signal.setitimer(signal.ITIMER_PROF, _BOUND)
    t0 = time.process_time()
    try:
        fn(*args)
    except _TooSlow:
        return _BOUND
    except ValueError:
        pass
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, old)
    return time.process_time() - t0


needs_timer = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="no setitimer on this platform")


def _pipeline(md):
    tl = tasks.parse_tasks(md)
    push.check_tasks(tl)
    push.render({"key": KEY, "type": {"id": "10002", "name": "Subtask"}, "ops": push.build_ops(tl, [], None)})


SHAPES = {
    "1mib-one-line": "- [ ] T001 " + "| \\\u202e  " * (_MIB // 6),
    "500x1990": "".join("- [x] T%03d %s\n" % (i, ("|\\ \t\u200b" * 400)[:1980]) for i in range(1, 501)),
    "20000-short": "- [ ] T001 a\n" * 20000,
    "200x5000": "".join("- [ ] T%03d %s\n" % (i, "\\|" * 2500) for i in range(1, 201)),
}


@needs_timer
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_plan_pipeline_is_linear(shape):
    assert _timed(_pipeline, SHAPES[shape]) < _BOUND


@needs_timer
@pytest.mark.parametrize("text", ["T001" + " " * _MIB, "T" + "0" * _MIB, "T001 " + "x" * _MIB, "\u202e" * _MIB],
                         ids=["blanks", "digits", "long", "bidi"])
def test_matcher_is_linear(text):
    assert _timed(push.task_id_of, text) < _BOUND


# ---- fix round 2: the cut never reaches the T### prefix ------------------------------------------------------------------------------

@pytest.mark.parametrize("title", ["\u0301" * 300, "e" + "\u0301" * 300], ids=["marks-only", "one-base-many-marks"])
def test_a_cluster_spanning_the_cut_leaves_the_id(title):
    s, cut = push.summary(T("T001", title))
    assert (s, cut) == ("T001", True) and push.task_id_of(s) == "T001"


PIECES = ["\U0001F468\u200d\U0001F469\u200d\U0001F467", "\u2764\ufe0f", "1\ufe0f\u20e3", "\U0001F44D\U0001F3FD",
          "\U0001F1EB\U0001F1F7", "e\u0301", "\u0301", "x", "a b", "\U0001F600"]


def test_summary_keeps_its_id_at_every_cut_offset():
    for tid in ("T001", "T123456789"):
        for piece in PIECES + ["".join(PIECES)]:
            for n in range(240, 301):
                title = (piece * (n // len(piece) + 1))[:n]
                s, cut = push.summary(T(tid, title))
                assert push.task_id_of(s) == tid, (tid, piece, n)
                assert len(s) <= 255 and _u16(s) <= 255 and (s, cut) == push.summary(T(tid, title))


def test_the_invariant_refuses_a_task_instead_of_creating_it(fake, monkeypatch):
    monkeypatch.setattr(push, "_clean", lambda s: "x" + s)  # a summary that no longer starts with its id
    p = planned(fake, [T("T001", "A")])
    assert [(o["action"], o.get("code")) for o in p["ops"]] == [("refused", "summary")]
    assert "| refused | T001 | - | - | refused: summary |" in push.render(p)
    push.apply(fake.client(), p, clock=lambda: 0.0, sleep=lambda s: None)
    assert fake.writes() == []
    with pytest.raises(push.SummaryRefused):
        push._checked("T001", "T001…", True)


def test_a_combining_mark_title_is_created_once(repo, fake, capsys):
    (repo / "tasks.md").write_text("- [ ] T001 " + "\u0301" * 300 + "\n", encoding="utf-8")
    for _ in range(3):
        assert run(fake, "push", KEY, "--from", "tasks.md", "--apply") == 0
    assert [(e["method"], e["path"]) for e in fake.writes()] == [("POST", "/issue")]
    capsys.readouterr()
    assert run(fake, "push", KEY, "--from", "tasks.md") == 0 and "create 0" in capsys.readouterr().out


# ---- A4: comment mode needs the approver's `/approve approve-push|approve-sync <hash of this plan>` --------------------------------

def test_a4_push_in_comment_mode_needs_the_approval_of_this_plan(repo, fake, capsys):
    import os
    from fakejira import ME_ID
    from skj import adf, approvals
    approver = "5b10ac8d82e05b22cc7d4ef5"  # invented
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    (repo / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK", "approvers": [approver],
                                                      "approval": "comment"}))
    for a in (["add", "-A"], ["commit", "-q", "-m", "target"], ["update-ref", "refs/remotes/origin/main", "HEAD"],
              ["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"]):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, stdin=subprocess.DEVNULL, env=env)
    assert run(fake, "push", KEY, "--plan-out", "p.md") == 0
    h = approvals.gate_hash("approve-sync", KEY, (repo / "p.md").read_bytes())
    assert run(fake, "push", KEY, "--apply", "--expect-plan", "p.md") == 2 and fake.writes() == []
    for who in (ME_ID, "5c20bd9e93f16c33dd8e5fa6"):  # the agent and a stranger never approve
        fake.add_comment(KEY, adf.md_to_adf("/approve approve-sync %s" % h), author=fake.user(who))
    assert run(fake, "push", KEY, "--apply", "--expect-plan", "p.md") == 2 and fake.writes() == []
    fake.add_comment(KEY, adf.md_to_adf("/approve approve-sync %s" % h), author=fake.user(approver))
    assert run(fake, "push", KEY, "--apply", "--expect-plan", "p.md") == 0 and len(fake.writes()) == 1
    (repo / "specs" / "001-x" / "tasks.md").write_text("- [ ] T001 From the spec dir\n- [ ] T002 New\n")
    assert run(fake, "push", KEY, "--apply") == 2 and len(fake.writes()) == 1  # a new plan needs a new approval
    capsys.readouterr()


def test_h1a_ac7_a_configured_done_name_that_lands_in_progress_is_refused_and_nothing_is_applied(fake):
    """H1a-AC7 (P4's check, now shared): the `Done`-named transition of this workflow leads to an `indeterminate` status."""
    next(t for t in fake.transitions if t["name"] == "Done")["to"] = "In Review"
    sub(fake, "T001 Ticked and open")
    rec = Recorder(fake)
    c = rec.client()
    p = push.plan(c, cfg(transitions={"active": None, "done": "Done"}), KEY, [T("T001", "Ticked and open", True)])
    assert [(o["action"], o.get("code")) for o in p["ops"]] == [("refused", "not-done")]
    assert transitions.MESSAGES["not-done"] in push.render(p)
    push.apply(c, p, clock=lambda: 0.0, sleep=lambda s: None)
    assert writes(rec) == [] and fake.issue("SPK-2")["status"] == "To Do"
    # a configured name whose target IS in the done category still fires
    next(t for t in fake.transitions if t["name"] == "Done")["to"] = "Done"
    p = push.plan(c, cfg(transitions={"active": None, "done": "Done"}), KEY, [T("T001", "Ticked and open", True)])
    assert [o["action"] for o in p["ops"]] == ["transition"]


def test_a_refused_login_on_a_write_says_authentication_failed(fake):
    """DF1 (D-003): the 404 + X-Seraph-LoginReason shape of a failed login (live runs A2-28, A5-35), hand-written, on the create."""
    class Refused(Recorder):
        def __call__(self, method, url, headers, body, timeout):
            if method == "POST":
                return 404, {"X-Seraph-LoginReason": "AUTHENTICATED_FAILED"}, b'{"errorMessages":["Issue does not exist"],"errors":{}}'
            return super().__call__(method, url, headers, body, timeout)
    rec = Refused(fake)
    c = rec.client()
    res = push.apply(c, push.plan(c, cfg(), KEY, [T("T001", "A")]), clock=lambda: 0.0, sleep=lambda s: None)
    assert res["stopped"].startswith("failed: create T001: Jira did not accept the credentials (HTTP 404, authentication failed")


def test_summary_scrubs_mentions_and_emails_like_decisions_and_questions():
    """DF1 (live run A6-23, D-014): `- [x] T010 normal title #time 1w @someone a@example.com` (fictional) became the subtask summary
    unchanged; AGENTS.md rule 8 and the decisions/questions posts replace @mentions and addresses."""
    s, cut = push.summary({"id": "T010", "title": "normal title #time 1w @someone a@example.com [~accountid:5b10ac8d82e05b22cc7d4ef5]"})
    assert s == "T010 normal title #time 1w @user [email] @user" and not cut
    assert "example.com" not in s and "someone" not in s and "5b10ac8d" not in s
    assert push.summary({"id": "T011", "title": "jane\u200b.doe@example.com"})[0] == "T011 [email]"  # an invisible split is no shield


def test_a_scrubbed_summary_is_what_the_plan_creates(fake):
    p = planned(fake, [T("T010", "ping @someone at a@example.com")])
    op = [o for o in p["ops"] if o["action"] == "create"][0]
    assert "@user" in json.dumps(op) and "a@example.com" not in json.dumps(p) and "someone" not in json.dumps(p)
