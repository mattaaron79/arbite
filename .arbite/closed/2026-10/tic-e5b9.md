---
id: tic-e5b9
title: request key on every --json payload that prints a raw ticket
status: closed
type: feature
tier: medium
domain: cli
epic: milieu-integration
priority: 2
tags:
- json
- raw
- request
- milieu
assignee: omp.qwen3.8-next-flash.001
depends_on: []
references:
- plans/milieu-cli-gaps.md
blocked_by: null
created: '2026-10-09T22:37:24'
updated: '2026-10-09T22:44:50'
closed: '2026-10-09T22:44:50'
---

## Description
`arbite list raw --json` gives every raw ticket a derived `request` key (the short text the ticket was captured from) so the backlog can be grouped and shown in one line. No other `--json` surface has it: a milieu view of a single raw ticket must instead dig the `Original request:` line out of a long triage block inside `description`.

Change: every command that prints a ticket as JSON must carry the same `request` key for tickets in the `raw` status, from the same code path as `list raw --json`. For a ticket in any other status, one consistent choice, applied identically in all commands: leave the key out or give `null`. Pick one and state it in the ticket note.

Recommended shape, because it is one writer instead of six injections: add the key inside `Ticket.to_dict` (`src/arbite/schema.py`, the single projection every `--json` path calls) as `"request": raw_captured_request(self) if self.status == "raw" else None`, so the key is always present and `null` for non-raw tickets, and then DELETE the now-duplicate injection in `src/arbite/cli.py::_cmd_list_raw` (`data["request"] = schema.raw_captured_request(t)` at cli.py:1747) so `list raw --json` keeps its current value from one place. `schema.raw_captured_request(ticket)` (schema.py:530) returns `""` when the body no longer carries the line -- decide what the payload shows then (`""`, or `null`) and make it consistent; the human `list raw` line already falls back to a placeholder, and tests should pin whichever you choose. `cmd_fetch`'s `derived_note` is a different key and stays as it is.

Blast radius is the point of the single-writer choice: `show`, `list`, `list next`, `list raw`, `list --tree`, `search`, `progress`, `delete`, `deps --json`, the claim payload and create/raw/shortcut `--json` all print through `to_dict`, so all of them gain the key with one edit. Confirm each of those still prints a valid document and that no existing test asserts the key's absence.

Acceptance (real CLI, file sink AND sqlite sink):
- `arbite bug "fix the door" --json` gives `"request": "fix the door"` and `arbite show <that id> --json` gives the same value; likewise for each of `feature`, `request`, `memo`, `wish` and the long form `arbite raw <type> "..."`.
- `arbite list --json`, `arbite search <term> --json` and `arbite list --status raw --json` carry the same value for the same ticket.
- `show <id> --json` for a non-raw ticket is consistent (key absent, or `null`) in every one of those commands.
- a raw ticket whose description was rewritten by hand (no `Original request:` line) produces the agreed fallback rather than an error.
- `arbite list raw --json` output for a raw ticket is unchanged from today apart from the (deliberate) unification above.

## Notes
- 2026-10-09T22:43:35 omp.qwen3.8-next-flash.008: tic-e5b9 implemented (omp.qwen3.8-next-flash.008). schema.py Ticket.to_dict is now the one writer of a derived `request` key: raw tickets get schema.raw_captured_request(self), every other status gets null, so the key is always present. The duplicate injection `data["request"] = schema.raw_captured_request(t)` in cli.py::_cmd_list_raw is deleted -- `list raw --json` keeps its value, now from to_dict, alongside show/list/list next/list --tree/search/progress/delete/deps/fetch and the create/raw/shortcut payloads. DECISION on the degenerate raw case: a raw ticket whose body no longer carries the `Original request:` line reports null, never "" -- one rule (null whenever there is no captured text), matching the description key convention of null-on-absent; the human `list raw` line still prints its placeholder and exits 0. storage untouched: no FIELD_ORDER/to_markdown/parse_ticket change, no frontmatter key, no sink schema change. Docs: JSON_HELP, the conventions `Parse JSON, not tables` prose and a new `docs fields` entry (FIELD_NOTES/_topic_fields) document the key; CR1/CR2 transcripts in .arbite/planning/interaction-examples.md pin it (null for the open create, the captured text for the raw shortcut). Tests added: test_schema.py::test_to_dict_derives_request_for_raw_tickets_only; test_sink_conformance.py::test_request_survives_the_storage_round_trip (both sinks); test_cli.py::test_every_json_payload_carries_the_captured_request, ::test_a_non_raw_ticket_reports_a_null_request_everywhere, ::test_a_raw_ticket_whose_capture_line_is_gone_reports_null, each parametrized over file and sqlite. Verifier will observe: `bug "fix the door" --json` and `show <id> --json` both give "request": "fix the door"; list/search/list --status raw/list raw --json agree on the same id; non-raw tickets give null in all of them; a hand-rewritten raw description gives null without an error. Deliberately left alone: cmd_fetch's `derived_note` injection (verified still present next to `request`), and the 0.4.0 version bump in pyproject.toml/__init__.py, which belongs to another worker.

- 2026-10-09T22:44:45 omp.qwen3.8-next-flash.001: verified by the parent, both sinks, 22 checks: every ticket-printing --json surface (bug/feature/request/memo/wish shortcuts, raw <type>, create, show, list, list --status raw, list raw, search, deps) carries 'request' for a raw ticket and null for any other status -- one representation everywhere; list raw --json is unchanged apart from the deliberate ''-to-null unification on a hand-rewritten capture; the human list raw placeholder still prints and exits 0. The only key added to the show payload versus HEAD is 'request' (verified as a set delta).

- 2026-10-09T22:44:50 system: Submitted; closed (review disabled).
