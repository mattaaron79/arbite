"""Renders .arbite/AGENTS.md: a self-describing reference doc written by `arbite init`.

Two jobs, one file: it explains the workflow, and it carries the command
reference. Kept in the package rather than hand-maintained per repo so it can't
drift from the actual CLI -- the command section is rendered straight from the
installed argparse parsers, and the prose interpolates the same vocabulary
constants the CLI validates against (it previously advertised a `frontier` tier
that `create` and `list --tier` both rejected).

It is also *sink-aware*: the file it renders is read by agents that will act on
what it says, so it may only claim what is true of the store in use. With a file
sink "a ticket's folder is the source of truth" is the central rule and belongs
front and centre; with a database sink that sentence would be a lie, so the
status/location prose switches on the sink's own capabilities instead of being
hard-coded.

Written terse on purpose: this file is read into an agent's context at the start
of every task, so it is optimised for facts-per-token rather than for prose.
Boilerplate that would otherwise repeat once per command is stated once and
cross-referenced, and argparse's longer per-command descriptions are dropped
because the prose sections already carry what they say -- `arbite <cmd> -h`
still prints them in full.

TICKET_ID_HELP / TICKET_ID_HELP_READONLY / JSON_HELP / MESSAGE_HELP live here
(cli.py imports them) so the renderer can collapse the copies of that
boilerplate into a single cross-reference without the two copies drifting apart.
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

from . import __version__
from .schema import (
    CLASSIFICATION_EPIC,
    FIELD_ORDER,
    RAW_TYPE_CHOICES,
    STATUSES,
    TIER_VALUES,
    TYPES,
)

# ---------------------------------------------------------------------------
# Help text shared with the CLI (single source of truth for both the real
# --help output and the dedupe substitutions in the renderer below).
# ---------------------------------------------------------------------------

TICKET_ID_HELP = (
    "ticket id or any wildcard (substring) match, e.g. 'f6' or 'tic-f607' both "
    "resolve to tic-f607; an exact id always wins, and for commands that modify "
    "a ticket an ambiguous match is an error listing the candidates rather than "
    "a guess"
)

# Read-only commands keep the old convenience: a guess there costs nothing.
TICKET_ID_HELP_READONLY = (
    "ticket id or any wildcard (substring) match, e.g. 'f6' or 'tic-f607' both "
    "resolve to tic-f607; if several tickets match, the first alphabetically is used"
)

JSON_HELP = (
    "emit machine-readable JSON on stdout instead of a formatted table "
    "(field names match the ticket frontmatter); exit code 2 means the query "
    "ran but matched nothing"
)

MESSAGE_HELP = (
    "brief request to capture, e.g. \"users can't save without auth\" "
    "(joined with spaces if multiple words)"
)

# ---------------------------------------------------------------------------
# The instructions block `arbite init --agents-doc` / `--claude-doc` installs
# into a project's own AGENTS.md / CLAUDE.md.
# ---------------------------------------------------------------------------

# Markers delimit the block. They are the sole thing `install_instructions` looks
# for, so a project may move, annotate or extend the block without `arbite init`
# stacking a second copy on top of it.
ARBITE_INSTRUCTIONS_BEGIN = "<!-- BEGIN ARBITE INSTRUCTIONS -->"
ARBITE_INSTRUCTIONS_END = "<!-- END ARBITE INSTRUCTIONS -->"

# The block is stored verbatim rather than read from AGENTS_EXAMPLE.md at runtime:
# an installed arbite must not depend on the source tree it was built from. Keep it
# byte-identical to the markdown block in AGENTS_EXAMPLE.md.
ARBITE_INSTRUCTIONS_BLOCK = """\
<!-- BEGIN ARBITE INSTRUCTIONS -->
# Arbite Ticketing System

## Ticketing
Use arbite ticketing system for all tasks. See /.arbite/AGENTS.md. Create a ticket if required and claim the ticket before starting work.

# Agent Identiy

When claiming a ticket, please use an identity format like: "claude.opus-5.001" where the company.model.instance is your best educated guess unless otherwise specified.
If orchestrating, let subagents know their identity and instance number.

## Shared directory: use arbite for file work
Read, claim and mutate source files through arbite -- not a shell or editor -- so a competing agent cannot silently overwrite your work:
```bash
arbite file list [PATH] --json                        # discover
arbite file search PATTERN [PATH] --json             # discover
arbite file claim PATH --ticket T --attempt A        # own the whole file
arbite file read  PATH --ticket T --attempt A --json # RE-READ; a pre-claim read does not authorize a write
arbite file write|edit|remove|rename ... --read-token R
```
Re-read after every claim, takeover and ticket boundary, and take a fresh read before each mutation: a token is single-use, and a stale or consumed token is refused with `stale_read` and no bytes change. A binary file takes `arbite file read PATH ... --version-only`, which returns a token without serving content.
Every `arbite file` command needs your active `--attempt` id; get it from `arbite export --scope coordination --no-artifacts` (the `work_attempts` entry with your ticket_id and `state: active`).
There is no runner, daemon, watcher or scheduler, and stale work is never taken over automatically -- agents are started manually and may use different providers; only `arbite claim --force --reason <why>` moves live work. Keep build/test output outside managed source paths: arbite records proxy mutations only, so a generated file written into the source tree is unattributed drift. Mutation evidence and artifacts accumulate and are never garbage-collected. Arbite enforces its own operations and reports observed drift, but it cannot prove who made a direct filesystem change -- an external editor or shell can still bypass the proxy.

## Sole command: "Work Next|All <epic>"

If your sole command is "Work Next" or "Work All", you can use the following commands to find the next arbite ticket(s):
```bash
arbite list next   # Show next workable ticket
arbite list --topo --status open [--epic <epic>]
```

Note: If there are no tickets, see next command "Classify". If "Work All", try to orchestrate tickets if that is in your skill set, otherwise
work in sequence until finished.

## Sole command "Classify"

If your sole command is "Classify" use the following command to list all tickets that require classification:
```bash
arbite list raw
```

Use your session to classify all tickets, looking deeper into the requirements, adding notes, etc until all raw tickets are classified.

# Ticketing etiquette addendum

In addition to etiquette specified in .arbite/AGENTS.md, please add to notes of ticket when closing a paragraph explaining what the user, QA, or other
agents will be able to observe via integration testing, if any new effects will be observable.

## Git
By default and unless otherwise specified, check into main/master after closing a ticket.

Do not modify this file without explicit permission.

## Comment Protocol

Do not fill the codebase with comments containing history, musings, or overly wordy explanations. Comments should be maximally useful and concise. Put long explanations and related context in Arbite tickets, and refer to the relevant ticket from a code comment when needed.
<!-- END ARBITE INSTRUCTIONS -->"""


def install_instructions(path: Path) -> str:
    """Put the arbite instructions block into the agent doc at `path`.

    Returns what it did, so `arbite init` can report it:

    - 'created'  -- the file did not exist; it is written with the block alone;
    - 'prepended' -- the file existed without the block; the block is prepended and
      the existing contents are preserved below it;
    - 'present'  -- the file already carries the block, so it is left untouched.

    Idempotent by demarkation: re-running `arbite init` must never stack a second
    copy, whatever else the file contains."""
    if not path.exists():
        path.write_text(ARBITE_INSTRUCTIONS_BLOCK + "\n", encoding="utf-8")
        return "created"
    existing = path.read_text(encoding="utf-8")
    if ARBITE_INSTRUCTIONS_BEGIN in existing or ARBITE_INSTRUCTIONS_END in existing:
        return "present"
    path.write_text(
        ARBITE_INSTRUCTIONS_BLOCK + "\n\n" + existing.lstrip("\n"), encoding="utf-8"
    )
    return "prepended"

# Per-field descriptions for the frontmatter table, keyed by field name.
# Interpolating the vocabulary constants keeps the doc honest about what the
# CLI actually accepts.
FIELD_NOTES = {
    "id": "unique ticket id, e.g. tic-a1b2 -- generated by `arbite create`, never set by hand",
    "title": "short human-readable summary",
    "status": f"{' | '.join(STATUSES)} -- the ticket's state in the workflow; 'raw' = "
    "unclassified, not workable, never offered by `list next` (see Triage). A file sink "
    "additionally keeps this in sync with the folder the ticket sits in",
    "type": f"{' | '.join(TYPES)} -- 'memo' (from `raw memo`) = update project notes/docs, not "
    "code; 'request' (from `raw request`) = a request for a change, not necessarily a bug or a "
    "new feature but a tweak or lateral change -- ordinary work once classified; 'wish' (from "
    "`raw wish`) = a wishlist item, reclassified as 'feature' and filed, never worked",
    "tier": f"{TIER_VALUES} -- the **agent capability tier** required to work the ticket: how "
    "capable the agent must be, ascending. The harness tells you your tier, or you self-assess "
    "from your model class (the company.model prefix of your agent id, e.g. claude.haiku sits "
    "below claude.opus); claim only at or below your tier",
    "domain": "what kind of agent/tool this needs, e.g. mesh, image_gen, audio_gen, ui, io -- "
    "drives routing",
    "epic": "the larger initiative this ticket belongs to, e.g. mesh-pipeline -- a freeform "
    "grouping label for filtering (`list [next] --epic`), not a dependency or ticket id",
    "priority": "numeric urgency index, lower = more urgent (1 is highest) -- orders which "
    "workable ticket to pick up next; unset (null) sorts last",
    "tags": "freeform list, for human/codebase-area search -- distinct from domain",
    "assignee": "agent id currently working the ticket, e.g. claude.haiku.001, or null if unclaimed",
    "depends_on": "list of other ticket ids that must close first (structural)",
    "blocked_by": "freeform reason OR a ticket id -- why it's stalled, only meaningful when "
    "status: blocked",
    "created": "creation timestamp (YYYY-MM-DDTHH:MM:SS); a bare YYYY-MM-DD also validates",
    "updated": "timestamp of the most recent change",
    "closed": "close timestamp, null until closed",
}

# ---------------------------------------------------------------------------
# Compaction helpers
# ---------------------------------------------------------------------------

# Boilerplate that would otherwise appear once per command: each entry is
# (verbatim argparse help text, short cross-reference to render instead).
_SUBSTITUTIONS = (
    (TICKET_ID_HELP, "see 'Ticket ids' under Conventions"),
    (TICKET_ID_HELP_READONLY, "see 'Ticket ids' under Conventions"),
    (JSON_HELP, "JSON output (see Conventions)"),
    (MESSAGE_HELP, "the request text (multiple words are joined with spaces)"),
    (
        "Tier is how capable the agent must be, ascending -- not how urgent the work "
        "is (that's priority, lower = more urgent) and not what specialization it "
        "needs (that's domain). Those axes are independent: a trivial chore can be "
        "urgent, and a low-tier ticket can still be audio_gen-only. An agent is "
        "either told its own tier by the harness or self-assesses from its model "
        "class (the company.model prefix of its agent id, e.g. claude.haiku sits "
        "below claude.opus), and should only claim tickets at or below that tier.",
        "See 'tier' under Ticket fields.",
    ),
)

# Actions that take no value, so their label needs no metavar.
_NO_VALUE_ACTIONS = (
    argparse._HelpAction,
    argparse._StoreTrueAction,
    argparse._StoreFalseAction,
    argparse._CountAction,
    argparse._VersionAction,
)

# The global storage selector appears on every command; documenting it once, in
# the sink section, beats repeating it under all twenty-odd commands.
_GLOBAL_ACTIONS = ("help", "sink", "version")


# ANSI/CSI escape sequences. argparse >= 3.14 colours its help output when it
# believes it is writing to a terminal (or when FORCE_COLOR is set, which agent
# harnesses do), and those codes would otherwise end up embedded in the markdown
# as junk like '\x1b[1;34m'. This file is always read as plain text, so they are
# stripped -- see _disable_color() for the belt to this braces.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _flat(text: str) -> str:
    """Collapse a wrapped help string onto one line, with no escape codes."""
    return _ANSI_RE.sub("", " ".join((text or "").split()))


def _disable_color(*parsers) -> None:
    """Ask parsers not to colourise their help (a no-op before Python 3.14).

    The renderer strips escapes either way, so this only keeps the intermediate
    text readable when debugging; the parsers are transient ones built for
    `arbite init`, never the ones serving `--help` to a user.
    """
    for parser in parsers:
        if hasattr(parser, "color"):
            parser.color = False


def _shorten(text: str) -> str:
    """Collapse and cross-reference the boilerplate repeated across commands."""
    text = _flat(text)
    for needle, replacement in _SUBSTITUTIONS:
        text = text.replace(needle, replacement)
    return text


def _action_label(action) -> str:
    """`--flag ARG` / `-r/--regex` / `POSITIONAL` for one argparse action."""
    if action.option_strings:
        label = "/".join(action.option_strings)
        if not isinstance(action, _NO_VALUE_ACTIONS) and action.nargs != 0:
            label += " " + (action.metavar or action.dest.upper())
        return label
    return str(action.metavar or action.dest)


def _iter_actions(subparser):
    """Every non-help action of a subparser: positionals and options alike."""
    for action in subparser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        if action.dest in _GLOBAL_ACTIONS:
            continue
        yield action


def _is_shorthand(subparser) -> bool:
    """True for `arbite bug|feature|memo|wish <message>`, the one-word forms of
    `arbite raw <type> <message>`: they are described as shorthands by the CLI
    itself, so their block would just repeat `arbite raw`'s. Detecting it from
    that wording fails safe -- if the wording ever changes they simply render in
    full again, just less tersely.
    """
    return "shorthand for" in _flat(subparser.description or "").lower()


def _usage(subparser) -> str:
    text = _flat(subparser.format_usage())
    return text[len("usage: "):] if text.startswith("usage: ") else text


def render(parser, subparsers_by_name: dict, active_info=None, stale_info=None) -> str:
    """Render the guide for the sink this project will actually use.

    `active_info` describes the store a *plain* command reads: the committed config,
    with no `--sink` and no `ARBITE_SINK` in play -- which is what an agent reading
    this file will get when it runs the commands here. `stale_info`, when given,
    describes another store in the same project that *has tickets* and that nothing
    selects (a database left behind by a deleted config, an `ARBITE_SINK` one-off,
    a half-finished migration).

    An agent reads this guide and then runs `arbite` with no flags, so the prose that
    describes *behavior* follows `active_info`, and a stale store is called out in
    bold rather than quietly ignored. A guide that named the wrong store would be
    worse than no guide at all: the wrong store looks exactly like an empty one."""
    _disable_color(parser, *subparsers_by_name.values())

    # Behavior prose (folder vs column, how to resume) describes what a plain
    # command will actually do.
    primary = active_info if active_info is not None else stale_info
    status_is_location = bool(getattr(primary, "status_is_location", False))
    sink_kind = getattr(primary, "kind", None)
    sink_root = getattr(primary, "root", None)
    stale_kind = getattr(stale_info, "kind", None)
    stale_root = getattr(stale_info, "root", None)
    stale_count = getattr(stale_info, "ticket_count", 0)
    mismatch = active_info is not None and stale_info is not None

    lines: list[str] = []
    add = lines.append

    add("# arbite -- agent guide")
    add("")
    add(
        f"_Auto-generated by `arbite init` (arbite {__version__}) on {date.today().isoformat()}: "
        "every `arbite init` run here rewrites it, so edit the package's docs template rather than "
        "this file. Not auto-discovered -- point your project's CLAUDE.md at it explicitly (e.g. a "
        "line 'read .arbite/AGENTS.md')._"
    )
    add("")

    # -- What this is ------------------------------------------------------
    add("## What this is")
    add("")
    add(
        "`arbite` is a low-tech ticketing system that lives inside this git repo, so several AI "
        "agents (and humans) can pick up tasks, track state, and leave a clean history of what "
        "happened and when."
    )
    add("")
    add(
        "Use `arbite` to create, claim, block, shelve, close and reopen work instead of ad hoc notes "
        "or files. Leave progress with `arbite note` rather than editing the ticket -- it appends a "
        "timestamped, agent-identified entry to the ticket's `## Notes` section."
    )
    add("")
    if status_is_location:
        add(
            "**Where a ticket is filed *is* its state.** Tickets are markdown files with YAML "
            "frontmatter, and the folder a ticket sits in mirrors its `status`; moving between "
            "folders is what a state change means, and every command does the move and the "
            "frontmatter update together. The folder is the source of truth: if the two ever "
            "disagree, the folder wins. An agent's memory of what it was doing is only a hint, to "
            "be checked against where the ticket actually is: a claimed ticket is one sitting in "
            "`in_progress/` with `assignee` set to your id."
        )
    else:
        add(
            "**Status is a field, and that field is the single source of truth** -- there is no "
            "folder to read as a shortcut. An agent's memory of what it was doing is only a hint, to "
            "be checked against the ticket itself: a claimed ticket is one whose `status` is "
            "`in_progress` with `assignee` set to your id. Check with "
            "`arbite show <id> --json` or `arbite list --assignee <your-id>`."
        )
    add("")

    # -- Where tickets live ------------------------------------------------
    add("## Where tickets live (the sink)")
    add("")
    add(
        "Tickets live in a **sink**: a storage backend selected per command. Everything above the "
        "sink -- every command, every field, every exit code -- behaves identically whichever one is "
        "in use, so only two things change: where the data sits, and what `arbite doctor` can check."
    )
    add("")
    if mismatch:
        add(
            f"> **Warning: this project holds a second ticket store that nothing selects.** "
            f"There are {stale_count} ticket(s) in a `{stale_kind}` store at `{stale_root}`, "
            f"but a plain `arbite` command -- no `--sink`, no `ARBITE_SINK` -- reads the "
            f"`{sink_kind}` store at `{sink_root}` instead. Decide before running anything "
            f"that writes: either point the project at the other store (add `sink: "
            f"{stale_kind}` to `arbite.yaml`, or pass `--sink {stale_kind}`), or bring its "
            f"tickets across with `arbite migrate --from {stale_kind} --to {sink_kind}`. "
            f"**The wrong store looks exactly like an empty one**, so if a command reports no "
            f"tickets, run `arbite sink info --json` and check its `kind` field before "
            f"concluding the queue is empty."
        )
        add("")
    if sink_kind:
        add(
            f"- active sink: `{sink_kind}`"
            + (f" at `{sink_root}`" if sink_root else "")
            + " -- what a command with no `--sink` flag reads"
        )
    else:
        add("- active sink: see `arbite sink info`")
    if mismatch:
        add(
            f"- also present, but not selected: `{stale_kind}` at `{stale_root}` "
            f"({stale_count} ticket(s))"
        )
    add(
        "- confirm it at any time: `arbite sink info --json` -- its `kind` field is the store "
        "your command will read"
    )
    add(
        "- selection, highest precedence first: `--sink <kind>`, the `ARBITE_SINK` environment "
        "variable, a `sink:` key in `arbite.yaml`, then the default (`file`)"
    )
    add(
        "- that `sink:` key is the committed choice and is what makes one store stick for every "
        "command; `arbite init` and a successful `arbite migrate` write it for you"
    )
    add(
        "- available kinds: `file` (markdown files under the arbite directory, the default, and the "
        "one that gives you `git log --follow` history) and `sqlite` (a single database file, "
        "queryable with real SQL, and not version-control friendly)"
    )
    add("- report it, or create it: `arbite sink info`, `arbite sink init`")
    add("")
    if status_is_location and sink_kind == "file":
        add("```")
        add(".arbite/")
        add("  raw/            unclassified captures -- not workable (see Triage)")
        add("  open/           actionable, unclaimed")
        add("  in_progress/    claimed, being worked")
        add("  blocked/        stalled -- see blocked_by")
        add("  shelved/        parked for later")
        add("  closed/YYYY-MM/ archived by close date")
        add("  wishlist/       reclassified wishes -- parked, not work")
        add("  planning/       planning/roadmap notes and scratch docs -- not tickets")
        add("  agents/         one scratchpad file per agent identity")
        add("```")
        add("")
        add(
            "The folders above are status *and* buckets. `raw/`, `open/`, `in_progress/`, "
            "`blocked/`, `shelved/` and `closed/YYYY-MM/` are status folders: the ticket's "
            "frontmatter `status` mirrors the folder, every state-changing command updates both at "
            "once, and when something outside arbite breaks the pairing the folder wins. "
            "`wishlist/` and `planning/` are buckets, not statuses: a ticket filed in one is out of "
            "the status workflow (so it is never offered by `list next`) but keeps whatever status "
            "it had. Filenames never change on a move, so `git log --follow` on a ticket file "
            "traces its whole lifecycle."
        )
    else:
        add(
            "This project does not store tickets as files, so there is no folder to read as state "
            "and nothing for `git log --follow` to follow: the sink holds the tickets and `status` "
            "is held with them. `arbite doctor` checks the invariants that still apply (invalid "
            "field values, dependency cycles, dangling dependencies, claimed tickets with no "
            "assignee) plus its own storage-specific ones."
        )
    add("")
    add(
        "**Buckets** are how a ticket is parked outside the status workflow -- `arbite move <id> "
        "/wishlist` files it in the wishlist bucket, and `arbite move <id> /` brings it back to its "
        "status location. What a bucket physically is belongs to the sink (a folder in the file "
        "sink, a recorded bucket in a database sink), so no command depends on it."
    )
    add("")

    # -- Fields ------------------------------------------------------------
    add("## Ticket fields (YAML frontmatter)")
    add("")
    for field_name in FIELD_ORDER:
        add(f"- `{field_name}` -- {FIELD_NOTES.get(field_name, '')}")
    add("")
    add(
        "These axes are independent -- don't collapse them: `depends_on` (structural ticket ids) vs "
        "`blocked_by` (freeform prose); `tier` (capability) vs `domain` (specialization) vs "
        "`priority` (urgency) -- an urgent low-tier chore is possible, and a low-tier ticket can "
        "still be audio_gen-only; `domain` (routing) vs `tags` (search). `epic` groups for filtering "
        "only. Given a choice of workable tickets, take the lowest `priority` number (`list` shows "
        "the more urgent first within a status)."
    )
    add("")

    # -- Identity ----------------------------------------------------------
    add("## Agent identity and resuming work")
    add("")
    add(
        "Agent ids are `company.model.instance`, e.g. `claude.haiku.001`; each agent has a scratchpad "
        "at `.arbite/agents/<agent_id>.md` recording what it is working on. Know your `tier` before "
        "claiming (see Ticket fields) and pass it to `arbite list next --tier <tier>` so you are only "
        "offered work you can actually do."
    )
    add("")
    if status_is_location:
        add(
            "To resume: check your scratchpad for a last-known ticket id, then verify that ticket is "
            "still where a claimed ticket belongs -- in `in_progress/`, with `assignee` matching your "
            "own id (`arbite show <id> --json` prints its path). The location is ground truth, the "
            "scratchpad only a hint. If it is missing, stale or mismatched, look for a ticket "
            "assigned to you with `arbite list --assignee <your-id>`. Identity assignment, collision "
            "avoidance and liveness detection belong to the agent harness, not arbite."
        )
    else:
        add(
            "To resume: check your scratchpad for a last-known ticket id, then verify that ticket is "
            "still `status: in_progress` with `assignee` matching your own id "
            "(`arbite show <id> --json`). That is ground truth, the scratchpad only a hint. If it is "
            "missing, stale or mismatched, look for a ticket assigned to you with "
            "`arbite list --assignee <your-id>`. Identity assignment, collision avoidance and "
            "liveness detection belong to the agent harness, not arbite."
        )
    add("")

    add("## Worker profiles (optional)")
    add("")
    add(
        "A worker id needs no registration: unregistered (ad-hoc) ids claim work exactly as above. "
        "An operator may register a passive profile with `arbite worker register <id> --tier <tier> "
        "[--provider/--model/--runtime LABEL] [--capability CAP] [--locality local|remote|unknown] "
        "[--cost-class local|paid|unknown] [--cost-amount N --cost-unit U --cost-provenance TEXT] "
        "[--capacity N]`, then `worker show|list|update|disable|enable|checkin|check`, all with "
        "`--json`. Registration launches nothing and calls no provider; labels are labels, every "
        "value is an operator assertion (not verified identity), and credential-like values are "
        "refused. Once registered, the profile's tier is authoritative for that id: `claim` and "
        "`list next --claim` refuse tickets above it (`worker_ineligible`, reason "
        "`tier_insufficient`), `list next --tier` may narrow but never exceed it "
        "(`declared_tier_exceeds_profile`), `--force` does not bypass it, and a disabled profile "
        "takes no new work while its history and running attempts are kept. Tier changes happen "
        "only through `arbite worker update --tier`, recorded as a `worker_updated` event. "
        "`last_checkin` is the worker's own declaration, never verified liveness; declared "
        "capacity is recorded but not yet enforced. `arbite worker check <id> [--ticket T] "
        "[--require-capability CAP] [--local-only] [--max-cost N --max-cost-unit U]` explains "
        "eligibility with stable reason codes and writes nothing; an unknown value fails an "
        "explicit constraint. Reservations, offers, packages and a job board are not available yet."
    )
    add("")

    # -- Workflow ----------------------------------------------------------
    add("## Typical workflow")
    add("")
    add("```")
    add("arbite list --status open --domain mesh --tier medium   # find something to work")
    add("arbite list next --tier high                            # next workable open ticket")
    add("arbite list next --count 3 --tier high                  # ...or a batch of three")
    add("arbite list next --epic mesh-pipeline                   # next workable ticket in an epic")
    add("arbite list next --epic classification                  # next raw ticket needing triage")
    add("arbite list raw                                         # raw backlog as a todo list")
    add('arbite search --params title,body "LOD pop-in"          # find tickets by text')
    add('arbite raw feature "add per-mesh LOD"                   # quick capture; classify later')
    add('arbite raw request "collapse the toolbar by default"     # a tweak/lateral change request')
    add('arbite raw wish "fly-through camera preview"            # capture a wish; file it later')
    add("arbite fetch [type]                                     # oldest raw ticket to classify")
    add("arbite move tic-a1b2 /wishlist                          # file a reclassified wish")
    add("arbite move tic-a1b2 /                                 # ...and un-file it again")
    add("arbite show tic-a1b2                                    # read it in full")
    add("arbite claim tic-a1b2 --agent claude.haiku.001          # take it")
    add('arbite note tic-a1b2 claude.haiku.001 "progress"        # ...do the work, log progress...')
    add('arbite block tic-a1b2 --reason "waiting on tic-c3d4"    # if stalled')
    add("arbite unblock tic-a1b2 --agent claude.haiku.001        # blocker cleared")
    add('arbite release tic-a1b2 --agent claude.haiku.001 --reason "wrong tier for me"')
    add('arbite shelve tic-a1b2 --reason "parked for later"      # if deprioritized')
    add('arbite unshelve tic-a1b2 --reason "back in scope"       # bring it back to open')
    add("arbite close tic-a1b2                                   # when done")
    add("arbite reopen tic-a1b2                                  # if it turns out not to be done")
    add("arbite sink info                                        # where do tickets live, and in what")
    add("arbite migrate --to sqlite                              # copy every ticket into another sink")
    add("")
    add("arbite file list --json                                 # discover workspace files (no lock)")
    add("arbite file claim src/app.py --ticket tic-a1b2 --attempt att-...   # own the whole file")
    add("arbite file read  src/app.py --ticket tic-a1b2 --attempt att-... --json  # re-read -> token")
    add("arbite file write src/app.py --ticket tic-a1b2 --attempt att-... --read-token R --input p.txt")
    add("arbite changes tic-a1b2 --json                          # what changed, and is it verifiable")
    add("```")
    add("")
    add(
        "`arbite bug|feature|request|memo|wish <message>` == `arbite raw <type> <message>`: an "
        "identical raw ticket from a shorter command. Any command takes `-h`/`--help`."
    )
    add("")

    # -- Shared directory --------------------------------------------------
    add("## Shared directory: the file proxy")
    add("")
    add(
        "When more than one agent (or an agent and a human) works in this checkout, do source "
        "discovery, reads and mutations through arbite instead of shell/editor writes: "
        "`arbite file list|search` to discover, `arbite file read PATH --ticket T --attempt A` to "
        "read, `arbite file claim PATH... --ticket T --attempt A` to own a whole file, then read "
        "again -- bytes read *before* you held the claim do not authorize a write -- and mutate "
        "with `arbite file write|edit|remove|rename`. Re-read after every claim, takeover and "
        "ticket boundary, and before each mutation: a token is consumed by the mutation it "
        "authorizes (even one that writes identical bytes), and a stale or already-consumed token "
        "is refused with `stale_read` and no bytes change. A binary file cannot be served as text; "
        "`arbite file read PATH ... --version-only` records its version and returns the token a "
        "replacement needs. Reading a file another attempt holds still returns "
        "the bytes, but with a `busy` owner and a non-writable receipt (`--fail-if-busy` refuses "
        "instead); claiming refused work returns structured `file_busy` naming the holder, and "
        "arbite never waits for or steals a claim. Shell is still fine for tests and builds."
    )
    add("")
    add(
        "Every `arbite file` command needs your active `--attempt` id. Get it from "
        "`arbite export --scope coordination --no-artifacts`: its `records.work_attempts` lists one "
        "entry per attempt -- take the one whose `ticket_id` is yours and whose `state` is "
        "`active`."
    )
    add("")
    add(
        "`arbite changes T [--attempt A] [--include-reads] --json` is the evidence view, and keeps "
        "them apart: mechanical operations (ordered receipts with before/after digests and "
        "verifiable artifact references), agent-authored ticket notes (prose, never used to derive "
        "the net change), and observed/unattributed drift (never assigned to the current agent). "
        "Claiming a ticket records a work attempt; `close`, `release`, `block` and `shelve` end it "
        "and release its file claims, while `reopen`/`unblock` mint a fresh attempt, so an old "
        "token is dead. `claim --force --reason <why>` is an explicit administrative takeover, "
        "never an inference from timestamps."
    )
    add("")
    add(
        "Arbite starts no runner, daemon, watcher or scheduler and never takes over stale work "
        "automatically: agents are started manually and may use different providers. A build, "
        "formatter or code generator that writes into managed source paths is unattributed drift "
        "-- keep such output in an ignored or out-of-tree directory. Mutation evidence and "
        "artifacts accumulate and are never garbage-collected yet, so budget disk and keep or "
        "export history deliberately. Finally, arbite enforces its own operations and reports "
        "observed drift, but a shell or editor can still write to the same directory and arbite "
        "cannot prove who made a direct filesystem change: runtime restriction is the caller's job, "
        "not a property of the store. When a file changes outside the proxy, a later read of it is "
        "non-writable (`claim_version_mismatch`) and an old token is refused (`stale_read`); the way "
        "forward is to `arbite file release` the path and `arbite file claim` it again, then read "
        "and write."
    )
    add("")

    # -- Conventions -------------------------------------------------------
    add("## Conventions (scripts and agent loops)")
    add("")
    add(
        "**Ticket ids.** A ticket id argument is an exact id or any unique wildcard (substring) "
        "match: 'f6' resolves to tic-f607, and an exact id always wins. Commands that modify a ticket "
        "treat an ambiguous match as an error listing the candidates rather than guessing; read-only "
        "commands (`show`, `deps`) take the first alphabetically instead."
    )
    add("")
    add(
        "**Exit codes**, so a shell loop can branch without matching on message text: 0 success with "
        "results; 1 error (bad arguments, ambiguous ticket id, refused claim, ...); 2 the query ran "
        "fine but matched nothing (e.g. no workable ticket right now); 3 `arbite doctor` found "
        "integrity problems."
    )
    add("")
    add("```")
    add("arbite list next --tier medium --claim claude.haiku.001 --json > ticket.json")
    add("case $? in")
    add("  0) ;;                       # got one, work it")
    add("  2) echo 'nothing ready'; exit 0 ;;")
    add("  *) echo 'arbite failed' >&2; exit 1 ;;")
    add("esac")
    add("```")
    add("")
    add(
        "**Parse JSON, not tables.** `--json` on `list`, `list next`, `list raw`, `fetch`, `show`, "
        "`search`, `deps`, `doctor`, `sink` and `delete` emits machine-readable output whose field "
        "names match the frontmatter; the human table format is not a stable interface. The `path` "
        "field is whatever the sink calls a ticket's location."
    )
    add("")
    add(
        "**Claim in one step.** `arbite list next --claim <agent_id>` selects the most urgent workable "
        "ticket *and* claims it in the same write. Prefer it to running `list next` then `claim`: "
        "between those two commands another agent can take the ticket you were just handed, and you "
        "would both work it. A plain `arbite claim` is likewise a compare-and-swap -- it fails if the "
        "ticket is already assigned to someone else unless you pass `--force`, and it refuses to "
        "write over a ticket that changed since it was read."
    )
    add("")
    add(
        "**Pull a batch with `--count N`.** `arbite list next --count 3` returns the 3 most urgent "
        "workable tickets instead of 1, so a dispatcher can fan work out to several agents in one "
        "query; adding `--claim <agent_id>` claims up to N of them. Each claim is individually "
        "compared-and-swapped, so if another agent races you for one the rest still succeed -- a "
        "short batch is a real result, and the count actually claimed is reported on stderr. "
        "`--count` also caps a plain `list`, `--topo` (rows) or `--tree` (top-level roots, never "
        "truncating a subtree)."
    )
    add("")
    add(
        "**Removing a ticket is not the same as finishing it.** `arbite close` moves a ticket out of "
        "the work queues and keeps it forever; `arbite delete <id> --force` destroys it, records a "
        "note saying so first, and is deliberately gated. Prefer `close`."
    )
    add("")

    # -- Triage ------------------------------------------------------------
    add("## Triage: raw tickets, wishes, filing, scaffolding")
    add("")
    add(
        f"A **raw** ticket (`arbite raw <{'|'.join(RAW_TYPE_CHOICES)}> <message>`) is a "
        f"deliberately unclassified quick capture: status `raw`, never returned by `arbite list "
        f"next`. It sets only `type` plus a placeholder title (`<type> (raw): Requires "
        f"Classification`), auto-groups the ticket under the `{CLASSIFICATION_EPIC}` epic (find "
        f"them with `arbite list next --epic {CLASSIFICATION_EPIC}`), and its body lists what "
        f"triage must fill in -- a real title, `tier`, `domain`, a real `epic`, `priority`, an "
        f"expanded description -- before it can be claimed or worked; a `memo` is a request to "
        f"update project notes/docs rather than change code, and a `request` is a request for a "
        f"change that is not necessarily a bug or a new feature (a tweak or lateral change). Raw "
        f"tickets exist so a thought isn't lost, not as work: classify them before picking them up."
    )
    add("")
    add(
        "`arbite list raw` prints the whole raw backlog as a running todo list -- grouped by type, one "
        "line per ticket (id + the request text it was captured from), oldest first -- until a "
        "classification run drains it."
    )
    add("")
    add(
        "**`arbite fetch [type]`** pulls the single oldest raw ticket (by `created`, optionally of one "
        "type) and prints it like `show`, with a `derived_note` at the top (a JSON field in `--json` "
        "mode, a leading block in text) telling you to classify it "
        "(title/tier/domain/epic/priority/description) and then either set `status` to `open` (if you "
        "are only triaging, so someone else can pick it up via `arbite list next`) or `arbite claim` "
        "it now (if you are working it yourself)."
    )
    add("")
    add(
        "A **wish** raw ticket (`arbite raw wish <message>`) is a wishlist item, not ordinary feature "
        "work: its body (and `fetch`'s `derived_note` for it) says to reclassify it as `feature`, "
        "with correct tags, an expanded description, an analysis of the request and a possible epic, "
        "then file it in the wishlist bucket -- never open or claim it as work."
    )
    add("")
    add(
        "A **request** raw ticket (`arbite request <message>` == `arbite raw request <message>`) is "
        "a request for a change that is not necessarily a bug or a new feature -- a tweak or lateral "
        "change to something that already exists (behaviour, UI, data or docs). Unlike a wish it is "
        "ordinary work once classified: keep the type as `request`, and open or claim it like any "
        "other raw ticket."
    )
    add("")
    add(
        "**`arbite move <id> <folder>`** files a ticket outside the status workflow: `/wishlist` puts "
        "it in the wishlist bucket, `/planning/ideas` in a nested one, and `/` returns it to its "
        "status location. It changes no field, so use the status commands (claim/block/close/...) for "
        "anything that should change state -- those un-file the ticket for you."
    )
    add("")
    add(
        "**Integrity.** `arbite doctor` reports what nothing else enforces, then exits 3 so it can "
        "gate CI or an agent's startup. The shared checks cover duplicate ids, invalid field values, "
        "dependency cycles, dangling and self dependencies, `in_progress` with no assignee, `blocked` "
        "with no reason, and closed-date mismatches. The sink adds its own -- for a file sink, "
        "frontmatter/folder drift (the folder wins), tickets left loose in the root, temp files "
        "stranded by an interrupted write, and closed tickets archived in the wrong "
        "month; for a database sink, an index that has drifted from a ticket body, orphaned index "
        "rows, an unexpected schema version and structural corruption. `--fix` repairs only the "
        "unambiguous cases and never guesses."
    )
    add("")

    # -- Commands ----------------------------------------------------------
    add("## Commands")
    add("")
    add(
        "Rendered from the installed version's own parsers, so it always matches the CLI: the usage "
        "line and flags of every command (argparse's longer descriptions are omitted -- `arbite <cmd> "
        "-h` prints them). Every command accepts `--sink <kind>`, and every status command updates "
        "`status`/`updated` together while `block`, `shelve`, `release`, `unblock`, `reopen` and "
        "`unshelve` also append an automatic timestamped note."
    )
    add("")
    add(
        "Command notes: `release` is how you hand back work you stop part-way (out of scope, out of "
        "context, or the wrong tier) -- it clears the assignee and any block reason so `list next` "
        "offers the ticket again; `unblock` clears `blocked_by` and returns the ticket to "
        "`in_progress` (`open` with `--open`), and is preferable to `arbite set status`, which would "
        "leave `blocked_by` populated and the ticket claiming to be stalled; `reopen` clears the "
        "closed date and any block reason and `unshelve` clears the assignee and any block reason; "
        "`set` takes PROPERTY VALUE pairs (any number per call, quote multi-word values, `''` clears "
        "a field), is type-aware (`tags`/`depends_on` comma-separated lists, `priority` an integer), "
        "cannot set the structural `id`, and re-files the ticket when `status` changes; `depend` adds "
        "a dependency (deduplicated), or with one argument clears them all; `search` matches a ticket "
        "if any selected field matches; `migrate` copies every ticket from one sink into another "
        "(the source is left alone); `delete` destroys a ticket and needs `--force`."
    )
    add("")
    add(f"`{_usage(parser)}` -- ticket sink CLI")
    add("")
    for sub_action in parser._actions:
        if isinstance(sub_action, argparse._SubParsersAction):
            for choice in getattr(sub_action, "_choices_actions", []):
                add(f"- `{choice.dest}` -- {_flat(choice.help)}")
            break
    add("")

    shorthands: list[str] = []
    for name, subparser in subparsers_by_name.items():
        if _is_shorthand(subparser):
            # Four near-identical blocks would say the same thing four times.
            shorthands.append(name)
            continue

        add(f"**`arbite {name}`** -- `{_usage(subparser)}`")
        bullets = []
        for action in _iter_actions(subparser):
            required = bool(action.option_strings and action.required)
            help_text = _shorten(action.help)
            if required and help_text.endswith(" (required)"):
                # Already flagged as required on this line.
                help_text = help_text[: -len(" (required)")]
            marker = " (required)" if required else ""
            bullets.append(f"- `{_action_label(action)}`{marker} -- {help_text}")
        if bullets:
            add("")
            lines.extend(bullets)
        add("")
    if shorthands:
        add(
            f"`arbite {'|'.join(shorthands)} <message>` == `arbite raw <type> <message>`: the same "
            "raw ticket of that type (see `arbite raw` above)."
        )
        add("")

    # Last line of defence: no escape code may reach the file, whatever the
    # running argparse version decided to colourise.
    return _ANSI_RE.sub("", "\n".join(lines)) + "\n"
