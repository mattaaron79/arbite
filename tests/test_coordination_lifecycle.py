"""Ticket acquisition and lifecycle policy (C03): readiness, adoption, takeover.

Every test runs unchanged against both shipped sinks, via the `sink` fixture, so
"acquisition behaves the same whichever store is configured" is a checked claim
rather than an aspiration. The store is reached through
`application.coordination_service_for` -- the same stopgap construction the CLI
uses -- so the tests exercise the real wiring, not a bespoke fixture.
"""

from __future__ import annotations

import copy

import pytest

from arbite import application, coordination as c, lifecycle, schema
from arbite.application import Actor
from arbite.errors import (
    ArbiteError,
    AttemptInactive,
    CoordinationConflict,
    CoordinationError,
    TicketError,
    UnsupportedCoordination,
)
from arbite.query import TicketQuery
from helpers import make_ticket


def make_lifecycle(sink, arbite_dir):
    """A `TicketLifecycle` for `sink`, built the way the CLI builds one."""
    service = application.coordination_service_for(
        sink, root=str(arbite_dir.parent), actor=Actor("tester")
    )
    return lifecycle.TicketLifecycle(service, sink)


def add(sink, ticket_id, **overrides):
    overrides.setdefault("status", "open")
    ticket = make_ticket(ticket_id, **overrides)
    sink.create(ticket)
    return ticket


def active_attempts(ctl, ticket_id):
    return [a for a in ctl.attempts_for(ticket_id) if a.is_active]


def events_of(sink, kind):
    return [e for e in sink.coordination().event_log() if e.event_kind == kind]


# --- readiness refusals ----------------------------------------------------


def test_require_claimable_refuses_an_unmet_dependency():
    blocker = make_ticket("tic-b1", status="open")
    dependent = make_ticket("tic-d1", status="open", depends_on=["tic-b1"])
    with pytest.raises(TicketError) as excinfo:
        lifecycle.require_claimable(dependent, {"tic-b1": blocker, "tic-d1": dependent}, None)
    assert "unmet dependencies" in str(excinfo.value)
    assert "tic-b1" in str(excinfo.value)


@pytest.mark.parametrize("field_name", ["type", "tier", "domain"])
def test_require_claimable_refuses_placeholder_classification(field_name):
    ticket = make_ticket("tic-p1", status="open", **{field_name: "TODO: classify me"})
    with pytest.raises(TicketError) as excinfo:
        lifecycle.require_claimable(ticket, {"tic-p1": ticket}, None)
    assert field_name in str(excinfo.value)


def test_require_claimable_refuses_status_not_open():
    ticket = make_ticket("tic-s1", status="raw")
    with pytest.raises(TicketError) as excinfo:
        lifecycle.require_claimable(ticket, {"tic-s1": ticket}, None)
    assert "not 'open'" in str(excinfo.value)


def test_require_claimable_refuses_a_ticket_with_an_active_attempt(sink, arbite_dir):
    add(sink, "tic-a1")
    ctl = make_lifecycle(sink, arbite_dir)
    # An *open* ticket that nevertheless carries an active attempt: the guard must
    # refuse it on the attempt check, and acquisition must refuse it too.
    attempt = c.WorkAttempt(
        id=c.new_record_id("work_attempt"),
        ticket_id="tic-a1",
        worker_id="w1",
        workspace_id=ctl.coordination.workspace.id,
        generation=1,
        started=ctl.now(),
        last_activity=ctl.now(),
    )
    with sink.coordination().transaction() as tx:
        tx.put(attempt)

    ticket = sink.get("tic-a1")
    with pytest.raises(CoordinationConflict) as excinfo:
        lifecycle.require_claimable(ticket, {ticket.id: ticket}, ctl.active_attempt(ticket.id))
    assert excinfo.value.retryable is True
    assert "active attempt" in str(excinfo.value)

    with pytest.raises(CoordinationConflict):
        ctl.acquire(ticket, worker_id="w2")
    assert [a.id for a in ctl.attempts_for("tic-a1")] == [attempt.id]


def test_a_second_acquire_while_an_attempt_is_active_is_refused(sink, arbite_dir):
    add(sink, "tic-a1")
    ctl = make_lifecycle(sink, arbite_dir)
    first = ctl.acquire(sink.get("tic-a1"), worker_id="w1")

    with pytest.raises(ArbiteError):
        ctl.acquire(sink.get("tic-a1"), worker_id="w2")

    attempts = ctl.attempts_for("tic-a1")
    assert [a.id for a in attempts] == [first.attempt.id]
    assert active_attempts(ctl, "tic-a1") == attempts


# --- concurrency: exactly one winner ---------------------------------------


def test_a_lost_race_records_no_attempt_and_leaves_one_active(sink, arbite_dir, monkeypatch):
    """The ticket CAS is the authority: a caller holding a stale read loses it.

    The loser's `Conflict` is raised before any coordination write, so exactly one
    active attempt exists afterwards -- the old behaviour's real bug was that two
    racers could both come away believing they owned the ticket. To reach the CAS
    with a genuinely stale view, the loser's ticket read *and* its attempt lookup
    are both pinned to the pre-race snapshot.
    """
    add(sink, "tic-race")
    ctl = make_lifecycle(sink, arbite_dir)
    stale = copy.deepcopy(sink.get("tic-race"))  # as a second process read it first
    winner = ctl.acquire(sink.get("tic-race"), worker_id="w1")
    real_attempts_for = ctl.attempts_for

    monkeypatch.setattr(sink, "get", lambda ticket_id, unique=False: stale)
    monkeypatch.setattr(ctl, "attempts_for", lambda ticket_id: [])
    with pytest.raises(CoordinationConflict) as excinfo:
        ctl.acquire(stale, worker_id="w2")
    assert excinfo.value.retryable is True

    attempts = real_attempts_for("tic-race")
    assert [a.id for a in attempts] == [winner.attempt.id]
    assert [a.worker_id for a in attempts if a.is_active] == ["w1"]


# --- ending attempts -------------------------------------------------------


def test_end_attempt_releases_it_and_the_token_stays_dead(sink, arbite_dir):
    add(sink, "tic-a1")
    ctl = make_lifecycle(sink, arbite_dir)
    result = ctl.acquire(sink.get("tic-a1"), worker_id="w1")

    ctl.end_attempt(result.attempt, state="released", reason="stopping")
    assert ctl.active_attempt("tic-a1") is None

    # A terminal attempt can never be reused or re-ended.
    with pytest.raises(AttemptInactive):
        application.require_active_attempt(result.attempt)
    with pytest.raises(ArbiteError):
        ctl.end_attempt(result.attempt, state="released")

    released = events_of(sink, "attempt_released")
    assert len(released) == 1
    assert released[0].payload["generation"] == 1


def test_end_attempt_finished_and_interrupted_are_recorded(sink, arbite_dir):
    add(sink, "tic-a1")
    add(sink, "tic-a2")
    ctl = make_lifecycle(sink, arbite_dir)
    one = ctl.acquire(sink.get("tic-a1"), worker_id="w1")
    two = ctl.acquire(sink.get("tic-a2"), worker_id="w1")

    ctl.end_attempt(one.attempt, state="finished", reason="done")
    ctl.end_attempt(two.attempt, state="interrupted", reason="revoked")

    assert one.attempt.state == "finished"
    assert two.attempt.state == "interrupted"
    assert len(events_of(sink, "attempt_finished")) == 1
    assert len(events_of(sink, "attempt_interrupted")) == 1


# --- takeover --------------------------------------------------------------


def test_takeover_requires_a_reason(sink, arbite_dir):
    add(sink, "tic-a1")
    ctl = make_lifecycle(sink, arbite_dir)
    ctl.acquire(sink.get("tic-a1"), worker_id="w1")

    with pytest.raises(TicketError) as excinfo:
        ctl.acquire(sink.get("tic-a1"), worker_id="w2", takeover=True, reason="")
    assert "--reason" in str(excinfo.value)
    assert [a.worker_id for a in active_attempts(ctl, "tic-a1")] == ["w1"]


def test_takeover_interrupts_the_old_attempt_and_increments_generation(sink, arbite_dir):
    add(sink, "tic-a1")
    ctl = make_lifecycle(sink, arbite_dir)
    first = ctl.acquire(sink.get("tic-a1"), worker_id="w1")

    second = ctl.acquire(
        sink.get("tic-a1"), worker_id="w2", takeover=True, reason="w1 stalled"
    )

    assert second.took_over is True
    assert second.attempt.generation == first.attempt.generation + 1
    attempts = ctl.attempts_for("tic-a1")
    assert [a.state for a in attempts] == ["interrupted", "active"]
    assert [a.worker_id for a in active_attempts(ctl, "tic-a1")] == ["w2"]
    # The old token is dead, not mutated back into service.
    assert attempts[0].outcome == "w1 stalled"
    with pytest.raises(AttemptInactive):
        application.require_active_attempt(attempts[0])

    interrupted = events_of(sink, "attempt_interrupted")
    assert len(interrupted) == 1
    assert interrupted[0].payload["superseded_by"] == second.attempt.id


# --- adoption --------------------------------------------------------------


def test_adoption_creates_an_attempt_for_a_legacy_in_progress_ticket(sink, arbite_dir):
    add(sink, "tic-legacy", status="in_progress", assignee="claude.old.001")
    ctl = make_lifecycle(sink, arbite_dir)
    assert ctl.attempts_for("tic-legacy") == []

    result = ctl.acquire(
        sink.get("tic-legacy"), worker_id="claude.new.001", adopt=True
    )

    attempt = result.attempt
    assert attempt.generation == 1
    # No history is invented: the attempt starts now.
    assert attempt.started == attempt.last_activity
    assert c.is_utc_timestamp(attempt.started)
    # The pre-existing declared owner is recorded, not discarded.
    assert "claude.old.001" in (attempt.handoff or "")
    assert result.ticket.status == "in_progress"
    assert result.ticket.assignee == "claude.new.001"

    started = events_of(sink, "attempt_started")
    assert len(started) == 1
    assert started[0].payload["origin"] == lifecycle.ORIGIN_ADOPTED
    assert started[0].payload["generation"] == 1


def test_adoption_is_refused_when_an_attempt_already_exists(sink, arbite_dir):
    add(sink, "tic-a1")
    ctl = make_lifecycle(sink, arbite_dir)
    ctl.acquire(sink.get("tic-a1"), worker_id="w1")

    with pytest.raises(TicketError) as excinfo:
        ctl.acquire(sink.get("tic-a1"), worker_id="w1", adopt=True)
    assert "--adopt" in str(excinfo.value)


def test_adoption_is_never_implicit(sink, arbite_dir):
    """A legacy in_progress ticket is not silently claimed by a normal claim."""
    add(sink, "tic-legacy", status="in_progress", assignee="claude.old.001")
    ctl = make_lifecycle(sink, arbite_dir)

    with pytest.raises(TicketError) as excinfo:
        ctl.acquire(sink.get("tic-legacy"), worker_id="claude.new.001")
    # The refusal names the recovery paths instead of a bare status complaint.
    assert "no attempt record" in str(excinfo.value)
    assert "--adopt" in str(excinfo.value)
    assert ctl.attempts_for("tic-legacy") == []


def test_adoption_requires_in_progress_status(sink, arbite_dir):
    add(sink, "tic-open", status="open")
    ctl = make_lifecycle(sink, arbite_dir)
    with pytest.raises(TicketError) as excinfo:
        ctl.acquire(sink.get("tic-open"), worker_id="w1", adopt=True)
    assert "in_progress" in str(excinfo.value)


# --- events ----------------------------------------------------------------


def test_acquisition_emits_attempt_started_with_origin_and_generation(sink, arbite_dir):
    add(sink, "tic-a1")
    ctl = make_lifecycle(sink, arbite_dir)
    ctl.acquire(sink.get("tic-a1"), worker_id="w1")

    started = events_of(sink, "attempt_started")
    claimed = events_of(sink, "ticket_claimed")
    assert started and claimed
    for event in started + claimed:
        assert event.category == "lifecycle"
        assert event.cursor is not None
        assert event.payload["origin"] == lifecycle.ORIGIN_CLAIMED
        assert event.payload["generation"] == 1
    # Cursors are per-store monotonic and unique.
    cursors = [e.cursor for e in sink.coordination().event_log()]
    assert cursors == sorted(cursors)
    assert len(set(cursors)) == len(cursors)


# --- dependency-edit / reopen race -----------------------------------------


def test_reopen_invalidates_dependents_without_touching_them(sink, arbite_dir):
    """A reopen never rewrites the dependents: it records what they lost.

    This is the documented serial outcome: work that already committed is not
    silently undone, while a later claim sees the reopened dependency and is
    refused by readiness.
    """
    add(sink, "tic-root", status="closed", closed="2026-01-02T00:00:00")
    add(sink, "tic-dep", status="open", depends_on=["tic-root"], assignee=None)
    ctl = make_lifecycle(sink, arbite_dir)

    reopened = sink.get("tic-root")
    every = sink.query(TicketQuery(buckets=("*",)))
    affected = ctl.invalidate_dependents(reopened, every)

    assert affected == ["tic-dep"]
    events = events_of(sink, "dependency_invalidated")
    assert len(events) == 1
    assert events[0].subject_ids[0] == "tic-dep"
    assert events[0].payload["reopened_ticket_id"] == "tic-root"
    assert events[0].payload["affected_ticket_ids"] == ["tic-dep"]

    # The dependent is untouched: same status, same assignee.
    dependent = sink.get("tic-dep")
    assert dependent.status == "open"
    assert dependent.assignee is None


def test_a_claim_after_a_dependency_reopens_is_refused(sink, arbite_dir):
    add(sink, "tic-root", status="closed", closed="2026-01-02T00:00:00")
    add(sink, "tic-dep", status="open", depends_on=["tic-root"])
    ctl = make_lifecycle(sink, arbite_dir)
    ctl.acquire(sink.get("tic-dep"), worker_id="w1")  # fine while the dep is closed

    # A second ticket keeps the same dependency; once the dependency reopens it is
    # refused by readiness rather than being claimable.
    add(sink, "tic-dep2", status="open", depends_on=["tic-root"])
    root = sink.get("tic-root")
    root.status = "open"
    root.closed = None
    sink.update(root)

    with pytest.raises(TicketError) as excinfo:
        ctl.acquire(sink.get("tic-dep2"), worker_id="w2")
    assert "unmet dependencies" in str(excinfo.value)
    # The claim that had already committed keeps its attempt.
    assert active_attempts(ctl, "tic-dep")


# --- set status/assignee guidance ------------------------------------------


def test_set_status_and_assignee_are_refused_while_an_attempt_is_active(sink, arbite_dir):
    add(sink, "tic-a1")
    ctl = make_lifecycle(sink, arbite_dir)
    result = ctl.acquire(sink.get("tic-a1"), worker_id="w1")
    ticket = sink.get("tic-a1")

    with pytest.raises(TicketError) as excinfo:
        ctl.refuse_status_edit(
            ticket, new_status="closed", new_assignee=None, attempt=result.attempt
        )
    assert "arbite close" in str(excinfo.value)

    with pytest.raises(TicketError) as excinfo:
        ctl.refuse_status_edit(
            ticket, new_status=None, new_assignee="w2", attempt=result.attempt
        )
    assert "assignee" in str(excinfo.value)

    # A no-op status set (same status) and unrelated field edits stay allowed.
    ctl.refuse_status_edit(
        ticket, new_status="in_progress", new_assignee="w1", attempt=result.attempt
    )


# --- the file-claim cleanup cascade (C09) ----------------------------------


def test_ending_an_attempt_releases_its_active_file_claims(sink, arbite_dir):
    """C09 supersedes C03's refusal: ending an attempt releases its claims.

    The old behaviour (refuse while an active claim exists) is replaced by real
    cleanup, because leaving the claim active would leak exclusive ownership of a
    path nobody will ever write. The claim record is retained as history.
    """
    add(sink, "tic-a1")
    ctl = make_lifecycle(sink, arbite_dir)
    result = ctl.acquire(sink.get("tic-a1"), worker_id="w1")

    claim = c.FileClaim(
        id=c.new_record_id("file_claim"),
        workspace_id=ctl.coordination.workspace.id,
        path="src/a.py",
        ticket_id="tic-a1",
        attempt_id=result.attempt.id,
        generation=1,
        acquired=ctl.now(),
        observed_version=c.digest_of_text("alpha"),
    )
    with sink.coordination().transaction() as tx:
        tx.put(claim)

    ctl.end_attempt(result.attempt, state="released")

    assert ctl.active_attempt("tic-a1") is None
    with sink.coordination().transaction(write=False) as tx:
        released = tx.get("file_claim", claim.id)
    assert released is not None and released.state == "released"
    assert released.released is not None
    events = [
        event
        for event in sink.coordination().event_log()
        if event.event_kind == "claim_released"
        and event.payload.get("path") == "src/a.py"
    ]
    assert events, "the cascade must record a claim_released event"


def test_ending_an_attempt_without_file_claims_needs_no_cleanup(sink, arbite_dir):
    add(sink, "tic-a1")
    ctl = make_lifecycle(sink, arbite_dir)
    result = ctl.acquire(sink.get("tic-a1"), worker_id="w1")
    ctl.end_attempt(result.attempt, state="released")
    assert ctl.active_attempt("tic-a1") is None


# --- no backdoor through --force -------------------------------------------


def test_force_does_not_bypass_an_unmet_dependency(sink, arbite_dir):
    """`--force` overrides ownership, never readiness (plan: no force backdoor)."""
    add(sink, "tic-b1")
    add(sink, "tic-d1", depends_on=["tic-b1"])
    ctl = make_lifecycle(sink, arbite_dir)
    with pytest.raises(TicketError) as excinfo:
        ctl.acquire(sink.get("tic-d1"), worker_id="w1", takeover=True, reason="expedite")
    assert "unmet dependencies" in str(excinfo.value)
    assert not active_attempts(ctl, "tic-d1")
    ticket = sink.get("tic-d1")
    assert ticket.status == "open" and ticket.assignee is None


def test_force_does_not_bypass_placeholder_classification(sink, arbite_dir):
    add(sink, "tic-p1", type="TODO: classify me")
    ctl = make_lifecycle(sink, arbite_dir)
    with pytest.raises(TicketError):
        ctl.acquire(sink.get("tic-p1"), worker_id="w1", takeover=True, reason="expedite")
    assert not active_attempts(ctl, "tic-p1")


def test_force_does_not_bypass_a_non_open_status(sink, arbite_dir):
    add(sink, "tic-x1", status="blocked", blocked_by="waiting on upstream")
    ctl = make_lifecycle(sink, arbite_dir)
    with pytest.raises(TicketError) as excinfo:
        ctl.acquire(sink.get("tic-x1"), worker_id="w1", takeover=True, reason="expedite")
    assert "not 'open'" in str(excinfo.value)
    assert not active_attempts(ctl, "tic-x1")
    assert sink.get("tic-x1").status == "blocked"


def test_force_recovers_a_legacy_in_progress_ticket_only_with_a_reason(sink, arbite_dir):
    """A pre-C03 `in_progress` ticket with no attempt is recovered explicitly:
    a bare forced acquisition is refused, `--force --reason` starts a takeover."""
    add(sink, "tic-l1", status="in_progress", assignee="old-worker")
    ctl = make_lifecycle(sink, arbite_dir)
    with pytest.raises(TicketError):
        ctl.acquire(sink.get("tic-l1"), worker_id="w1", takeover=True)
    assert not active_attempts(ctl, "tic-l1")

    result = ctl.acquire(
        sink.get("tic-l1"), worker_id="w1", takeover=True, reason="old worker gone"
    )
    assert result.took_over is True
    assert result.attempt.generation == 1
    assert result.attempt.worker_id == "w1"
    assert "old worker gone" in (result.attempt.handoff or "")
    stored = sink.get("tic-l1")
    assert stored.status == "in_progress" and stored.assignee == "w1"
    assert len(active_attempts(ctl, "tic-l1")) == 1
