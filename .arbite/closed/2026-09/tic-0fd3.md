---
id: tic-0fd3
title: 'Readable progress output: epic separation and reusable colour support'
status: closed
type: feature
tier: medium
domain: cli
epic: reporting
priority: 3
tags: []
assignee: zoo.deepseek-flash.001
depends_on: []
blocked_by: null
created: '2026-09-29T10:48:09'
updated: '2026-09-29T11:02:34'
closed: '2026-09-29T11:02:34'
---

## Description
Make 'arbite progress' easier to read, and add colour support that any command can reuse.

Layout: a blank line between epic groups, a horizontal rule under each epic header, a header that is not duplicated by the row's own epic column, and the '1 tickets' grammar fix.

Colour: a reusable terminal/colour helper (TTY detection, NO_COLOR, --color auto|always|never, ARBITE_COLOR) so later human-readability work in other commands reuses one implementation. Off by default when stdout is not a TTY or TERM is dumb, never applied to --json, and information is never carried by colour alone.

## Notes
- 2026-09-29T11:02:29 zoo.deepseek-flash.001: Made 'arbite progress' readable by eye, and added colour support any command can reuse.

What changed: new src/arbite/term.py owns the whole colour decision (TTY probe, TERM=dumb, NO_COLOR, ARBITE_COLOR, and a Windows console that must positively say it renders escapes) plus the status palette, so no command writes an escape code itself; '--color auto|always|never' is a global flag offered before and after the command exactly like --root, and main() resolves the mode once per process; 'arbite progress' now separates epics with a blank line, underlines each heading with a rule sized to it, prints '1 ticket' instead of '1 tickets' and paints the heading, ids and per-status counts; 'arbite status' and every flat table (list, list next, list --topo) inherit the same status colours from the shared helper.

Observable via integration testing: with stdout piped (a script, a log, CI, this harness) the report is plain text and byte-identical to before apart from the blank line, the rule and the grammar fix -- no escape code ever lands in a pipe; '--json' is untouched and is never coloured, even under --color always; '--color always' paints while '--color never' refuses, and the flag beats the environment in both directions; NO_COLOR silences ARBITE_COLOR=always, and an empty NO_COLOR means unset; 'arbite progress --color mauve' is an argparse error naming the valid values (exit 2). The generated .arbite/AGENTS.md and .arbite/WORKSPACE.md now document --color, refreshed with 'arbite init'.

- 2026-09-29T11:02:34 zoo.deepseek-flash.001: Submitted; closed (review disabled): Progress reads by eye: epics separated and underlined, counts grammatical, colour available everywhere behind --color/ARBITE_COLOR/NO_COLOR with a plain-text fallback
