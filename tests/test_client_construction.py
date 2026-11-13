"""K7: the site pin holds only if every authenticated client comes from `config.build_client`. No other code under
src/skj may construct `Client(...)` (the test fakes under tests/ may)."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "skj"


def _constructions(tree):
    """(enclosing function name or None, line) for every call of a name or attribute spelled `Client`."""
    out = []

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            f = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if isinstance(child, ast.Call):
                callee = child.func
                name = callee.id if isinstance(callee, ast.Name) else callee.attr if isinstance(callee, ast.Attribute) else None
                if name == "Client":
                    out.append((f, child.lineno))
            walk(child, f)
    walk(tree, None)
    return out


def test_client_is_constructed_only_in_build_client():
    found = {}
    for f in sorted(SRC.rglob("*.py")):
        for func, line in _constructions(ast.parse(f.read_text())):
            found.setdefault(f.name, []).append((func, line))
    assert {name: [fn for fn, _ in calls] for name, calls in found.items()} == {"config.py": ["build_client"]}


def test_no_alias_of_the_client_class_is_imported():
    for f in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("client"):
                assert all(a.name != "Client" for a in node.names), f.name  # `from .client import Client as X` dodges the scan


def test_the_scan_sees_a_construction():
    assert _constructions(ast.parse("def f():\n    return client.Client('s', 'e', 't')\nClient()\n")) == [("f", 2), (None, 3)]
