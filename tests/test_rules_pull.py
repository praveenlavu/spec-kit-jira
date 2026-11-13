"""RU2: the parent walk and `skj rules <ref>` (GETs only; every GET before any file; fixed texts; keys, counts and hashes in JSON),
and `skj dod` writing `Rules from:` from the same walk. Rebased from the fork's pull tests: HTML descriptions become ADF, integer ids
become keys, the hierarchy link becomes `fields.parent`. The fake's parent chains (an Epic above a Task above a Story, a 5-level
chain, cycles) are HAND-WRITTEN: a team-managed Free site has Epic > Story > Subtask only, and how Jira returns `fields.parent`
for deeper or company-managed hierarchies is not checked (docs/unverified.md U22, U23). All data is fictional."""
import json
import os
import subprocess

import pytest

from fakejira import FakeJira
from skj import decisions, dod, rules
from skj.__main__ import main
from skj.client import Client, ClientError
from skj.config import Config
from skj.issues import KeyMismatch

pytestmark = pytest.mark.rules

SITE = "example-site.atlassian.net"
TYPES = ["Epic", "Task"]


def para(s):
    return {"type": "paragraph", "content": [{"type": "text", "text": s}]}


def doc(*lines):
    return {"type": "doc", "version": 1, "content": [x if isinstance(x, dict) else para(x) for x in lines]}


def rule_lines(*specs):
    """A Business rules section: (n, status) per rule, every label present (no warnings)."""
    out = ["Business rules"]
    for n, s in specs:
        out += ["Rule %d: Customers earn %d points per euro." % (n, n), "Why: loyalty", "Example: spend 10", "Owner: Loyalty product owner",
                "Status: " + s]
    return out


def desc(*specs, extra=()):
    return doc(*rule_lines(*specs), *extra)


AC_DOC = doc({"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Acceptance criteria"}]},
             {"type": "bulletList", "content": [{"type": "listItem", "content": [para("Points are shown at checkout")]}]})


def chain(epic=None, task=None, story=AC_DOC, task_type="Task"):
    """LOY-1 Epic (rules 1 approved, 3 retired) <- LOY-2 Task (1 approved, 2 draft) <- LOY-3 Story."""
    f = FakeJira(project="LOY")
    f.add_issue("Loyalty programme", type="Epic", description=desc((1, "approved"), (3, "retired")) if epic is None else epic)
    f.add_issue("Secret task title", type=task_type, parent="LOY-1", description=desc((1, "approved"), (2, "draft")) if task is None else task)
    f.add_issue("Award points at checkout", type="Story", parent="LOY-2", description=story)
    return f


def set_parent(f, child, parent):
    f.issue(child)["parent"] = f.issue(parent)["id"] if parent else None


def cfg(types=TYPES):
    return Config(SITE, "LOY", rules={"enabled": True, "types": types})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, stdin=subprocess.DEVNULL)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rules, "_today", lambda: "2026-11-03")
    write_cfg(tmp_path, {"enabled": True, "types": TYPES})
    return tmp_path


def write_cfg(root, rules_cfg):
    data = {"site": SITE, "project": "LOY"}
    if rules_cfg is not None:
        data["rules"] = rules_cfg
    (root / "skj.config.json").write_text(json.dumps(data))


def run(f, capsys, *args):
    code = main(["rules", *args], client_factory=f.client)
    cap = capsys.readouterr()
    return code, (json.loads(cap.out) if cap.out.strip() else None), cap.err


def gets(f):
    return [(e["method"], e["path"]) for e in f.log]


def files(root):
    return sorted(p.name for p in root.rglob("Rules_*") if p.is_file() or p.is_symlink())


# ---- walk ----

def test_walk_story_task_epic():
    f = chain()
    got = rules.walk(f.client(), "LOY-3", TYPES)
    assert [(g["key"], g["type"]) for g in got] == [("LOY-2", "Task"), ("LOY-1", "Epic")]
    assert got[0]["issue"]["key"] == "LOY-2" and got[0]["parsed"]["found"]
    assert [r["id"] for r in got[0]["parsed"]["rules"]] == ["BR-LOY-2-1", "BR-LOY-2-2"]
    assert [r["id"] for r in got[1]["parsed"]["rules"]] == ["BR-LOY-1-1", "BR-LOY-1-3"]
    assert gets(f) == [("GET", "/issue/LOY-3"), ("GET", "/issue/LOY-2"), ("GET", "/issue/LOY-1")]
    assert f.log[0]["query"]["fields"] == "issuetype,parent" and f.log[1]["query"]["fields"] == "summary,issuetype,parent,description"


def test_walk_skips_unlisted_types_and_ancestors_without_section():
    f = chain(epic=doc("Just a description, no rules here."))
    assert [g["key"] for g in rules.walk(f.client(), "LOY-3", TYPES)] == ["LOY-2"]  # the Epic has no section: not in `from`
    assert [g["key"] for g in rules.walk(f.client(), "LOY-3", ["task", "EPIC"])] == ["LOY-2"]  # types are case-folded
    assert rules.walk(f.client(), "LOY-3", ["Epic"]) == []  # the Task is walked through, never kept
    f = chain()
    assert rules.walk(f.client(), "LOY-1", TYPES) == []  # the story itself never counts, even an Epic
    f = chain(epic=None)
    f.issue("LOY-1")["description"] = None  # no description at all
    assert [g["key"] for g in rules.walk(f.client(), "LOY-3", TYPES)] == ["LOY-2"]


def test_walk_keeps_an_ancestor_whose_problem_would_otherwise_vanish():
    deep = {"type": "paragraph", "content": []}
    node = deep
    for _ in range(rules.MAX_DEPTH):  # nesting past the limit: nothing is read, which must not pass as "no section"
        child = {"type": "blockquote", "content": []}
        node["content"].append(child)
        node = child
    f = chain(epic=doc(*rule_lines((1, "approved")), deep))
    got = rules.walk(f.client(), "LOY-3", ["Epic"])
    assert [g["key"] for g in got] == ["LOY-1"] and not got[0]["parsed"]["found"]
    assert [p["issue"] for p in got[0]["parsed"]["problems"]] == ["unreadable-content"]


def test_walk_depth_cap_is_3_and_the_4th_level_is_never_fetched():
    """RU2-AC2."""
    f = FakeJira(project="LOY")
    f.add_issue("top", type="Epic", description=desc((1, "approved")))  # LOY-1: the 5th issue up from the story
    for n in range(4):  # LOY-2..LOY-5, each the parent of the next
        f.add_issue("level", type="Epic", parent="LOY-%d" % (n + 1), description=desc((1, "approved")))
    f.add_issue("story", type="Story", parent="LOY-5", description=AC_DOC)  # LOY-6
    got = rules.walk(f.client(), "LOY-6", TYPES)
    assert [g["key"] for g in got] == ["LOY-5", "LOY-4", "LOY-3"]
    assert gets(f) == [("GET", "/issue/LOY-%d" % n) for n in (6, 5, 4, 3)]  # the story + 3 links; LOY-2 is never fetched
    assert rules.MAX_LINKS == 3


def test_parent_cycle_stops():
    """RU2-AC1: a cycle or a self-parent stops the walk with an error before the repeated issue is fetched again."""
    f = chain()
    set_parent(f, "LOY-1", "LOY-3")  # the Epic's parent is the story: LOY-3 -> LOY-2 -> LOY-1 -> LOY-3
    with pytest.raises(ValueError, match="parent chain of LOY-3 loops back to LOY-3"):
        rules.walk(f.client(), "LOY-3", TYPES)
    assert len(f.log) == 3
    f = chain()
    set_parent(f, "LOY-1", "LOY-2")  # LOY-2 <-> LOY-1, above the story
    with pytest.raises(ValueError, match="loops back to LOY-2"):
        rules.walk(f.client(), "LOY-3", TYPES)
    assert len(f.log) == 3
    f = chain()
    set_parent(f, "LOY-3", "LOY-3")  # a self-parent story: nothing above it is fetched
    with pytest.raises(ValueError, match="loops back to LOY-3"):
        rules.walk(f.client(), "LOY-3", TYPES)
    assert gets(f) == [("GET", "/issue/LOY-3")]
    f = chain()
    set_parent(f, "LOY-2", "LOY-2")  # a self-parent ancestor
    with pytest.raises(ValueError, match="loops back to LOY-2"):
        rules.walk(f.client(), "LOY-3", TYPES)
    assert len(f.log) == 2


def test_parent_cycle_is_a_refusal_with_nothing_written(repo, capsys):
    f = chain()
    set_parent(f, "LOY-1", "LOY-3")
    code, out, err = run(f, capsys, "LOY-3")
    assert code == 2 and out is None and "loops back to LOY-3" in err and "nothing written" in err
    assert files(repo) == [] and not (repo / "dod").exists() and f.writes() == []


@pytest.mark.parametrize("parent", [{"key": "loy-1"}, {"key": "LOY-01"}, {"key": "LOY-1\n"}, {"key": "../LOY-1"}, {"key": ""}, {"key": 1},
                                    {"id": "10000"}, {}, "LOY-1", ["LOY-1"], 12, True])
def test_walk_bad_parent_field_stops(parent, monkeypatch):
    f = chain()
    monkeypatch.setattr(f, "_ref", lambda issue: parent)
    problems = []
    assert rules.walk(f.client(), "LOY-3", TYPES, problems) == [] and len(f.log) == 1  # nothing else is fetched
    assert problems == [{"key": "LOY-3", "rule": None, "level": "error", "issue": "bad-parent-link"}]
    assert "bad-parent-link" in rules.WALK_ISSUES


def test_walk_a_moved_ancestor_or_story_is_refused(monkeypatch):
    f = chain()
    f.move_issue("LOY-2", "LOY-9")
    assert [g["key"] for g in rules.walk(f.client(), "LOY-3", TYPES)] == ["LOY-9", "LOY-1"]  # the parent field names the new key
    real = f._ref
    monkeypatch.setattr(f, "_ref", lambda issue: dict(real(issue), key="LOY-2") if issue["key"] == "LOY-9" else real(issue))
    with pytest.raises(KeyMismatch):  # a stale parent key whose GET answers for another key
        rules.walk(f.client(), "LOY-3", TYPES)
    f = chain()
    f.move_issue("LOY-3", "LOY-8")
    with pytest.raises(KeyMismatch):
        rules.walk(f.client(), "LOY-3", TYPES)
    with pytest.raises(ValueError):
        rules.walk(chain().client(), "loy-3", TYPES)


def test_walk_hostile_descriptions_degrade_to_a_named_problem():
    bidi = doc(*rule_lines((1, "approved"))[:2], "Why: ‮evil‬ loyalty\u0000\u0007", *rule_lines((1, "approved"))[3:])
    big = doc("Business rules", "Rule 1: " + "x" * (rules.MAX_RAW + 10))
    many = {"type": "doc", "version": 1, "content": [{"type": "nope", "content": [{"type": "x"}] * 50}] * 2000}
    for d, want in ((big, ["section-too-big"]), (many, ["unreadable-content"]), ("not ADF", ["unreadable-content"]),
                    (["Business rules"], ["unreadable-content"])):
        got = rules.walk(chain(epic=d).client(), "LOY-3", ["Epic"])
        assert [g["key"] for g in got] == ["LOY-1"] and [p["issue"] for p in got[0]["parsed"]["problems"]] == want
    got = rules.walk(chain(epic=bidi).client(), "LOY-3", ["Epic"])
    body = got[0]["parsed"]["rules"][0]["body"]
    assert "Why: evil loyalty" in body and not any(c in "".join(body) for c in "‮‬\u0000\u0007")


# ---- the verb ----

def test_pull_is_get_only(repo, capsys):
    """RU2-AC3: GETs only, no body, no write of any kind (the fake records every method)."""
    f = chain()
    code, out, _ = run(f, capsys, "3")
    assert code == 0 and files(repo) == ["Rules_LOY-1.md", "Rules_LOY-2.md"]
    assert gets(f) == [("GET", "/issue/LOY-%d" % n) for n in (3, 2, 1)]
    assert {e["method"] for e in f.log} == {"GET"} and f.writes() == []


def test_json_shape_and_files(repo, capsys):
    code, out, _ = run(chain(), capsys, "LOY-3")
    assert code == 0 and list(out) == ["from", "files", "rules", "problems", "changed", "decisions", "notes"]
    assert out["from"] == ["LOY-2", "LOY-1"] and out["changed"] == ["LOY-2", "LOY-1"] and out["decisions"] == []
    assert out["files"] == ["dod/Rules_LOY-2.md", "dod/Rules_LOY-1.md"]
    assert out["rules"] == {"approved": 2, "draft": 1, "retired": 1} and out["problems"] == [] and out["notes"] == 0
    text = (repo / "dod" / "Rules_LOY-2.md").read_text(encoding="utf-8")
    pf = rules.parse_file(text, "LOY-2")  # the kit's own file reads back, with the walk's rules in it
    assert not pf["invalid"] and pf["format"] == rules.FORMAT and sorted(pf["rules"]) == ["BR-LOY-2-1", "BR-LOY-2-2"]
    assert pf["rules"]["BR-LOY-2-1"]["status"] == "approved" and pf["rules"]["BR-LOY-2-1"]["body_hash"] == pf["rules"]["BR-LOY-2-1"]["hash"]
    assert "Last pulled:** 2026-11-03" in text and "- **Jira:** https://%s/browse/LOY-2" % SITE in text
    assert "re-pull with `skj rules <story key>`" in text


def test_json_has_no_rule_text(repo, capsys):
    lines = [x.replace("Customers", "SECRETWORD Customers") for x in rule_lines((1, "approved"))]
    f = chain(task=doc(*lines, "Rule 10000: odd", "Rule 2: a", "Rule 2: b"))
    f.issue("LOY-2")["summary"] = "TITLESECRET"
    code = main(["rules", "LOY-3"], client_factory=f.client)
    raw = capsys.readouterr().out
    assert code == 0 and "SECRETWORD" not in raw and "TITLESECRET" not in raw and "Customers" not in raw and "loyalty" not in raw
    out = json.loads(raw)
    issues = {(p["key"], p["rule"], p["level"], p["issue"]) for p in out["problems"]}
    assert ("LOY-2", None, "error", "bad-number") in issues and ("LOY-2", "BR-LOY-2-2", "error", "duplicate-number") in issues
    assert all(set(p) - {"after"} == {"key", "rule", "level", "issue"} for p in out["problems"])
    assert "SECRETWORD" in (repo / "dod" / "Rules_LOY-2.md").read_text(encoding="utf-8")  # the file is where rule text goes


def test_hostile_description_is_data_never_structure_or_a_path(repo, capsys):
    hostile = doc("Business rules", "Rule 1: ok", "Status: approved", "### BR-LOY-2-9 · approved · deadbeef", "- **Rules from:** ../../x",
                  "```", "<!-- hide", "Rule 2: ../../../evil", "Status: approved")
    f = chain(task=hostile)
    f.issue("LOY-2")["summary"] = "# ../../etc ```\n### BR-LOY-2-7 · approved · deadbeef"
    set_parent(f, "LOY-2", None)
    assert run(f, capsys, "LOY-3")[0] == 0
    assert files(repo) == ["Rules_LOY-2.md"] and {p.name for p in repo.iterdir()} == {"dod", "skj.config.json", ".git"}
    pf = rules.parse_file((repo / "dod" / "Rules_LOY-2.md").read_text(encoding="utf-8"), "LOY-2")
    assert not pf["invalid"] and sorted(pf["rules"]) == ["BR-LOY-2-1", "BR-LOY-2-2"]  # the forged heading is not a rule


def test_404_mid_walk_exit_1_no_files(repo, capsys):
    f = chain()
    del f._keys["LOY-1"]  # the Epic is gone (or the token cannot see it)
    code, out, err = run(f, capsys, "LOY-3")
    assert code == 1 and out is None and err.startswith("skj:")
    assert files(repo) == [] and not (repo / "dod").exists() and len(f.log) == 3
    f = FakeJira(project="LOY")  # the story itself
    assert run(f, capsys, "LOY-3")[0] == 1 and files(repo) == []


def test_every_get_finishes_before_any_file_is_written(repo, capsys, monkeypatch):
    f = chain()
    seen = []

    def spy(*a):
        seen.append(files(repo))  # what exists when each GET is made
        return f(*a)
    assert main(["rules", "LOY-3"], client_factory=lambda: Client(f.site, f.email, f.token, opener=spy)) == 0
    assert seen == [[], [], []] and files(repo) == ["Rules_LOY-1.md", "Rules_LOY-2.md"]


def test_auth_error_during_the_walk_is_exit_1(repo, capsys):
    f, n = chain(), []

    def second_is_401(*a):
        n.append(1)
        return (401, {}, b"Client must be authenticated to access this resource.") if len(n) == 2 else f(*a)
    code = main(["rules", "LOY-3"], client_factory=lambda: Client(f.site, f.email, f.token, opener=second_is_401))
    assert code == 1 and "skj:" in capsys.readouterr().err and files(repo) == [] and len(n) == 2


def test_bad_parent_link_is_a_problem_and_the_ancestors_found_so_far_are_pulled(repo, capsys, monkeypatch):
    f = chain()
    real = f._ref
    monkeypatch.setattr(f, "_ref", lambda issue: {"key": "12ab"} if issue["key"] == "LOY-1" else real(issue))  # LOY-2's parent
    code, out, _ = run(f, capsys, "LOY-3")
    assert code == 0 and out["from"] == ["LOY-2"] and len(f.log) == 2
    assert {"key": "LOY-2", "rule": None, "level": "error", "issue": "bad-parent-link"} in out["problems"]
    assert files(repo) == ["Rules_LOY-2.md"]


def test_no_ancestor_with_a_section_writes_nothing(repo, capsys):
    f = chain()
    set_parent(f, "LOY-3", None)
    code, out, _ = run(f, capsys, "LOY-3")
    assert code == 0 and out == {"from": [], "files": [], "rules": {"approved": 0, "draft": 0, "retired": 0}, "problems": [], "changed": [],
                                 "decisions": [], "notes": 0}
    assert files(repo) == [] and not (repo / "dod").exists()


def test_rerun_without_jira_change_rewrites_nothing(repo, capsys, monkeypatch):
    assert run(chain(), capsys, "LOY-3")[0] == 0
    f1, f2 = repo / "dod" / "Rules_LOY-2.md", repo / "dod" / "Rules_LOY-1.md"
    first = (f1.read_bytes(), f2.read_bytes())
    ino = (f1.stat().st_ino, f2.stat().st_ino)
    monkeypatch.setattr(rules, "_today", lambda: "2027-01-09")  # another day: only the `pulled` stamp would differ
    code, out, _ = run(chain(), capsys, "LOY-3")
    assert code == 0 and out["changed"] == [] and out["from"] == ["LOY-2", "LOY-1"] and len(out["files"]) == 2
    assert (f1.read_bytes(), f2.read_bytes()) == first and (f1.stat().st_ino, f2.stat().st_ino) == ino  # not even replaced
    assert b"2026-11-03" in first[0] and b"2027-01-09" not in first[0]
    # Jira changed one rule: that file is rewritten, with the new date; the other is left alone
    code, out, _ = run(chain(task=desc((1, "approved"), (2, "approved"))), capsys, "LOY-3")
    assert out["changed"] == ["LOY-2"] and f2.read_bytes() == first[1] and b"2027-01-09" in f1.read_bytes()
    assert out["rules"] == {"approved": 3, "draft": 0, "retired": 1}


def test_a_hand_edited_file_is_replaced_and_crlf_is_kept(repo, capsys):
    run(chain(), capsys, "LOY-3")
    f = repo / "dod" / "Rules_LOY-2.md"
    good = f.read_bytes()
    f.write_bytes(good.replace(b"approved \xc2\xb7", b"retired \xc2\xb7", 1))  # a PR edit
    code, out, _ = run(chain(), capsys, "LOY-3")
    assert out["changed"] == ["LOY-2"] and f.read_bytes() == good
    f.write_bytes(good.replace(b"\n", b"\r\n"))
    code, out, _ = run(chain(), capsys, "LOY-3")
    assert out["changed"] == [] and f.read_bytes() == good.replace(b"\n", b"\r\n")  # same content, its own terminator: untouched


def test_existing_symlink_or_non_file_is_refused_and_nothing_is_written(repo, capsys):
    (repo / "dod").mkdir()
    (repo / "elsewhere.md").write_text("keep me")
    os.symlink(repo / "elsewhere.md", repo / "dod" / "Rules_LOY-1.md")
    code, out, err = run(chain(), capsys, "LOY-3")
    assert code == 2 and out is None and err.startswith("skj:") and "elsewhere" not in err
    assert (repo / "elsewhere.md").read_text() == "keep me" and files(repo) == ["Rules_LOY-1.md"]  # no Rules_LOY-2.md either
    assert (repo / "dod" / "Rules_LOY-1.md").is_symlink()
    (repo / "dod" / "Rules_LOY-1.md").unlink()
    (repo / "dod" / "Rules_LOY-1.md").mkdir()  # a directory in the way
    assert run(chain(), capsys, "LOY-3")[0] == 2 and not (repo / "dod" / "Rules_LOY-2.md").exists()


def test_unwritable_dod_exit_2(repo, capsys):
    (repo / "dod").write_text("a file where the directory should be")
    code, out, err = run(chain(), capsys, "LOY-3")
    assert code == 2 and out is None and err.startswith("skj:")
    assert (repo / "dod").read_text() == "a file where the directory should be"
    (repo / "dod").unlink()
    (repo / "real").mkdir()
    os.symlink(repo / "real", repo / "dod")  # dod/ as a link: refused, nothing written through it
    assert run(chain(), capsys, "LOY-3")[0] == 2 and list((repo / "real").iterdir()) == []


def test_a_failed_write_is_one_fixed_text(repo, capsys, monkeypatch):
    def boom(path, text):
        raise OSError("disk full at /secret/place")
    monkeypatch.setattr(decisions, "_atomic_write", boom)
    code, out, err = run(chain(), capsys, "LOY-3")
    assert code == 2 and "could not write the rules file of LOY-2" in err and "secret" not in err
    monkeypatch.setattr(decisions, "_atomic_write", lambda path, text: (path.parent.mkdir(exist_ok=True), path.write_text(text[:10])))  # a short write: read back, refused
    assert run(chain(), capsys, "LOY-3")[0] == 2


def test_ancestor_decisions_listed_with_counts(repo, capsys):
    (repo / "dod").mkdir()
    (repo / "dod" / "Decisions_LOY-2.md").write_text(
        decisions.render_file("LOY-2")
        + "- [ ] D1: Ask the PO → _(open)_ · kind: decision · why: w\n- [ ] D2: Second → _(open)_ · kind: assumption · why: w\n"
        + "- [x] D3: Skip audit → rejected · by: PO · note: no\n- [x] D4: Cap → validated · by: PO · note: ok\n", encoding="utf-8")
    (repo / "dod" / "Decisions_LOY-3.md").write_text(decisions.render_file("LOY-3") + "- [ ] D1: a story one → _(open)_ · kind: decision · why: w\n")
    code, out, _ = run(chain(), capsys, "LOY-3")
    assert code == 0 and out["decisions"] == [{"key": "LOY-2", "open": 2, "rejected": 1}]  # only a kept ancestor's file; none for the Epic
    assert "Ask the PO" not in json.dumps(out)
    # a symlinked decisions file is refused before anything is written
    (repo / "dod" / "Decisions_LOY-1.md").symlink_to(repo / "dod" / "Decisions_LOY-2.md")
    for p in repo.glob("dod/Rules_*"):
        p.unlink()
    assert run(chain(), capsys, "LOY-3")[0] == 2 and files(repo) == []


def test_decisions_add_works_for_an_epic_key(repo, capsys):
    """The existing local verb takes any key of the project: a decision that spans stories is kept on the Epic."""
    assert main(["decisions", "LOY-1", "--add", "Points expire after 12 months", "--kind", "decision", "--why", "the PO said so"]) == 0
    capsys.readouterr()
    md = (repo / "dod" / "Decisions_LOY-1.md").read_text(encoding="utf-8")
    assert decisions.parse_decisions(md)["open"][0]["text"] == "Points expire after 12 months"


@pytest.mark.parametrize("rules_cfg", [None, {"enabled": False}, {"types": ["Epic"]}, {"enabled": True, "types": TYPES}])
def test_works_whatever_rules_enabled_says(repo, capsys, rules_cfg):
    write_cfg(repo, rules_cfg)
    f = chain()
    code, out, _ = run(f, capsys, "LOY-3")
    want = ["LOY-2", "LOY-1"] if (rules_cfg or {}).get("types") == TYPES else ["LOY-1"]  # the default types are ["Epic"]
    assert code == 0 and out["from"] == want and files(repo) == sorted("Rules_%s.md" % k for k in want)
    assert {e["method"] for e in f.log} == {"GET"}


def test_two_ancestors_problems_can_be_told_apart(repo, capsys):
    f = chain(epic=desc((1, "approved"), extra=["Rule 10000: x"]), task=desc((1, "approved"), extra=["Rule 10000: x"]))
    out = run(f, capsys, "LOY-3")[1]
    assert [(p["key"], p["issue"]) for p in out["problems"]] == [("LOY-2", "bad-number"), ("LOY-1", "bad-number")]


def test_json_carries_locators_and_the_notes_count(repo, capsys):
    lines = ["Business rules", "Intro ZZINTRO prose.", "R1: ZZSECRET", "Status: approved"] + rule_lines((1, "approved"))[1:] + [
        "Rule 2 - ZZSECRET", "Status approved"]
    code = main(["rules", "LOY-3"], client_factory=chain(task=doc(*lines)).client)
    raw = capsys.readouterr().out
    out = json.loads(raw)
    assert code == 0 and "ZZSECRET" not in raw and "ZZINTRO" not in raw and out["notes"] == 1
    probs = [(p["issue"], p["after"]) for p in out["problems"] if "after" in p]
    assert probs == [("unparsed-rule-text", rules.FIRST), ("bad-rule-line", "BR-LOY-2-1"), ("bad-status-line", "BR-LOY-2-1")]
    assert all(p["rule"] is None and p["level"] == "error" and p["key"] == "LOY-2" for p in out["problems"] if "after" in p)
    text = (repo / "dod" / "Rules_LOY-2.md").read_text(encoding="utf-8")
    assert "## Notes" in text and "ZZSECRET" not in text and "ZZINTRO" not in text
    pf = rules.parse_file(text, "LOY-2")
    assert not pf["invalid"] and pf["notes"] == [rules.NOTE_INTRO] and sorted(pf["rules"]) == ["BR-LOY-2-1"]


def test_cli_rules_exit_codes(repo, capsys):
    assert main(["rules", "LOY-3"], client_factory=chain().client) == 0
    capsys.readouterr()
    for argv in (["rules"], ["rules", "LOY-3", "--bogus"], ["rules", "LOY-3", "LOY-2"]):
        with pytest.raises(SystemExit) as e:
            main(argv, client_factory=chain().client)
        assert e.value.code == 2, argv
    for ref in ("abc", "0", "-3", "OTHER-3", "loy-3", "LOY-03"):
        f = chain()
        assert main(["rules", ref], client_factory=f.client) == 2 and f.log == [], ref  # refused before any request
    capsys.readouterr()
    f = chain()
    f.inject(403, body={"errorMessages": ["down"], "errors": {}})
    assert main(["rules", "LOY-3"], client_factory=f.client) == 1
    assert "skj:" in capsys.readouterr().err


def test_main_help_lists_the_verb(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "rules" in capsys.readouterr().out


# ---- `skj dod` writes `Rules from:` from the same walk (the fork's dod rules tests) ----

def _ledger(root):
    return (root / "dod" / "DoD_LOY-3.md").read_text(encoding="utf-8")


def _dod(f, *args):
    return main(["dod", *args, "LOY-3"], client_factory=f.client)


@pytest.mark.parametrize("rules_cfg", [None, {"enabled": False}, {"enabled": False, "types": TYPES}])
def test_disabled_dod_byte_identical_and_no_extra_get(repo, rules_cfg):
    write_cfg(repo, rules_cfg)
    f = chain()
    assert _dod(f, "seed") == 0 and len(f.log) == 1  # the story only: the walk is never made when disabled
    md = _ledger(repo)
    want = dod.render(f.client().request("GET", "/issue/LOY-3", params={"fields": ",".join(dod.FIELDS)}).data,
                      dod.ac_section(AC_DOC, "Acceptance criteria"), SITE, md.split("Last synced:** ")[1].splitlines()[0])
    assert md == want and "Rules from" not in md and "Rules reviewed" not in md
    assert _dod(f, "refresh") == 0 and _ledger(repo) == md


def test_enabled_dod_writes_rules_from(repo):
    f = chain()
    assert _dod(f, "seed") == 0
    assert {e["method"] for e in f.log} == {"GET"} and f.writes() == [] and len(f.log) == 4  # the story, then the walk
    md = _ledger(repo)
    assert "- **Spec dir:** _(set by speckit.jira-lifecycle.pull)_\n- **Rules from:** LOY-2, LOY-1\n\n## 1. " in md
    assert dod.ledger_rules_from(md) == {"present": True, "valid": True, "ids": ["LOY-2", "LOY-1"]}
    assert dod.ledger_rules_reviewed(md) == {"present": False, "valid": True, "hashes": {}}
    (repo / "dod" / "DoD_LOY-3.md").unlink()
    assert _dod(chain(epic=doc("no rules"), task=doc("none either")), "seed") == 0  # no ancestor with a section: `none`
    assert dod.ledger_rules_from(_ledger(repo)) == {"present": True, "valid": True, "ids": []}
    assert "- **Rules from:** none\n" in _ledger(repo)


def test_refresh_recomputes_rules_from_and_keeps_rules_reviewed(repo):
    reviewed = "BR-LOY-2-1@3f2a9c1b, BR-LOY-1-1@0c77e2d4"
    assert _dod(chain(), "seed") == 0
    p = repo / "dod" / "DoD_LOY-3.md"
    p.write_bytes(p.read_bytes().replace(b"- **Rules from:** LOY-2, LOY-1", ("- **Rules from:** LOY-7\n- **Rules reviewed:** %s" % reviewed).encode()))
    p.write_bytes(p.read_bytes().replace(b"_(fill in)_", b"MY REAL PLAN", 1))
    assert _dod(chain(), "refresh") == 0
    md = _ledger(repo)
    assert dod.ledger_rules_from(md)["ids"] == ["LOY-2", "LOY-1"] and "MY REAL PLAN" in md  # the stale value is recomputed
    assert "\n- **Rules reviewed:** %s\n" % reviewed in md and md.count("Rules reviewed") == 1
    f = chain()
    set_parent(f, "LOY-2", None)  # the walk changed (the Epic is no longer above): recomputed, not kept
    assert _dod(f, "refresh") == 0
    md2 = _ledger(repo)
    assert dod.ledger_rules_from(md2)["ids"] == ["LOY-2"] and "\n- **Rules reviewed:** %s\n" % reviewed in md2
    write_cfg(repo, {"enabled": False})  # rules off: no Rules from line, Rules reviewed kept, no walk GET
    f = chain()
    assert _dod(f, "refresh") == 0 and len(f.log) == 1
    md3 = _ledger(repo)
    assert "Rules from" not in md3 and "\n- **Rules reviewed:** %s\n" % reviewed in md3


@pytest.mark.parametrize("refresh", [False, True])
def test_ancestor_get_failure_exit_1_ledger_untouched(repo, refresh, capsys):
    if refresh:
        assert _dod(chain(), "seed") == 0
        old = _ledger(repo)
    f = chain()
    del f._keys["LOY-1"]
    assert _dod(f, "refresh" if refresh else "seed") == 1 and "skj:" in capsys.readouterr().err
    if refresh:
        assert _ledger(repo) == old and [x.name for x in (repo / "dod").iterdir()] == ["DoD_LOY-3.md"]
    else:
        assert not (repo / "dod").exists()


def test_dod_with_a_parent_cycle_stops_and_writes_nothing(repo, capsys):
    """RU2-AC1 through `skj dod`: the same walk, the same refusal; no ledger, and no write to Jira."""
    f = chain()
    set_parent(f, "LOY-1", "LOY-3")
    assert _dod(f, "seed") == 1 and "loops back to LOY-3" in capsys.readouterr().err
    assert not (repo / "dod").exists() and f.writes() == []


def test_walk_with_a_stub_client_never_sends_anything_but_get():
    """RU2-AC3 at the client seam: every call the walk makes is a GET with no body (a recording stand-in for the client)."""
    f = chain()
    real, calls = f.client(), []

    class Recorder:
        def request(self, method, path, **kw):
            calls.append((method, path, kw.get("body")))
            if method != "GET":
                raise AssertionError("a write in the pull")
            return real.request(method, path, **kw)
    rules.walk(Recorder(), "LOY-3", TYPES)
    assert [c[0] for c in calls] == ["GET"] * 3 and all(c[2] is None for c in calls)
    with pytest.raises(ClientError):
        rules.walk(FakeJira(project="LOY").client(), "LOY-3", TYPES)  # a missing story: the client's error, nothing caught
