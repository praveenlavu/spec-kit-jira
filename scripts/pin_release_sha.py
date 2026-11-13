#!/usr/bin/env python3
"""Pin the release commit into the skj install line: `git+<repository>@<COMMIT-SHA>` becomes `git+<repository>@<commit>`.

Takes only a full 40-character lowercase hex commit (never a tag, a branch or a short sha: a tag can be moved). Changes exactly the
tracked files (`git ls-files`) that hold the install line, prints each one, and refuses (exit 1) when no file holds it.
Usage: pin_release_sha.py <40-hex commit>
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List

REPO_URL = "https://github.com/praveenlavu/spec-kit-jira"
PLACEHOLDER = "<" + "COMMIT-SHA" + ">"  # split so this file never holds the install line it rewrites
LINE = "git+%s@%s" % (REPO_URL, PLACEHOLDER)
COMMIT = re.compile(r"[0-9a-f]{40}")
GIT_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class PinError(Exception):
    pass


def targets(root: Path) -> List[str]:
    """The tracked files that hold the unpinned install line, sorted."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], env=GIT_ENV, capture_output=True, check=True).stdout
    rels = sorted(r for r in out.decode("utf-8").split("\0") if r)
    return [r for r in rels if (Path(root) / r).is_file() and LINE in (Path(root) / r).read_text(encoding="utf-8", errors="replace")]


def pin(root: Path, sha: str) -> List[str]:
    if not COMMIT.fullmatch(sha):
        raise PinError("the commit must be 40 lowercase hex characters, got %r" % sha)
    found = targets(root)
    if not found:
        raise PinError("no install line holds %s: nothing to pin" % PLACEHOLDER)
    for rel in found:
        path = Path(root) / rel
        path.write_text(path.read_text(encoding="utf-8").replace(LINE, "git+%s@%s" % (REPO_URL, sha)), encoding="utf-8")
    return found


def main(argv: List[str]) -> int:
    if len(argv) != 1:
        print("usage: pin_release_sha.py <40-hex commit>", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parent.parent
    try:
        changed = pin(root, argv[0])
    except PinError as e:
        print("pin_release_sha: %s" % e, file=sys.stderr)
        return 2 if "40 lowercase hex" in str(e) else 1
    print("\n".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
