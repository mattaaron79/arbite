---
id: tic-e8ed
title: progress --json prints [] when no ticket is live
status: closed
type: bug
tier: low
domain: cli
epic: milieu
priority: null
tags: []
assignee: claude.opus-5-5.001
depends_on: []
blocked_by: null
created: '2026-10-10T01:36:05'
updated: '2026-10-10T01:59:19'
closed: '2026-10-10T01:59:19'
---

## Description
Milieu round 3, item 2. With no live ticket, 'progress --json' prints the plain text 'no live tickets (...)' on stdout and exits 2, where list --json and search --json print []. With --json print [] and still exit 2; keep the text without --json.

## Notes
- 2026-10-10T01:48:47 claude.opus-5-5.001: progress --json now prints [] (exit 2) when no ticket is live, including under --epic; the 'no live tickets (...)' text is kept for non-JSON output.

Observable: in a project with no tickets (or only closed/filed ones), 'arbite progress --json' prints [] on stdout and exits 2; 'arbite progress' still prints the 'no live tickets' line and exits 2.

- 2026-10-10T01:59:19 system: Submitted; closed (review disabled).
