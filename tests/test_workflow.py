"""F1: the `jira-lifecycle` Spec Kit workflow (workflows/jira-lifecycle/workflow.yml), ported from the fork's lifecycle workflow.

Safety argument (K4): a `shell` step of the installed Spec Kit engine substitutes `{{ ... }}` into the command TEXT and runs it with
`shell=True` and the whole process environment, so the only value that may ever reach a `run:` is `inputs.issue_number`, an input of
type `number` that the engine coerces to an int or float (`workflow run` refuses anything else before the first step, and so does a
resume that passes any `--input`; a PLAIN resume does not re-check the inputs saved in state.json: see
`test_a_resume_re_checks_the_number_only_when_it_passes_inputs`). The CLI builds
`<project>-<n>` from it and refuses 0, negatives and fractions (exit 2, no request).

F1-AC1 static: `test_only_issue_number_interpolated` (+ the checker's own tests on synthetic bad workflows).
F1-AC2 the REAL engine (the `specify` CLI, its own interpreter, run as a subprocess with a private HOME, no TTY, a fixed PATH and an
       unreachable proxy): `test_engine_rejects_injection_values`.
F1-AC3 `test_number_becomes_project_key`, `test_cli_refuses_non_positive_or_fractional_number`.
F1-AC4 `test_rejected_gate_aborts_without_write` (the real engine; `skj` on PATH is the real CLI in-process against the fake Jira).
"""
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import ambient

import skj
import skj.__main__ as m
from skj import approvals, keys, strict

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / "workflows" / "jira-lifecycle" / "workflow.yml"
ISSUE = "{{ inputs.issue_number }}"
PROJECT = "SPK"

# The extension commands the workflow runs (F2 ships them); each name is one the product already prints (status, dod, strict, prs).
EXT_COMMANDS = ("pull", "plan", "tasks", "implement", "gate", "review")
CHECKED = ("review-spec", "wait-po-answers", "review-plan", "review-review", "wait-po-validation")
LOOPS = {"questions": "approve-questions", "plan-questions": "approve-plan-questions", "answers-questions": "approve-answers-questions",
         "push": "approve-push", "decisions": "approve-decisions", "sync": "approve-sync", "pr": "approve-pr"}


def _wf(path=WORKFLOW):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _flat(steps):
    """Every step in run order, loop bodies right after their loop step."""
    for s in steps:
        yield s
        yield from _flat(s.get("steps", []))


def _steps(wf=None):
    return list(_flat((wf or _wf())["steps"]))


def _by_id(wf=None):
    return {s["id"]: s for s in _steps(wf)}


# ---- F1-AC1: the template checker -----------------------------------------------------------------------------------------------------

def _strings(value, path):
    """(path, string) for every string under `value` (dict keys and list items included)."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k, path + ".<key>")
            yield from _strings(v, "%s.%s" % (path, k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _strings(v, "%s[%d]" % (path, i))


def _templated(s):
    return any(t in s for t in ("{{", "}}", "{%", "%}", "${{"))


_GLUED = re.compile(r"[{$]" + re.escape(ISSUE) + "|" + re.escape(ISSUE) + r"\}")


def _only_issue(s):
    """True when the only template in `s` is the exact `{{ inputs.issue_number }}`: no filter, no GitHub-style `${{`, and no brace glued
    to it (`{{{ inputs.issue_number }}` and `{{ inputs.issue_number }}}` render with the number missing or a stray brace). Braces
    elsewhere are shell syntax (`${#args[@]}`, `{ ...; }`) and stay allowed."""
    return "${{" not in s and not _GLUED.search(s) and not _templated(s.replace(ISSUE, ""))


STEP_KEYS = {"shell": {"id", "type", "run", "timeout"}, "command": {"id", "command", "integration", "model", "input"},
             "gate": {"id", "type", "verdict_input", "message", "show_file", "options", "on_reject"},
             "do-while": {"id", "type", "max_iterations", "condition", "steps"}}


def template_violations(wf):
    """Every place a template could reach a shell or carry another value: [] for a safe workflow.

    Only four step types are allowed (any other type, e.g. if/switch/fan-out/prompt/workflow, is a violation, so no unchecked field can
    hold a command), each with a fixed key set. A shell `run` and a command's `input.args` may hold only `{{ inputs.issue_number }}`; a
    command's `integration` and `model` are exactly their own input (the engine hands them to the integration, never to a shell); a
    do-while `condition` is exactly the plan-changed test of its own apply step (the engine's expression evaluator, never a shell).
    Every other string of the document (gate messages, prompts, ids, metadata) holds no template at all."""
    bad = []
    for path, s in _strings({k: v for k, v in wf.items() if k != "steps"}, "$"):
        if _templated(s):
            bad.append((path, s))

    def walk(steps, where):
        if not isinstance(steps, list):
            bad.append((where, "steps is not a list"))
            return
        for i, step in enumerate(steps):
            here = "%s[%d]" % (where, i)
            if not isinstance(step, dict):
                bad.append((here, "not a mapping"))
                continue
            kind = step.get("type", "command")
            if kind not in STEP_KEYS:
                bad.append((here, "step type %r is not allowed" % kind))
                continue
            for k in set(step) - STEP_KEYS[kind]:
                bad.append((here, "key %r is not allowed in a %s step" % (k, kind)))
            for path, s in _strings({k: v for k, v in step.items() if k != "steps"}, here):
                field = path[len(here) + 1:]
                if kind == "shell" and field == "run":
                    ok = _only_issue(s)
                elif kind == "command" and field == "input.args":
                    ok = _only_issue(s)
                elif kind == "command" and field == "integration":
                    ok = s == "{{ inputs.integration }}"
                elif kind == "command" and field == "model":
                    ok = s == "{{ inputs.review_model }}"
                elif kind == "do-while" and field == "condition":
                    body = step.get("steps") if isinstance(step.get("steps"), list) else []
                    apply_id = body[-1].get("id") if body and isinstance(body[-1], dict) else None
                    ok = s == "{{ steps.%s.output.stderr | contains('plan changed since approval') }}" % apply_id
                else:
                    ok = not _templated(s)
                if not ok:
                    bad.append((path, s))
            if kind == "command" and not isinstance(step.get("input", {}), dict):
                bad.append((here, "input is not a mapping"))
            if kind == "command" and isinstance(step.get("input"), dict) and set(step["input"]) - {"args"}:
                bad.append((here, "command input holds more than args"))
            if kind == "shell" and not isinstance(step.get("run"), str):
                bad.append((here, "run is not a string"))
            if kind == "do-while":
                walk(step.get("steps"), here + ".steps")
            elif "steps" in step:
                bad.append((here, "nested steps outside a do-while"))

    walk(wf.get("steps"), "$.steps")
    return bad


def test_only_issue_number_interpolated():
    """F1-AC1: the shipped workflow has no template anywhere but the allowed places, and every `run:` holds only the issue number."""
    wf = _wf()
    assert template_violations(wf) == []
    runs = [s["run"] for s in _steps(wf) if s.get("type") == "shell"]
    assert len(runs) == 39
    for run in runs:
        assert all(e.strip() == "inputs.issue_number" for e in re.findall(r"\{\{(.*?)\}\}", run, re.S)), run
        assert "verdict" not in run and "review_model" not in run and "integration" not in run


def _synthetic(mutate):
    wf = copy.deepcopy(_wf())
    mutate(wf)
    return template_violations(wf)


def _set(sid, key, value):
    def go(wf):
        _by_id(wf)[sid][key] = value
    return go


@pytest.mark.parametrize("name,mutate", [
    ("another input in a run", _set("stack-check", "run", "skj stack show --check {{ inputs.integration }}")),
    ("a verdict in a run", _set("check-review-spec", "run", "skj approvals check {{ inputs.verdict_review_spec }} --gate review-spec")),
    ("a step output in a run", _set("push-applied", "run", "echo {{ steps.push-apply.output.stderr }}")),
    ("an env value in a run", _set("strict-verify", "run", "skj strict verify {{ env.X }}")),
    ("a filter on the issue number", _set("strict-check-implement", "run", "skj strict check {{ inputs.issue_number | default('1;id') }}")),
    ("GitHub-style interpolation", _set("strict-check-implement", "run", "skj strict check ${{ inputs.issue_number }} --phase implement")),
    ("a template split over lines", _set("strict-check-implement", "run", "skj strict check {{\n inputs.review_model }}")),
    ("an unterminated template", _set("strict-check-implement", "run", "skj strict check {{ inputs.issue_number")),
    ("a template in a command list", _set("stack-check", "command", ["sh", "-c", "{{ inputs.integration }}"])),
    ("a template in an args list", _set("stack-check", "args", ["{{ inputs.review_model }}"])),
    ("a nested run in a loop body", _set("push-preview", "run", "skj push {{ inputs.review_model }} --plan-out .skj/plan-push.md")),
    ("a triple opening brace", _set("strict-check-implement", "run", "skj strict check {{{ inputs.issue_number }} --phase implement")),
    ("a triple closing brace", _set("strict-check-implement", "run", "skj strict check {{ inputs.issue_number }}} --phase implement")),
    ("a run that is not a string", _set("stack-check", "run", ["skj", "{{ inputs.integration }}"])),
    ("another input in command args", lambda wf: _by_id(wf)["pull"]["input"].update(args="{{ inputs.review_model }} --workflow")),
    ("a template in a gate message", _set("review-spec", "message", "Review {{ inputs.integration }}")),
    ("a template in show_file", _set("approve-push", "show_file", ".skj/{{ inputs.integration }}.md")),
    ("a model from another input", _set("review", "model", "{{ inputs.integration }}")),
    ("a condition on another step", _set("push-loop", "condition", "{{ steps.pull.output.stderr | contains('x') }}")),
    ("a template in an input prompt", lambda wf: wf["inputs"]["integration"].update(prompt="{{ inputs.review_model }}")),
    ("an if step with a run inside", lambda wf: wf["steps"].append(
        {"id": "x", "type": "if", "condition": "true", "then": [{"id": "y", "type": "shell", "run": "echo {{ inputs.integration }}"}]})),
    ("a prompt step", lambda wf: wf["steps"].append({"id": "x", "type": "prompt", "prompt": "hello"})),
    ("an env key on a shell step", _set("stack-check", "env", {"A": "{{ inputs.integration }}"})),
    ("steps under a shell step", _set("stack-check", "steps", [{"id": "z", "type": "shell", "run": "echo {{ inputs.integration }}"}])),
])
def test_the_checker_finds_every_synthetic_bad_workflow(name, mutate):
    assert _synthetic(mutate), name


def test_the_checker_accepts_the_issue_number_alone():
    assert _synthetic(_set("stack-check", "run", "skj stack show --check")) == []
    assert _synthetic(_set("strict-check-implement", "run", "skj strict check {{ inputs.issue_number }} --phase implement")) == []


# ---- the step graph -------------------------------------------------------------------------------------------------------------------

def test_metadata_follows_the_names_and_the_installed_engine_schema():
    wf = _wf()
    assert wf["schema_version"] == "1.0"
    meta = wf["workflow"]
    assert meta["id"] == "jira-lifecycle" and meta["name"] == "Spec Kit lifecycle for Jira"
    assert meta["version"] == re.match(r"\d+\.\d+\.\d+", skj.__version__).group(0)  # the engine accepts X.Y.Z only
    assert wf["requires"]["speckit_version"] == ">=1.2.1" and wf["requires"]["integrations"]["any"]
    assert approvals.RUN_WORKFLOW == meta["id"] and approvals.RUN_INPUT == "issue_number"


def test_issue_number_is_a_required_number_without_default():
    spec = _wf()["inputs"]["issue_number"]
    assert spec["type"] == "number" and spec["required"] is True and "default" not in spec and "enum" not in spec


def test_inputs_are_the_issue_number_integration_model_and_one_verdict_per_gate():
    inputs = _wf()["inputs"]
    assert set(inputs) == {"issue_number", "integration", "review_model"} | {approvals._input(g) for g in approvals.GATES}
    assert inputs["integration"]["type"] == "string" and inputs["integration"]["default"] == "auto"
    spec = inputs["review_model"]
    assert spec["type"] == "string" and spec["default"] == "" and not spec.get("required")


def test_step_order_and_ids():
    ids = [s["id"] for s in _steps()]
    loop = lambda n: ["%s-loop" % n, "%s-preview" % n, LOOPS[n], "%s-apply" % n, "%s-applied" % n]  # noqa: E731
    assert ids == (["pull", "pull-done", "stack-check", "review-spec", "check-review-spec"] + loop("questions") + ["plan", "plan-done"]
                   + loop("plan-questions") + ["wait-po-answers", "check-wait-po-answers", "sync-po-answers", "plan-with-answers",
                                               "plan-with-answers-done"] + loop("answers-questions")
                   + ["review-plan", "check-review-plan", "tasks", "tasks-done"] + loop("push")
                   + ["strict-check-implement", "implement", "implement-done"] + loop("decisions") + ["strict-verify"] + loop("sync")
                   + ["gate", "gate-done", "gate-check", "review", "review-done", "review-review", "check-review-review"]
                   + loop("pr") + ["wait-po-validation", "sync-po-validation", "check-wait-po-validation"])


def test_every_gate_has_its_verdict_input_and_aborts_on_reject():
    gates = [s for s in _steps() if s.get("type") == "gate"]
    assert sorted(g["id"] for g in gates) == sorted(approvals.GATES) and len(gates) == 12
    inputs = _wf()["inputs"]
    for g in gates:
        assert g["verdict_input"] == approvals._input(g["id"])
        assert g["options"] == ["approve", "reject"] and g["on_reject"] == "abort" and "{{" not in g["message"]
        spec = inputs[g["verdict_input"]]
        assert spec["type"] == "string" and spec["enum"] == ["", "approve", "reject"] and spec["default"] == "" and not spec.get("required")


def _cmd(run):
    return shlex.split(run.split(" || ")[0])


@pytest.mark.parametrize("name", LOOPS)
def test_each_write_is_a_one_pass_preview_gate_apply_triplet(name):
    top = {s["id"]: s for s in _wf()["steps"]}
    loop, gate_id = top["%s-loop" % name], LOOPS[name]
    plan = approvals.APPLY_FILES[gate_id]
    assert loop["type"] == "do-while" and loop["max_iterations"] == 1  # one pass: a supplied verdict must not re-approve a changed plan
    assert [s["id"] for s in loop["steps"]] == ["%s-preview" % name, gate_id, "%s-apply" % name]
    preview, gate, apply = loop["steps"]
    assert gate["show_file"] == plan and preview["type"] == apply["type"] == "shell"
    assert _cmd(preview["run"])[-2:] == ["--plan-out", plan] and "--apply" not in preview["run"]
    assert _cmd(apply["run"])[-3:] == ["--apply", "--expect-plan", plan]
    assert _cmd(preview["run"])[:-2] == _cmd(apply["run"])[:-3]  # the same verb and flags, so the apply re-renders the same plan
    assert apply["run"].endswith(" || { [ $? -eq 2 ] && [ -e %s.new ]; }" % plan)
    applied = top["%s-applied" % name]
    assert applied["run"] == ("if [ -e %s.new ]; then echo 'skj: the %s plan changed after approval; start a new run' >&2; exit 1; fi"
                              % (plan, name.replace("-", " ")))


def test_every_apply_is_guarded_by_its_gate_and_expect_plan():
    steps = _steps()
    applies = [i for i, s in enumerate(steps) if s.get("type") == "shell" and "--apply" in s["run"]]
    assert [steps[i]["id"] for i in applies] == ["%s-apply" % n for n in LOOPS]
    for i in applies:
        assert "--expect-plan" in steps[i]["run"] and steps[i - 1]["type"] == "gate" and steps[i - 1]["on_reject"] == "abort"


def _sh(run, cwd, skj_body, env=None):
    bindir = Path(cwd) / "bin"
    bindir.mkdir(exist_ok=True)
    if skj_body is not None:  # None: keep the `skj` already there
        (bindir / "skj").write_text("#!/bin/sh\n" + skj_body, encoding="utf-8")
    (bindir / "skj").chmod(0o755)
    return subprocess.run(["sh", "-c", run.replace(ISSUE, "12")], cwd=str(cwd), capture_output=True, text=True,
                          env={"PATH": "%s:/usr/bin:/bin" % bindir, **(env or {})})


@pytest.mark.parametrize("code,writes_new,ok", [(0, False, True), (1, False, False), (2, False, False), (2, True, True)])
def test_apply_step_swallows_only_the_plan_changed_refusal(tmp_path, code, writes_new, ok):
    run = _by_id()["push-apply"]["run"]
    body = ("mkdir -p .skj; : > .skj/plan-push.md.new\n" if writes_new else "") + "exit %d\n" % code
    assert (_sh(run, tmp_path, body).returncode == 0) == ok


@pytest.mark.parametrize("left_new", [False, True])
def test_applied_step_stops_the_run_while_the_plan_differs(tmp_path, left_new):
    if left_new:
        (tmp_path / ".skj").mkdir()
        (tmp_path / ".skj" / "plan-push.md.new").write_text("fresh plan")
    assert (_sh(_by_id()["push-applied"]["run"], tmp_path, "exit 0\n").returncode == 0) == (not left_new)


def test_checks_follow_the_review_and_wait_gates():
    ids = [s["id"] for s in _steps()]
    by_id = _by_id()
    for g in CHECKED:
        assert by_id["check-" + g]["run"] == "skj approvals check %s --gate %s" % (ISSUE, g)
    for g in ("review-spec", "review-plan", "review-review", "wait-po-answers"):
        assert ids[ids.index(g) + 1] == "check-" + g
    assert ids[-3:] == ["wait-po-validation", "sync-po-validation", "check-wait-po-validation"]
    assert by_id["sync-po-answers"]["run"] == "skj questions %s --sync --if-comment-mode" % ISSUE
    assert by_id["sync-po-validation"]["run"] == "skj decisions %s --sync --if-comment-mode" % ISSUE
    assert ids.index("check-wait-po-answers") + 1 == ids.index("sync-po-answers") == ids.index("plan-with-answers") - 1


def test_each_agent_step_is_followed_by_the_check_of_its_product():
    """DF7 D-025: `claude -p` exits 0 when the agent stops on a refused tool call, so right after every command step a shell step runs
    `skj dod check <n> --after <step>` (exit 1 stops the run). D-028: the review's check sits before gate review-review."""
    steps, ids = _steps(), [s["id"] for s in _steps()]
    commands = [s for s in steps if "command" in s]
    assert len(commands) == 7
    for s in commands:
        nxt = steps[ids.index(s["id"]) + 1]
        assert nxt["id"] == s["id"] + "-done" and nxt["type"] == "shell", s["id"]
        assert nxt["run"] == "skj dod check %s --after %s" % (ISSUE, s["command"].split(".")[-1]), s["id"]
    assert ids.index("review-done") + 1 == ids.index("review-review")


def test_review_gates_say_the_artifacts_are_judged_as_committed():
    # A4: review artifacts are hashed from the commit at HEAD, so the gate text tells the human to commit before approving
    by_id = _by_id()
    for g in ("review-spec", "review-plan", "review-review"):
        assert "committed at HEAD" in by_id[g]["message"], g


def test_strict_steps():
    by_id = _by_id()
    ids = [s["id"] for s in _wf()["steps"]]
    assert by_id["strict-check-implement"]["run"] == "skj strict check %s --phase implement" % ISSUE
    assert ids.index("push-applied") < ids.index("strict-check-implement") == ids.index("implement") - 1
    assert ids.index("decisions-applied") + 1 == ids.index("strict-verify") < ids.index("sync-loop")
    assert by_id["strict-verify"]["timeout"] == 7200  # the engine's shell default is 300 s: it would kill the test runs mid-way
    assert [s["id"] for s in _steps() if "timeout" in s] == ["strict-verify"]
    assert by_id["decisions-preview"]["run"] == "skj decisions %s --post --if-any --plan-out .skj/plan-decisions.md" % ISSUE


def test_strict_verify_sees_the_real_environment():
    # fail closed (L5a): no `env -i` or other scrub, so verify's credentials-in-env check sees what the test command's ancestors hold
    run = _by_id()["strict-verify"]["run"]
    assert run == "skj strict verify %s" % ISSUE and "env" not in run


def test_gate_check_runs_the_gate_and_halts_on_exit_1_and_3(tmp_path):
    run = _by_id()["gate-check"]["run"]
    assert run.startswith("bash <<'EOF'") and "skj gate %s --src . --base \"$base\"" % ISSUE in run
    for code, fails in ((0, False), (1, True), (2, False), (3, True)):
        assert (_sh(run, tmp_path, "exit %d\n" % code).returncode != 0) == fails, code


def test_command_steps_are_the_extension_commands_with_the_integration():
    commands = [s for s in _steps() if "command" in s]
    assert [s["id"] for s in commands] == ["pull", "plan", "plan-with-answers", "tasks", "implement", "gate", "review"]
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src" / "skj").glob("*.py"))
    for s in commands:
        name = s["command"].split(".")[-1]
        assert s["command"] == "speckit.jira-lifecycle." + name and name in EXT_COMMANDS
        assert s["command"] in src  # a command the product already points the user at
        assert s["integration"] == "{{ inputs.integration }}"
        assert s["input"]["args"].startswith(ISSUE)
    assert _by_id()["plan-with-answers"]["input"]["args"] == ISSUE + " --workflow --close-assumed"
    assert [s["id"] for s in _steps() if "model" in s] == ["review"] and _by_id()["review"]["model"] == "{{ inputs.review_model }}"


def test_the_run_ends_at_the_open_pr():
    assert not [s for s in _steps() if "skj done" in s.get("run", "") or s.get("command", "").endswith(".done")]


def _skj_commands():
    """(step id, argv) of every `skj ...` command in a shell step, with the issue number 12 and the shell's own words resolved."""
    out = []
    for s in _steps():
        if s.get("type") != "shell":
            continue
        text = s["run"].replace(ISSUE, "12").replace('"$base"', "origin/main").replace('"${args[@]}"', "")
        for found in re.finditer(r"(?:^|[\s;&|(])skj ([^|;&<>\n]*)", text):
            out.append((s["id"], shlex.split(found.group(1))))
    return out


def test_every_skj_command_parses_against_the_cli():
    found = _skj_commands()
    assert len(found) == 32  # the 39 shell steps but the seven `*-applied` guards, one `skj` command each
    parser = m.build_parser()
    for sid, argv in found:
        try:
            args = parser.parse_args(argv)
        except SystemExit:
            pytest.fail("%s: `skj %s` does not parse" % (sid, " ".join(argv)))
        if hasattr(args, "ref"):
            assert args.ref == "12" and keys.parse_ref(args.ref, PROJECT) == "SPK-12", sid
        if getattr(args, "expect_plan", None):
            assert args.apply, sid


# ---- the real engine (F1-AC2..4) -------------------------------------------------------------------------------------------------------

def _specify():
    path = shutil.which("specify") or os.path.expanduser("~/.local/bin/specify")
    if not os.path.isfile(path):
        (pytest.fail if ambient.CI else pytest.skip)("the specify CLI is not installed")  # the run's CI, not the test's
    return path


HELPER = r'''
import json, sys
sys.path[:0] = [%(src)r, %(tests)r]
from fakejira import FakeJira
import skj.__main__ as m
fake = FakeJira()
for i in range(12):
    fake.add_issue("A fictional story %%d" %% (i + 1))
code = "exception"
try:
    code = m.main(sys.argv[1:], client_factory=fake.client)
except SystemExit as e:
    code = e.code
finally:
    with open(%(log)r, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "exit": code, "requests": [[e["method"], e["path"]] for e in fake.log],
                            "writes": [[e["method"], e["path"]] for e in fake.writes()]}) + "\n")
sys.exit(code if isinstance(code, int) else 1)
'''


class Engine:
    """A temp Spec Kit project driven by the real `specify` CLI. PATH is fixed: `bin/` (a `skj` that runs the real CLI in-process against
    a fresh fake Jira holding SPK-1..SPK-12 and logs argv, exit and requests; an `id` that leaves a marker), then /usr/bin:/bin. HOME is
    private, there is no TTY, and every proxy points at a closed local port, so nothing can leave the machine."""

    def __init__(self, tmp_path):
        self.specify = _specify()
        self.dir = tmp_path / "proj"
        self.dir.mkdir()
        (self.dir / ".specify").mkdir()
        home, self.bin, self.log = tmp_path / "home", tmp_path / "bin", tmp_path / "skj.log"
        home.mkdir()
        self.bin.mkdir()
        helper = tmp_path / "fake_skj.py"
        helper.write_text(HELPER % {"src": str(ROOT / "src"), "tests": str(ROOT / "tests"), "log": str(self.log)}, encoding="utf-8")
        self._exe("skj", 'exec "%s" "%s" "$@"\n' % (sys.executable, helper))
        self.id_marker = tmp_path / "id-ran"
        self._exe("id", ': > "%s"\n' % self.id_marker)
        proxy = "http://127.0.0.1:9"
        self.env = {"PATH": "%s:/usr/bin:/bin" % self.bin, "HOME": str(home), "XDG_CACHE_HOME": str(home / ".cache"),
                    "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local" / "share"), "NO_COLOR": "1",
                    "TERM": "dumb", "LANG": "C.UTF-8", "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy,
                    "https_proxy": proxy, "ALL_PROXY": proxy, "NO_PROXY": "", "PYTHONDONTWRITEBYTECODE": "1"}

    def _exe(self, name, body):
        (self.bin / name).write_text("#!/bin/sh\n" + body, encoding="utf-8")
        (self.bin / name).chmod(0o755)

    def sp(self, *args, extra_env=None):
        return subprocess.run([self.specify, *args], cwd=str(self.dir), env={**self.env, **(extra_env or {})}, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=180)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def state(self, run_id):
        return json.loads((self.dir / ".specify" / "workflows" / "runs" / run_id / "state.json").read_text(encoding="utf-8"))

    def mini(self, step_ids, number_type="number"):
        """A workflow file with the SHIPPED metadata and inputs and the SHIPPED top-level steps `step_ids` (loops whole)."""
        wf = _wf()
        top = {s["id"]: s for s in wf["steps"]}
        wf["steps"] = [top[i] for i in step_ids]
        wf["inputs"]["issue_number"]["type"] = number_type
        path = self.dir.parent / "mini.yml"
        path.write_text(yaml.safe_dump(wf, sort_keys=False), encoding="utf-8")
        return str(path)

    def story(self):
        """The project files `skj push` needs for SPK-12 (terminal mode: no origin, so the defaults judge)."""
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.dir)], check=True, stdin=subprocess.DEVNULL, capture_output=True)
        (self.dir / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": PROJECT}), encoding="utf-8")
        (self.dir / "dod").mkdir()
        (self.dir / "dod" / "DoD_SPK-12.md").write_text(
            "# DoD SPK-12\n\n- **Spec dir:** specs/001-x\n\n## 1. Acceptance criteria\n\n- AC1 a\n", encoding="utf-8")
        (self.dir / "specs" / "001-x").mkdir(parents=True)
        (self.dir / "specs" / "001-x" / "tasks.md").write_text("- [ ] T001 A fictional task\n", encoding="utf-8")


@pytest.fixture
def engine(tmp_path):
    return Engine(tmp_path)


INJECTIONS = ("SPK-1;touch x", "1;touch x", "$(id)")


def _no_side_effect(engine):
    return not (engine.dir / "x").exists() and not engine.id_marker.exists() and engine.calls() == []


def test_the_shipped_workflow_installs_and_validates_on_the_real_engine(engine):
    cp = engine.sp("workflow", "add", str(WORKFLOW))  # validates first; exit 1 and no install on an invalid workflow
    assert cp.returncode == 0, cp.stdout + cp.stderr
    installed = engine.dir / ".specify" / "workflows" / "jira-lifecycle" / "workflow.yml"
    assert installed.read_bytes() == WORKFLOW.read_bytes()
    cp = engine.sp("workflow", "info", "jira-lifecycle")
    assert cp.returncode == 0 and "jira-lifecycle" in cp.stdout and "Steps (%d)" % len(_wf()["steps"]) in cp.stdout
    for name in _wf()["inputs"]:
        assert name in cp.stdout


def test_engine_rejects_injection_values(engine):
    """F1-AC2: the installed workflow and a shell-first copy refuse each injection value before any step: no marker, no `skj` call.
    The controls prove the harness would see an injection: the same copy with `type: string` runs `touch x` and `id`."""
    engine.story()
    assert engine.sp("workflow", "add", str(WORKFLOW)).returncode == 0
    mini = engine.mini(["strict-check-implement"])
    for value in INJECTIONS:
        for source in ("jira-lifecycle", mini):
            cp = engine.sp("workflow", "run", source, "--input", "issue_number=" + value, "--json")
            assert cp.returncode != 0 and "expected a number" in cp.stdout + cp.stderr, (source, value, cp.stdout, cp.stderr)
            assert _no_side_effect(engine), (source, value)
    cp = engine.sp("workflow", "run", mini, "--input", "issue_number=12", "--json")  # control: a number runs the step
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert [c["argv"] for c in engine.calls()] == [["strict", "check", "12", "--phase", "implement"]]
    engine.log.unlink()
    unsafe = engine.mini(["strict-check-implement"], number_type="string")  # control: a string input reaches the shell
    engine.sp("workflow", "run", unsafe, "--input", "issue_number=1;touch x #", "--json")
    engine.sp("workflow", "run", unsafe, "--input", "issue_number=$(id)", "--json")
    assert (engine.dir / "x").exists() and engine.id_marker.exists()


def test_number_becomes_project_key(engine):
    """F1-AC3: issue_number=12 reaches the CLI as `12` and the CLI reads and plans SPK-12."""
    engine.story()
    cp = engine.sp("workflow", "run", engine.mini(["push-loop", "push-applied"]), "--input", "issue_number=12", "--json")
    out = json.loads(cp.stdout)
    assert cp.returncode == 0 and out["status"] == "paused" and out["gate"]["step_id"] == "approve-push", cp.stdout + cp.stderr
    (call,) = engine.calls()
    assert call["argv"] == ["push", "12", "--plan-out", ".skj/plan-push.md"] and call["exit"] == 0 and call["writes"] == []
    assert ["GET", "/issue/SPK-12"] in call["requests"]
    assert "SPK-12" in (engine.dir / ".skj" / "plan-push.md").read_text(encoding="utf-8")
    assert engine.state(out["run_id"])["inputs"]["issue_number"] == 12


def test_cli_refuses_non_positive_or_fractional_number(engine, tmp_path, monkeypatch, capsys):
    """F1-AC3: the engine accepts 0, -5 and 1.5 as numbers; every `skj` command of the workflow refuses them (exit 2, a fixed text,
    no client built, no request)."""
    mini = engine.mini(["strict-check-implement"])
    for value, shown in (("0", "0"), ("-5", "-5"), ("1.5", "1.5")):
        cp = engine.sp("workflow", "run", mini, "--input", "issue_number=" + value, "--json")
        assert cp.returncode != 0 and "expected a number" not in cp.stdout + cp.stderr  # accepted as a number, the step failed
        call = engine.calls()[-1]
        assert call["argv"][2] == shown and call["exit"] == 2 and call["requests"] == []
    work = tmp_path / "cli"
    work.mkdir()
    (work / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": PROJECT}), encoding="utf-8")
    monkeypatch.chdir(work)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    built = []
    with_ref = [(sid, argv) for sid, argv in _skj_commands() if "12" in argv]
    assert len(with_ref) == 31  # every command but `stack show --check`
    for value in ("0", "-5", "1.5"):
        for sid, argv in with_ref:
            assert m.main([value if a == "12" else a for a in argv], client_factory=lambda: built.append(1)) == 2, (sid, value)
            assert capsys.readouterr().err.strip() == "skj: not an issue number or a key of project SPK", (sid, value)
    assert not built and sorted(p.name for p in work.iterdir()) == ["skj.config.json"]


def test_a_step_that_left_nothing_stops_the_run_on_the_real_engine(engine):
    """DF7 D-025, as in the owner session: the pull agent was refused `skj get`, left no ledger, and the run went on to review-spec.
    On the real engine `pull-done` now fails the run and nothing after it runs."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(engine.dir)], check=True, stdin=subprocess.DEVNULL, capture_output=True)
    (engine.dir / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": PROJECT}), encoding="utf-8")
    cp = engine.sp("workflow", "run", engine.mini(["pull-done", "stack-check"]), "--input", "issue_number=12", "--json")
    assert cp.returncode != 0
    calls = engine.calls()
    assert [c["argv"] for c in calls] == [["dod", "check", "12", "--after", "pull"]] and calls[0]["exit"] == 1
    assert calls[0]["requests"] == []


@pytest.mark.parametrize("verdict", ["reject", "approve"])
def test_rejected_gate_aborts_without_write(engine, verdict):
    """F1-AC4: the real engine pauses at approve-push after the preview; a reject (the resume the poller builds) aborts the run: no apply,
    no later step, no write in the request log. The approve control applies the plan (one POST /issue) and runs on."""
    engine.story()
    steps = ["push-loop", "push-applied", "strict-check-implement"]
    cp = engine.sp("workflow", "run", engine.mini(steps), "--input", "issue_number=12", "--json")
    rid = json.loads(cp.stdout)["run_id"]
    assert [c["argv"][0] for c in engine.calls()] == ["push"]
    cp = engine.sp(*approvals.resume_argv(rid, "approve-push", verdict)[1:])
    st, calls = engine.state(rid), engine.calls()
    if verdict == "reject":
        assert cp.returncode != 0 and st["status"] == "aborted", (st["status"], cp.stdout, cp.stderr)
        assert [c["argv"] for c in calls] == [["push", "12", "--plan-out", ".skj/plan-push.md"]]
        assert not [w for c in calls for w in c["writes"]]
        assert "push-apply" not in st["step_results"] and "strict-check-implement" not in st["step_results"]
    else:
        assert cp.returncode == 0 and st["status"] == "completed", (st["status"], cp.stdout, cp.stderr)
        assert [c["argv"][:2] for c in calls] == [["push", "12"], ["push", "12"], ["strict", "check"]]
        assert calls[1]["argv"][2:] == ["--apply", "--expect-plan", ".skj/plan-push.md"] and calls[1]["writes"] == [["POST", "/issue"]]


def _verify_rules(engine, run_id):
    out = engine.state(run_id)["step_results"]["strict-verify"]["output"]["stdout"]
    return [f["rule"] for f in json.loads(out)["findings"]]


def test_strict_verify_refuses_a_run_started_with_credentials(engine):
    """L5a fail-closed through the real engine: the shipped strict-verify step runs the real `skj strict verify` (strict mode); a run
    started with SKJ_TOKEN set refuses with credentials-in-env only (names only, never the value), and a clean environment does not
    raise that finding."""
    engine.story()
    (engine.dir / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": PROJECT, "mode": "strict"}),
                                                encoding="utf-8")
    mini = engine.mini(["strict-verify"])
    cp = engine.sp("workflow", "run", mini, "--input", "issue_number=12", "--json", extra_env={"SKJ_TOKEN": "FAKE-leak-probe"})
    out = json.loads(cp.stdout)
    assert cp.returncode != 0 and out["status"] == "failed" and _verify_rules(engine, out["run_id"]) == ["credentials-in-env"]
    assert engine.calls()[-1]["argv"] == ["strict", "verify", "12"] and engine.calls()[-1]["exit"] == 2
    assert "FAKE-leak-probe" not in json.dumps(engine.state(out["run_id"]))
    cp = engine.sp("workflow", "run", mini, "--input", "issue_number=12", "--json")
    rules = _verify_rules(engine, json.loads(cp.stdout)["run_id"])
    assert "credentials-in-env" not in rules and rules  # the clean run gets past the check (this fictional project then has findings)


def test_a_resume_re_checks_the_number_only_when_it_passes_inputs(engine):
    """A resume with any `--input` (the poller's `approvals.resume_argv` always passes the twelve verdicts) re-checks every input, so a
    state.json whose issue_number was replaced by shell text is refused. A PLAIN resume does not re-check (engine behaviour, pinned so a
    change is noticed): that is why the runbook says `specify workflow resume <run> --input issue_number=<n>`."""
    engine.story()
    mini = engine.mini(["review-spec", "strict-check-implement"])

    def paused_with_shell_text():
        rid = json.loads(engine.sp("workflow", "run", mini, "--input", "issue_number=12", "--json").stdout)["run_id"]
        path = engine.dir / ".specify" / "workflows" / "runs" / rid / "state.json"
        st = json.loads(path.read_text(encoding="utf-8"))
        st["inputs"].update(issue_number="1;touch x #", verdict_review_spec="approve")
        path.write_text(json.dumps(st), encoding="utf-8")
        return rid

    rid = paused_with_shell_text()
    cp = engine.sp(*approvals.resume_argv(rid, "review-spec", "approve")[1:])  # the poller's resume
    assert cp.returncode != 0 and "expected a number" in cp.stdout + cp.stderr
    assert not (engine.dir / "x").exists() and engine.calls() == []
    cp = engine.sp("workflow", "resume", rid, "--input", "issue_number=12")  # the runbook's manual resume: the number replaces the text
    assert cp.returncode == 0 and engine.state(rid)["inputs"]["issue_number"] == 12, cp.stdout + cp.stderr
    assert not (engine.dir / "x").exists() and [c["argv"][2] for c in engine.calls()] == ["12"]
    engine.sp("workflow", "resume", paused_with_shell_text())  # plain: the saved text reaches the shell
    assert (engine.dir / "x").exists()


# ---- T2: the workflow folder's README, LICENSE and CHANGELOG ----------------------------------------------------------------------
WF_DIR = WORKFLOW.parent
KIND = {"command": "command", "shell": "shell", "gate": "gate", "do-while": "loop"}


def _readme_rows(heading):
    """(first cell, other cells) of each table row under `## <heading>` in the workflow README, in order."""
    part = (WF_DIR / "README.md").read_text(encoding="utf-8").split("\n## %s\n" % heading, 1)[1].split("\n## ", 1)[0]
    rows = [[c.strip() for c in line.strip().strip("|").split("|")] for line in part.splitlines() if line.startswith("| `")]
    return [(r[0].strip("`"), r[1:]) for r in rows]


def test_t2_the_three_folder_files_exist_and_are_tracked():
    tracked = subprocess.run(["git", "ls-files", "--", "workflows/jira-lifecycle"], cwd=str(ROOT), capture_output=True, text=True,
                             check=True).stdout.split()
    assert sorted(tracked) == ["workflows/jira-lifecycle/" + n for n in ("CHANGELOG.md", "LICENSE", "README.md", "workflow.yml")]
    assert (WF_DIR / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    changelog = (WF_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "\n## [%s] - 2026-11-13\n" % _wf()["workflow"]["version"] in changelog


def test_t2_the_readme_lists_every_input_and_every_step_in_order_with_each_gate_purpose():
    wf = _wf()
    assert [name for name, _ in _readme_rows("Inputs")] == list(wf["inputs"])
    assert [cells[0] for _, cells in _readme_rows("Inputs")] == [i["type"] for i in wf["inputs"].values()]
    rows = _readme_rows("Steps")
    assert [(sid, cells[0]) for sid, cells in rows] == [(s["id"], KIND[s.get("type", "command")]) for s in _steps(wf)]
    gates = [cells[1] for _, cells in rows if cells[0] == "gate"]
    assert len(gates) == 12 and all(len(purpose.split()) >= 5 for purpose in gates)


def test_t2_the_readme_says_why_the_three_fork_steps_are_missing():
    text = (WF_DIR / "README.md").read_text(encoding="utf-8")
    missing = [line for line in text.splitlines() if "`refresh-context`, `pr-clear` and `pr-prepare` steps are not in 0.1:" in line]
    assert len(missing) == 1 and "skj comments" in missing[0] and "from the ledger" in missing[0]
    assert "root README of the kit's repository (`https://github.com/praveenlavu/spec-kit-jira`)" in text and "Atlassian" not in text  # the trademark line lives in the root README only (X9)
    assert not {"refresh-context", "pr-clear", "pr-prepare"} & {s["id"] for s in _steps()}
