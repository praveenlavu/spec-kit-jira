"""L5a binding note 7 (new): every regex and loop of strict.py over PR-controlled text is linear, and odd files never hang or crash it.
Sizes: one 1 MiB line, 500 lines of 1990 characters, 20000 short lines (the sweeps that found quadratic code in R4b, L1b, L2, L3).
A CPU-time SIGPROF timer cuts a slow call off at _BOUND (a super-linear path fails the assertion, never hangs the suite)."""
import json
import os
import signal
import time
import types

import pytest

from skj import config, decisions, gate, stack, strict
from skj.dod import SECTION2
from test_stack_support import approve, commit, git, profile, git_env  # noqa: F401  (git_env: fixture)
from test_strict import KEY, _project, _rules, _no_credentials, _stub_detect  # noqa: F401  (fixture)

_BOUND = 2.0  # seconds per call; the linear code takes well under 0.3 s at these sizes
needs_timer = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="signal.setitimer is not available on this platform")
MIB, W = 1 << 20, 1990


class _TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise _TooSlow()


def _timed(fn, *args):
    """Seconds fn(*args) took; _BOUND when it was cut off there. A ValueError (a fixed refusal) is part of the contract."""
    # CPU time, not the wall clock: load slows the clock, not the work; a wall-clock backstop at 10x still cuts off a call blocked on a FIFO or a child
    old, old_real = signal.signal(signal.SIGPROF, _alarm), signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_PROF, _BOUND)
    signal.setitimer(signal.ITIMER_REAL, 10 * _BOUND)
    t0 = time.process_time()
    try:
        fn(*args)
    except _TooSlow:
        return _BOUND
    except ValueError:
        pass
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGPROF, old)
        signal.signal(signal.SIGALRM, old_real)
    return time.process_time() - t0


def _ledger(sec2="", sec3="", sec4=""):
    return f"# DoD\n- **Spec dir:** specs/x\n\n## 1. Seeded\n\n{SECTION2}\n{sec2}\n## 3. AC\n{sec3}\n## 4. Log\n{sec4}\n## 5. DoD\n"


SEC2 = {"1MiB-bullet-whitespace": "- **Approach" + " " * MIB, "500-fill-runs": "- **Approach:** x\n" + ("_(" + " " * W + "\n") * 500,
        "500-open-bold": ("- **" + "a" * W + "\n") * 500, "20000-lines": "- **Approach:** x\n" + "  more\n" * 20000}


@needs_timer
@pytest.mark.parametrize("name", SEC2)
def test_placeholders_are_linear(name):
    assert _timed(strict._placeholders, types.SimpleNamespace(ledger=_ledger(SEC2[name]))) < _BOUND


CELLS = {"1MiB-red": "red " * (MIB // 4), "1MiB-red-hex41": ("red " + "a" * 41 + " ") * (MIB // 46),
         "red-then-1MiB-spaces": "red 1234567" + " " * MIB + "x", "1MiB-colons": "red" + ":" * MIB}


@needs_timer
@pytest.mark.parametrize("name", CELLS)
def test_parse_evidence_is_linear(name):
    assert _timed(strict.parse_evidence, CELLS[name]) < _BOUND


_HEAD = "### Verification — 2026-01-01 @ abcdef1234567\n"
SEC4 = {"20000-headings": _HEAD * 20000, "500-ac-digit-runs": _HEAD + ("- AC-" + "1" * W + "\n") * 500,
        "1MiB-head-line": _HEAD + "- head: " + "a" * MIB + "\n",
        "20000-ac-lines": _HEAD + "".join(f"- AC-{n}: red aaaaaaaaaaaa exit 1 · green bbbbbbbbbbbb exit 0 · L1 ok\n" for n in range(20000)),
        "20000-fidelity-lines": "### AC fidelity — 2026-01-01 @ abcdef1\n" + "".join(f"- AC-{n}: asserts — " + "x" * 198 + "\n" for n in range(20000)),
        "500-long-fidelity-lines": "### AC fidelity — 2026-01-01 @ abcdef1\n" + ("- AC-1: asserts — " + "x" * W + "\n") * 500,
        "1MiB-fidelity-line": "### AC fidelity — 2026-01-01 @ abcdef1\n- AC-1: asserts — " + "x " * (MIB // 2) + "\n"}


@needs_timer
@pytest.mark.parametrize("name", SEC4)
def test_the_record_and_the_fidelity_block_are_read_in_linear_time(name):
    md = _ledger(sec4=SEC4[name])
    assert _timed(strict.validate_record, md, [], ".", "HEAD", None, KEY, None, True) < _BOUND  # squash_ok: no git
    assert _timed(strict.parse_fidelity, md) < _BOUND


DECS = {"500-D-then-spaces": ("D1" + " " * W + "\n") * 500, "500-D-colon-spaces": ("D1:" + " " * (W - 4) + "x\n") * 500,
        "500-arrow-spaces": ("D1 → " + " " * W + "\n") * 500, "500-D-digit-runs": ("D" + "1" * W + "x\n") * 500,
        "500-supersedes-bait": ("D1 : " + " " * 1980 + "supersedes\n") * 500, "1MiB-D-line": "D1:" + " " * MIB + "\n",
        "20000-overrides": "".join(f"- [ ] D{n}: skip l2 → _(open)_ · kind: override · why: w\n" for n in range(1, 20001)),
        "20000-superseders": "".join(f"- [ ] D{n}: supersedes D{n - 1}: x → _(open)_ · kind: decision · why: w\n" for n in range(2, 20002))}


@needs_timer
@pytest.mark.parametrize("name", DECS)
def test_the_decision_rules_are_linear(name):
    md = DECS[name]
    c = types.SimpleNamespace(decisions=decisions.parse_decisions(md), decisions_ok=True, decisions_md=md, id=KEY,
                              cfg=types.SimpleNamespace(rules={"enabled": True}))  # rules on: the rejected-exclusion filter is timed too
    for fn in (strict._decisions_pr, strict._decisions_done, strict.overrides):
        assert _timed(fn, c) < _BOUND, fn.__name__


@needs_timer
def test_the_record_write_is_linear_in_the_live_headings():
    """Found by the L5a sweep: one copy of the ledger per earlier heading (20000 headings took 2.2 s; 60000 take ~20 s)."""
    md = _ledger(sec4="### Verification\n" * (MIB // 17))
    assert _timed(strict._with_record, md, "### Verification — 2026-01-01 @ abcdef123456", ["- head: abcdef123456 exit 0"]) < _BOUND
    out = strict._with_record(_ledger(sec4="### Verification — a\n### Verification — b (superseded)\n### Verification — c\n"), "### Verification — d", [])
    assert out.count("(superseded)") == 3 and out.count("### Verification — d\n") == 1


# ---- found by the sweep: L1 costs several git runs per section 3 row (~30 ms); a 1 MiB ledger holds ~40000 rows ----
def _rows(n):
    return [{"ac": f"AC-{i}", "tests": [], "evidence": "", "status": f"red {i:07x}a → green {i:07x}b"} for i in range(n)]


def test_more_than_max_rows_is_one_finding_and_no_git_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config.subprocess, "run", lambda *a, **k: pytest.fail("no git may run"))
    monkeypatch.setattr(config.subprocess, "Popen", lambda *a, **k: pytest.fail("no git may run"))
    out = strict.check_l1(_rows(strict.MAX_ROWS + 1), str(tmp_path), "HEAD", gate.DEFAULT_TESTS, None, KEY)
    assert [f["rule"] for f in out] == ["evidence-too-many"] and str(strict.MAX_ROWS + 1) in out[0]["detail"]
    assert [f["rule"] for f in strict._range_findings(_rows(40000), str(tmp_path), "x")] == ["evidence-too-many"]
    assert "evidence-too-many" not in {r for rs in strict._COVERS.values() for r in rs}


@needs_timer
def test_sixteen_thousand_rows_through_check_and_verify_stay_fast(tmp_path, monkeypatch):
    sec3 = "| AC | c | t | e | s |\n|---|---|---|---|---|\n" + "".join(f"| AC-{i} | c | t | e | red {i:07x}a -> green {i:07x}b |\n" for i in range(16000))  # ~0.9 MiB: under the ledger cap
    cfg = _project(tmp_path, ledger=_ledger(sec3=sec3))
    assert _timed(strict.verify, cfg, KEY, False, None, str(tmp_path)) < _BOUND
    assert "evidence-too-many" in _rules(strict.verify(cfg, KEY, record=False, repo=str(tmp_path)))
    assert _timed(strict.check, cfg, KEY, "pr", str(tmp_path)) < _BOUND
    assert _rules(strict.check(cfg, KEY, "pr", repo=str(tmp_path))).count("evidence-too-many") == 1


def test_max_rows_itself_is_still_judged(tmp_path, git_env):
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "base", {"a.txt": "a\n"})
    out = strict.check_l1(_rows(3), str(tmp_path), "HEAD", gate.DEFAULT_TESTS, None, KEY)
    assert {f["rule"] for f in out} == {"sha-unknown"}  # under the cap: every row goes to git as before


# ---- odd files: FIFO, symlink and directory entries never hang or crash a check ----
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
@pytest.mark.parametrize("what", ["ledger", "decisions", "spec"])
def test_a_fifo_never_hangs_the_check(tmp_path, what):
    cfg = _project(tmp_path, ledger=False if what == "ledger" else None, files={} if what == "spec" else None)
    path = {"ledger": tmp_path / "dod" / f"DoD_{KEY}.md", "decisions": tmp_path / "dod" / f"Decisions_{KEY}.md",
            "spec": tmp_path / "specs" / "x" / "spec.md"}[what]
    if what == "spec":
        (tmp_path / "dod" / f"DoD_{KEY}.md").write_text((tmp_path / "dod" / f"DoD_{KEY}.md").read_text().replace("specs/001-members", "specs/x"))
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)
    want = {"ledger": "ledger-missing", "decisions": "decisions-invalid", "spec": "spec-missing"}[what]
    phase = "pr" if what == "decisions" else "implement"
    assert want in _rules(strict.check(cfg, KEY, phase, repo=str(tmp_path)))


@pytest.mark.parametrize("kind", ["symlink", "directory"])
@pytest.mark.parametrize("name", [f"DoD_{KEY}.md", f"Decisions_{KEY}.md"])
def test_a_symlinked_or_directory_ledger_or_decisions_file_fails_closed(tmp_path, kind, name):
    cfg = _project(tmp_path, ledger=None if name.startswith("Decisions") else False)
    (tmp_path / "real.md").write_text("x")
    p = tmp_path / "dod" / name
    os.symlink(tmp_path / "real.md", p) if kind == "symlink" else p.mkdir()
    want = "ledger-missing" if name.startswith("DoD") else "decisions-invalid"
    assert want in _rules(strict.check(cfg, KEY, "pr", repo=str(tmp_path)))


# ---- JSON bombs: a deep or huge head profile, lockfile or config is a finding, never a crash ----
def test_deep_and_huge_head_trust_files_are_findings_not_crashes(tmp_path, git_env, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    cmd = "PYTHONPATH=src python3 -m pytest -q"
    target = {"site": "example-site.atlassian.net", "project": "SPK", "mode": "strict", "strict": {"test_command": cmd}}
    commit(tmp_path, "base", {"skj.config.json": json.dumps(target), "skj.stack.json": json.dumps(approve(profile()))})
    base = git(tmp_path, "rev-parse", "HEAD")
    deep = "[" * 200000 + "]" * 200000
    commit(tmp_path, "pr", {"skj.stack.json": deep[:stack.MAX_PROFILE - 2] + "]]", "skj.kits.lock.json": deep[:stack.MAX_PROFILE],
                            "skj.config.json": '{"a":' * 100000 + "1" + "}" * 100000})
    cfg = config.Config.load_from_ref(base, str(tmp_path))
    cfg.from_target = True
    strict.read_target_trust(cfg, str(tmp_path))
    _project(tmp_path, prof=None)
    trust = strict._head_trust(cfg, str(tmp_path), git(tmp_path, "rev-parse", "HEAD"))
    assert trust["config_invalid"] is True and trust["profile"]["status"] in ("invalid", "unreadable") and trust["changed"]
    res = strict.check(cfg, KEY, "implement", repo=str(tmp_path))
    assert {"config-invalid", "stack-profile-invalid"} <= set(_rules(res))


# ---- L5b: the review at the head commit, the ledger-only comparison and the CI kit listing ----
from skj import kits, prs  # noqa: E402


def _bulk_commit(repo, paths, parent=None):
    """A commit whose tree holds every path in `paths` (one shared blob), built through the index: no file is written."""
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=b"x\n", capture_output=True, check=True).stdout.decode().strip()
    env = {**os.environ, "GIT_INDEX_FILE": str(repo / ".git" / "bulk-index")}
    if os.path.exists(env["GIT_INDEX_FILE"]):
        os.remove(env["GIT_INDEX_FILE"])
    info = "".join(f"100644 {blob}\t{p}\n" for p in paths).encode()
    subprocess.run(["git", "-C", str(repo), "update-index", "--add", "--index-info"], input=info, env=env, check=True)
    tree = subprocess.run(["git", "-C", str(repo), "write-tree"], env=env, capture_output=True, check=True).stdout.decode().strip()
    args = ["git", "-C", str(repo), "commit-tree", tree, "-m", "bulk"] + (["-p", parent] if parent else [])
    return subprocess.run(args, capture_output=True, check=True, text=True, env={**os.environ, **_ID}).stdout.strip()


_ID = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
import subprocess  # noqa: E402

REVIEW_LEDGERS = {"1MiB-sha": "## 4. Log\n### Review @ `" + "a" * MIB + "`\n",
                  "20000-reviews": "## 4. Log\n" + "### Review @ `abc1234`\n- **Blocks PR:** no\n" * 20000,
                  "500-severity-lines": "## 4. Log\n### Review @ `abc1234`\n" + ("- **Confirmed by severity:** " + "x 1 " * 497 + "\n") * 500,
                  "1MiB-backticks": "## 4. Log\n" + "`" * MIB, "1MiB-heading": "## 4. Log\n### Review" + " " * MIB + "\n"}


@needs_timer
@pytest.mark.parametrize("name", REVIEW_LEDGERS)
def test_review_blocks_is_linear_on_a_hostile_committed_ledger(tmp_path, git_env, name):
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "ledger", {f"dod/DoD_{KEY}.md": REVIEW_LEDGERS[name][:MIB - 64]})
    assert _timed(prs.review_blocks, KEY, ["blocker"], "HEAD", str(tmp_path)) < _BOUND
    assert prs.review_blocks(KEY, ["blocker"], "HEAD", str(tmp_path)) is not None  # none of these is a passing review


@needs_timer
@pytest.mark.parametrize("spec_dir", ["a/" * 200000, "　" * MIB, "./" * 300000 + "x", "x" * MIB])
def test_spec_rel_is_linear(spec_dir):
    assert _timed(prs._spec_rel, spec_dir, "") < _BOUND


@needs_timer
def test_only_ledger_since_is_linear_on_a_100k_path_diff(tmp_path, git_env):
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "base", {"README.md": "x\n"})
    base = git(tmp_path, "rev-parse", "HEAD")
    head = _bulk_commit(tmp_path, [f"src/m{n:06d}.py" for n in range(100000)], parent=base)
    assert _timed(prs.only_ledger_since, str(tmp_path), base, head, "specs/x", KEY) < _BOUND
    assert prs.only_ledger_since(str(tmp_path), base, head, "specs/x", KEY) is False


@needs_timer
@pytest.mark.parametrize("layout", ["100k-kit-dirs", "100k-root-entries"])
def test_tracked_kits_is_linear_on_a_100k_entry_tree(tmp_path, git_env, layout):
    git(tmp_path, "init", "-q", "-b", "main")
    paths = ([f".specify/extensions/k{n}/extension.yml" for n in range(100000)] if layout == "100k-kit-dirs"
             else [f"r{n:06d}.txt" for n in range(100000)] + [".specify/extensions/sf/extension.yml"])
    head = _bulk_commit(tmp_path, paths)
    assert _timed(strict._tracked_kits, str(tmp_path), head) < _BOUND
    out = strict._tracked_kits(str(tmp_path), head)
    if layout == "100k-kit-dirs":  # ~9 MB of listing: over kits.MAX_LISTING, so "git could not answer" (the _KIT_GIT finding), never a pass
        assert out is None
        out = strict._tracked_kits(str(tmp_path), _bulk_commit(tmp_path, paths[:60000]))
        assert out is not None and len(out[0]) == 60000
        res = strict._kit_result(sorted(out[1]), [])  # one short finding however many kits
        assert len(res["findings"]) == 1 and len(res["findings"][0]["detail"]) < 300
    else:
        assert out is not None and out[0] == {"sf"}


def test_an_over_cap_target_lock_is_one_finding_and_no_per_kit_git(tmp_path, git_env, monkeypatch):
    entry = {"version": "1.0.0", "source": None, "catalog_sha256": None, "dir_digest": "a" * 64, "agents_sha256": "b" * 64}
    cfg = _project(tmp_path)
    cfg.from_target, cfg.target_profile_blob = True, None
    cfg.target_lock_blob = json.dumps({"schema": 1, "kits": {f"k{n}": entry for n in range(strict.MAX_KITS + 1)}}).encode()
    monkeypatch.setattr(kits, "dir_digest_at", lambda *a: pytest.fail("no kit is judged one by one past the cap"))
    monkeypatch.setattr(strict, "_tracked_kits", lambda *a: pytest.fail("nothing is listed past the cap"))
    c = strict._Ctx(cfg, KEY, str(tmp_path))
    c._stack = {"head": {"lock": cfg.target_lock_blob}}
    res = strict._kits_ci(c)
    assert [f["rule"] for f in res["findings"]] == ["kit-changed"] and str(strict.MAX_KITS) in res["findings"][0]["detail"]
