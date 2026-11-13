"""PN: tests/PORT_NOTES.md keeps the port honest. It has its five sections and no placeholder text, every skip in tests/*.py is
listed in section 4 (so a new skip cannot land silently), every test file the PORTED list names exists, and no line names the
fork's folder, a home path, an address or a denylisted term."""
import ast
import importlib.util
import os
import re
from collections import Counter
from pathlib import Path

from test_no_ado_leftovers import PORTED

ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "tests" / "PORT_NOTES.md"
SECTIONS = ("## 1. Ported with changes", "## 2. Not ported (dropped)", "## 3. Deferred to 0.2", "## 4. Skipped in this repo",
            "## 5. Known 0.2 backlog")
PLACEHOLDER = re.compile(r"\bTODO\b|\bTBD\b|\bFIXME\b|\bXXX\b|lorem ipsum|<fill|\.\.\.\s*$", re.IGNORECASE)
# Every attribute named like a skip counts, whatever its root or where it sits (call target, decorator, `marks=`, or inside an
# expression such as `(pytest.fail if CI else pytest.skip)(...)`).
SKIP_NAMES = {"skip", "skipif", "importorskip", "skipIf", "skipUnless", "skipTest"}
# shortcut: not seen are `from pytest import skip`/`mark` aliases, xfail, files in tests/ subdirectories and conftest add_marker;
# none adds a skip in tests/ today, extend the walk if one appears.
FORBIDDEN = re.compile(r"spec-kit-ado-new|spec-kit-ghp-new|/Users/|HDSpace|@[A-Za-z0-9]|\.\./", re.IGNORECASE)


def _text():
    return NOTES.read_text(encoding="utf-8")


def _keyword(call):
    """The stable keyword of a skip: the longest string literal in its arguments (the reason), stripped."""
    strs = [n.value for n in ast.walk(call) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return max(strs, key=len).strip() if strs else ""


def skip_sites(name, source):
    """Counter of (file name, keyword) for every skip site in one test module's source; the keyword comes from the nearest
    enclosing call (empty for a bare decorator)."""
    tree = ast.parse(source)
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in SKIP_NAMES:
            up = parent.get(node)
            while up is not None and not isinstance(up, ast.Call):
                up = parent.get(up)
            found[(name, _keyword(up) if up is not None else "")] += 1
    return found


def repo_skips():
    found = Counter()
    for path in sorted((ROOT / "tests").glob("*.py")):
        found += skip_sites(path.name, path.read_text(encoding="utf-8"))
    return found


def section(text, number):
    part = text.split(SECTIONS[number - 1], 1)[1]
    return re.split(r"^## ", part, maxsplit=1, flags=re.M)[0]


def listed_skips(text):
    """Counter of (file name, keyword) from section 4's table rows: | `tests/<file>` | `<keyword>` | <sites> | ..."""
    found = Counter()
    for line in section(text, 4).splitlines():
        m = re.match(r"\|\s*`tests/([^`]+)`\s*\|\s*`([^`]*)`\s*\|\s*(\d+)\s*\|", line)
        if m:
            found[(m.group(1), m.group(2))] += int(m.group(3))
    return found


def test_pn_ac1_five_sections_in_order_each_with_content_and_no_placeholder():
    text = _text()
    at = [text.find(h) for h in SECTIONS]
    assert -1 not in at, [h for h, i in zip(SECTIONS, at) if i == -1]
    assert at == sorted(at)
    for n in range(1, 6):
        body = section(text, n)
        assert body.strip().count("\n") >= 2, SECTIONS[n - 1]
    hits = [(no, line) for no, line in enumerate(text.splitlines(), 1) if PLACEHOLDER.search(line)]
    assert hits == []


def test_pn_ac2_every_skip_in_tests_is_listed_in_section_4_with_its_count():
    """A new skip, a changed reason or a removed skip makes the two sides differ."""
    assert repo_skips(), "the AST walk found no skip at all: the walker is broken"
    assert repo_skips() == listed_skips(_text())


def test_pn_ac2_the_guard_sees_a_new_unlisted_skip():
    """The guard bites on each skip shape added to a file (in memory, nothing written)."""
    listed = listed_skips(_text())
    for src in ('import pytest\npytest.skip("brand new reason")\n',
                'import pytest\n@pytest.mark.skipif(True, reason="brand new reason")\ndef test_x(): pass\n',
                'import pytest\nnp = pytest.importorskip("numpy")\n',
                'import pytest\n@pytest.mark.skip\ndef test_x(): pass\n',
                'import unittest\nclass T(unittest.TestCase):\n    def test_x(self): self.skipTest("brand new reason")\n',
                'import pytest\n(pytest.fail if 0 else pytest.skip)("brand new reason")\n',
                'import pytest as pt\npytestmark = pt.mark.skip\n'):
        assert skip_sites("test_new.py", src), src
        assert repo_skips() + skip_sites("test_new.py", src) != listed, src
    # one more site with an already listed keyword in an already listed file also changes the count
    (name, kw), _ = next(iter(listed.items()))
    assert repo_skips() + Counter({(name, kw): 1}) != listed


def test_pn_every_ported_test_file_exists():
    missing = [rel for rel in PORTED if rel.startswith("tests/") and rel.endswith(".py") and not (ROOT / rel).is_file()]
    assert missing == []


def test_pn_no_fork_path_home_path_address_or_denylisted_term():
    spec = importlib.util.spec_from_file_location("scrub_check", ROOT / "scripts" / "scrub_check.py")
    scrub_check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scrub_check)
    denylist = scrub_check.load_denylist(os.environ, ROOT / scrub_check.DENYLIST_FILE)
    hits = []
    for no, line in enumerate(_text().splitlines(), 1):
        if FORBIDDEN.search(line) or any(term in line.lower() for term in denylist):
            hits.append(no)
    assert hits == []
