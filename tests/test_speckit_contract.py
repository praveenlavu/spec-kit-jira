"""F4 (X6): the contract between this kit and Spec Kit itself, and the CI workflow that runs it on the oldest and the newest Spec Kit.

Ported from the fork's tests/test_speckit_contract.py and adapted to Jira: it pins what the extension (`extension.yml`, 13 prompts) and the
workflow (`workflows/jira-lifecycle/workflow.yml`) rely on in the `specify` CLI and its Python API, so a Spec Kit release that renames or
changes one of them fails THIS file with a message that names the thing, not some far-off test of the kit.

Everything runs offline against the installed Spec Kit: the `specify` CLI (its own interpreter, found through the shebang of the script) as a
subprocess with a private HOME, a fixed PATH, no TTY and every proxy on a closed port. The helpers are the ones tests/test_manifest.py and
tests/test_workflow.py already use. Without `specify` a test skips, except under CI (then it fails: a CI leg without Spec Kit proves nothing).

F4-AC1 the suite passes on the installed Spec Kit (1.2.4.dev0 was the only one available when this was written).
F4-AC2 the minimum (1.2.1) and the weekly run are written down in .github/workflows/speckit-contract.yml and pinned by the YAML checks at
       the bottom of this file; offline only the installed version runs. The hosted job passed on 1.2.1 and the latest (2026-11-12).
"""
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from skj import approvals, doctor, kits
from test_manifest import COMMANDS, PREFIX, _isolated, _source_tree, _specify
from test_workflow import WORKFLOW, engine  # noqa: F401  (engine: fixture)
from test_catalog import REPO_URL

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "extension.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "speckit-contract.yml"
OWN_ID = "jira-lifecycle"

# ---- the Spec Kit API and paths this kit and its tests reach INTO (one place) -----------------------------------------------------------
# (module, attribute path, who reaches it, what for). Resolved in Spec Kit's own interpreter. Add a row whenever kit code or a test starts to
# use another part of Spec Kit; a row that stops resolving means a Spec Kit release renamed it.
PINNED_SYMBOLS = (
    ("specify_cli.extensions", "ExtensionManifest", "tests/test_manifest.py, this file", "loads extension.yml the way `extension add` does"),
    ("specify_cli.extensions", "ExtensionManifest.id", "tests/test_manifest.py, this file", "the extension id"),
    ("specify_cli.extensions", "ExtensionManifest.version", "tests/test_manifest.py, this file", "the extension version"),
    ("specify_cli.extensions", "ExtensionManifest.commands", "tests/test_manifest.py, this file", "the command list (name, file)"),
    ("specify_cli.extensions", "ExtensionManifest.hooks", "tests/test_manifest.py, this file", "the hooks (after_tasks, after_implement)"),
    ("specify_cli.extensions", "ExtensionManifest.requires_speckit_version", "this file", "the declared Spec Kit floor"),
    ("specify_cli.workflows.engine", "WorkflowDefinition.from_yaml", "this file", "the workflow loader"),
    ("specify_cli.workflows.engine", "validate_workflow", "this file", "the workflow validator `workflow add` runs"),
    ("specify_cli.workflows.engine", "WorkflowEngine._coerce_input", "this file (the number input of K4)", "the input type coercion"),
    ("specify_cli.workflows.engine", "RunState", "src/skj/approvals.py (read_run), tests/test_approvals.py", "writes the run's state.json"),
    ("specify_cli.workflows.base", "RunStatus", "src/skj/approvals.py (RUN_STATUSES)", "the run status names"),
    ("specify_cli.workflows._execution", "qualified_id", "src/skj/approvals.py (`<loop>:<step>:<n>`)", "the step id of a later loop pass"),
)
# CLI forms: (argv before --help, flags the help must list, who runs it)
PINNED_CLI = (
    (("extension", "add"), ("--dev",), "tests/test_manifest.py (install from a checkout)"),
    (("workflow", "add"), (), "tests/test_workflow.py"),
    (("workflow", "info"), (), "tests/test_workflow.py"),
    (("workflow", "run"), ("--input", "--json"), "tests/test_workflow.py, the runbook"),
    (("workflow", "resume"), ("--input",), "src/skj/approvals.py (resume_argv), the runbook"),
)
# Files and keys of an installed project the kit reads (`src/skj/kits.py`, `calibrate.py`, `approvals.py`)
STATE_KEYS = {"run_id", "workflow_id", "status", "current_step_id", "step_results", "inputs", "updated_at"}
REGISTRY_ENTRY_KEYS = {"version", "enabled", "registered_commands", "registered_skills"}

PROBE = r'''
import importlib, importlib.metadata as md, json, sys
from pathlib import Path
args = json.loads(sys.argv[1])
out = {"version": md.version("specify-cli"), "symbols": {}}
for mod, attr in args["symbols"]:
    key = mod + ":" + attr
    try:
        obj = importlib.import_module(mod)
        for part in attr.split("."):
            obj = getattr(obj, part)
        out["symbols"][key] = True
    except Exception as e:
        out["symbols"][key] = "%s: %s" % (type(e).__name__, e)
try:
    from specify_cli.extensions import ExtensionManifest
    m = ExtensionManifest(Path(args["manifest"]))
    out["manifest"] = {"id": m.id, "version": str(m.version), "warnings": list(m.warnings), "hooks": sorted(m.hooks),
                       "commands": [[c["name"], c["file"]] for c in m.commands], "requires": m.requires_speckit_version}
except Exception as e:
    out["manifest"] = "%s: %s" % (type(e).__name__, e)
try:
    from specify_cli.workflows.engine import WorkflowDefinition, WorkflowEngine, validate_workflow
    d = WorkflowDefinition.from_yaml(Path(args["workflow"]))
    out["workflow"] = {"id": d.id, "version": d.version, "inputs": d.inputs, "errors": validate_workflow(d), "steps": len(d.steps)}
    spec = d.inputs["issue_number"]
    coerced = []
    for value in args["values"]:
        try:
            coerced.append([repr(value), "ok", repr(WorkflowEngine._coerce_input("issue_number", value, spec))])
        except ValueError as e:
            coerced.append([repr(value), "refused", str(e)])
    out["coerced"] = coerced
    from specify_cli.workflows.base import RunStatus
    from specify_cli.workflows._execution import qualified_id
    out["statuses"] = [s.value for s in RunStatus]
    out["qualified"] = [qualified_id("approve-push"), qualified_id("approve-push", ("push-loop", 2))]
except Exception as e:
    out["workflow"] = "%s: %s" % (type(e).__name__, e)
print(json.dumps(out))
'''
VALUES = ["12", 12, 12.0, "1;touch x", "$(id)", "SPK-1", "12abc", "", True, None]


def _interpreter(exe):
    return open(exe, encoding="utf-8").readline()[2:].strip()  # the interpreter of the specify CLI (its own venv)


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    exe = _specify()
    tmp = tmp_path_factory.mktemp("probe")
    args = {"symbols": [[m, a] for m, a, _, _ in PINNED_SYMBOLS], "manifest": str(MANIFEST), "workflow": str(WORKFLOW), "values": VALUES}
    cp = subprocess.run([_interpreter(exe), "-I", "-c", PROBE, json.dumps(args)], capture_output=True, text=True, env=_isolated(tmp),
                        stdin=subprocess.DEVNULL, timeout=120)
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout)


def _sp(tmp_path, *args, cwd=None):
    return subprocess.run([_specify(), *args], cwd=str(cwd or tmp_path), env={**_isolated(tmp_path), "COLUMNS": "250"},
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)


def _numbers(version):
    """(major, minor, patch, final): `1.2.4.dev0` is (1, 2, 4, False): a pre-release sorts below its own release (PEP 440)."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)(.*)$", version)
    assert m, "cannot read a Spec Kit version from %r" % version
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) == ""


def _manifest():
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _floor():
    m = re.fullmatch(r">=(\d+)\.(\d+)\.(\d+)", _manifest()["requires"]["speckit_version"])
    assert m, "requires.speckit_version is no longer a plain `>=X.Y.Z` floor"
    return tuple(int(x) for x in m.groups())


# ---- the version: recorded, and at least the declared floor -----------------------------------------------------------------------------

def test_the_installed_speckit_version_is_recorded_and_read_by_the_kits_own_parser(probe, tmp_path, record_property):
    """The version of the Spec Kit this run is proven on: recorded in the report and the output (`-rP`), and the same number the CLI prints
    through `specify --version`, which `skj doctor`'s parser reads."""
    version = probe["version"]
    record_property("speckit_version", version)
    print("Spec Kit under test: %s" % version)
    cp = _sp(tmp_path, "--version")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    parsed = doctor._SPECIFY.search(cp.stdout)
    assert parsed, "`specify --version` no longer prints a line `skj doctor` can read: %r" % cp.stdout
    assert tuple(int(x) for x in parsed.groups()) == _numbers(version)[:3], (cp.stdout, version)


def _at_least(version, floor):
    found = _numbers(version)
    return found[:3] > floor or (found[:3] == floor and found[3])


def test_the_installed_speckit_is_at_least_the_declared_floor(probe):
    assert _at_least(probe["version"], _floor()), \
        "Spec Kit %s is below the declared floor >=%s: this suite proves nothing about it" % (probe["version"], ".".join(map(str, _floor())))


@pytest.mark.parametrize("version, ok", [("1.2.1", True), ("1.2.4.dev0", True), ("1.10.0", True), ("2.0.0rc1", True), ("1.2.0", False),
                                         ("1.2.1.dev0", False), ("1.2.1rc1", False), ("0.15.1", False)])
def test_the_version_comparison_treats_a_prerelease_of_the_floor_as_below_it(version, ok):
    assert _at_least(version, (1, 2, 1)) is ok


def test_the_floor_is_declared_the_same_everywhere():
    """The manifest's `speckit_version` is the one source; the workflow, `skj doctor` and the CI minimum say the same."""
    floor = _floor()
    text = ".".join(map(str, floor))
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert wf["requires"]["speckit_version"] == ">=" + text
    assert doctor.SPECKIT_FLOOR == floor
    assert yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["contract"]["strategy"]["matrix"]["speckit"][0] == text


# ---- the Python API and the CLI forms the kit reaches into -----------------------------------------------------------------------------

@pytest.mark.parametrize("mod, attr, who, why", PINNED_SYMBOLS, ids=["%s:%s" % (m, a) for m, a, _, _ in PINNED_SYMBOLS])
def test_a_speckit_symbol_the_kit_reaches_into_still_exists(probe, mod, attr, who, why):
    got = probe["symbols"]["%s:%s" % (mod, attr)]
    assert got is True, ("Spec Kit %s no longer has %s.%s (%s). Used by: %s. Update the kit or its declared floor; do not weaken this test. "
                         "Error: %s" % (probe["version"], mod, attr, why, who, got))


@pytest.mark.parametrize("argv, flags, who", PINNED_CLI, ids=[" ".join(a) for a, _, _ in PINNED_CLI])
def test_a_speckit_cli_form_the_kit_runs_still_exists(tmp_path, argv, flags, who):
    cp = _sp(tmp_path, *argv, "--help")
    out = cp.stdout + cp.stderr
    assert cp.returncode == 0, "`specify %s --help` failed (used by %s): %s" % (" ".join(argv), who, out)
    for flag in flags:
        assert flag in out, "`specify %s` no longer lists %s (used by %s)" % (" ".join(argv), flag, who)


# ---- the manifest ---------------------------------------------------------------------------------------------------------------------------

def test_the_manifest_loads_with_speckits_own_loader(probe):
    got = probe["manifest"]
    assert isinstance(got, dict), "ExtensionManifest refused extension.yml: %s" % got
    assert got["id"] == OWN_ID and got["warnings"] == [] and got["hooks"] == ["after_implement", "after_tasks"]
    assert sorted(n for n, _ in got["commands"]) == sorted(PREFIX + c for c in COMMANDS) and len(got["commands"]) == 13
    assert all((ROOT / f).is_file() for _, f in got["commands"])
    assert got["version"] == _manifest()["extension"]["version"]
    assert got["requires"] == ">=%s" % ".".join(map(str, _floor()))


def test_specify_extension_add_dev_installs_the_13_prompts_and_registers_the_hooks(tmp_path):
    src, proj = tmp_path / "src", tmp_path / "proj"
    src.mkdir()
    (proj / ".specify").mkdir(parents=True)
    _source_tree(src)
    cp = _sp(tmp_path, "extension", "add", "--dev", str(src), cwd=proj)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    inst = proj / ".specify" / "extensions" / OWN_ID
    assert (inst / "extension.yml").read_bytes() == MANIFEST.read_bytes()
    assert len(COMMANDS) == 13
    for name in COMMANDS:
        assert (inst / "commands" / ("%s.md" % name)).read_bytes() == (ROOT / "commands" / ("%s.md" % name)).read_bytes(), name
    # the registry `src/skj/kits.py` reads (the toolkit's own entry is left out of `kits.installed`, as designed)
    registry = json.loads((proj / ".specify" / "extensions" / ".registry").read_text(encoding="utf-8"))
    entry = registry["extensions"][OWN_ID]
    assert REGISTRY_ENTRY_KEYS <= set(entry), sorted(REGISTRY_ENTRY_KEYS - set(entry))
    assert entry["version"] == _manifest()["extension"]["version"] and entry["enabled"] is True
    assert isinstance(entry["registered_commands"], dict) and isinstance(entry["registered_skills"], list)
    assert kits.installed(str(proj)) == []
    # the two optional hooks of the manifest land in the project's hook list under the events the prompts talk about
    hooks = yaml.safe_load((proj / ".specify" / "extensions.yml").read_text(encoding="utf-8"))["hooks"]
    assert [(h["extension"], h["command"], h["optional"]) for h in hooks["after_tasks"]] == [(OWN_ID, PREFIX + "push", True)]
    assert [(h["extension"], h["command"], h["optional"]) for h in hooks["after_implement"]] == [(OWN_ID, PREFIX + "gate", True)]


# ---- the workflow ---------------------------------------------------------------------------------------------------------------------

def test_the_workflow_loads_and_validates_with_the_engines_loader(probe):
    got = probe["workflow"]
    assert isinstance(got, dict), "the engine refused to load the workflow: %s" % got
    assert got["errors"] == [] and got["id"] == OWN_ID == approvals.RUN_WORKFLOW and got["steps"] > 0
    assert got["inputs"]["issue_number"]["type"] == "number" and got["inputs"]["issue_number"]["required"] is True
    assert approvals.RUN_INPUT == "issue_number"


def test_the_engine_coerces_a_number_and_refuses_a_string(probe):
    """K4: the only value that reaches a shell is the issue number; the engine turns `12`, 12 and 12.0 into the int 12 and refuses every
    string that is not a number, a bool and a missing value, before any step runs."""
    got = {value: (out, text) for value, out, text in probe["coerced"]}
    for value in ("'12'", "12", "12.0"):
        assert got[value] == ("ok", "12"), (value, got[value])
    for value in ("'1;touch x'", "'$(id)'", "'SPK-1'", "'12abc'", "''", "True", "None"):
        assert got[value][0] == "refused", (value, got[value])


def test_the_run_state_the_poller_reads_is_what_the_real_engine_writes(engine):
    """`approvals.read_run` and `approvals.resume_argv` against a REAL paused run (the shipped review-spec gate, no TTY): the state file has the
    keys the poller reads, and the resume the poller builds completes the gate with the verdict as the choice."""
    cp = engine.sp("workflow", "run", engine.mini(["review-spec"]), "--input", "issue_number=12", "--json")
    out = json.loads(cp.stdout)
    assert cp.returncode == 0 and out["status"] == "paused" and out["gate"]["step_id"] == "review-spec", cp.stdout + cp.stderr
    rid = out["run_id"]
    state = engine.state(rid)
    assert STATE_KEYS <= set(state), sorted(STATE_KEYS - set(state))
    assert (state["workflow_id"], state["status"], state["current_step_id"], state["inputs"]["issue_number"]) == (
        "jira-lifecycle", "paused", "review-spec", 12)
    assert approvals.read_run(engine.dir, rid) == {"run_id": rid, "status": "paused", "step": "review-spec", "issue_number": 12}
    cp = engine.sp(*approvals.resume_argv(rid, "review-spec", "approve")[1:])
    state = engine.state(rid)
    assert cp.returncode == 0 and state["status"] == "completed", cp.stdout + cp.stderr
    assert state["step_results"]["review-spec"]["output"]["choice"] == "approve"
    assert approvals.read_run(engine.dir, rid)["status"] == "completed"


def test_the_status_names_and_the_loop_step_id_form_are_the_ones_the_kit_parses(probe):
    assert probe["statuses"] == list(approvals.RUN_STATUSES)
    plain, later = probe["qualified"]
    assert plain == "approve-push" and later == "push-loop:approve-push:2"
    assert approvals._step_ok(plain) and approvals._step_ok(later)


# ---- the CI workflow (.github/workflows/speckit-contract.yml): F4-AC2 -------------------------------------------------------------------

# The ONE list of action pins (T1): action -> (40-hex commit, the version comment its `uses:` line carries). Every workflow and template
# writes `<action>@<commit> # <version>`. The commits are the ones the fork source's CI uses; the hosted runs used them, but that each matches
# its version tag on GitHub is UNVERIFIED (U14). No placeholder pin is allowed (the old `<PIN-SHA-BEFORE-RELEASE>` is gone).
ACTION_PINS = {"actions/checkout": ("11bd71901bbe5b1630ceea73d27597364c9af683", "v4.2.2"),
               "actions/setup-python": ("a26af69be951a213d495a4c3e4e4022e16d87065", "v5.6.0")}
OLD_PIN_PLACEHOLDER = "PIN-SHA"
# The ci/templates/ install line, `git+<repository>@<ref>` (tests/test_github_workflows.py). REL1 filled the repository; the ref stays
# `<COMMIT-SHA>` until scripts/pin_release_sha.py writes the release commit. Exactly those two states pass: never a tag, branch or short sha.
INSTALL_PLACEHOLDERS = (REPO_URL, "<COMMIT-SHA>")
INSTALL_REF = re.compile(r"<COMMIT-SHA>|[0-9a-f]{40}")


def install_ref(text):
    """The ref of the one skj install line in `text` when it is `<COMMIT-SHA>` or a full 40-hex commit; None otherwise."""
    refs = re.findall(r'"git\+%s@([^"]*)"' % re.escape(REPO_URL), text)
    return refs[0] if len(refs) == 1 and INSTALL_REF.fullmatch(refs[0]) else None


@pytest.mark.parametrize("ref, ok", [("<COMMIT-SHA>", True), ("0123456789abcdef0123456789abcdef01234567", True), ("v0.1.0", False),
                                     ("main", False), ("<TAG>", False), ("0123456", False), ("0123456789ABCDEF0123456789ABCDEF01234567", False),
                                     ("0123456789abcdef0123456789abcdef012345678", False), ("0123456789abcdef0123456789abcdef0123456", False)])
def test_rel1_the_install_ref_is_the_placeholder_or_a_full_commit(ref, ok):
    line = 'python -I -m pip install "git+%s@%s"' % (REPO_URL, ref)
    assert (install_ref(line) == ref) is ok
    assert install_ref(line + "\n" + line) is None  # one install line only
PR_PATHS = ["extension.yml", "commands/**", "workflows/**", "tests/test_speckit_contract.py", "tests/test_workflow.py",
            "tests/test_manifest.py", "tests/test_catalog.py", "catalog/**"]  # HD-AC3: every test file the job runs; HD2-AC4: the data it reads
PYTEST_COMMAND = "python -m pytest tests/test_speckit_contract.py tests/test_workflow.py tests/test_manifest.py tests/test_catalog.py -q"
INSTALL_LINE = 'uv tool install specify-cli --from "git+https://github.com/github/spec-kit.git@${SPECKIT_REF}"'
SHA = re.compile(r"[0-9a-f]{40}")
RUNNER = "ubuntu-24.04"  # as tests.yml (tests/test_github_workflows.py RUNNER): newer images have no Python 3.9 for setup-python


def _steps(wf):
    return [s for job in wf["jobs"].values() for s in job["steps"]]


def ci_violations(text):
    """Every way the workflow text breaks the rules (tags; [] when it keeps them). Parsed with pyyaml, so `on:` is the key True."""
    bad = []
    wf = yaml.safe_load(text)
    on = wf.get("on", wf.get(True))
    if not isinstance(on, dict) or set(on) != {"schedule", "workflow_dispatch", "pull_request"}:
        bad.append("triggers")
        on = on if isinstance(on, dict) else {}
    crons = [s.get("cron") for s in on.get("schedule") or [] if isinstance(s, dict)]
    if not (len(crons) == 1 and re.fullmatch(r"\d{1,2} \d{1,2} \* \* [0-6]", str(crons[0]))):
        bad.append("weekly cron")
    pr = on.get("pull_request")
    if not (isinstance(pr, dict) and pr.get("paths") == PR_PATHS):
        bad.append("pull_request paths")
    if "pull_request_target" in text:
        bad.append("pull_request_target")
    if wf.get("permissions") != {"contents": "read"}:
        bad.append("permissions")
    jobs = wf.get("jobs") or {}
    if any("permissions" in j for j in jobs.values()):
        bad.append("job permissions")
    if not jobs or any(j.get("runs-on") != RUNNER for j in jobs.values()):  # the same image as tests.yml: it still has Python 3.9
        bad.append("runs-on")
    conc = wf.get("concurrency")
    if not (isinstance(conc, dict) and conc.get("group") and conc.get("cancel-in-progress") is True):
        bad.append("concurrency")
    matrix = ((jobs.get("contract") or {}).get("strategy") or {}).get("matrix") or {}
    if matrix.get("speckit") != ["1.2.1", "latest"]:
        bad.append("matrix speckit")
    if matrix.get("python") != ["3.9", "3.13"]:
        bad.append("matrix python")
    refs = {i.get("speckit"): i.get("ref") for i in matrix.get("include") or []}
    if not (SHA.fullmatch(str(refs.get("1.2.1"))) and refs.get("latest") == ""):
        bad.append("matrix refs")
    steps = _steps(wf) if jobs else []
    if not all(isinstance(s.get("name"), str) and s["name"].strip() for s in steps):
        bad.append("unnamed step")
    runs = [s["run"] for s in steps if "run" in s]
    if any("github.event" in r for r in runs):
        bad.append("github.event in run")
    if any("${{" in r for r in runs):
        bad.append("expression in run")
    if not any(PYTEST_COMMAND in [ln.strip() for ln in r.splitlines()] for r in runs):
        bad.append("pytest command")
    if not any(INSTALL_LINE in [ln.strip() for ln in r.splitlines()] for r in runs):
        bad.append("install line")
    uses = [s["uses"] for s in steps if "uses" in s]
    if any(not SHA.fullmatch(u.rpartition("@")[2]) for u in uses):
        bad.append("unpinned action")
    lines = re.findall(r"^\s*-?\s*uses:\s*(\S+@\S+)[ \t]*(?:#[ \t]*(\S+))?[ \t]*$", text, re.M)
    pins = [(u.rpartition("@")[0], (u.rpartition("@")[2], c)) for u, c in lines]
    if dict(pins) != ACTION_PINS or len(pins) != len(ACTION_PINS):
        bad.append("pin list")
    code = re.sub(r"(?m)#.*$", "", text)  # the header says "no secrets": comments are not searched
    if re.search(r"secrets|GITHUB_TOKEN", code) or any("env" in s and re.search(r"TOKEN|SECRET|PASSWORD", json.dumps(s["env"])) for s in steps):
        bad.append("secret")
    head = text.splitlines()[:4]
    if not (all(re.match(r"# \S", ln) for ln in head[:3]) and head[3].startswith("name:")):
        bad.append("header")
    return bad


def _ci():
    return CI_WORKFLOW.read_text(encoding="utf-8")


def test_the_ci_workflow_keeps_every_rule():
    """The real file: triggers (weekly cron, dispatch, pull_request on the paths, every test file the job runs among them), the 2x2 matrix, read-only permissions, concurrency, no
    pull_request_target, no `${{` in a `run:` (so no `github.event`), the pytest command, the Spec Kit install line, pins, no secrets."""
    assert ci_violations(_ci()) == []


def test_the_ci_workflow_matrix_is_the_minimum_and_the_latest_on_python_3_9_and_3_13():
    wf = yaml.safe_load(_ci())
    job = wf["jobs"]["contract"]
    assert job["strategy"]["matrix"]["speckit"] == ["1.2.1", "latest"] and job["strategy"]["matrix"]["python"] == ["3.9", "3.13"]
    assert job["strategy"]["fail-fast"] is False  # a red minimum must not hide the latest, and the other way round
    (setup,) = [s for s in _steps(wf) if str(s.get("uses", "")).startswith("actions/setup-python@")]
    assert setup["with"]["python-version"] == "${{ matrix.python }}"
    assert "refs/tags" in _ci()  # the latest is resolved from the newest release tag at run time


def test_every_action_pin_is_the_one_listed_commit():
    """T1-AC2: each `uses:` is `<action>@<the commit in ACTION_PINS> # <its version>`; the workflow uses each listed action once."""
    found = re.findall(r"uses:\s*(\S+)@([0-9a-f]{40}) # (v\d+\.\d+\.\d+)\n", _ci())
    assert {a: (s, v) for a, s, v in found} == ACTION_PINS and len(found) == len(ACTION_PINS)


def test_no_placeholder_pin_is_left_in_any_workflow_or_template():
    """T1-AC1: `grep -r 'PIN-SHA' .github ci` finds nothing (docs included: ci/README.md lives under ci/)."""
    hits = [str(p) for d in (".github", "ci") for p in sorted((ROOT / d).rglob("*")) if p.is_file()
            and OLD_PIN_PLACEHOLDER in p.read_text(encoding="utf-8")]
    assert hits == []


def _swap(old, new):
    def go(text):
        assert old in text, "mutant does not apply (the workflow text changed): %r" % old
        return text.replace(old, new, 1)
    return go


MUTANTS = {  # name -> (text transformation, the violation tag it must produce)
    "no manual trigger": (_swap("  workflow_dispatch:\n", ""), "triggers"),
    "daily, not weekly": (_swap('cron: "17 5 * * 1"', 'cron: "17 5 * * *"'), "weekly cron"),
    "a path dropped": (_swap("      - commands/**\n", ""), "pull_request paths"),
    "pull_request_target": (_swap("  pull_request:\n", "  pull_request_target:\n"), "pull_request_target"),
    "write permission": (_swap("  contents: read\n", "  contents: write\n"), "permissions"),
    "job permission": (_swap("    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-24.04\n    permissions:\n      contents: read\n"),
                       "job permissions"),
    "contract on ubuntu-latest": (_swap("    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-latest\n"), "runs-on"),
    "no concurrency": (_swap("  cancel-in-progress: true\n", ""), "concurrency"),
    "minimum dropped": (_swap('speckit: ["1.2.1", "latest"]', 'speckit: ["latest"]'), "matrix speckit"),
    "latest dropped": (_swap('speckit: ["1.2.1", "latest"]', 'speckit: ["1.2.1"]'), "matrix speckit"),
    "python 3.9 dropped": (_swap('python: ["3.9", "3.13"]', 'python: ["3.13"]'), "matrix python"),
    "python unquoted": (_swap('python: ["3.9", "3.13"]', "python: [3.9, 3.13]"), "matrix python"),
    "minimum not a commit": (_swap("ref: 9dca9bf42a031765ee5e5f2371137f2f26967e11", "ref: v1.2.1"), "matrix refs"),
    "step without a name": (_swap("      - name: Show the Spec Kit version\n        run:", "      - run:"), "unnamed step"),
    "github.event in a run": (_swap("run: specify --version", "run: echo ${{ github.event.pull_request.title }}"), "github.event in run"),
    "any expression in a run": (_swap("run: specify --version", "run: echo ${{ matrix.python }}"), "expression in run"),
    "pytest command changed": (_swap(PYTEST_COMMAND, PYTEST_COMMAND.replace(" -q", " -x")), "pytest command"),
    "a test file left out": (_swap("py tests/test_manifest.py", "py"), "pytest command"),
    "the catalog test left out": (_swap("py tests/test_catalog.py", "py"), "pytest command"),  # Fix-CI3: its bundle check runs in no other job
    "install line changed": (_swap("uv tool install specify-cli --from", "uv tool install specify-cli@latest --from"), "install line"),
    "action on a tag": (_swap("actions/setup-python@" + ACTION_PINS["actions/setup-python"][0], "actions/setup-python@v5"), "unpinned action"),
    "action on the old placeholder": (_swap("actions/setup-python@" + ACTION_PINS["actions/setup-python"][0],
                                            "actions/setup-python@<PIN-SHA-BEFORE-RELEASE>"), "unpinned action"),
    "unlisted action": (_swap("actions/checkout@", "actions/upload-artifact@"), "pin list"),
    "another commit": (_swap(ACTION_PINS["actions/checkout"][0], "b" * 40), "pin list"),
    "wrong version comment": (_swap(" # v4.2.2", " # v4"), "pin list"),
    "a secret": (_swap("run: specify --version", "run: echo ${{ secrets.SKJ_TOKEN }}"), "secret"),
    "header gone": (_swap("# Every week", "#Every week"), "header"),
}


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_each_ci_rule_is_enforced_by_a_mutant(name):
    """The mutation check, kept: the real text breaks no rule, and each mutant of it breaks (at least) the rule it names."""
    mutate, tag = MUTANTS[name]
    assert tag in ci_violations(mutate(_ci())), name
    assert tag not in ci_violations(_ci())


def test_every_rule_tag_has_a_mutant():
    tags = {"triggers", "weekly cron", "pull_request paths", "pull_request_target", "permissions", "job permissions", "concurrency",
            "runs-on", "matrix speckit", "matrix python", "matrix refs", "unnamed step", "github.event in run", "expression in run", "pytest command",
            "install line", "unpinned action", "pin list", "secret", "header"}
    assert {t for _, t in MUTANTS.values()} == tags


def test_the_ci_workflow_says_in_plain_words_what_it_does_and_why():
    head = _ci().splitlines()[:3]
    assert head[0].startswith("# Every week") and "Spec Kit" in head[0] and "no secrets" in head[2].lower()
