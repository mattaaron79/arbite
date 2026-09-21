"""arbite CLI: argument parsing and command dispatch.

Every command here talks to a *sink* -- the object resolved by
`config.open_sink()` -- and never to storage directly. That is the whole point of
the split: no command knows whether a ticket is a markdown file in a status folder
or a row in a SQLite database, so `arbite list next` means the same thing in both,
and the file sink is free to keep relocating files on a status change without any
of this code being aware of it.

Commands therefore work in terms of ticks and queries, never paths:

- read a ticket with `sink.get(id)`, a set with `sink.query(TicketQuery(...))`;
- change one by mutating the Ticket and calling `sink.update(...)`, passing an
  `Expect` built from what was just read so a concurrent change is reported as a
  conflict instead of being silently overwritten;
- ask the sink where a ticket is (`sink.location(id)`) only when a human needs to
  be told, and treat that string as opaque.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from . import __version__, config, docs, graph, schema
from .coordination import app as coordination_app
from .coordination import claims as coordination_claims
from .coordination import lifecycle as coordination_lifecycle
from .coordination import recovery as coordination_recovery
from .coordination import results as outcomes
from .coordination.scratch import ensure_scratch_dir, note_lines, scratch_summary
from .errors import ArbiteError, Busy, Conflict, NotReady, TicketError
from .query import TicketQuery, TextMatch, apply_limit, resolve_terms
from .schema import CLASSIFICATION_EPIC, STATUSES, TIERS, Ticket
from .sinks import (
    RAW_PROCESSED_DIR,
    SINK_KINDS,
    Expect,
    SinkInfo,
    build_sink,
    count_by_status,
    missing_references,
    raw_snapshot_name,
    reference_path,
    write_exclusive,
)

# Exit codes. Agents drive arbite from shell loops, so "nothing matched" has to
# be distinguishable from "worked fine" and from "broke" without parsing
# stdout: 0 = success with results, 1 = error, 2 = query ran but matched
# nothing, 3 = `doctor` found integrity problems, 4 = busy (a live claim or
# attempt holds it; nothing changed), 5 = stale (a token, digest or generation is
# no longer current; nothing changed). 4 and 5 are the coordination vocabulary's
# additions -- they need a different caller response from a genuine error -- and
# they are named here from `coordination.results` so the codes, the labels and the
# `next:` hints cannot drift apart.
EXIT_OK = outcomes.EXIT_OK
EXIT_ERROR = outcomes.EXIT_ERROR
EXIT_EMPTY = outcomes.EXIT_EMPTY
EXIT_PROBLEMS = outcomes.EXIT_PROBLEMS
EXIT_BUSY = outcomes.EXIT_BUSY
EXIT_STALE = outcomes.EXIT_STALE


def _split_csv(value):
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _status_list(value):
    """argparse type for --status: a single status or a comma-separated list of
    them, e.g. 'open' or 'open,in_progress,review'. Validated here so an unknown status
    is an argparse error (exit 2) naming the valid values, rather than a filter
    that silently matches nothing. The result is always a list, and an empty
    value means 'no status filter'."""
    statuses = _split_csv(value)
    unknown = [s for s in statuses if s not in STATUSES]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"invalid status '{unknown[0]}' (valid: {', '.join(STATUSES)})"
        )
    return statuses


def _json_flag(parser):
    parser.add_argument("--json", action="store_true", help=docs.JSON_HELP)


def _sink_flag(parser, suppress=True):
    """The global --sink selector.

    On subparsers the default is suppressed so that `arbite --sink sqlite list`
    is not overwritten by the subparser's own default -- argparse stores the
    subparser's value last, so a plain `default=None` there would silently drop
    the flag the user gave before the command name."""
    parser.add_argument(
        "--sink",
        metavar="KIND",
        choices=SINK_KINDS,
        default=argparse.SUPPRESS if suppress else None,
        help=f"which storage to use for this command: {', '.join(SINK_KINDS)} "
        "(default: the 'sink:' key in .arbite/project.yaml, else file)",
    )


def _print_json(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _emit_tickets(rows, as_json, sink):
    """Emit a ticket list as JSON or a table, exiting EXIT_EMPTY when empty so a
    caller can branch on 'nothing to do' without matching on message text."""
    if as_json:
        locations = sink.location_map(rows)
        _print_json([t.to_dict(locations.get(t.id)) for t in rows])
    elif rows:
        _print_flat(rows)
    else:
        print("no tickets found")
    if not rows:
        sys.exit(EXIT_EMPTY)


# Shared help for commands that take a single ticket id: searches are wildcard
# (substring) matches, so 'tic-' can be skipped entirely and e.g. 'f6' resolves
# to tic-f607. The strings themselves live in docs.py (imported here) so that
# .arbite/AGENTS.md can recognise this boilerplate and collapse it into a single
# cross-reference instead of repeating it once per command.
TICKET_ID_HELP = docs.TICKET_ID_HELP

# Read-only commands keep the old convenience: a guess there costs nothing.
TICKET_ID_HELP_READONLY = docs.TICKET_ID_HELP_READONLY

# The bucket a reclassified wish is filed in: a folder in the file sink, a recorded
# bucket in SQLite. `arbite init` creates it, `arbite move <id> /wishlist` files a
# ticket there by hand, and `arbite promote` is the command that does it as part of
# classifying a wish.
WISHLIST_BUCKET = "wishlist"

# `arbite bug|feature|request|memo|wish <message>` is shorthand for the
# equally-named `arbite raw <type> <message>` form: they create an identical raw
# ticket (same status 'raw', same 'classification' epic, same body notes), just
# with a shorter invocation. Each top-level subcommand is registered in
# build_parser() from this map so a single type can't drift from its shortcut.
RAW_SHORTCUT_HELP = {
    "bug": "capture a raw bug ticket (shorthand for 'arbite raw bug <message>')",
    "feature": "capture a raw feature ticket (shorthand for 'arbite raw feature <message>')",
    "request": "capture a raw change-request ticket (shorthand for 'arbite raw request <message>')",
    "memo": "capture a raw memo ticket (shorthand for 'arbite raw memo <message>')",
    "wish": "capture a raw wish ticket (shorthand for 'arbite raw wish <message>')",
}


def _cwd_sink(args):
    """The sink for a command that works on the current directory rather than on
    an existing project (`arbite init`), so it never inherits a location from a
    parent directory's config."""
    project_root = Path.cwd()
    spec = config.sink_spec(getattr(args, "sink", None), project_root)
    return spec, project_root


def _require_sink(args):
    """The configured sink, or a clear instruction to initialise one."""
    sink = config.open_sink(getattr(args, "sink", None))
    _warn_about_an_unused_database(args, sink)
    return sink


def _warn_about_an_unused_database(args, sink) -> None:
    """Warn when this command quietly used the file sink in a project that has a
    database nobody selected.

    Creating a database-backed project is `init --sink sqlite` or a `sink:` key,
    and the flag is deliberately per-command -- so a project can end up with
    `.arbite/arbite.db` full of tickets while `arbite list` (no flag, no config)
    reads an empty file store instead. Nothing about that failure is visible: the
    command succeeds and reports no tickets. This says so on stderr, once per
    command, and only in that exact ambiguous case: an explicit `--sink` or
    `ARBITE_SINK` is a decision, not a mistake."""
    if sink.kind != config.DEFAULT_SINK_KIND:
        return
    if getattr(args, "sink", None) or os.environ.get(config.ENV_SINK):
        return
    project_root = config.find_project_root()
    if config.load_config(project_root).get("sink"):
        return
    database = config.default_location("sqlite", project_root / config.ARBITE_DIRNAME)
    if database.exists():
        print(
            f"note: {database} exists but no sink is configured, so this command used "
            f"the '{config.DEFAULT_SINK_KIND}' sink instead -- pass --sink sqlite, set "
            f"{config.ENV_SINK}=sqlite, or add 'sink: sqlite' to .arbite/project.yaml",
            file=sys.stderr,
        )


def _store_source(args, project_root) -> str:
    """Where this command's sink selection came from, in words.

    A fact the workspace report states plainly, because "which store am I looking
    at" is the question the whole sink design exists to keep answerable: an
    explicit flag or an environment override is a per-invocation decision, a
    `sink:` key in the committed config is the project's, and neither is the same
    as the default."""
    kind = config.sink_spec(getattr(args, "sink", None), project_root).kind
    if getattr(args, "sink", None):
        return f"--sink {kind}"
    if os.environ.get(config.ENV_SINK):
        return f"{config.ENV_SINK}={kind}"
    if config.load_config(project_root).get("sink"):
        return f"sink: {kind} in {config.ARBITE_DIRNAME}/{config.CONFIG_FILENAME}"
    return (
        f"the default for this project -- no 'sink:' key in "
        f"{config.ARBITE_DIRNAME}/{config.CONFIG_FILENAME}"
    )


def _coordination_app(args, sink, project_root=None):
    """The application layer for this command's sink and project.

    Coordination policy lives there rather than here: this function only assembles
    the facts the layer needs (the located project root, the arbite directory, the
    resolved sink and where that selection came from) and nothing about claims,
    attempts or staleness is decided in argparse."""
    project_root = project_root or config.find_project_root()
    return coordination_app.CoordinationApp.open(
        sink,
        project_root,
        project_root / config.ARBITE_DIRNAME,
        store_source=_store_source(args, project_root),
    )


def _lifecycle(args, sink):
    """The ticket lifecycle operations for this command's sink and coordination store.

    Every command that acquires, ends or guards a *ticket transition* goes through
    this, so the rules -- readiness, one active attempt per ticket, an override that
    needs a reason -- cannot differ between `claim`, `list next --claim`, `set` and
    the rest. Nothing about them is decided here."""
    return coordination_lifecycle.TicketLifecycle(sink, _coordination_app(args, sink))


def _claims(args, sink):
    """The file-claim operations for this command's sink and coordination store.

    Claims are the durable half of the file proxy: what C06's reads, C07's writes and
    the rename path are all checked against later. The rules -- canonical paths,
    all-or-nothing acquisition, generation revocation -- live in
    `coordination.claims`, so every surface that acquires or releases a path goes
    through the same ones."""
    return coordination_claims.FileClaims(sink, _lifecycle(args, sink))


def _emit_file_result(result, as_json):
    """Print a file operation's result, and exit with the code its outcome carries.

    A refusal goes to **stderr** with the word its outcome prints (`busy:`,
    `stale_read:`), because that is where the exit-code vocabulary has always been
    reported and where a caller reading a single stream finds the branch it has to
    take; a success goes to stdout. `--json` is the machine's form and carries the
    same facts, so it is printed wherever it is asked for."""
    if as_json:
        _print_json(result.to_json())
    elif result.kind == outcomes.OK:
        print(result.to_text())
    else:
        print(result.to_text(), file=sys.stderr)
    if result.exit_code:
        sys.exit(result.exit_code)


def _all_tickets(sink):
    """Every ticket, bucketed ones included.

    Readiness is a property of the whole set, never of a filtered subset: a blocker
    parked in the wishlist still blocks, so the acquisition paths judge against this
    list rather than against the tickets their own filters happen to select."""
    return sink.query(TicketQuery(buckets=("*",)))


#: The commands this CLI defines, cached after the first look (`known_commands`).
_KNOWN_COMMANDS: Optional[set] = None


def known_commands() -> set:
    """Every command path this parser defines, as `('command', 'command subcommand', ...)`.

    Output may only name commands that exist: `doctor --fix` tells a human what to run next,
    and the receipt and change views it points at are tic-7c42's while scratch clearing is
    tic-95c0's. Asking the parser -- rather than keeping a list here -- is what keeps those
    sentences true in the build they are printed from, and lets them name the command the
    moment the slice that owns it lands."""
    global _KNOWN_COMMANDS
    if _KNOWN_COMMANDS is None:
        _, choices = build_parser()
        found = set()
        for name, sub in choices.items():
            found.add(name)
            found.update(f"{name} {inner}" for inner in _nested_choices(sub))
        _KNOWN_COMMANDS = found
    return _KNOWN_COMMANDS


def _nested_choices(parser) -> set:
    """The subcommand names a parser really has, read from its own action rather than
    guessed from a naming convention."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def knows_command(path: str) -> bool:
    """Whether `path` (e.g. `"receipt"`, `"scratch clear"`) is a command this arbite has."""
    return path in known_commands()


def _emit_result(result, as_json):
    """Print an application-layer result: JSON when asked, otherwise its lines.

    A result carries its own `next:` line, which is what lets a *successful* command
    hand the caller the step that follows -- claiming a ticket names the attempt a
    file command needs -- without the CLI having to know why."""
    if as_json:
        _print_json(result.to_json())
    else:
        print(result.to_text())


def _has_tickets(sink) -> bool:
    """Whether a store holds anything, tolerating one that was never created."""
    try:
        return bool(sink.ids())
    except ArbiteError:
        return False


def _find_stale_store(just_created, active, arbite_dir):
    """A store in this project that holds tickets and that nothing selects, if any.

    This is the state a deleted `sink:` key, an `ARBITE_SINK` one-off or an
    unfinished migration leaves behind, and from the outside it is indistinguishable
    from an empty backlog -- so the guide an agent follows calls it out in bold
    rather than quietly describing only the store that happens to be selected."""
    candidates = [just_created]
    for kind in SINK_KINDS:
        if kind != active.kind:
            candidates.append(build_sink(config.SinkSpec(kind=kind), arbite_dir))
    for candidate in candidates:
        if candidate.kind != active.kind and _has_tickets(candidate):
            return _describe_safely(candidate)
    return None


def _describe_safely(sink) -> SinkInfo:
    """A sink's `SinkInfo`, tolerating a store that does not exist yet.

    `arbite init` describes the *other* sink too -- the one a plain command will
    read -- and that store may never have been created. Capabilities are known from
    the class either way; only the counts are unavailable."""
    try:
        return sink.describe()
    except ArbiteError:
        return SinkInfo(
            kind=sink.kind,
            root=str(sink.root),
            status_is_location=sink.status_is_location,
            supports_buckets=sink.supports_buckets,
        )


def _expect_from(ticket: Ticket) -> Expect:
    """A compare-and-swap token for the exact state just read.

    Every mutating command writes through this, so if anything changed the ticket
    between the read and the write -- another agent claimed it, someone else
    closed it -- the write is refused with a Conflict instead of silently
    discarding that change. Commands that edit prose without changing state
    (`note`, `set` of a non-status field) still use it; only `move_to_bucket`
    does not, because filing is not a state change."""
    return Expect(status=ticket.status, assignee=ticket.assignee)


def _root_relative_segments(value: str) -> list:
    """The segments of a root-relative arbite path, an optional leading '/' ignored.

    The one definition of what a root-relative path means on the command line,
    shared by `move` (whose argument is a bucket) and `ref` (whose arguments are
    plan documents): a leading '/' is optional, empty and '.' segments are dropped,
    and '..' is refused so neither a bucket nor a reference can point outside the
    arbite root. Reusing it is what makes '/plans/a.md' and 'plans/a.md' one
    reference rather than two spellings arbite happens to accept."""
    parts = [p for p in value.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise TicketError(
            f"'{value}' may not contain '..' (a root-relative path must stay under "
            "the arbite root)"
        )
    return parts


def _normalise_reference(value: str) -> str:
    """A `references` entry in its canonical stored form, or a clear error.

    Applies `_root_relative_segments` -- so '/plans/a.md' and 'plans/a.md' are the
    same reference, and '..' is refused -- then the schema's own validator, which
    deliberately refuses a leading '/' (so the slash has to be gone *before* it
    runs) and an empty entry. `ref add` and `ref rm` both go through this, so what
    `rm` looks for is exactly what `add` wrote."""
    ref = "/".join(_root_relative_segments(value.strip()))
    schema.validate_field("references", [ref])
    return ref


def _warn_about_missing_references(references, sink) -> None:
    """Warn on stderr -- never fail -- about references with no document on disk.

    A plan is routinely written *after* the ticket that needs it, and a SQLite
    store may have no plans/ directory at all, so a missing document is an ordinary
    drafting state: the write still succeeds and the exit code stays 0. `arbite
    doctor` reports the same thing as a problem (kind 'dangling_reference'),
    resolved through the same `missing_references` helper, so the warning and the
    check cannot disagree about whether a referenced plan exists."""
    for ref in missing_references(references, getattr(sink, "arbite_dir", None)):
        print(
            f"warning: reference '{ref}' has no document at "
            f"{reference_path(ref, sink.arbite_dir)} -- a reference may point at a "
            "plan that has not been written yet; 'arbite doctor' reports dangling "
            "references",
            file=sys.stderr,
        )


def cmd_init(args):
    """Create the arbite directory and the selected sink's store, then write the
    agent-facing command reference.

    Which sink it creates is decided exactly like every other command decides --
    `--sink`, then `ARBITE_SINK`, then `sink:` in .arbite/project.yaml, then file --
    so setting up a SQLite project is one flag, not a different command. The store
    it creates then becomes the project default: `sink:` is written to
    .arbite/project.yaml (the directory and the file created if missing), so later
    commands -- including ones an agent runs with no flags -- read the same store.
    An `ARBITE_SINK` selection is treated as this-process-only and reported rather
    than written into committed config."""
    spec, project_root = _cwd_sink(args)
    arbite_dir = project_root / config.ARBITE_DIRNAME
    arbite_dir.mkdir(parents=True, exist_ok=True)

    sink = build_sink(spec, arbite_dir)
    # The coordination guards run before anything is created, so a layout that
    # cannot be built (a file where a directory belongs) refuses with an
    # instruction instead of failing halfway through setup, after the ticket
    # directories already exist.
    _coordination_app(args, sink, project_root).check_layout()
    sink.init()
    print(f"{sink.kind} sink ready at {sink.root}")

    # Agent scratchpads stay plain files regardless of sink: they are
    # harness-facing state, not tickets, and deliberately outside the sink
    # contract.
    agents_dir = arbite_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_ids = config.load_known_agent_ids(project_root)
    if not agent_ids:
        print(
            "no agents configured (add an 'agents:' list to .arbite/project.yaml to "
            "pre-create scratchpads)"
        )
    else:
        for agent_id in agent_ids:
            scratchpad = agents_dir / f"{agent_id}.md"
            if scratchpad.exists():
                continue
            scratchpad.write_text(f"# {agent_id}\n\nNo ticket claimed yet.\n", encoding="utf-8")
            print(f"created scratchpad for {agent_id}")

    # Make the choice sticky: the store this command just created becomes the
    # project default, so no later command -- least of all one an agent runs with
    # no flags -- can read a different store by accident. A store that exists but
    # that nothing selects looks exactly like an empty one, and that is the one
    # failure this project cannot leave silently available.
    explicit = getattr(args, "sink", None)
    resolved = config.configured_sink_spec(project_root)
    if resolved.kind != sink.kind:
        if not explicit and os.environ.get(config.ENV_SINK):
            # An environment override is one process's decision, not the project's.
            print(
                f"note: {config.ENV_SINK}={sink.kind} selected this store for this command "
                f"only; run 'arbite init --sink {sink.kind}' to make it the project default"
            )
        else:
            written = config.set_configured_sink(sink.kind, project_root)
            print(
                f"set 'sink: {sink.kind}' in {written.name} -- the store this command created "
                "is now the project default, so plain 'arbite' commands read it"
            )

    # Coordination state lives beside the tickets (a `coordination/` directory) or
    # in the same database, and the workspace binding is recorded exactly once, here
    # -- so every later command has one authoritative answer for "which workspace is
    # this" without a bind command to forget. This runs after the sink selection is
    # committed above, so the report names the store a plain command will read.
    ensure_scratch_dir(arbite_dir)
    recorded = _coordination_app(args, sink, project_root).record_workspace()
    if recorded.lines:
        print(recorded.to_text())

    parser, subparsers_by_name = build_parser()
    agents_md = arbite_dir / "AGENTS.md"
    # The guide is committed and read by processes other than this one, so it
    # describes the sink a *plain* command will use (committed config only, no
    # --sink, no environment) -- and names any other store here that holds tickets
    # but that nothing selects.
    active = build_sink(config.configured_sink_spec(project_root), arbite_dir)
    agents_md.write_text(
        docs.render(
            parser,
            subparsers_by_name,
            _describe_safely(active),
            _find_stale_store(sink, active, arbite_dir),
        ),
        encoding="utf-8",
    )
    print(
        f"AGENTS.md refreshed at {agents_md} -- this is not auto-discovered, so point your "
        f"project's CLAUDE.md (or similar) at it explicitly, e.g. a line like "
        f"'read {config.ARBITE_DIRNAME}/AGENTS.md', if you want agents to find arbite"
    )

    # --agents-doc / --claude-doc install the short instructions block into the
    # project's own doc files, so a harness that auto-reads AGENTS.md / CLAUDE.md
    # finds arbite without anyone hand-editing those files. The block text lives in
    # docs.py; here we only decide which files and report what happened.
    for enabled, filename in (
        (getattr(args, "agents_doc", False), "AGENTS.md"),
        (getattr(args, "claude_doc", False), "CLAUDE.md"),
    ):
        if not enabled:
            continue
        doc_path = project_root / filename
        outcome = docs.install_instructions(doc_path)
        if outcome == "created":
            print(f"created {doc_path} with the arbite instructions block")
        elif outcome == "prepended":
            print(
                f"prepended the arbite instructions block to {doc_path} "
                "(existing contents left in place)"
            )
        else:
            print(
                f"{doc_path} already contains the arbite instructions block; "
                "left unchanged"
            )


def cmd_sink(args):
    """Report or initialise the active sink: which implementation is in use,
    where its store lives, and what it can do."""
    action = args.subcommand or "info"
    if action == "init":
        sink = config.open_sink(getattr(args, "sink", None), require_initialised=False)
        sink.init()
        print(f"{sink.kind} sink ready at {sink.root}")
        return

    sink = _require_sink(args)
    info = sink.describe()
    if args.json:
        _print_json(info.to_dict())
        return
    print(f"sink: {info.kind}")
    print(f"root: {info.root}")
    print(f"status is folder location: {'yes' if info.status_is_location else 'no'}")
    # "support" vs "in use": the capability line and a sink's own list of buckets
    # in use would otherwise both print under the word "buckets".
    print(f"buckets supported: {'yes' if info.supports_buckets else 'no'}")
    # A user should not have to read the docs to discover that another store
    # exists; `sink info` is where they look to ask what is going on.
    print(
        f"available sinks: {', '.join(SINK_KINDS)} (choose one with --sink, "
        f"{config.ENV_SINK}, or a 'sink:' key in .arbite/project.yaml)"
    )
    if info.details:
        for key, value in sorted(info.details.items()):
            print(f"{key}: {value}")
    counts = ", ".join(f"{status} {n}" for status, n in sorted(info.status_counts.items()))
    print(f"tickets: {info.ticket_count}{f' ({counts})' if counts else ''}")


def cmd_workspace_show(args):
    """Report the workspace this project derives, and what its coordination state holds.

    The workspace is *derived* -- from the located `.arbite/` directory plus the
    resolved sink -- so there is no bind command, no `--force` override and no
    conflict path. This command is read-only: it writes nothing, not even the
    workspace record, so two runs on unchanged state print identical text."""
    sink = _require_sink(args)
    result = _coordination_app(args, sink).workspace_show()
    if args.json:
        _print_json(result.to_json())
    else:
        print(result.to_text())


def cmd_events(args):
    """Read the coordination event stream: what happened, in cursor order.

    One-shot by construction. `--follow` is *refused* rather than implemented,
    because a watcher that blocks is a sleeping process and this proxy deliberately
    has none: the refusal names the pattern that works (read the tail, or poll with
    your own loop and keep the cursor). The selection rules -- reads are a separate
    category, `--tail` bootstraps, `--after` resumes -- live in the application
    layer, and "nothing new since that cursor" is exit 2 rather than an error, so a
    poll can branch on the code without parsing prose."""
    if args.follow:
        # The one refusal this surface prints on stdout: the frozen transcript for
        # `--follow` (EV7) shows both the error line and the hint there, and the
        # example harness asserts an empty stderr for every scenario.
        result = coordination_app.follow_refusal()
        if args.json:
            _print_json(result.to_json())
        else:
            print(result.to_text())
        sys.exit(EXIT_ERROR)
    sink = _require_sink(args)
    result = _coordination_app(args, sink).events(
        after=args.after, tail=args.tail, include_reads=args.include_reads
    )
    if args.json:
        _print_json(result.to_json())
    else:
        print(result.to_text())
    if result.exit_code:
        sys.exit(result.exit_code)


def cmd_status(args):
    """Report the shape of the backlog: how many tickets sit in each status.

    A report rather than a query, and deliberately not any of the three things
    its name collides with: not `arbite set <id> status <value>` (which changes
    one ticket's status), not the `--status` filter on list/search (which selects
    tickets), and not `arbite sink info` (which describes the *store* -- its kind,
    root and capabilities). Every status in `schema.STATUSES` is rendered, in
    vocabulary order, including the ones holding nothing: an empty status must be
    visibly empty rather than absent, and a status added to the vocabulary later
    must show up without this command changing. Tickets filed in a bucket (a
    promoted wish in `wishlist/`, a `move <id> /plans` filing) are out of the
    status workflow, exactly as `arbite list` treats them, so they are not counted
    under the status they retain -- which is what keeps a `raw 2` here agreeing
    with `arbite list --status raw` showing two tickets."""
    sink = _require_sink(args)
    # The same `TicketQuery` plumbing every list view builds, so a filter means
    # here exactly what it means there. `arbite status` deliberately takes no
    # --status flag (the command's whole job is every status at once) and no
    # --priority flag, so both stay unset in the query.
    counts = count_by_status(sink.query(_filter_query(args)), vocabulary=True)
    total = sum(counts.values())
    if args.json:
        # A flat mapping of status -> count in `STATUSES` order, plus `total`.
        # Flat rather than nested because no status is named "total", so nothing
        # collides, and an agent can read the count for one status without
        # knowing the shape of a wrapper. Dict insertion order is the vocabulary
        # order, which json.dumps preserves.
        _print_json({**counts, "total": total})
        return
    filters = _filter_labels(args)
    print("tickets by status" + (f" (filters: {', '.join(filters)})" if filters else ""))
    status_width = max([len(status) for status in counts] + [len("total")])
    count_width = max([len(str(n)) for n in counts.values()] + [len(str(total))])
    for status, n in counts.items():
        print(f"{status:<{status_width}}  {n:>{count_width}}")
    print(f"{'total':<{status_width}}  {total:>{count_width}}")


def cmd_progress(args):
    """Show what is actually in flight, and the epics it sits in.

    `arbite status` counts the whole backlog; this answers the other question --
    "what is moving right now, and what surrounds it". The selection rule is the
    whole substance of the command:

    1. the **live** tickets are every ticket whose status is open, in_progress or
       review (`schema.LIVE_STATUSES`);
    2. the epics in scope are every epic holding at least one live ticket;
    3. **every** ticket of those epics is shown, whatever its status -- including
       closed and shelved ones, which are the context that makes the live ticket
       legible. An epic with seven closed siblings and one in_progress ticket
       shows all eight; an epic whose tickets are all closed never appears at all.

    Live tickets with no epic are grouped under a `no epic` heading rather than
    dropped. Within an epic the order is topological by `depends_on`
    (`graph.topo_order`, the same ordering `list next` uses), computed over the
    whole ticket set so a dependency outside the epic still orders the tickets
    inside it, and unsatisfiable cycles are warned about on stderr exactly as the
    other listings do.

    Buckets are excluded, as in every other view: a ticket filed in `wishlist/` or
    `plans/` is out of the status workflow, so it is not live and cannot pull its
    epic into the report."""
    sink = _require_sink(args)
    every = sink.query(TicketQuery())
    by_id = {t.id: t for t in every}

    live = [t for t in every if t.status in schema.LIVE_STATUSES]
    if args.epic is not None:
        live = [t for t in live if t.epic == args.epic]

    order = graph.topo_order(by_id)

    groups = []  # (heading, members), one entry per epic in scope
    for epic in sorted({t.epic for t in live if t.epic}):
        groups.append((epic, [by_id[tid] for tid in order if by_id[tid].epic == epic]))
    ungrouped = {t.id for t in live if not t.epic}
    if ungrouped:
        groups.append(("no epic", [by_id[tid] for tid in order if tid in ungrouped]))

    if not groups:
        qualifier = f" in epic '{args.epic}'" if args.epic else ""
        print(
            f"no live tickets{qualifier} (live = {' | '.join(schema.LIVE_STATUSES)})"
        )
        sys.exit(EXIT_EMPTY)

    selected = {t.id for _, members in groups for t in members}
    _warn_cycles(by_id, selected)

    if args.json:
        locations = sink.location_map(
            [t for _, members in groups for t in members]
        )
        payload = []
        for heading, members in groups:
            counts = count_by_status(members, vocabulary=True)
            payload.append(
                {
                    # `null` for the un-epic'd group rather than the display
                    # heading, so a consumer can test epic membership honestly.
                    "epic": None if heading == "no epic" else heading,
                    "counts": counts,
                    "live": sum(counts[status] for status in schema.LIVE_STATUSES),
                    "total": len(members),
                    "tickets": [t.to_dict(locations.get(t.id)) for t in members],
                }
            )
        _print_json(payload)
        return

    for heading, members in groups:
        counts = count_by_status(members, vocabulary=True)
        # Every status present, in vocabulary order, so the line reads the same
        # shape as `arbite status` -- the same counting implementation, so the
        # two cannot disagree.
        summary = " / ".join(f"{n} {status}" for status, n in counts.items() if n)
        print(f"{heading}  ({len(members)} tickets: {summary or 'none'})")
        _print_flat(members)


def cmd_create(args):
    if not args.blank:
        missing = [
            flag
            for flag, val in (
                ("--title", args.title),
                ("--type", args.type),
                ("--tier", args.tier),
                ("--domain", args.domain),
            )
            if not val
        ]
        if missing:
            raise TicketError(
                f"missing required arguments: {', '.join(missing)} "
                "(or pass --blank to scaffold a template ticket for a human to fill in)"
            )
    if args.priority is not None and args.priority < 1:
        raise TicketError("--priority must be a positive integer (lower = more urgent)")
    # `--references` is validated like any other settable field (tags/depends_on
    # are freeform, but a reference is a path under the arbite root, so a bad one
    # -- absolute or escaping with '..' -- is refused up front).
    if args.references:
        schema.validate_field("references", args.references)

    sink = _require_sink(args)
    now = schema.now()
    description = args.description or (schema.BLANK_DESCRIPTION if args.blank else "")
    body = schema.DEFAULT_BODY.format(description=description)
    if args.blank:
        body = f"{schema.BLANK_WARNING}\n\n{body}"

    new_ticket = Ticket(
        id=sink.new_id(),
        title=args.title or schema.BLANK_TITLE,
        status="open",
        type=args.type or schema.BLANK_TYPE,
        tier=args.tier or schema.BLANK_TIER,
        domain=args.domain or schema.BLANK_DOMAIN,
        epic=args.epic,
        priority=args.priority,
        tags=_split_csv(args.tags),
        assignee=None,
        depends_on=_split_csv(args.depends_on),
        references=_split_csv(args.references),
        blocked_by=None,
        created=now,
        updated=now,
        closed=None,
        body=body,
    )

    sink.create(new_ticket)
    location = sink.location(new_ticket.id)
    if args.blank:
        print(
            f"created blank template {new_ticket.id} at {location} -- fill in the TODOs "
            "and save before it's claimed"
        )
    else:
        print(f"created {new_ticket.id} at {location}")


def cmd_raw(args):
    """Create a deliberately unclassified 'raw' ticket from a brief request. Only
    the type and a placeholder title are set; the body explains what still needs
    to be filled in (title, tier, domain, epic, priority, and an expanded
    description) before the ticket can be claimed or worked. Status 'raw' keeps it
    out of 'arbite list next' until triage sets it to 'open' (or claims it
    directly). The ticket is auto-grouped under the 'classification' epic so
    triage/classification jobs can discover it with 'arbite list next --epic
    classification' or pull the oldest one with 'arbite fetch'. A 'request' raw
    ticket carries an extra note: it is a request for a change, not necessarily a
    bug or a new feature but a tweak or lateral change, and is treated as ordinary
    work once classified. A 'wish' raw ticket carries an extra note: wishlist
    items are reclassified as 'feature' and filed in the wishlist bucket rather
    than opened as work."""
    sink = _require_sink(args)
    now = schema.now()
    message = " ".join(args.message)
    # The id is minted first because a wish's note names the ticket it belongs to.
    new_id = sink.new_id()

    description = schema.RAW_DESCRIPTION.format(message=message)
    if args.type == "memo":
        description = f"{description}\n\n{schema.MEMO_RAW_NOTE}"
    elif args.type == "request":
        description = f"{description}\n\n{schema.REQUEST_RAW_NOTE}"
    elif args.type == "wish":
        description = f"{description}\n\n{schema.WISH_RAW_NOTE.format(id=new_id)}"

    new_ticket = Ticket(
        id=new_id,
        title=schema.RAW_TITLE_FORMAT.format(type=args.type),
        status="raw",
        type=args.type,
        tier=schema.BLANK_TIER,
        domain=schema.BLANK_DOMAIN,
        epic=CLASSIFICATION_EPIC,
        priority=None,
        tags=[],
        assignee=None,
        depends_on=[],
        blocked_by=None,
        created=now,
        updated=now,
        closed=None,
        body=schema.DEFAULT_BODY.format(description=description),
    )

    sink.create(new_ticket)
    print(
        f"created raw {args.type} ticket {new_ticket.id} at {sink.location(new_ticket.id)} -- "
        "classify it (title/tier/domain/epic/priority/description) before it can be worked; "
        f"it is grouped under the '{CLASSIFICATION_EPIC}' epic until then. "
        "Pull it for classification with 'arbite fetch'."
    )


def cmd_fetch(args):
    """Pull the oldest raw ticket (status 'raw'), optionally restricted to a type,
    and print it exactly like 'arbite show' would -- except with a 'derived_note'
    injected at the top (a JSON field in --json mode, a leading block in text mode)
    telling the calling agent to classify it with 'arbite promote' and either open it
    for someone else or claim it now. This is a triage queue, so it's oldest-first
    (by 'created') rather than priority-ordered like 'list next' -- a raw ticket has
    no priority yet, and fetch itself is strictly read-only: 'arbite promote' is the
    write half of triage."""
    sink = _require_sink(args)
    candidates = sink.query(
        TicketQuery(
            status=("raw",),
            type=(args.type,) if args.type else (),
            order="created_asc",
            limit=1,
        )
    )

    if not candidates:
        if args.json:
            _print_json(None)
        else:
            qualifier = f" of type '{args.type}'" if args.type else ""
            print(f"no raw tickets{qualifier} found")
        sys.exit(EXIT_EMPTY)

    t = candidates[0]
    note = schema.derived_note(t.id, t.type)
    if args.json:
        data = t.to_dict(sink.location(t.id))
        data["derived_note"] = note
        _print_json(data)
    else:
        print(f"derived_note: {note}\n")
        print(sink.render(t), end="")


def _promoted_body(captured_request: str, description: Optional[str], original_body: str) -> str:
    """The body a promoted ticket should carry.

    Triage replaces what `arbite raw` wrote, so the capture's 'this must be filled
    out before it can be worked' checklist must not survive promotion: the
    description becomes `--description` when it is given, and otherwise the request
    the ticket was captured from -- which is at least the thing that was asked for.

    Either way the capture is kept as an `Original request: ...` line, read out of
    the raw body by the same helper (`schema.raw_captured_request`) that found it
    when `arbite raw` wrote it, so nothing the original request said is lost --
    unless the description already quotes it. Any notes the ticket had are carried
    over verbatim, since promotion edits the description, not the audit trail."""
    text = (description or "").strip() or captured_request
    block = text
    if captured_request and captured_request not in text:
        block = (
            f"{block}\n\nOriginal request: {captured_request}"
            if block
            else f"Original request: {captured_request}"
        )
    body = schema.DEFAULT_BODY.format(description=block)
    notes = schema.notes_body(original_body).strip("\n")
    return f"{body}{notes}\n" if notes else body


def _write_raw_snapshot(sink, ticket) -> Path:
    """Freeze a raw capture as `<arbite_dir>/raw/processed/<id>.raw.md`.

    The content is `sink.render(ticket)` -- the ticket's own canonical text, taken
    *before* anything is rewritten -- and the name comes from the file sink's
    `raw_snapshot_name()` convention, so promote cannot drift from what the scan
    excludes. It is a filesystem artifact on *both* sinks (the same reasoning that
    keeps agent scratchpads as plain files either way), so the SQLite sink gets one
    too; the directory is created if missing.

    Written with an exclusive create, because a snapshot is frozen audit history: a
    second promote of the same id -- or two agents promoting it at once -- must
    refuse rather than rewrite the record of what was originally requested. A manual
    delete is the remedy if a snapshot is genuinely stale."""
    path = Path(sink.arbite_dir).joinpath(*RAW_PROCESSED_DIR) / raw_snapshot_name(ticket.id)
    try:
        write_exclusive(sink.render(ticket), path, ticket.id)
    except Conflict:
        # `write_exclusive` reports this as a lost race; for a snapshot the reason is
        # not a race but the audit rule, so say that instead.
        raise TicketError(
            f"a snapshot of {ticket.id} already exists at {path}, and a snapshot is frozen "
            "audit history -- it is never rewritten. If this one is genuinely stale, delete "
            "that file by hand and promote again"
        )
    return path


def cmd_promote(args):
    """Turn a raw capture into a classified, workable ticket: the write half of triage.

    `arbite fetch` is the read-only queue that hands out raw tickets; this is what an
    agent runs once it has classified one. In order:

    1. The raw ticket is snapshotted *verbatim* -- its own canonical text, taken
       before anything is rewritten -- to `<arbite_dir>/raw/processed/<id>.raw.md`,
       through the file sink's `raw_snapshot_name()` convention, on both sinks,
       created if missing. The snapshot is written with an exclusive create and is
       never overwritten (a second promote refuses and names the path), because it is
       frozen audit history. Snapshot first, then mutate, so a crash leaves the raw
       ticket intact rather than a half-classified one with no record of what was
       originally requested.
    2. The ticket is classified *in place*, through the sink's compare-and-swap
       `update(expect=...)` -- never delete-and-recreate -- so the id, `created` and
       any notes carry forward, and anything that already referenced the raw request
       stays valid. `--title`/`--tier`/`--domain` are required (the level `arbite
       create` requires, since a promoted ticket must never reach `list next` with
       placeholder fields), and a title still in the raw placeholder form
       (`<type> (raw): Requires Classification`) or a `TODO:` tier/domain is refused
       with the offending field(s) named, before anything is written.
    3. `type` is carried over unchanged -- there is deliberately no `--type` flag --
       except that a `wish` becomes `feature` (the documented wish rule). A wish is
       then filed in the `wishlist` bucket with its status left at `raw`: it leaves
       the triage queue because the default query excludes bucketed tickets, but it
       does not become work. `--agent` is refused for a wish rather than silently
       ignored, since claiming a wishlist item is meaningless.
    4. Otherwise the ticket moves to `open` -- or, with `--agent <id>`, straight to
       `in_progress` assigned to that agent in the same write, which is the end state
       `fetch`'s `derived_note` suggests for an agent about to work it.

    An omitted `--epic` clears the `classification` epic the capture was auto-grouped
    under (a hand-set real epic is left alone); `--priority` and `--tags` replace
    those fields; and `--description` replaces the capture's triage body, keeping the
    original request as an `Original request: ...` line and keeping any notes the
    ticket already had (see `_promoted_body`). The receipt names the id, the new
    status and location, and the snapshot path. Only a `raw` ticket can be promoted:
    a classified one is refused, so re-promoting cannot silently reclassify work that
    has moved on."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    if t.status != "raw":
        raise TicketError(
            f"ticket {t.id} is not a raw capture (status: {t.status}) -- only a raw ticket "
            "can be promoted; use 'arbite set' to change a classified ticket"
        )
    is_wish = t.type == "wish"
    if is_wish and args.agent:
        raise TicketError(
            f"--agent does not apply to a wish: {t.id} is a wishlist item, and promote files "
            "it in the wishlist bucket instead of claiming it (drop --agent)"
        )

    # The same fields `arbite create` refuses to do without: promotion means
    # classified, not merely moved out of `raw`.
    missing = [
        flag
        for flag, value in (
            ("--title", args.title),
            ("--tier", args.tier),
            ("--domain", args.domain),
        )
        if not value
    ]
    if missing:
        raise TicketError(
            f"missing required arguments: {', '.join(missing)} -- a promoted ticket is fully "
            "classified, so title/tier/domain cannot be left as the raw capture's placeholders"
        )

    # A second guard, on the values this call would actually write. The title needs two
    # tests because the raw form is not a `TODO:` value: `<type> (raw): Requires
    # Classification` is derived from RAW_TITLE_FORMAT (so it cannot drift from what
    # `arbite raw` wrote), while a plain `TODO: ...` title is the generic placeholder rule.
    still_placeholder = []
    if schema.is_raw_title_placeholder(args.title) or schema.is_placeholder(args.title):
        still_placeholder.append("--title")
    if schema.is_placeholder(args.tier):
        still_placeholder.append("--tier")
    if schema.is_placeholder(args.domain):
        still_placeholder.append("--domain")
    if still_placeholder:
        raise TicketError(
            f"still a placeholder: {', '.join(still_placeholder)} -- replace the placeholder "
            "text with a real value before promoting (a promoted ticket must not reach "
            "'arbite list next' with placeholder fields)"
        )
    # `--tier` deliberately has no argparse choices (like `arbite set`), so the shared
    # validator is what refuses a typo'd tier -- with the same message `set` gives.
    schema.validate_field("tier", args.tier)
    if args.priority is not None and args.priority < 1:
        raise TicketError("--priority must be a positive integer (lower = more urgent)")

    # Snapshot first, then mutate: a crash between the two must leave the raw ticket
    # intact rather than a classified ticket whose original capture was never recorded.
    snapshot = _write_raw_snapshot(sink, t)

    captured = schema.raw_captured_request(t)
    original_body = t.body
    expect = _expect_from(t)
    t.title = args.title
    t.tier = args.tier
    t.domain = args.domain
    t.tags = _split_csv(args.tags)
    t.priority = args.priority
    if args.epic:
        t.epic = args.epic
    elif t.epic == CLASSIFICATION_EPIC:
        # Raw tickets are grouped under the `classification` epic *for triage*; that
        # grouping is done with, so it goes unless a real epic replaces it.
        t.epic = None
    t.body = _promoted_body(captured, args.description, original_body)
    t.updated = schema.now()

    if is_wish:
        # Retyped, not opened: the documented wish rule, and the status deliberately
        # stays `raw` -- filing it in the wishlist bucket is what takes it out of the
        # triage queue (the default query excludes bucketed tickets).
        t.type = "feature"
    elif args.agent:
        t.status = "in_progress"
        t.assignee = args.agent
    else:
        t.status = "open"

    sink.update(t, expect=expect)
    if is_wish:
        # Filing is not a state change, so it is a second call -- but it is the half
        # that makes the wish stop being served by `fetch`/`list raw`.
        sink.move_to_bucket(t.id, WISHLIST_BUCKET)

    # Classifying and claiming in one write is an acquisition path too, so it creates
    # the attempt here: without it the worker this ticket was just assigned to would
    # have no attempt to claim its files with (the lifecycle operation refuses a
    # second one, so this cannot double up with a later claim).
    lifecycle = _lifecycle(args, sink)
    attempt = (
        lifecycle.begin_attempt(t, args.agent) if args.agent and not is_wish else None
    )

    if is_wish:
        print(
            f"promoted {t.id} -> {sink.location(t.id)} (reclassified as feature and filed in "
            f"the '{WISHLIST_BUCKET}' bucket; status stays raw, so it is out of the work and "
            "triage queues)"
        )
    else:
        owner = f", assigned to {args.agent}" if args.agent else ""
        print(f"promoted {t.id} -> {sink.location(t.id)} (status {t.status}{owner})")
    print(f"snapshot of the raw capture: {snapshot}")
    if attempt is not None:
        print(
            f"attempt: {attempt.id} (generation {attempt.generation}, ticket {t.id}, "
            f"workspace {attempt.workspace_id})"
        )
        print(lifecycle.file_hint_text(t.id, attempt.id))


def _filter_labels(args) -> list:
    """The filters an `arbite status` call actually narrowed by, as display-ready
    `--flag value` strings.

    `arbite status` echoes them above the table, so a narrowed report can never be
    mistaken for the whole backlog -- a count of `open 1` under `--epic workflow`
    means something quite different from `open 1` overall."""
    labels = []
    for flag in ("epic", "domain", "tier", "assignee"):
        value = getattr(args, flag, None)
        if value:
            labels.append(f"--{flag} {value}")
    return labels


def _filter_query(args) -> TicketQuery:
    """The structured filters shared by every list view.

    Built as a `TicketQuery` so the same selection is expressed once and executed
    by whichever sink is active, instead of being re-implemented as a Python
    predicate here."""
    return TicketQuery(
        status=tuple(getattr(args, "status", None) or ()),
        tier=getattr(args, "tier", None),
        domain=getattr(args, "domain", None),
        epic=getattr(args, "epic", None),
        assignee=getattr(args, "assignee", None),
        priority=getattr(args, "priority", None),
    ).normalized()


def _print_flat(rows):
    """Print tickets as a fixed-width table (rows must be non-empty)."""
    id_w = max(len(t.id) for t in rows) + 1
    status_w = max(len(t.status) for t in rows) + 1
    priority_w = max(len("-") if t.priority is None else len(str(t.priority)) for t in rows) + 1
    tier_w = max(len(t.tier) for t in rows) + 1
    domain_w = max(len(t.domain) for t in rows) + 1
    epic_w = max(len(t.epic or "-") for t in rows) + 1
    assignee_w = max(len(t.assignee or "-") for t in rows) + 1

    for t in rows:
        prio = "-" if t.priority is None else str(t.priority)
        print(
            f"{t.id:<{id_w}} {t.status:<{status_w}} {prio:<{priority_w}} "
            f"{t.tier:<{tier_w}} {t.domain:<{domain_w}} {(t.epic or '-'):<{epic_w}} "
            f"{(t.assignee or '-'):<{assignee_w}} {t.title}"
        )


def _print_raw_summary(raw):
    """Print the raw backlog as a running todo list: one line per raw ticket
    under a heading per raw type (with its count), oldest first so it reads
    like the `arbite fetch` queue it mirrors. Only types that have tickets are
    shown. The line is the id plus the request text the ticket was captured
    from -- a raw ticket's title is always '<type> (raw): Requires
    Classification' and its tier/domain/priority/epic are placeholders, so the
    flat table `list` normally prints would be pure noise here."""
    by_type = {}
    for t in raw:
        by_type.setdefault(t.type, []).append(t)

    print(f"raw tickets awaiting classification ({len(raw)}):")
    for raw_type in schema.RAW_TYPE_CHOICES:
        rows = by_type.get(raw_type)
        if not rows:
            continue
        print(f"\n{raw_type} ({len(rows)}):")
        for t in rows:
            request = schema.raw_captured_request(t)
            if request:
                print(f"  {t.id}  {request}")
            else:
                print(
                    f"  {t.id}  (request text no longer in body -- the description "
                    f"was edited; use 'arbite show {t.id}' to read what it is now)"
                )


def _cmd_list_raw(args, sink):
    """Summarize every raw ticket (status 'raw') as a running todo list until a
    classification run drains it. The classification fields of a raw ticket are
    placeholders and its title is always '<type> (raw): Requires
    Classification', so rather than the usual flat table this groups tickets by
    raw type and shows, one ticket per line, the request text each was captured
    from. Oldest first by 'created' (then id), matching the oldest-first queue
    that `arbite fetch` pulls from -- a stable, chronological backlog a human
    or triage run can scan top to bottom. Exits 2 when nothing is raw yet,
    mirroring the other list views."""
    raw = sink.query(TicketQuery(status=("raw",), order="created_asc"))

    if args.json:
        # JSON mode keeps the list contract: an array of ticket dicts whose
        # field names match the frontmatter, plus a derived 'request' field
        # (like `fetch` injects 'derived_note') so a caller can group or
        # display the captured text without parsing the body itself.
        locations = sink.location_map(raw)
        payload = []
        for t in raw:
            data = t.to_dict(locations.get(t.id))
            data["request"] = schema.raw_captured_request(t)
            payload.append(data)
        _print_json(payload)
    elif raw:
        _print_raw_summary(raw)
    else:
        print("no raw tickets found")
    if not raw:
        sys.exit(EXIT_EMPTY)


def _warn_cycles(by_id, scope_ids=None):
    """Report any unsatisfiable depends_on cycle touching the tickets in scope
    on stderr.

    A topological order that silently appends cycle members hands agents work
    that will never become workable. Warn rather than fail, so one bad edge
    doesn't take down every query; `arbite doctor` reports the same cycles as a
    hard problem."""
    for chain in graph.cycle_warnings(by_id, scope_ids):
        print(
            f"warning: dependency cycle, these tickets can never become workable: {chain}",
            file=sys.stderr,
        )


def _print_topo(by_id, selected_ids, sink, as_json=False, count=None):
    """Print the selected tickets in topological dependency order.

    The order is always computed over by_id (every ticket), so a blocker that
    the caller's filters exclude still holds back the tickets that depend on
    it; the filter is applied afterwards, as a pure selection over the
    already-ordered result."""
    _warn_cycles(by_id, selected_ids)
    rows = [by_id[tid] for tid in graph.topo_order(by_id) if tid in selected_ids]
    _emit_tickets(apply_limit(rows, count), as_json, sink)


def _cmd_list_next(args, sink, every):
    """Print the next open ticket(s) that are actually workable -- every ticket
    they depend on is closed -- most urgent (lowest priority number) first.
    Readiness is decided against the complete ticket set, so an unmet
    dependency holds a ticket back whatever tier/domain/epic its blocker sits
    at; --tier/--domain/--epic then narrow the workable candidates, and
    anything that isn't `open` is never considered. If nothing workable matches
    the filters, that's an answer with the reason and the exit code 2 (CL4).

    With --claim, the most urgent candidates are claimed in the same command, each
    through the same acquisition operation `arbite claim` uses -- so a ticket this
    queue hands out gets its attempt here too, and a ticket that already has one is
    never offered. That closes the race in the obvious two-step version (`list next`
    then `claim`): between those two commands another agent can claim the ticket you
    were just handed, and both agents then work it."""
    by_id = {t.id: t for t in every}
    open_matching = sink.query(
        TicketQuery(
            status=("open",),
            tier=args.tier,
            domain=args.domain,
            epic=args.epic,
            order="next",
        )
    )
    workable = [t for t in open_matching if graph.is_workable(t, by_id)]
    lifecycle = _lifecycle(args, sink)
    # Work that already has an active attempt is not offered: it is being done, and
    # the attempt is the fact that says so.
    candidates = [t for t in workable if lifecycle.active_attempt(t.id) is None]

    # `next` answers "what should I work on", so it returns one ticket unless
    # the caller asks for a batch.
    wanted = 1 if args.count is None else args.count

    if not candidates:
        # "Nothing is ready" and "everything is deadlocked" look identical from
        # the outside, so say which one it is rather than leaving an agent to
        # poll a queue that can never produce work.
        _warn_cycles(by_id)
        if not args.claim:
            if args.json:
                # JSON stays a document whatever the answer is: the two-line report
                # below is the *text* form of "nothing is ready yet".
                _emit_tickets([], True, sink)
            else:
                _report_nothing_workable(args, by_id, open_matching)
            return
        # Nothing to claim is not an error; the exit code carries it.
        _emit_tickets([], args.json, sink)
        return

    if not args.claim:
        _emit_tickets(candidates[:wanted], args.json, sink)
        return

    # Walk candidates in order, claiming until we have `wanted` of them: if
    # another agent wins the race for one, move on to the next rather than
    # failing the whole dispatch. Each claim is individually atomic, so a
    # partial batch is a correct result, not a broken one.
    claimed = []
    errors = []
    for candidate in candidates:
        if len(claimed) == wanted:
            break
        try:
            result = lifecycle.claim(candidate, args.claim)
        except Conflict as e:
            # Lost the race for this one. The next candidate is untouched, so the
            # loop simply moves on to it.
            errors.append(str(e))
            continue
        except NotReady as e:
            # The queue filtered on readiness, but the guard is the rule; a
            # dependency that changed in between means this candidate is not ours.
            errors.append(str(e))
            continue
        except Busy as e:
            if e.reason == "store_locked":
                # The store itself is unavailable: that is not "this candidate
                # was taken", it is "this dispatch cannot proceed".
                raise
            errors.append(str(e))
            continue
        claimed.append((candidate, result))

    if not claimed:
        raise TicketError(
            "every workable ticket was claimed by another agent first: " + "; ".join(errors)
        )

    if args.json:
        _print_json([result.data for _, result in claimed])
    else:
        _print_flat([ticket for ticket, _ in claimed])
    if len(claimed) < wanted:
        # Say so explicitly, and say why: a dispatcher that asked for 3 and got
        # 2 needs to know whether the queue ran dry or it lost races, because
        # those call for different responses (wait vs. retry immediately). The
        # table is the result; this note is the one line of commentary, on stderr
        # so a pipe reading the table is not disturbed mid-row.
        if errors:
            reason = f"{len(errors)} were claimed by another agent first"
        else:
            reason = "no more workable tickets match"
        sys.stdout.flush()
        print(
            f"note: asked for {wanted} ticket(s), claimed {len(claimed)} -- {reason}",
            file=sys.stderr,
        )


def _report_nothing_workable(args, by_id, open_matching):
    """Answer "nothing is ready yet" with *why*, and exit 2.

    `list next` is the queue an agent drives itself from, so its empty answer has to
    distinguish the three cases that look identical from the outside: the filters
    match nothing at all (the existing message), they match tickets whose
    dependencies are unmet (CL4: say how many, and name the command that shows the
    chain), or they match work someone is already doing. Only the second gets the
    two-line report, because a count of blocked tickets is the fact a caller can act
    on; "no tickets found" stays for the third and for an empty queue.

    The frozen CL4 shape is followed literally: the filter list is `tier=value
    epic=value` in tier-then-epic order, and the command it suggests carries
    `--status open` plus the epic filter."""
    blocked = [t for t in open_matching if not graph.is_workable(t, by_id)]
    if not blocked:
        print("no tickets found")
        sys.exit(EXIT_EMPTY)
    labels = [
        f"{name}={value}"
        for name, value in (("tier", args.tier), ("domain", args.domain), ("epic", args.epic))
        if value
    ]
    print("no workable open tickets" + (f" matching {' '.join(labels)}" if labels else ""))
    epic = f" --epic {args.epic}" if args.epic else ""
    print(
        f"blocked by dependencies: {len(blocked)} "
        f"(run 'arbite list --topo --status open{epic}')"
    )
    sys.exit(EXIT_EMPTY)


def cmd_list(args):
    if args.count is not None and args.count < 1:
        raise TicketError(f"--count must be a positive integer, got {args.count}")
    sink = _require_sink(args)

    # `every` includes tickets filed in buckets, because readiness and the
    # dependency graph are properties of the whole set: a blocker parked in the
    # wishlist still blocks. Listing, on the other hand, shows only tickets that
    # are in the status workflow, which is the sink's default for a query.
    every = sink.query(TicketQuery(buckets=("*",)))
    by_id = {t.id: t for t in every}
    tic_ids = set(resolve_terms(list(by_id), _split_csv(args.tic))) if args.tic else set()

    if args.subcommand == "next":
        _cmd_list_next(args, sink, every)
        return

    if args.subcommand == "raw":
        _cmd_list_raw(args, sink)
        return

    if args.tree or args.topo:
        if tic_ids:
            # --tic roots the tree/topo at those tickets and pulls in every
            # transitive dependency beneath them (other field filters are ignored).
            scope_ids = graph.dependency_closure(by_id, tic_ids) & set(by_id)
        else:
            scope_ids = {t.id for t in sink.query(_filter_query(args))}
            if not scope_ids:
                _emit_tickets([], args.json, sink)
                return
        if args.topo:
            _print_topo(by_id, scope_ids, sink, args.json, args.count)
            return
        scope = {tid: by_id[tid] for tid in scope_ids}
        roots = [tid for tid in tic_ids if tid in by_id] if tic_ids else graph.tree_roots(scope, [])
        # A tree has no single flat length to cap, so --count limits the number
        # of top-level roots shown; each one still prints its full subtree,
        # since a truncated dependency chain would be actively misleading.
        roots = apply_limit(
            sorted(roots, key=lambda tid: (scope[tid].priority_sort_key(), tid)), args.count
        )
        _warn_cycles(by_id, scope_ids)
        if args.json:
            _print_json(_tree_payload(scope, roots))
        else:
            _print_tree(scope, roots)
        return

    # Flat list: field filters plus the --tic id filter.
    rows = sink.query(_filter_query(args).evolve(order="flat"))
    if tic_ids:
        rows = [t for t in rows if t.id in tic_ids]
    _emit_tickets(apply_limit(rows, args.count), args.json, sink)


def _tree_payload(scope, roots):
    """The dependency forest as nested JSON-serialisable dicts, mirroring what
    _print_tree renders. A ticket already on the current path is emitted with
    "cycle": true and not descended into."""
    def child_key(d):
        return (scope[d].priority_sort_key(), d)

    def node(tid, seen):
        t = scope[tid]
        data = t.to_dict()
        data["path"] = None
        if tid in seen:
            data["cycle"] = True
            data["depends"] = []
            return data
        seen = seen | {tid}
        children = sorted((d for d in t.depends_on if d in scope), key=child_key)
        data["depends"] = [node(d, seen) for d in children]
        return data

    return [
        node(tid, set())
        for tid in sorted(roots, key=lambda tid: (scope[tid].priority_sort_key(), tid))
    ]


def _print_tree(scope, roots):
    """Print scope as a dependency forest: children = depends_on, siblings sorted by priority."""
    def child_key(d):
        return (scope[d].priority_sort_key(), d)

    def walk(tid, indent, seen):
        t = scope[tid]
        prio = "-" if t.priority is None else str(t.priority)
        print(f"{indent}{t.id} [{t.status}] p{prio} {t.title}")
        if tid in seen:
            print(f"{indent}  ... (cycle)")
            return
        seen = seen | {tid}
        children = sorted((d for d in t.depends_on if d in scope), key=child_key)
        for d in children:
            walk(d, indent + "  ", seen)

    for tid in sorted(roots, key=lambda tid: (scope[tid].priority_sort_key(), tid)):
        walk(tid, "", set())


def cmd_claim(args):
    """Claim a ticket for an agent: assign it, mark it in_progress and start its attempt.

    The acquisition itself is the lifecycle operation's (see
    `coordination.lifecycle`): the ticket write is the compare-and-swap that decides
    which of two racing claims wins, and the attempt that owns the work is recorded
    right after it, so a caller never needs a second command and every later file
    operation has the attempt id it must present.

    `--force` is the administrative takeover and therefore takes a `--reason`: it
    revokes the attempt that held the ticket and starts a new one, and the reason is
    the only record of why the previous worker lost it."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    result = _lifecycle(args, sink).claim(
        t, args.agent, force=args.force, reason=args.reason
    )
    _emit_result(result, args.json)


def cmd_release(args):
    """Return a claimed ticket to open/ and clear its assignee.

    The counterpart to claim: an agent that stops work part-way (out of scope, out of
    context, wrong capability tier) needs one command that unassigns and
    reopens together, so the ticket becomes visible to `list next` again rather
    than sitting in_progress owned by nobody who is still working it. The attempt
    that was doing the work ends with it, and receives the reason as its handoff."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    if t.status == "open" and t.assignee is None:
        raise TicketError(f"ticket {t.id} is already open and unassigned")
    _emit_result(_lifecycle(args, sink).release(t, args.agent, args.reason), False)


def cmd_block(args):
    """Block a ticket, ending the attempt that was working it.

    The attempt is *interrupted* rather than released: what stopped the work came from
    outside the worker. Nothing is undone, and the receipt says so, because the next
    worker has to re-read whatever is on disk rather than assume a clean tree."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    _emit_result(_lifecycle(args, sink).block(t, args.reason), False)


def cmd_unblock(args):
    """Clear a block and move the ticket back into play.

    The symmetric counterpart to `block`. Doing this with `set status` leaves
    blocked_by populated, so the ticket claims to be stalled by something in
    every listing while sitting in open -- exactly the frontmatter drift the
    folder-is-truth rule exists to prevent.

    Resuming an assigned ticket starts a *fresh* attempt: blocking ended the one
    that was running, so the worker it goes back to is starting again now, and an
    attempt that resumed under the old id would claim activity it never had."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    if t.status != "blocked":
        raise TicketError(f"ticket {t.id} is not blocked (status: {t.status})")
    reason = t.blocked_by
    was = f" (was blocked by: {reason})" if reason else ""
    message = f"Unblocked: {args.reason}" if args.reason else "Unblocked."
    if reason:
        message = f"{message.rstrip('.')} (was blocked by: {reason})."
    expect = _expect_from(t)
    schema.append_note(t, args.agent, message)
    t.blocked_by = None
    t.updated = schema.now()
    # Back to whoever was working it if it is still assigned, otherwise open.
    dest = "in_progress" if (t.assignee and not args.open) else "open"
    t.status = dest
    if dest == "open":
        t.assignee = None
    sink.update(t, expect=expect)
    if dest == "in_progress":
        lifecycle = _lifecycle(args, sink)
        if lifecycle.active_attempt(t.id) is None:
            lifecycle.begin_attempt(t, t.assignee, ticket_event=None)
    print(f"unblocked {t.id}{was} -> {sink.location(t.id)}")


def cmd_attempt_adopt(args):
    """Adopt a ticket that was already in_progress when attempt tracking began.

    The migration path, and deliberately the only way in: a legacy ticket gets an
    attempt *now*, with no invented history, and the receipt says so. Reconstructing
    activity that never happened would poison every later staleness decision these
    timestamps exist to feed."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    _emit_result(_lifecycle(args, sink).adopt(t, args.agent), args.json)


def cmd_file_claim(args):
    """Claim whole files for one attempt: exclusive writer ownership, all-or-nothing.

    The acquisition is the application layer's (`coordination.claims`): every path is
    canonicalised against the project root, checked against the ticket's own attempt
    and the current claim index, and then written in **one** commit, in canonical path
    order. A path held by another attempt refuses the whole request with the holder
    named and nothing claimed, because the rule the ordering exists for is that two
    agents never end up each holding half of a pair. Nothing here waits, steals or
    retries: contention is a structured answer with exit code 4.

    A claimed path is a *mutation* window, not a lock: the durable claim record is what
    the later write checks, so no process lock is ever held for an agent's work."""
    sink = _require_sink(args)
    result = _claims(args, sink).claim(args.ticket, args.attempt, args.paths)
    _emit_file_result(result, args.json)


def cmd_file_release(args):
    """Release this attempt's claims on whole files, keeping the bytes.

    The explicit, per-file counterpart to the claim: the attempt stays active (this is
    a decision about one file, not about the ticket), the generation is revoked, and
    the released record stays as that path's history. Bytes on disk are never reverted
    by a release -- partial work stays visible to the next worker, who must re-read it.
    A re-acquisition mints a new generation, so any read token from the old one is dead."""
    sink = _require_sink(args)
    result = _claims(args, sink).release(
        args.ticket, args.attempt, args.paths, args.reason
    )
    _emit_file_result(result, args.json)


def cmd_file_claims(args):
    """Report what is held right now: one line per claimed path.

    Read-only and one-shot, so an agent never has to infer ownership from `ls` or from
    a refusal. The active claims by default; `--all` adds the released records, which is
    how "this path was held by att-XXXX until 13:20" stays answerable from the store.
    No claims is an ordinary answer with its own exit code (2), not an error."""
    sink = _require_sink(args)
    result = _claims(args, sink).claims(include_released=args.all)
    _emit_result(result, args.json)
    if result.exit_code:
        sys.exit(result.exit_code)


def cmd_close(args):
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    expect = _expect_from(t)
    t.closed = schema.now()
    t.updated = t.closed
    t.status = "closed"
    sink.update(t, expect=expect)
    print(f"closed {t.id} -> {sink.location(t.id)}")


def cmd_reopen(args):
    """Reopen a ticket that is not currently open, recording why it was rejected.

    The reason is required and is the point of the command: reopening is the
    rejection path back out of review (or out of closed/blocked/shelved), and a
    rejection with no stated reason is useless to whoever has to act on it.
    --reason is enforced by argparse, so a bare `arbite reopen <id>` fails before
    any of this runs, and the note it writes is exactly 'Reopened: <reason>.'
    (timestamped and attributed like every other automatic note). The ticket goes
    back to open with its closed date and any block reason cleared; a ticket that
    is already open is still refused.

    An attempt that is still active ends here, and old attempts and file claims stay
    historical: nothing is re-acquired. Reopening a *prerequisite* is the interesting
    case, so the tickets that depend on this one are handed to the operation as facts
    -- the policy that only a *running* dependent is flagged lives there, and the flag
    is an event rather than an undo."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    if t.status == "open":
        raise TicketError(f"ticket {t.id} is already open")
    dependents = [other for other in _all_tickets(sink) if t.id in other.depends_on]
    result = _lifecycle(args, sink).reopen(
        t, args.agent, args.reason, dependents=dependents
    )
    _emit_result(result, False)


def cmd_submit(args):
    """Hand a finished ticket off: the transition out of the work loop.

    Where it lands is the project's committed answer, read from the `review:` key in
    `.arbite/project.yaml` through `config.review_enabled()`:

    - `review: true` (the default) -- the ticket becomes `review` and lands in
      `review/`, and it **keeps its assignee**: a ticket in review is still owned by
      whoever did the work, because they are the one a reviewer sends it back to.
      From there the rejection path is `arbite reopen --reason ...` and the accepting
      path is `arbite accept`.
    - `review: false` -- the ticket is closed instead, dated and filed exactly as
      `arbite close` does it.

    Either way the ticket gets an automatic note -- 'Submitted for review.' or
    'Submitted; closed (review disabled).' -- with `--message` appended as detail in
    the same shape release/shelve/unblock use. A ticket that is already closed is
    refused: there is nothing left to hand off. Everything else is accepted, because
    submit is a hand-off rather than a validator -- `arbite doctor` is what reports a
    ticket sitting in a status its history does not explain."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    if t.status == "closed":
        raise TicketError(f"ticket {t.id} is already closed")
    expect = _expect_from(t)
    detail = f": {args.message}" if args.message else "."
    if config.review_enabled():
        t.status = "review"
        t.updated = schema.now()
        schema.append_note(t, args.agent, f"Submitted for review{detail}")
        landed = "review"
    else:
        # Exactly what `arbite close` writes: same dating, same status, same move.
        t.closed = schema.now()
        t.updated = t.closed
        t.status = "closed"
        schema.append_note(t, args.agent, f"Submitted; closed (review disabled){detail}")
        landed = "closed"
    sink.update(t, expect=expect)
    print(f"submitted {t.id} -> {sink.location(t.id)} ({landed})")


def cmd_accept(args):
    """Close a ticket that is in review: the reviewer's counterpart to `submit`.

    Only a ticket actually in `review` can be accepted; anything else is refused
    with its real status named, because a general-purpose close is `arbite close`
    and silently closing an `open` ticket here would erase the distinction between
    "reviewed and accepted" and "never reviewed at all". The note is attributed to
    the **accepting** agent (`--agent`), not to the ticket's assignee: the point of
    the record is who approved the work. `--message` is appended as detail, and
    `closed` is dated the same way `arbite close` dates it."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    if t.status != "review":
        raise TicketError(
            f"ticket {t.id} is not in review (status: {t.status}); only a ticket "
            "awaiting review can be accepted (use 'arbite close' to close one)"
        )
    expect = _expect_from(t)
    detail = f": {args.message}" if args.message else "."
    schema.append_note(t, args.agent, f"Accepted{detail}")
    t.closed = schema.now()
    t.updated = t.closed
    t.status = "closed"
    sink.update(t, expect=expect)
    print(f"accepted {t.id} -> {sink.location(t.id)}")


def cmd_shelve(args):
    """Park a ticket, ending the attempt that was working it.

    Shelving is the worker's own decision to stop, so the attempt is *released*
    rather than interrupted -- and nothing on disk is undone."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    _emit_result(_lifecycle(args, sink).shelve(t, args.reason), False)


def cmd_unshelve(args):
    """Bring a shelved ticket back to open/.

    The counterpart to shelve: a ticket that was parked (deprioritized or
    paused) is moved back into the open status so it shows up in
    `arbite list next` again. The assignee and any stale block reason are
    cleared -- an unshelved ticket is back in the unclaimed pool, not reserved
    for whoever parked it."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    if t.status != "shelved":
        raise TicketError(f"ticket {t.id} is not shelved (status: {t.status})")
    expect = _expect_from(t)
    t.updated = schema.now()
    message = "Unshelved." if not args.reason else f"Unshelved: {args.reason}"
    schema.append_note(t, "system", message)
    t.assignee = None
    t.blocked_by = None
    t.status = "open"
    sink.update(t, expect=expect)
    print(f"unshelved {t.id} -> {sink.location(t.id)}")


def cmd_note(args):
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    message = " ".join(args.message)
    expect = _expect_from(t)
    schema.append_note(t, args.agent, message)
    t.updated = schema.now()
    sink.update(t, expect=expect)
    print(f"added note to {t.id} by {args.agent}")


def cmd_show(args):
    sink = _require_sink(args)
    t = sink.get(args.id)
    if args.json:
        _print_json(t.to_dict(sink.location(t.id)))
        return
    print(sink.render(t), end="")


def cmd_deps(args):
    sink = _require_sink(args)
    start = sink.get(args.id)
    by_id = {t.id: t for t in sink.query(TicketQuery(buckets=("*",)))}

    if args.json:
        def node(tid, seen):
            t = by_id.get(tid)
            if t is None:
                # A dangling depends_on id: reported rather than dropped, so a
                # caller can tell "no dependencies" from "dependency deleted".
                return {"id": tid, "missing": True, "depends": []}
            data = t.to_dict()
            data["path"] = None
            if tid in seen:
                data["cycle"] = True
                data["depends"] = []
                return data
            seen = seen | {tid}
            data["depends"] = [node(d, seen) for d in t.depends_on]
            return data

        _print_json(node(start.id, set()))
        return

    def walk(tid, indent, seen):
        t = by_id.get(tid)
        if t is None:
            print(f"{indent}{tid} (missing)")
            return
        print(f"{indent}{t.id} [{t.status}] {t.title}")
        if tid in seen:
            print(f"{indent}  ... (cycle)")
            return
        seen = seen | {tid}
        for dep in t.depends_on:
            walk(dep, indent + "  ", seen)

    walk(start.id, "", set())


def cmd_depend(args):
    """Set or clear a ticket's depends_on. With two arguments, <tic_a> is made to
    depend on <tic_b> (added to its depends_on, deduplicated). With a single
    argument, all of <tic_a>'s dependencies are cleared."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    expect = _expect_from(t)
    if args.dep is None:
        t.depends_on = []
        t.updated = schema.now()
        sink.update(t, expect=expect)
        print(f"cleared dependencies of {t.id}")
        return
    dep = sink.get(args.dep, unique=True)
    if dep.id == t.id:
        raise TicketError(f"ticket {t.id} cannot depend on itself")
    if dep.id not in t.depends_on:
        t.depends_on.append(dep.id)
        t.updated = schema.now()
        sink.update(t, expect=expect)
        print(f"{t.id} now depends on {dep.id}")
    else:
        print(f"{t.id} already depends on {dep.id}")


def cmd_move(args):
    """File a ticket somewhere other than its status location, or bring it back.

    `<folder>` is root-relative, and what it means is the sink's business: the
    file sink moves the ticket's file (`/plans` files it under a folder, created
    if missing; `/plans/ideas` nests one), the SQLite sink records a bucket.
    '/' returns the ticket to where its status says it belongs, which is the only
    way to un-file a ticket without changing its status. Nothing here changes a
    field -- use the status commands (claim/block/close/...) for moves that are
    state changes, which also un-file the ticket automatically."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)

    folder = args.folder.strip()
    if not folder.startswith("/"):
        raise TicketError(
            f"<folder> must be a root-relative path like '/plans', or '/' to return "
            f"the ticket to its status location, got '{args.folder}'"
        )
    # Root-relative -> bucket name through the shared path definition (dropping
    # empty/'.' segments, refusing '..'), so a ticket can never be filed outside
    # the arbite root -- and so a bucket and a reference agree about what a path is.
    parts = _root_relative_segments(folder)
    bucket = "/".join(parts) if parts else None

    if sink.bucket(t.id) == bucket:
        print(f"{t.id} is already at {sink.location(t.id)}")
        return
    sink.move_to_bucket(t.id, bucket)
    print(f"moved {t.id} -> {sink.location(t.id)}")


def cmd_ref_add(args):
    """Append plan references to a ticket, de-duplicated and order-preserving.

    Paths are root-relative documents under the arbite directory ('plans/a.md' ==
    .arbite/plans/a.md); a leading '/' is accepted and stripped, so '/plans/a.md'
    and 'plans/a.md' are the same reference -- the same normalisation `move`
    applies to a bucket (`_normalise_reference`). New entries are appended in the
    order given and one already present is left where it is: re-adding a path is a
    no-op success, not an error. A referenced document need not exist yet, so a
    missing one is a warning on stderr and the write still succeeds (exit 0);
    `arbite doctor` reports it as a problem."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    expect = _expect_from(t)

    named = []
    for raw in args.paths:
        ref = _normalise_reference(raw)
        if ref not in named:
            named.append(ref)

    current = list(t.references)
    added = [ref for ref in named if ref not in current]
    if not added:
        print(f"{t.id} already references {', '.join(named)}")
        return

    t.references = current + added
    t.updated = schema.now()
    sink.update(t, expect=expect)
    _warn_about_missing_references(added, sink)
    print(f"added {', '.join(added)} to {t.id} references")


def cmd_ref_rm(args):
    """Remove plan references from a ticket, refusing a path that is not referenced.

    Every path is checked against the ticket's references before anything is
    written, so a multi-path `rm` that names one path the ticket does not
    reference changes nothing at all -- the all-or-nothing rule `set` applies to
    its property/value pairs, for the same reason: a half-applied removal leaves
    the caller to work out which half happened. Paths are normalised exactly as
    `ref add` normalised them, so a reference can be removed the way it was
    added (including a leading '/')."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    expect = _expect_from(t)

    wanted = []
    for raw in args.paths:
        ref = _normalise_reference(raw)
        if ref not in wanted:
            wanted.append(ref)

    current = list(t.references)
    not_referenced = [ref for ref in wanted if ref not in current]
    if not_referenced:
        raise TicketError(
            f"ticket {t.id} does not reference "
            f"{', '.join(repr(ref) for ref in not_referenced)}; it references "
            f"{', '.join(current) if current else '(nothing)'}"
        )

    t.references = [ref for ref in current if ref not in wanted]
    t.updated = schema.now()
    sink.update(t, expect=expect)
    print(f"removed {', '.join(wanted)} from {t.id} references")


def cmd_ref_list(args):
    """Print a ticket's references, one per line, or as JSON with --json.

    Read-only, so an ambiguous id takes the first match like `show`/`deps` rather
    than being an error. --json emits an object with the ticket `id` and a
    `references` list (field names matching the frontmatter), e.g.
    {"id": "tic-x", "references": ["plans/a.md"]}; an empty list prints nothing in
    text mode, which is the same answer `show` gives by omitting the field."""
    sink = _require_sink(args)
    t = sink.get(args.id)
    if args.json:
        _print_json({"id": t.id, "references": list(t.references)})
        return
    for ref in t.references:
        print(ref)


def _apply_status_change(ticket, new_status):
    """Apply a status change to an already-read ticket, in place.

    The one definition of what changing a status *means*, shared by
    `arbite set <id> status <value>` and `arbite set-status <id> <status>` so the
    two front doors cannot drift. Relocating the ticket is deliberately not done
    here: status -> location is the sink's own side effect of `update()`, and a
    *change* of status is what moves the file into the folder matching the new
    status, un-filing a ticket that was sitting in a bucket. Asking for the status
    a ticket already has is a no-op that returns False and leaves it filed where it
    is -- which is exactly what `arbite set <id> status <value>` already did, so the
    two front doors stay identical. Moving to `closed` auto-dates `closed`, the same
    way `arbite close` does.

    Returns True when the status actually changed."""
    if new_status == ticket.status:
        return False
    if new_status == "closed" and ticket.closed is None:
        ticket.closed = ticket.updated
    ticket.status = new_status
    return True


def cmd_set(args):
    """Set one or more ticket properties on an existing ticket. Properties come in
    PROPERTY VALUE pairs (any number per call); quote any value that spans more
    than one word. Type-aware: 'tags'/'depends_on'/'references' are
    comma-separated lists, 'priority' must be an integer, and an empty quoted
    value ('') clears a field. A 'status' change also un-files the ticket (the
    file sink moves it) so status and location stay in sync, and auto-dates
    'closed' when a ticket is set to closed. Setting 'status' is the same change
    as `arbite set-status <id> <status>`, through one shared code path, so the
    two front doors cannot drift; `set-status` is the dedicated front door for
    the statuses no work-flow command reaches (the escape hatch)."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    assignments = args.assignments
    if len(assignments) % 2 != 0:
        raise TicketError(
            "properties must come in PROPERTY VALUE pairs -- got an odd number of "
            f"arguments: {' '.join(assignments)}"
        )
    pairs = list(zip(assignments[::2], assignments[1::2]))

    # Validate every property name and value before mutating anything, so a bad
    # call leaves the ticket untouched.
    for prop, value in pairs:
        if prop not in schema.SETTABLE_PROPERTIES:
            raise TicketError(
                f"unknown ticket property '{prop}' "
                f"(valid: {', '.join(sorted(schema.SETTABLE_PROPERTIES))})"
            )
        schema.validate_field(prop, value)

    # The lifecycle guards come next, still before any write: a status or assignee
    # change that would strand an active attempt is refused with the command that
    # owns the transition (LC5's rule, and the reason `force` has no backdoor).
    lifecycle = _lifecycle(args, sink)
    for prop, value in pairs:
        if prop == "status":
            lifecycle.guard_status_change(t, value)
        elif prop == "assignee":
            lifecycle.guard_assignee_change(t, schema.coerce_field_value("assignee", value))

    expect = _expect_from(t)
    new_status = None
    updated_given = False
    for prop, value in pairs:
        if prop == "status":
            # Left unapplied here and done below through the shared path, so
            # that `set status` and `set-status` cannot drift -- and so the
            # ticket's *previous* status is still readable when deciding whether
            # to date `closed`.
            new_status = value
            continue
        if prop == "updated":
            updated_given = True
        setattr(t, prop, schema.coerce_field_value(prop, value))

    if not updated_given:
        t.updated = schema.now()

    if new_status is not None:
        # A real status change also re-files the ticket (the file sink relocates
        # the file, the database sink clears its bucket), mirroring
        # claim/close/etc.; moving to closed auto-dates 'closed' like
        # `arbite close` does.
        _apply_status_change(t, new_status)

    sink.update(t, expect=expect)
    if any(prop == "references" for prop, _ in pairs):
        # The same write-time warning `ref add` gives, from the same helper: a
        # reference may legitimately point at a plan not written yet, so this
        # warns and the write stands. `arbite doctor` reports it as a problem.
        _warn_about_missing_references(t.references, sink)
    what = ", ".join(prop for prop, _ in pairs)
    print(f"set {what} on {t.id} at {sink.location(t.id)}")


def cmd_set_status(args):
    """Change one ticket's status: the dedicated front door for the same change
    `arbite set <id> status <value>` makes, writing through the same code path so
    the two cannot drift.

    Additive, not a replacement -- `arbite set status <value>` keeps working. Both
    funnel through `_apply_status_change`, so the two cannot drift: a status change
    lands the ticket in the folder matching the new status, which un-files a ticket
    that was sitting in a bucket so status and location cannot disagree, and moving
    to `closed` auto-dates `closed`. Asking for the status a ticket already has is a
    no-op that leaves it filed where it is -- identical to `arbite set <id> status
    <value>` with the same argument.

    This is the escape hatch rather than a workflow front door: it deliberately
    does *not* refuse transitions that dedicated commands exist for
    (claim/close/submit/accept). The point of it is reaching the rest of the
    vocabulary -- notably `review`, which no other command sets yet -- and
    `arbite doctor` is what catches the drift those dedicated commands prevent. The
    status vocabulary comes from `schema.STATUSES`, so a status added later is
    accepted here without this command changing.

    One thing it is not is a backdoor around a *running* attempt: a status whose
    lifecycle command exists is refused when an attempt is active, with that command
    named, because the attempt has to end with the ticket rather than being left
    behind by a setter."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    _lifecycle(args, sink).guard_status_change(t, args.status)
    expect = _expect_from(t)
    t.updated = schema.now()
    _apply_status_change(t, args.status)
    sink.update(t, expect=expect)
    print(f"set status on {t.id} at {sink.location(t.id)}")


def cmd_search(args):
    """Search every ticket for the given text, optionally restricted to specific
    fields with --params (comma-separated; 'body' = the rest of the ticket, 'all' =
    every field plus the body, the default). Matching is a case-insensitive substring
    by default; -w adds simple wildcards ('*' = any text) and -r treats the text as a
    regular expression."""
    sink = _require_sink(args)
    params = _split_csv(args.params) or ["all"]
    mode = "regex" if args.regex else "wildcard" if args.wildcard else "substring"
    text = TextMatch(" ".join(args.search_text), mode=mode, fields=tuple(params))

    q = TicketQuery(
        status=tuple(args.status or ()),
        text=text,
        order="flat",
    )
    _emit_tickets(sink.query(q), args.json, sink)


def cmd_doctor(args):
    """Check the invariants nothing else enforces, and optionally repair them.

    Two families of check, reported as one list. The ticket checks live in the sink, because
    part of the point of a pluggable store is that its failure modes differ: a file sink can
    suffer frontmatter/folder drift, a stray temp file or an archive in the wrong month, while
    a database sink can suffer a stale derived index or structural corruption. The checks that
    mean the same thing either way -- invalid field values, deadlocked dependencies, a claimed
    ticket with no assignee -- are shared, so `doctor` cannot mean two different things per
    sink.

    The coordination checks are the recovery engine's (`coordination.recovery`): claims nobody
    can use, unfinished file operations judged against the bytes on disk, and records that name
    something missing. Without `--fix` they are only *judged* -- nothing is written, which is
    why a reconcilable unfinished operation is not reported as a problem at all -- and with it
    the two unambiguous repairs are made: a dead attempt's claim is released, and an operation
    whose bytes are exactly one of its two recorded versions is finalised. Bytes that match
    neither are reported, with all three versions, and left alone.

    Exits 3 when problems remain, so this can gate CI or an agent's startup."""
    sink = _require_sink(args)
    problems = sink.check(fix=args.fix)
    info = sink.describe()
    app = _coordination_app(args, sink)
    tickets = [ticket.id for ticket in _all_tickets(sink)]
    if args.fix:
        problems.extend(
            coordination_recovery.repair(
                app.store,
                ticket_ids=tickets,
                root=app.project_root,
                known_command=knows_command,
            )
        )
    else:
        problems.extend(
            coordination_recovery.findings(app.store, ticket_ids=tickets)
        )
    fixed = sum(1 for p in problems if p.fixed)
    remaining = sum(1 for p in problems if not p.fixed)
    # The scratch area is *reported*, not judged: a leftover payload is what an interrupted
    # run leaves and the only copy of a change somebody may still need, so it is a fact about
    # the store rather than a problem with it, and it never changes the exit code by itself.
    notes = note_lines(
        scratch_summary(app.arbite_dir),
        guidance=not args.fix,
        clear_command="scratch clear" if knows_command("scratch clear") else None,
    )

    if args.json:
        # Which coordination backend is in use, and what is in it, is part of the report
        # because a project whose tickets and whose claims live in different places needs to
        # be able to say so out loud.
        facts = app.doctor_facts()
        _print_json(
            {
                "sink": {"kind": info.kind, "root": info.root},
                "coordination": facts["coordination"],
                "tickets_checked": info.ticket_count,
                "problems": [p.to_dict() for p in problems],
                "fixed": fixed,
                "remaining": remaining,
                "scratch": facts["scratch"],
            }
        )
    else:
        # The note sits with the findings when there are any and closes a clean report --
        # the shape the frozen DR1/DR2 and DR3 blocks print.
        if problems:
            for p in problems:
                prefix = "fixed" if p.fixed else "problem"
                where = f" [{p.ticket_id}]" if p.ticket_id else ""
                print(f"{prefix}{where} {p.kind}: {p.detail}")
            for line in notes:
                print(line)
            print(
                f"checked {info.ticket_count} tickets: {remaining} problem(s), {fixed} fixed"
            )
            if remaining and not args.fix:
                print("re-run with --fix to repair what arbite can correct automatically")
        else:
            print(f"checked {info.ticket_count} tickets: no problems found")
            for line in notes:
                print(line)

    if remaining:
        sys.exit(EXIT_PROBLEMS)


def cmd_delete(args):
    """Delete a ticket outright.

    Gated behind --force because it is irreversible, and it records a
    'Deleted by <agent>' note immediately before removing the ticket so the last
    state a ticket ever had is attributable. The receipt (id, title, location,
    note) is printed and included in --json, so a calling agent can log what it
    destroyed even though the ticket is gone."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    if not args.force:
        raise TicketError(
            f"refusing to delete {t.id} without --force (deletion is irreversible; "
            f"'arbite close {t.id}' archives it instead)"
        )
    note = f"Deleted by {args.agent}" + (f": {args.reason}" if args.reason else ".")
    location = sink.location(t.id)
    if not args.dry_run:
        expect = _expect_from(t)
        schema.append_note(t, args.agent, note)
        t.updated = schema.now()
        sink.update(t, expect=expect)
        sink.remove(t.id)
    if args.json:
        payload = t.to_dict(location)
        payload.update({"deleted": not args.dry_run, "note": note})
        _print_json(payload)
    else:
        verb = "would delete" if args.dry_run else "deleted"
        print(f"{verb} {t.id} ({t.title}) from {location}")


def cmd_migrate(args):
    """Copy every ticket from one sink into another.

    A copy, never a move (unless --prune asks for the cleanup), so migrating is
    reversible. Reading every ticket from one sink and writing it to the other
    exercises the whole interface -- ids, timestamps, body, tags, dependencies,
    notes, buckets -- which is why this command doubles as the end-to-end proof
    that two independent sinks agree.

    A successful migration also makes the destination the project default: the
    tickets live there now, and leaving `sink:` pointing at the store you migrated
    away from is the same silent-wrong-store trap `init` closes."""
    project_root = config.find_project_root()
    source = config.open_sink(args.from_sink, project_root)
    target = config.open_sink_kind(args.to_sink, project_root)
    if source.kind == str(target.kind) and str(source.root) == str(target.root):
        # The common way to arrive here: `migrate --to sqlite` after switching the
        # config to sqlite, meaning "bring my file tickets across". Name the
        # source instead of leaving the caller to work out what went wrong.
        others = [k for k in SINK_KINDS if k != source.kind]
        hint = (
            f"pass --from {others[0]} to copy from that sink"
            if args.from_sink is None and others
            else "pass --from or --to with a different kind"
        )
        raise TicketError(
            f"source and destination are both the {source.kind} sink at {source.root} "
            f"-- {hint}"
        )

    tickets = source.query(TicketQuery(buckets=("*",)))
    if not tickets:
        print(f"no tickets found in the {source.kind} sink at {source.root}")
        sys.exit(EXIT_EMPTY)

    if args.dry_run:
        present = (
            sum(1 for t in tickets if target.exists(t.id)) if _target_exists(target) else 0
        )
        held_back = present if not args.overwrite else 0
        print(
            f"would migrate {len(tickets)} ticket(s) from {source.kind} to {target.kind} "
            f"at {target.root}"
            + (f" ({held_back} already present, skipped)" if held_back else "")
        )
        if args.prune:
            if held_back:
                print(
                    f"would NOT prune: {held_back} ticket(s) already present, so the source "
                    "copies are the newest -- pass --overwrite to replace them first"
                )
            else:
                print(f"would prune {len(tickets)} ticket(s) from the {source.kind} sink")
        return

    target.init()
    migrated = 0
    overwritten = 0
    skipped = []
    for t in tickets:
        if target.exists(t.id):
            if not args.overwrite:
                skipped.append(t.id)
                continue
            target.remove(t.id)
            overwritten += 1
        target.create(t)
        bucket = source.bucket(t.id)
        if bucket:
            target.move_to_bucket(t.id, bucket)
        migrated += 1

    summary = (
        f"migrated {migrated} ticket(s) from {source.kind} to {target.kind} at {target.root}"
    )
    if overwritten:
        summary += f" ({overwritten} overwritten)"
    if skipped:
        summary += f" ({len(skipped)} already present, skipped: {', '.join(sorted(skipped))})"
    print(summary)

    if args.prune:
        # The copy is done; pruning is the destructive half, and it is refused
        # outright when it would leave a *stale* copy as the only copy. Nothing is
        # removed before that check, so a refused prune still leaves both stores
        # intact -- the migration simply happened without the cleanup.
        if skipped:
            raise TicketError(
                f"refusing to prune: {len(skipped)} ticket(s) already existed in the "
                f"{target.kind} sink and were left as they were "
                f"({', '.join(sorted(skipped))}), so the source copies are the newer ones -- "
                "re-run with --overwrite to replace them, then prune"
            )
        for t in tickets:
            source.remove(t.id)
        print(f"pruned {len(tickets)} ticket(s) from the {source.kind} sink at {source.root}")

    # The destination holds the tickets now, so it becomes the project default --
    # otherwise the next plain command would read the store you just migrated away
    # from, which is the same silent-wrong-store trap `init` closes above.
    configured = config.configured_sink_spec(project_root)
    if configured.kind != target.kind:
        written = config.set_configured_sink(target.kind, project_root)
        print(
            f"set 'sink: {target.kind}' in {written.name} -- commands now read the store "
            "migrated into"
        )


def _target_exists(target) -> bool:
    """Whether the migration target already holds tickets. Used by --dry-run,
    which must not create the target's store merely to report on it."""
    try:
        return bool(target.ids())
    except ArbiteError:
        return False


def build_parser():
    """Returns (parser, subparsers_by_name). The dict is used by `arbite init` to
    render .arbite/AGENTS.md's command reference straight from these parsers
    (every usage line and flag, compacted rather than dumped as --help text), so
    that doc can't drift from the real CLI."""
    parser = argparse.ArgumentParser(prog="arbite", description="Ticket sink CLI")
    parser.add_argument("--version", action="version", version=f"arbite {__version__}")
    _sink_flag(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p_init = sub.add_parser(
        "init",
        help="create the arbite directory and initialise the selected sink",
        description="Create .arbite/ in the current directory (like 'git init'), initialise "
        "the selected sink (status folders and buckets for the file sink; the database and "
        "its schema for the sqlite sink), write .arbite/AGENTS.md (the command reference, not "
        "auto-discovered -- point your project's CLAUDE.md or similar at it explicitly if you "
        "want agents to find arbite, or pass --agents-doc/--claude-doc to have the instructions "
        "block installed into those files for you), and pre-create a scratchpad file under .arbite/agents/ "
        "for every id listed in an 'agents:' list in .arbite/project.yaml, if present. Which sink is "
        "initialised follows the usual precedence: --sink, then ARBITE_SINK, then a 'sink:' key "
        "in .arbite/project.yaml, then file. The store it creates then becomes the project default -- "
        "'sink:' is written to .arbite/project.yaml, created if it does not exist, so no later command "
        "reads a different store by accident (an ARBITE_SINK selection is reported instead, "
        "since that was this process's decision rather than the project's). Running it again is "
        "safe: it never destroys data.",
    )
    p_init.add_argument(
        "--agents-doc",
        action="store_true",
        help="create ./AGENTS.md, or prepend the arbite instructions block to it if the "
        "file exists without that block, so a harness that reads AGENTS.md finds arbite",
    )
    p_init.add_argument(
        "--claude-doc",
        "--claud-doc",
        dest="claude_doc",
        action="store_true",
        help="create ./CLAUDE.md, or prepend the arbite instructions block to it if the "
        "file exists without that block, so Claude Code finds arbite ('--claud-doc' is "
        "accepted as an alias)",
    )
    _sink_flag(p_init)
    p_init.set_defaults(func=cmd_init)

    p_sink = sub.add_parser(
        "sink",
        help="show or initialise the active sink",
        description="Report which sink is in use, where its store lives, and what it supports "
        "(status_is_location, buckets, per-status ticket counts), or with 'init' create the "
        "store for the selected sink. The sink is chosen by --sink, then ARBITE_SINK, then a "
        "'sink:' key in .arbite/project.yaml, then the default (file). This describes the "
        "*store*; to read the backlog itself -- a count per status, with filters and --json -- "
        "use 'arbite status', which shares this command's counting implementation so the two "
        "cannot disagree.",
    )
    p_sink.add_argument(
        "subcommand",
        nargs="?",
        choices=["info", "init"],
        metavar="SUBCOMMAND",
        help="'info' (default) reports the active sink; 'init' creates its store if missing",
    )
    _sink_flag(p_sink)
    _json_flag(p_sink)
    p_sink.set_defaults(func=cmd_sink)

    p_workspace = sub.add_parser(
        "workspace",
        help="report the workspace this project derives (show)",
        description="Report the workspace this project *is*: the id derived from the located "
        ".arbite/ directory plus the resolved sink, the project root, which store is in use "
        "and where that choice came from, the coordination backend's location and what it "
        "currently holds, and the state of the scratch payload area. There is deliberately no "
        "`bind`: a workspace is derived, never bound, so a relocated root or a repointed store "
        "is a new workspace rather than a mutation of an old one, and there is no conflict "
        "path. Read-only: `workspace show` writes nothing at all, not even the workspace "
        "record (that is `arbite init`'s job), so two runs on unchanged state print identical "
        "text.",
    )
    workspace_sub = p_workspace.add_subparsers(
        dest="workspace_action", required=True, metavar="SUBCOMMAND"
    )
    _sink_flag(p_workspace)
    p_workspace_show = workspace_sub.add_parser(
        "show",
        help="show the derived workspace, its store and its coordination state",
        description="Print the workspace id, the project root, the ticket store in use (with "
        "where that selection came from), the coordination backend's location and counts "
        "(active claims, events, receipts), and the scratch payload area's file count and "
        "size. Every fact is printed in text and present in `--json`, and the exit code is "
        "always 0: 'nothing is happening' is a first-class answer, not an error.",
    )
    _json_flag(p_workspace_show)
    _sink_flag(p_workspace_show)
    p_workspace_show.set_defaults(func=cmd_workspace_show)

    p_attempt = sub.add_parser(
        "attempt",
        help="work-attempt operations: record the attempt for already-started work",
        description="Report the *attempt* side of a ticket: the durable record of one "
        "worker's run at it, which is what file ownership and change evidence hang off. "
        "'adopt' is the migration path for a ticket that was already in_progress when "
        "arbite began tracking attempts -- it records an attempt starting *now*, and says "
        "so, because inventing activity that happened before tracking would poison every "
        "later staleness decision these timestamps exist to feed. Every other attempt is "
        "created by the command that acquires the work ('claim', 'list next --claim', "
        "'promote --agent'), which is where readiness and the one-active-attempt rule are "
        "enforced.",
    )
    attempt_sub = p_attempt.add_subparsers(
        dest="attempt_action", required=True, metavar="SUBCOMMAND"
    )
    _sink_flag(p_attempt)
    p_attempt_adopt = attempt_sub.add_parser(
        "adopt",
        help="record the attempt for a ticket that was already in_progress",
        description="Create the attempt that owns work already underway. The ticket has to be "
        "in_progress, and it must not already have an active attempt or be assigned to a "
        "different worker -- that is the takeover, and 'arbite claim --force --reason' is "
        "what owns it. The attempt's timestamps start now, and the receipt states that no "
        "earlier activity is implied by it.",
    )
    p_attempt_adopt.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_attempt_adopt.add_argument(
        "--agent",
        required=True,
        help="agent id the adopted attempt is attributed to; when the ticket has an "
        "assignee it must be that id (required)",
    )
    _json_flag(p_attempt_adopt)
    _sink_flag(p_attempt_adopt)
    p_attempt_adopt.set_defaults(func=cmd_attempt_adopt)

    p_file = sub.add_parser(
        "file",
        help="file ownership: claim whole files, release them, list what is held",
        description="The durable half of the file proxy: which work attempt owns which whole "
        "file. A claim is what a later read, write, edit or rename is checked against -- it is "
        "a record, not a lock, so no lock is ever held for the length of an agent's work. "
        "Acquisition is all-or-nothing and in canonical path order, so two attempts can never "
        "end up each holding half of a pair; contention is a structured busy answer (exit 4) "
        "naming the holder, never a wait. Paths are validated against the project root: escapes, "
        "arbite's own state and `.git` metadata are refused. Reads, writes, edits, renames and "
        "removes are not here yet -- they arrive with their own slices.",
    )
    file_sub = p_file.add_subparsers(
        dest="file_action", required=True, metavar="SUBCOMMAND"
    )
    _sink_flag(p_file)
    p_file_claim = file_sub.add_parser(
        "claim",
        help="claim one or more whole files for an attempt (all-or-nothing)",
        description="Acquire exclusive ownership of every named path for one work attempt, or "
        "none of them. The set is canonicalised and sorted first, then written in one commit: a "
        "path another attempt holds refuses the whole request with the holder named and nothing "
        "claimed. A path that does not exist yet may be claimed -- creating a file is an "
        "explicit, claimable act, and the claim records the absent version. A claim is required "
        "before any mutation, and the read that authorises the mutation must happen *after* the "
        "claim.",
    )
    p_file_claim.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="one or more paths relative to the project root (absolute paths inside the root are "
        "accepted and canonicalised; escapes, `.git` metadata and arbite's own state are refused)",
    )
    p_file_claim.add_argument(
        "--ticket",
        required=True,
        metavar="TICKET_ID",
        help="the ticket the claiming attempt owns; the claim is recorded against it and its "
        "attempt (required)",
    )
    p_file_claim.add_argument(
        "--attempt",
        required=True,
        metavar="ATTEMPT_ID",
        help="the attempt (`att-XXXX`) that owns the ticket and will own the claim; `arbite claim "
        "` prints it, and an attempt that does not own the ticket is refused (required)",
    )
    _json_flag(p_file_claim)
    _sink_flag(p_file_claim)
    p_file_claim.set_defaults(func=cmd_file_claim)

    p_file_release = file_sub.add_parser(
        "release",
        help="release this attempt's claim on whole files, keeping the bytes",
        description="Revoke the claim generation on each named path and leave the file where it "
        "is. The attempt stays active: releasing a file is a decision about that file, not about "
        "the ticket. Nothing on disk is reverted and the released record stays as the path's "
        "history, so the next worker reads current bytes rather than an assumed clean tree. "
        "Re-claiming the path later mints a new generation, which is what makes a read token "
        "from the old one dead.",
    )
    p_file_release.add_argument("paths", nargs="+", metavar="PATH", help="paths this attempt holds")
    p_file_release.add_argument(
        "--ticket", required=True, metavar="TICKET_ID", help="the ticket the attempt owns (required)"
    )
    p_file_release.add_argument(
        "--attempt",
        required=True,
        metavar="ATTEMPT_ID",
        help="the attempt holding the claims; only the holder may release them (required)",
    )
    p_file_release.add_argument(
        "--reason",
        required=True,
        metavar="TEXT",
        help="why the path is being handed back; recorded with the released claim and in its "
        "event, so the next worker knows what to re-read (required)",
    )
    _json_flag(p_file_release)
    _sink_flag(p_file_release)
    p_file_release.set_defaults(func=cmd_file_release)

    p_file_claims = file_sub.add_parser(
        "claims",
        help="list the file claims in this workspace (who holds what, right now)",
        description="Report the current claim index in canonical path order: per path the holder "
        "ticket and attempt, the worker, the claim generation, when it was taken and the version "
        "it was taken against. `--all` adds the released records. Read-only and one-shot, so "
        "'who holds what' never has to be inferred from `ls` or from a refusal; no active claims "
        "exits 2.",
    )
    p_file_claims.add_argument(
        "--all",
        action="store_true",
        help="include released claims (the path's history) as well as the active ones",
    )
    _json_flag(p_file_claims)
    _sink_flag(p_file_claims)
    p_file_claims.set_defaults(func=cmd_file_claims)

    p_events = sub.add_parser(
        "events",
        help="read the coordination event stream: what happened, one line per event",
        description="Print the coordination event stream in cursor order: one line per event "
        "with its cursor, kind, subject, operation, ticket/attempt, actor, local time and "
        "outcome, and the cursor to resume from. Read observations are excluded unless "
        "--include-reads is given, because a research-heavy agent emits dozens of reads per "
        "write. The command is one-shot: --follow is refused, so a poll keeps its own cursor "
        "and calls again, and 'nothing new' exits 2.",
    )
    p_events.add_argument(
        "--after",
        type=int,
        metavar="CURSOR",
        default=None,
        help="print selected events after this cursor, and report the cursor to resume from",
    )
    p_events.add_argument(
        "--tail",
        type=int,
        metavar="N",
        default=None,
        help=f"print the last N selected events (a bare 'arbite events' prints the last "
        f"{coordination_app.DEFAULT_EVENT_TAIL})",
    )
    p_events.add_argument(
        "--include-reads",
        action="store_true",
        help="include read observations, which the default view excludes",
    )
    p_events.add_argument(
        "--follow",
        action="store_true",
        help="refused: arbite commands are one-shot and never block -- poll with '--after <cursor>'",
    )
    _json_flag(p_events)
    _sink_flag(p_events)
    p_events.set_defaults(func=cmd_events)

    p_status = sub.add_parser(
        "status",
        help="count tickets per status (the shape of the backlog, not 'set status')",
        description="Report how many tickets sit in each status: the whole backlog at a glance. "
        "Every status in the vocabulary is printed in its canonical order (raw, open, "
        "in_progress, review, blocked, shelved, closed) -- including the ones with a count of "
        "zero, so an empty status is visibly empty rather than absent -- followed by a total. "
        "'--json' emits a mapping of status to count plus 'total'. The usual filters narrow the "
        "counts, so 'arbite status --epic workflow' answers 'how far along is this epic'. This "
        "is a report, not a query: it always exits 0, even when the store is empty or a filter "
        "matches nothing (the '2 = nothing matched' convention does not apply). Tickets filed "
        "in a bucket (wishlist/plans) are out of the status workflow, so they are not counted "
        "under the status they retain -- exactly as 'arbite list' excludes them, which is what "
        "makes the two agree. Three things this is not: not 'arbite set <id> status <value>' "
        "(which changes one ticket's status), not the '--status' filter on list/search (which "
        "selects tickets), and not 'arbite sink info' (which describes the store -- its kind, "
        "root and capabilities -- rather than the backlog). There is deliberately no --status "
        "filter here: counting every status at once is this command's whole job.",
    )
    p_status.add_argument(
        "--epic", help="only count tickets in this epic, e.g. 'workflow' (narrows every count)"
    )
    p_status.add_argument("--domain", help="only count tickets in this domain")
    p_status.add_argument(
        "--tier",
        choices=TIERS,
        help=f"only count tickets at this agent capability tier. {schema.TIER_HELP}",
    )
    p_status.add_argument("--assignee", help="only count tickets assigned to this agent id")
    _json_flag(p_status)
    _sink_flag(p_status)
    p_status.set_defaults(func=cmd_status)

    p_progress = sub.add_parser(
        "progress",
        help="show live epics: what is in flight, plus the epic's other tickets",
        description="Report what is actually in flight, and the epics it sits in. The "
        "live tickets are those whose status is open, in_progress or review; the epics in "
        "scope are the ones holding at least one of them; and then every ticket of those "
        "epics is shown, whatever its status -- closed and shelved siblings included, "
        "because they are the context that makes the live ticket legible. An epic whose "
        "tickets are all closed never appears at all. Live tickets with no epic are "
        "grouped under a 'no epic' heading rather than dropped. Within an epic, tickets "
        "are ordered topologically by depends_on (the same ordering 'list next' uses) and "
        "an unsatisfiable dependency cycle is warned about on stderr, as the other "
        "listings do. Each epic gets a count line (e.g. '1 open / 1 in_progress / 6 "
        "closed') so progress is readable without counting rows. Tickets filed in a bucket "
        "(wishlist/plans) are out of the status workflow, so they are not live and cannot "
        "put an epic in scope. This is not 'arbite status' (which counts the whole backlog "
        "per status) and not 'arbite list --topo' (which shows a filtered selection rather "
        "than an epic's full membership).",
    )
    p_progress.add_argument(
        "--epic",
        help="only show this epic, e.g. 'workflow'; it still appears only if it holds "
        "a live ticket, so this narrows the report rather than forcing an empty one",
    )
    _json_flag(p_progress)
    _sink_flag(p_progress)
    p_progress.set_defaults(func=cmd_progress)

    p_create = sub.add_parser(
        "create",
        help="create a new ticket in the open status",
        description="Create a new ticket with a generated id (tic-XXXX). "
        "--title/--type/--tier/--domain are required unless --blank is given.",
    )
    p_create.add_argument("--title", help="short ticket title (required unless --blank)")
    p_create.add_argument(
        "--type",
        choices=schema.CREATE_TYPES,
        help="kind of work (required unless --blank)",
    )
    p_create.add_argument(
        "--tier",
        choices=TIERS,
        help="agent capability tier required to work this ticket "
        f"({schema.TIER_VALUES}). {schema.TIER_HELP} "
        "Required unless --blank.",
    )
    p_create.add_argument(
        "--domain",
        help="what kind of agent/tool this needs, e.g. mesh, image_gen, audio_gen, ui, io (required unless --blank)",
    )
    p_create.add_argument(
        "--epic",
        help="larger initiative this ticket belongs to, e.g. 'mesh-pipeline' (optional; "
        "group tickets under an epic with `arbite list --epic` / `arbite list next --epic`)",
    )
    p_create.add_argument(
        "--priority",
        type=int,
        default=None,
        help="numeric urgency index, lower = more urgent (e.g. 1 is highest priority); "
        "used to decide which workable ticket to pick up first",
    )
    p_create.add_argument(
        "--tags", default="", help="comma-separated, freeform, for codebase-area search, e.g. 'normals,curves'"
    )
    p_create.add_argument(
        "--depends-on", default="", help="comma-separated ticket ids that must close before this one, e.g. 'tic-a1b2,tic-c3d4'"
    )
    p_create.add_argument(
        "--references", default="", help="comma-separated plan documents under the arbite root's plans/ bucket, "
        "stored root-relative -- e.g. 'plans/review-workflow.md' (.arbite/plans/review-workflow.md), not a "
        "repo-root path; the referenced file need not exist yet"
    )
    p_create.add_argument("--description", default="", help="free-text body under the '## Description' heading")
    p_create.add_argument(
        "--blank",
        action="store_true",
        help="scaffold a blank template ticket for a human to fill in by hand: fills only the "
        "structural fields (id/status/created/updated), leaves --title/--type/--tier/--domain as "
        "TODO placeholders if not given, and adds a body warning telling agents not to claim it "
        "until it's been filled in and saved",
    )
    _sink_flag(p_create)
    p_create.set_defaults(func=cmd_create)

    p_raw = sub.add_parser(
        "raw",
        help="create an unclassified raw ticket from a brief request",
        description="Capture a brief request as a raw ticket, with status 'raw' (so "
        "it never shows up in 'arbite list next'). Sets the type and title to '<type> (raw): "
        "Requires Classification', leaves tier/domain/priority as TODO placeholders, "
        "auto-groups the ticket under the 'classification' epic (so triage/classification "
        "jobs can find it with `arbite list next --epic classification`, or pull the oldest "
        "one with `arbite fetch`), and writes a body explaining that the ticket must be "
        "filled out (a real title, tier, domain, epic, priority, and an expanded description) "
        "before it can be claimed or worked. Use 'memo' when the request is to update "
        "project notes / documentation rather than make a code change. Use 'request' for a "
        "request for a change that is not necessarily a bug or a new feature -- a tweak or "
        "lateral change to something that already exists; it is ordinary work once "
        "classified. Use 'wish' for a wishlist item: the ticket notes that wishlist items "
        "are reclassified as 'feature' and filed in the wishlist bucket rather than opened "
        "as work. Classification is the write half of triage and is done with 'arbite "
        "promote <id> --title ... --tier ... --domain ...' ('arbite fetch' is the read-only "
        "queue that hands the oldest raw ticket out).",
    )
    p_raw.add_argument(
        "type",
        choices=schema.RAW_TYPE_CHOICES,
        metavar="TYPE",
        help=f"kind of raw ticket: {' | '.join(schema.RAW_TYPE_CHOICES)}",
    )
    p_raw.add_argument("message", metavar="MESSAGE", nargs="+", help=docs.MESSAGE_HELP)
    _sink_flag(p_raw)
    p_raw.set_defaults(func=cmd_raw)

    # Shorthand subcommands: `arbite bug <message>` == `arbite raw bug <message>`,
    # and so on for each RAW_TYPE_CHOICES type. The type is baked in as a parser
    # default, so cmd_raw is reused as-is and the ticket is byte-for-byte identical
    # to the long form.
    for _name, _help in RAW_SHORTCUT_HELP.items():
        p_shortcut = sub.add_parser(
            _name,
            help=_help,
            description=f"Shorthand for 'arbite raw {_name} <message>': capture a raw "
            f"{_name} ticket exactly as that command would, in one shorter word. Creates a "
            "status 'raw' ticket with the type set accordingly, grouped under the "
            "'classification' epic; classify it (title/tier/domain/epic/priority/description) "
            "before it can be claimed or worked.",
        )
        p_shortcut.add_argument("message", metavar="MESSAGE", nargs="+", help=docs.MESSAGE_HELP)
        _sink_flag(p_shortcut)
        p_shortcut.set_defaults(func=cmd_raw, type=_name)

    p_fetch = sub.add_parser(
        "fetch",
        help="pull the oldest raw ticket for classification (optionally by type)",
        description="Pull the next raw ticket (status 'raw'), oldest first by creation "
        "date, optionally restricted to TYPE. Prints it the same as 'arbite show' (raw "
        "markdown, or --json), with a 'derived_note' injected at the top -- as a JSON "
        "field in --json mode, a leading block in text mode -- telling the caller how to "
        "classify the ticket (title/tier/domain/epic/priority/description) with 'arbite "
        "promote', which snapshots the capture to raw/processed/ and moves the ticket to "
        "'open', or to 'in_progress' for the agent that classifies it with --agent. fetch "
        "itself is strictly read-only: it is the triage queue, promote is the write half. A "
        "wish raw ticket is classified differently: 'arbite promote' reclassifies it as "
        "'feature' and files it in the wishlist bucket instead of opening or claiming it. "
        "Exits 2 if no raw ticket matches.",
    )
    p_fetch.add_argument(
        "type",
        nargs="?",
        default=None,
        choices=schema.RAW_TYPE_CHOICES,
        metavar="TYPE",
        help=f"restrict to raw tickets of this type: "
        f"{' | '.join(schema.RAW_TYPE_CHOICES)} (default: any type)",
    )
    _json_flag(p_fetch)
    _sink_flag(p_fetch)
    p_fetch.set_defaults(func=cmd_fetch)

    p_promote = sub.add_parser(
        "promote",
        help="classify a raw ticket and make it workable (the write half of triage)",
        description="Turn a raw capture (status 'raw') into a classified, workable ticket "
        "in one command -- the write half of triage, where 'arbite fetch' is the read-only "
        "queue. Writes a verbatim snapshot of the capture to raw/processed/<id>.raw.md "
        "first, by exclusive create: a snapshot is frozen audit history and is never "
        "overwritten, so promoting the same id twice is an error. It then classifies the "
        "ticket in place, carrying the id -- and so any note, dependency or reference that "
        "already names it -- forward. --title/--tier/--domain are required, and a title "
        "still in the raw placeholder form or a TODO: tier/domain is refused, naming the "
        "offending field; an omitted --epic clears the 'classification' epic. The ticket "
        "moves to 'open', or straight to 'in_progress' assigned to --agent when the same "
        "agent is going to work it. A wish is the exception: it is reclassified as 'feature' "
        "and filed in the wishlist bucket with its status left at 'raw' (so it leaves the "
        "triage queue without becoming work), and --agent is refused for it.",
    )
    p_promote.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_promote.add_argument(
        "--title",
        help="short human-readable ticket title (required; may not be left as the raw "
        "capture's placeholder)",
    )
    p_promote.add_argument(
        "--tier",
        help="agent capability tier required to work this ticket "
        f"({schema.TIER_VALUES}). {schema.TIER_HELP} Required.",
    )
    p_promote.add_argument(
        "--domain",
        help="what kind of agent/tool this needs, e.g. mesh, image_gen, audio_gen, ui, io "
        "(required)",
    )
    p_promote.add_argument(
        "--epic",
        help="the real epic this work belongs to, e.g. 'mesh-pipeline'; omit it to clear "
        "the 'classification' epic the raw capture was auto-grouped under",
    )
    p_promote.add_argument(
        "--priority",
        type=int,
        default=None,
        help="numeric urgency index, lower = more urgent (a raw capture has none; used to "
        "decide which workable ticket 'list next' offers first)",
    )
    p_promote.add_argument(
        "--description",
        default=None,
        help="the ticket's real description, replacing the raw capture's triage text; the "
        "request it was captured from is kept in the body as an 'Original request: ...' "
        "line, and any notes are preserved either way",
    )
    p_promote.add_argument(
        "--tags", default="", help="comma-separated, freeform, for codebase-area search, e.g. 'normals,curves'"
    )
    p_promote.add_argument(
        "--agent",
        metavar="AGENT_ID",
        help="agent id to claim the ticket for in the same command, e.g. claude.haiku.001: "
        "the ticket lands at 'in_progress' assigned to that agent instead of 'open'. "
        "Refused for a wish",
    )
    _sink_flag(p_promote)
    p_promote.set_defaults(func=cmd_promote)

    p_list = sub.add_parser(
        "list", help="list/filter tickets", description="List tickets, optionally filtered by one or more fields. "
        "Sorted so that within a status, more urgent tickets (lower priority number) come first."
    )
    p_list.add_argument(
        "--status",
        type=_status_list,
        default=[],
        metavar="STATUS",
        help="filter by status; a comma-separated list matches any of them, "
        "e.g. 'open,in_progress,review'. This selects tickets; to count tickets per "
        "status instead, see 'arbite status'",
    )
    p_list.add_argument(
        "--tier",
        choices=TIERS,
        help="filter by agent capability tier; with 'next', pass your own tier so you "
        f"are only offered work you can actually do. {schema.TIER_HELP}",
    )
    p_list.add_argument("--domain", help="filter by domain")
    p_list.add_argument("--epic", help="filter by epic (the larger initiative a ticket belongs to), e.g. 'mesh-pipeline'")
    p_list.add_argument("--priority", type=int, help="filter by exact priority number (lower = more urgent)")
    p_list.add_argument("--assignee", help="filter by assignee agent id")
    p_list.add_argument(
        "--tic",
        default="",
        metavar="TICKET_ID",
        help="filter by comma-separated ticket ids, each a wildcard (substring) "
        "match, e.g. 'f6' finds tic-f607; combine with --tree/--topo to root the "
        "dependency view at the matching tickets and pull in every transitive "
        "dependency beneath them",
    )
    view = p_list.add_mutually_exclusive_group()
    view.add_argument(
        "--tree",
        action="store_true",
        help="render the list as a dependency tree (children = depends_on) instead of a "
        "flat table; siblings sorted by priority",
    )
    view.add_argument(
        "--topo",
        action="store_true",
        help="render the list as a vertical list in topological dependency order; when "
        "several tickets are ready at once, the most urgent (lowest priority number) comes first",
    )
    p_list.add_argument(
        "subcommand",
        nargs="?",
        choices=["next", "raw"],
        metavar="SUBCOMMAND",
        help="'next' prints the next workable open ticket -- one whose depends_on "
        "tickets are all closed -- most urgent (lowest priority number) first; combine "
        "with --tier/--domain/--epic to restrict to a capability tier, a domain or an "
        "epic, --count to hand a batch of work to several agents at once, and --claim to "
        "take it in the same command. 'raw' prints the whole raw backlog (every status "
        "'raw' ticket) as a running todo list until a classification run drains it: "
        "grouped by type (memo/feature/request/bug/wish), one line per ticket showing its id "
        "and the request text it was captured from, oldest first -- since every raw "
        "ticket's title is '<type> (raw): Requires Classification' and its "
        "tier/domain/priority/epic are placeholders, the usual flat table would be noise",
    )
    p_list.add_argument(
        "--count",
        type=int,
        default=None,
        help="how many tickets to return. With 'next' the default is 1 (the single most "
        "urgent workable ticket) and --count N returns the N most urgent workable tickets, "
        "so a dispatcher can hand a batch out to several agents in one query; combined with "
        "--claim it claims up to N of them. For a plain list, --topo or --tree the default "
        "is no limit, and --count caps the rows shown (for --tree it caps the number of "
        "top-level roots, each of which still shows its full subtree)",
    )
    p_list.add_argument(
        "--claim",
        metavar="AGENT_ID",
        help="with 'next': claim the most urgent workable ticket for this agent id in the "
        "same command, e.g. --claim claude.haiku.001. Doing it in one step is race-free: "
        "running 'list next' and then 'claim' leaves a window in which another agent can "
        "take the ticket you were just handed. If another agent wins the race, the next "
        "workable ticket is claimed instead",
    )
    _json_flag(p_list)
    _sink_flag(p_list)
    p_list.set_defaults(func=cmd_list)

    p_search = sub.add_parser(
        "search",
        help="search tickets by field and/or body text",
        description="Search every ticket for SEARCH_TEXT, optionally restricted to specific "
        "fields with --params. Matching is a case-insensitive substring by default; -w adds "
        "simple wildcards ('*' matches any text, e.g. '*popup*') and -r treats SEARCH_TEXT "
        "as a regular expression. A ticket matches if any selected field matches.",
    )
    p_search.add_argument(
        "--params",
        default="all",
        metavar="FIELDS",
        help="comma-separated ticket fields to search, e.g. 'title,body'; 'body' means the "
        "rest of the ticket (markdown body), 'all' means every field plus the body "
        "(default: all)",
    )
    p_search.add_argument(
        "--status",
        type=_status_list,
        default=[],
        metavar="STATUS",
        help="only search tickets with these statuses (default: all statuses); a "
        "comma-separated list matches any of them, e.g. 'open,in_progress,review'. This "
        "selects tickets; to count tickets per status instead, see 'arbite status'",
    )
    search_mode = p_search.add_mutually_exclusive_group()
    search_mode.add_argument(
        "-r",
        "--regex",
        action="store_true",
        help="treat SEARCH_TEXT as a regular expression (case-insensitive)",
    )
    search_mode.add_argument(
        "-w",
        "--wildcard",
        action="store_true",
        help="simple wildcards: '*' matches any text, e.g. '*popup*' (case-insensitive)",
    )
    p_search.add_argument(
        "search_text",
        metavar="SEARCH_TEXT",
        nargs="+",
        help="text to search for (joined with spaces if multiple words)",
    )
    _json_flag(p_search)
    _sink_flag(p_search)
    p_search.set_defaults(func=cmd_search)

    p_claim = sub.add_parser(
        "claim",
        help="claim a ticket: assign it, set it in_progress and start its work attempt",
        description="Set a ticket's assignee, status and updated together: claiming sets "
        "status to 'in_progress' and the assignee in one write (the file sink files the "
        "ticket under in_progress/), so a caller never needs a separate 'set status' after "
        "claiming -- and it records the attempt that owns the work, printing its id because "
        "every later file command presents it. The claim is a compare-and-swap: a ticket "
        "already assigned to another agent is refused unless --force, and the write only "
        "lands if the ticket is still in the state it was read in, so two agents racing for "
        "the same ticket cannot both end up believing they own it. Readiness (every "
        "dependency closed), a real classification and the one-active-attempt rule are "
        "checked by the acquisition itself, not by the queue that suggested the ticket, so "
        "naming a ticket directly cannot bypass them. (Identity assignment and liveness "
        "remain the agent harness's job.)",
    )
    p_claim.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_claim.add_argument("--agent", required=True, help="agent id claiming the ticket, e.g. claude.haiku.001 (required)")
    p_claim.add_argument(
        "--force",
        action="store_true",
        help="take over a ticket another worker holds (needs --reason): ends their attempt "
        "as interrupted, records the revocation and starts a new attempt for you; without "
        "this, claiming someone else's ticket is an error",
    )
    p_claim.add_argument(
        "--reason",
        default=None,
        help="why the takeover is justified; required with --force, because an "
        "administrative override keeps its reason",
    )
    _json_flag(p_claim)
    _sink_flag(p_claim)
    p_claim.set_defaults(func=cmd_claim)

    p_release = sub.add_parser(
        "release",
        help="return a claimed ticket to the open status and clear its assignee",
        description="The counterpart to claim: reopen a ticket, clear its assignee and any "
        "block reason, append a timestamped note, and update status/updated. Use it when an "
        "agent stops work part-way -- out of scope, out of context, or the wrong capability "
        "tier -- so the ticket becomes visible to 'arbite list next' again instead of sitting "
        "claimed by nobody who is still working it.",
    )
    p_release.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_release.add_argument(
        "--agent",
        default="system",
        help="agent id attributed on the automatic release note (default: system)",
    )
    p_release.add_argument(
        "--reason", default="", help="why it's being released; included in the note (optional)"
    )
    _sink_flag(p_release)
    p_release.set_defaults(func=cmd_release)

    p_block = sub.add_parser(
        "block",
        help="mark a ticket blocked",
        description="Set status to blocked, set blocked_by, and update updated.",
    )
    p_block.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_block.add_argument(
        "--reason", required=True, help="why it's stalled: freeform text or another ticket id (required)"
    )
    _sink_flag(p_block)
    p_block.set_defaults(func=cmd_block)

    p_unblock = sub.add_parser(
        "unblock",
        help="clear a ticket's block and move it back into play",
        description="The counterpart to block: clear blocked_by, append a timestamped note "
        "recording what the block was, and set the ticket back to in_progress if it is still "
        "assigned, otherwise to open. Prefer this over 'arbite set status', which leaves "
        "blocked_by populated so the ticket keeps claiming to be stalled in every listing.",
    )
    p_unblock.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_unblock.add_argument(
        "--agent",
        default="system",
        help="agent id attributed on the automatic unblock note (default: system)",
    )
    p_unblock.add_argument(
        "--reason", default="", help="what unblocked it; included in the note (optional)"
    )
    p_unblock.add_argument(
        "--open",
        action="store_true",
        help="send it back to open and clear the assignee even if it is still assigned, "
        "instead of returning it to in_progress for its current owner",
    )
    _sink_flag(p_unblock)
    p_unblock.set_defaults(func=cmd_unblock)

    p_close = sub.add_parser(
        "close",
        help="close a ticket",
        description="Set status to closed, stamp the closed date, and update updated. A file "
        "sink additionally archives the ticket by close month; other sinks just record it.",
    )
    p_close.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    _sink_flag(p_close)
    p_close.set_defaults(func=cmd_close)

    p_reopen = sub.add_parser(
        "reopen",
        help="reopen a ticket (back to the open status); --reason is required",
        description="Set a ticket that is not currently open back to open: clear its "
        "closed date and block reason, append an automatic 'Reopened: <reason>.' note, and "
        "update status/updated. --reason is required, and deliberately so: reopening is the "
        "rejection path out of review, and a rejection with no stated reason is useless to "
        "whoever has to act on it, so a bare 'arbite reopen <id>' is an error. A ticket that "
        "is already open is refused.",
    )
    p_reopen.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_reopen.add_argument(
        "--reason",
        required=True,
        help="why it is being reopened -- the reason it was rejected, recorded verbatim "
        "in the automatic note as 'Reopened: <reason>.', e.g. 'tests fail on ARM' (required)",
    )
    p_reopen.add_argument(
        "--agent",
        default="system",
        help="agent id (or 'system') attributed on the automatic reopen note, e.g. "
        "claude.haiku.001 (default: system)",
    )
    _sink_flag(p_reopen)
    p_reopen.set_defaults(func=cmd_reopen)

    p_submit = sub.add_parser(
        "submit",
        help="submit a finished ticket for review (or close it when review is off)",
        description="Hand a finished ticket off. Where it lands is the project's "
        "committed answer, the 'review:' key in .arbite/project.yaml: with review enabled "
        "(the default) the ticket becomes 'review' and moves to review/, keeping its "
        "assignee -- a ticket in review is still owned by whoever did the work, since they "
        "are who a reviewer sends it back to; with 'review: false' it is closed instead, "
        "dated and filed exactly as 'arbite close' does. Either way an automatic note is "
        "appended ('Submitted for review.' or 'Submitted; closed (review disabled).'), with "
        "--message added as detail. A ticket that is already closed is refused. The "
        "rejection path out of review is 'arbite reopen --reason ...', and the accepting "
        "path is 'arbite accept'.",
    )
    p_submit.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_submit.add_argument(
        "--message",
        help='optional detail recorded in the automatic note, e.g. "needs a second pair of eyes"',
    )
    p_submit.add_argument(
        "--agent", default="system", help="agent id the note is attributed to (default: system)"
    )
    _sink_flag(p_submit)
    p_submit.set_defaults(func=cmd_submit)

    p_accept = sub.add_parser(
        "accept",
        help="accept a ticket that is in review (closes it, attributed to the reviewer)",
        description="Close a ticket that is in review: the reviewer's counterpart to "
        "'arbite submit'. Only a ticket whose status is 'review' can be accepted -- "
        "anything else is refused with its real status named, because the general-purpose "
        "close is 'arbite close'. An automatic 'Accepted.' note is appended, attributed to "
        "the accepting agent rather than to the ticket's assignee (the point of the record "
        "is who approved the work), with --message added as detail; 'closed' is dated "
        "exactly as 'arbite close' dates it. To send the work back instead, use 'arbite "
        "reopen --reason ...', which records why it was rejected.",
    )
    p_accept.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_accept.add_argument("--message", help="optional detail recorded in the automatic note")
    p_accept.add_argument(
        "--agent",
        default="system",
        help="agent id credited in the note (the accepting agent, not the assignee); default: system",
    )
    _sink_flag(p_accept)
    p_accept.set_defaults(func=cmd_accept)

    p_shelve = sub.add_parser(
        "shelve",
        help="shelve a ticket (park it for later)",
        description="Set status to shelved, update updated, and append an automatic "
        "timestamped note recording that it was shelved (including --reason if given).",
    )
    p_shelve.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_shelve.add_argument(
        "--reason",
        default="",
        help="why it's being shelved; included in the automatic note (optional)",
    )
    _sink_flag(p_shelve)
    p_shelve.set_defaults(func=cmd_shelve)

    p_unshelve = sub.add_parser(
        "unshelve",
        help="move a shelved ticket back to open (unshelve it)",
        description="Set a shelved ticket back to open, clear its assignee and any block "
        "reason, append an automatic timestamped note recording that it was unshelved "
        "(including --reason if given), and update status/updated. The counterpart to "
        "shelve: once unshelved, the ticket is available via 'arbite list next' again.",
    )
    p_unshelve.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_unshelve.add_argument(
        "--reason",
        default="",
        help="why it's being unshelved; included in the automatic note (optional)",
    )
    _sink_flag(p_unshelve)
    p_unshelve.set_defaults(func=cmd_unshelve)

    p_note = sub.add_parser(
        "note",
        help="append a timestamped, agent-identified note to a ticket",
        description="Append a timestamped, agent-identified entry to a ticket's '## Notes' "
        "section (blank line between entries) and update 'updated'. Agents should prefer "
        "this over directly editing a ticket, so attribution and timestamps stay "
        "consistent.",
    )
    p_note.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_note.add_argument("agent", metavar="AGENT_ID", help="agent id leaving the note, e.g. claude.haiku.001")
    p_note.add_argument("message", metavar="MESSAGE", nargs="+", help="note text (joined with spaces if multiple words)")
    _sink_flag(p_note)
    p_note.set_defaults(func=cmd_note)

    p_show = sub.add_parser(
        "show", help="print a ticket's full contents", description="Print a ticket in its canonical form: YAML frontmatter plus markdown body, exactly as a file sink would store it."
    )
    p_show.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP_READONLY)
    _json_flag(p_show)
    _sink_flag(p_show)
    p_show.set_defaults(func=cmd_show)

    p_deps = sub.add_parser(
        "deps",
        help="walk depends_on to show a dependency tree",
        description="Recursively walk a ticket's depends_on field and print the dependency tree.",
    )
    p_deps.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP_READONLY)
    _json_flag(p_deps)
    _sink_flag(p_deps)
    p_deps.set_defaults(func=cmd_deps)

    p_depend = sub.add_parser(
        "depend",
        help="add a dependency to a ticket's depends_on, or clear all of its dependencies",
        description="Declare that one ticket depends on another. With two arguments, "
        "<tic_a> is made to depend on <tic_b>: <tic_b> is added to <tic_a>'s "
        "depends_on (deduplicated, existing dependencies are kept). With a single "
        "argument, all of <tic_a>'s dependencies are cleared.",
    )
    p_depend.add_argument("id", metavar="TIC_A", help=TICKET_ID_HELP)
    p_depend.add_argument(
        "dep",
        metavar="TIC_B",
        nargs="?",
        default=None,
        help="ticket that <TIC_A> depends on; omit to clear all of <TIC_A>'s dependencies",
    )
    _sink_flag(p_depend)
    p_depend.set_defaults(func=cmd_depend)

    p_move = sub.add_parser(
        "move",
        help="file a ticket in a bucket (e.g. /plans), or '/' to un-file it",
        description="File a ticket somewhere other than its status location, without changing "
        "any field. <folder> is root-relative: '/plans' files it in the plans bucket, "
        "'/plans/ideas' in a nested one, and '/' returns it to wherever its status says it "
        "belongs. What a bucket physically is depends on the sink: a folder under the arbite "
        "root for the file sink (created if missing), a recorded bucket for the SQLite sink. "
        "This is deliberately not a state change, so a status command (claim/close/block/...) "
        "is what you want for anything that should change state -- and those un-file the "
        "ticket for you.",
    )
    p_move.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_move.add_argument(
        "folder",
        metavar="FOLDER",
        help="root-relative destination, e.g. '/plans', or '/' to return the ticket to "
        "its status location",
    )
    _sink_flag(p_move)
    p_move.set_defaults(func=cmd_move)

    p_ref = sub.add_parser(
        "ref",
        help="manage a ticket's plan references (add / rm / list)",
        description="Manage a ticket's `references`: the plan documents it draws on, stored "
        "as a frontmatter list. Entries are root-relative into the arbite directory's plans/ "
        "bucket ('plans/foo.md' == .arbite/plans/foo.md) -- the same root-relative form "
        "`move` takes for a bucket, including a leading '/' that is stripped. A referenced "
        "document need not exist yet: a plan is often written after the ticket that needs "
        "it, so a missing one warns on stderr and the write still succeeds -- `arbite "
        "doctor` reports it as a problem (kind 'dangling_reference').",
    )
    ref_sub = p_ref.add_subparsers(dest="ref_action", required=True, metavar="SUBCOMMAND")
    _sink_flag(p_ref)

    p_ref_add = ref_sub.add_parser(
        "add",
        help="append references to a ticket (de-duplicated, order preserved)",
        description="Append one or more plan references to a ticket. Each PATH is "
        "root-relative into the arbite directory's plans/ bucket, e.g. "
        "'plans/review-workflow.md'; a leading '/' is accepted and stripped, so "
        "'/plans/review-workflow.md' and 'plans/review-workflow.md' are the same "
        "reference. Entries are appended in the order given, keeping the ones the ticket "
        "already has in place; adding a path it already references is a no-op success, not "
        "an error. The referenced document need not exist yet: a missing one warns on "
        "stderr and the command still succeeds (exit 0).",
    )
    p_ref_add.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_ref_add.add_argument(
        "paths",
        metavar="PATH",
        nargs="+",
        help="root-relative plan document(s) under the arbite directory's plans/ bucket, "
        "e.g. 'plans/review-workflow.md' (one or more)",
    )
    _sink_flag(p_ref_add)
    p_ref_add.set_defaults(func=cmd_ref_add)

    p_ref_rm = ref_sub.add_parser(
        "rm",
        help="remove references from a ticket (errors on one it does not reference)",
        description="Remove one or more plan references from a ticket, keeping the order of "
        "the ones that remain. Each PATH must be one the ticket currently references, and "
        "is normalised exactly as `ref add` normalised it (so a leading '/' is accepted); a "
        "path it does not reference is an error (exit 1). The whole invocation is atomic: "
        "if any path is not referenced, nothing is written.",
    )
    p_ref_rm.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_ref_rm.add_argument(
        "paths",
        metavar="PATH",
        nargs="+",
        help="root-relative plan document(s) to remove, e.g. 'plans/review-workflow.md' "
        "(one or more; each must already be referenced by the ticket)",
    )
    _sink_flag(p_ref_rm)
    p_ref_rm.set_defaults(func=cmd_ref_rm)

    p_ref_list = ref_sub.add_parser(
        "list",
        help="print a ticket's references, one per line (--json for machine output)",
        description="Print a ticket's `references`, one per line -- or, with --json, an "
        "object {'id': <ticket id>, 'references': [<stored path>, ...]} whose field names "
        "match the frontmatter. A ticket with no references prints nothing, and reports an "
        "empty 'references' list in --json, matching how `show` omits the field entirely.",
    )
    p_ref_list.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP_READONLY)
    _json_flag(p_ref_list)
    _sink_flag(p_ref_list)
    p_ref_list.set_defaults(func=cmd_ref_list)

    p_set = sub.add_parser(
        "set",
        help="set one or more ticket properties",
        description="Set one or more ticket properties on an existing ticket. Properties "
        "are given as PROPERTY VALUE pairs and any number can be set in one call; quote "
        "any value that spans more than one word. Type-aware: 'tags', 'depends_on' and "
        "'references' are comma-separated lists, 'priority' must be an integer, and an "
        "empty quoted value ('') clears a field. If 'status' is set, the ticket is "
        "re-filed to match (moving to 'closed' auto-dates 'closed'); 'arbite set-status "
        "<id> <status>' is the dedicated front door for the same change, through the same "
        "code path, and is the escape hatch for the statuses no work-flow command reaches. "
        "'id' is structural "
        "and cannot be set. This changes one ticket; to count tickets per status across the "
        "whole backlog, use 'arbite status'.",
    )
    p_set.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_set.add_argument(
        "assignments",
        metavar="PROPERTY VALUE",
        nargs="+",
        help="one or more PROPERTY VALUE pairs to set, e.g. title 'New Title' tier high; "
        "quote any value that spans more than one word, and use an empty quoted value "
        "('') to clear a field",
    )
    _sink_flag(p_set)
    p_set.set_defaults(func=cmd_set)

    p_set_status = sub.add_parser(
        "set-status",
        help="change a ticket's status (moves it to the matching status folder)",
        description="Change one ticket's status. Additive, not a replacement: "
        "'arbite set <id> status <value>' keeps working, and both write through the same "
        "code path so the two cannot drift. In the file sink a status change moves the "
        "ticket into the folder matching the new status, which un-files a ticket sitting in "
        "a bucket so status and location cannot disagree; moving to 'closed' auto-dates "
        "'closed', and asking for the status a ticket already has is a no-op. The "
        "status vocabulary comes from schema.STATUSES, so 'review' -- and any status added "
        "later -- is accepted here without this command changing. This is the escape hatch "
        "rather than a workflow front door: it will make any status change into any other, "
        "including the transitions 'claim'/'close'/'submit'/'accept' exist for, because its "
        "purpose is reaching the rest of the vocabulary. It changes one ticket; to count "
        "tickets per status across the whole backlog, use 'arbite status'.",
    )
    p_set_status.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_set_status.add_argument(
        "status",
        choices=schema.STATUSES,
        metavar="STATUS",
        help=f"the new status: {' | '.join(schema.STATUSES)}",
    )
    _sink_flag(p_set_status)
    p_set_status.set_defaults(func=cmd_set_status)

    p_delete = sub.add_parser(
        "delete",
        help="delete a ticket outright (requires --force)",
        description="Remove a ticket from the store entirely. Irreversible, so it requires "
        "--force, and it records a 'Deleted by <agent>' note immediately before removing the "
        "ticket so the last state it ever had is attributable. The receipt (id, title, "
        "location, note) is printed and included in --json. Use 'arbite close' to archive a "
        "ticket instead of destroying it.",
    )
    p_delete.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_delete.add_argument(
        "--force",
        action="store_true",
        help="confirm the deletion; without it the command refuses (required)",
    )
    p_delete.add_argument(
        "--agent",
        default="system",
        help="agent id attributed on the deletion note (default: system)",
    )
    p_delete.add_argument(
        "--reason", default="", help="why it's being deleted; included in the note (optional)"
    )
    p_delete.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be deleted and make no change",
    )
    _json_flag(p_delete)
    _sink_flag(p_delete)
    p_delete.set_defaults(func=cmd_delete)

    p_migrate = sub.add_parser(
        "migrate",
        help="copy every ticket from one sink into another",
        description="Read every ticket from the source sink (status-managed tickets and "
        "bucketed ones alike) and write it to the destination sink, preserving ids, "
        "timestamps, body, tags, dependencies, notes and buckets verbatim. The source is "
        "never modified -- this is a copy -- and a successful run makes the destination the "
        "project default, so later commands read the store the tickets now live in. Tickets "
        "already present in the destination are skipped unless --overwrite is given; --prune "
        "additionally retires the source store once the copy is verified.",
    )
    p_migrate.add_argument(
        "--to",
        dest="to_sink",
        required=True,
        choices=SINK_KINDS,
        metavar="KIND",
        help=f"destination sink kind: {', '.join(SINK_KINDS)} (required)",
    )
    p_migrate.add_argument(
        "--from",
        dest="from_sink",
        default=None,
        choices=SINK_KINDS,
        metavar="KIND",
        help="source sink kind (default: the sink this command would otherwise use "
        "-- --sink/ARBITE_SINK/config)",
    )
    p_migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="report how many tickets would be migrated and change nothing",
    )
    p_migrate.add_argument(
        "--overwrite",
        action="store_true",
        help="replace destination tickets that already use one of the source ids, instead "
        "of skipping them",
    )
    p_migrate.add_argument(
        "--prune",
        action="store_true",
        help="after copying, delete the source tickets -- the destructive half of a "
        "migration, for retiring a store once its contents are verified in the other one. "
        "Refused if any ticket was skipped, because a stale destination copy would then be "
        "the only copy left; combine with --dry-run to see what it would remove",
    )
    _sink_flag(p_migrate)
    p_migrate.set_defaults(func=cmd_migrate)

    p_doctor = sub.add_parser(
        "doctor",
        help="check ticket integrity (drift, cycles, dangling deps/references, stale indexes, "
        "unusable claims, unfinished file operations)",
        description="Check the invariants nothing else enforces and report what it finds, "
        "then exit 3 if any problem remains. The shared checks cover duplicate ids, invalid "
        "field values, dependency cycles, dangling and self dependencies, dangling "
        "references (a referenced plan with no document on disk -- an ordinary drafting "
        "state, reported but never repaired: creating the plan and dropping the reference "
        "are equally plausible), in_progress "
        "tickets with no assignee, blocked tickets with no reason, and closed-date "
        "mismatches. The sink adds its own: for the file sink, frontmatter/folder drift (the "
        "folder is authoritative), stray temp files from an interrupted write, and closed "
        "tickets archived under the wrong month; for the SQLite sink, a note index that has "
        "drifted from the ticket body, orphaned index rows, an unexpected schema version and "
        "structural database corruption. The coordination store adds a third family, judged "
        "against the bytes on the disk: a claim whose attempt is over or does not exist, an "
        "unfinished file operation (one whose bytes are exactly one of its two recorded "
        "versions is repairable; bytes that match neither are drift, printed with all three "
        "versions and left alone), and coordination records naming something this store does "
        "not have. Scratch payloads are reported as a note and never change the exit code. "
        "--fix repairs only what is unambiguous.",
    )
    p_doctor.add_argument(
        "--fix",
        action="store_true",
        help="repair what can be corrected unambiguously: for the file sink, frontmatter "
        "status is rewritten to match the folder a ticket sits in, tickets loose in the root "
        "are re-filed, and closed tickets are moved into the archive month matching their "
        "close date; for the SQLite sink, a stale note index is rebuilt from the body and "
        "orphaned index rows are removed; in the coordination store, a claim nobody can use "
        "is released (the bytes and the receipts are untouched) and an unfinished operation "
        "whose bytes match one of its two recorded versions is finalised. Anything needing a "
        "judgement call (duplicate ids, dependency cycles, missing data, bytes matching "
        "neither recorded version) is only reported",
    )
    _json_flag(p_doctor)
    _sink_flag(p_doctor)
    p_doctor.set_defaults(func=cmd_doctor)

    return parser, dict(sub.choices)


def main():
    # Force UTF-8 on stdout/stderr so tickets containing characters outside the
    # console's default code page (e.g. cp1252 on Windows -- em dashes, arrows,
    # smart quotes, CJK, etc.) print correctly instead of raising
    # UnicodeEncodeError. Mirrors the PYTHONIOENCODING=utf-8 workaround without
    # needing the environment variable to be set.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            # Not a reconfigure-able stream (e.g. redirected to a non-text
            # handle); leave it untouched.
            pass

    parser, _ = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except ArbiteError as e:
        # One failure path for every expected refusal, driven by the outcome
        # vocabulary: the label (`error`, `busy`, `stale_read`), the `next:` hints
        # and the exit code all come from the same outcome, so a caller branching on
        # 4 or 5 reads the same word on stderr. A plain error prints exactly what it
        # always has (`error: <message>`, exit 1) and gains no hint it never had.
        outcome = outcomes.outcome_of(e)
        print(f"{outcome.label}: {e}", file=sys.stderr)
        hint = outcomes.text_hint_of(e)
        if hint:
            print(hint, file=sys.stderr)
        sys.exit(outcome.exit_code)


if __name__ == "__main__":
    main()
