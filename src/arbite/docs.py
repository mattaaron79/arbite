"""Renders the agent guide `arbite init` writes: a short .arbite/AGENTS.md
quickstart and the full .arbite/REFERENCE.md.

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
Use arbite ticketing system for all tasks. Read `.arbite/AGENTS.md` (the short quickstart) before your first arbite command; `.arbite/REFERENCE.md` is the full reference -- read only the section you need. Create a ticket if required and claim the ticket before starting work.

# Agent Identity

When claiming a ticket, please use an identity format like: "claude.opus-5.001" where the company.model.instance is your best educated guess unless otherwise specified.
If orchestrating, let subagents know their identity and instance number.

## Shared directory: use arbite for file work
Claim and mutate source files through arbite -- not a shell or editor -- so a competing agent cannot silently overwrite your work:
```bash
arbite claim T --agent ME                                           # prints your attempt id A
arbite file claim PATH [PATH ...] --ticket T --attempt A            # own every file of the task at once
arbite file read PATH --ticket T --attempt A --version-only --json  # read token R, no content served
arbite file edit PATH --ticket T --attempt A --read-token R --edits -    # stdin: [{"old": "...", "new": "..."}]
arbite file write PATH --ticket T --attempt A [--read-token R] --input - # new (claimed-absent) file: no token
```
Read files with your own tools -- reading changes nothing; only mutations must go through arbite. A read token proves the file is unchanged since you took it, not that you read it. Take a fresh one after every claim and before each mutation: tokens are single-use, and a stale or consumed token is refused with `stale_read` and no bytes change. Lost the attempt id? `arbite show T --json` reports `active_attempt.id`.
There is no runner, daemon, watcher or scheduler, and stale work is never taken over automatically -- agents are started manually and may use different providers; only `arbite claim --force --reason <why>` moves live work. Keep build/test output outside managed source paths: a generated file written into the source tree is unattributed drift. Mutation evidence is never garbage-collected. Arbite cannot prove who made a direct filesystem change -- an external editor or shell can still bypass the proxy.

## Sole command: "Work Next|All <epic>"

If your sole command is "Work Next" or "Work All", you can use the following commands to find the next arbite ticket(s):
```bash
arbite list next [--epic <epic>]   # Show next workable ticket
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

## Arbite feedback
Before closing a ticket, add one note that starts with `arbite feedback:` saying what helped and what got in the way when using arbite for that work (commands, refusals, extra calls, missing features, confusing docs). Keep it to a few lines, and say "nothing notable" for a side with nothing to report. The owner compiles these with `arbite search "arbite feedback:"`.

## Git
By default and unless otherwise specified, check into main/master after closing a ticket.

Do not modify this file without explicit permission.

## Comment Protocol

Do not fill the codebase with comments containing history, musings, or overly wordy explanations. Comments should be maximally useful and concise. Put long explanations and related context in Arbite tickets, and refer to the relevant ticket from a code comment when needed.
<!-- END ARBITE INSTRUCTIONS -->"""


def install_instructions(path: Path) -> str:
    """Put the current arbite instructions block into the agent doc at `path`.

    Returns what it did, so `arbite init` can report it:

    - 'created'  -- the file did not exist; it is written with the block alone;
    - 'prepended' -- the file existed without the block; the block is prepended and
      the existing contents are preserved below it;
    - 'updated'  -- the text between the markers differed from the current block and
      was replaced; everything outside the markers is preserved;
    - 'present'  -- the file already carries the current block, so it is untouched;
    - 'unmatched' -- only one marker, or END before BEGIN: left alone rather than
      guessing where the block ends.

    Idempotent by demarkation: re-running `arbite init` never stacks a second copy."""
    if not path.exists():
        path.write_text(ARBITE_INSTRUCTIONS_BLOCK + "\n", encoding="utf-8")
        return "created"
    existing = path.read_text(encoding="utf-8")
    begin = existing.find(ARBITE_INSTRUCTIONS_BEGIN)
    end = existing.find(ARBITE_INSTRUCTIONS_END)
    if begin == -1 and end == -1:
        path.write_text(
            ARBITE_INSTRUCTIONS_BLOCK + "\n\n" + existing.lstrip("\n"), encoding="utf-8"
        )
        return "prepended"
    if begin == -1 or end == -1 or end < begin:
        return "unmatched"
    end += len(ARBITE_INSTRUCTIONS_END)
    if existing[begin:end] == ARBITE_INSTRUCTIONS_BLOCK:
        return "present"
    path.write_text(existing[:begin] + ARBITE_INSTRUCTIONS_BLOCK + existing[end:], encoding="utf-8")
    return "updated"

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

    add("# arbite -- full reference")
    add("")
    add(
        f"_Auto-generated by `arbite init` (arbite {__version__}) on {date.today().isoformat()}: "
        "every `arbite init` run here rewrites it, so edit the package's docs template rather than "
        "this file. Start with the short `.arbite/AGENTS.md` quickstart; this file is the full "
        "reference, meant to be read one section at a time._"
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
        "`last_checkin` is the worker's own declaration, never verified liveness. Declared "
        "`--capacity` is the limit on *concurrent active attempts* for that worker id, enforced "
        "when work is acquired (a `--count N` batch or two concurrent claims cannot overfill it; "
        "a short batch is a correct result) -- only active attempts count, so a reservation or a "
        "later continuity-package member does not consume capacity, and a profile change affects "
        "future acquisition only: it never revokes running work. `arbite worker check <id> "
        "[--ticket T] [--require-capability CAP] [--local-only] [--max-cost N --max-cost-unit U]` "
        "explains eligibility with stable reason codes and writes nothing; an unknown value fails "
        "an explicit constraint. Offers, continuity packages and the job board are below."
    )
    add("")

    add("## Reservations (coordinators)")
    add("")
    add(
        "A coordinator can hold a set of tickets with `arbite reserve create T1 T2 ... --agent "
        "<coordinator>` (or `--epic E`, which snapshots the epic's non-closed tickets once; "
        "tickets added to the epic later are not included until `arbite reserve add`). While the "
        "reservation is active only its owner may acquire a member: `claim`, `list next --claim`, "
        "`--adopt`, `--force` and `set status in_progress` / `set assignee` refuse everyone else "
        "with `ticket_reserved`, and plain `list next` leaves reserved tickets out (noted on "
        "stderr). If you are refused, pick other work -- do not retry the same ticket. A "
        "reservation never starts work: it creates no attempt and leaves the ticket's status "
        "alone. Create/add are all-or-nothing (`reservation_conflict`, `details.conflicts[]` "
        "per ticket: `closed`, `not_found`, `already_reserved` -- no overlap or nesting -- "
        "`active_attempt_elsewhere`, `assigned_elsewhere`). `reserve add|remove|release` need "
        "`--agent <owner>` (or `--force --reason` for an administrative change) and accept "
        "`--expect-revision N`. `remove`/`release` refuse while an affected member has an active "
        "attempt (`active_attempts`) unless `--interrupt --reason` ends those attempts and "
        "returns the tickets to open; releasing a quiescent reservation returns its open "
        "members to ad-hoc availability. `reserve show|list [--state] [--owner] [--ticket]` "
        "report members with their current status and active attempt, and `arbite reserve "
        "progress RESERVATION [--owner OWNER] [--state active|released|all]` is the read-only "
        "observation view: every member classified `completed`, `blocked`, `active`, "
        "`dependency_waiting`, `ready` or `unavailable`, with its current worker, its latest "
        "recorded activity and the same readiness verdict `board` and `claim` use. Recorded "
        "activity is an observation timestamp, never a liveness guarantee. All take `--json`."
    )
    add("")

    add("## Offers and direct assignments")
    add("")
    add(
        "A publisher (for a reserved ticket: the reservation owner, which keeps its "
        "reservation) makes an open, unassigned ticket available with `arbite offer publish T "
        "--agent <owner>` (any eligible worker) or `arbite offer assign T --worker W --agent "
        "<owner>` (only the named worker(s)). Requirements -- `--min-tier`, "
        "`--require-capability`, `--local-only`, `--max-cost N --max-cost-unit U` -- are "
        "enforced at acquisition and an unknown worker value fails them; `--prefer-local`, "
        "`--prefer-low-cost` and `--prefer-worker` are hints only and never decide who wins: "
        "the first eligible claimant does. Find work with `arbite offer list --worker <you>` "
        "and accept it with `arbite offer claim OFFER --agent <you>`; plain `claim` and `list "
        "next --claim` use the same offer. Acceptance starts your attempt and marks the offer "
        "`accepted` in one step, so two workers never both get it -- a loser sees "
        "`offer_conflict` (`details.reason` `not_published`) and should choose other work. "
        "While an offer is published nobody it does not admit may acquire the ticket -- not "
        "the owner, not with `--force` (`worker_ineligible`, reason `worker_not_allowed` for "
        "someone else's assignment), and `set status in_progress` / `set assignee` refuse "
        "with `ticket_offered`. `arbite offer withdraw OFFER --agent <publisher>` stops future "
        "acceptance; an accepted offer refuses withdrawal (`accepted`) unless `--interrupt "
        "--reason` interrupts the worker's attempt. The offer follows its ticket: `close` "
        "completes it, and a release, takeover or unshelve that leaves the worker without the "
        "ticket cancels it (the owner may "
        "publish again). Releasing or removing reservation members withdraws their published "
        "offers. `offer show OFFER [--worker W]` explains eligibility. All take `--json`."
    )
    add("")

    # -- Continuity packages -----------------------------------------------
    add("## Continuity packages")
    add("")
    add(
        "A package is the contract 'A then B by the same worker'. "
        "`arbite package create T1 T2 [...] --agent <coordinator>` bundles two or more open "
        "tickets in order; package order is a scheduling edge, so only the current member (the "
        "first one not closed) can be acquired, and only the current member is ever offered by "
        "`list next` -- a bound package's only to its bound worker. Creation is all-or-nothing "
        "(`package_conflict`): a member that is closed, assigned, active, offered, reserved by "
        "someone else or already in another package is refused, and package order plus ticket "
        "dependencies may not form a cycle (reason `cycle`); dependencies outside the package "
        "are reported as external prerequisites. Acquiring the first member -- directly with "
        "`arbite claim`, or by accepting `arbite offer publish/assign --package PKG` -- binds "
        "every remaining member to that worker id in the same transaction. Each member is its "
        "own attempt, file claims are released when a member closes, and the next member needs "
        "fresh reads; same worker id is continuity identity, not the same model session. A "
        "blocked member stops the package until it is explicitly resolved, nothing advances "
        "merely because execution stopped, and nothing times out. Use `arbite package note PKG "
        "TEXT --agent <worker>` to leave durable continuity notes for a resumed session, and "
        "`arbite package handoff PKG --reason TEXT (--to W | --release) [--interrupt] [--note "
        "TEXT]` to rebind or release the *remaining* members (completed members stay completed; "
        "a rebind also checks the package offer's hard requirements against the new worker, and "
        "`--force` skips that). `arbite package show|list [--state] [--ticket] [--worker]` "
        "report progress, the current member, external prerequisites and the next action. All "
        "take `--json`."
    )
    add("")

    # -- Job board ---------------------------------------------------------
    add("## Job board and readiness")
    add("")
    add(
        "`arbite board --worker W [--epic E] [--tier T] [--count N] [--json]` explains, for one "
        "worker, what is ready now and why everything else is not; it is a query and claims "
        "nothing. Every candidate ticket in the status workflow (optionally narrowed to one "
        "`--epic`) is reported `ready` or with structured `reasons`, each carrying a stable "
        "`code` and the `axis` it belongs to: `status` and `classification` "
        "(`ticket_not_open`, `ticket_unclassified`), `dependency` (`dependencies_unmet`), "
        "`attempt` (`active_attempt`), `reservation` (`reserved_elsewhere`, or "
        "`worker_identity_required` when no worker was named), `offer` (`offer_ineligible`, with "
        "the nested eligibility reasons such as `worker_not_allowed`), `continuity` "
        "(`package_order`, `package_bound_elsewhere`), `worker` (`worker_ineligible`: tier, "
        "capability, locality, cost ceiling) and `capacity` (`capacity_exhausted`). The output "
        "also reports the worker's declared capacity, the active attempts counted against it and "
        "the free slots, plus `suggestions` -- the compatible ready work in the order `list next` "
        "would offer it. Readiness is derived from current state every time, and the same "
        "evaluator backs plain `list next` and acquisition, so a board cannot disagree with what "
        "`claim` accepts; a board query still cannot promise that a ticket it reports ready is "
        "free when you act on it, because `claim`/`list next --claim` re-check every condition "
        "under the operation lock. Local/low-cost offer preferences (`--prefer-local`, "
        "`--prefer-low-cost`, `--prefer-worker`) are reported as advisory hints and never decide "
        "pickup -- the first eligible claimant wins -- so an owner who wants a preference "
        "*enforced* uses a hard constraint (`--require-capability`, `--local-only`, `--max-cost`, "
        "`--min-tier`, `--allowed-worker`) or a direct `arbite offer assign`. The command exits 2 "
        "when nothing is ready for that worker."
    )
    add("")

    # -- Events and progress ------------------------------------------------
    add("## Events and progress (external callers)")
    add("")
    add(
        "`arbite events --after CURSOR --limit N [--category C] [--kind K] [--subject ID] "
        "[--json]` reads the durable coordination log once, in cursor order. `--after` takes "
        "either `0` (the beginning) or the `next_cursor` token a previous query returned; a "
        "token is store-bound (`<cursor_namespace>#<cursor>`), so a bare integer fails "
        "`invalid_cursor` and another store's token fails `cursor_foreign_store` -- enriched "
        "with an import mapping when that store's events were imported here -- and neither "
        "ever restarts silently from zero or reads the wrong store. Filters are applied to "
        "the whole ordered stream before `--limit`, and the returned token resumes after the "
        "last *matching* event, so a filtered stream neither skips nor re-reads. Every event "
        "carries a stable id: a retry or an overlapping page may deliver it again, so a "
        "consumer deduplicates by id. Each page reports `has_more`, `next_cursor`, the "
        "`cursor_namespace` and the filters used, and the command exits 2 when nothing "
        "matched (the token is still printed, so a loop resumes without ambiguity). "
        "Categories are `lifecycle`, `operation`, `claim`, `read`, `worker`, `reservation`, "
        "`offer` and `package`; file-read observations are the separate `read` category so "
        "lifecycle traffic is not buried. It is a one-shot query: there is no watcher, "
        "subscription server, polling loop, notification delivery or agent wakeup, so a "
        "'busy' or 'nothing ready' answer is an exit code (2) rather than a model process "
        "that has to stay alive, and a caller that exits and returns later resumes from the "
        "token it stored. Cursors are store-local, so a token from an export/import bundle "
        "is not a valid `--after` value here (the refusal names the destination cursor it "
        "mapped to); the file sink reconciles an interrupted commit before reporting its "
        "events, so no half-visible event is served."
    )
    add("")

    # -- Transfers, export and the doctor ---------------------------------
    add("## Transfers, export and the doctor")
    add("")
    add(
        "Worker profiles, reservations, offers and packages travel with `arbite export` and "
        "with `arbite migrate`/`arbite rebind` -- the migration's default `--coordination` "
        "moves attempts, claims, receipts, intents, profiles, reservations, offers, packages, "
        "events and artifact metadata together and rebinds the workspace. A transfer refuses "
        "while job-board work is in flight: `migrate`/`rebind` report `store_not_quiescent` "
        "and name the active attempt(s), claim(s), intent(s), operation(s), reservation(s), "
        "published offer(s) and live package(s) that block it, write nothing, and offer the "
        "way forward -- end the work and reconcile it, or pass an explicit administrative "
        "override `--force --reason TEXT`, which is reported on stderr (and as "
        "`unquiescent_override` in `rebind --json`) rather than applied silently. Released "
        "reservations, finished offers and completed or released packages do not block. The "
        "export bundle verifies its own job-board references and overlaps and fingerprints "
        "the job-board records, so a lossy transfer is refused instead of being reported as "
        "verified."
    )
    add("")
    add(
        "`arbite doctor` adds report-only job-board checks when the store actually carries "
        "coordination state: `coordination_dangling_offer`, `_package` and `_reservation` "
        "(a reference to a missing record or ticket), their `_overlapping_` variants (one "
        "ticket inside two live reservations, offers or packages), "
        "`coordination_package_reservation_mismatch` (a package and its recorded reservation "
        "disagreeing about members), `coordination_continuity_binding_invalid` (a live package "
        "with no bound worker, or bound to a disabled profile), "
        "`coordination_attempt_mismatch` (an active attempt whose ticket is missing, closed or "
        "shelved, an attempt disagreeing with the ticket's assignee, or an `in_progress` "
        "ticket with no attempt in a store that uses coordination), "
        "`coordination_capacity_exceeded` (active attempts above a declared capacity where at "
        "least one started *after* the profile changed -- a capacity merely lowered below "
        "running work is legal and never reported) and `coordination_namespace_mismatch` (an "
        "unreadable event-cursor registry). They are findings only: `--fix` repairs none of "
        "them, and `arbite doctor` exits 3 while any remains. Profiles have no delete at all "
        "-- `worker disable` stops future acquisition and keeps every attempt and event -- and "
        "a reservation, offer or package is closed by state "
        "(`released`/`withdrawn`/`completed`), never removed."
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
    add("")
    add("arbite worker register claude.opus.001 --tier high --provider anthropic   # optional profile")
    add("arbite board --worker claude.opus.001 --json         # ready now, and why not (exit 2 = nothing)")
    add("arbite reserve create tic-a1b2 tic-c3d4 --agent coord.1    # hold a set; starts nothing")
    add("arbite offer assign tic-a1b2 --worker claude.opus.001 --agent coord.1   # or: offer publish T")
    add("arbite package create tic-c3d4 tic-d4e5 --agent coord.1    # 'A then B by the same worker'")
    add("arbite offer claim off-... --agent claude.haiku.001   # accept an offer; starts your attempt")
    add("arbite reserve progress rsv-... --json                # what the reservation's members do now")
    add("arbite events --after 0 --limit 50 --json             # ordered, cursor-resumable log")
    add("arbite migrate --to sqlite                            # copy every ticket into another sink")
    add("")
    add("arbite file list --json                                 # discover workspace files (no lock)")
    add("arbite file claim src/app.py --ticket tic-a1b2 --attempt att-...   # own the whole file")
    add("arbite file read  src/app.py --ticket tic-a1b2 --attempt att-... --version-only --json  # token")
    add("arbite file edit  src/app.py --ticket tic-a1b2 --attempt att-... --read-token R --edits -")
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
        "again -- a read token taken *before* you held the claim does not authorize a write -- "
        "and mutate with `arbite file write|edit|remove|rename`. A read token is a freshness "
        "receipt (the file was at version X under your claim), not proof that its content was "
        "read, so `--version-only` or a `--lines START:END` read is enough to obtain one. "
        "Re-read after every claim, takeover and ticket boundary, and before each mutation: a "
        "token is consumed by the mutation it "
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
        "Every `arbite file` command needs your active `--attempt` id. `claim`, `list next "
        "--claim`, `offer claim` and `package claim` print it (`attempt_id` in `--json`), and "
        "`arbite show T --json` reports it as `active_attempt.id`. It is deliberately never "
        "inferred: after a takeover, the old id is what makes the previous worker's file commands "
        "fail instead of acting under the new attempt."
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
        "rows, an unexpected schema version and structural corruption. A store that carries "
        "coordination state also gets the job-board checks (dangling, overlapping or mismatched "
        "reservation/offer/package references, an invalid continuity binding, an attempt "
        "disagreeing with its ticket, impossible capacity arithmetic, an unreadable event-cursor "
        "registry); they are findings only, and `--fix` repairs none of them. `--fix` repairs "
        "only the unambiguous cases and never guesses."
    )
    add("")

    # -- Deferrals ---------------------------------------------------------
    add("## What is deliberately not here (job board)")
    add("")
    add(
        "The job board coordinates workers that already exist and are started by hand; it "
        "never starts one, and every participant invokes the same short-lived CLI. Explicitly "
        "out of scope, and not implemented in any dormant form: provider API integration or "
        "provider adapters, agent spawning, a daemon, watcher, scheduler, background "
        "subscription or notification delivery, agent wakeups, heartbeat expiration or "
        "automatic stale-work recovery, an auction or dynamic price lookup, a model price "
        "catalog or currency conversion, agent authentication (worker ids and "
        "provider/model/runtime labels are attribution, not identity), cross-machine or global "
        "capacity arbitration, a central database, a project factory or a dashboard, and "
        "artifact garbage collection. Capacity is per worker id inside this one local store "
        "and is never claimed as a global pool; no trustworthy occupancy is claimed across "
        "independent project stores. Nothing times out and nothing is reassigned because "
        "execution stopped: only an explicit `claim --force --reason`, an explicit reservation "
        "or offer change, or an explicit `package handoff --reason` moves live work. The "
        "records carry the ids a future integration needs (worker id, profile revision, "
        "offer/reservation/package/attempt ids, event cursors) without shipping dormant "
        "scheduling machinery."
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
        "(the source is left alone) and, like `rebind`, refuses while job-board work is live "
        "unless you pass `--force --reason <why>`; `delete` destroys a ticket and needs "
        "`--force`."
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


# ---------------------------------------------------------------------------
# The quickstart: .arbite/AGENTS.md
# ---------------------------------------------------------------------------

#: Upper bound on the rendered quickstart, enforced by the test suite: the file is
#: read at the start of every task, so growth belongs in REFERENCE.md instead.
QUICKSTART_MAX_CHARS = 9000

#: Where to look next: (topic, REFERENCE.md section heading).
_REFERENCE_INDEX = (
    ("ticket fields, tier vs priority vs domain", "Ticket fields (YAML frontmatter)"),
    ("resuming work, agent scratchpads", "Agent identity and resuming work"),
    ("the file proxy in depth: busy files, drift, evidence", "Shared directory: the file proxy"),
    ("batches, exit codes, race-free claiming", "Conventions (scripts and agent loops)"),
    ("raw tickets, wishes, classification, doctor", "Triage: raw tickets, wishes, filing, scaffolding"),
    ("worker profiles, capacity", "Worker profiles (optional)"),
    ("why a ticket is not ready for you", "Job board and readiness"),
    ("coordinating other agents", "Reservations (coordinators)"),
    ("offers and direct assignments", "Offers and direct assignments"),
    ("'A then B by the same worker'", "Continuity packages"),
    ("event log for external callers", "Events and progress (external callers)"),
    ("moving stores, export, integrity checks", "Transfers, export and the doctor"),
    ("every command's flags", "Commands"),
)


def render_quickstart(active_info=None, stale_info=None) -> str:
    """Render the short guide an agent reads first; REFERENCE.md holds the rest.

    Sink-aware in the two places a wrong statement would mislead an agent: how
    status is stored, and a second store that nothing selects."""
    primary = active_info if active_info is not None else stale_info
    status_is_location = bool(getattr(primary, "status_is_location", False))
    mismatch = active_info is not None and stale_info is not None

    lines: list[str] = []
    add = lines.append
    add("# arbite -- quickstart")
    add("")
    add(
        f"_Auto-generated by `arbite init` (arbite {__version__}) on {date.today().isoformat()}; "
        "edit the package's docs template, not this file. This is the short guide. The full "
        "reference is `.arbite/REFERENCE.md`: read only the section you need (index at the "
        "bottom). `arbite <command> -h` prints any command's flags._"
    )
    add("")
    if mismatch:
        add(
            f"> **Warning: this project holds a second ticket store that nothing selects** "
            f"({getattr(stale_info, 'ticket_count', 0)} ticket(s) in a "
            f"`{getattr(stale_info, 'kind', None)}` store at `{getattr(stale_info, 'root', None)}`). "
            "Plain commands read the other one. **The wrong store looks exactly like an empty "
            "one**: if a command reports no tickets, run `arbite sink info --json` and check its "
            "`kind` field before concluding the queue is empty. See REFERENCE.md, 'Where tickets "
            "live (the sink)'."
        )
        add("")

    add("## Work a ticket")
    add("")
    add(
        "Your id is `company.model.instance` (e.g. `claude.opus-5.001`). Claim only tickets at or "
        f"below your capability tier ({TIER_VALUES}); `priority` is urgency, lower first. "
        + (
            "A ticket's folder is its status and the source of truth. "
            if status_is_location
            else "`status` is a field and the source of truth. "
        )
        + "Your memory is a hint: `arbite list --assignee <you>` shows what you really hold."
    )
    add("")
    add("```bash")
    add("arbite list next --tier <tier> --claim <you> --json  # pick + claim in one race-free step")
    add("arbite claim tic-a1b2 --agent <you>                  # or claim a named ticket")
    add("arbite show tic-a1b2 --json                          # the ticket; active_attempt.id = --attempt")
    add('arbite note tic-a1b2 <you> "what changed and why"    # progress; never edit the ticket body')
    add("arbite close tic-a1b2 --agent <you>                  # done")
    add('arbite release tic-a1b2 --agent <you> --reason "wrong tier"   # stop part-way; back to open')
    add('arbite block tic-a1b2 --agent <you> --reason "waiting on tic-c3d4"')
    add("arbite create --title T --type bug --tier medium --domain io [--epic E] [--priority 5]")
    add('arbite raw feature "an idea to classify later"       # quick capture, not workable yet')
    add("```")
    add("")
    add(
        "Every acquisition prints your **attempt id** (`attempt att-...`, or `attempt_id` in "
        "`--json`); file commands need it. Lost it? `arbite show T --json` -> `active_attempt.id`. "
        "`close`, `release`, `block` and `shelve` end the attempt and release its file claims."
    )
    add("")

    add("## Edit files through arbite")
    add("")
    add(
        "In a shared checkout, change source files through the proxy, not a shell or editor, so "
        "no agent silently overwrites another's work. A **read token** is a freshness receipt "
        "(\"the file was at version X\"), not proof you read it. Read code with your own tools "
        "(reading changes nothing), and take the token with `--version-only` just before you "
        "change the file."
    )
    add("")
    add("```bash")
    add("T=tic-a1b2 A=att-...   # A: the attempt id your claim printed")
    add("arbite file claim src/a.py src/b.py --ticket $T --attempt $A   # every file of the task, once")
    add("arbite file read src/a.py --ticket $T --attempt $A --version-only --json   # -> data.read_token")
    add("arbite file edit src/a.py --ticket $T --attempt $A --read-token R --edits - <<'EOF'")
    add('[{"old": "exact text, unique in the file", "new": "replacement"},')
    add(' {"old": "a second unique anchor", "new": "its replacement"}]')
    add("EOF")
    add("arbite file write src/new.py --ticket $T --attempt $A --input -   # new file (claimed absent): no token")
    add("```")
    add("")
    add(
        "- One token per mutation: re-read (version-only is enough) before the next change to the "
        "same file. Put all of a file's changes in one `--edits` batch.\n"
        "- A batch is all-or-nothing: an `old` that is missing or not unique refuses the whole "
        "batch, nothing changes, and the token stays usable.\n"
        "- `stale_read`: the token is stale or used; read again. `claim_version_mismatch`: the "
        "file changed outside arbite; `arbite file release` it, claim it again, then read. "
        "`file_busy`: another attempt holds it; pick other work, never wait or steal.\n"
        "- A whole-file `write` replacing an existing file needs a token too; `remove` and "
        "`rename` also exist (`arbite file -h`).\n"
        "- Shell is fine for tests and builds, but keep their output out of source paths "
        "(it shows up as unattributed drift). `arbite changes T --json` shows what a ticket "
        "changed."
    )
    add("")

    add("## When arbite says no")
    add("")
    add(
        "- Exit codes: `0` ok, `1` error or refusal, `2` ran fine but nothing matched (e.g. no "
        "workable ticket), `3` `arbite doctor` found problems. Parse `--json`, not tables.\n"
        "- A claim refused as reserved, offered to someone else, out of package order or above "
        "your tier will not succeed on retry: pick other work. Over your declared capacity, "
        "finish or release something first. "
        "`arbite board --worker <you>` explains every ticket's status for you.\n"
        "- Nothing times out and nothing reassigns stale work: only "
        "`arbite claim T --agent <you> --force --reason <why>` takes over a live attempt.\n"
        "- Ticket ids accept any unique substring (`f6` -> `tic-f607`)."
    )
    add("")

    add("## Where to look next (`.arbite/REFERENCE.md`)")
    add("")
    for topic, heading in _REFERENCE_INDEX:
        add(f"- {topic}: '{heading}'")
    add("")
    return "\n".join(lines)
