"""Identifier patterns and JQL builders: the only place either lives.

Every pattern uses explicit ASCII classes and `\\Z` (not `$`, which matches before a trailing
newline; not `\\d`, which matches Unicode digits). JQL is built only from values that pass a
full-match pattern, each double-quoted; no free text (summary, title) ever enters JQL.
"""
import re

PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,9}\Z")  # 10-character cap UNVERIFIED (U13)
KEY_PATTERN = r"[A-Z][A-Z0-9_]{1,9}-[1-9][0-9]{0,9}"  # an issue key; every key regex is built from this one
KEY_RE = re.compile(r"^%s\Z" % KEY_PATTERN)
# A key inside free text (a branch name, a commit subject): same shape as KEY_RE, but not part of a longer token.
KEY_IN_TEXT_RE = re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % KEY_PATTERN)
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}\Z")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}\Z")
SITE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.atlassian\.net\Z")
_NUMBER_RE = re.compile(r"^[1-9][0-9]{0,9}\Z")
_LINE_NUMBER_RE = re.compile(r"[1-9][0-9]*\Z")


def line_number(digits):
    """True when `digits` (what follows the D of a decision line or the Q of a question line) is a line number: ASCII digits, no
    leading zero (H1a: Arabic-Indic or full-width digits, `01` and `0` are not; the caller then treats the line as malformed)."""
    return bool(_LINE_NUMBER_RE.match(digits))

class UsageError(ValueError):
    """A malformed command-line value (a bad --site, --kind, --why ...), refused before anything is read or written: exit 2, like an argparse
    usage error. A ValueError, so every caller that refuses a ValueError still refuses it."""


MAX_JQL_KEYS = 100  # one JQL clause stays small; callers batch beyond this


def _ok(rx, s):
    return isinstance(s, str) and rx.fullmatch(s) is not None


def _need(rx, s, what):
    if not _ok(rx, s):
        raise ValueError("invalid %s" % what)  # never echo the value into JQL-adjacent text
    return s


def valid_key(s):
    return _ok(KEY_RE, s)


def parse_ref(s, project):
    """`SPK-123` or `123` -> the key; ValueError otherwise (nothing is normalised)."""
    _need(PROJECT_RE, project, "project")
    if _ok(_NUMBER_RE, s):
        return "%s-%s" % (project, s)
    return _need(KEY_RE, s, "issue reference")


def jql_children(key):
    return 'parent = "%s" ORDER BY key' % _need(KEY_RE, key, "issue key")


def jql_by_label(project, label):
    return 'project = "%s" AND labels = "%s" ORDER BY key' % (
        _need(PROJECT_RE, project, "project"),
        _need(LABEL_RE, label, "label"),
    )


def jql_keys(keys):
    if not isinstance(keys, (list, tuple)):
        raise ValueError("keys must be a list or tuple")
    if not keys:
        raise ValueError("no keys given")
    if len(keys) > MAX_JQL_KEYS:
        raise ValueError("more than %d keys" % MAX_JQL_KEYS)
    return "key in (%s) ORDER BY key" % ", ".join('"%s"' % _need(KEY_RE, k, "issue key") for k in keys)


def dod_name(key):
    return "DoD_%s.md" % _need(KEY_RE, key, "issue key")
