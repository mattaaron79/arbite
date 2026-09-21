---
id: tic-920d
title: Validate and document manual multi-provider job-board coordination
status: shelved
type: feature
tier: medium
domain: ui
epic: multi-provider-job-board
priority: null
tags:
- job-board
- docs
- acceptance
assignee: null
depends_on:
- tic-4178
- tic-6015
references:
- planning/multi-provider-job-board.md
- planning/README.md
blocked_by: null
created: '2026-09-21T01:16:54'
updated: '2026-09-21T02:18:06'
closed: null
---

## Description
Planning key: B08
Handoff: .arbite/planning/multi-provider-job-board.md

## Outcome and scope
Document and demonstrate manually started independent workers, coordinator reservations, direct assignment and public package pickup. Verify complete integration with the file proxy and the current CLI. No provider adapters and no running processes are added.

## Acceptance criteria
- Two differently labelled workers can mix ad-hoc work and reservation offers while respecting tier, capacity, assignment and continuity.
- Package tickets produce separate attempts and proxy receipts, releasing files and re-reading between members.
- An external caller can inspect progress and events, exit, and resume querying later; busy and no-work responses need no resident model loop.
- README and generated docs describe the new scope and the deferrals, and full regressions plus race acceptance pass for both sinks.

## Validation
End-to-end recipe with two worker identities, a reservation of three tickets with one assigned and two packaged, proxy edits, attempted unauthorised claims, completion, event restart and the full pytest suite.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. The shared-directory proxy is available by this point in the epic, so use file claims while working this ticket rather than coordinating edits by hand. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T02:18:06 system: Shelved: Deferred by owner: the job-board design was inherited from the abandoned 2026-09-18 planning run and must be re-cut against the coordination primitives that actually land before any work starts. Not failed, just not now.
