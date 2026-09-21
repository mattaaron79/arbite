---
id: tic-008f
title: Add coordination migrations, export and integrity recovery
status: closed
type: feature
tier: high
domain: io
epic: shared-directory-coordination
priority: null
tags:
- coordination
- migration
- integrity
assignee: deepseek.code.013
depends_on:
- tic-7c42
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:33'
updated: '2026-09-21T16:19:43'
closed: '2026-09-21T16:19:43'
---

## Description
Planning key: C12
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (multi-record round trip)

## Outcome and scope
Extend migration, export and doctor to attempts, claims, receipts, artifacts and events. Preserve generations, actors and operation ids across a quiescent transfer, refine integrity checks per sink, and export a receipt summary suitable for a devlog before any pruning. Shared checks stay shared where they mean the same thing; storage-specific ones stay per sink.

## Acceptance criteria
- A file to SQLite to file round trip preserves attempts, claims, receipts and event cursors when quiescent.
- Active work prevents an unsafe store switch.
- Doctor detects claim without attempt, attempt on a closed ticket, orphaned claims, drifted note indexes and pending operations without guessing.
- No artifact garbage collection until retention and reference rules are designed, and disk growth is documented.
- Scenarios multi-record round trip pass exactly as written in the examples doc.

## Validation
Full round trip with live claims, deliberate integrity damage, refusal while work is active, cross-sink doctor parity, export summary reproducibility.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T16:19:39 deepseek.code.013: C12 implemented. Decisions, validation, limits.

DECISIONS
1. Coordination migration is an *exact copy of documents*, not a re-serialisation. Both backends already store the same JSON (`Record.to_dict()`), so `coordination/migrate.py` reads the source's own documents (`CoordinationStore.raw_documents`, new: unvalidated read implemented by both backends) and writes them into the target with `CoordinationTransaction.put_record(record, revision=...)` (new: a stated write counter, so a copy keeps the revision a read token means). Everything commits as ONE unit of work (file journal / SQL transaction), and the artifact bytes are written first, idempotently by digest, because storing content is a separate operation on both backends. `materialise` now treats a record deleted and rewritten in one transaction as starting from revision 0, which is what lets `--overwrite` restore the source's counters instead of refusing.
2. Schema revisions are carried through, never silently misread. `records.upgrade_document` applies `REVISION_UPGRADES` mechanically (rev 2 = `ReadObservation.spent_by: None`) and refuses a newer revision, a missing `schema_revision`, or a gap in the table by name; `from_dict` keeps refusing an older revision (a read never rewrites what it read) and its message now names `arbite migrate`. The migration uses `read_forward` for every document and reports "N record(s) written by an older arbite were upgraded".
3. What refuses a switch, and why. ACTIVE CLAIMS and ACTIVE ATTEMPTS in *either* store refuse it (exit 4, `busy:`, holders named, "no coordination records were copied and no tickets were moved") -- live ownership moved to another store would leave the work in one store and the ownership in another. A destination holding work of its own refuses unless `--overwrite` (a binding is replaced, not counted). Evidence that cannot be carried refuses UP FRONT: a version over `MAX_ARTIFACT_BYTES` (read at call time so the one rule both backends enforce is the one asked) and bytes already gone. PENDING OPERATIONS are *carried*, not refused: their bytes are on disk, the receipt is the only record of the intent, the target judges it exactly as the source would, and refusing would make a drifted store unmovable. `--prune` stays tickets-only: coordination state is never deleted anywhere (no retention policy yet).
4. Copy semantics preserved: the source is untouched (even `--prune` leaves its coordination records, documented in the CLI description), and the source's workspace binding travels with the records, because claims and attempts name the workspace they were created in -- re-deriving the binding during a *copy* would re-key the work instead of moving it. Recorded as a limitation rather than papered over.
5. Doctor refinements. New shared finding `attempt_on_closed_ticket` (active attempt whose ticket is closed), REPORTED AND NEVER REPAIRED: the attempt's `ended`/outcome were never recorded, so writing "ended now" would invent the history the plan forbids; both honest exits (`arbite release ... --agent ... --reason`, `arbite reopen ... --reason`) are named. Integrity is now assembled in one place: `CoordinationStore.record_problems(ticket_ids, closed_tickets, operation_findings)` = shared findings + `storage_problems(fix)` per backend (file: `pending_commit`, `orphan_revision_counters`; sqlite: `orphan_revision_rows`), and `cmd_doctor` asks the store instead of calling `findings` directly -- which also makes `pending_commit` reachable from `doctor` for the first time (it was only visible through `store.record_problems()`). Shared checks stay shared; the counters/journals are only where that storage keeps them.
6. Export: `arbite receipt --summary [--ticket T] [--json]` rather than a new command name (a new top-level command would break the README/guide checks that every parser command is documented, and README/guide belong to C15). One row per operation-path in log order: time, result, operation, kind, ticket/attempt, actor, path, both versions (short digests in text, full in JSON), then the result counts, the pending note, "evidence: N version(s) referenced ... the store holds M artifact record(s) (x KiB)" and "arbite never prunes evidence and has no retention policy yet, so the store grows with the work". No artefact garbage collection was invented; disk growth is documented in the output and the help text instead.
7. No new mutation kinds, no daemon, no passthrough, no retention policy; planning docs, project.yaml and AGENTS.md untouched.

VALIDATION
- `python3 -m pytest -q` -> 990 passed, 2 skipped, 0 failed (baseline 959+2; +31 new tests). No existing test needed changing. The known RC1 flake did not occur.
- New: tests/test_coordination_migration.py (round trip file->sqlite->file on real stores: documents, revisions, generations, actors, operation ids, cursors, event ids, verified artifact bytes, exit-0 doctor on both sinks; cross-sink parity of `events` and `receipt --summary`; refusal while live; refusal of a destination with work; --overwrite; --dry-run; revision-1 store refused by name then upgraded on both sinks; unreachable revision refused; size-limit and missing-evidence refusals; pending operation carried; summary stability/narrowing/forms).
- New: tests/test_coordination_integrity.py (attempt on a closed ticket reported on both sinks and NOT repaired by --fix; identical shared finding on both sinks; file orphan counters reported+fixed; sqlite orphan row reported+fixed; outstanding commit journal reported and not replayed by --fix; a drifted note index and a pending operation in one report, with --fix rebuilding the index from the body and leaving the drift untouched; doctor changes nothing without --fix).
- `PYTHONPATH=src python3 -m arbite.cli doctor` on this repo -> "checked 42 tickets: no problems found", exit 0 (healthy store). `arbite doctor` (pipx) -> exit 0 with 42 tickets. `PYTHONPATH=src python3 -m arbite.cli receipt --summary` on this repo -> exit 2, "no operations recorded" + next line (its store holds only legacy observations/events). `migrate --to sqlite --dry-run` here -> exit 0, "coordination: would copy 4 record(s)" (no live work in this store).

SCENARIO STATUS
The slice owns no single-command scenario; its target is the handoff's "attempts, events and artifacts survive a file to SQLite to file transfer while quiescent", proven above. DR1/DR2/DR3/DR4 and every other frozen block still pass exactly as written (their fixtures have no per-sink findings, so the report text is unchanged). No deviations from the frozen transcripts.

OBSERVABLE BY INTEGRATION TESTING
- `arbite migrate --to sqlite` on a project with claims/receipts/artifacts now prints a `coordination:` block (record counts, the binding that travelled, any upgrades/pending operations) and an `evidence:` line; `--dry-run` prints "would copy ... replacing ...".
- The same command with a live claim or attempt exits 4 with `busy:` on stderr, names each holder and the file/destination reason, and creates nothing; after `arbite release` the identical command succeeds.
- `arbite receipt --summary` (and `--ticket`, `--json`) prints the devlog export; exit 2 for a ticket with no operations, 1 for an unknown ticket.
- `arbite doctor` reports `orphan_revision_counters` (file) or `orphan_revision_rows` (sqlite) on hand-damaged bookkeeping and `--fix` drops them; `pending_commit` now appears for a leftover commit journal; `attempt_on_closed_ticket` appears for a live attempt on a closed ticket and survives `--fix` unchanged.

LIMITATIONS / DEFERRED
- No artifact retention or garbage collection by design: the summary is the pre-pruning record. Retention rules remain un-designed (a later ticket, not C13/C14/C15).
- A store written by an older arbite is *migrated* by copying it through another sink; there is no in-place upgrade command, and a record type nothing reads (e.g. a revision-1 observation in a store with no code path that reads observations) stays at its old revision until a migration copies it. Reads of that type refuse by name in the meantime.
- After a switch the target records the source's workspace binding (see decision 4), so `arbite workspace show`'s derived identity and the recorded binding can differ until `arbite init` re-records it; nothing in doctor flags this, deliberately, because flagging it would make a successful migration exit 3.
- `TransferPlan` reads the source (counts, live work, evidence); a migration is not a hot path, and it re-checks under the transfer itself.

- 2026-09-21T16:19:43 system: Submitted; closed (review disabled).
