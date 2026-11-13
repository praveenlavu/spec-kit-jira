"""Jira identity (email + API token): where it is read from, and nothing else.

Order: SKJ_EMAIL + SKJ_TOKEN (CI) win; else macOS Keychain (read only, K6); else ~/.skj/<site>.json
(mode 0600). The token is only ever read from a subprocess's STDOUT and never put on an argv;
there is no macOS write function (the human runs `security add-generic-password ... -w`, which
prompts). Every error text is fixed: it never contains the token, the email or a path.
"""
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

from .keys import SITE_RE

ENV_EMAIL = "SKJ_EMAIL"
ENV_TOKEN = "SKJ_TOKEN"

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9.-]{1,190}\Z")  # no whitespace, colon or controls
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,1024}\Z")  # printable ASCII, no whitespace
# Attribute lines of `security find-generic-password` (no -w). Checked once against real macOS output (live
# check LV1, 2026-11-12, one macOS version); the parse is strict on purpose: exact 4-space indent, a printable-ASCII quoted
# blob, and the key may occur on exactly one line of the whole output (a value that spans lines could
# otherwise smuggle in a second "acct" or "svce" line). Anything else fails closed.
_ACCT_RE = re.compile(r'    "acct"<blob>=(?:"([\x20\x21\x23-\x7e]*)"|<NULL>)')
_SVCE_RE = re.compile(r'    "svce"<blob>=(?:"([\x20\x21\x23-\x7e]*)"|<NULL>)')
_MAX_FILE = 65536
_SECURITY_TIMEOUT = 60


class SecretError(Exception):
    """The identity cannot be used (unsafe file, malformed input)."""


class SecretMissing(SecretError):
    """No identity is stored for this site."""


class SecretIncomplete(SecretMissing):
    """Only one of SKJ_EMAIL / SKJ_TOKEN is set."""


def _site(site):
    if not (isinstance(site, str) and SITE_RE.fullmatch(site)):
        raise ValueError("invalid site")  # before it can reach a service name or a path
    return site


def _email(email):
    if not (isinstance(email, str) and _EMAIL_RE.fullmatch(email)):
        raise SecretError("invalid email")
    return email


def _token(token):
    if not (isinstance(token, str) and _TOKEN_RE.fullmatch(token)):
        raise SecretError("invalid token")
    return token


def _dir():
    return Path.home() / ".skj"  # read at call time so tests can redirect HOME


_SHAPE_MSG = "attribute format not recognised (parser checked on one macOS version only)"
_BAD = object()  # the attribute line is missing, repeated or malformed


def _security(argv):
    """(fail, stdout text); fail is None or (exception class, fixed message) for the CALLER to raise.
    Bytes mode + our own strict decode: text=True would raise a UnicodeDecodeError whose repr carries the
    raw bytes (the token or the email). stderr is never read. Nothing is raised here, so no frame on the
    traceback holds the argv (it carries the email) or the output, and nothing has a __context__."""
    fail, r, text = None, None, None
    try:
        r = subprocess.run(argv, capture_output=True, timeout=_SECURITY_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        fail = (SecretMissing, "keychain unavailable")
    if fail is None and r.returncode != 0:
        fail = (SecretMissing, "no keychain item for this site")
    if fail is None:
        try:
            text = r.stdout.decode("utf-8")
        except ValueError:
            fail = (SecretError, "keychain output is not valid UTF-8")
    return fail, text


def _one_attr(out, key, rx):
    """The value of the single `key` attribute line (None for <NULL>), or _BAD unless it occurs exactly
    once, well-formed."""
    if out.count('"%s"<blob>=' % key) != 1:
        return _BAD
    ms = [rx.fullmatch(line) for line in out.split("\n")]
    ms = [m for m in ms if m]
    return ms[0].group(1) if len(ms) == 1 else _BAD


def _from_keychain(site):
    # No local holds a secret at the point an error is raised, so a traceback rendered with locals is clean.
    service = "skj:" + site
    email = token = acct = svce = out = None
    fail, out = _security(["security", "find-generic-password", "-s", service])
    if fail is None:
        acct, svce = _one_attr(out, "acct", _ACCT_RE), _one_attr(out, "svce", _SVCE_RE)
        if acct is _BAD or svce is _BAD:
            fail = (SecretMissing, _SHAPE_MSG)
        elif svce != service:
            fail = (SecretMissing, "keychain item belongs to another service")
        elif not acct:
            fail = (SecretMissing, "keychain item has no account")
        elif not _EMAIL_RE.fullmatch(acct):
            fail = (SecretError, "invalid email")
    if fail is None:
        email = acct
        fail, token = _security(["security", "find-generic-password", "-s", service, "-a", email, "-w"])
    if fail is None:
        token = token.rstrip("\n")
        if not _TOKEN_RE.fullmatch(token):
            fail = (SecretError, "invalid token")
    if fail:
        email = token = acct = svce = out = None
        raise fail[0](fail[1])
    return email, token


def _check_dir(d):
    """Refuse unless ~/.skj is a real directory (not a symlink), ours, with no group/world bits."""
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        raise SecretMissing("no stored identity for this site") from None
    except OSError:
        raise SecretError("identity directory cannot be checked") from None
    if (not stat.S_ISDIR(st.st_mode) or not hasattr(os, "getuid") or st.st_uid != os.getuid()
            or st.st_mode & 0o077):
        raise SecretError("identity directory must be a real directory owned by you with mode 0700")


def _from_file(site):
    d = _dir()
    _check_dir(d)
    path = d / (site + ".json")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise SecretMissing("no stored identity for this site") from None
    except OSError:
        raise SecretError("identity file cannot be opened") from None  # includes a symlink
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            raise SecretError("identity file must be a regular file with mode 0600")
        data = os.read(fd, _MAX_FILE)
    finally:
        os.close(fd)
    try:
        obj = json.loads(data.decode("utf-8"))
        email, token = obj["email"], obj["token"]
    except (ValueError, KeyError, TypeError):
        raise SecretError("identity file is malformed") from None
    return _email(email), _token(token)


def get(site):
    """Return (email, token) for `site` or raise SecretMissing / SecretError / ValueError."""
    _site(site)
    email, token = os.environ.get(ENV_EMAIL), os.environ.get(ENV_TOKEN)
    if email or token:
        if not (email and token):
            raise SecretIncomplete("%s and %s must both be set" % (ENV_EMAIL, ENV_TOKEN))
        return _email(email), _token(token)
    if sys.platform == "darwin":
        return _from_keychain(site)
    return _from_file(site)


def _sweep_stale(d, site):
    """Remove leftovers of an interrupted store_file: only this site's temp names, only regular files of ours."""
    rx = re.compile(r"\.%s\.[0-9a-f]{16}\.tmp\Z" % re.escape(site))
    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names:
        if not rx.fullmatch(name):
            continue
        try:
            st = os.lstat(d / name)
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
                os.unlink(d / name)
        except OSError:
            pass


def store_file(site, email, token):
    """Non-macOS: write ~/.skj/<site>.json (0600, in a 0700 directory) atomically. Returns the path."""
    _site(site)
    _email(email)
    _token(token)
    if sys.platform == "darwin":
        raise SecretError("on macOS the identity lives in the Keychain; this tool never writes it")
    d = _dir()
    try:
        os.mkdir(d, 0o700)
        os.chmod(d, 0o700)  # the umask may have narrowed it, never widened
    except FileExistsError:
        pass
    except OSError:
        raise SecretError("identity directory cannot be created") from None
    _check_dir(d)
    path = d / (site + ".json")
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):  # a symlink or a directory is never replaced
            raise SecretError("identity file must be a regular file")
    except FileNotFoundError:
        pass
    except OSError:
        raise SecretError("identity file cannot be written") from None
    _sweep_stale(d, site)
    tmp = d / (".%s.%s.tmp" % (site, os.urandom(8).hex()))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        raise SecretError("identity file cannot be written") from None
    failed, opened = False, False
    try:
        f = os.fdopen(fd, "wb")
        opened = True
        with f:
            f.write(json.dumps({"email": email, "token": token}).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # a new inode: a hard link to the old file never sees the new token
    except BaseException as ex:  # incl. KeyboardInterrupt: never leave a temp file holding the token
        if not opened:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if not isinstance(ex, OSError):
            raise
        failed = True
    if failed:
        email = token = None  # not in the frame of a traceback rendered with locals
        raise SecretError("identity file cannot be written")  # outside the except: no __context__
    return path
