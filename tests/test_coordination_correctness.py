"""Regression tests for the shared-directory coordination correctness pass.

Each test reproduces one finding from the post-epic review and runs against both
shipped sinks (the `sink` fixture), so the fix is a checked claim for the file
journal and for SQLite alike:

1. a mutation (or a new file claim) cannot complete under an attempt whose ticket
   has closed -- the engine reloads the authoritative attempt and claim inside its
   lock instead of trusting objects the caller loaded earlier;
2. acquiring a ticket cannot erase a concurrent edit (e.g. a new dependency): the
   ticket write is a revision compare-and-swap;
3. a lifecycle transition is never left half-applied: the ticket write and the
   coordination cascade are joined by a durable lifecycle intent that a failure
   compensates and a crash rolls forward (or abandons) on the next operation;
4. a read token is consumed by the mutation it authorizes, independent of the
   bytes written (an identical-bytes write, or content returning to an earlier
   version, cannot revive it);
5. rename recovery compares an existing destination against its recorded
   before-version rather than assuming it was absent;
6. an existing binary file can be replaced, removed or renamed through a
   versioned (content-free) read receipt -- and only through one.
"""

from __future__ import annotations

import copy
import json
import types

import pytest

from arbite import (
    application,
    cli,
    fileclaims,
    filemutations,
    filereads,
    lifecycle,
    mutation,
)
from arbite.application import Actor
from arbite.coordination import digest_of_bytes
from arbite.errors import (
    ArbiteError,
    AttemptInactive,
    ClaimConflict,
    CoordinationError,
    RecoveryRequired,
    SinkError,
    StaleRead,
)
from arbite.sinks.base import Expect
from helpers import make_ticket

WORKER = "claude.opus.001"
TICKET = "tic-a1b2"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _project(sink, arbite_dir, *, files=(("src/a.py", b"alpha\n"),), fault_injector=None):
    """A bound workspace with one claimed ticket, its attempt and file services."""
    root = arbite_dir.parent
    for rel, data in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    sink.create(make_ticket(TICKET))
    service = application.coordination_service_for(sink, root=str(root), actor=Actor(WORKER))
    ctl = lifecycle.TicketLifecycle(service, sink)
    attempt = ctl.acquire(sink.get(TICKET), worker_id=WORKER).attempt
    claims = fileclaims.FileClaimService(service)
    reads = filereads.FileReadService(service, claims=claims)
    mutations = filemutations.FileMutationService(
        service, claims=claims, reads=reads, fault_injector=fault_injector
    )
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


def _token(ctx, path):
    ctx.claims.claim(ctx.attempt, [path])
    receipt = ctx.reads.read(ctx.attempt, path)
    assert receipt.write_authorizing is True
    return receipt.read_token


def _arbite(monkeypatch, kind, root, *argv):
    """Run one CLI command in-process against the fixture's store.

    In-process (rather than a subprocess) so a test can inject a failure into the
    sink or the lifecycle and observe exactly what the real command left behind.
    """
    monkeypatch.chdir(root)
    monkeypatch.setenv("ARBITE_SINK", kind)
    parser, _ = cli.build_parser()
    args = parser.parse_args(list(argv))
    args.func(args)


def _attempt(service, attempt_id):
    with service.store.transaction(write=False) as tx:
        return tx.get("work_attempt", attempt_id)


def _lifecycle_intents(service, state=None):
    with service.store.transaction(write=False) as tx:
        found = list(tx.find("lifecycle_intent"))
    return [i for i in found if state is None or i.state == state]


def _events(service, kind):
    return [e for e in service.store.event_log() if e.event_kind == kind]


class _FaultAt:
    """Simulate a crash at one lifecycle boundary (once)."""

    def __init__(self, phase):
        self.phase = phase
        self.fired = False

    def __call__(self, phase):
        if phase == self.phase and not self.fired:
            self.fired = True
            raise lifecycle.LifecycleFault(phase)


# ---------------------------------------------------------------------------
# 1. mutations and claims revalidate authoritative state inside the lock
# ---------------------------------------------------------------------------


def test_rename_resumed_after_its_ticket_closes_is_refused(sink, kind, arbite_dir, monkeypatch):
    ctx = _project(sink, arbite_dir)
    token = _token(ctx, "src/a.py")

    class ClosingClaims(fileclaims.FileClaimService):
        """Claims the rename's paths, then the ticket closes before the engine runs."""

        def claim(self, attempt, requested_paths, **kwargs):
            result = super().claim(attempt, requested_paths, **kwargs)
            _arbite(monkeypatch, kind, ctx.root, "close", TICKET, "--agent", WORKER)
            return result

    closing = ClosingClaims(ctx.service)
    mutations = filemutations.FileMutationService(
        ctx.service, claims=closing, reads=filereads.FileReadService(ctx.service, claims=closing)
    )
    with pytest.raises((AttemptInactive, ClaimConflict)):
        mutations.rename(ctx.attempt, "src/a.py", "src/b.py", read_token=token)

    assert (ctx.root / "src" / "a.py").read_bytes() == b"alpha\n"
    assert not (ctx.root / "src" / "b.py").exists()
    assert sink.get(TICKET).status == "closed"
    assert ctx.claims.active_claims() == []


def test_a_stale_attempt_cannot_claim_files_after_its_ticket_closes(
    sink, kind, arbite_dir, monkeypatch
):
    ctx = _project(sink, arbite_dir)
    _arbite(monkeypatch, kind, ctx.root, "close", TICKET, "--agent", WORKER)

    assert ctx.attempt.is_active  # the caller's copy is stale
    with pytest.raises(AttemptInactive):
        ctx.claims.claim(ctx.attempt, ["src/a.py"])
    assert ctx.claims.active_claims() == []


def test_the_engine_refuses_a_released_claim_object_the_caller_kept(sink, arbite_dir):
    ctx = _project(sink, arbite_dir)
    claim = ctx.claims.claim(ctx.attempt, ["src/a.py"]).acquired[0]
    ctx.claims.release(ctx.attempt, ["src/a.py"], reason="done with it")

    engine = mutation.MutationEngine(ctx.service, ctx.claims)
    with pytest.raises(ClaimConflict):
        engine.write(ctx.attempt, "src/a.py", b"sneaky\n", claim=claim)
    assert (ctx.root / "src" / "a.py").read_bytes() == b"alpha\n"


def test_the_engine_refuses_a_superseded_claim_generation(sink, arbite_dir):
    ctx = _project(sink, arbite_dir)
    old = ctx.claims.claim(ctx.attempt, ["src/a.py"]).acquired[0]
    ctx.claims.release(ctx.attempt, ["src/a.py"], reason="cycle")
    ctx.claims.claim(ctx.attempt, ["src/a.py"])  # a new generation, same attempt

    engine = mutation.MutationEngine(ctx.service, ctx.claims)
    with pytest.raises(StaleRead):
        engine.write(ctx.attempt, "src/a.py", b"old token\n", claim=old)
    assert (ctx.root / "src" / "a.py").read_bytes() == b"alpha\n"


# ---------------------------------------------------------------------------
# 2. acquisition is a revision compare-and-swap
# ---------------------------------------------------------------------------


def test_claim_does_not_erase_a_concurrent_dependency(sink, arbite_dir, monkeypatch):
    root = arbite_dir.parent
    sink.create(make_ticket("tic-a1b2"))
    sink.create(make_ticket("tic-dep1"))
    service = application.coordination_service_for(sink, root=str(root), actor=Actor(WORKER))
    ctl = lifecycle.TicketLifecycle(service, sink)

    real_attempts_for = ctl.attempts_for
    raced = []

    def attempts_for_with_a_racing_edit(ticket_id):
        # Runs after acquisition read the ticket and before it writes: another
        # agent adds an unmet dependency in between.
        if not raced:
            raced.append(True)
            other = copy.deepcopy(sink.get("tic-a1b2"))
            other.depends_on = ["tic-dep1"]
            sink.update(other, expect=Expect(revision=other.revision))
        return real_attempts_for(ticket_id)

    monkeypatch.setattr(ctl, "attempts_for", attempts_for_with_a_racing_edit)
    with pytest.raises(ArbiteError):
        ctl.acquire(sink.get("tic-a1b2"), worker_id=WORKER)

    ticket = sink.get("tic-a1b2")
    assert ticket.depends_on == ["tic-dep1"]
    assert ticket.status == "open" and ticket.assignee is None
    assert real_attempts_for("tic-a1b2") == []


def test_a_ticket_edit_cannot_overwrite_a_concurrent_edit(sink, kind, arbite_dir, monkeypatch):
    """The CLI's compare-and-swap covers the whole ticket, not just status/assignee."""
    root = arbite_dir.parent
    sink.create(make_ticket("tic-a1b2"))
    sink.create(make_ticket("tic-dep1"))
    stale = copy.deepcopy(sink.get("tic-a1b2"))
    _arbite(monkeypatch, kind, root, "depend", "tic-a1b2", "tic-dep1")

    stale.title = "renamed from a stale read"
    with pytest.raises(ArbiteError):
        sink.update(stale, expect=cli._expect_from(copy.deepcopy(stale)))
    assert sink.get("tic-a1b2").depends_on == ["tic-dep1"]


# ---------------------------------------------------------------------------
# 3. lifecycle transitions are journaled, compensated and recoverable
# ---------------------------------------------------------------------------


def _claimed_project(sink, arbite_dir):
    ctx = _project(sink, arbite_dir)
    ctx.claims.claim(ctx.attempt, ["src/a.py"])
    return ctx


def _assert_still_working(ctx):
    ticket = ctx.sink.get(TICKET)
    assert ticket.status == "in_progress" and ticket.assignee == WORKER
    assert _attempt(ctx.service, ctx.attempt.id).is_active
    assert [c.path for c in ctx.claims.active_claims()] == ["src/a.py"]
    assert _lifecycle_intents(ctx.service, "pending") == []


def _assert_closed(ctx):
    ticket = ctx.sink.get(TICKET)
    assert ticket.status == "closed" and ticket.closed
    assert _attempt(ctx.service, ctx.attempt.id).state == "finished"
    assert ctx.claims.active_claims() == []
    assert _lifecycle_intents(ctx.service, "pending") == []
    assert len(_events(ctx.service, "ticket_closed")) == 1


def test_close_failing_at_the_ticket_write_changes_nothing(sink, kind, arbite_dir, monkeypatch):
    ctx = _claimed_project(sink, arbite_dir)
    real_update = type(sink).update

    def failing_update(self, ticket, expect=None):
        if ticket.status == "closed":
            raise SinkError("injected ticket-store failure")
        return real_update(self, ticket, expect)

    monkeypatch.setattr(type(sink), "update", failing_update)
    with pytest.raises(SinkError):
        _arbite(monkeypatch, kind, ctx.root, "close", TICKET, "--agent", WORKER)
    monkeypatch.setattr(type(sink), "update", real_update)

    # Whatever the ticket store did, the next operation settles the journal; the
    # ticket write never landed, so the transition is abandoned, not applied.
    ctx.ctl.reconcile_lifecycle()
    _assert_still_working(ctx)


def test_close_failing_after_the_ticket_write_is_compensated(sink, kind, arbite_dir, monkeypatch):
    ctx = _claimed_project(sink, arbite_dir)

    def failing_apply(self, intent):
        raise SinkError("injected coordination-store failure")

    monkeypatch.setattr(lifecycle.TicketLifecycle, "_apply_intent", failing_apply)
    with pytest.raises(SinkError):
        _arbite(monkeypatch, kind, ctx.root, "close", TICKET, "--agent", WORKER)

    # The ticket write was reverted: an in_progress ticket with its attempt and
    # claims intact, never an in_progress ticket with a finished attempt.
    _assert_still_working(ctx)


def test_close_crashing_after_the_ticket_write_is_rolled_forward(
    sink, kind, arbite_dir, monkeypatch
):
    ctx = _claimed_project(sink, arbite_dir)
    token = ctx.reads.read(ctx.attempt, "src/a.py").read_token
    monkeypatch.setattr(
        lifecycle.TicketLifecycle, "_fault", _FaultAt(lifecycle.FAULT_AFTER_TICKET_WRITE)
    )
    with pytest.raises(lifecycle.LifecycleFault):
        _arbite(monkeypatch, kind, ctx.root, "close", TICKET, "--agent", WORKER)
    monkeypatch.undo()

    # The crash window: the ticket says closed, the cascade has not run. A
    # mutation under the old attempt must not slip through it.
    assert sink.get(TICKET).status == "closed"
    assert len(_lifecycle_intents(ctx.service, "pending")) == 1
    with pytest.raises(RecoveryRequired):
        ctx.mutations.write(ctx.attempt, "src/a.py", b"late\n", read_token=token)
    with pytest.raises(RecoveryRequired):
        ctx.claims.claim(ctx.attempt, ["src/new.py"])
    assert (ctx.root / "src" / "a.py").read_bytes() == b"alpha\n"

    # The next lifecycle operation completes the recorded transition.
    ctx.ctl.reconcile_lifecycle()
    _assert_closed(ctx)


def test_close_crashing_before_the_ticket_write_is_abandoned(sink, kind, arbite_dir, monkeypatch):
    ctx = _claimed_project(sink, arbite_dir)
    monkeypatch.setattr(
        lifecycle.TicketLifecycle, "_fault", _FaultAt(lifecycle.FAULT_AFTER_INTENT)
    )
    with pytest.raises(lifecycle.LifecycleFault):
        _arbite(monkeypatch, kind, ctx.root, "close", TICKET, "--agent", WORKER)
    monkeypatch.undo()

    ctx.ctl.reconcile_lifecycle()
    _assert_still_working(ctx)
    abandoned = _lifecycle_intents(ctx.service, "abandoned")
    assert [i.transition for i in abandoned] == ["close"]

    # And the ticket can then be closed normally.
    _arbite(monkeypatch, kind, ctx.root, "close", TICKET, "--agent", WORKER)
    _assert_closed(ctx)


def test_a_pending_close_is_completed_by_the_next_lifecycle_command(
    sink, kind, arbite_dir, monkeypatch
):
    ctx = _claimed_project(sink, arbite_dir)
    monkeypatch.setattr(
        lifecycle.TicketLifecycle, "_fault", _FaultAt(lifecycle.FAULT_AFTER_TICKET_WRITE)
    )
    with pytest.raises(lifecycle.LifecycleFault):
        _arbite(monkeypatch, kind, ctx.root, "close", TICKET, "--agent", WORKER)
    monkeypatch.undo()

    # Reopening reconciles first, so it sees a closed ticket with no live attempt.
    _arbite(monkeypatch, kind, ctx.root, "reopen", TICKET)
    assert sink.get(TICKET).status == "open"
    assert _attempt(ctx.service, ctx.attempt.id).state == "finished"
    assert ctx.claims.active_claims() == []
    assert _lifecycle_intents(ctx.service, "pending") == []


def test_acquisition_crashing_after_the_ticket_write_is_rolled_forward(sink, arbite_dir):
    root = arbite_dir.parent
    sink.create(make_ticket(TICKET))
    service = application.coordination_service_for(sink, root=str(root), actor=Actor(WORKER))
    crashing = lifecycle.TicketLifecycle(service, sink)
    crashing._fault = _FaultAt(lifecycle.FAULT_AFTER_TICKET_WRITE)
    with pytest.raises(lifecycle.LifecycleFault):
        crashing.acquire(sink.get(TICKET), worker_id=WORKER)

    ticket = sink.get(TICKET)
    assert ticket.status == "in_progress" and ticket.assignee == WORKER
    ctl = lifecycle.TicketLifecycle(service, sink)
    assert ctl.reconcile_lifecycle()
    active = ctl.active_attempt(TICKET)
    assert active is not None and active.worker_id == WORKER
    assert len(_events(service, "attempt_started")) == 1


def test_acquisition_failing_after_the_ticket_write_reverts_the_ticket(
    sink, arbite_dir, monkeypatch
):
    root = arbite_dir.parent
    sink.create(make_ticket(TICKET))
    service = application.coordination_service_for(sink, root=str(root), actor=Actor(WORKER))
    ctl = lifecycle.TicketLifecycle(service, sink)

    def failing_apply(self, intent):
        raise SinkError("injected coordination-store failure")

    monkeypatch.setattr(lifecycle.TicketLifecycle, "_apply_intent", failing_apply)
    with pytest.raises(SinkError):
        ctl.acquire(sink.get(TICKET), worker_id=WORKER)
    monkeypatch.undo()

    ticket = sink.get(TICKET)
    assert ticket.status == "open" and ticket.assignee is None
    assert ctl.attempts_for(TICKET) == []
    assert _lifecycle_intents(service, "pending") == []


def test_doctor_reports_and_fixes_a_pending_lifecycle_intent(sink, kind, arbite_dir, monkeypatch):
    ctx = _claimed_project(sink, arbite_dir)
    monkeypatch.setattr(
        lifecycle.TicketLifecycle, "_fault", _FaultAt(lifecycle.FAULT_AFTER_TICKET_WRITE)
    )
    with pytest.raises(lifecycle.LifecycleFault):
        _arbite(monkeypatch, kind, ctx.root, "close", TICKET, "--agent", WORKER)
    monkeypatch.undo()

    kinds = [p.kind for p in sink.check()]
    assert "pending_lifecycle_intent" in kinds
    fixed = [p for p in sink.check(fix=True) if p.kind == "pending_lifecycle_intent"]
    assert fixed and all(p.fixed for p in fixed)
    _assert_closed(ctx)


# ---------------------------------------------------------------------------
# 4. read tokens are consumed by the mutation they authorize
# ---------------------------------------------------------------------------


def test_an_identical_bytes_write_consumes_its_token(sink, arbite_dir):
    ctx = _project(sink, arbite_dir)
    token = _token(ctx, "src/a.py")
    ctx.mutations.write(ctx.attempt, "src/a.py", b"alpha\n", read_token=token)

    with pytest.raises(StaleRead):
        ctx.mutations.write(ctx.attempt, "src/a.py", b"second use\n", read_token=token)
    assert (ctx.root / "src" / "a.py").read_bytes() == b"alpha\n"


def test_a_token_stays_dead_when_content_returns_to_its_version(sink, arbite_dir):
    ctx = _project(sink, arbite_dir)
    old_token = _token(ctx, "src/a.py")  # observed "alpha"
    second = ctx.reads.read(ctx.attempt, "src/a.py").read_token
    ctx.mutations.write(ctx.attempt, "src/a.py", b"beta\n", read_token=second)
    third = ctx.reads.read(ctx.attempt, "src/a.py").read_token
    ctx.mutations.write(ctx.attempt, "src/a.py", b"alpha\n", read_token=third)

    # The bytes are "alpha" again, but the old token predates two mutations.
    with pytest.raises(StaleRead):
        ctx.mutations.write(ctx.attempt, "src/a.py", b"resurrected\n", read_token=old_token)
    assert (ctx.root / "src" / "a.py").read_bytes() == b"alpha\n"


def test_an_edit_consumes_its_token_even_when_it_changes_nothing(sink, arbite_dir):
    ctx = _project(sink, arbite_dir)
    token = _token(ctx, "src/a.py")
    ctx.mutations.edit(
        ctx.attempt, "src/a.py", [{"old": "alpha", "new": "alpha"}], read_token=token
    )
    with pytest.raises(StaleRead):
        ctx.mutations.edit(
            ctx.attempt, "src/a.py", [{"old": "alpha", "new": "omega"}], read_token=token
        )
    assert (ctx.root / "src" / "a.py").read_bytes() == b"alpha\n"


# ---------------------------------------------------------------------------
# 5. rename recovery honours an existing destination's before-version
# ---------------------------------------------------------------------------


def _rename_over_existing(sink, arbite_dir, phase):
    ctx = _project(
        sink,
        arbite_dir,
        files=(("src/a.py", b"source\n"), ("src/b.py", b"destination\n")),
        fault_injector=_MutationFault(phase),
    )
    token = _token(ctx, "src/a.py")
    with pytest.raises(mutation.FaultInjected):
        ctx.mutations.rename(
            ctx.attempt,
            "src/a.py",
            "src/b.py",
            read_token=token,
            dest_expected=digest_of_bytes(b"destination\n"),
        )
    return ctx


class _MutationFault:
    def __init__(self, phase):
        self.phase = phase
        self.fired = False

    def __call__(self, phase):
        if phase == self.phase and not self.fired:
            self.fired = True
            raise mutation.FaultInjected(phase)


def test_rename_over_an_existing_destination_interrupted_before_any_change_reverts(
    sink, arbite_dir
):
    ctx = _rename_over_existing(sink, arbite_dir, mutation.FAULT_AFTER_INTENT)

    reports = ctx.mutations.engine.reconcile()  # must not raise DriftDetected
    assert [r.state for r in reports] == ["reverted"]
    assert (ctx.root / "src" / "a.py").read_bytes() == b"source\n"
    assert (ctx.root / "src" / "b.py").read_bytes() == b"destination\n"

    # Recovery-dependent work is not blocked: the rename can simply be retried.
    token = ctx.reads.read(ctx.attempt, "src/a.py").read_token
    ctx.mutations.rename(
        ctx.attempt,
        "src/a.py",
        "src/b.py",
        read_token=token,
        dest_expected=digest_of_bytes(b"destination\n"),
    )
    assert not (ctx.root / "src" / "a.py").exists()
    assert (ctx.root / "src" / "b.py").read_bytes() == b"source\n"


def test_rename_over_an_existing_destination_interrupted_between_paths_completes(
    sink, arbite_dir
):
    ctx = _rename_over_existing(sink, arbite_dir, mutation.FAULT_AFTER_DEST_COMMITTED)

    reports = ctx.mutations.engine.reconcile()
    assert [r.state for r in reports] == ["applied"]
    assert not (ctx.root / "src" / "a.py").exists()
    assert (ctx.root / "src" / "b.py").read_bytes() == b"source\n"


# ---------------------------------------------------------------------------
# 6. existing binary files can be overwritten through a versioned receipt
# ---------------------------------------------------------------------------

BINARY = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\xff"


def test_an_existing_binary_file_can_be_overwritten_with_a_version_receipt(sink, arbite_dir):
    ctx = _project(sink, arbite_dir, files=(("assets/logo.png", BINARY),))
    ctx.claims.claim(ctx.attempt, ["assets/logo.png"])

    receipt = ctx.reads.read(ctx.attempt, "assets/logo.png", version_only=True)
    assert receipt.write_authorizing is True
    assert receipt.version_only is True and receipt.text == ""
    assert receipt.whole_file_digest == digest_of_bytes(BINARY)
    assert receipt.encoding == "binary"

    result = ctx.mutations.write(
        ctx.attempt, "assets/logo.png", BINARY + b"\x01", read_token=receipt.read_token
    )
    assert result.applied
    assert (ctx.root / "assets" / "logo.png").read_bytes() == BINARY + b"\x01"
    # The version receipt is consumed like any other token.
    with pytest.raises(StaleRead):
        ctx.mutations.write(
            ctx.attempt, "assets/logo.png", b"again", read_token=receipt.read_token
        )


def test_a_version_receipt_is_not_write_authorizing_without_the_claim(sink, arbite_dir):
    ctx = _project(sink, arbite_dir, files=(("assets/logo.png", BINARY),))
    receipt = ctx.reads.read(ctx.attempt, "assets/logo.png", version_only=True)
    assert receipt.write_authorizing is False
    ctx.claims.claim(ctx.attempt, ["assets/logo.png"])
    with pytest.raises(StaleRead):
        ctx.mutations.write(
            ctx.attempt, "assets/logo.png", b"nope", read_token=receipt.read_token
        )
    assert (ctx.root / "assets" / "logo.png").read_bytes() == BINARY


def test_removing_a_binary_file_requires_a_version_receipt(sink, arbite_dir):
    ctx = _project(sink, arbite_dir, files=(("assets/logo.png", BINARY),))
    ctx.claims.claim(ctx.attempt, ["assets/logo.png"])
    with pytest.raises(StaleRead):
        ctx.mutations.remove(ctx.attempt, "assets/logo.png")
    assert (ctx.root / "assets" / "logo.png").read_bytes() == BINARY

    token = ctx.reads.read(ctx.attempt, "assets/logo.png", version_only=True).read_token
    assert ctx.mutations.remove(ctx.attempt, "assets/logo.png", read_token=token).applied
    assert not (ctx.root / "assets" / "logo.png").exists()


def test_renaming_a_binary_file_requires_a_version_receipt(sink, arbite_dir):
    ctx = _project(sink, arbite_dir, files=(("assets/logo.png", BINARY),))
    ctx.claims.claim(ctx.attempt, ["assets/logo.png"])
    with pytest.raises(StaleRead):
        ctx.mutations.rename(ctx.attempt, "assets/logo.png", "assets/icon.png")
    assert (ctx.root / "assets" / "logo.png").read_bytes() == BINARY
    assert not (ctx.root / "assets" / "icon.png").exists()

    token = ctx.reads.read(ctx.attempt, "assets/logo.png", version_only=True).read_token
    ctx.mutations.rename(ctx.attempt, "assets/logo.png", "assets/icon.png", read_token=token)
    assert (ctx.root / "assets" / "icon.png").read_bytes() == BINARY
    # The token was consumed by the rename.
    with pytest.raises(CoordinationError):
        ctx.mutations.rename(ctx.attempt, "assets/icon.png", "assets/x.png", read_token=token)


def test_the_cli_serves_a_version_receipt_for_a_binary_file(
    sink, kind, arbite_dir, monkeypatch, capsys
):
    ctx = _project(sink, arbite_dir, files=(("assets/logo.png", BINARY),))
    ctx.claims.claim(ctx.attempt, ["assets/logo.png"])
    capsys.readouterr()
    _arbite(
        monkeypatch,
        kind,
        ctx.root,
        "file",
        "read",
        "assets/logo.png",
        "--ticket",
        TICKET,
        "--attempt",
        ctx.attempt.id,
        "--version-only",
        "--json",
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["version_only"] is True
    assert data["write_authorizing"] is True
    assert data["whole_file_digest"] == digest_of_bytes(BINARY)
