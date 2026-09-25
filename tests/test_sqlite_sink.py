"""SQLite-sink behavior: the second implementation's own machinery.

Two themes run through these tests. The first is that the normalized schema is
*used*, not decorative: tags, dependencies and notes are rows, and the note rows
are a derived index that integrity checking can compare against the body. The
second is that pushdown is held to the reference: where this sink answers with SQL
and the file sink answers in Python, the answers must agree -- including in the
cases where a naive `LIKE` translation would quietly differ.
"""

from __future__ import annotations

import sqlite3

import pytest

from arbite.errors import Conflict, SinkNotInitialised, TicketNotFound
from arbite.query import TicketQuery, TextMatch, sort_tickets
from arbite.sinks.base import Expect, filter_tickets
from arbite.sinks.sqlite import SCHEMA_VERSION, SqliteSink
from helpers import make_ticket


@pytest.fixture
def db_path(arbite_dir):
    return arbite_dir / "arbite.db"


@pytest.fixture
def sqlite_sink(db_path):
    sink = SqliteSink(db_path)
    sink.init()
    return sink


def rows(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def execute(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# --- lifecycle -------------------------------------------------------------


def test_init_creates_the_schema_and_records_its_version(sqlite_sink, db_path):
    assert sqlite_sink.schema_version() == SCHEMA_VERSION
    tables = {r["name"] for r in rows(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "tickets",
        "ticket_tags",
        "ticket_deps",
        "ticket_references",
        "ticket_notes",
        "schema_version",
    } <= tables


def test_the_schema_version_records_the_current_vocabulary(sqlite_sink, db_path):
    """Version 2 recorded `review` joining `schema.STATUSES`; version 3 added the
    `references` list field and its normalized table. Neither changed the `tickets`
    columns, so no migration is written -- a fresh store states the current version
    and that is checked against the module constant."""
    assert SCHEMA_VERSION == 3
    assert sqlite_sink.schema_version() == 3
    assert rows(db_path, "SELECT version FROM schema_version") == [{"version": 3}]


def test_an_older_schema_version_is_reported_not_migrated(sqlite_sink, db_path):
    """Doctor's job here is to *report* the mismatch -- there is no migration to
    run, so `fix` has nothing to change and the store keeps saying 1."""
    execute(db_path, "UPDATE schema_version SET version = 1")
    problems = [p for p in sqlite_sink.check() if p.kind == "schema_version"]
    assert len(problems) == 1
    assert "1" in problems[0].detail and "3" in problems[0].detail
    assert sqlite_sink.check(fix=True)
    assert sqlite_sink.schema_version() == 1, "no migration is written"


def test_a_store_written_before_schema_v3_is_repaired_on_open(sqlite_sink, db_path):
    """A v1/v2 store is an arbite store missing exactly one table --
    `ticket_references` -- and every read names it, so without a repair the first
    ticket read fails with "no such table" (which is what a migration out of such a
    store, or any `list`, hits first). Opening it adds what is missing and nothing
    else: the tickets are still there, and the *version row is untouched*, so
    `doctor` goes on reporting the store as the old one it is rather than as current.
    """
    sqlite_sink.create(make_ticket("tic-a1b2"))
    execute(db_path, "DROP TABLE ticket_references")
    execute(db_path, "UPDATE schema_version SET version = 2")

    reopened = SqliteSink(db_path)
    assert reopened.ids() == ["tic-a1b2"], "the old store reads again"
    assert reopened.read("tic-a1b2").references == [], "a table that was absent reads empty"
    # The repair is the sink's own DDL, so the index declared alongside the table
    # comes back with it -- a repair that left the table unindexed would be half a fix.
    assert rows(db_path, "SELECT name FROM sqlite_master WHERE type='table' AND name='ticket_references'")
    assert rows(db_path, "SELECT name FROM sqlite_master WHERE type='index' AND name='ticket_references_ref_idx'")
    # Nothing else moved: same ticket, same recorded version.
    assert reopened.schema_version() == 2
    assert [r["id"] for r in rows(db_path, "SELECT id FROM tickets")] == ["tic-a1b2"]


def test_a_repaired_store_is_writable_not_merely_readable(sqlite_sink, db_path):
    """The repair has to leave the store *usable*, not just readable: every write
    rewrites the child rows, so a reference written after the repair is what proves
    the added table takes writes rather than only answering reads."""
    sqlite_sink.create(make_ticket("tic-a1b2"))
    execute(db_path, "DROP TABLE ticket_references")

    reopened = SqliteSink(db_path)
    ticket = reopened.read("tic-a1b2")
    ticket.references = ["plans/review-workflow.md"]
    reopened.update(ticket)

    assert reopened.read("tic-a1b2").references == ["plans/review-workflow.md"]
    assert [
        r["ref_path"] for r in rows(db_path, "SELECT ref_path FROM ticket_references")
    ] == ["plans/review-workflow.md"]


def test_init_is_idempotent_and_keeps_data(sqlite_sink, db_path):
    sqlite_sink.create(make_ticket("tic-a1b2"))
    sqlite_sink.init()
    assert sqlite_sink.ids() == ["tic-a1b2"]


def test_an_uninitialised_store_is_reported_clearly(arbite_dir):
    missing = SqliteSink(arbite_dir / "arbite.db")
    with pytest.raises(SinkNotInitialised):
        missing.ids()

    junk = arbite_dir / "junk.db"
    conn = sqlite3.connect(str(junk))
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(SinkNotInitialised):
        SqliteSink(junk).ids()


def test_journal_mode_is_wal_so_readers_do_not_block_writers(sqlite_sink):
    assert sqlite_sink.journal_mode().lower() == "wal"


def test_details_report_the_store(sqlite_sink, db_path):
    details = sqlite_sink.details()
    assert details["path"] == str(db_path)
    assert details["schema_version"] == SCHEMA_VERSION


def test_location_names_the_database_and_the_ticket(sqlite_sink, db_path):
    sqlite_sink.create(make_ticket("tic-a1b2"))
    assert sqlite_sink.location("tic-a1b2") == f"sqlite:{db_path}#tic-a1b2"


# --- the normalized schema is actually used --------------------------------


def test_body_is_stored_and_reloaded(sqlite_sink, db_path):
    """The body is not in FIELD_ORDER, so a column list derived from that alone
    silently stores empty bodies; the round-trip test in the conformance suite
    caught exactly that, and this pins it."""
    body = "## Description\nNormalize the retry layer.\n\n## Notes\n- 2026-01-01 a.1: note\n"
    sqlite_sink.create(make_ticket("tic-a1b2", body=body))
    assert rows(db_path, "SELECT body FROM tickets WHERE id = 'tic-a1b2'")[0]["body"] == body
    assert sqlite_sink.get("tic-a1b2").body == body


def test_tags_and_dependencies_become_rows_in_order(sqlite_sink, db_path):
    sqlite_sink.create(make_ticket("tic-a1b2", tags=["zeta", "alpha"], depends_on=["tic-c", "tic-a"]))
    tags = rows(db_path, "SELECT tag, ordinal FROM ticket_tags WHERE ticket_id = 'tic-a1b2' ORDER BY ordinal")
    deps = rows(db_path, "SELECT dep_id, ordinal FROM ticket_deps WHERE ticket_id = 'tic-a1b2' ORDER BY ordinal")
    assert [(t["tag"], t["ordinal"]) for t in tags] == [("zeta", 0), ("alpha", 1)]
    assert [(d["dep_id"], d["ordinal"]) for d in deps] == [("tic-c", 0), ("tic-a", 1)]


def test_references_become_rows_in_order(sqlite_sink, db_path):
    sqlite_sink.create(make_ticket("tic-a1b2", references=["plans/b.md", "plans/a.md"]))
    refs = rows(
        db_path,
        "SELECT ref_path, ordinal FROM ticket_references WHERE ticket_id = 'tic-a1b2' "
        "ORDER BY ordinal",
    )
    assert [(r["ref_path"], r["ordinal"]) for r in refs] == [("plans/b.md", 0), ("plans/a.md", 1)]


def test_an_update_rewrites_the_child_rows_rather_than_appending(sqlite_sink, db_path):
    sqlite_sink.create(
        make_ticket(
            "tic-a1b2", tags=["a", "b"], depends_on=["tic-b"], references=["plans/a.md"]
        )
    )
    ticket = sqlite_sink.get("tic-a1b2")
    ticket.tags = ["c"]
    ticket.depends_on = []
    ticket.references = ["plans/z.md"]
    sqlite_sink.update(ticket)
    assert [r["tag"] for r in rows(db_path, "SELECT tag FROM ticket_tags")] == ["c"]
    assert rows(db_path, "SELECT * FROM ticket_deps") == []
    assert [r["ref_path"] for r in rows(db_path, "SELECT ref_path FROM ticket_references")] == [
        "plans/z.md"
    ]


def test_notes_are_indexed_from_the_body(sqlite_sink, db_path):
    sqlite_sink.create(make_ticket("tic-c3d4"))
    sqlite_sink.add_note("tic-c3d4", "claude.haiku.001", "first progress update")
    sqlite_sink.add_note("tic-c3d4", "claude.opus.001", "second progress update")

    indexed = rows(
        db_path,
        "SELECT ordinal, agent, message FROM ticket_notes WHERE ticket_id = 'tic-c3d4' "
        "ORDER BY ordinal",
    )
    assert [r["message"] for r in indexed] == ["first progress update", "second progress update"]
    assert [r["agent"] for r in indexed] == ["claude.haiku.001", "claude.opus.001"]
    assert [n.ordinal for n in sqlite_sink.notes("tic-c3d4")] == [0, 1]


def test_a_closed_ticket_without_a_date_is_reported(sqlite_sink, db_path):
    """No sink will *write* this state, but a hand edit can leave it, which is the
    case `doctor` exists for."""
    sqlite_sink.create(make_ticket("tic-a1b2", status="closed", closed="2026-01-01T00:00:00"))
    execute(db_path, "UPDATE tickets SET closed = NULL WHERE id = 'tic-a1b2'")
    assert [p.kind for p in sqlite_sink.check()] == ["closed_without_date"]


def test_the_note_index_is_read_for_reads_and_repairable_from_the_body(sqlite_sink, db_path):
    """The body is authoritative and the index is derived, so a hand-broken index
    is detectable and repairable -- which is what justifies reading it at all."""
    sqlite_sink.create(make_ticket("tic-a1b2"))
    sqlite_sink.add_note("tic-a1b2", "claude.haiku.001", "a note")
    execute(db_path, "DELETE FROM ticket_notes")
    assert sqlite_sink.notes("tic-a1b2") == []

    problems = [p for p in sqlite_sink.check() if p.kind == "notes_index_drift"]
    assert len(problems) == 1
    assert "the body is authoritative" in problems[0].detail

    fixed = sqlite_sink.check(fix=True)
    assert [p.kind for p in fixed if p.fixed] == ["notes_index_drift"]
    assert [n.message for n in sqlite_sink.notes("tic-a1b2")] == ["a note"]
    assert sqlite_sink.check() == []


def test_a_body_edited_behind_the_index_is_repaired(sqlite_sink, db_path):
    sqlite_sink.create(make_ticket("tic-a1b2"))
    execute(
        db_path,
        "UPDATE tickets SET body = ? WHERE id = 'tic-a1b2'",
        ("## Description\nx\n\n## Notes\n- 2026-01-01T00:00:00 a.1: hand written\n",),
    )
    assert [p.kind for p in sqlite_sink.check()] == ["notes_index_drift"]
    sqlite_sink.check(fix=True)
    assert [n.message for n in sqlite_sink.notes("tic-a1b2")] == ["hand written"]


def test_orphaned_index_rows_are_reported_and_removed(sqlite_sink, db_path):
    sqlite_sink.create(make_ticket("tic-a1b2"))
    # A hand-edited database with foreign keys off can orphan a row.
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO ticket_tags (ticket_id, tag, ordinal) VALUES ('tic-gone', 'x', 0)")
    conn.commit()
    conn.close()

    assert [p.kind for p in sqlite_sink.check()] == ["orphan_index_rows"]
    sqlite_sink.check(fix=True)
    assert rows(db_path, "SELECT * FROM ticket_tags WHERE ticket_id = 'tic-gone'") == []
    assert sqlite_sink.check() == []


def test_removing_a_ticket_cascades_to_its_rows(sqlite_sink, db_path):
    sqlite_sink.create(
        make_ticket("tic-a1b2", tags=["x"], depends_on=["tic-b"], references=["plans/a.md"])
    )
    sqlite_sink.add_note("tic-a1b2", "claude.haiku.001", "note")
    sqlite_sink.remove("tic-a1b2")
    for table in ("ticket_tags", "ticket_deps", "ticket_references", "ticket_notes"):
        assert rows(db_path, f"SELECT * FROM {table}") == []


# --- compare-and-swap ------------------------------------------------------


def test_a_conflict_rolls_the_whole_write_back(sqlite_sink, db_path):
    """The expectation is checked inside the writing transaction, so a stale write
    cannot leave half its changes behind."""
    sqlite_sink.create(make_ticket("tic-a1b2", tags=["original"]))
    stale = sqlite_sink.get("tic-a1b2")
    stale.title = "should not land"
    stale.tags = ["changed"]
    with pytest.raises(Conflict):
        sqlite_sink.update(stale, expect=Expect(status="closed"))

    stored = sqlite_sink.get("tic-a1b2")
    assert stored.title != "should not land"
    assert stored.tags == ["original"]
    assert [r["tag"] for r in rows(db_path, "SELECT tag FROM ticket_tags")] == ["original"]


def test_a_status_change_clears_the_bucket_column(sqlite_sink, db_path):
    sqlite_sink.create(make_ticket("tic-a1b2", status="raw", type="feature"))
    sqlite_sink.move_to_bucket("tic-a1b2", "wishlist")
    assert sqlite_sink.bucket("tic-a1b2") == "wishlist"

    ticket = sqlite_sink.get("tic-a1b2")
    ticket.status = "open"
    sqlite_sink.update(ticket)
    assert sqlite_sink.bucket("tic-a1b2") is None
    assert rows(db_path, "SELECT bucket FROM tickets WHERE id = 'tic-a1b2'")[0]["bucket"] is None


def test_a_same_status_update_keeps_the_bucket(sqlite_sink):
    sqlite_sink.create(make_ticket("tic-a1b2", status="raw", type="feature"))
    sqlite_sink.move_to_bucket("tic-a1b2", "wishlist")
    ticket = sqlite_sink.get("tic-a1b2")
    ticket.title = "renamed in the wishlist"
    sqlite_sink.update(ticket)
    assert sqlite_sink.bucket("tic-a1b2") == "wishlist"


def test_move_to_bucket_normalizes_the_path_and_refuses_escapes(sqlite_sink):
    sqlite_sink.create(make_ticket("tic-a1b2"))
    sqlite_sink.move_to_bucket("tic-a1b2", "/planning/./ideas/")
    assert sqlite_sink.bucket("tic-a1b2") == "planning/ideas"
    from arbite.errors import TicketError

    with pytest.raises(TicketError):
        sqlite_sink.move_to_bucket("tic-a1b2", "planning/../..")


# --- pushdown parity -------------------------------------------------------


def test_sql_ordering_agrees_with_the_reference_for_random_data(sqlite_sink):
    import random

    rnd = random.Random(7)
    statuses = ["open", "in_progress", "blocked", "shelved", "closed"]
    for i in range(30):
        status = rnd.choice(statuses)
        sqlite_sink.create(
            make_ticket(
                f"tic-{i:04x}",
                status=status,
                priority=rnd.choice([None, None, 1, 2, 3, 10]),
                created=f"2026-{rnd.randint(1, 6):02d}-{rnd.randint(1, 28):02d}T00:00:00",
                closed="2026-02-01T00:00:00" if status == "closed" else None,
            )
        )
    visible = sqlite_sink.query(TicketQuery())
    for order in ("flat", "next", "created_asc", "id"):
        assert [t.id for t in sqlite_sink.query(TicketQuery(order=order))] == [
            t.id for t in sort_tickets(visible, order)
        ], order


def test_sql_filtering_agrees_with_the_reference_for_random_queries(sqlite_sink):
    import random

    rnd = random.Random(11)
    statuses = ["open", "in_progress", "blocked", "closed"]
    tiers = ["low", "medium", "high", "frontier"]
    domains = ["mesh", "ui", "io"]
    for i in range(30):
        status = rnd.choice(statuses)
        sqlite_sink.create(
            make_ticket(
                f"tic-{i:04x}",
                title=f"ticket {i} " + rnd.choice(["lod", "pop-in", "glow"]),
                status=status,
                type=rnd.choice(["bug", "feature", "chore"]),
                tier=rnd.choice(tiers),
                domain=rnd.choice(domains),
                priority=rnd.choice([None, 1, 2, 5]),
                created="2026-01-01T00:00:00",
                closed="2026-02-01T00:00:00" if status == "closed" else None,
            )
        )
    visible = sqlite_sink.query(TicketQuery())
    for _ in range(40):
        q = TicketQuery(
            status=rnd.choice([(), ("open",), ("open", "closed")]),
            type=rnd.choice([(), ("bug",)]),
            tier=rnd.choice([None, rnd.choice(tiers)]),
            domain=rnd.choice([None, rnd.choice(domains)]),
            priority=rnd.choice([None, 1, 2, 5]),
            text=rnd.choice([None, TextMatch("lod"), TextMatch("*pop*", "wildcard")]),
            order=rnd.choice(["flat", "next", "id"]),
        )
        assert [t.id for t in sqlite_sink.query(q)] == [
            t.id for t in filter_tickets(visible, q)
        ], q


def test_text_search_is_not_like_based(sqlite_sink):
    """SQLite's LIKE folds case in ASCII only, while the reference matcher uses
    Python's str.lower -- which folds the Kelvin sign (U+212A) to 'k'. A LIKE
    pre-filter would have dropped this row, so the sink does not use one."""
    sqlite_sink.create(make_ticket("tic-a1b2", title="\u212aelvin scale"))
    assert "k" not in "\u212aelvin"
    assert [t.id for t in sqlite_sink.query(TicketQuery(text=TextMatch("kelvin")))] == ["tic-a1b2"]
    assert [t.id for t in sqlite_sink.query(TicketQuery(text=TextMatch("KELVIN")))] == ["tic-a1b2"]


def test_regex_and_wildcard_searches_reach_the_body(sqlite_sink):
    sqlite_sink.create(make_ticket("tic-a1b2", body="## Description\nring buffer overflow\n"))
    assert [t.id for t in sqlite_sink.query(TicketQuery(text=TextMatch("buf.*over", "regex")))] == [
        "tic-a1b2"
    ]
    assert [t.id for t in sqlite_sink.query(TicketQuery(text=TextMatch("*overflow*", "wildcard")))] == [
        "tic-a1b2"
    ]


def test_unknown_ticket_lookups_are_reported(sqlite_sink):
    with pytest.raises(TicketNotFound):
        sqlite_sink.bucket("tic-9999")
    with pytest.raises(TicketNotFound):
        sqlite_sink.notes("tic-9999")
    with pytest.raises(TicketNotFound):
        sqlite_sink.move_to_bucket("tic-9999", "wishlist")
