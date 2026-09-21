---
id: tic-74e2
title: Expose tracked creation, removal and rename
status: closed
type: feature
tier: medium
domain: ui
epic: shared-directory-coordination
priority: null
tags:
- file-proxy
- remove
- rename
assignee: deepseek.code.009
depends_on:
- tic-60c7
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:33'
updated: '2026-09-21T13:47:59'
closed: '2026-09-21T13:47:59'
---

## Description
Planning key: C08
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (RN1, RN2, RN3, RN4, FC4)

## Outcome and scope
Implement arbite file remove and arbite file rename so agents do not fall back to shell writes. Rename claims both source and destination, requires absence or an explicit destination version, and records both paths. Remove keeps the bytes in a receipt. Parent creation is constrained to safe in-root directories and recursive directory deletion is refused.

## Acceptance criteria
- Rename moves ownership to the destination and releases the source path.
- An existing destination requires an explicit expected version.
- Remove and rename round-trip through receipts, including binary content.
- Refusals name the manual alternative rather than leaving the agent to improvise.
- Scenarios RN1, RN2, RN3, RN4, FC4 pass exactly as written in the examples doc.

## Validation
Rename across directories, destination collisions with and without an expected version, source or destination busy, removal evidence, directory refusal, both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T13:25:54 deepseek.code.009: Milestone 1: the engine's remove/rename were already in place (C05); this slice adds the command layer. New module coordination/moves.py (FileMoves.remove/rename), CLI `file remove`/`file rename`, shared helpers hoisted to module level in writes.py (mutation_target, active_claim, next_observation_id, evidence, version_facts, require_read_token, require_mutation_context, record_claim_probe) and mutations.py (spent_message/already_spent). Commands judging: remove refuses a directory BEFORE ticket/attempt (RN4's frozen command names neither), so --ticket/--attempt/-read-token are optional in argparse and refused in the app layer with exit 1. Rename requires both paths claimed at one generation (engine rule), records the destination's stated version as a probe observation under the claim, replaces an existing destination only with --expect-dest (full digest or the 12-char prefix a report prints, so the printed hint is runnable), and moves ownership: source claim released with reason 'renamed to <dest>' + observed_version=absent, destination claim refreshed to the moved digest, both in one commit inside the same operation lock as the apply. A remove keeps the claim active and records observed_version=absent (the FC4 absent-path state, so the same attempt can create again from a fresh probe). Both new commands run under store.operation_lock for the whole check+apply+claim step. Tests: tests/moves_state.py fixtures + tests/test_move_examples.py -- RN1, RN2, RN3, RN4 and FC4 pass byte-for-byte (5 passed). Existing suite re-run for the refactor: 102 passed on tests/test_write_examples.py tests/test_file_writes.py tests/test_read_examples.py tests/test_file_reads.py (one first-run flake in the two-process token race, a file-sink lock-timeout ordering under load; passes in isolation and on re-run).

- 2026-09-21T13:42:59 deepseek.code.009: Final: implementation, validation, scenario status, observability and limits.

WHAT LANDED (paths)
- New src/arbite/coordination/moves.py: FileMoves.remove / FileMoves.rename -- the command layer over the engine C05 already built (kinds remove/rename, one-syscall apply, receipts with both versions).
- src/arbite/cli.py: `file remove` and `file rename` subparsers + cmd_file_remove/cmd_file_rename + the `_moves` accessor; `_mutation_context` now delegates to coordination.writes.require_mutation_context; the `file` and `file write` help text updated (the old text said renames/removes "are not here yet", which this slice made false).
- src/arbite/coordination/writes.py: command helpers hoisted to module level so moves.py reuses them rather than copying them -- mutation_target, active_claim, next_observation_id, evidence, version_facts (the private _version_facts, renamed), require_read_token, require_mutation_context, record_claim_probe. Behaviour unchanged; C07's suites pass untouched.
- src/arbite/coordination/mutations.py: the WR3 spent-token refusal factored into spent_message()/already_spent() so a write, an edit, a removal and a rename answer a replay identically.
- Tests: tests/moves_state.py (RN fixtures), tests/test_move_examples.py (RN1-RN4 + FC4), tests/test_file_moves.py (guarantees, both sinks, multi-process); tests/examples.py gained with_token.
- No schema revision bump: no record gained a field. COORDINATION_SCHEMA_REVISION stays 2 (OperationReceipt already carried kinds rename/remove), so C12/tic-008f inherits no new migration obligation from this slice.

DECISIONS A REVIEWER SHOULD SEE
1. Check order for removal: the *path* is judged first, so the directory refusal precedes the ticket, attempt and token checks. That is forced by the frozen RN4 block, whose command names neither flag; consequence: --ticket/--attempt/--read-token are optional in argparse for both new commands and refused in the application layer with exit 1 (never argparse's exit 2, which means "nothing matched").
2. A rename needs both paths claimed at one generation (the engine's rule from C05: one token authorises the whole move). The command layer pre-checks ownership, generation and the destination version so each refusal names a repair that works: claim both paths in ONE acquisition (a destination claimed separately is the mistake the hint prevents); release the destination, re-claim both, re-read the source (generation mismatch); read the destination again (stale version).
3. The destination's stated version is recorded as a probe observation under the claim (record_claim_probe -- C07's creation probe one step on), so the engine checks it against the bytes exactly as it checks a read token. --expect-dest accepts the full sha256 digest or the 12-character prefix every report prints, because RN2's own hint prints the short form and a hint must be runnable; the value that reaches the engine is always the exact digest. A stated version that no longer matches the destination is stale (5, "no bytes were changed", read-the-destination repair); stating a version for a free name is an error (1, drop the flag); a malformed value is bad input (1).
4. Ownership bookkeeping, one commit, inside the operation's critical section (the store's operation lock now wraps check+apply+bookkeeping for both commands):
   - rename: the source's claim is released (reason "renamed to <dest>", observed_version -> absent, release.file event carrying generation, the moved digest and moved_to); the destination's claim -- which the caller already held, since a rename onto an unheld path is refused -- records the digest it now owns. The destination keeps the generation its acquisition gave it, which is what RN1 prints.
   - remove: the claim stays active and records absent. That is deliberately the FC4 absent-path state: a creation is authorised from a fresh probe, so the same attempt can re-create the path (asserted), and `file claims` never reports bytes a path does not hold.
   - If that commit cannot land after the bytes moved, the error is NOT a "nothing changed" refusal: it says the bytes moved, names the receipt, and names the `file release` that finishes the record.
5. A replayed removal whose path is gone answers the spent-token refusal (5) naming the operation that spent the token when the token names that path, and "no such path" otherwise -- so a retry is never told the file simply does not exist.
6. No directory is created or deleted: a destination whose parent is missing is refused by the path rules (probe), so a caller creates the directory (mkdir) and the proxy performs the move; RN4's text names `rmdir` as the manual alternative, which the frozen block requires.
7. Both-sink difference preserved and asserted, not papered over: the SQLite coordination backend still stores no artifact content, so remove/rename there refuse before any byte changes (ticket-7c42 named in the message); the file sink was not weakened.

VALIDATION (exact commands and outcomes)
- `PYTHONPATH=src python3 -m pytest -q` -> 852 passed, 3 skipped (baseline 823/3; +29 new tests, none removed or skipped).
- `PYTHONPATH=src python3 -m pytest -q tests/test_move_examples.py tests/test_file_moves.py` -> 29 passed.
- `arbite doctor` -> exit 0, "checked 42 tickets: no problems found"; `git status` shows only this ticket's move plus the files above (no sibling ticket touched).
- Manual integration run in a scratch project with PYTHONPATH=src python3 -m arbite.cli: claim both paths in one acquisition -> file read -> file rename prints RN1's three lines; rename onto an existing path prints RN2's refusal; file remove prints RN3's line; file remove <directory> prints RN4's refusal; `file claims --all` and `events --tail` show the release and the move.

SCENARIO STATUS
- RN1, RN2, RN3, RN4: pass exactly as written -- no deviation, no abridgement, asserted by the harness reading .arbite/planning/interaction-examples.md (command, exit code, stream, rows). The facts behind each transcript are separately asserted against the store: receipt before/after maps and artifacts, claim state/generation/observed version, the spent token, and the bytes on disk.
- FC4: passes exactly as written, and is additionally asserted in a project where a rename and a removal have already run, so "the creation case still passes with the new commands present" is a checked fact rather than an assumption.
- No transcript in the planning document was edited.

EXISTING TESTS UPDATED (justification)
1. tests/test_file_writes.py::test_help_text_names_only_commands_that_exist -- `file remove` and `file rename` moved from the "not in this build yet" list to the "a hint may name these" list. The rule is unchanged; the slice landed both commands, so the old expectation became false.
2. tests/test_file_writes.py::test_two_processes_with_one_token_race_and_exactly_one_wins -- fixture fix only. Both processes shared one scratch payload and a successful write consumes the payload it read, so the loser could stop with "no payload named 'base.py'" (exit 1) before reaching the token check. Verified pre-existing, not caused here: `git stash push -- src tests/helpers.py`, three runs of the test at HEAD -> one failure, then the stash was popped (nothing else ran at f8a3111). Each racer now gets its own payload copy; every assertion is unchanged ([0, 5], one payload on disk, one receipt, token spent), and the test passed 5/5 consecutive runs after the fix.
3. tests/examples.py gained with_token (additive; Scenario.with_ticket and every existing helper are untouched).

OBSERVABLE BY INTEGRATION TESTING
- `arbite file remove src/x.py --ticket T --attempt A --read-token R` deletes the file; `.arbite/coordination/artifacts/<sha256>` holds the exact bytes that were deleted and `.arbite/coordination/receipts/<op-id>.json` records before=<digest>, after="absent" (readable by hand today; `arbite receipt` is C11's). `arbite file claims` then shows the path held with `absent`, and `arbite file write src/x.py --input payload` with no read token re-creates it from a probe -- the remove-then-create workflow.
- `arbite file claim src/a.py src/b.py` then `arbite file rename src/a.py src/b.py --read-token R` moves the bytes; `arbite file claims --all` shows a.py released ("renamed to src/b.py") and b.py holding the moved digest; `arbite events --tail` shows rename.intent, rename.file, release.file. Renaming onto an existing path refuses with the version to name; adding `--expect-dest <that version>` replaces it and keeps the replaced bytes as an artifact.
- Refusals print error:/busy:/stale_read: on stderr with exit 1/4/5 and a runnable `next:` line; a refusal never changes bytes and never spends the presented token.
- Observable effects are all local state (receipts, artifacts, claim records, events) plus the tree; nothing is uploaded and no daemon runs.

REMAINING LIMITATIONS (deliberate, named)
- Receipt and change *views* are tic-7c42/C11; this slice only stores the evidence correctly, and no help text names a command that does not exist.
- Scratch transport is untroubled: remove/rename take no payload; `scratch list|clear` is tic-95c0/C09.
- No lifecycle cascade: neither command releases the ticket's other claims; close/submit/accept behaviour is tic-e9ed/C10 (a closed ticket already freezes mutations -- tested, exit 5).
- A rename is not reversible automatically: both versions are in the receipt, but re-applying them is C11's business.
- Renaming a path twice in a row needs a re-acquisition of both paths (the one-generation rule); the stale refusal names the sequence and a test proves it works.
- Directories are never renamed, removed or created; a move into a new directory needs the caller to create that directory first.
- SQLite coordination cannot store artifact content, so mutations there refuse before changing bytes (unchanged from C07; tic-7c42).

- 2026-09-21T13:47:58 deepseek.code.009: Final validation on the committed tree: PYTHONPATH=src python3 -m pytest -q -> 852 passed, 3 skipped (second consecutive full run, after the last docstring-only edit). arbite doctor -> exit 0, checked 42 tickets: no problems found. All frozen RN1-RN4 and FC4 blocks pass with no deviation from the transcripts in .arbite/planning/interaction-examples.md (nothing in the planning docs was edited). No schema revision change (still 2): no record gained a field.

- 2026-09-21T13:47:59 system: Submitted; closed (review disabled).
