---
id: tic-7be7
title: Add commands to manage a ticket's references
status: closed
type: feature
tier: medium
domain: cli
epic: workflow
priority: 3
tags:
- cli
- references
- plans
assignee: null
depends_on:
- tic-1342
- tic-ea0e
blocked_by: null
created: '2026-09-20T12:26:17'
updated: '2026-09-20T13:39:35'
closed: '2026-09-20T13:39:35'
---

## Description
Add CLI surface for the 'references' field beyond 'arbite set references a,b' (which comes free with the schema field as a list property).

Proposed shape -- 'arbite ref' with subcommands, so add/remove do not each burn a top-level command name:
- arbite ref add <id> <path>...   -- append, de-duplicating, preserving order
- arbite ref rm <id> <path>...    -- remove, erroring on a path that is not referenced
- arbite ref list <id>            -- print the references, --json aware

Paths are root-relative into the plans bucket ('plans/foo.md' -> .arbite/plans/foo.md). Normalise a leading '/' and reject '..', reusing the validation cmd_move already does for buckets rather than writing a second copy.

Warn -- do not error -- when a referenced document does not exist on disk: references are written while a plan is still being drafted, and in the sqlite sink there may be no plans directory at all. arbite doctor is the right place to report dangling references; add that check there.

Acceptance: add/rm/list round-trip through both sinks; a dangling reference warns on write and is reported by doctor.

## Notes
- 2026-09-20T13:39:34 zoo.orch.001: Added arbite ref add/rm/list (reusing cmd_move's bucket-path normalisation); dangling references warn on write and are reported by doctor as a shared check resolving <arbite_dir>/<ref> on both sinks; empty list renders as absent
