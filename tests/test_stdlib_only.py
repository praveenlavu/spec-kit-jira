import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "skj"

# Frozen for 3.9, which has no sys.stdlib_module_names (3.10+).
_STDLIB_39 = frozenset(
    "__future__ abc argparse array ast asyncio base64 binascii bisect builtins bz2 calendar "
    "codecs collections concurrent configparser contextlib copy csv ctypes dataclasses datetime "
    "decimal difflib email encodings enum errno fcntl fnmatch functools getpass glob gzip hashlib "
    "heapq hmac html http importlib inspect io ipaddress itertools json keyword locale logging "
    "math mimetypes numbers operator os pathlib platform posixpath pprint queue random re "
    "secrets select selectors shlex shutil signal socket ssl stat string struct subprocess sys "
    "tempfile termios textwrap threading time traceback types typing unicodedata urllib uuid warnings "
    "weakref xml zipfile zlib".split()
)
STDLIB = getattr(sys, "stdlib_module_names", _STDLIB_39)


def test_all_imports_are_stdlib_or_local():
    files = sorted(SRC.rglob("*.py"))
    assert files, "no source files found"
    bad = []
    for f in files:
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            else:
                continue
            bad += [f"{f.name}:{n}" for n in names if n.split(".")[0] not in STDLIB | {"skj"}]
    assert not bad, bad
