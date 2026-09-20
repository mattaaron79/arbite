---
id: tic-b0ac
title: Create review/ status folder in the file sink layout
status: open
type: feature
tier: low
domain: sinks
epic: layout
priority: 2
tags:
- file-sink
- layout
- review
assignee: null
depends_on:
- tic-9876
blocked_by: null
created: '2026-09-20T12:25:31'
updated: '2026-09-20T12:25:31'
closed: null
---

## Description
Give the new review status its folder in the file sink.

Touches:
- src/arbite/sinks/file.py: add "review" to FLAT_STATUS_DIRS, ordered to match schema.STATUSES (after in_progress). init() creates it from that tuple already, so no separate mkdir is needed -- but confirm that, rather than assuming it.
- The folder is created unconditionally, regardless of the config's 'review' flag: a project with review disabled must still be able to hold tickets that were put in review before it was disabled.

Watch _bucket_for/_expected_status_for/_is_ticket_file: they special-case paths of the form <status>/<bucket>/, so adding a status directory silently changes what counts as a bucket. A ticket at review/tic-xxxx.md must report status review and bucket None.

Acceptance: arbite init creates .arbite/review/; a ticket set to review lands there; arbite doctor reports no drift for it.

## Notes
