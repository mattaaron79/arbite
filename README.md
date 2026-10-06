# arbite — a ticket store for git repos and AI agents, with pluggable sinks

`arbite` is a small Python CLI that keeps a project's task list durable and
inspectable while AI agents (and humans) work on it. Tickets are never behind a
server or a daemon: they live in a **sink**, and the default sink is the filesystem
— plain markdown files with YAML frontmatter under a `.arbite/` directory, where a
ticket's **status is represented by the folder it sits in**. Because filenames stay
stable across moves, `git log --follow` on a ticket file traces its whole lifecycle
for free. An opt-in `sqlite` sink keeps the same tickets in one database file, where
`status` is a column and queries are real SQL. Every command, field, exit code and
`--json` payload is identical whichever sink is in use.

## Install

`arbite` needs Python 3.9+ and is not published on PyPI yet, so install it from a
clone of this repo:

```bash
git clone https://github.com/mattaaron79/arbite.git
cd arbite
pipx install .        # puts `arbite` on PATH for your user (plain `pip install .` also works)
arbite --version      # confirm it is on PATH
```

Then initialise it in the repo you want to track tickets in:

```bash
cd /path/to/your/project
arbite init           # creates .arbite/ and a pointer at .arbite/AGENTS.md
```

`arbite init` also installs the instructions block into your `AGENTS.md` /
`CLAUDE.md` if you pass `--agents-doc` / `--claude-doc`, and the runtime-state
entries into `.gitignore` if you pass `--gitignore`.

## Documentation lives in the command

Arbite documents itself, so the docs cannot drift from the installed version and
nothing is regenerated into your git tree:

```bash
arbite docs                 # the token-light overview
arbite docs list            # every topic, one line each
arbite docs <topic>         # one subject in depth
arbite docs commands [NAME] # every command, or one command's usage and flags
arbite docs search TERM     # which topics and lines mention a term
arbite docs all             # everything, in one stream (e.g. to write a file)
```

Topics: `workflow`, `fields`, `sinks`, `agents`, `triage`, `conventions`,
`workspace`, `limits`, `design`. The prose follows the sink in use, so a
database-backed project is never told "the folder is the source of truth".

## Quick start

```bash
arbite init                                   # create .arbite/ (file sink by default)
arbite raw feature "add per-mesh LOD"         # capture a thought without classifying it
arbite fetch                                   # read the oldest raw ticket
arbite promote tic-a1b2 --title "Add per-mesh LOD" --tier medium --domain mesh
arbite claim tic-a1b2 --agent claude.haiku.001 # take it (status -> in_progress)
arbite note tic-a1b2 claude.haiku.001 "progress"
arbite submit tic-a1b2                         # hand off: review/, or closed when review: false
arbite accept tic-a1b2 --agent claude.opus.001 # the reviewer closes it, credited
```

A new project on the database sink is one command, and the choice sticks:

```bash
arbite init --sink sqlite     # creates the database AND sets 'sink: sqlite' in .arbite/project.yaml
arbite migrate --to sqlite    # or bring an existing file-based project across
```

## Why arbite

Agent-driven work tends to leave state in places that don't survive a new session:
an agent's own memory file, a chat transcript, a stale assumption about what it was
doing. Since the repo is already the durable, shared, versioned medium, arbite puts
the tickets there and makes one location the single source of truth for a ticket's
state. Keeping the tickets as plain files means an agent or human can also read the
board from folder names alone, and `git log --follow` gives a ticket's whole
lifecycle for free.

Explicitly out of scope: agent identity, liveness and staleness detection (that is
an external harness's job), scheduling or dispatching work, a UI or web service, and
a distributed store. `arbite docs design` has the goals, non-goals and design
principles in full.

## Repository layout

| Path | What it is |
| --- | --- |
| `src/arbite/` | the CLI (`cli.py`), the docs surface (`docs.py`), the ticket schema and the query engine |
| `src/arbite/sinks/` | the storage interface (`base.py`) and its two implementations (`file.py`, `sqlite.py`) |
| `src/arbite/coordination/` | the file proxy: claims, attempts, receipts, events |
| `tests/` | the pytest suite |
| `INITIAL_DESIGN_DOC.md` | the original design rationale (historical) |

## Status

`arbite` is under active development; see `git log` for what has landed. Run
`arbite docs` for the guide, or `arbite <command> -h` for a command's own help.
