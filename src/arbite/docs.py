"""Renders the two agent-facing docs `arbite init` writes into the arbite directory.

`.arbite/AGENTS.md` is the *guide*: what the workflow is, the rules an agent must act
on, the field vocabulary, and a one-line index of every command. `.arbite/WORKSPACE.md`
is the *reference* for the file proxy and the workspace commands -- their flags, the
honest limits and where the evidence lives. Splitting them is what keeps the guide
small, because it is read into an agent's context at the start of every task: it
carries no per-flag reference at all (`arbite <cmd> -h` prints those), and the detail
only a proxy user needs is one pointer away in a file that only that work reads.

Both documents are rendered from the installed argparse parsers and the schema
constants, so neither can drift from the CLI: the command index and the reference
blocks come from the parsers themselves, and the prose interpolates the same
vocabulary the CLI validates against (it previously advertised a `frontier` tier that
`create` and `list --tier` both rejected).

Both are also *sink-aware*: the files are read by agents that will act on what they
say, so they may only claim what is true of the store in use. With a file sink "a
ticket's folder is the source of truth" is the central rule and belongs front and
centre; with a database sink that sentence would be a lie, so the status/location
prose switches on the sink's own capabilities instead of being hard-coded. The same
rule governs the file-proxy sections: they are rendered only for a sink kind
`open_coordination_store` accepts (asked, not assumed), they state the honest limits --
a shell can bypass the proxy, observation is not exclusivity, nothing recovers work
automatically, and evidence is never pruned -- and a sink with no coordination backend
gets one paragraph saying so instead.

Written terse on purpose: facts-per-token rather than prose. Boilerplate that would
otherwise repeat once per command is stated once and cross-referenced, and argparse's
longer per-command descriptions are dropped because the prose sections already carry
what they say -- `arbite <cmd> -h` still prints them in full.

TICKET_ID_HELP / TICKET_ID_HELP_READONLY / JSON_HELP / MESSAGE_HELP live here
(cli.py imports them) so the renderer can collapse the copies of that boilerplate
into a single cross-reference without the two copies drifting apart.
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

Use your session to classify all tickets with `arbite promote <id> --title ... --tier ... --domain ...` (adding `--description`/`--epic`/`--priority`/`--tags` as you learn more), looking deeper into the requirements, adding notes, etc until all raw tickets are classified. Add `--agent <your-id>` to claim one you are going to work yourself; a wish is reclassified and filed in the wishlist bucket by the same command instead of being opened.

## Workflow: claim -> in_progress -> submit -> review -> accept

```bash
arbite claim <id> --agent <your-id>        # take it: status -> in_progress
arbite note <id> <your-id> "what changed"  # log progress as you go
arbite submit <id>                         # finish: status -> review, assignee kept
arbite accept <id> --agent <reviewer-id>   # the reviewer closes it, credited to them
```

A reviewer who sends work back uses `arbite reopen <id> --reason "<why>"` -- the reason is required, because it is the only record of what the author must fix. With the file sink a ticket's status is also the folder it sits in (`open/`, `in_progress/`, `review/`, `blocked/`, `shelved/`, `closed/YYYY-MM/`), while `wishlist/` and `plans/` are buckets rather than statuses and `raw/processed/` holds frozen snapshots of promoted captures. A project can set `review: false` in `.arbite/project.yaml`, in which case `arbite submit` closes the ticket directly instead of parking it in review.

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
# CLI actually accepts. One line per field: the guide is read on every task, so a
# field earns its place by saying what to do with it, not by explaining itself.
FIELD_NOTES = {
    "id": "unique ticket id, e.g. tic-a1b2 -- minted by `arbite create`, never set by hand",
    "title": "short human-readable summary",
    "status": f"{' | '.join(STATUSES)} -- state in the workflow; 'raw' = unclassified, never "
    "offered by `list next`, and a file sink keeps this in sync with the ticket's folder",
    "type": f"{' | '.join(TYPES)} -- 'memo' = update project notes/docs rather than code; "
    "'request' = a tweak or lateral change, ordinary work once classified; a wish is "
    "reclassified to 'feature' and filed, never worked",
    "tier": f"{TIER_VALUES} -- **agent capability tier** needed to work it, ascending: your "
    "harness tells you yours, or self-assess from your model class (the company.model prefix "
    "of your id, e.g. claude.haiku sits below claude.opus). Claim only at or below your tier",
    "domain": "what kind of agent/tool this needs, e.g. mesh, image_gen, audio_gen, ui, io -- "
    "drives routing",
    "epic": "the larger initiative this belongs to, e.g. mesh-pipeline -- a freeform filtering "
    "label (`list [next] --epic`), not a dependency or a ticket id",
    "priority": "numeric urgency, lower = more urgent (1 is highest; unset sorts last) -- orders "
    "which workable ticket `list next` offers",
    "tags": "freeform list, for human/codebase-area search -- distinct from domain",
    "assignee": "agent id working the ticket, e.g. claude.haiku.001, or null if unclaimed",
    "depends_on": "ticket ids that must close first (structural)",
    "references": "plan documents under the arbite root's `plans/` bucket, root-relative -- e.g. "
    "'plans/review-workflow.md' (== .arbite/plans/review-workflow.md), *not* a repo-root path; "
    "the file need not exist yet, and the field is omitted entirely when empty",
    "blocked_by": "freeform reason or a ticket id -- why it is stalled; only meaningful when "
    "status: blocked",
    "created": "creation timestamp (YYYY-MM-DDTHH:MM:SS); a bare YYYY-MM-DD also validates",
    "updated": "timestamp of the most recent change",
    "closed": "close timestamp, null until closed",
}

# ---------------------------------------------------------------------------
# Compaction helpers
# ---------------------------------------------------------------------------

# Boilerplate that would otherwise appear once per command: each entry is
# (verbatim argparse help text, short cross-reference to render instead). The JSON
# one is special-cased by `_shorten`, because the exit-code table it points at lives
# in the guide while the reference that uses it may be WORKSPACE.md.
_JSON_STUB = "JSON output (see Conventions)"
_SUBSTITUTIONS = (
    (TICKET_ID_HELP, "see 'Ticket ids' under Conventions"),
    (TICKET_ID_HELP_READONLY, "see 'Ticket ids' under Conventions"),
    (JSON_HELP, _JSON_STUB),
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

#: The commands whose per-flag reference lives in WORKSPACE.md rather than in the
#: always-read guide: the ones that act on files through the proxy, plus the
#: coordination records around them. `arbite init` renders both documents from the
#: same parsers, so the split cannot drift from the CLI.
WORKSPACE_COMMANDS = (
    "file",
    "scratch",
    "receipt",
    "changes",
    "cmd",
    "events",
    "workspace",
    "attempt",
)


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


def _shorten(text: str, json_stub: str = _JSON_STUB) -> str:
    """Collapse and cross-reference the boilerplate repeated across commands."""
    text = _flat(text)
    for needle, replacement in _SUBSTITUTIONS:
        text = text.replace(needle, json_stub if needle is JSON_HELP else replacement)
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


def _nested_choices(subparser) -> list:
    """`(name, parser)` for a command group's own sub-commands, in the order they
    were registered -- `ref` yields `add`/`rm`/`list`. Empty for an ordinary
    command, so the renderer can document a nested group's sub-commands instead of
    collapsing them into one opaque `{add,rm,list}` usage line."""
    for action in subparser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices.items())
    return []


def _usage(subparser) -> str:
    text = _flat(subparser.format_usage())
    return text[len("usage: "):] if text.startswith("usage: ") else text


def _stamp(add, follow_up: str) -> None:
    """Emit the header both documents carry, then the blank line after it.

    `follow_up` is what the file says about *being reached*: the guide must warn
    that nothing discovers it automatically and point at WORKSPACE.md, while
    WORKSPACE.md says it is reached from the guide. Both carry the same version and
    date stamp, and both start with the same prefix -- a test strips that line to
    check that re-running `init` rewrites the file byte-identically."""
    add(
        f"_Auto-generated by `arbite init` (arbite {__version__}) on {date.today().isoformat()}"
        ": every `arbite init` run here rewrites it, so edit the package's docs template "
        f"(`docs.py`) rather than this file. {follow_up}_"
    )
    add("")


def _command_index_lines(parser) -> list:
    """One line per command: its name and the parser's own one-line help.

    Rendered from the parsers rather than hand-kept, so a command added to the CLI
    appears here without an edit -- and the guide can only ever name commands that
    exist. The help strings are already one line each (that is what the CLI passes
    to argparse for the index), which is why the guide can carry all of them."""
    lines: list[str] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for choice in getattr(action, "_choices_actions", []):
                lines.append(f"- `{choice.dest}` -- {_flat(choice.help)}")
            break
    return lines


def _command_block_lines(label: str, subparser, json_stub: str) -> list:
    """One command's usage line plus its own flags.

    Used for a top-level command and, one level down, for each sub-command of a
    group like `file`, so `arbite file edit` is documented in its own right rather
    than hidden behind `{claim,read,write,...}`."""
    lines = [f"**`{label}`** -- `{_usage(subparser)}`"]
    bullets = []
    for action in _iter_actions(subparser):
        required = bool(action.option_strings and action.required)
        help_text = _shorten(action.help, json_stub)
        if required and help_text.endswith(" (required)"):
            # Already flagged as required on this line.
            help_text = help_text[: -len(" (required)")]
        marker = " (required)" if required else ""
        bullets.append(f"- `{_action_label(action)}`{marker} -- {help_text}")
    if bullets:
        lines.append("")
        lines.extend(bullets)
    lines.append("")
    return lines


def _stale_store_warning(stale_kind, stale_root, stale_count, sink_kind, sink_root) -> str:
    """The one paragraph a project with two ticket stores must never be without.

    A store holding tickets that nothing selects looks exactly like an empty one, so
    the guide states the mismatch in bold and names both stores and the way out."""
    return (
        f"> **Warning: this project holds a second ticket store that nothing selects.** "
        f"There are {stale_count} ticket(s) in a `{stale_kind}` store at `{stale_root}`, "
        f"but a plain `arbite` command -- no `--sink`, no `ARBITE_SINK` -- reads the "
        f"`{sink_kind}` store at `{sink_root}` instead. Decide before running anything "
        f"that writes: either point the project at the other store (add `sink: "
        f"{stale_kind}` to `.arbite/project.yaml`, or pass `--sink {stale_kind}`), or bring its "
        f"tickets across with `arbite migrate --from {stale_kind} --to {sink_kind}`. "
        f"**The wrong store looks exactly like an empty one**, so if a command reports no "
        f"tickets, run `arbite sink info --json` and check its `kind` field before "
        f"concluding the queue is empty."
    )


def render(parser, subparsers_by_name: dict, active_info=None, stale_info=None) -> str:
    """Render the lean guide for the sink this project will actually use.

    `active_info` describes the store a *plain* command reads: the committed config,
    with no `--sink` and no `ARBITE_SINK` in play -- which is what an agent reading
    this file will get when it runs the commands here. `stale_info`, when given,
    describes another store in the same project that *has tickets* and that nothing
    selects (a database left behind by a deleted config, an `ARBITE_SINK` one-off,
    a half-finished migration).

    An agent reads this guide and then runs `arbite` with no flags, so the prose that
    describes *behavior* follows `active_info`, and a stale store is called out in
    bold rather than quietly ignored. A guide that named the wrong store would be
    worse than no guide at all: the wrong store looks exactly like an empty one.

    Only what an agent must act on is here: the rules, the workflow, the fields and a
    one-line index of every command. The per-flag reference for the workspace commands
    is in WORKSPACE.md (`render_workspace` below), and every other command's flags are
    a `arbite <cmd> -h` away -- which is what keeps this file small enough to be read
    on every task."""
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
    # The proxy sections describe a capability a sink may not have, so the question is
    # asked of the authority that builds the store (`open_coordination_store`) rather
    # than answered here with a second list of kinds.
    from .coordination.store import has_coordination_backend

    has_proxy = sink_kind is not None and has_coordination_backend(sink_kind)

    lines: list[str] = []
    add = lines.append

    add("# arbite -- agent guide")
    add("")
    _stamp(
        add,
        "Not auto-discovered -- point your project's CLAUDE.md (or similar) at it, e.g. a line "
        "'read .arbite/AGENTS.md'. The file-proxy and workspace-command reference is "
        "`.arbite/WORKSPACE.md`",
    )

    # -- What this is ------------------------------------------------------
    add("## What this is")
    add("")
    add(
        "`arbite` is a low-tech ticketing system that lives inside this git repo, so several AI "
        "agents (and humans) can pick up tasks, track state, and leave a clean history of what "
        "happened and when. Use it -- not ad hoc notes or files -- to create, claim, block, "
        "shelve, close and reopen work, and leave progress with `arbite note` (it appends a "
        "timestamped, agent-identified entry to the ticket's `## Notes`)."
    )
    add("")
    if status_is_location:
        add(
            "**Where a ticket is filed *is* its state.** Tickets are markdown files with YAML "
            "frontmatter, and the folder a ticket sits in mirrors its `status`: every command "
            "makes the move and the frontmatter update together, and when the two disagree **the "
            "folder is the source of truth**. A claimed ticket is one sitting in `in_progress/` "
            "with `assignee` set to your id -- your memory of what you were doing is only a hint "
            "to check against where the ticket actually is."
        )
    else:
        add(
            "**Status is a field, and that field is the single source of truth** -- there is no "
            "folder to read as a shortcut. A claimed ticket is one whose `status` is "
            "`in_progress` with `assignee` set to your id (`arbite show <id> --json`, "
            "`arbite list --assignee <your-id>`); your memory of what you were doing is only a "
            "hint to check against the ticket itself."
        )
    add("")

    # -- Where tickets live ------------------------------------------------
    add("## Where tickets live (the sink)")
    add("")
    add(
        "Tickets live in a **sink**: a storage backend selected per command. Commands, fields, "
        "filters and exit codes behave identically whichever one is in use, so only two things "
        "change: where the data sits, and what `arbite doctor` can check."
    )
    add("")
    if mismatch:
        add(_stale_store_warning(stale_kind, stale_root, stale_count, sink_kind, sink_root))
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
        "your command will read. Selection, highest precedence first: `--sink <kind>`, "
        "`ARBITE_SINK`, a `sink:` key in `.arbite/project.yaml`, then the default (`file`); that "
        "key is the committed choice, and `arbite init`/`arbite migrate` write it for you"
    )
    add(
        "- a top-level `review:` key (default `true`) is where a finished ticket goes: `review/` "
        "when true, `closed/` when false -- it does not remove the `review` status or folder, "
        "and a value that is not a real boolean is a config error naming the file and the key"
    )
    add(
        "- available kinds: `file` (markdown files under the arbite directory, the default, and "
        "the one that gives you `git log --follow` history) and `sqlite` (a single database "
        "file, queryable with real SQL, and not version-control friendly); report or create "
        "yours with `arbite sink info` / `arbite sink init`"
    )
    add(
        "- `arbite status` counts the *backlog* -- every status in vocabulary order, zeros and a "
        "total included, narrowed by `--epic`/`--domain`/`--tier`/`--assignee`; `arbite sink "
        "info` describes the *store* instead (its kind, root and capabilities, not what is in it)"
    )
    add(
        "- `arbite progress` shows what is *in flight*: the epics holding a live ticket (open, "
        "in_progress or review) and then every ticket of those epics, closed and shelved "
        "siblings included, in dependency order -- an epic with nothing live never appears, "
        "live tickets with no epic group under `no epic`, and `--epic` narrows the report"
    )
    add(
        "- `arbite set-status <id> <status>` changes one ticket's status through the same code "
        "path as `arbite set <id> status <value>`, so the two cannot drift; the vocabulary comes "
        "from the schema (so `review`, and anything added later, is accepted), a status change "
        "un-files a ticket held in a bucket, and asking for the status it already has is a no-op"
    )
    add(
        "- `arbite submit <id>` hands finished work off: with `review:` on (the default) the "
        "ticket becomes `review` in `review/`, **keeping its assignee** -- they are who a "
        "reviewer sends it back to -- and with `review: false` the same command closes it. "
        "`arbite accept <id>` closes accepted work, credited to the reviewer; the rejection "
        "path is `arbite reopen --reason ...`, which is why that reason is mandatory"
    )
    add("")
    if status_is_location and sink_kind == "file":
        add("```")
        add(".arbite/")
        add("  raw/            unclassified captures -- not workable (see Triage)")
        add("    processed/    snapshots of promoted captures -- audit copies, not tickets")
        add("  open/           actionable, unclaimed")
        add("  in_progress/    claimed, being worked")
        add("  review/         finished, awaiting review")
        add("  blocked/        stalled -- see blocked_by")
        add("  shelved/        parked for later")
        add("  closed/YYYY-MM/ archived by close date")
        add("  wishlist/       reclassified wishes -- parked, not work")
        add("  plans/          roadmap notes and scratch docs -- not tickets")
        add("  agents/         one scratchpad file per agent identity")
        add("```")
        add("")
        add(
            "`raw/`, `open/`, `in_progress/`, `review/`, `blocked/`, `shelved/` and "
            "`closed/YYYY-MM/` are status folders: the frontmatter `status` mirrors the folder, "
            "every state-changing command updates both at once, and when something outside "
            "arbite breaks the pairing the folder wins. `wishlist/` and `plans/` are **buckets**, "
            "not statuses: a ticket filed in one is out of the status workflow (so `list next` "
            "never offers it) but keeps the status it had -- `arbite move <id> /plans` files it, "
            "`arbite move <id> /` un-files it, and what a bucket physically is belongs to the "
            "sink. Filenames never change on a move, so `git log --follow` traces a ticket's "
            "whole lifecycle."
        )
        add("")
        add(
            "`raw/processed/` holds verbatim copies of captures that have since been promoted "
            "(`<id>.raw.md`, written once and never overwritten). A snapshot is **not** a ticket "
            "-- it keeps its original `status: raw` frontmatter -- so everything under the "
            "directory is skipped by path: invisible to `list`, `fetch`, `doctor` and the id "
            "index, and an already-promoted request is never re-served."
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

    # -- Fields ------------------------------------------------------------
    add("## Ticket fields (YAML frontmatter)")
    add("")
    for field_name in FIELD_ORDER:
        add(f"- `{field_name}` -- {FIELD_NOTES.get(field_name, '')}")
    add("")
    add(
        "These axes are independent -- don't collapse them: `depends_on` (structural ticket ids) "
        "vs `references` (plan documents, root-relative under `.arbite/`) vs `blocked_by` "
        "(freeform prose); `tier` (capability) vs `domain` (specialization) vs `priority` "
        "(urgency) -- an urgent low-tier chore is possible, and a low-tier ticket can still be "
        "audio_gen-only; `domain` (routing) vs `tags` (search). Given a choice of workable "
        "tickets, take the lowest `priority` number."
    )
    add("")
    add(
        "Manage `references` one at a time with `arbite ref add <id> <path>...` / `arbite ref rm "
        "<id> <path>...` / `arbite ref list <id>` (`--json` gives `{id, references}`); `set "
        "references` and `create --references` take the comma-separated form. A referenced plan "
        "need not exist yet, so a missing one only **warns** (and `doctor` reports "
        "`dangling_reference`): write the plan, or drop the reference by hand -- `doctor --fix` "
        "will not guess which you meant."
    )
    add("")

    # -- Identity ----------------------------------------------------------
    add("## Agent identity and resuming work")
    add("")
    add(
        "Agent ids are `company.model.instance`, e.g. `claude.haiku.001`; each agent has a "
        "scratchpad at `.arbite/agents/<agent_id>.md`. Know your `tier` before claiming and pass "
        "it to `arbite list next --tier <tier>` so you are only offered work you can actually do."
    )
    add("")
    if status_is_location:
        add(
            "To resume: check your scratchpad for a last-known ticket id, then verify that ticket "
            "is still where a claimed ticket belongs -- in `in_progress/`, with `assignee` "
            "matching your own id (`arbite show <id> --json` prints its path). The location is "
            "ground truth, the scratchpad only a hint; if it is missing, stale or mismatched, "
            "look for a ticket assigned to you with `arbite list --assignee <your-id>`. Identity "
            "assignment, collision avoidance and liveness detection belong to the agent harness, "
            "not arbite."
        )
    else:
        add(
            "To resume: check your scratchpad for a last-known ticket id, then verify that ticket "
            "is still `status: in_progress` with `assignee` matching your own id "
            "(`arbite show <id> --json`). That is ground truth, the scratchpad only a hint; if it "
            "is missing, stale or mismatched, look for a ticket assigned to you with "
            "`arbite list --assignee <your-id>`. Identity assignment, collision avoidance and "
            "liveness detection belong to the agent harness, not arbite."
        )
    add("")

    # -- Workflow ----------------------------------------------------------
    add("## Typical workflow")
    add("")
    add("```")
    add("arbite list next --tier high                          # next workable open ticket")
    add("arbite list next --count 3 --tier high                # ...or a batch of three")
    add("arbite list next --epic mesh-pipeline                 # next workable ticket in an epic")
    add("arbite list next --epic classification                # next raw ticket needing triage")
    add("arbite list raw                                       # raw backlog as a todo list")
    add("arbite status                                         # tickets per status (+ total)")
    add("arbite status --epic mesh-pipeline                    # ...for one epic")
    add("arbite progress                                       # live epics and their closed siblings")
    add('arbite search --params title,body "LOD pop-in"        # find tickets by text')
    add('arbite raw feature "add per-mesh LOD"                 # quick capture; classify later')
    add('arbite raw request "collapse the toolbar by default"  # a tweak/lateral change request')
    add('arbite raw wish "fly-through camera preview"          # capture a wish; file it later')
    add("arbite fetch                                          # oldest raw ticket to classify")
    add('arbite promote tic-a1b2 --title "Add per-mesh LOD" --tier medium --domain mesh')
    add("arbite promote tic-a1b2 --agent claude.haiku.001      # ...or classify and claim at once")
    add("arbite move tic-a1b2 /wishlist                        # file a reclassified wish by hand")
    add("arbite show tic-a1b2                                  # read it in full")
    add("arbite claim tic-a1b2 --agent claude.haiku.001        # take it (status -> in_progress)")
    add('arbite note tic-a1b2 claude.haiku.001 "progress"      # ...do the work, log progress...')
    add('arbite block tic-a1b2 --reason "waiting on tic-c3d4"  # if stalled')
    add("arbite unblock tic-a1b2 --agent claude.haiku.001      # blocker cleared")
    add('arbite release tic-a1b2 --agent claude.haiku.001 --reason "wrong tier for me"')
    add('arbite shelve tic-a1b2 --reason "parked for later"    # if deprioritized')
    add('arbite unshelve tic-a1b2 --reason "back in scope"     # bring it back to open')
    add("arbite close tic-a1b2                                 # when done")
    add('arbite reopen tic-a1b2 --reason "tests fail on ARM"   # --reason is required')
    add("arbite set-status tic-a1b2 review                     # any status, same path as set status")
    add("arbite submit tic-a1b2                                # hand it off (review/, or closed when review: false)")
    add("arbite accept tic-a1b2 --agent claude.opus.001        # the reviewer closes it, credited")
    add("arbite sink info                                      # where do tickets live, and in what")
    add("arbite migrate --to sqlite                            # copy every ticket into another sink")
    add("```")
    add("")
    add(
        "`arbite bug|feature|request|memo|wish <message>` == `arbite raw <type> <message>`: the "
        "same raw ticket from a shorter command. Any command takes `-h`/`--help`, which is where "
        "its flags are documented (the reference below is a name and a purpose, not a flag list)."
    )
    add("")

    # -- Triage ------------------------------------------------------------
    add("## Triage: raw tickets, wishes, filing")
    add("")
    add(
        f"A **raw** ticket (`arbite raw <{'|'.join(RAW_TYPE_CHOICES)}> <message>`) is a "
        f"deliberately unclassified quick capture: status `raw`, never returned by `arbite list "
        f"next`, carrying only `type` plus a placeholder title (`<type> (raw): Requires "
        f"Classification`), auto-grouped under the `{CLASSIFICATION_EPIC}` epic (find them with "
        f"`arbite list next --epic {CLASSIFICATION_EPIC}`). Its body lists what triage must fill "
        f"in -- a real title, `tier`, `domain`, a real `epic`, `priority`, an expanded "
        f"description -- before it can be claimed. Raw tickets exist so a thought isn't lost, not "
        f"as work: classify them before picking them up."
    )
    add("")
    add(
        "`arbite list raw` prints the whole raw backlog as a todo list (grouped by type, one line "
        "per ticket, oldest first) until a classification run drains it. `arbite fetch [type]` "
        "pulls the single oldest raw ticket and prints it like `show`, with a `derived_note` "
        "telling you to classify it -- it never writes anything."
    )
    add("")
    add(
        "**`arbite promote <id>`** is the write half: it classifies a raw ticket in one command. "
        "It snapshots the capture verbatim to `raw/processed/<id>.raw.md` first (exclusive "
        "create, so promoting the same id twice is an error), then writes the required "
        "`--title`/`--tier`/`--domain` plus `--epic`/`--priority`/`--description`/`--tags`, **in "
        "place** through a compare-and-swap -- the id, `created` and any notes carry over, and an "
        "omitted `--epic` clears the `classification` grouping. It lands at `open`, or at "
        "`in_progress` assigned to `--agent <id>` when you are about to work it; an "
        "already-classified ticket is refused."
    )
    add("")
    add(
        "A **wish** (`arbite wish <message>`) is a wishlist item, not work: `promote` retypes it "
        "to `feature`, applies the classification and files it in the wishlist bucket, leaving "
        "its status at `raw` so it leaves the triage queue without becoming work (`--agent` is "
        "refused). A **request** is a tweak or lateral change to something that already exists "
        "-- ordinary work once classified, so keep the type and promote it like any other raw "
        "ticket. A **memo** asks for project notes/docs rather than a code change."
    )
    add("")

    # -- Conventions -------------------------------------------------------
    add("## Conventions (scripts and agent loops)")
    add("")
    add(
        "**Ticket ids.** An id argument is an exact id or any unique wildcard (substring) match: "
        "'f6' resolves to tic-f607, and an exact id always wins. A command that modifies a ticket "
        "treats an ambiguous match as an error listing the candidates; read-only `show` and "
        "`deps` take the first alphabetically instead."
    )
    add("")
    add(
        "**Exit codes**, so a shell loop can branch without matching on message text: 0 success "
        "with results; 1 error (bad arguments, ambiguous ticket id, a refused path, a claim "
        "naming the wrong attempt, ...); 2 the query ran fine but matched nothing (e.g. no "
        "workable ticket right now); 3 `arbite doctor` found integrity problems; 4 busy -- a live "
        "claim or attempt holds it and **nothing changed**, so pick other work rather than "
        "retrying; 5 stale -- a token, digest or generation is no longer current and **nothing "
        "changed**, so re-read and retry. `arbite cmd` is the one exception: it returns the "
        "wrapped command's own code."
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
        "**Parse JSON, not tables.** `--json` on `list`, `list next`, `list raw`, `fetch`, "
        "`show`, `search`, `deps`, `doctor`, `sink`, `status` and `delete` emits machine-readable "
        "output whose field names match the frontmatter; the human table format is not a stable "
        "interface. The `path` field is whatever the sink calls a ticket's location."
    )
    add("")
    add(
        "**Claim in one step.** `arbite list next --claim <agent_id>` selects the most urgent "
        "workable ticket *and* claims it in the same write; `arbite claim <id> --agent "
        "<agent_id>` does the same for a ticket you already know. Either way the claim sets "
        "`status: in_progress` and the assignee together (on the file sink the ticket moves to "
        "`in_progress/`), so never follow a claim with a separate `set status` -- and prefer "
        "`--claim` to running `list next` then `claim`, because between those two commands "
        "another agent can take the ticket. A claim is a compare-and-swap: it fails if the ticket "
        "is already assigned to someone else unless you pass `--force`, and it refuses to write "
        "over a ticket that changed since it was read."
    )
    add("")
    add(
        "**A claim records an attempt, and hands you its id.** Claiming (directly, through `list "
        "next --claim`, or with `promote --agent`) records the *work attempt* that owns the work "
        "and prints it as `attempt: att-XXXX`: every later `arbite file` command presents that "
        "id, so keep it. Acquisition checks the rules a queue only filters on -- every "
        "`depends_on` closed, the classification real (no `TODO:` placeholders), the ticket "
        "`open` and unclaimed, no active attempt already -- so naming a ticket directly cannot "
        "bypass them. `--force` is the administrative takeover and needs `--reason`: it ends the "
        "holder's attempt as `interrupted`, records the revocation, and starts a fresh attempt "
        "for you. Work that was already `in_progress` before arbite tracked attempts is adopted "
        "with `arbite attempt adopt <id> --agent <your-id>`. Release, block, shelve and reopen "
        "end the attempt they stop, and reopening a ticket other work depends on flags the "
        "running dependents instead of undoing their work."
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
        "**Removing a ticket is not the same as finishing it.** `arbite close` moves a ticket out "
        "of the work queues and keeps it forever; `arbite delete <id> --force` destroys it, "
        "records a note saying so first, and is deliberately gated. Prefer `close`."
    )
    add("")

    # -- Commands ----------------------------------------------------------
    add("## Commands")
    add("")
    add(
        "Rendered from the installed version's own parsers, so it always matches the CLI: name "
        "and purpose here, flags and the longer descriptions in `arbite <cmd> -h` (the README "
        "documents every command in full). Every command accepts `--sink <kind>`; every status "
        "command updates `status`/`updated` together, and `block`, `shelve`, `release`, "
        "`unblock`, `reopen` and `unshelve` also append an automatic timestamped note. The "
        "workspace commands -- `file`, `scratch`, `receipt`, `changes`, `cmd`, `events`, "
        "`workspace` and `attempt` -- are documented flag by flag in `.arbite/WORKSPACE.md`."
    )
    add("")
    lines.extend(_command_index_lines(parser))
    add("")
    add(
        "Notes: `release` hands back work you stop part-way (clears the assignee and any block "
        "reason, so `list next` offers the ticket again); `unblock` clears `blocked_by` and "
        "returns the ticket to `in_progress` (`open` with `--open`) -- preferable to `arbite set "
        "status`, which would leave `blocked_by` populated and the ticket claiming to be "
        "stalled; `reopen` clears the closed date and any block reason and requires `--reason`, "
        "recording it as 'Reopened: <reason>.' -- it is the rejection path out of review; "
        "`unshelve` clears the assignee and any block reason; `set` takes PROPERTY VALUE pairs "
        "(quote multi-word values, `''` clears a field), is type-aware, cannot set the "
        "structural `id`, and re-files the ticket when `status` changes; `depend` adds a "
        "dependency (deduplicated) or, with one argument, clears them all; `search` matches a "
        "ticket if any selected field matches; `migrate` copies every ticket from one sink into "
        "another and leaves the source alone; `delete` destroys a ticket and needs `--force`; "
        "`status` always exits 0 (it is a report, not a query)."
    )
    add("")

    # -- The file proxy ----------------------------------------------------
    if has_proxy:
        add("## Changing files (the file proxy)")
        add("")
        add(
            "`arbite file` is the proxy for changing a file another agent may also touch: claim "
            "the paths, read for a version token, mutate under that token, then read the record "
            "back. **Prefer it to a shell write** for any file in this workspace -- only the "
            "proxy records the change against a ticket and an attempt, and `arbite changes <id>` "
            "/ `arbite receipt <op>` read that record back. A shell write is not blocked; it is "
            "unattributed drift that the next read reports as an external edit."
        )
        add("")
        add(
            "`arbite cmd -- CMD...` is the compromise when a familiar tool is the only sane "
            "option: it runs the tool and records what it observed it change (`--claim PATH...` "
            "adds guarded mode). The contract, the flags and the honest limits -- what arbite "
            "does *not* guarantee -- are in **`.arbite/WORKSPACE.md`**."
        )
        add("")
    elif sink_kind:
        add("## Changing files (the file proxy)")
        add("")
        add(
            f"**Not available in this project:** the active `{sink_kind}` sink has no coordination "
            "backend, so `arbite file ...`, `arbite cmd`, `arbite receipt`, `arbite changes`, "
            "`arbite events`, `arbite workspace` and `arbite attempt` refuse rather than pretend "
            "(exit 1, naming the sink). Everything outside the proxy -- tickets, statuses, the "
            "workflow above -- works normally."
        )
        add("")

    # Last line of defence: no escape code may reach the file, whatever the
    # running argparse version decided to colourise.
    return _ANSI_RE.sub("", "\n".join(lines)) + "\n"


def render_workspace(parser, subparsers_by_name: dict, active_info=None, stale_info=None) -> str:
    """Render the workspace reference for the sink this project will actually use.

    Same inputs and same rule as `render`, and it has to be a separate file for the
    same reason: this is detail an agent needs when it is about to change a file, and
    reading it on every task would spend context on flags that task never uses. The
    guide points here; here the pointer goes back, because the attempts and tickets
    named by every claim belong to the workflow there.

    A sink with a coordination backend gets the proxy contract and the per-flag
    reference for `WORKSPACE_COMMANDS`; one without gets a paragraph saying those
    commands refuse -- never a contract it cannot honour."""
    _disable_color(parser, *subparsers_by_name.values())

    primary = active_info if active_info is not None else stale_info
    sink_kind = getattr(primary, "kind", None)
    sink_root = getattr(primary, "root", None)
    stale_kind = getattr(stale_info, "kind", None)
    stale_root = getattr(stale_info, "root", None)
    stale_count = getattr(stale_info, "ticket_count", 0)
    mismatch = active_info is not None and stale_info is not None
    from .coordination.store import has_coordination_backend

    has_proxy = sink_kind is not None and has_coordination_backend(sink_kind)

    lines: list[str] = []
    add = lines.append

    # The JSON flag's boilerplate points at the guide's exit-code table rather than
    # repeating it: this file is reached from there.
    json_stub = "JSON output (see 'Conventions' in .arbite/AGENTS.md)"

    add("# arbite -- workspace and file-proxy guide")
    add("")
    _stamp(
        add,
        "Reached from `.arbite/AGENTS.md`, which owns the ticket workflow (statuses, fields, "
        "claiming, triage); this file owns everything that changes a file",
    )
    add("## What this covers")
    add("")
    if not has_proxy:
        add(
            f"**Not available in this project:** the active `{sink_kind}` sink has no coordination "
            "backend, so `arbite file ...`, `arbite cmd`, `arbite receipt`, `arbite changes`, "
            "`arbite events`, `arbite workspace` and `arbite attempt` refuse rather than pretend "
            "(exit 1, naming the sink). Everything outside the proxy -- tickets, statuses, the "
            "workflow in `.arbite/AGENTS.md` -- works normally, so there is no workspace contract "
            "to describe here."
        )
        add("")
        return _ANSI_RE.sub("", "\n".join(lines)) + "\n"
    add(
        "The **workspace** is the set of files arbite manages in this project: the proxy records "
        "which *work attempt* owns a path, serves bytes with a version token, changes bytes only "
        "under that token, and keeps the before and after bytes as evidence. This file is the "
        f"contract, the flags and the honest limits. Active sink: `{sink_kind}`"
        + (f" at `{sink_root}`" if sink_root else "")
        + " (`arbite sink info --json` confirms it; the ticket workflow is in `.arbite/AGENTS.md`)."
    )
    add("")
    if mismatch:
        add(_stale_store_warning(stale_kind, stale_root, stale_count, sink_kind, sink_root))
        add("")
    add(
        "`arbite workspace show` reports the workspace this project derives -- its id, root, the "
        "store behind it, the coordination state (active claims, events, receipts) and the "
        "scratch area -- and writes nothing, so two runs on unchanged state print identical "
        "text. It is derived from the located `.arbite/` directory plus the resolved sink, which "
        "is why there is no bind command to forget and no conflict path."
    )
    add("")

    # -- The contract ------------------------------------------------------
    add("## Claim, read, mutate, release")
    add("")
    add(
        "The order the proxy exists to enforce: a claim is exclusive writer ownership of a path, "
        "a read under that claim returns a token, and a mutation presents the token."
    )
    add("")
    add(
        "- claim before you write: `arbite file claim PATH... --ticket T --attempt A` is "
        "all-or-nothing in canonical path order, so two agents can never hold half of a pair; a "
        "path another attempt holds refuses the whole request with exit 4 and names the holder"
    )
    add(
        "- read for a token: `arbite file read PATH --ticket T --attempt A` serves the bytes and "
        "records a token (`op-XXXX`); one token authorises exactly one mutation. `--lines "
        "START[:END]` serves a range, and the digest still covers the whole file"
    )
    add(
        "- mutate under that token: `arbite file write` (a whole file, from `--input NAME` in "
        "`.arbite/scratch/` or stdin), `arbite file edit` (an ordered batch of exact "
        "substitutions, all-or-nothing), `arbite file remove`, `arbite file rename` (both paths "
        "at one generation). A stale token, moved bytes, a revoked generation or a closed ticket "
        "exits 5 and changes no bytes"
    )
    add(
        "- hand a claim back without touching the bytes: `arbite file release PATH... --ticket T "
        "--attempt A --reason TEXT`, so the next worker knows what to re-read"
    )
    add(
        "- `arbite file claims [--all]` reports what is held right now (or a path's history), and "
        "`arbite file list` / `arbite file search` are bounded, canonical-order listings that "
        "never authorise a write"
    )
    add("")

    # -- Passthrough -------------------------------------------------------
    add("## Keeping your habits: arbite cmd")
    add("")
    add(
        "`arbite cmd [--ticket T --attempt A] [--shell] -- CMD...` runs a familiar tool (`grep`, "
        "`sed`, `mv`) and records what it *observed* it change: a digest manifest of the managed "
        "paths before and after, a receipt and a `passthrough.changed` event per path that "
        "differed, one `passthrough.exec` event for the run -- and **no exclusivity claimed**, "
        "because nothing was acquired."
    )
    add("")
    add(
        "`--claim PATH...` is guarded mode instead: those paths are acquired all-or-nothing "
        "*before* the command starts (a busy path refuses the whole run, exit 125, and nothing "
        "starts), every change is verified against the claimed set afterwards (a change outside "
        "it is reported as `unclaimed_write` and left exactly where the tool put it, because "
        "arbite does not roll back a command it did not perform), and the claims are released "
        "when the run ends, including when the tool fails. The command's own exit code is what "
        "you get back; arbite's own refusals are 125 (refused before running), 126 (an "
        "invocation arbite does not support) and 127 (the tool is not on PATH)."
    )
    add("")

    # -- Reading the record ------------------------------------------------
    add("## Reading the record")
    add("")
    add(
        "- `arbite changes <id>` -- one row per path, from the version the first operation found "
        "to the version the last one left; `--all` adds the ordered operation log, where a "
        "change that was later reverted stays visible"
    )
    add(
        "- `arbite receipt <op>` -- one operation's evidence: the version it replaced, the version "
        "it wrote, where those bytes are kept, all read back and proved against the digests the "
        "receipt records before anything prints. Missing evidence is refused, not glossed over"
    )
    add(
        "- `arbite receipt --summary [--ticket T]` -- every operation in log order with both "
        "versions and who did it, plus what the store still holds. Take this *before* anything "
        "is pruned: the coordination store is ignored by git and dies with the machine, so this "
        "is the export a devlog is written from"
    )
    add(
        "- `arbite events [--tail N] [--after CURSOR] [--include-reads]` -- the coordination event "
        "stream, one line per event, in cursor order; reads are excluded unless asked for, and "
        "`--follow` is refused because arbite never blocks -- poll with `--after <cursor>`, where "
        "'nothing new' is exit 2 rather than an error"
    )
    add(
        "- `arbite scratch list` / `arbite scratch clear [NAME...] [--all]` -- what is staged as "
        "the payload of a write or an edit, and how to empty it deliberately"
    )
    add(
        "- `arbite attempt adopt <id> --agent <your-id>` -- record an attempt for work that was "
        "already `in_progress` when attempt tracking began"
    )
    add("")

    # -- Limits ------------------------------------------------------------
    add("## What arbite does not promise")
    add("")
    add(
        "- **A shell can bypass it.** arbite does not intercept the filesystem: `sed -i`, `> "
        "file`, or an editor writes bytes arbite never sees. Those bytes are *drift*, reported by "
        "the next read as an external edit attributable to no ticket, and **arbite cannot prove "
        "who wrote a file** -- it records attribution (the agent id a command carried, the actor "
        "in an event), never authentication"
    )
    add(
        "- **Observation is not exclusivity.** `arbite cmd` observed mode claims nothing; guarded "
        "mode claims before the run and verifies after it, but cannot stop a write *during* it, "
        "so a takeover mid-run or a foreign writer is reported rather than prevented"
    )
    add(
        "- **Reads are not isolated.** A read can observe a commit in flight; the file sink "
        "reports that as a `pending_commit` finding instead of hiding it"
    )
    add(
        "- **No daemon, no launcher, no automatic recovery.** Nothing runs in the background, a "
        "lock dies with its process, a killed guarded run leaves its claim for `arbite doctor` "
        "(or `--fix`) to release once the attempt is over, and there is no worktree workflow, no "
        "central database, no factory and no dashboard"
    )
    add(
        "- **Evidence is never pruned.** There is no retention policy or garbage collection yet, "
        "so the store grows with the work (each version is kept once, by digest, and one version "
        "may be at most 64 MiB); `arbite receipt --summary` prints the pre-pruning export a devlog "
        "wants. `arbite cmd`'s manifest walks the tree before and after each run, and arbite waits "
        "for the command it started"
    )
    if sink_kind == "sqlite":
        add(
            "- **This sink keeps evidence in the database.** The `sqlite` store holds the "
            "coordination records *and* the artifact bytes in the ticket database, so the "
            "database is one thing to back up but grows with every mutation; `doctor` adds its "
            "storage-specific findings (orphaned revision rows) to the shared ones"
        )
    else:
        add(
            "- **This sink keeps evidence beside the tickets.** The file sink holds coordination "
            "state in `.arbite/coordination/` and artifact bytes as files under it (both ignored "
            "by git), so evidence is local to this checkout; `doctor` adds its storage-specific "
            "findings (a pending commit journal, orphan revision counters) to the shared ones"
        )
    add(
        "- **Exit 4 and 5 mean nothing changed**, so neither is worth retrying blind: 4 is busy "
        "(a live claim or attempt holds the path), 5 is stale (a token, digest or generation is "
        "no longer current). The full exit-code vocabulary is in `.arbite/AGENTS.md`"
    )
    add("")

    # -- Commands ----------------------------------------------------------
    add("## Commands")
    add("")
    add(
        "Every command accepts `--sink <kind>` (documented once, in `.arbite/AGENTS.md` with the "
        "rest of the sink facts). Argparse's longer descriptions are omitted here too -- `arbite "
        "<cmd> -h` prints them."
    )
    add("")
    for name in WORKSPACE_COMMANDS:
        subparser = subparsers_by_name.get(name)
        if subparser is None:  # pragma: no cover - the parser always defines these
            continue
        lines.extend(_command_block_lines(f"arbite {name}", subparser, json_stub))
        for child_name, child in _nested_choices(subparser):
            lines.extend(_command_block_lines(f"arbite {name} {child_name}", child, json_stub))

    # Last line of defence: no escape code may reach the file, whatever the
    # running argparse version decided to colourise.
    return _ANSI_RE.sub("", "\n".join(lines)) + "\n"
