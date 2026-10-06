"""The `arbite docs` documentation surface.

Documentation is a command, not a set of committed files. `arbite docs` prints a
token-light overview -- the always-read half, kept small because it is pulled into an
agent's context -- and `arbite docs <topic>` prints one deeper subject on demand: the
workflow, the fields, the sinks, agent identity and triage, the conventions that
govern scripts and agent loops, the file proxy and its evidence, the honest limits,
and the design background. `arbite docs commands [NAME]` renders a command's usage
and flags from the live parser, and `arbite docs list` is the topic index. Nothing is
regenerated into a project's tree, so editing a sentence here changes what every
project reads with no git churn.

Topics are Python functions returning markdown lines -- this module is the registry.
The reference material is rendered from the installed argparse parsers and the schema
constants, so a topic can only ever name a command, status or field the CLI actually
accepts. The prose is also *sink-aware*: it describes the store the reader's plain
`arbite` command will touch, so a database-backed project is never told "the folder is
the source of truth", and a sink with no coordination backend is never handed a
file-proxy contract.

`arbite init` no longer writes either generated document; it leaves one static pointer
at `.arbite/AGENTS.md` telling a harness to run `arbite docs` (see
`install_agents_pointer`). The instructions block installed into a project's own
AGENTS.md/CLAUDE.md lives here too.

TICKET_ID_HELP / TICKET_ID_HELP_READONLY / JSON_HELP / MESSAGE_HELP live here
(cli.py imports them) so the command reference can collapse the copies of that
boilerplate into a single cross-reference without the two copies drifting apart.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

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

## Docs

Arbite documents itself from the command line: run `arbite docs` for the overview,
`arbite docs list` for every topic, and `arbite docs commands <name>` for a command's
flags. This block is the short version; `arbite docs` is the source of truth.

## Ticketing
Use arbite for all tasks. Create a ticket if required and claim it before starting work. Tickets live under `.arbite/` in state folders (`open/`, `in_progress/`, `review/`, `blocked/`, `shelved/`, `closed/YYYY-MM/`); `wishlist/` and `plans/` are buckets, not statuses, and `raw/processed/` holds frozen snapshots of promoted captures. The store and the review behaviour come from `.arbite/project.yaml` (`sink:`, `review:`).

# Agent Identity

When claiming a ticket, use an identity like "claude.opus-5.001" -- company.model.instance, your best educated guess unless told otherwise. If orchestrating, tell subagents their identity and instance number.

## Sole command: "Work Next|All <epic>"

```bash
arbite list next                      # next workable ticket
arbite list --topo --status open [--epic <epic>]
```

If there are no tickets, use "Classify". If "Work All", orchestrate if that is in your skill set, otherwise work in sequence until finished.

## Sole command "Classify"

```bash
arbite list raw
```

Classify every raw ticket with `arbite promote <id> --title ... --tier ... --domain ...` (add `--description`/`--epic`/`--priority`/`--tags` as you learn more). Add `--agent <your-id>` to classify and claim one you will work yourself; a wish is reclassified and filed in the wishlist bucket instead of being opened.

## Workflow: claim -> in_progress -> submit -> review -> accept

```bash
arbite claim <id> --agent <your-id>        # take it: status -> in_progress
arbite stream write <id> -                 # narrate as you work (piped from your output)
arbite note <id> <your-id> "what changed"  # log progress as you go
arbite submit <id>                         # finish: status -> review, assignee kept
arbite accept <id> --agent <reviewer-id>   # the reviewer closes it, credited to them
```

Send work back with `arbite reopen <id> --reason "<why>"` (the reason is required -- it is the only record of what the author must fix). With `review: false` in `.arbite/project.yaml`, `arbite submit` closes the ticket directly instead of parking it in review.

# Ticketing etiquette addendum

When closing a ticket, add a note paragraph explaining what the user, QA, or other agents will be able to observe via integration testing (and any new effects that will be observable).

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


# Markers delimit the ignore block: they make it *the arbite section*, so
# `install_gitignore` rewrites that one region in place and leaves every other
# line of the file exactly as it found it.
ARBITE_GITIGNORE_BEGIN = "# BEGIN ARBITE GITIGNORE"
ARBITE_GITIGNORE_END = "# END ARBITE GITIGNORE"

# The runtime state `arbite init` itself creates beside the tickets, and any
# sqlite store with its WAL/SHM sidecars. The paths are anchored to the project
# root (a leading slash) because a bare pattern would also swallow a
# same-named directory anywhere in the tree.
ARBITE_GITIGNORE_BLOCK = """\
# BEGIN ARBITE GITIGNORE
# Runtime state arbite writes beside the tickets: coordination claims/attempts/
# receipts/events/artifacts, scratch payloads, per-ticket narration streams, and
# any sqlite store (sidecars included). Tickets, agent scratchpads and the plans
# are the development record and stay committed; these are local evidence.
/.arbite/coordination/
/.arbite/scratch/
/.arbite/streams/
/.arbite/arbite.db*
# END ARBITE GITIGNORE"""


def install_gitignore(path: Path) -> str:
    """Set the arbite runtime-state entries in the ignore file at `path`.

    The markers make the block *the arbite section*: when the file already
    carries one, only the marked region is replaced with the current entries --
    everything above and below it survives byte for byte, and a stale section
    from an older arbite is refreshed rather than fossilised.

    Returns what it did, so `arbite init` can report it:

    - 'created'  -- the file did not exist; it is written with the block alone;
    - 'appended' -- the file existed with no section; the block is appended
      below the existing contents. Appended rather than prepended because git's
      last matching rule wins: arbite's lines must not get to override a
      project's own later rules;
    - 'updated'  -- the file carried a marked section and it changed; only that
      region was rewritten;
    - 'present'  -- the marked section already holds exactly the current
      entries, so the file is left untouched.

    A half-section -- one marker without the other, or markers out of order --
    counts as 'present': demarkation is then the project's business, and there
    is no safe region to replace."""
    if not path.exists():
        path.write_text(ARBITE_GITIGNORE_BLOCK + "\n", encoding="utf-8")
        return "created"
    existing = path.read_text(encoding="utf-8")
    begin = existing.find(ARBITE_GITIGNORE_BEGIN)
    end = existing.find(ARBITE_GITIGNORE_END)
    if begin == -1 and end == -1:
        path.write_text(
            existing.rstrip("\n") + "\n\n" + ARBITE_GITIGNORE_BLOCK + "\n",
            encoding="utf-8",
        )
        return "appended"
    if begin == -1 or end <= begin:
        return "present"
    end += len(ARBITE_GITIGNORE_END)
    replaced = existing[:begin] + ARBITE_GITIGNORE_BLOCK + existing[end:]
    if replaced == existing:
        return "present"
    path.write_text(replaced, encoding="utf-8")
    return "updated"


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
# one points at the exit-code and JSON contract in the `conventions` topic.
_JSON_STUB = "JSON output (see 'conventions' under `arbite docs list`)"
_SUBSTITUTIONS = (
    (TICKET_ID_HELP, "see 'ticket ids' in `arbite docs conventions`"),
    (TICKET_ID_HELP_READONLY, "see 'ticket ids' in `arbite docs conventions`"),
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
        "See 'tier' in `arbite docs fields`.",
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

# The global selectors appear on every command; documenting them once, in the sink
# section, beats repeating them under all twenty-odd commands.
_GLOBAL_ACTIONS = ("help", "sink", "root", "version", "color")


# ANSI/CSI escape sequences. argparse >= 3.14 colours its help output when it
# believes it is writing to a terminal (or when FORCE_COLOR is set, which agent
# harnesses do), and those codes would otherwise end up embedded in the markdown
# as junk like '\x1b[1;34m'. The docs are always read as plain text, so every line
# is stripped of escapes before it is returned.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _flat(text: str) -> str:
    """Collapse a wrapped help string onto one line, with no escape codes."""
    return _ANSI_RE.sub("", " ".join((text or "").split()))


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
    the docs state the mismatch in bold and name both stores and the way out."""
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


# ---------------------------------------------------------------------------
# The `arbite docs` surface
# ---------------------------------------------------------------------------
#
# Documentation is a command, not a set of committed files. `arbite docs` prints the
# token-light overview; `arbite docs <topic>` prints one deeper subject. Topic prose
# lives in this module; the command reference is rendered from the live parsers so a
# topic can only ever name a command the CLI actually accepts, and everything is
# filtered through `build_context` so the prose matches the store the reader's plain
# `arbite` command will touch. Nothing is regenerated into a project's tree, so
# editing a sentence here changes what every project reads with no git churn.

# The one static file `arbite init` leaves behind: a pointer, not a copy of the docs.
# Its content never varies with the sink or the version, so re-running `init` is a
# no-op and a project's committed docs stop churning.
AGENTS_POINTER = """\
# arbite

This project uses arbite for ticketing, and its documentation is a command rather
than a file.

Run `arbite docs` for the overview, `arbite docs list` for every topic, and
`arbite docs commands <name>` for one command's usage and flags.

This file is a pointer written by `arbite init`; edit the package's `docs.py`
instead."""


def install_agents_pointer(path: Path) -> str:
    """Write the static `.arbite/AGENTS.md` pointer, if it is ours to write.

    Returns what it did, so `arbite init` can report it:

    - 'created'   -- the file did not exist; it is written with the pointer;
    - 'refreshed' -- the file held the old generated guide (its header is
      '# arbite -- agent guide'); it is replaced with the pointer;
    - 'present'   -- the file already holds exactly the pointer; left untouched;
    - 'kept'      -- the file is something else (a hand-written guide); left
      untouched rather than clobbered.

    Written once and then left alone: the pointer is constant, so a re-run never
    dirties the tree the way the old sink-and-version-stamped guide did."""
    if not path.exists():
        path.write_text(AGENTS_POINTER + "\n", encoding="utf-8")
        return "created"
    existing = path.read_text(encoding="utf-8")
    if existing == AGENTS_POINTER + "\n":
        return "present"
    if existing.lstrip().startswith("# arbite -- agent guide"):
        path.write_text(AGENTS_POINTER + "\n", encoding="utf-8")
        return "refreshed"
    return "kept"


@dataclass
class DocContext:
    """Everything a topic renderer may branch on: the live parsers and the store the
    reader's plain `arbite` command will actually touch.

    `sink_kind`/`status_is_location` follow the committed config, not the flags of
    the invocation that asked for docs: a `--sink` one-off must not describe a
    different project than the one the reader is in. `standalone` is set when no
    `.arbite/` was located, in which case the defaults a fresh project would get
    (the `file` sink) are described so `arbite docs` still reads sensibly anywhere."""

    parser: Any
    subparsers_by_name: dict
    active_info: Any = None
    stale_info: Any = None
    sink_kind: Optional[str] = None
    sink_root: Optional[str] = None
    status_is_location: bool = True
    has_proxy: bool = True
    stale_kind: Optional[str] = None
    stale_root: Optional[str] = None
    stale_count: int = 0
    mismatch: bool = False
    standalone: bool = False


def build_context(parser, subparsers_by_name, active_info=None, stale_info=None,
                  standalone=False) -> DocContext:
    """Assemble the render context from the parsers and the located stores.

    `active_info` describes the store a *plain* command reads (committed config only);
    `stale_info`, when given, describes another store in the same project that holds
    tickets and that nothing selects. `standalone` says no project was located."""
    primary = active_info if active_info is not None else stale_info
    sink_kind = getattr(primary, "kind", None) or "file"
    from .coordination.store import has_coordination_backend

    return DocContext(
        parser=parser,
        subparsers_by_name=subparsers_by_name,
        active_info=active_info,
        stale_info=stale_info,
        sink_kind=sink_kind,
        sink_root=getattr(primary, "root", None),
        status_is_location=bool(getattr(primary, "status_is_location", sink_kind == "file")),
        has_proxy=has_coordination_backend(sink_kind),
        stale_kind=getattr(stale_info, "kind", None),
        stale_root=getattr(stale_info, "root", None),
        stale_count=getattr(stale_info, "ticket_count", 0),
        mismatch=active_info is not None and stale_info is not None,
        standalone=standalone,
    )


def _topic_overview(ctx: DocContext) -> list:
    """The always-read half: what arbite is, the mental model, the commands a caller
    actually types, and where to go deeper. Budgeted by a test -- it is pulled into an
    agent's context, so length is a design constraint, not a preference."""
    lines: list = []
    add = lines.append
    add("# arbite")
    add("")
    add(
        "`arbite` is a low-tech ticketing system that lives inside this git repo, so "
        "several agents (and humans) can pick up tasks, track state, and leave a clean "
        "history of what happened and when. It is a command, not a service: no daemon, "
        "no server, no lock service."
    )
    add("")
    if ctx.status_is_location:
        add(
            "**A ticket's state is the folder it sits in.** Tickets are markdown files "
            "with YAML frontmatter; `status` mirrors the folder (`open/`, "
            "`in_progress/`, `review/`, ...), and when the two disagree **the folder "
            "wins**. A claimed ticket is one in `in_progress/` with `assignee` set to "
            "your id."
        )
    else:
        add(
            "**A ticket's `status` is a field and the single source of truth** -- there "
            "is no folder to read as a shortcut. A claimed ticket is one whose `status` "
            "is `in_progress` with `assignee` set to your id."
        )
    add("")
    if ctx.standalone:
        add(
            "_No `.arbite/` found here, so this describes the default (`file`) sink. "
            "Run `arbite docs` inside a project for docs tailored to its sink._"
        )
        add("")
    add("## The commands you will type most")
    add("")
    add("```")
    add("arbite list next --tier high                  # next workable ticket")
    add("arbite list next --tier high --claim <id>     # select and claim in one step")
    add("arbite claim <id> --agent <your-id>           # take a ticket you already know")
    add('arbite note <id> <your-id> "what changed"     # log progress as you go')
    add("arbite submit <id>                            # hand off (review/, or closed)")
    add("arbite accept <id> --agent <reviewer-id>      # the reviewer closes it")
    add('arbite bug|feature|request|memo|wish "..."   # quick capture; classify later')
    add("arbite fetch                                   # oldest raw capture")
    add("arbite promote <id> --title ... --tier ... --domain ...   # classify in place")
    add("arbite show <id>                              # read a ticket in full")
    add("arbite status                                  # backlog counts per status")
    add("arbite sink info                              # which store am I reading")
    add("```")
    add("")
    add(
        "Changing a file another agent may touch? Use the proxy (`arbite docs workspace`) "
        "rather than a shell write: only the proxy records the change against a ticket and "
        "an attempt, and a shell write is drift the next read reports as an external edit."
    )
    add("")
    add("## Go deeper")
    add("")
    add("- `arbite docs list` -- every topic, one line each")
    add("- `arbite docs <topic>` -- one subject in depth")
    add("- `arbite docs commands [NAME]` -- usage and every flag for a command")
    add("- `arbite docs all` -- every topic in one stream (e.g. to regenerate a file)")
    add("- `arbite <command> -h` -- the CLI's own help for that command")
    add("")
    return lines


def _topic_workflow(ctx: DocContext) -> list:
    lines: list = []
    add = lines.append
    add("# Workflow: statuses and transitions")
    add("")
    add(
        "A ticket's `status` is one of `" + " | ".join(STATUSES) + "` -- its state in the "
        "workflow, and on a file sink the folder it sits in. `raw` is an unclassified "
        "capture (never offered by `list next`); `open` is actionable and unclaimed; "
        "`in_progress` is claimed; `review` is finished and awaiting a reviewer; "
        "`blocked` is stalled (`blocked_by`); `shelved` is parked; `closed` is done."
    )
    add("")
    add("## Transitions")
    add("")
    add(
        "- `claim <id> --agent <id>` -- take an open ticket: sets `status: in_progress` "
        "and the assignee together, and records the *work attempt* (printed as `attempt: "
        "att-XXXX` -- keep it, every later `arbite file` command presents it)"
    )
    add(
        "- `release <id> --agent <id> --reason TEXT` -- hand back work you stop part-way: "
        "clears the assignee and any block reason, so `list next` offers it again"
    )
    add(
        "- `block <id> --reason TEXT` / `unblock <id>` -- mark stalled, then clear the "
        "block and return to `in_progress` (`--open` returns it to `open`)"
    )
    add(
        "- `shelve <id> --reason TEXT` / `unshelve <id> --reason TEXT` -- park for later, "
        "then bring it back to `open`"
    )
    add(
        "- `close <id>` -- finish and archive (the ticket is kept forever); `delete <id> "
        "--force` destroys it, and is deliberately gated. Prefer `close`"
    )
    add(
        "- `reopen <id> --reason TEXT` -- reopen (clears the closed date and any block "
        "reason); the reason is **required**, and is the rejection path out of review"
    )
    add(
        "- `submit <id>` -- hand finished work off: with `review:` on (the default) it "
        "becomes `review` in `review/`, **keeping its assignee** (they are who a reviewer "
        "sends it back to); with `review: false` the same command closes it"
    )
    add(
        "- `accept <id> --agent <reviewer-id>` -- close accepted work, credited to the "
        "reviewer"
    )
    add(
        "- `set-status <id> <status>` -- any status, through the same path as `set <id> "
        "status <value>`; a change un-files a bucketed ticket, and asking for the status "
        "it already has is a no-op"
    )
    add("")
    if ctx.status_is_location:
        add(
            "Every state-changing command performs the file move and the frontmatter "
            "update in one operation, so `status` and the folder cannot disagree -- and "
            "when something outside arbite breaks the pairing, **the folder wins** and "
            "`arbite doctor --fix` rewrites the frontmatter to match. Use `set-status` in "
            "preference to `set status`: it is the same path and cannot drift."
        )
        add("")
    add(
        "`block`, `shelve`, `release`, `unblock`, `reopen` and `unshelve` also append an "
        "automatic timestamped note explaining the change."
    )
    add("")
    add("## A typical session")
    add("")
    add("```")
    add("arbite list next --tier high                          # next workable open ticket")
    add("arbite list next --count 3 --tier high                # ...or a batch of three")
    add("arbite list next --epic mesh-pipeline                 # next in one epic")
    add('arbite raw feature "add per-mesh LOD"                 # quick capture')
    add("arbite fetch                                          # oldest raw ticket")
    add('arbite promote tic-a1b2 --title "Add per-mesh LOD" --tier medium --domain mesh')
    add("arbite move tic-a1b2 /wishlist                        # file a reclassified wish")
    add("arbite show tic-a1b2                                  # read it in full")
    add("arbite claim tic-a1b2 --agent claude.haiku.001        # take it")
    add("arbite stream write tic-a1b2 -                         # narrate as you work")
    add('arbite note tic-a1b2 claude.haiku.001 "progress"      # log progress')
    add('arbite block tic-a1b2 --reason "waiting on tic-c3d4"  # if stalled')
    add("arbite unblock tic-a1b2 --agent claude.haiku.001      # blocker cleared")
    add('arbite release tic-a1b2 --agent claude.haiku.001 --reason "wrong tier for me"')
    add('arbite shelve tic-a1b2 --reason "parked for later"    # if deprioritized')
    add('arbite unshelve tic-a1b2 --reason "back in scope"     # bring it back to open')
    add("arbite submit tic-a1b2                                # hand off")
    add("arbite accept tic-a1b2 --agent claude.opus.001        # the reviewer closes it")
    add("arbite sink info                                      # where do tickets live")
    add("arbite migrate --to sqlite                            # copy into another sink")
    add("```")
    add("")
    add(
        "`arbite bug|feature|request|memo|wish <message>` == `arbite raw <type> "
        "<message>`: the same raw ticket from a shorter command. Read the `triage` topic "
        "for capture and classification, and `conventions` for ids, exit codes and JSON."
    )
    add("")
    return lines


def _topic_fields(ctx: DocContext) -> list:
    lines: list = []
    add = lines.append
    title = "# Ticket fields" + (" (YAML frontmatter)" if ctx.status_is_location else "")
    add(title)
    add("")
    for field_name in FIELD_ORDER:
        add(f"- `{field_name}` -- {FIELD_NOTES.get(field_name, '')}")
    add("")
    add(
        "These axes are independent -- don't collapse them: `depends_on` (structural "
        "ticket ids) vs `references` (plan documents, root-relative under `.arbite/`) vs "
        "`blocked_by` (freeform prose); `tier` (capability) vs `domain` (specialization) "
        "vs `priority` (urgency) -- an urgent low-tier chore is possible, and a low-tier "
        "ticket can still be audio_gen-only; `domain` (routing) vs `tags` (search). "
        "Given a choice of workable tickets, take the lowest `priority` number."
    )
    add("")
    add(
        "Manage `references` one at a time with `arbite ref add <id> <path>...` / `arbite "
        "ref rm <id> <path>...` / `arbite ref list <id>` (`--json` gives `{id, "
        "references}`); `set references` and `create --references` take the comma-separated "
        "form. A referenced plan need not exist yet, so a missing one only **warns** (and "
        "`doctor` reports `dangling_reference`): write the plan, or drop the reference by "
        "hand -- `doctor --fix` will not guess which you meant."
    )
    add("")
    return lines


def _topic_sinks(ctx: DocContext) -> list:
    lines: list = []
    add = lines.append
    add("# Where tickets live (the sink)")
    add("")
    add(
        "Tickets live in a **sink**: a storage backend selected per command. Commands, "
        "fields, filters and exit codes behave identically whichever one is in use, so "
        "only two things change: where the data sits, and what `arbite doctor` can check."
    )
    add("")
    if ctx.mismatch:
        add(_stale_store_warning(ctx.stale_kind, ctx.stale_root, ctx.stale_count,
                                 ctx.sink_kind, ctx.sink_root))
        add("")
    add(
        f"- active sink: `{ctx.sink_kind}`"
        + (f" at `{ctx.sink_root}`" if ctx.sink_root else "")
        + " -- what a command with no `--sink` flag reads"
    )
    if ctx.mismatch:
        add(
            f"- also present, but not selected: `{ctx.stale_kind}` at `{ctx.stale_root}` "
            f"({ctx.stale_count} ticket(s))"
        )
    add(
        "- selection, highest precedence first: `--sink <kind>`, the `ARBITE_SINK` "
        "environment variable, a `sink:` key in `.arbite/project.yaml`, then the default "
        "(`file`). `init`/`migrate` write that key for you, so the store you set up is the "
        "one a plain command reads"
    )
    add(
        "- available kinds: `file` (markdown files under the arbite directory, the "
        "default, and the one that gives you `git log --follow` history) and `sqlite` (a "
        "single database file, queryable with real SQL, and not version-control "
        "friendly). Report or create yours with `arbite sink info` / `arbite sink init`; "
        "`arbite sink info --json`'s `kind` field is the store your command will read"
    )
    add(
        "- `--root DIR` runs the command against another project: the project is the "
        "nearest `.arbite/` at or above DIR, exactly as if arbite were started there. It "
        "changes only *which* project is located, not the sink selection"
    )
    add(
        "- `--color WHEN` decides when reports are coloured: `auto` (the default, only "
        "when stdout is a terminal that will render escapes), `always`, or `never`. "
        "`NO_COLOR` turns it off and `ARBITE_COLOR` sets an environment's default; "
        "`--json` is never coloured"
    )
    add(
        "- a top-level `review:` key (default `true`) is where a finished ticket goes: "
        "`review/` when true, `closed/` when false -- it does not remove the `review` "
        "status or folder, and a value that is not a real boolean is a config error "
        "naming the file and the key"
    )
    add("")
    if ctx.status_is_location:
        add("A file-sink project looks like this:")
        add("")
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
            "`closed/YYYY-MM/` are status folders: the frontmatter `status` mirrors the "
            "folder, every state-changing command updates both at once, and when something "
            "outside arbite breaks the pairing **the folder wins**. `wishlist/` and `plans/` "
            "are **buckets**, not statuses: a ticket filed in one is out of the status "
            "workflow (so `list next` never offers it) but keeps the status it had -- `arbite "
            "move <id> /plans` files it, `arbite move <id> /` un-files it. Filenames never "
            "change on a move, so `git log --follow` traces a ticket's whole lifecycle."
        )
        add("")
        add(
            "`raw/processed/` holds verbatim copies of captures that have since been promoted "
            "(`<id>.raw.md`, written once). A snapshot is **not** a ticket -- it keeps its "
            "original `status: raw` frontmatter -- so everything under the directory is "
            "skipped by path: invisible to `list`, `fetch`, `doctor` and the id index."
        )
        add("")
    else:
        add(
            "This project does not store tickets as files, so there is no folder to "
            "read as state and nothing for `git log --follow` to follow: the sink "
            "holds the tickets and `status` is held with them. `arbite doctor` checks "
            "the invariants that still apply (invalid field values, dependency "
            "cycles, dangling dependencies, claimed tickets with no assignee) plus "
            "its own storage-specific ones."
        )
        add("")
    add("## Configuration")
    add("")
    add("```yaml")
    add("# .arbite/project.yaml")
    add("sink: sqlite")
    add("sinks:")
    add("  file:   { root: .arbite }            # optional location overrides")
    add("  sqlite: { path: .arbite/arbite.db }")
    add("agents: [claude.haiku.001]")
    add("review: true                           # where 'arbite submit' files finished work")
    add("```")
    add("")
    add(
        "`arbite init` initialises whichever sink is selected and writes the choice into "
        "`.arbite/project.yaml` (created if missing, every other key left alone), so no "
        "later command reads a different store by accident. Creating a database-backed "
        "project is one flag, not a different command: `arbite init --sink sqlite`. An "
        "`ARBITE_SINK` selection is this-process-only and is reported rather than written."
    )
    add("")
    add("## Migrating and checking")
    add("")
    add(
        "- `arbite migrate --to <kind>` copies every ticket -- status-managed and bucketed "
        "-- preserving ids, timestamps, body, tags, dependencies, notes and buckets "
        "verbatim, leaving the source alone. `--prune` retires the source once the copy is "
        "verified; `--dry-run` reports without touching either side; `--overwrite` "
        "replaces destination tickets that share an id"
    )
    add(
        "- coordination records travel with the tickets: attempts, claims, receipts and "
        "event cursors are copied as one unit, and `migrate` refuses (exit 4, holders "
        "named) while *either* store holds a live claim or attempt"
    )
    if ctx.status_is_location:
        add(
            "- `arbite doctor` checks the invariants that mean the same thing anywhere "
            "(duplicate ids, invalid fields, dependency cycles, dangling dependencies and "
            "references, `in_progress` without an assignee) plus this sink's own: "
            "frontmatter/folder drift, a loose ticket in the arbite root, stranded temp "
            "files, closed tickets archived under the wrong month. `--fix` repairs only "
            "what is unambiguous and exits 3 while problems remain"
        )
    else:
        add(
            "- `arbite doctor` checks the invariants that mean the same thing anywhere "
            "(duplicate ids, invalid fields, dependency cycles, dangling dependencies and "
            "references, `in_progress` without an assignee) plus this sink's own: a note "
            "index drifted from the ticket body, orphaned index rows, an unexpected schema "
            "version, structural corruption. `--fix` repairs only what is unambiguous and "
            "exits 3 while problems remain"
        )
    add(
        "- keep a sqlite store out of git (it is binary and gives no readable history); "
        "`arbite init --gitignore` installs the runtime-state entries (coordination/, "
        "scratch/, streams/, any sqlite store and its sidecars) into `.gitignore` as a "
        "marked, refreshable section"
    )
    add("")
    return lines


def _topic_agents(ctx: DocContext) -> list:
    lines: list = []
    add = lines.append
    add("# Agent identity, resuming, and attempts")
    add("")
    add(
        "Agent ids are `company.model.instance`, e.g. `claude.haiku.001`; each agent has a "
        "scratchpad at `.arbite/agents/<agent_id>.md`. Know your `tier` before claiming "
        "and pass it to `arbite list next --tier <tier>` so you are only offered work you "
        "can actually do. Identity assignment, collision avoidance and liveness detection "
        "belong to the agent harness, not arbite -- arbite only reads a static `agents:` "
        "list to pre-create scratchpads."
    )
    add("")
    if ctx.status_is_location:
        add(
            "To resume: check your scratchpad for a last-known ticket id, then verify that "
            "ticket is still where a claimed ticket belongs -- in `in_progress/`, with "
            "`assignee` matching your own id (`arbite show <id> --json` prints its path). "
            "The location is ground truth, the scratchpad only a hint; if it is missing, "
            "stale or mismatched, find a ticket assigned to you with `arbite list "
            "--assignee <your-id>`."
        )
    else:
        add(
            "To resume: check your scratchpad for a last-known ticket id, then verify that "
            "ticket is still `status: in_progress` with `assignee` matching your own id "
            "(`arbite show <id> --json`). That is ground truth, the scratchpad only a "
            "hint; if it is missing, stale or mismatched, find a ticket assigned to you "
            "with `arbite list --assignee <your-id>`."
        )
    add("")
    add("## Work attempts")
    add("")
    add(
        "Claiming (directly, through `list next --claim`, or with `promote --agent`) "
        "records the *work attempt* that owns the work and prints it as `attempt: "
        "att-XXXX`: every later `arbite file` command presents that id, so keep it. "
        "Acquisition checks the rules a queue only filters on -- every `depends_on` "
        "closed, the classification real (no `TODO:` placeholders), the ticket `open` and "
        "unclaimed, no active attempt already -- so naming a ticket directly cannot bypass "
        "them."
    )
    add("")
    add(
        "`--force` is the administrative takeover and needs `--reason`: it ends the "
        "holder's attempt as `interrupted`, records the revocation, and starts a fresh "
        "attempt for you. Work that was already `in_progress` before arbite tracked "
        "attempts is adopted with `arbite attempt adopt <id> --agent <your-id>`. Release, "
        "block, shelve and reopen end the attempt they stop, and reopening a ticket other "
        "work depends on flags the running dependents instead of undoing their work."
    )
    add("")
    return lines


def _topic_triage(ctx: DocContext) -> list:
    lines: list = []
    add = lines.append
    add("# Triage: raw tickets, wishes and filing")
    add("")
    add(
        f"A **raw** ticket (`arbite raw <{'|'.join(RAW_TYPE_CHOICES)}> <message>`) is a "
        f"deliberately unclassified quick capture: status `raw`, never returned by `arbite "
        f"list next`, carrying only `type` plus a placeholder title, auto-grouped under "
        f"the `{CLASSIFICATION_EPIC}` epic (find them with `arbite list next --epic "
        f"{CLASSIFICATION_EPIC}`). Its body lists what triage must fill in -- a real title, "
        f"`tier`, `domain`, a real `epic`, `priority`, an expanded description -- before "
        f"it can be claimed. Raw tickets exist so a thought isn't lost, not as work: "
        f"classify them before picking them up."
    )
    add("")
    add(
        "`arbite list raw` prints the whole raw backlog as a todo list (grouped by type, "
        "one line per ticket, oldest first). `arbite fetch [type]` pulls the single oldest "
        "raw ticket and prints it like `show`, with a `derived_note` telling you to "
        "classify it -- it never writes anything."
    )
    add("")
    add(
        "**`arbite promote <id>`** is the write half: it classifies a raw ticket in one "
        "command. It snapshots the capture verbatim to `raw/processed/<id>.raw.md` first "
        "(exclusive create, so promoting the same id twice is an error), then writes the "
        "required `--title`/`--tier`/`--domain` plus `--epic`/`--priority`/`--description`/"
        "`--tags`, **in place** through a compare-and-swap -- the id, `created` and any "
        "notes carry over, and an omitted `--epic` clears the `classification` grouping. "
        "It lands at `open`, or at `in_progress` assigned to `--agent <id>` when you are "
        "about to work it; an already-classified ticket is refused."
    )
    add("")
    add(
        "A **wish** (`arbite wish <message>`) is a wishlist item, not work: `promote` "
        "retypes it to `feature`, applies the classification and files it in the wishlist "
        "bucket, leaving its status at `raw` so it leaves the triage queue without "
        "becoming work (`--agent` is refused). A **request** is a tweak or lateral change "
        "to something that already exists -- ordinary work once classified, so keep the "
        "type and promote it like any other raw ticket. A **memo** asks for project "
        "notes/docs rather than a code change."
    )
    add("")
    add(
        "Any command that takes a ticket id accepts an exact id or any unique wildcard "
        "(substring) match: 'f6' resolves to tic-f607, and an exact id always wins. A "
        "command that modifies a ticket treats an ambiguous match as an error listing the "
        "candidates; read-only `show` and `deps` take the first alphabetically instead."
    )
    add("")
    return lines


def _topic_conventions(ctx: DocContext) -> list:
    lines: list = []
    add = lines.append
    add("# Conventions (scripts and agent loops)")
    add("")
    add(
        "**Ticket ids.** An id argument is an exact id or any unique wildcard (substring) "
        "match: 'f6' resolves to tic-f607, and an exact id always wins. A command that "
        "modifies a ticket treats an ambiguous match as an error listing the candidates; "
        "read-only `show` and `deps` take the first alphabetically instead."
    )
    add("")
    add(
        "**Exit codes**, so a shell loop can branch without matching on message text: 0 "
        "success with results; 1 error (bad arguments, ambiguous ticket id, a refused "
        "path, a claim naming the wrong attempt); 2 the query ran fine but matched nothing "
        "(e.g. no workable ticket right now); 3 `arbite doctor` found integrity problems; "
        "4 busy -- a live claim or attempt holds it and **nothing changed**, so pick other "
        "work rather than retrying; 5 stale -- a token, digest or generation is no longer "
        "current and **nothing changed**, so re-read and retry. `arbite cmd` is the one "
        "exception: it returns the wrapped command's own code."
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
        "`show`, `search`, `deps`, `doctor`, `sink`, `status` and `delete` emits "
        "machine-readable output whose field names match the frontmatter; the human table "
        "format is not a stable interface. The `path` field is whatever the sink calls a "
        "ticket's location."
    )
    add("")
    add(
        "**Claim in one step.** `arbite list next --claim <agent_id>` selects the most "
        "urgent workable ticket *and* claims it in the same write; `arbite claim <id> "
        "--agent <agent_id>` does the same for a ticket you already know. Either way the "
        "claim sets `status: in_progress` and the assignee together, so never follow a "
        "claim with a separate `set status` -- and prefer `--claim` to running `list next` "
        "then `claim`, because between those two commands another agent can take the "
        "ticket. A claim is a compare-and-swap: it fails if the ticket is already assigned "
        "to someone else unless you pass `--force`, and it refuses to write over a ticket "
        "that changed since it was read."
    )
    add("")
    add(
        "**Pull a batch with `--count N`.** `arbite list next --count 3` returns the 3 "
        "most urgent workable tickets instead of 1, so a dispatcher can fan work out to "
        "several agents in one query; adding `--claim <agent_id>` claims up to N of them. "
        "Each claim is individually compared-and-swapped, so if another agent races you "
        "for one the rest still succeed -- a short batch is a real result, and the count "
        "actually claimed is reported on stderr. `--count` also caps a plain `list`, "
        "`--topo` (rows) or `--tree` (top-level roots, never truncating a subtree)."
    )
    add("")
    add(
        "**Removing a ticket is not the same as finishing it.** `arbite close` moves a "
        "ticket out of the work queues and keeps it forever; `arbite delete <id> --force` "
        "destroys it, records a note saying so first, and is deliberately gated. Prefer "
        "`close`."
    )
    add("")
    return lines


def _topic_workspace(ctx: DocContext) -> list:
    lines: list = []
    add = lines.append
    if not ctx.has_proxy:
        add("# Changing files")
        add("")
        add(
            f"**Not available in this project:** the active `{ctx.sink_kind}` sink has no "
            "coordination backend, so `arbite file ...`, `arbite cmd`, `arbite receipt`, "
            "`arbite changes`, `arbite events`, `arbite stream`, `arbite workspace` and "
            "`arbite attempt` refuse rather than pretend (exit 1, naming the sink). "
            "Everything outside the proxy -- tickets, statuses, the workflow -- works "
            "normally."
        )
        add("")
        return lines
    heading = "# The file proxy and the record"
    if ctx.sink_root:
        heading += f" (sink: `{ctx.sink_kind}`)"
    add(heading)
    add("")
    add(
        "`arbite file` is the proxy for changing a file another agent may also touch: "
        "claim the paths, read for a version token, mutate under that token, then read the "
        "record back. **Prefer it to a shell write** for any file in this workspace -- "
        "only the proxy records the change against a ticket and an attempt, and `arbite "
        "changes <id>` / `arbite receipt <op>` read that record back. A shell write is not "
        "blocked; it is unattributed drift that the next read reports as an external edit."
    )
    add("")
    add("## Claim, read, mutate, release")
    add("")
    add(
        "The order the proxy exists to enforce: a claim is exclusive writer ownership of a "
        "path, a read under that claim returns a token, and a mutation presents the token."
    )
    add("")
    add(
        "- claim before you write: `arbite file claim PATH... --ticket T --attempt A` is "
        "all-or-nothing in canonical path order, so two agents can never hold half of a "
        "pair; a path another attempt holds refuses the whole request with exit 4 and "
        "names the holder"
    )
    add(
        "- read for a token: `arbite file read PATH --ticket T --attempt A` serves the "
        "bytes and records a token (`op-XXXX`); one token authorises exactly one mutation. "
        "`--lines START[:END]` serves a range, and the digest still covers the whole file"
    )
    add(
        "- mutate under that token: `arbite file write` (a whole file, from `--input NAME` "
        "in `.arbite/scratch/` or stdin), `arbite file edit` (an ordered batch of exact "
        "substitutions, all-or-nothing), `arbite file remove`, `arbite file rename` (both "
        "paths at one generation). A stale token, moved bytes, a revoked generation or a "
        "closed ticket exits 5 and changes no bytes"
    )
    add(
        "- hand a claim back without touching the bytes: `arbite file release PATH... "
        "--ticket T --attempt A --reason TEXT`, so the next worker knows what to re-read"
    )
    add(
        "- `arbite file claims [--all]` reports what is held right now (or a path's "
        "history); `arbite file list` / `arbite file search` are bounded, canonical-order "
        "listings that never authorise a write"
    )
    add("")
    add("## Keeping your habits: arbite cmd")
    add("")
    add(
        "`arbite cmd [--ticket T --attempt A] [--shell] -- CMD...` runs a familiar tool "
        "(`grep`, `sed`, `mv`) and records what it *observed* it change: a digest manifest "
        "of the managed paths before and after, a receipt and a `passthrough.changed` event "
        "per path that differed, one `passthrough.exec` event for the run -- and **no "
        "exclusivity claimed**, because nothing was acquired."
    )
    add("")
    add(
        "`--claim PATH...` is guarded mode instead: those paths are acquired "
        "all-or-nothing *before* the command starts (a busy path refuses the whole run, "
        "exit 125, and nothing starts), every change is verified against the claimed set "
        "afterwards (a change outside it is reported as `unclaimed_write` and left exactly "
        "where the tool put it -- arbite does not roll back a command it did not perform), "
        "and the claims are released when the run ends, including when the tool fails. "
        "The command's own exit code is what you get back; arbite's own refusals are 125 "
        "(refused before running), 126 (an invocation arbite does not support) and 127 "
        "(the tool is not on PATH)."
    )
    add("")
    add("## Reading the record")
    add("")
    add(
        "- `arbite changes <id>` -- one row per path, from the version the first operation "
        "found to the version the last one left; `--all` adds the ordered operation log, "
        "where a change that was later reverted stays visible"
    )
    add(
        "- `arbite receipt <op>` -- one operation's evidence: the version it replaced, the "
        "version it wrote, where those bytes are kept, all read back and proved against "
        "the digests the receipt records before anything prints. Missing evidence is "
        "refused, not glossed over"
    )
    add(
        "- `arbite receipt --summary [--ticket T]` -- every operation in log order with "
        "both versions and who did it, plus what the store still holds. Take this *before* "
        "anything is pruned: the coordination store is ignored by git and dies with the "
        "machine, so this is the export a devlog is written from"
    )
    add(
        "- `arbite events [--tail N] [--after CURSOR] [--include-reads]` -- the "
        "coordination event stream, one line per event, in cursor order; reads are "
        "excluded unless asked for, and `--follow` is refused because arbite never blocks "
        "-- poll with `--after <cursor>`, where 'nothing new' is exit 2"
    )
    add(
        "- `arbite stream write <id> -` / `arbite stream read <id> [--after SEQ | --tail "
        "N]` -- the per-ticket narration a worker writes as it goes, one record per line; "
        "poll with `--after`, where 'nothing new' is exit 2. This is per-ticket prose, "
        "distinct from `arbite events`, which is the coordination fact stream"
    )
    add(
        "- `arbite scratch list` / `arbite scratch clear [NAME...] [--all]` -- what is "
        "staged as the payload of a write or an edit, and how to empty it deliberately"
    )
    add(
        "- `arbite attempt adopt <id> --agent <your-id>` -- record an attempt for work "
        "that was already `in_progress` when attempt tracking began"
    )
    add("")
    return lines


def _topic_limits(ctx: DocContext) -> list:
    lines: list = []
    add = lines.append
    add("# What arbite does not promise")
    add("")
    add(
        "- **A shell can bypass it.** arbite does not intercept the filesystem: `sed -i`, "
        "`> file`, or an editor writes bytes arbite never sees. Those bytes are *drift*, "
        "reported by the next read as an external edit attributable to no ticket, and "
        "**arbite cannot prove who wrote a file** -- it records attribution (the agent id "
        "a command carried, the actor in an event), never authentication"
    )
    add(
        "- **Observation is not exclusivity.** `arbite cmd` observed mode claims nothing; "
        "guarded mode claims before the run and verifies after it, but cannot stop a write "
        "*during* it, so a takeover mid-run or a foreign writer is reported rather than "
        "prevented"
    )
    add(
        "- **Reads are not isolated.** A read can observe a commit in flight; the file "
        "sink reports that as a `pending_commit` finding instead of hiding it"
    )
    add(
        "- **No daemon, no launcher, no automatic recovery.** Nothing runs in the "
        "background, a lock dies with its process, a killed guarded run leaves its claim "
        "for `arbite doctor` (or `--fix`) to release once the attempt is over, and there "
        "is no worktree workflow, no central database, no factory and no dashboard"
    )
    add(
        "- **Evidence is never pruned.** There is no retention policy or garbage "
        "collection yet, so the store grows with the work (each version is kept once, by "
        "digest, and one version may be at most 64 MiB); `arbite receipt --summary` prints "
        "the pre-pruning export a devlog wants"
    )
    if ctx.sink_kind == "sqlite":
        add(
            "- **This sink keeps evidence in the database.** The `sqlite` store holds the "
            "coordination records *and* the artifact bytes in the ticket database, so the "
            "database is one thing to back up but grows with every mutation; `doctor` adds "
            "its storage-specific findings (orphaned revision rows) to the shared ones"
        )
    else:
        add(
            "- **This sink keeps evidence beside the tickets.** The file sink holds "
            "coordination state in `.arbite/coordination/` and artifact bytes as files "
            "under it (both ignored by git), so evidence is local to this checkout; "
            "`doctor` adds its storage-specific findings (a pending commit journal, "
            "orphan revision counters) to the shared ones"
        )
    add(
        "- **Exit 4 and 5 mean nothing changed**, so neither is worth retrying blind: 4 "
        "is busy (a live claim or attempt holds the path), 5 is stale (a token, digest or "
        "generation is no longer current)"
    )
    add("")
    return lines


def _topic_design(ctx: DocContext) -> list:
    lines: list = []
    add = lines.append
    add("# Why arbite is built this way")
    add("")
    add(
        "Agent-driven work tends to leave state in places that don't survive a new "
        "session: an agent's own memory file, a chat transcript, a stale assumption about "
        "what it was doing. Since the repo is already the durable, shared, versioned "
        "medium, arbite puts the tickets there and makes one location the single source of "
        "truth for a ticket's state -- any other record is a *hint* to verify against the "
        "ticket, never authority. Two constraints shaped most of the design: **agents, not "
        "humans, are the primary callers** (every query that matters has a `--json` mode "
        "with stable field names, and exit codes are meaningful), and **coordinates must "
        "be honest** (claiming is a compare-and-swap, and an ambiguous id refuses instead "
        "of guessing)."
    )
    add("")
    add("## Goals")
    add("")
    add(
        "- **Zero infrastructure.** No daemon, no lock service, no server. The default "
        "sink is a directory of files; the alternative is one SQLite file the stdlib "
        "already knows how to open. A clone of the repo is a complete working instance"
    )
    add(
        "- **One storage interface, two implementations.** Commands talk to a sink, not "
        "to files or tables, so claiming, readiness, ordering and exit codes are identical "
        "whichever store is in use"
    )
    add(
        "- **`ls` as a status board.** With the file sink, what is actionable is legible "
        "from folder names alone, without opening any file"
    )
    add(
        "- **Safe under concurrency.** Claiming is a compare-and-swap on both sinks; races "
        "resolve to exactly one winner, and a lost race is reported, not silently "
        "overwritten"
    )
    add(
        "- **Machine-first interfaces.** JSON output for every read query, distinct exit "
        "codes for success / error / empty / integrity-problems"
    )
    add(
        "- **Recoverable history.** Plain files plus git, with stable filenames so `git "
        "log --follow` traces a ticket's whole lifecycle; no proprietary format"
    )
    add("")
    add("## Non-goals (explicitly out of scope)")
    add("")
    add(
        "- **Agent identity, liveness and staleness detection** -- these belong to an "
        "external agent harness; arbite only reads a static `agents:` list"
    )
    add(
        "- **Scheduling or dispatching work** -- arbite answers \"what is workable\"; "
        "getting an agent started on it is the caller's job"
    )
    add("- **A UI, web service, or notifications** -- the CLI is the interface")
    add(
        "- **Enforcing policy beyond data integrity** -- arbite refuses to corrupt state "
        "and reports drift; it does not police who may do what"
    )
    add(
        "- **A distributed store** -- each sink is a single local store; across machines "
        "the repo (or the file) is the unit of exchange"
    )
    add("")
    add("## Upgrading (hard cuts, no fallback)")
    add("")
    add(
        "- **The project config is `.arbite/project.yaml`.** The old `arbite.yaml` and "
        "`.arbite.yaml` are not read at all: a project that still has only one behaves as "
        "if it had no config, silently falling back to the default `file` sink. Move the "
        "file into `.arbite/project.yaml`, or let `arbite init --sink <kind>` write it "
        "fresh"
    )
    add(
        "- **`arbite reopen` requires `--reason`.** It is the rejection path out of "
        "review, so a rejection with no stated reason is useless to whoever acts on it"
    )
    add(
        "- The default non-status bucket is `plans/` (the old `planning/` is not aliased), "
        "and `review` is a first-class status reached through `arbite submit` and left "
        "through `arbite accept` (or `arbite reopen --reason`)"
    )
    add("")
    return lines


@dataclass(frozen=True)
class _Topic:
    """One entry in the registry: a name, a one-line summary for the index, the section
    it groups under, and the renderer that produces its markdown lines."""

    name: str
    summary: str
    section: str
    render: Callable[["DocContext"], list]


#: The topic registry. `overview` is the default (`arbite docs`); `commands` is
#: reserved and handled separately because it renders from the parser rather than from
#: prose. The order here is the order `arbite docs list` and `arbite docs all` use.
TOPICS = (
    _Topic("overview", "what arbite is, and the commands you will type", "Start here",
           _topic_overview),
    _Topic("workflow", "statuses and the transitions between them", "Concepts",
           _topic_workflow),
    _Topic("fields", "every ticket field, and the independent axes", "Concepts",
           _topic_fields),
    _Topic("sinks", "where tickets live: file vs sqlite, selection, config, migration",
           "Concepts", _topic_sinks),
    _Topic("agents", "agent ids, tiers, scratchpads, resuming and work attempts",
           "Concepts", _topic_agents),
    _Topic("triage", "raw captures, wishes, fetch and promote", "Concepts", _topic_triage),
    _Topic("conventions", "ticket ids, exit codes, --json and atomic claims", "Concepts",
           _topic_conventions),
    _Topic("workspace", "the file proxy, arbite cmd, and the recorded evidence",
           "Workspace", _topic_workspace),
    _Topic("limits", "what arbite does not promise", "Workspace", _topic_limits),
    _Topic("design", "why arbite is built this way, goals and non-goals", "Background",
           _topic_design),
)

TOPIC_BY_NAME = {topic.name: topic for topic in TOPICS}
TOPIC_NAMES = tuple(topic.name for topic in TOPICS)
#: Names the docs command itself owns; they are not topics and are dispatched first.
RESERVED = ("list", "all", "search", "commands")


def _join(lines) -> str:
    return _ANSI_RE.sub("", "\n".join(lines)) + "\n"


def render_overview(ctx: DocContext) -> str:
    return _join(_topic_overview(ctx))


def render_topic(ctx: DocContext, name: str) -> str:
    """One topic by name. `overview` and the registry names resolve here; `commands`
    is dispatched by the caller (`render_commands`), and an unknown name raises
    `KeyError` for the CLI to turn into a listing."""
    if name in (None, "", "overview"):
        return render_overview(ctx)
    topic = TOPIC_BY_NAME.get(name)
    if topic is None:
        raise KeyError(name)
    return _join(topic.render(ctx))


def render_index(ctx: DocContext) -> str:
    lines: list = []
    add = lines.append
    add("# arbite docs -- topics")
    add("")
    add("Fetch one with `arbite docs <topic>`; fetch a command's flags with `arbite "
        "docs commands <name>`.")
    add("")
    sections: dict = {}
    for topic in TOPICS:
        sections.setdefault(topic.section, []).append(topic)
    for section, topics in sections.items():
        add(f"## {section}")
        add("")
        for topic in topics:
            add(f"- `{topic.name}` -- {topic.summary}")
        add("")
    add("## Reference")
    add("")
    add("- `commands [NAME]` -- usage and every flag for a command, or the index of all")
    add("")
    add("Also: `arbite docs all` (every topic in one stream), `arbite docs search TERM`.")
    add("")
    return _join(lines)


def render_all(ctx: DocContext) -> str:
    parts = [render_overview(ctx)]
    for topic in TOPICS:
        if topic.name == "overview":
            continue
        parts.append(_join(topic.render(ctx)))
    parts.append(render_commands(ctx))
    return "\n".join(parts)


def render_commands(ctx: DocContext, name: Optional[str] = None) -> str:
    """The command reference, rendered from the live parser.

    With no name: every command and the parser's own one-line purpose. With a name:
    that command's usage line and every flag, plus a block per nested sub-command
    (`arbite file edit`), so a group is documented in its own right rather than
    collapsed into `{claim,read,write,...}`. An unknown name raises `KeyError`."""
    if name is None:
        add = [
            "# Commands",
            "",
            "Every command, with the parser's own one-line purpose. "
            "`arbite docs commands <name>` adds its usage and every flag; "
            "`arbite <name> -h` prints the full help.",
            "",
        ]
        add.extend(_command_index_lines(ctx.parser))
        add.append("")
        return _join(add)
    subparser = ctx.subparsers_by_name.get(name)
    if subparser is None:
        raise KeyError(name)
    add = [f"# `arbite {name}`", ""]
    add.extend(_command_block_lines(f"arbite {name}", subparser, _JSON_STUB))
    for child_name, child in _nested_choices(subparser):
        add.extend(_command_block_lines(f"arbite {name} {child_name}", child, _JSON_STUB))
    return _join(add)


def topic_index() -> list:
    """`[{name, summary, section}]` for the JSON form of `arbite docs list`."""
    return [
        {"name": topic.name, "summary": topic.summary, "section": topic.section}
        for topic in TOPICS
    ]


def search(ctx: DocContext, term: str) -> dict:
    """Case-insensitive search across topic bodies plus the command index.

    Returns `{term, matches: [{topic, summary, lines}]}` so the CLI can print either a
    readable report or JSON. Bodies are rendered through the same context, so the
    hits match the sink-specific text the reader would actually see."""
    needle = term.casefold()
    matches = []
    for topic in TOPICS:
        lines = [line for line in topic.render(ctx) if needle in line.casefold()]
        if lines or needle in topic.name.casefold() or needle in topic.summary.casefold():
            matches.append({"topic": topic.name, "summary": topic.summary, "lines": lines[:8]})
    command_lines = [
        line for line in _command_index_lines(ctx.parser) if needle in line.casefold()
    ]
    if command_lines:
        matches.append(
            {"topic": "commands", "summary": "the command index", "lines": command_lines[:12]}
        )
    return {"term": term, "matches": matches}
