"""Markdown subset <-> Atlassian Document Format (ADF), and the canonical form approvals are hashed over.

Writer (`md_to_adf`) emits only: doc, paragraph, heading (1-6), bulletList, orderedList, listItem, codeBlock, text, hardBreak;
marks strong, code, link. Never `mention`, never an empty text node (Jira rejects both shapes or they are not ours).
Reader (`adf_to_md`) renders those plus blockquote, rule, mention (a cut accountId, never the name), inlineCard, emoji, date,
status. Any other node or mark, and any malformed node (untrusted JSON), raises `AdfUnknown` in strict mode (every trust path)
and is reported otherwise: an inline `[?<kind>]` marker in the text plus the kind appended to the caller's `unknown` list.

The reader escapes a paragraph line that would otherwise read as block syntax (`>`, a heading, a list item, a fence, `---`) with
one leading backslash, and the writer removes exactly that backslash, so literal text never renders like a quote, a heading or
a rule. Separation between blocks is always one blank line: ADF keeps no blank-line information.
`canonical_md` is strict `adf_to_md`; it ignores every attribute it does not render (so `attrs.localId` anywhere), mark
order, key order and how text is split into nodes. `digest` is the sha256 hex of it.
"""
import hashlib
import json
import re
import unicodedata

MAX_BODY = 1 << 20  # 1 MiB, the local cap for a body on write and on read (the server's own limit is UNVERIFIED)
MAX_DEPTH = 100  # JSON container nesting (a node level is two: the node and its content list)

_HEAD = re.compile(r"(#{1,6})[ \t]+(.*)")
_BULLET = re.compile(r"[-*][ \t]+(.*)")
_ORDERED = re.compile(r"([0-9]{1,9})\.[ \t]+(.*)")
_FENCE = re.compile(r"```([A-Za-z0-9_+-]{0,30})[ \t]*")
_LANG = re.compile(r"[A-Za-z0-9_+-]{0,30}")
# inline: `code`, **strong**, [text](https://url); nothing nested, by design
_INLINE = re.compile(r"`([^`\n]+)`|\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_HREF = re.compile(r"https?://[^)\s]+")
# a paragraph line starting like this is escaped by the reader (the backslashes keep the escape a bijection)
_BLOCKISH = re.compile(r"\\*(?:>|#{1,6}[ \t]|[-*][ \t]|[0-9]{1,9}\.[ \t]|```|---$)")

# Every other character `str.splitlines` breaks a line on. The writer refuses them, strict mode raises on them, and non-strict
# mode replaces the whole node with a `[?kind]` marker, so no rendering ever holds a line break a consumer could split on
# that the reader did not put there itself (a quoted `/approve` must not become a line of its own).
_BREAKS = "\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
_BREAKS_ESC = {ord(c): "\\u%04x" % ord(c) for c in _BREAKS + "\n\r"}

_BLOCKS = {"paragraph", "heading", "bulletList", "orderedList", "codeBlock", "blockquote", "rule"}
_INLINES = {"text", "hardBreak", "mention", "inlineCard", "emoji", "date", "status"}
_KNOWN = _BLOCKS | _INLINES | {"doc", "listItem"}
_MARKS = ("code", "strong", "link")  # rendering order, innermost first


class AdfError(ValueError):
    """A body the kit refuses to handle (too large, too deep, not a str, not JSON)."""


class AdfUnknown(AdfError):
    """A node, mark or attribute outside the known set, or a malformed node."""

    def __init__(self, node_type):
        self.node_type = node_type.translate(_BREAKS_ESC)  # a hostile type name lands in the marker
        super().__init__("ADF content not allowed here: %s" % node_type)


def _cap(size):
    if size > MAX_BODY:
        raise AdfError("body is %d bytes, over the 1 MiB local cap; refused" % size)


def _check(doc):
    """Depth first (iteratively, so a hostile body cannot exhaust the stack), then size."""
    stack = [(doc, 1)]
    while stack:
        v, d = stack.pop()
        if isinstance(v, (dict, list)):
            if d > MAX_DEPTH:
                raise AdfError("ADF nesting deeper than %d levels; refused" % MAX_DEPTH)
            stack.extend((x, d + 1) for x in (v.values() if isinstance(v, dict) else v))
    try:
        size = len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass"))
    except (TypeError, ValueError):
        raise AdfError("ADF body is not JSON; refused")
    _cap(size)


# --- writer ----------------------------------------------------------------------------------------------------------------

def _text(s, marks=None):
    node = {"type": "text", "text": s}
    if marks:
        node["marks"] = marks
    return node


def _inline(s):
    """Inline Markdown subset -> ADF inline nodes. Empty text nodes are never emitted (schema minLength 1)."""
    out, pos = [], 0
    for m in _INLINE.finditer(s):
        if m.start() > pos:
            out.append(_text(s[pos:m.start()]))
        if m.group(1) is not None:
            out.append(_text(m.group(1), [{"type": "code"}]))
        elif m.group(2) is not None:
            out.append(_text(m.group(2), [{"type": "strong"}]))
        else:
            out.append(_text(m.group(3), [{"type": "link", "attrs": {"href": m.group(4)}}]))
        pos = m.end()
    if pos < len(s):
        out.append(_text(s[pos:]))
    return out


def _unesc(line):
    return line[1:] if line.startswith("\\") and _BLOCKISH.match(line[1:]) else line


def _para(lines):
    content = []
    for i, line in enumerate(lines):
        if i:
            content.append({"type": "hardBreak"})
        content.extend(_inline(_unesc(line)))
    return {"type": "paragraph", "content": content}


def md_to_adf(md):
    """Markdown subset -> ADF doc (version 1). A body over MAX_BODY (as Markdown or as ADF) raises AdfError."""
    if not isinstance(md, str):
        raise AdfError("md_to_adf takes a str")
    _cap(len(md.encode("utf-8", "surrogatepass")))
    if any(c in md for c in _BREAKS):
        raise AdfError("Markdown holds a line break other than \\n or \\r (U+2028 and the like); refused")
    blocks, para, lst = [], [], None
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0

    def flush():
        nonlocal para, lst
        if para:
            blocks.append(_para(para))
            para = []
        if lst is not None:
            blocks.append(lst)
            lst = None

    while i < len(lines):
        line = lines[i]
        f = _FENCE.fullmatch(line)
        if f:
            flush()
            body = []
            i += 1
            while i < len(lines) and lines[i].strip() != "```":
                body.append(lines[i])
                i += 1
            node = {"type": "codeBlock", "attrs": {"language": f.group(1) or "text"}, "content": []}
            if "\n".join(body):
                node["content"] = [_text("\n".join(body))]
            blocks.append(node)
            i += 1
            continue
        h, b, o = _HEAD.fullmatch(line), _BULLET.fullmatch(line), _ORDERED.fullmatch(line)
        if not line.strip():
            flush()
        elif h:
            flush()
            blocks.append({"type": "heading", "attrs": {"level": len(h.group(1))}, "content": _inline(h.group(2))})
        elif b or o:
            kind = "bulletList" if b else "orderedList"
            if para or (lst is not None and lst["type"] != kind):
                flush()
            if lst is None:
                lst = {"type": kind, "content": []}
                if o and int(o.group(1)) != 1:
                    lst["attrs"] = {"order": int(o.group(1))}
            item = (b or o).group(1 if b else 2)
            lst["content"].append({"type": "listItem", "content": [{"type": "paragraph", "content": _inline(_unesc(item))}]})
        elif lst is not None and line.startswith("  "):  # continuation line of the last list item
            lst["content"][-1]["content"][0]["content"] += [{"type": "hardBreak"}] + _inline(_unesc(line[2:]))
        else:
            if lst is not None:
                flush()
            para.append(line)
        i += 1
    flush()
    doc = {"version": 1, "type": "doc", "content": blocks}
    _check(doc)
    return doc


# --- reader ----------------------------------------------------------------------------------------------------------------

def _parts(n):
    if not isinstance(n, dict):
        raise AdfUnknown("not-a-node")
    t = n.get("type")
    if not isinstance(t, str):
        raise AdfUnknown("no-type")
    c, a = n.get("content", []), n.get("attrs", {})
    if not isinstance(c, list):
        raise AdfUnknown("content:" + t)
    if not isinstance(a, dict):
        raise AdfUnknown("attrs:" + t)
    return t, c, a


def _str_attr(a, key, t):
    v = a.get(key)
    if not isinstance(v, str) or _has_break(v):
        raise AdfUnknown("attrs:%s.%s" % (t, key))
    return v


def _has_break(s):
    return any(c in s for c in _BREAKS + "\n\r")


def _esc(line):
    return "\\" + line if _BLOCKISH.match(line) else line


def _misplaced(t):
    return AdfUnknown("misplaced:" + t if t in _KNOWN else t)


def _text_md(n):
    s = n.get("text")
    if not isinstance(s, str) or not s:
        raise AdfUnknown("text")
    if _has_break(s):
        raise AdfUnknown("text:newline")
    marks = n.get("marks", [])
    if not isinstance(marks, list):
        raise AdfUnknown("marks")
    got = {}
    for m in marks:
        if not isinstance(m, dict) or not isinstance(m.get("type"), str) or not isinstance(m.get("attrs", {}), dict):
            raise AdfUnknown("mark")
        if m["type"] not in _MARKS:
            raise AdfUnknown("mark:" + m["type"])
        if m["type"] in got:
            raise AdfUnknown("mark:duplicate")
        got[m["type"]] = m
    # a marked text the writer could not have produced would render ambiguously
    if "code" in got and "`" in s or "strong" in got and "*" in s or "link" in got and "]" in s:
        raise AdfUnknown("mark:text")
    out = s
    if "code" in got:
        out = "`" + out + "`"
    if "strong" in got:
        out = "**" + out + "**"
    if "link" in got:
        href = got["link"].get("attrs", {}).get("href")
        if not isinstance(href, str) or not _HREF.fullmatch(href):
            raise AdfUnknown("mark:link.href")
        out = "[" + out + "](" + href + ")"
    return out


def _inline_md(n):
    t, c, a = _parts(n)
    if t == "text":
        return _text_md(n)
    if t == "hardBreak":
        return "\n"
    if t == "mention":
        return "@<" + _str_attr(a, "id", t)[:8] + ">"  # never the display name (`attrs.text`)
    if t == "inlineCard":
        return _str_attr(a, "url", t)
    if t == "emoji":
        return _str_attr(a, "shortName", t)
    if t == "date":
        return "<date " + _str_attr(a, "timestamp", t) + ">"
    if t == "status":
        return "[" + _str_attr(a, "text", t) + "]"
    raise _misplaced(t)


def adf_to_md(doc, strict=False, unknown=None):
    """ADF doc -> Markdown. strict: raise AdfUnknown on anything outside the known set; otherwise render `[?<kind>]` and
    append the kind to `unknown` (a list) when given. A body too large or too deep raises AdfError in both modes."""
    _check(doc)
    seen = [] if unknown is None else unknown

    def each(nodes, render):
        out = []
        for n in nodes:
            try:
                out.append(render(n))
            except AdfUnknown as e:
                if strict:
                    raise
                seen.append(e.node_type)
                out.append("[?" + e.node_type + "]")
        return out

    def inl(c):
        return "".join(each(c, _inline_md))

    def item(li):
        t, c, _ = _parts(li)
        if t != "listItem":
            raise _misplaced(t)
        return "\n".join(each(c, block)).replace("\n", "\n  ")

    def block(n):
        t, c, a = _parts(n)
        if t == "paragraph":
            return "\n".join(_esc(x) for x in inl(c).split("\n"))
        if t == "heading":
            level = a.get("level")
            if type(level) is not int or not 1 <= level <= 6:
                raise AdfUnknown("attrs:heading.level")
            s = inl(c)
            if "\n" in s:
                raise AdfUnknown("heading:hardBreak")
            return "#" * level + " " + s
        if t in ("bulletList", "orderedList"):
            start = a.get("order", 1) if t == "orderedList" else 0
            if type(start) is not int or start < 0:
                raise AdfUnknown("attrs:orderedList.order")
            rows = each(c, item)
            return "\n".join(("- " if t == "bulletList" else "%d. " % (start + k)) + r for k, r in enumerate(rows))
        if t == "codeBlock":
            lang = a.get("language")
            lang = "text" if lang is None else lang
            if not isinstance(lang, str) or not _LANG.fullmatch(lang):
                raise AdfUnknown("attrs:codeBlock.language")
            body = []
            for x in c:
                xt, _, _ = _parts(x)
                s = x.get("text")
                if xt != "text" or x.get("marks") or not isinstance(s, str) or not s or _has_break(s.replace("\n", "")):
                    raise AdfUnknown("codeBlock:content")
                body.append(s)
            text = "".join(body)
            if any(ln.strip() == "```" for ln in text.split("\n")):
                raise AdfUnknown("codeBlock:fence")
            return "```" + ("" if lang == "text" else lang) + "\n" + text + ("\n" if text else "") + "```"
        if t == "blockquote":
            inner = "\n\n".join(s for s in each(c, block) if s != "")
            return "\n".join(">" + (" " + x if x else "") for x in inner.split("\n"))
        if t == "rule":
            return "---"
        raise _misplaced(t) if t in _INLINES or t in ("doc", "listItem") else AdfUnknown(t)

    try:
        t, c, _ = _parts(doc)
        version = doc.get("version")
        if t != "doc":
            raise AdfUnknown("not-a-doc")
        if type(version) is not int or version != 1:
            raise AdfUnknown("doc.version")
    except AdfUnknown as e:
        if strict:
            raise
        seen.append(e.node_type)
        return "[?" + e.node_type + "]"
    return "\n\n".join(s for s in each(c, block) if s != "")


_LINE_BREAKS = "\t\n\r\x0b\x0c\x1c\x1d\x1e\x85"  # tab and what str.splitlines breaks on: kept, every file writer splits lines itself


def safe_text(s, control=""):
    """Jira text as it may be written to a file (D-011): every control character (ESC, BEL, NUL, DEL, C1) except a tab or a line break,
    and every format character (bidi overrides and isolates, zero-width characters, BOM) removed, nothing else changed. The one cleaner
    of every Jira-text-to-file path: the ledger's section 1 (`dod.ac_section`, so the AC hash is over the cleaned text), the rules files
    (`rules.normalise`, which passes `control=" "`: a control character there becomes a blank, as the rules format always did) and the
    notes and answers `--sync` writes (`approvals._note`). Linear."""
    out = []
    for c in s:
        cat = unicodedata.category(c)
        out.append(c if c in _LINE_BREAKS or cat not in ("Cc", "Cf") else control if cat == "Cc" else "")
    return "".join(out)


def canonical_md(doc):
    """The form approvals and AC hashes are taken over: strict Markdown (raises AdfUnknown / AdfError)."""
    return adf_to_md(doc, strict=True)


def digest(doc):
    """sha256 hex of `canonical_md(doc)`."""
    return hashlib.sha256(canonical_md(doc).encode("utf-8", "surrogatepass")).hexdigest()
