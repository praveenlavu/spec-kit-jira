"""R4: the DoD ledger `dod/DoD_<KEY>.md`, seeded from the story description's acceptance-criteria section (ADF).

The ledger grammar (five sections, the `### Acceptance Criteria` block, header lines, the splice rules of a refresh,
fences and comments that hide headings) is the fork source's, rebased on ADF fixtures. All data is fictional.
"""
import hashlib
import json
import os
import subprocess
import time

import pytest

from fakejira import FakeJira
from skj import adf, dod
from skj.dod import (SECTION2, DodExists, MissingAC, UnsafePath, ac_hash, ac_lines, ac_section, ac_stats, ledger_ac_hash,
                     ledger_path, ledger_spec_dir, render, section, seed, unclosed)

SITE = "example-site.atlassian.net"
HEADING = "Acceptance criteria"


# --- hand-built ADF ------------------------------------------------------------------------------------------------------

def t(s, *marks):
    n = {"type": "text", "text": s}
    if marks:
        n["marks"] = [{"type": m} for m in marks]
    return n


def p(*parts):
    return {"type": "paragraph", "content": [t(x) if isinstance(x, str) else x for x in parts]}


def h(level, *parts):
    return {"type": "heading", "attrs": {"level": level}, "content": [t(x) if isinstance(x, str) else x for x in parts]}


def ul(*items):
    return {"type": "bulletList", "content": [{"type": "listItem", "content": [p(i) if isinstance(i, str) else i]} for i in items]}


def ol(*items):
    return {"type": "orderedList", "content": [{"type": "listItem", "content": [p(i)]} for i in items]}


def code(s, lang="text"):
    return {"type": "codeBlock", "attrs": {"language": lang}, "content": [t(s)]}


def doc(*blocks):
    return {"version": 1, "type": "doc", "content": list(blocks)}


def story_doc(*ac, heading=HEADING, level=2):
    return doc(p("Some context for the story."), h(level, heading), *ac, h(level, "Notes"), p("after the section"))


def issue(*ac, summary="T", itype="Story", status="To Do", key="SPK-1", iid="10000"):
    return {"key": key, "id": iid, "fields": {"summary": summary, "issuetype": {"name": itype}, "status": {"name": status},
                                              "description": story_doc(*ac) if ac else None}}


def md_of(*ac, **kw):
    """The ledger rendered for a story whose AC section holds `ac` blocks."""
    i = issue(*ac, **{k: v for k, v in kw.items() if k in ("summary", "itype", "status")})
    return render(i, ac_section(i["fields"]["description"], HEADING), SITE, **{k: v for k, v in kw.items() if k in ("synced", "spec_dir")})


FULL_AC = ("Given a member with an active plan, when the nightly job runs, then eligibility is recalculated and written "
           "back to the record. Given a lapsed plan, the member is excluded and an audit row is written.")

FIVE = ["- [ ] every AC mapped to a passing real test", "- [ ] coverage meets the project's bar", "- [ ] no assertion-free tests",
        "- [ ] all assumptions / questions / edge cases resolved", "- [ ] gates green"]


def write(tmp_path, i, refresh=False, synced="2026-11-08", spec_dir=""):
    """dod._write as seed uses it, for a story dict `i` (no client)."""
    path = ledger_path(tmp_path, i["key"])
    return dod._write(path, i, ac_section(i["fields"]["description"], HEADING), SITE, synced, refresh, spec_dir)


def s5(md):
    return md[md.index("\n## 5. Definition of Done") + 1:]  # line-anchored: an escaped copy in section 1 is not it


def boxes(md):
    """The checkbox lines of the parsed section 5 (what `done` will read)."""
    return [x for x in section(md, 5).splitlines() if x.startswith("- [")]


# --- the fake and the CLI ------------------------------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo with a config, cwd inside it; returns its top level."""
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, stdin=subprocess.DEVNULL)
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK"}))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def cfg():
    from skj.config import Config
    return Config(SITE, "SPK")


def fake_with(description, summary="T"):
    f = FakeJira()
    f.add_issue(summary, description=description)
    return f


def cli(fake, *args):
    from skj.__main__ import main
    return main(["dod", *args], client_factory=fake.client)


# --- R4 named tests ------------------------------------------------------------------------------------------------------

def test_seed_copies_ac_verbatim(tmp_path):
    f = fake_with(story_doc(ul("Given a member", "Then the plan is shown"), p("And an ", t("audit", "strong"), " row")))
    path = seed(f.client(), cfg(), "SPK-1", root=tmp_path, synced="2026-11-08")
    assert path == tmp_path / "dod" / "DoD_SPK-1.md"
    md = path.read_text(encoding="utf-8")
    want = "- Given a member\n- Then the plan is shown\n\nAnd an **audit** row"
    assert ("### Acceptance Criteria\n" + want + "\n\n## 2. Brainstorm") in md.replace(md[md.index("> **Thin"):md.index("- Given")], "")
    assert "Some context" not in md and "after the section" not in md  # only the section under the heading
    assert ledger_ac_hash(md) == ac_hash(want) == hashlib.sha256(want.encode("utf-8")).hexdigest()
    assert ac_lines(md) == ["Given a member", "Then the plan is shown", "And an **audit** row"]
    # read-only and exactly one GET with explicit fields (no people, no comments)
    assert f.writes() == [] and [(e["method"], e["path"]) for e in f.log] == [("GET", "/issue/SPK-1")]
    assert f.log[0]["query"] == {"fields": "summary,description,issuetype,status,project"}


def test_missing_heading_refused(tmp_path):
    msg = ("the story's description has no 'Acceptance criteria' heading: refused (the kit never invents acceptance criteria; "
           "add the section in Jira, or set ac_heading in skj.config.json)")
    for d in (None, doc(), doc(p("Acceptance criteria"), ul("x")), doc(h(2, "Acceptance Criteria"), ul("x")),
              doc(h(2, "Acceptance criteria:"), ul("x")), doc(ul(h(2, HEADING)), ul("x")),
              doc({"type": "blockquote", "content": [h(2, HEADING)]}, ul("x"))):
        with pytest.raises(MissingAC) as e:
            ac_section(d, HEADING)
        assert str(e.value) == msg
    f = fake_with(doc(p("no criteria here")))
    with pytest.raises(MissingAC) as e:
        seed(f.client(), cfg(), "SPK-1", root=tmp_path)
    assert str(e.value) == msg and not (tmp_path / "dod").exists() and f.writes() == []


def test_ac_hash_stable_under_local_id():
    plain = story_doc(ul("Given a", "Then b"), p("x ", t("y", "strong", "code")))
    tagged = json.loads(json.dumps(plain))
    for k, n in enumerate(adf_nodes(tagged)):
        n.setdefault("attrs", {})["localId"] = "aaaaaaaa-1111-2222-3333-%012d" % k
        for m in n.get("marks", []):
            m["attrs"] = {"localId": "m%d" % k}
    tagged["content"][3]["content"][1]["marks"].reverse()  # mark order differs too
    assert tagged != plain
    assert ac_section(tagged, HEADING) == ac_section(plain, HEADING)
    assert ac_hash(ac_section(tagged, HEADING)) == ac_hash(ac_section(plain, HEADING))
    # a real edit changes it
    assert ac_hash(ac_section(story_doc(ul("Given a", "Then c")), HEADING)) != ac_hash(ac_section(plain, HEADING))


def adf_nodes(n):
    out = [n]
    for c in n.get("content", []):
        out += adf_nodes(c)
    return out


@pytest.mark.parametrize("key", ["spk-1", " SPK-1", "SPK-01", "SPK-0", "SPK-1/../../x", "../SPK-1", "SPK-1\n", "SPK-١", "SPK-1.md",
                                 "SPK-1;touch x", "", None])
def test_ledger_name_path_safe(tmp_path, key):
    with pytest.raises(ValueError):
        ledger_path(tmp_path, key)
    f = fake_with(story_doc(ul("x")))
    with pytest.raises(ValueError):
        seed(f.client(), cfg(), key, root=tmp_path)
    assert f.log == [] and not (tmp_path / "dod").exists()


def test_ledger_path_refuses_symlinks_and_non_files(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "repo"
    root.mkdir()
    (root / "dod").symlink_to(outside, target_is_directory=True)
    f = fake_with(story_doc(ul("x")))
    with pytest.raises(UnsafePath, match="symbolic link"):
        seed(f.client(), cfg(), "SPK-1", root=root)
    assert list(outside.iterdir()) == [] and f.log == []
    (root / "dod").unlink()
    (root / "dod").mkdir()
    target = outside / "victim.md"
    target.write_text("keep")
    (root / "dod" / "DoD_SPK-1.md").symlink_to(target)
    for refresh in (False, True):
        with pytest.raises(UnsafePath, match="symbolic link"):
            seed(f.client(), cfg(), "SPK-1", root=root, refresh=refresh)
    assert target.read_text() == "keep"
    (root / "dod" / "DoD_SPK-1.md").unlink()
    (root / "dod" / "DoD_SPK-1.md").mkdir()
    with pytest.raises(UnsafePath, match="not a regular file"):
        seed(f.client(), cfg(), "SPK-1", root=root, refresh=True)
    (root / "dod" / "DoD_SPK-1.md").rmdir()
    (root / "dod").rmdir()
    (root / "dod").write_text("a file")
    with pytest.raises(UnsafePath, match="not a directory"):
        seed(f.client(), cfg(), "SPK-1", root=root)
    assert f.log == []
    # a symlinked repository root is resolved first: the ledger lands in the real dod/, never through a link inside it
    (root / "dod").unlink()
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    path = seed(f.client(), cfg(), "SPK-1", root=link)
    assert path == root.resolve() / "dod" / "DoD_SPK-1.md" and path.is_file()


def test_refresh_keeps_section_5(tmp_path):
    p_ = write(tmp_path, issue(ul("old AC")))
    p_.write_text(p_.read_text().replace("- [ ] gates green", "- [x] gates green").replace("- [ ] no assertion-free tests",
                                                                                           "- [ ] no assertion-free tests MINE"))
    before = s5(p_.read_text())
    write(tmp_path, issue(ul("new AC"), summary="new"), refresh=True, synced="2026-11-09")
    out = p_.read_text()
    assert "SPK-1: new" in out and "new AC" in out and "old AC" not in out and s5(out) == before
    write(tmp_path, issue(ul("new AC")), refresh=True)
    assert s5(p_.read_text()) == before


def test_oversize_description_refused(tmp_path):
    big = story_doc(p("x" * (adf.MAX_BODY + 10)))
    with pytest.raises(adf.AdfError, match="1 MiB"):
        ac_section(big, HEADING)
    # the cap is on the whole description, not only on the section
    outside = doc(h(2, HEADING), ul("x"), h(2, "Notes"), p("y" * (adf.MAX_BODY + 10)))
    with pytest.raises(adf.AdfError, match="1 MiB"):
        ac_section(outside, HEADING)
    f = fake_with(big)
    with pytest.raises(adf.AdfError, match="1 MiB"):
        seed(f.client(), cfg(), "SPK-1", root=tmp_path)
    assert not (tmp_path / "dod").exists() and f.writes() == []


# --- ac_section ----------------------------------------------------------------------------------------------------------

def test_ac_section_bounds():
    # runs until the next heading of the same or a higher level; lower-level headings stay in (escaped in the ledger)
    d = doc(h(2, HEADING), p("a"), h(3, "Edge cases"), p("b"), h(1, "Top"), p("c"))
    assert ac_section(d, HEADING) == "a\n\n### Edge cases\n\nb"
    assert ac_section(doc(h(3, HEADING), p("a"), h(2, "Up"), p("b")), HEADING) == "a"
    assert ac_section(doc(h(1, HEADING), p("a"), h(6, "deep"), p("b")), HEADING) == "a\n\n###### deep\n\nb"
    assert ac_section(doc(h(2, HEADING), p("last")), HEADING) == "last"
    # the match is the heading's own text, exact after strip; marks and localId on it do not matter
    assert ac_section(doc(h(2, t("  Acceptance criteria ", "strong")), p("a")), HEADING) == "a"
    assert ac_section(doc(h(2, "Acceptance ", t("criteria", "strong")), p("a")), HEADING) == "a"
    assert ac_section(doc(h(2, "AC"), p("a")), "AC") == "a"


def test_ac_section_refuses_empty_duplicate_and_unknown():
    with pytest.raises(MissingAC, match="nothing under"):
        ac_section(doc(h(2, HEADING), h(2, "Next"), p("x")), HEADING)
    with pytest.raises(MissingAC, match="more than one"):
        ac_section(doc(h(2, HEADING), p("a"), h(2, HEADING), p("b")), HEADING)
    with pytest.raises(adf.AdfUnknown, match="panel"):
        ac_section(doc(h(2, HEADING), {"type": "panel", "content": [p("x")]}), HEADING)
    with pytest.raises(adf.AdfUnknown):
        ac_section(doc(h(2, HEADING), p("a\u2028b")), HEADING)  # a line break the reader did not put there
    for bad in ("Acceptance criteria", [], {"type": "doc", "version": 2, "content": []}, {"type": "doc", "content": []},
                {"type": "doc", "version": 1, "content": {}}):
        with pytest.raises(adf.AdfError):
            ac_section(bad, HEADING)
    # an unknown node outside the section is not read
    assert ac_section(doc(h(2, HEADING), p("a"), h(2, "Other"), {"type": "panel"}), HEADING) == "a"


# --- the ledger grammar (rebased) ----------------------------------------------------------------------------------------

def test_render_matches_the_reference_ledger():
    """R4-AC5: the whole ledger for a plain story, byte for byte (the fork source's template with Jira names)."""
    md = md_of(ul(FULL_AC), synced="2026-11-08")
    assert md == (
        "# DoD — SPK-1: T\n"
        "\n"
        "- **Jira:** https://example-site.atlassian.net/browse/SPK-1\n"
        "- **Key:** SPK-1\n"
        "- **Id:** 10000\n"
        "- **Type / State:** Story / To Do\n"
        "- **Last synced:** 2026-11-08\n"
        "- **AC hash:** %s\n"
        "- **Spec dir:** _(set by speckit.jira-lifecycle.pull)_\n"
        "\n"
        "## 1. Seeded from Jira (verbatim: the description's acceptance criteria section, ADF converted to Markdown — "
        "do not edit; refresh via `skj dod refresh SPK-1`)\n"
        "\n"
        "### Acceptance Criteria\n"
        "- %s\n"
        "\n"
        "## 2. Brainstorm & plan  (fill before building — per discipline)\n"
        "- **Approach / chosen option + why:** _(fill in)_\n"
        "- **Assumptions:** _(fill in)_ Each is also recorded in Decisions_SPK-1.md (`skj decisions SPK-1 --add`).\n"
        "- **Open questions → answers:** _(fill in as they arise)_\n"
        "  Format, one line each: `- [ ] Q1: <question> → _(open)_ · owner: PO`; answered: `- [x] Q1: <question> → <answer> · owner: PO`\n"
        "- **Use cases & edge cases:** _(fill in — each: covered-by test / N/A + why)_\n"
        "\n"
        "## 3. AC → tests (traceability)  (tag each AC as AC-1, AC-2…)\n"
        "\n"
        "Tests are free-form IDs: `Class.method`, `path::test_name` or `describe > it`.\n"
        "\n"
        "| AC | acceptance criterion | tests | commit/PR | status |\n"
        "|---|---|---|---|---|\n"
        "| AC-1 | _(what it must do)_ | _(test IDs, …)_ | _(commit / PR)_ | _(red→green)_ |\n"
        "\n"
        "## 4. Gate & review log  (appended by speckit.jira-lifecycle.gate and speckit.jira-lifecycle.review)\n"
        "_(none yet)_\n"
        "\n"
        "## 5. Definition of Done  (all ✅ to close)\n"
        + "\n".join(FIVE) + "\n") % (ac_hash("- " + FULL_AC), FULL_AC)


def test_render_has_all_five_sections():
    md = md_of(ul("x"))
    for marker in ["## 1. Seeded from Jira", "## 2. Brainstorm & plan", "## 3. AC", "## 4. Gate & review log", "## 5. Definition of Done"]:
        assert marker in md
    assert "coverage meets the project's bar" in md and "≥ 90%" not in md


def test_render_is_deterministic():
    assert md_of(ul("x"), synced="d") == md_of(ul("x"), synced="d")


def test_thin_ac_gets_callout_and_stays_verbatim():
    md = md_of(p("should work"))
    assert "Thin acceptance criteria** (11 chars)" in md and "should work" in md
    assert md.count("> **Thin") == 1 and "consider improving the story in Jira first" in md


def test_full_ac_has_no_callout():
    assert len(FULL_AC) >= dod.AC_THIN_CHARS
    md = md_of(p(FULL_AC))
    assert "Thin acceptance criteria" not in md and "eligibility is recalculated" in md


def test_ac_stats_measures_rendered_text_not_attributes():
    padded = story_doc({"type": "paragraph", "attrs": {"localId": "x" * 400}, "content": [t("too short")]})
    stats = ac_stats(ac_section(padded, HEADING))
    assert stats == {"ac_chars": len("too short"), "ac_thin": True}
    assert ac_stats(ac_section(story_doc(p(FULL_AC)), HEADING)) == {"ac_chars": len(FULL_AC), "ac_thin": False}


def test_ac_chars_ignore_list_markers():
    md_ac = ac_section(story_doc(ul("first thing", "second thing")), HEADING)
    want = len("first thing") + len("second thing")
    assert ac_stats(md_ac)["ac_chars"] == want == sum(map(len, ac_lines(md_of(ul("first thing", "second thing")))))


def test_write_creates_file(tmp_path):
    p_ = write(tmp_path, issue(ul("x")))
    assert p_ == tmp_path / "dod" / "DoD_SPK-1.md" and p_.name == dod_name("SPK-1")
    assert "## 1. Seeded from Jira" in p_.read_text() and list(p_.parent.iterdir()) == [p_]


def dod_name(key):
    from skj.keys import dod_name as n
    return n(key)


def test_write_refuses_clobber_without_refresh(tmp_path):
    p_ = write(tmp_path, issue(ul("x")))
    before = p_.read_bytes()
    with pytest.raises(DodExists, match="skj dod refresh SPK-1"):
        write(tmp_path, issue(ul("y")))
    assert p_.read_bytes() == before
    f = fake_with(story_doc(ul("y")))
    with pytest.raises(DodExists):
        seed(f.client(), cfg(), "SPK-1", root=tmp_path)
    assert f.log == []  # refused before any request


def test_refresh_replaces_section1_preserves_authored(tmp_path):
    p_ = write(tmp_path, issue(ul("old AC")))
    p_.write_text(p_.read_text().replace("_(fill in)_", "MY REAL PLAN", 1))
    write(tmp_path, issue(ul("new AC")), refresh=True, synced="2026-11-09")
    out = p_.read_text()
    assert "new AC" in out and "old AC" not in out and "MY REAL PLAN" in out and "Last synced:** 2026-11-09" in out
    assert ledger_ac_hash(out) == ac_hash("- new AC")


def test_refresh_without_a_ledger_is_refused(tmp_path):
    """DF1 (live run A4-14, D-009): `dod refresh` with no ledger wrote one (`action: created`); there is nothing to refresh."""
    with pytest.raises(DodExists, match="no ledger to refresh"):
        write(tmp_path, issue(ul("x")), refresh=True)
    assert not os.path.lexists(ledger_path(tmp_path, "SPK-1"))


def test_refresh_refuses_when_marker_missing(tmp_path):
    p_ = write(tmp_path, issue(ul("x")))
    p_.write_text("hand-mangled file, no section-2 header\nMY WORK")
    with pytest.raises(DodExists, match="refusing to refresh"):
        write(tmp_path, issue(ul("x")), refresh=True)
    assert p_.read_text() == "hand-mangled file, no section-2 header\nMY WORK"


def test_ledger_written_as_utf8(tmp_path):
    text = write(tmp_path, issue(ul("Café ✓"))).read_bytes().decode("utf-8")
    assert "—" in text and "Café ✓" in text and "≥" not in text


def test_section3_has_ac_tag_convention_and_evidence_column():
    md = md_of(ul("x"))
    assert "| AC | acceptance criterion | tests | commit/PR | status |" in md and "|---|---|---|---|---|" in md
    assert "AC-1" in md and "tag each AC" in md and "`path::test_name`" in md and "`describe > it`" in md


def test_ac_hash_empty_and_non_ascii():
    assert ac_hash("") == hashlib.sha256(b"").hexdigest()
    assert ac_hash("Café ✓") == hashlib.sha256("Café ✓".encode("utf-8")).hexdigest()


def test_ac_hash_stable_across_adf_variants():
    a = story_doc(p("A & ", t("B", "strong")))
    b = story_doc({"type": "paragraph", "content": [t("A "), t("& "), {"type": "text", "text": "B", "marks": [{"type": "strong"}]}]})
    assert ac_hash(ac_section(a, HEADING)) == ac_hash(ac_section(b, HEADING))


def test_render_has_hash_and_spec_dir_lines():
    md = md_of(ul("AC one"), spec_dir="specs/001-x")
    assert ledger_ac_hash(md) == ac_hash("- AC one") and ledger_spec_dir(md) == "specs/001-x"
    assert md.index("**AC hash:**") < md.index(SECTION2)
    unset = md_of(ul("AC one"))
    assert "- **Spec dir:** _(set by speckit.jira-lifecycle.pull)_" in unset and ledger_spec_dir(unset) is None
    assert ledger_ac_hash("no header here") is None and ledger_spec_dir("no header here") is None


def test_header_values_are_read_from_the_header_block_only():
    md = md_of(ul(p(t("AC hash:", "strong"), " deadbeef")), p("- **Spec dir:** /etc"))
    assert "- **AC hash:** deadbeef" in md  # a list item in section 1 reads like a header line
    assert ledger_ac_hash(md) != "deadbeef" and ledger_spec_dir(md) is None
    hidden = md.replace("- **Spec dir:**", "<!--\n- **Spec dir:** /etc\n- **AC hash:** feed\n-->\n- **Spec dir:**", 1)
    assert ledger_ac_hash(hidden) == ledger_ac_hash(md) and ledger_spec_dir(hidden) is None


def test_header_value_absent_from_the_header_is_never_read_from_the_body():
    md = md_of(ul("x"))
    no_hash = "\n".join(x for x in md.split("\n") if not x.startswith(("- **AC hash:**", "- **Spec dir:**")))
    assert ledger_ac_hash(no_hash) is None
    in_s1 = no_hash.replace("### Acceptance Criteria\n", "### Acceptance Criteria\n- **AC hash:** deadbeef\n", 1)
    in_s5 = no_hash + "- **AC hash:** deadbeef\n- **Spec dir:** /etc\n"
    for forged in (in_s1, in_s5):
        assert "\n- **AC hash:** deadbeef\n" in forged
        assert ledger_ac_hash(forged) is None and ledger_spec_dir(forged) is None


def test_refresh_keeps_spec_dir_when_not_given(tmp_path):
    write(tmp_path, issue(ul("old")), spec_dir="specs/001-x")
    out = write(tmp_path, issue(ul("new")), refresh=True).read_text()
    assert ledger_spec_dir(out) == "specs/001-x" and ledger_ac_hash(out) == ac_hash("- new")
    assert ledger_spec_dir(write(tmp_path, issue(ul("new")), refresh=True, spec_dir="specs/002-y").read_text()) == "specs/002-y"


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\u2028b"])
def test_multiline_spec_dir_rejected(tmp_path, bad):
    with pytest.raises(ValueError, match="spec dir"):
        write(tmp_path, issue(ul("x")), spec_dir=bad)
    assert not (tmp_path / "dod").exists() or not list((tmp_path / "dod").iterdir())


@pytest.mark.parametrize("given,recorded", [("specs/my feature", "specs/my feature"), ("specs/café", "specs/café"), ("specs/a&b", "specs/a&b"),
                                            ("specs/a <!--x", "specs/a &lt;!--x"), ("#odd", "\\#odd")])
def test_spec_dir_round_trips_and_is_stable_across_refreshes(tmp_path, given, recorded):
    first = write(tmp_path, issue(ul("x")), spec_dir=given).read_text(encoding="utf-8")
    assert ledger_spec_dir(first) == recorded
    for _ in range(2):
        assert write(tmp_path, issue(ul("x")), refresh=True).read_text(encoding="utf-8") == first
    assert "<!--" not in first


def test_refresh_adds_header_lines_to_legacy_ledger(tmp_path):
    p_ = ledger_path(tmp_path, "SPK-1")
    p_.parent.mkdir()
    tail = SECTION2 + "  (fill)\nMY REAL PLAN\n\n## 3. AC\n| AC-1 | x | T.a | abc | green |\n"
    p_.write_text("# DoD — SPK-1: T\n\n- **Last synced:** old\n\n## 1. Seeded\nold\n\n" + tail, encoding="utf-8")
    out = write(tmp_path, issue(ul("new")), refresh=True).read_text(encoding="utf-8")
    assert ledger_ac_hash(out) == ac_hash("- new") and ledger_spec_dir(out) is None
    assert out[out.index(SECTION2):] == tail


def test_refresh_splices_at_real_marker_when_section1_quotes_it(tmp_path):
    p_ = write(tmp_path, issue(h(3, SECTION2), p("old AC")))
    p_.write_text(p_.read_text().replace("_(fill in)_", "MY REAL PLAN", 1))
    out = write(tmp_path, issue(p(SECTION2), p("new AC")), refresh=True).read_text()
    assert "new AC" in out and "old AC" not in out and "MY REAL PLAN" in out
    assert out.count(SECTION2) == 2 and len([x for x in out.splitlines() if x.startswith("## ")]) == 5


def test_refresh_keeps_authored_text_that_mentions_the_marker_inline(tmp_path):
    p_ = write(tmp_path, issue(ul("old AC")))
    text = p_.read_text().replace("_(fill in)_", "MY PLAN, see `%s` above" % SECTION2, 1)
    p_.write_text(text)
    out = write(tmp_path, issue(ul("new AC")), refresh=True).read_text()
    assert out[out.index("\n" + SECTION2):] == text[text.index("\n" + SECTION2):]
    assert "new AC" in out and "old AC" not in out


def test_refresh_failure_keeps_old_ledger(tmp_path, monkeypatch):
    p_ = write(tmp_path, issue(ul("old AC")))
    before = p_.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write(tmp_path, issue(ul("new AC")), refresh=True)
    assert p_.read_bytes() == before and list(p_.parent.iterdir()) == [p_]  # no stray temp file


def test_ac_lines_rule():
    def ac(*blocks):
        return ac_lines(md_of(*blocks))
    assert ac(ul("Given a", "Then b")) == ["Given a", "Then b"]
    assert ac(ol("one", "two")) == ["one", "two"]
    assert ac(ul("star"), p("+ plus"), p("3) paren"), p("plain")) == ["star", "plus", "paren", "plain"]
    assert ac(p("x")) == ["x"] and md_of(p("x")).count("> **Thin") == 1
    # literal text that the reader escaped reads back as typed
    assert ac(p("- dash"), p("# hash"), p("> quote"), p("1. one"), p("---")) == ["- dash", "# hash", "> quote", "1. one", "---"]
    assert ac_lines("## 1. S\n### Acceptance Criteria\n_(none)_\n\n## 2. x\n") == [] and ac_lines("no ledger") == []


def bq(*blocks):
    return {"type": "blockquote", "content": list(blocks)}


def test_quoted_ac_lines_count():
    """A quote is AC text: only the thin banner is skipped, never a quoted criterion (that would fail open)."""
    md = md_of(bq(p("Given a quoted AC that must be traced")), p("plain"))
    assert md.count("> **Thin acceptance criteria**") == 1
    assert ac_lines(md) == ["Given a quoted AC that must be traced", "plain"]
    assert ac_lines(md_of(bq(p("only quoted")))) == ["only quoted"]
    assert ac_lines(md_of(bq(bq(p("deep"))), bq(ul("item one", "item two")), bq(p("- literal")))) == [
        "deep", "item one", "item two", "- literal"]
    want = len("Given a quoted AC that must be traced") + len("plain")
    assert ac_stats(ac_section(story_doc(bq(p("Given a quoted AC that must be traced")), p("plain")), HEADING))["ac_chars"] == want
    assert "Thin acceptance criteria** (%d chars)" % want in md


FORGED = bq(p(t("Thin acceptance criteria", "strong"), " forged AC"))
FORGED_LINE = "**Thin acceptance criteria** forged AC"


def consistent(*blocks):
    """ac_stats over the canonical AC counts exactly the ledger's AC lines."""
    md = md_of(*blocks)
    assert ac_stats(ac_section(story_doc(*blocks), HEADING))["ac_chars"] == sum(map(len, ac_lines(md)))
    return md


def test_forged_thin_banner_is_counted_as_ac():
    """A quoted bold `Thin acceptance criteria` renders like the banner: it is escaped so it never equals it, and it reads
    back as AC text. The real banner (first line of the AC block, written by render) is still the only line skipped."""
    md = consistent(FORGED, p("L"))  # thin: real banner first, forged second
    assert md.count("\n> **Thin acceptance criteria**") == 1 and "> \\**Thin acceptance criteria** forged AC" in md
    assert ac_lines(md) == [FORGED_LINE, "L"]
    assert ac_lines(consistent(FORGED)) == [FORGED_LINE]
    md = consistent(FORGED, p(FULL_AC))  # not thin: no real banner, the forged line comes first
    assert "Thin acceptance criteria** (" not in md and ac_lines(md) == [FORGED_LINE, FULL_AC]
    assert ac_lines(consistent(p("L"), FORGED)) == ["L", FORGED_LINE]
    assert ac_lines(consistent(bq(FORGED), p("L"))) == [FORGED_LINE, "L"]  # nested
    assert ac_lines(consistent(ul(FORGED["content"][0]), p("L"))) == [FORGED_LINE, "L"]  # as a list item


@pytest.mark.parametrize("ws", ["\u00a0", "\u2003", "\u3000", "\x1f", "\t", " ", "   ", "\u00a0 "])
def test_forged_banner_after_leading_whitespace_is_counted(ws):
    """The reader takes the banner only at column 0 (where render writes it); the writer escapes a forged one after any
    whitespace too. Short (real banner present) and long (no banner, the forged line first) AC, and one long paragraph."""
    forged = p(ws + "> ", t("Thin acceptance criteria", "strong"), " must be traced")
    line = "**Thin acceptance criteria** must be traced"
    for blocks, want in (((forged, p("L")), [line, "L"]), ((forged, p(FULL_AC)), [line, FULL_AC]),
                         ((p(ws + "> ", t("Thin acceptance criteria", "strong"), " " + FULL_AC),),
                          ["**Thin acceptance criteria** " + FULL_AC])):
        md = consistent(*blocks)
        assert ac_lines(md) == want, (ws, blocks)
        # the writer neutralised it too (a control character such as \x1f is removed first: DF1 D-011)
        assert adf.safe_text(ws) + "> \\**Thin acceptance criteria**" in md
        assert md.count("\n" + dod.THIN_BANNER) == (1 if ac_stats(ac_section(story_doc(*blocks), HEADING))["ac_thin"] else 0)


def test_cli_ac_chars_count_each_line_once(repo, capsys):
    """`- literal` and a quoted criterion: the printed count is the canonical one (no second marker or quote strip)."""
    blocks = (bq(p("quote")), p("- literal"), bq(p("> nested literal")))
    want = ac_stats(ac_section(story_doc(*blocks), HEADING))["ac_chars"]
    assert want == len("quote") + len("- literal") + len("> nested literal")
    assert cli(fake_with(story_doc(*blocks)), "seed", "1") == 0
    assert "ac_chars: %d" % want in capsys.readouterr().out.splitlines()


def test_banner_counts_only_as_the_first_line_of_the_ac_block():
    """A hand-edited ledger: a raw banner-like line anywhere but first is AC text."""
    md = md_of(p(FULL_AC))
    hand = md.replace("### Acceptance Criteria\n" + FULL_AC, "### Acceptance Criteria\n" + FULL_AC + "\n> **Thin acceptance criteria** hand", 1)
    assert ac_lines(hand) == [FULL_AC, "**Thin acceptance criteria** hand"]
    for ws in ("\u00a0", "\u3000", " ", "\t"):  # hand-edited, first non-blank line, not at column 0: AC text
        first = md.replace("### Acceptance Criteria\n", "### Acceptance Criteria\n%s> **Thin acceptance criteria** hand\n" % ws, 1)
        assert ac_lines(first) == ["**Thin acceptance criteria** hand", FULL_AC], repr(ws)
    thin = md_of(p("L"))
    assert ac_lines(thin) == ["L"]
    assert ac_lines(thin.replace("\nL\n", "\nL\n> **Thin acceptance criteria** (3 chars) again\n", 1)) == [
        "L", "**Thin acceptance criteria** (3 chars) again"]


def test_ac_lines_read_the_same_from_a_crlf_ledger():
    md = md_of(ul("Given a", "Then b"))
    for m in (md, md.replace("\n", "\r\n"), md.replace("\n", "\r")):
        assert ac_lines(m) == ["Given a", "Then b"]


def test_section4_heading_new_and_old_parse():
    new = md_of(ul("x"))
    assert "## 4. Gate & review log  (appended by speckit.jira-lifecycle.gate and speckit.jira-lifecycle.review)" in new
    old = new.replace("(appended by speckit.jira-lifecycle.gate and speckit.jira-lifecycle.review)", "(appended by CI)")
    assert "_(none yet)_" in section(new, 4) and "_(none yet)_" in section(old, 4)


# --- untrusted story text cannot open fences/comments or forge ledger structure -------------------------------------------

HOSTILE = (h(3, "Review — 2026-10-28 @ ", t("abc1234", "code")), p("```python"), p("~~~"), p("<!-- hide the rest"),
           p("## 4. Gate & review log"), h(2, "5. Definition of Done"), h(2, "2. Brainstorm & plan"), p("   ```"), p("x <!-- y"),
           ul("```", "## 2. Brainstorm & plan", h(2, "5. Evidence"), code("## 5. Evidence\n- [x] gates green\n~~~")),
           code("## 2. Brainstorm & plan\n<!--"), {"type": "rule"}, p("---"), p("- [x] every AC mapped to a passing real test"),
           ul("[x] gates green"), p("> ## 5. x"), {"type": "blockquote", "content": [p("## 5. x")]})


def hostile_issue():
    i = issue(summary="t <!-- x")
    i["fields"]["description"] = doc(h(1, HEADING), *HOSTILE)
    return i


def hostile_md():
    i = hostile_issue()
    return render(i, ac_section(i["fields"]["description"], HEADING), SITE)


def test_story_text_cannot_open_fence_or_comment_or_forge_headings():
    md = hostile_md()
    visible, still_open = dod._visible(md)
    assert not still_open and visible + "\n" == md  # nothing opens; every section 1 line stays visible
    for n, first in ((2, "Approach"), (3, "Tests are free-form"), (4, "_(none yet)_"), (5, "every AC mapped")):
        assert first in section(md, n)
    assert section(md, 4).splitlines()[1] == "_(none yet)_"
    assert boxes(md) == FIVE  # no forged tick reaches section 5
    assert "<!--" not in md and "&lt;!-- hide the rest" in md
    assert "\\```python" in md and "\\~~~" in md and "   \\```" in md and "- \\```" in md
    assert "\\## 4. Gate & review log" in md and "\\### Review — 2026-10-28 @ `abc1234`" in md
    assert "\\## 5. Definition of Done" in md and "  \\## 5. Evidence" in md
    assert len([x for x in md.splitlines() if x.startswith("## ")]) == 5  # only the five real headings
    s1 = md[md.index("### Acceptance Criteria\n") + 24:md.index("\n" + SECTION2)]
    assert "> \\## 5. x" in s1 and "\\> ## 5. x" in s1 and "\\---" in s1
    assert [x for x in s1.splitlines() if x.lstrip(" -").startswith(("#", "```", "~~~"))] == []


def test_forged_structure_cannot_alter_parsed_sections(tmp_path):
    """An AC holding `## 5.` headings, ticked boxes, `---` lines and section markers changes neither the parsed sections
    nor what a refresh keeps."""
    clean = md_of(ul("x"))
    md = hostile_md()
    for n in (2, 3, 4, 5):
        assert section(md, n) == section(clean, n)
    assert unclosed(md) is False and dod._section2_at(md) == md.index("\n" + SECTION2) + 1
    p_ = ledger_path(tmp_path, "SPK-1")
    p_.parent.mkdir()
    p_.write_text(clean.replace("- [ ] gates green", "- [x] gates green"), encoding="utf-8")
    before = s5(p_.read_text())
    out = dod._write(p_, hostile_issue(), ac_section(hostile_issue()["fields"]["description"], HEADING), SITE, "", True, "").read_text()
    assert s5(out) == before and boxes(out) == FIVE[:4] + ["- [x] gates green"]
    again = dod._write(p_, issue(ul("calm")), "- calm", SITE, "", True, "").read_text()
    assert s5(again) == before and "Review — 2026" not in again


def test_ac_hash_is_over_canonical_markdown_not_the_escaped_ledger():
    d = story_doc(p("Given ```x"), h(4, "4. Gate & <!-- c"), code("## 2. x"))
    want = "Given ```x\n\n#### 4. Gate & <!-- c\n\n```\n## 2. x\n```"
    assert ac_section(d, HEADING) == want
    i = issue()
    i["fields"]["description"] = d
    md = render(i, want, SITE)
    assert ledger_ac_hash(md) == ac_hash(want) == hashlib.sha256(want.encode("utf-8")).hexdigest()
    assert "\\#### 4. Gate & &lt;!-- c" in md and "\\```\n\\## 2. x\n\\```" in md


def test_ac_lines_and_thin_count_ignore_escapes():
    md = md_of(p("```python"), h(3, "4. Gate"), p("a <!-- b"), ul("~~~"))
    want = ["```python", "### 4. Gate", "a <!-- b", "~~~"]
    assert ac_lines(md) == want
    assert "Thin acceptance criteria** (%d chars)" % sum(map(len, want)) in md


@pytest.mark.parametrize("summary", ["t\r```\n<!--", "t\u2028```", "t\u2029~~~", "t\x0c<!--", "t\x85```", "t\x1b[31m", "t\u202eevil",
                                     "t\u200bzero", "t\x00nul"])
def test_summary_is_one_clean_line(summary):
    md = md_of(ul("x"), summary=summary)
    first = md.splitlines()[0]
    assert md.split("\n")[0] == first and md.split("\n")[1] == ""  # nothing splits the title line, for any line reader
    assert first.startswith("# DoD — SPK-1: t ") and "<!--" not in first
    assert not any(ord(c) < 32 or 0x7f <= ord(c) < 0xa0 or c in "\u2028\u2029\u202e\u200b" for c in first)
    visible, still_open = dod._visible(md)
    assert not still_open and section(md, 4).splitlines()[1] == "_(none yet)_"


def test_summary_is_capped_and_other_header_values_stay_on_one_line():
    md = md_of(ul("x"), summary="s" * 1000, itype="Bug\r\n- **AC hash:** 0", status="Active\n## x <!--")
    lines = md.splitlines()
    assert len(lines[0]) <= len("# DoD — SPK-1: ") + dod.MAX_SUMMARY + 1 and lines[0].endswith("…")
    assert "- **Type / State:** Bug - **AC hash:** 0 / Active ## x &lt;!--" in lines
    assert ledger_ac_hash(md) == ac_hash("- x")


def test_header_values_without_special_characters_are_unchanged():
    md = md_of(ul("x"), summary="Loyalty points", status="In Progress", synced="2026-07-14", spec_dir="specs/001-loyalty")
    assert md.split("\n\n## 1.")[0] == (
        "# DoD — SPK-1: Loyalty points\n\n- **Jira:** https://example-site.atlassian.net/browse/SPK-1\n- **Key:** SPK-1\n- **Id:** 10000\n"
        "- **Type / State:** Story / In Progress\n- **Last synced:** 2026-07-14\n- **AC hash:** %s\n- **Spec dir:** specs/001-loyalty"
        % ac_hash("- x"))


def test_no_people_in_the_ledger(tmp_path):
    d = story_doc(p("ask ", {"type": "mention", "attrs": {"id": "5b10ac8d82e05b22cc7d4ef5", "text": "@Pat Example"}}))
    f = fake_with(d)
    md = seed(f.client(), cfg(), "SPK-1", root=tmp_path).read_text()
    assert "Pat Example" not in md and "5b10ac8d82e05b22cc7d4ef5" not in md and "@<5b10ac8d>" in md
    assert "example.com" not in md  # the fake's user emails are never read


# --- fences and comments that hide headings ------------------------------------------------------------------------------

HIDERS = {"fence": ("```\n", "```\n"), "tilde": ("~~~\n", "~~~\n"), "comment": ("<!--\n", "-->\n")}


def authored(hide=None, extra=""):
    o, c = HIDERS[hide] if hide else ("", "")
    copy = ("%s%s\nFAKE PLAN\n## 5. Definition of Done\n- [x] fake\n%s" % (o, SECTION2, c)) if hide else ""
    return md_of(ul("x")).replace("_(none yet)_", "_(none yet)_\n" + copy + extra, 1).replace("_(fill in)_", "MY REAL PLAN", 1)


@pytest.mark.parametrize("hide", HIDERS)
def test_section_and_marker_ignore_hidden_headings(hide):
    md = authored(hide)
    assert md.index(SECTION2, dod._section2_at(md) + 1) > dod._section2_at(md)
    assert "MY REAL PLAN" in section(md, 2) and "FAKE PLAN" not in section(md, 2)
    assert "- [x] fake" in section(md, 4) and "- [x] fake" not in section(md, 5) and "every AC mapped" in section(md, 5)


@pytest.mark.parametrize("hide", HIDERS)
def test_refresh_keeps_sections_2_to_5_when_a_hidden_copy_of_the_marker_sits_later(tmp_path, hide):
    md = authored(hide)
    p_ = ledger_path(tmp_path, "SPK-1")
    p_.parent.mkdir()
    p_.write_text(md, encoding="utf-8")
    out = write(tmp_path, issue(ul("brand new AC")), refresh=True).read_text(encoding="utf-8")
    assert out[out.index("\n" + SECTION2):] == md[md.index("\n" + SECTION2):]
    assert "brand new AC" in out and "MY REAL PLAN" in out


def test_ac_lines_ignore_a_hidden_ac_heading_and_marker():
    md = md_of(ul("real one"))
    fenced = md.replace("### Acceptance Criteria", "```\n### Acceptance Criteria\nfake\n```\n### Acceptance Criteria", 1)
    assert ac_lines(fenced) == ["real one"]
    assert ac_lines(md + "<!--\n%s\n-->\n" % SECTION2) == ["real one"]


@pytest.mark.parametrize("opener", ["```\n", "~~~\n", "<!--\n"])
def test_refresh_refuses_an_unclosed_opener(tmp_path, opener):
    md = authored(extra=opener)
    p_ = ledger_path(tmp_path, "SPK-1")
    p_.parent.mkdir()
    p_.write_text(md, encoding="utf-8")
    with pytest.raises(DodExists, match="unclosed code fence or comment"):
        write(tmp_path, issue(ul("new")), refresh=True)
    assert p_.read_text(encoding="utf-8") == md


def test_mask_is_length_preserving_and_visible_is_unchanged():
    for m in ("a\r\nb\n```\nx\r\n```\nc <!-- q\nr --> d\n", "x\x0cy ```\nz\n", "```\nunclosed", "t\n\n"):
        assert len(dod._mask(m)) == len(m)
        assert dod._visible(m)[0].count("\n") == len(m.splitlines()) - 1
    assert dod._mask("a <!-- b --> c\n```\nd\n```\n") == "a " + " " * 10 + " c\n" + " " * 3 + "\n \n" + " " * 3 + "\n"


# an unclosed opener in section 1 (a hand-edited ledger): a refresh repairs it, spliced at the FIRST raw marker
def s1(opener):
    md = md_of(ul("x")).replace("_(fill in)_", "MY PLAN", 1)
    return md.replace("### Acceptance Criteria\n", "### Acceptance Criteria\n%s\nsee code\n" % opener, 1)


def marker_copy(md, opener=""):
    return md.replace("- **Assumptions:**", "%s\n%s\n- example\n- **Assumptions:**" % (opener, SECTION2), 1)


def refresh_raw(tmp_path, md, *ac):
    p_ = ledger_path(tmp_path, "SPK-1")
    p_.parent.mkdir(exist_ok=True)
    p_.write_bytes(md.encode("utf-8"))
    write(tmp_path, issue(*(ac or (ul("x"),))), refresh=True)
    return p_.read_bytes().decode("utf-8")


def refused(tmp_path, md):
    p_ = ledger_path(tmp_path, "SPK-1")
    p_.parent.mkdir(exist_ok=True)
    p_.write_bytes(md.encode("utf-8"))
    with pytest.raises(DodExists, match="unclosed code fence or comment"):
        write(tmp_path, issue(ul("x")), refresh=True)
    assert p_.read_bytes().decode("utf-8") == md


@pytest.mark.parametrize("crlf", [False, True])
@pytest.mark.parametrize("opener", ["```", "~~~", "<!--"])
def test_refresh_repairs_an_unclosed_opener_in_section_1(tmp_path, opener, crlf):
    md = s1(opener)
    md = md.replace("\n", "\r\n") if crlf else md
    assert unclosed(md)
    out = refresh_raw(tmp_path, md, ul("new AC"), p(opener))
    assert out[out.index(SECTION2):] == md[md.index(SECTION2):].replace("\r\n", "\n")
    assert not unclosed(out) and "new AC" in out and "MY PLAN" in out and "see code" not in out
    assert ("&lt;!--" if opener == "<!--" else "\\" + opener) in out


def test_refresh_repairs_two_openers_in_section_1(tmp_path):
    md = s1("```\n~~~")
    out = refresh_raw(tmp_path, md)
    assert out[out.index(SECTION2):] == md[md.index(SECTION2):] and "MY PLAN" in out


def test_refresh_repairs_a_ledger_whose_title_left_a_comment_open(tmp_path):
    md = md_of(ul("x")).replace("# DoD — SPK-1: T", "# DoD — SPK-1: T <!-- hand edit", 1)
    assert unclosed(md)
    out = refresh_raw(tmp_path, md)
    assert not unclosed(out) and "hand edit" not in out


@pytest.mark.parametrize("o1,o2", [("```", "~~~"), ("~~~", "```"), ("```", "<!--"), ("<!--", "```")])
def test_refresh_refuses_openers_in_section_1_and_2_before_a_marker_copy(tmp_path, o1, o2):
    refused(tmp_path, marker_copy(s1(o1), o2))


@pytest.mark.parametrize("opener", ["```", "~~~", "<!--"])
def test_refresh_refuses_an_opener_in_section_2_before_a_pasted_marker_copy(tmp_path, opener):
    refused(tmp_path, marker_copy(md_of(ul("x")).replace("_(fill in)_", "MY PLAN", 1), opener))


@pytest.mark.parametrize("opener", ["```", "~~~", "<!--"])
def test_refresh_refuses_an_opener_in_section_4_with_a_marker_copy(tmp_path, opener):
    refused(tmp_path, md_of(ul("x")).replace("_(none yet)_", "%s\n%s" % (opener, SECTION2), 1))


@pytest.mark.parametrize("o1,o2", [("```", "~~~"), ("~~~", "```"), ("<!--", "```"), ("```", "<!--")])
def test_refresh_refuses_when_the_suffix_from_the_marker_is_still_unclosed(tmp_path, o1, o2):
    refused(tmp_path, s1(o1) + "%s\ntrailing\n" % o2)


def test_refusal_messages_point_to_the_way_out(tmp_path):
    p_ = write(tmp_path, issue(ul("x")))
    base = p_.read_text()
    texts = []
    for text, refresh in ((base, False), (base.replace("## 2. Brainstorm", "<!-- ## 2. Brainstorm", 1), True),
                          (base.replace("## 2. Brainstorm & plan", "## 2. other", 1), True)):
        p_.write_text(text)
        with pytest.raises(DodExists) as e:
            write(tmp_path, issue(ul("x")), refresh=refresh)
        texts.append(str(e.value))
    assert "skj dod refresh SPK-1" in texts[0] and "delete" in texts[0]
    assert all("Fix the file by hand" in x for x in texts[1:])


def test_render_is_linear_on_a_large_ac():
    big = story_doc(*[p("```", " <!-- ", "# x " * 150) for _ in range(1000)])
    start = time.monotonic()
    md = render(issue(), ac_section(big, HEADING), SITE)
    assert time.monotonic() - start < 2.0 and not unclosed(md)


# --- the CLI ------------------------------------------------------------------------------------------------------------

def test_cli_seed_then_refresh(repo, capsys):
    f = fake_with(story_doc(ul("Given a", "Then b")), summary="Story one")
    assert cli(f, "seed", "1") == 0
    out = capsys.readouterr().out.splitlines()
    path = repo.resolve() / "dod" / "DoD_SPK-1.md"
    assert out == ["path: %s" % path, "action: created", "ac_chars: %d" % len("Given aThen b"), "ac_thin: yes"]
    first = path.read_text()
    assert cli(f, "seed", "SPK-1") == 2 and "skj dod refresh SPK-1" in capsys.readouterr().err
    assert path.read_text() == first
    assert cli(f, "refresh", "SPK-1", "--spec-dir", "specs/001-one") == 0
    assert capsys.readouterr().out.splitlines()[1] == "action: refreshed"
    assert ledger_spec_dir(path.read_text()) == "specs/001-one"
    assert cli(f, "refresh", "1") == 0 and ledger_spec_dir(path.read_text()) == "specs/001-one"
    assert f.writes() == [] and {e["method"] for e in f.log} == {"GET"}


def test_cli_from_a_subdirectory_writes_at_the_top_level(repo, monkeypatch, capsys):
    (repo / "sub").mkdir()
    monkeypatch.setenv("SKJ_CONFIG", str(repo / "skj.config.json"))
    monkeypatch.chdir(repo / "sub")
    assert cli(fake_with(story_doc(ul("x"))), "seed", "1") == 0
    assert (repo / "dod" / "DoD_SPK-1.md").is_file() and not (repo / "sub" / "dod").exists()


def test_cli_refusals(repo, capsys):
    f = fake_with(doc(p("nothing")))
    for ref in ("OTHER-1", "spk-1", "0", "SPK-1;x"):
        assert cli(f, "seed", ref) == 2 and "not an issue number or a key of project SPK" in capsys.readouterr().err
    assert cli(f, "seed", "1") == 1 and "no 'Acceptance criteria' heading" in capsys.readouterr().err
    assert not (repo / "dod").exists() and f.writes() == []
    with pytest.raises(SystemExit):
        cli(f)  # an action is required


def test_cli_outside_a_git_repository_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK"}))
    monkeypatch.chdir(tmp_path)
    f = fake_with(story_doc(ul("x")))
    assert cli(f, "seed", "1") == 1 and "not inside a git repository" in capsys.readouterr().err
    assert not (tmp_path / "dod").exists() and f.log == []


# --- R4b: every ledger reader is linear-time (a PR controls the ledger the L3 gate parses) ------------------------------------

import random
import re
import signal
from skj import decisions, questions

_MIB = 1 << 20
_BOUND = 2.0  # seconds per call at 1 MiB; the linear readers take well under 0.3 s, the old `_scan` took 16 s on `<!---->` runs


class _TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise _TooSlow()


def _timed(fn, *args):
    """Seconds fn(*args) took; _BOUND when it was cut off there (a quadratic reader must fail the assertion, never hang the suite).
    Exceptions the function raises on purpose (ValueError from the decisions verbs) are part of its normal contract here."""
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
    except ValueError:
        pass
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, old)
    return time.process_time() - t0


# the units of the adversarial inputs: comment openers/closers, fence lookalikes, headings, blanks, escapes, ledger structure
UNITS = {"empty-comment": "<!---->", "open-comment": "<!--", "close-run": "-->", "backtick": "`", "tilde": "~",
         "fence3": "```", "tilde3": "~~~", "fence-info": "```x", "heading": "## ", "newline": "\n", "blank": " ", "unicode-blank": "\u2003",
         "esc-comment": "\\<!--", "esc-fence": "\\```", "section2": "## 2. Brainstorm & plan", "section3": "## 3.",
         "ac-hash": "- **AC hash:** x", "ac-heading": "### Acceptance Criteria", "quote": "> ", "none": "_(none"}
LAYOUTS = ("one", "perline", "short", "wide")


def _payload(unit, layout):
    """About 1 MiB of `unit`: `one` line; `perline` one unit per line; `short` 20000 lines of ~52 characters;
    `wide` 500 lines of ~1990 characters."""
    if layout == "one":
        return unit * (_MIB // len(unit))
    if layout == "perline":
        return "\n".join([unit] * (_MIB // (len(unit) + 1)))
    k = max(1, round((52 if layout == "short" else 1990) / len(unit)))
    line = unit * k
    return "\n".join([line] * (20000 if layout == "short" else 500))


_HEAD = "# DoD — SPK-1: x\n\n- **AC hash:** abc\n- **Spec dir:** specs/x\n\n## 1. Seeded\n\n### Acceptance Criteria\n"
_MARKS = "\n## 2. Brainstorm & plan\n- [ ] Q1: a → _(open)_ · owner: PO\n## 3. AC\n## 4. Gate\n## 5. DoD\n"


def _readers(md):
    """(name, zero-argument call) for every reader of ledger text, `md` being the whole input."""
    d = "- [ ] D1: a → _(open)_ · kind: decision · why: b"
    return [
        ("_scan", lambda: dod._scan(md, "")), ("_scan fill", lambda: dod._scan(md, " ")), ("_visible", lambda: dod._visible(md)),
        ("_mask", lambda: dod._mask(md)), ("unclosed", lambda: dod.unclosed(md)), ("_section2_at", lambda: dod._section2_at(md)),
        ("_repair_at", lambda: dod._repair_at(md)), ("section_span", lambda: dod.section_span(md, 3)),
        ("section 1", lambda: dod.section(md, 1)), ("section 2", lambda: dod.section(md, 2)), ("section 5", lambda: dod.section(md, 5)),
        ("ac_lines", lambda: dod.ac_lines(md)), ("ac_stats", lambda: dod.ac_stats(md)), ("ledger_ac_stats", lambda: dod.ledger_ac_stats(md)),
        ("ledger_ac_hash", lambda: dod.ledger_ac_hash(md)), ("ledger_spec_dir", lambda: dod.ledger_spec_dir(md)),
        ("_unescape", lambda: dod._unescape(md)), ("_esc", lambda: dod._esc(md)),
        ("parse_questions", lambda: questions.parse_questions(md)),
        ("parse_decisions", lambda: decisions.parse_decisions(md)), ("has_decision_lines", lambda: decisions.has_decision_lines(md)),
        ("decisions.add", lambda: decisions.add(md, "decision", "x")),
        ("decisions.resolve", lambda: decisions.resolve(md + "\n" + d + "\n", "D1", "validated", "PO", "")),
        ("decisions.stamp_used", lambda: decisions.stamp_used(md + "\n" + d + "\n", ["D1"], "2026-01-01")),
    ]


# H1b trim (the full grid took 38 s, `perline` 21 s of it): every unit as `one` line and as `wide` lines; `short` (20000 lines) only where a
# line of its own is the shape (fence and heading lines, blank lines); the most lines of all is a run of newlines, kept only as `one` (10^6 lines)
_LINE_UNITS = ("backtick", "tilde", "fence3", "tilde3", "fence-info", "heading", "blank")
_CASES = [(u, l) for u in sorted(UNITS) for l in LAYOUTS if (l in ("one", "wide") or l == "short" and u in _LINE_UNITS)
          and (u != "newline" or l == "one")]


# the readers that differ in code path (the others wrap `_mask` or `_scan` and are in the swept table of the task report)
_CORE = ("_scan", "_visible", "_mask", "section 2", "_section2_at", "_repair_at", "ac_lines", "ac_stats", "ledger_ac_stats",
         "ledger_ac_hash", "_unescape", "_esc", "parse_questions", "parse_decisions")


@pytest.mark.parametrize("unit,layout", _CASES)
def test_r4b_ac1_every_reader_is_linear_on_1mib_adversarial_ledgers(unit, layout):
    pay = _payload(UNITS[unit], layout)
    assert _MIB * 0.85 < len(pay) < _MIB * 1.2
    slow = {n: dt for n, call in _readers(_HEAD + pay + _MARKS) if n in _CORE and (dt := _timed(call)) >= _BOUND}
    assert not slow, slow


@pytest.mark.parametrize("unit", ["empty-comment", "open-comment", "fence3", "heading", "esc-comment", "blank"])
def test_r4b_ac1_every_other_reader_is_linear_with_the_payload_in_section_2(unit):
    md = _HEAD + _MARKS + _payload(UNITS[unit], "short")
    slow = {n: dt for n, call in _readers(md) if (dt := _timed(call)) >= _BOUND}
    assert not slow, slow


def _slow_calls(md, names):
    return {n: dt for n, call in _readers(md) if n in names and (dt := _timed(call)) >= _BOUND}


def test_r4b_ac1_a_header_value_with_a_long_inner_blank_run_is_linear():
    for md in ("- **AC hash:** a" + " " * _MIB + "x\n## 2. Brainstorm & plan\n", "- **Spec dir:** a" + "\t" * _MIB + "x\n",
               "- **AC hash:**" + " " * _MIB + "x\n"):
        assert not _slow_calls(md, ("ledger_ac_hash", "ledger_spec_dir"))
    assert dod.ledger_ac_hash("- **AC hash:**  \t abc  x \t \n") == "abc  x"


def test_r4b_ac1_the_ac_heading_followed_by_a_blank_run_is_linear():
    md = _HEAD.replace("### Acceptance Criteria\n", "### Acceptance Criteria" + " " * _MIB + "x\n") + _MARKS
    assert not _slow_calls(md, ("ac_lines", "ledger_ac_stats"))


def test_r4b_ac1_many_section3_headings_after_an_unclosed_opener_are_linear():
    md = "<!--\n## 2. Brainstorm & plan\n" + "## 3.\n" * (_MIB // 6)
    assert not _slow_calls(md, ("_repair_at",)) and dod._repair_at(md) == -1


def test_r4b_ac1_ac_lines_over_a_huge_ac_block_are_linear():
    for body in ("_(none" + "a" * _MIB, ">" * _MIB, "> " * (_MIB // 2), "-" + " " * _MIB + "x", "1" * _MIB + ".", "\\" * _MIB,
                 "\n".join(["- a"] * (_MIB // 4))):  # (H1b) many AC lines: the shape the trimmed `perline` cases covered
        md = _HEAD + body + _MARKS
        assert not _slow_calls(md, ("ac_lines", "ledger_ac_stats")), body[:8]


# --- R4b-AC2: the rewrite is the old scanner, exactly ---------------------------------------------------------------------------

def _old_scan(md, fill):
    """The R4 implementation, kept verbatim as the oracle (re-copies the rest of the line per comment: quadratic)."""
    out, fence, comment = [], None, False
    for line in md.splitlines():
        if fence:
            if re.fullmatch(r" {0,3}%s{%d,}[ \t]*" % (re.escape(fence[0]), len(fence)), line):
                fence = None
            out.append(fill * len(line))
            continue
        m = None if comment else dod._FENCE_OPEN.match(line)
        if m:
            fence = m.group(1) or m.group(2)
            out.append(fill * len(line))
            continue
        kept, rest = "", line
        while rest:
            if comment:
                end = rest.find("-->")
                if end < 0:
                    kept += fill * len(rest)
                    break
                comment, kept, rest = False, kept + fill * (end + 3), rest[end + 3:]
            else:
                start = rest.find("<!--")
                if start < 0:
                    kept += rest
                    break
                kept, rest, comment = kept + rest[:start] + fill * 4, rest[start + 4:], True
        out.append(kept)
    return out, bool(fence or comment)


def _old_mask(md):
    out, _ = _old_scan(md, " ")
    return "".join(k + ("\r\n" if raw.endswith("\r\n") else "\n" if raw[len(k):] else "")
                   for k, raw in zip(out, md.splitlines(keepends=True)))


def _old_repair_at(md):
    m = dod._SECTION2_RE.search(md)
    if (m and _old_scan(md[:m.start()], "")[1] and not _old_scan(md[m.start():], "")[1]
            and re.search(r"^## 3\..*?^## 4\..*?^## 5\.", _old_mask(md[m.start():]), re.M | re.S)):
        return m.start()
    return -1


def _old_header_value(md, label):
    vis = _old_mask(md)
    h = re.search(r"^## ", vis, re.M)
    m = re.search(r"^- \*\*%s:\*\*[ \t]*(.*?)[ \t]*$" % re.escape(label), vis[:h.start()] if h else vis, re.M)
    return m.group(1) if m else None


_PIECES = ["```", "````", "~~~", "~~~~", "``` py", "   ```", "    ```", "```` ", "``` x`", "~~~ x", "<!--", "-->", "<!---->",
           "<!-- x -->", "a <!-- b --> c <!-- d", "e --> f <!-- g -->", "<!--<!-- -->-->", "--><!--", "\\```", "&lt;!--", "\\<!--",
           "## 2. Brainstorm & plan", "## 3. AC", "## 4. Gate", "## 5. DoD", "## 1. Seeded", "### Acceptance Criteria", "> **Thin acceptance criteria** (5 chars)",
           "- **AC hash:** abc  ", "- **AC hash:**\t x y ", "- **Spec dir:** specs/x", "- [ ] Q1: a → _(open)_ · owner: PO", "text", "", " ", "\t",
           "  - item", "> quoted", "_(none yet)_", "x<!--y", "z-->w", "\u2003 ", "``` `"]
_EOLS = ["\n", "\n", "\n", "\r\n", "\r", "\u2028", "\x0c", "\x85"]


def _random_ledger(rng):
    return "".join(rng.choice(_PIECES) + rng.choice(_EOLS) for _ in range(rng.randint(0, 40)))


def test_r4b_ac2_the_linear_scanner_agrees_with_the_r4_scanner_on_random_ledgers():
    rng, seen_open = random.Random(20261109), 0
    for i in range(600):
        md = _random_ledger(rng)
        for fill in ("", " "):
            assert dod._scan(md, fill) == _old_scan(md, fill), (i, md)
        seen_open += dod.unclosed(md)
        old_lines, old_open = _old_scan(md, "")
        assert dod._mask(md) == _old_mask(md) and dod._visible(md) == ("\n".join(old_lines), old_open) and dod.unclosed(md) is old_open
        assert dod._repair_at(md) == _old_repair_at(md), (i, md)
        for label in ("AC hash", "Spec dir"):
            assert dod._header_value(md, label) == _old_header_value(md, label), (i, label, md)
    assert 100 < seen_open < 500  # the corpus really holds both balanced and unclosed ledgers


def test_r4b_ac2_repair_at_agrees_with_the_r4_pattern_on_every_order_and_subset_of_the_later_headings():
    import itertools
    found = set()
    for opener in ("<!--\n", "```\n", "text\n"):
        for r in range(4):
            for heads in itertools.permutations(("## 3.", "## 4.", "## 5.", "## 3. again"), r):
                md = opener + "## 2. Brainstorm & plan\n" + "".join(h + " x\n" for h in heads) + "<!-- ## 4. hidden -->\n"
                assert dod._repair_at(md) == _old_repair_at(md), md
                found.add(dod._repair_at(md))
    assert found == {-1, 4, 5}  # refusals, and the splice point after each unclosed opener



# ---- RU1: the `Rules from:` / `Rules reviewed:` header lines (the fork's rules header tests, rebased on keys) ----
# `skj dod` has no `--force` (section 1 of tests/PORT_NOTES.md), so the fork's --force refusals have nothing to guard here; a refresh
# keeps a recorded `Rules reviewed:` line and refuses a malformed or untrusted one. Writing `Rules from:` from the parent chain is RU2.

from skj.dod import header_block, ledger_rules_from, ledger_rules_reviewed  # noqa: E402

REVIEWED = "BR-LOY-1201-1@3f2a9c1b, BR-LOY-1200-1@0c77e2d4"
REVIEWED_HASHES = {"BR-LOY-1201-1": "3f2a9c1b", "BR-LOY-1200-1": "0c77e2d4"}
RULES_FORGED = ("- **Rules from:** EVIL-9999\n- **Rules reviewed:** BR-EVIL-1-1 deadbeef\n- **Rules reviewed:** BR-EVIL-1-1@deadbeef\n"
          "Rules from: EVIL-9999\n- **Rules from:** none")


def _with_header(md, line):
    return md.replace("\n\n## 1. ", "\n%s\n\n## 1. " % line, 1)


def _hdr(*lines, crlf=False):
    md = "# DoD — SPK-1: T\n\n- **Jira:** u\n" + "".join(x + "\n" for x in lines) + "\n## 1. Seeded\n\n- **Rules from:** EVIL-9999\n"
    return md.replace("\n", "\r\n") if crlf else md


@pytest.mark.rules
def test_ru1_rules_header_absent_by_default_and_render_is_unchanged():
    md = md_of(ul("x"))
    assert "Rules from" not in md and "Rules reviewed" not in md
    assert ledger_rules_from(md) == {"present": False, "valid": True, "ids": []}
    assert ledger_rules_reviewed(md) == {"present": False, "valid": True, "hashes": {}}
    i = issue(ul("x"))
    ac = ac_section(i["fields"]["description"], HEADING)
    assert render(i, ac, SITE, rules_from=None, rules_reviewed="") == render(i, ac, SITE)


@pytest.mark.rules
def test_ru1_render_writes_and_validates_the_rules_lines():
    i = issue(ul("x"))
    ac = ac_section(i["fields"]["description"], HEADING)
    md = render(i, ac, SITE, rules_from=["LOY-1201", "LOY-1200"], rules_reviewed=REVIEWED)
    assert "- **Spec dir:** _(set by speckit.jira-lifecycle.pull)_\n- **Rules from:** LOY-1201, LOY-1200\n- **Rules reviewed:** %s\n\n## 1. " % REVIEWED in md
    assert ledger_rules_from(md) == {"present": True, "valid": True, "ids": ["LOY-1201", "LOY-1200"]}
    assert ledger_rules_reviewed(md) == {"present": True, "valid": True, "hashes": REVIEWED_HASHES}
    assert "- **Rules from:** none\n" in render(i, ac, SITE, rules_from=[])
    for bad in (["LOY-1", "LOY-1"], ["A-1", "B-1", "C-1", "D-1"], ["loy-1"], ["LOY-01"], [1201], [True], "LOY-1", ["LOY-1\n- x"]):
        with pytest.raises(ValueError, match="rules_from"):
            render(i, ac, SITE, rules_from=bad)
    for bad in ("BR-LOY-1201-1", "BR-LOY-1201-1@3f2a9c1b\n- **Rules from:** X-9", "x", " BR-LOY-1201-1@3f2a9c1b", "BR-loy-1201-1@3f2a9c1b"):
        with pytest.raises(ValueError, match="rules_reviewed"):
            render(i, ac, SITE, rules_from=["LOY-1"], rules_reviewed=bad)
    assert header_block(render(i, ac, SITE, rules_from=["LOY-5", "LOY-4"], rules_reviewed=REVIEWED)).endswith(
        "- **Rules from:** LOY-5, LOY-4\n- **Rules reviewed:** %s\n\n" % REVIEWED)


@pytest.mark.rules
@pytest.mark.parametrize("value", ["../1", "LOY-1e3", "1201", "LOY-1201, LOY-1200, LOY-1100, LOY-1000", "LOY-1201, LOY-1201", "LOY-1201,LOY-1200",
                                   "", "none, LOY-1201", "LOY-0", "LOY-007", "loy-1201", "Loy-1201", "LOY-1201 LOY-1200", "ＬＯＹ-１２０１",
                                   "LOY-1201,", "None", "LOY-1201 ", " LOY-1201", "L-1201", "LOY-12345678901", "BR-LOY-1201-1"])
def test_ru1_invalid_rules_from(value):
    """RU1-AC2 for the ledger header: a lower-case or malformed key in `Rules from:` makes the line invalid, never an id."""
    assert ledger_rules_from(_hdr("- **Rules from:** %s" % value)) == {"present": True, "valid": False, "ids": []}, value


@pytest.mark.rules
def test_ru1_invalid_rules_from_forms_and_valid_ones():
    bad = {"present": True, "valid": False, "ids": []}
    assert ledger_rules_from(_hdr("- **Rules from:** LOY-1201", "- **Rules from:** LOY-1200")) == bad  # two lines
    assert ledger_rules_from(_hdr("- **Rules from:** LOY-1201", "- **Rules from:** LOY-1201")) == bad
    assert ledger_rules_from(_hdr("- **Rules from:**LOY-1201")) == bad
    assert ledger_rules_from(_hdr("- **Rules from**: LOY-1201")) == bad
    assert ledger_rules_from(_hdr("* **rules from:** LOY-1201")) == bad
    assert ledger_rules_from(_hdr("- **Rules from:** LOY-1201 - **Rules from:** LOY-1200")) == bad  # a line break the readers also see
    assert ledger_rules_from(_hdr("<!-- never closed", "- **Rules from:** LOY-1201")) == bad  # an unclosed opener hides the rest of the header
    assert ledger_rules_from(_hdr("- **Rules from:** LOY-1201", "- **Rules from:** none")) == bad
    assert ledger_rules_from(_hdr("- **Rules from:** ABCDEFGHIJ-9999999999, A_1-1, LOY-2")) == {
        "present": True, "valid": True, "ids": ["ABCDEFGHIJ-9999999999", "A_1-1", "LOY-2"]}
    assert ledger_rules_from(_hdr("- **Rules from:** none")) == {"present": True, "valid": True, "ids": []}
    assert ledger_rules_from(_hdr("- **Spec dir:** Rules from: LOY-1201")) == {"present": False, "valid": True, "ids": []}


@pytest.mark.rules
@pytest.mark.parametrize("line", ["- **Rules reviewed:** BR-LOY-1201-1@3f2a9c1b, BR-LOY-1201-1@3f2a9c1b", "- **Rules reviewed:** BR-LOY-1201-1@3F2A9C1B",
                                  "- **Rules reviewed:** BR-LOY-1201-1@3f2a9c1", "- **Rules reviewed:** BR-LOY-1201-1@3f2a9c1b ",
                                  "- **Rules reviewed:** BR-LOY-0-1@3f2a9c1b", "- **Rules reviewed:** BR-LOY-1201-0@3f2a9c1b",
                                  "- **Rules reviewed:** BR-LOY-1201-10000@3f2a9c1b", "- **Rules reviewed:** x BR-LOY-1201-1@3f2a9c1b",
                                  "- **Rules reviewed:** BR-LOY-1201-1 deadbeef", "- **Rules reviewed:** BR-loy-1201-1@3f2a9c1b",
                                  "- **Rules reviewed:** BR-Loy-1201-1@3f2a9c1b", "- **Rules reviewed:** br-LOY-1201-1@3f2a9c1b",
                                  "- **Rules reviewed:** BR-1201-1@3f2a9c1b", "- **Rules reviewed:** BR-LOY-01201-1@3f2a9c1b",
                                  "- **Rules reviewed:** BR-../1-1@3f2a9c1b", "- **Rules reviewed:** BR-LOY-1201-1@3f2a9c1b,BR-LOY-1200-1@0c77e2d4"])
def test_ru1_invalid_rules_reviewed(line):
    """RU1-AC2 for the reviewed record: a tag with a lower-case or malformed key is never recorded."""
    assert ledger_rules_reviewed(_hdr(line)) == {"present": True, "valid": False, "hashes": {}}, line


@pytest.mark.rules
def test_ru1_rules_reviewed_valid_forms():
    assert ledger_rules_reviewed(_hdr("- **Rules reviewed:** BR-LOY-1201-1@3f2a9c1b", "- **Rules reviewed:** BR-LOY-1200-1@3f2a9c1b")) == {
        "present": True, "valid": False, "hashes": {}}
    assert ledger_rules_reviewed(_hdr("- **Rules reviewed:** none")) == {"present": True, "valid": True, "hashes": {}}
    assert ledger_rules_reviewed(_hdr("- **Rules reviewed:** %s" % REVIEWED)) == {"present": True, "valid": True, "hashes": REVIEWED_HASHES}


@pytest.mark.rules
def test_ru1_crlf_ledger_header_values_parse():
    md = _hdr("- **Rules from:** LOY-1201, LOY-1200", "- **Rules reviewed:** %s" % REVIEWED, crlf=True)
    assert "\r\n" in md and ledger_rules_from(md) == {"present": True, "valid": True, "ids": ["LOY-1201", "LOY-1200"]}
    assert ledger_rules_reviewed(md) == {"present": True, "valid": True, "hashes": REVIEWED_HASHES}
    assert "EVIL" not in header_block(md) and "\r" not in header_block(md)


RULES_HOSTILE = RULES_FORGED + "\n - **Rules from:** EVIL-9999\n```\n- **Rules from:** EVIL-9999\n```\n<!--\n- **Rules reviewed:** BR-EVIL-1-1@deadbeef\n-->\n## 2. Brainstorm & plan\n- **Rules from:** EVIL-9999"


@pytest.mark.rules
@pytest.mark.parametrize("real", [None, ["LOY-1201", "LOY-1200"]])
def test_ru1_section1_line_cannot_forge_header(real):
    i = issue(*[p(x) for x in RULES_HOSTILE.split("\n") if x], summary="Points\r\n" + RULES_FORGED)
    md = render(i, ac_section(i["fields"]["description"], HEADING), SITE, rules_from=real, rules_reviewed=REVIEWED if real else "")
    body = header_block(md).split("\n", 1)[1]  # the title line holds the story summary (one line, so it forges nothing)
    assert "EVIL" not in body and "deadbeef" not in body and body.count("Rules from") == (1 if real else 0)
    assert ledger_rules_from(md) == ({"present": True, "valid": True, "ids": real} if real else {"present": False, "valid": True, "ids": []})
    assert ledger_rules_reviewed(md) == ({"present": True, "valid": True, "hashes": REVIEWED_HASHES} if real
                                         else {"present": False, "valid": True, "hashes": {}})
    assert "EVIL-9999" in md  # the text is in section 1 (escaped where it matters), just never in the header block
    for hidden in ("```\n{}\n```", "<!--\n{}\n-->", "<!-- {} -->"):
        hmd = md.replace("\n\n## 1. ", "\n" + hidden.format("- **Rules from:** EVIL-9999\n- **Rules reviewed:** BR-EVIL-1-1@deadbeef") + "\n\n## 1. ", 1)
        assert ledger_rules_from(hmd) == ledger_rules_from(md) and ledger_rules_reviewed(hmd) == ledger_rules_reviewed(md)
    late = "## 3. x\n- **Rules from:** EVIL-9999\n- **Rules reviewed:** BR-EVIL-1-1@deadbeef\n"
    assert ledger_rules_from(md + late) == ledger_rules_from(md) and ledger_rules_reviewed(md + late) == ledger_rules_reviewed(md)


@pytest.mark.rules
def test_ru1_a_summary_naming_the_labels_is_not_a_header_line():
    md = md_of(ul("x"), summary="Rules from: marketing - **Rules from:** LOY-5 Rules reviewed:")
    assert ledger_rules_from(md)["present"] is False and ledger_rules_reviewed(md)["present"] is False


RULES_HIDDEN_H = ["<!--\n## 1. S\n-->", "```\n## 1. S\n```", "~~~\n## 1. S\n~~~", "<!--\n## 1. S"]


@pytest.mark.rules
@pytest.mark.parametrize("hide", RULES_HIDDEN_H)
def test_ru1_a_first_heading_hidden_by_a_comment_or_fence_makes_the_header_untrusted(hide):
    md = "# DoD — SPK-1: T\n\n- **Jira:** u\n- **Rules from:** LOY-1201\n- **Rules reviewed:** %s\n%s\n## 2. Brainstorm & plan\n" % (REVIEWED, hide)
    assert ledger_rules_from(md) == {"present": True, "valid": False, "ids": []}
    assert ledger_rules_reviewed(md) == {"present": True, "valid": False, "hashes": {}}
    ok = md.replace(hide + "\n", "## 1. S\n" + hide + "\n", 1)  # a visible first heading: trusted again
    assert ledger_rules_from(ok)["ids"] == ["LOY-1201"] and ledger_rules_reviewed(ok)["valid"] is True


@pytest.mark.rules
@pytest.mark.parametrize("prefix", ["> ", ">", "> > ", ">   "])
@pytest.mark.parametrize("line,reader", [("- **Rules from:** LOY-1201", ledger_rules_from), ("- **Rules reviewed:** %s" % REVIEWED, ledger_rules_reviewed)])
def test_ru1_a_blockquote_prefixed_header_line_is_invalid_not_absent(prefix, line, reader):
    out = reader(_hdr(prefix + line))
    assert out["present"] is True and out["valid"] is False


@pytest.mark.rules
def test_ru1_a_comment_opened_in_the_summary_and_closed_in_section_1_makes_the_header_untrusted():
    md = "# DoD — SPK-1: T <!--\n\n- **Jira:** u\n\n## 1. Seeded\n-->\n- **Rules reviewed:** BR-EVIL-1-1@deadbeef\n## 2. x\n"
    assert ledger_rules_reviewed(md) == {"present": True, "valid": False, "hashes": {}}
    assert ledger_rules_from(md) == {"present": True, "valid": False, "ids": []}


@pytest.mark.rules
def test_ru1_refresh_keeps_rules_reviewed_byte_for_byte(tmp_path):
    p_ = write(tmp_path, issue(ul("old AC")))
    p_.write_bytes(_with_header(p_.read_text(encoding="utf-8"), "- **Rules reviewed:** %s" % REVIEWED).replace("_(fill in)_", "MY REAL PLAN", 1).encode("utf-8"))
    write(tmp_path, issue(ul("new AC")), refresh=True, synced="2026-11-09")
    out = p_.read_text(encoding="utf-8")
    assert "\n- **Rules reviewed:** %s\n" % REVIEWED in out and out.count("Rules reviewed") == 1 and "MY REAL PLAN" in out and "new AC" in out
    assert ledger_rules_reviewed(out)["hashes"] == REVIEWED_HASHES
    assert out.replace("- **Rules reviewed:** %s\n" % REVIEWED, "") == md_of(ul("new AC"), synced="2026-11-09").replace("_(fill in)_", "MY REAL PLAN", 1)
    write(tmp_path, issue(ul("new AC")), refresh=True, synced="2026-11-09")
    assert p_.read_text(encoding="utf-8") == out  # stable across refreshes


@pytest.mark.rules
def test_ru1_refresh_keeps_none_and_drops_rules_from(tmp_path):
    p_ = write(tmp_path, issue(ul("x")))
    p_.write_bytes(_with_header(p_.read_text(encoding="utf-8"), "- **Rules from:** LOY-7\n- **Rules reviewed:** none").encode("utf-8"))
    out = write(tmp_path, issue(ul("x")), refresh=True).read_text(encoding="utf-8")
    assert "\n- **Rules reviewed:** none\n" in out and "Rules from" not in out  # `Rules from:` is recomputed by the pull (RU2), never copied


@pytest.mark.rules
def test_ru1_refresh_of_a_crlf_ledger_keeps_rules_reviewed(tmp_path):
    p_ = write(tmp_path, issue(ul("x")))
    p_.write_bytes(_with_header(p_.read_text(encoding="utf-8"), "- **Rules reviewed:** %s" % REVIEWED).replace("\n", "\r\n").encode("utf-8"))
    write(tmp_path, issue(ul("x")), refresh=True)
    out = p_.read_bytes().decode("utf-8")
    assert ledger_rules_reviewed(out)["hashes"] == REVIEWED_HASHES and "- **Rules reviewed:** %s" % REVIEWED in out


@pytest.mark.rules
@pytest.mark.parametrize("bad", ["BR-LOY-1201-1@3f2a9c1b,BR-LOY-1200-1@0c77e2d4", "BR-LOY-1201-1@3f2a9c1b, BR-LOY-1201-1@0c77e2d4", "BR-LOY-1201-1",
                                 "BR-LOY-1-1@deadbeef0", "br-LOY-1201-1@3f2a9c1b", "BR-loy-1201-1@3f2a9c1b", "BR-LOY-1201-01@3f2a9c1b",
                                 "BR-../1-1@3f2a9c1b", "", "BR-1201-1@3f2a9c1b"])
def test_ru1_refresh_refuses_an_invalid_rules_reviewed_line(tmp_path, bad):
    p_ = write(tmp_path, issue(ul("x")))
    md = _with_header(p_.read_text(encoding="utf-8"), "- **Rules reviewed:** %s" % bad)
    p_.write_bytes(md.encode("utf-8"))
    with pytest.raises(DodExists, match="Rules reviewed") as e:
        write(tmp_path, issue(ul("y")), refresh=True)
    assert p_.read_bytes().decode("utf-8") == md and list(p_.parent.iterdir()) == [p_]  # untouched, no temp file left
    assert "deadbeef" not in str(e.value) and "../" not in str(e.value)


@pytest.mark.rules
def test_ru1_refresh_refuses_two_rules_reviewed_lines_and_a_quoted_one(tmp_path):
    p_ = write(tmp_path, issue(ul("x")))
    base = p_.read_text(encoding="utf-8")
    for line in ("- **Rules reviewed:** BR-LOY-1-1@3f2a9c1b\n- **Rules reviewed:** BR-LOY-2-1@0c77e2d4", "> - **Rules reviewed:** %s" % REVIEWED):
        md = _with_header(base, line)
        p_.write_bytes(md.encode("utf-8"))
        with pytest.raises(DodExists, match="Rules reviewed"):
            write(tmp_path, issue(ul("y")), refresh=True)
        assert p_.read_bytes().decode("utf-8") == md


@pytest.mark.rules
def test_ru1_refresh_refuses_to_launder_a_forged_rules_reviewed_through_an_untrusted_header(tmp_path):
    """A summary that opened `<!--` in an old ledger and a description that closes it could forge the line: refused, untouched."""
    i = issue(p("-->"), ul("**Rules reviewed:** BR-EVIL-1-1@deadbeef"))
    p_ = write(tmp_path, i)
    md = p_.read_text(encoding="utf-8").replace("# DoD — SPK-1: T", "# DoD — SPK-1: T <!--", 1)  # an old ledger did not escape it
    assert "\n- **Rules reviewed:** BR-EVIL-1-1@deadbeef\n" in md and ledger_rules_reviewed(md)["valid"] is False
    p_.write_bytes(md.encode("utf-8"))
    with pytest.raises(DodExists, match="Rules reviewed") as e:
        write(tmp_path, i, refresh=True)
    assert p_.read_bytes().decode("utf-8") == md and "deadbeef" not in str(e.value)


@pytest.mark.rules
def test_ru1_refresh_still_repairs_an_untrusted_header_that_names_no_rules_line(tmp_path):
    md = md_of(ul("x")).replace("# DoD — SPK-1: T", "# DoD — SPK-1: T <!-- legacy", 1)
    out = refresh_raw(tmp_path, md)  # no `Rules reviewed:` anywhere: rebuilt as before, not refused
    assert not unclosed(out) and "legacy" not in out and "Rules reviewed" not in out


@pytest.mark.rules
def test_ru1_cli_refresh_of_a_malformed_reviewed_line_exits_2_untouched(repo, capsys):
    f = fake_with(story_doc(ul("x")))
    assert cli(f, "seed", "SPK-1") == 0
    p_ = repo / "dod" / "DoD_SPK-1.md"
    md = _with_header(p_.read_text(encoding="utf-8"), "- **Rules reviewed:** BR-EVIL-1-1 deadbeef")
    p_.write_bytes(md.encode("utf-8"))
    capsys.readouterr()
    assert cli(f, "refresh", "SPK-1") == 2
    err = capsys.readouterr().err
    assert "Rules reviewed" in err and "deadbeef" not in err and p_.read_bytes().decode("utf-8") == md


@pytest.mark.rules
def test_ru1_header_readers_are_linear():
    big = "# DoD — SPK-1: T\n" + ("- **Rules from:** " + "LOY-1, " * 50000 + "\n") + "- " + " " * 400000 + "x\n" + ">" * 300000 + "\n## 1. x\n"
    tm = time.perf_counter()
    ledger_rules_from(big), ledger_rules_reviewed(big), header_block(big)
    assert time.perf_counter() - tm < 1.0


# ---- DF1: live runs A4-14, A4-16, A4-21, A4-22, A4-26b (D-009, D-010, D-011, D-012) ----
# Hand-written fixtures of the AC text the live site stored and returned (evidence A4/A4-21.F8.sent.json, A4-22, A4-21-nul).
LIVE_HOSTILE = ["Bell \u0007 here", "ANSI \u001b[31mred\u001b[0m text", "DEL \u007f and C1 \u009b31m", "NUL \u0000 byte",
           "RLO \u202eevil\u202c end", "LRI \u2066iso\u2069 end", "ZWSP a\u200bb end", "BOM \ufeff and ZWJ \u200d end"]
UNSAFE = set("\u0007\u001b\u007f\u009b\u0000\u202e\u202c\u2066\u2069\u200b\ufeff\u200d")


def _unsafe_in(text):
    return sorted("U+%04X" % ord(c) for c in set(text) & UNSAFE)


def test_seed_and_refresh_write_no_control_bidi_or_zero_width_character(repo, capsys):
    assert cli(fake_with(story_doc(ul(*LIVE_HOSTILE))), "seed", "1") == 0
    led = repo / "dod" / "DoD_SPK-1.md"
    text = led.read_text(encoding="utf-8")
    assert _unsafe_in(text) == [] and "Bell  here" in text and "a" + "b end" in text and "\t" not in text
    assert cli(fake_with(story_doc(ul(*LIVE_HOSTILE))), "refresh", "1") == 0
    assert _unsafe_in(led.read_text(encoding="utf-8")) == []


def test_the_ac_hash_is_over_the_cleaned_text_so_stripped_characters_never_drift():
    """The hash is sha256 of section 1's canonical Markdown after the unsafe characters are removed: a story that differs only by them
    hashes the same, so the gate's drift check (which recomputes it the same way) cannot report drift for ever."""
    dirty = ac_section(story_doc(ul(*LIVE_HOSTILE)), HEADING)
    clean = ac_section(story_doc(ul(*["".join(c for c in x if c not in UNSAFE) for x in LIVE_HOSTILE])), HEADING)
    assert dirty == clean and ac_hash(dirty) == ac_hash(clean) and _unsafe_in(dirty) == []
    assert ac_section(story_doc(ul("tab\there")), HEADING) == "- tab\there"  # tab kept
    assert ledger_ac_hash(md_of(ul(*LIVE_HOSTILE))) == ac_hash(clean)


def test_safe_text_is_the_one_cleaner():
    assert adf.safe_text("a\x1b[31mb\x07\x00\x7f\x9b\u202e\u2066\u200b\ufeffc\n\td") == "a[31mbc\n\td"
    assert adf.safe_text("caf\u00e9 \u0645\u0631\u062d\u0628\u0627") == "caf\u00e9 \u0645\u0631\u062d\u0628\u0627"


def test_refresh_refuses_a_ledger_over_1_mib(repo, capsys):
    """A4-16: done and gate refused a 1,180,175-byte ledger, `dod refresh` read and rewrote it."""
    assert cli(fake_with(story_doc(ul(FULL_AC))), "seed", "1") == 0
    led = repo / "dod" / "DoD_SPK-1.md"
    text = led.read_text(encoding="utf-8")
    big = text.replace("- **Approach / chosen option + why:** _(fill in)_", "- **Approach:** " + "x" * (1024 * 1024 + 10), 1)
    led.write_text(big, encoding="utf-8")
    capsys.readouterr()
    assert cli(fake_with(story_doc(ul(FULL_AC))), "refresh", "1") == 2
    assert "over 1 MiB" in capsys.readouterr().err and led.read_text(encoding="utf-8") == big


def test_cli_refresh_without_a_ledger_is_refused(repo, capsys):
    assert cli(fake_with(story_doc(ul(FULL_AC))), "refresh", "1") == 2
    assert "no ledger to refresh" in capsys.readouterr().err and not (repo / "dod" / "DoD_SPK-1.md").exists()


def test_a_code_block_belongs_to_the_criterion_before_it():
    """A4-26b: one bullet plus a two-line python code block gave 5 AC (the fences and each code line)."""
    md = md_of(ul("The export looks like this:"), code("print('draft')\nx = 1", "python"))
    assert "\\```python" in md  # the escaped fence still keeps the ledger structure
    assert ac_lines(md) == ["The export looks like this:"]
    assert ac_lines(md_of(ul("first"), code("a\nb"), ul("second"))) == ["first", "second"]
    only = ac_lines(md_of(code("only code\nline 2")))
    assert len(only) == 1 and only[0].startswith("```")  # no criterion before it: the block is one AC, never none
    assert dod.ledger_ac_stats(md) == ac_stats(ac_section(story_doc(ul("The export looks like this:"), code("print('draft')\nx = 1", "python")), HEADING))


def test_an_unclosed_fence_like_line_does_not_hide_the_criteria_after_it():
    assert ac_lines(md_of(p("```not a block"), ul("after"))) == ["```not a block", "after"]


# ---- DF2 (live run A12-X3, D-022): --spec-dir is checked where it enters, before any request ----
@pytest.mark.parametrize("bad", ["../../etc", "/etc", "$(id)", "specs/../../x", "specs/a b", "specs/a;b", "`id`", "dod", "dod/x", ".",
                                 "specs/a'b", "C:\\x", "-x", ""])
@pytest.mark.parametrize("action", ["seed", "refresh"])
def test_a_bad_spec_dir_is_a_usage_error_before_any_request(repo, capsys, bad, action):
    f = fake_with(story_doc(ul("x")))
    if action == "refresh":
        assert cli(f, "seed", "SPK-1") == 0
    before = os.path.lexists(ledger_path(repo, "SPK-1")) and ledger_path(repo, "SPK-1").read_bytes()
    f.log.clear()
    assert cli(f, action, "SPK-1", "--spec-dir=" + bad) == 2
    assert "--spec-dir" in capsys.readouterr().err and f.log == []
    assert (os.path.lexists(ledger_path(repo, "SPK-1")) and ledger_path(repo, "SPK-1").read_bytes()) == before


def test_a_good_spec_dir_is_recorded(repo):
    f = fake_with(story_doc(ul("x")))
    assert cli(f, "seed", "SPK-1", "--spec-dir", "specs/001-x_y.z") == 0
    assert ledger_spec_dir(ledger_path(repo, "SPK-1").read_text(encoding="utf-8")) == "specs/001-x_y.z"
