---
id: tic-f838
title: Add 'arbite status' -- ticket counts by status
status: closed
type: feature
tier: low
domain: cli
epic: reporting
priority: 3
tags:
- cli
- reporting
assignee: null
depends_on:
- tic-9876
blocked_by: null
created: '2026-09-20T12:26:32'
updated: '2026-09-20T13:56:12'
closed: '2026-09-20T13:56:12'
---

## Description
Add 'arbite status': a one-screen overview giving a count per status, in schema.STATUSES order (raw, open, in_progress, review, blocked, shelved, closed) plus a total.

- Statuses with a count of zero are still listed, so the shape of the backlog is readable at a glance and a newly added status is visibly empty rather than absent.
- --json emits a mapping of status -> count plus the total.
- Respects the standard filters where they make sense (--epic, --domain, --tier, --assignee) so 'arbite status --epic workflow' answers 'how far along is this epic'.

Name collision to be aware of, not to avoid: 'arbite status' (this command) sits next to 'arbite set status' and the --status filter. They are unambiguous to argparse; make sure the help text of all three reads clearly side by side.

Acceptance: arbite status prints every status with its count and a total; --json round-trips; filters narrow the counts.

## Notes
- 2026-09-20T13:56:12 zoo.orch.001: Added arbite status: per-status counts in STATUSES order including zeros, plus a total; --json mapping + total; --epic/--domain/--tier/--assignee filters; counts reuse the same implementation as sink info and reconcile with list (bucketed tickets are out of the status workflow); exit 0 always; help text distinguishes it from set status / --status / sink info
