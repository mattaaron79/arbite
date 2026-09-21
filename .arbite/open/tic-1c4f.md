---
id: tic-1c4f
title: Expose bounded discovery and versioned reads
status: open
type: feature
tier: medium
domain: ui
epic: shared-directory-coordination
priority: null
tags:
- file-proxy
- discovery
- reads
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
Planning key: C06
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (LS1, LS2, LS3, LS4, LS5, RD1, RD2, RD3, RD4)

## Outcome and scope
Implement arbite file list and arbite file search with deterministic ordering, counts, truncation markers and continuation tokens, and arbite file read with whole-file digests, line ranges, read tokens, busy banners, --fail-if-busy, and drift notes for external edits. Exclude scratch and coordination state from discovery, scanning and claims.

## Acceptance criteria
- Discovery labels claim state and never authorizes a write.
- A read of a path held by another attempt returns bytes with the holder named and a read-only token; --fail-if-busy refuses with no bytes served.
- A ranged read still carries the whole-file digest.
- Every truncation hint names a token printed by the same command.
- Scenarios LS1, LS2, LS3, LS4, LS5, RD1, RD2, RD3, RD4 pass exactly as written in the examples doc.

## Validation
Ordering and pagination determinism, truncation token round trips, foreign-read and busy tests, exclusion tests for scratch and coordination paths, both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
