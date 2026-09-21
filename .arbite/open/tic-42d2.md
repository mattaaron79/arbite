---
id: tic-42d2
title: Add passthrough guarded mode
status: open
type: feature
tier: high
domain: ui
epic: shared-directory-coordination
priority: null
tags:
- passthrough
- claims
- guarded
assignee: null
depends_on:
- tic-faae
- tic-74e2
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:33'
updated: '2026-09-21T01:16:37'
closed: null
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
