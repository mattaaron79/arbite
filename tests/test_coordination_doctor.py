"""Integrity checks for a *stored* coordination store (planning key C11).

These tests run against both shipped sinks wherever the semantics are shared, so
"the doctor means the same thing whichever store is configured" is a checked claim.
Corruption is injected with *direct* storage writes (raw JSON files under
`coordination/`, or `sqlite3` UPDATE/INSERT), never through the public API -- the
API's strictness is part of what is being tested and is left alone.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from arbite import application, coordination as c, workspace as w
from arbite.application import Actor
from arbite.sinks import SQLITE_FILENAME, SinkSpec, build_sink
from arbite.sinks.file import FileSink, write_atomic
from arbite.sinks.coordination_file import FileCoordinationStore
from arbite.sinks.sqlite import DDL, SCHEMA_VERSION, SqliteSink
from arbite.coordination_doctor import coordination_problems
from helpers import make_ticket

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

COORDINATION_DIRS = ("records", "events", "events_by_operation", "bindings", "journal", "artifacts", "namespaces")


# ---------------------------------------------------------------------------
# Raw-storage tamper helpers
# ---------------------------------------------------------------------------


def _connect(arbite_dir):
    conn = sqlite3.connect(str(arbite_dir / SQLITE_FILENAME))
    conn.row_factory = sqlite3.Row
    return conn


def _record_path(arbite_dir, record_kind, record_id):
    return arbite_dir / "coordination" / "records" / record_kind / f"{record_id}.json"


def _read_envelope(kind, arbite_dir, record_kind, record_id):
    if kind == "file":
        return json.loads(_record_path(arbite_dir, record_kind, record_id).read_text(encoding="utf-8"))
    conn = _connect(arbite_dir)
    try:
        row = conn.execute(
            "SELECT payload FROM coordination_records WHERE kind = ? AND record_id = ?",
            (record_kind, record_id),
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row["payload"])


def _write_record_raw(kind, arbite_dir, record_kind, record_id, text):
    """Store `text` as the record payload, bypassing the API entirely."""
    if kind == "file":
        path = _record_path(arbite_dir, record_kind, record_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    conn = _connect(arbite_dir)
    try:
        cur = conn.execute(
            "UPDATE coordination_records SET payload = ? WHERE kind = ? AND record_id = ?",
            (text, record_kind, record_id),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO coordination_records (kind, record_id, revision, payload) "
                "VALUES (?, ?, ?, ?)",
                (record_kind, record_id, 1, text),
            )
        conn.commit()
    finally:
        conn.close()


def _write_envelope(kind, arbite_dir, record_kind, record_id, payload):
    _write_record_raw(kind, arbite_dir, record_kind, record_id, json.dumps(payload))


def _tamper_record_field(kind, arbite_dir, record_kind, record_id, field, value):
    envelope = _read_envelope(kind, arbite_dir, record_kind, record_id)
    envelope["record"][field] = value
    _write_envelope(kind, arbite_dir, record_kind, record_id, envelope)


def _tamper_revision(kind, arbite_dir, record_kind, record_id, revision):
    envelope = _read_envelope(kind, arbite_dir, record_kind, record_id)
    envelope["revision"] = revision
    _write_envelope(kind, arbite_dir, record_kind, record_id, envelope)


def _tamper_artifact_blob(kind, arbite_dir, digest, data):
    if kind == "file":
        path = arbite_dir / "coordination" / "artifacts" / digest[len("sha256:"):]
        path.write_bytes(data)
        return
    conn = _connect(arbite_dir)
    try:
        conn.execute(
            "UPDATE coordination_artifacts SET content = ? WHERE digest = ?",
            (sqlite3.Binary(data), digest),
        )
        conn.commit()
    finally:
        conn.close()


def _tamper_event_cursor(kind, arbite_dir, event, new_cursor):
    if kind == "file":
        path = arbite_dir / "coordination" / "events" / f"{int(event.cursor):012d}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["cursor"] = new_cursor
        path.write_text(json.dumps(payload), encoding="utf-8")
        return
    conn = _connect(arbite_dir)
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


def _tables(arbite_dir):
    conn = sqlite3.connect(str(arbite_dir / SQLITE_FILENAME))
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Building state
# ---------------------------------------------------------------------------


def service_for(sink, arbite_dir):
    return application.coordination_service_for(
        sink, root=str(arbite_dir.parent), actor=Actor("tester")
    )


def a_claim(workspace_id, attempt_id, *, path="src/a.py", generation=1, state="active", released=None):
    return c.FileClaim(
        id=c.new_record_id("file_claim"),
        workspace_id=workspace_id,
        path=path,
        ticket_id="tic-a1b2",
        attempt_id=attempt_id,
        generation=generation,
        acquired=c.utc_now(),
        observed_version=c.digest_of_text("alpha"),
        state=state,
        released=released,
    )


def an_intent(workspace_id, *, path, before, after, state="pending", operation_id=None):
    now = c.utc_now()
    return c.OperationIntent(
        id=c.new_record_id("operation_intent"),
        operation_id=operation_id or c.new_operation_id(),
        workspace_id=workspace_id,
        attempt_id="att-0000000000000000",
        ticket_id="tic-a1b2",
        actor="tester",
        kind_="write",
        created=now,
        updated=now,
        before={path: before},
        after={path: after},
        paths=[path],
        state=state,
    )


def populated(sink, arbite_dir):
    """A populated, *clean* coordination store: binding, finished attempt, released
    claim, a receipt/intent referencing a stored artifact and one event."""
    sink.create(make_ticket("tic-a1b2", status="open"))
    service = service_for(sink, arbite_dir)
    store = sink.coordination()
    workspace = service.workspace
    now = c.utc_now()

    attempt = c.WorkAttempt(
        id=c.new_record_id("work_attempt"),
        ticket_id="tic-a1b2",
        worker_id="tester",
        workspace_id=workspace.id,
        generation=1,
        started=now,
        last_activity=now,
        state="finished",
        ended=now,
        outcome="ok",
    )
    data = b"hello evidence"
    descriptor = store.store_artifact_bytes(data, media_type="text/plain")
    artifact = c.Artifact(
        id=descriptor.id,
        digest=descriptor.digest,
        size=descriptor.size,
        created=now,
        location=descriptor.location,
        media_type=descriptor.media_type,
    )
    claim = a_claim(
        workspace.id, attempt.id, generation=1, state="released", released=now
    )
    receipt = c.OperationReceipt(
        id=c.new_operation_id(),
        attempt_id=attempt.id,
        ticket_id="tic-a1b2",
        actor="tester",
        kind_="write",
        timestamp=now,
        before={"src/a.py": c.digest_of_bytes(b"old")},
        after={"src/a.py": c.digest_of_bytes(data)},
        paths=["src/a.py"],
        artifact_refs=[artifact.id],
        claim_generation=1,
    )
    intent = c.OperationIntent(
        id=c.new_record_id("operation_intent"),
        operation_id=receipt.id,
        workspace_id=workspace.id,
        attempt_id=attempt.id,
        ticket_id="tic-a1b2",
        actor="tester",
        kind_="write",
        created=now,
        updated=now,
        before={"src/a.py": c.digest_of_bytes(b"old")},
        after={"src/a.py": c.digest_of_bytes(data)},
        paths=["src/a.py"],
        artifact_refs=[artifact.id],
        claim_generation=1,
        state="finalized",
    )
    event = c.Event(
        id=c.new_record_id("event"),
        kind_="attempt_finished",
        category="lifecycle",
        timestamp=now,
        subject_ids=[attempt.id, "tic-a1b2"],
        payload={"workspace_id": workspace.id, "generation": 1},
    )
    with store.transaction() as tx:
        tx.put(attempt)
        tx.put(artifact)
        tx.put(claim)
        tx.put(receipt)
        tx.put(intent)
        tx.append_event(event)

    return {
        "service": service,
        "store": store,
        "workspace": workspace,
        "attempt": attempt,
        "artifact": artifact,
        "claim": claim,
        "receipt": receipt,
        "intent": intent,
        "event": event,
    }


# ---------------------------------------------------------------------------
# Clean and unchanged stores
# ---------------------------------------------------------------------------


def test_clean_store_has_no_coordination_problems(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    assert coordination_problems(sink) == []
    # ...and the wiring adds nothing to the ordinary doctor report either.
    assert sink.check() == []
    # The store really *was* checked: a deliberately seeded problem is reported.
    _tamper_revision(sink.kind, arbite_dir, state["claim"].kind, state["claim"].record_id, 0)
    assert any(
        p.kind == "coordination_revision_drift" for p in coordination_problems(sink)
    )


def test_uninitialised_store_reports_nothing_and_creates_nothing(kind, tmp_path):
    arbite_dir = tmp_path / ".arbite"
    sink = build_sink(SinkSpec(kind=kind), arbite_dir)
    assert sink.coordination().is_initialised() is False

    assert coordination_problems(sink) == []

    assert sink.coordination().is_initialised() is False
    assert not arbite_dir.exists()


def test_legacy_file_store_with_no_coordination_state_is_untouched(arbite_dir):
    sink = FileSink(arbite_dir)
    sink.init()
    write_atomic(make_ticket("tic-a1b2").to_markdown(), arbite_dir / "open" / "tic-a1b2.md")

    assert coordination_problems(sink) == []
    assert sink.check() == []
    assert not (arbite_dir / "coordination").exists()


def test_legacy_sqlite_store_with_tickets_but_no_coordination_tables(arbite_dir):
    db_path = arbite_dir / SQLITE_FILENAME
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(DDL)
        conn.execute(
            "INSERT INTO schema_version (version) SELECT ? WHERE NOT EXISTS "
            "(SELECT 1 FROM schema_version)",
            (SCHEMA_VERSION,),
        )
        conn.execute(
            "INSERT INTO tickets (id, title, status, type, tier, domain, body, created, updated) "
            "VALUES ('tic-a1b2', 'Legacy ticket', 'open', 'bug', 'medium', 'mesh', '', "
            "'2026-01-01', '2026-01-01')"
        )
        conn.commit()
    finally:
        conn.close()

    sink = SqliteSink(db_path)
    assert coordination_problems(sink) == []
    assert sink.check() == []
    assert "coordination_records" not in _tables(arbite_dir)
    assert "coordination_events" not in _tables(arbite_dir)


# ---------------------------------------------------------------------------
# 1. orphan claim
# ---------------------------------------------------------------------------


def test_orphan_claim_with_an_absent_attempt_is_reported(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    store = sink.coordination()
    orphan = a_claim(state["workspace"].id, c.new_record_id("work_attempt"), path="src/orphan.py")
    with store.transaction() as tx:
        tx.put(orphan)

    problems = [p for p in coordination_problems(sink) if p.kind == "coordination_orphan_claim"]
    assert problems
    assert any(orphan.id in p.detail for p in problems)
    assert all(p.fixed is False for p in problems)


# ---------------------------------------------------------------------------
# 2. invalid generation
# ---------------------------------------------------------------------------


def test_generation_zero_is_reported(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    store = sink.coordination()
    claim = a_claim(state["workspace"].id, state["attempt"].id, path="src/gen0.py")
    with store.transaction() as tx:
        tx.put(claim)
    _tamper_record_field(sink.kind, arbite_dir, claim.kind, claim.record_id, "generation", 0)

    problems = [p for p in coordination_problems(sink) if p.kind == "coordination_invalid_generation"]
    assert any(claim.id in p.detail and "generation" in p.detail for p in problems)
    assert all(p.fixed is False for p in problems)


def test_two_active_claims_for_one_path_are_reported(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    store = sink.coordination()
    first = a_claim(state["workspace"].id, state["attempt"].id, path="src/dup.py")
    second = a_claim(state["workspace"].id, state["attempt"].id, path="src/dup.py")
    with store.transaction() as tx:
        tx.put(first)
        tx.put(second)

    problems = [p for p in coordination_problems(sink) if p.kind == "coordination_invalid_generation"]
    assert any("src/dup.py" in p.detail and "active claim" in p.detail for p in problems)


def test_two_active_attempts_for_one_ticket_are_reported(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    store = sink.coordination()
    now = c.utc_now()
    attempts = [
        c.WorkAttempt(
            id=c.new_record_id("work_attempt"),
            ticket_id="tic-a1b2",
            worker_id="tester",
            workspace_id=state["workspace"].id,
            generation=generation,
            started=now,
            last_activity=now,
        )
        for generation in (2, 3)
    ]
    with store.transaction() as tx:
        for attempt in attempts:
            tx.put(attempt)

    problems = [p for p in coordination_problems(sink) if p.kind == "coordination_invalid_generation"]
    assert any("active attempt" in p.detail for p in problems)


def test_generation_regression_below_a_released_claim_is_reported(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    store = sink.coordination()
    released = a_claim(
        state["workspace"].id,
        state["attempt"].id,
        path="src/reg.py",
        generation=5,
        state="released",
        released=c.utc_now(),
    )
    active = a_claim(state["workspace"].id, state["attempt"].id, path="src/reg.py", generation=4)
    with store.transaction() as tx:
        tx.put(released)
        tx.put(active)

    problems = [p for p in coordination_problems(sink) if p.kind == "coordination_invalid_generation"]
    assert any(active.id in p.detail and "released generation" in p.detail for p in problems)


# ---------------------------------------------------------------------------
# 3. missing artifact
# ---------------------------------------------------------------------------


def test_reference_to_an_absent_artifact_is_reported(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    store = sink.coordination()
    now = c.utc_now()
    missing_id = c.new_record_id("artifact")
    receipt = c.OperationReceipt(
        id=c.new_operation_id(),
        attempt_id=state["attempt"].id,
        ticket_id="tic-a1b2",
        actor="tester",
        kind_="write",
        timestamp=now,
        artifact_refs=[missing_id],
    )
    with store.transaction() as tx:
        tx.put(receipt)

    problems = [p for p in coordination_problems(sink) if p.kind == "coordination_missing_artifact"]
    assert any(missing_id in p.detail for p in problems)
    assert all(p.fixed is False for p in problems)


def test_artifact_record_marked_unavailable_is_reported(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    store = sink.coordination()
    artifact = c.Artifact(
        id=c.new_record_id("artifact"),
        digest=c.digest_of_bytes(b"gone"),
        size=4,
        created=c.utc_now(),
        location="nowhere",
        available=False,
    )
    with store.transaction() as tx:
        tx.put(artifact)
    _tamper_record_field(sink.kind, arbite_dir, artifact.kind, artifact.record_id, "available", False)

    problems = [p for p in coordination_problems(sink) if p.kind == "coordination_missing_artifact"]
    assert any(artifact.id in p.detail for p in problems)


# ---------------------------------------------------------------------------
# 4. corrupt artifact
# ---------------------------------------------------------------------------


def test_tampered_artifact_blob_is_reported(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    store = sink.coordination()
    data = b"genuine bytes"
    descriptor = store.store_artifact_bytes(data)
    artifact = c.Artifact(
        id=descriptor.id,
        digest=descriptor.digest,
        size=descriptor.size,
        created=c.utc_now(),
        location=descriptor.location,
    )
    with store.transaction() as tx:
        tx.put(artifact)

    _tamper_artifact_blob(sink.kind, arbite_dir, descriptor.digest, b"tampered bytes!!")

    problems = [p for p in coordination_problems(sink) if p.kind == "coordination_artifact_corrupt"]
    assert any(descriptor.digest in p.detail for p in problems)
    assert all(p.fixed is False for p in problems)


def test_artifact_size_mismatch_is_reported(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    store = sink.coordination()
    data = b"size checked"
    descriptor = store.store_artifact_bytes(data)
    artifact = c.Artifact(
        id=descriptor.id,
        digest=descriptor.digest,
        size=descriptor.size,
        created=c.utc_now(),
        location=descriptor.location,
    )
    with store.transaction() as tx:
        tx.put(artifact)

    _tamper_record_field(
        sink.kind, arbite_dir, artifact.kind, artifact.record_id, "size", descriptor.size + 1
    )

    problems = [p for p in coordination_problems(sink) if p.kind == "coordination_artifact_corrupt"]
    assert any("bytes but its record claims" in p.detail for p in problems)


# ---------------------------------------------------------------------------
# 5. pending intent (observation against the real workspace bytes)
# ---------------------------------------------------------------------------


def _seed_intent_files(sink, arbite_dir):
    """A rooted workspace with three files, plus intents in each observation state."""
    state = populated(sink, arbite_dir)
    store = sink.coordination()
    workspace_id = state["workspace"].id
    (arbite_dir.parent / "src").mkdir(parents=True, exist_ok=True)
    before_bytes = b"before\n"
    after_bytes = b"after\n"

    (arbite_dir.parent / "src" / "before.py").write_bytes(before_bytes)
    (arbite_dir.parent / "src" / "after.py").write_bytes(after_bytes)
    (arbite_dir.parent / "src" / "drift.py").write_bytes(b"drifted\n")

    receipt = c.OperationReceipt(
        id=c.new_operation_id(),
        attempt_id=state["attempt"].id,
        ticket_id="tic-a1b2",
        actor="tester",
        kind_="write",
        timestamp=c.utc_now(),
    )
    before_intent = an_intent(
        workspace_id,
        path="src/before.py",
        before=c.digest_of_bytes(before_bytes),
        after=c.digest_of_bytes(after_bytes),
    )
    after_applied = an_intent(
        workspace_id,
        path="src/after.py",
        before=c.digest_of_bytes(before_bytes),
        after=c.digest_of_bytes(after_bytes),
    )
    after_finalized = an_intent(
        workspace_id,
        path="src/drift.py",
        before=c.digest_of_bytes(before_bytes),
        after=c.digest_of_bytes(b"drifted\n"),
        operation_id=receipt.id,
    )
    # The drift file must match neither side, so give it its own bytes.
    (arbite_dir.parent / "src" / "drift.py").write_bytes(b"drifted\n")
    # The "applied" intent's file must equal its after value: rewrite it.
    (arbite_dir.parent / "src" / "after.py").write_bytes(after_bytes)
    drift_intent = an_intent(
        workspace_id,
        path="src/drift2.py",
        before=c.digest_of_bytes(before_bytes),
        after=c.digest_of_bytes(after_bytes),
    )
    (arbite_dir.parent / "src" / "drift2.py").write_bytes(b"neither\n")

    with store.transaction() as tx:
        tx.put(receipt)
        tx.put(before_intent)
        tx.put(after_applied)
        tx.put(after_finalized)
        tx.put(drift_intent)
    return {
        "before": before_intent,
        "after_applied": after_applied,
        "after_finalized": after_finalized,
        "drift": drift_intent,
    }


def test_pending_intents_report_which_side_the_bytes_match(sink, arbite_dir):
    intents = _seed_intent_files(sink, arbite_dir)
    problems = {
        p.detail: p for p in coordination_problems(sink) if p.kind == "coordination_pending_intent"
    }
    assert problems
    for problem in problems.values():
        assert problem.fixed is False

    matched = {name: [d for d in problems if intent.id in d] for name, intent in intents.items()}
    assert any("before" in d for d in matched["before"])
    assert any("after" in d for d in matched["after_applied"])
    assert any("after" in d for d in matched["after_finalized"])
    assert any("drifted" in d for d in matched["drift"])

    # Drift is never auto-fixed; the other three are left untouched in plain mode.
    store = sink.coordination()
    with store.transaction(write=False) as tx:
        assert tx.get("operation_intent", intents["drift"].id).state == "pending"


def test_fix_applies_the_unambiguous_intent_transitions(sink, arbite_dir):
    intents = _seed_intent_files(sink, arbite_dir)
    store = sink.coordination()

    fixed = coordination_problems(sink, fix=True)
    by_detail = {p.detail: p for p in fixed if p.kind == "coordination_pending_intent"}
    assert by_detail
    assert any(intents["before"].id in d and p.fixed for d, p in by_detail.items())
    assert any(intents["after_applied"].id in d and p.fixed for d, p in by_detail.items())
    assert any(intents["after_finalized"].id in d and p.fixed for d, p in by_detail.items())
    assert any(intents["drift"].id in d and not p.fixed for d, p in by_detail.items())

    with store.transaction(write=False) as tx:
        assert tx.get("operation_intent", intents["before"].id).state == "reverted"
        assert tx.get("operation_intent", intents["after_applied"].id).state == "applied"
        assert tx.get("operation_intent", intents["after_finalized"].id).state == "finalized"
        assert tx.get("operation_intent", intents["drift"].id).state == "pending"


# ---------------------------------------------------------------------------
# 6. binding drift
# ---------------------------------------------------------------------------


def test_marker_disagreeing_with_the_binding_is_reported_and_never_rewritten(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    store = sink.coordination()
    marker_path = Path(store.binding_marker_path())
    assert marker_path.is_file()

    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["sink_kind"] = "sqlite" if marker.get("sink_kind") != "sqlite" else "file"
    marker_path.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
    before = marker_path.read_bytes()

    problems = [p for p in coordination_problems(sink) if p.kind == "coordination_binding_drift"]
    assert problems
    assert all(p.fixed is False for p in problems)

    # The marker mirror is authoritative-evidence-for-nobody: fix must not touch it.
    coordination_problems(sink, fix=True)
    assert marker_path.read_bytes() == before


def test_marker_naming_an_unbound_workspace_is_reported(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    store = sink.coordination()
    marker_path = Path(store.binding_marker_path())
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["workspace_id"] = c.new_record_id("workspace")
    marker_path.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")

    problems = [p for p in coordination_problems(sink) if p.kind == "coordination_binding_drift"]
    assert any("no stored binding" in p.detail for p in problems)


# ---------------------------------------------------------------------------
# 7. revision drift
# ---------------------------------------------------------------------------


def test_envelope_with_a_bad_revision_is_reported(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    claim = state["claim"]
    _tamper_revision(sink.kind, arbite_dir, claim.kind, claim.record_id, 0)

    problems = [p for p in coordination_problems(sink) if p.kind == "coordination_revision_drift"]
    assert any(claim.record_id in p.detail for p in problems)
    assert all(p.fixed is False for p in problems)


# ---------------------------------------------------------------------------
# 8. event cursor conflict
# ---------------------------------------------------------------------------


def test_duplicate_event_cursor_is_reported(sink, arbite_dir):
    store = sink.coordination()
    with store.transaction() as tx:
        first = tx.append_event(
            c.Event(
                id=c.new_record_id("event"),
                kind_="operation_recorded",
                category="operation",
                timestamp=c.utc_now(),
            )
        )
        second = tx.append_event(
            c.Event(
                id=c.new_record_id("event"),
                kind_="operation_recorded",
                category="operation",
                timestamp=c.utc_now(),
            )
        )
    _tamper_event_cursor(sink.kind, arbite_dir, second, first.cursor)

    problems = [
        p for p in coordination_problems(sink) if p.kind == "coordination_event_cursor_conflict"
    ]
    assert problems
    assert all(p.fixed is False for p in problems)


# ---------------------------------------------------------------------------
# 9. legacy record
# ---------------------------------------------------------------------------


def test_legacy_envelope_is_upgraded_only_when_it_validates(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    workspace = state["workspace"]
    _write_envelope(
        sink.kind, arbite_dir, workspace.kind, workspace.record_id, workspace.to_dict()
    )

    plain = [p for p in coordination_problems(sink) if p.kind == "coordination_legacy_record"]
    assert any(workspace.id in p.detail for p in plain)
    assert all(p.fixed is False for p in plain)

    fixed = coordination_problems(sink, fix=True)
    upgraded = [p for p in fixed if p.kind == "coordination_legacy_record"]
    assert any(workspace.id in p.detail and p.fixed for p in upgraded)

    store = sink.coordination()
    entry = next(
        e
        for e in store.inspect_records()
        if e["record_id"] == workspace.record_id and e["kind"] == "workspace"
    )
    assert entry["error"] is None
    assert entry["revision"] >= 1
    assert entry["payload"]["schema_version"] >= 1
    with store.transaction(write=False) as tx:
        assert tx.get("workspace", workspace.record_id) is not None


def test_invalid_legacy_payload_is_reported_but_not_upgraded(sink, arbite_dir):
    populated(sink, arbite_dir)
    invalid = {
        "kind": "file_claim",
        "id": c.new_record_id("file_claim"),
        "workspace_id": "ws-0000000000000000",
        "path": "src/a.py",
        "ticket_id": "tic-a1b2",
        "attempt_id": "att-0000000000000000",
        "generation": 0,
        "acquired": c.utc_now(),
        "observed_version": c.digest_of_text("alpha"),
        "state": "active",
        "released": None,
        "contract_version": 1,
    }
    _write_envelope(sink.kind, arbite_dir, invalid["kind"], invalid["id"], invalid)

    plain = [p for p in coordination_problems(sink) if p.kind == "coordination_legacy_record"]
    assert any(invalid["id"] in p.detail and not p.fixed for p in plain)

    coordination_problems(sink, fix=True)
    stored = _read_envelope(sink.kind, arbite_dir, invalid["kind"], invalid["id"])
    assert "schema_version" not in stored


# ---------------------------------------------------------------------------
# 10. unreadable record
# ---------------------------------------------------------------------------


def test_unreadable_record_is_reported(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    claim = state["claim"]
    _write_record_raw(sink.kind, arbite_dir, claim.kind, claim.record_id, "this is not JSON\n")

    problems = [
        p for p in coordination_problems(sink) if p.kind == "coordination_record_unreadable"
    ]
    assert any(claim.record_id in p.detail for p in problems)
    assert all(p.fixed is False for p in problems)


# ---------------------------------------------------------------------------
# 11/12. pending journal and derived operation index (file sink only)
# ---------------------------------------------------------------------------


def _crash_with_a_leftover_journal(root: str, claim_id: str) -> None:  # pragma: no cover
    from arbite.coordination_storage import CRASH_AFTER_JOURNAL

    store = FileCoordinationStore(Path(root), crash_point=CRASH_AFTER_JOURNAL)
    with store.transaction() as tx:
        tx.put(
            c.FileClaim(
                id=claim_id,
                workspace_id="ws-0000000000000000",
                path="src/a.py",
                ticket_id="tic-a1b2",
                attempt_id="att-0000000000000000",
                generation=1,
                acquired=c.utc_now(),
                observed_version=c.digest_of_text("alpha"),
            )
        )
    os._exit(0)


def test_pending_journal_is_reported_and_replayed_by_fix(kind, arbite_dir):
    if kind != "file":
        pytest.skip("only the file sink leaves a write-ahead journal")
    coordination_dir = arbite_dir / "coordination"
    claim_id = c.new_record_id("file_claim")
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_with_a_leftover_journal, args=(str(coordination_dir), claim_id)
    )
    process.start()
    process.join(60)
    assert process.exitcode == 97

    store = FileCoordinationStore(coordination_dir)
    pending = store.pending_journals()
    assert len(pending) == 1

    plain = [p for p in coordination_problems(store) if p.kind == "coordination_pending_operation"]
    assert plain and all(p.fixed is False for p in plain)
    # A plain report never repairs: the journal is still there.
    assert store.pending_journals() == pending

    fixed = coordination_problems(store, fix=True)
    assert any(
        p.kind == "coordination_pending_operation" and p.fixed for p in fixed
    )
    assert store.pending_journals() == []


def test_missing_event_operation_index_is_reported_and_rebuilt(kind, sink, arbite_dir):
    if kind != "file":
        pytest.skip("only the file sink keeps a derived per-operation index")
    store = sink.coordination()
    operation_id = "op-0123456789abcdef"
    with store.transaction() as tx:
        tx.append_event(
            c.Event(
                id=c.new_record_id("event"),
                kind_="operation_recorded",
                category="operation",
                timestamp=c.utc_now(),
                operation_id=operation_id,
            )
        )
    index_path = arbite_dir / "coordination" / "events_by_operation" / f"{operation_id}.json"
    assert index_path.exists()
    index_path.unlink()

    plain = [
        p for p in coordination_problems(sink) if p.kind == "coordination_stale_operation_index"
    ]
    assert plain and all(p.fixed is False for p in plain)

    fixed = coordination_problems(sink, fix=True)
    assert any(
        p.kind == "coordination_stale_operation_index" and p.fixed for p in fixed
    )
    assert store.missing_event_operation_indexes() == []


# ---------------------------------------------------------------------------
# Problem shape and CLI exit codes
# ---------------------------------------------------------------------------


PROBLEM_KEYS = {"kind", "detail", "id", "path", "fixed"}


def test_problem_to_dict_shape_is_unchanged(sink, arbite_dir):
    state = populated(sink, arbite_dir)
    claim = state["claim"]
    _tamper_revision(sink.kind, arbite_dir, claim.kind, claim.record_id, 0)

    problems = coordination_problems(sink)
    assert problems
    for problem in problems:
        assert set(problem.to_dict()) == PROBLEM_KEYS


def _run_cli(project, *args, sink=None, expect=0):
    environment = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    environment.pop("ARBITE_SINK", None)
    if sink:
        environment["ARBITE_SINK"] = sink
    proc = subprocess.run(
        [sys.executable, "-m", "arbite.cli", *args],
        cwd=str(project),
        env=environment,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == expect, (
        f"arbite {' '.join(args)} -> exit {proc.returncode}, expected {expect}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc


def test_cli_doctor_exits_three_on_a_coordination_problem_and_fixes_it(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _run_cli(project, "init", sink="file")

    # Seed a legacy (unversioned) WorkAttempt directly under the coordination tree.
    record_dir = project / ".arbite" / "coordination" / "records" / "work_attempt"
    record_dir.mkdir(parents=True)
    now = c.utc_now()
    record = {
        "kind": "work_attempt",
        "id": "att-0000000000000000",
        "ticket_id": "tic-a1b2",
        "worker_id": "tester",
        "workspace_id": "ws-0000000000000000",
        "generation": 1,
        "started": now,
        "last_activity": now,
        "state": "active",
        "ended": None,
        "outcome": None,
        "handoff": None,
        "contract_version": 1,
    }
    (record_dir / "att-0000000000000000.json").write_text(json.dumps(record), encoding="utf-8")

    report = json.loads(_run_cli(project, "doctor", "--json", expect=3, sink="file").stdout)
    assert any(
        p["kind"] == "coordination_legacy_record" for p in report["problems"]
    )

    fixed = json.loads(_run_cli(project, "doctor", "--json", "--fix", sink="file").stdout)
    assert fixed["remaining"] == 0
    assert fixed["fixed"] >= 1
    payload = json.loads((record_dir / "att-0000000000000000.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] >= 1 and payload["revision"] >= 1
