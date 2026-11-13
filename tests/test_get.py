"""`skj get <n|KEY>` (R3): a safe summary of one issue of the configured project (K4: one project per repo)."""
import json
import os

import pytest

from fakejira import EMAIL, ME_ID, TOKEN, FakeJira
from skj.__main__ import main


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK"}))
    monkeypatch.chdir(tmp_path)
    f = FakeJira()
    story = f.add_issue("SUMMARYTEXT story", description={"version": 1, "type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "DESCTEXT"}]}]})
    f.add_issue("SUMMARYTEXT sub", type="Subtask", parent=story)
    return f


def get(fake, ref):
    return main(["get", ref], client_factory=fake.client)


def test_get_story_by_number_and_key(fake, capsys):
    assert get(fake, "1") == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["key: SPK-1", "id: 10000", "status: To Do (new)", "type: Story", "parent: -",
                                "subtasks: 1"]
    assert get(fake, "SPK-1") == 0 and capsys.readouterr().out == out
    assert [e["path"] for e in fake.log] == ["/issue/SPK-1", "/issue/SPK-1"] and fake.writes() == []


def test_get_subtask_shows_parent(fake, capsys):
    assert get(fake, "SPK-2") == 0
    lines = capsys.readouterr().out.splitlines()
    assert "parent: SPK-1" in lines and "type: Subtask" in lines and "subtasks: 0" in lines


def test_get_output_has_no_text_names_or_ids(fake, capsys):
    get(fake, "1")
    get(fake, "2")
    out, err = capsys.readouterr()
    for s in ("SUMMARYTEXT", "DESCTEXT", "Fictional", EMAIL, TOKEN, ME_ID, "atlassian.net"):
        assert s not in out + err


@pytest.mark.parametrize("ref", ["ABC-1", "spk-1", "SPK-01", "0", "SPK-1;x", " 1"])
def test_get_refuses_foreign_and_bad_refs_before_any_request(fake, capsys, ref):
    assert get(fake, ref) == 2
    assert fake.log == [] and "Traceback" not in capsys.readouterr().err


def test_get_missing_issue_is_an_error_not_a_traceback(fake, capsys):
    assert get(fake, "99") == 1
    assert "Traceback" not in capsys.readouterr().err


def test_get_server_names_are_sanitised(fake, capsys):
    orig = fake._status
    fake._status = lambda name: dict(orig(name), name="Bad\x1b[31m\nName")
    assert get(fake, "1") == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and len(out.splitlines()) == 6


def test_h1a_ac2_server_error_text_is_plain_and_capped(fake, capsys):
    """H1a-AC2: a server message with ESC sequences, a bidi override and 5 kB of text prints as plain text of at most 200 characters."""
    evil = "\x1b[31mred\x1b[0m \x1b]0;title\x07 ‮evil‬ " + "x" * 5000
    fake.inject(500, body={"errorMessages": [evil, "second"], "errors": {"summary": "⁦more⁩"}})
    assert get(fake, "1") == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("skj: HTTP 500: red evil x")
    text = err[len("skj: HTTP 500: "):]
    assert len(text) <= 200 and text.isprintable() and "[31m" not in text and "title" not in text
    assert not any(c in err for c in "\x1b\x07‮‬⁦⁩")


def test_h1a_ac2_a_secret_split_by_an_invisible_character_is_redacted_not_rebuilt(fake, capsys):
    """Fix round 1: the server text is made plain FIRST, then redacted, then cut: removing a ZWSP or a bidi override after the
    redaction would rebuild the very email or token the redactor missed."""
    split_email = EMAIL[:3] + "\u200b" + EMAIL[3:]
    split_token = TOKEN[:5] + "\u200b" + TOKEN[5:9] + "\u202e" + TOKEN[9:]
    fake.inject(500, body={"errorMessages": ["user %s token %s" % (split_email, split_token)], "errors": {}})
    assert get(fake, "1") == 1
    err = capsys.readouterr().err
    assert EMAIL not in err and TOKEN not in err and err.count("[redacted]") == 2


@pytest.mark.parametrize("before", ["\x1b", "\x1b[", "\x1b[12", "\x1b]0;", "\u200b" * 290, "\u200b" * 1590],
                         ids=["esc", "csi", "csi-param", "osc", "zwsp-290", "zwsp-1590"])
def test_h1a_ac2_an_escape_or_a_long_invisible_run_before_a_secret_never_uncovers_it(fake, capsys, before):
    """Fix round 2: the raw text is redacted BEFORE it is made plain (an ESC must not eat a secret's first character, the pre-cut
    must not keep a prefix of one), and once more after; the 200 cut comes last."""
    fake.inject(500, body={"errorMessages": ["bad %s%s and %s%s" % (before, EMAIL, before, TOKEN)], "errors": {}})
    assert get(fake, "1") == 1
    err = capsys.readouterr().err
    for secret in (EMAIL, TOKEN):
        assert not any(secret[i:i + 6] in err for i in range(len(secret) - 5)), (repr(before[:6]), err[:120])


@pytest.mark.parametrize("token", [TOKEN, TOKEN[:5] + "\u200b" + TOKEN[5:]], ids=["plain", "zwsp-split"])
def test_h1a_ac2_a_secret_straddling_the_200_cut_shows_no_fragment(fake, capsys, token):
    """The cut comes after the last redaction: a split token rebuilt by `_plain` must be redacted before anything is cut."""
    for pad in range(185, 201):
        fake.inject(500, body={"errorMessages": ["y" * pad + token], "errors": {}})
        assert get(fake, "1") == 1
        err = capsys.readouterr().err
        assert not any(TOKEN[i:i + 4] in err for i in range(len(TOKEN) - 3)), pad


def test_get_with_a_refused_login_says_authentication_failed(fake, capsys):
    """DF1 (live run A2-28, D-003): a bad identity got `HTTP 404: Issue does not exist ...`. Hand-written live shape."""
    fake.inject(404, headers={"X-Seraph-LoginReason": "AUTHENTICATED_FAILED"},
                body={"errorMessages": ["Issue does not exist or you do not have permission to see it."], "errors": {}})
    assert get(fake, "SPK-1") == 1
    err = capsys.readouterr().err
    assert "authentication failed" in err and "does not exist" not in err and TOKEN not in err and EMAIL not in err
    assert len(fake.log) == 1  # not retried
