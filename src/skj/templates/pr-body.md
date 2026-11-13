## Summary

{{summary}}

Story: {{key}} ({{jira_url}})

## Acceptance criteria

| AC | status | tests |
|---|---|---|
{{ac_rows}}

Full traceability, decisions and the Definition of Done: `{{ledger_path}}` on this branch.

Decisions and assumptions: {{decisions}}

## Gate{{gate_date}}

{{gate_lines}}

## Review{{review_date}}

{{review_lines}}

## Reviewer checklist

- Read the AC table against the ledger section 3: a status is copied from the ledger, never upgraded here.
- Check by hand what the gate cannot judge (rollback notes, data changes, anything the review lists as open).
