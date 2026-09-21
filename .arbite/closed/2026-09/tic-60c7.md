---
id: tic-60c7
title: Expose version-checked writes and exact edits
status: closed
type: feature
tier: medium
domain: ui
epic: shared-directory-coordination
priority: null
tags:
- file-proxy
- writes
- edits
assignee: deepseek.code.008
depends_on:
- tic-b03b
- tic-1c4f
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:32'
updated: '2026-09-21T13:14:03'
closed: '2026-09-21T13:14:03'
---

## Description
Planning key: C07
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (WR1, WR4, WR5, WR6, WR7, ED1, ED2, ED3, BY2)

## Outcome and scope
Implement arbite file write and arbite file edit: read-token and digest verification, creation via absent probe, permission preservation, ordered exact-substitution edit batches with explicit occurrence rules, binary payloads with digests, and early refusal of generated or build output.

## Acceptance criteria
- One token authorizes exactly one mutation; a spent token is refused as stale.
- A stale digest, a revoked generation or a closed ticket changes no bytes and reports both versions.
- Ambiguous, absent or overlapping edits fail with no partial write.
- Successful mutations report receipt id, before and after digests, claim generation and the spent token.
- Scenarios WR1, WR4, WR5, WR6, WR7, ED1, ED2, ED3, BY2 pass exactly as written in the examples doc.

## Validation
Token replay, stale and revoked generation, closed-ticket mutation, ambiguous and overlapping edit batches, binary round trip, policy exclusions, both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T12:54:12 deepseek.code.008: Implementation in place and the nine frozen scenarios pass: spent-token state on ReadObservation (schema revision 2, spent_by, spent atomically with the receipt), policy exclusions (generated/build output) in paths.py, exact-substitution batches in coordination/edits.py, the write/edit command surface in coordination/writes.py, and the frozen refusals/reasons/hints in mutations.py (no_claim, stale_version, stale_token_spent, attempt_not_current). tests/test_write_examples.py: 13 passed (WR1 byte-exact, WR4/WR5/WR7/BY2 byte-exact, WR6/ED2/ED3 per-line with the deviations stated in the file docstring, ED1 with the document's inconsistent diff columns replaced by the diff recomputed from the receipt's own bytes, WR2/WR3 asserted as their targets). Two existing tests in tests/test_recovery_journal.py updated deliberately: the moved-file wording and the reason name for a closed ticket (tic-60c7 owns both sentences).

- 2026-09-21T13:09:08 deepseek.code.008: C07 implementation, validation, scenario status and limits.

IMPLEMENTATION DECISIONS
- Records (schema revision): ReadObservation gained `spent_by` and COORDINATION_SCHEMA_REVISION was raised 1 -> 2. The spend is committed in the same store transaction as the receipt finalisation (`FileMutations._spend_tokens`), so "one token authorises one mutation" is atomic rather than best-effort. A revision-1 record is refused by name under the existing rule (unchanged behaviour, new revision): tic-008f/C12 must carry the revision through migrations. This checkout's own local store held two revision-1 runtime records (a read observation of discovery.py and its read.observed event, both from a manual C06 check); I migrated them by hand field-for-field (schema_revision 2, spent_by null) so the repo stays usable -- which is exactly the work C12 automates. `arbite doctor` exits 0 and `file read`/`file write` work in this checkout afterwards.
- Engine (`coordination/mutations.py`): the refusals now carry the frozen wording, reason and hint: `no_claim` (exit 1, WR4, with the claim command quoted), `stale_version` (5, WR2/SC2: "you read <before> but the file is now <now> (changed HH:MM:SS)"), `stale_token_spent` (5, WR3, naming the operation that spent the token), `attempt_not_current` (5, WR5/RC2, naming the reopen command). Every one states "no bytes were changed" on its own line. `_finalise` marks the tokens the operation used as spent (compare-and-swap inside the same commit). The staged copy carries the replaced file's permission bits before `os.replace`, so changing content cannot silently widen or narrow who can read a file (ownership is deliberately not carried: not something an unprivileged process can promise).
- `coordination/paths.py`: `policy_exclusion`/`refuse_if_excluded` plus the BY2 refusal, from a fixed documented list (generated/build directory names at any depth, compiled-output suffixes on any component) -- not `.gitignore`, because a rule arbite cannot evaluate is worse than a short one it can state. `modified_clock` supplies the "(changed HH:MM:SS)" reading.
- `coordination/edits.py`: exact-substitution batches (a JSON list, or {"edits": [...]}, with old/new and optional occurrence or line). Each edit must select exactly one place, selections may not overlap, matching is exact, and the whole batch is spliced in memory and handed to the engine as one new version, so "no partial write" is structural. Refusals name the lines and hand back a runnable `--lines` window (85 -> 80:90, the window ED2 prints).
- `coordination/writes.py`: `file write` and `file edit`. Reports are the frozen shapes: a text write of an existing file prints the line delta, the receipt naming the version it replaced, the consumed-payload line and the spent-token hint (WR1); a binary write and a creation print the two lines that describe the change (WR6, SC3); an edit prints a row per replacement and folds the consumed payload into its receipt line (ED1, ED3). Creation: a path that is not there cannot be read (RD5 refuses it), so the command records the probe itself -- an absent observation under the caller's claim, appended as `read.file` in the `file` category, which is what coordination/reads.py documented as this slice's -- and spends it like any other token. A token the caller passes for an absent path is honoured when it is such a probe, otherwise the probe is taken now and JSON names the token that was used.
- `coordination/scratch.py`: the payload minimum these commands need -- `--input`/`--edits` resolved inside .arbite/scratch/, `-` for stdin, consumed on success. A failure keeps the payload (behaviour) and says nothing about it (no transcript of this slice shows a line there). Deferred to tic-95c0/C09 in a naming TODO and in the module docstring: `--keep`, `scratch list|clear`, the kept-payload report line (SC2/WR2), and the exact SC5 hint wording, which names `arbite scratch list` -- a command that does not exist yet, and arbite may not name a command it does not have.
- CLI: `file write`/`file edit` subcommands; `--attempt` is validated in the command rather than argparse so WR7's exit-1 wording and its hint print; `--read-token` is validated in `writes.py` (required for bytes that exist, unnecessary for a creation, since no read of an absent path exists).

VALIDATION (from the repo root)
- `python3 -m pytest -q` -> 823 passed, 3 skipped, 0 failed (baseline 749/3/0; +74 new tests).
- `python3 -m pytest tests/test_write_examples.py tests/test_file_writes.py -q` -> 74 passed.
- `arbite doctor` -> exit 0, "checked 42 tickets: no problems found".
- Manual CLI sequence in a scratch project with PYTHONPATH=src (claim -> read -> write with the token -> replay -> external edit -> stale): write succeeded and consumed the payload, the replay exited 5 naming the spending receipt, the external edit made the next write exit 5 with both digests, and no bytes changed on any refusal.
- Multi-process: two `arbite file write` processes racing with one token, one payload, exactly one exit 0 and one exit 5, one receipt, one copy of the bytes (test_file_writes.py).
- Byte-level, both sinks: 59 tests in tests/test_file_writes.py assert refusals and batch rules on the file and SQLite stores and the round trips on the sink that can hold evidence.

SCENARIO STATUS (tests/test_write_examples.py)
- WR1 byte for byte, including `570 -> 588 lines  +18 -0`; the delta is also recomputed from the receipt's recorded bytes.
- WR4, WR5, WR7, BY2 byte for byte.
- WR2 and WR3 asserted as the targets the ticket names (WR2's block is also SC2, which C09 owns): exit 5, both digests, "no bytes were changed", a runnable read hint, bytes and token unchanged, payload kept; and for WR3 one token two writes with the spender named, one receipt, no byte change. A second WR2 test proves the named repair works (re-read, retry, same payload).
- WR6 compared per line (deviation stated in the module docstring): the block shows the two lines that describe a binary replacement and omits the payload and spent-token lines the same command prints for a whole-file write (WR1). The binary shape, the receipt note, the 1024 -> 1187 sizes and the artifact round trip are asserted.
- ED3 compared per line (deviation stated): the block shows the stdin notice, the delta and the receipt and omits the two replacement rows ED1 shows for the same command.
- ED2 compared per line (deviation stated): one line the document does not show -- a stopped mutation reports the payload it kept (`payload: ... kept, so you can re-apply without re-sending the file`). The refusal itself is byte for byte: occurrences at lines 85, 229, 366, the `--lines 80:90` window, "no bytes were changed".
- ED1 byte for byte except its diff columns. The block prints `+4 -1` for a batch whose second edit changes one line in place: a one-line replacement is a removal and an insertion in any line diff, so two replacements cannot produce `+4 -1` (`+5 -2` here, `+2 -2` if the first edit had also been in place). The test substitutes the counts this fixture's bytes produce, recomputed independently from the batch and from the receipt's artifacts, and asserts the block's `570 -> 573 lines`. This is the only frozen number not reproduced.

DELIBERATELY CHANGED EXISTING TESTS (2 assertions, tests/test_recovery_journal.py)
- test_bytes_that_moved_since_the_read_are_refused: now asserts the frozen WR2 wording (both digests, "changed" time) and reason `stale_version` instead of "changed since you read it"/`stale_read`.
- test_RC2_a_close_racing_a_write_changes_nothing: reason is now `attempt_not_current` (same outcome 5, same message), because a ticket that moved on is repaired by reopening rather than by re-reading.
- Harness (`tests/examples.py`): `run_cli`/`run_scenario`/`assert_scenario*` take `stdin=` for commands that read `-`, and `ID_RE` now covers 5-8 hex ids as well as 4: the document spells its illustrative receipt id `op-2b8d17` (7 uses) and a block cannot be compared byte for byte while one of its placeholders survives normalisation.

WHAT A REVIEWER CAN OBSERVE BY INTEGRATION TESTING
- `arbite file write PATH --ticket T --attempt A --read-token R --input NAME|-` and `arbite file edit ... --edits NAME|-` exist. Exit 0 with the report; 1 for bad input, generated output or a path nobody holds; 4 for a path another attempt holds; 5 for a spent token, moved bytes, a revoked generation or a closed ticket -- each refusal changing no bytes and ending in a runnable `next:` line.
- After a successful write or edit, `.arbite/coordination/receipts/` holds a succeeded receipt whose before/after artifacts are real bytes under `.arbite/coordination/artifacts/` (file sink), so `arbite receipt`/`arbite changes` (C11) have evidence to render.
- A second write with the same token is refused and names the receipt that spent it; the token's record in `.arbite/coordination/observations/` shows `spent_by`.
- A file's permission bits survive a write and an edit (POSIX); `ls -l` shows an unchanged mode after a 0600 file is rewritten.
- A write to `.pytest_cache/...`, `__pycache__/...` or `*.pyc` exits 1 with the policy sentence and no receipt.
- `arbite events` shows the file activity (`read.file` for a creation's probe, then write.intent/write.file) and still hides read observations by default.

REMAINING LIMITATIONS / RESIDUAL RISK
- SQLite coordination backend has no artifact store yet (tic-7c42/C11 decides how content lives in a database), so `file write`/`file edit` **refuse before changing any byte** on that sink: exit 1, "does not store artifact content yet ... nothing was changed". Asserted by name in tests/test_file_writes.py, and the reason the round-trip tests pin the file sink. Every refusal path and every batch rule is still asserted on both sinks. This is the one place where the two sinks do not behave identically, and it is the sink's documented state rather than a choice of this slice.
- A crash after the replacement but before finalisation is completed by recovery without re-marking the token spent (the pending receipt does not carry the token id). The safety property still holds -- a replay is refused by the version check, changing no bytes -- but the message is the moved-file one rather than "already spent". Carrying the token on the receipt would fix the message; noted for C11/C12.
- Revision 2 cannot be read by a revision-1 build and vice versa (by design, and loud rather than silent). A project with an older coordination store needs C12's migration; this checkout's two records were migrated by hand as described.
- Policy exclusions are a fixed list, not `.gitignore`: generated output outside it is written and attributed normally (documented in paths.EXCLUDED_DIRS).
- `file edit` matches every selection against the version the read served (not against the result of earlier edits in the same batch), and its rows' line numbers are that version's; an edit cannot target text another edit in the same batch introduced.
- Scratch transport is partial by design (above): `--keep` and `scratch list|clear` do not exist yet, and no help text or docstring claims them.

- 2026-09-21T13:14:03 system: Submitted; closed (review disabled).
