"""L3: the gate. Ported from the fork source's tests/test_gate.py (story id -> key `SPK-<n>`, work item HTML -> the story's
ADF description, the numeric story reference -> the key). The rules-specific cases are in tests/test_rules_coverage.py and
tests/test_rules_drift.py (RU3); here only the baseline with rules off and the switch. All data
is fictional: keys SPK-n, site example-site.atlassian.net."""
import argparse
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from skj import config, gate, rules
from skj.config import Config
from skj.decisions import render_file
from skj.dod import SECTION2, ac_section, render
from skj.gate import (DEFAULT_TESTS, check_commits, check_drift, check_smart_commits, check_traceability, discover_tests,
                      missing_evidence, parse_section3, render_summary, run_gate, scan_assertions, test_present)

SITE = "example-site.atlassian.net"
HEADING = "Acceptance criteria"
KEY = "SPK-7"
HDR = "| AC | acceptance criterion | tests | commit/PR | status |\n|---|---|---|---|---|\n"
LEDGER = f"""# DoD — SPK-1001
{SECTION2}
## 3. AC → tests (traceability)
{HDR}| AC-1 | does X | FooTest.testX | abc123 | green |
| AC-2 | does Y | BarTest.testY, BarTest.testY2 |  | red |
## 4. Gate log
"""
APEX = "@IsTest private class FooTest { @IsTest static void testX(){ System.assert(true); } }"
SOURCES = {"FooTest.cls": APEX}


def t(s, *marks):
    n = {"type": "text", "text": s}
    if marks:
        n["marks"] = [{"type": m} for m in marks]
    return n


def p(*parts):
    return {"type": "paragraph", "content": [t(x) if isinstance(x, str) else x for x in parts]}


def h(level, text):
    return {"type": "heading", "attrs": {"level": level}, "content": [t(text)]}


def ul(*items):
    return {"type": "bulletList", "content": [{"type": "listItem", "content": [p(i) if isinstance(i, str) else i]} for i in items]}


def ol(*items):
    return {"type": "orderedList", "content": [{"type": "listItem", "content": [p(i)]} for i in items]}


def _desc(ac):
    return {"version": 1, "type": "doc", "content": [p("Context."), h(2, HEADING), *([p(ac)] if isinstance(ac, str) else ac)]}


def _wi(ac):
    """The story as `issues.get` returns it (the fork's `_wi` work item)."""
    return {"key": KEY, "id": "10007", "fields": {"summary": "T", "issuetype": {"name": "Story"}, "status": {"name": "To Do"},
                                                  "description": _desc(ac)}}


def _ledger_for(ac):
    i = _wi(ac)
    return render(i, ac_section(i["fields"]["description"], HEADING), SITE, synced="2026-07-14")


@pytest.fixture
def cfg():
    return Config(SITE, "SPK")


# ---- ported graceful-degradation cases ----
def test_parse_section3_extracts_rows():
    assert parse_section3(LEDGER) == [
        {"ac": "AC-1", "tests": ["FooTest.testX"], "evidence": "abc123", "status": "green"},
        {"ac": "AC-2", "tests": ["BarTest.testY", "BarTest.testY2"], "evidence": "", "status": "red"},
    ]


def test_parse_section3_graceful_on_missing_or_placeholder():
    assert parse_section3("no section 3 here") == []
    row = f"{SECTION2}\n## 3. AC\n| AC-1 | x | _(test IDs, …)_ | _(commit / PR)_ | _(red→green)_ |"
    assert parse_section3(row) == [{"ac": "AC-1", "tests": [], "evidence": "", "status": ""}]


def test_check_traceability_maps_and_flags():
    r = check_traceability(parse_section3(LEDGER), SOURCES)
    assert r["evaluated"] is True
    assert r["mapped"] == ["AC-1"]
    assert r["unmapped"] == ["AC-2"]


def test_check_traceability_graceful_when_no_rows():
    r = check_traceability([], SOURCES)
    assert r["evaluated"] is False
    assert "insufficient" in r["note"].lower()
    assert r["unmapped"] == []


def test_run_gate_advisory_by_default_even_with_gaps():
    r = run_gate(LEDGER, SOURCES, {"BTest.cls": "@IsTest class BTest{ @IsTest static void m(){} }"})
    assert r["traceability"]["unmapped"] == ["AC-2"]
    assert r["assertion_gaps"] == [{"file": "BTest.cls", "issue": "no assertions"}]
    assert r["blocking"] is False


def test_run_gate_blocks_only_opted_in_present_failure():
    assert run_gate(LEDGER, SOURCES, {}, block=["ac_traceability"])["blocking"] is True


def test_run_gate_untouched_template_blocks_only_on_traceability():
    ledger = _ledger_for("x")
    assert run_gate(ledger, {}, {}, block=["ac_traceability"])["blocking"] is True  # AC-1 has no test
    r = run_gate(ledger, {}, {}, block=["assertion", "ac_drift"])
    assert r["blocking"] is False
    assert r["missing_evidence"] == []      # placeholder status "_(red→green)_" is not green


def test_run_gate_blocks_only_named_missing_test():
    ledger = f"{SECTION2}\n## 3. AC\n{HDR}| AC-1 | x | Gone.testX | | red |\n"
    assert run_gate(ledger, {}, {}, block=["ac_traceability"])["blocking"] is True
    assert run_gate(ledger, {"Gone.cls": "class Gone { testX }"}, {}, block=["ac_traceability"])["blocking"] is False


# ---- test_present ----
def test_trace_pytest_id():
    assert test_present("tests/test_calc.py::test_add", {"tests/test_calc.py": "def test_add(): assert 1"})
    assert not test_present("tests/test_calc.py::test_sub", {"tests/test_calc.py": "def test_add(): assert 1"})
    assert not test_present("tests/test_calc.py::test_add", {"tests/other.py": "def test_add(): assert 1"})


def test_trace_jest_describe_it():
    src = {"cart.test.ts": "describe('Cart', () => { it('adds item', () => { expect(1) }) })"}
    assert test_present("Cart > adds item", src)
    assert not test_present("Cart > removes item", src)


def test_trace_apex_class_method():
    assert test_present("FooTest.testX", SOURCES)
    assert not test_present("FooTest.testZ", SOURCES)


def test_segments_must_share_a_file():
    assert not test_present("Foo.testX", {"a.txt": "Foo", "b.txt": "testX"})
    assert not test_present("", SOURCES)


# ---- scan_assertions ----
def test_unknown_language_changed_file_ignored():
    assert scan_assertions({"notes.txt": "nothing", "src/app.py": "x = 1"}, DEFAULT_TESTS) == []


@pytest.mark.parametrize("path", [
    "tests/test_a.py", "web/cart.test.ts", "src/CartTest.java",
    "src/CartTests.cs", "pkg/cart_test.go", "classes/CartTest.cls",
])
def test_no_assertions_flagged_per_language(path):
    assert scan_assertions({path: "// nothing checked here\n"}, DEFAULT_TESTS) == [
        {"file": path, "issue": "no assertions"}]


@pytest.mark.parametrize("path,text", [
    ("tests/test_a.py", "assert 1"), ("web/cart.test.ts", "expect(1)"),
    ("src/CartTest.java", "assertEquals(1,1)"), ("src/CartTests.cs", "Assert.Equal(1,1)"),
    ("pkg/cart_test.go", "t.Error(1)"), ("classes/CartTest.cls", "System.assert(true)"),
])
def test_assertions_present_not_flagged(path, text):
    assert scan_assertions({path: text}, DEFAULT_TESTS) == []


def test_discover_tests_globs_and_skips_unreadable(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("assert 1")
    (tmp_path / "test_top.py").write_text("assert 2")
    (tmp_path / "tests" / "test_bad.py").write_bytes(b"\xff\xfe\x00bad")
    (tmp_path / "app.py").write_text("x")
    found = discover_tests(str(tmp_path), DEFAULT_TESTS)
    assert sorted(p.rsplit("/", 1)[-1] for p in found) == ["test_a.py", "test_top.py"]


# ---- file confinement (a PR must not use gate.tests regexes as a content oracle) ----
def _sandbox(tmp_path):
    src, outside = tmp_path / "src", tmp_path / "outside"
    src.mkdir()
    outside.mkdir()
    return src, outside


def _put_ledger(src, text):
    (src / "dod").mkdir(exist_ok=True)
    (src / "dod" / f"DoD_{KEY}.md").write_text(text)


def _no_client():
    raise AssertionError("the gate asked for a client")


def _eval(cfg, src, changed=(), offline=True, factory=_no_client, base=None):
    return gate.evaluate(cfg, KEY, src=str(src), changed=list(changed), base=base, offline=offline, client_factory=factory)


def test_discover_tests_never_reads_symlinks_or_outside_files(tmp_path):
    src, outside = _sandbox(tmp_path)
    (outside / "secret.txt").write_text("ALPHA")
    (src / "real.py").write_text("assert 1")
    (src / "inside_target.txt").write_text("assert 2")
    (src / "test_out.py").symlink_to(outside / "secret.txt")  # leaves --src
    (src / "test_in.py").symlink_to(src / "inside_target.txt")  # stays inside, still skipped
    (src / "linkdir").symlink_to(outside, target_is_directory=True)
    (outside / "test_viadir.py").write_text("assert 3")
    skipped = set()
    found = discover_tests(str(src), {"**/*.py": "assert"}, skipped)
    assert [p.rsplit("/", 1)[-1] for p in found] == ["real.py"]
    assert len(skipped) == 2  # linkdir is never descended, so it is not even a candidate


def test_discover_tests_skips_fifo_and_oversize_without_blocking(tmp_path):
    from skj.gate import MAX_READ
    src, _ = _sandbox(tmp_path)
    os.mkfifo(src / "test_fifo.py")  # open() on it would hang without O_NONBLOCK
    (src / "test_big.py").write_text("assert 1\n" + "#" * MAX_READ)
    (src / "test_ok.py").write_text("assert 1")
    skipped = set()
    found = discover_tests(str(src), {"**/*.py": "assert"}, skipped)
    assert [p.rsplit("/", 1)[-1] for p in found] == ["test_ok.py"] and len(skipped) == 2


def test_read_returns_a_file_of_exactly_the_cap_and_rejects_one_byte_more(tmp_path):
    from skj.gate import MAX_READ, _read
    (tmp_path / "ok.py").write_text("a" * MAX_READ)
    (tmp_path / "big.py").write_text("a" * (MAX_READ + 1))
    skipped = set()
    assert _read(tmp_path / "ok.py", str(tmp_path), skipped) == "a" * MAX_READ and not skipped
    assert _read(tmp_path / "big.py", str(tmp_path), skipped) is None and len(skipped) == 1


def test_read_skips_a_path_through_a_symlinked_directory_that_leaves_src(tmp_path):
    from skj.gate import _read
    src, outside = _sandbox(tmp_path)
    (outside / "x.py").write_text("assert 1")
    (src / "d").symlink_to(outside, target_is_directory=True)
    skipped = set()
    assert _read(src / "d" / "x.py", str(src), skipped) is None and len(skipped) == 1


def test_read_tolerates_a_swapped_in_symlink_via_o_nofollow(tmp_path, monkeypatch):
    src, outside = _sandbox(tmp_path)
    (outside / "x.txt").write_text("ALPHA")
    (src / "t.py").symlink_to(outside / "x.txt")
    monkeypatch.setattr(gate.os.path, "islink", lambda p: False)  # the check lost the race: O_NOFOLLOW must still refuse
    skipped = set()
    assert gate._read(src / "t.py", str(src), skipped) is None and len(skipped) == 1


def test_oracle_probe_leaks_nothing(cfg, tmp_path, monkeypatch):
    """Two outside files starting A-M and N-Z, symlinked into the repo; regexes that would tell them apart."""
    src, outside = _sandbox(tmp_path)
    (outside / "one").write_text("ALPHA")
    (outside / "two").write_text("ZULU")
    for n in ("one", "two"):
        (src / f"test_{n}.py").symlink_to(outside / n)
    _put_ledger(src, _ledger_for("x"))
    cfg.gate["tests"] = {"**/test_*.py": "[A-M]", "**/test_two.py": "[N-Z]"}
    monkeypatch.chdir(src)
    out = _eval(cfg, ".", changed=["./test_one.py", "./test_two.py"])
    assert out["assertion_gaps"] == [] and out["assertion_notes"] == []
    assert out["skipped_note"] == "2 file(s) skipped: symlink, outside --src, not a regular file, or over 1 MiB"
    text = render_summary(out)
    assert "Files skipped:" in text and not any(w in text for w in ("test_one.py", "test_two.py", str(outside), "ALPHA", "ZULU"))
    # a regular file with the same content is still judged
    (src / "test_real.py").write_text("ZZZ")
    out = _eval(cfg, ".", changed=["./test_real.py"])
    assert out["assertion_gaps"] == [{"file": "./test_real.py", "issue": "no assertions"}]


def test_changed_path_outside_src_is_not_read(cfg, tmp_path):
    src, outside = _sandbox(tmp_path)
    (outside / "test_x.py").write_text("no asserts here")
    _put_ledger(src, _ledger_for("x"))
    out = _eval(cfg, src, changed=[str(outside / "test_x.py")])
    assert out["assertion_gaps"] == [] and "1 file(s) skipped" in out["skipped_note"]


def test_symlinked_parent_counts_a_missing_and_an_existing_target_alike(tmp_path):
    from skj.gate import _read
    src, outside = _sandbox(tmp_path)
    (outside / "secret").write_text("ALPHA")
    (src / "dlink").symlink_to(outside, target_is_directory=True)
    a, b = set(), set()
    assert _read(src / "dlink" / "secret", str(src), a) is None
    assert _read(src / "dlink" / "nope", str(src), b) is None
    assert len(a) == len(b) == 1
    c = set()  # a missing file that really is under --src stays uncounted, as before
    assert _read(src / "gone.py", str(src), c) is None and not c


def test_changed_through_a_symlinked_parent_gives_the_same_note_for_existing_and_missing(cfg, tmp_path):
    src, outside = _sandbox(tmp_path)
    (outside / "secret").write_text("ALPHA")
    (src / "dlink").symlink_to(outside, target_is_directory=True)
    _put_ledger(src, _ledger_for("x"))
    notes = [_eval(cfg, src, changed=[str(src / "dlink" / n)])["skipped_note"] for n in ("secret", "nope")]
    assert notes[0] == notes[1] and notes[0].startswith("1 file(s) skipped")


def _ledger_cases(tmp_path):
    src, outside = _sandbox(tmp_path)
    (outside / "ledger.md").write_text(LEDGER)  # ledger-shaped content outside --src
    (src / "dod").mkdir()
    os.mkfifo(src / "fifo.md")
    return src, {"outside-symlink": outside / "ledger.md", "dev-zero": Path("/dev/zero"), "fifo": src / "fifo.md"}


@pytest.mark.parametrize("kind", ["outside-symlink", "dev-zero", "fifo"])
def test_gate_ledger_is_read_confined(cfg, tmp_path, kind):
    src, targets = _ledger_cases(tmp_path)
    led = src / "dod" / f"DoD_{KEY}.md"
    if kind != "fifo":
        led.symlink_to(targets[kind])
    else:
        (src / "fifo.md").rename(led)
    out = _eval(cfg, src)  # a hang here would be the bug
    assert out["ledger"] == "unreadable" and out["blocking"] is False
    assert "unreadable" in out["note"] and "FooTest" not in str(out) and "AC-1" not in str(out)
    assert str(targets[kind]) not in out["note"]  # the ledger's place may be named; a symlink target never


def test_gate_ledger_inside_src_still_reads(cfg, tmp_path):
    _put_ledger(tmp_path, LEDGER)
    out = _eval(cfg, tmp_path)
    assert "traceability" in out


def test_gate_missing_ledger_is_an_advisory_note(cfg, tmp_path):
    out = _eval(cfg, tmp_path)
    assert out == {"ledger": "missing", "blocking": False,
                   "note": f"no ledger at dod/DoD_{KEY}.md — run `skj dod seed {KEY}` to enable tracking"}


def test_gate_refuses_a_key_of_another_project(cfg, tmp_path):
    with pytest.raises(ValueError, match="project SPK"):
        gate.evaluate(cfg, "OTHER-7", src=str(tmp_path), offline=True)


# ---- drift / evidence ----
def test_drift_detected():
    ledger = _ledger_for("old criteria")
    assert check_drift(ledger, _wi("new criteria")) == {"evaluated": True, "drift": True}
    assert check_drift(ledger, _wi("old criteria"))["drift"] is False


def test_drift_not_evaluated_without_hash():
    assert check_drift("no header", _wi("x"))["evaluated"] is False
    assert check_drift(_ledger_for("x"), None)["evaluated"] is False


def test_drift_when_the_ac_heading_is_gone_or_the_description_unreadable_fails_closed():
    """The AC heading removed in Jira after seeding (`dod.MissingAC`), a null description, two headings, an unknown node:
    drift, with a fixed note; never a crash and never `no drift`."""
    ledger = _ledger_for("x")
    gone = _wi("x")
    gone["fields"]["description"] = {"version": 1, "type": "doc", "content": [p("the AC section was deleted")]}
    null = _wi("x")
    null["fields"]["description"] = None
    two = _wi("x")
    two["fields"]["description"]["content"] += [h(2, HEADING), p("x")]
    odd = _wi([{"type": "mysteryNode", "content": []}])
    for issue in (gone, null, two, odd, {"key": KEY, "id": "10007"}):
        d = check_drift(ledger, issue)
        assert d == {"evaluated": True, "drift": True, "note": gate.AC_UNREADABLE}, issue
    r = run_gate(ledger, {}, {}, gone, block=["ac_drift"])
    assert r["blocking"] is True and r["blocked_by"] == ["ac_drift"]
    md = render_summary(r)
    assert gate.AC_UNREADABLE in md.partition("**Blocking")[2] and "no drift" not in md


def test_drift_uses_the_configured_heading():
    i = _wi("x")
    i["fields"]["description"]["content"][1] = h(2, "Done when")
    led = render(i, ac_section(i["fields"]["description"], "Done when"), SITE)
    assert check_drift(led, i, heading="Done when") == {"evaluated": True, "drift": False}
    assert check_drift(led, i)["drift"] is True  # the default heading is not there


def test_missing_evidence():
    assert missing_evidence(parse_section3(f"{SECTION2}\n## 3.\n{HDR}| AC-1 | a | T.x |  | green |\n"
                                           "| AC-2 | b | T.y | pr-1 | green |\n"
                                           "| AC-3 | c | T.z |  | red |\n")) == ["AC-1"]


def test_block_ac_drift_sets_blocking():
    ledger = _ledger_for("old")
    assert run_gate(ledger, {}, {}, _wi("new"), block=["ac_drift"])["blocking"] is True
    assert run_gate(ledger, {}, {}, _wi("new"), block=[])["blocking"] is False
    assert run_gate(ledger, {}, {}, _wi("old"), block=["ac_drift"])["blocking"] is False


def test_missing_info_never_blocks():
    r = run_gate("", {}, {}, None, block=["ac_traceability", "assertion", "ac_drift"])
    assert r["blocking"] is False
    assert r["drift"]["evaluated"] is False


# ---- the evaluate step (the fork's CLI dispatch) ----
def test_config_tests_override_glob(cfg, tmp_path):
    _put_ledger(tmp_path, "## 3\n")
    f = tmp_path / "test_a.py"
    f.write_text("assert 1")
    cfg.gate["tests"] = {"**/test_*.py": "MAGIC"}
    out = _eval(cfg, tmp_path, changed=[str(f)])
    assert out["assertion_gaps"] == [{"file": str(f), "issue": "no assertions"}]


def test_gate_cli_offline_does_not_fetch(cfg, tmp_path):
    _put_ledger(tmp_path, _ledger_for("x"))
    out = _eval(cfg, tmp_path)  # _no_client raises if asked
    assert out["drift"]["evaluated"] is False


def test_gate_cli_fetch_failure_degrades(cfg, tmp_path, monkeypatch):
    from skj.client import ClientError
    from skj.secretstore import SecretMissing
    _put_ledger(tmp_path, _ledger_for("x"))
    cfg.gate["block"] = ["ac_drift"]

    def boom(client, key, fields=None):
        raise ClientError("down")
    monkeypatch.setattr(gate.issues, "get", boom)
    out = _eval(cfg, tmp_path, offline=False, factory=lambda: object())
    assert out["drift"]["evaluated"] is False and out["blocking"] is False
    assert out["drift"]["note"] == gate.UNREACHABLE

    def no_secret():
        raise SecretMissing("no identity")
    out = _eval(cfg, tmp_path, offline=False, factory=no_secret)
    assert out["drift"]["evaluated"] is False and out["blocking"] is False


def test_gate_cli_drift_blocks(cfg, tmp_path, monkeypatch):
    _put_ledger(tmp_path, _ledger_for("old"))
    cfg.gate["block"] = ["ac_drift"]
    seen = []
    monkeypatch.setattr(gate.issues, "get", lambda client, key, fields=None: seen.append((key, fields)) or _wi("new"))
    assert _eval(cfg, tmp_path, offline=False, factory=lambda: object())["blocking"] is True
    assert seen == [(KEY, ["description"])]  # one GET, the description only


def test_gate_cli_drift_against_the_fake_jira(cfg, tmp_path):
    """End to end through the real client and `issues.get` against the fake: one GET, no write."""
    from fakejira import FakeJira
    f = FakeJira()
    f.add_issue("T", description=_desc("old"))
    (tmp_path / "dod").mkdir()
    (tmp_path / "dod" / "DoD_SPK-1.md").write_text(_ledger_for("old").replace(KEY, "SPK-1"))
    out = gate.evaluate(cfg, "SPK-1", src=str(tmp_path), offline=False, client_factory=f.client)
    assert out["drift"] == {"evaluated": True, "drift": False}
    assert f.writes() == [] and [(e["method"], e["path"]) for e in f.log] == [("GET", "/issue/SPK-1")]


# ---- fix round 1 ----
def test_discover_tests_prunes_vendored_dirs(tmp_path):
    for d in ("tests", "node_modules/x", ".venv/lib/site", "a/.git", "build"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "test_calc.py").write_text("def test_add(): assert 1")
    found = discover_tests(str(tmp_path), DEFAULT_TESTS)
    assert [p.replace(str(tmp_path), "") for p in found] == ["/tests/test_calc.py"]


def test_path_id_requires_path_boundary():
    src = {"tests/my_test_calc.py": "def test_add(): assert 1"}
    assert not test_present("test_calc.py::test_add", src)
    assert test_present("my_test_calc.py::test_add", src)
    assert test_present("tests/my_test_calc.py::test_add", src)
    assert test_present("test_calc.py::test_add", {"/abs/tests/test_calc.py": "def test_add(): pass"})
    assert test_present("test_calc.py::test_add", {"test_calc.py": "def test_add(): pass"})


def test_run_gate_graceful_never_blocks_info_poor():
    r = run_gate("no section 3", {}, {}, block=["ac_traceability", "assertion", "ac_drift"])
    assert r["traceability"]["evaluated"] is False
    assert r["blocking"] is False
    assert scan_assertions({}, DEFAULT_TESTS) == []


# ---- fix round 2 ----
@pytest.mark.parametrize("pattern,path,ok", [
    ("tests/**/test_*.py", "tests/test_x.py", True),
    ("tests/**/test_*.py", "tests/a/b/test_x.py", True),
    ("tests/**/test_*.py", "tests/a/b/x.py", False),
    ("src/**/*.spec.ts", "src/a/b/x.spec.ts", True),
    ("src/**/*.spec.ts", "lib/x.spec.ts", False),
    ("**/*.test.[jt]s", "a.test.js", True),
    ("**/*.test.[jt]s", "d/a.test.ts", True),
    ("**/*.test.[jt]s", "d/a.test.py", False),
    ("**/test_*.py", "/abs/proj/tests/test_x.py", True),
    ("tests/**", "tests/a/b.py", True),
    ("**/*Test.java", "src/AppTest.java.bak", False),
    # beyond the fork's cases: the component-wise matcher keeps these meanings
    ("*.py", "a/b.py", True),            # right-anchored: leading directories tolerated
    ("a/*.py", "a/b/c.py", False),       # `*` never crosses a `/`
    ("a/?.py", "a/b.py", True),
    ("a/?.py", "a/bc.py", False),
    ("**/[!x]*.py", "d/a.py", True),
    ("**/[!x]*.py", "d/xa.py", False),
    ("tests/**", "tests", False),        # a trailing `**` needs one component more
    ("**/a*b*c", "x/aXbYc", True),
    ("**/a*b*c", "x/aXbY/c", False),
])
def test_glob_match(pattern, path, ok):
    from skj.gate import _glob_match
    assert _glob_match(path, pattern) is ok


@pytest.mark.parametrize("pattern", ["**/[!]", "a/[b", "src/**test.py", "a**", "a/[/]b", "x/***/y"])
def test_valid_glob_refuses_what_the_matcher_cannot_judge(pattern):
    """Ruling (L3): `**` only as a whole path segment, and no `/` inside a class; the fork's regex let `**` cross `/` mid-segment."""
    assert gate.valid_glob(pattern) is False


def test_discover_tests_interior_doublestar(tmp_path):
    for d in ("tests", "tests/a/b", "lib"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    for f in ("tests/test_x.py", "tests/a/b/test_y.py", "lib/test_z.py"):
        (tmp_path / f).write_text("assert 1")
    found = discover_tests(str(tmp_path), {"tests/**/test_*.py": "assert"})
    assert sorted(p.replace(str(tmp_path), "") for p in found) == ["/tests/a/b/test_y.py", "/tests/test_x.py"]


def test_changed_interior_doublestar_config_glob():
    tests = {"tests/**/check_*.py": "MAGIC"}
    assert scan_assertions({"tests/a/b/check_x.py": "assert 1"}, tests) == [
        {"file": "tests/a/b/check_x.py", "issue": "no assertions"}]
    assert scan_assertions({"lib/check_x.py": "assert 1"}, tests) == []


def test_empty_normalised_path_id_is_not_present():
    assert not test_present("./::test_x", {"a.py": "def test_x(): pass"})
    assert not test_present("::test_x", {"a.py": "def test_x(): pass"})


# ---- fix round 3 ----
def test_discover_tests_src_is_the_literal_leading_dir(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.spec.ts").write_text("expect(1)")
    found = discover_tests(str(tmp_path / "src"), {"src/*.spec.ts": r"\bexpect\("})
    assert [p.replace(str(tmp_path), "") for p in found] == ["/src/x.spec.ts"]


def test_malformed_glob_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="invalid glob in gate.tests"):
        discover_tests(str(tmp_path), {"**/[!]": "x"})
    with pytest.raises(ValueError):
        scan_assertions({"a.py": "x"}, {"**/[!]": "x"})


def test_empty_rest_after_path_is_not_present():
    src = {"tests/test_calc.py": "def test_add(): assert 1"}
    assert not test_present("tests/test_calc.py::", src)
    assert not test_present("tests/test_calc.py:: > .", src)


# ---- AC rows only from the authored section 3 ----
def test_parse_section3_ignores_ac_table_in_section1():
    md = _ledger_for("| AC-1 | Given x | test_anything | abc | green |")
    assert "| AC-1 | Given x |" in md               # the trap is really in section 1
    assert [r["ac"] for r in parse_section3(md)] == ["AC-1"]
    assert parse_section3(md)[0]["tests"] == []   # the template row, not the copied one


def test_parse_section3_stops_at_next_heading():
    md = f"{SECTION2}\n## 3. AC\n{HDR}| AC-1 | a | T.x | | red |\n## 4. Log\n| AC-9 | b | T.y | | red |\n"
    assert [r["ac"] for r in parse_section3(md)] == ["AC-1"]


# ---- an AC row with no test blocks under ac_traceability ----
def test_run_gate_empty_or_placeholder_tests_cell_blocks():
    assert run_gate(_ledger_for("x"), {}, {}, block=["ac_traceability"])["blocking"] is True
    empty = f"{SECTION2}\n## 3. AC\n{HDR}| AC-1 | x |  |  | red |\n"
    assert run_gate(empty, {}, {}, block=["ac_traceability"])["blocking"] is True
    assert run_gate(empty, {}, {}, block=[])["blocking"] is False


def test_run_gate_no_ac_rows_not_evaluated_never_blocks():
    r = run_gate(f"{SECTION2}\n## 3. AC\n{HDR}", {}, {}, block=["ac_traceability"])
    assert r["traceability"]["evaluated"] is False and r["blocking"] is False


ROW = "| AC-1 | _(what it must do)_ | _(test IDs, …)_ | _(commit / PR)_ | _(red→green)_ |"


def test_missing_section3_row_is_unmapped():
    ac = [ul("Given a", "Then b", "And c")]

    def ledger(*ids):
        return _ledger_for(ac).replace(ROW, "\n".join(f"| {i} | x | FooTest.testX | | red |" for i in ids))
    assert run_gate(ledger("AC-1", "AC-2", "AC-3"), SOURCES, {})["traceability"]["unmapped"] == []
    r = run_gate(ledger("AC-1", "AC-2"), SOURCES, {}, block=["ac_traceability"])  # AC-3 row deleted
    assert r["traceability"]["unmapped"] == ["AC-3"] and "AC-3" in r["traceability"]["note"]
    assert r["blocking"] is True
    assert check_traceability([], SOURCES, ac_count=2)["unmapped"] == ["AC-1", "AC-2"]
    # a row beyond n is evaluated normally
    assert run_gate(ledger("AC-1", "AC-2", "AC-3", "AC-9"), SOURCES, {})["traceability"]["unmapped"] == []
    assert run_gate(ledger("AC-1", "AC-2", "AC-3").replace("AC-3 | x | FooTest.testX", "AC-9 | x | Gone.t"),
                    SOURCES, {})["traceability"]["unmapped"] == ["AC-9", "AC-3"]


def test_nested_block_ac_rows_pass_blocking_traceability():
    for ac in ([ul(p("Given a"), p("Then b"))], [ol("Given a", "Then b")]):
        md = _ledger_for(ac).replace(ROW, "| AC-1 | a | FooTest.testX | | red |\n| AC-2 | b | FooTest.testX | | red |")
        r = run_gate(md, SOURCES, {}, block=["ac_traceability"])
        assert r["traceability"]["unmapped"] == [] and r["blocking"] is False


# ---- test ids match whole identifiers, not substrings ----
def test_test_id_segments_match_whole_identifiers():
    src = {"tests/test_cart.py": "def test_add_many(): assert 1\nclass LoginTest:\n def testLoginFails(): pass"}
    assert not test_present("tests/test_cart.py::test_add", src)
    assert not test_present("test_add", src)
    assert not test_present("LoginTest.testLogin", src)
    assert test_present("tests/test_cart.py::test_add_many", src)
    assert test_present("LoginTest.testLoginFails", src)


def test_whole_identifier_rule_on_segments_with_separators():
    """The linear matcher keeps the fork's `(?<![A-Za-z0-9_])SEG(?![A-Za-z0-9_])` meaning, overlapping occurrences included."""
    def old(seg, text):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(seg)}(?![A-Za-z0-9_])", text) is not None
    cases = [("x x", "ax x x"), ("adds item", "it('adds item')"), ("adds item", "it('adds items')"), ("-a", "x-a"),
             ("a-", "a-b"), ("a-", "a- "), (" a ", "b a c"), ("a.b", "xa.b"), ("_x", "_x"), ("x\x00y", "x\x00y"),
             ("é", "aé"), ("a-a-a", "a-a-a-a"), ("ab", "abab ab")]
    for seg, text in cases:
        assert gate._present(seg, gate._marked(text)) is old(seg, text), (seg, text)


def test_inline_marker_in_section4_does_not_hide_section3():
    md = (f"{SECTION2}\n## 3. AC\n{HDR}| AC-1 | x | Gone.testX | | red |\n"
          f"## 4. Gate & review log\n- reviewer: re-read `{SECTION2}` before merging\n")
    assert [r["ac"] for r in parse_section3(md)] == ["AC-1"]
    assert run_gate(md, {}, {}, block=["ac_traceability"])["blocking"] is True


def test_too_many_test_ids_fail_closed_with_a_note():
    ids = ", ".join(f"T.t{i}" for i in range(gate.MAX_TEST_IDS + 5))
    md = f"{SECTION2}\n## 3. AC\n{HDR}| AC-1 | a | T.t0 | | red |\n| AC-2 | b | {ids} | | red |\n| AC-3 | c | T.t{gate.MAX_TEST_IDS + 2} | | red |\n"
    src = {"T.cls": " ".join(f"t{i}" for i in range(gate.MAX_TEST_IDS + 5)) + " class T"}
    r = check_traceability(parse_section3(md), src)
    assert r["mapped"] == ["AC-1", "AC-2"] and r["unmapped"] == ["AC-3"]  # AC-3's only test is past the cap
    assert gate.TOO_MANY_IDS in r["note"]
    long_id = "T." + "x" * gate.MAX_TEST_ID
    assert not test_present(long_id, {"T.cls": "class T " + "x" * gate.MAX_TEST_ID}) and check_traceability(
        [{"ac": "AC-1", "tests": [long_id], "evidence": "", "status": ""}], {"T.cls": "T " + "x" * gate.MAX_TEST_ID})["unmapped"] == ["AC-1"]


def test_the_text_search_budget_fails_closed_with_a_note(monkeypatch):
    src = {"web/cart.test.ts": "describe('Cart', () => { it('adds item', () => { expect(1) }) })"}
    md = f"{SECTION2}\n## 3. AC\n{HDR}| AC-1 | a | Cart > adds item | | red |\n"
    assert check_traceability(parse_section3(md), src)["unmapped"] == []
    monkeypatch.setattr(gate, "SCAN_BUDGET", 10)  # less than one file: the non-identifier segment is never searched
    r = check_traceability(parse_section3(md), src)
    assert r["unmapped"] == ["AC-1"] and gate.SCAN_EXHAUSTED in r["note"]


def test_summary_prints_the_capped_or_exhausted_traceability_note(monkeypatch):
    ids = ", ".join(f"T.t{i}" for i in range(gate.MAX_TEST_IDS + 1))
    md = render_summary(run_gate(f"{SECTION2}\n## 3. AC\n{HDR}| AC-1 | a | {ids} | | red |\n", {"T.cls": "class T t0"}, {}))
    assert "Traceability: " in md and gate.TOO_MANY_IDS in md
    monkeypatch.setattr(gate, "SCAN_BUDGET", 10)
    md = render_summary(run_gate(f"{SECTION2}\n## 3. AC\n{HDR}| AC-1 | a | Cart > adds item | | red |\n",
                                 {"c.test.ts": "describe('Cart', () => { it('adds item') })"}, {}))
    assert gate.SCAN_EXHAUSTED in md
    assert "Traceability: " not in render_summary(run_gate(LEDGER, SOURCES, {}))


# ---- render_summary / render_error drive the CI comment ----
def test_summary_blocking_lists_blocking_findings():
    md = render_summary(run_gate(LEDGER, SOURCES, {}, block=["ac_traceability"]))
    blocking, _, advisory = md.partition("**Advisory")
    assert "**Blocking" in blocking and "`AC-2`" in blocking
    assert "`AC-2`" not in advisory


def test_summary_advisory_only():
    r = run_gate(LEDGER.replace("abc123", ""), SOURCES, {"BTest.cls": "@IsTest class BTest{}"})
    md = render_summary(r)
    assert "**Blocking" not in md
    assert "`AC-2`" in md and "`BTest.cls`" in md and "`AC-1`" in md  # unmapped, gap, no evidence
    assert "drift not checked" in md


def test_summary_all_clean():
    clean = f"{SECTION2}\n## 3. AC\n{HDR}| AC-1 | x | FooTest.testX | abc | green |\n"
    md = render_summary(run_gate(clean, SOURCES, {}))
    assert "**Blocking" not in md and "**Advisory" not in md and "No findings" in md


@pytest.mark.parametrize("now,block,line", [
    ("new", [], "AC changed in Jira since the ledger was seeded"),
    ("old", [], "no drift"),
    (None, [], "drift not checked"),
])
def test_summary_drift_line(now, block, line):
    md = render_summary(run_gate(_ledger_for("old"), {}, {}, now and _wi(now), block=block))
    assert line in md


def test_summary_drift_blocking_is_listed_as_blocking():
    md = render_summary(run_gate(_ledger_for("old"), {}, {}, _wi("new"), block=["ac_drift"]))
    assert "AC changed in Jira" in md.partition("**Advisory")[0].partition("**Blocking")[2]


def test_summary_missing_ledger_is_advisory_note():
    md = render_summary({"ledger": "missing", "blocking": False, "note": f"no ledger at dod/DoD_{KEY}.md"})
    assert "no ledger" in md and "**Blocking" not in md


def test_summary_untrusted_text_is_one_capped_code_span():
    evil = "x`y\n## pwn | z" + "a" * 500
    r = {**run_gate("", {}, {}), "assertion_gaps": [{"file": evil, "issue": "no assertions"}]}
    md = render_summary(r)
    (line,) = [ln for ln in md.splitlines() if "pwn" in ln]
    assert line.count("`") == 2 and "## pwn | z" in line
    assert "\n## pwn" not in md and len(line) < 300


def test_gate_refuses_when_skj_token_in_env(monkeypatch, tmp_path):
    """L3-AC4: the fork's `ADO_TOKEN` detection, now `SKJ_TOKEN` (and `SKJ_EMAIL`). The fork REDACTS the token from the gate's
    error summary and does NOT refuse to run when it is set (the gate needs it in CI for the drift GET); that is ported
    exactly. The plan's word "refuses" in this test's name is read as "never leaks": the value never reaches the summary,
    and the gate's git runs never receive either variable."""
    from skj.gate import render_error
    monkeypatch.setenv("SKJ_TOKEN", "tok-1001-secret")
    monkeypatch.setenv("SKJ_EMAIL", "dev.person@example.com")
    md = render_error(ValueError("401 for tok-1001-secret by dev.person@example.com\nat https://example-site.atlassian.net"))
    assert "tok-1001-secret" not in md and "dev.person@example.com" not in md and "could not run" in md and "***" in md
    seen = []
    real = subprocess.run
    monkeypatch.setattr(config.subprocess, "run", lambda *a, **kw: seen.append(kw["env"]) or real(*a, **kw))
    check_commits(str(tmp_path), "SPK-1")
    check_smart_commits(str(tmp_path))
    assert seen and all("SKJ_TOKEN" not in e and "SKJ_EMAIL" not in e for e in seen)


def test_render_error_names_an_invalid_config_as_a_failure():
    md = gate.render_error(config.ConfigInvalid("skj.config.json: 'gate.block' must be a list of strings"))
    assert "FAILED" in md and "skj.config.json is invalid" in md


# ---- per-test assertion check (Python via ast) ----
def _ledger_tests(*ids):
    return f"{SECTION2}\n## 3. AC → tests (traceability)\n{HDR}| AC-1 | a | {', '.join(ids)} |  | red |\n"


def _gate_py(src, *ids, path="tests/test_x.py"):
    return run_gate(_ledger_tests(*ids), {path: src}, {})


def test_assertion_free_mapped_test_flagged_in_asserting_file():
    src = "def test_x(): pass\n\ndef test_y():\n    assert 1\n"
    r = _gate_py(src, "tests/test_x.py::test_x", "tests/test_x.py::test_y")
    assert r["assertion_gaps"] == [{"test": "tests/test_x.py::test_x", "issue": "no assertions"}]


def test_class_method_form_checked_per_test():
    src = ("class TestA:\n    def test_m(self): pass\n"
           "class TestB:\n    def test_m(self): assert 1\n")
    r = _gate_py(src, "TestA.test_m", "tests/test_x.py::TestB::test_m")
    assert r["assertion_gaps"] == [{"test": "TestA.test_m", "issue": "no assertions"}]


@pytest.mark.parametrize("body", [
    "with pytest.raises(ValueError):\n            int('x')",
    "pytest.fail('no')",
    "self.assertEqual(1, 1)",
    "m.assert_called_once()",
    "if True:\n            assert 1",
    "with raises(ValueError):\n            int('x')",
    "assert_allclose(1, 1)",
    "assert_that(1).is_equal_to(1)",
    "with pt.raises(ValueError):\n            int('x')",
    "with pytest.warns(UserWarning):\n            pass",
    "self.fail('no')",
    "raise AssertionError('no')",
    "raise AssertionError",
    "self._chk()",
    "check(1)",
])
def test_assertion_forms_count(body):
    # the fork indented the second line of these bodies like the first (an IndentationError it never saw: an unparseable
    # file passed silently there); parse failures are findings now, so each body must really parse
    src = ("import pytest\nimport pytest as pt\nfrom pytest import raises\n"
           "def check(x):\n    assert x\n"
           f"class T:\n    def _chk(self):\n        self.assertTrue(1)\n\n    def test_a(self):\n        {body}\n")
    assert _gate_py(src, "T.test_a")["assertion_gaps"] == []


def test_helper_without_assertion_does_not_count():
    src = "def check(x): return x\ndef test_a(): check(1)\n"
    assert _gate_py(src, "test_a")["assertion_gaps"] == [{"test": "test_a", "issue": "no assertions"}]


UNANALYSED_GAP = [{"file": "tests/test_x.py", "issue": "could not be analysed"}]


def test_deeply_nested_source_notes_not_crash():
    r = _gate_py("def test_a(): x = " + "(" * 5000 + "1" + ")" * 5000, "test_a")
    assert r["assertion_gaps"] == UNANALYSED_GAP and any("parse" in n for n in r["assertion_notes"])


def test_unparseable_python_is_a_blocking_assertion_finding_not_a_crash():
    """Fix round 1 (fail closed; the fork only noted it): a Python test file the gate cannot parse is an `assertion` finding."""
    r = run_gate(_ledger_tests("tests/test_x.py::test_a"), {"tests/test_x.py": "def test_a(:\n"}, {}, block=["assertion"])
    assert r["assertion_gaps"] == UNANALYSED_GAP and r["blocking"] is True and r["blocked_by"] == ["assertion"]
    assert any("test_x.py" in n and "parse" in n for n in r["assertion_notes"])
    assert "`tests/test_x.py`" in render_summary(r).partition("**Blocking")[2]
    assert run_gate(_ledger_tests("tests/test_x.py::test_a"), {"tests/test_x.py": "def test_a(:\n"}, {})["blocking"] is False


BOMBS = {"attr": "x = a" + ".a" * 300000, "neg": "x = " + "-" * 300000 + "1", "not": "x = " + "not " * 200000 + "1",
         "lambda": "x = " + "lambda:" * 140000 + "1", "add": "x = " + "1+" * 300000 + "1", "call": "x = f" + "()" * 300000,
         "index": "x = a" + "[0]" * 300000}


@pytest.mark.skipif(not hasattr(__import__("threading"), "stack_size"), reason="threading.stack_size is not available")
@pytest.mark.parametrize("name", sorted(BOMBS))
def test_parser_stack_bombs_are_a_finding_and_the_process_survives(name):
    """ast.parse on the caller's stack SIGSEGVs on 3.9.6 for these (< 1 MiB); the gate parses on a big worker stack, and a
    file it still cannot parse is an `assertion` finding. Runs in this process: a crash would kill the test run."""
    src = "def test_x():\n    pass\n" + BOMBS[name] + "\n"
    assert len(src) < _MIB
    r = run_gate(_ledger_tests("tests/test_x.py::test_x"), {"tests/test_x.py": src}, {}, block=["assertion"])
    assert r["blocking"] is True and r["assertion_gaps"] in (UNANALYSED_GAP, [{"test": "tests/test_x.py::test_x", "issue": "no assertions"}])


def test_a_nesting_bomb_is_always_the_unanalysed_finding_and_the_run_survives():
    """Whatever the platform's stack: 500k open parentheses are a SyntaxError (or worse) in every parser, never a pass."""
    src = "def test_x():\n    assert 1\nx = " + "(" * 500000 + "\n"
    r = run_gate(_ledger_tests("tests/test_x.py::test_x"), {"tests/test_x.py": src}, {}, block=["assertion"])
    assert r["assertion_gaps"] == UNANALYSED_GAP and r["blocking"] is True


BOM = "\ufeff"


def test_a_utf8_bom_test_file_is_parsed_not_blocked(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_bytes((BOM + "def test_x():\n    assert 1\n").encode("utf-8"))
    sources = discover_tests(str(tmp_path), DEFAULT_TESTS)
    (text,) = sources.values()
    assert text.startswith(BOM)  # _read keeps the BOM: the parser strips it
    r = run_gate(_ledger_tests("test_x.py::test_x"), sources, {}, block=["assertion", "ac_traceability"])
    assert r["assertion_gaps"] == [] and r["traceability"]["unmapped"] == [] and r["blocking"] is False


def test_a_utf8_bom_does_not_hide_a_gap_or_an_unparseable_file():
    r = _gate_py(BOM + "def test_x():\n    pass\n", "test_x")
    assert r["assertion_gaps"] == [{"test": "test_x", "issue": "no assertions"}]
    r = run_gate(_ledger_tests("tests/test_x.py::test_x"), {"tests/test_x.py": BOM + "def test_x(:\n"}, {}, block=["assertion"])
    assert r["assertion_gaps"] == UNANALYSED_GAP and r["blocking"] is True


def test_a_parse_that_cannot_get_a_big_stack_is_a_finding(monkeypatch):
    import threading

    def refuse(size=0):
        raise ValueError("size not valid")
    monkeypatch.setattr(threading, "stack_size", refuse)
    assert _gate_py("def test_x():\n    assert 1\n", "test_x")["assertion_gaps"] == UNANALYSED_GAP


def test_jest_mapped_test_is_note_only():
    src = "describe('Cart', () => { it('adds', () => { }); });"
    r = run_gate(_ledger_tests("Cart > adds"), {"web/cart.test.ts": src}, {}, block=["assertion"])
    assert r["assertion_gaps"] == [] and r["blocking"] is False
    assert any("Python only" in n for n in r["assertion_notes"])


def test_python_only_mapping_has_no_note():
    assert _gate_py("def test_x(): assert 1", "test_x")["assertion_notes"] == []


def test_summary_shows_per_test_gap_and_note():
    r = {**_gate_py("def test_x(): pass", "test_x"), "assertion_notes": ["per-test checking covers Python only"]}
    md = render_summary(r)
    assert "`test_x`" in md and "Python only" in md


def test_offline_drift_note():
    led = _ledger_for("x")
    assert check_drift(led, None, "offline — drift not checked")["note"] == "offline — drift not checked"
    assert check_drift(led, None)["note"] == "Jira not reachable or no identity — drift not checked"
    assert check_drift("no header", _wi("x"))["note"] == "no AC hash in the ledger — drift not checked"
    md = render_summary(run_gate(led, {}, {}, None, reason="offline — drift not checked"))
    assert "AC drift: offline — drift not checked" in md


# ---- commits_without_story and smart_commit_commands ----
def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL).stdout.strip()


def _commit(repo, msg, name="f"):
    (repo / name).write_text(msg)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "--short", "HEAD")


@pytest.fixture
def git_env(monkeypatch, tmp_path):
    """Isolate git from the user's config (signing, hooks) and give commits a fixed, fictional identity."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{k}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{k}_EMAIL", "test@example.invalid")


@pytest.fixture
def crepo(tmp_path, git_env):
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, "init")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(tmp_path, "switch", "-q", "-c", "feature/SPK-2-x")
    return tmp_path


def test_commits_with_and_without_story_ref(crepo):
    ok = _commit(crepo, "feat: a\n\nbody SPK-2", "a")
    bad = _commit(crepo, "feat: b", "b")
    other = _commit(crepo, "feat: c SPK-3", "c")
    inword = _commit(crepo, "feat: d xSPK-2", "d")
    longer = _commit(crepo, "feat: e SPK-22", "e")
    subj = _commit(crepo, "feat: f SPK-2", "f")
    got, note = check_commits(str(crepo), "SPK-2")
    assert note is None and ok not in got and subj not in got
    assert set(got) == {bad, other, inword, longer}


def test_commits_without_story_uses_key(crepo):
    """L3-AC2: the reference is the story KEY (KEY_RE shape, uppercase, exact). A foreign project's key, a lowercase,
    zero-padded or Unicode-digit lookalike, or a key glued to a longer token is NOT a reference."""
    good = {_commit(crepo, m, n) for n, m in (("a", "SPK-2: x"), ("b", "fix (SPK-2)"), ("c", "x\n\nRefs: SPK-2."),
                                              ("d", "SPK-1 SPK-2 both"), ("e", "x\r\n\r\nSPK-2\r\n"))}
    lookalikes = ["spk-2 x", "SPK-02 x", "SPK-\uff12 x", "SPK-2\u0663 x", "OTHER-2 x", "SPK-2abc x", "SPK_2 x", "\uff33PK-2 x",
                  "SPK-2_ x", "XSPK-2 x", "SPK-\u0662 x", "SP K-2"]
    bad = {_commit(crepo, m, "l%d" % i) for i, m in enumerate(lookalikes)}
    got, note = check_commits(str(crepo), "SPK-2")
    assert note is None and set(got) == bad and not good & set(got)
    blk = run_gate(LEDGER, SOURCES, {}, None, block=["commits_without_story"], commits=(got, note))
    assert blk["commits_without_story"] == got and blk["blocked_by"] == ["commits_without_story"]
    assert "Commits without the story key" in render_summary(blk)
    for bad_key in ("spk-2", "SPK-02", "SPK", 2):
        with pytest.raises(ValueError):
            check_commits(str(crepo), bad_key)


def test_an_oversize_commit_message_fails_closed(crepo, monkeypatch):
    monkeypatch.setattr(gate, "MAX_MESSAGE", 64)
    big = _commit(crepo, "SPK-2 " + "x" * 100, "big")
    ok = _commit(crepo, "SPK-2 small", "ok")
    note = gate.UNSCANNED % 1
    assert check_commits(str(crepo), "SPK-2") == ([big], note)  # not scanned: counted as without the key
    assert check_smart_commits(str(crepo)) == ([big], note)     # and as a possible command
    assert ok
    r = run_gate(LEDGER, SOURCES, {}, None, commits=check_commits(str(crepo), "SPK-2"), smart_commits=check_smart_commits(str(crepo)))
    assert "smart_commits_note" not in r and f"Commit check: `{note}`" in render_summary(r)


def test_more_commits_than_the_cap_fail_closed(crepo, monkeypatch):
    monkeypatch.setattr(gate, "MAX_COMMITS", 2)
    for i in range(3):
        _commit(crepo, f"SPK-2 c{i}", f"c{i}")
    got, note = check_commits(str(crepo), "SPK-2")
    assert got == ["?"] and note == gate.TOO_MANY_COMMITS
    assert check_smart_commits(str(crepo)) == (["?"], gate.TOO_MANY_COMMITS)


SMART = ["SPK-1 #done", "SPK-1 #close", "SPK-1 #in-progress", "SPK-1 #time 2h", "SPK-1 #comment x", "SPK-1 SPK-2 #done",
         "SPK-1 fix the thing #resolve", "SPK-1 fix\n\nbody\n#done", "SPK-1\u00a0#DONE", "SPK-1\u2003#Done", "SPK-1 x\r\n#close\r\n",
         "OTHER-9 #done", "fix(SPK-1): x #start-progress", "SPK-1\t#Review", "SPK-1 #\u00e9tat", "SPK-1 x\n\nSPK-2 #comment y",
         "SPK-1#done", "spk-1 #done", "xSPK-1 #done", "SPK-1 #done SPK-2", "[SPK-1] #close", "SPK-1 #1st-review", "SPK-1 #1x",
         "ABCDEFGHIJK-1 #done", "S-1 #done", "SPK-12345678901 #close"]
NOT_SMART = ["#done", "#done SPK-1", "fix #123 SPK-1", "SPK-1 see #123", "SPK-1 in C#", "SPK-1 # done", "SPK-1 ## Notes",
             "plain text", "SPK-1 done", "x #close\nSPK-1 later", "SPK-1 #_x", "SPK-1 #123 and #45", "SPK-1 #1_"]


@pytest.mark.parametrize("msg", SMART)
def test_smart_command_shapes_are_flagged(msg):
    assert gate.smart_command(msg) is True


@pytest.mark.parametrize("msg", NOT_SMART)
def test_text_that_is_not_a_smart_command_is_not_flagged(msg):
    assert gate.smart_command(msg) is False


def test_smart_commit_command_flagged(crepo):
    """L3-AC3 (Q6): a commit in <base>..HEAD with a Jira smart-commit command (`SPK-1 #done`) is reported in
    `smart_commit_commands`, rendered in the summary, and blocks only when gate.block names the check."""
    done = _commit(crepo, "SPK-2 finish it\n\nSPK-1 #done", "a")
    clean = _commit(crepo, "SPK-2 refs #12", "b")
    close = _commit(crepo, "SPK-2 #close", "c")
    got, note = check_smart_commits(str(crepo))
    assert note is None and got == [close, done] and clean not in got
    adv = run_gate(LEDGER, SOURCES, {}, None, smart_commits=(got, note))
    assert adv["smart_commit_commands"] == got and adv["blocking"] is False
    md = render_summary(adv)
    assert "smart-commit command" in md and f"`{done}`" in md and f"`{close}`" in md
    blk = run_gate(LEDGER, SOURCES, {}, None, block=["smart_commit_commands"], smart_commits=(got, note))
    assert blk["blocking"] is True and blk["blocked_by"] == ["smart_commit_commands"]
    assert f"`{done}`" in render_summary(blk).partition("**Blocking")[2].partition("**Advisory")[0]
    assert run_gate(LEDGER, SOURCES, {}, None)["smart_commit_commands"] == []
    assert "smart_commit_commands" in gate.GATE_CHECKS


def test_smart_commits_use_the_same_range_and_base_rules(crepo):
    _commit(crepo, "SPK-2 #done old", "o")
    _git(crepo, "branch", "dev")
    _git(crepo, "update-ref", "refs/remotes/origin/dev", "dev")
    new = _commit(crepo, "SPK-2 #close new", "p")
    assert check_smart_commits(str(crepo), "origin/dev") == ([new], None)
    for base in ("--output=x", "-x", "a..b", "main^", "a b", "x:y"):
        got, note = check_smart_commits(str(crepo), base)
        assert got == [] and "not a ref" in note


def test_smart_commits_read_merge_commits(crepo):
    _git(crepo, "switch", "-q", "-c", "side")
    _commit(crepo, "side work SPK-2", "s")
    _git(crepo, "switch", "-q", "feature/SPK-2-x")
    _commit(crepo, "main work SPK-2", "m")
    _git(crepo, "merge", "-q", "--no-ff", "-m", "Merge SPK-1 #done", "side")
    merge = _git(crepo, "rev-parse", "--short", "HEAD")
    assert check_smart_commits(str(crepo)) == ([merge], None)
    assert check_commits(str(crepo), "SPK-2") == ([], None)  # the story-key check still ignores merges (as the fork)
    _git(crepo, "reset", "-q", "--hard", "HEAD~1")
    _git(crepo, "merge", "-q", "--no-ff", "-m", "Merge branch side for SPK-2", "side")
    assert check_smart_commits(str(crepo)) == ([], None)  # a plain merge is not a command


def test_merge_commits_ignored(crepo):
    _git(crepo, "switch", "-q", "-c", "side")
    _commit(crepo, "side work SPK-2", "s")
    _git(crepo, "switch", "-q", "feature/SPK-2-x")
    _commit(crepo, "main work SPK-2", "m")
    _git(crepo, "merge", "-q", "--no-ff", "-m", "Merge branch side", "side")
    assert check_commits(str(crepo), "SPK-2") == ([], None)


def test_no_base_is_a_note_not_a_block(tmp_path, git_env):
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, "no ref here")
    got, note = check_commits(str(tmp_path), "SPK-2")
    r = run_gate(LEDGER, SOURCES, {}, None, block=["commits_without_story"], commits=(got, note))
    assert not r["blocking"] and r["commits_note"] == note
    assert "Commit check: " in render_summary(r)
    assert got == [] and note == "no default branch (origin/HEAD) — commits not checked"
    assert check_commits(str(tmp_path / "nope"), "SPK-2")[1].startswith("git failed")


def test_commits_blocking_only_when_configured(crepo):
    bad = _commit(crepo, "feat: b")
    commits = check_commits(str(crepo), "SPK-2")
    assert commits == ([bad], None)
    adv = run_gate(LEDGER, SOURCES, {}, None, commits=commits)
    assert adv["commits_without_story"] == [bad] and not adv["blocking"]
    blk = run_gate(LEDGER, SOURCES, {}, None, block=["commits_without_story"], commits=commits)
    assert blk["blocking"] and blk["blocked_by"] == ["commits_without_story"]
    assert "Blocking" in render_summary(blk) and f"`{bad}`" in render_summary(blk)
    assert run_gate(LEDGER, SOURCES, {}, None)["commits_without_story"] == []


def test_explicit_base_gives_the_right_range(crepo):
    # PR targets a non-default branch `dev`: only commits after dev count, not those dev already holds
    old = _commit(crepo, "dev-only work, no ref", "o")
    _git(crepo, "branch", "dev")
    _git(crepo, "update-ref", "refs/remotes/origin/dev", "dev")
    bad = _commit(crepo, "feat: after dev", "p")
    assert check_commits(str(crepo), "SPK-2", "origin/dev") == ([bad], None)
    assert set(check_commits(str(crepo), "SPK-2")[0]) == {old, bad}  # default (origin/HEAD) range is wider


def test_bad_base_is_a_note_never_a_block(crepo):
    _commit(crepo, "feat: b")
    for base in ("--output=x", "-x", "origin/main..HEAD", "HEAD~1", "@{-1}", "main\n", "a\tb", ""):
        got, note = check_commits(str(crepo), "SPK-2", base)
        assert got == [] and "not a ref" in note and note.endswith("commits not checked"), base
    assert not (crepo / "x").exists()
    assert check_commits(str(crepo), "SPK-2", "origin/nope") == ([], "base origin/nope not found — commits not checked")


def test_summary_caps_commit_list_at_20():
    shas = [f"{i:07x}" for i in range(25)]
    md = render_summary(run_gate(LEDGER, SOURCES, {}, None, commits=(shas, None), smart_commits=(shas, None)))
    assert "`0000013`" in md and "`0000014`" not in md and md.count("…and 5 more") == 2


# ---- gate inputs ignore hidden headings and fail closed on an open fence ----
def test_gate_inputs_ignore_a_fenced_section3_and_marker():
    led = (f"# DoD\n## 1. S\n### Acceptance Criteria\nreal AC\n\n{SECTION2}\n```\n## 3. AC\n{HDR}| AC-1 | x | FooTest.testX | | green |\n```\n"
           "## 3. AC\n" + HDR + "| AC-2 | y |  |  | red |\n")
    assert [r["ac"] for r in parse_section3(led)] == ["AC-2"]
    r = run_gate(led, SOURCES, {})
    assert "AC-1" in r["traceability"]["unmapped"]  # the fenced AC-1 row maps nothing


@pytest.mark.parametrize("opener", ["```", "~~~", "<!--"])
def test_gate_unclosed_opener_in_the_ledger_is_a_blocking_ledger_note(opener):
    from skj.gate import LEDGER_UNCLOSED_NOTE
    led = LEDGER + f"{opener}\n## 5. Definition of Done\n- [x] a\n"
    r = run_gate(led, SOURCES, {}, block=["open_questions"])
    assert r["ledger_unclosed"] == LEDGER_UNCLOSED_NOTE and r["open_questions"] == [] and "open_questions_note" not in r
    assert r["blocking"] and r["blocked_by"] == ["open_questions"]
    assert LEDGER_UNCLOSED_NOTE in render_summary(r) and "Blocking" in render_summary(r)
    advisory = run_gate(led, SOURCES, {})  # advises unless listed in gate.block
    assert not advisory["blocking"] and "Advisory" in render_summary(advisory)
    assert "ledger_unclosed" not in run_gate(LEDGER, SOURCES, {})


# ---- open_decisions, read from dod/Decisions_<KEY>.md ----
_D_OPEN = "- [ ] D1: Use cursor paging → _(open)_ · kind: decision · why: the story names no page size"
_D_VAL = "- [x] D2: Cap at 50 rows → validated · by: PO · note: agreed"
_D_REJ = "- [x] D3: Skip audit log → rejected · by: PO · note: not acceptable"
_D_OPEN10 = "- [ ] D10: Retry twice → _(open)_ · kind: assumption · why: unspecified"
BASE_KEYS = {"traceability", "assertion_gaps", "assertion_notes", "drift", "missing_evidence", "open_questions",
             "commits_without_story", "smart_commit_commands", "blocked_by", "blocking"}  # the fork's v0.2 set + smart_commit_commands


def _dfile(*lines):
    return render_file("SPK-1001") + "\n".join(lines) + "\n"


def _gd(*lines, **kw):
    return run_gate(LEDGER, SOURCES, {}, decisions_md=_dfile(*lines), **kw)


def test_open_decisions_absent_without_a_file_or_decision_lines():
    assert set(run_gate(LEDGER, SOURCES, {})) == BASE_KEYS
    assert set(run_gate(_ledger_for("x"), {}, {})) == BASE_KEYS
    assert set(_gd()) == BASE_KEYS  # a fresh file: title and format note only
    assert "open_decisions" not in render_summary(_gd())
    # a D line inside the LEDGER is not read
    old = LEDGER.replace("## 3. AC", _D_OPEN + "\n## 3. AC", 1)
    assert set(run_gate(old, SOURCES, {}, block=["open_decisions"])) == BASE_KEYS


def test_open_decisions_lists_ids_and_rejected_counts_as_open():
    r = _gd(_D_OPEN10, _D_VAL, _D_REJ, _D_OPEN)
    assert r["open_decisions"] == ["D1", "D3", "D10"]  # numeric order; validated is resolved
    assert set(r) == BASE_KEYS | {"open_decisions"}
    assert _gd(_D_VAL)["open_decisions"] == []  # present, nothing open
    assert _gd("- [ ] D4 not in the grammar")["open_decisions"] == ["?"]  # unparsed D line: fail closed


def test_open_decisions_blocks_only_when_listed():
    assert _gd(_D_OPEN)["blocking"] is False
    r = _gd(_D_OPEN, block=["open_decisions"])
    assert r["blocked_by"] == ["open_decisions"] and r["blocking"] is True
    assert _gd(_D_VAL, block=["open_decisions"])["blocking"] is False
    assert run_gate(LEDGER, SOURCES, {}, block=["open_decisions"])["blocking"] is False  # no file


def test_summary_line():
    open_md = render_summary(_gd(_D_OPEN, _D_REJ))
    assert "- Open decisions in the decisions file: `D1`, `D3`" in open_md.partition("**Advisory:**")[2]
    blocked = render_summary(_gd(_D_OPEN, block=["open_decisions"]))
    assert "`D1`" in blocked.partition("**Blocking")[2].partition("**Advisory")[0]
    assert "Open decisions: none" in render_summary(_gd(_D_VAL))


def test_open_decisions_fail_closed_on_malformed_hidden_or_unreadable():
    r = _gd("- [ ] D4 not in the grammar", block=["open_decisions"])
    assert r["open_decisions"] == ["?"] and r["blocked_by"] == ["open_decisions"] and r["blocking"] is True
    assert _gd("- [ ] D4 not in the grammar")["blocking"] is False  # advisory unless listed
    for hidden in (("```", _D_OPEN), ("<!--", _D_OPEN)):  # an unclosed fence or comment hides the D line
        r = _gd(*hidden, block=["open_decisions"])
        assert r["open_decisions"] == ["?"] and r["blocked_by"] == ["open_decisions"] and r["blocking"] is True
    r = run_gate(LEDGER, SOURCES, {}, decisions_unreadable=True, block=["open_decisions"])
    assert r["open_decisions"] == ["?"] and r["blocking"] is True
    assert run_gate(LEDGER, SOURCES, {}, decisions_unreadable=True)["blocking"] is False


def test_summary_open_decisions_fail_closed_lines():
    for lines in (("- [ ] D4 not in the grammar",), ("```", _D_OPEN)):
        md = render_summary(_gd(*lines, block=["open_decisions"]))
        blocking = md.partition("**Blocking")[2].partition("**Advisory")[0]
        assert "- Open decisions in the decisions file: `?`" in blocking
        assert "AC changed" not in blocking and "no drift" not in blocking and "drift not checked" not in blocking


def test_gate_reads_decisions_file_confined_and_key_absent_without_file(cfg, tmp_path):
    _put_ledger(tmp_path, _ledger_for("x"))
    cfg.gate["block"] = ["open_decisions"]
    run = lambda: _eval(cfg, tmp_path)  # noqa: E731
    assert "open_decisions" not in run() and run()["blocking"] is False  # no file
    f = tmp_path / "dod" / f"Decisions_{KEY}.md"
    f.write_text(_dfile(_D_OPEN, _D_VAL))
    out = run()
    assert out["open_decisions"] == ["D1"] and out["blocking"] is True and out["blocked_by"] == ["open_decisions"]
    f.write_text(_dfile(_D_VAL))
    out = run()
    assert out["open_decisions"] == [] and out["blocking"] is False
    f.write_bytes(b"\xff\xfe")  # unreadable text: fail closed
    assert run()["open_decisions"] == ["?"]
    f.unlink()
    outside = tmp_path.parent / (tmp_path.name + "-secret.md")
    outside.write_text(_dfile(_D_VAL))
    f.symlink_to(outside)  # a symlink is never followed, even to a harmless file: counted as unreadable
    out = run()
    assert out["open_decisions"] == ["?"] and out["blocking"] is True
    f.unlink()
    f.write_text(_dfile(_D_VAL) + "x" * (1 << 20))  # over MAX_READ
    assert run()["open_decisions"] == ["?"]
    f.write_text(_dfile(_D_VAL))
    assert run()["open_decisions"] == []


def test_open_decisions_use_the_effective_set_and_fail_closed_on_a_bad_supersedes():
    rej, answer = _D_REJ, "- [ ] D5: supersedes D3: answered → _(open)_ · kind: decision · why: w"
    assert _gd(rej)["open_decisions"] == ["D3"]
    assert _gd(rej, answer)["open_decisions"] == ["D5"]  # D3 is answered: only the new decision is open
    assert _gd(rej, answer, block=["open_decisions"])["blocking"] is True  # D5 itself is still open
    assert _gd(rej, "- [x] D5: supersedes D3: answered → validated · by: PO · note: ok")["open_decisions"] == []
    assert _gd(rej, "- [x] D5: supersedes D3: answered → rejected · by: PO · note: no")["open_decisions"] == ["D3", "D5"]
    bad = _gd("- [ ] D1: supersedes D2: x → _(open)_ · kind: decision · why: w", _D_REJ.replace("D3", "D2"), block=["open_decisions"])
    assert "?" in bad["open_decisions"] and bad["blocking"] is True  # a lower-id superseder is invalid: fail closed
    assert "?" in _gd("- [ ] D5: supersedes D9: x → _(open)_ · kind: decision · why: w")["open_decisions"]


# ---- rules: the gate's output with rules off is the baseline exactly; with rules on (RU3) it gains the `rules` key ----
def test_disabled_gate_json_key_set_equals_baseline():
    asked = []

    class _Block:
        def __contains__(self, key):
            asked.append(key)
            return False
    off = run_gate(LEDGER, SOURCES, {}, None, block=_Block())
    assert set(off) == BASE_KEYS and "rule_coverage" not in asked and "rule_drift" not in asked and "rules" not in off
    assert "Business rules" not in render_summary(off) and "rule" not in render_summary(off).lower().replace("traceability", "")


def test_rules_stub_is_gone():
    assert not hasattr(rules, "RulesUnavailable")  # RU3: coverage and local_files are real
    r = run_gate(LEDGER, SOURCES, {}, None, rules_ctx={"files": {}})
    assert r["rules"]["header"] == ["rules-from"]  # this ledger has no `Rules from:` line: a finding, never a pass


def test_gate_cli_rules_off_reads_nothing_and_on_reads_the_files(cfg, tmp_path, monkeypatch):
    """RU1 review minor 5: the name says what it covers. Off (absent or false) reads no rules file and adds no key; on reads them."""
    _put_ledger(tmp_path, LEDGER)
    (tmp_path / "dod" / "Rules_SPK-1.md").write_text("# rules")
    with monkeypatch.context() as mp:
        mp.setattr(rules, "local_files", lambda *a: pytest.fail("rules files were read"))
        for c in (cfg, Config(SITE, "SPK", rules={"enabled": False})):
            out = _eval(c, tmp_path)
            assert "rules" not in out and set(out) - {"commits_note"} == BASE_KEYS  # commits_note: the temp dir is no git repository
    read = []
    monkeypatch.setattr(rules, "local_files", lambda *a: read.append(a) or {})
    out = _eval(Config(SITE, "SPK", rules={"enabled": True}), tmp_path)
    assert read and set(out) - {"commits_note"} == BASE_KEYS | {"rules"} and out["rules"]["header"] == ["rules-from"]


def test_gate_checks_are_run_gates_finding_keys():
    """config.py rejects gate.block names outside GATE_CHECKS: every name is a check run_gate can block on (the two rule checks only
    with rules enabled, as `rules`), so a config can name it."""
    r = run_gate(LEDGER, SOURCES, {}, None, decisions_md=_dfile(_D_OPEN))
    findings = {"ac_traceability": "traceability", "assertion": "assertion_gaps", "ac_drift": "drift"}
    assert [c for c in gate.GATE_CHECKS if findings.get(c, c) not in r] == ["rule_coverage", "rule_drift"]
    on = run_gate(LEDGER, SOURCES, {}, None, decisions_md=_dfile(_D_OPEN), block=gate.GATE_CHECKS, rules_ctx={"files": {}})
    assert {"rule_coverage", "rule_drift"} <= set(on["blocked_by"]) and "rules" in on


def test_config_accepts_gate_entries_now_the_gate_exists(tmp_path, monkeypatch):
    import json
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK", "gate": {
        "block": ["smart_commit_commands", "ac_drift"], "tests": {"**/test_*.py": "assert"}}}))
    c = Config.load(str(tmp_path))
    assert c.gate == {"block": ["smart_commit_commands", "ac_drift"], "tests": {"**/test_*.py": "assert"}}
    for gate_cfg, where in (({"block": ["nope"]}, "gate.block"), ({"tests": {"src/**x.py": "a"}}, "gate.tests"),
                            ({"tests": {"**/*.py": "("}}, "gate.tests")):
        (tmp_path / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK", "gate": gate_cfg}))
        with pytest.raises(config.ConfigInvalid, match=re.escape(where)):
            Config.load(str(tmp_path))


# ---- the CLI verb ----
def _cli_repo(tmp_path, monkeypatch, ledger=LEDGER, extra=None):
    import json
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK", **(extra or {})}))
    _put_ledger(tmp_path, ledger)
    (tmp_path / "FooTest.cls").write_text(APEX)
    monkeypatch.chdir(tmp_path)


def test_gate_verb_prints_json_writes_the_summary_and_exits_by_blocking(tmp_path, monkeypatch, capsys):
    import json
    from skj.__main__ import main
    _cli_repo(tmp_path, monkeypatch)
    assert main(["gate", "7", "--offline", "--summary-out", "out/s.md"], client_factory=_no_client) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["traceability"]["unmapped"] == ["AC-2"] and not out["blocking"]
    assert (tmp_path / "out" / "s.md").read_text().startswith("### skj gate")
    _cli_repo(tmp_path, monkeypatch, extra={"gate": {"block": ["ac_traceability"]}})
    assert main(["gate", KEY, "--offline"], client_factory=_no_client) == 1
    assert json.loads(capsys.readouterr().out)["blocked_by"] == ["ac_traceability"]
    assert main(["gate", "OTHER-7", "--offline"], client_factory=_no_client) == 2
    assert "project SPK" in capsys.readouterr().err


def test_gate_verb_invalid_config_fails_closed_with_exit_3(tmp_path, monkeypatch, capsys):
    from skj.__main__ import main
    _cli_repo(tmp_path, monkeypatch, extra={"gate": {"block": ["nope"]}})
    assert main(["gate", "7", "--offline", "--summary-out", "s.md"], client_factory=_no_client) == 3
    assert "gate.block" in capsys.readouterr().err
    assert "FAILED" in (tmp_path / "s.md").read_text()


# ---- linear time on adversarial input (the PR controls the ledger, the tests, the commit messages) ----
_MIB = 1 << 20
_BOUND = 2.0  # seconds per call; the linear scanners take well under 0.5 s at these sizes
needs_timer = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="signal.setitimer is not available on this platform")


class _TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise _TooSlow()


def _timed(fn, *args):
    """Seconds fn(*args) took; _BOUND when it was cut off there (a super-linear scanner fails the assertion, never hangs the suite)."""
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


def _lines(unit, n=500, width=1990):
    return "\n".join((unit * (width // len(unit) + 1))[:width] for _ in range(n))


MESSAGES = {
    "hashes": "SPK-1 " + "#" * _MIB, "keys": "SPK-1 " * (_MIB // 6), "dashes": "A" + "A-" * (_MIB // 2), "spaces": "SPK-1" + " " * _MIB,
    "near-keys": "AAAAAAAAAA-" * (_MIB // 11), "hash-digits": "SPK-1 " + "#1" * (_MIB // 2), "lines-keys": _lines("SPK-1 "),
    "lines-hash": _lines("# "), "lines-blank": _lines(" \u00a0"), "lines-near": _lines("SPK-"),
}


@needs_timer
@pytest.mark.parametrize("name", sorted(MESSAGES))
def test_commit_message_scanners_are_linear(name):
    msg = MESSAGES[name]
    assert _timed(gate.smart_command, msg) < _BOUND
    assert _timed(gate._has_key, msg, "SPK-2") < _BOUND


def _rows(cell, n):
    return f"{SECTION2}\n## 3. AC\n{HDR}" + "".join(f"| AC-{i} | a | {cell} | | red |\n" for i in range(1, n + 1))


LEDGERS = {
    "many-ids": _rows(", ".join(f"T.t{i}" for i in range(4000)), 20),
    "long-ids": _rows(", ".join("a-" * 990 for _ in range(1)), 500),
    "pipes": f"{SECTION2}\n## 3. AC\n" + ("|" * 1990 + "\n") * 500,
    "commas": _rows("," * 1980, 500),
    "big-row": _rows("T." + "x" * (_MIB - 100), 1),
    "jest": _rows(", ".join(f"T > it {i} x" for i in range(300)), 5),  # non-identifier segments: each one is a text search
}
TEXTS = {"periodic": "x" + "a-" * (_MIB // 2), "ids": " ".join(f"t{i}" for i in range(_MIB // 7)),
         "hashes": "#" * _MIB}


@needs_timer
@pytest.mark.parametrize("led", sorted(LEDGERS))
@pytest.mark.parametrize("text", sorted(TEXTS))
def test_traceability_and_assertion_checks_are_linear(led, text):
    md, sources = LEDGERS[led], {"tests/test_t.py": TEXTS[text], "web/T.test.ts": TEXTS[text]}
    rows = parse_section3(md)
    assert _timed(parse_section3, md) < _BOUND
    assert _timed(check_traceability, rows, sources, 500) < _BOUND
    assert _timed(gate.check_test_assertions, rows, sources) < _BOUND
    assert _timed(scan_assertions, sources, DEFAULT_TESTS) < _BOUND


@needs_timer
def test_test_present_is_linear_on_a_periodic_text():
    text = {"t.ts": "x" + "a-" * (_MIB // 2)}
    seg = "a-" * (gate.MAX_TEST_ID // 2 - 1)
    # 8 lookups (0.33 s CPU, 6x under the bound; 20 took 0.82 s): each lookup on its own is what a quadratic search blows up
    assert _timed(lambda: [test_present(seg + str(i), text) for i in range(8)]) < _BOUND


@needs_timer
def test_python_assertion_check_is_linear_on_a_big_test_file():
    src = "".join(f"def test_{i}():\n    pass\n" for i in range(_MIB // 26))
    rows = [{"ac": "AC-1", "tests": [f"test_{i}" for i in range(gate.MAX_TEST_IDS)], "evidence": "", "status": ""}]
    assert _timed(gate.check_test_assertions, rows, {"tests/test_big.py": src}) < _BOUND


GLOBS = {"stacked": "**/" * 66 + "*b", "stars": "*a" * 99 + "b", "doublestar": "/".join(["**", "a"] * 39), "classes": "[ab]*" * 40, "mixed": "**/" + "a*" * 98}


@needs_timer
@pytest.mark.parametrize("name", sorted(GLOBS))
def test_glob_match_is_linear(name):
    pat = GLOBS[name]
    assert gate.valid_glob(pat)
    paths = ["a" * 4000, "a/" * 2000 + "c", ("a" * 200 + "/") * 20, "/".join(["a"] * 2000)]
    assert _timed(lambda: [gate._glob_match(p, pat) for p in paths]) < _BOUND


def test_stacked_doublestars_are_one_token():
    pat = "**/" * 66 + "*b"
    assert gate.valid_glob(pat) and gate._glob_tokens(pat) == gate._glob_tokens("**/*b")
    path = "/".join(["a"] * 2000) + "/xb"
    t0 = time.process_time()
    assert gate._glob_match(path, pat) and not gate._glob_match(path + "c", pat)
    assert time.process_time() - t0 < 1.0


# ---- DF1 (live run A7-12, D-015): --summary-out has the --plan-out guards ----
def test_summary_out_never_writes_through_a_planted_symlink(tmp_path, monkeypatch, capsys):
    from skj.__main__ import main
    _cli_repo(tmp_path, monkeypatch)
    outside = tmp_path.parent / ("%s-outside.md" % tmp_path.name)
    outside.write_text("KEEP")
    (tmp_path / ".skj").mkdir()
    os.symlink(outside, tmp_path / ".skj" / "link.md")
    assert main(["gate", "7", "--offline", "--summary-out", ".skj/link.md"], client_factory=_no_client) == 0
    assert outside.read_text() == "KEEP"  # the file outside was overwritten in the live run
    link = tmp_path / ".skj" / "link.md"
    assert not link.is_symlink() and link.read_text().startswith("### skj gate")


@pytest.mark.parametrize("target", [".git/hooks/post-checkout", ".GIT/x.md", "sub/.git/s.md"])
def test_summary_out_inside_a_git_directory_is_a_usage_error(tmp_path, monkeypatch, capsys, target):
    from skj.__main__ import main
    _cli_repo(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as e:
        main(["gate", "7", "--offline", "--summary-out", target], client_factory=_no_client)
    assert e.value.code == 2 and ".git" in capsys.readouterr().err
    assert not (tmp_path / target).exists()


@pytest.mark.parametrize("flag", ["--summary-out", "--plan-out"])
def test_df3_a_symlinked_parent_directory_is_refused_like_the_file(tmp_path, monkeypatch, capsys, flag):
    """Review m3: a committed `.skj -> <elsewhere>` and `--summary-out .skj/authorized_keys` replaced the file elsewhere."""
    from skj.__main__ import main
    _cli_repo(tmp_path, monkeypatch)
    elsewhere = tmp_path.parent / ("%s-elsewhere" % tmp_path.name)
    elsewhere.mkdir()
    (elsewhere / "authorized_keys").write_text("KEEP")
    os.symlink(elsewhere, tmp_path / ".skj")
    argv = ["gate", "7", "--offline", flag, ".skj/authorized_keys"] if flag == "--summary-out" else \
        ["push", "7", flag, ".skj/authorized_keys"]
    with pytest.raises(SystemExit) as e:
        main(argv, client_factory=_no_client)
    assert e.value.code == 2 and "symbolic link" in capsys.readouterr().err
    assert (elsewhere / "authorized_keys").read_text() == "KEEP"
    out = tmp_path.parent / ("%s-out" % tmp_path.name)  # a real directory outside the checkout ($RUNNER_TEMP) stays allowed
    assert main(["gate", "7", "--offline", "--summary-out", str(out / "s.md")], client_factory=_no_client) == 0 and (out / "s.md").exists()


@pytest.mark.parametrize("flag", ["--summary-out", "--plan-out"])
@pytest.mark.parametrize("path", ["L/../.ssh/authorized_keys", "./L/../.ssh/authorized_keys", "sub/L/../../.ssh/authorized_keys",
                                  "M/../../../{name}-home/.ssh/authorized_keys"])  # the last is lexically outside the checkout
def test_df4_dot_dot_after_a_symlinked_directory_is_refused(tmp_path, monkeypatch, capsys, flag, path):
    """Re-review N3: `abspath` dropped `L/..` before the link was looked at, while the kernel follows `L` first."""
    from skj.__main__ import main
    _cli_repo(tmp_path, monkeypatch)
    home = tmp_path.parent / ("%s-home" % tmp_path.name)
    (home / ".config" / "x").mkdir(parents=True)
    (home / ".ssh").mkdir()
    (home / ".ssh" / "authorized_keys").write_text("KEEP")
    (tmp_path / "sub").mkdir()
    os.symlink(home / ".config", tmp_path / "L")  # a committed link: L/.. is `home`, not the checkout
    os.symlink(home / ".config" / "x", tmp_path / "sub" / "L")  # sub/L/../.. is `home` too
    os.symlink(home / ".config" / "x", tmp_path / "M")  # M/../../.. is home's parent; lexically it is the checkout's grandparent
    path = path.format(name=tmp_path.name)
    argv = ["gate", "7", "--offline", flag, path] if flag == "--summary-out" else ["push", "7", flag, path]
    with pytest.raises(SystemExit) as e:
        main(argv, client_factory=_no_client)
    assert e.value.code == 2 and "symbolic link" in capsys.readouterr().err
    assert (home / ".ssh" / "authorized_keys").read_text() == "KEEP"


def test_df3_a_plain_file_name_and_a_real_subdirectory_are_still_accepted(tmp_path, monkeypatch):
    from skj.__main__ import _plan_arg
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    assert [_plan_arg(v) for v in ("plan.md", "sub/plan.md", "./sub/../plan.md", "new/dir/p.md")] == \
        ["plan.md", "sub/plan.md", "./sub/../plan.md", "new/dir/p.md"]


def test_df5_dot_dot_under_a_symlinked_temp_root_outside_the_checkout_is_accepted(tmp_path, monkeypatch):
    """Re-review D6: `/tmp/x/../plan.md` on macOS (/tmp -> /private/tmp): the link is above the `..`, so the kernel and the lexical
    reading land in the same directory; only a `..` right after a link (`L/..`) is refused."""
    from skj.__main__ import _plan_arg
    (tmp_path / "real" / "x").mkdir(parents=True)
    (tmp_path / "cwd").mkdir()
    os.symlink(tmp_path / "real", tmp_path / "tmp")  # a symlinked temp root
    monkeypatch.chdir(tmp_path / "cwd")
    for p in (tmp_path / "tmp" / "x" / ".." / "plan.md", tmp_path / "tmp" / "new" / ".." / "plan.md"):
        assert _plan_arg(str(p)) == str(p)
    os.symlink(tmp_path / "real" / "x", tmp_path / "far")
    with pytest.raises(argparse.ArgumentTypeError):
        _plan_arg(str(tmp_path / "far" / ".." / "plan.md"))  # `..` right after a link that lands elsewhere: still refused
