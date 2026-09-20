"""The sink conformance suite: every test here runs against every sink.

This is the contract, made executable. It is deliberately written in terms of the
interface only -- no paths, no folders, no SQL -- so a sink cannot pass by
accident of sharing code with another; the file sink and the SQLite sink have no
storage logic in common, and both must satisfy every assertion below.

Two kinds of test live here, and the distinction matters:

- **behavioral**: what a caller can rely on (a claim has exactly one winner, a
  query returns what the reference predicate selects, a note is appended in order).
- **parity**: where a sink is allowed to implement something its own way -- query
  ordering and filtering are SQL for one and Python for the other -- the result is
  compared against the storage-neutral reference implementation, not merely
  checked for plausibility.
"""

from __future__ import annotations

import pytest

from arbite.errors import AmbiguousTicketId, Conflict, TicketError, TicketNotFound
from arbite.query import TicketQuery, TextMatch, sort_tickets
from arbite.schema import parse_notes, parse_ticket
from arbite.sinks.base import Expect, filter_tickets
from helpers import make_ticket

# --- create / read ---------------------------------------------------------


def test_create_then_read_returns_the_same_ticket(sink):
    original = make_ticket("tic-a1b2", title="a title", tags=["x", "y"], depends_on=["tic-zzzz"])
    sink.create(original)
    got = sink.get("tic-a1b2")
    assert got.id == "tic-a1b2"
    assert got.title == "a title"
    assert got.tags == ["x", "y"]
    # depends_on keeps its entries (a dangling target is doctor's problem, not the
    # sink's) and its order, which is why it cannot live in a set.
    assert got.depends_on == ["tic-zzzz"]
    assert got.body == original.body


def test_body_survives_a_round_trip(sink):
    """The freeform body is not a frontmatter field, so it is the field most
    likely to be dropped by a storage mapping; a lost body is a lost ticket."""
    body = "## Description\nNormalize the retry layer.\n\n## Notes\n- 2026-01-01 a.1: hi\n"
    sink.create(make_ticket("tic-a1b2", body=body))
    assert sink.get("tic-a1b2").body == body


def test_lists_preserve_order(sink):
    sink.create(make_ticket("tic-a1b2", tags=["zeta", "alpha"], depends_on=["tic-c", "tic-a"]))
    got = sink.get("tic-a1b2")
    assert got.tags == ["zeta", "alpha"]
    assert got.depends_on == ["tic-c", "tic-a"]


def test_render_round_trips_through_parse_ticket(sink, populated):
    for ticket in populated.query(TicketQuery(buckets=("*",))):
        text = populated.render(ticket)
        reparsed = parse_ticket(text)
        assert reparsed.to_markdown() == text
        assert reparsed.id == ticket.id
        assert reparsed.body.strip() == ticket.body.strip()


def test_render_is_the_text_form_not_a_summary(sink):
    sink.create(make_ticket("tic-a1b2", title="t"))
    text = sink.render(sink.get("tic-a1b2"))
    assert text.startswith("---\n")
    assert "id: tic-a1b2" in text
    assert "## Description" in text


def test_exists_and_ids_are_sorted(sink):
    for tid in ("tic-cccc", "tic-aaaa", "tic-bbbb"):
        sink.create(make_ticket(tid))
    assert sink.ids() == ["tic-aaaa", "tic-bbbb", "tic-cccc"]
    assert sink.exists("tic-aaaa")
    assert not sink.exists("tic-9999")


def test_get_resolves_a_wildcard_term(sink):
    sink.create(make_ticket("tic-f607"))
    assert sink.get("f6").id == "tic-f607"
    assert sink.get("tic-f607").id == "tic-f607"


def test_missing_ticket_raises_that_it_is_missing(sink):
    with pytest.raises(TicketNotFound):
        sink.get("tic-9999")
    with pytest.raises(TicketNotFound):
        sink.read("tic-9999")
    with pytest.raises(TicketNotFound):
        sink.location("tic-9999")
    with pytest.raises(TicketNotFound):
        sink.remove("tic-9999")


def test_ambiguous_term_is_refused_for_mutations_but_tolerated_for_reads(sink):
    sink.create(make_ticket("tic-a1b2"))
    sink.create(make_ticket("tic-a1b2c"))
    with pytest.raises(AmbiguousTicketId):
        sink.get("a1b2", unique=True)
    assert sink.get("a1b2").id == "tic-a1b2"
    # an exact id is never ambiguous, even as a prefix of another
    assert sink.get("tic-a1b2", unique=True).id == "tic-a1b2"


def test_create_refuses_an_id_that_already_exists(sink):
    sink.create(make_ticket("tic-a1b2", title="first"))
    with pytest.raises(Conflict):
        sink.create(make_ticket("tic-a1b2", title="second"))
    # the original is untouched
    assert sink.get("tic-a1b2").title == "first"


def test_new_id_is_not_already_in_use(sink):
    seen = set()
    for _ in range(20):
        new = sink.new_id()
        assert new not in seen, "new_id must not hand out an id already in the store"
        seen.add(new)
        sink.create(make_ticket(new))
    assert len(sink.ids()) == 20


# --- update ----------------------------------------------------------------


def test_update_writes_the_change(sink):
    sink.create(make_ticket("tic-a1b2", title="before"))
    ticket = sink.get("tic-a1b2")
    ticket.title = "after"
    ticket.tags = ["new"]
    sink.update(ticket)
    reread = sink.get("tic-a1b2")
    assert reread.title == "after"
    assert reread.tags == ["new"]


def test_update_honours_a_matching_expectation(sink):
    sink.create(make_ticket("tic-a1b2"))
    ticket = sink.get("tic-a1b2")
    ticket.assignee = "claude.haiku.001"
    ticket.status = "in_progress"
    sink.update(ticket, expect=Expect.unclaimed("open"))
    assert sink.get("tic-a1b2").assignee == "claude.haiku.001"


def test_update_refuses_when_the_expectation_is_stale(sink):
    """The claim race: two agents hold the same read state, one wins, the other
    must be told rather than silently overwriting the winner."""
    sink.create(make_ticket("tic-a1b2"))
    first, second = sink.get("tic-a1b2"), sink.get("tic-a1b2")
    first.status, first.assignee = "in_progress", "agent.first"
    sink.update(first, expect=Expect.unclaimed("open"))

    second.status, second.assignee = "in_progress", "agent.second"
    with pytest.raises(Conflict):
        sink.update(second, expect=Expect.unclaimed("open"))
    assert sink.get("tic-a1b2").assignee == "agent.first"


def test_update_refuses_an_expectation_about_the_wrong_ticket(sink):
    sink.create(make_ticket("tic-a1b2"))
    ticket = sink.get("tic-a1b2")
    ticket.title = "changed"
    with pytest.raises(Conflict):
        sink.update(ticket, expect=Expect(status="closed"))


def test_a_failed_update_leaves_the_store_unchanged(sink):
    sink.create(make_ticket("tic-a1b2", title="original"))
    ticket = sink.get("tic-a1b2")
    ticket.title = "should not land"
    with pytest.raises(Conflict):
        sink.update(ticket, expect=Expect(status="closed"))
    assert sink.get("tic-a1b2").title == "original"


def test_update_a_missing_ticket_is_an_error(sink):
    with pytest.raises(TicketNotFound):
        sink.update(make_ticket("tic-9999"))


# --- notes -----------------------------------------------------------------


def test_add_note_appends_in_order_and_bumps_updated(sink):
    sink.create(make_ticket("tic-a1b2", updated="2026-01-01T00:00:00"))
    sink.add_note("tic-a1b2", "claude.haiku.001", "first")
    sink.add_note("tic-a1b2", "claude.opus.001", "second", note_date="2026-05-05T05:05:05")
    notes = sink.notes("tic-a1b2")
    assert [n.message for n in notes] == ["first", "second"]
    assert [n.agent for n in notes] == ["claude.haiku.001", "claude.opus.001"]
    assert notes[1].date == "2026-05-05T05:05:05"
    assert sink.get("tic-a1b2").updated == "2026-05-05T05:05:05"


def test_notes_agree_with_the_reference_derivation(sink, populated):
    """A sink may index notes or re-parse the body on demand; either way the answer
    must equal what the schema derives from the body, which is authoritative."""
    for ticket in populated.query(TicketQuery(buckets=("*",))):
        expected = parse_notes(sink.get(ticket.id).body)
        assert sink.notes(ticket.id) == expected


def test_notes_are_empty_for_a_body_without_a_notes_section(sink):
    sink.create(make_ticket("tic-a1b2", body="## Description\nnothing yet\n"))
    assert sink.notes("tic-a1b2") == []


def test_notes_of_a_missing_ticket_are_an_error(sink):
    with pytest.raises(TicketNotFound):
        sink.notes("tic-9999")


# --- buckets ---------------------------------------------------------------


def test_move_to_bucket_files_and_unfiles_a_ticket(sink):
    sink.create(make_ticket("tic-a1b2"))
    sink.move_to_bucket("tic-a1b2", "wishlist")
    assert sink.bucket("tic-a1b2") == "wishlist"
    assert sink.query(TicketQuery()) == []
    assert [t.id for t in sink.query(TicketQuery(buckets=("wishlist",)))] == ["tic-a1b2"]

    sink.move_to_bucket("tic-a1b2", None)
    assert sink.bucket("tic-a1b2") is None
    assert [t.id for t in sink.query(TicketQuery())] == ["tic-a1b2"]


def test_any_bucket_query_sweeps_everything(sink, populated):
    everything = populated.query(TicketQuery(buckets=("*",)))
    status_managed = populated.query(TicketQuery())
    assert {t.id for t in everything} - {t.id for t in status_managed} == {"tic-0718"}


def test_a_status_change_takes_a_ticket_out_of_a_bucket(sink):
    """Filing is not a state; a state change is, and it returns the ticket to the
    workflow. The file sink expresses that by moving the file, a database sink by
    clearing the bucket, and a caller sees the same thing either way."""
    sink.create(make_ticket("tic-a1b2", status="raw", type="feature"))
    sink.move_to_bucket("tic-a1b2", "wishlist")
    ticket = sink.get("tic-a1b2")
    ticket.status = "open"
    sink.update(ticket)
    assert sink.bucket("tic-a1b2") is None
    assert [t.id for t in sink.query(TicketQuery(status="open"))] == ["tic-a1b2"]


def test_move_to_bucket_refuses_to_escape_the_root(sink):
    sink.create(make_ticket("tic-a1b2"))
    with pytest.raises(TicketError):
        sink.move_to_bucket("tic-a1b2", "../outside")
    with pytest.raises(TicketNotFound):
        sink.move_to_bucket("tic-9999", "wishlist")


def test_bucket_is_none_for_a_status_managed_ticket(sink):
    sink.create(make_ticket("tic-a1b2"))
    assert sink.bucket("tic-a1b2") is None


# --- delete ----------------------------------------------------------------


def test_remove_deletes_the_ticket_and_its_derived_data(sink, populated):
    populated.remove("tic-c3d4")
    assert not populated.exists("tic-c3d4")
    assert "tic-c3d4" not in populated.ids()
    assert [t.id for t in populated.query(TicketQuery())].count("tic-c3d4") == 0
    with pytest.raises(TicketNotFound):
        populated.get("tic-c3d4")
    assert populated.check() == []


# --- query -----------------------------------------------------------------


def test_query_applies_every_structured_filter(sink, populated):
    def ids(q):
        return sorted(t.id for t in populated.query(q))

    assert ids(TicketQuery(status="open")) == ["tic-a1b2", "tic-b2c3"]
    assert ids(TicketQuery(status=("open", "blocked"))) == ["tic-a1b2", "tic-b2c3", "tic-d4e5"]
    assert ids(TicketQuery(status="blocked")) == ["tic-d4e5"]
    assert ids(TicketQuery(type="bug")) == ["tic-a1b2"]
    assert ids(TicketQuery(type=("bug", "refactor"))) == ["tic-a1b2", "tic-f607"]
    assert ids(TicketQuery(tier="high")) == ["tic-b2c3", "tic-d4e5"]
    assert ids(TicketQuery(domain="mesh")) == ["tic-a1b2", "tic-b2c3"]
    assert ids(TicketQuery(epic="mesh-pipeline")) == ["tic-a1b2", "tic-b2c3"]
    assert ids(TicketQuery(assignee="claude.haiku.001")) == ["tic-c3d4"]
    assert ids(TicketQuery(priority=2)) == ["tic-a1b2"]
    assert ids(TicketQuery(ids=("tic-a1b2", "tic-f607"))) == ["tic-a1b2", "tic-f607"]
    assert ids(TicketQuery(text=TextMatch("pop-in", fields=("title",)))) == ["tic-a1b2"]


def test_query_text_modes(sink, populated):
    # "pop-in" appears only in tic-a1b2's title and body, so a match here is a
    # match of the pattern rather than of a word the fixture repeats everywhere.
    assert [t.id for t in populated.query(TicketQuery(text=TextMatch("pop-in")))] == ["tic-a1b2"]
    assert [t.id for t in populated.query(TicketQuery(text=TextMatch("*POP-IN*", "wildcard")))] == [
        "tic-a1b2"
    ]
    assert [t.id for t in populated.query(TicketQuery(text=TextMatch("p.p-i.", "regex")))] == [
        "tic-a1b2"
    ]
    # text search reaches the body, not just the frontmatter
    assert [t.id for t in populated.query(TicketQuery(text=TextMatch("pops between")))] == [
        "tic-a1b2"
    ]
    # and lists, in their comma-joined form
    assert [t.id for t in populated.query(TicketQuery(text=TextMatch("lod, mesh", fields=("tags",))))] == [
        "tic-a1b2"
    ]


def test_query_with_no_match_returns_empty(sink, populated):
    assert populated.query(TicketQuery(domain="nonexistent")) == []
    assert populated.query(TicketQuery(text=TextMatch("nothing matches this"))) == []


@pytest.mark.parametrize("order", ["flat", "next", "created_asc", "id"])
def test_query_ordering_matches_the_reference(sink, populated, order):
    """SQL ordering (the SQLite sink) and Python ordering (the file sink) must
    agree with `query.sort_tickets`; a database that sorted differently would be a
    bug that only showed up in production."""
    got = [t.id for t in populated.query(TicketQuery(order=order))]
    visible = populated.query(TicketQuery())
    expected = [t.id for t in sort_tickets(visible, order)]
    assert got == expected


def test_query_limit_keeps_the_most_relevant_rows(sink, populated):
    rows = populated.query(TicketQuery(order="next", limit=2))
    assert len(rows) == 2
    assert rows == populated.query(TicketQuery(order="next"))[:2]


def test_query_with_text_and_limit_filters_before_limiting(sink):
    """A limit pushed into storage before the text filter would truncate rows that
    were going to be rejected anyway."""
    for i in range(6):
        sink.create(make_ticket(f"tic-{i:04d}", title=f"chore {i}"))
    sink.create(make_ticket("tic-9999", title="needle in a haystack"))
    rows = sink.query(TicketQuery(text=TextMatch("needle"), limit=1))
    assert [t.id for t in rows] == ["tic-9999"]


def test_query_matches_the_reference_predicate(sink, populated):
    """The storage-neutral reference (`filter_tickets`) is the answer a query must
    produce; the SQLite sink chooses SQL as its *means*, not as a second opinion."""
    queries = [
        TicketQuery(status="open"),
        TicketQuery(status=("open", "in_progress"), tier="medium"),
        TicketQuery(priority=1),
        TicketQuery(epic="mesh-pipeline", order="next"),
        TicketQuery(type="bug", text=TextMatch("LOD")),
        TicketQuery(ids=("tic-a1b2",)),
        TicketQuery(order="created_asc", limit=3),
    ]
    visible = populated.query(TicketQuery())
    for q in queries:
        assert [t.id for t in populated.query(q)] == [
            t.id for t in filter_tickets(visible, q)
        ], q


def test_query_rejects_an_unknown_order_and_bad_regex(sink, populated):
    with pytest.raises(TicketError):
        populated.query(TicketQuery(order="sideways"))
    with pytest.raises(TicketError):
        populated.query(TicketQuery(text=TextMatch("(", "regex")))


# --- integrity -------------------------------------------------------------


def test_check_is_clean_for_a_consistent_store(populated):
    assert populated.check() == []


def test_check_reports_shared_problems_in_every_sink(sink):
    """The checks that mean the same thing in any store. Sink-specific ones (a
    closed ticket with no date, say, which no sink will *write* but a hand edit or
    a crash can produce) are covered in each sink's own test module."""
    sink.create(make_ticket("tic-a1b2", type="not-a-type"))
    sink.create(make_ticket("tic-b2c3", tier="impossible"))
    sink.create(make_ticket("tic-c3d4", priority=0))
    sink.create(make_ticket("tic-d4e5", created="not-a-date"))
    sink.create(make_ticket("tic-e5f6", depends_on=["tic-gone", "tic-e5f6"]))
    sink.create(make_ticket("tic-f607", depends_on=["tic-0718"]))
    sink.create(make_ticket("tic-0718", depends_on=["tic-f607"]))
    sink.create(make_ticket("tic-1829", status="in_progress"))
    sink.create(make_ticket("tic-293a", status="blocked"))
    sink.create(make_ticket("tic-3a4b", status="open", closed="2026-01-01"))

    kinds = {p.kind for p in sink.check()}
    assert {
        "invalid_field",
        "dangling_dependency",
        "self_dependency",
        "dependency_cycle",
        "in_progress_unassigned",
        "blocked_without_reason",
        "closed_date_on_open_ticket",
    } <= kinds


def test_check_does_not_flag_todo_placeholders(sink):
    """`arbite raw` and `create --blank` write TODO placeholders on purpose;
    reporting them would leave `doctor` failing in any repo mid-triage."""
    sink.create(make_ticket("tic-a1b2", status="raw", type="feature"))
    sink.create(
        make_ticket("tic-b2c3", status="raw", title="TODO: replace", tier="TODO: low|medium")
    )
    assert sink.check() == []


def test_check_reports_a_duplicated_id_where_a_store_can_have_one(sink, kind, arbite_dir):
    """Only a file store can hold one id twice; a database sink enforces it in the
    schema. Where it *is* possible, every location is named."""
    sink.create(make_ticket("tic-a1b2", title="first"))
    if kind != "file":
        pytest.skip("this store cannot hold a duplicate id")
    from arbite.sinks.file import write_atomic

    duplicate = make_ticket("tic-a1b2", title="second")
    write_atomic(duplicate.to_markdown(), arbite_dir / "blocked" / "tic-a1b2.md")
    problems = [p for p in sink.check() if p.kind == "duplicate_id"]
    assert len(problems) == 1
    assert "tic-a1b2.md" in problems[0].detail


def test_describe_reports_capabilities_and_counts(sink, populated):
    info = sink.describe()
    assert info.kind == sink.kind
    assert info.root == str(sink.root)
    assert info.ticket_count == 6  # the bucketed ticket is out of the workflow
    assert info.status_counts["open"] == 2
    assert info.status_is_location is (sink.kind == "file")
    assert info.supports_buckets is True
    assert info.to_dict()["kind"] == sink.kind


def test_location_is_a_stable_string_that_names_the_ticket(sink, populated):
    location = populated.location("tic-a1b2")
    assert location == populated.location("tic-a1b2")
    assert "tic-a1b2" in location
    mapping = populated.location_map(populated.query(TicketQuery()))
    assert mapping["tic-a1b2"] == location


def test_storage_locations_lists_every_copy(sink, populated):
    assert populated.storage_locations("tic-a1b2") == [populated.location("tic-a1b2")]


# --- the status vocabulary --------------------------------------------------


def test_review_round_trips_validates_and_renders(sink):
    """`review` is a first-class status, so it must behave like any other one:
    create/get returns it, a status query selects exactly it, integrity checking
    is clean, and the rendered text re-parses byte-for-byte. Nothing below is
    review-specific except the value -- which is the point."""
    sink.create(make_ticket("tic-a1b2", status="review"))
    assert sink.get("tic-a1b2").status == "review"
    assert [t.id for t in sink.query(TicketQuery(status="review"))] == ["tic-a1b2"]
    assert sink.check() == []

    text = sink.render(sink.get("tic-a1b2"))
    reparsed = parse_ticket(text)
    assert reparsed.to_markdown() == text
    assert reparsed.status == "review"


# --- the references field ---------------------------------------------------


def test_references_absent_renders_no_line_and_reads_back_empty(sink):
    """A ticket with no references is byte-for-byte what it was before the field
    existed: no `references:` line at all, and a read yields an empty list."""
    sink.create(make_ticket("tic-a1b2"))
    text = sink.render(sink.get("tic-a1b2"))
    assert "references" not in text
    assert sink.get("tic-a1b2").references == []


def test_references_empty_is_rendered_as_absent(sink):
    """Absent and empty are deliberately indistinguishable: an explicit empty list
    is never re-emitted, and it reads back as an empty list."""
    sink.create(make_ticket("tic-a1b2", references=[]))
    text = sink.render(sink.get("tic-a1b2"))
    assert "references" not in text
    assert sink.get("tic-a1b2").references == []


def test_references_single_round_trips(sink):
    sink.create(make_ticket("tic-a1b2", references=["plans/review-workflow.md"]))
    got = sink.get("tic-a1b2")
    assert got.references == ["plans/review-workflow.md"]
    text = sink.render(got)
    assert parse_ticket(text).references == ["plans/review-workflow.md"]
    assert parse_ticket(text).to_markdown() == text


def test_references_multiple_preserve_order(sink):
    refs = ["plans/b.md", "plans/a.md", "plans/nested/c.md"]
    sink.create(make_ticket("tic-a1b2", references=refs))
    assert sink.get("tic-a1b2").references == refs
    text = sink.render(sink.get("tic-a1b2"))
    assert parse_ticket(text).references == refs
    assert parse_ticket(text).to_markdown() == text


def test_references_renders_immediately_after_depends_on(sink):
    """The stored position is pinned: `references` follows `depends_on`, and only
    appears at all when it is non-empty."""
    sink.create(make_ticket("tic-a1b2", depends_on=["tic-zzzz"], references=["plans/a.md"]))
    text = sink.render(sink.get("tic-a1b2"))
    assert "depends_on:\n- tic-zzzz\nreferences:\n- plans/a.md\n" in text
