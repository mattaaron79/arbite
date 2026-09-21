---
id: tic-cf9f
title: Create work attempts and guard every ticket acquisition path
status: open
type: feature
tier: high
domain: io
epic: shared-directory-coordination
priority: null
tags:
- attempts
- lifecycle
- readiness
assignee: null
depends_on:
- tic-1a75
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:32'
updated: '2026-09-21T01:16:34'
closed: null
---

## Description
Planning key: C03
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (CL1, CL2, CL3, CL4, CL5, CL6, LC5, RC1)

## Outcome and scope
Create and end durable attempts on ticket acquisition and transitions, and enforce readiness atomically with acquisition. Route direct claim, list next --claim, batch claim, set, set-status, force, release, block, shelve and reopen through the application layer. Add explicit adoption for legacy in-progress tickets, and generation revocation for explicit administrative takeover.

## Acceptance criteria
- Direct claim cannot bypass unmet dependencies, placeholder classification, an invalid status, or another active attempt.
- Claim racing a dependency edit or reopen has a documented serial outcome; reopening a prerequisite emits an invalidation without silently undoing running work.
- Generic setters route through the lifecycle or refuse, so no backdoor exists through force.
- Activity timestamps are stored with no expiry logic and no liveness inference.
- Timeout-prone callers get a fast, structured refusal rather than a wait.
- Scenarios CL1, CL2, CL3, CL4, CL5, CL6, LC5, RC1 pass exactly as written in the examples doc.

## Validation
Concurrent claims, stale-generation calls, dependency changes racing a claim, legacy adoption, every lifecycle entry point and batch claims on both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
