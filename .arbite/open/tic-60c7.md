---
id: tic-60c7
title: Expose version-checked writes and exact edits
status: open
type: feature
tier: medium
domain: ui
epic: shared-directory-coordination
priority: null
tags:
- file-proxy
- writes
- edits
assignee: null
depends_on:
- tic-b03b
- tic-1c4f
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:32'
updated: '2026-09-21T01:16:35'
closed: null
---

## Description
Planning key: C07
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (WR1, WR4, WR5, WR6, WR7, ED1, ED2, ED3, BY2)

## Outcome and scope
Implement arbite file write and arbite file edit: read-token and digest verification, creation via absent probe, permission preservation, ordered exact-substitution edit batches with explicit occurrence rules, binary payloads with digests, and early refusal of generated or build output.

## Acceptance criteria
- One token authorizes exactly one mutation; a spent token is refused as stale.
- A stale digest, a revoked generation or a closed ticket changes no bytes and reports both versions.
- Ambiguous, absent or overlapping edits fail with no partial write.
- Successful mutations report receipt id, before and after digests, claim generation and the spent token.
- Scenarios WR1, WR4, WR5, WR6, WR7, ED1, ED2, ED3, BY2 pass exactly as written in the examples doc.

## Validation
Token replay, stale and revoked generation, closed-ticket mutation, ambiguous and overlapping edit batches, binary round trip, policy exclusions, both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
