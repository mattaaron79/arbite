---
id: tic-71c7
title: 'Split the generated guide: lean .arbite/AGENTS.md plus .arbite/WORKSPACE.md'
status: in_progress
type: request
tier: medium
domain: docs
epic: docs
priority: 1
tags:
- docs
- tokens
- docs-renderer
assignee: qwen.qwen3.001
depends_on: []
blocked_by: null
created: '2026-09-21T20:16:59'
updated: '2026-09-21T20:17:43'
closed: null
---

## Description
The generated .arbite/AGENTS.md is 595 lines / ~60 KiB and is read into an agent context at the start of every task, most of it being the per-flag command reference. Split it: AGENTS.md keeps the mission statements, the rules an agent must act on, the workflow block and a one-line-per-command index (no flags); .arbite/WORKSPACE.md carries the workspace/proxy command reference (file, scratch, receipt, changes, cmd, events, workspace, attempt) plus the honest limits. Both are rendered by arbite init from the same parsers and sink info, and all prose is trimmed for token count. Ticket-command flags live in arbite <cmd> -h and the README.

Original request: trim .arbite/AGENTS.md and move the workspace/proxy reference into .arbite/WORKSPACE.md

## Notes
- 2026-09-21T20:17:43 qwen.qwen3.001: Baseline: .arbite/AGENTS.md is 595 lines / 60534 bytes. Chosen split: AGENTS.md keeps mission, rules, workflow block and a one-line-per-command index (no flags); .arbite/WORKSPACE.md carries the workspace/proxy command reference (file/scratch/receipt/changes/cmd/events/workspace/attempt) plus honest limits, rendered by arbite init from the same parsers and sink info.
