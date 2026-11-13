import json
import os
import re
import subprocess
import sys
import traceback

import pytest

from skj import secretstore as ss

SITE = "example-site.atlassian.net"
EMAIL = "dev.person@example.com"
TOKEN = "FAKE-tok-9f3a7c1e5b2d-NOTREAL"
# Shape of `security find-generic-password -s ...` (no -w). Matches the real macOS output seen in live check LV1
# (2026-11-12, one macOS version); the values here are fictional.
_DUMP = (
    'keychain: "/Users/example/Library/Keychains/login.keychain-db"\n'
    'version: 512\n'
    'class: "genp"\n'
    'attributes:\n'
    '    0x00000007 <blob>="skj:%(site)s"\n'
    '    0x00000008 <blob>=<NULL>\n'
    '%(acct)s'
    '    "cdat"<timedate>=0x32303236313130383030303030305A00  "20261108000000Z\\000"\n'
    '    "crtr"<uint32>=<NULL>\n'
    '    "desc"<blob>=<NULL>\n'
    '    "icmt"<blob>=<NULL>\n'
    '%(svce)s'
    '    "type"<uint32>=<NULL>\n'
)


def _dump(acct='    "acct"<blob>="%s"\n' % EMAIL, svce='    "svce"<blob>="skj:%s"\n' % SITE, site=SITE):
    return _DUMP % {"acct": acct, "svce": svce, "site": site}


ATTRS = _dump()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Never touch the real home, env credentials or the real `security` command."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("SKJ_EMAIL", raising=False)
    monkeypatch.delenv("SKJ_TOKEN", raising=False)

    def _blocked(argv, **kw):
        raise AssertionError("unexpected subprocess call")

    monkeypatch.setattr(ss.subprocess, "run", _blocked)


def _completed(argv, kw, rc, out, err=b""):
    """Emulate subprocess.run's contract: bytes unless text mode is requested; text mode decodes strictly
    (as the real one does, raising UnicodeDecodeError whose repr carries the raw bytes)."""
    if isinstance(out, str):
        out = out.encode("utf-8")
    if isinstance(err, str):
        err = err.encode("utf-8")
    if kw.get("text") or kw.get("universal_newlines") or kw.get("encoding"):
        out, err = out.decode("utf-8"), err.decode("utf-8")
    return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)


def _mac(monkeypatch, calls=None, attrs=ATTRS, token=TOKEN, rc=0):
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(argv, **kw):
        if calls is not None:
            calls.append(list(argv))
        assert TOKEN not in repr(kw.get("env")) and TOKEN not in repr(kw.get("input"))
        if rc != 0:
            return _completed(argv, kw, rc, "", "could not be found")
        return _completed(argv, kw, 0, token + "\n" if "-w" in argv else attrs)

    monkeypatch.setattr(ss.subprocess, "run", fake_run)


def _linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")


def _file(tmp_path):
    return tmp_path / ".skj" / (SITE + ".json")


def _mkdir(tmp_path, mode=0o700):
    d = tmp_path / ".skj"
    d.mkdir()
    os.chmod(d, mode)
    return d


def _assert_clean(exc):
    text = str(exc.value) + repr(exc.value)
    assert TOKEN not in text and EMAIL not in text and "dev.person" not in text


def test_keychain_read_uses_service_skj_colon_site(monkeypatch):
    calls = []
    _mac(monkeypatch, calls)
    assert ss.get(SITE) == (EMAIL, TOKEN)
    assert len(calls) == 2
    first, second = calls
    assert first == ["security", "find-generic-password", "-s", "skj:" + SITE]
    assert second == ["security", "find-generic-password", "-s", "skj:" + SITE, "-a", EMAIL, "-w"]


def test_no_argv_contains_token(monkeypatch, tmp_path):
    calls = []
    _mac(monkeypatch, calls)
    ss.get(SITE)  # keychain path
    monkeypatch.setenv("SKJ_EMAIL", EMAIL)
    monkeypatch.setenv("SKJ_TOKEN", TOKEN)
    ss.get(SITE)  # env path
    monkeypatch.delenv("SKJ_EMAIL")
    monkeypatch.delenv("SKJ_TOKEN")
    _linux(monkeypatch)
    ss.store_file(SITE, EMAIL, TOKEN)
    assert ss.get(SITE) == (EMAIL, TOKEN)  # file path
    _mac(monkeypatch, calls, rc=44)
    with pytest.raises(ss.SecretMissing):
        ss.get(SITE)  # keychain failure path
    assert len(calls) >= 3
    assert not any(TOKEN in part for argv in calls for part in argv)
    assert not hasattr(ss, "store_keychain")  # K6: no macOS write function at all


def test_file_mode_0600(monkeypatch, tmp_path):
    _linux(monkeypatch)
    ss.store_file(SITE, EMAIL, TOKEN)
    assert oct(os.stat(_file(tmp_path)).st_mode & 0o777) == "0o600"
    assert oct(os.stat(tmp_path / ".skj").st_mode & 0o777) == "0o700"
    assert json.loads(_file(tmp_path).read_text()) == {"email": EMAIL, "token": TOKEN}
    assert ss.get(SITE) == (EMAIL, TOKEN)


def test_store_file_tightens_a_preexisting_loose_file(monkeypatch, tmp_path):
    _linux(monkeypatch)
    _mkdir(tmp_path)
    _file(tmp_path).write_text("old")
    os.chmod(_file(tmp_path), 0o644)
    ss.store_file(SITE, EMAIL, TOKEN)
    assert oct(os.stat(_file(tmp_path)).st_mode & 0o777) == "0o600"
    assert ss.get(SITE) == (EMAIL, TOKEN)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666])
def test_group_readable_file_refused(monkeypatch, tmp_path, mode):
    _linux(monkeypatch)
    ss.store_file(SITE, EMAIL, TOKEN)
    os.chmod(_file(tmp_path), mode)
    with pytest.raises(ss.SecretError) as e:
        ss.get(SITE)
    _assert_clean(e)


def test_symlinked_token_file_refused(monkeypatch, tmp_path):
    _linux(monkeypatch)
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"email": EMAIL, "token": TOKEN}))
    os.chmod(real, 0o600)
    _mkdir(tmp_path)
    _file(tmp_path).symlink_to(real)
    with pytest.raises(ss.SecretError) as e:
        ss.get(SITE)
    _assert_clean(e)
    with pytest.raises(ss.SecretError):
        ss.store_file(SITE, EMAIL, TOKEN)
    assert json.loads(real.read_text())["token"] == TOKEN  # target untouched


def test_env_overrides_store(monkeypatch, tmp_path):
    _linux(monkeypatch)
    ss.store_file(SITE, "other.person@example.com", "FAKE-file-token")
    monkeypatch.setenv("SKJ_EMAIL", EMAIL)
    monkeypatch.setenv("SKJ_TOKEN", TOKEN)
    assert ss.get(SITE) == (EMAIL, TOKEN)
    calls = []
    _mac(monkeypatch, calls)
    assert ss.get(SITE) == (EMAIL, TOKEN)
    assert calls == []  # the Keychain is never consulted


@pytest.mark.parametrize("which", ["SKJ_EMAIL", "SKJ_TOKEN"])
def test_partial_env_is_an_error_not_a_fallback(monkeypatch, tmp_path, which):
    _linux(monkeypatch)
    ss.store_file(SITE, EMAIL, TOKEN)
    monkeypatch.setenv(which, "x@example.com" if which == "SKJ_EMAIL" else TOKEN)
    with pytest.raises(ss.SecretMissing) as e:
        ss.get(SITE)
    _assert_clean(e)


def test_errors_do_not_echo_secret(monkeypatch, tmp_path):
    cases = []

    def boom(fn, *a):
        with pytest.raises(Exception) as e:
            fn(*a)
        cases.append(e)
        _assert_clean(e)

    _linux(monkeypatch)
    boom(ss.get, SITE)  # no file
    _mkdir(tmp_path)
    _file(tmp_path).write_text("{not json " + TOKEN + " " + EMAIL)
    os.chmod(_file(tmp_path), 0o600)
    boom(ss.get, SITE)  # malformed
    _file(tmp_path).write_text(json.dumps({"email": EMAIL}))
    boom(ss.get, SITE)  # token absent
    _file(tmp_path).write_text(json.dumps({"email": EMAIL, "token": TOKEN + "\n\x00"}))
    boom(ss.get, SITE)  # token with control chars
    os.chmod(_file(tmp_path), 0o644)
    boom(ss.get, SITE)  # loose mode
    boom(ss.store_file, SITE, EMAIL + "\n", TOKEN)  # bad email
    boom(ss.store_file, SITE, EMAIL, TOKEN + " \n")  # bad token
    boom(ss.store_file, "evil.example.com", EMAIL, TOKEN)  # bad site
    monkeypatch.setenv("SKJ_EMAIL", "bad email " + EMAIL)
    monkeypatch.setenv("SKJ_TOKEN", TOKEN)
    boom(ss.get, SITE)  # bad env email
    monkeypatch.delenv("SKJ_EMAIL")
    monkeypatch.delenv("SKJ_TOKEN")
    _mac(monkeypatch, rc=44)
    boom(ss.get, SITE)  # keychain item absent
    _mac(monkeypatch, attrs="no account line here\n")
    boom(ss.get, SITE)  # attributes unparsable
    _mac(monkeypatch, token="")
    boom(ss.get, SITE)  # empty token
    _mac(monkeypatch, attrs=ATTRS.replace(EMAIL, "bad email" + EMAIL, 1))
    boom(ss.get, SITE)  # keychain account that is not an email
    monkeypatch.setattr(ss.subprocess, "run", lambda a, **kw: (_ for _ in ()).throw(FileNotFoundError(TOKEN)))
    boom(ss.get, SITE)  # no `security` binary
    assert len(cases) >= 14


BAD_SITES = ["", "../x", "a/b", "evil.com", "Example.atlassian.net", SITE + "\n", "x.atlassian.net/../y",
             "-x.atlassian.net", ".atlassian.net", "a.atlassian.net.evil.com", None]


@pytest.mark.parametrize("site", BAD_SITES)
def test_bad_site_refused_before_any_access(monkeypatch, tmp_path, site):
    for plat in ("darwin", "linux"):
        monkeypatch.setattr(sys, "platform", plat)
        with pytest.raises(ValueError):
            ss.get(site)
        with pytest.raises(ValueError):
            ss.store_file(site, EMAIL, TOKEN)
    assert not (tmp_path / ".skj").exists()  # and the autouse guard proves no subprocess ran
    assert list(tmp_path.iterdir()) == []


# ---- S4b hardening ----

def _spoof_label(fake):
    # a label whose value runs over several lines and carries a fake acct line
    return ATTRS.replace('    0x00000007 <blob>="skj:%s"\n' % SITE,
                         '    0x00000007 <blob>="x\n%s\n"\n' % fake)


def _refused_without_second_call(monkeypatch, attrs):
    calls = []
    _mac(monkeypatch, calls, attrs=attrs)
    with pytest.raises(ss.SecretMissing) as e:
        ss.get(SITE)
    _assert_clean(e)
    assert len(calls) == 1 and "-w" not in calls[0]  # the token is never requested


def test_acct_parse_rejects_spoofed_label_line(monkeypatch):
    evil = '    "acct"<blob>="evil.person@example.com"'
    _refused_without_second_call(monkeypatch, _spoof_label(evil))  # real acct line also present
    _refused_without_second_call(
        monkeypatch, _spoof_label(evil).replace('    "acct"<blob>="%s"\n' % EMAIL, '    "acct"<blob>=<NULL>\n'))


def test_acct_parse_rejects_two_acct_lines(monkeypatch):
    second = '    "acct"<blob>="other.person@example.com"\n'
    _refused_without_second_call(monkeypatch, _dump(acct='    "acct"<blob>="%s"\n' % EMAIL + second))
    _refused_without_second_call(monkeypatch, ATTRS + ATTRS)  # two items concatenated


@pytest.mark.parametrize("acct", [
    '    "acct"<blob>=<NULL>\n',
    '    "acct"<blob>=0x64657640 "dev@"\n',  # hex form
    '    "acct"<blob>="d\u00e9v.person@example.com"\n',  # non-ASCII
    '   "acct"<blob>="%s"\n' % EMAIL,  # 3-space indent
    '     "acct"<blob>="%s"\n' % EMAIL,  # 5-space indent
    '\t"acct"<blob>="%s"\n' % EMAIL,
    '    "acct"<blob>="%s" \n' % EMAIL,  # trailing text
    '    "acct"<blob>="%s"\r\n' % EMAIL,
    "",
])
def test_acct_parse_rejects_odd_shapes(monkeypatch, acct):
    _refused_without_second_call(monkeypatch, _dump(acct=acct))


@pytest.mark.parametrize("svce", [
    '    "svce"<blob>="skj:other-site.atlassian.net"\n',
    '    "svce"<blob>="skj:%s-x"\n' % SITE,
    '    "svce"<blob>=<NULL>\n',
    "",  # no svce line
    '    "svce"<blob>="skj:%s"\n    "svce"<blob>="skj:%s"\n' % (SITE, SITE),  # two
])
def test_acct_parse_rejects_svce_mismatch(monkeypatch, svce):
    _refused_without_second_call(monkeypatch, _dump(svce=svce))


def test_store_file_atomic_failure_keeps_old_file(monkeypatch, tmp_path):
    _linux(monkeypatch)
    ss.store_file(SITE, EMAIL, TOKEN)
    old = _file(tmp_path).read_bytes()

    def boom(*a, **k):
        raise OSError("disk on fire " + TOKEN)

    for name in ("replace", "fsync"):
        with monkeypatch.context() as m:
            m.setattr(ss.os, name, boom)
            with pytest.raises(ss.SecretError) as e:
                ss.store_file(SITE, "new.person@example.com", "FAKE-new-token-0000")
            _assert_clean(e)
        assert _file(tmp_path).read_bytes() == old
        assert os.listdir(tmp_path / ".skj") == [SITE + ".json"]  # temp file removed
    assert ss.get(SITE) == (EMAIL, TOKEN)


def test_store_file_temp_is_exclusive_nofollow_0600(monkeypatch, tmp_path):
    _linux(monkeypatch)
    real_open, seen = os.open, []

    def spy(path, flags, mode=0o777, **kw):
        seen.append((str(path), flags, mode))
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(ss.os, "open", spy)
    ss.store_file(SITE, EMAIL, TOKEN)
    (path, flags, mode), = seen
    assert path != str(_file(tmp_path)) and os.path.dirname(path) == str(tmp_path / ".skj")
    assert flags & os.O_CREAT and flags & os.O_EXCL and flags & os.O_NOFOLLOW and not flags & os.O_TRUNC
    assert mode == 0o600


def test_store_file_does_not_write_through_hardlink(monkeypatch, tmp_path):
    _linux(monkeypatch)
    ss.store_file(SITE, "old.person@example.com", "FAKE-old-token-1111")
    peer = tmp_path / "peer.json"
    os.link(_file(tmp_path), peer)
    peer_before = peer.read_bytes()
    ss.store_file(SITE, EMAIL, TOKEN)
    assert peer.read_bytes() == peer_before and TOKEN.encode() not in peer.read_bytes()
    assert ss.get(SITE) == (EMAIL, TOKEN)
    assert oct(os.stat(_file(tmp_path)).st_mode & 0o777) == "0o600"


def test_store_file_refuses_non_regular_target(monkeypatch, tmp_path):
    _linux(monkeypatch)
    _mkdir(tmp_path)
    _file(tmp_path).mkdir()
    with pytest.raises(ss.SecretError):
        ss.store_file(SITE, EMAIL, TOKEN)


def test_dir_symlink_refused_on_write_and_read(monkeypatch, tmp_path):
    _linux(monkeypatch)
    target = tmp_path / "elsewhere"
    target.mkdir()
    os.chmod(target, 0o700)
    (target / (SITE + ".json")).write_text(json.dumps({"email": EMAIL, "token": TOKEN}))
    os.chmod(target / (SITE + ".json"), 0o600)
    (tmp_path / ".skj").symlink_to(target)
    with pytest.raises(ss.SecretError) as e:
        ss.get(SITE)
    _assert_clean(e)
    assert not isinstance(e.value, ss.SecretMissing)
    before = sorted(os.listdir(target))
    with pytest.raises(ss.SecretError) as e:
        ss.store_file(SITE, "new.person@example.com", "FAKE-new-token-0000")
    _assert_clean(e)
    assert sorted(os.listdir(target)) == before
    assert json.loads((target / (SITE + ".json")).read_text())["token"] == TOKEN


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o705, 0o770, 0o777, 0o710])
def test_dir_wrong_mode_refused(monkeypatch, tmp_path, mode):
    _linux(monkeypatch)
    ss.store_file(SITE, EMAIL, TOKEN)
    before = _file(tmp_path).read_bytes()
    os.chmod(tmp_path / ".skj", mode)
    with pytest.raises(ss.SecretError) as e:
        ss.get(SITE)
    _assert_clean(e)
    with pytest.raises(ss.SecretError):
        ss.store_file(SITE, "new.person@example.com", "FAKE-new-token-0000")
    assert _file(tmp_path).read_bytes() == before  # a loose dir is refused, not silently tightened


def test_dir_wrong_owner_refused(monkeypatch, tmp_path):
    _linux(monkeypatch)
    ss.store_file(SITE, EMAIL, TOKEN)
    me = os.getuid()
    monkeypatch.setattr(ss.os, "getuid", lambda: me + 1)
    with pytest.raises(ss.SecretError):
        ss.get(SITE)
    with pytest.raises(ss.SecretError):
        ss.store_file(SITE, EMAIL, TOKEN)


def test_dir_that_is_a_file_refused(monkeypatch, tmp_path):
    _linux(monkeypatch)
    (tmp_path / ".skj").write_text("x")
    for fn in (lambda: ss.get(SITE), lambda: ss.store_file(SITE, EMAIL, TOKEN)):
        with pytest.raises(ss.SecretError):
            fn()


def test_store_file_refuses_on_darwin(monkeypatch, tmp_path):
    _mac(monkeypatch)
    with pytest.raises(ss.SecretError) as e:
        ss.store_file(SITE, EMAIL, TOKEN)
    _assert_clean(e)
    assert not (tmp_path / ".skj").exists()


@pytest.mark.parametrize("out,ok", [
    (TOKEN + "\n", True),
    (TOKEN, True),
    (TOKEN + " \n", False),
    (TOKEN + "\r\n", False),
    (" " + TOKEN + "\n", False),
    ("\n" + TOKEN + "\n", False),
    ("\t" + TOKEN, False),
])
def test_keychain_token_rstrip_newline_only(monkeypatch, out, ok):
    _mac(monkeypatch)

    def fake_run(argv, **kw):
        assert TOKEN not in repr(kw.get("env")) and TOKEN not in repr(kw.get("input"))  # S4c-AC3 hygiene
        return _completed(argv, kw, 0, out if "-w" in argv else ATTRS)

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    if ok:
        assert ss.get(SITE) == (EMAIL, TOKEN)
    else:
        with pytest.raises(ss.SecretError) as e:
            ss.get(SITE)
        _assert_clean(e)


# ---- S4c: decode-error leak and cleanup ----

_SECRETS = (TOKEN, EMAIL, "FAKE-tok", "dev.person")


def _assert_no_leak(e):
    """No secret in str/repr, no chained exception at all, and none in a traceback rendered WITH locals."""
    exc, tb = e.value, e.tb
    assert exc.__context__ is None and exc.__cause__ is None
    while tb.tb_frame.f_code.co_filename == __file__:  # the test's own locals hold fixtures; the product frames are the subject
        tb = tb.tb_next
    text = str(exc) + repr(exc) + "".join(traceback.TracebackException(e.type, exc, tb, capture_locals=True).format())
    assert not any(secret in text for secret in _SECRETS)


def _run_returning(monkeypatch, per_call):
    """per_call(argv) -> (rc, stdout bytes, stderr bytes); emulates the bytes/text contract."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ss.subprocess, "run", lambda argv, **kw: _completed(argv, kw, *per_call(argv)))


def test_non_utf8_token_output_does_not_leak(monkeypatch):
    _run_returning(monkeypatch, lambda argv: (0, b"FAKE-tok\xff\n", b"") if "-w" in argv else (0, ATTRS, b""))
    with pytest.raises(ss.SecretError) as e:
        ss.get(SITE)
    _assert_no_leak(e)


def test_non_utf8_attributes_output_does_not_leak(monkeypatch):
    bad = ATTRS.encode().replace(b'"cdat"', b'"cd\xffat"')
    _run_returning(monkeypatch, lambda argv: (0, bad, b""))
    with pytest.raises(ss.SecretError) as e:
        ss.get(SITE)
    _assert_no_leak(e)
    # an invalid byte inside the account value itself
    bad = ATTRS.encode().replace(EMAIL.encode(), b"dev.person\xff@example.com")
    _run_returning(monkeypatch, lambda argv: (0, bad, b""))
    with pytest.raises(ss.SecretError) as e:
        ss.get(SITE)
    _assert_no_leak(e)


@pytest.mark.parametrize("rc", [0, 44])
def test_stderr_with_token_not_in_errors(monkeypatch, rc):
    err = b"leaked " + TOKEN.encode() + b" for " + EMAIL.encode()
    _run_returning(monkeypatch, lambda argv: (rc, b"", err))
    with pytest.raises(ss.SecretError) as e:
        ss.get(SITE)
    _assert_no_leak(e)
    # the same when the failure is on the second (-w) call
    _run_returning(monkeypatch, lambda argv: (44, b"", err) if "-w" in argv else (0, ATTRS, b""))
    with pytest.raises(ss.SecretError) as e:
        ss.get(SITE)
    _assert_no_leak(e)


def test_subprocess_failure_has_no_chained_exception(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    for make in (lambda argv: subprocess.TimeoutExpired(argv, 60, output=TOKEN.encode()),
                 lambda argv: FileNotFoundError(TOKEN + EMAIL),
                 lambda argv: subprocess.SubprocessError(TOKEN)):
        def boom(argv, **kw):
            raise make(argv)

        monkeypatch.setattr(ss.subprocess, "run", boom)
        with pytest.raises(ss.SecretMissing) as e:
            ss.get(SITE)
        _assert_no_leak(e)


def test_validation_failures_do_not_hold_secrets_in_frames(monkeypatch):
    for attrs, token in ((ATTRS.replace(EMAIL, "bad email" + EMAIL, 1), TOKEN), (ATTRS, TOKEN + " x"), (ATTRS, "")):
        _mac(monkeypatch, attrs=attrs, token=token)
        with pytest.raises(ss.SecretError) as e:
            ss.get(SITE)
        _assert_no_leak(e)


def _temps(tmp_path):
    return [n for n in os.listdir(tmp_path / ".skj") if n.endswith(".tmp")]


def test_keyboard_interrupt_removes_temp_file(monkeypatch, tmp_path):
    _linux(monkeypatch)
    ss.store_file(SITE, EMAIL, TOKEN)
    old = _file(tmp_path).read_bytes()
    for name in ("fsync", "replace"):
        with monkeypatch.context() as m:
            def intr(*a, **k):
                raise KeyboardInterrupt

            m.setattr(ss.os, name, intr)
            with pytest.raises(KeyboardInterrupt):
                ss.store_file(SITE, "new.person@example.com", "FAKE-new-token-0000")
        assert os.listdir(tmp_path / ".skj") == [SITE + ".json"]
        assert _file(tmp_path).read_bytes() == old


def _stale(tmp_path, site=SITE, suffix="0123456789abcdef"):
    return tmp_path / ".skj" / (".%s.%s.tmp" % (site, suffix))


def test_stale_temp_files_swept(monkeypatch, tmp_path):
    _linux(monkeypatch)
    d = _mkdir(tmp_path)
    mine = [_stale(tmp_path), _stale(tmp_path, suffix="fedcba9876543210")]
    for p in mine:
        p.write_text("leftover " + TOKEN)
    keep = {
        _stale(tmp_path, site="other-site.atlassian.net").name: "file",  # another site's temp
        ".%s.not-hex.tmp" % SITE: "file",  # not our temp pattern
        ".%s.0123456789abcdef.tmp.bak" % SITE: "file",
        SITE + ".json.0123456789abcdef.tmp": "file",
        ".%s.00000000000000aa.tmp" % SITE: "dir",  # matches the pattern but is not a regular file
        ".%s.00000000000000bb.tmp" % SITE: "link",
        ".%s.00000000000000cc.tmp" % SITE: "foreign",  # regular file owned by someone else
    }
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch")
    for name, kind in keep.items():
        p = d / name
        if kind == "dir":
            p.mkdir()
        elif kind == "link":
            p.symlink_to(victim)
        else:
            p.write_text("x")
    foreign, real_lstat = str(d / (".%s.00000000000000cc.tmp" % SITE)), os.lstat

    def lstat(path, *a, **k):
        st = real_lstat(path, *a, **k)
        if str(path) == foreign:
            st = os.stat_result(tuple(st[:4]) + (st.st_uid + 1,) + tuple(st[5:]))
        return st

    monkeypatch.setattr(ss.os, "lstat", lstat)
    ss.store_file(SITE, EMAIL, TOKEN)
    monkeypatch.undo()
    assert sorted(os.listdir(d)) == sorted(list(keep) + [SITE + ".json"])
    assert victim.read_text() == "do not touch"


def _parse_failure_message(monkeypatch, **dump_kw):
    _mac(monkeypatch, attrs=_dump(**dump_kw))
    with pytest.raises(ss.SecretMissing) as e:
        ss.get(SITE)
    _assert_no_leak(e)
    return str(e.value)


def test_parse_failure_messages_are_distinct(monkeypatch):
    shape = "attribute format not recognised (parser checked on one macOS version only)"
    for kw in ({"acct": ""}, {"acct": '   "acct"<blob>="%s"\n' % EMAIL}, {"svce": ""},
               {"acct": '    "acct"<blob>="a@example.com"\n' * 2}, {"acct": '    "acct"<blob>=0x64657640 "dev@"\n'}):
        assert _parse_failure_message(monkeypatch, **kw) == shape
    mismatch = _parse_failure_message(monkeypatch, svce='    "svce"<blob>="skj:other-site.atlassian.net"\n')
    no_account = _parse_failure_message(monkeypatch, acct='    "acct"<blob>=<NULL>\n')
    assert len({shape, mismatch, no_account}) == 3
    assert shape not in (mismatch, no_account) and "UNVERIFIED" not in mismatch + no_account
    monkeypatch.setattr(sys, "platform", "darwin")
    _mac(monkeypatch, attrs="no account line here\n")
    with pytest.raises(ss.SecretMissing) as e:
        ss.get(SITE)
    assert str(e.value) == shape


def test_write_failure_keeps_old_file(monkeypatch, tmp_path):
    _linux(monkeypatch)
    ss.store_file(SITE, EMAIL, TOKEN)
    old = _file(tmp_path).read_bytes()

    class _Failing:
        def __init__(self, fd):
            self.fd = fd

        def __enter__(self):
            return self

        def __exit__(self, *a):
            os.close(self.fd)

        def write(self, b):
            os.write(self.fd, b[:7])  # a partial write reaches the temp file
            raise OSError("disk full " + TOKEN)

    monkeypatch.setattr(ss.os, "fdopen", lambda fd, mode: _Failing(fd))
    with pytest.raises(ss.SecretError) as e:
        ss.store_file(SITE, "new.person@example.com", "FAKE-new-token-0000")
    _assert_no_leak(e)
    assert _file(tmp_path).read_bytes() == old
    assert os.listdir(tmp_path / ".skj") == [SITE + ".json"]

    # fdopen itself failing must not leak the raw descriptor either
    real_open, fds = os.open, []

    def spy(path, flags, mode=0o777, **kw):
        fds.append(real_open(path, flags, mode, **kw))
        return fds[-1]

    def bad_fdopen(fd, mode):
        raise OSError("no memory " + TOKEN)

    monkeypatch.setattr(ss.os, "open", spy)
    monkeypatch.setattr(ss.os, "fdopen", bad_fdopen)
    with pytest.raises(ss.SecretError) as e:
        ss.store_file(SITE, "new.person@example.com", "FAKE-new-token-0000")
    _assert_no_leak(e)
    assert len(fds) == 1
    with pytest.raises(OSError):
        os.fstat(fds[0])  # closed
    assert os.listdir(tmp_path / ".skj") == [SITE + ".json"]
    assert _file(tmp_path).read_bytes() == old
