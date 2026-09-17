"""arbite CLI: argument parsing and command dispatch."""

from __future__ import annotations

import argparse
import heapq
import json
import re
import sys
from pathlib import Path

from . import __version__, config, docs, ticket as ticket_mod
from .ticket import STATUSES, TIERS, TYPES, Ticket, TicketError

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


def _print_json(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _emit_tickets(rows, as_json):
    """Emit a ticket list as JSON or a table, exiting EXIT_EMPTY when empty so a
    caller can branch on 'nothing to do' without matching on message text."""
    if as_json:
        _print_json([t.to_dict() for t in rows])
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

# `arbite bug|feature|memo|wish <message>` is shorthand for the equally-named
# `arbite raw <type> <message>` form: they create an identical raw ticket (same
# status 'raw', same 'classification' epic, same body notes), just with a shorter
# invocation. Each top-level subcommand is registered in build_parser() from this
# map so a single type can't drift from its shortcut.
RAW_SHORTCUT_HELP = {
    "bug": "capture a raw bug ticket (shorthand for 'arbite raw bug <message>')",
    "feature": "capture a raw feature ticket (shorthand for 'arbite raw feature <message>')",
    "memo": "capture a raw memo ticket (shorthand for 'arbite raw memo <message>')",
    "wish": "capture a raw wish ticket (shorthand for 'arbite raw wish <message>')",
}


def _require_tickets_root() -> Path:
    tickets_root = config.find_arbite_dir()
    if tickets_root is None:
        print(
            f"error: no {config.ARBITE_DIRNAME}/ directory found (run 'arbite init' first)",
            file=sys.stderr,
        )
        sys.exit(1)
    return tickets_root


def cmd_init(args):
    project_root = Path.cwd()
    tickets_root = project_root / config.ARBITE_DIRNAME
    for status in ticket_mod.FLAT_STATUS_DIRS:
        (tickets_root / status).mkdir(parents=True, exist_ok=True)
    (tickets_root / "closed").mkdir(parents=True, exist_ok=True)
    # Non-status buckets. Neither is a status -- tickets/notes there are parked,
    # not workable: wishlist/ holds reclassified wishes (see `arbite raw wish`
    # and `arbite move ... /wishlist`), planning/ holds planning/roadmap notes
    # and scratch docs.
    (tickets_root / "wishlist").mkdir(parents=True, exist_ok=True)
    (tickets_root / "planning").mkdir(parents=True, exist_ok=True)
    agents_dir = tickets_root / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    print(f"{config.ARBITE_DIRNAME}/ ready at {tickets_root}")

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
    agents_md = tickets_root / "AGENTS.md"
    agents_md.write_text(docs.render(parser, subparsers_by_name), encoding="utf-8")
    print(
        f"AGENTS.md refreshed at {agents_md} -- this is not auto-discovered, so point your "
        f"project's CLAUDE.md (or similar) at it explicitly, e.g. a line like "
        f"'read {config.ARBITE_DIRNAME}/AGENTS.md', if you want agents to find arbite"
    )


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
    now = ticket_mod.now()

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
        created=now,
        updated=now,
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
    """Create a deliberately unclassified 'raw' ticket in raw/ from a brief
    request. Only the type and a placeholder title are set; the body explains
    what still needs to be filled in (title, tier, domain, epic, priority, and
    an expanded description) before the ticket can be claimed or worked.
    Status 'raw' keeps it out of raw/'s workable neighbors -- it never shows
    up in 'arbite list next' until triage sets it to 'open' (or claims it
    directly). The ticket is auto-grouped under the 'classification' epic so
    triage/classification jobs can discover it with 'arbite list next --epic
    classification' or pull the oldest one with 'arbite fetch'. A 'wish' raw
    ticket carries an extra note: wishlist items are reclassified as 'feature'
    and filed in .arbite/wishlist/ rather than opened as work."""
    tickets_root = _require_tickets_root()
    existing_ids = {t.id for _, t in ticket_mod.load_all_tickets(tickets_root)}
    new_id = ticket_mod.gen_id(existing_ids)
    now = ticket_mod.now()
    message = " ".join(args.message)

    description = ticket_mod.RAW_DESCRIPTION.format(message=message)
    if args.type == "memo":
        description = f"{description}\n\n{ticket_mod.MEMO_RAW_NOTE}"
    elif args.type == "wish":
        description = f"{description}\n\n{ticket_mod.WISH_RAW_NOTE.format(id=new_id)}"
        (tickets_root / "wishlist").mkdir(parents=True, exist_ok=True)

    body = ticket_mod.DEFAULT_BODY.format(description=description)

    new_ticket = Ticket(
        id=new_id,
        title=ticket_mod.RAW_TITLE_FORMAT.format(type=args.type),
        status="raw",
        type=args.type,
        tier=ticket_mod.BLANK_TIER,
        domain=ticket_mod.BLANK_DOMAIN,
        epic=ticket_mod.CLASSIFICATION_EPIC,
        priority=None,
        tags=[],
        assignee=None,
        depends_on=[],
        blocked_by=None,
        created=now,
        updated=now,
        closed=None,
        body=body,
    )

    dest = ticket_mod.status_dir("raw", tickets_root) / f"{new_id}.md"
    ticket_mod.save_ticket(new_ticket, dest)
    print(
        f"created raw {args.type} ticket {new_id} at {dest} -- classify it "
        "(title/tier/domain/epic/priority/description) before it can be worked; "
        f"it is grouped under the '{ticket_mod.CLASSIFICATION_EPIC}' epic until then. "
        "Pull it for classification with 'arbite fetch'."
    )


def cmd_fetch(args):
    """Pull the oldest raw ticket (status 'raw'), optionally restricted to a type, and
    print it exactly like 'arbite show' would -- except with a 'derived_note' injected at
    the top (a JSON field in --json mode, a leading block in text mode) telling the
    calling agent to classify it and either open it for someone else or claim it now.
    This is a triage queue, so it's oldest-first (by 'created') rather than priority-
    ordered like 'list next' -- a raw ticket has no priority yet."""
    tickets_root = _require_tickets_root()
    all_tickets = [t for _, t in ticket_mod.load_all_tickets(tickets_root)]
    candidates = [
        t for t in all_tickets if t.status == "raw" and (not args.type or t.type == args.type)
    ]
    candidates.sort(key=lambda t: (t.created or "", t.id))

    if not candidates:
        if args.json:
            _print_json(None)
        else:
            qualifier = f" of type '{args.type}'" if args.type else ""
            print(f"no raw tickets{qualifier} found")
        sys.exit(EXIT_EMPTY)

    t = candidates[0]
    path = ticket_mod.status_dir(t.status, tickets_root, t.closed) / f"{t.id}.md"
    note = ticket_mod.derived_note(t.id, t.type)
    if args.json:
        data = t.to_dict(path)
        data["derived_note"] = note
        _print_json(data)
    else:
        print(f"derived_note: {note}\n")
        print(path.read_text(encoding="utf-8"))


def _matches_field_filters(args, t):
    """True if t passes the --status/--tier/--domain/--epic/--priority/--assignee
    filters. --status is a list of statuses and matches any one of them."""
    if args.status and t.status not in args.status:
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
    for raw_type in ticket_mod.RAW_TYPE_CHOICES:
        rows = by_type.get(raw_type)
        if not rows:
            continue
        print(f"\n{raw_type} ({len(rows)}):")
        for t in rows:
            request = ticket_mod.raw_captured_request(t)
            if request:
                print(f"  {t.id}  {request}")
            else:
                print(
                    f"  {t.id}  (request text no longer in body -- the description "
                    f"was edited; use 'arbite show {t.id}' to read what it is now)"
                )


def _cmd_list_raw(args, all_tickets):
    """Summarize every raw ticket (status 'raw') as a running todo list until a
    classification run drains it. The classification fields of a raw ticket are
    placeholders and its title is always '<type> (raw): Requires
    Classification', so rather than the usual flat table this groups tickets by
    raw type and shows, one ticket per line, the request text each was captured
    from. Oldest first by 'created' (then id), matching the oldest-first queue
    that `arbite fetch` pulls from -- a stable, chronological backlog a human
    or triage run can scan top to bottom. Exits 2 when nothing is raw yet,
    mirroring the other list views."""
    raw = [t for t in all_tickets if t.status == "raw"]
    raw.sort(key=lambda t: (t.created or "", t.id))

    if args.json:
        # JSON mode keeps the list contract: an array of ticket dicts whose
        # field names match the frontmatter, plus a derived 'request' field
        # (like `fetch` injects 'derived_note') so a caller can group or
        # display the captured text without parsing the body itself.
        payload = []
        for t in raw:
            data = t.to_dict()
            data["request"] = ticket_mod.raw_captured_request(t)
            payload.append(data)
        _print_json(payload)
    elif raw:
        _print_raw_summary(raw)
    else:
        print("no raw tickets found")
    if not raw:
        sys.exit(EXIT_EMPTY)


def _unmet_dependencies(t, by_id):
    """Ticket ids in t.depends_on that are not closed yet.

    Readiness is a property of the whole ticket set, never of a filtered
    subset: a blocker that a --tier/--domain/--epic filter excludes still
    blocks, so by_id must always be every known ticket. Dependency ids that
    don't resolve to a ticket are ignored rather than blocking forever."""
    return [d for d in t.depends_on if d in by_id and by_id[d].status != "closed"]


def _is_workable(t, by_id):
    """True if every ticket t depends on is closed (see _unmet_dependencies)."""
    return not _unmet_dependencies(t, by_id)


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
    (lowest priority number) is emitted first, then id for a stable tie-break.

    Only unmet dependencies (see _unmet_dependencies) constrain the order -- a closed
    dependency is already satisfied, so it must not hold its dependent back. Callers
    pass the full ticket set as scope so that a ticket excluded by their filters still
    orders the tickets that depend on it."""
    indegree = {tid: len(_unmet_dependencies(t, scope)) for tid, t in scope.items()}
    dependents = {tid: [] for tid in scope}
    for tid, t in scope.items():
        for d in _unmet_dependencies(t, scope):
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


def _tree_payload(scope, roots):
    """The dependency forest as nested JSON-serialisable dicts, mirroring what
    _print_tree renders. A ticket already on the current path is emitted with
    "cycle": true and not descended into."""
    def child_key(d):
        return (scope[d].priority_sort_key(), d)

    def node(tid, seen):
        t = scope[tid]
        data = t.to_dict()
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


def _live_cycles(by_id):
    """Dependency cycles that are actually unsatisfiable.

    A closed ticket satisfies anything depending on it, so a loop with even one
    closed member is already cut and its remaining tickets can still become
    workable -- reporting those would cry wolf on ordinary history. Only a
    cycle in which every member is still open/in_progress/blocked/shelved is a
    real deadlock: each ticket waits on another that waits back, and none can
    ever be worked."""
    return [
        cycle
        for cycle in ticket_mod.find_cycles(by_id)
        if all(by_id[tid].status != "closed" for tid in cycle)
    ]


def _warn_cycles(by_id, scope_ids=None):
    """Report any unsatisfiable depends_on cycle touching the tickets in scope
    on stderr.

    A topological order that silently appends cycle members hands agents work
    that will never become workable. Warn rather than fail, so one bad edge
    doesn't take down every query; `arbite doctor` reports the same cycles as a
    hard problem."""
    cycles = _live_cycles(by_id)
    if scope_ids is not None:
        cycles = [c for c in cycles if any(tid in scope_ids for tid in c)]
    for cycle in cycles:
        chain = " -> ".join(cycle + [cycle[0]])
        print(
            f"warning: dependency cycle, these tickets can never become workable: {chain}",
            file=sys.stderr,
        )
    return cycles


def _apply_count(rows, count):
    """Cap a result list to --count entries. None means no cap; the rows are
    already in the view's own order, so this always keeps the most relevant
    ones (most urgent first for a flat list, dependencies first for --topo)."""
    return rows if count is None else rows[:count]


def _print_topo(by_id, selected_ids, as_json=False, count=None):
    """Print the selected tickets in topological dependency order.

    The order is always computed over by_id (every ticket), so a blocker that
    the caller's filters exclude still holds back the tickets that depend on
    it; the filter is applied afterwards, as a pure selection over the
    already-ordered result."""
    _warn_cycles(by_id, selected_ids)
    rows = [by_id[tid] for tid in _topo_order(by_id) if tid in selected_ids]
    _emit_tickets(_apply_count(rows, count), as_json)


def _cmd_list_next(args, tickets_root, all_tickets):
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
    by_id = {t.id: t for t in all_tickets}
    candidates = [
        t
        for t in all_tickets
        if t.status == "open"
        and _is_workable(t, by_id)
        and (not args.tier or t.tier == args.tier)
        and (not args.domain or t.domain == args.domain)
        and (not args.epic or t.epic == args.epic)
    ]
    # Every candidate is workable, so none depends on another of them: the
    # topological order restricted to this set is exactly priority order.
    candidates.sort(key=lambda t: (t.priority_sort_key(), t.id))

    if not candidates:
        # "Nothing is ready" and "everything is deadlocked" look identical from
        # the outside, so say which one it is rather than leaving an agent to
        # poll a queue that can never produce work.
        _warn_cycles(by_id)

    # `next` answers "what should I work on", so it returns one ticket unless
    # the caller asks for a batch.
    wanted = 1 if args.count is None else args.count

    if not args.claim:
        _emit_tickets(candidates[:wanted], args.json)
        return

    if not candidates:
        # Nothing to claim is not an error; the exit code carries it.
        _emit_tickets([], args.json)
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
        path = ticket_mod.status_dir(candidate.status, tickets_root, candidate.closed) / f"{candidate.id}.md"
        try:
            new_path = ticket_mod.claim_ticket(path, candidate, tickets_root, args.claim)
        except TicketError as e:
            errors.append(str(e))
            continue
        claimed.append((candidate, new_path))

    if not claimed:
        raise TicketError(
            "every workable ticket was claimed by another agent first: " + "; ".join(errors)
        )

    if args.json:
        _print_json([t.to_dict(p) for t, p in claimed])
    else:
        _print_flat([t for t, _ in claimed])
        # The table is the result; the claim receipts are commentary on stderr.
        # Flush first so the two streams stay in order when stdout is a pipe.
        sys.stdout.flush()
        for t, new_path in claimed:
            print(f"claimed {t.id} for {args.claim} -> {new_path}", file=sys.stderr)
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
    if args.count is not None and args.count < 1:
        raise TicketError(f"--count must be a positive integer, got {args.count}")
    tickets_root = _require_tickets_root()
    all_tickets = [t for _, t in ticket_mod.load_all_tickets(tickets_root)]
    by_id = {t.id: t for t in all_tickets}
    tic_ids = set(_resolve_tic_terms(all_tickets, _split_csv(args.tic)))

    if args.subcommand == "next":
        _cmd_list_next(args, tickets_root, all_tickets)
        return

    if args.subcommand == "raw":
        _cmd_list_raw(args, all_tickets)
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
                _emit_tickets([], args.json)
                return
        if args.topo:
            _print_topo(by_id, set(scope), args.json, args.count)
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
        # A tree has no single flat length to cap, so --count limits the number
        # of top-level roots shown; each one still prints its full subtree,
        # since a truncated dependency chain would be actively misleading.
        roots = _apply_count(
            sorted(roots, key=lambda tid: (scope[tid].priority_sort_key(), tid)), args.count
        )
        _warn_cycles(by_id, set(scope))
        if args.json:
            _print_json(_tree_payload(scope, roots))
        else:
            _print_tree(scope, roots)
        return

    # Flat list: field filters plus the --tic id filter.
    rows = [
        t
        for t in all_tickets
        if _matches_field_filters(args, t) and (not tic_ids or t.id in tic_ids)
    ]
    # Within each status, more urgent (lower priority number) tickets come first;
    # tickets without a priority set sort last so they don't jump the queue.
    rows.sort(key=lambda t: (t.status, t.priority_sort_key(), t.id))
    _emit_tickets(_apply_count(rows, args.count), args.json)


def cmd_claim(args):
    """Claim a ticket for an agent. Claiming is a compare-and-swap, not a
    blind write: a ticket already assigned to somebody else is refused unless
    --force, and the move into in_progress/ is itself atomic, so two agents
    racing for the same ticket can't both come away believing they own it."""
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id, unique=True)
    previous = t.assignee
    if previous and previous != args.agent and not args.force:
        raise TicketError(
            f"ticket {t.id} is already assigned to {previous} (status: {t.status}); "
            "pass --force to take it over"
        )
    if previous and previous != args.agent:
        ticket_mod.append_note(
            t, args.agent, f"Claim taken over from {previous} (--force)."
        )
    new_path = ticket_mod.claim_ticket(path, t, tickets_root, args.agent)
    print(f"claimed {t.id} for {args.agent} -> {new_path}")


def cmd_release(args):
    """Return a claimed ticket to open/ and clear its assignee.

    The counterpart to claim: an agent that stops work part-way (out of scope,
    out of context, wrong capability tier) needs one command that unassigns and
    reopens together, so the ticket becomes visible to `list next` again rather
    than sitting in in_progress/ owned by nobody who is still working it."""
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id, unique=True)
    if t.status == "open" and t.assignee is None:
        raise TicketError(f"ticket {t.id} is already open and unassigned")
    previous = t.assignee
    message = "Released." if not args.reason else f"Released: {args.reason}"
    ticket_mod.append_note(t, args.agent, message)
    t.assignee = None
    t.blocked_by = None
    t.updated = ticket_mod.now()
    new_path = ticket_mod.move_ticket(path, t, tickets_root, "open")
    owner = f" (was {previous})" if previous else ""
    print(f"released {t.id}{owner} -> {new_path}")


def cmd_block(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id, unique=True)
    t.blocked_by = args.reason
    t.updated = ticket_mod.now()
    new_path = ticket_mod.move_ticket(path, t, tickets_root, "blocked")
    print(f"blocked {t.id} ({args.reason}) -> {new_path}")


def cmd_unblock(args):
    """Clear a block and move the ticket back into play.

    The symmetric counterpart to `block`. Doing this with `set status` leaves
    blocked_by populated, so the ticket claims to be stalled by something in
    every listing while sitting in open/ -- exactly the frontmatter drift the
    folder-is-truth rule exists to prevent."""
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id, unique=True)
    if t.status != "blocked":
        raise TicketError(f"ticket {t.id} is not blocked (status: {t.status})")
    reason = t.blocked_by
    was = f" (was blocked by: {reason})" if reason else ""
    message = f"Unblocked: {args.reason}" if args.reason else "Unblocked."
    if reason:
        message = f"{message.rstrip('.')} (was blocked by: {reason})."
    ticket_mod.append_note(t, args.agent, message)
    t.blocked_by = None
    t.updated = ticket_mod.now()
    # Back to whoever was working it if it is still assigned, otherwise open.
    dest = "in_progress" if (t.assignee and not args.open) else "open"
    if dest == "open":
        t.assignee = None
    new_path = ticket_mod.move_ticket(path, t, tickets_root, dest)
    print(f"unblocked {t.id}{was} -> {new_path}")


def cmd_close(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id, unique=True)
    t.closed = ticket_mod.now()
    t.updated = t.closed
    new_path = ticket_mod.move_ticket(path, t, tickets_root, "closed")
    print(f"closed {t.id} -> {new_path}")


def cmd_reopen(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id, unique=True)
    if t.status == "open":
        raise TicketError(f"ticket {t.id} is already open")
    t.closed = None
    t.blocked_by = None
    t.updated = ticket_mod.now()
    ticket_mod.append_note(t, args.agent, "Reopened.")
    new_path = ticket_mod.move_ticket(path, t, tickets_root, "open")
    print(f"reopened {t.id} -> {new_path}")


def cmd_shelve(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id, unique=True)
    t.updated = ticket_mod.now()
    message = "Shelved."
    if args.reason:
        message = f"Shelved: {args.reason}"
    ticket_mod.append_note(t, "system", message)
    new_path = ticket_mod.move_ticket(path, t, tickets_root, "shelved")
    print(f"shelved {t.id} -> {new_path}")


def cmd_unshelve(args):
    """Bring a shelved ticket back to open/.

    The counterpart to shelve: a ticket that was parked (deprioritized or
    paused) is moved back to open/ so it shows up in `arbite list next` again.
    The assignee and any stale block reason are cleared -- an unshelved ticket
    is back in the unclaimed pool, not reserved for whoever parked it."""
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id, unique=True)
    if t.status != "shelved":
        raise TicketError(f"ticket {t.id} is not shelved (status: {t.status})")
    t.updated = ticket_mod.now()
    message = "Unshelved."
    if args.reason:
        message = f"Unshelved: {args.reason}"
    ticket_mod.append_note(t, "system", message)
    t.assignee = None
    t.blocked_by = None
    new_path = ticket_mod.move_ticket(path, t, tickets_root, "open")
    print(f"unshelved {t.id} -> {new_path}")


def cmd_note(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id)
    message = " ".join(args.message)
    ticket_mod.append_note(t, args.agent, message)
    t.updated = ticket_mod.now()
    ticket_mod.save_ticket(t, path)
    print(f"added note to {t.id} by {args.agent}")


def cmd_show(args):
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id)
    if args.json:
        _print_json(t.to_dict(path))
        return
    print(path.read_text(encoding="utf-8"))


def cmd_deps(args):
    tickets_root = _require_tickets_root()
    _, start = ticket_mod.find_ticket(tickets_root, args.id)
    by_id = {t.id: t for _, t in ticket_mod.load_all_tickets(tickets_root)}

    if args.json:
        def node(tid, seen):
            t = by_id.get(tid)
            if t is None:
                # A dangling depends_on id: reported rather than dropped, so a
                # caller can tell "no dependencies" from "dependency deleted".
                return {"id": tid, "missing": True, "depends": []}
            data = t.to_dict()
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
    tickets_root = _require_tickets_root()
    path, t = ticket_mod.find_ticket(tickets_root, args.id, unique=True)
    if args.dep is None:
        t.depends_on = []
        t.updated = ticket_mod.now()
        ticket_mod.save_ticket(t, path)
        print(f"cleared dependencies of {t.id}")
        return
    _, dep = ticket_mod.find_ticket(tickets_root, args.dep, unique=True)
    if dep.id == t.id:
        raise TicketError(f"ticket {t.id} cannot depend on itself")
    if dep.id not in t.depends_on:
        t.depends_on.append(dep.id)
        t.updated = ticket_mod.now()
        ticket_mod.save_ticket(t, path)
        print(f"{t.id} now depends on {dep.id}")
    else:
        print(f"{t.id} already depends on {dep.id}")


def _find_any_ticket(tickets_root: Path, term: str):
    """Resolve a ticket id to (path, Ticket) by scanning every markdown file
    under the arbite root, including non-status folders (wishlist/, planning/)
    that the normal status-folder scan skips. `arbite move` uses this so it can
    move a ticket that is already filed in such a folder, not just one sitting
    in a status folder. An exact id always wins; otherwise an ambiguous match
    is an error listing the candidates."""
    term_lower = term.lower()
    matches = []
    for path in tickets_root.rglob("*.md"):
        if path.name == "AGENTS.md":
            # AGENTS.md and agent scratchpads aren't tickets and won't parse as
            # one; skip them so a stray parse error can't be mistaken for a
            # candidate or silently collapse the search.
            continue
        try:
            t = ticket_mod.load_ticket(path)
        except TicketError:
            continue
        if term_lower in t.id.lower():
            matches.append((path, t))
    matches.sort(key=lambda pair: pair[1].id)
    if not matches:
        raise TicketError(f"no ticket found matching '{term}'")
    exact = [m for m in matches if m[1].id.lower() == term.lower()]
    if exact:
        return exact[0]
    if len(matches) > 1:
        candidates = ", ".join(t.id for _, t in matches)
        raise TicketError(
            f"'{term}' is ambiguous -- it matches {len(matches)} tickets: "
            f"{candidates}. Pass a full ticket id."
        )
    return matches[0]


def cmd_move(args):
    """Move a ticket's file to a folder under the arbite root without changing
    its status or frontmatter -- a raw file move, nothing more. <folder> is
    root-relative, like '/' for the arbite root itself or '/wishlist' for
    .arbite/wishlist/; the destination folder is created if it doesn't exist.
    Because it does not touch status/updated, use it to file tickets into
    non-status buckets (wishlist/, planning/), and use the status commands
    (claim/block/close/...) for any move that should change state."""
    tickets_root = _require_tickets_root()
    path, t = _find_any_ticket(tickets_root, args.id)

    folder = args.folder.strip()
    if not folder.startswith("/"):
        raise TicketError(
            f"<folder> must be a root-relative path like '/' (the arbite root) or "
            f"'/wishlist', got '{args.folder}'"
        )
    # Root-relative -> absolute under tickets_root, dropping empty/'.' segments
    # and refusing '..' so a folder can never escape the arbite root.
    parts = [p for p in folder[1:].split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise TicketError(
            f"<folder> may not contain '..' (it must stay under the arbite root): '{folder}'"
        )
    dest_dir = tickets_root.joinpath(*parts) if parts else tickets_root
    dest_path = dest_dir / path.name

    if dest_path == path:
        print(f"{t.id} is already at {dest_path}")
        return
    if dest_path.exists():
        raise TicketError(f"a file already exists at {dest_path}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    path.replace(dest_path)
    print(f"moved {t.id} -> {dest_path}")


# Fields `arbite set` accepts: every frontmatter field except the structural id
# (the id is the filename, generated by `arbite create` and never renamed by
# hand), plus 'body' for the freeform markdown body.
SETTABLE_PROPERTIES = (set(ticket_mod.FIELD_ORDER) | {"body"}) - {"id"}

# Optional text fields: an empty quoted value clears them back to None.
_CLEARABLE_TEXT_FIELDS = {"epic", "assignee", "blocked_by", "closed"}


def _validate_field(prop: str, value: str) -> None:
    """Reject values `arbite create` would never have produced.

    `set` is the one way to write any field by hand, so without this it is the
    hole every controlled vocabulary leaks through -- a typo'd tier or a
    free-text date silently persists and then quietly fails to match the
    filters that route work to agents."""
    if value == "":
        return
    if prop == "status" and value not in STATUSES:
        raise TicketError(f"invalid status '{value}' (valid: {', '.join(STATUSES)})")
    if prop == "type" and value not in TYPES:
        raise TicketError(f"invalid type '{value}' (valid: {', '.join(TYPES)})")
    if prop == "tier" and value not in TIERS:
        raise TicketError(f"invalid tier '{value}' (valid: {', '.join(TIERS)})")
    if prop == "priority":
        try:
            if int(value) < 1:
                raise TicketError(
                    f"priority must be a positive integer, got '{value}' (lower = more urgent)"
                )
        except ValueError:
            raise TicketError(f"priority must be an integer, got '{value}'")
    if prop in ticket_mod.DATE_FIELDS and not ticket_mod.DATE_PATTERN.match(value):
        raise TicketError(
            f"{prop} must be a YYYY-MM-DD date or YYYY-MM-DDTHH:MM:SS timestamp, got '{value}'"
        )


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
    path, t = ticket_mod.find_ticket(tickets_root, args.id, unique=True)
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
        _validate_field(prop, value)

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
        t.updated = ticket_mod.now()

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
        if (not args.status or t.status in args.status)
        and any(matcher(_ticket_field_value(t, p)) for p in params)
    ]
    rows.sort(key=lambda t: (t.status, t.priority_sort_key(), t.id))
    _emit_tickets(rows, args.json)


def _expected_dir_status(path: Path, tickets_root: Path):
    """The status a ticket file's location implies, or None if it isn't in a
    recognised status folder."""
    try:
        rel = path.relative_to(tickets_root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) == 2 and parts[0] in ticket_mod.FLAT_STATUS_DIRS:
        return parts[0]
    if len(parts) == 3 and parts[0] == "closed":
        return "closed"
    return None


def cmd_doctor(args):
    """Check the invariants nothing else enforces, and optionally repair them.

    The whole design rests on the ticket's folder being the single source of
    truth, with frontmatter mirroring it -- but tickets are plain files in a git
    repo. Humans `mv` them, merges and rebases resurrect and mangle them, and a
    crash mid-move can strand a temp file. Every arbite command keeps the
    invariant; nothing until now noticed when something outside arbite broke
    it, which meant drift stayed invisible until an agent acted on a wrong
    status. Exits 3 when problems remain, so this can gate CI or an agent's
    startup."""
    tickets_root = _require_tickets_root()
    problems = []

    def report(kind, detail, ticket_id=None, fixed=False, path=None):
        problems.append(
            {
                "kind": kind,
                "detail": detail,
                "id": ticket_id,
                "path": str(path) if path else None,
                "fixed": fixed,
            }
        )

    # Unparseable files first: they can't take part in any later check, and a
    # ticket arbite cannot read is invisible to every listing.
    loaded = []
    for path in ticket_mod.iter_ticket_paths(tickets_root):
        try:
            loaded.append((path, ticket_mod.load_ticket(path)))
        except TicketError as e:
            report("unreadable", str(e), path=path)

    # Crash artifacts from an interrupted save/move. The content is intact, so
    # this is a recoverable ticket, not a lost one -- but only if someone looks.
    for status_dir_name in list(ticket_mod.FLAT_STATUS_DIRS) + ["closed"]:
        base = tickets_root / status_dir_name
        if not base.is_dir():
            continue
        for tmp in base.rglob(f"{ticket_mod.TMP_PREFIX}*"):
            if tmp.is_file():
                report(
                    "stray_temp_file",
                    f"leftover temp file from an interrupted write: {tmp} "
                    "(inspect it; it holds the full ticket content)",
                    path=tmp,
                )

    by_id = {}
    duplicates = {}
    for path, t in loaded:
        if t.id in by_id:
            duplicates.setdefault(t.id, [by_id[t.id][0]]).append(path)
        else:
            by_id[t.id] = (path, t)
    for tid, paths in duplicates.items():
        report(
            "duplicate_id",
            f"{len(paths)} files share id {tid}: {', '.join(str(p) for p in paths)} "
            "(resolve by hand -- arbite cannot know which is current)",
            ticket_id=tid,
        )

    for path, t in loaded:
        expected = _expected_dir_status(path, tickets_root)
        if expected is None:
            report("stray_file", f"ticket file outside any status folder: {path}", t.id, path=path)
            continue

        # The core invariant: folder wins, frontmatter is corrected to match.
        if t.status != expected:
            if args.fix:
                stale = t.status
                t.status = expected
                ticket_mod.save_ticket(t, path)
                report(
                    "status_drift",
                    f"frontmatter said '{stale}' but the file sits in {expected}/ "
                    f"-- corrected to '{expected}' (folder is source of truth)",
                    t.id, fixed=True, path=path,
                )
            else:
                report(
                    "status_drift",
                    f"frontmatter says status '{t.status}' but the file sits in "
                    f"{expected}/ -- the folder is source of truth",
                    t.id, path=path,
                )

        if expected == "closed":
            if not t.closed:
                report("closed_without_date", "closed ticket has no 'closed' date", t.id, path=path)
            else:
                month = t.closed[:7]
                actual_month = path.parent.name
                if month != actual_month:
                    if args.fix:
                        new_path = ticket_mod.move_ticket(path, t, tickets_root, "closed")
                        report(
                            "wrong_archive_month",
                            f"closed {t.closed} but archived under {actual_month}/ "
                            f"-- moved to {new_path.parent.name}/",
                            t.id, fixed=True, path=new_path,
                        )
                    else:
                        report(
                            "wrong_archive_month",
                            f"closed {t.closed} but archived under closed/{actual_month}/ "
                            f"(expected closed/{month}/)",
                            t.id, path=path,
                        )

        if expected == "in_progress" and not t.assignee:
            report(
                "in_progress_unassigned",
                "in_progress but has no assignee -- nobody is accountable for it and "
                "'list next' will never offer it; release it or claim it",
                t.id, path=path,
            )
        if expected == "blocked" and not t.blocked_by:
            report(
                "blocked_without_reason",
                "blocked but blocked_by is empty -- nothing records what is stalling it",
                t.id, path=path,
            )
        if expected != "closed" and t.closed:
            report(
                "closed_date_on_open_ticket",
                f"not closed but has a 'closed' date of {t.closed}",
                t.id, path=path,
            )

        for dep in t.depends_on:
            if dep not in by_id:
                report(
                    "dangling_dependency",
                    f"depends_on '{dep}', which is not a known ticket "
                    "(readiness silently ignores it, so this ticket can look workable "
                    "when its real prerequisite is gone)",
                    t.id, path=path,
                )
        if t.id in t.depends_on:
            report("self_dependency", "depends on itself", t.id, path=path)

        for prop, value in (("status", t.status), ("type", t.type), ("tier", t.tier)):
            if value is None:
                continue
            # `arbite raw` and `create --blank` deliberately write TODO
            # placeholders for a human or triage job to replace. Those are
            # pending work, not corruption -- flagging them would leave doctor
            # permanently failing in any repo with an untriaged ticket, which
            # is exactly when its exit code needs to mean something.
            if str(value).startswith("TODO:"):
                continue
            try:
                _validate_field(prop, str(value))
            except TicketError as e:
                report("invalid_field", str(e), t.id, path=path)
        for date_field in ticket_mod.DATE_FIELDS:
            value = getattr(t, date_field)
            if value and not ticket_mod.DATE_PATTERN.match(str(value)):
                report(
                    "invalid_field",
                    f"{date_field} is not a valid date/timestamp "
                    f"(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS): '{value}'",
                    t.id, path=path,
                )

    # Cycles are checked over the whole graph: an unsatisfiable loop means
    # every ticket in it is permanently unworkable, however it is filtered.
    tickets_by_id = {tid: pair[1] for tid, pair in by_id.items()}
    for cycle in _live_cycles(tickets_by_id):
        chain = " -> ".join(cycle + [cycle[0]])
        report(
            "dependency_cycle",
            f"dependency cycle -- none of these can ever become workable: {chain}",
            cycle[0],
        )

    if args.json:
        _print_json(
            {
                "tickets_checked": len(loaded),
                "problems": problems,
                "fixed": sum(1 for p in problems if p["fixed"]),
                "remaining": sum(1 for p in problems if not p["fixed"]),
            }
        )
    else:
        if not problems:
            print(f"checked {len(loaded)} tickets: no problems found")
        else:
            for p in problems:
                prefix = "fixed" if p["fixed"] else "problem"
                where = f" [{p['id']}]" if p["id"] else ""
                print(f"{prefix}{where} {p['kind']}: {p['detail']}")
            fixed = sum(1 for p in problems if p["fixed"])
            remaining = len(problems) - fixed
            print(f"\nchecked {len(loaded)} tickets: {remaining} problem(s), {fixed} fixed")
            if remaining and not args.fix:
                print("re-run with --fix to repair what arbite can correct automatically")

    if any(not p["fixed"] for p in problems):
        sys.exit(EXIT_PROBLEMS)


def build_parser():
    """Returns (parser, subparsers_by_name). The dict is used by `arbite init` to
    render .arbite/AGENTS.md's command reference straight from these parsers
    (every usage line and flag, compacted rather than dumped as --help text), so
    that doc can't drift from the real CLI."""
    parser = argparse.ArgumentParser(prog="arbite", description="File-based ticketing system")
    parser.add_argument("--version", action="version", version=f"arbite {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p_init = sub.add_parser(
        "init",
        help="create .arbite/ folder structure and agent scratchpads",
        description="Create .arbite/{raw,open,in_progress,blocked,shelved,closed,wishlist,planning,agents}/ "
        "in the current directory (like 'git init'), write .arbite/AGENTS.md (the command "
        "reference, not auto-discovered -- point your project's CLAUDE.md or similar at it "
        "explicitly if you want agents to find arbite), and pre-create a scratchpad file "
        "under .arbite/agents/ for every id listed in an 'agents:' list in ./arbite.yaml, "
        "if present. wishlist/ and planning/ are non-status buckets: wishlist/ holds "
        "reclassified wishes, planning/ holds planning notes and scratch docs.",
    )
    p_init.set_defaults(func=cmd_init)

    p_create = sub.add_parser(
        "create",
        help="create a new ticket in open/",
        description="Create a new ticket in open/ with a generated id (tic-XXXX). "
        "--title/--type/--tier/--domain are required unless --blank is given.",
    )
    p_create.add_argument("--title", help="short ticket title (required unless --blank)")
    p_create.add_argument(
        "--type",
        choices=ticket_mod.CREATE_TYPES,
        help="kind of work (required unless --blank)",
    )
    p_create.add_argument(
        "--tier",
        choices=TIERS,
        help="agent capability tier required to work this ticket "
        f"({ticket_mod.TIER_VALUES}). {ticket_mod.TIER_HELP} "
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
    p_create.set_defaults(func=cmd_create)

    p_raw = sub.add_parser(
        "raw",
        help="create an unclassified raw ticket in raw/ from a brief request",
        description="Capture a brief request as a raw ticket in raw/, with status 'raw' (so "
        "it never shows up in 'arbite list next'). Sets the type and title to '<type> (raw): "
        "Requires Classification', leaves tier/domain/priority as TODO placeholders, "
        "auto-groups the ticket under the 'classification' epic (so triage/classification "
        "jobs can find it with `arbite list next --epic classification`, or pull the oldest "
        "one with `arbite fetch`), and writes a body explaining that the ticket must be "
        "filled out (a real title, tier, domain, epic, priority, and an expanded description) "
        "before it can be claimed or worked. Use 'memo' when the request is to update "
        "project notes / documentation rather than make a code change. Use 'wish' for a "
        "wishlist item: the ticket notes that wishlist items are reclassified as 'feature' "
        "and filed in .arbite/wishlist/ rather than opened as work.",
    )
    p_raw.add_argument(
        "type",
        choices=ticket_mod.RAW_TYPE_CHOICES,
        metavar="TYPE",
        help="kind of raw ticket: memo | feature | bug | wish",
    )
    p_raw.add_argument("message", metavar="MESSAGE", nargs="+", help=docs.MESSAGE_HELP)
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
            "status 'raw' ticket in raw/ with the type set accordingly, grouped under the "
            "'classification' epic; classify it (title/tier/domain/epic/priority/description) "
            "before it can be claimed or worked.",
        )
        p_shortcut.add_argument("message", metavar="MESSAGE", nargs="+", help=docs.MESSAGE_HELP)
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
        "as 'feature' and filed in .arbite/wishlist/ instead of being opened or claimed. "
        "Exits 2 if no raw ticket matches.",
    )
    p_fetch.add_argument(
        "type",
        nargs="?",
        default=None,
        choices=ticket_mod.RAW_TYPE_CHOICES,
        metavar="TYPE",
        help="restrict to raw tickets of this type: memo | feature | bug | wish (default: any type)",
    )
    _json_flag(p_fetch)
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
        f"are only offered work you can actually do. {ticket_mod.TIER_HELP}",
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
        "grouped by type (memo/feature/bug/wish), one line per ticket showing its id "
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
    p_search.set_defaults(func=cmd_search)

    p_claim = sub.add_parser(
        "claim",
        help="move a ticket to in_progress/ and assign it",
        description="Move a ticket to in_progress/, set its assignee, and update "
        "status/updated. The claim is a compare-and-swap: a ticket already assigned to "
        "another agent is refused unless --force, and the move itself is atomic, so two "
        "agents racing for the same ticket cannot both end up believing they own it. "
        "(Identity assignment and liveness remain the agent harness's job.)",
    )
    p_claim.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_claim.add_argument("--agent", required=True, help="agent id claiming the ticket, e.g. claude.haiku.001 (required)")
    p_claim.add_argument(
        "--force",
        action="store_true",
        help="take over a ticket already assigned to another agent (records the takeover "
        "as a note); without this, claiming someone else's ticket is an error",
    )
    p_claim.set_defaults(func=cmd_claim)

    p_release = sub.add_parser(
        "release",
        help="return a claimed ticket to open/ and clear its assignee",
        description="The counterpart to claim: move a ticket back to open/, clear "
        "its assignee and any block reason, append a timestamped note, and update "
        "status/updated. Use it when an agent stops work part-way -- out of scope, out of "
        "context, or the wrong capability tier -- so the ticket becomes visible to "
        "'arbite list next' again instead of sitting in in_progress/ owned by nobody.",
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
    p_release.set_defaults(func=cmd_release)

    p_block = sub.add_parser(
        "block",
        help="move a ticket to blocked/",
        description="Move a ticket to blocked/, set blocked_by, and update status/updated.",
    )
    p_block.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_block.add_argument(
        "--reason", required=True, help="why it's stalled: freeform text or another ticket id (required)"
    )
    p_block.set_defaults(func=cmd_block)

    p_unblock = sub.add_parser(
        "unblock",
        help="clear a ticket's block and move it back into play",
        description="The counterpart to block: clear blocked_by, append a timestamped note "
        "recording what the block was, and move the ticket out of blocked/ -- back "
        "to in_progress/ if it is still assigned, otherwise to open/. Prefer this over "
        "'arbite set status', which leaves blocked_by populated so the ticket keeps "
        "claiming to be stalled in every listing.",
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
        help="send it to open/ and clear the assignee even if it is still assigned, "
        "instead of returning it to in_progress/ for its current owner",
    )
    p_unblock.set_defaults(func=cmd_unblock)

    p_close = sub.add_parser(
        "close",
        help="move a ticket to closed/YYYY-MM/",
        description="Move a ticket to closed/YYYY-MM/ (by today's date) and set status/closed/updated.",
    )
    p_close.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_close.set_defaults(func=cmd_close)

    p_reopen = sub.add_parser(
        "reopen",
        help="move a ticket back to open/ (reopen it)",
        description="Move a ticket that is not currently open back to open/: clear its "
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
        description="Move a ticket to shelved/, set status/updated, and append an automatic "
        "timestamped note recording that it was shelved (including --reason if given).",
    )
    p_shelve.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_shelve.add_argument(
        "--reason",
        default="",
        help="why it's being shelved; included in the automatic note (optional)",
    )
    p_shelve.set_defaults(func=cmd_shelve)

    p_unshelve = sub.add_parser(
        "unshelve",
        help="move a shelved ticket back to open/ (unshelve it)",
        description="Move a shelved ticket back to open/, clear its assignee and any block "
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
    p_unshelve.set_defaults(func=cmd_unshelve)

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
    p_show.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP_READONLY)
    _json_flag(p_show)
    p_show.set_defaults(func=cmd_show)

    p_deps = sub.add_parser(
        "deps",
        help="walk depends_on to show a dependency tree",
        description="Recursively walk a ticket's depends_on field and print the dependency tree.",
    )
    p_deps.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP_READONLY)
    _json_flag(p_deps)
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

    p_move = sub.add_parser(
        "move",
        help="move a ticket file to a folder under the arbite root (e.g. /wishlist)",
        description="Move a ticket's file to a folder under the arbite root, without "
        "changing its status or frontmatter. <folder> is root-relative: '/' is the arbite "
        "root itself (e.g. .arbite/), '/wishlist' is .arbite/wishlist/, and a longer path "
        "like '/planning/ideas' nests folders. The destination folder is created if it "
        "doesn't exist. This is deliberately a raw file move -- it does not update "
        "status/updated -- so use it to file tickets into non-status buckets (wishlist/, "
        "planning/), and use the status commands (claim/block/close/...) for any move that "
        "should change state.",
    )
    p_move.add_argument("id", metavar="TICKET_ID", help=TICKET_ID_HELP)
    p_move.add_argument(
        "folder",
        metavar="FOLDER",
        help="root-relative destination folder, e.g. '/' for the arbite root or "
        "'/wishlist' for .arbite/wishlist/",
    )
    p_move.set_defaults(func=cmd_move)

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

    p_doctor = sub.add_parser(
        "doctor",
        help="check ticket integrity (folder/frontmatter drift, cycles, dangling deps)",
        description="Check the invariants nothing else enforces and report what it finds. "
        "Tickets are plain files in a git repo -- humans move them, merges and rebases "
        "mangle them, and a crash mid-write can strand a temp file -- so the folder/"
        "frontmatter sync rule that every arbite command upholds can still be broken from "
        "outside. Detects status drift (the folder is authoritative), duplicate ids, "
        "unreadable tickets, stray/temp files, dependency cycles, dangling and self "
        "dependencies, wrong closed/YYYY-MM archive months, in_progress tickets with no "
        "assignee, blocked tickets with no reason, and invalid field values. Exits 3 if "
        "any problem remains, so it can gate CI or an agent's startup.",
    )
    p_doctor.add_argument(
        "--fix",
        action="store_true",
        help="repair what can be corrected unambiguously: frontmatter status is rewritten "
        "to match the folder it sits in, and closed tickets are moved into the archive "
        "month matching their close date. Anything needing a judgement call (duplicate "
        "ids, dependency cycles, missing data) is only reported",
    )
    _json_flag(p_doctor)
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
    except TicketError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    main()
