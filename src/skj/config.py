"""skj.config.json: one validator for the working tree and the target branch, the trust keys (X4) and the site pin (K7).

The config comes from the pull request in CI, so every message names a key path or a position, never a value.
`approvers`, `approval` and `unattended` (TRUST_KEYS) and the pinned `site` are believed only from the target
branch (`trusted`), read with git; the working-tree values of those keys are never trusted.
"""
import collections
import errno
import json
import os
import re
import stat
import subprocess
import threading
import unicodedata
from pathlib import Path

from .keys import ACCOUNT_RE, LABEL_RE, PROJECT_RE, SITE_RE

CONFIG_NAME = "skj.config.json"
REVIEW_SEVERITIES = ("blocker", "major", "minor")  # review_block_on entries (case-sensitive)
MODES = ("advisory", "strict")
STRICT_GATE_BLOCK = ("ac_traceability", "assertion", "open_questions", "commits_without_story", "smart_commit_commands")  # gate.block floor in strict mode
STRICT_RULES_GATE_BLOCK = ("rule_coverage", "rule_drift")  # added to that floor when rules are enabled
STRICT_REVIEW_BLOCK = ("blocker",)  # review_block_on floor in strict mode
STRICT_DEFAULTS = {"test_command": None, "test_timeout": 300}
RULES_DEFAULTS = {"enabled": False, "types": ["Epic"]}  # business rules are read from the level-1 parent; off by default
TRANSITIONS_DEFAULTS = {"active": None, "done": None}  # None: chosen by status category (K3); a name: that transition
MAX_REF_CONFIG = 1024 * 1024  # bytes, for every reader of config text (working tree and git ref): the PR controls it; the same cap as the ledger
MAX_DIGITS = 1000  # a private copy of stack.MAX_DIGITS (stack imports config): CPython's int() is quadratic in the digits
MAX_APPROVERS = 20
MAX_LABELS = 20
MAX_TYPE_NAME = 64  # story_type, subtask_type, rules.types entries
MAX_HEADING = 200  # ac_heading
MAX_TRANSITION_NAME = 100
MAX_BRANCH_PREFIX = 100
APPROVAL_MODES = ("terminal", "comment")
UNATTENDED_DEFAULTS = {"assume_after_hours": None, "approval_requests": False}
TRUST_KEYS = ("approvers", "approval", "unattended")  # read only from the target branch (`trusted`), never from the working tree
REF_LABEL = CONFIG_NAME + " at the target branch"  # names the file in messages, never the ref (it is a CLI value)
GIT_TIMEOUT = 30  # seconds per git call

BRANCH_PREFIX_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
# git resolves a short name `x/...` as refs/x/..., refs/heads/x/..., ...: a branch under these would shadow or be shadowed by another ref (D-013)
_REF_NAMESPACES = ("refs", "heads", "tags", "remotes")
TOP_KEYS = ("site", "project", "story_type", "subtask_type", "ac_heading", "transitions", "branch_prefix", "labels", "gate",
            "review_block_on", "stack", "mode", "strict", "rules", "approvers", "approval", "unattended")


def valid_branch_prefix(prefix):
    """Empty, or a git-ref-safe start of a branch name (`<prefix><KEY>-<slug>` must stay a valid ref)."""
    return isinstance(prefix, str) and len(prefix) <= MAX_BRANCH_PREFIX and (prefix == "" or (
        BRANCH_PREFIX_RE.fullmatch(prefix) is not None
        and not any(bad in prefix for bad in ("..", "//", "/.", ".lock/"))
        and prefix.split("/", 1)[0].lower() not in _REF_NAMESPACES))


_HARMLESS_FORMAT = "\u200c\u200d\u200e\u200f"  # ZWNJ, ZWJ, LRM, RLM: Persian/Indic names, emoji sequences, mixed-RTL text


_FIELDS = frozenset({  # fixed config field names: safe to print
    "site", "project", "story_type", "subtask_type", "ac_heading", "transitions", "active", "done", "branch_prefix",
    "labels", "gate", "tests", "block", "review_block_on", "stack", "kit", "phases", "mode", "strict", "test_command",
    "test_timeout", "rules", "enabled", "types", "approvers", "approval", "unattended", "assume_after_hours",
    "approval_requests"})


def _has_control(s):
    """Control (Cc), line/paragraph separators (Zl, Zp), surrogates, private use and format characters (bidi overrides
    and isolates included), except the four joiner/direction marks above. Cn (unassigned) is deliberately accepted:
    it depends on the Unicode DB version."""
    return any(unicodedata.category(c) in ("Cc", "Zl", "Zp", "Cs", "Co")
               or (unicodedata.category(c) == "Cf" and c not in _HARMLESS_FORMAT) for c in s)


def reject_control_chars(node, where, path):
    """No string (value or key, any depth) may carry a control character (it could start a new CI log line or reorder
    what a reviewer sees). `where` is the key path ("" at the top). Names the key path, never the offending text."""
    if isinstance(node, str):
        if _has_control(node):
            raise ConfigInvalid(f"{path}: {where} contains a control character")
    elif isinstance(node, dict):
        for n, (k, v) in enumerate(node.items(), 1):
            if _has_control(k):
                raise ConfigInvalid(f"{path}: {where or 'top level'}: a key contains a control character")
            # a key that is not one of the fixed field names is user text: name it by position
            label = k if k in _FIELDS else f"<entry #{n}>"
            reject_control_chars(v, f"{where}.{label}" if where else label, path)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            reject_control_chars(v, f"{where}[{i}]", path)


def _int(digits):
    if len(digits) > MAX_DIGITS:
        raise ValueError(f"a number is longer than {MAX_DIGITS} digits")
    return int(digits)


class ConfigError(Exception):
    """No usable skj.config.json found."""


class ConfigInvalid(ConfigError):
    """A config file is present but invalid (explicit misconfiguration; CI fails)."""


class AuthRefused(Exception):
    """K7: the site in use is not the target branch's pinned site, so no credential may be attached to a request."""


def _name(v, cap):
    """A non-empty, not-blank string of at most `cap` characters (control characters are refused before this)."""
    return isinstance(v, str) and 0 < len(v) <= cap and v.strip() != ""


def _unknown(obj, valid, what, path):
    for n, k in enumerate(obj, 1):
        if k not in valid:  # named by position: the key is PR-controlled text
            raise ConfigInvalid(f"{path}: {what} entry #{n}: unknown key (valid: {', '.join(valid)})")


def _read_regular(path, label, follow=False):
    """The bytes of the regular file `path`, at most MAX_REF_CONFIG of them, else ConfigInvalid(f"{label}: ...") with a fixed text. Opened
    non-blocking (and without following a final symlink unless `follow`) and tested after the open, so a FIFO is never waited on and a big
    file or a device is never read whole."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | (0 if follow else getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno == errno.ELOOP and not follow:
            raise ConfigInvalid(f"{label}: not a regular file (a symlink, directory, FIFO or device is refused)") from None
        raise ConfigInvalid(f"{label}: cannot read ({e.strerror})") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):  # tested on the descriptor: fdopen itself refuses a directory with another text
        os.close(fd)
        raise ConfigInvalid(f"{label}: not a regular file (a symlink, directory, FIFO or device is refused)")
    with os.fdopen(fd, "rb") as f:
        try:
            return f.read(MAX_REF_CONFIG + 1)  # one byte over the cap is enough for `_parse` to refuse
        except OSError as e:
            raise ConfigInvalid(f"{label}: cannot read ({e.strerror})") from None


def _parse(raw, label):
    """The JSON value of config bytes `raw`: at most MAX_REF_CONFIG bytes, UTF-8, no integer longer than MAX_DIGITS, nested no deeper than
    the parser allows. ConfigInvalid(f"{label}: ...") otherwise, whose text names a position, never the content. Duplicate keys: the last wins."""
    if len(raw) > MAX_REF_CONFIG:
        raise ConfigInvalid(f"{label}: the file is larger than 1 MiB")
    try:
        return json.loads(raw.decode("utf-8"), parse_int=_int)
    except UnicodeDecodeError:
        raise ConfigInvalid(f"{label}: invalid JSON: not UTF-8") from None
    except ValueError as e:  # a JSONDecodeError (its text is a position) or the digit guard's fixed text
        raise ConfigInvalid(f"{label}: invalid JSON: {e}") from None
    except RecursionError:
        raise ConfigInvalid(f"{label}: nested too deeply") from None


class Config:
    def __init__(self, site, project, story_type="Story", subtask_type="Subtask", ac_heading="Acceptance criteria",
                 transitions=None, branch_prefix="feature/", labels=None, gate=None, review_block_on=None, stack=None,
                 mode="advisory", strict=None, rules=None, approvers=None, approval="terminal", unattended=None):
        self.site = site
        self.project = project
        self.story_type = story_type
        self.subtask_type = subtask_type
        self.ac_heading = ac_heading
        self.transitions = {**TRANSITIONS_DEFAULTS, **(transitions or {})}
        self.branch_prefix = branch_prefix
        self.labels = list(labels or [])
        self.gate = {"tests": {}, "block": [], **(gate or {})}  # partial gate keeps defaults
        self.review_block_on = list(review_block_on or [])
        self.stack = dict(stack or {})
        self.mode = mode
        self.strict = {**STRICT_DEFAULTS, **(strict or {})}
        self.rules = {**RULES_DEFAULTS, **(rules or {})}
        self.rules["types"] = list(self.rules["types"])
        self.approvers = list(approvers or [])  # accountIds; only the target branch's value may be trusted (`trusted`)
        self.approval = approval
        self.unattended = {**UNATTENDED_DEFAULTS, **(unattended or {})}
        self.source_commit = None  # the 40-hex target commit load_from_ref resolved; None for a config read from disk
        self.from_target = False  # True when CI took this config from the PR's target branch (`strict` / `gate` with a base)
        self.target_profile_blob = None  # skj.stack.json / skj.kits.lock.json bytes at source_commit (strict.read_target_trust); None: absent
        self._target_lock_blob = None
        self.target_lock_read = False  # True once target_lock_blob was set (strict.read_target_trust ran): None then means "the target has none"

    @property
    def target_lock_blob(self):
        return self._target_lock_blob

    @target_lock_blob.setter
    def target_lock_blob(self, raw):
        self._target_lock_blob, self.target_lock_read = raw, True

    def _effective(self, project_list, floor):
        out = list(project_list)
        if self.mode == "strict":
            out += [x for x in floor if x not in out]
        return out

    def effective_gate_block(self):
        """gate.block in force: the project's list, plus the strict floor when mode is strict (and the rule checks when rules are enabled)."""
        return self._effective(self.gate["block"], STRICT_GATE_BLOCK + (STRICT_RULES_GATE_BLOCK if self.rules["enabled"] else ()))

    def effective_review_block_on(self):
        """review_block_on in force: the project's list, plus the strict floor when mode is strict."""
        return self._effective(self.review_block_on, STRICT_REVIEW_BLOCK)

    @classmethod
    def _from_data(cls, data, path):
        """The one validator behind `load` and `load_from_ref`; `path` only labels the messages (the PR chooses the content: never the text)."""
        try:
            return cls._validate(data, path)
        except RecursionError:  # a value nested too deeply for the validator
            raise ConfigInvalid(f"{path}: nested too deeply") from None

    @classmethod
    def _validate(cls, data, path):
        if not isinstance(data, dict):
            raise ConfigInvalid(f"{path}: top level must be a JSON object")
        reject_control_chars(data, "", path)
        _unknown(data, TOP_KEYS, "top level", path)  # D-002: a mistyped key (a trust key included) must not be ignored
        missing = [k for k in ("site", "project") if k not in data]
        if missing:
            raise ConfigInvalid(f"{path}: missing required key(s): {', '.join(missing)}")
        if not (isinstance(data["site"], str) and SITE_RE.fullmatch(data["site"])):
            raise ConfigInvalid(f"{path}: 'site' must be a Jira Cloud host <name>.atlassian.net (lowercase, no scheme or path)")
        if not (isinstance(data["project"], str) and PROJECT_RE.fullmatch(data["project"])):
            raise ConfigInvalid(f"{path}: 'project' must be a project key: an uppercase letter, then 1-9 uppercase letters, digits or '_'")
        for k in ("gate", "stack", "rules", "unattended", "transitions", "strict"):
            if k in data and not isinstance(data[k], dict):
                raise ConfigInvalid(f"{path}: '{k}' must be a JSON object")
        for k, cap in (("story_type", MAX_TYPE_NAME), ("subtask_type", MAX_TYPE_NAME), ("ac_heading", MAX_HEADING)):
            if k in data and not _name(data[k], cap):
                raise ConfigInvalid(f"{path}: '{k}' must be a non-blank string of at most {cap} characters")
        transitions = data.get("transitions", {})
        _unknown(transitions, tuple(TRANSITIONS_DEFAULTS), "transitions", path)
        for k, v in transitions.items():
            if v is not None and not _name(v, MAX_TRANSITION_NAME):
                raise ConfigInvalid(f"{path}: 'transitions.{k}' must be null or a transition name of at most "
                                    f"{MAX_TRANSITION_NAME} characters")
        if not valid_branch_prefix(data.get("branch_prefix", "feature/")):
            raise ConfigInvalid(f"{path}: 'branch_prefix' must be empty or a git-ref-safe string of at most {MAX_BRANCH_PREFIX} "
                                "characters (letters, digits, '.', '_', '-', '/'; no '..', '//', '/.', '.lock/'; not starting with "
                                "refs/, heads/, tags/ or remotes/)")
        labels = data.get("labels", [])
        if not (isinstance(labels, list) and len(labels) <= MAX_LABELS):
            raise ConfigInvalid(f"{path}: 'labels' must be a list of at most {MAX_LABELS} labels")
        for i, lab in enumerate(labels):
            if not (isinstance(lab, str) and LABEL_RE.fullmatch(lab)):
                raise ConfigInvalid(f"{path}: labels[{i}]: must match {LABEL_RE.pattern}")
        gate = data.get("gate", {})
        if "tests" in gate and not isinstance(gate["tests"], dict):
            raise ConfigInvalid(f"{path}: 'gate.tests' must be a JSON object")
        block = gate.get("block", [])
        if not (isinstance(block, list) and all(isinstance(b, str) for b in block)):
            raise ConfigInvalid(f"{path}: 'gate.block' must be a list of strings")
        if gate.get("tests") or block:
            cls._validate_gate_entries(gate.get("tests", {}), block, path)
        rbo = data.get("review_block_on", [])
        if not (isinstance(rbo, list) and all(isinstance(r, str) for r in rbo)):
            raise ConfigInvalid(f"{path}: 'review_block_on' must be a list of strings")
        for i, r in enumerate(rbo):
            if r not in REVIEW_SEVERITIES:
                raise ConfigInvalid(f"{path}: review_block_on[{i}]: unknown severity "
                                    f"(valid: {', '.join(REVIEW_SEVERITIES)})")
        if "mode" in data and data["mode"] not in MODES:
            raise ConfigInvalid(f"{path}: 'mode' must be \"advisory\" or \"strict\"")
        strict = data.get("strict", {})
        _unknown(strict, tuple(STRICT_DEFAULTS), "strict", path)
        cmd = strict.get("test_command")
        if "test_command" in strict and not (isinstance(cmd, str) and 0 < len(cmd) <= 500):
            raise ConfigInvalid(f"{path}: 'strict.test_command' must be a string of 1-500 characters")
        tt = strict.get("test_timeout")
        if "test_timeout" in strict and not (isinstance(tt, int) and not isinstance(tt, bool) and 1 <= tt <= 3600):
            raise ConfigInvalid(f"{path}: 'strict.test_timeout' must be an integer from 1 to 3600")
        rules = data.get("rules", {})
        _unknown(rules, tuple(RULES_DEFAULTS), "rules", path)
        if "enabled" in rules and not isinstance(rules["enabled"], bool):
            raise ConfigInvalid(f"{path}: 'rules.enabled' must be true or false")
        types = rules.get("types")
        if "types" in rules:
            if not (isinstance(types, list) and 1 <= len(types) <= 10
                    and all(isinstance(t, str) and 0 < len(t) <= MAX_TYPE_NAME for t in types)):
                raise ConfigInvalid(f"{path}: 'rules.types' must be a list of 1-10 non-empty strings of at most {MAX_TYPE_NAME} characters")
            story = data.get("story_type", "Story").casefold()
            for i, t in enumerate(types):
                if t.casefold() == story:
                    raise ConfigInvalid(f"{path}: rules.types[{i}]: a story type cannot carry rules (it is 'story_type')")
        approvers = cls._validate_trust_keys(data, path)
        return cls(
            site=data["site"],
            project=data["project"],
            story_type=data.get("story_type", "Story"),
            subtask_type=data.get("subtask_type", "Subtask"),
            ac_heading=data.get("ac_heading", "Acceptance criteria"),
            transitions=transitions,
            branch_prefix=data.get("branch_prefix", "feature/"),
            labels=labels,
            gate=data.get("gate"),
            review_block_on=rbo,
            stack=data.get("stack"),
            mode=data.get("mode", "advisory"),
            strict=strict,
            rules=rules,
            approvers=approvers,
            approval=data.get("approval", "terminal"),
            unattended=data.get("unattended"),
        )

    @staticmethod
    def _validate_gate_entries(tests, block, path):
        """gate.tests globs and gate.block names are the gate module's vocabulary; until it is ported (L3) a config that
        sets them is refused rather than accepted unchecked."""
        try:
            from .gate import GATE_CHECKS, valid_glob  # local: gate imports config
        except ModuleNotFoundError as e:
            if e.name != __package__ + ".gate":
                raise
            raise ConfigInvalid(f"{path}: 'gate.tests' and 'gate.block' entries are not available in this build") from None
        for n, (glob, rx) in enumerate(tests.items(), 1):
            # entries are named by position: the glob is PR-controlled text and must not reach the CI log
            if not (isinstance(rx, str) and 0 < len(rx) <= 500):
                raise ConfigInvalid(f"{path}: 'gate.tests' entry #{n}: the value must be a regex string of 1-500 characters")
            if not valid_glob(glob):
                raise ConfigInvalid(f"{path}: 'gate.tests' entry #{n}: the glob must be 1-200 characters "
                                    "with balanced '[...]' classes")
            try:
                re.compile(rx)
            except re.error:
                raise ConfigInvalid(f"{path}: 'gate.tests' entry #{n}: the regex is invalid") from None
        for i, b in enumerate(block):
            if b not in GATE_CHECKS:
                raise ConfigInvalid(f"{path}: gate.block[{i}]: unknown check (valid: {', '.join(GATE_CHECKS)})")

    @staticmethod
    def _validate_trust_keys(data, path):
        """`approvers`, `approval`, `unattended`. Errors name the key and the position, never a value. Returns the
        approvers exactly as written (accountIds are case-sensitive: nothing is normalised). A duplicate is rejected."""
        approvers = data.get("approvers", [])
        if not (isinstance(approvers, list) and len(approvers) <= MAX_APPROVERS):
            raise ConfigInvalid(f"{path}: 'approvers' must be a list of at most {MAX_APPROVERS} accountIds")
        out = []
        for i, a in enumerate(approvers):
            if not (isinstance(a, str) and ACCOUNT_RE.fullmatch(a)):
                raise ConfigInvalid(f"{path}: approvers[{i}]: not a Jira accountId (1-128 letters, digits, ':', '_' or '-')")
            if a in out:
                raise ConfigInvalid(f"{path}: approvers[{i}]: duplicate of an earlier entry")
            out.append(a)
        if "approval" in data and data["approval"] not in APPROVAL_MODES:
            raise ConfigInvalid(f"{path}: 'approval' must be \"terminal\" or \"comment\"")
        unattended = data.get("unattended", {})
        _unknown(unattended, tuple(UNATTENDED_DEFAULTS), "unattended", path)
        hours = unattended.get("assume_after_hours")
        if "assume_after_hours" in unattended and not (isinstance(hours, int) and not isinstance(hours, bool) and 1 <= hours <= 720):
            raise ConfigInvalid(f"{path}: 'unattended.assume_after_hours' must be an integer from 1 to 720")
        if "approval_requests" in unattended and not isinstance(unattended["approval_requests"], bool):
            raise ConfigInvalid(f"{path}: 'unattended.approval_requests' must be true or false")
        return out

    @classmethod
    def load_from_ref(cls, ref, repo="."):
        """The config at `ref` (the PR's target branch), read with git and checked by the same validator as `load`: a Config, or None
        when the repository has no skj.config.json at that ref. Anything else (a ref that is no commit, git missing or failing, a
        non-regular or over 1 MiB file, bad content) raises ConfigInvalid with text that never echoes the ref: CI fails closed.
        `ref` is resolved as refs/remotes/<ref>, refs/heads/<ref>, or a full commit id; never a tag."""
        def fail(why):
            return ConfigInvalid(f"{REF_LABEL}: {why}")
        commit = _resolve_base(repo, ref) if isinstance(ref, str) and ref else ""
        if not commit:
            raise fail("the base ref is not a commit in this repository (or git could not be run); fetch it (fetch-depth: 0)")
        raw = _blob_at(repo, commit, CONFIG_NAME, MAX_REF_CONFIG, REF_LABEL)
        if raw is None:
            return None  # the path does not exist at that commit
        cfg = cls._from_data(_parse(raw, REF_LABEL), REF_LABEL)
        cfg.source_commit = commit
        return cfg

    @classmethod
    def load(cls, start_dir=None):
        candidates = []
        env_path = os.environ.get("SKJ_CONFIG")
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(Path(start_dir or os.getcwd()) / CONFIG_NAME)
        for path in candidates:
            if os.path.lexists(path):  # anything at the path is read or refused (a link, FIFO, directory, device), never skipped
                return cls._from_data(_parse(_read_regular(path, path), path), path)
        raise ConfigError(
            "No skj config found. Run `skj setup` to create skj.config.json here, "
            "or point $SKJ_CONFIG at a config file."
        )

    @staticmethod
    def trusted(repo=".", base=None):
        return trusted(repo, base)


# ---- git: list argv, no shell, a scrubbed environment ----
# shortcut: a minimal copy of the git helpers the fork keeps in strict.py / prs.py; L5 ports strict.py and can share these.
# replace refs and old-style grafts rewrite history as git reports it: never let them hide a change
_GIT_ENV = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0"}
# H1a: an allow-list of what git, its ssh/credential helpers and hooks need; everything else stays behind: any credential, GIT_ASKPASS,
# GIT_SSH*, GIT_EXTERNAL_DIFF, SKJ_*, GH_TOKEN (gh gets its own `prs.gh_env`), and every selector or config injection (a hook's
# GIT_INDEX_FILE, an outer GIT_DIR, GIT_CONFIG_PARAMETERS/COUNT/KEY_*) that could point git at another repository or index
_GIT_ENV_ALLOW = frozenset((
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TMPDIR", "TZ", "EMAIL", "SSH_AUTH_SOCK", "SSH_AGENT_PID",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "no_proxy", "all_proxy",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_CEILING_DIRECTORIES", "GIT_TEST_ASSUME_DIFFERENT_OWNER",
    # the toolchain a hook (or a resumed workflow's test run, approvals._resume) needs: strict's runner allow-list
    "VIRTUAL_ENV", "JAVA_HOME", "GOPATH", "NODE_PATH", "GOROOT", "FLUTTER_ROOT", "DOTNET_ROOT", "ANDROID_HOME", "ANDROID_SDK_ROOT",
    "RUSTUP_HOME",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "PROGRAMDATA",
    # the standard locale names, not any LC_* (an LC_SFDX_AUTH_URL holds a credential)
    "LC_ADDRESS", "LC_ALL", "LC_COLLATE", "LC_CTYPE", "LC_IDENTIFICATION", "LC_MEASUREMENT", "LC_MESSAGES", "LC_MONETARY", "LC_NAME",
    "LC_NUMERIC", "LC_PAPER", "LC_TELEPHONE", "LC_TIME"))


def git_env(base=None):
    """The environment for a git run: only `_GIT_ENV_ALLOW` (no repository selector, config injection, helper command or credential),
    replace refs and grafts off, no pager, no prompt."""
    return {**{k: v for k, v in (os.environ if base is None else base).items() if k in _GIT_ENV_ALLOW}, **_GIT_ENV}


def _argv(repo, args):
    return ["git", "--no-pager", "--no-replace-objects", "-c", "core.fsmonitor=false", "-C", str(repo), *args]


def _git(repo, *args):
    """A finished git run (bytes), or None when git is missing, cannot start or runs past GIT_TIMEOUT: a failure, never a pass."""
    try:
        return subprocess.run(_argv(repo, args), check=False, capture_output=True, timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL, env=git_env())
    except (OSError, subprocess.SubprocessError):
        return None


def _kill(proc):
    try:
        proc.kill()
    except OSError:
        pass


def _git_capped(repo, cap, *args):
    """stdout of a successful git run of at most `cap` bytes, else None (git missing or failing, past GIT_TIMEOUT, or more output: the pipe
    is read to `cap` + 1 bytes and git is killed, so a big blob is never held whole)."""
    try:
        proc = subprocess.Popen(_argv(repo, args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=git_env())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    timer = threading.Timer(GIT_TIMEOUT, _kill, (proc,))
    timer.start()
    try:
        out = proc.stdout.read(cap + 1)
        if len(out) > cap:
            _kill(proc)
        proc.stdout.close()
        code = proc.wait()
    finally:
        timer.cancel()
    return out if code == 0 and len(out) <= cap else None


def _out(repo, *args):
    """stdout (bytes) of a successful git run, else None."""
    r = _git(repo, *args)
    return r.stdout if r is not None and r.returncode == 0 else None


def _rev(repo, rev):
    return (_out(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}") or b"").decode().strip()


def _commit(repo, sha):
    """Full id of the one commit whose id starts with the hex `sha`, else None. Object lookup only: a branch or tag
    named like the SHA never stands in, and an ambiguous prefix or a non-commit object is refused."""
    out = _out(repo, "rev-parse", f"--disambiguate={sha.lower()}")
    objs = out.decode("ascii", "replace").split() if out is not None else []
    return objs[0] if len(objs) == 1 and _out(repo, "cat-file", "-t", objs[0]) == b"commit\n" else None


DEFAULT_NAMES = ("refs/remotes/origin/main", "refs/remotes/origin/master")  # the conventional default branches origin/HEAD is checked against
E_TARGET_MOVED = ("target-moved: origin/HEAD does not point at origin/main (or origin/master when there is no origin/main), which is in strict mode or comment approval, "
                  "so the local origin/HEAD cannot be believed: run `git remote set-head origin --auto` (it asks the remote) and try again")


def _guarded(cfg):
    """D-023: is a default branch's config one that origin/HEAD must not move away from (strict mode or comment approval)?"""
    return cfg is not None and (cfg.mode == "strict" or cfg.approval == "comment")


def default_base(repo):
    """The ref the target trust is read from (DF5 D8): the home ref (below) when it is in strict mode or comment approval and
    origin/HEAD is unset or (in CI) names another existing branch; else the full ref `origin/HEAD` points at (`refs/remotes/origin/<b>`),
    or "" when it is not set. D-023: one local `git remote
    set-head` must not pick a weaker target. The ONE home ref is origin/main when it exists, else origin/master (DF4 N1: never
    "either", so a forged local origin/master beside origin/main is never accepted, and a stale origin/master is never read then).
    When the home ref is in strict mode or comment approval, origin/HEAD must be exactly it (ConfigInvalid `target-moved`: another
    branch with the same trust keys could still carry weaker gate settings, test command, stack profile or lockfile), and when
    origin/HEAD is not set the home ref is used, in CI and locally alike (DF3 M2). DF4 N6: in CI, an origin/HEAD that names another
    existing branch (the templates point it at the PR's target) is not refused: the guarded home ref is returned, so trust is read
    from it and `--base` is compared with it (the stricter side). CI=1 set locally looks the same: the guard is against drift and
    mistakes, not against whoever controls the clone and its environment.
    Residual (documented): creating, rewriting or deleting any refs/remotes/origin/* ref locally changes what the local CLI trusts."""
    out = _out(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    ref = out.decode("utf-8", "replace").strip() if out else ""
    home = next((n for n in DEFAULT_NAMES if _rev(repo, n)), "")
    if home and ref != home and _guarded(Config.load_from_ref(home, repo=repo)):
        if ref and not (in_ci() and _rev(repo, ref)):
            raise ConfigInvalid(E_TARGET_MOVED)
        return home
    return ref


def ci_head(repo):
    """DF5 D1: in CI, `origin/<b>` when origin/HEAD names an existing branch other than the one `default_base` returns (the ci/ templates
    point it at the PR's target), else "". A judging verb run without --base takes it as its base, so the stricter-of rule runs."""
    if not in_ci():
        return ""
    out = _out(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    ref = out.decode("utf-8", "replace").strip() if out else ""
    if not ref.startswith("refs/remotes/origin/") or not _rev(repo, ref) or ref == default_base(repo):
        return ""
    return ref[len("refs/remotes/"):]


def _resolve_base(repo, base):
    """Full commit id of `base`: `refs/remotes/<base>`, then `refs/heads/<base>`, then a full 40-hex commit id (or a full
    `refs/heads/` or `refs/remotes/` name). Never a tag or a short name: a tag named like a branch must not move it. "" when none."""
    if base.startswith(("refs/heads/", "refs/remotes/")):
        return _rev(repo, base)
    full = _rev(repo, f"refs/remotes/{base}") or _rev(repo, f"refs/heads/{base}")
    return full or (_commit(repo, base) or "" if re.fullmatch(r"[0-9a-f]{40}", base) else "")


def _blob_at(repo, commit, path, cap, label):
    """The bytes of regular file `path` at the already-resolved 40-hex `commit` (ValueError otherwise), or None when the path does
    not exist there. Anything else (git missing or failing, a symlink, submodule or directory, more than `cap` bytes) raises
    ConfigInvalid(f"{label}: ...") with a fixed text that never echoes the path or the commit: CI fails closed."""
    if not (isinstance(commit, str) and re.fullmatch(r"[0-9a-fA-F]{40}", commit)):
        raise ValueError("commit must be a resolved 40-hex commit id")

    def fail(why):
        return ConfigInvalid(f"{label}: {why}")
    listed = _git(repo, "ls-tree", "-l", "--full-tree", "--end-of-options", commit, "--", path)
    if listed is None or listed.returncode:
        raise fail("git could not read the tree")
    if not listed.stdout.strip():
        return None
    meta = listed.stdout.decode("utf-8", "replace").split("\t", 1)[0].split()
    if len(meta) != 4 or meta[0] not in ("100644", "100755") or meta[1] != "blob" or not meta[3].isdigit():
        raise fail("the file is not a regular file")
    limit = f"{cap // 1048576} MiB" if cap % 1048576 == 0 else f"{cap} bytes"
    if int(meta[3]) > cap:
        raise fail(f"the file is larger than {limit}")
    shown = _git_capped(repo, cap, "show", "--no-textconv", "--end-of-options", f"{commit}:{path}")
    if shown is None:
        raise fail("git could not read the file")
    return shown


# ---- trusted(): the target branch's trust keys and site ----
# site: the target's pinned site, None when no target config was read; base_known: a target branch was resolved and read
Trust = collections.namedtuple("Trust", "approvers approval unattended site base_known")
_BAD_BASE = re.compile(r"[~^:\s\x00-\x1f\x7f]|@\{|\.\.|^-")  # a revision expression, a range or an option, not a plain ref name


E_NO_TARGET = ("the config file sets approvers, approval or unattended, which are read only from the target branch, "
               "and there is none to read: set origin/HEAD or pass --base")
E_NONROOT_TRUST = ("the config file in use is not the repo-root skj.config.json and sets approvers, approval or unattended: those keys are "
                   "read only from skj.config.json on the default branch; move them there")


def _has_trust_keys(p):
    """Does the regular file `p` (a symlink is followed: it is only searched for the three keys) hold a trust key? Over 1 MiB, unreadable or not a JSON
    object: ConfigInvalid (when it cannot be read, nobody can say it holds none)."""
    try:
        data = _parse(_read_regular(p, p, follow=True), p)
    except ConfigInvalid:
        data = None
    if not isinstance(data, dict):
        raise ConfigInvalid("the config file cannot be read as a JSON object of at most 1 MiB, so it cannot be checked for the approval keys")
    return any(k in data for k in TRUST_KEYS)


def _working_tree_has_trust_keys(repo):
    """True when a config file this process would read from the working tree holds a trust key (read the way `Config.load` finds it).
    A path that is no file is absent."""
    paths = [Path(repo) / CONFIG_NAME]
    if os.environ.get("SKJ_CONFIG"):
        paths.insert(0, Path(os.environ["SKJ_CONFIG"]))
    return any(p.is_file() and _has_trust_keys(p) for p in paths)


def _top_config(repo):
    """The git top level's skj.config.json (`repo`'s own when git cannot say)."""
    top = _out(str(repo), "rev-parse", "--show-toplevel")
    return Path(top.decode("utf-8", "replace").strip() if top else repo) / CONFIG_NAME


def _same_file(a, b):
    """Same file on disk (a symlink, `..`, another spelling or another letter case on a case-insensitive disk included); a missing one is not."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def trusted(repo=".", base=None):
    """`Trust(approvers, approval, unattended, site, base_known)` that may be believed: the TARGET branch's, never the working tree's (the PR edits that).
    `base` (CI: `--base`) or else `default_base(repo)` (`origin/HEAD`). A base is read with `load_from_ref` (a target without a
    config = the defaults and site None; anything unreadable raises ConfigInvalid). A base that is not a plain ref name (a revision
    expression such as `main@{1}` or `origin/main~1`, a range, whitespace, a leading `-`) is refused: it could pick an older target
    config. With NO base: the defaults when the working-tree config sets none of the three keys, else ConfigInvalid, because the
    keys could not be checked against anything. `default_base` refuses a moved origin/HEAD beside a guarded home ref (origin/main, else
    origin/master) and uses that ref when origin/HEAD is unset (D-023). Residual: creating, rewriting or deleting any
    `refs/remotes/origin/*` ref locally changes what the local CLI trusts; CI fetches fresh.

    The site pin (K7) sees three states: (a) `base_known` and `site` set: the target's site, which must equal the one in use;
    (b) `base_known` and `site` None: the target has no config (a target config without `site` is invalid, so it raises);
    (c) `base_known` False: no base could be found, so nothing was read; `site_pin_ok` refuses this state in CI."""
    # only the git top level's skj.config.json is read from the target: trust keys in the file `Config.load` reads instead would be dropped silently
    used = next((p for p in (Path(os.environ.get("SKJ_CONFIG") or "\0"), Path(repo) / CONFIG_NAME) if p.is_file()), None)
    if used is not None and not _same_file(used, _top_config(repo)) and _has_trust_keys(used):
        raise ConfigInvalid(E_NONROOT_TRUST)
    ref = base or default_base(str(repo))
    if ref:
        if not isinstance(ref, str) or _BAD_BASE.search(ref):
            raise ConfigInvalid("the base is not a plain branch or ref name (no revision expressions, ranges, spaces or leading dash)")
        cfg = Config.load_from_ref(ref, repo=str(repo))
    elif _working_tree_has_trust_keys(repo):
        raise ConfigInvalid(E_NO_TARGET)
    else:
        cfg = None
    site = cfg.site if cfg is not None else None
    cfg = cfg or Config("", "")  # no config at the target (or no base): the defaults
    return Trust(list(cfg.approvers), cfg.approval, dict(cfg.unattended), site, bool(ref))


# ---- K7: the site pin ----
PIN_ALLOWED = ("ok", "no-target-config", "no-base")  # the pin_status values that let credentials be attached
_PIN_REFUSAL = {
    "mismatch": "the site in skj.config.json differs from the site pinned on the target branch: credentials are not "
                "attached; change 'site' on the target branch first",
    "refused-ci-no-base": "no target branch could be read to check the site in skj.config.json against, and this is CI: "
                          "credentials are not attached; pass --base or set origin/HEAD",
    "invalid": "the site pin could not be checked: credentials are not attached",
}


def in_ci(env=None):
    """True when `CI` or `GITHUB_ACTIONS` (from `env`, default os.environ at call time) is non-empty and not "0"/"false"."""
    env = os.environ if env is None else env
    return any((env.get(k) or "").strip().lower() not in ("", "0", "false") for k in ("CI", "GITHUB_ACTIONS"))


def pin_status(cfg, trust, ci=None):
    """The site pin's state, a stable string (doctor prints it):
    "ok"                  base known, the target's site equals cfg.site;
    "mismatch"            base known, the target's site differs (refused);
    "no-target-config"    base known, the target branch has no skj.config.json (allowed: nothing to pin yet);
    "no-base"             no base could be resolved, outside CI (allowed, but the pin is not enforced: doctor WARNs);
    "refused-ci-no-base"  no base could be resolved, in CI (refused: the PR could choose the site);
    "invalid"             `trust` is not a Trust from `trusted` (refused).
    `ci` None means `in_ci()` read now."""
    if not (isinstance(trust, Trust) and isinstance(trust.base_known, bool)):
        return "invalid"
    if not trust.base_known:
        return "refused-ci-no-base" if (in_ci() if ci is None else ci) else "no-base"
    if trust.site is None:
        return "no-target-config"
    return "ok" if trust.site == cfg.site else "mismatch"


def site_pin_ok(cfg, trust, ci=None):
    """True when credentials may be attached for `cfg.site`. The three states (see `trusted`): (a) base known with a target
    site: only that exact site; (b) base known, no target config: allowed; (c) base unknown: refused in CI, allowed
    locally (pin_status "no-base"). Anything that is not a Trust is False (fail closed)."""
    return pin_status(cfg, trust, ci) in PIN_ALLOWED


def require_site_pin(cfg, trust, ci=None):
    """Raise AuthRefused unless `site_pin_ok`. The text names neither site (both are PR-reachable values)."""
    status = pin_status(cfg, trust, ci)
    if status not in PIN_ALLOWED:
        raise AuthRefused(_PIN_REFUSAL[status])


def build_client(cfg, trust, email, token, ci=None, **kw):
    """The one way to build an authenticated client from a config: the site pin is checked before the client exists."""
    require_site_pin(cfg, trust, ci)
    from . import client  # local: the client module stays importable without config
    return client.Client(cfg.site, email, token, **kw)
