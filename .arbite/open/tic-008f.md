---
id: tic-008f
title: Add coordination migrations, export and integrity recovery
status: open
type: feature
tier: high
domain: io
epic: shared-directory-coordination
priority: null
tags:
- coordination
- migration
- integrity
assignee: null
depends_on:
- tic-7c42
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:33'
updated: '2026-09-21T01:16:36'
closed: null
---

## Description
Planning key: C12
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (multi-record round trip)

## Outcome and scope
Extend migration, export and doctor to attempts, claims, receipts, artifacts and events. Preserve generations, actors and operation ids across a quiescent transfer, refine integrity checks per sink, and export a receipt summary suitable for a devlog before any pruning. Shared checks stay shared where they mean the same thing; storage-specific ones stay per sink.

## Acceptance criteria
- A file to SQLite to file round trip preserves attempts, claims, receipts and event cursors when quiescent.
- Active work prevents an unsafe store switch.
- Doctor detects claim without attempt, attempt on a closed ticket, orphaned claims, drifted note indexes and pending operations without guessing.
- No artifact garbage collection until retention and reference rules are designed, and disk growth is documented.
- Scenarios multi-record round trip pass exactly as written in the examples doc.

## Validation
Full round trip with live claims, deliberate integrity damage, refusal while work is active, cross-sink doctor parity, export summary reproducibility.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
