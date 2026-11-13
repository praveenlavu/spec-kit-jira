import re
from pathlib import Path

from skj.__main__ import main

ROOT = Path(__file__).resolve().parent.parent


def test_version_flag(capsys):
    try:
        rc = main(["--version"])
    except SystemExit as e:  # argparse exits on --version
        rc = e.code
    assert rc in (0, None)
    assert capsys.readouterr().out.strip() == "skj 0.1.0"


def test_no_runtime_dependency():
    text = (ROOT / "pyproject.toml").read_text()
    # 3.9 has no tomllib: no top-level `dependencies = [...]` under [project]
    project = re.search(r"^\[project\]\n(.*?)(?=^\[)", text, re.S | re.M).group(1)
    assert not re.search(r"^dependencies\s*=", project, re.M)
    assert "dynamic" not in project or "dependencies" not in project
    dev = re.search(r"^\[project\.optional-dependencies\]\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
    assert dev and re.search(r"^dev\s*=", dev.group(1), re.M)
    assert re.search(r'^requires-python\s*=\s*">=3\.9"', project, re.M)
