# arbite — a file-based ticketing system for git repos and AI agents

`arbite` is a small Python CLI that keeps a project's task list as **plain markdown
files inside the repo**, under a `.arbite/` directory. Tickets are never stored in a
database or behind a server: they are files with YAML frontmatter, and a ticket's
**status is represented by the folder it sits in**. Moving a ticket between folders
*is* the state change, and because filenames stay stable across moves, `git log
--follow` on a ticket file traces its whole lifecycle for free.

## Example AGENTS.md
See AGENTS_EXAMPLE.md. An AGENTS.md or CLAUDE.md pointing the agent to the .arbite/AGENTS.md file will
in almost all cases cause agents to use the tickets system automatically

---

## Why

Agent-driven work tends to leave state in places that don't survive a new session:
an agent's own memory file, a chat transcript, a stale assumption about what it was
doing. Since the repo is already the durable, shared, versioned medium, arbite makes
the ticket's **folder location the single source of truth** for its state. Any other
record — an agent scratchpad, a prior session's notes — is treated as a *hint* to be
verified against the actual ticket, never as authority.

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

## Goals

- **Zero infrastructure.** No database, no daemon, no lock service. A clone of the
  repo is a complete working instance.
- **`ls` as a status board.** An agent or human should be able to tell what is
  actionable from folder names alone, without opening any file.
- **One invariant, enforced everywhere.** The frontmatter `status` mirrors the
  folder, and every state-changing command performs the file move *and* the
  frontmatter update in a single operation so the two can never disagree.
- **Safe under concurrency.** Claiming is atomic; races resolve to exactly one
  winner, and a lost race is reported, not silently overwritten.
- **Machine-first interfaces.** JSON output for every read query, distinct exit
  codes for success / error / empty / integrity-problems.
- **A self-describing command surface.** `arbite init` regenerates
  [`docs.py`](src/arbite/docs.py:1)-rendered `.arbite/AGENTS.md` straight from
  argparse's own `--help`, so the agent-facing docs cannot drift from the CLI.
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
  see [`load_known_agent_ids()`](src/arbite/config.py:38).
- **Scheduling or dispatching work.** arbite answers "what is workable"; getting an
  agent started on it is the caller's job.
- **A UI, web service, or notifications.** The CLI is the interface.
- **Enforcing policy beyond data integrity.** arbite refuses to corrupt state and
  reports drift; it does not police who may do what.

Planned but *not* part of arbite: any claim-on-startup collision detection or
timestamp-based staleness checks. The claim operation is corruption prevention
only.

---

## Design principles

- **Status is folder location.** `status` in the frontmatter mirrors the folder and
  must never be allowed to drift from it. When something outside arbite breaks that
  pairing (a hand `mv`, a merge, a rebase, an interrupted write), `arbite doctor`
  reports it and — with `--fix` — rewrites the frontmatter to match the folder,
  because **the folder wins**.
- **Stable filenames.** A ticket keeps the same filename in `open/`,
  `in_progress/`, and `closed/2026-08/`, so moves show up as folder-move commits
  under `git log --follow`.
- **Atomic writes and moves.** Saves stage a complete temp file and `os.replace`
  it into position ([`_write_atomic()`](src/arbite/ticket.py:297)); moves stage into
  the destination, unlink the source, then rename. The worst case of a crash is a
  visible, recoverable temp file — never two files sharing one id.
- **Compare-and-swap claiming.** Claiming uses an `O_CREAT|O_EXCL` create at the
  destination as the mutex ([`claim_ticket()`](src/arbite/ticket.py:459)); two agents
  racing for the same ticket cannot both come away believing they own it.
- **Ambiguity is an error for mutations.** Commands that modify a ticket require an
  unambiguous id and list the candidates otherwise; read-only commands keep the
  convenience of the first alphabetical match.
- **Controlled vocabularies live in one place.** `STATUSES`, `TYPES`, `TIERS` and
  friends in [`ticket.py`](src/arbite/ticket.py:16) are validated by both `set` and
  `doctor`, and interpolated into `.arbite/AGENTS.md`, so the docs, the CLI and the
  validator cannot drift apart.

---

## Scope: what is implemented

The package exposes the console script `arbite`, providing:

| Area | Commands |
| --- | --- |
| Setup | `init` |
| Creation | `create` (incl. `--blank` scaffolding), `raw <memo\|feature\|bug\|wish>` plus the one-word shortcuts `bug` / `feature` / `memo` / `wish` |
| Triage | `fetch [type]` (oldest raw ticket + injected `derived_note`), `list raw` |
| Reading | `list` (flat, `next`, `raw`, `--topo`, `--tree`, `--epic`, `--tic`, `--count`), `search`, `show`, `deps` |
| Lifecycle | `claim`, `release`, `block`, `unblock`, `shelve`, `unshelve`, `close`, `reopen` |
| Authoring | `note`, `set`, `depend`, `move` |
| Integrity | `doctor [--fix]` |

Key behaviours worth calling out:

- **`arbite list next`** returns the most urgent (lowest `priority` number) `open`
  ticket whose `depends_on` are all closed, in topological dependency order, and can
  filter by `--tier` / `--domain` / `--epic`. If nothing is ready it exits `2`; if
  everything is deadlocked it says so on stderr instead of leaving an agent polling a
  queue that can never yield work.
- **`arbite list next --claim <agent_id>`** selects *and* claims in one atomic step,
  closing the race inherent in calling `list next` then `claim`. If it loses the race
  for the top ticket it takes the next workable one rather than failing.
- **`--count N`** turns `list next` into a batch pull; with `--claim` each claim is
  individually atomic, so a short batch is a correct result, reported as such.
- **`arbite fetch`** implements the triage queue: it pulls the oldest `raw` ticket
  and prints it with a `derived_note` (a JSON field in `--json` mode, a leading block
  otherwise) telling the caller exactly how to classify it.
- **`arbite doctor`** checks the invariants nothing else enforces: status drift,
  duplicate ids, unreadable tickets, stray temp files, unsatisfiable dependency
  cycles, dangling and self dependencies, wrong `closed/YYYY-MM` months,
  `in_progress` with no assignee, `blocked` with no reason, and invalid field values.
  `--fix` repairs only the unambiguous cases; anything needing a judgement call is
  reported, never guessed. It exits `3` when problems remain, so it can gate CI or an
  agent's startup.

`CLAUDE.md` records a handful of deliberately open judgement calls — validation
strictness for `domain`/`tags`, the `deps` visualization format, and whether
`blocked_by` should support multiple blockers — plus a standing rule to ask before
schema changes.

---

## On-disk layout

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
- `wishlist/` and `planning/` are **non-status buckets**, not statuses: parked
  wishes and planning notes respectively. `arbite init` creates both.
- `agents/` holds one scratchpad file per known agent identity (no required schema),
  pre-created from the `agents:` list in `arbite.yaml` / `.arbite.yaml`.
- `AGENTS.md` is regenerated on every `arbite init`. It is **not auto-discovered** —
  a project that wants agents to find arbite must point at it explicitly (e.g. a line
  in its own `CLAUDE.md` like "read `.arbite/AGENTS.md`").

### Ticket format

```yaml
---
id: tic-a1b2
title: Fix off-by-one in vertex normal calc
status: open              # raw | open | in_progress | blocked | shelved | closed — mirrors folder
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
editable installs the way pip does):

```bash
pip install -e .
arbite --version
```

On Windows, [`update-arbite.bat`](update-arbite.bat:1) wraps
`python -m pipx install "." --force` to reinstall the current checkout into the global
pipx environment.

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

## Agent-facing conventions

These exist because agents, not humans, are the main callers:

- **`--json`** on `list`, `list next`, `fetch`, `show`, `search`, `deps` and
  `doctor` emits machine-readable output whose field names match the frontmatter. The
  human table format is explicitly *not* a stable interface.
- **Exit codes** let a shell loop branch without matching message text: `0` success
  with results, `1` error, `2` the query ran but matched nothing, `3` `doctor` found
  problems.
- **Prefer `list next --claim`** over `list next` followed by `claim`; the two-step
  version has a race another agent can win.
- **Prefer `arbite note`** over hand-editing a ticket file, so attribution and
  timestamps stay consistent.
- **Resume by verifying, not remembering.** Check your own scratchpad for a
  last-known ticket id, then confirm the ticket is actually in `.arbite/in_progress/`
  with `assignee` matching your id; the folder is ground truth, the scratchpad is a
  hint. If it is absent, stale, or mismatched, scan `in_progress/` for a ticket
  assigned to you.
- **Stay at or below your tier.** Pass your own tier to `arbite list next --tier`, so
  you are only offered work you can actually do.

---

## Repository layout

```
pyproject.toml          packaging + console-script entry point
update-arbite.bat       reinstall the checkout into global pipx (Windows)
CLAUDE.md               the original design spec and rationale
src/arbite/
  __init__.py           package version
  cli.py                argument parsing, command dispatch, exit codes
  ticket.py             ticket read/write, frontmatter parsing, atomic moves, claiming
  config.py             locating .arbite/ and loading known agent ids
  docs.py               renders .arbite/AGENTS.md from the real argparse output
scripts/
  seed_demo.py          generates ~2.5 months of realistic seed tickets for the
                        local .arbite/, via the same code paths the CLI uses
```

## Status

The core system is implemented: creation, listing and filtering, dependency-ordered
`list next`, raw capture plus triage, the full claim/release/block/unblock/shelve/
close/reopen lifecycle, notes, `set`/`move`/`depend`, and the `doctor` integrity
checker. `CLAUDE.md` remains the canonical record of the design; open judgement calls
and any schema change should be raised there before implementation.
