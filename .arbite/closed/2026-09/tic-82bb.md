---
id: tic-82bb
title: 'Prettify the --tree view for people: dimmed structure, coloured rows, truecolor
  epic with a 256/16 fallback'
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
created: '2026-09-29T14:40:32'
updated: '2026-09-29T14:52:57'
closed: '2026-09-29T14:52:57'
---

## Description
The dependency forest (arbite deps, arbite list --tree) is a view a person reads, not a machine: dim the connectors so the structure recedes, bold the ids, colour the status bracket, tick a satisfied edge in green, show the owner in the assignee orange, colour a cycle or a dangling id as the problem it is, and separate the trees of a forest with one blank line.

The epic colour the maintainer picked by hand is a truecolor value, so the accent gets a proper ladder: truecolor where COLORTERM/TERM says so, the 256-colour approximation where only that is advertised, magenta on a base-palette console. Nothing about the plain-text output changes.

## Notes
- 2026-09-29T14:52:57 zoo.deepseek-flash.001: Prettified the dependency forest for people, and gave the epic accent a proper palette ladder.

What changed: 'arbite deps' and 'arbite list --tree' now dim the connectors and the back-references, bold the ids, paint a status by its meaning, tick a satisfied edge green, paint a cycle or a dangling id red, show an owner as the assignee orange, and separate the trees of a forest with one blank line. The rows themselves are untouched. term gained paint_good/paint_problem and an empty-text guard in paint() (so a depth-zero indent emits no bytes at all), plus a truecolor rung: the epic violet chosen by hand is 24-bit, so EPIC_TRUECOLOR is emitted only where supports_truecolor() hears the terminal say so (COLORTERM truecolor/24bit, or a truecolor TERM, or a Windows console already trusted), EPIC_256 (the same soft violet in the 256 palette) where only that is advertised, and magenta on the base 16. A dangling dependency id no longer prints '(missing)' twice.

Observable via integration testing: with colour off, on a pipe, under NO_COLOR or with --json the only differences are the blank line between trees and the single '(missing)' mark; on a terminal 'arbite deps <id>' shows the coloured forest above and 'arbite list --tree' the same per tree with a blank line between them; the epic violet appears at whichever rung the terminal advertises (TERM=xterm-256color COLORTERM=truecolor -> 24-bit, TERM=xterm-256color -> 38;5;139, TERM=xterm -> magenta). Note for anyone reading the output next: which root tree prints first is the walker's order (dependency_rows sorts roots by urgency and then walks them in reverse), which this change left alone.

- 2026-09-29T14:52:57 zoo.deepseek-flash.001: Submitted; closed (review disabled): The dependency forest reads for a person: dimmed structure, coloured tickets, and the epic accent laddered from 24-bit to 256 to the base palette
