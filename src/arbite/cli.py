"""arbite CLI: argument parsing and command dispatch."""

from __future__ import annotations

import argparse
import heapq
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


def _matches_field_filters(args, t):
    """True if t passes the --status/--tier/--domain/--priority/--assignee filters."""
    if args.status and t.status != args.status:
        return False
    if args.tier and t.tier != args.tier:
        return False
    if args.domain and t.domain != args.domain:
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
    assignee_w = max(len(t.assignee or "-") for t in rows) + 1

    for t in rows:
        prio = "-" if t.priority is None else str(t.priority)
        print(
            f"{t.id:<{id_w}} {t.status:<{status_w}} {prio:<{priority_w}} "
            f"{t.tier:<{tier_w}} {t.domain:<{domain_w}} "
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
    urgent -- lowest priority number -- comes first). --tier narrows the
    candidates, and anything that isn't `open` is never considered."""
    scope = {
        t.id: t
        for t in all_tickets
        if t.status == "open" and (not args.tier or t.tier == args.tier)
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

    p_list = sub.add_parser(
        "list", help="list/filter tickets", description="List tickets, optionally filtered by one or more fields. "
        "Sorted so that within a status, more urgent tickets (lower priority number) come first."
    )
    p_list.add_argument("--status", choices=STATUSES, help="filter by status")
    p_list.add_argument("--tier", choices=["low", "medium", "high"], help="filter by tier")
    p_list.add_argument("--domain", help="filter by domain")
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
        "dependency order (most urgent first); combine with --tier to restrict to a "
        "capability tier",
    )
    p_list.set_defaults(func=cmd_list)

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
