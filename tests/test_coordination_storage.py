"""Multiprocess and crash-injection acceptance for the C02 storage layer.

Everything here is about the guarantees that only show up across processes or
across a death: two writers racing one revision, many writers appending to one
event log, a process killed while holding the file sink's lock, and a process
killed in the middle of the file sink's journal protocol. The file sink and the
SQLite sink are checked with the same assertions wherever the semantics are the
same; the file-only tests are the ones about journals and process-safe locks.

Workers are module-level functions and are run through
`multiprocessing.get_context("spawn")`, because that is the only configuration
that proves the guarantee for *separate* interpreters rather than for threads
sharing one. Timeouts are short: a regression here should fail quickly, not hang.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from arbite import coordination as coord
from arbite.coordination_storage import (
    CRASH_AFTER_JOURNAL,
    CRASH_AFTER_RECORDS,
    check_revision,
    decode_journal,
    decode_record,
    encode_record,
    journal_intent,
    safe_component,
)
from arbite.errors import CoordinationConflict, InvalidRecord
from arbite.sinks.coordination_file import FileCoordinationStore
from arbite.sinks.coordination_sqlite import SqliteCoordinationStore

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

ARTIFACT_ID = "art-0123456789abcdef"
DOOMED_ARTIFACT_ID = "art-ffffffffffffffff"
ATTEMPT_ID = "att-0123456789abcdef"
EVENT_ID = "evt-0123456789abcdef"
WORKSPACE_ID = "ws-0123456789abcdef"

SPAWN = "spawn"


# ---------------------------------------------------------------------------
# Records (deterministic ids so the parent can look for exactly what a child wrote)
# ---------------------------------------------------------------------------


def _artifact(location: str, record_id: str = ARTIFACT_ID) -> coord.Artifact:
    return coord.Artifact(
        id=record_id,
        digest=coord.digest_of_text("alpha"),
        size=5,
        created=coord.utc_now(),
        location=location,
    )


def _attempt() -> coord.WorkAttempt:
    now = coord.utc_now()
    return coord.WorkAttempt(
        id=ATTEMPT_ID,
        ticket_id="tic-a1b2",
        worker_id="claude.opus.001",
        workspace_id=WORKSPACE_ID,
        generation=1,
        started=now,
        last_activity=now,
    )


def _event(operation_id=None) -> coord.Event:
    return coord.Event(
        id=EVENT_ID,
        kind_="operation_recorded",
        category="operation",
        timestamp=coord.utc_now(),
        operation_id=operation_id,
    )


def _store_for(kind: str, root: str):
    if kind == "file":
        return FileCoordinationStore(Path(root))
    return SqliteCoordinationStore(Path(root))


def _root_for(kind: str, arbite_dir: Path) -> str:
    if kind == "file":
        return str(arbite_dir / "coordination")
    return str(arbite_dir / "arbite.db")


# ---------------------------------------------------------------------------
# Module-level workers (spawn must be able to import and pickle these)
# ---------------------------------------------------------------------------


def _race_revision(kind: str, root: str, location: str, results) -> None:
    """Claim revision 1 of one artifact by writing a *different* location.

    Both racers see the same starting revision, so the winner is decided by the
    store's own serialization: exactly one put may observe revision 1.
    """
    store = _store_for(kind, root)
    record = _artifact(location=location)
    try:
        with store.transaction() as tx:
            tx.put(record, expect_revision=1)
    except CoordinationConflict as e:
        results.put(("conflict", location, e.details.get("current")))
        return
    results.put(("ok", location, None))


def _append_events(kind: str, root: str, count: int, results) -> None:
    """Append `count` *distinct* events in one transaction and report cursors.

    Distinct ids matter: deduplication is by event id, so reusing one id here
    would (correctly) collapse every append into the same event and the test
    would be measuring the dedup, not the cursor allocation.
    """
    store = _store_for(kind, root)
    with store.transaction() as tx:
        cursors = []
        for _ in range(count):
            event = _event()
            event.id = coord.new_record_id("event")
            cursors.append(tx.append_event(event).cursor)
    results.put(cursors)


def _hold_lock_and_die(root: str) -> None:
    """Take the file sink's coarse lock and die without releasing it.

    Nothing calls `release()`: if the guarantee holds, the operating system drops
    the `flock` when this process exits, and no lock file state persists.
    """
    store = FileCoordinationStore(Path(root))
    with store.coarse_lock():
        os._exit(93)


def _crash_during_commit(root: str, phase: str) -> None:
    store = FileCoordinationStore(Path(root), crash_point=phase)
    with store.transaction() as tx:
        tx.put(_attempt())
        tx.append_event(_event())
    os._exit(0)  # pragma: no cover - the crash point exits first


def _crash_with_an_unattributed_artifact(root: str) -> None:
    """Journal an artifact -- a record with no workspace id -- then die."""
    store = FileCoordinationStore(Path(root), crash_point=CRASH_AFTER_JOURNAL)
    with store.transaction() as tx:
        tx.put(_artifact(location="artifact:no-workspace"))
    os._exit(97)  # pragma: no cover


def _begin_and_die(db_path: str) -> None:
    """Open a real write transaction on the SQLite store and die inside it."""
    store = SqliteCoordinationStore(Path(db_path))
    store.init()
    conn = sqlite3.connect(db_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT OR REPLACE INTO coordination_records (kind, record_id, revision, payload) "
        "VALUES ('artifact', ?, 1, '{\"schema_version\": 1, \"revision\": 1, "
        "\"record\": {\"kind\": \"artifact\", \"id\": \"art-ffffffffffffffff\", "
        "\"digest\": \"sha256:0\", \"size\": 0, \"created\": \"2026-01-01T00:00:00Z\", "
        "\"location\": \"artifact:doomed\"}}')",
        (DOOMED_ARTIFACT_ID,),
    )
    os._exit(91)


# ---------------------------------------------------------------------------
# The storage-neutral helper module
# ---------------------------------------------------------------------------


def test_record_envelopes_round_trip_with_their_revision():
    record = _artifact(location="artifact:one")
    envelope = encode_record(record, 3)
    assert envelope["schema_version"] == 1 and envelope["revision"] == 3
    decoded, revision = decode_record(envelope)
    assert decoded == record and revision == 3
    # ... and from the JSON string a sink actually stores.
    import json

    decoded, revision = decode_record(json.dumps(envelope))
    assert decoded == record and revision == 3


def test_record_envelopes_refuse_a_missing_version_or_revision():
    for bad in ({"revision": 1, "record": {}}, {"schema_version": 1, "record": {}}):
        with pytest.raises(InvalidRecord):
            decode_record(bad)
    with pytest.raises(InvalidRecord):
        encode_record(_artifact(location="x"), 0)


def test_check_revision_reports_a_retryable_conflict_with_the_numbers():
    assert check_revision("file_claim", "clm-x", None, 4) == 4
    assert check_revision("file_claim", "clm-x", 4, 4) == 4
    with pytest.raises(CoordinationConflict) as excinfo:
        check_revision("file_claim", "clm-x", 3, 4)
    assert excinfo.value.retryable is True
    assert excinfo.value.details == {
        "kind": "file_claim",
        "record_id": "clm-x",
        "expected": 3,
        "current": 4,
    }


def test_journal_intents_round_trip_and_are_validated():
    intent = journal_intent(
        "txn-abc", workspace_ids=[WORKSPACE_ID], records=[{"kind": "x"}], events=[]
    )
    assert decode_journal(intent) == intent
    with pytest.raises(InvalidRecord):
        decode_journal({"schema_version": 1, "records": [], "events": []})


def test_store_keys_refuse_path_traversal():
    assert safe_component("work_attempt", what="kind") == "work_attempt"
    for bad in ("../escape", "a/b", "", ".."):
        with pytest.raises(InvalidRecord):
            safe_component(bad, what="record id")


# ---------------------------------------------------------------------------
# Two processes, one revision
# ---------------------------------------------------------------------------


def test_two_processes_racing_one_revision_produce_exactly_one_winner(kind, arbite_dir, sink):
    store = sink.coordination()
    with store.transaction() as tx:
        tx.put(_artifact(location="artifact:original"))

    context = multiprocessing.get_context(SPAWN)
    results = context.Queue()
    processes = [
        context.Process(
            target=_race_revision, args=(kind, _root_for(kind, arbite_dir), f"artifact:{i}", results)
        )
        for i in (1, 2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(60)
    assert [p.exitcode for p in processes] == [0, 0]

    outcomes = sorted(results.get(timeout=10) for _ in processes)
    assert [outcome[0] for outcome in outcomes] == ["conflict", "ok"]
    winner = [outcome[1] for outcome in outcomes if outcome[0] == "ok"][0]
    loser = [outcome for outcome in outcomes if outcome[0] == "conflict"][0]
    # The loser lost on the revision, not on the lock: it saw revision 2.
    assert loser[2] == 2

    with store.transaction(write=False) as tx:
        stored = tx.get("artifact", ARTIFACT_ID)
        assert stored.location == winner
        assert tx.revision_of("artifact", ARTIFACT_ID) == 2


# ---------------------------------------------------------------------------
# Many processes, one event log
# ---------------------------------------------------------------------------


def test_many_processes_appending_events_get_unique_contiguous_cursors(kind, arbite_dir, sink):
    store = sink.coordination()
    context = multiprocessing.get_context(SPAWN)
    results = context.Queue()
    process_count, per_process = 4, 3
    processes = [
        context.Process(
            target=_append_events,
            args=(kind, _root_for(kind, arbite_dir), per_process, results),
        )
        for _ in range(process_count)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(60)
    assert [p.exitcode for p in processes] == [0] * process_count

    allocated = []
    for _ in processes:
        allocated.extend(results.get(timeout=10))
    expected = list(range(1, process_count * per_process + 1))
    assert len(allocated) == len(expected)
    assert len(set(allocated)) == len(allocated)
    assert sorted(allocated) == expected

    log = store.event_log()
    assert [e.cursor for e in log] == expected
    assert len({e.id for e in log}) == len(expected)


# ---------------------------------------------------------------------------
# The file sink: process death, the lock, and the journal
# ---------------------------------------------------------------------------


def test_process_death_while_holding_the_file_lock_leaves_no_permanent_lock(arbite_dir):
    root = arbite_dir / "coordination"
    context = multiprocessing.get_context(SPAWN)
    process = context.Process(target=_hold_lock_and_die, args=(str(root),))
    process.start()
    process.join(60)
    assert process.exitcode == 93

    # The lock file exists, but the lock itself died with the process.
    assert (root / "lock").exists()
    store = FileCoordinationStore(root)
    started = time.monotonic()
    with store.transaction() as tx:
        tx.put(_artifact(location="artifact:after-crash"))
    assert time.monotonic() - started < 2.0, "the lock must not outlive its process"

    assert list((root / "journal").glob("*.json")) == []
    assert store.recover_pending(WORKSPACE_ID) == []


@pytest.mark.parametrize(
    "phase",
    [CRASH_AFTER_JOURNAL, CRASH_AFTER_RECORDS],
)
def test_a_crash_mid_commit_is_reported_then_replayed_deterministically(arbite_dir, phase):
    root = arbite_dir / "coordination"
    context = multiprocessing.get_context(SPAWN)
    process = context.Process(target=_crash_during_commit, args=(str(root), phase))
    process.start()
    process.join(60)
    assert process.exitcode == 97

    store = FileCoordinationStore(root)

    # Inspection is a separate step from repair: the leftover journal is reported
    # as pending, attributed to the workspace the intent names, and is still there.
    reports = store.recover_pending(WORKSPACE_ID)
    assert [report.state for report in reports] == ["pending"]
    assert reports[0].operation_id.startswith("txn-")
    assert list((root / "journal").glob("*.json"))

    # A transaction -- read-only is enough -- replays it forward before reading.
    with store.transaction(write=False) as tx:
        record = tx.get("work_attempt", ATTEMPT_ID)
        assert record is not None
        assert record.ticket_id == "tic-a1b2"
        assert record.workspace_id == WORKSPACE_ID and record.generation == 1
        assert [e.id for e in tx.find("event")] == [EVENT_ID]

    assert store.recover_pending(WORKSPACE_ID) == []
    assert list((root / "journal").glob("*.json")) == []

    # Exactly one record at exactly one revision, exactly one event at its
    # journaled cursor -- not two, and not a bumped revision.
    log = store.event_log()
    assert [e.id for e in log] == [EVENT_ID]
    assert [e.cursor for e in log] == [1]
    with store.transaction(write=False) as tx:
        assert tx.revision_of("work_attempt", ATTEMPT_ID) == 1

    # Replaying again is a no-op: the intent is gone and nothing duplicated.
    with store.transaction(write=False) as tx:
        assert [e.id for e in tx.find("event")] == [EVENT_ID]
    assert len(store.event_log()) == 1


def test_an_unattributed_file_journal_is_reported_to_every_caller(arbite_dir):
    """A journal that names no workspace is a finding, not something to hide."""
    root = arbite_dir / "coordination"
    context = multiprocessing.get_context(SPAWN)
    process = context.Process(target=_crash_with_an_unattributed_artifact, args=(str(root),))
    process.start()
    process.join(60)
    assert process.exitcode == 97

    store = FileCoordinationStore(root)
    for workspace in (WORKSPACE_ID, coord.new_record_id("workspace")):
        reports = store.recover_pending(workspace)
        assert [report.state for report in reports] == ["pending"]
        assert reports[0].workspace_id == ""
        assert "names no workspace" in reports[0].detail


# ---------------------------------------------------------------------------
# The SQLite sink: a killed writer leaves nothing behind
# ---------------------------------------------------------------------------


def test_a_killed_sqlite_writer_leaves_no_lock_and_no_pending_state(arbite_dir, sink):
    db_path = arbite_dir / "arbite.db"
    context = multiprocessing.get_context(SPAWN)
    process = context.Process(target=_begin_and_die, args=(str(db_path),))
    process.start()
    process.join(60)
    assert process.exitcode == 91

    store = SqliteCoordinationStore(db_path)
    started = time.monotonic()
    with store.transaction() as tx:
        tx.put(_artifact(location="artifact:after-crash"))
    assert time.monotonic() - started < 2.0, "a dead writer must not hold the database"

    assert store.recover_pending(WORKSPACE_ID) == []
    # The uncommitted insert died with its process: no half-state survives.
    assert _raw_rows(
        db_path, "SELECT record_id FROM coordination_records WHERE record_id = ?", (DOOMED_ARTIFACT_ID,)
    ) == []
    assert _raw_rows(
        db_path, "SELECT record_id FROM coordination_records WHERE record_id = ?", (ARTIFACT_ID,)
    ) == [ARTIFACT_ID]


def _raw_rows(db_path: Path, sql: str, params=()) -> list:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return [row[0] for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rollback and operation-id retry, at the sink level
# ---------------------------------------------------------------------------


def test_rollback_discards_state_and_a_retried_operation_id_is_idempotent(sink):
    store = sink.coordination()
    receipt_id = "op-0123456789abcdef"

    # A rolled-back transaction leaves nothing: no record, no event, no revision.
    tx = store.transaction()
    tx.put(_attempt())
    tx.append_event(_event(operation_id=receipt_id))
    tx.rollback()
    with store.transaction(write=False) as tx:
        assert tx.get("work_attempt", ATTEMPT_ID) is None
        assert tx.get("event", EVENT_ID) is None
        assert tx.revision_of("work_attempt", ATTEMPT_ID) == 0
    assert store.event_log() == []

    # The retried operation id is deduplicated by the store itself.
    receipt = coord.OperationReceipt(
        id=receipt_id,
        attempt_id=ATTEMPT_ID,
        ticket_id="tic-a1b2",
        actor="claude.opus.001",
        kind_="write",
        timestamp=coord.utc_now(),
    )
    with store.transaction() as tx:
        _stored, created = tx.put_if_absent(receipt)
        assert created is True
        event = tx.append_event(_event(operation_id=receipt_id))

    with store.transaction() as tx:
        again, created_again = tx.put_if_absent(receipt)
        assert created_again is False
        assert again.id == receipt_id
        replayed_event = tx.append_event(_event(operation_id=receipt_id))
        assert replayed_event.id == event.id
        assert tx.revision_of("operation_receipt", receipt_id) == 1

    assert [e.id for e in store.event_log()] == [event.id]


# ---------------------------------------------------------------------------
# The file sink does not secretly require SQLite
# ---------------------------------------------------------------------------


def test_the_file_store_runs_with_sqlite3_blocked():
    """A fresh interpreter where `import sqlite3` raises still drives the store.

    A module-source check alone would not prove this: the subprocess refuses the
    import outright and then binds a workspace, writes a record and appends an
    event through the file store.
    """
    source = '''
import importlib.abc
import pathlib
import sys
import tempfile


class BlockSqlite3(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "sqlite3" or fullname.startswith("sqlite3."):
            raise ImportError("sqlite3 is blocked for this test")
        return None


sys.meta_path.insert(0, BlockSqlite3())
sys.modules.pop("sqlite3", None)

from arbite import coordination as coord
from arbite.sinks.coordination_file import FileCoordinationStore

root = pathlib.Path(tempfile.mkdtemp())
store = FileCoordinationStore(root)
now = coord.utc_now()
workspace = coord.Workspace(id=coord.new_record_id("workspace"), root=str(root), created=now, updated=now)
binding = coord.StoreBinding(
    id=coord.new_record_id("store_binding"),
    workspace_id=workspace.id,
    sink_kind="file",
    location=str(root),
    bound_at=now,
)
store.bind_store(binding)
with store.transaction() as tx:
    tx.put(coord.Artifact(
        id="art-0123456789abcdef",
        digest=coord.digest_of_text("alpha"),
        size=5,
        created=now,
        location="artifact:no-db",
    ))
    tx.append_event(coord.Event(
        id="evt-0123456789abcdef",
        kind_="operation_recorded",
        category="operation",
        timestamp=now,
    ))
cursors = [event.cursor for event in store.event_log()]
assert cursors == [1], cursors
assert store.recover_pending(workspace.id) == []
assert "sqlite3" not in sys.modules, sorted(m for m in sys.modules if m.startswith("sqlite"))
print("ok")
'''
    environment = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    process = subprocess.run(
        [sys.executable, "-c", source],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip().endswith("ok")
