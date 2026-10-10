---
id: tic-3bed
title: the palette test asserts the 256-colour rung whatever the terminal announces
status: closed
type: bug
tier: low
domain: ui
epic: test-determinism
priority: 1
tags:
- tests
- term
- colour
- cli
assignee: omp.qwen3.8-next-flash.005
depends_on: []
blocked_by: null
created: '2026-10-09T21:41:18'
updated: '2026-10-09T22:12:21'
closed: '2026-10-09T22:12:21'
---

## Description
`python -m pytest` fails in a shell that exports `COLORTERM=truecolor` (any modern terminal) and passes with it unset. The product is right and the test is blind: `term.epic_code()` (src/arbite/term.py:264-270) picks the best rung the terminal announced -- `EPIC_TRUECOLOR` when `COLORTERM` says `truecolor`/`24bit` (term.py:151), `EPIC_256` on a 256-colour terminal, `EPIC_16` otherwise -- but `test_the_epic_and_the_assignee_carry_their_own_accents` (tests/test_cli.py:3032-3048) asserts `term.EPIC_256 in rich` unconditionally.

Cause: the `cli` fixture (tests/test_cli.py:33-57) builds the child environment as `dict(os.environ, PYTHONPATH=...)` and only then applies the test's `env=`, so the developer's `COLORTERM` leaks into a subprocess whose test explicitly states `{"TERM": "xterm-256color"}`. The fixture already refuses to inherit one piece of the developer's shell, with a comment saying why: `ARBITE_SINK` (line 39-41, "every test states what it is testing, including 'the default is files'"). Colour capability is the same kind of fact.

Fix: make the colour rung a stated input rather than an inherited accident. Preferred shape, mirroring the `ARBITE_SINK` precedent: the `cli` fixture drops the ambient colour announcements (`COLORTERM`, `NO_COLOR`, `ARBITE_COLOR`, `TERM`) so a CLI test starts from a known baseline, and the accent test keeps stating the rung it means by passing `env=` explicitly -- including a `COLORTERM=truecolor` case asserting `EPIC_TRUECOLOR`, which the current test never covers even though the ladder has three rungs and term.py:144-154 documents the announcement. If you instead scope the fix to the one test, say why in the note. Audit the rest of tests/ for other assertions that depend on the ambient rung (`EPIC_256`, `ASSIGNEE_256`, `EPIC_16`, status-colour constants) and pin those the same way; tests/test_term.py:152-188 already passes `env=` explicitly to the term functions and needs nothing.

Acceptance:
- `python -m pytest -q tests/test_cli.py` passes with `COLORTERM=truecolor` exported, with `COLORTERM` unset, with `NO_COLOR=1`, and with `TERM=xterm-256color` -- the four shell shapes a developer actually has. Report the four result lines.
- The full suite result is unchanged otherwise (known unrelated failure: scenario LC1, tracked separately).
- The accent test still proves the thing it was written to prove: epic violet and assignee orange present on a 256-colour terminal, the nearest plain colours on a base terminal with no 256 codes leaking through, and the words identical after stripping ANSI. It must additionally prove the truecolor rung, so a future change to the ladder cannot pass by accident.
- Nothing in src/arbite/ changes: the ladder is correct as built. If you think it is not, stop and report instead of editing product code.

## Notes
- 2026-10-09T22:00:13 omp.qwen3.8-next-flash.005: Colour rung is now a stated input, taken in tests/test_cli.py only; src/ untouched.

Added child_env() (test_cli.py:33-44): the environment every arbite subprocess runs with is os.environ + PYTHONPATH minus ARBITE_SINK, COLORTERM, NO_COLOR, ARBITE_COLOR and TERM. It replaces the two places that each rebuilt the environment and popped only ARBITE_SINK -- the cli fixture (now :52) and run_cli (now :79) -- so the CLI surface has one baseline. TERM is safe to drop: term.py is the only product reader of it, and only for colour.

Rewrote test_the_epic_and_the_assignee_carry_their_own_accents (:3042-3072) to cover all three rungs of the ladder, each stated by the test: COLORTERM=truecolor asserts EPIC_TRUECOLOR present, ASSIGNEE_256 present (the orange has no higher rung) and EPIC_256 absent; TERM=xterm-256color asserts EPIC_256 + ASSIGNEE_256 present and EPIC_TRUECOLOR absent (24-bit not assumed from 256); TERM=xterm asserts EPIC_16 + ASSIGNEE_16 present, no 256 code, and no '\x1b[38;' anywhere, i.e. no extended code at all on a base terminal. ANSI.sub equality now spans all three outputs.

Fixed the PLAIN_ENV comment (:2955-2958), which claimed an empty NO_COLOR was there to neutralise an inherited one -- there is none to neutralise now; the entry stays as the test's own statement that empty means unset.

Verifier observes: python -m pytest -q tests/test_cli.py -> 216 passed under COLORTERM=truecolor, under env -u COLORTERM, under NO_COLOR=1 and under TERM=xterm-256color (was: 1 failed, the accent test, whenever COLORTERM announces truecolour). Negative control: the accent test passes under COLORTERM=24bit NO_COLOR=1 TERM=dumb -- all three announcements hostile at once -- which is exactly the shape that failed before. tests/test_term.py needed nothing (it passes env= explicitly): 23 passed both with COLORTERM=truecolor and with it unset. The sweep of tests/ found colour assertions only in test_cli.py and test_term.py; the other subprocess runners (examples.py, test_file_writes/moves, test_recovery_journal, test_coordination_*) assert no colour at all, so they were left alone rather than pinned speculatively.

- 2026-10-09T22:12:20 omp.qwen3.8-next-flash.001: verified by the parent: the colour tests pass under four shell shapes (COLORTERM=truecolor, TERM=xterm-256color, NO_COLOR=1, and all three hostile at once) in 3s each; the full suite run as COLORTERM=24bit NO_COLOR=1 TERM=dumb is 1345 passed / 2 skipped / 0 failed -- the first fully green run of this repo's suite. Scope confirmed complete by grep: ANSI assertions live only in tests/test_cli.py (now pinned through child_env) and tests/test_term.py (explicit env= dicts), and the only 'TERM' outside term.py in src/arbite is the argparse metavar at cli.py:3472, so dropping it from the child environment changes nothing but colour selection. Observable: pytest no longer depends on whether the developer's terminal advertises 24-bit colour, and the accent test now proves the truecolor rung as well as the 256 and 16 rungs.

- 2026-10-09T22:12:21 system: Submitted; closed (review disabled).
