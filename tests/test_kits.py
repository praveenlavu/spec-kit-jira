import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from skj import __main__ as cli
from skj import kits, stack
from skj.config import ConfigInvalid
from test_stack_support import TypedStdin, git, git_env, run_in_pty, shown_code, terminal  # noqa: F401  (git_env: fixture)

KIT = ".specify/extensions/sf"
ROOT = Path(__file__).resolve().parent.parent
MARK = "IGNORE-ALL-PREVIOUS-INSTRUCTIONS-MARKER"


def _registry(*entries):
    return json.dumps({"schema_version": "1.0", "extensions": dict(entries)})


def _entry(commands=(), skills=(), **kw):
    return {"version": "1.0.0", "source": "local", "enabled": True,
            "registered_commands": {"claude": list(commands)} if commands else {}, "registered_skills": list(skills), **kw}


def _put(root, rel, data=b"x\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return p


@pytest.fixture
def repo(tmp_path, git_env):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "Dana Fictional")
    git(tmp_path, "config", "user.email", "dana@contoso.example")
    _put(tmp_path, f"{KIT}/extension.yml", "id: sf\n")
    _put(tmp_path, f"{KIT}/commands/specify.md", "specify\n")
    _put(tmp_path, f"{KIT}/sf-config.yml", "org: contoso\n")
    _put(tmp_path, f"{KIT}/local-config.yml", "me: 1\n")
    _put(tmp_path, ".specify/extensions/sf-extra/extension.yml", "id: sf-extra\n")
    _put(tmp_path, ".claude/skills/speckit-sf-specify/SKILL.md", "skill sf\n")
    _put(tmp_path, ".claude/skills/speckit-sf-extra-run/SKILL.md", "skill extra\n")
    _put(tmp_path, ".specify/extensions/.registry", _registry(
        ("sf", _entry(commands=["speckit.sf.specify"])), ("sf-extra", _entry(commands=["speckit.sf-extra.run"]))))
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _head(repo):
    return git(repo, "rev-parse", "HEAD")


# ---- dir_digest ----
def test_digest_changes_on_one_byte(repo):
    before = kits.dir_digest(repo, "sf")
    assert len(before) == 64
    _put(repo, f"{KIT}/commands/specify.md", "specifY\n")
    assert kits.dir_digest(repo, "sf") != before


def test_digest_excludes_local_and_kit_config_files(repo):
    before = kits.dir_digest(repo, "sf")
    _put(repo, f"{KIT}/local-config.yml", "me: 2\n")
    _put(repo, f"{KIT}/sf-config.yml", "org: fabrikam\n")
    _put(repo, f"{KIT}/other-config.local.yml", "a: 1\n")
    assert kits.dir_digest(repo, "sf") == before
    _put(repo, f"{KIT}/other-config.yml", "a: 1\n")  # not an excluded name: it counts
    assert kits.dir_digest(repo, "sf") != before


@pytest.mark.parametrize("autocrlf", [False, True])
def test_digest_local_equals_committed(repo, autocrlf):
    if autocrlf:
        git(repo, "config", "core.autocrlf", "true")
    _put(repo, f"{KIT}/commands/crlf.md", b"one\r\ntwo\r\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "crlf")
    assert kits.dir_digest(repo, "sf") == kits.dir_digest_at(repo, _head(repo), "sf")


def test_empty_kit_dir_raises(repo):
    git(repo, "rm", "-q", f"{KIT}/extension.yml", f"{KIT}/commands/specify.md")
    (repo / ".specify/extensions/sf-extra/extension.yml").unlink()  # tracked but gone from the disk: unreadable, never skipped
    with pytest.raises(kits.KitError) as e:
        kits.dir_digest(repo, "sf-extra")
    assert e.value.reason == "unreadable"
    with pytest.raises(kits.KitError) as e:
        kits.dir_digest(repo, "sf")
    assert e.value.reason == "empty"
    with pytest.raises(kits.KitError) as e:  # the id grammar is checked before any path is built
        kits.dir_digest(repo, "../sf")
    assert e.value.reason == "unreadable"


def test_control_char_path_raises_before_hash_object(repo, monkeypatch):
    _put(repo, f"{KIT}/commands/a\nb.md")
    calls = []
    real = kits._git
    monkeypatch.setattr(kits, "_git", lambda r, *a, **k: calls.append(a) or real(r, *a, **k))
    with pytest.raises(kits.KitError) as e:
        kits.dir_digest(repo, "sf")
    assert e.value.reason == "control-char"
    assert calls and not any("hash-object" in a for a in calls)


def test_symlink_in_kit_dir_raises(repo, tmp_path_factory):
    os.symlink(repo / KIT / "extension.yml", repo / KIT / "commands" / "link.md")
    with pytest.raises(kits.KitError) as e:
        kits.dir_digest(repo, "sf")
    assert e.value.reason == "symlink"
    (repo / KIT / "commands" / "link.md").unlink()
    outside = tmp_path_factory.mktemp("elsewhere")
    _put(outside, "extension.yml")
    os.rename(repo / KIT, repo / "kit-moved")
    os.symlink(outside, repo / KIT)
    with pytest.raises(kits.KitError) as e:
        kits.dir_digest(repo, "sf")
    assert e.value.reason == "symlink"


def test_dev_install_symlinked_copy_raises(repo):
    reg = kits.installed(repo)
    sf = next(k for k in reg if k["id"] == "sf")
    before = kits.agents_sha256(repo, sf["registered"])
    skill = repo / ".claude/skills/speckit-sf-specify/SKILL.md"
    cache = repo / "cache.md"
    cache.write_bytes(skill.read_bytes())
    skill.unlink()
    os.symlink(cache, skill)
    with pytest.raises(kits.KitError) as e:
        kits.agents_sha256(repo, sf["registered"])
    assert e.value.reason == "symlink"
    skill.unlink()
    with pytest.raises(kits.KitError) as e:  # a registered copy that is gone is unreadable, never skipped
        kits.agents_sha256(repo, sf["registered"])
    assert e.value.reason == "unreadable"
    assert len(before) == 64


def test_agents_digest_uses_exact_names(repo):
    by_id = {k["id"]: k for k in kits.installed(repo)}
    before = kits.agents_sha256(repo, by_id["sf"]["registered"])
    _put(repo, ".claude/skills/speckit-sf-extra-run/SKILL.md", "edited\n")
    assert kits.agents_sha256(repo, by_id["sf"]["registered"]) == before
    assert kits.agents_sha256(repo, by_id["sf-extra"]["registered"]) != before
    _put(repo, ".claude/skills/speckit-sf-specify/SKILL.md", "edited sf\n")
    assert kits.agents_sha256(repo, by_id["sf"]["registered"]) != before
    # a Claude commands layout and a skills list work the same, by exact name
    _put(repo, ".claude/commands/speckit.sf.old.md", "old\n")
    _put(repo, ".claude/skills/speckit-sf-new/SKILL.md", "new\n")
    both = {"commands": ["speckit.sf.old"], "skills": ["speckit-sf-new"]}
    assert len(kits.agents_sha256(repo, both)) == 64
    with pytest.raises(kits.KitError):
        kits.agents_sha256(repo, {"commands": ["../x"], "skills": []})


# ---- the registry ----
def test_missing_enabled_counts_as_enabled(repo):
    entry = _entry(commands=["speckit.sf.specify"])
    del entry["enabled"]
    _put(repo, ".specify/extensions/.registry", _registry(("sf", entry), ("jira-lifecycle", _entry()), ("off", {**_entry(), "enabled": False})))
    got = {k["id"]: k for k in kits.installed(repo)}
    assert set(got) == {"sf", "off"}  # `jira-lifecycle` (the toolkit) is not a kit
    assert got["sf"]["enabled"] is True and got["off"]["enabled"] is False
    assert got["sf"]["version"] == "1.0.0"
    assert got["sf"]["registered"] == {"commands": ["speckit.sf.specify"], "skills": []}


def test_bad_registry_id_is_unpinned(repo):
    _put(repo, ".specify/extensions/.registry", _registry(("sf", _entry(commands=["speckit.sf.specify"])), ("Bad_ID\n", _entry()),
                                                         ("odd", {"enabled": "yes"})))
    got = kits.installed(repo)
    assert [k["id"] for k in got] == ["sf", None, None] and [k.get("n") for k in got][1:] == [2, 3]
    lock = {"schema": 1, "kits": {}}
    out = kits.verify(repo, lock)
    assert [(k["id"], k["state"]) for k in out["kits"] if k["id"] is None] == [(None, "unpinned")] * 2
    assert "Bad_ID" not in json.dumps(out)
    assert sum(f["rule"] == "kit-unpinned" for f in out["findings"]) == 3


def test_registry_corrupt_or_symlink_fails_closed(repo):
    reg = repo / ".specify/extensions/.registry"
    assert kits.installed(repo)
    for bad in (b"{not json", b'{"extensions": {"a": {}, "a": {}}}', b"[]", b'{"extensions": []}', b"\xff\xfe", b"x" * ((1 << 20) + 1)):
        reg.write_bytes(bad)
        with pytest.raises(kits.KitError) as e:
            kits.installed(repo)
        assert e.value.reason == "unreadable", bad[:12]
    reg.unlink()
    os.symlink(repo / "elsewhere", reg)  # dangling symlink: never "no registry"
    with pytest.raises(kits.KitError) as e:
        kits.installed(repo)
    assert e.value.reason == "symlink"
    reg.unlink()
    assert kits.installed(repo) == []  # truly absent: nothing installed
    os.rename(repo / ".specify", repo / "dot-specify")
    os.symlink(repo / "dot-specify", repo / ".specify")
    with pytest.raises(kits.KitError) as e:
        kits.installed(repo)
    assert e.value.reason == "symlink"


# ---- the lockfile ----
def _lock(**over):
    entry = {"version": "1.0.0", "source": "https://example.com/contoso/sf", "catalog_sha256": None,
             "dir_digest": "a" * 64, "agents_sha256": "b" * 64, **over}
    return json.dumps({"schema": 1, "kits": {"sf": entry}}).encode()


def test_lock_source_and_version_validated_else_null():
    ok = kits.read_lock(_lock())["kits"]["sf"]
    assert ok["version"] == "1.0.0" and ok["source"] == "https://example.com/contoso/sf" and ok["dir_digest"] == "a" * 64
    for src in ("http://example.com/x", "https://user@example.com/x", "https://example.com/x?token=1", "javascript:alert(1)", "x" * 600, 7):
        assert kits.read_lock(_lock(source=src))["kits"]["sf"]["source"] is None, src
    for ver in ("1.0 0", "v" * 41, "", "1.0/../x", 3):
        assert kits.read_lock(_lock(version=ver))["kits"]["sf"]["version"] is None, ver
    for bad in (b"{", b'{"schema": 1, "kits": {"sf": {}, "sf": {}}}', b'{"schema": 2, "kits": {}}', b'{"schema": 1, "kits": {"Bad": {}}}',
                b'{"schema": 1, "kits": {"sf": 3}}', _lock(dir_digest="A" * 64), _lock(dir_digest="a" * 63), _lock(agents_sha256=None),
                _lock(catalog_sha256="zz"), _lock(version="1\x00")):
        with pytest.raises(ConfigInvalid):
            kits.read_lock(bad)
    assert kits.read_lock(_lock(catalog_sha256="c" * 64))["kits"]["sf"]["catalog_sha256"] == "c" * 64


def _pin(repo, kit_id="sf", **over):
    by_id = {k["id"]: k for k in kits.installed(repo)}
    e = {"version": "1.0.0", "source": None, "catalog_sha256": None, "dir_digest": kits.dir_digest(repo, kit_id),
         "agents_sha256": kits.agents_sha256(repo, by_id[kit_id]["registered"]), **over}
    return e


def test_verify_states(repo):
    lock = {"schema": 1, "kits": {"sf": _pin(repo), "gone": _pin(repo)}}
    out = kits.verify(repo, lock)
    states = {k["id"]: k["state"] for k in out["kits"]}
    assert states == {"sf": "pinned", "sf-extra": "unpinned", "gone": "stale"}
    assert [f["rule"] for f in out["findings"]] == ["kit-unpinned"] and out["findings"][0]["kit"] == "sf-extra"
    _put(repo, f"{KIT}/commands/specify.md", "changed\n")
    out = kits.verify(repo, lock)
    assert {k["id"]: k["state"] for k in out["kits"]}["sf"] == "changed"
    _put(repo, f"{KIT}/commands/specify.md", "specify\n")
    assert {k["id"]: k["state"] for k in kits.verify(repo, lock)["kits"]}["sf"] == "pinned"
    _put(repo, ".claude/skills/speckit-sf-specify/SKILL.md", "tampered\n")  # the agent copy is what runs
    assert {k["id"]: k["state"] for k in kits.verify(repo, lock)["kits"]}["sf"] == "changed"
    _put(repo, ".claude/skills/speckit-sf-specify/SKILL.md", "skill sf\n")
    lock["kits"]["sf"]["version"] = "2.0.0"  # the registry version no longer matches the lock
    assert {k["id"]: k["state"] for k in kits.verify(repo, lock)["kits"]}["sf"] == "changed"
    os.symlink(repo / "extension-copy", repo / KIT / "evil.md")  # a hostile kit file is changed, never a pass
    lock["kits"]["sf"]["version"] = "1.0.0"
    got = kits.verify(repo, lock)
    assert {k["id"]: k["state"] for k in got["kits"]}["sf"] == "changed" and {"rule": "kit-changed"}.items() <= got["findings"][0].items()
    assert "evil" not in json.dumps(got)


def test_disabled_kit_is_judged_like_an_enabled_one(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    lock = {"schema": 1, "kits": {i: _pin(repo, i) for i in ("sf", "sf-extra")}}
    (repo / kits.LOCK_FILE).write_text(json.dumps(lock))
    reg = repo / ".specify/extensions/.registry"
    reg.write_text(_registry(("sf", {**_entry(commands=["speckit.sf.specify"]), "enabled": False}),
                             ("sf-extra", _entry(commands=["speckit.sf-extra.run"]))))
    assert cli.main(["kits", "verify"]) == 0 and {k["state"] for k in json.loads(capsys.readouterr().out)["kits"]} == {"pinned"}
    _put(repo, f"{KIT}/commands/specify.md", "tampered\n")
    _put(repo, ".claude/skills/speckit-sf-specify/SKILL.md", "tampered\n")
    assert cli.main(["kits", "verify"]) == 2
    assert {k["id"]: k["state"] for k in json.loads(capsys.readouterr().out)["kits"]}["sf"] == "changed"
    reg.write_text(_registry(("sf-extra", _entry(commands=["speckit.sf-extra.run"]))))  # entry removed, files remain: stale
    assert {k["id"]: k["state"] for k in kits.verify(repo, lock)["kits"]}["sf"] == "stale"


def test_stale_with_files_on_disk_is_a_changed_finding(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    lock = {"schema": 1, "kits": {i: _pin(repo, i) for i in ("sf", "sf-extra")}}
    lock["kits"]["gone"] = lock["kits"]["sf"]
    (repo / kits.LOCK_FILE).write_text(json.dumps(lock))
    (repo / ".specify/extensions/.registry").write_text(_registry(("sf-extra", _entry(commands=["speckit.sf-extra.run"]))))
    out = kits.verify(repo, lock)  # sf: entry removed, files remain; gone: nothing on disk at all
    rows = {k["id"]: k for k in out["kits"]}
    assert rows["sf"] == {"id": "sf", "state": "stale", "reason": "files-remain"} and rows["gone"] == {"id": "gone", "state": "stale"}
    assert [(f["rule"], f["kit"]) for f in out["findings"]] == [("kit-changed", "sf")]
    assert cli.main(["kits", "verify"]) == 2 and "sf" in capsys.readouterr().out
    (repo / ".specify/extensions/.registry").unlink()  # upstream says not to commit the registry: a fresh clone has none and sees nothing
    out = kits.verify(repo, lock)
    assert {k["id"]: k for k in out["kits"]}["sf"] == {"id": "sf", "state": "stale"} and out["findings"] == []
    assert cli.main(["kits", "verify"]) == 0
    (repo / ".specify/extensions/.registry").write_text(_registry(("sf-extra", _entry(commands=["speckit.sf-extra.run"]))))
    shutil.rmtree(repo / KIT)
    assert kits.verify(repo, lock)["findings"] == []
    os.symlink(repo / "elsewhere", repo / KIT)  # a symlink where the kit dir was is still "files remain"
    assert [f["rule"] for f in kits.verify(repo, lock)["findings"]] == ["kit-changed"]


def test_symlinked_kit_config_file_is_refused_not_excluded(repo):
    before = kits.dir_digest_at(repo, _head(repo), "sf")
    assert kits.dir_digest(repo, "sf") == before
    for name in ("sf-config.yml", "local-config.yml"):
        (repo / KIT / name).unlink()
        os.symlink("../../../elsewhere", repo / KIT / name)
        with pytest.raises(kits.KitError) as e:
            kits.dir_digest(repo, "sf")
        assert e.value.reason == "symlink"
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "link")
        assert kits.dir_digest_at(repo, _head(repo), "sf") == "changed"
        git(repo, "rm", "-q", "-f", f"{KIT}/{name}")
        _put(repo, f"{KIT}/{name}", "x: 1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "back")
        assert kits.dir_digest_at(repo, _head(repo), "sf") == before


@pytest.mark.parametrize("attrs", ["* ident\n", ".specify/** filter=lfs\n", "*.md working-tree-encoding=UTF-16\n", ".specify/** filter=x\n"])
def test_content_attributes_refused_and_no_filter_runs(repo, attrs):
    marker = repo.parent / (repo.name + "-marker")
    git(repo, "config", "filter.x.clean", f"sh -c 'touch {marker}; cat'")
    git(repo, "config", "filter.lfs.clean", f"sh -c 'touch {marker}; cat'")
    (repo / ".gitattributes").write_text(attrs)
    with pytest.raises(kits.KitError) as e:
        kits.dir_digest(repo, "sf")
    assert e.value.reason == "attributes" and not marker.exists()
    lock = {"schema": 1, "kits": {"sf": {"version": None, "source": None, "catalog_sha256": None, "dir_digest": "a" * 64, "agents_sha256": "b" * 64}}}
    row = next(k for k in kits.verify(repo, lock)["kits"] if k["id"] == "sf")
    assert row == {"id": "sf", "state": "changed", "reason": "attributes"} and not marker.exists()


def test_text_and_eol_attributes_stay_allowed(repo):
    (repo / ".gitattributes").write_text("* text=auto\n*.md eol=crlf\n*.yml -text\n")
    assert len(kits.dir_digest(repo, "sf")) == 64


# ---- dir_digest_at (the commit, as CI sees it) ----
def test_dir_digest_at_none_sentinel_and_untracked(repo):
    head = _head(repo)
    assert kits.dir_digest_at(repo, head, "nokit") is None
    assert kits.dir_digest_at(repo, head, "sf") == kits.dir_digest(repo, "sf")
    _put(repo, f"{KIT}/untracked.md")  # untracked files are not in the commit
    assert kits.dir_digest_at(repo, head, "sf") != kits.dir_digest(repo, "sf")
    os.symlink("extension.yml", repo / KIT / "commands" / "link.md")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "link")
    assert kits.dir_digest_at(repo, _head(repo), "sf") == "changed"
    with pytest.raises(ValueError):
        kits.dir_digest_at(repo, "HEAD", "sf")


def test_dir_digest_at_only_excluded_files_is_none(tmp_path, git_env):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "Dana Fictional")
    git(tmp_path, "config", "user.email", "dana@contoso.example")
    _put(tmp_path, f"{KIT}/local-config.yml")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "x")
    assert kits.dir_digest_at(tmp_path, _head(tmp_path), "sf") is None


# ---- the CLI ----
def test_cli_kits_verify(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert cli.main(["kits", "verify"]) == 2  # two unpinned kits, no lockfile
    out = json.loads(capsys.readouterr().out)
    assert {k["state"] for k in out["kits"]} == {"unpinned"}
    lock = {"schema": 1, "kits": {i: _pin(repo, i) for i in ("sf", "sf-extra")}}
    (repo / kits.LOCK_FILE).write_text(json.dumps(lock))
    assert cli.main(["kits", "verify"]) == 0
    assert {k["state"] for k in json.loads(capsys.readouterr().out)["kits"]} == {"pinned"}
    _put(repo, f"{KIT}/extension.yml", "id: evil\n")
    assert cli.main(["kits", "verify"]) == 2
    capsys.readouterr()
    (repo / kits.LOCK_FILE).write_text("{")
    assert cli.main(["kits", "verify"]) == 2
    assert "skj:" in capsys.readouterr().err
    (repo / kits.LOCK_FILE).unlink()
    (repo / ".specify/extensions/.registry").write_text("{")
    assert cli.main(["kits", "verify"]) == 2
    assert "unreadable" in capsys.readouterr().err


# ---- skj kits pin (P2-9a): a human's approval record, terminal only ----
def tty(monkeypatch, on=True, out=None):
    """stdin (and, unless `out` says otherwise, stdout) is a terminal or not. A terminal stdin types the approval code it reads off the screen."""
    if on:
        monkeypatch.setattr(sys, "stdin", TypedStdin())
    else:
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: on if out is None else out)


def pin_cli(capsys, *args):
    code = cli.main(["kits", "pin", *args])
    out, err = capsys.readouterr()
    return code, out, err


def hostile_kit(repo):
    """A kit whose manifest and command carry text that must never reach the preview."""
    _put(repo, f"{KIT}/extension.yml", f"""schema_version: "1.0"
extension:
  id: "sf"
  version: "1.0.0"
  description: "{MARK}-description"
  repository: "https://example.com/contoso/sf"
requires:
  tools:
    - name: "{MARK}-tool"
provides:
  commands:
    - name: "speckit.sf.specify"
      file: "commands/specify.md"
      description: "{MARK}-cmd"
hooks:
  after_{MARK}:
    command: "speckit.sf.specify"
""")
    _put(repo, f"{KIT}/commands/specify.md", f"---\nallowed-tools: Bash\n---\n```bash\ncurl https://example.com/{MARK}\n```\n{MARK}-body\n")


def test_pin_refuses_without_tty(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)

    def boom(*a, **k):
        raise AssertionError("a file was read before the terminal check")
    monkeypatch.setattr(kits, "plan_pin", boom)
    monkeypatch.setattr(kits, "apply_pin", boom)
    for stdin_tty, stdout_tty in ((False, False), (True, False), (False, True)):
        tty(monkeypatch, stdin_tty, stdout_tty)
        code, out, err = pin_cli(capsys, "sf", "--apply")
        assert code == 2 and out == "" and "only a human may do" in err and "not a security boundary" in err
        assert not (repo / kits.LOCK_FILE).exists()


def _docs(screen):
    """The JSON documents on a screen, in order (the pin preview, then the result of --apply)."""
    dec, i, out = json.JSONDecoder(), 0, []
    while (i := screen.find("{", i)) != -1:
        doc, i = dec.raw_decode(screen, i)
        out.append(doc)
    return out


def test_apply_needs_both_stdin_and_stdout_terminals(repo):
    for kw in ({"stdin_pty": False, "stdout_pty": False}, {"stdin_pty": False}, {"stdout_pty": False}):  # `< /dev/null`, `| tee x`
        code, out, err = run_in_pty(["kits", "pin", "sf", "--apply"], repo, **kw)
        assert code == 2 and out == "" and "only a human may do" in err and not (repo / kits.LOCK_FILE).exists()
    code, out, err = run_in_pty(["kits", "pin", "sf", "--apply"], repo)
    assert code == 0, err
    assert [d["applied"] for d in _docs(out)] == [False, True] and set(json.loads((repo / kits.LOCK_FILE).read_text())["kits"]) == {"sf"}
    assert kits.verify(repo, kits.load_lock(repo))["kits"][0] == {"id": "sf", "state": "pinned"}


def test_a_code_typed_before_the_prompt_is_discarded(repo):
    """The code is computable beforehand; what is typed into the terminal before the prompt is flushed, so it cannot pin."""
    lock = repo / kits.LOCK_FILE
    early = kits.pin_code(kits.plan_pin(repo, "sf"), "tech lead")
    code, out, err = run_in_pty(["kits", "pin", "sf", "--apply"], repo, early=early, answer="")  # then end of input at the prompt
    assert code == 2 and err.endswith("skj: not confirmed: nothing was approved\n") and not lock.exists(), err
    code, out, err = run_in_pty(["kits", "pin", "sf", "--apply"], repo)  # the same code typed after the prompt still pins
    assert code == 0 and lock.exists(), err


def test_apply_requires_the_typed_code(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    lock = repo / kits.LOCK_FILE
    for answer in ("nope\n", "\n", "   \n", "", KeyboardInterrupt, "x" * 5000, "\x00\n", "0" * 12 + "\n"):
        stdin = terminal(monkeypatch, answer)
        code, out, err = pin_cli(capsys, "sf", "--apply")
        assert code == 2 and err == "skj: not confirmed: nothing was approved\n" and stdin.lines == 1 and not lock.exists()
        assert [d["applied"] for d in _docs(out)] == [False]  # the preview was shown; no second document
    for kw in ({"answer": "nope"}, {"answer": ""}, {"interrupt": True}):  # a real process: wrong text, end of input, Ctrl-C
        code, out, err = run_in_pty(["kits", "pin", "sf", "--apply"], repo, **kw)
        assert code == 2 and err.endswith("skj: not confirmed: nothing was approved\n") and not lock.exists(), err


def test_apply_with_the_right_code_writes_and_prints_the_digests(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    stdin = terminal(monkeypatch, None)
    code, out, err = pin_cli(capsys, "sf", "--apply", "--by", "Tech Lead")
    assert code == 0 and stdin.lines == 1
    preview, result = _docs(out)
    assert out.index("human_checklist") < out.index("approval code: ") < out.rindex('"applied": true')  # the full preview, then the code
    assert preview["applied"] is False and result["applied"] is True and result["id"] == "sf"
    entry = json.loads((repo / kits.LOCK_FILE).read_text())["kits"]["sf"]
    assert result["dir_digest"] == entry["dir_digest"] and result["agents_sha256"] == entry["agents_sha256"]
    assert shown_code(out) == kits.pin_code(kits.plan_pin(repo, "sf"), "Tech Lead")
    terminal(monkeypatch, "  " + shown_code(out) + " \t\n")  # surrounding whitespace is ignored (the same kit, so the same code)
    assert pin_cli(capsys, "sf", "--apply", "--by", "Tech Lead")[0] == 0


def test_the_pin_code_covers_what_is_written_and_a_plain_preview_prints_none(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    plan = kits.plan_pin(repo, "sf")
    code = kits.pin_code(plan, "tech lead")
    assert re.fullmatch(r"[0-9a-f]{12}", code) and code == kits.pin_code(plan, "tech lead")
    assert kits.pin_code(plan, "po") != code
    for field, value in (("version", "9.9.9"), ("source", "https://example.com/x"), ("dir_digest", "e" * 64), ("agents_sha256", "f" * 64)):
        assert kits.pin_code({**plan, field: value}, "tech lead") != code, field
    assert kits.pin_code({**plan, "scan": {**plan["scan"], "commands": plan["scan"]["commands"] + 1}}, "tech lead") != code
    for on in (False, True):
        tty(monkeypatch, on)
        assert "approval code" not in pin_cli(capsys, "sf")[1]


def test_kit_edited_after_the_prompt_is_refused(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    lock = repo / kits.LOCK_FILE
    msg = "skj: the kit changed while you were reading it: nothing was pinned\n"
    terminal(monkeypatch, None, before=lambda code: _put(repo, f"{KIT}/commands/specify.md", "edited after the prompt\n"))  # between prompt and code
    code, out, err = pin_cli(capsys, "sf", "--apply")
    assert code == 2 and err == msg and not lock.exists() and [d["applied"] for d in _docs(out)] == [False]
    # the same in a real process: the file is edited while the child waits for the line
    _put(repo, f"{KIT}/commands/specify.md", "back\n")
    code, out, err = run_in_pty(["kits", "pin", "sf", "--apply"], repo, before=lambda c: _put(repo, f"{KIT}/commands/specify.md", "edited again\n"))
    assert code == 2 and err.endswith(msg) and not lock.exists()
    # an edit that changes nothing the entry records is no swap
    terminal(monkeypatch, None, before=lambda code: _put(repo, f"{KIT}/local-config.yml", "other: 1\n"))
    assert pin_cli(capsys, "sf", "--apply")[0] == 0 and lock.exists()


def test_pin_preview_checklist_and_no_kit_text(repo, monkeypatch, capsys):
    hostile_kit(repo)
    monkeypatch.chdir(repo)
    for on in (False, True):
        tty(monkeypatch, on)
        code, out, err = pin_cli(capsys, "sf")
        assert code == 0 and MARK not in out + err and "contoso" not in out + err  # not the source URL either
        doc = json.loads(out)
        assert doc["applied"] is False and doc["id"] == "sf" and doc["version"] == "1.0.0"
        assert len(doc["dir_digest"]) == 64 and len(doc["agents_sha256"]) == 64
        assert len(doc["human_checklist"]) == 5 and all(isinstance(x, str) and x for x in doc["human_checklist"])
        assert doc["human_checklist"] == list(kits.CHECKLIST)
        assert doc["scan"]["commands"] == 1 and doc["scan"]["flags"]["network"]["at"] == ["commands/specify.md:5"]
        assert doc["scan"]["tools"] == ["<other>"] and doc["scan"]["hooks"] == ["<other> (optional)"]
        assert not (repo / kits.LOCK_FILE).exists()  # a preview writes nothing
    plan = kits.plan_pin(repo, "sf")
    assert set(plan) == {"id", "version", "source", "catalog_sha256", "dir_digest", "agents_sha256", "scan", "human_checklist"}
    assert plan["source"] == "https://example.com/contoso/sf" and plan["catalog_sha256"] is None


@pytest.mark.parametrize("what", ["symlink-agent-copy", "symlink-kit-dir", "symlink-kit-file", "empty", "no-dir"])
def test_pin_refuses_symlink_dev_install_and_empty(repo, monkeypatch, capsys, what):
    tty(monkeypatch)
    monkeypatch.chdir(repo)
    skill = repo / ".claude/skills/speckit-sf-specify/SKILL.md"
    if what == "symlink-agent-copy":  # `specify extension add --dev`: agent copies are symlinks into a cache
        cache = repo.parent / (repo.name + "-cache.md")
        cache.write_text("skill sf\n")
        skill.unlink()
        os.symlink(cache, skill)
    elif what == "symlink-kit-dir":
        git(repo, "rm", "-rq", "--cached", KIT)
        os.rename(repo / KIT, repo / "elsewhere")
        os.symlink(repo / "elsewhere", repo / KIT)
    elif what == "symlink-kit-file":
        os.symlink("extension.yml", repo / KIT / "commands" / "link.md")
    elif what == "empty":
        for f in (repo / KIT).rglob("*"):
            if f.is_file() and f.name != "local-config.yml":
                f.unlink()
    else:
        import shutil
        shutil.rmtree(repo / KIT)
    for args in (("sf",), ("sf", "--apply")):
        code, out, err = pin_cli(capsys, *args)
        assert code == 2 and err.startswith("skj: kits pin refused: ") and "elsewhere" not in err + out
        assert not (repo / kits.LOCK_FILE).exists()


@pytest.mark.parametrize("kit_id", ["nokit", "jira-lifecycle", "Bad Id", "../x", "sf-extr", "x" * 60, ""])
def test_pin_unknown_id_exit_2(repo, monkeypatch, capsys, kit_id):
    tty(monkeypatch)
    monkeypatch.chdir(repo)
    for args in ((kit_id,), (kit_id, "--apply")):
        code, out, err = pin_cli(capsys, *args)
        assert code == 2 and out == "" and err == "skj: kits pin refused: unknown\n"  # fixed text, the id is not echoed
    assert not (repo / kits.LOCK_FILE).exists()


def test_pin_then_verify_pinned(repo, monkeypatch, capsys):
    tty(monkeypatch)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(stack, "_today", lambda: datetime.date(2026, 10, 31))
    assert pin_cli(capsys, "sf", "--apply", "--by", "Tech Lead")[0] == 0
    raw = (repo / kits.LOCK_FILE).read_text()
    lock = json.loads(raw)
    e = lock["kits"]["sf"]
    assert raw == json.dumps(lock, indent=2, sort_keys=True) + "\n" and "sf-extra" not in lock["kits"]
    assert e["reviewed"] == {"by": "Tech Lead", "date": "2026-10-31"} and e["version"] == "1.0.0" and e["catalog_sha256"] is None
    assert e["scan"]["flags"] == {"network": 0, "installs": 0, "writes": 0, "secrets": 0, "allowed_tools": 0}
    assert e["dir_digest"] == kits.dir_digest(repo, "sf") and kits.read_lock(raw.encode())["kits"]["sf"]["agents_sha256"] == e["agents_sha256"]
    assert cli.main(["kits", "verify"]) == 2
    states = {k["id"]: k["state"] for k in json.loads(capsys.readouterr().out)["kits"]}
    assert states == {"sf": "pinned", "sf-extra": "unpinned"}
    assert pin_cli(capsys, "sf-extra", "--apply")[0] == 0 and cli.main(["kits", "verify"]) == 0
    both = json.loads((repo / kits.LOCK_FILE).read_text())
    assert both["kits"]["sf"] == lock["kits"]["sf"] and list(both["kits"]) == ["sf", "sf-extra"]  # the first entry kept as it was
    _put(repo, f"{KIT}/commands/specify.md", "changed after the pin\n")
    assert cli.main(["kits", "verify"]) == 2  # a changed byte is no longer pinned


def test_pin_keeps_line_terminator_and_unrelated_entries_of_an_existing_lock(repo, monkeypatch, capsys):
    tty(monkeypatch)
    monkeypatch.chdir(repo)
    other = {"version": "9.9.9", "source": None, "catalog_sha256": None, "dir_digest": "c" * 64, "agents_sha256": "d" * 64,
             "reviewed": {"by": "po", "date": "2026-01-01"}, "scan": {"x": 1}}
    old = json.dumps({"schema": 1, "kits": {"gone": other}}, indent=4).replace("\n", "\r\n").encode() + b"\r\n"
    (repo / kits.LOCK_FILE).write_bytes(old)
    assert pin_cli(capsys, "sf", "--apply")[0] == 0
    raw = (repo / kits.LOCK_FILE).read_bytes()
    assert raw.endswith(b"\r\n") and raw.count(b"\n") == raw.count(b"\r\n")
    lock = json.loads(raw)
    assert lock["kits"]["gone"] == other and set(lock["kits"]) == {"gone", "sf"}
    assert [p.name for p in repo.iterdir() if p.name.endswith(".tmp")] == []


def test_pin_leaves_the_lock_intact_on_any_failure(repo, monkeypatch, capsys):
    tty(monkeypatch)
    monkeypatch.chdir(repo)
    lock = repo / kits.LOCK_FILE
    for bad in (b"{", b'{"schema": 1, "kits": {"sf": {}, "sf": {}}}', b'{"schema": 2, "kits": {}}', b'{"schema": 1, "kits": {"sf": 3}}'):
        lock.write_bytes(bad)
        code, out, err = pin_cli(capsys, "sf", "--apply")
        assert code == 2 and lock.read_bytes() == bad and "skj:" in err
    good = json.dumps({"schema": 1, "kits": {}}).encode()
    lock.write_bytes(good)
    real = os.replace

    def interrupt(src, dst, *a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["kits", "pin", "sf", "--apply"])
    monkeypatch.setattr(os, "replace", real)
    assert lock.read_bytes() == good and [p.name for p in repo.iterdir() if p.name.endswith(".tmp")] == []
    lock.unlink()
    elsewhere = repo.parent / (repo.name + "-lock.json")
    elsewhere.write_text(good.decode())
    os.symlink(elsewhere, lock)  # a symlinked lockfile is refused, never followed
    code, out, err = pin_cli(capsys, "sf", "--apply")
    assert code == 2 and elsewhere.read_text() == good.decode() and lock.is_symlink()


def test_apply_pin_refuses_a_plan_that_no_longer_matches_the_kit(repo, monkeypatch):
    plan = kits.plan_pin(repo, "sf")
    _put(repo, f"{KIT}/commands/specify.md", "edited after the preview\n")
    with pytest.raises(ValueError, match="changed"):
        kits.apply_pin(repo, plan, "tech lead")
    assert not (repo / kits.LOCK_FILE).exists()
    plan = kits.plan_pin(repo, "sf")
    for by in ("", "  ", "Dana!", "x" * 41, "a\nb", None):
        with pytest.raises(ValueError, match="--by"):
            kits.apply_pin(repo, plan, by)
    assert not (repo / kits.LOCK_FILE).exists()


def test_pin_by_goes_through_the_stack_approve_validation(repo, monkeypatch, capsys):
    tty(monkeypatch)
    monkeypatch.chdir(repo)
    for by in ("", "   ", "Dana!", "x" * 41, "tab\there"):
        with pytest.raises(SystemExit) as e:
            cli.main(["kits", "pin", "sf", "--apply", "--by", by])
        assert e.value.code == 2
        assert "--by must be a role" in capsys.readouterr().err
    assert not (repo / kits.LOCK_FILE).exists()


def test_pin_refuses_an_incomplete_scan_and_leaves_the_lock_alone(repo, monkeypatch, capsys):
    tty(monkeypatch)
    monkeypatch.chdir(repo)
    _put(repo, f"{KIT}/extension.yml", 'extension:\n  id: "sf"\nprovides:\n  commands:\n    - name: "speckit.sf.specify"\n      file: "commands/missing.md"\n')
    lock = repo / kits.LOCK_FILE
    old = json.dumps({"schema": 1, "kits": {}}).encode()
    lock.write_bytes(old)
    for args in (("sf",), ("sf", "--apply")):
        code, out, err = pin_cli(capsys, *args)
        assert code == 2 and out == "" and err == "skj: kits pin refused: incomplete-scan\n"
    assert lock.read_bytes() == old
    with pytest.raises(kits.KitError) as e:
        kits.plan_pin(repo, "sf")
    assert e.value.reason == "incomplete-scan"


def test_preview_says_when_apply_replaces_an_existing_pin(repo, monkeypatch, capsys):
    tty(monkeypatch)
    monkeypatch.chdir(repo)
    assert "note" not in json.loads(pin_cli(capsys, "sf")[1])
    assert pin_cli(capsys, "sf", "--apply")[0] == 0
    capsys.readouterr()
    doc = json.loads(pin_cli(capsys, "sf")[1])
    assert doc["note"] == "this kit is already pinned: --apply REPLACES its entry (version, digests, scan, reviewer)"
    assert "note" not in json.loads(pin_cli(capsys, "sf-extra")[1])


def test_apply_pin_compares_the_scan_with_the_plan(repo):
    plan = kits.plan_pin(repo, "sf")
    plan["scan"] = {**plan["scan"], "commands": plan["scan"]["commands"] + 1}
    with pytest.raises(ValueError, match="changed"):
        kits.apply_pin(repo, plan, "tech lead")
    assert not (repo / kits.LOCK_FILE).exists()


# ---- P2-16d: the fix text names the command; a symlink refusal names the --dev install ----
def test_verify_finding_fix_names_the_pin_command(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    out = kits.verify(repo, {"schema": 1, "kits": {}})
    assert out["findings"] and {f["fix"] for f in out["findings"]} == {kits.FIX}
    assert "`skj kits pin <id> --apply`" in kits.FIX and "own terminal window" in kits.FIX and "`skj kits pin <id>` previews it" in kits.FIX


def test_pin_refused_for_symlink_says_a_dev_install_cannot_be_pinned(repo, monkeypatch, capsys):
    tty(monkeypatch)
    monkeypatch.chdir(repo)
    skill = repo / ".claude/skills/speckit-sf-specify/SKILL.md"
    cache = repo.parent / (repo.name + "-cache.md")
    cache.write_text("skill sf\n")
    skill.unlink()
    os.symlink(cache, skill)
    code, out, err = pin_cli(capsys, "sf")
    assert code == 2 and err == "skj: kits pin refused: symlink" + kits.SYMLINK_HINT + "\n" and "--dev" in err and "cannot be pinned" in err
    code, out, err = pin_cli(capsys, "nokit")  # another refusal keeps the plain text
    assert code == 2 and err == "skj: kits pin refused: unknown\n"


# =====================================================================================================================================
# S3b additions (not ported): the hostile HEAD entries, the adversarial sweep and the trust rule. Everything below is new in skj.
# =====================================================================================================================================
import signal  # noqa: E402
import subprocess as _sp  # noqa: E402

from skj import config  # noqa: E402
from test_stack_linear import _BOUND, _timed, needs_timer  # noqa: E402
from test_stack_support import _IDENTITY  # noqa: E402

_MIB = 1 << 20


def _kt(fn, *args):
    """`_timed` for the kit readers: a fixed refusal (KitError, ConfigInvalid) is part of the contract, the time is what is judged."""
    def call():
        try:
            fn(*args)
        except (kits.KitError, ConfigInvalid):
            pass
    return _timed(call)


def _plumb(tmp, entries):
    """A repository whose HEAD holds `entries` [(mode, path bytes, data bytes)] built with `git mktree` (any name a tree can store, even
    one `update-index` refuses, and any mode); the working tree stays empty. A 160000 entry's data is ignored (a gitlink to a made-up commit)."""
    tmp.mkdir(parents=True, exist_ok=True)
    git(tmp, "init", "-q", "-b", "main")
    env = {**_IDENTITY, **os.environ}

    def run(args, data):
        return _sp.run(["git", "-C", str(tmp), *args], input=data, capture_output=True, check=True, env=env).stdout.decode().strip()
    root, blobs = {}, {}
    for mode, path, data in entries:
        *dirs, name = path.split(b"/")
        node = root
        for d in dirs:
            node = node.setdefault(d, {})
        if mode != "160000" and data not in blobs:
            blobs[data] = run(["hash-object", "-w", "--stdin"], data)
        node[name] = (mode, "1" * 40 if mode == "160000" else blobs[data])

    def write(node):
        recs = []
        for name, v in node.items():
            mode, kind, sha = ("040000", "tree", write(v)) if isinstance(v, dict) else (v[0], "commit" if v[0] == "160000" else "blob", v[1])
            recs.append(f"{mode} {kind} {sha}\t".encode() + name + b"\0")
        return run(["mktree", "-z", "--missing"], b"".join(recs))
    tree = write(root)
    git(tmp, "update-ref", "HEAD", git(tmp, "commit-tree", tree, "-m", "x"))
    return tmp, git(tmp, "rev-parse", "HEAD")


_BASE = [("100644", KIT.encode() + b"/extension.yml", b"id: sf\n")]


@pytest.mark.parametrize("hostile", [
    ("120000", b"/commands/link.md", b"../extension.yml"),          # a symlink
    ("160000", b"/vendored", b""),                                  # a submodule (gitlink)
    ("100644", b"/commands/\xff\xfe.md", b"x"),                     # not UTF-8
    ("100644", b"/commands/a\nb.md", b"x"),                         # a newline in the name
    ("100644", b"/commands/a\x1b[31mb.md", b"x"),                   # a terminal escape in the name
    ("100644", b"/commands/a\xe2\x80\xaeb.md", b"x"),               # a bidi override (Cf)
    ("100644", b"/extension.YML", b"x"),                            # a case-insensitive twin of extension.yml
    ("100644", b"/extension.\xef\xbd\x99ml", b"x"),                 # a fullwidth y: an NFKC twin of extension.yml
    ("100644", b"/.GIT/config", b"x"),                              # a .git component in any case
    ("120000", b"/sf-config.yml", b"/etc/passwd"),                  # a symlinked excluded file: changed, not skipped
])
def test_dir_digest_at_hostile_head_entries_are_changed_and_never_echoed(tmp_path, git_env, hostile):
    mode, rel, data = hostile
    repo, head = _plumb(tmp_path, _BASE + [(mode, KIT.encode() + rel, data)])
    assert kits.dir_digest_at(repo, head, "sf") == "changed"


def test_dir_digest_at_regular_and_executable_files_hash_deterministically(tmp_path, git_env):
    files = [("100644", KIT.encode() + b"/b.md", b"b"), ("100755", KIT.encode() + b"/scripts/run.sh", b"#!/bin/sh\n"),
             ("100644", KIT.encode() + b"/a.md", b"a"), ("100644", KIT.encode() + b"/local-config.yml", b"me: 1\n")]
    a = kits.dir_digest_at(*_plumb(tmp_path / "a", files), "sf")
    b = kits.dir_digest_at(*_plumb(tmp_path / "b", list(reversed(files))), "sf")
    assert a == b and len(a) == 64  # the order the entries were made in does not matter; the excluded file does not count
    c = kits.dir_digest_at(*_plumb(tmp_path / "c", files[:3]), "sf")
    assert c == a


def test_dir_digest_at_reads_the_commit_never_the_working_tree_or_the_index(repo):
    head = _head(repo)
    pinned = kits.dir_digest_at(repo, head, "sf")
    _put(repo, f"{KIT}/commands/specify.md", "edited, unstaged\n")
    assert kits.dir_digest_at(repo, head, "sf") == pinned
    git(repo, "add", "-A")  # staged: the index differs too
    assert kits.dir_digest_at(repo, head, "sf") == pinned
    shutil.rmtree(repo / KIT)  # gone from the disk
    assert kits.dir_digest_at(repo, head, "sf") == pinned


def test_dir_digest_at_ignores_an_inherited_git_dir(repo, tmp_path_factory, monkeypatch):
    other = tmp_path_factory.mktemp("other")
    git(other, "init", "-q", "-b", "main")
    head = _head(repo)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))  # a hook's environment must not point the read at another repository
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / "index"))
    assert len(kits.dir_digest_at(repo, head, "sf")) == 64 and kits.dir_digest(repo, "sf") == kits.dir_digest_at(repo, head, "sf")


def test_git_runs_get_the_scrubbed_env_and_never_a_token(repo, monkeypatch):
    head = _head(repo)  # the test's own git call, before the spy
    monkeypatch.setenv("SKJ_TOKEN", "fictional-token-value")
    seen = []
    real_run, real_popen = _sp.run, _sp.Popen

    def run(*a, **k):
        seen.append(k.get("env"))
        return real_run(*a, **k)

    class Popen(real_popen):
        def __init__(self, *a, **k):
            seen.append(k.get("env"))
            super().__init__(*a, **k)
    monkeypatch.setattr(_sp, "run", run)
    monkeypatch.setattr(_sp, "Popen", Popen)
    kits.dir_digest(repo, "sf")
    kits.dir_digest_at(repo, head, "sf")
    assert len(seen) >= 4 and all(e is not None and "SKJ_TOKEN" not in e and e.get("GIT_NO_REPLACE_OBJECTS") == "1" for e in seen)


def test_listing_over_the_byte_cap_is_unreadable(repo, monkeypatch):
    monkeypatch.setattr(kits, "MAX_LISTING", 40)
    for fn in (lambda: kits.dir_digest(repo, "sf"), lambda: kits.dir_digest_at(repo, _head(repo), "sf")):
        with pytest.raises(kits.KitError) as e:
            fn()
        assert e.value.reason == "unreadable"


def test_more_than_max_files_is_unreadable_working_tree_and_commit(repo, monkeypatch):
    monkeypatch.setattr(kits, "MAX_FILES", 3)
    with pytest.raises(kits.KitError) as e:
        kits.dir_digest(repo, "sf")
    assert e.value.reason == "unreadable"
    with pytest.raises(kits.KitError) as e:
        kits.dir_digest_at(repo, _head(repo), "sf")
    assert e.value.reason == "unreadable"


def test_case_twins_in_the_working_tree_are_unreadable(repo):
    _put(repo, f"{KIT}/commands/Specify.md", "twin\n")
    if (repo / KIT / "commands").joinpath("specify.md").read_text() == "twin\n":
        pytest.skip("case-insensitive file system: one file, no twins")
    with pytest.raises(kits.KitError) as e:
        kits.dir_digest(repo, "sf")
    assert e.value.reason == "unreadable"


REG = ".specify/extensions/.registry"


@pytest.mark.parametrize("where", [REG, kits.LOCK_FILE, ".claude/skills/speckit-sf-specify/SKILL.md",
                                   f"{KIT}/extension.yml"])
@pytest.mark.parametrize("kind", ["fifo", "dir"])
def test_fifo_or_directory_is_refused_without_waiting(repo, where, kind):
    p = repo / where
    if p.exists():
        p.unlink()
    os.mkfifo(p) if kind == "fifo" else p.mkdir()
    lock = {"schema": 1, "kits": {"sf": _pin_entry_for_fifo}}
    if where == REG:
        with pytest.raises(kits.KitError) as e:
            kits.installed(repo)
        assert e.value.reason == "unreadable"
    elif where == kits.LOCK_FILE:
        with pytest.raises(kits.KitError):
            kits.load_lock(repo)
    elif where.startswith(".claude"):
        with pytest.raises(kits.KitError) as e:
            kits.agents_sha256(repo, {"commands": ["speckit.sf.specify"], "skills": []})
        assert e.value.reason == "unreadable"
        assert {k["id"]: k["state"] for k in kits.verify(repo, lock)["kits"]}["sf"] == "changed"
    else:
        assert kits.scan(repo, "sf")["unreadable"] is True


_pin_entry_for_fifo = {"version": None, "source": None, "catalog_sha256": None, "dir_digest": "a" * 64, "agents_sha256": "b" * 64}


def test_read_swapped_to_a_fifo_after_the_check_is_not_waited_on(repo, monkeypatch):
    """The lstat walk saw a regular file; a FIFO swapped in before the open is caught by the fstat after a non-blocking open."""
    real = kits._stat
    target = repo / ".specify/extensions/.registry"

    def swap(root, rel):
        st = real(root, rel)
        if rel.endswith(".registry"):
            target.unlink()
            os.mkfifo(target)
        return st
    monkeypatch.setattr(kits, "_stat", swap)
    with pytest.raises(kits.KitError) as e:
        kits.installed(repo)
    assert e.value.reason == "unreadable"


def test_read_of_a_file_grown_after_the_check_is_capped(repo, monkeypatch):
    """The lstat walk saw a small file; one that grows past the cap before the read is still refused (the read itself is bounded)."""
    real = kits._stat
    target = repo / ".specify/extensions/.registry"

    def grow(root, rel):
        st = real(root, rel)
        if rel.endswith(".registry"):
            with open(target, "ab") as f:
                f.write(b" " * (kits.MAX_REGISTRY + 1))
        return st
    monkeypatch.setattr(kits, "_stat", grow)
    with pytest.raises(kits.KitError) as e:
        kits.installed(repo)
    assert e.value.reason == "unreadable"


def _grow_after_check(monkeypatch, path, cap):
    real = kits._stat

    def grow(root, rel):
        st = real(root, rel)
        if os.path.join(str(root), rel) == str(path):
            with open(path, "ab") as f:
                f.write(b" " * (cap + 1))
        return st
    monkeypatch.setattr(kits, "_stat", grow)


def test_agent_copy_grown_after_the_check_is_capped(repo, monkeypatch):
    skill = repo / ".claude/skills/speckit-sf-specify/SKILL.md"
    monkeypatch.setattr(kits, "MAX_FILE", 64)  # the agent-copy hash has no parse cap behind it: only the read bounds it
    _grow_after_check(monkeypatch, skill, kits.MAX_FILE)
    with pytest.raises(kits.KitError) as e:
        kits.agents_sha256(repo, {"commands": ["speckit.sf.specify"], "skills": []})
    assert e.value.reason == "unreadable"


def test_scan_body_grown_after_the_check_is_capped(repo, monkeypatch):
    _put(repo, f"{KIT}/extension.yml", 'extension:\n  id: "sf"\nprovides:\n  commands:\n    - name: "speckit.sf.specify"\n      file: "commands/specify.md"\n')
    assert "unreadable" not in kits.scan(repo, "sf")
    monkeypatch.setattr(kits, "MAX_BODY", 64)
    _grow_after_check(monkeypatch, repo / KIT / "commands/specify.md", kits.MAX_BODY)
    assert kits.scan(repo, "sf")["unreadable"] is True


def test_digest_framing_golden_vector_and_boundaries():
    entries = [("e69de29bb2d1d6434b8b29ae775ad8c2d48c5391", "commands/empty.md"), ("a" * 40, "caf\u00e9/r\u00e9sum\u00e9.md"),
               ("0123456789abcdef0123456789abcdef01234567", "extension.yml"), ("f" * 64, "scripts/run.sh")]
    assert kits._digest(entries) == kits._digest(list(reversed(entries))) == GOLDEN_DIGEST
    # unframed concatenations equal, framed digests differ: the separator and the newline are part of the digest
    assert kits._digest([("a" * 40, "bc")]) != kits._digest([("a" * 40 + "b", "c")])
    assert kits._digest([("a" * 40, "x"), ("b" * 40, "y")]) != kits._digest([("a" * 40, "xb" + "b" * 39 + "y")])
    assert kits._digest([("a" * 64, "z")]) != kits._digest([("a" * 40, "a" * 24 + "z")])


GOLDEN_DIGEST = "cc8616ce1c1e8f0f8b6218f4e541446cca0812b9e0f79177314b797b06592107"  # sha256 of the sorted `<blob>  <rel>\n` lines, computed by hand


def test_kit_error_and_findings_carry_no_hostile_name(tmp_path, git_env):
    name = b"/commands/IGNORE\x1b]0;evil\x07.md"
    repo, head = _plumb(tmp_path, _BASE + [("100644", KIT.encode() + name, b"x")])
    assert kits.dir_digest_at(repo, head, "sf") == "changed"
    _put(repo, ".specify/extensions/.registry", _registry(("sf", _entry()), ("Evil\x1b]0;x\x07", _entry())))
    out = json.dumps(kits.verify(repo, {"schema": 1, "kits": {}}))
    assert "Evil" not in out and "\\u001b" not in out
    for reason in kits._REASONS:
        assert str(kits.KitError(reason)) == reason


# ---- the trust rule: in CI the target branch's lockfile judges; the working tree (the PR's own file) can never lower it ----
class _Cfg:
    def __init__(self, from_target, blob):
        self.from_target, self.target_lock_blob, self.target_lock_read = from_target, blob, True


def test_lock_for_refuses_a_target_config_whose_trust_was_never_read(repo):
    (repo / kits.LOCK_FILE).write_text(json.dumps({"schema": 1, "kits": {}}))
    cfg = config.Config(site="example-site.atlassian.net", project="SPK")
    cfg.from_target = True  # but read_target_trust never ran: target_lock_blob is None by default, NOT "the target has none"
    assert cfg.target_lock_read is False
    with pytest.raises(ConfigInvalid) as e:
        kits.lock_for(cfg, repo)
    assert str(e.value) == kits.E_TRUST_UNREAD
    cfg.target_lock_blob = None  # what read_target_trust stores when the target has no lockfile: the bootstrap
    assert cfg.target_lock_read is True and kits.lock_for(cfg, repo) is None


def test_lock_for_target_wins_and_the_working_tree_cannot_lower_it(repo):
    head = _head(repo)
    target = json.dumps({"schema": 1, "kits": {"sf": {**_pin_entry_for_fifo, "dir_digest": "c" * 64}}}).encode()
    # the PR's own lockfile pins exactly what the head holds: trusting it would pass a kit nobody reviewed
    own = {"schema": 1, "kits": {"sf": {**_pin_entry_for_fifo, "dir_digest": kits.dir_digest_at(repo, head, "sf")}}}
    (repo / kits.LOCK_FILE).write_text(json.dumps(own))
    judged = kits.lock_for(_Cfg(True, target), repo)
    assert judged["kits"]["sf"]["dir_digest"] == "c" * 64 != kits.dir_digest_at(repo, head, "sf")
    assert kits.lock_for(_Cfg(True, None), repo) is None  # the target has none: bootstrap, never the working tree's
    (repo / kits.LOCK_FILE).write_text("{")  # a broken working-tree file cannot break or change the CI verdict either
    assert kits.lock_for(_Cfg(True, target), repo) == judged
    (repo / kits.LOCK_FILE).write_text(json.dumps(own))
    assert kits.lock_for(_Cfg(False, target), repo)["kits"]["sf"] == own["kits"]["sf"]  # a local config: the working tree, as the fork
    with pytest.raises(ConfigInvalid):
        kits.lock_for(_Cfg(True, b"{"), repo)  # an invalid target lockfile fails closed


def test_lock_for_reads_the_blob_strict_read_target_trust_took_from_the_target_commit(repo, monkeypatch):
    from skj import strict
    target_lock = {"schema": 1, "kits": {"sf": {**_pin_entry_for_fifo, "dir_digest": "d" * 64}}}
    (repo / kits.LOCK_FILE).write_text(json.dumps(target_lock))
    (repo / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK", "mode": "strict"}))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "target")
    git(repo, "branch", "target")
    (repo / kits.LOCK_FILE).write_text(json.dumps({"schema": 1, "kits": {}}))  # the PR unpins everything in its working tree and head
    git(repo, "commit", "-qam", "pr")
    cfg = config.Config.load_from_ref("target", repo=str(repo))
    cfg.from_target = True
    strict.read_target_trust(cfg, str(repo))
    assert kits.lock_for(cfg, repo) == kits.read_lock(json.dumps(target_lock).encode())


# ---- the adversarial sweep: every reader and loop stays linear (SIGALRM bound, as tests/test_stack_linear.py) ----
def _sweep_kit(root, manifest=None, body=None):
    if manifest is not None:
        _put(root, f"{KIT}/extension.yml", manifest)
    if body is not None:
        _put(root, f"{KIT}/commands/run.md", body)


_MANIFEST = 'extension:\n  id: "sf"\n  repository: "https://example.com/x"\nprovides:\n  commands:\n    - name: "speckit.sf.run"\n      file: "commands/run.md"\n'
_SHAPES = {
    "one-line-1mib": lambda c: c * (_MIB - 64),
    "500x1990": lambda c: ("x" * 1989 + c + "\n") * 500,
    "20000-short": lambda c: (c * 20 + "\n") * 20000,
}


@needs_timer
@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("char", [" ", "a", ":", "-", "\"", "'", "#", "`", "pip ", "curl ", "https://", "|", "\t", "---\n"])
def test_scan_and_repository_are_linear_on_hostile_text(tmp_path, shape, char):
    text = _SHAPES[shape](char)[:_MIB - 512]
    _sweep_kit(tmp_path, manifest=_MANIFEST + text, body=text)
    assert _kt(kits.scan, tmp_path, "sf") < _BOUND
    assert _kt(kits._repository, _MANIFEST + text) < _BOUND
    assert _kt(kits._manifest_facts, text) < _BOUND


@needs_timer
@pytest.mark.parametrize("raw", [
    b"[" * _MIB, b'{"extensions":' + b"[" * (_MIB - 64), b'{"extensions": {"a": ' + b"1" * (_MIB - 64) + b"}}",
    b'{"extensions": {' + b",".join(b'"k%d": {}' % i for i in range(60000)) + b"}}",
    json.dumps({"extensions": {"sf": _entry(commands=["speckit.sf.x%d" % i for i in range(5000)])}}).encode(),
    json.dumps({"extensions": {"sf": _entry(commands=["speckit.sf.x%d" % i for i in range(5001)])}}).encode(),
])
def test_registry_hostile_shapes_are_bounded(tmp_path, raw):
    _put(tmp_path, ".specify/extensions/.registry", raw[:_MIB])
    assert _kt(kits.installed, tmp_path) < _BOUND
    assert _kt(kits.verify, tmp_path, {"schema": 1, "kits": {}}) < _BOUND


def test_registered_names_over_the_cap_make_the_entry_unpinned(tmp_path):
    _put(tmp_path, ".specify/extensions/.registry",
         json.dumps({"extensions": {"sf": _entry(commands=["speckit.sf.x%d" % i for i in range(kits.MAX_FILES + 1)])}}))
    assert kits.installed(tmp_path) == [{"id": None, "n": 1}]


@needs_timer
@pytest.mark.parametrize("raw", [
    b"[" * (stack.MAX_PROFILE - 1), b'{"schema":1,"kits":{"sf":' + b"[" * (stack.MAX_PROFILE - 64),
    b'{"schema":1,"kits":{"sf":{"x":' + b"9" * (stack.MAX_PROFILE - 64) + b"}}}",
    b'{"schema":1,"kits":{' + b",".join(b'"k%d":{}' % i for i in range(20000))[:stack.MAX_PROFILE - 64] + b"}}",
])
def test_lockfile_hostile_shapes_are_bounded_and_refused(tmp_path, raw):
    (tmp_path / kits.LOCK_FILE).write_bytes(raw)
    t = _kt(kits.load_lock, tmp_path)
    assert t < _BOUND
    with pytest.raises((ConfigInvalid, kits.KitError)):
        kits.load_lock(tmp_path)


def test_lockfile_over_the_cap_is_refused_unread(tmp_path, monkeypatch):
    (tmp_path / kits.LOCK_FILE).write_bytes(b" " * (stack.MAX_PROFILE + 1))
    with pytest.raises(kits.KitError) as e:
        kits.load_lock(tmp_path)
    assert e.value.reason == "unreadable"


@needs_timer
def test_dir_digest_at_a_100k_entry_tree_is_refused_quickly(tmp_path, git_env):
    entries = [("100644", ("%s/f/%06d.md" % (KIT, i)).encode(), b"x") for i in range(100_000)]
    repo, head = _plumb(tmp_path, entries)
    assert _kt(kits.dir_digest_at, repo, head, "sf") < _BOUND
    with pytest.raises(kits.KitError) as e:
        kits.dir_digest_at(repo, head, "sf")
    assert e.value.reason == "unreadable"


@needs_timer
def test_dir_digest_at_max_files_deep_paths_is_linear(tmp_path, git_env):
    deep = "/".join(["d"] * 400)  # deep nesting: every component is checked, still linear
    entries = [("100644", ("%s/%s/%04d.md" % (KIT, deep, i)).encode(), b"x") for i in range(kits.MAX_FILES)]
    repo, head = _plumb(tmp_path, entries)
    assert _kt(kits.dir_digest_at, repo, head, "sf") < _BOUND
    assert len(kits.dir_digest_at(repo, head, "sf")) == 64


@needs_timer
def test_dir_digest_many_untracked_files_is_refused_quickly(repo):
    d = repo / KIT / "many"
    d.mkdir()
    for i in range(kits.MAX_FILES + 1):
        (d / ("%05d.md" % i)).write_bytes(b"x")
    assert _kt(kits.dir_digest, repo, "sf") < _BOUND
    with pytest.raises(kits.KitError) as e:
        kits.dir_digest(repo, "sf")
    assert e.value.reason == "unreadable"


@needs_timer
def test_scan_a_manifest_naming_thousands_of_files_is_bounded(tmp_path):
    many = "".join("    - name: s%d\n      file: scripts/s%d.sh\n" % (i, i) for i in range(20000))
    _sweep_kit(tmp_path, manifest=_MANIFEST + "  scripts:\n" + many)
    for i in range(kits.MAX_BODIES):
        _put(tmp_path, f"{KIT}/scripts/s{i}.sh", "curl x\n" * 2000)
    assert _kt(kits.scan, tmp_path, "sf") < _BOUND
