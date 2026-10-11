---
id: tic-a342
title: 'progress: count blocked tickets as live so their epics appear'
status: closed
type: feature
tier: low
domain: io
epic: null
priority: null
tags:
- cli
- progress
assignee: omp.qwen3.8-next-flash.003
depends_on: []
blocked_by: null
created: '2026-10-11T10:46:59'
updated: '2026-10-11T10:59:02'
closed: '2026-10-11T10:59:02'
---

## Description
'arbite progress' selects epics through schema.LIVE_STATUSES (open, in_progress, review), so an epic whose only active work is blocked is invisible in the report -- the one state a coordinator most needs to see. Include 'blocked' in the live set: an epic holding a blocked ticket is in scope, and the epic's blocked count counts toward the JSON 'live' field. Update the selection prose (cmd_progress docstring, progress --help) and the 'no live tickets (live = ...)' line, which is generated from the list, and pin the behaviour with a test.

## Notes
- 2026-10-11T10:59:02 omp.qwen3.8-next-flash.003: schema.LIVE_STATUSES is now open|in_progress|review|blocked, in vocabulary order, and cmd_progress selects epics through that one list, so blocked work pulls its epic into 'arbite progress'.

Observable via integration testing: an epic whose only non-finished ticket is 'blocked' now appears in 'arbite progress' with its full membership (the blocked ticket plus its closed/shelved siblings) and a count line such as '2 tickets: 1 blocked / 1 closed', topologically ordered, on both sinks; 'progress --json' reports that group with counts.blocked = 1 and live = 1 (blocked counts as live, not merely as context), 'progress --epic <stalled-epic>' narrows to it, and 'progress --epic <all-closed-epic>' still exits 2. The empty answer reads 'no live tickets (live = open | in_progress | review | blocked)' -- the list is generated from the vocabulary, so it cannot drift from the selection rule -- and 'progress --json' keeps printing [] at exit 2. An epic with nothing live at all (all closed/shelved, or only bucketed tickets) is still absent; colour, the rules under headings, the raw/shelved exclusions and 'arbite status' are untouched. progress -h states the new selection rule.

- 2026-10-11T10:59:02 system: Submitted; closed (review disabled).
