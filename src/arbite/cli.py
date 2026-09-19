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
import contextlib
import copy
import json
import os
import sys
from pathlib import Path

from . import (
    __version__,
    application,
    changes,
    config,
    coordination,
    coordination_doctor,
    coordination_export,
    docs,
    eligibility,
    fileclaims,
    filemutations,
    filereads,
    graph,
    lifecycle,
    schema,
    workers,
    workspace,
)
from .application import Actor
from .errors import (
    ArbiteError,
    ClaimConflict,
    Conflict,
    CoordinationConflict,
    CoordinationError,
    CoordinationNotFound,
    TicketError,
    UnsupportedCoordination,
)
from .query import TicketQuery, TextMatch, apply_limit, resolve_terms
from .schema import CLASSIFICATION_EPIC, STATUSES, TIERS, Ticket
from .sinks import SINK_KINDS, Expect, SinkInfo, build_sink

# Exit codes. Agents drive arbite from shell loops, so "nothing matched" has to
# be distinguishable from "worked fine" and from "broke" without parsing
# stdout: 0 = success with results, 1 = error, 2 = query ran but matched
# nothing, 3 = `doctor` found integrity problems.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_EMPTY = 2
EXIT_PROBLEMS = 3


def _split_csv(value):
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _status_list(value):
    """argparse type for --status: a single status or a comma-separated list of
    them, e.g. 'open' or 'open,in_progress'. Validated here so an unknown status
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
        "(default: the 'sink:' key in arbite.yaml, else file)",
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
            f"{config.ENV_SINK}=sqlite, or add 'sink: sqlite' to arbite.yaml",
            file=sys.stderr,
        )


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
    closed it, added a dependency or a note -- the write is refused with a
    Conflict instead of silently discarding that change. The status/assignee
    pair names the state being replaced; the store revision catches an edit to
    any other field. Only `move_to_bucket` does not use it, because filing is
    not a state change."""
    return Expect(status=ticket.status, assignee=ticket.assignee, revision=ticket.revision)


def _ticket_write_lock(sink):
    """The coordination operation lock a plain ticket edit holds, when there is one.

    Acquisition validates readiness (the ticket's dependencies, their statuses)
    under this lock, so an edit that can change readiness -- `depend`, `set`,
    `note` -- takes it too: the two serialize instead of racing. A sink without
    the coordination contract has no acquisition to race, and no lock."""
    store = sink.coordination()
    if store is None:
        return contextlib.nullcontext()
    return store.operation_lock()


def _lifecycle(args, sink, agent=None) -> "lifecycle.TicketLifecycle":
    """The ticket lifecycle for this command, built lazily.

    Ticket acquisition and every transition now run through `arbite.lifecycle`,
    which needs the workspace's coordination service. That service is constructed
    with `application.coordination_service_for` -- the documented C03 stopgap that
    derives a stable workspace id from the canonical project root, because the CLI
    has no workspace registry yet (C04's job). Constructing it is cheap and
    idempotent; nothing here is policy, only plumbing."""
    acting = agent or getattr(args, "agent", None) or getattr(args, "claim", None) or "system"
    service = application.coordination_service_for(
        sink, root=str(config.find_project_root()), actor=Actor(str(acting))
    )
    return lifecycle.TicketLifecycle(service, sink)


def _revocation_requested(args, attempt, agent: str) -> bool:
    """True when this call is an explicit administrative revocation of `attempt`.

    `--force` by someone other than the attempt's worker is a revocation: the old
    attempt is interrupted (not merely released) and the non-empty `--reason` is
    recorded. A `--force` by the worker's own agent is not a revocation -- there is
    nothing to revoke -- and `require_ownership` has already allowed it.
    """
    return bool(getattr(args, "force", False)) and attempt is not None and attempt.worker_id != agent


def cmd_init(args):
    """Create the arbite directory and the selected sink's store, then write the
    agent-facing command reference.

    Which sink it creates is decided exactly like every other command decides --
    `--sink`, then `ARBITE_SINK`, then `sink:` in arbite.yaml, then file -- so
    setting up a SQLite project is one flag, not a different command. The store it
    creates then becomes the project default: `sink:` is written to arbite.yaml
    (created if missing), so later commands -- including ones an agent runs with no
    flags -- read the same store. An `ARBITE_SINK` selection is treated as
    this-process-only and reported rather than written into committed config."""
    spec, project_root = _cwd_sink(args)
    arbite_dir = project_root / config.ARBITE_DIRNAME
    arbite_dir.mkdir(parents=True, exist_ok=True)

    sink = build_sink(spec, arbite_dir)
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
            "no agents configured (add an 'agents:' list to arbite.yaml to "
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
        f"{config.ENV_SINK}, or a 'sink:' key in arbite.yaml)"
    )
    if info.details:
        for key, value in sorted(info.details.items()):
            print(f"{key}: {value}")
    counts = ", ".join(f"{status} {n}" for status, n in sorted(info.status_counts.items()))
    print(f"tickets: {info.ticket_count}{f' ({counts})' if counts else ''}")


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
    telling the calling agent to classify it and either open it for someone else or
    claim it now. This is a triage queue, so it's oldest-first (by 'created')
    rather than priority-ordered like 'list next' -- a raw ticket has no priority
    yet."""
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
    the filters, that's an answer: nothing at that tier/domain/epic is ready
    yet, and the exit code is 2.

    With --claim, the single most urgent candidate is claimed in the same
    command. That closes the race in the obvious two-step version (`list next`
    then `claim`): between those two commands another agent can claim the
    ticket you were just handed, and both agents then work it."""
    by_id = {t.id: t for t in every}
    candidates = sink.query(
        TicketQuery(
            status=("open",),
            tier=args.tier,
            domain=args.domain,
            epic=args.epic,
            order="next",
        )
    )
    candidates = [t for t in candidates if graph.is_workable(t, by_id)]

    if not candidates:
        # "Nothing is ready" and "everything is deadlocked" look identical from
        # the outside, so say which one it is rather than leaving an agent to
        # poll a queue that can never produce work.
        _warn_cycles(by_id)

    # `next` answers "what should I work on", so it returns one ticket unless
    # the caller asks for a batch.
    wanted = 1 if args.count is None else args.count

    if not args.claim:
        _emit_tickets(candidates[:wanted], args.json, sink)
        return

    if not candidates:
        # Nothing to claim is not an error; the exit code carries it.
        _emit_tickets([], args.json, sink)
        return

    # Walk candidates in order, claiming until we have `wanted` of them. Each
    # acquisition goes through the lifecycle layer, so readiness and the "one
    # active attempt per ticket" rule are enforced on the operation -- the
    # `graph.is_workable` filter above only *selects* candidates and cannot be
    # the authority. If another agent wins the race for one, move on to the next
    # rather than failing the whole dispatch: each claim is individually atomic,
    # so a partial batch is a correct result, not a broken one.
    ctl = _lifecycle(args, sink, agent=args.claim)
    # A registered worker is selected through its profile: --tier may narrow the
    # search but never exceed the configured tier (refused outright), and tickets
    # the profile cannot take are skipped. acquire() re-checks both.
    declaration = ctl.worker_declaration(args.claim, declared_tier=args.tier)
    if not declaration.enabled:
        eligibility.evaluate(eligibility.Requirements(restricted=False), declaration).require()
    requirements_for = eligibility.requirements_for_ticket
    eligible = [
        t for t in candidates
        if eligibility.evaluate(requirements_for(t), declaration).eligible
    ]
    if len(eligible) < len(candidates):
        print(
            f"note: skipped {len(candidates) - len(eligible)} workable ticket(s) above "
            f"{args.claim}'s configured tier ({declaration.tier})",
            file=sys.stderr,
        )
    candidates = eligible
    if not candidates:
        _emit_tickets([], args.json, sink)
        return
    claimed = []
    errors = []
    for candidate in candidates:
        if len(claimed) == wanted:
            break
        try:
            result = ctl.acquire(candidate, worker_id=args.claim, declared_tier=args.tier)
        except ArbiteError as e:
            # Lost the race for this one (or it stopped being ready). The next
            # candidate is untouched, so the loop simply moves on to it.
            errors.append(str(e))
            continue
        claimed.append(result.ticket)

    if not claimed:
        raise TicketError(
            "every workable ticket was claimed by another agent first: " + "; ".join(errors)
        )

    if args.json:
        locations = sink.location_map(claimed)
        _print_json([t.to_dict(locations.get(t.id)) for t in claimed])
    else:
        _print_flat(claimed)
        # The table is the result; the claim receipts are commentary on stderr.
        # Flush first so the two streams stay in order when stdout is a pipe.
        sys.stdout.flush()
        for t in claimed:
            print(f"claimed {t.id} for {args.claim} -> {sink.location(t.id)}", file=sys.stderr)
    if len(claimed) < wanted:
        # Say so explicitly, and say why: a dispatcher that asked for 3 and got
        # 2 needs to know whether the queue ran dry or it lost races, because
        # those call for different responses (wait vs. retry immediately).
        if errors:
            reason = f"{len(errors)} were claimed by another agent first"
        else:
            reason = "no more workable tickets match"
        sys.stdout.flush()
        print(
            f"note: asked for {wanted} ticket(s), claimed {len(claimed)} -- {reason}",
            file=sys.stderr,
        )


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
    """Claim a ticket for an agent, recording a work attempt.

    Acquisition runs entirely through `arbite.lifecycle.TicketLifecycle.acquire`,
    so readiness (open status, real classification, closed dependencies) and the
    "one active attempt per ticket" rule are enforced on the operation, not just
    in `list next`. Three explicit forms:

    - a normal claim of an open, ready ticket (`--agent`);
    - `--adopt`: take over a legacy `in_progress` ticket that has no attempt
      record, starting its attempt now (no history is invented);
    - `--force --reason <why>`: an administrative takeover that interrupts the
      live attempt and starts a new one at a higher generation.

    The ticket compare-and-swap is what makes two racing claims produce exactly
    one winner; a losing claim raises before any attempt is recorded."""
    sink = _require_sink(args)
    ctl = _lifecycle(args, sink, agent=args.agent)
    with ctl.locked():
        t = sink.get(args.id, unique=True)
        previous = t.assignee
        result = ctl.acquire(
            t,
            worker_id=args.agent,
            adopt=bool(getattr(args, "adopt", False)),
            takeover=bool(getattr(args, "force", False)),
            reason=getattr(args, "reason", None),
        )
        if result.took_over and previous and previous != args.agent:
            taken = sink.get(result.ticket.id, unique=True)
            expect = _expect_from(taken)
            schema.append_note(taken, args.agent, f"Claim taken over from {previous} (--force).")
            taken.updated = schema.now()
            sink.update(taken, expect=expect)
            result = lifecycle.AcquisitionResult(taken, result.attempt, True, True)
    print(f"claimed {result.ticket.id} for {args.agent} -> {sink.location(result.ticket.id)}")


def cmd_release(args):
    """Return a claimed ticket to open/ and clear its assignee.

    The counterpart to claim: an agent that stops work part-way (out of scope, out of
    context, wrong capability tier) needs one command that unassigns and
    reopens together, so the ticket becomes visible to `list next` again rather
    than sitting in_progress owned by nobody who is still working it.

    If the ticket has an active attempt, only its worker -- or an explicit
    `--force --reason` administrative revocation -- may release it, and the
    attempt is ended (with an `attempt_released` event) *before* the ticket moves.
    A ticket with no attempt (a pre-C03 legacy ticket) keeps the old behaviour:
    there is no attempt to bypass."""
    sink = _require_sink(args)
    ctl = _lifecycle(args, sink, agent=args.agent)
    with ctl.locked():
        t = sink.get(args.id, unique=True)
        attempt = ctl.active_attempt(t.id)
        if attempt is not None:
            ctl.require_ownership(
                attempt,
                args.agent,
                force=getattr(args, "force", False),
                reason=args.reason,
                action="release",
            )
        if t.status == "open" and t.assignee is None:
            raise TicketError(f"ticket {t.id} is already open and unassigned")
        before = copy.deepcopy(t)
        previous = t.assignee
        message = "Released." if not args.reason else f"Released: {args.reason}"
        schema.append_note(t, args.agent, message)
        t.assignee = None
        t.blocked_by = None
        t.status = "open"
        t.updated = schema.now()
        if attempt is not None:
            state = "interrupted" if _revocation_requested(args, attempt, args.agent) else "released"
            ctl.commit_transition(
                "release", t, previous=before, end=attempt, end_state=state,
                reason=args.reason or None,
            )
        else:
            ctl.commit_transition(
                "release", t, previous=before, events=[ctl.transition_event("released", t.id)]
            )
    owner = f" (was {previous})" if previous else ""
    print(f"released {t.id}{owner} -> {sink.location(t.id)}")


def cmd_block(args):
    """Mark a ticket blocked, ending any active attempt.

    A blocked ticket is not being worked, so the worker's attempt is interrupted
    (recorded with `attempt_interrupted`) rather than left live: leaving it active
    would make the ticket unclaimable while it claims to be stalled. Only the
    attempt's worker may block it, unless an explicit `--force --reason` revokes
    the attempt."""
    sink = _require_sink(args)
    agent = getattr(args, "agent", "system")
    ctl = _lifecycle(args, sink, agent=agent)
    with ctl.locked():
        t = sink.get(args.id, unique=True)
        attempt = ctl.active_attempt(t.id)
        if attempt is not None:
            ctl.require_ownership(
                attempt,
                agent,
                force=getattr(args, "force", False),
                reason=args.reason,
                action="block",
            )
        before = copy.deepcopy(t)
        t.blocked_by = args.reason
        t.status = "blocked"
        t.updated = schema.now()
        event = ctl.ticket_event(
            "ticket_blocked",
            t.id,
            attempt_id=attempt.id if attempt is not None else None,
            payload={"ticket_id": t.id, "blocked_by": args.reason, "agent": agent},
        )
        ctl.commit_transition(
            "block",
            t,
            previous=before,
            end=attempt,
            end_state="interrupted" if attempt is not None else None,
            reason=args.reason,
            events=[event],
        )
    print(f"blocked {t.id} ({args.reason}) -> {sink.location(t.id)}")


def cmd_unblock(args):
    """Clear a block and move the ticket back into play.

    The symmetric counterpart to `block`. Doing this with `set status` leaves
    blocked_by populated, so the ticket claims to be stalled by something in
    every listing while sitting in open -- exactly the frontmatter drift the
    folder-is-truth rule exists to prevent.

    When the ticket still has an active attempt, returning it to `in_progress`
    simply records activity on that attempt (`touch`); sending it to `open` ends
    the attempt, because nobody then owns the work."""
    sink = _require_sink(args)
    agent = getattr(args, "agent", "system")
    ctl = _lifecycle(args, sink, agent=agent)
    with ctl.locked():
        t = sink.get(args.id, unique=True)
        if t.status != "blocked":
            raise TicketError(f"ticket {t.id} is not blocked (status: {t.status})")
        attempt = ctl.active_attempt(t.id)
        if attempt is not None:
            ctl.require_ownership(
                attempt,
                agent,
                force=getattr(args, "force", False),
                reason=args.reason,
                action="unblock",
            )
        reason = t.blocked_by
        was = f" (was blocked by: {reason})" if reason else ""
        message = f"Unblocked: {args.reason}" if args.reason else "Unblocked."
        if reason:
            message = f"{message.rstrip('.')} (was blocked by: {reason})."
        before = copy.deepcopy(t)
        schema.append_note(t, agent, message)
        t.blocked_by = None
        t.updated = schema.now()
        # Back to whoever was working it if it is still assigned, otherwise open.
        dest = "in_progress" if (t.assignee and not args.open) else "open"
        t.status = dest
        if dest == "open":
            t.assignee = None
        cascade = {}
        if attempt is not None:
            revoked = _revocation_requested(args, attempt, agent)
            if dest == "in_progress" and not revoked:
                cascade = {"touch": attempt}
            else:
                cascade = {
                    "end": attempt,
                    "end_state": "interrupted" if revoked else "released",
                    "reason": args.reason or None,
                }
        elif dest == "in_progress":
            # `block` ended the previous attempt, so resuming mints a *fresh*
            # generation: the old attempt and its file tokens stay dead, and a new
            # read must be taken under the new attempt before any write.
            handoff = f"resumed after block: {args.reason}" if args.reason else "resumed after block"
            cascade = {
                "start": ctl.new_attempt(t.id, worker_id=t.assignee, handoff=handoff),
                "origin": lifecycle.ORIGIN_RESUMED,
                "reason": args.reason,
            }
        event = ctl.ticket_event(
            "ticket_unblocked", t.id, payload={"ticket_id": t.id, "status": dest, "agent": agent}
        )
        ctl.commit_transition("unblock", t, previous=before, events=[event], **cascade)
    print(f"unblocked {t.id}{was} -> {sink.location(t.id)}")


def cmd_close(args):
    """Close a ticket, finishing (or revoking) any active attempt.

    Closing is a terminal transition, so an active attempt is *finished*
    (`attempt_finished`) before the ticket becomes `closed` -- unless the caller
    is another agent using `--force --reason`, which interrupts it instead. The
    old attempt is never reactivated: resuming the ticket mints a new one."""
    sink = _require_sink(args)
    agent = getattr(args, "agent", "system")
    ctl = _lifecycle(args, sink, agent=agent)
    with ctl.locked():
        t = sink.get(args.id, unique=True)
        attempt = ctl.active_attempt(t.id)
        if attempt is not None:
            ctl.require_ownership(
                attempt,
                agent,
                force=getattr(args, "force", False),
                reason=getattr(args, "reason", ""),
                action="close",
            )
        else:
            # No attempt to end, but a prior interrupted operation must still be
            # reconciled so close cannot report itself clean over ambiguous bytes.
            # (With an attempt, commit_transition reconciles before ending it.)
            ctl.reconcile_operations()
        before = copy.deepcopy(t)
        t.closed = schema.now()
        t.updated = t.closed
        t.status = "closed"
        revoked = attempt is not None and _revocation_requested(args, attempt, agent)
        # One journaled transition: the ticket write, the attempt ending, the
        # release of its file claims (plus a defensive sweep of any orphan claim a
        # pre-C09 store holds for the ticket) and the ticket_closed event either
        # all happen or none do.
        ctl.commit_transition(
            "close",
            t,
            previous=before,
            end=attempt,
            end_state=("interrupted" if revoked else "finished") if attempt is not None else None,
            reason=(getattr(args, "reason", "") or "closed"),
            sweep_ticket_claims=True,
            events=[
                ctl.ticket_event(
                    "ticket_closed",
                    t.id,
                    payload={"ticket_id": t.id, "closed": t.closed, "agent": agent},
                )
            ],
        )
    print(f"closed {t.id} -> {sink.location(t.id)}")


def cmd_reopen(args):
    """Reopen a ticket and record what its dependents lose.

    Reopening must not silently undo running or finished work: the reopened
    ticket is moved back to `open`, but tickets that depend on it are left with
    their status and assignee untouched -- they get `dependency_invalidated`
    events naming them instead. A claim that already committed keeps its attempt;
    a claim that starts afterwards sees the reopened dependency and is refused by
    readiness."""
    sink = _require_sink(args)
    agent = getattr(args, "agent", "system")
    ctl = _lifecycle(args, sink, agent=agent)
    with ctl.locked():
        t = sink.get(args.id, unique=True)
        if t.status == "open":
            raise TicketError(f"ticket {t.id} is already open")
        attempt = ctl.active_attempt(t.id)
        if attempt is not None:
            ctl.require_ownership(
                attempt,
                agent,
                force=getattr(args, "force", False),
                reason=getattr(args, "reason", ""),
                action="reopen",
            )
        before = copy.deepcopy(t)
        t.closed = None
        t.blocked_by = None
        t.status = "open"
        t.updated = schema.now()
        schema.append_note(t, agent, "Reopened.")
        revoked = attempt is not None and _revocation_requested(args, attempt, agent)
        ctl.commit_transition(
            "reopen",
            t,
            previous=before,
            end=attempt,
            end_state=("interrupted" if revoked else "released") if attempt is not None else None,
            reason=(getattr(args, "reason", "") or "reopened"),
            events=[
                ctl.ticket_event("ticket_reopened", t.id, payload={"ticket_id": t.id, "agent": agent})
            ],
        )
        affected = ctl.invalidate_dependents(t, sink.query(TicketQuery(buckets=("*",))))
    if affected:
        print(
            f"note: {len(affected)} live ticket(s) depend on {t.id} and are no longer "
            f"workable: {', '.join(affected)}",
            file=sys.stderr,
        )
    print(f"reopened {t.id} -> {sink.location(t.id)}")


def cmd_shelve(args):
    """Shelve a ticket, releasing any active attempt.

    A shelved ticket is parked, so its attempt is released (nobody is working it)
    unless another agent is revoking with `--force --reason`, in which case it is
    interrupted."""
    sink = _require_sink(args)
    agent = getattr(args, "agent", "system")
    ctl = _lifecycle(args, sink, agent=agent)
    with ctl.locked():
        t = sink.get(args.id, unique=True)
        attempt = ctl.active_attempt(t.id)
        if attempt is not None:
            ctl.require_ownership(
                attempt,
                agent,
                force=getattr(args, "force", False),
                reason=args.reason,
                action="shelve",
            )
        before = copy.deepcopy(t)
        t.status = "shelved"
        t.updated = schema.now()
        message = "Shelved." if not args.reason else f"Shelved: {args.reason}"
        schema.append_note(t, "system", message)
        revoked = attempt is not None and _revocation_requested(args, attempt, agent)
        ctl.commit_transition(
            "shelve",
            t,
            previous=before,
            end=attempt,
            end_state=("interrupted" if revoked else "released") if attempt is not None else None,
            reason=args.reason or None,
            events=[
                ctl.ticket_event("ticket_shelved", t.id, payload={"ticket_id": t.id, "agent": agent})
            ],
        )
    print(f"shelved {t.id} -> {sink.location(t.id)}")


def cmd_unshelve(args):
    """Bring a shelved ticket back to open/.

    The counterpart to shelve: a ticket that was parked (deprioritized or
    paused) is moved back into the open status so it shows up in
    `arbite list next` again. The assignee and any stale block reason are
    cleared -- an unshelved ticket is back in the unclaimed pool, not reserved
    for whoever parked it. Any active attempt is released, since the ticket is
    returning to the unclaimed pool."""
    sink = _require_sink(args)
    agent = getattr(args, "agent", "system")
    ctl = _lifecycle(args, sink, agent=agent)
    with ctl.locked():
        t = sink.get(args.id, unique=True)
        if t.status != "shelved":
            raise TicketError(f"ticket {t.id} is not shelved (status: {t.status})")
        attempt = ctl.active_attempt(t.id)
        if attempt is not None:
            ctl.require_ownership(
                attempt,
                agent,
                force=getattr(args, "force", False),
                reason=args.reason,
                action="unshelve",
            )
        before = copy.deepcopy(t)
        t.updated = schema.now()
        message = "Unshelved." if not args.reason else f"Unshelved: {args.reason}"
        schema.append_note(t, "system", message)
        t.assignee = None
        t.blocked_by = None
        t.status = "open"
        revoked = attempt is not None and _revocation_requested(args, attempt, agent)
        ctl.commit_transition(
            "unshelve",
            t,
            previous=before,
            end=attempt,
            end_state=("interrupted" if revoked else "released") if attempt is not None else None,
            reason=args.reason or None,
            events=[
                ctl.ticket_event(
                    "ticket_unshelved", t.id, payload={"ticket_id": t.id, "agent": agent}
                )
            ],
        )
    print(f"unshelved {t.id} -> {sink.location(t.id)}")


def cmd_note(args):
    sink = _require_sink(args)
    with _ticket_write_lock(sink):
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
    argument, all of <tic_a>'s dependencies are cleared.

    Held under the same operation lock acquisition validates readiness under, so
    a dependency added while a claim is in flight either lands first (and the
    claim is refused) or after (and the claim stands) -- never erased."""
    sink = _require_sink(args)
    with _ticket_write_lock(sink):
        _depend_locked(args, sink)


def _depend_locked(args, sink):
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
    file sink moves the ticket's file (a folder, created if missing), the SQLite
    sink records a bucket. '/' returns the ticket to where its status says it
    belongs, which is the only way to un-file a ticket without changing its
    status. Nothing here changes a field -- use the status commands
    (claim/block/close/...) for moves that are state changes, which also un-file
    the ticket automatically."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)

    folder = args.folder.strip()
    if not folder.startswith("/"):
        raise TicketError(
            f"<folder> must be a root-relative path like '/wishlist', or '/' to return "
            f"the ticket to its status location, got '{args.folder}'"
        )
    # Root-relative -> bucket name, dropping empty/'.' segments and refusing '..'
    # so a ticket can never be filed outside the arbite root.
    parts = [p for p in folder[1:].split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise TicketError(
            f"<folder> may not contain '..' (it must stay under the arbite root): '{folder}'"
        )
    bucket = "/".join(parts) if parts else None

    if sink.bucket(t.id) == bucket:
        print(f"{t.id} is already at {sink.location(t.id)}")
        return
    sink.move_to_bucket(t.id, bucket)
    print(f"moved {t.id} -> {sink.location(t.id)}")


def cmd_set(args):
    """Set one or more ticket properties on an existing ticket. Properties come in
    PROPERTY VALUE pairs (any number per call); quote any value that spans more
    than one word. Type-aware: 'tags'/'depends_on' are comma-separated lists,
    'priority' must be an integer, and an empty quoted value ('') clears a field.
    A 'status' change also un-files the ticket (the file sink moves it) so status
    and location stay in sync, and auto-dates 'closed' when a ticket is set to
    closed."""
    sink = _require_sink(args)
    ctl = _lifecycle(args, sink)
    with ctl.locked():
        _set_locked(args, sink, ctl)


def _set_locked(args, sink, ctl):
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

    # `status`/`assignee` are ownership-bearing fields: while an attempt is live
    # they must go through the lifecycle commands, which end/touch the attempt and
    # record the change. Other field edits stay allowed and merely touch the
    # attempt. With no attempt, the legacy field-edit behaviour is unchanged.
    attempt = ctl.active_attempt(t.id)
    requested_status = None
    requested_assignee = None
    for prop, value in pairs:
        if prop == "status":
            requested_status = value
        if prop == "assignee":
            requested_assignee = schema.coerce_field_value("assignee", value)
    if attempt is not None:
        ctl.refuse_status_edit(
            t,
            new_status=requested_status,
            new_assignee=requested_assignee,
            attempt=attempt,
        )

    expect = _expect_from(t)
    original_status = t.status
    new_status = None
    updated_given = False
    for prop, value in pairs:
        if prop == "status":
            new_status = value
        if prop == "updated":
            updated_given = True
        setattr(t, prop, schema.coerce_field_value(prop, value))

    if not updated_given:
        t.updated = schema.now()

    if new_status is not None and new_status != original_status:
        # A real status change also re-files the ticket (the file sink relocates
        # the file, the database sink clears its bucket), mirroring
        # claim/close/etc.; moving to closed auto-dates 'closed' like
        # `arbite close` does.
        if new_status == "closed" and t.closed is None:
            t.closed = t.updated
        t.status = new_status

    sink.update(t, expect=expect)
    if attempt is not None:
        ctl.touch(attempt)
    what = ", ".join(prop for prop, _ in pairs)
    print(f"set {what} on {t.id} at {sink.location(t.id)}")


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

    The checks themselves live in the sink, because part of the point of a
    pluggable store is that its failure modes differ: a file sink can suffer
    frontmatter/folder drift, a stray temp file or an archive in the wrong month,
    while a database sink can suffer a stale derived index or structural
    corruption. The checks that mean the same thing either way -- invalid field
    values, deadlocked dependencies, a claimed ticket with no assignee -- are
    shared, so `doctor` cannot mean two different things per sink.

    Exits 3 when problems remain, so this can gate CI or an agent's startup."""
    sink = _require_sink(args)
    problems = sink.check(fix=args.fix)
    info = sink.describe()
    fixed = sum(1 for p in problems if p.fixed)
    remaining = sum(1 for p in problems if not p.fixed)

    if args.json:
        _print_json(
            {
                "sink": {"kind": info.kind, "root": info.root},
                "tickets_checked": info.ticket_count,
                "problems": [p.to_dict() for p in problems],
                "fixed": fixed,
                "remaining": remaining,
            }
        )
    else:
        if not problems:
            print(f"checked {info.ticket_count} tickets: no problems found")
        else:
            for p in problems:
                prefix = "fixed" if p.fixed else "problem"
                where = f" [{p.ticket_id}]" if p.ticket_id else ""
                print(f"{prefix}{where} {p.kind}: {p.detail}")
            print(
                f"\nchecked {info.ticket_count} tickets: {remaining} problem(s), {fixed} fixed"
            )
            if remaining and not args.fix:
                print("re-run with --fix to repair what arbite can correct automatically")

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
    # `--force` overrides the confirmation, never the lifecycle rules: a ticket
    # with an active attempt/claim is refused so cleanup is not bypassed, and one
    # with retained change history is refused rather than silently cascaded away.
    ctl = _lifecycle(args, sink, agent=getattr(args, "agent", None))
    note = f"Deleted by {args.agent}" + (f": {args.reason}" if args.reason else ".")
    location = sink.location(t.id)
    with ctl.locked():
        t = sink.get(t.id, unique=True)
        ctl.require_deletable(t)
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
    away from is the same silent-wrong-store trap `init` closes.

    Shared-directory coordination history (attempts, claims, receipts, intents,
    events, artifacts) is moved too when *both* sinks expose a coordination store
    and the source actually has coordination state. `--coordination` forces the
    transfer on and `--no-coordination` forces it off. The transfer refuses a
    non-quiescent source (naming what is active), verifies the destination, then
    rebinds the marker, the stored `StoreBinding` and `sink:` so all three agree.
    It never prunes source coordination state: artifacts and evidence are
    retained, so disk growth is expected. `--prune` still prunes tickets only.

    The order is deliberate and is the guarantee the command offers:

    1. resolve the workspace and refuse a non-quiescent source -- this happens
       *before* the destination is initialised or written, so a refused migration
       leaves the destination exactly as it was (no tickets copied, marker and
       `sink:` still naming the source);
    2. copy the tickets;
    3. transfer the coordination history and verify the destination;
    4. rebind the marker and the stored `StoreBinding`;
    5. switch `sink:` in the config.

    `--dry-run` performs step 1 too -- so a non-quiescent source is reported as a
    refusal rather than hidden behind a "would migrate" line -- and writes
    nothing.
    """
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

    source_store = source.coordination()
    target_store = target.coordination()
    coordination_available = source_store is not None and target_store is not None
    coordination_state = coordination_available and _coordination_state_present(source_store)
    if args.coordination is None:
        coordination_active = coordination_state
    elif args.coordination:
        if not coordination_available:
            raise UnsupportedCoordination(
                "coordination transfer was requested, but the "
                f"{source.kind if source_store is None else target.kind} sink does not "
                "implement the shared-directory coordination contract"
            )
        coordination_active = True
    else:
        coordination_active = False

    tickets = source.query(TicketQuery(buckets=("*",)))
    if not tickets:
        # The message is never lost, but when coordination state is being moved
        # there is work to do even with no tickets -- so the early exit is skipped.
        print(f"no tickets found in the {source.kind} sink at {source.root}")
        if not (coordination_active and coordination_state):
            sys.exit(EXIT_EMPTY)

    # Refuse a non-quiescent source BEFORE anything touches the destination. The
    # ticket copy below writes into the destination, so the transfer's refusal has
    # to happen here -- for a real run and for --dry-run alike (both write
    # nothing). The same CoordinationConflict names the blocking
    # attempts/claims/intents/journals.
    workspace_id = None
    if coordination_active:
        workspace_id = _migration_workspace_id(source_store, project_root)
        coordination_export.require_quiescent_store(source_store, workspace_id)

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
        if coordination_active:
            bundle = coordination_export.export_coordination(source_store)
            counts = coordination_export.bundle_counts(bundle)
            print(
                "would transfer coordination history "
                f"(namespace {bundle.get('cursor_namespace')}): "
                f"{counts['work_attempts']} attempt(s), {counts['file_claims']} claim(s), "
                f"{counts['operation_receipts']} receipt(s), "
                f"{counts['operation_intents']} intent(s), {counts['events']} event(s), "
                f"{counts['artifacts']} artifact(s); artifacts and evidence are retained "
                "(disk growth is expected)"
            )
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

    # Coordination first, and before any destructive prune: migrate_coordination
    # refuses a non-quiescent source, exports, verifies the bundle, imports and
    # then re-verifies the destination -- all before the marker or `sink:` move.
    if coordination_active:
        result = coordination_export.migrate_coordination(
            source_store,
            target_store,
            workspace_id=workspace_id,
            overwrite=args.overwrite,
        )
        records = result.get("records", {})
        print(
            "transferred coordination history "
            f"({result.get('source_namespace')} -> {result.get('target_namespace')}): "
            f"{records.get('work_attempts', 0)} attempt(s), "
            f"{records.get('file_claims', 0)} claim(s), "
            f"{records.get('operation_receipts', 0)} receipt(s), "
            f"{records.get('operation_intents', 0)} intent(s), "
            f"{result.get('events', 0)} event(s), {result.get('artifacts', 0)} artifact(s); "
            "destination verified, artifacts and evidence are retained "
            "(disk growth is expected)"
        )

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

    # Only now, after the destination verified and the copy is done, move the
    # authoritative binding so the marker, the stored binding and the config agree.
    if coordination_active:
        _rebind_to_coordination_store(project_root, target, target_store)

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


#: Record kinds that mean a store holds actual coordination *work*. A workspace
#: record and a store binding are created merely by building the lifecycle service
#: (any `set`/`claim`/`close` binds the workspace), so they alone would make every
#: migration of a once-touched project try to move coordination history.
_COORDINATION_WORK_KINDS = (
    "work_attempt",
    "file_claim",
    "read_observation",
    "operation_receipt",
    "operation_intent",
    "recovery_report",
    "lifecycle_intent",
)


def _coordination_state_present(store) -> bool:
    """Whether `store` holds real coordination *work*, not merely a binding.

    A freshly initialised SQLite store already has its coordination tables, and a
    workspace record plus store binding are written the first time any lifecycle
    service is built (a `set`, a `claim`, ...). Requiring at least one attempt/
    claim/observation/receipt/intent/recovery record makes "the source has
    coordination state to move" mean substantive history rather than plumbing, so
    ticket-only migrations are unchanged. Read-only and defensive: any failure
    here just means "nothing to transfer", and an explicit --coordination still
    reports the real problem through `migrate_coordination`.
    """
    try:
        if not store.is_initialised():
            return False
        with store.transaction(write=False) as tx:
            return any(list(tx.find(kind)) for kind in _COORDINATION_WORK_KINDS)
    except ArbiteError:
        return False


def _marker_names_store(marker, store) -> bool:
    """Whether the binding `marker` names the same store as `store`.

    Compares both the kind (from the store's cursor namespace) and the location
    (through `binding_location()`), so a marker for the file sink is never taken
    to name the SQLite sink that happens to share the same `.arbite` directory.
    Read-only: a store that cannot answer counts as "not this store".
    """
    try:
        namespace = store.cursor_namespace()
        location = store.binding_location()
    except ArbiteError:
        return False
    kind = namespace.split(":", 1)[0] if isinstance(namespace, str) else None
    if marker.get("sink_kind") != kind:
        return False
    return _same_location(marker.get("location"), location)


def _same_location(left, right) -> bool:
    """Whether two store locations denote the same place (realpath when possible)."""
    if not left or not right:
        return False
    try:
        return os.path.realpath(str(left)) == os.path.realpath(str(right))
    except OSError:  # pragma: no cover - defensive
        return str(left) == str(right)


def _migration_workspace_id(source_store, project_root):
    """The workspace a coordination migration is about, or `None`.

    Prefers the project's binding marker when it names the *source* store and that
    workspace actually exists in the source (the normal case: the marker says
    which store the project coordinates against). Otherwise falls back to the
    single workspace the source store holds, or `None` when there is none or more
    than one -- `migrate_coordination` then resolves it and reports the ambiguity.
    Read-only: an uninitialised source exports a well-formed empty bundle.
    """
    try:
        bundle = coordination_export.export_coordination(source_store)
    except ArbiteError:
        return None
    records = bundle.get("records") if isinstance(bundle, dict) else None
    workspace_records = records.get("workspaces") if isinstance(records, dict) else None
    workspace_ids = [
        record.get("id")
        for record in (workspace_records or [])
        if isinstance(record, dict) and record.get("id")
    ]
    marker = None
    try:
        marker = workspace.load_marker(project_root / config.ARBITE_DIRNAME)
    except ArbiteError:
        marker = None
    if marker is not None and _marker_names_store(marker, source_store):
        marker_id = marker.get("workspace_id")
        if marker_id in workspace_ids:
            return marker_id
    if len(workspace_ids) == 1:
        return workspace_ids[0]
    return None


def _rebind_to_coordination_store(project_root, target, target_store) -> None:
    """Make `target` the authoritative store for this project's workspace.

    Writes the marker through `workspace.ensure_binding`, which also stores the
    authoritative `StoreBinding` in the target and (best-effort) a
    `workspace_bound` event. Already matching the target is a no-op; no marker
    means this is the first bind (`rebind=False`); a marker naming a different
    store is an explicit rebind (`rebind=True`), which verifies the previously
    bound store is quiescent.
    """
    arbite_dir = project_root / config.ARBITE_DIRNAME
    marker = workspace.load_marker(arbite_dir)
    location = str(target.root)
    if marker is not None and (
        str(marker.get("sink_kind")) == str(target.kind)
        and str(marker.get("location")) == location
    ):
        return
    workspace.ensure_binding(
        arbite_dir,
        root=str(project_root),
        sink_kind=target.kind,
        location=location,
        store=target_store,
        rebind=marker is not None,
    )


def _target_exists(target) -> bool:
    """Whether the migration target already holds tickets. Used by --dry-run,
    which must not create the target's store merely to report on it."""
    try:
        return bool(target.ids())
    except ArbiteError:
        return False


# --- export and explicit rebind (shared-directory coordination, C11) --------


_EXPORT_COORDINATION_COUNT_KEYS = (
    "workspaces",
    "store_bindings",
    "work_attempts",
    "file_claims",
    "read_observations",
    "operation_receipts",
    "operation_intents",
    "recovery_reports",
    "lifecycle_intents",
    "worker_profiles",
    "events",
    "artifacts",
)


def _ticket_export_document(sink) -> dict:
    """The `--scope tickets` document, in the same rendering `list`/`show` use."""
    rows = sink.query(TicketQuery(buckets=("*",)))
    locations = sink.location_map(rows)
    return {
        "schema_version": 1,
        "arbite_ticket_export": 1,
        "sink": {"kind": sink.kind, "root": str(sink.root)},
        "tickets": [t.to_dict(locations.get(t.id)) for t in rows],
    }


def _coordination_count_line(counts) -> str:
    return ", ".join(
        f"{counts.get(key, 0)} {key}" for key in _EXPORT_COORDINATION_COUNT_KEYS
    )


def cmd_export(args):
    """Export coordination history and/or tickets as one JSON document.

    READ-ONLY: nothing here creates the store, the `.arbite` directory or the
    coordination layout. An uninitialised coordination store exports a
    well-formed *empty* bundle (correct namespace and contract, no records) and
    creates nothing.

    Scopes:

    - `coordination` (default): a `coordination_export` bundle.
    - `tickets`: `{"schema_version": 1, "arbite_ticket_export": 1,
      "sink": {"kind", "root"}, "tickets": [<ticket dicts>]}`.
    - `all`: `{"schema_version": 1, "coordination": <bundle>,
      "tickets": <ticket document>}`.

    `--out FILE` writes the document atomically (temp + `os.replace`) and prints
    a short human summary; without `--out` the document itself is printed to
    stdout so it can be redirected. `--workspace ID` filters the coordination
    export to one workspace and `--no-artifacts` keeps artifact *metadata* while
    omitting the bytes (`data_omitted`).

    `--json` prints a `coordination.ok_result(...)` status payload instead of the
    document. The `data` mapping is stable and later tickets extend it::

        {"scope", "workspace_id", "cursor_namespace", "retained_history",
         "counts", "out", "include_artifacts"}

    where `counts` is the bundle's per-group counts for `coordination`,
    `{"tickets": n}` for `tickets`, and `{"coordination": {...}, "tickets": n}`
    for `all`. `out` is the written path or `None`.
    """
    sink = _require_sink(args)
    scope = args.scope

    coordination_bundle = None
    ticket_document = None
    if scope in ("coordination", "all"):
        coordination_bundle = coordination_export.export_coordination(
            sink,
            workspace_id=args.workspace,
            include_artifacts=not args.no_artifacts,
        )
    if scope in ("tickets", "all"):
        ticket_document = _ticket_export_document(sink)

    if scope == "coordination":
        document = coordination_bundle
        counts = coordination_export.bundle_counts(coordination_bundle)
        namespace = coordination_bundle.get("cursor_namespace")
        ticket_count = None
    elif scope == "tickets":
        document = ticket_document
        ticket_count = len(ticket_document["tickets"])
        counts = {"tickets": ticket_count}
        namespace = None
    else:
        ticket_count = len(ticket_document["tickets"])
        document = {
            "schema_version": 1,
            "coordination": coordination_bundle,
            "tickets": ticket_document,
        }
        counts = {
            "coordination": coordination_export.bundle_counts(coordination_bundle),
            "tickets": ticket_count,
        }
        namespace = coordination_bundle.get("cursor_namespace")

    out_path = str(args.out) if args.out else None
    if out_path is not None:
        out_path = coordination_export.write_bundle(document, out_path)

    if args.json:
        _print_json(
            coordination.ok_result(
                {
                    "scope": scope,
                    "workspace_id": args.workspace,
                    "cursor_namespace": namespace,
                    "retained_history": True,
                    "counts": counts,
                    "out": out_path,
                    "include_artifacts": not args.no_artifacts,
                }
            )
        )
        return

    if out_path is None:
        print(json.dumps(document, indent=2, ensure_ascii=False, default=str))
        return

    label = {
        "coordination": "coordination export",
        "tickets": "ticket export",
        "all": "coordination+ticket export",
    }[scope]
    print(f"wrote {label} to {out_path}")
    if scope in ("coordination", "all"):
        print(f"  namespace {namespace}, retained_history True")
        print(
            "  "
            + _coordination_count_line(
                coordination_export.bundle_counts(coordination_bundle)
            )
        )
    if scope in ("tickets", "all"):
        print(f"  {ticket_count} ticket(s)")


def _emit_rebind_result(args, payload, lines) -> None:
    """Print the documented `ok_result` payload, or the human lines, never both."""
    if getattr(args, "json", False):
        _print_json(coordination.ok_result(payload))
        return
    for line in lines:
        print(line)


def cmd_rebind(args):
    """Switch this project's authoritative coordination store, explicitly.

    The current binding is the `.arbite/workspace-binding.json` marker when it
    exists (the config/`--sink` selection only describes "from" when there is no
    marker yet). Verification, in order, and every step *before* anything is
    written:

    1. the currently bound store is quiescent -- `workspace.require_quiescent`
       refuses while it still has an active work attempt or file claim (no marker
       means there is nothing to unbind);
    2. the destination sink can be opened and exposes a coordination store;
    3. the destination's `contract_version()` equals `coordination.CONTRACT_VERSION`;
    4. when the destination store is already initialised,
       `coordination_doctor.coordination_problems(..., fix=False)` must be empty.

    Only then does it rewrite the marker through `workspace.ensure_binding`
    (writing the stored `StoreBinding` too, and rebinding quiescently) and make
    the destination the project default with `config.set_configured_sink`. There
    is no sleeping, daemon, retry or automatic takeover.

    `--dry-run` performs every check and writes nothing. `--json` prints a
    `coordination.ok_result` payload with `rebound`, `from`, `to`, `workspace_id`,
    `dry_run` and `verified`.
    """
    if not args.to:
        raise TicketError(
            "rebind needs --to KIND to name the store being switched to "
            f"(valid: {', '.join(SINK_KINDS)})"
        )
    project_root = config.find_project_root()
    arbite_dir = project_root / config.ARBITE_DIRNAME
    current = config.open_sink(getattr(args, "sink", None), project_root)
    marker = workspace.load_marker(arbite_dir)

    destination = config.open_sink_kind(args.to, project_root)
    dest_location = str(destination.root)

    if marker is not None:
        from_kind = str(marker["sink_kind"])
        from_location = str(marker["location"])
        workspace_id = marker["workspace_id"]
    else:
        from_kind = current.kind
        from_location = str(current.root)
        workspace_id = workspace.stable_workspace_id(str(project_root))

    if from_kind == str(destination.kind) and from_location == dest_location:
        raise TicketError(
            f"already bound to {from_kind}:{from_location}; rebind needs a different "
            "--to store"
        )

    if marker is not None:
        previous = coordination.StoreBinding(
            id=coordination.new_record_id("store_binding"),
            workspace_id=workspace_id,
            sink_kind=from_kind,
            location=from_location,
            bound_at=marker["bound_at"],
        )
        workspace.require_quiescent(previous, workspace_id)

    dest_store = destination.coordination()
    if dest_store is None:
        raise UnsupportedCoordination(
            f"the {destination.kind} sink does not implement the shared-directory "
            "coordination contract, so it cannot become the authoritative store"
        )
    contract = dest_store.contract_version()
    if contract != coordination.CONTRACT_VERSION:
        raise TicketError(
            f"the {destination.kind} sink implements coordination contract version "
            f"{contract}, but this arbite speaks {coordination.CONTRACT_VERSION}; "
            "refusing to rebind"
        )
    if dest_store.is_initialised():
        problems = coordination_doctor.coordination_problems(destination, fix=False)
        if problems:
            kinds = ", ".join(sorted({p.kind for p in problems}))
            raise CoordinationConflict(
                f"refusing to rebind to {destination.kind}:{dest_location}: its "
                f"coordination store has integrity problems that must be resolved first "
                f"({kinds})",
                details={"problems": [p.to_dict() for p in problems]},
            )

    from_label = f"{from_kind}:{from_location}"
    to_label = f"{destination.kind}:{dest_location}"

    if args.dry_run:
        _emit_rebind_result(
            args,
            {
                "rebound": False,
                "from": {"kind": from_kind, "location": from_location},
                "to": {"kind": destination.kind, "location": dest_location},
                "workspace_id": workspace_id,
                "dry_run": True,
                "verified": True,
            },
            [
                f"would rebind workspace {workspace_id} from {from_label} to {to_label} "
                "(verified; nothing written)"
            ],
        )
        return

    resolution = workspace.ensure_binding(
        arbite_dir,
        root=str(project_root),
        sink_kind=destination.kind,
        location=dest_location,
        store=dest_store,
        rebind=marker is not None,
    )
    written = config.set_configured_sink(destination.kind, project_root)
    _emit_rebind_result(
        args,
        {
            "rebound": True,
            "from": {"kind": from_kind, "location": from_location},
            "to": {"kind": destination.kind, "location": dest_location},
            "workspace_id": resolution.workspace_id,
            "dry_run": False,
            "verified": True,
        },
        [
            f"rebound workspace {resolution.workspace_id} from {from_label} to {to_label}",
            f"set 'sink: {destination.kind}' in {written.name}",
        ],
    )


# --- file ownership (shared-directory coordination, C04) -------------------


def _load_attempt_for(store, ticket_id: str, attempt_id: str):
    """The work attempt `attempt_id`, which must belong to `ticket_id`."""
    with store.transaction(write=False) as tx:
        attempt = tx.get("work_attempt", attempt_id)
    if attempt is None or attempt.ticket_id != ticket_id:
        raise CoordinationNotFound(
            f"no attempt {attempt_id!r} is recorded for ticket {ticket_id!r}",
            details={"ticket_id": ticket_id, "attempt_id": attempt_id},
        )
    return attempt


def _file_claim_context(args, sink):
    """The `FileClaimService` and the attempt this file command acts for."""
    store = sink.coordination()
    if store is None:
        raise UnsupportedCoordination(
            f"the {sink.kind} sink does not implement the shared-directory "
            "coordination contract, so file claims cannot be recorded"
        )
    attempt = _load_attempt_for(store, args.ticket, args.attempt)
    service = application.coordination_service_for(
        sink, root=str(config.find_project_root()), actor=Actor(attempt.worker_id)
    )
    if attempt.workspace_id != service.workspace.id:
        raise ClaimConflict(
            f"attempt {attempt.id} belongs to workspace {attempt.workspace_id}, but "
            f"this project root is workspace {service.workspace.id}; file claims are "
            "per workspace and cannot cross a rebind",
            details={
                "attempt_id": attempt.id,
                "attempt_workspace": attempt.workspace_id,
                "workspace_id": service.workspace.id,
            },
        )
    return fileclaims.FileClaimService(service), attempt


def _emit_file_result(args, payload, human_lines) -> None:
    """Print the documented JSON payload, or the human lines, never both."""
    if getattr(args, "json", False):
        _print_json(coordination.ok_result(payload))
        return
    for line in human_lines:
        print(line)


def _file_error(args, error) -> None:
    """Render a coordination failure as JSON under `--json`, else re-raise."""
    if getattr(args, "json", False):
        _print_json(error.to_result())
        sys.exit(EXIT_ERROR)
    raise error


def cmd_file_claim(args):
    """Claim one or more workspace paths exclusively for a ticket attempt.

    Runs entirely through `arbite.fileclaims.FileClaimService`, so the canonical
    path validation, the all-or-nothing claim set, the `file_busy` holder payload
    and the idempotent reentrant acquisition are the same whether invoked here or
    from another caller."""
    sink = _require_sink(args)
    try:
        service, attempt = _file_claim_context(args, sink)
        result = service.claim(attempt, args.paths)
    except CoordinationError as error:
        _file_error(args, error)
        return
    payload = {
        "workspace_id": result.workspace_id,
        "ticket_id": result.ticket_id,
        "attempt_id": result.attempt_id,
        "operation_id": result.operation_id,
        "paths": result.paths,
        "claimed": [claim.to_dict() for claim in result.acquired],
        "reentrant": [claim.to_dict() for claim in result.reentrant],
    }
    lines = [
        f"claimed {claim.path} (generation {claim.generation}, "
        f"version {claim.observed_version}) for {result.ticket_id} "
        f"attempt {result.attempt_id}"
        for claim in result.acquired
    ] + [
        f"already held {claim.path} (generation {claim.generation}) by "
        f"{result.ticket_id} attempt {result.attempt_id}"
        for claim in result.reentrant
    ]
    _emit_file_result(args, payload, lines or ["nothing claimed"])


def cmd_file_release(args):
    """Release this attempt's exclusive claims on one or more paths.

    The work attempt is retained; only the file token is revoked. A later claim of
    the same path mints a new generation, so an old read token cannot authorize a
    write after the release."""
    sink = _require_sink(args)
    try:
        service, attempt = _file_claim_context(args, sink)
        result = service.release(attempt, args.paths, reason=args.reason)
    except CoordinationError as error:
        _file_error(args, error)
        return
    payload = {
        "workspace_id": result.workspace_id,
        "ticket_id": result.ticket_id,
        "attempt_id": result.attempt_id,
        "operation_id": result.operation_id,
        "reason": result.reason,
        "released": [claim.to_dict() for claim in result.released],
        "already_released": [claim.to_dict() for claim in result.already_released],
    }
    lines = [
        f"released {claim.path} (generation {claim.generation}) for "
        f"{result.ticket_id} attempt {result.attempt_id}: {result.reason}"
        for claim in result.released
    ] + [
        f"already released {claim.path} (generation {claim.generation})"
        for claim in result.already_released
    ]
    _emit_file_result(args, payload, lines or ["nothing released"])


# --- bounded discovery and versioned reads (shared-directory coordination, C06) --


def _line_range_arg(value):
    """argparse type for `--lines START:END`: a usage error when malformed."""
    try:
        return filereads.parse_line_range(value)
    except UnsupportedCoordination as error:
        raise argparse.ArgumentTypeError(str(error))


def _file_read_context(args, sink, *, require_attempt: bool):
    """The `FileReadService` (and, when required, the attempt) a read command acts for.

    Reads do not take ownership, so no claim is acquired here; the attempt is
    loaded only to record whose observation a read is. The workspace check is the
    same one the claim surface applies, so a read and a claim can never disagree
    about which workspace a path belongs to."""
    store = sink.coordination()
    if store is None:
        raise UnsupportedCoordination(
            f"the {sink.kind} sink does not implement the shared-directory "
            "coordination contract, so reads cannot be recorded"
        )
    service = application.coordination_service_for(
        sink, root=str(config.find_project_root())
    )
    attempt = None
    if require_attempt:
        attempt = _load_attempt_for(store, args.ticket, args.attempt)
        if attempt.workspace_id != service.workspace.id:
            raise ClaimConflict(
                f"attempt {attempt.id} belongs to workspace {attempt.workspace_id}, but "
                f"this project root is workspace {service.workspace.id}; reads and claims "
                "are per workspace and cannot cross a rebind",
                details={
                    "attempt_id": attempt.id,
                    "attempt_workspace": attempt.workspace_id,
                    "workspace_id": service.workspace.id,
                },
            )
    return filereads.FileReadService(service), attempt, service


def _discovery_lines(page) -> list:
    """Human lines for a list/search page, with explicit truncation markers."""
    lines = []
    for entry in getattr(page, "entries", None) or []:
        version = f" {entry.version}" if entry.version else ""
        omitted = " (version omitted)" if entry.version_omitted else ""
        lines.append(f"{entry.kind} {entry.path}{version}{omitted}")
    for match in getattr(page, "matches", None) or []:
        if match.line is None:
            lines.append(f"path {match.path}")
        else:
            shown = match.text if not match.text_truncated else match.text + "..."
            lines.append(f"{match.path}:{match.line}: {shown}")
    for marker in page.markers:
        lines.append(f"marker: {marker}")
    if page.truncated:
        if page.next_offset is not None:
            lines.append(f"truncated: next_offset={page.next_offset}")
        else:
            lines.append("truncated: no deterministic next page (scan limit reached)")
    return lines


def cmd_file_list(args):
    """List workspace entries (bounded, deterministic, JSON-capable)."""
    sink = _require_sink(args)
    try:
        service, _attempt, _svc = _file_read_context(args, sink, require_attempt=False)
        page = service.list(
            getattr(args, "path", None), limit=args.limit, offset=args.offset
        )
    except CoordinationError as error:
        _file_error(args, error)
        return
    _emit_file_result(args, page.to_dict(), _discovery_lines(page) or ["no paths found"])


def cmd_file_search(args):
    """Search workspace paths and text lines (bounded, text/path discovery only)."""
    sink = _require_sink(args)
    try:
        service, _attempt, _svc = _file_read_context(args, sink, require_attempt=False)
        page = service.search(
            args.pattern,
            getattr(args, "path", None),
            limit=args.limit,
            offset=args.offset,
        )
    except CoordinationError as error:
        _file_error(args, error)
        return
    _emit_file_result(args, page.to_dict(), _discovery_lines(page) or ["no matches found"])


def cmd_file_read(args):
    """Serve a file (or a line range) with an explicit write receipt.

    Unlike a mutation this takes no lock and acquires no ownership. A file held by
    another attempt is still served, but the receipt is non-writable and names the
    busy owner; `--fail-if-busy` refuses instead so a caller can avoid spending
    tokens on bytes it may not write. Without `--json` the served text goes to
    stdout and any non-writable/busy warning goes to stderr."""
    sink = _require_sink(args)
    try:
        service, attempt, _svc = _file_read_context(args, sink, require_attempt=True)
        receipt = service.read(
            attempt,
            args.path,
            lines=getattr(args, "lines", None),
            actor=Actor(attempt.worker_id),
            fail_if_busy=args.fail_if_busy,
            version_only=bool(getattr(args, "version_only", False)),
        )
    except CoordinationError as error:
        _file_error(args, error)
        return
    if getattr(args, "json", False):
        _print_json(coordination.ok_result(receipt.to_dict()))
        return
    if receipt.busy:
        print(
            f"warning: {receipt.path} is held by ticket "
            f"{receipt.busy_owner['holder_ticket']} (attempt "
            f"{receipt.busy_owner['holder_attempt']}); this read is NOT "
            "write-authorizing",
            file=sys.stderr,
        )
    elif receipt.non_writable:
        print(
            f"warning: this read is NOT write-authorizing "
            f"({receipt.non_writable_reason}); claim and re-read before writing",
            file=sys.stderr,
        )
    if receipt.version_only:
        print(f"{receipt.path}: {receipt.whole_file_digest} ({receipt.size} bytes, {receipt.encoding})")
        print(f"read_token: {receipt.read_token}")
        return
    end = "" if receipt.text.endswith("\n") else "\n"
    print(receipt.text, end=end)


def cmd_file_probe(args):
    """Inspect an absent path for a safe create (records nothing, owns nothing)."""
    sink = _require_sink(args)
    try:
        service, attempt, _svc = _file_read_context(args, sink, require_attempt=True)
        receipt = service.probe(attempt, args.path)
    except CoordinationError as error:
        _file_error(args, error)
        return
    payload = receipt.to_dict()
    lines = [
        f"{receipt.path}: " + ("exists" if receipt.exists else "absent"),
        f"version: {receipt.version}",
        f"safe_to_create: {'yes' if receipt.safe_to_create else 'no'}",
        receipt.next_action,
    ]
    _emit_file_result(args, payload, lines)


# --- version-checked mutations (shared-directory coordination, C07) ---------


def _file_mutation_context(args, sink):
    """The `FileMutationService` and the attempt a write/edit command acts for.

    Mutations act for an explicitly loaded, active attempt and are recorded
    through the same workspace binding as claims and reads, so a mutation can
    never disagree about which workspace a path belongs to."""
    store = sink.coordination()
    if store is None:
        raise UnsupportedCoordination(
            f"the {sink.kind} sink does not implement the shared-directory "
            "coordination contract, so file mutations cannot be recorded"
        )
    attempt = _load_attempt_for(store, args.ticket, args.attempt)
    service = application.coordination_service_for(
        sink, root=str(config.find_project_root()), actor=Actor(attempt.worker_id)
    )
    if attempt.workspace_id != service.workspace.id:
        raise ClaimConflict(
            f"attempt {attempt.id} belongs to workspace {attempt.workspace_id}, but "
            f"this project root is workspace {service.workspace.id}; file mutations "
            "are per workspace and cannot cross a rebind",
            details={
                "attempt_id": attempt.id,
                "attempt_workspace": attempt.workspace_id,
                "workspace_id": service.workspace.id,
            },
        )
    return filemutations.FileMutationService(service), attempt


def _read_payload_bytes(source: str) -> bytes:
    """Read a mutation payload verbatim: `-` is stdin, anything else a file.

    Payload temp files are transport for bytes the proxy is about to record, not
    workspace mutations themselves: they are never claimed, journalled or
    attributed."""
    if source in ("-", None):
        return sys.stdin.buffer.read()
    try:
        with open(source, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise UnsupportedCoordination(
            f"could not read the payload file {source!r}: {error}",
            details={"input": source},
        ) from error


def _read_payload_text(source: str) -> str:
    data = _read_payload_bytes(source)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UnsupportedCoordination(
            f"the payload file {source!r} is not UTF-8 text: {error}",
            details={"input": source},
        ) from error


def _mutation_payload(result) -> dict:
    """The documented JSON payload for a successful (or replayed) mutation.

    The whole-file `version` is the recorded after-digest; `read_token_invalidated`
    states plainly that the token this call used has been consumed and a fresh
    proxy read is required before another mutation. `created_parents` lists any
    in-root directories this call created for a missing destination."""
    first_path = result.paths[0] if result.paths else None
    return {
        "operation_id": result.operation_id,
        "kind": result.kind,
        "paths": list(result.paths),
        "before": dict(result.before),
        "after": dict(result.after),
        "version": result.after.get(first_path) if first_path else None,
        "artifact_refs": list(result.artifact_refs),
        "claim_generation": result.receipt.claim_generation,
        "applied": result.applied,
        "deduplicated": result.deduplicated,
        "recovered": result.recovered,
        "created_parents": list(getattr(result, "created_parents", []) or []),
        "receipt_timestamp": result.receipt.timestamp,
        "read_token_invalidated": bool(result.applied),
        "next_action": "read the file through arbite again before the next mutation",
    }


def _mutation_lines(result, verb: str) -> list:
    lines = []
    for path in result.paths:
        if result.applied:
            lines.append(f"{verb} {path} -> {result.after.get(path)}")
        elif result.deduplicated:
            lines.append(f"already applied {path} (operation {result.operation_id})")
        else:
            lines.append(f"{verb} {path} (no change)")
    if result.applied:
        lines.append(
            "the read token is now invalid; read the file again through arbite "
            "before the next mutation"
        )
    return lines


def cmd_file_write(args):
    """Create or replace a whole file with version-checked, journalled bytes.

    An existing file requires `--read-token` for a fresh post-claim read; an
    absent path is a create authorized by its absent-path claim. The payload is
    read verbatim (`--input -` for stdin), so binary content round-trips."""
    sink = _require_sink(args)
    try:
        mutations, attempt = _file_mutation_context(args, sink)
        content = _read_payload_bytes(args.input)
        result = mutations.write(
            attempt, args.path, content, read_token=args.read_token
        )
    except CoordinationError as error:
        _file_error(args, error)
        return
    _emit_file_result(args, _mutation_payload(result), _mutation_lines(result, "wrote"))


def cmd_file_edit(args):
    """Apply an exact old/new edit batch to an existing file, once, all-or-nothing.

    The batch comes from `--edits` (a JSON payload file, or `-` for stdin) and is
    matched exactly against the file's current bytes; an absent, ambiguous or
    overlapping selection refuses the whole batch with no bytes changed."""
    sink = _require_sink(args)
    try:
        mutations, attempt = _file_mutation_context(args, sink)
        edits = filemutations.parse_edits_json(_read_payload_text(args.edits))
        result = mutations.edit(
            attempt, args.path, edits, read_token=args.read_token
        )
    except CoordinationError as error:
        _file_error(args, error)
        return
    _emit_file_result(args, _mutation_payload(result), _mutation_lines(result, "edited"))


def cmd_file_remove(args):
    """Delete a file through the journal, preserving its bytes as evidence.

    Removal needs a fresh `--read-token` (a read this attempt recorded after it
    claimed the path) and is refused for a directory: arbite v1 has no recursive
    deletion. The removed bytes are stored content-addressed before the unlink, so
    the receipt can reproduce them."""
    sink = _require_sink(args)
    try:
        mutations, attempt = _file_mutation_context(args, sink)
        result = mutations.remove(attempt, args.path, read_token=args.read_token)
    except CoordinationError as error:
        _file_error(args, error)
        return
    _emit_file_result(args, _mutation_payload(result), _mutation_lines(result, "removed"))


def cmd_file_rename(args):
    """Move SOURCE to DEST, owning and recording both paths.

    The source needs a fresh `--read-token`; DEST is claimed as part of this call
    and must be absent, or its current version must be given explicitly with
    `--dest-expected`. A DEST held by another attempt is `file_busy` and changes
    neither path. Missing in-root parent directories of DEST are created safely;
    the moved bytes and (when DEST existed) the bytes it replaced are stored as
    evidence, and both paths appear in the receipt."""
    sink = _require_sink(args)
    try:
        mutations, attempt = _file_mutation_context(args, sink)
        result = mutations.rename(
            attempt,
            args.source,
            args.destination,
            read_token=args.read_token,
            dest_expected=args.dest_expected,
        )
    except CoordinationError as error:
        _file_error(args, error)
        return
    source = result.paths[0] if result.paths else None
    destination = result.paths[1] if len(result.paths) > 1 else None
    payload = _mutation_payload(result)
    payload.update(
        {
            "source": source,
            "destination": destination,
            "destination_version": result.after.get(destination) if destination else None,
        }
    )
    lines = []
    for path in result.paths:
        if result.applied:
            lines.append(f"{path} -> {result.after.get(path)}")
        elif result.deduplicated:
            lines.append(f"already applied {path} (operation {result.operation_id})")
        else:
            lines.append(f"{path} (no change)")
    if result.applied:
        lines.insert(0, f"renamed {source} -> {destination}")
        lines.append(
            "the read token is now invalid; read the file again through arbite "
            "before the next mutation"
        )
    _emit_file_result(args, payload, lines or ["nothing renamed"])


# --- change receipts and net change views (shared-directory coordination, C10) --


def _short_version(value) -> str:
    """A digest (or the absent marker) in a width a human line can hold."""
    if value == coordination.ABSENT:
        return "<absent>"
    text = str(value)
    return text if len(text) <= 23 else text[:20] + "..."


def _change_lines(view) -> list:
    """Human lines for a change view: net changes first, then ordered operations."""
    heading = f"{view.scope} changes for {view.ticket_id}"
    if view.attempt_id:
        heading += f" attempt {view.attempt_id}"
    lines = [heading]
    if not view.net_changes:
        lines.append("no recorded file changes")
    for entry in view.net_changes:
        suffix = " (reverted)" if entry["reverted"] else ""
        lines.append(
            f"{entry['change']}{suffix}: {entry['path']} "
            f"{_short_version(entry['before'])} -> {_short_version(entry['after'])}"
        )
    for entry in view.operations:
        paths = ", ".join(entry["paths"]) or "-"
        lines.append(
            f"op {entry['operation_id']} {entry['operation_kind']} "
            f"{entry['result']}: {paths}"
        )
    if view.unattributed:
        lines.append(
            f"{len(view.unattributed)} observed/unattributed finding(s) "
            "(not attributed to any agent)"
        )
    if view.read_observations:
        lines.append(
            f"{len(view.read_observations)} read observation(s) (separate stream)"
        )
    if view.bounds["operations"]["truncated"]:
        lines.append(
            f"truncated: showing {view.bounds['operations']['returned']} of "
            f"{view.bounds['operations']['total']} operations; "
            f"next --offset {view.bounds['operations']['next_offset']}"
        )
    return lines


def cmd_changes(args):
    """Answer "what changed for this ticket (or attempt), and is it verifiable?".

    Read-only, bounded and one-shot. Mechanical operation receipts (with
    before/after digests and per-artifact verification), the folded net change
    view, agent-authored ticket notes and observed/unattributed drift are returned
    as SEPARATE sections, so a closure never needs an LLM summary to produce a
    file-change manifest. Read observations are a separate stream that only
    appears with --include-reads.
    """
    sink = _require_sink(args)
    try:
        ticket = sink.get(args.id, unique=True)
        store = sink.coordination()
        if store is None:
            raise UnsupportedCoordination(
                f"the {sink.kind} sink does not implement the shared-directory "
                "coordination contract, so change evidence cannot be queried"
            )
        view = changes.ChangesQuery(store, ticket=ticket).view(
            ticket.id,
            attempt_id=args.attempt,
            include_reads=args.include_reads,
            limit=args.limit,
            offset=args.offset,
        )
    except CoordinationError as error:
        _file_error(args, error)
        return
    _emit_file_result(args, view.to_dict(), _change_lines(view))


# --- worker profiles (multi-provider job board, B01) ------------------------


def _worker_store(sink):
    store = sink.coordination()
    if store is None:
        raise UnsupportedCoordination(
            f"the {getattr(sink, 'kind', '?')} sink has no coordination store, so worker "
            "profiles cannot be recorded"
        )
    return store


def _worker_service(args, sink) -> "workers.WorkerProfileService":
    return workers.WorkerProfileService(
        _worker_store(sink), actor=getattr(args, "actor", None) or None
    )


def _worker_lines(view: dict) -> list:
    """Human rendering of one profile view. Availability is labelled declared."""
    labels = " / ".join(view.get(k) or "-" for k in ("provider", "model", "runtime"))
    estimate = view.get("cost_estimate")
    cost = f"class {view['cost_class']}"
    if estimate:
        cost += (
            f", estimate {estimate['amount']} {estimate['unit']} "
            f"(provenance: {estimate['provenance']})"
        )
    capacity = view.get("capacity")
    lines = [
        f"worker {view['worker_id']} [{view['state']}] profile {view['id']} "
        f"revision {view['revision']}",
        f"  tier: {view['tier']} (configured; authoritative for this worker id)",
        f"  provider / model / runtime: {labels} (labels only)",
        f"  capabilities: {', '.join(view['capabilities']) or '(none declared)'}",
        f"  locality: {view['locality']}",
        f"  cost: {cost}",
        f"  capacity: {capacity if capacity is not None else '(undeclared)'} (declared)",
        f"  last checkin: {view.get('last_checkin') or 'never'} (declared by the worker; "
        "not verified liveness)",
        f"  created: {view['created']}  updated: {view['updated']}",
    ]
    if view["state"] == "disabled":
        reason = view.get("disabled_reason") or "no reason recorded"
        lines.append(f"  disabled: {view.get('disabled_at')} ({reason})")
    return lines


def _emit_worker(args, stored, *, extra=None, headline=None) -> None:
    view = stored.view()
    if getattr(args, "json", False):
        payload = {"profile": view, "liveness_notice": workers.LIVENESS_NOTICE}
        payload.update(extra or {})
        _print_json(coordination.ok_result(payload))
        return
    if headline:
        print(headline)
    for line in _worker_lines(view):
        print(line)


def _worker_profile_fields(args) -> dict:
    """The update keyword arguments the given flags name (absent flags omitted)."""
    fields = {}
    if args.tier is not None:
        fields["tier"] = args.tier
    for name in ("provider", "model", "runtime", "locality"):
        value = getattr(args, name, None)
        if value is not None:
            fields[name] = value
    if args.cost_class is not None:
        fields["cost_class"] = args.cost_class
    estimate = workers.build_cost_estimate(args.cost_amount, args.cost_unit, args.cost_provenance)
    if estimate is not None:
        fields["cost_estimate"] = estimate
    if args.capacity is not None:
        fields["capacity"] = workers.parse_capacity(args.capacity)
    return fields


def cmd_worker(args):
    """Worker profiles: optional, passive declarations about a worker id.

    Nothing here launches an agent, calls a provider or checks liveness. A
    registered profile's tier is authoritative at acquisition (`claim`, `list
    next --claim`); an unregistered (ad-hoc) worker id keeps working as before."""
    sink = _require_sink(args)
    try:
        _run_worker_command(args, sink)
    except CoordinationError as error:
        _file_error(args, error)


def _run_worker_command(args, sink) -> None:
    command = args.worker_command
    service = _worker_service(args, sink)

    if command == "register":
        fields = _worker_profile_fields(args)
        fields.pop("tier", None)
        change = service.register(
            args.worker,
            tier=args.tier,
            capabilities=args.capability,
            **fields,
        )
        _emit_worker(args, change.stored, extra={"changes": change.changes},
                     headline=f"registered worker {args.worker}")
        return

    if command == "show":
        _emit_worker(args, service.get(args.worker))
        return

    if command == "list":
        rows = service.list(state=args.state)
        if args.json:
            _print_json(coordination.ok_result({
                "workers": [row.view() for row in rows],
                "count": len(rows),
                "liveness_notice": workers.LIVENESS_NOTICE,
            }))
        elif rows:
            print(f"{'WORKER':<28} {'STATE':<9} {'TIER':<9} {'PROVIDER/MODEL':<30} "
                  "LAST CHECKIN (declared)")
            for row in rows:
                view = row.view()
                labels = "/".join(view.get(k) or "-" for k in ("provider", "model"))
                print(f"{view['worker_id']:<28} {view['state']:<9} {view['tier']:<9} "
                      f"{labels:<30} {view.get('last_checkin') or 'never'}")
        else:
            print("no worker profiles registered (ad-hoc worker ids need none)")
        if not rows:
            sys.exit(EXIT_EMPTY)
        return

    if command == "update":
        fields = _worker_profile_fields(args)
        for name in ("provider", "model", "runtime"):
            if name in fields and fields[name] == "":
                fields[name] = None
        if args.clear_cost_estimate:
            if "cost_estimate" in fields:
                raise TicketError("--clear-cost-estimate conflicts with --cost-amount/--cost-unit")
            fields["cost_estimate"] = None
        if args.capability:
            fields["capabilities"] = args.capability
        change = service.update(
            args.worker,
            expect_revision=args.expect_revision,
            reason=args.reason,
            add_capabilities=args.add_capability,
            remove_capabilities=args.remove_capability,
            **fields,
        )
        headline = (
            f"updated worker {args.worker}: {', '.join(sorted(change.changes))}"
            if change.changed else f"worker {args.worker} unchanged"
        )
        _emit_worker(args, change.stored, extra={"changes": change.changes}, headline=headline)
        return

    if command in ("disable", "enable"):
        operation = service.disable if command == "disable" else service.enable
        change = operation(args.worker, reason=args.reason, expect_revision=args.expect_revision)
        verb = "disabled" if command == "disable" else "enabled"
        headline = (
            f"{verb} worker {args.worker}" if change.changed
            else f"worker {args.worker} was already {verb}"
        )
        _emit_worker(args, change.stored, extra={"changes": change.changes}, headline=headline)
        return

    if command == "checkin":
        stored = service.checkin(args.worker)
        _emit_worker(args, stored, headline=(
            f"recorded declared check-in for {args.worker} at {stored.profile.last_checkin}"
        ))
        return

    if command == "check":
        _worker_check(args, sink, service)
        return

    raise TicketError(f"unknown worker command {command!r}")


def _worker_check(args, sink, service) -> None:
    """Evaluate worker constraints only (no readiness, nothing written)."""
    ticket = sink.get(args.ticket) if args.ticket else None
    explicit = bool(
        args.min_tier or args.require_capability or args.local_only
        or args.max_cost is not None or args.allowed_worker
    )
    if args.max_cost is not None and not args.max_cost_unit:
        raise TicketError("--max-cost needs --max-cost-unit (costs are never unit-less)")
    if explicit:
        requirements = eligibility.Requirements(
            min_tier=args.min_tier or (ticket.tier if ticket is not None
                                       and ticket.tier in eligibility.TIER_RANK else None),
            capabilities=tuple(workers.normalise_capabilities(args.require_capability)),
            local_only=bool(args.local_only),
            max_cost=(
                {"amount": args.max_cost, "unit": args.max_cost_unit}
                if args.max_cost is not None else None
            ),
            allowed_workers=tuple(_split_csv(",".join(args.allowed_worker or []))),
            restricted=not args.unrestricted,
        )
    elif ticket is not None:
        requirements = eligibility.requirements_for_ticket(ticket)
    else:
        requirements = eligibility.Requirements(restricted=not args.unrestricted)
    declaration = service.declaration(args.worker, declared_tier=args.declared_tier)
    result = eligibility.evaluate(requirements, declaration)
    if args.json:
        payload = result.to_dict()
        payload["ticket_id"] = ticket.id if ticket is not None else None
        payload["scope"] = "worker constraints only; readiness is checked at acquisition"
        _print_json(coordination.ok_result(payload))
        return
    verdict = "eligible" if result.eligible else "NOT eligible"
    subject = f" for {ticket.id}" if ticket is not None else ""
    source = "registered profile" if declaration.registered else "ad-hoc (no profile)"
    print(f"worker {args.worker} ({source}) is {verdict}{subject}")
    for reason in result.reasons:
        print(f"  refused [{reason.code}]: {reason.message}")
    for note in result.notes:
        print(f"  note [{note.code}]: {note.message}")
    print("  (worker constraints only; readiness is checked at acquisition)")


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
        "for every id listed in an 'agents:' list in ./arbite.yaml, if present. Which sink is "
        "initialised follows the usual precedence: --sink, then ARBITE_SINK, then a 'sink:' key "
        "in ./arbite.yaml, then file. The store it creates then becomes the project default -- "
        "'sink:' is written to arbite.yaml, created if it does not exist, so no later command "
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
        "'sink:' key in arbite.yaml, then the default (file).",
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
        "as work.",
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
        "field in --json mode, a leading block in text mode -- giving brief instructions "
        "to classify the ticket (title/tier/domain/epic/priority/description) and then "
        "either set status to 'open' (if just triaging) or claim it immediately (if going "
        "to work it now). A wish raw ticket is classified differently: it is reclassified "
        "as 'feature' and filed in the wishlist bucket instead of being opened or claimed. "
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
        "e.g. 'open,in_progress'",
    )
    p_list.add_argument(
        "--tier",
        choices=TIERS,
        help="filter by agent capability tier; with 'next', pass your own tier so you "
        "are only offered work you can actually do (with --claim, a registered worker "
        f"profile's configured tier caps it). {schema.TIER_HELP}",
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
        "comma-separated list matches any of them, e.g. 'open,in_progress'",
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
        help="claim a ticket: assign it and mark it in progress",
        description="Set a ticket's assignee, status and updated together. The claim is a "
        "compare-and-swap: a ticket already assigned to another agent is refused unless "
        "--force, and the write only lands if the ticket is still in the state it was read "
        "in, so two agents racing for the same ticket cannot both end up believing they own "
        "it. (Identity assignment and liveness remain the agent harness's job.)",
    )
    p_claim.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_claim.add_argument("--agent", required=True, help="agent id claiming the ticket, e.g. claude.haiku.001 (required)")
    p_claim.add_argument(
        "--force",
        action="store_true",
        help="administrative takeover: interrupt the ticket's live work attempt and start "
        "a new one for this agent (requires a non-empty --reason). Without it, claiming a "
        "ticket that already has an active attempt is refused",
    )
    p_claim.add_argument(
        "--adopt",
        action="store_true",
        help="adopt a legacy in_progress ticket that has no attempt record (e.g. one "
        "started by a pre-coordination arbite): start its attempt now, recording the "
        "pre-existing declared assignee in the attempt handoff. Never implicit",
    )
    p_claim.add_argument(
        "--reason",
        default="",
        help="why an administrative takeover is being made (required with --force when the "
        "ticket has a live attempt); recorded in the attempt and its events",
    )
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
    p_release.add_argument(
        "--force",
        action="store_true",
        help="revoke another agent's active attempt on this ticket (requires a non-empty "
        "--reason); without it, only the attempt's own worker may release it",
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
    p_block.add_argument(
        "--agent",
        default="system",
        help="agent id blocking it; must be the active attempt's worker, unless --force "
        "revokes that attempt (default: system)",
    )
    p_block.add_argument(
        "--force",
        action="store_true",
        help="revoke another agent's active attempt while blocking (requires --reason)",
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
    p_unblock.add_argument(
        "--force",
        action="store_true",
        help="revoke another agent's active attempt while unblocking (requires --reason)",
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
    p_close.add_argument(
        "--agent",
        default="system",
        help="agent id closing it; must be the active attempt's worker, unless --force "
        "revokes that attempt (default: system)",
    )
    p_close.add_argument(
        "--reason", default="", help="why it's being closed; recorded with the attempt (optional)"
    )
    p_close.add_argument(
        "--force",
        action="store_true",
        help="revoke another agent's active attempt while closing (requires --reason)",
    )
    _sink_flag(p_close)
    p_close.set_defaults(func=cmd_close)

    p_reopen = sub.add_parser(
        "reopen",
        help="reopen a ticket (back to the open status)",
        description="Set a ticket that is not currently open back to open: clear its "
        "closed date and block reason, append an automatic 'Reopened' note, and update "
        "status/updated.",
    )
    p_reopen.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_reopen.add_argument(
        "--agent",
        default="system",
        help="agent id (or 'system') attributed on the automatic reopen note, e.g. "
        "claude.haiku.001 (default: system)",
    )
    p_reopen.add_argument(
        "--reason", default="", help="why it's being reopened; recorded with the attempt (optional)"
    )
    p_reopen.add_argument(
        "--force",
        action="store_true",
        help="revoke another agent's active attempt while reopening (requires --reason)",
    )
    _sink_flag(p_reopen)
    p_reopen.set_defaults(func=cmd_reopen)

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
    p_shelve.add_argument(
        "--agent",
        default="system",
        help="agent id shelving it; must be the active attempt's worker, unless --force "
        "revokes that attempt (default: system)",
    )
    p_shelve.add_argument(
        "--force",
        action="store_true",
        help="revoke another agent's active attempt while shelving (requires --reason)",
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
    p_unshelve.add_argument(
        "--agent",
        default="system",
        help="agent id unshelving it; must be the active attempt's worker, unless --force "
        "revokes that attempt (default: system)",
    )
    p_unshelve.add_argument(
        "--force",
        action="store_true",
        help="revoke another agent's active attempt while unshelving (requires --reason)",
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
        help="file a ticket in a bucket (e.g. /wishlist), or '/' to un-file it",
        description="File a ticket somewhere other than its status location, without changing "
        "any field. <folder> is root-relative: '/wishlist' files it in the wishlist bucket, "
        "'/planning/ideas' in a nested one, and '/' returns it to wherever its status says it "
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
        help="root-relative destination, e.g. '/wishlist', or '/' to return the ticket to "
        "its status location",
    )
    _sink_flag(p_move)
    p_move.set_defaults(func=cmd_move)

    p_set = sub.add_parser(
        "set",
        help="set one or more ticket properties",
        description="Set one or more ticket properties on an existing ticket. Properties "
        "are given as PROPERTY VALUE pairs and any number can be set in one call; quote "
        "any value that spans more than one word. Type-aware: 'tags' and 'depends_on' "
        "are comma-separated lists, 'priority' must be an integer, and an empty quoted "
        "value ('') clears a field. If 'status' is set, the ticket is re-filed to match "
        "(moving to 'closed' auto-dates 'closed'). 'id' is structural and cannot be set.",
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
        "the only copy left; combine with --dry-run to see what it would remove. Prunes "
        "tickets only: coordination history is never deleted",
    )
    p_migrate.add_argument(
        "--coordination",
        dest="coordination",
        action="store_true",
        default=None,
        help="also move shared-directory coordination history (attempts, claims, receipts, "
        "intents, events, artifacts) and rebind this project to the destination; default: "
        "move it iff the source already has coordination state",
    )
    p_migrate.add_argument(
        "--no-coordination",
        dest="coordination",
        action="store_false",
        default=None,
        help="transfer tickets only, leaving all coordination history in the source store",
    )
    _sink_flag(p_migrate)
    p_migrate.set_defaults(func=cmd_migrate)

    p_doctor = sub.add_parser(
        "doctor",
        help="check ticket integrity (drift, cycles, dangling deps, stale indexes)",
        description="Check the invariants nothing else enforces and report what it finds, "
        "then exit 3 if any problem remains. The shared checks cover duplicate ids, invalid "
        "field values, dependency cycles, dangling and self dependencies, in_progress "
        "tickets with no assignee, blocked tickets with no reason, and closed-date "
        "mismatches. The sink adds its own: for the file sink, frontmatter/folder drift (the "
        "folder is authoritative), stray temp files from an interrupted write, and closed "
        "tickets archived under the wrong month; for the SQLite sink, a note index that has "
        "drifted from the ticket body, orphaned index rows, an unexpected schema version and "
        "structural database corruption. --fix repairs only what is unambiguous.",
    )
    p_doctor.add_argument(
        "--fix",
        action="store_true",
        help="repair what can be corrected unambiguously: for the file sink, frontmatter "
        "status is rewritten to match the folder a ticket sits in, tickets loose in the root "
        "are re-filed, and closed tickets are moved into the archive month matching their "
        "close date; for the SQLite sink, a stale note index is rebuilt from the body and "
        "orphaned index rows are removed. Anything needing a judgement call (duplicate ids, "
        "dependency cycles, missing data) is only reported",
    )
    _json_flag(p_doctor)
    _sink_flag(p_doctor)
    p_doctor.set_defaults(func=cmd_doctor)

    p_export = sub.add_parser(
        "export",
        help="export coordination history and/or tickets as JSON (read-only)",
        description="Write a JSON document describing this store -- the shared-directory "
        "coordination history (a versioned, verifiable bundle), the tickets, or both. "
        "Read-only: it never creates the store, the .arbite directory or the coordination "
        "layout, and a store with no coordination state exports a well-formed empty bundle. "
        "With --out the document is written atomically; without it the document is printed "
        "to stdout. --json prints a status payload (counts, cursor namespace, "
        "retained_history, out path) instead of the document.",
    )
    p_export.add_argument(
        "--scope",
        choices=("coordination", "tickets", "all"),
        default="coordination",
        help="what to export: 'coordination' (default) is the shared-directory history "
        "bundle, 'tickets' the ticket store, 'all' both",
    )
    p_export.add_argument(
        "--out",
        metavar="FILE",
        default=None,
        help="write the document to FILE atomically instead of printing it to stdout",
    )
    p_export.add_argument(
        "--workspace",
        metavar="ID",
        default=None,
        help="restrict the coordination export to one workspace id (default: all)",
    )
    p_export.add_argument(
        "--no-artifacts",
        action="store_true",
        help="record artifact metadata but omit the artifact bytes (marked data_omitted)",
    )
    _json_flag(p_export)
    _sink_flag(p_export)
    p_export.set_defaults(func=cmd_export)

    p_rebind = sub.add_parser(
        "rebind",
        help="switch this project's authoritative coordination store",
        description="Explicitly change which store a workspace coordinates against, then "
        "make it the project default. Verifies first, writing nothing until every check "
        "passes: the currently bound store must be quiescent (no active attempt or file "
        "claim), the destination must expose a coordination store, its contract version "
        "must match, and when it is already initialised its coordination doctor must find "
        "no problems. --dry-run runs every check and reports what would change. There is no "
        "daemon, retry or automatic takeover.",
    )
    p_rebind.add_argument(
        "--to",
        dest="to",
        default=None,
        choices=SINK_KINDS,
        metavar="KIND",
        help=f"the sink kind to bind this workspace to (required): {', '.join(SINK_KINDS)}",
    )
    p_rebind.add_argument(
        "--dry-run",
        action="store_true",
        help="run every verification and report what would change, writing nothing",
    )
    _json_flag(p_rebind)
    _sink_flag(p_rebind)
    p_rebind.set_defaults(func=cmd_rebind)

    p_file = sub.add_parser(
        "file",
        help="discover, read, claim, write, edit, remove or rename workspace files",
        description="The shared-directory file surface. list/search are bounded "
        "discovery (deterministic ordering, explicit pagination/truncation markers, "
        "no write authority). read serves a file or line range and records a read "
        "observation; a read takes no lock and never acquires ownership, and a file "
        "held by another attempt is still served with an explicit busy owner and a "
        "non-writable receipt (--fail-if-busy refuses instead). probe inspects an "
        "absent path for a safe create. claim is exclusive whole-file writer "
        "ownership keyed by a canonical workspace path, acquired all-or-nothing; "
        "arbite never waits for or steals a claim, and release revokes one file "
        "token while retaining the work attempt. write replaces a whole file (or "
        "creates one from an absent-path claim) with version-checked, journalled "
        "bytes and requires a fresh --read-token for a replacement; edit applies an "
        "exact old/new batch once, all-or-nothing; remove deletes one file through "
        "the journal with a fresh --read-token (a directory is refused: there is no "
        "recursive deletion in v1); rename moves SOURCE to DEST owning BOTH paths "
        "(the destination must be absent or its version stated with --dest-expected) "
        "and creates missing in-root parent directories safely. See "
        ".arbite/planning/shared-directory-coordination.md.",
    )
    file_sub = p_file.add_subparsers(dest="file_command", required=True)

    p_file_claim = file_sub.add_parser(
        "claim",
        help="claim one or more workspace paths exclusively for a ticket attempt",
        description="Claim every PATH for the given ticket/attempt, all-or-nothing. On "
        "contention this returns file_busy with the holder's ticket and attempt; "
        "arbite never waits for or steals a claim. Re-claiming a path this attempt "
        "already holds is idempotent, and a not-yet-existing path is a valid creation/"
        "rename destination claim whose recorded version is ABSENT.",
    )
    p_file_claim.add_argument(
        "paths", nargs="+", metavar="PATH", help="workspace-relative path(s) to claim"
    )
    p_file_claim.add_argument(
        "--ticket", required=True, metavar="T", help="ticket id the attempt belongs to"
    )
    p_file_claim.add_argument(
        "--attempt", required=True, metavar="A", help="active work attempt id"
    )
    _json_flag(p_file_claim)
    _sink_flag(p_file_claim)
    p_file_claim.set_defaults(func=cmd_file_claim)

    p_file_release = file_sub.add_parser(
        "release",
        help="release this attempt's exclusive claims on one or more paths",
        description="Release the active claims PATH for the given ticket/attempt, "
        "all-or-nothing. The work attempt is retained; only the file token is revoked, "
        "so later re-acquisition mints a new generation and requires a fresh read. A "
        "reason is required and recorded with the release event.",
    )
    p_file_release.add_argument(
        "paths", nargs="+", metavar="PATH", help="workspace-relative path(s) to release"
    )
    p_file_release.add_argument(
        "--ticket", required=True, metavar="T", help="ticket id the attempt belongs to"
    )
    p_file_release.add_argument(
        "--attempt", required=True, metavar="A", help="active work attempt id"
    )
    p_file_release.add_argument(
        "--reason",
        required=True,
        metavar="TEXT",
        help="why the claim is being released (recorded with the release event)",
    )
    _json_flag(p_file_release)
    _sink_flag(p_file_release)
    p_file_release.set_defaults(func=cmd_file_release)

    p_file_list = file_sub.add_parser(
        "list",
        help="list workspace entries under PATH (bounded, deterministic)",
        description="Enumerate entries under PATH (default: the workspace root) in "
        "canonical-path order. Output is bounded (--limit, hard cap 2000) with "
        "--offset paging; protected arbite/.git metadata and symlinks are excluded "
        "and counted, and the result says so in its markers. Listing is read-only: "
        "it authorizes no mutation.",
    )
    p_file_list.add_argument(
        "path",
        nargs="?",
        default=None,
        metavar="PATH",
        help="workspace-relative directory (or file) to enumerate (default: the root)",
    )
    p_file_list.add_argument(
        "--limit", type=int, default=None, metavar="N", help="maximum entries to return"
    )
    p_file_list.add_argument(
        "--offset", type=int, default=None, metavar="N", help="entries to skip (paging)"
    )
    _json_flag(p_file_list)
    _sink_flag(p_file_list)
    p_file_list.set_defaults(func=cmd_file_list)

    p_file_search = file_sub.add_parser(
        "search",
        help="search workspace paths and text lines for PATTERN (bounded)",
        description="Regular-expression text/path discovery under PATH (default: the "
        "workspace root). A file matches when its path matches or when any decoded "
        "line matches; binary, non-UTF-8 and oversized files are reported as skipped "
        "with a reason, never silently omitted. Output is bounded (--limit, hard cap "
        "500) with --offset paging and explicit truncation markers. Search is "
        "discovery only: no import tracing, and no write authority.",
    )
    p_file_search.add_argument("pattern", metavar="PATTERN", help="regular expression to find")
    p_file_search.add_argument(
        "path",
        nargs="?",
        default=None,
        metavar="PATH",
        help="workspace-relative directory to search under (default: the root)",
    )
    p_file_search.add_argument(
        "--limit", type=int, default=None, metavar="N", help="maximum matches to return"
    )
    p_file_search.add_argument(
        "--offset", type=int, default=None, metavar="N", help="matches to skip (paging)"
    )
    _json_flag(p_file_search)
    _sink_flag(p_file_search)
    p_file_search.set_defaults(func=cmd_file_search)

    p_file_read = file_sub.add_parser(
        "read",
        help="serve PATH (or a line range) with a versioned, write-aware receipt",
        description="Read PATH through the canonical pipeline and record a read "
        "observation. The whole-file digest is always recorded, even for --lines. A "
        "read takes no lock and acquires no ownership. Reading a file another attempt "
        "has claimed still returns the bytes, but with an explicit busy owner and a "
        "NON-writable receipt; --fail-if-busy refuses instead so no tokens are spent "
        "on bytes that cannot be written. A write requires a FRESH read after claiming, "
        "by this same attempt: the returned read_token is that evidence, and it is "
        "consumed by the mutation it authorizes. --version-only records the whole-file "
        "version without serving content; it works for binary files, whose token a "
        "whole-file replacement presents.",
    )
    p_file_read.add_argument("path", metavar="PATH", help="workspace-relative file to read")
    p_file_read.add_argument(
        "--ticket", required=True, metavar="T", help="ticket id the attempt belongs to"
    )
    p_file_read.add_argument(
        "--attempt", required=True, metavar="A", help="work attempt id the read is for"
    )
    p_file_read.add_argument(
        "--lines",
        type=_line_range_arg,
        default=None,
        metavar="START:END",
        help="1-based inclusive line range to return (whole-file digest still recorded)",
    )
    p_file_read.add_argument(
        "--fail-if-busy",
        action="store_true",
        help="refuse with file_busy (naming the holder) instead of serving bytes from a "
        "file claimed by another attempt",
    )
    p_file_read.add_argument(
        "--version-only",
        action="store_true",
        help="record the whole-file version and return a read token without serving "
        "content (works for binary files; cannot be combined with --lines)",
    )
    _json_flag(p_file_read)
    _sink_flag(p_file_read)
    p_file_read.set_defaults(func=cmd_file_read)

    p_file_probe = file_sub.add_parser(
        "probe",
        help="inspect PATH for a safe create (absent-path probe)",
        description="Inspect PATH through the canonical pipeline and report whether it "
        "is safe to create. A probe records no observation and acquires no ownership: "
        "a safe create still needs the absent path claimed (observed_version ABSENT) "
        "before a whole-file write.",
    )
    p_file_probe.add_argument("path", metavar="PATH", help="workspace-relative path to probe")
    p_file_probe.add_argument(
        "--ticket", required=True, metavar="T", help="ticket id the attempt belongs to"
    )
    p_file_probe.add_argument(
        "--attempt", required=True, metavar="A", help="work attempt id the probe is for"
    )
    _json_flag(p_file_probe)
    _sink_flag(p_file_probe)
    p_file_probe.set_defaults(func=cmd_file_probe)

    p_file_write = file_sub.add_parser(
        "write",
        help="create or replace PATH with a version-checked whole-file payload",
        description="Replace PATH (or create it from an absent-path claim) with the "
        "bytes in --input (a file, or '-' for stdin). Replacing an existing file "
        "REQUIRES --read-token: a read recorded by this attempt after it claimed "
        "the path. The token's digest, the claim generation and the file's current "
        "digest are all re-checked inside the operation lock, so a stale or "
        "mismatched token changes no bytes and returns stale_read; two writes using "
        "one token cannot both succeed. Creation needs an absent-path claim "
        "(observed_version ABSENT) and takes no read token. Bytes are recorded as "
        "content-addressed before/after evidence, an existing file's supported "
        "permissions are preserved, and a successful write invalidates the old read "
        "token -- read the file again before the next mutation.",
    )
    p_file_write.add_argument("path", metavar="PATH", help="workspace-relative file to write")
    p_file_write.add_argument(
        "--ticket", required=True, metavar="T", help="ticket id the attempt belongs to"
    )
    p_file_write.add_argument(
        "--attempt", required=True, metavar="A", help="active work attempt id"
    )
    p_file_write.add_argument(
        "--read-token",
        default=None,
        metavar="R",
        help="read observation authorizing a replacement (required for an existing "
        "file; omit it to create an absent path)",
    )
    p_file_write.add_argument(
        "--input",
        required=True,
        metavar="CONTENT_FILE",
        help="file holding the whole payload bytes, or '-' to read stdin",
    )
    _json_flag(p_file_write)
    _sink_flag(p_file_write)
    p_file_write.set_defaults(func=cmd_file_write)

    p_file_edit = file_sub.add_parser(
        "edit",
        help="apply an exact old/new edit batch to PATH, all-or-nothing",
        description="Apply the JSON edit batch in --edits (a file, or '-' for stdin) "
        "to the existing PATH with the version-checked --read-token this attempt "
        "recorded after claiming it. Each edit is an object "
        "{'old': str, 'new': str, 'occurrence': 'unique'|'all'|'first'|'last'|'nth', "
        "'index': N}; occurrences are matched EXACTLY (no fuzzy/textual approximation, "
        "no AST edit), 'unique' is the default and requires exactly one match. An "
        "absent, ambiguous or overlapping selection refuses the WHOLE batch before "
        "any byte changes: the batch is applied to the validated in-memory version "
        "and the file is replaced once. Untouched bytes and CRLF/LF newline "
        "conventions are preserved.",
    )
    p_file_edit.add_argument("path", metavar="PATH", help="workspace-relative file to edit")
    p_file_edit.add_argument(
        "--ticket", required=True, metavar="T", help="ticket id the attempt belongs to"
    )
    p_file_edit.add_argument(
        "--attempt", required=True, metavar="A", help="active work attempt id"
    )
    p_file_edit.add_argument(
        "--read-token",
        required=True,
        metavar="R",
        help="read observation authorizing this edit (a fresh post-claim read)",
    )
    p_file_edit.add_argument(
        "--edits",
        required=True,
        metavar="EDITS_FILE",
        help="JSON edit batch (list, or object with an 'edits' list), or '-' for stdin",
    )
    _json_flag(p_file_edit)
    _sink_flag(p_file_edit)
    p_file_edit.set_defaults(func=cmd_file_edit)

    p_file_remove = file_sub.add_parser(
        "remove",
        help="delete PATH through the journal, preserving its bytes as evidence",
        description="Delete PATH with the version-checked --read-token this attempt "
        "recorded after claiming it; the whole-file digest must still match, so a "
        "removal races a concurrent writer the same way a replacement write does. "
        "The deleted bytes are stored content-addressed BEFORE the unlink, so the "
        "receipt preserves what was deleted (binary bytes included). A directory is "
        "refused explicitly: arbite v1 has no recursive deletion. A successful "
        "removal invalidates the read token.",
    )
    p_file_remove.add_argument("path", metavar="PATH", help="workspace-relative file to delete")
    p_file_remove.add_argument(
        "--ticket", required=True, metavar="T", help="ticket id the attempt belongs to"
    )
    p_file_remove.add_argument(
        "--attempt", required=True, metavar="A", help="active work attempt id"
    )
    p_file_remove.add_argument(
        "--read-token",
        required=True,
        metavar="R",
        help="read observation authorizing this removal (a fresh post-claim read)",
    )
    _json_flag(p_file_remove)
    _sink_flag(p_file_remove)
    p_file_remove.set_defaults(func=cmd_file_remove)

    p_file_rename = file_sub.add_parser(
        "rename",
        help="move SOURCE to DEST, recording both paths and version-checked evidence",
        description="Move SOURCE to DEST through the journal. The SOURCE needs the "
        "version-checked --read-token this attempt recorded after claiming it; DEST "
        "is claimed as part of the call (all-or-nothing, so a DEST held by another "
        "attempt is file_busy and NEITHER path is touched) and must be absent, or "
        "its current version must be stated explicitly with --dest-expected to "
        "authorize replacing it. Missing in-root parent directories of DEST are "
        "created safely (never through a symlink, never outside the root). The moved "
        "bytes -- and the bytes an existing DEST had before it was replaced -- are "
        "stored as evidence, the receipt names BOTH paths and both versions, and a "
        "rename interrupted between its two paths is completed by recovery rather "
        "than requiring a shell move.",
    )
    p_file_rename.add_argument(
        "source", metavar="SOURCE", help="workspace-relative file to move"
    )
    p_file_rename.add_argument(
        "destination", metavar="DEST", help="workspace-relative destination path"
    )
    p_file_rename.add_argument(
        "--ticket", required=True, metavar="T", help="ticket id the attempt belongs to"
    )
    p_file_rename.add_argument(
        "--attempt", required=True, metavar="A", help="active work attempt id"
    )
    p_file_rename.add_argument(
        "--read-token",
        required=True,
        metavar="R",
        help="read observation authorizing the move (a fresh post-claim read of SOURCE)",
    )
    p_file_rename.add_argument(
        "--dest-expected",
        default=None,
        metavar="VERSION",
        help="the destination's current whole-file version, required to overwrite an "
        "existing DEST (omit for an absent destination)",
    )
    _json_flag(p_file_rename)
    _sink_flag(p_file_rename)
    p_file_rename.set_defaults(func=cmd_file_rename)

    p_worker = sub.add_parser(
        "worker",
        help="register, show, list, update, disable or check passive worker profiles",
        description="Optional, provider-neutral worker profiles for the job board. A "
        "profile records a worker id's configured tier, capability labels, execution "
        "locality, cost class (with an optional estimate in explicit units), declared "
        "capacity and enabled state. Registration launches nothing, calls no provider "
        "API and verifies nothing: provider/model/runtime are labels, every value is an "
        "operator assertion, and credentials are refused. A registered profile's tier "
        "is authoritative when that worker id acquires work (claim, list next --claim): "
        "a per-call --tier cannot exceed it and a disabled profile cannot take new "
        "work. Unregistered (ad-hoc) worker ids keep working as before. last_checkin is "
        "declared activity, never verified liveness. Disable keeps the profile and its "
        "history; there is no delete. Declared capacity is recorded but not yet "
        "enforced.",
    )
    worker_sub = p_worker.add_subparsers(dest="worker_command", required=True)

    def worker_parser(name, help_text, description):
        parser_ = worker_sub.add_parser(name, help=help_text, description=description)
        parser_.add_argument("worker", metavar="WORKER", help="worker id, e.g. claude.opus-5.002")
        return parser_

    def profile_flags(parser_, *, register):
        parser_.add_argument(
            "--tier", choices=TIERS, required=register,
            help="configured capability tier (authoritative for this worker id)",
        )
        for name, what in (
            ("provider", "provider label, e.g. anthropic, openai, local"),
            ("model", "model label, e.g. claude-opus-5"),
            ("runtime", "runtime/harness label, e.g. claude-code"),
        ):
            parser_.add_argument(
                f"--{name}", default=None, metavar="LABEL",
                help=what + (" (labels only; never used to call a provider)" if register
                             else "; '' clears it"),
            )
        parser_.add_argument(
            "--capability", action="append", default=None, metavar="CAP",
            help="capability/tool label (repeatable or comma-separated); on update it "
            "replaces the whole set",
        )
        parser_.add_argument(
            "--locality", choices=coordination.WORKER_LOCALITIES, default=None,
            help="declared execution locality (default unknown)",
        )
        parser_.add_argument(
            "--cost-class", choices=coordination.COST_CLASSES, default=None,
            help="declared cost class (default unknown)",
        )
        parser_.add_argument("--cost-amount", default=None, metavar="N",
                             help="optional estimated cost amount (needs --cost-unit and "
                             "--cost-provenance)")
        parser_.add_argument("--cost-unit", default=None, metavar="UNIT",
                             help="explicit unit for --cost-amount, e.g. USD/ticket")
        parser_.add_argument("--cost-provenance", default=None, metavar="TEXT",
                             help="where the estimate comes from, e.g. 'operator guess 2026-09'")
        parser_.add_argument("--capacity", default=None, metavar="N",
                             help="declared concurrent-work capacity (integer >= 1"
                             + ("" if register else ", or 'none' to clear") + "); "
                             "recorded, not yet enforced")
        parser_.add_argument("--actor", default=None, metavar="NAME",
                             help="who is making this change (attribution only)")
        _json_flag(parser_)
        _sink_flag(parser_)
        parser_.set_defaults(func=cmd_worker)

    p_w_register = worker_parser(
        "register", "register a new worker profile",
        "Register a profile for WORKER. Refused when WORKER already has one (use update).",
    )
    profile_flags(p_w_register, register=True)

    p_w_update = worker_parser(
        "update", "change a worker profile explicitly",
        "Change the named fields of WORKER's profile; omitted flags are untouched. The "
        "change (including any tier change) is recorded as a worker_updated event with "
        "before/after values. Running attempts are never revoked by a profile change.",
    )
    profile_flags(p_w_update, register=False)
    p_w_update.add_argument("--add-capability", action="append", default=None, metavar="CAP",
                            help="add capability label(s)")
    p_w_update.add_argument("--remove-capability", action="append", default=None,
                            metavar="CAP", help="remove capability label(s)")
    p_w_update.add_argument("--clear-cost-estimate", action="store_true",
                            help="remove the cost estimate")

    for name, help_text, description in (
        ("disable", "disable a worker profile (history is kept)",
         "Stop WORKER from acquiring new work. The profile, its events and every attempt "
         "that references the worker id are kept; running attempts are not revoked."),
        ("enable", "re-enable a disabled worker profile",
         "Allow WORKER to acquire new work again."),
    ):
        parser_ = worker_parser(name, help_text, description)
        parser_.add_argument("--reason", default=None, metavar="TEXT",
                             help="why (recorded with the event)")
        parser_.add_argument("--expect-revision", type=int, default=None, metavar="N",
                             help="refuse unless the profile is at this revision")
        parser_.add_argument("--actor", default=None, metavar="NAME",
                             help="who is making this change (attribution only)")
        _json_flag(parser_)
        _sink_flag(parser_)
        parser_.set_defaults(func=cmd_worker)

    p_w_update.add_argument("--reason", default=None, metavar="TEXT",
                            help="why (recorded with the event)")
    p_w_update.add_argument("--expect-revision", type=int, default=None, metavar="N",
                            help="refuse unless the profile is at this revision")

    for name, help_text, description in (
        ("show", "show one worker profile", "Show WORKER's profile, revision and state."),
        ("checkin", "record a declared check-in (not liveness)",
         "Set WORKER's last_checkin to now. This is the worker's own declaration of "
         "activity; arbite never infers from it that the worker is running, and records no "
         "event for it."),
    ):
        parser_ = worker_parser(name, help_text, description)
        _json_flag(parser_)
        _sink_flag(parser_)
        parser_.set_defaults(func=cmd_worker)

    p_w_list = worker_sub.add_parser(
        "list", help="list worker profiles",
        description="List registered profiles sorted by worker id. Exits 2 when none.",
    )
    p_w_list.add_argument("--state", choices=("all", "enabled", "disabled"), default="all",
                          help="which profiles to list (default all)")
    _json_flag(p_w_list)
    _sink_flag(p_w_list)
    p_w_list.set_defaults(func=cmd_worker)

    p_w_check = worker_parser(
        "check", "evaluate a worker against worker constraints (writes nothing)",
        "Evaluate WORKER (registered or ad-hoc) against a ticket's tier (--ticket) or "
        "explicit constraints, and list every unmet constraint with a stable reason "
        "code. Explicit constraints are restricted by default: an unknown tier, "
        "capability set, locality or cost fails. Readiness (status, dependencies, "
        "active attempts) is not evaluated; acquisition checks it.",
    )
    p_w_check.add_argument("--ticket", default=None, metavar="T",
                           help="use this ticket's tier as the minimum tier")
    p_w_check.add_argument("--min-tier", choices=TIERS, default=None, help="required minimum tier")
    p_w_check.add_argument("--require-capability", action="append", default=None, metavar="CAP",
                           help="required capability label (repeatable or comma-separated)")
    p_w_check.add_argument("--local-only", action="store_true", help="require local execution")
    p_w_check.add_argument("--max-cost", type=float, default=None, metavar="N",
                           help="cost ceiling amount (needs --max-cost-unit)")
    p_w_check.add_argument("--max-cost-unit", default=None, metavar="UNIT",
                           help="unit of --max-cost; never converted")
    p_w_check.add_argument("--allowed-worker", action="append", default=None, metavar="W",
                           help="restrict to these worker ids (repeatable or comma-separated)")
    p_w_check.add_argument("--unrestricted", action="store_true",
                           help="treat unknown worker values as advisory notes instead of failures")
    p_w_check.add_argument("--declared-tier", choices=TIERS, default=None,
                           help="a per-call tier declaration (may not exceed a registered profile)")
    _json_flag(p_w_check)
    _sink_flag(p_w_check)
    p_w_check.set_defaults(func=cmd_worker)

    p_changes = sub.add_parser(
        "changes",
        help="show a ticket's (or attempt's) recorded change evidence and net changes",
        description="Read-only, bounded change evidence for T (or for one --attempt). "
        "Returns MECHANICAL evidence (the ordered file-change operations: write, "
        "edit, remove, rename -- each with before/after "
        "digests and artifact references, each artifact marked verifiable only after "
        "its bytes are read back and hashed) separately from agent-authored ticket "
        "notes (prose, never used to derive the net change) and from external "
        "observed/unattributed drift (never assigned to the current agent). The net "
        "change fold compares the first before-version to the latest after-version per "
        "path -- so an edit-then-revert reports 'reverted' while every operation stays "
        "in the ordered history. Output is bounded (--limit, hard cap 2000) with "
        "--offset paging and an explicit truncation marker. Read observations are a "
        "separate stream and appear only with --include-reads. No LLM summary is "
        "required: the mechanical view alone is a file-change manifest.",
    )
    p_changes.add_argument("id", metavar="T", help="ticket id to report on")
    p_changes.add_argument(
        "--attempt",
        default=None,
        metavar="A",
        help="report one work attempt for the ticket instead of the whole ticket",
    )
    p_changes.add_argument(
        "--include-reads",
        action="store_true",
        help="also return the separate read-observation stream (bytes served)",
    )
    p_changes.add_argument(
        "--limit", type=int, default=None, metavar="N", help="maximum operations to return"
    )
    p_changes.add_argument(
        "--offset", type=int, default=None, metavar="N", help="operations to skip (paging)"
    )
    _json_flag(p_changes)
    _sink_flag(p_changes)
    p_changes.set_defaults(func=cmd_changes)

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
        print(f"error: {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    main()
