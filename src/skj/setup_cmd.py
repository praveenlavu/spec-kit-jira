"""`skj setup`: write skj.config.json from the template and tell the human how to store the Jira identity (K6).

Nothing is sent to Jira. The config holds the site and the project, never a secret or an email (emails differ per developer).
macOS: the identity lives in the Keychain and this tool never writes it and never reads the token: setup prints the
`security add-generic-password ... -w` line (with an `<your-email>` placeholder, so the output carries no email) for the human
to run; `-w` with no value makes `security` prompt, which keeps the token out of argv. Elsewhere: the email is asked, the token
is read with a hidden prompt and `secretstore.store_file` writes ~/.skj/<site>.json (mode 0600). Output never echoes either.
An existing skj.config.json (or anything at that path, a symlink included) is never overwritten.
"""
import getpass
import json
import os
import sys

from . import secretstore
from .config import CONFIG_NAME, Config
from .keys import PROJECT_RE, SITE_RE, UsageError

# The repository's skj.config.template.json (a test keeps the two equal); site and project are replaced.
TEMPLATE = {
    "site": "example-site.atlassian.net",
    "project": "SPK",
    "story_type": "Story",
    "subtask_type": "Subtask",
    "ac_heading": "Acceptance criteria",
    "transitions": {"active": None, "done": None},
    "branch_prefix": "feature/",
    "labels": [],
    "gate": {"tests": {}, "block": []},
    "review_block_on": [],
    "stack": {"kit": "", "phases": {}},
}


class SetupError(ValueError):
    """Setup refused; the text is fixed (never a typed value)."""


class SetupUsage(SetupError, UsageError):
    """A malformed --site or --project: exit 2 (D-001)."""


def keychain_line(site):
    return "security add-generic-password -a <your-email> -s skj:%s -w" % site


def run_setup(site=None, project=None, *, out_path=CONFIG_NAME, input_fn=input, getpass_fn=getpass.getpass):
    """0 when the config was written (and, off macOS, the identity stored); 2 when the human entered nothing."""
    site = (site if site is not None else input_fn("Jira site (<name>.atlassian.net): ")).strip()
    if not SITE_RE.fullmatch(site):
        raise SetupUsage("invalid site: must be <name>.atlassian.net (lowercase, no scheme or path)")
    project = (project if project is not None else input_fn("Project key (e.g. SPK): ")).strip()
    if not PROJECT_RE.fullmatch(project):
        raise SetupUsage("invalid project key: an uppercase letter, then 1-9 uppercase letters, digits or '_'")
    if os.path.lexists(out_path):
        raise SetupError("%s already exists; not overwriting (edit it by hand)" % CONFIG_NAME)
    data = dict(TEMPLATE, site=site, project=project)
    Config._from_data(data, CONFIG_NAME)  # never write what load() refuses
    darwin = sys.platform == "darwin"
    if not darwin:
        email = input_fn("Jira account email: ").strip()
        token = getpass_fn("Jira API token (input hidden): ").strip() if email else ""
        if not token:
            print("No email or token entered: nothing stored, nothing written.")
            return 2
        secretstore.store_file(site, email, token)
        email = token = None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(out_path, flags, 0o644)
    except FileExistsError:
        raise SetupError("%s already exists; not overwriting (edit it by hand)" % CONFIG_NAME) from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    print("Wrote %s for %s, project %s. Commit it to the default branch: the site is pinned from there." % (
        CONFIG_NAME, site, project))
    if darwin:
        print("Store your Jira identity in the Keychain: run this, with your Atlassian email in place of <your-email>; "
              "it prompts for the API token (https://id.atlassian.com/manage-profile/security/api-tokens):")
        print("  " + keychain_line(site))
    else:
        print("Stored the identity in ~/.skj/%s.json (mode 0600)." % site)
    print("Then run `skj doctor`.")
    return 0
