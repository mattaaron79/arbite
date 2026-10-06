---
id: tic-e445
title: 'bug (raw): Requires Classification'
status: raw
type: bug
tier: 'TODO: low|medium|high|frontier'
domain: 'TODO: e.g. mesh, image_gen, audio_gen, ui, io'
epic: classification
priority: null
tags: []
assignee: null
depends_on: []
blocked_by: null
created: '2026-10-05T23:05:32'
updated: '2026-10-05T23:05:32'
closed: null
---

## Description
This is a **raw** ticket: it was captured from a brief request without proper classification. It must be filled out before it can be worked.

Original request: LC1 frozen transcript (test_LC1_close_releases_claims) hard-codes .arbite/closed/2026-09/ so it fails once the calendar month changes; the close path's month needs normalising like ids/digests are

What still needs to be done -- human or agent triage, which `arbite fetch` starts and `arbite promote <id>` finishes in one write:
- title -- replace "Requires Classification" with a short human-readable summary
- tier -- low | medium | high | frontier (agent capability tier required to work it; how capable the agent must be, not how urgent the work is)
- domain -- e.g. mesh, image_gen, audio_gen, ui, io (drives routing)
- epic -- this raw ticket is auto-grouped under the 'classification' epic (so triage can find it with `arbite list next --epic classification`); pass the real epic this work belongs to (e.g. mesh-pipeline) to `arbite promote` and it replaces that grouping
- priority -- numeric urgency index, lower = more urgent
- description -- expand this body into a proper task description based on the original request, including any acceptance criteria
- status -- `arbite promote <id> ...` classifies these fields in place and moves the ticket to `open` (or claims it in the same command with `--agent <your-id>`) so it becomes workable via `arbite list next`; the same fields can still be written by hand with `arbite set`

## Notes
