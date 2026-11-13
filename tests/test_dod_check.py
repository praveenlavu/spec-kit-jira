"""DF7 (D-025, D-028): `skj dod check <ref> --after <step>`, the check the workflow runs after each agent `command:` step.

`claude -p` exits 0 when the agent stops on a refused tool call, so the engine carried on with nothing done (the owner session, LV4). The
check reads only local files and git: exit 0 when the step's product is there and committed, else exit 1 with one line that names the
step and points at the headless permission docs. A review that did not run (a `could not run` block, or no well-formed block at a commit
the code has not moved past) stops the run before gate review-review (D-028). Real temporary git repositories, no Jira, no network."""
import pytest

import skj.__main__ as m
from skj import dod
from test_stack_support import commit, git, git_env  # noqa: F401  (git_env: fixture)
from test_status import KEY, env, world  # noqa: F401  (env: fixture)

LEDGER = "dod/DoD_%s.md" % KEY
FILLED = ("- **Approach / chosen option + why:** a\n- **Assumptions:** b\n- **Use cases & edge cases:** c\n")


def _check(step, capsys):
    code = m.main(["dod", "check", KEY, "--after", step])
    return code, capsys.readouterr().err.strip()


def _edit(repo, old, new, msg="docs: ledger SPK-1", commit_it=True):
    text = (repo / LEDGER).read_text(encoding="utf-8")
    assert old in text, old
    (repo / LEDGER).write_text(text.replace(old, new, 1), encoding="utf-8")
    if commit_it:
        commit(repo, msg)


@pytest.fixture
def done(env):
    """A story whose every agent step is complete: section 2 filled, ledger committed."""
    world(env)
    _edit(env, "- **Approach:** a\n", FILLED)
    return env


STEPS = ("pull", "plan", "tasks", "implement", "gate", "review")


def test_every_step_passes_on_a_complete_story(done, capsys):
    for step in STEPS:
        assert _check(step, capsys) == (0, ""), step


def _fails(step, capsys, why):
    code, err = _check(step, capsys)
    assert code == 1 and err.count("\n") == 0, (step, err)
    assert err.startswith("skj: the %s step did not finish: " % step) and why in err, err
    assert "docs/quick-start.md#the-workflow-runs-the-agent-headless" in err
    return err


def test_no_ledger_fails_every_step(env, capsys):
    world(env, ledger=False)
    for step in STEPS:
        _fails(step, capsys, "no readable ledger")


def test_pull_needs_a_spec_dir_and_its_spec(env, capsys):
    world(env, spec=False)
    _fails("pull", capsys, "spec.md")
    _edit(env, "- **Spec dir:** specs/001-x\n", "- **Spec dir:** %s\n" % dod.SPEC_DIR_UNSET)
    _fails("pull", capsys, "Spec dir")
    _edit(env, "- **Spec dir:** %s\n" % dod.SPEC_DIR_UNSET, "- **Spec dir:** specs/0 1\n")  # not the shape every step accepts
    _fails("pull", capsys, "Spec dir")


def test_plan_needs_plan_md_and_a_filled_section_2(done, capsys):
    _edit(done, FILLED, "- **Approach / chosen option + why:** _(fill in)_\n- **Assumptions:** b\n- **Use cases & edge cases:** c\n")
    _fails("plan", capsys, "section 2")
    git(done, "rm", "-q", "specs/001-x/plan.md")
    commit(done, "chore: drop plan SPK-1")
    _fails("plan", capsys, "plan.md")


def test_tasks_needs_a_task(done, capsys):
    (done / "specs/001-x/tasks.md").write_text("# no tasks\n")
    commit(done, "docs: tasks SPK-1")
    _fails("tasks", capsys, "tasks.md")


def test_implement_needs_section_3_filled(done, capsys):
    _edit(done, "| AC-1 | it works |", "| AC-1 | _(what it must do)_ |")
    _fails("implement", capsys, "section 3")


def test_every_step_needs_its_files_committed(done, capsys):
    _edit(done, FILLED, FILLED + "- **Questions posted:** 10001:abcdef12\n", commit_it=False)
    for step in STEPS:
        _fails(step, capsys, "uncommitted")
    git(done, "checkout", "-q", "--", LEDGER)
    (done / "specs/001-x/new.md").write_text("x\n")  # an untracked file in the spec dir counts too
    _fails("pull", capsys, "uncommitted")


def test_gate_needs_a_gate_block(env, capsys):
    world(env, gate=None)
    _fails("gate", capsys, "gate result")


def test_a_stale_gate_fails(done, capsys):
    world_stale(done)
    _fails("gate", capsys, "code changed")


def world_stale(repo):
    commit(repo, "feat: more SPK-1", {"src/more.py": "y = 2\n"})


@pytest.mark.parametrize("heading", ["### Review — 2026-11-13 — could not run (no branch commits)\n",
                                     "### Review — 2026-11-13 — `main...HEAD` @ `abc1234` — not run\n"])
def test_review_that_did_not_run_stops_the_run(done, capsys, heading):
    """D-028: the owner session's review wrote `could not run (no branch commits)` and the run still reached gate review-review."""
    text = (done / LEDGER).read_text(encoding="utf-8")
    start = text.index("### Review")
    end = text.index("## 5.")
    (done / LEDGER).write_text(text[:start] + heading + "\n- **Confirmed by severity:** blocker 0 · major 0 · minor 0\n"
                               "- **Blocks PR:** no\n\n" + text[end:], encoding="utf-8")
    commit(done, "docs: review log SPK-1")
    _fails("review", capsys, "review")


def test_review_needs_a_current_well_formed_block(env, capsys):
    world(env, review=None)
    _fails("review", capsys, "no review")


def test_a_stale_review_fails(done, capsys):
    world_stale(done)
    _fails("review", capsys, "code changed")


def test_the_step_is_one_of_the_six(done, capsys):
    with pytest.raises(SystemExit):
        m.main(["dod", "check", KEY, "--after", "push"])
    capsys.readouterr()


def test_the_could_not_run_heading_review_md_prescribes_stops_the_run(done, capsys):
    from pathlib import Path
    import re
    text = (Path(__file__).resolve().parent.parent / "commands" / "review.md").read_text(encoding="utf-8")
    forms = set(re.findall(r"`(### Review — <YYYY-MM-DD> — could not run \([^`]*\))`", text))
    assert len(forms) == 2 and "`review-done` step reads it as no review" in text
    for form in forms:
        heading = form.replace("<YYYY-MM-DD>", "2026-11-13").replace("<why>", "no branch commits")
        log = (done / LEDGER).read_text(encoding="utf-8")
        (done / LEDGER).write_text(log.replace("## 5.", heading + "\n\n## 5.", 1), encoding="utf-8")
        commit(done, "docs: review log SPK-1")
        _fails("review", capsys, "did not run")
