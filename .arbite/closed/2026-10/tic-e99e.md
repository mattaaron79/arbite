---
id: tic-e99e
title: 'list --type <type>: filter tickets by type like the other filters'
status: closed
type: feature
tier: low
domain: cli
epic: milieu-integration
priority: 2
tags:
- list
- filters
- type
- json
- milieu
assignee: omp.qwen3.8-next-flash.001
depends_on: []
references:
- plans/milieu-cli-gaps.md
blocked_by: null
created: '2026-10-09T20:54:26'
updated: '2026-10-09T21:23:04'
closed: '2026-10-09T21:23:04'
---

## Description
`list` filters by --status, --tier, --domain, --epic, --priority and --assignee but has no filter for `type`. `search --params type` can filter by type, yet it cannot combine with the other filters or with --tree/--topo, so "give me the open bugs" has no single answer. See plans/milieu-cli-gaps.md.

Change: add --type to `list`, taking the same values as the `type` field, and behave exactly as --domain does -- combinable with every other filter and with --tree, --topo and --json.

Implementation: one argparse flag on p_list (cli.py:4586, description string 4587-4589) plus one line in _filter_query (cli.py:1631-1645), which already builds a TicketQuery from the other filters with getattr defaults. Nothing else: TicketQuery.type exists end-to-end (query.py:136 field, 152-161 scalar coercion, 186-187 predicate), the file sink answers it through filter_tickets (sinks/base.py:556) and sqlite already emits `type IN (...)` (sqlite.py:481-485), both already covered by test_sink_conformance.py:283-296. --tree/--topo apply the filters as scope before graph work (cmd_list cli.py:1939; _print_topo selects afterwards over the unfiltered topo order), so no graph.py change and no ordering surprise.

Value vocabulary: validate against schema.TYPES (schema.py:45, includes memo and wish -- raw captures are real ticket types and are already invisible in the default list view), following the `choices=` precedent used by --tier (cli.py:4601) rather than inventing an error path. Single value like --domain, not CSV like --status; the query layer coerces a scalar already.

Also add it to `list next` for parity, since --domain is offered there: _cmd_list_next builds its own query (cli.py:1789-1796) and _report_nothing_workable (1902-1907) lists the supplied filters in tier/domain/epic order -- extend that tuple. The pinned CL4/CL5 transcripts (interaction-examples.md:223-243) name only the filters actually passed, so they stay byte-identical.

Docs: p_list's description and the `next` subcommand help (cli.py:4643-4657); docs.py FIELD_NOTES['type'] (249) and mirror the epic entry that advertises `list [next] --epic` (261-262). The command reference renders from the live parser (docs.py:1475). README has no per-flag content.

Acceptance: `arbite list --type bug --status open --json` returns only open bugs. --type combines with each other filter and with --tree, --topo and --json exactly as --domain does. An unknown type is rejected the same way an unknown --tier value is.

Tests: tests/test_cli.py beside the filter-agreement pattern at 2256-2260, parametrized over sink_kind (785); query semantics need no new coverage (test_query.py:64-67, 97-126; test_sqlite_sink.py:392-408 already exercise type). No existing scenario runs a filtered flat `list`; if a transcript is wanted, add a new scenario id.

## Notes
- 2026-10-09T21:15:07 omp.qwen3.8-next-flash.003: list --type <type> shipped: p_list gains --type (choices=schema.TYPES, single value) wired through _filter_query, so it combines with every filter and with --tree/--topo/--json exactly like --domain; list next gains the same filter (query in _cmd_list_next, and the nothing-workable report now lists type=value alongside tier/domain/epic). Docs: docs commands list renders the flag from the live parser, docs fields type entry advertises list [next] --type. Observable: 'arbite list --type bug --status open --json' returns only open bugs on both sinks; 'arbite list --type nonsense' exits 2 with the same argparse invalid-choice shape as an unknown --tier; unfiltered list/list --topo/list --tree/list next output is byte-identical to HEAD (diffed after id-normalizing). Tests: tests/test_cli.py adds create_type/seed_typed_backlog/walked_ids helpers plus 3 parametrized tests (filter agreement incl. tree/topo set equality, list next filtering incl. the type= blocked report, vocabulary rejection) - 17 pass over both sinks; CL4/CL5 pinned transcripts byte-identical (test_lifecycle_examples -k 'CL4 or CL5' 2 passed). Pre-existing failures unrelated to this change, reproduce on pristine HEAD: test_LC1_close_releases_claims, test_the_epic_and_the_assignee_carry_their_own_accents. Targeted run: pytest -q tests/test_cli.py tests/test_query.py tests/test_sink_conformance.py -> 1 failed (color test, pre-existing) 333 passed 1 skipped.

- 2026-10-09T21:23:03 omp.qwen3.8-next-flash.001: verified independently by the parent (both sinks, real CLI): list --type bug --status open --json returns exactly the two open bugs; --type combines with --tier/--domain/--epic/--priority/--assignee and yields the identical id set through flat, --topo and --tree --json; list next --type bug selects the bug while unfiltered next still prefers urgency; an unknown value is rejected with the same argparse exit-2 shape as an unknown --tier, on list and on next; unfiltered list/list --topo/list --tree/list next text is byte-identical to the HEAD binary run against the same project. Note for the reader: --type memo matches a raw memo capture because 'list' with no --status already covers every status at HEAD -- not a change introduced here.

- 2026-10-09T21:23:04 system: Submitted; closed (review disabled).
