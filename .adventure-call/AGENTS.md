<!-- Maintainers: this file is read by agents on every task. Keep it as short as
possible while still complete: every line must earn its tokens. Edit the template
in adventure_call/agents_doc.py; `adventure-call update` regenerates this copy. -->

# adventure-call: code navigation for agents

Static call/import/data-flow graph of this Python project (adventure-call 0.1.0).
Use it BEFORE grepping or reading whole files: find where a symbol is defined, who
calls it, what it reaches, what a change affects, then read only the lines named.

Run from anywhere inside the project. Output is compact JSON on stdout (`source`
prints plain text). If sources changed since the last analysis, the next query
re-analyses first (a few seconds; notice on stderr). Do NOT run `analyze` for
queries: it writes standalone JSON that queries never read. Use `update` only
after changing analysis options. `-h` on any command gives full options. `vcall`
is a short alias for `adventure-call`.

## Commands

| Command | Answers |
| --- | --- |
| `overview` | Codebase map: counts, top dirs, top entry points, most-called, most complex, most-imported, import cycles |
| `find TEXT [--kind K,K] [-r]` | Where is it? `ID KIND path:line` per match |
| `symbol ID [--code]` | Signature, doc, role, metrics, callers (including `module_callers`), callees, reads/writes, effects, external/unresolved calls; `--code` appends plain source |
| `source ID [ID...]` | Exact source text of symbols (current file contents) |
| `file PATH [--kind K] [--match TEXT] [--outline-only]` | Importers/imports/externals and a filterable outline |
| `refs NAME [--kind attr\|name\|string]` | Current syntactic name matches, grouped by file and enclosing symbol; attributes are not type-checked |
| `calls ID [--direction down\|up\|both] [--depth N]` | Call-flow cone: `nodes` = `ID: "<hop> path:line"`, +N callees, -N callers; `edges` |
| `impact ID [--all]` | Blast radius: direct callers (call sites), transitive count (`--all` lists), readers/writers or importers; `files`, `test_files` |
| `tests [FILE...] [--since REV]` | Which tests to run: uncommitted (and untracked) changed files by default; a changed test file counts as itself. `--plain` prints one path per line |
| `state ID` | Module/class state read or written, directly and `through_calls` (`name:r\|w\|rw`) |
| `imports [PATH] [--depth N] [--direction in\|out\|both] [--cycles]` | File import graph, or local view with hop counts |
| `tree [DIR] [--depth N] [--metric symbols\|lines\|files]` | Directory sizes; dirs end in `/`, `_total` per dir |
| `entries [--include-tests]` | Entry points ranked by transitive reach, with the evidence for each |
| `orphans` | Callables with no callers, callees or framework role (possibly unused) |
| `update` | Re-analyse by hand (after changing options, e.g. `update --exclude-dir gen`) |
| `serve` | Human-only: opens the workspace web UI in a browser, from this store, on loopback. Agents stay on the JSON queries |

## IDs

`ID` accepts: full dotted id `pkg.mod.Class.method`; any unique suffix or bare name
(`Class.method`, `method`); a file path (`src/x.py`); or `path:LINE` for the innermost
symbol on that line. Ambiguous input exits 1 with `candidates`; unknown exits 2 with
`did_you_mean`.

## Reading the output

- References are `"ID path:line"`. In `callers` the line is the call site; elsewhere it is
  the definition. A trailing ` ?` marks a heuristic (name-only) resolution.
- Empty fields are omitted. `<list>_more: N` means N entries were cut (raise `--limit`).
- `role`: `internal` (called in-project, including from a module script), `entry` (no known caller), `framework-entry`
  (test/route/command/dunder/property...), `referenced` (passed as a value), `orphan`.
- `metrics`: `fan_in`/`fan_out` direct callers/callees; `reach_down`/`reach_up`
  transitive; `complexity` cyclomatic-style.
- `truncated`/`beyond`: the walk hit its budget / N more lie just outside the view.

## Limits (trust accordingly)

Static analysis only; Python only. Calls through untyped locals, `getattr`, callbacks
and other dynamic dispatch are often unresolved (see `overview.call_resolution`), so
callers/impact lists are lower bounds: absence is not proof. `symbol` shows each
function's `unresolved` calls. Confirm with the source before deleting or renaming.

## Recipes

- Understand a function: `symbol ID --code`.
- Before changing a signature: `impact ID` -> edit every `direct_callers` site -> run `test_files`.
- Before running tests: `tests` (bare = what you have not committed) -> `tests --plain | xargs -r pytest -q`.
  An empty list means no test imports the change, not that the change is safe.
- Trace a feature: `find TEXT` -> `calls ID --depth 3` -> `source` the relevant nodes.
- Find consumers of a field: `refs FIELD --kind attr` (then confirm name-based hits in source).
- Orient in an unfamiliar area: `tree DIR --depth 1`, `file PATH`, `imports PATH`.
- Exit codes: 0 ok, 1 error/ambiguous, 2 not found, 3 no `.adventure-call/` (run `adventure-call init`).
