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

import json
import os
import sqlite3
from dataclasses import replace

import pytest

from arbite import (
    application,
    changes,
    coordination as coord,
    fileclaims,
    filemutations,
    filereads,
    lifecycle,
)
from arbite.errors import (
    AmbiguousTicketId,
    ArbiteError,
    AttemptInactive,
    Conflict,
    CoordinationConflict,
    EditSelectionError,
    FileBusy,
    StaleRead,
    StoreBindingConflict,
    TicketError,
    TicketNotFound,
    UnsupportedCoordination,
)
from arbite.query import TicketQuery, TextMatch, sort_tickets
from arbite.schema import parse_notes, parse_ticket
from arbite.sinks import SQLITE_FILENAME, SinkSpec, build_sink
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


# --- shared-directory coordination storage (C02) ---------------------------
#
# Every test below runs against both sinks, which *is* the equivalence claim: the
# file sink journals and the SQLite sink uses real transactions, and a caller
# cannot tell which one it is talking to. Records come from `arbite.coordination`
# so real, validated records exercise the storage layer rather than stubs.

ARTIFACT_ID = "art-0123456789abcdef"
OPERATION_ID = "op-0123456789abcdef"


def coordination_fixtures():
    """A workspace, its binding, an active attempt and a claim for one path."""
    now = coord.utc_now()
    workspace = coord.Workspace(
        id=coord.new_record_id("workspace"), root="/tmp/ws", created=now, updated=now
    )
    binding = coord.StoreBinding(
        id=coord.new_record_id("store_binding"),
        workspace_id=workspace.id,
        sink_kind="file",
        location="/tmp/ws/.arbite",
        bound_at=now,
    )
    workspace.bind(binding)
    attempt = coord.WorkAttempt(
        id=coord.new_record_id("work_attempt"),
        ticket_id="tic-a1b2",
        worker_id="claude.opus.001",
        workspace_id=workspace.id,
        generation=1,
        started=now,
        last_activity=now,
    )
    claim = coord.FileClaim(
        id=coord.new_record_id("file_claim"),
        workspace_id=workspace.id,
        path="src/a.py",
        ticket_id=attempt.ticket_id,
        attempt_id=attempt.id,
        generation=1,
        acquired=now,
        observed_version=coord.digest_of_text("alpha"),
    )
    return workspace, binding, attempt, claim


def an_event(category="operation", operation_id=None):
    return coord.Event(
        id=coord.new_record_id("event"),
        kind_="operation_recorded",
        category=category,
        timestamp=coord.utc_now(),
        operation_id=operation_id,
    )


def a_receipt(attempt, operation_id=OPERATION_ID):
    return coord.OperationReceipt(
        id=operation_id,
        attempt_id=attempt.id,
        ticket_id=attempt.ticket_id,
        actor="claude.opus.001",
        kind_="write",
        timestamp=coord.utc_now(),
    )


def test_coordination_is_advertised_by_both_sinks(sink, kind, arbite_dir):
    store = sink.coordination()
    assert store is not None
    assert store.contract_version() == coord.CONTRACT_VERSION
    # Merely asking for the store creates nothing: the file layout appears on
    # first use, and a legacy SQLite database gains no tables by being asked.
    if kind == "file":
        assert not (arbite_dir / "coordination").exists()
    workspace, binding, _attempt, _claim = coordination_fixtures()
    assert store.store_binding(workspace.id) is None
    assert store.bind_store(binding).id == binding.id
    assert store.store_binding(workspace.id).id == binding.id


def test_coordination_binding_is_idempotent_and_a_conflicting_store_is_refused(sink):
    store = sink.coordination()
    workspace, binding, _attempt, _claim = coordination_fixtures()
    assert store.bind_store(binding).id == binding.id
    assert store.bind_store(binding).id == binding.id  # identical -> idempotent

    with pytest.raises(StoreBindingConflict):
        store.bind_store(replace(binding, sink_kind="sqlite"))
    stored = store.store_binding(workspace.id)
    assert stored.id == binding.id and stored.sink_kind == "file"
    assert store.store_binding(coord.new_record_id("workspace")) is None


def test_coordination_records_round_trip_through_the_store(sink):
    store = sink.coordination()
    workspace, _binding, attempt, claim = coordination_fixtures()
    with store.transaction() as tx:
        tx.put(workspace)
        tx.put(attempt)
        tx.put(claim)

    with store.transaction(write=False) as tx:
        assert tx.get("workspace", workspace.id) == workspace
        assert tx.get("work_attempt", attempt.id) == attempt
        assert tx.get("file_claim", claim.id) == claim
        assert tx.get("file_claim", "clm-0123456789abcdef") is None
        assert [c.id for c in tx.find("file_claim", workspace_id=workspace.id)] == [claim.id]
        assert tx.find("file_claim", workspace_id="ws-absent") == []


def test_put_revisions_increment_and_expect_revision_is_enforced(sink):
    store = sink.coordination()
    _ws, _binding, attempt, _claim = coordination_fixtures()
    with store.transaction() as tx:
        tx.put(attempt)
        assert tx.revision_of("work_attempt", attempt.id) == 1

    with store.transaction() as tx:
        assert tx.revision_of("work_attempt", attempt.id) == 1
        attempt.last_activity = coord.utc_now()
        tx.put(attempt, expect_revision=1)
        assert tx.revision_of("work_attempt", attempt.id) == 2

    with store.transaction() as tx:
        attempt.last_activity = coord.utc_now()
        tx.put(attempt)
        assert tx.revision_of("work_attempt", attempt.id) == 3

    tx = store.transaction()
    try:
        with pytest.raises(CoordinationConflict) as excinfo:
            tx.put(attempt, expect_revision=1)
    finally:
        tx.rollback()
    details = excinfo.value.details
    assert details["kind"] == "work_attempt"
    assert details["record_id"] == attempt.id
    assert details["expected"] == 1 and details["current"] == 3
    assert excinfo.value.retryable is True

    # The refused put wrote nothing at all.
    with store.transaction(write=False) as tx:
        assert tx.revision_of("work_attempt", attempt.id) == 3


def test_commit_persists_and_rollback_discards_everything(sink):
    store = sink.coordination()
    _ws, _binding, attempt, claim = coordination_fixtures()
    committed_event = an_event()
    with store.transaction() as tx:
        tx.put(claim)
        tx.append_event(committed_event)

    doomed_event = an_event()
    tx = store.transaction()
    tx.put(attempt)
    tx.append_event(doomed_event)
    tx.rollback()

    with store.transaction(write=False) as tx:
        assert tx.get("file_claim", claim.id) == claim
        assert tx.get("work_attempt", attempt.id) is None
    assert [e.id for e in store.event_log()] == [committed_event.id]


def test_event_cursors_are_monotonic_unique_and_survive_a_fresh_store(sink, kind, arbite_dir):
    store = sink.coordination()
    events = []
    with store.transaction() as tx:
        for _ in range(3):
            events.append(tx.append_event(an_event()))
    with store.transaction() as tx:
        events.append(tx.append_event(an_event()))

    cursors = [event.cursor for event in events]
    assert all(cursor is not None for cursor in cursors)
    assert cursors == sorted(cursors)
    assert len(set(cursors)) == len(cursors)
    assert [e.cursor for e in store.event_log()] == cursors

    # A fresh store instance over the same location sees the same cursors: they
    # are stored, not re-minted per process.
    fresh = build_sink(SinkSpec(kind=kind), arbite_dir)
    assert [e.cursor for e in fresh.coordination().event_log()] == cursors


def test_event_append_deduplicates_by_id_and_by_operation_id(sink):
    store = sink.coordination()
    first = an_event(operation_id=OPERATION_ID)
    with store.transaction() as tx:
        stored = tx.append_event(first)
    assert stored.cursor == 1

    with store.transaction() as tx:
        by_operation = tx.append_event(an_event(operation_id=OPERATION_ID))
        assert by_operation.id == first.id
        assert by_operation.cursor == stored.cursor
        by_id = tx.append_event(replace(first, operation_id=None))
        assert by_id.id == first.id

    assert [e.id for e in store.event_log()] == [first.id]


def test_event_log_filters_by_category_and_cursor(sink):
    store = sink.coordination()
    with store.transaction() as tx:
        first = tx.append_event(an_event(category="read"))
        second = tx.append_event(an_event(category="operation"))
    assert [e.id for e in store.event_log(category="read")] == [first.id]
    assert [e.id for e in store.event_log(after_cursor=first.cursor)] == [second.id]


def test_put_if_absent_is_idempotent_and_deduplicates_a_retried_operation(sink):
    store = sink.coordination()
    _ws, _binding, attempt, _claim = coordination_fixtures()
    with store.transaction() as tx:
        stored, created = tx.put_if_absent(attempt)
        assert created is True and stored is attempt
        again, created_again = tx.put_if_absent(attempt)
        assert created_again is False and again is attempt

    with store.transaction() as tx:
        third, created_third = tx.put_if_absent(attempt)
        assert created_third is False
        assert third == attempt
        # Revision 1 is the proof: a second stored write would have bumped it.
        assert tx.revision_of("work_attempt", attempt.id) == 1

    # The operation-id dedup primitive, end to end: a retried receipt plus its
    # event leaves exactly one of each behind.
    receipt = a_receipt(attempt)
    with store.transaction() as tx:
        _stored, created = tx.put_if_absent(receipt)
        assert created is True
        event = tx.append_event(an_event(operation_id=receipt.id))

    with store.transaction() as tx:
        replay, created_again = tx.put_if_absent(receipt)
        assert created_again is False
        assert replay.id == receipt.id
        replayed_event = tx.append_event(an_event(operation_id=receipt.id))
        assert replayed_event.id == event.id

    assert [e.id for e in store.event_log()] == [event.id]
    with store.transaction(write=False) as tx:
        assert tx.revision_of("operation_receipt", receipt.id) == 1


def test_ticket_revisions_cas_prevents_a_lost_update_of_an_unrelated_field(sink):
    """Acceptance 1: two readers edit a DIFFERENT field and neither touches
    status/assignee, so only an explicit revision can stop the second write."""
    sink.create(make_ticket("tic-a1b2", title="original"))
    revision = sink.revision("tic-a1b2")
    assert revision == 1

    first, second = sink.get("tic-a1b2"), sink.get("tic-a1b2")
    first.title = "first wins"
    sink.update(first, expect=Expect(revision=revision))

    second.tags = ["second"]
    with pytest.raises(Conflict):
        sink.update(second, expect=Expect(revision=revision))

    reread = sink.get("tic-a1b2")
    assert reread.title == "first wins"
    assert reread.tags == []
    assert reread.assignee is None and reread.status == "open"
    assert sink.revision("tic-a1b2") == revision + 1


def test_a_plain_ticket_update_is_still_last_write_wins(sink):
    """Backward compatibility: a revision is checked only when a caller passes
    one, exactly as the unguarded (`add_note`) path is documented to behave."""
    sink.create(make_ticket("tic-a1b2", title="original"))
    first, second = sink.get("tic-a1b2"), sink.get("tic-a1b2")
    first.title = "first"
    sink.update(first)
    second.title = "second"
    sink.update(second)
    assert sink.get("tic-a1b2").title == "second"
    assert sink.revision("tic-a1b2") == 3
    sink.add_note("tic-a1b2", "claude.opus.001", "a note")
    assert sink.revision("tic-a1b2") == 4
    assert [n.message for n in sink.notes("tic-a1b2")] == ["a note"]


def test_recover_pending_is_empty_after_committed_transactions(sink):
    store = sink.coordination()
    workspace, binding, attempt, _claim = coordination_fixtures()
    store.bind_store(binding)
    with store.transaction() as tx:
        tx.put(attempt)
        tx.put(workspace)
        tx.append_event(an_event())
    assert store.recover_pending(workspace.id) == []
    assert store.recover_pending(coord.new_record_id("workspace")) == []


# --- cursor namespaces and the namespace registry (C11) --------------------
#
# Cursors are store-local, so an export or import has to name the store they came
# from. These are behavioral tests of that surface: stable, side-effect-free store
# identities, a non-creating initialisation probe, and a registry that merges
# repeated imports the same way in both sinks.

NAMESPACE_KEYS = {
    "namespace",
    "imported_at",
    "event_count",
    "cursor_map",
    "source_contract_version",
}
NAMESPACE_A = "file:/ws/.arbite"


def test_cursor_namespace_is_stable_and_identifies_the_store(sink, kind, arbite_dir, tmp_path):
    store = sink.coordination()
    namespace = store.cursor_namespace()
    assert namespace.startswith("file:" if kind == "file" else "sqlite:")
    # One location, one namespace, across store instances...
    reopened = build_sink(SinkSpec(kind=kind), arbite_dir).coordination()
    assert reopened.cursor_namespace() == namespace
    # ...a different location is a different namespace...
    elsewhere = tmp_path / "elsewhere" / ".arbite"
    other = build_sink(SinkSpec(kind=kind), elsewhere).coordination()
    assert other.cursor_namespace() != namespace
    # ...and the two sink kinds never share a namespace, even at one location.
    cross_kind = "sqlite" if kind == "file" else "file"
    cross = build_sink(SinkSpec(kind=cross_kind), arbite_dir).coordination()
    assert cross.cursor_namespace() != namespace


def test_is_initialised_is_a_side_effect_free_probe(kind, tmp_path):
    arbite_dir = tmp_path / ".arbite"
    store = build_sink(SinkSpec(kind=kind), arbite_dir).coordination()
    assert store.is_initialised() is False
    # The probe created nothing at all: no coordination dir, no database file.
    assert not arbite_dir.exists()
    store.init()
    assert store.is_initialised() is True
    # A fresh, untouched location is uninitialised again -- and still untouched.
    unused = tmp_path / "unused" / ".arbite"
    fresh = build_sink(SinkSpec(kind=kind), unused).coordination()
    assert fresh.is_initialised() is False
    assert not unused.exists()


def test_namespaces_are_empty_and_reading_creates_nothing(kind, tmp_path):
    arbite_dir = tmp_path / ".arbite"
    store = build_sink(SinkSpec(kind=kind), arbite_dir).coordination()
    assert store.namespaces() == []
    assert not arbite_dir.exists()
    store.init()
    assert store.namespaces() == []


def test_record_namespace_round_trips_merges_and_survives_a_fresh_store(sink, kind, arbite_dir):
    store = sink.coordination()
    first = store.record_namespace(
        NAMESPACE_A,
        imported_at="2026-01-01T00:00:00Z",
        event_count=3,
        cursor_map={1: 1, 2: 5},
        source_contract_version=1,
    )
    assert set(first) == NAMESPACE_KEYS
    assert first["namespace"] == NAMESPACE_A
    assert first["imported_at"] == "2026-01-01T00:00:00Z"
    assert first["event_count"] == 3
    assert first["cursor_map"] == {"1": 1, "2": 5}
    assert first["source_contract_version"] == 1
    assert store.namespaces() == [first]

    merged = store.record_namespace(
        NAMESPACE_A,
        imported_at="2026-02-02T00:00:00Z",
        event_count=4,
        cursor_map={2: 9, 3: 10},
        source_contract_version=2,
    )
    # One entry: the union of the cursor maps (the new destination wins for a
    # shared source cursor), a cumulative event count, and the freshest import.
    assert merged["event_count"] == 7
    assert merged["cursor_map"] == {"1": 1, "2": 9, "3": 10}
    assert merged["imported_at"] == "2026-02-02T00:00:00Z"
    assert merged["source_contract_version"] == 2
    assert store.namespaces() == [merged]

    fresh = build_sink(SinkSpec(kind=kind), arbite_dir).coordination()
    assert fresh.namespaces() == [merged]


def test_a_namespace_with_separators_round_trips_without_escaping_the_store(
    sink, kind, arbite_dir
):
    store = sink.coordination()
    odd = "../a weird:name /.. /b"
    entry = store.record_namespace(
        odd,
        imported_at="2026-01-01T00:00:00Z",
        event_count=1,
        cursor_map={},
        source_contract_version=1,
    )
    assert entry["namespace"] == odd  # the full namespace, not the filename
    assert store.namespaces() == [entry]

    # Two namespaces that sanitise to the same slug still get distinct entries.
    for namespace in ("a/b", "a_b"):
        store.record_namespace(
            namespace,
            imported_at="2026-01-01T00:00:00Z",
            event_count=1,
            cursor_map={},
            source_contract_version=1,
        )
    expected = {odd, "a/b", "a_b"}
    assert {e["namespace"] for e in store.namespaces()} == expected

    if kind == "file":
        namespaces_dir = arbite_dir / "coordination" / "namespaces"
        files = sorted(namespaces_dir.glob("*.json"))
        assert len(files) == 3
        for path in files:
            assert "/" not in path.name
            assert path.name not in (".", "..")
            assert path.resolve().parent == namespaces_dir.resolve()

    fresh = build_sink(SinkSpec(kind=kind), arbite_dir).coordination()
    assert {e["namespace"] for e in fresh.namespaces()} == expected


# --- ticket acquisition and lifecycle (C03) --------------------------------
#
# The lifecycle layer is storage-neutral policy, but its *effects* must be
# identical in both sinks: exactly one active attempt per ticket, terminal
# attempts that stay terminal, takeover generations, adoption of legacy
# in-progress tickets, and a hard refusal when an active file claim exists. The
# service is built the way the CLI builds it, so this also checks that the two
# stores accept the same construction.


def lifecycle_for(sink, arbite_dir):
    service = application.coordination_service_for(
        sink, root=str(arbite_dir.parent), actor=application.Actor("conformance")
    )
    return lifecycle.TicketLifecycle(service, sink)


def test_lifecycle_acquire_creates_exactly_one_active_attempt(sink, arbite_dir):
    sink.create(make_ticket("tic-a1b2", status="open"))
    ctl = lifecycle_for(sink, arbite_dir)

    result = ctl.acquire(sink.get("tic-a1b2"), worker_id="claude.opus.001")

    assert result.ticket.status == "in_progress"
    assert result.ticket.assignee == "claude.opus.001"
    assert result.attempt.generation == 1
    assert ctl.active_attempt("tic-a1b2").id == result.attempt.id
    assert ctl.next_generation("tic-a1b2") == 2


def test_lifecycle_two_acquisitions_of_one_ticket_yield_one_active_and_one_refusal(
    sink, arbite_dir
):
    sink.create(make_ticket("tic-a1b2", status="open"))
    ctl = lifecycle_for(sink, arbite_dir)
    winner = ctl.acquire(sink.get("tic-a1b2"), worker_id="w1")

    with pytest.raises(ArbiteError):  # readiness (active attempt) or CAS refusal
        ctl.acquire(sink.get("tic-a1b2"), worker_id="w2")

    active = [a for a in ctl.attempts_for("tic-a1b2") if a.is_active]
    assert [a.id for a in active] == [winner.attempt.id]


def test_lifecycle_release_and_close_end_the_attempt(sink, arbite_dir):
    sink.create(make_ticket("tic-a1b2", status="open"))
    sink.create(make_ticket("tic-b2c3", status="open"))
    ctl = lifecycle_for(sink, arbite_dir)
    released = ctl.acquire(sink.get("tic-a1b2"), worker_id="w1")
    closed = ctl.acquire(sink.get("tic-b2c3"), worker_id="w1")

    ctl.end_attempt(released.attempt, state="released")
    ctl.end_attempt(closed.attempt, state="finished")

    assert ctl.active_attempt("tic-a1b2") is None
    assert ctl.active_attempt("tic-b2c3") is None
    kinds = {e.event_kind for e in sink.coordination().event_log()}
    assert "attempt_released" in kinds and "attempt_finished" in kinds


def test_lifecycle_a_terminal_attempt_cannot_be_reused(sink, arbite_dir):
    sink.create(make_ticket("tic-a1b2", status="open"))
    ctl = lifecycle_for(sink, arbite_dir)
    result = ctl.acquire(sink.get("tic-a1b2"), worker_id="w1")
    ctl.end_attempt(result.attempt, state="released")

    with pytest.raises(AttemptInactive):
        application.require_active_attempt(result.attempt)
    with pytest.raises(AttemptInactive):
        ctl.end_attempt(result.attempt, state="released")
    # The terminal record is preserved as history, not deleted.
    assert [a.state for a in ctl.attempts_for("tic-a1b2")] == ["released"]


def test_lifecycle_takeover_increments_generation_and_starts_a_new_attempt(sink, arbite_dir):
    sink.create(make_ticket("tic-a1b2", status="open"))
    ctl = lifecycle_for(sink, arbite_dir)
    first = ctl.acquire(sink.get("tic-a1b2"), worker_id="w1")

    second = ctl.acquire(
        sink.get("tic-a1b2"), worker_id="w2", takeover=True, reason="stalled"
    )

    assert second.took_over is True
    assert second.attempt.generation == first.attempt.generation + 1
    assert [a.state for a in ctl.attempts_for("tic-a1b2")] == ["interrupted", "active"]
    assert [a.worker_id for a in ctl.attempts_for("tic-a1b2") if a.is_active] == ["w2"]


def test_lifecycle_adoption_of_a_legacy_in_progress_ticket(sink, arbite_dir):
    sink.create(make_ticket("tic-a1b2", status="in_progress", assignee="claude.haiku.001"))
    ctl = lifecycle_for(sink, arbite_dir)
    assert ctl.attempts_for("tic-a1b2") == []

    result = ctl.acquire(sink.get("tic-a1b2"), worker_id="claude.haiku.001", adopt=True)

    assert result.attempt.generation == 1
    assert result.attempt.started == result.attempt.last_activity
    assert "claude.haiku.001" in (result.attempt.handoff or "")
    started = [e for e in sink.coordination().event_log() if e.event_kind == "attempt_started"]
    assert started[-1].payload["origin"] == lifecycle.ORIGIN_ADOPTED


def test_lifecycle_transition_releases_active_file_claims(sink, arbite_dir):
    """C09 supersedes C03: ending an attempt releases its claims (not refuses)."""
    sink.create(make_ticket("tic-a1b2", status="open"))
    ctl = lifecycle_for(sink, arbite_dir)
    result = ctl.acquire(sink.get("tic-a1b2"), worker_id="w1")
    claim = coord.FileClaim(
        id=coord.new_record_id("file_claim"),
        workspace_id=ctl.coordination.workspace.id,
        path="src/a.py",
        ticket_id="tic-a1b2",
        attempt_id=result.attempt.id,
        generation=1,
        acquired=ctl.now(),
        observed_version=coord.digest_of_text("alpha"),
    )
    with sink.coordination().transaction() as tx:
        tx.put(claim)

    ctl.end_attempt(result.attempt, state="released")

    assert ctl.active_attempt("tic-a1b2") is None
    with sink.coordination().transaction(write=False) as tx:
        stored = tx.get("file_claim", claim.id)
    assert stored is not None and stored.state == "released"
    released = [
        event
        for event in sink.coordination().event_log()
        if event.event_kind == "claim_released"
        and event.payload.get("path") == "src/a.py"
    ]
    assert released, "the lifecycle cascade must record a claim_released event"


# --- exclusive file claims and explicit release (C04) -----------------------
#
# Claim sets, their all-or-nothing behaviour and explicit release are
# storage-neutral application policy, but their *effects* must be identical in
# both sinks: one active claim per path, a deterministic generation per
# acquisition, and a released token that never authorizes anything again.


def file_claim_service(sink, arbite_dir, *, ticket_id="tic-a1b2", worker="conformance"):
    """A `FileClaimService` for a bound workspace with one active attempt."""
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "a.py").write_text("alpha\n")
    (root / "src" / "b.py").write_text("beta\n")
    if not sink.exists(ticket_id):
        sink.create(make_ticket(ticket_id, status="open"))
    service = application.coordination_service_for(
        sink, root=str(root), actor=application.Actor(worker)
    )
    ctl = lifecycle.TicketLifecycle(service, sink)
    acquired = ctl.acquire(sink.get(ticket_id), worker_id=worker)
    return fileclaims.FileClaimService(service), acquired.attempt


def test_file_claim_and_release_agree_across_sinks(sink, arbite_dir):
    claims, attempt = file_claim_service(sink, arbite_dir)
    first = claims.claim(attempt, ["src/a.py", "src/b.py"])
    assert sorted(claim.path for claim in first.acquired) == ["src/a.py", "src/b.py"]
    assert all(claim.generation == 1 for claim in first.acquired)

    released = claims.release(attempt, ["src/a.py"], reason="done with a")
    assert [claim.state for claim in released.released] == ["released"]
    # The attempt is retained, and only the released path became free.
    assert [claim.path for claim in claims.active_claims(attempt_id=attempt.id)] == [
        "src/b.py"
    ]


def test_file_claim_sets_are_all_or_nothing_across_sinks(sink, arbite_dir):
    claims, attempt = file_claim_service(sink, arbite_dir)
    claims.claim(attempt, ["src/a.py"])

    sink.create(make_ticket("tic-z9y8", status="open"))
    other_service = application.coordination_service_for(
        sink, root=str(arbite_dir.parent), actor=application.Actor("other")
    )
    other_ctl = lifecycle.TicketLifecycle(other_service, sink)
    other_attempt = other_ctl.acquire(sink.get("tic-z9y8"), worker_id="other").attempt
    other_claims = fileclaims.FileClaimService(other_service)

    with pytest.raises(FileBusy):
        other_claims.claim(other_attempt, ["src/b.py", "src/a.py"])
    assert other_claims.active_claims(attempt_id=other_attempt.id) == []


# --- read observations (C06) ------------------------------------------------
#
# A read through the proxy must leave a durable observation in either store, and
# read traffic must stay in its own event category so it cannot flood ordinary
# queries. This is the sink-equivalence half of C06; the service-level read
# semantics live in tests/test_file_reads.py.

def test_read_observations_persist_and_keep_their_own_event_category(sink, arbite_dir):
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "a.py").write_text("alpha\nbeta\n")
    sink.create(make_ticket("tic-a1b2", status="open"))
    service = application.coordination_service_for(
        sink, root=str(root), actor=application.Actor("conformance")
    )
    attempt = lifecycle.TicketLifecycle(service, sink).acquire(
        sink.get("tic-a1b2"), worker_id="conformance"
    ).attempt
    reads = filereads.FileReadService(service)

    receipt = reads.read(attempt, "src/a.py")

    with service.store.transaction(write=False) as tx:
        rows = tx.find("read_observation", attempt_id=attempt.id)
    assert [row.id for row in rows] == [receipt.read_token]
    assert rows[0].digest == coord.digest_of_text("alpha\nbeta\n")
    assert rows[0].write_authorizing is False  # no claim was held
    assert coord.is_utc_timestamp(rows[0].observed_at)

    read_events = service.store.event_log(category="read")
    assert [event.operation_id for event in read_events] == [receipt.operation_id]
    assert all(event.category == "read" for event in read_events)
    assert all(
        event.category != "read"
        for event in service.store.event_log(category="operation")
    )


# --- version-checked mutations (C07) ----------------------------------------
#
# A write/edit records a receipt plus content-addressed evidence and advances the
# live claim's observed version -- which is what invalidates the read token. This
# is the sink-equivalence half of C07; the service-level write/edit semantics live
# in tests/test_file_mutations.py.


def test_write_mutation_persists_evidence_and_invalidates_the_token(sink, arbite_dir):
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "a.py").write_text("alpha\n")
    sink.create(make_ticket("tic-a1b2", status="open"))
    service = application.coordination_service_for(
        sink, root=str(root), actor=application.Actor("conformance")
    )
    attempt = lifecycle.TicketLifecycle(service, sink).acquire(
        sink.get("tic-a1b2"), worker_id="conformance"
    ).attempt
    claims = fileclaims.FileClaimService(service)
    reads = filereads.FileReadService(service, claims=claims)
    mutations = filemutations.FileMutationService(service, claims=claims, reads=reads)

    claims.claim(attempt, ["src/a.py"])
    token = reads.read(attempt, "src/a.py").read_token

    result = mutations.write(attempt, "src/a.py", b"beta\n", read_token=token)

    assert result.applied
    assert (root / "src" / "a.py").read_bytes() == b"beta\n"
    # The receipt is stored under the operation id, with both versions recorded.
    receipt = service.read_record("operation_receipt", result.operation_id)
    assert receipt.result == "ok"
    assert receipt.before["src/a.py"] == coord.digest_of_text("alpha\n")
    assert receipt.after["src/a.py"] == coord.digest_of_text("beta\n")
    # Both before and after bytes are durable, content-addressed evidence.
    assert service.store.read_artifact_bytes(coord.digest_of_text("alpha\n")) == b"alpha\n"
    assert service.store.read_artifact_bytes(coord.digest_of_text("beta\n")) == b"beta\n"
    # The token is consumed: a second write with it is refused with no bytes moved.
    assert claims.claim_for("src/a.py").observed_version == result.after["src/a.py"]
    with pytest.raises(StaleRead):
        mutations.write(attempt, "src/a.py", b"gamma\n", read_token=token)
    assert (root / "src" / "a.py").read_bytes() == b"beta\n"


def test_edit_batch_rejects_the_whole_batch_across_sinks(sink, arbite_dir):
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "a.py").write_text("one\ntwo\n")
    sink.create(make_ticket("tic-a1b2", status="open"))
    service = application.coordination_service_for(
        sink, root=str(root), actor=application.Actor("conformance")
    )
    attempt = lifecycle.TicketLifecycle(service, sink).acquire(
        sink.get("tic-a1b2"), worker_id="conformance"
    ).attempt
    claims = fileclaims.FileClaimService(service)
    reads = filereads.FileReadService(service, claims=claims)
    mutations = filemutations.FileMutationService(service, claims=claims, reads=reads)

    claims.claim(attempt, ["src/a.py"])
    token = reads.read(attempt, "src/a.py").read_token

    with pytest.raises(EditSelectionError):
        mutations.edit(
            attempt,
            "src/a.py",
            [
                filemutations.Edit(old="one", new="ONE"),
                filemutations.Edit(old="missing", new="X"),
            ],
            read_token=token,
        )
    # Nothing was applied by the valid half of the batch.
    assert (root / "src" / "a.py").read_bytes() == b"one\ntwo\n"


# ---------------------------------------------------------------------------
# remove/rename keep deleted and moved bytes as evidence and record both rename
# paths on both sinks. This is the sink-equivalence half of C08; the
# service-level remove/rename semantics (destination rules, parent creation,
# refusal reasons, interruption recovery) live in tests/test_file_remove_rename.py.
# ---------------------------------------------------------------------------


def test_remove_and_rename_persist_evidence_for_every_path(sink, arbite_dir):
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "a.py").write_text("alpha\n")
    (root / "src" / "b.py").write_text("beta\n")
    sink.create(make_ticket("tic-a1b2", status="open"))
    service = application.coordination_service_for(
        sink, root=str(root), actor=application.Actor("conformance")
    )
    attempt = lifecycle.TicketLifecycle(service, sink).acquire(
        sink.get("tic-a1b2"), worker_id="conformance"
    ).attempt
    claims = fileclaims.FileClaimService(service)
    reads = filereads.FileReadService(service, claims=claims)
    mutations = filemutations.FileMutationService(service, claims=claims, reads=reads)

    claims.claim(attempt, ["src/a.py"])
    removed = mutations.remove(
        attempt, "src/a.py", read_token=reads.read(attempt, "src/a.py").read_token
    )
    assert removed.applied and removed.receipt.result == "ok"
    assert not (root / "src" / "a.py").exists()
    # The deleted bytes are durable, content-addressed evidence on both sinks.
    assert service.store.read_artifact_bytes(coord.digest_of_text("alpha\n")) == b"alpha\n"
    # The live claim now records the absence, so the old token cannot be reused.
    assert claims.claim_for("src/a.py").observed_version == coord.ABSENT

    claims.claim(attempt, ["src/b.py"])
    moved = mutations.rename(
        attempt, "src/b.py", "src/c.py",
        read_token=reads.read(attempt, "src/b.py").read_token,
    )
    assert moved.applied and moved.receipt.result == "ok"
    assert moved.paths == ["src/b.py", "src/c.py"]
    receipt = service.read_record("operation_receipt", moved.operation_id)
    assert receipt.paths == ["src/b.py", "src/c.py"]
    assert receipt.after["src/b.py"] == coord.ABSENT
    assert receipt.after["src/c.py"] == coord.digest_of_text("beta\n")
    assert (root / "src" / "c.py").read_bytes() == b"beta\n"
    assert not (root / "src" / "b.py").exists()
    # Rename owned the destination: the claim it acquired is recorded for it.
    destination_claim = claims.claim_for("src/c.py")
    assert destination_claim is not None
    assert destination_claim.attempt_id == attempt.id
    assert destination_claim.observed_version == moved.after["src/c.py"]


# ---------------------------------------------------------------------------
# automatic change receipts / net change views (planning key C10)
#
# The parity claim here is against the receipts the application layer itself
# recorded: the storage-neutral query may not invent, drop or re-attribute an
# operation, and its net fold must agree with the recorded before/after versions
# on both sinks.
# ---------------------------------------------------------------------------


def test_change_query_folds_recorded_receipts_identically_on_every_sink(sink, arbite_dir):
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "a.py").write_text("alpha\n")
    sink.create(make_ticket("tic-a1b2", status="open"))
    service = application.coordination_service_for(
        sink, root=str(root), actor=application.Actor("conformance")
    )
    attempt = lifecycle.TicketLifecycle(service, sink).acquire(
        sink.get("tic-a1b2"), worker_id="conformance"
    ).attempt
    claims = fileclaims.FileClaimService(service)
    reads = filereads.FileReadService(service, claims=claims)
    mutations = filemutations.FileMutationService(service, claims=claims, reads=reads)

    claims.claim(attempt, ["src/a.py"])
    token = reads.read(attempt, "src/a.py").read_token
    mutations.write(attempt, "src/a.py", b"beta\n", read_token=token)
    token = reads.read(attempt, "src/a.py").read_token
    mutations.edit(
        attempt, "src/a.py", [filemutations.Edit(old="beta", new="gamma")], read_token=token
    )

    with service.store.transaction(write=False) as tx:
        receipts = [
            r
            for r in tx.find("operation_receipt", ticket_id="tic-a1b2")
            if r.operation_kind in coord.MUTATION_KINDS
        ]
    view = changes.change_view(service.store, "tic-a1b2", ticket=sink.get("tic-a1b2"))

    assert {op["operation_id"] for op in view.operations} == {r.id for r in receipts}
    assert [op["operation_kind"] for op in view.operations] == ["write", "edit"]
    assert view.touched_paths == ["src/a.py"]

    net = view.net_changes[0]
    assert net["before"] == coord.digest_of_text("alpha\n")
    assert net["after"] == coord.digest_of_text("gamma\n")
    assert net["change"] == "modified"
    assert net["before_artifact"]["verifiable"] is True
    assert net["after_artifact"]["verifiable"] is True
    # Verifiable means exactly "the bytes read back and hash to the digest".
    assert service.store.read_artifact_bytes(net["before"]) == b"alpha\n"
    assert service.store.read_artifact_bytes(net["after"]) == b"gamma\n"


# ---------------------------------------------------------------------------
# integrity inspection and maintenance (planning key C11)
# ---------------------------------------------------------------------------
#
# These probes are the raw-storage view a later `coordination_doctor` builds on,
# and the equivalence claim is stronger here than elsewhere: the two sinks store
# records in a folder of JSON files and in database rows, yet a caller must see
# the same unvalidated shape and the same honest `[]`/`False` defaults.
#
# Integrity tests prove nothing if they only feed the store well-formed data, so
# corruption is injected *directly* into the bytes/rows below -- never through the
# real API, whose strictness is part of what is being tested and is left alone.


def _sqlite_path(arbite_dir):
    return arbite_dir / SQLITE_FILENAME


def _db_connect(arbite_dir):
    conn = sqlite3.connect(str(_sqlite_path(arbite_dir)))
    conn.row_factory = sqlite3.Row
    return conn


def _overwrite_record_payload(kind, arbite_dir, record_kind, record_id, payload):
    """Replace one stored record envelope with `payload`, bypassing the API."""
    if kind == "file":
        path = arbite_dir / "coordination" / "records" / record_kind / f"{record_id}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return
    conn = _db_connect(arbite_dir)
    try:
        conn.execute(
            "UPDATE coordination_records SET payload = ? WHERE kind = ? AND record_id = ?",
            (json.dumps(payload), record_kind, record_id),
        )
        conn.commit()
    finally:
        conn.close()


def _tamper_event_cursor(kind, arbite_dir, event, new_cursor):
    """Rewrite the *payload* cursor of a stored event, keeping its identity.

    The row/file stays where the real append put it; only the encoded-envelope
    cursor changes. That is exactly the corruption `inspect_events` exists to
    surface, and it never weakens the real append path.
    """
    if kind == "file":
        path = arbite_dir / "coordination" / "events" / f"{int(event.cursor):012d}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["cursor"] = new_cursor
        path.write_text(json.dumps(payload), encoding="utf-8")
        return
    conn = _db_connect(arbite_dir)
    try:
        row = conn.execute(
            "SELECT payload FROM coordination_events WHERE cursor = ?", (int(event.cursor),)
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["cursor"] = new_cursor
        conn.execute(
            "UPDATE coordination_events SET payload = ? WHERE cursor = ?",
            (json.dumps(payload), int(event.cursor)),
        )
        conn.commit()
    finally:
        conn.close()


def test_inspect_probes_return_empty_and_create_nothing(kind, tmp_path):
    """A probe answers a question; it must never be the thing that creates a store."""
    arbite_dir = tmp_path / ".arbite"
    store = build_sink(SinkSpec(kind=kind), arbite_dir).coordination()
    assert store.inspect_records() == []
    assert store.inspect_events() == []
    assert not arbite_dir.exists()


def test_inspect_records_and_events_show_what_the_store_holds(sink):
    store = sink.coordination()
    workspace, _binding, _attempt, claim = coordination_fixtures()
    with store.transaction() as tx:
        tx.put(workspace)
        tx.put(claim)
        event = tx.append_event(an_event())

    records = {(entry["kind"], entry["record_id"]): entry for entry in store.inspect_records()}
    for record in (workspace, claim):
        entry = records[(record.kind, record.record_id)]
        assert entry["error"] is None
        assert entry["revision"] >= 1
        # The payload is the stored envelope, and its `record` half parses back.
        decoded = coord.record_from_dict(entry["payload"]["record"])
        assert (decoded.kind, decoded.record_id) == (record.kind, record.record_id)

    events = store.inspect_events()
    assert [entry["cursor"] for entry in events] == [event.cursor]
    assert events[0]["error"] is None
    assert events[0]["payload"]["event"]["id"] == event.id


def test_inspect_records_reports_a_malformed_envelope_without_raising(sink, kind, arbite_dir):
    store = sink.coordination()
    workspace, _binding, _attempt, _claim = coordination_fixtures()
    with store.transaction() as tx:
        tx.put(workspace)

    # A hand-written payload with no envelope at all: unversioned and unusable.
    _overwrite_record_payload(
        kind, arbite_dir, workspace.kind, workspace.record_id, {"hand": "written"}
    )

    entries = [e for e in store.inspect_records() if e["record_id"] == workspace.record_id]
    assert len(entries) == 1
    assert entries[0]["revision"] is None
    assert entries[0]["error"]
    assert entries[0]["payload"] == {"hand": "written"}


def test_inspect_events_keeps_duplicates_visible(sink, kind, arbite_dir):
    """A duplicated cursor is a finding to report, not something to collapse."""
    store = sink.coordination()
    with store.transaction() as tx:
        first = tx.append_event(an_event())
        second = tx.append_event(an_event())
    assert second.cursor != first.cursor

    _tamper_event_cursor(kind, arbite_dir, second, first.cursor)

    entries = store.inspect_events()
    assert len(entries) == 2
    assert [entry["cursor"] for entry in entries] == [first.cursor, first.cursor]
    assert sorted(entry["payload"]["event"]["id"] for entry in entries) == sorted(
        [first.id, second.id]
    )


def test_journal_and_index_maintenance_defaults_are_honest(sink, kind, arbite_dir):
    store = sink.coordination()
    assert store.pending_journals() == []
    assert store.replay_journals() == []
    assert store.missing_event_operation_indexes() == []
    assert store.rebuild_event_operation_index("op-does-not-exist") is False

    if kind == "sqlite":
        # The journal/index half of the surface is a statement of fact, not a gap:
        # atomic commits and a database-maintained index leave nothing to do.
        assert store.pending_journals() == []
        assert store.replay_journals() == []
        assert store.missing_event_operation_indexes() == []
        assert store.rebuild_event_operation_index("op-does-not-exist") is False
    else:
        # None of the probes created the coordination layout on the way through.
        assert not (arbite_dir / "coordination").exists()


def test_binding_location_and_marker_path_target_the_real_store(sink, kind, arbite_dir):
    store = sink.coordination()
    location = store.binding_location()
    # Exactly what `application.ensure_binding` is called with (`str(sink.root)`),
    # canonicalised, so a marker can be compared to the open store.
    assert os.path.isabs(location)
    assert location == os.path.realpath(location)
    assert location == os.path.realpath(str(sink.root))

    marker = store.binding_marker_path()
    assert marker is not None
    assert marker.endswith("workspace-binding.json")
    # The marker sits at the project's `.arbite` directory for both sinks -- the
    # file sink's root, and the SQLite database's parent directory.
    assert os.path.dirname(marker) == os.path.realpath(str(arbite_dir))


def test_check_includes_a_seeded_coordination_problem(sink, kind, arbite_dir):
    """The coordination doctor is wired into `check()`, on both sinks.

    A legacy (unversioned) record envelope is the simplest seeded coordination
    problem: the store must be initialised for the check to run at all, so this
    also proves the wiring fires once there is state to look at."""
    store = sink.coordination()
    now = coord.utc_now()
    attempt = coord.WorkAttempt(
        id=coord.new_record_id("work_attempt"),
        ticket_id="tic-a1b2",
        worker_id="conformance",
        workspace_id=coord.new_record_id("workspace"),
        generation=1,
        started=now,
        last_activity=now,
    )
    with store.transaction() as tx:
        tx.put(attempt)
    _overwrite_record_payload(kind, arbite_dir, attempt.kind, attempt.record_id, attempt.to_dict())

    kinds = {p.kind for p in sink.check(fix=False)}
    assert "coordination_legacy_record" in kinds
