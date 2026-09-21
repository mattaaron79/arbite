---
id: tic-e9ed
title: Cascade ticket lifecycle through file ownership and receipts
status: closed
type: feature
tier: high
domain: io
epic: shared-directory-coordination
priority: null
tags:
- lifecycle
- claims
- cascade
assignee: deepseek.code.011
depends_on:
- tic-74e2
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:33'
updated: '2026-09-21T14:53:37'
closed: '2026-09-21T14:53:37'
---

## Description
Planning key: C10
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (CL7, LC1, LC2, LC3, LC4)

## Outcome and scope
Wire close, submit, accept, release, block, shelve, reopen, delete and forced takeover so each ends or preserves attempts correctly, finalizes receipts, and releases file claims. No observer may mutate successfully under an old token after a close succeeds. Delete is refused while claims or attempts are live, and generic setters route through these transitions or refuse.

## Acceptance criteria
- Close releases every active claim of the attempt and retains the receipt manifest.
- Block and shelve end the attempt, release claims, and leave partial work visible for the next worker to re-read.
- Reopen resurrects nothing and creates a fresh attempt on the next claim.
- Takeover revokes the previous generation, releases its claims and states that partial bytes remain.
- Scenarios CL7, LC1, LC2, LC3, LC4 pass exactly as written in the examples doc.

## Validation
Close racing a write, release and takeover followed by an old-token write attempt, delete with live claims, reopen history, both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T14:35:46 deepseek.code.011: Implementation (C10 cascade) in place: end_attempt now commits the attempt's terminal state and every claim it holds (with release.file events) in one coordination transaction under the store operation lock; close/submit/accept/reconcile+end+release+write; takeover revokes and releases in the same commit; delete refuses live attempts/claims (LC3); set-status/set-assignee guards extended to review; reopen clears the assignee so the LC4 hint is runnable. Scenario tests CL7/LC1-LC4 added to tests/test_lifecycle_examples.py with tests/cascade_state.py fixtures - all pass; tests/test_cli.py, test_write_examples.py, test_coordination_lifecycle.py all pass (only the known RC1 flake failed once).

- 2026-09-21T14:53:34 deepseek.code.011: C10 implementation, decisions and validation

DECISIONS
1. end_attempt() now commits the attempt's terminal state AND every claim it still holds (release.file events, release_reason naming the attempt) in ONE coordination transaction, under the store's operation lock. Module docstring of ended plus release: an attempt that is over while its paths stay reserved is exactly doctor's orphaned_claim.
2. takeover (_start_attempt with revoke_reason) releases the revoked attempt's claims inside the same commit and reports generation + claims released + remaining bytes (CL7). The forced claim holds the operation lock across its ticket exchange and that revocation, so a mid-flight file operation cannot apply bytes under the generation being revoked.
3. close/submit/accept are one cascade in lifecycle: reconcile pending operations, end the attempt (state finished), release claims, then write the ticket; close holds the lock for the whole sequence, which is what makes 'no observer mutates under an old token after close succeeds' true (a file op either finishes first - receipt kept - or meets a non-in_progress ticket + non-current attempt and is refused with nothing written; WR5/RC2 unchanged).
4. close reports the receipt manifest (count of finalized receipts for the ticket + the 'arbite changes <id>' command tic-7c42/C11 delivers) and leaves it intact; evidence is never pruned.
5. delete refuses while an attempt or claims are live (LC3) and the guard runs before the --force check; after a close the same delete succeeds, and the coordination history (released claims, receipts) is not cascaded away with the ticket.
6. set-status/set route review to 'arbite submit' now that submit owns that transition (LC5's closed entry untouched, word for word); unshelve and unblock run the same guard for the transition back to open, so no path returns a still-held ticket to the pool.
7. reopen clears the assignee as well as closed/blocked_by: it returns the ticket to the open pool, which is what makes LC4's own next: line ('arbite claim tic-fc9f --agent claude.haiku.003') runnable, and consistent with release/unshelve. This is the one deliberate behaviour change beyond the literal acceptance list.
8. Presentation: several released paths print as an aligned table under the heading (LC1); a single one prints inline (LC2). CL7's takeover ends with the new attempt line and no next: line, exactly as the frozen block does.

SCENARIOS (frozen transcripts, asserted byte-for-byte after normalisation, in tests/test_lifecycle_examples.py with fixtures in tests/cascade_state.py)
- CL7 forced takeover: PASS, no deviation. Fixture world = tic-cf9f in progress for att-91bd holding schema.py + sinks/base.py, schema.py written 5x through real 'arbite file write'.
- LC1 close releases claims: PASS, no deviation. The 5 receipts are 5 real writes; both claim rows and the manifest count come from the store.
- LC2 block: PASS, no deviation.
- LC3 delete refused: PASS, no deviation.
- LC4 reopen does not resurrect claims: PASS, no deviation.
- LC5, CL1-CL6, WR5: unchanged and still passing.
Extra assertions beyond the transcripts: claims records are released (not deleted) with the attempt named, no claim comes back active, doctor exits 0 with no problems after every cascade and after a takeover (the C04 orphaned_claim consequence is gone), receipts/artifacts survive closure, and the ticket's own next: hints are runnable.

VALIDATION
- python3 -m pytest -q  ->  901 passed, 3 skipped, 0 failed (baseline 885 + 16 new). One earlier full run failed only the known pre-existing RC1 flake (test_coordination_lifecycle.py::test_RC1_two_workers_claiming_one_ticket_produce_one_winner, a losing claim exiting 4 instead of 1); re-runs pass and it is tic-cf9f/tic-9b57's path, left to C15 as instructed.
- New multi-process/crash evidence in tests/test_coordination_lifecycle.py: close racing a write with two real processes and a starting gun (either the write landed first and its receipt survived the close, or the close won and the write exited 5 with no bytes changed); a real os._exit inside the cascade's commit at both commit_staged and commit_applied (the attempt and its claims land together or not at all, never an orphan); old-token writes refused after release/block/shelve/close.
- arbite doctor (pipx and checkout) in this repo: exit 0, 42 tickets, no problems.

OBSERVABLE BEHAVIOUR (reproducible by integration testing)
arbite close <tic> on a ticket with a live attempt now prints 'closed <tic> (<agent>) -> <path>', 'ended attempt <att> (N file claims released)' with a row per path ('last written by <tic>, sha256:...' when the bytes are exactly what one of the ticket's receipts recorded, 'unchanged since claim, sha256:...' otherwise), and 'receipts: N operations retained (arbite changes <tic>)'. A write under the closed attempt's own read token exits 5 with 'no bytes were changed'. 'arbite claim --force --reason ...' prints the revocation, the released paths, whether partial work remains, and the new attempt id; doctor then reports no orphaned claim. Verified end to end in a scratch project with the file sink.

EXISTING TESTS UPDATED (1)
tests/test_recovery_journal.py::test_RC2_a_close_racing_a_write_changes_nothing read scene.attempt (the active attempt) after the close; the close now ends the attempt, so it reads it before and additionally asserts the attempt is ended and the claims released. Same refusal, same wording, stronger assertions.

DELIBERATELY DEFERRED / LIMITATIONS
- 'arbite changes' (tic-7c42/C11) does not exist yet; LC1's frozen line requires printing it, as claims.py's busy hint already does.
- receipt/changes views (C11), migrations/export (C12), passthrough (C13/C14), docs (C15) untouched.
- The SQLite coordination backend still refuses mutations before changing any byte (it stores no artifact content), so the cascade is exercised there for attempts/claims/events but not for file writes; the file sink is not weakened to match it.
- The stale/pending journal consequences of a process killed inside the cascade's commit are the file backend's existing replay behaviour (the next write finishes the unit); the lifecycle does not add recovery logic of its own.
- Attempt generations stay attempt-scoped, so a fresh attempt's first acquisition is generation 1 again (claims.py's rule); what makes an old token dead is that the claim now names another attempt.

- 2026-09-21T14:53:37 system: Submitted; closed (review disabled).
