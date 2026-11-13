"""`skj setup` (R3, K6): writes skj.config.json from the template; on macOS it only prints the Keychain command,
elsewhere a hidden prompt stores the identity in ~/.skj/<site>.json (0600). Nothing is sent to Jira."""
import getpass
import inspect
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from fakejira import EMAIL, TOKEN
from skj import secretstore, setup_cmd
from skj.__main__ import main

ROOT = Path(__file__).resolve().parent.parent
SITE = "example-site.atlassian.net"
LINE = "security add-generic-password -a <your-email> -s skj:%s -w" % SITE


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    def no_subprocess(*a, **k):
        raise AssertionError("setup ran a subprocess")
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, no_subprocess)
    monkeypatch.setattr(os, "system", no_subprocess)
    return h


def never(*a, **k):
    raise AssertionError("must not be called")


def answers(*values):
    it = iter(values)
    return lambda prompt="": next(it)


def test_macos_prints_security_line_only(home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(secretstore, "store_file", never)
    assert setup_cmd.run_setup(site=SITE, project="SPK", input_fn=never, getpass_fn=never) == 0
    out = capsys.readouterr().out
    assert [ln for ln in out.splitlines() if "add-generic-password" in ln] == ["  " + LINE]
    assert not (home / ".skj").exists()
    data = json.loads(Path("skj.config.json").read_text())
    assert data == {**setup_cmd.TEMPLATE, "site": SITE, "project": "SPK"}


def test_non_macos_hidden_prompt_writes_0600(home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    prompts = []

    def hidden(prompt=""):
        prompts.append(prompt)
        return TOKEN
    assert setup_cmd.run_setup(site=SITE, project="SPK", input_fn=answers(EMAIL), getpass_fn=hidden) == 0
    assert len(prompts) == 1 and "hidden" in prompts[0]
    assert inspect.signature(setup_cmd.run_setup).parameters["getpass_fn"].default is getpass.getpass
    f = home / ".skj" / (SITE + ".json")
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert json.loads(f.read_text()) == {"email": EMAIL, "token": TOKEN}
    assert "add-generic-password" not in capsys.readouterr().out


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_setup_output_has_no_secret(home, monkeypatch, capsys, platform):
    monkeypatch.setattr(sys, "platform", platform)
    assert setup_cmd.run_setup(site=SITE, project="SPK", input_fn=answers(EMAIL),
                               getpass_fn=lambda prompt="": TOKEN) == 0
    out, err = capsys.readouterr()
    for s in (TOKEN, EMAIL, "@"):
        assert s not in out + err
    written = Path("skj.config.json").read_text()
    assert TOKEN not in written and EMAIL not in written and "@" not in written


def test_prompts_for_site_and_project_and_validates_them(home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert setup_cmd.run_setup(input_fn=answers(" " + SITE + " ", "SPK")) == 0
    assert json.loads(Path("skj.config.json").read_text())["site"] == SITE
    os.remove("skj.config.json")
    for site, project in (("https://" + SITE, "SPK"), ("evil.example.com", "SPK"), (SITE, "spk"), (SITE, "S")):
        with pytest.raises(ValueError):
            setup_cmd.run_setup(site=site, project=project, input_fn=never, getpass_fn=never)
        assert not Path("skj.config.json").exists()


def test_refuses_to_overwrite_an_existing_config(home, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    Path("skj.config.json").write_text("{\"mine\": 1}")
    with pytest.raises(setup_cmd.SetupError, match="already exists"):
        setup_cmd.run_setup(site=SITE, project="SPK", input_fn=never, getpass_fn=never)
    assert Path("skj.config.json").read_text() == "{\"mine\": 1}"
    os.remove("skj.config.json")
    os.symlink(tmp_path / "elsewhere.json", "skj.config.json")  # a dangling symlink is not followed
    with pytest.raises(setup_cmd.SetupError):
        setup_cmd.run_setup(site=SITE, project="SPK", input_fn=never, getpass_fn=never)
    assert not (tmp_path / "elsewhere.json").exists()


def test_empty_token_or_email_stores_and_writes_nothing(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert setup_cmd.run_setup(site=SITE, project="SPK", input_fn=answers(EMAIL), getpass_fn=lambda p="": "") == 2
    assert setup_cmd.run_setup(site=SITE, project="SPK", input_fn=answers(""), getpass_fn=never) == 2
    assert not Path("skj.config.json").exists() and not (home / ".skj").exists()


def test_template_matches_the_repo_template():
    data = json.loads((ROOT / "skj.config.template.json").read_text())
    assert data == setup_cmd.TEMPLATE


def test_cli_setup(home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(getpass, "getpass", never)
    assert main(["setup", "--site", SITE, "--project", "SPK"]) == 0
    assert LINE in capsys.readouterr().out
    assert main(["setup", "--site", SITE, "--project", "SPK"]) == 1  # exists: refused, no traceback
    assert "already exists" in capsys.readouterr().err


# ---- DF1 (live runs A1-04, A1-05, D-001; A1-06, D-007) ----
BAD_SITES = ["https://speckjira-test.atlassian.net", "SPECKJIRA-TEST.atlassian.net", "evil.example.com",
             "speckjira-test.atlassian.net.evil.com", "a b.atlassian.net", "x\u202e.atlassian.net"]


@pytest.mark.parametrize("site,project", [(s, "SPK") for s in BAD_SITES] + [(SITE, p) for p in ("spk", "1SPK", "ABCDEFGHIJK", "SPK;id")])
def test_cli_setup_bad_value_is_a_usage_error_exit_2(home, monkeypatch, capsys, site, project):
    """A1-04/A1-05: each was refused with a fixed text and no file, but with exit 1; `skj get` exits 2 on a bad ref."""
    monkeypatch.setattr(sys, "platform", "darwin")
    assert main(["setup", "--site", site, "--project", project]) == 2
    err = capsys.readouterr().err
    assert "invalid" in err and site not in err and project not in err.replace("SPK)", "")
    assert not os.path.lexists("skj.config.json")


@pytest.mark.parametrize("argv", [[], ["--site", SITE], ["--project", "SPK"]])
def test_cli_setup_refuses_to_prompt_without_a_terminal(home, monkeypatch, capsys, argv):
    """A1-06: `skj setup </dev/null` printed the prompt and read stdin; piped answers would have been taken."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(SITE + "\nSPK\n"))
    monkeypatch.setattr("builtins.input", never)
    assert main(["setup", *argv]) == 2
    captured = capsys.readouterr()
    assert "terminal" in captured.err and "Jira site" not in captured.out
    assert not os.path.lexists("skj.config.json")


def test_cli_setup_off_macos_needs_a_terminal_even_with_flags(home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("x\n"))
    monkeypatch.setattr("builtins.input", never)
    monkeypatch.setattr(getpass, "getpass", never)
    assert main(["setup", "--site", SITE, "--project", "SPK"]) == 2
    assert "terminal" in capsys.readouterr().err and not os.path.lexists("skj.config.json")
