---
id: tic-bdb7
title: Self-heal missing ticket_references table on open; ensure project.yaml on init
status: closed
type: bug
tier: medium
domain: io
epic: null
priority: null
tags:
- sqlite
- migration
- init
assignee: qwen.qwen3.8-mxfp4.001
depends_on: []
blocked_by: null
created: '2026-09-25T12:48:44'
updated: '2026-09-25T13:12:49'
closed: '2026-09-25T13:12:49'
---

## Description
Old (v1/v2) sqlite stores open fine (tickets table present) but lack ticket_references, so any read (incl. migrate) fails with 'no such table'. Make the sqlite sink self-heal missing tables on open (mirroring the coordination backend), and ensure 'arbite init' creates .arbite/project.yaml when missing.

## Notes
- 2026-09-25T13:12:46 qwen.qwen3.8-mxfp4.001: Done -- two changes, both covered by tests.

1) sqlite sink self-heal (src/arbite/sinks/sqlite.py). _connect now checks the tables a ticket read names (NORMALIZED_TABLES: ticket_tags/deps/references/notes) and, when one is missing, runs the sink's own IF-NOT-EXISTS DDL before it opens the write transaction. A store written by an older arbite (v1/v2, no ticket_references) is readable and writable again instead of failing with 'no such table: ticket_references' -- the failure that stopped `arbite migrate --from sqlite`. Nothing is rewritten and no migration is claimed: the DDL only adds what is absent, and the schema_version row is deliberately left alone, so `doctor` still reports such a store as old.

2) `arbite init` always leaves a committed selection (src/arbite/cli.py). The config is written whenever there is no project.yaml OR the created store is not the committed one -- so plain `arbite init` writes 'sink: file' instead of leaving no config at all. The ARBITE_SINK case is unchanged and is the one exception: an environment choice stays this-process-only and writes no config, because writing one would answer "what does this project read?" with a store the project never chose and would silence the unused-database warning.

README updated for both (Selecting a sink / Integrity checking per sink).

What a user, QA or another agent can observe by integration testing:
- In a fresh directory, `arbite init` now creates .arbite/project.yaml containing 'sink: file' and prints "set 'sink: file' in project.yaml -- the store this command created is now the project default..."; `arbite init --sink sqlite` prints and writes as before.
- Against a database written by an older arbite (ticket_references table absent): `arbite list` and `arbite migrate --from sqlite --to file` now succeed where they previously errored; the tickets are copied, the source keeps its tickets, and `arbite doctor --sink sqlite` still reports only the schema-version mismatch (exit 3) rather than crashing. Writing a reference into such a store works too.
- On a current store nothing changes: the added check is one sqlite_master lookup per connection, and the DDL runs only when a table is actually missing.

Tests: tests/test_sqlite_sink.py::test_a_store_written_before_schema_v3_is_repaired_on_open and ::test_a_repaired_store_is_writable_not_merely_readable; tests/test_cli.py::test_migrate_reads_a_store_written_before_the_references_table_existed, ::test_init_records_the_default_sink_too, plus updates to the tests that pinned "a fresh file project has no config". Full suite: 1077 passed, 2 skipped (run on 3.13; pytest on 3.11 cannot import tests/test_recovery_journal.py, which uses a 3.12+ f-string -- pre-existing).

- 2026-09-25T13:12:49 system: Submitted; closed (review disabled).
