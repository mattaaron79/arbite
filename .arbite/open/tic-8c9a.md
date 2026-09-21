---
id: tic-8c9a
title: Expose resumable event queries and coordinator progress views
status: open
type: feature
tier: medium
domain: ui
epic: multi-provider-job-board
priority: null
tags:
- job-board
- events
- progress
assignee: null
depends_on:
- tic-e59a
- tic-7c42
references:
- planning/multi-provider-job-board.md
- planning/README.md
blocked_by: null
created: '2026-09-21T01:16:53'
updated: '2026-09-21T01:16:56'
closed: null
---

## Description
Planning key: B06
Handoff: .arbite/planning/multi-provider-job-board.md

## Outcome and scope
Add one-shot events --after and --limit plus reservation progress queries over durable events. Include offer, package and attempt changes with enough ids for external callers to requery readiness. No watchers and no notification delivery.

## Acceptance criteria
- Ordered cursor pagination resumes across process restarts; filters advance the cursor consistently and an invalid or foreign cursor fails clearly.
- Consumers can deduplicate repeated event delivery by stable id; only committed or reconciled events appear final.
- Reservation progress distinguishes complete, active, ready, blocked and dependency-waiting states, with recorded activity rather than liveness inference.
- Read events are separable from job lifecycle events, and dependency completion is a requery hint rather than an irrevocable readiness promise.

## Validation
Filtered and unfiltered cursor pagination, repeated reads, restart and recovery, and reservation status fixtures on both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. The shared-directory proxy is available by this point in the epic, so use file claims while working this ticket rather than coordinating edits by hand. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
