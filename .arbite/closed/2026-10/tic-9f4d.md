---
id: tic-9f4d
title: init --gitignore flag with sensible defaults
status: closed
type: feature
tier: low
domain: io
epic: null
priority: null
tags: []
assignee: zoo.qwen3.8-flash.001
depends_on: []
blocked_by: null
created: '2026-10-05T00:26:42'
updated: '2026-10-05T00:46:38'
closed: '2026-10-05T00:46:38'
---

## Description


## Notes
- 2026-10-05T00:26:59 zoo.qwen3.8-flash.001: Add --gitignore flag to 'arbite init' with sensible defaults: install a demarcated runtime-state block into ./.gitignore (coordination/, scratch/, arbite.db*), idempotent by markers like --agents-doc. Claiming.

- 2026-10-05T00:46:37 zoo.qwen3.8-flash.001: Done: --gitignore on init, darkpact-style marked section. docs.install_gitignore returns created/appended/updated/present; only the region between '# BEGIN ARBITE GITIGNORE' and '# END ARBITE GITIGNORE' is ever rewritten, so an existing .gitignore is never clobbered and a stale section is refreshed in place (half-marked files count as present and are left alone). cli cmd_init reports each outcome; parser help + init description updated. README gitignore paragraph rewritten. OBSERVABLE: 'arbite init --gitignore' on a fresh repo writes .gitignore with /.arbite/coordination/, /.arbite/scratch/, /.arbite/arbite.db*; on a repo with an existing .gitignore it appends the marked section below existing rules (append, not prepend: git last-match wins); re-running prints 'already contains' and changes nothing; editing the section by hand then re-running prints 'refreshed the arbite section' and restores current entries while surrounding lines survive byte-for-byte. A plain 'arbite init' still never touches .gitignore. VALIDATION: 15 init tests pass (4 new, incl. section-replacement); full suite 1128 passed 2 failed 2 skipped -- both failures reproduce on the unmodified baseline (accent/colour test, and LC1 transcript frozen at closed/2026-09 vs October).

- 2026-10-05T00:46:38 system: Submitted; closed (review disabled).
