---
id: tic-faae
title: Add passthrough command observation
status: closed
type: feature
tier: medium
domain: ui
epic: shared-directory-coordination
priority: null
tags:
- passthrough
- cmd
- observation
assignee: deepseek.code.014
depends_on:
- tic-7c42
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:33'
updated: '2026-09-21T16:56:00'
closed: '2026-09-21T16:56:00'
---

## Description
Planning key: C13
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (PC1, PC5, PC6)

## Outcome and scope
Implement arbite cmd in observed mode: argv execution by default with --shell opting into sh -c, bounded output capture, a before and after digest manifest of managed paths, receipts for observed changes, and a passthrough.exec event carrying tool, argv hash, exit code and duration. Arbite-level refusals use 125, 126 and 127 so a wrapped tool's own exit code survives untouched. The output states plainly that observed means no exclusivity is claimed.

## Acceptance criteria
- A familiar tool's changes are captured without the agent changing habits.
- Refusals never run the command, and every refusal says command did not run.
- Redirections and shell syntax are documented as visible only after the fact.
- Interactive, long-running and background invocations are refused clearly.
- The passthrough.exec stream records enough to evaluate whether to mandate passthrough later.
- Scenarios PC1, PC5, PC6 pass exactly as written in the examples doc.

## Validation
Exit-code passthrough including collision cases, shell opt-in behaviour, refusals for unsupported invocations, observed-diff receipts, event content, both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T16:39:38 deepseek.code.014: C13 observed mode implemented: coordination/passthrough.py (manifest, diff, bounded capture, refusals, receipts+events), cli cmd surface, evidence.py taught that an observed receipt's replaced version has no image by design. PC1 (byte-exact), PC5 and PC6 fences pass; validating behaviour tests next.

- 2026-09-21T16:55:57 deepseek.code.014: C13 observed mode is implemented and validated. Files: NEW src/arbite/coordination/passthrough.py (the whole slice: manifest, diff, bounded capture, refusals, recording, report) and tests/passthrough_state.py + tests/test_passthrough.py + tests/test_passthrough_examples.py; MODIFIED src/arbite/cli.py (cmd_cmd + _command_argv + the `cmd` parser with --ticket/--attempt/--claim/--shell/--json and a REMAINDER), src/arbite/coordination/evidence.py (an observed receipt's replaced version has no image *by design*, so the receipt view reports it instead of refusing -- see below), tests/examples.py (normalise a `(N ms)` duration), tests/test_file_writes.py and tests/test_cli.py (the two slice-existence/README sweeps, justified below).

IMPLEMENTATION DECISIONS
- Exit codes. The wrapped command's own code is returned untouched, including 0-5 (a tool really can exit 4/5); a signal death maps to 128+N the way a shell reports it. Arbite's own pre-run refusals are 125 (policy: --claim not implemented, no coordination store, attempt not current), 126 (invocation arbite does not support: no command, shell syntax without --shell, interactive tool, watcher, background job, --ticket/--attempt given singly) and 127 (not on PATH). Nothing else is produced by this command.
- Refusals are values, not exceptions. `PassthroughRuns.plan()` returns either a `Refusal` or a `PassthroughRun`; only a run can start a process or append an event, so "refusals never run the command" is a property of the shape. Refusals print on stderr as `error: <message>` (+ `command did not run` where the frozen blocks print it), and `--json` always carries `ran: false`.
- Shell syntax detection is whole-token operators (`>`, `>>`, `|`, `&`, `;`, `2>&1`, ...) and substitutions (`$(`, `${`, backtick). A `|` or `*` *inside* a token is a character a program asked for (PC1's sed script proves it), so it is passed through as argv.
- Interactive tools (vim/vi/nano/less/top/screen/tmux/ssh/...), watchers (`watch`, `tail -f`, `journalctl -f`, `follow` flags) and background jobs (a bare `&` token, or a trailing/`&` operator in a --shell line) are refused before anything starts; the child gets stdin=DEVNULL and no terminal, which is what makes the interactive refusal true rather than a preference.
- Manifest. Before and after, `_manifest()` walks the project root and records every *managed* path (the files discovery offers as claimable rows), skipping symlinks, hard links, `.git`, `.arbite/coordination`, the store files, `.arbite/project.yaml`, scratch and the generated/build output a mutation refuses. Documents under `.arbite/` (tickets, agents/, planning/) ARE managed. Per path it keeps the whole-file digest, the size, and -- for UTF-8 text -- one identity per line (Python's string hash, in-process only, never stored or printed), which is what lets a row print `+1 -1` for a path whose *before* bytes no longer exist. Cost on this checkout: 170 managed paths, ~96 ms per manifest (so ~0.2 s per command) - a documented limit, not a hidden one.
- Diff and receipts. Every path whose digest differs gets one `OperationReceipt` (kind `passthrough`, result `succeeded`, ticket/attempt/actor when given, before/after digests, `absent` on the side that had nothing) plus one `passthrough.changed` event in the ordinary `file` category, so the change sits in `arbite events --tail` and in the log `arbite changes` orders by cursor. One `passthrough.exec` event (category `passthrough`) records the run: tool, argv, argv hash (sha256 of the NUL-joined argv, `\0shell` for a shell run), shell, mode, exclusive=false, exit code, duration, changed paths, receipt ids, the live claims on those paths, and the paths that were unclaimed. Everything commits in one transaction.
- Attribution/ownership. Observation claims nothing: receipts record the claim generation *this attempt holds* (0 when it has none) and never a claim held by someone else, while the event payload names the holder and lists the path as unclaimed. The report prints `mode: observed (no exclusivity claimed)`; the frozen PC1 hint's `--claim` half is mirrored in JSON as `exclusivity: {available: false, reason: guarded_not_implemented}` so no consumer reads it as an existing capability.
- Output. Both pipes are drained by daemon threads into a bounded buffer (40 lines / 8 KiB kept, the rest read and discarded, so memory is bounded and the child cannot block on a full pipe). The kept stdout is printed in place after the echo line; the tool's stderr goes to our stderr; `--json` carries both in the payload and prints nothing else. Truncation is reported per stream. After the command exits the readers get 0.5 s in total: a process the command left holding the pipes does not make arbite wait (reported as `stdout/stderr was still open ...`).
- Evidence kept. The version the command *left* is archived as an artifact where the store can hold it (best effort; over the size limit or a backend without artifacts it is reported as a digest-only record, and the tool has already run so this cannot be a refusal). The version it *replaced* is a digest with no image: capturing it would mean snapshotting the whole tree before every command, which is exactly the cost the plan's digest manifest avoids.
- evidence.py (focused change, required by this slice): `ChangeViews.receipt` used to refuse any receipt naming a version with no artifact, because for a *proxy* write that is drift. A passthrough receipt names such a version by construction, so it was unreadable. Now `OBSERVED_KINDS = ("passthrough",)`: the artifacts a receipt does name are still checked and verified exactly as before (a passthrough whose after-image went missing is still refused), while an uncovered version on an observed receipt prints `sha256:... (no image: observed, not performed by arbite)` and `artifact:` reports the image that is really held. JSON already distinguished this (`kind: passthrough`, `artifact.sides`), so no receipt field and no schema revision was added: no record type changed, schema stays at revision 2.

VALIDATION (all run from the repo root, Python 3.14)
- `python3 -m pytest -q` -> 1039 passed, 2 skipped, 0 failed (baseline was 990/2/0; +49 new tests). The pre-existing RC1 flake did not appear.
- `python3 -m pytest -q tests/test_passthrough.py tests/test_passthrough_examples.py` -> 49 passed.
- `arbite doctor` -> exit 0, "checked 42 tickets: no problems found".
- `git status --porcelain` -> only this slice's files plus the ticket's own move to .arbite/in_progress/; no sibling ticket file touched.
- Manual integration runs (PYTHONPATH=src python3 -m arbite.cli cmd ...) in a scratch project: PC1-shaped sed run (row `M ... +1 -1`, `(op-XXXX)`, exit 0); `--shell -- 'grep -c def ... > .arbite/scratch/count.txt'` (exit 0, redirect landed, no change reported); `false` -> 1, `sh -c 'exit 4'` -> 4 (collision case), `sh -c 'kill -TERM $$'` -> 143; `not-a-real-tool` -> 127; `vim` -> 126; `--claim` -> 125; uninitialised project -> 125; bad attempt id -> 125; `sleep 5 &` child -> prompt return with the "still open" note; `arbite receipt OP` and `arbite changes T` read the observed change back.

SCENARIO STATUS
- PC1: passes byte for byte (tests/test_passthrough_examples.py::test_PC1_wrap_a_familiar_tool_in_observed_mode), with `(14 ms)` normalised as a duration (a wall-clock reading, like HH:MM:SS) - tests/examples.py gained one rule for it. The run's real effects are asserted too: the file contains the substituted flag, one `passthrough` receipt exists with generation 0, and the two events exist.
- PC5: passes per line rather than byte for byte: its `event:` row omits the `actor: claude.opus.001` column that PC1 prints for the *same* ticket and attempt, so the test calls `assert_scenario_abridged` and names that abridgement. Every other line is asserted, plus the redirect landing in scratch and scratch not being a reported change.
- PC6: fences 2 (127 not found) and 3 (126 interactive) pass byte for byte. Fence 1 (126 shell syntax) cannot pass as written: its `$` line is `... -- sed -i 's/a/b/' src/arbite/cli.py`, which contains no shell syntax at all, while PC1's `sed -i 's/O_EXCL/O_EXCL|O_NOFOLLOW/'` contains a `|` and must run - no rule can refuse one and run the other, so the fence's command is the bug (and, run through a shell as the block writes it, a redirection would never reach arbite anyway). The test asserts the block's frozen refusal text, unchanged, for the same argv *plus* the redirection token the fence is about, and a second test runs the frozen command as written to record that it executes and is recorded. C15 should fix that fence (adding the `>` token) and the PC5 actor column; both are in this note for it.
- The doc's prose "Every one of those three prints `command did not run`" is also not reproducible: the frozen 126 blocks do not print it (they say the invocation is unsupported), and the frozen 127 block prints no `next:` line although the conventions say every non-zero outcome has one. The frozen blocks won, and the same fact is carried in JSON as `ran: false` for every refusal.

INTEGRATION-TESTABLE BEHAVIOUR
- `arbite cmd [--ticket T --attempt A] [--shell] -- CMD...` runs a real tool and prints: the `arbite cmd: <cmdline>` echo, the command's own stdout, `exit: N (M ms)  mode: observed (no exclusivity claimed)` (or the redirection note in --shell mode), a `changed N path(s):` block with one row per path (`M`/`A`/`D`, short digests, `+a -d`, the receipt id), the `event: passthrough.exec` line with tool/ticket/attempt/actor, and a `next:` hint naming `arbite changes T` (or `arbite receipt OP` when no ticket was given; no hint at all when nothing changed). A tool that changes files but fails still prints its rows.
- `arbite events --tail` shows `passthrough.changed` then `passthrough.exec`; `arbite changes T` and `arbite receipt OP` read the change back (the before side as `no image: observed, not performed by arbite`); `arbite cmd --json` gives the same facts plus `exclusivity`.
- Refusals print nothing on stdout, change no bytes, append no event, and exit 125/126/127.

LIMITATIONS / HANDOFF
- C14 (tic-42d2) must add: `--claim PATH...` all-or-nothing acquisition and its busy refusal (125, PC3), verification of every observed change against the claimed set with the `claimed`/`NOT claimed` column between the detail and the operation id (PC4) and the `unclaimed_write` report, release-on-completion. Seams left for it: the parsed `--claim` flag, the `claim_paths` field on the run, the `_claim_facts` per-path facts, and three `TODO(tic-42d2 / C14)` comments (the refusal, the row column, the verification point in `_record`). PC1's frozen `--claim` hint starts working then, and the JSON `exclusivity.available` flips to true.
- Deliberately not done: no runtime bound on the command (arbite waits for the command it started, since a kill is not a *refusal* and 125/126/127 mean "did not run"), only named long-running shapes are refused; no before-image for observed changes (see above); a change made and reverted inside one command is invisible to a before/after manifest; changes outside the manifest (arbite state, generated output) are not reported; the manifest is O(tree), so a very large checkout pays for it twice per command; no retention policy for the after-images (C13 adds artifacts, nothing prunes); `.arbite/AGENTS.md` (generated guide) and README do not yet describe `cmd` - that is C15.
- Existing tests deliberately updated: tests/test_file_writes.py::test_help_text_names_only_commands_that_exist now asserts `cmd` *exists* (it asserted it had not landed; `arbite cmd`'s hints name `changes`/`receipt`/`file edit`/`init`, all real), and tests/test_cli.py::test_the_readme_documents_every_command_and_the_new_model gained a one-entry `README_PENDING` allowance for `cmd` with the slice that removes it (C15 writes the README's passthrough section). No other existing test was changed; no safety restriction was relaxed.

- 2026-09-21T16:56:00 system: Submitted; closed (review disabled).
