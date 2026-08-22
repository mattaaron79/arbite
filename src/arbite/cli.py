"""arbite CLI: argument parsing and command dispatch."""

from __future__ import annotations

import argparse
import heapq
import re
import sys
from pathlib import Path

from . import __version__, config, docs, ticket as ticket_mod
from .ticket import STATUSES, Ticket, TicketError


def _split_csv(value):
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


# Shared help for commands that take a single ticket id: searches are wildcard
# (substring) matches, so 'tic-' can be skipped entirely and e.g. 'f6' resolves
# to tic-f607; if several tickets match, the first alphabetically is used.
TICKET_ID_HELP = (
    "ticket id or any wildcard (substring) match, e.g. 'f6' or 'tic-f607' both "
    "resolve to tic-f607; if several tickets match, the first alphabetically is used"
)


def _require_tickets_root() -> Path:
    tickets_root = config.find_tickets_dir()
    if tickets_root is None:
        print("error: no tickets/ directory found (run 'arbite init' first)", file=sys.stderr)
        sys.exit(1)
    return tickets_root


def cmd_init(args):
    project_root = Path.cwd()
    tickets_root = project_root / "tickets"
    for status in ticket_mod.FLAT_STATUS_DIRS:
        (tickets_root / status).mkdir(parents=True, exist_ok=True)
    (tickets_root / "closed").mkdir(parents=True, exist_ok=True)
    agents_dir = tickets_root / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    print(f"tickets/ ready at {tickets_root}")

    agent_ids = config.load_known_agent_ids(project_root)
    if not agent_ids:
        print("no agents configured (add an 'agents:' list to arbite.yaml to pre-create scratchpads)")
    else:
        for agent_id in agent_ids:
            scratchpad = agents_dir / f"{agent_id}.md"
            if scratchpad.exists():
                continue
            scratchpad.write_text(f"# {agent_id}\n\nNo ticket claimed yet.\n", encoding="utf-8")
            print(f"created scratchpad for {agent_id}")

    parser, subparsers_by_name = build_parser()
    arbite_md = project_root / "ARBITE.md"
    arbite_md.write_text(docs.render(parser, subparsers_by_name), encoding="utf-8")
    print(f"ARBITE.md refreshed at {arbite_md} (point CLAUDE.md or similar at it)")


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

    tickets_root = _require_tickets_root()
    existing_ids = {t.id for _, t in ticket_mod.load_all_tickets(tickets_root)}
    new_id = ticket_mod.gen_id(existing_ids)
    today = ticket_mod.today()

    description = args.description or (ticket_mod.BLANK_DESCRIPTION if args.blank else "")
    body = ticket_mod.DEFAULT_BODY.format(description=description)
    if args.blank:
        body = f"{ticket_mod.BLANK_WARNING}\n\n{body}"

    new_ticket = Ticket(
        id=new_id,
        title=args.title or ticket_mod.BLANK_TITLE,
        status="open",
        type=args.type or ticket_mod.BLANK_TYPE,
        tier=args.tier or ticket_mod.BLANK_TIER,
        domain=args.domain or ticket_mod.BLANK_DOMAIN,
        epic=args.epic,
        priority=args.priority,
        tags=_split_csv(args.tags),
        assignee=None,
        depends_on=_split_csv(args.depends_on),
        blocked_by=None,
        created=today,
        updated=today,
        closed=None,
        body=body,
    )

    dest = ticket_mod.status_dir("open", tickets_root) / f"{new_id}.md"
    ticket_mod.save_ticket(new_ticket, dest)
    if args.blank:
        print(f"created blank template {new_id} at {dest} -- fill in the TODOs and save before it's claimed")
    else:
        print(f"created {new_id} at {dest}")


def cmd_raw(args):
    """Create a deliberately unclassified 'raw' ticket in tickets/open/ from a
    brief request. Only the type and a placeholder title are set; the body
    explains what still needs to be filled in (title, tier, domain, epic,
    priority, and an expanded description) before the ticket can be claimed or
    worked. The ticket is auto-grouped under the 'classification' epic so
    triage/classification jobs can discover it."""
    tickets_root = _require_tickets_root()
    existing_ids = {t.id for _, t in ticket_mod.load_all_tickets(tickets_root)}
    new_id = ticket_mod.gen_id(existing_ids)
    today = ticket_mod.today()
    message = " ".join(args.message)

    description = ticket_mod.RAW_DESCRIPTION.format(message=message)
    if args.type == "memo":
        description = f"{description}\n\n{ticket_mod.MEMO_RAW_NOTE}"

    body = ticket_mod.DEFAULT_BODY.format(description=description)

    new_ticket = Ticket(
        id=new_id,
        title=ticket_mod.RAW_TITLE_FORMAT.format(type=args.type),
        status="open",
        type=args.type,
        tier=ticket_mod.BLANK_TIER,
        domain=ticket_mod.BLANK_DOMAIN,
        epic=ticket_mod.CLASSIFICATION_EPIC,
        priority=None,
        tags=[],
        assignee=None,
        depends_on=[],
        blocked_by=None,
        created=today,
        updated=today,
        closed=None,
        body=body,
    )

    dest = ticket_mod.status_dir("open", tickets_root) / f"{new_id}.md"
    ticket_mod.save_ticket(new_ticket, dest)
    print(
        f"created raw {args.type} ticket {new_id} at {dest} -- classify it "
        "(title/tier/domain/epic/priority/description) before it can be worked; "
        f"it is grouped under the '{ticket_mod.CLASSIFICATION_EPIC}' epic until then"
    )


def _matches_field_filters(args, t):
    """True if t passes the --status/--tier/--domain/--epic/--priority/--assignee filters."""
    if args.status and t.status != args.status:
        return False
    if args.tier and t.tier != args.tier:
        return False
    if args.domain and t.domain != args.domain:
        return False
    if args.epic and t.epic != args.epic:
        return False
    if args.assignee and t.assignee != args.assignee:
        return False
    if args.priority is not None and t.priority != args.priority:
        return False
    return True


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


def _dependency_closure(by_id, root_ids):
    """All ticket ids reachable from root_ids by following depends_on (roots included)."""
    scope = set()
    stack = list(root_ids)
    while stack:
        tid = stack.pop()
        if tid in scope:
            continue
        scope.add(tid)
        t = by_id.get(tid)
        if t is None:
            continue
        stack.extend(t.depends_on)
    return scope


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


def _topo_order(scope):
    """Kahn's algorithm over scope: dependencies are emitted before the tickets that
    depend on them; when several tickets are ready at once (siblings), the most urgent
    (lowest priority number) is emitted first, then id for a stable tie-break."""
    indegree = {tid: len([d for d in t.depends_on if d in scope]) for tid, t in scope.items()}
    dependents = {tid: [] for tid in scope}
    for tid, t in scope.items():
        for d in t.depends_on:
            if d in scope:
                dependents[d].append(tid)
    heap = []
    for tid, t in scope.items():
        if indegree[tid] == 0:
            heapq.heappush(heap, (t.priority_sort_key(), tid))
    order = []
    while heap:
        _, tid = heapq.heappop(heap)
        order.append(tid)
        for parent in dependents[tid]:
            indegree[parent] -= 1
            if indegree[parent] == 0:
                heapq.heappush(heap, (scope[parent].priority_sort_key(), parent))
    # Any node never emitted (e.g. a depends_on cycle) is appended in priority order.
    emitted = set(order)
    leftover = sorted(
        (tid for tid in scope if tid not in emitted),
        key=lambda tid: (scope[tid].priority_sort_key(), tid),
    )
    order.extend(leftover)
    return order


def _print_topo(scope):
    """Print scope as a vertical list in topological dependency order."""
    rows = [scope[tid] for tid in _topo_order(scope)]
    if not rows:
        print("no tickets found")
        return
    _print_flat(rows)


def _cmd_list_next(args, all_tickets):
    """Print a single list entry: the next open ticket in topological dependency
    order (dependencies come first; when several are ready at once, the most
    urgent -- lowest priority number -- comes first). --tier/--epic narrow the
    candidates, and anything that isn't `open` is never considered."""
    scope = {
        t.id: t
        for t in all_tickets
        if t.status == "open"
        and (not args.tier or t.tier == args.tier)
        and (not args.epic or t.epic == args.epic)
    }
    if not scope:
        print("no tickets found")
        return
    nxt = scope[_topo_order(scope)[0]]
    _print_flat([nxt])


def _resolve_tic_terms(all_tickets, terms):
    """Expand each --tic term into every ticket id it matches by wildcard
    (substring) search, e.g. 'f6' resolves to tic-f607. Each term must match
    at least one ticket (TicketError otherwise). Returns a sorted set of ids."""
    by_id = {t.id: t for t in all_tickets}
    resolved = []
    for term in terms:
        term_lower = term.lower()
        matches = sorted(tid for tid in by_id if term_lower in tid.lower())
        if not matches:
            raise TicketError(f"no ticket found matching '{term}'")
        resolved.extend(matches)
    return sorted(set(resolved))


def cmd_list(args):
    tickets_root = _require_tickets_root()
    all_tickets = [t for _, t in ticket_mod.load_all_tickets(tickets_root)]
    by_id = {t.id: t for t in all_tickets}
    tic_ids = set(_resolve_tic_terms(all_tickets, _split_csv(args.tic)))

    if args.subcommand == "next":
        _cmd_list_next(args, all_tickets)
        return

    if args.tree or args.topo:
        if tic_ids:
            # --tic roots the tree/topo at those tickets and pulls in every
            # transitive dependency beneath them (other field filters are ignored).
            scope = {
                tid: by_id[tid]
                for tid in _dependency_closure(by_id, tic_ids)
                if tid in by_id
            }
        else:
            scope = {t.id: t for t in all_tickets if _matches_field_filters(args, t)}
            if not scope:
                print("no tickets found")
                return
        if args.topo:
            _print_topo(scope)
            return
        if tic_ids:
            roots = [tid for tid in tic_ids if tid in by_id]
        else:
            # Forest roots are the tickets nothing else depends on.
            roots = [
                tid
                for tid in scope
                if not any(tid in other.depends_on for other in scope.values())
            ]
            if not roots:
                roots = list(scope)
        _print_tree(scope, roots)
        return

    # Flat list: field filters plus the --tic id filter.
    rows = [
        t
        for t in all_tickets
        if _matches_field_filters(args, t) and (not tic_ids or t.id in tic_ids)
    ]
    if not rows:
        print("no tickets found")
        return

    # Within each status, more urgent (lower priority number) tickets come first;
    # tickets without a priority set sort last so they don't jump the queue.
    rows.sort(key=lambda t: (t.status, t.priority_sort_key(), t.id))
    _print_flat(rows)


def cmd_claim(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id)
    t.assignee = args.agent
    t.updated = ticket_mod.today()
    new_path = ticket_mod.move_ticket(path, t, tickets_root, "in_progress")
    print(f"claimed {t.id} for {args.agent} -> {new_path}")


def cmd_block(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id)
    t.blocked_by = args.reason
    t.updated = ticket_mod.today()
    new_path = ticket_mod.move_ticket(path, t, tickets_root, "blocked")
    print(f"blocked {t.id} ({args.reason}) -> {new_path}")


def cmd_close(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id)
    t.closed = ticket_mod.today()
    t.updated = t.closed
    new_path = ticket_mod.move_ticket(path, t, tickets_root, "closed")
    print(f"closed {t.id} -> {new_path}")


def cmd_reopen(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id)
    if t.status == "open":
        raise TicketError(f"ticket {t.id} is already open")
    t.closed = None
    t.blocked_by = None
    t.updated = ticket_mod.today()
    ticket_mod.append_note(t, args.agent, "Reopened.")
    new_path = ticket_mod.move_ticket(path, t, tickets_root, "open")
    print(f"reopened {t.id} -> {new_path}")


def cmd_shelve(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id)
    t.updated = ticket_mod.today()
    message = "Shelved."
    if args.reason:
        message = f"Shelved: {args.reason}"
    ticket_mod.append_note(t, "system", message)
    new_path = ticket_mod.move_ticket(path, t, tickets_root, "shelved")
    print(f"shelved {t.id} -> {new_path}")


def cmd_note(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id)
    message = " ".join(args.message)
    ticket_mod.append_note(t, args.agent, message)
    t.updated = ticket_mod.today()
    ticket_mod.save_ticket(t, path)
    print(f"added note to {t.id} by {args.agent}")


def cmd_show(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id)
    print(path.read_text(encoding="utf-8"))


def cmd_deps(args):
    tickets_root = _require_tickets_root()
    _, start = ticket_mod.find_ticket(tickets_root, args.id)
    by_id = {t.id: t for _, t in ticket_mod.load_all_tickets(tickets_root)}

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
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id)
    if args.dep is None:
        t.depends_on = []
        t.updated = ticket_mod.today()
        ticket_mod.save_ticket(t, path)
        print(f"cleared dependencies of {t.id}")
        return
    _, dep = ticket_mod.find_ticket(tickets_root, args.dep)
    if dep.id == t.id:
        raise TicketError(f"ticket {t.id} cannot depend on itself")
    if dep.id not in t.depends_on:
        t.depends_on.append(dep.id)
        t.updated = ticket_mod.today()
        ticket_mod.save_ticket(t, path)
        print(f"{t.id} now depends on {dep.id}")
    else:
        print(f"{t.id} already depends on {dep.id}")


# Fields `arbite set` accepts: every frontmatter field except the structural id
# (the id is the filename, generated by `arbite create` and never renamed by
# hand), plus 'body' for the freeform markdown body.
SETTABLE_PROPERTIES = (set(ticket_mod.FIELD_ORDER) | {"body"}) - {"id"}

# Optional text fields: an empty quoted value clears them back to None.
_CLEARABLE_TEXT_FIELDS = {"epic", "assignee", "blocked_by", "closed"}


def _coerce_set_value(prop: str, value: str):
    """Convert a CLI string into the typed value a ticket property expects:
    lists (tags/depends_on) are comma-split, priority is parsed as an int, and an
    empty quoted value clears optional/list/int fields."""
    if prop in ("tags", "depends_on"):
        return _split_csv(value)
    if prop == "priority":
        return None if value == "" else int(value)
    if value == "" and prop in _CLEARABLE_TEXT_FIELDS:
        return None
    return value


def cmd_set(args):
    """Set one or more ticket properties on an existing ticket. Properties come in
    PROPERTY VALUE pairs (any number per call); quote any value that spans more
    than one word. Type-aware: 'tags'/'depends_on' are comma-separated lists,
    'priority' must be an integer, and an empty quoted value ('') clears a field.
    A 'status' change also moves the ticket file so folder and frontmatter stay in
    sync (and auto-dates 'closed' when a ticket is set to closed)."""
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id)
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
        if prop not in SETTABLE_PROPERTIES:
            raise TicketError(
                f"unknown ticket property '{prop}' "
                f"(valid: {', '.join(sorted(SETTABLE_PROPERTIES))})"
            )
        if prop == "status" and value not in STATUSES:
            raise TicketError(f"invalid status '{value}' (valid: {', '.join(STATUSES)})")
        if prop == "priority" and value != "":
            try:
                int(value)
            except ValueError:
                raise TicketError(f"priority must be an integer, got '{value}'")

    original_status = t.status
    new_status = None
    updated_given = False
    for prop, value in pairs:
        if prop == "status":
            new_status = value
        if prop == "updated":
            updated_given = True
        setattr(t, prop, _coerce_set_value(prop, value))

    if not updated_given:
        t.updated = ticket_mod.today()

    if new_status is not None and new_status != original_status:
        # A real status change also moves the file (mirrors claim/close/etc.);
        # moving to closed auto-dates 'closed' like `arbite close` does.
        if new_status == "closed" and t.closed is None:
            t.closed = t.updated
        new_path = ticket_mod.move_ticket(path, t, tickets_root, new_status)
        print(f"set {', '.join(prop for prop, _ in pairs)} on {t.id} -> {new_path}")
    else:
        ticket_mod.save_ticket(t, path)
        print(f"set {', '.join(prop for prop, _ in pairs)} on {t.id} at {path}")


def _ticket_field_value(t: Ticket, name: str) -> str:
    """String form of a ticket field for searching; 'body' is the markdown body."""
    if name == "body":
        return t.body or ""
    value = getattr(t, name, None)
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _compile_matcher(pattern: str, use_regex: bool, use_wildcard: bool, ignore_case: bool = True):
    """Build a text->bool matcher from the search pattern and mode flags. Default is a
    case-insensitive substring match; -w treats '*' as 'any' (simple globbing); -r uses
    the pattern as a regular expression (invalid patterns raise TicketError)."""
    flags = re.IGNORECASE if ignore_case else 0
    if use_regex:
        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            raise TicketError(f"invalid regex '{pattern}': {e}")
        return lambda text: rx.search(text) is not None
    if use_wildcard:
        # Translate simple globs: everything is literal except '*' = any text (incl. empty).
        rx = re.compile(re.escape(pattern).replace(r"\*", ".*"), flags)
        return lambda text: rx.search(text) is not None
    needle = pattern.lower()
    return lambda text: needle in text.lower()


# Fields `arbite search --params` accepts: every frontmatter field plus the body.
SEARCH_PARAMS = set(ticket_mod.FIELD_ORDER) | {"body"}


def cmd_search(args):
    """Search every ticket for the given text, optionally restricted to specific
    fields with --params (comma-separated; 'body' = the rest of the ticket, 'all' =
    every field plus the body, the default). Matching is a case-insensitive substring
    by default; -w adds simple wildcards ('*' = any text) and -r treats the text as a
    regular expression."""
    tickets_root = _require_tickets_root()
    params = _split_csv(args.params) or ["all"]
    if "all" in params:
        params = sorted(SEARCH_PARAMS)
    unknown = [p for p in params if p not in SEARCH_PARAMS]
    if unknown:
        raise TicketError(
            f"unknown ticket field(s) to search: {', '.join(unknown)} "
            f"(valid: all, body, {', '.join(ticket_mod.FIELD_ORDER)})"
        )
    matcher = _compile_matcher(" ".join(args.search_text), args.regex, args.wildcard)
    rows = [
        t
        for _, t in ticket_mod.load_all_tickets(tickets_root)
        if (not args.status or t.status == args.status)
        and any(matcher(_ticket_field_value(t, p)) for p in params)
    ]
    if not rows:
        print("no tickets found")
        return
    rows.sort(key=lambda t: (t.status, t.priority_sort_key(), t.id))
    _print_flat(rows)


def build_parser():
    """Returns (parser, subparsers_by_name). The dict is used by `arbite init`
    to render ARBITE.md's command reference straight from argparse's own
    --help output, so that doc can't drift from the real CLI."""
    parser = argparse.ArgumentParser(prog="arbite", description="File-based ticketing system")
    parser.add_argument("--version", action="version", version=f"arbite {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p_init = sub.add_parser(
        "init",
        help="create tickets/ folder structure and agent scratchpads",
        description="Create tickets/{open,in_progress,blocked,closed,agents}/ in the current "
        "directory (like 'git init'), and pre-create a scratchpad file under tickets/agents/ "
        "for every id listed in an 'agents:' list in ./arbite.yaml, if present.",
    )
    p_init.set_defaults(func=cmd_init)

    p_create = sub.add_parser(
        "create",
        help="create a new ticket in open/",
        description="Create a new ticket in tickets/open/ with a generated id (tic-XXXX). "
        "--title/--type/--tier/--domain are required unless --blank is given.",
    )
    p_create.add_argument("--title", help="short ticket title (required unless --blank)")
    p_create.add_argument(
        "--type",
        choices=["bug", "feature", "refactor", "chore"],
        help="kind of work (required unless --blank)",
    )
    p_create.add_argument(
        "--tier",
        choices=["low", "medium", "high"],
        help="capability level required to work this ticket (required unless --blank)",
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
    p_create.set_defaults(func=cmd_create)

    p_raw = sub.add_parser(
        "raw",
        help="create an unclassified raw ticket in open/ from a brief request",
        description="Capture a brief request as a raw ticket in tickets/open/. Sets the type "
        "and title to '<type> (raw): Requires Classification', leaves tier/domain/priority as "
        "TODO placeholders, auto-groups the ticket under the 'classification' epic (so "
        "triage/classification jobs can find it with `arbite list next --epic classification`), "
        "and writes a body explaining that the ticket must be filled out (a real title, tier, "
        "domain, epic, priority, and an expanded description) before it can be claimed or "
        "worked. Use 'memo' when the request is to update project notes / documentation "
        "rather than make a code change.",
    )
    p_raw.add_argument(
        "type",
        choices=ticket_mod.RAW_TYPE_CHOICES,
        metavar="TYPE",
        help="kind of raw ticket: memo | feature | bug",
    )
    p_raw.add_argument(
        "message",
        metavar="MESSAGE",
        nargs="+",
        help="brief request to capture, e.g. \"users can't save without auth\" "
        "(joined with spaces if multiple words)",
    )
    p_raw.set_defaults(func=cmd_raw)

    p_list = sub.add_parser(
        "list", help="list/filter tickets", description="List tickets, optionally filtered by one or more fields. "
        "Sorted so that within a status, more urgent tickets (lower priority number) come first."
    )
    p_list.add_argument("--status", choices=STATUSES, help="filter by status")
    p_list.add_argument("--tier", choices=["low", "medium", "high"], help="filter by tier")
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
        choices=["next"],
        metavar="SUBCOMMAND",
        help="'next' prints a single list entry for the next open ticket in topological "
        "dependency order (most urgent first); combine with --tier/--epic to restrict "
        "to a capability tier or an epic",
    )
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
        choices=STATUSES,
        help="only search tickets with this status (default: all statuses)",
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
    p_search.set_defaults(func=cmd_search)

    p_claim = sub.add_parser(
        "claim",
        help="move a ticket to in_progress/ and assign it",
        description="Move a ticket to tickets/in_progress/, set its assignee, and update status/updated.",
    )
    p_claim.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_claim.add_argument("--agent", required=True, help="agent id claiming the ticket, e.g. claude.haiku.001 (required)")
    p_claim.set_defaults(func=cmd_claim)

    p_block = sub.add_parser(
        "block",
        help="move a ticket to blocked/",
        description="Move a ticket to tickets/blocked/, set blocked_by, and update status/updated.",
    )
    p_block.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_block.add_argument(
        "--reason", required=True, help="why it's stalled: freeform text or another ticket id (required)"
    )
    p_block.set_defaults(func=cmd_block)

    p_close = sub.add_parser(
        "close",
        help="move a ticket to closed/YYYY-MM/",
        description="Move a ticket to tickets/closed/YYYY-MM/ (by today's date) and set status/closed/updated.",
    )
    p_close.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_close.set_defaults(func=cmd_close)

    p_reopen = sub.add_parser(
        "reopen",
        help="move a ticket back to open/ (reopen it)",
        description="Move a ticket that is not currently open back to tickets/open/: clear its "
        "closed date and block reason, append an automatic 'Reopened' note, and update status/updated.",
    )
    p_reopen.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_reopen.add_argument(
        "--agent",
        default="system",
        help="agent id (or 'system') attributed on the automatic reopen note, e.g. "
        "claude.haiku.001 (default: system)",
    )
    p_reopen.set_defaults(func=cmd_reopen)

    p_shelve = sub.add_parser(
        "shelve",
        help="move a ticket to shelved/ (shelve it)",
        description="Move a ticket to tickets/shelved/, set status/updated, and append an automatic "
        "timestamped note recording that it was shelved (including --reason if given).",
    )
    p_shelve.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_shelve.add_argument(
        "--reason",
        default="",
        help="why it's being shelved; included in the automatic note (optional)",
    )
    p_shelve.set_defaults(func=cmd_shelve)

    p_note = sub.add_parser(
        "note",
        help="append a timestamped, agent-identified note to a ticket",
        description="Append a timestamped, agent-identified entry to a ticket's '## Notes' "
        "section (blank line between entries) and update 'updated'. Agents should prefer "
        "this over directly editing a ticket file to leave progress notes.",
    )
    p_note.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_note.add_argument("agent", metavar="AGENT_ID", help="agent id leaving the note, e.g. claude.haiku.001")
    p_note.add_argument("message", metavar="MESSAGE", nargs="+", help="note text (joined with spaces if multiple words)")
    p_note.set_defaults(func=cmd_note)

    p_show = sub.add_parser(
        "show", help="print a ticket's full contents", description="Print a ticket's raw markdown file (frontmatter + body)."
    )
    p_show.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_show.set_defaults(func=cmd_show)

    p_deps = sub.add_parser(
        "deps",
        help="walk depends_on to show a dependency tree",
        description="Recursively walk a ticket's depends_on field and print the dependency tree.",
    )
    p_deps.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
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
    p_depend.set_defaults(func=cmd_depend)

    p_set = sub.add_parser(
        "set",
        help="set one or more ticket properties",
        description="Set one or more ticket properties on an existing ticket. Properties "
        "are given as PROPERTY VALUE pairs and any number can be set in one call; quote "
        "any value that spans more than one word. Type-aware: 'tags' and 'depends_on' "
        "are comma-separated lists, 'priority' must be an integer, and an empty quoted "
        "value ('') clears a field. If 'status' is set, the ticket is moved to the "
        "matching folder so folder and frontmatter never disagree (moving to 'closed' "
        "auto-dates 'closed'). 'id' is structural and cannot be set.",
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
    p_set.set_defaults(func=cmd_set)

    return parser, dict(sub.choices)


def main():
    parser, _ = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except TicketError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
