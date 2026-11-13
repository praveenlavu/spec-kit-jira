"""P2: `skj pr link KEY --url URL [--title T] [--apply]` - link on merge without any app: one remote link (`globalId` = the PR URL) and
one kit comment `[skj:pr <hash8>]`, behind the X2 gate. Against the fake Jira only (no network); all data fictional.

The fork's link step is specific to its own tracker (artifact links on the work item); only its shape is kept: plan, gate, one write each, never
retried, re-read after an uncertain write. The comment machinery is W4's (questions._own_comment, _compose, _recheck), imported."""
import json
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

from fakejira import OTHER_ID, FakeJira
from skj import adf, gate, prlink, questions
from skj.client import Client
from skj.__main__ import main

KEY = "SPK-1"
URL = "https://github.com/example-org/example-repo/pull/12"
TITLE = "SPK-1 Add the fictional export"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    monkeypatch.delenv("GH_HOST", raising=False)
    for k in ("SKJ_EMAIL", "SKJ_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, stdin=subprocess.DEVNULL)
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK"}))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def fake():
    f = FakeJira()
    assert f.add_issue("A fictional story") == KEY
    return f


def link(fake, *args, url=URL, title=TITLE, client=None):
    argv = ["pr", "link", KEY, "--url", url] + (["--title", title] if title is not None else []) + list(args)
    return main(argv, client_factory=client or (lambda: fake.client(sleep=lambda s: None)))


def _writes(fake):
    return [(e["method"], e["path"]) for e in fake.log if e["method"] != "GET"]


def _own(fake):
    return [c for c in fake.comments.values() if c["author"]["accountId"] == fake.account_id]


def _links(fake):
    return fake.issue(KEY)["links"]


READS = [("GET", "/issue/" + KEY), ("GET", "/myself"), ("GET", "/issue/%s/comment" % KEY), ("GET", "/issue/%s/comment" % KEY),
         ("GET", "/issue/%s/remotelink" % KEY)]
LINK_POST = ("POST", "/issue/%s/remotelink" % KEY)
COMMENT_POST = ("POST", "/issue/%s/comment" % KEY)


# ---- the three named tests (P2-AC1..3) -------------------------------------------------------------------------------------------

def test_twice_one_link_one_comment(repo, fake, capsys):
    """P2-AC1: two applies leave ONE remote link (globalId = the PR URL, title `KEY: <title>`) and ONE kit comment; the second run
    finds both and writes nothing."""
    assert link(fake, "--apply") == 0
    out = capsys.readouterr().out
    assert _writes(fake) == [LINK_POST, COMMENT_POST]
    assert "linked: remote link" in out and "posted comment" in out
    assert link(fake, "--apply") == 0
    out = capsys.readouterr().out
    assert _writes(fake) == [LINK_POST, COMMENT_POST]  # nothing more
    (lk,) = _links(fake)
    assert lk["globalId"] == URL and lk["object"] == {"url": URL, "title": "SPK-1: Add the fictional export"}
    assert set(lk) == {"globalId", "object", "id"}  # no invented field (relationship, application) is sent
    (c,) = fake.comments.values()
    assert c["visibility"] is None and c["author"]["accountId"] == fake.account_id
    md = adf.adf_to_md(c["body"], strict=True)
    assert re.match(r"\[skj:pr [0-9a-f]{8}\]\n\n", md)
    assert "remote link: none (link %s already shows this pull request)" % lk["id"] in out
    assert "comment: none (comment %s already shows this link)" % c["id"] in out


def test_the_link_posted_but_the_comment_lost_still_ends_with_one_of_each(repo, fake, capsys):
    """A comment POST that never reached Jira: the next run sends the (idempotent) link only if it differs, and the comment once."""
    fake.fail_transport(on=fake._n + 7, applied=False)  # 5 reads, the link POST, then the comment POST
    assert link(fake, "--apply") == 1
    assert "write uncertain: not found on re-read" in capsys.readouterr().out
    assert len(_links(fake)) == 1 and fake.comments == {}
    assert link(fake, "--apply") == 0
    assert _writes(fake) == [LINK_POST, COMMENT_POST, COMMENT_POST]
    assert len(_links(fake)) == 1 and len(fake.comments) == 1


def test_dry_run_writes_nothing(repo, fake, capsys):
    """P2-AC2: without --apply only GETs, in the planned order; exit 0; the plan quotes both bodies exactly and is deterministic."""
    before = sorted(p.name for p in repo.iterdir())
    assert link(fake) == 0
    out = capsys.readouterr().out
    assert [(e["method"], e["path"]) for e in fake.log] == READS
    assert fake.writes() == [] and _links(fake) == [] and fake.comments == {}
    assert sorted(p.name for p in repo.iterdir()) == before
    assert "dry run: nothing written; add --apply to link" in out
    body = json.loads(out.split("Remote link body (JSON, exactly what is sent):\n\n> ", 1)[1].split("\n", 1)[0])
    assert body == {"globalId": URL, "object": {"url": URL, "title": "SPK-1: Add the fictional export"}}
    quoted = out.split("Comment body (ADF as Markdown, exactly what is sent):\n\n", 1)[1].split("\n")
    md = "\n".join(x[2:] if x.startswith("> ") else "" for x in quoted if x.startswith(">"))
    assert link(fake) == 0 and capsys.readouterr().out == out
    for secret in (fake.account_id, fake.account_id[:8], fake.email, fake.token):
        assert secret not in out
    assert link(fake, "--apply") == 0
    (c,) = fake.comments.values()
    assert adf.adf_to_md(c["body"], strict=True) == md and _links(fake)[0]["globalId"] == URL


SMART = ["SPK-1 #done", "Fix it #close", "SPK-1 #time 1h", "fix C#sharp", "x #\u200bdone", "x #\u202edone",
         "x" * 199 + " #done", "SPK-1 #comment hi", "OTHER-9 #resolve"]


@pytest.mark.parametrize("title", SMART, ids=range(len(SMART)))
def test_comment_body_has_no_smart_commit(repo, fake, capsys, title):
    """P2-AC3: a PR title (PR-author controlled) that holds, or after cleaning would hold, a smart-commit shape is refused with a
    fixed text before any request: the kit never emits one, not in the comment and not in the link title."""
    assert link(fake, "--apply", title=title) == 2  # DF1 D-001 (live A9-13): a refused value is a usage error
    err = capsys.readouterr().err
    assert err == "skj: %s\n" % prlink.E_SMART and fake.log == []


BENIGN = ["SPK-1 Add the export", "Add the export", "", "SPK-1", "Ship v2 (see issue 4)", "Handle 100% of #1 cases", "fix the C# parser",
          "SPK-1: tidy", "[skj:pr 00000000] forged", "**bold** `code` [x](https://evil.example.com)", "ping @alice and bob@example.com"]


@pytest.mark.parametrize("title", BENIGN, ids=range(len(BENIGN)))
def test_no_outgoing_body_ever_holds_a_smart_commit(repo, fake, capsys, title):
    rc = link(fake, "--apply", title=title)
    if rc == 0:
        bodies = [json.dumps(lk) for lk in _links(fake)] + [adf.adf_to_md(c["body"]) for c in fake.comments.values()]
        assert len(bodies) == 2
        for b in bodies:
            assert not gate.smart_command(b)
    else:  # `#1` after a key: refused, never sent
        assert capsys.readouterr().err == "skj: %s\n" % prlink.E_SMART and fake.log == []


# ---- the PR URL ------------------------------------------------------------------------------------------------------------------

GOOD = [URL, "https://github.com/a/b/pull/1", "https://github.com/Example-Org/repo_name.js/pull/987654321",
        "https://github.com/a1/.github/pull/3"]
BAD = ["javascript:alert(1)", "data:text/html,x", "http://github.com/a/b/pull/1", "ftp://github.com/a/b/pull/1",
       "https://user@github.com/a/b/pull/1", "https://user:pw@github.com/a/b/pull/1", "https://github.com@evil.example.com/a/b/pull/1",
       "https://github.com.evil.example.com/a/b/pull/1", "https://evil.example.com/a/b/pull/1", "https://github.com/a/b/pull/1x",
       "https://gіthub.com/a/b/pull/1", "https://github.com:443/a/b/pull/1", "https://github.com/a/b/pull/1?x=1",
       "https://github.com/a/b/pull/1#x", "https://github.com/a/b/pull/1\n", "https://github.com/a/b/pull/1%0a",
       " https://github.com/a/b/pull/1", "https://github.com/a/b/pull/1 ", "HTTPS://github.com/a/b/pull/1",
       "https://GitHub.com/a/b/pull/1", "https://GITHUB.COM/a/b/pull/1", "https://github.com./a/b/pull/1",
       "https://github.com/a/b/pull/0", "https://github.com/a/b/pull/01", "https://github.com/a/b/pull/1234567890",
       "https://github.com/a/b/pull/1/", "https://github.com/a/b/pull/1/files", "https://github.com/a/b/issues/1",
       "https://github.com/a/../pull/1", "https://github.com/a/./pull/1", "https://github.com/../b/pull/1", "https://github.com/a/b/c/pull/1",
       "https://github.com//b/pull/1", "https://github.com/-a/b/pull/1", "https://github.com/a_b/c/pull/1",
       "https://github.com/a/b%2f/pull/1", "https://github.com\\@evil.example.com/a/b/pull/1", "https://github.com/a/b/pull/1\x00",
       "https://github.com/a/b/pull/1\u202e", "https://github.com/" + "a" * 40 + "/b/pull/1", "https://github.com/a/" + "b" * 101 + "/pull/1",
       "https://github.com/a/b/pull/1" + "1" * 300, "", "github.com/a/b/pull/1", "//github.com/a/b/pull/1",
       "https://ghe.example.com/a/b/pull/1", "https://github.com/a/b/pull/１"]


@pytest.mark.parametrize("url", GOOD)
def test_a_valid_pr_url_is_kept_byte_for_byte(url):
    assert prlink.check_url(url, {}) == url


@pytest.mark.parametrize("url", BAD, ids=range(len(BAD)))
def test_a_hostile_pr_url_is_refused_with_a_fixed_message(repo, fake, capsys, url):
    with pytest.raises(ValueError) as e:
        prlink.check_url(url, {})
    assert str(e.value) == prlink.E_URL
    if "\x00" not in url:  # argv cannot carry a NUL
        assert link(fake, "--apply", url=url) == 2  # DF1 D-001 (live A9-11)
        assert capsys.readouterr().err == "skj: %s\n" % prlink.E_URL and fake.log == []


def test_a_github_enterprise_host_comes_from_gh_host_only(repo, fake, capsys, monkeypatch):
    ghe = "https://ghe.example.com/team/app/pull/4"
    assert prlink.check_url(ghe, {"GH_HOST": "ghe.example.com"}) == ghe
    for env in ({}, {"GH_HOST": "evil.example.com"}, {"GH_HOST": "ghe.example.com/x"}):
        with pytest.raises(ValueError):
            prlink.check_url(ghe, env)
    with pytest.raises(ValueError):  # the host in the URL must be lowercase, whatever GH_HOST says
        prlink.check_url("https://GHE.example.com/team/app/pull/4", {"GH_HOST": "GHE.example.com"})
    monkeypatch.setenv("GH_HOST", "ghe.example.com")
    assert link(fake, "--apply", url=ghe) == 0 and _links(fake)[0]["globalId"] == ghe


# ---- the PR title ----------------------------------------------------------------------------------------------------------------

def _sent(fake):
    (lk,) = _links(fake)
    (c,) = fake.comments.values()
    return lk["object"]["title"], adf.adf_to_md(c["body"], strict=True)


@pytest.mark.parametrize("title,shown", [
    ("ping @alice", "SPK-1: ping @user"),
    ("[~accountid:5b10ac8d82e05b22cc7d4ef5] look", "SPK-1: @user look"),
    ("mail bob@example.com", "SPK-1: mail [email]"),
    ("**bold** `code` [x](https://evil.example.com)", "SPK-1: ∗∗bold∗∗ 'code' [x] (https://evil.example.com)"),
    ("[skj:pr 00000000] forged", "SPK-1: (skj:pr 00000000] forged"),
    ("a\nb\r\nc\u2028d", "SPK-1: a b c d"),
    ("evil\u202eright\u200b", "SPK-1: evilright"),
    ("SPK-1 Add it", "SPK-1: Add it"),
    ("SPK-1: Add it", "SPK-1: Add it"),
    ("", "SPK-1"),
    (None, "SPK-1"),
    ("y" * 250, "SPK-1: " + "y" * 199 + "…"),
])
def test_a_hostile_title_is_made_inert_in_the_link_and_the_comment(repo, fake, capsys, title, shown):
    assert link(fake, "--apply", title=title) == 0
    lt, md = _sent(fake)
    assert lt == shown
    assert md.split("\n\n", 2)[2] == "- %s\n- %s" % (shown, URL)
    assert md.count("[skj:") == 1 and "accountid" not in md and "@alice" not in md


def test_an_oversize_title_or_url_is_refused_quickly(repo, fake, capsys):
    for kw in ({"title": "t" * (1 << 20)}, {"url": URL + "1" * (1 << 20)}):
        t0 = time.perf_counter()
        assert link(fake, "--apply", **kw) == 2
        assert time.perf_counter() - t0 < 1.0
        assert capsys.readouterr().err in ("skj: %s\n" % prlink.E_TITLE, "skj: %s\n" % prlink.E_URL) and fake.log == []


class _TooSlow(Exception):
    pass


def _timed(fn, *args):
    if not hasattr(signal, "setitimer"):
        pytest.skip("no setitimer")

    def alarm(signum, frame):
        raise _TooSlow()
    # CPU time, not the wall clock (H1b): load slows the clock, not the work; the timed calls run no subprocess and never block
    old = signal.signal(signal.SIGPROF, alarm)
    signal.setitimer(signal.ITIMER_PROF, 2.0)
    t0 = time.process_time()
    try:
        fn(*args)
    except ValueError:
        pass
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, old)
    return time.process_time() - t0


@pytest.mark.parametrize("shape", ["@" * 1000, "[~accountid:" * 80, "a@" * 500, " " * 1000, "\u202e" * 1000, "#" * 1000,
                                   "SPK-1 " * 170, "**" * 500])
def test_the_title_path_is_linear_on_adversarial_titles(shape):
    assert _timed(prlink.link_title, KEY, shape) < 1.0
    assert _timed(prlink.link_title, KEY, shape * 1000) < 1.0  # over the input cap: refused before any scan


# ---- the existing link and comment -----------------------------------------------------------------------------------------------

def test_a_link_with_another_title_is_updated_in_place(repo, fake, capsys):
    fake.issue(KEY)["links"].append({"id": 10000, "globalId": URL, "object": {"url": URL, "title": "SPK-1: old"}})
    assert link(fake) == 0
    assert "remote link: post (updates link 10000: its url or title differs)" in capsys.readouterr().out
    assert link(fake, "--apply") == 0
    (lk,) = _links(fake)
    assert lk["id"] == 10000 and lk["object"]["title"] == "SPK-1: Add the fictional export"


def test_another_globalid_does_not_count(repo, fake, capsys):
    fake.issue(KEY)["links"].append({"id": 10000, "globalId": URL + "3", "object": {"url": URL, "title": "SPK-1: Add the fictional export"}})
    assert link(fake, "--apply") == 0
    assert len(_links(fake)) == 2 and _writes(fake)[0] == LINK_POST


@pytest.mark.parametrize("answer", [{"x": 1}, b'"x"', [1], [{"id": 1, "globalId": URL}], [{"id": "x", "globalId": URL, "object": {}}],
                                    [{"globalId": URL, "object": {"url": URL, "title": "t"}}],
                                    [{"id": 1, "globalId": URL, "object": {}}, {"id": 2, "globalId": URL, "object": {}}],
                                    [{"id": i, "globalId": "g%d" % i, "object": {}} for i in range(1, 1100)]],
                         ids=["dict", "str", "int-item", "no-object", "str-id", "no-id", "two-same", "too-many"])
def test_a_malformed_link_list_refuses_before_any_write(repo, fake, capsys, monkeypatch, answer):
    monkeypatch.setattr(fake, "_h_list_links", lambda req, ref: (200, answer))
    assert link(fake, "--apply") == 1
    assert capsys.readouterr().err == "skj: %s\n" % prlink.E_LINKS and _writes(fake) == []


def test_a_foreign_marker_comment_is_ignored(repo, fake, capsys):
    assert link(fake) == 0
    out = capsys.readouterr().out
    quoted = out.split("Comment body (ADF as Markdown, exactly what is sent):\n\n", 1)[1].split("\n")
    md = "\n".join(x[2:] if x.startswith("> ") else "" for x in quoted if x.startswith(">"))
    fake.add_comment(KEY, adf.md_to_adf(md), author=fake.user(OTHER_ID))
    assert link(fake, "--apply") == 0
    out = capsys.readouterr().out
    assert "marker comment(s) by another account ignored" in out and len(_own(fake)) == 1 and len(fake.comments) == 2


def test_an_edited_own_comment_does_not_count(repo, fake, capsys):
    assert link(fake, "--apply") == 0
    (cid,) = fake.comments
    c = fake.comments[cid]
    md = adf.adf_to_md(c["body"], strict=True)
    fake.edit_comment(cid, adf.md_to_adf(md.replace("Add the", "Remove the")), editor=fake._me())
    capsys.readouterr()
    assert link(fake, "--apply") == 0
    out = capsys.readouterr().out
    assert "whose body no longer matches the marker (edited) ignored: %s" % cid in out
    assert _writes(fake) == [LINK_POST, COMMENT_POST, COMMENT_POST] and len(fake.comments) == 2


def test_a_truncated_comment_list_refuses(repo, fake, capsys, monkeypatch):
    real = fake._comment_page
    fake.add_comment(KEY, adf.md_to_adf("first"))
    fake.add_comment(KEY, adf.md_to_adf("second"))

    def short_total(issue, start, size):
        page = real(issue, start, size)
        page["total"] = 1
        return page
    monkeypatch.setattr(fake, "_comment_page", short_total)
    assert link(fake, "--apply") == 1
    assert "looks truncated or changed" in capsys.readouterr().err and _writes(fake) == []


def test_an_unreadable_own_marker_comment_refuses(repo, fake, capsys):
    cid = fake.add_comment(KEY, {"version": 1, "type": "doc", "content": [{"type": "mediaSingle", "content": []}]})
    assert link(fake, "--apply") == 1
    assert capsys.readouterr().err == "skj: %s\n" % (prlink.E_UNREADABLE % cid) and _writes(fake) == []


def test_a_moved_issue_or_another_project_refuses(repo, fake, capsys):
    fake.move_issue(KEY, "SPK-9")
    assert link(fake, "--apply") == 1
    assert "Jira answered SPK-9 for SPK-1; refused" in capsys.readouterr().err
    assert [e["path"] for e in fake.log] == ["/issue/" + KEY]
    f2 = FakeJira()
    f2.add_issue("A fictional story")
    f2.project = "OTH"
    assert link(f2, "--apply") == 1
    assert "not in project SPK" in capsys.readouterr().err and _writes(f2) == []


def test_a_key_of_another_project_is_refused(repo, fake, capsys):
    assert main(["pr", "link", "OTH-1", "--url", URL], client_factory=fake.client) == 2 and fake.log == []


# ---- uncertain and failed writes -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("applied", [True, False])
def test_an_uncertain_link_post_is_re_read_and_the_comment_waits(repo, fake, capsys, applied):
    fake.fail_transport(on=fake._n + 6, applied=applied)  # 5 reads, then the link POST
    assert link(fake, "--apply") == 1
    out = capsys.readouterr().out
    assert _writes(fake) == [LINK_POST] and fake.comments == {}
    assert fake.log[-1]["method"] == "GET" and fake.log[-1]["path"] == "/issue/%s/remotelink" % KEY
    if applied:
        assert "linked (found on re-read): remote link %s" % _links(fake)[0]["id"] in out
    else:
        assert "write uncertain: the remote link was not found on re-read" in out and _links(fake) == []
    assert "comment: not posted in this run" in out
    assert link(fake, "--apply") == 0
    assert len(_links(fake)) == 1 and len(fake.comments) == 1


@pytest.mark.parametrize("applied", [True, False])
def test_an_uncertain_comment_post_is_re_read_and_never_repeated(repo, fake, capsys, applied):
    fake.fail_transport(on=fake._n + 7, applied=applied)
    assert link(fake, "--apply") == 1
    out = capsys.readouterr().out
    assert _writes(fake) == [LINK_POST, COMMENT_POST]
    if applied:
        (cid,) = fake.comments
        assert "posted (found on re-read): comment %s" % cid in out
    else:
        assert "write uncertain: not found on re-read" in out and fake.comments == {}
    assert link(fake, "--apply") == 0
    assert len(_links(fake)) == 1 and len(fake.comments) == 1


def _answering(fake, path_end, answer):
    def opener(method, url, headers, body, timeout):
        if method == "POST" and url.endswith(path_end):
            fake.log.append({"method": method, "path": "intercepted" + path_end, "write": True})
            return answer[0], {"Content-Type": "application/json"}, json.dumps(answer[1]).encode()
        return fake.opener(method, url, headers, body, timeout)
    return Client(fake.site, fake.email, fake.token, opener=opener, sleep=lambda s: None)


@pytest.mark.parametrize("status", [400, 403, 404, 413])
def test_a_4xx_on_the_link_is_a_fixed_message_and_no_comment(repo, fake, capsys, status):
    c = _answering(fake, "/remotelink", (status, {"errorMessages": ["fake: secret server text"], "errors": {}}))
    assert link(fake, "--apply", client=lambda: c) == 1
    cap = capsys.readouterr()
    assert "failed: Jira answered HTTP %d; no remote link written, no comment posted" % status in cap.out
    assert "secret" not in cap.out + cap.err and fake.comments == {}


@pytest.mark.parametrize("answer", [(502, {"errorMessages": ["fake: secret"]}), (201, {"id": "x"}), (201, [])])
def test_a_5xx_or_odd_2xx_on_the_link_is_uncertain(repo, fake, capsys, answer):
    c = _answering(fake, "/remotelink", answer)
    assert link(fake, "--apply", client=lambda: c) == 1
    out = capsys.readouterr().out
    assert "write uncertain: the remote link was not found on re-read" in out and "secret" not in out and fake.comments == {}


@pytest.mark.parametrize("status", [400, 403])
def test_a_4xx_on_the_comment_is_a_fixed_message(repo, fake, capsys, status):
    c = _answering(fake, "/comment", (status, {"errorMessages": ["fake: secret server text"], "errors": {}}))
    assert link(fake, "--apply", client=lambda: c) == 1
    cap = capsys.readouterr()
    assert "failed: Jira answered HTTP %d; no comment posted" % status in cap.out and "secret" not in cap.out + cap.err
    assert len(_links(fake)) == 1


def test_a_read_error_echoes_no_server_text(repo, fake, capsys):
    fake.inject(500, body={"errorMessages": ["fake: secret server text"], "errors": {}})
    assert link(fake, "--apply") == 1
    cap = capsys.readouterr()
    assert "secret" not in cap.out + cap.err and _writes(fake) == []


# ---- the framework and the source -------------------------------------------------------------------------------------------------

def test_the_apply_is_gated_by_expect_plan(repo, fake, capsys):
    assert link(fake, "--plan-out", "p.md") == 0
    assert link(fake, "--apply", "--expect-plan", "p.md", title="SPK-1 another title") == 2
    assert _writes(fake) == [] and (repo / "p.md.new").exists()
    assert link(fake, "--apply", "--expect-plan", "p.md") == 0
    assert _writes(fake) == [LINK_POST, COMMENT_POST]


def test_only_the_planned_requests_exist_in_the_source():
    """No PUT or DELETE of a comment or a link: the module's requests are exactly the GETs and the two POSTs."""
    src = Path(prlink.__file__).read_text(encoding="utf-8")
    calls = re.findall(r"""\.request\(\s*"([A-Z]+)",\s*("[^"]*")""", src)
    assert sorted(set(calls)) == [("GET", '"/issue/%s/remotelink"'), ("POST", '"/issue/%s/comment"'),
                                  ("POST", '"/issue/%s/remotelink"')]
    assert src.count(".request(") == len(calls)
    for word in ("PUT", "DELETE", "visibility", "properties", "relationship", "application"):
        assert '"%s"' % word not in src


def test_a_url_over_255_characters_is_refused_even_on_a_long_gh_host():
    host = "h" * 60 + "." + "g" * 60 + ".example.com"
    url = "https://%s/%s/%s/pull/123456789" % (host, "o" * 39, "r" * 100)
    assert len(url) > prlink.MAX_URL
    with pytest.raises(ValueError, match="not a GitHub pull request URL"):
        prlink.check_url(url, {"GH_HOST": host})
    short = "https://%s/o/r/pull/1" % host
    assert prlink.check_url(short, {"GH_HOST": host}) == short
