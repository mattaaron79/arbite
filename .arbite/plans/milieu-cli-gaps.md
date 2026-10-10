# Milieu 1.0 CLI gaps

Origin: milieu (a small web app that shows arbite projects in a browser) audited
arbite 0.3.0 and found four gaps. Milieu changes tickets only by running `arbite`
commands and reads only `--json` output; it never edits ticket files. Every change
below adds to a command and changes nothing a command does today.

## Tickets

| Change | Ticket | Tier | Priority |
| --- | --- | --- | --- |
| `description` as a convenience field | tic-5600 | high | 1 |
| `list --type <type>` | tic-e99e | low | 2 |
| `--json` on `create` and `raw` | tic-8519 | medium | 3, depends on tic-5600 |
| `depend <a> <b> --remove` | tic-bcaa | medium | 5 (wanted, not needed) |

## Shared implementation anchors

Verified against the tree on 2026-10-09; re-check line numbers before editing.

- One JSON projection: `Ticket.to_dict` (`src/arbite/schema.py:304`) — FIELD_ORDER keys
  plus `body` and `path`. Call sites: `_emit_tickets` (cli.py:165, list/search/topo),
  `cmd_show` (2701), `_cmd_list_raw` (1734), `cmd_fetch` (1394), `_tree_payload` (2022),
  `cmd_progress` (1215), `cmd_delete` (3176), `coordination/lifecycle.py:1326`.
  Per-command derived keys already exist (`request`, `derived_note`).
- Body sections: `schema.DEFAULT_BODY` (108), `NOTES_HEADING` (226), `append_note` (396),
  `notes_body` (417), `parse_notes` (429). No Description extractor exists.
- `set` whitelist: `SETTABLE_PROPERTIES` (schema.py:504), refusal at `cmd_set`
  (cli.py:2920-2926), application loop 2938-2948 with the `status` special case at 2941.
- Structured filters: `_filter_query` (cli.py:1631) → `TicketQuery` (query.py:136-190);
  file sink `filter_tickets` (sinks/base.py:556), sqlite SQL pushdown
  (sinks/sqlite.py:465-522). `type` is already supported end-to-end.
- Writes: every mutating command passes `expect=_expect_from(t)` (cli.py:546) to
  `sink.update`; `enforce_expect` (sinks/base.py:75) raises `Conflict` (errors.py:42) and
  `main()` maps it to `error:`/exit 1 (`coordination/results.py:297`). Exit 4/5 arise only
  in the coordination layer, so `depend`/`set`/`create` exit 0 or 1.
- Docs: `docs.py:render_commands` (1475) renders flags from the live parsers, so a new
  flag needs only its `add_argument` help; prose topics are hand-maintained
  (`JSON_HELP` 66, `FIELD_NOTES` 247, the "Parse JSON, not tables" list 1060).
- Normative transcripts: `.arbite/planning/interaction-examples.md`, asserted by scenario
  id through `tests/examples.py` (JSON blocks compared as parsed documents). Cross-sink
  parity is enforced by `tests/test_sink_conformance.py`; CLI tests are parametrized with
  `@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])` (`tests/test_cli.py:785`).

## Explicitly not changing

- Text search: milieu uses `search --json` as it is.
- `show`/`deps` exiting 1 rather than 2 when nothing matches — acceptable to milieu.
- Exit 5 (stale): milieu treats it the same as 4 (busy).
