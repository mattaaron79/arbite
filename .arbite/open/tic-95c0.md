---
id: tic-95c0
title: Add scratch payload transport
status: open
type: feature
tier: low
domain: ui
epic: shared-directory-coordination
priority: null
tags:
- scratch
- transport
assignee: null
depends_on:
- tic-60c7
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:33'
updated: '2026-09-21T01:16:36'
closed: null
---

## Description
Planning key: C09
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (SC1, SC2, SC3, SC4, SC5, DR3)

## Outcome and scope
Add .arbite/scratch/ as the project-local payload area for --input and --edits, with '-' meaning stdin. Payloads are consumed and cleared on success and kept on failure, with --keep opting out. Add arbite scratch list and arbite scratch clear NAME or --all. Scratch is invisible to discovery, scanning, claims and changes, and appears on the doctor report only as a count and size note that never affects the exit code.

## Acceptance criteria
- No documented command writes a payload outside the project.
- A failed write leaves the payload usable, so a recoverable error does not force a model to re-emit a file.
- Scratch never appears as a ticket, a file listing, a claim target, or a stray-file problem.
- Doctor reports the scratch count and size on every run, and the exit code is unchanged by it.
- Scenarios SC1, SC2, SC3, SC4, SC5, DR3 pass exactly as written in the examples doc.

## Validation
Consume and keep transitions, stdin payloads, refusal of outside-project paths, invisibility tests across discovery and doctor, both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
