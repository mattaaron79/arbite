"""Transactions, revisions and the event cursor, checked against both backends.

`CoordinationStore` is a conformance surface, so everything here runs once per
backend unless a test says otherwise: the file backend commits through a journal
plus a process lock, SQLite through a real transaction, and a caller must not be
able to tell which one it is talking to.

The acceptance these tests carry, in the ticket's words: concurrent updates cannot
lose fields under an unchanged status and assignee; related state and events commit
together or recover deterministically; retrying an operation id deduplicates; and
both sinks produce equivalent outcomes.
"""

from __future__ import annotations

import json

import pytest

from arbite.coordination import records as coordination_records
from arbite.coordination.file_backend import FileCoordinationStore
from arbite.coordination.store import open_coordination_store
from arbite.errors import CoordinationError, RecordError, Stale
from arbite.sinks import SinkSpec, build_sink

BACKENDS = ("file", "sqlite")

#: A fixed time for records that two stores will be compared over: the streams have
#: to be byte-identical apart from identifiers, or "the same observable state" is
#: not a comparison anybody can make.
T0 = "2026-09-21T13:12:04Z"


def make_store(root, kind: str = "file"):
    arbite_dir = root / ".arbite"
    arbite_dir.mkdir(parents=True, exist_ok=True)
    sink = build_sink(SinkSpec(kind=kind), arbite_dir)
    sink.init()
    store = open_coordination_store(sink)
    store.init()
    return store


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    return make_store(tmp_path, request.param)


def attempt(store, **overrides):
    values = dict(
        id="att-91bd",
        ticket_id="tic-cf9f",
        worker_id="claude.opus.001",
        workspace_id="ws-7c41",
        generation=1,
        state="active",
        started=T0,
        last_activity=T0,
    )
    values.update(overrides)
    return coordination_records.WorkAttempt(**values)


def claim(store, **overrides):
    values = dict(
        id="clm-a1b2",
        workspace_id="ws-7c41",
        path="src/arbite/schema.py",
        ticket_id="tic-cf9f",
        attempt_id="att-91bd",
        generation=1,
        acquired=T0,
    )
    values.update(overrides)
    return coordination_records.FileClaim(**values)


def receipt(operation_id, result=coordination_records.RECEIPT_SUCCEEDED, **overrides):
    values = dict(
        id=operation_id,
        kind="write",
        paths=["src/arbite/schema.py"],
        result=result,
        recorded_at=T0,
        ticket_id="tic-cf9f",
        attempt_id="att-91bd",
        before={"src/arbite/schema.py": coordination_records.ABSENT},
        after={"src/arbite/schema.py": "sha256:" + "2" * 64},
    )
    values.update(overrides)
    return coordination_records.OperationReceipt(**values)


# --- committing -------------------------------------------------------------


def test_a_unit_of_work_commits_everything_it_buffered(store):
    """Several records and the event that describes them arrive together, and the
    event is given the store's next cursor rather than a number the caller chose."""
    with store.transaction() as txn:
        txn.put_record(attempt(store))
        txn.put_record(claim(store))
        event = txn.append_event(
            "claim.acquired",
            "claim",
            subject="src/arbite/schema.py",
            result="gen 1",
            ticket_id="tic-cf9f",
            attempt_id="att-91bd",
            actor="claude.opus.001",
            operation_id="op-4f19",
        )

    assert event.cursor == 1
    assert [stored.id for stored in store.records("attempt")] == ["att-91bd"]
    assert [stored.id for stored in store.records("claim")] == ["clm-a1b2"]
    assert [stored.cursor for stored in store.events()] == [1]
    assert store.info().events == 1


def test_reads_inside_a_transaction_see_its_own_writes(store):
    """An operation can build a state change and read it back: that is what makes
    "read, decide, write" expressible as one unit."""
    with store.transaction() as txn:
        txn.put_record(claim(store))
        seen = txn.get_record("claim", "clm-a1b2")

        assert seen.path == "src/arbite/schema.py"
        assert txn.find_record("claim", "clm-nope") is None
        # Its own write is one revision ahead of whatever is stored.
        assert txn.revision("claim", "clm-a1b2") == store.revision("claim", "clm-a1b2") + 1


def test_a_raising_body_rolls_the_unit_back(store):
    """All-or-nothing, and the *earlier* steps are the point: an operation that puts
    three records and then fails leaves none of them."""
    with pytest.raises(RuntimeError):
        with store.transaction() as txn:
            txn.put_record(claim(store))
            txn.append_event("claim.acquired", "claim", ticket_id="tic-cf9f")
            raise RuntimeError("the operation gave up")

    assert store.records("claim") == []
    assert store.events() == []
    assert store.revision("claim", "clm-a1b2") == 0


def test_an_invalid_record_never_reaches_storage(store):
    """Validation happens when the record is buffered, so a bad record cannot even
    start a unit of work."""
    with pytest.raises(RecordError):
        with store.transaction() as txn:
            txn.put_record(claim(store, path="/etc/passwd"))

    assert store.records("claim") == []


def test_committing_the_same_transaction_twice_is_refused(store):
    txn = store.transaction()
    txn.put_record(claim(store))
    txn.commit()

    with pytest.raises(CoordinationError):
        txn.commit()
    with pytest.raises(CoordinationError):
        txn.put_record(attempt(store))


def test_an_empty_transaction_commits_nothing_and_does_not_fail(store):
    result = store.transaction().commit()

    assert result.applied is True
    assert store.has_state is False


# --- revisions --------------------------------------------------------------


def test_every_write_bumps_the_records_revision(store):
    """Revisions are explicit: a caller can ask for one, and it counts the writes to
    that record rather than the writes to the store."""
    assert store.revision("claim", "clm-a1b2") == 0
    store.put_record(claim(store))
    assert store.revision("claim", "clm-a1b2") == 1
    store.put_record(claim(store, observed_version="sha256:" + "1" * 64))
    assert store.revision("claim", "clm-a1b2") == 2
    assert store.revision("attempt", "att-91bd") == 0

    store.delete_record("claim", "clm-a1b2")
    assert store.revision("claim", "clm-a1b2") == 0


def test_a_stale_expectation_changes_nothing_at_all(store):
    """The optimistic write: the loser's whole unit is discarded, including the
    event it was going to append, and its revision is left exactly as the winner
    left it."""
    store.put_record(claim(store))
    seen_revision = store.revision("claim", "clm-a1b2")

    with store.transaction() as txn:
        txn.replace_record(
            claim(store, observed_version="sha256:" + "1" * 64), expect_revision=seen_revision
        )
    assert store.revision("claim", "clm-a1b2") == seen_revision + 1

    with pytest.raises(Stale) as failure:
        with store.transaction() as txn:
            txn.replace_record(
                claim(store, observed_version="sha256:" + "2" * 64),
                expect_revision=seen_revision,
            )
            txn.append_event("claim.conflict", "claim", ticket_id="tic-cf9f")

    assert "revision" in str(failure.value)
    assert store.events() == []
    assert store.get_record("claim", "clm-a1b2").observed_version.endswith("1" * 64)
    assert store.revision("claim", "clm-a1b2") == seen_revision + 1


def test_a_concurrent_change_cannot_lose_another_writers_field(store):
    """The ticket's acceptance criterion: with the status left unchanged, two writers
    that read the same revision cannot both commit -- so the second one's field
    cannot silently overwrite the first one's. The loser re-reads and retries, and
    then *both* changes are in the record."""
    store.put_record(attempt(store, worker_id="claude.opus.001"))
    seen = store.revision("attempt", "att-91bd")

    # Worker A changes the worker id, keeping the state.
    with store.transaction() as txn:
        txn.replace_record(attempt(store, worker_id="claude.opus.002"), expect_revision=seen)

    # Worker B, working from the same read, changes the handoff.
    with pytest.raises(Stale):
        with store.transaction() as txn:
            txn.replace_record(
                attempt(store, worker_id="claude.opus.001", handoff="resume at the parser"),
                expect_revision=seen,
            )

    assert store.get_record("attempt", "att-91bd").worker_id == "claude.opus.002"

    # B re-reads and retries with the current revision: now both fields are present,
    # which is exactly what "cannot lose fields" means.
    fresh = store.revision("attempt", "att-91bd")
    with store.transaction() as txn:
        txn.replace_record(
            attempt(store, worker_id="claude.opus.002", handoff="resume at the parser"),
            expect_revision=fresh,
        )

    stored = store.get_record("attempt", "att-91bd")
    assert (stored.worker_id, stored.handoff) == ("claude.opus.002", "resume at the parser")


def test_an_expectation_is_validated(store):
    with pytest.raises(RecordError):
        store.transaction().replace_record(claim(store), expect_revision=-1)


# --- the event cursor -------------------------------------------------------


def test_appended_events_take_the_stores_next_cursor(store, tmp_path):
    """Monotonic and store-wide, across store instances -- which is what makes
    `--after <cursor>` resumable by the *next* process as well as this one."""
    for _ in range(3):
        with store.transaction() as txn:
            txn.append_event("write.file", "file", subject="src/arbite/schema.py")

    reopened = make_store(tmp_path, store.kind)
    assert [event.cursor for event in reopened.events()] == [1, 2, 3]
    with reopened.transaction() as txn:
        assert txn.append_event("write.file", "file").cursor == 4

    # An event placed at an explicit cursor -- an import (tic-008f) -- cannot be
    # overtaken by the next append.
    reopened.put_record(
        coordination_records.Event(
            id="evt-0009",
            cursor=9,
            kind="write.file",
            category="file",
            recorded_at=T0,
        )
    )
    with reopened.transaction() as txn:
        event = txn.append_event("write.file", "file")

    assert event.cursor == 10


def test_an_event_is_deduplicated_by_its_operation_id(store):
    """A retried operation must not leave a second copy of its effects. The same
    operation id with the same kind is the same event; a different kind is a
    different event about the same operation, which is how a lifecycle records two
    facts about one act."""
    with store.transaction() as txn:
        first = txn.append_event("claim.acquired", "claim", operation_id="op-4f19")

    with store.transaction() as txn:
        again = txn.append_event(
            "claim.acquired", "claim", subject="ignored", operation_id="op-4f19"
        )
        other = txn.append_event("claim.released", "claim", operation_id="op-4f19")

    assert again.id == first.id and again.cursor == first.cursor
    assert other.id != first.id
    assert [event.kind for event in store.events()] == ["claim.acquired", "claim.released"]


def test_a_retried_operation_id_commits_once(store):
    """The caller-level retry: an operation that already committed is not applied a
    second time, so a re-run after a timeout cannot duplicate the claim, the receipt
    or the event."""
    with store.transaction(operation_id="op-1234") as txn:
        txn.put_record(claim(store))
        txn.append_event("claim.acquired", "claim", operation_id="op-1234")
        txn.put_record(receipt("op-1234"))

    assert [event.cursor for event in store.events()] == [1]

    retry = store.transaction(operation_id="op-1234")
    retry.put_record(claim(store, path="src/arbite/errors.py"))
    retry.append_event("claim.acquired", "claim", operation_id="op-1234")
    result = retry.commit()

    assert (result.applied, result.deduplicated) == (False, True)
    assert [stored.path for stored in store.records("claim")] == ["src/arbite/schema.py"]
    assert [event.cursor for event in store.events()] == [1]


def test_a_pending_operation_is_not_deduplicated(store):
    """A *pending* receipt is an operation that started and never finished: a retry
    has to run it, because treating it as done would lose the write. Reconciling it
    is the recovery engine's job (tic-b03b)."""
    store.put_record(receipt("op-1234", result=coordination_records.RECEIPT_PENDING))

    with store.transaction(operation_id="op-1234") as txn:
        txn.put_record(claim(store))

    assert len(store.records("claim")) == 1


# --- one interface, two backends --------------------------------------------


def test_the_two_backends_reach_the_same_observable_state(tmp_path):
    """Nothing in a caller's code should be able to tell the journal-plus-lock
    backend from the SQL engine: the same operations leave the same records, the
    same revisions and the same event stream."""
    file_store = make_store(tmp_path / "file", "file")
    sqlite_store = make_store(tmp_path / "sqlite", "sqlite")

    for store in (file_store, sqlite_store):
        with store.transaction() as txn:
            txn.put_record(claim(store))
            txn.append_event(
                "claim.acquired",
                "claim",
                subject="src/arbite/schema.py",
                result="gen 1",
                ticket_id="tic-cf9f",
                attempt_id="att-91bd",
                actor="claude.opus.001",
                operation_id="op-4f19",
            )
        store.put_record(receipt("op-2b8d"))
        with pytest.raises(Stale):
            with store.transaction() as txn:
                txn.replace_record(claim(store), expect_revision=0)

    def projection(store) -> dict:
        """Everything a caller can observe, with the generated ids (the only
        difference the two backends are allowed to have) left out."""
        info = store.info().to_dict()

        def without_id(document: dict) -> dict:
            return {key: value for key, value in document.items() if key != "id"}

        return {
            "events": [without_id(event.to_dict()) for event in store.events()],
            "claims": [stored.to_dict() for stored in store.records("claim")],
            "receipts": [stored.to_dict() for stored in store.records("receipt")],
            "counts": {key: value for key, value in info.items() if key not in ("kind", "root")},
            "revisions": {
                f"claim/{claim.id}": store.revision("claim", claim.id)
                for claim in store.records("claim")
            },
        }

    assert projection(file_store) == projection(sqlite_store)


def test_only_the_file_backend_has_a_commit_journal(tmp_path):
    """The journal is one backend's *mechanism*, not part of the interface: SQLite
    gets the same outcome from its own transaction, and nothing in the shared layer
    asks it for a journal."""
    file_store = make_store(tmp_path / "file", "file")
    sqlite_store = make_store(tmp_path / "sqlite", "sqlite")

    assert isinstance(file_store, FileCoordinationStore)
    assert file_store.read_commit_journal() is None
    with file_store.transaction() as txn:
        txn.put_record(claim(file_store))
    assert file_store.read_commit_journal() is None
    assert json.loads((file_store._root / "revisions.json").read_text()) == {"claim/clm-a1b2": 1}
    assert not hasattr(sqlite_store, "read_commit_journal")
