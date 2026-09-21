---
id: tic-e9ed
title: Cascade ticket lifecycle through file ownership and receipts
status: open
type: feature
tier: high
domain: io
epic: shared-directory-coordination
priority: null
tags:
- lifecycle
- claims
- cascade
assignee: null
depends_on:
- tic-74e2
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:33'
updated: '2026-09-21T01:16:36'
closed: null
---

## Description
Planning key: C10
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (CL7, LC1, LC2, LC3, LC4)

## Outcome and scope
Wire close, submit, accept, release, block, shelve, reopen, delete and forced takeover so each ends or preserves attempts correctly, finalizes receipts, and releases file claims. No observer may mutate successfully under an old token after a close succeeds. Delete is refused while claims or attempts are live, and generic setters route through these transitions or refuse.

## Acceptance criteria
- Close releases every active claim of the attempt and retains the receipt manifest.
- Block and shelve end the attempt, release claims, and leave partial work visible for the next worker to re-read.
- Reopen resurrects nothing and creates a fresh attempt on the next claim.
- Takeover revokes the previous generation, releases its claims and states that partial bytes remain.
- Scenarios CL7, LC1, LC2, LC3, LC4 pass exactly as written in the examples doc.

## Validation
Close racing a write, release and takeover followed by an old-token write attempt, delete with live claims, reopen history, both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
