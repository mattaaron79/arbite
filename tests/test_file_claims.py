"""Exclusive file claims, canonical paths and explicit release (planning key C04).

Every behavioural test is parametrized over both sinks through the `sink` fixture,
so "the file sink and the SQLite sink behave the same" is checked rather than
assumed. The binding tests build both sinks over one project directory on purpose:
rejecting a workspace that would coordinate against two stores is the point.
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from arbite import application, coordination, fileclaims, lifecycle, paths
from arbite.application import Actor
from arbite.errors import (
    ClaimConflict,
    CoordinationConflict,
    CoordinationNotFound,
    FileBusy,
    StaleRead,
    StoreBindingConflict,
    UnsupportedCoordination,
)
from arbite.sinks import SinkSpec, build_sink
from helpers import make_ticket


# --- helpers ---------------------------------------------------------------


def _project(sink, arbite_dir, *, ticket_id="tic-a1b2", worker="claude.opus.001"):
    """A bound workspace with one active attempt and two real files."""
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "a.py").write_text("alpha\n")
    (root / "src" / "b.py").write_text("beta\n")
    sink.create(make_ticket(ticket_id))
    service = application.coordination_service_for(
        sink, root=str(root), actor=Actor(worker)
    )
    ctl = lifecycle.TicketLifecycle(service, sink)
    acquired = ctl.acquire(sink.get(ticket_id), worker_id=worker)
    return fileclaims.FileClaimService(service), acquired.attempt, root, service


def _second_attempt(sink, root, *, ticket_id="tic-b2c3", worker="claude.opus.002"):
    """A second ticket/attempt in the same workspace, for contention tests."""
    sink.create(make_ticket(ticket_id))
    service = application.coordination_service_for(
        sink, root=str(root), actor=Actor(worker)
    )
    ctl = lifecycle.TicketLifecycle(service, sink)
    acquired = ctl.acquire(sink.get(ticket_id), worker_id=worker)
    return fileclaims.FileClaimService(service), acquired.attempt, service


# --- claim sets ------------------------------------------------------------


def test_claim_set_is_all_or_nothing_and_reports_the_holder(sink, arbite_dir):
    first_claims, first_attempt, root, _ = _project(sink, arbite_dir)
    first = first_claims.claim(first_attempt, ["src/a.py"])
    assert [c.path for c in first.acquired] == ["src/a.py"]
    assert first.acquired[0].generation == 1
    assert first.acquired[0].observed_version == coordination.digest_of_text("alpha\n")

    other_claims, other_attempt, _ = _second_attempt(sink, root)
    with pytest.raises(FileBusy) as excinfo:
        other_claims.claim(other_attempt, ["src/b.py", "src/a.py"])
    details = excinfo.value.details
    assert details["path"] == "src/a.py"
    assert details["holder_ticket"] == "tic-a1b2"
    assert details["holder_attempt"] == first_attempt.id
    assert details["holder_generation"] == 1
    assert details["requested_attempt"] == other_attempt.id
    assert details["available_actions"]

    # All-or-nothing: the free path in the refused set was not claimed, and the
    # holder's claim is untouched.
    assert other_claims.active_claims(attempt_id=other_attempt.id) == []
    assert [c.path for c in first_claims.active_claims(attempt_id=first_attempt.id)] == [
        "src/a.py"
    ]


def test_unrelated_paths_are_owned_independently(sink, arbite_dir):
    first_claims, first_attempt, root, _ = _project(sink, arbite_dir)
    first_claims.claim(first_attempt, ["src/a.py"])
    other_claims, other_attempt, _ = _second_attempt(sink, root)

    second = other_claims.claim(other_attempt, ["src/b.py"])
    assert [c.path for c in second.acquired] == ["src/b.py"]
    assert {c.path for c in other_claims.active_claims(attempt_id=other_attempt.id)} == {
        "src/b.py"
    }
    assert {c.path for c in first_claims.active_claims(attempt_id=first_attempt.id)} == {
        "src/a.py"
    }


def test_reentrant_claim_is_explicitly_idempotent(sink, arbite_dir):
    claims, attempt, _root, _ = _project(sink, arbite_dir)
    first = claims.claim(attempt, ["src/a.py"])
    again = claims.claim(attempt, ["src/a.py"])

    assert again.acquired == []
    assert [c.id for c in again.reentrant] == [first.acquired[0].id]
    assert again.reentrant[0].generation == first.acquired[0].generation
    # No second active claim was minted for one path.
    assert len(claims.active_claims(attempt_id=attempt.id)) == 1


def test_creation_destination_is_representable_and_cannot_evade_ownership(sink, arbite_dir):
    claims, attempt, root, _ = _project(sink, arbite_dir)
    created = claims.claim(attempt, ["src/new.py"]).acquired[0]
    assert created.observed_version == coordination.ABSENT

    other_claims, other_attempt, _ = _second_attempt(sink, root)
    with pytest.raises(FileBusy):
        other_claims.claim(other_attempt, ["src/new.py"])

    with pytest.raises(UnsupportedCoordination):
        claims.claim(attempt, ["missing_dir/new.py"])


# --- release and token revocation -----------------------------------------


def test_release_then_reacquire_mints_a_new_generation_and_revokes_old_read_token(
    sink, arbite_dir
):
    claims, attempt, _root, service = _project(sink, arbite_dir)
    first = claims.claim(attempt, ["src/a.py"]).acquired[0]
    observation = service.record_read(attempt, "src/a.py", b"alpha\n", claim=first)
    assert observation.write_authorizing is True
    assert observation.claim_generation == first.generation

    released = claims.release(attempt, ["src/a.py"], reason="handing the file off")
    assert [c.state for c in released.released] == ["released"]
    assert released.released[0].id == first.id
    # Idempotent re-release: the token is already dead, nothing new is minted.
    again = claims.release(attempt, ["src/a.py"], reason="already released")
    assert [c.id for c in again.already_released] == [first.id]

    second = claims.claim(attempt, ["src/a.py"]).acquired[0]
    assert second.id != first.id
    assert second.generation == first.generation + 1
    # The old read token can no longer authorize a write under the new claim.
    with pytest.raises(StaleRead):
        application.require_write_authorization(observation, claim=second, attempt=attempt)


def test_release_refuses_paths_not_held_by_this_attempt(sink, arbite_dir):
    claims, attempt, root, _ = _project(sink, arbite_dir)
    claims.claim(attempt, ["src/a.py"])

    other_claims, other_attempt, _ = _second_attempt(sink, root)
    with pytest.raises(ClaimConflict):
        other_claims.release(other_attempt, ["src/a.py"], reason="not mine")
    with pytest.raises(CoordinationNotFound):
        claims.release(attempt, ["src/b.py"], reason="never claimed")
    with pytest.raises(ClaimConflict):
        claims.release(attempt, ["src/a.py"], reason="   ")


def test_release_validates_the_expected_generation(sink, arbite_dir):
    claims, attempt, _root, _ = _project(sink, arbite_dir)
    first = claims.claim(attempt, ["src/a.py"]).acquired[0]

    with pytest.raises(StaleRead):
        claims.release(
            attempt,
            ["src/a.py"],
            reason="stale caller",
            expected_generation=first.generation + 1,
        )
    # A refused stale release revoked nothing.
    assert claims.claim_for("src/a.py").generation == first.generation

    released = claims.release(
        attempt,
        ["src/a.py"],
        reason="correct generation",
        expected_generation=first.generation,
    )
    assert [c.id for c in released.released] == [first.id]


# --- aliases, escapes, special files --------------------------------------


def test_aliases_escapes_and_special_files_are_rejected(sink, arbite_dir):
    claims, attempt, root, _ = _project(sink, arbite_dir)
    for bad in (
        "../outside.py",
        "/etc/passwd",
        "~/.bashrc",
        ".arbite/arbite.db",
        ".git/config",
        "src/../../escape.py",
    ):
        with pytest.raises(UnsupportedCoordination):
            claims.claim(attempt, [bad])

    # A directory is not a whole-file mutation target.
    with pytest.raises(UnsupportedCoordination):
        claims.claim(attempt, ["src"])

    # A symlinked directory component is refused rather than followed.
    (root / "linkdir").symlink_to(root / "src", target_is_directory=True)
    with pytest.raises(UnsupportedCoordination):
        claims.claim(attempt, ["linkdir/a.py"])

    # A symlinked file target is refused.
    (root / "src" / "alias.py").symlink_to(root / "src" / "a.py")
    with pytest.raises(UnsupportedCoordination):
        claims.claim(attempt, ["src/alias.py"])

    # A hard-linked mutation target is refused rather than aliased.
    os.link(root / "src" / "a.py", root / "src" / "hard.py")
    with pytest.raises(UnsupportedCoordination):
        claims.claim(attempt, ["src/hard.py"])

    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "src" / "pipe")
        with pytest.raises(UnsupportedCoordination):
            claims.claim(attempt, ["src/pipe"])


# --- case semantics --------------------------------------------------------


def test_case_folding_is_used_only_on_a_case_insensitive_volume(tmp_path):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("alpha\n")

    folded = paths.resolve_target(root, "SRC/A.PY", case_insensitive=True)
    assert folded.relative == "src/a.py"
    assert folded.exists is True
    assert folded.case_folded is True

    creation = paths.resolve_target(root, "SRC/New.PY", case_insensitive=True)
    assert creation.relative == "src/new.py"
    assert creation.is_creation is True

    exact = paths.resolve_target(root, "src/a.py", case_insensitive=False)
    assert exact.relative == "src/a.py"
    # On a case-sensitive volume `SRC` is a different, missing parent.
    with pytest.raises(UnsupportedCoordination):
        paths.resolve_target(root, "SRC/a.py", case_insensitive=False)


def test_case_folded_claims_collide_for_two_attempts(sink, arbite_dir):
    first_claims, first_attempt, root, _ = _project(sink, arbite_dir)
    fileclaims.FileClaimService(
        first_claims.service, case_insensitive=True
    ).claim(first_attempt, ["src/A.py"])

    other_claims, other_attempt, _ = _second_attempt(sink, root)
    with pytest.raises(FileBusy):
        fileclaims.FileClaimService(other_claims.service, case_insensitive=True).claim(
            other_attempt, ["src/a.py"]
        )
    assert first_claims.claim_for("src/a.py") is not None


# --- binding ---------------------------------------------------------------


def test_conflicting_store_selection_requires_an_explicit_quiescent_rebind(tmp_path):
    arbite_dir = tmp_path / ".arbite"
    arbite_dir.mkdir()
    root = tmp_path
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("alpha\n")

    file_sink = build_sink(SinkSpec(kind="file"), arbite_dir)
    file_sink.init()
    file_sink.create(make_ticket("tic-a1b2"))
    service = application.coordination_service_for(
        file_sink, root=str(root), actor=Actor("w1")
    )
    ctl = lifecycle.TicketLifecycle(service, file_sink)
    acquired = ctl.acquire(file_sink.get("tic-a1b2"), worker_id="w1")

    sqlite_sink = build_sink(SinkSpec(kind="sqlite"), arbite_dir)
    sqlite_sink.init()

    with pytest.raises(StoreBindingConflict) as excinfo:
        application.coordination_service_for(
            sqlite_sink, root=str(root), actor=Actor("w1")
        )
    assert excinfo.value.details["rebind_required"] is True
    assert excinfo.value.details["bound"]["sink_kind"] == "file"
    assert excinfo.value.details["requested"]["sink_kind"] == "sqlite"

    # An explicit rebind while the file-sink work is live is refused as not quiescent.
    with pytest.raises(CoordinationConflict) as excinfo:
        application.coordination_service_for(
            sqlite_sink, root=str(root), actor=Actor("w1"), rebind=True
        )
    assert excinfo.value.details["active_attempt_ids"] == [acquired.attempt.id]

    # Quiesce, then rebind explicitly.
    ctl.end_attempt(acquired.attempt, state="released")
    rebound = application.coordination_service_for(
        sqlite_sink, root=str(root), actor=Actor("w1"), rebind=True
    )
    assert rebound.workspace.id == service.workspace.id
    assert rebound.workspace.store_binding.sink_kind == "sqlite"

    # The previously selected store now refuses in the other direction.
    with pytest.raises(StoreBindingConflict):
        application.coordination_service_for(
            file_sink, root=str(root), actor=Actor("w1")
        )


def test_duplicate_store_selection_for_one_kind_is_refused(tmp_path):
    arbite_dir = tmp_path / ".arbite"
    arbite_dir.mkdir()
    root = tmp_path

    primary = build_sink(SinkSpec(kind="file"), arbite_dir)
    primary.init()
    application.coordination_service_for(primary, root=str(root), actor=Actor("w1"))

    other_location = tmp_path / "other-store"
    secondary = build_sink(SinkSpec(kind="file", root=str(other_location)), arbite_dir)
    with pytest.raises(StoreBindingConflict):
        application.coordination_service_for(secondary, root=str(root), actor=Actor("w1"))


# --- a real two-process race ----------------------------------------------


def _race_worker(kind, root, arbite_dir, ticket_id, worker, path, start, queue):
    """Claim `path` for one worker's attempt, after a shared start gate.

    Module-level so it is picklable under the `spawn` start method.
    """
    try:
        sink = build_sink(SinkSpec(kind=kind), Path(arbite_dir))
        service = application.coordination_service_for(
            sink, root=root, actor=Actor(worker)
        )
        claims = fileclaims.FileClaimService(service)
        with service.store.transaction(write=False) as tx:
            attempt = tx.find("work_attempt", ticket_id=ticket_id)[0]
        start.wait(30)
        try:
            claims.claim(attempt, [path])
            queue.put("ok")
        except FileBusy:
            queue.put("busy")
    except Exception as error:  # pragma: no cover - surfaced through the queue
        queue.put(f"error:{type(error).__name__}:{error}")


def test_two_processes_racing_for_one_path_yield_exactly_one_winner(sink, kind, arbite_dir):
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "a.py").write_text("alpha\n")
    for ticket_id in ("tic-a1b2", "tic-b2c3"):
        sink.create(make_ticket(ticket_id))
    service = application.coordination_service_for(
        sink, root=str(root), actor=Actor("parent")
    )
    ctl = lifecycle.TicketLifecycle(service, sink)
    ctl.acquire(sink.get("tic-a1b2"), worker_id="w1")
    ctl.acquire(sink.get("tic-b2c3"), worker_id="w2")

    context = multiprocessing.get_context("spawn")
    start = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(
            target=_race_worker,
            args=(kind, str(root), str(arbite_dir), ticket_id, worker, "src/a.py", start, queue),
        )
        for ticket_id, worker in (("tic-a1b2", "w1"), ("tic-b2c3", "w2"))
    ]
    for process in processes:
        process.start()
    outcomes = sorted(queue.get(timeout=60) for _ in processes)
    for process in processes:
        process.join(60)
        assert process.exitcode == 0
    assert outcomes == ["busy", "ok"]
