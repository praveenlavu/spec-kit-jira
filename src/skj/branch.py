"""`skj branch KEY [--apply]`: name the local feature branch `<branch_prefix><KEY>-<slug>` (local git only, no Jira write).

The key stays UPPERCASE: Jira's smart commits and development-panel linking match the key case-sensitively in a branch
name, so `feature/spk-12-x` would not link. The slug comes from the summary, which is untrusted text.
"""
import re
import unicodedata

from . import config, keys

SLUG_MAX = 40
NAME_MAX = 100  # whole branch name; the slug is shortened (or dropped) to fit under a long prefix
_FORBIDDEN_IN_PART = ("..", "@{", "//")


def slug(title, limit=SLUG_MAX):
    """ASCII-folded, lowercase `[a-z0-9-]`, single dashes, none at the ends, at most `limit` chars, cut at a word
    boundary when it has one. May be empty (a title with no ASCII letters or digits)."""
    folded = unicodedata.normalize("NFKD", title if isinstance(title, str) else "").encode("ascii", "ignore").decode().lower()
    full = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    cut = full[:limit]
    if len(full) > limit and full[limit] != "-" and "-" in cut:  # mid-word: drop the partial word
        cut = cut.rsplit("-", 1)[0]
    return cut.rstrip("-")


def check_ref(name):
    """ValueError unless `name` is a safe branch name: stricter than `git check-ref-format --branch` (ASCII only,
    no space or control character, none of `~^:?*[\\`, no `..`, `@{`, `//`, leading `-`, trailing `.` or `/`, no
    component starting with `.` or ending in `.lock`, and at most NAME_MAX characters)."""
    ok = (isinstance(name, str) and 0 < len(name) <= NAME_MAX and re.fullmatch(r"[\x21-\x7e]+", name)
          and not re.search(r"[~^:?*\[\\]", name) and name[0] not in "-/" and name[-1] not in "./" and name not in ("@", "HEAD")
          and not any(bad in name for bad in _FORBIDDEN_IN_PART)
          and not any(part.startswith(".") or part.endswith(".lock") for part in name.split("/")))
    if not ok:
        raise ValueError("unsafe branch name (check branch_prefix in skj.config.json)")
    return name


def branch_name(prefix, key, summary):
    """`<prefix><KEY>-<slug>`, or `<prefix><KEY>` when the slug is empty. The key must already be a valid UPPERCASE key."""
    if not keys.valid_key(key):
        raise ValueError("invalid issue key")
    if not config.valid_branch_prefix(prefix):
        raise ValueError("unsafe branch_prefix")
    base = prefix + key
    check_ref(base)
    tail = slug(summary, min(SLUG_MAX, NAME_MAX - len(base) - 1))
    name = check_ref(base + "-" + tail if tail else base)
    if story_from_branch(name) != key:  # e.g. prefix `feature/SPK-9/` or `story` (-> `storySPK-5`): the name must read back as `key`
        raise ValueError("branch_prefix hides the key; choose a prefix that ends in a separator and holds no other key")
    return name


def story_from_branch(branch):
    """The uppercase key in a branch name (the first key that is not part of a longer token), else None. A lowercase key
    (`feature/spk-12-x`) is not recognised; `XSPK-12` is the key XSPK-12, never SPK-12; `SPK-12abc` is not a key."""
    m = keys.KEY_IN_TEXT_RE.search(branch) if isinstance(branch, str) else None
    return m.group(0) if m else None


def exists(root, name):
    r = config._git(root, "show-ref", "--verify", "--quiet", "refs/heads/" + name)
    return r is not None and r.returncode == 0


def create(root, name):
    """`git switch -c <name>` in `root`. The name passed check_ref, so it cannot parse as an option."""
    if story_from_branch(check_ref(name)) is None:
        raise ValueError("branch name holds no issue key")
    r = config._git(root, "switch", "-c", name)
    if r is None or r.returncode:
        err = ((r.stderr.decode("utf-8", "replace").strip().splitlines() or ["git failed"])[-1] if r else "git did not run")
        raise ValueError("git switch -c failed: " + re.sub(r"[^\x20-\x7e]", "?", err)[:200])
