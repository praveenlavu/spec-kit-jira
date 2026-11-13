#!/usr/bin/env python3
"""The local half of the CI rehearsal: the checks a CI job runs, run here on throwaway git repositories.

Each case in cases.json is built as a bare "origin" repository (a temp directory, cloned over `file://`, so no network) holding `main` and a
`feature` branch made from the static files under fixtures/ (fictional names only). The case's clones are made the way a CI checkout makes
them (`depth: 1` is `fetch-depth: 1`, the default of actions/checkout; no depth is `fetch-depth: 0`), then the same `skj` commands a CI
template runs are executed in a clone, with CI=true, a private HOME, every proxy on a closed port and no Jira credentials, and the exit
code, the stderr text and a subset of the JSON on stdout are compared with the case's expectation. Nothing here talks to Jira or GitHub.
A pass shows the checks give the expected answer on a fixture; it does not show that a hosted runner or a branch protection rule behaves the
same way: that is what the release checklist's run on real runners is for.

    python3 scripts/ci_smoke/run_local.py          # all cases, exit 0 only when every step gave its expected result
    python3 scripts/ci_smoke/run_local.py g0       # some cases

Needs git and a source checkout (it puts the checkout's `src/` on PYTHONPATH itself; nothing is installed). Stdlib only, Python 3.9.
Cases a to d (C1) run the `run:` steps of the four ci/templates/ files, read from their YAML (needs pyyaml, the dev extra), against
the fake Jira (tests/fakejira.py): each job gets a fresh Python whose stub pip installs a `skj` that is this checkout's real CLI with its
Jira client wired to the fake (see run_job and tests/test_ci_smoke_fixtures.py).
"""
import json
import os
import pickle
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FIX = HERE / "fixtures"
_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            **{"GIT_%s_%s" % (k, f): v for k in ("AUTHOR", "COMMITTER") for f, v in (("NAME", "Smoke"), ("EMAIL", "smoke@example.invalid"))}}
_PROXY = "http://127.0.0.1:9"  # a closed port: a request that escapes fails at once instead of reaching a network


def load_cases():
    return json.loads((HERE / "cases.json").read_text(encoding="utf-8"))


def _env(home):
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C.UTF-8", "CI": "true",
            "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1", "HTTP_PROXY": _PROXY, "HTTPS_PROXY": _PROXY,
            "http_proxy": _PROXY, "https_proxy": _PROXY, "NO_PROXY": "", **_GIT_ENV}


def _git(env, cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)


def _copy(src, dst):
    for root, dirs, files in os.walk(src):  # a link already at a destination path is replaced (copytree would raise or write through it)
        for name in dirs + files:
            s, d = Path(root, name), Path(dst, os.path.relpath(os.path.join(root, name), src))
            if d.is_symlink() or (s.is_symlink() and d.is_file()):
                d.unlink()
    # modes, mtimes and symlinks as a checkout has them; git still sees every change (an overwrite gets a new ctime, `git add -A` rehashes)
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True, copy_function=shutil.copy2)


def _origin(tmp, env, base, branch, main_config=None, branch_config=None, plant=False):
    """The bare origin: `base` holds the fixture config (or `main_config`), `branch` is base plus two commits naming the story (the fixture
    files, then a change), and with `branch_config` the branch also replaces skj.config.json; `plant` adds fixtures/planted/ (modules a
    hostile pull request could add, each leaving a marker in $RUNNER_TEMP if it ever runs). Returns the origin's path."""
    origin, work = tmp / "origin.git", tmp / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, env=env, capture_output=True)
    _git(env, origin, "symbolic-ref", "HEAD", "refs/heads/" + base)
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True, env=env, capture_output=True)
    _git(env, work, "symbolic-ref", "HEAD", "refs/heads/" + base)
    shutil.copyfile(FIX / "skj.config.json", work / "skj.config.json")
    if main_config is not None:
        (work / "skj.config.json").write_text(json.dumps(main_config) + "\n", encoding="utf-8")
    _git(env, work, "add", "-A")
    _git(env, work, "commit", "-q", "-m", "base")
    _git(env, work, "checkout", "-q", "-b", branch)
    _copy(FIX / "feature", work)
    if plant:
        _copy(FIX / "planted", work)
    if branch_config is not None:
        (work / "skj.config.json").write_text(json.dumps(branch_config) + "\n", encoding="utf-8")
    _git(env, work, "add", "-A")
    _git(env, work, "commit", "-q", "-m", "SPK-1 members list")
    (work / "src" / "app.py").write_text("def members():\n    return ['x']\n", encoding="utf-8")
    _git(env, work, "commit", "-q", "-am", "SPK-1 return a member")
    _git(env, work, "push", "-q", origin.as_uri(), base + ":" + base, branch + ":" + branch)
    return origin


def build(case, tmp, env):
    """The bare origin (`main` = the fixture config; the case's branch = main plus one commit per fixture file, each naming the story) and the
    case's clones. Returns {clone name: path}."""
    origin = _origin(tmp, env, case["base"], case["branch"])
    clones = {}
    for name, how in case["clones"].items():
        path = tmp / ("clone-" + name)
        depth = ["--depth", str(how["depth"])] if how.get("depth") else []
        subprocess.run(["git", "clone", "-q", *depth, "--branch", case["branch"], origin.as_uri(), str(path)], check=True, env=env,
                       capture_output=True, stdin=subprocess.DEVNULL)
        clones[name] = path
    return clones


def run(argv, repo, env):
    """(exit code, parsed stdout or None, stderr) of `skj <argv>` run in `repo` by a fresh interpreter."""
    cp = subprocess.run([sys.executable, "-m", "skj", *argv], cwd=str(repo), env=env, capture_output=True, text=True, timeout=120,
                        stdin=subprocess.DEVNULL)
    try:
        doc = json.loads(cp.stdout)
    except ValueError:
        doc = None
    return cp.returncode, doc, cp.stderr


def run_case(name, case, ran=None):
    """[(argv, expected, actual)] for every step whose result differs from the expectation (empty: the case passed). A step's `out` is a
    subset of the JSON the command printed; each text of `stderr_has` must appear in its stderr."""
    if "template" in case:
        return run_template_case(name, case, ran)
    tmp = Path(tempfile.mkdtemp(prefix="ci-smoke-%s-" % name))
    try:
        (tmp / "home").mkdir()
        env = _env(tmp / "home")
        clones = build(case, tmp, env)
        bad = []
        for step in case["steps"]:
            code, doc, err = run(step["argv"], clones[step["clone"]], env)
            shown = doc if isinstance(doc, dict) else {}
            want = {"exit": step["exit"], "out": step.get("out", {}), "stderr_has": step.get("stderr_has", [])}
            got = {"exit": code, "out": {k: shown.get(k, "<absent>") for k in want["out"]},
                   "stderr_has": [t for t in want["stderr_has"] if t in err]}
            if got != want:
                bad.append((step["argv"], want, dict(got, stderr=err.strip()[:300])))
        return bad
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- the template cases (a to d): the `run:` steps of a ci/templates/ file, read from its YAML and run as a runner would ------------------

INSTALL_STEP = "Install skj"  # run as written: `python -I -m pip install ...` reaches the stub pip below (no network)
INSTALL_ARGS = ["install", "git+https://github.com/praveenlavu/spec-kit-jira@<COMMIT-SHA>"]  # the only pip command line the stub accepts: the templates' install line
PR_URL = "https://github.com/example-org/example-repo/pull/12"
_EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}", re.S)
_COND = re.compile(r"(env|vars|github\.event\.pull_request)\.(\w+) (==|!=) (?:'([^']*)'|(true|false))")
_SHIM = """#!%(python)s
# skj for the rehearsal: the real CLI (this checkout's src/), whose client talks to the fake Jira kept in %(state)s.
import pickle, sys
sys.path[:0] = [%(src)r, %(tests)r]
from skj import config
from skj.__main__ import main
with open(%(state)r, "rb") as f:
    fake = pickle.load(f)
real = config.build_client  # every client the CLI builds (site pin, secrets from SKJ_EMAIL/SKJ_TOKEN) gets the fake as its opener
config.build_client = lambda *a, **kw: real(*a, opener=fake, **kw)
try:
    code = main(sys.argv[1:])
finally:
    with open(%(state)r, "wb") as f:
        pickle.dump(fake, f)
sys.exit(code)
"""
_PIP = """# pip for the rehearsal (no network): it accepts only the templates' install line and puts the rehearsal's skj (the file named by
# CI_SMOKE_SKJ) into this environment's bin directory, where a real `pip install` puts the skj console script.
import os, shutil, sys
if sys.argv[1:] != %(args)r:
    sys.exit("rehearsal pip: unexpected arguments %%r" %% (sys.argv[1:],))
dest = os.path.join(os.path.dirname(sys.executable), "skj")
shutil.copyfile(os.environ["CI_SMOKE_SKJ"], dest)
os.chmod(dest, 0o755)
print("rehearsal pip: installed skj")
"""


def _python(tmp, env):
    """A fresh Python environment, as setup-python gives a job: `python`/`python3` in its bin directory, and in its site-packages a stub
    `pip` instead of the real one. A module the pull request plants in the working directory (pip.py, json.py, sitecustomize.py, ...)
    shadows these exactly as it would on a runner whenever a step starts Python without -I from there."""
    venv = tmp / "python"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, env=env, capture_output=True)
    py = venv / "bin" / "python"
    site = subprocess.run([str(py), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()
    (Path(site) / "pip").mkdir()
    (Path(site) / "pip" / "__init__.py").write_text("")
    (Path(site) / "pip" / "__main__.py").write_text(_PIP % {"args": INSTALL_ARGS})
    return venv / "bin"


def _fake():
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        import fakejira
    finally:
        sys.path.remove(str(ROOT / "tests"))
    return fakejira


def load_template(rel):
    """(workflow, its one job) of a ci/templates/ file. The template cases read YAML, so they need pyyaml (the dev extra; never a runtime
    dependency of skj)."""
    import yaml
    wf = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
    (job,) = wf["jobs"].values()
    return wf, job


def _contexts(job_spec, fj):
    """The `${{ }}` values a runner would fill in, from the job's fictional pull request. An expression not listed here is an error."""
    ev = dict({"title": "", "merged": True, "html_url": PR_URL}, **job_spec.get("event", {}))
    secrets = job_spec.get("secrets", {"SKJ_EMAIL": fj.EMAIL, "SKJ_TOKEN": fj.TOKEN})
    values = {"github.head_ref": job_spec["branch"], "github.base_ref": job_spec.get("base", "main"),
              "github.event.pull_request.title": ev["title"], "github.event.pull_request.html_url": ev["html_url"]}
    values.update({"secrets." + k: v for k, v in secrets.items()})
    return values, ev, job_spec.get("vars", {})


def _fill(mapping, values):
    out = {}
    for k, v in (mapping or {}).items():
        v = str(v)
        m = _EXPR.fullmatch(v.strip())
        if m:
            if m.group(1) not in values:
                raise KeyError("the rehearsal has no value for ${{ %s }}" % m.group(1))
            v = values[m.group(1)]
        elif "${{" in v:
            raise ValueError("an env value must be one whole expression or plain text: %r" % v)
        out[k] = v
    return out


def _if(cond, env, ev, vars_):
    """'success' (the default), 'always', True or False for a step's or job's `if:`; only the forms the templates use are known."""
    if cond is None:
        return "success"
    m = _EXPR.fullmatch(str(cond).strip())
    text = m.group(1) if m else str(cond).strip()
    if text == "always()":
        return "always"
    m = _COND.fullmatch(text)
    if not m:
        raise ValueError("the rehearsal cannot evaluate if: %s" % text)
    where, name, op, s, b = m.groups()
    left = env.get(name, "") if where == "env" else vars_.get(name, "") if where == "vars" else ev.get(name)
    right = s if s is not None else (b == "true")
    return (left == right) == (op == "==")


def _checkout(origin, dest, branch, depth, env):
    """What actions/checkout leaves: the branch's commit, the other branches as origin/* when fetch-depth is 0 (only the one commit at
    depth 1, the default), and no origin/HEAD."""
    d = ["--depth", str(depth), "--single-branch"] if depth else ["--no-single-branch"]
    subprocess.run(["git", "clone", "-q", *d, "--branch", branch, origin.as_uri(), str(dest)], check=True, env=env, capture_output=True,
                   stdin=subprocess.DEVNULL)
    _git(env, dest, "remote", "set-head", "origin", "--delete")


def run_job(rel, job_spec, tmp, env0, state):
    """Run one CI job of the template `rel` against a fresh origin, checkout and Python: every `run:` step, in order, honouring `if:`, the step's `env:` (expressions filled in), $GITHUB_ENV between steps, bash -eo pipefail. Returns what the job did."""
    fj = _fake()
    wf, job = load_template(rel)
    values, ev, vars_ = _contexts(job_spec, fj)
    if _if(job.get("if"), {}, ev, vars_) is False:
        return {"exit": None, "output": "", "ran": []}
    shell = ((wf.get("defaults") or {}).get("run") or {}).get("shell")
    (co,) = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
    depth = job_spec.get("fetch_depth", (co.get("with") or {}).get("fetch-depth", 1))
    base, branch = job_spec.get("base", "main"), job_spec["branch"]
    origin = _origin(tmp, env0, base, branch, job_spec.get("main_config"), job_spec.get("branch_config"), job_spec.get("plant", False))
    repo, runner = tmp / "checkout", tmp / "runner"
    runner.mkdir()
    # the checkout is made first, so even the steps before `Get the code` run with the pull request's files in their working directory:
    # stricter than a runner (whose workspace is still empty then), so an install that does not leave it is caught
    _checkout(origin, repo, branch, depth, env0)
    bindir = _python(tmp, env0)
    built = tmp / "skj-built"  # what the stub pip installs as `skj`: the real CLI, its Jira client wired to the fake
    built.write_text(_SHIM % {"python": str(bindir / "python"), "state": str(state), "src": str(ROOT / "src"), "tests": str(ROOT / "tests")})
    files = {"GITHUB_ENV": runner / "env", "GITHUB_STEP_SUMMARY": runner / "summary.md"}
    for f in files.values():
        f.write_text("")
    base_env = dict(env0, PATH=str(bindir) + os.pathsep + env0["PATH"], GITHUB_ACTIONS="true", RUNNER_TEMP=str(runner),
                    CI_SMOKE_SKJ=str(built), **{k: str(v) for k, v in files.items()}, **job_spec.get("env", {}))
    out, ran, failed = [], [], None
    for i, step in enumerate(job["steps"]):
        if "run" not in step or step.get("name") in job_spec.get("skip_steps", []):
            continue
        added = dict(ln.split("=", 1) for ln in files["GITHUB_ENV"].read_text().splitlines() if ln)
        env = dict(base_env, **added, **_fill(step.get("env"), values))
        when = _if(step.get("if"), env, ev, vars_)
        if when is False or (when == "success" and failed is not None):
            continue
        if step.get("shell", shell) != "bash":
            raise ValueError("step %r: the templates run bash only" % step.get("name"))
        script = runner / ("step%d.sh" % i)
        script.write_text(step["run"])
        cp = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)], cwd=str(repo), env=env, capture_output=True,
                            text=True, timeout=300, stdin=subprocess.DEVNULL)
        ran.append(step["name"])
        out.append(cp.stdout + cp.stderr)
        if cp.returncode and failed is None:
            failed = cp.returncode
    return {"exit": failed or 0, "output": "".join(out), "ran": ran, "summary": files["GITHUB_STEP_SUMMARY"].read_text()}


def jira_state(state):
    """(remote links, kit comments) on the story SPK-1 in the fake Jira."""
    with open(str(state), "rb") as f:
        fake = pickle.load(f)
    issue = fake.issue("SPK-1")
    own = [c for c in fake.comments.values() if c["author"]["accountId"] == fake.account_id]
    return len(issue["links"]), len(own)


def run_template_case(name, case, ran=None):
    """[(job title, expected, actual)] for every job of the case whose result differs (empty: the case passed). One fake Jira holds the
    story SPK-1 for the whole case; every job gets a fresh origin and checkout, as every CI run does. A job's `exit` is the first failing
    step's exit code (0 when none failed; null when the job's own `if:` skipped it); `out_has` / `out_lacks` are texts of its output;
    `jira` is [links, comments] after it; `absent` are files that must not exist in the runner's temp directory. `ran`, when given, is a
    set that collects the names of the steps that ran."""
    tmp = Path(tempfile.mkdtemp(prefix="ci-smoke-%s-" % name))
    try:
        fake = _fake().FakeJira()
        fake.add_issue("A fictional story")
        state = tmp / "fake-jira.pickle"
        with open(str(state), "wb") as f:
            pickle.dump(fake, f)
        bad = []
        for n, spec in enumerate(case["jobs"]):
            jt = tmp / ("job%d" % n)
            (jt / "home").mkdir(parents=True)
            got = run_job(case["template"], spec, jt, _env(jt / "home"), state)
            if ran is not None:
                ran.update(got["ran"])
            want = {"exit": spec["exit"], "out_has": spec.get("out_has", []), "out_lacks": [], "ran": spec.get("ran", got["ran"]),
                    "jira": spec.get("jira"), "absent": spec.get("absent", [])}
            seen = {"exit": got["exit"], "out_has": [t for t in want["out_has"] if t in got["output"]],
                    "out_lacks": [t for t in spec.get("out_lacks", []) if t in got["output"]], "ran": got["ran"],
                    "jira": list(jira_state(state)) if spec.get("jira") is not None else None,
                    "absent": [p for p in want["absent"] if not (jt / "runner" / p).exists()]}
            if seen != want:
                bad.append((spec["title"], want, dict(seen, output=got["output"].strip()[-600:])))
        return bad
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    cases, wanted = load_cases(), (argv if argv is not None else sys.argv[1:])
    unknown = [w for w in wanted if w not in cases]
    if unknown:
        print("unknown case(s): %s; known: %s" % (" ".join(unknown), " ".join(cases)), file=sys.stderr)
        return 2
    failed = 0
    for name in wanted or cases:
        bad = run_case(name, cases[name])
        failed += bool(bad)
        print("(%s) %s: %s" % (name, cases[name]["title"], "ok" if not bad else "DIFFERS"))
        for what, want, got in bad:
            print("    %s: expected %s, got %s" % (" ".join(what) if isinstance(what, list) else what, want, got))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
