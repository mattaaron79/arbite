"""arbite CLI: argument parsing and command dispatch."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, ticket as ticket_mod
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
        return

    for agent_id in agent_ids:
        scratchpad = agents_dir / f"{agent_id}.md"
        if scratchpad.exists():
            continue
        scratchpad.write_text(f"# {agent_id}\n\nNo ticket claimed yet.\n", encoding="utf-8")
        print(f"created scratchpad for {agent_id}")


def cmd_create(args):
    tickets_root = _require_tickets_root()
    existing_ids = {t.id for _, t in ticket_mod.load_all_tickets(tickets_root)}
    new_id = ticket_mod.gen_id(existing_ids)
    today = ticket_mod.today()

    new_ticket = Ticket(
        id=new_id,
        title=args.title,
        status="open",
        type=args.type,
        tier=args.tier,
        domain=args.domain,
        tags=_split_csv(args.tags),
        assignee=None,
        depends_on=_split_csv(args.depends_on),
        blocked_by=None,
        created=today,
        updated=today,
        closed=None,
        body=ticket_mod.DEFAULT_BODY.format(description=args.description or ""),
    )

    dest = ticket_mod.status_dir("open", tickets_root) / f"{new_id}.md"
    ticket_mod.save_ticket(new_ticket, dest)
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
    parser = argparse.ArgumentParser(prog="arbite", description="File-based ticketing system")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create tickets/ folder structure and agent scratchpads")
    p_init.set_defaults(func=cmd_init)

    p_create = sub.add_parser("create", help="create a new ticket in open/")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--type", required=True, choices=["bug", "feature", "refactor", "chore"])
    p_create.add_argument("--tier", required=True, choices=["low", "medium", "high"])
    p_create.add_argument("--domain", required=True)
    p_create.add_argument("--tags", default="", help="comma-separated")
    p_create.add_argument("--depends-on", default="", help="comma-separated ticket ids")
    p_create.add_argument("--description", default="")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list", help="list/filter tickets")
    p_list.add_argument("--status", choices=STATUSES)
    p_list.add_argument("--tier", choices=["low", "medium", "high"])
    p_list.add_argument("--domain")
    p_list.add_argument("--assignee")
    p_list.set_defaults(func=cmd_list)

    p_claim = sub.add_parser("claim", help="move a ticket to in_progress/ and assign it")
    p_claim.add_argument("id")
    p_claim.add_argument("--agent", required=True)
    p_claim.set_defaults(func=cmd_claim)

    p_block = sub.add_parser("block", help="move a ticket to blocked/")
    p_block.add_argument("id")
    p_block.add_argument("--reason", required=True)
    p_block.set_defaults(func=cmd_block)

    p_close = sub.add_parser("close", help="move a ticket to closed/YYYY-MM/")
    p_close.add_argument("id")
    p_close.set_defaults(func=cmd_close)

    p_show = sub.add_parser("show", help="print a ticket's full contents")
    p_show.add_argument("id")
    p_show.set_defaults(func=cmd_show)

    p_deps = sub.add_parser("deps", help="walk depends_on to show a dependency tree")
    p_deps.add_argument("id")
    p_deps.set_defaults(func=cmd_deps)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except TicketError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
