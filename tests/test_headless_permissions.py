"""DF7 (D-024): the permission set for the headless agent the workflow starts (`claude -p`, where no tool call can be approved).

One place holds it: docs/claude-permissions.json, in the `.claude/settings.json` form (`permissions.defaultMode`, `allow`, `deny`). The
docs show it a second way, as the `SPECKIT_INTEGRATION_CLAUDE_EXTRA_ARGS` line Spec Kit adds to the agent's command line (shlex-split),
which works without the one-time trust accept. Rules are matched here the way Claude Code reads them: `Bash(<prefix>:*)` is a prefix
match on whole words, any other `*` matches any text; a command is allowed when an allow rule matches and no deny rule does.
"""
import json
import re
import shlex
import subprocess
from pathlib import Path

import yaml

from test_manifest import _code_lines, _fill, _specify, skj_commands

ROOT = Path(__file__).resolve().parent.parent
PERMS = ROOT / "docs" / "claude-permissions.json"
WORKFLOW = ROOT / "workflows" / "jira-lifecycle" / "workflow.yml"
ENV = "SPECKIT_INTEGRATION_CLAUDE_EXTRA_ARGS"
DOCS = ("docs/quick-start.md", "workflows/jira-lifecycle/README.md")


def _perms():
    return json.loads(PERMS.read_text(encoding="utf-8"))["permissions"]


def _matches(rule, cmd):
    body = re.fullmatch(r"Bash\((.+)\)", rule).group(1)
    if body.endswith(":*"):
        return cmd == body[:-2] or cmd.startswith(body[:-2] + " ")
    return re.fullmatch(".*".join(re.escape(p) for p in body.split("*")), cmd, re.S) is not None


def allowed(cmd, perms=None):
    p = perms or _perms()
    return any(_matches(r, cmd) for r in p["allow"]) and not any(_matches(r, cmd) for r in p["deny"])


def gated(argv):
    """True for a `skj` command that never runs in the agent: a Jira or PR write, a human-only verb, or a step the workflow runs itself."""
    if argv[0] in ("push", "pr", "done", "approvals", "setup"):
        return True
    if argv[:2] in (["stack", "approve"], ["stack", "init"], ["kits", "pin"], ["dod", "refresh"], ["strict", "verify"]):
        return True
    return (argv[0] == "questions" and "--apply" in argv) or (argv[0] == "decisions" and bool({"--post", "--resolve"} & set(argv)))


def _step_files():
    """The command files the workflow starts the agent with (its `command:` steps)."""
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["steps"]
    names, todo = [], list(steps)
    while todo:
        s = todo.pop(0)
        todo[:0] = s.get("steps", [])
        if "command" in s and s["command"].split(".")[-1] not in names:
            names.append(s["command"].split(".")[-1])
    return {n: (ROOT / "commands" / ("%s.md" % n)).read_text(encoding="utf-8") for n in names}


def test_the_step_files_are_the_six_agent_steps():
    assert sorted(_step_files()) == ["gate", "implement", "plan", "pull", "review", "tasks"]


def test_every_skj_command_of_a_step_is_allowed_unless_it_stays_behind_a_gate():
    seen = 0
    for name, text in _step_files().items():
        for raw, argv in skj_commands(text):
            cmd = "skj " + " ".join(shlex.quote(a) for a in argv)
            seen += 1
            assert allowed(cmd) != gated(argv), (name, cmd, "allowed" if allowed(cmd) else "not allowed")
    assert seen > 60


WRITES = ("skj push 12 --apply --expect-plan .skj/plan-push.md", "skj push 12 --plan-out .skj/plan-push.md",
          "skj pr create 12 --push --apply --expect-plan .skj/plan-pr.md", "skj pr link 12 --url u --apply --expect-plan p",
          "skj done 12 --apply --expect-plan .skj/plan-done.md", "skj questions 12 --post --apply --expect-plan .skj/plan-questions.md",
          "skj questions --apply 12 --post --expect-plan p", "skj questions 12 --apply", "skj decisions 12 --post --plan-out p",
          "skj decisions --post 12 --apply --expect-plan p", "skj decisions 12 --resolve D1 --validated",
          "skj decisions --resolve D1 12 --rejected --force", "skj approvals sync r1 --resume", "skj approvals check 12 --gate review-spec",
          "skj stack approve --apply", "skj kits pin k --apply", "skj setup", "skj dod refresh 12", "skj strict verify 12",
          "git push origin HEAD", "git push")


def test_no_jira_or_pr_write_and_no_human_only_verb_is_allowed():
    p = _perms()
    for cmd in WRITES:
        assert not allowed(cmd), cmd
    for rule in p["allow"]:
        body = re.fullmatch(r"Bash\((.+)\)", rule).group(1)
        assert body.endswith(":*") and "*" not in body[:-2], rule  # prefixes only: no wildcard that could reach a write verb
        words = body[:-2].split()
        if words[0] == "skj":
            assert len(words) >= 2 and not gated(words[1:]), rule
        assert words[:2] != ["git", "push"], rule
    assert p["defaultMode"] == "acceptEdits" and set(p) == {"defaultMode", "allow", "deny"}


def test_local_writes_the_steps_need_stay_allowed():
    # a blanket `skj * --apply*` deny was wrong: pull's `skj branch <ref> --apply` is a local `git switch -c`
    for cmd in ("skj branch 12 --apply", "skj dod seed 12", "skj decisions 12 --add 'x' --kind decision --why 'y'", "skj rules 12 --confirm",
                "skj questions 12 --post --plan-out .skj/plan-questions.md", "skj gate 12 --src . --base origin/main"):
        assert allowed(cmd), cmd


def test_the_matcher_finds_what_it_must():
    p = {"allow": ["Bash(skj questions:*)"], "deny": ["Bash(skj questions * --apply*)"]}
    assert allowed("skj questions 12", p) and allowed("skj questions 12 --post --plan-out x", p)
    assert not allowed("skj questions 12 --post --apply --expect-plan x", p) and not allowed("skj questionsx 12", p)
    assert not allowed("skj push 12", p)


def test_every_git_command_of_a_step_is_allowed_and_no_push():
    seen = 0
    for name, text in _step_files().items():
        for line in _code_lines(text):
            for found in re.finditer(r"(?<![\w./-])git ([a-z][a-z-]*)([^`|;&]*)", line):
                seen += 1
                cmd = _fill("git %s%s" % (found.group(1), found.group(2))).strip()
                assert found.group(1) != "push" and allowed(cmd), (name, cmd)
    assert seen > 20


def _core_pack():
    py = open(_specify(), encoding="utf-8").readline()[2:].strip()
    out = subprocess.run([py, "-I", "-c", "import os, specify_cli; print(os.path.dirname(specify_cli.__file__))"], capture_output=True,
                         text=True, check=True)
    return Path(out.stdout.strip()) / "core_pack" / "commands"


def test_every_spec_kit_script_a_routed_core_command_runs_is_allowed():
    """The core commands the steps route to (specify, plan, tasks, implement) run `scripts/bash/<name>.sh` from `.specify/` (installed
    Spec Kit, read-only). Each must be allowed, or the plan phase writes plan.md by hand as in the owner session."""
    found = []
    for name in ("specify", "plan", "tasks", "implement"):
        text = (_core_pack() / ("%s.md" % name)).read_text(encoding="utf-8")
        found += re.findall(r"^\s+sh: scripts/bash/([a-z-]+\.sh)( [^\n]*)?$", text, re.M)
    assert len(found) >= 3
    for script, args in found:
        cmd = ".specify/scripts/bash/%s%s" % (script, args)
        assert allowed(cmd), cmd


def _env_value(doc):
    lines = [x for x in (ROOT / doc).read_text(encoding="utf-8").splitlines() if x.startswith("export %s=" % ENV)]
    assert len(lines) == 1, doc
    return shlex.split(lines[0][len("export %s=" % ENV):])[0]


def test_the_docs_show_the_one_list_as_the_env_var_and_point_at_the_settings_file():
    p = _perms()
    for doc in DOCS:
        argv = shlex.split(_env_value(doc))
        assert argv == ["--permission-mode", p["defaultMode"], "--allowedTools", ",".join(p["allow"]),
                        "--disallowedTools", ",".join(p["deny"])], doc
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert "claude-permissions.json" in text and ".claude/settings.json" in text and "trust" in text, doc
        assert "dangerously" not in text and "bypassPermissions" not in text, doc
    readme = (ROOT / "README.md").read_text(encoding="utf-8").split("## Quick start", 1)[1].split("**1. Install.**", 1)[0]
    assert "docs/claude-permissions.json" in readme and "headless" in readme
