"""L1: the decisions file `dod/Decisions_<KEY>.md` and the local verbs of `skj decisions` (list, --add, --supersedes, --resolve).

The `D<n>` grammar, the file format and the refusals are the fork source's, run on the same fixture lines. Differences by design: the key
names the file (`Decisions_SPK-1.md`), marker ids are comment id STRINGS, and `--resolve` reads `approvers` from the TARGET branch.
`--post` is W5 and `--sync` is A2 (below, against the fake Jira only). All data is fictional; nothing here touches a network.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from skj import decisions
from skj.__main__ import main

SITE = "example-site.atlassian.net"
KEY = "SPK-1"
APPROVER = "5b10ac8d82e05b22cc7d4ef5"  # invented accountId
OPEN = "- [ ] D1: Use cursor paging → _(open)_ · kind: decision · why: the story names no page size"
VAL = "- [x] D2: Cap at 50 rows → validated · by: PO · note: agreed in the thread"
REJ = "- [x] D3: Skip audit log → rejected · by: PO · note: not acceptable"
OPEN2 = "- [ ] D4: Retry twice → _(open)_ · kind: assumption · why: the story is silent"
KEPT = "- [ ] D4: Wrap it → _(open)_ · kind: override · why: skip the rule · posted: 5:abcdef12 · used: 2026-01-01"


def _file(body):
    """A decisions file: the template (title and format note) followed by the entries."""
    return decisions.render_file(KEY) + body + "\n"


def _all(parsed):
    return [d for k in ("open", "validated", "rejected") for d in parsed[k]]


# ---- L1-AC1: grammar and file format -------------------------------------------------------------------------------------

def test_l1_ac1_parse_open_validated_rejected():
    r = decisions.parse_decisions(_file("\n".join([OPEN, VAL, REJ])))
    assert r["unparsed"] is False
    assert r["open"] == [{"id": "D1", "kind": "decision", "text": "Use cursor paging",
                          "why": "the story names no page size", "status": "open", "by": "", "note": "",
                          "posted": None, "used": ""}]
    assert [d["id"] for d in r["validated"]] == ["D2"] and r["validated"][0]["by"] == "PO"
    assert r["validated"][0]["note"] == "agreed in the thread" and r["validated"][0]["status"] == "validated"
    assert [d["id"] for d in r["rejected"]] == ["D3"] and r["rejected"][0]["status"] == "rejected"


def test_l1_ac1_round_trip_render_line():
    lines = [OPEN, VAL, REJ,
             OPEN + " · posted: 42:" + decisions.line_hash("Use cursor paging"),
             "- [ ] D4: Skip the freeze → _(open)_ · kind: override · why: outage · used: 2026-11-02",
             "- [x] D5: a → b → c → validated · by: PO · note: ok · posted: 7:deadbeef",
             "- [x] D6: Cap → validated · by: approver · note: fine · via: 123 · posted: 7:deadbeef"]
    for line in lines:
        d = _all(decisions.parse_decisions(_file(line)))[0]
        assert decisions.render_line(d) == line, line


def test_l1_ac1_parse_reads_whole_file_ignoring_fences():
    body = f"```\n{OPEN}\n```\n<!-- {VAL} -->\n<!--\n{REJ}\n-->\n- [ ] D9: real → _(open)_ · kind: assumption · why: w"
    r = decisions.parse_decisions(_file(body))
    assert [d["id"] for d in r["open"]] == ["D9"] and not r["validated"] and not r["rejected"]
    assert r["unparsed"] is False
    assert decisions.parse_decisions(_file("<!-- never closed\n" + OPEN))["unparsed"] is True
    # no `## ` heading is needed or looked for: lines after any heading are read too
    r = decisions.parse_decisions("# Decisions\n## Later\n" + OPEN + "\n## 3. x\n" + VAL + "\n")
    assert [d["id"] for d in r["open"]] == ["D1"] and [d["id"] for d in r["validated"]] == ["D2"]


def test_l1_ac1_duplicate_id_first_wins():
    r = decisions.parse_decisions(_file(OPEN + "\n" + OPEN.replace("cursor paging", "offset paging") + "\n" + VAL.replace("D2", "D1")))
    assert [d["text"] for d in r["open"]] == ["Use cursor paging"] and r["validated"] == []


def test_l1_ac1_malformed_line_sets_unparsed():
    for bad in ["- [ ] D1: no arrow", "- [ ] D1: t → _(open)_ · why: no kind",
                "- [ ] D1: t → _(open)_ · kind: guess · why: w", "- [ ] D1: t → validated · by: PO · note: n",
                "- [x] D1: t → _(open)_ · kind: decision · why: w", "- [x] D1: t → validated · note: no role",
                "- [ ] D1: t → _(open)_ · kind: decision · why: w · posted: nope"]:
        r = decisions.parse_decisions(_file(bad + "\n" + VAL))
        assert r["unparsed"] is True and r["open"] == [] and [d["id"] for d in r["validated"]] == ["D2"], bad
    assert decisions.parse_decisions("nothing") == {"open": [], "validated": [], "rejected": [], "unparsed": False}


def test_l1_ac1_posted_marker_id_is_a_string_and_the_hash_changes_with_the_text():
    h = decisions.line_hash("Use cursor paging")
    assert len(h) == 8 and h == decisions.line_hash("Use  cursor\tpaging ") and h != decisions.line_hash("Use offset")
    r = decisions.parse_decisions(_file(f"{OPEN} · posted: 42:{h}"))
    assert r["open"][0]["posted"] == {"comment_id": "42", "hash": h}  # a string: a Jira comment id is never an int
    # a comment id is ASCII digits without a leading zero, at most 12 of them; anything else breaks the grammar
    for bad in ("0042", "x42", "٤٢", "1234567890123", "", "-1", "4 2"):
        r = decisions.parse_decisions(_file(f"{OPEN} · posted: {bad}:{h}"))
        assert r["unparsed"] is True and r["open"] == [], bad


def test_l1_ac1_file_template_has_title_and_format_note_that_does_not_parse():
    md = decisions.render_file(KEY)
    assert md.startswith("# Decisions — SPK-1\n") and "<!--" not in md and "-->" not in md
    assert "`- [ ] D1: <text> → _(open)_ · kind: decision · why: <reason>`" in md
    assert decisions.parse_decisions(md) == {"open": [], "validated": [], "rejected": [], "unparsed": False}
    assert not decisions.has_decision_lines(md)


def test_l1_ac1_has_decision_lines():
    assert decisions.has_decision_lines(_file(OPEN)) and decisions.has_decision_lines(_file(VAL))
    assert decisions.has_decision_lines(_file("- [ ] D4 not in the grammar"))  # malformed still counts
    assert not decisions.has_decision_lines(_file("- [ ] Q1: which? → _(open)_ · owner: lead"))
    assert not decisions.has_decision_lines(_file("```\n" + OPEN + "\n```"))  # a fenced example is not a line
    assert not decisions.has_decision_lines(_file("<!-- " + OPEN + " -->"))
    assert not decisions.has_decision_lines("# DoD\nno section 2\n")


def test_l1_ac1_pathological_whitespace_line_parses_fast():
    status = "- [ ] D1: t → _(open)_"
    for bad in (status + " " * 200_000 + "· kind: decision · why: w", "- [ ] D1: " + "x" * 200_000 + " → _(open)_ · kind: decision · why: w"):
        t0 = time.process_time()
        r = decisions.parse_decisions(_file(bad + "\n" + VAL))
        assert time.process_time() - t0 < 1
        assert r["unparsed"] is True and r["open"] == [] and [d["id"] for d in r["validated"]] == ["D2"]


def test_l1_ac1_via_key_is_validated():
    ok = "- [x] D2: Cap → validated · by: approver · note: fine · via: 123"
    d = decisions.parse_decisions(_file(ok))["validated"][0]
    assert d["via"] == "123" and decisions.render_line(d) == ok
    for bad in ("- [x] D2: Cap → validated · by: approver · note: fine · via: 0123", "- [x] D2: Cap → validated · by: PO · via: abc",
                "- [x] D2: Cap → validated · by: PO · via: ", "- [x] D2: Cap → validated · by: PO · via: 12 3",
                "- [x] D2: Cap → validated · by: PO · via: 1234567890123",
                "- [x] D2: Cap → validated · by: PO · via: 1 · via: 2", "- [x] D2: Cap → validated · by: PO · via: ١٢٣",
                "- [ ] D2: Cap → _(open)_ · kind: decision · why: w · via: 12"):
        r = decisions.parse_decisions(_file(bad))
        assert r["unparsed"] is True and not _all(r), bad
    # a `via` word inside a note is not a key
    assert decisions.parse_decisions(_file("- [x] D2: Cap → validated · by: PO · note: a via: b"))["unparsed"] is False
    with pytest.raises(ValueError):
        decisions.resolve(_file(OPEN), "D1", "validated", "approver", "", False, "12; rm")
    with pytest.raises(ValueError):
        decisions.resolve(_file(OPEN), "D1", "validated", "approver", "", False, 12)  # an int is not a comment id
    # a terminal verdict over a synced one drops the via key: it no longer claims a comment
    synced = decisions.resolve(_file(OPEN), "D1", "validated", "approver", "", False, "12")
    assert " · via: 12" in synced and " · via:" not in decisions.resolve(synced, "D1", "rejected", "PO", "no", True)


def test_l1_ac1_used_marker_is_stamped_once_and_keeps_every_other_byte():
    md = "# Decisions\n\n" + KEPT.replace(" · used: 2026-01-01", "") + "\n" + OPEN + "\n" + VAL + "\n"
    new = decisions.stamp_used(md, ["D4", "D2"], "2026-10-30")
    lines = new.splitlines()
    assert lines[2].endswith(" · used: 2026-10-30") and lines[3] == OPEN and lines[4].endswith(" · used: 2026-10-30")
    parsed = decisions.parse_decisions(new)
    assert parsed["unparsed"] is False and sorted(d["used"] for d in _all(parsed) if d["used"]) == ["2026-10-30", "2026-10-30"]
    assert decisions.stamp_used(new, ["D4", "D2"], "2027-01-01") == new  # once: a stamped entry keeps its first date
    assert decisions.stamp_used(md, [], "2026-10-30") == md
    crlf = md.replace("\n", "\r\n")
    assert decisions.stamp_used(crlf, ["D4"], "2026-10-30").count("\r\n") == crlf.count("\r\n")
    for ids, date in ((["D9"], "2026-10-30"), (["D4"], "yesterday"), (["D4"], "2026-10-30 · by: PO")):
        with pytest.raises(ValueError):
            decisions.stamp_used(md, ids, date)  # an unknown id or a bad date raises and returns nothing
    with pytest.raises(ValueError):
        decisions.stamp_used("<!-- note --> " + OPEN, ["D1"], "2026-10-30")


def test_decisions_module_compiles_without_a_warning():
    import warnings
    src = Path(decisions.__file__)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        compile(src.read_text(encoding="utf-8"), str(src), "exec")


# ---- supersedes ------------------------------------------------------------------------------------------------------------

def _dl(n, text, status="open", kind="decision", posted=False):
    """One decisions-file line (exact grammar) for `D<n>`; `posted`: with the marker the PO's thread would give it."""
    d = {"id": f"D{n}", "kind": kind, "text": text, "why": "w", "status": status, "by": "" if status == "open" else "PO",
         "note": "", "posted": None, "used": ""}
    if posted:
        d["posted"] = {"comment_id": "7", "hash": decisions.marker_hash(_first(d))}
    return decisions.render_line(d) + "\n"


def _first(d):
    """The entry `parse_decisions` reads back from `d` (so a supersedes prefix is seen)."""
    return _all(decisions.parse_decisions(decisions.render_line(d) + "\n"))[0]


def _parse(*lines):
    return decisions.parse_decisions(decisions.render_file(KEY) + "".join(lines))


def _ids(parsed):
    return [d["id"] for d in _all(parsed)]


def test_parse_extracts_supersedes_only_from_the_exact_prefix():
    r = _parse(_dl(3, "x", "rejected"), _dl(5, "supersedes D3: use v2"), _dl(6, "supersedes D999999: a"))
    [d5, d6] = r["open"]
    assert d5["supersedes"] == "D3" and d5["text"] == "supersedes D3: use v2" and d6["supersedes"] == "D999999"
    assert "supersedes" not in r["rejected"][0]  # no key at all on an ordinary line
    for text in ("Supersedes D3: a", "supersedes d3: a", "supersedes  D3: a", "supersedes D3 : a", "supersedes D3:a", "supersedes D3 a",
                 "please supersedes D3: a", "supersedes D1234567: a", "supersedes D: a", "supersedes D３: a", "supersedes D٣: a",
                 "supersedes D3:"):
        assert _parse(_dl(5, text))["open"][0].get("supersedes") is None, text
    line = _dl(5, "supersedes D3: use v2").rstrip()
    assert decisions.render_line(_parse(line)["open"][0]) == line  # the grammar is unchanged: it round-trips


def test_effective_drops_the_superseded_entry_unless_the_superseder_is_rejected():
    base = (_dl(3, "x", "rejected"),)
    assert _ids(decisions.effective(_parse(*base, _dl(5, "supersedes D3: y")))) == ["D5"]  # open is live
    assert _ids(decisions.effective(_parse(*base, _dl(5, "supersedes D3: y", "validated")))) == ["D5"]
    assert _ids(decisions.effective(_parse(*base, _dl(5, "supersedes D3: y", "rejected")))) == ["D3", "D5"]  # re-activated
    plain = _parse(_dl(1, "a"), _dl(2, "b", "validated"), _dl(3, "c", "rejected"))
    assert decisions.effective(plain) == plain  # nothing superseded: the same entries
    assert decisions.effective(_parse(*base))["unparsed"] is False
    assert decisions.effective({**_parse(*base), "unparsed": True})["unparsed"] is True


def test_chain_and_cycle_and_self_reference_and_unknown_target():
    d3 = _dl(3, "x", "rejected")

    def chain(s5, s7):
        return _parse(d3, _dl(5, "supersedes D3: a", s5), _dl(7, "supersedes D5: b", s7))
    assert _ids(decisions.effective(chain("open", "open"))) == ["D7"]
    assert _ids(decisions.effective(chain("rejected", "open"))) == ["D7"]  # D7 stands in for the rejected D5, which stood in for D3
    assert _ids(decisions.effective(chain("rejected", "rejected"))) == ["D3", "D5", "D7"]
    assert _ids(decisions.effective(chain("open", "rejected"))) == ["D5", "D7"]  # D7 rejected: D5 is back, and D5 still answers D3
    assert decisions.supersede_problem(_parse(_dl(1, "supersedes D2: a"), _dl(2, "supersedes D1: b"))) is True  # cycle
    for lines in ((_dl(2, "supersedes D2: a"),),  # self-reference
                  (_dl(1, "supersedes D2: a"), _dl(2, "b", "rejected")),  # a lower id supersedes a higher one
                  (_dl(2, "supersedes D9: a"),),  # unknown target
                  (_dl(2, "supersedes D0: a"),)):
        p = _parse(*lines)
        assert decisions.supersede_problem(p) is True and decisions.effective(p) == p, lines  # invalid edges drop nothing
    assert decisions.supersede_problem(_parse(d3, _dl(5, "supersedes D3: a"))) is False
    assert decisions.supersede_problem(_parse(_dl(1, "a"))) is False


def test_override_cannot_be_superseded():
    p = _parse(_dl(1, "skip l2", kind="override"), _dl(2, "supersedes D1: a"))
    assert decisions.supersede_problem(p) is True
    assert _ids(decisions.effective(p)) == ["D1", "D2"]  # the override stays in the set


def test_a_superseder_marker_covers_its_id_so_a_copy_on_another_id_does_not_match():
    d9 = _first({"id": "D9", "kind": "decision", "text": "supersedes D3: x", "why": "w", "status": "open", "by": "", "note": "",
                 "posted": None, "used": ""})
    d10 = {**d9, "id": "D10"}
    assert decisions.marker_hash(d9) != decisions.marker_hash(d10)
    plain = {k: v for k, v in {**d9, "text": "x"}.items() if k != "supersedes"}
    assert decisions.marker_hash(plain) == decisions.line_hash("x")  # an ordinary line: text only


def test_masked_by_names_the_standing_superseder():
    p = _parse(_dl(3, "x", "rejected"), _dl(5, "supersedes D3: a"), _dl(6, "supersedes D3: b", "rejected"))
    assert decisions.masked_by(p, "D3") == "D5" and decisions.masked_by(p, "D5") is None and decisions.masked_by(p, "D6") is None
    assert decisions.masked_by(_parse(_dl(3, "x", "rejected"), _dl(5, "supersedes D3: a", "rejected")), "D3") is None


def _addmd(md, text="my reply", supersedes=None, kind="decision"):
    return decisions.add(md, kind, text, "w", supersedes=supersedes)


def test_add_supersedes_validates_target_exists_and_is_rejected_or_open():
    md = (decisions.render_file(KEY) + _dl(1, "a", "rejected") + _dl(2, "b", posted=True) + _dl(3, "c", "validated")
          + _dl(4, "skip l2", kind="override"))
    new, d_id = _addmd(md, supersedes="D1")
    assert d_id == "D5" and new.endswith("- [ ] D5: supersedes D1: my reply → _(open)_ · kind: decision · why: w\n")
    assert decisions.parse_decisions(new)["open"][-1]["supersedes"] == "D1" and not decisions.supersede_problem(decisions.parse_decisions(new))
    assert _addmd(md, supersedes="D2")[1] == "D5"  # an open target too, once the PO has seen it (a matching posted marker)
    for bad, msg in (("D9", "not in the decisions file"), ("D3", "rejected or open"), ("D4", "override"), ("3", "D<n>"), ("D", "D<n>"),
                     ("d1", "D<n>"), ("D1234567", "D<n>"), ("D１", "D<n>"), ("D1 ", "D<n>"), ("", "D<n>")):
        with pytest.raises(ValueError) as e:
            _addmd(md, supersedes=bad)
        assert msg in str(e.value) and "my reply" not in str(e.value), bad
    # a second LIVE superseder of the same target is refused, naming the existing one
    md2, _ = _addmd(md, supersedes="D1")
    with pytest.raises(ValueError, match="D5"):
        _addmd(md2, supersedes="D1")
    with pytest.raises(ValueError, match="post D5 first"):  # D5 is open and the PO has not seen it (no posted marker)
        _addmd(md2, supersedes="D5")
    d5 = decisions.parse_decisions(md2)["open"][-1]
    md2p = md2.replace(decisions.render_line(d5), decisions.render_line({**d5, "posted": {"comment_id": "7", "hash": decisions.marker_hash(d5)}}))
    assert _addmd(md2p, supersedes="D5")[1] == "D6"  # a chain is fine once D5 is posted
    # once that superseder is rejected it is not live: the target may be answered again
    md3 = md2.replace("- [ ] D5: supersedes D1: my reply → _(open)_ · kind: decision · why: w",
                      "- [x] D5: supersedes D1: my reply → rejected · by: PO · note: no")
    assert decisions.parse_decisions(md3)["rejected"][-1]["id"] == "D5"
    assert _addmd(md3, supersedes="D1")[1] == "D6"
    # the prefix cannot be typed by hand, with or without the flag
    for sup in (None, "D1"):
        with pytest.raises(ValueError, match="supersedes D<n>"):
            _addmd(md, text="supersedes D1: sneaky", supersedes=sup)
    assert decisions.add(md, "decision", "mentions supersedes D1: later", "w")[1] == "D5"  # only a leading prefix counts


def test_add_scrubs_wiki_mentions_so_an_account_id_never_enters_the_file():
    new, _ = decisions.add(decisions.render_file(KEY), "decision", f"ask [~accountid:{APPROVER}] and [~dana] now", f"[~accountid:{APPROVER}]")
    assert APPROVER not in new and "dana" not in new
    assert new.splitlines()[-1] == "- [ ] D1: ask @user and @user now → _(open)_ · kind: decision · why: @user"


def test_scrub_emails_is_linear_on_adversarial_input():
    t0 = time.process_time()
    decisions._scrub_emails("a" * 100_000 + "@" + "b." * 50_000)
    decisions._scrub_emails("@" * 100_000)
    assert time.process_time() - t0 < 5
    assert decisions._scrub_emails("mail d@example.com now") == "mail [email] now"


# ---- the CLI -----------------------------------------------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo with a config, cwd inside it; returns its top level."""
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    for k in ("SKJ_EMAIL", "SKJ_TOKEN"):  # a local verb never reads an identity
        monkeypatch.delenv(k, raising=False)
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, stdin=subprocess.DEVNULL)
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": SITE, "project": "SPK"}))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _dec(repo, body=None, key=KEY):
    """The decisions file path; with `body` it is written (after the template) first."""
    f = repo / "dod" / ("Decisions_%s.md" % key)
    if body is not None:
        f.parent.mkdir(exist_ok=True)
        f.write_text(_file(body), encoding="utf-8")
    return f


def run(*args):
    return main(["decisions", *args])


def _add(*extra, text="Use cursor paging", kind="decision", ref=KEY):
    return run(ref, "--add", text, "--kind", kind, *extra)


def _tty(monkeypatch, on=True):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: on)


def _resolve(*extra, ref=KEY):
    return run(ref, "--resolve", *extra)


def test_l1_ac3_file_is_decisions_key_md_in_dod(repo, capsys):
    assert _add("--why", "the story names no page size") == 0
    f = repo / "dod" / "Decisions_SPK-1.md"
    assert f.is_file() and [p.name for p in (repo / "dod").iterdir()] == ["Decisions_SPK-1.md"]
    assert json.loads(capsys.readouterr().out) == {"key": "SPK-1", "id": "D1", "file": "dod/Decisions_SPK-1.md"}
    assert decisions.decisions_file(repo, "SPK-1") == Path(os.path.realpath(str(repo))) / "dod" / "Decisions_SPK-1.md"
    assert run("1") == 0  # a bare number names the same file
    assert [d["id"] for d in json.loads(capsys.readouterr().out)["open"]] == ["D1"]
    assert _add(text="b", ref="2") == 0 and (repo / "dod" / "Decisions_SPK-2.md").is_file()


def test_l1_ac3_the_file_name_never_comes_from_unvalidated_input(repo):
    for bad in ("spk-1", "SPK-0", "SPK-01", "../x", "SPK-1/../x", "SPK-1;touch x", "", "SPK-１"):
        with pytest.raises(ValueError):
            decisions.decisions_file(repo, bad)
    # the CLI refuses a key of another project and a malformed reference before any file is touched
    for ref in ("OTHER-1", "spk-1", "SPK-0", "SPK-1;touch x", "0", "-1", "1.5"):
        assert run(ref) == 2 and _add(ref=ref) == 2
    assert not (repo / "dod").exists()


def test_decisions_file_refuses_a_symlink_and_a_non_file(repo):
    (repo / "dod").mkdir()
    (repo / "elsewhere.md").write_text(_file(OPEN))
    (repo / "dod" / "Decisions_SPK-1.md").symlink_to(repo / "elsewhere.md")
    before = (repo / "elsewhere.md").read_bytes()
    assert run(KEY) == 1 and _add() == 1 and (repo / "elsewhere.md").read_bytes() == before
    (repo / "dod" / "Decisions_SPK-1.md").unlink()
    (repo / "dod" / "Decisions_SPK-1.md").mkdir()
    assert run(KEY) == 1
    (repo / "dod" / "Decisions_SPK-1.md").rmdir()
    (repo / "dod").rmdir()
    (repo / "real").mkdir()
    (repo / "dod").symlink_to(repo / "real")  # a symlinked dod/ is refused too
    assert _add() == 1 and list((repo / "real").iterdir()) == []


def test_l1_ac1_cli_list_is_read_only_and_a_missing_file_is_refused(repo, capsys):
    assert run(KEY) == 1
    assert "`skj decisions SPK-1 --add " in capsys.readouterr().err
    led = _dec(repo, OPEN + "\n" + VAL)
    before = led.read_bytes()
    assert run(KEY) == 0
    out = json.loads(capsys.readouterr().out)
    assert [d["id"] for d in out["open"]] == ["D1"] and [d["id"] for d in out["validated"]] == ["D2"] and out["unparsed"] is False
    assert led.read_bytes() == before
    led.write_bytes(b"\xff\xfe")
    assert run(KEY) == 1 and "cannot read the decisions file" in capsys.readouterr().err


def test_a_decisions_file_over_1_mib_is_refused_untouched(repo, capsys):
    led = _dec(repo, OPEN)
    led.write_text(_file(OPEN) + "x" * (1 << 20))
    before = led.read_bytes()
    assert run(KEY) == 1 and _add() == 1
    assert "1 MiB" in capsys.readouterr().err and led.read_bytes() == before


def test_the_size_cap_counts_bytes_not_characters(repo, capsys):
    led = _dec(repo, OPEN)
    body = _file(OPEN) + "é" * ((1 << 19) - 100)  # about 0.5 M characters, a little over 1 MiB in bytes
    led.write_text(body, encoding="utf-8")
    assert len(body) < (1 << 20) < len(body.encode("utf-8"))
    before = led.read_bytes()
    assert run(KEY) == 1 and _add() == 1
    assert "1 MiB" in capsys.readouterr().err and led.read_bytes() == before


# ---- --add ---------------------------------------------------------------------------------------------------------------------

def test_l1_ac1_add_creates_file_and_appends_next_id(repo, capsys):
    f = _dec(repo)
    assert _add("--why", "the story names no page size") == 0
    assert json.loads(capsys.readouterr().out)["id"] == "D1"
    assert f.read_text(encoding="utf-8") == decisions.render_file(KEY) + OPEN + "\n"
    # next id = max existing + 1, even with gaps, a resolved line and a duplicate id
    f.write_text(decisions.render_file(KEY) + "\n".join([OPEN, VAL.replace("D2", "D7"), OPEN.replace("paging", "x")]) + "\n")
    before = f.read_text()
    assert _add("--why", "w", text="Retry twice", kind="assumption") == 0
    assert json.loads(capsys.readouterr().out)["id"] == "D8"
    after = f.read_text()
    assert after == before + "- [ ] D8: Retry twice → _(open)_ · kind: assumption · why: w\n"
    d = decisions.parse_decisions(after)
    assert d["unparsed"] is False and [x["id"] for x in d["open"]][-1] == "D8"
    assert list(f.parent.glob("*.tmp")) == []


def test_add_defaults_why_adds_a_missing_final_newline_and_keeps_crlf(repo):
    f = _dec(repo)
    assert _add() == 0
    assert "· kind: decision · why: unspecified\n" in f.read_text()
    f.write_bytes((decisions.render_file(KEY) + OPEN).replace("\n", "\r\n").encode("utf-8"))  # no final newline
    assert _add(text="b", kind="assumption") == 0
    raw = f.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n") and b"D2: b" in raw
    assert decisions.parse_decisions(raw.decode("utf-8"))["unparsed"] is False


def test_add_numbers_after_a_malformed_line_too(repo):
    f = _dec(repo, "- [ ] D5 not in the grammar")
    assert _add() == 0
    assert "D6: Use cursor paging" in f.read_text()


def test_add_counts_ids_hidden_in_comments_and_fences(repo):
    f = _dec(repo, f"<!-- {VAL.replace('D2', 'D12')} -->\n```\n{REJ.replace('D3', 'D20')}\n```\n" + OPEN)
    assert _add() == 0
    assert "- [ ] D21: Use cursor paging" in f.read_text(encoding="utf-8")


def test_add_scrubs_mentions_and_emails_and_trims(repo):
    f = _dec(repo)
    assert _add("--why", "ask @dana", text="  mail d@example.com <b>x</b> ") == 0
    assert f.read_text().splitlines()[-1] == "- [ ] D1: mail [email] <b>x</b> → _(open)_ · kind: decision · why: ask @user"


def test_add_refuses_control_chars_comment_openers_and_long_text(repo, capsys):
    f = _dec(repo)
    secret = "TOPSECRETTEXT"
    bad_text = ["a\nb", "a\rb", "a\tb", "a\x1bb", "a\x00b", "a\x7fb", "a\x85b", "a b", "a b",
                "x <!-- y", "x --> y", "a ``` b", "~~~", "x ````", "", "   ", "x" * 1501]
    for t in bad_text:
        assert (_add(text=secret + t) if t.strip() else _add(text=t)) == 2, repr(t)  # DF1 D-001: a bad value is a usage error
        err = capsys.readouterr().err
        assert err.startswith("skj:") and secret not in err and not f.exists(), repr(t)
    for why in ("a\nb", "a\x1bb", "w <!-- x", "w ```", "w" * 301, "w · kind: assumption"):
        # a why that only fails the read-back of the whole line (a `·` field) is refused by the file grammar: exit 1
        assert _add("--why", why, text=secret) == (1 if "·" in why else 2), repr(why)
        err = capsys.readouterr().err
        assert err.startswith("skj:") and secret not in err and "wwww" not in err and not f.exists(), repr(why)
    for kind in ("override", "Decision", "", "x"):
        assert _add(text=secret, kind=kind) == 2  # A5-05: was exit 1
        err = capsys.readouterr().err
        assert "decision or assumption" in err and secret not in err and not f.exists()
    assert _add(text="x" * 1500, kind="decision") == 0  # the limits themselves are fine
    assert _add("--why", "w" * 300) == 0


def test_add_refuses_when_the_new_line_would_be_hidden_and_writes_nothing(repo, capsys):
    f = _dec(repo, "```\n" + OPEN)  # unclosed fence
    before = f.read_bytes()
    assert _add() == 1
    assert "decisions file" in capsys.readouterr().err and f.read_bytes() == before and list(f.parent.glob("*.tmp")) == []
    f.write_bytes(b"\xff\xfe")
    assert _add() == 1 and f.read_bytes() == b"\xff\xfe"


def test_add_write_is_atomic_and_keeps_the_mode(repo, monkeypatch, capsys):
    f = _dec(repo)
    assert _add() == 0
    before = f.read_bytes()
    f.chmod(0o640)
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    assert _add(text="more") == 1
    assert "disk full" in capsys.readouterr().err and f.read_bytes() == before and list(f.parent.glob("*.tmp")) == []
    monkeypatch.setattr(os, "replace", real)
    assert _add(text="more") == 0 and (f.stat().st_mode & 0o777) == 0o640


def test_add_supersedes_cli(repo, capsys):
    f = _dec(repo, _dl(1, "use v1", "rejected").rstrip())
    assert _add("--supersedes", "D1", text="use v2 as the PO asked") == 0
    assert json.loads(capsys.readouterr().out)["id"] == "D2"
    assert f.read_text().endswith("- [ ] D2: supersedes D1: use v2 as the PO asked → _(open)_ · kind: decision · why: unspecified\n")
    before = f.read_bytes()
    assert _add("--supersedes", "D1") == 1 and _add("--supersedes", "D7") == 1 and _add("--supersedes", "x") == 2
    assert f.read_bytes() == before
    err = capsys.readouterr().err
    assert "D2" in err and "not in the decisions file" in err
    assert run(KEY, "--supersedes", "D1") == 2 and f.read_bytes() == before  # needs --add


def test_add_flag_combinations_exit_2(repo, capsys):
    f = _dec(repo)
    for argv in (["--add", "t"], ["--add", "t", "--kind", "decision", "--resolve", "D1", "--validated"],
                 ["--add", "t", "--kind", "decision", "--validated"], ["--add", "t", "--kind", "decision", "--by", "PO"],
                 ["--add", "t", "--kind", "decision", "--note", "n"], ["--add", "t", "--kind", "decision", "--force"],
                 ["--kind", "decision"], ["--why", "w"]):
        assert run(KEY, *argv) == 2, argv
        assert capsys.readouterr().err.startswith("skj:")
    assert not f.exists()


# ---- --resolve ---------------------------------------------------------------------------------------------------------------

def test_resolve_validates_only_that_line(repo, monkeypatch, capsys):
    led = _dec(repo, "\n".join([OPEN, VAL, REJ]))
    _tty(monkeypatch)
    before = led.read_text(encoding="utf-8").splitlines()
    assert _resolve("D1", "--validated") == 0
    assert json.loads(capsys.readouterr().out)["resolved"] == "D1"
    after = led.read_text(encoding="utf-8").splitlines()
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(before) == len(after) and len(changed) == 1 and after[changed[0]].startswith("- [x] D1:")
    d = decisions.parse_decisions(led.read_text(encoding="utf-8"))
    assert d["unparsed"] is False and [x["id"] for x in d["validated"]] == ["D1", "D2"] and d["open"] == []
    assert d["validated"][0]["by"] == "PO" and d["validated"][0]["note"] == ""
    assert list(led.parent.glob("*.tmp")) == []


def test_resolve_rejected_then_validated_allowed_with_force(repo, monkeypatch):
    led = _dec(repo, "\n".join([OPEN, REJ]))
    _tty(monkeypatch)
    assert _resolve("D1", "--rejected") == 0
    assert _resolve("D3", "--validated", "--note", "changed my mind") == 1  # already resolved: needs --force
    assert _resolve("D3", "--validated", "--note", "changed my mind", "--force") == 0
    d = decisions.parse_decisions(led.read_text(encoding="utf-8"))
    assert [x["id"] for x in d["rejected"]] == ["D1"] and [x["id"] for x in d["validated"]] == ["D3"]
    assert d["validated"][0]["note"] == "changed my mind"


def test_re_resolving_needs_force(repo, monkeypatch, capsys):
    led = _dec(repo, VAL)
    _tty(monkeypatch)
    before = led.read_bytes()
    assert _resolve("D2", "--rejected") == 1
    assert "--force" in capsys.readouterr().err and led.read_bytes() == before
    assert _resolve("D2", "--rejected", "--force") == 0
    assert decisions.parse_decisions(led.read_text(encoding="utf-8"))["rejected"][0]["id"] == "D2"


def test_resolve_refuses_without_a_terminal_before_looking_for_the_file(repo, monkeypatch, capsys):
    led = _dec(repo, OPEN)
    _tty(monkeypatch, False)
    before = led.read_bytes()
    assert _resolve("D1", "--validated") == 1
    assert "own terminal" in capsys.readouterr().err and led.read_bytes() == before
    led.unlink()  # checked before the file is even looked for
    assert _resolve("D1", "--validated") == 1
    err = capsys.readouterr().err
    assert "own terminal" in err and "no decisions file" not in err


def test_resolve_a_missing_file_names_the_add_command(repo, monkeypatch, capsys):
    _tty(monkeypatch)
    assert _resolve("D1", "--validated") == 1
    assert "`skj decisions SPK-1 --add " in capsys.readouterr().err


def test_resolve_unknown_id_is_refused(repo, monkeypatch, capsys):
    led = _dec(repo, OPEN)
    _tty(monkeypatch)
    before = led.read_bytes()
    for bad in ("D9", "1", "d1", "D1x"):
        assert _resolve(bad, "--validated") == 1
        assert "no decision" in capsys.readouterr().err and led.read_bytes() == before


def test_resolve_by_and_note_are_validated(repo, monkeypatch, capsys):
    led = _dec(repo, OPEN)
    _tty(monkeypatch)
    before = led.read_bytes()
    bad = [("--by", "Dana; PO"), ("--by", "x" * 41), ("--by", ""), ("--by", "P\nO"), ("--by", "P·O"), ("--by", "PO\x1b"),
           ("--note", "n" * 301), ("--note", "a\nb"), ("--note", "a\rb"), ("--note", "a\x1b[8mb"), ("--note", "a\x00b"),
           ("--note", "a\x85b"), ("--note", "a\x7fb"), ("--note", "x · kind: assumption"), ("--note", "x → rejected"),
           ("--note", "x <!-- y"), ("--note", "x --> y"), ("--note", "<!--"), ("--note", "a ``` b"), ("--note", "~~~")]
    for flag, val in bad:
        # DF1 D-001: a bad value is a usage error; a note that only fails the read-back of the line (a `·` or `→` field) is exit 1
        assert _resolve("D1", "--validated", flag, val) == (1 if flag == "--note" and ("·" in val or "→" in val) else 2), (flag, val)
        assert capsys.readouterr().err.startswith("skj:") and led.read_bytes() == before, (flag, val)
    assert _resolve("D1", "--validated", "--by", "Product Owner 2", "--note", "n" * 300) == 0
    d = decisions.parse_decisions(led.read_text(encoding="utf-8"))["validated"][0]
    assert d["by"] == "Product Owner 2" and d["note"] == "n" * 300


def test_resolve_keeps_posted_marker_and_other_markers(repo, monkeypatch):
    led = _dec(repo, KEPT + "\n" + VAL)
    _tty(monkeypatch)
    old = decisions.parse_decisions(led.read_text(encoding="utf-8"))["open"][0]
    assert _resolve("D4", "--rejected", "--note", "no") == 0
    md = led.read_text(encoding="utf-8")
    line = next(x for x in md.splitlines() if "D4:" in x)
    assert line == "- [x] D4: Wrap it → rejected · by: PO · note: no · kind: override · why: skip the rule · posted: 5:abcdef12 · used: 2026-01-01"
    new = decisions.parse_decisions(md)["rejected"][0]
    assert decisions.render_line(new) == line
    assert {k: new[k] for k in ("id", "kind", "text", "why", "posted", "used")} == {k: old[k] for k in ("id", "kind", "text", "why", "posted", "used")}


def test_resolve_keeps_line_terminators(repo, monkeypatch):
    led = _dec(repo, OPEN)
    led.write_text(_file(OPEN + " " + VAL), encoding="utf-8")  # a U+2028 is a line break to str.splitlines, not to the file's reader
    _tty(monkeypatch)
    assert _resolve("D1", "--validated") == 0
    md = led.read_text(encoding="utf-8")
    assert " - [x] D2:" in md and decisions.parse_decisions(md)["unparsed"] is False
    led.write_bytes(_file(OPEN + "\n" + VAL).replace("\n", "\r\n").encode("utf-8"))
    before = led.read_bytes()
    assert _resolve("D2", "--rejected", "--by", "PO", "--force") == 0
    after = led.read_bytes()
    assert after.count(b"\r\n") == before.count(b"\r\n") == after.count(b"\n")
    assert [x["id"] for x in decisions.parse_decisions(after.decode("utf-8"))["rejected"]] == ["D2"]


def test_resolved_line_over_max_line_is_refused(repo, monkeypatch, capsys):
    led = _dec(repo, f"- [ ] D1: {'t' * 1800} → _(open)_ · kind: decision · why: w")
    _tty(monkeypatch)
    before = led.read_bytes()
    assert _resolve("D1", "--validated", "--note", "n" * 250) == 1
    assert str(decisions.MAX_LINE) in capsys.readouterr().err and led.read_bytes() == before


def test_resolve_flag_combinations_exit_2(repo, monkeypatch, capsys):
    led = _dec(repo, OPEN)
    _tty(monkeypatch)
    before = led.read_bytes()
    for argv in (["--resolve", "D1"], ["--validated"], ["--by", "PO"], ["--note", "x"], ["--force"]):
        assert run(KEY, *argv) == 2, argv
        assert capsys.readouterr().err.startswith("skj:")
    with pytest.raises(SystemExit) as e:  # --validated and --rejected exclude each other (argparse)
        run(KEY, "--resolve", "D1", "--validated", "--rejected")
    assert e.value.code == 2 and led.read_bytes() == before


def test_resolve_write_is_atomic_and_refuses_a_file_changed_meanwhile(repo, monkeypatch, capsys):
    led = _dec(repo, OPEN)
    _tty(monkeypatch)
    before = led.read_bytes()
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    assert _resolve("D1", "--validated") == 1
    assert "disk full" in capsys.readouterr().err and led.read_bytes() == before and list(led.parent.glob("*.tmp")) == []
    monkeypatch.setattr(os, "replace", real)
    pure = decisions.resolve
    monkeypatch.setattr(decisions, "resolve", lambda *a: (led.write_text(led.read_text() + "edited\n"), pure(*a))[1])
    assert _resolve("D1", "--validated") == 1
    assert "changed" in capsys.readouterr().err and "edited" in led.read_text() and "[ ] D1" in led.read_text()


def test_whole_file_is_verified_before_writing(monkeypatch):
    md = _file(OPEN + "\n" + OPEN2)
    monkeypatch.setattr(decisions, "_MARKUP", re.compile("(?!)"))  # as if the note check missed `<!--`
    with pytest.raises(ValueError, match="D1"):  # the line alone reads back, the file does not
        decisions.resolve(md, "D1", "validated", "PO", "x <!-- y")
    assert "- [x] D1:" in decisions.resolve(md, "D1", "validated", "PO", "fine")  # the happy path still works


def test_inline_comment_in_the_target_line_is_refused(repo, monkeypatch, capsys):
    led = _dec(repo, "- [ ] D1: Use <!-- hidden --> paging → _(open)_ · kind: decision · why: w\n" + OPEN2)
    _tty(monkeypatch)
    before = led.read_bytes()
    assert _resolve("D1", "--validated") == 1
    assert "D1" in capsys.readouterr().err and led.read_bytes() == before
    assert _resolve("D4", "--validated") == 0  # a comment in another line does not matter


def test_empty_note_leaves_no_double_space(repo, monkeypatch):
    led = _dec(repo, KEPT)
    _tty(monkeypatch)
    assert _resolve("D4", "--validated") == 0
    line = next(x for x in led.read_text(encoding="utf-8").splitlines() if "D4:" in x)
    assert "  " not in line
    assert decisions.render_line(decisions.parse_decisions(led.read_text(encoding="utf-8"))["validated"][0]) == line


def test_temp_file_is_unique_and_the_mode_is_kept(repo, monkeypatch):
    led = _dec(repo, OPEN)
    _tty(monkeypatch)
    led.chmod(0o640)
    other = led.with_name(led.name + ".tmp")
    other.write_text("not mine", encoding="utf-8")
    assert _resolve("D1", "--validated") == 0
    assert other.read_text(encoding="utf-8") == "not mine" and (led.stat().st_mode & 0o777) == 0o640
    other.unlink()
    assert list(led.parent.glob("*.tmp")) == []


def test_resolve_only_reads_the_decisions_file_never_the_dod_ledger(repo, monkeypatch):
    led = _dec(repo, OPEN)
    other = repo / "dod" / "DoD_SPK-1.md"
    other.write_text("# DoD\n" + OPEN.replace("D1", "D9") + "\n", encoding="utf-8")  # a D line in a ledger is never read
    before = other.read_bytes()
    _tty(monkeypatch)
    assert _resolve("D1", "--validated") == 0 and _resolve("D9", "--validated") == 1 and other.read_bytes() == before
    assert "- [x] D1:" in led.read_text(encoding="utf-8")


def test_resolve_on_a_superseded_decision_prints_a_notice_and_changes_nothing_else(repo, monkeypatch, capsys):
    led = _dec(repo, (_dl(3, "x").rstrip() + "\n" + _dl(5, "supersedes D3: a").rstrip() + "\n" + _dl(6, "plain").rstrip()))
    _tty(monkeypatch)
    assert _resolve("D3", "--rejected") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["notice"] == "D3 is superseded by D5: this verdict is masked while D5 stands (a rejected D5 would bring it back)"
    d = decisions.parse_decisions(led.read_text(encoding="utf-8"))
    assert [x["id"] for x in d["rejected"]] == ["D3"]  # the verdict is recorded as always
    assert _resolve("D6", "--validated") == 0
    assert "notice" not in json.loads(capsys.readouterr().out)


# ---- L1-AC2: --resolve is refused when `approvers` is set on the target branch ----------------------------------------------

_ID = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
       "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
BASE = {"site": SITE, "project": "SPK"}


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env={**os.environ, **_ID, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}).stdout.strip()


def _target(repo, target_cfg, head_cfg=None):
    """Commit `target_cfg` (a dict, or a raw string) as the default branch with an origin/HEAD pointing at it; the working tree gets `head_cfg`."""
    _git(repo, "checkout", "-q", "-B", "main")
    cfg = repo / "skj.config.json"
    cfg.write_text(target_cfg if isinstance(target_cfg, str) else json.dumps(target_cfg))
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    if head_cfg is not None:
        cfg.write_text(json.dumps(head_cfg))


def test_l1_ac2_resolve_refused_when_approvers_set_on_the_target_branch(repo, monkeypatch, capsys):
    _target(repo, {**BASE, "approvers": [APPROVER], "approval": "comment"})
    led = _dec(repo, "\n".join([OPEN, VAL]))
    _tty(monkeypatch)
    before = led.read_bytes()
    assert _resolve("D1", "--validated") == 1 and _resolve("D1", "--rejected", "--force") == 1
    err = capsys.readouterr().err
    assert "--sync" in err and "approvers" in err and led.read_bytes() == before and list(led.parent.glob("*.tmp")) == []
    assert APPROVER not in err
    _tty(monkeypatch, False)  # the same text without a terminal: the approvers rule comes first
    assert _resolve("D1", "--validated") == 1 and "--sync" in capsys.readouterr().err
    led.unlink()  # and before the file is looked for
    assert _resolve("D1", "--validated") == 1 and "--sync" in capsys.readouterr().err


def test_l1_ac2_the_working_tree_cannot_hide_the_approvers(repo, monkeypatch, capsys):
    # the PR (working tree) deletes the approvers and the comment mode: the target still has them
    _target(repo, {**BASE, "approvers": [APPROVER], "approval": "comment"}, head_cfg=BASE)
    led = _dec(repo, OPEN)
    _tty(monkeypatch)
    before = led.read_bytes()
    assert _resolve("D1", "--validated") == 1 and "approvers" in capsys.readouterr().err and led.read_bytes() == before


def test_l1_ac2_approvers_added_only_in_the_working_tree_do_not_block(repo, monkeypatch):
    _target(repo, BASE, head_cfg={**BASE, "approvers": [APPROVER]})  # an unmerged edit is not the target's word
    led = _dec(repo, OPEN)
    _tty(monkeypatch)
    assert _resolve("D1", "--validated") == 0 and "- [x] D1:" in led.read_text(encoding="utf-8")


def test_l1_ac2_no_approvers_on_the_target_is_todays_behaviour(repo, monkeypatch):
    _target(repo, {**BASE, "approvers": []})
    led = _dec(repo, OPEN)
    _tty(monkeypatch)
    assert _resolve("D1", "--validated") == 0 and "- [x] D1:" in led.read_text(encoding="utf-8")


@pytest.mark.parametrize("target", ["{ not json", {**BASE, "approvers": "SECRETVALUE"}, {**BASE, "approvers": [APPROVER, APPROVER]}])
def test_l1_ac2_an_unreadable_target_config_fails_closed(repo, monkeypatch, capsys, target):
    _target(repo, target, head_cfg=BASE)  # the working tree is fine: only the target is unreadable
    led = _dec(repo, OPEN)
    _tty(monkeypatch)
    before = led.read_bytes()
    assert _resolve("D1", "--validated") == 1 and led.read_bytes() == before
    err = capsys.readouterr().err
    assert err.startswith("skj:") and "SECRETVALUE" not in err and APPROVER not in err


def test_l1_ac2_a_base_that_cannot_be_read_while_the_config_sets_trust_keys_fails_closed(repo, monkeypatch, capsys):
    (repo / "skj.config.json").write_text(json.dumps({**BASE, "approvers": []}))  # no origin/HEAD at all: nothing to check it against
    led = _dec(repo, OPEN)
    _tty(monkeypatch)
    before = led.read_bytes()
    assert _resolve("D1", "--validated") == 1 and led.read_bytes() == before
    assert "origin/HEAD" in capsys.readouterr().err


def test_l1_ac2_a_non_root_config_with_approvers_and_no_base_fails_closed(repo, monkeypatch, capsys):
    # no origin/HEAD; the repo-root config has no approvers, the one in the subdirectory (the one Config.load reads from cwd) has
    sub = repo / "sub"
    sub.mkdir()
    (sub / "skj.config.json").write_text(json.dumps({**BASE, "approvers": [APPROVER]}))
    led = _dec(repo, OPEN)
    monkeypatch.chdir(sub)
    _tty(monkeypatch)
    before = led.read_bytes()
    assert _resolve("D1", "--validated") == 1 and led.read_bytes() == before and list(led.parent.glob("*.tmp")) == []
    err = capsys.readouterr().err
    assert "not the repo-root" in err and APPROVER not in err


def test_l1_ac2_list_and_add_do_not_need_the_target_config(repo, capsys):
    _target(repo, "{ not json")  # an unreadable target blocks only --resolve
    (repo / "skj.config.json").write_text(json.dumps(BASE))
    assert _add() == 0 and run(KEY) == 0


# ---- placeholders, no network, no identity -------------------------------------------------------------------------------------

def test_local_verbs_never_build_a_client_or_read_an_identity(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a local verb built a client")
    monkeypatch.setattr("skj.__main__.default_client", boom)
    monkeypatch.setattr("skj.secretstore.get", boom)
    _tty(monkeypatch)
    assert main(["decisions", KEY, "--add", "t", "--kind", "decision"]) == 0
    assert main(["decisions", KEY]) == 0
    assert main(["decisions", KEY, "--resolve", "D1", "--validated"]) == 0


# ---- L1b: the decisions file is a repository file the PR controls: no regex may backtrack over a run of blanks ------------------
import random
import signal

# The L1 code (commit 230b295, verbatim), kept as the oracle the linear parser must agree with: its regexes `_COLON`, `_STATUS` and
# `_SEP` backtrack quadratically over a blank run. It runs against the current module's other helpers (`_visible`, `_answers`, ...).
_OLD_SRC = r'''
_CAND = re.compile(r"^\s*(?:[-*+]\s+)?\[([ xX])\]\s*D(\d+)(.*)$")
_COLON = re.compile(r"\s*:\s*(.*?)\s*$")
_STATUS = re.compile(r"\s*(?:→|->)\s*(_\(open\)_|validated|rejected)(?=\s*(?:·|$))")
_KEYS = ("kind", "why", "by", "note", "posted", "used", "via")
_SEP = re.compile(r"\s*·\s*(?=(?:" + "|".join(_KEYS) + r")\s*:)")

MAX_LINE = 2000

_SUP = re.compile(r"supersedes D([0-9]{1,6}): ")

def _line(raw):
    """One file line -> the decision dict, or None when it is a `D` line that breaks the grammar."""
    m = _CAND.match(raw)
    c = _COLON.match(m.group(3)) if m else None
    if not c or not keys.line_number(m.group(2)):  # H1a's rule, added to the oracle: `D05`, `D0`, `D٣` break the grammar
        return None
    done = m.group(1) in "xX"
    rest = c.group(1)
    st = list(_STATUS.finditer(rest))  # the LAST status arrow: a `→` inside the text must not split it
    if not st or (st[-1].group(1) == _OPEN) == done:
        return None
    s = st[-1]
    text, tail = rest[:s.start()].strip(), rest[s.end():]
    first, *parts = _SEP.split(tail)
    kv = {}
    for p in parts:
        k, _, v = p.partition(":")
        if k.strip() in kv:
            return None
        kv[k.strip()] = v.strip()
    kind = kv.get("kind", "decision")
    posted = _POSTED.fullmatch(kv["posted"]) if "posted" in kv else None
    bad = (first.strip() or not text or kind not in KINDS or ("posted" in kv and not posted)
           or (not done and not (kv.get("kind") and kv.get("why")))
           or (done and not kv.get("by")) or (not done and ("by" in kv or "note" in kv or "via" in kv))
           or ("via" in kv and not _VIA.fullmatch(kv["via"])))
    if bad:
        return None
    sup = _SUP.match(text)
    return {"id": f"D{m.group(2)}", "kind": kind, "text": text, "why": kv.get("why", ""),
            "status": "open" if not done else s.group(1), "by": kv.get("by", ""), "note": kv.get("note", ""),
            "posted": {"comment_id": posted.group(1), "hash": posted.group(2)} if posted else None,
            "used": kv.get("used", ""), **({"supersedes": f"D{sup.group(1)}"} if sup else {}), **({"via": kv["via"]} if "via" in kv else {})}

def has_decision_lines(md):
    """True when the decisions file has a `D<n>` checkbox line outside fences and comments, well-formed or not."""
    return any(_CAND.match(ln) for ln in _visible(md)[0].splitlines())

def parse_decisions(md):
    """The decisions file -> {'open': [...], 'validated': [...], 'rejected': [...], 'unparsed': bool}. Lines inside code
    fences or <!-- --> comments are ignored; the first line of a duplicate id wins. `unparsed` is true when a `D` line
    breaks the grammar (it is left out) or a fence or comment is left open. A text that starts with `supersedes D<n>: `
    adds `supersedes: "D<n>"` to its entry (no other entry has the key). Empty text -> all empty."""
    out = {"open": [], "validated": [], "rejected": []}
    body, out["unparsed"] = _visible(md)
    seen = set()
    for raw in body.splitlines():
        if not _CAND.match(raw):  # linear: only the `- [ ] D<n>` prefix is matched here
            continue
        d = _line(raw) if len(raw) <= MAX_LINE else None
        if d is None:
            out["unparsed"] = True
        elif d["id"] not in seen:
            seen.add(d["id"])
            out[d["status"]].append(d)
    return out

def resolve(md, d_id, status, by, note, force=False, via=None):
    """The file text with the first `d_id` line rewritten as validated|rejected; its kind, why, posted and used
    markers and its line ending are kept, every other byte is unchanged. `via` (a comment id string, digits only; `--sync` will pass the
    approver's comment) is written as ` · via: <id>`; without it an old `via` is dropped, since the new verdict no longer comes from that comment. A line that is already resolved needs `force`.
    ValueError for a bad status, role or note, an unknown id, a line holding an inline comment, or a result that
    `parse_decisions` would not read back the same over the whole file."""
    by, note = by.strip(), note.strip()
    if via is not None and not (isinstance(via, str) and _VIA.fullmatch(via)):
        raise ValueError("via must be a comment id (digits only)")
    if status not in ("validated", "rejected"):
        raise ValueError("the status must be validated or rejected")
    if not _BY.fullmatch(by):
        raise ValueError("--by must be 1-40 letters, digits or spaces (a role, never a name)")
    if len(note) > MAX_NOTE or _CTRL.search(note) or _MARKUP.search(note):
        raise ValueError(f"--note must be at most {MAX_NOTE} characters with no control characters, line breaks, "
                         "`<!--`, `-->` or code fences")
    out, found = [], False
    for raw, v in zip(md.splitlines(keepends=True), _visible(md)[0].split("\n")):
        d = _line(v) if not found and _CAND.match(v) and len(v) <= MAX_LINE else None
        if d and d["id"] == d_id:
            found = True
            if v != raw.splitlines()[0]:
                raise ValueError(f"{d_id} holds an inline <!-- --> comment or fence that a rewrite would drop; edit it by hand")
            if d["status"] != "open" and not force:
                raise ValueError(f"{d_id} is already {d['status']} (by {d['by']}); pass --force to overwrite that verdict")
            new = {**{k: v for k, v in d.items() if k != "via"}, "status": status, "by": by, "note": note, **({"via": via} if via else {})}
            line = render_line(new)
            if len(line) > MAX_LINE or _line(line) != new:  # over-long, or the note collides with the grammar
                raise ValueError(f"the resolved line would be longer than {MAX_LINE} characters or would not read back "
                                 "(a note must not contain ` · kind:`-style markers or `→ validated`)")
            raw = line + raw[len(raw.splitlines()[0]):]  # splitlines also ends a line at U+2028, \x0c, \x85, \x1c
        out.append(raw)
    if not found:
        raise ValueError(f"no decision {d_id} in the decisions file (see `skj decisions <KEY>`)")
    new_md = "".join(out)
    a, b = md.splitlines(keepends=True), new_md.splitlines(keepends=True)
    before, after = parse_decisions(md), parse_decisions(new_md)
    if (len(a) != len(b) or sum(x != y for x, y in zip(a, b)) != 1 or after["unparsed"] != before["unparsed"]
            or _ids(after) != {**_ids(before), d_id: status}):
        raise ValueError(f"the rewrite of {d_id} would change more than that line or break the file; nothing written")
    return new_md

def _supersedes_target(md, target):
    """Refuse (ValueError, fixed texts) unless `target` is a decision of `md` the developer may answer: it exists, is not an
    override, is rejected or open (not validated), and no live decision answers it yet."""
    if not re.fullmatch(r"D[0-9]{1,6}", target if isinstance(target, str) else ""):
        raise ValueError("--supersedes must be D<n>: the letter D and one to six digits")
    ents, by, _, stands = _answers(parse_decisions(md))
    if target not in ents:
        raise ValueError("--supersedes names a decision that is not in the decisions file")
    if ents[target]["kind"] == "override":
        raise ValueError("--supersedes cannot answer an override")
    if ents[target]["status"] == "validated":
        raise ValueError("--supersedes can only answer a rejected or open decision, not a validated one")
    if ents[target]["status"] == "open" and _stale(ents[target]):
        raise ValueError(f"post {target} first (`skj decisions <KEY> --post`): the PO has not seen it, so it cannot be superseded")
    live = [s for s in by.get(target, ()) if stands[s]]
    if live:
        raise ValueError(f"{target} is already superseded by {live[0]}: supersede {live[0]} instead, or have the PO reject it first")

def add(md, kind, text, why="", _kinds=_ADD_KINDS, _min_why=0, supersedes=None):
    """(the file text with one new open `D<n>` line appended, "D<n>"). n = highest id in the file + 1 (a malformed or
    duplicate line still counts), grammar-exact. @mentions and addresses are replaced as in a posted comment.
    ValueError (never echoing the text) for a bad kind, a text of 0 or over MAX_TEXT characters, a why over MAX_NOTE,
    control characters, line breaks, `<!--`, `-->`, code fences, or a line that would not read back (so a file with an
    unclosed fence or comment, which would hide the new line, is refused too). `supersedes` ("D<n>"): the line's text then
    starts with `supersedes D<n>: ` (see `_supersedes_target` for what is refused); the prefix cannot be typed by hand."""
    if kind not in _kinds:
        raise ValueError("--kind must be decision or assumption")
    for name, v in (("the text", text), ("--why", why)):  # before strip(), which would drop a trailing line break
        if _CTRL.search(v) or _MARKUP.search(v):
            raise ValueError(f"{name} must not contain control characters, line breaks, `<!--`, `-->` or code fences")
    text, why = text.strip(), why.strip()
    if kind != "override":  # an override reason is refused for markup, never rewritten
        text, why = _scrub(text), _scrub(why)
    why = why or "unspecified"
    if not 1 <= len(text) <= MAX_TEXT:
        raise ValueError(f"the text must be 1 to {MAX_TEXT} characters")
    if not _min_why <= len(why) <= MAX_NOTE:
        raise ValueError(f"--why must be {f'{_min_why} to ' if _min_why else 'at most '}{MAX_NOTE} characters")
    if _SUP.match(text):
        raise ValueError("the text must not start with `supersedes D<n>: `; use --supersedes D<n>")
    if supersedes is not None:
        _supersedes_target(md, supersedes)
        text = f"supersedes {supersedes}: {text}"
    nums = [int(n) for n in re.findall(r"(?m)^[ \t]*(?:<!--[ \t]*)?(?:[-*+][ \t]+)?\[[ xX]\][ \t]*D(\d+)", md)]  # raw text: an id inside a comment or fence is taken too
    d_id = f"D{max(nums, default=0) + 1}"
    new = {"id": d_id, "kind": kind, "text": text, "why": why, "status": "open", "by": "", "note": "",
           "posted": None, "used": "", **({"supersedes": supersedes} if supersedes is not None else {})}
    line = render_line(new)
    eol = "\r\n" if "\r\n" in md else "\n"
    new_md = md + ("" if not md or md.endswith(("\n", "\r")) else eol) + line + eol
    before, after = parse_decisions(md), parse_decisions(new_md)
    if len(line) > MAX_LINE or _line(line) != new or _ids(after) != {**_ids(before), d_id: "open"} \
            or after["unparsed"] != before["unparsed"]:
        raise ValueError("the line would not read back from the decisions file (a text or --why holding ` · kind:`-style "
                         "markers or `→ validated`, or an unclosed code fence or comment in the file); nothing written")
    return new_md, d_id

def stamp_used(md, ids, date):
    """The file text with ` · used: <date>` appended to the lines of the decisions in `ids`, once: a line that already
    has `used` keeps its date. Pure: the caller writes it (`_atomic_write`). Every other byte, line endings included,
    is unchanged. ValueError (nothing returned) for a bad date, an id that is not a line of the file, or a line holding
    an inline comment or fence that a rewrite would drop."""
    if not isinstance(date, str) or not _DATE.fullmatch(date):
        raise ValueError("the date must be YYYY-MM-DD")
    out, todo = [], set(ids)
    for raw, v in zip(md.splitlines(keepends=True), _visible(md)[0].split("\n")):
        text = raw.splitlines()[0]  # the terminator may be U+2028 etc.: keep it as it is
        d = _line(v) if _CAND.match(v) and len(v) <= MAX_LINE else None
        if d and d["id"] in todo:
            todo.discard(d["id"])
            if v != text:
                raise ValueError(f"{d['id']} holds an inline <!-- --> comment or fence that a rewrite would drop; edit it by hand")
            if not d["used"]:
                raw = f"{text} · used: {date}{raw[len(text):]}"
        out.append(raw)
    if todo:
        raise ValueError(f"no such decision in the decisions file: {', '.join(sorted(todo))}")
    return "".join(out)
'''
_OLD = dict(vars(decisions))
exec(_OLD_SRC, _OLD)


def _outcome(fn, *args, **kw):
    try:
        return ("ok", fn(*args, **kw))
    except ValueError as e:
        return ("err", str(e))


def _everything(ns, md):
    """What the public functions of `ns` (the old oracle or the module under test) make of one file, errors included."""
    parsed = ns["parse_decisions"](md)
    out = {"parse": parsed, "has": ns["has_decision_lines"](md)}
    for d_id in [d["id"] for k in ("open", "validated", "rejected") for d in parsed[k]] + ["D1", "D9"]:
        out["resolve " + d_id] = _outcome(ns["resolve"], md, d_id, "validated", "PO", "n", force=True)
        out["reject " + d_id] = _outcome(ns["resolve"], md, d_id, "rejected", "PO", "", via="12")
        out["stamp " + d_id] = _outcome(ns["stamp_used"], md, [d_id], "2026-01-01")
    out["add"] = _outcome(ns["add"], md, "decision", "t")
    out["add why"] = _outcome(ns["add"], md, "assumption", "t", "w", supersedes="D1")
    return out


_WS = [" ", "", "  ", "\t", " ", " ", "　", "\x1f", " \t ", "​"]  # ​ is not whitespace
_KV = ["kind: decision", "kind: assumption", "kind: override", "kind: nope", "why: w", "why:", "by: PO", "by:", "note: n", "note: a → b",
       "posted: 5:abcdef12", "posted: 05:abcdef12", "posted: 5:abc", "used: 2026-01-01", "via: 12", "via: 012", "kind:decision", "extra: x",
       "kind : decision", "why : w", "why: a → validated", "note: b -> rejected z", "why: c → _(open)_ x", "note: → validated ·"]


def _generated_lines(n=600, seed=11):
    rnd = random.Random(seed)
    ws = lambda: rnd.choice(_WS) if rnd.random() < 0.5 else " "  # noqa: E731
    for _ in range(n):
        done = rnd.random() < 0.4
        head = rnd.choice(["- ", "* ", "+ ", "", "  - ", "\t- "]) + rnd.choice(["[x]", "[X]"] if done else ["[ ]", "[ ]", "[x]"]) + ws()
        head += "D" + rnd.choice(["1", "2", "7", "10", "0", "05", "007", "00"]) + ws() + ":" + ws()
        text = "".join(ws() + rnd.choice(["a", "b c", "→", "->", "·", "|", "supersedes D1: ", "x → _(open)_", "y -> validated", "[skj:"]) for _ in range(rnd.randint(1, 4)))
        status = rnd.choice(["validated", "rejected"] if done else ["_(open)_", "_(open)_", "validated", "open", "validatedx"])
        arrow = ws() + rnd.choice(["→", "->", "→", "→→", "-->", "—>", ""]) + ws()
        need = (["by: PO"] if done else ["kind: decision", "why: w"]) if rnd.random() < 0.8 else []  # most lines carry the keys their status needs
        tail = "".join(ws() + rnd.choice(["·", "·", "·", "|", "· ·"]) + ws() + kv for kv in need + [rnd.choice(_KV) for _ in range(rnd.randint(0, 4))])
        yield head + text + arrow + status + tail + rnd.choice(["", ws(), " ·", " → _(open)_", "x"])


def _fixture_lines():
    ok = [OPEN, VAL, REJ, OPEN2, KEPT, OPEN + " · posted: 42:" + decisions.line_hash("Use cursor paging"), "- [x] D5: a → b → c → validated · by: PO · note: ok · posted: 7:deadbeef",
          "- [x] D6: Cap → validated · by: approver · note: fine · via: 123 · posted: 7:deadbeef", "- [ ] D1: supersedes D2: Use paging → _(open)_ · kind: decision · why: w",
          "- [ ] D4: Skip the freeze → _(open)_ · kind: override · why: outage · used: 2026-11-02", "- [ ] D4 not in the grammar", "- [ ] D1: a → _(open)_ · kind: decision"]
    return ok + [ln for ln in Path(__file__).read_text(encoding="utf-8").splitlines() if re.match(r'\s*(?:"|f?")?\s*- \[[ x]\] D\d', ln)]


def test_l1b_ac2_the_linear_parser_agrees_with_the_l1_code_on_generated_and_fixture_lines():
    lines = list(_generated_lines()) + _fixture_lines()
    assert len(lines) >= 300 and all(len(x) <= 300 for x in lines)
    shown = {"open": 0, "validated": 0, "rejected": 0, "unparsed": 0, "resolved": 0, "stamped": 0, "added": 0}
    for i, line in enumerate(lines):  # one line per file, so nothing hides behind a duplicate id; every third file also holds neighbours
        md = _file(line) if i % 3 else _file("\n".join([OPEN, line, VAL, REJ, line]))
        new, old = _everything(vars(decisions), md), _everything(_OLD, md)
        assert new == old, repr(line)
        for k in ("open", "validated", "rejected"):
            shown[k] += bool(new["parse"][k])
        shown["unparsed"] += new["parse"]["unparsed"]
        shown["resolved"] += any(k.startswith("resolve") and v[0] == "ok" for k, v in new.items() if k != "parse")
        shown["stamped"] += any(k.startswith("stamp") and v[0] == "ok" for k, v in new.items() if k != "parse")
        shown["added"] += new["add"][0] == "ok"
    assert min(shown.values()) > 15, shown  # the sample really reaches every outcome


# ---- timing: every function that takes file text, on the shapes of a PR-controlled file ------------------------------------------

_MIB = 1 << 20
_BOUND = 2.0  # seconds per call; the linear code takes well under 0.5 s on 1 MiB, the L1 code took 6-16 s on the `wide` layout
_needs_alarm = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs signal.setitimer to cut a hung call off")


class _TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise _TooSlow()


def _timed(fn):
    """Seconds fn() took; _BOUND when it was cut off there (a quadratic parser must fail the assertion, never hang the suite).
    The ValueErrors the verbs raise on purpose are part of their contract here."""
    # CPU time, not the wall clock (H1b): load slows the clock, not the work; the timed calls run no subprocess and never block
    old = signal.signal(signal.SIGPROF, _alarm)
    signal.setitimer(signal.ITIMER_PROF, _BOUND)
    t0 = time.process_time()
    try:
        fn()
    except _TooSlow:
        return _BOUND
    except ValueError:
        pass
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, old)
    return time.process_time() - t0


_P = "- [ ] D1: a"
_Q = _P + " → _(open)_"
_T = " · kind: decision · why: w"
_R = "- [x] D1: a → validated · by: PO"
# name: (head, unit, tail). The line is `head + unit * k + tail`, k chosen for the length; a unit of one blank takes each blank kind.
SHAPES = {
    "text-gap": (_P, " ", "b → _(open)_" + _T), "before-arrow": (_P, " ", "→ _(open)_" + _T), "before-ascii-arrow": (_P, " ", "-> _(open)_" + _T),
    "after-arrow": (_P + " →", " ", "_(open)_" + _T), "after-ascii-arrow": (_P + " ->", " ", "_(open)_" + _T),
    "between-arrows": (_P + " →", " ", "→ _(open)_" + _T), "before-status-tail": (_Q, " ", "x" + _T),
    "before-dot": (_Q, " ", "·" + _T[2:]), "after-dot": (_Q + " ·", " ", "kind: decision · why: w"),
    "before-key-colon": (_Q + " · kind", " ", ": decision · why: w"), "after-key-colon": (_Q + " · kind:", " ", "decision · why: w"),
    "before-pipe": (_Q + _T + " ·", " ", "| b"), "after-pipe": (_Q + " | ", " ", "b"), "id-colon": ("- [ ] D1", " ", ": a → _(open)_" + _T),
    "after-id-colon": ("- [ ] D1:", " ", "a → _(open)_" + _T), "indent": ("", " ", _P + " → _(open)_" + _T), "bullet-box": ("-", " ", "[ ] D1: a → _(open)_" + _T),
    "box-id": ("- [ ]", " ", "D1: a → _(open)_" + _T), "box-id-x": ("- [x]", " ", "D1: a → validated · by: PO"),
    "posted-value": (_Q + _T + " · posted:", " ", "5:abcdef12"), "posted-colon": (_Q + _T + " · posted", " ", ": 5:abcdef12"),
    "before-posted": (_Q + _T, " ", "· posted: 5:abcdef12"), "used-value": (_Q + _T + " · used:", " ", "2026-01-01"),
    "before-used": (_Q + _T, " ", "· used: 2026-01-01"), "via-value": (_R + " · via:", " ", "12"), "via-colon": (_R + " · via", " ", ": 12"),
    "before-via": (_R, " ", "· via: 12"), "by-value": ("- [x] D1: a → validated · by:", " ", "PO"), "note-tail": (_R + " · note: n", " ", "x"),
    "many-arrows": (_P, " →", ""), "many-ascii-arrows": (_P, " ->", ""), "many-dots": (_Q, " ·", ""), "many-pipes": (_Q, " |", ""),
    "many-dot-keys": (_Q, " · kind:", ""), "many-dot-posted": (_Q + _T, " · posted:", ""), "many-ids": (_P, " D1:", ""), "many-fragments": (_P, " [skj:", ""),
    "many-status": (_P, " → validated", ""), "many-open": (_P, " → _(open)_", ""), "many-open-dots": (_P, " → _(open)_ ·", ""),
    "many-lines": ("", "- [ ] D1: a → _(open)_" + _T + "\n", ""), "many-markers": (_P, " <!-- ", ""), "many-fences": (_P, " ```", ""),
    "many-at": ("- [ ] D1: a", " @a", ""), "many-mentions": (_P, " [~a", ""), "many-addresses": (_P, " a@a.", ""),
}
BLANKS = (" ", "\t", " ", "　")


def _line_of(name, n, blank):
    head, unit, tail = SHAPES[name]
    unit = blank + unit[1:] if unit.startswith(" ") else unit
    k = max(1, (n - len(head) - len(tail)) // len(unit))
    return head + unit * k + tail


def _payload(name, layout, blank):
    """About 1 MiB: `one` line; `wide` 500 lines of about 1990 characters (under MAX_LINE); `short` 20000 lines of about 52."""
    if layout == "one":
        return _line_of(name, _MIB, blank)
    head, _, tail = SHAPES[name]
    return "\n".join([_line_of(name, 1990 if layout == "wide" else max(52, len(head) + len(tail) + 4), blank)] * (500 if layout == "wide" else 20000))


def _calls(md):
    """(name, call) for every function of decisions.py that takes file text; `md` is the whole text."""
    d = "- [ ] D1: a → _(open)_ · kind: decision · why: b"
    parse = decisions.parse_decisions
    return [
        ("parse_decisions", lambda: parse(md)), ("has_decision_lines", lambda: decisions.has_decision_lines(md)),
        ("effective, supersede_problem, masked_by, _ids", lambda: [f(parse(md)) for f in (decisions.effective, decisions.supersede_problem, decisions._ids,
                                                                                         lambda p: decisions.masked_by(p, "D1"))]),
        ("resolve", lambda: decisions.resolve(md + "\n" + d + "\n", "D1", "validated", "PO", "", force=True)),
        ("resolve first", lambda: decisions.resolve(d + "\n" + md[:200_000], "D1", "rejected", "PO", "n", force=True, via="12")),
        ("add", lambda: decisions.add(md, "decision", "x")), ("add supersedes", lambda: decisions.add(md, "decision", "x", supersedes="D1")),
        ("add text", lambda: decisions.add("", "decision", md)),
        ("stamp_used", lambda: decisions.stamp_used(md + "\n" + d + "\n", ["D1"], "2026-01-01")),
        ("_scrub", lambda: decisions._scrub(md)), ("line_hash", lambda: decisions.line_hash(md)),
    ]


def _slow(md):
    """{function: seconds} for the calls that took _BOUND or more (the first one only: a failing run stays short)."""
    for n, call in _calls(md):
        if (dt := _timed(call)) >= _BOUND:
            return {n: dt}
    return {}


def _file_of(payload):
    return decisions.render_file(KEY) + payload + "\n"


# the committed cases sample the shapes (two thirds on `wide` with a rotating blank, the rest on `one`, every fifth on `short`); the full shape x blank x
# layout x function sweep is `scripts`-free and ad hoc (its table is in the task report)
_NAMES = list(SHAPES)
_CASES = ([(n, "wide", BLANKS[i % 4]) for i, n in enumerate(_NAMES) if i % 3 != 2] + [(n, "one", BLANKS[(i + 1) % 4]) for i, n in enumerate(_NAMES) if i % 3 == 2]
          + [(n, "short", BLANKS[(i + 2) % 4]) for i, n in enumerate(_NAMES) if i % 5 == 0])


@_needs_alarm
@pytest.mark.parametrize("name,layout,blank", _CASES, ids=lambda v: repr(v) if v in BLANKS else str(v))
def test_l1b_ac1_every_function_is_linear_on_blank_run_shapes(name, layout, blank):
    pay = _payload(name, layout, blank)
    assert _MIB * 0.8 < len(pay) < _MIB * 1.6
    assert max(map(len, pay.split("\n"))) <= (decisions.MAX_LINE if layout != "one" else _MIB * 1.25)
    assert not _slow(_file_of(pay))


@_needs_alarm
def test_l1b_ac1_the_file_verbs_are_linear_on_a_wide_file(tmp_path):
    """`_read` and the three apply paths, end to end on a file that is under the 1 MiB cap."""
    lines = [_line_of(n, 1990, " ") for n in ("before-arrow", "after-dot", "before-key-colon", "many-arrows", "many-dot-keys")] * 70
    path = tmp_path / "Decisions_SPK-1.md"
    path.write_text(_file(OPEN) + "\n".join(lines) + "\n", encoding="utf-8")
    assert _MIB * 0.7 < path.stat().st_size < _MIB  # the cap is in bytes: `→` and `·` take three
    calls = [("_read", lambda: decisions._read(path)), ("apply_resolve", lambda: decisions.apply_resolve(path, "D1", "validated", "PO", "n", True)),
             ("apply_add", lambda: decisions.apply_add(path, KEY, "decision", "x")), ("apply_stamp", lambda: decisions.apply_stamp(path, KEY, ["D1"]))]
    assert not {n: dt for n, call in calls if (dt := _timed(call)) >= _BOUND}


@_needs_alarm
def test_l1b_ac1_a_long_id_is_linear():
    big = "- [ ] D" + "9" * 1_000_000 + ": a → _(open)_" + _T
    md = _file(big + "\n" + OPEN)
    assert not {n: dt for n, call in [("add", lambda: decisions.add(md, "decision", "x")), ("parse", lambda: decisions.parse_decisions(md))] if (dt := _timed(call)) >= _BOUND}


def test_l1b_ac2_next_number_is_max_plus_one_as_int_arithmetic_has_it():
    rnd = random.Random(5)
    pool = ["0", "00", "9", "99", "009", "10", "7", "٣", "١٢", "999", "1000", "0999"]  # includes non-ASCII decimal digits, which `\d` and int() accept
    for _ in range(300):
        ns = [rnd.choice(pool) for _ in range(rnd.randint(0, 4))]
        assert decisions._next_number(ns) == str(max([int(n) for n in ns], default=0) + 1), ns


# ==== W5: `decisions --post` (against the fake Jira only) ========================================================================
import hashlib

from fakejira import OTHER_ID, FakeJira
from skj import adf, client as cl, comments, config, questions
from skj.client import Client

W5 = "\n".join([OPEN, VAL, REJ, OPEN2])
W5_REST = ("Decisions and assumptions made during development; please validate or reject:\n\n"
           "- D1 (decision): Use cursor paging — why: the story names no page size\n"
           "- D4 (assumption): Retry twice — why: the story is silent")


@pytest.fixture
def fake():
    f = FakeJira()
    assert f.add_issue("A fictional story") == KEY
    return f


def dpost(fake, *args, client=None):
    return main(["decisions", KEY, "--post", *args], client_factory=client or fake.client)


def _posts(fake):
    return [e for e in fake.log if e["method"] != "GET"]


def _quoted_body(out):
    lines = out.split("Comment body (ADF as Markdown, exactly what is sent):\n\n", 1)[1].split("\n")
    body = []
    for x in lines:
        if not x.startswith(">"):
            break
        body.append(x[2:] if x.startswith("> ") else "")
    return "\n".join(body)


def _planned(repo):
    return decisions.body(decisions.unposted(decisions.parse_decisions(_dec(repo).read_text(encoding="utf-8"))))


def _stamps(repo):
    p = decisions.parse_decisions(_dec(repo).read_text(encoding="utf-8"))
    return {d["id"]: d["posted"] for d in _all(p) if d["posted"]}


def test_post_preview_matches_body(repo, fake, capsys):
    """W5-AC1: the dry run sends nothing and quotes the exact ADF-as-Markdown body; the apply sends exactly that ADF, first line the
    marker whose hash8 is sha256 of the canonical Markdown after it. Only the unposted open lines are listed."""
    f = _dec(repo, W5)
    before = f.read_bytes()
    assert dpost(fake) == 0
    out = capsys.readouterr().out
    assert _posts(fake) == [] and f.read_bytes() == before and "dry run: nothing posted" in out
    preview = _quoted_body(out)
    md, doc, h = _planned(repo)
    assert preview == md == adf.adf_to_md(doc, strict=True)
    rest = md.split("\n\n", 1)[1]
    assert md.split("\n", 1)[0] == "[skj:decisions %s]" % h and h == hashlib.sha256(rest.encode()).hexdigest()[:8]
    assert rest == W5_REST
    assert [(e["method"], e["path"]) for e in fake.log] == [
        ("GET", "/issue/" + KEY), ("GET", "/myself"), ("GET", "/issue/%s/comment" % KEY), ("GET", "/issue/%s/comment" % KEY)]
    assert dpost(fake, "--apply") == 0
    assert [(e["method"], e["path"]) for e in _posts(fake)] == [("POST", "/issue/%s/comment" % KEY)]
    (c,) = fake.comments.values()
    assert adf.adf_to_md(c["body"], strict=True) == preview and c["body"] == doc and c["visibility"] is None


def test_the_plan_is_deterministic_and_holds_no_account_id(repo, fake, capsys):
    _dec(repo, W5)
    fake.add_comment(KEY, adf.md_to_adf("[skj:decisions 00000000]\n\nx"), author=fake.user(OTHER_ID))
    assert dpost(fake) == 0
    a = capsys.readouterr().out
    assert dpost(fake) == 0
    assert a == capsys.readouterr().out
    for secret in (fake.account_id, OTHER_ID, fake.email, fake.token, fake.account_id[:8], OTHER_ID[:8]):
        assert secret not in a


def test_repeat_post_is_noop(repo, fake, capsys):
    """W5-AC2: after a post the lines carry `posted:`; the next run has nothing to post and sends no request at all."""
    _dec(repo, W5)
    assert dpost(fake, "--apply") == 0
    capsys.readouterr()
    n = len(fake.log)
    assert dpost(fake, "--apply") == 0
    out = capsys.readouterr().out
    assert len(fake.log) == n and len(fake.comments) == 1
    assert "action: none (no unposted decisions; nothing to post)" in out and "Comment body" not in out


def test_an_own_comment_found_without_stamps_is_a_noop_that_stamps(repo, fake, capsys):
    """The comment was posted but the file was not stamped (the stamp failed, or a fresh clone): Jira's own comment decides."""
    f = _dec(repo, W5)
    before = f.read_text(encoding="utf-8")
    assert dpost(fake, "--apply") == 0
    (cid,) = fake.comments
    f.write_text(before, encoding="utf-8")  # the stamps are gone
    assert dpost(fake) == 0
    out = capsys.readouterr().out
    assert "action: none (comment %s already shows these decisions)" % cid in out and "D1, D4" in out
    assert dpost(fake, "--apply") == 0
    assert len(_posts(fake)) == 1 and set(_stamps(repo)) == {"D1", "D4"}
    assert "nothing posted: comment %s already shows these decisions" % cid in capsys.readouterr().out


def _timeout_on_post(fake, applied):
    fake.fail_transport(on=fake._n + 5, applied=applied)  # issue, identity, comments, probe, then the POST


def test_timeout_then_rerun_finds_marker(repo, fake, capsys):
    """W5-AC3 / Review Focus 6: the POST times out after Jira applied it: one fresh re-read finds it, the lines are stamped, exit 1, no
    second POST; the next run has nothing to post."""
    _dec(repo, W5)
    _timeout_on_post(fake, applied=True)
    assert dpost(fake, "--apply") == 1
    out = capsys.readouterr().out
    (cid,) = fake.comments
    assert "posted (found on re-read): comment %s" % cid in out
    assert [e["method"] for e in _posts(fake)] == ["POST"] and [e["method"] for e in fake.log[5:]] == ["GET", "GET"]
    assert {i: s["comment_id"] for i, s in _stamps(repo).items()} == {"D1": cid, "D4": cid}
    assert dpost(fake, "--apply") == 0 and len(_posts(fake)) == 1 and len(fake.comments) == 1


def test_timeout_before_jira_applied_it_is_not_posted_again_in_that_run(repo, fake, capsys):
    f = _dec(repo, W5)
    before = f.read_bytes()
    _timeout_on_post(fake, applied=False)
    assert dpost(fake, "--apply") == 1
    assert "write uncertain: not found on re-read; not posted again in this run" in capsys.readouterr().out
    assert len(_posts(fake)) == 1 and fake.comments == {} and f.read_bytes() == before
    assert dpost(fake, "--apply") == 0 and len(_posts(fake)) == 2 and len(fake.comments) == 1


def test_a_failed_re_read_after_a_timeout_posts_nothing_more(repo, fake, capsys):
    f = _dec(repo, W5)
    before = f.read_bytes()
    _timeout_on_post(fake, applied=True)
    fake.fail_transport(on=fake._n + 6, applied=False)
    assert dpost(fake, "--apply") == 1
    assert "the re-read failed; not posted again in this run" in capsys.readouterr().out
    assert len(_posts(fake)) == 1 and f.read_bytes() == before


def _client_answering_post(fake, answer):
    def opener(method, url, headers, body, timeout):
        if method == "POST":
            fake.log.append({"method": method, "path": "intercepted", "write": True})
            if answer == "apply-then-201-garbage":
                fake.opener(method, url, headers, body, timeout)
                return 201, {"Content-Type": "application/json"}, b"not json"
            return answer[0], {"Content-Type": "application/json"}, json.dumps(answer[1]).encode()
        return fake.opener(method, url, headers, body, timeout)
    return Client(fake.site, fake.email, fake.token, opener=opener, sleep=lambda s: None)


@pytest.mark.parametrize("status", [400, 403, 404, 413])
def test_a_4xx_is_a_fixed_message_with_the_status_only(repo, fake, capsys, status):
    f = _dec(repo, W5)
    before = f.read_bytes()
    c = _client_answering_post(fake, (status, {"errorMessages": ["fake: secret server text"], "errors": {}}))
    assert dpost(fake, "--apply", client=lambda: c) == 1
    cap = capsys.readouterr()
    assert "failed: Jira answered HTTP %d; nothing posted" % status in cap.out
    assert "secret" not in cap.out + cap.err and f.read_bytes() == before and len(_posts(fake)) == 1


def test_a_5xx_or_an_unreadable_2xx_is_uncertain_and_re_read(repo, fake, capsys):
    _dec(repo, W5)
    c = _client_answering_post(fake, (502, {"errorMessages": ["fake: secret"]}))
    assert dpost(fake, "--apply", client=lambda: c) == 1
    assert "write uncertain: not found on re-read" in capsys.readouterr().out and _stamps(repo) == {}
    c = _client_answering_post(fake, (201, {"id": 10000}))  # an int id is an unreadable answer: re-read, never coerced
    assert dpost(fake, "--apply", client=lambda: c) == 1
    assert "write uncertain: not found on re-read" in capsys.readouterr().out and _stamps(repo) == {}
    c = _client_answering_post(fake, "apply-then-201-garbage")
    assert dpost(fake, "--apply", client=lambda: c) == 1
    (cid,) = fake.comments
    assert "posted (found on re-read): comment %s" % cid in capsys.readouterr().out and set(_stamps(repo)) == {"D1", "D4"}


def test_posted_id_recorded_as_string(repo, fake, capsys):
    """W5: each posted line gets ` · posted: <comment id>:<marker hash>` (an id STRING); every other byte of the file is unchanged."""
    f = _dec(repo, W5)
    before = f.read_text(encoding="utf-8")
    assert dpost(fake, "--apply") == 0
    (cid,) = fake.comments
    after = f.read_text(encoding="utf-8")
    st = _stamps(repo)
    assert set(st) == {"D1", "D4"} and all(type(s["comment_id"]) is str and s["comment_id"] == cid for s in st.values())
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert len(before.splitlines()) == len(after.splitlines()) and changed == [
        (OPEN, OPEN + " · posted: %s:%s" % (cid, decisions.line_hash("Use cursor paging"))),
        (OPEN2, OPEN2 + " · posted: %s:%s" % (cid, decisions.line_hash("Retry twice")))]
    assert "decisions file: stamped D1, D4 with posted: %s" % cid in capsys.readouterr().out
    assert decisions.unposted(decisions.parse_decisions(after)) == []


def test_stamp_posted_keeps_crlf_and_refuses_bad_ids_edits_and_inline_comments():
    md = _file("\n".join([OPEN, OPEN2])).replace("\n", "\r\n")
    p = decisions.parse_decisions(md)
    hashes = {d["id"]: decisions.marker_hash(d) for d in p["open"]}
    new = decisions.stamp_posted(md, hashes, "10001")
    assert new.count("\r\n") == md.count("\r\n") and new.replace(" · posted: 10001:" + hashes["D1"], "", 1).replace(
        " · posted: 10001:" + hashes["D4"], "", 1) == md
    again = decisions.stamp_posted(new, hashes, "10002")  # a re-post replaces the one marker the grammar allows
    assert "10001" not in again and again.count("posted: 10002:") == 2
    for bad in (10001, "01", "0", "1" * 13, "1\n", "١٢"):
        with pytest.raises(ValueError, match="^not a comment id string; nothing stamped$"):
            decisions.stamp_posted(md, hashes, bad)
    with pytest.raises(ValueError, match="changed since the plan"):
        decisions.stamp_posted(md.replace("Retry twice", "Retry thrice"), hashes, "10001")
    with pytest.raises(ValueError, match="changed since the plan"):
        decisions.stamp_posted(md.replace(OPEN2, ""), hashes, "10001")
    with pytest.raises(ValueError, match="inline"):
        decisions.stamp_posted(md.replace("paging →", "paging <!-- x --> →"), hashes, "10001")


def test_a_stamped_line_that_would_not_read_back_is_refused(monkeypatch):
    """Defence in depth behind the grammar: no real line was found that `render_line` cannot round-trip with its marker, so a faulty
    render is injected; the per-line read-back refuses it and nothing is returned."""
    md = _file(OPEN)
    (d,) = decisions.parse_decisions(md)["open"]
    real = decisions.render_line
    monkeypatch.setattr(decisions, "render_line", lambda x: real(x) + " · used: 2099-01-01")
    with pytest.raises(ValueError, match="would not read back"):
        decisions.stamp_posted(md, {"D1": decisions.marker_hash(d)}, "77")


def test_a_marker_lookalike_in_the_text_is_never_taken_for_the_stamp():
    line = "- [ ] D1: keep · posted: 5:abcdef12 here → _(open)_ · kind: decision · why: w"
    md = _file(line)
    (d,) = decisions.parse_decisions(md)["open"]
    assert d["posted"] is None and d["text"] == "keep · posted: 5:abcdef12 here"
    new = decisions.stamp_posted(md, {"D1": decisions.marker_hash(d)}, "77")
    (d2,) = decisions.parse_decisions(new)["open"]
    assert d2["text"] == d["text"] and d2["posted"] == {"comment_id": "77", "hash": decisions.marker_hash(d)}


def test_a_changed_decision_posts_a_new_comment_and_never_touches_the_old_one(repo, fake, capsys):
    f = _dec(repo, W5)
    assert dpost(fake, "--apply") == 0
    capsys.readouterr()
    (first,) = fake.comments
    old = json.dumps(fake.comments[first], sort_keys=True)
    f.write_text(f.read_text(encoding="utf-8").replace("Retry twice", "Retry three times"), encoding="utf-8")
    assert dpost(fake) == 0
    out = capsys.readouterr().out
    assert "- D4 (assumption): Retry three times" in out and "D1 (decision)" not in out  # only the stale line
    assert dpost(fake, "--apply") == 0
    assert len(_posts(fake)) == 2 and json.dumps(fake.comments[first], sort_keys=True) == old
    second = sorted(fake.comments)[-1]
    st = _stamps(repo)
    assert st["D1"]["comment_id"] == first and st["D4"] == {"comment_id": second, "hash": decisions.line_hash("Retry three times")}


def test_what_is_posted_follows_the_fork(repo):
    """`unposted`: open lines (a superseded open one too, the PO must see it first), then validated overrides; never another resolved line."""
    md = _file("\n".join([OPEN, VAL, REJ, "- [x] D5: skip l2 → validated · by: PO · note: ok · kind: override · why: outage",
                          "- [ ] D6: supersedes D3: audit to a file → _(open)_ · kind: decision · why: PO said no",
                          OPEN.replace("D1", "D7").replace("cursor", "offset") + " · posted: 9:" + decisions.line_hash("x")]))
    ids = [d["id"] for d in decisions.unposted(decisions.parse_decisions(md))]
    assert ids == ["D1", "D6", "D7", "D5"]
    sup = [d["id"] for d in decisions.supersede_unposted(decisions.parse_decisions(md))]
    assert sup == ["D6"]
    body = decisions.body(decisions.unposted(decisions.parse_decisions(md)))[0]
    assert "- D6 (decision): supersedes D3: audit to a file — why: PO said no" in body
    assert "- D5 (override): skip l2 — why: outage" in body and "D2" not in body and "D3 (" not in body


def test_supersedes_an_open_decision_works_once_it_is_posted(repo, fake, capsys):
    """The L1 limitation: `--add --supersedes` of an OPEN decision needs a matching `posted:` marker, which only --post writes."""
    _dec(repo, OPEN)
    assert _add("--why", "w", "--supersedes", "D1", text="use keyset paging") == 1
    assert "post D1 first" in capsys.readouterr().err
    assert dpost(fake, "--apply") == 0
    capsys.readouterr()
    assert _add("--why", "w", "--supersedes", "D1", text="use keyset paging") == 0
    assert json.loads(capsys.readouterr().out)["id"] == "D2"
    assert dpost(fake) == 0 and "- D2 (decision): supersedes D1: use keyset paging — why: w" in capsys.readouterr().out


def test_lines_changed_between_plan_and_apply_post_nothing(repo, fake, capsys):
    f = _dec(repo, W5)
    md = f.read_text(encoding="utf-8")
    c = fake.client()
    p = decisions.plan_post(c, config.Config.load(), KEY, decisions.parse_decisions(md))
    f.write_text(md.replace("Retry twice", "Retry never"), encoding="utf-8")
    res = decisions.apply_post(c, p, repo)
    assert res["ok"] is False and "changed since the plan" in res["lines"][-1] and _posts(fake) == []


def _near_cap(repo, slack):
    """The W5 file padded with a plain text line to `slack` bytes under 1 MiB."""
    f = _dec(repo, W5)
    base = f.read_bytes()
    f.write_bytes(base + b"x" * (_MIB - slack - len(base) - 1) + b"\n")
    assert len(f.read_bytes()) == _MIB - slack
    return f


def test_stamps_that_would_push_the_file_over_1_mib_are_refused_before_any_post(repo, fake, capsys):
    f = _near_cap(repo, 40)  # two stamps take more than 40 bytes
    before = f.read_bytes()
    assert dpost(fake, "--apply") == 1
    assert "failed: the file would exceed 1 MiB with the posted: stamps; nothing posted" in capsys.readouterr().out
    assert _posts(fake) == [] and f.read_bytes() == before
    with pytest.raises(ValueError, match="^the file would exceed 1 MiB with the posted: stamps; nothing stamped$"):
        decisions.stamp_posted(before.decode(), {d["id"]: decisions.marker_hash(d) for d in decisions.unposted(
            decisions.parse_decisions(before.decode()))}, "10001")
    f = _near_cap(repo, 200)  # where the stamps fit, they are written
    assert dpost(fake, "--apply") == 0 and set(_stamps(repo)) == {"D1", "D4"} and len(f.read_bytes()) <= _MIB


def test_a_read_back_that_fails_after_the_write_puts_the_old_text_back(repo, monkeypatch):
    f = _dec(repo, OPEN)
    before = f.read_bytes()
    real, calls = decisions._read, []

    def flaky(path):
        calls.append(path)
        if len(calls) == 3:  # the read-back after the write
            raise ValueError("the decisions file is larger than 1 MiB; nothing written")
        return real(path)
    monkeypatch.setattr(decisions, "_read", flaky)
    with pytest.raises(ValueError, match="did not read back as written"):
        decisions.apply_add(f, KEY, "decision", "another", "w")
    assert f.read_bytes() == before and list(f.parent.glob("*.tmp")) == []


def test_a_stamp_that_cannot_be_written_names_the_lines_by_hand(repo, fake, capsys, monkeypatch):
    f = _dec(repo, W5)
    md = f.read_text(encoding="utf-8")
    c = fake.client()
    p = decisions.plan_post(c, config.Config.load(), KEY, decisions.parse_decisions(md))
    victim = repo / "victim.md"
    victim.write_text(md, encoding="utf-8")
    real = decisions.stamp_posted
    calls = []

    def swap(m, h, cid):  # the pre-check passes; then (while the POST is sent) the file becomes a symlink
        calls.append(cid)
        out = real(m, h, cid)
        if len(calls) == 1:
            f.unlink()
            f.symlink_to(victim)
        return out
    monkeypatch.setattr(decisions, "stamp_posted", swap)
    res = decisions.apply_post(c, p, repo)
    (cid,) = fake.comments
    assert res["ok"] is False and "symbolic link" in res["lines"][-1] and "D1: · posted: %s:" % cid in res["lines"][-1]
    assert victim.read_text(encoding="utf-8") == md and len(_posts(fake)) == 1


# ---- the trust rules of the own-comment search (W4's helpers, reused) -------------------------------------------------------------

def test_a_forged_marker_by_another_account_never_suppresses_the_post(repo, fake, capsys):
    _dec(repo, W5)
    md, doc, h = _planned(repo)
    other = fake.add_comment(KEY, doc, author=fake.user(OTHER_ID))
    app = fake.add_comment(KEY, doc, author=dict(fake.user(fake.account_id), accountType="app"))
    assert dpost(fake) == 0
    out = capsys.readouterr().out
    assert "action: post ONE new comment" in out
    assert "note: 2 marker comment(s) by another account ignored" in out and other in out and app in out
    assert OTHER_ID not in out and fake.account_id not in out
    assert dpost(fake, "--apply") == 0 and len(_posts(fake)) == 1 and set(_stamps(repo)) == {"D1", "D4"}


def test_a_questions_comment_never_counts_as_the_decisions_comment(repo, fake, capsys):
    _dec(repo, W5)
    md, doc, h = _planned(repo)
    fake.add_comment(KEY, adf.md_to_adf(md.replace("[skj:decisions", "[skj:questions", 1)))
    assert dpost(fake) == 0 and "action: post ONE new comment" in capsys.readouterr().out


def test_an_edited_own_comment_no_longer_counts_unless_it_is_exactly_the_planned_text(repo, fake, capsys):
    f = _dec(repo, W5)
    before = f.read_text(encoding="utf-8")
    assert dpost(fake, "--apply") == 0
    (cid,) = fake.comments
    f.write_text(before, encoding="utf-8")  # unstamped again: the post decision rests on Jira
    md = adf.adf_to_md(fake.comments[cid]["body"])
    fake.edit_comment(cid, adf.md_to_adf(md.replace("Retry twice", "Retry never")), editor=fake._me())
    capsys.readouterr()
    assert dpost(fake) == 0
    out = capsys.readouterr().out
    assert "action: post ONE new comment" in out and "no longer matches the marker (edited) ignored: %s" % cid in out
    fake.edit_comment(cid, adf.md_to_adf(md), editor=fake.user(OTHER_ID))
    assert dpost(fake) == 0 and "already shows these decisions" in capsys.readouterr().out


def test_restricted_own_comments_do_not_count(repo, fake, capsys):
    _dec(repo, W5)
    md, doc, h = _planned(repo)
    vis = fake.add_comment(KEY, doc, visibility={"type": "role", "value": "Developers"})
    assert dpost(fake) == 0
    out = capsys.readouterr().out
    assert "action: post ONE new comment" in out and "visibility restriction ignored: %s" % vis in out


def _unreadable_own(fake, first=None):
    content = ([first] if first else []) + [{"type": "mediaSingle", "content": []}]
    return fake.add_comment(KEY, {"version": 1, "type": "doc", "content": content})


def _deep(n):
    v = []
    for _ in range(n):
        v = [v]
    return v


def _answering_inspection(fake, answer):
    def opener(method, url, headers, body, timeout):
        if method == "GET" and "/comment/" in url:
            fake.log.append({"method": method, "path": "inspection", "write": False})
            if answer == "timeout":
                raise TimeoutError("timed out")
            if callable(answer):
                st, h, raw = fake.opener(method, url, headers, body, timeout)
                return st, h, json.dumps(answer(json.loads(raw))).encode()
            return answer[0], {"Content-Type": "application/json"}, json.dumps(answer[1]).encode()
        return fake.opener(method, url, headers, body, timeout)
    return Client(fake.site, fake.email, fake.token, opener=opener, sleep=lambda s: None)


def test_an_unreadable_own_decisions_comment_refuses_the_post(repo, fake, capsys):
    _dec(repo, W5)
    md, doc, h = _planned(repo)
    odd = json.loads(json.dumps(doc))
    odd["content"].append({"type": "mediaSingle", "content": []})
    cid = fake.add_comment(KEY, odd)
    assert dpost(fake, "--apply") == 1
    assert capsys.readouterr().err == "skj: %s\n" % (decisions.E_UNREADABLE % cid)
    assert _posts(fake) == [] and fake.log[-1]["path"] == "/issue/%s/comment/%s" % (KEY, cid)


def test_an_unreadable_own_comment_without_the_marker_is_listed_and_ignored(repo, fake, capsys):
    _dec(repo, W5)
    plain = _unreadable_own(fake, {"type": "paragraph", "content": [{"type": "text", "text": "a screenshot:"}]})
    q = _unreadable_own(fake, {"type": "paragraph", "content": [{"type": "text", "text": "[skj:questions 0a1b2c3d]"}]})
    assert dpost(fake) == 0  # a questions marker is not a decisions marker
    out = capsys.readouterr().out
    assert "own comment(s) the kit cannot read (no decisions marker) ignored: %s, %s" % (plain, q) in out


@pytest.mark.parametrize("answer", [
    lambda d: {k: v for k, v in d.items() if k != "body"},
    lambda d: dict(d, body="[skj:decisions 0a1b2c3d]"),
    lambda d: dict(d, body=[d["body"]]),
    lambda d: dict(d, id="10999"),
    lambda d: dict(d, body={"version": 1, "type": "doc", "content": "x"}),
    lambda d: "not an object",
    lambda d: dict(d, body={"version": 1, "type": "doc"}),
    lambda d: dict(d, body={"version": 1, "type": "doc", "content": []}),
    lambda d: dict(d, body={"version": 1, "type": "doc", "content": _deep(150)}),
    (500, {"errorMessages": ["fake: down"]}), (404, {"errorMessages": []}), "timeout",
], ids=["no-body", "str-body", "list-body", "other-id", "odd-doc", "not-object", "no-content", "empty-content", "too-deep", "5xx", "404", "timeout"])
def test_an_inspection_that_is_not_definitive_refuses_the_post(repo, fake, capsys, answer):
    _dec(repo, W5)
    cid = _unreadable_own(fake, {"type": "paragraph", "content": [{"type": "text", "text": "a screenshot:"}]})
    c = _answering_inspection(fake, answer)
    assert dpost(fake, "--apply", client=lambda: c) == 1
    assert capsys.readouterr().err == "skj: %s\n" % (decisions.E_UNREADABLE % cid) and _posts(fake) == []


def test_inspections_stop_at_the_first_refusal_and_are_capped(repo, fake, capsys):
    _dec(repo, W5)
    first = _unreadable_own(fake)
    for _ in range(3):
        _unreadable_own(fake)
    assert dpost(fake, "--apply") == 1 and (decisions.E_UNREADABLE % first) in capsys.readouterr().err
    assert sum("/comment/" in e["path"] for e in fake.log) == 1
    f2 = FakeJira()
    f2.add_issue("A fictional story")
    for _ in range(questions.MAX_INSPECT + 1):
        _unreadable_own(f2, {"type": "paragraph", "content": [{"type": "text", "text": "fine"}]})
    assert dpost(f2, "--apply") == 1 and capsys.readouterr().err == "skj: %s\n" % decisions.E_TOO_MANY
    assert not any("/comment/" in e["path"] for e in f2.log) and _posts(f2) == []


def test_a_truncated_or_inconsistent_comment_list_refuses_to_post(repo, fake, capsys, monkeypatch):
    _dec(repo, W5)
    md, doc, h = _planned(repo)
    fake.add_comment(KEY, adf.md_to_adf("first"))
    fake.add_comment(KEY, doc)
    real = fake._comment_page

    def short_total(issue, start, size):
        page = real(issue, start, size)
        page["total"] = 1
        return page
    monkeypatch.setattr(fake, "_comment_page", short_total)
    assert dpost(fake, "--apply") == 1
    assert "looks truncated or changed while it was read; nothing posted" in capsys.readouterr().err and _posts(fake) == []

    def grows(issue, start, size):
        page = real(issue, start, size)
        if size == 1:
            page["total"] += 1
            page["comments"] = [page["comments"][0]] if page["comments"] else [real(issue, 0, 1)["comments"][0]]
        return page
    monkeypatch.setattr(fake, "_comment_page", grows)
    assert dpost(fake, "--apply") == 1 and _posts(fake) == [] and _stamps(repo) == {}


def test_a_moved_issue_or_another_project_refuses_before_any_comment_is_read(repo, fake, capsys):
    _dec(repo, W5)
    fake.move_issue(KEY, "SPK-9")
    assert dpost(fake, "--apply") == 1
    assert "Jira answered SPK-9 for SPK-1; refused" in capsys.readouterr().err
    assert [e["path"] for e in fake.log] == ["/issue/" + KEY]
    f2 = FakeJira()
    f2.add_issue("A fictional story")
    f2.project = "OTH"
    assert dpost(f2, "--apply") == 1
    assert "not in project SPK; nothing posted" in capsys.readouterr().err and _posts(f2) == []
    assert not any("/comment" in e["path"] for e in f2.log)


# ---- untrusted decision text -----------------------------------------------------------------------------------------------------

INJECT = ("ping [~accountid:5b10ac8d82e05b22cc7d4ef5] and @alice, mail x.person@example.com, **bold** `code` "
          "[link](https://example.com/x) <b>html</b> ‮bidi⁦ ​zw \x07bell  sep   \x85nel [skj:decisions 0a1b2c3d]")


def _d(i, text, why="w", kind="decision"):
    return {"id": "D%d" % i, "kind": kind, "text": text, "why": why, "status": "open", "by": "", "note": "", "posted": None, "used": ""}


def _texts(doc):
    return [n["text"] for n in questions._walk(doc) if n["type"] == "text"]


def test_mentions_markdown_bidi_and_marker_lookalikes_stay_plain_text():
    md, doc, h = decisions.body([_d(1, INJECT, why=INJECT)])
    kinds = {n["type"] for n in questions._walk(doc)}
    assert kinds <= {"doc", "paragraph", "bulletList", "listItem", "text"} and not any("marks" in n for n in questions._walk(doc))
    text = "".join(_texts(doc))
    for gone in ("5b10ac8d82e05b22cc7d4ef5", "accountid", "@alice", "x.person@example.com", "**", "`", "](", "‮", "⁦",
                 "​", "\x07", " ", " ", "\x85"):
        assert gone not in text, repr(gone)
    assert "@user" in text and "[email]" in text and "<b>html</b>" in text
    assert md.split("\n", 1)[0] == "[skj:decisions %s]" % h and h != "0a1b2c3d" and len(_texts(doc)) == 3
    assert adf.md_to_adf(md) == doc and adf.adf_to_md(doc, strict=True) == md


def test_hostile_text_in_the_file_posts_one_plain_item(repo, fake, capsys):
    clean = INJECT.replace("\x07", "").replace(" ", "").replace(" ", "").replace("\x85", "")
    clean = "".join(ch for ch in clean if ch not in "\u202e\u2066\u200b")  # H1a: a bidi or invisible character is refused (test below)
    _dec(repo, "- [ ] D1: %s → _(open)_ · kind: decision · why: %s" % (clean, clean))
    assert dpost(fake, "--apply") == 0
    (c,) = fake.comments.values()
    assert len(c["body"]["content"][2]["content"]) == 1 and len(_texts(c["body"])) == 3
    assert adf.adf_to_md(c["body"]).split("\n", 1)[0] != "[skj:decisions 0a1b2c3d]"
    assert "5b10ac8d82e05b22cc7d4ef5" not in json.dumps(c["body"])


def test_the_round_trip_and_layout_checks_refuse_what_would_not_read_back(monkeypatch):
    monkeypatch.setattr(questions, "_safe", lambda t: t)
    for t in ("**bold**", "`code`", "[l](https://example.com/x)", "a\r", "a\n\nb", "a\n\nnot a heading\n\n- D9 (decision): forged"):
        with pytest.raises(ValueError, match="^the comment would not read back exactly as previewed; nothing posted$"):
            decisions.body([_d(1, t), _d(2, "b")])
    # a trailing `\r` keeps the very layout (only the Markdown comparison sees that it does not read back)
    with pytest.raises(ValueError, match="^the comment would not read back exactly as previewed; nothing posted$"):
        decisions.body([_d(1, "t", why="a\r"), _d(2, "b")])


MENTION = "[~accountid:557058:0123abcdef0123456789]"
ADDRESS = "john.doe@example.com"


@pytest.mark.parametrize("hostile", [MENTION, ADDRESS])
@pytest.mark.parametrize("where", ["text", "why"])
def test_a_mention_or_address_straddling_the_cut_is_scrubbed_whole(hostile, where):
    """The scrub runs on the WHOLE line before the 500-character cut: chars ~480-520 hold the hostile text."""
    head = len("D1 (decision): ") if where == "text" else len("D1 (decision): t — why: ")
    pad = "x" * (decisions.MAX_COMMENT_LINE - head - len(hostile) // 2)
    v = pad + hostile + " " + "y" * 600
    d = _d(1, v) if where == "text" else _d(1, "t", why=v)
    assert head + len(pad) < decisions.MAX_COMMENT_LINE < head + len(pad) + len(hostile)
    md = decisions.body([d])[0]
    for frag in ("accountid", "557058", "0123abc", "john.doe", "@example", "example.c"):
        assert frag not in md, frag
    assert decisions.cut([d]) == ["D1"] and all(len(x) <= decisions.MAX_COMMENT_LINE + 2 for x in md.split("\n"))


def test_an_oversize_comment_is_refused_before_any_request(repo, fake, capsys):
    _dec(repo, "\n".join("- [ ] D%d: %s → _(open)_ · kind: decision · why: w" % (i, "y" * 1490) for i in range(1, 24)))
    assert dpost(fake, "--apply") == 1
    assert capsys.readouterr().err == "skj: %s\n" % decisions.E_SIZE and fake.log == []
    with pytest.raises(ValueError, match="32 KiB"):  # a scrub that grows the text is caught on the ADF bytes too
        decisions.body([_d(i, "*" * 480) for i in range(1, 30)])


def test_an_unparsed_file_refuses_the_post_before_any_request(repo, fake, capsys):
    _dec(repo, OPEN + "\n```\n- [ ] D9: hidden → _(open)_ · kind: decision · why: w")
    assert dpost(fake, "--apply") == 1 and decisions.E_UNPARSED in capsys.readouterr().err and fake.log == []
    _dec(repo, OPEN + "\n- [ ] D9 broken line")
    assert dpost(fake, "--apply") == 1 and fake.log == []


# ---- the CLI: --if-any, flags, comment mode, X2 wiring ----------------------------------------------------------------------------

def test_if_any_a_missing_file_or_nothing_to_post_is_a_no_op_without_a_request(repo, fake, capsys):
    assert dpost(fake, "--apply") == 1 and "no decisions file" in capsys.readouterr().err
    assert dpost(fake, "--if-any", "--apply", client=lambda: 1 / 0) == 0
    assert "action: none (no decisions file; nothing to post)" in capsys.readouterr().out
    assert dpost(fake, "--if-any", "--plan-out", "p.md") == 0 and (repo / "p.md").exists()
    assert not (repo / "dod").exists() and fake.log == []
    _dec(repo, "\n".join([VAL, REJ]))

    def boom():
        raise AssertionError("a client was built with nothing to post")
    assert dpost(fake, "--if-any", "--apply", client=boom) == 0 and dpost(fake, "--apply", client=boom) == 0
    assert "no unposted decisions" in capsys.readouterr().out and fake.log == []


def test_post_flag_combinations_are_usage_errors(repo, fake, capsys):
    _dec(repo, W5)
    for argv in (["--apply"], ["--plan-out", "q.md"], ["--if-any"], ["--post", "--add", "x", "--kind", "decision"],
                 ["--post", "--resolve", "D1", "--validated"]):
        assert main(["decisions", KEY, *argv], client_factory=fake.client) == 2, argv
        assert "--post" in capsys.readouterr().err
    assert run(KEY, "--post", "--sync") == 2 and "--sync" in capsys.readouterr().err  # A2: --sync is its own verb now
    assert fake.log == [] and not (repo / "q.md").exists()


def test_the_post_is_gated_by_expect_plan(repo, fake, capsys):
    f = _dec(repo, W5)
    assert dpost(fake, "--plan-out", "p.md") == 0
    f.write_text(f.read_text(encoding="utf-8").replace("Retry twice", "Retry, really"), encoding="utf-8")
    assert dpost(fake, "--apply", "--expect-plan", "p.md") == 2 and _posts(fake) == [] and (repo / "p.md.new").exists()
    assert dpost(fake, "--plan-out", "p.md") == 0 and dpost(fake, "--apply", "--expect-plan", "p.md") == 0
    assert len(_posts(fake)) == 1


def _approve_plan(fake, repo, gate="approve-decisions", who=APPROVER, verb="/approve"):
    """The approver's `/approve approve-decisions <hash>` of the plan the dry run shows now (`--plan-out` writes the canonical bytes)."""
    from skj import adf, approvals
    assert dpost(fake, "--plan-out", "p.md") == 0
    h = approvals.gate_hash(gate, KEY, (repo / "p.md").read_bytes())
    fake.add_comment(KEY, adf.md_to_adf("%s %s %s" % (verb, gate, h)), author=fake.user(who))
    return h


def test_w5_ac4_comment_mode_needs_the_approvers_approve_of_this_plan(repo, fake, capsys):
    """W5-AC4, the real check (A4 replaces W5's placeholder refusal): with `approval: comment` on the TARGET branch, --apply posts only
    when a trusted approver commented `/approve approve-decisions <hash of this exact plan>`; the dry run still shows the plan."""
    from skj import approvals
    _target(repo, {**BASE, "approvers": [APPROVER], "approval": "comment"}, head_cfg=BASE)  # the PR cannot drop it
    f = _dec(repo, W5)
    before = f.read_bytes()
    assert dpost(fake) == 0 and "Comment body" in capsys.readouterr().out
    assert dpost(fake, "--apply") == 2  # no approval: exit 2, nothing posted
    cap = capsys.readouterr()
    assert cap.err.splitlines()[-1].startswith("skj: comment approval: no valid approval for this plan on story %s." % KEY)
    assert APPROVER not in cap.out + cap.err and _posts(fake) == [] and f.read_bytes() == before
    _approve_plan(fake, repo, who=ME_ID)  # the agent's own approval never counts
    assert dpost(fake, "--apply") == 2 and _posts(fake) == []
    _approve_plan(fake, repo)
    f.write_text(f.read_text(encoding="utf-8").replace("Retry twice", "Retry thrice"), encoding="utf-8")  # the plan changed since
    assert dpost(fake, "--apply") == 2 and _posts(fake) == []
    _approve_plan(fake, repo)
    assert dpost(fake, "--apply", "--expect-plan", "p.md") == 0
    assert [(e["method"], e["path"]) for e in _posts(fake)] == [("POST", "/issue/%s/comment" % KEY)]


def test_w5_ac4_a_newer_reject_of_the_plan_refuses_the_post(repo, fake, capsys):
    _target(repo, {**BASE, "approvers": [APPROVER], "approval": "comment"}, head_cfg=BASE)
    _dec(repo, W5)
    _approve_plan(fake, repo)
    _approve_plan(fake, repo, verb="/reject")
    assert dpost(fake, "--apply") == 2 and _posts(fake) == []


def test_w5_ac4_an_unreadable_target_config_fails_closed(repo, fake, capsys):
    _target(repo, "{ not json", head_cfg=BASE)
    _dec(repo, W5)
    assert dpost(fake, "--apply") == 1 and _posts(fake) == [] and _stamps(repo) == {}
    _target(repo, {**BASE, "approval": "terminal"}, head_cfg={**BASE, "approval": "comment"})  # the working tree's word is not trusted
    assert dpost(fake, "--apply") == 0 and len(_posts(fake)) == 1


def test_decisions_post_is_wired_through_the_framework():
    import skj.__main__ as m
    ps = [x for x in m._all_parsers(m.build_parser()) if x.get_default("write_verb") == "decisions --post"]
    assert len(ps) == 1 and {"--apply", "--plan-out", "--expect-plan", "--post", "--if-any"} <= {
        s for a in ps[0]._actions for s in a.option_strings}


def test_there_is_no_comment_edit_or_delete_and_no_visibility_in_0_1():
    src = open(decisions.__file__, encoding="utf-8").read()
    assert '"PUT"' not in src and '"DELETE"' not in src and "properties" not in src.replace("no properties", "")
    assert "/comment/" not in src  # the one inspection GET is W4's (questions._marked), reused
    assert src.count('c.request("POST"') == 1 and 'body={"body": p["doc"]}' in src
    w5 = src.split("# ---- `skj decisions <ref> --post` (W5)", 1)[1]
    assert not re.search(r"\bint\(", w5)  # comment ids stay strings


# ---- linear time over a PR-controlled decisions file ---------------------------------------------------------------------------------

def _w5_pipeline(md):
    """Everything the post path does to the file text before and after the request, as (name, zero-argument call) in order, each timed
    on its own (H1b: the whole chain in one bound left 3x slack only); each step runs even when another refuses."""
    st = {}

    def parse():
        st["p"] = decisions.parse_decisions(md)
        st["todo"] = decisions.unposted(st["p"])
        decisions.supersede_unposted(st["p"])
    return [("parse", parse), ("body", lambda: decisions.body(st["todo"])), ("cut", lambda: decisions.cut(st["todo"])),
            ("stamp", lambda: decisions.stamp_posted(md, {d["id"]: decisions.marker_hash(d) for d in st["todo"]}, "10001"))]


def _w5_shapes():
    pad = lambda head, n=1990: head + " " * (n - len(head))  # noqa: E731
    line = "- [ ] D%d: %s → _(open)_ · kind: decision · why: w"
    return {
        "one-mib-line": _file("- [ ] D1: " + "[~" * (_MIB // 2 - 600)),
        "500x1990-blank": _file("\n".join(pad("- [ ] D%d: a" % i, 1950) + "b → _(open)_ · kind: decision · why: w" for i in range(1, 501))),
        "500x1990-mentions": _file("\n".join(line % (i, "[~" * 900) for i in range(1, 501))),
        "500x1990-posted-lookalikes": _file("\n".join(line % (i, "· posted: " * 180) for i in range(1, 501))),
        "16000-short": _file("\n".join("- [ ] D%d: [~@a*`]( → _(open)_ · kind: decision · why: w" % i for i in range(1, 16001))),
        "16000-at": _file("\n".join("- [ ] D%d: a@b.c@d.e@ → _(open)_ · kind: decision · why: w" % i for i in range(1, 16001))),
    }


@_needs_alarm
@pytest.mark.parametrize("shape", sorted(_w5_shapes()))
def test_the_post_path_is_linear_on_adversarial_files(shape):
    md = _w5_shapes()[shape]
    assert len(md.encode("utf-8")) <= _MIB
    assert not {n: dt for n, call in _w5_pipeline(md) if (dt := _timed(call)) >= _BOUND}


@_needs_alarm
def test_the_body_escape_is_linear_at_the_cap():
    ds = [_d(i, "[~" * 200 + "@x.y@" * 10, why="[~" * 30) for i in range(1, 50)]
    assert _timed(lambda: decisions.body(ds)) < _BOUND


# ==== A2: `decisions --sync` (EXPERIMENTAL comment approval, tamper-evident only; against the fake Jira only) ==================
# The verdict comes from the newest VALID approver comment (`approvals.guard` + the newest-posting rule) and is pinned as
# ` · via: <comment id>:<hash8>`. A local write of the decisions file only: GETs, never a Jira write.
from fakejira import ME_ID
from skj import approvals

COMMENT_MODE = {**BASE, "approvers": [APPROVER], "approval": "comment"}


@pytest.fixture
def cm(repo):
    """The repo with `approval: comment` and one approver on the TARGET branch (origin/HEAD)."""
    _target(repo, COMMENT_MODE)
    return repo


def _kit_post(fake, md, ids=None):
    """The kit (token user) posts the unposted lines of `md` (or the decisions `ids`) as W5 does; (`md` stamped, comment id)."""
    parsed = decisions.parse_decisions(md)
    todo = decisions.unposted(parsed) if ids is None else [d for d in _all(parsed) if d["id"] in ids]
    cid = fake.add_comment(KEY, decisions.body(todo)[1])
    return decisions.stamp_posted(md, {d["id"]: decisions.marker_hash(d) for d in todo}, cid), cid


def _say(fake, text, who=APPROVER, **kw):
    return fake.add_comment(KEY, adf.md_to_adf(text), author=fake.user(who), **kw)


def _posted_file(repo, fake, body=W5):
    f = _dec(repo, body)
    md, post = _kit_post(fake, f.read_text(encoding="utf-8"))
    f.write_text(md, encoding="utf-8")
    return f, post


def dsync(fake, *args, client=None):
    return main(["decisions", KEY, "--sync", *args], client_factory=client or fake.client)


def _out(capsys):
    cap = capsys.readouterr()
    return json.loads(cap.out), cap.err


def _pin(fake, cid):
    return "%s:%s" % (cid, adf.digest(fake.comments[cid]["body"])[:8])


def _d1(f):
    return next(x for x in f.read_text(encoding="utf-8").splitlines() if x.startswith(("- [ ] D1: ", "- [x] D1: ")))


def _no_secrets(text, fake):
    for s in (fake.account_id, APPROVER, OTHER_ID, fake.email, fake.token, fake.account_id[:8], APPROVER[:8], "other.person@example.com"):
        assert s not in text


def test_sync_records_pin(cm, fake, capsys):
    """A2-AC2: the verdict lands with ` · via: <comment id>:<hash8>` (hash8 = first 8 hex of `adf.digest` of the approval body);
    every other byte is unchanged; only GETs were sent; the output holds ids and codes, never an accountId."""
    f, post = _posted_file(cm, fake)
    before = f.read_text(encoding="utf-8")
    vote = _say(fake, "/approve D1: fine by me")
    assert dsync(fake) == 0
    out, err = _out(capsys)
    pin = _pin(fake, vote)
    assert approvals.PIN_RE.match(pin) and pin.split(":")[1] == adf.digest(fake.comments[vote]["body"])[:8]
    h = decisions.marker_hash(decisions.parse_decisions(before)["open"][0])
    assert _d1(f) == ("- [x] D1: Use cursor paging → validated · by: approver · note: fine by me · via: %s · why: the story names no "
                      "page size · posted: %s:%s" % (pin, post, h))
    assert [x for x in f.read_text(encoding="utf-8").splitlines() if " D1: " not in x] == [x for x in before.splitlines() if " D1: " not in x]
    assert out["applied"] == ["D1"] and out["status"] == {"D1": "validated"} and out["skipped"] == {
        "D2": "not-posted", "D3": "not-posted", "D4": "no-verdict"}
    assert decisions.parse_decisions(f.read_text(encoding="utf-8"))["validated"][0]["via"] == pin
    assert {e["method"] for e in fake.log} == {"GET"} and fake.writes() == []
    _no_secrets(json.dumps(out) + err, fake)
    assert dsync(fake) == 0 and _out(capsys)[0]["skipped"]["D1"] == "up-to-date"  # the same comment again: nothing changes
    assert f.read_text(encoding="utf-8").count(pin) == 1


def test_sync_ignores_edited(cm, fake, capsys):
    """A guard failure is skipped AND reported: an edited approval (updated moved, or another updateAuthor) never counts."""
    f, _ = _posted_file(cm, fake)
    before = f.read_bytes()
    vote = _say(fake, "/approve D1")
    fake.edit_comment(vote, adf.md_to_adf("/approve D1"), editor=fake.user(APPROVER))  # the same text, but edited
    other = _say(fake, "/approve D4")
    fake.comments[other]["updateAuthor"] = fake.user(OTHER_ID)  # same stamps, another editor
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["applied"] == [] and out["skipped"]["D1"] == "no-verdict" and out["skipped"]["D4"] == "no-verdict"
    assert out["skipped_comments"] == {vote: "edited", other: "edited"} and f.read_bytes() == before
    # a later EDITED reject is never obeyed, and (Fix round 1) it does not let the earlier approve through either: newer-tampered
    _say(fake, "/approve D1: yes")
    late = _say(fake, "/reject D1: no")
    fake.edit_comment(late, adf.md_to_adf("/reject D1: no"), editor=fake.user(APPROVER))
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["skipped"]["D1"] == "newer-tampered" and out["skipped_comments"][late] == "edited" and f.read_bytes() == before


def test_sync_noop_in_terminal_mode(repo, fake, capsys):
    """A2-AC3: outside comment mode `--sync --if-comment-mode` is a no-op and `--sync` alone refuses; neither builds a client. The mode
    comes from the TARGET branch only: a working tree that says comment changes nothing."""
    def boom():
        raise AssertionError("a client was built outside comment mode")
    f = _dec(repo, W5)
    before = f.read_bytes()
    for target, head in ((None, None), ({**BASE, "approvers": [APPROVER], "approval": "terminal"}, COMMENT_MODE),
                         ({**BASE, "approvers": [APPROVER]}, COMMENT_MODE)):
        if target is not None:
            _target(repo, target, head_cfg=head)
        assert dsync(fake, "--if-comment-mode", client=boom) == 0
        out, err = _out(capsys)
        assert out == {"key": KEY, "synced": False, "note": approvals.E_NOT_COMMENT_MODE} and err == ""
        assert dsync(fake, client=boom) == 1
        assert capsys.readouterr().err == "skj: %s\n" % approvals.E_SYNC_TERMINAL
    assert f.read_bytes() == before and fake.log == []


def test_sync_refuses_comment_mode_without_approvers_and_needs_no_file_with_if_comment_mode(repo, fake, capsys):
    def boom():
        raise AssertionError("a client was built")
    _target(repo, {**BASE, "approval": "comment"}, head_cfg=COMMENT_MODE)  # the working tree's approvers are not believed
    assert dsync(fake, client=boom) == 1 and capsys.readouterr().err == "skj: %s\n" % approvals.E_SYNC_NO_APPROVERS
    assert dsync(fake, "--if-comment-mode", client=boom) == 1
    _target(repo, COMMENT_MODE)
    assert dsync(fake, "--if-comment-mode", client=boom) == 0  # no decisions file: nothing to sync
    assert _out(capsys)[0] == {"key": KEY, "synced": False, "note": approvals.E_NO_DECISIONS_FILE}
    assert dsync(fake, client=boom) == 1 and "no decisions file" in capsys.readouterr().err


def test_sync_flag_combinations_are_usage_errors(cm, fake, capsys):
    _dec(cm, W5)
    for argv in (["--sync", "--post"], ["--sync", "--add", "x", "--kind", "decision"], ["--sync", "--resolve", "D1", "--validated"],
                 ["--sync", "--apply"], ["--sync", "--plan-out", "p.md"], ["--if-comment-mode"], ["--post", "--if-comment-mode"]):
        assert main(["decisions", KEY, *argv], client_factory=fake.client) == 2, argv
        assert "--sync" in capsys.readouterr().err
    assert fake.log == []


def test_sync_skips_a_restricted_approval_and_reports_it(cm, fake, capsys):
    """A2-AC4: an approval with a `visibility` is skipped and reported with its reason code."""
    f, _ = _posted_file(cm, fake)
    before = f.read_bytes()
    vote = _say(fake, "/approve D1", visibility={"type": "role", "value": "Administrators"})
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["applied"] == [] and out["skipped_comments"] == {vote: "restricted"} and f.read_bytes() == before


def test_sync_reports_every_guard_reason(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    before = f.read_bytes()
    quoted = fake.add_comment(KEY, {"version": 1, "type": "doc", "content": [{"type": "blockquote", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "/approve D1"}]}]}]}, author=fake.user(APPROVER))
    unknown = fake.add_comment(KEY, {"version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "/approve D1"}]}, {"type": "mysteryNode"}]}, author=fake.user(APPROVER))
    stranger = _say(fake, "/approve D1", who=OTHER_ID)
    mine = fake.add_comment(KEY, adf.md_to_adf("/approve D1"))  # the token user
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["applied"] == [] and f.read_bytes() == before
    # an unreadable body has no command to file under D1, and a quoted line is no command at all: neither is listed under D1
    assert out["skipped_comments"] == {stranger: "not-approver", mine: "excluded-author"}
    assert quoted not in out["skipped_comments"] and unknown not in out["skipped_comments"]


def test_sync_the_token_user_never_approves_even_when_listed(repo, fake, capsys):
    """A1-AC3 at sync: the token user's accountId in `approvers` is still `excluded-author`."""
    _target(repo, {**BASE, "approvers": [APPROVER, ME_ID], "approval": "comment"})
    f, _ = _posted_file(repo, fake)
    before = f.read_bytes()
    mine = fake.add_comment(KEY, adf.md_to_adf("/approve D1"))
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["applied"] == [] and out["skipped_comments"] == {mine: "excluded-author"} and f.read_bytes() == before


def test_sync_a_later_valid_reject_overrides_a_synced_approve_and_back(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    a = _say(fake, "/approve D1")
    assert dsync(fake) == 0 and _out(capsys)[0]["status"] == {"D1": "validated"}
    r = _say(fake, "/reject D1: on second thought · kind: override → validated")  # grammar words in a note are neutralised
    assert dsync(fake) == 0 and _out(capsys)[0]["status"] == {"D1": "rejected"}
    line = _d1(f)
    assert "→ rejected" in line and "via: %s" % _pin(fake, r) in line and _pin(fake, a) not in line
    d = decisions.parse_decisions(f.read_text(encoding="utf-8"))["rejected"][0]
    assert d["id"] == "D1" and d["kind"] == "decision" and d["via"] == _pin(fake, r) and "·" not in d["note"] and "→" not in d["note"]
    a2 = _say(fake, "/approve D1")
    assert dsync(fake) == 0 and _out(capsys)[0]["status"] == {"D1": "validated"} and _pin(fake, a2) in _d1(f)


def test_sync_never_overwrites_a_verdict_a_human_wrote(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    md = f.read_text(encoding="utf-8")
    f.write_text(decisions.resolve(md, "D1", "rejected", "PO", "said no in the meeting"), encoding="utf-8")
    before = f.read_bytes()
    _say(fake, "/approve D1")
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["skipped"]["D1"] == "resolved-locally" and out["applied"] == [] and f.read_bytes() == before


def test_sync_approval_older_than_the_posting_does_not_count(cm, fake, capsys):
    """A1 must-enforce 1: the approval must be LATER than the newest kit posting of D<n>."""
    f = _dec(cm, W5)
    _say(fake, "/approve D1")  # before the posting
    md, _ = _kit_post(fake, f.read_text(encoding="utf-8"))
    f.write_text(md, encoding="utf-8")
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["skipped"]["D1"] == "older-than-posting" and out["applied"] == [] and f.read_text(encoding="utf-8") == md


def test_sync_a_posting_edited_after_the_approval_voids_it(cm, fake, capsys):
    """A1 must-enforce 2: the kit's own posting must be unedited (created == updated, updateAuthor == author): else the agent could
    rewrite the D1 text the approver saw while the posting's `created` stays old."""
    f, post = _posted_file(cm, fake)
    md = f.read_text(encoding="utf-8")
    _say(fake, "/approve D1")
    fake.edit_comment(post, fake.comments[post]["body"], editor=fake._me())  # an own edit, even to the same body
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["skipped"]["D1"] == "posting-edited" and f.read_text(encoding="utf-8") == md
    fake.comments[post]["updated"] = fake.comments[post]["created"]
    fake.comments[post]["updateAuthor"] = fake.user(OTHER_ID)  # same stamps, another editor
    assert dsync(fake) == 0 and _out(capsys)[0]["skipped"]["D1"] == "posting-edited"


def test_sync_the_posted_line_must_equal_the_ledger_line(cm, fake, capsys):
    """A1 must-enforce 3: the D1 line the posting shows must be the line the file holds now."""
    f, post = _posted_file(cm, fake)
    md = f.read_text(encoding="utf-8")
    _say(fake, "/approve D1")
    edited = md.replace("Use cursor paging", "Use offset paging")
    f.write_text(edited, encoding="utf-8")  # the marker hash no longer matches
    assert dsync(fake) == 0 and _out(capsys)[0]["skipped"]["D1"] == "edited"
    d = decisions.parse_decisions(edited)["open"][0]
    forged = edited.replace(":" + d["posted"]["hash"], ":" + decisions.marker_hash(d))  # the agent recomputes the marker
    f.write_text(forged, encoding="utf-8")
    assert dsync(fake) == 0 and _out(capsys)[0]["skipped"]["D1"] == "posting-mismatch" and f.read_text(encoding="utf-8") == forged


def test_sync_replay_of_an_old_approval_after_a_reworded_repost_does_not_count(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    _say(fake, "/approve D1")
    md = f.read_text(encoding="utf-8").replace("Use cursor paging", "Use offset paging")
    md, _ = _kit_post(fake, md, ids={"D1"})  # the reworded line is posted AFTER the approval
    f.write_text(md, encoding="utf-8")
    assert dsync(fake) == 0 and _out(capsys)[0]["skipped"]["D1"] == "older-than-posting" and f.read_text(encoding="utf-8") == md
    _say(fake, "/approve D1")
    assert dsync(fake) == 0 and _out(capsys)[0]["status"] == {"D1": "validated"}


def test_sync_a_marker_comment_by_another_account_is_ignored(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    _say(fake, "/approve D1")
    forged = fake.add_comment(KEY, decisions.body([_d(1, "Something else entirely")])[1], author=fake.user(OTHER_ID))
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["status"] == {"D1": "validated"} and out["foreign_postings"] == [forged]


def test_sync_the_timestamp_formats_of_one_instant_compare_as_instants(cm, fake, capsys):
    f, post = _posted_file(cm, fake)
    md = f.read_text(encoding="utf-8")
    vote = _say(fake, "/approve D1")
    cases = [("2026-01-01T10:00:00.000+0000", "2026-01-01T09:30:00.000-0100", "validated"),  # 10:30Z is later; raw strings disagree
             ("2026-01-01T09:00:00.000-0200", "2026-01-01T10:00:00.000+0000", "older-than-posting"),  # 11:00Z vs 10:00Z
             ("2026-01-01T10:00:00.000+0000", "2026-01-01T12:00:00.000+0200", "validated"),  # the same instant: the higher id wins
             ("2026-01-01T10:00:00Z", "2026-01-01T10:00:00.000+00:00", "validated"),
             ("2026-01-01T10:00:00.000+0000", "2026-01-01T10:00:00.000+02:00", "older-than-posting"),
             ("2026-01-01T10:00:00.000+0000", "2026-13-01T10:00:00.000+0000", "no-verdict"),  # an impossible date: never counted
             ("2026-02-30T10:00:00.000+0000", "2026-01-01T10:00:00.000+0000", "posting-missing")]
    for p_at, v_at, want in cases:
        f.write_text(md, encoding="utf-8")
        for cid, at in ((post, p_at), (vote, v_at)):
            fake.comments[cid]["created"] = fake.comments[cid]["updated"] = at
        code = dsync(fake)
        out, _ = _out(capsys)
        assert code == 0 and (out["status"].get("D1") or out["skipped"]["D1"]) == want, (p_at, v_at)
        if want == "no-verdict":
            assert out["skipped_comments"] == {vote: "date"} and f.read_text(encoding="utf-8") == md


def test_sync_a_tie_on_the_same_millisecond_is_broken_by_the_comment_id(cm, fake, capsys):
    f, post = _posted_file(cm, fake)
    md = f.read_text(encoding="utf-8")
    vote = _say(fake, "/approve D1")
    assert int(vote) > int(post)
    fake.comments[vote]["created"] = fake.comments[vote]["updated"] = fake.comments[post]["created"]
    assert dsync(fake) == 0 and _out(capsys)[0]["status"] == {"D1": "validated"}  # the higher id is the later comment
    # the approval written FIRST (the lower id) at the same instant as the posting is older than it
    f = _dec(cm, W5)
    early = _say(fake, "/approve D4")
    md, post2 = _kit_post(fake, f.read_text(encoding="utf-8"), ids={"D4"})
    f.write_text(md, encoding="utf-8")
    fake.comments[early]["created"] = fake.comments[early]["updated"] = fake.comments[post2]["created"]
    assert dsync(fake) == 0 and _out(capsys)[0]["skipped"]["D4"] == "older-than-posting" and f.read_text(encoding="utf-8") == md


def test_sync_a_deleted_approval_leaves_the_verdict_and_lists_it(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    vote = _say(fake, "/approve D1")
    assert dsync(fake) == 0 and _out(capsys)[0]["applied"] == ["D1"]
    synced = f.read_bytes()
    del fake.comments[vote]
    fake.issues[next(iter(fake.issues))]["comments"].remove(vote)
    assert dsync(fake) == 0
    out, err = _out(capsys)
    assert out["no_longer_valid"] == ["D1"] and out["skipped"]["D1"] == "no-verdict" and f.read_bytes() == synced
    assert err == "approval-no-longer-valid: D1\n"


def test_sync_refuses_a_truncated_or_moved_story_and_writes_nothing(cm, fake, capsys, monkeypatch):
    """A1 must-enforce 5: an incomplete comment list (a total that is not what was read, a comment past the end) refuses."""
    f, _ = _posted_file(cm, fake)
    _say(fake, "/approve D1")
    before = f.read_bytes()
    real = fake._comment_page

    def short_total(issue, start, size):
        page = real(issue, start, size)
        page["total"] = 1
        return page
    monkeypatch.setattr(fake, "_comment_page", short_total)
    assert dsync(fake) == 1 and "truncated" in capsys.readouterr().err and f.read_bytes() == before

    def grows(issue, start, size):
        page = real(issue, start, size)
        if size == 1:
            page["total"] += 1
            page["comments"] = [real(issue, 0, 1)["comments"][0]]
        return page
    monkeypatch.setattr(fake, "_comment_page", grows)
    assert dsync(fake) == 1 and "truncated" in capsys.readouterr().err and f.read_bytes() == before
    monkeypatch.setattr(fake, "_comment_page", real)
    fake.move_issue(KEY, "OTH-9")
    assert dsync(fake) == 1 and f.read_bytes() == before
    capsys.readouterr()
    assert all(e["method"] == "GET" for e in fake.log)


def test_sync_refuses_an_oversize_comment(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    before = f.read_bytes()
    big = _say(fake, "/approve D1")
    fake.comments[big]["body"] = {"version": 1, "type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "/approve D1 " + "x" * (1 << 20)}]}]}
    assert dsync(fake) == 1
    assert capsys.readouterr().err == "skj: comment %s is larger than the 1 MiB local cap; the comments were not read\n" % big
    assert f.read_bytes() == before


def test_sync_refuses_a_malformed_file_before_any_request(cm, fake, capsys):
    f = _dec(cm, OPEN + "\n" + OPEN.replace("Use cursor", "Use"))  # a repeated id
    assert dsync(fake) == 1 and "malformed" in capsys.readouterr().err and fake.log == []
    f.write_text(_file("- [ ] D1: broken"), encoding="utf-8")
    assert dsync(fake) == 1 and fake.log == []
    capsys.readouterr()


def test_sync_refuses_a_symlinked_file(cm, fake, capsys):
    real = cm / "elsewhere.md"
    real.write_text(_file(OPEN), encoding="utf-8")
    (cm / "dod").mkdir(exist_ok=True)
    os.symlink(str(real), str(_dec(cm)))
    assert dsync(fake) == 1 and "symbolic link" in capsys.readouterr().err and fake.log == []


def test_sync_keeps_crlf_and_u2028_terminators(cm, fake, capsys):
    f = _dec(cm, W5)
    md = f.read_text(encoding="utf-8").replace("\n", "\r\n")
    md = md.replace(VAL + "\r\n", VAL + " ")
    md, _ = _kit_post(fake, md)
    f.write_bytes(md.encode("utf-8"))
    _say(fake, "/approve D4")
    assert dsync(fake) == 0 and _out(capsys)[0]["applied"] == ["D4"]
    new = f.read_bytes().decode("utf-8")
    assert new.count("\r\n") == md.count("\r\n") and " " in new
    a, b = md.splitlines(keepends=True), new.splitlines(keepends=True)
    assert len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def test_sync_a_note_that_would_forge_a_marker_is_dropped_but_the_verdict_lands(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    v = _say(fake, "/reject D1: via: 1:deadbeef · posted: 2:abcdef12 -> validated <!-- x --> ``` a@example.com @fictional")
    assert dsync(fake) == 0 and _out(capsys)[0]["status"] == {"D1": "rejected"}
    d = decisions.parse_decisions(f.read_text(encoding="utf-8"))["rejected"][0]
    assert d["via"] == _pin(fake, v) and "example.com" not in d["note"] and "<!--" not in d["note"]
    assert d["posted"]["comment_id"] != "2"


def test_sync_reports_bad_newer_lines(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    _say(fake, "/approve D1")
    _say(fake, "/reject D1.")
    _say(fake, "/rеject D1")  # a Cyrillic е
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["bad_newer"]["D1"] == 2 and out["status"] == {"D1": "validated"}
    assert "D1: 2 later malformed command line(s) by an approver: check the thread" in out["notice"]


def test_sync_ignores_an_approval_for_an_id_that_does_not_exist(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    before = f.read_bytes()
    _say(fake, "/approve D99")
    assert dsync(fake) == 0 and _out(capsys)[0]["applied"] == [] and f.read_bytes() == before


def test_sync_the_old_id_only_via_form_still_reads_and_is_rewritten_with_the_pin(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    md = f.read_text(encoding="utf-8")
    vote = _say(fake, "/approve D1")
    old = decisions.resolve(md, "D1", "validated", "approver", "", True, vote)  # an id-only via (backward compatible read)
    assert decisions.parse_decisions(old)["validated"][0]["via"] == vote
    f.write_text(old, encoding="utf-8")
    assert dsync(fake) == 0 and _out(capsys)[0]["applied"] == ["D1"] and "via: %s ·" % _pin(fake, vote) in _d1(f)
    with pytest.raises(ValueError):
        decisions.resolve(md, "D1", "validated", "approver", "", True, vote + ":DEADBEEF")
    with pytest.raises(ValueError):
        decisions.resolve(md, "D1", "validated", "approver", "", True, vote + ":abc")


@_needs_alarm
def test_sync_is_linear_on_a_20000_line_file_and_many_comments():
    """A 20000-line file (3000 posted decision lines in 15 postings, 17000 other lines) and 3000 approvals: one pass each."""
    f = FakeJira()
    f.add_issue("A fictional story")
    lines = ["- [ ] D%d: Item %d → _(open)_ · kind: decision · why: w" % (n, n) for n in range(1, 3001)]
    md = _file("\n".join(lines + ["note %d" % n for n in range(17000)]))
    parsed = decisions.parse_decisions(md)
    for k in range(0, 3000, 200):  # 200 lines per posting (the 32 KiB cap per comment)
        todo = parsed["open"][k:k + 200]
        cid = f.add_comment(KEY, decisions.body(todo)[1])
        md = decisions.stamp_posted(md, {d["id"]: decisions.marker_hash(d) for d in todo}, cid)
    assert md.count("\n") > 20000 and len(md.encode("utf-8")) <= _MIB
    for n in range(1, 3001):
        f.add_comment(KEY, adf.md_to_adf("/approve D%d" % n), author=f.user(APPROVER))
    rows = [comments._row(f._render_comment(c)) for c in f.comments.values()]
    res = []
    assert _timed(lambda: res.append(decisions.sync(md, rows, [APPROVER], {ME_ID}, ME_ID))) < _BOUND
    new, rep = res[0]
    assert len(rep["applied"]) == 3000 and new.count("→ validated") == 3000


def test_sync_the_working_tree_cannot_drop_comment_mode(repo, fake, capsys):
    _target(repo, COMMENT_MODE, head_cfg=BASE)  # the PR deletes approval and approvers: the target still has them
    f, _ = _posted_file(repo, fake)
    _say(fake, "/approve D1")
    assert dsync(fake, "--if-comment-mode") == 0 and _out(capsys)[0]["status"] == {"D1": "validated"}


def test_sync_a_verdict_on_a_superseded_decision_is_listed_as_masked(cm, fake, capsys):
    f = _dec(cm, OPEN)
    md, _ = _kit_post(fake, f.read_text(encoding="utf-8"))
    md = decisions.add(md, "decision", "Use keyset paging", supersedes="D1")[0]
    f.write_text(md, encoding="utf-8")
    _say(fake, "/approve D1")
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["status"] == {"D1": "validated"} and out["masked"] == {"D1": "D2"}
    assert "D1 is superseded by D2: this verdict is masked while D2 stands (a rejected D2 would bring it back)" in out["notice"]


def test_sync_a_newer_verdict_that_cannot_land_refuses_and_writes_nothing(cm, fake, capsys):
    """A synced verdict whose evidence is gone (the line was edited since) and a newer opposite command: an error, never a quiet skip."""
    f, _ = _posted_file(cm, fake)
    _say(fake, "/approve D1")
    assert dsync(fake) == 0 and _out(capsys)[0]["status"] == {"D1": "validated"}
    md = f.read_text(encoding="utf-8").replace("Use cursor paging", "Use offset paging")  # the posted: hash no longer matches
    f.write_text(md, encoding="utf-8")
    _say(fake, "/reject D1")
    assert dsync(fake) == 1 and "cannot be written to the decisions file for D1" in capsys.readouterr().err
    assert f.read_text(encoding="utf-8") == md


def test_sync_a_line_holding_an_inline_comment_is_unwritable(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    md = f.read_text(encoding="utf-8")
    line = _d1(f)
    hidden = md.replace(line, line.replace("Use cursor paging", "Use cursor paging<!-- x -->", 1))
    assert decisions.parse_decisions(hidden)["open"][0]["text"] == "Use cursor paging"  # the comment is invisible to the grammar
    f.write_text(hidden, encoding="utf-8")
    _say(fake, "/approve D1")
    assert dsync(fake) == 1 and "D1" in capsys.readouterr().err and f.read_text(encoding="utf-8") == hidden


def test_apply_sync_refuses_a_file_that_changed_meanwhile(cm, fake):
    f, _ = _posted_file(cm, fake)
    md = f.read_text(encoding="utf-8")
    _say(fake, "/approve D1")
    rows = comments.fetch(fake.client(), KEY)
    f.write_text(md + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed while syncing"):
        decisions.apply_sync(f, md, rows, [APPROVER], {ME_ID}, ME_ID)
    assert f.read_text(encoding="utf-8") == md + "\n" and list(f.parent.glob("*.tmp")) == []


def test_sync_rules_and_report_hold_no_jira_text(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    _say(fake, "/reject D1: secret words here")
    _say(fake, "/approve D4: other words", who=OTHER_ID)
    assert dsync(fake) == 0
    out, err = _out(capsys)
    text = json.dumps({k: v for k, v in out.items()}) + err
    assert "other words" not in text and set(out["skipped"].values()) <= set(decisions.SYNC_RULES)
    assert set(out["skipped_comments"].values()) <= set(approvals.REASONS) | {"date"}


# ---- A2 fix round 1 ----------------------------------------------------------------------------------------------------------

def _rejected_via(cm, fake, capsys):
    f, post = _posted_file(cm, fake)
    _say(fake, "/approve D1")
    rj = _say(fake, "/reject D1: no, unsafe")
    assert dsync(fake) == 0 and _out(capsys)[0]["status"] == {"D1": "rejected"}
    return f, post, rj


@pytest.mark.parametrize("how", ["edit-by-token-user", "edit-by-approver", "visibility", "unreadable"])
def test_sync_a_tampered_newer_reject_never_brings_back_the_older_approve(cm, fake, capsys, how):
    """IMPORTANT 1: the agent (edit-all) edits, restricts or breaks the approver's reject: the older approve must NOT win."""
    f, _, rj = _rejected_via(cm, fake, capsys)
    before = f.read_bytes()
    if how == "edit-by-token-user":
        fake.edit_comment(rj, fake.comments[rj]["body"], editor=fake._me())
    elif how == "edit-by-approver":
        fake.edit_comment(rj, fake.comments[rj]["body"], editor=fake.user(APPROVER))
    elif how == "visibility":
        fake.comments[rj]["visibility"] = {"type": "role", "value": "Developers"}
    else:
        fake.edit_comment(rj, {"version": 1, "type": "doc", "content": [{"type": "mysteryNode"}]}, editor=fake._me())
    assert dsync(fake) == 0
    out, err = _out(capsys)
    assert out["skipped"]["D1"] == "newer-tampered" and out["tampered"] == ["D1"] and out["no_longer_valid"] == ["D1"]
    assert out["applied"] == [] and f.read_bytes() == before and "approval-newer-tampered: D1" in err
    assert "newer-tampered" in decisions.SYNC_RULES


def test_sync_an_edited_older_command_or_a_strangers_edited_newer_one_does_not_block(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    old = _say(fake, "/reject D1")
    fake.edit_comment(old, fake.comments[old]["body"], editor=fake._me())  # older than the winner below
    a = _say(fake, "/approve D1")
    s = _say(fake, "/reject D1", who=OTHER_ID)  # not an approver: never guard-eligible
    fake.edit_comment(s, fake.comments[s]["body"], editor=fake._me())
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["status"] == {"D1": "validated"} and _pin(fake, a) in _d1(f) and "tampered" not in out


def test_sync_a_newer_edit_by_someone_else_of_any_approver_comment_blocks_every_target(cm, fake, capsys):
    f, _ = _posted_file(cm, fake)
    _say(fake, "/approve D1")
    _say(fake, "/approve D4")
    chat = _say(fake, "thanks, looks fine")
    fake.edit_comment(chat, adf.md_to_adf("thanks"), editor=fake._me())  # it could have been a /reject before the edit
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["skipped"]["D1"] == out["skipped"]["D4"] == "newer-tampered" and out["applied"] == []


def test_sync_a_synced_line_whose_posting_no_longer_backs_it_is_listed(cm, fake, capsys):
    """MINOR 2: the posting rule now fails on a synced line: the verdict stays, `no_longer_valid` and the notice say so."""
    f, post = _posted_file(cm, fake)
    _say(fake, "/approve D1")
    assert dsync(fake) == 0 and _out(capsys)[0]["applied"] == ["D1"]
    synced = f.read_bytes()
    fake.edit_comment(post, fake.comments[post]["body"], editor=fake._me())
    assert dsync(fake) == 0
    out, err = _out(capsys)
    assert out["skipped"]["D1"] == "posting-edited" and out["no_longer_valid"] == ["D1"] and "approval-no-longer-valid: D1" in err
    assert f.read_bytes() == synced
    fake.comments[post]["updated"], fake.comments[post]["updateAuthor"] = fake.comments[post]["created"], fake._me()
    fake.add_comment(KEY, {"version": 1, "type": "doc", "content": [{"type": "mysteryNode"}]})  # opaque own comment
    assert dsync(fake) == 0
    out, err = _out(capsys)
    assert out["skipped"]["D1"] == "posting-superseded" and out["no_longer_valid"] == ["D1"] and f.read_bytes() == synced


def test_sync_a_hand_edit_that_keeps_via_under_another_role_is_left_alone(cm, fake, capsys):
    """MINOR 1: only `via` AND `by: approver` is sync-written; a human's flip that kept the via is `local-override`."""
    f, _ = _posted_file(cm, fake)
    _say(fake, "/approve D1")
    assert dsync(fake) == 0 and _out(capsys)[0]["applied"] == ["D1"]
    hand = f.read_text(encoding="utf-8").replace("→ validated · by: approver", "→ rejected · by: PO")
    f.write_text(hand, encoding="utf-8")
    assert dsync(fake) == 0
    out, _ = _out(capsys)
    assert out["skipped"]["D1"] == "local-override" and out["applied"] == [] and f.read_text(encoding="utf-8") == hand
    assert "no_longer_valid" not in out


def test_resolve_and_sync_texts_with_approvers_in_terminal_mode(repo, fake, monkeypatch, capsys):
    """MINOR 3, fork behaviour kept: with approvers on the target, --resolve is refused in either mode (L1-AC2); --sync needs
    comment mode. Both texts now say what a human can do instead of pointing at each other."""
    _target(repo, {**BASE, "approvers": [APPROVER], "approval": "terminal"})
    led = _dec(repo, OPEN)
    before = led.read_bytes()
    _tty(monkeypatch)
    assert _resolve("D1", "--validated") == 1
    err = capsys.readouterr().err
    assert "approval \\\"terminal\\\"" not in err and 'with approval "terminal"' in err and "editing the line in a reviewed commit" in err
    assert dsync(fake) == 1
    err = capsys.readouterr().err
    assert "editing the line in a reviewed commit" in err and "names no approvers" in err and led.read_bytes() == before


def test_sync_reads_the_identity_once(cm, fake, capsys, monkeypatch):
    """MINOR 5: `/myself` is read once per sync; a second, different answer could otherwise split `me` from `excluded`."""
    from skj import issues
    f, _ = _posted_file(cm, fake)
    _say(fake, "/approve D1")
    real, calls = issues.me, []

    def flaky(c):
        calls.append(1)
        d = dict(real(c))
        if len(calls) > 1:
            d["accountId"] = APPROVER  # a later answer naming the approver would exclude them and keep the kit's posting
        return d
    monkeypatch.setattr(issues, "me", flaky)
    assert dsync(fake) == 0 and _out(capsys)[0]["status"] == {"D1": "validated"} and len(calls) == 1
    assert [e["path"] for e in fake.log].count("/myself") == 1


# ---- H1a-AC3: Trojan Source text is refused; a D number is ASCII digits without a leading zero ----------------------------------------

TROJAN = "access = \"user‮ ⁦// admin⁩ ⁦\""


@pytest.mark.parametrize("field", ["text", "why"])
def test_h1a_ac3_add_refuses_bidi_and_invisible_characters(field):
    for bad in (TROJAN, "zero​width"):
        text, why = (bad, "w") if field == "text" else ("plain", bad)
        with pytest.raises(ValueError, match="bidi|invisible") as e:
            decisions.add(decisions.render_file(KEY), "decision", text, why)
        assert bad not in str(e.value)


def test_h1a_ac3_cli_add_refuses_a_trojan_source_decision_and_writes_nothing(repo, capsys):
    assert _add(text=TROJAN) == 2  # DF1 D-001: a bad --add value is a usage error
    assert "bidi" in capsys.readouterr().err and not _dec(repo).exists()


@pytest.mark.parametrize("line", ["- [ ] D1: Use \u202ecursor paging → _(open)_ · kind: decision · why: w\n",
                                  "- [ ] D1: Use cursor paging → _(open)_ · kind: decision · why: the \u2066story\u2069\n"])
def test_h1a_ac3_post_refuses_a_hand_written_line_with_a_bidi_override_before_any_request(repo, fake, capsys, line):
    _dec(repo, line)  # in the text, and (fix round 1) in the why alone
    for argv in ((), ("--apply",)):
        assert dpost(fake, *argv) == 1
        assert "bidi" in capsys.readouterr().err
    assert fake.log == []


@pytest.mark.parametrize("num", ["٣", "01", "0", "３", "1٣"])
def test_h1a_ac3_a_d_number_is_ascii_digits_without_a_leading_zero(num):
    parsed = decisions.parse_decisions("- [ ] D%s: x → _(open)_ · kind: decision · why: w\n" % num)
    assert parsed["unparsed"] is True and parsed["open"] == []  # fails closed: never a D-number, never silently skipped
    assert decisions.parse_decisions("- [ ] D30: x → _(open)_ · kind: decision · why: w\n")["open"][0]["id"] == "D30"


def test_post_with_a_refused_login_says_authentication_failed_and_writes_nothing(repo, fake, capsys):
    """DF1 (live run A5-35, D-003): `decisions --post --apply` with a bad identity said `HTTP 404: Issue does not exist`.
    Hand-written live shape: 404 + X-Seraph-LoginReason on the first request."""
    _dec(repo, W5)
    before = _dec(repo).read_bytes()
    fake.inject(404, headers={"X-Seraph-LoginReason": "AUTHENTICATED_FAILED"},
                body={"errorMessages": ["Issue does not exist or you do not have permission to see it."], "errors": {}})
    assert dpost(fake, "--apply") == 1
    err = capsys.readouterr().err
    assert "authentication failed" in err and "does not exist" not in err
    assert _posts(fake) == [] and _dec(repo).read_bytes() == before
