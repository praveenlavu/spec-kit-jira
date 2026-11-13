"""`skj branch KEY [--apply]` (R5): `<branch_prefix><KEY>-<slug>`, local git only, one GET and no Jira write."""
import subprocess

import pytest

from fakejira import FakeJira
from skj import branch, config
from skj.__main__ import main


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=config.git_env(),
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def check_ref_format(name):
    """The real git's verdict (scrubbed env)."""
    return subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True, env=config.git_env(),
                          stdin=subprocess.DEVNULL).returncode == 0


# --- R5 named tests ------------------------------------------------------------------------------------------------

def test_uppercase_key_in_branch():
    name = branch.branch_name("feature/", "SPK-123", "Count active members")
    assert name == "feature/SPK-123-count-active-members"
    assert "SPK-123" in name and "spk-123" not in name
    for lower in ("spk-123", "Spk-123", "SPK-0123", " SPK-123", "SPK-123\n"):
        with pytest.raises(ValueError):
            branch.branch_name("feature/", lower, "x")  # refused, never upper-cased


UNSAFE = [  # (summary, expected slug)
    ("two  spaces\tand\ttabs", "two-spaces-and-tabs"),
    ("a/../b", "a-b"), ("..", ""), ("--leading and trailing--", "leading-and-trailing"), ("-rf", "rf"),
    ("ctl\x00\x01\x1b[31m\x7f\n\rend", "ctl-31m-end"),
    ("Café Über", "cafe-uber"), ("日本語", ""), ("fire \U0001f525 rocket \U0001f680", "fire-rocket"),
    ("a\\b/c:d?e*f[g]h~i^j", "a-b-c-d-e-f-g-h-i-j"), ("x@{y}", "x-y"), ("foo.lock", "foo-lock"), ("trailing.", "trailing"),
    ("", ""), (None, ""), (12, ""), ("; rm -rf $HOME `x`", "rm-rf-home-x"),
]


@pytest.mark.parametrize("summary,expected", UNSAFE)
def test_slug_sanitised(summary, expected):
    assert branch.slug(summary) == expected


def test_slug_length_cap():
    assert branch.slug("x" * 500) == "x" * branch.SLUG_MAX
    assert branch.slug("a" * 39 + " b") == "a" * 39
    assert branch.slug("Count active team members by status and role") == "count-active-team-members-by-status-and"
    assert branch.slug("a" * 36 + " bbb ccc") == "a" * 36 + "-bbb"
    assert branch.slug("hello world", 5) == "hello" and branch.slug("hello world", 7) == "hello"
    assert branch.slug("hello world", 0) == ""


def test_story_from_branch_returns_key():
    assert branch.story_from_branch("feature/SPK-12-count-active") == "SPK-12"
    assert branch.story_from_branch("refs/heads/feature/SPK-12") == "SPK-12"
    assert branch.story_from_branch("SPK-7-x") == "SPK-7"
    for name in ("feature/SPK-12-x", "SPK-1", "main", "001-spec", "feature/12-x", "feature/spk-12-x"):
        got = branch.story_from_branch(name)
        assert got is None or (isinstance(got, str) and not isinstance(got, int))


def test_lowercase_key_branch_not_recognised():
    assert branch.story_from_branch("feature/spk-12-x") is None
    assert branch.story_from_branch("spk-12") is None
    assert branch.story_from_branch("feature/Spk-12-x") is None


@pytest.mark.parametrize("name,expected", [
    ("feature/XSPK-12-x", "XSPK-12"),   # a longer project token is a different key, never SPK-12
    ("feature/aSPK-12-x", None), ("feature/SPK-12abc", None), ("feature/SPK-12_x", None), ("feature/SPK-0-x", None),
    ("feature/SPK-012-x", None), ("feature/SPK-12345678901", None), ("feature/12", None), ("", None),
    ("feature/SPK-12-fix-ABC-3", "SPK-12"),  # the first key wins
    (None, None), (7, None),
])
def test_story_from_branch_boundaries(name, expected):
    assert branch.story_from_branch(name) == expected


def test_round_trip():
    for key in ("SPK-1", "SPK-2147483647", "AB-9999999999"):
        assert branch.story_from_branch(branch.branch_name("feature/", key, "Some title")) == key
        assert branch.story_from_branch(branch.branch_name("", key, "Some title")) == key


# --- the name: own pre-check and real git agree ---------------------------------------------------------------------

def test_names_for_the_unsafe_table_pass_git_check_ref_format():
    for summary, _ in UNSAFE:
        for prefix in ("", "feature/", "users/team-a/", "story-"):
            name = branch.branch_name(prefix, "SPK-5", summary)
            assert check_ref_format(name), name
            branch.check_ref(name)
            assert len(name) <= branch.NAME_MAX


def test_empty_slug_falls_back_to_prefix_and_key():
    assert branch.branch_name("feature/", "SPK-5", "日本語") == "feature/SPK-5"
    assert branch.branch_name("", "SPK-5", "") == "SPK-5"


def test_total_length_cap_with_a_long_prefix():
    prefix = "p" * 90 + "/"
    name = branch.branch_name(prefix, "SPK-12", "x" * 100)
    assert len(name) <= branch.NAME_MAX and name.startswith(prefix + "SPK-12") and check_ref_format(name)
    with pytest.raises(ValueError):
        branch.branch_name("p" * 99 + "/", "ABCDEFGHIJ-1234567890", "x")  # prefix + key alone exceed the cap


BAD_NAMES = ["a..b", "a@{b", "-x", "--force", "a b", "a\tb", "a\x00b", "a\x7fb", "a\x1bb", "x.lock", "a/x.lock/b", "a.",
             "a/", "/a", "a//b", "a/.b", "@", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b", "", "a/b.lock", "café"]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_check_ref_refuses(name):
    with pytest.raises(ValueError):
        branch.check_ref(name)
    if name and "\x00" not in name:
        assert not check_ref_format(name) or name in ("café", "@")  # git accepts these (non-ASCII; `@` = HEAD)


@pytest.mark.parametrize("prefix", ["-x/", "a..b/", "x.lock/", "a//", "/feature/", "bad ref/", "a@{b/", "é/"])
def test_bad_prefix_refused(prefix):
    with pytest.raises(ValueError):
        branch.branch_name(prefix, "SPK-5", "title")


@pytest.mark.parametrize("name", ["feature/SPK-1-x", "SPK-1", "users/team-a/SPK-1-x", "a.b/SPK-1-x-1.2"])
def test_check_ref_accepts(name):
    branch.check_ref(name)
    assert check_ref_format(name)


# --- the CLI on a real temporary repo and the S9 fake ---------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    import json
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK"}))
    git(tmp_path, "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
        "commit", "-q", "--allow-empty", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def fake():
    f = FakeJira()
    f.add_issue("Count active members")
    return f


def run(fake, *args):
    return main(["branch", *args], client_factory=fake.client)


def head(repo):
    return git(repo, "symbolic-ref", "--short", "HEAD")


def test_dry_run_prints_name_and_command_and_changes_nothing(repo, fake, capsys):
    assert run(fake, "SPK-1") == 0
    out = capsys.readouterr().out
    assert "branch: feature/SPK-1-count-active-members" in out
    assert "git switch -c feature/SPK-1-count-active-members" in out and "--apply" in out
    assert head(repo) == "main" and git(repo, "branch", "--list") == "* main"
    assert [e["path"] for e in fake.log] == ["/issue/SPK-1"] and fake.log[0]["query"] == {"fields": "summary"}
    assert fake.writes() == []


def test_apply_creates_and_switches(repo, fake, capsys):
    assert run(fake, "SPK-1", "--apply") == 0
    assert head(repo) == "feature/SPK-1-count-active-members"
    assert git(repo, "rev-parse", "main") == git(repo, "rev-parse", "HEAD")
    assert "applied" in capsys.readouterr().out and fake.writes() == []
    assert branch.story_from_branch(head(repo)) == "SPK-1"


def test_number_is_accepted(repo, fake):
    assert run(fake, "1", "--apply") == 0 and head(repo) == "feature/SPK-1-count-active-members"


def test_existing_branch_refused_in_dry_run_and_apply(repo, fake, capsys):
    git(repo, "branch", "feature/SPK-1-count-active-members")
    for extra in ((), ("--apply",)):
        assert run(fake, "SPK-1", *extra) == 1
        assert "already exists" in capsys.readouterr().err
    assert head(repo) == "main"


def test_unsafe_summary_is_sanitised_end_to_end(repo, capsys):
    f = FakeJira()
    f.add_issue("../../etc -rf \x1b[31m @{ x.lock \U0001f525")
    assert run(f, "SPK-1", "--apply") == 0
    name = head(repo)
    assert name == "feature/SPK-1-etc-rf-31m-x-lock" and check_ref_format(name)


def test_non_ascii_summary_falls_back_to_key_only(repo, capsys):
    f = FakeJira()
    f.add_issue("日本語")
    assert run(f, "SPK-1", "--apply") == 0 and head(repo) == "feature/SPK-1"


@pytest.mark.parametrize("ref", ["spk-1", "ABC-1", "SPK-01", "0", "SPK-1;x", " 1", "-1"])
def test_bad_or_foreign_refs_refused_before_any_request(repo, fake, capsys, ref):
    assert run(fake, ref) == 2
    assert fake.log == [] and "Traceback" not in capsys.readouterr().err


def test_custom_prefix_from_config(repo, fake):
    import json
    (repo / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK",
                                                      "branch_prefix": "users/team-a/"}))
    assert run(fake, "SPK-1", "--apply") == 0 and head(repo) == "users/team-a/SPK-1-count-active-members"


def test_outside_a_git_repository_refused(tmp_path, monkeypatch, fake, capsys):
    import json
    monkeypatch.delenv("SKJ_CONFIG", raising=False)
    (tmp_path / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK"}))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    monkeypatch.chdir(tmp_path)
    assert run(fake, "SPK-1", "--apply") == 1
    assert "git repository" in capsys.readouterr().err and fake.log == []


def test_git_runs_with_a_scrubbed_env(repo, fake, monkeypatch):
    monkeypatch.setenv("SKJ_TOKEN", "sekret-token-value")
    seen = []
    real = subprocess.run

    def spy(argv, **kw):
        seen.append((argv, kw.get("env")))
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    assert run(fake, "SPK-1", "--apply") == 0
    assert seen and all(isinstance(a, list) for a, _ in seen)
    assert all(e is not None and not any(k.startswith("SKJ_") for k in e) for _, e in seen)


# --- fix round 1: the name must read back as the same key ------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["feature/SPK-9/", "REL-2/", "X", "story", "feature", "team_", "a.lock", "a1"])
def test_prefix_that_hides_the_key_is_refused(prefix):
    assert config.valid_branch_prefix(prefix)  # the config accepts it; the round trip is what refuses
    with pytest.raises(ValueError, match="hides the key"):
        branch.branch_name(prefix, "SPK-5", "Count active")


@pytest.mark.parametrize("prefix", ["feature/", "", "users/jo/", "team-", "release/v1.2/"])
def test_valid_prefixes_round_trip(prefix):
    name = branch.branch_name(prefix, "SPK-5", "Count active")
    assert branch.story_from_branch(name) == "SPK-5" and check_ref_format(name)


def test_cli_refuses_a_prefix_that_hides_the_key(repo, fake, capsys):
    import json
    (repo / "skj.config.json").write_text(json.dumps({"site": "example-site.atlassian.net", "project": "SPK",
                                                      "branch_prefix": "feature/SPK-9/"}))
    assert run(fake, "SPK-1", "--apply") == 1
    assert "hides the key" in capsys.readouterr().err and head(repo) == "main"


def test_create_rechecks_the_name(repo):
    for name in ("storySPK-5", "no-key-here"):
        with pytest.raises(ValueError):
            branch.create(repo, name)
    assert head(repo) == "main" and git(repo, "branch", "--list") == "* main"


def test_head_is_refused():
    with pytest.raises(ValueError):
        branch.check_ref("HEAD")


def test_h1a_ac6_branch_has_no_dead_link_option(capsys):
    """K9 cut the branch link: no `--link` option, and no registered write verb that has no parser."""
    import skj.__main__ as m
    with pytest.raises(SystemExit) as e:
        m.main(["branch", "--help"])
    assert e.value.code == 0 and "--link" not in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        m.main(["branch", "1", "--link"])
    assert e.value.code == 2 and "unrecognized arguments: --link" in capsys.readouterr().err
    assert not any("--link" in v for v in m.WRITE_VERBS)
