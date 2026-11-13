"""Shared helpers of the stack test suites (no test of its own). The fork keeps these in its conftest.py; this build keeps tests/conftest.py to
the network guard, so the stack helpers live here and the suites import them by name (`from test_stack_support import ...`)."""
import copy
import os
import pty
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from skj import stack


def profile(**over):
    p = {"schema": 1, "version": 2, "status": "draft",
         "stacks": [{"id": "python", "root": ".",
                     "detected": {"confidence": "high", "evidence": ["pyproject.toml"]},
                     "commands": {"test": {"run": "PYTHONPATH=src python3 -m pytest -q", "source": "S1"},
                                  "lint": {"run": "ruff check .", "source": "unverified"},
                                  "org_only": [{"run": "sf apex run test --wait 10", "source": "S2"}]},
                     "conventions": [{"rule": "Test files are named test_*.py", "source": "S1"},
                                     {"rule": "Keep modules small", "source": "unverified"}],
                     "dod": [{"item": "No new lint findings", "source": "S1"}]}],
         "sources": {"S1": {"kind": "official", "url": "https://docs.pytest.org/en/stable/", "retrieved": "2026-01-15",
                            "evidence": "Run tests with python -m pytest."},
                     "S2": {"kind": "repo", "path": "package.json", "retrieved": "2026-01-15", "evidence": "scripts.test"}}}
    p.update(over)
    return p


def approve(p, **over):
    """`p` as a human's approval records it: status approved and the hash of everything but `approval`."""
    p = copy.deepcopy(p)
    p["status"] = "approved"
    p["approval"] = {"by": "tech lead", "date": "2026-01-16", "sha256": "0" * 64}
    p["approval"]["sha256"] = stack.profile_hash(p)
    p["approval"].update(over)
    return p


# ---- real temp git repos ----
_IDENTITY = {f"GIT_{k}_{f}": v for k in ("AUTHOR", "COMMITTER") for f, v in (("NAME", "Test"), ("EMAIL", "test@example.invalid"))}


def git(repo, *a):
    # a commit needs an identity even in a bare container (no global git config); an identity already in the environment wins
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, env={**_IDENTITY, **os.environ}).stdout.strip()


def write_files(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def commit(root, msg, files=None):
    write_files(root, files or {})
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)
    return git(root, "rev-parse", "--short", "HEAD")


@pytest.fixture
def git_env(monkeypatch):
    """Isolate git from the user's config (signing, hooks) and give commits a fixed identity."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{k}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{k}_EMAIL", "test@example.invalid")


# ---- the typed approval code of `skj stack approve --apply` ----
APPROVAL_CODE = re.compile(r"approval code: ([0-9a-f]{12})")
_SRC = str(Path(__file__).resolve().parent.parent / "src")


def shown_code(screen):
    """The approval code on a screen (what a human would read and type)."""
    found = APPROVAL_CODE.findall(screen)
    assert found, "no approval code on the screen"
    return found[-1]


class TypedStdin:
    """An in-process terminal stdin (`isatty` is True) whose `readline` answers like a human who reads the screen: by default it types the
    code printed so far on `sys.stdout` (pytest's capture, read without consuming it). `answer` is returned as the line instead
    (a newline alone is an empty line, `""` end of input, an exception class is raised). `before(code)` runs after the prompt and before the answer:
    the hook point for changing a file between the prompt and the typed code. `lines` counts the readline calls."""

    def __init__(self, answer=None, before=None):
        self.answer, self.before, self.lines = answer, before, 0

    def isatty(self):
        return True

    def readline(self, size=-1):
        self.lines += 1
        if self.before is not None:
            self.before(shown_code(sys.stdout.getvalue()))
        if isinstance(self.answer, type) and issubclass(self.answer, BaseException):
            raise self.answer
        return self.answer if self.answer is not None else shown_code(sys.stdout.getvalue()) + "\n"


def terminal(monkeypatch, answer=None, before=None):
    """stdin is a `TypedStdin` and stdout a terminal (use with capsys); returns the stdin."""
    stdin = TypedStdin(answer, before)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    return stdin


def run_in_pty(args, cwd, answer=None, before=None, stdout_pty=True, stdin_pty=True, interrupt=False, early=None, timeout=60, argv=None):
    """`python -m skj ARGS` as a real process with a pty on stdin (and on stdout unless `stdout_pty` is False). When the screen shows
    `approval code: <12 hex>` it calls `before(code)` (change a file between the prompt and the typed code), then types `answer` (default:
    the code; `""` sends Ctrl-D, end of input) or, with `interrupt`, sends SIGINT. `early` is typed right after the start, before any prompt. Returns `(returncode, screen, stderr)`; `screen` has
    CRLF turned into LF. Without a code on the screen nothing is typed. `timeout` is a deadline for the whole run: a child still running
    then is killed and the test fails. `argv` replaces `python -m skj ARGS` (for testing the helper itself)."""
    env = {**os.environ, "PYTHONPATH": _SRC}
    env.pop("SKJ_CONFIG", None)
    master, slave = pty.openpty()
    proc = subprocess.Popen(argv or [sys.executable, "-m", "skj", *args], cwd=cwd, env=env, stdin=slave if stdin_pty else subprocess.DEVNULL,
                            stdout=slave if stdout_pty else subprocess.PIPE, stderr=subprocess.PIPE,
                            # a background job (nohup, `&`) ignores SIGINT and the child would inherit it: Ctrl-C must reach it as at a terminal
                            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    os.close(slave)
    if early is not None:
        os.write(master, early.encode() + b"\n")
    out, typed, deadline = b"", False, time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() > deadline:
                proc.kill()
                pytest.fail(f"run_in_pty: the child did not finish within {timeout} s and was killed (it waits for input that never comes?)")
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            elif proc.poll() is not None:
                break
            if not typed and APPROVAL_CODE.search(out.decode(errors="replace")):
                typed, code = True, shown_code(out.decode(errors="replace"))
                if before is not None:
                    before(code)
                if interrupt:
                    proc.send_signal(signal.SIGINT)
                else:
                    os.write(master, b"\x04" if answer == "" else (code if answer is None else answer).encode() + b"\n")
        piped, err = proc.communicate(timeout=timeout)
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
            proc.communicate()  # reaps the killed child and closes its pipes
    return proc.returncode, (out if stdout_pty else piped).decode().replace("\r\n", "\n"), err.decode()
