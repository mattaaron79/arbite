# arbite — a ticket store for git repos and AI agents, with pluggable sinks

`arbite` is a small Python CLI that keeps a project's task list durable and
inspectable while AI agents (and humans) work on it. Tickets are never behind a
server or a daemon: they live in a **sink**, and the default sink is the
filesystem — plain markdown files with YAML frontmatter under a `.arbite/`
directory, where a ticket's **status is represented by the folder it sits in**.
Moving a ticket between folders *is* the state change, and because filenames stay
stable across moves, `git log --follow` on a ticket file traces its whole
lifecycle for free.

The storage is pluggable. An opt-in `sqlite` sink keeps the same tickets in one
database file, where `status` is a column and queries are real SQL. Every command,
field, exit code and `--json` payload is identical whichever sink is in use — see
[Sinks](#sinks).

## Example AGENTS.md

See AGENTS_EXAMPLE.md. An AGENTS.md or CLAUDE.md pointing the agent to the .arbite/AGENTS.md file will
in almost all cases cause agents to use the ticket system automatically

## Useful Human Commands

Topological sort based on ticket dependency order:
arbite list --topo --status open

Tree view (more for humans):
arbite list --tree

View epic status:
arbite list --topo --epic <epic_name>

Get the next workable ticket (if you'd like to specify the ticket to an agent):
arbite list next

File a quick bug/feature/memo:
arbite bug The thing doesn't work that I want to work!
arbite feature Make the button glow when hovering
arbite memo The readme probably needs to be updated

Which store am I using, and where is it?
arbite sink info

---

## Why

Agent-driven work tends to leave state in places that don't survive a new session:
an agent's own memory file, a chat transcript, a stale assumption about what it was
doing. Since the repo is already the durable, shared, versioned medium, arbite
puts the tickets there and makes one location the single source of truth for a
ticket's state. Any other record — an agent scratchpad, a prior session's notes —
is treated as a *hint* to be verified against the actual ticket, never as
authority.

Two constraints shaped most of the design:

1. **Agents, not humans, are the primary callers.** Every query that matters has a
   `--json` mode with stable field names, and exit codes are meaningful so a shell
   loop can branch without matching on message text.
2. **Coordinates must be honest.** Multiple agents can be working at once, so
   claiming a ticket is a compare-and-swap rather than a blind write, and any command
   that could pick the wrong ticket from an ambiguous id refuses instead of guessing.

## Use in Planning

A cost and token saving method of agentic engineering is to use a high-tier LLM or
agent to do a planning and requirements session with the user. Have them generate a spec
doc based on the conversation. Then ask a high tier planning agent to generate one
or more epics in arbite. Once the tickets are ready, use an agent in orchestration
mode to work the tickets until completion. Well designed tickets can usually be completed
by small or medium-sized models.

---

## Ticket Creation

Usually it's best to have the agent create the tickets, as a command line interface can be
cumbersome. However, there are 'raw' tickets that are created via the 'arbite bug|feature|memo|wish' commands.
Raw tickets need to be classified before they are worked.

You can view raw tickets via:
arbite list raw

See AGENTS_EXAMPLE.md, which tells the agent to classify and work raw tickets when told simple directives like 'Work Next'.

## Goals

- **Zero infrastructure.** No daemon, no lock service, no server to run. The
  default sink is a directory of files; the alternative is one SQLite file, which
  the Python standard library already knows how to open. Either way a clone of the
  repo (or a copied file) is a complete working instance.
- **One storage interface, two implementations.** Commands talk to a sink, not to
  files or tables, so the *behavior* agents depend on — claiming, readiness,
  ordering, exit codes — is identical whichever store is in use, and a new backend
  has one contract to satisfy rather than a CLI to re-implement.
- **`ls` as a status board.** With the file sink, an agent or human can tell what
  is actionable from folder names alone, without opening any file.
- **One invariant per sink, enforced everywhere.** With the file sink, the
  frontmatter `status` mirrors the folder, and every state-changing command
  performs the file move *and* the frontmatter update in a single operation so the
  two can never disagree.
- **Safe under concurrency.** Claiming is a compare-and-swap on both sinks; races
  resolve to exactly one winner, and a lost race is reported, not silently
  overwritten.
- **Machine-first interfaces.** JSON output for every read query, distinct exit
  codes for success / error / empty / integrity-problems.
- **A self-describing command surface.** `arbite init` regenerates
  [`docs.py`](src/arbite/docs.py)-rendered `.arbite/AGENTS.md` straight from
  argparse's own `--help`, and words it for the sink actually in use, so the
  agent-facing docs cannot drift from the CLI *or* lie about the storage.
- **A dependency graph, not just a list.** Tickets can declare structural
  `depends_on` links, and `arbite list next` returns work in topological order.
- **Recoverable history.** Plain files plus git; no proprietary format to migrate.

## Non-goals

Explicitly **out of scope** for arbite (see [CLAUDE.md](CLAUDE.md:139) for the
original rationale):

- **Agent identity, liveness and staleness detection.** Assigning identities,
  avoiding collisions, and detecting abandoned work belong to an external *agent
  harness*. arbite only reads a static list of known agent ids from
  `arbite.yaml`/`.arbite.yaml` so `arbite init` can pre-create scratchpads —
  see [`load_known_agent_ids()`](src/arbite/config.py).
- **Scheduling or dispatching work.** arbite answers "what is workable"; getting an
  agent started on it is the caller's job.
- **A UI, web service, or notifications.** The CLI is the interface.
- **Enforcing policy beyond data integrity.** arbite refuses to corrupt state and
  reports drift; it does not police who may do what.
- **A distributed store.** Each sink is a single local store — a directory or a
  database file. Across machines the repo (or the file) is the unit of exchange.

Planned but *not* part of arbite: any claim-on-startup collision detection or
timestamp-based staleness checks. The claim operation is corruption prevention
only.

---

## Sinks

A **sink** is where tickets live. It is selected per command, and everything above
it is storage-neutral.

| | `file` (default) | `sqlite` |
| --- | --- | --- |
| Storage | markdown files under `.arbite/` | one database at `.arbite/arbite.db` |
| Status lives in | the folder the ticket sits in | a `status` column |
| State change is | a file move + frontmatter rewrite | a row update |
| Buckets (`arbite move`) | folders (`wishlist/`, `planning/ideas`) | a `bucket` column |
| History | `git log --follow` per ticket | database backups |
| Best for | committed, reviewable, greppable tickets | filtering/joining at scale |
| Runtime dependency | none beyond PyYAML | none (`sqlite3` is in the stdlib) |

### Selecting a sink

Highest precedence first:

1. `--sink <kind>` on any command (before or after the command name)
2. the `ARBITE_SINK` environment variable
3. a `sink:` key in `arbite.yaml` / `.arbite.yaml`
4. the default: `file`

```yaml
# arbite.yaml
sink: sqlite
sinks:
  file:   { root: .arbite }            # optional location overrides
  sqlite: { path: .arbite/arbite.db }
agents: [claude.haiku.001]
```

`arbite init` initialises whichever sink is selected, and `arbite sink info`
reports what is active and where. Creating a database-backed project is a config
edit or one flag, not a different command:

```bash
arbite init --sink sqlite
arbite --sink sqlite list next --tier high
```

If the database file is used, add it to `.gitignore`: unlike the file sink, it is
binary and won't produce a readable history.

### The interface

Commits to storage go through one CRUD-shaped surface
([`sinks/base.py`](src/arbite/sinks/base.py)):

| Method | Verb | Notes |
| --- | --- | --- |
| `new_id()`, `create(ticket)` | Create | a duplicate id is a conflict, not an overwrite |
| `get(id, unique=False)` | Read | a wildcard id resolves; mutations pass `unique=True` and refuse ambiguity |
| `get_many(ids)`, `exists(id)`, `query(TicketQuery)`, `notes(id)` | Read | one query vocabulary, two implementations |
| `render(ticket)`, `location(id)`, `describe_location`-style `location_map(tickets)` | Read | `render` is the ticket's canonical text on *both* sinks; a location is an opaque string |
| `update(ticket, expect=None)`, `add_note(...)` | Update | `expect` is the compare-and-swap token |
| `delete(id)` | Delete | irreversible; the CLI requires `--force` |
| `bucket(id)`, `move_to_bucket(id, bucket)` | — | filing, which is not a state change |
| `check(fix=False)` | — | integrity: shared checks plus the sink's own |

Three deliberate properties of that surface:

- **The status→location side effect belongs to the sink.** A command sets
  `status` and calls `update()`; only the file sink knows that this means moving a
  file, and only the SQLite sink knows it means writing a column. That is what
  makes "status is folder location" a *feature of the file sink* rather than an
  assumption baked into every command.
- **Compare-and-swap is an argument, not a separate method.** `update(ticket,
  expect=Expect(status="open", assignee=None))` means "write this only if the
  ticket is still open and unassigned" — a claim. The file sink enforces it with an
  exclusive create at the destination file; the SQLite sink with a conditional
  `UPDATE` inside a transaction. One write path, so a claim cannot bypass it.
- **Queries are storage-neutral and verified.** `TicketQuery` names the filters
  (status, type, tier, domain, epic, assignee, priority, ids, buckets, text, order,
  limit) and [`query.py`](src/arbite/query.py) holds the reference implementation.
  The SQLite sink answers structured filters with SQL, but text matching runs
  through the same matcher as the file sink — SQLite's `LIKE` folds case in ASCII
  only, so pushing a substring search into it can return a *different* answer.

### Integrity checking per sink

`arbite doctor` reports the checks that mean the same thing anywhere (duplicate
ids, invalid field values, dependency cycles, dangling and self dependencies,
`in_progress` without an assignee, `blocked` without a reason, closed-date
mismatches) and then the ones that don't:

- **file sink** — frontmatter/folder drift (the folder wins), a ticket left loose
  in the arbite root, temp files stranded by an interrupted write, a closed ticket
  archived in the wrong month, unreadable files;
- **sqlite sink** — a note index that has drifted from the ticket body, orphaned
  index rows, an unexpected schema version, structural database corruption.

`--fix` repairs only what is unambiguous, and exits `3` while problems remain, so
it can gate CI or an agent's startup.

### Moving tickets between sinks

`arbite migrate --to <kind>` copies every ticket — status-managed ones and
bucketed ones — into the other sink, preserving ids, timestamps, body, tags,
dependencies, notes and buckets verbatim. It never touches the source unless you
ask it to, so a migration is undone by not switching the config over. Because it
reads through one sink and writes through the other, a `file → sqlite → file`
round trip that reproduces the original files byte for byte is the end-to-end test
of the whole interface; `tests/test_cli.py` asserts exactly that.

Two things worth being explicit about, because both are easy to assume wrongly:

- **`arbite init --sink sqlite` does not migrate anything.** It creates the store.
  Existing tickets stay in the file store until you run `migrate`, and until
  `sink: sqlite` is in the config (or you pass `--sink sqlite`) every command still
  reads the files. `.arbite/AGENTS.md` warns in bold when a store exists that no
  command would select.
- **`--prune` retires the old store** once the copy is verified: after migrating, it
  deletes the source tickets, so `arbite migrate --to sqlite --prune` followed by
  `sink: sqlite` leaves exactly one store. It refuses outright if any ticket was
  *skipped* because the destination already had that id — the source copy is then
  the newer one, and pruning would destroy it (add `--overwrite` to replace it
  first). A refused prune destroys nothing: the copy has happened, the cleanup
  hasn't. `--dry-run` reports what it would migrate and prune without touching
  either side, and `--overwrite` replaces destination tickets that share an id
  instead of skipping them.

---

## Design principles

- **Storage is a sink.** One interface ([`sinks/base.py`](src/arbite/sinks/base.py)),
  two implementations ([`file.py`](src/arbite/sinks/file.py),
  [`sqlite.py`](src/arbite/sinks/sqlite.py)) that share no storage code. A second
  implementation is only worth having if it is genuinely second, so the sink that
  has no files has no file-shaped shortcuts.
- **The schema is one thing, not a copy per sink.** [`schema.py`](src/arbite/schema.py)
  owns the `Ticket` model, the controlled vocabularies, the markdown form and the
  `## Notes` derivation. Both sinks render tickets with the same
  `Ticket.to_markdown()`, which is why `arbite show` looks identical on either.
- **Status is folder location — in the file sink.** `status` mirrors the folder and
  must never be allowed to drift from it. When something outside arbite breaks that
  pairing (a hand `mv`, a merge, a rebase, an interrupted write), `arbite doctor`
  reports it and — with `--fix` — rewrites the frontmatter to match the folder,
  because **the folder wins**.
- **Stable filenames.** A ticket keeps the same filename in `open/`,
  `in_progress/`, and `closed/2026-08/`, so moves show up as folder-move commits
  under `git log --follow`.
- **Atomic writes and moves.** Saves stage a complete temp file and `os.replace`
  it into position; moves stage into the destination, unlink the source, then
  rename. The worst case of a crash is a visible, recoverable temp file — never two
  files sharing one id.
- **Compare-and-swap claiming.** Claiming never blind-writes: the expectation is
  checked and enforced *inside* the storage operation that performs it, so two
  agents racing for the same ticket cannot both come away believing they own it.
- **Ambiguity is an error for mutations.** Commands that modify a ticket require an
  unambiguous id and list the candidates otherwise; read-only commands keep the
  convenience of the first alphabetical match.
- **Controlled vocabularies live in one place.** `STATUSES`, `TYPES`, `TIERS` and
  friends in [`schema.py`](src/arbite/schema.py) are validated by `set`, `create`
  and every sink's `check`, and interpolated into `.arbite/AGENTS.md`, so the docs,
  the CLI and the validator cannot drift apart.
- **The docs must be true for the sink in use.** `.arbite/AGENTS.md` is rendered
  from the sink's own capabilities, so a database-backed project is never told that
  "the folder is the source of truth".

---

## Scope: what is implemented

The package exposes the console script `arbite`, providing:

| Area | Commands |
| --- | --- |
| Setup | `init`, `sink [info\|init]` |
| Creation | `create` (incl. `--blank` scaffolding), `raw <memo\|feature\|bug\|wish>` plus the one-word shortcuts `bug` / `feature` / `wish` / `memo` |
| Triage | `fetch [type]` (oldest raw ticket + injected `derived_note`), `list raw` |
| Reading | `list` (flat, `next`, `raw`, `--topo`, `--tree`, `--epic`, `--tic`, `--count`), `search`, `show`, `deps` |
| Lifecycle | `claim`, `release`, `block`, `unblock`, `shelve`, `unshelve`, `close`, `reopen` |
| Authoring | `note`, `set`, `depend`, `move` |
| Storage | `migrate --to <sink> [--from] [--overwrite] [--prune] [--dry-run]` |
| Integrity | `doctor [--fix]` |
| Destruction | `delete <id> --force` |

Key behaviours worth calling out:

- **`arbite list next`** returns the most urgent (lowest `priority` number) `open`
  ticket whose `depends_on` are all closed, in topological dependency order, and can
  filter by `--tier` / `--domain` / `--epic`. If nothing is ready it exits `2`; if
  everything is deadlocked it says so on stderr instead of leaving an agent polling a
  queue that can never yield work.
- **`arbite list next --claim <agent_id>`** selects *and* claims in one step,
  closing the race inherent in calling `list next` then `claim`. If it loses the race
  for the top ticket it takes the next workable one rather than failing.
- **`--count N`** turns `list next` into a batch pull; with `--claim` each claim is
  individually a compare-and-swap, so a short batch is a correct result, reported as
  such.
- **`arbite fetch`** implements the triage queue: it pulls the oldest `raw` ticket
  and prints it with a `derived_note` (a JSON field in `--json` mode, a leading block
  otherwise) telling the caller exactly how to classify it.
- **`arbite move`** files a ticket in a bucket (`/wishlist`, `/planning/ideas`) or
  returns it to its status location (`/`). It changes no field, so it is not a state
  change — status commands un-file a ticket for you.
- **`arbite delete`** destroys a ticket and refuses to do so without `--force`; it
  records a `Deleted by <agent>` note first and prints a receipt. `close` is usually
  what you want.
- **`arbite doctor`** checks the invariants nothing else enforces and exits `3` when
  problems remain — see [Integrity checking per sink](#integrity-checking-per-sink).

`CLAUDE.md` records a handful of deliberately open judgement calls — validation
strictness for `domain`/`tags`, the `deps` visualization format, and whether
`blocked_by` should support multiple blockers.

---

## Storage: what a file sink project looks like

```
.arbite/
  raw/             unclassified quick captures -- not workable yet
  open/            actionable, unclaimed
  in_progress/     claimed, being worked
  blocked/         stalled, see blocked_by
  shelved/         parked for later
  wishlist/        reclassified wishes (type: feature) -- not work until promoted
  planning/        planning/roadmap notes and scratch docs -- not tickets
  closed/
    2026-08/
    2026-07/
    ...
  agents/
    claude.haiku.001.md
    ...
  AGENTS.md        generated command reference
```

- `raw/`, `open/`, `in_progress/`, `blocked/`, `shelved/` are **status folders**.
- `closed/` archives monthly by close date so it doesn't become one flat directory.
- `wishlist/` and `planning/` are **buckets**, not statuses: a ticket filed in one
  is out of the status workflow (so `list next` never offers it) and keeps whatever
  status it had. `planning/` is for planning notes that aren't tickets at all;
  `arbite init` creates both, and a markdown file there is only treated as a ticket
  if it is named like one (`tic-XXXX.md`).
- `agents/` holds one scratchpad file per known agent identity (no required schema),
  pre-created from the `agents:` list in `arbite.yaml` / `.arbite.yaml`. Scratchpads
  stay files whichever sink is active: they are harness-facing state, not tickets.
- `AGENTS.md` is regenerated on every `arbite init`. It is **not auto-discovered** —
  a project that wants agents to find arbite must point at it explicitly (e.g. a line
  in its own `CLAUDE.md` like "read `.arbite/AGENTS.md`").

A SQLite-sink project has the same `.arbite/agents/` directories and `AGENTS.md`,
with `arbite.db` in place of the status folders.

### Ticket format

```yaml
---
id: tic-a1b2
title: Fix off-by-one in vertex normal calc
status: open              # raw | open | in_progress | blocked | shelved | closed — mirrors the folder
type: bug                 # bug | feature | refactor | chore | memo | wish
tier: medium              # low | medium | high | frontier — agent capability tier required
domain: mesh              # routing: mesh, image_gen, audio_gen, ui, io, ...
epic: mesh-pipeline       # larger initiative (optional), freeform grouping label
priority: 2               # numeric urgency, lower = more urgent; unset sorts last
tags: [normals, curves]   # freeform, for codebase-area search

assignee: null            # e.g. claude.haiku.001
depends_on: []            # structural: ticket ids that must close first
blocked_by: null          # freeform reason OR a ticket id

created: 2026-08-06T14:32:09
updated: 2026-08-06T14:32:09
closed: null              # set on close; drives closed/YYYY-MM/ archiving
---

## Description
Free text description of the task.

## Notes
- 2026-08-06T14:32:09 claude.haiku.001: progress update, appended over time.
```

This is the same schema on both sinks, and `arbite show` prints it in this form
either way. The SQLite sink additionally indexes the `## Notes` section into rows so
notes are queryable, but the body stays authoritative: the index is rebuilt from it,
and `doctor` reports if the two disagree.

The independent axes are easy to conflate, so they are deliberately separate fields:

- `tier` is **capability** (a ladder — an agent may work anything at or below its own
  tier). `domain` is **specialization** (what kind of agent is needed). `priority` is
  **urgency**. A low-tier chore can be urgent; a high-tier ticket can be
  `audio_gen`-only.
- `depends_on` is **structural** (a dependency tree). `blocked_by` is **narrative**
  (what is actually stalling it right now, possibly external). Don't collapse them.
- `domain` drives routing; `tags` drive search by codebase area.
- `epic` groups tickets under a larger initiative and is a freeform filter label, not
  a dependency. Raw tickets are auto-grouped under the `classification` epic so triage
  jobs can find them via `arbite list next --epic classification`.

---

## Installation

`arbite` is a standard installable package ([`pyproject.toml`](pyproject.toml:1)) with
a single runtime dependency (`pyyaml`) and a console-script entry point:

```toml
[project.scripts]
arbite = "arbite.cli:main"
```

For day-to-day use, install globally with `pipx` so `arbite` is on `PATH` everywhere
without activating a virtualenv:

```bash
pipx install .
```

For active development, prefer an editable local install (pipx does not support
editable installs the way pip does), and install the test extra:

```bash
pip install -e ".[dev]"
arbite --version
pytest
```

Helper scripts in [`scripts/`](scripts/) wrap `pipx install "." --force` to reinstall
the current checkout into the global pipx environment. Both derive the repo root from
their own location, so they can be run from any directory:

- Windows: [`scripts/update-arbite.bat`](scripts/update-arbite.bat:1)
- Linux/macOS: [`scripts/update-arbite.sh`](scripts/update-arbite.sh:1), e.g.
  `./scripts/update-arbite.sh`

Run the shell script as your normal user, **not** with `sudo`: pipx installs per-user,
so under root the package lands in `/root/.local` and stays invisible to your shell.
The script refuses to run as root for exactly this reason.

`pyproject.toml` declares `requires-python = ">=3.9"`.

## Quick start

```bash
# 1. Create the directory structure and the generated AGENTS.md
arbite init

# 2. Capture a thought without classifying it yet
arbite raw feature "add per-mesh LOD"
arbite list raw                       # the running triage backlog

# 3. Triage: pull the oldest raw ticket and classify it
arbite fetch
arbite set tic-a1b2 title "Add per-mesh LOD" tier medium domain mesh \
    epic mesh-pipeline priority 3
arbite set tic-a1b2 status open

# 4. Work it
arbite list next --tier medium        # what's ready at my capability level?
arbite claim tic-a1b2 --agent claude.haiku.001
arbite note tic-a1b2 claude.haiku.001 "found the LOD cache invalidation bug"
arbite block tic-a1b2 --reason "waiting on tic-c3d4"
arbite unblock tic-a1b2 --agent claude.haiku.001
arbite close tic-a1b2

# 5. Housekeeping
arbite doctor --fix
```

Using the database sink instead of files. `--sink` is a *per-command* choice, so
either pass it every time or make it the project default — one line of config:

```bash
# A new project on the database sink
arbite init --sink sqlite                  # creates .arbite/arbite.db + AGENTS.md
printf 'sink: sqlite\n' >> arbite.yaml     # make every later command use it
arbite sink info                           # which store, where, what it supports

# Or bring an existing file-based project across, then switch
arbite migrate --to sqlite                 # copies every ticket; source untouched
printf 'sink: sqlite\n' >> arbite.yaml
arbite doctor                              # verify the new store

# ...and once you are satisfied, retire the old store (destructive)
arbite migrate --to sqlite --prune --dry-run   # what would be migrated and pruned
arbite migrate --to sqlite --prune             # copy, then delete the file tickets
```

For a one-off command, skip the config: `arbite --sink sqlite list --status open
--domain mesh`, or `ARBITE_SINK=sqlite arbite list`. Until `sink:` is set, plain
`arbite` commands use the file sink — and if a database exists that nobody has
selected, the CLI says so on stderr rather than quietly reading the other store.

## Agent-facing conventions

These exist because agents, not humans, are the main callers:

- **`--json`** on `list`, `list next`, `fetch`, `show`, `search`, `deps`, `doctor`,
  `sink` and `delete` emits machine-readable output whose field names match the
  frontmatter. The human table format is explicitly *not* a stable interface. The
  `path` field is whatever the sink calls a ticket's location (a file path, or
  `sqlite:/…/arbite.db#tic-a1b2`).
- **Exit codes** let a shell loop branch without matching message text: `0` success
  with results, `1` error, `2` the query ran but matched nothing, `3` `doctor` found
  problems.
- **Prefer `list next --claim`** over `list next` followed by `claim`; the two-step
  version has a race another agent can win.
- **Prefer `arbite note`** over hand-editing a ticket, so attribution and timestamps
  stay consistent.
- **Resume by verifying, not remembering.** Check your own scratchpad for a
  last-known ticket id, then confirm the ticket really is claimed by you — with the
  file sink that means it sits in `.arbite/in_progress/` with `assignee` matching
  your id; with any sink, `arbite show <id> --json` settles it. The store is ground
  truth, the scratchpad is a hint. If it is absent, stale, or mismatched, scan for a
  ticket assigned to you with `arbite list --assignee <your-id>`.
- **Stay at or below your tier.** Pass your own tier to `arbite list next --tier`, so
  you are only offered work you can actually do.
- **Don't assume the storage.** Use `arbite sink info` rather than reaching for a
  file path; a path that works on one sink does not exist on the other.

---

## Repository layout

```
pyproject.toml          packaging + console-script entry point (+ pytest config)
CLAUDE.md               the original design spec and rationale
src/arbite/
  __init__.py           package version
  cli.py                argument parsing, command dispatch, exit codes
  schema.py             the Ticket model, controlled vocabularies, markdown format,
                        notes derivation, field validation
  query.py              TicketQuery / TextMatch: the storage-neutral query vocabulary
  graph.py              dependency closure, readiness, topological order, cycles
  errors.py             the exception hierarchy every command and sink reports through
  sinks/
    base.py             the TicketSink CRUD interface + the checks shared by all sinks
    file.py             the file sink: status folders, atomic moves, temp-file recovery
    sqlite.py           the sqlite sink: normalized tables, SQL queries, note index
    __init__.py         the sink registry
  config.py             locating .arbite/ and resolving which sink to use
  docs.py               renders .arbite/AGENTS.md from the real argparse output
                        and the active sink's capabilities
tests/
  test_sink_conformance.py  one suite, run against every sink
  test_file_sink.py         file-specific: folders, drift, temp files, archives
  test_sqlite_sink.py       sqlite-specific: schema, note index, pushdown parity
  test_cli.py               end-to-end argv, exit codes, --json, migrate round trip
  test_graph.py             dependency-graph semantics, pinned
  test_query.py             query vocabulary semantics, pinned
scripts/
  seed_demo.py          generates ~2.5 months of realistic seed tickets for either
                        sink (--sink file|sqlite), via the same code paths the CLI uses
  update-arbite.bat     reinstall the checkout into global pipx (Windows)
  update-arbite.sh      reinstall the checkout into global pipx (Linux/macOS)
```

## Status

The core system is implemented: creation, listing and filtering, dependency-ordered
`list next`, raw capture plus triage, the full claim/release/block/unblock/shelve/
close/reopen lifecycle, notes, `set`/`move`/`depend`, the `doctor` integrity checker,
the sink abstraction with both the file and SQLite implementations, sink selection
and `sink info`, `delete`, and `migrate`. `CLAUDE.md` remains the canonical record of
the design; open judgement calls and any schema change should be raised there before
implementation.
