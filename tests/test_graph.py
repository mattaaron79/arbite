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
