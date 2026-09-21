---
id: tic-7c42
title: Expose change receipts and net ticket change views
status: open
type: feature
tier: medium
domain: ui
epic: shared-directory-coordination
priority: null
tags:
- evidence
- receipts
- changes
assignee: null
depends_on:
- tic-e9ed
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:33'
updated: '2026-09-21T01:16:36'
closed: null
---

## Description
Planning key: C11
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (EV1, EV6)

## Outcome and scope
Implement arbite receipt OP and arbite changes T with --all: an ordered operation log and a net change view per attempt and ticket. Store content once by digest where practical, verify artifacts, and make size limits explicit. Evidence is captured for create, delete, rename and binary changes, not only for the final ticket diff.

## Acceptance criteria
- A receipt reproduces before and after digests and holds the bytes or an exact reversible representation.
- The net view never hides an operation that was reverted; edit-then-revert stays visible under --all.
- If required evidence cannot be stored, the mutation fails before any bytes change.
- No automatic summarisation and no remote upload.
- Scenarios EV1, EV6 pass exactly as written in the examples doc.

## Validation
Receipt round trips for text, binary, create and delete, edit-then-revert fixtures, artifact verification and size-limit refusal, both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
