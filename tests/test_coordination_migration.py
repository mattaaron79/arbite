"""The scenario this slice owns: the multi-record round trip, and what makes a switch unsafe.

There is no frozen block for a store transfer -- the examples document lists this ticket as
"no single-command scenarios; multi-record round trip" -- so the acceptance target is the
handoff's own sentence: *attempts, events and artifacts survive a file to SQLite to file
transfer while quiescent*. These tests assert exactly that against real stores built by the
real commands, then assert the refusals that keep a switch from being attempted while work is
live, or into a destination that holds work of its own.

Document-level equality is what makes the claim checkable: the same records with the same
event cursors and the same write counters on both sides of the trip, not merely equal counts.
Both sinks are exercised wherever the answer has to be the same; the backend-specific
statements (a size limit, a hand-written older document) are made where the storage is.
"""

from __future__ import annotations

import json

import pytest

import migration_state as state
from arbite.coordination import migrate as coordination_migrate
from arbite.errors import CoordinationError
from arbite.coordination import records as coordination_records
from arbite.coordination import store as coordination_store


@pytest.fixture(params=state.SINKS)
def quiescent(request, tmp_path):
    """A rich store with nothing live, on each sink."""
    return state.quiescent(tmp_path, request.param)


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_C12_a_file_to_sqlite_to_file_round_trip_preserves_every_record(tmp_path):
    """The scenario, end to end: every record, its revision and its evidence survive.

    A quiescent file store holding one attempt's five operations (with the claims, read
    observations, receipts and artifacts behind them) is copied to SQLite and back. The
    comparisons are of the *stored documents* and the write counters, so a transfer that
    renumbered a cursor, re-minted an id or dropped a completed operation's evidence fails
    here even though the record counts would still match."""
    project = state.quiescent(tmp_path, "file")
    start = state.snapshot(state.store_for(project, "file"))
    assert start["claim"] and start["receipt"] and start["artifact"] and start["event"]

    state.run(project, "migrate", "--to", "sqlite")
    assert state.snapshot(state.store_for(project, "sqlite")) == start, (
        "a copy is the same records, not merely the same number of them"
    )

    state.run(project, "migrate", "--from", "sqlite", "--to", "file", "--overwrite")
    assert state.snapshot(state.store_for(project, "file")) == start, (
        "the round trip returns the store it started from"
    )

    # The evidence travelled with the records: every version a receipt names is still there
    # and still hashes to its own digest, on both sinks.
    for sink_kind in state.SINKS:
        store = state.store_for(project, sink_kind)
        digests = [artifact.digest for artifact in store.records("artifact")]
        assert digests, "the fixture stores evidence"
        for digest in digests:
            assert store.verify_artifact(digest)

    assert state.run(project, "doctor").returncode == 0
    assert state.run(project, "doctor", sink_kind="sqlite").returncode == 0


def test_C12_the_round_trip_keeps_generations_actors_operation_ids_and_cursors(tmp_path):
    """The named facts of the scenario, asserted one by one rather than only as a whole.

    The document-level comparison above already covers these; this test is what fails
    *legibly* when one of them is the thing that broke, and it is the list the acceptance
    criteria use."""
    project = state.quiescent(tmp_path, "file")
    before = state.store_for(project, "file")
    start = state.snapshot(before)

    claims = {claim.path: claim for claim in before.records("claim")}
    receipts = {receipt.id: receipt for receipt in before.receipts()}
    cursors = [event.cursor for event in before.events()]
    attempts = {attempt.id: attempt for attempt in before.records("attempt")}
    assert claims and receipts and cursors and attempts

    state.run(project, "migrate", "--to", "sqlite")
    after = state.store_for(project, "sqlite")

    moved_claims = {claim.path: claim for claim in after.records("claim")}
    assert set(moved_claims) == set(claims)
    for path, claim in claims.items():
        moved = moved_claims[path]
        assert moved.generation == claim.generation
        assert moved.attempt_id == claim.attempt_id
        assert moved.observed_version == claim.observed_version
        assert moved.state == claim.state

    moved_attempts = {attempt.id: attempt for attempt in after.records("attempt")}
    assert set(moved_attempts) == set(attempts)
    for attempt_id, attempt in attempts.items():
        assert moved_attempts[attempt_id].worker_id == attempt.worker_id
        assert moved_attempts[attempt_id].generation == attempt.generation
        assert moved_attempts[attempt_id].outcome == attempt.outcome

    moved_receipts = {receipt.id: receipt for receipt in after.receipts()}
    assert set(moved_receipts) == set(receipts)
    for operation_id, receipt in receipts.items():
        moved = moved_receipts[operation_id]
        assert moved.actor == receipt.actor
        assert moved.attempt_id == receipt.attempt_id
        assert moved.before == receipt.before and moved.after == receipt.after
        assert moved.result == receipt.result
        # The write counter a read token means travels too, or a copy would silently reset
        # the history an optimistic write contends on.
        assert start["receipt"][operation_id][1] == state.snapshot(after)["receipt"][
            operation_id
        ][1]

    assert [event.cursor for event in after.events()] == cursors
    assert [event.id for event in after.events()] == [event.id for event in before.events()]


def test_C12_both_stores_answer_the_same_questions_after_the_switch(tmp_path):
    """Cross-sink parity: the same event stream and the same receipt summary on both.

    The store is supposed to mean the same thing wherever it lives, and the switch is where a
    difference would show up. Both views are compared byte for byte."""
    project = state.quiescent(tmp_path, "file")
    state.run(project, "migrate", "--to", "sqlite")

    assert state.run(project, "events", "--tail", "100", sink_kind="file").stdout == state.run(
        project, "events", "--tail", "100", sink_kind="sqlite"
    ).stdout

    summary = state.run(project, "receipt", "--summary", sink_kind="file").stdout
    assert summary.startswith("receipt summary: ")
    assert summary == state.run(project, "receipt", "--summary", sink_kind="sqlite").stdout


# ---------------------------------------------------------------------------
# Refusals: live work, and a destination that holds work of its own
# ---------------------------------------------------------------------------


def test_C12_a_switch_is_refused_while_work_is_active(tmp_path):
    """Live ownership stops the switch, and says who holds what and what to do next.

    A migration that moved the store under a working agent would leave the work in one store
    and the ownership in another, so nothing is copied: the destination is not created, the
    source is untouched, and the refusal is outcome 4 -- busy, not an error."""
    project, attempt = state.live(tmp_path, "file")
    proc = state.run(project, "migrate", "--to", "sqlite", expect=4)

    assert "busy:" in proc.stderr
    assert "coordination_active_work" in proc.stderr
    assert attempt in proc.stderr and state.SCHEMA_PY in proc.stderr
    assert "no coordination records were copied and no tickets were moved" in proc.stderr
    assert "arbite doctor" in proc.stderr, "the refusal names what to do about it"

    assert not (project / ".arbite" / "arbite.db").exists(), "nothing was created"
    store = state.store_for(project, "file")
    assert store.active_claims() and store.active_attempts()


def test_C12_the_switch_proceeds_once_the_work_is_released(tmp_path):
    """The refusal's own advice works: release the work and the same command copies.

    This is the other half of the rule -- a refusal nobody can clear would be a dead end, and
    the message names the command that clears it."""
    project, attempt = state.live(tmp_path, "file")
    state.run(
        project,
        "release",
        state.HOLDER_TICKET,
        "--agent",
        state.HOLDER_WORKER,
        "--reason",
        "handing on",
    )

    state.run(project, "migrate", "--to", "sqlite")
    store = state.store_for(project, "sqlite")
    assert [claim.state for claim in store.records("claim")] == [
        coordination_records.CLAIM_RELEASED
    ]
    assert state.snapshot(store) == state.snapshot(state.store_for(project, "file"))


def test_C12_a_destination_holding_work_needs_overwrite(tmp_path):
    """A copy may not sit beside another store's records; `--overwrite` is the answer.

    The destination's own history is not silently merged or replaced: the switch is refused
    with the count and the flag, and only `--overwrite` makes the destination's state the
    source's."""
    project = state.quiescent(tmp_path, "file")
    state.run(project, "migrate", "--to", "sqlite")
    path = state.released_work(project, state.HOLDER_TICKET, "sqlite")

    proc = state.run(project, "migrate", "--from", "file", "--to", "sqlite", expect=4)
    assert "coordination_target_work" in proc.stderr and "--overwrite" in proc.stderr
    assert state.store_for(project, "sqlite").find_record(
        "claim", coordination_records.claim_id_for(
            state.store_for(project, "sqlite").get_workspace().id, path
        )
    ) is not None, "a refused switch destroys nothing"

    state.run(project, "migrate", "--from", "file", "--to", "sqlite", "--overwrite")
    assert state.snapshot(state.store_for(project, "sqlite")) == state.snapshot(
        state.store_for(project, "file")
    )
    assert state.run(project, "doctor", expect=0)


def test_C12_a_dry_run_reports_the_coordination_copy_and_creates_nothing(tmp_path):
    """`--dry-run` says what would be carried, names the destination's records, writes nothing."""
    project = state.quiescent(tmp_path, "file")
    state.run(project, "migrate", "--to", "sqlite")
    state.released_work(project, state.HOLDER_TICKET, "sqlite")

    proc = state.run(
        project, "migrate", "--from", "file", "--to", "sqlite", "--overwrite", "--dry-run"
    )
    assert "would copy" in proc.stdout and "replacing" in proc.stdout
    assert "evidence:" in proc.stdout, "the summary says what evidence would travel"
    before = state.snapshot(state.store_for(project, "sqlite"))
    assert state.snapshot(state.store_for(project, "sqlite")) == before

    elsewhere = tmp_path / "fresh"
    elsewhere.mkdir()
    fresh = state.quiescent(elsewhere, "file")
    listing = state.run(fresh, "migrate", "--to", "sqlite", "--dry-run").stdout
    assert "coordination: would copy" in listing
    assert not (fresh / ".arbite" / "arbite.db").exists()


# ---------------------------------------------------------------------------
# Schema revisions: an older store is migrated, never misread
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sink_kind", state.SINKS)
def test_C12_a_revision_1_store_is_refused_by_name_and_migrated_forward(tmp_path, sink_kind):
    """The two halves of the revision contract C07 and C11 handed to this ticket.

    A revision-1 document must never be *read* as if it were current -- its fields may not
    mean the same thing -- so a command that reads the store refuses it by name. The migration
    is the one path that brings it forward, mechanically, and it reports how many records it
    upgraded."""
    project = state.quiescent(tmp_path, sink_kind)
    rewritten = state.old_revision_documents(project, sink_kind)
    assert {record_type for record_type, _ in rewritten} == {"claim", "observation"}

    refusal = state.run(project, "file", "claims", sink_kind=sink_kind, expect=1)
    assert "schema revision 1" in refusal.stderr

    target = "sqlite" if sink_kind == "file" else "file"
    proc = state.run(project, "migrate", "--from", sink_kind, "--to", target, sink_kind=sink_kind)
    assert "upgraded" in proc.stdout and "older arbite" in proc.stdout

    migrated = state.store_for(project, target)
    for record_type, record_id in rewritten:
        document = state.stored_document(project, record_type, record_id, target)
        assert document["schema_revision"] == coordination_records.COORDINATION_SCHEMA_REVISION
        if record_type == "observation":
            assert document["spent_by"] is None, "revision 2's field arrives with its one value"
        assert migrated.find_record(record_type, record_id) is not None
    assert state.run(project, "doctor", sink_kind=target, expect=0)


def test_C12_a_revision_a_build_cannot_reach_is_refused_not_guessed():
    """The upgrade is mechanical where it is defined, and refused where it is not.

    A newer revision (a build this arbite does not know) and a missing revision both fail by
    name: "bring it forward" may not mean "invent the fields". The unit-level check is here
    because a store-level one could only ever present the revision it holds."""
    newer = {"record": "claim", "id": "clm-0001", "schema_revision": 99}
    with pytest.raises(coordination_records.RecordError) as refusal:
        coordination_records.upgrade_document(newer)
    assert "newer than this arbite's" in str(refusal.value)

    without = {"record": "claim", "id": "clm-0001"}
    with pytest.raises(coordination_records.RecordError) as refusal:
        coordination_records.upgrade_document(without)
    assert "no usable 'schema_revision'" in str(refusal.value)

    current = coordination_records.FileClaim(
        id="clm-0001",
        workspace_id="ws-0001",
        path="src/arbite/cli.py",
        ticket_id="tic-0001",
        attempt_id="att-0001",
        generation=1,
        acquired="2026-09-21T13:12:04Z",
    ).to_dict()
    assert coordination_records.upgrade_document(current) == current


def test_C12_the_plan_reads_a_store_it_could_not_otherwise_read(tmp_path):
    """The plan tolerates an older store, which is the store a migration exists for.

    `TransferPlan` is what `--dry-run` reports and what a real run checks before copying, so a
    store written by an older arbite has to be plannable even though every command that *reads*
    it refuses."""
    project = state.quiescent(tmp_path, "file")
    state.old_revision_documents(project, "file")

    planned = coordination_migrate.plan(
        state.store_for(project, "file"), state.store_for(project, "sqlite")
    )
    assert planned.upgradable >= 2
    assert not planned.is_blocked
    assert coordination_migrate.plan_lines(planned)


# ---------------------------------------------------------------------------
# Evidence that cannot be carried
# ---------------------------------------------------------------------------


def test_C12_evidence_over_the_size_limit_refuses_the_switch(tmp_path, monkeypatch):
    """A version this proxy will not store stops the copy before anything moves.

    The alternative would be a destination whose receipts name bytes that were never written,
    so the limit is checked against the source's records before the transfer starts, and the
    refusal names the version and the limit. Checked through the library rather than the CLI
    because the limit is a module constant the CLI *process* reads for itself; what the CLI
    adds on top of this is the rendering, which the missing-evidence test below covers with a
    real command."""
    project = state.quiescent(tmp_path, "file")
    source = state.store_for(project, "file")
    target = state.store_for(project, "sqlite")
    monkeypatch.setattr(coordination_store, "MAX_ARTIFACT_BYTES", 4)

    planned = coordination_migrate.plan(source, target)
    assert {blocker.kind for blocker in planned.blockers} == {
        coordination_migrate.EVIDENCE_UNCARRIED
    }
    assert len(planned.blockers) == planned.artifacts, "every version is named, not just one"
    assert "over the 4-byte limit" in planned.blockers[0].detail

    with pytest.raises(CoordinationError) as refusal:
        coordination_migrate.transfer(source, target)
    assert "coordination_evidence_uncarried" in str(refusal.value)
    assert not (project / ".arbite" / "arbite.db").exists(), "nothing was written"
    assert state.snapshot(source) == state.snapshot(state.store_for(project, "file"))


def test_C12_missing_evidence_refuses_the_switch(tmp_path):
    """Bytes that are already gone stop the copy too, and the finding names the digest.

    A receipt whose evidence is missing is drift the source cannot fix, and copying it would
    reproduce that drift in a store with no history to explain it."""
    project = state.quiescent(tmp_path, "file")
    store = state.store_for(project, "file")
    artifact = store.records("artifact")[0]
    store.artifact_path(artifact.digest).unlink()

    proc = state.run(project, "migrate", "--to", "sqlite", expect=4)
    assert "coordination_evidence_uncarried" in proc.stderr
    assert artifact.digest in proc.stderr


def test_C12_a_pending_operation_travels_with_its_receipt_rather_than_blocking(tmp_path):
    """An unfinished operation is carried, not refused, and the report says so.

    Its bytes are on disk and its receipt is the only record of what was intended, so the
    destination can judge it exactly as the source would. Refusing instead would leave a
    drifted store unmovable -- a dead end rather than a safety property."""
    project = state.quiescent(tmp_path, "file")
    store = state.store_for(project, "file")
    receipt = store.receipts()[0]
    store.put_record(
        coordination_records.replace(receipt, result=coordination_records.RECEIPT_PENDING)
    )

    proc = state.run(project, "migrate", "--to", "sqlite")
    assert "pending operation(s) travelled with their receipts" in proc.stdout

    migrated = state.store_for(project, "sqlite")
    assert migrated.get_record("receipt", receipt.id).is_pending
    shown = state.run(project, "workspace", "show", "--json", sink_kind="sqlite")
    assert json.loads(shown.stdout)["coordination"]["pending_operations"] == 1


# ---------------------------------------------------------------------------
# The export a devlog is generated from
# ---------------------------------------------------------------------------


def test_C12_the_receipt_summary_is_stable_and_carries_the_facts(quiescent):
    """The export is reproducible, complete, and says what is retained before any pruning.

    Complete means a reader with only the summary has the operation, its ticket, its actor,
    its paths and both versions; reproducible means two runs on unchanged state print the same
    text, which is what makes it a record rather than a report about a moment."""
    project = quiescent
    first = state.run(project, "receipt", "--summary").stdout
    assert first == state.run(project, "receipt", "--summary").stdout

    store = state.store_for(project, state.sink_of(project))
    receipts = [receipt for receipt in store.receipts() if receipt.kind != "passthrough"]
    assert receipts
    for receipt in receipts:
        assert receipt.id in first, "every operation is named"
        for path in receipt.paths:
            assert path in first
        if receipt.actor:
            assert receipt.actor in first
    assert "evidence:" in first and "never prunes evidence" in first
    assert f"{len(store.records('artifact'))} artifact record(s)" in first

    payload = json.loads(state.run(project, "receipt", "--summary", "--json").stdout)
    assert {entry["operation"] for entry in payload["receipts"]} == {
        receipt.id for receipt in store.receipts()
    }
    for entry in payload["receipts"]:
        for path in entry["paths"]:
            for side in ("before", "after"):
                digest = path[side]
                if digest != coordination_records.ABSENT:
                    assert len(digest) == len("sha256:") + 64, "JSON carries the whole digest"


def test_C12_the_summary_narrows_to_one_ticket_and_says_when_there_is_nothing(quiescent):
    """A ticket filter is a smaller answer, and a ticket with no operations is exit 2.

    "Nothing recorded" has to be distinguishable from "not found": a summary answering both
    with an empty list would invite a reader to believe work happened."""
    project = quiescent
    narrowed = state.run(project, "receipt", "--summary", "--ticket", state.HOLDER_TICKET)
    assert "receipt summary for " in narrowed.stdout
    assert f"for {state.HOLDER_TICKET}" in narrowed.stdout

    other = state.run(project, "receipt", "--summary", "--ticket", state.RIVAL_TICKET, expect=2)
    assert "no operations recorded for " in other.stdout
    assert "arbite events --tail 20" in other.stdout

    missing = state.run(project, "receipt", "--summary", "--ticket", "tic-ffff", expect=1)
    assert "no ticket tic-ffff" in missing.stderr


def test_C12_the_summary_forms_refuse_the_other_half_of_the_command(quiescent):
    """`--summary` and an operation id are alternatives, and neither alone is guesswork.

    EV6 is one receipt by id; the summary is every operation. Asking for both at once, or for
    neither, is refused with the form that does what the caller meant."""
    project = quiescent
    both = state.run(project, "receipt", "--summary", "op-0001", expect=1)
    assert "--summary reports every operation" in both.stderr

    neither = state.run(project, "receipt", expect=1)
    assert "name the operation id" in neither.stderr
