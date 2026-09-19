"""File-sink behavior: the things that are true only of a folder of markdown files.

These are exactly the properties the sink abstraction was introduced to isolate:
folder-follows-status, byte-identical files, stable filenames, and the failure
modes a filesystem has and a database does not (drift, stray temp files, an
archive in the wrong month).
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from arbite import coordination as coord
from arbite.coordination_storage import CRASH_AFTER_JOURNAL
from arbite.errors import Conflict, TicketNotFound
from arbite.query import TicketQuery
from arbite.schema import parse_ticket
from arbite.sinks.coordination_file import FileCoordinationStore
from arbite.sinks.file import TMP_PREFIX, FileSink, write_atomic
from helpers import make_ticket


@pytest.fixture
def file_sink(arbite_dir):
    """A file sink specifically, so these tests are not parametrized: they are
    about the file sink's own behavior."""
    sink = FileSink(arbite_dir)
    sink.init()
    return sink


def test_init_creates_the_documented_layout(arbite_dir):
    sink = FileSink(arbite_dir)
    sink.init()
    for name in ("raw", "open", "in_progress", "blocked", "shelved", "closed", "wishlist",
                 "planning", "agents"):
        assert (arbite_dir / name).is_dir(), name


def test_init_is_idempotent_and_keeps_existing_tickets(file_sink, arbite_dir):
    file_sink.create(make_ticket("tic-a1b2"))
    file_sink.init()
    assert (arbite_dir / "open" / "tic-a1b2.md").exists()


def test_create_writes_byte_identical_markdown(file_sink, arbite_dir):
    """The file content is the ticket's canonical text, not a rendering of it: this
    is what makes `arbite show`, `git log --follow` and a migrate round trip agree."""
    ticket = make_ticket("tic-a1b2", title="Fix LOD pop-in", tags=["lod"])
    file_sink.create(ticket)
    path = arbite_dir / "open" / "tic-a1b2.md"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == ticket.to_markdown()
    assert parse_ticket(path.read_text(encoding="utf-8")).to_markdown() == ticket.to_markdown()


def test_status_decides_the_folder(file_sink, arbite_dir):
    cases = {
        "raw": "raw",
        "open": "open",
        "in_progress": "in_progress",
        "blocked": "blocked",
        "shelved": "shelved",
    }
    for status, folder in cases.items():
        ticket = make_ticket("tic-0000", status=status, assignee="agent" if status == "in_progress" else None, blocked_by="x" if status == "blocked" else None)
        ticket.id = f"tic-{status[:4]}"
        file_sink.create(ticket)
        assert (arbite_dir / folder / f"{ticket.id}.md").exists(), status


def test_closed_archives_by_close_month(file_sink, arbite_dir):
    file_sink.create(make_ticket("tic-a1b2", status="closed", closed="2026-03-02T10:00:00"))
    assert (arbite_dir / "closed" / "2026-03" / "tic-a1b2.md").exists()


def test_a_status_change_moves_the_file_and_keeps_its_name(file_sink, arbite_dir):
    file_sink.create(make_ticket("tic-a1b2"))
    ticket = file_sink.get("tic-a1b2")
    ticket.status, ticket.assignee = "in_progress", "claude.haiku.001"
    file_sink.update(ticket)
    assert not (arbite_dir / "open" / "tic-a1b2.md").exists()
    assert (arbite_dir / "in_progress" / "tic-a1b2.md").exists()

    ticket = file_sink.get("tic-a1b2")
    ticket.status, ticket.closed = "closed", "2026-04-01T09:00:00"
    file_sink.update(ticket)
    # same filename in every folder: git log --follow traces the lifecycle
    assert (arbite_dir / "closed" / "2026-04" / "tic-a1b2.md").exists()
    assert file_sink.get("tic-a1b2").status == "closed"


def test_a_claim_leaves_exactly_one_file_when_two_agents_race(file_sink, arbite_dir):
    file_sink.create(make_ticket("tic-a1b2"))
    first, second = file_sink.get("tic-a1b2"), file_sink.get("tic-a1b2")
    for ticket, agent in ((first, "agent.first"), (second, "agent.second")):
        ticket.status, ticket.assignee = "in_progress", agent
    from arbite.sinks.base import Expect

    file_sink.update(first, expect=Expect.unclaimed("open"))
    with pytest.raises(Conflict):
        file_sink.update(second, expect=Expect.unclaimed("open"))
    assert file_sink.get("tic-a1b2").assignee == "agent.first"
    assert len(list(arbite_dir.rglob("tic-a1b2.md"))) == 1


def test_an_unconditional_move_does_not_have_to_win_a_race(file_sink, arbite_dir):
    """Only a compared update needs the exclusive create; closing a ticket does not."""
    file_sink.create(make_ticket("tic-a1b2"))
    ticket = file_sink.get("tic-a1b2")
    ticket.status, ticket.closed = "closed", "2026-05-01T00:00:00"
    file_sink.update(ticket)
    assert len(list(arbite_dir.rglob("tic-a1b2.md"))) == 1
    assert not list(arbite_dir.glob(f"{TMP_PREFIX}*"))


def test_buckets_are_folders_and_nested_buckets_are_allowed(file_sink, arbite_dir):
    file_sink.create(make_ticket("tic-a1b2"))
    file_sink.move_to_bucket("tic-a1b2", "planning/ideas")
    assert (arbite_dir / "planning" / "ideas" / "tic-a1b2.md").exists()
    assert file_sink.bucket("tic-a1b2") == "planning/ideas"
    assert file_sink.buckets() == ["planning/ideas"]


def test_a_bucket_ticket_is_out_of_the_status_workflow(file_sink):
    file_sink.create(make_ticket("tic-a1b2", status="raw", type="feature"))
    file_sink.move_to_bucket("tic-a1b2", "wishlist")
    assert file_sink.query(TicketQuery(status="raw")) == []
    assert [t.id for t in file_sink.query(TicketQuery(buckets=("*",)))] == ["tic-a1b2"]


def test_planning_notes_are_not_mistaken_for_tickets(file_sink, arbite_dir):
    """planning/ is documented to hold non-ticket markdown, so a file there is only
    a ticket if it is named like one."""
    (arbite_dir / "planning" / "roadmap.md").write_text("# Roadmap\n\nno frontmatter here\n")
    file_sink.create(make_ticket("tic-a1b2"))
    assert file_sink.ids() == ["tic-a1b2"]
    assert file_sink.check() == []


def test_agents_dir_and_agents_md_are_ignored(file_sink, arbite_dir):
    (arbite_dir / "agents").mkdir(exist_ok=True)
    (arbite_dir / "agents" / "claude.haiku.001.md").write_text("# scratchpad\n")
    (arbite_dir / "AGENTS.md").write_text("# not a ticket\n")
    file_sink.create(make_ticket("tic-a1b2"))
    assert file_sink.ids() == ["tic-a1b2"]
    assert file_sink.check() == []


def test_status_drift_is_reported_and_the_folder_wins(file_sink, arbite_dir):
    file_sink.create(make_ticket("tic-a1b2"))
    (arbite_dir / "open" / "tic-a1b2.md").rename(arbite_dir / "shelved" / "tic-a1b2.md")

    problems = file_sink.check()
    assert [p.kind for p in problems] == ["status_drift"]
    assert "folder is source of truth" in problems[0].detail
    assert file_sink.get("tic-a1b2").status == "open"  # untouched without --fix

    fixed = file_sink.check(fix=True)
    assert fixed[0].fixed
    assert file_sink.get("tic-a1b2").status == "shelved"
    assert file_sink.check() == []


def test_a_ticket_left_in_the_root_is_reported_and_refiled(file_sink, arbite_dir):
    file_sink.create(make_ticket("tic-a1b2"))
    loose = arbite_dir / "tic-a1b2.md"
    (arbite_dir / "open" / "tic-a1b2.md").rename(loose)

    assert [p.kind for p in file_sink.check()] == ["stray_file"]
    assert [p.kind for p in file_sink.check(fix=True)] == ["stray_file"]
    assert not loose.exists()
    assert (arbite_dir / "open" / "tic-a1b2.md").exists()


def test_a_closed_ticket_in_the_wrong_month_is_moved(file_sink, arbite_dir):
    file_sink.create(make_ticket("tic-a1b2", status="closed", closed="2026-03-02T10:00:00"))
    wrong = arbite_dir / "closed" / "2026-01" / "tic-a1b2.md"
    wrong.parent.mkdir()
    (arbite_dir / "closed" / "2026-03" / "tic-a1b2.md").rename(wrong)

    problems = file_sink.check()
    assert [p.kind for p in problems] == ["wrong_archive_month"]
    assert "expected closed/2026-03/" in problems[0].detail

    assert file_sink.check(fix=True)[0].fixed
    assert (arbite_dir / "closed" / "2026-03" / "tic-a1b2.md").exists()
    assert not wrong.exists()


def test_a_closed_ticket_without_a_date_is_reported(file_sink, arbite_dir):
    """No command writes this state, but a hand edit or a crash mid-write can."""
    file_sink.create(make_ticket("tic-a1b2", status="closed", closed="2026-01-01T00:00:00"))
    path = arbite_dir / "closed" / "2026-01" / "tic-a1b2.md"
    # The value is quoted by PyYAML (it round-trips as text, not as a date), so
    # the line is rewritten wholesale rather than by string replacement.
    text = path.read_text()
    path.write_text(
        "\n".join("closed: null" if line.startswith("closed:") else line for line in text.splitlines())
        + "\n"
    )
    assert [p.kind for p in file_sink.check()] == ["closed_without_date"]


def test_an_unreadable_ticket_in_a_status_folder_is_reported(file_sink, arbite_dir):
    (arbite_dir / "open" / "tic-broken.md").write_text("no frontmatter at all\n")
    problems = file_sink.check()
    assert [p.kind for p in problems] == ["unreadable"]
    assert "missing YAML frontmatter" in problems[0].detail


def test_a_leftover_temp_file_is_reported_and_never_read_as_a_ticket(file_sink, arbite_dir):
    file_sink.create(make_ticket("tic-a1b2"))
    stranded = arbite_dir / "open" / f"{TMP_PREFIX}abc123"
    stranded.write_text((arbite_dir / "open" / "tic-a1b2.md").read_text())

    assert file_sink.ids() == ["tic-a1b2"]
    problems = file_sink.check()
    assert [p.kind for p in problems] == ["stray_temp_file"]
    assert "holds the full ticket content" in problems[0].detail


def test_remove_unlinks_the_file(file_sink, arbite_dir):
    file_sink.create(make_ticket("tic-a1b2"))
    file_sink.remove("tic-a1b2")
    assert not (arbite_dir / "open" / "tic-a1b2.md").exists()
    with pytest.raises(TicketNotFound):
        file_sink.get("tic-a1b2")


def test_a_duplicated_id_is_refused_rather_than_guessed(file_sink, arbite_dir):
    file_sink.create(make_ticket("tic-a1b2", title="first"))
    duplicate = make_ticket("tic-a1b2", title="second")
    write_atomic(duplicate.to_markdown(), arbite_dir / "blocked" / "tic-a1b2.md")

    with pytest.raises(Conflict):
        file_sink.get("tic-a1b2", unique=True)
    with pytest.raises(Conflict):
        file_sink.read("tic-a1b2")
    problems = [p for p in file_sink.check() if p.kind == "duplicate_id"]
    assert len(problems) == 1
    assert "tic-a1b2.md" in problems[0].detail
    assert len(file_sink.storage_locations("tic-a1b2")) == 2


def test_details_describe_the_layout(file_sink, arbite_dir):
    file_sink.create(make_ticket("tic-a1b2"))
    file_sink.move_to_bucket("tic-a1b2", "wishlist")
    details = file_sink.details()
    assert details["closed_dir"] == "closed"
    assert "in_progress" in details["status_dirs"]
    assert details["buckets"] == ["wishlist"]


# ---------------------------------------------------------------------------
# coordination-store integrity inspection (C11)
# ---------------------------------------------------------------------------
#
# The write-ahead journal and the derived per-operation event index are the two
# pieces of state only this sink has, so the crash-injection and index-repair
# tests belong here rather than in the both-sink conformance suite. They use the
# documented `crash_point` hook and the existing crash-injection style (a spawned
# process that exits at a commit boundary), so the leftover state under test is
# the real thing, not a hand-rolled approximation.

OPERATION_ID = "op-0123456789abcdef"


def _a_claim(claim_id: str):
    now = coord.utc_now()
    return coord.FileClaim(
        id=claim_id,
        workspace_id="ws-0000000000000000",
        path="src/a.py",
        ticket_id="tic-a1b2",
        attempt_id=coord.new_record_id("work_attempt"),
        generation=1,
        acquired=now,
        observed_version=coord.digest_of_text("alpha"),
    )


def _an_event_with_operation(operation_id: str):
    return coord.Event(
        id=coord.new_record_id("event"),
        kind_="operation_recorded",
        category="operation",
        timestamp=coord.utc_now(),
        operation_id=operation_id,
    )


def _crash_with_a_leftover_journal(root: str, claim_id: str) -> None:
    """Write a journal with the documented hook, then die before applying it."""
    store = FileCoordinationStore(Path(root), crash_point=CRASH_AFTER_JOURNAL)
    with store.transaction() as tx:
        tx.put(_a_claim(claim_id))
    os._exit(0)  # pragma: no cover - the crash point exits first


def test_pending_journals_names_a_crash_leftover_and_replay_applies_it(arbite_dir):
    coordination_dir = arbite_dir / "coordination"
    claim_id = coord.new_record_id("file_claim")
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_with_a_leftover_journal, args=(str(coordination_dir), claim_id)
    )
    process.start()
    process.join(60)
    assert process.exitcode == 97

    store = FileCoordinationStore(coordination_dir)

    # Inspection names the leftover intent without replaying it.
    pending = store.pending_journals()
    assert len(pending) == 1
    assert pending[0].startswith("txn-")

    # The explicit replay applies it forward; the journal is gone afterwards.
    replayed = store.replay_journals()
    assert replayed == pending
    assert store.pending_journals() == []

    with store.transaction(write=False) as tx:
        stored = tx.get("file_claim", claim_id)
    assert stored is not None and stored.path == "src/a.py"


def test_replay_journals_reports_but_preserves_an_undecodable_journal(arbite_dir):
    """An undecodable journal is preserved, never guessed at. Replay returns its
    operation id because it was *present*, and it stays pending afterwards -- that
    is how a caller tells "encountered" from "applied"."""
    coordination_dir = arbite_dir / "coordination"
    journal_dir = coordination_dir / "journal"
    journal_dir.mkdir(parents=True)
    bad = journal_dir / "txn-undecodable.json"
    bad.write_text("this is not a journal\n", encoding="utf-8")

    store = FileCoordinationStore(coordination_dir)
    assert store.pending_journals() == ["txn-undecodable"]

    assert store.replay_journals() == ["txn-undecodable"]
    assert store.pending_journals() == ["txn-undecodable"]
    assert bad.exists()


def test_missing_event_operation_index_is_reported_and_rebuilt(file_sink, arbite_dir):
    store = file_sink.coordination()
    with store.transaction() as tx:
        event = tx.append_event(_an_event_with_operation(OPERATION_ID))

    index_path = (
        arbite_dir / "coordination" / "events_by_operation" / f"{OPERATION_ID}.json"
    )
    assert index_path.exists(), "the real append path must have written the index"

    # Losing the DERIVED index is reported, and rebuilt from the stored event.
    index_path.unlink()
    assert store.missing_event_operation_indexes() == [OPERATION_ID]
    assert store.rebuild_event_operation_index(OPERATION_ID) is True
    assert index_path.exists()
    assert store.missing_event_operation_indexes() == []
    assert store.rebuild_event_operation_index(OPERATION_ID) is False  # already there

    # The retry-dedup path works again: a fresh event for the same operation id
    # resolves to the original instead of appending a second one.
    with store.transaction() as tx:
        replayed = tx.append_event(_an_event_with_operation(OPERATION_ID))
    assert replayed.id == event.id
    assert replayed.cursor == event.cursor
    assert [e.id for e in store.event_log()] == [event.id]
