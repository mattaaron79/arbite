"""The coordination store, checked against both backends.

`CoordinationStore` is a second conformance surface beside `TicketSink`, so these
tests run once per backend: the file backend keeps JSON documents under
`.arbite/coordination/`, the SQLite backend keeps them in the ticket database, and
every question the layer asks -- what is active, what is pending, what is wrong with
these records -- must have the same answer on both.

The old-store fixtures matter as much as the new ones: a store built before this
slice has no coordination state at all, and that has to read as "nothing recorded"
rather than as an error or, worse, as an empty-looking store that cannot be used.
"""

from __future__ import annotations

import json

import pytest

from arbite.coordination import records as coordination_records
from arbite.coordination.file_backend import FileCoordinationStore
from arbite.coordination.sqlite_backend import SqliteCoordinationStore
from arbite.coordination.store import CoordinationStore, open_coordination_store
from arbite.errors import CoordinationError, RecordError
from arbite.sinks import SinkSpec, build_sink

BACKENDS = ("file", "sqlite")


def make_workspace(store, root="/tmp/arbite-project") -> coordination_records.Workspace:
    return coordination_records.Workspace(
        id=coordination_records.derived_workspace_id(root, store.kind, store.root),
        root=root,
        store_kind=store.kind,
        store_root=store.root,
        coordination_kind=store.kind,
        coordination_root=store.root,
    )


def make_attempt(workspace, **overrides):
    values = dict(
        id="att-91bd",
        ticket_id="tic-cf9f",
        worker_id="claude.opus.001",
        workspace_id=workspace.id,
        generation=1,
        state="active",
        started=coordination_records.utc_now(),
        last_activity=coordination_records.utc_now(),
    )
    values.update(overrides)
    return coordination_records.WorkAttempt(**values)


def make_claim(workspace, **overrides):
    values = dict(
        id="clm-a1b2",
        workspace_id=workspace.id,
        path="src/arbite/schema.py",
        ticket_id="tic-cf9f",
        attempt_id="att-91bd",
        generation=1,
        acquired=coordination_records.utc_now(),
    )
    values.update(overrides)
    return coordination_records.FileClaim(**values)


def make_receipt(**overrides):
    values = dict(
        id="op-4f19",
        kind="write",
        paths=["src/arbite/sinks/base.py"],
        result=coordination_records.RECEIPT_SUCCEEDED,
        recorded_at=coordination_records.utc_now(),
        ticket_id="tic-1a75",
        attempt_id="att-91bd",
        before={"src/arbite/sinks/base.py": coordination_records.ABSENT},
        after={"src/arbite/sinks/base.py": "sha256:" + "2" * 64},
    )
    values.update(overrides)
    return coordination_records.OperationReceipt(**values)


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path) -> CoordinationStore:
    """One coordination store per backend, initialised as `arbite init` leaves it."""
    arbite_dir = tmp_path / ".arbite"
    arbite_dir.mkdir()
    sink = build_sink(SinkSpec(kind=request.param), arbite_dir)
    sink.init()
    coordination = open_coordination_store(sink)
    coordination.init()
    return coordination


# --- the layout ------------------------------------------------------------


def test_the_two_backends_are_what_the_sink_resolves_to(tmp_path):
    """Coordination state lives with the *store*, so it is resolved from the sink
    rather than from the project directory."""
    arbite_dir = tmp_path / ".arbite"
    arbite_dir.mkdir()
    file_sink = build_sink(SinkSpec(kind="file"), arbite_dir)
    sqlite_sink = build_sink(SinkSpec(kind="sqlite"), arbite_dir)

    assert isinstance(open_coordination_store(file_sink), FileCoordinationStore)
    assert isinstance(open_coordination_store(sqlite_sink), SqliteCoordinationStore)
    assert open_coordination_store(file_sink).root == str(arbite_dir / "coordination")
    assert open_coordination_store(sqlite_sink).root == str(arbite_dir / "arbite.db")


def test_initialising_the_layout_is_idempotent_and_never_removes_a_record(store):
    workspace = make_workspace(store)
    store.put_record(workspace)

    store.init()
    store.init()

    assert store.get_record("workspace", workspace.id) == workspace


def test_a_store_that_was_never_initialised_reads_as_empty(tmp_path):
    """The old-store fixture: a project whose store predates coordination state.
    It must answer "nothing recorded" -- not an error, and not a store that looks
    present but cannot be read."""
    arbite_dir = tmp_path / ".arbite"
    arbite_dir.mkdir()
    sink = build_sink(SinkSpec(kind="file"), arbite_dir)
    sink.init()

    coordination = open_coordination_store(sink)

    assert coordination.get_workspace() is None
    assert coordination.counts()["events"] == 0
    assert coordination.active_claims() == []
    assert coordination.record_problems() == []
    assert coordination.has_state is False


def test_a_sqlite_store_written_before_this_slice_still_reads(tmp_path):
    """A v3 database without the coordination table answers "nothing recorded" and
    does not create tables behind a read command's back."""
    arbite_dir = tmp_path / ".arbite"
    arbite_dir.mkdir()
    sink = build_sink(SinkSpec(kind="sqlite"), arbite_dir)
    sink.init()  # tickets only: the coordination DDL has not run

    coordination = open_coordination_store(sink)
    assert coordination.has_records_table() is False
    assert coordination.counts()["events"] == 0
    assert coordination.get_workspace() is None

    # ...while a write is what creates them, because `arbite init` writes through
    # this path.
    workspace = make_workspace(coordination)
    coordination.put_workspace(workspace)
    assert coordination.has_records_table() is True
    assert coordination.get_workspace() == workspace


def test_an_unreadable_record_is_reported_rather_than_skipped(tmp_path):
    """A record that cannot be parsed must fail by name: a store that silently
    counts fewer claims than it holds is the failure mode this whole design is
    arranged against."""
    arbite_dir = tmp_path / ".arbite"
    arbite_dir.mkdir()
    sink = build_sink(SinkSpec(kind="file"), arbite_dir)
    sink.init()
    coordination = open_coordination_store(sink)
    coordination.init()
    (arbite_dir / "coordination" / "claims" / "clm-a1b2.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(RecordError) as failure:
        coordination.counts()

    assert "clm-a1b2.json" in str(failure.value)


# --- records ---------------------------------------------------------------


def test_records_round_trip_and_come_back_in_a_deterministic_order(store):
    workspace = make_workspace(store)
    store.put_workspace(workspace)
    store.put_record(make_attempt(workspace))
    store.put_record(make_claim(workspace, id="clm-b2c3", path="src/arbite/errors.py"))
    store.put_record(make_claim(workspace, id="clm-a1b2", path="src/arbite/schema.py"))
    for cursor in (3, 1, 2):
        store.put_record(
            coordination_records.Event(
                id=f"evt-{cursor:04x}",
                cursor=cursor,
                kind="claim.acquired",
                recorded_at=coordination_records.utc_now(),
                category="claim",
            )
        )

    assert store.get_workspace() == workspace
    assert [claim.path for claim in store.records("claim")] == [
        "src/arbite/schema.py",
        "src/arbite/errors.py",
    ]
    # Events order by cursor, not by id or by insertion: "after this cursor" has to
    # mean something even if the clock went backwards between two writes.
    assert [event.cursor for event in store.records("event")] == [1, 2, 3]
    assert store.get_record("attempt", "att-91bd").worker_id == "claude.opus.001"


def test_an_unknown_record_type_is_refused_by_both_backends(store):
    with pytest.raises(RecordError):
        store.records("reservation")

    with pytest.raises(RecordError):
        store.get_record("reservation", "tic-a1b2")


def test_asking_for_a_record_that_is_not_there_says_so(store):
    with pytest.raises(CoordinationError) as failure:
        store.get_record("claim", "clm-ffff")

    assert "no claim record clm-ffff" in str(failure.value)


def test_writing_replaces_a_record_of_the_same_id(store):
    """Rewriting one record -- a claim released, an attempt ended -- is how state
    changes; the append-only rule is about *history*, which is events and receipts."""
    workspace = make_workspace(store)
    store.put_record(make_attempt(workspace))
    store.put_record(
        make_attempt(workspace, state="released", ended=coordination_records.utc_now())
    )

    assert len(store.records("attempt")) == 1
    assert store.active_attempts() == []


def test_an_invalid_record_never_reaches_storage(store):
    """A record is validated when it is built (and again when it is written), so a
    store cannot hold a claim no later check could resolve."""
    workspace = make_workspace(store)

    with pytest.raises(RecordError):
        make_claim(workspace, path="/etc/passwd")

    assert store.records("claim") == []


def test_an_event_that_moves_cursor_leaves_no_second_copy(store):
    """The stored filename encodes an event's cursor, so a rewrite must not leave
    the old file behind as a duplicate of one id."""
    event = coordination_records.Event(
        id="evt-0001",
        cursor=1,
        kind="claim.acquired",
        recorded_at=coordination_records.utc_now(),
        category="claim",
    )
    store.put_record(event)
    store.put_record(
        coordination_records.Event(
            id="evt-0001",
            cursor=2,
            kind="claim.released",
            recorded_at=coordination_records.utc_now(),
            category="claim",
        )
    )

    assert [stored.cursor for stored in store.records("event")] == [2]


# --- counts and derived queries -------------------------------------------


def test_counts_and_the_active_index_agree_with_the_records(store):
    workspace = make_workspace(store)
    store.put_workspace(workspace)
    store.put_record(make_attempt(workspace))
    store.put_record(
        make_attempt(
            workspace, id="att-4c81", state="released", ended=coordination_records.utc_now()
        )
    )
    store.put_record(make_claim(workspace))
    store.put_record(
        make_claim(
            workspace,
            id="clm-c3d4",
            path="src/arbite/errors.py",
            state="released",
            released=coordination_records.utc_now(),
        )
    )
    store.put_record(make_receipt())
    store.put_record(make_receipt(id="op-5a2b", result=coordination_records.RECEIPT_PENDING))

    info = store.info()

    assert (info.kind, info.root) == (store.kind, store.root)
    assert info.claims_active == 1
    assert info.claims_released == 1
    assert info.attempts_active == 1
    assert info.receipts == 2
    assert info.pending_operations == 1
    assert info.artifacts == 0
    assert [claim.id for claim in store.active_claims()] == ["clm-a1b2"]
    assert [claim.id for claim in store.claims_for_path("src/arbite/schema.py")] == ["clm-a1b2"]
    assert store.claims_for_path("src/arbite/errors.py") == []
    assert [attempt.id for attempt in store.active_attempts("tic-cf9f")] == ["att-91bd"]
    assert store.active_attempts("tic-other") == []
    assert [receipt.id for receipt in store.pending_operations()] == ["op-5a2b"]
    assert store.has_state is True


def test_the_doctor_shape_of_the_info_is_a_subset_with_the_same_numbers(store):
    """`doctor` names the backend and reports the counts it acts on; the wider
    report keeps the receipt and artifact totals for the change views."""
    info = store.info()

    doctor = info.doctor_dict()

    assert set(doctor) == {"kind", "root", "claims_active", "events", "pending_operations"}
    for key, value in doctor.items():
        assert value == info.to_dict()[key]
    assert set(info.to_dict()) > set(doctor)


def test_a_store_holds_one_workspace_binding(store):
    """A relocated root or a repointed store is a *new* workspace, so the previous
    record is replaced rather than kept -- and a store that somehow holds two is
    reported rather than silently resolved."""
    first = make_workspace(store, root="/tmp/project-one")
    store.put_workspace(first)
    second = make_workspace(store, root="/tmp/project-two")
    store.put_workspace(second)

    assert store.get_workspace() == second
    assert len(store.records("workspace")) == 1


def test_duplicate_workspace_bindings_are_reported_not_resolved(store):
    """A store holding two workspace records is ambiguous -- two roots reaching one
    store -- so it is reported rather than guessed at. Only the database backend can
    reach this state: the file backend keeps one `workspace.json`, and writing a
    second binding replaces the first."""
    if not isinstance(store, SqliteCoordinationStore):
        pytest.skip("the file backend cannot hold two workspace records: one file, one binding")

    for root in ("/tmp/project-one", "/tmp/project-two"):
        store.put_record(make_workspace(store, root=root))

    with pytest.raises(RecordError):
        store.get_workspace()
    assert [problem.kind for problem in store.record_problems()] == ["multiple_workspaces"]


# --- integrity --------------------------------------------------------------


def test_record_problems_reports_an_orphaned_claim(store):
    workspace = make_workspace(store)
    store.put_workspace(workspace)
    store.put_record(
        make_attempt(workspace, state="finished", ended=coordination_records.utc_now())
    )
    store.put_record(make_claim(workspace))

    problems = store.record_problems(["tic-cf9f"])

    assert [problem.kind for problem in problems] == ["orphaned_claim"]
    assert "not active" in problems[0].detail
    assert problems[0].ticket_id == "tic-cf9f"


def test_record_problems_reports_a_claim_that_names_no_attempt(store):
    workspace = make_workspace(store)
    store.put_workspace(workspace)
    store.put_record(make_claim(workspace))

    problems = store.record_problems()

    assert [problem.kind for problem in problems] == ["claim_without_attempt"]


def test_record_problems_reports_a_pending_operation(store):
    workspace = make_workspace(store)
    store.put_workspace(workspace)
    store.put_record(make_receipt(result=coordination_records.RECEIPT_PENDING))

    problems = store.record_problems()

    assert [problem.kind for problem in problems] == ["pending_operation"]
    assert "op-4f19 staged a write" in problems[0].detail
    assert problems[0].ticket_id == "tic-1a75"


def test_record_problems_reports_an_attempt_for_a_ticket_that_is_gone(store):
    workspace = make_workspace(store)
    store.put_workspace(workspace)
    store.put_record(make_attempt(workspace))

    problems = store.record_problems(ticket_ids=["tic-other"])

    assert [problem.kind for problem in problems] == ["attempt_without_ticket"]


def test_record_problems_reports_records_from_another_workspace(store):
    other = make_workspace(store, root="/tmp/project-two")
    store.put_workspace(make_workspace(store))
    store.put_record(
        coordination_records.FileClaim(
            id="clm-a1b2",
            workspace_id="ws-ffff",
            path="src/arbite/schema.py",
            ticket_id="tic-cf9f",
            attempt_id="att-91bd",
            generation=1,
            acquired=coordination_records.utc_now(),
        )
    )
    assert other.id != "ws-ffff"

    kinds = [problem.kind for problem in store.record_problems()]

    assert "claim_for_another_workspace" in kinds


def test_a_clean_store_reports_nothing(store):
    workspace = make_workspace(store)
    store.put_workspace(workspace)
    store.put_record(make_attempt(workspace))
    store.put_record(make_claim(workspace))
    store.put_record(make_receipt())

    assert store.record_problems(["tic-cf9f"]) == []


def test_a_duplicate_event_cursor_is_reported(store):
    for cursor, event_id in ((1, "evt-0001"), (1, "evt-0002")):
        store.put_record(
            coordination_records.Event(
                id=event_id,
                cursor=cursor,
                kind="claim.acquired",
                recorded_at=coordination_records.utc_now(),
                category="claim",
            )
        )

    problems = store.record_problems()

    assert [problem.kind for problem in problems] == ["duplicate_event_cursor"]


# --- the interfaces later slices fill in -----------------------------------


def test_the_transaction_revision_and_recovery_hooks_say_what_is_missing(store):
    """Defined now, implemented by later slices: a stub that named the ticket is
    honest, and a command cannot call one by accident."""
    for call, ticket in ((store.transaction, "tic-1a75"), (store.recover, "tic-b03b")):
        with pytest.raises(NotImplementedError) as failure:
            call()
        assert ticket in str(failure.value)

    with pytest.raises(NotImplementedError) as failure:
        store.revision("claim", "clm-a1b2")

    assert "tic-1a75" in str(failure.value)


def test_stored_documents_are_the_same_document_on_both_backends(store):
    """One serialisation, so moving coordination state between backends is
    field-for-field exact (tic-008f's round trip depends on this) and there is one
    record shape to keep versioned."""
    workspace = make_workspace(store)
    expected = make_claim(workspace)
    store.put_record(expected)

    assert store.get_record("claim", "clm-a1b2").to_dict() == expected.to_dict()

    if isinstance(store, FileCoordinationStore):
        # ...and on disk it really is that document, not a private encoding.
        document = json.loads(
            (store._root / "claims" / "clm-a1b2.json").read_text(encoding="utf-8")
        )
        assert document == expected.to_dict()


def test_the_sqlite_backend_refuses_artifact_content_by_name(store):
    """How artifact *content* is stored in a database is the change-receipt slice's
    decision (a BLOB column or a sidecar file), so this backend refuses it with the
    ticket that decides, rather than inventing an answer or raising AttributeError."""
    if isinstance(store, FileCoordinationStore):
        pytest.skip("the file backend stores artifact content: see the test above it")

    with pytest.raises(NotImplementedError) as failure:
        store.put_artifact_bytes("sha256:" + "0" * 64, b"content")

    assert "tic-7c42" in str(failure.value)
    with pytest.raises(NotImplementedError):
        store.get_artifact_bytes("sha256:" + "0" * 64)


def test_the_file_backend_stores_artifact_bytes_by_digest(tmp_path):
    """Content once by digest: the bytes file *is* the content address, which is
    what lets two receipts share a version and an edit-then-revert keep both."""
    arbite_dir = tmp_path / ".arbite"
    arbite_dir.mkdir()
    sink = build_sink(SinkSpec(kind="file"), arbite_dir)
    sink.init()
    store = open_coordination_store(sink)
    store.init()

    digest = coordination_records.digest_bytes(b"hello")
    path = store.put_artifact_bytes(digest, b"hello")

    assert path == store.artifact_path(digest)
    assert store.get_artifact_bytes(digest) == b"hello"
    assert store.get_artifact_bytes(digest) == b"hello"  # stored once, read twice
    with pytest.raises(CoordinationError):
        store.get_artifact_bytes(coordination_records.digest_bytes(b"missing"))
