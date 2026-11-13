"""skj.config.json: the validator, the target-branch trust keys (X4) and the site pin (K7).

Rebased from the fork source's test_config.py: the trust-key, control-character and load_from_ref
cases keep their intent; organization became `site`, GUID approvers became accountIds.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from skj import config as config_mod
from skj.config import (
    REVIEW_SEVERITIES, STRICT_GATE_BLOCK, AuthRefused, Config, ConfigError, ConfigInvalid, Trust,
    require_site_pin, site_pin_ok, trusted,
)

ROOT = Path(__file__).resolve().parent.parent
SITE = "example-site.atlassian.net"
BASE = {"site": SITE, "project": "SPK"}
TRUST_BASE = BASE
A1, A2 = "5b10ac8d82e05b22cc7d4ef5", "712020:0e1f2a3b-4c5d-6e7f-8a9b-0c1d2e3f4a5b"  # invented accountIds
UNATTENDED_DEFAULT = {"assume_after_hours": None, "approval_requests": False}
_IDENTITY = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


# ---- git helpers: real temporary repositories, isolated from the user's git config ----
@pytest.fixture
def git_env(monkeypatch):
    """Isolate git from the user's config (signing, hooks) and give commits a fixed identity."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k, v in _IDENTITY.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    for k in ("CI", "GITHUB_ACTIONS"):  # the site pin reads these at call time; each test sets them explicitly
        monkeypatch.delenv(k, raising=False)


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True,
                          env={**os.environ, **_IDENTITY, "GIT_CONFIG_GLOBAL": os.devnull,
                               "GIT_CONFIG_NOSYSTEM": "1"}).stdout.strip()


def commit(root, msg, files=None):
    for rel, text in (files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)
    return git(root, "rev-parse", "--short", "HEAD")


def _write(tmp_path, **extra):
    (tmp_path / "skj.config.json").write_text(json.dumps({**BASE, **extra}))
    return Config.load(start_dir=tmp_path)


_load = _write


# ---- loading ----
def test_load_from_cwd_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    cfg = _write(tmp_path)
    assert (cfg.site, cfg.project) == (SITE, "SPK")


def test_env_var_path_wins(tmp_path, monkeypatch):
    env_file = tmp_path / "other.json"
    env_file.write_text(json.dumps({**BASE, "project": "ENV"}))
    (tmp_path / "skj.config.json").write_text(json.dumps({**BASE, "project": "CWD"}))
    monkeypatch.setenv("SKJ_CONFIG", str(env_file))
    assert Config.load(start_dir=tmp_path).project == "ENV"


@pytest.mark.parametrize("key", ["site", "project"])
def test_missing_required_key_raises(tmp_path, key):
    (tmp_path / "skj.config.json").write_text(json.dumps({k: v for k, v in BASE.items() if k != key}))
    with pytest.raises(ConfigError, match=key):
        Config.load(start_dir=tmp_path)


def test_no_config_anywhere_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    with pytest.raises(ConfigError, match="skj setup"):
        Config.load(start_dir=tmp_path)


def test_malformed_json_raises_config_error(tmp_path):
    (tmp_path / "skj.config.json").write_text("{not valid json")
    with pytest.raises(ConfigError, match="invalid JSON"):
        Config.load(start_dir=tmp_path)


def test_top_level_non_object_is_config_error(tmp_path):
    (tmp_path / "skj.config.json").write_text("[]")
    with pytest.raises(ConfigError, match="skj.config.json.*object"):
        Config.load(start_dir=tmp_path)


def test_unreadable_config_is_config_error_naming_file(tmp_path, monkeypatch):
    (tmp_path / "skj.config.json").write_text(json.dumps(BASE))
    monkeypatch.delenv("SKJ_CONFIG", raising=False)

    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    (tmp_path / "skj.config.json").chmod(0)  # R3b: the reader opens the file itself (no Path.read_text to replace)
    try:
        with pytest.raises(ConfigError, match="skj.config.json"):
            Config.load(start_dir=tmp_path)
    finally:
        (tmp_path / "skj.config.json").chmod(0o644)


def test_defaults(tmp_path):
    cfg = _write(tmp_path)
    assert (cfg.story_type, cfg.subtask_type, cfg.ac_heading) == ("Story", "Subtask", "Acceptance criteria")
    assert cfg.transitions == {"active": None, "done": None}
    assert (cfg.branch_prefix, cfg.labels, cfg.review_block_on, cfg.mode) == ("feature/", [], [], "advisory")
    assert cfg.gate == {"tests": {}, "block": []}
    assert cfg.strict == {"test_command": None, "test_timeout": 300}
    assert cfg.rules == {"enabled": False, "types": ["Epic"]}
    assert (cfg.approvers, cfg.approval, cfg.unattended) == ([], "terminal", UNATTENDED_DEFAULT)


def test_custom_values_load(tmp_path):
    cfg = _write(tmp_path, story_type="User story", subtask_type="Sub-task", ac_heading="Done when",
                 transitions={"active": "Start work", "done": "Close"}, branch_prefix="", labels=["speckit", "a.b_c-1"])
    assert (cfg.story_type, cfg.subtask_type, cfg.ac_heading, cfg.branch_prefix) == ("User story", "Sub-task", "Done when", "")
    assert cfg.transitions == {"active": "Start work", "done": "Close"} and cfg.labels == ["speckit", "a.b_c-1"]
    assert _write(tmp_path, transitions={"done": "Close"}).transitions == {"active": None, "done": "Close"}


def test_template_is_a_valid_fictional_config(monkeypatch):
    monkeypatch.setenv("SKJ_CONFIG", str(ROOT / "skj.config.template.json"))
    cfg = Config.load(start_dir=ROOT / "no-such-dir")
    assert (cfg.site, cfg.project) == (SITE, "SPK")
    data = json.loads((ROOT / "skj.config.template.json").read_text())
    assert not set(data) & set(config_mod.TRUST_KEYS)  # the trust keys are set on the target branch by a human


# ---- per-key validation ----
@pytest.mark.parametrize("site", [
    "example.com", "example-site.atlassian.net.evil.com", "evil.com/.atlassian.net", "Example-site.atlassian.net",
    "-x.atlassian.net", "atlassian.net", ".atlassian.net", "a.b.atlassian.net", "x.atlassian.net\n",
    "https://example-site.atlassian.net", "example-site.atlassian.net/", "a" * 64 + ".atlassian.net",
    "example-site.jira.com", 5, ["example-site.atlassian.net"], None, "",
])
def test_site_must_be_atlassian_net(tmp_path, site):
    with pytest.raises(ConfigInvalid, match="site") as e:
        _write(tmp_path, site=site)
    shown = str(e.value).split(": ", 1)[1].replace("<name>.atlassian.net", "")
    assert not (isinstance(site, str) and site and site in shown)


@pytest.mark.parametrize("site", ["example-site.atlassian.net", "a.atlassian.net", "a" * 63 + ".atlassian.net", "0-9.atlassian.net"])
def test_valid_site_accepted(tmp_path, site):
    assert _write(tmp_path, site=site).site == site


@pytest.mark.parametrize("project", ["spk", "S", "SPK-1", " SPK", "SPK ", "ABCDEFGHIJK", "1SP", 5, None, ""])
def test_bad_project_is_config_error(tmp_path, project):
    with pytest.raises(ConfigInvalid, match="'project'"):
        _write(tmp_path, project=project)


@pytest.mark.parametrize("key,cap", [("story_type", 64), ("subtask_type", 64), ("ac_heading", 200)])
def test_names_are_bounded_nonempty_strings(tmp_path, key, cap):
    assert getattr(_write(tmp_path, **{key: "x" * cap}), key) == "x" * cap
    for bad in ("", "   ", 3, None, ["x"], "x" * (cap + 1)):
        with pytest.raises(ConfigInvalid, match="'%s'" % key) as e:
            _write(tmp_path, **{key: bad})
        assert "x" * cap not in str(e.value)


def test_transitions_shape(tmp_path):
    for bad, where in (([], "'transitions' must be a JSON object"), ("Close", "'transitions' must be a JSON object"),
                       ({"active": ""}, "'transitions.active'"), ({"done": 5}, "'transitions.done'"),
                       ({"done": "x" * 101}, "'transitions.done'"), ({"done": True}, "'transitions.done'")):
        with pytest.raises(ConfigInvalid, match=where.replace(".", r"\.")):
            _write(tmp_path, transitions=bad)
    with pytest.raises(ConfigInvalid, match=r"transitions entry #2: unknown key \(valid: active, done\)") as e:
        _write(tmp_path, transitions={"done": "Close", "removed-SECRET": "x"})
    assert "SECRET" not in str(e.value)
    assert _write(tmp_path, transitions={"done": "x" * 100}).transitions["done"] == "x" * 100


@pytest.mark.parametrize("prefix", ["../x", "a..b", "a//b", "a/.b", "x.lock/", "-x", " x", "x" * 101, 5, None])
def test_bad_branch_prefix_is_config_error(tmp_path, prefix):
    with pytest.raises(ConfigInvalid, match="'branch_prefix'"):
        _write(tmp_path, branch_prefix=prefix)


@pytest.mark.parametrize("labels", ["speckit", ["Speckit"], ["a b"], ["-x"], [""], ["x" * 101], [5], ["ok", None],
                                    ["a\"b"], ["x"] * 21, {"a": 1}])
def test_bad_labels_are_config_error(tmp_path, labels):
    with pytest.raises(ConfigInvalid, match="labels"):
        _write(tmp_path, labels=labels)


def test_review_block_on_loaded(tmp_path):
    assert _write(tmp_path, review_block_on=["blocker", "major"]).review_block_on == ["blocker", "major"]


@pytest.mark.parametrize("bad", ["blocker", ["blocker", 1], {"a": 1}, None])
def test_review_block_on_must_be_list_of_strings(tmp_path, bad):
    with pytest.raises(ConfigError, match="review_block_on"):
        _write(tmp_path, review_block_on=bad)


@pytest.mark.parametrize("bad", ["critical", "Blocker", ""])
def test_unknown_review_block_on_severity_is_config_error_listing_valid_set(tmp_path, bad):
    with pytest.raises(ConfigError) as e:
        _write(tmp_path, review_block_on=["major", bad])
    assert "review_block_on[1]: unknown severity (valid: " in str(e.value)
    assert not bad or bad not in str(e.value)
    assert "(valid: blocker, major, minor)" in str(e.value)
    assert REVIEW_SEVERITIES == ("blocker", "major", "minor")


@pytest.mark.parametrize("key", ["gate", "stack", "rules", "unattended", "transitions", "strict"])
def test_non_object_sections_are_config_error(tmp_path, key):
    with pytest.raises(ConfigError, match=key):
        _write(tmp_path, **{key: None})


def test_gate_shape_and_entries_are_checked_by_the_gate_module(tmp_path):
    with pytest.raises(ConfigInvalid, match="gate.tests"):
        _write(tmp_path, gate={"tests": []})
    for block in ("assertion", [1], None, {"a": 1}):
        with pytest.raises(ConfigInvalid, match="gate.block"):
            _write(tmp_path, gate={"block": block})
    assert _write(tmp_path, gate={"block": []}).gate == {"tests": {}, "block": []}  # partial gate keeps defaults
    for gate in ({"block": ["assertion"]}, {"tests": {"*.py": "def test"}}):  # entries are checked by the gate module (L3)
        assert _write(tmp_path, gate=gate).gate == {"tests": {}, "block": [], **gate}
    with pytest.raises(ConfigInvalid, match="unknown check"):
        _write(tmp_path, gate={"block": ["nope"]})


def test_mode_rejects_unknown_value(tmp_path):
    for bad in ("Strict", "off", "", None, True, ["strict"]):
        with pytest.raises(ConfigInvalid, match="'mode'"):
            _load(tmp_path, mode=bad)
    assert _load(tmp_path, mode="strict").mode == "strict"


def test_strict_object_keys_validated(tmp_path):
    cfg = _load(tmp_path, strict={"test_command": "pytest -q", "test_timeout": 60})
    assert cfg.strict == {"test_command": "pytest -q", "test_timeout": 60}
    assert _load(tmp_path, strict={"test_timeout": 3600}).strict == {"test_command": None, "test_timeout": 3600}
    for bad in ([], "x", None, 5):
        with pytest.raises(ConfigInvalid, match="'strict' must be a JSON object"):
            _load(tmp_path, strict=bad)
    for strict, where in (({"test_command": ""}, "strict.test_command"), ({"test_command": 5}, "strict.test_command"),
                          ({"test_command": "x" * 501}, "strict.test_command"),
                          ({"test_timeout": 0}, "strict.test_timeout"), ({"test_timeout": 3601}, "strict.test_timeout"),
                          ({"test_timeout": "60"}, "strict.test_timeout"), ({"test_timeout": True}, "strict.test_timeout"),
                          ({"test_timeout": 1.5}, "strict.test_timeout")):
        with pytest.raises(ConfigInvalid, match=where.replace(".", r"\.")):
            _load(tmp_path, strict=strict)
    with pytest.raises(ConfigInvalid, match="control character"):
        _load(tmp_path, strict={"test_command": "pytest\n::error::x"})
    with pytest.raises(ConfigInvalid) as e:  # unknown key: named by position, never echoed
        _load(tmp_path, strict={"secret-key-name": 1})
    assert "strict entry #1" in str(e.value) and "secret-key-name" not in str(e.value)


def test_effective_lists_in_strict_union_project_and_defaults(tmp_path):
    cfg = _load(tmp_path, mode="strict", review_block_on=["major"])
    assert cfg.effective_review_block_on() == ["major", "blocker"]
    assert cfg.effective_gate_block() == list(STRICT_GATE_BLOCK)
    assert _load(tmp_path, review_block_on=["major"]).effective_review_block_on() == ["major"]
    cfg = _load(tmp_path, mode="advisory")
    assert cfg.effective_review_block_on() == [] and cfg.effective_gate_block() == []
    cfg.review_block_on.append("minor")  # the returned lists are copies
    assert cfg.effective_review_block_on() == ["minor"]
    cfg.effective_gate_block().append("x")
    assert cfg.effective_gate_block() == []


# ---- rules: accepted from RU1 on (off by default) ----
def test_rules_enabled_is_accepted(tmp_path, git_env):
    assert _load(tmp_path, rules={"enabled": True}).rules == {"enabled": True, "types": ["Epic"]}
    assert _load(tmp_path, rules={"enabled": False}).rules["enabled"] is False
    assert _load(tmp_path).rules["enabled"] is False  # the default
    repo = tmp_path / "r"
    repo.mkdir()
    _ref_repo(repo, json.dumps({**BASE, "rules": {"enabled": True, "types": ["Epic", "Initiative"]}}))  # and from the target branch
    assert Config.load_from_ref("refs/heads/main", repo=str(repo)).rules == {"enabled": True, "types": ["Epic", "Initiative"]}


def test_rules_rejects_non_bool_enabled(tmp_path):
    for bad in ("true", 1, 0, None, [True]):
        with pytest.raises(ConfigInvalid, match="'rules.enabled' must be true or false"):
            _load(tmp_path, rules={"enabled": bad})
    for bad in ([], "x", None, 5, True):
        with pytest.raises(ConfigInvalid, match="'rules' must be a JSON object"):
            _load(tmp_path, rules=bad)


def test_rules_types_shape(tmp_path):
    assert _load(tmp_path, rules={"types": ["A"] * 10}).rules["types"] == ["A"] * 10
    assert _load(tmp_path, rules={"types": ["x" * 64]}).rules["types"] == ["x" * 64]
    for bad in ([], ["A"] * 11, "Epic", None, [1], [""], ["x" * 65], ["Epic", None], {"a": "b"}):
        with pytest.raises(ConfigInvalid, match="'rules.types' must be a list of 1-10 non-empty strings of at most 64 characters") as e:
            _load(tmp_path, rules={"types": bad})
        assert "x" * 65 not in str(e.value)


def test_rules_unknown_key_named_by_position(tmp_path):
    with pytest.raises(ConfigInvalid, match=r"rules entry #2: unknown key \(valid: enabled, types\)") as e:
        _load(tmp_path, rules={"enabled": False, "secret-key-name": 1})
    assert "secret-key-name" not in str(e.value)


def test_rules_types_may_not_contain_story_type(tmp_path):
    for types, story_type in ((["Epic", "story"], None), (["STORY"], None), (["Bug"], "bug")):
        extra = {"rules": {"types": types}, **({"story_type": story_type} if story_type else {})}
        with pytest.raises(ConfigInvalid, match=r"rules\.types\[\d\]: a story type cannot carry rules") as e:
            _load(tmp_path, **extra)
        assert "Bug" not in str(e.value) and "story" not in str(e.value).lower().replace("story type", "").replace("story_type", "")
    assert _load(tmp_path, story_type="Bug", rules={"types": ["Story"]}).rules["types"] == ["Story"]


# ---- control characters are rejected in every string of the (PR-supplied) config ----
BAD_CHARS = {"newline": "\n", "nul": "\x00", "esc": "\x1b", "ls": "\u2028", "ps": "\u2029", "zwsp": "\u200b",
             "del": "\x7f", "c1": "\x85", "private": "\ue000", "rlo": "\u202e", "bidi-isolate": "\u2066",
             "bom": "\ufeff", "soft-hyphen": "\u00ad", "tag": "\U000e0041", "surrogate": "\ud800"}
# (config-with-a-hostile-hole, key path in the message); `{}` is replaced by "a<bad>b"
HOLES = [
    ({"site": "{}"}, "site"),
    ({"project": "{}"}, "project"),
    ({"story_type": "{}"}, "story_type"),
    ({"subtask_type": "{}"}, "subtask_type"),
    ({"ac_heading": "{}"}, "ac_heading"),
    ({"branch_prefix": "{}"}, "branch_prefix"),
    ({"transitions": {"active": "{}"}}, "transitions.active"),
    ({"transitions": {"done": "{}"}}, "transitions.done"),
    ({"labels": ["ok", "{}"]}, "labels[1]"),
    ({"approvers": ["{}"]}, "approvers[0]"),
    ({"approval": "{}"}, "approval"),
    ({"stack": {"phases": {"{}": "x"}}}, "stack.phases"),
    ({"stack": {"phases": {"plan": "{}"}}}, "stack.phases.<entry #1>"),
    ({"gate": {"tests": {"{}": "x"}}}, "gate.tests"),
    ({"gate": {"tests": {"src/**": "{}"}}}, "gate.tests.<entry #1>"),
    ({"{}": 1}, "top level"),
]


def _fill(node, bad):
    if isinstance(node, str):
        return node.replace("{}", "a" + bad + "b")
    if isinstance(node, dict):
        return {_fill(k, bad): _fill(v, bad) for k, v in node.items()}
    if isinstance(node, list):
        return [_fill(v, bad) for v in node]
    return node


@pytest.mark.parametrize("name", BAD_CHARS)
@pytest.mark.parametrize("hole,where", HOLES)
def test_control_character_in_any_config_string_is_rejected(tmp_path, hole, where, name):
    bad = BAD_CHARS[name]
    (tmp_path / "skj.config.json").write_text(json.dumps({**BASE, **_fill(hole, bad)}))
    with pytest.raises(ConfigInvalid) as e:
        Config.load(start_dir=tmp_path)
    msg = str(e.value)
    assert "control character" in msg and where in msg, msg
    assert "a" + bad + "b" not in msg and bad not in msg  # never echoes the offending value
    assert "src/**" not in msg and "phases.plan" not in msg  # nor a user-chosen key


def test_control_character_is_rejected_in_required_keys_and_nested_lists(tmp_path):
    for cfg in ({"site": "example-site.atlassian.net\n", "project": "SPK"},
                {**BASE, "gate": {"block": ["x\n"]}},
                {**BASE, "stack": {"phases": {"plan": ["ok", ["deep\u202e"]]}}}):
        (tmp_path / "skj.config.json").write_text(json.dumps(cfg))
        with pytest.raises(ConfigInvalid, match="control character"):
            Config.load(start_dir=tmp_path)


GOOD_STRINGS = ["Équipe", "Terminé", "日本語", "می\u200cخواهم", "क्\u200dष", "a\u200eb\u200fc", "a\u00a0b",
                "\U0001F600", "\U0001F468\u200d\U0001F469\u200d\U0001F467", "\u0378", "\U0001FAE8"]  # last two: Cn


@pytest.mark.parametrize("s", GOOD_STRINGS)
def test_ordinary_non_ascii_text_and_unassigned_code_points_stay_valid(tmp_path, s):
    (tmp_path / "skj.config.json").write_text(json.dumps(
        {**BASE, "story_type": s, "ac_heading": s, "transitions": {"done": s}, "stack": {"phases": {s + "/**": s}}},
        ensure_ascii=False), encoding="utf-8")
    cfg = Config.load(start_dir=tmp_path)
    assert cfg.story_type == s and cfg.ac_heading == s and cfg.transitions["done"] == s


# ---- Config.load_from_ref (the target branch's config, read with git, same validator) ----
def _ref_repo(tmp_path, cfg=json.dumps({**BASE, "mode": "strict"}), name="skj.config.json"):
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "base", {"README.md": "x\n", **({name: cfg} if cfg is not None else {})})
    return tmp_path


def test_load_from_ref_reads_the_file_at_the_ref_not_the_working_tree(tmp_path, git_env, monkeypatch):
    repo = _ref_repo(tmp_path)
    (repo / "skj.config.json").write_text(json.dumps({**BASE, "project": "OTHER"}))  # the working tree is the PR's: ignored
    monkeypatch.setenv("SKJ_CONFIG", str(repo / "skj.config.json"))
    cfg = Config.load_from_ref("refs/heads/main", repo=str(repo))
    assert (cfg.project, cfg.mode) == ("SPK", "strict")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    sha = git(repo, "rev-parse", "HEAD")
    for ref in ("origin/main", "main", sha):  # remote-tracking first, then a local branch, then a full commit id
        assert Config.load_from_ref(ref, repo=str(repo)).mode == "strict", ref
    assert Config.load_from_ref(sha, repo=str(repo)).source_commit == sha


def test_load_from_ref_validates_like_load(tmp_path, git_env):
    bad = ['{"site": "example-site.atlassian.net"}', "[1]", "{not json",
           '{"site": "example-site.atlassian.net", "project": "SPK", "mode": "x"}',
           '{"site": "example-site.atlassian.net", "project": "SPK", "approvers": ["a b"]}',
           '{"site": "example-site.atlassian.net", "project": "SPK", "story_type": "p\\u0007"}']
    for n, text in enumerate(bad):
        repo = tmp_path / str(n)
        repo.mkdir()
        _ref_repo(repo, text)
        with pytest.raises(ConfigInvalid) as e:
            Config.load_from_ref("refs/heads/main", repo=str(repo))
        (repo / "skj.config.json").write_text(text)  # the same text through Config.load gives the same complaint
        with pytest.raises(ConfigInvalid) as e2:
            Config.load(start_dir=repo)
        assert str(e.value).split(": ", 1)[1] == str(e2.value).split(": ", 1)[1]
        assert str(repo) not in str(e.value) and "\x07" not in str(e.value)


def test_load_from_ref_is_none_when_the_file_does_not_exist_at_the_ref(tmp_path, git_env):
    repo = _ref_repo(tmp_path, cfg=None)
    assert Config.load_from_ref("refs/heads/main", repo=str(repo)) is None
    repo2 = tmp_path / "r2"
    repo2.mkdir()
    _ref_repo(repo2, json.dumps(BASE), name="sub/skj.config.json")  # only a nested file
    assert Config.load_from_ref("refs/heads/main", repo=str(repo2)) is None


def test_load_from_ref_fails_closed(tmp_path, git_env, monkeypatch):
    repo = _ref_repo(tmp_path)
    git(repo, "tag", "v1")
    git(repo, "tag", "origin/main")  # a tag named like a remote-tracking branch is not the base
    for ref in ("", "nope", "origin/main", "v1", "refs/tags/v1", "--output=x", "-h", "--help", "HEAD", "main:skj.config.json",
                "refs/heads/nope", "abc123", "x" * 300):
        with pytest.raises(ConfigInvalid) as e:
            Config.load_from_ref(ref, repo=str(repo))
        assert ref not in str(e.value) or ref == ""
    assert not (repo / "x").exists()
    with pytest.raises(ConfigInvalid):  # not a repository at all
        Config.load_from_ref("refs/heads/main", repo=str(tmp_path / "missing"))
    monkeypatch.setenv("PATH", "")  # git cannot run
    with pytest.raises(ConfigInvalid):
        Config.load_from_ref("refs/heads/main", repo=str(repo))


def test_load_from_ref_a_missing_blob_is_a_git_failure_not_an_absent_file(tmp_path, git_env):
    repo = _ref_repo(tmp_path)
    blob = git(repo, "rev-parse", "main:skj.config.json")
    os.remove(repo / ".git" / "objects" / blob[:2] / blob[2:])
    with pytest.raises(ConfigInvalid):
        Config.load_from_ref("refs/heads/main", repo=str(repo))


def test_load_from_ref_refuses_a_big_file_a_symlink_and_a_directory(tmp_path, git_env):
    pad = " " * (1024 * 1024)
    repo = _ref_repo(tmp_path, json.dumps(BASE) + pad)  # 1 MiB + the object
    with pytest.raises(ConfigInvalid, match="larger than 1 MiB"):
        Config.load_from_ref("refs/heads/main", repo=str(repo))
    repo2 = tmp_path / "ok"
    repo2.mkdir()
    exact = json.dumps(BASE)
    _ref_repo(repo2, exact + " " * (1024 * 1024 - len(exact)))  # exactly 1 MiB is fine
    assert Config.load_from_ref("refs/heads/main", repo=str(repo2)).project == "SPK"
    repo3 = tmp_path / "link"
    repo3.mkdir()
    git(repo3, "init", "-q", "-b", "main")
    commit(repo3, "base", {"real.json": json.dumps(BASE)})
    os.symlink("real.json", repo3 / "skj.config.json")
    commit(repo3, "link")
    with pytest.raises(ConfigInvalid, match="not a regular file"):
        Config.load_from_ref("refs/heads/main", repo=str(repo3))
    repo4 = tmp_path / "dir"
    repo4.mkdir()
    git(repo4, "init", "-q", "-b", "main")
    commit(repo4, "base", {"skj.config.json/inner": "x"})
    with pytest.raises(ConfigInvalid):
        Config.load_from_ref("refs/heads/main", repo=str(repo4))


def test_load_from_ref_ignores_an_inherited_git_dir(tmp_path, git_env, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    _ref_repo(other, json.dumps({**BASE, "project": "WRONG"}))
    repo = tmp_path / "repo"
    repo.mkdir()
    _ref_repo(repo)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert Config.load_from_ref("refs/heads/main", repo=str(repo)).project == "SPK"


def test_git_runs_with_list_argv_and_without_the_credentials(tmp_path, git_env, monkeypatch):
    repo = _ref_repo(tmp_path)
    monkeypatch.setenv("SKJ_TOKEN", "tok-SECRET")
    monkeypatch.setenv("SKJ_EMAIL", "dev@example.com")
    monkeypatch.setenv("OTHER_TOKEN", "tok-OTHER")
    seen = []
    real = subprocess.run

    def spy(argv, *a, **kw):
        seen.append((argv, kw))
        return real(argv, *a, **kw)
    monkeypatch.setattr(config_mod.subprocess, "run", spy)
    assert Config.load_from_ref("refs/heads/main", repo=str(repo)).project == "SPK"
    assert seen
    for argv, kw in seen:
        assert isinstance(argv, list) and argv[0] == "git" and not kw.get("shell")
        env = kw["env"]
        assert "SKJ_TOKEN" not in env and "SKJ_EMAIL" not in env and "OTHER_TOKEN" not in env
        assert "tok-SECRET" not in " ".join(argv)


def test_deeply_nested_config_is_invalid_not_a_crash(tmp_path, git_env):
    """A nested value that the parser or the validator cannot walk is ConfigInvalid, never an uncaught RecursionError."""
    refused = 0
    for depth in (200, 500, 900, 5000, 200000):  # the shallow ones are valid configs (config does not check what `stack` holds)
        text = json.dumps(BASE)[:-1] + ', "stack": {"x": ' + "[" * depth + "]" * depth + "}}"
        repo = tmp_path / ("d%d" % depth)
        repo.mkdir()
        _ref_repo(repo, text)
        (repo / "skj.config.json").write_text(text)
        for load in (lambda: Config.load_from_ref("refs/heads/main", repo=str(repo)), lambda: Config.load(start_dir=repo)):
            try:
                load()
            except ConfigInvalid as e:
                refused += 1
                assert "[[[" not in str(e)
    assert refused >= 4  # at least the 5000 and 200000 levels, through both loaders


# ---- the trust keys `approvers`, `approval`, `unattended` and `trusted` ----
def test_trust_keys_default_when_absent(tmp_path):
    cfg = _load(tmp_path)
    assert (cfg.approvers, cfg.approval, cfg.unattended) == ([], "terminal", UNATTENDED_DEFAULT)
    assert _load(tmp_path, unattended={}).unattended == UNATTENDED_DEFAULT and _load(tmp_path, unattended={}).unattended is not UNATTENDED_DEFAULT
    full = _load(tmp_path, approvers=[A1, A2], approval="comment", unattended={"assume_after_hours": 720, "approval_requests": True})
    assert (full.approvers, full.approval, full.unattended) == ([A1, A2], "comment", {"assume_after_hours": 720, "approval_requests": True})
    assert len(_load(tmp_path, approvers=["acct%02d" % i for i in range(1, 21)]).approvers) == 20  # 20 is the cap
    assert _load(tmp_path, unattended={"assume_after_hours": 1}).unattended == {"assume_after_hours": 1, "approval_requests": False}


@pytest.mark.parametrize("extra, where", [
    ({"approvers": "SECRETVALUE"}, "'approvers' must be a list"),
    ({"approvers": {"a": 1}}, "'approvers' must be a list"),
    ({"approvers": ["acct%02d" % i for i in range(1, 22)]}, "'approvers' must be a list of at most 20"),  # 21 entries
    ({"approvers": [A1, "SECRET VALUE"]}, "approvers[1]: not a Jira accountId"),
    ({"approvers": [A1, 5]}, "approvers[1]: not a Jira accountId"),
    ({"approvers": ["{" + A1 + "}"]}, "approvers[0]: not a Jira accountId"),
    ({"approvers": [" " + A1]}, "approvers[0]: not a Jira accountId"),
    ({"approvers": [A1 + "\n"]}, "approvers[0]"),
    ({"approvers": [A1, A2, A1]}, "approvers[2]: duplicate of an earlier entry"),  # duplicates are rejected, not collapsed
    ({"approval": "SECRETVALUE"}, "'approval' must be \"terminal\" or \"comment\""),
    ({"approval": "Comment"}, "'approval' must be"),
    ({"approval": True}, "'approval' must be"),
    ({"approval": None}, "'approval' must be"),
    ({"unattended": "SECRETVALUE"}, "'unattended' must be a JSON object"),
    ({"unattended": [1]}, "'unattended' must be a JSON object"),
    ({"unattended": {"SECRETVALUE": 1}}, "unattended entry #1: unknown key"),
    ({"unattended": {"assume_after_hours": 0}}, "'unattended.assume_after_hours' must be an integer from 1 to 720"),
    ({"unattended": {"assume_after_hours": 721}}, "'unattended.assume_after_hours' must be an integer from 1 to 720"),
    ({"unattended": {"assume_after_hours": True}}, "'unattended.assume_after_hours'"),
    ({"unattended": {"assume_after_hours": "5"}}, "'unattended.assume_after_hours'"),
    ({"unattended": {"assume_after_hours": 1.5}}, "'unattended.assume_after_hours'"),
    ({"unattended": {"assume_after_hours": None}}, "'unattended.assume_after_hours'"),
    ({"unattended": {"approval_requests": 1}}, "'unattended.approval_requests' must be true or false"),
    ({"unattended": {"approval_requests": "yes"}}, "'unattended.approval_requests' must be true or false"),
    ({"unattended": {"approval_requests": "true"}}, "'unattended.approval_requests' must be true or false"),
])
def test_trust_keys_validated(tmp_path, extra, where):
    with pytest.raises(ConfigInvalid) as e:
        _load(tmp_path, **extra)
    assert where in str(e.value)
    for secret in ("SECRETVALUE", "SECRET VALUE", A1, A2, "acct"):
        assert secret not in str(e.value), "a value was echoed"


@pytest.mark.parametrize("acct", [
    "11111111-2222-3333-4444-555555555555x!", "user@example.com", "a b", "a;b", "a/b", "", "x" * 129, "été",
    "5b10ac8d82e05b22cc7d4ef5 ", "{5b10ac8d82e05b22cc7d4ef5}", 5, None, ["x"],
])
def test_approver_must_be_account_id(tmp_path, acct):
    with pytest.raises(ConfigInvalid, match=r"approvers\[0\]") as e:
        _load(tmp_path, approvers=[acct])
    assert not (isinstance(acct, str) and acct and acct in str(e.value))
    for ok in (A1, A2, "x" * 128, "qm:1a2b3c:4d5e"):
        assert _load(tmp_path, approvers=[ok]).approvers == [ok]  # kept exactly: no lower-casing
    assert not hasattr(config_mod, "GUID_RE")  # no GUID path left
    assert "GUID" not in (ROOT / "src" / "skj" / "config.py").read_text()


def test_trust_keys_validated_from_the_target_branch_too(tmp_path, git_env):
    repo = _ref_repo(tmp_path, json.dumps({**TRUST_BASE, "approvers": [A1, "SECRET VALUE"]}))
    with pytest.raises(ConfigInvalid, match=r"approvers\[1\]: not a Jira accountId") as e:
        Config.load_from_ref("refs/heads/main", repo=str(repo))
    assert "SECRET" not in str(e.value)


def _trust_repo(tmp_path, target, head=None, origin=True):
    """A repository whose `main` holds `target` as skj.config.json; the working tree gets `head` (uncommitted) when given."""
    repo = _ref_repo(tmp_path, None if target is None else json.dumps(target))
    if origin:
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    if head is not None:
        (repo / "skj.config.json").write_text(head if isinstance(head, str) else json.dumps(head))
    return repo


def test_trusted_reads_the_target_branch(tmp_path, git_env):
    repo = _trust_repo(tmp_path, {**TRUST_BASE, "approvers": [A1], "approval": "comment", "unattended": {"assume_after_hours": 24, "approval_requests": True}})
    t = trusted(str(repo))
    assert (t.approvers, t.approval, t.unattended, t.site) == ([A1], "comment", {"assume_after_hours": 24, "approval_requests": True}, SITE)
    assert trusted(str(repo), base="refs/heads/main").approvers == [A1]  # an explicit base (CI passes --base)
    assert trusted(str(repo), base="origin/main").approvers == [A1]
    assert Config.trusted(str(repo), "origin/main") == trusted(str(repo), base="origin/main")


def test_trusted_ignores_working_tree_edit(tmp_path, git_env):
    # the PR (working tree) adds an approver, switches to comment mode, an unattended window and another site: the target has none of it
    repo = _trust_repo(tmp_path, TRUST_BASE, {**TRUST_BASE, "site": "attacker.atlassian.net", "approvers": [A2],
                                              "approval": "comment", "unattended": {"assume_after_hours": 1}})
    t = trusted(str(repo))
    assert (t.approvers, t.approval, t.unattended, t.site, t.base_known) == ([], "terminal", UNATTENDED_DEFAULT, SITE, True)
    # and the other way round: removing the target's approvers in the PR changes nothing
    (tmp_path / "two").mkdir()
    repo2 = _trust_repo(tmp_path / "two", {**TRUST_BASE, "approvers": [A1], "approval": "comment"}, TRUST_BASE)
    t = trusted(str(repo2))
    assert (t.approvers, t.approval) == ([A1], "comment")
    (repo2 / "skj.config.json").write_text("{ not json")  # an unparsable working tree is not read either
    assert trusted(str(repo2)).approvers == [A1]


def test_a_head_branch_that_adds_approvers_is_not_believed(tmp_path, git_env):
    repo = _trust_repo(tmp_path, TRUST_BASE)
    git(repo, "checkout", "-q", "-b", "feature/SPK-1-x")
    commit(repo, "add approvers", {"skj.config.json": json.dumps({**TRUST_BASE, "approvers": [A2], "approval": "comment"})})
    assert Config.load(start_dir=repo).approvers == [A2]  # the head's own file says so...
    t = trusted(str(repo))
    assert (t.approvers, t.approval) == ([], "terminal")  # ...but only the target branch is believed
    assert trusted(str(repo), base="main").approvers == []


def test_trusted_unreadable_target_refuses(tmp_path, git_env):
    repo = _trust_repo(tmp_path, TRUST_BASE)
    git(repo, "commit", "-q", "--allow-empty", "-m", "second")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")  # origin/main has a reflog entry and a parent now
    for base in ("origin/nope", "refs/heads/nope", "not a ref", "--upload-pack=x", "main@{0}", "origin/main@{1}", "origin/main~0", "origin/main~1",
                 "origin/main^", "origin/main^0", "origin/main^{commit}", "origin/main:skj.config.json", "main..origin/main", "origin/main\n", " origin/main",
                 "origin/ main", "origin/main\t", "-x", "@{-1}", "HEAD~1"):
        with pytest.raises(ConfigInvalid) as e:
            trusted(str(repo), base=base)
        assert base.strip() not in str(e.value) or base.strip() == "", base  # fixed text: the base is never echoed
    assert trusted(str(repo), base="origin/main").approvers == []  # the plain names still work
    assert trusted(str(repo), base="refs/remotes/origin/main").approvers == []
    (tmp_path / "bad").mkdir()
    bad = _trust_repo(tmp_path / "bad", {**TRUST_BASE, "approvers": "SECRETVALUE"})  # invalid at the target
    with pytest.raises(ConfigInvalid) as e:
        trusted(str(bad))
    assert "SECRETVALUE" not in str(e.value)
    (tmp_path / "junk").mkdir()
    junk = _ref_repo(tmp_path / "junk", "{ not json")
    git(junk, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(junk, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    with pytest.raises(ConfigInvalid):
        trusted(str(junk))
    (tmp_path / "dangling").mkdir()
    dangling = _ref_repo(tmp_path / "dangling")
    git(dangling, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")  # origin/HEAD points nowhere
    with pytest.raises(ConfigInvalid):
        trusted(str(dangling))
    with pytest.raises(ConfigInvalid):
        trusted(str(tmp_path / "missing-dir"), base="origin/main")


def test_trusted_returns_only_the_trust_keys_and_the_site(tmp_path, git_env):
    t = trusted(str(_trust_repo(tmp_path, {**TRUST_BASE, "approvers": [A1]})))
    assert isinstance(t, Trust) and t._fields == ("approvers", "approval", "unattended", "site", "base_known") and t.approvers == [A1]
    assert not hasattr(t, "project") and not hasattr(t, "mode")
    assert config_mod.TRUST_KEYS == ("approvers", "approval", "unattended")


def test_trusted_target_without_a_config_is_the_defaults(tmp_path, git_env):
    repo = _trust_repo(tmp_path, None, {**TRUST_BASE, "approvers": [A1], "approval": "comment"})  # the PR adds the config: it is not trusted
    t = trusted(str(repo))
    assert (t.approvers, t.approval, t.unattended, t.site, t.base_known) == ([], "terminal", UNATTENDED_DEFAULT, None, True)


def test_no_base_no_trust_keys_defaults(tmp_path, git_env):
    repo = _trust_repo(tmp_path, TRUST_BASE, origin=False)  # a fresh repository: no origin/HEAD
    t = trusted(str(repo))
    assert (t.approvers, t.approval, t.unattended, t.site, t.base_known) == ([], "terminal", UNATTENDED_DEFAULT, None, False)
    (tmp_path / "none").mkdir()
    assert trusted(str(tmp_path / "none")).approval == "terminal"  # not even a config file


@pytest.mark.parametrize("extra", [{"approvers": []}, {"approvers": [A1]}, {"approval": "terminal"}, {"approval": "comment"},
                                   {"unattended": {}}, {"unattended": {"approval_requests": False}}])
def test_no_base_with_trust_keys_refuses(tmp_path, git_env, monkeypatch, extra):
    repo = _trust_repo(tmp_path, TRUST_BASE, {**TRUST_BASE, **extra}, origin=False)
    with pytest.raises(ConfigInvalid, match="origin/HEAD or pass --base") as e:
        trusted(str(repo))
    assert A1 not in str(e.value)
    (repo / "skj.config.json").write_text("{ not json")  # cannot tell whether it holds a trust key: refuse
    with pytest.raises(ConfigInvalid):
        trusted(str(repo))
    elsewhere = tmp_path / "elsewhere.json"  # a config named by $SKJ_CONFIG counts too
    elsewhere.write_text(json.dumps({**TRUST_BASE, **extra}))
    (repo / "skj.config.json").write_text(json.dumps(TRUST_BASE))
    monkeypatch.setenv("SKJ_CONFIG", str(elsewhere))
    with pytest.raises(ConfigInvalid, match="not the repo-root skj.config.json"):  # a non-root file with a trust key: its own text, base or not
        trusted(str(repo))


def test_a_non_root_config_with_a_trust_key_is_refused_with_a_base_too(tmp_path, git_env, monkeypatch):
    repo = _trust_repo(tmp_path, {**TRUST_BASE, "approvers": [A1]})
    p = repo / "config" / "skj.json"
    p.parent.mkdir()
    p.write_text(json.dumps({**TRUST_BASE, "approval": "comment"}))
    monkeypatch.setenv("SKJ_CONFIG", str(p))
    for base in (None, "origin/main"):
        with pytest.raises(ConfigInvalid) as e:
            trusted(str(repo), base=base)
        assert str(e.value) == config_mod.E_NONROOT_TRUST
    p.write_text(json.dumps(TRUST_BASE))  # without trust keys the non-root file is only the rest of the config
    assert trusted(str(repo)).approvers == [A1]
    monkeypatch.setenv("SKJ_CONFIG", str(repo / "config" / ".." / "skj.config.json"))  # the root file by another spelling
    (repo / "skj.config.json").write_text(json.dumps({**TRUST_BASE, "approvers": [A2]}))
    assert trusted(str(repo)).approvers == [A1]


def test_no_base_reads_a_symlinked_working_tree_config_only_to_look_for_the_keys(tmp_path, git_env):
    repo = _trust_repo(tmp_path, TRUST_BASE, origin=False)
    real = tmp_path / "real.json"
    real.write_text(json.dumps(TRUST_BASE))
    (repo / "skj.config.json").unlink()
    (repo / "skj.config.json").symlink_to(real)
    with pytest.raises(ConfigInvalid, match="not a regular file"):  # R3b: Config.load refuses a link; trusted only searches it for the keys
        Config.load(start_dir=repo)
    assert trusted(str(repo)).approval == "terminal"
    real.write_text(json.dumps({**TRUST_BASE, "approvers": [A1]}))  # keys behind the link and no base: still refused
    with pytest.raises(ConfigInvalid, match="origin/HEAD or pass --base"):
        trusted(str(repo))
    (repo / "skj.config.json").unlink()
    (repo / "skj.config.json").symlink_to(tmp_path / "missing.json")  # a dangling link is no file
    assert trusted(str(repo)).approvers == []


def test_no_base_refuses_a_huge_or_unreadable_working_tree_config(tmp_path, git_env):
    repo = _trust_repo(tmp_path, TRUST_BASE, origin=False)
    (repo / "skj.config.json").write_text(json.dumps({**TRUST_BASE, "pad": "x" * (1024 * 1024)}))
    with pytest.raises(ConfigInvalid):
        trusted(str(repo))
    (repo / "skj.config.json").write_text(json.dumps({**TRUST_BASE, "pad": "x" * 1000}))  # a big one under the cap is read
    assert trusted(str(repo)).approvers == []
    for raw in (b"[1]", b"\xff\xfe"):
        (repo / "skj.config.json").write_bytes(raw)
        with pytest.raises(ConfigInvalid):
            trusted(str(repo))


def test_no_base_refusal_texts_are_fixed_and_name_no_file(tmp_path, git_env, monkeypatch):
    elsewhere = tmp_path / "SECRETNAME.json"
    elsewhere.write_text(json.dumps({**TRUST_BASE, "approvers": [A1]}))
    (tmp_path / "r").mkdir()
    repo = _trust_repo(tmp_path / "r", TRUST_BASE, origin=False)
    monkeypatch.setenv("SKJ_CONFIG", str(elsewhere))
    with pytest.raises(ConfigInvalid) as e:
        trusted(str(repo))
    assert "not the repo-root skj.config.json" in str(e.value) and "SECRETNAME" not in str(e.value)
    elsewhere.write_text("{ not json")
    with pytest.raises(ConfigInvalid) as e:
        trusted(str(repo))
    assert "the config file cannot be read" in str(e.value) and "skj.config.json" not in str(e.value) and "SECRETNAME" not in str(e.value)


# ---- K7: the site pin ----
def test_site_pin_mismatch_refuses(tmp_path, git_env, monkeypatch):
    repo = _trust_repo(tmp_path, TRUST_BASE, {**TRUST_BASE, "site": "attacker.atlassian.net"})  # the branch repoints the site
    cfg, t = Config.load(start_dir=repo), trusted(str(repo))
    assert (cfg.site, t.site) == ("attacker.atlassian.net", SITE)
    assert site_pin_ok(cfg, t) is False
    with pytest.raises(AuthRefused, match="site") as e:
        require_site_pin(cfg, t)
    assert "attacker" not in str(e.value) and SITE not in str(e.value)
    from skj import client as client_mod
    built = []
    monkeypatch.setattr(client_mod, "Client", lambda *a, **k: built.append(a))
    with pytest.raises(AuthRefused):
        config_mod.build_client(cfg, t, "dev@example.com", "tok-not-real")
    assert built == []  # the client is never built, so no credential is attached
    same = Config.load(start_dir=repo)
    same.site = SITE
    assert site_pin_ok(same, t) is True


def test_site_pin_no_target_allows(tmp_path, git_env, monkeypatch):
    repo = _trust_repo(tmp_path, None, {**TRUST_BASE, "site": "other-site.atlassian.net"})  # the target has no config yet
    cfg, t = Config.load(start_dir=repo), trusted(str(repo))
    assert (t.site, t.base_known) == (None, True)
    assert site_pin_ok(cfg, t, ci=True) is True and config_mod.pin_status(cfg, t, ci=True) == "no-target-config"
    require_site_pin(cfg, t)  # no raise
    from skj import client as client_mod
    built = []
    monkeypatch.setattr(client_mod, "Client", lambda *a, **k: built.append(a) or "client")
    assert config_mod.build_client(cfg, t, "dev@example.com", "tok-not-real") == "client"
    assert built == [("other-site.atlassian.net", "dev@example.com", "tok-not-real")]
    (tmp_path / "same").mkdir()
    repo2 = _trust_repo(tmp_path / "same", TRUST_BASE, TRUST_BASE)  # the same site on both sides
    assert site_pin_ok(Config.load(start_dir=repo2), trusted(str(repo2))) is True


def test_site_pin_refuses_anything_but_a_trust_with_a_matching_site():
    cfg = Config(SITE, "SPK")
    for bad in (None, {"site": SITE}, (SITE,)):
        assert site_pin_ok(cfg, bad) is False
    for ci in (False, True):
        assert site_pin_ok(cfg, Trust([], "terminal", dict(UNATTENDED_DEFAULT), SITE, True), ci=ci) is True
        assert site_pin_ok(cfg, Trust([], "terminal", dict(UNATTENDED_DEFAULT), SITE.upper(), True), ci=ci) is False  # no case folding
        assert site_pin_ok(cfg, Trust([], "terminal", dict(UNATTENDED_DEFAULT), SITE, "yes"), ci=ci) is False  # base_known is a strict bool


# ---- K7: the three states, and an unknown base ----
def _trust_of(site, base_known):
    return Trust([], "terminal", dict(UNATTENDED_DEFAULT), site, base_known)


def test_pin_status_names_every_state():
    cfg = Config(SITE, "SPK")
    cases = [((SITE, True), False, "ok"), ((SITE, True), True, "ok"),
             (("other.atlassian.net", True), False, "mismatch"), (("other.atlassian.net", True), True, "mismatch"),
             ((None, True), False, "no-target-config"), ((None, True), True, "no-target-config"),
             ((None, False), False, "no-base"), ((None, False), True, "refused-ci-no-base")]
    for (site, known), ci, status in cases:
        t = _trust_of(site, known)
        assert config_mod.pin_status(cfg, t, ci=ci) == status, (site, known, ci)
        assert site_pin_ok(cfg, t, ci=ci) is (status in config_mod.PIN_ALLOWED)
    assert config_mod.PIN_ALLOWED == ("ok", "no-target-config", "no-base")
    assert config_mod.pin_status(cfg, None, ci=False) == "invalid" and site_pin_ok(cfg, None, ci=False) is False


@pytest.mark.parametrize("env,ci", [({}, False), ({"CI": ""}, False), ({"CI": "0"}, False), ({"CI": "false"}, False),
                                    ({"CI": "FALSE"}, False), ({"GITHUB_ACTIONS": "False"}, False), ({"CI": "true"}, True),
                                    ({"CI": "1"}, True), ({"GITHUB_ACTIONS": "true"}, True), ({"CI": "0", "GITHUB_ACTIONS": "true"}, True)])
def test_in_ci_reads_ci_and_github_actions(env, ci):
    assert config_mod.in_ci(env) is ci


def _unknown_base_repo(tmp_path):
    """The reviewer's reproduction: main pins SITE, the working tree points at another site, and no base can be resolved."""
    return _trust_repo(tmp_path, TRUST_BASE, {**TRUST_BASE, "site": "attacker.atlassian.net"}, origin=False)


def test_unknown_base_in_ci_refuses_and_never_builds_the_client(tmp_path, git_env, monkeypatch):
    repo = _unknown_base_repo(tmp_path)
    cfg, t = Config.load(start_dir=repo), trusted(str(repo))
    assert (t.site, t.base_known) == (None, False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")  # read at call time, not at import
    assert config_mod.pin_status(cfg, t) == "refused-ci-no-base" and site_pin_ok(cfg, t) is False
    from skj import client as client_mod
    built = []
    monkeypatch.setattr(client_mod, "Client", lambda *a, **k: built.append(a))
    with pytest.raises(AuthRefused, match="no target branch") as e:
        config_mod.build_client(cfg, t, "dev@example.com", "tok-not-real")
    assert "attacker" not in str(e.value) and SITE not in str(e.value)
    with pytest.raises(AuthRefused):
        require_site_pin(cfg, t, ci=True)
    assert built == []


def test_unknown_base_locally_is_allowed_with_status_no_base(tmp_path, git_env, monkeypatch):
    repo = _unknown_base_repo(tmp_path)
    cfg, t = Config.load(start_dir=repo), trusted(str(repo))
    assert config_mod.pin_status(cfg, t) == "no-base" and site_pin_ok(cfg, t) is True
    from skj import client as client_mod
    monkeypatch.setattr(client_mod, "Client", lambda *a, **k: "client")
    assert config_mod.build_client(cfg, t, "dev@example.com", "tok-not-real", ci=False) == "client"


def test_known_base_states_do_not_depend_on_ci(tmp_path, git_env):
    for n, (target, head, status) in enumerate(((TRUST_BASE, {**TRUST_BASE, "site": "attacker.atlassian.net"}, "mismatch"),
                                                 (TRUST_BASE, TRUST_BASE, "ok"), (None, TRUST_BASE, "no-target-config"))):
        (tmp_path / str(n)).mkdir()
        repo = _trust_repo(tmp_path / str(n), target, head)
        cfg, t = Config.load(start_dir=repo), trusted(str(repo))
        assert t.base_known is True
        assert config_mod.pin_status(cfg, t, ci=True) == config_mod.pin_status(cfg, t, ci=False) == status
        assert trusted(str(repo), base="main").base_known is True


# ---- R3b: every reader of config text is bounded (size in bytes, integer digits, nesting, file type) ----
import signal
import stat
import time

_MIB = 1 << 20
_BOUND = 2.0  # seconds per call at 1 MiB; the bounded readers take well under 0.3 s, json's int() on 1M digits took 4.4 s
needs_timer = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="signal.setitimer is not available on this platform")
_HEAD = json.dumps(BASE)[:-1] + ', "x": '


class _TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise _TooSlow()


def _timed(fn, *args):
    """Seconds fn(*args) took; _BOUND when it was cut off there (a quadratic reader fails the assertion, never hangs the suite).
    ConfigError (a refusal) is part of the contract here."""
    # CPU time, not the wall clock: load slows the clock, not the work; a wall-clock backstop at 10x still cuts off a call blocked on a FIFO or a child
    old, old_real = signal.signal(signal.SIGPROF, _alarm), signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_PROF, _BOUND)
    signal.setitimer(signal.ITIMER_REAL, 10 * _BOUND)
    t0 = time.process_time()
    try:
        fn(*args)
    except _TooSlow:
        return _BOUND
    except ConfigError:
        pass
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGPROF, old)
        signal.signal(signal.SIGALRM, old_real)
    return time.process_time() - t0


def _mib_of(unit, close=""):
    """`_HEAD` + `unit` repeated + `close` + `}`, exactly 1 MiB (in bytes) or just under."""
    n = (_MIB - len(_HEAD) - len(close) - 1) // len(unit)
    return _HEAD + unit * n + close + "}"


ADVERSARIAL = {  # name: (text, fixed words in the refusal); each is at most 1 MiB, so the size cap does not catch it
    "digits": (_mib_of("1"), "number"),
    "neg-digits": (_HEAD + "-" + _mib_of("1")[len(_HEAD) + 1:], "number"),
    "deep-list": (_mib_of("["), "nested too deeply"),
    "deep-object": (_mib_of('{"a":'), "nested too deeply"),
}
BIG = {  # over 1 MiB: the cap refuses before parsing
    "brief-digits": _HEAD + "1" * _MIB + "}",
    "two-mib": json.dumps(BASE) + " " * (2 * _MIB),
}


def _assert_fixed(msg, text):
    assert "111" not in msg and "[[[" not in msg and '{"a"' not in msg and len(msg) < 300
    assert text[len(_HEAD):len(_HEAD) + 20] not in msg


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_r3b_ac1_load_refuses_long_integers_and_deep_nesting_with_a_fixed_text(tmp_path, monkeypatch, name):
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    text, words = ADVERSARIAL[name]
    assert len(text.encode("utf-8")) <= _MIB
    (tmp_path / "skj.config.json").write_text(text)
    with pytest.raises(ConfigInvalid, match=words) as e:
        Config.load(start_dir=tmp_path)
    _assert_fixed(str(e.value), text)


@pytest.mark.parametrize("name", sorted(BIG))
def test_r3b_ac1_load_refuses_more_than_1_mib_in_bytes(tmp_path, monkeypatch, name):
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    (tmp_path / "skj.config.json").write_text(BIG[name])
    with pytest.raises(ConfigInvalid, match="larger than 1 MiB") as e:
        Config.load(start_dir=tmp_path)
    _assert_fixed(str(e.value), BIG[name])


def test_r3b_ac1_the_cap_counts_bytes_not_characters(tmp_path, monkeypatch):
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    exact = json.dumps({**BASE, "ac_heading": "x"})
    pad = _MIB - len(exact)
    (tmp_path / "skj.config.json").write_bytes(exact.encode() + b" " * pad)  # exactly 1 MiB: read
    assert Config.load(start_dir=tmp_path).project == "SPK"
    text = json.dumps({**BASE, "pad": "é" * (_MIB // 2)}, ensure_ascii=False)  # under 1 Mi characters, over 1 MiB in UTF-8
    assert len(text) < _MIB < len(text.encode("utf-8"))
    (tmp_path / "skj.config.json").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigInvalid, match="larger than 1 MiB"):
        Config.load(start_dir=tmp_path)


def test_r3b_ac1_a_short_integer_and_a_duplicate_key_behave_as_before(tmp_path, git_env):
    # 1000 digits: fine; last key wins (under `stack`, whose content config does not check: an unknown top-level key is refused, D-002)
    text = json.dumps(BASE)[:-1] + ', "stack": {"x": ' + "1" * 1000 + '}, "strict": {"test_timeout": 3600}, "project": "DUP", "project": "SPK"}'
    (tmp_path / "skj.config.json").write_text(text)
    cfg = Config.load(start_dir=tmp_path)
    assert (cfg.project, cfg.strict["test_timeout"]) == ("SPK", 3600)
    repo = tmp_path / "r"
    repo.mkdir()
    _ref_repo(repo, text)
    assert Config.load_from_ref("refs/heads/main", repo=str(repo)).project == "SPK"
    (tmp_path / "skj.config.json").write_text(_HEAD + "1" * 1001 + "}")
    with pytest.raises(ConfigInvalid, match="number"):
        Config.load(start_dir=tmp_path)


@pytest.mark.parametrize("name", sorted(ADVERSARIAL) + sorted(BIG))
def test_r3b_ac1_load_from_ref_refuses_the_same_inputs(tmp_path, git_env, name):
    text = ADVERSARIAL[name][0] if name in ADVERSARIAL else BIG[name]
    repo = _ref_repo(tmp_path, text)
    with pytest.raises(ConfigInvalid) as e:
        Config.load_from_ref("refs/heads/main", repo=str(repo))
    _assert_fixed(str(e.value), text)
    assert str(e.value).startswith(config_mod.REF_LABEL + ": ")


def test_r3b_ac1_the_blob_is_read_from_a_bounded_pipe_never_whole(tmp_path, git_env, monkeypatch):
    repo = _ref_repo(tmp_path, json.dumps(BASE) + " " * (2 * _MIB))
    sha = git(repo, "rev-parse", "main:skj.config.json")
    assert config_mod._git_capped(str(repo), _MIB, "cat-file", "blob", sha) is None  # more than the cap: None, the rest unread
    assert config_mod._git_capped(str(repo), 3 * _MIB, "cat-file", "blob", sha).startswith(b'{"site"')
    assert config_mod._git_capped(str(repo), _MIB, "cat-file", "blob", "0" * 40) is None  # git fails: None
    asked = []
    real_popen0 = subprocess.Popen

    class _Spy:
        def __init__(self, f):
            self.f = f

        def read(self, n=-1):
            asked.append(n)
            return self.f.read(n)

        def close(self):
            self.f.close()

    def popen(*a, **kw):
        proc = real_popen0(*a, **kw)
        proc.stdout = _Spy(proc.stdout)
        return proc
    with monkeypatch.context() as m:
        m.setattr(config_mod.subprocess, "Popen", popen)
        assert config_mod._git_capped(str(repo), _MIB, "cat-file", "blob", sha) is None
    assert asked and all(0 <= n <= _MIB + 1 for n in asked)  # never a read to the end
    repo2 = tmp_path / "ok"
    repo2.mkdir()
    _ref_repo(repo2)
    seen = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def spy_run(argv, *a, **kw):
        seen.append(("run", argv, kw))
        return real_run(argv, *a, **kw)

    def spy_popen(argv, *a, **kw):
        seen.append(("popen", argv, kw))
        return real_popen(argv, *a, **kw)
    monkeypatch.setattr(config_mod.subprocess, "run", spy_run)
    monkeypatch.setattr(config_mod.subprocess, "Popen", spy_popen)
    monkeypatch.setenv("SKJ_TOKEN", "tok-SECRET")
    assert Config.load_from_ref("refs/heads/main", repo=str(repo2)).project == "SPK"
    shows = [s for s in seen if "show" in s[1]]
    assert shows and all(kind == "popen" for kind, _, _ in shows)  # the content never goes through an unbounded capture
    for kind, argv, kw in seen:
        assert argv[0] == "git" and not kw.get("shell") and "SKJ_TOKEN" not in kw["env"] and kw["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert all("--end-of-options" in argv for _, argv, _ in shows)


def _special(tmp_path, kind):
    p = tmp_path / "skj.config.json"
    if kind == "symlink":
        real = tmp_path / "real.json"
        real.write_text(json.dumps(BASE))
        p.symlink_to(real)
    elif kind == "dangling":
        p.symlink_to(tmp_path / "missing.json")
    elif kind == "fifo":
        os.mkfifo(p)
    elif kind == "dir":
        p.mkdir()
    return p


@pytest.mark.parametrize("kind", ["symlink", "dangling", "fifo", "dir"])
def test_r3b_ac2_load_refuses_a_link_fifo_or_directory_without_waiting(tmp_path, monkeypatch, kind):
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    _special(tmp_path, kind)
    with pytest.raises(ConfigInvalid, match="not a regular file") as e:
        Config.load(start_dir=tmp_path)
    assert str(tmp_path / "real.json") not in str(e.value)


@pytest.mark.parametrize("dev", ["/dev/zero", "/dev/null"])
def test_r3b_ac2_load_refuses_a_device_named_by_skj_config(tmp_path, monkeypatch, dev):
    if not os.path.exists(dev):
        pytest.skip("no " + dev)
    (tmp_path / "skj.config.json").write_text(json.dumps(BASE))  # the env path wins: it is refused, never skipped for this one
    monkeypatch.setenv("SKJ_CONFIG", dev)
    with pytest.raises(ConfigInvalid, match="not a regular file"):
        Config.load(start_dir=tmp_path)


@needs_timer
def test_r3b_ac2_a_fifo_named_by_skj_config_is_refused_without_blocking(tmp_path, monkeypatch):
    fifo = tmp_path / "pipe.json"
    os.mkfifo(fifo)
    monkeypatch.setenv("SKJ_CONFIG", str(fifo))
    assert stat.S_ISFIFO(os.stat(fifo).st_mode)
    assert _timed(Config.load, str(tmp_path)) < _BOUND
    with pytest.raises(ConfigInvalid, match="not a regular file"):
        Config.load(start_dir=tmp_path)


# ---- R3b-AC3: timing through every path (Config.load, load_from_ref, trusted, the CLI) ----
def _cli(argv):
    from skj.__main__ import main
    return main(argv)


@needs_timer
@pytest.mark.parametrize("name", sorted(ADVERSARIAL) + sorted(BIG))
def test_r3b_ac3_every_reader_finishes_under_the_bound(tmp_path, git_env, monkeypatch, capsys, name):
    text = ADVERSARIAL[name][0] if name in ADVERSARIAL else BIG[name]
    repo = _ref_repo(tmp_path, text)
    (repo / "skj.config.json").write_text(text)
    monkeypatch.chdir(repo)
    slow = {}
    for label, call in (("Config.load", lambda: Config.load(start_dir=repo)),
                        ("load_from_ref", lambda: Config.load_from_ref("refs/heads/main", repo=str(repo))),
                        ("trusted (no base)", lambda: trusted(str(repo))),
                        ("trusted (base)", lambda: trusted(str(repo), base="main")),
                        ("stack show", lambda: _cli(["stack", "show"])),
                        ("stack approve", lambda: _cli(["stack", "approve"])),
                        ("gate", lambda: _cli(["gate", "1", "--offline"]))):
        dt = _timed(call)
        if dt >= _BOUND:
            slow[label] = dt
    assert not slow, slow
    capsys.readouterr()
    assert _cli(["stack", "show"]) == 2 and _cli(["stack", "approve"]) == 2 and _cli(["gate", "1", "--offline"]) == 3
    err = capsys.readouterr().err
    assert "111" not in err and "[[[" not in err


# ---- H1a-AC1: git_env is an allow-list ---------------------------------------------------------------------------------------------

_LEAKY = ("AWS_SECRET_ACCESS_KEY", "ATLASSIAN_API_KEY", "JIRA_EMAIL", "NPM_AUTH", "ANTHROPIC_API_KEY", "GIT_ASKPASS", "GIT_SSH_COMMAND",
          "GIT_EXTERNAL_DIFF", "DEPLOY_KEY", "GH_TOKEN", "GITHUB_TOKEN", "SKJ_TOKEN", "LC_SFDX_AUTH_URL", "RANDOM_VAR",
          # fix round 1: the names whose leak runs code or redirects git, not only shows a secret
          "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_PARAMETERS", "GIT_EXEC_PATH", "GIT_SSH",
          "GIT_PROXY_COMMAND", "SSH_ASKPASS", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "BASH_ENV", "ENV", "PROMPT_COMMAND",
          "PAGER", "EDITOR", "GIT_EDITOR", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
_KEPT = {"PATH": "/bin", "HOME": "/h", "LANG": "C.UTF-8", "LC_ALL": "C", "USER": "u", "LOGNAME": "u", "TERM": "xterm", "TMPDIR": "/t",
         "SSH_AUTH_SOCK": "/s", "GIT_AUTHOR_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid", "GIT_CONFIG_GLOBAL": "/g",
         "GIT_CONFIG_NOSYSTEM": "1", "XDG_CONFIG_HOME": "/x", "HTTPS_PROXY": "http://proxy.example.com:3128", "JAVA_HOME": "/j"}


def test_h1a_ac1_git_env_passes_only_the_allow_list():
    from skj import prs
    env = config_mod.git_env({**_KEPT, **{k: "v-" + k for k in _LEAKY}})
    assert not set(env) & set(_LEAKY)
    assert {k: env[k] for k in _KEPT} == _KEPT  # PATH/HOME/LANG, identity, ssh agent, proxy, toolchain for hooks
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_NO_REPLACE_OBJECTS"] == "1"
    # the gh path is unchanged: gh's own token still reaches gh (CI), never git
    gh = prs.gh_env({**_KEPT, "GH_TOKEN": "gh-own", "GITHUB_TOKEN": "gh-ci"})
    assert gh["GH_TOKEN"] == "gh-own" and gh["GITHUB_TOKEN"] == "gh-ci"


def test_h1a_ac1_a_real_git_run_sees_none_of_them(tmp_path, git_env, monkeypatch):
    repo = _ref_repo(tmp_path)
    for k in _LEAKY:
        monkeypatch.setenv(k, "v-" + k)
    seen, real = [], subprocess.run
    monkeypatch.setattr(config_mod.subprocess, "run", lambda argv, *a, **kw: (seen.append(kw["env"]), real(argv, *a, **kw))[1])
    assert Config.load_from_ref("refs/heads/main", repo=str(repo)).project == "SPK"
    assert seen and all(not set(e) & set(_LEAKY) and "PATH" in e for e in seen)


# ---- DF1 (live run A1-17, D-002): an unknown top-level key is refused, as docs/configuration.md says ----
@pytest.mark.parametrize("key", ["bogus", "aprovers", "unatended", "Site"])
def test_unknown_top_level_key_is_refused_by_position(tmp_path, key):
    """A1-17 added `"bogus": 1` to a working config and doctor said `ok config`; a mistyped trust key failed open the same way."""
    with pytest.raises(ConfigInvalid, match=r"top level entry #3: unknown key \(valid: site, project, ") as e:
        _write(tmp_path, **{key: 1})
    assert key not in str(e.value).split("(valid:")[0]  # named by position: the key is PR-controlled text


def test_every_documented_top_level_key_is_accepted(tmp_path):
    cfg = _write(tmp_path, story_type="Story", subtask_type="Subtask", ac_heading="AC", transitions={}, branch_prefix="f/",
                 labels=[], gate={}, review_block_on=[], stack={}, mode="advisory", strict={}, rules={}, approvers=[],
                 approval="terminal", unattended={})
    assert cfg.project == "SPK"


# ---- DF1 (live run A4-33, D-013): a prefix whose names git would resolve to another ref is refused ----
@pytest.mark.parametrize("prefix", ["refs/heads/", "refs/", "Refs/heads/", "heads/", "tags/x/", "remotes/origin/"])
def test_ref_namespace_branch_prefix_is_refused(tmp_path, prefix):
    with pytest.raises(ConfigInvalid, match="'branch_prefix'"):
        _write(tmp_path, branch_prefix=prefix)


@pytest.mark.parametrize("prefix", ["feature/", "users/refs/", "references/", "headsup/", "tagsx/"])
def test_ordinary_branch_prefix_still_accepted(tmp_path, prefix):
    assert _write(tmp_path, branch_prefix=prefix).branch_prefix == prefix


# ---- DF2 (live run A12-X7, D-023): a local `git remote set-head` must not pick a weaker target branch ----
GUARDED = {**TRUST_BASE, "approvers": [A1], "approval": "comment", "mode": "strict"}


def _moved(tmp_path, main_cfg, weak_cfg=TRUST_BASE, default="main"):
    """origin/<default> holds `main_cfg`; a feature commit with `weak_cfg` is pushed as origin/lv-x7 and origin/HEAD is moved to it."""
    repo = _ref_repo(tmp_path, json.dumps(main_cfg))
    git(repo, "update-ref", f"refs/remotes/origin/{default}", "HEAD")
    git(repo, "checkout", "-q", "-b", "feature/SPK-1-x")
    commit(repo, "weaken", {"skj.config.json": json.dumps(weak_cfg)})
    git(repo, "update-ref", "refs/remotes/origin/lv-x7", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/lv-x7")  # what `git remote set-head origin lv-x7` writes
    return repo


@pytest.mark.parametrize("main_cfg", [GUARDED, {**TRUST_BASE, "approval": "comment", "approvers": [A1]}, {**TRUST_BASE, "mode": "strict"}])
@pytest.mark.parametrize("default", ["main", "master"])
def test_origin_head_moved_off_a_guarded_default_branch_is_refused(tmp_path, git_env, main_cfg, default):
    repo = _moved(tmp_path, main_cfg, default=default)
    with pytest.raises(ConfigInvalid, match="target-moved") as e:
        trusted(str(repo))
    assert "lv-x7" not in str(e.value) and A1 not in str(e.value)  # the moved-to branch is the agent's own name: never echoed
    with pytest.raises(ConfigInvalid, match="target-moved"):
        config_mod.default_base(str(repo))
    assert trusted(str(repo), base=f"origin/{default}").approval == main_cfg.get("approval", "terminal")  # an explicit base is unchanged


def test_origin_head_on_another_branch_with_the_same_trust_is_refused(tmp_path, git_env):
    """DF3 M1: same five trust keys are not enough (gate settings, test command, stack profile and lockfile come from origin/HEAD too):
    beside a guarded origin/main, origin/HEAD must be origin/main itself. A `develop` default branch is refused (documented)."""
    repo = _moved(tmp_path, GUARDED, {**GUARDED, "labels": ["develop-only"]})
    with pytest.raises(ConfigInvalid, match="target-moved"):
        trusted(str(repo))


STRONG = {**GUARDED, "gate": {"block": ["ac_traceability"]}, "strict": {"test_command": "pytest -q"}}


@pytest.mark.parametrize("target", ["refs/heads/weak", "refs/remotes/origin/weak"])
def test_df3_m1_origin_head_on_a_same_trust_branch_with_weaker_judging_is_refused(tmp_path, git_env, monkeypatch, target):
    from skj import __main__ as m
    repo = _moved(tmp_path, STRONG, {**STRONG, "gate": {"block": []}, "strict": {"test_command": "true"}})
    git(repo, "branch", "weak", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/weak", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", target)  # a local branch or a remote one: the five keys match, the rest is weaker
    monkeypatch.chdir(repo)
    with pytest.raises(ConfigInvalid, match="target-moved"):
        config_mod.default_base(str(repo))
    with pytest.raises(ConfigInvalid, match="target-moved"):
        m._judging_config(repo=str(repo))
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    assert config_mod.default_base(str(repo)) == "refs/remotes/origin/main"


def test_origin_head_moved_off_an_unguarded_default_branch_is_left_alone(tmp_path, git_env):
    repo = _moved(tmp_path, {**TRUST_BASE, "approvers": [A1]}, TRUST_BASE)  # terminal approval, advisory: nothing to switch off
    assert trusted(str(repo)).approvers == []
    assert config_mod.default_base(str(repo)) == "refs/remotes/origin/lv-x7"


@pytest.mark.parametrize("ci", ["", "1"])
def test_origin_head_deleted_beside_a_guarded_default_branch_compares_with_it(tmp_path, git_env, monkeypatch, ci):
    """DF3 M2: `git remote set-head origin --delete` (with or without CI set) leaves origin/main as the comparison branch."""
    monkeypatch.setenv("CI", ci)
    repo = _moved(tmp_path, GUARDED)
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")  # `git remote set-head origin --delete`
    assert config_mod.default_base(str(repo)) == "refs/remotes/origin/main"
    assert trusted(str(repo)).approval == "comment"
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert config_mod.default_base(str(repo)) == ""  # no remote default branch at all: the old no-base path (documented residual)
    assert trusted(str(repo)).base_known is False


@pytest.mark.parametrize("ci", ["", "1"])
def test_df3_m2_ci_set_locally_with_origin_head_deleted_keeps_main_strict(tmp_path, git_env, monkeypatch, ci):
    """The review's M2 input: origin/lv-x7 advisory with terminal approval, origin/main strict with comment approval, origin/HEAD deleted."""
    from skj import __main__ as m
    monkeypatch.setenv("CI", ci)
    repo = _moved(tmp_path, GUARDED)
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    monkeypatch.chdir(repo)
    assert m._write_judge("origin/lv-x7", str(repo)).mode == "strict"
    trust, refs = m._stricter_trust("origin/lv-x7", lambda t: bool(t.approvers), str(repo))
    assert trust.approval == "comment" and "refs/remotes/origin/main" in refs


def test_origin_head_on_a_guarded_branch_with_other_approvers_is_refused(tmp_path, git_env):
    repo = _moved(tmp_path, GUARDED, {**GUARDED, "approvers": [A2]})  # still comment approval and strict, but the agent's own approver list
    with pytest.raises(ConfigInvalid, match="target-moved"):
        trusted(str(repo))


def test_ci_without_origin_head_compares_with_a_guarded_main(tmp_path, git_env, monkeypatch):
    """DF3 M2: a CI checkout with a guarded origin/main and no origin/HEAD reads origin/main; one with no origin/main is unchanged."""
    repo = _moved(tmp_path, GUARDED)
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    monkeypatch.setenv("CI", "true")
    assert config_mod.default_base(str(repo)) == "refs/remotes/origin/main"
    assert config_mod.pin_status(Config(SITE, "SPK"), trusted(str(repo))) == "ok"
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert config_mod.default_base(str(repo)) == ""
    assert config_mod.pin_status(Config(SITE, "SPK"), trusted(str(repo))) == "refused-ci-no-base"


# ---- DF4 (re-review N1): ONE home ref, origin/main when it exists, else origin/master; never "either" ----
@pytest.mark.parametrize("master_cfg", [{**GUARDED, "mode": "advisory"}, {**STRONG, "gate": {"block": []}, "strict": {"test_command": "true"}}])
def test_df4_a_forged_origin_master_beside_a_guarded_main_is_refused(tmp_path, git_env, monkeypatch, master_cfg):
    """The re-review's A3/A2 inputs: a new local refs/remotes/origin/master (no push, origin/main untouched) and origin/HEAD set to it."""
    from skj import __main__ as m
    repo = _moved(tmp_path, STRONG, master_cfg)
    git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")  # `git remote set-head origin master`
    commit(repo, "pr", {"skj.config.json": json.dumps(TRUST_BASE)})
    monkeypatch.chdir(repo)
    for f in (lambda: config_mod.default_base(str(repo)), lambda: trusted(str(repo)), lambda: m._judging_config(repo=str(repo)),
              lambda: m._write_judge(None, str(repo))):
        with pytest.raises(ConfigInvalid, match="target-moved"):
            f()
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    assert config_mod.default_base(str(repo)) == "refs/remotes/origin/main"  # unset: the home ref, never the forged master


def test_df4_origin_master_is_not_read_when_origin_main_exists(tmp_path, git_env):
    """Probe H: a stale origin/master whose config is invalid no longer breaks every verb when origin/HEAD -> a valid origin/main."""
    repo = _moved(tmp_path, GUARDED)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    commit(repo, "stale", {"skj.config.json": json.dumps({**TRUST_BASE, "bogus": 1})})
    git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    assert config_mod.default_base(str(repo)) == "refs/remotes/origin/main"
    assert trusted(str(repo)).approval == "comment"


# ---- DF4 (re-review N6): the ci/ templates point origin/HEAD at the PR's target; in CI that is no target-moved refusal ----
def test_df4_ci_origin_head_at_the_pr_target_reads_the_guarded_main(tmp_path, git_env, monkeypatch):
    repo = _moved(tmp_path, GUARDED)  # origin/HEAD -> origin/lv-x7 (advisory), as the template's `git symbolic-ref` for a PR into it
    monkeypatch.setenv("CI", "true")
    assert config_mod.default_base(str(repo)) == "refs/remotes/origin/main"
    assert trusted(str(repo)).approval == "comment"  # `skj pr link`'s trust (site pin): the guarded main, never refused
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")  # a target that does not exist is still refused
    with pytest.raises(ConfigInvalid, match="target-moved"):
        config_mod.default_base(str(repo))
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/lv-x7")
    monkeypatch.setenv("CI", "")  # a local run where origin/HEAD merely drifted keeps the refusal
    with pytest.raises(ConfigInvalid, match="target-moved"):
        config_mod.default_base(str(repo))
