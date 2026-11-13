"""HTTPS transport to one pinned Jira Cloud site: the only module that carries the credential.

Every request goes to `https://<site>/rest/api/3<path>`, where `site` passed `keys.SITE_RE` at
construction and `path` is a fixed-charset `/a/b` path (no `..`, `//`, `?`, `#`, `%`, spaces or
controls); a query comes only from `params`, urlencoded here. Headers are fixed: `Authorization:
Basic base64(email:token)`, `Accept: application/json`, and `Content-Type: application/json` only
when a body is sent; the one exception is the attachment upload (P3: `content=` bytes with
`extra_headers=`, which may set only a multipart `Content-Type` and `X-Atlassian-Token: no-check`). Before sending, the final URL's scheme and host are checked against the pin.

No redirect is ever followed (any 3xx is an error), a body larger than `max_body` is refused while
it is read (Content-Length is not trusted), and only reads are retried (see "Retry" below). The email, token
and encoded header live only in closures, never in an attribute value.

Guaranteed: the client's `repr`/`str` and the `str`, `repr` and rendered traceback text of every
exception this module raises contain no email, token or header, and the default opener never prints
headers (debuglevel pinned to 0). The exception chain is clean for every `ClientError` that `request`
raises: it is raised outside any `except` block, with `__cause__` and `__context__` both None (the
opener's original exception is reduced to its class name and dropped), and its traceback holds no
frame with the request headers (`headers` is deleted first; a `ClientError` coming out of the opener
or a urllib handler is re-raised with its own traceback and context cleared), so even
`capture_locals` rendering shows no credential. NOT guaranteed: an exception that is not `OSError`,
`http.client.HTTPException` or `ClientError` (a programming error in a custom opener, e.g. an
AssertionError) propagates unchanged; a debugger stopped inside `_attempt` sees the headers.
Response bodies are never logged or echoed; server
error text is taken only from Jira's `errorMessages` / `errors` JSON, capped, stripped of control
characters and with any credential material redacted.

Opener contract (the injection seam for tests and the fake Jira; no sockets needed):

    opener(method, url, headers, body, timeout) -> (status, headers, stream)

    method   str, one of GET POST PUT DELETE
    url      str, the full https URL (already pinned and validated)
    headers  dict[str, str] of request headers (contains the credential: never store or print it)
    body     bytes or None (UTF-8 JSON, or the multipart bytes of an attachment upload)
    timeout  seconds (TIMEOUT)
    returns  status: int, the HTTP status, for EVERY status (2xx, 3xx, 4xx, 5xx: never raise for one);
             headers: any mapping with .items(), names in any case (the client lower-cases them);
             stream: bytes, None (an empty body, same as b""), or a binary file-like with read(n)
             (and optionally close()); the client reads at most max_body + 1 bytes and then closes it.
    A transport failure is raised as OSError (or http.client.HTTPException); the client turns it into
    ClientError("network error: <class name>"), without its text.

The default opener (`_urllib_opener`) is an `OpenerDirector` holding only the HTTPS handler
(debuglevel 0, its own verifying `ssl.create_default_context()`), a
redirect handler that raises, and the error handlers: no plain-HTTP, file, ftp, data or proxy
handler, so it speaks https only and ignores `HTTPS_PROXY` / `https_proxy` (and every other *_proxy
variable). Behind a mandatory proxy the symptom is `ClientError("network error: URLError")`.

Retry (S7, Atlassian's documented rule: "only retry if the API is idempotent and the response
includes a Retry-After header"). `request` loops around `Client._attempt`, which makes one request.

    Retry-safe   GET by default (`retry_safe=None`; pass False to opt out); `POST /search/jql` only
                 with `retry_safe=True`. `retry_safe=True` on any other POST, or on PUT/DELETE, is a
                 programming error: ValueError before any request. Writes are never retried.
    When         status 429 or 503 AND a valid `Retry-After`; anything else (401, 403, 404, 500, 502,
                 a timeout or network error, a 429 without Retry-After) raises at once.
    Retry-After  ASCII delay-seconds (`^[0-9]{1,10}$`) or an HTTP-date (relative to the response's
                 `Date` header, else the wall clock; a past date counts as 0). Negative, fractional
                 or unparseable means no valid Retry-After, so no retry. Above MAX_RETRY_AFTER (60 s)
                 the error is raised instead: the client never sleeps less than the server asked,
                 and a hostile server cannot park it for hours.
    Schedule     retry n (1..MAX_RETRIES=4) sleeps max(Retry-After, backoff(n)) with
                 backoff(n) = min(30, 5 * 2**(n-1)) * (0.7 + 0.6 * rand()).
                 Example, rand() = 0.5 and Retry-After 2: 5, 10, 20, 30 s; at most 5 requests.
    Budget       if a sleep would end more than MAX_TOTAL_WAIT (120 s, by `clock`) after the first
                 request started, the last error is raised instead of sleeping.

A timeout or network error on a write (any method except GET and `POST /search/jql`) raises
`WriteUncertain`: the write may have happened, so the caller re-reads and looks for its kit marker
before trying again. On a read it is a plain `ClientError` and is not retried. The same holds (S6b)
when a write (POST, PUT, DELETE except `POST /search/jql` and `POST /search/approximate-count`) gets
a 5xx status (429 stays a plain `JiraError`) or a response body larger than `max_body`: the text
names the method and the status class, never the body. Request bodies are serialised with
`allow_nan=False`: NaN or Infinity is refused locally. A body nested too deeply for the JSON parser
counts as not JSON. Rate-limit headers
(`x-ratelimit-nearlimit`, `ratelimit-reason`, ...) are surfaced lower-cased in `Response.headers`
and `JiraError.headers` and are never acted on (no throttling).
"""
import base64
import collections
import datetime
import email.utils
import http.client
import json
import random
import re
import ssl
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from .keys import SITE_RE
from .secretstore import _EMAIL_RE, _TOKEN_RE

TIMEOUT = 30  # seconds per request
MAX_BODY = 8 * 1024 * 1024
MAX_MESSAGES = 5
MAX_MESSAGE_LEN = 200  # characters of all the server text an error carries (H1a)
# an ANSI escape: CSI (ESC [ or 0x9b ... final), OSC (ESC ] ... BEL or ESC \\), or ESC and one character
_ANSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b.?", re.S)

_METHODS = frozenset(("GET", "POST", "PUT", "DELETE"))
_PATH_RE = re.compile(r"^(?:/[A-Za-z0-9._~:@,=+-]+)+\Z")
_LOGIN_REASON_RE = re.compile(r"^[A-Z_]{1,40}\Z")
# Jira Cloud answers a failed Basic login on most paths with a 404 (or 403) that carries this header, not a 401 (live run A2-28, D-003)
_LOGIN_FAILED = ("AUTHENTICATED_FAILED", "AUTHENTICATION_DENIED")
_OLD_SEARCH = "/search"
_SEARCH_JQL = "/search/jql"
_READ_POSTS = (_SEARCH_JQL, "/search/approximate-count")  # POST reads for S6b-AC5
_BAD = object()  # _loads: the body is not JSON
_DELAY_RE = re.compile(r"^[0-9]{1,10}\Z")

MAX_RETRIES = 4
MAX_RETRY_AFTER = 60  # seconds; a longer Retry-After is raised, not slept
MAX_TOTAL_WAIT = 120  # seconds from the first request
BACKOFF_BASE, BACKOFF_CAP = 5, 30
_RETRY_STATUSES = (429, 503)

Response = collections.namedtuple("Response", "status headers data")


class ClientError(Exception):
    """Transport failure or refusal (network, oversize body, pin violation); never carries a secret."""


class JiraError(ClientError):
    """Jira answered with a non-2xx status (or an unusable 2xx body). status: int or None."""

    def __init__(self, status, messages, note=""):
        self.status, self.messages = status, [m for m in map(_clean, messages) if m]
        self.headers = {}  # lower-cased response headers (rate-limit headers surface here)
        text = "HTTP %s" % status
        if self.messages:
            text += ": " + _clean("; ".join(self.messages))
        super().__init__(text + (" (%s)" % note if note else ""))


class WriteUncertain(ClientError):
    """A write timed out or lost its connection: it may or may not have happened. Never retried."""

    def __init__(self, method, cause):
        super().__init__("%s during %s; the write may have happened: re-read and look for "
                         "the kit marker before trying again" % (cause, method))


class AuthError(JiraError):
    """401: bad or missing credential. Never retried: repeated failures can trigger a CAPTCHA."""

    def __init__(self, status, messages, login_reason=""):
        self.login_reason = login_reason
        note = "authentication failed"
        if login_reason:
            note += ", X-Seraph-LoginReason: " + login_reason
        note += ("; not retried, because repeated failed logins can trigger a CAPTCHA that blocks REST "
                 "authentication; check the email and the classic API token")
        super().__init__(status, messages, note)


class NotFound(JiraError):
    """404: Jira gives the same answer for a missing item and one the user may not see."""

    def __init__(self, status, messages):
        super().__init__(status, messages, "missing or no permission")


def why(e):
    """`Jira answered HTTP <n>` for a JiraError, or, for an AuthError, that the credentials were refused: a 404 that is a failed login
    must never read as "not found" (D-003). Never echoes Jira's text or a credential."""
    status = "HTTP %s" % (e.status if type(e.status) is int else "?")
    if isinstance(e, AuthError):
        return ("Jira did not accept the credentials (%s, authentication failed; not retried: repeated failed logins can trigger a "
                "CAPTCHA; check the email and the API token, then run `skj doctor`)" % status)
    return "Jira answered " + status


class SearchRemoved(JiraError):
    """410 on the old `/search` path: the API is removed; use `/search/jql`."""

    def __init__(self, status, messages):
        super().__init__(status, messages, "the old search API is removed; use /search/jql")


def _url(site, path, params):
    if not (isinstance(path, str) and _PATH_RE.fullmatch(path)) or any(
        seg in (".", "..") for seg in path.split("/")
    ):
        raise ValueError("invalid request path")
    url = "https://%s/rest/api/3%s" % (site, path)
    if params is None:
        return url
    if not isinstance(params, dict) or not all(
        isinstance(k, str) and k and type(v) in (str, int) for k, v in params.items()
    ):
        raise ValueError("params must map names to str or int values")
    return url + "?" + urllib.parse.urlencode(params)


def _plain(text):
    """Server text as plain text: ANSI escapes removed, every other control, format (bidi, zero-width) or separator character dropped
    or a space, blanks collapsed. Linear: cut first (at 8 x MAX_MESSAGE_LEN, so the final cap never sees the cut). `Client._classify`
    redacts before AND after it: an ESC or the cut could hide a secret's start, a removed character could rebuild one."""
    text = _ANSI.sub("", str(text)[:MAX_MESSAGE_LEN * 8])
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else "" if unicodedata.category(ch)[0] == "C" else ch
                   for ch in text)
    return " ".join(text.split())


def _clean(text):
    """`_plain`, at most MAX_MESSAGE_LEN characters: what JiraError keeps (idempotent on text that is already plain and redacted)."""
    return _plain(text)[:MAX_MESSAGE_LEN]


def _loads(blob):
    try:
        return json.loads(blob.decode("utf-8"))
    except (ValueError, RecursionError):  # ValueError includes UnicodeDecodeError; deep nesting recurses
        return _BAD


def _messages(blob):
    doc = _loads(blob)
    if not isinstance(doc, dict):
        return []
    out = [m for m in doc.get("errorMessages") or [] if isinstance(m, str)] if isinstance(
        doc.get("errorMessages"), list) else []
    errors = doc.get("errors")
    if isinstance(errors, dict):
        out += ["%s: %s" % (k, v) for k, v in errors.items() if isinstance(k, str) and isinstance(v, str)]
    return out[:MAX_MESSAGES]


def _read_capped(stream, max_body):
    """The body bytes, or None when it is larger than max_body."""
    if stream is None:
        return b""
    if isinstance(stream, (bytes, bytearray)):
        return bytes(stream) if len(stream) <= max_body else None
    try:
        buf = bytearray()
        while True:
            chunk = stream.read(min(65536, max_body + 1 - len(buf)))
            if not chunk:
                return bytes(buf)
            buf += chunk
            if len(buf) > max_body:
                return None
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would copy Authorization onto the redirect: refuse every one, wherever it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise JiraError(code, [], "redirect refused: credentials are never sent on a redirect")


def _build_opener():
    od = urllib.request.OpenerDirector()
    # debuglevel=0: from 3.12 None inherits HTTPConnection.debuglevel, which prints headers to stdout.
    # Explicit context: a global ssl._create_default_https_context override cannot turn verification off.
    https = urllib.request.HTTPSHandler(debuglevel=0, context=ssl.create_default_context())
    for h in (urllib.request.UnknownHandler(), https, _NoRedirect(),
              urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
        od.add_handler(h)
    return od


_OPENER = _build_opener()


def _urllib_opener(method, url, headers, body, timeout):
    if not url.startswith("https://"):
        raise ClientError("only https is allowed")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        resp = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:  # a non-2xx status: hand it back like any other
        return e.code, e.headers, e
    return resp.status, resp.headers, resp


def _is_read(method, path):
    return method == "GET" or (method == "POST" and path == _SEARCH_JQL)


def _is_write(method, path):
    return method != "GET" and not (method == "POST" and path in _READ_POSTS)


def _retry_after(h):
    """Seconds from a valid Retry-After (delay-seconds or HTTP-date), else None."""
    value = h.get("retry-after", "").strip()
    if _DELAY_RE.fullmatch(value):
        return int(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
        ref = h.get("date")
        ref = email.utils.parsedate_to_datetime(ref) if ref else datetime.datetime.now(datetime.timezone.utc)
        utc = datetime.timezone.utc
        when, ref = (d if d.tzinfo else d.replace(tzinfo=utc) for d in (when, ref))
        delta = when - ref
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return max(0.0, delta.total_seconds())


def _dumps(body):
    try:
        return json.dumps(body, allow_nan=False).encode("utf-8")
    except ValueError:  # NaN/Infinity, or a circular reference
        return _BAD


_UPLOAD_PATH_RE = re.compile(r"/issue/[A-Z][A-Z0-9_]{1,9}-[1-9][0-9]{0,9}/attachments\Z")  # the key part is keys.KEY_RE (pinned by a test)
_MULTIPART_RE = re.compile(r"^multipart/form-data; boundary=[A-Za-z0-9'()+_,./:=?-]{1,70}\Z")  # RFC 2046 bchars, no space


def _upload_headers(method, path, body, retry_safe, content, extra):
    """The validated extra headers of an upload (P3), or None when neither `content` nor `extra` is given. (Not called
    `headers`: no frame of `request` holds a local of that name, see test_exception_chain_holds_no_credentials.)
    A plain dict of str to str only, copied first and the copy validated (a dict subclass could iterate other keys)."""
    if content is None and extra is None:
        return None
    copy = dict(extra.items()) if type(extra) is dict else None
    if not (method == "POST" and isinstance(path, str) and _UPLOAD_PATH_RE.fullmatch(path) and body is None
            and retry_safe is not True and type(content) is bytes and copy is not None
            and all(type(k) is str and type(v) is str for k, v in copy.items())
            and set(copy) <= {"Content-Type", "X-Atlassian-Token"}
            and _MULTIPART_RE.fullmatch(copy.get("Content-Type", ""))
            and copy.get("X-Atlassian-Token", "no-check") == "no-check"):
        raise ValueError("content needs POST /issue/<KEY>/attachments, bytes, no JSON body, no retry and exactly the multipart "
                         "Content-Type (and optionally X-Atlassian-Token: no-check)")
    return copy


def _redactor(secrets):
    rx = re.compile("|".join(re.escape(s) for s in secrets), re.IGNORECASE)
    return lambda text: rx.sub("[redacted]", text)


class Client:
    __slots__ = ("_site", "_opener", "_sleep", "_clock", "_rand", "_max", "_auth", "_redact")

    def __init__(self, site, email, token, *, opener=None, sleep=time.sleep, clock=time.monotonic,
                 rand=random.random, max_body=MAX_BODY):
        if not (isinstance(site, str) and SITE_RE.fullmatch(site)):
            raise ValueError("invalid site")
        if not (isinstance(email, str) and _EMAIL_RE.fullmatch(email)):
            raise ValueError("invalid email")  # never echo it
        if not (isinstance(token, str) and _TOKEN_RE.fullmatch(token)):
            raise ValueError("invalid token")
        if type(max_body) is not int or max_body < 1:
            raise ValueError("max_body must be a positive int")
        encoded = base64.b64encode(("%s:%s" % (email, token)).encode("utf-8")).decode("ascii")
        header = "Basic " + encoded
        self._auth = lambda: header  # closure: the value is in no attribute and no repr
        self._redact = _redactor((encoded, token, email))
        self._site, self._max = site, max_body
        self._opener = opener or _urllib_opener
        self._sleep, self._clock, self._rand = sleep, clock, rand

    def __repr__(self):
        return "Client(site=%r)" % self._site

    def request(self, method, path, *, body=None, params=None, retry_safe=None, raw=False, content=None, extra_headers=None):
        """Response(status, lower-cased headers dict, data). data is parsed JSON (None for an empty body),
        or the body bytes untouched when raw=True. retry_safe: None (GET retries, nothing else does),
        False, or True (GET and POST /search/jql only); see the module docstring for the schedule.
        content/extra_headers (P3, the attachment upload only): a POST whose body is the bytes `content`, sent as is,
        with `extra_headers` exactly {"Content-Type": "multipart/form-data; boundary=<1-70 safe chars>", "X-Atlassian-Token":
        "no-check"} (the token header optional); any other name or value is a ValueError, so Authorization, Accept
        and Host can never be set by a caller. Never retried, like every write."""
        if method not in _METHODS:
            raise ValueError("invalid method")
        extra = _upload_headers(method, path, body, retry_safe, content, extra_headers)
        if retry_safe is None:
            retry_safe = method == "GET"
        elif type(retry_safe) is not bool:
            raise ValueError("retry_safe must be None, True or False")
        elif retry_safe and not _is_read(method, path):
            raise ValueError("retry_safe is only for GET and POST /search/jql: a write is never retried")
        url = _url(self._site, path, params)
        data = content if extra else None if body is None else _dumps(body)
        if data is _BAD:
            raise ClientError("request body is not valid JSON (NaN, Infinity or a circular reference); "
                              "refused")
        deadline = self._clock() + MAX_TOTAL_WAIT
        for n in range(1, MAX_RETRIES + 2):
            try:
                return self._attempt(method, path, url, data, raw, extra)
            except JiraError as e:
                floor = _retry_after(e.headers) if e.status in _RETRY_STATUSES else None
                if not retry_safe or n > MAX_RETRIES or floor is None or floor > MAX_RETRY_AFTER:
                    raise
                backoff = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (n - 1)) * (0.7 + 0.6 * self._rand())
                wait = min(max(floor, backoff), MAX_RETRY_AFTER)
                if self._clock() + wait > deadline:
                    raise
            self._sleep(wait)

    def _attempt(self, method, path, url, data, raw, extra=None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.netloc != self._site:
            raise ClientError("refused: the request URL is not on the pinned site")
        headers = {"Authorization": self._auth(), "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra or {})  # only the two names _upload_headers lets through
        # Every raise below is outside the except blocks, after `del headers`: no chained context and no
        # traceback frame holding the credential (S6b-AC4).
        failure = None
        try:
            status, rheaders, stream = self._opener(method, url, headers, data, TIMEOUT)
            blob = _read_capped(stream, self._max)
        except ClientError as e:  # the opener's own refusal (https only, a redirect)
            failure = e.with_traceback(None)
            failure.__context__ = failure.__cause__ = None
        except (OSError, http.client.HTTPException) as e:
            failure = "network error: %s" % type(e).__name__
        del headers
        write = _is_write(method, path)
        if isinstance(failure, ClientError):
            raise failure from None
        if failure is not None:  # S7: uncertain on everything but GET and POST /search/jql
            err = ClientError(failure) if _is_read(method, path) else WriteUncertain(method, failure)
            raise err from None
        if blob is None:
            text = "response larger than %d bytes; refused" % self._max
            raise (WriteUncertain(method, text) if write else ClientError(text)) from None
        h = {str(k).lower(): str(v) for k, v in (rheaders or {}).items()}
        if type(status) is not int:
            raise ClientError("transport returned no HTTP status")
        if 200 <= status < 300:
            doc = blob if raw else _loads(blob) if blob else None
            if doc is _BAD:
                err = JiraError(status, [], "malformed response from Jira (not JSON)")
                raise self._error(err, h) from None
            return Response(status, h, doc)
        if write and 500 <= status < 600:
            raise WriteUncertain(method, "HTTP 5xx") from None
        raise self._error(self._classify(status, h, blob, path), h) from None

    @staticmethod
    def _error(err, h):
        err.headers = h
        return err

    def _classify(self, status, h, blob, path):
        # redact the raw text (an ESC or the pre-cut in `_plain` must not hide a secret's start), make it plain, redact again (a removed
        # invisible character must not rebuild one); JiraError's `_clean` cuts to MAX_MESSAGE_LEN only after that
        msgs = [self._redact(_plain(self._redact(m))) for m in _messages(blob)]
        if 300 <= status < 400:
            return JiraError(status, [], "redirect refused: credentials are never sent on a redirect")
        reason = h.get("x-seraph-loginreason", "")
        ok = _LOGIN_REASON_RE.fullmatch(reason) and self._redact(reason) == reason
        if status == 401:
            return AuthError(status, msgs, reason if ok else "")
        if 400 <= status < 500 and ok and reason in _LOGIN_FAILED:  # Jira's own text ("Issue does not exist") would mislead: dropped
            return AuthError(status, [], reason)
        if status == 404:
            return NotFound(status, msgs)
        if status == 410 and path == _OLD_SEARCH:
            return SearchRemoved(status, msgs)
        return JiraError(status, msgs)
