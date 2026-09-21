---
id: tic-f576
title: Publish offers and direct assignments with atomic worker pickup
status: open
type: feature
tier: medium
domain: io
epic: multi-provider-job-board
priority: null
tags:
- job-board
- offers
- assignment
assignee: null
depends_on:
- tic-ada8
- tic-0cdb
- tic-e9ed
references:
- planning/multi-provider-job-board.md
- planning/README.md
blocked_by: null
created: '2026-09-21T01:16:53'
updated: '2026-09-21T01:16:55'
closed: null
---

## Description
Planning key: B03
Handoff: .arbite/planning/multi-provider-job-board.md

## Outcome and scope
Implement offers over tickets, including direct worker assignments and reservation-backed public pickup. Validate eligibility and readiness and create the execution attempt in the same acquisition transaction.

## Acceptance criteria
- Publishing retains coordinator reservation ownership; worker pickup needs no orchestrator round trip.
- Two workers accepting one offer produce exactly one active attempt.
- Withdraw versus accept is serialized; withdrawal or release never silently cancels an active worker.
- Direct claim and list-next cannot bypass assignments, requirements or reservation access; cost preferences are not misrepresented as winner guarantees.

## Validation
Offer and assignment eligibility, concurrent pickup, withdraw races, explicit interruption, and force and set bypass regression tests on both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. The shared-directory proxy is available by this point in the epic, so use file claims while working this ticket rather than coordinating edits by hand. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
