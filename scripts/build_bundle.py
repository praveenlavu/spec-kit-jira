#!/usr/bin/env python3
"""Build the two release zips from the tracked files of a git checkout.

- `<id>-<version>.zip`: the extension, exactly the tracked files `.extensionignore` admits, extension.yml at the archive root.
- `jira-lifecycle-workflow.zip`: the workflow, the tracked files of workflows/jira-lifecycle/ with workflow.yml at the root
  (Spec Kit installs a workflow archive only with workflow.yml at its root or in exactly one top-level directory).

The file list is `git ls-files` (config-scrubbed git), so an untracked file never ships; any symlink on a selected path refuses the
build. extension.yml ships byte for byte as tracked (its `repository`/`homepage` are the public repository's).
Deterministic: sorted entries, fixed timestamp, mode and create_system; the deflate bytes still depend on the zlib version.
Usage: build_bundle.py --out DIR   (DIR outside the repository; never commit a zip)
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import List, Tuple

IGNORE_FILE = ".extensionignore"
WORKFLOW_DIR = "workflows/jira-lifecycle/"
WORKFLOW_ZIP = "jira-lifecycle-workflow.zip"
STAMP = (1980, 1, 1, 0, 0, 0)
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "LANG": "C"}


class BuildError(Exception):
    pass


def _rules(root: Path) -> List[Tuple[bool, bool, bool, str]]:
    """(negated, anchored, dir_only, pattern) per line."""
    out = []
    for line in (root / IGNORE_FILE).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        line = line[1:] if neg else line
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = line.startswith("/") or "/" in line
        out.append((neg, anchored, dir_only, line.lstrip("/")))
    return out


def _ignored(rules, rel: str, is_dir: bool) -> bool:
    # shortcut: covers the gitignore forms .extensionignore uses (anchored, dir-only, negation, basename globs; no `**`),
    # upgrade to full gitignore semantics if the file grows other forms.
    hit = False
    for neg, anchored, dir_only, pat in rules:
        if dir_only and not is_dir:
            continue
        if anchored:
            ok = rel.count("/") == pat.count("/") and fnmatch.fnmatchcase(rel, pat)
        else:
            ok = fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pat)
        if ok:
            hit = not neg
    return hit


def tracked_files(root: Path) -> List[str]:
    """`git ls-files` of `root`, which must be the top of its work tree; refuses a selected path that is or passes a symlink."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.devnull, **GIT_ENV}
    run = lambda *a: subprocess.run(("git", "-C", str(root)) + a, env=env, capture_output=True, check=False)
    top = run("rev-parse", "--show-toplevel")
    if top.returncode or Path(top.stdout.decode().strip()).resolve() != Path(root).resolve():
        raise BuildError("not the top of a git work tree: {}".format(root))
    out = run("ls-files", "-z")
    if out.returncode:
        raise BuildError("git ls-files failed")
    return sorted(p for p in out.stdout.decode("utf-8").split("\0") if p)


def _refuse_links(root: Path, rel: str) -> None:
    parts = rel.split("/")
    for i in range(1, len(parts) + 1):
        if os.path.islink(os.path.join(str(root), *parts[:i])):
            raise BuildError("refusing symlink: " + "/".join(parts[:i]))


def included_files(root: Path) -> List[str]:
    """Tracked files the installer would copy: an excluded directory prunes its subtree (as Spec Kit's copytree does), and the
    ignore file itself never ships."""
    tracked = tracked_files(root)
    rules = _rules(root)
    found = []
    for rel in tracked:
        parts = rel.split("/")
        if rel == IGNORE_FILE or any(_ignored(rules, "/".join(parts[:i]), i < len(parts)) for i in range(1, len(parts) + 1)):
            continue
        _refuse_links(root, rel)
        found.append(rel)
    return found


def _zip(target: Path, entries) -> Path:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(entries):
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return target


def build(root: Path, out_dir: Path) -> Tuple[Path, Path]:
    root = Path(root)
    manifest = (root / "extension.yml").read_text(encoding="utf-8")
    found = {k: re.search(r'(?m)^  %s: "([^"]+)"' % k, manifest) for k in ("id", "version")}
    for k, hit in found.items():
        if hit is None:
            raise BuildError("extension.yml has no quoted " + k)
    ext_id, version = found["id"].group(1), found["version"].group(1)
    files = included_files(root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {rel: (root / rel).read_bytes() for rel in files}
    ext = _zip(out_dir / "{}-{}.zip".format(ext_id, version), data.items())
    wf = _zip(out_dir / WORKFLOW_ZIP, [(rel[len(WORKFLOW_DIR):], b) for rel, b in data.items() if rel.startswith(WORKFLOW_DIR)])
    return ext, wf


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="build_bundle.py")
    ap.add_argument("--out", required=True, help="output directory, outside the repository")
    args = ap.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    out = Path(args.out).resolve()  # D-020: a zip in the work tree is one `git add` away from a commit
    # samefile: `resolve()` keeps the letter case, and on a case-insensitive disk `/REPO/x` is inside `/repo` (DF3 m1)
    if any(p.exists() and os.path.samefile(p, root) for p in (out, *out.parents)):
        ap.error("--out must be a directory outside the repository")
    try:
        for p in build(root, out):
            print(p)
    except BuildError as e:
        print("build_bundle: " + str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
