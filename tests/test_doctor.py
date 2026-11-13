"""`skj doctor` rows: git, specify, config, config-target, site-pin, token, identity, project-type (R3); approvers, edit-all (A5).

Rebased in intent from the fork source's test_doctor.py: rows `{id, status, fix}`, statuses ok/warn/fail/unknown,
exit 1 only on a fail, fixed texts that never carry a token, an email or a full accountId.
"""
import json
import os
import subprocess

import pytest

from fakejira import EMAIL, ME_ID, TOKEN, FakeJira
from skj import config, doctor, secretstore
from skj.__main__ import main
from skj.client import Client

SITE = "example-site.atlassian.net"
OTHER_SITE = "other-site.atlassian.net"
BASE = {"site": SITE, "project": "SPK"}
_IDENTITY = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True,
                          env={**os.environ, **_IDENTITY, "GIT_CONFIG_GLOBAL": os.devnull,
                               "GIT_CONFIG_NOSYSTEM": "1"}).stdout.strip()


def make_repo(path, target=BASE, origin=True, working=BASE):
    """A repository whose origin/main (the target branch) holds `target` (None: no config there) and whose working
    tree holds `working`."""
    path.mkdir(exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    if target is not None:
        (path / "skj.config.json").write_text(json.dumps(target))
        git(path, "add", "-A")
    git(path, "commit", "-q", "--allow-empty", "-m", "target")
    if origin:
        git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    (path / "skj.config.json").write_text(json.dumps(working))
    return path


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("SKJ_CONFIG", "SKJ_EMAIL", "SKJ_TOKEN", "CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(k, raising=False)
    repo = make_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    tools(monkeypatch)  # never the machine's own git --version / specify --version
    return repo


TOOL_OUT = {"git": (0, b"git version 2.40.1\n"), "specify": (0, b"specify 1.2.4\n")}


def tools(monkeypatch, **answers):
    """Stub the subprocess doctor._run starts: answers maps the tool to (returncode, stdout) or an exception to raise."""
    table = {**TOOL_OUT, **answers}
    started, real = [], subprocess.run

    def fake_run(argv, *a, **kw):
        if list(argv[1:]) != ["--version"]:  # the config trust path's own git calls on the temp repos
            return real(argv, *a, **kw)
        started.append(argv)
        a = table[argv[0]]
        if isinstance(a, BaseException):
            raise a
        return subprocess.CompletedProcess(argv, a[0], a[1], b"")
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    return started


def rows(result):
    return {r["id"]: (r["status"], r["fix"]) for r in result["rows"]}


def run(repo, factory, base=None):
    return doctor.run(str(repo), base=base, client_factory=factory)


class ClassicJira(FakeJira):
    def _h_project(self, req, key):
        status, body = super()._h_project(req, key)
        if status == 200:
            body = dict(body, style="classic", simplified=False)
        return status, body


def test_row_ids_in_order(env):
    fake = FakeJira()
    assert [r["id"] for r in run(env, fake.client)["rows"]] == [
        "git", "specify", "config", "config-target", "site-pin", "token", "identity", "project-type", "approvers", "edit-all"]


def test_one_myself_call(env):
    fake = FakeJira()
    r = rows(run(env, fake.client))
    assert r["token"][0] == "ok" and r["identity"][0] == "ok" and r["project-type"] == ("ok", "")
    assert [e["path"] for e in fake.log if e["path"] == "/myself"] == ["/myself"]
    assert fake.writes() == [] and all(e["method"] == "GET" for e in fake.log)
    assert [e["path"] for e in fake.log] == ["/myself", "/project/SPK", "/mypermissions"]  # one project read, one permission read


def test_401_stops_and_shows_login_reason(env):
    fake = FakeJira()
    bad = lambda: Client(fake.site, fake.email, "WRONG-tok-0000", opener=fake, sleep=fake.sleeps.append)  # noqa: E731
    r = rows(run(env, bad))
    status, fix = r["token"]
    assert status == "fail" and "X-Seraph-LoginReason: AUTHENTICATED_FAILED" in fix and "CAPTCHA" in fix
    assert r["identity"][0] == "unknown" and r["project-type"][0] == "unknown"
    assert "not checked" in r["identity"][1] and "not checked" in r["project-type"][1]
    assert len(fake.log) == 1 and fake.sleeps == []  # stopped after the 401, never retried


def test_401_without_login_reason_says_none(env):
    fake = FakeJira()
    fake.inject(401, body={"errorMessages": ["fake: synthetic 401"], "errors": {}})  # the live no-credential 401: no header
    status, fix = rows(run(env, fake.client))["token"]
    assert status == "fail" and "X-Seraph-LoginReason" not in fix and "CAPTCHA" in fix
    assert "synthetic" not in fix  # server text is never echoed
    assert len(fake.log) == 1


def test_project_type_warn_on_classic(env):
    fake = ClassicJira()
    status, fix = rows(run(env, fake.client))["project-type"]
    assert status == "warn" and "not tested" in fix
    assert main(["doctor"], client_factory=fake.client) == 0  # a warning is not a failure


def test_project_type_unknown_style_and_missing_project(env):
    class Odd(FakeJira):
        def _h_project(self, req, key):
            status, body = super()._h_project(req, key)
            return status, dict(body, style="\x1b[31mweird")
    status, fix = rows(run(env, Odd().client))["project-type"]
    assert status == "unknown" and "\x1b" not in fix and "weird" not in fix
    (env / "skj.config.json").write_text(json.dumps({**BASE, "project": "NOPE"}))
    status, fix = rows(run(env, FakeJira().client))["project-type"]
    assert status == "fail" and "not found" in fix


@pytest.mark.parametrize("target,origin,ci,want", [
    (BASE, True, False, "ok"),
    (None, True, False, "ok"),  # no-target-config
    (BASE, False, False, "warn"),  # no-base
    ({**BASE, "site": OTHER_SITE}, True, False, "fail"),  # mismatch
    (BASE, False, True, "fail"),  # refused-ci-no-base
])
def test_site_pin_row(env, tmp_path, monkeypatch, target, origin, ci, want):
    repo = make_repo(tmp_path / "pin", target=target, origin=origin)
    monkeypatch.chdir(repo)
    if ci:
        monkeypatch.setenv("CI", "true")
    fake = FakeJira()
    r = rows(run(repo, fake.client))
    assert r["site-pin"][0] == want
    if want == "warn":
        assert r["site-pin"][1] == "site pin not enforced: no base (pass --base or fetch origin/HEAD)"
    if target is None:
        assert "no skj.config.json" in r["site-pin"][1]
    if want == "fail":
        assert r["token"][0] == "unknown" and "site pin" in r["token"][1]
        assert fake.log == []  # no credential was attached to anything
        # DF1 (live runs A1-11, A1-21, D-004): the target config WAS read; the pin refused it, so it is not "could not be read"
        assert r["approvers"] == ("unknown", doctor.NO_BASE_APPROVERS if ci else doctor.APPROVERS_PIN_REFUSED)
        assert OTHER_SITE not in r["site-pin"][1]
    else:
        assert r["token"][0] == "ok"


def test_site_pin_invalid_and_unreadable_trust_fail(env, monkeypatch):
    monkeypatch.setattr(config, "trusted", lambda repo=".", base=None: ("not", "a", "Trust"))
    fake = FakeJira()
    r = rows(run(env, fake.client))
    assert r["site-pin"][0] == "fail" and r["token"][0] == "unknown" and fake.log == []

    def boom(repo=".", base=None):
        raise config.ConfigInvalid("x: secret-looking value")
    monkeypatch.setattr(config, "trusted", boom)
    r = rows(run(env, fake.client))
    assert r["site-pin"][0] == "fail" and "secret-looking" not in r["site-pin"][1] and fake.log == []
    assert "could not be read" in r["approvers"][1]  # unreadable: the old text stays right here


def test_base_flag_reaches_the_pin(env, tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "b", origin=False)
    git(repo, "branch", "-f", "release")
    monkeypatch.chdir(repo)
    assert rows(run(repo, FakeJira().client))["site-pin"][0] == "warn"
    assert rows(run(repo, FakeJira().client, base="release"))["site-pin"][0] == "ok"


def test_missing_token_fails_with_fixed_hint(env, monkeypatch, capsys):
    def none():
        raise secretstore.SecretMissing("no keychain item for this site")
    r = rows(run(env, none))
    assert r["token"] == ("fail", doctor.NO_TOKEN)
    assert r["identity"][0] == "unknown" and r["project-type"][0] == "unknown"
    assert main(["doctor"], client_factory=none) == 1
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err and doctor.NO_TOKEN in out


def test_only_one_credential_variable_is_named_not_reported_as_missing_identity(env, monkeypatch):
    """LV1 (2026-11-12): a lone SKJ_EMAIL made doctor say "no Jira identity" while the Keychain item existed."""
    monkeypatch.setenv("SKJ_EMAIL", "dev@example.com")
    monkeypatch.delenv("SKJ_TOKEN", raising=False)
    status, text = rows(doctor.run(str(env)))["token"]
    assert status == "fail" and doctor.HALF_SET == text and "SKJ_EMAIL" in text and "SKJ_TOKEN" in text


def test_default_client_reads_the_secret_store(env, monkeypatch):
    seen = []

    def missing(site):
        seen.append(site)
        raise secretstore.SecretMissing("x")
    monkeypatch.setattr(secretstore, "get", missing)
    assert rows(doctor.run(str(env)))["token"] == ("fail", doctor.NO_TOKEN) and seen == [SITE]


def test_unusable_secret_fails_without_its_text(env):
    def bad():
        raise secretstore.SecretError("identity file must be a regular file with mode 0600")
    status, fix = rows(run(env, bad))["token"]
    assert status == "fail" and "SecretError" not in fix


def test_missing_config_skips_the_jira_rows(env):
    os.remove(env / "skj.config.json")
    called = []
    r = rows(run(env, lambda: called.append(1)))
    assert r["config"][0] == "fail" and "skj setup" in r["config"][1]
    assert r["token"][0] == "unknown" and called == []


def test_unknown_top_level_key_fails_the_config_row(env):
    """DF1 (live run A1-17, D-002): `"bogus": 1` in the working config gave `ok config` and exit 0."""
    data = json.loads((env / "skj.config.json").read_text())
    (env / "skj.config.json").write_text(json.dumps({**data, "bogus": 1}))
    called = []
    r = rows(run(env, lambda: called.append(1)))
    assert r["config"][0] == "fail" and "unknown key" in r["config"][1] and "bogus" not in r["config"][1]
    assert called == []


def test_identity_non_atlassian_fails(env):
    fake = FakeJira(account_type="app")
    r = rows(run(env, fake.client))
    assert r["token"][0] == "ok" and r["identity"][0] == "fail"
    assert [e["path"] for e in fake.log].count("/myself") == 1


def test_network_failure_is_unknown_never_ok(env):
    fake = FakeJira()
    fake.fail_transport(OSError("boom secret"), on=1)
    r = rows(run(env, fake.client))
    assert r["token"][0] == "unknown" and "boom" not in r["token"][1]


def test_crash_is_a_row_naming_the_class(env, monkeypatch):
    monkeypatch.setattr(doctor, "_git_row", lambda repo: 1 / 0)
    assert rows(run(env, FakeJira().client))["git"] == (
        "unknown", "this check failed unexpectedly (ZeroDivisionError) and gave no result")


def test_output_has_no_token_email_or_full_account_id(env, capsys):
    fake = FakeJira()
    assert main(["doctor"], client_factory=fake.client) == 0
    out, err = capsys.readouterr()
    for s in (TOKEN, EMAIL, ME_ID, "Fictional"):
        assert s not in out + err
    assert ME_ID[:8] + "..." in out


def test_cli_exit_code_is_1_on_a_fail(env, tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "mm", target={**BASE, "site": OTHER_SITE})
    monkeypatch.chdir(repo)
    assert main(["doctor"], client_factory=FakeJira().client) == 1


# --- git and specify rows: fed fixed outputs, never the machine's tools ---------------------------------------

@pytest.mark.parametrize("tool,answer,want", [
    ("git", (0, b"git version 2.23.9\n"), "fail"),  # below the floor
    ("git", (0, b"git version 2.24\n"), "ok"),  # at the floor (no patch number)
    ("git", FileNotFoundError(), "fail"),  # missing
    ("git", (0, b"gti versoin two\n"), "unknown"),  # garbled
    ("git", (1, b"git version 2.40.1\n"), "unknown"),  # non-zero exit
    ("git", subprocess.TimeoutExpired("git", 10), "unknown"),
    ("specify", (0, b"specify 1.2.0\n"), "fail"),
    ("specify", (0, b"specify-cli v1.2.1\n"), "ok"),
    ("specify", FileNotFoundError(), "warn"),
    ("specify", (0, b"\x1b[31mnonsense\n"), "unknown"),
    ("specify", (2, b"specify 1.2.4\n"), "unknown"),
])
def test_tool_rows(env, monkeypatch, tool, answer, want):
    started = tools(monkeypatch, **{tool: answer})
    status, fix = rows(run(env, FakeJira().client))[tool]
    assert status == want and "nonsense" not in fix
    assert sorted(a[0] for a in started) == ["git", "specify"]


def test_tool_runs_get_a_scrubbed_env_and_a_timeout(env, monkeypatch):
    seen = []
    monkeypatch.setenv("SKJ_TOKEN", "x")
    monkeypatch.setenv("SKJ_EMAIL", "y")
    real = subprocess.run

    def spy(argv, *a, **kw):
        if list(argv[1:]) == ["--version"]:
            seen.append(kw)
            return subprocess.CompletedProcess(argv, 0, TOOL_OUT[argv[0]][1], b"")
        return real(argv, *a, **kw)
    monkeypatch.setattr(subprocess, "run", spy)
    run(env, FakeJira().client)
    assert len(seen) == 2 and all(kw["timeout"] == doctor.TIMEOUT and "SKJ_TOKEN" not in kw["env"] for kw in seen)


# --- exit code: the token row must prove the credentials work -------------------------------------------------

def test_5xx_on_myself_exits_nonzero(env, capsys):
    fake = FakeJira()
    fake.inject(500)
    assert rows(run(env, fake.client))["token"][0] == "unknown"
    fake = FakeJira()
    fake.inject(500)
    assert main(["doctor"], client_factory=fake.client) == 1
    assert "Traceback" not in "".join(capsys.readouterr())


def test_network_error_on_myself_exits_nonzero(env, capsys):
    fake = FakeJira()
    fake.fail_transport(OSError("boom"), on=1)
    assert main(["doctor"], client_factory=fake.client) == 1
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err and "unknown token" in out.replace("  ", " ").replace("  ", " ")


def test_other_unknown_rows_do_not_fail(env, monkeypatch):
    tools(monkeypatch, specify=(0, b"garbled"))
    class Odd(FakeJira):
        def _h_project(self, req, key):
            status, body = super()._h_project(req, key)
            return status, dict(body, style=None)
    fake = Odd()
    result = run(env, fake.client)
    r = rows(result)
    assert r["specify"][0] == r["project-type"][0] == "unknown" and r["token"][0] == "ok"
    assert not doctor.failed(result)


# --- A5: the trust rows `approvers` and `edit-all`, and the one tamper sentence ----------------------------------

from fakejira import OTHER_ID  # noqa: E402
from skj import approvals, strict  # noqa: E402
from skj.config import Config  # noqa: E402

EDIT_DELETE = ("EDIT_ALL_COMMENTS", "DELETE_ALL_COMMENTS", "DELETE_OWN_COMMENTS")
NONE_OF_THEM = dict.fromkeys(EDIT_DELETE, False)
COMMENT = {**BASE, "approval": "comment", "approvers": [OTHER_ID]}
STRICT_COMMENT = {**COMMENT, "mode": "strict"}
SENTENCE = "On this site the token can edit or delete comments, including its own, so an approval can be shown to have been tampered with only if it was edited; a deleted comment leaves no trace; comment approval is experimental and cannot be made tamper-proof."  # noqa: E501


def at(tmp_path, monkeypatch, target, working=BASE, name="t"):
    repo = make_repo(tmp_path / name, target=target, working=working)
    monkeypatch.chdir(repo)
    return repo


def perm_calls(fake):
    return [e for e in fake.log if e["path"] == "/mypermissions"]


def test_edit_all_false_ok(env):  # A5-AC2
    fake = FakeJira(permissions=NONE_OF_THEM)
    result = run(env, fake.client)
    assert rows(result)["edit-all"] == ("ok", "") and not doctor.failed(result)
    (call,) = perm_calls(fake)
    assert call["method"] == "GET" and call["query"] == {"projectKey": "SPK", "permissions": ",".join(EDIT_DELETE)}


def test_edit_all_warns_advisory(env, capsys):  # A5-AC1, advisory/terminal: a warning, never a failure
    fake = FakeJira()  # the Free site owner: all three are true
    status, fix = rows(run(env, fake.client))["edit-all"]
    assert status == "warn" and fix != approvals.TAMPER_SENTENCE and "terminal" in fix
    assert all(p in fix for p in EDIT_DELETE)
    assert main(["doctor"], client_factory=FakeJira().client) == 0
    assert "warn    edit-all" in capsys.readouterr()[0]


def test_edit_all_strict_comment_mode_prints_sentence_does_not_refuse(tmp_path, env, monkeypatch, capsys):  # A5-AC1
    at(tmp_path, monkeypatch, STRICT_COMMENT, working=STRICT_COMMENT)
    result = run(".", FakeJira().client)
    assert rows(result)["edit-all"] == ("warn", approvals.TAMPER_SENTENCE)
    assert not doctor.failed(result)  # the owner decision: the sentence is printed, nothing is refused
    assert main(["doctor"], client_factory=FakeJira().client) == 0
    out = capsys.readouterr()[0]
    assert "warn    edit-all       " + approvals.TAMPER_SENTENCE in out.splitlines()  # rendered whole, never cut


@pytest.mark.parametrize("name", EDIT_DELETE)
def test_edit_all_each_permission_alone_warns(tmp_path, env, monkeypatch, name):
    at(tmp_path, monkeypatch, COMMENT, working=COMMENT)
    assert rows(run(".", FakeJira(permissions=dict(NONE_OF_THEM, **{name: True})).client))["edit-all"] == (
        "warn", approvals.TAMPER_SENTENCE)
    at(tmp_path, monkeypatch, BASE, name="t2")
    status, fix = rows(run(".", FakeJira(permissions=dict(NONE_OF_THEM, **{name: True})).client))["edit-all"]
    assert status == "warn" and name in fix and [p for p in EDIT_DELETE if p in fix] == [name]


def test_edit_all_in_comment_mode_reads_the_target_branch(tmp_path, env, monkeypatch):
    at(tmp_path, monkeypatch, BASE, working=COMMENT)  # the PR says comment, the target says terminal: terminal
    assert rows(run(".", FakeJira().client))["edit-all"][1] != approvals.TAMPER_SENTENCE
    at(tmp_path, monkeypatch, COMMENT, working=BASE, name="t2")
    assert rows(run(".", FakeJira().client))["edit-all"] == ("warn", approvals.TAMPER_SENTENCE)


class PermsAnswer(FakeJira):
    def __init__(self, answer, **kw):
        super().__init__(**kw)
        self.answer = answer

    def _h_mypermissions(self, req):
        return self.answer


@pytest.mark.parametrize("answer", [
    (200, {"permissions": {}}),  # every name omitted: cannot tell, never "all false"
    (200, {"permissions": {"EDIT_ALL_COMMENTS": {"havePermission": False}, "DELETE_ALL_COMMENTS": {"havePermission": False}}}),
    (200, {"permissions": dict.fromkeys(EDIT_DELETE, {"havePermission": "false"})}),
    (200, {"permissions": dict.fromkeys(EDIT_DELETE, {"havePermission": None})}),
    (200, {"permissions": []}),
    (200, ["no"]),
    (500, {"errorMessages": ["fake: secret-ish server text"], "errors": {}}),
    (403, {"errorMessages": ["fake: secret-ish server text"], "errors": {}}),
])
def test_edit_all_malformed_or_refused_answer_is_unknown_never_ok(env, answer):
    result = run(env, PermsAnswer(answer).client)
    status, fix = rows(result)["edit-all"]
    assert status == "unknown" and "secret-ish" not in fix and not doctor.failed(result)


def test_edit_all_401_is_not_retried(env):
    fake = PermsAnswer((401, {"errorMessages": [], "errors": {}}))
    assert rows(run(env, fake.client))["edit-all"][0] == "unknown"
    assert len(perm_calls(fake)) == 1 and fake.sleeps == []


def test_edit_all_network_failure_is_unknown(env):
    fake = FakeJira(permissions=NONE_OF_THEM)
    fake.fail_transport(OSError("boom"), on=3)
    assert rows(run(env, fake.client))["edit-all"][0] == "unknown"


def test_edit_all_no_request_when_earlier_rows_failed(env):
    fake = FakeJira()
    bad = lambda: Client(fake.site, fake.email, "WRONG-tok-0000", opener=fake, sleep=fake.sleeps.append)  # noqa: E731
    r = rows(run(env, bad))
    assert r["edit-all"][0] == "unknown" and "not checked" in r["edit-all"][1] and perm_calls(fake) == []
    (env / "skj.config.json").write_text(json.dumps({**BASE, "project": "NOPE"}))  # the project row fails
    fake = FakeJira()
    r = rows(run(env, fake.client))
    assert r["project-type"][0] == "fail" and r["edit-all"][0] == "unknown" and perm_calls(fake) == []
    fake = FakeJira()
    fake.inject(500)  # /myself
    assert rows(run(env, fake.client))["edit-all"][0] == "unknown" and perm_calls(fake) == []


def test_row_order_and_render_width(env):
    result = run(env, FakeJira().client)
    assert [r["id"] for r in result["rows"]] == list(doctor.ROW_IDS)
    assert doctor.ROW_IDS[-3:] == ("project-type", "approvers", "edit-all")
    for line, r in zip(doctor.render(result).splitlines(), result["rows"]):
        assert line == f"{r['status']:<8}{r['id']:<15}{r['fix']}".rstrip()


# approvers (A1-AC3, the doctor half: the token user listed as an approver fails)

def test_approvers_valid_in_comment_mode_ok(tmp_path, env, monkeypatch):
    at(tmp_path, monkeypatch, COMMENT, working=COMMENT)
    status, fix = rows(run(".", FakeJira().client))["approvers"]
    assert status == "ok" and "1" in fix and OTHER_ID not in fix


def test_approvers_token_user_listed_fails(tmp_path, env, monkeypatch, capsys):  # A1-AC3
    cfg = {**BASE, "approval": "comment", "approvers": [OTHER_ID, ME_ID]}
    at(tmp_path, monkeypatch, cfg, working=cfg)
    result = run(".", FakeJira().client)
    status, fix = rows(result)["approvers"]
    assert status == "fail" and doctor.failed(result)
    assert "approvers[1]" in fix and ME_ID[:8] + "..." in fix and ME_ID not in fix and OTHER_ID[:8] not in fix
    assert main(["doctor"], client_factory=FakeJira().client) == 1
    assert ME_ID not in "".join(capsys.readouterr())


def test_approvers_token_user_listed_fails_in_terminal_mode_too(tmp_path, env, monkeypatch):
    cfg = {**BASE, "approvers": [ME_ID]}
    at(tmp_path, monkeypatch, cfg, working=cfg)
    assert rows(run(".", FakeJira().client))["approvers"][0] == "fail"


def test_approvers_compare_exact_strings(tmp_path, env, monkeypatch):
    cfg = {**BASE, "approval": "comment", "approvers": [ME_ID.upper()]}  # accountIds are case-sensitive: another account
    at(tmp_path, monkeypatch, cfg, working=cfg)
    assert rows(run(".", FakeJira().client))["approvers"][0] == "ok"


@pytest.mark.parametrize("bad", ["", "a b", "x" * 129, "id\n", 5, None])
def test_approvers_not_an_account_id_fails(env, monkeypatch, bad):
    # the config validator already refuses these; the row checks again on whatever Trust it is handed
    monkeypatch.setattr(config, "trusted", lambda repo=".", base=None: config.Trust([OTHER_ID, bad], "comment", {}, SITE, True))
    status, fix = rows(run(env, FakeJira().client))["approvers"]
    assert status == "fail" and "approvers[1]" in fix and OTHER_ID not in fix


def test_approvers_comment_mode_empty_fails(tmp_path, env, monkeypatch):
    cfg = {**BASE, "approval": "comment"}
    at(tmp_path, monkeypatch, cfg, working=cfg)
    result = run(".", FakeJira().client)
    assert rows(result)["approvers"][0] == "fail" and doctor.failed(result)


def test_approvers_terminal(tmp_path, env, monkeypatch):
    assert rows(run(env, FakeJira().client))["approvers"] == ("ok", doctor.APPROVERS_NOT_USED)
    cfg = {**BASE, "approvers": [OTHER_ID]}
    at(tmp_path, monkeypatch, cfg, working=cfg)
    result = run(".", FakeJira().client)
    status, fix = rows(result)["approvers"]
    assert status == "warn" and "terminal" in fix and not doctor.failed(result)


@pytest.mark.parametrize("myself", ["500", "net", "401", "app"])
def test_approvers_unknown_when_myself_cannot_be_read(tmp_path, env, monkeypatch, myself):
    at(tmp_path, monkeypatch, COMMENT, working=COMMENT)
    fake = FakeJira(account_type="app") if myself == "app" else FakeJira()
    if myself == "500":
        fake.inject(500)
    elif myself == "net":
        fake.fail_transport(OSError("boom"), on=1)
    factory = fake.client
    if myself == "401":
        factory = lambda: Client(fake.site, fake.email, "WRONG-tok-0000", opener=fake, sleep=fake.sleeps.append)  # noqa: E731
    assert rows(run(".", factory))["approvers"][0] == "unknown"


def test_approvers_unknown_without_trust(env, monkeypatch):
    def boom(repo=".", base=None):
        raise config.ConfigInvalid("x")
    monkeypatch.setattr(config, "trusted", boom)
    assert rows(run(env, FakeJira().client))["approvers"][0] == "unknown"


def test_approvers_read_from_the_target_branch_never_the_working_tree(tmp_path, env, monkeypatch):
    listed = {**BASE, "approval": "comment", "approvers": [ME_ID]}
    at(tmp_path, monkeypatch, BASE, working=listed)  # the working tree lists the token user, the target lists nobody
    assert rows(run(".", FakeJira().client))["approvers"] == ("ok", doctor.APPROVERS_NOT_USED)
    at(tmp_path, monkeypatch, listed, working=BASE, name="t2")  # the reverse: the target wins
    assert rows(run(".", FakeJira().client))["approvers"][0] == "fail"


# the sentence (A5-AC3): one constant, the same text in doctor, the strict output and the README

def test_sentence_constant_is_shared(tmp_path, env, monkeypatch):
    assert approvals.TAMPER_SENTENCE == SENTENCE  # the wording is a requirement (owner decision O5, A2 escalation)
    at(tmp_path, monkeypatch, COMMENT, working=COMMENT)
    doctor_text = rows(run(".", FakeJira().client))["edit-all"][1]
    out = strict.check(Config(SITE, "SPK", mode="strict", approval="comment"), "SPK-1", "implement", repo=".")
    assert doctor_text == SENTENCE and out["warnings"].count(SENTENCE) == 1
    terminal = Config(SITE, "SPK", mode="strict")
    terminal.from_target = True  # a terminal target: no note
    assert SENTENCE not in json.dumps(strict.check(terminal, "SPK-1", "implement", repo="."))
    here = os.path.dirname(os.path.abspath(__file__))
    for mod in ("doctor.py", "strict.py"):  # used by name, never copied
        with open(os.path.join(here, "..", "src", "skj", mod), encoding="utf-8") as f:
            src = f.read()
        assert "approvals.TAMPER_SENTENCE" in src and "tamper-proof" not in src


@pytest.mark.parametrize("target,working,cfg_approval,want", [
    (COMMENT, BASE, "terminal", True),  # the PR's own file says terminal: it cannot hide the note
    (BASE, COMMENT, "comment", False),  # the PR's own file says comment: the target says terminal, no note
    ({**BASE, "approval": "bogus"}, BASE, "terminal", True),  # an unreadable target errs toward the note
])
def test_strict_note_reads_the_target_branch(tmp_path, env, monkeypatch, target, working, cfg_approval, want):
    at(tmp_path, monkeypatch, target, working=working)
    out = strict.check(Config(SITE, "SPK", mode="strict", approval=cfg_approval), "SPK-1", "implement", repo=".")
    assert (SENTENCE in out.get("warnings", [])) is want
    judged = Config(SITE, "SPK", mode="strict", approval="comment")
    judged.from_target = True  # a judging config taken from the target is believed as it is
    assert SENTENCE in strict.check(judged, "SPK-1", "implement", repo=".")["warnings"]


def test_no_base_makes_no_claim_about_the_target(tmp_path, env, monkeypatch):
    repo = make_repo(tmp_path / "nb", target=COMMENT, origin=False)  # the real target: comment approval with approvers
    monkeypatch.chdir(repo)
    r = rows(run(repo, FakeJira().client))
    assert r["site-pin"][0] == "warn"
    assert r["approvers"][0] == "unknown" and "no base" in r["approvers"][1]
    status, fix = r["edit-all"]
    assert status == "warn" and "terminal" not in fix and "unknown" in fix and fix != approvals.TAMPER_SENTENCE
    assert all(p in fix for p in EDIT_DELETE)
    assert rows(run(repo, FakeJira(permissions=NONE_OF_THEM).client))["edit-all"] == ("ok", "")
    monkeypatch.setenv("CI", "true")  # CI with no base: the pin fails, the trust rows are unknown, nothing is sent
    fake = FakeJira()
    r = rows(run(repo, fake.client))
    assert r["site-pin"][0] == "fail" and r["approvers"][0] == r["edit-all"][0] == "unknown" and fake.log == []


# C2 must put approvals.TAMPER_SENTENCE verbatim on ONE unwrapped line of README.md; this skip then becomes a real check.
def test_readme_carries_the_sentence_when_present():
    readme = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "README.md")
    if not os.path.exists(readme):
        pytest.skip("no README.md yet (installed by task C2)")
    with open(readme, encoding="utf-8") as f:
        assert SENTENCE in f.read()


def test_d023_a_moved_origin_head_fails_the_target_and_pin_rows_with_no_request(env):
    """DF2 (D-023): origin/main is in comment approval; origin/HEAD is moved to a branch that is not. Both rows say target-moved and no
    client is built."""
    git(env, "checkout", "-q", "-b", "feature")
    (env / "skj.config.json").write_text(json.dumps({**BASE, "approvers": ["5b10ac8d82e05b22cc7d4ef5"], "approval": "comment"}))
    git(env, "add", "-A")
    git(env, "commit", "-q", "-m", "guard")
    git(env, "update-ref", "refs/remotes/origin/main", "HEAD")
    (env / "skj.config.json").write_text(json.dumps(BASE))
    git(env, "commit", "-q", "-am", "weaken")
    git(env, "update-ref", "refs/remotes/origin/lv-x7", "HEAD")
    git(env, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/lv-x7")
    fake = FakeJira()
    r = rows(run(env, fake.client))
    assert r["config-target"] == ("fail", config.E_TARGET_MOVED) and r["site-pin"] == ("fail", config.E_TARGET_MOVED)
    assert r["token"][0] == "unknown" and fake.log == []
    # DF3 (R1 retest, D-004 variant b): the target was read and refused, so the approvers row says so, never "could not be read"
    assert r["approvers"] == ("unknown", doctor.APPROVERS_TARGET_MOVED)


@pytest.mark.parametrize("bad", [{"rules": {"types": ["Story"]}}, {"bogus": 1}, {"branch_prefix": "f\x07/"}])
def test_df3_an_invalid_working_tree_config_is_named_in_the_approvers_row(env, bad):
    """R1 retest, D-004 variant a (A8-16): config-target ok, the working-tree config invalid: the approvers row points at the config row."""
    (env / "skj.config.json").write_text(json.dumps({**BASE, **bad}))
    fake = FakeJira()
    r = rows(run(env, fake.client))
    assert r["config"][0] == "fail" and r["approvers"] == ("unknown", doctor.APPROVERS_NO_CONFIG) and fake.log == []
