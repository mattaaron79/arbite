"""A moved project: `doctor --fix` restamps the old workspace ids (tic-9969), and
`workspace reset` clears live coordination state (tic-cf80)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import examples
from arbite.coordination import records as coordination_records
from arbite.coordination import relocation
from arbite.coordination.store import open_coordination_store
from arbite.sinks import SinkSpec, build_sink

SINK_KINDS = ("file", "sqlite")
AGENT = "claude.opus.001"


def cli(project: Path, *args, expect: int = 0):
    proc = examples.run_cli(project, *args)
    assert proc.returncode == expect, (
        f"arbite {' '.join(args)} -> exit {proc.returncode}, expected {expect}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc


def coordination(project: Path):
    sink_kind = "sqlite" if (project / ".arbite" / "arbite.db").exists() else "file"
    return open_coordination_store(build_sink(SinkSpec(kind=sink_kind), project / ".arbite"))


def make_project(tmp_path: Path, sink_kind: str) -> Path:
    project = tmp_path / "before"
    (project / ".arbite").mkdir(parents=True)
    (project / ".arbite" / "project.yaml").write_text(f"sink: {sink_kind}\n", encoding="utf-8")
    cli(project, "init")
    return project


def claimed_work(project: Path) -> tuple:
    """A claimed ticket holding one file claim: `(ticket id, attempt id)`."""
    out = cli(
        project, "create", "--title", "work", "--type", "chore", "--tier", "low",
        "--domain", "io",
    ).stdout
    ticket = out.split()[1]
    cli(project, "claim", ticket, "--agent", AGENT)
    attempt = coordination(project).active_attempts(ticket)[0].id
    (project / "notes.txt").write_text("hello\n", encoding="utf-8")
    cli(project, "file", "claim", "notes.txt", "--ticket", ticket, "--attempt", attempt)
    return ticket, attempt


def doctor(project: Path, *args, expect: int) -> dict:
    return json.loads(cli(project, "doctor", "--json", *args, expect=expect).stdout)


def kinds(report: dict, fixed: bool = False) -> list:
    return sorted(p["kind"] for p in report["problems"] if p.get("fixed", False) == fixed)


@pytest.fixture(params=SINK_KINDS)
def sink_kind(request):
    return request.param


def test_a_moved_and_reinitialised_project_is_restamped_by_doctor_fix(tmp_path, sink_kind):
    project = make_project(tmp_path, sink_kind)
    ticket, attempt = claimed_work(project)
    old_id = coordination(project).get_workspace().id
    moved = tmp_path / "after"
    shutil.move(str(project), str(moved))
    cli(moved, "init")
    new_id = coordination(moved).get_workspace().id
    assert new_id != old_id

    report = doctor(moved, expect=3)
    assert kinds(report) == ["attempt_for_another_workspace", "claim_for_another_workspace"]
    assert all(old_id in p["detail"] for p in report["problems"])

    fixed = doctor(moved, "--fix", expect=0)
    assert kinds(fixed, fixed=True) == [
        "attempt_for_another_workspace",
        "claim_for_another_workspace",
    ]
    assert kinds(fixed) == []

    store = coordination(moved)
    assert {a.workspace_id for a in store.records("attempt")} == {new_id}
    (claim,) = store.active_claims()
    assert claim.workspace_id == new_id
    assert claim.id == coordination_records.claim_id_for(new_id, "notes.txt")
    assert claim.attempt_id == attempt
    doctor(moved, expect=0)
    # The restamped claim is the attempt's own: closing the ticket releases it.
    cli(moved, "close", ticket)
    assert coordination(moved).active_claims() == []


def test_a_moved_project_that_was_not_reinitialised_has_its_binding_re_recorded(
    tmp_path, sink_kind
):
    project = make_project(tmp_path, sink_kind)
    claimed_work(project)
    moved = tmp_path / "after"
    shutil.move(str(project), str(moved))

    report = doctor(moved, expect=3)
    assert "stale_workspace_binding" in kinds(report)

    doctor(moved, "--fix", expect=0)
    store = coordination(moved)
    workspace = store.get_workspace()
    assert workspace.root == str(moved)
    assert {a.workspace_id for a in store.records("attempt")} == {workspace.id}
    assert {c.workspace_id for c in store.records("claim")} == {workspace.id}
    doctor(moved, expect=0)


# ---------------------------------------------------------------------------
# Re-keying claims, store level
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, sink_kind):
    """A coordination store as `arbite init` leaves it, on each backend."""
    arbite_dir = tmp_path / ".arbite"
    arbite_dir.mkdir()
    sink = build_sink(SinkSpec(kind=sink_kind), arbite_dir)
    sink.init()
    store = open_coordination_store(sink)
    store.init()
    return store


def workspace_at(store, root: str):
    return coordination_records.Workspace(
        id=coordination_records.derived_workspace_id(root, store.kind, store.root),
        root=root,
        store_kind=store.kind,
        store_root=str(store.root),
        coordination_kind=store.kind,
        coordination_root=str(store.root),
    )


def claim(workspace_id: str, attempt_id: str, path: str = "a.txt", active: bool = True):
    now = coordination_records.utc_now()
    return coordination_records.FileClaim(
        id=coordination_records.claim_id_for(workspace_id, path),
        workspace_id=workspace_id,
        path=path,
        ticket_id="tic-cf9f",
        attempt_id=attempt_id,
        generation=1,
        acquired=now,
        state="active" if active else "released",
        released=None if active else now,
    )


def attempt(workspace_id: str, attempt_id: str, active: bool = True):
    now = coordination_records.utc_now()
    return coordination_records.WorkAttempt(
        id=attempt_id,
        ticket_id="tic-cf9f",
        worker_id=AGENT,
        workspace_id=workspace_id,
        generation=1,
        state="active" if active else "finished",
        started=now,
        last_activity=now,
        ended=None if active else now,
    )


def test_a_released_foreign_claim_is_dropped_when_the_path_has_a_current_record(store):
    old, new = workspace_at(store, "/old"), workspace_at(store, "/new")
    store.put_workspace(new)
    store.put_record(attempt(old.id, "att-0001", active=False))
    store.put_record(claim(old.id, "att-0001", active=False))
    store.put_record(attempt(new.id, "att-0002"))
    store.put_record(claim(new.id, "att-0002"))

    result = relocation.restamp(store, new)

    assert result.superseded == {old.id: 1}
    assert result.conflicts == []
    (only,) = store.records("claim")
    assert only.attempt_id == "att-0002"


def test_two_active_owners_of_one_path_are_left_alone_and_reset_settles_them(store):
    old, new = workspace_at(store, "/old"), workspace_at(store, "/new")
    store.put_workspace(new)
    store.put_record(attempt(old.id, "att-0001"))
    store.put_record(claim(old.id, "att-0001"))
    store.put_record(attempt(new.id, "att-0002"))
    store.put_record(claim(new.id, "att-0002"))

    result = relocation.restamp(store, new)
    assert [c.attempt_id for c in result.conflicts] == ["att-0001"]
    assert len(store.active_claims()) == 2

    reset = relocation.reset(store, new, actor=AGENT)

    assert {a.id for a in reset.attempts} == {"att-0001", "att-0002"}
    assert len(reset.claims) == 2
    assert store.active_claims() == []
    assert store.active_attempts() == []
    assert {c.workspace_id for c in store.records("claim")} == {new.id}
    assert {a.outcome for a in store.records("attempt")} == {relocation.RESET_OUTCOME}
    assert relocation.findings(store, new) == []


def test_workspace_reset_previews_without_force_and_clears_live_state_with_it(
    tmp_path, sink_kind
):
    project = make_project(tmp_path, sink_kind)
    ticket, attempt = claimed_work(project)

    preview = cli(project, "workspace", "reset", expect=1)
    assert f"end attempt {attempt}" in preview.stderr
    assert "nothing was changed" in preview.stderr
    assert len(coordination(project).active_claims()) == 1

    report = json.loads(
        cli(project, "workspace", "reset", "--force", "--agent", AGENT, "--json").stdout
    )
    assert report["attempts"][0]["id"] == attempt
    assert report["claims"] == ["notes.txt"]
    store = coordination(project)
    assert store.active_claims() == []
    assert store.active_attempts() == []
    (ended,) = store.records("attempt")
    assert (ended.state, ended.outcome) == ("interrupted", relocation.RESET_OUTCOME)
    # The ticket keeps its status; its worker re-attaches.
    assert json.loads(cli(project, "show", ticket, "--json").stdout)["status"] == "in_progress"
    cli(project, "attempt", "adopt", ticket, "--agent", AGENT)
    doctor(project, expect=0)


def test_an_ended_attempt_for_a_deleted_ticket_is_history_not_a_problem(store):
    workspace = workspace_at(store, "/here")
    store.put_workspace(workspace)
    store.put_record(attempt(workspace.id, "att-0001", active=False))

    assert store.record_problems(ticket_ids=["tic-other"]) == []
