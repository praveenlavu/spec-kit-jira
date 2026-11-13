"""Z3: the release documents (CHANGELOG.md, VERSIONING.md, CONTRIBUTING.md, docs/release-checklist.md, docs/catalog-submission.md).

They are pinned so the release cannot drift from the repository: one version in every place it lives, every placeholder the
earlier tasks left has a row saying what replaces it, hosted CI is recorded as blocked on the publication decision (O2) while no
tag exists, the hosted runs are recorded as what ran and what is left (HD), and the docs keep the C2 rules (commands parse, links resolve,
no fork vocabulary, no account names, the not-affiliated line only in the README, plain headings)."""
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

import skj
import test_catalog as tc
import test_docs as td
import test_manifest as tm
from test_speckit_contract import ACTION_PINS

ROOT = Path(__file__).resolve().parent.parent
NEW = ("CHANGELOG.md", "VERSIONING.md", "CONTRIBUTING.md", "docs/release-checklist.md", "docs/catalog-submission.md")
CHECKLIST = "docs/release-checklist.md"
# The release commit flips these on purpose, together with the docs lines they pin (docs/release-checklist.md, section 4):
HOSTED_CI_GREEN = False  # True once the scrub job, a consumer-repo run of ci/ and a fork pull request are green: X7_LEFT then goes
TAG_CREATED = True  # True in the commit before the owner tags: the no-tag sentence and the empty `git tag -l` check then go
CHANGELOG_DATE = "2026-11-13"  # the release date, set by the owner in the release commit
# HD (hosted runs of 2026-11-12): what ran on GitHub-hosted runners, and what is left. No run count or minutes: they would go stale.
HOSTED_RAN = ("The tests on Python 3.9 and 3.13, the Spec Kit contract job on 1.2.1 and the latest release, and `actionlint` "
              "pass on GitHub-hosted runners (`ubuntu-24.04`, 2026-11-12).")
X7_LEFT = ("Hosted CI green (X7) is not complete: the scrub job has not passed yet (its `SCRUB_DENYLIST` secret was stored after the "
           "run), and the checks in ci/ have not run in a repository of their own, nor on a pull request from a fork.")
NO_TAG = "No tag has been created. Only the owner creates the release tag."
GONE = ("blocked on O2: no repository exists yet", "A hosted Python 3.13 run is still UNVERIFIED", "never run on GitHub",
        "has not run (U14)", "the 1.2.1 floor has not", "the 1.2.1 floor itself has not", "has run on GitHub yet",
        "no hosted repository yet", "set up for CI but has not run")  # the sentences HD retired: each was true before 2026-11-12
NOT_SUBMITTED = "This repository has NOT been submitted to the Spec Kit community catalog."
NOT_IN_01 = ("comment approval hardening", "`skj strict override`", "single-item write", "`query`",
             "attachment download", "company-managed", "scoped API tokens", "Data Center", "`removed` state")
JARGON = re.compile(r"`|\b[A-Z]{1,3}\d+[a-z]?\b|\b[A-Z]{3,}\b|_")


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _base(version):
    return re.match(r"\d+\.\d+\.\d+", version).group(0)


def test_every_new_doc_exists_and_the_public_set_lists_the_two_docs_pages():
    assert all((ROOT / rel).is_file() for rel in NEW)
    assert {"docs/release-checklist.md", "docs/catalog-submission.md"} <= set(td.PUBLIC)


def _pep440(version):
    """The PEP 440 sort key of the forms in use (`X.Y.Z`, `X.Y.Z.devN`); any other form fails closed."""
    m = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.dev(0|[1-9]\d*))?", version)
    assert m, "unparseable version: %r" % version
    dev = m.group(4)
    return tuple(int(g) for g in m.groups()[:3]) + ((0, int(dev)) if dev is not None else (1, 0))  # X.Y.Z.devN < X.Y.Z


def test_pep440_order_of_the_forms_in_use():
    assert _pep440("0.1.0.dev0") < _pep440("0.1.0.dev1") < _pep440("0.1.0") < _pep440("0.1.1.dev0") < _pep440("0.10.0")
    for bad in ("0.1", "0.1.0-dev0", "0.1.0rc1", "v0.1.0", "01.0.0", "0.1.0.dev"):
        with pytest.raises(AssertionError, match="unparseable"):
            _pep440(bad)


def test_one_version_everywhere():
    heads = re.findall(r"^## \[([^\]]+)\](.*)$", _read("CHANGELOG.md"), re.M)
    assert heads == [("0.1.0", " - " + CHANGELOG_DATE)], heads  # no invented date
    pyproject = re.search(r'^version = "([^"]+)"$', _read("pyproject.toml"), re.M).group(1)
    ext = yaml.safe_load(_read("extension.yml"))["extension"]["version"]
    wf = yaml.safe_load(_read("workflows/jira-lifecycle/workflow.yml"))["workflow"]["version"]
    cat_ext = tc._entry("extensions.json", "extensions", "jira-lifecycle")["version"]
    cat_wf = tc._entry("workflows.json", "workflows", "jira-lifecycle")["version"]
    assert {heads[0][0], _base(pyproject), _base(skj.__version__), ext, wf, cat_ext, cat_wf} == {"0.1.0"}
    assert pyproject == skj.__version__
    tools = yaml.safe_load(_read("extension.yml"))["requires"]["tools"]
    cat_tools = tc._entry("extensions.json", "extensions", "jira-lifecycle")["requires"]["tools"]
    floors = [t["version"] for t in tools + cat_tools if t["name"] == "skj"]
    assert len(floors) == 2 and floors[0] == floors[1] and floors[0].startswith(">="), floors
    assert _pep440(floors[0][2:]) <= _pep440(pyproject), (floors[0], pyproject)  # T8: the package meets its own floor
    table = [r.strip("|").split("|")[0].strip() for r in _rows(_read("VERSIONING.md").split("## Where the version lives", 1)[1]
                                                                  .split("\n## ", 1)[0])]
    assert table[0] == "Place" and table[1:] == [
        "`pyproject.toml`", "`src/skj/__init__.py`", "`extension.yml`", "`workflows/jira-lifecycle/workflow.yml`",
        "`catalog/extensions.json` and `catalog/workflows.json`", "`bundle/bundle.yml`", "`CHANGELOG.md`", "`tests/test_package.py`",
        "`README.md`, `docs/quick-start.md` and `SECURITY.md`"], table
    today = {r.strip("|").split("|")[0].strip(): r.strip("|").split("|")[1].strip() for r in _rows(_read("VERSIONING.md"))[1:]}
    dev_places = ("`pyproject.toml`", "`src/skj/__init__.py`", "`tests/test_package.py`", "`README.md`, `docs/quick-start.md` and `SECURITY.md`")
    assert {k: v.strip("`") for k, v in today.items()} == {k: pyproject if k in dev_places else _base(pyproject) for k in today}, today
    for rel in ("README.md", "docs/quick-start.md", "SECURITY.md"):  # the text places the table names really carry the version
        assert pyproject in _read(rel), rel
    assert "skj %s" % pyproject in _read("tests/test_package.py")


def _rows(text):
    return [line for line in text.splitlines() if line.startswith("| ")]


# The release placeholders the checklist table records (REL1 replaced them; `<COMMIT-SHA>` waits for scripts/pin_release_sha.py).
RELEASE_PLACEHOLDERS = ("<REPO-URL>", "<TAG>", "<REPO-URL>/archive/refs/tags/<TAG>.zip",
                        "<REPO-URL>/releases/download/<TAG>/jira-lifecycle-workflow.zip", "<REPO-URL>/blob/<TAG>/CHANGELOG.md",
                        "<REPO-URL>/security/advisories/new", "<COMMIT-SHA>")
# Files that may name a placeholder as text: the checklist table, this file, the tests that read the install line, and the pin script.
PLACEHOLDER_TEXT = {CHECKLIST, "tests/test_release_docs.py", "tests/test_speckit_contract.py", "tests/test_github_workflows.py",
                    "tests/test_pin_release_sha.py", "scripts/pin_release_sha.py"}


def placeholders_left(files):
    """(path, placeholder) for each release placeholder left in `files` (rel -> text) outside PLACEHOLDER_TEXT. `<COMMIT-SHA>` may stay
    only inside the skj install line, `git+<REPO_URL>@<COMMIT-SHA>`, until the release commit is pinned."""
    install = "git+%s@<COMMIT-SHA>" % tc.REPO_URL
    return [(rel, ph) for rel, text in sorted(files.items()) if rel not in PLACEHOLDER_TEXT
            for ph in ("<REPO-URL>", "<TAG>", "<COMMIT-SHA>") if ph in text.replace(install, "")]


def test_rel1_no_release_placeholder_is_left():
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout: the tracked files cannot be listed")
    rels = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], env=env, capture_output=True, check=True).stdout.decode().split("\0")
    files = {rel: (ROOT / rel).read_text(encoding="utf-8", errors="replace") for rel in rels if rel and (ROOT / rel).is_file()}
    assert placeholders_left(files) == []
    install = 'pip install "git+%s@<COMMIT-SHA>"' % tc.REPO_URL
    assert placeholders_left({"README.md": install}) == [] and placeholders_left({"x.md": "git+<REPO-URL>@<COMMIT-SHA>"}) != []
    assert placeholders_left({"README.md": "see `<COMMIT-SHA>`"}) == [("README.md", "<COMMIT-SHA>")]  # prose: stale after the pin


def test_the_checklist_replaces_every_placeholder():
    checklist = _read(CHECKLIST)
    for ph in RELEASE_PLACEHOLDERS:
        rows = [r for r in _rows(checklist) if r.startswith("| `%s` |" % ph)]
        assert len(rows) == 1, ph
        cells = [c.strip() for c in rows[0].strip("|").split("|")]
        assert len(cells) == 3 and all(cells), (ph, cells)  # placeholder | where it is | what replaces it
    for action, (_sha, version) in ACTION_PINS.items():
        assert "`%s` (%s)" % (action, version) in checklist, action
    for words in ("tests/test_manifest.py", "tests/test_catalog.py", "README.md", "_placeholder_manifest", "sha256"):
        assert words in checklist, words


def _git_tags():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout: the no-tag fact cannot be read")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1"}
    out = subprocess.run(["git", "-C", str(ROOT), "tag", "-l"], env=env, capture_output=True, text=True, check=True)
    return out.stdout.split()


def test_hosted_ci_says_what_is_left_and_no_tag_exists():  # Z3-AC2, HD-AC1
    checklist = " ".join(_read(CHECKLIST).split())
    assert (X7_LEFT in checklist) is not HOSTED_CI_GREEN
    assert (NO_TAG in checklist) is not TAG_CREATED
    if not TAG_CREATED:
        assert _git_tags() == [], "a tag exists: flip TAG_CREATED and the checklist line on purpose"


def test_the_docs_say_what_really_ran_on_github():  # Z3-AC3, HD-AC1, HD-AC2
    for rel in (CHECKLIST, "CHANGELOG.md"):
        text = " ".join(_read(rel).split())
        assert HOSTED_RAN in text, rel
        assert "only Python 3.9.6 is installed" not in text, rel  # false: 3.13.14 is installed here
    tracked = subprocess.run(["git", "ls-files", "-z", "*.md"], cwd=str(ROOT), capture_output=True, check=True).stdout.decode()
    for rel in filter(None, tracked.split("\0")):
        text = " ".join(_read(rel).split())
        assert not [s for s in GONE if s in text], rel
    compat = " ".join(_read("docs/compatibility.md").split())
    assert "Python 3.9 and 3.13 on GitHub-hosted runners" in compat and "1.2.1 and the latest release on the hosted contract job" in compat
    u14 = [line for line in _read("docs/unverified.md").splitlines() if line.startswith("| U14 |")]
    assert len(u14) == 1 and all(w in u14[0] for w in ("ci/", "fork", "`skj` gate job", "scrub job", "version tag")), u14


# HD2, DF8: what the live checks LV1 to LV4 on one real site proved (2026-11), and what is still unproven
LIVE_RAN = ("About 600 live checks covered setup and doctor, get and comments (including more than 100 comments and pagination), the "
            "branch dry run, dod seed and refresh with acceptance-criteria drift, decisions, questions (posted once, never duplicated), "
            "push of subtasks, the gate, strict mode (its refusals and its success path, up to `done` closing a story), business rules "
            "on a real Epic parent chain, pr link, done with the ledger attachment, the Spec Kit add-on and its 13 commands, the bundle "
            "and a security sweep; the 23 defects they found are fixed and re-tested. The `jira-lifecycle` workflow, driven by Claude "
            "Code, ran end to end on a story, up to a pull request on a private GitHub repository; a rejected gate left Jira unchanged, "
            "and a second and third run added no duplicate comment or subtask.")
LIVE_LEFT = ("Not yet run live: comment approval by a second user and an edit by someone else (comment approval stays "
             "experimental), company-managed projects, real 429, 503, timeout and 403 answers, the token store on Linux and Windows, "
             "the copy-in GitHub checks in a repository of their own and a pull request from a fork, and `skj done` after a real "
             "merge with a decision a human validated.")
LIVE_GONE = ("Built and tested against a simulated Jira; the first live checks", "simulated Jira so far",
             "ran a few commands once", "Not yet run on a real site: comment approval, business rules",
             "LV3 and LV4 have not run", "for the first live checks", "simulated team-managed only",
             "not yet run against a real Jira site", "nothing has been run against a real Jira site", "no real site yet",
             "(LV1 to LV4) have not run", "Uploading and downloading attachments", "Not part of the live trial",
             "upload NEVER ran live", "Upload has NEVER run live", "has not been tried against a real item",
             "not yet tried against a real keychain item")  # each true before LV1, LV2


def test_the_docs_say_what_the_live_checks_proved():  # HD2-AC1, DF8
    readme = " ".join(_read("README.md").split("## Status", 1)[1].split("\n## ", 1)[0].split())
    changelog = " ".join(_read("CHANGELOG.md").split())
    for text in (readme, changelog):
        assert td.NOT_LIVE in text and LIVE_RAN in text and LIVE_LEFT in text
    checklist = " ".join(_read(CHECKLIST).split())
    assert all("- [x] LV%d:" % n in checklist for n in (1, 2, 3, 4)) and "- [x] Only after LV3 and LV4 pass:" in checklist
    assert "The live checks LV1 to LV4 ran on a real Jira Cloud Free site (2026-11)" in checklist
    u5 = [line for line in _read("docs/unverified.md").splitlines() if line.startswith("| U5 |")]
    assert len(u5) == 1 and "Settled (2026-11): an editor-typed `/approve D1` comment is stored as plain text." in u5[0], u5
    u6 = [line for line in _read("docs/unverified.md").splitlines() if line.startswith("| U6 |")]
    assert len(u6) == 1 and u6[0].startswith("| U6 | Downloading attachments, and opening an attached ledger in the browser, on a real "
                                             "site. |") and "Upload worked on a real site" in u6[0], u6
    compat = " ".join(_read("docs/compatibility.md").split())
    assert ("| Jira | Jira Cloud only | a simulated Jira, and one real Jira Cloud Free site (about 600 live checks and one workflow "
            "run end to end, 2026-11) |") in compat
    assert "| Jira project type | team-managed projects | a simulated team-managed project and one real one; company-managed not tried (U1) |" in compat
    assert "checked strictly and was read from a real keychain item once (one macOS version)" in compat
    tracked = subprocess.run(["git", "ls-files", "-z", "*.md", "tests/*.py"], cwd=str(ROOT), capture_output=True, check=True).stdout.decode()
    for rel in filter(None, tracked.split("\0")):
        if rel != "tests/test_release_docs.py":
            text = " ".join(_read(rel).split())
            assert not [s for s in LIVE_GONE if s in text], rel

def test_the_checklist_names_every_step_that_turns_the_suite_red_or_leaves_stale_text():
    checklist = " ".join(_read(CHECKLIST).split())
    for words in ("`tests/test_package.py`", "`HOSTED_CI_GREEN`", "`TAG_CREATED`", "`CHANGELOG_DATE`",
                  "`README.md`", "`docs/quick-start.md`", "`SECURITY.md`", "private vulnerability reporting",
                  "`CLAUDE.md`", "`Today` column of `VERSIONING.md`", "`tests/PORT_NOTES.md`"):
        assert words in checklist, words


def test_install_claims_match_the_installed_spec_kit():  # community catalogs are discovery only (1.2.4.dev0)
    page = " ".join(_read("docs/catalog-submission.md").split())
    assert "discovery only" in page and "--from" in page
    assert "would find this kit by name" not in page
    checklist = " ".join(_read(CHECKLIST).split())
    assert "`specify extension add --dev <unzipped dir>`" in checklist and "`specify workflow add <local workflow zip>`" in checklist
    changelog = " ".join(_read("CHANGELOG.md").split())
    assert "only required input is the issue number" in changelog and "only input is" not in changelog


# Fix-Docs: the findings of task-T5T2T8T7T6T9-review.md and task-Z3-rereview.md, each pinned so it cannot come back
PLACEHOLDER_LISTS = "`PLACEHOLDERS` in `tests/test_catalog.py`, `PLACEHOLDERS` and `SECURITY_PLACEHOLDERS` in `tests/test_docs.py`, " \
    "and `INSTALL_PLACEHOLDERS` in `tests/test_speckit_contract.py`"
VERDICT_INPUTS = "meant to be set only by `skj approvals sync --resume`"
SEMVER_PIN = "every version a bundle pins must be a semantic version, so `extension.yml` carries no `.dev0` either"
PORT_ROW = ("| release_checklist, catalog (fork group names) |", "| `test_catalog.py` (Z2), `test_release_docs.py` (Z3) |")


def release_docs_problems(docs):
    """Every Fix-Docs rule broken by these texts (rel -> text, whitespace as in the files), as short labels."""
    flat = {rel: " ".join(text.split()) for rel, text in docs.items()}
    bad = []
    if "which still calls this a private pre-release" in flat[CHECKLIST]:
        bad.append("stale security step")
    if " and and " in flat[CHECKLIST] or PLACEHOLDER_LISTS not in flat[CHECKLIST]:
        bad.append("placeholder lists")
    if "reaches the repository owner only" in flat["SECURITY.md"] or "reaches the repository's maintainers privately" not in flat["SECURITY.md"]:
        bad.append("security reach")
    if VERDICT_INPUTS not in flat["workflows/jira-lifecycle/CHANGELOG.md"] or "(set only by" in flat["workflows/jira-lifecycle/CHANGELOG.md"]:
        bad.append("verdict inputs")
    if "this gate ends the run" in flat["workflows/jira-lifecycle/README.md"]:
        bad.append("last gate")
    if "it parses an extension version under PEP 440" not in flat["VERSIONING.md"] or SEMVER_PIN not in flat["VERSIONING.md"]:
        bad.append("extension version")
    if "below 21.3" not in flat["CONTRIBUTING.md"] or "Do not install the project in editable mode" in flat["CONTRIBUTING.md"]:
        bad.append("editable")
    if "`specify workflow add jira-lifecycle --from" not in flat["docs/catalog-submission.md"]:
        bad.append("workflow --from")
    if not any(ln.startswith(PORT_ROW[0]) and ln.endswith(PORT_ROW[1]) for ln in docs["tests/PORT_NOTES.md"].splitlines()):
        bad.append("port row")
    scrub_box = flat[CHECKLIST].split("## 1. Before anything else", 1)[-1].split("- [ ]")[1]
    if not all(w in scrub_box for w in ("`SCRUB_DENYLIST` variable", "untracked `%s` file" % td._scrub().DENYLIST_FILE)):
        bad.append("scrub names")
    return bad


def test_the_release_docs_findings_stay_fixed():  # Fix-Docs (FD-AC1..AC3)
    rels = (CHECKLIST, "SECURITY.md", "workflows/jira-lifecycle/CHANGELOG.md", "workflows/jira-lifecycle/README.md", "VERSIONING.md",
            "CONTRIBUTING.md", "docs/catalog-submission.md", "tests/PORT_NOTES.md")
    docs = {rel: _read(rel) for rel in rels}
    assert release_docs_problems(docs) == []
    assert 'env.get("SCRUB_DENYLIST")' in _read("scripts/scrub_check.py")  # the variable the checklist names is the one read
    port = next(ln for ln in docs["tests/PORT_NOTES.md"].splitlines() if ln.startswith(PORT_ROW[0]))
    mutants = {
        "stale security step": (CHECKLIST, "## 4.", "- [ ] update its status section, which still calls this a private pre-release.\n\n## 4."),
        "placeholder lists": (CHECKLIST, "`SECURITY_PLACEHOLDERS` in", "in"),
        "security reach": ("SECURITY.md", "maintainers privately", "owner only"),
        "verdict inputs": ("workflows/jira-lifecycle/CHANGELOG.md", "meant to be set\n  only by", "set only by"),
        "last gate": ("workflows/jira-lifecycle/README.md", "this is the last gate", "this gate ends the run"),
        "extension version": ("VERSIONING.md", "carries no `.dev0` either", "carries the base version"),
        "editable": ("CONTRIBUTING.md", "below 21.3", "older"),
        "workflow --from": ("docs/catalog-submission.md", "`specify workflow add jira-lifecycle --from <workflow-url>`", "the same"),
        "port row": ("tests/PORT_NOTES.md", port + "\n", ""),
        "scrub names": (CHECKLIST, "in the `SCRUB_DENYLIST` variable", "in a variable"),
    }
    for label, (rel, old, new) in mutants.items():
        assert old in docs[rel], label
        assert label in release_docs_problems({**docs, rel: docs[rel].replace(old, new, 1)}), label


def test_contributing_states_the_cli_and_the_agent_rule_apart():
    text = " ".join(_read("CONTRIBUTING.md").split())
    assert "needs `--expect-plan`" not in text and "The apply carries `--expect-plan`" not in text
    assert "the agent always passes it" in text
    assert "pip install -e" not in text  # fails on the macOS system Python 3.9.6 (pip 21.2.4, no PEP 660)


def test_the_catalog_page_says_it_was_not_submitted_and_names_no_account():
    page = " ".join(_read("docs/catalog-submission.md").split())
    assert NOT_SUBMITTED in page and "the owner's personal GitHub account" in page and "UNVERIFIED" in page


def test_the_changelog_lists_what_is_not_in_0_1():
    text = _read("CHANGELOG.md")
    assert re.findall(r"^### (.*)$", text, re.M) == ["Added", "Known limits", "Not in 0.1"]
    not_in = " ".join(text.split("### Not in 0.1\n", 1)[1].split())
    missing = [w for w in NOT_IN_01 if w.lower() not in not_in.lower()]
    assert not missing, missing


def test_every_command_and_link_in_the_new_docs_resolves(capsys):  # the C2 helpers
    bad = []
    for rel in NEW:
        text = _read(rel)
        bad += ["%s: %s" % (rel, ln) for ln in td.unwrap(text).splitlines() if "skj strict override" in ln and "not in 0.1" not in ln]
        for raw, argv in tm.skj_commands(text):
            if argv[:2] == ["strict", "override"]:
                continue
            args = None if {"--help", "--version"} & set(argv) else tm._parse(argv)
            if not (td._names_a_command(argv) if args is None else
                    args.command and not (getattr(args, "expect_plan", None) and not args.apply)):
                bad.append("%s: skj %s" % (rel, raw))
        for target in td._LINK.findall(text):
            if not re.match(r"[a-z]+:", target) and not (ROOT / rel).parent.joinpath(target).resolve().exists():
                bad.append("%s: link %s" % (rel, target))
    capsys.readouterr()
    assert not bad, "\n".join(bad)


def test_no_fork_words_account_names_or_trademark_misuse_and_plain_headings():
    scrub = td._scrub()
    deny = scrub.load_denylist(os.environ, ROOT / scrub.DENYLIST_FILE)
    for rel in NEW:
        text = _read(rel)
        assert not [ln for ln in text.splitlines() if td.NO_ADO.search(ln) or tm.HTML.search(ln)], rel
        assert scrub.scan_text(rel, text, deny) == [], rel
        assert not re.search(r"praveenlavu|!\[|https?://", text.replace(tc.REPO_URL, ""), re.I), rel  # no account, image or URL but the repository (REL1)
        assert set(td._EMAIL.findall(text)) <= {"you@example.com"} and set(td._SITE.findall(text)) <= {"your-site.atlassian.net"}, rel
        assert td.NOT_AFFILIATED not in " ".join(text.split()) and "endorsed" not in text, rel  # X9: only in the README
        if rel != "CONTRIBUTING.md":
            assert "Atlassian" not in text, rel
        for heading in re.findall(r"^#+ (.*)", text, re.M):
            assert "Atlassian" not in heading and not re.search(r"(?<!for )(?<!with )(?<!in )Jira", heading), (rel, heading)
            assert not JARGON.search(heading), (rel, heading)
    for line in _read("CONTRIBUTING.md").splitlines():  # referential only: "Atlassian's" product, never a claim of a tie
        if "Atlassian" in line:
            assert re.search(r"Atlassian's (?:products|trademarks)", line), line


# ---- Fix-Final-Docs: the final branch review (I1, I2 half, I3, B2 line) ----------------------------------------------------------

HOSTED_README = "The kit's own workflows have run on GitHub; the GitHub checks in ci/ have not yet run in a repository of their own."
# T10 (owner ruling 2026-11-12): what was decided and done about the internal files and the history (checklist section 1)
T10_DECIDED = ("Decided by the owner (T10):", "the internal build files are out of the tree",
               "publication is from a clean export of one commit", "the local development history stays local and is never pushed",
               "`python3 scripts/scrub_check.py --history --require-denylist`", "in that export, before the first push")
# The internal build files T10 took out of the tree (kept only on the owner's disk). No tracked file may name one again.
INTERNAL = ("SPIKE-J0-BRIEF.md", "SPIKE-RESULTS.md", "spike/", "docs/plans/", "PLAN-0.1.md", "run_live.py", "spike row")


def final_review_problems(docs):
    """Every Fix-Final-Docs rule broken by these texts {rel: text}, as short labels (empty: all hold)."""
    bad = []
    checklist = " ".join(docs[CHECKLIST].split())
    section1 = checklist.split("## 1. ", 1)[-1].split("## 2. ", 1)[0]
    if not all(s in section1 for s in T10_DECIDED) or any(s in checklist for s in INTERNAL):
        bad.append("t10")
    if "working-name" in checklist:
        bad.append("manifest url")
    status = " ".join(docs["README.md"].split("## Status", 1)[-1].split("\n## ", 1)[0].split())
    works = status.split("**Works today:**", 1)[-1].split(" - **", 1)[0]
    if (HOSTED_README in works) is HOSTED_CI_GREEN or td.NOT_LIVE not in status:
        bad.append("hosted ci")
    if "the README status" not in checklist.split("## 5. ", 1)[-1]:
        bad.append("hosted ci flip")
    return bad


def test_the_final_review_findings_stay_fixed():
    docs = {rel: _read(rel) for rel in (CHECKLIST, "README.md")}
    assert final_review_problems(docs) == []
    mutants = {  # each mutant must break exactly the rule named
        "t10": (CHECKLIST, T10_DECIDED[3], "the history is filtered"),
        "manifest url": (CHECKLIST, "## 4. ", "The manifests hold a working-name URL.\n\n## 4. "),
        "hosted ci": ("README.md", HOSTED_README, ""),
        "hosted ci flip": (CHECKLIST, "the README status", "the docs"),
    }
    for label, (rel, old, new) in mutants.items():
        assert old in docs[rel] or old in " ".join(docs[rel].split()), label
        text = docs[rel] if old in docs[rel] else " ".join(docs[rel].split())
        assert final_review_problems({**docs, rel: text.replace(old, new, 1)}) == [label], label


def test_no_tracked_file_is_or_names_an_internal_file():  # T10-AC1, T10-AC2
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=str(ROOT), capture_output=True, check=True).stdout.decode().split("\0")
    assert not [rel for rel in tracked if rel.startswith(INTERNAL[:4])], "an internal file is tracked again"
    hits = []
    for rel in tracked:
        if rel and rel != "tests/test_release_docs.py" and (ROOT / rel).is_file():
            text = (ROOT / rel).read_bytes().decode(errors="replace")
            hits += ["%s: %s" % (rel, name) for name in INTERNAL if name in text]
    assert not hits, hits


def test_fru_ac1_the_checklist_says_strict_rules_needs_the_online_gate():  # Fix-RU, task-RU3-review.md finding 1
    checklist = " ".join(_read(CHECKLIST).split())
    assert td.RULES_ONLINE in checklist and td.RULES_ONLINE_WHY in checklist


# D-030: an installed project holds only the shipped files, so a relative link in one of them must point at another shipped file
WORKFLOW_DIR = "workflows/jira-lifecycle/"
LINK = re.compile(r"\]\(([^)\s]+)\)")


def unshipped_links(files, read):
    """(file, target) for each relative markdown link in a shipped file whose target its own archive does not hold."""
    bad = []
    for rel in files:
        if not rel.endswith(".md"):
            continue
        # the workflow zip holds only workflows/jira-lifecycle/, so its files must link inside it
        pool = [f for f in files if f.startswith(WORKFLOW_DIR)] if rel.startswith(WORKFLOW_DIR) else files
        for target in LINK.findall(read(rel)):
            if re.match(r"[a-z][a-z0-9+.-]*:|#|<", target, re.I):
                continue
            path = os.path.normpath(os.path.join(os.path.dirname(rel), target.split("#", 1)[0]))
            if path not in pool:
                bad.append((rel, target))
    return bad


def test_shipped_files_link_only_to_shipped_files():  # D-030
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout: the shipped file list cannot be read")
    files = tc._bundle_module().included_files(ROOT)
    assert "workflows/jira-lifecycle/README.md" in files and "rules/hitl.md" in files
    assert unshipped_links(files, _read) == []
    wf = "workflows/jira-lifecycle/README.md"  # mutant: a link back to the repository's docs
    assert unshipped_links(files, lambda r: _read(r) + ("\n[x](../../docs/ci.md)\n" if r == wf else "")) == [(wf, "../../docs/ci.md")]
