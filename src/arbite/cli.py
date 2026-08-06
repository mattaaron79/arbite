"""arbite CLI: argument parsing and command dispatch."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, config, docs, ticket as ticket_mod
from .ticket import STATUSES, Ticket, TicketError


def _split_csv(value):
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


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


def cmd_list(args):
    tickets_root = _require_tickets_root()
    rows = []
    for _, t in ticket_mod.load_all_tickets(tickets_root):
        if args.status and t.status != args.status:
            continue
        if args.tier and t.tier != args.tier:
            continue
        if args.domain and t.domain != args.domain:
            continue
        if args.assignee and t.assignee != args.assignee:
            continue
        rows.append(t)

    if not rows:
        print("no tickets found")
        return

    rows.sort(key=lambda t: (t.status, t.id))
    id_w = max(len(t.id) for t in rows) + 1
    status_w = max(len(t.status) for t in rows) + 1
    tier_w = max(len(t.tier) for t in rows) + 1
    domain_w = max(len(t.domain) for t in rows) + 1
    assignee_w = max(len(t.assignee or "-") for t in rows) + 1

    for t in rows:
        print(
            f"{t.id:<{id_w}} {t.status:<{status_w}} {t.tier:<{tier_w}} "
            f"{t.domain:<{domain_w}} {(t.assignee or '-'):<{assignee_w}} {t.title}"
        )


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


def cmd_show(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id)
    print(path.read_text(encoding="utf-8"))


def cmd_deps(args):
    tickets_root = _require_tickets_root()
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

    if args.id not in by_id:
        print(f"error: no ticket found with id {args.id}", file=sys.stderr)
        sys.exit(1)
    walk(args.id, "", set())


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
        "list", help="list/filter tickets", description="List tickets, optionally filtered by one or more fields."
    )
    p_list.add_argument("--status", choices=STATUSES, help="filter by status")
    p_list.add_argument("--tier", choices=["low", "medium", "high"], help="filter by tier")
    p_list.add_argument("--domain", help="filter by domain")
    p_list.add_argument("--assignee", help="filter by assignee agent id")
    p_list.set_defaults(func=cmd_list)

    p_claim = sub.add_parser(
        "claim",
        help="move a ticket to in_progress/ and assign it",
        description="Move a ticket to tickets/in_progress/, set its assignee, and update status/updated.",
    )
    p_claim.add_argument("id", metavar="TICKET_ID", help="ticket id, e.g. tic-a1b2")
    p_claim.add_argument("--agent", required=True, help="agent id claiming the ticket, e.g. claude.haiku.001 (required)")
    p_claim.set_defaults(func=cmd_claim)

    p_block = sub.add_parser(
        "block",
        help="move a ticket to blocked/",
        description="Move a ticket to tickets/blocked/, set blocked_by, and update status/updated.",
    )
    p_block.add_argument("id", metavar="TICKET_ID", help="ticket id, e.g. tic-a1b2")
    p_block.add_argument(
        "--reason", required=True, help="why it's stalled: freeform text or another ticket id (required)"
    )
    p_block.set_defaults(func=cmd_block)

    p_close = sub.add_parser(
        "close",
        help="move a ticket to closed/YYYY-MM/",
        description="Move a ticket to tickets/closed/YYYY-MM/ (by today's date) and set status/closed/updated.",
    )
    p_close.add_argument("id", metavar="TICKET_ID", help="ticket id, e.g. tic-a1b2")
    p_close.set_defaults(func=cmd_close)

    p_show = sub.add_parser(
        "show", help="print a ticket's full contents", description="Print a ticket's raw markdown file (frontmatter + body)."
    )
    p_show.add_argument("id", metavar="TICKET_ID", help="ticket id, e.g. tic-a1b2")
    p_show.set_defaults(func=cmd_show)

    p_deps = sub.add_parser(
        "deps",
        help="walk depends_on to show a dependency tree",
        description="Recursively walk a ticket's depends_on field and print the dependency tree.",
    )
    p_deps.add_argument("id", metavar="TICKET_ID", help="ticket id, e.g. tic-a1b2")
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
