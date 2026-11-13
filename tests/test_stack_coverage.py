"""P2-16a: a profile with no test command makes any strict.test_command drift, the coverage rule of `stack show --check`, and detected roots a
profile cannot name are listed but never counted as `stack-unprofiled`.

Deferred with the strict mode (L5) and the docs (C2), each named in the report: the halves of these tests that run `strict check`, the CI
trust fixtures (`_ci`, `_stk`, `_impl`), `gate.ignored_roots_note` and the docs pin. What is kept runs the `show --check` side of each."""
import json

import pytest

from skj import stack
from skj.__main__ import main
from test_stack_support import approve, commit, git, profile, git_env  # noqa: F401  (fixture)
from test_stack_show import config, put

NULL_WHY = "tests run on the platform"


def null_profile(**kw):
    p = profile(**kw)
    p["stacks"][0]["commands"]["test"] = {"run": None, "why": NULL_WHY}
    return p


def _show(capsys, *flags):
    code = main(["stack", "show", *flags])
    return code, json.loads(capsys.readouterr().out)


def test_null_test_command_profile_flags_any_config_command(tmp_path, monkeypatch, capsys, git_env):
    approved = approve(null_profile())
    # local `stack show --check`: strict config with a hand-set command is drift; without one it is clean
    loc = tmp_path / "loc"
    loc.mkdir()
    git(loc, "init", "-q", "-b", "main")
    commit(loc, "init", {"pyproject.toml": "[project]\n"})
    put(loc, approved)
    monkeypatch.chdir(loc)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    config(loc, "strict")
    code, res = _show(capsys, "--check")
    assert (code, res["notes"], res["test_command"]["drift"]) == (0, [], False)
    config(loc, "strict", test_command="echo hi")
    code, res = _show(capsys, "--check")
    assert code == 2 and res["test_command"] == {"profile": None, "config": "echo hi", "drift": True}
    assert res["notes"] and all(n.startswith("stack-profile:") for n in res["notes"]) and "echo hi" not in json.dumps(res["notes"])


def _case(files, pid="python", root="."):
    p = profile()
    p["stacks"][0].update(id=pid, root=root)
    p["stacks"][0]["commands"]["test"] = {"run": None, "why": NULL_WHY}
    return approve(p), files


COVERAGE = {
    "same-root-other-family": (("python", "."), {"pyproject.toml": "[project]\n", "package.json": "{}\n", "tsconfig.json": "{}\n"}, ["./typescript"]),
    "same-root-node-vs-python": (("python", "."), {"pyproject.toml": "[project]\n", "package.json": "{}\n"}, ["./node"]),
    "nested-root": (("python", "."), {"pyproject.toml": "[project]\n", "web/package.json": "{}\n"}, ["web/node"]),
    "nested-other-stack": (("node", "."), {"package.json": "{}\n", "api/pyproject.toml": "[project]\n"}, ["api/python"]),
    "same-family-ts-for-node": (("typescript", "."), {"package.json": "{}\n"}, []),
    "same-family-flutter-for-dart": (("flutter", "."), {"pubspec.yaml": "name: x\n"}, []),
    "other-family-flutter-for-node": (("flutter", "."), {"package.json": "{}\n"}, ["./node"]),
    "free-id-covers-root": (("web-app", "."), {"package.json": "{}\n"}, []),
    "profile-root-elsewhere": (("python", "api"), {"pyproject.toml": "[project]\n"}, ["./python"]),
    "covered-both": (("python", "."), {"pyproject.toml": "[project]\n"}, []),
}


@pytest.mark.parametrize("case", COVERAGE)
def test_show_check_family_coverage(tmp_path, monkeypatch, capsys, git_env, case):
    (pid, root), files, want = COVERAGE[case]
    prof, files = _case(files, pid, root)
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "init", files)
    put(tmp_path, prof)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    config(tmp_path, "strict")
    code, res = _show(capsys, "--check")
    shown = sorted(n.split()[1] + "/" + n.split()[2].strip("()") for n in res["notes"] if n.startswith("stack-unprofiled:"))
    assert shown == sorted(want), (shown, want)  # the fork also compared this with strict's own finding (L5)
    assert (code == 2) == bool(want)


def test_unrepresentable_roots_are_ignored_but_listed(tmp_path, monkeypatch, capsys, git_env):
    git(tmp_path, "init", "-q", "-b", "main")
    files = {"pyproject.toml": "[project]\n", ".github/tools/package.json": "{}\n", "my app/package.json": "{}\n", "café/package.json": "{}\n",
             "docs/.hidden/package.json": "{}\n"}
    commit(tmp_path, "init", files)
    res = stack.detect(str(tmp_path))
    assert [(s["id"], s["root"]) for s in res["stacks"]] == [("python", ".")] and res["outcome"] == "detected"
    ig = res["ignored_roots"]
    assert ig["count"] == 4 and len(ig["paths"]) == 4 and ".github/tools" in ig["paths"]
    assert "my app" not in json.dumps(res) and "café" not in json.dumps(res) and sum(p.startswith("<path #") for p in ig["paths"]) == 2
    # at most 5 are listed, the count is the whole
    many = {f"d{n} x/package.json": "{}\n" for n in range(7)}
    commit(tmp_path, "more", many)
    ig = stack.detect(str(tmp_path))["ignored_roots"]
    assert ig["count"] == 11 and len(ig["paths"]) == 5
    # a repo with nothing unrepresentable has no key at all (the output is unchanged)
    clean = tmp_path / "clean"
    clean.mkdir()
    git(clean, "init", "-q", "-b", "main")
    commit(clean, "init", {"pyproject.toml": "[project]\n"})
    assert "ignored_roots" not in stack.detect(str(clean))
    # an unrepresentable-only repo is `unknown`, and `skj stack detect` prints the key
    only = tmp_path / "only"
    only.mkdir()
    git(only, "init", "-q", "-b", "main")
    commit(only, "init", {".github/tools/package.json": "{}\n"})
    res = stack.detect(str(only))
    assert res["outcome"] == "unknown" and res["stacks"] == [] and res["ignored_roots"] == {"count": 1, "paths": [".github/tools"]}
    assert main(["stack", "detect", "--repo", str(only)]) == 0
    assert json.loads(capsys.readouterr().out)["ignored_roots"]["count"] == 1
    # `show --check` does not count them
    d = tmp_path / "strict"
    d.mkdir()
    git(d, "init", "-q", "-b", "main")
    prof, _ = _case({})
    commit(d, "init", files)
    put(d, prof)
    monkeypatch.chdir(d)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    config(d, "strict")
    code, shown = _show(capsys, "--check")
    assert code == 0 and shown["notes"] == []


@pytest.mark.parametrize("root", [".", "a", "a/b", "a-b_c.d/e", "x" * 200])
def test_representable_roots_match_the_validator(root):
    assert stack.representable_root(root)


@pytest.mark.parametrize("root", [".github", "a/.b", "a b", "café", "../x", "-rf", "a//b", "", "x" * 201, "a/", "/a", "a\nb"])
def test_unrepresentable_roots_match_the_validator(root):
    p = profile()
    p["stacks"][0]["root"] = root
    assert not stack.representable_root(root) and stack.validate(p)
