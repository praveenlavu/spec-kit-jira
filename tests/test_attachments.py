"""P3: attachment upload (library only, used by P4 `done`; no CLI verb: K9). Against the fake Jira only (no network); data fictional.

Upload ran live once (LV2, 2026-11-12; download is U6): beyond that run, the multipart shape, the `X-Atlassian-Token` rule, the size limit and the `fields.attachment` shape are the
our reading of the API documentation, pinned here against tests/fakejira.py. Ported from the fork's attachments tests: the allow-list
(.md/.svg/.txt, case-insensitive extension), symlink / special-file / oversize refusals, bytes read once and exactly those uploaded."""
import email.parser
import email.policy
import os
import signal
import time

import pytest

from fakejira import FakeJira
from skj import attachments as at
from skj import client as cl

KEY = "SPK-1"
LEDGER = b"# DoD SPK-1 (fictional)\n- [x] done\n"


@pytest.fixture
def fake():
    f = FakeJira()
    assert f.add_issue("A fictional story") == KEY
    return f


@pytest.fixture
def root(tmp_path):
    (tmp_path / "dod").mkdir()
    (tmp_path / "dod" / "DoD_SPK-1.md").write_bytes(LEDGER)
    return tmp_path


def client(fake):
    return fake.client(sleep=lambda s: None)


def plan(fake, root, *paths):
    return at.plan_attachments(client(fake), str(root), KEY, list(paths) or ["dod/DoD_SPK-1.md"])


def apply(fake, p, **kw):
    return at.apply_attachments(client(fake), p, clock=lambda: 0.0, sleep=lambda s: None, **kw)


def uploads(fake):
    return [e for e in fake.log if e["method"] == "POST"]


def names(fake):
    return [(a["filename"], a["size"]) for a in fake.issue(KEY)["attachments"]]


def refused(fake, root, *paths, match=None):
    with pytest.raises(at.Refused, match=match) as ei:
        plan(fake, root, *paths)
    assert fake.log == [], "a refused path must be refused before any request"
    assert str(root) not in str(ei.value)  # no absolute path in any message
    return str(ei.value)


class Spy:
    """An opener around the fake that keeps the last upload's headers and body (the fake logs neither)."""

    def __init__(self, fake):
        self.fake, self.seen = fake, []

    def __call__(self, method, url, headers, body, timeout):
        if method == "POST":
            self.seen.append((dict(headers), body))
        return self.fake(method, url, headers, body, timeout)


def spy_client(fake):
    spy = Spy(fake)
    return cl.Client(fake.site, fake.email, fake.token, opener=spy, sleep=lambda s: None), spy


def parse(headers, body):
    return email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + headers["Content-Type"].encode("latin-1") + b"\r\n\r\n" + body)


# --- P3-AC3: the multipart request ------------------------------------------------------------------------------------------------------


def test_multipart_headers(fake, root):
    c, spy = spy_client(fake)
    p = at.plan_attachments(c, str(root), KEY, ["dod/DoD_SPK-1.md"])
    res = at.apply_attachments(c, p, clock=lambda: 0.0, sleep=lambda s: None)
    assert res["ok"] and res["lines"] == ["uploaded dod/DoD_SPK-1.md (%d bytes)" % len(LEDGER)]
    (headers, body), = spy.seen
    assert headers["X-Atlassian-Token"] == "no-check"
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert set(headers) == {"Authorization", "Accept", "Content-Type", "X-Atlassian-Token"}
    msg = parse(headers, body)
    parts = list(msg.iter_parts())
    assert len(parts) == 1
    part = parts[0]
    assert part.get_param("name", header="content-disposition") == "file"
    assert part.get_filename() == "DoD_SPK-1.md" and part.get_content_type() == "text/markdown"
    assert part.get_payload(decode=True) == LEDGER
    boundary = msg.get_boundary()
    assert boundary.encode() not in LEDGER
    assert body.startswith(b"--" + boundary.encode() + b"\r\n") and body.endswith(b"\r\n--" + boundary.encode() + b"--\r\n")
    assert names(fake) == [("DoD_SPK-1.md", len(LEDGER))]
    assert [e["path"] for e in fake.log] == ["/issue/SPK-1", "/issue/SPK-1/attachments"]
    assert fake.log[0]["query"] == {"fields": "attachment"}


@pytest.mark.parametrize("name, ctype", [("a.md", "text/markdown"), ("a.txt", "text/plain"), ("a.svg", "image/svg+xml"),
                                         ("a.MD", "text/markdown"), ("a.Svg", "image/svg+xml")])
def test_content_type_is_fixed_by_extension_never_sniffed(fake, root, name, ctype):
    data = b'<svg xmlns="http://www.w3.org/2000/svg"/>' if name.lower().endswith(".svg") else b"<svg>not sniffed</svg>"
    (root / "dod" / name).write_bytes(data)
    c, spy = spy_client(fake)
    at.apply_attachments(c, at.plan_attachments(c, str(root), KEY, ["dod/" + name]), clock=lambda: 0.0, sleep=lambda s: None)
    (headers, body), = spy.seen
    part, = parse(headers, body).iter_parts()
    assert part.get_content_type() == ctype and part.get_payload(decode=True) == data


def test_boundary_is_redrawn_when_it_occurs_in_the_payload():
    draws = iter(["0" * 32, "1" * 32])
    data = b"x --skj-" + b"0" * 32 + b" y"
    ctype, body = at.multipart("a.md", "text/markdown", data, token=lambda n: next(draws))
    assert ctype == "multipart/form-data; boundary=skj-" + "1" * 32
    assert body.count(b"skj-" + b"1" * 32) == 2
    part, = parse({"Content-Type": ctype}, body).iter_parts()
    assert part.get_payload(decode=True) == data


def test_boundary_never_found_refuses():
    with pytest.raises(at.Refused):
        at.multipart("a.md", "text/markdown", b"skj-" + b"0" * 32, token=lambda n: "0" * 32)


def test_random_boundaries_differ():
    a = at.multipart("a.md", "text/markdown", b"x")[0]
    b = at.multipart("a.md", "text/markdown", b"x")[0]
    assert a != b


# --- P3-AC2: idempotence ----------------------------------------------------------------------------------------------------------------


def test_same_file_skipped(fake, root):
    p = plan(fake, root)
    assert [i.action for i in p.items] == ["upload"]
    assert apply(fake, p)["ok"]
    p2 = plan(fake, root)
    assert [i.action for i in p2.items] == ["skip"]
    assert p2.lines == ("skip dod/DoD_SPK-1.md: already attached (same name and size)",)
    n = len(uploads(fake))
    res = apply(fake, p2)
    assert res == {"lines": ["skipped dod/DoD_SPK-1.md: already attached"], "ok": True}
    assert len(uploads(fake)) == n == 1 and names(fake) == [("DoD_SPK-1.md", len(LEDGER))]


def test_same_name_different_size_is_uploaded_and_reported(fake, root):
    assert apply(fake, plan(fake, root))["ok"]
    (root / "dod" / "DoD_SPK-1.md").write_bytes(LEDGER + b"- [x] more\n")
    p = plan(fake, root)
    assert [i.action for i in p.items] == ["upload"]
    assert "a different DoD_SPK-1.md is already attached; Jira keeps both" in p.lines[0]
    assert apply(fake, p)["ok"]
    assert names(fake) == [("DoD_SPK-1.md", len(LEDGER)), ("DoD_SPK-1.md", len(LEDGER) + 11)]


def test_same_size_other_name_is_uploaded(fake, root):
    fake.issue(KEY)["attachments"].append({"id": "1", "filename": "other.md", "size": len(LEDGER)})
    assert [i.action for i in plan(fake, root).items] == ["upload"]


@pytest.mark.parametrize("listing", [
    None, "x", {"filename": "a"}, [None], ["x"], [{"filename": 1, "size": 1}], [{"size": 1}], [{"filename": "a"}],
    [{"filename": "a", "size": True}], [{"filename": "a", "size": "12"}], [{"filename": "a", "size": 1.0}],
    [{"filename": "a", "size": -1}],
])
def test_malformed_attachment_listing_refuses(fake, root, listing):
    iid = fake.issue(KEY)["id"]
    fields = {} if listing is None else {"attachment": listing}
    fake.inject(200, body={"id": iid, "key": KEY, "fields": fields})
    with pytest.raises(at.Refused, match="attachments of SPK-1 could not be read safely"):
        plan(fake, root)
    assert uploads(fake) == []


def test_too_many_existing_attachments_refuses(fake, root):
    fake.issue(KEY)["attachments"] = [{"id": str(i), "filename": "x%d.md" % i, "size": 1} for i in range(at.MAX_LISTED + 1)]
    with pytest.raises(at.Refused, match="could not be read safely"):
        plan(fake, root)


def test_moved_issue_refuses(fake, root):
    fake.move_issue(KEY, "SPK-9")
    with pytest.raises(at.Refused):
        plan(fake, root)
    assert uploads(fake) == []


# --- P3-AC1: what may be uploaded -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["a.md", "a.txt", "b.svg", "DESIGN.MD", "Decisions_SPK-1.md", "x_1-2.TXT"])
def test_extension_allow_list(fake, root, name):
    data = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>' if name.endswith("svg") else b"text\n"
    (root / "dod" / name).write_bytes(data)
    p = plan(fake, root, "dod/" + name)
    assert [(i.name, i.data) for i in p.items] == [(name, data)]
    fake.log.clear()
    for bad in ["data.csv", "export.xlsx", "scan.pdf", "img.png", "noext", "a.md.exe", "a.exe", "a.html", "a.svgz", "a.mdx",
                "a.markdown", "a.md~", "a.md.", "a.MD.sh", "a.txt.md.exe"]:
        (root / "dod" / bad).write_bytes(b"x")
        refused(fake, root, "dod/" + bad, match="not uploadable")


def _outside(tmp_path):
    out = tmp_path / "outside" / "secret.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("TOPSECRET")
    return out


def test_path_outside_dod_refused(fake, root, tmp_path):
    secret = _outside(tmp_path)
    (root / "spec.md").write_text("fine but not in dod")
    (root / "dod" / "sub").mkdir()
    (root / "dod" / "sub" / "x.md").write_text("nested")
    (root / ".git").mkdir()
    (root / ".git" / "x.md").write_text("git")
    for rel in [str(secret), "/etc/hosts.md", "spec.md", "dod/../spec.md", "../outside/secret.md", "dod/../../outside/secret.md",
                "dod/sub/x.md", "dod/./DoD_SPK-1.md", "dod//DoD_SPK-1.md", ".git/x.md", "dod/.git/x.md", "./dod/DoD_SPK-1.md",
                "dod\\DoD_SPK-1.md", "DOD/DoD_SPK-1.md", "dod/", "dod", "", "dod/DoD_SPK-1.md/"]:
        refused(fake, root, rel)


def test_symlink_at_the_file_is_refused(fake, root, tmp_path):
    secret = _outside(tmp_path)
    inside = root / "dod" / "DoD_SPK-1.md"
    for target in (secret, inside):
        link = root / "dod" / ("link_%s.md" % target.parent.name)
        link.symlink_to(target)
        assert "symbolic link" in refused(fake, root, "dod/" + link.name)
    (root / "dod" / "dangling.md").symlink_to(root / "nowhere")
    refused(fake, root, "dod/dangling.md")


def test_symlinked_dod_is_refused(fake, tmp_path):
    secret = _outside(tmp_path)
    r = tmp_path / "repo"
    r.mkdir()
    (r / "dod").symlink_to(secret.parent)
    with pytest.raises(at.Refused, match="dod/ is not a directory inside the repository"):
        at.plan_attachments(client(fake), str(r), KEY, ["dod/secret.md"])
    assert fake.log == []


def test_symlinked_root_is_resolved_first(fake, root, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(root)
    p = at.plan_attachments(client(fake), str(alias), KEY, ["dod/DoD_SPK-1.md"])
    assert [i.data for i in p.items] == [LEDGER]


def test_parent_swapped_for_a_symlink_after_the_check_is_not_followed(fake, root, tmp_path, monkeypatch):
    """dod/ is opened once (O_NOFOLLOW|O_DIRECTORY) and the file is opened relative to that descriptor."""
    secret = _outside(tmp_path)
    (secret.parent / "DoD_SPK-1.md").write_text("TOPSECRET")
    real_open = os.open
    swapped = []

    def swapping_open(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        if str(path).endswith("dod") and not swapped:
            swapped.append(1)
            os.rename(str(root / "dod"), str(root / "dod.old"))
            os.symlink(str(secret.parent), str(root / "dod"))
        return fd
    monkeypatch.setattr(at.os, "supports_dir_fd", os.supports_dir_fd | {swapping_open})
    monkeypatch.setattr(at.os, "open", swapping_open)
    p = at.plan_attachments(client(fake), str(root), KEY, ["dod/DoD_SPK-1.md"])
    assert swapped and [i.data for i in p.items] == [LEDGER]


def test_file_swapped_for_a_symlink_after_the_check_is_not_followed(fake, root, tmp_path, monkeypatch):
    secret = _outside(tmp_path)
    f = root / "dod" / "DoD_SPK-1.md"
    real = at._check_name

    def swap(rel):
        out = real(rel)
        f.unlink()
        f.symlink_to(secret)
        return out
    monkeypatch.setattr(at, "_check_name", swap)
    refused(fake, root, "dod/DoD_SPK-1.md", match="symbolic link")


def test_fifo_directory_and_hardlink_are_refused(fake, root, tmp_path):
    os.mkfifo(str(root / "dod" / "pipe.md"))
    t0 = time.monotonic()
    assert "not a regular file" in refused(fake, root, "dod/pipe.md")
    assert time.monotonic() - t0 < 5  # never waits on the FIFO
    (root / "dod" / "notes.md").mkdir()
    assert "not a regular file" in refused(fake, root, "dod/notes.md")
    secret = _outside(tmp_path)
    try:
        os.link(str(secret), str(root / "dod" / "hard.md"))
    except OSError:
        pytest.skip("hardlinks unsupported here")
    assert "hard link" in refused(fake, root, "dod/hard.md")


def test_device_is_refused(fake, root):
    if not os.path.exists("/dev/null"):
        pytest.skip("no /dev/null")
    os.symlink("/dev/null", str(root / "dod" / "dev.md"))
    refused(fake, root, "dod/dev.md")


@pytest.mark.parametrize("name", [
    ".hidden.md", "-dash.md", "a b.md", 'a".md', "a'.md", "a\r.md", "a\n.md", "a\x00.md", "a\x1b.md", "a‮.md", "a‏.md",
    "é.md", "a;b.md", "a%2e.md", "a:b.md", "a" * 101 + ".md", "a..md", "a.b.md",
])
def test_unsafe_names_are_refused(fake, root, name):
    try:
        (root / "dod" / name).write_bytes(b"x")
    except (OSError, ValueError):
        pass  # the name cannot even exist here: still refused by the name check alone
    refused(fake, root, "dod/" + name, match="not uploadable")


def test_case_only_difference_is_refused(fake, root):
    refused(fake, root, "dod/DoD_SPK-1.md", "dod/dod_spk-1.md", match="twice")
    if os.path.exists(str(root / "dod" / "dod_spk-1.md")):  # a case-insensitive filesystem: the other spelling must not open it
        refused(fake, root, "dod/dod_spk-1.md", match="spelled differently")


def test_size_cap_boundary_and_empty_file(fake, root, monkeypatch):
    monkeypatch.setattr(at, "MAX_BYTES", 16)
    (root / "dod" / "ok.md").write_bytes(b"x" * 16)
    assert [i.size for i in plan(fake, root, "dod/ok.md").items] == [16]
    fake.log.clear()
    (root / "dod" / "big.md").write_bytes(b"x" * 17)
    assert "larger than 16 bytes" in refused(fake, root, "dod/big.md")
    (root / "dod" / "empty.md").write_bytes(b"")
    assert "empty" in refused(fake, root, "dod/empty.md")


def test_default_cap_is_10_mib():
    assert at.MAX_BYTES == 10 * 1024 * 1024


def test_non_utf8_and_nul_are_refused(fake, root):
    (root / "dod" / "bad.md").write_bytes(b"\xff\xfe x")
    assert "UTF-8" in refused(fake, root, "dod/bad.md")
    (root / "dod" / "nul.txt").write_bytes(b"a\x00b")
    refused(fake, root, "dod/nul.txt")


def test_file_count_cap(fake, root):
    paths = []
    for i in range(at.MAX_FILES + 1):
        (root / "dod" / ("f%d.md" % i)).write_text("x")
        paths.append("dod/f%d.md" % i)
    refused(fake, root, *paths, match="at most 10")
    with pytest.raises(at.Refused):
        at.plan_attachments(client(fake), str(root), KEY, [])
    with pytest.raises(at.Refused):
        at.plan_attachments(client(fake), str(root), KEY, "dod/DoD_SPK-1.md")  # a str is not a list of paths
    assert fake.log == []
    fake.log.clear()
    assert len(plan(fake, root, *paths[:10]).items) == 10


@pytest.mark.parametrize("key", ["spk-1", "SPK-0", " SPK-1", "SPK-1;x", "SPK-01"])
def test_invalid_key_refused_before_any_request(fake, root, key):
    with pytest.raises((at.Refused, ValueError)):
        at.plan_attachments(client(fake), str(root), key, ["dod/DoD_SPK-1.md"])
    assert fake.log == []


def test_one_bad_file_refuses_the_whole_plan(fake, root):
    (root / "dod" / "x.exe").write_bytes(b"x")
    refused(fake, root, "dod/DoD_SPK-1.md", "dod/x.exe")


# --- SVG: allowed type, hostile content refused (fail closed) --------------------------------------------------------------------------

SVG_OK = [
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect x="1" y="1" width="8" height="8" fill="#0a0"/></svg>',
    b'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg"><title>a &amp; b &lt;ok&gt;</title></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><defs><linearGradient id="g"/></defs>'
    b'<rect fill="url(#g)" width="1" height="1"/><use xlink:href="#g"/><use href="#g"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><text>the word onclick and script as text</text></svg>',
]

SVG_BAD = [
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><SCRIPT>alert(1)</SCRIPT></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><svg:script xmlns:svg="http://www.w3.org/2000/svg">x</svg:script></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><rect ONCLICK="x" width="1"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><rect\nonmouseover\n=\n"x"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)"><text>x</text></a></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><a href="JaVaScRiPt:alert(1)"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><a href="jav&#x09;ascript:alert(1)"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><a href="&#106;avascript:alert(1)"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><a href="java\nscript:alert(1)"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><div>x</div></foreignObject></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><FOREIGNOBJECT/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/x.png"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><image xlink:href="http://example.com/x"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/png;base64,AAAA"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><use href="other.svg#x"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><use href="//example.com/x.svg#x"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><use href=" https://example.com/x"/></svg>',
    b'<!DOCTYPE svg [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg"><text>&a;</text></svg>',
    b'<!doctype svg><svg xmlns="http://www.w3.org/2000/svg"/>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><text>&undefined;</text></svg>',
    b'<?xml-stylesheet href="https://example.com/x.css"?><svg xmlns="http://www.w3.org/2000/svg"/>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(https://example.com/x.css);</style></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(https://example.com/x)"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url( https://example.com/x)"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><iframe src="https://example.com"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="href" to="javascript:alert(1)"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><set attributeName="onclick" to="x"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><handler type="text/javascript">x</handler></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><![CDATA[<script>x</script>]]></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><rect></svg>',  # not well-formed
    b'not xml at all',
    b'<html><body>x</body></html>',  # not an svg root
    b'<svg xmlns="http://www.w3.org/2000/svg"><a href="vbscript:x"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="&#117;rl(https://example.com)"/></svg>',
]


@pytest.mark.parametrize("data", SVG_OK)
def test_plain_svg_is_allowed(fake, root, data):
    (root / "dod" / "fig.svg").write_bytes(data)
    assert [i.data for i in plan(fake, root, "dod/fig.svg").items] == [data]


@pytest.mark.parametrize("data", SVG_BAD)
def test_hostile_svg_is_refused(fake, root, data):
    (root / "dod" / "fig.svg").write_bytes(data)
    assert "SVG" in refused(fake, root, "dod/fig.svg")


def test_svg_check_is_not_applied_to_markdown(fake, root):
    (root / "dod" / "notes.md").write_bytes(b"Write <script> in docs; it is text here.\n")
    assert len(plan(fake, root, "dod/notes.md").items) == 1


# --- apply: X2 seam, uncertainty, 4xx, pacing -------------------------------------------------------------------------------------------


def test_apply_refuses_a_hand_built_plan(fake, root):
    real = plan(fake, root)
    fake.log.clear()
    with pytest.raises(at.Refused, match="plan_attachments"):
        at.apply_attachments(client(fake), object())
    forged = object.__new__(at.Plan)
    for k in at.Plan.__slots__:
        try:
            object.__setattr__(forged, k, getattr(real, k))
        except AttributeError:
            pass
    with pytest.raises(at.Refused, match="plan_attachments"):
        at.apply_attachments(client(fake), forged)
    import copy
    import pickle
    for dup in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            dup(real)
    assert fake.log == []


def test_a_real_plan_with_swapped_items_uploads_only_what_was_validated(fake, root, tmp_path):
    c, spy = spy_client(fake)
    p = at.plan_attachments(c, str(root), KEY, ["dod/DoD_SPK-1.md"])
    evil = at.Item("dod/evil.md", "evil.md", 6, "text/markdown", "upload", "", b"SECRET")
    object.__setattr__(p, "items", (evil,))
    object.__setattr__(p, "key", "SPK-2")
    assert at.apply_attachments(c, p, clock=lambda: 0.0, sleep=lambda s: None)["ok"]
    (headers, body), = spy.seen
    assert b"SECRET" not in body and names(fake) == [("DoD_SPK-1.md", len(LEDGER))]
    assert [e["path"] for e in fake.log if e["method"] == "POST"] == ["/issue/SPK-1/attachments"]


def test_without_posix_descriptors_it_refuses(fake, root, monkeypatch):
    monkeypatch.delattr(at.os, "O_NOFOLLOW")
    refused(fake, root, match="POSIX")


def test_plan_is_immutable(fake, root):
    p = plan(fake, root)
    with pytest.raises(AttributeError):
        p.items = ()
    with pytest.raises(AttributeError):
        p.items[0].data = b"other"
    assert isinstance(p.items, tuple)


def test_a_plan_is_applied_once(fake, root):
    p = plan(fake, root)
    assert apply(fake, p)["ok"]
    with pytest.raises(at.Refused, match="already applied"):
        apply(fake, p)
    assert len(uploads(fake)) == 1


def test_bytes_are_read_once_and_exactly_those_are_uploaded(fake, root):
    c, spy = spy_client(fake)
    p = at.plan_attachments(c, str(root), KEY, ["dod/DoD_SPK-1.md"])
    (root / "dod" / "DoD_SPK-1.md").write_bytes(b"swapped after the plan, same name")
    at.apply_attachments(c, p, clock=lambda: 0.0, sleep=lambda s: None)
    (headers, body), = spy.seen
    part, = parse(headers, body).iter_parts()
    assert part.get_payload(decode=True) == LEDGER


@pytest.mark.parametrize("applied, line", [
    (True, "uploaded dod/DoD_SPK-1.md (found on re-read)"),
    (False, "write uncertain: dod/DoD_SPK-1.md was not found on re-read; not uploaded again in this run"),
])
def test_write_uncertain_never_uploads_twice(fake, root, applied, line):
    (root / "dod" / "Decisions_SPK-1.md").write_text("decisions\n")
    p = plan(fake, root, "dod/DoD_SPK-1.md", "dod/Decisions_SPK-1.md")
    fake.fail_transport(on=len(fake.log) + 1, applied=applied)
    res = apply(fake, p)
    assert res["ok"] is applied
    assert res["lines"][0] == line
    ledger_posts = 1  # the uncertain file is never sent twice
    if applied:  # found on re-read: certain, the run goes on with the next file
        assert res["lines"][1:] == ["uploaded dod/Decisions_SPK-1.md (10 bytes)"] and len(uploads(fake)) == ledger_posts + 1
        assert names(fake) == [("DoD_SPK-1.md", len(LEDGER)), ("Decisions_SPK-1.md", 10)]
    else:  # still uncertain: the run stops, the second file waits for the next run
        assert res["lines"][1:] == ["dod/Decisions_SPK-1.md: not uploaded in this run (run again)"]
        assert len(uploads(fake)) == ledger_posts and names(fake) == []
    gets = [e for e in fake.log if e["method"] == "GET"]
    assert gets[-1]["query"] == {"fields": "attachment"}


def test_write_uncertain_and_the_re_read_fails(fake, root):
    p = plan(fake, root)
    n = len(fake.log)
    fake.fail_transport(on=n + 1)
    fake.fail_transport(on=n + 2)
    res = apply(fake, p)
    assert not res["ok"] and "the re-read failed" in res["lines"][0] and len(uploads(fake)) == 1


def test_5xx_is_uncertain(fake, root):
    p = plan(fake, root)
    fake.inject(502)
    res = apply(fake, p)
    assert not res["ok"] and "not found on re-read" in res["lines"][0] and len(uploads(fake)) == 1


def test_malformed_2xx_is_uncertain(fake, root):
    p = plan(fake, root)
    fake.inject(200, body={"not": "a list"})
    res = apply(fake, p)
    assert not res["ok"] and "write uncertain" in res["lines"][0] and len(uploads(fake)) == 1


@pytest.mark.parametrize("status, text", [
    (400, "Jira refused the upload (HTTP 400)"),
    (403, "no permission to attach files to SPK-1, or attachments are off (HTTP 403)"),
    (404, "SPK-1 is missing or not visible (HTTP 404)"),
    (413, "the file is larger than Jira accepts (HTTP 413)"),
    (415, "Jira refused the upload format (HTTP 415)"),
    (422, "Jira answered HTTP 422"),
])
def test_4xx_has_fixed_messages_and_stops(fake, root, status, text):
    (root / "dod" / "Decisions_SPK-1.md").write_text("decisions\n")
    p = plan(fake, root, "dod/DoD_SPK-1.md", "dod/Decisions_SPK-1.md")
    fake.inject(status, body={"errorMessages": ["server-text-SECRETISH"]})
    res = apply(fake, p)
    assert res["lines"][0] == "failed dod/DoD_SPK-1.md: %s; nothing uploaded for it" % text
    assert not res["ok"] and "SECRETISH" not in "\n".join(res["lines"])
    assert len(uploads(fake)) == 1


def test_uploads_are_paced(fake, root, monkeypatch):
    paths = []
    for i in range(at.MAX_FILES):
        (root / "dod" / ("f%d.md" % i)).write_text("x%d" % i)
        paths.append("dod/f%d.md" % i)
    p = plan(fake, root, *paths)
    from skj import push
    orig = push._Pacer
    monkeypatch.setattr(push, "_Pacer", lambda clock, sleep: orig(clock, sleep, n=3))  # W3's pacer, with a smaller burst
    sleeps = []
    res = at.apply_attachments(client(fake), p, clock=lambda: 0.0, sleep=sleeps.append)
    assert res["ok"] and len(uploads(fake)) == 10
    assert len(sleeps) == 7 and all(s > 0 for s in sleeps)


def test_messages_hold_no_absolute_path_or_secret(fake, root):
    p = plan(fake, root)
    res = apply(fake, p)
    text = "\n".join(p.lines + tuple(res["lines"])) + repr(p)
    assert str(root) not in text and fake.token not in text and fake.email not in text
    assert LEDGER.decode() not in repr(p) and LEDGER.decode() not in repr(p.items[0])


# --- linear time ------------------------------------------------------------------------------------------------------------------------

_BOUND = 10.0


class _TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise _TooSlow


def _timed(fn, *args):
    if not hasattr(signal, "setitimer"):
        pytest.skip("no setitimer")
    # CPU time, not the wall clock (H1b): load slows the clock, not the work; the timed calls run no subprocess and never block
    old = signal.signal(signal.SIGPROF, _alarm)
    signal.setitimer(signal.ITIMER_PROF, _BOUND)
    t0 = time.process_time()
    try:
        fn(*args)
    except _TooSlow:
        return _BOUND
    except at.Refused:
        pass
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, old)
    return time.process_time() - t0


def test_linear_on_a_10_mib_file_and_a_huge_listing(fake, root):
    (root / "dod" / "big.md").write_bytes(b"- [x] line --skj-\n" * (at.MAX_BYTES // 18))
    assert _timed(plan, fake, root, "dod/big.md") < 3
    # 150000 elements: 0.8 s CPU, 6x under the bound (300000 took 1.6 s)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg">' + b'<rect width="1" height="1"/>' * 150000 + b"</svg>"
    (root / "dod" / "big.svg").write_bytes(svg)
    assert len(svg) < at.MAX_BYTES
    assert _timed(plan, fake, root, "dod/big.svg") < 5
    hostile = b'<svg xmlns="http://www.w3.org/2000/svg">' + b"<a " + b"o" * (at.MAX_BYTES - 100) + b"/></svg>"
    (root / "dod" / "h.svg").write_bytes(hostile)
    assert _timed(plan, fake, root, "dod/h.svg") < 5
    fake.issue(KEY)["attachments"] = [{"id": str(i), "filename": "x.md", "size": i} for i in range(20000)]
    assert _timed(plan, fake, root) < 3
    assert _timed(at.multipart, "a.md", "text/markdown", b"x" * at.MAX_BYTES) < 3


def test_no_cli_verb_for_attachments():
    """K9: no single-item write verb; P4 `done` is the caller. Nothing to register in WRITE_VERBS."""
    from skj import __main__ as m
    assert "attach" not in m.WRITE_VERBS and "attachments" not in m.WRITE_VERBS
    parser = m.build_parser()
    sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    assert "attach" not in sub.choices and "attachments" not in sub.choices


# --- fix round 1: namespaces, CSS escapes, encoding declaration -------------------------------------------------------------------------

XH = 'xmlns:x="http://www.w3.org/1999/xhtml"'
SVG_BAD_R1 = [
    '<svg xmlns="http://www.w3.org/2000/svg"><x:meta %s http-equiv="refresh" content="0;url=http://e"/></svg>' % XH,
    '<svg xmlns="http://www.w3.org/2000/svg"><x:form %s action="http://e"><x:button>go</x:button></x:form></svg>' % XH,
    '<svg xmlns="http://www.w3.org/2000/svg"><x:video %s poster="http://e/p.png"/></svg>' % XH,
    '<svg xmlns="http://www.w3.org/1999/xhtml"><meta http-equiv="refresh" content="0;url=http://e"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><m:math xmlns:m="http://www.w3.org/1998/Math/MathML"><m:mi>x</m:mi></m:math></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><o:thing xmlns:o="urn:example:other"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><g><rect/><unknown xmlns=""/></g></svg>',
    '<svg><rect/></svg>',  # no namespace at all
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:o="urn:example:other"><rect o:href="http://e"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" xml:base="http://e/"><use href="#a"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:o="urn:example:other"><rect o:fill="red"/></svg>',  # foreign attribute alone
    '<svg xmlns="http://www.w3.org/2000/svg" %s><rect x:title="t"/></svg>' % XH,
    '<svg xmlns="http://www.w3.org/2000/svg"><style>rect{fill:\\75 rl(http://e/x)}</style></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>@\\69mport "http://e/x.css";</style></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:\\75rl(http://e/t)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:\\000075rl(http://e/t)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>rect{fill:u\\72l(http://e/x)}</style></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>@\\49 MPORT "http://e/x.css";</style></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>rect{fill:u/**/rl(http://e/x)}</style></svg>',
    '<?xml version="1.0" encoding="UTF-16"?><svg xmlns="http://www.w3.org/2000/svg"/>',
    "<?xml version='1.0' encoding='latin-1'?><svg xmlns=\"http://www.w3.org/2000/svg\"/>",
    '<?xml version="1.0" encoding="ISO-8859-1"?><svg xmlns="http://www.w3.org/2000/svg"/>',
]


@pytest.mark.parametrize("data", SVG_BAD_R1)
def test_hostile_svg_round_1_is_refused(fake, root, data):
    (root / "dod" / "fig.svg").write_bytes(data.encode("utf-8"))
    assert "SVG" in refused(fake, root, "dod/fig.svg")


@pytest.mark.parametrize("data", [
    '<svg:svg xmlns:svg="http://www.w3.org/2000/svg"><svg:rect width="1" height="1"/></svg:svg>',
    '<?xml version="1.0" encoding="UTF-8"?><svg xmlns="http://www.w3.org/2000/svg"/>',
    "<?xml version='1.0' encoding='utf-8'?><svg xmlns=\"http://www.w3.org/2000/svg\" xml:space=\"preserve\"/>",
])
def test_svg_namespace_variants_are_allowed(fake, root, data):
    (root / "dod" / "fig.svg").write_bytes(data.encode("utf-8"))
    assert len(plan(fake, root, "dod/fig.svg").items) == 1


# --- fix round 2: CSS is allow-listed on the joined text ----------------------------------------------------------------------------------

def _svg(inner, attrs=""):
    return '<svg xmlns="http://www.w3.org/2000/svg"%s>%s</svg>' % (attrs, inner)


CSS_BAD = [
    _svg('<style>rect{fill:u<!---->rl(http://e/x)}</style>'),
    _svg('<style>@im<!---->port "http://e/x.css";</style>'),
    _svg('<style>rect{fill:u<g/>rl(http://e/x)}</style>'),
    _svg('<style>@im<g/>port "http://e/x.css";</style>'),
    _svg('<style>rect{width:expr<!---->ession(alert(1))}</style>'),
    _svg('<style>rect{fill:u<!-- x -->rl(#g)}</style>'),  # any comment inside <style>, even a harmless-looking one
    _svg('<style>rect{fill:u<!---->rl(#g)}</style>'),  # joined text is valid CSS: only the child/comment rule refuses it
    _svg('<style>rect{fill:u<g/>rl(#g)}</style>'),
    _svg('<style>rect{background-image:image-set("http://e/x.png" 1x)}</style>'),
    _svg('<style>rect{background-image:IMAGE-SET(x 1x)}</style>'),
    _svg('<style>rect{cursor:url(http://e/c.cur),auto}</style>'),
    _svg('<style>rect{mask-image:url(https://e/m.svg)}</style>'),
    _svg('<style>rect{mask-image:URL( "https://e/m.svg" )}</style>'),
    _svg('<style>@font-face{font-family:x}</style>'),
    _svg('<style>@IMPORT x;</style>'),
    _svg('<style>rect{fill:\\75 rl(http://e/x)}</style>'),
    _svg('<rect style="fill:url(http://e/t)"/>'),
    _svg('<rect style="fill:URL(http://e/t)"/>'),
    _svg('<rect style="width:calc(1px + url(http://e))"/>'),
    _svg('<rect style="fill:url(#a) url(http://e)"/>'),
    _svg('<rect style="fill:url(#a&#41;"/>'),
    _svg('<rect style="fill:u\nrl(http://e/t)"/>'),
    _svg('<style>rect{fill:url\n(http://e/x)}</style>'),
    _svg('<style>rect{fill:ur\tl(http://e/x)}</style>'),
    _svg('<rect style="fill:attr(x)"/>'),
    _svg('<rect style="filter:drop-shadow(1px 1px red)"/>'),
    _svg('<rect style="font-family:\'http://e\'"/>'),
    _svg('<rect style="font-family:\'a"/>'),  # an unclosed quote
    _svg('<style>rect{content:"a/b"}</style>'),
    _svg('<style>rect{fill:red} &lt;x&gt;</style>'),
    _svg('<rect style="behavior:x"/>'),
    _svg('<rect style="-MOZ-BINDING:x"/>'),
    _svg('<rect style="fill:(red)"/>'),  # a bare parenthesis is no allowed function
]

CSS_OK = [
    _svg('<g id="node1"><title>a</title><ellipse style="fill:#000000;stroke:none;font-family:\'Times,serif\';font-size:14.00"/></g>'),
    _svg('<!-- Generated by graphviz, comment outside style --><g><!-- node --><rect style="fill:url(#grad1)"/></g>'),
    _svg('<rect style="transform:translate(10px,20px) rotate(5deg)"/>'),
    _svg('<style>rect{fill:url("#g");stroke:rgb(1,2,3)} .a{fill:hsla(1,2%,3%,0.5)}</style><rect class="a"/>'),
    _svg('<rect style="fill:url( \'#g\' );width:calc(1px + 2px);fill:var(--x)"/>'),
    _svg('<rect style="font-family:\'Times New Roman\'"/>'),
]


@pytest.mark.parametrize("data", CSS_BAD)
def test_hostile_css_is_refused(fake, root, data):
    (root / "dod" / "fig.svg").write_bytes(data.encode("utf-8"))
    assert "SVG" in refused(fake, root, "dod/fig.svg")


@pytest.mark.parametrize("data", CSS_OK)
def test_plain_css_is_allowed(fake, root, data):
    (root / "dod" / "fig.svg").write_bytes(data.encode("utf-8"))
    assert len(plan(fake, root, "dod/fig.svg").items) == 1


def test_css_allow_list_unit():
    assert at._css_problem("fill:url(#g)") is None
    for bad in ("fill:url(#g ", "fill:url(#g)x(1)", "fill:url(##)", "fill:url(#g\\)", "a:b(1)"):
        assert at._css_problem(bad)


@pytest.mark.parametrize("data", [
    '<rect xmlns="http://www.w3.org/2000/svg" width="1"/>',
    '<g xmlns="http://www.w3.org/2000/svg"><svg/></g>',
])
def test_root_must_be_svg(fake, root, data):
    (root / "dod" / "fig.svg").write_bytes(data.encode("utf-8"))
    assert "root element" in refused(fake, root, "dod/fig.svg")


# --- fix round 3: every attribute value is CSS-checked; linear CSS scan -------------------------------------------------------------------

ATTR_BAD = [
    _svg('<rect width="9" height="9" mask="image-set(\'http://e/x.png\' 1x)"/>'),
    _svg('<rect width="9" height="9" mask="-webkit-image-set(x 1x)"/>'),
    _svg('<rect width="9" height="9" cursor="image-set(\'http://e/c.png\' 1x), auto"/>'),
    _svg('<rect width="9" height="9"><set attributeName="mask" to="image-set(\'http://e/x.png\' 1x)"/></rect>'),
    _svg('<rect width="9" height="9"><animate attributeName="fill" values="red;cross-fade(a 50%, b)"/></rect>'),
    _svg('<rect width="9" height="9"><animate attributeName="fill" from="src(x)" to="red"/></rect>'),
    _svg('<rect width="9" height="9"><animate attributeName="fill" by="url(http://e)"/></rect>'),
    _svg('<rect width="9" height="9" fill="cross-fade(a 50%, b)"/>'),
    _svg('<rect width="9" height="9" fill="src(x)"/>'),
    _svg('<rect width="9" height="9" fill="url(http://e)"/>'),
    _svg('<rect width="9" height="9" fill="URL(http://e)"/>'),
    _svg('<rect width="9" height="9" MASK="IMAGE-SET(x 1x)"/>'),
    _svg('<rect width="9" height="9" fill="url(&quot;http://e&quot;)"/>'),
    _svg('<rect width="9" height="9" fill="u&#114;l(x)"/>'),
    _svg('<rect width="9" height="9" filter="drop-shadow(1px 1px red)"/>'),
    _svg('<rect width="9" height="9" data-x="url(http://e)"/>'),
]

PLAIN = _svg(
    '<defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/></linearGradient></defs>'
    '<path d="M10 10 L20 20 Z" fill="url(#g)" stroke="#000" stroke-width="1.5"/>'
    '<polygon points="1,2 3,4 5,6" opacity=".5"/>'
    '<g transform="translate(1,2) rotate(3)"><text x="1" y="2" font-family="Arial, Helvetica" style="fill:rgb(1,2,3)">ok</text></g>'
    '<use xlink:href="#g" href="#g"/>',
    ' xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10" width="10" height="10" version="1.1"'
    ' preserveAspectRatio="xMidYMid meet" xml:space="preserve" xml:lang="en"')


@pytest.mark.parametrize("data", ATTR_BAD)
def test_hostile_attribute_values_are_refused(fake, root, data):
    (root / "dod" / "fig.svg").write_bytes(data.encode("utf-8"))
    assert "SVG" in refused(fake, root, "dod/fig.svg")


def test_a_realistic_plain_svg_passes(fake, root):
    (root / "dod" / "fig.svg").write_bytes(PLAIN.encode("utf-8"))
    assert len(plan(fake, root, "dod/fig.svg").items) == 1


@pytest.mark.parametrize("css", [
    "a" * (1 << 20), "a" * (1 << 20) + "(", ("a" * 1990 + "\n") * 500, " " * (1 << 20) + "x(", "url" + " " * (1 << 20),
    ("url" + " " * 1000) * 1000, "'" * (1 << 20), '"' * (1 << 20) + "a", "a-" * (1 << 19), ("a " * 1000 + "\n") * 500,
    "url(" + "#" * (1 << 20), "url(" * (1 << 18),
])
def test_css_check_is_linear(css):
    assert _timed(at._css_problem, css) < 2


@pytest.mark.parametrize("make", [
    lambda n: _svg("<style>rect{fill:%s}</style>" % ("a" * n)),
    lambda n: _svg('<rect fill="%s"/>' % ("a" * n)),
    lambda n: _svg('<rect fill="%s"/>' % ("a-" * (n // 2))),
    lambda n: _svg('<rect fill="%s"/>' % ("url " * (n // 4))),
])
def test_svg_check_is_linear_on_long_values(make):
    assert _timed(at._svg_problem, make(1 << 20)) < 3
