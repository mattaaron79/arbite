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


def tree_roots(scope: dict, roots) -> list:
    """The forest roots within scope: the tickets nothing else in scope depends
    on. Falls back to every ticket in scope when the whole scope is one cycle,
    so a caller always gets something to render."""
    if roots:
        return list(roots)
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
