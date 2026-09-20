---
id: tic-675c
title: Update generated agent guide and all --help text
status: open
type: chore
tier: medium
domain: docs
epic: docs
priority: 4
tags:
- docs
- help
- agents-md
assignee: null
depends_on:
- tic-9876
- tic-1342
- tic-b757
- tic-8c96
- tic-b0ac
- tic-ea0e
- tic-3b55
- tic-f177
- tic-0088
- tic-08ad
- tic-89aa
- tic-7e03
- tic-7be7
- tic-25a2
- tic-f838
- tic-8c61
blocked_by: null
created: '2026-09-20T12:26:54'
updated: '2026-09-20T12:26:54'
closed: null
---

## Description
Sweep every piece of in-tool text so it describes the system as it now is. This lands last on purpose: the guide is generated from the parser, so it is only correct once every command exists.

Touches:
- src/arbite/docs.py: render() and the constants around it -- FIELD_NOTES needs an entry for 'references'; ARBITE_INSTRUCTIONS_BLOCK and the workflow narrative need the claim -> in_progress -> submit -> review -> accept chain, the promote step in triage, and the new folder layout (review/, plans/, raw/processed/).
- src/arbite/cli.py: --help and description strings for every command touched by these changes, plus the ones that merely mention a renamed thing (arbite.yaml -> .arbite/project.yaml, planning -> plans).
- Regenerate .arbite/AGENTS.md and the CLAUDE.md block via the existing --agent-doc/--claude-doc paths, and confirm the regenerated output actually names the new statuses, fields and commands.

The rule the codebase already holds itself to: the guide must never name a store, folder or command that the commands will not read. Check the generated output rather than assuming the templates covered it.

Acceptance: a fresh arbite init produces an AGENTS.md describing review, references, submit/accept/promote/set-status/status/progress, the plans bucket and .arbite/project.yaml, with no surviving mention of arbite.yaml or the planning bucket.

## Notes
