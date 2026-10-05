# arbite narration streams (`arbite stream`) — implementation plan

## Context

Add a per-ticket, append-only "stream of thought" area to arbite so a working agent can
narrate what it is doing as it does it, giving a later live dashboard a tail-friendly feed.
Today arbite has two related but different logs: `arbite note` (milestones appended to the
ticket body) and the coordination *event* stream (`arbite events`, structured claims/reads/
writes, one JSON file per event, gitignored). Neither suits free-form, high-volume narration:
notes require a ticket rewrite, and coordination events are coordination facts, not prose.

Intended end state: one JSONL file per ticket at `.arbite/streams/<ticket_id>.jsonl`, written
only through `arbite stream write` (structured records with seq, time, attempt, actor and
kind), read with cursor semantics that mirror `arbite events` (`--after`, `--tail`, exit 2 for
"nothing new", `--follow` refused). The area is gitignored runtime state. Adoption is
*suggested, not forced*: documented in the generated guides, surfaced on `arbite claim`, and
soft-gated at `arbite submit` (a `note:` when the ending attempt wrote nothing) and reported
by `arbite doctor`/`arbite workspace show`. Retention is explicit: `arbite stream clear`.

## Ticket cut (epic `arbite-stream`)

Six arbite tickets. **T1/T2 are independent and run in parallel; T3 gates on both; T4/T5 are
disjoint-file parallel; T6 is the integration/verification ticket and gates on everything.**
Each ticket's work order is the numbered Approach section named below; each is independently
acceptable with the acceptance stated here.

| Label | Title | tier | domain | priority | depends_on | Spec |
|---|---|---|---|---|---|---|
| T1 | Narration streams: storage module `coordination/streams.py` + unit tests | medium | io | 1 | — | §1 |
| T2 | Narration streams: layout, scan exclusion and proxy protection | medium | io | 1 | — | §2 |
| T3 | Narration streams: `arbite stream` CLI group (write/read/list/path/clear) | medium | io | 2 | T1, T2 | §3 |
| T4 | Narration streams: claim/submit/doctor/workspace wiring | high | io | 3 | T1, T3 | §4 |
| T5 | Narration streams: generated guides, instructions block and README | low | io | 3 | T3 | §5 |
| T6 | Narration streams: frozen transcripts and integration verification | high | io | 4 | T1–T5 | §6 |

Acceptance (observable, per ticket):
- **T1** — `python -m pytest tests/test_streams.py -q` green; `from arbite.coordination import streams`
  exposes the exact constants/signatures in §1.
- **T2** — `python -m pytest tests/test_workspace.py tests/test_file_discovery.py -q` green;
  after `arbite init`, `.arbite/streams/` exists; a file `.arbite/streams/tic-a1b2.jsonl`
  written by the module does **not** appear in `arbite list`, `arbite doctor` or
  `arbite file list`, and `arbite file read .arbite/streams/tic-a1b2.jsonl` is refused.
- **T3** — `arbite stream -h` lists the five subcommands; the Verification §2 smoke (write
  inline + stdin, read bare/`--after`/`--tail`, exit 2, list, path, clear) passes on both
  sinks; `python -m pytest tests/test_stream_cli.py -q` green.
- **T4** — claim output contains the `stream:` line; `arbite submit` prints the soft-gate note
  only when the ending attempt has no records; `arbite doctor --json` carries `streams` and
  `streams_missing` and the text note appears only for in-flight tickets with none;
  `arbite workspace show` reports the area; `python -m pytest tests/test_stream_wiring.py -q` green.
- **T5** — `arbite init` regenerates `.arbite/AGENTS.md` and `.arbite/WORKSPACE.md` with the
  stream line/block; README documents `arbite stream`;
  `python -m pytest tests/test_cli.py -q` green (guide-lean, README-completeness, instructions-block tests).
- **T6** — `python -m pytest -q` green, including new ST1–ST6 and the updated frozen blocks.

Create them with (run inside the project root; `sed` only parses `create`'s own stdout):
```sh
mkdir -p .arbite/plans && cp <this plan> .arbite/plans/arbite-stream-sink.md
c() { arbite create --type feature --epic arbite-stream --domain io \
      --references plans/arbite-stream-sink.md \
      --title "$1" --tier "$2" --priority "$3" ${4:+--depends-on "$4"} \
      --description "Work order: plan section $5. Acceptance in the plan's Ticket cut." \
      | sed -n 's/^created \(tic-[0-9a-f]*\).*/\1/p'; }
T1=$(c "Narration streams: storage module and unit tests"        medium 1 ""   §1)
T2=$(c "Narration streams: layout, scan exclusion, protection"   medium 1 ""   §2)
T3=$(c "Narration streams: arbite stream CLI group"              medium 2 "$T1,$T2" §3)
T4=$(c "Narration streams: lifecycle/doctor/workspace wiring"    high   3 "$T1,$T3" §4)
T5=$(c "Narration streams: generated guides and README"          low    3 "$T3" §5)
T6=$(c "Narration streams: transcripts and integration"          high   4 "$T1,$T2,$T3,$T4,$T5" §6)
```
Then work the epic with the normal claim → note → submit flow (`arbite claim <id> --agent
<identity>`, `arbite note ...`, `arbite submit <id>`). `arbite list next --epic arbite-stream`
offers T1/T2 concurrently; once both close, T3 offers, and so on. Each subagent reads
`.arbite/plans/arbite-stream-sink.md`.

## Approach

Ordered so the tree builds and the suite passes after each step; the step number is the
ticket's work order.

### 1. New module `src/arbite/coordination/streams.py`  (T1)

Filesystem-only peer of `coordination/scratch.py` (same shape: constants → dataclasses →
functions returning `OperationResult`). No `Record`, no coordination store, no schema
revision — the format is a private JSONL contract.

Constants (exact):
```python
STREAMS_DIRNAME = "streams"
STREAM_SUFFIX = ".jsonl"
LOCK_NAME = ".lock"
STREAM_KINDS = ("thought", "action", "result")
DEFAULT_KIND = "thought"
DEFAULT_STREAM_TAIL = 20           # mirrors coordination.app.DEFAULT_EVENT_TAIL
```

Stored record — one JSON object per line, keys in this order:
```python
{"seq": 1, "recorded_at": "2026-10-05T08:30:00Z", "ticket_id": "tic-a1b2",
 "attempt_id": "att-9f3c", "actor": "claude.opus.001", "kind": "thought", "text": "..."}
```
`recorded_at`/`ticket_id`/`attempt_id` follow `records.Event` naming; `json.dumps(..., ensure_ascii=False)`.
The read view renames them to the `Event` view names (`at`, `ticket`, `attempt`) — same as
`app._event_json`.

Functions (exact signatures/behaviour):

- `streams_root(arbite_dir) -> Path` — `Path(arbite_dir) / STREAMS_DIRNAME`.
- `ensure_streams_dir(arbite_dir) -> Path` — `mkdir(parents=True, exist_ok=True)`, return root.
- `stream_path(arbite_dir, ticket_id) -> Path` — refuse (`TicketError`) unless
  `schema.ID_PATTERN.match(ticket_id)`; return `streams_root(arbite_dir) / f"{ticket_id}.jsonl"`.
  (Guards the filename against traversal; `streams_root` is never derived from input.)
- `_parse_line(raw) -> dict|None` — `json.loads`; return None for blank or unparseable lines.
- `read_records(arbite_dir, ticket_id) -> tuple[list[dict], int]` — records in file order
  (each with its stored fields), and the count of unparseable lines skipped. Missing file →
  `([], 0)`.
- `record_count(arbite_dir, ticket_id=None, attempt_id=None) -> int` — records matching the
  filters; used by the soft gate and `doctor`. `read_records` per ticket when `ticket_id` is
  given, else scan every `*.jsonl`.
- `append_records(arbite_dir, ticket_id, attempt_id, actor, kind, texts) -> list[int]` —
  under `StoreLock(streams_root(arbite_dir) / LOCK_NAME, describe=str(arbite_dir))`
  (`from .locking import StoreLock`; its `hold()` raises `Busy`, exit 4, on contention):
  compute `next_seq` = (last stored `seq` in the file, or 0) + 1, then for each `text` write
  one record as a single `json.dumps(record) + "\n"` via `open(path, "a", encoding="utf-8")`,
  `flush()`. Return the seqs written. `ensure_streams_dir` first.
- `stream_entries(arbite_dir) -> list[StreamEntry]` — one row per `*.jsonl` in name order.
  `StreamEntry` (frozen dataclass): `ticket, path, records, bytes, cursor, last_at,
  last_actor, attempt`; `to_dict()` for JSON; the last three from the last record (None when
  empty). Ignores non-`.jsonl` files (the lock file included).
- `StreamSummary` (frozen dataclass): `files, bytes`; `is_empty`, `describe() -> "no streams"`
  or `f"{n} stream(s), {human_size(bytes)}"`, `to_dict()` → `{"files": N, "bytes": B}`.
  `human_size` imported from `.scratch` (already shared that way by `paths.py`); no new formatter.
- `stream_summary(arbite_dir) -> StreamSummary`.
- `stream_note_lines(summary, missing) -> list[str]` — `doctor`'s notes. Return `[]` when
  `summary` is empty and `missing` is empty (unlike scratch, an empty stream area is the
  normal state and is not reported). Otherwise:
  - `f"note: .arbite/streams/ holds {summary.files} stream(s) ({human_size(summary.bytes)})"`
  - `f"note: {len(missing)} ticket(s) in flight have no stream entries: {', '.join(missing)}"`
- Result builders (each returns `OperationResult`, mirroring `scratch_list`/`scratch_clear`):
  - `stream_write(arbite_dir, ticket_id, attempt_id, actor, kind, texts)` — `OK`, line
    `f"wrote {n} record(s) to .arbite/streams/{ticket_id}.jsonl (seq {first}..{last})"`;
    for one record `f"wrote 1 record to .arbite/streams/{ticket_id}.jsonl (seq {seq})"`.
    JSON: `{"ticket", "attempt", "actor", "kind", "path", "records", "cursor", "seqs"}`.
  - `stream_read(arbite_dir, ticket_id, after=None, tail=None)` — selection copied from
    `app.events`: refuse `--after`+`--tail` together (`CoordinationError`), refuse `tail < 1`,
    `after < 0`; bare → last `DEFAULT_STREAM_TAIL`. Row mirrors `app._event_row` (fixed widths,
    no delimiters, `.rstrip()`):
    `f"{seq:<4}{local_time:<10}{actor:<18}{kind:<8}{text}".rstrip()` with
    `local_time = parse_utc(recorded_at).astimezone().strftime("%H:%M:%S")` (import `parse_utc`
    from `.records`). Cursor = last selected `seq`, else `after or 0`. Non-empty adds
    `f"cursor: {cursor} (resume with 'arbite stream read {ticket_id} --after {cursor}')"`.
    Empty → `Outcome(EMPTY)` (exit 2) with `f"no stream for {ticket_id} since seq {after}"`
    (or `f"no stream for {ticket_id}"` when `after` is None). JSON:
    `{"ticket", "records": [<Event-shaped dict>...], "cursor", "next_actions"}`.
  - `stream_list(arbite_dir)` — header `f"{n} stream(s) in .arbite/streams/:"` then rows
    `f"  {e.ticket}   {e.records} record(s)  {human_size(e.bytes)}  last {local_time} by {actor}"`.
    Empty → `Outcome(EMPTY)`, line `"no streams in .arbite/streams/"`. JSON:
    `{"streams": [e.to_dict() ...], "count": n}`.
  - `stream_path_result(arbite_dir, ticket_id)` — one line, the **absolute** path
    (`str(stream_path(...))`); JSON `{"ticket", "path", "records"}`.
  - `stream_clear(arbite_dir, ticket_ids, all_)` — mirror `scratch_clear`'s refusals/messages:
    neither target → `"'stream clear' needs a ticket id, or '--all' to clear every stream"`;
    both → `"'stream clear' takes ticket ids or '--all', not both"`; a named id with no file →
    `f"no stream for '{id}' in .arbite/streams/"`. One file →
    `f"cleared .arbite/streams/{id}.jsonl ({records} record(s), {human_size})"`; `--all` →
    `f"cleared {n} stream(s) from .arbite/streams/ ({rows})"` / `"cleared 0 streams from
    .arbite/streams/ (nothing was recorded)"`. Never deletes `LOCK_NAME`. JSON:
    `{"cleared": [...], "count", "bytes"}`.

Unit tests `tests/test_streams.py` (new): append/read round-trip; seq monotonic across calls;
stdin-style multi-line `texts` produce one record each; a trailing partial line is skipped by
`read_records` and does not reset `seq`; `stream_path` refuses a traversal id; `stream_clear`
never removes `.lock`; `stream_note_lines` empty-case returns `[]`. Call the module directly
(no CLI), so T1 does not depend on T2/T3.

### 2. Layout, scan exclusion and proxy protection  (T2)

`src/arbite/sinks/file.py`: add `"streams"` to `RESERVED_DIRS` and extend that block's comment
(runtime state holding no tickets, excluded from scanning and buckets). `FileSink.init()` then
creates `.arbite/streams/`, and `_is_ticket_file`, `buckets()` and the scan skip it. This is
**required for correctness**: a stream file `tic-a1b2.jsonl` has `path.stem == "tic-a1b2"`,
which matches `ID_PATTERN`, so without the exclusion `arbite list`/`doctor` would see phantom
tickets.

`src/arbite/coordination/paths.py`: add `"streams"` to `PROTECTED_ARBITE_DIRS` and extend the
comment. `arbite_state` then classifies `.arbite/streams/...` as runtime state: the proxy
refuses a claim/read/write there and discovery treats it as invisible — the same treatment
`coordination/` and `scratch/` get.

Tests: extend `tests/test_workspace.py` (assert `.arbite/streams/` exists after `init`) and
`tests/test_file_discovery.py` / `tests/test_file_claims.py` (a stream file is invisible to
`file list`/`search`; a proxy claim/read/write of `.arbite/streams/...` is refused).

### 3. CLI surface in `src/arbite/cli.py`  (T3)

Register a `stream` group in `build_parser()` beside `p_scratch`/`p_events` (same
`sub.add_parser` + nested `add_subparsers(dest="stream_action", required=True,
metavar="SUBCOMMAND")` + `_sink_flag` on the group and every child + `_json_flag` on leaves).
Handlers near `cmd_scratch_*`:

- `cmd_stream_write` — `sink = _require_sink(args)`; `t = sink.get(args.id, unique=True)`;
  `app = _coordination_app(args, sink)`; `active = app.store.active_attempts(t.id)`.
  Zero → `TicketError(f"ticket {t.id} is not being worked (no active attempt); claim it first "
  "with 'arbite claim <id> --agent <you>'")`; more than one → `TicketError` naming them.
  `actor = args.actor or active[0].worker_id`. Text: `args.message` (`nargs="*"`) —
  `["-"]` → `sys.stdin.read().splitlines()` dropping blank lines (one record per line);
  non-empty → `[" ".join(args.message)]`; otherwise → `TicketError("nothing to write (pass "
  "TEXT, or '-' to read from stdin)")`. Then `streams.stream_write(app.arbite_dir, t.id,
  active[0].id, actor, args.kind, texts)` and `_emit_result`.
- `cmd_stream_read` — `sink.get(args.id, unique=True)` then
  `streams.stream_read(app.arbite_dir, t.id, args.after, args.tail)`.
- `cmd_stream_list` — `streams.stream_list(_coordination_app(args, sink).arbite_dir)`.
- `cmd_stream_path` — `sink.get(args.id, unique=True)` then
  `streams.stream_path_result(app.arbite_dir, t.id)`.
- `cmd_stream_clear` — `streams.stream_clear(app.arbite_dir, args.ids, args.all)`.
- All use `_emit_result(result, args.json)` and `if result.exit_code: sys.exit(result.exit_code)`.

Argparse geometry (exact):
- `write`: `id` (TICKET_ID_HELP), `message` `nargs="*"` metavar `TEXT` (help: "the line to
  record; '-' reads stdin (one record per line)"), `--kind` choices `STREAM_KINDS` default
  `DEFAULT_KIND`, `--actor` metavar `AGENT_ID` default None (help: "who to attribute the
  record to (default: the active attempt's worker); arbite records attribution, never
  authentication").
- `read`: `id`, `--after` int metavar `SEQ`, `--tail` int metavar `N` (help names
  `DEFAULT_STREAM_TAIL`).
- `list`: no positionals. `path`: `id`. `clear`: `ids` `nargs="*"` metavar `TICKET_ID`,
  `--all` store_true.

Tests `tests/test_stream_cli.py` (new), via `examples.run_cli`, parametrised over
`["file","sqlite"]`: write inline + stdin (one record per line); seq increments; `--actor`
override and attempt-worker default; no active attempt → exit 1 and nothing written; read bare
tail cap, `--after`, `--tail`, exit 2 on empty selection, `--after`+`--tail` refused, JSON
record shape; list rows/empty exit 2/JSON; path absolute + unknown id exit 1; clear named /
unknown refusal / `--all` / `--all` empty exit 0 / neither+both refusals, `.lock` survives.

### 4. Lifecycle / doctor / workspace wiring  (T4)

- **Claim surfaces the stream** — `coordination/lifecycle.py::_claimed_result` (≈:1240):
  append `f"stream: {streams.stream_path(self.app.arbite_dir, ticket.id)} (write with "
  f"'arbite stream write {ticket.id} -')"` to `lines` (after the attempt line, before the
  `next:` hint); set `data["stream"] = {"path": str(streams.stream_path(...)), "write":
  f"arbite stream write {ticket.id} -"}`. One edit covers `claim`, `list next --claim` and
  `promote --agent` (all route through `_claimed_result`).
- **Submit soft gate** — `coordination/lifecycle.py::submit` (≈:618), both branches: after the
  attempt is ended, `entries = streams.record_count(self.app.arbite_dir, ticket_id=ticket.id,
  attempt_id=ended.attempt.id) if ended is not None else 0`; set `data["stream_entries"] =
  entries`; when `ended is not None and entries == 0`, append `f"note: attempt
  {ended.attempt.id} recorded no stream entries (narrate with 'arbite stream write
  {ticket.id} -')"`. Add a private `_stream_gate_lines(ticket_id, ended) -> list[str]` and call
  it in both branches so the `review:` and `review: false` paths cannot drift. Do **not** gate
  `close`/`accept`.
- **Doctor** — `coordination/app.py::doctor_facts`: add
  `"streams": streams.stream_summary(self.arbite_dir).to_dict()` and `"streams_missing":
  sorted(ids)`. `streams_missing` = for each ticket with `status in ("in_progress","review")`,
  take its active attempt (in_progress) or its most-recently-`started` attempt (review), and
  include the ticket id when that attempt exists and
  `streams.record_count(self.arbite_dir, attempt_id=attempt.id) == 0`.
  `cli.cmd_doctor`: compute `facts = app.doctor_facts()` once **before** the `if args.json`
  branch (today it is computed only inside the JSON branch); append
  `streams.stream_note_lines(...)` to `notes` (after the scratch notes, so DR1/DR2/DR3 keep
  their shape); add `"streams": facts["streams"], "streams_missing": facts["streams_missing"]`
  to the JSON payload.
- **Workspace show** — `coordination/app.py::workspace_show`: add a `streams` line
  `_field("streams", f"{streams_path}  ({summary.describe()})")` where `streams_path` is the
  directory with a trailing slash (as `scratch` does) and `summary =
  streams.stream_summary(self.arbite_dir)`; add JSON `"streams": {"root":
  _relative(streams_root(self.arbite_dir), self.project_root), **summary.to_dict()}`. Add
  `from .streams import stream_summary, streams_root` beside the `scratch` import in `app.py`.

Tests `tests/test_stream_wiring.py` (new): claim output/JSON carry the stream facts; submit
gate appears with no records and is absent with records (the gate also asserts the file is
gitignored/absent from `git status` is out of scope — assert only the data); doctor text/JSON;
workspace text/JSON; a review-status ticket's empty last attempt is listed by `doctor`.

### 5. Generated guides, instructions block and README  (T5)

- `docs.py::WORKSPACE_COMMANDS` (≈:324): add `"stream"` after `"events"`. The full per-flag
  block renders automatically from the parser; `test_cli.py` requires the block to exist.
- `docs.py::render_workspace` "Reading the record" bullet list (≈:1128): add
  `- arbite stream read <id> [--after SEQ | --tail N] -- the narration stream a worker writes
  as it works, one record per line; poll with --after, where 'nothing new' is exit 2`.
- `docs.py::render` "Typical workflow" block (≈:741): after the `arbite claim` line add
  `add('arbite stream write tic-a1b2 -                         # narrate as you work (live tail)')`.
  Add a short `## Narration streams` prose section (2 sentences) after the workflow paragraph:
  "A worker narrates as it goes with `arbite stream write <id> -` (or a line argument); records
  land in `.arbite/streams/<id>.jsonl`, gitignored, and a dashboard polls them with
  `arbite stream read <id> --after <seq>`. This is per-ticket prose, distinct from
  `arbite events`, which is the coordination fact stream."
- `docs.py::ARBITE_INSTRUCTIONS_BLOCK` **and** `AGENTS_EXAMPLE.md` (byte-identical — asserted
  by `test_cli.py`): add to the workflow code block after the `arbite note` line
  `arbite stream write <id> -                    # narrate as you work (piped from your output)`.
- `docs.py::ARBITE_GITIGNORE_BLOCK` **and** the repo's own `.gitignore`: add `/.arbite/streams/`
  (comment: runtime narration, local evidence).
- `README.md`: add a section near the `events`/`scratch` sections documenting
  `arbite stream write|read|list|path|clear` (the README test requires the token `` `stream` ``).
  Cover the command surface, the JSONL record shape, the gitignored location and the poll pattern.

### 6. Frozen transcripts and integration verification  (T6)

- New `tests/stream_state.py`: build a project with a claimed ticket (`tic-cf9f`, agent
  `claude.opus.001`, attempt `att-91bd` — reuse the ids `tests/event_stream.py` and
  `tests/test_examples.py` already use) and N records, on both sinks; reuse `examples.run_cli`.
- New `tests/test_stream_examples.py` + six frozen scenarios `ST1`–`ST6` appended to
  `.arbite/planning/interaction-examples.md` under a `# ST — narration streams` heading:
  ST1 write (inline + stdin) and read; ST2 `--after` resume and exit-2 nothing-new; ST3 `list`
  + `path`; ST4 `clear`; ST5 write refused with no active attempt; ST6 the `submit` soft-gate
  note. Generate each block by running the command and pasting the real output (the harness
  normalises ids/times/paths).
- **Update existing frozen transcripts** whose output changes (run `pytest` and fix every
  reported diff): `WS1`, `WS2` (new `workspace show` streams line), `DR4` (new `doctor --json`
  keys), and every block printing claim output — `CL1`, `CL7`, `RC1`, plus any others the suite
  reports (the new `stream:` claim line). DR1/DR2/DR3 must stay unchanged (no streams, no
  in-flight tickets in those fixtures); if one changes, the `stream_note_lines` empty-case rule
  is wrong.

## Critical files & anchors

- `src/arbite/coordination/streams.py` — new module (all storage/format logic).
- `src/arbite/coordination/scratch.py` — the pattern to copy (`scratch_root`,
  `scratch_list`/`scratch_clear`, `note_lines` ≈:167, `human_size`).
- `src/arbite/coordination/app.py:388` `events()` — selection/cursor/exit-2 semantics to copy;
  `app.py:499` `_event_row`/`_event_json` — row and JSON shapes; `workspace_show` and
  `doctor_facts` — where the streams facts are added.
- `src/arbite/coordination/lifecycle.py:1240` `_claimed_result` and `:618` `submit`.
- `src/arbite/cli.py:3670` `p_scratch`/`:3854` `p_events` (argparse), `:2281`
  `cmd_scratch_list`/`:2298` `cmd_scratch_clear` (handlers), `:2849` `cmd_doctor`.
- `src/arbite/docs.py` — `WORKSPACE_COMMANDS:324`, `ARBITE_GITIGNORE_BLOCK:197`,
  `ARBITE_INSTRUCTIONS_BLOCK:100`, `render():741`, `render_workspace` list.
- `src/arbite/sinks/file.py:66` `RESERVED_DIRS`; `src/arbite/coordination/paths.py:56`
  `PROTECTED_ARBITE_DIRS`.

## Verification

Run from `/mnt/storage/Projects/arbite` (tests use `pythonpath=["src"]`; no install needed).

1. Full suite: `python -m pytest -q`. Must be green, including every updated frozen block.
2. End-to-end smoke (file sink) in a throwaway project:
   ```
   d=$(mktemp -d); cd "$d"; git init -q
   arbite init
   arbite raw feature "smoke"
   id=$(arbite list raw --json | python -c 'import json,sys;print(json.load(sys.stdin)[0]["id"])')
   arbite promote "$id" --title t --tier medium --domain io
   id=$(arbite list --json | python -c 'import json,sys;print(json.load(sys.stdin)[0]["id"])')
   arbite claim "$id" --agent claude.opus.001
   ```
   Observable: the claim output contains `stream: .../.arbite/streams/<id>.jsonl (write with 'arbite stream write <id> -')`.
   ```
   arbite stream write "$id" "thinking about it"          # 1 record, seq 1
   printf 'a\n\nb\n' | arbite stream write "$id" -        # 2 records, seq 2..3
   arbite stream read "$id"                               # 3 rows + cursor: 3 (resume ...)
   arbite stream read "$id" --after 1                     # 2 rows, cursor 3
   arbite stream read "$id" --after 99; echo "exit=$?"    # "no stream ... since seq 99", exit=2
   arbite stream list                                     # one row for <id>
   arbite stream path "$id"                               # absolute path; cat it -> 3 JSONL lines
   arbite submit "$id"                                    # NO gate note (has entries)
   ```
   Then a second ticket with no entries: `claim` then `submit` → the soft-gate `note:` line
   appears; while it is `in_progress`, `arbite doctor` prints `note: 1 ticket(s) in flight have
   no stream entries` and `arbite workspace show` prints a `streams` line; `arbite stream clear
   --all` reports the count and removes the JSONL files (leaving `.lock`).
3. Same smoke against `--sink sqlite` (streams still land in `.arbite/streams/`).
4. Guard: `arbite file read .arbite/streams/<id>.jsonl` is refused as runtime state;
   `arbite list` does not treat `streams/` as a bucket; `arbite doctor` reports no stray-file
   finding for it.

## Assumptions & contingencies

- `stream` is not an existing command (confirmed against `arbite --help`) and is distinct from
  the coordination "event stream"; docs prose disambiguates them. If the name is rejected during
  execution, the same design works under `arbite narrate` — rename the group only.
- Retention is explicit (chosen): no auto-clear on claim/close; `arbite stream clear
  <id>`/`--all` is the documented prune.
- `doctor`'s streams note deliberately prints nothing when the area is empty *and* no ticket is
  in flight, so DR1/DR2/DR3 keep their shape; if `test_examples.py` reports a diff there, the
  empty-case branch of `stream_note_lines` (or the `streams_missing` scope) is wrong — fix it,
  do not re-pin the transcripts.
- If `test_the_guide_stays_lean` fails, prefer trimming the added prose; raise
  `LEAN_GUIDE_LINES`/`LEAN_GUIDE_BYTES` (`tests/test_cli.py`) only if the standard cannot be
  stated more briefly, and say so in the ticket note.
- Frozen transcripts are byte-compared after normalisation; generate new/updated blocks from
  real command output rather than hand-writing ids/times. Any block the suite reports beyond
  the list in §6 is the same mechanical update.
