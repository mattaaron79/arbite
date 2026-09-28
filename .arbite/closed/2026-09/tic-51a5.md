---
id: tic-51a5
title: Add a global --root flag so arbite can target another project directory
status: closed
type: feature
tier: medium
domain: ui
epic: null
priority: 1
tags:
- cli
- config
- ergonomics
assignee: zoo.deepseek-flash.001
depends_on: []
blocked_by: null
created: '2026-09-28T14:47:04'
updated: '2026-09-28T15:01:13'
closed: '2026-09-28T15:01:13'
---

## Description
arbite derives its project root by walking up from the process working directory for a .arbite/ directory (config.find_arbite_dir/find_project_root). There is no way to point a command at another project without cd'ing into it. Add a global --root DIR flag (working in the same way and positions as --sink: accepted before or after the command) so 'arbite --root /path/to/other list' and 'arbite file list . --root /path/to/other' resolve the nearest .arbite/ at or above DIR instead of the cwd. Plumb the resolved root through cli helpers (_require_sink, _cwd_sink, _coordination_app, _warn_about_an_unused_database, cmd_scratch_clear, cmd_migrate) and fix TicketLifecycle.submit to read review_enabled() from the coordinated app's project_root rather than the cwd. Document it once in the generated guide, like --sink. No change to config precedence or sink selection.

## Notes
- 2026-09-28T15:01:10 zoo.deepseek-flash.001: Implemented global --root DIR. It resolves the project as the nearest .arbite/ at or above DIR (config.find_project_root(start)), i.e. as if arbite were started in DIR. Added _root_flag (mirrors _sink_flag: offered before the command and, via _add_root_flag_deeply, on every subparser recursively) plus _root_start/_project_root helpers; routed _require_sink, _cwd_sink (init uses DIR directly, no upward walk), _warn_about_an_unused_database, _coordination_app, 'sink init', 'scratch clear' and 'migrate' through it. Fixed TicketLifecycle.submit to read config.review_enabled(self.app.project_root) so a --root submit honours the target project's review flag, not the cwd's. Documented once in docs.py (added root to _GLOBAL_ACTIONS so it is not repeated per command; a bullet in the guide's sink facts; the WORKSPACE.md global note). Regenerated .arbite/AGENTS.md and .arbite/WORKSPACE.md. FILES: src/arbite/cli.py, src/arbite/coordination/lifecycle.py, src/arbite/docs.py, tests/test_cli.py. VALIDATION: full suite 1081 passed 2 skipped; 5 new tests; doctor 'no problems found' exit 0. OBSERVABLE BEHAVIOUR: 'arbite --root /other list' and 'arbite list --root /other' read the other project; 'arbite file list . --root /other' lists that project's managed files; 'arbite init --root DIR' creates .arbite/ in DIR (not the cwd); a --root that is not a directory exits 1 with '--root <DIR> is not a directory'. LOCAL STATE NOTE: this checkout moved from /media/matt/m2tb/projects/arbite to /mnt/storage/Projects/arbite, but the gitignored .arbite/coordination store still held the old workspace binding ws-6dd9. Running 'arbite init' to regenerate the docs re-derived the binding to ws-489f, orphaning existing claims/attempts so doctor reported claim/attempt_for_another_workspace; restored workspace.json and revisions.json to ws-6dd9 to leave the local store exactly as found. Any future 'arbite init' in this relocated checkout will rebind again (separate pre-existing issue).

- 2026-09-28T15:01:13 system: Submitted; closed (review disabled).
