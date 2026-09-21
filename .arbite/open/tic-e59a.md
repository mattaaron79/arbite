---
id: tic-e59a
title: Implement ordered same-worker packages and explicit continuity handoff
status: open
type: feature
tier: high
domain: io
epic: multi-provider-job-board
priority: null
tags:
- job-board
- packages
- continuity
assignee: null
depends_on:
- tic-f576
- tic-e9ed
references:
- planning/multi-provider-job-board.md
- planning/README.md
blocked_by: null
created: '2026-09-21T01:16:53'
updated: '2026-09-21T01:16:56'
closed: null
---

## Description
Planning key: B04
Handoff: .arbite/planning/multi-provider-job-board.md

## Outcome and scope
Bundle ordered tickets into one continuity contract. Accept binds the worker while only the current ready member starts. Validate the combined package and dependency graph, and implement explicit remaining-work handoff.

## Acceptance criteria
- Later package members cannot be cherry-picked by direct claim, and no early in_progress state appears.
- Same-worker identity is durable but is not a promise of the same conversation; each ticket has its own attempt.
- Completion releases file claims through the lifecycle, and the next member requires fresh reads; blocked members do not advance.
- Duplicate memberships and cycles are refused; external prerequisites are explained; an explicit rebind preserves completed members and records the reason.

## Validation
Two-member continuity, crash and resume identity, external dependency pause, partial completion and rebind, combined-graph cycles and bypass tests.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. The shared-directory proxy is available by this point in the epic, so use file claims while working this ticket rather than coordinating edits by hand. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
