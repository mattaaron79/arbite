---
id: tic-b757
title: Move project config to .arbite/project.yaml (hard cut)
status: open
type: refactor
tier: high
domain: config
epic: config
priority: 1
tags:
- config
- breaking
assignee: null
depends_on: []
blocked_by: null
created: '2026-09-20T12:25:09'
updated: '2026-09-20T12:25:09'
closed: null
---

## Description
Move the project config from the repo-root arbite.yaml to .arbite/project.yaml, as a hard cut: the old filenames are no longer consulted at all.

Touches:
- src/arbite/config.py: CONFIG_FILENAMES, config_path(), load_config(), set_configured_sink() (it writes the file, and must create .arbite/project.yaml when absent rather than a root-level file). The docstring's references to arbite.yaml all need rewriting.
- src/arbite/cli.py: every error and hint string that names arbite.yaml -- notably the unknown-sink error in config.sink_spec and the 'no agents configured' message in cmd_init.
- src/arbite/docs.py: any rendered guidance naming arbite.yaml.

Deliberate decision: no fallback, no deprecation warning, no auto-migration. A project with only a root arbite.yaml behaves as if it had no config -- so config_path() returning None must still lead to sane defaults, not a crash.

Note the ordering trap: config lives inside .arbite/ now, but find_arbite_dir() is what locates .arbite/ in the first place. Config resolution must go through find_arbite_dir/find_project_root, not the other way round.

Acceptance: a project configured only via .arbite/project.yaml resolves its sink, agents list and per-sink locations correctly from any subdirectory; a root arbite.yaml is ignored.

## Notes
