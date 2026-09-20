"""File-sink behavior: the things that are true only of a folder of markdown files.

These are exactly the properties the sink abstraction was introduced to isolate:
folder-follows-status, byte-identical files, stable filenames, and the failure
modes a filesystem has and a database does not (drift, stray temp files, an
archive in the wrong month).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arbite.errors import Conflict, TicketNotFound
from arbite.query import TicketQuery
from arbite.schema import ID_PATTERN, parse_ticket
from arbite.sinks.file import (
    RAW_PROCESSED_DIR,
    RAW_SNAPSHOT_SUFFIX,
    TMP_PREFIX,
    FileSink,
    is_raw_snapshot,
    raw_snapshot_name,
    write_atomic,
)
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
    for name in ("raw", "open", "in_progress", "review", "blocked", "shelved", "closed",
                 "wishlist", "plans", "agents"):
        assert (arbite_dir / name).is_dir(), name


def test_init_creates_the_plans_bucket_and_not_planning(arbite_dir):
    """The default bucket is `plans`, a hard rename with no alias: `init` creates
    it, and a project that still has a `planning/` directory simply keeps that as
    a non-default bucket."""
    FileSink(arbite_dir).init()
    assert (arbite_dir / "plans").is_dir()
    assert not (arbite_dir / "planning").exists()


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
        "review": "review",
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
    file_sink.move_to_bucket("tic-a1b2", "plans/ideas")
    assert (arbite_dir / "plans" / "ideas" / "tic-a1b2.md").exists()
    assert file_sink.bucket("tic-a1b2") == "plans/ideas"
    assert file_sink.buckets() == ["plans/ideas"]


def test_a_bucket_ticket_is_out_of_the_status_workflow(file_sink):
    file_sink.create(make_ticket("tic-a1b2", status="raw", type="feature"))
    file_sink.move_to_bucket("tic-a1b2", "wishlist")
    assert file_sink.query(TicketQuery(status="raw")) == []
    assert [t.id for t in file_sink.query(TicketQuery(buckets=("*",)))] == ["tic-a1b2"]


def test_plans_notes_are_not_mistaken_for_tickets(file_sink, arbite_dir):
    """plans/ is documented to hold non-ticket markdown, so a file there is only
    a ticket if it is named like one."""
    (arbite_dir / "plans" / "roadmap.md").write_text("# Roadmap\n\nno frontmatter here\n")
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


def test_init_creates_the_snapshot_area(arbite_dir):
    """`raw/processed/` exists as soon as a project is initialised: `arbite promote`
    will write its audit copy there, and should not have to invent the layout."""
    FileSink(arbite_dir).init()
    assert arbite_dir.joinpath(*RAW_PROCESSED_DIR).is_dir()
    assert RAW_PROCESSED_DIR == ("raw", "processed")


def test_the_snapshot_convention_is_idempotent_and_never_ticket_shaped():
    """The convention `arbite promote` depends on: `<ticket-id>.raw.md`, a flat file
    in raw/processed/, produced by one documented helper. Reconciling the suffix is
    idempotent -- snapshotting a snapshot yields itself -- and the result can never
    be read as a second copy of the ticket, because ID_PATTERN only matches .md."""
    assert RAW_SNAPSHOT_SUFFIX == ".raw.md"
    assert raw_snapshot_name("tic-a1b2") == "tic-a1b2.raw.md"
    assert raw_snapshot_name(raw_snapshot_name("tic-a1b2")) == "tic-a1b2.raw.md"
    assert is_raw_snapshot("tic-a1b2.raw.md")
    assert not is_raw_snapshot("tic-a1b2.md")
    assert not ID_PATTERN.match(Path(raw_snapshot_name("tic-a1b2")).stem)


def test_a_snapshot_is_an_audit_copy_not_a_ticket(file_sink, arbite_dir):
    """A promoted request leaves a snapshot that keeps its *original* raw
    frontmatter, so it cannot be told from a live raw ticket by its status. It is
    excluded by path instead: if the scan saw it, the id would be duplicated and
    `fetch` would re-serve an already-promoted request forever."""
    original = make_ticket(
        "tic-a1b2",
        title="feature (raw): Requires Classification",
        status="raw",
        type="feature",
        body="## Description\nthe original request\n\n## Notes\n",
    )
    file_sink.create(make_ticket("tic-a1b2", title="Add per-mesh LOD", status="open"))
    snapshot = arbite_dir.joinpath(*RAW_PROCESSED_DIR) / raw_snapshot_name("tic-a1b2")
    snapshot.write_text(original.to_markdown())

    # Hand-written here: nothing in this sink creates a snapshot, promote will.
    assert snapshot.exists()
    assert parse_ticket(snapshot.read_text()).status == "raw"

    assert [p.name for p in file_sink._iter_files()] == ["tic-a1b2.md"]
    assert [(p.name, t.status) for p, t, _e in file_sink._scan()] == [("tic-a1b2.md", "open")]
    assert file_sink.ids() == ["tic-a1b2"]
    # No ambiguity and no duplicate: the id resolves to the live ticket alone.
    assert file_sink.get("tic-a1b2", unique=True).status == "open"
    assert file_sink.location("tic-a1b2") == str(arbite_dir / "open" / "tic-a1b2.md")
    assert file_sink.storage_locations("tic-a1b2") == [str(arbite_dir / "open" / "tic-a1b2.md")]
    # The snapshot is filed nowhere and implies no status, so it is no bucket either.
    assert file_sink._bucket_for(snapshot) is None
    assert file_sink._expected_status_for(snapshot) is None
    assert file_sink.buckets() == []

    # `list raw` / `fetch` have nothing to offer, and the one matching ticket is the
    # open one -- exactly once.
    assert file_sink.query(TicketQuery(status="raw")) == []
    assert [t.id for t in file_sink.query(TicketQuery(ids=("tic-a1b2",)))] == ["tic-a1b2"]

    # `doctor` sees no stray file, no misfiling, no unreadable file, no duplicate id.
    assert file_sink.check() == []
    assert file_sink.check(fix=True) == []


def test_everything_under_raw_processed_is_invisible_not_just_snapshots(file_sink, arbite_dir):
    """The exclusion is by path rather than by filename: a snapshot keeps raw
    frontmatter, so a name-shaped guard would be the wrong one. Whatever is dropped
    into the snapshot area stays out of the inventory, however it is named."""
    processed = arbite_dir.joinpath(*RAW_PROCESSED_DIR)
    (processed / "tic-ffff.md").write_text(make_ticket("tic-ffff", status="raw").to_markdown())
    (processed / "notes.md").write_text("# not even frontmatter\n")
    (processed / "nested").mkdir()
    (processed / "nested" / "tic-1234.md").write_text(
        make_ticket("tic-1234", status="raw").to_markdown()
    )
    file_sink.create(make_ticket("tic-a1b2"))

    assert file_sink.ids() == ["tic-a1b2"]
    assert file_sink.buckets() == []
    assert file_sink.query(TicketQuery(status="raw")) == []
    assert file_sink.check() == []
    assert file_sink.check(fix=True) == []


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
    assert "review" in details["status_dirs"]
    assert details["buckets"] == ["wishlist"]
