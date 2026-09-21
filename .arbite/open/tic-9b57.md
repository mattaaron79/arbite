---
id: tic-9b57
title: Implement canonical paths and exclusive file claims
status: open
type: feature
tier: high
domain: io
epic: shared-directory-coordination
priority: null
tags:
- file-proxy
- claims
- paths
assignee: null
depends_on:
- tic-cf9f
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:32'
updated: '2026-09-21T01:16:34'
closed: null
---

## Description
Planning key: C04
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (FC1, FC2, FC3, FC4, FC5, FC6, FC7, FC8, LS6, RD5)

## Outcome and scope
Claim records keyed by canonical relative path with generations and observed versions. Acquire requested claim sets all-or-nothing in canonical path order with rollback on any conflict. Provide arbite file claims for inspection, explicit release, and re-acquisition that must not reactivate an old token. Reject traversal, protected arbite state, .git metadata, special files, symlink components and hard-linked mutation targets.

## Acceptance criteria
- Two claims on one path produce exactly one winner and one busy result; a conflict leaves no partial claims.
- Claims acquire in canonical order; the deterministic order is what prevents two agents each holding half of a pair.
- Releasing a path revokes its generation; a later claim mints a new one and old tokens stay dead.
- Aliases and escapes cannot bypass ownership, whatever route they arrive by.
- Scenarios FC1, FC2, FC3, FC4, FC5, FC6, FC7, FC8, LS6, RD5 pass exactly as written in the examples doc.

## Validation
Multiprocess claim races on one and many paths, ordering and rollback tests, alias and traversal refusals, generation lifecycle tests, both-sink persistence.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
