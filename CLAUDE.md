# arbite — file-based ticketing system

## Purpose

A low-tech, file-based ticket system that lives inside a git repo. Built for
a large, hand-written Blender addon codebase where multiple AI agents (and a
human) need to pick up tasks, track state, and leave a clean git history of
what happened and when. Tickets are plain files, moved between folders as
their status changes — no database, no server.

Build this as a Python CLI named `arbite`.

## Design principles

- Tickets are markdown files with YAML frontmatter: structured metadata up
  top, freeform human/agent-written notes in the body.
- **Status is represented by folder location, not just a field** — an agent
  or human should be able to tell what's actionable via `ls`, without
  opening files. The frontmatter `status` field mirrors the folder and must
  never be allowed to drift from it — the CLI enforces this on every move.
- **The ticket's folder location is the single source of truth for state.**
  Anything else (an agent's own memory file, assumptions from a previous
  session) is a hint, not authority, and must be checked against actual
  ticket location before being trusted.
- Filenames stay stable across folder moves (a ticket keeps the same
  filename whether it's in `open/`, `in_progress/`, or `closed/2026-08/`),
  so `git log --follow` traces a ticket's full lifecycle as folder-move
  commits — this is intentional, since it's how "dev history" gets
  generated from the ticket system for free.

## Directory layout

```
tickets/
  open/
  in_progress/
  blocked/
  closed/
    2026-08/
    2026-07/
    ...
  agents/
    claude.haiku.001.md
    claude.opus.001.md
    ...
```

- `closed/` is archived into monthly subfolders (`closed/YYYY-MM/`) based on
  close date, so it doesn't become one giant flat directory over time.
- `agents/` holds one persistent scratchpad file per agent identity — see
  "Agent identity and memory" below.

## Ticket frontmatter schema

```yaml
---
id: tic-a1b2
title: Fix off-by-one in vertex normal calc
status: open              # open | in_progress | blocked | closed — mirrors folder
type: bug                  # bug | feature | refactor | chore
tier: medium                # low | medium | high — capability level required
domain: mesh                 # what kind of agent/tool this needs, e.g. mesh, image_gen, audio_gen, ui, io
tags: [normals, curves]       # freeform, for human/codebase-area search — distinct from domain

assignee: null                # e.g. claude.haiku.001, or null if unclaimed
depends_on: []                  # list of other ticket ids that must close first (structural)
blocked_by: null                 # freeform reason OR a ticket id — why it's stalled, if status: blocked

created: 2026-08-06
updated: 2026-08-06
closed: null                      # set on close; drives which closed/YYYY-MM/ folder it archives into
---

## Description
Free text description of the task.

## Notes
- 2026-08-06: agent/human notes, findings, progress updates, appended over time.
```

Field notes:
- `depends_on` vs `blocked_by`: `depends_on` is structural (ticket ids,
  enables building a dependency tree). `blocked_by` is a human-readable
  explanation of what's actually stalling the ticket right now — it may
  reference a ticket id or may be entirely external ("waiting on upstream
  Blender API fix"). Keep these separate; don't collapse them.
- `tier` vs `domain`: `tier` is capability level (low/medium/high). `domain`
  is category/specialization (mesh, image_gen, audio_gen, etc.). Two
  independent axes — a ticket can need high-tier + audio_gen simultaneously.
  Don't merge into one field.
- `domain` vs `tags`: `domain` drives routing (what kind of agent should
  claim this). `tags` is for searching by codebase area. Different purposes
  even though both are strings.

## Agent identity and memory

- Agent identity format: `company.model.instance`, e.g. `claude.haiku.001`,
  `claude.opus.002`. The prefix (`company.model`) tells an agent its general
  class (useful for self-assessing whether it should claim a given `tier`).
  The instance suffix disambiguates concurrent instances of the same model.
- Persistent identity across sessions doesn't matter much — what matters is
  that concurrent instances of the same model don't collide, and that one
  instance can take over another's abandoned work.
- Identity assignment, collision avoidance, and staleness/liveness detection
  are all handled by the **agent harness** (planned separately, not part of
  this ticket system), since identities and metadata will be known ahead of
  time via config. Do NOT build claim-on-startup collision detection or
  timestamp-based staleness checks into `arbite` itself.
- Each agent has a scratchpad file at `tickets/agents/<agent_id>.md` — plain
  text/markdown, no required schema — where it records what ticket it's
  currently working on, so it can resume across sessions.
- Resume logic for an agent: check its own memory file first for a
  last-known ticket → verify that ticket is still in `in_progress/` with
  `assignee` matching its own id (ticket folder is ground truth, memory
  file is only a hint) → if memory file is absent or stale/mismatched, fall
  back to scanning `in_progress/` for a ticket where `assignee` matches its
  own id.

## CLI command surface (proposed — not yet finalized in detail)

Command name: `arbite`. Suggested commands to implement:
- `arbite init` — create `tickets/` folder structure if it doesn't exist, and create
  `agents/` scratchpad files for each known agent identity (from config)
- `arbite create` — create a new ticket in `open/`
- `arbite list [--status --tier --domain --assignee]` — list/filter tickets
- `arbite claim <id> --agent <id>` — move ticket to `in_progress/`, set
  `assignee`, update `status` and `updated`
- `arbite block <id> --reason <text or ticket id>` — move to `blocked/`,
  set `blocked_by`, update `status`
- `arbite close <id>` — move to `closed/YYYY-MM/` (by current date), set
  `status: closed` and `closed` date
- `arbite show <id>` — print a ticket's full contents
- `arbite deps <id>` — walk `depends_on` to show a dependency tree (nice
  to have, not required for first pass)

All state-changing commands must perform the folder move AND update the
frontmatter (`status`, `updated`, and any command-specific fields) in the
same operation, so folder and frontmatter never disagree.

## Packaging / installation

`arbite` should be installable globally via `pipx`, so it's available as a
plain `arbite` command from any directory without needing to activate a
virtualenv.

- Structure this as a proper installable Python package (not a loose
  script). Use `pyproject.toml` with a console script entry point:

```toml
[project]
name = "arbite"
version = "0.1.0"
description = "File-based ticketing system for git repos and AI agents"
requires-python = ">=3.9"
dependencies = [
    "pyyaml",           # or "python-frontmatter", pick one and use it consistently
]

[project.scripts]
arbite = "arbite.cli:main"

[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"
```

- Package layout should follow the standard `src/` or flat-package layout,
  e.g.:
```
arbite/
  pyproject.toml
  src/
    arbite/
      __init__.py
      cli.py        # entry point, argument parsing, command dispatch
      ticket.py      # ticket read/write, frontmatter parsing
      ...
  tickets/            # example/default tickets dir for this repo itself, if applicable
```
- `cli.py` should expose a `main()` function (no required args) that argparse
  or click can hang off of — this is what the `arbite = "arbite.cli:main"`
  entry point calls.
- Once packaged, install and test with `pipx install .` from the project
  root (or `pipx install -e .` isn't supported by pipx for editable installs
  the way pip is — for active development, prefer running via
  `python -m arbite.cli` or `pip install -e .` in a local venv, and only use
  `pipx install .` for the "final" global install once it's stable).
- Keep runtime dependencies minimal (YAML parsing library is likely the only
  hard requirement) since pipx installs into an isolated venv per tool and
  extra dependencies just add install time for no benefit here.

## Not yet decided / open for judgment calls

- Exact validation strictness for `domain` and `tags` (fixed enum vs free
  string) — free-string is fine as a starting default.
- `arbite deps` dependency-tree visualization format.
- Whether `blocked_by` should support a list (multiple blockers) or stay
  single-value — single value with a string is fine as a starting default,
  revisit if it comes up in practice.

Implement a first working version covering ticket creation, listing,
claiming, blocking, and closing, with the frontmatter/folder sync rule
enforced everywhere. Ask before making schema changes beyond what's above.
