---
id: tic-65e6
title: 'wish (raw): Requires Classification'
status: raw
type: wish
tier: 'TODO: low|medium|high|frontier'
domain: 'TODO: e.g. mesh, image_gen, audio_gen, ui, io'
epic: classification
priority: null
tags: []
assignee: null
depends_on: []
blocked_by: null
created: '2026-09-21T01:16:58'
updated: '2026-09-21T01:16:58'
closed: null
---

## Description
This is a **raw** ticket: it was captured from a brief request without proper classification. It must be filled out before it can be worked.

Original request: Require agents to route edits through arbite instead of raw shell writes. Decision deferred until C13 (tic-faae) has produced passthrough.exec evidence: which tools agents actually reach for, how often a change escapes a claimed set, whether guarded mode blocks real work, and whether any agent bypasses the proxy entirely. Options to weigh then: mandate arbite cmd in the generated guide, restrict writes at the harness level, or leave it advisory.

What still needs to be done -- human or agent triage, which `arbite fetch` starts and `arbite promote <id>` finishes in one write:
- title -- replace "Requires Classification" with a short human-readable summary
- tier -- low | medium | high | frontier (agent capability tier required to work it; how capable the agent must be, not how urgent the work is)
- domain -- e.g. mesh, image_gen, audio_gen, ui, io (drives routing)
- epic -- this raw ticket is auto-grouped under the 'classification' epic (so triage can find it with `arbite list next --epic classification`); pass the real epic this work belongs to (e.g. mesh-pipeline) to `arbite promote` and it replaces that grouping
- priority -- numeric urgency index, lower = more urgent
- description -- expand this body into a proper task description based on the original request, including any acceptance criteria
- status -- `arbite promote <id> ...` classifies these fields in place and moves the ticket to `open` (or claims it in the same command with `--agent <your-id>`) so it becomes workable via `arbite list next`; the same fields can still be written by hand with `arbite set`

> **Note:** this is a **wishlist** item, not ordinary feature work. When it is classified, reclassify it as `feature` (not `wish`), with the correct `tags`, an expanded `description`, an analysis of the request, and a possible `epic`, then file the ticket in the wishlist bucket (a folder in the file sink, a bucket in a database sink). `arbite promote tic-65e6 ...` does all of that for a wish: it reclassifies it as `feature` and files it in the wishlist bucket without opening it, instead of just doing the filing by hand (`arbite move tic-65e6 /wishlist`). A wish is captured so it isn't forgotten, not so it is worked: leave it in the wishlist until it is deliberately promoted to real work.

## Notes
