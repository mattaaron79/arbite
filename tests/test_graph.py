"""Dependency-graph behavior, pinned as literals.

These expectations are hand-written rather than compared against the code they
were extracted from, because the code they were extracted from is deleted at the
end of the sinks work: a test that compares two implementations is worthless once
one of them is gone, while a pinned expectation keeps protecting the semantics of
`list --topo`, `list --tree`, `list next` readiness and `doctor`'s cycle
detection forever.
"""

from __future__ import annotations

from arbite import graph
from helpers import by_id, make_ticket


def test_unmet_dependencies_ignores_closed_and_unknown():
    a = make_ticket("tic-a", status="open")
    b = make_ticket("tic-b", status="closed", closed="2026-01-02")
    t = make_ticket("tic-t", depends_on=["tic-a", "tic-b", "tic-missing"])
    assert graph.unmet_dependencies(t, by_id(a, b, t)) == ["tic-a"]
    assert graph.is_workable(t, by_id(a, b, t)) is False
    assert graph.is_workable(t, by_id(b, t)) is True


def test_dependency_closure_is_transitive_and_keeps_dangling_ids():
    a = make_ticket("tic-a")
    b = make_ticket("tic-b", depends_on=["tic-a"])
    c = make_ticket("tic-c", depends_on=["tic-b", "tic-gone"])
    # A dangling dependency stays in the closure so the caller can report it;
    # renderers intersect the closure with the known set before walking it.
    assert graph.dependency_closure(by_id(a, b, c), ["tic-c"]) == {
        "tic-c",
        "tic-b",
        "tic-a",
        "tic-gone",
    }
    assert graph.dependency_closure(by_id(a, b, c), ["tic-gone"]) == {"tic-gone"}


def test_find_cycles_detects_loops_and_ignores_diamonds():
    diamond = by_id(
        make_ticket("tic-a"),
        make_ticket("tic-b", depends_on=["tic-a"]),
        make_ticket("tic-c", depends_on=["tic-a"]),
        make_ticket("tic-d", depends_on=["tic-b", "tic-c"]),
    )
    assert graph.find_cycles(diamond) == []

    loop = by_id(
        make_ticket("tic-a", depends_on=["tic-b"]),
        make_ticket("tic-b", depends_on=["tic-a"]),
    )
    assert graph.find_cycles(loop) == [["tic-a", "tic-b"]]

    three = by_id(
        make_ticket("tic-a", depends_on=["tic-c"]),
        make_ticket("tic-b", depends_on=["tic-a"]),
        make_ticket("tic-c", depends_on=["tic-b"]),
    )
    assert graph.find_cycles(three) == [["tic-a", "tic-c", "tic-b"]]

    self_dep = by_id(make_ticket("tic-a", depends_on=["tic-a"]))
    assert graph.find_cycles(self_dep) == [["tic-a"]]


def test_live_cycles_excludes_a_loop_already_cut_by_a_close():
    """A closed ticket satisfies anything depending on it, so a loop containing a
    closed member is not a deadlock and must not be reported as one."""
    cut = by_id(
        make_ticket("tic-a", status="closed", closed="2026-01-02", depends_on=["tic-b"]),
        make_ticket("tic-b", depends_on=["tic-a"]),
    )
    assert graph.live_cycles(cut) == []

    deadlocked = by_id(
        make_ticket("tic-a", depends_on=["tic-b"]),
        make_ticket("tic-b", depends_on=["tic-a"]),
    )
    assert graph.live_cycles(deadlocked) == [["tic-a", "tic-b"]]


def test_topo_order_puts_dependencies_first_and_breaks_ties_by_priority():
    a = make_ticket("tic-a", priority=9)
    b = make_ticket("tic-b", priority=1, depends_on=["tic-a"])
    c = make_ticket("tic-c", priority=2)
    scope = by_id(a, b, c)
    # c (p2) is ready immediately; b (p1) must wait for a, which is emitted first.
    assert graph.topo_order(scope) == ["tic-c", "tic-a", "tic-b"]


def test_topo_order_ties_break_on_id_and_unset_priority_sorts_last():
    late = make_ticket("tic-zzz")
    early = make_ticket("tic-aaa")
    mid = make_ticket("tic-mmm", priority=5)
    assert graph.topo_order(by_id(late, early, mid)) == ["tic-mmm", "tic-aaa", "tic-zzz"]


def test_topo_order_does_not_let_a_closed_dependency_hold_its_dependent_back():
    closed_dep = make_ticket("tic-a", status="closed", closed="2026-01-02", priority=9)
    dependent = make_ticket("tic-b", priority=1, depends_on=["tic-a"])
    # `a` is satisfied, so it no longer constrains `b`; b is emitted on priority.
    assert graph.topo_order(by_id(closed_dep, dependent)) == ["tic-b", "tic-a"]


def test_topo_order_appends_cycle_members_in_priority_order():
    a = make_ticket("tic-a", priority=1, depends_on=["tic-b"])
    b = make_ticket("tic-b", priority=2, depends_on=["tic-a"])
    assert graph.topo_order(by_id(a, b)) == ["tic-a", "tic-b"]


def test_tree_roots_are_the_tickets_nothing_depends_on():
    a = make_ticket("tic-a")
    b = make_ticket("tic-b", depends_on=["tic-a"])
    scope = by_id(a, b)
    assert graph.tree_roots(scope, []) == ["tic-b"]


def test_tree_roots_fall_back_to_every_ticket_when_scope_is_all_cycle():
    a = make_ticket("tic-a", depends_on=["tic-b"])
    b = make_ticket("tic-b", depends_on=["tic-a"])
    scope = by_id(a, b)
    assert sorted(graph.tree_roots(scope, [])) == ["tic-a", "tic-b"]


def test_tree_roots_prefer_explicit_roots():
    a = make_ticket("tic-a")
    b = make_ticket("tic-b", depends_on=["tic-a"])
    scope = by_id(a, b)
    assert graph.tree_roots(scope, ["tic-a"]) == ["tic-a"]


def test_cycle_warnings_format_a_chain_and_scope_filter():
    a = make_ticket("tic-a", depends_on=["tic-b"])
    b = make_ticket("tic-b", depends_on=["tic-a"])
    c = make_ticket("tic-c")
    scope = by_id(a, b, c)
    assert graph.cycle_warnings(scope) == ["tic-a -> tic-b -> tic-a"]
    assert graph.cycle_warnings(scope, scope_ids={"tic-c"}) == []
    assert graph.cycle_warnings(scope, scope_ids={"tic-b"}) == ["tic-a -> tic-b -> tic-a"]


# --- what the tree and deps views render ----------------------------------


def test_dependency_rows_expand_a_shared_dependency_once():
    """A diamond is a tree with a back-reference, not two copies of the same subtree.

    This is the point of keeping two guards: `path` marks a loop, `expanded` marks a
    node already printed, so a shared prerequisite is expanded at the first path that
    reaches it and referred back to from every later one. With one path-scoped set the
    output is as long as the number of root-to-node paths, which is what made a
    release gate that every ticket feeds into thousands of repeated lines."""
    a = make_ticket("tic-a", priority=1)
    b = make_ticket("tic-b", priority=2, depends_on=["tic-a"])
    c = make_ticket("tic-c", priority=3, depends_on=["tic-a"])
    d = make_ticket("tic-d", priority=4, depends_on=["tic-b", "tic-c"])
    assert graph.dependency_rows(by_id(a, b, c, d), ["tic-d"]) == [
        graph.Row("tic-d", 0, graph.EDGE_ROOT, True, None),
        graph.Row("tic-b", 1, graph.EDGE_UNMET, False, None),
        graph.Row("tic-a", 2, graph.EDGE_UNMET, True, None),
        graph.Row("tic-c", 1, graph.EDGE_UNMET, True, None),
        graph.Row("tic-a", 2, graph.EDGE_UNMET, True, graph.MARK_REPEAT),
    ]


def test_dependency_rows_full_is_the_unrolled_walk():
    """`--full` is the older per-path walk: the same ticket once per path that reaches
    it, and no back-reference anywhere."""
    a = make_ticket("tic-a", priority=1)
    b = make_ticket("tic-b", priority=2, depends_on=["tic-a"])
    c = make_ticket("tic-c", priority=3, depends_on=["tic-a"])
    d = make_ticket("tic-d", priority=4, depends_on=["tic-b", "tic-c"])
    scope = by_id(a, b, c, d)
    rows = graph.dependency_rows(scope, ["tic-d"], expand_once=False)
    assert [row.id for row in rows] == ["tic-d", "tic-b", "tic-a", "tic-c", "tic-a"]
    assert all(row.mark is None for row in rows)


def test_dependency_rows_mark_a_loop_a_dangling_id_and_a_satisfied_edge():
    """The three things a row can say beyond the ticket's own fields: the edge closes a
    loop, the id resolves to no ticket (reported rather than dropped, so 'no
    dependency' and 'dependency deleted' stay distinguishable), and the dependency is
    closed -- which is `satisfied`, because it no longer holds its dependent back."""
    x = make_ticket("tic-x", depends_on=["tic-y"])
    y = make_ticket("tic-y", depends_on=["tic-x"])
    z = make_ticket("tic-z", depends_on=["tic-x", "tic-gone"])
    done = make_ticket("tic-done", status="closed", closed="2026-02-01")
    waiting = make_ticket("tic-waiting", priority=1, depends_on=["tic-done"])

    assert graph.dependency_rows(by_id(x, y, z), ["tic-z"]) == [
        graph.Row("tic-z", 0, graph.EDGE_ROOT, True, None),
        graph.Row("tic-x", 1, graph.EDGE_UNMET, False, None),
        graph.Row("tic-y", 2, graph.EDGE_UNMET, True, None),
        graph.Row("tic-x", 3, graph.EDGE_UNMET, True, graph.MARK_CYCLE),
        graph.Row("tic-gone", 1, graph.EDGE_UNKNOWN, True, graph.MARK_MISSING),
    ]
    assert graph.dependency_rows(by_id(done, waiting), ["tic-waiting"]) == [
        graph.Row("tic-waiting", 0, graph.EDGE_ROOT, True, None),
        graph.Row("tic-done", 1, graph.EDGE_SATISFIED, True, None),
    ]


def test_dependency_rows_leave_out_what_the_caller_scoped_away():
    """A filter is not a break in the graph: an id outside the scope is not rendered at
    all, while one that resolves to nothing is still reported as missing."""
    a = make_ticket("tic-a", priority=1)
    b = make_ticket("tic-b", priority=2, depends_on=["tic-a", "tic-gone"])
    rows = graph.dependency_rows(by_id(a, b), ["tic-b"], scope_ids={"tic-b"})
    assert [row.id for row in rows] == ["tic-b", "tic-gone"]
    assert [row.mark for row in rows] == [None, graph.MARK_MISSING]


def test_dependency_rows_walk_dependents_when_asked():
    """The same forest from its other end: children are the tickets that depend on a
    ticket, so a prerequisite shows what it is holding up."""
    a = make_ticket("tic-a", priority=1)
    b = make_ticket("tic-b", priority=2, depends_on=["tic-a"])
    c = make_ticket("tic-c", priority=3, depends_on=["tic-a"])
    rows = graph.dependency_rows(by_id(a, b, c), ["tic-a"], dependents=True)
    assert [(row.id, row.depth, row.edge) for row in rows] == [
        ("tic-a", 0, graph.EDGE_ROOT),
        ("tic-b", 1, graph.EDGE_UNMET),
        ("tic-c", 1, graph.EDGE_UNMET),
    ]


def test_tree_roots_can_come_from_the_dependents_end():
    """Without explicit roots the forest's roots are the tickets nothing depends on; walk
    dependents instead, and they are the tickets whose own dependencies all sit outside
    the scope."""
    a = make_ticket("tic-a")
    b = make_ticket("tic-b", depends_on=["tic-a"])
    scope = by_id(a, b)
    assert graph.tree_roots(scope, []) == ["tic-b"]
    assert graph.tree_roots(scope, [], dependents=True) == ["tic-a"]


def test_dependency_rows_stay_one_row_per_ticket_in_a_lattice():
    """The property the memoisation buys, on the shape that needs it: four rungs of two
    nodes, each rung depending on both nodes below it. The walk stays bounded by the
    graph -- ten tickets and sixteen edges -- where the unrolled one spells out all 47
    paths down to the shared leaf."""
    leaf = make_ticket("tic-leaf")
    tickets = [leaf]
    below = [leaf]
    for depth in range(4):
        below = [
            make_ticket(f"tic-{depth}{side}", depends_on=[t.id for t in below])
            for side in "ab"
        ]
        tickets.extend(below)
    top = make_ticket("tic-top", depends_on=[t.id for t in below])
    tickets.append(top)
    scope = by_id(*tickets)

    rows = graph.dependency_rows(scope, ["tic-top"])
    unrolled = graph.dependency_rows(scope, ["tic-top"], expand_once=False)
    assert {row.id for row in rows} == {ticket.id for ticket in tickets}
    # Nine of the sixteen edges expand a ticket for the first time and the other seven
    # are back-references, so the row count is bounded by the graph rather than by the
    # number of paths through it.
    assert len(rows) == 17
    assert sum(1 for row in rows if row.mark == graph.MARK_REPEAT) == 7
    assert len(unrolled) == 47, "the unrolled walk is one row per path"
