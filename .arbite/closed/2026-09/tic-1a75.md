---
id: tic-1a75
title: Implement transactional coordination storage and durable events
status: closed
type: feature
tier: high
domain: io
epic: shared-directory-coordination
priority: null
tags:
- coordination
- storage
- events
assignee: deepseek.code.002
depends_on:
- tic-7918
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:32'
updated: '2026-09-21T09:24:46'
closed: '2026-09-21T09:24:46'
---

## Description
Planning key: C02
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (EV2, EV3, EV4, EV5, EV7)

## Outcome and scope
Implement record revision checks, store-local multi-record operations, and event append with a stable cursor, plus the arbite events read surface with --after, --tail and --include-reads. The file sink uses a recoverable journal and process serialisation; SQLite uses transactions. A small coarse operation lock is acceptable, and no lock may be held for a ticket duration. Read observations are a separate category from mutations.

## Acceptance criteria
- Concurrent updates cannot lose fields under unchanged status and assignee; revisions are explicit.
- Related state and events commit together or recover deterministically; retrying an operation id deduplicates.
- Process death leaves no permanent runtime lock, and durable file claims are unaffected.
- Both sinks produce equivalent outcomes, with no hidden SQLite requirement for the file sink.
- Events render one line per event with kind, subject, ticket, attempt, actor, time and outcome; --follow is refused.
- Scenarios EV2, EV3, EV4, EV5, EV7 pass exactly as written in the examples doc.

## Validation
Multiprocess conflicting revisions and events, process death around commit, rollback and idempotent retry, cursor pagination across restarts, both-sink conformance.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T09:14:57 deepseek.code.002: Progress: store layer + events surface implemented. Design: (1) records.py -- fixed the new_id docstring flagged in review (both backends DO replace, so no uniqueness guarantee is claimed) and added Event.subject/.result as reads of a documented payload convention (EVENT_SUBJECT_KEY/EVENT_RESULT_KEY) rather than new record fields, so the stored document shape and schema revision are untouched. (2) store.py -- CoordinationTransaction buffers writes, reads see the buffer first; materialise() runs inside the backend's serialisation and assigns ABSOLUTE revisions/cursors (so a journal replay is idempotent) and raises Stale before anything is written when expect_revision no longer holds; revision()/next_event_cursor()/find_record() added; commit_transaction()/revision()/next_event_cursor() are backend hooks; recover() stays a stub naming tic-b03b. put_workspace(workspace, txn=None) now commits the removal+new binding as one unit either way. (3) file_backend -- flock on coordination/lock (ephemeral: the OS drops it on process death, held for one commit only; bounded 5s wait then Busy/store_locked), commit journal written->applied->removed with removal as the commit point, revisions.json beside the documents (a record document must stay exactly the record, per the C01 migration test), cursor allocation from event FILENAMES (zero-padded), replay_commit_journal() at the head of every write path, and a pending_commit finding in record_problems so a reader is told rather than shown a half-applied unit. (4) sqlite_backend -- conn.isolation_level=None and an explicit BEGIN IMMEDIATE so the unit of work is exactly the statements wrapped (executescript ends an open transaction, so the DDL runs before BEGIN); new coordination_revisions table (COORDINATION_SCHEMA_VERSION 2, created by the same CREATE TABLE IF NOT EXISTS on first write, no ALTER); put_record/delete_record are now one-write transactions so document and counter always arrive together. (5) app/cli -- GuardedOperation.run runs its steps in one transaction (steps take (app, txn)); arbite events [--after|--tail] [--include-reads] [--json] with --follow refused on stdout/exit 1. (6) results.py to_json keeps a data-published next_actions when the outcome has none of its own, so the events view's resume command reaches JSON without to_text appending a duplicate next: line. Validation so far: tests/test_events_examples.py EV2/EV3/EV4/EV5/EV7 all pass byte-for-byte (23 tests across the two new events modules).

- 2026-09-21T09:24:46 deepseek.code.002: C02 final note (tic-1a75) -- implementation, evidence, scenarios, limits.

IMPLEMENTATION DECISIONS
1. Revisions live BESIDE a record, not inside it. The stored document must stay exactly the record (C01's migration test compares the on-disk JSON with to_dict()), so the file backend keeps coordination/revisions.json and SQLite gains a coordination_revisions table. `store.revision()` is explicit (0 for an absent record or one written before revisions existed); `transaction().replace_record(record, expect_revision=N)` checks it inside the backend's serialisation, so two writers that read revision N cannot both commit and the loser gets Stale with nothing written.
2. One commit path. A transaction buffers its writes, reads see the buffer first, and `materialise()` assigns ABSOLUTE revisions and cursors inside the backend's serialisation (which is what lets a journal replay run twice and land on the same state). File backend: flock on coordination/lock for one commit only, commit journal written -> applied -> removed, removal as the commit point; the next write replays a journal a dead process left (redo, not undo: no judgement about workspace bytes). SQLite: connection in autocommit mode with an explicit BEGIN IMMEDIATE, so the unit is exactly the statements wrapped (executescript ends an open transaction, so the DDL runs before BEGIN). put_record/delete_record are one-write transactions on both backends, so document and counter always arrive together.
3. Cursor. Appended events take the store's next cursor: the file backend reads it from the event FILENAMES (zero-padded, already in cursor order), SQLite from the stored events. `put_record` places an event at an explicit cursor (that is the import path, tic-008f) and cannot be overtaken.
4. Retry dedup. `transaction(operation_id=...)` is a no-op when that id already has a FINALISED receipt (pending means "started, not finished" and must run again -- that reconciliation is tic-b03b). `append_event(operation_id=..., kind=...)` returns the existing event instead of a second copy.
5. Reads are not isolated; they are told. A read takes no lock, so `record_problems()` reports a `pending_commit` finding for an outstanding journal (visible in doctor), and the next WRITE finishes it. The multi-record guarantee is therefore "commits together, or recovers deterministically".
6. `arbite events [--after C | --tail N] [--include-reads] [--json]`, --follow refused. The selection, rendering and cursor line live in the application layer (coordination/app.py), the CLI only parses and prints. Exit 2 for "nothing new since cursor N". The read filter is on `category`: "read" is the observation stream the flag exposes, while a served read that belongs to an operation is file activity (kind read.file, category file) -- which is what makes EV2/EV3's rows and EV5's target true at the same time (C06 emits these; this is the contract it should follow).
7. Two display rules the frozen transcripts forced, both documented in the code:
   a. The `(resume with --after C)` parenthetical appears on the job view (EV2/EV3) and not on the read-inclusive view (EV5) or when nothing was returned (EV4). `--include-reads` is the only difference between EV5 and EV3, and both must pass exactly.
   b. The subject column width is computed from the rows the command LAID OUT, not from the rows it prints: a tail lays out its own window (EV5, width 23) while a cursor resume lays out the whole selected stream (EV3 prints two rows at width 28, matching EV2).
8. `GuardedOperation.run` now runs an operation's steps in one transaction (steps take (app, txn)) and `store.put_workspace(workspace, txn=None)` commits the removal-plus-new-binding as one unit either way, so `arbite init` is the first genuinely transactional operation on both sinks. No event is appended by init; the slices that own lifecycle events append through `transaction().append_event(...)`.
9. `results.OperationResult.to_json()` no longer overwrites a `next_actions` an operation published in `data` when the outcome has no actions of its own: the events view renders its continuation inline in the `cursor:` line, and `to_text()` must not append a duplicate `next:` line.
10. errors.Busy/Stale take an optional `reason` (defaulting to the old behaviour) so a refusal's reason -- `store_locked`, `stale_revision` -- is explicit rather than set by hand.

VALIDATION (exact commands and observed results)
- `python3 -m pytest -q tests/test_events_examples.py` -> 6 passed: EV2, EV3 (transcript + the documented JSON poll shape), EV4, EV5, EV7 byte-for-byte against .arbite/planning/interaction-examples.md via tests/examples.py.
- `python3 -m pytest -q tests/test_events_cli.py` -> 16 passed: read filter changes the answer, bare command == --tail 20 (the number the --follow refusal suggests), a poll loops over every job event exactly once, JSON == text facts, exit 2 with the cursor echoed, refusals for --tail 0 / negative --after / --after with --tail, and both sinks printing the same stream.
- `python3 -m pytest -q tests/test_coordination_transactions.py` -> 30 passed (both backends): unit commits everything, raising body rolls back, stale expectation discards the whole unit INCLUDING its event, two writers cannot lose each other's field (the ticket's criterion), empty transaction, double commit refused, revisions explicit per record and cleared on delete, events deduplicated by operation id, retried operation id applies once, pending receipt is not deduplicated, append cursors continue across store instances, both backends reach the same observable state.
- `python3 -m pytest -q tests/test_coordination_durability.py` -> 9 passed, real processes (tests/coordination_worker.py):
  * 4 processes release from a barrier after all read revision 1 and race to replace the same claim -> exactly 1 applied, 3 stale, stored version == the winner's digest, revision moved once; same result on BOTH backends.
  * kill -9 (os._exit(9)) at the staged boundary: the journal is on disk, doctor/record_problems reports `pending_commit`, the crashed unit's claim+event are absent; the next write replays them TOGETHER (cursor 1), the pre-existing durable claim is byte-identical, the journal is gone, the next append gets cursor 3 (nothing reused or skipped), and the write that triggered the replay completed in < LOCK_TIMEOUT (no permanent lock).
  * kill -9 at the applied boundary: replay is idempotent -- one event, revision NOT bumped again, journal gone.
  * SQLite killed inside BEGIN IMMEDIATE: nothing applied, cursor not consumed, next append is cursor 1.
  * store lock held by another process: refusal with reason `store_locked` in under 2 seconds, nothing written; after the holder exits, a write succeeds with no cleanup step.
  * cursor pagination across four processes, read with the CLI (`events --after 2 --json`).
- `python3 -m pytest -q` -> 557 passed, 3 skipped, 1 FAILED: tests/test_config.py::test_the_repos_own_config_takes_the_default. This failure is PRE-EXISTING at 61dfaa0 (verified by stashing this work and running the file: same failure). Cause: `.arbite/project.yaml` is committed with `review: false` while that test asserts the repo config omits the key. `.arbite/project.yaml` is out of bounds for this ticket, so it is reported, not edited -- it needs the owner's decision (either the test or the config changes).
- `arbite doctor` -> exit 0, 41 tickets, no problems. `PYTHONPATH=src python3 -m arbite.cli events` in this checkout -> "no events recorded / cursor: 0", exit 2.

WHAT A REVIEWER CAN OBSERVE (integration testing)
- In a scratch project (sink: file or sqlite), `PYTHONPATH=src python3 -m arbite.cli init` now commits the layout and the binding as one transaction; then `arbite events` reports "no events recorded" (exit 2) and `arbite events --follow` is refused on stdout with exit 1, exactly as EV7 prints it.
- Seed events through the store (tests/event_stream.py shows how) and `events --tail N`, `--after C`, `--include-reads`, `--json` behave as the transcripts; `--after 34` when nothing is newer exits 2.
- Crash the store during a commit (kill a process at the staged boundary; the tests do it with a fault-injection hook, `store.crash_hook`) and `arbite doctor --json` reports a `pending_commit` problem until the next write to the store replays it -- then the doctor report is clean again and the interrupted claim+event are both present.
- There is no CLI surface yet for revisions or stale refusals: exit 5 becomes reachable when C03/C07 consume `transaction().replace_record`, and `arbite events` stays empty until the slices that emit events land (C03 onward). Nothing in the new code claims otherwise.

DEFERRED TO NAMED TICKETS
- tic-cf9f (C03): attempts, readiness/claim guarding and the lifecycle events that will use `transaction().append_event`.
- tic-9b57 (C04): canonical paths, exclusive claims, the current-state claim index.
- tic-b03b (C05): the file-operation intent journal, `store.recover()` (still a stub naming that ticket) and `doctor --fix`. My commit journal is a different thing, named differently and documented as such.
- tic-1c4f (C06): discovery and reads, including the read-observation events and the category split described above.
- tic-7c42/tic-008f (C11/C12): receipts view, artifact content, migrations; tic-008f should use `put_record` to place events at their existing cursors (supported and tested).

LIMITATIONS / RESIDUAL RISK
- The journal is written atomically and fsynced, but its DIRECTORY is not: the guarantee is about a process dying, not about the machine losing power mid-rename (documented in the file backend's docstring).
- SQLite's next cursor parses the stored events (O(events) per append; `arbite events` reads the whole stream anyway). A projection can replace it if a store grows large.
- Reads can observe a commit in flight; they are told via `pending_commit` rather than isolated from it.
- The non-POSIX lock path (msvcrt) is written but untested here; POSIX (flock) is what this checkout and the suite exercise.
- `next_event_cursor()` is documented as a hint, not a reservation; only `append_event` may allocate.
- 1 pre-existing test failure, unrelated to this slice, as described above.

- 2026-09-21T09:24:46 system: Submitted; closed (review disabled).
