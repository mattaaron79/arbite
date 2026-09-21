---
id: tic-b03b
title: Build the file-operation intent journal and recovery engine
status: closed
type: feature
tier: high
domain: io
epic: shared-directory-coordination
priority: null
tags:
- file-proxy
- journal
- recovery
assignee: deepseek.code.006
depends_on:
- tic-9b57
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:32'
updated: '2026-09-21T11:05:08'
closed: '2026-09-21T11:05:08'
---

## Description
Planning key: C05
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (DR1, DR2, RC2)

## Outcome and scope
Persist intent and before and after artifacts, stage bytes, verify claims and versions, apply the filesystem operation, then finalize the receipt. Use operation ids for retry deduplication. Detect incomplete intent on the next relevant operation and reconcile against recorded versions. Expose pending operations through doctor and repair only the unambiguous cases with --fix.

## Acceptance criteria
- Injected failures before and after each boundary recover honestly, including replacement followed by sink failure and a rename interrupted between paths.
- Bytes matching neither the recorded before nor after version are reported as drift with evidence preserved.
- No recovery daemon and no invented ownership release.
- Errors state whether bytes may already have changed and which operation to inspect or retry.
- Scenarios DR1, DR2, RC2 pass exactly as written in the examples doc.

## Validation
Crash injection at every boundary on both sinks, retry idempotence, drift fixtures, doctor --fix behaviour on repairable and unrepairable cases.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T11:00:50 deepseek.code.006: Implementation in place, suite green. Added src/arbite/coordination/recovery.py (OperationJudgement/COMPLETED/NOT_APPLIED/DRIFT/UNKNOWN, judge/settle/reconcile, doctor findings + repair) and src/arbite/coordination/mutations.py (FileMutations.apply: verify -> archive intent+evidence -> stage beside the target -> apply -> finalise, with BOUNDARIES as crash-hook seams). store.recover(root=None) implemented for both backends (delegates to recovery.reconcile), store.operation_lock() added (ephemeral mutex: file backend's flock, no-op for a store whose only shared state is the database). record_problems now renders through recovery.findings, in three ordered phases (live-but-unusable claims, unfinished operations, records naming something missing) and judges pending operations against the bytes. doctor (cli.py) merges the coordination findings, prints the scratch note, and --fix releases claims nobody can use plus finalises operations whose bytes are exactly one recorded version. Validation so far: python3 -m pytest -q -> 694 passed, 3 skipped, 0 failed (baseline 649+3; +45 tests, 1 existing test deliberately rewritten). PYTHONPATH=src python3 -m arbite.cli doctor in this repo -> exit 0. DR1/DR2 pass on both sinks apart from two documented cross-slice literals (arbite scratch clear --all is tic-95c0's; arbite receipt/arbite changes are tic-7c42's), which are printed only when those commands exist (cli.knows_command walks the parser).

- 2026-09-21T11:05:05 deepseek.code.006: Closing note: implementation decisions, validation, scenario status, observable behaviour, limitations.

IMPLEMENTATION DECISIONS
1. coordination/recovery.py (new) owns the judgement and the doctor findings. A pending receipt is judged per path against the bytes on disk, all-or-nothing: every path at the recorded after version -> COMPLETED (finalise succeeded, apply nothing); every path at the recorded before version -> NOT_APPLIED (finalise failed, discard the staged copy); anything else -> DRIFT (change nothing, preserve what can be preserved, report all three versions); a path arbite will not read (symlink, directory, no workspace root) -> UNKNOWN (report, change nothing). No version is ever guessed at. store.recover(root=None) = recovery.reconcile, implemented once for both backends (the root defaults to the store's own workspace binding). Findings render in three phases: a live claim nobody can use, an unfinished operation, then records naming something missing.
2. coordination/mutations.py (new) is the engine the write/edit (tic-60c7) and create/remove/rename (tic-74e2) commands will call: verify (ticket status, attempt currency, claim generation, read token, whole-file digest, all re-checked at use time inside store.operation_lock()) -> archive before/after bytes as artifacts + commit the pending receipt -> stage the bytes beside the target as .<name>.<op>.arbite-stage (atomic os.replace, same directory) -> apply -> finalise. Operation ids deduplicate retries: a receipt already finalised changes nothing, and a retry meeting its own pending receipt judges it first and either completes it (bytes already at the after version) or re-runs it from the top. Unsure leftovers are reconciled before this operation stages anything.
3. Crash seams: BOUNDARIES = intent_persisted, staged, before_replace, replaced, before_finalize, reached through a class-level crash_hook like the store's own C02 seam, so the durability tests kill real processes.
4. The SQLite backend cannot store artifact content yet, so a mutation is refused BEFORE anything changes, naming tic-7c42; recovery and reporting work on both sinks, and a drift's evidence is archived by the file backend while SQLite leaves the bytes exactly where they are (nothing is ever changed on drift).
5. store.operation_lock() is the ephemeral kind: the file backend's existing flock, so check-and-apply is one critical section against every other arbite process, released by the kernel on process death; the base class returns a null lock and states why (a store whose records are the only shared state needs none). Never conflated with a durable claim, and a claim held by a LIVE attempt is never released by a repair.
6. doctor's text prints the scratch note (the count-and-size fact, from the existing scratch_summary) and, per DR1/DR2/DR3, the note sits with the findings when there are any and closes a clean report. --fix releases a claim nobody can use (claim record + release.file event, compare-and-swapped on the claim's revision) and finalises an operation whose bytes are exactly one recorded version.
7. Two literals in my transcripts name commands that do not exist yet: 'arbite scratch clear --all' (tic-95c0) and 'arbite receipt'/'arbite changes' (tic-7c42). Nothing may name a capability it does not have, so cli.knows_command() asks the parser and the sentences print without those commands until their slices land -- at which point the frozen text comes back by itself, with no test edit (the tests accept either form). Everything else in DR1/DR2 is byte-exact, including the line breaks, which are part of the messages so they stay stable whatever length a digest prints at; doctor prints full digests, as the display rule says.
8. record_problems now renders through recovery.findings, which is why its orphaned/dangling claim wording changed (the claim id is no longer printed; the parenthetical uses the attempt's own outcome words, e.g. "ticket closed <ended>"). No test asserted the old detail text.

VALIDATION (exact commands, exact results)
- python3 -m pytest -q -> 694 passed, 3 skipped, 0 failed (baseline 649 passed, 3 skipped; +45 tests).
- python3 -m pytest tests/test_recovery_journal.py -q -> 31 passed. Includes the injected-failure matrix, each boundary crossed by a real killed process (exit 9): test_RC_a_kill_at_every_boundary_recovers_exactly_once[5 params] and test_a_retry_after_a_kill_completes_the_operation_exactly_once[5 params]. Per-boundary evidence: intent_persisted -> receipt pending, bytes still the before version, no stage file, no artifacts? (artifacts yes: evidence is written before the bytes change); staged and before_replace -> also a leftover stage file, which the recovery discards; replaced and before_finalize -> bytes are the after version with the receipt still pending, which the recovery finalises as succeeded exactly once (that is the "replacement followed by sink failure" case). A retry after each kill ends with exactly one applied operation: the receipt succeeded, the bytes at the after version, one event recording it, no stage file.
- Other journal tests: retry dedup (no bytes, no events), a token authorises only the version it observed, bytes moved since the read refused as stale, a path held by another attempt refused as busy, RC2's target, create/write/remove/rename round trips through the receipt, the SQLite evidence refusal (names tic-7c42, no bytes, no receipt, no stage), drift (bytes untouched, receipt still pending, evidence archived, retry refuses), rename killed before the replace (nothing moved, destination untouched), after it (complete), interrupted between its paths with a third party's bytes at one path (drift, both paths untouched), and both-sink recovery parity.
- python3 -m pytest tests/test_recovery_examples.py -q -> 16 passed: DR1 and DR2 on the file sink AND the sqlite sink, the DR1 JSON facts, the second --fix run, and a direct assertion that doctor names no command this arbite does not have.
- PYTHONPATH=src python3 -m arbite.cli doctor (this repo) -> exit 0, "checked 42 tickets: no problems found" plus the scratch note.
- Deliberately damaged store (tests/recovery_state.py): doctor -> 3 problems and exit 3 (orphaned_claim, pending_operation printing all three versions, claim_without_attempt); doctor --fix -> 2 fixed and 1 problem, exit 3, bytes and receipts unchanged; a second --fix -> 1 problem, 0 fixed.

SCENARIO STATUS
- DR1: passes exactly as written on both sinks, except the scratch-note sentence, whose guidance names 'arbite scratch clear --all' (tic-95c0). The fact line it must print IS printed.
- DR2: passes exactly as written on both sinks, except the scratch-note sentence (tic-95c0) and the not-fixed clause that names 'arbite receipt'/'arbite changes' (tic-7c42). Everything else matches, including the fixed lines, the counts and the exit code.
- RC2: its command (arbite file write) is tic-60c7's, so it is not runnable here; its target is asserted at the engine that command will call -- test_RC2_a_close_racing_a_write_changes_nothing: outcome 5 (Stale, reason stale_read), message "attempt att-XXXX generation N is no longer current (tic-cf9f closed HH:MM:SS)" + "no bytes were changed", and the file still holds the version it held before.

OBSERVABLE BEHAVIOUR (integration-testable)
- arbite doctor on a store with an unfinished file operation now reports it, and --fix finalises the unambiguous ones and releases claims whose attempt is over or absent, always leaving bytes and receipts alone; a clean store still exits 0 and reports the scratch area as a note only.
- An interrupted mutation leaves a recognisable hidden .NAME.OP.arbite-stage file beside the target until the next operation or doctor --fix discards it (drift keeps it as evidence).
- A mutation on the SQLite sink refuses with a message naming tic-7c42 and changes nothing.
- arbite events gains <kind>.intent, <kind>.file, recover.finalized and recover.drift events, each carrying the operation id.

EXISTING TESTS CHANGED DELIBERATELY
1. tests/test_coordination_store.py::test_transactions_are_real_and_the_recovery_hook_says_what_is_missing -> rewritten as ..._the_recovery_hook_is_real_and_a_store_with_nothing_pending_is_untouched. It asserted the NotImplementedError stub that THIS ticket implements; it now asserts the real behaviour (a store with nothing pending reconciles nothing and writes nothing), and the recovery-with-bytes cases live in test_recovery_journal.py.
2. tests/test_examples.py::dr4_project -> one of its 22 tickets is now tic-cf9f, the ticket its seeded attempt names. The transcript's facts are unchanged (22 tickets checked, no problems, exit 0); the fixture was internally inconsistent now that doctor judges the coordination records against the ticket set it just checked, and attempt_without_ticket is a finding a clean store cannot have.

DEFERRED / LIMITATIONS
- Reads and token consumption ('one token authorises one mutation', WR3) are tic-1c4f/tic-60c7; this engine verifies a token is of this path, this attempt, this generation and the version the caller claims, but consuming it needs a record field and a schema revision (tic-008f).
- The commands that mutate bytes are tic-60c7 (write/edit) and tic-74e2 (create/remove/rename).
- Artifact content on SQLite, artifact verification and retention are tic-7c42/tic-008f.
- The lifecycle cascade that ends an attempt when its ticket closes is tic-e9ed; today a closed ticket with a live attempt is refused by the status check, the same honest answer and the same words RC2 freezes.
- Scratch clearing ('arbite scratch clear') is tic-95c0: the note is printed, the command is not.

- 2026-09-21T11:05:08 system: Submitted; closed (review disabled).
