"""Coordinator reservations over explicit ticket sets (planning key B02).

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
from arbite import lifecycle, reservations, workers
from arbite.application import Actor
from arbite.errors import (
    ArbiteError,
    CoordinationConflict,
    ReservationConflict,
    TicketError,
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


def service(sink, arbite_dir, actor="tester"):
    return reservations.ReservationService(make_lifecycle(sink, arbite_dir), actor=actor)


def add(sink, ticket_id, **overrides):
    overrides.setdefault("status", "open")
    sink.create(make_ticket(ticket_id, **overrides))
    return sink.get(ticket_id)


def reservation_events(sink):
    return [e for e in sink.coordination().event_log() if e.category == "reservation"]


def all_attempts(ctl, *ids):
    return [a for t in ids for a in ctl.attempts_for(t)]


def make_reservation(**overrides) -> c.Reservation:
    now = c.utc_now()
    data = dict(id=c.new_record_id("reservation"), owner="coord", members=["tic-a1"],
                created=now, updated=now)
    data.update(overrides)
    return c.Reservation(**data)


# --- the record --------------------------------------------------------------


def test_reservation_record_round_trips_and_validates():
    record = make_reservation(members=["tic-a1", "tic-a2"],
                              source={"kind": "epic", "epic": "ep", "excluded": []})
    assert record.validate() == []
    assert c.record_from_dict(json.loads(json.dumps(record.to_dict()))) == record
    assert c.is_opaque_id(record.id, "rsv")


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"owner": "has space"}, "owner"),
        ({"members": []}, "at least one member"),
        ({"members": ["tic-a1", "tic-a1"]}, "repeat"),
        ({"state": "paused"}, "state"),
        ({"state": "released"}, "released"),
        ({"released": "2026-01-01T00:00:00Z"}, "must not have 'released'"),
        ({"source": {"kind": "epic"}}, "epic"),
    ],
)
def test_reservation_validation_reports_bad_fields(overrides, fragment):
    problems = make_reservation(**overrides).validate()
    assert any(fragment in problem for problem in problems), problems


def test_overlapping_active_reservations_are_a_collection_problem():
    first = make_reservation(members=["tic-a1", "tic-a2"])
    second = make_reservation(members=["tic-a2"])
    assert any("two active reservations" in p for p in c.validate_collection([first, second]))
    now = c.utc_now()
    second.state, second.released = "released", now
    assert c.validate_collection([first, second]) == []


# --- create ------------------------------------------------------------------


def test_create_persists_without_starting_work(sink, kind, arbite_dir):
    for tid in ("tic-a1", "tic-a2"):
        add(sink, tid)
    svc = service(sink, arbite_dir)
    change = svc.create("coord", tickets=["tic-a1", "tic-a2"], note="sprint")
    rid = change.stored.reservation.id
    assert change.stored.revision == 1 and change.added == ["tic-a1", "tic-a2"]

    # Reservation alone creates no attempt and leaves tickets untouched.
    ctl = make_lifecycle(sink, arbite_dir)
    assert all_attempts(ctl, "tic-a1", "tic-a2") == []
    assert [sink.get(t).status for t in ("tic-a1", "tic-a2")] == ["open", "open"]
    assert [sink.get(t).assignee for t in ("tic-a1", "tic-a2")] == [None, None]

    reopened = make_sink(kind, arbite_dir, initialise=False)
    stored = service(reopened, arbite_dir).get(rid)
    assert stored.reservation.members == ["tic-a1", "tic-a2"]
    assert stored.reservation.note == "sprint" and stored.reservation.state == "active"
    [event] = reservation_events(reopened)
    assert event.event_kind == "reservation_created"
    assert event.subject_ids == [rid, "tic-a1", "tic-a2"]
    assert event.payload["owner"] == "coord"


@pytest.mark.parametrize("problem", ["closed", "active_attempt_elsewhere", "assigned_elsewhere",
                                     "not_found", "already_reserved"])
def test_create_is_all_or_nothing(sink, arbite_dir, problem):
    add(sink, "tic-ok")
    svc = service(sink, arbite_dir)
    ctl = make_lifecycle(sink, arbite_dir)
    bad = "tic-bad"
    if problem == "closed":
        add(sink, bad, status="closed", closed="2026-09-01")
    elif problem == "active_attempt_elsewhere":
        add(sink, bad)
        ctl.acquire(sink.get(bad), worker_id="worker.b")
    elif problem == "assigned_elsewhere":
        add(sink, bad, assignee="someone")
    elif problem == "already_reserved":
        add(sink, bad)
        svc.create("other.coord", tickets=[bad])
    before = len(reservation_events(sink))

    with pytest.raises(ReservationConflict) as excinfo:
        svc.create("coord", tickets=["tic-ok", bad])
    details = excinfo.value.details
    assert excinfo.value.error_code == "reservation_conflict"
    assert details["reason"] == "members_unavailable"
    assert [(e["ticket_id"], e["reason"]) for e in details["conflicts"]] == [(bad, problem)]
    # Nothing leaked: tic-ok is not reserved and no event was written.
    assert reservations.reservation_for(sink.coordination(), "tic-ok") is None
    assert len(reservation_events(sink)) == before


def test_owner_may_reserve_its_own_running_work_without_transfer(sink, arbite_dir):
    add(sink, "tic-a1")
    ctl = make_lifecycle(sink, arbite_dir)
    running = ctl.acquire(sink.get("tic-a1"), worker_id="coord").attempt
    service(sink, arbite_dir).create("coord", tickets=["tic-a1"])
    assert ctl.active_attempt("tic-a1").id == running.id


def test_nested_and_overlapping_reservations_are_refused(sink, arbite_dir):
    for tid in ("tic-a1", "tic-a2", "tic-a3"):
        add(sink, tid)
    svc = service(sink, arbite_dir)
    outer = svc.create("coord", tickets=["tic-a1", "tic-a2"]).stored.reservation
    with pytest.raises(ReservationConflict) as excinfo:  # a nested subset, same owner
        svc.create("coord", tickets=["tic-a2"])
    [conflict] = excinfo.value.details["conflicts"]
    assert conflict["reservation_id"] == outer.id and "nest" in conflict["detail"]
    with pytest.raises(ReservationConflict):  # an overlap by another coordinator
        svc.create("other", tickets=["tic-a3", "tic-a1"])
    assert reservations.reservation_for(sink.coordination(), "tic-a3") is None


def test_epic_is_a_snapshot(sink, arbite_dir):
    add(sink, "tic-e1", epic="ep")
    add(sink, "tic-e2", epic="ep", status="closed", closed="2026-09-01")
    add(sink, "tic-e3", epic="ep", status="blocked")
    add(sink, "tic-x1", epic="other")
    svc = service(sink, arbite_dir)
    change = svc.create("coord", epic="ep")
    reservation = change.stored.reservation
    assert sorted(reservation.members) == ["tic-e1", "tic-e3"]
    assert change.excluded == [{"ticket_id": "tic-e2", "reason": "closed"}]
    assert reservation.source["kind"] == "epic" and reservation.source["excluded"] == ["tic-e2"]

    add(sink, "tic-e4", epic="ep")  # new epic ticket: not silently included
    assert "tic-e4" not in svc.get(reservation.id).reservation.members
    ctl = make_lifecycle(sink, arbite_dir)
    assert ctl.acquire(sink.get("tic-e4"), worker_id="anyone").attempt.worker_id == "anyone"

    add(sink, "tic-e5", epic="ep")
    with pytest.raises(ReservationConflict) as excinfo:  # re-resolving is all-or-nothing too
        svc.add(reservation.id, agent="coord", epic="ep")
    assert [e["ticket_id"] for e in excinfo.value.details["conflicts"]] == ["tic-e4"]
    added = svc.add(reservation.id, agent="coord", tickets=["tic-e5"])
    assert added.added == ["tic-e5"] and added.stored.revision == 2
    with pytest.raises(ReservationConflict):
        svc.create("coord", epic="nothing-here")


def test_disabled_owner_cannot_reserve(sink, arbite_dir):
    add(sink, "tic-a1")
    profiles = workers.WorkerProfileService(sink.coordination())
    profiles.register("coord", tier="high")
    profiles.disable("coord", reason="off")
    with pytest.raises(WorkerIneligible):
        service(sink, arbite_dir).create("coord", tickets=["tic-a1"])


# --- enforcement ---------------------------------------------------------------


def test_only_the_owner_may_acquire_a_member(sink, arbite_dir):
    add(sink, "tic-a1")
    add(sink, "tic-a2")
    svc = service(sink, arbite_dir)
    rid = svc.create("coord", tickets=["tic-a1", "tic-a2"]).stored.reservation.id
    ctl = make_lifecycle(sink, arbite_dir)

    with pytest.raises(TicketReserved) as excinfo:
        ctl.acquire(sink.get("tic-a1"), worker_id="worker.b")
    assert excinfo.value.details == {
        "ticket_id": "tic-a1", "worker_id": "worker.b", "reservation_id": rid, "owner": "coord",
    }
    assert ctl.attempts_for("tic-a1") == []

    owned = ctl.acquire(sink.get("tic-a1"), worker_id="coord")
    assert owned.attempt.worker_id == "coord"
    # --force is not a way around the reservation for a non-owner...
    with pytest.raises(TicketReserved):
        ctl.acquire(sink.get("tic-a1"), worker_id="worker.b", takeover=True, reason="mine now")
    assert ctl.active_attempt("tic-a1").id == owned.attempt.id


def test_adopt_respects_reservations(sink, arbite_dir):
    add(sink, "tic-l1", status="in_progress", assignee="coord")
    service(sink, arbite_dir).create("coord", tickets=["tic-l1"])
    ctl = make_lifecycle(sink, arbite_dir)
    with pytest.raises(TicketReserved):
        ctl.acquire(sink.get("tic-l1"), worker_id="worker.b", adopt=True)
    assert ctl.acquire(sink.get("tic-l1"), worker_id="coord", adopt=True).attempt


def test_set_and_delete_respect_reservations(sink, arbite_dir):
    add(sink, "tic-a1")
    service(sink, arbite_dir).create("coord", tickets=["tic-a1"])
    ctl = make_lifecycle(sink, arbite_dir)
    ticket = sink.get("tic-a1")
    with pytest.raises(TicketReserved):
        ctl.refuse_reserved_edit(ticket, new_status="in_progress", new_assignee=None)
    with pytest.raises(TicketReserved):
        ctl.refuse_reserved_edit(ticket, new_status=None, new_assignee="worker.b")
    ctl.refuse_reserved_edit(ticket, new_status="blocked", new_assignee="coord")  # allowed
    with pytest.raises(TicketError, match="reservation"):
        ctl.require_deletable(ticket)


# --- serial reserve-vs-claim outcomes -------------------------------------------


def test_claim_first_then_reserve_is_refused(sink, arbite_dir):
    add(sink, "tic-a1")
    ctl = make_lifecycle(sink, arbite_dir)
    ctl.acquire(sink.get("tic-a1"), worker_id="worker.b")
    with pytest.raises(ReservationConflict) as excinfo:
        service(sink, arbite_dir).create("coord", tickets=["tic-a1"])
    assert excinfo.value.details["conflicts"][0]["reason"] == "active_attempt_elsewhere"


def test_reserve_first_then_claim_is_refused(sink, arbite_dir):
    add(sink, "tic-a1")
    service(sink, arbite_dir).create("coord", tickets=["tic-a1"])
    with pytest.raises(TicketReserved):
        make_lifecycle(sink, arbite_dir).acquire(sink.get("tic-a1"), worker_id="worker.b")


# --- membership and release -------------------------------------------------------


def test_membership_changes_are_owner_only_atomic_and_revisioned(sink, arbite_dir):
    for tid in ("tic-a1", "tic-a2", "tic-a3"):
        add(sink, tid)
    add(sink, "tic-cl", status="closed", closed="2026-09-01")
    svc = service(sink, arbite_dir)
    rid = svc.create("coord", tickets=["tic-a1"]).stored.reservation.id

    with pytest.raises(ReservationConflict) as excinfo:
        svc.add(rid, agent="intruder", tickets=["tic-a2"])
    assert excinfo.value.details["reason"] == "not_owner"
    with pytest.raises(ReservationConflict):  # force without a reason
        svc.add(rid, agent="intruder", tickets=["tic-a2"], force=True)
    with pytest.raises(ReservationConflict):  # one bad member refuses the batch
        svc.add(rid, agent="coord", tickets=["tic-a2", "tic-cl"])
    assert svc.get(rid).reservation.members == ["tic-a1"]
    with pytest.raises(CoordinationConflict):
        svc.add(rid, agent="coord", tickets=["tic-a2"], expect_revision=7)

    change = svc.add(rid, agent="admin", tickets=["tic-a2", "tic-a3", "tic-a1"], force=True,
                     reason="rebalancing", expect_revision=1)
    assert change.added == ["tic-a2", "tic-a3"] and change.stored.revision == 2
    assert change.event.payload["forced"] is True
    assert svc.add(rid, agent="coord", tickets=["tic-a2"]).event is None  # no-op

    with pytest.raises(ReservationConflict) as excinfo:
        svc.remove(rid, agent="coord", tickets=["tic-cl"])
    assert excinfo.value.details["reason"] == "not_a_member"
    with pytest.raises(ReservationConflict) as excinfo:
        svc.remove(rid, agent="coord", tickets=["tic-a1", "tic-a2", "tic-a3"])
    assert excinfo.value.details["reason"] == "would_empty"

    removed = svc.remove(rid, agent="coord", tickets=["tic-a3"])
    assert removed.removed == ["tic-a3"] and removed.stored.revision == 3
    ctl = make_lifecycle(sink, arbite_dir)
    assert ctl.acquire(sink.get("tic-a3"), worker_id="worker.b").attempt  # free again
    assert [e.event_kind for e in reservation_events(sink)] == [
        "reservation_created", "reservation_members_added", "reservation_members_removed",
    ]


def test_release_refuses_active_work_unless_interrupted(sink, kind, arbite_dir):
    add(sink, "tic-a1")
    add(sink, "tic-a2")
    svc = service(sink, arbite_dir)
    rid = svc.create("coord", tickets=["tic-a1", "tic-a2"]).stored.reservation.id
    ctl = make_lifecycle(sink, arbite_dir)
    running = ctl.acquire(sink.get("tic-a1"), worker_id="coord").attempt

    with pytest.raises(ReservationConflict) as excinfo:
        svc.release(rid, agent="coord")
    assert excinfo.value.details["reason"] == "active_attempts"
    assert excinfo.value.details["active"][0]["attempt_id"] == running.id
    with pytest.raises(ReservationConflict) as excinfo:
        svc.remove(rid, agent="coord", tickets=["tic-a1"])
    assert excinfo.value.details["reason"] == "active_attempts"
    with pytest.raises(ReservationConflict) as excinfo:
        svc.release(rid, agent="coord", interrupt=True)
    assert excinfo.value.details["reason"] == "reason_required"
    assert svc.get(rid).reservation.state == "active"
    assert ctl.active_attempt("tic-a1").id == running.id

    change = svc.release(rid, agent="coord", interrupt=True, reason="replanning")
    assert change.interrupted == [
        {"ticket_id": "tic-a1", "attempt_id": running.id, "worker_id": "coord"}
    ]
    reopened = make_sink(kind, arbite_dir, initialise=False)
    stored = service(reopened, arbite_dir).get(rid)
    assert stored.reservation.state == "released" and stored.revision == 2
    assert stored.reservation.release_reason == "replanning"
    [attempt] = ctl.attempts_for("tic-a1")
    assert attempt.state == "interrupted"
    assert reopened.get("tic-a1").status == "open" and reopened.get("tic-a1").assignee is None

    # Released: members are back in ad-hoc availability; the record is kept.
    assert ctl.acquire(reopened.get("tic-a2"), worker_id="worker.b").attempt
    with pytest.raises(ReservationConflict) as excinfo:
        svc.release(rid, agent="coord")
    assert excinfo.value.details["reason"] == "not_active"
    assert [row.reservation.id for row in svc.list(state="released")] == [rid]
    assert svc.list() == []


def test_quiescent_release_returns_members_to_the_pool(sink, arbite_dir):
    add(sink, "tic-a1")
    svc = service(sink, arbite_dir)
    rid = svc.create("coord", tickets=["tic-a1"]).stored.reservation.id
    with pytest.raises(ReservationConflict):
        svc.release(rid, agent="worker.b")
    change = svc.release(rid, agent="coord", reason="done")
    assert change.interrupted == [] and change.event.event_kind == "reservation_released"
    ctl = make_lifecycle(sink, arbite_dir)
    assert ctl.acquire(sink.get("tic-a1"), worker_id="worker.b").attempt


# --- export ---------------------------------------------------------------------


def test_reservations_travel_in_export_bundles(sink, kind, arbite_dir, tmp_path):
    add(sink, "tic-a1")
    rid = service(sink, arbite_dir).create("coord", tickets=["tic-a1"]).stored.reservation.id
    bundle = x.export_coordination(sink)
    assert [r["id"] for r in bundle["records"]["reservations"]] == [rid]
    assert bundle["counts"]["reservations"] == 1
    assert x.bundle_problems(bundle) == []

    other = "sqlite" if kind == "file" else "file"
    target_dir = tmp_path / "target" / ".arbite"
    target_dir.mkdir(parents=True)
    target = make_sink(other, target_dir)
    x.import_coordination(target, bundle)
    assert reservations.reservation_for(target.coordination(), "tic-a1").id == rid

    legacy = json.loads(json.dumps(bundle))
    del legacy["records"]["reservations"]
    legacy["events"] = [e for e in legacy["events"] if e["category"] != "reservation"]
    assert x.bundle_problems(legacy) == []


# --- multiprocess races -------------------------------------------------------------


def _racer(kind, arbite_dir, action, tickets, barrier, results):
    sink = build_sink(SinkSpec(kind=kind), Path(arbite_dir))
    svc = application.coordination_service_for(
        sink, root=str(Path(arbite_dir).parent), actor=Actor(action[1])
    )
    ctl = lifecycle.TicketLifecycle(svc, sink)
    barrier.wait(30)
    try:
        if action[0] == "reserve":
            reservations.ReservationService(ctl).create(action[1], tickets=tickets)
        else:
            ctl.acquire(sink.get(tickets[0]), worker_id=action[1])
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
                        args=(kind, str(arbite_dir), action, tickets, barrier, results))
        for action, tickets in plans
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(90)
    assert [p.exitcode for p in processes] == [0] * len(plans)
    return dict(results.get(timeout=10) for _ in plans)


def test_processes_reserving_overlapping_sets_have_one_winner(sink, kind, arbite_dir):
    for tid in ("tic-a1", "tic-a2", "tic-a3"):
        add(sink, tid)
    make_lifecycle(sink, arbite_dir)  # bind the workspace before the race
    outcome = _race(kind, arbite_dir, [
        (("reserve", "coord.1"), ["tic-a1", "tic-a2"]),
        (("reserve", "coord.2"), ["tic-a2", "tic-a3"]),
    ])
    assert sorted(outcome.values()) == ["ok", "reservation_conflict"]
    winner = [who for who, result in outcome.items() if result == "ok"][0]
    held = reservations.active_reservations(sink.coordination())
    expected = ["tic-a1", "tic-a2"] if winner == "coord.1" else ["tic-a2", "tic-a3"]
    assert sorted(held) == expected  # the loser's free ticket did not leak
    assert {r.owner for r in held.values()} == {winner}


def test_processes_racing_reserve_against_claim_have_one_serial_outcome(sink, kind, arbite_dir):
    add(sink, "tic-a1")
    add(sink, "tic-a2")
    ctl = make_lifecycle(sink, arbite_dir)
    outcome = _race(kind, arbite_dir, [
        (("reserve", "coord"), ["tic-a1", "tic-a2"]),
        (("claim", "worker.b"), ["tic-a1"]),
    ])
    attempts = ctl.attempts_for("tic-a1")
    held = reservations.active_reservations(sink.coordination())
    if outcome["coord"] == "ok":
        assert outcome["worker.b"] == "ticket_reserved"
        assert attempts == [] and sorted(held) == ["tic-a1", "tic-a2"]
    else:
        assert outcome == {"coord": "reservation_conflict", "worker.b": "ok"}
        assert [a.worker_id for a in attempts] == ["worker.b"] and held == {}


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


def test_cli_reservation_flow(cli):
    make = lambda title: created_id(cli(  # noqa: E731
        "create", "--title", title, "--type", "bug", "--tier", "low", "--domain", "py",
        "--epic", "ep",
    ))
    t1, t2, t3 = make("one"), make("two"), make("three")
    cli("reserve", "list", expect=2)

    created = json.loads(cli("reserve", "create", "--epic", "ep", "--agent", "coord",
                             "--json").stdout)
    assert created["ok"]
    view = created["data"]["reservation"]
    rid = view["id"]
    assert sorted(view["members"]) == sorted([t1, t2, t3]) and view["revision"] == 1
    assert {m["status"] for m in view["member_status"]} == {"open"}

    refused = cli("claim", t1, "--agent", "worker.b", expect=1)
    assert "reserved by 'coord'" in refused.stderr
    cli("set", t1, "assignee", "worker.b", expect=1)
    plain = cli("list", "next", expect=2)
    assert "held by reservations" in plain.stderr
    picked = json.loads(cli("list", "next", "--claim", "coord", "--json").stdout)
    assert len(picked) == 1 and picked[0]["assignee"] == "coord"

    busy = json.loads(cli("reserve", "release", rid, "--agent", "coord", "--json",
                          expect=1).stdout)
    assert busy["code"] == "reservation_conflict"
    assert busy["details"]["reason"] == "active_attempts"

    busy_id = picked[0]["id"]
    idle = [t for t in (t1, t2, t3) if t != busy_id]
    removed = json.loads(cli("reserve", "remove", rid, idle[0], "--agent", "coord",
                             "--json").stdout)["data"]
    assert removed["removed"] == [idle[0]] and removed["reservation"]["revision"] == 2
    cli("claim", idle[0], "--agent", "worker.b")

    listed = json.loads(cli("reserve", "list", "--ticket", busy_id, "--json").stdout)["data"]
    assert [r["id"] for r in listed["reservations"]] == [rid]

    released = json.loads(cli(
        "reserve", "release", rid, "--agent", "coord", "--interrupt", "--reason", "replan",
        "--json",
    ).stdout)["data"]
    assert released["reservation"]["state"] == "released"
    assert [i["ticket_id"] for i in released["interrupted"]] == [busy_id]
    cli("claim", busy_id, "--agent", "worker.b")
    human = cli("reserve", "show", rid).stdout
    assert "released" in human and "does not start work" in human
    missing = json.loads(cli("reserve", "show", "rsv-0000000000000000", "--json",
                             expect=1).stdout)
    assert missing["code"] == "not_found"
    counts = json.loads(cli("export", "--json").stdout)
    assert "reservations" in json.dumps(counts)
