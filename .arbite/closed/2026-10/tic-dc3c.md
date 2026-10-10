---
id: tic-dc3c
title: Raise the version to 0.4.1 for milieu round 3 and the C YAML loader
status: closed
type: chore
tier: low
domain: io
epic: milieu
priority: null
tags: []
assignee: claude.opus-5-5.001
depends_on: []
blocked_by: null
created: '2026-10-10T09:43:26'
updated: '2026-10-10T09:54:04'
closed: '2026-10-10T09:54:04'
---

## Description
Patch bump 0.4.0 -> 0.4.1 in pyproject.toml and src/arbite/__init__.py (the user chose a patch bump over the minor one first requested), so milieu can name the release that has --buckets, the bucket JSON key, progress --json [] and the libyaml loader.

## Notes
- 2026-10-10T09:54:03 claude.opus-5-5.001: Version 0.4.1 in pyproject.toml and src/arbite/__init__.py; pipx install refreshed with scripts/update-arbite.sh.

Observable: 'arbite --version' prints 'arbite 0.4.1'.

- 2026-10-10T09:54:04 system: Submitted; closed (review disabled).
