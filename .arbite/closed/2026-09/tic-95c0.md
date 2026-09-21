---
id: tic-95c0
title: Add scratch payload transport
status: closed
type: feature
tier: low
domain: ui
epic: shared-directory-coordination
priority: null
tags:
- scratch
- transport
assignee: deepseek.code.010
depends_on:
- tic-60c7
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:33'
updated: '2026-09-21T14:23:02'
closed: '2026-09-21T14:23:02'
---

## Description
Planning key: C09
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (SC1, SC2, SC3, SC4, SC5, DR3)

## Outcome and scope
Add .arbite/scratch/ as the project-local payload area for --input and --edits, with '-' meaning stdin. Payloads are consumed and cleared on success and kept on failure, with --keep opting out. Add arbite scratch list and arbite scratch clear NAME or --all. Scratch is invisible to discovery, scanning, claims and changes, and appears on the doctor report only as a count and size note that never affects the exit code.

## Acceptance criteria
- No documented command writes a payload outside the project.
- A failed write leaves the payload usable, so a recoverable error does not force a model to re-emit a file.
- Scratch never appears as a ticket, a file listing, a claim target, or a stray-file problem.
- Doctor reports the scratch count and size on every run, and the exit code is unchanged by it.
- Scenarios SC1, SC2, SC3, SC4, SC5, DR3 pass exactly as written in the examples doc.

## Validation
Consume and keep transitions, stdin payloads, refusal of outside-project paths, invisibility tests across discovery and doctor, both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T14:03:41 deepseek.code.010: Implementation done; SC1-SC5 and DR3 asserted byte-exact.

Code: coordination/scratch.py (PayloadEntry, payload_entries, writer_of, scratch_list, scratch_clear, kept_note/keep_note, the SC5 hint), coordination/writes.py (--keep, kept_refusal for the version-moved case, JSON keep), cli.py (cmd_scratch_list/cmd_scratch_clear, scratch list|clear, --keep on write/edit), README.md (scope row + scratch bullet, stale 'no command changes bytes' paragraph corrected).

Validation so far (PYTHONPATH=src python3 -m arbite.cli ...):
- python3 -m pytest -q tests/test_scratch_examples.py -> 33 passed. SC1 (both fences), SC2, SC3, SC4 (both fences), SC5 and DR3 compare byte for byte against the frozen blocks; the rest assert --keep, stdin, spent-token silence, list/clear refusals, an outside-area clear, and scratch invisibility to discovery/claims/doctor.
- python3 -m pytest -q tests/test_write_examples.py tests/test_file_writes.py tests/test_file_moves.py tests/test_move_examples.py tests/test_file_discovery.py tests/test_discovery_examples.py tests/test_workspace.py tests/test_recovery_examples.py -> 160 passed. DR1/DR2 now print the frozen two-line scratch guidance verbatim (the capability probe did its job), so that deviation is gone from tests/test_recovery_examples.py.

- 2026-09-21T14:22:59 deepseek.code.010: ## Final note: implementation, validation, scenario status

### Implementation decisions
- `coordination/scratch.py` now owns the whole payload area: `PayloadEntry`/`payload_entries` (name, size, age read from the file), `writer_of`, `scratch_list`, `scratch_clear`, the SC5 hint wording, and the payload's own sentences (`kept_note` on a refusal, `keep_note` under `--keep`).
- `coordination/writes.py`: `--keep` on both commands (the change lands and is recorded either way; only the staged copy's fate changes), and `kept_refusal` -- the version-moved refusal gains `payload: .arbite/scratch/<name> kept, so you can re-apply without re-sending the file` (SC2/WR2). The payload JSON gains `"keep": true` only when asked, so a consumed payload carries exactly the fields it always did.
- The kept line is printed for the *version-moved* refusal only. WR3 (spent token) and WR5 (attempt not current) keep the payload but print no line, and WR5's fixture does stage a payload, so a blanket "any stopped mutation says so" rule cannot hold without breaking a byte-exact block. This narrows the note tic-60c7 left ("a stopped mutation reports the payload it kept") and the reason is recorded in the module docstring beside the rule. `--keep` with `-` is refused: a piped payload has no copy to keep.
- `scratch list` attribution: arbite never performs the write that stages a payload, so the row names the store's most recently active attempt -- attribution of the work in progress, not proof of authorship -- stated as such in the help text and module docstring; a store naming no attempt prints `(no attempt recorded)`. SC1's world holds one attempt so the sentence is unambiguous.
- Exit shapes chosen where no transcript freezes them: `scratch list` on an empty area is exit 2 (the answer `file list` gives for an empty directory) with no `next:` line; `clear --all` on an empty area is exit 0 (`cleared 0 files ... (nothing was staged)`); `clear` with neither names nor `--all`, or with both, is exit 1; a clear refuses a name that is absent or points out of the area *before* unlinking anything, and a name copied out of a listing (`./base.py`) clears as it reads.
- Required correction while implementing SC3: a *text creation* printed `created, N (binary)`, because the report treated the absent before-version as binary. The frozen SC3 block prints `created, 84 lines`, so the report now judges only the new bytes for a creation (a replacement is still binary when either side is). No existing test asserted the old shape.

### Validation (exact commands and results)
- `python3 -m pytest -q` -> 885 passed, 3 skipped, 0 failed (baseline 852+3; the 33 new tests are tests/test_scratch_examples.py). An intermediate full run hit the pre-existing RC1 flake described below; the final run was green.
- `python3 -m pytest -q tests/test_scratch_examples.py tests/test_recovery_examples.py tests/test_examples.py` -> 58 passed.
- `python3 -m pytest -q tests/test_write_examples.py tests/test_file_writes.py tests/test_file_moves.py tests/test_move_examples.py tests/test_file_discovery.py tests/test_discovery_examples.py tests/test_workspace.py tests/test_recovery_examples.py` -> 160 passed.
- `PYTHONPATH=src python3 -m arbite.cli doctor` in this checkout -> `checked 42 tickets: no problems found` + `note: .arbite/scratch/ is empty`, exit 0. `arbite doctor` (pipx 0.2.0, lifecycle only) also exits 0 with 42 tickets; the pipx copy predates the scratch note, so DR3 was verified with the checkout under PYTHONPATH.

### Scenario status
- SC1, SC2, SC3, SC4, SC5, DR3: byte-exact, all six, no deviation. Two readings worth stating:
  - SC1 and SC4 each hold two fences for one narrative whose numbers differ (SC1's list shows a 4.1 KiB payload, its write turns a 570-line file into a 588-line one), so each fence is asserted against the world its own numbers describe.
  - The halves that *succeed* (SC1's write, SC3's creation, the --keep tests) are asserted on the file sink only: the sqlite coordination backend deliberately cannot store a mutation's artifact content yet (`EVIDENCE_UNAVAILABLE`, tic-7c42 decides how), which predates this slice and is asserted in test_file_writes. The list/clear/refusal/doctor halves run on both sinks.
  - SC3's `--read-token op-5d11` is illustrative like the other ids: a path that does not exist cannot be read, so the command records its own probe; the harness normalises the token and it is never consulted.
- DR1/DR2 guidance: verified, not assumed. `cli.knows_command("scratch clear")` is now true, so `note_lines` prints the frozen two-line sentence verbatim; `tests/test_recovery_examples.py` asserts the frozen scratch note inside DR1's output, keeps the frozen block byte-exact, and its deviation list now holds only the `arbite receipt`/`arbite changes` sentence (tic-7c42).

### Existing tests deliberately updated
- tests/test_recovery_examples.py: the scratch entry left DEFERRED; `_only_deferred` no longer asserts a block contains a deviation (DR1 no longer has one); `test_the_deferred_sentences_are_absent_because_their_commands_are` became `test_the_scratch_guidance_prints_because_its_command_exists`; DR1 gained the frozen-note assertion.
- tests/test_file_writes.py: `test_help_text_names_only_commands_that_exist` moved `scratch list`/`scratch clear` to the existing list (they exist now); `receipt`/`changes` stay in the other.
- tests/writes_state.py: one comment ("no arbite command produces a payload yet") corrected -- the payload area is listed and cleared now, but still staged by the caller.
- Plus new: tests/scratch_state.py, tests/test_scratch_examples.py.

### What a reviewer can observe by integration testing
In a throwaway project (`sink: file`), after `init`/`raw`/`promote --agent`/`file claim`/`file read`:
- `scratch list` on an empty area: `no payloads in .arbite/scratch/`, exit 2.
- stage `.arbite/scratch/payload.py` -> `scratch list`: `1 file in .arbite/scratch:` / `  payload.py   14 B  written HH:MM:SS (agent deepseek.code.010)`.
- `file write src/app.py ... --input payload.py`: `wrote ... 2 -> 3 lines  +1 -0`, `receipt: op-XXXX ...`, `payload: .arbite/scratch/payload.py consumed and cleared (bytes retained as receipt artifact)`, exit 0; a second `scratch list` is empty.
- with the file moved by hand after the read: `stale_read: you read sha256:... but the file is now sha256:... (changed HH:MM:SS)`, `no bytes were changed`, `payload: .arbite/scratch/next.py kept, so you can re-apply without re-sending the file`, exit 5 -- and the payload is still on disk.
- with `--keep`: `payload: .arbite/scratch/next.py kept (--keep)`, exit 0, payload still on disk; `--json` carries `"keep": true`.
- `scratch clear --all`: `cleared 1 file from .arbite/scratch/ (next.py, 8 B)`, exit 0; `scratch clear base.py`: per-file sentence + `next: 'arbite scratch list' to see what remains`.
- `--input /tmp/next.py`: the SC5 refusal, exit 1; `file claim`/`file write` on `.arbite/scratch/...`: `arbite does not manage its own runtime state`, exit 1.
- `doctor` with a payload staged: exit 0 (a payload is never a problem), note naming the count/size and `clear with 'arbite scratch clear --all'`; `doctor --fix` prints the shorter DR2 note; `doctor --json` carries `"scratch": {"files": N, "bytes": M}`.

### Remaining limitations and residual risk
- Deferred, deliberately: receipts/changes views (tic-7c42, the one remaining doctor deviation), lifecycle cascade (tic-e9ed), migrations/export (tic-008f), passthrough (tic-faae, tic-42d2). Nothing here claims them.
- The list row's agent is the store's last-active attempt, not authorship; multi-agent workspaces get the most recent one.
- A clear stops at the first file it cannot remove and names it (exit 1), rather than reporting a partial success.
- Pre-existing flake, reproduced on a clean HEAD worktree (c9c6fb7) at 1 failure in 60 runs: `tests/test_coordination_lifecycle.py::test_RC1_two_workers_claiming_one_ticket_produce_one_winner` occasionally sees a losing claim exit 4 instead of 1 (`assert [0, 1, 1, 4] == [0, 1, 1, 1]`). It is in the claim/lock path this slice does not touch; owner is the claim slices (tic-cf9f, tic-9b57). Evidence: 60 consecutive single-test runs in a detached worktree of the pre-change commit failed once with that assertion; the same loop in the working tree passed 40/40 and 60/60 on later runs.

- 2026-09-21T14:23:02 system: Submitted; closed (review disabled).
