"""The application layer: guards, the service, and the sink interfaces.

The interface tests here pin the *shape* of the storage contract later tickets
implement (abstract methods, no behaviour pre-implemented, both current sinks
reporting "not yet") and the behaviour tests pin the hard rules the planning
documents require: only an active attempt may act, a claim must be held by the
caller, generations and digests must be current, a pre-claim read never
authorizes a write, and a refused operation leaves no half-applied record but
does leave an error receipt.

The store used is a small in-memory implementation of the same interface. That
is intentional: it proves the interface is sufficient to express a guarded,
multi-record operation without any SQL or filesystem knowledge, which is the
property later tickets depend on.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from arbite import application, coordination as c
from arbite.application import Actor, CoordinationService
from arbite.errors import (
    AttemptInactive,
    ClaimConflict,
    CoordinationConflict,
    CoordinationNotFound,
    InvalidRecord,
    RecoveryRequired,
    StaleRead,
    StoreBindingConflict,
)
from arbite.sinks import CoordinationStore, CoordinationTransaction
from arbite.sinks.file import FileSink
from arbite.sinks.sqlite import SqliteSink


# --- a minimal store implementing the interface ----------------------------


class FakeTransaction(CoordinationTransaction):
    """Buffers record/event writes and applies them atomically on commit.

    Upgraded for C02 so the application-layer dedup tests are meaningful: it
    honours `expect_revision`, answers `revision_of`, and deduplicates events by
    id and by operation id -- the same three behaviours the two real sinks
    implement, expressed in memory.
    """

    def __init__(self, store, write):
        self.store = store
        self.write = write
        self.pending = {}
        self.pending_revisions = {}
        self.events = []

    def put(self, record, *, expect_revision=None):
        problems = record.validate()
        if problems:
            raise InvalidRecord(f"invalid {record.kind}: {'; '.join(problems)}")
        if not self.write:
            raise InvalidRecord("transaction is read-only")
        current = self.revision_of(record.kind, record.record_id)
        if expect_revision is not None and int(expect_revision) != current:
            raise CoordinationConflict(
                f"{record.kind} {record.record_id} is at revision {current}, "
                f"not {int(expect_revision)}",
                details={
                    "kind": record.kind,
                    "record_id": record.record_id,
                    "expected": int(expect_revision),
                    "current": current,
                },
            )
        key = (record.kind, record.record_id)
        self.pending[key] = record
        self.pending_revisions[key] = current + 1
        return record

    def revision_of(self, kind, record_id):
        if kind == "event":
            return 0
        key = (kind, record_id)
        if key in self.pending_revisions:
            return int(self.pending_revisions[key])
        return int(self.store.revisions.get(key, 0))

    def get(self, kind, record_id):
        if (kind, record_id) in self.pending:
            return self.pending[(kind, record_id)]
        record = self.store.records.get((kind, record_id))
        if record is not None:
            return record
        if kind == "event":
            for event in self.store.event_log:
                if event.id == record_id:
                    return event
        return None

    def find(self, kind, **fields):
        found = {}
        for source in (self.pending, self.store.records):
            for (rec_kind, record_id), record in source.items():
                if rec_kind != kind:
                    continue
                found[record_id] = record
        if kind == "event":
            for event in self.store.event_log:
                found.setdefault(event.id, event)
            for event in self.events:
                found[event.id] = event
        matches = [
            record
            for record in found.values()
            if all(getattr(record, name, None) == value for name, value in fields.items())
        ]
        if kind == "event":
            matches.sort(key=lambda e: (e.cursor is None, e.cursor or 0))
        return matches

    def append_event(self, event):
        if not self.write:
            raise InvalidRecord("transaction is read-only")
        for candidate in list(self.store.event_log) + list(self.events):
            if candidate.id == event.id:
                return candidate
            if event.operation_id and candidate.operation_id == event.operation_id:
                return candidate
        event.cursor = None
        self.events.append(event)
        return event

    def commit(self):
        for key, record in self.pending.items():
            self.store.records[key] = record
            self.store.revisions[key] = self.pending_revisions[key]
        for event in self.events:
            event.cursor = self.store.cursor
            self.store.cursor += 1
            self.store.event_log.append(event)
        self.pending.clear()
        self.pending_revisions.clear()
        self.events.clear()

    def rollback(self):
        self.pending.clear()
        self.pending_revisions.clear()
        self.events.clear()


class FakeCoordinationStore(CoordinationStore):
    def __init__(self):
        self.records = {}
        self.revisions = {}
        self.event_log = []
        self.cursor = 0
        self.bindings = {}
        self.reports = []

    def bind_store(self, binding):
        problems = binding.validate()
        if problems:
            raise InvalidRecord("invalid binding: " + "; ".join(problems))
        existing = self.bindings.get(binding.workspace_id)
        if existing is None:
            self.bindings[binding.workspace_id] = binding
            return binding
        if existing.matches_binding(binding):
            return existing
        raise StoreBindingConflict(
            f"{existing.sink_kind}:{existing.location} is already authoritative",
            details={"bound": existing.sink_kind, "requested": binding.sink_kind},
        )

    def store_binding(self, workspace_id):
        return self.bindings.get(workspace_id)

    def transaction(self, write=True):
        return FakeTransaction(self, write)

    def recover_pending(self, workspace_id):
        return [r for r in self.reports if r.workspace_id == workspace_id]


class Clock:
    """Monotonic, valid UTC seconds so ordering assertions are exact."""

    def __init__(self, start=0):
        self.seconds = start

    def __call__(self):
        self.seconds += 1
        return f"2026-01-01T00:00:{self.seconds:02d}Z"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store():
    return FakeCoordinationStore()


@pytest.fixture
def workspace(store, clock):
    ws = c.Workspace(
        id=c.new_record_id("workspace"),
        root="/tmp/ws",
        created=clock(),
        updated=clock(),
    )
    binding = c.StoreBinding(
        id=c.new_record_id("store_binding"),
        workspace_id=ws.id,
        sink_kind="file",
        location="/tmp/ws/.arbite",
        bound_at=clock(),
    )
    ws.bind(binding)
    store.bind_store(binding)
    return ws


@pytest.fixture
def attempt(workspace, clock):
    return c.WorkAttempt(
        id=c.new_record_id("work_attempt"),
        ticket_id="tic-a1b2",
        worker_id="claude.opus.001",
        workspace_id=workspace.id,
        generation=1,
        started=clock(),
        last_activity=clock(),
    )


@pytest.fixture
def service(workspace, store, clock):
    return CoordinationService(
        workspace, store, actor=Actor("claude.opus.001"), clock=clock
    )


def held_claim(workspace, attempt, clock, path="src/a.py", generation=1, observed_version=None):
    return c.FileClaim(
        id=c.new_record_id("file_claim"),
        workspace_id=workspace.id,
        path=path,
        ticket_id=attempt.ticket_id,
        attempt_id=attempt.id,
        generation=generation,
        acquired=clock(),
        observed_version=observed_version or c.digest_of_text("alpha"),
    )


# --- the interfaces themselves ---------------------------------------------


def test_the_coordination_interfaces_are_abstract():
    with pytest.raises(TypeError):
        CoordinationStore()
    with pytest.raises(TypeError):
        CoordinationTransaction()


def test_coordination_store_exposes_the_agreed_abstract_methods():
    assert CoordinationStore.__abstractmethods__ == frozenset(
        {"bind_store", "store_binding", "transaction", "recover_pending"}
    )
    assert CoordinationTransaction.__abstractmethods__ == frozenset(
        {"put", "get", "find", "append_event", "commit", "rollback"}
    )


def test_both_sinks_implement_coordination_and_the_file_store_is_not_sqlite(arbite_dir):
    """The C02 parity baseline, replacing the C01 "neither sink does this yet" one.

    Both shipped sinks now return a coordination store, and neither may quietly
    require the other: the file store's own module mentions no database at all.
    (The stronger, behavioural proof that the file store runs with `sqlite3`
    unavailable lives in `test_coordination_storage.py`.)
    """
    with pytest.raises(TypeError):
        CoordinationStore()

    file_store = FileSink(arbite_dir).coordination()
    sqlite_store = SqliteSink(arbite_dir / "arbite.db").coordination()
    assert file_store is not None
    assert sqlite_store is not None
    assert file_store.contract_version() == c.CONTRACT_VERSION
    assert sqlite_store.contract_version() == c.CONTRACT_VERSION
    assert type(file_store) is not type(sqlite_store)

    source = Path(inspect.getsourcefile(type(file_store))).read_text(encoding="utf-8")
    assert "sqlite3" not in source


def test_store_contract_version_matches_the_domain_contract(store):
    assert store.contract_version() == c.CONTRACT_VERSION


# --- binding ---------------------------------------------------------------


def test_service_requires_a_bound_workspace(store, clock):
    unbound = c.Workspace(
        id=c.new_record_id("workspace"), root="/tmp/x", created=clock(), updated=clock()
    )
    with pytest.raises(InvalidRecord):
        CoordinationService(unbound, store)


def test_binding_is_idempotent_and_a_conflicting_store_is_refused(service, store, workspace):
    first = service.binding("file", "/tmp/ws/.arbite")
    assert first.sink_kind == "file"
    assert store.store_binding(workspace.id) is first

    again = service.binding("file", "/tmp/ws/.arbite")
    assert again.id == first.id

    with pytest.raises(StoreBindingConflict) as excinfo:
        service.binding("sqlite", "/tmp/ws/.arbite/arbite.db")
    assert excinfo.value.error_code == "store_binding_conflict"


# --- guards ----------------------------------------------------------------


def test_require_active_attempt_refuses_missing_and_terminal_attempts(attempt, clock):
    with pytest.raises(CoordinationNotFound):
        application.require_active_attempt(None)

    attempt.release(clock())
    with pytest.raises(AttemptInactive) as excinfo:
        application.require_active_attempt(attempt)
    assert excinfo.value.error_code == "attempt_inactive"
    assert excinfo.value.details["state"] == "released"


def test_require_claim_holder_refuses_missing_wrong_holder_and_released(workspace, attempt, clock):
    with pytest.raises(ClaimConflict):
        application.require_claim_holder(
            None, attempt=attempt, workspace_id=workspace.id, path="src/a.py"
        )

    other = c.WorkAttempt(
        id=c.new_record_id("work_attempt"),
        ticket_id="tic-a1b2",
        worker_id="claude.haiku.001",
        workspace_id=workspace.id,
        generation=2,
        started=clock(),
        last_activity=clock(),
    )
    claim = held_claim(workspace, other, clock)
    with pytest.raises(ClaimConflict):
        application.require_claim_holder(
            claim, attempt=attempt, workspace_id=workspace.id, path=claim.path
        )

    claim.release(clock())
    with pytest.raises(ClaimConflict):
        application.require_claim_holder(
            claim, attempt=other, workspace_id=workspace.id, path=claim.path
        )


def test_generation_and_digest_guards_reject_stale_state():
    with pytest.raises(StaleRead):
        application.require_current_generation(1, 2)
    assert application.require_current_generation(2, 2) == 2

    with pytest.raises(StaleRead):
        application.require_expected_digest(c.digest_of_text("a"), None, path="src/a.py")
    with pytest.raises(StaleRead):
        application.require_expected_digest(c.digest_of_text("new"), c.digest_of_text("old"), path="src/a.py")
    assert application.require_expected_digest(c.ABSENT, c.ABSENT, path="src/new.py") == c.ABSENT


def test_a_pre_claim_read_never_authorizes_a_write(workspace, attempt, clock):
    claim = held_claim(workspace, attempt, clock)  # acquired at t
    early = c.ReadObservation(
        id=c.new_record_id("read_observation"),
        operation_id=c.new_operation_id(),
        path=claim.path,
        digest=claim.observed_version,
        observed_at="2026-01-01T00:00:00Z",  # before the claim
        attempt_id=attempt.id,
        claim_generation=claim.generation,
    )
    with pytest.raises(StaleRead):
        application.require_write_authorization(early, claim=claim, attempt=attempt)
    assert early.write_authorizing is False


def test_a_matching_post_claim_read_authorizes_the_write(workspace, attempt, clock):
    claim = held_claim(workspace, attempt, clock)
    fresh = c.ReadObservation(
        id=c.new_record_id("read_observation"),
        operation_id=c.new_operation_id(),
        path=claim.path,
        digest=claim.observed_version,
        observed_at=clock(),
        attempt_id=attempt.id,
        claim_generation=claim.generation,
    )
    application.require_write_authorization(fresh, claim=claim, attempt=attempt)
    assert fresh.write_authorizing is True

    # ... but only for the attempt it was served to.
    other = c.WorkAttempt(
        id=c.new_record_id("work_attempt"),
        ticket_id="tic-a1b2",
        worker_id="claude.haiku.001",
        workspace_id=workspace.id,
        generation=2,
        started=clock(),
        last_activity=clock(),
    )
    with pytest.raises(StaleRead):
        application.require_write_authorization(fresh, claim=claim, attempt=other)


# --- observations ----------------------------------------------------------


def test_record_read_records_a_whole_file_digest_and_no_write_authority(service, store, attempt):
    content = b"line one\nline two\n"
    observation = service.record_read(attempt, "src/a.py", content, line_range=(1, 1))

    assert observation.digest == c.digest_of_bytes(content)
    assert observation.line_range == (1, 1)
    assert observation.covers_whole_file is False
    assert observation.write_authorizing is False
    assert store.records[("read_observation", observation.id)] is observation

    events = [e for e in store.event_log if e.event_kind == "read_observed"]
    assert events and events[0].category == "read"


def test_record_read_under_a_held_claim_authorizes_it(service, attempt, clock):
    claim = held_claim(service.workspace, attempt, clock)
    observation = service.record_read(attempt, claim.path, b"alpha", claim=claim)
    assert observation.write_authorizing is True
    assert observation.claim_generation == claim.generation


def test_record_read_refuses_a_claim_held_by_another_attempt(service, attempt, clock):
    other = c.WorkAttempt(
        id=c.new_record_id("work_attempt"),
        ticket_id="tic-a1b2",
        worker_id="claude.haiku.001",
        workspace_id=service.workspace.id,
        generation=2,
        started=clock(),
        last_activity=clock(),
    )
    claim = held_claim(service.workspace, other, clock)
    with pytest.raises(ClaimConflict):
        service.record_read(attempt, claim.path, b"alpha", claim=claim)


# --- guarded operations ----------------------------------------------------


def test_guarded_operation_commits_a_receipt_and_an_event(service, store, attempt):
    marker = c.Artifact(
        id=c.new_record_id("artifact"),
        digest=c.digest_of_text("x"),
        size=1,
        created=service.now(),
        location="artifact:" + c.digest_of_text("x"),
    )

    with service.guarded("write", attempt=attempt, paths=["src/a.py"]) as op:
        op.track_path("src/a.py", c.ABSENT, c.digest_of_text("x"))
        op.add_artifact(marker)

    receipts = [r for (kind, _), r in store.records.items() if kind == "operation_receipt"]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.result == "ok"
    assert receipt.operation_kind == "write"
    assert receipt.before["src/a.py"] == c.ABSENT
    assert receipt.after["src/a.py"] == c.digest_of_text("x")
    assert marker.id in receipt.artifact_refs

    events = [e for e in store.event_log if e.event_kind == "operation_recorded"]
    assert len(events) == 1
    assert events[0].operation_id == receipt.id
    assert events[0].cursor == 0


def test_retrying_an_operation_id_leaves_one_receipt_and_one_event(service, store, attempt):
    """A retry of the same operation id is deduplicated end to end.

    The operation id *is* the receipt id, so the second guarded operation finds
    the original receipt and the original event and writes neither a second
    receipt nor a second event.
    """
    operation_id = "op-0123456789abcdef"

    with service.guarded(
        "write", attempt=attempt, paths=["src/a.py"], operation_id=operation_id
    ) as op:
        op.track_path("src/a.py", c.ABSENT, c.digest_of_text("x"))

    with service.guarded(
        "write", attempt=attempt, paths=["src/a.py"], operation_id=operation_id
    ) as op:
        op.track_path("src/a.py", c.ABSENT, c.digest_of_text("x"))

    receipts = {
        record.id: record
        for (kind, _rid), record in store.records.items()
        if kind == "operation_receipt"
    }
    assert list(receipts) == [operation_id]
    events = [e for e in store.event_log if e.event_kind == "operation_recorded"]
    assert len(events) == 1
    assert events[0].operation_id == operation_id
    # Revision 1 proves the retry wrote nothing: a second stored receipt would
    # have bumped it to 2.
    assert store.revisions[("operation_receipt", operation_id)] == 1


def test_a_retried_refused_operation_keeps_one_error_receipt(service, store, attempt):
    """The error-audit path is deduplicated too, not just the success path."""
    operation_id = "op-0123456789abcdef"

    for _attempt_number in range(2):
        with pytest.raises(StaleRead):
            with service.guarded("write", attempt=attempt, operation_id=operation_id):
                raise StaleRead("bytes changed under us", details={"path": "src/a.py"})

    receipts = [r for (kind, _), r in store.records.items() if kind == "operation_receipt"]
    assert len(receipts) == 1
    assert receipts[0].result == "error"
    events = [e for e in store.event_log if e.event_kind == "operation_recorded"]
    assert len(events) == 1


def test_a_refused_operation_rolls_back_and_preserves_an_error_receipt(service, store, attempt):
    staged = c.Artifact(
        id=c.new_record_id("artifact"),
        digest=c.digest_of_text("y"),
        size=1,
        created=service.now(),
        location="artifact:y",
    )

    with pytest.raises(StaleRead):
        with service.guarded("write", attempt=attempt, paths=["src/a.py"]) as op:
            op.transaction.put(staged)
            op.track_path("src/a.py", c.digest_of_text("old"), c.digest_of_text("new"))
            raise StaleRead("bytes changed under us", details={"path": "src/a.py"})

    # The body's write was rolled back...
    assert ("artifact", staged.id) not in store.records
    # ... but the refusal is evidence, recorded with its error code.
    receipts = [r for (kind, _), r in store.records.items() if kind == "operation_receipt"]
    assert len(receipts) == 1
    assert receipts[0].result == "error"
    assert receipts[0].error["code"] == "stale_read"


def test_guarded_requires_an_active_attempt(service, attempt, clock):
    attempt.release(clock())
    with pytest.raises(AttemptInactive):
        service.guarded("write", attempt=attempt)


def test_guarded_rejects_an_unknown_operation_kind(service, attempt):
    with pytest.raises(InvalidRecord):
        service.guarded("teleport", attempt=attempt)


def test_apply_guards_checks_generation_and_digest_together(service, attempt, clock):
    claim = held_claim(service.workspace, attempt, clock, generation=2)

    held = service.apply_guards(claim=claim, attempt=attempt, expected_generation=2,
                                observed=claim.observed_version, expected_digest=claim.observed_version)
    assert held is claim

    with pytest.raises(StaleRead):
        service.apply_guards(claim=claim, attempt=attempt, expected_generation=1)


# --- recovery --------------------------------------------------------------


def test_pending_recovery_is_inspection_only(service, store, attempt):
    report = c.RecoveryReport(
        workspace_id=service.workspace.id,
        operation_id=c.new_operation_id(),
        state="drifted",
        observed_at=service.now(),
        paths=["src/a.py"],
        bytes_may_have_changed=True,
        detail="observed bytes match neither before nor after",
    )
    store.reports.append(report)

    pending = service.pending_recovery()
    assert pending == [report]

    with pytest.raises(RecoveryRequired) as excinfo:
        service.require_no_pending(report.operation_id)
    assert excinfo.value.bytes_may_have_changed is True
    assert excinfo.value.details["state"] == "drifted"

    service.require_no_pending("op-does-not-exist")


# --- attribution -----------------------------------------------------------


def test_actor_is_attribution_only():
    actor = Actor("claude.opus.001", declared_name="Claude Opus")
    assert actor.attribution_only is True
    assert str(actor) == "Claude Opus"
    assert str(Actor("claude.opus.001")) == "claude.opus.001"
    assert "authentication" in application.ATTRIBUTION_NOTICE.lower()
