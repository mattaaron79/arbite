---
id: tic-e394
title: Update README and design doc for the new model
status: closed
type: chore
tier: medium
domain: docs
epic: docs
priority: 4
tags:
- docs
- readme
assignee: null
depends_on:
- tic-675c
blocked_by: null
created: '2026-09-20T12:26:54'
updated: '2026-09-20T14:35:43'
closed: '2026-09-20T14:35:43'
---

## Description
Bring the long-form documentation in line with the implemented system.

Touches:
- README.md: the status vocabulary and its lifecycle diagram, the folder layout listing, the command reference, the configuration section (.arbite/project.yaml, the 'review' flag), the ticket schema table (add 'references'), and the triage narrative (fetch then promote, with the raw/processed snapshot).
- INITIAL_DESIGN_DOC.md: it is a design record rather than live documentation. Decide explicitly whether to amend it in place or append a section recording what changed and why; do not leave it silently contradicting the code.
- AGENTS_EXAMPLE.md: check whether it shows a workflow that the review status changes.

Call out the two breaking changes prominently, since anyone upgrading hits them immediately: the config file moved with no fallback, and 'arbite reopen' now requires --reason.

Acceptance: no document in the repo describes arbite.yaml, the planning bucket, or a lifecycle without review; the README command list matches 'arbite --help'.

## Notes
- 2026-09-20T14:35:43 zoo.orch.001: Closed as a duplicate of tic-675c: the two docs tickets were implemented as one sweep (guide + all --help text + README + design doc + AGENTS_EXAMPLE), so the README/design-doc work this ticket describes is already delivered under tic-675c.
