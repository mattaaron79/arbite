---
id: tic-25a2
title: Lock in claim -> in_progress with a test and documentation
status: closed
type: chore
tier: low
domain: cli
epic: workflow
priority: 4
tags:
- cli
- claim
- tests
assignee: null
depends_on: []
blocked_by: null
created: '2026-09-20T12:26:17'
updated: '2026-09-20T12:56:40'
closed: '2026-09-20T12:56:40'
---

## Description
'arbite claim' already sets status to in_progress (cli.py cmd_claim), so there is no behaviour to add. This ticket exists to make that contract explicit rather than incidental, since the review workflow now depends on the claim -> in_progress -> submit -> review -> accept chain being complete.

Do:
- Add a direct test asserting claim sets in_progress and moves the file to in_progress/ in the file sink, including the --force takeover path, if tests/test_cli.py does not already cover it head-on.
- Make the transition explicit in the claim help text and in the rendered agent guide, so an agent reading only the docs knows it does not need a separate status call after claiming.

Acceptance: the test exists and passes; 'arbite claim --help' states the status transition.

## Notes
- 2026-09-20T12:56:40 zoo.orch.001: Pinned the claim -> in_progress contract with a direct file-sink test (including --force takeover and the refused-race path); claim help text and rendered agent guide now state the transition explicitly
