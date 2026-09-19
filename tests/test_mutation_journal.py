"""The recoverable file-operation intent/artifact journal (planning key C05).

Every behavioural test runs against both sinks (the `sink` fixture), so "the file
sink and the SQLite sink behave equivalently" is checked, not assumed. The heart of
the suite is *fault injection at every boundary*: a test injects a crash between
the durable intent and the receipt and then asserts that the next relevant
operation reconciles honestly -- recognizing an applied change, a reverted one, or
preserving evidence and refusing to guess when observed bytes match neither.

Nothing here starts a daemon or a timer: recovery is always invoked by the *next*
operation (`MutationEngine.reconcile`, which each mutation calls first).
"""

from __future__ import annotations

import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from arbite import application, artifacts, coordination, fileclaims, lifecycle, locking, mutation
from arbite.application import Actor
from arbite.errors import (
    ArtifactCapacityError,
    ArtifactCorrupt,
    CoordinationConflict,
    DriftDetected,
    RecoveryRequired,
    StaleRead,
)
from arbite.sinks import SinkSpec, build_sink
from helpers import make_ticket

ABSENT = coordination.ABSENT


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FaultOnce:
    """A fault injector that raises once, at one named boundary."""

    def __init__(self, phase: str):
        self.phase = phase
        self.fired = False

    def __call__(self, phase: str) -> None:
        if phase == self.phase and not self.fired:
            self.fired = True
            raise mutation.FaultInjected(phase)


def _workspace(
    sink,
    arbite_dir,
    *,
    files=("src/a.py", "src/b.py"),
    worker="claude.opus.001",
    ticket="tic-a1b2",
    fault_injector=None,
    max_artifact_bytes=artifacts.DEFAULT_MAX_ARTIFACT_BYTES,
):
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    for rel in files:
        (root / rel).write_text(f"{rel}\n")
    sink.create(make_ticket(ticket))
    service = application.coordination_service_for(sink, root=str(root), actor=Actor(worker))
    ctl = lifecycle.TicketLifecycle(service, sink)
    attempt = ctl.acquire(sink.get(ticket), worker_id=worker).attempt
    claims = fileclaims.FileClaimService(service)
    engine = mutation.MutationEngine(
        service,
        claims,
        fault_injector=fault_injector,
        max_artifact_bytes=max_artifact_bytes,
    )
    return engine, claims, attempt, root, service, ctl


def _claim(claims, attempt, path):
    return claims.claim(attempt, [path]).acquired[0]


def _intents(service, operation_id):
    with service.store.transaction(write=False) as tx:
        return list(tx.find("operation_intent", operation_id=operation_id))


def _receipt(service, operation_id):
    return service.read_record("operation_receipt", operation_id)


def _event_count(service, operation_id):
    with service.store.transaction(write=False) as tx:
        return len(list(tx.find("event", operation_id=operation_id)))


# ---------------------------------------------------------------------------
# happy path: durable evidence for a successful change
# ---------------------------------------------------------------------------


def test_write_records_intent_receipt_and_content_addressed_artifacts(sink, arbite_dir):
    engine, claims, attempt, root, service, _ = _workspace(sink, arbite_dir)
    claim = _claim(claims, attempt, "src/a.py")

    result = engine.write(attempt, "src/a.py", b"new\n", claim=claim)

    assert result.ok and result.applied and not result.deduplicated
    assert result.before["src/a.py"] == coordination.digest_of_text("src/a.py\n")
    assert result.after["src/a.py"] == coordination.digest_of_text("new\n")
    assert (root / "src" / "a.py").read_bytes() == b"new\n"

    # Evidence: both before and after bytes are stored, content-addressed.
    assert service.store.read_artifact_bytes(coordination.digest_of_text("src/a.py\n")) == b"src/a.py\n"
    assert service.store.read_artifact_bytes(coordination.digest_of_text("new\n")) == b"new\n"
    assert result.artifact_refs
    for ref in result.artifact_refs:
        artifact = service.read_record("artifact", ref)
        assert artifact is not None
        assert service.store.has_artifact(artifact.digest)

    # Intent finalized, receipt durable, exactly one event for the operation.
    intents = _intents(service, result.operation_id)
    assert len(intents) == 1 and intents[0].state == "finalized"
    receipt = _receipt(service, result.operation_id)
    assert receipt.result == "ok"
    assert receipt.paths == ["src/a.py"]
    assert _event_count(service, result.operation_id) == 1

    # The live claim's observed_version advanced, invalidating the old read token.
    assert claims.claim_for("src/a.py").observed_version == result.after["src/a.py"]


def test_create_then_remove_preserves_deleted_bytes(sink, arbite_dir):
    engine, claims, attempt, root, _service, _ = _workspace(sink, arbite_dir)
    created = _claim(claims, attempt, "src/new.py")
    result = engine.write(attempt, "src/new.py", b"created\n", claim=created)
    assert result.ok and (root / "src" / "new.py").read_bytes() == b"created\n"
    assert result.before["src/new.py"] == ABSENT

    # The write re-used the same active claim (observed_version advanced).
    claim = claims.claim_for("src/new.py")
    removed = engine.remove(attempt, "src/new.py", claim=claim)
    assert removed.ok
    assert removed.after["src/new.py"] == ABSENT
    assert not (root / "src" / "new.py").exists()
    # The deleted bytes survive as evidence.
    assert _service.store.read_artifact_bytes(coordination.digest_of_text("created\n")) == b"created\n"


# ---------------------------------------------------------------------------
# idempotent retry / stale refusal / capacity
# ---------------------------------------------------------------------------


def test_idempotent_retry_does_not_apply_an_edit_twice(sink, arbite_dir):
    engine, claims, attempt, root, service, _ = _workspace(sink, arbite_dir)
    claim = _claim(claims, attempt, "src/a.py")
    operation_id = "op-0123456789abcdef"

    first = engine.write(attempt, "src/a.py", b"one\n", claim=claim, operation_id=operation_id)
    assert first.applied

    # A replay with the same operation id, even with different bytes, must not
    # apply a second change.
    replay = engine.write(attempt, "src/a.py", b"two\n", claim=claim, operation_id=operation_id)
    assert replay.deduplicated and not replay.applied
    assert replay.receipt.id == first.receipt.id
    assert (root / "src" / "a.py").read_bytes() == b"one\n"
    assert len(_intents(service, operation_id)) == 1
    assert _event_count(service, operation_id) == 1


def test_stale_expectation_changes_nothing(sink, arbite_dir):
    engine, claims, attempt, root, service, _ = _workspace(sink, arbite_dir)
    claim = _claim(claims, attempt, "src/a.py")

    with pytest.raises(StaleRead):
        engine.write(
            attempt,
            "src/a.py",
            b"x\n",
            claim=claim,
            expected_digest=coordination.digest_of_text("something else\n"),
        )
    assert (root / "src" / "a.py").read_bytes() == b"src/a.py\n"
    assert engine.pending_intents() == []


def test_capacity_failure_happens_before_any_bytes_change(sink, arbite_dir):
    engine, claims, attempt, root, service, _ = _workspace(
        sink, arbite_dir, max_artifact_bytes=4
    )
    claim = _claim(claims, attempt, "src/a.py")

    with pytest.raises(ArtifactCapacityError) as excinfo:
        engine.write(attempt, "src/a.py", b"0123456789", claim=claim)

    assert excinfo.value.bytes_may_have_changed is False
    assert (root / "src" / "a.py").read_bytes() == b"src/a.py\n"
    assert engine.pending_intents() == []
    assert service.store.read_artifact_bytes(coordination.digest_of_text("0123456789")) is None


# ---------------------------------------------------------------------------
# sink failure after replacement -> recovered on the NEXT operation
# ---------------------------------------------------------------------------


def test_sink_failure_after_replacement_is_reconciled_applied(sink, arbite_dir):
    engine, claims, attempt, root, service, _ = _workspace(
        sink, arbite_dir, fault_injector=FaultOnce(mutation.FAULT_AFTER_APPLY)
    )
    claim = _claim(claims, attempt, "src/a.py")
    operation_id = "op-aaaaaaaaaaaaaaaa"

    with pytest.raises(mutation.FaultInjected):
        engine.write(attempt, "src/a.py", b"after\n", claim=claim, operation_id=operation_id)

    # Bytes already changed; the intent is durable and unresolved.
    assert (root / "src" / "a.py").read_bytes() == b"after\n"
    assert [i.operation_id for i in engine.pending_intents()] == [operation_id]
    assert _receipt(service, operation_id) is None

    # The next relevant operation (a write to a different claimed path) reconciles
    # the interrupted one first.
    engine2 = mutation.MutationEngine(service, claims)
    other = _claim(claims, attempt, "src/b.py")
    engine2.write(attempt, "src/b.py", b"b-new\n", claim=other)

    receipt = _receipt(service, operation_id)
    assert receipt.result == "ok"
    assert receipt.before["src/a.py"] == coordination.digest_of_text("src/a.py\n")
    assert receipt.after["src/a.py"] == coordination.digest_of_text("after\n")
    assert engine2.pending_intents() == []
    assert (root / "src" / "a.py").read_bytes() == b"after\n"


def test_crash_before_apply_is_reconciled_reverted(sink, arbite_dir):
    engine, claims, attempt, root, service, _ = _workspace(
        sink, arbite_dir, fault_injector=FaultOnce(mutation.FAULT_BEFORE_APPLY)
    )
    claim = _claim(claims, attempt, "src/a.py")
    operation_id = "op-bbbbbbbbbbbbbbbb"

    with pytest.raises(mutation.FaultInjected):
        engine.write(attempt, "src/a.py", b"never\n", claim=claim, operation_id=operation_id)
    assert (root / "src" / "a.py").read_bytes() == b"src/a.py\n"

    engine2 = mutation.MutationEngine(service, claims)
    engine2.reconcile()

    receipt = _receipt(service, operation_id)
    assert receipt.result == "error"
    assert receipt.error["code"] == "operation_reverted"
    assert receipt.error["bytes_may_have_changed"] is False
    assert (root / "src" / "a.py").read_bytes() == b"src/a.py\n"
    assert engine2.pending_intents() == []


@pytest.mark.parametrize(
    "phase,expected",
    [
        (mutation.FAULT_AFTER_INTENT, "reverted"),
        (mutation.FAULT_AFTER_STAGE, "reverted"),
        (mutation.FAULT_BEFORE_APPLY, "reverted"),
        (mutation.FAULT_AFTER_APPLY, "applied"),
        (mutation.FAULT_BEFORE_FINALIZE, "applied"),
        (mutation.FAULT_AFTER_FINALIZE, "finalized"),
    ],
)
def test_every_write_boundary_reconciles_honestly(sink, arbite_dir, phase, expected):
    engine, claims, attempt, root, service, _ = _workspace(
        sink, arbite_dir, fault_injector=FaultOnce(phase)
    )
    claim = _claim(claims, attempt, "src/a.py")
    operation_id = "op-1111111111111111"

    with pytest.raises(mutation.FaultInjected):
        engine.write(attempt, "src/a.py", b"after\n", claim=claim, operation_id=operation_id)

    engine2 = mutation.MutationEngine(service, claims)
    engine2.reconcile()

    receipt = _receipt(service, operation_id)
    if expected == "reverted":
        assert (root / "src" / "a.py").read_bytes() == b"src/a.py\n"
        assert receipt.result == "error"
        assert receipt.error["code"] == "operation_reverted"
        # Any staged sibling the crash left behind is cleaned up, not applied.
        assert list((root / "src").glob(".*arbite-stage-*")) == []
    else:
        assert (root / "src" / "a.py").read_bytes() == b"after\n"
        assert receipt.result == "ok"
    assert engine2.pending_intents() == []


def test_edit_replaces_an_existing_file_with_edit_evidence(sink, arbite_dir):
    engine, claims, attempt, root, service, _ = _workspace(sink, arbite_dir)
    claim = _claim(claims, attempt, "src/a.py")

    result = engine.edit(attempt, "src/a.py", b"edited\n", claim=claim)
    assert result.ok and result.kind == "edit"
    assert (root / "src" / "a.py").read_bytes() == b"edited\n"
    receipt = _receipt(service, result.operation_id)
    assert receipt.operation_kind == "edit"
    assert receipt.artifact_refs
    assert service.store.read_artifact_bytes(coordination.digest_of_text("src/a.py\n")) == b"src/a.py\n"


# ---------------------------------------------------------------------------
# rename interrupted between the two paths
# ---------------------------------------------------------------------------


def test_rename_interrupted_between_paths_is_completed_by_recovery(sink, arbite_dir):
    engine, claims, attempt, root, service, _ = _workspace(
        sink,
        arbite_dir,
        fault_injector=FaultOnce(mutation.FAULT_AFTER_DEST_COMMITTED),
    )
    source_claim = _claim(claims, attempt, "src/a.py")
    dest_claim = _claim(claims, attempt, "src/c.py")
    operation_id = "op-cccccccccccccccc"

    with pytest.raises(mutation.FaultInjected):
        engine.rename(
            attempt,
            "src/a.py",
            "src/c.py",
            source_claim=source_claim,
            dest_claim=dest_claim,
            operation_id=operation_id,
        )

    # Exactly the "between paths" state: destination committed, source still there.
    assert (root / "src" / "c.py").read_bytes() == b"src/a.py\n"
    assert (root / "src" / "a.py").exists()

    engine2 = mutation.MutationEngine(service, claims)
    engine2.reconcile()

    assert not (root / "src" / "a.py").exists()
    assert (root / "src" / "c.py").read_bytes() == b"src/a.py\n"
    receipt = _receipt(service, operation_id)
    assert receipt.result == "ok"
    assert receipt.paths == ["src/a.py", "src/c.py"]
    assert receipt.before["src/a.py"] == coordination.digest_of_text("src/a.py\n")
    assert receipt.after["src/c.py"] == coordination.digest_of_text("src/a.py\n")
    assert receipt.after["src/a.py"] == ABSENT


# ---------------------------------------------------------------------------
# drift: preserve evidence, never guess or release ownership
# ---------------------------------------------------------------------------


def test_unknown_external_bytes_are_preserved_and_block_further_mutation(sink, arbite_dir):
    engine, claims, attempt, root, service, _ = _workspace(
        sink, arbite_dir, fault_injector=FaultOnce(mutation.FAULT_AFTER_APPLY)
    )
    claim = _claim(claims, attempt, "src/a.py")
    operation_id = "op-dddddddddddddddd"

    with pytest.raises(mutation.FaultInjected):
        engine.write(attempt, "src/a.py", b"proxy\n", claim=claim, operation_id=operation_id)

    # An external (non-proxy) writer changes the bytes to something matching
    # neither the recorded before nor the recorded after version.
    (root / "src" / "a.py").write_bytes(b"external\n")

    engine2 = mutation.MutationEngine(service, claims)
    with pytest.raises(DriftDetected) as excinfo:
        engine2.reconcile()
    assert excinfo.value.bytes_may_have_changed is True
    assert excinfo.value.details["operation_id"] == operation_id
    assert excinfo.value.details["state"] == "drifted"

    # Evidence preserved, external bytes untouched, ownership NOT released.
    assert (root / "src" / "a.py").read_bytes() == b"external\n"
    assert service.store.read_artifact_bytes(coordination.digest_of_text("src/a.py\n")) == b"src/a.py\n"
    assert service.store.read_artifact_bytes(coordination.digest_of_text("proxy\n")) == b"proxy\n"
    assert claims.claim_for("src/a.py") is not None

    # Drift keeps blocking: the next operation refuses too (no silent takeover).
    with pytest.raises(DriftDetected):
        engine2.reconcile()
    wrong = _claim(claims, attempt, "src/b.py")
    with pytest.raises(DriftDetected):
        engine2.write(attempt, "src/b.py", b"nope\n", claim=wrong)


# ---------------------------------------------------------------------------
# artifacts: dedup, verification, explicit limits
# ---------------------------------------------------------------------------


def test_artifact_content_is_stored_once_and_verified(sink, arbite_dir):
    _workspace(sink, arbite_dir)
    store = sink.coordination()

    first = store.store_artifact_bytes(b"the same bytes")
    second = store.store_artifact_bytes(b"the same bytes")
    assert first.id == second.id
    assert first.digest == second.digest
    assert store.has_artifact(first.digest)
    assert store.read_artifact_bytes(first.digest) == b"the same bytes"

    other = store.store_artifact_bytes(b"different bytes")
    assert other.digest != first.digest


def test_corrupted_artifact_is_reported_not_trusted(sink, arbite_dir):
    _workspace(sink, arbite_dir)
    store = sink.coordination()
    artifact = store.store_artifact_bytes(b"evidence bytes")
    _corrupt_artifact(sink, artifact.digest)

    with pytest.raises(ArtifactCorrupt):
        store.read_artifact_bytes(artifact.digest)


def _corrupt_artifact(sink, digest: str) -> None:
    store = sink.coordination()
    if sink.kind == "file":
        path = Path(store.root) / "artifacts" / digest.split(":", 1)[1]
        data = path.read_bytes()
        path.write_bytes(bytes([data[0] ^ 0xFF]) + data[1:])
        return
    conn = sqlite3.connect(store.root)
    try:
        row = conn.execute(
            "SELECT content FROM coordination_artifacts WHERE digest = ?", (digest,)
        ).fetchone()
        content = row[0]
        conn.execute(
            "UPDATE coordination_artifacts SET content = ? WHERE digest = ?",
            (bytes([content[0] ^ 0xFF]) + content[1:], digest),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# serialization: the operation lock and lifecycle
# ---------------------------------------------------------------------------


def test_engine_holds_the_operation_lock_across_apply(sink, arbite_dir):
    held_at_apply = []
    service_holder = {}

    def injector(phase):
        if phase == mutation.FAULT_BEFORE_APPLY:
            path = service_holder["service"].store.operation_lock_path()
            held_at_apply.append(locking.operation_lock_held(path))

    engine, claims, attempt, _root, service, _ = _workspace(
        sink, arbite_dir, fault_injector=injector
    )
    service_holder["service"] = service
    claim = _claim(claims, attempt, "src/a.py")
    engine.write(attempt, "src/a.py", b"locked\n", claim=claim)

    assert held_at_apply == [True]


def test_lifecycle_transitions_take_the_operation_lock(sink, arbite_dir):
    engine, claims, attempt, _root, service, ctl = _workspace(sink, arbite_dir)
    calls = []

    class _Spy:
        def __enter__(self):
            calls.append("acquire")
            return self

        def __exit__(self, exc_type, exc, tb):
            calls.append("release")
            return False

    service.store.operation_lock = lambda timeout=None: _Spy()
    ctl.touch(attempt)
    assert calls == ["acquire", "release"]


def test_lifecycle_reconciles_a_reverted_operation_before_ending(sink, arbite_dir):
    """A pending-but-unchanged operation is reconciled, not refused (C09).

    At `FAULT_BEFORE_APPLY` the intent is durable but no bytes changed, so
    reconciliation is unambiguous (`reverted`). C09 replaces the C03-era blanket
    refusal with "reconcile, then proceed": ending the attempt is safe, and the
    operation is finalized as reverted so no pending intent is left behind.
    """
    engine, claims, attempt, _root, service, ctl = _workspace(
        sink, arbite_dir, fault_injector=FaultOnce(mutation.FAULT_BEFORE_APPLY)
    )
    claim = _claim(claims, attempt, "src/a.py")
    with pytest.raises(mutation.FaultInjected):
        engine.write(attempt, "src/a.py", b"pending\n", claim=claim)
    assert (arbite_dir.parent / "src" / "a.py").read_bytes() == b"src/a.py\n"

    # The claim is still active: the cascade must release it as it ends the attempt.
    ctl.end_attempt(attempt)

    assert ctl.active_attempt("tic-a1b2") is None
    assert engine.pending_intents() == []
    with service.store.transaction(write=False) as tx:
        intents = list(tx.find("operation_intent", attempt_id=attempt.id))
    # The interrupted operation was finalized as reverted, never as applied.
    assert [intent.state for intent in intents] == ["reverted"]


def test_lifecycle_refuses_to_end_an_attempt_over_an_ambiguous_operation(sink, arbite_dir):
    """A drifted (ambiguous) operation stops a false clean close (C09).

    Bytes matching neither the recorded before nor after version cannot be
    reconciled, so the transition is refused with `bytes_may_have_changed=True`
    and the attempt stays active for an explicit resolution.
    """
    engine, claims, attempt, _root, _service, ctl = _workspace(
        sink, arbite_dir, fault_injector=FaultOnce(mutation.FAULT_BEFORE_APPLY)
    )
    claim = _claim(claims, attempt, "src/a.py")
    with pytest.raises(mutation.FaultInjected):
        engine.write(attempt, "src/a.py", b"pending\n", claim=claim)
    # An unrestricted external writer changes the bytes to a third value.
    (arbite_dir.parent / "src" / "a.py").write_bytes(b"external\n")

    with pytest.raises(DriftDetected) as excinfo:
        ctl.end_attempt(attempt)
    assert excinfo.value.bytes_may_have_changed is True
    assert ctl.active_attempt("tic-a1b2").id == attempt.id


# ---------------------------------------------------------------------------
# the operation lock across processes (both sinks)
# ---------------------------------------------------------------------------


def _try_operation_lock(kind, arbite_dir, timeout, results):
    try:
        sink = build_sink(SinkSpec(kind=kind), Path(arbite_dir))
        with sink.coordination().operation_lock(timeout=timeout):
            results.put("acquired")
    except CoordinationConflict:
        results.put("conflict")
    except Exception as error:  # pragma: no cover - surfaced through the queue
        results.put(f"error:{type(error).__name__}:{error}")


def test_operation_lock_excludes_other_processes(sink, kind, arbite_dir):
    store = sink.coordination()
    held = store.operation_lock()
    held.acquire()
    context = multiprocessing.get_context("spawn")

    blocked = context.Queue()
    process = context.Process(
        target=_try_operation_lock, args=(kind, str(arbite_dir), 0.3, blocked)
    )
    process.start()
    assert blocked.get(timeout=30) == "conflict"
    process.join(30)
    assert process.exitcode == 0

    held.release()

    after = context.Queue()
    process = context.Process(
        target=_try_operation_lock, args=(kind, str(arbite_dir), 5.0, after)
    )
    process.start()
    assert after.get(timeout=30) == "acquired"
    process.join(30)
    assert process.exitcode == 0


# ---------------------------------------------------------------------------
# the intent record itself
# ---------------------------------------------------------------------------


def test_operation_intent_record_round_trips_and_validates():
    moment = coordination.utc_now()
    intent = coordination.OperationIntent(
        id=coordination.new_record_id("operation_intent"),
        operation_id=coordination.new_operation_id(),
        workspace_id="ws-0123456789abcdef",
        attempt_id="att-0123456789abcdef",
        ticket_id="tic-a1b2",
        actor="claude.opus.001",
        kind_="write",
        created=moment,
        updated=moment,
        paths=["src/a.py"],
        before={"src/a.py": ABSENT},
        after={"src/a.py": coordination.digest_of_text("x\n")},
        state="pending",
    )
    assert intent.validate() == []
    assert intent.is_active is True
    reloaded = coordination.record_from_dict(intent.to_dict())
    assert reloaded.operation_kind == "write"
    assert reloaded.before == intent.before
    assert reloaded.state == "pending"

    intent.state = "not-a-state"
    assert intent.validate()
