---
id: tic-18e5
title: 'Narration streams: layout, scan exclusion, protection'
status: closed
type: feature
tier: medium
domain: io
epic: arbite-stream
priority: 1
tags: []
assignee: deepseek.deepseek-flash.002
depends_on: []
references:
- plans/arbite-stream-sink.md
blocked_by: null
created: '2026-10-05T12:37:27'
updated: '2026-10-05T12:40:52'
closed: '2026-10-05T12:40:52'
---

## Description
Work order: plan section §2. Acceptance in the plan's Ticket cut.

## Notes
- 2026-10-05T12:40:47 deepseek.deepseek-flash.002: T2 done: layout/scan exclusion/proxy protection for .arbite/streams/.

Changes:
- src/arbite/sinks/file.py: RESERVED_DIRS now ('agents','coordination','scratch','streams'); comment extended (runtime narration, holds no tickets; a 'tic-a1b2.jsonl' stem matches ID_PATTERN so without exclusion list/doctor got phantom tickets). FileSink.init() therefore creates .arbite/streams/.
- src/arbite/coordination/paths.py: PROTECTED_ARBITE_DIRS now ('coordination','scratch','streams'); comment extended. arbite_state classifies .arbite/streams/... as STATE_COORDINATION, so the proxy refuses claim/read/write and discovery skips it. The SCRATCH_DIRNAME branch / import was left untouched.
- tests/test_workspace.py: asserts .arbite/streams/ after init (file sink, whose layout creates RESERVED_DIRS; sqlite has no file layout).
- tests/test_file_discovery.py: extended the runtime-state invisibility test; a stream file is absent from 'file list .' and '.arbite/streams' list/search are refused.
- tests/test_file_claims.py: added '.arbite/streams/tic-a1b2.jsonl' to the protected-path claim cases, plus a new test that file read and file write are refused too.

Verification (from /mnt/storage/Projects/arbite):
$ uv run --no-project --python 3.13 --with pytest --with pyyaml python -m pytest tests/test_workspace.py tests/test_file_discovery.py tests/test_file_claims.py -q
64 passed in 27.96s

$ PYTHONPATH=src python3 -c "from arbite.sinks.file import RESERVED_DIRS; from arbite.coordination.paths import PROTECTED_ARBITE_DIRS; assert 'streams' in RESERVED_DIRS and 'streams' in PROTECTED_ARBITE_DIRS"
ok ('agents', 'coordination', 'scratch', 'streams') ('coordination', 'scratch', 'streams')

Phantom-ticket smoke in "$(mktemp -d)" with git init and PYTHONPATH=<repo>/src python3 -m arbite.cli init, then writing .arbite/streams/tic-a1b2.jsonl with one JSON line:
- arbite list --json  -> []
- arbite doctor       -> 'checked 0 tickets: no problems found' (no stray-file finding)
- arbite file list    -> AGENTS.md, WORKSPACE.md, project.yaml, .arbite/scratch/ only; streams/ not shown
- arbite file read .arbite/streams/tic-a1b2.jsonl -> exit 1, \".arbite/streams/tic-a1b2.jsonl' is protected: arbite does not manage its own runtime state\"

QA observability: after integration, write any .arbite/streams/<tic-*.jsonl> file by hand and observe that arbite list / list --json / doctor never surface it as a ticket and file list never walks it, while any file claim/read/write naming it is refused as runtime state with the same wording coordination/ and scratch/ use.

- 2026-10-05T12:40:52 system: Submitted; closed (review disabled).
