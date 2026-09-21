---
id: tic-ada8
title: Add passive worker profiles and eligibility declarations
status: shelved
type: feature
tier: high
domain: io
epic: multi-provider-job-board
priority: null
tags:
- job-board
- profiles
- eligibility
assignee: null
depends_on:
- tic-cf9f
references:
- planning/multi-provider-job-board.md
- planning/README.md
blocked_by: null
created: '2026-09-21T01:16:53'
updated: '2026-09-21T02:18:04'
closed: null
---

## Description
Planning key: B01
Handoff: .arbite/planning/multi-provider-job-board.md

## Outcome and scope
Persist optional provider-neutral worker profiles with tier, tools and capabilities, locality, cost metadata and units, declared capacity, enabled state and checkin timestamps. Support register, show, list, update and disable without launching agents.

## Acceptance criteria
- A configured profile is authoritative; per-call declarations cannot silently elevate its tier.
- Ad-hoc ids still work on unrestricted tickets; unknown fields fail constrained eligibility with reasons.
- Provider and model labels do not trigger API calls, and credentials are never stored in profiles.
- Declared availability is not displayed as verified liveness; disabling a profile preserves historical references.

## Validation
Both-sink profile persistence, tier and capability and unknown-cost validation, ad-hoc compatibility, disable and history tests.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. The shared-directory proxy is available by this point in the epic, so use file claims while working this ticket rather than coordinating edits by hand. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
- 2026-09-21T02:18:04 system: Shelved: Deferred by owner: the job-board design was inherited from the abandoned 2026-09-18 planning run and must be re-cut against the coordination primitives that actually land before any work starts. Not failed, just not now.
