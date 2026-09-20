---
id: tic-89aa
title: Make 'arbite reopen --reason' mandatory and record it
status: closed
type: refactor
tier: low
domain: cli
epic: workflow
priority: 3
tags:
- cli
- workflow
- breaking
assignee: null
depends_on: []
blocked_by: null
created: '2026-09-20T12:25:55'
updated: '2026-09-20T12:54:21'
closed: '2026-09-20T12:54:21'
---

## Description
'arbite reopen' currently appends a bare 'Reopened.' note. Add a --reason argument, make it REQUIRED, and put it in the auto-note: 'Reopened: <reason>.'

This is deliberately breaking -- every existing caller that runs bare 'arbite reopen tic-xxxx' will now fail with argparse's missing-argument error. That is the intent: reopening is the rejection path out of review, and a rejection with no stated reason is useless to whoever has to act on it.

Touches src/arbite/cli.py: cmd_reopen and p_reopen. Existing behaviour to preserve -- clearing 'closed' and 'blocked_by', and refusing a ticket that is already open.

Acceptance: bare reopen errors; reopen --reason 'tests fail on ARM' reopens the ticket with that text in the note.

## Notes
- 2026-09-20T12:54:21 zoo.orch.001: Made reopen --reason mandatory (breaking); auto-note is now 'Reopened: <reason>.'; closed/blocked_by clearing and already-open refusal preserved; docs and tests updated
