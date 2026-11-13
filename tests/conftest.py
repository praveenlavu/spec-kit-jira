import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import ambient  # noqa: E402,F401  (records the run's CI marker before the fixture below removes it)


@pytest.fixture(autouse=True)
def _no_ambient_ci(monkeypatch):
    """No test sees the runner's CI markers: a test that needs CI semantics sets CI / GITHUB_ACTIONS itself (the code reads them at call
    time, so an ambient value would change what a test checks)."""
    for k in ("CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test may touch the network: connect, send and DNS lookups all raise."""

    def _blocked(*args, **kwargs):
        raise RuntimeError("network access is blocked in tests")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _blocked)
    for name in ("getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, _blocked)
