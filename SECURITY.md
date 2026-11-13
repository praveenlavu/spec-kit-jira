# Security Policy

## Supported versions

Only the latest 0.x release gets security fixes. Older 0.x releases do not: upgrade to the latest one. The latest release
is 0.1.0.

## Reporting a vulnerability

Report it privately through GitHub private vulnerability reporting on this repository: open the repository's Security tab and
choose Report a vulnerability, or go straight to `https://github.com/praveenlavu/spec-kit-jira/security/advisories/new`. The report reaches the repository's
maintainers privately. Please do not open a public issue, pull request or discussion for a suspected vulnerability.

Please include:

- the affected version (`skj --version`) and how it was installed
- steps to reproduce, or a minimal proof of concept
- what you expected, what happened, and the impact you see

## What to expect

We will acknowledge your report. We will then tell you whether we can reproduce it, and work on a fix with you in the private
advisory before anything is made public. No response time is promised.

## Keep secrets out of the report

Never paste an API token, secret or real site data into an issue, pull request or report. Use fictional names (for example
`example-site.atlassian.net` and issue `PROJ-1`). If a token may have been exposed, revoke it in your Atlassian account first.
