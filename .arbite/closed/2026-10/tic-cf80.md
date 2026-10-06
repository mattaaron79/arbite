---
id: tic-cf80
title: 'arbite workspace reset: clear live coordination state for a stale project'
status: closed
type: feature
tier: high
domain: coordination
epic: workspace-relocation
priority: 2
tags:
- workspace
- claims
- attempts
assignee: claude.opus-5.001
depends_on:
- tic-9969
blocked_by: null
created: '2026-10-05T22:41:30'
updated: '2026-10-05T23:05:43'
closed: '2026-10-05T23:05:43'
---

## Description
For a stale or relocated project, leftover file claims and active attempts (e.g. an attempt for a ticket that no longer exists) block the file proxy and nothing clears them. 'arbite workspace reset' releases every active claim, ends every active attempt with outcome 'reset', re-records the workspace binding from the current location and restamps old workspace ids. History (receipts, events, artifacts) is kept. In-progress tickets stay in_progress; agents re-attach with 'arbite attempt adopt'. Requires --force (or reports what it would do).

## Notes
- 2026-10-05T23:05:43 claude.opus-5.001: Implemented as relocation.reset() + CoordinationApp.workspace_reset() + 'arbite workspace reset [--force] [--agent] [--json]'. Releases first, then restamps to the derived workspace, so two active owners of one path (which doctor --fix leaves alone) are always settled. Attempts end interrupted/outcome 'reset' with attempt.ended events; claims release with release.file events; a workspace.reset event summarises. Tickets keep their status; next line points at 'arbite attempt adopt'. Docs: README, WORKSPACE.md template in docs.py.

- 2026-10-05T23:05:43 claude.opus-5.001: Observable via integration testing: 'arbite workspace reset' (no --force) prints the attempts it would end, the claim count and any stale stamps, says 'nothing was changed' and exits 1 without writing. With --force it prints 'ended attempt att-X (tic, worker)' per attempt, 'released N active claims', any restamp lines and a 'next: arbite attempt adopt' hint; afterwards 'arbite file claims' shows none active, 'arbite events' shows attempt.ended/release.file/workspace.reset, in-progress tickets are still in_progress, and 'arbite attempt adopt <id> --agent <me>' works. On a copy of diku it ended att-69f9 (tic-d53d) and att-6027 (tic-f8f6) and released 8 claims; doctor was then clean apart from the stream note.

- 2026-10-05T23:05:43 system: Submitted; closed (review disabled).
