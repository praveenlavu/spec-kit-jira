"""`skj doctor`: what is wrong with this setup, in rows `{id, status, fix}` (core rows, R3; the trust rows `approvers` and `edit-all`, A5).

Status is `ok`, `warn`, `fail` or `unknown`; `unknown` means a probe could not tell, and a probe that cannot tell never says `ok`.
Nothing here writes. Local reads: `git --version`, `specify --version` (scrubbed environment, a timeout, only a parsed version is
kept), the working-tree config, the target branch's config and the site pin (`config.trusted` / `config.pin_status`, the verbs' own
path). Jira, only once the site pin allows credentials: exactly one `GET /myself` (the `token` and `identity` rows) and, once that
answered, one `GET /project/<project>` (`project-type`, K1), and once that row is ok or warn, one `GET /mypermissions` for
EDIT_ALL_COMMENTS, DELETE_ALL_COMMENTS and DELETE_OWN_COMMENTS (`edit-all`). A 401 stops doctor at once: it is never retried, because
repeated failed logins can trigger a CAPTCHA. The trust rows read `approvers` and `approval` from the TARGET branch only: the Trust the
site-pin row got from `config.trusted(repo, base)` (`--base`, else origin/HEAD), never the working tree. `mode` changes neither row:
with approval "comment" on the target the `edit-all` warning is the fixed `approvals.TAMPER_SENTENCE` in advisory and strict mode alike,
and it is never a fail (owner decision: printed, not refused). Row texts are fixed: ids, counts, numeric versions, the configured project key and a cut accountId,
never a token, an email, a full accountId or server text (except the validated `X-Seraph-LoginReason` value).
Process help, not a security boundary: a hostile checkout can shape the local rows it controls, and `--base` is user-supplied and
not cross-checked (a local diagnostic; CI's own checks read the target themselves). With no base at all (`base_known` False) the
target was not read: `approvers` is unknown and `edit-all` makes no claim about the approval mode."""
import re
import subprocess

from . import approvals, config, issues, keys, secretstore
from .client import AuthError, ClientError, JiraError, NotFound
from .comments import short_id
from .config import Config, ConfigError, ConfigInvalid

GIT_FLOOR = (2, 24)  # `--end-of-options`
SPECKIT_FLOOR = (1, 2, 1)
TIMEOUT = 10  # seconds per git/specify call
ROW_IDS = ("git", "specify", "config", "config-target", "site-pin", "token", "identity", "project-type", "approvers", "edit-all")
EDIT_DELETE = ("EDIT_ALL_COMMENTS", "DELETE_ALL_COMMENTS", "DELETE_OWN_COMMENTS")  # an edit is detected, a deletion is not (A2)
_SPECIFY = re.compile(r"^specify(?:[ -]cli)?[ \t]+v?(\d+)\.(\d+)\.(\d+)", re.M | re.I)
_GIT = re.compile(r"^git version (\d+)\.(\d+)(?:\.(\d+))?", re.M)

NO_TOKEN = ("no Jira identity for this site: on macOS run the `security add-generic-password ... -w` line `skj setup` prints; "
            "elsewhere run `skj setup`; in CI set SKJ_EMAIL and SKJ_TOKEN")
HALF_SET = "only one of SKJ_EMAIL and SKJ_TOKEN is set: set both (CI), or unset both to use the Keychain identity"
BAD_SECRET = "the stored Jira identity cannot be used (unsafe file mode, or a malformed email or token): run `skj setup` again"
CAPTCHA = ("; not retried: repeated failed logins can trigger a CAPTCHA that blocks REST authentication until you sign in "
           "once in a browser. Check the email and the classic API token")
STOPPED = "not checked: doctor stopped after the 401 (never retried)"
NO_TARGET_NOTE = "the target branch has no skj.config.json yet, so there is no site to pin: merge the setup there"
NO_BASE = "site pin not enforced: no base (pass --base or fetch origin/HEAD)"
APPROVERS_NOT_USED = 'not used: approval is "terminal" on the target branch'
NO_BASE_APPROVERS = "not checked: no base, so the target branch's approvers cannot be read (see the site-pin row)"
APPROVERS_PIN_REFUSED = ("not checked: the site pin refused the target branch's config (see the site-pin row), so its approvers are not "
                         "used")
PIN_NO_CONFIG = "not checked: no usable skj.config.json"
APPROVERS_NO_CONFIG = "not checked: the working-tree skj.config.json is not usable (see the config row), so no approvers were read"
APPROVERS_TARGET_MOVED = ("not checked: origin/HEAD does not point at the guarded default branch (target-moved, see the site-pin row), so "
                          "no approvers are believed")



class _Missing(Exception):
    """The tool is not installed."""


class _Unusable(Exception):
    """The tool ran but gave no usable answer (timeout, crash, non-zero exit)."""


def _run(argv):
    """stdout (text) of a successful run: no SKJ_* or secret-looking variable in its environment, no stdin, a timeout."""
    try:
        r = subprocess.run(argv, env={**config.git_env(), "NO_COLOR": "1"}, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=TIMEOUT, check=False)
    except FileNotFoundError:
        raise _Missing from None
    except (OSError, subprocess.SubprocessError):
        raise _Unusable from None
    if r.returncode:
        raise _Unusable
    return r.stdout.decode("utf-8", "replace")


def _vers(m):
    return tuple(int(g or 0) for g in m.groups())


def _dotted(v):
    return ".".join(str(n) for n in v)


def _git_row(repo):
    try:
        m = _GIT.search(_run(["git", "--version"]))
    except _Missing:
        return "fail", "git is not installed or not on PATH: install git 2.24 or newer"
    except _Unusable:
        return "unknown", "`git --version` gave no usable answer"
    if not m:
        return "unknown", "`git --version` gave an answer that could not be read"
    found = _vers(m)[:2]
    if found < GIT_FLOOR:
        return "fail", f"git {_dotted(found)} is older than {_dotted(GIT_FLOOR)}: upgrade git"
    return "ok", ""


def _specify_row(repo):
    try:
        m = _SPECIFY.search(_run(["specify", "--version"]))
    except _Missing:
        return "warn", "the `specify` CLI is not on PATH: install Spec Kit (`uv tool install specify-cli`) to use the lifecycle workflow"
    except _Unusable:
        return "unknown", "`specify --version` gave no usable answer"
    if not m:
        return "unknown", "`specify --version` gave an answer that could not be read"
    if _vers(m) < SPECKIT_FLOOR:
        return "fail", f"specify {_dotted(_vers(m))} is older than {_dotted(SPECKIT_FLOOR)}: upgrade Spec Kit"
    return "ok", ""


def _config_text(msg, what):
    """The validator's own text, minus its file label: fixed wording that names keys by position and never a value."""
    tail = msg.split(": ", 1)[1] if ": " in msg else msg
    if tail.startswith("invalid JSON"):
        tail = "is not valid JSON"
    elif tail.startswith("cannot read"):
        tail = "cannot be read"
    return re.sub(r"[^\x20-\x7e]", "?", f"{what} {tail}".strip())[:300]


def _load_config(repo):
    """(Config or None, (status, fix) for the `config` row)."""
    try:
        return Config.load(str(repo)), ("ok", "")
    except ConfigInvalid as e:
        return None, ("fail", _config_text(str(e), "skj.config.json:") + ": fix it, then run `skj doctor` again")
    except ConfigError:
        return None, ("fail", "no skj.config.json found: run `skj setup`")


def _target_row(repo, base):
    try:
        ref = base or config.default_base(str(repo))
    except ConfigInvalid as e:  # D-023: origin/HEAD moved off a guarded default branch
        if str(e) == config.E_TARGET_MOVED:
            return "fail", str(e)
        return "fail", _config_text(str(e), "") + ": CI cannot trust the target branch's config until this is fixed"
    if not ref:
        return "unknown", "no origin/HEAD and no --base, so the target branch is unknown: pass --base or run `git remote set-head origin -a`"
    try:
        target = Config.load_from_ref(ref, repo=str(repo))
    except ConfigInvalid as e:
        return "fail", _config_text(str(e), "") + ": CI cannot trust the target branch's config until this is fixed"
    if target is None:
        return "warn", "the target branch has no skj.config.json yet, so CI judges a PR by its own: merge the setup"
    return "ok", ""


def _pin_row(repo, base, cfg):
    """((status, fix), Trust or None); the Trust only when the pin lets credentials be attached."""
    if cfg is None:
        return ("unknown", PIN_NO_CONFIG), None
    try:
        trust = config.trusted(str(repo), base)
    except ConfigInvalid as e:
        why = str(e) if str(e) in (config.E_NONROOT_TRUST, config.E_NO_TARGET, config.E_TARGET_MOVED) else (
            "a config file cannot be read (see the config and config-target rows), so the site pin cannot be checked")
        return ("fail", why), None
    status = config.pin_status(cfg, trust)
    if status == "ok":
        return ("ok", ""), trust
    if status == "no-target-config":
        return ("ok", NO_TARGET_NOTE), trust
    if status == "no-base":
        return ("warn", NO_BASE), trust
    return ("fail", config._PIN_REFUSAL.get(status, config._PIN_REFUSAL["invalid"])), None


def _default_client(cfg, trust):
    email, token = secretstore.get(cfg.site)
    return config.build_client(cfg, trust, email, token)


def _jira_rows(cfg, trust, client_factory):
    """(token, identity, project-type, client, me) : one GET /myself, then one GET /project/<key>. `client` is None unless the
    project row is ok or warn (the only state `edit-all` reads in); `me` is None unless /myself answered usably."""
    if trust is None:
        skip = ("unknown", "not checked: no usable config" if cfg is None else "not checked: the site pin refused credentials")
        return skip, skip, skip, None, None
    try:
        c = (client_factory or (lambda: _default_client(cfg, trust)))()
    except secretstore.SecretMissing as e:
        none = ("unknown", "not checked: there is no identity")
        return ("fail", HALF_SET if isinstance(e, secretstore.SecretIncomplete) else NO_TOKEN), none, none, None, None
    except (secretstore.SecretError, ValueError, config.AuthRefused) as e:
        fix = config._PIN_REFUSAL["invalid"] if isinstance(e, config.AuthRefused) else BAD_SECRET
        return ("fail", fix), ("unknown", "not checked: no client"), ("unknown", "not checked: no client"), None, None
    try:
        me = issues.me(c)
    except AuthError as e:
        reason = ", X-Seraph-LoginReason: %s" % e.login_reason if e.login_reason else ""
        return ("fail", "Jira did not accept the credentials (HTTP %s%s)%s" % (e.status, reason, CAPTCHA)), \
            ("unknown", STOPPED), ("unknown", STOPPED), None, None
    except issues.ReadError as e:  # Jira answered 200: the token works, the account does not fit
        return ("ok", ""), ("fail", str(e)), ("unknown", "not checked: the identity is not usable"), None, None
    except JiraError as e:
        why = "Jira answered HTTP %s to GET /myself" % e.status
        return ("fail" if e.status == 403 else "unknown", why), ("unknown", "not checked: " + why), ("unknown", "not checked"), None, None
    except ClientError:
        why = "could not reach Jira: check the network"
        return ("unknown", why), ("unknown", "not checked: " + why), ("unknown", "not checked: " + why), None, None
    ident = ("ok", "accountId %s" % short_id(me["accountId"])) if me["active"] else (
        "fail", "the token's account is not active")
    project = _project_row(c, cfg.project)
    return ("ok", ""), ident, project, (c if project[0] in ("ok", "warn") else None), me


def _approvers_row(trust, me, pin=None):
    """`approvers` from the TARGET branch's Trust: each an accountId (keys.ACCOUNT_RE), none the token user (exact strings, A1-AC3).
    Approval "comment" needs at least one; with "terminal" a list is not used (warn). The token user is shown cut, never in full."""
    if trust is None:
        if pin is not None and pin[1] == config._PIN_REFUSAL["mismatch"]:  # read, then refused (D-004): not "could not be read"
            return "unknown", APPROVERS_PIN_REFUSED
        if pin is not None and pin[1] == config._PIN_REFUSAL["refused-ci-no-base"]:
            return "unknown", NO_BASE_APPROVERS
        if pin is not None and pin[1] == PIN_NO_CONFIG:  # DF3 (D-004 a): the working tree, not the target
            return "unknown", APPROVERS_NO_CONFIG
        if pin is not None and pin[1] == config.E_TARGET_MOVED:  # DF3 (D-004 b): read and refused
            return "unknown", APPROVERS_TARGET_MOVED
        return "unknown", "not checked: the target branch's trust keys could not be read (see the site-pin row)"
    if not trust.base_known:
        return "unknown", NO_BASE_APPROVERS
    approvers = list(trust.approvers)
    for i, a in enumerate(approvers):
        if not (isinstance(a, str) and keys.ACCOUNT_RE.fullmatch(a)):
            return "fail", f"approvers[{i}] on the target branch is not a Jira accountId: fix it there"
    comment = trust.approval == "comment"
    if not approvers:
        return ("fail", 'approval is "comment" on the target branch but approvers is empty, so nobody can approve: add the '
                        "approvers' accountIds there") if comment else ("ok", APPROVERS_NOT_USED)
    if me is None:
        return "unknown", "not checked: GET /myself gave no usable answer, so the token's own account cannot be ruled out"
    for i, a in enumerate(approvers):
        if a == me["accountId"]:
            return "fail", (f"approvers[{i}] is the token's own account (accountId {short_id(a)}): the agent cannot approve its own "
                            "work; remove it on the target branch")
    if not comment:
        return "warn", f'{len(set(approvers))} approver(s) set on the target branch but approval is "terminal", so they are not used'
    return "ok", f"{len(set(approvers))} approver(s), none is the token's own account"


def _edit_all_row(c, project, trust):
    """`edit-all`: one GET /mypermissions for EDIT_DELETE. All false: ok. Any true: warn, never fail; with approval "comment" on the
    target the fix is approvals.TAMPER_SENTENCE. An omitted name or an odd answer cannot tell: unknown, never ok."""
    if c is None or trust is None:
        return "unknown", "not checked: the earlier Jira rows did not succeed"
    try:
        have = issues.permissions(c, project, names=EDIT_DELETE, omitted_false=False)
    except AuthError:
        return "unknown", "Jira did not accept the credentials for GET /mypermissions (HTTP 401); not retried"
    except JiraError as e:
        return "unknown", "Jira answered HTTP %s to GET /mypermissions" % e.status
    except issues.ReadError:
        return "unknown", "Jira's GET /mypermissions answer could not be read"
    except ClientError:
        return "unknown", "could not reach Jira: check the network"
    can = [n for n in EDIT_DELETE if have[n]]
    if not can:
        return "ok", ""
    if not trust.base_known:  # the defaults, not the target: never claim the mode
        return "warn", ("the target branch was not read, so whether comment approval is on is unknown; the token has %s on this "
                        "project" % ", ".join(can))
    if trust.approval == "comment":
        return "warn", approvals.TAMPER_SENTENCE
    return "warn", ("the token has %s on this project; it does not matter while approval is \"terminal\", but comment approval "
                    "here could only show an edit, never a deletion" % ", ".join(can))


def _project_row(c, project):
    try:
        d = c.request("GET", "/project/" + project).data
    except NotFound:
        return "fail", f"project {project} not found, or the token's user may not see it: check 'project' in skj.config.json"
    except JiraError as e:
        return "unknown", "Jira answered HTTP %s to GET /project" % e.status
    except ClientError:
        return "unknown", "could not reach Jira: check the network"
    style = d.get("style") if isinstance(d, dict) else None
    if style == "next-gen":
        return "ok", ""
    if style == "classic":
        return "warn", "company-managed (classic) project: not tested in 0.1, which is tested on team-managed projects only"
    return "unknown", "Jira did not say whether the project is team-managed or company-managed"


def _crash(e):
    """A fixed text plus the exception CLASS name (code-defined; never the message, which may carry user text)."""
    return "unknown", f"this check failed unexpectedly ({type(e).__name__}) and gave no result"


def _safe(fn, wrap, *args):
    """fn(*args), or `wrap(crash row)` when it raises: a diagnostic must report, not crash."""
    try:
        return fn(*args)
    except Exception as e:
        return wrap(_crash(e))


def run(repo=".", base=None, client_factory=None):
    """`{"rows": [{"id", "status", "fix"}]}` in ROW_IDS order. `client_factory`: a zero-argument callable returning a Client
    (tests: the fake); default: the secret store, then `config.build_client`. It is called only once the site pin allows it."""
    out = {}
    row = lambda r: r  # noqa: E731
    out["git"] = _safe(_git_row, row, repo)
    out["specify"] = _safe(_specify_row, row, repo)
    cfg, out["config"] = _safe(_load_config, lambda r: (None, r), repo)
    out["config-target"] = _safe(_target_row, row, repo, base)
    out["site-pin"], trust = _safe(_pin_row, lambda r: (r, None), repo, base, cfg)
    out["token"], out["identity"], out["project-type"], c, me = _safe(_jira_rows, lambda r: (r, r, r, None, None), cfg, trust,
                                                                       client_factory)
    out["approvers"] = _safe(_approvers_row, row, trust, me, out["site-pin"])
    out["edit-all"] = _safe(_edit_all_row, row, c, cfg.project if cfg else None, trust)
    return {"rows": [{"id": i, "status": out[i][0], "fix": out[i][1]} for i in ROW_IDS]}


def failed(result):
    """Any `fail`, or a `token` row that is not proven (`unknown`): without it nothing shows the tool can work. `edit-all` is never
    a fail (it warns), so it never makes doctor exit non-zero."""
    return any(r["status"] == "fail" or (r["id"] == "token" and r["status"] == "unknown") for r in result["rows"])


def render(result):
    """The text form: one line per row, `status  id  fix`."""
    return "\n".join(f"{r['status']:<8}{r['id']:<15}{r['fix']}".rstrip() for r in result["rows"])
