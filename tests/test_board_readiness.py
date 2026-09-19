"""Unified readiness, worker capacity and routing explanations (planning key B05).

Two layers are checked here:

- the pure/one-shot evaluator in `arbite.readiness` (reason codes, capacity
  arithmetic, hard requirements vs advisory hints), which runs against both
  sinks through the `sink` fixture where it touches the store;
- the `arbite board` CLI surface and the `list next`/`claim` paths it must agree
  with, run as real subprocesses in a throwaway project for each sink.

Nothing here touches the checkout's own `.arbite/` store.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from arbite import application, coordination as c, eligibility, lifecycle
from arbite import offers as offers_module
from arbite import readiness, workers
from arbite.application import Actor
from arbite.errors import CoordinationConflict, TicketError, WorkerIneligible
from helpers import by_id, make_ticket

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def make_lifecycle(sink, arbite_dir) -> lifecycle.TicketLifecycle:
    service = application.coordination_service_for(
        sink, root=str(arbite_dir.parent), actor=Actor("tester")
    )
    return lifecycle.TicketLifecycle(service, sink)


def add(sink, ticket_id, **overrides):
    overrides.setdefault("status", "open")
    overrides.setdefault("tier", "low")
    sink.create(make_ticket(ticket_id, **overrides))
    return sink.get(ticket_id)


def make_attempt(ticket_id: str, worker_id: str, workspace_id: str, generation: int = 1):
    moment = c.utc_now()
    return c.WorkAttempt(
        id=c.new_record_id("work_attempt"),
        ticket_id=ticket_id,
        worker_id=worker_id,
        workspace_id=workspace_id,
        generation=generation,
        started=moment,
        last_activity=moment,
    )


def make_offer(**overrides) -> c.Offer:
    data = dict(
        id=c.new_record_id("offer"),
        tickets=["tic-0001"],
        mode="public",
        publisher="coord",
        created=c.utc_now(),
        updated=c.utc_now(),
    )
    data.update(overrides)
    return c.Offer(**data)


# --- the ticket axis (pure) ---------------------------------------------------


def test_ticket_reasons_cover_every_axis_with_stable_codes():
    ready = make_ticket("tic-0001")
    waiting = make_ticket("tic-0002", depends_on=["tic-0001"])
    unclassified = make_ticket(
        "tic-0003", status="raw", tier="TODO: low|medium", domain="TODO: e.g. mesh"
    )
    closed = make_ticket("tic-0004", status="closed", closed="2026-02-01T00:00:00")
    every = by_id(ready, waiting, unclassified, closed)

    assert readiness.ticket_reasons(ready, every) == []

    waiting_codes = [r.code for r in readiness.ticket_reasons(waiting, every)]
    assert waiting_codes == ["dependencies_unmet"]
    assert readiness.ticket_reasons(waiting, every)[0].details["unmet"] == ["tic-0001"]

    assert [r.code for r in readiness.ticket_reasons(closed, every)] == ["ticket_not_open"]
    assert [r.code for r in readiness.ticket_reasons(unclassified, every)] == [
        "ticket_not_open", "ticket_unclassified", "ticket_unclassified",
    ]

    attempt = make_attempt("tic-0001", "w.a", "ws-1")
    taken = readiness.ticket_reasons(ready, every, attempt)
    assert [r.code for r in taken] == ["active_attempt"]
    assert taken[0].axis == "attempt"


def test_require_claimable_raises_what_the_lifecycle_always_raised():
    closed = make_ticket("tic-0001", status="closed")
    open_ticket = make_ticket("tic-0002")
    attempt = make_attempt("tic-0002", "w.a", "ws-1")

    with pytest.raises(TicketError) as direct:
        readiness.require_claimable(closed, by_id(closed), None)
    with pytest.raises(TicketError) as delegated:
        lifecycle.require_claimable(closed, by_id(closed), None)
    assert str(direct.value) == str(delegated.value)

    with pytest.raises(CoordinationConflict):
        readiness.require_claimable(open_ticket, by_id(open_ticket), attempt)
    with pytest.raises(CoordinationConflict):
        lifecycle.require_claimable(open_ticket, by_id(open_ticket), attempt)

    # An unready ticket id, an unmet dependency and an active attempt are all
    # refused identically through both entry points.
    assert readiness.require_claimable(open_ticket, by_id(open_ticket), None) is None


# --- capacity (pure + store) --------------------------------------------------


def test_capacity_remaining_and_exhausted():
    assert readiness.Capacity().unlimited is True
    assert readiness.Capacity().remaining is None
    assert readiness.Capacity(declared=2, active=1).remaining == 1
    assert readiness.Capacity(declared=2, active=2).exhausted is True
    # A profile lowered below its running work never reports negative slots.
    lowered = readiness.Capacity(declared=1, active=3)
    assert lowered.remaining == 0 and lowered.exhausted is True


def test_acquire_enforces_declared_capacity_and_spares_a_superseded_attempt(sink, arbite_dir):
    lc = make_lifecycle(sink, arbite_dir)
    workers.WorkerProfileService(sink.coordination()).register("w.cap", tier="medium", capacity=1)
    first = add(sink, "tic-0001")
    second = add(sink, "tic-0002")

    lc.acquire(first, worker_id="w.cap")

    with pytest.raises(WorkerIneligible) as refused:
        lc.acquire(second, worker_id="w.cap")
    details = refused.value.details
    assert details["reasons"][0]["code"] == "capacity_exhausted"
    assert details["reasons"][0]["axis"] == "capacity"
    assert details["capacity"] == {
        "declared": 1, "active": 1, "remaining": 0, "exhausted": True,
    }

    # Capacity is per worker id: an ad-hoc worker is unaffected.
    lc.acquire(second, worker_id="w.adhoc")

    # A takeover of the worker's own attempt replaces it, so it is not double
    # counted -- the worker still owns exactly one active attempt.
    taken = lc.acquire(first, worker_id="w.cap", takeover=True, reason="restart in place")
    assert taken.took_over is True
    active = [a for a in lc.attempts_for("tic-0001") if a.is_active]
    assert len(active) == 1 and active[0].worker_id == "w.cap"
    assert readiness.active_attempt_count(sink.coordination(), "w.cap") == 1


def test_capacity_counts_active_attempts_only(sink, arbite_dir):
    store = sink.coordination()
    workers.WorkerProfileService(store).register("w.only", tier="medium", capacity=3)
    declaration = workers.WorkerProfileService(store).declaration("w.only")

    assert readiness.capacity_for(readiness.load(store, []), "w.only", declaration).active == 0

    with store.transaction() as tx:
        tx.put(make_attempt("tic-0001", "w.only", "ws-1"), expect_revision=0)
        tx.put(make_attempt("tic-0002", "w.someone-else", "ws-1"), expect_revision=0)
    state = readiness.load(store, [make_ticket("tic-0001")])
    assert state.active_for("w.only") == 1
    assert readiness.capacity_for(state, "w.only", declaration).remaining == 2


# --- hard requirements vs advisory hints (pure) -------------------------------


def test_offer_requirements_reject_and_preferences_only_hint():
    ticket = make_ticket("tic-0001")
    ad_hoc = eligibility.WorkerDeclaration.ad_hoc("w.remote", declared_tier="high")

    hard = make_offer(
        requirements={"capabilities": ["rust"]},
        preferences={"prefer_local": True},
    )
    hard_state = readiness.BoardState(
        by_id={"tic-0001": ticket},
        published_offers={"tic-0001": offers_module.StoredOffer(hard, 1)},
    )
    refused = readiness.evaluate(ticket, hard_state, worker_id="w.remote", declaration=ad_hoc)
    assert refused.ready is False
    assert [r.code for r in refused.reasons] == ["offer_ineligible"]
    nested = refused.reasons[0].details["reasons"]
    assert [r["code"] for r in nested] == ["capabilities_unknown"]
    assert refused.eligibility.eligible  # the ticket's own tier rule is satisfied

    # The same ticket with a preference-only offer stays ready: the hint is
    # advisory, so passive first-eligible pickup is unaffected.
    soft = make_offer(preferences={"prefer_local": True})
    soft_state = readiness.BoardState(
        by_id={"tic-0001": ticket},
        published_offers={"tic-0001": offers_module.StoredOffer(soft, 1)},
    )
    hinted = readiness.evaluate(ticket, soft_state, worker_id="w.remote", declaration=ad_hoc)
    assert hinted.ready is True
    assert [h.code for h in hinted.hints] == ["preference_unsatisfied"]
    assert hinted.hints[0].hard is False
    assert [r.code for r in readiness.preference_hints(soft, ad_hoc)] == ["preference_unsatisfied"]
    satisfied = eligibility.WorkerDeclaration.from_profile(
        c.WorkerProfile(
            id=c.new_record_id("worker_profile"), worker_id="w.local", tier="high",
            locality="local", created=c.utc_now(), updated=c.utc_now(),
        )
    )
    assert [h.code for h in readiness.preference_hints(soft, satisfied)] == [
        "preference_satisfied"
    ]


def test_evaluation_separates_axes_and_reports_capacity_last():
    ticket = make_ticket("tic-0001")
    reservation = c.Reservation(
        id=c.new_record_id("reservation"), owner="coord", members=["tic-0001"],
        created=c.utc_now(), updated=c.utc_now(),
    )
    declaration = eligibility.WorkerDeclaration.ad_hoc("w.a", declared_tier="high")
    state = readiness.BoardState(
        by_id={"tic-0001": ticket},
        reservations={"tic-0001": reservation},
        active_by_worker={"w.a": 1},
    )
    verdict = readiness.evaluate(
        ticket, state, worker_id="w.a", declaration=declaration,
        capacity=readiness.Capacity(declared=1, active=1),
    )
    assert verdict.ready is False
    assert [r.code for r in verdict.reasons] == ["reserved_elsewhere", "capacity_exhausted"]
    assert verdict.has_axis("reservation", "capacity") is True
    assert verdict.by_axis("capacity")[0].message.startswith("worker w.a has reached")
    assert readiness.AXES == tuple(dict.fromkeys(readiness.AXES))  # stable, ordered


# --- the CLI: board, list next and claim agree --------------------------------

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
    return re.search(r"(tic-[0-9a-f]{4})", proc.stdout).group(1)


def make(cli, title, tier="low", **extra):
    args = ["create", "--title", title, "--type", "bug", "--tier", tier, "--domain", "py"]
    for name, value in extra.items():
        args += [f"--{name.replace('_', '-')}", str(value)]
    return created_id(cli(*args))


def board(cli, worker, *extra, expect=0):
    return json.loads(cli("board", "--worker", worker, "--json", *extra, expect=expect).stdout)["data"]


def ready_ids(data) -> list:
    return [ticket["id"] for ticket in data["ready"]]


def excluded_codes(data, ticket_id) -> list:
    entry = [ticket for ticket in data["excluded"] if ticket["id"] == ticket_id][0]
    return [reason["code"] for reason in entry["readiness"]["reasons"]]


def test_board_explains_every_axis_and_agrees_with_list_next(cli):
    cli("worker", "register", "w.a", "--tier", "high", "--capability", "python", "--json")
    low = make(cli, "the low one", priority=1)
    high = make(cli, "the high one", tier="high", priority=2)
    dependent = make(cli, "waits for low", tier="high", priority=3)
    cli("depend", dependent, low)
    cli("raw", "feature", "an unclassified capture")
    unclassified = json.loads(cli("list", "--status", "raw", "--json").stdout)[0]["id"]

    data = board(cli, "w.a")
    assert data["worker"]["worker_id"] == "w.a"
    assert data["worker"]["source"] == "profile"
    assert data["capacity"] == {
        "declared": None, "active": 0, "remaining": None, "exhausted": False,
    }
    # most urgent ready first, and the dependency holds nothing back but its own
    assert ready_ids(data) == [low, high]
    assert data["suggestions"] == [low, high]

    assert excluded_codes(data, dependent) == ["dependencies_unmet"]
    codes = excluded_codes(data, unclassified)
    assert codes[0] == "ticket_not_open" and "ticket_unclassified" in codes

    # Parity: with no offers/reservations/packages and a worker that can take
    # everything, the board's ready set is exactly what `list next` offers.
    listed = json.loads(cli("list", "next", "--count", "10", "--json").stdout)
    assert [ticket["id"] for ticket in listed] == ready_ids(data)

    # An unknown worker is ad-hoc: no profile, unlimited capacity, and the same
    # unrestricted ticket tier rule (an unknown tier is advisory, not a refusal).
    ad_hoc = board(cli, "nobody.at.all")
    assert ad_hoc["worker"]["source"] == "ad_hoc"
    assert ad_hoc["capacity"]["declared"] is None
    assert ad_hoc["capacity"]["remaining"] is None
    assert ready_ids(ad_hoc) == [low, high]

    # --epic narrows what is explained, not how readiness is computed.
    epics = board(cli, "w.a", "--epic", "nope", expect=2)
    assert epics["ready"] == [] and epics["excluded"] == []

    # ...and claiming the ready set takes exactly it.
    claimed = json.loads(
        cli("list", "next", "--claim", "w.a", "--count", "10", "--json").stdout
    )
    assert [ticket["id"] for ticket in claimed] == ready_ids(data)
    assert ready_ids(board(cli, "w.a", expect=2)) == []

    # A per-call tier may never elevate a registered profile.
    cli("worker", "register", "w.low", "--tier", "medium", "--json")
    elevated = cli("board", "--worker", "w.low", "--tier", "high", "--json", expect=1)
    payload = json.loads(elevated.stdout)
    assert payload["code"] == "worker_ineligible"
    assert "exceeds the registered profile" in json.dumps(payload)

    # A disabled profile is explained as such and ready for nothing.
    cli("worker", "disable", "w.low", "--reason", "parked")
    disabled = board(cli, "w.low", expect=2)
    assert ready_ids(disabled) == []
    open_excluded = [t for t in disabled["excluded"] if t["status"] == "open"]
    assert open_excluded and all(
        "worker_ineligible" in excluded_codes(disabled, ticket["id"])
        for ticket in open_excluded
    )
    nested = [reason["code"]
              for reason in open_excluded[0]["readiness"]["reasons"][-1]["details"]["reasons"]]
    assert "worker_disabled" in nested and "tier_insufficient" in nested


def test_board_is_read_only(cli):
    cli("worker", "register", "w.ro", "--tier", "high", "--json")
    make(cli, "something to do")
    before = json.loads(cli("export", "--scope", "coordination", "--json").stdout)["data"]
    board(cli, "w.ro")
    cli("board", "--worker", "w.ro", expect=0)
    after = json.loads(cli("export", "--scope", "coordination", "--json").stdout)["data"]
    assert after["counts"]["work_attempts"] == before["counts"]["work_attempts"] == 0
    assert after["counts"]["reservations"] == 0 and after["counts"]["offers"] == 0
    assert json.loads(cli("list", "--status", "in_progress", "--json", expect=2).stdout) == []


def test_capacity_limits_a_batch_and_frees_on_close(cli):
    cli("worker", "register", "w.cap", "--tier", "high", "--capacity", "1", "--json")
    first = make(cli, "first", priority=1)
    second = make(cli, "second", priority=2)
    third = make(cli, "third", priority=3)

    batch = cli("list", "next", "--claim", "w.cap", "--count", "3", "--json")
    assert [ticket["id"] for ticket in json.loads(batch.stdout)] == [first]
    assert "declared capacity of 1" in batch.stderr
    assert "asked for 3 ticket(s), claimed 1" in batch.stderr

    data = board(cli, "w.cap", expect=2)
    assert data["capacity"] == {
        "declared": 1, "active": 1, "remaining": 0, "exhausted": True,
    }
    assert ready_ids(data) == []
    assert any("capacity_exhausted" in excluded_codes(data, ticket_id)
               for ticket_id in (second, third))
    assert data["excluded"][0]["readiness"]["reasons"][-1]["details"]["declared"] == 1

    blocked = cli("list", "next", "--claim", "w.cap", "--count", "2", expect=2)
    assert "declared capacity of 1" in blocked.stderr

    # Finishing the work releases the slot for future acquisition.
    cli("close", first, "--agent", "w.cap")
    again = cli("list", "next", "--claim", "w.cap", "--count", "3", "--json")
    assert [ticket["id"] for ticket in json.loads(again.stdout)] == [second]


def test_concurrent_claims_cannot_overfill_a_worker(cli, tmp_project, kind):
    cli("worker", "register", "w.race", "--tier", "high", "--capacity", "1", "--json")
    one = make(cli, "race one")
    two = make(cli, "race two")
    environment = dict(os.environ, PYTHONPATH=str(SRC_DIR), ARBITE_SINK=kind)

    def start(ticket):
        return subprocess.Popen(
            [sys.executable, "-m", "arbite.cli", "claim", ticket, "--agent", "w.race"],
            cwd=str(tmp_project), env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    first, second = start(one), start(two)
    out_one, err_one = first.communicate()
    out_two, err_two = second.communicate()
    assert sorted([first.returncode, second.returncode]) == [0, 1]
    loser = err_one if first.returncode else err_two
    assert "declared capacity" in loser
    assert "capacity 1" in loser

    data = board(cli, "w.race", expect=2)
    assert data["capacity"]["active"] == 1
    in_progress = json.loads(cli("list", "--status", "in_progress", "--json").stdout)
    assert len(in_progress) == 1  # exactly one attempt survived the race


def test_reservations_and_future_package_members_do_not_consume_capacity(cli):
    cli("worker", "register", "coord", "--tier", "high", "--capacity", "1", "--json")
    mine = make(cli, "coordinator's own")
    held_one = make(cli, "held one")
    held_two = make(cli, "held two")
    cli("reserve", "create", held_one, held_two, "--agent", "coord", "--json")

    # A reservation is a hold, not work: list next leaves members out for others.
    noticed = cli("list", "next", "--count", "10", "--json")
    assert [ticket["id"] for ticket in json.loads(noticed.stdout)] == [mine]
    assert "held by reservations" in noticed.stderr

    cli("claim", mine, "--agent", "coord")
    data = board(cli, "coord", expect=2)
    # Three tickets are in play for `coord`, but only the one active attempt counts.
    assert data["capacity"] == {
        "declared": 1, "active": 1, "remaining": 0, "exhausted": True,
    }
    assert "capacity_exhausted" in excluded_codes(data, held_one)
    refused = cli("claim", held_one, "--agent", "coord", expect=1)
    assert "declared capacity" in refused.stderr

    # A package's later members have no attempt yet, so they do not consume the
    # bound worker's capacity either.
    cli("worker", "register", "w.pkg", "--tier", "high", "--capacity", "2", "--json")
    step_one = make(cli, "package step one", tier="high", priority=8)
    step_two = make(cli, "package step two", tier="high", priority=9)
    spare = make(cli, "unbound spare", tier="high", priority=10)
    package = json.loads(cli(
        "package", "create", step_one, step_two, "--agent", "coord", "--json",
    ).stdout)["data"]["package"]["id"]
    cli("package", "handoff", package, "--agent", "coord", "--reason", "give to w.pkg",
        "--to", "w.pkg", "--json")
    cli("claim", step_one, "--agent", "w.pkg")

    bound = board(cli, "w.pkg", expect=0)
    assert bound["capacity"] == {
        "declared": 2, "active": 1, "remaining": 1, "exhausted": False,
    }
    assert ready_ids(bound) == [spare]  # the free slot is real
    assert step_two in [ticket["id"] for ticket in bound["excluded"]]
    assert "package_order" in excluded_codes(bound, step_two)


def test_offer_hard_requirements_reject_while_preferences_only_hint(cli):
    cli("worker", "register", "w.hard", "--tier", "high", "--capability", "python",
        "--locality", "remote", "--json")
    cli("worker", "register", "w.other", "--tier", "high", "--capability", "rust", "--json")
    ticket = make(cli, "offered work", tier="high")

    cli("offer", "publish", ticket, "--agent", "coord", "--require-capability", "rust",
        "--prefer-local", "--json")
    hard = board(cli, "w.hard", expect=2)
    assert ready_ids(hard) == []
    assert excluded_codes(hard, ticket) == ["offer_ineligible"]
    entry = [t for t in hard["excluded"] if t["id"] == ticket][0]
    nested = entry["readiness"]["reasons"][0]["details"]["reasons"]
    assert [reason["code"] for reason in nested] == ["capability_missing"]

    # Only the worker that meets the hard constraint is offered it.
    eligible = board(cli, "w.other")
    assert ready_ids(eligible) == [ticket]
    hints = eligible["ready"][0]["readiness"]["hints"]
    assert [hint["code"] for hint in hints] == ["preference_unsatisfied"]
    assert all(hint["hard"] is False for hint in hints)

    published = json.loads(cli("offer", "list", "--json").stdout)["data"]["offers"][0]["id"]
    cli("offer", "withdraw", published, "--agent", "coord", "--json")

    # A preference-only offer leaves the ticket ready for anyone eligible: the
    # hint never decides who wins, so passive pickup still takes it.
    cli("offer", "publish", ticket, "--agent", "coord", "--prefer-local", "--json")
    soft = board(cli, "w.hard")
    assert ready_ids(soft) == [ticket]
    assert [hint["code"] for hint in soft["ready"][0]["readiness"]["hints"]] == [
        "preference_unsatisfied"
    ]
    claimed = json.loads(cli("list", "next", "--claim", "w.hard", "--json").stdout)
    assert [entry["id"] for entry in claimed] == [ticket]

    # A direct assignment *can* enforce the owner's choice: it is a hard limit.
    cli("offer", "withdraw", published, "--agent", "coord", "--json", expect=1)
    other = make(cli, "assigned work", tier="high")
    cli("offer", "assign", other, "--worker", "w.other", "--agent", "coord", "--json")
    assigned = board(cli, "w.hard", expect=2)
    assert excluded_codes(assigned, other) == ["offer_ineligible"]
    entry = [t for t in assigned["excluded"] if t["id"] == other][0]
    nested = entry["readiness"]["reasons"][0]["details"]["reasons"]
    assert [reason["code"] for reason in nested] == ["worker_not_allowed"]
    refused = cli("claim", other, "--agent", "w.hard", expect=1)
    assert "may not acquire it" in refused.stderr


def test_profile_change_affects_future_work_without_revoking_a_running_attempt(cli):
    cli("worker", "register", "w.up", "--tier", "medium", "--capacity", "1", "--json")
    running = make(cli, "already running", priority=1)
    waiting = make(cli, "waits its turn", priority=2)

    claimed = json.loads(cli("list", "next", "--claim", "w.up", "--json").stdout)
    assert [ticket["id"] for ticket in claimed] == [running]

    raised = json.loads(cli(
        "worker", "update", "w.up", "--tier", "high", "--capacity", "3",
        "--reason", "hardware upgrade", "--json",
    ).stdout)["data"]
    assert sorted(raised["changes"]) == ["capacity", "tier"]

    still_running = json.loads(cli("show", running, "--json").stdout)
    assert still_running["status"] == "in_progress"
    assert still_running["assignee"] == "w.up"

    data = board(cli, "w.up")
    assert data["capacity"] == {
        "declared": 3, "active": 1, "remaining": 2, "exhausted": False,
    }
    assert ready_ids(data) == [waiting]

    # Lowering capacity below the running work blocks future pickup without
    # touching the attempt that is already underway.
    lowered = json.loads(cli(
        "worker", "update", "w.up", "--capacity", "1", "--reason", "budget", "--json",
    ).stdout)["data"]
    assert sorted(lowered["changes"]) == ["capacity"]
    assert json.loads(cli("show", running, "--json").stdout)["status"] == "in_progress"
    full = board(cli, "w.up", expect=2)
    assert full["capacity"]["exhausted"] is True
    assert ready_ids(full) == []
    cli("claim", waiting, "--agent", "w.up", expect=1)
