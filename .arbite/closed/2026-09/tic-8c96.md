---
id: tic-8c96
title: 'Add ''review: true|false'' to project config'
status: closed
type: feature
tier: low
domain: config
epic: config
priority: 1
tags:
- config
- review
assignee: null
depends_on:
- tic-b757
blocked_by: null
created: '2026-09-20T12:25:09'
updated: '2026-09-20T13:28:34'
closed: '2026-09-20T13:28:34'
---

## Description
Add a top-level boolean 'review' key to .arbite/project.yaml, defaulting to true, with an accessor in src/arbite/config.py (e.g. review_enabled(project_root)) so no command has to reach into the raw dict.

The flag has exactly one job: it decides where 'arbite submit' sends a ticket -- to review when true, straight to closed when false. It deliberately does NOT remove the review status from the vocabulary and does NOT stop init creating the review/ folder, because a project that flips the flag off must not strand tickets already sitting in review.

Non-boolean values are an error naming the file and the key, consistent with how load_config already reports a malformed config, rather than a silent falsey default.

Acceptance: absent key reads as true; 'review: false' reads as false; a garbage value errors clearly.

## Notes
- 2026-09-20T13:28:34 zoo.orch.001: Added top-level boolean review: key to .arbite/project.yaml with review_enabled() accessor; defaults true; non-boolean values error naming file+key; does not affect the review status or the review/ folder
