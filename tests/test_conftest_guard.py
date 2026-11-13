import socket

import pytest

BLOCKED = "network access is blocked"


def test_socket_connect_is_blocked():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(RuntimeError, match=BLOCKED):
            s.connect(("127.0.0.1", 9))


def test_socket_connect_ex_is_blocked():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(RuntimeError, match=BLOCKED):
            s.connect_ex(("127.0.0.1", 9))


def test_socket_sendto_is_blocked():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(RuntimeError, match=BLOCKED):
            s.sendto(b"x", ("127.0.0.1", 9))


def test_getaddrinfo_is_blocked():
    with pytest.raises(RuntimeError, match=BLOCKED):
        socket.getaddrinfo("localhost", 80)


def test_gethostbyname_is_blocked():
    with pytest.raises(RuntimeError, match=BLOCKED):
        socket.gethostbyname("localhost")
