# Port notes: tests carried from the fork

This file records how the fork's test suite was carried into this repository: what was ported with changes, what was dropped and
why, what is deferred to 0.2, every skip in `tests/`, and the known 0.2 backlog. It is built from the build ledger (task reports and
the progress log under `.superpowers/sdd/PLAN-0.1/`, not tracked) and from this repository's own `tests/` tree at commit 5c63a9a.
Task ids (S4, R4, L5a, ...) are the build's own task labels; the build plan that defined them is not part of this repository. "Unknown" means the ledger does not record it and it
was not checked; it is never a guess.

`tests/test_port_notes.py` keeps this file honest: the five sections exist, section 4 lists every skip call site found by an AST
walk of `tests/*.py` (with its count), and every test file in the PORTED list of `tests/test_no_ado_leftovers.py` exists.

## 1. Ported with changes

One row per fork test group. The task reports hold the per-test lists.

| Fork group | Files here | What changed and why | Task |
|---|---|---|---|
| tasks, calibrate | `test_tasks.py`, `test_calibrate.py` | Package rename. calibrate: exit codes kept from the fork (refusal 2, not-ok score 1); the golden fixtures' Apex assertions reworded so the no-fork-names grep passes (labels, ids and AC text unchanged); the review rubric is read from the real `commands/review.md`. | S3, S3c, F2 |
| kits, kits_scan | `test_kits.py`, `test_kits_scan.py` | Package rename and the `.skj` directory; all 41 + 14 fork test functions kept; a golden digest vector added; the lockfile is read from the target branch (`kits.lock_for`). | S3b |
| secretstore | `test_secretstore.py` | Rewritten for K6: Keychain service `skj:<site>`, account = the email, the token never in argv; a file store mode 0600 off macOS; CI env `SKJ_EMAIL`/`SKJ_TOKEN`. | S4, S4b, S4c |
| stack_*, seeds | `test_stack_approve.py`, `test_stack_coverage.py`, `test_stack_detect.py`, `test_stack_init.py`, `test_stack_show.py`, `test_stack_validate.py`, `test_stack_support.py`, `test_stack_linear.py`, `test_seed_node_typescript.py`, `test_seed_python_pytest.py`, `test_seed_salesforce.py` | Package rename; strict and docs halves of some tests split off (see section 2); `profile_for` moved to L5a; `kits pin --by` half restored in S3b; the pty harness resets SIGINT so it also passes in a background job (H1b). | L4, L5a, S3b, H1b |
| gate | `test_gate.py` | Story id to key (`SPK-7`), HTML AC to ADF, `dispatch(FakeClient, args)` to `gate.evaluate`; Jira smart-commit commands added as a check; the rules branch is in `test_rules_coverage.py` and `test_rules_drift.py` (RU3). | L3, RU3 |
| strict, strict_runner, strict_wire, ci_trust | `test_strict.py`, `test_strict_runner.py`, `test_strict_wire.py`, `test_strict_checks.py`, `test_strict_stubs.py`, `test_strict_linear.py`, `test_ci_trust.py` | Split in two: core (L5a) and the checks that needed later modules (L5b). Ledger directory fixed to `dod/` (K8), so the `dod_path` variants inside kept tests are gone; the target-branch config always wins; `test_strict.py` 161 by name, 3 renamed. | L5a, L5b, A3 |
| approvals | `test_approvals.py` | HTML to ADF, GUID to accountId (kept exactly, never lower-cased); the ADO versions API, bypass bit, on-behalf and revision cases removed; edit detection uses `updated`/`updateAuthor`; the command must be the first non-empty block (stricter than the fork). | A1, A2, A4 |
| comments | `test_comments.py` | Rebased on Jira comments in ADF with a 1 MiB local cap per body. | R2 |
| decisions | `test_decisions.py` | Rebased: local verbs (L1), posting (W5), `--sync` (A2); `via:` gains an optional `:<hash8>`; a changed decision posts a new comment. | L1, W5, A2 |
| questions | `test_questions.py` | Questions live in ledger section 2; plain text lines for ADF instead of HTML escaping; ADO identity checks replaced by `/myself` checks on FakeJira. | L2, W4, A2 |
| dod | `test_dod.py` | Rebased on ADF; AC read from a description section under the configured heading (K2); a missing or empty AC section is refused, not given a placeholder; no `--force`. | R4, R4b |
| status | `test_status.py` | Board double to FakeJira; ADO artifact links to Jira remote links; done by status category, never `resolution` (K3); run awareness removed (section 2); the rules stage is back since RU3 (`test_stage_rules`, `test_rules_stage_only_when_enabled`). | R6, RU3 |
| prs | `test_prs.py` | `skj pr create` builds title and body from the ledger itself; the fork's agent-written prepared files and `--check-committed` are not ported. | P1 |
| push | `test_push.py` | Tasks become subtasks (K2); no title update and no `removed` state (K9, K10); a ticked task with no subtask yet is transitioned on the next push. | W3 |
| done | `test_done.py` | Done = status category `done` (K3); the fork's `done` has no `--force`, so none exists here either. | P4 |
| doctor, setup, config | `test_doctor.py`, `test_setup.py`, `test_config.py` | Organization replaced by the pinned `site` (K7); accountIds kept exactly; trust keys read from the target branch; `setup` prints the Keychain line for the human (K6). | S8, R3, R3b, A5 |
| client | `test_client.py`, `test_client_retry.py`, `test_client_construction.py` | Rewritten (class D); the fork's retry, size-cap and no-redirect patterns reused as patterns. | S6, S6b, S7 |
| branch | `test_branch.py` | Slug table, round trip, empty prefix, not-a-repo and real-git dry-run/apply kept in spirit; key parsing replaces AB# ids. | R5 |
| attachments | `test_attachments.py` | Upload only (download cut, K9); adds plain-SVG checks and refuses hard links; a skip needs the same name and size (fork: name alone). | P3 |
| newest_posting | `test_newest_posting.py` | Rebased: the matrix keeps the SYNC column only (wait gate and cross-check columns live in A4/A3 tests). | A2 |
| strict xcheck | `test_strict_xcheck.py`, `test_trust_crosscheck.py` | Rebased fork cases plus new cross-check tests; only synced lines are cross-checked. | A3 |
| trust_minors | `test_ci_trust.py` | 6 of 22 fork tests (non-root trust keys, raw format characters in `src/`) moved into `test_ci_trust.py`; the invisible-character rule test (`test_the_invisible_character_rule_matches_exactly_the_documented_code_points`, over `rules._INVISIBLE`) added there in Fix-RU. | L5a, Fix-RU |
| workflow | `test_workflow.py` | Input `issue_number` (type number, K4); 12 verdict inputs; real-engine runs of the installed `specify` CLI against FakeJira. | F1 |
| manifest, docs | `test_manifest.py`, `test_docs.py` | Pins rewritten for the skj names and the agent documents; the public docs pins of C3. | F2, F3, C2 |
| speckit_contract, ci_*, github_workflows | `test_speckit_contract.py`, `test_ci_smoke.py`, `test_ci_smoke_fixtures.py`, `test_github_workflows.py` | Pins rewritten for the four GitHub templates, the kit's own workflows and the local rehearsal harness. | F4, C1, H1b |
| scrub_check | `test_scrub_check.py` | Rebuilt from the fork test's behaviour list (the fork file was not read): case-insensitive substring, a hit never prints the term, every secret pattern including `atlassian-token`, env denylist wins, `--history` (removed term, author email, message), `--require-denylist` exit 2, staged blob scanned, git failure exit 3; the secret list equals `skj.stack.SECRETS`. Runs the script as a CLI in a throwaway git repository. | T5 |
| rules (parse, file, fixtures) | `test_rules_parse.py`, `test_rules_file.py`, `test_rules_fixtures.py` | ADF descriptions in place of HTML (the fixtures are hand-written ADF, not captured from Jira); `BR-<KEY>-<n>` and `Rules_<KEY>.md` in place of integer ids, checked with `keys.KEY_RE`; the site replaces the organization; `rules-format` started at 1 (2 since RU2, whose file note names `skj rules`); the HTML-only problem `unclosed-markup` is replaced by `unreadable-content` (an unknown, malformed, too deep or macro node). The rule hashes of the four fixtures equal the fork's. | RU1 |
| dod rules header | `test_dod.py` (`test_ru1_*`) | `Rules from:` holds issue keys; a refresh keeps `Rules reviewed:` and refuses a malformed or untrusted one; no `--force` exists, so its refusal tests have no subject. | RU1 |
| rules pull (the walk and `skj rules`), dod `Rules from:` | `test_rules_pull.py` | The pull half of the fork file (the walk, the verb, the dod `Rules from:` tests): `fields.parent` in place of the hierarchy link, keys in place of ids (`problems`/`decisions` entries carry `key`), ADF descriptions, the fake Jira's GET log in place of the call list; a cycle or a self-parent is now a refusal (exit 2 for `skj rules`, 1 for `skj dod`, nothing written) where the fork stopped quietly; a bad parent field rebases the bad-URL cases; a moved issue is `issues.KeyMismatch`; local refusals exit 2, Jira errors 1. Added: `test_parent_cycle_stops`, the depth-cap test with a 5-level chain, hostile ADF descriptions, a GET-only recorder. The parent chains above an Epic are hand-written (U22). | RU2 |
| rules scope (tags, exclusions) | `test_rules_scope.py` | Keys in place of ids (`BR-LOY-1201-1`); the tag run now includes `_`, as keys and `TAG_RE`'s look-arounds do (RU1 hand-off; `test_tags_runs_include_underscore_as_keys_do` added); ids sort by (project, issue number, rule number); added a stray-key case and lower-case and keyless exclusion ids. | RU3 |
| rules coverage (the gate's `rule_coverage`) | `test_rules_coverage.py` | The rules block of the fork's `test_gate.py`: keys in place of ids, `gate.evaluate` in place of the CLI dispatch; problem items name the issue key (`LOY-1201:no-parsable-rules`); the summary fix texts name `skj rules` and `skj dod refresh`. | RU3 |
| rules drift (`rule_drift`, live, GETs only) | `test_rules_drift.py` | The fake Jira in place of the fork's scripted client; errors are an HTTP status (404, 403, 401) or a transport error per request; a hostile answer is the fake's issue render replaced; added `client None` (no identity) and the gate's `--offline`/no-identity CLI paths. The chains are hand-written (U22, U24, U25). | RU3 |
| strict rules check | `test_rules_strict.py` | The rules block of the fork's `test_strict.py`, on this repository's `_story`; the fix texts name `skj` commands; the rules stub is gone (`STUBS == {}`). | RU3 |
| rules reconfirmed (target branch and branch history) | `test_rules_reconfirmed.py` | Keys in place of ids; the ledger is always `dod/DoD_<KEY>.md`, so the `dod_path` refusal cases became revision-expression bases; added a failing `git log`. | RU3 |
| `skj rules --confirm` | `test_rules_confirm.py` | The `--confirm` half of the fork's `test_rules_pull.py`: keys in place of ids, `skj:` in place of `error:`, `key` in place of `id` in the JSON; the ledger comes from `skj dod seed`; the pty harness test moved here with it. | RU3 |
| ci_trust rules case | `test_ci_trust.py::test_pr_disabling_rules_still_judged_enabled` | The fix text names `skj dod refresh`. | RU3 |
| cli (write verbs) | `test_x2_no_write_without_apply.py` | Mapping to a single fork file: unknown. Every write verb is a dry run without `--apply` (X2). | W1 |

## 2. Not ported (dropped)

| Fork test or group | Reason | Jira equivalent here |
|---|---|---|
| workitems, branch_link, pipelines, advisory_regression (about 630 lines) | ADO work items, Azure Repos branch links and Azure pipelines | none: feature not in 0.1 |
| HTML-to-text converter tests in dod (`test_text_strips_html_and_unescapes` and 6 more) | the converter does not exist; Jira text is ADF | `test_adf.py`; `test_dod.py::test_render_is_linear_on_a_large_ac` |
| `test_render_handles_missing_ac`, `test_empty_ac_keeps_placeholder_and_is_thin` | R4-AC1 inverts them: a missing or empty AC section is refused | `test_dod.py` (`test_ac_section_refuses_empty_duplicate_and_unknown`) |
| `test_force_overwrites` (dod) | `--force` not ported; it carries the rules header protection (RU1 to RU3) | none: feature not in 0.1 |
| `test_force_refuses_with_reviewed_line_and_leaves_ledger_untouched`, `test_force_refuses_a_reviewed_line_in_an_untrusted_header_and_hostile_bytes`, `test_advice_never_says_use_force_without_the_reviewed_line_caveat`, `test_force_without_reviewed_line_unchanged` (dod) | `skj dod` has no `--force`: deleting the file is the way to start over, and a refresh keeps the line | `test_dod.py::test_ru1_refresh_keeps_rules_reviewed_byte_for_byte` |
| `test_cli_refresh_of_a_laundering_ledger_exits_2_with_the_ledger_untouched` (dod) | the refusal half was ported in RU1; the other four fork dod `Rules from:` tests are ported in RU2 (section 1) | `test_dod.py::test_ru1_cli_refresh_of_a_malformed_reviewed_line_exits_2_untouched` |
| `test_walk_two_parents_or_an_id_that_does_not_match_stop` (rules_pull) | a Jira issue has one `parent` field, so two parents cannot occur; an answer for another key is refused by `issues.get` (an error, not a problem) | `test_rules_pull.py::test_walk_bad_parent_field_stops`, `::test_walk_a_moved_ancestor_or_story_is_refused` |
| the `--post` half of `test_ado_decisions_add_and_post_work_for_a_feature_id` (rules_pull) | posting is the decisions verb's own (W5) and already takes any key of the project | `test_rules_pull.py::test_decisions_add_works_for_an_epic_key`; `test_decisions.py` posting tests |
| `test_an_opener_in_a_header_value_cannot_forge_the_header`, `test_header_values_*`, `test_spec_dir_round_trips_*` (dod, rules variants) | the ledger header values are R4's and already tested there; only the rules lines are new | `test_dod.py` R4 header tests, `test_ru1_section1_line_cannot_forge_header` |
| HTML normaliser steps (rules_parse): `test_comments_removed_non_greedy`, `test_word_downlevel_revealed_comment_keeps_text`, `test_style_script_xml_elements_removed`, `test_unclosed_markup_is_error`, `test_entities_nbsp_and_crlf`, `test_entity_encoded_and_span_split_emails_are_scrubbed`, the HTML entries of `ADVERSARIAL` | a description is ADF, so there are no comments, elements, entities or tags to strip | `test_rules_parse.py` ADF walk tests (`test_unknown_malformed_or_macro_content_is_one_error`, `test_split_and_marked_emails_are_scrubbed`, `ADVERSARIAL_DOCS`) |
| `test_template_example_in_docs_parses` (rules_parse) | there is no `docs/business-rules.md` page yet (the public doc set is pinned by `test_docs.py`); RU2 adds none | none yet |
| `test_fixtures_are_raw_ado_html`, `test_word_fixture_asserts_exactly_the_artefacts_that_survived_ado` (rules_fixtures) | the fork's fixtures were read back from a real server; these are hand-written ADF | `test_rules_fixtures.py` (provenance stated in its docstring) |
| `test_doc_example_marker_is_the_real_hash` (rules_scope) | it reads `docs/business-rules-reference.md`, which this repository does not have | `test_rules_scope.py::test_scope_from_real_decisions_file` |
| `test_adoconfig_set_is_refused` (rules_pull, `--confirm`) | the fork refused a config path that could move `dod_path`; here the ledger is always `dod/` under the repository root, so `SKJ_CONFIG` cannot move it | none: nothing to refuse |
| `dod_path` cases of `test_unreadable_target_ledger_warns` (a parent-relative path, an absolute path, a pathspec) (rules_reconfirmed) | the ledger path is fixed (`dod/DoD_<KEY>.md`), never built from config | `test_rules_reconfirmed.py::test_unreadable_target_ledger_warns` (revision-expression bases) |
| `test_neither_unconfirmed_nor_changed_can_be_overridden`, `test_override_offers_no_rules_rule` (strict rules block) | `decisions.OVERRIDE_RULES` and `skj strict override` do not exist in 0.1; an override is a hand-written `skip <rule>` line, and `_COVERS` names no rules finding | `test_rules_strict.py::test_rules_findings_are_not_overridable` |
| `test_kits_and_strict_import_in_either_order`-style import check for rules (strict) | covered by every test that imports `skj.strict` (it imports `skj.rules` at load) | `test_strict_stubs.py::test_the_stubs_import_no_unbuilt_module` |
| Fork strict_wire rules cases (4: `pr create` and `done` refusing on a rules finding, a covered story passing, the CLI check and its target-branch judgement) | the strict_wire `pr create`/`done` tests were not ported one for one (row below); the rules findings reach `pr create` and `done` through the same `strict.check` | `test_rules_strict.py`; `test_ci_trust.py::test_pr_disabling_rules_still_judged_enabled` |
| Stack DoD items in dod (`test_new_ledger_gets_items` and 6 more) | `extra_dod`/`stack_dod_items` not ported | none: feature not in 0.1 |
| ADF has no textual quote end: mail-client quote classes, `<q>`, quote-closing tricks, `strip_quotes`, 1 MiB HTML shapes (approvals) | HTML-only shapes | `test_approvals.py` Markdown sweep and first-block rule |
| on-behalf, `forged`/`authorized_as`, revision paging, versions 1..n (approvals, A1, A4) | ADO identities and versions API | `test_approvals.py` author and `updateAuthor` guard |
| bypass bit / `comment_mode_allowed` (approvals, xcheck, newest_posting) | ADO "Bypass rules" permission has no Jira counterpart | `test_doctor.py` `edit-all` row (A5) |
| ADO anonymous identity, `connectionData`, org-level identity (questions, config) | no Jira counterpart | `test_questions.py` `/myself` tests; `test_config.py` site tests |
| approver lower-casing, organization tests, `dod_gate` rename message, byte-identical regression (config) | accountIds are exact; site pin replaces the organization | `test_config.py` |
| `test_sync_is_a_placeholder_that_refuses_before_anything_is_read` (decisions, questions) | they pinned an L1/L2 placeholder that A2 replaced | `test_decisions.py`, `test_questions.py` sync tests |
| `test_via_key_keeps_existing_fixture_splits` (decisions) | meta test over the fork's own test files | `test_decisions.py::test_l1_ac1_via_key_is_validated` |
| `render_dod`/`write_dod` decisions-heading tests (decisions) | ADO ledger template detail | `test_dod.py` |
| `test_renamed_task_updates_title`, `test_plan_shows_state_change_and_old_title` (push) | no title update in 0.1 | none: feature not in 0.1 |
| `test_removed_child_*`, `test_live_recreated_child_beats_lower_id_removed_one` (push) | no `removed` state (K9, K10) | `test_push.py` orphan rows |
| `test_failed_state_after_create_keeps_work_item_id`, `test_apply_closes_done_task_after_create` (push) | the done transition of a new subtask waits for the next push (W3 decision 1) | `test_push.py` deferred-transition tests |
| `test_children_none_skips_batch_get`, `test_children_wiql_orders_by_id` (push) | no WIQL or batch GET in Jira | `test_push.py` |
| `test_gate_preview_190_191_boundary` (push) | plan size owned by W1 | `test_x2_no_write_without_apply.py` |
| `test_done_task_uses_configured_done_state` (push) | replaced by a configured transition name | `test_push.py` |
| `test_pr_link_detected_from_relation`, ADO relation cases of `test_stage_pr_link` (status) | ADO artifact links | `test_status.py::test_pr_from_remote_link` and the hostile URL table |
| `test_verb_rejects_a_bad_id` (status) | skj takes a key or a number | `test_status.py::test_verb_rejects_a_bad_ref` |
| Spec Kit rename/switch plan, `--link` Azure Repos URLs, `--id-from`, AB# and int32 cap, segment exclusion, prefix tests (branch) | no skj counterpart; prefix tests already in `test_config.py` | `test_branch.py`, `test_config.py` |
| `test_render_error_redacts_token` (gate) | replaced | `test_gate.py::test_gate_refuses_when_skj_token_in_env` |
| request-comment cases of approvals (`request_posted_once` and 9 more) | no approval-request comment in 0.1 (A4b deferred, section 3) | none: feature not in 0.1 |
| `test_cli_story_id_range_still_checked_for_check_only` (approvals) | keys are validated by `_project_key` | which test pins `_project_key` for approvals: unknown |
| invisible-character-prefixed and HTML markup postings, `ci_identity` parameter, `test_marker_fix_text_names_a_non_approver_poster` (xcheck) | ADF marker must be the exact first line; no configured CI identity | `test_strict_xcheck.py` posting-superseded cases |
| `test_reworded_repost_then_revert_wait_gate_open`, `..._xcheck_finding`, `test_a_wait_gate_ignores_unmarked_lines`, `test_bypass_refusal_comes_before` (newest_posting) | wait gate and cross-check columns live elsewhere; bypass has no counterpart | `test_approvals.py` (A4), `test_strict_xcheck.py` (A3) |
| fork `tests/test_ci_speckit_repo.py` | strict kit-lock rules, already covered | `test_kits.py`, `test_kits_scan.py`, `test_ci_trust.py` |
| Not found by name at 5c63a9a, owner task recorded but whether a later task covered the behaviour under another name is unknown: `test_setup_never_writes_the_test_command` (stack_approve), `test_command_commit_examples_carry_story_ref` (gate), `test_test_command_fix_text_points_to_override_when_profile_exists`, `test_ignored_roots_are_a_warning_never_a_finding`, `test_ignored_roots_warning_text_is_pinned_in_the_docs` (stack_coverage), `test_branch_id_from_*` (3, ci_trust), `test_gate_comment_notes_a_first_kits_lockfile` (strict), `test_apply_files_match_the_shipped_workflow` (approvals), `test_rules_story_type_rule_and_shape_apply_from_target_branch` (config, dropped without a recorded reason) | unknown | unknown |
| Fork strict_wire `pr create`/`done` tests (17 + 6, e.g. `test_pr_create_strict_refuses_each_rule_without_prompt`, `test_done_strict_refuses_open_decision`) | not ported one for one; L5b says the behaviour is covered only in part | `test_strict_checks.py` (in part) |
| trust_minors approvals/sync/post halves (16) | handed to A2/A3/W5; whether each was rebased is unknown | unknown |
| parse_once, whole_delta, blob_at (fork group names) | no ledger record: unknown | unknown |
| release_checklist, catalog (fork group names) | not ported: written new for this repository's local catalog (Z2) and release documents (Z3) | `test_catalog.py` (Z2), `test_release_docs.py` (Z3) |

Removed in T10, when the internal build files left the tree (they were checks that a build file agrees with a tracked one, so
the tracked side stands alone): the build-plan halves of `test_docs.py::test_unverified_lists_exactly_the_pinned_ids` (`docs/unverified.md`
is the U-id list) and `test_speckit_contract.py::test_the_floor_is_declared_the_same_everywhere` (the manifest is the one source), the
plan pins of `test_release_docs.py::test_the_final_review_findings_stay_fixed`, and the scrub exemption of those files in
`test_scrub_check.py` (now every tracked file is scanned). Provenance comments in `tests/fakejira.py`, `tests/test_fakejira.py` and
`tests/test_adf.py` now cite the tracked fixtures and the row-numbered tests instead.

## 3. Deferred to 0.2

| Feature | Fork tests waiting for it | Ledger ruling |
|---|---|---|
| A3b: strict `--phase done` cross-check in CI | none ported; behaviour only | Ruling (ultra-lite cut): A3b deferred to 0.2, documented as a known limit |
| A4b: approval-request comment behind X2, review-review re-check at `pr create` | approvals request-comment cases (section 2) | Ruling (ultra-lite cut): A4b deferred to 0.2 |
| `skj strict override` command | `test_override_refuses_without_tty` and 5 more | L5b: new task; H1a reworded strict findings because the command does not exist in 0.1 (hand-written override decision lines are honoured) |
| Single-item write verbs (`create`, `update`, `comment`, `link`, `attach`), raw `query` | none listed | K9 (CHANGELOG.md, Not in 0.1) |
| Attachment download | none listed | K9 (CHANGELOG.md, Not in 0.1); U6 in `docs/unverified.md` |
| Company-managed projects | none listed | K1 (CHANGELOG.md, Not in 0.1); U1 in `docs/unverified.md` |
| R6b: status run awareness and `--catch-up` | about 27 status tests (paused/failed/running notes, run-id and cap tests, shipped step ids) | candidate task from R6; a scheduling ruling is not recorded: unknown |
| `questions --sync` due / `unattended.assume_after_hours` | `test_wait_due_counts_with_server_time` | A2 and A4: new-task candidate (needs a Jira server-time design) |

## 4. Skipped in this repo

Every skip site in `tests/*.py`, as the AST walk in `tests/test_port_notes.py` finds it: any attribute named `skip`, `skipif`,
`importorskip`, `skipIf`, `skipUnless` or `skipTest`, whatever its root and wherever it sits (a call, a decorator, `marks=`, or an
expression such as `(pytest.fail if ambient.CI else pytest.skip)(...)`). Keyword = the longest string literal in the nearest
enclosing call (its reason). Sites = call sites in the file (a `needs_timer = pytest.mark.skipif(...)` marker counts once, however many
tests it marks).

| File | Keyword | Sites | Condition | Why |
|---|---|---|---|---|
| `tests/test_approvals.py` | `needs signal.setitimer` | 5 | `signal` has no `setitimer` | timing bound needs SIGALRM (POSIX only) |
| `tests/test_attachments.py` | `hardlinks unsupported here` | 1 | `os.link` raises OSError | the hard-link refusal cannot be set up |
| `tests/test_attachments.py` | `no /dev/null` | 1 | `/dev/null` absent | the device refusal cannot be set up |
| `tests/test_attachments.py` | `no setitimer` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_catalog.py` | `the disk under the repository is case-sensitive` | 1 | the repository path in swapped letter case does not exist | the DF3 m1 case-variant `--out` refusal needs a case-insensitive disk (macOS APFS) |
| `tests/test_config.py` | `no` | 1 | the parametrized device (`/dev/zero`, `/dev/null`) is absent | the device refusal cannot be set up |
| `tests/test_config.py` | `root reads a mode-000 file` | 1 | running as root | root can read a mode-000 file, so unreadable cannot be shown |
| `tests/test_config.py` | `signal.setitimer is not available on this platform` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_decisions.py` | `needs signal.setitimer to cut a hung call off` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_doctor.py` | `no README.md yet (installed by task C2)` | 1 | `README.md` absent | README exists since C2, so this no longer fires |
| `tests/test_dod.py` | `no setitimer` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_done.py` | `no setitimer` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_gate.py` | `signal.setitimer is not available on this platform` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_gate.py` | `threading.stack_size is not available` | 1 | no `threading.stack_size` | the parser stack-bomb test runs the parse in a big-stack thread |
| `tests/test_kits.py` | `case-insensitive file system: one file, no twins` | 1 | the file system folds case (default macOS) | case twins cannot exist in the working tree |
| `tests/test_manifest.py` | `the specify CLI is not installed` | 1 | no `specify` on PATH or in `~/.local/bin`; when `ambient.CI` (the run's CI flag) is set the test fails instead of skipping | the real-engine tests need the installed Spec Kit CLI |
| `tests/test_newest_posting.py` | `needs signal.setitimer` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_pin_release_sha.py` | `not a git checkout: the tracked files cannot be listed` | 1 | no `.git` at the repository root | `git ls-files` cannot be read, so the repository's own install lines cannot be listed |
| `tests/test_pr_link.py` | `no setitimer` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_prs.py` | `no setitimer on this platform` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_push.py` | `no setitimer on this platform` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_questions.py` | `no setitimer on this platform` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_release_docs.py` | `not a git checkout: the no-tag fact cannot be read` | 1 | no `.git` at the repository root | `git tag -l` cannot be read, so the no-tag fact (Z3-AC2) cannot be shown |
| `tests/test_release_docs.py` | `not a git checkout: the shipped file list cannot be read` | 1 | no `.git` at the repository root | `git ls-files` cannot be read, so the shipped-links check (D-030) cannot run |
| `tests/test_release_docs.py` | `not a git checkout: the tracked files cannot be listed` | 1 | no `.git` at the repository root | `git ls-files` cannot be read, so the no-placeholder-left check (REL1) cannot run |
| `tests/test_scrub_check.py` | `no denylist here: the tracked-name check reads SCRUB_DENYLIST or the untracked .scrub-denylist` | 1 | neither `SCRUB_DENYLIST` nor `.scrub-denylist` holds a term (the default: the list is the owner's and never tracked; CI gives it only to the history step) | the names to look for are never spelled in a tracked file, so without the list there is nothing to check |
| `tests/test_stack_linear.py` | `signal.setitimer is not available on this platform` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_status.py` | `signal.setitimer is not available on this platform` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_strict_linear.py` | `no FIFOs here` | 1 | no `os.mkfifo` | the FIFO-never-hangs test cannot be set up |
| `tests/test_strict_linear.py` | `signal.setitimer is not available on this platform` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_trust_crosscheck.py` | `signal.setitimer is not available on this platform` | 1 | no `setitimer` | timing bound needs SIGALRM |
| `tests/test_workflow.py` | `the specify CLI is not installed` | 1 | no `specify` on PATH or in `~/.local/bin`; when `ambient.CI` (the run's CI flag) is set the test fails instead of skipping | the real-engine tests need the installed Spec Kit CLI |
| `tests/test_x2_no_write_without_apply.py` | `%s not built until %s` | 1 | a verb of the plan list is absent from the parser (its owner task is then read from `NOT_BUILT`) | `NOT_BUILT` is empty at 5c63a9a, so this no longer fires |

The guard does not see `from pytest import ...` aliases, xfail marks, skips in subdirectories of `tests/` or markers added by a
conftest `add_marker`; none exists today.

On the local macOS run at c9095d1 the suite reports 2 skipped: the one the PN report names, and the client-handle check that needs the untracked denylist file.

## 5. Known 0.2 backlog

Not built. Taken from the ledger's explicit "0.2 backlog" rulings and from the minors it sent to H1 that H1a and H1b did not take
(H1a and H1b briefs say "nothing else"). Whether a later fix round closed any of the H1 minors was not re-checked: unknown.

Client, secrets and reads
- S4c: secrets can sit in frames or chained context on the env path, in `_from_file` decode errors and in `store_file` validators; `os.close` after an `io.open` failure may raise EBADF; pin sweep hex strictness; the `<NULL>` service message wording.
- S6b: client docstring over-claims `capture_locals`; one shared read set for `_is_read`/`_is_write`/the fake; 3xx on a write should be WriteUncertain; WriteUncertain lacks status and headers; `in_ci` strips whitespace; the fake clock accepts a naive datetime; AdfUnknown message formats the raw node type.
- R1: builders should return a `Jql` type that `search_jql` requires (one JQL grammar); duplicate ids across pages kept; non-dict `fields` should be a ReadError; private helper coupling (`keys._ok`).
- R2: `comments.py` imports private `issues._id_ok`; CLI shows `edited=no` when both authors are None; `default_client` untested; `fetch` docstring should say "call issues.get first".
- W2: `_clean` keeps combining marks, variation selectors and invisible fillers; absent `isAvailable` counts as available while absent `hasScreen`/`isConditional` is malformed.

Ledger, decisions and questions
- R4: spurious AC-n lines from code fences or nested-list markers; first-vs-last section 2 untested; `seed()` does not enforce `cfg.project`; refresh not byte for byte for CRLF ledgers; PermissionError traceback in `cmd_dod`; heading inside a panel gives a misleading message; heading level unchecked; `cmd_dod` runs `git rev-parse` twice; heading match is case-sensitive (owner call).
- L1: dead ported code in decisions (`stamp_used`, `apply_stamp`, `effective` and others); `scrub_emails` copied privately.
- L2: `_me` ignores `active`; `assumed()` prefix rule inherited; a second ledger reader beside `decisions._read`.
- L1b: SIGALRM cannot interrupt one long C call; `_timed` swallows ValueError; `_next_number` lambda cosmetic.
- H1a ruling: allow ZWJ/ZWNJ inside words; `_next_number` and `--supersedes D01`; duplicated locale lists; `gh_env` passes any `LC_*`; `CLAUDE_CONFIG_DIR` and `AWS_SESSION_TOKEN` in the resume env; mermaid edge-label quoting unpinned.
- A2: an approver editing an unrelated comment into an unknown node blocks all targets (document); exit 0 when newer-tampered applied nothing; `E_SYNC_TERMINAL` names `--resolve` for questions too; a deleted or restricted reject is hidden.
- W4: `comments.fetch` could report completeness itself; doubled or full-width at-sign mentions are not scrubbed (mention behaviour unverified).

Gate, strict, stack and kits
- L3: scan budget worst case 3.3 s; dead ModuleNotFoundError branch and stale docstring in `config.py`; no `safe.directory`; a bare test id matches any test file naming it; non-UTF-8 files skipped silently.
- L4: `force-app/` roots bypass `MAX_CANDIDATES`; `_was_approved` fails open on an unreadable approved profile; negative-number digit boundary; dirty-tree test should use the git env fixture.
- R3b: `_git_capped` can block past `GIT_TIMEOUT` when a git child outlives git, no test for the timer path, git not reaped when the read raises; `_int` counts the sign; `stack._read_config` lstat-then-read race.
- H1b ruling: `stack.py` Ctrl-C between prompt prints.
- W1: `allow_abbrev` enforcement is a post-build tree walk; 5 of 24 abbreviation cases guard nothing; the scan is blind to write flags with other names.

Push, PR, link, attachments and done
- W3: a distinct exit code when refused or deferred operations remain (owner call); "N transition(s) refused" wording for a summary refusal; `create_subtask` does not check the T### prefix itself; redundant length guards.
- P1: no wheel-contents test for templates; the body smart-commit scan refuses harmless `#tag`/`C#` text.
- P2: GH_HOST accepts loopback names; a retitled PR gets a second comment; a 4xx on the link posts no comment (owner calls).
- P3: the SVG attribute check is the slow part on huge files; fail-closed false positives on some attributes (document); keep one linear timing test small so a regression fails rather than hangs.
- S3c: exit code 2 for refusals vs 1 elsewhere (document); wrong message on an `--emit` OSError; missing answer file message wording.

Status, approvals, setup and doctor
- R6: no origin remote gives a bare `git remote set-head` failure; detached HEAD suggests `skj pr create`; stale main suggests config instead of pull; "landed" judged on the branch's own ledger heading text.
- A1: ADF atoms (inlineCard url, emoji shortName) render raw attributes; `approvals.excluded` only sees readable bodies starting with the plain marker; `"[?" in md` docstring.
- R3: the Client-construction scan only sees direct `Client(...)` calls; bad `--base` message wording; setup writes the config in cwd and not atomically; doctor reads private `config._PIN_REFUSAL`; Ctrl-C in the setup prompt prints a traceback.
- R5: case-insensitive file system branch collisions (doc note); `KEY_IN_TEXT_RE` boundary is ASCII only; private `config._git` use.
- C2: unused config keys `labels`, `story_type`, `transitions.active`, `unattended.*` (remove or wire).

CI and tests
- H1b ruling: move the one `KEY=$key` line from `GITHUB_ENV` to `GITHUB_OUTPUT`; step-level `env`/`shell`/`working-directory` not checked by the secret-step rule; a misleading tag on benign edits; the `_w5_pipeline` KeyError message.
- R4b: the dod test oracle uses constants from the module under test; mid-file imports in `test_dod.py`.
- L5a: exit 3 for an invalid origin/HEAD config also hits advisory projects (note for the changelog).
