"""C1-AC3/AC4: the local rehearsal of the four ci/templates/ files (scripts/ci_smoke/cases.json, cases a to d) against the fake Jira.

The harness reads each template's YAML and runs ITS `run:` steps (not a copy typed here) as a runner would: bash -eo pipefail, the step's
`env:` with the `${{ }}` values of a fictional pull request filled in, $GITHUB_ENV between steps, `if:` honoured, in a checkout of a local
bare origin made the way actions/checkout makes it (the template's fetch-depth; no origin/HEAD). `skj` on PATH is this checkout's real
CLI whose Jira client gets the fake as its opener (the site pin and the SKJ_EMAIL/SKJ_TOKEN secrets still go through the real code). It
is put there by the template's own `Install skj` step, run as written, through a stub pip in a fresh Python environment; the jobs with
`plant` add pip.py, skj.py, sitecustomize.py, usercustomize.py and json.py to the pull request and show that none of them ever runs.
No network, no GitHub, no `gh`. The templates have not run in a hosted repository of their own (U14).
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SMOKE = ROOT / "scripts" / "ci_smoke"
TEMPLATE_CASES = {"a": "ci/templates/check-pull-request.yml", "b": "ci/templates/check-branch-name.yml",
                  "c": "ci/templates/link-pull-request.yml", "d": "ci/templates/final-check.yml"}


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("ci_smoke_run_local_c1", SMOKE / "run_local.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", sorted(TEMPLATE_CASES))
def test_cases_a_to_d_on_fake(smoke, name):
    """C1-AC3: every job of the case gives its expected exit code, output, Jira state and steps run, and no `run:` step of the template
    escapes the rehearsal: each one (the install too, through the stub pip) runs in at least one job."""
    case = smoke.load_cases()[name]
    assert case["template"] == TEMPLATE_CASES[name]
    ran = set()
    assert smoke.run_case(name, case, ran) == []
    _, job = smoke.load_template(case["template"])
    assert {s["name"] for s in job["steps"] if "run" in s} <= ran


def test_link_on_merge_is_idempotent(smoke):
    """C1-AC4: the template's link step run twice for the same merged PR leaves ONE remote link and ONE comment (P2's guarantee, through
    the template's own command line)."""
    case = smoke.load_cases()["c"]
    twice = [j for j in case["jobs"] if j["title"] in ("merged: one link and one comment", "the same run again: nothing more")]
    assert [j["jira"] for j in twice] == [[1, 1], [1, 1]]
    assert smoke.run_case("c", dict(case, jobs=twice)) == []


@pytest.mark.parametrize("change", [
    lambda c: c["jobs"][0].update(exit=1),                                     # a job that passes expected to fail
    lambda c: c["jobs"][0].update(out_has=["a text the job never prints"]),
    lambda c: c["jobs"][0].update(out_lacks=["Story key"]),
    lambda c: c["jobs"][0].update(ran=["Find the story key"]),
    lambda c: c["jobs"][0].update(fetch_depth=1),                              # the shallow clone: the gate no longer answers
    lambda c: c["jobs"][0].update(branch="feature/members"),                   # no key: the job fails
])
def test_a_changed_expectation_is_reported_as_a_difference(smoke, change):
    """The harness can fail: each change to case a's first job makes `run_case` report it."""
    case = json.loads(json.dumps(smoke.load_cases()["a"]))
    case["jobs"] = case["jobs"][:1]
    change(case)
    assert smoke.run_case("a", case) != []


@pytest.mark.parametrize("change", [lambda j: j.update(jira=[2, 2]), lambda j: j.update(absent=["env"])])
def test_a_wrong_jira_state_or_a_present_file_is_a_difference(smoke, change):
    case = json.loads(json.dumps(smoke.load_cases()["c"]))
    case["jobs"] = [j for j in case["jobs"] if j["title"] == "merged: one link and one comment"]
    change(case["jobs"][0])
    assert smoke.run_case("c", case) != []


def test_an_expression_the_rehearsal_does_not_know_is_an_error(smoke, tmp_path):
    """A template that gains a `${{ }}` value the harness cannot fill fails loudly instead of running with an empty value."""
    with pytest.raises(KeyError, match="no value"):
        smoke._fill({"X": "${{ github.event.pull_request.body }}"}, {})
    with pytest.raises(ValueError, match="one whole expression"):
        smoke._fill({"X": "a ${{ github.head_ref }}"}, {"github.head_ref": "b"})
    with pytest.raises(ValueError, match="cannot evaluate"):
        smoke._if("github.event_name == 'push'", {}, {}, {})
