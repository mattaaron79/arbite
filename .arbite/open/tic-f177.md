---
id: tic-f177
title: Add 'arbite set-status' command
status: open
type: feature
tier: low
domain: cli
epic: workflow
priority: 3
tags:
- cli
- status
assignee: null
depends_on:
- tic-9876
- tic-b0ac
blocked_by: null
created: '2026-09-20T12:25:55'
updated: '2026-09-20T12:25:55'
closed: null
---

## Description
Add 'arbite set-status <id> <status>' as a dedicated front door for changing a ticket's status, which in the file sink means moving the file into the matching status folder.

Additive, not a replacement: 'arbite set status <value>' keeps working. Both must funnel through the same code path so they cannot drift -- factor the status-change logic out of cmd_set rather than writing it twice. cmd_set already handles the two subtleties: un-filing the ticket from any bucket so status and location stay in sync, and auto-dating 'closed' when moving to closed.

Status choices come from schema.STATUSES, so 'review' is available automatically once that lands.

Open question to settle while implementing: whether set-status should refuse transitions the dedicated commands exist for (claim/close/submit/accept). Recommendation: it should not -- it is the escape hatch, and doctor already catches the frontmatter drift the dedicated commands prevent.

Acceptance: arbite set-status tic-xxxx review moves the file to review/ and updates 'updated'; the same via arbite set produces an identical result.

## Notes
