---
id: tic-74e2
title: Expose tracked creation, removal and rename
status: open
type: feature
tier: medium
domain: ui
epic: shared-directory-coordination
priority: null
tags:
- file-proxy
- remove
- rename
assignee: null
depends_on:
- tic-60c7
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:33'
updated: '2026-09-21T01:16:35'
closed: null
---

## Description
Planning key: C08
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (RN1, RN2, RN3, RN4, FC4)

## Outcome and scope
Implement arbite file remove and arbite file rename so agents do not fall back to shell writes. Rename claims both source and destination, requires absence or an explicit destination version, and records both paths. Remove keeps the bytes in a receipt. Parent creation is constrained to safe in-root directories and recursive directory deletion is refused.

## Acceptance criteria
- Rename moves ownership to the destination and releases the source path.
- An existing destination requires an explicit expected version.
- Remove and rename round-trip through receipts, including binary content.
- Refusals name the manual alternative rather than leaving the agent to improvise.
- Scenarios RN1, RN2, RN3, RN4, FC4 pass exactly as written in the examples doc.

## Validation
Rename across directories, destination collisions with and without an expected version, source or destination busy, removal evidence, directory refusal, both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
