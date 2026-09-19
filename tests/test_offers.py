"""Offers and direct assignments with atomic worker pickup (planning key B03).

Store-backed tests run on both sinks through the `sink` fixture; the race tests
use spawned processes; the CLI test drives the real command in a throwaway
project. Nothing here touches the checkout's own `.arbite/` store.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

from arbite import application, coordination as c, coordination_export as x
from arbite import lifecycle, offers, reservations, workers
from arbite.application import Actor
from arbite.errors import (
    ArbiteError,
    CoordinationConflict,
    OfferConflict,
    ReservationConflict,
    TicketError,
    TicketOffered,
    TicketReserved,
    WorkerIneligible,
)
from arbite.sinks import SinkSpec, build_sink
from conftest import make_sink
from helpers import make_ticket

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def make_lifecycle(sink, arbite_dir):
    svc = application.coordination_service_for(
        sink, root=str(arbite_dir.parent), actor=Actor("tester")
    )
    return lifecycle.TicketLifecycle(svc, sink)


def offer_service(sink, arbite_dir, actor="tester"):
    return offers.OfferService(make_lifecycle(sink, arbite_dir), actor=actor)


def reserve_service(sink, arbite_dir):
    return reservations.ReservationService(make_lifecycle(sink, arbite_dir), actor="coord")


def add(sink, ticket_id, **overrides):
    overrides.setdefault("status", "open")
    sink.create(make_ticket(ticket_id, **overrides))
    return sink.get(ticket_id)


def reserved(sink, arbite_dir, *ids, owner="coord"):
    for tid in ids:
        add(sink, tid)
    return reserve_service(sink, arbite_dir).create(owner, tickets=list(ids)).stored.reservation


def offer_events(sink):
    return [e for e in sink.coordination().event_log() if e.category == "offer"]


def register(sink, worker_id, **kwargs):
    kwargs.setdefault("tier", "high")
    workers.WorkerProfileService(sink.coordination()).register(worker_id, **kwargs)


def make_offer(**overrides) -> c.Offer:
    now = c.utc_now()
    data = dict(id=c.new_record_id("offer"), tickets=["tic-a1"], mode="public",
                publisher="coord", created=now, updated=now)
    data.update(overrides)
    return c.Offer(**data)


# --- the record --------------------------------------------------------------


def test_offer_record_round_trips_and_validates():
    record = make_offer(mode="assigned", allowed_workers=["worker.a"],
                        requirements={"min_tier": "high", "capabilities": ["python"],
                                      "max_cost": {"amount": 1, "unit": "usd"}},
                        preferences={"prefer_local": True},
                        reservation_id=c.new_record_id("reservation"))
    assert record.validate() == []
    assert c.record_from_dict(json.loads(json.dumps(record.to_dict()))) == record
    assert c.is_opaque_id(record.id, "off")


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"tickets": []}, "exactly one ticket"),
        ({"tickets": ["tic-a1", "tic-a2"]}, "exactly one ticket"),
        ({"target_kind": "package"}, "target_kind"),
        ({"mode": "auction"}, "mode"),
        ({"mode": "assigned"}, "at least one allowed worker"),
        ({"allowed_workers": ["worker.a"]}, "public offer has no allowed_workers"),
        ({"requirements": {"price": 3}}, "unexpected keys"),
        ({"requirements": {"min_tier": "ultra"}}, "min_tier"),
        ({"requirements": {"max_cost": {"amount": 1}}}, "max_cost"),
        ({"preferences": {"bid": 1}}, "unexpected keys"),
        ({"state": "accepted"}, "accepting worker"),
        ({"state": "withdrawn"}, "ended_at"),
        ({"reservation_id": "tic-a1"}, "reservation id"),
    ],
)
def test_offer_validation_reports_bad_fields(overrides, fragment):
    problems = make_offer(**overrides).validate()
    assert any(fragment in problem for problem in problems), problems


def test_two_live_offers_for_one_ticket_are_a_collection_problem():
    first, second = make_offer(), make_offer()
    assert any("two live offers" in p for p in c.validate_collection([first, second]))
    second.state, second.ended_at = "withdrawn", c.utc_now()
    assert c.validate_collection([first, second]) == []


# --- publish --------------------------------------------------------------------


def test_publishing_keeps_the_reservation_and_starts_nothing(sink, kind, arbite_dir):
    rsv = reserved(sink, arbite_dir, "tic-a1")
    svc = offer_service(sink, arbite_dir)
    change = svc.publish("tic-a1", agent="coord", requirements={"min_tier": "medium"},
                         preferences={"prefer_low_cost": True}, note="pick me")
    offer = change.stored.offer
    assert change.stored.revision == 1 and offer.state == "published"
    assert offer.reservation_id == rsv.id and offer.publisher == "coord"
    assert offer.provenance["forced"] is False

    ctl = make_lifecycle(sink, arbite_dir)
    assert ctl.attempts_for("tic-a1") == []
    assert sink.get("tic-a1").status == "open" and sink.get("tic-a1").assignee is None
    assert reservations.reservation_for(sink.coordination(), "tic-a1").id == rsv.id

    reopened = make_sink(kind, arbite_dir, initialise=False)
    stored = offer_service(reopened, arbite_dir).get(offer.id)
    assert stored.offer == offer
    [event] = offer_events(reopened)
    assert event.event_kind == "offer_published"
    assert event.subject_ids == [offer.id, "tic-a1", rsv.id]
    assert event.payload["preferences"] == {"prefer_low_cost": True}
    view = offer_service(reopened, arbite_dir).view(stored)
    assert view["preferences_enforced"] is False
    assert view["ticket_status"][0]["status"] == "open"


def test_publish_refusals_write_nothing(sink, arbite_dir):
    reserved(sink, arbite_dir, "tic-a1")
    add(sink, "tic-bl", status="blocked")
    add(sink, "tic-as", assignee="someone")
    add(sink, "tic-run")
    svc = offer_service(sink, arbite_dir)
    make_lifecycle(sink, arbite_dir).acquire(sink.get("tic-run"), worker_id="worker.b")

    def reason_of(call):
        with pytest.raises(OfferConflict) as excinfo:
            call()
        return excinfo.value.details["reason"]

    assert reason_of(lambda: svc.publish("tic-a1", agent="intruder")) == "not_owner"
    assert reason_of(lambda: svc.publish("tic-a1", agent="intruder", force=True)) == "not_owner"
    for tid in ("tic-bl", "tic-as", "tic-run"):
        assert reason_of(lambda: svc.publish(tid, agent="coord")) == "ticket_unavailable"
    assert offer_events(sink) == [] and svc.list(state="all") == []

    forced = svc.publish("tic-a1", agent="admin", force=True, reason="owner is away").stored
    assert forced.offer.provenance["forced"] is True
    assert forced.offer.provenance["reservation_owner"] == "coord"
    assert reason_of(lambda: svc.publish("tic-a1", agent="coord")) == "already_offered"
    with pytest.raises(ArbiteError):
        svc.publish("tic-run", agent="coord", mode="assigned")  # no worker named


# --- acceptance -----------------------------------------------------------------


def test_offer_claim_creates_the_attempt_and_accepts_atomically(sink, kind, arbite_dir):
    rsv = reserved(sink, arbite_dir, "tic-a1")
    svc = offer_service(sink, arbite_dir)
    offer_id = svc.publish("tic-a1", agent="coord").stored.offer.id

    result, stored = svc.claim(offer_id, worker_id="worker.b")
    assert result.offer_id == offer_id and result.attempt.worker_id == "worker.b"
    assert stored.revision == 2 and stored.offer.state == "accepted"
    assert stored.offer.accepted_by == "worker.b" and stored.offer.attempt_id == result.attempt.id
    ticket = sink.get("tic-a1")
    assert ticket.status == "in_progress" and ticket.assignee == "worker.b"
    # Publishing and pickup left the coordinator's reservation in place.
    assert reservations.reservation_for(sink.coordination(), "tic-a1").id == rsv.id

    reopened = make_sink(kind, arbite_dir, initialise=False)
    kinds = [e.event_kind for e in reopened.coordination().event_log()
             if e.category in ("offer", "lifecycle")]
    assert kinds[-3:] == ["attempt_started", "ticket_claimed", "offer_accepted"]
    accepted = [e for e in offer_events(reopened) if e.event_kind == "offer_accepted"][0]
    assert accepted.payload["attempt_id"] == result.attempt.id

    with pytest.raises(OfferConflict) as excinfo:
        svc.claim(offer_id, worker_id="worker.c")
    assert excinfo.value.details["reason"] == "not_published"
    assert excinfo.value.details["state"] == "accepted"
    assert [a.worker_id for a in make_lifecycle(sink, arbite_dir).attempts_for("tic-a1")] == [
        "worker.b"
    ]


def test_plain_claim_routes_through_a_public_offer(sink, arbite_dir):
    reserved(sink, arbite_dir, "tic-a1")
    offer_id = offer_service(sink, arbite_dir).publish("tic-a1", agent="coord").stored.offer.id
    result = make_lifecycle(sink, arbite_dir).acquire(sink.get("tic-a1"), worker_id="worker.b")
    assert result.offer_id == offer_id
    assert offer_service(sink, arbite_dir).get(offer_id).offer.accepted_by == "worker.b"


def test_requirements_are_restricted_and_preferences_are_not_enforced(sink, arbite_dir):
    reserved(sink, arbite_dir, "tic-a1", "tic-a2")
    register(sink, "remote.pro", tier="high", capabilities=["python"], locality="remote",
             cost_class="paid")
    register(sink, "small", tier="low", capabilities=["python"], locality="local")
    svc = offer_service(sink, arbite_dir)
    ctl = make_lifecycle(sink, arbite_dir)
    strict = svc.publish("tic-a1", agent="coord",
                         requirements={"min_tier": "high", "capabilities": ["python"]}).stored

    with pytest.raises(WorkerIneligible) as excinfo:
        ctl.acquire(sink.get("tic-a1"), worker_id="adhoc.worker")
    codes = {r["code"] for r in excinfo.value.details["reasons"]}
    assert codes == {"tier_unknown", "capabilities_unknown"}
    assert excinfo.value.details["offer_id"] == strict.offer.id
    with pytest.raises(WorkerIneligible) as excinfo:
        ctl.acquire(sink.get("tic-a1"), worker_id="small")
    assert {r["code"] for r in excinfo.value.details["reasons"]} == {"tier_insufficient"}
    # An ad-hoc declared tier satisfies the tier but not the capability.
    with pytest.raises(WorkerIneligible):
        svc.claim(strict.offer.id, worker_id="adhoc.worker", declared_tier="high")
    assert ctl.attempts_for("tic-a1") == []

    hinted = svc.publish("tic-a2", agent="coord",
                         preferences={"prefer_local": True, "prefer_low_cost": True,
                                      "prefer_workers": ["small"]}).stored
    # A remote, paid worker still wins a preference-only offer: first eligible claimant.
    result, stored = svc.claim(hinted.offer.id, worker_id="remote.pro")
    assert stored.offer.accepted_by == "remote.pro"
    assert svc.claim(strict.offer.id, worker_id="remote.pro")[1].offer.state == "accepted"


def test_direct_assignment_cannot_be_bypassed(sink, arbite_dir):
    reserved(sink, arbite_dir, "tic-a1")
    svc = offer_service(sink, arbite_dir)
    ctl = make_lifecycle(sink, arbite_dir)
    offer = svc.publish("tic-a1", agent="coord", mode="assigned",
                        allowed_workers=["worker.a"]).stored.offer

    for who, kwargs in [("worker.b", {}), ("worker.b", {"takeover": True, "reason": "mine"}),
                        ("coord", {}), ("coord", {"takeover": True, "reason": "owner"})]:
        with pytest.raises(WorkerIneligible) as excinfo:
            ctl.acquire(sink.get("tic-a1"), worker_id=who, **kwargs)
        assert excinfo.value.details["reasons"][0]["code"] == "worker_not_allowed"
    with pytest.raises(WorkerIneligible):
        svc.claim(offer.id, worker_id="worker.b")
    assert ctl.attempts_for("tic-a1") == []

    # set/delete cannot hand it out either.
    ticket = sink.get("tic-a1")
    with pytest.raises(TicketOffered):
        ctl.refuse_reserved_edit(ticket, new_status="in_progress", new_assignee=None)
    with pytest.raises(TicketOffered):
        ctl.refuse_reserved_edit(ticket, new_status=None, new_assignee="worker.b")
    with pytest.raises(TicketOffered):
        ctl.require_deletable(ticket)

    result = ctl.acquire(sink.get("tic-a1"), worker_id="worker.a")
    assert result.offer_id == offer.id


def test_unreserved_assignment_restricts_plain_claims(sink, arbite_dir):
    add(sink, "tic-u1")
    svc = offer_service(sink, arbite_dir)
    offer = svc.publish("tic-u1", agent="lead", mode="assigned",
                        allowed_workers=["worker.a"]).stored.offer
    assert offer.reservation_id is None
    ctl = make_lifecycle(sink, arbite_dir)
    with pytest.raises(WorkerIneligible):
        ctl.acquire(sink.get("tic-u1"), worker_id="lead")
    svc.withdraw(offer.id, agent="lead")
    assert ctl.acquire(sink.get("tic-u1"), worker_id="lead").offer_id is None


# --- withdrawal -----------------------------------------------------------------


def test_withdrawal_removes_eligibility(sink, arbite_dir):
    reserved(sink, arbite_dir, "tic-a1")
    svc = offer_service(sink, arbite_dir)
    offer = svc.publish("tic-a1", agent="coord").stored.offer

    with pytest.raises(OfferConflict) as excinfo:
        svc.withdraw(offer.id, agent="worker.b")
    assert excinfo.value.details["reason"] == "not_publisher"
    with pytest.raises(CoordinationConflict):
        svc.withdraw(offer.id, agent="coord", expect_revision=7)

    change = svc.withdraw(offer.id, agent="coord", reason="replan")
    assert change.stored.offer.state == "withdrawn" and change.stored.revision == 2
    assert change.stored.offer.end_reason == "replan"
    assert change.event.event_kind == "offer_withdrawn"

    ctl = make_lifecycle(sink, arbite_dir)
    with pytest.raises(TicketReserved):  # back to owner-only
        ctl.acquire(sink.get("tic-a1"), worker_id="worker.b")
    with pytest.raises(OfferConflict) as excinfo:
        svc.claim(offer.id, worker_id="worker.b")
    assert excinfo.value.details["state"] == "withdrawn"
    with pytest.raises(OfferConflict) as excinfo:
        svc.withdraw(offer.id, agent="coord")
    assert excinfo.value.details["reason"] == "not_live"
    # A new offer may be published after the old one ended.
    assert svc.publish("tic-a1", agent="coord").stored.offer.state == "published"


def test_withdrawing_an_accepted_offer_needs_explicit_interruption(sink, kind, arbite_dir):
    reserved(sink, arbite_dir, "tic-a1")
    svc = offer_service(sink, arbite_dir)
    offer = svc.publish("tic-a1", agent="coord").stored.offer
    result, _ = svc.claim(offer.id, worker_id="worker.b")

    with pytest.raises(OfferConflict) as excinfo:
        svc.withdraw(offer.id, agent="coord")
    assert excinfo.value.details["reason"] == "accepted"
    assert excinfo.value.details["active"][0]["attempt_id"] == result.attempt.id
    with pytest.raises(OfferConflict) as excinfo:
        svc.withdraw(offer.id, agent="coord", interrupt=True)
    assert excinfo.value.details["reason"] == "reason_required"
    ctl = make_lifecycle(sink, arbite_dir)
    assert ctl.active_attempt("tic-a1").id == result.attempt.id

    change = svc.withdraw(offer.id, agent="coord", interrupt=True, reason="wrong worker")
    assert change.interrupted[0]["worker_id"] == "worker.b"
    assert change.stored.offer.state == "cancelled"
    assert "wrong worker" in change.stored.offer.end_reason
    [attempt] = ctl.attempts_for("tic-a1")
    assert attempt.state == "interrupted"
    reopened = make_sink(kind, arbite_dir, initialise=False)
    assert reopened.get("tic-a1").status == "open" and reopened.get("tic-a1").assignee is None


# --- offers track their ticket ---------------------------------------------------


def _close(ctl, ticket_id, agent):
    import copy

    ticket = ctl.tickets.get(ticket_id, unique=True)
    before = copy.deepcopy(ticket)
    ticket.status, ticket.closed = "closed", "2026-09-18T00:00:00"
    attempt = ctl.active_attempt(ticket_id)
    ctl.commit_transition("close", ticket, previous=before, end=attempt,
                          end_state="finished" if attempt else None, reason="done")


def _release(ctl, ticket_id):
    import copy

    ticket = ctl.tickets.get(ticket_id, unique=True)
    before = copy.deepcopy(ticket)
    ticket.status, ticket.assignee = "open", None
    ctl.commit_transition("release", ticket, previous=before,
                          end=ctl.active_attempt(ticket_id), end_state="released")


def test_offer_completes_with_ticket_close_and_cancels_on_release(sink, arbite_dir):
    reserved(sink, arbite_dir, "tic-a1", "tic-a2", "tic-a3")
    svc = offer_service(sink, arbite_dir)
    ctl = make_lifecycle(sink, arbite_dir)
    done = svc.publish("tic-a1", agent="coord").stored.offer
    svc.claim(done.id, worker_id="worker.b")
    _close(ctl, "tic-a1", "worker.b")
    completed = svc.get(done.id)
    assert completed.offer.state == "completed" and completed.offer.accepted_by == "worker.b"

    dropped = svc.publish("tic-a2", agent="coord").stored.offer
    svc.claim(dropped.id, worker_id="worker.b")
    _release(ctl, "tic-a2")
    assert svc.get(dropped.id).offer.state == "cancelled"
    # The ticket is back under the reservation; the owner can offer it again.
    with pytest.raises(TicketReserved):
        ctl.acquire(sink.get("tic-a2"), worker_id="worker.c")
    assert svc.publish("tic-a2", agent="coord").stored.offer.state == "published"

    unclaimed = svc.publish("tic-a3", agent="coord").stored.offer
    _close(ctl, "tic-a3", "coord")
    assert svc.get(unclaimed.id).offer.state == "cancelled"
    kinds = [e.event_kind for e in offer_events(sink)]
    assert kinds.count("offer_completed") == 1 and kinds.count("offer_cancelled") == 2


def test_owner_takeover_cancels_the_accepted_offer(sink, arbite_dir):
    reserved(sink, arbite_dir, "tic-a1")
    svc = offer_service(sink, arbite_dir)
    offer = svc.publish("tic-a1", agent="coord").stored.offer
    svc.claim(offer.id, worker_id="worker.b")
    ctl = make_lifecycle(sink, arbite_dir)
    with pytest.raises(TicketReserved):  # the offer is consumed; others need the owner
        ctl.acquire(sink.get("tic-a1"), worker_id="worker.c", takeover=True, reason="x")
    taken = ctl.acquire(sink.get("tic-a1"), worker_id="coord", takeover=True, reason="stalled")
    assert taken.took_over and taken.offer_id is None
    assert svc.get(offer.id).offer.state == "cancelled"


def test_acceptance_crash_after_ticket_write_rolls_the_offer_forward(sink, arbite_dir):
    reserved(sink, arbite_dir, "tic-a1")
    offer = offer_service(sink, arbite_dir).publish("tic-a1", agent="coord").stored.offer
    crashing = make_lifecycle(sink, arbite_dir)

    def crash(phase):
        if phase == lifecycle.FAULT_AFTER_TICKET_WRITE:
            raise lifecycle.LifecycleFault(phase)

    crashing._fault = crash
    with pytest.raises(lifecycle.LifecycleFault):
        crashing.acquire(sink.get("tic-a1"), worker_id="worker.b")
    assert offer_service(sink, arbite_dir).get(offer.id).offer.state == "published"

    ctl = make_lifecycle(sink, arbite_dir)
    ctl.reconcile_lifecycle()
    stored = offer_service(sink, arbite_dir).get(offer.id)
    assert stored.offer.state == "accepted"
    assert stored.offer.attempt_id == ctl.active_attempt("tic-a1").id


def test_acceptance_is_refused_when_the_offer_moved_under_the_cascade(sink, arbite_dir):
    """The cascade re-checks the offer revision: a mismatch compensates the
    ticket write, so no attempt exists without its accepted offer."""
    reserved(sink, arbite_dir, "tic-a1")
    offer = offer_service(sink, arbite_dir).publish("tic-a1", agent="coord").stored.offer
    ctl = make_lifecycle(sink, arbite_dir)
    original = offers.acceptance_for

    def stale(grant, worker_id):
        data = original(grant, worker_id)
        data["expected_revision"] = 99
        return data

    offers.acceptance_for = stale
    try:
        with pytest.raises(OfferConflict):
            ctl.acquire(sink.get("tic-a1"), worker_id="worker.b")
    finally:
        offers.acceptance_for = original
    assert ctl.active_attempt("tic-a1") is None
    assert sink.get("tic-a1").status == "open" and sink.get("tic-a1").assignee is None
    assert offer_service(sink, arbite_dir).get(offer.id).offer.state == "published"


# --- reservations ------------------------------------------------------------------


def test_releasing_a_reservation_withdraws_its_offers(sink, arbite_dir):
    rsv = reserved(sink, arbite_dir, "tic-a1", "tic-a2", "tic-a3")
    svc = offer_service(sink, arbite_dir)
    rsvc = reserve_service(sink, arbite_dir)
    open_offer = svc.publish("tic-a1", agent="coord").stored.offer
    removed_offer = svc.publish("tic-a3", agent="coord").stored.offer
    busy_offer = svc.publish("tic-a2", agent="coord", mode="assigned",
                             allowed_workers=["worker.a"]).stored.offer
    svc.claim(busy_offer.id, worker_id="worker.a")

    change = rsvc.remove(rsv.id, agent="coord", tickets=["tic-a3"])
    assert change.withdrawn_offers == [removed_offer.id]
    assert svc.get(removed_offer.id).offer.state == "withdrawn"

    with pytest.raises(ReservationConflict) as excinfo:
        rsvc.release(rsv.id, agent="coord")
    assert excinfo.value.details["reason"] == "active_attempts"
    assert svc.get(open_offer.id).offer.state == "published"  # refusal wrote nothing

    change = rsvc.release(rsv.id, agent="coord", interrupt=True, reason="replan")
    assert change.withdrawn_offers == [open_offer.id]
    assert svc.get(open_offer.id).offer.state == "withdrawn"
    assert svc.get(busy_offer.id).offer.state == "cancelled"
    assert change.event.payload["withdrawn_offers"] == [open_offer.id]
    ctl = make_lifecycle(sink, arbite_dir)
    assert ctl.acquire(sink.get("tic-a1"), worker_id="anyone").offer_id is None


def test_reserving_a_ticket_offered_by_someone_else_is_refused(sink, arbite_dir):
    add(sink, "tic-u1")
    offer_service(sink, arbite_dir).publish("tic-u1", agent="lead")
    with pytest.raises(ReservationConflict) as excinfo:
        reserve_service(sink, arbite_dir).create("coord", tickets=["tic-u1"])
    assert excinfo.value.details["conflicts"][0]["reason"] == "offered_elsewhere"
    assert reserve_service(sink, arbite_dir).create("lead", tickets=["tic-u1"])


# --- export ---------------------------------------------------------------------


def test_offers_travel_in_export_bundles(sink, kind, arbite_dir, tmp_path):
    reserved(sink, arbite_dir, "tic-a1")
    offer = offer_service(sink, arbite_dir).publish("tic-a1", agent="coord").stored.offer
    bundle = x.export_coordination(sink)
    assert [o["id"] for o in bundle["records"]["offers"]] == [offer.id]
    assert x.bundle_problems(bundle) == []

    other = "sqlite" if kind == "file" else "file"
    target_dir = tmp_path / "target" / ".arbite"
    target_dir.mkdir(parents=True)
    target = make_sink(other, target_dir)
    x.import_coordination(target, bundle)
    assert offers.live_offer_for(target.coordination(), "tic-a1").offer.id == offer.id

    legacy = json.loads(json.dumps(bundle))
    del legacy["records"]["offers"]
    legacy["events"] = [e for e in legacy["events"] if e["category"] != "offer"]
    assert x.bundle_problems(legacy) == []


# --- multiprocess races -------------------------------------------------------------


def _racer(kind, arbite_dir, action, target, barrier, results):
    sink = build_sink(SinkSpec(kind=kind), Path(arbite_dir))
    svc = application.coordination_service_for(
        sink, root=str(Path(arbite_dir).parent), actor=Actor(action[1])
    )
    ctl = lifecycle.TicketLifecycle(svc, sink)
    barrier.wait(30)
    try:
        if action[0] == "accept":
            offers.OfferService(ctl).claim(target, worker_id=action[1])
        elif action[0] == "claim":
            ctl.acquire(sink.get(target), worker_id=action[1])
        else:
            offers.OfferService(ctl).withdraw(target, agent=action[1])
    except ArbiteError as e:
        results.put((action[1], getattr(e, "error_code", type(e).__name__)))
    else:
        results.put((action[1], "ok"))


def _race(kind, arbite_dir, plans):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(len(plans))
    results = context.Queue()
    processes = [
        context.Process(target=_racer,
                        args=(kind, str(arbite_dir), action, target, barrier, results))
        for action, target in plans
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(90)
    assert [p.exitcode for p in processes] == [0] * len(plans)
    return dict(results.get(timeout=10) for _ in plans)


def test_simultaneous_acceptance_has_one_winner(sink, kind, arbite_dir):
    reserved(sink, arbite_dir, "tic-a1")
    offer = offer_service(sink, arbite_dir).publish("tic-a1", agent="coord").stored.offer
    outcome = _race(kind, arbite_dir, [
        (("accept", "worker.1"), offer.id),
        (("accept", "worker.2"), offer.id),
        (("claim", "worker.3"), "tic-a1"),
    ])
    winners = [who for who, result in outcome.items() if result == "ok"]
    assert len(winners) == 1, outcome
    ctl = make_lifecycle(sink, arbite_dir)
    attempts = ctl.attempts_for("tic-a1")
    assert [a.worker_id for a in attempts] == winners
    stored = offer_service(sink, arbite_dir).get(offer.id)
    assert stored.offer.accepted_by == winners[0] and stored.revision == 2
    assert [e.event_kind for e in offer_events(sink)].count("offer_accepted") == 1


def test_withdraw_racing_accept_has_one_serial_outcome(sink, kind, arbite_dir):
    reserved(sink, arbite_dir, "tic-a1")
    offer = offer_service(sink, arbite_dir).publish("tic-a1", agent="coord").stored.offer
    outcome = _race(kind, arbite_dir, [
        (("withdraw", "coord"), offer.id),
        (("accept", "worker.b"), offer.id),
    ])
    ctl = make_lifecycle(sink, arbite_dir)
    stored = offer_service(sink, arbite_dir).get(offer.id).offer
    if outcome["coord"] == "ok":
        assert outcome["worker.b"] == "offer_conflict"
        assert stored.state == "withdrawn" and ctl.attempts_for("tic-a1") == []
    else:
        assert outcome == {"coord": "offer_conflict", "worker.b": "ok"}
        assert stored.state == "accepted"
        assert [a.worker_id for a in ctl.attempts_for("tic-a1")] == ["worker.b"]


# --- the CLI ------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_project, kind):
    def run(*args, expect=0):
        environment = dict(os.environ, PYTHONPATH=str(SRC_DIR), ARBITE_SINK=kind)
        proc = subprocess.run(
            [sys.executable, "-m", "arbite.cli", *args],
            cwd=str(tmp_project), env=environment, capture_output=True, text=True,
        )
        assert proc.returncode == expect, (
            f"arbite {' '.join(args)} -> {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
        )
        return proc

    run("init")
    return run


def created_id(proc) -> str:
    import re

    return re.search(r"(tic-[0-9a-f]{4})", proc.stdout).group(1)


def test_cli_offer_flow(cli):
    make = lambda title: created_id(cli(  # noqa: E731
        "create", "--title", title, "--type", "bug", "--tier", "low", "--domain", "py",
    ))
    t1, t2, t3 = make("one"), make("two"), make("three")
    cli("offer", "list", expect=2)
    rid = json.loads(cli("reserve", "create", t1, t2, t3, "--agent", "coord",
                         "--json").stdout)["data"]["reservation"]["id"]

    assigned = json.loads(cli("offer", "assign", t1, "--worker", "worker.a", "--agent", "coord",
                              "--json").stdout)["data"]
    a_offer = assigned["offer"]["id"]
    assert assigned["offer"]["mode"] == "assigned" and assigned["offer"]["reservation_id"] == rid
    assert assigned["assignee_eligibility"][0]["eligible"] is True
    public = json.loads(cli("offer", "publish", t2, "--agent", "coord", "--prefer-local",
                            "--json").stdout)["data"]["offer"]
    assert public["preferences"] == {"prefer_local": True}
    assert public["preferences_enforced"] is False

    refused = json.loads(cli("offer", "claim", a_offer, "--agent", "worker.b", "--json",
                             expect=1).stdout)
    assert refused["code"] == "worker_ineligible"
    assert refused["details"]["reasons"][0]["code"] == "worker_not_allowed"
    cli("claim", t1, "--agent", "worker.b", expect=1)
    cli("claim", t1, "--agent", "worker.b", "--force", "--reason", "mine", expect=1)
    cli("set", t1, "assignee", "worker.b", expect=1)

    plain = cli("list", "next", expect=2)
    assert "offers/assignments" in plain.stderr
    picked = json.loads(cli("list", "next", "--claim", "worker.b", "--json").stdout)
    assert [t["id"] for t in picked] == [t2]
    mine = json.loads(cli("offer", "list", "--worker", "worker.a", "--json").stdout)["data"]
    assert [o["id"] for o in mine["offers"]] == [a_offer]

    busy = json.loads(cli("offer", "withdraw", public["id"], "--agent", "coord", "--json",
                          expect=1).stdout)
    assert busy["code"] == "offer_conflict" and busy["details"]["reason"] == "accepted"

    claimed = json.loads(cli("offer", "claim", a_offer, "--agent", "worker.a",
                             "--json").stdout)["data"]
    assert claimed["offer"]["state"] == "accepted" and claimed["attempt_id"].startswith("att-")
    cli("close", t1, "--agent", "worker.a")
    shown = json.loads(cli("offer", "show", a_offer, "--json").stdout)["data"]["offer"]
    assert shown["state"] == "completed"

    third = json.loads(cli("offer", "publish", t3, "--agent", "coord", "--min-tier", "high",
                           "--json").stdout)["data"]["offer"]["id"]
    explained = json.loads(cli("offer", "show", third, "--worker", "adhoc", "--json").stdout)
    assert explained["data"]["offer"]["eligibility"]["eligible"] is False
    withdrawn = json.loads(cli("offer", "withdraw", third, "--agent", "coord", "--reason", "x",
                               "--json").stdout)["data"]["offer"]
    assert withdrawn["state"] == "withdrawn"
    listed = json.loads(cli("offer", "list", "--state", "all", "--json").stdout)["data"]
    assert listed["count"] == 3
    human = cli("offer", "show", public["id"]).stdout
    assert "NOT enforced" in human and "first eligible" in human
    counts = json.loads(cli("export", "--json").stdout)
    assert "offers" in json.dumps(counts)
