"""The description fixtures the rules parser tests read (tests/fixtures/rules/*.json), in Atlassian Document Format.

PROVENANCE, stated plainly: these four descriptions were WRITTEN BY HAND for this port, in the node shapes the kit's own ADF reader
and writer use (paragraph, strong and em marks, bulletList, empty paragraphs between blocks). They were NOT captured from a real Jira
site: the kit has not been run against one. How the Jira editor really stores a product owner's typed rules, and what a paste from
Word really leaves behind (the Symbol-font `·` bullets with no-break spaces and the zero-width space kept here are an assumption
carried over from the fork's own synthetic Word fixture), is UNVERIFIED. The issues, the site and every name are fictional."""
import json
import pathlib

import pytest

pytestmark = pytest.mark.rules

FIX = pathlib.Path(__file__).parent / "fixtures" / "rules"
NAMES = ("epic_po_typed", "feature_po_typed", "feature_word_paste", "feature_intro_text")


def read(name):
    return json.loads((FIX / (name + ".json")).read_bytes().decode("utf-8"))


def texts(d):
    """Every text node's text, in order (a plain walk, independent of skj.rules)."""
    out, stack = [], [d]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            if n.get("type") == "text":
                out.append(n["text"])
            stack.extend(reversed(n.get("content", [])))
    return out


def marks(d):
    out, stack = set(), [d]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            out.update(m["type"] for m in n.get("marks", []))
            stack.extend(n.get("content", []))
    return out


@pytest.mark.parametrize("name", NAMES)
def test_fixtures_are_adf_documents_with_a_rules_section(name):
    raw = (FIX / (name + ".json")).read_bytes()
    assert len(raw) < 256 * 1024
    d = json.loads(raw.decode("utf-8"))
    assert d["type"] == "doc" and d["version"] == 1 and isinstance(d["content"], list)
    joined = "".join(texts(d))
    assert "Business rules" in joined and "Rule 1:" in joined
    for leak in ("/Users/", "C:\\Users", "@", "file:///", "atlassian.net", "accountId"):
        assert leak not in raw.decode("utf-8"), leak
    assert "<" not in joined  # no HTML: the description is ADF


def test_typed_fixtures_have_the_editor_shapes():
    epic, feat = read("epic_po_typed"), read("feature_po_typed")
    assert "Rule 1: Members must be 18 or older. " in texts(epic) and marks(epic) == {"strong"}
    assert feat["content"][3]["type"] == "bulletList"  # a rule typed as a bullet
    assert feat["content"][3]["content"][0]["content"][0]["content"][0] == {"type": "text", "text": "Rule 1:", "marks": [{"type": "strong"}]}
    assert ["Status ", ": Approved "] == [x for x in texts(feat) if x in ("Status ", ": Approved ")]  # `Status : Approved`
    assert "Rule:" in texts(feat) and " Points can't pay for delivery. " in texts(feat)  # a rule with no number; a plain apostrophe
    assert sum(x.startswith("Rule ") for x in texts(feat)) == 3 and {"type": "paragraph", "content": []} in feat["content"]


def test_word_fixture_holds_the_assumed_paste_artefacts():
    word = read("feature_word_paste")
    joined = "".join(texts(word))
    for kept in ("\u00a0", "\u200b", "\u00b7\u00a0\u00a0\u00a0\u00a0When", "Rule 3 -"):
        assert kept in joined, repr(kept)
    assert marks(word) == {"strong", "em"}
