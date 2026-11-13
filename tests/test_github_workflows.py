"""C1: the four GitHub Actions templates users copy (ci/templates/) and this repository's own workflows (.github/workflows/), read as YAML.

The templates run in OTHER people's repositories, on pull requests anyone can open. The rules pinned here:
- no `${{ }}` expression inside a `run:` script (text from the event goes in through `env:`), and expressions only in env/with/if/name
  fields; the checker walks the parsed YAML, so anchors and multi-line scalars are covered, and synthetic bad workflows prove it can fail;
- every `uses:` is `<action>@<40-hex commit> # vX.Y.Z` from the ONE list (test_speckit_contract.ACTION_PINS); no placeholder pin (T1);
- the story key comes from the branch name or the title through env variables and a bash regex that finds what keys.KEY_IN_TEXT_RE finds;
- least privilege: `contents: read`, `persist-credentials: false`, no pull_request_target, secrets only on the two steps allowed to hold
  them (the optional online gate step and the link step), never in the job that runs the pull request's tests (`strict verify`);
- fetch-depth 0 and `--base "origin/$BASE"` (BASE from github.base_ref) wherever skj compares with the target branch; `--title=` form;
- skj is installed BEFORE the checkout, from $RUNNER_TEMP, with `python -I -m pip` at `<COMMIT-SHA>` or the pinned 40-hex commit, and no later step starts python,
  pip or pytest by bare name without -I (a module the PR plants in the working directory would run instead); no `continue-on-error`;
  the link step skips with a notice when there is no Jira login (a merged fork PR gets no secrets).
Each rule has a mutant that breaks it (MUTANTS). `actionlint` is not run here (not installed; tests.yml runs it on GitHub, T4, and it
passed there); the templates have not run in a repository of their own (U14). The kit's own tests.yml has its own rules and mutants (own_ci_violations, OWN_MUTANTS; T4).
"""
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from skj.keys import KEY_IN_TEXT_RE
from test_speckit_contract import ACTION_PINS, INSTALL_PLACEHOLDERS, install_ref

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = {  # the friendly file name -> its earlier technical name (the table in ci/README.md)
    "check-pull-request.yml": "skj-gate",
    "check-branch-name.yml": "skj-branch-guard",
    "link-pull-request.yml": "skj-link-on-merge",
    "final-check.yml": "skj-strict-verify",
}
OWN = ("tests.yml", "speckit-contract.yml")  # this repository's own CI
SECRET_STEPS = {("check-pull-request.yml", "Check the pull request against the checklist and Jira"),
                ("link-pull-request.yml", "Add the link to the Jira story")}
FIND_KEY = "Find the story key"
# Any use of the secrets context: `secrets.X`, `secrets['X']`, `secrets["X"]` and `toJSON(secrets)` (all of them at once) alike.
SECRET_REF = re.compile(r"\bsecrets\b")
# The run's own GitHub token by its context name (`github.token`) or a token-like field of the event payload: never given to a step.
GITHUB_TOKEN_CONTEXT = re.compile(r"\bgithub\.token\b|\bgithub\.event\.[\w.\[\]'\"-]*token", re.I)
HOLDS = re.compile(r"%s|(?i:%s)|TOKEN|SKJ_EMAIL" % (SECRET_REF.pattern, GITHUB_TOKEN_CONTEXT.pattern))  # a step env that holds one
REPO_URL = INSTALL_PLACEHOLDERS[0]
PIN = install_ref((ROOT / "ci/templates/check-pull-request.yml").read_text(encoding="utf-8"))  # `<COMMIT-SHA>` until pinned, then 40-hex
INSTALL = 'cd "$RUNNER_TEMP" && python -I -m pip install "git+%s@%s"' % (REPO_URL, PIN)
# Python or a Python tool started by bare name: after the checkout it must run isolated (-I), or a module the PR plants in the working
# directory (pip.py, json.py, sitecustomize.py, ...) runs instead.
BARE_PYTHON = re.compile(r"(?<![\w./-])(python3?|pip3?|pytest)(?![\w.-])(?!\s+-I\b)")
KEY_REGEX = "re='(^|[^A-Za-z0-9_])([A-Z][A-Z0-9_]{1,9}-[1-9][0-9]{0,9})([^A-Za-z0-9_]|$)'"
UNTRUSTED = re.compile(r"github\s*\.\s*(event|head_ref|ref_name)\b|github\s*\[|\binputs\s*\.")
EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)
SHA = re.compile(r"[0-9a-f]{40}")


def _path(name):
    return ROOT / ("ci/templates/" if name in TEMPLATES else ".github/workflows/") / name


def _text(name):
    return _path(name).read_text(encoding="utf-8")


def _on(wf):
    return wf.get("on", wf.get(True))  # pyyaml reads the key `on` as True


def _steps(wf):
    return [s for job in (wf.get("jobs") or {}).values() for s in job.get("steps") or []]


# ---- C1-AC1: no untrusted expression in a run: block ---------------------------------------------------------------------------------

def expression_violations(text):
    """(field path, tag) for each `${{ }}` in the wrong place. Walks the parsed YAML (aliases are resolved by the loader, block scalars are
    plain strings by then). In a `run:` (or `shell:`) any expression is refused, an untrusted one tagged so; elsewhere expressions may only
    be the value of an `env:` or `with:` entry, an `if:`, a `name:` or the concurrency group, and untrusted ones only under env/with/if."""
    bad = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + (str(k),))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + (str(i),))
        elif isinstance(node, str):
            for expr in EXPR.findall(node):
                where = "/".join(path)
                if path[-1] in ("run", "shell"):
                    bad.append((where, "untrusted expression in run" if UNTRUSTED.search(expr) else "expression in run"))
                elif not (len(path) > 1 and path[-2] in ("env", "with") or path[-1] in ("if", "name", "group")):
                    bad.append((where, "expression outside env/with/if/name"))
                elif UNTRUSTED.search(expr) and not (len(path) > 1 and path[-2] in ("env", "with") or path[-1] == "if"):
                    bad.append((where, "untrusted expression outside env/with/if"))
    walk(yaml.safe_load(text), ())
    return bad


ALL = tuple(TEMPLATES) + OWN


@pytest.mark.parametrize("name", ALL)
def test_no_untrusted_expression_in_run(name):
    """C1-AC1: the real files: no `${{ }}` in any run: script; untrusted contexts only reach a step through env: (or with:/if:)."""
    assert expression_violations(_text(name)) == []


BAD_WORKFLOWS = {  # synthetic workflows the checker must refuse: (yaml text, the tag it must report)
    "title in a run": ("jobs: {j: {steps: [{run: 'echo ${{ github.event.pull_request.title }}'}]}}", "untrusted expression in run"),
    "no spaces": ("jobs: {j: {steps: [{run: 'echo ${{github.event.issue.title}}'}]}}", "untrusted expression in run"),
    "head_ref": ("jobs: {j: {steps: [{run: 'git checkout ${{ github.head_ref }}'}]}}", "untrusted expression in run"),
    "ref_name": ("jobs: {j: {steps: [{run: 'echo ${{ github.ref_name }}'}]}}", "untrusted expression in run"),
    "inputs": ("jobs: {j: {steps: [{run: 'echo ${{ inputs.ref }}'}]}}", "untrusted expression in run"),
    "index form": ("jobs: {j: {steps: [{run: \"echo ${{ github['event'].pull_request.body }}\"}]}}", "untrusted expression in run"),
    "block scalar": ("jobs:\n  j:\n    steps:\n      - run: |\n          echo ok\n          echo \"${{ github.event.pull_request.body }}\"\n",
                     "untrusted expression in run"),
    "folded scalar": ("jobs:\n  j:\n    steps:\n      - run: >\n          echo ${{\n          github.head_ref }}\n", "untrusted expression in run"),
    "an anchor reused as a run": ("x: &cmd 'echo ${{ github.event.comment.body }}'\njobs: {j: {steps: [{run: *cmd}]}}",
                                  "untrusted expression in run"),
    "a secret in a run": ("jobs: {j: {steps: [{run: 'echo ${{ secrets.SKJ_TOKEN }}'}]}}", "expression in run"),
    "a matrix value in a run": ("jobs: {j: {steps: [{run: 'echo ${{ matrix.python }}'}]}}", "expression in run"),
    "shell field": ("jobs: {j: {steps: [{shell: 'bash ${{ github.head_ref }}', run: 'true'}]}}", "untrusted expression in run"),
    "working-directory": ("jobs: {j: {steps: [{working-directory: '${{ github.head_ref }}', run: 'true'}]}}",
                          "expression outside env/with/if/name"),
    "untrusted in a name": ("jobs: {j: {name: '${{ github.event.pull_request.title }}', steps: []}}",
                            "untrusted expression outside env/with/if"),
}


@pytest.mark.parametrize("label", sorted(BAD_WORKFLOWS))
def test_the_expression_checker_refuses_each_bad_workflow(label):
    text, tag = BAD_WORKFLOWS[label]
    assert tag in [t for _, t in expression_violations(text)], label


def test_the_expression_checker_accepts_env_indirection():
    ok = ("jobs:\n  j:\n    if: github.event.pull_request.merged == true\n    name: Python ${{ matrix.python }}\n    steps:\n"
          "      - env:\n          TITLE: ${{ github.event.pull_request.title }}\n        run: echo \"$TITLE\"\n"
          "      - uses: a/b@" + "0" * 40 + " # v1\n        with:\n          ref: ${{ github.event.pull_request.merge_commit_sha }}\n")
    assert expression_violations(ok) == []


# ---- C1-AC2: the templates parse and every action is pinned -------------------------------------------------------------------------

def pin_violations(text):
    """T1-AC3: each `uses:` must be `<action>@<40 hex> # vX.Y.Z` with the commit and version ACTION_PINS lists for that action; a tag, a
    branch, a short SHA, a placeholder, a missing comment or an unlisted action/commit is refused. Every parsed `uses:` is one such line."""
    bad = []
    parsed = [s["uses"] for s in _steps(yaml.safe_load(text)) if "uses" in s]
    lines = re.findall(r"^\s*-?\s*uses:\s*(\S+)(.*)$", text, re.M)
    if sorted(u for u, _ in lines) != sorted(parsed):
        bad.append("uses outside a step line")
    for uses, rest in lines:
        action, _, ref = uses.rpartition("@")
        comment = re.fullmatch(r"\s+#\s+(v\d+\.\d+\.\d+)\s*", rest)
        if not (SHA.fullmatch(ref) and comment):
            bad.append("unpinned: " + uses + rest)
        elif ACTION_PINS.get(action) != (ref, comment.group(1)):
            bad.append("unlisted pin: " + uses + rest)
    return bad


@pytest.mark.parametrize("name", ALL)
def test_actions_pinned(name):
    """C1-AC2, T1-AC2: each file parses as YAML and pins every action to its commit in the one list (ACTION_PINS)."""
    wf = yaml.safe_load(_text(name))
    assert isinstance(wf, dict) and wf.get("jobs")
    assert [s for s in _steps(wf) if "uses" in s], "no action used: the checkout is missing"
    assert pin_violations(_text(name)) == []


_CO_SHA = ACTION_PINS["actions/checkout"][0]


@pytest.mark.parametrize("uses", ["actions/checkout@v4", "actions/checkout@main", "actions/checkout@11bd719",
                                  "actions/checkout@" + _CO_SHA, "actions/checkout@" + _CO_SHA + " # v4",
                                  "actions/checkout@" + _CO_SHA + " # v4.2.1", "actions/checkout@" + "b" * 40 + " # v4.2.2",
                                  "actions/upload-artifact@" + _CO_SHA + " # v4.2.2", "actions/checkout@" + _CO_SHA.upper() + " # v4.2.2",
                                  "actions/checkout@<PIN-SHA-BEFORE-RELEASE> # v4", "actions/checkout@<PIN-SHA-BEFORE-RELEASE>"])
def test_the_pin_checker_refuses_a_tag_a_branch_a_short_sha_a_placeholder_or_an_unlisted_pin(uses):
    """T1-AC3: anything but a listed 40-hex commit with its exact version comment fails."""
    assert pin_violations("jobs:\n  j:\n    steps:\n      - uses: %s\n" % uses)


def test_the_pin_checker_accepts_each_listed_pin():
    for action, (sha, version) in ACTION_PINS.items():
        assert pin_violations("jobs:\n  j:\n    steps:\n      - uses: %s@%s # %s\n" % (action, sha, version)) == []


# ---- the story key: env variables and a bash regex -----------------------------------------------------------------------------------

def _find_key_step(name):
    (step,) = [s for s in _steps(yaml.safe_load(_text(name))) if s.get("name") == FIND_KEY]
    return step


@pytest.mark.parametrize("name", ["check-pull-request.yml", "link-pull-request.yml", "final-check.yml"])
def test_key_taken_via_env_and_regex(name, tmp_path):
    """The key step reads BRANCH and TITLE from env (filled from github.head_ref and the PR title), matches them with one bash regex in the
    C locale, and its result equals the FIRST keys.KEY_IN_TEXT_RE match of the branch, else of the title, on hostile inputs; no input runs
    a command. The step text is the same in every template (and the branch guard uses the same regex line)."""
    step = _find_key_step(name)
    assert step["env"] == {"BRANCH": "${{ github.head_ref }}", "TITLE": "${{ github.event.pull_request.title }}", "LC_ALL": "C"}
    assert KEY_REGEX in step["run"].splitlines()[0]
    assert step["run"] == _find_key_step("check-pull-request.yml")["run"]
    canary = tmp_path / "pwned"
    inputs = [("feature/SPK-12-x", ""), ("feature/x", "SPK-3 Add it"), ("SPK-1", "SPK-2"), ("spk-1", "Spk-2"), ("SPK-0", "SPK-01"),
              ("xSPK-1", "SPK-1x"), ("SPK-1_a", "_SPK-1"), ("SPK-١", "SPK-1é"), ("A-1", "ABCDEFGHIJK-1"), ("AB-CD-1", ""),
              ("SPK-12345678901", "SPK-1234567890"), ("OTHER-2-SPK-1", ""), ("feature/x", "$(touch %s) SPK-4" % canary),
              ("feature/x", "`touch %s`" % canary), ("feature/x", "a\nSPK-5\n--apply"), ("feature/x", "--apply"), ("", ""),
              ("feature/x", "@here SPK-1 #done"), ("A1-1", "X_Y-9")]
    for branch, title in inputs:
        want = (KEY_IN_TEXT_RE.search(branch) or KEY_IN_TEXT_RE.search(title))
        envfile = tmp_path / "env"
        envfile.write_text("")
        cp = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin", "BRANCH": branch, "TITLE": title, "LC_ALL": "C", "GITHUB_ENV": str(envfile)},
                            cwd=str(tmp_path), timeout=30)
        assert cp.returncode == 0, cp.stderr
        assert envfile.read_text() == "KEY=%s\n" % (want.group(0) if want else ""), (branch, title)
    assert not canary.exists()


def test_the_branch_guard_takes_the_first_key_of_the_branch_and_the_project_from_the_target_branch():
    (step,) = [s for s in _steps(yaml.safe_load(_text("check-branch-name.yml"))) if "run" in s]
    assert step["env"] == {"BRANCH": "${{ github.head_ref }}", "BASE": "${{ github.base_ref }}", "LC_ALL": "C"}
    run = step["run"]
    assert KEY_REGEX in run and "TITLE" not in run
    assert 'git show "origin/$BASE:skj.config.json"' in run and "python3 -I -c" in run  # -I: no file of the checkout is imported
    assert '"${BASH_REMATCH[2]%-*}" = "$project"' in run


# ---- no step or job may swallow its own failure ---------------------------------------------------------------------------------------

def continue_violations(text):
    """Paths of every `continue-on-error` key (any value, any level): a failing check must fail the run."""
    bad = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "continue-on-error":
                    bad.append("/".join(path + (k,)))
                walk(v, path + (str(k),))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + (str(i),))
    walk(yaml.safe_load(text), ())
    return bad


@pytest.mark.parametrize("name", ALL)
def test_no_continue_on_error(name):
    assert continue_violations(_text(name)) == []


@pytest.mark.parametrize("text", ["jobs: {j: {continue-on-error: true, steps: []}}", "jobs: {j: {steps: [{run: x, continue-on-error: true}]}}",
                                  "jobs: {j: {steps: [{run: x, continue-on-error: '${{ matrix.ok }}'}]}}",
                                  "x: &s {run: x, continue-on-error: false}\njobs: {j: {steps: [*s]}}"])
def test_the_continue_on_error_checker_finds_it_anywhere(text):
    assert continue_violations(text)


def test_the_install_placeholders_are_the_one_list():
    """REL1 (INSTALL_PLACEHOLDERS in test_speckit_contract.py): each template that installs skj has the one install line, from the public
    repository at `<COMMIT-SHA>` or a full commit, the same in all three, and no other placeholder."""
    assert PIN is not None
    for name in ("check-pull-request.yml", "final-check.yml", "link-pull-request.yml"):
        assert _text(name).count(INSTALL) == 1 and install_ref(_text(name)) == PIN
        assert set(re.findall(r"<[A-Z-]+>", _text(name))) <= {"<COMMIT-SHA>"}
    assert set(re.findall(r"<[A-Z-]+>", _text("check-branch-name.yml"))) == set()


# ---- structure and least privilege -----------------------------------------------------------------------------------------------

def template_violations(name, text):
    """Every way a template breaks the structure rules (tags; [] when it keeps them)."""
    bad = []
    wf = yaml.safe_load(text)
    on = _on(wf) or {}
    want_on = {"pull_request": {"types": ["closed"], "branches": ["main"]}} if name == "link-pull-request.yml" else \
        {"pull_request": {"branches": ["main"]}}
    if on != want_on:
        bad.append("triggers")
    if "pull_request_target" in text:
        bad.append("pull_request_target")
    if wf.get("permissions") != {"contents": "read"}:
        bad.append("permissions")
    jobs = wf.get("jobs") or {}
    if len(jobs) != 1 or any("permissions" in j or "env" in j or "secrets" in j for j in jobs.values()) or "env" in wf:
        bad.append("job shape")  # one job; no job- or workflow-level env (a secret there would reach every step)
    if ((wf.get("defaults") or {}).get("run") or {}).get("shell") != "bash":
        bad.append("shell")
    job = next(iter(jobs.values()), {})
    steps = job.get("steps") or []
    if not all(isinstance(s.get("name"), str) and s["name"].strip() for s in steps):
        bad.append("unnamed step")
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    w = (checkouts[0].get("with") or {}) if len(checkouts) == 1 else {}
    if w.get("persist-credentials") is not False:
        bad.append("persist-credentials")
    if w.get("fetch-depth") != 0:
        bad.append("fetch-depth")
    runs = [s for s in steps if "run" in s]
    for s in runs:
        calls = re.findall(r"\bskj (gate|strict \w+)\b[^\n]*", s["run"])
        if calls and (not all('--base "origin/$BASE"' in c for c in re.findall(r"\bskj (?:gate|strict \w+)\b[^\n]*", s["run"]))
                      or (s.get("env") or {}).get("BASE") != "${{ github.base_ref }}"):
            bad.append("base")
    if name in ("check-pull-request.yml", "final-check.yml", "link-pull-request.yml") and \
            [s.get("name") for s in runs[1:2]] != [FIND_KEY]:
        bad.append("key step")  # the install, then the key, before anything uses it
    code = re.sub(r"(?m)#.*$", "", text)
    for s in steps:
        holds = bool(HOLDS.search(yaml.safe_dump(s.get("env") or {})))
        if holds and (name, s.get("name")) not in SECRET_STEPS:
            bad.append("secret outside its step")
    if re.search(r"\bsecrets\b", code) and not any((name, s.get("name")) in SECRET_STEPS for s in steps):
        bad.append("secret outside its step")
    if "strict verify" in code and re.search(r"\bsecrets\b|SKJ_TOKEN:|SKJ_EMAIL:", code):
        bad.append("secret in the verify job")
    if name == "link-pull-request.yml":
        if job.get("if") != "github.event.pull_request.merged == true":
            bad.append("merged only")
        link = [s["run"] for s in runs if "skj pr link" in s["run"]]
        if not (len(link) == 1 and '--title="$TITLE"' in link[0] and not re.search(r"--title\s", link[0])):
            bad.append("title form")
        if not any(s.get("run") == 'git symbolic-ref refs/remotes/origin/HEAD "refs/remotes/origin/$BASE"' for s in runs):
            bad.append("origin/HEAD")
    if name == "check-pull-request.yml":
        gates = [s for s in runs if "skj gate" in s["run"]]
        offline = [s for s in gates if "--offline" in s["run"]]
        if not (len(gates) == 2 and len(offline) == 1 and offline[0].get("if") == "vars.SKJ_GATE_ONLINE != 'true'"
                and (offline[0].get("env") or {}).keys() == {"BASE"}):
            bad.append("gate variants")
        if any("--summary-out" in s["run"] and '--summary-out "$RUNNER_TEMP/' not in s["run"] for s in gates):
            bad.append("summary path")  # never a path in the checkout (a planted symlink could redirect it)
    if name != "check-branch-name.yml":  # (the branch guard installs nothing)
        names = [s.get("name") for s in steps]
        inst = [s for s in steps if s.get("name") == "Install skj"]
        if not (len(inst) == 1 and inst[0].get("run") == INSTALL and checkouts and names.index("Install skj") < steps.index(checkouts[0])):
            bad.append("install")  # before the checkout, from $RUNNER_TEMP, isolated, at a commit
    after = steps[steps.index(checkouts[0]) + 1:] if checkouts else steps
    if any(BARE_PYTHON.search(s["run"]) for s in after if "run" in s):
        bad.append("interpreter in the checkout")
    if re.search(r"<[A-Z][A-Z-]*>", text.replace("@<COMMIT-SHA>\"", "")):
        bad.append("install")  # no placeholder but the commit one (REL1)
    if name == "link-pull-request.yml":
        link = [s for s in runs if "skj pr link" in s["run"]]
        if not (link and link[0]["run"].splitlines()[1].startswith('[ -n "$SKJ_TOKEN" ] || { echo "::notice::') and "exit 0; }" in link[0]["run"]):
            bad.append("fork guard")
    lines = text.splitlines()
    if not (len(lines) <= 70 and all(re.match(r"# \S", ln) for ln in lines[:3]) and lines[3].startswith("name:")):
        bad.append("header")
    return bad


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_each_template_keeps_its_structure(name):
    assert template_violations(name, _text(name)) == []


# A script started from the checked-out tree: `./x`, `../x`, `bash|sh|zsh x`, `source x`, `. x` with a relative path (not an option, an
# absolute path or a variable). Comments are not stripped: a `#` inside a string could otherwise hide the rest of the line.
TREE_SCRIPT = re.compile(r"(?<![\w/.$-])(?:(?:bash|sh|zsh|source|\.)\s+[\"']?(?![-/$\"'])[^\s;&|\"']|\.{1,2}/)")
MAIN_PUSH = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
SCRUB_TREE = "python3 -I scripts/scrub_check.py"
SCRUB_HISTORY = SCRUB_TREE + " --require-denylist --history"
# The one step of this repository's own CI that holds a secret: (name, run, if). Only on a push to main, so the script is main's own.
OWN_SECRET_STEP = ("Scan the files and the whole history for private words", SCRUB_HISTORY, MAIN_PUSH)
KEY_LINE = 'echo "KEY=$key" >> "$GITHUB_ENV"'  # the one env write a template makes: a key the bash regex matched, no newline possible
# A step holding a secret may run only these line shapes (an allow-list: no interpreter, make, env prefix, `;`, `$(` or redirect can
# fit): `skj` with plain words, `--flags`, `--flag="$VAR"` and quoted variables; the origin/HEAD line; the `[ -n "$VAR" ] || { echo; exit; }`
# guard; a comment. Anything after ` #` is a comment to bash (no token may hold a quote, so the `#` cannot sit inside a string).
_SKJ_TOKEN = r'(?:[a-z][a-z-]*|--[a-z][a-z-]*(?:="\$[A-Z_]+")?|"\$[A-Z_]+"|"origin/\$BASE"|"\$RUNNER_TEMP/[\w.-]+")'
SECRET_STEP_LINE = re.compile(r'(?:skj(?: %s)+|git symbolic-ref refs/remotes/origin/HEAD "refs/remotes/origin/\$BASE"'
                              r'|\[ -n "\$[A-Z_]+" \] \|\| \{ echo "::(?:error|notice)::[^"$`\\]*"; exit [01]; \})(?: +#.*)?|#.*' % _SKJ_TOKEN)


def checkout_violations(name, text):
    """(a) a script from the checkout: in a step that holds a secret any line outside SECRET_STEP_LINE, in a step at or before the isolated
    install a TREE_SCRIPT match (any file); (b) in a template, any $GITHUB_PATH or $GITHUB_ENV write (the PR tree must never influence
    PATH or the environment) except KEY_LINE in the key step."""
    bad = []
    steps = _steps(yaml.safe_load(text))
    names = [s.get("name") for s in steps]
    install = names.index("Install skj") if "Install skj" in names else 0
    for i, s in enumerate(steps):
        run = str(s.get("run", ""))
        holds = bool(HOLDS.search(yaml.safe_dump(s.get("env") or {})))
        if name == "tests.yml" and (s.get("name"), run, s.get("if")) == OWN_SECRET_STEP:
            continue  # T4: the one own-CI step with a secret runs the scrub script of main itself, never of a pull request
        if holds and not all(SECRET_STEP_LINE.fullmatch(ln.strip()) for ln in run.splitlines() if ln.strip()) \
                or i <= install and "Install skj" in names and TREE_SCRIPT.search(run):
            bad.append("script from the checkout")
    if name in TEMPLATES:
        allowed = sum(1 for s in steps if s.get("name") == FIND_KEY and KEY_LINE in [ln.strip() for ln in str(s.get("run", "")).splitlines()])
        if len(re.findall(r"GITHUB_(?:PATH|ENV)\b", text)) > min(allowed, 1):
            bad.append("PATH or env write")
    return bad


@pytest.mark.parametrize("name", ALL)
def test_no_script_from_the_checkout_where_it_matters_and_no_path_or_env_write(name):
    assert checkout_violations(name, _text(name)) == []


def _swap(old, new):
    def go(text):
        assert old in text, "mutant does not apply (the template text changed): %r" % old
        return text.replace(old, new, 1)
    return go


GATE, BRANCH, LINK, FINAL = "check-pull-request.yml", "check-branch-name.yml", "link-pull-request.yml", "final-check.yml"
MUTANTS = {  # name -> (template, text change, the tag it must produce); one per safety property at least
    "inline title in a run": (LINK, _swap('--title="$TITLE"', '--title="${{ github.event.pull_request.title }}"'), "untrusted expression in run"),
    "inline branch in a run": (GATE, _swap('skj gate "$KEY" --base "origin/$BASE" --offline', 'skj gate "${{ github.head_ref }}" --base "origin/$BASE" --offline'),
                               "untrusted expression in run"),
    "a secret in final-check": (FINAL, _swap("          BASE: ${{ github.base_ref }}\n        run: skj strict verify",
                                             "          BASE: ${{ github.base_ref }}\n          SKJ_TOKEN: ${{ secrets.SKJ_TOKEN }}\n        run: skj strict verify"),
                                "secret in the verify job"),
    "a secret on the offline gate": (GATE, _swap("          BASE: ${{ github.base_ref }}\n        run: |\n          [ -n \"$KEY\" ] || { echo \"::error::Put the story key (like PROJ-123) in the branch name or the title.\"; exit 1; }\n          skj gate \"$KEY\" --base \"origin/$BASE\" --offline",
                                                 "          BASE: ${{ github.base_ref }}\n          SKJ_TOKEN: ${{ secrets.SKJ_TOKEN }}\n        run: |\n          [ -n \"$KEY\" ] || { echo \"::error::Put the story key (like PROJ-123) in the branch name or the title.\"; exit 1; }\n          skj gate \"$KEY\" --base \"origin/$BASE\" --offline"),
                                     "secret outside its step"),
    "toJSON(secrets) in final-check": (FINAL, _swap("          BASE: ${{ github.base_ref }}\n        run: skj strict verify",
                                                    "          BASE: ${{ github.base_ref }}\n          D: ${{ toJSON(secrets) }}\n        run: skj strict verify"),
                                       "secret in the verify job"),
    "secrets['X'] on the offline gate": (GATE, _swap("          BASE: ${{ github.base_ref }}\n        run: |\n          [ -n \"$KEY\" ] || { echo \"::error::Put the story key (like PROJ-123) in the branch name or the title.\"; exit 1; }\n          skj gate \"$KEY\" --base \"origin/$BASE\" --offline",
                                                     "          BASE: ${{ github.base_ref }}\n          D: ${{ secrets['OTHER'] }}\n        run: |\n          [ -n \"$KEY\" ] || { echo \"::error::Put the story key (like PROJ-123) in the branch name or the title.\"; exit 1; }\n          skj gate \"$KEY\" --base \"origin/$BASE\" --offline"),
                                         "secret outside its step"),
    "github.token on the offline gate": (GATE, _swap("          BASE: ${{ github.base_ref }}\n        run: |\n          [ -n \"$KEY\" ] || { echo \"::error::Put the story key (like PROJ-123) in the branch name or the title.\"; exit 1; }\n          skj gate \"$KEY\" --base \"origin/$BASE\" --offline",
                                                     "          BASE: ${{ github.base_ref }}\n          G: ${{ github.token }}\n        run: |\n          [ -n \"$KEY\" ] || { echo \"::error::Put the story key (like PROJ-123) in the branch name or the title.\"; exit 1; }\n          skj gate \"$KEY\" --base \"origin/$BASE\" --offline"),
                                         "secret outside its step"),
    "a job-level secret": (LINK, _swap("    timeout-minutes: 10\n", "    timeout-minutes: 10\n    env:\n      SKJ_TOKEN: ${{ secrets.SKJ_TOKEN }}\n"), "job shape"),
    "fetch-depth removed": (GATE, _swap("          fetch-depth: 0   # skj compares", "          # skj compares"), "fetch-depth"),
    "fetch-depth removed (verify)": (FINAL, _swap("          fetch-depth: 0   # the red", "          # the red"), "fetch-depth"),
    "credentials persisted": (FINAL, _swap("persist-credentials: false", "persist-credentials: true"), "persist-credentials"),
    "unpinned uses": (LINK, _swap("actions/checkout@" + ACTION_PINS["actions/checkout"][0] + " # v4.2.2", "actions/checkout@v4"), "pin"),
    "placeholder pin": (BRANCH, _swap(ACTION_PINS["actions/checkout"][0], "<PIN-SHA-BEFORE-RELEASE>"), "pin"),
    "pull_request_target": (GATE, _swap("  pull_request:\n", "  pull_request_target:\n"), "pull_request_target"),
    "space form of --title": (LINK, _swap('--title="$TITLE"', '--title "$TITLE"'), "title form"),
    "write permission": (BRANCH, _swap("  contents: read\n", "  contents: write\n"), "permissions"),
    "--base dropped": (FINAL, _swap('--no-record --base "origin/$BASE"', "--no-record"), "base"),
    "BASE not from base_ref": (GATE, _swap("          BASE: ${{ github.base_ref }}\n        run: |\n          [ -n \"$KEY\" ] || { echo \"::error::Put the story key (like PROJ-123) in the branch name or the title.\"; exit 1; }\n          skj gate \"$KEY\" --base \"origin/$BASE\" --offline",
                                           "          BASE: ${{ github.head_ref }}\n        run: |\n          [ -n \"$KEY\" ] || { echo \"::error::Put the story key (like PROJ-123) in the branch name or the title.\"; exit 1; }\n          skj gate \"$KEY\" --base \"origin/$BASE\" --offline"),
                               "base"),
    "merged check dropped": (LINK, _swap("    if: github.event.pull_request.merged == true", "    if: always()"), "merged only"),
    "origin/HEAD step dropped": (LINK, _swap('        run: git symbolic-ref refs/remotes/origin/HEAD "refs/remotes/origin/$BASE"', "        run: 'true'"),
                                 "origin/HEAD"),
    "link on every close of any kind": (LINK, _swap("    types: [closed]\n", "    types: [closed, opened]\n"), "triggers"),
    "summary in the checkout": (GATE, _swap('--offline --summary-out "$RUNNER_TEMP/gate-summary.md"', "--offline --summary-out gate-summary.md"),
                                "summary path"),
    "gate always online": (GATE, _swap("        if: vars.SKJ_GATE_ONLINE != 'true'\n", ""), "gate variants"),
    "default shell": (FINAL, _swap("defaults:\n  run:\n    shell: bash\n", ""), "shell"),
    "header gone": (BRANCH, _swap("# What this does", "#What this does"), "header"),
    "install after the checkout, not isolated": (GATE, lambda t: _move_install_after_checkout(t, 'python -m pip install "git+%s@%s"' % (REPO_URL, PIN)),
                                                 "install"),
    "install not isolated": (FINAL, _swap("python -I -m pip install", "python -m pip install"), "install"),
    "install from the checkout": (LINK, _swap('cd "$RUNNER_TEMP" && python -I -m pip', "python -I -m pip"), "install"),
    "install on a tag": (GATE, _swap("@%s\"" % PIN, "@v0.1.0\""), "install"),
    "install on a branch": (FINAL, _swap("@%s\"" % PIN, "@main\""), "install"),
    "install on a short sha": (LINK, _swap("@%s\"" % PIN, "@0123456\""), "install"),
    "bare python3 after the checkout": (BRANCH, _swap("python3 -I -c", "python3 -c"), "interpreter in the checkout"),
    "pytest after the checkout": (FINAL, _swap('run: skj strict verify', 'run: pytest -q; skj strict verify'), "interpreter in the checkout"),
    "continue-on-error on the gate step": (GATE, _swap("        if: vars.SKJ_GATE_ONLINE != 'true'\n", "        if: vars.SKJ_GATE_ONLINE != 'true'\n        continue-on-error: true\n"),
                                           "continue-on-error"),
    "continue-on-error on verify": (FINAL, _swap("      - name: Re-run the tests at each red and green commit\n", "      - name: Re-run the tests at each red and green commit\n        continue-on-error: true\n"),
                                    "continue-on-error"),
    "continue-on-error on the job": (LINK, _swap("    timeout-minutes: 10\n", "    timeout-minutes: 10\n    continue-on-error: true\n"), "continue-on-error"),
    "no fork guard": (LINK, _swap('          [ -n "$SKJ_TOKEN" ] ||', '          true ||'), "fork guard"),
    "bash ./script in the link step": (LINK, _swap("          skj pr link", "          bash ./scripts/pre-link.sh\n          skj pr link"), "script from the checkout"),
    "source ./ in the online gate step": (GATE, _swap('          git symbolic-ref refs/remotes/origin/HEAD "refs/remotes/origin/$BASE"   # skj',
                                                      '          source ./ci/env.sh\n          git symbolic-ref refs/remotes/origin/HEAD "refs/remotes/origin/$BASE"   # skj'),
                                          "script from the checkout"),
    "./script in the online gate step": (GATE, _swap('          skj gate "$KEY" --base "origin/$BASE" --summary-out', '          ./gate.sh; skj gate "$KEY" --base "origin/$BASE" --summary-out'),
                                         "script from the checkout"),
    "python -I tool in the online gate step": (GATE, _swap('          git symbolic-ref refs/remotes/origin/HEAD', '          python -I ci/tool.py; git symbolic-ref refs/remotes/origin/HEAD'),
                                               "script from the checkout"),
    "make in the link step": (LINK, _swap('          skj pr link "$KEY"', '          make lint && skj pr link "$KEY"'), "script from the checkout"),
    "PATH prefix on the online gate": (GATE, _swap('          skj gate "$KEY" --base "origin/$BASE" --summary-out', '          PATH="$PWD/bin:$PATH" skj gate "$KEY" --base "origin/$BASE" --summary-out'),
                                       "script from the checkout"),
    "PYTHONPATH prefix on the link": (LINK, _swap('          skj pr link "$KEY"', '          PYTHONPATH=. skj pr link "$KEY"'), "script from the checkout"),
    "env prefix on the link": (LINK, _swap('          skj pr link "$KEY"', '          SKJ_X=1 skj pr link "$KEY"'), "script from the checkout"),
    "command substitution in skj args": (LINK, _swap('--url "$PR_URL"', '--url "$(./u.sh)"'), "script from the checkout"),
    "sh script before the install":(FINAL, _swap("      - name: Install skj\n        run: ", "      - name: Install skj\n        run: sh setup.sh && "), "script from the checkout"),
    "GITHUB_PATH write": (FINAL, _swap('        run: skj strict verify', '        run: echo "$PWD/bin" >> "$GITHUB_PATH"; skj strict verify'), "PATH or env write"),
    "GITHUB_ENV write of PATH": (GATE, _swap('echo "KEY=$key" >> "$GITHUB_ENV"', 'echo "PATH=$PWD:$PATH" >> "$GITHUB_ENV"'), "PATH or env write"),
    "second GITHUB_ENV write in the key step": (LINK, _swap('          echo "KEY=$key" >> "$GITHUB_ENV"\n',
                                                            '          echo "KEY=$key" >> "$GITHUB_ENV"\n          echo "BASH_ENV=$PWD/x" >> $GITHUB_ENV\n'),
                                                "PATH or env write"),
}


def _move_install_after_checkout(text, cmd):
    """The install step (and its comment) cut from before the checkout and put back right after it, running `cmd`."""
    start = text.index("      # Installed BEFORE")
    end = text.index("      - name: Get the code")
    text = text[:start] + text[end:]
    at = text.index("      - name: Find the story key")
    return text[:at] + "      - name: Install skj\n        run: %s\n\n" % cmd + text[at:]


def _all_tags(name, text):
    return (set(template_violations(name, text)) | set(checkout_violations(name, text)) | {t for _, t in expression_violations(text)} | ({"pin"} if pin_violations(text) else set())
            | ({"continue-on-error"} if continue_violations(text) else set()))


@pytest.mark.parametrize("label", sorted(MUTANTS))
def test_each_safety_property_is_enforced_by_a_mutant(label):
    name, mutate, tag = MUTANTS[label]
    assert tag in _all_tags(name, mutate(_text(name))), label
    assert tag not in _all_tags(name, _text(name))


# ---- T4: this repository's own CI (tests.yml): tests on 3.9/3.13, the wheel's data files, the scrub, actionlint ---------------------

OWN_JOBS = {"tests", "scrub", "actionlint"}
RUNNER = "ubuntu-24.04"  # Python 3.9 availability: newer images have no 3.9 build for actions/setup-python
WHEEL_DATA = ("skj/seeds/*.json", "skj/golden/*.json", "skj/templates/*.md")  # == pyproject's package-data (test below)
ACTIONLINT_VERSION = "1.7.7"
LINT_LINE = '"$RUNNER_TEMP/actionlint" .github/workflows/*.yml ci/templates/*.yml'


def _wheel_step(wf):
    steps = (wf.get("jobs") or {}).get("tests", {}).get("steps") or []
    found = [s for s in steps if "pip wheel" in str(s.get("run", ""))]
    return found[0] if len(found) == 1 else {}


def _wheel_check(run):
    """The python the wheel step feeds through its <<'PY' heredoc ("" when there is none)."""
    m = re.search(r"<<'PY'\n(.*?)\nPY(?:\n|$)", run, re.S)
    return m.group(1) if m else ""


def own_ci_violations(text):
    """Every way tests.yml breaks the T4 rules (tags; [] when it keeps them)."""
    bad = []
    wf = yaml.safe_load(text)
    on = _on(wf)
    if not (isinstance(on, dict) and set(on) == {"push", "pull_request"}) or "pull_request_target" in text:
        bad.append("triggers")
    jobs = wf.get("jobs") or {}
    if wf.get("permissions") != {"contents": "read"} or any("permissions" in j for j in jobs.values()):
        bad.append("permissions")
    if set(jobs) != OWN_JOBS:
        bad.append("jobs")
    if any(j.get("runs-on") != RUNNER for j in jobs.values()):
        bad.append("runs-on")
    tests = jobs.get("tests") or {}
    if ((tests.get("strategy") or {}).get("matrix") or {}).get("python") != ["3.9", "3.13"] \
            or (tests.get("strategy") or {}).get("fail-fast") is not False:  # Fix-CI2b: one red Python must not cancel the other
        bad.append("matrix")
    runs = [s["run"] for s in tests.get("steps") or [] if "run" in s]
    if runs[:2] != ['python -m pip install -e ".[dev]"', "python -m pytest -q -rs --durations=40"] or len(runs) != 3:
        bad.append("test steps")
    if tests.get("timeout-minutes") != 90:  # Fix-CI3: the diagnostic ceiling; lower it here and in tests.yml together
        bad.append("timeout")
    wrun = str(_wheel_step(wf).get("run", ""))
    check = _wheel_check(wrun)
    if not ('git archive HEAD | tar -x -C "$RUNNER_TEMP/clean"' in wrun
            and 'python -I -m pip wheel --no-deps -w "$RUNNER_TEMP/dist" "$RUNNER_TEMP/clean"' in wrun
            and 'python -I - "$RUNNER_TEMP/clean" "$RUNNER_TEMP"/dist/skj-*.whl <<\'PY\'' in wrun
            and "for pattern in %r:" % (WHEEL_DATA,) in check):
        bad.append("wheel")
    scrub = (jobs.get("scrub") or {}).get("steps") or []
    named = {s.get("name"): s for s in scrub}
    tree = named.get("Scan the files") or {}
    hist = named.get(OWN_SECRET_STEP[0]) or {}
    co = [s for s in scrub if str(s.get("uses", "")).startswith("actions/checkout@")]
    if not (len(scrub) == 3 and len(co) == 1 and (co[0].get("with") or {}).get("fetch-depth") == 0
            and tree.get("run") == SCRUB_TREE and "env" not in tree and tree.get("if") == "!(%s)" % MAIN_PUSH
            and (hist.get("name"), hist.get("run"), hist.get("if")) == OWN_SECRET_STEP
            and hist.get("env") == {"SCRUB_DENYLIST": "${{ secrets.SCRUB_DENYLIST }}"}):
        bad.append("scrub")
    code = re.sub(r"(?m)^\s*(?:-\s+)?name:.*$", "", re.sub(r"(?m)#.*$", "", text))  # a step or job name may say "secrets"
    if len(SECRET_REF.findall(code)) != 1 or re.search(r"TOKEN", code) or GITHUB_TOKEN_CONTEXT.search(code):
        bad.append("secret")  # the one secret is the denylist, on the history step (checked above); no token to PR code
    lint = (jobs.get("actionlint") or {}).get("steps") or []
    dl = [s for s in lint if "actionlint" in str(s.get("run", "")) and "curl" in str(s.get("run", ""))]
    d = str(dl[0].get("run", "")) if len(dl) == 1 else ""
    if not (len(dl) == 1 and dl[0].get("env") == {"VERSION": ACTIONLINT_VERSION}
            and 'base="https://github.com/rhysd/actionlint/releases/download/v$VERSION"' in d
            and 'sha256sum -c --ignore-missing "actionlint_${VERSION}_checksums.txt"' in d
            and d.index("sha256sum") < d.index("tar -xzf")
            and [s.get("run") for s in lint if s.get("run") == LINT_LINE] == [LINT_LINE]):
        bad.append("actionlint")
    checkouts = [s for j in jobs.values() for s in j.get("steps") or [] if str(s.get("uses", "")).startswith("actions/checkout@")]
    if len(checkouts) != len(OWN_JOBS) or any((s.get("with") or {}).get("persist-credentials") is not False for s in checkouts):
        bad.append("credentials")
    lines = text.splitlines()
    if not (all(re.match(r"# \S", ln) for ln in lines[:3]) and lines[3].startswith("name:")):
        bad.append("header")
    return bad


def test_the_kits_own_ci_keeps_every_rule():
    """T4-AC1..AC5: scrub job (tree on PRs, denylist + history on push to main, fetch-depth 0), actionlint 1.7.7 with a checksum, the
    wheel built from a clean copy carries its data files, every job on ubuntu-24.04, tests on 3.9 and 3.13, one secret only."""
    assert own_ci_violations(_text("tests.yml")) == []


CONTRACT_READS = {"tests/test_catalog.py": "catalog/**"}  # HD2-AC4: a data folder a contract test file reads must start the job too


def contract_trigger_gaps(text):
    """HD-AC3, HD2-AC4: each test file the contract job's pytest command names, and each folder in CONTRACT_READS such a file reads,
    that a pull_request (or push) `paths` filter leaves out."""
    wf = yaml.safe_load(text)
    runs = " ".join(s.get("run", "") for s in _steps(wf))
    files = sorted(set(re.findall(r"tests/[\w/]+\.py", runs.split("pytest", 1)[-1])))
    files += [CONTRACT_READS[f] for f in files if f in CONTRACT_READS]
    on = _on(wf)
    return [(event, f) for event in ("pull_request", "push") if isinstance(on.get(event), dict) and "paths" in on[event]
            for f in files if f not in on[event]["paths"]] + ([] if files else [("pytest", "no test file found")])


def test_every_contract_test_file_starts_the_contract_workflow():  # HD-AC3
    text = _text("speckit-contract.yml")
    assert contract_trigger_gaps(text) == []
    assert contract_trigger_gaps(text.replace("      - tests/test_catalog.py\n", "", 1)) == [("pull_request", "tests/test_catalog.py")]
    assert contract_trigger_gaps(text.replace("      - catalog/**\n", "", 1)) == [("pull_request", "catalog/**")]


# Fix-CI: the owner step before the public flip that no agent pass makes (the runner step was done in Fix-Final-CI)
FLIP_STEPS = ("move `SCRUB_DENYLIST` to an environment secret limited to `main`",)


def test_the_release_checklist_carries_the_ci_steps_left_for_the_flip():
    checklist = " ".join((ROOT / "docs/release-checklist.md").read_text(encoding="utf-8").split())
    assert all("- [ ] " + step[0].upper() + step[1:] in checklist for step in FLIP_STEPS), FLIP_STEPS
    assert "speckit-contract.yml` with" not in checklist  # the runner step is done: the contract workflow is on RUNNER (I4)


def test_the_wheel_data_list_is_pyprojects_package_data():
    m = re.search(r"\[tool\.setuptools\.package-data\]\nskj = \[(.*?)\]", (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert sorted("skj/" + p for p in re.findall(r'"([^"]+)"', m.group(1))) == sorted(WHEEL_DATA)


def _run_wheel_check(tmp_path, names, wheels=1):
    """Run the workflow's own check (its heredoc) with this tree as the clean copy and fake wheels holding `names`."""
    paths = []
    for i in range(wheels):
        whl = tmp_path / ("skj-0-%d-py3-none-any.whl" % i)
        with zipfile.ZipFile(str(whl), "w") as z:
            for n in names:
                z.writestr(n, "x")
        paths.append(str(whl))
    check = _wheel_check(_wheel_step(yaml.safe_load(_text("tests.yml"))).get("run", ""))
    assert check, "the wheel step has no <<'PY' check"
    return subprocess.run([sys.executable, "-I", "-", str(ROOT)] + paths, input=check, capture_output=True, text=True, timeout=60)


def _tree_data():
    src = ROOT / "src"
    return sorted(p.relative_to(src).as_posix() for pat in WHEEL_DATA for p in src.glob(pat))


def test_the_wheel_check_passes_a_wheel_with_every_data_file(tmp_path):
    names = _tree_data()
    assert any(n.startswith("skj/templates/") for n in names) and any(n.startswith("skj/golden/") for n in names)
    cp = _run_wheel_check(tmp_path, names + ["skj/__init__.py"])
    assert cp.returncode == 0, cp.stderr


@pytest.mark.parametrize("drop", ["skj/templates/", "skj/golden/", "skj/seeds/", "skj/seeds/salesforce.json"])
def test_the_wheel_check_fails_when_a_data_file_is_missing(tmp_path, drop):
    cp = _run_wheel_check(tmp_path, [n for n in _tree_data() if not n.startswith(drop)])
    assert cp.returncode != 0 and "the wheel lacks" in cp.stderr


@pytest.mark.parametrize("wheels", [0, 2])
def test_the_wheel_check_wants_exactly_one_wheel(tmp_path, wheels):
    assert _run_wheel_check(tmp_path, _tree_data(), wheels).returncode != 0


OWN_MUTANTS = {  # name -> (text change of tests.yml, the tag it must produce)
    "pull_request_target": (_swap("  pull_request:\n", "  pull_request_target:\n"), "triggers"),
    "write permission": (_swap("  contents: read\n", "  contents: write\n"), "permissions"),
    "actionlint job gone": (_swap("  actionlint:\n", "  lint:\n"), "jobs"),
    "tests on ubuntu-latest": (_swap("runs-on: ubuntu-24.04", "runs-on: ubuntu-latest"), "runs-on"),
    "python 3.9 dropped": (_swap('python: ["3.9", "3.13"]', 'python: ["3.13"]'), "matrix"),
    "pytest changed": (_swap("run: python -m pytest -q -rs", "run: python -m pytest -q -rs -x"), "test steps"),
    "skip reasons hidden": (_swap("run: python -m pytest -q -rs", "run: python -m pytest -q"), "test steps"),
    "slowest tests not named": (_swap(" --durations=40", ""), "test steps"),
    "timeout back to 40": (_swap("    timeout-minutes: 90\n", "    timeout-minutes: 40\n"), "timeout"),
    "fail-fast on": (_swap("      fail-fast: false", "      fail-fast: true"), "matrix"),
    "fail-fast left to its default": (_swap("      fail-fast: false   # a failure on one Python must not cancel the other\n", ""), "matrix"),
    "wheel from the working tree": (_swap('-w "$RUNNER_TEMP/dist" "$RUNNER_TEMP/clean"', '-w "$RUNNER_TEMP/dist" .'), "wheel"),
    "no clean copy": (_swap('git archive HEAD | tar -x -C "$RUNNER_TEMP/clean"', 'cp -R . "$RUNNER_TEMP/clean"'), "wheel"),
    "templates not checked": (_swap(", 'skj/templates/*.md')", ")"), "wheel"),
    "shallow history": (_swap("          fetch-depth: 0", "          fetch-depth: 1"), "scrub"),
    "history not scanned": (_swap("scrub_check.py --require-denylist --history", "scrub_check.py --require-denylist"), "scrub"),
    "denylist not required": (_swap("scrub_check.py --require-denylist --history", "scrub_check.py --history"), "scrub"),
    "history scan on every event": (_swap("        if: github.event_name == 'push' && github.ref == 'refs/heads/main'\n", ""), "scrub"),
    "denylist secret on the PR scan": (_swap("        run: python3 -I scripts/scrub_check.py\n",
                                             "        env:\n          SCRUB_DENYLIST: ${{ secrets.SCRUB_DENYLIST }}\n"
                                             "        run: python3 -I scripts/scrub_check.py\n"), "secret"),
    "a second secret": (_swap('          CI: ""\n', '          CI: ""\n          X: ${{ secrets.OTHER }}\n'), "secret"),
    "actionlint unpinned": (_swap('VERSION: "1.7.7"', 'VERSION: "latest"'), "actionlint"),
    "no checksum": (_swap('          sha256sum -c --ignore-missing "actionlint_${VERSION}_checksums.txt"\n', ""), "actionlint"),
    "templates not linted": (_swap(" ci/templates/*.yml'", "'"), "actionlint"),
    "credentials persisted": (_swap("persist-credentials: false", "persist-credentials: true"), "credentials"),
    "header gone": (_swap("# What this does", "#What this does"), "header"),
    # Fix-CI: every form of the secrets context and the run's own token, given to the step that runs the pull request's code
    "toJSON(secrets) to the tests": (_swap('          CI: ""\n', '          CI: ""\n          D: ${{ toJSON(secrets) }}\n'), "secret"),
    "secrets['X'] to the tests": (_swap('          CI: ""\n', '          CI: ""\n          D: ${{ secrets[\'SCRUB_DENYLIST\'] }}\n'), "secret"),
    'secrets["X"] to the tests': (_swap('          CI: ""\n', '          CI: ""\n          D: ${{ secrets["SCRUB_DENYLIST"] }}\n'), "secret"),
    "github.token to the tests": (_swap('          CI: ""\n', '          CI: ""\n          G: ${{ github.token }}\n'), "secret"),
    "event token to the tests": (_swap('          CI: ""\n', '          CI: ""\n          G: ${{ github.event.client_payload.token }}\n'), "secret"),
}


def _own_tags(text):
    return set(own_ci_violations(text)) | {"secret" for _ in checkout_violations("tests.yml", text)} | \
        {t for _, t in expression_violations(text)} | ({"pin"} if pin_violations(text) else set())


@pytest.mark.parametrize("label", sorted(OWN_MUTANTS))
def test_each_own_ci_rule_is_enforced_by_a_mutant(label):
    mutate, tag = OWN_MUTANTS[label]
    assert tag in _own_tags(mutate(_text("tests.yml"))), label
    assert _own_tags(_text("tests.yml")) == set()


def test_the_one_secret_step_exception_is_exact():
    """checkout_violations lets exactly OWN_SECRET_STEP hold a secret in tests.yml: the same step without its main-only `if`, or with
    another command, is a script from the checkout holding a secret."""
    ok = "jobs:\n  s:\n    steps:\n      - name: %s\n        if: %s\n        env:\n          SCRUB_DENYLIST: ${{ secrets.SCRUB_DENYLIST }}\n" \
         "        run: %s\n" % (OWN_SECRET_STEP[0], '"%s"' % MAIN_PUSH, SCRUB_HISTORY)
    assert checkout_violations("tests.yml", ok) == []
    assert checkout_violations("tests.yml", ok.replace(MAIN_PUSH, "always()"))
    assert checkout_violations("tests.yml", ok.replace(SCRUB_HISTORY, SCRUB_HISTORY + "; ./x.sh"))
    assert checkout_violations("speckit-contract.yml", ok)


def test_the_templates_are_not_live_workflows_of_this_repository():
    """Copy-in templates must not run here (they would gate the kit's own PRs on a config it does not have): they live in ci/templates/,
    and .github/workflows/ holds only the kit's own CI. The earlier technical names map to the friendly names in ci/README.md."""
    assert sorted(p.name for p in (ROOT / ".github/workflows").iterdir()) == sorted(OWN)
    assert sorted(p.name for p in (ROOT / "ci/templates").iterdir()) == sorted(TEMPLATES)
    readme = (ROOT / "ci/README.md").read_text(encoding="utf-8")
    for friendly, plan in TEMPLATES.items():
        assert re.search(r"\| `%s` \| `%s` \|" % (re.escape(friendly), plan), readme), friendly
    assert "```mermaid" in readme and "CODEOWNERS" in readme
    for word in ("Atlassian", "pull_request_target"):
        assert word not in "".join(_text(n) for n in TEMPLATES), word
