---
id: tic-3b55
title: Add raw/processed/ snapshot area, excluded from the ticket scan
status: closed
type: feature
tier: medium
domain: sinks
epic: layout
priority: 2
tags:
- file-sink
- layout
- raw
assignee: null
depends_on: []
blocked_by: null
created: '2026-09-20T12:25:32'
updated: '2026-09-20T12:40:14'
closed: '2026-09-20T12:40:14'
---

## Description
Create .arbite/raw/processed/ as the home for verbatim snapshots of raw requests that have since been promoted. A snapshot is NOT a ticket: it is a frozen copy of the original capture, kept for audit.

The whole point is that the scan must never see these files. If it does, 'arbite fetch' will re-serve an already-promoted request forever.

Touches:
- src/arbite/sinks/file.py: init() creates raw/processed/. _iter_files/_scan/_is_ticket_file must skip everything under raw/processed/ by path, the way GENERATED_FILES skips AGENTS.md -- path exclusion, not a status check, because the snapshot deliberately keeps its original raw frontmatter.
- _bucket_for and _expected_status_for must not treat 'processed' as a bucket of the raw status.
- doctor must not report the snapshots as stray or misfiled files.

Snapshot filenames carry a suffix that cannot collide with a ticket filename (ID_PATTERN matches tic-XXXX.md), e.g. tic-a1b2.raw.md -- decide and document the convention here, since arbite promote will depend on it.

Acceptance: a snapshot under raw/processed/ is invisible to list, fetch, doctor and the id index, while the promoted ticket of the same id lives normally in open/.

## Notes
- 2026-09-20T12:40:14 zoo.orch.001: Added raw/processed/ snapshot area excluded from the scan by path; snapshot convention <id>.raw.md exposed as reusable file-sink helpers for the future promote command
