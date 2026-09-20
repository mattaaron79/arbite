---
id: tic-ea0e
title: Rename the 'planning' bucket to 'plans'
status: closed
type: refactor
tier: medium
domain: sinks
epic: layout
priority: 2
tags:
- file-sink
- layout
- buckets
- breaking
assignee: null
depends_on: []
blocked_by: null
created: '2026-09-20T12:25:32'
updated: '2026-09-20T12:36:36'
closed: '2026-09-20T12:36:36'
---

## Description
Rename the default 'planning' bucket to 'plans' everywhere. This is the bucket the new ticket 'references' field points into: a reference stored as 'plans/foo.md' resolves to .arbite/plans/foo.md.

Touches:
- src/arbite/sinks/file.py: DEFAULT_BUCKETS, plus the buckets() docstring which names 'planning' and 'planning/ideas' as its examples.
- src/arbite/cli.py: the cmd_move docstring and the --folder help, which use '/wishlist' and planning-flavoured examples.
- src/arbite/docs.py and any rendered guidance naming the planning bucket.
- tests/: test_file_sink.py and test_cli.py reference bucket names.

Hard cut, consistent with the config move: 'planning' is not aliased. An existing project with a planning/ directory simply has a non-default bucket of that name -- buckets() discovers any directory that isn't a status folder, so nothing breaks, it just isn't the default any more. Do not write migration code.

Acceptance: arbite init creates .arbite/plans/ and not .arbite/planning/; arbite move tic-xxxx /plans files a ticket there.

## Notes
- 2026-09-20T12:36:36 zoo.orch.001: Renamed default planning bucket to plans (hard cut, no alias); .gitignore anchored to /plans/ so .arbite/plans/ is tracked; docs/tests/README updated
