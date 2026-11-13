import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Explicit list of ported files, relative to the repo root. Later tasks extend it (F2 added the manifest and the 13 prompts;
# tests/test_manifest.py holds the ADO patterns it greps for, so it is not scanned, as this file is not):
# A3 adds the deferred strict suite (test_strict_xcheck.py); L5b added test_strict_checks.py. F3 added the agent documents (tests/test_docs.py
# holds the ADO patterns it greps for, so it is not scanned). F4 added the contract suite, its CI workflow and the ci_smoke rehearsal;
# C1 the four CI templates, their README, the kit's own tests workflow and their tests; C2 the README and the docs/ pages.
PORTED = [
    "src/skj/tasks.py",
    "tests/test_tasks.py",
    "src/skj/config.py",
    "tests/test_config.py",
    "src/skj/dod.py",
    "tests/test_dod.py",
    "src/skj/branch.py",
    "tests/test_branch.py",
    "src/skj/decisions.py",
    "tests/test_decisions.py",
    "src/skj/questions.py",
    "tests/test_questions.py",
    "src/skj/gate.py",
    "tests/test_gate.py",
    "src/skj/stack.py",
    "tests/test_stack_support.py",
    "tests/test_stack_approve.py",
    "tests/test_stack_coverage.py",
    "tests/test_stack_detect.py",
    "tests/test_stack_init.py",
    "tests/test_stack_show.py",
    "tests/test_stack_validate.py",
    "tests/test_seed_node_typescript.py",
    "tests/test_seed_python_pytest.py",
    "tests/test_seed_salesforce.py",
    "src/skj/seeds/node-typescript.json",
    "src/skj/seeds/python-pytest.json",
    "src/skj/seeds/salesforce.json",
    "src/skj/strict.py",
    "tests/test_strict.py",
    "tests/test_strict_runner.py",
    "tests/test_strict_wire.py",
    "tests/test_ci_trust.py",
    "tests/test_strict_stubs.py",
    "tests/test_strict_linear.py",
    "tests/test_strict_checks.py",
    "src/skj/__main__.py",
    "tests/test_x2_no_write_without_apply.py",
    "src/skj/push.py",
    "tests/test_push.py",
    "src/skj/prs.py",
    "tests/test_prs.py",
    "src/skj/prlink.py",
    "tests/test_pr_link.py",
    "src/skj/templates/pr-body.md",
    "src/skj/attachments.py",
    "tests/test_attachments.py",
    "src/skj/done.py",
    "tests/test_done.py",
    "src/skj/kits.py",
    "tests/test_kits.py",
    "tests/test_kits_scan.py",
    "src/skj/calibrate.py",
    "src/skj/golden/fidelity.json",
    "tests/test_calibrate.py",
    "src/skj/status.py",
    "tests/test_status.py",
    "src/skj/approvals.py",
    "tests/test_approvals.py",
    "tests/test_newest_posting.py",
    "tests/test_strict_xcheck.py",
    "tests/test_trust_crosscheck.py",
    "workflows/jira-lifecycle/workflow.yml",
    "workflows/jira-lifecycle/README.md",
    "workflows/jira-lifecycle/CHANGELOG.md",
    "tests/test_workflow.py",
    "extension.yml",
    "commands/constitution.md",
    "commands/done.md",
    "commands/gate.md",
    "commands/implement.md",
    "commands/plan.md",
    "commands/pr.md",
    "commands/pull.md",
    "commands/push.md",
    "commands/review.md",
    "commands/setup.md",
    "commands/stack.md",
    "commands/status.md",
    "commands/tasks.md",
    "AGENTS.md",
    "CLAUDE.md",
    "rules/hitl.md",
    "rules/routing.md",
    "tests/test_speckit_contract.py",
    ".github/workflows/speckit-contract.yml",
    "scripts/ci_smoke/run_local.py",
    "scripts/ci_smoke/cases.json",
    "scripts/ci_smoke/conftest.py",
    "scripts/ci_smoke/fixtures/skj.config.json",
    "scripts/ci_smoke/fixtures/feature/src/app.py",
    "tests/test_ci_smoke.py",
    "ci/README.md",
    "ci/templates/check-pull-request.yml",
    "ci/templates/check-branch-name.yml",
    "ci/templates/link-pull-request.yml",
    "ci/templates/final-check.yml",
    ".github/workflows/tests.yml",
    "tests/test_github_workflows.py",
    "tests/test_ci_smoke_fixtures.py",
    "tests/ambient.py",
    "scripts/ci_smoke/fixtures/planted/pip.py",
    "scripts/ci_smoke/fixtures/planted/skj.py",
    "scripts/ci_smoke/fixtures/planted/sitecustomize.py",
    "scripts/ci_smoke/fixtures/planted/usercustomize.py",
    "scripts/ci_smoke/fixtures/planted/json.py",
    "README.md",
    "docs/quick-start.md",
    "docs/configuration.md",
    "docs/glossary.md",
    "docs/security-and-trust.md",
    "docs/ci.md",
    "docs/compatibility.md",
    "docs/stack-and-kits.md",
    "docs/unverified.md",
    "src/skj/rules.py",
    "tests/test_rules_parse.py",
    "tests/test_rules_file.py",
    "tests/test_rules_fixtures.py",
    "tests/test_rules_pull.py",
    "tests/test_rules_scope.py",
    "tests/test_rules_coverage.py",
    "tests/test_rules_drift.py",
    "tests/test_rules_strict.py",
    "tests/test_rules_reconfirmed.py",
    "tests/test_rules_confirm.py",
    ".gitignore",  # Fix-Minor (FM-AC1): the fork's `.ado/` entry was parity leftover N9
]

# Sanctioned exceptions: (file, line text) pairs allowed to match. Keep tiny, justify each.
SANCTIONED = [
    # Apex's own assertion API (`System.assert`), which the gate's default Apex regex recognises: test fixtures, not a fork name
    ("tests/test_gate.py", 'APEX = "@IsTest private class FooTest { @IsTest static void testX(){ System.assert(true); } }"'),
    ("tests/test_gate.py", '("pkg/cart_test.go", "t.Error(1)"), ("classes/CartTest.cls", "System.assert(true)"),'),
    # T2: the one sentence that says which steps of the kit this one was forked from are not in 0.1, and why (parity row N7)
    ("workflows/jira-lifecycle/README.md", "The ADO kit's `refresh-context`, `pr-clear` and `pr-prepare` steps are not in 0.1: `skj "
     "comments` takes a full key and writes no context file (the review command reads `skj comments <KEY> --md` itself), and `skj pr "
     "create` builds the PR title and body from the ledger, so there are no prepared PR files to clear or prepare."),
    # M1: the README's one link to the public Azure DevOps sibling kit (pinned as FAMILY_ADO_LINE in tests/test_docs.py)
    ("README.md", "- **Azure DevOps:** [spec-kit-ado](https://github.com/praveenlavu/spec-kit-ado), for Azure DevOps work items."),
]

# Same pattern as the plan's grep -niE "\bado\b|AB#|azure|dev\.azure|System\."
NAMES = re.compile(r"\bado\b|AB#|azure|dev\.azure|System\.", re.IGNORECASE)


def test_ported_files_have_no_ado_names():
    hits = []
    for rel in PORTED:
        path = ROOT / rel
        assert path.is_file(), f"ported file missing: {rel}"
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if NAMES.search(line) and (rel, line.strip()) not in SANCTIONED:
                hits.append(f"{rel}:{n}")
    assert not hits, hits
