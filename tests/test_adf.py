import copy
import json
import random
import re
from pathlib import Path

import pytest

from skj import adf
from skj.adf import MAX_BODY, MAX_DEPTH, AdfError, AdfUnknown, adf_to_md, canonical_md, digest, md_to_adf

FIX = Path(__file__).resolve().parent / "fixtures" / "adf"
WRITER_NODES = {"doc", "paragraph", "heading", "bulletList", "orderedList", "listItem", "codeBlock", "text", "hardBreak"}
WRITER_MARKS = {"strong", "code", "link"}

# The Markdown the live check posted for the recorded comment (its ADF request body: tests/fixtures/adf/comment-sent.json).
RECORDED_MD = (
    "[spec-kit-jira:questions] J0 fictional\n\n## Open questions\n\n- Q1: is the cap 10? _(open)_\n\n"
    "1. first\n2. second\n\n```\nAC-1 -> tests/test_total.py::test_total\n```\n\n"
    "Ref [PR](https://example.com/pr/1) and `code`."
)

# Every construct the ado ledger templates and dod.py render (headings with Unicode and inline code, `**Label:**` bullets,
# `- [ ]`/`- [x]` checkboxes, the indented continuation line, `_(fill in)_`, pipe-table rows, ordered lists, fences, links,
# the kit's marker and comment lines, the approval grammar, and dod's own `\#` / `\```` line escapes). Blocks are separated by
# one blank line: ADF keeps no blank-line information (test_block_separation_is_one_blank_line).
LEDGER_MD = "\n".join([
    "# DoD — SPK-12: Fictional story title",
    "",
    "- **Jira:** https://example-site.atlassian.net/browse/SPK-12",
    "- **Type / State:** Story / In Progress",
    "- **AC hash:** " + "a" * 64,
    "- **Spec dir:** _(set by speckit.jira-lifecycle.pull)_",
    "",
    "## 1. Seeded from Jira (verbatim — do not edit; refresh via `skj dod SPK-12 --refresh`)",
    "",
    "### Acceptance Criteria",
    "",
    "AC-1: totals are rounded",
    "\\# not a heading",
    "\\```",
    "",
    "## 2. Brainstorm & plan  (fill before building — per discipline)",
    "",
    "- **Approach / chosen option + why:** _(fill in)_",
    "- **Open questions → answers:** _(fill in as they arise)_",
    "  Format, one line each: `- [ ] Q1: <question> → _(open)_ · owner: PO`; answered: `- [x] Q1: <question> → <answer> · owner: PO`",
    "- [ ] Q1: is the cap 10? → _(open)_ · owner: PO",
    "- [x] Q2: use `parent`? → yes · owner: PO",
    "",
    "## 3. AC → tests (traceability)  (tag each AC as AC-1, AC-2…)",
    "",
    "| AC | acceptance criterion | tests | commit/PR | status |",
    "|---|---|---|---|---|",
    "| AC-1 | _(what it must do)_ | `tests/test_total.py::test_total` | [PR 1](https://example.com/pr/1) | red→green |",
    "",
    "1. first step",
    "2. second step",
    "",
    "```python",
    "print(1)",
    "```",
    "",
    "```",
    "AC-1 -> tests/test_total.py::test_total",
    "```",
    "",
    "## 5. Definition of Done  (all ✅ to close)",
    "",
    "- [ ] every AC mapped to a passing real test",
    "- [ ] gates green",
    "",
    "[skj:questions 0a1b2c3d]",
    "Open questions for the product owner:",
    "Q1: is the cap 10?",
    "",
    "/approve D3 " + "0" * 64 + ": looks right",
    "",
    "See **bold** and `code` and [docs](https://example.com/a).",
])


def _load(name):
    return json.loads((FIX / name).read_text())


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.get("content") or [])


def _doc(*blocks):
    return {"version": 1, "type": "doc", "content": list(blocks)}


def _p(*inl):
    return {"type": "paragraph", "content": list(inl)}


def _t(s, *marks):
    n = {"type": "text", "text": s}
    if marks:
        n["marks"] = list(marks)
    return n


# --- S5-AC1 ---------------------------------------------------------------------------------------------------------------

def test_roundtrip_ledger_constructs():
    doc = md_to_adf(LEDGER_MD)
    assert adf_to_md(doc, strict=True) == LEDGER_MD
    assert canonical_md(doc) == LEDGER_MD
    kinds = {n["type"] for n in _walk(doc)}
    assert {"heading", "bulletList", "orderedList", "codeBlock", "hardBreak"} <= kinds
    marks = {m["type"] for n in _walk(doc) for m in n.get("marks", [])}
    assert marks == WRITER_MARKS


@pytest.mark.parametrize("md", [
    "plain", "## h2 with `code`", "- a\n- b", "1. a\n2. b", "0. zero\n1. one", "3. three\n4. four",
    "- [ ] open\n- [x] done", "- a\n  continued", "a\nb\nc", "```\nx\n\ny\n```", "```sh\nls -l\n```",
    "**b** `c` [l](https://example.com/x?y=1)", "\\# escaped", "\\- escaped", "\\1. escaped", "\\---",
    "- a\n\n- b", "- a\n\n1. b",
])
def test_roundtrip_small_constructs(md):
    assert adf_to_md(md_to_adf(md), strict=True) == md


def test_block_separation_is_one_blank_line():
    md = "## 5. Definition of Done\n- [ ] gates green\n\n\n\nafter"
    assert adf_to_md(md_to_adf(md)) == "## 5. Definition of Done\n\n- [ ] gates green\n\nafter"


def test_reader_output_reparses_to_same_doc_fuzz():
    """For any input the writer accepts, the reader's Markdown parses back to the very same ADF (no ambiguity on the writer's image)."""
    rnd = random.Random(5)
    atoms = ["a", " ", "  ", "-", "*", "#", "##", ">", "\\", "`", "**", "[", "]", "(", ")", "https://example.com/p", "1.", "0.",
             "---", "```", "```py", "\n", "\n\n", "\t", "→", "_(open)_", "[ ] ", "/approve D1 "]
    for _ in range(3000):
        md = "".join(rnd.choice(atoms) for _ in range(rnd.randint(0, 25)))
        doc = md_to_adf(md)
        back = adf_to_md(doc, strict=True)
        assert md_to_adf(back) == doc, (md, back)
        assert adf_to_md(md_to_adf(back), strict=True) == back


# --- S5-AC2 ---------------------------------------------------------------------------------------------------------------

def test_matches_recorded_fixture():
    sent, stored = _load("comment-sent.json"), _load("comment-stored.json")
    assert sent == stored  # recorded: only key order differs
    assert md_to_adf(RECORDED_MD) == sent
    assert adf_to_md(stored, strict=True) == RECORDED_MD
    assert digest(sent) == digest(stored)


# --- S5-AC3 ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("node,kind", [
    ({"type": "panel", "content": [_p(_t("x"))]}, "panel"),
    ({"type": "table", "content": []}, "table"),
    ({"type": "mediaSingle", "content": []}, "mediaSingle"),
    ({"type": "expand", "content": []}, "expand"),
])
def test_strict_raises_on_unknown_node(node, kind):
    doc = _doc(_p(_t("/approve D1 " + "0" * 64 + ": ok")), node)
    with pytest.raises(AdfUnknown) as e:
        adf_to_md(doc, strict=True)
    assert e.value.node_type == kind
    with pytest.raises(AdfUnknown):
        canonical_md(doc)
    with pytest.raises(AdfUnknown):
        digest(doc)
    seen = []
    md = adf_to_md(doc, unknown=seen)  # non-strict: reported, never dropped silently
    assert seen == [kind]
    assert "[?" + kind + "]" in md


@pytest.mark.parametrize("inline,kind", [
    ({"type": "mediaInline", "attrs": {}}, "mediaInline"),
    ({"type": "placeholder", "attrs": {"text": "x"}}, "placeholder"),
    (_t("x", {"type": "em"}), "mark:em"),
    (_t("x", {"type": "textColor", "attrs": {"color": "#f00"}}), "mark:textColor"),
])
def test_strict_raises_on_unknown_inline_and_mark(inline, kind):
    doc = _doc(_p(_t("a "), inline))
    with pytest.raises(AdfUnknown) as e:
        adf_to_md(doc, strict=True)
    assert e.value.node_type == kind
    seen = []
    assert "[?" + kind + "]" in adf_to_md(doc, unknown=seen)
    assert seen == [kind]


MALFORMED = [
    "not a dict",
    None,
    [],
    {"type": "paragraph", "content": []},  # root is not a doc
    {"type": "doc", "version": 2, "content": []},
    {"type": "doc", "version": 1, "content": "x"},
    _doc("text"),
    _doc({"content": []}),  # missing type
    _doc({"type": 5}),
    _doc({"type": "paragraph", "content": {"a": 1}}),
    _doc(_p({"type": "text", "text": 5})),
    _doc(_p({"type": "text"})),
    _doc(_p({"type": "text", "text": ""})),
    _doc(_p({"type": "text", "text": "a\nb"})),
    _doc(_p({"type": "text", "text": "a\rb"})),
    _doc(_p({"type": "text", "text": "x", "marks": "strong"})),
    _doc(_p({"type": "text", "text": "x", "marks": ["strong"]})),
    _doc(_p({"type": "text", "text": "x", "marks": [{"type": "strong"}, {"type": "strong"}]})),
    _doc(_p({"type": "text", "text": "x", "marks": [{"type": "link"}]})),
    _doc(_p({"type": "text", "text": "x", "marks": [{"type": "link", "attrs": {"href": "javascript:alert(1)"}}]})),
    _doc(_p({"type": "text", "text": "x", "marks": [{"type": "link", "attrs": {"href": 7}}]})),
    _doc(_p({"type": "text", "text": "a`b", "marks": [{"type": "code"}]})),
    _doc(_p({"type": "text", "text": "a*b", "marks": [{"type": "strong"}]})),
    _doc(_p({"type": "text", "text": "a]b", "marks": [{"type": "link", "attrs": {"href": "https://example.com"}}]})),
    _doc({"type": "paragraph", "attrs": "x", "content": []}),
    _doc({"type": "heading", "attrs": {"level": 7}, "content": [_t("h")]}),
    _doc({"type": "heading", "attrs": {"level": "2"}, "content": [_t("h")]}),
    _doc({"type": "heading", "attrs": {"level": True}, "content": [_t("h")]}),
    _doc({"type": "heading", "content": [_t("h")]}),
    _doc({"type": "heading", "attrs": {"level": 1}, "content": [_t("a"), {"type": "hardBreak"}, _t("b")]}),
    _doc({"type": "orderedList", "attrs": {"order": -1}, "content": [{"type": "listItem", "content": [_p(_t("x"))]}]}),
    _doc({"type": "bulletList", "content": [_p(_t("x"))]}),  # child not a listItem
    _doc({"type": "listItem", "content": [_p(_t("x"))]}),  # listItem outside a list
    _doc({"type": "codeBlock", "attrs": {"language": "x\n```"}, "content": [_t("c")]}),
    _doc({"type": "codeBlock", "content": [_t("c", {"type": "strong"})]}),
    _doc({"type": "codeBlock", "content": [_t("a\n```\nb")]}),
    _doc({"type": "codeBlock", "content": [{"type": "hardBreak"}]}),
    _doc(_p({"type": "paragraph", "content": []})),  # block inside inline content
    _doc(_p({"type": "mention", "attrs": {"id": 5}})),
    _doc(_p({"type": "mention"})),
    _doc(_p({"type": "inlineCard", "attrs": {"url": "https://example.com/\nx"}})),
    _doc(_p({"type": "status", "attrs": {"text": ["x"]}})),
]


@pytest.mark.parametrize("doc", MALFORMED, ids=range(len(MALFORMED)))
def test_malformed_doc_raises_clearly_in_strict_and_is_reported_otherwise(doc):
    with pytest.raises(AdfUnknown):
        adf_to_md(doc, strict=True)
    seen = []
    md = adf_to_md(doc, unknown=seen)
    assert isinstance(md, str) and seen


def test_fuzz_random_json_never_crashes():
    rnd = random.Random(7)
    leaves = [None, True, 0, -1, 7, 1.5, "", "x", "doc", "paragraph", "text", "mention", "a\nb", [], {}]
    keys = ["type", "content", "text", "attrs", "marks", "version", "level", "order", "href", "id", "localId"]

    def junk(depth):
        r = rnd.random()
        if depth > 5 or r < 0.3:
            return rnd.choice(leaves)
        if r < 0.5:
            return [junk(depth + 1) for _ in range(rnd.randint(0, 3))]
        node = {k: junk(depth + 1) for k in rnd.sample(keys, rnd.randint(0, 5))}
        if rnd.random() < 0.7:
            node["type"] = rnd.choice(sorted(adf._KNOWN) + ["panel"])
        return node

    for _ in range(4000):
        doc = {"type": "doc", "version": 1, "content": [junk(0) for _ in range(rnd.randint(0, 3))]}
        try:
            adf_to_md(doc, strict=True)
        except AdfError:
            pass
        assert isinstance(adf_to_md(doc), str)


def test_depth_cap():
    node = _p(_t("deep"))
    for _ in range(MAX_DEPTH + 5):
        node = {"type": "blockquote", "content": [node]}
    for strict in (True, False):
        with pytest.raises(AdfError, match="nest"):
            adf_to_md(_doc(node), strict=strict)
    deep = []
    cur = deep
    for _ in range(100000):  # far past the interpreter's recursion limit
        nxt = []
        cur.append(nxt)
        cur = nxt
    with pytest.raises(AdfError, match="nest"):
        adf_to_md({"type": "doc", "version": 1, "content": deep})


# --- S5-AC4 ---------------------------------------------------------------------------------------------------------------

def _add_local_ids(doc):
    out = copy.deepcopy(doc)
    for i, n in enumerate(_walk(out)):
        n.setdefault("attrs", {})["localId"] = "fictional-local-%d" % i
        for m in n.get("marks", []):
            m.setdefault("attrs", {})["localId"] = "mark-local-%d" % i
    return out


def test_digest_ignores_local_id():
    doc = md_to_adf(LEDGER_MD)
    assert re.fullmatch(r"[0-9a-f]{64}", digest(doc))
    assert digest(_add_local_ids(doc)) == digest(doc)
    rich = _doc(_p(_t("hi "), {"type": "mention", "attrs": {"id": "5b10ac8d82e05b22cc7d4ef5", "text": "@Fictional"}},
                   {"type": "status", "attrs": {"text": "DONE", "color": "green"}}), {"type": "rule"})
    assert digest(_add_local_ids(rich)) == digest(rich)


def test_digest_ignores_mark_order():
    a = _doc(_p(_t("x", {"type": "strong"}, {"type": "code"}, {"type": "link", "attrs": {"href": "https://example.com"}})))
    b = _doc(_p(_t("x", {"type": "link", "attrs": {"href": "https://example.com"}}, {"type": "code"}, {"type": "strong"})))
    assert digest(a) == digest(b)
    assert adf_to_md(a) == "[**`x`**](https://example.com)"


def test_digest_ignores_key_order_and_text_splits():
    a = _doc(_p(_t("/approve D1 ok")))
    b = {"content": [{"content": [_t("/approve "), _t("D1 ok")], "type": "paragraph"}], "type": "doc", "version": 1}
    assert digest(a) == digest(b)


def test_digest_changes_on_real_change():
    base = md_to_adf(LEDGER_MD)
    d0 = digest(base)
    one_char = md_to_adf(LEDGER_MD.replace("looks right", "looks rigHt"))
    href = md_to_adf(LEDGER_MD.replace("https://example.com/a", "https://example.com/b"))
    mark = md_to_adf(LEDGER_MD.replace("**bold**", "bold"))
    level = md_to_adf(LEDGER_MD.replace("### Acceptance", "#### Acceptance"))
    lang = md_to_adf(LEDGER_MD.replace("```python", "```py"))
    for other in (one_char, href, mark, level, lang):
        assert digest(other) != d0
    quoted = _doc({"type": "blockquote", "content": [_p(_t("/approve D1 ok"))]})
    plain = _doc(_p(_t("/approve D1 ok")))
    literal = _doc(_p(_t("> /approve D1 ok")))  # text typed as `> ` is not the same as a quote either
    assert len({digest(quoted), digest(plain), digest(literal)}) == 3
    rule = _doc({"type": "rule"})
    dashes = _doc(_p(_t("---")))
    assert digest(rule) != digest(dashes)
    para2 = _doc(_p(_t("a")), _p(_t("b")))
    brk = _doc(_p(_t("a"), {"type": "hardBreak"}, _t("b")))
    assert digest(para2) != digest(brk)
    quote2 = _doc({"type": "blockquote", "content": [_p(_t("a")), _p(_t("b"))]})
    quote_brk = _doc({"type": "blockquote", "content": [brk["content"][0]]})
    assert digest(quote2) != digest(quote_brk)


# --- S5-AC5 ---------------------------------------------------------------------------------------------------------------

def test_never_emits_mention_or_empty_text():
    rnd = random.Random(11)
    atoms = ["@x", "@<5b10ac8d>", "[~accountid:5b10ac8d82e05b22cc7d4ef5]", "`", "``", "****", "**", "[]()", "[a]()", "",
             " ", "\n", "\r", "\r\n", "```", "```\n```", "- ", "1. ", "# ", "a", "\\"]
    samples = ["", "\n\n", "```\n```", "```\n\n```", "- ", "# ", "``", "****", "@someone hi"]
    samples += ["".join(rnd.choice(atoms) for _ in range(rnd.randint(0, 20))) for _ in range(3000)]
    for md in samples:
        doc = md_to_adf(md)
        for n in _walk(doc):
            assert n["type"] in WRITER_NODES, (md, n)
            if n["type"] == "text":
                assert isinstance(n["text"], str) and n["text"] != "", (md, n)
            for m in n.get("marks", []):
                assert m["type"] in WRITER_MARKS, (md, m)
        adf_to_md(doc, strict=True)  # the writer's output is always readable on a trust path


# --- reader extras -------------------------------------------------------------------------------------------------------

def test_quote_is_kept_for_the_reader():
    cmd = "/approve D1 " + "0" * 64 + ": ok"
    quoted = _doc({"type": "blockquote", "content": [_p(_t(cmd)), _p(_t("second"))]}, _p(_t("after")))
    assert adf_to_md(quoted, strict=True) == "> " + cmd + "\n>\n> second\n\nafter"
    literal = _doc(_p(_t("> " + cmd)))
    assert adf_to_md(literal, strict=True) == "\\> " + cmd
    nested = _doc({"type": "blockquote", "content": [{"type": "bulletList", "content": [
        {"type": "listItem", "content": [_p(_t(cmd))]}]}]})
    assert adf_to_md(nested, strict=True) == "> - " + cmd


def test_reader_renders_known_atoms_without_names():
    doc = _doc(_p(
        {"type": "mention", "attrs": {"id": "5b10ac8d82e05b22cc7d4ef5", "text": "@Fictional Person", "accessLevel": ""}},
        _t(" see "), {"type": "inlineCard", "attrs": {"url": "https://example.com/card"}},
        _t(" "), {"type": "emoji", "attrs": {"shortName": ":smile:", "id": "1f604", "text": "x"}},
        _t(" "), {"type": "date", "attrs": {"timestamp": "1700000000000"}},
        _t(" "), {"type": "status", "attrs": {"text": "DONE", "color": "green"}}), {"type": "rule"})
    md = adf_to_md(doc, strict=True)
    assert md == "@<5b10ac8d> see https://example.com/card :smile: <date 1700000000000> [DONE]\n\n---"
    assert "Fictional" not in md and "5b10ac8d82e05b22cc7d4ef5" not in md


# --- S5-AC6 ---------------------------------------------------------------------------------------------------------------

def test_oversize_refused():
    assert MAX_BODY == 1 << 20
    with pytest.raises(AdfError, match="1 MiB"):
        md_to_adf("x" * (MAX_BODY + 1))
    with pytest.raises(AdfError, match="1 MiB"):
        md_to_adf("é" * (MAX_BODY // 2 + 1))  # bytes, not characters
    with pytest.raises(AdfError, match="1 MiB"):
        md_to_adf("- a\n" * (MAX_BODY // 5))  # under the cap as Markdown, over it as ADF
    md_to_adf("x" * (MAX_BODY - 200))
    big = _doc(_p(_t("x" * MAX_BODY)))
    for fn in (lambda d: adf_to_md(d), lambda d: adf_to_md(d, strict=True), canonical_md, digest):
        with pytest.raises(AdfError, match="1 MiB"):
            fn(big)


def test_writer_rejects_non_string():
    for bad in (None, 5, b"bytes", ["x"]):
        with pytest.raises(AdfError):
            md_to_adf(bad)


# --- line breaks other than \n (fix round 1: str.splitlines also breaks on these) ------------------------------------------

BREAKS = ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]
CMD = "/approve D1 " + "0" * 64 + ": ok"


def _break_docs(ch):
    link = {"type": "link", "attrs": {"href": "https://example.com"}}
    yield _doc({"type": "blockquote", "content": [_p(_t("see" + ch + CMD))]})
    yield _doc(_p(_t("x" + ch + "## Acceptance Criteria")))
    yield _doc(_p(_t("a" + ch + "b", link)))
    yield _doc(_p(_t("a" + ch + "b", {"type": "code"})))
    yield _doc({"type": "heading", "attrs": {"level": 2}, "content": [_t("h" + ch + "x")]})
    yield _doc({"type": "codeBlock", "content": [_t("a" + ch + "```")]})
    for atom, key in (("mention", "id"), ("inlineCard", "url"), ("emoji", "shortName"), ("date", "timestamp"), ("status", "text")):
        yield _doc({"type": "blockquote", "content": [_p({"type": atom, "attrs": {key: "x" + ch + CMD}})]})
    yield _doc({"type": "blockquote", "content": [{"type": "x" + ch + CMD}]})  # an unknown type's name lands in the marker
    yield _doc(_p({"type": "y" + ch + CMD}))


@pytest.mark.parametrize("ch", BREAKS, ids=[hex(ord(c)) for c in BREAKS])
def test_other_line_breaks_raise_in_strict_and_are_neutralised_otherwise(ch):
    assert len(("a" + ch + "b").splitlines()) == 2  # the premise: splitlines breaks on it
    for doc in _break_docs(ch):
        for fn in (lambda d: adf_to_md(d, strict=True), canonical_md, digest):
            with pytest.raises(AdfUnknown):
                fn(doc)
        seen = []
        md = adf_to_md(doc, unknown=seen)
        assert seen and not any(c in md for c in BREAKS), (doc, md)
        assert md.splitlines() == md.split("\n")
        assert not any(c in k for k in seen for c in BREAKS)


@pytest.mark.parametrize("ch", BREAKS, ids=[hex(ord(c)) for c in BREAKS])
def test_writer_refuses_other_line_breaks(ch):
    for md in ("a" + ch + "b", "- a" + ch + "b", "```\na" + ch + "b\n```", "# h" + ch + "x"):
        with pytest.raises(AdfError, match="line break"):
            md_to_adf(md)


def test_canonical_has_only_newline_breaks_fuzz():
    rnd = random.Random(13)
    atoms = ["a", " ", "\n", "\r", "\r\n", "- ", "> ", "```", "**x**", "`c`"] + BREAKS
    for _ in range(3000):
        md = "".join(rnd.choice(atoms) for _ in range(rnd.randint(0, 15)))
        try:
            doc = md_to_adf(md)
        except AdfError:
            assert any(c in md for c in BREAKS)
            continue
        s = canonical_md(doc)
        assert not s or s.splitlines() == s.split("\n") or s.endswith("\n"), (md, s)
        assert not any(c in s for c in BREAKS)
