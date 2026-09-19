"""Board verdict == claim outcome, across every job-board restriction.

`TicketLifecycle.acquire` does not call `readiness.evaluate`; it enforces the
same rules from shared pieces (`offers.acquisition_grant`, `require_claimable`,
`eligibility.evaluate`, `require_capacity`). This matrix pins the two together:
for each worker and scenario, the board says ready exactly when a claim succeeds.
Every case gets a fresh project, so one claim never changes another's answer.
"""

from __future__ import annotations

import pytest

from arbite import application, lifecycle, offers, packages, readiness, reservations, workers
from arbite.application import Actor
from arbite.errors import ArbiteError
from arbite.query import TicketQuery
from helpers import make_ticket

RESERVED = "tic-0001"         # reserved by coord
LOCAL_ONLY = "tic-0002"       # public offer requiring a local worker
PACKAGE_FIRST = "tic-0003"    # member 1 of an unbound package
PACKAGE_SECOND = "tic-0004"   # member 2: out of package order
PLAIN = "tic-0005"
ASSIGNED = "tic-0006"         # direct assignment to w.remote
DEPENDENT = "tic-0007"        # depends on PLAIN (still open)

TICKETS = (RESERVED, LOCAL_ONLY, PACKAGE_FIRST, PACKAGE_SECOND, PLAIN, ASSIGNED, DEPENDENT)

#: What the board must report ready, per worker (verified by hand in tic-f765).
EXPECTED_READY = {
    "coord": {RESERVED, PACKAGE_FIRST, PLAIN},
    "w.capped": {PACKAGE_FIRST, PLAIN},
    "w.remote": {PACKAGE_FIRST, PLAIN, ASSIGNED},
    "w.local": {LOCAL_ONLY, PACKAGE_FIRST, PLAIN},
    "nobody": {PACKAGE_FIRST, PLAIN},
}


def _project(sink, arbite_dir) -> lifecycle.TicketLifecycle:
    service = application.coordination_service_for(
        sink, root=str(arbite_dir.parent), actor=Actor("tester")
    )
    ctl = lifecycle.TicketLifecycle(service, sink)
    for ticket_id in TICKETS:
        depends = [PLAIN] if ticket_id == DEPENDENT else []
        sink.create(make_ticket(ticket_id, status="open", tier="low", depends_on=depends))
    profiles = workers.WorkerProfileService(sink.coordination())
    profiles.register("w.capped", tier="high", capacity=1)
    profiles.register("w.remote", tier="high", locality="remote")
    profiles.register("w.local", tier="high", locality="local")
    reservations.ReservationService(ctl).create("coord", tickets=[RESERVED])
    offer_service = offers.OfferService(ctl, actor="coord")
    offer_service.publish(
        LOCAL_ONLY, agent="coord", requirements=offers.build_requirements(local_only=True)
    )
    offer_service.publish(ASSIGNED, agent="coord", mode="assigned", allowed_workers=["w.remote"])
    packages.PackageService(ctl, actor="coord").create([PACKAGE_FIRST, PACKAGE_SECOND], agent="coord")
    return ctl


@pytest.mark.parametrize("ticket_id", TICKETS)
@pytest.mark.parametrize("worker", sorted(EXPECTED_READY))
def test_board_verdict_matches_claim_outcome(sink, arbite_dir, worker, ticket_id):
    ctl = _project(sink, arbite_dir)
    store = sink.coordination()
    state = readiness.load(store, sink.query(TicketQuery(buckets=("*",))))
    verdict = readiness.evaluate(
        sink.get(ticket_id), state,
        worker_id=worker, declaration=workers.declaration_for(store, worker),
    )
    codes = [reason.code for reason in verdict.reasons]
    assert verdict.ready == (ticket_id in EXPECTED_READY[worker]), codes

    try:
        ctl.acquire(sink.get(ticket_id), worker_id=worker)
    except ArbiteError as refusal:
        assert not verdict.ready, f"board said ready but claim refused: {refusal}"
    else:
        assert verdict.ready, f"claim succeeded but board said {codes}"
