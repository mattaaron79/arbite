---
id: tic-faae
title: Add passthrough command observation
status: open
type: feature
tier: medium
domain: ui
epic: shared-directory-coordination
priority: null
tags:
- passthrough
- cmd
- observation
assignee: null
depends_on:
- tic-7c42
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:33'
updated: '2026-09-21T01:16:37'
closed: null
---

## Description
Planning key: C13
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (PC1, PC5, PC6)

## Outcome and scope
Implement arbite cmd in observed mode: argv execution by default with --shell opting into sh -c, bounded output capture, a before and after digest manifest of managed paths, receipts for observed changes, and a passthrough.exec event carrying tool, argv hash, exit code and duration. Arbite-level refusals use 125, 126 and 127 so a wrapped tool's own exit code survives untouched. The output states plainly that observed means no exclusivity is claimed.

## Acceptance criteria
- A familiar tool's changes are captured without the agent changing habits.
- Refusals never run the command, and every refusal says command did not run.
- Redirections and shell syntax are documented as visible only after the fact.
- Interactive, long-running and background invocations are refused clearly.
- The passthrough.exec stream records enough to evaluate whether to mandate passthrough later.
- Scenarios PC1, PC5, PC6 pass exactly as written in the examples doc.

## Validation
Exit-code passthrough including collision cases, shell opt-in behaviour, refusals for unsupported invocations, observed-diff receipts, event content, both sinks.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
