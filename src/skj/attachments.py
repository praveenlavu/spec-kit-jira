"""P3: upload ledger files to a Jira issue (library only: P4 `done` is the caller, behind its X2 gate; no CLI verb, K9).

Two steps. `plan_attachments(c, root, key, paths)` is read-only: it validates every path, reads each file ONCE through one descriptor,
then one `GET /issue/{key}?fields=attachment` decides upload or skip per file. `apply_attachments(c, plan)` writes: one paced
`POST /issue/{key}/attachments` per file to upload, never retried, with exactly the bytes the plan read. It accepts only the very object
`plan_attachments` returned, once (a hand-built plan, a copy or a second apply is refused), so unvalidated paths never reach Jira.

What may be uploaded (P3-AC1; the fork's allow-list .md/.svg/.txt, extension case-insensitive as in the fork):
- `paths` is a list of 1 to MAX_FILES (10) strings, each exactly `dod/<name>`, relative to the repository root `root` (resolved first,
  so a symlinked checkout reads inside the real one). `dod/` is the ledger directory (K8). Refused: absolute paths, `..`, `.`, `//`,
  backslashes, nested directories, `.git`, the same name twice (compared case-insensitively: one file on a case-insensitive filesystem).
- `<name>` is plain ASCII: a letter or digit, then up to 99 letters, digits, `_` or `-`, then ONE extension `.md`, `.svg` or `.txt`.
  So no hidden file, no double extension (`a.md.exe`, `a.b.md`), no quote, blank, control, bidi or non-ASCII character; the multipart
  `filename` needs no quoting and no RFC 5987 form (non-ASCII names are refused, a decision: none is expected in dod/).
- `dod/` is opened with O_DIRECTORY|O_NOFOLLOW and the file relative to that descriptor with O_NOFOLLOW|O_NONBLOCK, then tested on
  the descriptor: a regular file with one link (a symlink, FIFO, device, directory or hard link is refused; the fork did not check
  hard links), spelled exactly as on disk (a case-insensitive filesystem would open `dod_spk-1.md` for `DoD_SPK-1.md`), 1 byte to
  MAX_BYTES (10 MiB, a local cap: Jira's default limit is 10 MB, UNVERIFIED on the site; the fork capped at 5 MiB), strict UTF-8
  without NUL (all three types are text; the fork's `done` read its files as UTF-8 too). Read once; exactly those bytes are uploaded.
- An `.svg` must be well-formed XML whose root is `svg` in the SVG namespace and whose every element is in the SVG namespace (XHTML,
  MathML or any other namespace refused), attributes un-namespaced or xlink/xml only (`xml:base` refused), the XML declaration (if any)
  utf-8, no backslash and no `/*` anywhere in the file (CSS escapes or comments could hide `url(`/`@import`), and nothing active (fail
  closed): no DOCTYPE or entity declaration (also keeps
  the parser away from entity expansion), no processing instruction but the XML declaration, no CDATA, no character reference (`&#`,
  which could spell `javascript:`), no script / foreignObject / iframe / embed / object / handler / listener element, no `on*`
  attribute or animation of one, no href / xlink:href except a local `#fragment`, no src, no `javascript:` / `vbscript:` / `data:`,
  no `url(` other than `url(#...)`, no `@import`, `expression(`, `-moz-binding` or `behavior:`.
- SVG: plain SVG only; CSS is allow-listed; Inkscape/Graphviz output with DOCTYPE or editor namespaces is refused (use Plain SVG).
  A `<style>` element must hold only text (no child element and no XML comment: either could split a CSS token, `u<!---->rl(`); its
  joined text and EVERY attribute value (presentation attributes and SMIL to/values/from/by are parsed as CSS) pass `_css_problem`: no `@`, backslash, `/*`, `<`, `>` or `&`; a quoted string holds
  no `:` or `/` (font names pass, URLs do not); every function is one of rgb rgba hsl hsla translate translatex translatey rotate
  scale scalex scaley skewx skewy matrix calc var url, and `url(` only as `url(#id)`, `url("#id")` or `url('#id')`; and no
  `expression`, `behavior`, `-moz-binding` or `image-set` anywhere. XML comments outside `<style>` stay allowed.

Idempotence (P3-AC2): a file whose name AND size are already in `fields.attachment` is skipped. Same name, different size: uploaded,
and the plan says so (Jira keeps both; the fork skipped on the name alone). The listing must be a list of at most MAX_LISTED dicts with
a str `filename` and an int `size` (bool is not int); anything else refuses the plan.

The request (P3-AC3): `X-Atlassian-Token: no-check`, `Content-Type: multipart/form-data; boundary=skj-<32 random hex>` (redrawn while it
occurs in the payload), CRLF framing, one part `Content-Disposition: form-data; name="file"; filename="<name>"` with a fixed
`Content-Type` per extension (text/markdown, image/svg+xml, text/plain; never sniffed). Answers: 2xx with a list holding the file's
name and size is success; a 4xx is a fixed message and stops the run; a timeout, a 5xx or a 2xx it cannot read is uncertain: one re-read
of `fields.attachment` looks for the name and size, the result is reported as is, and the file is never sent twice in a run (J7: an
upload that shows up only later would be uploaded again by the next run; Jira keeps both). Messages name only `dod/<name>`.

U6: upload has NEVER run against a live site; the multipart shape, the XSRF header, 413/415, the redirect behaviour and the shape of
`fields.attachment` are our reading of the API documentation, pinned against the fake only.
"""
import errno
import os
import re
import secrets
import stat
import time
import weakref
import xml.etree.ElementTree as ET
from collections import namedtuple

from . import client as _client, issues, keys, push

MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 10
MAX_LISTED = 1000
LEDGER_DIR = "dod"
CTYPES = {".md": "text/markdown", ".svg": "image/svg+xml", ".txt": "text/plain"}
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}(\.[A-Za-z]{2,3})\Z", re.ASCII)
_SHOWN_RE = re.compile(r"[A-Za-z0-9._/-]{1,120}\Z", re.ASCII)
_BOUNDARY_RE = re.compile(r"skj-[0-9a-f]{32}\Z", re.ASCII)

E_PLAN = ("refused: apply_attachments takes only a plan that plan_attachments returned, once (not a hand-built plan, a copy, or "
          "one already applied); nothing uploaded")
E_COUNT = "at most %d files can be uploaded at once, and at least one (a list of dod/<name> paths); nothing uploaded"
E_NAME = ("%s: not uploadable: only dod/<name>.md, .svg or .txt (one plain ASCII name directly in dod/: a letter or digit, then "
          "letters, digits, _ or -, one extension); nothing uploaded")
E_TWICE = "%s: named twice (names are compared ignoring case); nothing uploaded"
E_DOD = "dod/ is not a directory inside the repository (missing, a symbolic link or another kind of file); nothing uploaded"
E_LISTED = "the attachments of %s could not be read safely (malformed or more than %d); nothing uploaded"
E_SVG = "%s: the SVG is refused: it is not plain, well-formed SVG or it holds active or external content (%s); nothing uploaded"


class Refused(ValueError):
    """A path, file, listing or plan the upload refuses; the text is fixed and names at most `dod/<name>`."""


class Item(namedtuple("Item", "rel name size ctype action note data")):
    """One planned file: action "upload" or "skip"; `data` the exact bytes read (kept out of repr)."""
    __slots__ = ()

    def __repr__(self):
        return "Item(rel=%r, size=%d, action=%r)" % (self.rel, self.size, self.action)


class Plan:
    """What `plan_attachments` decided: `key`, `items` (tuple of Item), `lines` (tuple of str). Read-only; cannot be copied."""
    __slots__ = ("key", "items", "lines", "__weakref__")

    def __setattr__(self, k, v):
        raise AttributeError("a plan is read-only")

    def __reduce_ex__(self, protocol):
        raise TypeError("a plan cannot be copied or pickled")

    def __repr__(self):
        return "Plan(key=%r, items=%r)" % (self.key, self.items)


# The Plans plan_attachments returned and nobody applied yet -> (key, items) as validated. apply uses these values, never the
# object's attributes, so even object.__setattr__ on a real plan cannot swap in unvalidated items.
_ISSUED = weakref.WeakKeyDictionary()


def _shown(rel):
    return rel if isinstance(rel, str) and _SHOWN_RE.fullmatch(rel) and not rel.startswith("/") else "a path"


def _check_name(rel):
    """The file name of `dod/<name>`, or Refused(E_NAME)."""
    parts = rel.split("/") if isinstance(rel, str) and rel.isascii() else []
    m = _NAME_RE.fullmatch(parts[1]) if len(parts) == 2 and parts[0] == LEDGER_DIR else None
    if not (m and m.group(1).lower() in CTYPES):
        raise Refused(E_NAME % _shown(rel))
    return parts[1]


def _read(dfd, rel, name):
    """The bytes of dod/<name>, opened relative to the dod/ descriptor without following a link, tested on the descriptor, read once."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise Refused("%s: is a symbolic link; refusing to upload it (upload the real file)" % rel) from None
        raise Refused("%s: missing or unreadable; nothing uploaded" % rel) from None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):  # tested before fdopen, which refuses a directory with its own text
        os.close(fd)
        raise Refused("%s: not a regular file (a directory, FIFO or device is refused); nothing uploaded" % rel)
    with os.fdopen(fd, "rb") as f:
        if st.st_nlink != 1:
            raise Refused("%s: has another hard link; refusing to upload it (copy it instead); nothing uploaded" % rel)
        if name not in os.listdir(dfd):
            raise Refused("%s: spelled differently from the file on disk (a case-insensitive filesystem); nothing uploaded" % rel)
        try:
            data = f.read(MAX_BYTES + 1)
        except OSError:
            raise Refused("%s: missing or unreadable; nothing uploaded" % rel) from None
    if len(data) > MAX_BYTES:
        raise Refused("%s: larger than %d bytes; refusing to upload it" % (rel, MAX_BYTES))
    if not data:
        raise Refused("%s: empty; nothing uploaded" % rel)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = "\x00"
    if "\x00" in text:
        raise Refused("%s: not UTF-8 text (or it holds a NUL byte); nothing uploaded" % rel)
    if name.lower().endswith(".svg"):
        why = _svg_problem(text)
        if why:
            raise Refused(E_SVG % (rel, why))
    return data


_SVG_TOKENS = ("<!doctype", "<!entity", "<!attlist", "<!element", "<![cdata[", "&#", "<script", "<foreignobject", "<iframe",
               "<embed", "<object", "<handler", "<listener", "javascript:", "vbscript:", "data:", "@import", "expression(",
               "-moz-binding", "behavior:")
_SVG_TAGS = frozenset(("script", "foreignobject", "iframe", "embed", "object", "handler", "listener"))
_URL_RE = re.compile(r"url\((?![\"']?#)")
_XML_DECL_RE = re.compile(r"\A\ufeff?<\?xml[ \t\r\n][^?<>]*\?>")
_BLANKS_RE = re.compile(r"[\x00-\x20]+")
_ENCODING_RE = re.compile(r"encoding\s*=\s*[\"']([^\"']*)[\"']")
_CSS_FUNCS = frozenset("rgb rgba hsl hsla translate translatex translatey rotate scale scalex scaley skewx skewy matrix calc var url".split())
_CSS_STRING_RE = re.compile(r"\"[^\"]*\"|'[^']*'")
# An identifier run (with any blanks and a "(" after it) or a bare "(": tokens never overlap and no attempt rescans a blank or
# identifier run, so the scan is linear (an optional-ident form `([..]*)\s*\(` retried at every blank was quadratic).
_CSS_FUNC_RE = re.compile(r"[A-Za-z0-9_-]+\s*\(?|\(")
_CSS_URL_RE = re.compile(r"url\(\s*(?:#[A-Za-z0-9_.-]+|\"#[A-Za-z0-9_.-]+\"|'#[A-Za-z0-9_.-]+')\s*\)", re.I)
_CSS_URL_START_RE = re.compile(r"url\s*\(", re.I)
_CSS_WORDS = ("expression", "behavior", "-moz-binding", "image-set")
_SVG_NS, _XLINK_NS, _XML_NS = "{http://www.w3.org/2000/svg}", "{http://www.w3.org/1999/xlink}", "{http://www.w3.org/XML/1998/namespace}"


def _css_problem(css):
    """None when the CSS (a style element's joined text or a style attribute) passes the allow-list, else a short fixed reason."""
    low = css.lower()
    if any(ch in css for ch in "@\\<>&") or "/*" in css:
        return "its CSS has an at-rule, an escape, a comment or markup"
    if any(w in low for w in _CSS_WORDS):
        return "its CSS uses a refused property or value"
    strings = _CSS_STRING_RE.findall(css)
    rest = _CSS_STRING_RE.sub('""', css)
    if '"' in rest.replace('""', "") or "'" in rest or any(":" in t or "/" in t for t in strings):
        return "its CSS has a quoted string holding : or / (or an unclosed quote)"
    calls = (m.group(0) for m in _CSS_FUNC_RE.finditer(rest) if m.group(0).endswith("("))
    if any(c[:-1].strip().lower() not in _CSS_FUNCS for c in calls):  # a bare "(" has the empty name: refused
        return "its CSS calls a function that is not allowed"
    if any(not _CSS_URL_RE.match(css, m.start()) for m in _CSS_URL_START_RE.finditer(css)):  # on the text with its strings
        return "its CSS loads a url() that is not a local #fragment"
    return None


def _svg_problem(text):
    """None when the SVG text is plain, else a short fixed reason. Text checks first (on the lowercased text with every blank
    removed, so `java\\nscript:` is caught), then the parse (no DOCTYPE reached it, so no entity can expand), then each element."""
    low = text.lower()
    squashed = _BLANKS_RE.sub("", low)
    for tok in _SVG_TOKENS:
        if tok in squashed:
            return "it contains %s" % tok
    if _URL_RE.search(squashed):
        return "it loads a url() that is not a local #fragment"
    if "\\" in text or "/*" in squashed:  # anywhere in the file: a CSS escape (\75 rl) or comment could hide url( or @import
        return "it contains a backslash or a /* comment"
    decl = _XML_DECL_RE.match(low)
    if "<?" in low[decl.end() if decl else 0:]:
        return "it has a processing instruction"
    enc = _ENCODING_RE.search(decl.group(0)) if decl else None
    if enc and enc.group(1) != "utf-8":
        return "it declares an encoding other than utf-8"
    try:  # comments kept in the tree, so a comment inside <style> is seen as a child
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        parser.feed(text)
        root = parser.close()
    except (ET.ParseError, ValueError, RecursionError):
        return "it is not well-formed XML"
    if root.tag != _SVG_NS + "svg":
        return "its root element is not svg in the SVG namespace"
    for el in root.iter():
        if el.tag is ET.Comment:
            continue  # a comment outside <style> (Graphviz writes them) is inert
        if not (isinstance(el.tag, str) and el.tag.startswith(_SVG_NS)):
            return "it has an element outside the SVG namespace"
        tag = el.tag[len(_SVG_NS):].lower()
        if tag in _SVG_TAGS:
            return "it has a %s element" % tag
        if tag == "style":
            if len(el):
                return "its style element holds a comment or an element"
            why = _css_problem("".join(el.itertext()))
            if why:
                return why
        for k, v in el.attrib.items():
            why = _css_problem(v)  # every value: browsers parse presentation attributes and SMIL to/values/from/by as CSS
            if why:
                return why
            if k.startswith("{") and not k.startswith((_XLINK_NS, _XML_NS)):
                return "it has an attribute outside the SVG, xlink and xml namespaces"
            if k == _XML_NS + "base":
                return "it sets xml:base"
            attr, val = k.rsplit("}", 1)[-1].lower(), v.strip().lower()
            if attr.startswith("on"):
                return "it has an event handler attribute"
            if attr == "href" and not val.startswith("#"):
                return "it links to something that is not a local #fragment"
            if attr == "src":
                return "it has a src attribute"
            if attr == "attributename" and (val.startswith("on") or val.endswith("href")):
                return "it animates an event handler or a link"
    return None


def _existing(c, key):
    """{(filename, size)} of the issue's attachments, from `GET /issue/{key}?fields=attachment`; Refused when malformed or too many."""
    try:
        d = issues.get(c, key, fields=["attachment"])
    except issues.ReadError:
        raise Refused(E_LISTED % (key, MAX_LISTED)) from None
    fields = d.get("fields")
    att = fields.get("attachment") if isinstance(fields, dict) else None
    if not (isinstance(att, list) and len(att) <= MAX_LISTED):
        raise Refused(E_LISTED % (key, MAX_LISTED))
    out = set()
    for a in att:
        if not (isinstance(a, dict) and type(a.get("filename")) is str and type(a.get("size")) is int and a["size"] >= 0):
            raise Refused(E_LISTED % (key, MAX_LISTED))
        out.add((a["filename"], a["size"]))
    return out


def plan_attachments(c, root, key, paths):
    """The Plan for uploading `paths` (each `dod/<name>`, relative to the repository root `root`) to issue `key`. Read-only: every
    local check runs before the one GET; any refusal (Refused) refuses the whole plan. A transport error of the GET propagates."""
    if not keys.valid_key(key):
        raise Refused("invalid issue key; nothing uploaded")
    if not (all(hasattr(os, f) for f in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")) and os.open in os.supports_dir_fd
            and os.listdir in os.supports_fd):
        raise Refused("uploading attachments needs a POSIX filesystem (O_NOFOLLOW, O_DIRECTORY, dir_fd); nothing uploaded")
    if not (isinstance(paths, list) and 1 <= len(paths) <= MAX_FILES):
        raise Refused(E_COUNT % MAX_FILES)
    seen, checked = set(), []
    for rel in paths:
        name = _check_name(rel)
        if name.lower() in seen:
            raise Refused(E_TWICE % rel)
        seen.add(name.lower())
        checked.append((rel, name))
    try:
        dfd = os.open(os.path.join(os.path.realpath(str(root)), LEDGER_DIR), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise Refused(E_DOD) from None
    try:
        read = [(rel, name, _read(dfd, rel, name)) for rel, name in checked]
    finally:
        os.close(dfd)
    have = _existing(c, key)
    names = {n for n, _ in have}
    items, lines = [], []
    for rel, name, data in read:
        ctype = CTYPES[os.path.splitext(name)[1].lower()]
        if (name, len(data)) in have:
            items.append(Item(rel, name, len(data), ctype, "skip", "already attached", data))
            lines.append("skip %s: already attached (same name and size)" % rel)
            continue
        note = "a different %s is already attached; Jira keeps both" % name if name in names else ""
        items.append(Item(rel, name, len(data), ctype, "upload", note, data))
        lines.append("upload %s (%d bytes, %s)" % (rel, len(data), ctype) + ("; " + note if note else ""))
    p = object.__new__(Plan)
    for k, v in (("key", key), ("items", tuple(items)), ("lines", tuple(lines))):
        object.__setattr__(p, k, v)
    _ISSUED[p] = (key, tuple(items))
    return p


def multipart(name, ctype, data, token=secrets.token_hex):
    """(Content-Type header value, body bytes) of one `file` part. The boundary `skj-<32 hex>` is redrawn while it occurs in the
    payload (at most 8 draws, then Refused). `name` must be a valid dod/ name, so it needs no quoting."""
    m = _NAME_RE.fullmatch(name) if isinstance(name, str) else None
    if not (m and CTYPES.get(m.group(1).lower()) == ctype and type(data) is bytes):
        raise Refused("not uploadable; nothing uploaded")
    for _ in range(8):
        boundary = "skj-" + token(16)
        if _BOUNDARY_RE.fullmatch(boundary) and boundary.encode("ascii") not in data:
            break
    else:
        raise Refused("no multipart boundary absent from the file could be drawn; nothing uploaded")
    head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
            % (boundary, name, ctype)).encode("ascii")
    return "multipart/form-data; boundary=" + boundary, head + data + ("\r\n--%s--\r\n" % boundary).encode("ascii")


_WHY = {400: "Jira refused the upload (HTTP 400)", 403: "no permission to attach files to %s, or attachments are off (HTTP 403)",
        404: "%s is missing or not visible (HTTP 404)", 413: "the file is larger than Jira accepts (HTTP 413)",
        415: "Jira refused the upload format (HTTP 415)"}


def _answer_ok(d, it):
    return isinstance(d, list) and any(isinstance(a, dict) and a.get("filename") == it.name and type(a.get("size")) is int
                                       and a["size"] == it.size for a in d)


def _recheck(c, key, it):
    """(line, found) after an uncertain upload: one re-read of the attachments, never a second upload."""
    try:
        found = (it.name, it.size) in _existing(c, key)
    except (_client.ClientError, Refused):
        return "write uncertain: %s: the re-read failed; not uploaded again in this run (the next run's plan decides)" % it.rel, False
    if found:
        return "uploaded %s (found on re-read)" % it.rel, True
    return "write uncertain: %s was not found on re-read; not uploaded again in this run" % it.rel, False


def apply_attachments(c, plan, clock=time.monotonic, sleep=time.sleep):
    """The plan's uploads: {"lines", "ok"}. Only a plan `plan_attachments` returned, once (else Refused(E_PLAN)). Each upload paced
    (push's pacer), never retried. A 4xx: that file was not uploaded and the run stops. Uncertain (timeout, 5xx, an answer it cannot
    read): one re-read decides; found means uploaded and the run goes on, otherwise the run stops. Files not reached are listed."""
    try:
        sealed = _ISSUED.pop(plan, None) if type(plan) is Plan else None
    except TypeError:
        sealed = None
    if sealed is None:
        raise Refused(E_PLAN)
    key, items = sealed[0], list(sealed[1])
    pacer, lines = push._Pacer(clock, sleep), []
    for n, it in enumerate(items):
        if it.action == "skip":
            lines.append("skipped %s: already attached" % it.rel)
            continue
        pacer.wait()
        ctype, body = multipart(it.name, it.ctype, it.data)
        try:
            d = c.request("POST", "/issue/%s/attachments" % key, content=body,
                          extra_headers={"Content-Type": ctype, "X-Atlassian-Token": "no-check"}).data
            if not _answer_ok(d, it):
                raise _client.WriteUncertain("POST", "a malformed answer from Jira")
            lines.append("uploaded %s (%d bytes)" % (it.rel, it.size))
            continue
        except _client.ClientError as e:
            if isinstance(e, _client.JiraError) and type(e.status) is int and 400 <= e.status < 500:
                why = _client.why(e) if isinstance(e, _client.AuthError) else _WHY.get(e.status, _client.why(e))
                lines.append("failed %s: %s; nothing uploaded for it" % (it.rel, why.replace("%s", key)))
            else:
                line, found = _recheck(c, key, it)
                lines.append(line)
                if found:
                    continue
        rest = [x.rel for x in items[n + 1:] if x.action == "upload"]
        if rest:
            lines.append("%s: not uploaded in this run (run again)" % ", ".join(rest))
        return {"lines": lines, "ok": False}
    return {"lines": lines, "ok": True}
