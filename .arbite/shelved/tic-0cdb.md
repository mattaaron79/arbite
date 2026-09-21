---
id: tic-0cdb
title: Add atomic coordinator reservations over explicit ticket sets
status: shelved
type: feature
tier: high
domain: io
epic: multi-provider-job-board
priority: null
tags:
- job-board
- reservations
assignee: null
depends_on:
- tic-cf9f
references:
- planning/multi-provider-job-board.md
- planning/README.md
blocked_by: null
created: '2026-09-21T01:16:53'
updated: '2026-09-21T02:18:05'
closed: null
---

## Description
Planning key: B02
Handoff: .arbite/planning/multi-provider-job-board.md

## Outcome and scope
Implement reservation ownership independently of execution assignment. Support explicit ticket sets and snapshot expansion from an epic, with atomic membership update and release and overlap prevention.

## Acceptance criteria
- A reservation alone does not set tickets in_progress or create attempts.
- Reserve versus claim and overlapping reservations have one serial outcome; a partial set reservation never leaks.
- New epic tickets are not silently included, and nested reservations are refused.
- Direct claim, set and force respect reservations; releasing active work requires explicit interruption or is refused.

## Validation
Multiprocess overlap and reserve-versus-claim races, snapshot membership, active-release refusal and both-sink persistence tests.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. The shared-directory proxy is available by this point in the epic, so use file claims while working this ticket rather than coordinating edits by hand. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T02:18:05 system: Shelved: Deferred by owner: the job-board design was inherited from the abandoned 2026-09-18 planning run and must be re-cut against the coordination primitives that actually land before any work starts. Not failed, just not now.
