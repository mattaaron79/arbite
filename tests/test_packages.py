"""Ordered same-worker continuity packages and explicit handoff (planning key B04).

Store-backed tests run on both sinks through the `sink` fixture; the CLI test
drives the real command in a throwaway project. Nothing here touches the
checkout's own `.arbite/` store.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from arbite import application, coordination as c, coordination_export as x
from arbite import graph, lifecycle, offers, packages, reservations, workers
from arbite.application import Actor
from arbite.errors import (
    InvalidRecord,
    OfferConflict,
    PackageConflict,
    ReservationConflict,
    TicketError,
    TicketPackaged,
    WorkerIneligible,
)
from conftest import make_sink
from helpers import make_ticket

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def make_lifecycle(sink, arbite_dir):
    svc = application.coordination_service_for(
        sink, root=str(arbite_dir.parent), actor=Actor("tester")
    )
    return lifecycle.TicketLifecycle(svc, sink)


def pkg_service(sink, arbite_dir):
    return packages.PackageService(make_lifecycle(sink, arbite_dir), actor="coord")


def offer_service(sink, arbite_dir):
    return offers.OfferService(make_lifecycle(sink, arbite_dir), actor="coord")


def add(sink, ticket_id, **overrides):
    overrides.setdefault("status", "open")
    sink.create(make_ticket(ticket_id, **overrides))
    return sink.get(ticket_id)


def fresh(sink, arbite_dir, *ids, agent="coord"):
    for tid in ids:
        add(sink, tid)
    return pkg_service(sink, arbite_dir).create(list(ids), agent=agent).stored.package


def acquire(sink, arbite_dir, ticket_id, worker, **kwargs):
    ctl = make_lifecycle(sink, arbite_dir)
    return ctl.acquire(sink.get(ticket_id), worker_id=worker, **kwargs)


def close(sink, arbite_dir, ticket_id):
    ctl = make_lifecycle(sink, arbite_dir)
    ticket = sink.get(ticket_id)
    before = copy.deepcopy(ticket)
    ticket.status, ticket.closed = "closed", "2026-09-18T00:00:00"
    attempt = ctl.active_attempt(ticket_id)
    ctl.commit_transition("close", ticket, previous=before, end=attempt,
                          end_state="finished" if attempt else None, reason="done",
                          sweep_ticket_claims=True)


def block(sink, arbite_dir, ticket_id, why="stuck"):
    ctl = make_lifecycle(sink, arbite_dir)
    ticket = sink.get(ticket_id)
    before = copy.deepcopy(ticket)
    ticket.status, ticket.blocked_by = "blocked", why
    attempt = ctl.active_attempt(ticket_id)
    ctl.commit_transition("block", ticket, previous=before, end=attempt,
                          end_state="interrupted" if attempt else None, reason=why)


def stored_pkg(sink, arbite_dir, package_id):
    return pkg_service(sink, arbite_dir).get(package_id)


def package_events(sink):
    return [e for e in sink.coordination().event_log() if e.category == "package"]


# --- record validation ------------------------------------------------------------


def test_package_record_round_trip_and_validation():
    now = c.utc_now()
    package = c.Package(id=c.new_record_id("package"), tickets=["tic-a1", "tic-a2"],
                        created_by="coord", created=now, updated=now)
    assert package.validate() == []
    assert c.record_from_dict(package.to_dict()) == package
    assert package.id.startswith("pkg-")
    bad = copy.deepcopy(package)
    bad.tickets = ["tic-a1", "tic-a1"]
    assert any("repeat" in p for p in bad.validate())
    bad = copy.deepcopy(package)
    bad.state = "bound"
    assert any("bound_worker" in p for p in bad.validate())
    single = copy.deepcopy(package)
    single.tickets = ["tic-a1"]
    assert any("at least two" in p for p in single.validate())
    other = copy.deepcopy(package)
    other.id = c.new_record_id("package")
    assert any("two live packages" in p for p in c.validate_collection([package, other]))


def test_combined_graph_cycles_include_package_order():
    a = make_ticket("tic-a1", depends_on=["tic-a2"])
    b = make_ticket("tic-a2")
    by_id = {"tic-a1": a, "tic-a2": b}
    assert graph.find_cycles(by_id) == []
    # Package order a1 -> a2 means a2 waits for a1; a1 depends on a2: a cycle.
    assert graph.combined_cycles(by_id, [["tic-a1", "tic-a2"]])
    assert graph.combined_cycles(by_id, [["tic-a2", "tic-a1"]]) == []


# --- creation -----------------------------------------------------------------


def test_create_starts_nothing_and_records_an_event(sink, kind, arbite_dir):
    package = fresh(sink, arbite_dir, "tic-a1", "tic-a2", "tic-a3")
    assert package.state == "open" and package.bound_worker is None
    assert package.current == "tic-a1" and package.tickets == ["tic-a1", "tic-a2", "tic-a3"]
    assert all(sink.get(t).status == "open" for t in package.tickets)
    assert make_lifecycle(sink, arbite_dir).attempts_for("tic-a1") == []
    [event] = package_events(sink)
    assert event.kind_ == "package_created" and package.id in event.subject_ids
    # durable across reopen
    reopened = make_sink(kind, arbite_dir, initialise=False)
    assert stored_pkg(reopened, arbite_dir, package.id).package.tickets == package.tickets


def test_create_refuses_duplicates_membership_and_unavailable_members(sink, arbite_dir):
    fresh(sink, arbite_dir, "tic-a1", "tic-a2")
    add(sink, "tic-b1")
    add(sink, "tic-b2", status="closed", closed="2026-01-02T00:00:00")
    service = pkg_service(sink, arbite_dir)
    with pytest.raises(PackageConflict) as excinfo:
        service.create(["tic-b1", "tic-b1"], agent="coord")
    assert excinfo.value.details["reason"] == "duplicate_member"
    with pytest.raises(PackageConflict) as excinfo:
        service.create(["tic-b1", "tic-a2"], agent="coord")
    conflict = excinfo.value.details["conflicts"][0]
    assert conflict["reason"] == "already_packaged" and conflict["ticket_id"] == "tic-a2"
    with pytest.raises(PackageConflict) as excinfo:
        service.create(["tic-b1", "tic-b2"], agent="coord")
    assert excinfo.value.details["conflicts"][0]["reason"] == "not_open"
    with pytest.raises(InvalidRecord):
        service.create(["tic-b1"], agent="coord")
    assert len(service.list(state="all")) == 1


def test_create_refuses_combined_cycles(sink, arbite_dir):
    add(sink, "tic-a1", depends_on=["tic-a2"])
    add(sink, "tic-a2")
    service = pkg_service(sink, arbite_dir)
    with pytest.raises(PackageConflict) as excinfo:
        service.create(["tic-a1", "tic-a2"], agent="coord")
    assert excinfo.value.details["reason"] == "cycle"
    assert set(excinfo.value.details["cycles"][0]) == {"tic-a1", "tic-a2"}
    # The same tickets in dependency-compatible order are fine.
    assert service.create(["tic-a2", "tic-a1"], agent="coord").stored.package.state == "open"


def test_cycle_across_two_packages_is_refused(sink, arbite_dir):
    add(sink, "tic-b1", depends_on=["tic-a2"])
    add(sink, "tic-b2")
    add(sink, "tic-a1", depends_on=["tic-b2"])  # external prerequisite of P1
    add(sink, "tic-a2")
    service = pkg_service(sink, arbite_dir)
    service.create(["tic-a1", "tic-a2"], agent="coord")
    # b2 waits for b1 (new order), b1 needs a2, a2 waits for a1 (P1 order), a1 needs b2.
    with pytest.raises(PackageConflict) as excinfo:
        service.create(["tic-b1", "tic-b2"], agent="coord")
    assert excinfo.value.details["reason"] == "cycle"
    assert set(excinfo.value.details["cycles"][0]) == {"tic-a1", "tic-a2", "tic-b1", "tic-b2"}


def test_reserved_members_need_the_owner(sink, arbite_dir):
    add(sink, "tic-a1")
    add(sink, "tic-a2")
    ctl = make_lifecycle(sink, arbite_dir)
    rsv = reservations.ReservationService(ctl).create("coord", tickets=["tic-a1", "tic-a2"])
    with pytest.raises(PackageConflict) as excinfo:
        pkg_service(sink, arbite_dir).create(["tic-a1", "tic-a2"], agent="other")
    assert excinfo.value.details["conflicts"][0]["reason"] == "reserved_elsewhere"
    package = pkg_service(sink, arbite_dir).create(["tic-a1", "tic-a2"], agent="coord")
    assert package.stored.package.reservation_id == rsv.stored.reservation.id


# --- ordering, binding, continuity -----------------------------------------------


def test_two_member_continuity(sink, arbite_dir):
    package = fresh(sink, arbite_dir, "tic-a1", "tic-a2")
    # Later member: never early, by any path.
    for kwargs in ({}, {"takeover": True, "reason": "mine"}):
        with pytest.raises(TicketPackaged) as excinfo:
            acquire(sink, arbite_dir, "tic-a2", "worker.b", **kwargs)
        assert excinfo.value.details["reason"] == "package_order"
    assert sink.get("tic-a2").status == "open"

    first = acquire(sink, arbite_dir, "tic-a1", "worker.b")
    bound = stored_pkg(sink, arbite_dir, package.id).package
    assert bound.state == "bound" and bound.bound_worker == "worker.b"
    assert sink.get("tic-a2").status == "open"  # still not in progress
    with pytest.raises(TicketPackaged):
        acquire(sink, arbite_dir, "tic-a2", "worker.b")  # a1 not closed yet

    # A file claim held under member 1 is released when it closes.
    svc = make_lifecycle(sink, arbite_dir)
    claims = svc.coordination.store
    from arbite.fileclaims import FileClaimService
    FileClaimService(svc.coordination, root=str(arbite_dir.parent)).claim(first.attempt,
                                                                          ["x.py"])
    with claims.transaction(write=False) as tx:
        assert [cl.path for cl in tx.find("file_claim") if cl.is_active] == ["x.py"]
    close(sink, arbite_dir, "tic-a1")
    with claims.transaction(write=False) as tx:
        assert [cl for cl in tx.find("file_claim") if cl.is_active] == []

    advanced = stored_pkg(sink, arbite_dir, package.id).package
    assert advanced.current == "tic-a2" and advanced.state == "bound"
    with pytest.raises(TicketPackaged) as excinfo:
        acquire(sink, arbite_dir, "tic-a2", "worker.c")
    assert excinfo.value.details["reason"] == "bound_elsewhere"
    second = acquire(sink, arbite_dir, "tic-a2", "worker.b")
    assert second.attempt.id != first.attempt.id and second.attempt.generation == 1
    close(sink, arbite_dir, "tic-a2")
    done = stored_pkg(sink, arbite_dir, package.id).package
    assert done.state == "completed" and done.ended_at
    kinds = [e.kind_ for e in package_events(sink)]
    assert kinds == ["package_created", "package_bound", "package_member_started",
                     "package_advanced", "package_member_started", "package_completed"]


def test_crash_resume_identity_uses_package_claim_and_notes(sink, arbite_dir):
    package = fresh(sink, arbite_dir, "tic-a1", "tic-a2")
    service = pkg_service(sink, arbite_dir)
    first = service.claim(package.id, worker_id="worker.b")
    assert first.ticket.id == "tic-a1"
    service.note(package.id, agent="worker.b", text="a1 done except docs; a2 needs schema v3")
    close(sink, arbite_dir, "tic-a1")
    # A new session (fresh service objects) under the same worker id resumes.
    resumed = pkg_service(sink, arbite_dir)
    view = resumed.view(resumed.get(package.id))
    assert view["progress"]["current"] == "tic-a2" and view["bound_worker"] == "worker.b"
    assert view["notes"][-1]["text"].startswith("a1 done")
    assert "claims tic-a2" in view["next_action"]
    assert view["members"][0]["latest_attempt"]["state"] == "finished"
    with pytest.raises(PackageConflict) as excinfo:
        resumed.note(package.id, agent="stranger", text="hi")
    assert excinfo.value.details["reason"] == "not_controller"
    second = resumed.claim(package.id, worker_id="worker.b")
    assert second.ticket.id == "tic-a2" and second.attempt.worker_id == "worker.b"


def test_set_and_delete_cannot_bypass_the_package(sink, arbite_dir):
    fresh(sink, arbite_dir, "tic-a1", "tic-a2")
    ctl = make_lifecycle(sink, arbite_dir)
    ticket = sink.get("tic-a2")
    with pytest.raises(TicketPackaged):
        ctl.refuse_reserved_edit(ticket, new_status="in_progress", new_assignee=None)
    with pytest.raises(TicketPackaged):
        ctl.refuse_reserved_edit(ticket, new_status=None, new_assignee="worker.x")
    with pytest.raises(TicketPackaged):
        ctl.require_deletable(ticket)


def test_external_prerequisite_pauses_without_claims(sink, arbite_dir):
    add(sink, "tic-e1")
    add(sink, "tic-a1")
    add(sink, "tic-a2", depends_on=["tic-e1"])
    service = pkg_service(sink, arbite_dir)
    package = service.create(["tic-a1", "tic-a2"], agent="coord").stored.package
    view = service.view(service.get(package.id))
    assert view["external_prerequisites"] == [{
        "member": "tic-a2", "depends_on": "tic-e1", "status": "open", "met": False,
        "blocks": True}]
    acquire(sink, arbite_dir, "tic-a1", "worker.b")
    close(sink, arbite_dir, "tic-a1")
    view = service.view(service.get(package.id))
    assert view["waiting_on"][0]["depends_on"] == "tic-e1"
    assert view["next_action"].startswith("paused")
    with pytest.raises(TicketError):
        acquire(sink, arbite_dir, "tic-a2", "worker.b")
    ctl = make_lifecycle(sink, arbite_dir)
    assert ctl.active_attempt("tic-a2") is None
    with ctl.coordination.store.transaction(write=False) as tx:
        assert [cl for cl in tx.find("file_claim") if cl.is_active] == []
    assert stored_pkg(sink, arbite_dir, package.id).package.bound_worker == "worker.b"
    # The external work is not package work: anyone may do it.
    acquire(sink, arbite_dir, "tic-e1", "worker.z")
    close(sink, arbite_dir, "tic-e1")
    assert acquire(sink, arbite_dir, "tic-a2", "worker.b").ticket.status == "in_progress"


def test_blocked_member_does_not_advance(sink, arbite_dir):
    package = fresh(sink, arbite_dir, "tic-a1", "tic-a2")
    acquire(sink, arbite_dir, "tic-a1", "worker.b")
    block(sink, arbite_dir, "tic-a1")
    assert stored_pkg(sink, arbite_dir, package.id).package.current == "tic-a1"
    with pytest.raises(TicketPackaged) as excinfo:
        acquire(sink, arbite_dir, "tic-a2", "worker.b")
    assert excinfo.value.details["reason"] == "package_order"
    view = pkg_service(sink, arbite_dir).view(stored_pkg(sink, arbite_dir, package.id))
    assert "explicit resolution" in view["next_action"]


# --- explicit handoff ----------------------------------------------------------------


def test_partial_completion_then_rebind(sink, arbite_dir):
    package = fresh(sink, arbite_dir, "tic-a1", "tic-a2", "tic-a3")
    service = pkg_service(sink, arbite_dir)
    acquire(sink, arbite_dir, "tic-a1", "worker.b")
    close(sink, arbite_dir, "tic-a1")
    acquire(sink, arbite_dir, "tic-a2", "worker.b")

    with pytest.raises(PackageConflict) as excinfo:
        service.handoff(package.id, agent="worker.b", reason=None, to="worker.c")
    assert excinfo.value.details["reason"] == "reason_required"
    with pytest.raises(PackageConflict) as excinfo:
        service.handoff(package.id, agent="stranger", reason="x", to="worker.c")
    assert excinfo.value.details["reason"] == "not_controller"
    with pytest.raises(PackageConflict) as excinfo:
        service.handoff(package.id, agent="worker.b", reason="out of budget", to="worker.c")
    assert excinfo.value.details["reason"] == "active_attempt"
    assert sink.get("tic-a2").assignee == "worker.b"

    change = service.handoff(package.id, agent="coord", reason="out of budget", to="worker.c",
                             note="a2 half done: see branch notes", interrupt=True)
    rebound = change.stored.package
    assert rebound.state == "bound" and rebound.bound_worker == "worker.c"
    assert change.interrupted[0]["worker_id"] == "worker.b"
    entry = rebound.handoffs[-1]
    assert entry["action"] == "rebind" and entry["from_worker"] == "worker.b"
    assert entry["completed"] == ["tic-a1"] and entry["remaining"] == ["tic-a2", "tic-a3"]
    assert sink.get("tic-a1").status == "closed"  # completed members stay completed
    assert sink.get("tic-a2").status == "open" and sink.get("tic-a2").assignee is None
    with pytest.raises(TicketPackaged):
        acquire(sink, arbite_dir, "tic-a2", "worker.b")
    assert acquire(sink, arbite_dir, "tic-a2", "worker.c").attempt.worker_id == "worker.c"
    assert package_events(sink)[-2].kind_ == "package_rebound"


def test_rebind_clears_a_blocked_members_old_assignee(sink, arbite_dir):
    package = fresh(sink, arbite_dir, "tic-a1", "tic-a2")
    acquire(sink, arbite_dir, "tic-a1", "worker.b")
    block(sink, arbite_dir, "tic-a1")
    assert sink.get("tic-a1").assignee == "worker.b"
    change = pkg_service(sink, arbite_dir).handoff(package.id, agent="worker.b",
                                                   reason="cannot finish", to="worker.c")
    assert change.stored.package.handoffs[-1]["cleared_assignee"] == ["tic-a1"]
    blocked = sink.get("tic-a1")
    assert blocked.status == "blocked" and blocked.assignee is None


def test_release_returns_remaining_members(sink, arbite_dir):
    package = fresh(sink, arbite_dir, "tic-a1", "tic-a2", "tic-a3")
    acquire(sink, arbite_dir, "tic-a1", "worker.b")
    close(sink, arbite_dir, "tic-a1")
    change = pkg_service(sink, arbite_dir).handoff(package.id, agent="worker.b",
                                                   reason="reprioritised", release=True)
    released = change.stored.package
    assert released.state == "released" and released.ended_at
    assert packages.live_package_for(sink.coordination(), "tic-a2") is None
    # Ordinary availability again: anyone, any order.
    assert acquire(sink, arbite_dir, "tic-a3", "worker.z").attempt.worker_id == "worker.z"
    with pytest.raises(PackageConflict) as excinfo:
        pkg_service(sink, arbite_dir).handoff(package.id, agent="coord", reason="x",
                                              release=True)
    assert excinfo.value.details["reason"] == "not_live"


# --- package offers ----------------------------------------------------------------


def test_package_offer_binds_and_completes(sink, arbite_dir):
    package = fresh(sink, arbite_dir, "tic-a1", "tic-a2")
    svc = offer_service(sink, arbite_dir)
    with pytest.raises(OfferConflict) as excinfo:
        svc.publish("tic-a1", agent="coord")
    assert excinfo.value.details["reason"] == "package_member"
    offer = svc.publish(None, package=package.id, agent="coord", mode="assigned",
                        allowed_workers=["worker.b"]).stored.offer
    assert offer.target_kind == "package" and offer.tickets == ["tic-a1", "tic-a2"]
    with pytest.raises(WorkerIneligible):
        svc.claim(offer.id, worker_id="worker.c")
    with pytest.raises(WorkerIneligible):
        acquire(sink, arbite_dir, "tic-a1", "worker.c")
    result, accepted = svc.claim(offer.id, worker_id="worker.b")
    assert result.ticket.id == "tic-a1" and accepted.offer.state == "accepted"
    assert stored_pkg(sink, arbite_dir, package.id).package.bound_worker == "worker.b"
    with pytest.raises(OfferConflict) as excinfo:
        svc.withdraw(offer.id, agent="coord")
    assert excinfo.value.details["reason"] == "accepted"
    close(sink, arbite_dir, "tic-a1")
    assert svc.get(offer.id).offer.state == "accepted"  # member close does not complete it
    result, _ = svc.claim(offer.id, worker_id="worker.b")  # continues with the next member
    assert result.ticket.id == "tic-a2"
    close(sink, arbite_dir, "tic-a2")
    assert svc.get(offer.id).offer.state == "completed"
    assert stored_pkg(sink, arbite_dir, package.id).package.state == "completed"


def test_reserved_package_offer_and_bound_worker_beats_reservation(sink, arbite_dir):
    add(sink, "tic-a1")
    add(sink, "tic-a2")
    ctl = make_lifecycle(sink, arbite_dir)
    reservations.ReservationService(ctl).create("coord", tickets=["tic-a1", "tic-a2"])
    package = pkg_service(sink, arbite_dir).create(["tic-a1", "tic-a2"],
                                                   agent="coord").stored.package
    svc = offer_service(sink, arbite_dir)
    with pytest.raises(OfferConflict) as excinfo:
        svc.publish(None, package=package.id, agent="other")
    assert excinfo.value.details["reason"] == "not_owner"
    offer = svc.publish(None, package=package.id, agent="coord").stored.offer
    svc.claim(offer.id, worker_id="worker.b")
    close(sink, arbite_dir, "tic-a1")
    # The reservation owner may not steal the bound worker's next member...
    with pytest.raises(TicketPackaged):
        acquire(sink, arbite_dir, "tic-a2", "coord")
    # ...and the bound worker needs no reservation grant for it.
    assert acquire(sink, arbite_dir, "tic-a2", "worker.b").attempt.worker_id == "worker.b"


def test_rebind_respects_package_offer_requirements(sink, arbite_dir):
    package = fresh(sink, arbite_dir, "tic-a1", "tic-a2")
    workers.WorkerProfileService(sink.coordination()).register("worker.b", tier="high")
    svc = offer_service(sink, arbite_dir)
    offer = svc.publish(None, package=package.id, agent="coord",
                        requirements=offers.build_requirements(min_tier="high")).stored.offer
    svc.claim(offer.id, worker_id="worker.b")
    close(sink, arbite_dir, "tic-a1")
    service = pkg_service(sink, arbite_dir)
    with pytest.raises(WorkerIneligible):
        service.handoff(package.id, agent="coord", reason="swap", to="adhoc.worker")
    change = service.handoff(package.id, agent="coord", reason="swap", to="adhoc.worker",
                             force=True)
    assert change.ended_offers == [offer.id]
    assert svc.get(offer.id).offer.state == "cancelled"


def test_reserving_members_of_someone_elses_package_is_refused(sink, arbite_dir):
    fresh(sink, arbite_dir, "tic-a1", "tic-a2", agent="lead")
    ctl = make_lifecycle(sink, arbite_dir)
    with pytest.raises(ReservationConflict) as excinfo:
        reservations.ReservationService(ctl).create("coord", tickets=["tic-a2"])
    assert excinfo.value.details["conflicts"][0]["reason"] == "packaged_elsewhere"


def test_list_next_only_surfaces_the_current_member_to_the_bound_worker(cli):
    make = lambda title: created_id(cli(  # noqa: E731
        "create", "--title", title, "--type", "bug", "--tier", "low", "--domain", "py",
    ))
    t1, t2 = make("one"), make("two")
    pkg = json.loads(cli("package", "create", t1, t2, "--agent", "coord",
                         "--json").stdout)["data"]["package"]["id"]
    listed = json.loads(cli("list", "next", "--count", "5", "--json").stdout)
    assert [t["id"] for t in listed] == [t1]
    picked = json.loads(cli("list", "next", "--claim", "worker.b", "--count", "5",
                            "--json").stdout)
    assert [t["id"] for t in picked] == [t1]
    cli("close", t1, "--agent", "worker.b")
    cli("list", "next", "--claim", "worker.c", expect=2)
    assert "continuity packages" in cli("list", "next", expect=2).stderr
    picked = json.loads(cli("list", "next", "--claim", "worker.b", "--json").stdout)
    assert [t["id"] for t in picked] == [t2]
    shown = json.loads(cli("package", "show", pkg, "--json").stdout)["data"]["package"]
    assert shown["state"] == "bound" and shown["progress"]["current"] == t2


# --- export -------------------------------------------------------------------------


def test_packages_travel_in_export_bundles(sink, kind, arbite_dir, tmp_path):
    package = fresh(sink, arbite_dir, "tic-a1", "tic-a2")
    acquire(sink, arbite_dir, "tic-a1", "worker.b")
    bundle = x.export_coordination(sink)
    assert [p["id"] for p in bundle["records"]["packages"]] == [package.id]
    assert x.bundle_problems(bundle) == []

    other = "sqlite" if kind == "file" else "file"
    target_dir = tmp_path / "target" / ".arbite"
    target_dir.mkdir(parents=True)
    target = make_sink(other, target_dir)
    x.import_coordination(target, bundle)
    moved = packages.live_package_for(target.coordination(), "tic-a2")
    assert moved.package.id == package.id and moved.package.bound_worker == "worker.b"

    legacy = json.loads(json.dumps(bundle))
    del legacy["records"]["packages"]
    legacy["events"] = [e for e in legacy["events"] if e["category"] != "package"]
    assert x.bundle_problems(legacy) == []


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


def test_cli_package_flow(cli):
    make = lambda title: created_id(cli(  # noqa: E731
        "create", "--title", title, "--type", "bug", "--tier", "low", "--domain", "py",
    ))
    t1, t2, t3 = make("one"), make("two"), make("three")
    cli("package", "list", expect=2)
    created = json.loads(cli("package", "create", t1, t2, t3, "--agent", "coord",
                             "--json").stdout)["data"]["package"]
    pkg = created["id"]
    assert created["state"] == "open" and created["progress"]["current"] == t1
    dup = json.loads(cli("package", "create", t3, t1, "--agent", "coord", "--json",
                         expect=1).stdout)
    assert dup["code"] == "package_conflict"

    offer = json.loads(cli("offer", "assign", "--package", pkg, "--worker", "worker.b",
                           "--agent", "coord", "--json").stdout)["data"]["offer"]
    assert offer["target_kind"] == "package" and offer["package_id"] == pkg
    refused = cli("claim", t2, "--agent", "worker.b", expect=1)
    assert "worked in order" in refused.stderr
    cli("claim", t2, "--agent", "worker.b", "--force", "--reason", "skip", expect=1)
    cli("set", t2, "status", "in_progress", expect=1)

    claimed = json.loads(cli("offer", "claim", offer["id"], "--agent", "worker.b",
                             "--json").stdout)["data"]
    assert claimed["ticket_id"] == t1
    cli("close", t1, "--agent", "worker.b")
    stolen = json.loads(cli("package", "claim", pkg, "--agent", "worker.c", "--json",
                            expect=1).stdout)
    assert stolen["code"] == "ticket_packaged"
    assert stolen["details"]["reason"] == "bound_elsewhere"
    cli("package", "note", pkg, "t1 done; t2 next", "--agent", "worker.b")
    nxt = json.loads(cli("package", "claim", pkg, "--agent", "worker.b", "--json").stdout)["data"]
    assert nxt["ticket_id"] == t2

    busy = json.loads(cli("package", "handoff", pkg, "--agent", "coord", "--reason", "swap",
                          "--to", "worker.c", "--json", expect=1).stdout)
    assert busy["code"] == "package_conflict" and busy["details"]["reason"] == "active_attempt"
    handed = json.loads(cli("package", "handoff", pkg, "--agent", "coord", "--reason", "swap",
                            "--to", "worker.c", "--interrupt", "--json").stdout)["data"]
    assert handed["package"]["bound_worker"] == "worker.c"
    assert handed["ended_offers"] == [offer["id"]]
    human = cli("package", "show", pkg).stdout
    assert "rebind worker.b -> worker.c" in human and "t1 done" in human
    cli("package", "claim", pkg, "--agent", "worker.c")
    cli("close", t2, "--agent", "worker.c")
    released = json.loads(cli("package", "handoff", pkg, "--agent", "worker.c", "--reason",
                              "t3 dropped", "--release", "--json").stdout)["data"]["package"]
    assert released["state"] == "released"
    assert json.loads(cli("package", "list", "--state", "all", "--json").stdout)["data"]["count"] == 1
    cli("claim", t3, "--agent", "worker.z")
    counts = json.loads(cli("export", "--json").stdout)
    assert "packages" in json.dumps(counts)
