"""L2: the open questions of a story and the local verbs of `skj questions` (list, parse `Q<n>` lines, render the comment text).

The `Q<n>` grammar and the rendered comment are the fork source's, run on the same fixture lines. Differences by design: the questions
live in section 2 of the DoD ledger `dod/DoD_<KEY>.md` (as in the fork source: there is no separate questions file), the marker is
`[skj:questions <hash8>]` (W4), and comment ids are STRINGS (an account id is what `_me` returns; there are no integer ids and no GUIDs).
`--post` is W4 and `--sync` is A2 (below, against the fake Jira only). All data is fictional; nothing here touches a network.
"""
import hashlib
import json
import os
import re
import signal
import subprocess
import time

import pytest

from fakejira import OTHER_ID, FakeJira
from skj import adf, client as cl, comments, config, dod, issues, questions
from skj.client import Client
from skj.__main__ import main
from skj.questions import parse_questions

SECTION2 = dod.SECTION2
KEY = "SPK-1"
QS = """- [ ] Q1: Are contractors included? → _(open)_ · owner: PO
- [x] Q2: Which locale? → en-GB only (PO comment 2026-07-01) · owner: PO
-   [ ]  Q3 :  Retention period ->   _(open)_
- [ ] Q4: Export format? → CSV · owner: Dev
- [x] Q5: Cap? → _(open)_ · owner: PO
"""
OPEN2 = "- [ ] Q1: Are contractors <in> scope? → _(open)_ · owner: Dana Fictional\n- [ ] Q3: Retention? → _(open)_\n"
BODY_REST = "Open questions for the product owner:\n\n- Q1: Are contractors <in> scope?\n- Q3: Retention?"


def _ledger(body, tail="## 3. AC\n"):
    return f"# DoD\n{SECTION2}\n- **Approach:** x\n{body}{tail}"


# ---- L2-AC1: parse ---------------------------------------------------------------------------------------------------------------

def test_l2_ac1_parse_open_answered_lenient():
    r = parse_questions(_ledger(QS))
    assert r["open"] == [
        {"id": "Q1", "question": "Are contractors included?", "owner": "PO"},
        {"id": "Q3", "question": "Retention period", "owner": "?"},
        {"id": "Q4", "question": "Export format?", "owner": "Dev"},  # box [ ] with an answer is still open
        {"id": "Q5", "question": "Cap?", "owner": "PO"},            # [x] with _(open)_ is still open
    ]
    assert r["answered"] == [{"id": "Q2", "question": "Which locale?", "owner": "PO",
                              "answer": "en-GB only (PO comment 2026-07-01)"}]


def test_l2_ac1_parse_ignores_fences_comments_and_other_sections():
    body = ("```\n- [ ] Q1: fenced → _(open)_ · owner: PO\n```\n<!-- - [ ] Q2: hidden → _(open)_ -->\n"
            "<!--\n- [ ] Q3: multi → _(open)_\n-->\n- [ ] Q4: real → _(open)_ · owner: PO\n")
    r = parse_questions(_ledger(body, "## 3. AC\n- [ ] Q9: elsewhere → _(open)_\n"))
    assert [q["id"] for q in r["open"]] == ["Q4"] and r["answered"] == []


def test_l2_ac1_parse_no_section2():
    assert parse_questions("nothing") == {"open": [], "answered": [], "unparsed": False}


def test_l2_ac1_the_seeded_ledger_has_a_format_hint_not_a_question():
    md = dod.render({"key": KEY, "id": "10001", "fields": {}}, "- AC one", "example-site.atlassian.net", synced="2026-07-14")
    assert "Format, one line each: `- [ ] Q1:" in md
    assert parse_questions(md) == {"open": [], "answered": [], "unparsed": False}
    assert "_(fill in as they arise)_" in md


def test_l2_ac1_empty_assumed_answer_stays_open():
    led = _ledger("- [x] Q1: a? → assumed: · owner: PO\n- [x] Q2: b? → assumed:   \n- [x] Q3: c? → assumed: y · owner: PO\n")
    out = parse_questions(led)
    assert [q["id"] for q in out["open"]] == ["Q1", "Q2"] and [q["id"] for q in out["answered"]] == ["Q3"]


def test_l2_ac1_unclosed_fence_or_comment_in_section2_is_unreadable():
    for opener in ("```\n", "<!--\n"):
        r = parse_questions(_ledger(opener + "- [ ] Q1: hidden → _(open)_ · owner: PO\n"))
        assert r["unparsed"] is True and r["open"] == [{"id": "?", "question": questions.UNPARSED_NOTE, "owner": "?"}]
    assert parse_questions(_ledger(QS))["unparsed"] is False
    u = parse_questions(_ledger("```\n- [ ] Q1: x → _(open)_\n"))
    assert u["open"][-1]["id"] == "?" and "could not be read" in u["open"][-1]["question"]


def test_l2_ac1_trailing_open_answer_wins_over_box_and_arrow_in_question():
    r = parse_questions(_ledger("- [x] Q1: Does A -> B hold? → _(open)_ · owner: PO\n"))
    assert r["answered"] == [] and r["open"] == [{"id": "Q1", "question": "Does A -> B hold?", "owner": "PO"}]


def test_l2_ac1_arrow_split_rule_last_unicode_else_first_ascii():
    r = parse_questions(_ledger("- [x] Q1: Retention? → 30 days -> confirmed by PO · owner: PO\n"
                                "- [ ] Q2: Cap -> per user? -> _(open)_ · owner: PO\n"))
    assert r["answered"] == [{"id": "Q1", "question": "Retention?", "owner": "PO", "answer": "30 days -> confirmed by PO"}]
    assert r["open"] == [{"id": "Q2", "question": "Cap", "owner": "PO"}]  # first ASCII arrow, answer `per user? -> _(open)_`


def test_l2_ac1_duplicate_ids_are_listed_once_first_wins():
    r = parse_questions(_ledger("- [ ] Q2: a → _(open)_\n- [ ] Q1: b → _(open)_\n- [ ] Q2: c → _(open)_\n"))
    assert [q["id"] for q in r["open"]] == ["Q2", "Q1"] and r["open"][0]["question"] == "a"


def test_l2_ac1_hidden_section2_heading_and_questions_are_ignored():
    fenced = f"# DoD\n{SECTION2}\n- [ ] Q1: real → _(open)_ · owner: PO\n```\n## 3. AC\n- [ ] Q2: shown? → _(open)_\n```\n"
    assert [q["id"] for q in parse_questions(fenced)["open"]] == ["Q1"]


def test_l2_ac1_a_forged_section2_heading_in_section_1_loses_to_the_real_one():
    md = f"# DoD\n## 1. Seeded\n{SECTION2}\n- [ ] Q7: forged → _(open)_\n{SECTION2}\n- [ ] Q1: real → _(open)_\n## 3. AC\n"
    assert [q["id"] for q in parse_questions(md)["open"]] == ["Q1"]


# ---- a PR-controlled ledger: no regex may backtrack over a long line ------------------------------------------------------------

def _timed(md):
    t0 = time.process_time()
    r = parse_questions(md)
    return r, time.process_time() - t0


@pytest.mark.parametrize("line", ["- [ ] Q1: a" + " " * (1 << 20) + "b",
                                  "- [ ] Q1: a" + " " * (1 << 20) + "·x",
                                  "- [ ] Q1: a → b" + "\t" * (1 << 20) + "· owner: PO"])
def test_l2_ac1_a_blank_run_line_is_unparsed_in_linear_time(line):
    r, dt = _timed(_ledger(line + "\n- [ ] Q2: fine → _(open)_\n"))
    assert dt < 2, dt
    assert r["unparsed"] is True and [q["id"] for q in r["open"]] == ["Q2", "?"]  # never silently "no question"


def test_l2_ac1_a_question_line_is_valid_up_to_2000_characters():
    def line(n):
        head, tail = "- [ ] Q1: ", " → _(open)_ · owner: PO"
        return head + "x" * (n - len(head) - len(tail)) + tail
    ok = parse_questions(_ledger(line(questions.MAX_LEDGER_LINE) + "\n"))
    assert questions.MAX_LEDGER_LINE == 2000 and [q["id"] for q in ok["open"]] == ["Q1"] and ok["unparsed"] is False
    over = parse_questions(_ledger(line(2001) + "\n"))
    assert over["unparsed"] is True and [q["id"] for q in over["open"]] == ["?"]


def test_l2_ac1_a_long_line_that_is_not_a_question_is_just_text():
    r, dt = _timed(_ledger("prose " * 200_000 + "\n" + " " * (1 << 20) + "\n"))
    assert dt < 2 and r == {"open": [], "answered": [], "unparsed": False}


def _blank_lines(n_lines=500):
    """Lines of about 1990 characters whose cost is a long run of blanks, in several shapes (all under MAX_LEDGER_LINE)."""
    def pad(head, tail, blank):
        return head + blank * (1990 - len(head) - len(tail)) + tail
    shapes = [pad("- [ ] Q1: a", "b", " "), pad("- [ ] Q1: a", "·x", " "), pad("- [ ] Q1: a → b", "· owner: PO", "\t"),
              pad("- [ ] Q1: a", "b", "\u00a0"), pad("- [x] Q1: a", "→ answer", " "), pad("- [ ] Q1: a", "-> b", " "),
              pad("- [ ] Q1: a → b", "· owner:", " "), pad("- [x] Q1: a → assumed", ": · owner: PO", " ")]
    return [shapes[i % len(shapes)] for i in range(n_lines)]


def test_l2_ac1_many_blank_run_lines_cost_linear_time_in_total():
    lines = _blank_lines()
    assert max(map(len, lines)) <= questions.MAX_LEDGER_LINE and sum(map(len, lines)) > 900_000  # fits the 1 MiB ledger cap
    r, dt = _timed(_ledger("\n".join(lines) + "\n"))
    assert dt < 2, dt
    assert r["unparsed"] is False and len(r["open"]) == 1 and r["answered"]  # open duplicates are listed once, every line was read


# the fork source's regexes, kept as the oracle the linear parser must agree with on short lines
_OLD_LINE = re.compile(r"^\s*(?:[-*+]\s+)?\[([ xX])\]\s*Q(\d+)\s*:\s*(.*?)\s*$")
_OLD_OWNER = re.compile(r"\s*[·|]\s*owner\s*:\s*([^·|]*?)\s*$", re.I)
_OLD_EMPTY = re.compile(r"assumed\s*:\s*$", re.I)


def _old_parse(ledger_md):
    from skj.dod import _visible, section
    out = {"open": [], "answered": []}
    body, out["unparsed"] = _visible(section(ledger_md, 2) or "")
    for line in body.splitlines():
        m = _OLD_LINE.match(line)
        if not m:
            continue
        rest, owner = m.group(3), "?"
        if o := _OLD_OWNER.search(rest):
            rest, owner = rest[:o.start()], o.group(1) or "?"
        arrows = list(re.finditer(r"\s*→\s*", rest)) or list(re.finditer(r"\s*->\s*", rest))[:1]
        q, a = (rest[:arrows[-1].start()], rest[arrows[-1].end():]) if arrows else (rest, "")
        q, a = q.strip(), a.strip()
        done = m.group(1) in "xX" and a not in ("", "_(open)_") and not _OLD_EMPTY.match(a)
        item = {"id": "Q" + m.group(2), "question": q, "owner": owner}
        out["answered" if done else "open"].append({**item, "answer": a} if done else item)
    first = {}
    for q in out["open"]:
        first.setdefault(q["id"], q)
    out["open"] = list(first.values())
    if out["unparsed"]:
        out["open"].append({"id": "?", "question": questions.UNPARSED_NOTE, "owner": "?"})
    return out


def _generated_lines(n=300, seed=7):
    import random
    rnd = random.Random(seed)
    ws = [" ", "", "  ", "\t", "\u00a0", "\u2003", "\u3000", "\x1f", " \t ", "\u200b"]  # \u200b is not whitespace
    bits = ["Retention?", "Are contractors in?", "x", "30 days", "→", "->", "→", "·", "|", "owner: PO", "Owner :Dev", "owner:", "OWNER : ",
            "assumed:", "assumed: y", "_(open)_", "a->b", "·owner:Dev", "|owner: x · y"]
    for i in range(n):
        head = rnd.choice(["- ", "* ", "+ ", "", "  - ", "\t- "]) + rnd.choice(["[ ]", "[x]", "[X]", "[ ]"]) + rnd.choice(ws)
        body = "".join(rnd.choice(ws) + rnd.choice(bits) for _ in range(rnd.randint(1, 8))) + rnd.choice(ws)
        yield head + "Q%d" % rnd.randint(1, 4) + rnd.choice(ws) + ":" + rnd.choice(ws) + body


def test_l2_ac1_the_linear_parser_agrees_with_the_fork_regexes_on_short_lines():
    lines = list(_generated_lines())
    lines += [x for x in QS.splitlines()] + ["- [ ] Q1: a → b → c · owner: PO", "- [x] Q2: a -> b -> c · owner:  ", "- [ ] Q3: | owner: x"]
    assert all(len(x) <= 200 for x in lines)
    mixed = {"open": 0, "answered": 0}
    for line in lines:  # one line per ledger: every line is compared on its own, nothing hides behind a duplicate id
        md = _ledger(line + "\n")
        new = parse_questions(md)
        assert new == _old_parse(md), repr(line)
        mixed["open"] += bool(new["open"])
        mixed["answered"] += bool(new["answered"])
    assert mixed["open"] > 20 and mixed["answered"] > 20  # the sample really reaches both outcomes


# ---- L2-AC1: render --------------------------------------------------------------------------------------------------------------

def test_l2_ac1_comment_never_names_owners_and_leaves_out_the_unparsed_note():
    parsed = parse_questions(_ledger(OPEN2))
    md = questions.body(parsed["open"])[0]
    assert md.split("\n\n", 1)[1] == BODY_REST
    assert "Dana" not in md and "owner: " not in md
    with pytest.raises(ValueError, match="no open questions"):
        questions.body([{"id": "?", "question": "unparsed", "owner": "?"}])


def test_l2_ac1_assumed_lists_only_assumption_answers():
    parsed = parse_questions(_ledger(OPEN2 + "- [x] Q4: Archived too? → assumed: archived are excluded · owner: PO\n"
                                     "- [x] Q5: Cap? → 50 per page · owner: PO\n"))
    assert [q["id"] for q in questions.assumed(parsed["answered"])] == ["Q4"]
    assert "Q4" not in questions.body(parsed["open"])[0] and "Q5" not in questions.body(parsed["open"])[0]  # answered: never posted


def test_l2_ac1_a_long_question_line_is_capped_and_marked():
    parsed = parse_questions(_ledger(f"- [ ] Q1: {'x' * 600} → _(open)_\n"))
    md = questions.body(parsed["open"])[0]
    line = next(x for x in md.splitlines() if x.startswith("- Q1: "))
    assert len(line) == questions.MAX_LINE + 2 and line.endswith("…") and line.startswith("- Q1: " + "x" * 10)
    assert questions.cut(parsed["open"]) == ["Q1"] and questions.cut(parse_questions(_ledger(OPEN2))["open"]) == []


# ---- L2-AC2: ids are strings -----------------------------------------------------------------------------------------------------

def _me(**kw):
    fake = FakeJira(**kw)
    return fake, fake.client()


def test_l2_ac2_me_is_the_account_id_string_from_issues_me():
    fake, c = _me()
    got = questions._me(c)
    assert type(got) is str and got == fake.account_id == issues.me(fake.client())["accountId"]
    assert [e["path"] for e in fake.log] == ["/myself", "/myself"] and not any(e["write"] for e in fake.log)


@pytest.mark.parametrize("kw", [{"account_type": "app"}, {"account_type": ""}])
def test_l2_ac2_me_refuses_a_non_user_identity_without_echoing_it(kw):
    _, c = _me(**kw)
    with pytest.raises(ValueError, match="cannot tell which Jira comments are yours.*nothing posted") as e:
        questions._me(c)
    assert "app" not in str(e.value)


def test_l2_ac2_me_turns_a_client_error_into_a_fixed_refusal():
    fake, c = _me()
    fake.inject(401, body={"errorMessages": ["fake: secret server text"], "errors": {}})
    with pytest.raises(ValueError, match="cannot tell which Jira comments are yours.*nothing posted") as e:
        questions._me(c)
    assert "secret" not in str(e.value)
    assert not isinstance(e.value, cl.ClientError)


@pytest.mark.parametrize("data", [None, [], {}, {"accountType": "atlassian", "accountId": 5, "active": True},
                                  {"accountType": "atlassian", "accountId": "a b", "active": True}])
def test_l2_ac2_me_refuses_a_malformed_answer(data):
    class C:
        def request(self, *a, **k):
            return cl.Response(200, {}, data)
    with pytest.raises(ValueError, match="nothing posted"):
        questions._me(C())


def test_l2_ac2_no_integer_comment_id_survives_in_the_module():
    src = open(questions.__file__, encoding="utf-8").read()
    for gone in ("connectionData", "descriptor", "is_anonymous", "comment_id"):
        assert gone not in src, gone
    assert not re.search(r"\bint\(", src)


# ---- the CLI ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo with a config, cwd inside it; returns its top level."""
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    for k in ("SKJ_EMAIL", "SKJ_TOKEN"):  # a local verb never reads an identity
        monkeypatch.delenv(k, raising=False)
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, stdin=subprocess.DEVNULL)
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK"}))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _led(repo, body=None, key=KEY):
    f = repo / "dod" / ("DoD_%s.md" % key)
    if body is not None:
        f.parent.mkdir(exist_ok=True)
        f.write_text(_ledger(body), encoding="utf-8")
    return f


def run(*args):
    return main(["questions", *args])


def test_l2_ac1_cli_json_lists_open_and_answered(repo, capsys):
    led = _led(repo, QS)
    before = led.read_bytes()
    assert run(KEY) == 0
    out = json.loads(capsys.readouterr().out)
    assert [q["id"] for q in out["open"]] == ["Q1", "Q3", "Q4", "Q5"] and out["answered"][0]["id"] == "Q2"
    assert out["unparsed"] is False and led.read_bytes() == before  # read-only
    assert all(type(q["id"]) is str for k in ("open", "answered") for q in out[k])
    assert run("1") == 0  # a bare number names the same ledger
    assert json.loads(capsys.readouterr().out) == out


def test_missing_ledger_is_an_error_that_names_the_seed_command(repo, capsys):
    assert run(KEY) == 1
    assert "run `skj dod seed SPK-1` first" in capsys.readouterr().err
    assert not (repo / "dod").exists()


def test_the_ledger_name_never_comes_from_unvalidated_input(repo, capsys):
    _led(repo, QS)
    for ref in ("OTHER-1", "spk-1", "SPK-0", "SPK-01", "../x", "SPK-1;touch x", "0", "-1", "1.5", "SPK-１"):
        assert run(ref) == 2, ref
    assert capsys.readouterr().out == ""


def test_the_ledger_is_never_read_through_a_symlink(repo, capsys):
    (repo / "dod").mkdir()
    (repo / "elsewhere.md").write_text(_ledger(QS))
    (repo / "dod" / "DoD_SPK-1.md").symlink_to(repo / "elsewhere.md")
    assert run(KEY) == 1 and "symbolic link" in capsys.readouterr().err
    (repo / "dod" / "DoD_SPK-1.md").unlink()
    (repo / "dod" / "DoD_SPK-1.md").mkdir()
    assert run(KEY) == 1


def test_a_ledger_over_1_mib_is_refused_and_the_cap_counts_bytes(repo, capsys):
    led = _led(repo, QS)
    led.write_text(_ledger(QS) + "x" * (1 << 20))
    assert run(KEY) == 1 and "1 MiB" in capsys.readouterr().err
    body = _ledger(QS) + "é" * ((1 << 19) - 100)  # fewer than 1 Mi characters, a little over 1 MiB in bytes
    led.write_text(body, encoding="utf-8")
    assert len(body) < (1 << 20) < len(body.encode("utf-8"))
    assert run(KEY) == 1 and "1 MiB" in capsys.readouterr().err


def test_an_unreadable_ledger_is_refused_without_its_text(repo, capsys):
    led = _led(repo, QS)
    led.write_bytes(b"\xff\xfe")
    assert run(KEY) == 1 and "cannot read the DoD ledger" in capsys.readouterr().err


def test_local_verbs_never_build_a_client_or_read_an_identity(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a local verb built a client")
    monkeypatch.setattr("skj.__main__.default_client", boom)
    monkeypatch.setattr("skj.secretstore.get", boom)
    _led(repo, QS)
    assert main(["questions", KEY]) == 0


# ==== W4: `questions --post` ======================================================================================================

W4_QS = ("- [ ] Q1: Are contractors included? → _(open)_ · owner: PO\n"
         "- [x] Q2: Which locale? → en-GB only · owner: PO\n"
         "- [ ] Q3: Retention period? → _(open)_ · owner: Dev\n")


@pytest.fixture
def fake():
    f = FakeJira()
    assert f.add_issue("A fictional story") == KEY
    return f


def post(fake, *args, client=None):
    return main(["questions", KEY, "--post", *args], client_factory=client or fake.client)


def _quoted_body(out):
    """The comment body as the plan quotes it."""
    lines = out.split("Comment body (ADF as Markdown, exactly what is sent):\n\n", 1)[1].split("\n")
    body = []
    for x in lines:
        if not x.startswith(">"):
            break
        body.append(x[2:] if x.startswith("> ") else "")
    return "\n".join(body)


def _posts(fake):
    return [e for e in fake.log if e["method"] != "GET"]


def _own(fake):
    return [c for c in fake.comments.values() if c["author"]["accountId"] == fake.account_id]


def test_post_preview_matches_body(repo, fake, capsys):
    """W4-AC1: the dry run sends nothing and quotes the exact ADF-as-Markdown body; the apply sends that ADF (its Markdown is the
    preview), first line the marker whose hash8 is sha256 of the canonical Markdown after it."""
    led = _led(repo, W4_QS)
    before = led.read_bytes()
    assert post(fake) == 0
    out = capsys.readouterr().out
    assert _posts(fake) == [] and led.read_bytes() == before and "dry run: nothing posted" in out
    preview = _quoted_body(out)
    md, doc, h = questions.body(parse_questions(led.read_text())["open"])
    assert preview == md == adf.adf_to_md(doc, strict=True)
    rest = md.split("\n\n", 1)[1]
    assert md.split("\n", 1)[0] == "[skj:questions %s]" % h and h == hashlib.sha256(rest.encode()).hexdigest()[:8]
    assert rest == "Open questions for the product owner:\n\n- Q1: Are contractors included?\n- Q3: Retention period?"
    assert [e["method"] for e in fake.log] == ["GET"] * 4  # the issue, the identity, the comments, the probe past the end
    assert fake.log[0]["path"] == "/issue/" + KEY and fake.log[1]["path"] == "/myself"
    assert post(fake, "--apply") == 0
    assert [(e["method"], e["path"]) for e in _posts(fake)] == [("POST", "/issue/%s/comment" % KEY)]
    (c,) = fake.comments.values()
    assert adf.adf_to_md(c["body"], strict=True) == preview and c["body"] == doc and c["visibility"] is None


def test_the_plan_is_deterministic_and_holds_no_account_id(repo, fake, capsys):
    _led(repo, W4_QS)
    fake.add_comment(KEY, adf.md_to_adf("[skj:questions 00000000]\n\nx"), author=fake.user(OTHER_ID))
    assert post(fake) == 0
    a = capsys.readouterr().out
    assert post(fake) == 0
    b = capsys.readouterr().out
    assert a == b
    for secret in (fake.account_id, OTHER_ID, fake.email, fake.token, fake.account_id[:8], OTHER_ID[:8]):
        assert secret not in a


def test_repeat_post_is_noop(repo, fake, capsys):
    """W4-AC2: the second apply finds its own marker comment (same hash, same body, own author) and posts nothing."""
    _led(repo, W4_QS)
    assert post(fake, "--apply") == 0
    capsys.readouterr()
    assert post(fake, "--apply") == 0
    out = capsys.readouterr().out
    assert len(_posts(fake)) == 1 and len(fake.comments) == 1
    (cid,) = fake.comments
    assert "action: none (comment %s already shows these questions)" % cid in out and "ledger: already recorded" in out
    assert "Comment body" not in out


def test_a_changed_open_set_posts_a_new_comment_and_never_touches_the_old_one(repo, fake, capsys):
    led = _led(repo, W4_QS)
    assert post(fake, "--apply") == 0
    (first,) = fake.comments
    old = json.dumps(fake.comments[first], sort_keys=True)
    led.write_text(led.read_text().replace("- [ ] Q3: Retention period? → _(open)_ · owner: Dev\n",
                                           "- [x] Q3: Retention period? → 30 days · owner: Dev\n"))
    assert post(fake, "--apply") == 0
    assert [e["method"] for e in _posts(fake)] == ["POST", "POST"] and len(fake.comments) == 2
    assert json.dumps(fake.comments[first], sort_keys=True) == old
    second = sorted(fake.comments)[-1]  # ids are digit strings of one length here: the newer one sorts last
    assert "Q3" not in adf.adf_to_md(fake.comments[second]["body"]) and questions.recorded(led.read_text())[0] == second


def _timeout_on_post(fake, applied):
    fake.fail_transport(on=fake._n + 5, applied=applied)  # issue, identity, comments, probe, then the POST


def test_timeout_then_rerun_finds_marker(repo, fake, capsys):
    """W4-AC3 / Review Focus 6: the POST times out after Jira applied it: the run re-reads, reports it found, records the id, exits
    non-zero, and posts nothing again; the next run is a no-op."""
    led = _led(repo, W4_QS)
    _timeout_on_post(fake, applied=True)
    assert post(fake, "--apply") == 1
    out = capsys.readouterr().out
    (cid,) = fake.comments
    assert "posted (found on re-read): comment %s" % cid in out
    assert [e["method"] for e in _posts(fake)] == ["POST"] and questions.recorded(led.read_text())[0] == cid
    assert [e["method"] for e in fake.log[5:]] == ["GET", "GET"]  # a fresh read of the comments and the probe, not a cache
    assert post(fake, "--apply") == 0
    assert len(_posts(fake)) == 1 and "already shows these questions" in capsys.readouterr().out


def test_timeout_before_jira_applied_it_is_not_posted_again_in_that_run(repo, fake, capsys):
    led = _led(repo, W4_QS)
    before = led.read_bytes()
    _timeout_on_post(fake, applied=False)
    assert post(fake, "--apply") == 1
    out = capsys.readouterr().out
    assert "write uncertain: not found on re-read; not posted again in this run" in out
    assert len(_posts(fake)) == 1 and fake.comments == {} and led.read_bytes() == before
    assert post(fake, "--apply") == 0  # the next run's re-read decides: one comment
    assert len(_posts(fake)) == 2 and len(fake.comments) == 1


def test_a_failed_re_read_after_a_timeout_posts_nothing_more(repo, fake, capsys):
    _led(repo, W4_QS)
    _timeout_on_post(fake, applied=True)
    fake.fail_transport(on=fake._n + 6, applied=False)  # the re-read's first GET
    assert post(fake, "--apply") == 1
    assert "the re-read failed; not posted again in this run" in capsys.readouterr().out and len(_posts(fake)) == 1


def _client_answering_post(fake, answer):
    """A client whose comment POST gets `answer` (status, body) instead of the fake's own handling; everything else is the fake."""
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
    led = _led(repo, W4_QS)
    before = led.read_bytes()
    c = _client_answering_post(fake, (status, {"errorMessages": ["fake: secret server text"], "errors": {}}))
    assert post(fake, "--apply", client=lambda: c) == 1
    cap = capsys.readouterr()
    assert "failed: Jira answered HTTP %d; nothing posted" % status in cap.out
    assert "secret" not in cap.out + cap.err and led.read_bytes() == before and len(_posts(fake)) == 1


def test_a_5xx_or_an_unreadable_2xx_is_uncertain_and_re_read(repo, fake, capsys):
    _led(repo, W4_QS)
    c = _client_answering_post(fake, (502, {"errorMessages": ["fake: secret"]}))
    assert post(fake, "--apply", client=lambda: c) == 1
    assert "write uncertain: not found on re-read" in capsys.readouterr().out
    c = _client_answering_post(fake, "apply-then-201-garbage")
    assert post(fake, "--apply", client=lambda: c) == 1
    (cid,) = fake.comments
    assert "posted (found on re-read): comment %s" % cid in capsys.readouterr().out


def test_posted_id_recorded_as_string(repo, fake, capsys):
    """W4-AC4: the comment id Jira gave is written in section 2 as a string; nothing else in the ledger changes."""
    led = _led(repo, W4_QS)
    before = led.read_text()
    assert post(fake, "--apply") == 0
    (cid,) = fake.comments
    after = led.read_text()
    rec = questions.recorded(after)
    h = questions.body(parse_questions(before)["open"])[2]
    assert rec == (cid, h) and all(type(x) is str for x in rec)
    assert "- **Questions posted:** %s:%s\n" % (cid, h) in after
    assert parse_questions(after) == parse_questions(before)
    assert [x for x in after.splitlines() if x not in before.splitlines()] == ["- **Questions posted:** %s:%s" % (cid, h)]
    assert dod.section(after, 2).rstrip("\n").endswith("- **Questions posted:** %s:%s" % (cid, h))
    assert "ledger: recorded - **Questions posted:** %s:%s" % (cid, h) in capsys.readouterr().out


def test_a_noop_records_an_id_the_ledger_lacks_and_rewrites_an_old_record(repo, fake, capsys):
    led = _led(repo, W4_QS)
    assert post(fake, "--apply") == 0
    (cid,) = fake.comments
    h = questions.recorded(led.read_text())[1]
    led.write_text(led.read_text().replace("- **Questions posted:** %s:%s" % (cid, h), "- **Questions posted:** 999:%s" % h))
    assert post(fake) == 0 and "ledger: record `- **Questions posted:** %s:%s` in section 2" % (cid, h) in capsys.readouterr().out
    assert post(fake, "--apply") == 0
    assert questions.recorded(led.read_text()) == (cid, h) and len(_posts(fake)) == 1


def test_record_text_keeps_crlf_and_every_other_byte():
    md = _ledger("- [ ] Q1: a? → _(open)_\n").replace("\n", "\r\n")
    new = questions.record_text(md, "10001", "0a1b2c3d")
    assert new.replace("- **Questions posted:** 10001:0a1b2c3d\r\n", "") == md
    again = questions.record_text(new, "10002", "0a1b2c3e")
    assert again == new.replace("10001:0a1b2c3d", "10002:0a1b2c3e")
    with pytest.raises(ValueError, match="not a comment id string"):
        questions.record_text(md, 10001, "0a1b2c3d")  # an int is never an id
    for bad in ("01", "0", "1" * 13, "1\n", "١٢"):
        with pytest.raises(ValueError):
            questions.record_text(md, bad, "0a1b2c3d")


# ---- the trust rules of the own-comment search --------------------------------------------------------------------------------------

def _planned(repo):
    return questions.body(parse_questions(_led(repo).read_text())["open"])


def test_a_forged_marker_by_another_account_never_suppresses_the_post(repo, fake, capsys):
    _led(repo, W4_QS)
    md, doc, h = _planned(repo)
    other = fake.add_comment(KEY, doc, author=fake.user(OTHER_ID))  # the exact planned comment, by someone else
    app = fake.add_comment(KEY, doc, author=dict(fake.user(fake.account_id), accountType="app"))  # our id, but not a user account
    assert post(fake) == 0
    out = capsys.readouterr().out
    assert "action: post ONE new comment" in out
    assert "note: 2 marker comment(s) by another account ignored" in out and other in out and app in out
    assert OTHER_ID not in out and fake.account_id not in out
    assert post(fake, "--apply") == 0 and len(_posts(fake)) == 1


def test_an_edited_own_comment_no_longer_counts_unless_it_is_exactly_the_planned_text(repo, fake, capsys):
    led = _led(repo, W4_QS)
    assert post(fake, "--apply") == 0
    (cid,) = fake.comments
    md = adf.adf_to_md(fake.comments[cid]["body"])
    fake.edit_comment(cid, adf.md_to_adf(md.replace("Retention period?", "Something else?")), editor=fake._me())
    capsys.readouterr()
    assert post(fake) == 0
    out = capsys.readouterr().out
    assert "action: post ONE new comment" in out and "no longer matches the marker (edited) ignored: %s" % cid in out
    fake.edit_comment(cid, adf.md_to_adf(md), editor=fake.user(OTHER_ID))  # edited back to exactly the planned text
    assert post(fake) == 0 and "already shows these questions" in capsys.readouterr().out
    assert questions.recorded(led.read_text())[0] == cid


def test_a_same_hash_different_body_does_not_count(repo, fake, capsys, monkeypatch):
    """The hash8 is a 32-bit label: the body must equal the planned one byte for byte (a crafted collision proves nothing)."""
    _led(repo, W4_QS)
    md, doc, h = _planned(repo)
    monkeypatch.setattr(questions, "_hash8", lambda s: h)  # every body collides with the planned hash
    forged = "[skj:questions %s]\n\nOpen questions for the product owner:\n\n- Q1: Nothing to see here" % h
    rows = [comments.Row("10001", fake.account_id, "atlassian", fake.account_id, "t", "t", None, forged, (), "")]
    assert questions._scan(rows, fake.account_id, md) == (None, questions._report())
    monkeypatch.undo()
    rows = [rows[0]._replace(md=md)]
    assert questions._scan(rows, fake.account_id, md)[0] == "10001"
    assert questions._scan(rows, OTHER_ID, md)[0] is None
    assert questions._scan([rows[0]._replace(author=None)], None, md)[0] is None
    assert questions._scan([rows[0]._replace(author_type=None)], fake.account_id, md)[0] is None


def test_restricted_or_unreadable_own_comments_do_not_count(repo, fake, capsys):
    _led(repo, W4_QS)
    md, doc, h = _planned(repo)
    vis = fake.add_comment(KEY, doc, visibility={"type": "role", "value": "Developers"})
    plain = fake.add_comment(KEY, {"version": 1, "type": "doc", "content": [  # readable from its first character, no marker
        {"type": "paragraph", "content": [{"type": "text", "text": "a screenshot:"}]}, {"type": "mediaSingle", "content": []}]})
    odd = json.loads(json.dumps(doc))
    odd["content"].append({"type": "mediaSingle", "content": []})
    fake.add_comment(KEY, odd, author=fake.user(OTHER_ID))  # someone else's unreadable marker comment: ignored
    assert post(fake) == 0
    out = capsys.readouterr().out
    assert "action: post ONE new comment" in out and "visibility restriction ignored: %s" % vis in out
    assert "own comment(s) the kit cannot read (no questions marker) ignored: %s" % plain in out


def test_an_unreadable_own_marker_comment_refuses_the_post(repo, fake, capsys):
    """A server-side rewrite of the kit's own comment into something the strict reader rejects must not make every apply post a
    duplicate: refuse while such a comment exists (live shape UNVERIFIED, LV2-AC1)."""
    _led(repo, W4_QS)
    md, doc, h = _planned(repo)
    odd = json.loads(json.dumps(doc))
    odd["content"].append({"type": "mediaSingle", "content": []})
    cid = fake.add_comment(KEY, odd)
    assert post(fake, "--apply") == 1
    assert capsys.readouterr().err == "skj: %s\n" % (questions.E_UNREADABLE % cid)
    assert _posts(fake) == [] and fake.log[-1]["path"] == "/issue/%s/comment/%s" % (KEY, cid)


def _unreadable_own(fake, first=None):
    content = ([first] if first else []) + [{"type": "mediaSingle", "content": []}]
    return fake.add_comment(KEY, {"version": 1, "type": "doc", "content": content})


def _deep(n):
    v = []
    for _ in range(n):
        v = [v]
    return v


def _answering_inspection(fake, answer):
    """A client whose GET of one comment (the inspection) gets `answer`: (status, body), "timeout", or a function of the real body."""
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


@pytest.mark.parametrize("answer", [
    lambda d: {k: v for k, v in d.items() if k != "body"},          # no body
    lambda d: dict(d, body="[skj:questions 0a1b2c3d]"),              # a string body
    lambda d: dict(d, body=[d["body"]]),                             # a list body
    lambda d: dict(d, id="10999"),                                   # another comment's answer
    lambda d: dict(d, body={"version": 1, "type": "doc", "content": "x"}),  # renders as a placeholder
    lambda d: "not an object",
    lambda d: dict(d, body={"version": 1, "type": "doc"}),                 # empty: contradicts the unknown nodes just listed
    lambda d: dict(d, body={"version": 1, "type": "doc", "content": []}),
    lambda d: dict(d, body={"version": 1, "type": "doc", "content": _deep(150)}),  # too deep: the reader raises
    (500, {"errorMessages": ["fake: down"]}), (404, {"errorMessages": []}), "timeout",
], ids=["no-body", "str-body", "list-body", "other-id", "odd-doc", "not-object", "no-content", "empty-content", "too-deep", "5xx", "404", "timeout"])
def test_an_inspection_that_is_not_definitive_refuses_the_post(repo, fake, capsys, answer):
    _led(repo, W4_QS)
    cid = _unreadable_own(fake, {"type": "paragraph", "content": [{"type": "text", "text": "a screenshot:"}]})
    c = _answering_inspection(fake, answer)
    assert post(fake, "--apply", client=lambda: c) == 1
    assert capsys.readouterr().err == "skj: %s\n" % (questions.E_UNREADABLE % cid) and _posts(fake) == []


def test_an_unknown_node_before_the_marker_counts_as_marked(repo, fake, capsys):
    _led(repo, W4_QS)
    md, doc, h = _planned(repo)
    odd = json.loads(json.dumps(doc))
    odd["content"].insert(0, {"type": "mediaSingle", "content": []})
    cid = fake.add_comment(KEY, odd)
    assert post(fake, "--apply") == 1 and capsys.readouterr().err == "skj: %s\n" % (questions.E_UNREADABLE % cid)
    assert _posts(fake) == []


def test_inspections_stop_at_the_first_refusal_and_are_capped(repo, fake, capsys):
    _led(repo, W4_QS)
    first = _unreadable_own(fake)  # renders `[?mediaSingle]` first: refuses
    for _ in range(5):
        _unreadable_own(fake)
    assert post(fake, "--apply") == 1 and (questions.E_UNREADABLE % first) in capsys.readouterr().err
    assert sum("/comment/" in e["path"] for e in fake.log) == 1  # short-circuit: one inspection GET
    f2 = FakeJira()
    f2.add_issue("A fictional story")
    for _ in range(questions.MAX_INSPECT + 1):
        _unreadable_own(f2, {"type": "paragraph", "content": [{"type": "text", "text": "fine"}]})
    assert post(f2, "--apply") == 1 and capsys.readouterr().err == "skj: %s\n" % questions.E_TOO_MANY
    assert not any("/comment/" in e["path"] for e in f2.log) and _posts(f2) == []
    f3 = FakeJira()
    f3.add_issue("A fictional story")
    for _ in range(questions.MAX_INSPECT):
        _unreadable_own(f3, {"type": "paragraph", "content": [{"type": "text", "text": "fine"}]})
    assert post(f3, "--apply") == 0 and sum("/comment/" in e["path"] for e in f3.log) == questions.MAX_INSPECT
    assert len(_posts(f3)) == 1


def test_a_truncated_or_inconsistent_comment_list_refuses_to_post(repo, fake, capsys, monkeypatch):
    _led(repo, W4_QS)
    md, doc, h = _planned(repo)
    fake.add_comment(KEY, adf.md_to_adf("first"))
    fake.add_comment(KEY, doc)  # our comment, on what the lying total says is past the end
    real = fake._comment_page

    def short_total(issue, start, size):
        page = real(issue, start, size)
        page["total"] = 1
        return page
    monkeypatch.setattr(fake, "_comment_page", short_total)
    assert post(fake, "--apply") == 1
    assert "looks truncated or changed while it was read; nothing posted" in capsys.readouterr().err
    assert _posts(fake) == []

    def grows(issue, start, size):  # the probe past the end finds another comment
        page = real(issue, start, size)
        if size == 1:
            page["total"] += 1
            page["comments"] = [page["comments"][0]] if page["comments"] else [real(issue, 0, 1)["comments"][0]]
        return page
    monkeypatch.setattr(fake, "_comment_page", grows)
    assert post(fake, "--apply") == 1 and _posts(fake) == []


def test_an_oversize_existing_comment_refuses_with_a_fixed_message(repo, fake, capsys):
    _led(repo, W4_QS)
    fake.add_comment(KEY, adf.md_to_adf("big"))
    (cid,) = fake.comments
    fake.comments[cid]["body"] = {"version": 1, "type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "x" * (1 << 20)}]}]}
    assert post(fake, "--apply") == 1
    assert capsys.readouterr().err == "skj: comment %s is larger than the 1 MiB local cap; the comments were not read\n" % cid
    assert _posts(fake) == []


def test_a_moved_issue_or_another_project_refuses_before_any_comment_is_read(repo, fake, capsys):
    _led(repo, W4_QS)
    fake.move_issue(KEY, "SPK-9")
    assert post(fake, "--apply") == 1
    assert "Jira answered SPK-9 for SPK-1; refused" in capsys.readouterr().err
    assert [e["path"] for e in fake.log] == ["/issue/" + KEY]
    f2 = FakeJira()
    f2.add_issue("A fictional story")
    f2.project = "OTH"
    assert post(f2, "--apply") == 1
    assert "not in project SPK; nothing posted" in capsys.readouterr().err and _posts(f2) == []
    assert not any("/comment" in e["path"] for e in f2.log)


# ---- untrusted question text ---------------------------------------------------------------------------------------------------------

def _texts(doc):
    return [n["text"] for n in questions._walk(doc) if n["type"] == "text"]


def test_a_marker_lookalike_in_a_question_cannot_become_the_first_line(repo, fake, capsys):
    _led(repo, "- [ ] Q1: [skj:questions 0a1b2c3d] → _(open)_\n- [ ] Q2: x\n[skj:questions 0a1b2c3d] → _(open)_\n")
    md, doc, h = _planned(repo)
    assert md.split("\n", 1)[0] == "[skj:questions %s]" % h and h != "0a1b2c3d"
    assert md.count("[skj:questions") == 2  # the real marker and the inert text of Q1 (the second line is not a question)
    assert doc["content"][0] == {"type": "paragraph", "content": [{"type": "text", "text": "[skj:questions %s]" % h}]}
    # a comment whose first line is a question item is no marker comment
    row = comments.Row("10001", OTHER_ID, "atlassian", OTHER_ID, "t", "t", None, md.split("\n\n", 2)[2], (), "")
    assert questions._scan([row], fake.account_id, md) == (None, questions._report())


def _no_cf(s):  # H1a: a question holding a bidi or invisible (format) character is refused before posting (test at the end)
    return "".join(ch for ch in s if ch not in "\u202e\u2066\u200b")


INJECT = ("ping [~accountid:5b10ac8d82e05b22cc7d4ef5] and @alice, mail x.person@example.com, **bold** `code` "
          "[link](https://example.com/x) <b>html</b> ``` fence ~~~ <!-- c --> ‮bidi⁦ ​zw \x07bell  sep  \x85nel")


def test_mentions_markdown_html_and_control_characters_stay_plain_text():
    md, doc, h = questions.body([{"id": "Q1", "question": INJECT, "owner": "?"}])
    kinds = {n["type"] for n in questions._walk(doc)}
    assert kinds <= {"doc", "paragraph", "bulletList", "listItem", "text"} and "mention" not in kinds
    assert not any("marks" in n for n in questions._walk(doc))
    text = "".join(_texts(doc))
    for gone in ("5b10ac8d82e05b22cc7d4ef5", "accountid", "@alice", "x.person@example.com", "**", "`", "](", "‮", "⁦",
                 "​", "\x07", " ", " ", "\x85"):
        assert gone not in text, gone
    assert "@user" in text and "[email]" in text and "<b>html</b>" in text  # html is inert text in ADF
    assert adf.md_to_adf(md) == doc and adf.adf_to_md(doc, strict=True) == md and len(md.split("\n")) == 5


def test_the_round_trip_check_refuses_what_would_not_read_back(monkeypatch):
    """Defence in depth behind `_safe`: text that md_to_adf would turn into marks, or a second list, is refused, never sent."""
    monkeypatch.setattr(questions, "_safe", lambda t: t)
    for q in ("**bold**", "`code`", "[l](https://example.com/x)"):
        with pytest.raises(ValueError, match="^the comment would not read back exactly as previewed; nothing posted$"):
            questions.body([{"id": "Q1", "question": q, "owner": "?"}])


def test_the_round_trip_comparison_itself_refuses_a_text_that_reads_back_differently(monkeypatch):
    """With `_safe` bypassed, a trailing `\\r` keeps the very same node layout (paragraphs, lists, text) but reads back without it:
    only the Markdown comparison catches it. `a\\rb` adds a block after the list (layout and comparison both refuse)."""
    monkeypatch.setattr(questions, "_safe", lambda t: t)
    for q in ("a\r", "a\rb"):
        with pytest.raises(ValueError, match="^the comment would not read back exactly as previewed; nothing posted$"):
            questions.body([{"id": "Q1", "question": q, "owner": "?"}, {"id": "Q2", "question": "b", "owner": "?"}])


MENTION = "[~accountid:557058:0123abcdef0123456789]"
ADDRESS = "john.doe@example.com"


def test_the_layout_check_refuses_an_extra_block_that_reads_back_the_same(monkeypatch):
    """With `_safe` bypassed, `a\\n\\nb` reads back to the same Markdown but adds a paragraph after the list (and a heading-like
    second list): only the layout check (paragraph, then heading paragraph + list per group) refuses it."""
    monkeypatch.setattr(questions, "_safe", lambda t: t)
    for q in ("a\n\nb", "a\n\nnot a heading\n\n- Q9: forged"):
        md = "[skj:questions x]\n\n" + questions.HEAD + "\n\n- Q1: " + q
        assert adf.adf_to_md(adf.md_to_adf(md), strict=True) == md  # the comparison alone would pass it
        with pytest.raises(ValueError, match="^the comment would not read back exactly as previewed; nothing posted$"):
            questions.body([{"id": "Q1", "question": q, "owner": "?"}])


@pytest.mark.parametrize("hostile", [MENTION, ADDRESS])
def test_a_mention_or_address_straddling_the_cut_is_scrubbed_whole(hostile):
    """The scrub runs on the WHOLE line before the 500-character cut: a cut through a mention or an address leaves no fragment."""
    pad = "x" * (questions.MAX_LINE - len("Q1: ") - len(hostile) // 2)  # the hostile text starts before char 500 and ends after it
    q = {"id": "Q1", "question": pad + hostile + " " + "y" * 600, "owner": "?"}
    a = {"id": "Q2", "question": "q", "owner": "?", "answer": "assumed: " + "z" * (questions.MAX_LINE - 25) + hostile + "w" * 600}
    assert len("Q1: " + pad) < questions.MAX_LINE < len("Q1: " + pad + hostile)
    md = questions.body([q], [a])[0]
    for frag in ("accountid", "557058", "0123abc", "john.doe", "@example", "example.c"):
        assert frag not in md, frag
    assert questions.cut([q, a]) == ["Q1", "Q2"] and all(len(x) <= questions.MAX_LINE + 2 for x in md.split("\n"))


@pytest.mark.parametrize("open_qs,assumed_qs,layout", [
    ([{"id": "Q1", "question": "o?"}], [], ["paragraph", "paragraph", "bulletList"]),
    ([], [{"id": "Q2", "question": "a?", "answer": "assumed: yes"}], ["paragraph", "paragraph", "bulletList"]),
    ([{"id": "Q1", "question": "o?"}], [{"id": "Q2", "question": "a?", "answer": "assumed: yes"}],
     ["paragraph", "paragraph", "bulletList", "paragraph", "bulletList"]),
], ids=["open-only", "assumed-only", "both"])
def test_the_fork_layout_round_trips(open_qs, assumed_qs, layout):
    md, doc, h = questions.body(open_qs, assumed_qs)
    assert [n["type"] for n in doc["content"]] == layout and adf.md_to_adf(md) == doc and adf.adf_to_md(doc, strict=True) == md
    rest = md.split("\n\n", 1)[1]
    assert h == hashlib.sha256(rest.encode()).hexdigest()[:8]
    assert (questions.HEAD in md) == bool(open_qs) and (questions.ASSUMED_HEAD in md) == bool(assumed_qs)
    if assumed_qs:
        assert md.endswith(questions.ASSUMED_HEAD + "\n\n- Q2: a? → assumed: yes")
    with pytest.raises(ValueError, match="nothing to post"):
        questions.body([], [])


def test_postable_dedupes_assumed_and_keeps_an_open_id_open():
    parsed = parse_questions(_ledger("- [ ] Q1: open? → _(open)_\n- [x] Q1: open? → assumed: x\n- [x] Q2: a? → assumed: first\n"
                                     "- [x] Q2: a? → assumed: second\n- [x] Q3: b? → 30 days\n- [x] Q4: c? → Assumed: caps\n"))
    qs, ass = questions.postable(parsed)
    assert [q["id"] for q in qs] == ["Q1"] and [(q["id"], q["answer"]) for q in ass] == [("Q2", "assumed: first"), ("Q4", "Assumed: caps")]


ASSUMED_LEDGER = "- [x] Q1: Which locale? → en-GB only · owner: PO\n- [x] Q2: Archived too? → assumed: archived are excluded · owner: PO\n"


def test_assumed_answers_alone_are_posted_and_a_repeat_is_a_no_op(repo, fake, capsys):
    _led(repo, ASSUMED_LEDGER)
    assert post(fake, "--apply") == 0
    (c,) = fake.comments.values()
    md = adf.adf_to_md(c["body"], strict=True)
    assert md.split("\n\n", 1)[1] == questions.ASSUMED_HEAD + "\n\n- Q2: Archived too? → assumed: archived are excluded"
    assert questions.HEAD not in md and "Q1" not in md
    assert post(fake, "--apply") == 0 and len(_posts(fake)) == 1 and "already shows these questions" in capsys.readouterr().out


def test_open_and_assumed_post_once_and_a_changed_assumption_posts_a_new_comment(repo, fake, capsys):
    led = _led(repo, W4_QS + "- [x] Q4: Archived? → assumed: no · owner: PO\n")
    assert post(fake, "--apply") == 0 and post(fake, "--apply") == 0
    assert len(_posts(fake)) == 1
    (first,) = fake.comments
    old = json.dumps(fake.comments[first], sort_keys=True)
    led.write_text(led.read_text().replace("assumed: no", "assumed: yes, for a year"))
    assert post(fake, "--apply") == 0
    assert [e["method"] for e in _posts(fake)] == ["POST", "POST"] and json.dumps(fake.comments[first], sort_keys=True) == old
    new = sorted(fake.comments)[-1]
    assert adf.adf_to_md(fake.comments[new]["body"]).endswith("- Q4: Archived? → assumed: yes, for a year")
    assert questions.recorded(led.read_text())[0] == new


def test_hostile_text_in_an_assumed_answer_stays_plain_text(repo, fake, capsys):
    _led(repo, "- [x] Q1: q? → assumed: %s [skj:questions 0a1b2c3d] · owner: PO\n" % _no_cf(INJECT.split("\u2028")[0]))
    assert post(fake, "--apply") == 0
    (c,) = fake.comments.values()
    kinds = [n["type"] for n in questions._walk(c["body"])]
    assert set(kinds) <= {"doc", "paragraph", "bulletList", "listItem", "text"} and kinds.count("text") == 3
    text = "".join(_texts(c["body"]))
    for gone in ("5b10ac8d82e05b22cc7d4ef5", "@alice", "x.person@example.com", "**", "`", "](", "\u202e", "\u2066"):
        assert gone not in text, gone
    assert adf.adf_to_md(c["body"]).split("\n", 1)[0] != "[skj:questions 0a1b2c3d]"


def test_the_ledger_form_of_the_injection_posts_one_plain_item(repo, fake, capsys):
    _led(repo, "- [ ] Q1: %s → _(open)_\n" % _no_cf(INJECT.split(" ")[0]))
    assert post(fake, "--apply") == 0
    (c,) = fake.comments.values()
    items = c["body"]["content"][2]["content"]
    assert len(items) == 1 and [n["type"] for n in questions._walk(c["body"])].count("text") == 3


def test_an_oversize_comment_is_refused_before_any_request(repo, fake, capsys):
    _led(repo, "".join("- [ ] Q%d: %s → _(open)_\n" % (i, "y" * 480) for i in range(1, 80)))
    assert post(fake, "--apply") == 1
    assert capsys.readouterr().err == "skj: %s\n" % questions.E_SIZE and fake.log == []
    with pytest.raises(ValueError, match="32 KiB"):  # a scrub that grows the text is caught on the ADF bytes too
        questions.body([{"id": "Q%d" % i, "question": "*" * 480, "owner": "?"} for i in range(1, 30)])


def test_closed_questions_and_the_unparsed_note_are_not_posted(repo, fake, capsys):
    _led(repo, W4_QS + "- [x] Q4: Archived? → assumed: no · owner: PO\n")
    assert post(fake, "--apply") == 0
    (c,) = fake.comments.values()
    md = adf.adf_to_md(c["body"])
    assert "Q1" in md and "Q3" in md and "Q2" not in md  # a human's answer is not posted
    assert md.endswith("\n\n%s\n\n- Q4: Archived? → assumed: no" % questions.ASSUMED_HEAD)  # the team's assumption is
    _led(repo, W4_QS + "```\n- [ ] Q5: hidden → _(open)_\n")
    assert post(fake, "--apply") == 1 and "unclosed code fence" in capsys.readouterr().err and len(_posts(fake)) == 1


def test_nothing_open_and_nothing_assumed_is_a_no_op_without_a_request(repo, fake, capsys):
    led = _led(repo, "- [x] Q1: a? → yes · owner: PO\n")
    assert post(fake, "--apply") == 0
    assert "action: none (no open questions; nothing to post)" in capsys.readouterr().out and fake.log == []
    led.unlink()
    assert post(fake, "--apply") == 1 and "run `skj dod seed SPK-1` first" in capsys.readouterr().err and fake.log == []
    for flags in (["--apply"], ["--plan-out", "p.md"]):
        assert main(["questions", KEY, *flags], client_factory=fake.client) == 2
        assert "need --post" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["questions", KEY, "--post", "--if-any"], client_factory=fake.client)  # not a flag of this verb (W5 owns it)
    assert fake.log == [] and not (repo / "p.md").exists()


def test_the_post_is_gated_by_expect_plan(repo, fake, capsys):
    led = _led(repo, W4_QS)
    assert post(fake, "--plan-out", "p.md") == 0
    led.write_text(led.read_text().replace("Retention period?", "Retention period, really?"))
    assert post(fake, "--apply", "--expect-plan", "p.md") == 2
    assert _posts(fake) == [] and (repo / "p.md.new").exists()
    assert post(fake, "--plan-out", "p.md") == 0 and post(fake, "--apply", "--expect-plan", "p.md") == 0
    assert len(_posts(fake)) == 1


def test_questions_post_is_wired_through_the_framework():
    import skj.__main__ as m
    ps = [x for x in m._all_parsers(m.build_parser()) if x.get_default("write_verb") == "questions --post"]
    assert len(ps) == 1 and {"--apply", "--plan-out", "--expect-plan", "--post"} <= {
        s for a in ps[0]._actions for s in a.option_strings}


def test_there_is_no_comment_edit_or_delete_and_no_visibility_in_0_1():
    src = open(questions.__file__, encoding="utf-8").read()
    assert '"PUT"' not in src and '"DELETE"' not in src and "properties" not in src.replace("no properties", "")
    assert [x.strip() for x in src.splitlines() if "/comment/" in x] == ['d = c.request("GET", "/issue/%s/comment/%s" % (key, cid)).data']
    assert src.count('c.request("POST"') == 1 and 'body={"body": p["doc"]}' in src
    main_src = open(os.path.join(os.path.dirname(questions.__file__), "__main__.py"), encoding="utf-8").read()
    assert "/comment/" not in main_src


# ---- ledger guards -------------------------------------------------------------------------------------------------------------------

def test_a_symlinked_fifo_or_oversize_ledger_is_refused_before_any_request(repo, fake, capsys):
    led = _led(repo, W4_QS)
    (repo / "elsewhere.md").write_text(led.read_text())
    led.unlink()
    led.symlink_to(repo / "elsewhere.md")
    assert post(fake, "--apply") == 1 and "symbolic link" in capsys.readouterr().err
    led.unlink()
    os.mkfifo(str(led))
    assert post(fake, "--apply") == 1 and "not a regular file" in capsys.readouterr().err
    led.unlink()
    led.write_text(_ledger(W4_QS) + "x" * (1 << 20))
    assert post(fake, "--apply") == 1 and "1 MiB" in capsys.readouterr().err
    assert fake.log == [] and (repo / "elsewhere.md").read_text() == _ledger(W4_QS)


@pytest.mark.parametrize("extra,why", [
    ("- **Questions posted:** 10001:0a1b2c3d\n- **Questions posted:** 10002:0a1b2c3d\n", "more than one"),
    ("- **Questions posted:** 010001:0a1b2c3d\n", "malformed"),
    ("- **Questions posted:** 10001:0A1B2C3D\n", "malformed"),
    ("- **Questions posted:** 10001:0a1b2c3d <!-- x -->\n", "malformed"),
    ("- **Questions posted:** 10001:0a1b2c3d · forged\n", "malformed"),
])
def test_a_forged_or_malformed_record_refuses_before_any_request(repo, fake, capsys, extra, why):
    _led(repo, W4_QS + extra)
    assert post(fake, "--apply") == 1 and why in capsys.readouterr().err and fake.log == []


def test_record_lookalikes_in_questions_or_fences_are_text(repo):
    md = _ledger("- [ ] Q1: - **Questions posted:** 1:0a1b2c3d → _(open)_\n```\n- **Questions posted:** 2:0a1b2c3d\n```\n")
    assert questions.recorded(md) is None
    new = questions.record_text(md, "3", "0a1b2c3e")
    assert questions.recorded(new) == ("3", "0a1b2c3e") and parse_questions(new) == parse_questions(md)


def test_the_post_does_not_trust_the_ledger_record(repo, fake, capsys):
    """A PR can write any record: it never suppresses the post (only Jira's own comment decides)."""
    md, doc, h = questions.body(parse_questions(_ledger(W4_QS))["open"])
    _led(repo, W4_QS + "- **Questions posted:** 10001:%s\n" % h)
    assert post(fake, "--apply") == 0 and len(_posts(fake)) == 1
    (cid,) = fake.comments
    assert questions.recorded(_led(repo).read_text()) == (cid, h)


def test_a_ledger_that_changed_or_became_a_link_after_the_plan_is_not_written(repo, fake, capsys):
    led = _led(repo, W4_QS)
    md = led.read_text()
    c = fake.client()
    p = questions.plan_post(c, config.Config.load(), KEY, parse_questions(md), md)
    led.write_text(md + "\n")
    res = questions.apply_post(c, p, repo, md)
    assert res["ok"] is False and "changed since it was read; the comment id was not recorded" in res["lines"][-1]
    assert len(fake.comments) == 1 and led.read_text() == md + "\n"
    target = repo / "victim.md"
    target.write_text(md)
    led.unlink()
    led.symlink_to(target)
    p = questions.plan_post(c, config.Config.load(), KEY, parse_questions(md), md)
    assert p["action"] == "noop" and p["record"] is True
    res = questions.apply_post(c, p, repo, md)
    assert res["ok"] is False and "symbolic link" in res["lines"][-1] and target.read_text() == md
    assert len(_posts(fake)) == 1


# ---- linear time over a PR-controlled ledger --------------------------------------------------------------------------------------------

_BOUND = 2.0
needs_timer = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="no setitimer on this platform")


class _TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise _TooSlow()


def _w4_timed(fn, *args):
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


def _pipeline(md):
    """Everything the post path does to the ledger text before a request: parse, record, body (escape, hash, ADF, round trip)."""
    questions.recorded(md)
    questions.body(*questions.postable(parse_questions(md)))
    questions.record_text(md, "10001", "0a1b2c3d")


def _w4_shapes():
    mib = 1 << 20
    pad = lambda head, n=1990: head + " " * (n - len(head))  # noqa: E731
    return {
        "one-mib-line": _ledger("- [ ] Q1: " + "[~" * (mib // 2 - 60) + "\n"),
        "record-lookalike-mib": _ledger("- **Questions posted:** " + " " * (mib - 100) + "\n"),
        "500x1990-blank": _ledger("".join(pad("- [ ] Q%d: a" % i) + "b\n" for i in range(500))),
        "500x1990-mentions": _ledger("".join("- [ ] Q%d: " % i + "[~" * 990 + "\n" for i in range(500))),
        "500x1990-records": _ledger("".join(pad("- **Questions posted:**") + "\n" for _ in range(500))),
        "20000-short": _ledger("".join("- [ ] Q%d: [~@a*`](\n" % i for i in range(20000))),
        "500x1990-assumed-mentions": _ledger("".join("- [x] Q%d: q → assumed: " % i + "[~" * 980 + "\n" for i in range(500))),
        "20000-at": _ledger("".join("- [ ] Q1: a@b.c@d.e@\n" for _ in range(20000))),
    }


@needs_timer
@pytest.mark.parametrize("shape", sorted(_w4_shapes()))
def test_the_post_path_is_linear_on_adversarial_ledgers(shape):
    md = _w4_shapes()[shape]
    assert len(md.encode("utf-8")) <= questions.MAX_LEDGER
    assert _w4_timed(_pipeline, md) < _BOUND


@needs_timer
def test_the_body_escape_is_linear_at_the_cap():
    qs = [{"id": "Q%d" % i, "question": "[~" * 240 + "@x.y@" * 10, "owner": "?"} for i in range(60)]
    assert _w4_timed(questions.body, qs) < _BOUND


# ==== A2: `questions --sync` (EXPERIMENTAL comment approval, tamper-evident only; against the fake Jira only) =================
# An approver's `/answer Q<n>: text`, newer than the kit's newest own `[skj:questions <hash8>]` posting that shows that open question as
# the ledger holds it now, becomes `- [x] Q<n>: <question> → <answer> (PO comment <id>) · owner: PO`. A local write of the ledger only.
from fakejira import ME_ID
from skj import approvals
from test_decisions import APPROVER, BASE, COMMENT_MODE, _target


@pytest.fixture
def cmq(repo):
    _target(repo, COMMENT_MODE)
    return repo


def _qpost(fake, md):
    """The kit posts the open questions and assumed answers of ledger `md` as W4 does; the comment id."""
    qs, assumed_qs = questions.postable(questions.parse_questions(md))
    return fake.add_comment(KEY, questions.body(qs, assumed_qs)[1])


def _ask(fake, text, who=APPROVER, **kw):
    return fake.add_comment(KEY, adf.md_to_adf(text), author=fake.user(who), **kw)


def qsync(fake, *args, client=None):
    return main(["questions", KEY, "--sync", *args], client_factory=client or fake.client)


def _qout(capsys):
    cap = capsys.readouterr()
    return json.loads(cap.out), cap.err


def _qline(led, qid):
    return next(x for x in led.read_text(encoding="utf-8").splitlines() if (" %s:" % qid) in x)


def test_sync_answer_from_approver(cmq, fake, capsys):
    """The answer lands on that question's line only, with its provenance; every other byte is unchanged; GETs only."""
    led = _led(cmq, W4_QS)
    md = led.read_text(encoding="utf-8")
    _qpost(fake, md)
    a = _ask(fake, "/answer Q1: yes, contractors are in scope")
    assert qsync(fake) == 0
    out, err = _qout(capsys)
    assert _qline(led, "Q1") == "- [x] Q1: Are contractors included? → yes, contractors are in scope (PO comment %s) · owner: PO" % a
    new = led.read_text(encoding="utf-8")
    assert [x for x in new.splitlines() if " Q1:" not in x] == [x for x in md.splitlines() if " Q1:" not in x]
    assert out["answered"] == ["Q1"] and out["open"] == ["Q3"] and out["skipped"] == {"Q3": "no-answer"}
    assert questions.parse_questions(new)["answered"][0] == {"id": "Q1", "question": "Are contractors included?", "owner": "PO",
                                                               "answer": "yes, contractors are in scope (PO comment %s)" % a}
    assert {e["method"] for e in fake.log} == {"GET"} and fake.writes() == []
    for s in (fake.account_id, APPROVER, fake.email, fake.token, APPROVER[:8], fake.account_id[:8]):
        assert s not in json.dumps(out) + err
    assert qsync(fake) == 0 and led.read_text(encoding="utf-8") == new  # an answer is never replaced
    capsys.readouterr()
    _ask(fake, "/answer Q1: actually no")
    assert qsync(fake) == 0 and _qout(capsys)[0]["ignored"] == ["Q1"] and led.read_text(encoding="utf-8") == new


def test_sync_answer_needs_a_newer_unedited_own_posting_that_shows_the_line(cmq, fake, capsys):
    led = _led(cmq, W4_QS)
    md = led.read_text(encoding="utf-8")
    _ask(fake, "/answer Q1: early")  # before any posting
    assert qsync(fake) == 0 and _qout(capsys)[0]["skipped"]["Q1"] == "not-posted"
    post = _qpost(fake, md)
    assert qsync(fake) == 0 and _qout(capsys)[0]["skipped"]["Q1"] == "older-than-posting"
    _ask(fake, "/answer Q1: late")
    fake.edit_comment(post, fake.comments[post]["body"], editor=fake._me())
    assert qsync(fake) == 0 and _qout(capsys)[0]["skipped"]["Q1"] == "posting-edited" and led.read_text(encoding="utf-8") == md
    fake.comments[post]["updated"] = fake.comments[post]["created"]
    fake.comments[post]["updateAuthor"] = fake._me()
    led.write_text(md.replace("Are contractors included?", "Are contractors excluded?"), encoding="utf-8")  # the line changed
    assert qsync(fake) == 0 and _qout(capsys)[0]["skipped"]["Q1"] == "not-posted"
    led.write_text(md, encoding="utf-8")
    fake.comments[post]["visibility"] = {"type": "role", "value": "Developers"}
    assert qsync(fake) == 0 and _qout(capsys)[0]["skipped"]["Q1"] == "posting-restricted"
    fake.comments[post]["visibility"] = None
    assert qsync(fake) == 0 and _qout(capsys)[0]["answered"] == ["Q1"]


def test_sync_answer_a_reworded_repost_after_the_answer_voids_it(cmq, fake, capsys):
    led = _led(cmq, W4_QS)
    md = led.read_text(encoding="utf-8")
    _qpost(fake, md)
    _ask(fake, "/answer Q1: yes")
    md2 = md.replace("Are contractors included?", "Are interns included?")
    _qpost(fake, md2)  # the question changed after the answer
    led.write_text(md2, encoding="utf-8")
    assert qsync(fake) == 0 and _qout(capsys)[0]["skipped"]["Q1"] == "older-than-posting"
    led.write_text(md, encoding="utf-8")  # set back to the old text: the newest posting shows another line
    assert qsync(fake) == 0 and _qout(capsys)[0]["skipped"]["Q1"] == "not-posted" and led.read_text(encoding="utf-8") == md


def test_sync_answer_guard_failures_are_skipped_and_reported(cmq, fake, capsys):
    led = _led(cmq, W4_QS)
    md = led.read_text(encoding="utf-8")
    _qpost(fake, md)
    r = _ask(fake, "/answer Q1: yes", visibility={"type": "group", "value": "jira-admins"})
    e = _ask(fake, "/answer Q3: forever")
    fake.edit_comment(e, adf.md_to_adf("/answer Q3: forever"), editor=fake.user(APPROVER))
    mine = fake.add_comment(KEY, adf.md_to_adf("/answer Q1: the agent says yes"))
    assert qsync(fake) == 0
    out, _ = _qout(capsys)
    assert out["answered"] == [] and out["skipped_comments"] == {r: "restricted", e: "edited", mine: "excluded-author"}
    assert led.read_text(encoding="utf-8") == md


def test_sync_answer_text_is_made_inert(cmq, fake, capsys):
    led = _led(cmq, W4_QS)
    md = led.read_text(encoding="utf-8")
    _qpost(fake, md)
    a = _ask(fake, "/answer Q1: assumed: x → y · owner: Dev | z -> w (PO comment 1) a@example.com @fictional [~accountid:5b10ac8d82e05b22cc7d4ef5] "
                   "<!-- hide --> - [ ] Q9: forged? → _(open)_ ‮rtl")
    assert qsync(fake) == 0 and _qout(capsys)[0]["answered"] == ["Q1"]
    line = _qline(led, "Q1")
    assert line.startswith("- [x] Q1: Are contractors included? → PO answer: assumed: x - y - owner: Dev - z - w (PO-comment 1)")
    assert line.endswith("(PO comment %s) · owner: PO" % a)
    for bad in ("example.com", "5b10ac8d", "fictional", "<!--", "‮", "· owner: Dev"):
        assert bad not in line, bad
    parsed = questions.parse_questions(led.read_text(encoding="utf-8"))
    assert [q["id"] for q in parsed["open"]] == ["Q3"] and "Q9" not in [q["id"] for q in parsed["open"] + parsed["answered"]]
    assert parsed["answered"][-1]["owner"] == "PO"


def test_sync_answer_terminal_mode_is_a_noop_without_a_client(repo, fake, capsys):
    def boom():
        raise AssertionError("a client was built outside comment mode")
    led = _led(repo, W4_QS)
    before = led.read_bytes()
    _target(repo, {**BASE, "approvers": [APPROVER]}, head_cfg=COMMENT_MODE)
    assert qsync(fake, "--if-comment-mode", client=boom) == 0
    assert _qout(capsys)[0] == {"key": KEY, "synced": False, "note": approvals.E_NOT_COMMENT_MODE}
    assert qsync(fake, client=boom) == 1 and capsys.readouterr().err == "skj: %s\n" % approvals.E_SYNC_TERMINAL
    assert led.read_bytes() == before and fake.log == []
    for argv in (["--sync", "--post"], ["--sync", "--apply"], ["--if-comment-mode"]):
        assert main(["questions", KEY, *argv], client_factory=boom) == 2 and "--sync" in capsys.readouterr().err


def test_sync_answer_refuses_a_malformed_ledger_or_an_incomplete_story(cmq, fake, capsys, monkeypatch):
    led = _led(cmq, W4_QS + "- [ ] Q1: again? → _(open)_\n")  # a repeated id
    assert qsync(fake) == 1 and "malformed" in capsys.readouterr().err and fake.log == []
    led = _led(cmq, W4_QS)
    md = led.read_text(encoding="utf-8")
    _qpost(fake, md)
    _ask(fake, "/answer Q1: yes")
    real = fake._comment_page

    def short_total(issue, start, size):
        page = real(issue, start, size)
        page["total"] = 1
        return page
    monkeypatch.setattr(fake, "_comment_page", short_total)
    assert qsync(fake) == 1 and "truncated" in capsys.readouterr().err and led.read_text(encoding="utf-8") == md


def test_sync_answer_keeps_crlf(cmq, fake, capsys):
    led = _led(cmq, W4_QS)
    md = led.read_text(encoding="utf-8").replace("\n", "\r\n")
    led.write_bytes(md.encode("utf-8"))
    _qpost(fake, md)
    _ask(fake, "/answer Q3: seven years")
    assert qsync(fake) == 0
    new = led.read_bytes().decode("utf-8")
    a, b = md.splitlines(keepends=True), new.splitlines(keepends=True)
    assert len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1 and new.count("\r\n") == md.count("\r\n")


def test_sync_answer_never_overwrites_text_a_person_wrote_and_skips_a_cut_line(cmq, fake, capsys):
    led = _led(cmq, W4_QS.replace("Retention period? → _(open)_", "Retention period? → maybe 7y") + "- [ ] Q7: " + "y" * 600 + " → _(open)_\n")
    md = led.read_text(encoding="utf-8")
    _qpost(fake, md)
    _ask(fake, "/answer Q3: seven years")
    _ask(fake, "/answer Q7: fine")
    assert qsync(fake) == 0
    out, _ = _qout(capsys)
    assert out["skipped"]["Q3"] == "has-answer" and out["skipped"]["Q7"] == "posting-lossy" and led.read_text(encoding="utf-8") == md


def test_sync_answer_an_assumed_listing_or_an_unreadable_own_comment_is_not_the_open_posting(cmq, fake, capsys):
    led = _led(cmq, W4_QS)
    md = led.read_text(encoding="utf-8")
    _qpost(fake, md)
    fake.add_comment(KEY, {"version": 1, "type": "doc", "content": [{"type": "mysteryNode"}]})  # own and unreadable: could be a posting
    _ask(fake, "/answer Q1: yes")
    assert qsync(fake) == 0 and _qout(capsys)[0]["skipped"]["Q1"] == "posting-superseded" and led.read_text(encoding="utf-8") == md
    f2 = FakeJira()
    f2.add_issue("A fictional story")
    assumed_md = md.replace("- [ ] Q1: Are contractors included? → _(open)_", "- [x] Q1: Are contractors included? → assumed: yes")
    _qpost(f2, assumed_md)  # the newest posting shows Q1 as the team's assumption, not open
    _ask(f2, "/answer Q1: yes")
    assert qsync(f2) == 0 and _qout(capsys)[0]["skipped"]["Q1"] == "not-posted"


@pytest.mark.parametrize("how", ["edit", "visibility"])
def test_sync_answer_a_tampered_newer_answer_blocks_the_older_one(cmq, fake, capsys, how):
    led = _led(cmq, W4_QS)
    md = led.read_text(encoding="utf-8")
    _qpost(fake, md)
    _ask(fake, "/answer Q1: yes")
    later = _ask(fake, "/answer Q1: no, never")
    if how == "edit":
        fake.edit_comment(later, fake.comments[later]["body"], editor=fake._me())
    else:
        fake.comments[later]["visibility"] = {"type": "role", "value": "Developers"}
    assert qsync(fake) == 0
    out, err = _qout(capsys)
    assert out["skipped"]["Q1"] == "newer-tampered" and out["tampered"] == ["Q1"] and "approval-newer-tampered: Q1" in err
    assert led.read_text(encoding="utf-8") == md


# ---- A4: comment mode needs the approver's `/approve <questions gate> <hash of this plan>` -----------------------------------------

def _a4_comment_mode(repo, approver="5b10ac8d82e05b22cc7d4ef5"):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, stdin=subprocess.DEVNULL, env=env)
    (repo / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK", "approvers": [approver],
                                                      "approval": "comment"}))
    git("add", "skj.config.json")
    git("commit", "-q", "-m", "target")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return approver


def test_a4_questions_post_in_comment_mode_needs_the_approval_of_this_plan(repo, fake, capsys):
    from fakejira import ME_ID
    from skj import approvals
    approver = _a4_comment_mode(repo)
    _led(repo, W4_QS)
    assert post(fake, "--plan-out", "p.md") == 0  # the preview works in comment mode
    data = (repo / "p.md").read_bytes()
    assert post(fake, "--apply", "--expect-plan", "p.md") == 2 and _posts(fake) == []
    assert capsys.readouterr().err.splitlines()[-1].startswith("skj: comment approval: no valid approval for this plan on story SPK-1.")
    say = lambda t, who=approver: fake.add_comment(KEY, adf.md_to_adf(t), author=fake.user(who))  # noqa: E731
    say("/approve approve-questions %s" % approvals.gate_hash("approve-questions", KEY, data), ME_ID)  # the agent's: never counts
    assert post(fake, "--apply", "--expect-plan", "p.md") == 2 and _posts(fake) == []
    say("/approve approve-plan-questions %s" % approvals.gate_hash("approve-plan-questions", KEY, data))  # any gate of the verb
    assert post(fake, "--apply", "--expect-plan", "p.md") == 0
    assert [(e["method"], e["path"]) for e in _posts(fake)] == [("POST", "/issue/%s/comment" % KEY)]


def test_a4_questions_post_refuses_an_approval_of_an_older_plan(repo, fake, capsys):
    from skj import approvals
    approver = _a4_comment_mode(repo)
    led = _led(repo, W4_QS)
    assert post(fake, "--plan-out", "p.md") == 0
    h = approvals.gate_hash("approve-questions", KEY, (repo / "p.md").read_bytes())
    fake.add_comment(KEY, adf.md_to_adf("/approve approve-questions %s" % h), author=fake.user(approver))
    led.write_text(led.read_text().replace("Are contractors included?", "Are contractors in scope?"))
    assert post(fake, "--apply") == 2 and _posts(fake) == []


# ---- H1a-AC3: a Q number is ASCII digits without a leading zero; bidi text is refused before posting -----------------------------------

@pytest.mark.parametrize("num", ["٣", "01", "0", "３"])
def test_h1a_ac3_a_q_number_is_ascii_digits_without_a_leading_zero(num):
    r = questions.parse_questions(_ledger("- [ ] Q%s: Is it? → _(open)_ · owner: PO\n" % num))
    assert r["unparsed"] is True and all(q["id"] != "Q3" and q["id"] != "Q" + num for q in r["open"])
    assert questions.parse_questions(_ledger("- [ ] Q30: Is it? → _(open)_\n"))["open"][0]["id"] == "Q30"


def test_h1a_ac3_post_refuses_a_question_with_a_bidi_override_before_any_request(repo, fake, capsys):
    _led(repo, "- [ ] Q1: Are ‮contractors included? → _(open)_ · owner: PO\n")
    for argv in ((), ("--apply",)):
        assert post(fake, *argv) == 1
        assert "bidi" in capsys.readouterr().err
    assert fake.log == []
