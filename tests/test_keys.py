import inspect

import pytest

from skj import keys
from skj.keys import (
    ACCOUNT_RE,
    KEY_RE,
    LABEL_RE,
    MAX_JQL_KEYS,
    PROJECT_RE,
    SITE_RE,
    dod_name,
    jql_by_label,
    jql_children,
    jql_keys,
    parse_ref,
    valid_key,
)


def test_valid_keys():
    for k in ["SPK-1", "SPK-123", "AB-9999999999", "A1_B-7", "ABCDEFGHIJ-10", "S_-5"]:
        assert valid_key(k), k
        assert KEY_RE.fullmatch(k)


BAD_SHAPES = [
    "spk-1",  # lowercase
    "Spk-1",
    " SPK-1",  # padded
    "SPK-1 ",
    "SPK -1",
    "SPK-0",  # zero
    "SPK-01",  # leading zero
    "SPK-",
    "SPK",
    "-1",
    "S-1",  # project too short
    "ABCDEFGHIJK-1",  # project too long (11)
    "1SPK-1",
    "SPK-12345678901",  # number too long (11 digits)
    "SPK-١٢",  # Arabic-Indic digits
    "SPK-１",  # fullwidth digit
    "ＳPK-1",  # fullwidth S
    "",
]


@pytest.mark.parametrize("bad", BAD_SHAPES)
def test_rejects_lowercase_padded_zero_leading_unicode_digits(bad):
    assert valid_key(bad) is False
    assert KEY_RE.fullmatch(bad) is None
    with pytest.raises(ValueError):
        jql_children(bad)
    with pytest.raises(ValueError):
        dod_name(bad)


@pytest.mark.parametrize("bad", ["SPK-1;touch x", 'SPK-1" OR 1=1', "SPK-1\n"])
def test_rejects_injection(bad):
    assert valid_key(bad) is False
    assert KEY_RE.match(bad) is None  # \Z, not $: a trailing newline cannot slip through
    for fn in (jql_children, dod_name):
        with pytest.raises(ValueError):
            fn(bad)
    with pytest.raises(ValueError):
        jql_keys(["SPK-1", bad])
    with pytest.raises(ValueError):
        parse_ref(bad, "SPK")


@pytest.mark.parametrize("bad", [None, 1, b"SPK-1", ["SPK-1"], 1.5])
def test_non_str_refused(bad):
    assert valid_key(bad) is False
    with pytest.raises(ValueError):
        jql_children(bad)
    with pytest.raises(ValueError):
        dod_name(bad)
    with pytest.raises(ValueError):
        parse_ref(bad, "SPK")


def test_jql_children_quotes():
    assert jql_children("SPK-6") == 'parent = "SPK-6" ORDER BY key'


def test_jql_by_label_quotes():
    assert jql_by_label("SPK", "skj-managed") == 'project = "SPK" AND labels = "skj-managed" ORDER BY key'


@pytest.mark.parametrize(
    "project,label",
    [
        ("spk", "ok"),
        ("SPK", "Upper"),
        ("SPK", 'a" OR 1=1'),
        ("SPK", "ok\n"),
        ("SPK\n", "ok"),
        ("SPK", ""),
        ("SPK", "-lead"),
        ("SPK", "a b"),
        ("SPK", "x" * 101),
        ('SPK" OR "1"="1', "ok"),
        (None, "ok"),
        ("SPK", None),
    ],
)
def test_jql_by_label_rejects(project, label):
    with pytest.raises(ValueError):
        jql_by_label(project, label)


def test_jql_keys_quotes_each():
    assert jql_keys(["SPK-1", "SPK-2"]) == 'key in ("SPK-1", "SPK-2") ORDER BY key'
    assert jql_keys(("SPK-1",)) == 'key in ("SPK-1") ORDER BY key'


def test_jql_keys_empty_refused():
    with pytest.raises(ValueError):
        jql_keys([])


def test_jql_keys_cap():
    assert MAX_JQL_KEYS == 100
    ok = [f"SPK-{i}" for i in range(1, MAX_JQL_KEYS + 1)]
    assert jql_keys(ok).count('"') == 2 * MAX_JQL_KEYS
    with pytest.raises(ValueError):
        jql_keys(ok + ["SPK-9999"])


def test_jql_keys_rejects_bare_string_and_non_iterable():
    for bad in ["SPK-1", None, 5]:
        with pytest.raises(ValueError):
            jql_keys(bad)


def test_no_free_text_parameter():
    # S2-AC3: only key / project / label / keys parameters exist; no summary or title.
    assert list(inspect.signature(jql_children).parameters) == ["key"]
    assert list(inspect.signature(jql_by_label).parameters) == ["project", "label"]
    assert list(inspect.signature(jql_keys).parameters) == ["keys"]


def test_parse_ref_number_uses_project():
    assert parse_ref("123", "SPK") == "SPK-123"
    assert parse_ref("SPK-123", "SPK") == "SPK-123"
    assert parse_ref("OTHER-4", "SPK") == "OTHER-4"  # a full key is accepted as typed


@pytest.mark.parametrize("bad", ["0", "-5", "1.5", "+3", " 12", "12 ", "12\n", "012", "١٢", "", "spk-1", "abc"])
def test_parse_ref_rejects(bad):
    with pytest.raises(ValueError):
        parse_ref(bad, "SPK")


@pytest.mark.parametrize("project", ["spk", "S", "SPK\n", "", "SPK-1", None, "ABCDEFGHIJK"])
def test_parse_ref_bad_project(project):
    with pytest.raises(ValueError):
        parse_ref("12", project)


def test_dod_name_is_path_safe():
    assert dod_name("SPK-12") == "DoD_SPK-12.md"
    for bad in ["../x", "SPK-1/../../etc", "a/b-1", "SPK-1\\x", "..", "SPK-1.md"]:
        with pytest.raises(ValueError):
            dod_name(bad)


def test_patterns_are_module_constants():
    assert PROJECT_RE.fullmatch("SPK") and not PROJECT_RE.fullmatch("spk")
    assert PROJECT_RE.match("SPK\n") is None
    assert ACCOUNT_RE.fullmatch("5b10ac8d82e05b22cc7d4ef5")
    assert ACCOUNT_RE.fullmatch("712020:abc-DEF_1")
    assert ACCOUNT_RE.match("abc\n") is None
    assert not ACCOUNT_RE.fullmatch("")
    assert not ACCOUNT_RE.fullmatch("a" * 129)
    assert ACCOUNT_RE.fullmatch("a" * 128)
    assert LABEL_RE.fullmatch("skj_v1.0-x") and not LABEL_RE.fullmatch("Skj")
    assert SITE_RE.fullmatch("example-site.atlassian.net")
    for bad in ["example.com", "evil.com/.atlassian.net", "-x.atlassian.net", "x.atlassian.net\n",
                "x.atlassian.net.evil.com", "a" * 64 + ".atlassian.net", "EXAMPLE.atlassian.net", "a.b.atlassian.net"]:
        assert SITE_RE.match(bad) is None, bad
    assert keys.KEY_RE.pattern.startswith("^")


@pytest.mark.parametrize("text,found", [
    ("feature/SPK-12-x", "SPK-12"), ("SPK-12", "SPK-12"), ("a/b/SPK-1", "SPK-1"), ("fix-ABC-3-y", "ABC-3"),
    ("XSPK-12", "XSPK-12"),  # a longer token is a different (here foreign) key, never SPK-12
    ("feature/spk-12-x", None), ("SPK-123abc", None), ("SPK-12_x", None), ("aSPK-12", None), ("1SPK-12", None),
    ("_SPK-12", None), ("SPK-0", None), ("SPK-01", None), ("SPK-12345678901", None), ("ABCDEFGHIJKL-1", None),
    ("S-1", None), ("", None),
])
def test_key_in_text_boundaries(text, found):
    m = keys.KEY_IN_TEXT_RE.search(text)
    assert (m.group(0) if m else None) == found
