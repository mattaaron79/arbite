---
id: tic-665e
title: 'Narration streams: lifecycle/doctor/workspace wiring'
status: closed
type: feature
tier: high
domain: io
epic: arbite-stream
priority: 3
tags: []
assignee: deepseek.deepseek-flash.001
depends_on:
- tic-36fa
- tic-3764
references:
- plans/arbite-stream-sink.md
blocked_by: null
created: '2026-10-05T12:37:28'
updated: '2026-10-05T12:48:45'
closed: '2026-10-05T12:48:45'
---

## Description
Work order: plan section §4. Acceptance in the plan's Ticket cut.

## Notes
- 2026-10-05T12:48:45 deepseek.deepseek-flash.001: Implemented: _claimed_result appends the stream: line (absolute path + write command) and data['stream'] — claim and list next --claim route through it; promote --agent raises its own attempt so it prints the same line via TicketLifecycle.stream_line, and list next --claim carries the facts in --json. submit's soft gate is one helper (_stream_gate) called from BOTH branches (review and review:false) with data['stream_entries']; close/accept are untouched. app.doctor_facts(tickets) now carries streams + streams_missing (in_progress uses the active attempt, review the most recently started one; open tickets and attempt-less legacy work are not blamed) and cmd_doctor appends stream_note_lines after the scratch notes plus both JSON keys. workspace_show prints a streams line and JSON. tests/test_stream_wiring.py: 30 passed on both sinks; smoke verified claim/submit/doctor/workspace show text and JSON on file and sqlite.

- 2026-10-05T12:48:45 system: Submitted; closed (review disabled).
