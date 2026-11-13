import argparse
import os
import re
import sys
from typing import List, Optional

from skj import __version__

# ---- X2: preview, human gate, apply (the fork's batch-verb `_plan`, hardened) ----
# Every verb that can write to Jira or a PR, as "<command path> [--flag]". Its parser gets --apply, --plan-out and --expect-plan only
# through `_add_write_flags`, which refuses an unregistered name; tests/test_x2_no_write_without_apply.py scans the argparse tree.
# H1a: `branch --link` dropped (its source was cut by K9; it never had a parser)
WRITE_VERBS = ("push", "done", "questions --post", "decisions --post", "pr create", "pr link")
# Verbs whose --apply writes local files or git only (never Jira or a PR): not X2 writes, so no plan files.
LOCAL_APPLY = {
    "branch": "git switch -c <name>: a local branch, nothing sent to Jira",
    "stack init": "writes the draft skj.stack.json",
    "stack approve": "writes the approval into skj.stack.json and skj.config.json (a human at a terminal)",
    "kits pin": "writes one kit entry of skj.kits.lock.json (a human at a terminal types the approval code)",
}
MAX_PLAN = 1 << 20  # bytes, for a plan and for an approved plan file
# Upstream's gate prints at most GateStep.MAX_SHOW_FILE_LINES = 200 lines of show_file, plus its own truncation line (fork value).
GATE_VISIBLE_LINES = 190


class PlanChanged(Exception):
    """`--apply --expect-plan F`: the fresh plan is not F. Exit 2; only <F>.new was written. Not a ValueError, so no verb swallows it.
    Exit 2 is also argparse's usage exit: a caller tells "plan changed" apart only by <F>.new existing (the workflow idiom
    `|| { [ $? -eq 2 ] && [ -e F.new ]; }`), so a stale <F>.new followed by a usage error looks like "plan changed". A mismatch whose
    <F>.new cannot be written exits 1 instead (fail closed: nothing sent)."""


def _plan_arg(v):
    """argparse type of --plan-out/--expect-plan/--summary-out: no NUL, not empty, never inside a .git directory (any letter case)."""
    if not v or "\x00" in v:
        raise argparse.ArgumentTypeError("the plan path is empty or holds a NUL")
    full = os.path.join(os.path.realpath(os.path.dirname(v) or "."), os.path.basename(v))
    if any(part.lower() == ".git" for part in full.split(os.sep)):
        raise argparse.ArgumentTypeError("the plan path is inside a .git directory")
    # DF3 m3: under the current directory no parent may be a symbolic link (a committed `.skj -> ~/.ssh`); outside it ($RUNNER_TEMP) is not checked.
    # DF4 N3: realpath of the path AS GIVEN (it follows `L` before `L/..`, as the kernel does), never of its abspath (which drops `L/..`
    # first); a path with a `..` component is checked wherever it lands, so `..` after a link is refused. DF5 D6: outside the checkout
    # the lexical reading is compared by its real path, so a link ABOVE the `..` (`/tmp/x/../plan.md`, /tmp -> /private/tmp) is fine.
    here, given = os.getcwd(), os.path.dirname(v) or "."
    rel = os.path.relpath(os.path.abspath(given), here)
    inside = rel != os.pardir and not rel.startswith(os.pardir + os.sep)
    real = os.path.realpath(given)
    if ((inside and real != os.path.normpath(os.path.join(os.path.realpath(here), rel)))
            or (os.pardir in given.split(os.sep) and real != os.path.realpath(os.path.abspath(given)))):
        raise argparse.ArgumentTypeError("the plan path goes through a symbolic link")
    return v


def _add_write_flags(p, verb):
    """The only way a parser gets the X2 flags; `verb` must be registered, else the CLI cannot even start."""
    if verb not in WRITE_VERBS:
        raise RuntimeError("%s writes but is not in WRITE_VERBS" % verb)
    p.add_argument("--apply", action="store_true", help="perform the writes (default: plan only, nothing written)")
    p.add_argument("--plan-out", metavar="PATH", type=_plan_arg, help="write the plan here (mode 0600)")
    p.add_argument("--expect-plan", metavar="PATH", type=_plan_arg,
                   help="with --apply: refuse (exit 2, writes PATH.new) unless the plan re-renders identical to this approved file")
    p.set_defaults(write_verb=verb)


def _plan_bytes(text):
    """The canonical plan: `\\n` line ends, one final newline, UTF-8, at most MAX_PLAN bytes; a plan the gate cannot show in full gets a
    warning as its FIRST line. Linear in the text; the verb renders the text from validated values only, deterministically."""
    if len(text) > MAX_PLAN:  # a character is at least one byte: refuse before any copy
        raise ValueError("the plan is larger than 1 MiB; nothing written")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    n = text.count("\n")
    if n > GATE_VISIBLE_LINES:
        text = ("⚠ Preview longer than the approval gate can show (%d lines): open the plan file and read it in full before approving.\n"
                % n) + text
    try:
        data = text.encode("utf-8")
    except UnicodeError:
        raise ValueError("the plan cannot be encoded as UTF-8; nothing written") from None
    if len(data) > MAX_PLAN:
        raise ValueError("the plan is larger than 1 MiB; nothing written")
    return data


def _read_plan_file(path):
    """The approved plan's bytes, None when it does not exist. A symlink, FIFO, directory or device, or more than MAX_PLAN bytes, is
    refused with a fixed text (opened non-blocking, without following a final symlink, and tested on the descriptor)."""
    import errno
    import stat
    kind = "the approved plan file is not a regular file (a symlink, directory, FIFO or device is refused); nothing written"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(kind if e.errno == errno.ELOOP else "the approved plan file cannot be read (%s); nothing written" % e.strerror) from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):  # on the descriptor, before fdopen (which refuses a directory with another text)
        os.close(fd)
        raise ValueError(kind)
    with os.fdopen(fd, "rb") as f:
        try:
            data = f.read(MAX_PLAN + 1)
        except OSError as e:
            raise ValueError("the approved plan file cannot be read (%s); nothing written" % e.strerror) from None
    if len(data) > MAX_PLAN:
        raise ValueError("the approved plan file is larger than 1 MiB; nothing written")
    return data


def _write_plan_file(path, data):
    """Write `data` to `path` atomically, mode 0600: a new temp file beside it (O_EXCL, O_NOFOLLOW), then a rename, which replaces a
    planted symlink at `path` and never writes through it. The parent is created as the fork does."""
    import secrets
    parent = os.path.dirname(path) or "."
    tmp = os.path.join(parent, ".%s.%s.tmp" % (os.path.basename(path), secrets.token_hex(8)))
    try:
        os.makedirs(parent, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ValueError("cannot write the plan file (%s); nothing sent" % e.strerror) from None


def plan_gate(args, text):
    """The X2 gate of a write verb, called after its reads and before any write: True only when the verb may write now (--apply, and with
    --expect-plan the fresh plan is byte for byte the approved file). On a mismatch (or a missing approved file) the fresh plan goes to
    <F>.new and PlanChanged is raised (exit 2): nothing else is written. --plan-out writes the plan and drops a stale <F>.new. The plan's
    sha256 is printed on stderr (never its text)."""
    import hashlib
    data = _plan_bytes(text)
    digest = hashlib.sha256(data).hexdigest()
    if args.expect_plan:
        if _read_plan_file(args.expect_plan) != data:
            _write_plan_file(args.expect_plan + ".new", data)
            raise PlanChanged("plan changed since approval: nothing written; re-run the preview and approve again (the current plan, "
                              "sha256 %s, is in the .new file beside the approved one)" % digest)
    if args.plan_out:
        _write_plan_file(args.plan_out, data)
        try:
            os.unlink(args.plan_out + ".new")
        except FileNotFoundError:
            pass
        except OSError as e:
            raise ValueError("cannot remove the stale <F>.new beside the plan (%s)" % e.strerror) from None
    print("skj: plan sha256 %s" % digest, file=sys.stderr)
    return bool(args.apply)


def default_client():
    """The one CLI way to an authenticated client: config, the target-branch trust (site pin), then the secret."""
    from skj import config, secretstore
    cfg = config.Config.load()
    trust = config.trusted()
    config.require_site_pin(cfg, trust)  # before the secret is read
    email, token = secretstore.get(cfg.site)
    return config.build_client(cfg, trust, email, token)


def cmd_comments(args, client_factory):
    from skj import comments, config
    key = _project_key(args.ref, config.Config.load().project)  # D-021: the configured project only, as every other ref verb
    if key is None:
        return 2
    for line in comments.cli_lines(comments.fetch(client_factory(), key), with_md=args.md):
        print(line)
    return 0


def _add_comments(sub):
    p = sub.add_parser("comments", help="list an issue's comments (ids, cut authors, flags; no bodies unless --md)")
    p.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    p.add_argument("--md", action="store_true", help="also print each comment body as Markdown (untrusted text)")
    p.set_defaults(func=cmd_comments)


def cmd_get(args, client_factory):
    """A safe summary of one issue of the configured project: no summary or description text, no people."""
    from skj import config, issues, keys
    cfg = config.Config.load()
    key = _project_key(args.ref, cfg.project)
    if key is None:
        return 2
    data = issues.get(client_factory(), key, ["status", "issuetype", "parent", "subtasks"])
    f = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    status = f.get("status") if isinstance(f.get("status"), dict) else {}
    cat = status.get("statusCategory") if isinstance(status.get("statusCategory"), dict) else {}
    itype = f.get("issuetype") if isinstance(f.get("issuetype"), dict) else {}
    parent = (f.get("parent") or {}).get("key") if isinstance(f.get("parent"), dict) else None
    subs = f.get("subtasks")
    for line in ("key: " + data["key"], "id: " + data["id"],
                 "status: %s (%s)" % (_shown(status.get("name")), _shown(cat.get("key"))),
                 "type: " + _shown(itype.get("name")),
                 "parent: " + (parent if keys.valid_key(parent) else "-" if parent is None else "?"),
                 "subtasks: " + (str(len(subs)) if isinstance(subs, list) else "?")):
        print(line)
    return 0


def _project_key(ref, project):
    """The key for `ref` (a number or a key) of the configured project, else None after printing why (K4: one project per repo)."""
    from skj import keys
    try:
        key = keys.parse_ref(ref, project)
    except ValueError:
        key = None
    if key is None or key.rsplit("-", 1)[0] != project:
        print("skj: not an issue number or a key of project %s" % project, file=sys.stderr)
        return None
    return key


def cmd_dod(args, client_factory):
    """Seed or refresh dod/DoD_<KEY>.md (a local file; the only Jira request is one GET of the story)."""
    from skj import config, dod
    cfg = config.Config.load()
    key = _project_key(args.ref, cfg.project)
    if key is None:
        return 2
    if args.action == "check":
        from skj import status
        why = status.after_step(cfg, key, str(dod.repo_root()), args.after)
        if why:
            print("skj: %s" % status.after_step_line(args.after, why), file=sys.stderr)
            return 1
        return 0
    refresh = args.action == "refresh"
    if args.spec_dir is not None:  # D-022: the shape every later step (push, pr create, pull.md step 5) accepts, checked where it enters
        from skj import keys, prs
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", args.spec_dir) or args.spec_dir.startswith("-") or prs._spec_rel(args.spec_dir, "") is None:
            raise keys.UsageError("--spec-dir must be a relative path inside the repository: letters, digits and . _ / - only, no `..`, "
                                  "not `.`, not dod/ or inside it")
    try:
        existed = os.path.lexists(str(dod.ledger_path(dod.repo_root(), key)))
        path = dod.seed(client_factory(), cfg, key, refresh=refresh, spec_dir=args.spec_dir or "")
    except dod.DodExists as e:
        print("skj: %s" % e, file=sys.stderr)
        return 2
    stats = dod.ledger_ac_stats(path.read_text(encoding="utf-8"))
    for line in ("path: %s" % path, "action: " + ("refreshed" if existed else "created"),
                 "ac_chars: %d" % stats["ac_chars"], "ac_thin: " + ("yes" if stats["ac_thin"] else "no")):
        print(line)
    return 0


def _add_dod(sub):
    p = sub.add_parser("dod", help="seed or refresh the DoD ledger dod/DoD_<KEY>.md from the story's acceptance criteria")
    acts = p.add_subparsers(dest="action", required=True)
    for name, text in (("seed", "write a new ledger (refused when one exists)"),
                       ("refresh", "re-pull the header and section 1; sections 2 to 5 are kept byte for byte (refused when there is no ledger or it is over 1 MiB)")):
        q = acts.add_parser(name, help=text)
        q.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
        q.add_argument("--spec-dir", help="Spec Kit feature dir recorded in the ledger (kept on refresh)")
    q = acts.add_parser("check", help="read-only, local: exit 1 when the product of a workflow agent step is missing or uncommitted "
                        "(the workflow runs it after each agent step)")
    q.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    q.add_argument("--after", required=True, choices=("pull", "plan", "tasks", "implement", "gate", "review"),
                   help="the agent step whose product to check")
    p.set_defaults(func=cmd_dod)


def _rules_confirm(key, root):
    """`skj rules <ref> --confirm` (RU3): records in the ledger header the rule hashes a human reviewed (local only: no identity, no
    request). New ids are added by anyone, also when another id changed or was dropped (written first, in one atomic write, then exit 2
    naming the two lists). An already-recorded id that would get another hash, or leave the record, is a human re-reading a changed rule:
    it needs a terminal on stdin AND stdout (a guard against accidents, not a security boundary: a pseudo-terminal gives a tty), prints the
    ids and old/new hashes, and writes only when the typed approval code matches; everything is read again and the code recomputed right
    before the write."""
    from skj import rules, stack
    prep = rules.prepare_confirm(root, key)
    if prep["needs_human"]:
        if not (sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty()):
            prep["write_free"]()  # the new ids are free: recorded in one atomic write before the refusal for the rest
            raise ValueError(rules.confirm_refusal(prep))
        print(rules.confirm_preview(key, prep))
        if not stack.confirm_code(prep["code"]):
            raise ValueError(stack.E_NOT_CONFIRMED)
        again = rules.prepare_confirm(root, key)
        if again["code"] != prep["code"]:
            raise ValueError(rules.CONFIRM_SWAPPED)
        prep = again
    written = prep["write"]()
    return {"key": key, "reviewed": prep["reviewed"], "added": prep["added"], "changed": prep["changed"], "removed": prep["removed"],
            "written": written}


def cmd_rules(args, client_factory):
    """`skj rules <ref>` (RU2): the business rules of the story's ancestors into dod/Rules_<KEY>.md (GETs only; local files). With
    `--confirm` (RU3): local only, no request; see `_rules_confirm`."""
    import json
    from skj import config, dod, rules
    cfg = config.Config.load()
    key = _project_key(args.ref, cfg.project)
    if key is None:
        return 2
    try:
        out = _rules_confirm(key, dod.repo_root()) if args.confirm else rules.pull(client_factory(), cfg, key, dod.repo_root())
    except ValueError as e:  # a refusal (a cycle, a file in the way, a failed write); a Jira error is a ClientError: exit 1
        print("skj: %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return 0


def _add_rules(sub):
    p = sub.add_parser("rules", help="pull the business rules of the story's parent chain (at most 3 levels up, rules.types only) into "
                       "dod/Rules_<KEY>.md (GETs only; local files; keys, counts and problem keys out, never rule text); with --confirm, record the "
                       "reviewed rule hashes in the ledger (local only)")
    p.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    p.add_argument("--confirm", action="store_true", help="local only: record in the ledger header the hashes of the rules in this story's "
                   "scope (`Rules reviewed:`). New ids need no terminal; re-confirming a changed hash or dropping a recorded id needs your own "
                   "terminal and a typed code")
    p.set_defaults(func=cmd_rules)


_SYNC_ALONE = "--sync cannot be combined with other options (only --if-comment-mode)"


def _sync_usage(args, others):
    """The usage error of `--sync` / `--if-comment-mode` (exit 2), else None. `others` are the values of every other option."""
    if args.if_comment_mode and not args.sync:
        return "--if-comment-mode needs --sync"
    if args.sync and any(v not in (None, False) for v in others):
        return _SYNC_ALONE
    return None


def _sync_trust(args, key):
    """`--sync` (A2): the TARGET branch's trust keys (`config.trusted()` with no argument: never the working tree, X4). Not comment mode:
    with --if-comment-mode a no-op (prints the note, returns None), else a refusal; comment mode without approvers refuses. Nothing here
    reads the story file or builds a client, so outside comment mode no request is ever made."""
    import json
    from skj import approvals, config
    trust = config.trusted()
    if getattr(trust, "approval", None) != "comment":
        if args.if_comment_mode:
            print(json.dumps({"key": key, "synced": False, "note": approvals.E_NOT_COMMENT_MODE}, indent=2))
            return None
        raise ValueError(approvals.E_SYNC_TERMINAL)
    if not trust.approvers:
        raise ValueError(approvals.E_SYNC_NO_APPROVERS)
    return trust


def _sync_reads(c, cfg, key):
    """(me, every comment, excluded): the GETs of a sync (`questions._story`: the issue and its project, the identity, the comments and
    the probe past the end; then `approvals.excluded`)."""
    from skj import approvals, questions
    me, rows = questions._story(c, cfg, key, "nothing written", questions.E_SYNC_TRUNC)
    return me, rows, approvals.excluded(c, rows, me=me)  # `/myself` once per run


def _sync_print(key, report):
    import json
    notice = ["%s: %d later malformed command line(s) by an approver: check the thread" % (i, n) for i, n in report["bad_newer"].items() if n]
    notice += ["%s is superseded by %s: this verdict is masked while %s stands (a rejected %s would bring it back)" % (i, m, m, m)
               for i, m in report.get("masked", {}).items()]
    notice += ["%s: answered already: a later /answer was not applied; correct the ledger by hand" % i for i in report.get("ignored", ())]
    print(json.dumps({"key": key, "synced": True, **report, **({"notice": notice} if notice else {})}, indent=2))
    if report.get("no_longer_valid"):  # the file keeps the old verdict, but its comment no longer backs it
        print("approval-no-longer-valid: %s" % ", ".join(report["no_longer_valid"]), file=sys.stderr)
    if report.get("tampered"):  # nothing applied there: an approver's newer comment was changed by someone or cannot be read
        print("approval-newer-tampered: %s (an approver's newer comment was edited, restricted or made unreadable; nothing applied: "
              "check the thread)" % ", ".join(report["tampered"]), file=sys.stderr)


def _sync_decisions(args, cfg, key, client_factory):
    """`decisions --sync` (A2, EXPERIMENTAL): approvers' `/approve D<n>` / `/reject D<n>` comments become verdicts in the local decisions
    file (no Jira write, no --apply: a local write). Every refusal (exit 1, fixed text, nothing written) comes before anything changes."""
    from skj import approvals, decisions, dod
    trust = _sync_trust(args, key)
    if trust is None:
        return 0
    path = decisions.decisions_file(dod.repo_root(), key)
    if args.if_comment_mode and not os.path.lexists(str(path)):
        import json
        print(json.dumps({"key": key, "synced": False, "note": approvals.E_NO_DECISIONS_FILE}, indent=2))
        return 0
    try:
        md = decisions._read(_existing_decisions(path, key))
    except (OSError, UnicodeError) as e:
        raise ValueError("cannot read the decisions file (%s)" % e.__class__.__name__)
    decisions.check_sync_file(md)  # a malformed file refuses before any request
    me, rows, excluded = _sync_reads(client_factory(), cfg, key)
    _sync_print(key, decisions.apply_sync(path, md, rows, trust.approvers, excluded, me))
    return 0


def _sync_questions(args, cfg, key, client_factory):
    """`questions --sync` (A2, EXPERIMENTAL): approvers' `/answer Q<n>: text` comments become the PO's answers in the local ledger."""
    from skj import dod, questions
    trust = _sync_trust(args, key)
    if trust is None:
        return 0
    root = dod.repo_root()
    path = dod.ledger_path(root, key)
    if not path.exists():
        raise ValueError("no DoD ledger at dod/%s; run `skj dod seed %s` first" % (path.name, key))
    md = questions.read_ledger(path)
    questions.check_ledger(md)  # a malformed ledger refuses before any request
    me, rows, excluded = _sync_reads(client_factory(), cfg, key)
    _sync_print(key, questions.apply_sync(root, key, md, rows, trust.approvers, excluded, me))
    return 0


def _decisions_usage(args):
    """The usage error of a bad flag combination (printed by the caller, exit 2), else None."""
    sync = _sync_usage(args, (args.post, args.add, args.kind, args.why, args.supersedes, args.resolve, args.verdict, args.by, args.note,
                              args.force, args.if_any, args.apply, args.plan_out, args.expect_plan))
    if sync:
        return sync
    if args.post and (args.add is not None or args.resolve):
        return "--post cannot be combined with --add or --resolve"
    if not args.post and (args.apply or args.plan_out or args.expect_plan or args.if_any):
        return "--apply, --plan-out, --expect-plan and --if-any need --post"
    if args.add is not None and (args.resolve or args.verdict or args.by is not None or args.note is not None or args.force):
        return "--add cannot be combined with --resolve options"
    if args.add is not None and args.kind is None:
        return "--add needs --kind decision|assumption"
    if args.add is None and (args.kind is not None or args.why is not None or args.supersedes is not None):
        return "--kind, --why and --supersedes need --add"
    if args.resolve and not args.verdict:
        return "--resolve needs --validated or --rejected"
    if not args.resolve and (args.verdict or args.by is not None or args.note is not None or args.force):
        return "--validated, --rejected, --by, --note and --force need --resolve"
    return None


def _resolve_decision(args, path):
    """`--resolve`: the PO's verdict, recorded by a human. `approvers` is a trust key: read from the TARGET branch only (X4), and an
    unreadable target raises (fail closed) before anything is read or written."""
    from skj import config, decisions
    if config.trusted().approvers:  # as default_client: cwd, so the non-root trust-key guard applies
        raise ValueError("--resolve is refused: approvers are configured on the target branch, so verdicts come from their `/approve D<n>` and "
                         "`/reject D<n>` comments: run `skj decisions <KEY> --sync` (it needs approval \"comment\" on the target branch; "
                         "with approval \"terminal\" and approvers set, a human records the verdict by editing the line in a reviewed commit)")
    if not (sys.stdin and sys.stdin.isatty()):
        raise ValueError("--resolve records the PO's verdict, which only a human may do: run it in your own terminal "
                         "(this shell has no terminal on stdin; a guard against accidents, not a security boundary)")
    path = _existing_decisions(path, args.key)
    by, note = args.by if args.by is not None else "PO", args.note or ""
    try:
        masked = decisions.masked_by(decisions.parse_decisions(decisions._read(path)), args.resolve)
    except (OSError, UnicodeError):
        masked = None  # apply_resolve reports an unreadable file
    decisions.apply_resolve(path, args.resolve, args.verdict, by, note, args.force)
    return {"key": args.key, "resolved": args.resolve, "status": args.verdict, "by": by.strip(), "note": note.strip(),
            **({"notice": "%s is superseded by %s: this verdict is masked while %s stands (a rejected %s would bring it back)"
                          % (args.resolve, masked, masked, masked)} if masked else {})}


def _existing_decisions(path, key):
    if not path.exists():
        raise ValueError("no decisions file at dod/%s; record one with `skj decisions %s --add \"<text>\" --kind decision`" % (path.name, key))
    return path


def _post_decisions(args, cfg, key, path, client_factory):
    """`--post` (W5): plan (reads only; no request when nothing is unposted), the X2 gate, then in comment mode the trusted approver's
    `/approve approve-decisions <hash of this plan>` (A4 `approvals.require`; it replaces W5's placeholder refusal), then one POST and the
    `posted:` stamps. `--if-any`: a missing decisions file is the same no-op (exit 0), as in the fork."""
    from skj import approvals, config, decisions, dod
    md = None
    if path.exists() or not args.if_any:
        try:
            md = decisions._read(_existing_decisions(path, key))
        except (OSError, UnicodeError) as e:
            raise ValueError("cannot read the decisions file (%s)" % e.__class__.__name__)
    parsed = decisions.parse_decisions(md or "")
    c = client_factory() if decisions.unposted(parsed) and not parsed["unparsed"] else None  # nothing to post: no request
    p = decisions.plan_post(c, cfg, key, parsed, missing=md is None)
    text = decisions.render_plan(p)
    print(text, end="")
    if not plan_gate(args, text):
        print("dry run: nothing posted; add --apply to post")
        return 0
    if p["action"] == "post":  # a plan that sends nothing needs no approval (the fork's `_approved`)
        approvals.require(c, config.trusted(), cfg, key, "decisions-post", _plan_bytes(text))  # TARGET branch keys; unreadable raises
    res = decisions.apply_post(c, p, dod.repo_root())
    for line in res["lines"]:
        print(line)
    return 0 if res["ok"] else 1


def cmd_decisions(args, client_factory):
    """List the decisions of a story, or record one (`--add`) or a verdict (`--resolve`): local files only, no Jira request. `--post`
    (W5) plans posting the unposted ones as one comment and, behind the X2 gate, posts it and stamps the lines."""
    import json
    from skj import config, decisions, dod
    usage = _decisions_usage(args)
    if usage:
        print("skj: " + usage, file=sys.stderr)
        return 2
    cfg = config.Config.load()
    key = _project_key(args.ref, cfg.project)
    if key is None:
        return 2
    if args.sync:
        return _sync_decisions(args, cfg, key, client_factory)
    args.key = key
    path = decisions.decisions_file(dod.repo_root(), key)
    if args.post:
        return _post_decisions(args, cfg, key, path, client_factory)
    if args.add is not None:
        out = {"key": key, "id": decisions.apply_add(path, key, args.kind, args.add, args.why or "", args.supersedes),
               "file": "dod/" + path.name}
    elif args.resolve:
        out = _resolve_decision(args, path)
    else:
        try:
            out = decisions.parse_decisions(decisions._read(_existing_decisions(path, key)))
        except (OSError, UnicodeError) as e:
            raise ValueError("cannot read the decisions file (%s)" % e.__class__.__name__)
    print(json.dumps(out, indent=2))
    return 0


def _add_decisions(sub):
    p = sub.add_parser("decisions", help="list the D<n> lines of dod/Decisions_<KEY>.md; --add records one, --resolve a verdict (local "
                       "files only); --post sends the unposted ones to the story as one new comment (plan only unless --apply)")
    p.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    p.add_argument("--add", metavar="TEXT", help="record one assumption or decision: appends the next D<n> line (the file is created when missing); needs --kind")
    p.add_argument("--kind", help="with --add: decision or assumption")
    p.add_argument("--why", help="with --add: the reason (at most 300 characters)")
    p.add_argument("--supersedes", metavar="D<n>", help="with --add: this decision answers the rejected (or posted open) D<n>")
    p.add_argument("--resolve", metavar="D<n>", help="a human records the PO's verdict on this line (needs --validated or --rejected; refused when "
                   "approvers are set on the target branch, and without a terminal)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--validated", dest="verdict", action="store_const", const="validated")
    g.add_argument("--rejected", dest="verdict", action="store_const", const="rejected")
    p.add_argument("--by", help="with --resolve: the role that decided (default PO; never a name)")
    p.add_argument("--note", help="with --resolve: a short note (at most 300 characters)")
    p.add_argument("--force", action="store_true", help="with --resolve: overwrite the verdict of a resolved line")
    p.add_argument("--post", action="store_true", help="plan posting the unposted decisions as one new comment, then stamp their lines "
                   "with posted: <comment id>:<hash>")
    p.add_argument("--if-any", action="store_true", help="with --post: a missing decisions file is a no-op (exit 0, nothing to post)")
    p.add_argument("--sync", action="store_true", help="EXPERIMENTAL: record the approvers' newest valid `/approve D<n>` and `/reject D<n>` "
                   "comments as verdicts in the decisions file, pinned `via: <comment id>:<hash8>` (a local write, GETs only; needs approval "
                   "\"comment\" and approvers on the target branch)")
    p.add_argument("--if-comment-mode", action="store_true", help="with --sync: a no-op (exit 0, no request) unless the target branch sets "
                   "approval \"comment\"; then also when the decisions file does not exist")
    _add_write_flags(p, "decisions --post")
    p.set_defaults(func=cmd_decisions)


def cmd_questions(args, client_factory):
    """List the open and answered questions of a story from its DoD ledger (a local read, no Jira request); with --post, plan posting
    the open ones as one comment (reads only) and, behind the X2 gate, post it and record its id in the ledger (W4)."""
    import json
    from skj import config, dod, questions
    usage = _sync_usage(args, (args.post, args.apply, args.plan_out, args.expect_plan))
    if usage:
        print("skj: " + usage, file=sys.stderr)
        return 2
    if not args.post and (args.apply or args.plan_out or args.expect_plan):
        print("skj: --apply, --plan-out and --expect-plan need --post", file=sys.stderr)
        return 2
    cfg = config.Config.load()
    key = _project_key(args.ref, cfg.project)
    if key is None:
        return 2
    if args.sync:
        return _sync_questions(args, cfg, key, client_factory)
    root = dod.repo_root()
    path = dod.ledger_path(root, key)
    if not path.exists():
        raise ValueError("no DoD ledger at dod/%s; run `skj dod seed %s` first" % (path.name, key))
    md = questions.read_ledger(path)
    parsed = questions.parse_questions(md)
    if not args.post:
        print(json.dumps(parsed, indent=2))
        return 0
    c = client_factory() if any(questions.postable(parsed)) and not parsed["unparsed"] else None  # nothing to post: no request
    p = questions.plan_post(c, cfg, key, parsed, md)
    text = questions.render_plan(p)
    print(text, end="")
    if not plan_gate(args, text):
        print("dry run: nothing posted; add --apply to post")
        return 0
    if p["action"] == "post":  # comment mode (A4): a trusted approver's `/approve` of this exact plan, else exit 2 and nothing sent
        from skj import approvals
        approvals.require(c, config.trusted(), cfg, key, "questions-post", _plan_bytes(text))
    res = questions.apply_post(c, p, root, md)
    for line in res["lines"]:
        print(line)
    return 0 if res["ok"] else 1


def _add_questions(sub):
    p = sub.add_parser("questions", help="list the open and answered Q<n> questions of the story's DoD ledger; --post sends the open ones "
                       "and the team's assumed answers to the story as one new comment (plan only unless --apply)")
    p.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    p.add_argument("--post", action="store_true", help="plan posting the open questions and assumed answers as one new comment, then record its id in the ledger")
    p.add_argument("--sync", action="store_true", help="EXPERIMENTAL: record the approvers' newest valid `/answer Q<n>: text` comments as the "
                   "PO's answers in the ledger (a local write, GETs only; needs approval \"comment\" and approvers on the target branch)")
    p.add_argument("--if-comment-mode", action="store_true", help="with --sync: a no-op (exit 0, no request) unless the target branch sets "
                   "approval \"comment\"")
    _add_write_flags(p, "questions --post")
    p.set_defaults(func=cmd_questions)


def cmd_branch(args, client_factory):
    """Name (and with --apply create) the local branch for a story: one GET of its summary, no Jira write."""
    from skj import branch, config, dod, issues
    cfg = config.Config.load()
    key = _project_key(args.ref, cfg.project)
    if key is None:
        return 2
    root = dod.repo_root()
    summary = (issues.get(client_factory(), key, ["summary"]).get("fields") or {}).get("summary")
    name = branch.branch_name(cfg.branch_prefix, key, summary)
    if branch.exists(root, name):
        raise ValueError("branch %s already exists; nothing changed" % name)
    print("branch: " + name)
    print("command: git switch -c " + name)
    if args.apply:
        branch.create(root, name)
        print("applied: switched to the new branch")
    else:
        print("dry run: nothing changed; add --apply to run the command")
    return 0


def _add_branch(sub):
    p = sub.add_parser("branch", help="name the local branch <prefix>KEY-<slug> for a story (dry run unless --apply)")
    p.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    p.add_argument("--apply", action="store_true", help="run `git switch -c <name>` (local only; nothing is sent to Jira)")
    p.set_defaults(func=cmd_branch)


def _judging_config(base=None, repo="."):
    """`_judge_at(base)`; DF4 N6: in CI with an explicit `--base` (the PR's target, e.g. `develop`), the stricter of that and the guarded
    home ref (`config.default_base`: origin/main, else origin/master), so a PR into a weaker branch is held to a strict main. Both
    strict: the `--base` one (as `_write_judge`). Outside CI a `--base` is read as given (read-only verbs, fork parity)."""
    from skj import config
    base = base if base is not None else (config.ci_head(repo) or None)  # DF5 D1: CI without --base: origin/HEAD's branch is the target
    cfg = _judge_at(base, repo)
    if base is not None and cfg.mode != "strict" and config.in_ci() and config.default_base(repo):
        home = _judge_at(None, repo)
        if home.mode == "strict":
            return home
    return cfg


def _stray_commit_id(repo, ref):
    """A bare 40-hex `ref` that no refs/remotes/origin/ branch tip equals (an old commit, not a target branch). DF5 D5: a tip under
    another remote namespace (`refs/remotes/zz/old`, made locally in one command) does not count."""
    from skj import config
    if not re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        return False
    tips = (config._out(repo, "for-each-ref", "--format=%(objectname)", "refs/remotes/origin/") or b"").decode().split()
    return ref.lower() not in tips


def _judge_at(base=None, repo="."):
    """The config that judges this run (gate, strict, `stack show`). The TARGET branch's skj.config.json is read first, at `base` or else
    the branch origin/HEAD points at (as `config.trusted()` finds it): when it says `mode: strict`, the whole run uses it and the PR's own
    file is never read (a PR cannot lower its own gate), and the target's skj.stack.json and skj.kits.lock.json are read and validated
    too (ConfigInvalid fails closed). A target with no config, or an advisory one, leaves the PR's config in charge. A base that is no
    commit here (a shallow clone, a revision expression, an option) cannot say what the target holds: an explicit `base` falls back to
    origin/HEAD's config when that resolves (D-016: a strict default branch stays in charge, and the strict check reports
    `base-unknown`); with no such fallback and an advisory PR config that is the fork's v0.2 path (outside CI only: in CI it fails closed whatever the PR's mode); with a strict one it fails closed; a base
    that IS a commit with an unreadable config fails closed."""
    from skj import config, strict
    ref = base if base is not None else config.default_base(repo)
    if config.in_ci() and not (ref and not config._BAD_BASE.search(ref) and config._resolve_base(repo, ref)):
        # as config.trusted()'s `refused-ci-no-base`: in CI a missing or shallow base must not let the PR's own file pick the mode
        raise config.ConfigInvalid(config.REF_LABEL + ": in CI the target branch must be readable to judge the PR, and the base "
                                   "cannot be resolved (or none is given and origin/HEAD is not set); pass --base origin/<target> "
                                   "and fetch it (fetch-depth: 0)")
    if base is not None or ref:
        try:
            if config._BAD_BASE.search(ref):  # never read: `main~1` or `main@{1}` could pick an older target config
                raise config.ConfigInvalid(config.REF_LABEL + ": the base is not a plain branch or ref name")
            target = config.Config.load_from_ref(ref, repo=repo)
            if (base is not None and not config.in_ci() and (target is None or target.mode != "strict")
                    and _stray_commit_id(repo, ref) and config.Config.load().mode != "strict"):  # else this run is strict anyway
                # DF4 N5: an old main's commit id would be judged by its own weaker config: refused beside a strict default branch
                home = config.default_base(repo)
                if home and _judge_at(home, repo).mode == "strict":
                    raise config.ConfigInvalid(config.REF_LABEL + ": the base is a commit id that is not the tip of a remote branch, "
                                               "and its config is weaker than the strict default branch: pass --base origin/<target>")
        except config.ConfigInvalid:
            if not config._BAD_BASE.search(ref) and config._resolve_base(repo, ref):
                raise  # the base is a commit and its config is the problem
            fallback = config.default_base(repo) if base is not None else ""
            if config._BAD_BASE.search(ref):  # a revision expression (`main~1`) is refused, never read, beside a strict default branch (DF3)
                if fallback and config._resolve_base(repo, fallback) and _judging_config(fallback, repo).mode == "strict":
                    raise
                fallback = ""
            if fallback and fallback != ref and config._resolve_base(repo, fallback):
                # D-016: a mistyped --base must not drop the default branch's strict mode; strict.check then reports base-unknown
                return _judging_config(fallback, repo)
            head = config.Config.load()
            if head.mode == "strict":
                raise
            return head
        if target is not None and target.mode == "strict":
            target.from_target = True
            strict.read_target_trust(target, repo)
            return target
    return config.Config.load()


def _strict_required(cfg, key, phase, root, **kw):
    """`skj pr create` / `skj done` in strict mode: the strict check of `phase` (`strict.check`), first, before any plan, push or request.
    Findings: the check as JSON on stdout, `skj: strict check failed: <rule> (<fix>); ...` on stderr, and None (the caller exits 2).
    Else the ids of the overrides it relied on (stamped `used` on --apply, just before anything is sent)."""
    import json
    from skj import strict
    res = strict.check(cfg, key, phase, repo=str(root), **kw)
    if res["ok"]:
        return res["overrides_used"]
    print(json.dumps(res, indent=2))
    print("skj: %s" % strict.Refused(res), file=sys.stderr)
    return None


def _stricter_trust(base, is_strict, root="."):
    """`(Trust, refs)` for `done` (the fork's `_stricter_trust` with ci=True): the stricter of the target branch `--base` and origin/HEAD, so a
    `--base` naming a weaker branch cannot turn a requirement off; `refs` are the refs read. `is_strict(trust)` picks the strict side(s); both
    strict with different approver sets refuses (ValueError: two lists cannot be ordered). No --base: `config.trusted` (origin/HEAD) as
    before. A --base that is no commit here raises ConfigInvalid (nothing is believed without it); no default base at all (`config.default_base` is "": no origin/HEAD
    and no guarded origin/main or origin/master) leaves --base alone."""
    from skj import config, strict
    home_ref = config.default_base(str(root))
    ref = strict.base_ref(str(root), base) if base else ""
    if not ref:
        return config.trusted(str(root)), [home_ref] * bool(home_ref)
    target = config.trusted(str(root), base=ref)
    home = config.trusted(str(root)) if home_ref else None
    strict_ones = [t for t in (home, target) if t is not None and is_strict(t)]
    refs = [r for r in (home_ref, ref) if r]
    if not strict_ones:
        return (home or target), refs
    if len(strict_ones) == 2 and set(home.approvers) != set(target.approvers):
        # DF6 R3: in CI without --base the target came from origin/HEAD, so "drop --base" is no fix: name both refs and the real one
        raise ValueError("the target branch (%s) and the home branch (%s) name different approvers, so it is unclear who may approve: "
                         "a human makes the two approvers lists the same, or closes the story in Jira"
                         % tuple(re.sub(r"^refs/(?:remotes|heads)/", "", r) for r in (ref, home_ref)))
    return strict_ones[0], refs


def _xcheck(cfg, base, root, key, client_factory):
    """(findings, blocking) of `done`'s cross-check (A3, `strict.xcheck_run`): none unless the stricter of `--base` and origin/HEAD names
    approvers (`--base` cannot weaken it). `approvers` and `approval` come from the target side(s) only, never the working tree; approval
    "comment" on either side that names approvers is the stricter mode (a hand-written verdict is then refused). It blocks with approval
    "comment" in any mode, and when this config or either side's config is strict (a PR that sets `mode: advisory` must not turn its own
    cross-check into a warning); only approval "terminal" with no strict side makes its findings warnings. The
    decisions file is read as confined as `done` reads it (an unreadable one is the `xcheck-malformed` finding); the client is built
    only when there is something to check."""
    from skj import config, decisions, gate, strict
    trust, refs = _stricter_trust(base, lambda t: bool(t.approvers), root)
    if not trust.approvers:
        return [], False
    sides = [trust] + [config.trusted(str(root), base=r) for r in refs]
    approval = "comment" if any(t.approvers and t.approval == "comment" for t in sides) else trust.approval
    # a project that opted into comment approvals never closes on an unverifiable approval, whatever its mode (A3 fix round 1)
    blocking = approval == "comment" or cfg.mode == "strict" or any((c is not None and c.mode == "strict")
                                                                    for c in (config.Config.load_from_ref(r, repo=str(root)) for r in refs))
    try:
        dec = decisions.decisions_file(root, key)
        md = gate._read(str(dec), str(root), set()) if os.path.lexists(str(dec)) else ""
    except ValueError:  # dod/ or the file is a link or the wrong kind of file
        md = None
    return strict.xcheck_run(client_factory, cfg, key, md, list(trust.approvers), approval), blocking


def _stamp_used(root, key, ids):
    """The apply paths, after the strict check passed and just before anything is sent: `used: <today>` on the overrides it relied on.
    A failed write is ValueError (exit 1) with nothing sent."""
    from skj import decisions
    if ids:
        decisions.apply_stamp(decisions.decisions_file(root, key), key, ids)


def _write_judge(base, repo="."):
    """The config that judges a WRITE verb run from a developer's machine (`done`, `pr create`): the stricter of `_judging_config(base)` and
    `_judging_config()` (in CI without --base: the `ci_head` target's judge, else origin/HEAD's), so a `--base` naming a weaker branch cannot turn strict mode off (as `_stricter_trust` for
    the approvers). Both strict: the --base one. No --base, or no default base here (`config.default_base` is ""): `_judging_config(base)`
    alone. An unresolvable --base fails closed as `_judging_config` does. The read-only verbs (`strict check|verify|mode`, `gate`, `stack
    show --check`) keep `_judging_config(--base)`: there --base IS the CI target ref the workflow passes (fork parity)."""
    from skj import config
    cfg = _judging_config(base, repo)
    if base is None or cfg.mode == "strict" or not config.default_base(repo):
        return cfg
    home = _judging_config(None, repo)
    return home if home.mode == "strict" else cfg


def _gate_base(cfg, args):
    """The ref the gate's commit checks walk from. In strict mode it is the full commit id the base resolves to (a tag named like the base
    must not move the range the strict floor judges); a base that cannot be resolved is a note locally and fails closed in CI."""
    from skj import config
    if cfg.mode != "strict":
        return args.base
    ref = args.base if args.base is not None else config.default_base(args.src)
    full = config._resolve_base(args.src, ref) if ref and not config._BAD_BASE.search(ref) else ""
    if not full and config.in_ci():
        raise config.ConfigInvalid("strict mode: the base branch cannot be resolved, so the commits of the PR cannot be judged; "
                                   "pass --base origin/<target branch> and fetch it (fetch-depth: 0)")
    return full or args.base


def cmd_gate(args, client_factory):
    """The DoD gate for a story: JSON on stdout, exit 1 when a check named in gate.block fails, 3 for an invalid config
    (CI fails closed); `--summary-out` gets the Markdown comment, also when the gate cannot run. gate.block, mode and gate.tests come
    from the target branch's config when it is strict (`_judging_config`)."""
    import json
    from pathlib import Path
    from skj import config, gate, strict
    out = err = None
    extra = ""
    try:
        args.base = args.base if args.base is not None else (config.ci_head(args.src) or None)  # DF5 D1
        cfg = _judging_config(args.base, args.src)
        key = _project_key(args.ref, cfg.project)
        if key is None:
            return 2
        out = gate.evaluate(cfg, key, src=args.src, changed=args.changed, base=_gate_base(cfg, args), offline=args.offline,
                            client_factory=client_factory, target=args.base)
        if cfg.from_target:  # additive: only when the target's config judged this run
            out["config"] = strict.TARGET_CONFIG_NOTE
            if strict.edits_trust_files(cfg, args.src):  # the PR edits the profile, lockfile or test command: say so in the comment
                out["trust_note"] = strict.TRUST_NOTE
        elif args.base is not None and cfg.mode == "strict":  # a strict PR config under --base: this PR may turn strict mode on
            out["config"] = strict.PR_CONFIG_NOTE
            if strict.adoption_note(cfg, args.src, args.base):
                out["trust_note"] = strict.TRUST_NOTE
        extra = "".join(f"{line}\n" for line in (("config: " + out["config"]) if out.get("config") else None, out.get("trust_note")) if line)
        print(json.dumps(out, indent=2))
    except (config.ConfigError, config.AuthRefused, ValueError) as e:
        err = e
    if args.summary_out:  # after the JSON, so a write failure cannot hide a blocking result
        text = gate.render_error(err) if err is not None else gate.render_summary(out)
        head = "### skj gate\n\n"
        text = head + extra + text[len(head):] if extra and text.startswith(head) else text
        try:  # D-015: the --plan-out writer: a new file renamed over the path, so a planted symlink is replaced, never followed
            _write_plan_file(args.summary_out, text.encode("utf-8"))
        except (OSError, ValueError):  # advisory output only: keep the gate's own exit code
            print("skj: warning: could not write the --summary-out file", file=sys.stderr)
    if isinstance(err, config.ConfigInvalid):
        print("skj: %s" % err, file=sys.stderr)
        return 3
    if err is not None:
        raise err
    return 1 if out.get("blocking") else 0


def _add_gate(sub):
    p = sub.add_parser("gate", help="DoD gate: AC to test traceability, assertions, AC drift, evidence, commits, and with rules.enabled the "
                       "business rules (GETs only, none with --offline)")
    p.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    p.add_argument("--src", default=".", help="the checkout to scan (its dod/ holds the ledger)")
    p.add_argument("--changed", nargs="*", default=[], help="changed files (PR diff)")
    p.add_argument("--base", metavar="REF",
                   help="ref the PR targets, e.g. origin/main: a strict skj.config.json there judges the run (the PR's own file is not read), "
                   "and commits in REF..HEAD must name the story (default: origin/HEAD)")
    p.add_argument("--offline", action="store_true", help="skip the Jira reads (AC drift and rule drift not checked)")
    p.add_argument("--summary-out", metavar="PATH", type=_plan_arg,
                   help="write a Markdown summary here (also when the gate cannot run); never inside .git; a final symlink is replaced, never followed, and under the current directory (or after a `..`) no parent may be a symlink")
    p.set_defaults(func=cmd_gate)


E_TASKS_FILE = ("cannot read the tasks file (missing, a symlink, outside the repository, not a regular file, over 1 MiB or not UTF-8); "
                "nothing read or written")


def _tasks_text(root, key, source):
    """tasks.md as text: `--from` (relative to the working directory), else `<ledger spec dir>/tasks.md`. Read as confined as the gate
    reads (gate._read: a regular file under the repository, no symlink, at most 1 MiB); the ledger's spec dir is untrusted and never echoed."""
    from skj import dod, gate
    if source is None:
        ledger = gate._read(str(dod.ledger_path(root, key)), str(root), set())
        if ledger is None:
            raise ValueError("no --from given and the DoD ledger of %s cannot be read; pass --from PATH or run `skj dod seed %s`"
                             % (key, key))
        spec_dir = dod.ledger_spec_dir(ledger)
        if not spec_dir:
            # not `dod refresh`: it re-pulls section 1 and resets the AC hash, which hides AC drift (pull.md step 3a, plan.md step 1)
            raise ValueError("no --from given and the DoD ledger of %s has no spec dir; pass --from PATH, or set the ledger's "
                             "`- **Spec dir:**` header line to the feature's spec dir (edit only that line)" % key)
        source = os.path.join(str(root), spec_dir, "tasks.md")
    text = gate._read(source, str(root), set())
    if text is None:
        raise ValueError(E_TASKS_FILE)
    return text


def cmd_push(args, client_factory):
    """Mirror tasks.md into subtasks of the story: the plan (reads only), the X2 gate, then the writes (W3)."""
    from skj import config, dod, push, tasks
    cfg = config.Config.load()
    key = _project_key(args.ref, cfg.project)
    if key is None:
        return 2
    task_list = tasks.parse_tasks(_tasks_text(dod.repo_root(), key, args.source))
    push.check_tasks(task_list)  # before the client: a refused tasks.md sends nothing
    c = client_factory()
    p = push.plan(c, cfg, key, task_list)
    text = push.render(p)
    print(text, end="")
    if not plan_gate(args, text):
        print("dry run: nothing written; add --apply to write")
        return 0
    if any(o["action"] in ("create", "transition") for o in p["ops"]):  # comment mode (A4): the approver's `/approve` of this plan
        from skj import approvals
        approvals.require(c, config.trusted(), cfg, key, "push", _plan_bytes(text))
    res = push.apply(c, p)
    for line in res["lines"]:
        print(line)
    refused = sum(o["action"] == "refused" for o in p["ops"])
    if refused:
        print("skj: %d transition(s) refused: see the plan" % refused, file=sys.stderr)
    deferred = sum(o["action"] == "create" and bool(o.get("deferred")) for o in p["ops"])
    if deferred:
        print("skj: %d done transition(s) deferred: run push again" % deferred, file=sys.stderr)
    return 1 if res["stopped"] else 0


def _add_push(sub):
    p = sub.add_parser("push", help="mirror the T### tasks of tasks.md into subtasks of the story, and fire the done transition of "
                       "ticked ones (plan only unless --apply)")
    p.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    p.add_argument("--from", dest="source", metavar="PATH", help="tasks.md (default: <ledger spec dir>/tasks.md)")
    _add_write_flags(p, "push")
    p.set_defaults(func=cmd_push)


def cmd_done(args, client_factory):
    """Close a story (P4): local checks (no request), reads, the plan, the X2 gate, then the attachments and only then the done
    transition. Not ready or a refused transition: exit 1, nothing written even with --apply. First the decisions cross-check when the
    stricter of --base and origin/HEAD names approvers (`_xcheck`, A3: GETs only; a finding, or Jira that cannot be read, is exit 2 when
    it blocks, else a warning), then in strict mode (L5b) the strict `done` check: a finding is exit 2 with nothing more read from Jira;
    the overrides it relied on are stamped on --apply."""
    import json
    from skj import config, dod, done, strict
    if args.base is not None and not args.base:
        print("skj: --base must not be empty", file=sys.stderr)  # an empty base would fall back to origin/HEAD
        return 2
    args.base = args.base if args.base is not None else (config.ci_head(".") or None)  # DF5 D1: and the trust read (_xcheck)
    cfg = _write_judge(args.base)  # the stricter of the --base judge and the origin/HEAD judge: --base cannot lower it
    key = _project_key(args.ref, cfg.project)
    if key is None:
        return 2
    root = dod.repo_root()
    used = []
    xf, blocking = _xcheck(cfg, args.base, root, key, client_factory)
    if xf and blocking:  # approvers are named and the cross-check found something (or could not read Jira): nothing is sent
        res = {"phase": "done", "mode": "strict", "ok": False, "findings": xf, "overrides_used": []}
        print(json.dumps(res, indent=2))
        print("skj: %s" % strict.Refused(res), file=sys.stderr)
        return 2
    for f in xf:  # advisory everywhere: a warning, as the fork's `warnings`
        print("skj: warning: %s: %s" % (f["rule"], f["detail"]), file=sys.stderr)
    if cfg.mode == "strict":
        used = _strict_required(cfg, key, "done", root)
        if used is None:
            return 2
    loc = done.local(root, key)
    c = client_factory() if loc["check"]["ready"] else None  # not ready: no request at all
    p = done.plan(c, cfg, root, key, loc)
    text = done.render(p)
    print(text, end="")
    refused = not p["ready"] or bool(p["refusal"])
    if refused and p["ready"]:
        print("skj: the done transition is refused (%s): set transitions.done in skj.config.json" % p["refusal"]["code"], file=sys.stderr)
    if not plan_gate(args, text):
        print("dry run: nothing written; add --apply to write" if not refused else "nothing written")
        return 1 if refused else 0
    if p["ready"] and not p.get("refusal"):
        _stamp_used(root, key, used)  # before anything is sent; a failed stamp sends nothing
    res = done.apply(c, p)  # a refused plan writes nothing there
    for line in res["lines"]:
        print(line)
    return 0 if res["ok"] else 1


def _add_done(sub):
    p = sub.add_parser("done", help="close a story whose ledger section 5 is fully ticked: attach the ledger (and the decisions file), "
                       "then fire the done transition (plan only unless --apply)")
    p.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    p.add_argument("--base", metavar="REF", help="the target branch whose strict config judges (default: origin/HEAD)")
    _add_write_flags(p, "done")
    p.set_defaults(func=cmd_done)


def _pr_tasks(root, key, source, allow):
    """(draft?, (field, value) plan row) for `pr create`: tasks.md ticked all through is a normal PR, an unticked task (or a tasks.md that
    cannot be read or lists nothing) is refused unless `--allow-incomplete`, which opens a draft. The reasons are fixed texts."""
    from skj import prs, push, tasks
    try:
        parsed = tasks.parse_tasks(_tasks_text(root, key, source))
        push.check_tasks(parsed)
        if not parsed:
            raise ValueError("tasks.md lists no tasks")
    except ValueError as e:
        why = str(e).replace("; nothing read or written", "")
        if not allow:
            raise ValueError("refusing to open a PR: %s. Fix it, or pass --allow-incomplete to open it as a draft" % why) from None
        return True, ("draft", "yes \u2014 tasks.md could not be checked (%s)" % why)
    open_ids, row = prs.tasks_row(parsed)
    if open_ids and not allow:
        raise ValueError("refusing to open a PR: %s. Finish them, or pass --allow-incomplete to open it as a draft"
                         % row[1].replace("yes \u2014 ", "", 1))
    return bool(open_ids), row


def cmd_pr_create(args, client_factory):
    """Open the pull request of a story through `gh` (P1): local files and git only (no Jira request), the plan, the X2 gate, then (with
    --push first) one `gh pr create`. Strict mode (L5b) first runs the strict `pr` check at the --head branch, which must be the checked-out
    commit (the working-tree ledger and decisions file are judged), against the base's full ref: a finding is exit 2 with nothing sent;
    the overrides it relied on are stamped on --apply, before the push and `gh`."""
    from skj import config, dod, gate, keys, prs, strict
    base = prs.check_branch("base", args.base) if args.base is not None else None
    cfg = _write_judge(base)  # the stricter of the --base judge and the origin/HEAD judge: --base cannot lower it
    key = _project_key(args.ref, cfg.project)
    if key is None:
        return 2
    root = dod.repo_root()
    if base is None:  # DF5 D1: in CI the branch origin/HEAD names (the PR's target), so the trust read below is the stricter-of too
        ci = config.ci_head(str(root))
        ref = "refs/remotes/" + ci if ci else config.default_base(str(root))
        base = prs.check_branch("base", ref[len("refs/remotes/origin/"):]) if ref.startswith("refs/remotes/origin/") else None
        if base is None:
            raise ValueError("no --base given and origin/HEAD is not set; pass --base <target branch>")
    head = args.head
    if head is None:
        ref = (config._out(str(root), "symbolic-ref", "--quiet", "HEAD") or b"").decode("utf-8", "replace").strip()
        head = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None
        if head is None:
            raise ValueError("HEAD is not on a branch; pass --head <branch>")
    prs.check_branch("head", head)
    used = []
    if cfg.mode == "strict":  # first: any finding refuses before a plan file, a push or a request
        if not strict.head_is_checkout(str(root), head):
            raise ValueError("strict mode judges the working-tree ledger and decisions file: check out the --head branch first")
        used = _strict_required(cfg, key, "pr", root, head=head, base=strict.base_ref(str(root), base), push=args.push)
        if used is None:
            return 2
    ledger = gate._read(str(dod.ledger_path(root, key)), str(root), set())
    if ledger is None:
        raise ValueError("the DoD ledger of %s cannot be read (missing, a symlink, over 1 MiB or not UTF-8); run `skj dod seed %s`" % (key, key))
    dpath = dod.ledger_path(root, key).with_name("Decisions_%s.md" % key)
    decisions_md = gate._read(str(dpath), str(root), set()) if os.path.lexists(str(dpath)) else None
    if os.path.lexists(str(dpath)) and decisions_md is None:
        raise ValueError("the decisions file cannot be read (a symlink, outside the repository, over 1 MiB or not UTF-8)")
    why = prs.review_refusal(prs.review_summary(ledger), cfg.effective_review_block_on())
    if why:
        raise ValueError("refusing to open a PR: " + why)
    draft, tasks_row = _pr_tasks(root, key, args.source, args.allow_incomplete)
    title = prs.make_title(key, args.title if args.title is not None else prs.ledger_title(ledger))
    body = prs.build_body(cfg, key, ledger, decisions_md)
    prs.check_body(title, key)
    head_sha, remote = prs.head_state(str(root), head, args.push, args.apply)
    text = prs.render_plan(key, title, body, base, head, draft, tasks_row, head_sha, remote, args.push)
    print(text, end="" if text.endswith("\n") else "\n")
    if not plan_gate(args, text):
        print("dry run: nothing created; add --apply to open the pull request")
        return 0
    trust = _stricter_trust(base, lambda t: t.approval == "comment", root)[0]  # the fork's `_pr_trust`: comment mode on either side wins
    if trust.approval != "terminal":  # comment mode (A4): the approver's `/approve approve-pr <hash>`; terminal: no Jira client at all
        from skj import approvals
        approvals.require(client_factory(), trust, cfg, key, "pr-create", _plan_bytes(text))
    _stamp_used(root, key, used)  # before anything is sent; a failed stamp sends nothing
    if args.push:
        prs.push_branch(str(root), head)
        print("pushed: origin/" + head)
    url = prs.gh_create(title, body, base, head, draft, cwd=str(root))
    print("created: " + url)
    return 0


def cmd_pr_link(args, client_factory):
    """Link on merge (P2): one remote link (globalId = the PR URL) and one kit comment, behind the X2 gate. Local checks first, then
    reads only, the plan, the gate, then the writes. A Jira error while reading is its status only (no server text)."""
    from skj import client, config, prlink
    cfg = config.Config.load()
    key = _project_key(args.ref, cfg.project)
    if key is None:
        return 2
    prlink.check_url(args.url)
    prlink.link_title(key, args.title)  # the local refusals come before any request
    c = client_factory()
    try:
        p = prlink.plan_link(c, cfg, key, args.url, args.title)
    except client.JiraError as e:
        raise ValueError("%s while reading; nothing written" % client.why(e)) from None
    text = prlink.render_plan(p)
    print(text, end="")
    if not plan_gate(args, text):
        print("dry run: nothing written; add --apply to link")
        return 0
    res = prlink.apply_link(c, p)
    for line in res["lines"]:
        print(line)
    return 0 if res["ok"] else 1


def _add_pr(sub):
    p = sub.add_parser("pr", help="the pull request of a story (through the local gh)")
    psub = p.add_subparsers(dest="pr_command", required=True)
    q = psub.add_parser("create", help="open the PR: <KEY> <title>, the body from the ledger, gate and review (plan only unless --apply)")
    q.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    q.add_argument("--title", help="the text after the key (default: the story summary in the ledger)")
    q.add_argument("--base", metavar="BRANCH", help="the target branch (default: the branch origin/HEAD points at)")
    q.add_argument("--head", metavar="BRANCH", help="the branch to open the PR from (default: the current branch)")
    q.add_argument("--from", dest="source", metavar="PATH", help="tasks.md (default: <ledger spec dir>/tasks.md)")
    q.add_argument("--allow-incomplete", action="store_true",
                   help="open a draft PR when tasks.md has unticked tasks or cannot be read (default: refuse)")
    q.add_argument("--push", action="store_true", help="with --apply: `git push -u origin <head>` first (default: no push)")
    _add_write_flags(q, "pr create")
    q.set_defaults(func=cmd_pr_create)
    q = psub.add_parser("link", help="link the merged PR to the story: one remote link and one comment (plan only unless --apply)")
    q.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    q.add_argument("--url", required=True, help="the pull request URL (https://github.com/<owner>/<repo>/pull/<n>, or the GH_HOST host)")
    q.add_argument("--title", help="the PR title (in CI: the PR event's title; default: the key alone)")
    _add_write_flags(q, "pr link")
    q.set_defaults(func=cmd_pr_link)


def _shown(v):
    """A server name as printable ASCII, at most 64 characters; `?` when it is not a string."""
    return re.sub(r"[^\x20-\x7e]", "?", v)[:64] if isinstance(v, str) and v else "?"


def _add_get(sub):
    p = sub.add_parser("get", help="summarise one issue: key, id, status, type, parent, subtask count")
    p.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    p.set_defaults(func=cmd_get)


def cmd_doctor(args, client_factory):
    from skj import doctor
    result = doctor.run(".", base=args.base, client_factory=client_factory)
    print(doctor.render(result))
    return 1 if doctor.failed(result) else 0


def _add_doctor(sub):
    p = sub.add_parser("doctor", help="what is wrong with this setup (read-only: one GET /myself, one GET /project, one GET /mypermissions)")
    p.add_argument("--base", help="the target branch the site pin is read from (default: origin/HEAD)")
    p.set_defaults(func=cmd_doctor, own_client=True)


def cmd_setup(args, client_factory):
    from skj import setup_cmd
    prompts = args.site is None or args.project is None or sys.platform != "darwin"  # off macOS it always asks for the identity
    if prompts and not (sys.stdin and sys.stdin.isatty()):
        print("skj: setup asks its questions only in a terminal (stdin is not one), so a pipe or an agent cannot answer them; "
              "run it in your own terminal, or on macOS pass both --site and --project; nothing written", file=sys.stderr)
        return 2
    return setup_cmd.run_setup(args.site, args.project)


def _add_setup(sub):
    p = sub.add_parser("setup", help="write skj.config.json; say how to store the Jira identity (never sends anything)")
    p.add_argument("--site", help="<name>.atlassian.net (asked when omitted)")
    p.add_argument("--project", help="project key, e.g. SPK (asked when omitted)")
    p.set_defaults(func=cmd_setup)


def _role(v):
    """argparse type of `--by`: a role, never a name (the one grammar is `stack._ROLE`)."""
    from skj import stack
    if not stack._ROLE.fullmatch(v):
        raise argparse.ArgumentTypeError(stack.E_ROLE)
    return v


def _story(v):
    """argparse type of `--story`: a full-match issue key (SPK-123); nothing is upper-cased or trimmed."""
    from skj import stack
    if not stack.valid_story(v):
        raise argparse.ArgumentTypeError(stack._E_STORY)
    return v


def _stack_show(args):
    """`skj stack show`: the config only supplies the mode and strict.test_command, from the target branch when its config is strict
    (`_judging_config`; none is advisory; an invalid one fails closed). Under a strict target the target's profile judges: `--check`
    also fails when it is not approved, and a working-tree profile that differs from it gets the trust note (review it as code)."""
    import json
    from skj import config, stack, strict
    try:
        cfg = _judging_config(args.base)
    except config.ConfigInvalid:
        raise
    except config.ConfigError:
        cfg = None
    result, approved, findings = stack.show(".", cfg.mode if cfg else "advisory", cfg.strict["test_command"] if cfg else None)
    if cfg is not None and cfg.from_target:
        target = stack.profile_for(cfg, ".")
        if target["status"] != "approved":
            result["notes"].append("stack-profile: skj.stack.json at the target branch is %s: it judges strict CI" % target["status"])
            findings += 1
        try:
            head = stack._read_small(stack.PROFILE_FILE, stack.MAX_PROFILE)
        except OSError:
            head = None
        if head != cfg.target_profile_blob:
            result["trust_note"] = strict.TRUST_NOTE
    print(stack.show_md(result, approved) if args.md else json.dumps(result, indent=2))
    if args.check and findings:
        print("skj: stack check failed: %d finding(s)" % findings, file=sys.stderr)
        return 2
    return 0


def _stack_init(args):
    """`skj stack init`: a draft `skj.stack.json` from a seed or a file under `.skj/`. A local write: no Jira call, no gate, and the draft is
    not trusted until approved. Output is counts and fixed error texts, never profile text."""
    import json
    from skj import stack
    if args.root is not None and (args.seed is None or len(args.seed) > 1):
        args.parser.error("--root needs exactly one --seed")
    if args.force and not args.apply:
        args.parser.error("--force needs --apply")
    plan = stack.plan_init(args.seed if args.seed is not None else stack.read_source(".", args.from_file), ".", args.stack, args.root)
    out = {"ok": plan["ok"], "errors": plan["errors"], "counts": plan["counts"]}
    if not plan["ok"]:
        print(json.dumps(out, indent=2))
        print("skj: the profile is invalid (%d error(s)); nothing written" % len(plan["errors"]), file=sys.stderr)
        return 2
    for warning in stack.init_warnings(plan, args.seed):
        print("warning: %s" % warning, file=sys.stderr)
    if args.apply:
        out.update(stack.apply_init(".", plan, args.force))
        if args.from_file is not None and os.path.realpath(args.from_file) == os.path.realpath(stack.DRAFT_FILE):
            try:
                os.unlink(stack.DRAFT_FILE)
            except OSError:
                print("warning: could not remove %s" % stack.DRAFT_FILE, file=sys.stderr)
    print(json.dumps(out, indent=2))
    return 0


def _timeout_info(cfg, command):
    """The `strict.test_timeout` sentence of the approve summary (a flat string, so the summary stays one JSON object); None when no command is written."""
    from skj import stack
    return None if command is None else stack.timeout_text(cfg.strict["test_timeout"] if cfg else None)


def _stack_approve(args):
    """`skj stack approve`: the human's approval. `--apply` needs a terminal on stdin AND stdout, tested before any file or the config is
    read (a guard against accidents, not a security boundary: a pseudo-terminal gives a tty). It prints the full preview, then `approval
    code: <12 hex>`, and approves only when the human types the code; the profile is read again and the code recomputed right before the
    write. A terminal preview prints the profile's commands and rules in full (printed, never stored) when stdin and stdout are both
    terminals; anything else prints counts and lengths only, so unapproved research text never reaches an agent's shell.
    No trust key is read here: the approval is the human at this terminal, as in the fork; `approvers` (target branch only) are the PO's."""
    import json
    from skj import config, stack
    shown = bool(sys.stdin and sys.stdin.isatty()) and bool(sys.stdout and sys.stdout.isatty())  # the full text only to a terminal: not into a pipe or a file
    if args.apply and not shown:
        raise ValueError("approving a stack profile changes what strict CI runs, which only a human may do: run it in your own terminal "
                         "(stdin and stdout must both be a terminal, so the full preview is seen; a guard against accidents, not a security boundary)")
    if args.apply:
        if os.environ.get("SKJ_CONFIG"):
            raise ValueError(stack._E_ENV)
        cfg = config.Config.load()
        prep = stack.prepare_approve(".", config.CONFIG_NAME, args.by, args.story)
        plan = prep["plan"]
        print(stack.preview_text(plan["profile"], plan["test_command"], prep["config_command"], plan["version"], args.story, args.by, plan["status"],
                                 timeout=cfg.strict["test_timeout"]))
        if not stack.confirm_code(prep["code"]):
            raise ValueError(stack.E_NOT_CONFIRMED)
        res = stack.apply_approve(".", config.CONFIG_NAME, args.by, args.story, prep["code"])
        print(json.dumps({"applied": True, "version": res["version"], "sha256": res["sha256"], "by": res["by"], "date": res["date"],
                          "decision": res["decision"], "test_command": res["config"],
                          "command": stack._clean(res["test_command"], stack.MAX_PROFILE, markup=False) if res["test_command"] is not None else None,
                          "test_timeout": _timeout_info(cfg, res["test_command"])}, indent=2))
        return 0
    try:
        cfg = config.Config.load()
    except config.ConfigInvalid:
        raise
    except config.ConfigError:
        cfg = None
    plan = stack.plan_approve(".", cfg)
    if shown:
        print(stack.preview_text(plan["profile"], plan["test_command"], plan["config_command"], plan["version"], args.story, args.by,
                                 plan["status"], bool(os.environ.get("SKJ_CONFIG")), cfg.strict["test_timeout"] if cfg else None))
    else:
        cmd = plan["test_command"]
        print(json.dumps({"applied": False, "status": plan["status"], "version": plan["version"], "counts": plan["counts"], "unverified": plan["unverified"],
                          "test_command": {"set": cmd is not None, "length": len(cmd) if cmd is not None else 0},
                          "test_timeout": _timeout_info(cfg, cmd),
                          "note": "read the profile and approve it in your own terminal window (stdin and stdout both a terminal): `skj stack approve`, then `--apply`"}, indent=2))
    return 0


def cmd_stack(args, client_factory):
    """The `stack` verbs: local only (no token, no request); every refusal is exit 2, as in the fork."""
    import json
    from skj import config, stack
    try:
        if args.stack_command == "detect":
            print(json.dumps(stack.detect(args.repo), indent=2))
            return 0
        return {"show": _stack_show, "init": _stack_init, "approve": _stack_approve}[args.stack_command](args)
    except (config.ConfigError, ValueError) as e:
        print("skj: %s" % e, file=sys.stderr)
        return 2


def _add_stack(sub):
    from skj import stack
    p = sub.add_parser("stack", help="which stacks the repository holds and the stack profile skj.stack.json (local only; `stack detect` reads the files committed at HEAD)")
    st = p.add_subparsers(dest="stack_command", required=True)
    q = st.add_parser("detect", help="detect the stack(s) from the files committed at HEAD (never the working tree) and show why: JSON "
                      "{outcome, stacks, domain_hints}; no commit yet is `unknown` with a note; exit 2 when git cannot be read")
    q.add_argument("--repo", default=".", metavar="DIR", help="the repository (default: the current directory)")
    q = st.add_parser("show", help="the stack profile skj.stack.json as status, counts and fixed error texts (JSON, the default); `--md` prints an "
                      "APPROVED profile as data for the agent; the text of a draft or changed profile is never printed; `--check` exits 2 in "
                      "strict mode unless the profile is approved, matches strict.test_command and covers every high-confidence root")
    fmt = q.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="JSON (default)")
    fmt.add_argument("--md", action="store_true", help="markdown (the profile text only when approved)")
    q.add_argument("--check", action="store_true", help="strict mode: exit 2 when there is a finding (advisory: always 0)")
    q.add_argument("--base", metavar="REF", help="the target branch whose strict config (and profile) judges (default: origin/HEAD)")
    q = st.add_parser("init", help="create the DRAFT profile skj.stack.json from a seed (package data) or from a file the agent wrote under .skj/. "
                      "A local write; the draft is not trusted until approved. Preview by default (counts and error positions only, never "
                      "profile text); --apply writes; an approved profile needs --force")
    q.set_defaults(parser=q)
    src = q.add_mutually_exclusive_group(required=True)
    src.add_argument("--from", dest="from_file", metavar="FILE", help="a regular file inside .skj/ (validated; status forced to draft, approval dropped)")
    src.add_argument("--seed", metavar="NAME", action="append", help="one of: " + ", ".join(stack.SEEDS) + ". Repeat it for a repository with "
                     "several stacks: one draft with one stack per seed (each at its detected root)")
    q.add_argument("--root", metavar="R", help="with ONE --seed: the stack's root (default: the one detected root that fits, else .)")
    q.add_argument("--stack", metavar="ID", help="name the stack (a profile with exactly one stack)")
    q.add_argument("--apply", action="store_true", help="write skj.stack.json (default: preview, nothing written)")
    q.add_argument("--force", action="store_true", help="with --apply: replace an approved profile")
    q = st.add_parser("approve", help="a human approves the profile (a terminal is needed for --apply): status approved, version + 1, an approval "
                      "record, and strict.test_command in skj.config.json set to the composed command (or removed). Preview by default: in a "
                      "terminal every command is printed in full, otherwise counts and the command's length only. Process enforcement, "
                      "not a security boundary; the approval is a record, not proof")
    q.add_argument("--story", type=_story, metavar="KEY", help="also record one decision line `Stack profile v<n> approved, sha256 <12>` for this story (an issue key, e.g. SPK-123)")
    q.add_argument("--by", type=_role, default="tech lead", metavar="ROLE", help="a role, never a name (letters, digits, spaces; at most 40; default: tech lead)")
    q.add_argument("--apply", action="store_true", help="write the approval (needs stdin and stdout to be terminals; the printed code must be typed)")
    p.set_defaults(func=cmd_stack)


def _kits_verify(args):
    """`skj kits verify`: local and read-only (no token, no config). JSON; exit 2 on a kit that is unpinned or changed, or when the registry
    or a file cannot be trusted (a fixed error text; a bad lockfile is ConfigInvalid, exit 2 here too)."""
    import json
    from skj import kits
    try:
        out = kits.verify(".", kits.load_lock("."))
    except kits.KitError as e:
        print("skj: kits check failed: %s" % e.reason, file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    if out["findings"]:
        print("skj: kits verify failed: %d finding(s)" % len(out["findings"]), file=sys.stderr)
        return 2
    return 0


def _kits_pin(args):
    """`skj kits pin ID [--by ROLE] [--apply]`: a human's approval record of one installed kit in `skj.kits.lock.json`. The preview prints the
    digests, the scan facts and the fixed checklist only (never kit text, so any output is safe). `--apply` needs a terminal on BOTH stdin
    and stdout, tested before any file is read (a guard against accidents, not a security boundary: a pseudo-terminal gives a tty); it
    prints that preview, then `approval code: <12 hex>`, and pins only when the human types the code (the code covers the lock entry; the
    kit is read again and the code recomputed right before the write). As in the fork, `--by` is a role the human states, not checked
    against `approvers`: the record is the human at this terminal."""
    import json
    from skj import kits, stack
    if args.apply and not (sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty()):
        raise ValueError("pinning a kit records that a human reviewed it, which only a human may do: run it in your own terminal "
                         "(stdin and stdout must both be a terminal, so the full preview is seen; a guard against accidents, not a security boundary)")
    try:
        prior = kits.load_lock(".")  # a bad lockfile is refused here too, in the preview as well as at --apply
        plan = kits.plan_pin(".", args.id)
        shown = {k: v for k, v in plan.items() if k != "source"}  # the manifest's URL is kit text: it is recorded, not printed
        out = {"applied": False, **shown, "source_recorded": plan["source"] is not None}
        if args.id in prior["kits"]:
            out["note"] = "this kit is already pinned: --apply REPLACES its entry (version, digests, scan, reviewer)"
        if args.apply:
            print(json.dumps(out, indent=2))
            code = kits.pin_code(plan, args.by)
            if not stack.confirm_code(code):
                raise ValueError(stack.E_NOT_CONFIRMED)
            kits.apply_pin(".", plan, args.by, code)
            out["applied"] = True
    except kits.KitError as e:
        print("skj: kits pin refused: %s" % e.reason + (kits.SYMLINK_HINT if e.reason == "symlink" else ""), file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))  # after --apply: the pinned digests (dir_digest, agents_sha256) as written
    return 0


def cmd_kits(args, client_factory):
    """The `kits` verbs: local only (no token, no request, no config); every refusal is exit 2, as in the fork."""
    from skj import config
    try:
        return _kits_pin(args) if args.kits_command == "pin" else _kits_verify(args)
    except (config.ConfigError, ValueError) as e:
        print("skj: %s" % e, file=sys.stderr)
        return 2


def _add_kits(sub):
    p = sub.add_parser("kits", help="Spec Kit extensions against the lockfile skj.kits.lock.json (local, read-only but for `pin --apply`)")
    ks = p.add_subparsers(dest="kits_command", required=True)
    ks.add_parser("verify", help="list the installed kits and whether each is pinned, changed or unpinned (JSON); exit 2 on any kit "
                  "that is unpinned or changed since a human pinned it, or when the registry or a kit file cannot be trusted")
    q = ks.add_parser("pin", help="a human pins one installed kit in skj.kits.lock.json (a terminal is needed for --apply): records digests of the kit "
                      "files and agent copies, the scan facts and who reviewed it. Preview by default: digests, scan counts with file:line and a "
                      "fixed 5-line checklist, never kit text. A --dev install, a symlink and an empty kit dir cannot be pinned. A human approval "
                      "record, process enforcement and not a sandbox")
    q.add_argument("id", help="an installed kit id (a-z, 0-9, -)")
    q.add_argument("--by", type=_role, default="tech lead", metavar="ROLE", help="a role, never a name (letters, digits, spaces; at most 40; default: tech lead)")
    q.add_argument("--apply", action="store_true", help="write the lockfile entry (default: preview, nothing written; both stdin and stdout must be terminals)")
    p.set_defaults(func=cmd_kits)


def cmd_strict(args, client_factory):
    """`skj strict check|verify|mode`: local and read-only but for verify's record; `check --phase implement` reads the story's subtasks
    (the board check: the client is built only then, and a client that cannot be built is the finding board-unchecked). The config
    is `_judging_config`'s: a strict target branch judges, the PR cannot lower it. Exit 2 lists the findings as JSON; `mode` exits 3
    when the config cannot be trusted (CI fails closed), `check` and `verify` exit 2 then."""
    import json
    from skj import config, strict
    try:
        args.base = args.base if args.base is not None else (config.ci_head(".") or None)  # DF5 D1: also the range of check/verify
        cfg = _judging_config(args.base)
        if args.strict_command == "mode":
            out = {"mode": cfg.mode, "source": "target branch" if cfg.from_target else "pull request"}
        else:
            key = _project_key(args.ref, cfg.project)
            if key is None:
                return 2
            if args.strict_command == "verify":
                out = strict.verify(cfg, key, record=args.record, base=args.base, allow_override=args.allow_override)
            else:
                base = args.base
                if base is None and args.phase == "pr" and cfg.mode == "strict":
                    base = config.default_base(".")  # "" when there is no default base (no origin/HEAD, no guarded home ref): base-unknown, never a pass
                out = strict.check(cfg, key, args.phase, head=args.head, base=base, target=args.base, client_factory=client_factory)
    except config.ConfigInvalid as e:
        print("skj: %s" % e, file=sys.stderr)
        return 3 if args.strict_command == "mode" else 2
    except (config.ConfigError, ValueError) as e:
        print("skj: %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    if args.strict_command != "mode" and not out["ok"]:
        print("skj: strict %s failed: %d finding(s)" % (args.strict_command, len(out["findings"])), file=sys.stderr)
        return 2
    return 0


def _add_strict(sub):
    from skj import strict
    p = sub.add_parser("strict", help="strict SDD+TDD mode: phase checks (read-only) and `verify` (runs the tests, records the result)")
    ss = p.add_subparsers(dest="strict_command", required=True)
    q = ss.add_parser("check", help="check the strict-mode rules of a phase (read-only; exit 2 lists the findings as JSON)")
    q.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    q.add_argument("--phase", required=True, choices=sorted(strict.PHASES), help="implement, pr or done")
    q.add_argument("--base", metavar="REF", help="the ref the PR targets, e.g. origin/main (resolved as refs/remotes/REF, refs/heads/REF, then a full "
                   "commit id, never a tag): a strict config there judges; pr: red and green must lie in REF..head (an unknown REF is the "
                   "finding base-unknown; default: the branch origin/HEAD points at)")
    q.add_argument("--head", metavar="REF", default="HEAD", help="the branch or commit the PR is made from (default HEAD; CI passes the head SHA)")
    q = ss.add_parser("mode", help="which mode CI judges a PR by (read-only, no token): prints {\"mode\", \"source\"}; exit 3 when the config "
                      "cannot be trusted (CI fails closed)")
    q.add_argument("--base", metavar="REF", help="the ref the PR targets: a strict config there decides (default: origin/HEAD)")
    q = ss.add_parser("verify", help="run L1, then strict.test_command at each AC's red (must fail) and green (must pass) commit; records the "
                      "result in ledger section 4 (exit 2 lists the findings as JSON)")
    q.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    rec = q.add_mutually_exclusive_group()
    rec.add_argument("--record", dest="record", action="store_true", default=True, help="append the verification record to the ledger (default)")
    rec.add_argument("--no-record", dest="record", action="store_false", help="write nothing, only set the exit code (CI)")
    q.add_argument("--base", metavar="REF", help="a strict config there judges; refuse a red or green commit that is already in REF")
    q.add_argument("--allow-override", action="store_true",
                   help="with --no-record: accept an override in force (default: it is the finding override-in-force, since it skips the run)")
    p.set_defaults(func=cmd_strict)


def cmd_calibrate(args, client_factory):
    """`skj calibrate (--emit PATH | --score FILE)`: local only (no token, no network, no model, no config: `client_factory` is never called). `--emit`
    writes the plane 8 rubric and the 12 unlabelled golden items under `.skj/`; `--score` reads a model's answer (under `.skj/`, at most 64 KiB) and
    exits 0 only at agreement >= 10 of 12 with no false pass, else 1. A refused file or answer is exit 2 with a fixed text, never a piece of the answer."""
    import json

    from skj import calibrate
    try:
        if args.emit is not None:
            path = calibrate.skj_path(args.emit, ".md")
            items = calibrate.load_items()
            text = calibrate.emit(calibrate.read_review("."), items)
            try:
                calibrate.write_emit(path, text)
            except OSError:
                raise ValueError(calibrate.E_WRITE) from None
            print(json.dumps({"emitted": os.path.relpath(path), "items": len(items), "bytes": len(text.encode("utf-8"))}, indent=2))
            return 0
        result = calibrate.score(calibrate.load_items(), calibrate.read_answer(calibrate.skj_path(args.score)))
    except (ValueError, OSError) as e:
        print("skj: %s" % (e if isinstance(e, ValueError) else calibrate.E_UNREADABLE), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def _add_calibrate(sub):
    p = sub.add_parser("calibrate", help="How far a model's plane 8 (AC fidelity) reading can be trusted: --emit writes the rubric and 12 unlabelled "
                       "golden items under .skj/; --score reads the model's JSON answer (exit 0 at 10 of 12 and no false pass, else 1; local only)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--emit", metavar="PATH", help="write the calibration prompt to PATH (a .md file under .skj/)")
    g.add_argument("--score", metavar="FILE", help='score the answer in FILE (under .skj/, at most 64 KiB): {"G1": "asserts|partial|does not assert", ...}')
    p.set_defaults(func=cmd_calibrate)


def cmd_status(args, client_factory):
    """`skj status KEY [--json] [--offline]`: the first unmet stage of a story and the one command that moves it on. Read-only (no write flag;
    only local reads and GETs). It reports, it does not judge: exit 0 whatever the stage, also with Jira unreachable (then only the local
    stages are judged and a line says so). The config that judges is `_judging_config`'s (a strict target branch cannot be lowered by the
    PR). Without a usable config the key must be given in full."""
    import json
    from skj import config, dod, keys, status
    try:
        project = config.Config.load().project
    except config.ConfigError:
        project = None  # the `config` stage says so
    if project is not None:
        key = _project_key(args.ref, project)
        if key is None:
            return 2
    elif keys.valid_key(args.ref):
        key = args.ref
    else:
        print("skj: no usable skj.config.json: give the full issue key, e.g. SPK-123", file=sys.stderr)
        return 2
    try:
        repo = str(dod.repo_root())
    except ValueError:
        repo = "."
    res = status.evaluate(client_factory, key, repo, offline=args.offline, cfg_loader=lambda r: _judging_config(repo=r))
    print(json.dumps(res, indent=2) if args.json else status.render(res, key))
    return 0


def _add_status(sub):
    p = sub.add_parser("status", help="which stage a story is at and the one command that moves it on (read-only; the Jira stages need the network)")
    p.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.add_argument("--offline", action="store_true", help="do not read Jira: the stages that need it are 'not checked', never met")
    p.set_defaults(func=cmd_status)


def cmd_approvals(args, client_factory):
    """`approvals check KEY --gate G` and `approvals sync RUN_ID [--resume] [--dry-run]` (A4, EXPERIMENTAL comment approval). The gate or
    the run id is checked first, then the TARGET branch's trust keys (`config.trusted()` with no argument, never the working tree). In
    terminal mode `check` is a no-op (exit 0, no client, no request) and `sync` is refused (exit 2, nothing read). A refusal is exit 2
    with a fixed text (`approvals.ApprovalMissing`); a failed resume exit 1. Neither ever writes to Jira."""
    import json
    from skj import approvals, config, dod
    if args.approvals_command == "sync":
        if not approvals.valid_run_id(args.run_id):
            raise approvals.ApprovalMissing(approvals.E_RUN_ID)
        trust = config.trusted()
        approvals._ci_target_known(trust)
        if trust.approval == "terminal":
            raise approvals.ApprovalMissing(approvals.E_POLL_TERMINAL)
        cfg = config.Config.load()
        out = approvals.sync(client_factory, trust, cfg, args.run_id, dod.repo_root(), resume=args.resume, dry_run=args.dry_run)
    else:
        if args.gate not in approvals.GATES:
            raise approvals.ApprovalMissing(approvals.E_GATE)
        cfg = config.Config.load()
        key = _project_key(args.ref, cfg.project)
        if key is None:
            return 2
        out = approvals.check(client_factory, config.trusted(), cfg, key, args.gate, dod.repo_root())
    print(json.dumps(out, indent=2))
    return 0


def _add_approvals(sub):
    p = sub.add_parser("approvals", help='EXPERIMENTAL comment approval (approval "comment" on the target branch): check a workflow gate, '
                       "or poll the gate a Spec Kit run is paused at (read-only on Jira)")
    asub = p.add_subparsers(dest="approvals_command", required=True)
    q = asub.add_parser("check", help="exit 0 when the gate is met, 2 with a fixed text when not (GETs only). Review gates need an "
                        "approver's `/approve <gate> <hash>` of the artifacts at HEAD; the wait gates are met by the approvers' /answer, "
                        "/approve and /reject comments; a no-op when approval is \"terminal\" on the target branch")
    q.add_argument("ref", help="issue number (123) or key (SPK-123) of the configured project")
    q.add_argument("--gate", required=True, metavar="GATE", help="one of: " + ", ".join(_approval_gates()))
    q.set_defaults(func=cmd_approvals)
    q = asub.add_parser("sync", help="the poller (comment mode only): prints {gate, verdict: approve|reject|waiting} for the gate a Spec "
                        "Kit run is paused at, from the approvers' comments; with --resume runs `specify workflow resume` with that "
                        "verdict. Never writes to Jira. Exit 0, 1 (the resume failed), 2 (refused)")
    q.add_argument("run_id", metavar="RUN_ID", help="the Spec Kit run id")
    q.add_argument("--resume", action="store_true", help="run `specify workflow resume` with the verdict when there is one")
    q.add_argument("--dry-run", action="store_true", help="resume nothing: only print what would happen")
    q.set_defaults(func=cmd_approvals)


def _approval_gates():
    from skj import approvals
    return approvals.GATES


COMMANDS = (_add_comments, _add_get, _add_dod, _add_rules, _add_decisions, _add_questions, _add_branch, _add_push, _add_done, _add_pr, _add_gate, _add_doctor, _add_setup, _add_stack, _add_kits, _add_strict, _add_calibrate, _add_status, _add_approvals)  # each adds one subparser


def build_parser():
    ap = argparse.ArgumentParser(prog="skj")
    ap.add_argument("--version", action="version", version=f"skj {__version__}")
    sub = ap.add_subparsers(dest="command")
    for add in COMMANDS:
        add(sub)
    for p in _all_parsers(ap):  # after every verb is added, so a new one cannot forget: `--app` must never mean `--apply` (X2)
        p.allow_abbrev = False  # per parser: 3.9 does not pass it on to subparsers
    return ap


def _all_parsers(ap):
    """Every parser in the tree, at any depth, each once (an alias shares its verb's parser)."""
    seen, todo = [], [ap]
    while todo:
        p = todo.pop()
        if any(p is s for s in seen):
            continue
        seen.append(p)
        todo += [c for a in p._actions if isinstance(a, argparse._SubParsersAction) for c in a.choices.values()]
    return seen


def main(argv: Optional[List[str]] = None, *, client_factory=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command is None:
        return 0
    if getattr(args, "expect_plan", None) and not args.apply:
        ap.error("--expect-plan needs --apply")
    from skj import config, secretstore
    from skj.approvals import ApprovalMissing
    from skj.client import ClientError
    from skj.keys import UsageError
    try:
        # doctor builds its own client after its site-pin row (None: its default); every other verb gets default_client
        factory = client_factory or (None if getattr(args, "own_client", False) else default_client)
        return args.func(args, factory)
    except (PlanChanged, ApprovalMissing) as e:  # ApprovalMissing: comment approval refused (A4), a fixed text; nothing written
        print("skj: %s" % e, file=sys.stderr)
        return 2
    except EOFError:
        print("skj: no input; nothing written", file=sys.stderr)
        return 2
    except UsageError as e:  # a malformed command-line value (D-001): exit 2 like every other usage error
        print("skj: %s" % e, file=sys.stderr)
        return 2
    except (ClientError, config.ConfigError, config.AuthRefused, secretstore.SecretError, ValueError) as e:
        print("skj: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
