"""RU3: `skj rules <ref> --confirm`: the hashes a human reviewed go into the ledger header; local only, no identity, no request. Ported from
the `--confirm` half of the fork's tests/test_rules_pull.py (work item ids -> keys, the fork CLI -> `skj`, `error:` -> `skj:`, `id` -> `key` in
the JSON). The chain the fixtures pull is the hand-written one of tests/test_rules_pull.py (LOY-1 Epic <- LOY-2 Task <- LOY-3 Story);
the dropped fork tests are listed with reasons in tests/PORT_NOTES.md. All data is fictional."""
import json
import os
import sys

import pytest

from skj import decisions, dod, rules, stack
from skj.__main__ import main
from test_rules_pull import TYPES, chain, desc, repo, write_cfg  # noqa: F401  (repo: fixture)
from test_stack_support import TypedStdin, run_in_pty, terminal

pytestmark = pytest.mark.rules

FROM_LINE = "- **Rules from:** LOY-2, LOY-1\n"
CMD = ["rules", "LOY-3", "--confirm"]


def _no_jira():
    raise AssertionError("--confirm must not reach Jira")


class Ctx:
    """A pulled repo (Rules_LOY-2.md, Rules_LOY-1.md) and a ledger with `Rules from: LOY-2, LOY-1` in `root`."""

    def __init__(self, root):
        self.root = root
        self.ledger = root / "dod" / "DoD_LOY-3.md"
        self.h = {r: v["hash"] for k in ("LOY-1", "LOY-2") for r, v in rules.parse_file(self.rules_text(k), k)["rules"].items()}

    def rules_text(self, key):
        return (self.root / "dod" / f"Rules_{key}.md").read_text(encoding="utf-8")

    def text(self):
        return self.ledger.read_bytes().decode("utf-8")

    def put(self, md):
        self.ledger.write_bytes(md.encode("utf-8"))

    def record(self, value):
        """Hand-write a `Rules reviewed:` line under `Rules from:` (as an earlier --confirm would have)."""
        md = self.text()
        assert FROM_LINE in md
        self.put(md.replace(FROM_LINE, FROM_LINE + f"- **Rules reviewed:** {value}\n"))

    def value(self):
        return self.text().split("- **Rules reviewed:** ")[1].split("\n")[0].rstrip("\r")

    def want(self):
        return f"BR-LOY-1-1@{self.h['BR-LOY-1-1']}, BR-LOY-2-1@{self.h['BR-LOY-2-1']}"

    def run(self, capsys, *extra):
        code = main([*CMD, *extra], client_factory=_no_jira)
        cap = capsys.readouterr()
        return code, (json.loads(cap.out) if cap.out.strip().startswith("{") else None), cap.out, cap.err


def _pulled(repo, capsys, f):
    assert main(["rules", "LOY-3"], client_factory=f.client) == 0
    assert main(["dod", "seed", "LOY-3"], client_factory=f.client) == 0
    capsys.readouterr()
    return Ctx(repo)


@pytest.fixture
def ctx(repo, capsys):
    return _pulled(repo, capsys, chain())


def refusal(recorded="none", changed="none", removed="none"):
    return rules.CONFIRM_TERMINAL.format(recorded=recorded, changed=changed, removed=removed)


def err_of(text):
    return f"skj: {text}\n"


def leftovers(ctx):
    return sorted(p.name for p in (ctx.root / "dod").iterdir() if p.name.endswith(".tmp"))


def test_first_confirm_without_tty_writes(ctx, capsys):
    before = ctx.text()
    code, out, raw, err = ctx.run(capsys)
    assert code == 0 and err == ""
    assert ctx.value() == ctx.want()  # sorted by issue, then number; the draft BR-LOY-2-2 and the retired BR-LOY-1-3 are not in scope
    assert ctx.text() == before.replace(FROM_LINE, FROM_LINE + f"- **Rules reviewed:** {ctx.want()}\n")
    assert dod.ledger_rules_reviewed(ctx.text()) == {"present": True, "valid": True, "hashes": {"BR-LOY-1-1": ctx.h["BR-LOY-1-1"], "BR-LOY-2-1": ctx.h["BR-LOY-2-1"]}}
    assert out == {"key": "LOY-3", "reviewed": ctx.want(), "added": ["BR-LOY-1-1", "BR-LOY-2-1"], "changed": [], "removed": [], "written": True}
    assert "Customers earn" not in raw and leftovers(ctx) == []


def test_a_bare_number_names_the_story_too(ctx, capsys):
    assert main(["rules", "3", "--confirm"], client_factory=_no_jira) == 0 and ctx.value() == ctx.want()


def test_confirm_is_a_no_op_when_nothing_changed(ctx, capsys):
    assert ctx.run(capsys)[0] == 0
    first = ctx.ledger.stat()
    code, out, _, _ = ctx.run(capsys)
    assert code == 0 and out["written"] is False and out["added"] == out["changed"] == out["removed"] == []
    assert ctx.ledger.stat().st_ino == first.st_ino and ctx.ledger.stat().st_mtime_ns == first.st_mtime_ns


def test_a_recorded_line_in_another_order_is_not_a_change(ctx, capsys):
    ctx.record(f"BR-LOY-2-1@{ctx.h['BR-LOY-2-1']}, BR-LOY-1-1@{ctx.h['BR-LOY-1-1']}")
    before = ctx.text()
    code, out, _, _ = ctx.run(capsys)
    assert code == 0 and out["written"] is False and ctx.text() == before


def test_new_ids_are_added_without_a_terminal_and_the_old_ones_kept(ctx, capsys):
    ctx.record(f"BR-LOY-1-1@{ctx.h['BR-LOY-1-1']}")
    code, out, _, _ = ctx.run(capsys)
    assert code == 0 and out["added"] == ["BR-LOY-2-1"] and out["changed"] == [] and ctx.value() == ctx.want()


def test_changed_hash_refused_without_tty(ctx, capsys):
    ctx.record(f"BR-LOY-1-1@{ctx.h['BR-LOY-1-1']}, BR-LOY-2-1@aaaaaaaa")
    before = ctx.text()
    code, out, raw, err = ctx.run(capsys)
    assert code == 2 and out is None and raw == ""
    assert err == err_of(refusal(changed="BR-LOY-2-1")) and ctx.text() == before
    assert "aaaaaaaa" not in err and ctx.h["BR-LOY-2-1"] not in err and leftovers(ctx) == []


def test_changed_hash_with_tty_rewrites_only_that_line(ctx, capsys, monkeypatch):
    ctx.record(f"BR-LOY-1-1@{ctx.h['BR-LOY-1-1']}, BR-LOY-2-1@aaaaaaaa")
    before = ctx.text()
    stdin = terminal(monkeypatch)
    code, out, raw, err = ctx.run(capsys)
    assert code == 0 and err == "" and stdin.lines == 1
    assert "BR-LOY-2-1" in raw and "aaaaaaaa" in raw and ctx.h["BR-LOY-2-1"] in raw and "Customers earn" not in raw
    after = ctx.text()
    assert after == before.replace("BR-LOY-2-1@aaaaaaaa", f"BR-LOY-2-1@{ctx.h['BR-LOY-2-1']}") and leftovers(ctx) == []
    assert json.loads(raw[raw.index("{"):]) == {"key": "LOY-3", "reviewed": ctx.want(), "added": [], "changed": ["BR-LOY-2-1"], "removed": [], "written": True}


def test_dropping_a_recorded_id_needs_the_terminal_too(ctx, capsys, monkeypatch):
    ctx.record(f"BR-LOY-1-1@{ctx.h['BR-LOY-1-1']}, BR-LOY-1-2@bbbbbbbb, BR-LOY-2-1@{ctx.h['BR-LOY-2-1']}")  # BR-LOY-1-2 is in no file
    before = ctx.text()
    code, _, _, err = ctx.run(capsys)
    assert code == 2 and err == err_of(refusal(removed="BR-LOY-1-2")) and ctx.text() == before
    terminal(monkeypatch)
    code, out, raw, _ = ctx.run(capsys)
    assert code == 0 and "BR-LOY-1-2" in raw and "bbbbbbbb" in raw and out is None  # the preview comes first, the JSON after it
    assert ctx.value() == ctx.want()


def test_an_excluded_rule_leaves_the_record_only_through_the_terminal(ctx, capsys, monkeypatch):
    md, _ = decisions.add(decisions.render_file("LOY-3"), "decision", "out of scope: BR-LOY-2-1", why="not for this story")
    d1 = decisions.parse_decisions(md)["open"][0]
    (ctx.root / "dod" / "Decisions_LOY-3.md").write_text(md.rstrip("\n") + f" · posted: 5:{decisions.marker_hash(d1)}\n", encoding="utf-8")
    ctx.record(ctx.want())
    assert ctx.run(capsys)[0] == 2
    terminal(monkeypatch)
    code, _, _, _ = ctx.run(capsys)
    assert code == 0 and ctx.value() == f"BR-LOY-1-1@{ctx.h['BR-LOY-1-1']}"


def test_nothing_in_scope_records_none(ctx, capsys):
    ctx.put(ctx.text().replace(FROM_LINE, "- **Rules from:** none\n"))
    code, out, _, _ = ctx.run(capsys)
    assert code == 0 and ctx.value() == "none" and out["reviewed"] == "none" and dod.ledger_rules_reviewed(ctx.text())["valid"]


def test_confirm_survives_refresh(ctx, capsys):
    assert ctx.run(capsys)[0] == 0
    line = f"- **Rules reviewed:** {ctx.want()}\n"
    assert main(["dod", "refresh", "LOY-3"], client_factory=chain().client) == 0
    assert line in ctx.text() and ctx.text().count("Rules reviewed") == 1
    write_cfg(ctx.root, {"enabled": False})  # rules off: the line is kept too
    assert main(["dod", "refresh", "LOY-3"], client_factory=chain().client) == 0 and line in ctx.text()


def test_missing_ledger_exit_2(ctx, capsys):
    ctx.ledger.unlink()
    code, out, _, err = ctx.run(capsys)
    assert code == 2 and out is None and err == err_of(rules.CONFIRM_NO_LEDGER) and not ctx.ledger.exists()


def test_crlf_ledger_keeps_its_terminator_on_insert_and_on_replace(ctx, capsys, monkeypatch):
    ctx.put(ctx.text().replace("\n", "\r\n"))
    before = ctx.ledger.read_bytes()
    assert ctx.run(capsys)[0] == 0
    after = ctx.ledger.read_bytes()
    crlf = FROM_LINE.replace("\n", "\r\n").encode()
    assert after == before.replace(crlf, crlf + b"- **Rules reviewed:** " + ctx.want().encode() + b"\r\n")
    assert after.count(b"\n") == after.count(b"\r\n")
    ctx.put(ctx.text().replace(f"BR-LOY-2-1@{ctx.h['BR-LOY-2-1']}", "BR-LOY-2-1@aaaaaaaa"))
    before = ctx.ledger.read_bytes()
    terminal(monkeypatch)
    assert ctx.run(capsys)[0] == 0
    assert ctx.ledger.read_bytes() == before.replace(b"BR-LOY-2-1@aaaaaaaa", f"BR-LOY-2-1@{ctx.h['BR-LOY-2-1']}".encode())


def test_a_symlinked_ledger_is_refused_and_nothing_is_written(ctx, capsys):
    real = ctx.root / "real_ledger.md"
    real.write_bytes(ctx.ledger.read_bytes())
    ctx.ledger.unlink()
    ctx.ledger.symlink_to(real)
    before = real.read_bytes()
    code, _, _, err = ctx.run(capsys)
    assert code == 2 and err == err_of(rules.CONFIRM_LEDGER) and real.read_bytes() == before and ctx.ledger.is_symlink() and leftovers(ctx) == []


def test_a_hard_linked_or_non_regular_ledger_is_refused(ctx, capsys):
    os.link(ctx.ledger, ctx.root / "other_name.md")
    assert ctx.run(capsys)[0] == 2 and "Rules reviewed" not in ctx.text()
    os.unlink(ctx.root / "other_name.md")
    ctx.ledger.unlink()
    ctx.ledger.mkdir()
    code, _, _, err = ctx.run(capsys)
    assert code == 2 and err == err_of(rules.CONFIRM_LEDGER)


def test_a_rule_whose_text_no_longer_matches_its_heading_hash_is_not_recordable(ctx, capsys):
    path = ctx.root / "dod" / "Rules_LOY-2.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Customers earn 1 points per euro.", "Customers earn 100 points per euro."), encoding="utf-8")
    before = ctx.text()
    code, _, raw, err = ctx.run(capsys)
    assert code == 2 and raw == "" and ctx.text() == before
    assert err == err_of(rules.CONFIRM_HASH.format(ids="BR-LOY-2-1")) and "100 points" not in err


def test_an_invalid_or_missing_rules_file_is_refused(ctx, capsys):
    before = ctx.text()
    path = ctx.root / "dod" / "Rules_LOY-1.md"
    good = path.read_text(encoding="utf-8")
    path.write_text(good + "\n<!-- hide -->\n", encoding="utf-8")
    assert ctx.run(capsys)[3] == err_of(rules.CONFIRM_FILES)
    path.write_text(good.replace("rules-format: 2", "rules-format: 3"), encoding="utf-8")
    assert ctx.run(capsys)[3] == err_of(rules.CONFIRM_FILES)
    path.unlink()
    code, _, _, err = ctx.run(capsys)
    assert code == 2 and err == err_of(rules.CONFIRM_FILES) and ctx.text() == before


def test_a_symlinked_rules_file_is_refused(ctx, capsys):
    path = ctx.root / "dod" / "Rules_LOY-1.md"
    real = ctx.root / "copy.md"
    real.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(real)
    assert ctx.run(capsys)[3] == err_of(rules.CONFIRM_FILES) and "Rules reviewed" not in ctx.text()


def test_an_unreadable_decisions_file_is_refused(ctx, capsys):
    (ctx.root / "dod" / "Decisions_LOY-3.md").mkdir()
    code, _, _, err = ctx.run(capsys)
    assert code == 2 and err == err_of(rules.CONFIRM_DECISIONS) and "Rules reviewed" not in ctx.text()


@pytest.mark.parametrize("name,edit", [
    ("opener in the title hides the first heading", lambda md: md.replace("# DoD — LOY-3: ", "# DoD — LOY-3: x <!-- ", 1)),
    ("a fence hides the first heading", lambda md: md.replace("\n- **Jira:**", "\n```\n- **Jira:**", 1)),
    ("two Rules reviewed lines", lambda md: md.replace(FROM_LINE, FROM_LINE + "- **Rules reviewed:** none\n- **Rules reviewed:** none\n")),
    ("a malformed Rules reviewed line", lambda md: md.replace(FROM_LINE, FROM_LINE + "- **Rules reviewed:** BR-LOY-2-1 deadbeef\n")),
    ("a quoted Rules reviewed line", lambda md: md.replace(FROM_LINE, FROM_LINE + "> - **Rules reviewed:** none\n")),
    ("a repeated id", lambda md: md.replace(FROM_LINE, FROM_LINE + "- **Rules reviewed:** BR-LOY-2-1@aaaaaaaa, BR-LOY-2-1@bbbbbbbb\n")),
    ("a malformed Rules from line", lambda md: md.replace(FROM_LINE, "- **Rules from:** LOY-2,LOY-1\n")),
    ("two Rules from lines", lambda md: md.replace(FROM_LINE, FROM_LINE + FROM_LINE)),
])
def test_an_untrusted_or_malformed_header_is_refused(ctx, capsys, monkeypatch, name, edit):
    ctx.put(edit(ctx.text()))
    before = ctx.ledger.read_bytes()
    for tty in (False, True):
        if tty:
            terminal(monkeypatch)
        code, _, _, err = ctx.run(capsys)
        assert code == 2 and err == err_of(rules.CONFIRM_HEADER) and ctx.ledger.read_bytes() == before, name
    assert leftovers(ctx) == []


def test_a_ledger_without_rules_from_is_refused(ctx, capsys):
    ctx.put(ctx.text().replace(FROM_LINE, ""))
    code, _, _, err = ctx.run(capsys)
    assert code == 2 and err == err_of(rules.CONFIRM_NO_FROM) and "Rules reviewed" not in ctx.text()


def test_a_forged_line_in_section_1_is_not_the_header(ctx, capsys):
    ctx.put(ctx.text().replace("### Acceptance Criteria\n", "### Acceptance Criteria\n- **Rules reviewed:** BR-LOY-2-1@aaaaaaaa\n"))
    code, out, _, _ = ctx.run(capsys)
    assert code == 0 and out["added"] == ["BR-LOY-1-1", "BR-LOY-2-1"] and ctx.value() == ctx.want()


def test_the_confirm_code_covers_the_ledger_and_the_value(ctx):
    ctx.record(f"BR-LOY-1-1@{ctx.h['BR-LOY-1-1']}, BR-LOY-2-1@aaaaaaaa")
    a = rules.prepare_confirm(ctx.root, "LOY-3")
    assert a["needs_human"] and a["changed"] == ["BR-LOY-2-1"] and a["code"] == rules.prepare_confirm(ctx.root, "LOY-3")["code"]
    ctx.put(ctx.text().replace("Last synced:** ", "Last synced:** x"))
    assert rules.prepare_confirm(ctx.root, "LOY-3")["code"] != a["code"]


# ---- the typed approval code (the same mechanism as `skj stack approve`) ----

def stale(ctx):
    ctx.record(f"BR-LOY-1-1@{ctx.h['BR-LOY-1-1']}, BR-LOY-2-1@aaaaaaaa")
    return ctx.ledger.read_bytes()


@pytest.mark.parametrize("answer", ["deadbeef0000\n", "\n", "", "yes\n", KeyboardInterrupt])
def test_a_wrong_or_missing_code_confirms_nothing(ctx, capsys, monkeypatch, answer):
    before = stale(ctx)
    terminal(monkeypatch, answer)
    code, _, _, err = ctx.run(capsys)
    assert code == 2 and err == err_of(stack.E_NOT_CONFIRMED) and ctx.ledger.read_bytes() == before and leftovers(ctx) == []


def test_the_code_is_never_read_from_a_pipe(ctx, capsys, monkeypatch):
    before = stale(ctx)

    class Piped(TypedStdin):
        def isatty(self):
            return False
    monkeypatch.setattr(sys, "stdin", Piped())
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    code, _, raw, err = ctx.run(capsys)
    assert code == 2 and err == err_of(refusal(changed="BR-LOY-2-1")) and "approval code" not in raw and ctx.ledger.read_bytes() == before


def test_stdout_must_be_a_terminal_too(ctx, capsys, monkeypatch):
    before = stale(ctx)
    monkeypatch.setattr(sys, "stdin", TypedStdin())
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    code, _, raw, err = ctx.run(capsys)
    assert code == 2 and err == err_of(refusal(changed="BR-LOY-2-1")) and "approval code" not in raw and ctx.ledger.read_bytes() == before


def test_the_ledger_edited_between_the_prompt_and_the_code_is_refused(ctx, capsys, monkeypatch):
    before = stale(ctx)

    def edit(code):
        ctx.ledger.write_bytes(before.replace(b"Last synced:** ", b"Last synced:** x"))
    terminal(monkeypatch, None, before=edit)
    code, _, _, err = ctx.run(capsys)
    assert code == 2 and err == err_of(rules.CONFIRM_SWAPPED) and b"BR-LOY-2-1@aaaaaaaa" in ctx.ledger.read_bytes()


def test_a_rules_file_edited_between_the_prompt_and_the_code_is_refused(ctx, capsys, monkeypatch):
    before = stale(ctx)
    path = ctx.root / "dod" / "Rules_LOY-2.md"

    def edit(code):
        path.write_text(path.read_text(encoding="utf-8").replace("Customers earn 1 points per euro.", "Customers earn 100 points per euro."), encoding="utf-8")
    terminal(monkeypatch, None, before=edit)
    code, _, _, err = ctx.run(capsys)
    assert code == 2 and err.startswith("skj: ") and "nothing" in err and ctx.ledger.read_bytes() == before


def test_the_ledger_replaced_by_a_symlink_before_the_write_is_refused(ctx):
    before = stale(ctx)
    prep = rules.prepare_confirm(ctx.root, "LOY-3")
    real = ctx.root / "elsewhere.md"
    real.write_bytes(before)
    ctx.ledger.unlink()
    ctx.ledger.symlink_to(real)
    with pytest.raises(ValueError) as e:
        prep["write"]()
    assert str(e.value) == rules.CONFIRM_SWAPPED and real.read_bytes() == before and ctx.ledger.is_symlink() and leftovers(ctx) == []


def test_a_write_error_has_one_fixed_text(ctx, monkeypatch):
    before = stale(ctx)
    prep = rules.prepare_confirm(ctx.root, "LOY-3")
    monkeypatch.setattr(stack, "_atomic_replace", lambda *a, **k: (_ for _ in ()).throw(ValueError(stack._E_BACK)))
    with pytest.raises(ValueError) as e:
        prep["write"]()
    assert str(e.value) == rules.CONFIRM_WRITE and ctx.ledger.read_bytes() == before


def test_the_text_about_to_be_renamed_is_checked(ctx, monkeypatch):
    stale(ctx)
    prep = rules.prepare_confirm(ctx.root, "LOY-3")
    seen = {}
    orig = stack._atomic_replace

    def spy(path, text, check=None, expect=None):
        seen["check"] = check
        return orig(path, text, check, expect)
    monkeypatch.setattr(stack, "_atomic_replace", spy)
    prep["write"]()
    good = ctx.ledger.read_bytes()
    seen["check"](good)
    for bad in (good.replace(b"BR-LOY-2-1@", b"BR-LOY-2-2@"), good.replace(b"Rules from:** LOY-2, LOY-1", b"Rules from:** LOY-2"),
                good.replace(b"Last synced:** ", b"Last synced:** x")):
        with pytest.raises(ValueError) as e:
            seen["check"](bad)
        assert str(e.value) == rules.CONFIRM_WRITE


@pytest.mark.parametrize("eol", ["\r", " "])
def test_an_unusual_line_terminator_on_the_line_is_refused(ctx, capsys, eol):
    ctx.put(ctx.text().replace(FROM_LINE, "- **Rules from:** LOY-2, LOY-1" + eol + "- **Note:** x\n"))
    before = ctx.ledger.read_bytes()
    code, _, _, err = ctx.run(capsys)
    assert code == 2 and err.startswith("skj: ") and ctx.ledger.read_bytes() == before


def test_the_new_line_takes_the_terminator_of_the_files_first_line(ctx, capsys, monkeypatch):
    mixed = ctx.text().replace("\n", "\r\n", 1)  # the first line is CRLF, every other line LF
    ctx.put(mixed)
    assert ctx.run(capsys)[0] == 0
    want = f"- **Rules reviewed:** {ctx.want()}"
    assert ctx.ledger.read_bytes() == mixed.replace(FROM_LINE, FROM_LINE + want + "\r\n").encode()
    stale_line = f"- **Rules reviewed:** BR-LOY-1-1@{ctx.h['BR-LOY-1-1']}, BR-LOY-2-1@aaaaaaaa\n"
    ctx.put(ctx.text().replace(want + "\r\n", stale_line))
    before = ctx.text()
    terminal(monkeypatch)
    assert ctx.run(capsys)[0] == 0
    assert ctx.ledger.read_bytes() == before.replace(stale_line, want + "\r\n").encode()


def test_early_typed_code_is_flushed_and_the_same_code_typed_after_the_prompt_confirms(ctx):
    stale(ctx)
    before = ctx.ledger.read_bytes()
    early = rules.prepare_confirm(ctx.root, "LOY-3")["code"]
    code, screen, err = run_in_pty(CMD, ctx.root, early=early, answer="")  # then end of input at the prompt
    assert code == 2 and stack.E_NOT_CONFIRMED in err and ctx.ledger.read_bytes() == before, (screen, err)
    code, screen, err = run_in_pty(CMD, ctx.root)
    assert code == 0 and ctx.value() == ctx.want(), (screen, err)
    assert "BR-LOY-2-1" in screen and "aaaaaaaa" in screen and "Customers earn" not in screen


def test_a_real_process_without_terminals_refuses_a_re_confirmation(ctx):
    stale(ctx)
    before = ctx.ledger.read_bytes()
    for kw in ({"stdout_pty": False}, {"stdin_pty": False}, {"stdout_pty": False, "stdin_pty": False}):
        code, screen, err = run_in_pty(CMD, ctx.root, **kw)
        assert code == 2 and refusal(changed="BR-LOY-2-1") in err and "approval code" not in screen and ctx.ledger.read_bytes() == before, kw
    code, screen, err = run_in_pty(CMD, ctx.root, answer="not-the-code")
    assert code == 2 and ctx.ledger.read_bytes() == before


def test_a_real_process_without_terminals_still_adds_new_ids(ctx):
    code, screen, err = run_in_pty(CMD, ctx.root, stdout_pty=False, stdin_pty=False)
    assert code == 0 and ctx.value() == ctx.want() and '"written": true' in screen, (screen, err)


def test_confirm_makes_no_request_and_needs_no_identity(ctx, monkeypatch):
    for k in ("SKJ_TOKEN", "SKJ_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    assert main(CMD, client_factory=_no_jira) == 0


def test_help_and_parser_for_confirm(capsys):
    with pytest.raises(SystemExit):
        main(["rules", "--help"])
    assert "--confirm" in capsys.readouterr().out
    for argv in (["rules", "--confirm"], ["rules", "LOY-3", "--confirm", "--apply"]):
        with pytest.raises(SystemExit) as e:
            main(argv)
        assert e.value.code == 2, argv


# ---- a confirm without a terminal records the free new ids even when another id changed or was dropped (the fork's P3-13c) ----

@pytest.fixture
def ctx3(repo, capsys):
    return _pulled(repo, capsys, chain(epic=desc((1, "approved"), (2, "approved"), (3, "retired"))))  # in scope: BR-LOY-1-1, BR-LOY-1-2, BR-LOY-2-1


def rec(ctx, *ids, **other):
    """`Rules reviewed:` text: the current hash of each id in `ids`, then `id=hash` pairs from `other` (keys with `_` for `-`)."""
    return ", ".join([f"{r}@{ctx.h[r]}" for r in ids] + [f"{r.replace('_', '-')}@{h}" for r, h in other.items()])


def test_no_terminal_one_changed_two_new_records_the_two_new_and_refuses_the_changed(ctx3, capsys):
    ctx3.record("BR-LOY-2-1@aaaaaaaa")
    before = ctx3.text()
    code, out, raw, err = ctx3.run(capsys)
    assert code == 2 and out is None and raw == ""
    assert err == err_of(refusal(recorded="BR-LOY-1-1, BR-LOY-1-2", changed="BR-LOY-2-1"))
    assert "aaaaaaaa" not in err and ctx3.h["BR-LOY-2-1"] not in err and "Customers earn" not in err
    assert ctx3.value() == rec(ctx3, "BR-LOY-1-1", "BR-LOY-1-2", BR_LOY_2_1="aaaaaaaa")  # the old record untouched, the two new ids in
    assert [x for x in ctx3.text().splitlines() if "Rules reviewed" not in x] == [x for x in before.splitlines() if "Rules reviewed" not in x]
    assert leftovers(ctx3) == []


def test_the_free_part_is_one_atomic_write(ctx3, capsys, monkeypatch):
    ctx3.record("BR-LOY-2-1@aaaaaaaa")
    calls = []
    real = stack._atomic_replace
    monkeypatch.setattr(stack, "_atomic_replace", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    assert ctx3.run(capsys)[0] == 2 and len(calls) == 1


def test_no_terminal_a_dropped_id_and_a_new_id(ctx3, capsys):
    ctx3.record(f"BR-LOY-1-1@{ctx3.h['BR-LOY-1-1']}, BR-LOY-1-9@bbbbbbbb, BR-LOY-2-1@{ctx3.h['BR-LOY-2-1']}")  # BR-LOY-1-9 is in no file
    code, _, raw, err = ctx3.run(capsys)
    assert code == 2 and raw == "" and err == err_of(refusal(recorded="BR-LOY-1-2", removed="BR-LOY-1-9"))
    assert ctx3.value() == rec(ctx3, "BR-LOY-1-1", "BR-LOY-1-2", BR_LOY_1_9="bbbbbbbb", BR_LOY_2_1=ctx3.h["BR-LOY-2-1"])


def test_no_terminal_changed_and_dropped_with_nothing_new_writes_nothing(ctx3, capsys):
    ctx3.record(f"BR-LOY-1-1@{ctx3.h['BR-LOY-1-1']}, BR-LOY-1-2@{ctx3.h['BR-LOY-1-2']}, BR-LOY-2-1@aaaaaaaa, BR-LOY-1-9@bbbbbbbb")
    before, st = ctx3.ledger.read_bytes(), ctx3.ledger.stat()
    code, _, _, err = ctx3.run(capsys)
    assert code == 2 and err == err_of(refusal(changed="BR-LOY-2-1", removed="BR-LOY-1-9"))
    assert ctx3.ledger.read_bytes() == before and ctx3.ledger.stat().st_ino == st.st_ino and ctx3.ledger.stat().st_mtime_ns == st.st_mtime_ns


def test_with_a_terminal_and_the_code_everything_is_recorded(ctx3, capsys, monkeypatch):
    ctx3.record("BR-LOY-2-1@aaaaaaaa")
    stdin = terminal(monkeypatch)
    code, _, raw, err = ctx3.run(capsys)
    assert code == 0 and err == "" and stdin.lines == 1
    assert ctx3.value() == rec(ctx3, "BR-LOY-1-1", "BR-LOY-1-2", "BR-LOY-2-1")
    assert json.loads(raw[raw.index("{"):]) == {"key": "LOY-3", "reviewed": ctx3.value(), "added": ["BR-LOY-1-1", "BR-LOY-1-2"],
                                                "changed": ["BR-LOY-2-1"], "removed": [], "written": True}


@pytest.mark.parametrize("answer", ["deadbeef0000\n", "", KeyboardInterrupt])
def test_a_wrong_code_records_nothing_not_even_the_new_ids(ctx3, capsys, monkeypatch, answer):
    ctx3.record("BR-LOY-2-1@aaaaaaaa")
    before = ctx3.ledger.read_bytes()
    terminal(monkeypatch, answer)
    code, _, _, err = ctx3.run(capsys)
    assert code == 2 and err == err_of(stack.E_NOT_CONFIRMED) and ctx3.ledger.read_bytes() == before and leftovers(ctx3) == []


def test_an_untrusted_or_malformed_header_writes_nothing_even_for_new_ids(ctx3, capsys):
    ctx3.record("BR-LOY-2-1@aaaaaaaa")
    bad = ctx3.text().replace("BR-LOY-2-1@aaaaaaaa", "BR-LOY-2-1@aaaaaaaa, BR-LOY-2-1@aaaaaaaa")  # a duplicate id: malformed
    ctx3.put(bad)
    code, _, raw, err = ctx3.run(capsys)
    assert code == 2 and err == err_of(rules.CONFIRM_HEADER) and raw == "" and ctx3.text() == bad
    ctx3.put(ctx3.text().replace(FROM_LINE, "<!--\n" + FROM_LINE + "-->\n" + FROM_LINE, 1))
    before = ctx3.ledger.read_bytes()
    assert ctx3.run(capsys)[0] == 2 and ctx3.ledger.read_bytes() == before


def test_a_hash_mismatch_rule_stops_everything_new_ids_included(ctx3, capsys):
    ctx3.record("BR-LOY-2-1@aaaaaaaa")
    f = ctx3.root / "dod" / "Rules_LOY-1.md"
    f.write_text(f.read_text(encoding="utf-8").replace("Customers earn 1 points per euro.", "Customers earn nothing."), encoding="utf-8")
    before = ctx3.ledger.read_bytes()
    code, _, raw, err = ctx3.run(capsys)
    assert code == 2 and "BR-LOY-1-1" in err and raw == "" and ctx3.ledger.read_bytes() == before


def test_crlf_ledger_keeps_its_terminators_in_the_free_write(ctx3, capsys):
    crlf = FROM_LINE.replace("\n", "\r\n")
    ctx3.put(ctx3.text().replace("\n", "\r\n").replace(crlf, crlf + "- **Rules reviewed:** BR-LOY-2-1@aaaaaaaa\r\n"))
    code, _, _, err = ctx3.run(capsys)
    assert code == 2 and "BR-LOY-1-1" in err
    raw = ctx3.ledger.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n") and ctx3.value() == rec(ctx3, "BR-LOY-1-1", "BR-LOY-1-2", BR_LOY_2_1="aaaaaaaa")


def test_the_refusal_text_is_fixed_and_lists_ids_only():
    assert "{recorded}" in rules.CONFIRM_TERMINAL and "{changed}" in rules.CONFIRM_TERMINAL and "{removed}" in rules.CONFIRM_TERMINAL
    text = rules.confirm_refusal({"added": [f"BR-LOY-1-{n}" for n in range(1, 26)], "changed": ["BR-LOY-2-1"], "removed": []})
    assert "and 5 more" in text and "BR-LOY-1-20" in text and "BR-LOY-1-21" not in text and "Changed since confirmed: BR-LOY-2-1" in text
    assert "Dropped: none" in text and "own terminal" in text and "not a security boundary" in text


def test_a_real_process_without_terminals_records_the_new_ids_and_exits_2(ctx3):
    base = ctx3.text()
    for kw in ({"stdout_pty": False}, {"stdin_pty": False}, {"stdout_pty": False, "stdin_pty": False}):
        ctx3.put(base)
        ctx3.record("BR-LOY-2-1@aaaaaaaa")
        code, screen, err = run_in_pty(CMD, ctx3.root, **kw)
        assert code == 2 and refusal(recorded="BR-LOY-1-1, BR-LOY-1-2", changed="BR-LOY-2-1") in err, kw
        assert "approval code" not in screen and ctx3.value() == rec(ctx3, "BR-LOY-1-1", "BR-LOY-1-2", BR_LOY_2_1="aaaaaaaa"), kw


def test_refresh_after_a_crlf_confirm_keeps_the_line(ctx, capsys):
    ctx.put(ctx.text().replace("\n", "\r\n"))
    assert ctx.run(capsys)[0] == 0
    line = f"- **Rules reviewed:** {ctx.want()}"
    assert main(["dod", "refresh", "LOY-3"], client_factory=chain().client) == 0
    assert line in ctx.text() and ctx.text().count("Rules reviewed") == 1


def test_run_in_pty_kills_a_child_that_waits_forever_within_the_timeout():
    import time
    start = time.monotonic()
    for kw in ({}, {"stdout_pty": False}):
        with pytest.raises(pytest.fail.Exception) as e:
            run_in_pty([], ".", timeout=2, argv=[sys.executable, "-c", "import time; time.sleep(60)"], **kw)
        assert "did not finish within 2 s and was killed" in str(e.value)
    assert time.monotonic() - start < 15
