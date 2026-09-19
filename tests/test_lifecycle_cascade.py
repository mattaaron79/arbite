"""The lifecycle cleanup cascade (planning key C09).

Every transition that ends a work attempt must also clean up after it: reconcile
any incomplete file operation, release the attempt's exclusive file claims, and
retain the released claims, receipts and partial work as history. This module
checks that cascade directly against both sinks, plus the multiprocess
close-vs-write race and the delete guard.

Nothing here starts a daemon or a timer: reconciliation is invoked by the
lifecycle transition itself, exactly as a mutation invokes it.
"""

from __future__ import annotations

import multiprocessing
import types
from pathlib import Path

import pytest

from arbite import (
    application,
    fileclaims,
    filereads,
    filemutations,
    lifecycle,
    mutation,
    schema,
)
from arbite.application import Actor
from arbite.errors import (
    AttemptInactive,
    ClaimConflict,
    CoordinationError,
    DriftDetected,
    StaleRead,
    TicketError,
)
from arbite.sinks import SinkSpec, build_sink
from arbite.sinks.base import Expect
from helpers import make_ticket


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FaultOnce:
    """A fault injector that raises once, at one named boundary."""

    def __init__(self, phase: str):
        self.phase = phase
        self.fired = False

    def __call__(self, phase: str) -> None:
        if phase == self.phase and not self.fired:
            self.fired = True
            raise mutation.FaultInjected(phase)


def _project(sink, arbite_dir, *, ticket="tic-a1b2", worker="claude.opus.001"):
    """A bound workspace with one active attempt and the file services."""
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "a.py").write_text("alpha\n")
    sink.create(make_ticket(ticket))
    service = application.coordination_service_for(
        sink, root=str(root), actor=Actor(worker)
    )
    ctl = lifecycle.TicketLifecycle(service, sink)
    attempt = ctl.acquire(sink.get(ticket), worker_id=worker).attempt
    claims = fileclaims.FileClaimService(service)
    reads = filereads.FileReadService(service, claims=claims)
    mutations = filemutations.FileMutationService(service, claims=claims, reads=reads)
    return types.SimpleNamespace(
        sink=sink,
        service=service,
        ctl=ctl,
        attempt=attempt,
        claims=claims,
        reads=reads,
        mutations=mutations,
        root=root,
    )


def _mark_closed(ctx, ticket_id: str, *, status: str = "closed") -> None:
    """Move the ticket to a status/closed state the way `cmd_close` does."""
    ticket = ctx.sink.get(ticket_id, unique=True)
    expect = Expect(status=ticket.status, assignee=ticket.assignee)
    ticket.closed = schema.now()
    ticket.updated = ticket.closed
    ticket.status = status
    ctx.sink.update(ticket, expect=expect)


# ---------------------------------------------------------------------------
# the cascade itself
# ---------------------------------------------------------------------------


def test_end_attempt_releases_every_claim(sink, arbite_dir):
    ctx = _project(sink, arbite_dir)
    (ctx.root / "src" / "b.py").write_text("beta\n")
    held = ctx.claims.claim(ctx.attempt, ["src/a.py", "src/b.py"]).acquired
    assert len(held) == 2

    ctx.ctl.end_attempt(ctx.attempt, state="released", reason="yielding")

    assert ctx.claims.active_claims() == []
    with ctx.service.store.transaction(write=False) as tx:
        stored = [tx.get("file_claim", claim.id) for claim in held]
    assert [claim.state for claim in stored] == ["released", "released"]
    assert all(claim.released for claim in stored)
    released = [
        event
        for event in sink.coordination().event_log()
        if event.event_kind == "claim_released"
    ]
    assert {event.payload["path"] for event in released} == {"src/a.py", "src/b.py"}


def test_yielding_preserves_partial_work_and_evidence(sink, arbite_dir):
    ctx = _project(sink, arbite_dir)
    ctx.claims.claim(ctx.attempt, ["src/a.py"])
    token = ctx.reads.read(ctx.attempt, "src/a.py").read_token
    ctx.mutations.write(ctx.attempt, "src/a.py", b"partial\n", read_token=token)

    ctx.ctl.end_attempt(
        ctx.attempt, state="released", reason="out of context", handoff="half done"
    )

    # A release never reverts or deletes partial work, and keeps the evidence.
    assert (ctx.root / "src" / "a.py").read_bytes() == b"partial\n"
    attempts = ctx.ctl.attempts_for("tic-a1b2")
    assert [attempt.state for attempt in attempts] == ["released"]
    assert attempts[0].handoff == "half done"
    with ctx.service.store.transaction(write=False) as tx:
        receipts = [
            receipt
            for receipt in tx.find("operation_receipt", attempt_id=ctx.attempt.id)
            if receipt.result == "ok" and receipt.operation_kind == "write"
        ]
    assert receipts, "the successful change must keep its receipt"


def test_takeover_releases_old_claims_and_starts_a_new_generation(sink, arbite_dir):
    ctx = _project(sink, arbite_dir)
    ctx.claims.claim(ctx.attempt, ["src/a.py"])
    held = ctx.claims.claim_for("src/a.py")
    old = ctx.attempt

    result = ctx.ctl.acquire(
        sink.get("tic-a1b2"), worker_id="w2", takeover=True, reason="stalled"
    )

    assert result.took_over is True
    assert result.attempt.generation == old.generation + 1
    assert ctx.claims.active_claims() == []
    with ctx.service.store.transaction(write=False) as tx:
        stored = tx.get("file_claim", held.id)
    assert stored.state == "released" and stored.released is not None
    events = sink.coordination().event_log()
    released = [
        event
        for event in events
        if event.event_kind == "claim_released"
        and event.payload.get("path") == "src/a.py"
    ]
    assert released and released[-1].payload.get("reason") == "stalled"
    interrupted = [
        event
        for event in events
        if event.event_kind == "attempt_interrupted"
        and event.payload.get("reason") == "stalled"
    ]
    assert interrupted, "the takeover must record why the old attempt was revoked"

    # The stale worker cannot claim or mutate under its old attempt: creating a
    # new path needs no read token, so the (inactive) attempt guard is the refusal.
    with pytest.raises((AttemptInactive, ClaimConflict)):
        ctx.mutations.write(old, "src/new.py", b"stale\n")


def test_reopen_never_resurrects_old_claims(sink, arbite_dir):
    ctx = _project(sink, arbite_dir)
    ctx.claims.claim(ctx.attempt, ["src/a.py"])
    held = ctx.claims.claim_for("src/a.py")
    token = ctx.reads.read(ctx.attempt, "src/a.py").read_token

    # A close/reopen ends the attempt (releasing its claim) and returns the ticket
    # to the open pool without any history being rewritten.
    ctx.ctl.end_attempt(ctx.attempt, state="released", reason="reopened")
    ticket = sink.get("tic-a1b2", unique=True)
    expect = Expect(status=ticket.status, assignee=ticket.assignee)
    ticket.status = "open"
    ticket.assignee = None
    ticket.updated = schema.now()
    sink.update(ticket, expect=expect)

    assert [attempt.state for attempt in ctx.ctl.attempts_for("tic-a1b2")] == ["released"]
    assert ctx.claims.active_claims() == []
    with ctx.service.store.transaction(write=False) as tx:
        assert tx.get("file_claim", held.id).state == "released"

    # A new attempt must re-read; the old observation authorizes nothing, and the
    # path is reacquired at a strictly higher generation.
    fresh = ctx.ctl.acquire(sink.get("tic-a1b2"), worker_id="w2").attempt
    assert fresh.generation == 2
    with pytest.raises(CoordinationError):
        ctx.mutations.write(ctx.attempt, "src/a.py", b"stale\n", read_token=token)
    reacquired = fileclaims.FileClaimService(ctx.service).claim(fresh, ["src/a.py"])
    assert reacquired.acquired[0].generation == held.generation + 1


def test_delete_guard_refuses_active_and_historical_tickets(sink, arbite_dir):
    ctx = _project(sink, arbite_dir)
    sink.create(make_ticket("tic-clean"))
    # A ticket with no coordination history is deletable.
    assert ctx.ctl.require_deletable(sink.get("tic-clean")) is None

    with pytest.raises(TicketError) as active:
        ctx.ctl.require_deletable(sink.get("tic-a1b2"))
    assert "active attempt" in str(active.value)

    ctx.ctl.end_attempt(ctx.attempt, state="released")
    with pytest.raises(TicketError) as history:
        ctx.ctl.require_deletable(sink.get("tic-a1b2"))
    assert "change history" in str(history.value)


def test_pending_ambiguous_write_prevents_a_clean_close(sink, arbite_dir):
    ctx = _project(sink, arbite_dir)
    claim = ctx.claims.claim(ctx.attempt, ["src/a.py"]).acquired[0]
    engine = mutation.MutationEngine(
        ctx.service, ctx.claims, fault_injector=_FaultOnce(mutation.FAULT_BEFORE_APPLY)
    )
    with pytest.raises(mutation.FaultInjected):
        engine.write(ctx.attempt, "src/a.py", b"pending\n", claim=claim)
    # An unrestricted external writer leaves bytes matching neither the recorded
    # before nor after version: reconciliation cannot guess, so the close refuses.
    (ctx.root / "src" / "a.py").write_bytes(b"external\n")

    with pytest.raises(DriftDetected) as caught:
        ctx.ctl.end_attempt(ctx.attempt, state="finished", reason="closed")

    assert caught.value.bytes_may_have_changed is True
    # No false clean close: the attempt is still active for an explicit resolution.
    assert ctx.ctl.active_attempt("tic-a1b2").id == ctx.attempt.id


def test_crash_after_attempt_end_before_ticket_close_is_recoverable(sink, arbite_dir):
    ctx = _project(sink, arbite_dir)
    ctx.claims.claim(ctx.attempt, ["src/a.py"])

    # The attempt ends (and its claims are released) but the process "crashes"
    # before the ticket is marked closed: the documented legacy state.
    ctx.ctl.end_attempt(ctx.attempt, state="finished", reason="closed")
    assert ctx.ctl.active_attempt("tic-a1b2") is None
    assert ctx.claims.active_claims() == []
    assert sink.get("tic-a1b2").status == "in_progress"

    # Re-running close takes the no-attempt path and completes cleanly.
    ctx.ctl.reconcile_operations()
    ctx.ctl.release_ticket_claims("tic-a1b2", reason="closed")
    _mark_closed(ctx, "tic-a1b2")
    assert sink.get("tic-a1b2").status == "closed"


# ---------------------------------------------------------------------------
# the close-vs-write race across processes (both sinks)
# ---------------------------------------------------------------------------


def _close_race_writer(kind, arbite_dir, root, attempt_id, token, path, content, start, results):
    """Try one proxy write under an attempt another process is closing.

    Module-level so it is picklable under the `spawn` start method.
    """
    try:
        sink = build_sink(SinkSpec(kind=kind), Path(arbite_dir))
        service = application.coordination_service_for(
            sink, root=str(root), actor=Actor("racer")
        )
        with service.store.transaction(write=False) as tx:
            attempt = tx.get("work_attempt", attempt_id)
        mutations = filemutations.FileMutationService(service)
        start.wait(30)
        try:
            result = mutations.write(attempt, path, content, read_token=token)
            results.put("applied" if result.applied else "replay")
        except (StaleRead, AttemptInactive, ClaimConflict):
            results.put("refused")
    except Exception as error:  # pragma: no cover - surfaced through the queue
        results.put(f"error:{type(error).__name__}:{error}")


def test_close_races_a_write_and_leaves_no_stale_mutation(sink, kind, arbite_dir):
    ctx = _project(sink, arbite_dir)
    ctx.claims.claim(ctx.attempt, ["src/a.py"])
    token = ctx.reads.read(ctx.attempt, "src/a.py").read_token

    context = multiprocessing.get_context("spawn")
    start = context.Barrier(2)
    results = context.Queue()
    process = context.Process(
        target=_close_race_writer,
        args=(
            kind,
            str(arbite_dir),
            str(ctx.root),
            ctx.attempt.id,
            token,
            "src/a.py",
            b"raced\n",
            start,
            results,
        ),
    )
    process.start()
    start.wait(30)
    # Close while the writer races: end the attempt (releasing its claim in the
    # same transaction) and mark the ticket closed, exactly as `cmd_close` does.
    ctx.ctl.end_attempt(ctx.attempt, state="finished", reason="closed")
    _mark_closed(ctx, "tic-a1b2")
    outcome = results.get(timeout=60)
    process.join(60)
    assert process.exitcode == 0
    assert outcome in ("applied", "refused"), outcome

    # No observer may mutate under the old token after close succeeds, and the
    # bytes are stable regardless of which side of the race the writer landed on.
    before = (ctx.root / "src" / "a.py").read_bytes()
    with pytest.raises(CoordinationError):
        ctx.mutations.write(ctx.attempt, "src/a.py", b"after\n", read_token=token)
    assert (ctx.root / "src" / "a.py").read_bytes() == before
    # The cascade released the claim whichever side won.
    assert ctx.claims.active_claims() == []
    if outcome == "applied":
        # The concurrent change that did land before close keeps its receipt.
        with ctx.service.store.transaction(write=False) as tx:
            receipts = [
                receipt
                for receipt in tx.find("operation_receipt", attempt_id=ctx.attempt.id)
                if receipt.result == "ok" and receipt.operation_kind == "write"
            ]
        assert receipts, "a write that won the race must not lose its evidence"
