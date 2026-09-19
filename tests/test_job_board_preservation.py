"""B07: job-board records survive export, migration and the doctor.

The slice's contract, executed against both shipped sinks:

- a quiescent transfer round-trips worker profiles, reservations, offers and
  packages completely, preserving disabled-profile history, offer state and
  revisions, reservation ownership, and a partly completed package's order,
  continuity binding and completed-member history;
- an export bundle verifies its own job-board references and overlaps, and the
  migration fingerprint covers the job-board groups, so a lossy transfer is
  refused instead of passing as "history preserved";
- `migrate`/`rebind` refuse a store that still has job-board work in flight, name
  the offending records, and continue only on an explicit `--force --reason`
  override that is reported rather than silent;
- `arbite doctor` reports dangling references, overlapping membership,
  inconsistent attempts, an invalid continuity binding, impossible capacity
  arithmetic and an unreadable namespace registry -- and repairs none of them;
- a legacy store with no job-board records exports, imports and doctors clean.

Damage is injected with direct storage writes, never through the public API.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from arbite import application, coordination as c, coordination_doctor as doc
from arbite import coordination_export as x
from arbite import events as ev
from arbite.application import Actor
from arbite.errors import CoordinationConflict, ForeignCursor, InvalidRecord
from arbite.query import TicketQuery
from arbite.sinks import SinkSpec, build_sink
from helpers import make_ticket

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
SINKS = ("file", "sqlite")

WORKER = "claude.opus-5.001"
DISABLED = "claude.haiku.009"
COORDINATOR = "coord.1"


def other_kind(kind: str) -> str:
    return "sqlite" if kind == "file" else "file"


def make_sink(kind: str, arbite_dir: Path, initialise: bool = True):
    sink = build_sink(SinkSpec(kind=kind), arbite_dir)
    if initialise:
        sink.init()
    return sink


def other_sink(kind: str, tmp_path: Path, name: str = "target"):
    directory = tmp_path / name / ".arbite"
    directory.mkdir(parents=True, exist_ok=True)
    return make_sink(other_kind(kind), directory)


def project_sink(project_root: Path, kind: str):
    return make_sink(kind, project_root / ".arbite")


def put(store, *records):
    with store.transaction() as tx:
        for record in records:
            tx.put(record)


# ---------------------------------------------------------------------------
# Record factories
# ---------------------------------------------------------------------------


def make_profile(worker_id, **overrides):
    now = c.utc_now()
    data = dict(
        id=c.new_record_id("worker_profile"),
        worker_id=worker_id,
        tier="medium",
        created=now,
        updated=now,
    )
    data.update(overrides)
    return c.WorkerProfile(**data)


def make_attempt(ticket_id, worker_id, workspace_id, *, state="active", generation=1,
                 moment=None):
    moment = moment or c.utc_now()
    return c.WorkAttempt(
        id=c.new_record_id("work_attempt"),
        ticket_id=ticket_id,
        worker_id=worker_id,
        workspace_id=workspace_id,
        generation=generation,
        started=moment,
        last_activity=moment,
        state=state,
        ended=None if state == "active" else moment,
        outcome=None if state == "active" else "ok",
    )


def make_offer(tickets, **overrides):
    now = c.utc_now()
    data = dict(
        id=c.new_record_id("offer"),
        tickets=list(tickets),
        mode="public",
        publisher=COORDINATOR,
        created=now,
        updated=now,
    )
    data.update(overrides)
    return c.Offer(**data)


def make_reservation(members, **overrides):
    now = c.utc_now()
    data = dict(
        id=c.new_record_id("reservation"),
        owner=COORDINATOR,
        members=list(members),
        created=now,
        updated=now,
    )
    data.update(overrides)
    return c.Reservation(**data)


def make_package(tickets, **overrides):
    now = c.utc_now()
    data = dict(
        id=c.new_record_id("package"),
        tickets=list(tickets),
        created_by=COORDINATOR,
        created=now,
        updated=now,
    )
    data.update(overrides)
    return c.Package(**data)


def make_event(kind, category, subjects, payload, workspace_id):
    return c.Event(
        id=c.new_record_id("event"),
        cursor=None,
        kind_=kind,
        category=category,
        timestamp=c.utc_now(),
        subject_ids=list(subjects),
        payload={"workspace_id": workspace_id, **payload},
    )


# ---------------------------------------------------------------------------
# The board fixture used by every test
# ---------------------------------------------------------------------------


def board(sink, arbite_dir, *, live=True, active_attempt=True):
    """Build a job board with real history in `sink`; return its pieces.

    ``live=True`` keeps an active reservation, a published offer and a bound
    package ("pending work"); ``active_attempt`` keeps a live work attempt. The
    three useful variants are therefore:

    - ``board(...)``                               -- busy: transfers are refused
    - ``board(..., active_attempt=False)``         -- pending job-board work only
    - ``board(..., live=False, active_attempt=False)`` -- quiescent, transferable
    """
    service = application.coordination_service_for(
        sink, root=str(arbite_dir.parent), actor=Actor("tester")
    )
    workspace = service.workspace
    now = c.utc_now()

    for ticket_id, status, assignee in (
        ("tic-a100", "closed", None),
        ("tic-a200", "in_progress", WORKER),
        ("tic-a300", "open", None),
        ("tic-a400", "open", None),
        ("tic-a500", "open", None),
    ):
        overrides = {"assignee": assignee}
        if status == "closed":
            overrides["closed"] = "2026-09-01T00:00:00"
        sink.create(make_ticket(ticket_id, status=status, **overrides))

    profiles = {
        "enabled": make_profile(
            WORKER,
            provider="anthropic",
            model="claude-opus-5",
            capabilities=["python", "shell"],
            locality="local",
            cost_class="paid",
            cost_estimate={"amount": 2.5, "unit": "USD/ticket", "provenance": "operator"},
            capacity=2,
            last_checkin=now,
        ),
        "disabled": make_profile(
            DISABLED,
            provider="anthropic",
            model="claude-haiku-4",
            enabled=False,
            disabled_at=now,
            disabled_reason="retired operator account",
        ),
    }

    attempts = {
        "disabled_history": make_attempt(
            "tic-a100", DISABLED, workspace.id, state="finished", generation=1
        ),
        "completed_member": make_attempt(
            "tic-a100", WORKER, workspace.id, state="finished", generation=2
        ),
        "current_member": make_attempt(
            "tic-a200",
            WORKER,
            workspace.id,
            state="active" if active_attempt else "finished",
            generation=1,
        ),
    }

    reservation = make_reservation(["tic-a300", "tic-a400"])
    released = make_reservation(
        ["tic-a500"],
        owner="coord.2",
        state="released",
        released=now,
        released_by="coord.2",
        release_reason="returned to ad-hoc availability",
    )
    if not live:
        reservation.state = "released"
        reservation.released = now
        reservation.released_by = COORDINATOR
        reservation.release_reason = "closed out before the transfer"

    package = make_package(
        ["tic-a100", "tic-a200"],
        state="bound" if live else "released",
        bound_worker=WORKER,
        bound_at=now,
        current="tic-a200",
        handoffs=[{"kind": "rebind", "to": WORKER, "reason": "continuity", "at": now}],
        notes=[{"actor": WORKER, "note": "first member completed", "at": now}],
        ended_at=None if live else now,
        ended_by=None if live else WORKER,
        outcome=None if live else "handoff",
    )
    assigned_offer = make_offer(
        ["tic-a300"],
        mode="assigned",
        allowed_workers=[WORKER],
        reservation_id=reservation.id,
        requirements={"min_tier": "medium", "local_only": False},
        preferences={"prefer_local": True, "prefer_workers": [WORKER]},
        state="published" if live else "withdrawn",
        ended_at=None if live else now,
        ended_by=None if live else COORDINATOR,
        end_reason=None if live else "no longer needed",
    )
    package_offer = make_offer(
        ["tic-a100", "tic-a200"],
        mode="assigned",
        allowed_workers=[WORKER],
        target_kind="package",
        package_id=package.id,
        state="accepted" if live else "completed",
        accepted_by=WORKER,
        accepted_at=now,
        attempt_id=attempts["completed_member"].id,
        ended_at=None if live else now,
        ended_by=None if live else WORKER,
        end_reason=None if live else "package released",
    )
    withdrawn_offer = make_offer(
        ["tic-a500"],
        publisher="coord.2",
        state="withdrawn",
        ended_at=now,
        ended_by="coord.2",
        end_reason="no takers",
    )

    records = [
        profiles["enabled"],
        profiles["disabled"],
        *attempts.values(),
        reservation,
        released,
        package,
        assigned_offer,
        package_offer,
        withdrawn_offer,
    ]
    events = [
        make_event("attempt_finished", "lifecycle",
                   [attempts["disabled_history"].id, "tic-a100"],
                   {"worker_id": DISABLED}, workspace.id),
        make_event("attempt_started", "lifecycle",
                   [attempts["current_member"].id, "tic-a200"],
                   {"worker_id": WORKER}, workspace.id),
        make_event("worker_disabled", "worker", [profiles["disabled"].id],
                   {"worker_id": DISABLED, "reason": "retired operator account"},
                   workspace.id),
        make_event("worker_registered", "worker", [profiles["enabled"].id],
                   {"worker_id": WORKER}, workspace.id),
        make_event("reservation_created", "reservation",
                   [reservation.id, "tic-a300", "tic-a400"], {}, workspace.id),
        make_event("reservation_released", "reservation",
                   [released.id, "tic-a500"], {}, workspace.id),
        make_event("offer_published", "offer", [assigned_offer.id, "tic-a300"], {},
                   workspace.id),
        make_event("offer_accepted", "offer",
                   [package_offer.id, attempts["completed_member"].id], {},
                   workspace.id),
        make_event("package_bound", "package", [package.id, "tic-a200"], {},
                   workspace.id),
    ]

    with sink.coordination().transaction() as tx:
        for record in records:
            tx.put(record)
        for event in events:
            tx.append_event(event)

    return {
        "workspace": workspace,
        "profiles": profiles,
        "attempts": attempts,
        "reservation": reservation,
        "released_reservation": released,
        "package": package,
        "assigned_offer": assigned_offer,
        "package_offer": package_offer,
        "withdrawn_offer": withdrawn_offer,
    }


def job_board_groups(bundle) -> dict:
    records = bundle["records"]
    return {name: records[name] for name in
            ("worker_profiles", "reservations", "offers", "packages")}


# ---------------------------------------------------------------------------
# Export and round trip
# ---------------------------------------------------------------------------


def test_live_board_is_doctor_clean(kind, sink, arbite_dir):
    board(sink, arbite_dir)
    assert doc.coordination_problems(sink) == []


def test_export_bundle_carries_the_whole_job_board(kind, sink, arbite_dir):
    state = board(sink, arbite_dir)
    bundle = x.export_coordination(sink)
    counts = bundle["counts"]
    assert counts["worker_profiles"] == 2
    assert counts["reservations"] == 2
    assert counts["offers"] == 3
    assert counts["packages"] == 1
    assert x.bundle_problems(bundle) == []

    profiles = {p["worker_id"]: p for p in bundle["records"]["worker_profiles"]}
    assert profiles[WORKER]["capacity"] == 2
    assert profiles[WORKER]["enabled"] is True
    assert profiles[DISABLED]["enabled"] is False
    assert profiles[DISABLED]["disabled_reason"] == "retired operator account"

    offers = {o["id"]: o for o in bundle["records"]["offers"]}
    assert offers[state["assigned_offer"].id]["state"] == "published"
    assert offers[state["assigned_offer"].id]["allowed_workers"] == [WORKER]
    assert offers[state["package_offer"].id]["state"] == "accepted"
    assert offers[state["package_offer"].id]["attempt_id"]

    package = bundle["records"]["packages"][0]
    assert package["tickets"] == ["tic-a100", "tic-a200"]
    assert package["bound_worker"] == WORKER
    assert package["current"] == "tic-a200"
    assert package["handoffs"] and package["notes"]

    released = [r for r in bundle["records"]["reservations"] if r["state"] == "released"]
    assert len(released) == 1 and released[0]["release_reason"]


def test_round_trip_preserves_board_and_history(kind, sink, arbite_dir, tmp_path):
    state = board(sink, arbite_dir)
    bundle = x.export_coordination(sink)
    target = other_sink(kind, tmp_path)

    result = x.import_coordination(target, bundle)
    assert result["records"]["worker_profiles"] == 2
    assert result["records"]["reservations"] == 2
    assert result["records"]["offers"] == 3
    assert result["records"]["packages"] == 1
    assert result["events"] == bundle["counts"]["events"]

    again = x.export_coordination(target)
    assert job_board_groups(again) == job_board_groups(bundle)
    assert again["counts"]["events"] == bundle["counts"]["events"]
    assert x.bundle_problems(again) == []

    # Board and progress still work against the destination store.
    store = target.coordination()
    with store.transaction(write=False) as tx:
        attempts = list(tx.find("work_attempt"))
        packages = list(tx.find("package"))
    active = [a for a in attempts if a.ticket_id == "tic-a200"]
    assert active and active[0].worker_id == WORKER
    assert packages[0].bound_worker == WORKER
    assert packages[0].tickets == ["tic-a100", "tic-a200"]


def test_disabled_profile_history_survives_transfer(kind, sink, arbite_dir, tmp_path):
    state = board(sink, arbite_dir)
    target = other_sink(kind, tmp_path)
    x.import_coordination(target, x.export_coordination(sink))

    store = target.coordination()
    with store.transaction(write=False) as tx:
        profiles = {p.worker_id: p for p in tx.find("worker_profile")}
        attempts = list(tx.find("work_attempt"))
        events = list(tx.find("event"))
    assert profiles[DISABLED].enabled is False
    assert profiles[DISABLED].disabled_at
    assert [a.id for a in attempts if a.worker_id == DISABLED] == [
        state["attempts"]["disabled_history"].id
    ]
    assert any(
        profiles[DISABLED].id in event.subject_ids and event.kind_ == "worker_disabled"
        for event in events
    )


def test_a_transfer_that_loses_job_board_records_is_refused(kind, sink, arbite_dir,
                                                           tmp_path, monkeypatch):
    """Both layers refuse, and they catch different losses.

    A *dropped* reservation is caught by bundle verification (the offer that named
    it is now dangling), while an *alteration* no bundle check can see -- the
    package's continuity binding -- is caught by the history fingerprint. Without
    either, a lossy transfer would report "destination verified".
    """
    state = board(sink, arbite_dir, live=False, active_attempt=False)
    original = x.import_coordination

    def without_reservations(sink_, payload, **kwargs):
        dropped = json.loads(json.dumps(payload))
        lost = {record["id"] for record in dropped["records"]["reservations"]}
        dropped["records"]["reservations"] = []
        dropped["events"] = [
            event for event in dropped["events"]
            if not (set(event.get("subject_ids") or []) & lost)
        ]
        return original(sink_, dropped, **kwargs)

    monkeypatch.setattr(x, "import_coordination", without_reservations)
    with pytest.raises(CoordinationConflict) as refused:
        x.migrate_coordination(sink, other_sink(kind, tmp_path, "lossy"),
                               workspace_id=state["workspace"].id)
    assert "invalid destination bundle" in str(refused.value)
    assert "coordination_dangling_offer" in {
        problem["kind"] for problem in refused.value.details["problems"]
    }

    def with_a_rebound_package(sink_, payload, **kwargs):
        changed = json.loads(json.dumps(payload))
        changed["records"]["packages"][0]["bound_worker"] = "worker.other.9"
        return original(sink_, changed, **kwargs)

    monkeypatch.setattr(x, "import_coordination", with_a_rebound_package)
    with pytest.raises(CoordinationConflict) as refused:
        x.migrate_coordination(sink, other_sink(kind, tmp_path, "altered"),
                               workspace_id=state["workspace"].id)
    assert "fingerprint" in str(refused.value)


def test_quiescent_board_migrates_and_verifies(kind, sink, arbite_dir, tmp_path):
    state = board(sink, arbite_dir, live=False, active_attempt=False)
    target = other_sink(kind, tmp_path)
    assert doc.coordination_problems(sink) == []
    for ticket in sink.query(TicketQuery(buckets=("*",))):
        target.create(ticket)

    result = x.migrate_coordination(sink, target, workspace_id=state["workspace"].id)
    assert result["verified"] is True
    again = x.export_coordination(target)
    assert job_board_groups(again) == job_board_groups(x.export_coordination(sink))
    assert doc.coordination_problems(target) == []


# ---------------------------------------------------------------------------
# Quiescence and the switching refusal
# ---------------------------------------------------------------------------


def test_the_live_board_blocks_a_transfer_and_names_the_records(kind, sink, arbite_dir):
    state = board(sink, arbite_dir, active_attempt=False)
    store = sink.coordination()
    blockers = x.quiescence_blockers(store, state["workspace"].id)
    assert blockers["active_reservation_ids"] == [state["reservation"].id]
    assert blockers["published_offer_ids"] == [state["assigned_offer"].id]
    assert blockers["live_package_ids"] == [state["package"].id]
    assert blockers["active_attempt_ids"] == []

    with pytest.raises(CoordinationConflict) as refused:
        x.require_quiescent_store(store, state["workspace"].id)
    assert "not quiescent" in str(refused.value)
    details = refused.value.details
    assert details["code"] == "store_not_quiescent"
    assert state["reservation"].id in details["active_reservation_ids"]

    with pytest.raises(InvalidRecord):
        x.require_quiescent_store(store, state["workspace"].id, allow_unquiescent=True)
    allowed = x.require_quiescent_store(
        store, state["workspace"].id, allow_unquiescent=True, override_reason="operator"
    )
    assert allowed["live_package_ids"] == [state["package"].id]


def test_migrate_refuses_a_live_board_and_touches_nothing(kind, sink, arbite_dir, tmp_path):
    state = board(sink, arbite_dir)
    target = other_sink(kind, tmp_path)
    with pytest.raises(CoordinationConflict) as refused:
        x.migrate_coordination(sink, target, workspace_id=state["workspace"].id)
    assert "not quiescent" in str(refused.value)
    assert x.export_coordination(target)["counts"]["worker_profiles"] == 0


# ---------------------------------------------------------------------------
# Bundle verification
# ---------------------------------------------------------------------------


def test_bundle_verification_flags_job_board_damage(kind, sink, arbite_dir, tmp_path):
    board(sink, arbite_dir)
    bundle = x.export_coordination(sink)
    assert x.bundle_problems(bundle) == []

    dangling = json.loads(json.dumps(bundle))
    dangling["records"]["offers"][0]["reservation_id"] = "res-0000000000000000"
    assert "coordination_dangling_offer" in {p.kind for p in x.bundle_problems(dangling)}

    overlapping = json.loads(json.dumps(bundle))
    active = next(r for r in overlapping["records"]["reservations"]
                  if r["state"] == "active")
    twin = json.loads(json.dumps(active))
    twin["id"] = c.new_record_id("reservation")
    overlapping["records"]["reservations"].append(twin)
    assert "coordination_overlapping_reservation" in {
        p.kind for p in x.bundle_problems(overlapping)
    }

    target = other_sink(kind, tmp_path)
    with pytest.raises(InvalidRecord):
        x.import_coordination(target, dangling)
    assert x.export_coordination(target)["counts"]["worker_profiles"] == 0


# ---------------------------------------------------------------------------
# Doctor findings
# ---------------------------------------------------------------------------


def damage_dangling_offer(sink, state):
    put(sink.coordination(),
        make_offer(["tic-a300"], reservation_id=c.new_record_id("reservation")))


def damage_dangling_offer_ticket(sink, state):
    put(sink.coordination(), make_offer(["tic-ffff"]))


def damage_overlapping_reservation(sink, state):
    put(sink.coordination(), make_reservation(["tic-a400"], owner="coord.9"))


def damage_overlapping_package(sink, state):
    put(sink.coordination(), make_package(["tic-a200", "tic-a400"],
                                          created_by="coord.9", current="tic-a200"))


def damage_reserved_while_packaged(sink, state):
    put(sink.coordination(), make_reservation(["tic-a200"], owner="coord.9"))


def damage_active_attempt_on_a_closed_ticket(sink, state):
    put(sink.coordination(), make_attempt("tic-a100", WORKER, state["workspace"].id))


def damage_offer_attempt_mismatch(sink, state):
    now = c.utc_now()
    put(sink.coordination(), make_offer(
        ["tic-a300"], mode="assigned", allowed_workers=[WORKER], state="accepted",
        accepted_by=WORKER, accepted_at=now,
        attempt_id=state["attempts"]["completed_member"].id,
    ))


def damage_continuity_binding(sink, state):
    package = c.record_from_dict(json.loads(json.dumps(state["package"].to_dict())))
    package.bound_worker = DISABLED
    put(sink.coordination(), package)


def damage_capacity_arithmetic(sink, state):
    profile = c.record_from_dict(
        json.loads(json.dumps(state["profiles"]["enabled"].to_dict()))
    )
    profile.capacity = 1
    profile.updated = "2026-09-01T00:00:00Z"
    put(
        sink.coordination(),
        profile,
        make_attempt("tic-a300", WORKER, state["workspace"].id),
    )


def damage_namespace_self_import(sink, state):
    store = sink.coordination()
    store.record_namespace(
        store.cursor_namespace(), imported_at=c.utc_now(), event_count=1,
        cursor_map={}, source_contract_version=1,
    )


def damage_namespace_out_of_stream(sink, state):
    store = sink.coordination()
    store.record_namespace(
        "file:/elsewhere", imported_at=c.utc_now(), event_count=1,
        cursor_map={"1": 9999}, source_contract_version=1,
    )


DAMAGE_CASES = (
    ("dangling offer (missing reservation)", "coordination_dangling_offer",
     damage_dangling_offer),
    ("dangling offer (missing ticket)", "coordination_dangling_offer",
     damage_dangling_offer_ticket),
    ("overlapping reservations", "coordination_overlapping_reservation",
     damage_overlapping_reservation),
    ("overlapping packages", "coordination_overlapping_package",
     damage_overlapping_package),
    ("reserved while packaged", "coordination_package_reservation_mismatch",
     damage_reserved_while_packaged),
    ("active attempt on a closed ticket", "coordination_attempt_mismatch",
     damage_active_attempt_on_a_closed_ticket),
    ("accepted offer naming another ticket's attempt", "coordination_attempt_mismatch",
     damage_offer_attempt_mismatch),
    ("continuity binding to a disabled worker",
     "coordination_continuity_binding_invalid", damage_continuity_binding),
    ("capacity below post-update acquisitions", "coordination_capacity_exceeded",
     damage_capacity_arithmetic),
    ("namespace registered as its own import", "coordination_namespace_mismatch",
     damage_namespace_self_import),
    ("namespace cursor map outside the stream", "coordination_namespace_mismatch",
     damage_namespace_out_of_stream),
)


@pytest.mark.parametrize(
    "case,code,damage", DAMAGE_CASES, ids=[case for case, _, _ in DAMAGE_CASES]
)
def test_doctor_reports_job_board_damage_without_guessing(kind, sink, arbite_dir,
                                                           case, code, damage):
    assert doc.coordination_problems(sink) == []
    state = board(sink, arbite_dir)
    assert doc.coordination_problems(sink) == []
    damage(sink, state)

    problems = doc.coordination_problems(sink)
    kinds = {problem.kind for problem in problems}
    assert code in kinds, (case, sorted(kinds))
    assert all(problem.fixed is False for problem in problems)

    # --fix repairs none of these: each has more than one defensible resolution.
    fixing = doc.coordination_problems(sink, fix=True)
    assert all(problem.fixed is False for problem in fixing)
    assert code in {problem.kind for problem in fixing}


# ---------------------------------------------------------------------------
# Legacy stores and cursor provenance
# ---------------------------------------------------------------------------


def test_legacy_ticket_only_store_round_trips_and_doctors_clean(kind, sink, arbite_dir,
                                                                tmp_path):
    sink.create(make_ticket("tic-a1b2", status="in_progress", assignee=WORKER))
    sink.create(make_ticket("tic-a2c3"))

    bundle = x.export_coordination(sink)
    counts = bundle["counts"]
    assert all(counts[name] == 0 for name in
               ("worker_profiles", "reservations", "offers", "packages"))
    assert x.bundle_problems(bundle) == []

    target = other_sink(kind, tmp_path)
    result = x.import_coordination(target, bundle)
    assert all(result["records"][name] == 0 for name in
               ("worker_profiles", "reservations", "offers", "packages"))
    assert doc.coordination_problems(target) == []
    assert doc.coordination_problems(sink) == []


def test_a_cursor_from_an_imported_store_is_refused_with_the_mapping(kind, sink,
                                                                     arbite_dir,
                                                                     tmp_path):
    board(sink, arbite_dir)
    bundle = x.export_coordination(sink)
    target = other_sink(kind, tmp_path)
    x.import_coordination(target, bundle)

    source_token = ev.cursor_token(bundle["cursor_namespace"], 1)
    with pytest.raises(ForeignCursor) as refused:
        ev.query(target, after=source_token)
    details = refused.value.details
    assert details["token_namespace"] == bundle["cursor_namespace"]
    assert details["mapped_cursor"] == 1
    assert details["mapped_cursor_token"] == ev.cursor_token(
        target.coordination().cursor_namespace(), 1
    )


def test_a_cursor_from_an_unknown_store_is_still_refused(kind, sink, arbite_dir):
    board(sink, arbite_dir)
    with pytest.raises(ForeignCursor) as refused:
        ev.query(sink, after="sqlite:/somewhere/else.db#3")
    assert "mapped_cursor" not in refused.value.details


# ---------------------------------------------------------------------------
# CLI: refusal messages, the override and the doctor exit code
# ---------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_project):
    def run(*args, expect=0, sink=None):
        environment = dict(os.environ, PYTHONPATH=str(SRC_DIR))
        environment.pop("ARBITE_SINK", None)
        if sink:
            environment["ARBITE_SINK"] = sink
        proc = subprocess.run(
            [sys.executable, "-m", "arbite.cli", *args],
            cwd=str(tmp_project),
            env=environment,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == expect, (
            f"arbite {' '.join(args)} -> exit {proc.returncode}, expected {expect}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        return proc

    return run


def config_bytes(project_root: Path) -> bytes:
    path = project_root / "arbite.yaml"
    return path.read_bytes() if path.exists() else b""


def seed_project(cli, project_root, kind, **board_kwargs):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    sink = project_sink(project_root, kind)
    state = board(sink, project_root / ".arbite", **board_kwargs)
    return sink, state


def test_migrate_cli_refuses_a_pending_board_and_names_the_records(cli, tmp_project, kind):
    sink, state = seed_project(cli, tmp_project, kind, active_attempt=False)
    before = config_bytes(tmp_project)

    refused = cli("migrate", "--to", other_kind(kind), expect=1, sink=kind)
    assert "not quiescent" in refused.stderr
    assert state["reservation"].id in refused.stderr
    assert state["assigned_offer"].id in refused.stderr
    assert state["package"].id in refused.stderr

    assert config_bytes(tmp_project) == before
    destination = make_sink(other_kind(kind), tmp_project / ".arbite")
    assert x.export_coordination(destination)["counts"]["worker_profiles"] == 0


def test_migrate_force_needs_a_reason(cli, tmp_project, kind):
    seed_project(cli, tmp_project, kind)
    refused = cli("migrate", "--to", other_kind(kind), "--force", expect=1, sink=kind)
    assert "--force needs a non-empty --reason" in refused.stderr


def test_rebind_cli_refuses_a_pending_board_then_reports_the_override(cli, tmp_project,
                                                                      kind):
    seed_project(cli, tmp_project, kind, active_attempt=False)
    target = other_kind(kind)

    refused = cli("rebind", "--to", target, expect=1, sink=kind)
    assert "not quiescent" in refused.stderr

    forced = cli("rebind", "--to", target, "--force", "--reason",
                 "operator decision", expect=0, sink=kind)
    assert "warning: --force overrides the quiescence check" in forced.stderr
    assert "operator decision" in forced.stderr

    marker = json.loads((tmp_project / ".arbite" / "workspace-binding.json").read_text())
    assert marker["sink_kind"] == target
    assert f"sink: {target}" in (tmp_project / "arbite.yaml").read_text()


def test_doctor_cli_reports_job_board_damage_and_exits_3(cli, tmp_project, kind):
    sink, state = seed_project(cli, tmp_project, kind)
    assert cli("doctor", "--json", sink=kind).returncode == 0

    damage_dangling_offer(sink, state)
    report = cli("doctor", "--json", expect=3, sink=kind)
    payload = json.loads(report.stdout)
    kinds = {problem["kind"] for problem in payload["problems"]}
    assert "coordination_dangling_offer" in kinds
    assert payload["fixed"] == 0 and payload["remaining"] >= 1
    assert all(problem["fixed"] is False for problem in payload["problems"])


def test_legacy_project_doctors_clean(cli, tmp_project, kind):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    cli("create", "--title", "legacy", "--type", "bug", "--tier", "medium",
        "--domain", "mesh", sink=kind)
    report = cli("doctor", "--json", sink=kind)
    assert json.loads(report.stdout)["problems"] == []
