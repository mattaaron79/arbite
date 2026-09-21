---
id: tic-1a75
title: Implement transactional coordination storage and durable events
status: open
type: feature
tier: high
domain: io
epic: shared-directory-coordination
priority: null
tags:
- coordination
- storage
- events
assignee: null
depends_on:
- tic-7918
references:
- planning/shared-directory-coordination.md
- planning/interaction-examples.md
blocked_by: null
created: '2026-09-21T01:16:32'
updated: '2026-09-21T01:16:34'
closed: null
---

## Description
Planning key: C02
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (EV2, EV3, EV4, EV5, EV7)

## Outcome and scope
Implement record revision checks, store-local multi-record operations, and event append with a stable cursor, plus the arbite events read surface with --after, --tail and --include-reads. The file sink uses a recoverable journal and process serialisation; SQLite uses transactions. A small coarse operation lock is acceptable, and no lock may be held for a ticket duration. Read observations are a separate category from mutations.

## Acceptance criteria
- Concurrent updates cannot lose fields under unchanged status and assignee; revisions are explicit.
- Related state and events commit together or recover deterministically; retrying an operation id deduplicates.
- Process death leaves no permanent runtime lock, and durable file claims are unaffected.
- Both sinks produce equivalent outcomes, with no hidden SQLite requirement for the file sink.
- Events render one line per event with kind, subject, ticket, attempt, actor, time and outcome; --follow is refused.
- Scenarios EV2, EV3, EV4, EV5, EV7 pass exactly as written in the examples doc.

## Validation
Multiprocess conflicting revisions and events, process death around commit, rollback and idempotent retry, cursor pagination across restarts, both-sink conformance.

## Boundaries and handoff
Implement this slice only: no daemon, agent launcher, automatic stale recovery, worktree workflow, central database, factory or dashboard. Preserve both-sink behaviour. Sibling tickets may touch the same files; coordinate edits or work serially until the proxy exists. Record implementation decisions, validation results, observable behaviour and remaining limitations in ticket notes before close.


## Notes
