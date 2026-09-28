"""Dependency-graph logic over a set of tickets.

Pure by construction: takes `{ticket_id: Ticket}` and returns ids or cycles, so
it is shared by every sink and every command without touching storage. These
functions were moved out of `cli.py` and `ticket.py` unchanged in behavior --
`list --topo`, `list --tree`, `list next` readiness and `doctor`'s cycle
detection depend on exactly these semantics.

Throughout: readiness is a property of the whole ticket set, never of a filtered
subset. A blocker that a --tier/--domain/--epic filter excludes still blocks, so
callers must always pass every known ticket as `by_id`/`scope`.
"""

from __future__ import annotations

import heapq
from typing import NamedTuple, Optional


def unmet_dependencies(ticket, by_id: dict) -> list:
    """Ticket ids in `ticket.depends_on` that are not closed yet.

    Dependency ids that don't resolve to a known ticket are ignored rather than
    blocking forever; `arbite doctor` reports those as dangling instead."""
    return [d for d in ticket.depends_on if d in by_id and by_id[d].status != "closed"]


def is_workable(ticket, by_id: dict) -> bool:
    """True if every ticket this one depends on is closed."""
    return not unmet_dependencies(ticket, by_id)


def dependency_closure(by_id: dict, root_ids) -> set:
    """All ticket ids reachable from root_ids by following depends_on (roots included).

    Ids that don't resolve to a known ticket are included in the result rather
    than dropped: the caller then decides whether a dangling dependency is a
    problem to report (`doctor`) or a node to skip while rendering (the tree and
    topological views both intersect this with `by_id`)."""
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


def find_cycles(by_id: dict) -> list:
    """Every depends_on cycle among the given tickets, each as a list of ids in
    cycle order. Iterative DFS with an explicit stack, so a pathological
    dependency graph can't exhaust the recursion limit."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in by_id}
    cycles = []
    seen_keys = set()
    for start in sorted(by_id):
        if color[start] != WHITE:
            continue
        color[start] = GREY
        stack = [(start, iter(by_id[start].depends_on))]
        chain = [start]
        while stack:
            node, deps = stack[-1]
            descended = False
            for dep in deps:
                if dep not in by_id:
                    continue
                if color[dep] == GREY:
                    cycle = chain[chain.index(dep):]
                    key = tuple(sorted(cycle))
                    if key not in seen_keys:
                        seen_keys.add(key)
                        cycles.append(cycle)
                    continue
                if color[dep] == WHITE:
                    color[dep] = GREY
                    stack.append((dep, iter(by_id[dep].depends_on)))
                    chain.append(dep)
                    descended = True
                    break
            if not descended:
                color[node] = BLACK
                stack.pop()
                chain.pop()
    return cycles


def live_cycles(by_id: dict) -> list:
    """Dependency cycles that are actually unsatisfiable.

    A closed ticket satisfies anything depending on it, so a loop with even one
    closed member is already cut and its remaining tickets can still become
    workable -- reporting those would cry wolf on ordinary history. Only a cycle
    in which every member is still open/in_progress/blocked/shelved is a real
    deadlock: each ticket waits on another that waits back, and none can ever be
    worked."""
    return [
        cycle
        for cycle in find_cycles(by_id)
        if all(by_id[tid].status != "closed" for tid in cycle)
    ]


def topo_order(scope: dict) -> list:
    """Kahn's algorithm over scope: dependencies are emitted before the tickets that
    depend on them; when several tickets are ready at once (siblings), the most urgent
    (lowest priority number) is emitted first, then id for a stable tie-break.

    Only unmet dependencies (see `unmet_dependencies`) constrain the order -- a closed
    dependency is already satisfied, so it must not hold its dependent back. Callers
    pass the full ticket set as scope so that a ticket excluded by their filters still
    orders the tickets that depend on it."""
    indegree = {tid: len(unmet_dependencies(t, scope)) for tid, t in scope.items()}
    dependents = {tid: [] for tid in scope}
    for tid, t in scope.items():
        for d in unmet_dependencies(t, scope):
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


def tree_roots(scope: dict, roots, *, dependents: bool = False) -> list:
    """The forest roots within scope: the tickets nothing else in scope depends on --
    or, with `dependents`, the tickets whose dependencies all sit outside scope, which
    is the other end of the same forest. Falls back to every ticket in scope when the
    whole scope is one cycle, so a caller always gets something to render."""
    if roots:
        return list(roots)
    if dependents:
        found = [
            tid
            for tid, ticket in scope.items()
            if not any(dep in scope for dep in ticket.depends_on)
        ]
    else:
        found = [
            tid for tid in scope if not any(tid in other.depends_on for other in scope.values())
        ]
    return found or list(scope)


def cycle_warnings(by_id: dict, scope_ids=None) -> list:
    """Cycle chains to warn about, as formatted 'a -> b -> a' strings.

    A topological order that silently appends cycle members hands agents work
    that will never become workable. Callers warn on stderr rather than fail, so
    one bad edge doesn't take down every query; `arbite doctor` reports the same
    cycles as a hard problem."""
    cycles = live_cycles(by_id)
    if scope_ids is not None:
        cycles = [c for c in cycles if any(tid in scope_ids for tid in c)]
    return [" -> ".join(cycle + [cycle[0]]) for cycle in cycles]


# --- Display: flattening a dependency forest into rows ----------------------
#
# `deps`, `list --tree` and their `--json` documents all render the same thing, so
# they share one walk. What keeps a *DAG* legible as an indented tree is that two
# guards stay two sets: `path` holds the ancestors of the node being visited, so an
# edge back up is a cycle, while `expanded` holds everything already emitted
# anywhere, so the second path that reaches a shared prerequisite ends in a
# back-reference instead of that subtree printed a second time. Collapsing them into
# one path-scoped set -- which is what these views used to do -- makes the output as
# long as the number of distinct root-to-node paths, so a mesh like a release gate
# that every ticket feeds into prints thousands of repeated lines rather than one
# per ticket.

EDGE_ROOT = "root"
EDGE_UNMET = "unmet"
EDGE_SATISFIED = "satisfied"
EDGE_UNKNOWN = "unknown"

MARK_REPEAT = "repeat"
MARK_CYCLE = "cycle"
MARK_MISSING = "missing"


class Row(NamedTuple):
    """One line of a rendered forest.

    `depth` is 0 for a root, `last` says whether the row is its parent's last child
    (what a connector needs), and `edge` is how the row stands to the ticket above
    it: `unmet` while that dependency is still open -- and so holds its dependent
    back -- `satisfied` once it is closed, `unknown` when the id resolves to no
    ticket at all, and `root` for a root. `mark` is set only when there is more to
    say than the ticket's own fields do: its subtree was already printed above, the
    edge closes a loop, or the id is dangling. A marked row is never descended into,
    so it is always a leaf of the rendered tree."""

    id: str
    depth: int
    edge: str
    last: bool
    mark: Optional[str]


def dependency_rows(
    by_id: dict,
    roots,
    *,
    scope_ids=None,
    dependents: bool = False,
    expand_once: bool = True,
) -> list:
    """Flatten the dependency forest rooted at `roots` into `Row`s, in display order.

    Children are the ticket's `depends_on`, most urgent first (priority, then id) --
    or, with `dependents`, the tickets in scope that depend on it, which walks the
    same forest from its other end. Only ids in `scope_ids` are rendered, because a
    caller's filter is not a break in the graph; an id that resolves to no ticket is
    rendered anyway and marked `missing`, so a caller can tell 'no dependency' from
    'dependency deleted'.

    Every ticket is expanded once, wherever the walk first reaches it; arriving a
    second time prints a `repeat` row instead of the subtree again, which is what
    stops a diamond multiplying the output. `expand_once=False` restores the unrolled
    per-path walk `--full` asks for. Either way the walk is iterative, so a
    pathological dependency chain cannot exhaust the recursion limit."""
    if scope_ids is None:
        scope_ids = set(by_id)
    children_by_id = _dependents_index(by_id, scope_ids) if dependents else None

    def visible(tid: str) -> bool:
        return tid not in by_id or tid in scope_ids

    def urgency(tid: str) -> tuple:
        ticket = by_id.get(tid)
        # A dangling id has no priority to sort on, so it sorts after every real
        # ticket, by id.
        if ticket is None:
            return (1, 0.0, tid)
        return (0, ticket.priority_sort_key(), tid)

    def ordered_children(tid: str) -> list:
        if children_by_id is not None:
            child_ids = children_by_id.get(tid, [])
        else:
            ticket = by_id.get(tid)
            child_ids = list(ticket.depends_on) if ticket is not None else []
        return sorted((child for child in child_ids if visible(child)), key=urgency)

    def edge_of(ticket) -> str:
        return EDGE_SATISFIED if ticket.status == "closed" else EDGE_UNMET

    def describe(tid: str) -> tuple:
        """(edge, mark) for a ticket reached from the node whose frame is open."""
        ticket = by_id.get(tid)
        if ticket is None:
            return EDGE_UNKNOWN, MARK_MISSING
        if tid in path:
            return edge_of(ticket), MARK_CYCLE
        if expand_once and tid in expanded:
            return edge_of(ticket), MARK_REPEAT
        return edge_of(ticket), None

    rows = []
    expanded = set()
    path = set()
    # Frames are (tid, depth, children, next_index): children are materialised once, so
    # 'is this the last sibling' is a comparison, and the stack is exactly the path,
    # which is what a cycle edge is measured against.
    stack = []

    def open_row(tid: str, depth: int, edge: str, mark, last: bool):
        """Record one row, and return the frame that walks its children -- or None when
        the row is a leaf, since a marked row is never descended into."""
        rows.append(Row(tid, depth, edge, last, mark))
        expanded.add(tid)
        if mark is not None:
            return None
        path.add(tid)
        return (tid, depth, ordered_children(tid), 0)

    ordered_roots = sorted(roots, key=urgency)
    for position in range(len(ordered_roots) - 1, -1, -1):
        tid = ordered_roots[position]
        _, mark = describe(tid)
        frame = open_row(tid, 0, EDGE_ROOT, mark, position == len(ordered_roots) - 1)
        if frame is None:
            continue
        stack.append(frame)
        while stack:
            open_tid, depth, children, index = stack[-1]
            if index >= len(children):
                path.discard(open_tid)
                stack.pop()
                continue
            child = children[index]
            stack[-1] = (open_tid, depth, children, index + 1)
            edge, mark = describe(child)
            child_frame = open_row(
                child, depth + 1, edge, mark, index == len(children) - 1
            )
            if child_frame is not None:
                stack.append(child_frame)
    return rows


def _dependents_index(by_id: dict, scope_ids) -> dict:
    """`{id: [ids in scope that depend on it]}`, the reverse of `depends_on`."""
    index = {tid: [] for tid in scope_ids}
    for tid in scope_ids:
        ticket = by_id.get(tid)
        if ticket is None:
            continue
        for dep in ticket.depends_on:
            if dep in index:
                index[dep].append(tid)
    return index
