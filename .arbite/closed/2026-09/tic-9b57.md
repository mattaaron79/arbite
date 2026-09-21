---
id: tic-9b57
title: Implement canonical paths and exclusive file claims
status: closed
type: feature
tier: high
domain: io
epic: shared-directory-coordination
priority: null
tags:
- file-proxy
- claims
- paths
assignee: deepseek.code.005
depends_on:
- tic-cf9f
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:32'
updated: '2026-09-21T10:34:30'
closed: '2026-09-21T10:34:30'
---

## Description
Planning key: C04
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (FC1, FC2, FC3, FC4, FC5, FC6, FC7, FC8, LS6, RD5)

## Outcome and scope
Claim records keyed by canonical relative path with generations and observed versions. Acquire requested claim sets all-or-nothing in canonical path order with rollback on any conflict. Provide arbite file claims for inspection, explicit release, and re-acquisition that must not reactivate an old token. Reject traversal, protected arbite state, .git metadata, special files, symlink components and hard-linked mutation targets.

## Acceptance criteria
- Two claims on one path produce exactly one winner and one busy result; a conflict leaves no partial claims.
- Claims acquire in canonical order; the deterministic order is what prevents two agents each holding half of a pair.
- Releasing a path revokes its generation; a later claim mints a new one and old tokens stay dead.
- Aliases and escapes cannot bypass ownership, whatever route they arrive by.
- Scenarios FC1, FC2, FC3, FC4, FC5, FC6, FC7, FC8, LS6, RD5 pass exactly as written in the examples doc.

## Validation
Multiprocess claim races on one and many paths, ordering and rollback tests, alias and traversal refusals, generation lifecycle tests, both-sink persistence.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T10:27:15 deepseek.code.005: Implemented C04: canonical paths (coordination/paths.py), the exclusive claim surface (coordination/claims.py), records.claim_id_for (one claim record per path = the current-state index, CAS on its revision), the arbite file claim/release/claims CLI, PathRefused/NotOwner errors, REFUSAL_INDENT moved to results.py, and a text_hint escape for the two frozen blocks that join hints differently (FC3, RD5). Scenario tests FC1-FC8/LS6/RD5 pass against the document via tests/claims_state.py + tests/test_claim_examples.py (17 tests). Harness extended in tests/examples.py: digests normalised (the document's content literals describe an earlier revision of this repo - its sinks/file.py is 412 lines, ours is 579 - so the fixture reproduces the line counts literally while the digest relationship is asserted against the store), and frozen commands are now split with shlex so FC7's quoted --reason reaches the CLI as one argument. tests/test_file_claims.py (27 tests) covers aliases/protected/escape/symlink/hard-link/special-file refusals, generation lifecycle, release semantics, both-sink parity and real multi-process races. The races found a real bug: the CAS token was read after the ownership check, so a commit landing in between let a second attempt overwrite a just-claimed path (observed [0,0,4] for three racing attempts); the token is now read before the check, so any intervening commit is caught either by the check or by the commit's revision check. python3 -m pytest -q: 649 passed, 3 skipped (was 605+3).

- 2026-09-21T10:34:19 deepseek.code.005: C04 final. FILES: new src/arbite/coordination/paths.py (canonical_relative, probe/Version, missing_path_refusal, escape_refusal) and src/arbite/coordination/claims.py (FileClaims: claim/release/claims + the frozen row formats); records.claim_id_for; errors.NotOwner + errors.PathRefused; results.REFUSAL_INDENT (moved from lifecycle, same name re-exported there) and results.text_hint_of; cli.py: 'file claim|release|claims' + _claims()/_emit_file_result(); docs.py exit-code prose; README file-ownership bullet; backend docstrings; tests/claims_state.py, tests/test_claim_examples.py (17), tests/test_file_claims.py (27), tests/coordination_worker.py 'file-claim' op, tests/examples.py digests+shlex. DECISIONS: (1) one claim record per path, id derived from workspace+path, so the claim set IS the current-state index and acquisition is replace_record(expect_revision) - the CAS is what makes exclusivity real across processes and no log is replayed; history lives in claim.file/release.file events. (2) The CAS token is read BEFORE the ownership check (in claim and release): a commit landing between them is then caught either by the check or by the revision check. (3) Generations are attempt-scoped (1 + the attempt's highest existing claim generation, minted once per acquisition and stamped on every path it takes): that reproduces every frozen number in the document's narrative (FC1 1, FC2 2, schema.py 3, FC8 re-acquisition 3) and makes the FC8 note literal - a generation identifies an acquisition, so a token from an older one is dead. (4) Row shape: a version previously recorded by a released claim prints the digest alone (FC8), everything else prints digest + line count (FC1/FC2), ABSENT prints the frozen creation parenthetical (FC4); the read hint follows the same split. (5) Refusals: escape and .git/runtime-state/configuration protection at exit 1 with the frozen LS6 wording; the RD5 refusal lives in paths.missing_path_refusal for tic-1c4f to raise. (6) FC3/RD5 join their hints at the end of the previous line while CL2/CL5/FC5 join at the start, so a failure may now carry a rendered text_hint while next_actions stays the bare commands the JSON publishes. (7) Path policy: escapes, .git, .arbite/{coordination,scratch}, .arbite/arbite.db*, .arbite/project.yaml, directories, special files, symlinked components, hard-linked targets, missing parent directories and backslashes are refused; absolute paths inside the root are aliases (same claim record); tickets/plans/scratchpads under .arbite/ are deliberately NOT protected here (discovery is tic-1c4f's question). VALIDATION: python3 -m pytest -q -> 649 passed, 3 skipped, 0 failed (baseline 605+3; +44 new, no existing test changed - every edit to an existing test file was additive: two harness extensions and one worker operation). python3 -m pytest tests/test_file_claims.py tests/test_claim_examples.py -q -> 44 passed, repeated 3x. Multi-process exclusivity, real processes with a starting gun: three attempts race one path -> exit codes [0,4,4], exactly one claim record, one claim.file event (test_three_attempts_race_for_one_path, both sinks); two attempts race the same pair from opposite argument orders -> [0,4], one attempt holds both paths at one generation, nobody holds half (test_a_racing_pair_claim_leaves_nobody_holding_half, both sinks); two processes of ONE attempt -> codes subset of {0,5} with one owner (same-attempt races are a re-read, not a conflict). Those races found a real bug: with the CAS token read after the ownership check, a commit landing in the window let a second attempt overwrite a just-claimed path (observed [0,0,4]); fixed by reading the token first, and the comment in claims.claim says why the order is the guarantee rather than a detail. arbite doctor exits 0 (42 tickets) and no sibling ticket was touched. SCENARIOS: FC1, FC2, FC3 (+its JSON block), FC4, FC5, FC6, FC7, FC8 pass exactly as written through tests/claims_state.py driving the document's own chain with the real CLI; LS6 passes through 'file claim' for both cases (the escape refusal and the .git refusal are the same functions the read surface will call) and RD5 through paths.missing_path_refusal. DEVIATION: LS6/RD5 are 'file read'/'file list' transcripts and those commands are tic-1c4f's, so they are not added here; the *refusal text, hints and exit codes* are asserted against the frozen blocks byte for byte (test_LS6..., test_RD5...) and test_the_read_surface_is_not_claimed_by_this_slice pins that file read still does not exist. Content metadata: the document's digests/lines describe an earlier revision of this repo (its sinks/file.py is 412 lines, ours is 579), so tests/examples.py now normalises sha256 values while the fixtures reproduce the line counts literally (412/570) and the digest relationship is asserted against the store (observed_version == digest of those bytes). Frozen commands are split with shlex so FC7's quoted --reason arrives as one argument. OBSERVABLE (integration): in any project, after 'arbite claim <id> --agent A' prints att-XXXX, 'arbite file claim src/... --ticket <id> --attempt att-XXXX' prints 'claimed 1 path ... (generation 1)' with digest and line count, a second attempt gets exit 4 with 'busy: 1 of N paths is held ... held by ... since HH:MM:SS, gen N' and claims nothing, 'arbite file claims' lists holders and '--all' shows released history (exit 2 when empty), 'arbite file release ... --reason ...' revokes the generation without touching bytes, and re-claiming prints the new generation with the 'any token from generation N-1 is dead' note; 'arbite events' shows claim.file/release.file rows carrying path and 'gen N'. LIMITATIONS/DEFERRED: reads, writes, edits, renames, removes (tic-1c4f, tic-60c7, tic-74e2), the close/block/reopen cascade that releases an attempt's claims (tic-e9ed - until it lands, a takeover leaves the old attempt's claims active and doctor reports them as orphaned_claim), the intent journal/recovery (tic-b03b), receipts and  (tic-7c42), scratch payload transport (tic-95c0) - the busy hint names 'arbite file list' and 'arbite changes' exactly as frozen, which do not exist yet and are those slices'. Also deliberately not implemented: case-insensitive filesystem emulation beyond os.path.normcase, and any protection against an external writer racing between probe and commit (documented in paths.py; no check eliminates it).

- 2026-09-21T10:34:26 deepseek.code.005: Correction to the previous note, where a backticked command name was eaten by the shell: the deferred-slices sentence should read "receipts and the net change view (arbite changes, tic-7c42)". Nothing else in that note is affected, and no code, test or document changed as a result of it.

- 2026-09-21T10:34:30 system: Submitted; closed (review disabled).
