---
id: tic-b03b
title: Build the file-operation intent journal and recovery engine
status: open
type: feature
tier: high
domain: io
epic: shared-directory-coordination
priority: null
tags:
- file-proxy
- journal
- recovery
assignee: null
depends_on:
- tic-9b57
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:32'
updated: '2026-09-21T01:16:35'
closed: null
---

## Description
Planning key: C05
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (DR1, DR2, RC2)

## Outcome and scope
Persist intent and before and after artifacts, stage bytes, verify claims and versions, apply the filesystem operation, then finalize the receipt. Use operation ids for retry deduplication. Detect incomplete intent on the next relevant operation and reconcile against recorded versions. Expose pending operations through doctor and repair only the unambiguous cases with --fix.

## Acceptance criteria
- Injected failures before and after each boundary recover honestly, including replacement followed by sink failure and a rename interrupted between paths.
- Bytes matching neither the recorded before nor after version are reported as drift with evidence preserved.
- No recovery daemon and no invented ownership release.
- Errors state whether bytes may already have changed and which operation to inspect or retry.
- Scenarios DR1, DR2, RC2 pass exactly as written in the examples doc.

## Validation
Crash injection at every boundary on both sinks, retry idempotence, drift fixtures, doctor --fix behaviour on repairable and unrepairable cases.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
