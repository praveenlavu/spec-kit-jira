"""W1: the X2 matrix. Every tracker or PR write is preview, human gate, apply: nothing is written without `--apply`, an
`--apply --expect-plan F` whose fresh plan differs from F exits 2 having written only `<F>.new`, and a write verb cannot reach the
argparse tree without being in `WRITE_VERBS` (W1-AC1..3).

The mechanism is the fork's `_plan` (`--plan-out`, `--expect-plan`, `<F>.new`), hardened: plan files are regular files only, at most
1 MiB, written atomically with mode 0600 and never through a planted symlink, never inside `.git`. No write verb is wired at W1, so
the framework is exercised end to end through a synthetic verb `demo` that a fixture adds to the CLI (a GET, then a PUT behind the
gate) against the S9 fake Jira; the registered verbs that exist today run as themselves.
"""
import argparse
import hashlib
import json
import os
import subprocess

import pytest

import skj.__main__ as m
from skj import issues
from fakejira import FakeJira

KEY = "SPK-1"

# The plan's list (W1), verbatim. `WRITE_VERBS` must equal it; a later change to the list is a plan change. H1a dropped `branch --link`
# (its fork source was cut by K9 and it never had a parser).
PLAN_LIST = ("push", "done", "questions --post", "decisions --post", "pr create", "pr link")

# Registered entries not wired to the plan/apply framework yet, with the task that wires each. A task that wires its verb removes its line
# (the scan fails while a wired entry is still listed), and its AC1 skip below becomes a real run.
NOT_BUILT = {}

# The argv each registered entry runs with in the AC1 matrix (no --apply). A task that builds a verb fixes its argv here if it needs more.
ARGV = {
    "push": ["push", KEY, "--from", "tasks.md"],  # one create, one create with a deferred transition, one transition (W3)
    "done": ["done", KEY],
    "questions --post": ["questions", KEY, "--post"],
    "decisions --post": ["decisions", KEY, "--post"],
    "pr create": ["pr", "create", KEY, "--base", "main", "--head", "feat/x", "--from", "tasks.md", "--allow-incomplete"],  # a dry run needs no network: gh is faked
    "pr link": ["pr", "link", KEY, "--url", "https://github.com/example-org/example-repo/pull/1", "--title", "A fictional PR"],
}

PLAN_FLAGS = {"--apply", "--plan-out", "--expect-plan"}


# ---- the argparse scan (W1-AC3) -------------------------------------------------------------------------------------------------------

DESTS = {"apply": "--apply", "plan_out": "--plan-out", "expect_plan": "--expect-plan"}


def _parsers(parser, path=(), seen=None):
    """Every parser of the tree, interior ones and the root ("") included, once each under its own name: an alias shares its verb's
    parser object, and `add_parser` puts the verb's name in `choices` before its aliases."""
    seen = [] if seen is None else seen
    if any(parser is s for s in seen):
        return
    seen.append(parser)
    yield " ".join(path), parser
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            for name, p in a.choices.items():
                yield from _parsers(p, path + (name,), seen)


def _split(entry):
    words = entry.split()
    return (" ".join(words[:-1]), words[-1]) if words[-1].startswith("--") else (entry, None)


def _signals(p):
    """The X2 flags of parser `p`: an option named --apply/--plan-out/--expect-plan, or any option whose dest is apply/plan_out/expect_plan
    (`--go` with dest="apply" is labelled with its dest). A flag named --yes, --force or --write is invisible to this scan by design."""
    out = set()
    for a in p._actions:
        if set(a.option_strings) & PLAN_FLAGS or a.dest in DESTS:
            out.add(DESTS[a.dest] if a.option_strings == [DESTS.get(a.dest)] else "%s (dest %s)" % ("/".join(a.option_strings), a.dest))
    return out


def _tree(ap):
    return {name: ({s for a in p._actions for s in a.option_strings}, p.get_default("write_verb"), _signals(p))
            for name, p in _parsers(ap)}


def _exists(tree, entry):
    name, flag = _split(entry)
    return name in tree and (flag is None or flag in tree[name][0])


def _scan(ap, registry, local_apply, not_built):
    """Every violation of the registry rule in the argparse tree `ap`. Any parser (a leaf, an interior one, the root) is write-capable
    when it has an X2 flag (`_signals`): it must carry the `write_verb` marker of a registered entry (set only by `_add_write_flags`), or
    be a local verb whose literal --apply is listed in LOCAL_APPLY (and then have no other X2 flag). Every registered entry is wired (a
    parser carries its marker) or is in `not_built` with its owning task, never both. Names are the verbs' own; an alias is never one."""
    tree, bad = _tree(ap), []
    wired = set()
    for name, (opts, marker, signals) in tree.items():
        if marker is not None:
            wired.add(marker)
            if marker not in registry:
                bad.append("%s: marker %r is not in WRITE_VERBS" % (name, marker))
            elif _split(marker)[0] != name or not _exists(tree, marker):
                bad.append("%s: marker %r names another verb or a missing flag" % (name, marker))
            if not PLAN_FLAGS <= signals:
                bad.append("%s: a write verb without --apply, --plan-out and --expect-plan" % name)
        elif signals - ({"--apply"} if name in local_apply else set()):
            bad.append("%s: can write (%s) but is not registered in WRITE_VERBS" % (name, ", ".join(sorted(signals))))
    for entry in registry:
        if entry not in wired and entry not in not_built:
            bad.append("%s: registered but not wired, and not on the not-built list with its owning task" % entry)
        if entry in wired and entry in not_built:
            bad.append("%s: wired now: remove it from NOT_BUILT so its AC1 test runs" % entry)
    for entry in not_built:
        if entry not in registry:
            bad.append("%s: on the not-built list but not registered" % entry)
    for name in local_apply:
        if name not in tree or "--apply" not in tree[name][0]:
            bad.append("%s: listed as a local --apply verb but has no --apply" % name)
    return bad


def test_registry_is_the_plan_list():
    assert m.WRITE_VERBS == PLAN_LIST
    assert set(m.LOCAL_APPLY) == {"branch", "stack init", "stack approve", "kits pin"} and all(m.LOCAL_APPLY.values())


def test_unregistered_write_verb_fails(monkeypatch):
    """W1-AC3: the real tree is clean, and each way of adding a write verb without registering it is caught."""
    assert _scan(m.build_parser(), m.WRITE_VERBS, m.LOCAL_APPLY, NOT_BUILT) == []

    def tree_with(add):
        monkeypatch.setattr(m, "COMMANDS", m.COMMANDS + (add,))
        try:
            return _scan(m.build_parser(), m.WRITE_VERBS, m.LOCAL_APPLY, NOT_BUILT)
        finally:
            monkeypatch.undo()

    def raw_apply(sub):  # a hand-added --apply, not through the framework
        sub.add_parser("zap").add_argument("--apply", action="store_true")

    def raw_plan_out(sub):
        sub.add_parser("zap").add_argument("--plan-out")

    def nested(sub):
        sub.add_parser("zap").add_subparsers(dest="z").add_parser("now").add_argument("--expect-plan")

    for add in (raw_apply, raw_plan_out, nested):
        bad = tree_with(add)
        assert len(bad) == 1 and "not registered in WRITE_VERBS" in bad[0], bad

    def framework_unregistered(sub):
        m._add_write_flags(sub.add_parser("zap"), "zap")

    monkeypatch.setattr(m, "COMMANDS", m.COMMANDS + (framework_unregistered,))
    with pytest.raises(RuntimeError, match="not in WRITE_VERBS"):
        m.build_parser()  # so the CLI itself cannot start: nobody forgets silently
    monkeypatch.undo()

    # a registered entry that is neither wired nor on the not-built list (a typo, or a verb that vanished)
    bad = _scan(m.build_parser(), m.WRITE_VERBS + ("frobnicate",), m.LOCAL_APPLY, NOT_BUILT)
    assert bad == ["frobnicate: registered but not wired, and not on the not-built list with its owning task"]
    # a local --apply allowlist entry for a verb that cannot write locally
    assert _scan(m.build_parser(), m.WRITE_VERBS, dict(m.LOCAL_APPLY, get="x"), NOT_BUILT) == [
        "get: listed as a local --apply verb but has no --apply"]
    # a local --apply verb that grows plan flags is a write verb
    def branch_plan_out(sub):
        sub.choices["branch"].add_argument("--plan-out")

    assert tree_with(branch_plan_out) == ["branch: can write (--apply, --plan-out) but is not registered in WRITE_VERBS"]

    def interior_apply(sub):  # a parser with subcommands AND its own --apply
        p = sub.add_parser("zap")
        p.add_argument("--apply", action="store_true")
        p.add_subparsers(dest="z").add_parser("now")

    def dest_apply(sub):  # another spelling of the same switch
        sub.add_parser("zap").add_argument("--go", dest="apply", action="store_true")

    assert tree_with(interior_apply) == ["zap: can write (--apply) but is not registered in WRITE_VERBS"]
    assert tree_with(dest_apply) == ["zap: can write (--go (dest apply)) but is not registered in WRITE_VERBS"]

    def local_dest_apply(sub):  # a local --apply verb cannot hide a second switch behind the allowlist
        sub.choices["branch"].add_argument("--go", dest="apply", action="store_true")

    assert tree_with(local_dest_apply) == ["branch: can write (--apply, --go (dest apply)) but is not registered in WRITE_VERBS"]


def test_the_scan_is_alias_aware(monkeypatch):
    def add(sub):
        p = sub.add_parser("demo", aliases=["dm"])
        m._add_write_flags(p, "demo")
        p.add_subparsers(dest="d").add_parser("deep", aliases=["dp"])
    monkeypatch.setattr(m, "WRITE_VERBS", m.WRITE_VERBS + ("demo",))
    monkeypatch.setattr(m, "COMMANDS", m.COMMANDS + (add,))
    assert _scan(m.build_parser(), m.WRITE_VERBS, m.LOCAL_APPLY, NOT_BUILT) == []
    # an alias is not a verb: registering it is an entry that nothing wires (fail closed)
    assert _scan(m.build_parser(), m.WRITE_VERBS + ("dm",), m.LOCAL_APPLY, NOT_BUILT) == [
        "dm: registered but not wired, and not on the not-built list with its owning task"]


def test_no_parser_accepts_an_abbreviated_option():
    """Every parser of the tree, at any depth, has allow_abbrev False (set by build_parser after every verb is added, so a new verb is
    covered without remembering): `--app` must never mean `--apply`."""
    parsers = [p for _, p in _parsers(m.build_parser())]
    assert len(parsers) > 15 and all(p.allow_abbrev is False for p in parsers)
    assert len(m._all_parsers(m.build_parser())) == len(parsers)


ABBREVIATIONS = ["--a", "--ap", "--app", "--appl", "--plan", "--exp"]


@pytest.mark.parametrize("abbr", ABBREVIATIONS)
@pytest.mark.parametrize("argv", [["demo"], ["branch", KEY], ["stack", "init", "--seed", "python-pytest"], ["stack", "approve"]],
                         ids=["demo", "branch", "stack-init", "stack-approve"])
def test_an_abbreviated_x2_flag_is_a_usage_error(argv, abbr, repo, fake, demo, capsys):
    head = subprocess.run(["git", "symbolic-ref", "HEAD"], capture_output=True, text=True).stdout
    extra = [abbr] if abbr in ("--a", "--ap", "--app", "--appl") else [abbr, "p.md"]
    before = _snapshot(repo)
    with pytest.raises(SystemExit) as e:
        run(fake, *argv, *extra)
    assert e.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err
    assert fake.log == [] and _snapshot(repo) == before
    assert subprocess.run(["git", "symbolic-ref", "HEAD"], capture_output=True, text=True).stdout == head


def test_a_wired_verb_must_leave_the_not_built_list(demo):
    assert _scan(m.build_parser(), m.WRITE_VERBS, m.LOCAL_APPLY, NOT_BUILT) == []
    assert _scan(m.build_parser(), m.WRITE_VERBS, m.LOCAL_APPLY, dict(NOT_BUILT, demo="W9")) == [
        "demo: wired now: remove it from NOT_BUILT so its AC1 test runs"]


# ---- fixtures ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo with a config, a DoD ledger and a decisions file for SPK-1; cwd inside it."""
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    for k in ("SKJ_EMAIL", "SKJ_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, stdin=subprocess.DEVNULL)
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK"}))
    (tmp_path / "dod").mkdir()
    (tmp_path / "dod" / "DoD_SPK-1.md").write_text(  # one open question: `questions --post` plans a real post (W4)
        "# DoD SPK-1\n\n## 1. Acceptance criteria\n\n- AC1 a fictional criterion\n\n## 2. Brainstorm & plan\n"
        "- [ ] Q1: A fictional open question? → _(open)_ · owner: PO\n\n## 3. AC → tests\n\n"
        "## 5. Definition of Done\n- [x] gates green\n")  # section 5 ticked: `done` plans a real close (P4)
    (tmp_path / "dod" / "Decisions_SPK-1.md").write_text(  # one open decision: `decisions --post` plans a real post (W5)
        "# Decisions SPK-1\n\n- [ ] D1: A fictional decision → _(open)_ · kind: decision · why: a fictional reason\n")
    (tmp_path / "tasks.md").write_text("- [ ] T001 a fictional task\n- [x] T002 a ticked one\n- [x] T003 an existing one\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def fake():
    f = FakeJira()
    assert f.add_issue("A fictional story") == KEY
    f.add_issue("T003 an existing one", type="Subtask", parent=KEY, labels=["skj"])  # push plans its done transition
    return f


def _snapshot(root):
    out = {}
    for d, dirs, files in os.walk(str(root)):
        dirs[:] = [x for x in dirs if x != ".git"]
        for n in files + [x for x in dirs]:
            p = os.path.join(d, n)
            st = os.lstat(p)
            out[os.path.relpath(p, str(root))] = (st.st_mode, open(p, "rb").read() if os.path.isfile(p) and not os.path.islink(p) else None)
    return out


def _cmd_demo(args, client_factory):
    """A synthetic write verb: one GET, the plan, then (only when the gate allows it) one PUT."""
    c = client_factory()
    issues.get(c, KEY, ["summary"])
    text = "Plan for %s\n\n| action | field | value |\n|---|---|---|\n| edit | summary | %s |\n" % (KEY, args.to)
    if m.plan_gate(args, text):
        c.request("PUT", "/issue/" + KEY, body={"fields": {"summary": args.to}})
        print("applied")
    else:
        print("dry run")
    return 0


@pytest.fixture
def demo(monkeypatch):
    def add(sub):
        p = sub.add_parser("demo")
        p.add_argument("--to", default="new title")
        m._add_write_flags(p, "demo")
        p.set_defaults(func=_cmd_demo)
    monkeypatch.setattr(m, "WRITE_VERBS", m.WRITE_VERBS + ("demo",))
    monkeypatch.setattr(m, "COMMANDS", m.COMMANDS + (add,))


def run(fake, *argv):
    return m.main(list(argv), client_factory=fake.client)


# ---- W1-AC1 -----------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("verb", PLAN_LIST)
def test_no_write_without_apply(verb, repo, fake, capsys, monkeypatch):
    """W1-AC1: each registered verb that exists, run without --apply, sends no non-GET and no write-class POST and changes no local file
    (with --plan-out, the plan file is the one file written)."""
    tree = _tree(m.build_parser())
    if not _exists(tree, verb):
        pytest.skip("%s not built until %s" % (verb, NOT_BUILT[verb]))
    before = _snapshot(repo)
    ran = _gh_guard(monkeypatch)
    code = run(fake, *ARGV[verb])
    assert ran == [], "a dry run executed gh or git push"
    if verb == "pr create":  # P1-AC2: a real plan, exit 0, no `gh pr create` argv executed
        assert code == 0 and "Plan for pull request" in capsys.readouterr().out
    assert fake.writes() == [] and all(e["method"] == "GET" or (e["method"], e["path"]) == ("POST", "/search/jql") for e in fake.log)
    assert _snapshot(repo) == before, "a file was written without --apply"
    assert code in (0, 1, 2)
    if verb == "done":  # P4: a real plan (attach, then the done transition), reads only, exit 0
        assert code == 0 and [(e["method"], e["path"]) for e in fake.log] == [
            ("GET", "/issue/" + KEY), ("GET", "/issue/%s/transitions" % KEY), ("GET", "/issue/" + KEY)]
    if verb in ("questions --post", "decisions --post", "pr link"):  # a real plan: an early refusal must not pass as "no write"
        assert code == 0 and [(e["method"], e["path"]) for e in fake.log] == [
            ("GET", "/issue/" + KEY), ("GET", "/myself"), ("GET", "/issue/%s/comment" % KEY), ("GET", "/issue/%s/comment" % KEY)] + (
            [("GET", "/issue/%s/remotelink" % KEY)] if verb == "pr link" else [])
    if "--plan-out" in tree[_split(verb)[0]][0]:
        assert run(fake, *ARGV[verb], "--plan-out", ".skj/plan.md") in (0, 1, 2)
        assert fake.writes() == []
        assert set(_snapshot(repo)) - set(before) <= {".skj", ".skj/plan.md"}


def _gh_guard(monkeypatch, out="https://github.com/example-owner/example-repo/pull/5\n"):
    """Records every `gh` and `git push` argv. They answer a canned success; local git calls still run."""
    real, seen = subprocess.run, []

    def run_(argv, *a, **k):
        if argv[0] == "gh" or (argv[0] == "git" and "push" in argv[:12]):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=out.encode(), stderr=b"")
        return real(argv, *a, **k)
    monkeypatch.setattr(subprocess, "run", run_)
    return seen


def test_pr_create_through_the_framework(repo, fake, capsys, monkeypatch):
    """P1 / W1-AC2: --apply --expect-plan with a missing or changed plan exits 2 and runs no gh; the approved plan applies exactly once."""
    ran = _gh_guard(monkeypatch)
    argv = ARGV["pr create"]
    git = ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid"]  # the head branch exists locally and on origin, as after a push
    for cmd in (git + ["commit", "-q", "--allow-empty", "-m", "x"], ["git", "branch", "feat/x"], ["git", "update-ref", "refs/remotes/origin/feat/x", "feat/x"]):
        subprocess.run(cmd, check=True, capture_output=True, stdin=subprocess.DEVNULL)
    before = _snapshot(repo)
    assert run(fake, *argv, "--apply", "--expect-plan", "nope.md") == 2
    assert ran == [] and fake.log == [] and set(_snapshot(repo)) - set(before) == {"nope.md.new"}
    assert run(fake, *argv, "--plan-out", "p.md") == 0 and ran == []
    (repo / "tasks.md").write_text("- [ ] T001 a fictional task\n- [ ] T009 another\n")  # the plan no longer matches
    assert run(fake, *argv, "--apply", "--expect-plan", "p.md") == 2 and ran == []
    (repo / "tasks.md").write_text("- [ ] T001 a fictional task\n- [x] T002 a ticked one\n- [x] T003 an existing one\n")
    capsys.readouterr()
    assert run(fake, *argv, "--apply", "--expect-plan", "p.md") == 0
    assert [a[:3] for a in ran] == [["gh", "pr", "create"]] and "--draft" in ran[0] and fake.log == []


def test_framework_no_write_without_apply(repo, fake, demo, capsys):
    """The framework itself: preview and --plan-out never write; --apply alone (the fork's terminal path) writes once."""
    before = _snapshot(repo)
    assert run(fake, "demo") == 0 and "dry run" in capsys.readouterr().out
    assert fake.writes() == [] and _snapshot(repo) == before
    assert run(fake, "demo", "--plan-out", "p.md") == 0
    assert fake.writes() == [] and set(_snapshot(repo)) - set(before) == {"p.md"}
    assert run(fake, "demo", "--apply") == 0 and "applied" in capsys.readouterr().out
    assert [(e["method"], e["path"]) for e in fake.writes()] == [("PUT", "/issue/" + KEY)]


def test_expect_plan_needs_apply(repo, fake, demo, capsys):
    with pytest.raises(SystemExit) as e:
        run(fake, "demo", "--expect-plan", "p.md")
    assert e.value.code == 2 and "--expect-plan needs --apply" in capsys.readouterr().err
    assert fake.log == []


# ---- W1-AC2 -----------------------------------------------------------------------------------------------------------------------

def test_expect_plan_mismatch_exit_2_writes_nothing(repo, fake, demo, capsys):
    """W1-AC2: the approved plan no longer matches: exit 2, no Jira write, no file written but <F>.new (the fresh plan, 0600)."""
    assert run(fake, "demo", "--plan-out", "p.md") == 0
    approved = (repo / "p.md").read_bytes()
    capsys.readouterr()
    before = _snapshot(repo)
    assert run(fake, "demo", "--to", "another title", "--apply", "--expect-plan", "p.md") == 2
    err = capsys.readouterr().err
    assert fake.writes() == []
    after = _snapshot(repo)
    assert set(after) - set(before) == {"p.md.new"} and {k: v for k, v in after.items() if k != "p.md.new"} == before
    fresh = (repo / "p.md.new").read_bytes()
    assert b"another title" in fresh and (repo / "p.md").read_bytes() == approved
    assert (os.lstat(str(repo / "p.md.new")).st_mode & 0o777) == 0o600
    assert "plan changed since approval" in err and "another title" not in err  # a fixed text: never the plan's content
    assert hashlib.sha256(fresh).hexdigest() in err
    for secret in (fake.token, fake.email):
        assert secret.encode() not in fresh and secret.encode() not in approved

    # a missing approved file is a mismatch too (the fork's behaviour): exit 2 and only <F>.new
    assert run(fake, "demo", "--apply", "--expect-plan", "nope.md") == 2
    assert fake.writes() == [] and (repo / "nope.md.new").is_file() and not (repo / "nope.md").exists()

    # approve the fresh plan, then the apply matches and writes exactly once; a new preview clears the stale <F>.new
    assert run(fake, "demo", "--to", "another title", "--plan-out", "p.md") == 0
    assert not (repo / "p.md.new").exists()
    assert run(fake, "demo", "--to", "another title", "--apply", "--expect-plan", "p.md") == 0
    assert [(e["method"], e["path"]) for e in fake.writes()] == [("PUT", "/issue/" + KEY)]


def test_plan_is_canonical_and_its_hash_is_shown(repo, fake, demo, capsys):
    assert run(fake, "demo", "--plan-out", "a.md") == 0
    err = capsys.readouterr().err
    assert run(fake, "demo", "--plan-out", "b.md") == 0
    a, b = (repo / "a.md").read_bytes(), (repo / "b.md").read_bytes()
    assert a == b and a.endswith(b"\n") and b"\r" not in a
    assert "plan sha256 " + hashlib.sha256(a).hexdigest() in err
    assert (os.lstat(str(repo / "a.md")).st_mode & 0o777) == 0o600
    assert m._plan_bytes("x\r\ny\rz") == b"x\ny\nz\n" == m._plan_bytes("x\ny\nz\n")


def test_a_long_plan_gets_the_gate_warning_first(repo):
    long = "".join("| row %d |\n" % i for i in range(300))
    data = m._plan_bytes(long)
    first = data.split(b"\n", 1)[0].decode("utf-8")
    assert "longer than the approval gate can show (300 lines)" in first and data.endswith(long.encode())
    assert m._plan_bytes("".join("| r |\n" for _ in range(m.GATE_VISIBLE_LINES))).startswith(b"| r |")


def test_an_oversize_plan_is_refused_and_nothing_is_written(repo, fake, demo, capsys):
    assert len(m._plan_bytes("a" * (m.MAX_PLAN - 1))) == m.MAX_PLAN
    with pytest.raises(ValueError, match="^the plan is larger than 1 MiB; nothing written$"):
        m._plan_bytes("a" * m.MAX_PLAN)
    with pytest.raises(ValueError, match="^the plan is larger than 1 MiB; nothing written$"):
        m._plan_bytes("é" * (m.MAX_PLAN // 2))  # 2 bytes each in UTF-8: over the cap in bytes, not in characters
    with pytest.raises(ValueError, match="^the plan cannot be encoded as UTF-8; nothing written$"):
        m._plan_bytes("bad \udcff")
    before = _snapshot(repo)
    assert run(fake, "demo", "--to", "x" * m.MAX_PLAN, "--plan-out", "p.md") == 1
    assert "larger than 1 MiB" in capsys.readouterr().err and fake.writes() == [] and _snapshot(repo) == before


# ---- plan files: what is refused ----------------------------------------------------------------------------------------------------

def _approve(fake, repo):
    assert run(fake, "demo", "--plan-out", "real.md") == 0
    return (repo / "real.md").read_bytes()


E_KIND = "skj: the approved plan file is not a regular file (a symlink, directory, FIFO or device is refused); nothing written\n"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "oversize"])
def test_a_bad_approved_plan_file_is_refused_and_nothing_is_written(kind, repo, fake, demo, capsys):
    approved = _approve(fake, repo)
    f = repo / "p.md"
    if kind == "symlink":
        f.symlink_to("real.md")  # even a link to the identical approved plan
    elif kind == "fifo":
        os.mkfifo(str(f))
    elif kind == "directory":
        f.mkdir()
    else:
        f.write_bytes(approved + b"x" * m.MAX_PLAN)
    capsys.readouterr()
    before = _snapshot(repo)
    assert run(fake, "demo", "--apply", "--expect-plan", "p.md") == 1
    err = capsys.readouterr().err
    assert err == ("skj: the approved plan file is larger than 1 MiB; nothing written\n" if kind == "oversize" else E_KIND)
    assert fake.writes() == [] and _snapshot(repo) == before


def test_a_planted_symlink_at_the_new_file_is_replaced_not_followed(repo, fake, demo, capsys):
    _approve(fake, repo)
    (repo / "p.md").write_text("an older plan\n")
    victim = repo / "victim.txt"
    victim.write_text("keep me\n")
    (repo / "p.md.new").symlink_to("victim.txt")
    assert run(fake, "demo", "--apply", "--expect-plan", "p.md") == 2
    assert victim.read_text() == "keep me\n" and fake.writes() == []
    assert not os.path.islink(str(repo / "p.md.new")) and (repo / "p.md.new").read_bytes() == (repo / "real.md").read_bytes()
    assert (os.lstat(str(repo / "p.md.new")).st_mode & 0o777) == 0o600
    # the same for --plan-out: a link at F is replaced, its target untouched
    (repo / "out.md").symlink_to("victim.txt")
    assert run(fake, "demo", "--plan-out", "out.md") == 0
    assert victim.read_text() == "keep me\n" and not os.path.islink(str(repo / "out.md"))
    assert not [n for n in os.listdir(str(repo)) if n.endswith(".tmp")]


@pytest.mark.parametrize("flag", ["--plan-out", "--expect-plan"])
@pytest.mark.parametrize("path,why", [("p\x00.md", "a NUL"), (".git/p.md", ".git"), ("sub/.GIT/hooks/p.md", ".git"), ("", "empty")])
def test_plan_paths_are_refused(flag, path, why, repo, fake, demo, capsys):
    argv = ["demo", flag, path] + (["--apply"] if flag == "--expect-plan" else [])
    before = _snapshot(repo)
    with pytest.raises(SystemExit) as e:
        run(fake, *argv)
    assert e.value.code == 2 and "plan path" in capsys.readouterr().err
    assert fake.log == [] and _snapshot(repo) == before
    assert not (repo / ".git" / "p.md").exists()


def test_plan_out_creates_its_parent_directory_as_the_fork_does(repo, fake, demo):
    assert run(fake, "demo", "--plan-out", ".skj/plans/p.md") == 0
    assert (repo / ".skj" / "plans" / "p.md").is_file()
