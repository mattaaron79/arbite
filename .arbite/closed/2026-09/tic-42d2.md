---
id: tic-42d2
title: Add passthrough guarded mode
status: closed
type: feature
tier: high
domain: ui
epic: shared-directory-coordination
priority: null
tags:
- passthrough
- claims
- guarded
assignee: deepseek.code.015
depends_on:
- tic-faae
- tic-74e2
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:33'
updated: '2026-09-21T17:20:48'
closed: '2026-09-21T17:20:48'
---

## Description
Planning key: C14
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (PC2, PC3, PC4)

## Outcome and scope
Add --claim PATH... to arbite cmd: claim the declared paths all-or-nothing before running, refuse before running when any is busy, then verify that every observed change falls inside the claimed set. A change outside it is reported as unclaimed_write, recorded and attributed honestly but never undone, because arbite does not roll back a command it did not perform. Claims are released on completion unless the caller asks to hold them.

## Acceptance criteria
- Passthrough cannot become the bypass that quietly evades claim ownership.
- A busy declared path refuses before the command runs, with the holder named and alternatives offered.
- An escape from the claimed set is detected, attributed, and left in place with a repair path suggested.
- Guarded output distinguishes arbite's own refusals from the wrapped tool's result.
- Scenarios PC2, PC3, PC4 pass exactly as written in the examples doc.

## Validation
Guarded versus observed parity, busy refusal before execution, unclaimed-write detection, claim release on completion and on failure, both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T17:17:48 deepseek.code.015: C14 guarded mode implemented and green: coordination/passthrough.py (--claim acquisition through the claim layer before the run, PC3 busy refusal at 125 with the holder named, verification of every change, unclaimed_write report that undoes nothing, release on completion whether the tool succeeded, failed or escaped), cli cmd help/docstring, tests/test_passthrough_guarded.py (29 new behaviour tests, both sinks), PC2/PC3/PC4 transcript tests in tests/test_passthrough_examples.py, guarded fixtures in tests/passthrough_state.py; two C13 tests updated (see final note). Full suite: 1072 passed, 2 skipped, 0 failed (baseline 1039/2/0, +33 tests); arbite doctor exit 0. PC3 passes byte for byte; PC2 and PC4 are asserted by their facts with the block abridgements named (see final note).

- 2026-09-21T17:20:44 deepseek.code.015: C14 guarded mode is implemented, validated and ready to close.

FILES: MODIFIED src/arbite/coordination/passthrough.py (the whole slice), src/arbite/cli.py (cmd docstring + parser description + --claim help), tests/passthrough_state.py (guarded fixtures), tests/test_passthrough.py (two obsolete expectations, listed below), tests/test_passthrough_examples.py (PC2/PC3/PC4); NEW tests/test_passthrough_guarded.py (29 behaviour tests).

IMPLEMENTATION DECISIONS
- Where it lives: `PassthroughRuns.plan` no longer refuses `--claim`; its last step is `_acquire`, so every refusal an invocation can meet is judged before any state changes and a refused run never leaves a claim behind. Acquisition is `FileClaims.claim` -- the same operation `arbite file claim` presents -- so guarded mode cannot hold anything a plain claim would refuse, generations come from the same attempt-scoped counter, and all-or-nothing is the claim layer's rule instead of a second implementation of it.
- Busy: the claim layer's `file_busy` outcome becomes a refusal labelled `busy` at 125 (0-5 belong to the wrapped tool), with FC3-shaped rows (held rows name the holder and the actor; free rows say `free`), the sentence `command did not run`, and PC3's frozen hint -- prose in text, bare commands as JSON `next_actions`. `Refusal` gained `label`/`mode`/`rows`/`data` for this; every C13 refusal prints exactly as it did.
- Verification: the claimed set is the canonical set the acquisition recorded, so `inside` is ownership rather than what is on disk now. Each change is stamped (`part_of_claim`), the escaped paths return in canonical order, and nothing is undone. The escape is recorded on the change's `passthrough.changed` payload (`claimed`) and on the run's `passthrough.exec` payload (`claim_paths`, `claim_generation`, `unclaimed_write`): no new receipt kind and no schema revision (schema stays at 2, and `evidence.OBSERVED_KINDS` needs no new entry because no kind was added).
- Exit codes: `exit_code` stays the wrapped tool's everywhere. A guarded run whose verification finds an escape returns 1 -- arbite's own error, the one code of its own for a command that really ran -- and JSON adds `arbite_exit_code` so a machine consumer sees both without ambiguity.
- Release: after `_record` (the receipts read the live claim's generation), through the same claim layer, reason `work complete for this command`. It runs whether the tool succeeded, failed or escaped; the released record stays as history (`file claims --all` shows it with its reason) and the `release.file` event makes the revocation auditable. A release that cannot be recorded is reported (`note: the claims could not be released (...); arbite file claims shows who holds them now`), JSON says `released: false` with the error, and `doctor --fix` releases a claim whose attempt is over.
- Display: guarded mode prints the acquisition banner (the lines `file claim` prints, including a re-acquisition's note) before the echo line; `exit: N (M ms)  mode: guarded (exclusive on K path(s))` (with the redirection note in `--shell` mode); `changed N path(s), all inside the claimed set:` or `..., M OUTSIDE the claimed set:`; the `claimed`/`NOT claimed` column appears only in a run where the rows disagree with each other (PC4), because in a run where they all agree the heading already says so (PC2); path columns are padded so multi-row blocks line up; then the `unclaimed_write` block, the release line, and a hint (claim every escaped path + review). Observed mode's lines are unchanged, byte for byte, so PC1/PC5/PC6 pass untouched.
- JSON: `exclusivity` reports the seam truthfully in both modes (`available: true`, `claimed` = what this run held, `reason: null`); a guarded report adds `claims` (paths, generation, released, release_reason), `unclaimed_write`, `escaped` and `arbite_exit_code`.

VALIDATION (all from the repo root, Python 3.14)
- `python3 -m pytest -q` -> 1072 passed, 2 skipped, 0 failed (baseline 1039/2/0; +33 tests). The pre-existing RC1 flake did not appear.
- `python3 -m pytest -q tests/test_passthrough_guarded.py` -> 29 passed (both sinks where recording matters).
- `python3 -m pytest -q tests/test_passthrough.py tests/test_passthrough_examples.py tests/test_passthrough_guarded.py tests/test_claim_examples.py tests/test_file_claims.py tests/test_change_evidence.py tests/test_coordination_results.py` -> 175 passed.
- `arbite doctor` -> exit 0, `checked 42 tickets: no problems found`.
- `git status --porcelain` -> only this slice's files plus the ticket's own move to `.arbite/in_progress/`; no sibling ticket, planning doc or config touched.
- Manual integration runs (`PYTHONPATH=src python3 -m arbite.cli cmd ...`) in scratch projects: guarded sed (banner, row, event, release line, review hint, exit 0); guarded `--shell` with a redirect and an echo (mode + shell note on the exit line, exit 0); the wrapped command itself asking `file claims --json` and finding att-91bd holding the path while it ran; guarded busy with two declared paths (exit 125, FC3-shaped table, the free path named, nothing claimed, no event); observed mode unchanged; `--claim` without `--attempt` -> 126 with the pair named; `file claims --all` after a guarded run shows the released record; `arbite receipt OP` reads the change back with `claim generation: 1`.

SCENARIO STATUS
- PC3: passes byte for byte (tests/test_passthrough_examples.py::test_PC3_guarded_mode_a_busy_declared_path), including empty stdout, the `command did not run` line, the holder row and the two-line hint.
- PC2: passes by its facts (`examples.assert_facts`, per line, whitespace collapsed) with two abridgements named in the test: its command line is elided in the block (`sed -i ... src/arbite/sinks/file.py`) and its banner column is hand-padded one space short of what the same acquisition prints (FC1). Both restored lines come from other frozen blocks -- PC1's echo for this command, FC1's banner for this kind of path -- so nothing is invented. The block also omits the `event:` line and the review hint PC1 prints for the same situation; the report prints both (named in the test).
- PC4: passes by its facts with three abridgements named in the test: the same elided echo; the banner folded onto one line and stripped of its version row (restored to `file claim`'s two lines, FC1); and the row order, where the block writes schema.py before query.py while paths sort lexically, so query.py comes first -- the rows are compared as facts in any order and canonical order is asserted directly.
- PC1, PC5, PC6: unchanged and still passing. What the blocks assert beyond their text is asserted too (the sed's bytes, the receipt generations, the release, the absence of any event or claim when a refusal happens).
- These are abridged or elided transcript lines of the same kind C13 recorded on tic-faae, not new capability gaps. C15's frozen-block reconciliation list should carry: PC2 and PC4's echo lines, PC4's banner, and PC4's row order. PC3 needed no fix.

EXISTING TESTS DELIBERATELY UPDATED (2, both in tests/test_passthrough.py)
1. `_refusals`' `guarded mode (C14)` entry asserted the exact refusal this ticket removes (`--claim` -> 125 for any path). It is replaced by a real busy refusal (a rival's live claim on the declared path -> 125), so the refusal table still covers guarded mode. Its shared label assertion widened from `error:` to the exit-code vocabulary's labels, because a refused claim prints `busy:` -- the code stays 125, since 0-5 belong to the wrapped tool.
2. `test_the_json_report_carries_the_same_facts_as_the_text` asserted `exclusivity.available: false` and `reason: guarded_not_implemented`. Both facts stopped being true when guarded mode landed, which is C13's own handoff for C14 (PC1's frozen `--claim` hint starts working then, and the JSON `exclusivity.available` flips to true). It now asserts `available: true`, `reason: null`, `claimed: []`.
No other existing test was changed, and no safety restriction was relaxed.

INTEGRATION-TESTABLE BEHAVIOUR
- `arbite cmd --ticket T --attempt A --claim PATH... -- CMD...` claims before it runs: the banner appears above the echo line, the exit line says `mode: guarded (exclusive on K path(s))`, and the run ends with the release line. `arbite file claims` shows nothing held afterwards; `arbite file claims --all` shows the released record and why.
- A busy declared path prints `busy: ... nothing was claimed and the command did not run` on stderr with exit 125, and leaves no event, no receipt, no claim and no changed byte -- including the paths of the same request that were free.
- An escape prints exit 1 while `exit_code` stays the tool's, leaves the escaped bytes exactly as the tool wrote them, and `arbite events --tail` shows which change fell outside the claim (`claimed: false` on `passthrough.changed`, `unclaimed_write` on `passthrough.exec`).
- A tool that fails still releases its claims, and the release line says so instead of claiming the work was complete.

LIMITATIONS / DEFERRED
- No runtime enforcement during the run: the declared set is checked before the command starts and verified after it. A takeover mid-run, or a foreign writer touching an unclaimed path, is reported from the manifest rather than prevented (unchanged from C13's stated limits).
- A killed guarded run leaves its claim live; `doctor`'s unusable-claim check is what releases it once the attempt is over (no automatic stale recovery, per the boundary).
- No `--hold` or `--keep-claims` flag. The ticket's outcome text mentions releasing unless the caller asks to hold them, but no such flag exists in the frozen surface and the plan's C14 slice text does not name one; adding CLI surface would exceed this slice, so the release decision is always on completion here. Recorded for the owner: a flag for it would be a new ticket.
- A change made and reverted inside one run is still invisible (the before/after manifest), and a change outside the manifest is not an escape because it is not an observed write.
- Documentation is C15's: the README still does not describe `cmd` (tests/test_cli.py README_PENDING), and `docs.py` and `.arbite/AGENTS.md` are untouched.

- 2026-09-21T17:20:48 system: Submitted; closed (review disabled).
