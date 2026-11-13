"""P4: `skj done` closes a story: ledger section 5 fully ticked (the fork's rule), the ledger (and Decisions_<KEY>.md) attached
FIRST, then the done transition chosen by W2, behind the X2 gate. Against the fake Jira only.

Ported from the fork's tests/test_done.py (the `check` cases, idempotence, attach-before-state, the decisions file, symlinked files,
--expect-plan); rebased on the Jira model: the state is a transition chosen by status category (K3), never a PATCH/PUT of a state
field, `resolution` is never read, an attachment is skipped on name AND size (P3). The fork's `done` has no `--force`: nothing
overrides an unticked section 5, so `--force` is a usage error here too (P4-AC5).
"""
import json
import os
import signal
import subprocess
import time

import pytest

import skj.__main__ as m
from skj import attachments, dod, done, gate, transitions
from skj.client import Client
from fakejira import FakeJira

KEY = "SPK-1"
SITE = "example-site.atlassian.net"
S5 = "## 5. Definition of Done  (all ✅ to close)\n"
ISSUE = {"key": KEY, "id": "10001", "fields": {"summary": "A fictional story", "issuetype": {"name": "Story"},
                                               "status": {"name": "To Do"}}}
AC = "- A fictional criterion about the fictional widget that must render a fictional list of fictional rows\n"


def ledger(*items, tail=""):
    """The fork's minimal ledger: a box before the marker (never counted), the marker, section 5 with `items`."""
    return "# DoD\n\n- [ ] not in section 5\n\n" + dod.SECTION2 + "\n\n" + S5 + "".join("%s\n" % i for i in items) + tail


def tick(md):
    """The kit's own template with every section 5 box ticked."""
    head, s5 = md.split("## 5.", 1)
    return head + "## 5." + s5.replace("- [ ]", "- [x]")


TEMPLATE = dod.render(ISSUE, AC, SITE)
READY = tick(TEMPLATE)


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, stdin=subprocess.DEVNULL)


@pytest.fixture
def git_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv("GIT_%s_NAME" % k, "Test")
        monkeypatch.setenv("GIT_%s_EMAIL" % k, "test@example.invalid")


@pytest.fixture
def repo(tmp_path, monkeypatch, git_env):
    for k in ("SKJ_CONFIG", "CI", "GITHUB_ACTIONS", "SKJ_EMAIL", "SKJ_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK"}))
    (tmp_path / "dod").mkdir()
    (tmp_path / "dod" / "DoD_SPK-1.md").write_text(READY, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def fake():
    f = FakeJira()
    assert f.add_issue("A fictional story", status="In Progress") == KEY
    return f


def run(fake, *argv, ref=KEY):
    return m.main(["done", ref, *argv], client_factory=fake.client)


def calls(fake):
    return [(e["method"], e["path"]) for e in fake.log]


def writes(fake):
    return [(e["method"], e["path"]) for e in fake.writes()]


READS = [("GET", "/issue/" + KEY), ("GET", "/issue/%s/transitions" % KEY), ("GET", "/issue/" + KEY)]
ATTACH, TRANSITION = ("POST", "/issue/%s/attachments" % KEY), ("POST", "/issue/%s/transitions" % KEY)


def attach_existing(fake, name, data):
    fake.issue(KEY)["attachments"].append({"id": "9%d" % len(fake.issue(KEY)["attachments"]), "filename": name,
                                           "size": len(data), "mimeType": "text/markdown"})


def no_jira():
    raise AssertionError("done must not build a Jira client here")


# ---- the ledger rule: section 5 fully ticked (ported `check` cases) ---------------------------------------------------------------

def test_check_all_ticked():
    assert done.check(ledger("- [x] a", "- [X] b")) == {"ready": True, "unticked": [], "boxes": 2}


def test_check_lists_unticked():
    r = done.check(ledger("- [x] a", "- [ ] b", "- [ ] c"))
    assert (r["ready"], r["unticked"]) == (False, ["b", "c"])


def test_missing_section5():
    assert done.check("# DoD\n- [x] a\n")["unticked"] == [done.NO_SECTION]


def test_section5_without_checklist():
    for body in ("just prose", "", "   ", "### a sub heading\nprose"):
        r = done.check(ledger(body))
        assert (r["ready"], r["unticked"]) == (False, [done.NO_CHECKLIST])
    r = done.check(dod.SECTION2 + "\n" + S5)  # an empty section 5 at the very end
    assert (r["ready"], r["unticked"]) == (False, [done.NO_CHECKLIST])


def test_check_ignores_boxes_after_next_heading():
    assert done.check(ledger("- [x] a", tail="\n## 6. Notes\n- [ ] later\n"))["ready"] is True


@pytest.mark.parametrize("line", ["* [ ] b", "+ [ ] b", "  - [ ] b", "-  [ ] b", "- [ ]", "\t- [ ] b"])
def test_check_box_variants_fail_closed(line):
    r = done.check(ledger("- [x] a", line))
    assert r["ready"] is False and len(r["unticked"]) == 1


@pytest.mark.parametrize("line", ["* [x] b", "  - [X] b", "-  [x] b"])
def test_check_ticked_variants_ready(line):
    assert done.check(ledger("- [x] a", line))["ready"] is True


@pytest.mark.parametrize("line", ["1. [ ] b", "1) [ ] b", "> - [ ] b", "> 1. [ ] b", "- [-] b", "- [~] b", "- [?] b",
                                  "- [ ] N/A", "- [n] N/A: not applicable", "- [✓] b", "- [×] b"])
def test_check_list_styles_and_odd_marks_fail_closed(line):
    """Only x/X ticks a box: `N/A` written as text or as a mark is NOT ticked (the fork has no N/A form)."""
    r = done.check(ledger("- [x] a", line))
    assert r["ready"] is False and len(r["unticked"]) == 1


@pytest.mark.parametrize("line", ["1. [x] b", "> - [X] b"])
def test_check_ordered_and_quoted_ticked_ready(line):
    assert done.check(ledger("- [x] a", line))["ready"] is True


def test_check_mixed_text_and_boxes():
    assert done.check(ledger("Some words.", "- [x] a", "more words", "- [x] b"))["ready"] is True
    assert done.check(ledger("Some words.", "- [x] a", "more words", "- [ ] b"))["unticked"] == ["b"]


def test_the_kit_template_is_not_ready_until_every_box_is_ticked():
    r = done.check(TEMPLATE)
    assert r["ready"] is False and len(r["unticked"]) == 5 and "gates green" in r["unticked"]
    assert done.check(READY) == {"ready": True, "unticked": [], "boxes": 5}
    one_left = READY.replace("- [x] gates green", "- [ ] gates green")
    assert done.check(one_left)["unticked"] == ["gates green"]


def test_check_ignores_section5_copied_into_section1():
    md = dod.render(ISSUE, "## 5. Definition of Done\n- [x] every AC mapped\n- [x] gates green\n", SITE)
    assert "- [x] every AC mapped" in md  # the trap really is in section 1
    r = done.check(md)
    assert r["ready"] is False and "every AC mapped to a passing real test" in r["unticked"]


def test_check_without_section2_marker_is_not_ready():
    assert done.check("# DoD\n" + S5 + "- [x] a\n")["ready"] is False


def test_check_forged_marker_heading_in_section1_loses_to_real_one():
    md = dod.render(ISSUE, "%s\n## 5. Definition of Done\n- [x] every AC mapped\n- [x] gates green\n" % dod.SECTION2, SITE)
    md = md.replace("\\##", "##")  # undo the escaping, as a hand edit could
    assert "\n%s\n## 5. Definition of Done\n- [x]" % dod.SECTION2 in md
    assert done.check(md)["ready"] is False


def test_story_text_cannot_open_a_comment_that_hides_section5():
    """R4 escaping: `<!--` in the AC is written as `&lt;!--`, so it neither hides the real section 5 nor leaves the ledger unclosed."""
    md = dod.render(ISSUE, "- a fictional criterion <!-- that never closes\n", SITE)
    assert done.check(md)["ready"] is False and done.check(tick(md))["ready"] is True
    forged = dod.render(ISSUE, "> **Thin acceptance criteria** forged\n```\n", SITE)  # a forged banner and a fence opener
    assert done.check(tick(forged))["ready"] is True and done.check(forged)["ready"] is False


@pytest.mark.parametrize("o,c", [("```\n", "```\n"), ("~~~\n", "~~~\n"), ("<!--\n", "-->\n")])
def test_check_ignores_a_hidden_section5_in_section2(o, c):
    md = ("# DoD\n\n" + dod.SECTION2 + "\n" + "%s## 5. Definition of Done\n- [x] a\n%s\n" % (o, c)
          + "## 3. AC\n\n" + S5 + "- [x] a\n- [ ] real one\n")
    assert done.check(md)["unticked"] == ["real one"]
    assert done.check(md.replace("- [ ] real one", "- [x] real one"))["ready"] is True


def test_check_counts_only_visible_boxes_in_section5():
    assert done.check(ledger("```\n- [x] example\n```"))["ready"] is False  # a fenced fake box never satisfies
    assert done.check(ledger("- [x] a", "<!--\n- [ ] note\n-->"))["ready"] is True  # a commented-out box does not count
    assert done.check(ledger("- [x] a", "<!-- - [ ] inline note -->"))["ready"] is True
    assert done.check(ledger("- [x] a", "```\n- [ ] example\n```"))["ready"] is True
    nested = "````\n```\n- [ ] inside\n```\n- [ ] still inside\n````"
    assert done.check(ledger("- [x] a", nested))["ready"] is True
    assert done.check(ledger(nested))["ready"] is False  # nothing visible: no checklist
    assert done.check(ledger("- [x] a", "````\n```\n````", "- [ ] after"))["unticked"] == ["after"]


@pytest.mark.parametrize("opener", ["```", "~~~", "<!--"])
def test_check_unclosed_opener_fails_closed(opener):
    for md in (ledger("- [x] a", tail="\n%s\n" % opener), ledger("- [x] a", opener)):
        assert done.check(md)["unticked"] == [done.UNCLOSED]
    sec1 = "# DoD\n" + opener + "\n" + dod.SECTION2 + "\n" + S5 + "- [x] a\n"
    assert done.check(sec1)["unticked"] == [done.UNCLOSED]


# ---- P4-AC1: dry run unless --apply --------------------------------------------------------------------------------------------------

def test_dry_run_writes_nothing(repo, fake, capsys):
    before = (repo / "dod" / "DoD_SPK-1.md").read_bytes()
    assert run(fake) == 0
    out = capsys.readouterr().out
    assert calls(fake) == READS and writes(fake) == []
    assert "upload dod/DoD_SPK-1.md (%d bytes, text/markdown)" % len(before) in out
    assert "transition: 41 Done (to Done)" in out and "dry run" in out
    assert fake.issue(KEY)["status"] == "In Progress" and fake.issue(KEY)["attachments"] == []
    assert (repo / "dod" / "DoD_SPK-1.md").read_bytes() == before
    assert sorted(os.listdir(str(repo))) == [".git", "dod", "skj.config.json"]


def test_reads_name_their_fields_and_expand_the_transition_fields(repo, fake):
    assert run(fake) == 0
    assert fake.log[0]["query"] == {"fields": "status"}  # U19: explicit fields; K3: never `resolution`
    assert fake.log[1]["query"] == {"expand": "transitions.fields"}
    assert fake.log[2]["query"] == {"fields": "attachment"}
    assert not any("resolution" in json.dumps(e["query"]) for e in fake.log)


def test_plan_out_writes_only_the_plan(repo, fake):
    assert run(fake, "--plan-out", ".skj/plan.md") == 0
    assert writes(fake) == [] and "Plan for closing SPK-1" in (repo / ".skj" / "plan.md").read_text()


def test_the_plan_is_deterministic(repo, fake, capsys):
    assert run(fake, "--plan-out", "a.md") == 0 and run(fake, "--plan-out", "b.md") == 0
    assert (repo / "a.md").read_bytes() == (repo / "b.md").read_bytes()


# ---- the order of writes ---------------------------------------------------------------------------------------------------------

def test_attach_then_transition_order(repo, fake, capsys):
    assert run(fake, "--apply") == 0
    assert writes(fake) == [ATTACH, TRANSITION]
    assert fake.issue(KEY)["status"] == "Done"
    assert [a["filename"] for a in fake.issue(KEY)["attachments"]] == ["DoD_SPK-1.md"]
    out = capsys.readouterr().out
    assert "uploaded dod/DoD_SPK-1.md" in out and "transitioned SPK-1 to Done (id 41)" in out
    assert fake.sleeps == []


def test_decisions_file_attached_after_the_ledger(repo, fake, capsys):
    (repo / "dod" / "Decisions_SPK-1.md").write_text("# Decisions SPK-1\n", encoding="utf-8")
    assert run(fake) == 0
    out = capsys.readouterr().out
    assert out.index("upload dod/DoD_SPK-1.md") < out.index("upload dod/Decisions_SPK-1.md")
    assert run(fake, "--apply") == 0
    assert writes(fake) == [ATTACH, ATTACH, TRANSITION]
    assert [a["filename"] for a in fake.issue(KEY)["attachments"]] == ["DoD_SPK-1.md", "Decisions_SPK-1.md"]


def test_without_a_decisions_file_only_the_ledger(repo, fake, capsys):
    assert run(fake, "--apply") == 0
    assert "Decisions_" not in capsys.readouterr().out and writes(fake) == [ATTACH, TRANSITION]


def test_ledger_already_attached_only_transitions(repo, fake, capsys):
    attach_existing(fake, "DoD_SPK-1.md", READY.encode("utf-8"))
    assert run(fake, "--apply") == 0
    assert writes(fake) == [TRANSITION] and "skip dod/DoD_SPK-1.md" in capsys.readouterr().out


def test_another_attachment_still_uploads(repo, fake):
    attach_existing(fake, "other.md", b"x")
    attach_existing(fake, "DoD_SPK-1.md", b"an older, shorter ledger")  # same name, other size: uploaded, Jira keeps both (P3)
    assert run(fake, "--apply") == 0 and writes(fake) == [ATTACH, TRANSITION]


# ---- P4-AC2: a second apply writes nothing ------------------------------------------------------------------------------------------

def test_second_apply_noop(repo, fake, capsys):
    (repo / "dod" / "Decisions_SPK-1.md").write_text("# Decisions SPK-1\n", encoding="utf-8")
    assert run(fake, "--apply") == 0
    n = len(fake.writes())
    capsys.readouterr()
    fake.log.clear()
    assert run(fake, "--apply", "--plan-out", "p.md") == 0
    assert fake.writes() == [] and len(fake.log) == 2  # the story, then the attachments: no transitions read when already done
    out = capsys.readouterr().out
    assert "nothing to do" in out.lower() and "nothing to do" in (repo / "p.md").read_text().lower()
    assert n == 3 and fake.issue(KEY)["status"] == "Done" and len(fake.issue(KEY)["attachments"]) == 2


# ---- P4-AC3: done is the status category -----------------------------------------------------------------------------------------

def test_already_done_skipped(repo, fake, capsys):
    fake.issue(KEY)["status"] = "Done"
    attach_existing(fake, "DoD_SPK-1.md", READY.encode("utf-8"))
    assert run(fake, "--apply") == 0
    assert fake.writes() == [] and calls(fake) == [("GET", "/issue/" + KEY), ("GET", "/issue/" + KEY)]
    assert "already done" in capsys.readouterr().out.lower()


def test_already_done_story_without_the_ledger_attaches_it_and_leaves_the_status(repo, fake, capsys):
    fake.issue(KEY)["status"] = "Done"
    assert run(fake) == 0
    out = capsys.readouterr().out
    assert "upload dod/DoD_SPK-1.md" in out and "already done" in out.lower() and "nothing to do" not in out.lower()
    assert run(fake, "--apply") == 0
    assert writes(fake) == [ATTACH] and fake.issue(KEY)["status"] == "Done"


def _rewrite_story(fake, change):
    """An opener around the fake that edits the story's GET answer (the fake's statuses are fixed)."""
    def opener(method, url, headers, body, timeout):
        status, h, out = fake(method, url, headers, body, timeout)
        if method == "GET" and url.split("?")[0].endswith("/issue/" + KEY) and "fields=status" in url:
            d = json.loads(out)
            change(d)
            out = json.dumps(d).encode("utf-8")
        return status, h, out
    return lambda: Client(fake.site, fake.email, fake.token, opener=opener, sleep=fake.sleeps.append)


def test_status_category_decides_not_the_name_and_never_resolution(repo, fake, capsys):
    def named_done(d):  # a status NAMED Done in the in-progress category, with a resolution set: not done
        d["fields"]["status"]["name"] = "Done"
        d["fields"]["resolution"] = {"name": "Fixed"}
    assert m.main(["done", KEY, "--apply"], client_factory=_rewrite_story(fake, named_done)) == 0
    assert writes(fake) == [ATTACH, TRANSITION]

    fake2 = FakeJira()
    fake2.add_issue("A fictional story", status="In Progress")

    def cat_done(d):  # a status named anything in the done category, resolution null: done
        d["fields"]["status"]["name"] = "Shipped"
        d["fields"]["status"]["statusCategory"]["key"] = "done"
        d["fields"]["resolution"] = None
    assert m.main(["done", KEY, "--apply"], client_factory=_rewrite_story(fake2, cat_done)) == 0
    assert writes(fake2) == [ATTACH] and fake2.issue(KEY)["status"] == "In Progress"
    src = open(done.__file__, encoding="utf-8").read()
    assert "resolution" not in src and '"PUT"' not in src and "'PUT'" not in src


@pytest.mark.parametrize("bad", [
    lambda d: d["fields"].pop("status"),
    lambda d: d["fields"]["status"].update(id=10002),
    lambda d: d["fields"]["status"].update(id="0"),
    lambda d: d["fields"]["status"].pop("statusCategory"),
    lambda d: d["fields"]["status"]["statusCategory"].update(key="finished"),
    lambda d: d["fields"]["status"].update(name=["x"]),
    lambda d: d.update(fields=[]),
])
def test_a_malformed_status_is_refused(repo, fake, capsys, bad):
    assert m.main(["done", KEY, "--apply"], client_factory=_rewrite_story(fake, bad)) == 1
    assert fake.writes() == [] and done.E_STATUS in capsys.readouterr().err


# ---- P4-AC4: never guess the transition ---------------------------------------------------------------------------------------------

def _done_transition(fake):
    return next(t for t in fake.transitions if t["to"] == "Done")


@pytest.mark.parametrize("change,code", [
    ({"hasScreen": True}, "screen"), ({"isConditional": True}, "conditional"),
    ({"fields": {"customfield_10050": {"required": True, "name": "Fix version"}}}, "required-fields")])
def test_screen_refused_with_hint(repo, fake, capsys, change, code):
    _done_transition(fake).update(change)
    for argv in ((), ("--apply",)):
        assert run(fake, *argv) == 1
        out, err = capsys.readouterr()
        assert "transition: refused: %s" % code in out and transitions.MESSAGES[code] in out
        assert "transitions.done" in out and "transitions.done" in err
    assert fake.writes() == [] and fake.issue(KEY)["attachments"] == []


@pytest.mark.parametrize("ledger_md", [READY, TEMPLATE], ids=["refused-transition", "not-ready"])
def test_apply_writes_nothing_for_a_refusing_plan(repo, fake, ledger_md):
    from skj import config
    _done_transition(fake)["hasScreen"] = True
    c = fake.client()
    p = done.plan(c, config.Config.load(), str(repo), KEY, {"ledger": ledger_md, "decisions": False, "check": done.check(ledger_md)})
    assert done.apply(c, p) == {"lines": ["nothing written: the plan refuses the close"], "ok": False}
    assert fake.writes() == []


def test_required_fields_are_seen_only_through_the_expand(repo, fake):
    _done_transition(fake)["fields"] = {"resolution_like": {"required": True}}
    assert run(fake, "--apply") == 1 and fake.writes() == []
    assert fake.log[1]["query"] == {"expand": "transitions.fields"}


def test_an_unavailable_transition_is_refused(repo, fake, capsys):
    def opener(method, url, headers, body, timeout):
        status, h, out = fake(method, url, headers, body, timeout)
        if method == "GET" and "/transitions" in url:
            d = json.loads(out)
            for t in d["transitions"]:
                t["isAvailable"] = False
            out = json.dumps(d).encode("utf-8")
        return status, h, out
    assert m.main(["done", KEY, "--apply"], client_factory=lambda: Client(fake.site, fake.email, fake.token, opener=opener)) == 1
    assert "refused: unavailable" in capsys.readouterr().out and fake.writes() == []


def test_two_done_transitions_are_ambiguous_until_configured(repo, fake, capsys):
    fake.transitions.append({"id": "51", "name": "Close ‮quickly", "to": "Done", "hasScreen": False, "isConditional": False,
                             "fields": {}})
    assert run(fake, "--apply") == 1
    out = capsys.readouterr().out
    assert "refused: ambiguous" in out and "transitions.done" in out and "Close quickly" in out and "‮" not in out
    assert fake.writes() == []
    (repo / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK", "transitions": {"done": "Done"}}))
    assert run(fake, "--apply") == 0
    assert writes(fake) == [ATTACH, TRANSITION] and fake.issue(KEY)["status"] == "Done"
    assert "transitioned SPK-1 to Done (id 41)" in capsys.readouterr().out  # the configured name, not the twin 51


def test_a_configured_name_that_matches_nothing_is_refused(repo, fake, capsys):
    (repo / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK", "transitions": {"done": "Ship it"}}))
    assert run(fake, "--apply") == 1
    assert "refused: none" in capsys.readouterr().out and fake.writes() == []


@pytest.mark.parametrize("name", ["To Do", "In Progress", "In Review"])
def test_a_configured_name_that_does_not_close_the_story_is_refused(repo, fake, capsys, name):
    (repo / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK", "transitions": {"done": name}}))
    for argv in ((), ("--apply",)):
        assert run(fake, *argv) == 1
        out = capsys.readouterr().out
        assert "transition: refused:" in out and "transitions.done" in out and "transitioned" not in out
    assert fake.writes() == [] and fake.issue(KEY)["status"] == "In Progress"


def test_a_configured_name_that_closes_the_story_passes(repo, fake, capsys):
    (repo / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK", "transitions": {"done": "Done"}}))
    assert run(fake, "--apply") == 0 and writes(fake) == [ATTACH, TRANSITION] and fake.issue(KEY)["status"] == "Done"


def test_the_report_after_the_transition_is_the_re_read_status(repo, fake, capsys):
    """A 204 that did not move the story (a workflow post-function, a race): the re-read decides, exit 1."""
    def opener(method, url, headers, body, timeout):
        if method == "POST" and url.endswith("/transitions"):
            fake.log.append({"method": method, "path": url.split("/rest/api/3", 1)[1], "query": {}, "write": True, "status": 204})
            return 204, {}, b""
        return fake(method, url, headers, body, timeout)
    factory = lambda: Client(fake.site, fake.email, fake.token, opener=opener, sleep=fake.sleeps.append)  # noqa: E731
    assert m.main(["done", KEY, "--apply"], client_factory=factory) == 1
    out = capsys.readouterr().out
    assert done.E_SENT_NOT_DONE % ("41", KEY) in out and "transitioned" not in out
    assert calls(fake)[-1] == ("GET", "/issue/" + KEY) and writes(fake) == [ATTACH, TRANSITION]


def test_a_failed_re_read_after_the_transition_is_not_success(repo, fake, capsys):
    fake.fail_transport(on=6)  # reads 1-3, upload 4, transition 5 (applied), the confirming re-read 6 fails
    assert run(fake, "--apply") == 1
    out = capsys.readouterr().out
    assert "was sent but the re-read failed" in out and "transitioned" not in out and writes(fake) == [ATTACH, TRANSITION]


def test_no_done_transition_is_refused(repo, fake, capsys):
    fake.transitions[:] = [t for t in fake.transitions if t["to"] != "Done"]
    assert run(fake, "--apply") == 1 and "refused: none" in capsys.readouterr().out and fake.writes() == []


# ---- P4-AC5: --force keeps the fork's rules: `done` has no override at all ---------------------------------------------------------

def test_not_ready_never_writes_even_with_apply(repo, fake, capsys):
    (repo / "dod" / "DoD_SPK-1.md").write_text(TEMPLATE, encoding="utf-8")
    assert run(fake, "--apply") == 1
    out = capsys.readouterr().out
    assert "Not ready" in out and "- gates green" in out and fake.log == []  # not even a read


@pytest.mark.parametrize("flag", ["--force", "--forc", "--yes", "--allow-incomplete"])
def test_force_is_not_an_option_of_done(repo, fake, capsys, flag):
    (repo / "dod" / "DoD_SPK-1.md").write_text(TEMPLATE, encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        run(fake, "--apply", flag)
    assert e.value.code == 2 and fake.log == []


def test_unticked_items_are_cleaned_and_capped_in_the_plan(repo, fake, capsys):
    items = ["- [ ] item %d with | pipe and \x1b[31m escape ‮" % i for i in range(done.MAX_SHOWN + 5)]
    (repo / "dod" / "DoD_SPK-1.md").write_text(ledger(*items), encoding="utf-8")
    assert run(fake, "--plan-out", "p.md") == 1
    out = capsys.readouterr().out
    assert "\x1b" not in out and "‮" not in out and "\\|" in out
    assert "and 5 more" in out and out.count("\n- item") == done.MAX_SHOWN and fake.log == []
    assert "Not ready" in (repo / "p.md").read_text()


# ---- ledger and decisions file refusals (ported: symlinks, encoding; new: size) -----------------------------------------------------

def _outside_secret(tmp_path):
    out = tmp_path.parent / (tmp_path.name + "-outside")
    out.mkdir()
    (out / "secret.md").write_text("TOPSECRET\n")
    return out / "secret.md"


def test_missing_ledger_refused_without_a_request(repo, capsys):
    (repo / "dod" / "DoD_SPK-1.md").unlink()
    assert m.main(["done", KEY, "--apply"], client_factory=no_jira) == 1
    assert done.E_LEDGER in capsys.readouterr().err


@pytest.mark.parametrize("target", ["outside", "inside"])
def test_a_symlinked_ledger_is_never_read_or_uploaded(repo, capsys, target):
    led = repo / "dod" / "DoD_SPK-1.md"
    real = _outside_secret(repo) if target == "outside" else repo / "dod" / "real.md"
    if target == "inside":
        real.write_text(READY)
    led.unlink()
    led.symlink_to(real)
    assert m.main(["done", KEY, "--apply"], client_factory=no_jira) == 1
    err = capsys.readouterr().err
    assert ("ledger" in err or "symbolic link" in err) and "TOPSECRET" not in err and "secret.md" not in err


@pytest.mark.parametrize("data", [b"\xff\xfe not utf-8", b"x" * ((1 << 20) + 1)], ids=["not-utf8", "oversize"])
def test_an_unreadable_or_oversize_ledger_is_refused(repo, capsys, data):
    (repo / "dod" / "DoD_SPK-1.md").write_bytes(data)
    assert m.main(["done", KEY, "--apply"], client_factory=no_jira) == 1
    assert done.E_LEDGER in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["outside", "inside", "not-utf8", "directory"])
def test_a_bad_decisions_file_is_refused_before_any_request(repo, capsys, kind):
    dec = repo / "dod" / "Decisions_SPK-1.md"
    if kind == "outside":
        dec.symlink_to(_outside_secret(repo))
    elif kind == "inside":
        (repo / "dod" / "real.md").write_text("# Decisions\n")
        dec.symlink_to(repo / "dod" / "real.md")
    elif kind == "not-utf8":
        dec.write_bytes(b"\xff\xfe")
    else:
        dec.mkdir()
    for argv in (["--plan-out", "p.md"], ["--apply"]):
        assert m.main(["done", KEY, *argv], client_factory=no_jira) == 1
        err = capsys.readouterr().err
        assert "decisions file" in err and "TOPSECRET" not in err and "secret.md" not in err
    assert not (repo / "p.md").exists()


def test_the_uploaded_ledger_is_the_one_that_was_checked(repo, fake, capsys, monkeypatch):
    """The ledger is read twice (the check, then P3's one read for the upload): different bytes refuse the whole run."""
    real = gate._read
    monkeypatch.setattr(gate, "_read", lambda p, r, s: READY if p.endswith("DoD_SPK-1.md") else real(p, r, s))
    (repo / "dod" / "DoD_SPK-1.md").write_text(TEMPLATE, encoding="utf-8")  # what P3 reads and would upload: not ticked
    assert run(fake, "--apply") == 1
    assert done.E_CHANGED in capsys.readouterr().err and fake.writes() == []


# ---- uncertain and failed writes ------------------------------------------------------------------------------------------------------

def test_an_uncertain_upload_stops_before_the_transition(repo, fake, capsys):
    fake.fail_transport(on=4)  # the upload POST times out, not applied
    assert run(fake, "--apply") == 1
    out = capsys.readouterr().out
    assert writes(fake) == [ATTACH] and "write uncertain" in out and done.NOT_TRANSITIONED in out
    assert fake.issue(KEY)["status"] == "In Progress"


def test_an_upload_found_on_re_read_still_stops_this_run(repo, fake, capsys):
    fake.fail_transport(on=4, applied=True)  # the upload happened, the answer was lost
    assert run(fake, "--apply") == 1
    out = capsys.readouterr().out
    assert "(found on re-read)" in out and done.NOT_TRANSITIONED in out and writes(fake) == [ATTACH]
    fake.log.clear()
    assert run(fake, "--apply") == 0 and writes(fake) == [TRANSITION]  # the next run skips the file and closes the story
    assert len(fake.issue(KEY)["attachments"]) == 1


def _failing(fake, path_end, status=403):
    def opener(method, url, headers, body, timeout):
        if method == "POST" and url.endswith(path_end):
            fake.log.append({"method": method, "path": url.split("/rest/api/3", 1)[1], "query": {}, "write": True, "status": status})
            return status, {"Content-Type": "application/json"}, b'{"errorMessages":["\\u202eevil server text"],"errors":{}}'
        return fake(method, url, headers, body, timeout)
    return lambda: Client(fake.site, fake.email, fake.token, opener=opener, sleep=fake.sleeps.append)


def test_a_refused_upload_stops_before_the_transition(repo, fake, capsys):
    assert m.main(["done", KEY, "--apply"], client_factory=_failing(fake, "/attachments")) == 1
    out = capsys.readouterr().out
    assert "HTTP 403" in out and done.NOT_TRANSITIONED in out and "evil" not in out
    assert writes(fake) == [ATTACH] and fake.issue(KEY)["status"] == "In Progress"


def test_a_refused_transition_is_reported_and_not_retried(repo, fake, capsys):
    assert m.main(["done", KEY, "--apply"], client_factory=_failing(fake, "/transitions", 400)) == 1
    out = capsys.readouterr().out
    assert "uploaded dod/DoD_SPK-1.md" in out and "HTTP 400" in out and "evil" not in out
    assert writes(fake) == [ATTACH, TRANSITION]


@pytest.mark.parametrize("applied,found", [(True, True), (False, False)])
def test_an_uncertain_transition_re_reads_the_status(repo, fake, capsys, applied, found):
    fake.fail_transport(on=5, applied=applied)  # reads 1-3, upload 4, transition 5
    assert run(fake, "--apply") == 1
    out = capsys.readouterr().out
    assert writes(fake) == [ATTACH, TRANSITION]  # never a second POST
    assert calls(fake)[-1] == ("GET", "/issue/" + KEY) and fake.log[-1]["query"] == {"fields": "status"}
    assert ("transitioned SPK-1 (found on re-read)" in out) is found
    assert ("write uncertain" in out) is not found


def test_an_uncertain_transition_whose_re_read_fails(repo, fake, capsys):
    fake.fail_transport(on=5)
    fake.fail_transport(on=6)
    assert run(fake, "--apply") == 1
    assert "re-read failed" in capsys.readouterr().out and writes(fake) == [ATTACH, TRANSITION]


def test_apply_passes_the_injectable_clock_and_sleep(repo, fake, monkeypatch):
    seen = {}
    real = attachments.apply_attachments

    def spy(c, plan, clock, sleep):
        seen.update(clock=clock, sleep=sleep)
        return real(c, plan, clock, sleep)
    monkeypatch.setattr(attachments, "apply_attachments", spy)
    from skj import config
    clock, sleep = (lambda: 0.0), (lambda s: None)
    c = fake.client()
    p = done.plan(c, config.Config.load(), str(repo), KEY, done.local(str(repo), KEY))
    assert done.apply(c, p, clock=clock, sleep=sleep)["ok"] is True
    assert seen == {"clock": clock, "sleep": sleep}


# ---- planning reads fail closed with fixed texts ---------------------------------------------------------------------------------------

def test_a_read_error_is_a_fixed_text(repo, fake, capsys):
    fake.inject(404, body={"errorMessages": ["‮server text"], "errors": {}})
    assert run(fake, "--apply") == 1
    err = capsys.readouterr().err
    assert "HTTP 404" in err and "server text" not in err and fake.writes() == []


def test_a_transport_error_while_reading_is_a_fixed_text(repo, fake, capsys):
    fake.fail_transport(on=2)
    assert run(fake, "--apply") == 1
    assert "nothing written" in capsys.readouterr().err and fake.writes() == []


def test_a_moved_story_is_refused(repo, fake, capsys):
    fake.move_issue(KEY, "SPK-7")
    assert run(fake, "--apply") == 1
    assert "SPK-7" in capsys.readouterr().err and fake.writes() == []


def test_a_bad_attachment_listing_is_refused(repo, fake, capsys):
    fake.issue(KEY)["attachments"].append({"filename": 7, "size": "x"})
    assert run(fake, "--apply") == 1
    assert "could not be read safely" in capsys.readouterr().err and fake.writes() == []


@pytest.mark.parametrize("ref", ["OTHER-1", "spk-1", "SPK-01", "SPK-1;x", "0"])
def test_a_bad_key_is_a_usage_error(repo, capsys, ref):
    assert m.main(["done", ref, "--apply"], client_factory=no_jira) == 2


def test_an_issue_number_is_the_configured_project(repo, fake):
    assert run(fake, ref="1") == 0 and calls(fake) == READS


def test_an_empty_base_is_a_usage_error(repo, capsys):
    assert m.main(["done", KEY, "--base", ""], client_factory=no_jira) == 2


# ---- X2: --expect-plan (ported F22) ------------------------------------------------------------------------------------------------

def test_apply_refuses_when_plan_changed(repo, fake, capsys):
    assert run(fake, "--plan-out", "p.md") == 0
    assert "upload dod/DoD_SPK-1.md" in (repo / "p.md").read_text()
    attach_existing(fake, "DoD_SPK-1.md", READY.encode("utf-8"))  # someone attached it since the preview
    fake.log.clear()
    assert run(fake, "--apply", "--expect-plan", "p.md") == 2
    assert "plan changed" in capsys.readouterr().err and fake.writes() == []
    assert "skip dod/DoD_SPK-1.md" in (repo / "p.md.new").read_text()


def test_apply_proceeds_when_plan_matches(repo, fake):
    assert run(fake, "--plan-out", "p.md") == 0
    assert run(fake, "--apply", "--expect-plan", "p.md") == 0 and writes(fake) == [ATTACH, TRANSITION]


def test_expect_plan_needs_apply(repo, fake):
    with pytest.raises(SystemExit) as e:
        run(fake, "--expect-plan", "p.md")
    assert e.value.code == 2 and fake.log == []


# ---- strict mode runs the strict done check first (L5b; P4 refused "until L5b"): a finding is exit 2 with no request ----------------

STRICT = {"site": SITE, "project": "SPK", "mode": "strict", "strict": {"test_command": "python -m pytest"}}


def test_strict_mode_runs_the_strict_done_check_and_refuses_its_findings(repo, capsys):
    (repo / "skj.config.json").write_text(json.dumps(STRICT))
    assert m.main(["done", KEY], client_factory=no_jira) == 2  # this story has no record, no review, no profile: findings, no request
    cap = capsys.readouterr()
    rules = {f["rule"] for f in json.loads(cap.out)["findings"]}
    assert "record-missing" in rules and rules & {"review-missing", "review-blocks"}
    assert cap.err.startswith("skj: strict check failed: ")


def test_a_strict_target_branch_judges_whatever_the_working_tree_says(repo, capsys):
    (repo / "skj.config.json").write_text(json.dumps(STRICT))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "strict")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    (repo / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK"}))  # the PR lowers it: ignored
    assert m.main(["done", KEY], client_factory=no_jira) == 2
    assert "skj: strict check failed: " in capsys.readouterr().err


# ---- the plan text carries no people, no secrets, no raw server text ---------------------------------------------------------------

def test_plan_has_no_people_tokens_or_raw_server_text(repo, fake, capsys):
    t = _done_transition(fake)
    t["name"] = "Do‮ne\x07  now"
    assert run(fake, "--plan-out", "p.md") == 0
    text = (repo / "p.md").read_text()
    for bad in ("‮", "\x07", " ", fake.token, fake.email, fake.account_id):
        assert bad not in text
    assert "41 Done now" in text


# ---- linear time on adversarial ledgers --------------------------------------------------------------------------------------------

class _TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise _TooSlow


_BOUND = 2.0


def _timed(fn, *args):
    if not hasattr(signal, "setitimer"):
        pytest.skip("no setitimer")
    # CPU time, not the wall clock (H1b): load slows the clock, not the work; the timed calls run no subprocess and never block
    old = signal.signal(signal.SIGPROF, _alarm)
    signal.setitimer(signal.ITIMER_PROF, _BOUND)
    t0 = time.process_time()
    try:
        fn(*args)
    except _TooSlow:
        return _BOUND
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, old)
    return time.process_time() - t0


MIB = 1 << 20
SHAPES = {
    "1MiB-ticked-boxes": ledger(*["- [x] a"] * (MIB // 8)),
    "1MiB-unticked-boxes": ledger(*["- [ ] a"] * (MIB // 8)),
    "500x1990-blanks": ledger(*[" " * 1990] * 500),
    "500x1990-quotes": ledger(*[">" * 1990] * 500),
    "500x1990-dash-blanks": ledger(*["-" + " " * 1989] * 500),
    "500x1990-digits": ledger(*["1" * 1990] * 500),
    "500x1990-open-bracket": ledger(*["- [" * 663] * 500),
    "20000-lines": ledger(*["- [x] %d" % i for i in range(20000)]),
    "nested-fences": ledger(*(["````", "```", "- [ ] x", "```"] * 5000 + ["````"])),
    "comment-runs": ledger(*["<!-- - [ ] x -->" * 120] * 500),
    "one-line-1MiB": ledger("- [x] " + "a" * (MIB - 200)),
}


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_check_is_linear(name):
    md = SHAPES[name]
    assert len(md) <= MIB + 4096
    assert _timed(done.check, md) < _BOUND


@pytest.mark.parametrize("name", ["1MiB-unticked-boxes", "nested-fences", "500x1990-open-bracket"])
def test_plan_render_is_bounded_and_linear(name):
    chk = done.check(SHAPES[name])
    p = {"key": KEY, "ready": chk["ready"], "check": chk}
    assert _timed(done.render, p) < _BOUND
    assert len(done.render(p)) < 64 * 1024


# ---- DF1 (live run A9-16, D-018): an item rewritten as `[N/A]` or plain `N/A` is still an item, and unticked ----
@pytest.mark.parametrize("line", ["- [N/A] gates green", "- N/A gates green", "- [n/a] gates green", "- [xx] gates green",
                                  "- gates green", "1. [na] gates green", "> - N/A gates green"])
def test_an_item_that_is_no_ticked_box_is_unticked(line):
    r = done.check(ledger("- [x] a", "- [x] b", "- [x] c", "- [x] d", line))
    assert r["ready"] is False and r["boxes"] == 5 and len(r["unticked"]) == 1 and "gates green" in r["unticked"][0]


# ---- DF3 (review m2): nested evidence notes and thematic breaks are ordinary Markdown, not DoD items; D-018 holds at the box level ----
@pytest.mark.parametrize("extra", [["  - evidence: run 12"], ["\t- evidence: run 12", "    1. second note"], ["", "* * *"], ["- - -"],
                                   ["", "___"]])
def test_nested_notes_and_thematic_breaks_are_not_items(extra):
    r = done.check(ledger("- [x] gates green", *extra, "- [x] merged"))
    assert r["ready"] is True and r["unticked"] == [] and r["boxes"] == 2


def test_a_nested_unticked_box_and_a_top_level_na_still_count():
    assert done.check(ledger("- [x] gates green", "  - [ ] sub step"))["unticked"] == ["sub step"]
    assert done.check(ledger("- [x] gates green", "  - evidence", "- [N/A] merged"))["unticked"] == ["[N/A] merged"]


# ---- DF4 (re-review N4): an `[N/A]` or `N/A` line anywhere in section 5, at any indent, is not done (D-018 at every depth) ----
@pytest.mark.parametrize("lines", [["- [x] a", "  - [N/A] gates green"], ["- [x] a", ">   - [N/A] gates green"],
                                   ["1. [x] a", "   2. gates green N/A"], ["- [x] a", "    N/A gates green"],
                                   ["- [x] a", "\t- [n/a] gates green"], ["- [x] gates green N/A"]])
def test_df4_an_na_line_at_any_indent_is_not_done(lines):
    r = done.check(ledger(*lines))
    assert r["ready"] is False and any("gates green" in t for t in r["unticked"]), r


def test_df4_plain_nested_notes_stay_free_and_a_top_level_na_is_listed_once():
    assert done.check(ledger("- [x] gates green", "  - evidence: run 12 (NA-west region)"))["ready"] is True
    assert done.check(ledger("- [x] a", "- [N/A] merged"))["unticked"] == ["[N/A] merged"]


# ---- DF5 (re-review D3): structural rule: a deeper item that starts with a bracketed mark other than a ticked box, or any line saying
# not-applicable in any spelling (NFKC-folded), is not done; plain deeper notes stay free ----
@pytest.mark.parametrize("item", ["[NA] gates green", "[skip] gates green", "[n.a.] gates green", "[  ] gates green", "[N∕A] gates green",
                                  "[Ｎ/Ａ] gates green", "[N / A] gates green", "not applicable: gates green", "［x］ gates green",
                                  "[] gates green", "[N⁄A] gates green", "Not-Applicable gates green", "n.a. gates green"])
@pytest.mark.parametrize("indent", ["  - ", "\t- ", "    1. ", ">   - "])
def test_df5_a_deeper_item_with_any_non_box_mark_is_not_done(item, indent):
    r = done.check(ledger("- [x] a", indent + item))
    assert r["ready"] is False and any("gates green" in t for t in r["unticked"]), r


@pytest.mark.parametrize("note", ["  - evidence: run 12", "  - evidence: run 12 (NA-west region)", "    1. second note",
                                  "  - see [the log][log]", "  - nan/avg 0.1", "  - banana/apple", "  - N. Atlantic region",
                                  "  - 12 runs green", "  - Évidence: ok", "  - [x] sub step done"])
def test_df5_plain_deeper_notes_stay_free(note):
    assert done.check(ledger("- [x] gates green", note))["ready"] is True


# ---- DF6 (re-review R4, owner ruling: allow-list, fail closed): a deeper item is a free note only when, after NFKC and with format
# characters removed, its text starts with a letter or digit; anything else (a box glyph, `( )`, `\[ ]`, emphasis, a link) is an item.
# Deliberate: `  - [run 12](url) green` (DF5: free) is now an item too, since `  - [gates green](#)` looked the same ----
@pytest.mark.parametrize("item", ["\u2610 gates green", "\u2b1c gates green", "( ) gates green", "{x} gates green", "\\[ ] gates green",
                                  "**[ ]** gates green", "[gates green](#)", "~~gates green~~", "-[ ] gates green", "\u200b[ ] gates green",
                                  "[run 12](https://ci.example/run/12) gates green", ""])
@pytest.mark.parametrize("indent", ["  - ", "\t- ", "    1. ", ">   - "])
def test_df6_a_deeper_item_that_does_not_start_with_a_letter_or_digit_is_not_done(item, indent):
    r = done.check(ledger("- [x] a", indent + item))
    assert r["ready"] is False and len(r["unticked"]) == 1, r


@pytest.mark.parametrize("note", ["  - N\u200b/A gates green", "  - n/\u200ba gates green", "  - N\u2060/\ufeffA gates green",
                                  "  - evidence: N\u2215A", "  - evidence: n\u2044a"])
def test_df6_a_hidden_or_look_alike_na_in_a_note_is_not_done(note):
    """N2: the slash look-alikes (NFKC keeps U+2215 / U+2044) outside brackets; zero-width characters inside N/A."""
    assert done.check(ledger("- [x] gates green", note))["ready"] is False


# ---- DF5 (re-review D7): a ticked line holding N/A is listed as blocking, without its tick mark ----
def test_df5_a_ticked_na_line_is_listed_without_its_mark():
    r = done.check(ledger("- [x] a", "- [x] gates green - evidence: coverage N/A"))
    assert r["unticked"] == ["gates green - evidence: coverage N/A (marked not applicable: not done)"]
