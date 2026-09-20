---
id: tic-8c61
title: Add 'arbite progress' -- live epics in dependency order
status: open
type: feature
tier: high
domain: cli
epic: reporting
priority: 3
tags:
- cli
- reporting
- epics
- graph
assignee: null
depends_on:
- tic-9876
- tic-f838
blocked_by: null
created: '2026-09-20T12:26:33'
updated: '2026-09-20T12:26:33'
closed: null
---

## Description
Add 'arbite progress': the view that answers 'what is actually in flight, and what surrounds it'.

Selection rule, which is the whole substance of this ticket:
1. Take every ticket whose status is open, in_progress or review -- call these the live tickets.
2. Take every epic that contains at least one live ticket.
3. Show ALL tickets of those epics, whatever their status -- including closed and shelved ones.

So an epic with seven closed tickets and one in_progress shows all eight. The closed siblings are the context that makes the live ticket legible; that is the point of the command, not an accident to be optimised away.

Tickets with no epic: include the live ones, grouped under a clear 'no epic' heading. Do not let them vanish.

Output: grouped by epic, and within each epic sorted topologically by depends_on. graph.topo_order and graph.cycle_warnings already exist and are what _cmd_list_next uses -- reuse them rather than writing a second sort, and surface cycle warnings the same way the existing listings do. Include a per-epic count line (e.g. 6 closed / 1 in_progress / 1 open) so progress is visible without counting rows.

--json aware, and honours --epic to narrow to one epic.

Acceptance: an epic with a single in_progress ticket and otherwise-closed siblings shows every one of them, topologically ordered; an epic with all tickets closed does not appear at all.

## Notes
