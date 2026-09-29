---
id: tic-de73
title: 'Palette accents: epic purple and assignee orange, with a base-palette fallback'
status: closed
type: feature
tier: low
domain: cli
epic: reporting
priority: 4
tags: []
assignee: zoo.deepseek-flash.001
depends_on: []
blocked_by: null
created: '2026-09-29T11:47:26'
updated: '2026-09-29T11:58:08'
closed: '2026-09-29T11:58:08'
---

## Description
Paint the epic name in the 'epic' purple a levelling-game reader already reads as rare-and-valuable, and the assignee in a reddish orange, so the two columns stand apart from the status they sit beside.

Extended-palette accents degrade: a terminal that advertises 256 colours gets the real purple and orange, one that does not gets the nearest base-palette colours (magenta, bright red). 'review' moves off magenta so the vocabulary stays distinguishable from an epic.

## Notes
- 2026-09-29T11:58:07 zoo.deepseek-flash.001: Accents for the epic and the assignee, with a palette fallback.

What changed: arbite.term gained the two accents the base 16 colours cannot express -- EPIC_256 (the 'epic' violet) and ASSIGNEE_256 (the reddish orange) -- each with a base-palette stand-in (magenta, bright red) chosen once in configure() by supports_256_colors(), which believes only what the terminal says about itself (TERM naming a 256color/truecolor variant, COLORTERM saying truecolor/24bit, or a Windows console already known to render escapes). paint_heading now paints in the epic violet, and new paint_epic/paint_assignee (plus epic_code/assignee_code for callers building their own lines) paint those two columns in every flat table -- progress, list, list next, list --topo. 'review' moved from magenta to blue so a status and a grouping can never read as each other, and the '-' placeholder for no-epic/unassigned stays plain.

Observable via integration testing: on a 256-colour terminal 'arbite progress --color always' shows the epic name (heading and row column) in the violet and the assignee in orange; with TERM=xterm the same report uses magenta and bright red instead, so a base-palette console degrades to the nearest colour rather than being handed a code it would render as something arbitrary; with colour off, on a pipe, under NO_COLOR or with --json the output is byte-identical to before, escapes and all. 'arbite status' shows review in blue. The generated .arbite/AGENTS.md documents the base-palette-with-extended-accents policy.

- 2026-09-29T11:58:08 zoo.deepseek-flash.001: Submitted; closed (review disabled): Epic violet and assignee orange, drawn from the extended palette only where the terminal advertises it, with the base 16 colours as the fallback
