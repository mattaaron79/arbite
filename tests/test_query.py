"""The query vocabulary, pinned as literals.

Like `test_graph.py`, expectations are hand-written: this is the reference
behavior every sink must reproduce, so it must be stated independently of any
implementation that could drift.
"""

from __future__ import annotations

import pytest

from arbite import query
from arbite.errors import AmbiguousTicketId, TicketError, TicketNotFound
from helpers import make_ticket


# --- text matching ---------------------------------------------------------


def test_compile_matcher_modes():
    assert query.compile_matcher("LOD", "substring")("mesh lod pop-in")
    assert not query.compile_matcher("zzz", "substring")("mesh lod pop-in")
    assert query.compile_matcher("*pop*", "wildcard")("mesh LOD pop-in")
    assert query.compile_matcher("lod.*pop", "regex")("mesh LOD pop-in")
    # case-insensitive in every mode
    assert query.compile_matcher("lod", "substring")("LOD")
    assert query.compile_matcher("L[Oo]D", "regex")("lod")


def test_compile_matcher_rejects_bad_input():
    with pytest.raises(TicketError, match="invalid text match mode"):
        query.compile_matcher("x", "fuzzy")
    with pytest.raises(TicketError, match="invalid regex"):
        query.compile_matcher("(", "regex")


def test_text_match_searches_only_selected_fields():
    t = make_ticket(title="fix LOD pop-in", domain="mesh", tags=["lod"])
    assert query.TextMatch("pop-in", fields=("title",)).matches(t)
    assert query.TextMatch("pop-in", fields=("domain",)).matches(t) is False
    # lists are searched in their comma-joined form
    assert query.TextMatch("lod", fields=("tags",)).matches(t)


def test_text_match_all_covers_body_and_every_field():
    t = make_ticket(domain="audio_gen", body="## Description\nring buffer fix\n\n## Notes\n")
    assert query.TextMatch("ring buffer").matches(t)
    assert query.TextMatch("audio_gen").matches(t)
    assert query.TextMatch("nothing here").matches(t) is False


def test_text_match_unknown_fields_are_reported():
    assert query.TextMatch("x", fields=("title", "nope")).unknown_fields() == ["nope"]
    assert query.TextMatch("x").unknown_fields() == []
    q = query.TicketQuery(text=query.TextMatch("x", fields=("nope",)))
    with pytest.raises(TicketError, match="unknown ticket field"):
        q.normalized()


# --- query construction ----------------------------------------------------


def test_normalized_coerces_scalars_to_tuples():
    q = query.TicketQuery(status="open", type=["bug", "feature"], ids="tic-a1b2").normalized()
    assert q.status == ("open",)
    assert q.type == ("bug", "feature")
    assert q.ids == ("tic-a1b2",)
    assert query.TicketQuery().normalized().status == ()


def test_normalized_validates_order_and_fails_fast_on_bad_regex():
    with pytest.raises(TicketError, match="invalid order"):
        query.TicketQuery(order="sideways").normalized()
    with pytest.raises(TicketError, match="invalid regex"):
        query.TicketQuery(text=query.TextMatch("(", "regex")).normalized()


def test_evolve_keeps_a_normalized_copy():
    q = query.TicketQuery(status="open").evolve(limit=3)
    assert q.status == ("open",)
    assert q.limit == 3


# --- reference predicate ---------------------------------------------------


def test_matches_applies_every_structured_filter():
    t = make_ticket(
        status="blocked",
        type="bug",
        tier="high",
        domain="mesh",
        epic="mesh-pipeline",
        assignee="claude.haiku.001",
        priority=2,
    )
    def q(**kw):
        return query.TicketQuery(**kw).normalized()

    assert q(status="blocked").matches(t)
    assert q(status=["open", "blocked"]).matches(t)
    assert q(status="open").matches(t) is False
    assert q(type="bug").matches(t)
    assert q(type="chore").matches(t) is False
    assert q(tier="high").matches(t)
    assert q(tier="low").matches(t) is False
    assert q(domain="mesh").matches(t)
    assert q(domain="io").matches(t) is False
    assert q(epic="mesh-pipeline").matches(t)
    assert q(epic="other").matches(t) is False
    assert q(assignee="claude.haiku.001").matches(t)
    assert q(assignee="claude.opus.001").matches(t) is False
    assert q(priority=2).matches(t)
    assert q(priority=3).matches(t) is False
    # An unset priority is not equal to a priority filter, even priority 0-style filters.
    assert q(priority=1).matches(make_ticket()) is False
    assert q(ids=["tic-zzzz"]).matches(t) is False
    assert q(ids=["tic-a1b2"]).matches(t)


def test_matches_combines_filters_and_text():
    t = make_ticket(title="fix LOD pop-in", domain="mesh")
    q = query.TicketQuery(status="open", domain="mesh", text=query.TextMatch("lod")).normalized()
    assert q.matches(t)
    assert q.evolve(domain="io").matches(t) is False
    assert q.evolve(text=query.TextMatch("audio")).matches(t) is False


# --- ordering --------------------------------------------------------------


def test_sort_key_per_order():
    a = make_ticket("tic-a", status="open", priority=5, created="2026-03-01")
    b = make_ticket("tic-b", status="blocked", priority=1, created="2026-01-01")
    c = make_ticket("tic-c", status="open", created="2026-02-01")

    assert [t.id for t in query.sort_tickets([a, b, c], "flat")] == ["tic-b", "tic-a", "tic-c"]
    assert [t.id for t in query.sort_tickets([a, b, c], "next")] == ["tic-b", "tic-a", "tic-c"]
    assert [t.id for t in query.sort_tickets([a, b, c], "created_asc")] == [
        "tic-b",
        "tic-c",
        "tic-a",
    ]
    assert [t.id for t in query.sort_tickets([a, b, c], "id")] == ["tic-a", "tic-b", "tic-c"]


def test_flat_order_groups_by_status_then_urgency_then_id():
    blocked = make_ticket("tic-a", status="blocked", priority=1)
    open_late = make_ticket("tic-z", status="open", priority=9)
    open_urgent = make_ticket("tic-m", status="open", priority=2)
    ordered = query.sort_tickets([open_late, blocked, open_urgent], "flat")
    assert [t.id for t in ordered] == ["tic-a", "tic-m", "tic-z"]


def test_apply_limit():
    rows = [1, 2, 3]
    assert query.apply_limit(rows, None) == rows
    assert query.apply_limit(rows, 2) == [1, 2]
    assert query.apply_limit(rows, 0) == []


# --- id resolution ---------------------------------------------------------


def test_wildcard_matches_is_a_case_insensitive_substring_search():
    ids = ["tic-f607", "tic-a1b2", "tic-AB12"]
    assert query.wildcard_matches(ids, "f6") == ["tic-f607"]
    assert query.wildcard_matches(ids, "tic-") == ["tic-AB12", "tic-a1b2", "tic-f607"]
    assert query.wildcard_matches(ids, "zzz") == []


def test_resolve_id_exact_beats_substring_and_missing_raises():
    ids = ["tic-a1b2", "tic-a1b2c"]
    # 'tic-a1b2' is a substring of both, but the exact match wins outright.
    assert query.resolve_id(ids, "tic-a1b2", unique=True) == "tic-a1b2"
    with pytest.raises(TicketNotFound):
        query.resolve_id(ids, "nope")


def test_resolve_id_unique_refuses_an_ambiguous_term():
    ids = ["tic-a1b2", "tic-a1b2c"]
    with pytest.raises(AmbiguousTicketId, match="ambiguous"):
        query.resolve_id(ids, "a1b2", unique=True)
    # Read-only callers keep the first alphabetical match.
    assert query.resolve_id(ids, "a1b2", unique=False) == "tic-a1b2"


def test_resolve_terms_expands_each_term_and_requires_a_match():
    ids = ["tic-f607", "tic-a1b2"]
    assert query.resolve_terms(ids, ["f6", "a1b2"]) == ["tic-a1b2", "tic-f607"]
    with pytest.raises(TicketNotFound):
        query.resolve_terms(ids, ["nope"])
