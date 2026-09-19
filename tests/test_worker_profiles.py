"""Passive worker profiles and eligibility declarations (planning key B01).

Store-backed tests run against both sinks through the `sink` fixture; the CLI
tests run the real command in a throwaway project for each sink. Nothing here
touches the checkout's own `.arbite/` store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from arbite import application, coordination as c, coordination_export as x, eligibility as el
from arbite import lifecycle, workers
from arbite.application import Actor
from arbite.errors import (
    CoordinationConflict,
    CoordinationNotFound,
    InvalidRecord,
    WorkerIneligible,
)
from conftest import make_sink
from helpers import make_ticket

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def make_profile(**overrides) -> c.WorkerProfile:
    now = c.utc_now()
    data = dict(
        id=c.new_record_id("worker_profile"),
        worker_id="claude.opus-5.001",
        tier="medium",
        created=now,
        updated=now,
    )
    data.update(overrides)
    return c.WorkerProfile(**data)


def service(sink, **kwargs) -> workers.WorkerProfileService:
    return workers.WorkerProfileService(sink.coordination(), **kwargs)


def make_lifecycle(sink, arbite_dir):
    svc = application.coordination_service_for(
        sink, root=str(arbite_dir.parent), actor=Actor("tester")
    )
    return lifecycle.TicketLifecycle(svc, sink)


def add(sink, ticket_id, **overrides):
    overrides.setdefault("status", "open")
    sink.create(make_ticket(ticket_id, **overrides))
    return sink.get(ticket_id)


def worker_events(sink):
    return [e for e in sink.coordination().event_log() if e.category == "worker"]


# --- the record --------------------------------------------------------------


def test_profile_round_trips_and_validates_clean():
    profile = make_profile(
        provider="anthropic",
        model="claude-opus-5",
        capabilities=["python", "shell"],
        locality="local",
        cost_class="paid",
        cost_estimate={"amount": 2.5, "unit": "USD/ticket", "provenance": "operator"},
        capacity=2,
    )
    assert profile.validate() == []
    again = c.record_from_dict(json.loads(json.dumps(profile.to_dict())))
    assert again == profile
    assert c.is_opaque_id(profile.id, "wkr")


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"tier": "godlike"}, "tier"),
        ({"worker_id": "has space"}, "worker_id"),
        ({"capabilities": ["Not Lower"]}, "capability"),
        ({"capabilities": ["a", "a"]}, "repeat"),
        ({"locality": "moon"}, "locality"),
        ({"cost_class": "cheap"}, "cost_class"),
        ({"cost_estimate": {"amount": 1}}, "unit"),
        ({"cost_estimate": {"amount": -1, "unit": "USD", "provenance": "x"}}, "amount"),
        ({"capacity": 0}, "capacity"),
        ({"enabled": False}, "disabled_at"),
        ({"last_checkin": "2026-01-01T00:00:00"}, "last_checkin"),
    ],
)
def test_profile_validation_reports_bad_fields(overrides, fragment):
    problems = make_profile(**overrides).validate()
    assert any(fragment in problem for problem in problems), problems


@pytest.mark.parametrize(
    "value",
    [
        "sk-ant-api03-abcdefghijklmnopqrstuv",
        "sk-proj-abcdefghijklmnopqrstuvwx",
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
        "AKIAABCDEFGHIJKLMNOP",
        "api_key=hunter2",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8",
    ],
)
def test_credential_like_values_are_refused(value):
    assert c.looks_like_secret(value)
    problems = make_profile(model=value).validate()
    assert any("credential" in problem for problem in problems)


@pytest.mark.parametrize("value", ["anthropic", "claude-opus-5", "gpt-5.1-codex", "local-llama-70b"])
def test_ordinary_labels_are_not_mistaken_for_secrets(value):
    assert not c.looks_like_secret(value)


def test_two_profiles_for_one_worker_is_a_collection_problem():
    first = make_profile()
    second = make_profile()
    problems = c.validate_collection([first, second])
    assert any("two profiles" in problem for problem in problems)


# --- eligibility (pure) ----------------------------------------------------


def ad_hoc(**kwargs):
    return el.WorkerDeclaration.ad_hoc("adhoc.1", **kwargs)


def codes(result):
    return [reason.code for reason in result.reasons]


def test_restricted_requirements_fail_every_unknown_with_a_reason():
    requirements = el.Requirements(
        min_tier="low",
        capabilities=("python",),
        local_only=True,
        max_cost={"amount": 1, "unit": "USD/ticket"},
    )
    result = el.evaluate(requirements, ad_hoc())
    assert not result.eligible
    assert codes(result) == ["tier_unknown", "capabilities_unknown", "locality_unknown", "cost_unknown"]
    with pytest.raises(WorkerIneligible) as excinfo:
        result.require(subject="offer")
    assert excinfo.value.error_code == "worker_ineligible"
    assert [r["code"] for r in excinfo.value.details["reasons"]] == codes(result)


def test_unrestricted_requirements_keep_ad_hoc_workers_working():
    ticket = make_ticket("tic-a1", status="open", tier="frontier")
    result = el.evaluate(el.requirements_for_ticket(ticket), ad_hoc())
    assert result.eligible
    assert [note.code for note in result.notes] == ["tier_unverified"]


def test_known_values_are_enforced_even_when_unrestricted():
    profile = make_profile(tier="medium")
    declaration = el.WorkerDeclaration.from_profile(profile)
    ticket = make_ticket("tic-a1", status="open", tier="high")
    result = el.evaluate(el.requirements_for_ticket(ticket), declaration)
    assert codes(result) == ["tier_insufficient"]
    ok = el.evaluate(el.requirements_for_ticket(make_ticket("tic-a2", tier="low")), declaration)
    assert ok.eligible and ok.notes == ()


def test_declared_tier_cannot_elevate_a_profile():
    profile = make_profile(tier="medium")
    with pytest.raises(WorkerIneligible) as excinfo:
        el.WorkerDeclaration.from_profile(profile, declared_tier="high")
    assert excinfo.value.details["reasons"][0]["code"] == "declared_tier_exceeds_profile"
    lower = el.WorkerDeclaration.from_profile(profile, declared_tier="low")
    assert lower.tier == "medium"  # the configured tier stays authoritative


def test_disabled_profile_and_allow_list_fail():
    now = c.utc_now()
    profile = make_profile(enabled=False, disabled_at=now)
    result = el.evaluate(
        el.Requirements(allowed_workers=("someone.else",), restricted=False),
        el.WorkerDeclaration.from_profile(profile),
    )
    assert codes(result) == ["worker_disabled", "worker_not_allowed"]


def test_capability_locality_and_cost_checks_on_known_values():
    profile = make_profile(
        capabilities=["python"],
        locality="remote",
        cost_class="paid",
        cost_estimate={"amount": 5, "unit": "USD/ticket", "provenance": "guess"},
    )
    declaration = el.WorkerDeclaration.from_profile(profile)
    result = el.evaluate(
        el.Requirements(
            capabilities=("python", "rust"), local_only=True,
            max_cost={"amount": 1, "unit": "USD/ticket"},
        ),
        declaration,
    )
    assert codes(result) == ["capability_missing", "locality_mismatch", "cost_exceeds_ceiling"]
    other_unit = el.evaluate(el.Requirements(max_cost={"amount": 100, "unit": "EUR/ticket"}), declaration)
    assert codes(other_unit) == ["cost_unit_mismatch"]


def test_local_cost_class_satisfies_a_ceiling_with_a_note():
    declaration = el.WorkerDeclaration.from_profile(make_profile(cost_class="local", locality="local"))
    result = el.evaluate(
        el.Requirements(local_only=True, max_cost={"amount": 0, "unit": "USD/ticket"}), declaration
    )
    assert result.eligible
    assert [note.code for note in result.notes] == ["cost_assumed_local"]


def test_requirements_refuse_a_unitless_ceiling():
    with pytest.raises(InvalidRecord):
        el.Requirements(max_cost={"amount": 1})


# --- the service, on both sinks --------------------------------------------


def test_register_show_list_persist_across_reopen(sink, kind, arbite_dir):
    change = service(sink, actor="operator").register(
        "claude.a",
        tier="high",
        provider="anthropic",
        model="claude-opus-5",
        capabilities=["Python", "shell,python"],
        locality="local",
        cost_class="paid",
        cost_estimate={"amount": 3, "unit": "USD/ticket", "provenance": "operator guess"},
        capacity=2,
    )
    assert change.stored.revision == 1
    assert change.stored.profile.capabilities == ["python", "shell"]

    reopened = make_sink(kind, arbite_dir, initialise=False)
    stored = service(reopened).get("claude.a")
    assert stored.revision == 1
    assert stored.profile == change.stored.profile
    view = stored.view()
    assert view["liveness"] == "unverified"
    assert view["last_checkin"] is None
    assert [row.profile.worker_id for row in service(reopened).list()] == ["claude.a"]

    events = worker_events(reopened)
    assert [e.event_kind for e in events] == ["worker_registered"]
    assert events[0].payload["actor"] == "operator"
    assert events[0].payload["worker_id"] == "claude.a"
    assert set(events[0].subject_ids) == {"claude.a", change.stored.profile.id}


def test_register_refuses_a_duplicate_and_secrets(sink):
    svc = service(sink)
    svc.register("w1", tier="low")
    with pytest.raises(CoordinationConflict):
        svc.register("w1", tier="high")
    with pytest.raises(InvalidRecord) as excinfo:
        svc.register("w2", tier="low", runtime="token=abc123")
    assert excinfo.value.details["reason"] == "secret_like_value"
    assert svc.find("w2") is None
    assert [row.profile.worker_id for row in svc.list()] == ["w1"]


def test_update_records_explicit_before_after_and_honours_expect_revision(sink):
    svc = service(sink)
    svc.register("w1", tier="low", capabilities=["python"])
    change = svc.update("w1", tier="high", add_capabilities=["rust"], reason="promoted",
                        expect_revision=1)
    assert change.stored.revision == 2
    assert change.changes["tier"] == {"from": "low", "to": "high"}
    assert change.changes["capabilities"] == {"from": ["python"], "to": ["python", "rust"]}
    event = worker_events(sink)[-1]
    assert event.event_kind == "worker_updated"
    assert event.payload["reason"] == "promoted"

    with pytest.raises(CoordinationConflict):
        svc.update("w1", tier="low", expect_revision=1)
    assert svc.get("w1").profile.tier == "high"

    noop = svc.update("w1", tier="high")
    assert not noop.changed and noop.event is None
    assert svc.get("w1").revision == 2

    cleared = svc.update("w1", provider=None, capacity=None, remove_capabilities=["python"])
    assert cleared.stored.profile.capabilities == ["rust"]
    with pytest.raises(CoordinationNotFound):
        svc.update("nobody", tier="low")


def test_checkin_is_declared_activity_without_an_event(sink):
    svc = service(sink, clock=lambda: "2026-09-18T10:00:00Z")
    svc.register("w1", tier="low")
    stored = svc.checkin("w1")
    assert stored.profile.last_checkin == "2026-09-18T10:00:00Z"
    assert stored.view()["liveness"] == "unverified"
    assert [e.event_kind for e in worker_events(sink)] == ["worker_registered"]


def test_list_on_an_uninitialised_coordination_store_creates_nothing(kind, arbite_dir):
    fresh = make_sink(kind, arbite_dir)
    store = fresh.coordination()
    assert workers.WorkerProfileService(store).list() == []
    assert workers.declaration_for(store, "anyone").source == el.SOURCE_AD_HOC


# --- acquisition, on both sinks --------------------------------------------


def test_ad_hoc_workers_still_claim_any_tier(sink, arbite_dir):
    add(sink, "tic-a1", tier="frontier")
    ctl = make_lifecycle(sink, arbite_dir)
    result = ctl.acquire(sink.get("tic-a1"), worker_id="adhoc.1")
    assert result.attempt.worker_id == "adhoc.1"


def test_registered_tier_is_authoritative_at_acquisition(sink, arbite_dir):
    add(sink, "tic-hi", tier="high")
    add(sink, "tic-md", tier="medium")
    service(sink).register("w.med", tier="medium")
    ctl = make_lifecycle(sink, arbite_dir)

    with pytest.raises(WorkerIneligible) as excinfo:
        ctl.acquire(sink.get("tic-hi"), worker_id="w.med")
    assert excinfo.value.details["reasons"][0]["code"] == "tier_insufficient"
    assert ctl.attempts_for("tic-hi") == []
    assert sink.get("tic-hi").status == "open"

    with pytest.raises(WorkerIneligible) as excinfo:
        ctl.acquire(sink.get("tic-md"), worker_id="w.med", declared_tier="high")
    assert excinfo.value.details["reasons"][0]["code"] == "declared_tier_exceeds_profile"
    assert ctl.attempts_for("tic-md") == []

    assert ctl.acquire(sink.get("tic-md"), worker_id="w.med").attempt.worker_id == "w.med"


def test_force_takeover_does_not_bypass_eligibility(sink, arbite_dir):
    add(sink, "tic-hi", tier="high")
    ctl = make_lifecycle(sink, arbite_dir)
    first = ctl.acquire(sink.get("tic-hi"), worker_id="adhoc.1")
    service(sink).register("w.low", tier="low")
    with pytest.raises(WorkerIneligible):
        ctl.acquire(sink.get("tic-hi"), worker_id="w.low", takeover=True, reason="testing")
    assert ctl.active_attempt("tic-hi").id == first.attempt.id


def test_disable_blocks_new_work_but_keeps_history_and_running_attempts(sink, arbite_dir):
    add(sink, "tic-a1", tier="low")
    add(sink, "tic-a2", tier="low")
    svc = service(sink)
    svc.register("w1", tier="medium")
    ctl = make_lifecycle(sink, arbite_dir)
    running = ctl.acquire(sink.get("tic-a1"), worker_id="w1").attempt

    change = svc.disable("w1", reason="rotating out")
    assert change.stored.profile.enabled is False
    assert change.stored.profile.disabled_reason == "rotating out"
    assert ctl.active_attempt("tic-a1").id == running.id  # not revoked

    with pytest.raises(WorkerIneligible) as excinfo:
        ctl.acquire(sink.get("tic-a2"), worker_id="w1")
    assert excinfo.value.details["reasons"][0]["code"] == "worker_disabled"

    # The profile is retained and still resolves the worker id attempts refer to.
    assert svc.get("w1").profile.worker_id == running.worker_id
    assert [row.profile.worker_id for row in svc.list(state="disabled")] == ["w1"]
    assert svc.list(state="enabled") == []
    assert svc.disable("w1").changed is False

    svc.enable("w1", reason="back")
    assert ctl.acquire(sink.get("tic-a2"), worker_id="w1").attempt.worker_id == "w1"
    assert [e.event_kind for e in worker_events(sink)] == [
        "worker_registered", "worker_disabled", "worker_enabled",
    ]


def test_profiles_travel_in_export_bundles(sink, kind, tmp_path):
    service(sink).register("w1", tier="high", capabilities=["python"])
    bundle = x.export_coordination(sink)
    assert [p["worker_id"] for p in bundle["records"]["worker_profiles"]] == ["w1"]
    assert bundle["counts"]["worker_profiles"] == 1

    other = "sqlite" if kind == "file" else "file"
    target_dir = tmp_path / "target" / ".arbite"
    target_dir.mkdir(parents=True)
    target = make_sink(other, target_dir)
    x.import_coordination(target, bundle)
    assert workers.WorkerProfileService(target.coordination()).get("w1").profile.tier == "high"

    # A bundle written before profiles existed (no group, no worker events) verifies.
    legacy = json.loads(json.dumps(bundle))
    del legacy["records"]["worker_profiles"]
    legacy["events"] = [e for e in legacy["events"] if e["category"] != "worker"]
    assert x.bundle_problems(legacy) == []


# --- the CLI -----------------------------------------------------------------


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


def test_cli_worker_lifecycle_and_eligibility(cli):
    cli("worker", "list", expect=2)
    reg = json.loads(cli(
        "worker", "register", "claude.a", "--tier", "medium", "--provider", "anthropic",
        "--capability", "python", "--cost-class", "paid", "--cost-amount", "2",
        "--cost-unit", "USD/ticket", "--cost-provenance", "guess", "--json",
    ).stdout)
    assert reg["ok"] and reg["data"]["profile"]["revision"] == 1
    assert reg["data"]["profile"]["liveness"] == "unverified"

    dup = json.loads(cli("worker", "register", "claude.a", "--tier", "low", "--json", expect=1).stdout)
    assert dup["code"] == "conflict"
    secret = json.loads(cli(
        "worker", "register", "x", "--tier", "low", "--model", "sk-ant-api03-abcdefghijklmnop",
        "--json", expect=1,
    ).stdout)
    assert secret["code"] == "invalid_record"
    assert "sk-ant" not in secret["message"]

    high = created_id(cli("create", "--title", "hard", "--type", "bug", "--tier", "high", "--domain", "py"))
    med = created_id(cli("create", "--title", "easy", "--type", "bug", "--tier", "medium", "--domain", "py"))

    elevated = cli("list", "next", "--tier", "high", "--claim", "claude.a", expect=1)
    assert "exceeds the registered profile" in elevated.stderr
    refused = cli("claim", high, "--agent", "claude.a", expect=1)
    assert "below the required tier" in refused.stderr

    picked = cli("list", "next", "--claim", "claude.a", "--json")
    assert [t["id"] for t in json.loads(picked.stdout)] == [med]
    assert "skipped 1" in picked.stderr

    cli("claim", high, "--agent", "adhoc.worker")  # ad-hoc ids are unaffected

    check = json.loads(cli(
        "worker", "check", "claude.a", "--require-capability", "rust", "--local-only", "--json",
    ).stdout)["data"]
    assert check["eligible"] is False
    assert [r["code"] for r in check["reasons"]] == ["capability_missing", "locality_unknown"]

    cli("worker", "disable", "claude.a", "--reason", "done")
    shown = json.loads(cli("worker", "show", "claude.a", "--json").stdout)["data"]["profile"]
    assert shown["state"] == "disabled" and shown["revision"] == 2
    listed = json.loads(cli("worker", "list", "--state", "disabled", "--json").stdout)["data"]
    assert [w["worker_id"] for w in listed["workers"]] == ["claude.a"]
    human = cli("worker", "checkin", "claude.a").stdout
    assert "not verified liveness" in human

    missing = json.loads(cli("worker", "show", "nobody", "--json", expect=1).stdout)
    assert missing["code"] == "not_found"
