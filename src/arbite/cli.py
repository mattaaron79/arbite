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

from . import __version__, config, docs, graph, schema
from .errors import ArbiteError, Conflict, TicketError
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
    closed it -- the write is refused with a Conflict instead of silently
    discarding that change. Commands that edit prose without changing state
    (`note`, `set` of a non-status field) still use it; only `move_to_bucket`
    does not, because filing is not a state change."""
    return Expect(status=ticket.status, assignee=ticket.assignee)


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

    # Walk candidates in order, claiming until we have `wanted` of them: if
    # another agent wins the race for one, move on to the next rather than
    # failing the whole dispatch. Each claim is individually atomic, so a
    # partial batch is a correct result, not a broken one.
    claimed = []
    errors = []
    for candidate in candidates:
        if len(claimed) == wanted:
            break
        # The expectation is taken from the ticket as read, *before* the mutation
        # below: taken afterwards it would describe the new state rather than the
        # state the write has to replace.
        expect = _expect_from(candidate)
        candidate.status = "in_progress"
        candidate.assignee = args.claim
        candidate.updated = schema.now()
        try:
            sink.update(candidate, expect=expect)
        except Conflict as e:
            # Lost the race for this one. The next candidate is untouched, so the
            # loop simply moves on to it.
            errors.append(str(e))
            continue
        claimed.append(candidate)

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
    """Claim a ticket for an agent. Claiming is a compare-and-swap, not a
    blind write: a ticket already assigned to somebody else is refused unless
    --force, and the write carries the expectation that the ticket is still in
    the state it was read in, so two agents racing for the same ticket can't
    both come away believing they own it."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    previous = t.assignee
    if previous and previous != args.agent and not args.force:
        raise TicketError(
            f"ticket {t.id} is already assigned to {previous} (status: {t.status}); "
            "pass --force to take it over"
        )
    if previous and previous != args.agent:
        schema.append_note(t, args.agent, f"Claim taken over from {previous} (--force).")
    expect = _expect_from(t)
    t.status = "in_progress"
    t.assignee = args.agent
    t.updated = schema.now()
    sink.update(t, expect=expect)
    print(f"claimed {t.id} for {args.agent} -> {sink.location(t.id)}")


def cmd_release(args):
    """Return a claimed ticket to open/ and clear its assignee.

    The counterpart to claim: an agent that stops work part-way (out of scope, out of
    context, wrong capability tier) needs one command that unassigns and
    reopens together, so the ticket becomes visible to `list next` again rather
    than sitting in_progress owned by nobody who is still working it."""
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    if t.status == "open" and t.assignee is None:
        raise TicketError(f"ticket {t.id} is already open and unassigned")
    previous = t.assignee
    expect = _expect_from(t)
    message = "Released." if not args.reason else f"Released: {args.reason}"
    schema.append_note(t, args.agent, message)
    t.assignee = None
    t.blocked_by = None
    t.status = "open"
    t.updated = schema.now()
    sink.update(t, expect=expect)
    owner = f" (was {previous})" if previous else ""
    print(f"released {t.id}{owner} -> {sink.location(t.id)}")


def cmd_block(args):
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    expect = _expect_from(t)
    t.blocked_by = args.reason
    t.status = "blocked"
    t.updated = schema.now()
    sink.update(t, expect=expect)
    print(f"blocked {t.id} ({args.reason}) -> {sink.location(t.id)}")


def cmd_unblock(args):
    """Clear a block and move the ticket back into play.

    The symmetric counterpart to `block`. Doing this with `set status` leaves
    blocked_by populated, so the ticket claims to be stalled by something in
    every listing while sitting in open -- exactly the frontmatter drift the
    folder-is-truth rule exists to prevent."""
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
    print(f"unblocked {t.id}{was} -> {sink.location(t.id)}")


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
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    if t.status == "open":
        raise TicketError(f"ticket {t.id} is already open")
    expect = _expect_from(t)
    t.closed = None
    t.blocked_by = None
    t.status = "open"
    t.updated = schema.now()
    schema.append_note(t, args.agent, "Reopened.")
    sink.update(t, expect=expect)
    print(f"reopened {t.id} -> {sink.location(t.id)}")


def cmd_shelve(args):
    sink = _require_sink(args)
    t = sink.get(args.id, unique=True)
    expect = _expect_from(t)
    t.status = "shelved"
    t.updated = schema.now()
    message = "Shelved." if not args.reason else f"Shelved: {args.reason}"
    schema.append_note(t, "system", message)
    sink.update(t, expect=expect)
    print(f"shelved {t.id} -> {sink.location(t.id)}")


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
        help="take over a ticket already assigned to another agent (records the takeover "
        "as a note); without this, claiming someone else's ticket is an error",
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
        "the only copy left; combine with --dry-run to see what it would remove",
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
