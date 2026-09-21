---
id: tic-b82e
title: Unify job-board readiness, worker capacity and routing explanations
status: shelved
type: feature
tier: medium
domain: ui
epic: multi-provider-job-board
priority: null
tags:
- job-board
- readiness
- capacity
assignee: null
depends_on:
- tic-e59a
references:
- planning/multi-provider-job-board.md
- planning/README.md
blocked_by: null
created: '2026-09-21T01:16:53'
updated: '2026-09-21T02:18:05'
closed: null
---

## Description
Planning key: B05
Handoff: .arbite/planning/multi-provider-job-board.md

## Outcome and scope
Use one readiness evaluator for board queries, list-next, direct claim and package or offer acquisition. Expose structured reasons and distinguish hard requirements from routing hints. Enforce configured capacity during atomic acquisition.

## Acceptance criteria
- The board explains dependency, classification, reservation, continuity and worker constraints, and a claim rechecks all relevant conditions.
- Active attempts count toward capacity; reservations and future package tickets do not.
- Concurrent and batch acquisition cannot overfill a configured worker; profile changes affect future work without revoking existing attempts.
- Local and low-cost preference is advisory in passive first-eligible pickup; explicit assignment or hard constraints can enforce an owner choice.

## Validation
Readiness and query parity, concurrent capacity races, tier and capability and cost unknowns, batch partial results and combined exclusion explanations.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. The shared-directory proxy is available by this point in the epic, so use file claims while working this ticket rather than coordinating edits by hand. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T02:18:05 system: Shelved: Deferred by owner: the job-board design was inherited from the abandoned 2026-09-18 planning run and must be re-cut against the coordination primitives that actually land before any work starts. Not failed, just not now.
