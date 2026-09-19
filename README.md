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

File a quick bug/feature/request/memo:
arbite bug The thing doesn't work that I want to work!
arbite feature Make the button glow when hovering
arbite request Collapse the toolbar when scrolling
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
cumbersome. However, there are 'raw' tickets that are created via the
`arbite bug|feature|request|memo|wish` commands. A `request` is a request for a change that is
not necessarily a bug or a new feature, but a tweak or lateral change. Raw tickets need to be
classified before they are worked.

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
- **Coordinates agents in one shared directory.** A file proxy (`arbite file`)
  gives agents exclusive whole-file claims, version-checked reads and writes,
  per-ticket change evidence, and attempt-scoped cleanup on close/release/block,
  so two independently started agents can work one checkout without a successful
  write silently overwriting a competing one — see
  [Shared-directory coordination](#shared-directory-coordination-the-file-proxy).
- **Machine-first interfaces.** JSON output for every read query, distinct exit
  codes for success / error / empty / integrity-problems.
- **A self-describing command surface.** `arbite init` regenerates the
  [`docs.py`](src/arbite/docs.py)-rendered guide: a short `.arbite/AGENTS.md`
  quickstart (size-capped by a test) and the full `.arbite/REFERENCE.md`, whose
  command section comes straight from argparse's own `--help`. Both are worded for
  the sink actually in use, so the agent-facing docs cannot drift from the CLI *or*
  lie about the storage.
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
- **Proving who changed a file outside arbite.** The file proxy enforces its own
  operations and detects observed drift, and it supplies the hooks a future
  sandbox could restrict, but a shell, editor or build tool can still write to the
  same directory and arbite cannot prove who did it — see
  [the optional external enforcement boundary](#the-optional-external-enforcement-boundary).
- **A distributed store.** Each sink is a single local store — a directory or a
  database file. Across machines the repo (or the file) is the unit of exchange.

Deliberately **absent**: any runner, daemon, watcher or scheduler; automatic
stale-work detection or takeover; worktrees and a merge-back workflow; a central
database or dashboard; and artifact garbage collection. Agents are started
manually — possibly by different providers or runtimes — and a stopped worker is
never inferred from a timestamp: handing over live work is always an explicit
`arbite claim --force --reason`. See
[the shared-directory section](#shared-directory-coordination-the-file-proxy).

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
reports what is active and where. Creating a database-backed project is one flag,
not a different command — and `init` **writes the choice into `arbite.yaml`**
(created if missing, every other key left alone), so the store you just set up is
the one every later command reads, including commands an agent runs with no flags
at all:

```bash
arbite init --sink sqlite        # creates the database AND sets sink: sqlite
arbite list next --tier high     # ...so no flag is needed from here on
arbite --sink file list          # a one-off against the other store still works
```

`arbite migrate` does the same for its destination, since that is where the tickets
now live. An `ARBITE_SINK` selection is treated as this-process-only: it is reported
rather than written to committed config.

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

Three things worth being explicit about, because all are easy to assume wrongly:

- **`arbite init --sink sqlite` does not migrate anything.** It creates the store and
  makes it the project default. Existing tickets stay in the file store until you run
  `migrate`.
- **A store that nothing selects is called out.** If a project ends up holding
  tickets in a store no command would read — a deleted `sink:` key, an `ARBITE_SINK`
  one-off, an unfinished migration — commands warn on stderr, and the generated
  `.arbite/AGENTS.md` carries a bold warning naming that store and its ticket count,
  because the wrong store looks exactly like an empty one.
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

## Shared-directory coordination (the file proxy)

Two agents started independently — different providers, different runtimes, no
shared parent process — can work in one checkout through `arbite` without a
successful write silently overwriting a competing one. The workspace directory
*is* the working state: there is no worktree, no merge-back step and no server.
Both sinks implement the same semantics (the file sink serializes with process
locks and a journal, the SQLite sink with real transactions), and the file sink
never requires SQLite.

### The command surface

- **Discovery.** `arbite file list [PATH]` and `arbite file search PATTERN [PATH]`
  return bounded, deterministically ordered entries with explicit paging and
  truncation markers. Discovery is not write authority.
- **Reads.** `arbite file read PATH --ticket T --attempt A [--lines S:E]` records a
  versioned receipt: the whole-file digest is always recorded, even when only a
  line range is returned, alongside an explicit `write_authorizing` flag. A read
  takes no lock and acquires no ownership, and a file held by another attempt is
  still served — flagged `busy`, naming the holder, and marked non-writable.
  `--fail-if-busy` refuses with `file_busy` instead of spending tokens on bytes
  that cannot be written.
- **Claims.** `arbite file claim PATH... --ticket T --attempt A` acquires
  exclusive whole-file writer ownership, all-or-nothing and in deterministic
  order. Contention returns structured `file_busy` naming the holder's ticket and
  attempt; arbite never waits for or steals a claim. `arbite file release
  PATH... --reason TEXT` revokes a file token early but keeps the work attempt.
- **Mutations.** `arbite file write`, `edit`, `remove` and `rename` are journalled
  and version-checked inside an operation lock. Replacing an existing file
  requires a *fresh* read token recorded by the same attempt after it claimed the
  path; a stale, pre-claim, foreign or already-consumed token changes no bytes and
  returns `stale_read`. A token is consumed by the mutation it authorizes, even
  one that writes identical bytes. A binary file, which cannot be read as text,
  gets its token from `arbite file read PATH --version-only`, which records the
  whole-file version without serving content. `rename` owns both paths, and a destination held by
  another attempt leaves neither path touched. v1 has no recursive deletion and
  no metadata editing.
- **Evidence.** `arbite changes T [--attempt A] [--include-reads] [--json]` keeps
  three things apart: mechanical operations (ordered receipts with before/after
  digests and artifact references), agent-authored ticket notes (prose, never used
  to derive the net change), and observed/unattributed drift (never assigned to
  the current agent).
- **Lifecycle.** Claiming a ticket records a **work attempt** (generation, worker,
  started/last-activity). `close`, `release`, `block` and `shelve` end the attempt
  and release its file claims in the same operation; `reopen`/`unblock` mint a
  fresh attempt and never resurrect an old token. `claim --force --reason` is an
  explicit administrative takeover, not an inference from timestamps.
- **Administration.** `arbite export` dumps the coordination history (and/or
  tickets) as one JSON document; `arbite rebind` switches the workspace's
  authoritative store after verifying it; `arbite migrate --coordination` moves
  the history between sinks; `arbite doctor` reports orphan claims, invalid
  generations, missing artifacts, pending intents and binding/revision drift, and
  `--fix` repairs only the unambiguous cases.

### The agent mandate

Agents are instructed (see [`AGENTS_EXAMPLE.md`](AGENTS_EXAMPLE.md:1) and the
generated `.arbite/AGENTS.md`) to do source **discovery, reads and mutations**
through arbite rather than shell or editor writes, and to **re-read through
arbite after acquiring a claim, after a takeover, and at every ticket boundary** —
a read token taken before a claim does not authorize a write. A read token is a
freshness receipt, not proof the content was read, so `--version-only` (or a
`--lines` range) is enough to obtain one. Shell commands remain fine for
tests and builds. The enforcement is instruction plus the proxy's own checks: a
direct shell write bypasses the proxy.

Every `arbite file` command takes `--attempt A`. Acquisition prints it (`claim`,
`list next --claim`, `offer claim` and `package claim`; `attempt_id` in `--json`),
and `arbite show T --json` reports it as `active_attempt.id`. It is never inferred:
after a takeover the old id is what refuses the previous worker's file commands.

### Owner recipe: two agents in one directory

[`scripts/proxy_recipe.py`](scripts/proxy_recipe.py:1) runs the whole flow
reproducibly in a throwaway workspace and prints a transcript:

```bash
python3 scripts/proxy_recipe.py --sink file     # ...or --sink sqlite
```

It creates two tickets, claims one per agent using two separate `arbite`
processes, has each agent own a different file, races both agents for one
contested file (one wins, the loser gets a structured `file_busy`), proves a stale
read is refused without changing bytes, closes the winner's ticket and shows the
loser can then take the released file, and demonstrates an administrative takeover
plus an external-tool write that arbite detects and never silently overwrites. The
same flow is asserted end-to-end by
[`tests/test_proxy_acceptance.py`](tests/test_proxy_acceptance.py:1) on both
sinks.

To do it by hand in a disposable copy:

```bash
mkdir /tmp/two-agents && cd /tmp/two-agents && git init -q .
arbite init
arbite create --title "Agent A task" --type chore --tier medium --domain io
arbite create --title "Agent B task" --type chore --tier medium --domain io
arbite list next --claim agent.a.001        # one ticket per agent
arbite list next --claim agent.b.001
arbite show <ticket> --json                 # then work files as in the mandate
```

### No runner, daemon or automatic takeover

Arbite starts nothing. There is no worker process, watcher, scheduler, heartbeat
or stale timer; every command is one-shot and returns promptly. Contention is
reported, not waited on, and a stopped worker is **never** inferred from an
elapsed timestamp — only an explicit `claim --force --reason` (or
`close`/`release`/`block` with `--force --reason`) moves live work. Recovery from
an interrupted operation happens in the *next* relevant operation or when you run
`arbite doctor`, never in the background.

### Test and build output

The proxy records the writes it performs. A build, formatter, test run or code
generator that writes into the tree is **unattributed drift**: arbite can detect
that bytes changed, but it did not attribute the change to a ticket and cannot say
who made it. Keep generated output outside managed source paths — a gitignored
directory, an out-of-tree build directory, or a temp dir — and keep the managed
source paths to what the proxy owns.

### Evidence growth and retention

Every successful mutation stores its before/after bytes (content-addressed by
digest) as durable evidence, so an active workspace grows with use. There is **no
artifact garbage collection** yet: retention and reference rules have not been
designed, and closing a ticket deliberately does not delete its evidence. Budget
disk accordingly, and use `arbite export` (optionally `--no-artifacts` to omit
bytes) to archive or hand off history.

### The optional external enforcement boundary

Honest scope: arbite enforces **its own** operations — claims, generation checks,
version checks and journalled mutations — and it *detects observed drift* when the
bytes it recorded are no longer what is on disk. It also supplies the hooks
(operation receipts, per-attempt ids, workspace binding) a future sandbox or
runtime restriction could use. It cannot, by itself, prevent or attribute a direct
filesystem change: an external editor, script or build tool can write to the same
directory, and arbite cannot prove who did it. Restricting that is the user's
runtime choice, not a property of the store.

A change the proxy did not make also invalidates the held claim's recorded
version, so a later read of that path is deliberately non-writable
(`claim_version_mismatch`) and a write with an old token is refused
(`stale_read`) with the external bytes intact. The way forward is explicit:
`arbite file release` the path and `arbite file claim` it again — a new claim
generation over the current bytes — then read and write.

### Deliberately absent

No runner/daemon/watcher/scheduler, no automatic stale detection or takeover, no
worktrees or merge workflow, no central database, no dashboard or factory, and no
artifact GC. The job-board epic adds no provider integration either: no provider
adapters or API calls, no agent spawning, no background subscription, notification
delivery or agent wakeup, no heartbeat expiration, no auction, price catalog or
currency conversion, no agent authentication, and no cross-machine or global
capacity arbitration. Capacity is per worker id inside one local store; nothing is
ever claimed about occupancy across independent project stores. Nothing times out
and nothing is reassigned because execution stopped -- only an explicit
`claim --force --reason`, an explicit reservation/offer change, or an explicit
`package handoff --reason` moves live work. See [Deferred](#deferred) for the full
list.

### Worker profiles (optional)

`arbite worker register <id> --tier <tier>` records an optional, provider-neutral
profile for a worker id: configured tier, capability labels, execution locality
(`local`/`remote`/`unknown`), cost class (`local`/`paid`/`unknown`) with an
optional estimate that must carry explicit units and provenance, and a declared
capacity. `worker show|list|update|disable|enable|checkin|check` complete the
surface, all with `--json`. Nothing is launched or contacted: provider, model and
runtime are labels, every value is an operator assertion rather than verified
identity, and credential-like values are refused.

Registration is never required — ad-hoc worker ids keep claiming as before. Once a
worker id *is* registered, its configured tier is authoritative at acquisition:
`claim` and `list next --claim` refuse a ticket above it (`worker_ineligible` /
`tier_insufficient`), `list next --tier` may narrow the search but never exceed
it (`declared_tier_exceeds_profile`), and `--force` does not bypass it. Changing
the tier is an explicit `worker update --tier`, recorded as a `worker_updated`
event with before/after values. `disable` stops new acquisitions while keeping the
profile, its events and every attempt that names the worker; running attempts are
not revoked. `last_checkin` is declared activity, never verified liveness.
Declared capacity *is* enforced when work is acquired, and only **active
attempts** count against it: a reservation or a later package member consumes
nothing, a `--count N` batch or two simultaneous claims cannot overfill the
worker (a short batch is the correct result), and a profile change affects future
acquisition only. `worker check` evaluates a
worker against a ticket's tier or explicit constraints (`--require-capability`,
`--local-only`, `--max-cost N --max-cost-unit U`) and lists every unmet or unknown
constraint with a stable reason code; it writes nothing and does not evaluate
readiness.

### Reservations (coordinators)

`arbite reserve create T1 T2 ... --agent <coordinator>` holds an explicit ticket
set for a coordinator; `--epic E` instead snapshots the epic's non-closed tickets
once (tickets added to the epic later are not members until `reserve add`). A
reservation is ownership of *who may acquire*, not execution: it creates no
attempt and never sets a ticket `in_progress`. While it is active only the owner
may acquire a member — `claim`, `list next --claim`, `--adopt`, `--force` and
`set status in_progress` / `set assignee` refuse everyone else with
`ticket_reserved`, and a plain `list next` leaves reserved tickets out. The owner
lets other workers pick up members through [offers](#offers-and-direct-assignments).

Create and `reserve add` are all-or-nothing: one closed, unknown, already-reserved
(no overlap, so no nesting), otherwise-assigned ticket, or one with another
worker's active attempt refuses the whole request (`reservation_conflict`, with a
per-ticket reason). All reservation writes take the same operation lock as
acquisition, so reserve-vs-claim has one serial outcome. `reserve add|remove|release`
need `--agent <owner>` (or `--force --reason`) and accept `--expect-revision`;
`remove`/`release` refuse while an affected member has an active attempt unless
`--interrupt --reason` ends it (ticket back to open). Released reservations are
kept as history (`reserve list --state all`), recorded as `reservation` category
events, and carried by `export`/`migrate --coordination`. Releasing (or removing
members) also withdraws their published offers in the same transaction.

### Offers and direct assignments

`arbite offer publish T --agent <owner>` makes one open, unassigned ticket
available to any worker meeting its requirements; `arbite offer assign T --worker W
--agent <owner>` restricts it to the named worker(s). On a reserved ticket only the
reservation owner (or `--force --reason`) may publish, and the reservation stays in
place. Requirements (`--min-tier`, `--require-capability`, `--local-only`,
`--max-cost N --max-cost-unit U`) are evaluated at acquisition in restricted mode —
an unknown tier/capability/locality/cost fails — while `--prefer-local`,
`--prefer-low-cost` and `--prefer-worker` are stored and shown as hints only: in
this passive mode the first eligible claimant wins, and an explicit `assign` is how
an owner makes a preference stick. There is no auction, pricing catalog or
scheduler.

Workers pick up offers themselves with `arbite offer claim OFFER --agent <id>`
(plain `claim` and `list next --claim` go through the same offer); the owner need
not be online. Acceptance *is* acquisition: the lifecycle cascade that stores the
new attempt also moves the offer `published → accepted`, under the operation lock
and the ticket compare-and-swap, so simultaneous acceptance has exactly one winner
and the loser gets `offer_conflict`. While an offer is published, no one it does
not admit can acquire the ticket — the owner, `--force` and `--adopt` included
(`worker_ineligible`) — and `set status in_progress` / `set assignee` / `delete`
refuse with `ticket_offered`. `offer withdraw` (publisher or reservation owner, else
`--force --reason`) ends a published offer; withdraw-vs-accept is serialized by the
same lock, and an accepted offer refuses withdrawal unless `--interrupt --reason`
interrupts the worker (ticket back to open). An offer follows its ticket: closing it
completes the accepted offer (or cancels an unaccepted one); a release, takeover or
`unblock --open` that leaves the accepting worker without the ticket cancels it.
Offers are `offer` category events, listed with `offer list [--state] [--ticket]
[--reservation] [--worker W]`, explained with `offer show --worker W`, and carried by
`export`/`migrate --coordination`. `offer publish --package PKG` and
`offer assign --package PKG` offer a whole continuity package instead of one
ticket: acceptance starts only the package's first ready member and binds every
remaining member to that worker (see [Continuity packages](#continuity-packages)).

### Continuity packages

A **package** is the contract "A then B, by the same worker".
`arbite package create T1 T2 [...] --agent <coordinator>` bundles two or more open
tickets in order; package order is a scheduling edge, so only the current member
(the first one not closed) can be acquired, and only that member is offered by
`list next` -- and only to the worker the package is bound to. Creation is
all-or-nothing (`package_conflict`): a member that is closed, assigned, active,
offered, reserved by someone else or already in another package is refused, and
package order plus the tickets' own `depends_on` may not form a cycle.
Dependencies outside the package are reported as external prerequisites.

Accepting the first member -- directly with `arbite claim`, or by accepting
`arbite offer publish/assign --package PKG` -- binds every remaining member to that
worker id in the same transaction. Each member is its own attempt; closing one
releases its file claims, and the next member needs a **fresh read**. *Same
worker* is a continuity identity, not the same model session: use
`arbite package note PKG TEXT --agent W` for handoff context and
`arbite package handoff PKG --reason TEXT (--to W | --release) [--interrupt]
[--note TEXT]` to rebind or release the remaining members. Completed members stay
completed, nothing times out, and a blocked member stops the package until it is
explicitly resolved. `package show|list` report progress, the current member,
external prerequisites and the next action.

### The board: readiness, capacity and routing

`arbite board --worker W [--epic E] [--tier T] [--count N] [--json]` answers one
question per candidate ticket: ready now, or not -- and if not, why. It is a query
that claims nothing, and it exits `2` when nothing is ready, so a caller needs no
resident loop. Every exclusion carries a stable reason `code` and an `axis`:
`status`/`classification` (`ticket_not_open`, `ticket_unclassified`),
`dependency` (`dependencies_unmet`), `attempt` (`active_attempt`), `reservation`
(`reserved_elsewhere`), `offer` (`offer_ineligible`, with the nested eligibility
reasons such as `worker_not_allowed`), `continuity` (`package_order`,
`package_bound_elsewhere`), `worker` (`worker_ineligible`: tier, capability,
locality, cost ceiling -- an unknown value fails) and `capacity`
(`capacity_exhausted`). The output also reports the declared capacity, the active
attempts counted against it and the free slots, plus `suggestions` -- the compatible
ready work in the order `list next` would hand it out.

One evaluator backs the board, `list next` and acquisition, so a board cannot
disagree with what `claim` accepts -- but it also cannot promise that a ticket it
reports ready is still free when you act on it, because acquisition re-checks
every condition under the operation lock. Advisory `--prefer-*` hints are reported
and never decide pickup: the first eligible claimant wins, and an owner who wants a
preference *enforced* uses a hard constraint or an explicit `offer assign`.
`arbite reserve progress RESERVATION [--owner OWNER] [--state active|released|all]`
is the coordinator's view of the same state: every member classified `completed`,
`blocked`, `active`, `dependency_waiting`, `ready` or `unavailable`, with its
current worker and latest recorded activity. That activity timestamp is an
observation read from durable records -- arbite never infers that a worker is alive
or dead, and no heartbeat expires anything.

### Events and progress (external callers)

`arbite events --after CURSOR --limit N [--category C] [--kind K] [--subject ID]
[--json]` reads the durable coordination log once, in cursor order. `--after` takes
either `0` or the `next_cursor` token a previous query returned; a token is
store-bound (`<cursor_namespace>#<cursor>`), so a bare integer fails
`invalid_cursor` and another store's token fails `cursor_foreign_store` (with an
import mapping when that store's events were imported here) -- neither silently
restarts or reads the wrong store. Filters apply to the whole ordered stream before
`--limit`, and the returned token resumes after the last *matching* event, so a
filtered stream neither skips nor re-reads. Every event has a stable id -- a retry
may deliver it again, so consumers deduplicate by id -- and `read` observations are
their own category, separate from lifecycle traffic. A caller that exits and
returns later resumes from the token it stored; an empty page is exit `2` with the
token still printed. This is the whole orchestration loop: **no watcher,
subscription, notification or wakeup exists**, and a "busy" answer is an exit code,
not a model process that has to stay alive.

### Transfers, export and the doctor

Profiles, reservations, offers and packages travel with `arbite export` and with
`arbite migrate`/`arbite rebind` -- the coordination half moves attempts, claims,
receipts, intents, profiles, reservations, offers, packages, events and artifact
metadata together and rebinds the workspace. A transfer **refuses while job-board
work is in flight**, reporting `store_not_quiescent` and naming the active
attempt(s), claim(s), intent(s), operation(s), reservation(s), published offer(s)
and live package(s) that block it, writing nothing. End the work and reconcile it,
or pass an explicit override: `--force --reason TEXT`, reported on stderr (and as
`unquiescent_override` in `rebind --json`) rather than applied silently. A released
reservation, a withdrawn or finished offer and a completed or released package do
not block. The export bundle verifies its own job-board references and overlaps and
fingerprints the job-board records, so a lossy transfer is refused instead of being
reported as verified.

`arbite doctor` adds report-only job-board checks whenever the store carries
coordination state -- dangling and overlapping reservations/offers/packages, a
package/reservation membership mismatch, an invalid continuity binding, an attempt
disagreeing with its ticket, impossible capacity arithmetic, an unreadable event
cursor registry. They are findings only: `--fix` repairs none of them, and `doctor`
exits `3` while any remains. There is no delete for a profile (`worker disable`
keeps its history and running attempts) and no delete for a reservation, offer or
package -- they close by state.

### Owner recipe: the manual two-provider demonstration

This is the epic's acceptance demonstration, run in a throwaway directory with the
CLI only: no provider adapter, no server, nothing started by arbite. "Alice" uses
Claude and "Bob" uses GPT; both are plain worker ids with provider *labels*, and a
coordinator handles the reservation. Capture the ids the commands print and
substitute them in the next step -- the transcript notes below are from a real run.

```bash
mkdir /tmp/job-board && cd /tmp/job-board && git init -q .
arbite init

# 1. Two workers, two providers, two labels. This launches nothing.
arbite worker register claude.opus.001 --tier high --provider anthropic \
    --model claude-opus-5 --capability python --locality local --cost-class paid \
    --capacity 2 --json
arbite worker register openai.gpt.002 --tier medium --provider openai \
    --model gpt-5 --capability python --locality remote --cost-class paid \
    --capacity 2 --json

# 2. Four tickets: one ad-hoc, one to assign, two to package.
arbite create --title "ad-hoc work"         --type chore --tier medium --domain python --epic board --priority 1
arbite create --title "direct assignment"   --type chore --tier medium --domain python --epic board --priority 2
arbite create --title "sequence member one" --type chore --tier medium --domain python --epic board --priority 3
arbite create --title "sequence member two" --type chore --tier medium --domain python --epic board --priority 4

# 3. What is ready, and what is not (exit 0 with work, 2 with none).
arbite board --worker claude.opus.001

# 4. The coordinator holds three: one assigned to Alice, two packaged for Bob.
arbite reserve create <direct> <member-one> <member-two> --agent coord.lead
arbite offer assign <direct> --worker claude.opus.001 --agent coord.lead
arbite package create <member-one> <member-two> --agent coord.lead
arbite offer publish --package <pkg> --agent coord.lead --min-tier medium --require-capability python

# 5. Bob cannot take Alice's assignment, so he accepts the sequence instead:
#    that starts member one and binds both members to him.
arbite claim <direct> --agent openai.gpt.002                 # exit 1: directly assigned
arbite offer claim <offer> --agent openai.gpt.002            # -> claimed <member-one> (..., attempt <att-one>)

# 6. Alice cannot steal Bob's second member -- plain or with --force (exit 1).
arbite claim <member-two> --agent claude.opus.001

# 7. Bob works member one through the proxy, with the attempt id step 5 printed
#    (or: arbite show <member-one> --json -> active_attempt.id).
arbite file claim src/step_one.py --ticket <member-one> --attempt <att-one>
printf 'step = 1\n' | arbite file write src/step_one.py --ticket <member-one> --attempt <att-one> --input -
arbite file read src/step_one.py --ticket <member-one> --attempt <att-one> --json   # -> read_token
printf 'step = 1  # openai.gpt.002\n' | arbite file write src/step_one.py --ticket <member-one> \
    --attempt <att-one> --read-token <token> --input -
arbite changes <member-one> --json                           # receipts, tagged with the attempt

# 8. Closing a member releases its file claims; the next member is a NEW attempt.
arbite close <member-one> --agent openai.gpt.002
arbite package claim <pkg> --agent openai.gpt.002             # member two, its own attempt id

# 9. Alice takes her assignment; the coordinator watches with no resident loop.
arbite claim <direct> --agent claude.opus.001
arbite reserve progress <rsv> --json                         # per-member counts + workers

# 10. Events are read once, and a NEW process resumes from the stored token.
arbite events --after 0 --limit 3 --json                     # -> data.next_cursor
arbite events --after <next_cursor> --limit 3 --json

# 11. Finish: the store is quiescent, so it transfers, board state and all.
arbite close <direct> --agent claude.opus.001
arbite reserve release <rsv> --agent coord.lead
arbite doctor                                                # exit 0, no problems
arbite migrate --to sqlite                                   # copies the board, rebinds
arbite board --worker claude.opus.001 --json                 # reads the destination
```

What to look for:

- `board --worker claude.opus.001` exits `0` with a `ready` list, the worker's
  declaration and its free capacity; with nothing ready it exits `2`.
- `reserve create` prints a reservation id and leaves every member `open` with
  `attempts created: 0`.
- `claim <direct> --agent openai.gpt.002` exits `1`: the ticket "is directly
  assigned to claude.opus.001 (offer off-...)".
- `claim <member-two> --agent claude.opus.001` exits `1`: the ticket "is member 2
  of package pkg-...; members are worked in order" -- identical with
  `--force --reason`.
- `package show <pkg>` ends `completed -- all 2 members closed (bound
  openai.gpt.002)`; the two members have **different** attempt ids in
  `arbite export`, and `arbite changes` shows each member's write receipts under
  its own attempt.
- After the first member closes, `file claim src/step_one.py` with *another*
  attempt succeeds (the claim was released), and the next member's file must be
  claimed and read again.
- `reserve progress` prints the counts (`{'completed': 2, 'active': 1, ...}`) and
  one line per member with its worker; the activity timestamps are observations,
  not liveness.
- `events --after 0 --limit 3 --json` returns cursors `1,2,3` plus a
  `next_cursor`; a **separate process** using that token returns `4,5,6`.
- `migrate --to sqlite` on the quiescent store copies the board: `worker list`,
  `reserve list --state all`, `events` and `board` all answer in the destination
  and `arbite doctor` still exits `0`. While a reservation or an offer is live the
  same command refuses with `store_not_quiescent` and names the records.

The same flow, with no human at the keyboard, is asserted by
[`tests/test_job_board_acceptance.py`](tests/test_job_board_acceptance.py:1) on
both sinks.

### Deferred

Live fleet registration, cross-project discovery, network transport, provider
adapters and spawning, bidding, dynamic price lookup, agent authentication,
notification delivery, heartbeat expiration, automatic stale recovery, and
cross-machine or global capacity arbitration. Worth knowing before relying on it: a
worker id and its provider/model labels are *attribution, not authentication*;
profile values are operator assertions, not verified facts; declared capacity is
never a global pool; `last_checkin` is a self-declaration, never liveness; the
attempt/claim event records the worker id but not the profile revision used at
acquisition; `unblock`'s resume path does not re-check reservations (it can only
resume the ticket's existing assignee); and mutation evidence accumulates with no
garbage collection.

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
| Creation | `create` (incl. `--blank` scaffolding), `raw <memo\|feature\|request\|bug\|wish>` plus the one-word shortcuts `bug` / `feature` / `request` / `wish` / `memo` |
| Triage | `fetch [type]` (oldest raw ticket + injected `derived_note`), `list raw` |
| Reading | `list` (flat, `next`, `raw`, `--topo`, `--tree`, `--epic`, `--tic`, `--count`), `search`, `show`, `deps` |
| Lifecycle | `claim`, `release`, `block`, `unblock`, `shelve`, `unshelve`, `close`, `reopen` |
| Authoring | `note`, `set`, `depend`, `move` |
| Proxy discovery/read | `file list`, `file search`, `file read [--lines S:E] [--fail-if-busy]`, `file probe` |
| Proxy mutation | `file claim`, `file release`, `file write`, `file edit`, `file remove`, `file rename` |
| Evidence | `changes [--attempt A] [--include-reads]` |
| Worker profiles | `worker register\|show\|list\|update\|disable\|enable\|checkin\|check` |
| Reservations | `reserve create\|show\|list\|progress\|add\|remove\|release` |
| Offers | `offer publish\|assign\|list\|show\|withdraw\|claim` (one ticket or a whole `--package`) |
| Packages | `package create\|show\|list\|claim\|note\|handoff` |
| Job board | `board --worker W [--epic E] [--tier T] [--count N]` (a query; claims nothing) |
| Events | `events --after CURSOR --limit N [--category C] [--kind K] [--subject ID]` |
| Storage | `migrate --to <sink> [--from] [--overwrite] [--prune] [--dry-run] [--coordination\|--no-coordination]`, `rebind --to <sink>`, `export [--scope ...] [--out FILE] [--no-artifacts]` |
| Integrity | `doctor [--fix]` (tickets plus coordination findings) |
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
- **`arbite file`** is the proxy surface: discover, read, claim and mutate
  workspace files so a competing agent cannot silently overwrite them. Discovery
  and reads take no lock; a replacement needs a fresh read after the claim, and a
  stale token is refused (`stale_read`) without changing bytes.
- **`arbite changes`** answers "what changed for this ticket, and is it
  verifiable?" — mechanical operations, agent prose and observed/unattributed
  drift are reported separately.
- **`arbite doctor`** checks the invariants nothing else enforces (including
  coordination findings: orphan claims, invalid generations, missing artifacts,
  pending intents and binding/revision drift) and exits `3` when problems remain —
  see [Integrity checking per sink](#integrity-checking-per-sink).

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
  AGENTS.md        generated quickstart (read first)
  REFERENCE.md     generated full reference and command list
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
- `AGENTS.md` and `REFERENCE.md` are regenerated on every `arbite init`. They are
  **not auto-discovered** — a project that wants agents to find arbite points at the
  quickstart explicitly, e.g. with `arbite init --claude-doc` / `--agents-doc`, which
  install (and on later runs refresh) a marked instructions block in the project's
  own `CLAUDE.md` / `AGENTS.md`; text outside the markers is never touched.

A SQLite-sink project has the same `.arbite/agents/` directories and `AGENTS.md`,
with `arbite.db` in place of the status folders.

### Ticket format

```yaml
---
id: tic-a1b2
title: Fix off-by-one in vertex normal calc
status: open              # raw | open | in_progress | blocked | shelved | closed — mirrors the folder
type: bug                 # bug | feature | request | refactor | chore | memo | wish
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
# A new project on the database sink: one command, and the choice sticks
arbite init --sink sqlite                  # creates arbite.db and sets sink: sqlite
arbite sink info                           # which store, where, what it supports

# Or bring an existing file-based project across
arbite migrate --to sqlite                 # copies every ticket and selects the database
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
  coordination.py       storage-neutral coordination records: workspaces, attempts,
                        claims, observations, receipts, intents, events, artifacts
  application.py        the storage-neutral coordination application layer + the
                        write-authorization guard
  lifecycle.py          guarded ticket transitions (claims, takeover, cleanup cascade)
  workspace.py          workspace binding, canonical root, exclusive binding
  paths.py              canonical in-root path resolution and refusals
  fileclaims.py         exclusive whole-file claims with generations
  filereads.py          discovery and versioned reads (non-write-authorizing receipts)
  filemutations.py      write/edit/remove/rename over the recoverable journal
  mutation.py           intent/artifact journal and filesystem recovery engine
  artifacts.py          content-addressed evidence storage
  locking.py            the coarse local coordination lock (no daemon, no timers)
  changes.py            the bounded change/evidence query surface
  coordination_export.py  coordination history export/migration
  coordination_doctor.py  coordination integrity findings (and unambiguous repairs)
  workers.py            passive worker profiles (register/update/disable/check-in)
  eligibility.py        worker eligibility vocabulary and pure evaluation
  reservations.py       coordinator reservations over explicit ticket sets
  offers.py             offers/direct assignments and atomic worker pickup
  readiness.py          the one readiness evaluator (board, list next, acquisition)
  packages.py           ordered same-worker continuity packages
  events.py             ordered, cursor-resumable event queries (+ progress views)
tests/
  test_sink_conformance.py  one suite, run against every sink
  test_file_sink.py         file-specific: folders, drift, temp files, archives
  test_sqlite_sink.py       sqlite-specific: schema, note index, pushdown parity
  test_cli.py               end-to-end argv, exit codes, --json, migrate round trip
  test_coordination_*.py    attempts/lifecycle, claims, reads, mutations, journal,
                            changes, export, migration, doctor (both sinks)
  test_proxy_acceptance.py  two-process shared-directory acceptance on both sinks
  test_job_board_acceptance.py  manual two-provider job-board acceptance (both sinks)
  test_graph.py             dependency-graph semantics, pinned
  test_query.py             query vocabulary semantics, pinned
scripts/
  seed_demo.py          generates ~2.5 months of realistic seed tickets for either
                        sink (--sink file|sqlite), via the same code paths the CLI uses
  proxy_recipe.py       the documented disposable-workspace two-agent recipe
                        (--sink file|sqlite); run it to see the flow end to end
  update-arbite.bat     reinstall the checkout into global pipx (Windows)
  update-arbite.sh      reinstall the checkout into global pipx (Linux/macOS)
```

## Status

The core system is implemented: creation, listing and filtering, dependency-ordered
`list next`, raw capture plus triage, the full claim/release/block/unblock/shelve/
close/reopen lifecycle, notes, `set`/`move`/`depend`, the `doctor` integrity checker,
the sink abstraction with both the file and SQLite implementations, sink selection
and `sink info`, `delete`, and `migrate`.

The shared-directory coordination layer is implemented on top of that: the
`arbite file` proxy (discovery, versioned reads, exclusive claims, journalled
write/edit/remove/rename), work attempts with guarded lifecycle cleanup, durable
change evidence (`arbite changes`), crash-safe recovery with no daemon, and
coordination-aware `export`/`rebind`/`migrate --coordination`/`doctor`. The
job board is implemented on top of that, and documented above: passive worker
profiles with an authoritative tier, enforced per-worker capacity and
eligibility/media checks; coordinator reservations with explicit, all-or-nothing
membership; offers and direct assignments with atomic pickup; ordered same-worker
continuity packages with explicit handoff; one readiness evaluator behind
`board`, `list next` and acquisition; a read-only reservation progress view; and
durable, cursor-resumable `arbite events`. Migration/export/doctor preserve and
verify the job-board records, and `arbite doctor` reports their integrity without
repairing it. Both sinks carry the same semantics, and the file sink never
requires SQLite. Deliberately absent, and documented as such above: any runner,
daemon, watcher or scheduler, provider adapters or spawning, automatic stale
detection or takeover, heartbeats, worktrees, a central database, a dashboard, and
artifact garbage collection. `CLAUDE.md` remains the canonical record of the
original design; open judgement calls and any schema change should be raised there
before implementation.
