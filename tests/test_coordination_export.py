"""Export, verification, import and migration of coordination history (C11).

The shared-behaviour tests run unchanged against both shipped sinks via the
`kind`/`sink` fixtures, so "export/import behaves the same whichever store is
configured" is a checked claim. The round-trip and migration tests build the two
kinds explicitly (file -> SQLite -> file).

The populated fixture is deliberately *quiescent*: the attempt is finished, the
claim is released and the intent is finalized, so `migrate_coordination` may run.
"""

from __future__ import annotations

import base64
import copy
import json

import pytest

from arbite import application, coordination as c, coordination_export as x
from arbite.application import Actor
from arbite.errors import CoordinationConflict, InvalidRecord
from conftest import make_sink
from helpers import make_ticket


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def other_kind(kind: str) -> str:
    return "sqlite" if kind == "file" else "file"


def service_for(sink, arbite_dir):
    """The service the CLI would build, so the workspace/binding is real."""
    return application.coordination_service_for(
        sink, root=str(arbite_dir.parent), actor=Actor("tester")
    )


def populate(sink, arbite_dir):
    """A populated, quiescent workspace: attempt, claim, receipt, observation,
    finalized intent, a stored artifact and its events."""
    service = service_for(sink, arbite_dir)
    store = sink.coordination()
    workspace = service.workspace

    sink.create(make_ticket("tic-a1b2", status="in_progress", assignee="tester"))
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
    artifact_bytes = b"hello evidence"
    artifact = store.store_artifact_bytes(artifact_bytes, media_type="text/plain")
    claim = c.FileClaim(
        id=c.new_record_id("file_claim"),
        workspace_id=workspace.id,
        path="src/a.py",
        ticket_id="tic-a1b2",
        attempt_id=attempt.id,
        generation=1,
        acquired=now,
        observed_version=c.digest_of_bytes(b"old"),
        state="released",
        released=now,
    )
    receipt = c.OperationReceipt(
        id=c.new_record_id("operation_receipt"),
        attempt_id=attempt.id,
        ticket_id="tic-a1b2",
        actor="tester",
        kind_="write",
        timestamp=now,
        before={"src/a.py": c.digest_of_bytes(b"old")},
        after={"src/a.py": c.digest_of_bytes(b"new")},
        paths=["src/a.py"],
        artifact_refs=[artifact.id],
        claim_generation=1,
    )
    observation = c.ReadObservation(
        id=c.new_record_id("read_observation"),
        operation_id=receipt.id,
        path="src/a.py",
        digest=c.digest_of_bytes(b"old"),
        observed_at=now,
        attempt_id=attempt.id,
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
        after={"src/a.py": c.digest_of_bytes(b"new")},
        paths=["src/a.py"],
        artifact_refs=[artifact.id],
        claim_generation=1,
        state="finalized",
    )
    events = [
        c.Event(
            id=c.new_record_id("event"), cursor=None, kind_="attempt_finished",
            category="lifecycle", timestamp=now,
            subject_ids=[attempt.id, "tic-a1b2"],
            payload={"workspace_id": workspace.id, "generation": 1},
        ),
        c.Event(
            id=c.new_record_id("event"), cursor=None, kind_="claim_released",
            category="claim", timestamp=now,
            subject_ids=[claim.id, attempt.id, "tic-a1b2"],
            payload={"workspace_id": workspace.id, "path": "src/a.py"},
        ),
        c.Event(
            id=c.new_record_id("event"), cursor=None, kind_="operation_recorded",
            category="operation", timestamp=now,
            subject_ids=[attempt.id, "tic-a1b2"], operation_id=receipt.id,
            payload={"operation_kind": "write"},
        ),
        c.Event(
            id=c.new_record_id("event"), cursor=None, kind_="read_observed",
            category="read", timestamp=now, subject_ids=[attempt.id],
            operation_id=receipt.id, payload={"path": "src/a.py"},
        ),
    ]

    with store.transaction() as tx:
        tx.put(attempt)
        tx.put(artifact)
        tx.put(claim)
        tx.put(receipt)
        tx.put(observation)
        tx.put(intent)
        for event in events:
            tx.append_event(event)

    return {
        "service": service,
        "workspace": workspace,
        "attempt": attempt,
        "artifact": artifact,
        "claim": claim,
        "receipt": receipt,
        "observation": observation,
        "intent": intent,
        "events": events,
    }


def event_signature(events):
    """Events compared modulo their (store-local, freshly assigned) cursor."""
    return sorted(
        (
            event["id"],
            event["event_kind"],
            event.get("operation_id"),
            tuple(event.get("subject_ids") or []),
        )
        for event in events
    )


# ---------------------------------------------------------------------------
# Bundle shape and versioning
# ---------------------------------------------------------------------------


def test_bundle_shape_and_versioning(kind, sink, arbite_dir):
    state = populate(sink, arbite_dir)
    store = sink.coordination()
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)

    for key in x.BUNDLE_KEYS:
        assert key in bundle, key
    assert bundle["schema_version"] == x.EXPORT_VERSION
    assert bundle["arbite_coordination_export"] == x.EXPORT_VERSION
    assert bundle["retained_history"] is True
    assert bundle["contract_version"] == store.contract_version()
    assert bundle["cursor_namespace"] == store.cursor_namespace()
    assert bundle["workspace_id"] == state["workspace"].id
    assert bundle["bound_at"] == state["service"].workspace.store_binding.bound_at

    assert set(bundle["records"]) == set(x.RECORD_GROUPS)
    for group in x.RECORD_GROUPS:
        assert isinstance(bundle["records"][group], list)
    assert bundle["counts"] == x.bundle_counts(bundle)
    assert bundle["counts"]["work_attempts"] == 1
    assert bundle["counts"]["operation_receipts"] == 1
    assert bundle["counts"]["events"] == len(bundle["events"]) >= 4
    assert bundle["counts"]["artifacts"] == 1

    # Every event names the store its source cursor belongs to.
    for event in bundle["events"]:
        assert event["source_namespace"] == bundle["cursor_namespace"]
        assert "source_cursor" in event
        assert event["source_cursor"] is not None


def test_export_of_uninitialised_store_is_empty_and_creates_nothing(kind, arbite_dir):
    sink = make_sink(kind, arbite_dir, initialise=False)
    store = sink.coordination()
    assert store.is_initialised() is False

    bundle = x.export_coordination(sink)

    assert store.is_initialised() is False
    assert bundle["cursor_namespace"] == store.cursor_namespace()
    assert bundle["contract_version"] == store.contract_version()
    assert bundle["records"] == {group: [] for group in x.RECORD_GROUPS}
    assert bundle["events"] == []
    assert bundle["artifacts"] == []
    assert bundle["counts"]["events"] == 0


def test_clean_bundle_has_no_problems(kind, sink, arbite_dir):
    state = populate(sink, arbite_dir)
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)
    assert x.bundle_problems(bundle) == []


# ---------------------------------------------------------------------------
# write_bundle / read_bundle
# ---------------------------------------------------------------------------


def test_write_and_read_bundle_round_trip(kind, sink, arbite_dir, tmp_path):
    state = populate(sink, arbite_dir)
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)

    path = tmp_path / "bundle.json"
    returned = x.write_bundle(bundle, path)
    assert returned == str(path)
    assert x.read_bundle(path) == bundle
    # The file is real JSON with a trailing newline and sorted keys.
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert list(json.loads(text)) == sorted(json.loads(text))


def test_read_bundle_rejects_truncated_wrong_version_and_tampered(kind, sink, arbite_dir, tmp_path):
    state = populate(sink, arbite_dir)
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)
    path = tmp_path / "bundle.json"
    x.write_bundle(bundle, path)
    text = path.read_text(encoding="utf-8")

    # Truncated raw bytes.
    truncated = tmp_path / "truncated.json"
    truncated.write_bytes(text[: len(text) // 2].encode("utf-8"))
    with pytest.raises(InvalidRecord):
        x.read_bundle(truncated)

    # Wrong version.
    wrong = copy.deepcopy(bundle)
    wrong["schema_version"] = 999
    wrong_path = tmp_path / "wrong.json"
    wrong_path.write_text(json.dumps(wrong), encoding="utf-8")
    with pytest.raises(InvalidRecord):
        x.read_bundle(wrong_path)

    # Tampered shape: a required record group is gone.
    tampered = copy.deepcopy(bundle)
    del tampered["records"]["work_attempts"]
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(InvalidRecord):
        x.read_bundle(tampered_path)


# ---------------------------------------------------------------------------
# bundle_problems: purity and stable kinds
# ---------------------------------------------------------------------------


def _kinds(problems):
    return {p.kind for p in problems}


def test_bundle_problems_never_mutates(kind, sink, arbite_dir):
    state = populate(sink, arbite_dir)
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)
    snapshot = copy.deepcopy(bundle)

    problems = x.bundle_problems(bundle)

    assert problems == []
    assert bundle == snapshot


def test_bundle_problems_flags_a_corrupt_artifact(kind, sink, arbite_dir):
    state = populate(sink, arbite_dir)
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)
    entry = bundle["artifacts"][0]
    raw = bytearray(base64.b64decode(entry["data_b64"]))
    raw[0] ^= 0xFF
    entry["data_b64"] = base64.b64encode(bytes(raw)).decode("ascii")

    assert "coordination_artifact_corrupt" in _kinds(x.bundle_problems(bundle))


def test_bundle_problems_flags_a_size_mismatch(kind, sink, arbite_dir):
    state = populate(sink, arbite_dir)
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)
    bundle["artifacts"][0]["size"] = bundle["artifacts"][0]["size"] + 1
    assert "coordination_artifact_corrupt" in _kinds(x.bundle_problems(bundle))


def test_bundle_problems_flags_a_missing_artifact(kind, sink, arbite_dir):
    state = populate(sink, arbite_dir)
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)
    bundle["artifacts"] = []
    assert "coordination_missing_artifact" in _kinds(x.bundle_problems(bundle))


def test_bundle_problems_flags_an_orphan_claim(kind, sink, arbite_dir):
    state = populate(sink, arbite_dir)
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)
    orphan = dict(bundle["records"]["file_claims"][0])
    orphan["id"] = "clm-00000000000000aa"
    orphan["attempt_id"] = "att-00000000000000bb"
    bundle["records"]["file_claims"].append(orphan)
    assert "coordination_orphan_claim" in _kinds(x.bundle_problems(bundle))


def test_bundle_problems_flags_an_invalid_artifact_ref(kind, sink, arbite_dir):
    state = populate(sink, arbite_dir)
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)
    bundle["records"]["operation_receipts"][0]["artifact_refs"] = ["sha256:not-a-digest"]
    assert "coordination_bundle_invalid" in _kinds(x.bundle_problems(bundle))


# ---------------------------------------------------------------------------
# Artifact omission
# ---------------------------------------------------------------------------


def test_include_artifacts_false_omits_data_and_marks_it(kind, sink, arbite_dir):
    state = populate(sink, arbite_dir)
    bundle = x.export_coordination(
        sink, workspace_id=state["workspace"].id, include_artifacts=False
    )
    entry = bundle["artifacts"][0]
    assert "data_b64" not in entry
    assert entry["data_omitted"] is True
    # A metadata-only bundle declares its omission, so it is not "missing data".
    assert "coordination_missing_artifact" not in _kinds(x.bundle_problems(bundle))


# ---------------------------------------------------------------------------
# Import: fresh cursors, namespace registry, foreign bindings
# ---------------------------------------------------------------------------


def test_import_into_other_kind_assigns_fresh_cursors_and_records_namespace(kind, sink, arbite_dir):
    state = populate(sink, arbite_dir)
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)

    target = make_sink(other_kind(kind), arbite_dir)
    target_store = target.coordination()

    # Seed the destination so its cursors demonstrably start above the source's.
    seed = c.Event(
        id=c.new_record_id("event"), cursor=None, kind_="workspace_bound",
        category="lifecycle", timestamp=c.utc_now(), subject_ids=[], payload={},
    )
    with target_store.transaction() as tx:
        tx.append_event(seed)
    assert seed.cursor == 1

    result = x.import_coordination(target, bundle)

    source_event_ids = {event["id"] for event in bundle["events"]}
    imported = [e for e in target_store.event_log() if e.id in source_event_ids]
    destination_cursors = sorted(e.cursor for e in imported)
    source_cursors = sorted(event["source_cursor"] for event in bundle["events"])

    assert destination_cursors == list(range(2, 2 + len(bundle["events"])))
    assert destination_cursors != source_cursors
    assert result["events"] == len(bundle["events"])
    assert result["source_namespace"] == bundle["cursor_namespace"]
    assert result["target_namespace"] == target_store.cursor_namespace()

    registry = target_store.namespaces()
    assert len(registry) == 1
    entry = registry[0]
    assert entry["namespace"] == bundle["cursor_namespace"]
    assert entry["source_contract_version"] == bundle["contract_version"]
    assert set(entry["cursor_map"]) == {str(cursor) for cursor in source_cursors}
    assert sorted(entry["cursor_map"].values()) == destination_cursors


def test_foreign_store_bindings_are_skipped(kind, sink, arbite_dir):
    state = populate(sink, arbite_dir)
    store = sink.coordination()
    workspace = state["workspace"]
    # A store_binding *record* naming a foreign location.
    foreign = c.StoreBinding(
        id=c.new_record_id("store_binding"),
        workspace_id=workspace.id,
        sink_kind="file",
        location="/tmp/foreign/.arbite",
        bound_at=c.utc_now(),
    )
    with store.transaction() as tx:
        tx.put(foreign)

    bundle = x.export_coordination(sink, workspace_id=workspace.id)
    assert any(b["id"] == foreign.id for b in bundle["records"]["store_bindings"])

    target = make_sink(other_kind(kind), arbite_dir)
    result = x.import_coordination(target, bundle)

    assert result["bindings_skipped"] >= 1
    # Nothing foreign was written: the destination still has no binding.
    assert target.coordination().store_binding(workspace.id) is None


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_repeat_import_does_not_duplicate_events_or_namespaces(kind, sink, arbite_dir):
    state = populate(sink, arbite_dir)
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)

    target = make_sink(other_kind(kind), arbite_dir)
    target_store = target.coordination()

    first = x.import_coordination(target, bundle)
    count_after_first = len(target_store.event_log())
    namespaces_after_first = len(target_store.namespaces())

    second = x.import_coordination(target, bundle)

    assert len(target_store.event_log()) == count_after_first
    assert second["events"] == 0
    assert second["skipped_existing"] > 0
    assert len(target_store.namespaces()) == namespaces_after_first == 1
    assert first["events"] == len(bundle["events"])


# ---------------------------------------------------------------------------
# Full file -> SQLite -> file round trip
# ---------------------------------------------------------------------------


def test_file_sqlite_file_round_trip(tmp_path):
    src_dir = tmp_path / "src" / ".arbite"
    src_dir.mkdir(parents=True)
    file_sink = make_sink("file", src_dir)
    state = populate(file_sink, src_dir)
    workspace_id = state["workspace"].id

    first = x.export_coordination(file_sink, workspace_id=workspace_id)
    assert x.bundle_problems(first) == []

    mid_dir = tmp_path / "mid" / ".arbite"
    mid_dir.mkdir(parents=True)
    sqlite_sink = make_sink("sqlite", mid_dir)
    first_hop = x.migrate_coordination(file_sink, sqlite_sink, workspace_id=workspace_id)
    assert first_hop["verified"] is True

    second = x.export_coordination(sqlite_sink, workspace_id=workspace_id)
    assert second["records"] == first["records"]
    assert event_signature(second["events"]) == event_signature(first["events"])

    dst_dir = tmp_path / "dst" / ".arbite"
    dst_dir.mkdir(parents=True)
    file_sink_2 = make_sink("file", dst_dir)
    second_hop = x.migrate_coordination(sqlite_sink, file_sink_2, workspace_id=workspace_id)
    assert second_hop["verified"] is True

    third = x.export_coordination(file_sink_2, workspace_id=workspace_id)
    assert third["records"] == first["records"]
    assert event_signature(third["events"]) == event_signature(first["events"])
    assert (
        third["records"]["operation_receipts"][0]["artifact_refs"]
        == first["records"]["operation_receipts"][0]["artifact_refs"]
    )


# ---------------------------------------------------------------------------
# Migration refusals
# ---------------------------------------------------------------------------


def test_import_refuses_a_tampered_bundle_and_leaves_the_destination_untouched(tmp_path):
    src_dir = tmp_path / "src" / ".arbite"
    src_dir.mkdir(parents=True)
    file_sink = make_sink("file", src_dir)
    state = populate(file_sink, src_dir)
    bundle = x.export_coordination(file_sink, workspace_id=state["workspace"].id)

    tampered = copy.deepcopy(bundle)
    raw = bytearray(base64.b64decode(tampered["artifacts"][0]["data_b64"]))
    raw[0] ^= 0xFF
    tampered["artifacts"][0]["data_b64"] = base64.b64encode(bytes(raw)).decode("ascii")

    dst_dir = tmp_path / "dst" / ".arbite"
    dst_dir.mkdir(parents=True)
    target = make_sink("file", dst_dir)

    with pytest.raises(InvalidRecord):
        x.import_coordination(target, tampered, verify=True)

    # Untouched: no events, no records, and the store was never initialised.
    assert target.coordination().is_initialised() is False
    after = x.export_coordination(target, workspace_id=state["workspace"].id)
    assert after["events"] == []
    assert after["counts"] == x.bundle_counts(after)
    assert all(value == 0 for value in after["counts"].values())


# ---------------------------------------------------------------------------
# Quiescence
# ---------------------------------------------------------------------------


def test_quiescence_blockers_detect_active_work(kind, sink, arbite_dir):
    state = populate(sink, arbite_dir)
    store = sink.coordination()
    workspace = state["workspace"]

    # Quiescent baseline.
    assert x.quiescence_blockers(store, workspace.id) == {
        "active_attempt_ids": [],
        "active_claim_paths": [],
        "pending_intent_ids": [],
        "pending_operations": [],
    }
    x.require_quiescent_store(store, workspace.id)

    now = c.utc_now()
    active = c.WorkAttempt(
        id=c.new_record_id("work_attempt"),
        ticket_id="tic-active",
        worker_id="tester",
        workspace_id=workspace.id,
        generation=1,
        started=now,
        last_activity=now,
    )
    with store.transaction() as tx:
        tx.put(active)

    blockers = x.quiescence_blockers(store, workspace.id)
    assert blockers["active_attempt_ids"] == [active.id]

    with pytest.raises(CoordinationConflict) as excinfo:
        x.require_quiescent_store(store, workspace.id)
    assert active.id in str(excinfo.value)
    assert active.id in excinfo.value.details["active_attempt_ids"]


def test_quiescence_of_an_uninitialised_store_creates_nothing(kind, arbite_dir):
    sink = make_sink(kind, arbite_dir, initialise=False)
    store = sink.coordination()
    blockers = x.quiescence_blockers(store, "ws-00000000000000aa")
    assert blockers == {
        "active_attempt_ids": [],
        "active_claim_paths": [],
        "pending_intent_ids": [],
        "pending_operations": [],
    }
    x.require_quiescent_store(store, "ws-00000000000000aa")
    assert store.is_initialised() is False


def test_migrate_refuses_a_non_quiescent_source_and_touches_nothing(tmp_path):
    src_dir = tmp_path / "src" / ".arbite"
    src_dir.mkdir(parents=True)
    file_sink = make_sink("file", src_dir)
    state = populate(file_sink, src_dir)
    store = file_sink.coordination()
    now = c.utc_now()
    active = c.WorkAttempt(
        id=c.new_record_id("work_attempt"),
        ticket_id="tic-active",
        worker_id="tester",
        workspace_id=state["workspace"].id,
        generation=1,
        started=now,
        last_activity=now,
    )
    with store.transaction() as tx:
        tx.put(active)

    dst_dir = tmp_path / "dst" / ".arbite"
    dst_dir.mkdir(parents=True)
    target = make_sink("file", dst_dir)

    with pytest.raises(CoordinationConflict):
        x.migrate_coordination(file_sink, target, workspace_id=state["workspace"].id)

    after = x.export_coordination(target, workspace_id=state["workspace"].id)
    assert after["events"] == []
    assert all(value == 0 for value in after["counts"].values())


def test_migrate_requires_a_workspace_when_several_exist(tmp_path):
    src_dir = tmp_path / "src" / ".arbite"
    src_dir.mkdir(parents=True)
    file_sink = make_sink("file", src_dir)
    populate(file_sink, src_dir)
    store = file_sink.coordination()
    other = c.Workspace(
        id=c.new_record_id("workspace"),
        root="/tmp/another",
        created=c.utc_now(),
        updated=c.utc_now(),
    )
    with store.transaction() as tx:
        tx.put(other)

    dst_dir = tmp_path / "dst" / ".arbite"
    dst_dir.mkdir(parents=True)
    target = make_sink("file", dst_dir)

    with pytest.raises(CoordinationConflict):
        x.migrate_coordination(file_sink, target)
