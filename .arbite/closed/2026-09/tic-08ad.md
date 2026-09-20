---
id: tic-08ad
title: Add 'arbite accept' -- close a ticket that is in review
status: closed
type: feature
tier: low
domain: cli
epic: workflow
priority: 3
tags:
- cli
- review
- workflow
assignee: null
depends_on:
- tic-0088
blocked_by: null
created: '2026-09-20T12:25:55'
updated: '2026-09-20T14:23:15'
closed: '2026-09-20T14:23:15'
---

## Description
Add 'arbite accept <id>': a ticket in status 'review' becomes 'closed', dated, with an auto-generated note.

- Refuse a ticket that is not in review, naming its actual status -- accept is the reviewer's counterpart to submit, not a general-purpose close.
- Auto-note 'Accepted.', with an optional --message appended as detail.
- The note is attributed to the accepting agent (--agent), not to the ticket's assignee: the point of the record is who approved it.
- Dates 'closed' the same way cmd_close does.

The rejection path is 'arbite reopen --reason ...', which is a separate ticket; do not build a reject command here.

Acceptance: accept on a review ticket closes it with an attributed note; accept on an open or closed ticket errors.

## Notes
- 2026-09-20T14:23:15 zoo.orch.001: Added "arbite accept": closes a ticket that is in review, dating closed exactly as arbite close does, with an "Accepted." auto-note (plus --message as detail) credited to the accepting --agent rather than to the ticket assignee, because the record is about who approved the work. Refuses any ticket not in review, naming its real status, so accept cannot be used as a general-purpose close; no reject command was added, since the rejection path is arbite reopen --reason. The full claim -> submit -> review -> reopen/accept loop is pinned by an end-to-end test.
