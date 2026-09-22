---
id: tic-71c7
title: 'Split the generated guide: lean .arbite/AGENTS.md plus .arbite/WORKSPACE.md'
status: closed
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
updated: '2026-09-21T20:40:01'
closed: '2026-09-21T20:40:01'
---

## Description
The generated .arbite/AGENTS.md is 595 lines / ~60 KiB and is read into an agent context at the start of every task, most of it being the per-flag command reference. Split it: AGENTS.md keeps the mission statements, the rules an agent must act on, the workflow block and a one-line-per-command index (no flags); .arbite/WORKSPACE.md carries the workspace/proxy command reference (file, scratch, receipt, changes, cmd, events, workspace, attempt) plus the honest limits. Both are rendered by arbite init from the same parsers and sink info, and all prose is trimmed for token count. Ticket-command flags live in arbite <cmd> -h and the README.

Original request: trim .arbite/AGENTS.md and move the workspace/proxy reference into .arbite/WORKSPACE.md

## Notes
- 2026-09-21T20:17:43 qwen.qwen3.001: Baseline: .arbite/AGENTS.md is 595 lines / 60534 bytes. Chosen split: AGENTS.md keeps mission, rules, workflow block and a one-line-per-command index (no flags); .arbite/WORKSPACE.md carries the workspace/proxy command reference (file/scratch/receipt/changes/cmd/events/workspace/attempt) plus honest limits, rendered by arbite init from the same parsers and sink info.

- 2026-09-21T20:40:00 qwen.qwen3.001: SPLIT DONE. Before: .arbite/AGENTS.md 595 lines / 60534 bytes. After: .arbite/AGENTS.md 201 lines / 24075 bytes (-66% lines, -60% bytes) plus .arbite/WORKSPACE.md 189 lines / 17439 bytes, read only when a file is about to change. STRUCTURE: docs.py has render() (the guide: mission, folder-is-state rule, sink facts, field vocabulary, identity/resume, workflow block, triage, conventions, and a one-line index of every command rendered from the parsers -- no per-flag reference) and render_workspace() (claim->read->mutate->receipt, arbite cmd observed vs guarded, reading the record back, the honest limits, exit 4/5, evidence location per sink, and the per-flag reference for file/scratch/receipt/changes/cmd/events/workspace/attempt). Both keep the same signature, share _stamp() and _command_block_lines(), and stay sink-aware (status_is_location, has_coordination_backend, the stale-store warning); a sink without a coordination backend gets one paragraph saying the proxy commands refuse. The index comes from parser._choices_actions, so it still names every command the parser defines. FILES: src/arbite/docs.py; src/arbite/cli.py (cmd_init writes both docs from one active/stale SinkInfo; the init description and build_parser docstring name both); src/arbite/sinks/file.py and src/arbite/coordination/discovery.py (WORKSPACE.md joins GENERATED_FILES, so it is never scanned as a ticket, never reported as a stray root file, and prints the existing generated note); .arbite/AGENTS.md and .arbite/WORKSPACE.md regenerated, scripts/.arbite/ regenerated for consistency; README (highlights, layout tree, generated-docs bullet, docs.py entry); .arbite/planning/interaction-examples.md (LS5 gains the WORKSPACE.md row, 5 entries). TESTS: test_cli.py -- init writes and names WORKSPACE.md, the vocabulary check also covers the reference, idempotent rewrite covers both docs, BY3/BY1 split across the two files (limits in WORKSPACE.md, rule+pointer in the guide, memory-sink refusal in both), the migration snapshot skips GENERATED_FILES instead of naming AGENTS.md, and a new test_the_guide_stays_lean pins the budget (220 lines / 26000 bytes) and fails if the flag blocks creep back; test_file_discovery.py -- the pinned generated list must contain WORKSPACE.md, plus a new test proving both documents are skipped by the ticket scan (ids() empty, check() clean). VERIFICATION: pytest 1074 passed, 2 skipped (both pre-existing); arbite doctor exit 0 on 43 tickets; arbite file list .arbite reports both documents as generated rows; the nine proxy claims were released before submission. Committed as cb646c9 on main. WHAT A REVIEWER CAN OBSERVE: running init (PYTHONPATH=src python3 -m arbite.cli init) in any project now writes a much shorter .arbite/AGENTS.md -- still carrying every command name, the workflow block and the rules an agent acts on -- plus .arbite/WORKSPACE.md with the workspace-command flags; the guide names WORKSPACE.md in its header and its Changing files section. arbite file list .arbite reports both as generated, doctor stays clean, and a second init reproduces both files byte for byte, so a committed doc only diffs when something real changed. No ticket, status, field or exit-code behaviour changed.

- 2026-09-21T20:40:01 qwen.qwen3.001: Submitted; closed (review disabled): docs split done; see the note for before/after sizes and the test coverage
