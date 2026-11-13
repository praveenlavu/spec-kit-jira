"""L4: `stack` on adversarial input. A pull request controls the committed tree (paths, manifests), `skj.stack.json` and `skj.config.json`, so every
reader must be linear and every refusal a fixed text, never a hang, a traceback or a C-stack crash. Real temp git repositories are built with
plumbing (no working tree), so a 20000-entry tree costs about a second. `_timed` bounds CPU time (SIGPROF), as tests/test_approvals.py does.

Also L4-AC2: `stack detect` reads the files at HEAD, never the working tree."""
import importlib.util
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from skj import stack
from skj.__main__ import main
from test_stack_support import _IDENTITY, approve, git, git_env, profile, terminal, write_files  # noqa: F401  (git_env: fixture)

ROOT = Path(__file__).resolve().parent.parent
_MIB = 1 << 20
_BOUND = 6.0  # seconds per call; the linear code takes about 1 s at 20000 tree entries (git included), the quadratic dart rule took minutes
needs_timer = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="signal.setitimer is not available on this platform")


class _TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise _TooSlow()


def _timed(fn, *args):
    """Seconds fn(*args) took; _BOUND when it was cut off there (a super-linear reader fails the assertion, never hangs the suite). An exception
    the function raises (ValueError, a fixed refusal) is part of its contract here."""
    # CPU time, not the wall clock: load slows the clock, not the work; a wall-clock backstop at 10x still cuts off a call blocked on a FIFO or a child
    old, old_real = signal.signal(signal.SIGPROF, _alarm), signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_PROF, _BOUND)
    signal.setitimer(signal.ITIMER_REAL, 10 * _BOUND)
    t0 = time.process_time()
    try:
        fn(*args)
    except _TooSlow:
        return _BOUND
    except (ValueError, OSError):
        pass
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGPROF, old)
        signal.signal(signal.SIGALRM, old_real)
    return time.process_time() - t0


def mkrepo(tmp_path, entries):
    """A repository whose HEAD holds `entries` ({path: bytes}) and whose working tree is empty: blobs, index, tree and commit by plumbing."""
    git(tmp_path, "init", "-q", "-b", "main")
    env = {**_IDENTITY, **os.environ}
    blobs, lines = {}, []
    for path, data in entries.items():
        if data not in blobs:
            blobs[data] = subprocess.run(["git", "-C", str(tmp_path), "hash-object", "-w", "--stdin"], input=data, capture_output=True,
                                         check=True, env=env).stdout.decode().strip()
        lines.append(f"100644 {blobs[data]}\t{path}\0".encode())
    subprocess.run(["git", "-C", str(tmp_path), "update-index", "--add", "-z", "--index-info"], input=b"".join(lines), check=True, capture_output=True, env=env)
    tree = git(tmp_path, "write-tree")
    git(tmp_path, "update-ref", "HEAD", git(tmp_path, "commit-tree", tree, "-m", "x"))
    return tmp_path


# ---- L4-AC2: detect reads HEAD, never the working tree ----
def test_l4_ac2_detect_reads_head_not_the_working_tree(tmp_path, capsys):
    pkg = {"scripts": {"test": "jest"}, "dependencies": {"@pega/x": "1", "ffmpeg": "1"}}
    write_files(tmp_path, {"package.json": json.dumps(pkg), "tsconfig.json": "{}", "pyproject.toml": "[tool.pytest.ini_options]\n", "README.txt": "x\n"})
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    before = stack.detect(tmp_path)
    assert [(s["id"], s["hints"].get("test_script"), s["hints"].get("pega")) for s in before["stacks"]] == [("typescript", True, True), ("python", None, None)]
    assert before["domain_hints"] == ["media", "pega"]
    # every way the working tree can differ from HEAD: a manifest edited, a marker deleted, untracked and staged markers, a manifest made a symlink
    write_files(tmp_path, {"package.json": "{}", "go.mod": "x\n", "sfdx-project.json": "{}", "Cargo.toml": "x\n", "web/package.json": "{}"})
    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "tsconfig.json").unlink()
    git(tmp_path, "add", "Cargo.toml")
    os.symlink("README.txt", tmp_path / "pkg-link.json")
    assert stack.detect(tmp_path) == before
    assert git(tmp_path, "status", "--porcelain")  # the tree really is dirty: the equality above is not vacuous
    assert main(["stack", "detect", "--repo", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == before


# ---- the tree listing (a PR controls every path) ----
@needs_timer
@pytest.mark.parametrize("shape", ["dart+android", "dart-only", "one-dir-requirements", "many-roots", "many-salesforce-roots", "force-app-dirs", "unsafe-roots"])
def test_detect_is_linear_on_a_20000_entry_tree(tmp_path, git_env, shape):
    n = 20000
    entries = {
        "dart+android": {**{f"d{i}/pubspec.yaml": b"name: x\n" for i in range(n // 2)}, **{f"d{i}/android/build.gradle": b"x" for i in range(n // 2)}},
        "dart-only": {f"d{i}/pubspec.yaml": b"name: x\n" for i in range(n)},
        "one-dir-requirements": {f"requirements-{i}.txt": b"x" for i in range(n)},
        "many-roots": {f"p{i}/package.json": b"{}" for i in range(n)},
        "many-salesforce-roots": {f"d{i}/sfdx-project.json": b"{}" for i in range(n)},
        "force-app-dirs": {f"force-app/s{i}/x/y.cls": b"x" for i in range(n)},
        "unsafe-roots": {f"a b{i}/go.mod": b"x" for i in range(n)},
    }[shape]
    repo = mkrepo(tmp_path, entries)
    assert _timed(stack.detect, repo) < _BOUND


@needs_timer
def test_detect_refuses_more_than_max_candidates_quickly(tmp_path, git_env):
    repo = mkrepo(tmp_path, {f"d{i}/go.mod": b"x" for i in range(stack.MAX_CANDIDATES + 1)})
    t0 = time.perf_counter()
    with pytest.raises(ValueError, match="^too many files to inspect$"):
        stack.detect(repo)
    assert time.perf_counter() - t0 < _BOUND


@needs_timer
@pytest.mark.parametrize("name", ["x" * _MIB + "/package.json", "p" + "a" * _MIB, "a/" * 2000 + "go.mod", " /go.mod", "`[]\\*/**/go.mod", "a" * 300 + "/go.mod"],
                         ids=["1MiB-segment", "1MiB-name", "2000-segments", "nbsp", "punctuation", "300-char-segment"])  # ids: the test id is in the environment
def test_detect_is_linear_on_hostile_path_shapes(tmp_path, git_env, name):
    repo = mkrepo(tmp_path, {name: b"x", "package.json": b"{}"})
    assert _timed(stack.detect, repo) < _BOUND
    try:
        res = stack.detect(repo)
    except ValueError as e:  # git itself may refuse a very deep tree: a fixed text
        assert str(e) == "cannot read git history here"
        return
    assert [s["id"] for s in res["stacks"] if s["root"] == "."] == ["node"]  # the hostile path never changes what a plain marker says


# ---- the manifests (read from the commit's blobs, at most MAX_READS of them, at most 1 MiB each) ----
_M = _MIB - 64  # a manifest of at most MAX_MANIFEST bytes is read; these sit just under it
MANIFESTS = {
    "package.json": {"open-brackets": b"[" * _M, "nested-objects": b'{"a":' * (_M // 5), "one-big-integer": b'{"a": ' + b"1" * (_M - 10) + b"}",
                     "many-keys": b"{" + b",".join(b'"k%d":1' % i for i in range(60000))[: _M - 10] + b"}",
                     "unicode-escapes": b'{"a": "' + b"\\u00e9" * (_M // 6 - 20) + b'"}',
                     "many-deps": json.dumps({"dependencies": {f"@pega{i}": "1" for i in range(30000)}}).encode()},
    "pyproject.toml": {"blank-run": b" " * _M, "one-token": b"a" * _M, "500x1990": b"\n".join([b"x" * 1990] * 500),
                       "20000-lines": b"\n".join([b"[tool.pytest"] * 20000), "nbsp": " ".encode() * (_M // 2),
                       "punctuation": b"`[]\\./*-+" * (_M // 9)},
    "requirements.txt": {"blank-then-pytest": b" " * _M + b"pytest", "20000-lines": b"\n".join([b"  pytest-" + b"a" * 5] * 20000),
                         "500x1990": b"\n".join([b"  pytest" + b"a" * 1980] * 500), "token-run": b"a.+-" * (_M // 4)},
    "pubspec.yaml": {"blank-run": b"  sdk: flutter" + b" " * _M + b"x", "20000-lines": b"\n".join([b"  sdk:      "] * 60000),
                     "500x1990": b"\n".join([b"  sdk: flutter" + b" " * 1970 + b"x"] * 500)},
    "CMakeLists.txt": {"token-run": b"a." * (_M // 2), "punctuation": b"a+.-" * (_M // 4)},
}


@needs_timer
@pytest.mark.parametrize("name,shape", [(n, s) for n, shapes in MANIFESTS.items() for s in shapes], ids=lambda v: v)
def test_detect_is_linear_on_adversarial_manifests(tmp_path, git_env, name, shape):
    data = MANIFESTS[name][shape]
    assert len(data) <= stack.MAX_MANIFEST
    assert _timed(stack.detect, mkrepo(tmp_path, {name: data})) < _BOUND


@needs_timer
def test_detect_reads_a_budget_of_big_manifests_in_linear_time(tmp_path, git_env):
    entries = {f"p{i}/package.json": b'{"scripts":{"test":"x"}}' + b" " * (_MIB - 30) for i in range(stack.MAX_READS + 8)}
    assert _timed(stack.detect, mkrepo(tmp_path, entries)) < _BOUND


def test_a_package_json_with_a_huge_integer_is_unreadable_not_slow(tmp_path, git_env):
    """CPython's int() is quadratic in the digits (1 MiB of digits: 3.4 s on 3.9): such a number is refused like any unparseable manifest."""
    repo = mkrepo(tmp_path, {"package.json": b'{"scripts": {"test": "x"}, "n": ' + b"1" * (_MIB - 60) + b"}"})
    res = stack.detect(repo)
    assert res["stacks"][0]["hints"] == {"test_script": False} and res["note"] == "hints incomplete"
    (tmp_path / "ok").mkdir()
    ok = mkrepo(tmp_path / "ok", {"package.json": b'{"scripts": {"test": "x"}, "n": 12345678901234567890}'})
    assert stack.detect(ok)["stacks"][0]["hints"]["test_script"] is True  # a number of ordinary size is read as ever


@pytest.mark.parametrize("depth", [1000, 100000, _MIB // 2])
def test_a_deeply_nested_package_json_is_unreadable_not_a_crash(tmp_path, git_env, depth):
    repo = mkrepo(tmp_path, {"package.json": b"[" * depth + b"]" * depth})
    res = stack.detect(repo)
    assert res["outcome"] == "detected" and res["stacks"][0]["hints"] == {"test_script": False} and res["note"] == "hints incomplete"


# ---- the profile and the config (256 KiB and 1 MiB of PR-controlled JSON) ----
@pytest.mark.parametrize("raw,cap", [(b"[" * stack.MAX_PROFILE, stack.MAX_PROFILE), (b"[" * _MIB, stack.MAX_PROFILE), (b'{"a":' * (_MIB // 5), _MIB),
                                     (b"[" * _MIB, _MIB), (b"[" * 100000 + b"]" * 100000, _MIB), (b"1" * stack.MAX_PROFILE, stack.MAX_PROFILE),
                                     (b"1" * _MIB, _MIB), (b"-" + b"1" * (stack.MAX_DIGITS + 1), _MIB)],
                         ids=["open-cap", "open-1MiB-over-cap", "objects-1MiB", "open-1MiB", "balanced-100k", "digits-cap", "digits-1MiB", "digits-1001"])
def test_parse_json_refuses_deep_or_huge_values_with_a_fixed_text(raw, cap):
    with pytest.raises(ValueError) as e:
        stack.parse_json(raw, cap)
    assert str(e.value) in ("the profile is not valid JSON", f"the profile is larger than {cap} bytes")
    assert stack.parse_json(b"1" * stack.MAX_DIGITS, cap) == int("1" * stack.MAX_DIGITS)  # the longest number still read


@needs_timer
def test_the_profile_readers_are_linear_on_adversarial_json(tmp_path):
    cap = stack.MAX_PROFILE
    many = {"schema": 1, "version": 1, "status": "draft", "stacks": [{"id": "a", "root": ".", **{f"k{i}": 1 for i in range(20000)}}]}
    run = lambda c: {"schema": 1, "version": 1, "status": "draft", "stacks": [{"id": "a", "root": ".", "commands": {"test": {"run": c, "source": "unverified"}}}]}  # noqa: E731
    cases = [lambda: stack.validate(many), lambda: stack.validate(run("a" * (cap - 200))), lambda: stack.validate(run("|" + " " * 499)),
             lambda: stack.validate(run("`" * 499)), lambda: stack.validate(run(" " * 499)), lambda: stack.validate(run("-----BEGIN " + " " * 480)),
             lambda: stack.validate(run("a" * (cap - 200) + "\n")), lambda: stack.read_profile(tmp_path, blob=json.dumps(many).encode()),
             lambda: stack.read_profile(tmp_path, blob=b"[" * cap), lambda: stack.plan_init(b"{" + b'"a":1,' * (cap // 6) + b'"b":2}'),
             lambda: stack.plan_init(b"[" * _MIB)]
    assert [i for i, c in enumerate(cases) if _timed(c) >= _BOUND] == []


@needs_timer
def test_lock_error_is_linear_and_iterative(tmp_path):
    cap = stack.MAX_PROFILE
    wrap = lambda v: json.dumps({"schema": 1, "kits": {"a-b": {"x": v}}}).encode()  # noqa: E731
    cases = [lambda: stack.lock_error(wrap("a" * (cap - 100))), lambda: stack.lock_error(wrap("-----BEGIN " + " " * (cap - 200))),
             lambda: stack.lock_error(wrap("-----BEGIN A" * (cap // 14))), lambda: stack.lock_error(wrap("gh" * (cap // 4))),
             lambda: stack.lock_error(b'{"schema":1,"kits":{"a-b":' + b"[" * 100000 + b"]" * 100000 + b"}}"),
             lambda: stack.lock_error(b'{"schema":1,"kits":{"a-b":' + b"[[]," * (cap // 8) + b"[]]}}")]
    assert [i for i, c in enumerate(cases) if _timed(c) >= _BOUND] == []
    assert stack.lock_error(b'{"schema":1,"kits":{"a-b":' + b"[" * 100000 + b"]" * 100000 + b"}}") == "is not valid JSON, or has a duplicate key"
    assert stack.lock_error(wrap("ghp_" + "A" * 36)) == "contains a control character or a secret pattern"
    assert stack.lock_error(wrap("fine")) is None


@needs_timer
def test_the_text_helpers_are_linear_within_their_caps():
    """`render_md` and `preview_text` print a validated profile, so every string is within the caps (500 characters): the markup-strip loop is
    quadratic in its input only, which these caps bound (4 ms)."""
    p = approve(profile())
    nested = "<!" * 75 + "-" * 150
    for n in range(48):
        p["stacks"][0]["conventions"].append({"rule": nested[:300], "source": "S1"})
    p["stacks"][0]["commands"]["lint"] = {"run": ("<!" * 125 + "-" * 250)[:500], "source": "S1"}
    p = approve(p)
    assert stack.validate(p) == []
    assert _timed(stack.render_md, p) < _BOUND and _timed(stack.preview_text, p, stack.compose_test_command(p), None, 3, None, "tech lead") < _BOUND
    for text in ("*" * 5000, "[" * 5000, "\\" * 5000, " " * 5000, "`" * 5000, "a/" * 100000 + "!", "/" * 100000):
        assert _timed(stack.safe_path, text, 1) < _BOUND and _timed(stack.representable_root, text) < _BOUND


@needs_timer
@pytest.mark.parametrize("raw", ["{\n" + " " * 1000 + "\n" * 1000 + "}", "{" + ("\n" + " " * 1000) * 1000, "{" + ("\r\n" + "\t" * 1000) * 1000 + "x",
                                 "\r" * _MIB, " " * _MIB, "\n" * _MIB, "{" + "\n \n" * (_MIB // 3), " " * (_MIB // 2)],
                         ids=["blank-lines", "newline-runs", "crlf-tabs", "cr-only", "spaces", "lf-only", "lf-space-lf", "nbsp"])
def test_the_config_layout_reader_is_linear(raw):
    assert _timed(stack._config_text, raw, {"a": 1}) < _BOUND


# ---- approve: a config nested too deeply, or with a huge number, is a bad config with a fixed text ----
@pytest.fixture
def repo(tmp_path, monkeypatch, git_env):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / stack.PROFILE_FILE).write_text(json.dumps(profile()), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("body", [b"[" * (_MIB - 100) + b"]", b"[" * 100000 + b"]" * 100000, b'{"site": "example-site.atlassian.net", "project": "SPK", "x": ' + b"1" * 5000 + b"}",
                                  b'{"site": "example-site.atlassian.net", "project": "SPK", "x": ' + b"[" * 100000 + b"]" * 100000 + b"}"],
                         ids=["1MiB-open", "balanced-100k", "5000-digits", "nested-in-an-object"])
def test_approve_refuses_a_hostile_config_with_a_fixed_text(repo, body):
    (repo / "skj.config.json").write_bytes(body)
    before = {p.name: p.read_bytes() for p in repo.iterdir() if p.is_file()}
    with pytest.raises(ValueError) as e:
        stack.apply_approve(".", "skj.config.json")
    assert str(e.value) == stack._E_CFGBAD
    assert {p.name: p.read_bytes() for p in repo.iterdir() if p.is_file()} == before


# ---- no hang on a FIFO, no escape through a link ----
@needs_timer
def test_init_over_a_fifo_profile_neither_hangs_nor_reads_it(repo):
    (repo / stack.PROFILE_FILE).unlink()
    os.mkfifo(repo / stack.PROFILE_FILE)
    assert _timed(stack._was_approved, repo / stack.PROFILE_FILE) < _BOUND
    assert stack._was_approved(repo / stack.PROFILE_FILE) is False
    plan = stack.plan_init(json.dumps(profile()).encode())
    assert _timed(stack.apply_init, str(repo), plan) < _BOUND  # replaces the FIFO as it would any invalid file


def test_was_approved_reads_a_bounded_regular_file_only(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps(approve(profile())), encoding="utf-8")
    assert stack._was_approved(f) is True
    f.write_text(json.dumps(profile()), encoding="utf-8")
    assert stack._was_approved(f) is False
    f.write_text(" " * (_MIB + 1) + json.dumps({"status": "approved"}), encoding="utf-8")
    assert stack._was_approved(f) is False  # over the cap: not read
    link = tmp_path / "link.json"
    os.symlink(f, link)
    assert stack._was_approved(link) is False  # a link is not followed
    assert stack._was_approved(tmp_path / "missing.json") is False and stack._was_approved(tmp_path) is False


def test_the_decisions_directory_cannot_be_a_link_out_of_the_repository(repo, monkeypatch, capsys):
    from test_stack_approve import CONFIG, config_text, snapshot
    (repo / "skj.config.json").write_text(config_text(CONFIG), encoding="utf-8")
    outside = repo.parent / (repo.name + "-outside")
    outside.mkdir()
    os.symlink(outside, repo / "dod")
    before = snapshot(repo)
    terminal(monkeypatch, None)
    code = main(["stack", "approve", "--apply", "--story", "SPK-1001"])
    err = capsys.readouterr().err
    assert code == 2 and "symbolic link" in err and snapshot(repo) == before and list(outside.iterdir()) == []


# ---- the secrets list is the scrub script's ----
def test_secrets_are_a_copy_of_the_scrub_scripts():
    spec = importlib.util.spec_from_file_location("scrub_check", ROOT / "scripts" / "scrub_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert [(n, p.pattern) for n, p in stack.SECRETS] == [(n, p.pattern) for n, p in mod.SECRETS]
