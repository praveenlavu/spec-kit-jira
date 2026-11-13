"""S7: retry rules. Synthetic 429/503 fixtures (a real 429 was never provoked, U4); no sleep, no sockets."""
import socket

import pytest

from skj import client as cl

from test_client import EMAIL, SITE, TOKEN, Fake, full_text, no_secret

DATE = "Wed, 21 Oct 2026 07:28:00 GMT"


class Clock:
    """Fake monotonic clock that the fake sleep advances."""

    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        assert 0 <= s <= cl.MAX_RETRY_AFTER, s
        self.sleeps.append(s)
        self.t += s


def mk(*answers, rand=lambda: 0.5):
    fake, clock = Fake(*answers), Clock()
    c = cl.Client(SITE, EMAIL, TOKEN, opener=fake, sleep=clock.sleep, clock=clock, rand=rand)
    return c, fake, clock


def r429(retry_after="2", **extra):
    h = {"Retry-After": retry_after} if retry_after is not None else {}
    h.update(extra)
    return (429, h, b"")


OK = (200, {}, b'{"ok": true}')


# --- S7-AC1 ---------------------------------------------------------------------------------------


def test_get_retries_with_retry_after():
    c, fake, clock = mk(r429("2"), (503, {"Retry-After": "1"}, b""), OK)
    assert c.request("GET", "/issue/SPK-1").data == {"ok": True}  # GET is retry-safe by default
    assert len(fake.calls) == 3
    # rand 0.5 -> jitter 1.0: backoff 5 s then 10 s, each above the Retry-After floor
    assert clock.sleeps == [5.0, 10.0]


def test_retry_after_is_the_floor():
    c, _, clock = mk(r429("45"), OK)
    c.request("GET", "/myself")
    assert clock.sleeps == [45]


def test_four_retries_then_error():
    c, fake, clock = mk(r429("1"))  # Fake repeats its last answer forever
    with pytest.raises(cl.JiraError) as ei:
        c.request("GET", "/myself", retry_safe=True)
    assert ei.value.status == 429
    assert len(fake.calls) == 1 + cl.MAX_RETRIES == 5
    assert clock.sleeps == [5.0, 10.0, 20.0, 30.0]  # base 5, doubling, cap 30


@pytest.mark.parametrize("r, jitter", [(0.0, 0.7), (0.999999, 1.3)])
def test_backoff_bounds_with_injected_rand(r, jitter):
    c, _, clock = mk(r429("0"), rand=lambda: r)
    with pytest.raises(cl.JiraError):
        c.request("GET", "/myself")
    expected = [min(30, 5 * 2 ** n) * jitter for n in range(4)]
    assert clock.sleeps == pytest.approx(expected, rel=1e-5)
    assert all(3.5 - 1e-9 <= s <= 39 + 1e-9 for s in clock.sleeps)


def test_search_jql_post_retries():
    c, fake, clock = mk(r429("3"), OK)
    r = c.request("POST", "/search/jql", body={"jql": 'parent = "SPK-1"'}, retry_safe=True)
    assert r.data == {"ok": True} and len(fake.calls) == 2 and len(clock.sleeps) == 1
    assert fake.calls[0][3] == fake.calls[1][3]  # the same body is resent
    c, fake, clock = mk(r429("3"), OK)  # without the opt-in the search POST is not retried
    with pytest.raises(cl.JiraError):
        c.request("POST", "/search/jql", body={"jql": 'parent = "SPK-1"'})
    assert len(fake.calls) == 1 and clock.sleeps == []


def test_retry_safe_false_disables_get_retry():
    c, fake, clock = mk(r429("1"), OK)
    with pytest.raises(cl.JiraError):
        c.request("GET", "/myself", retry_safe=False)
    assert len(fake.calls) == 1 and clock.sleeps == []


# --- S7-AC2 and Retry-After parsing ---------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 503])
def test_no_retry_without_retry_after(status):
    c, fake, clock = mk((status, {}, b""), OK)
    with pytest.raises(cl.JiraError) as ei:
        c.request("GET", "/myself")
    assert ei.value.status == status and len(fake.calls) == 1 and clock.sleeps == []


@pytest.mark.parametrize("value", ["-5", "1.5", "", "  ", "soon", "１", "2 3", "0x10", "+3", "99999999999",
                                   "61", "86400", "Wed, 21 Oct 2099 07:28:00 GMT", "Wed, 99 Foo 2026"])
def test_invalid_or_huge_retry_after_not_retried(value):
    c, fake, clock = mk(r429(value, Date=DATE), OK)
    with pytest.raises(cl.JiraError):
        c.request("GET", "/myself")
    assert len(fake.calls) == 1 and clock.sleeps == []


def test_retry_after_http_date():
    later = "Wed, 21 Oct 2026 07:28:40 GMT"  # 40 s after the response's Date header
    c, _, clock = mk(r429(later, Date=DATE), OK)
    c.request("GET", "/myself")
    assert clock.sleeps == [40.0]
    past = "Wed, 21 Oct 2026 07:27:00 GMT"  # a past date means "now": backoff alone
    c, _, clock = mk(r429(past, Date=DATE), OK)
    c.request("GET", "/myself")
    assert clock.sleeps == [5.0]


def test_total_wait_is_bounded():
    c, fake, clock = mk(r429("50"))
    with pytest.raises(cl.JiraError):
        c.request("GET", "/myself")
    assert sum(clock.sleeps) <= cl.MAX_TOTAL_WAIT
    assert clock.sleeps == [50, 50] and len(fake.calls) == 3  # a third 50 s sleep would pass 120 s


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 500, 502, 504])
def test_other_statuses_not_retried(status):
    c, fake, clock = mk((status, {"Retry-After": "1"}, b""), OK)
    with pytest.raises(cl.JiraError) as ei:
        c.request("GET", "/myself", retry_safe=True)
    assert ei.value.status == status and len(fake.calls) == 1 and clock.sleeps == []


# --- S7-AC3 ---------------------------------------------------------------------------------------


def test_post_comment_never_retried():
    c, fake, clock = mk(r429("1"), OK)
    with pytest.raises(cl.JiraError) as ei:
        c.request("POST", "/issue/SPK-1/comment", body={"body": "x"})
    assert ei.value.status == 429 and len(fake.calls) == 1 and clock.sleeps == []


@pytest.mark.parametrize("method, path", [("PUT", "/issue/SPK-1"),
                                          ("DELETE", "/issue/SPK-1/comment/10001"),
                                          ("POST", "/issue"), ("POST", "/search/approximate-count")])
@pytest.mark.parametrize("status", [429, 503])
def test_writes_never_retried(method, path, status):
    c, fake, clock = mk((status, {"Retry-After": "1"}, b""), OK)
    # S6b-AC5: a 503 on a write (approximate-count is a read) is WriteUncertain; 429 stays JiraError
    uncertain = status == 503 and path != "/search/approximate-count"
    with pytest.raises(cl.WriteUncertain if uncertain else cl.JiraError):
        c.request(method, path, body={})
    assert len(fake.calls) == 1 and clock.sleeps == []


@pytest.mark.parametrize("method, path", [("PUT", "/issue/SPK-1"), ("DELETE", "/issue/SPK-1"),
                                          ("POST", "/issue/SPK-1/comment"), ("POST", "/search"),
                                          ("POST", "/search/jql/x")])
def test_retry_safe_on_a_write_is_refused_before_sending(method, path):
    c, fake, _ = mk(OK)
    with pytest.raises(ValueError):
        c.request(method, path, body={}, retry_safe=True)
    assert fake.calls == []


@pytest.mark.parametrize("flag", [1, "yes", 0])
def test_retry_safe_must_be_bool_or_none(flag):
    c, fake, _ = mk(OK)
    with pytest.raises(ValueError):
        c.request("GET", "/myself", retry_safe=flag)
    assert fake.calls == []


@pytest.mark.parametrize("exc", [socket.timeout("timed out"), TimeoutError(), ConnectionResetError(),
                                 OSError("reset")])
@pytest.mark.parametrize("method, path", [("POST", "/issue/SPK-1/comment"), ("PUT", "/issue/SPK-1"),
                                          ("DELETE", "/issue/SPK-1")])
def test_write_timeout_is_write_uncertain(exc, method, path):
    calls = []

    def boom(m, url, headers, body, timeout):
        calls.append(m)
        raise type(exc)("timed out; header was %s; %s" % (headers["Authorization"], TOKEN))

    sleeps = []
    c = cl.Client(SITE, EMAIL, TOKEN, opener=boom, sleep=sleeps.append)
    with pytest.raises(cl.WriteUncertain) as ei:
        c.request(method, path, body={"body": "x"})
    e = ei.value
    assert isinstance(e, cl.ClientError) and calls == [method] and sleeps == []
    assert "re-read" in str(e) and "kit marker" in str(e) and type(exc).__name__ in str(e)
    no_secret(full_text(e))
    assert e.__cause__ is None and e.__suppress_context__


@pytest.mark.parametrize("method, path, kw", [("GET", "/myself", {}),
                                              ("POST", "/search/jql", {"retry_safe": True})])
def test_read_timeout_is_plain_network_error_not_retried(method, path, kw):
    calls = []

    def boom(m, url, headers, body, timeout):
        calls.append(m)
        raise socket.timeout("timed out")

    sleeps = []
    c = cl.Client(SITE, EMAIL, TOKEN, opener=boom, sleep=sleeps.append)
    with pytest.raises(cl.ClientError) as ei:
        c.request(method, path, body={} if method == "POST" else None, **kw)
    assert not isinstance(ei.value, cl.WriteUncertain)
    assert calls == [method] and sleeps == []


# --- S7-AC4 ---------------------------------------------------------------------------------------


def test_rate_limit_headers_surfaced_not_acted_on():
    h = {"X-RateLimit-NearLimit": "true", "RateLimit-Reason": "jira-burst-based", "X-RateLimit-Remaining": "3"}
    c, fake, clock = mk((200, h, b"{}"))
    r = c.request("GET", "/myself")
    assert r.headers["x-ratelimit-nearlimit"] == "true"
    assert r.headers["ratelimit-reason"] == "jira-burst-based"
    c.request("GET", "/myself")
    assert len(fake.calls) == 2 and clock.sleeps == []  # no throttling
    c, _, _ = mk(r429(None, **{"RateLimit-Reason": "jira-per-issue-on-write"}))
    with pytest.raises(cl.JiraError) as ei:
        c.request("POST", "/issue/SPK-1/comment", body={})
    assert ei.value.headers["ratelimit-reason"] == "jira-per-issue-on-write"


# --- S6 guarantees still hold through the retry loop ----------------------------------------------


def test_401_never_retried_even_with_retry_safe():
    c, fake, clock = mk((401, {"Retry-After": "1"}, b""), OK)
    with pytest.raises(cl.AuthError):
        c.request("GET", "/myself", retry_safe=True)
    assert len(fake.calls) == 1 and clock.sleeps == []


def test_final_error_after_retries_has_no_secret():
    c, _, _ = mk((503, {"Retry-After": "1"}, b'{"errorMessages": ["busy %s"]}' % TOKEN.encode()))
    with pytest.raises(cl.JiraError) as ei:
        c.request("GET", "/myself")
    no_secret(full_text(ei.value))
    assert ei.value.__context__ is None or ei.value.__suppress_context__
