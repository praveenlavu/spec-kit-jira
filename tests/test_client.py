import base64
import http.client
import io
import ssl
import json
import traceback
import urllib.error
import urllib.request
import urllib.response
from email.message import Message

import pytest

from skj import client as cl

SITE = "example-site.atlassian.net"
EMAIL = "dev.person@example.com"
TOKEN = "FAKE-tok-9f3a7c1e5b2d-NOTREAL"
B64 = base64.b64encode(("%s:%s" % (EMAIL, TOKEN)).encode()).decode()
SECRETS = (EMAIL, TOKEN, B64)


class Fake:
    """A pure-Python opener: records every call, answers from a queue of (status, headers, body)."""

    def __init__(self, *answers):
        self.answers = list(answers) or [(200, {}, b"{}")]
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def mk(*answers, **kw):
    kw.setdefault("sleep", lambda s: None)  # no test may really sleep
    fake = Fake(*answers)
    return cl.Client(SITE, EMAIL, TOKEN, opener=fake, **kw), fake


def jbody(doc):
    return json.dumps(doc).encode()


def no_secret(text):
    for s in SECRETS:
        assert s not in text, "secret material leaked"


def full_text(exc):
    return str(exc) + repr(exc) + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


# --- S6-AC1 ---------------------------------------------------------------------------------------


def test_url_and_headers():
    c, fake = mk((200, {"Content-Type": "application/json"}, jbody({"accountId": "x"})))
    r = c.request("GET", "/myself")
    assert r.status == 200 and r.data == {"accountId": "x"}
    assert r.headers["content-type"] == "application/json"  # names lower-cased
    method, url, headers, body, timeout = fake.calls[0]
    assert (method, url, body, timeout) == ("GET", "https://%s/rest/api/3/myself" % SITE, None, cl.TIMEOUT)
    assert headers == {"Authorization": "Basic " + B64, "Accept": "application/json"}

    c.request("POST", "/search/jql", body={"jql": 'parent = "SPK-1"'}, params={"fields": "summary", "n": 5})
    method, url, headers, body, _ = fake.calls[1]
    assert url == "https://%s/rest/api/3/search/jql?fields=summary&n=5" % SITE
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"jql": 'parent = "SPK-1"'}
    assert set(headers) == {"Authorization", "Accept", "Content-Type"}


def test_params_are_urlencoded():
    c, fake = mk()
    c.request("GET", "/issue/SPK-1", params={"fields": "a b&c=d"})
    assert fake.calls[0][1].endswith("/issue/SPK-1?fields=a+b%26c%3Dd")


@pytest.mark.parametrize("params", [{"a": ["x"]}, {"a": None}, {1: "x"}, ["a"], {"a": True}])
def test_bad_params_refused_before_sending(params):
    c, fake = mk()
    with pytest.raises(ValueError):
        c.request("GET", "/myself", params=params)
    assert fake.calls == []


@pytest.mark.parametrize(
    "site",
    ["evil.example.com", "Example-site.atlassian.net", "example-site.atlassian.net.evil.com",
     "example-site.atlassian.net:443", "user@example-site.atlassian.net", "-x.atlassian.net",
     " example-site.atlassian.net", "example-site.atlassian.net\n", "", None],
)
def test_site_validated_at_construction(site):
    with pytest.raises(ValueError):
        cl.Client(site, EMAIL, TOKEN, opener=Fake())


@pytest.mark.parametrize(
    "email,token",
    [("", TOKEN), (EMAIL, ""), ("a:b@example.com", TOKEN), (EMAIL, "tok en"), (EMAIL, "tok\n"),
     (None, TOKEN), (EMAIL, None)],
)
def test_bad_identity_refused_without_echo(email, token):
    with pytest.raises(ValueError) as ei:
        cl.Client(SITE, email, token, opener=Fake())
    no_secret(full_text(ei.value))


@pytest.mark.parametrize(
    "path",
    ["myself", "/", "//evil.example.com/x", "/issue/../../admin", "/issue/./x", "/issue//x",
     "/issue/SPK-1?x=1", "/issue/SPK-1#f", "/issue/SPK 1", "/issue/SPK-1\r\nX: y", "/issue/%2e%2e",
     "/issue/é", "@evil.example.com/", "/issue/SPK-1/", "", None],
)
def test_bad_path_refused_before_sending(path):
    c, fake = mk()
    with pytest.raises(ValueError):
        c.request("GET", path)
    assert fake.calls == []


@pytest.mark.parametrize("method", ["get", "TRACE", "CONNECT", "GET\r\n", None])
def test_bad_method_refused(method):
    c, fake = mk()
    with pytest.raises(ValueError):
        c.request(method, "/myself")
    assert fake.calls == []


def test_host_pin_refuses_foreign_url(monkeypatch):
    c, fake = mk()
    monkeypatch.setattr(cl, "_url", lambda site, path, params: "https://evil.example.com/rest/api/3/myself")
    with pytest.raises(cl.ClientError) as ei:
        c.request("GET", "/myself")
    assert fake.calls == []
    no_secret(full_text(ei.value))


def test_host_pin_refuses_http_scheme(monkeypatch):
    c, fake = mk()
    monkeypatch.setattr(cl, "_url", lambda site, path, params: "http://%s/rest/api/3/myself" % SITE)
    with pytest.raises(cl.ClientError):
        c.request("GET", "/myself")
    assert fake.calls == []


# --- S6-AC2 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_refused(status):
    c, fake = mk((status, {"Location": "https://evil.example.com/x"}, b""))
    with pytest.raises(cl.JiraError) as ei:
        c.request("GET", "/attachment/content/10001")
    assert ei.value.status == status
    assert len(fake.calls) == 1  # never followed
    assert "redirect refused" in str(ei.value) and ei.value.messages == []
    assert "evil.example.com" not in str(ei.value)
    no_secret(full_text(ei.value))


def test_default_opener_redirect_handler_raises():
    req = urllib.request.Request("https://%s/rest/api/3/x" % SITE, headers={"Authorization": "Basic " + B64})
    with pytest.raises(cl.JiraError) as ei:
        cl._NoRedirect().redirect_request(req, None, 302, "Found", Message(), "https://evil.example.com/")
    assert ei.value.status == 302
    no_secret(full_text(ei.value))


def _patch_https(monkeypatch, code, headers=None, body=b""):
    """Replace the default opener's HTTPS handler socket step with an in-memory response."""
    seen = []
    handler = next(h for h in cl._OPENER.handlers if isinstance(h, urllib.request.HTTPSHandler))

    def https_open(req):
        seen.append(req.full_url)
        msg = Message()
        for k, v in (headers or {}).items():
            msg[k] = v
        resp = urllib.response.addinfourl(io.BytesIO(body), msg, req.full_url, code)
        resp.msg = "reason"  # http.client responses carry it; HTTPErrorProcessor reads it
        return resp

    monkeypatch.setattr(handler, "https_open", https_open)
    return seen


def test_default_opener_does_not_follow_302(monkeypatch):
    seen = _patch_https(monkeypatch, 302, {"Location": "https://evil.example.com/steal"})
    c = cl.Client(SITE, EMAIL, TOKEN)
    with pytest.raises(cl.JiraError) as ei:
        c.request("GET", "/attachment/content/10001")
    assert ei.value.status == 302
    assert seen == ["https://%s/rest/api/3/attachment/content/10001" % SITE]
    no_secret(full_text(ei.value))


def test_default_opener_returns_error_status_and_body(monkeypatch):
    doc = {"errorMessages": ["Issue does not exist or you do not have permission to see it."], "errors": {}}
    _patch_https(monkeypatch, 404, {"Content-Type": "application/json"}, jbody(doc))
    status, headers, stream = cl._urllib_opener("GET", "https://%s/rest/api/3/issue/SPK-9" % SITE, {}, None, 5)
    assert status == 404 and headers["content-type"] == "application/json"
    assert json.loads(stream.read()) == doc


def test_default_opener_success(monkeypatch):
    _patch_https(monkeypatch, 200, {}, b'{"ok": true}')
    assert cl.Client(SITE, EMAIL, TOKEN).request("GET", "/myself").data == {"ok": True}


@pytest.mark.parametrize("url", ["http://%s/rest/api/3/x" % SITE, "file:///etc/passwd", "ftp://%s/x" % SITE])
def test_default_opener_is_https_only(url):
    with pytest.raises((cl.ClientError, urllib.error.URLError)):
        cl._urllib_opener("GET", url, {}, None, 5)


def _https_handler(opener):
    return next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler))


def _assert_quiet_and_verifying(h):
    assert h._debuglevel == 0  # 3.12+ would otherwise inherit HTTPConnection.debuglevel and print headers
    assert h._context.verify_mode == ssl.CERT_REQUIRED and h._context.check_hostname is True


def test_default_https_handler_is_quiet_and_verifies():
    _assert_quiet_and_verifying(_https_handler(cl._OPENER))


def test_global_http_debuglevel_does_not_reach_handler(monkeypatch):
    monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 1)
    monkeypatch.setattr(ssl, "_create_default_https_context", ssl._create_unverified_context)
    _assert_quiet_and_verifying(_https_handler(cl._build_opener()))


def test_default_opener_has_no_proxy_or_plain_http_handler():
    kinds = {type(h) for h in cl._OPENER.handlers}
    assert urllib.request.HTTPHandler not in kinds
    assert urllib.request.ProxyHandler not in kinds
    assert urllib.request.HTTPRedirectHandler not in kinds


# --- S6-AC3 ---------------------------------------------------------------------------------------


class Stream:
    """Ignores Content-Length; counts what was read."""

    def __init__(self, data):
        self.buf, self.read_total, self.closed = io.BytesIO(data), 0, False

    def read(self, n=-1):
        out = self.buf.read(n)
        self.read_total += len(out)
        return out

    def close(self):
        self.closed = True


def test_oversize_response_refused():
    s = Stream(b"x" * 10000)
    c, _ = mk((200, {"Content-Length": "5"}, s), max_body=100)
    with pytest.raises(cl.ClientError) as ei:
        c.request("GET", "/myself")
    assert "100" in str(ei.value)
    assert s.read_total <= 101 and s.closed  # read at most max_body + 1, then closed


def test_body_at_cap_accepted_and_oversize_error_body_refused():
    c, _ = mk((200, {}, b'"' + b"a" * 98 + b'"'), max_body=100)
    assert c.request("GET", "/myself").data == "a" * 98
    c, _ = mk((500, {}, b"x" * 101), max_body=100)
    with pytest.raises(cl.ClientError) as ei:
        c.request("GET", "/myself")
    assert not isinstance(ei.value, cl.JiraError) or ei.value.status is None


# --- S6-AC4 ---------------------------------------------------------------------------------------


def test_secret_not_in_errors_or_repr():
    echo = jbody({"errorMessages": ["user %s token %s auth Basic %s" % (EMAIL, TOKEN, B64)], "errors": {}})
    cases = [
        (401, {"X-Seraph-LoginReason": "AUTHENTICATED_FAILED"}, echo),
        (404, {}, echo),
        (410, {}, echo),
        (500, {}, echo),
        (302, {"Location": "https://x.example.com/?" + TOKEN}, echo),
        (200, {}, b"<html>%s</html>" % TOKEN.encode()),
    ]
    for status, headers, body in cases:
        c, _ = mk((status, headers, body))
        no_secret(repr(c) + str(c))
        with pytest.raises(cl.ClientError) as ei:
            c.request("GET", "/search")
        e = ei.value
        no_secret(full_text(e))
        no_secret(repr(getattr(e, "messages", "")))
        assert e.__cause__ is None and e.__suppress_context__
    c, _ = mk()
    no_secret(repr(c) + str(c) + repr([getattr(c, a, None) for a in cl.Client.__slots__]))
    assert not hasattr(c, "__dict__")


def test_network_error_text_is_safe():
    def boom(method, url, headers, body, timeout):
        raise OSError("connection reset; header was %s" % headers["Authorization"])

    c = cl.Client(SITE, EMAIL, TOKEN, opener=boom)
    with pytest.raises(cl.ClientError) as ei:
        c.request("GET", "/myself")
    assert "OSError" in str(ei.value)
    no_secret(full_text(ei.value))
    assert ei.value.__suppress_context__


# --- S6-AC5 and error shapes ----------------------------------------------------------------------


def test_401_single_attempt_reports_login_reason():
    body = jbody({"errorMessages": ["Client must be authenticated to access this resource."], "errors": {}})
    c, fake = mk((401, {"X-Seraph-LoginReason": "AUTHENTICATED_FAILED"}, body))
    with pytest.raises(cl.AuthError) as ei:
        c.request("GET", "/myself", retry_safe=True)
    assert len(fake.calls) == 1
    e = ei.value
    assert e.status == 401 and e.login_reason == "AUTHENTICATED_FAILED"
    assert "AUTHENTICATED_FAILED" in str(e) and "CAPTCHA" in str(e)
    assert isinstance(e, cl.JiraError)


@pytest.mark.parametrize("reason", ["AUTHENTICATED_FAILED\r\nX-Evil: 1", "<script>", "a" * 80, "‮FAILED"])
def test_401_untrusted_login_reason_dropped(reason):
    c, _ = mk((401, {"X-Seraph-LoginReason": reason}, b""))
    with pytest.raises(cl.AuthError) as ei:
        c.request("GET", "/myself")
    assert ei.value.login_reason == "" and reason not in str(ei.value) and "CAPTCHA" in str(ei.value)


def test_410_is_search_removed():
    body = jbody({"errorMessages": ["The requested API has been removed. Please migrate to the "
                                    "/rest/api/3/search/jql API"], "errors": {}})
    c, fake = mk((410, {}, body))
    with pytest.raises(cl.SearchRemoved) as ei:
        c.request("GET", "/search", params={"jql": 'key = "SPK-1"'})
    assert ei.value.status == 410 and "/search/jql" in str(ei.value)
    assert len(fake.calls) == 1
    c, _ = mk((410, {}, body))
    with pytest.raises(cl.JiraError) as ei:
        c.request("GET", "/issue/SPK-1")
    assert not isinstance(ei.value, cl.SearchRemoved)


def test_404_shape():
    msg = "Issue does not exist or you do not have permission to see it."
    c, fake = mk((404, {}, jbody({"errorMessages": [msg], "errors": {}})))
    with pytest.raises(cl.NotFound) as ei:
        c.request("GET", "/issue/SPK-9")
    e = ei.value
    assert e.status == 404 and e.messages == [msg]
    assert "missing or no permission" in str(e)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("status", [500, 502])  # 429/503 with Retry-After now retry: tests/test_client_retry.py
def test_500_and_502_raise_jira_error_once(status):
    c, fake = mk((status, {"Retry-After": "1"}, b""))
    sleeps = []
    c2 = cl.Client(SITE, EMAIL, TOKEN, opener=fake, sleep=sleeps.append)
    for client in (c, c2):
        with pytest.raises(cl.JiraError) as ei:
            client.request("GET", "/myself", retry_safe=True)
        assert ei.value.status == status
    assert len(fake.calls) == 2 and sleeps == []  # only 429/503 retry (S7)


def test_400_messages_parsed_capped_and_sanitised():
    doc = {"errorMessages": ["bad\x1b[31m jql‮"] + ["m%d" % i for i in range(20)],
           "errors": {"summary": "x" * 1000, "n": 5}}
    c, _ = mk((400, {}, jbody(doc)))
    with pytest.raises(cl.JiraError) as ei:
        c.request("POST", "/issue", body={"fields": {}})
    m = ei.value.messages
    assert len(m) <= cl.MAX_MESSAGES
    assert m[0] == "bad jql"  # H1a: the ANSI sequence and the bidi override are gone, not turned into text
    assert all(len(x) <= cl.MAX_MESSAGE_LEN for x in m)
    assert all(x.isprintable() for x in m)
    assert "\x1b" not in str(ei.value)


@pytest.mark.parametrize("body", [b"<html>login</html>", b"\xff\xfe", b"{", b"not json"])
def test_bad_json_success_body(body):
    c, _ = mk((200, {}, body))
    with pytest.raises(cl.JiraError) as ei:
        c.request("GET", "/myself")
    assert "not JSON" in str(ei.value)
    assert body.decode("latin-1") not in str(ei.value)
    assert ei.value.__suppress_context__


@pytest.mark.parametrize("body", [b"<html>oops</html>", b"[1,2]", b'{"errorMessages": "x"}', b""])
def test_non_jira_error_body_gives_status_only(body):
    c, _ = mk((502, {}, body))
    with pytest.raises(cl.JiraError) as ei:
        c.request("GET", "/myself")
    assert ei.value.status == 502 and ei.value.messages == []
    assert "oops" not in str(ei.value)


def test_empty_body_and_raw():
    c, _ = mk((204, {}, b""))
    assert c.request("DELETE", "/issue/SPK-1/comment/10001").data is None
    blob = b"\x89PNG\r\n\x00not json"
    c, _ = mk((200, {}, blob))
    assert c.request("GET", "/attachment/content/10001", raw=True).data == blob
    c, _ = mk((200, {}, io.BytesIO(blob)))
    assert c.request("GET", "/attachment/content/10001", raw=True).data == blob


def test_construction_does_no_io():
    fake = Fake()
    c = cl.Client(SITE, EMAIL, TOKEN, opener=fake)
    assert fake.calls == [] and "example-site.atlassian.net" in repr(c)


# --- S6b ------------------------------------------------------------------------------------------

DEEP = b"[" * 100000 + b"]" * 100000


def test_deep_json_2xx_is_jiraerror():
    c, _ = mk((200, {"X-RateLimit-NearLimit": "true"}, DEEP))
    with pytest.raises(cl.JiraError) as ei:
        c.request("GET", "/myself")
    e = ei.value
    assert "not JSON" in str(e) and "[[" not in str(e)
    assert e.__context__ is None and e.__cause__ is None


def test_deep_json_error_body_is_jiraerror():
    c, _ = mk((400, {}, b'{"errorMessages": ' + DEEP + b"}"))
    with pytest.raises(cl.JiraError) as ei:
        c.request("GET", "/myself")
    assert ei.value.status == 400 and ei.value.messages == [] and "[[" not in str(ei.value)


def test_opener_stream_none_is_empty_body():
    c, _ = mk((204, {}, None))
    assert c.request("DELETE", "/issue/SPK-1/comment/10001").data is None
    c, _ = mk((200, {}, None))
    assert c.request("GET", "/attachment/content/10001", raw=True).data == b""
    assert "None" in cl.__doc__ and "empty body" in cl.__doc__


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_body_refused_before_request(bad):
    c, fake = mk()
    with pytest.raises(cl.ClientError) as ei:
        c.request("POST", "/issue", body={"fields": {"n": bad}})
    assert fake.calls == [] and "NaN" in str(ei.value)
    assert ei.value.__context__ is None


def _opener_raises_os_error(method, url, headers, body, timeout):
    raise OSError("hdr " + headers["Authorization"])


def _opener_raises_client_error(method, url, headers, body, timeout):
    auth = headers["Authorization"]
    try:
        raise OSError("hdr " + auth)
    except OSError:
        raise cl.ClientError("opener refused")  # implicitly chains the OSError as __context__


def _opener_oversize(method, url, headers, body, timeout):
    return 200, {}, Stream(b"x" * 500)


@pytest.mark.parametrize("opener", [_opener_raises_os_error, _opener_raises_client_error, _opener_oversize])
@pytest.mark.parametrize("method, path", [("GET", "/myself"), ("POST", "/issue")])
def test_exception_chain_holds_no_credentials(opener, method, path):
    c = cl.Client(SITE, EMAIL, TOKEN, opener=opener, sleep=lambda s: None, max_body=100)
    with pytest.raises(cl.ClientError) as ei:
        c.request(method, path, body={} if method == "POST" else None)
    e = ei.value
    assert e.__context__ is None and e.__cause__ is None
    te = traceback.TracebackException(type(e), e, e.__traceback__, capture_locals=True)
    rendered = "".join(te.format())
    no_secret(str(e) + repr(e) + rendered)
    assert "Authorization" not in rendered
    tb, names = e.__traceback__, set()
    while tb is not None:
        names |= set(tb.tb_frame.f_locals)
        assert tb.tb_frame.f_code.co_name != opener.__name__  # the opener's frames are dropped
        tb = tb.tb_next
    assert not names & {"headers", "auth"}
    assert "chain" in cl.__doc__


WRITES = [("POST", "/issue"), ("PUT", "/issue/SPK-1"), ("DELETE", "/issue/SPK-1/comment/10001"),
          ("POST", "/issue/SPK-1/transitions")]


@pytest.mark.parametrize("status", [502, 504, 500])
def test_write_5xx_is_write_uncertain(status):
    for method, path in WRITES:
        c, fake = mk((status, {"Retry-After": "1"}, jbody({"errorMessages": ["secret-body-text"]})))
        with pytest.raises(cl.WriteUncertain) as ei:
            c.request(method, path, body={"x": 1})
        e = ei.value
        assert not isinstance(e, cl.JiraError) and len(fake.calls) == 1
        assert method in str(e) and "5xx" in str(e) and "secret-body-text" not in str(e)
        assert e.__context__ is None and e.__cause__ is None


def test_write_oversize_response_is_write_uncertain():
    for status in (200, 201, 500):
        c, _ = mk((status, {}, Stream(b"x" * 500)), max_body=100)
        with pytest.raises(cl.WriteUncertain) as ei:
            c.request("POST", "/issue", body={})
        assert "POST" in str(ei.value) and "x" * 10 not in str(ei.value)
    for method, path in [("GET", "/myself"), ("POST", "/search/jql"), ("POST", "/search/approximate-count")]:
        c, _ = mk((200, {}, Stream(b"x" * 500)), max_body=100)
        with pytest.raises(cl.ClientError) as ei:
            c.request(method, path, body={} if method == "POST" else None)
        assert not isinstance(ei.value, cl.WriteUncertain) and "100" in str(ei.value)


def test_write_429_stays_jira_error():
    c, fake = mk((429, {"Retry-After": "1"}, b""))
    with pytest.raises(cl.JiraError) as ei:
        c.request("POST", "/issue", body={})
    assert ei.value.status == 429 and not isinstance(ei.value, cl.WriteUncertain) and len(fake.calls) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
@pytest.mark.parametrize("method, path", [("GET", "/myself"), ("POST", "/search/jql"),
                                          ("POST", "/search/approximate-count")])
def test_read_5xx_unchanged(status, method, path):
    c, fake = mk((status, {}, b""))
    with pytest.raises(cl.JiraError) as ei:
        c.request(method, path, body={} if method == "POST" else None)
    assert ei.value.status == status and not isinstance(ei.value, cl.WriteUncertain) and len(fake.calls) == 1


def test_malformed_2xx_carries_headers():
    c, _ = mk((200, {"X-RateLimit-NearLimit": "true"}, b"not json"))
    with pytest.raises(cl.JiraError) as ei:
        c.request("GET", "/myself")
    assert ei.value.headers == {"x-ratelimit-nearlimit": "true"}


def test_https_handler_built_with_explicit_quiet_verifying_kwargs(monkeypatch):
    seen = []

    class Spy(urllib.request.HTTPSHandler):
        def __init__(self, *args, **kwargs):
            seen.append((args, kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(urllib.request, "HTTPSHandler", Spy)
    cl._build_opener()
    (args, kwargs), = seen
    assert args == () and kwargs["debuglevel"] == 0 and type(kwargs["debuglevel"]) is int
    ctx = kwargs["context"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


def test_docstring_documents_proxy_is_ignored():
    assert "HTTPS_PROXY" in cl.__doc__ and "https_proxy" in cl.__doc__
    assert "network error: URLError" in cl.__doc__


# --- P3: a caller-supplied bytes body for the one multipart upload ---------------------------------------------------

MP = "multipart/form-data; boundary=skj-0123456789abcdef"
UP = {"Content-Type": MP, "X-Atlassian-Token": "no-check"}


def test_content_with_upload_headers_is_sent_untouched():
    c, fake = mk((200, {}, jbody([{"id": "1"}])))
    r = c.request("POST", "/issue/SPK-1/attachments", content=b"--x\r\nraw\r\n", extra_headers=dict(UP))
    (method, url, headers, body, _), = fake.calls
    assert method == "POST" and body == b"--x\r\nraw\r\n" and r.data == [{"id": "1"}]
    assert headers["Content-Type"] == MP and headers["X-Atlassian-Token"] == "no-check"
    assert headers["Authorization"] == "Basic " + B64 and headers["Accept"] == "application/json"
    assert set(headers) == {"Authorization", "Accept", "Content-Type", "X-Atlassian-Token"}


@pytest.mark.parametrize("extra", [
    {"Authorization": "Basic eA=="}, {"authorization": "Basic eA=="}, {"Host": "evil.example.com"},
    {"Accept": "*/*"}, {"X-Atlassian-Token": "nocheck"}, {"Content-Type": "application/json"},
    {"Content-Type": "multipart/form-data"}, {"Content-Type": MP + "\r\nX-Evil: 1"}, {"content-type": MP},
    {"Content-Type": "multipart/form-data; boundary=" + "a" * 71}, {"X-Atlassian-Token": 1},
])
def test_upload_headers_are_a_tiny_allow_list(extra):
    c, fake = mk()
    with pytest.raises(ValueError):
        c.request("POST", "/issue/SPK-1/attachments", content=b"x", extra_headers=dict(UP, **extra))
    assert fake.calls == []


@pytest.mark.parametrize("kw", [
    dict(content=b"x"),  # no Content-Type
    dict(extra_headers=dict(UP)),  # headers without content
    dict(content="x", extra_headers=dict(UP)),  # not bytes
    dict(content=b"x", extra_headers=dict(UP), body={"a": 1}),  # both bodies
    dict(content=b"x", extra_headers=dict(UP), retry_safe=True),
])
def test_content_misuse_is_refused_before_any_request(kw):
    c, fake = mk()
    with pytest.raises(ValueError):
        c.request("POST", "/issue/SPK-1/attachments", **kw)
    assert fake.calls == []


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_content_only_on_post(method):
    c, fake = mk()
    with pytest.raises(ValueError):
        c.request(method, "/issue/SPK-1/attachments", content=b"x", extra_headers=dict(UP))
    assert fake.calls == []


def test_upload_is_never_retried_and_a_timeout_is_uncertain():
    c, fake = mk((503, {"Retry-After": "1"}, b""))
    with pytest.raises(cl.WriteUncertain):
        c.request("POST", "/issue/SPK-1/attachments", content=b"x", extra_headers=dict(UP))
    assert len(fake.calls) == 1

    def boom(*a):
        fake.calls.append(a)
        raise TimeoutError("timed out")
    c2 = cl.Client(SITE, EMAIL, TOKEN, opener=boom, sleep=lambda s: None)
    with pytest.raises(cl.WriteUncertain) as ei:
        c2.request("POST", "/issue/SPK-1/attachments", content=b"x", extra_headers=dict(UP))
    no_secret(full_text(ei.value))
    assert len(fake.calls) == 2


def test_json_requests_are_unchanged_by_the_new_parameters():
    c, fake = mk()
    c.request("POST", "/issue", body={"a": 1})
    (_, _, headers, body, _), = fake.calls
    assert headers["Content-Type"] == "application/json" and "X-Atlassian-Token" not in headers and body == b'{"a": 1}'


class _Smuggler(dict):
    """A dict subclass whose iteration shows only the allowed names, while its items carry more."""

    def __iter__(self):
        return iter(["Content-Type"])

    def keys(self):
        return ["Content-Type"]


def test_a_dict_subclass_cannot_smuggle_headers():
    c, fake = mk()
    evil = _Smuggler({"Content-Type": MP, "Authorization": "Basic eA==", "Host": "evil.example.com"})
    with pytest.raises(ValueError):
        c.request("POST", "/issue/SPK-1/attachments", content=b"x", extra_headers=evil)
    with pytest.raises(ValueError):
        c.request("POST", "/issue/SPK-1/attachments", content=b"x", extra_headers=_Smuggler({"Content-Type": MP}))
    assert fake.calls == []


@pytest.mark.parametrize("bad", [{"Content-Type": MP, "X-Atlassian-Token": b"no-check"}, {"Content-Type": MP.encode()}])
def test_upload_header_values_must_be_str(bad):
    c, fake = mk()
    with pytest.raises(ValueError):
        c.request("POST", "/issue/SPK-1/attachments", content=b"x", extra_headers=bad)
    assert fake.calls == []


@pytest.mark.parametrize("path", ["/search/jql", "/issue", "/issue/SPK-1/comment", "/issue/spk-1/attachments",
                                  "/issue/SPK-01/attachments", "/issue/10001/attachments", "/issue/SPK-1/attachments/x"])
def test_content_only_on_the_attachments_path(path):
    c, fake = mk()
    with pytest.raises(ValueError):
        c.request("POST", path, content=b"x", extra_headers=dict(UP))
    assert fake.calls == []


def test_upload_path_key_part_matches_keys_key_re():
    from skj import keys
    sample = ["SPK-1", "AB-9999999999", "A_1-2", "ABCDEFGHIJ-1", "ABCDEFGHIJK-1", "A-1", "SPK-0", "SPK-01", "spk-1", "1SP-1",
              "SP_-3", "SPK-12345678901", "SPK-1x", "SPK--1", "ÄB-1", "SPK-١"]
    for key in sample:
        assert bool(cl._UPLOAD_PATH_RE.fullmatch("/issue/%s/attachments" % key)) == keys.valid_key(key), key


# --- DF1 (live runs A2-28, A5-35, D-003): a failed Basic login answered with 404 + X-Seraph-LoginReason ---------------------------
# Hand-written fixture of the live shape: Jira Cloud answered `GET /issue/SPK-8` with a bad identity as HTTP 404, the header
# `x-seraph-loginreason: AUTHENTICATED_FAILED` and the usual "does not exist" body (evidence A2/A2-28.txt, headers only).
LIVE_404_BODY = jbody({"errorMessages": ["Issue does not exist or you do not have permission to see it."], "errors": {}})
LIVE_404_AUTH = (404, {"x-seraph-loginreason": "AUTHENTICATED_FAILED"}, LIVE_404_BODY)


@pytest.mark.parametrize("method,path,body", [("GET", "/issue/SPK-8", None), ("POST", "/issue/SPK-8/comment", {"body": "x"}),
                                              ("PUT", "/issue/SPK-8", {"fields": {}}), ("POST", "/issue/SPK-8/transitions", {}),
                                              ("POST", "/search/jql", {"jql": "x"}), ("GET", "/issue/SPK-8/comment", None)])
def test_404_with_a_failed_login_is_an_auth_error_on_every_path(method, path, body):
    c, fake = mk(LIVE_404_AUTH, LIVE_404_AUTH)
    with pytest.raises(cl.AuthError) as ei:
        c.request(method, path, body=body, retry_safe=method == "GET" or path == "/search/jql")
    e = ei.value
    assert len(fake.calls) == 1 and e.status == 404 and e.login_reason == "AUTHENTICATED_FAILED"
    assert "authentication failed" in str(e) and "does not exist" not in str(e) and "CAPTCHA" in str(e)
    no_secret(full_text(e))


def test_403_with_authentication_denied_is_an_auth_error():
    c, _ = mk((403, {"X-Seraph-LoginReason": "AUTHENTICATION_DENIED"}, b""))
    with pytest.raises(cl.AuthError):
        c.request("GET", "/issue/SPK-8")


@pytest.mark.parametrize("headers", [{}, {"x-seraph-loginreason": "OK"}, {"x-seraph-loginreason": "AUTHORISATION_FAILED"},
                                     {"x-seraph-loginreason": "AUTHENTICATED_FAILED\r\nX: 1"}])
def test_404_without_a_failed_login_stays_not_found(headers):
    c, _ = mk((404, headers, LIVE_404_BODY))
    with pytest.raises(cl.NotFound) as ei:
        c.request("GET", "/issue/SPK-8")
    assert not isinstance(ei.value, cl.AuthError)


def test_why_names_a_refused_login_and_keeps_the_plain_status_otherwise():
    assert cl.why(cl.JiraError(400, [])) == "Jira answered HTTP 400"
    assert cl.why(cl.NotFound(404, [])) == "Jira answered HTTP 404"
    auth = cl.why(cl.AuthError(404, [], "AUTHENTICATED_FAILED"))
    assert "HTTP 404" in auth and "authentication failed" in auth and "not retried" in auth
    assert cl.why(cl.JiraError(None, [])) == "Jira answered HTTP ?"
