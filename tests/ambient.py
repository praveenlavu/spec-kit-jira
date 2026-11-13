"""The CI marker of the environment pytest was started in, read once at import, before any test runs.

tests/conftest.py removes CI and GITHUB_ACTIONS from every test's environment (a test that wants CI semantics sets them itself), so
tests behave the same on a laptop and on a runner. Only one question is about the real run: is a missing Spec Kit CLI a failure here
(CI) or a skip (a laptop)? It reads `CI` below.
"""
import os

CI = os.environ.get("CI", "")
