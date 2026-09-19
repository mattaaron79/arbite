"""CLI surface for coordination export, migration, rebind and the doctor (C11).

These run the real CLI in a subprocess, in a throwaway project, for both shipped
sink kinds: the contract being protected is the one an agent or shell script
sees -- argv, exit codes, stdout/stderr and `--json` payloads.

Coordination state is seeded through the real coordination service (the same
`application.coordination_service_for` the CLI builds), so the workspace and its
`StoreBinding`/marker are genuine; corruption for the doctor's report-only
findings is injected with direct storage writes, never through the public API.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from arbite import application, coordination as c, coordination_export as x
from arbite.application import Actor
from arbite.sinks import SQLITE_FILENAME, SinkSpec, build_sink

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

SINKS = ("file", "sqlite")
PROBLEM_KEYS = {"kind", "detail", "id", "path", "fixed"}
OK_RESULT_KEYS = {
    "ok",
    "code",
    "message",
    "data",
    "details",
    "retryable",
    "bytes_may_have_changed",
    "schema_version",
}


def other_kind(kind: str) -> str:
    return "sqlite" if kind == "file" else "file"


# ---------------------------------------------------------------------------
# Subprocess runner (same conventions as tests/test_cli.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_project):
    def run(*args, expect=0, sink=None, env=None):
        environment = dict(os.environ, PYTHONPATH=str(SRC_DIR))
        environment.pop("ARBITE_SINK", None)
        if sink:
            environment["ARBITE_SINK"] = sink
        if env:
            environment.update(env)
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


@pytest.fixture(params=SINKS)
def kind(request) -> str:
    return request.param


def ticket_id(output: str) -> str:
    match = re.search(r"(tic-[0-9a-f]{4})", output)
    assert match, f"no ticket id in output: {output}"
    return match.group(1)


def create(cli, title="A ticket", sink=None) -> str:
    args = ["create", "--title", title, "--type", "bug", "--tier", "medium", "--domain", "mesh"]
    if sink:
        args += ["--sink", sink]
    return ticket_id(cli(*args).stdout)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def project_sink(project_root: Path, kind: str):
    return build_sink(SinkSpec(kind=kind), project_root / ".arbite")


def seed_coordination(
    project_root: Path,
    kind: str,
    *,
    active: bool = False,
    with_artifact: bool = False,
    orphan_claim: bool = False,
):
    """Write real coordination work into `kind`'s store; return the pieces.

    A finished attempt (the default) leaves the store *quiescent*; `active=True`
    writes a live attempt, which is what migration/rebind must refuse.
    """
    sink = project_sink(project_root, kind)
    service = application.coordination_service_for(
        sink, root=str(project_root), actor=Actor("tester")
    )
    store = sink.coordination()
    workspace = service.workspace
    now = c.utc_now()

    attempt = c.WorkAttempt(
        id=c.new_record_id("work_attempt"),
        ticket_id="tic-a1b2",
        worker_id="tester",
        workspace_id=workspace.id,
        generation=1,
        started=now,
        last_activity=now,
        state="active" if active else "finished",
        ended=None if active else now,
        outcome=None if active else "ok",
    )

    records = [attempt]
    if with_artifact or orphan_claim:
        descriptor = store.store_artifact_bytes(b"hello evidence", media_type="text/plain")
        artifact = c.Artifact(
            id=descriptor.id,
            digest=descriptor.digest,
            size=descriptor.size,
            created=descriptor.created,
            location=descriptor.location,
            media_type=descriptor.media_type,
        )
        receipt = c.OperationReceipt(
            id=c.new_record_id("operation_receipt"),
            attempt_id=attempt.id,
            ticket_id="tic-a1b2",
            actor="tester",
            kind_="write",
            timestamp=now,
            before={"src/a.py": c.digest_of_bytes(b"old")},
            after={"src/a.py": c.digest_of_bytes(b"new")},
            paths=["src/a.py"],
            artifact_refs=[artifact.id],
            claim_generation=1,
        )
        records.extend([artifact, receipt])

    claim = None
    if orphan_claim:
        claim = c.FileClaim(
            id=c.new_record_id("file_claim"),
            workspace_id="ws-0000000000000000",
            path="src/orphan.py",
            ticket_id="tic-a1b2",
            attempt_id="att-0000000000000000",
            generation=1,
            acquired=now,
            observed_version=c.digest_of_bytes(b"old"),
            state="active",
            released=None,
        )
        records.append(claim)

    event = c.Event(
        id=c.new_record_id("event"),
        cursor=None,
        kind_="attempt_finished" if not active else "attempt_started",
        category="lifecycle",
        timestamp=now,
        subject_ids=[attempt.id, "tic-a1b2"],
        payload={"workspace_id": workspace.id, "generation": 1},
    )
    with store.transaction() as tx:
        for record in records:
            tx.put(record)
        tx.append_event(event)

    return {
        "sink": sink,
        "store": store,
        "workspace": workspace,
        "attempt": attempt,
        "claim": claim,
        "event": event,
    }


def orphan_claim_in(project_root: Path, kind: str):
    """Put one orphan claim (no matching attempt) directly into `kind`'s store."""
    sink = project_sink(project_root, kind)
    store = sink.coordination()
    now = c.utc_now()
    claim = c.FileClaim(
        id=c.new_record_id("file_claim"),
        workspace_id="ws-0000000000000000",
        path="src/orphan.py",
        ticket_id="tic-a1b2",
        attempt_id="att-0000000000000000",
        generation=1,
        acquired=now,
        observed_version=c.digest_of_bytes(b"old"),
        state="active",
        released=None,
    )
    with store.transaction() as tx:
        tx.put(claim)
    return claim


def read_marker(project_root: Path) -> dict:
    return json.loads((project_root / ".arbite" / "workspace-binding.json").read_text())


def config_bytes(project_root: Path) -> bytes:
    path = project_root / "arbite.yaml"
    return path.read_bytes() if path.exists() else b""


def export_of(project_root: Path, kind: str, workspace_id=None) -> dict:
    return x.export_coordination(
        project_sink(project_root, kind), workspace_id=workspace_id
    )


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_coordination_stdout_and_out(cli, tmp_project, kind):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    state = seed_coordination(tmp_project, kind, with_artifact=True)
    ws = state["workspace"].id

    # stdout (no --out, no --json) is the document itself.
    document = json.loads(cli("export", "--scope", "coordination", sink=kind).stdout)
    assert document["schema_version"] == x.EXPORT_VERSION
    assert document["retained_history"] is True
    assert document["cursor_namespace"]
    assert document["counts"]["work_attempts"] == 1

    # --out writes a bundle that reads back.
    target = tmp_project / "bundle.json"
    summary = cli(
        "export", "--scope", "coordination", "--out", str(target), sink=kind
    ).stdout
    assert "wrote coordination export" in summary
    assert "retained_history True" in summary
    bundle = x.read_bundle(target)
    assert bundle["retained_history"] is True
    assert bundle["cursor_namespace"] == document["cursor_namespace"]
    assert bundle["counts"]["work_attempts"] == 1

    # --json payload shape and counts (documented, stable).
    payload = json.loads(
        cli("export", "--scope", "coordination", "--json", sink=kind).stdout
    )
    assert set(payload) == OK_RESULT_KEYS
    assert payload["ok"] is True
    assert payload["schema_version"] == c.CONTRACT_VERSION
    data = payload["data"]
    assert data["scope"] == "coordination"
    assert data["retained_history"] is True
    assert data["cursor_namespace"] == document["cursor_namespace"]
    assert data["out"] is None
    assert data["counts"]["work_attempts"] == 1
    assert data["counts"]["artifacts"] == 1

    # --no-artifacts keeps the metadata but drops the bytes.
    lean = json.loads(
        cli(
            "export", "--scope", "coordination", "--no-artifacts", "--json", sink=kind
        ).stdout
    )
    assert lean["data"]["counts"]["artifacts"] == 1
    lean_bundle = json.loads(
        cli("export", "--scope", "coordination", "--no-artifacts", sink=kind).stdout
    )
    assert lean_bundle["artifacts"]
    assert all("data_b64" not in entry for entry in lean_bundle["artifacts"])
    assert all(entry.get("data_omitted") is True for entry in lean_bundle["artifacts"])
    full_bundle = json.loads(cli("export", "--scope", "coordination", sink=kind).stdout)
    assert any("data_b64" in entry for entry in full_bundle["artifacts"])

    # --workspace filters.
    mine = json.loads(
        cli("export", "--scope", "coordination", "--workspace", ws, "--json", sink=kind).stdout
    )
    assert mine["data"]["workspace_id"] == ws
    assert mine["data"]["counts"]["work_attempts"] == 1
    empty = json.loads(
        cli(
            "export",
            "--scope",
            "coordination",
            "--workspace",
            "ws-0000000000000000",
            "--json",
            sink=kind,
        ).stdout
    )
    assert empty["data"]["counts"]["work_attempts"] == 0
    assert empty["data"]["counts"]["workspaces"] == 0


def test_export_all_and_tickets_documents(cli, tmp_project, kind):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    tid = create(cli, "exported", sink=kind)
    seed_coordination(tmp_project, kind)

    combined = json.loads(cli("export", "--scope", "all", sink=kind).stdout)
    assert combined["schema_version"] == 1
    assert combined["coordination"]["schema_version"] == x.EXPORT_VERSION
    assert combined["tickets"]["arbite_ticket_export"] == 1
    assert [t["id"] for t in combined["tickets"]["tickets"]] == [tid]
    assert combined["tickets"]["sink"]["kind"] == kind

    payload = json.loads(cli("export", "--scope", "all", "--json", sink=kind).stdout)
    assert payload["data"]["counts"]["tickets"] == 1
    assert payload["data"]["counts"]["coordination"]["work_attempts"] == 1

    tickets_only = json.loads(cli("export", "--scope", "tickets", sink=kind).stdout)
    assert tickets_only["arbite_ticket_export"] == 1
    assert [t["id"] for t in tickets_only["tickets"]] == [tid]
    assert "coordination" not in tickets_only

    tickets_payload = json.loads(
        cli("export", "--scope", "tickets", "--json", sink=kind).stdout
    )
    assert tickets_payload["data"]["counts"] == {"tickets": 1}
    assert tickets_payload["data"]["cursor_namespace"] is None


def test_export_of_an_uninitialised_store_is_empty_and_creates_nothing(
    cli, tmp_project, kind
):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    coordination_dir = tmp_project / ".arbite" / "coordination"
    marker = tmp_project / ".arbite" / "workspace-binding.json"

    payload = json.loads(cli("export", "--scope", "coordination", "--json", sink=kind).stdout)
    assert payload["ok"] is True
    assert payload["data"]["counts"]["work_attempts"] == 0
    assert payload["data"]["counts"]["events"] == 0
    assert payload["data"]["cursor_namespace"]

    if kind == "file":
        assert not coordination_dir.exists(), "export must not create the layout"
    assert not marker.exists(), "export must not bind or create a marker"

    bundle = json.loads(cli("export", "--scope", "coordination", sink=kind).stdout)
    assert bundle["records"] == {group: [] for group in x.RECORD_GROUPS}
    assert bundle["events"] == []


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


def test_migrate_transfers_tickets_and_coordination_then_rebinds(
    cli, tmp_project, kind
):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    tid = create(cli, "moving", sink=kind)
    state = seed_coordination(tmp_project, kind)
    ws = state["workspace"].id
    target = other_kind(kind)

    out = cli("migrate", "--to", target, sink=kind).stdout
    assert f"migrated 1 ticket(s) from {kind} to {target}" in out
    assert "transferred coordination history" in out
    assert "destination verified" in out

    # marker, stored StoreBinding and `sink:` all name the destination.
    assert read_marker(tmp_project)["sink_kind"] == target
    assert (tmp_project / "arbite.yaml").read_text().strip() == f"sink: {target}"
    destination_store = project_sink(tmp_project, target).coordination()
    binding = destination_store.store_binding(ws)
    assert binding is not None and binding.sink_kind == target

    moved = export_of(tmp_project, target, workspace_id=ws)
    assert moved["counts"]["work_attempts"] == 1
    assert moved["counts"]["events"] >= 1

    # the ticket itself moved
    assert tid in cli("list", "--json", "--tic", "tic-", sink=target).stdout

    # never prune source coordination state
    source = export_of(tmp_project, kind, workspace_id=ws)
    assert source["counts"]["work_attempts"] == 1


def test_migrate_dry_run_transfers_nothing(cli, tmp_project, kind):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    create(cli, "moving", sink=kind)
    seed_coordination(tmp_project, kind)
    target = other_kind(kind)
    marker_before = (tmp_project / ".arbite" / "workspace-binding.json").read_bytes()
    config_before = config_bytes(tmp_project)

    out = cli("migrate", "--to", target, "--dry-run", sink=kind).stdout
    assert "would migrate 1 ticket(s)" in out
    assert "would transfer coordination history" in out

    assert (tmp_project / ".arbite" / "workspace-binding.json").read_bytes() == marker_before
    assert config_bytes(tmp_project) == config_before
    if kind == "file":
        assert not (tmp_project / ".arbite" / "arbite.db").exists(), "--dry-run must not create the target"


def test_migrate_refuses_a_non_quiescent_source(cli, tmp_project, kind):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    create(cli, "busy", sink=kind)
    state = seed_coordination(tmp_project, kind, active=True)

    proc = cli("migrate", "--to", other_kind(kind), expect=1, sink=kind)
    assert "not quiescent" in proc.stderr
    assert state["attempt"].id in proc.stderr


def test_migrate_refusal_happens_before_any_destination_mutation(cli, tmp_project, kind):
    """The non-quiescent refusal runs *before* the ticket copy, so the destination
    is left exactly as it was: it is not even initialised, no source ticket lands
    in it, and both the marker and `sink:` still name the source."""
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    tid = create(cli, "busy", sink=kind)
    state = seed_coordination(tmp_project, kind, active=True)
    target = other_kind(kind)
    marker_path = tmp_project / ".arbite" / "workspace-binding.json"
    marker_before = marker_path.read_bytes()
    config_before = config_bytes(tmp_project)

    proc = cli("migrate", "--to", target, expect=1, sink=kind)
    assert "not quiescent" in proc.stderr
    assert state["attempt"].id in proc.stderr

    # The destination was never initialised or written: a refused migration must
    # not leave it partially populated.
    if target == "sqlite":
        assert not (tmp_project / ".arbite" / "arbite.db").exists(), (
            "a refused migration must not create the destination database"
        )
    else:
        assert not (tmp_project / ".arbite" / "open").exists(), (
            "a refused migration must not lay out the destination file sink"
        )
        assert tid not in {path.stem for path in (tmp_project / ".arbite").rglob("*.md")}

    assert marker_path.read_bytes() == marker_before
    assert read_marker(tmp_project)["sink_kind"] == kind
    assert config_bytes(tmp_project) == config_before

    # --dry-run performs the same refusal (and still writes nothing).
    dry = cli("migrate", "--to", target, "--dry-run", expect=1, sink=kind)
    assert "not quiescent" in dry.stderr
    assert marker_path.read_bytes() == marker_before
    assert config_bytes(tmp_project) == config_before


def test_migrate_no_coordination_leaves_coordination_in_the_source(cli, tmp_project, kind):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    create(cli, "moving", sink=kind)
    state = seed_coordination(tmp_project, kind)
    ws = state["workspace"].id
    target = other_kind(kind)

    out = cli("migrate", "--to", target, "--no-coordination", sink=kind).stdout
    assert "transferred coordination history" not in out
    assert "migrated 1 ticket(s)" in out

    assert export_of(tmp_project, kind, workspace_id=ws)["counts"]["work_attempts"] == 1
    assert export_of(tmp_project, target, workspace_id=ws)["counts"]["work_attempts"] == 0
    # No rebind happened: the marker still names the source store.
    assert read_marker(tmp_project)["sink_kind"] == kind


def test_migrate_ticket_only_behaviour_is_unchanged_when_coordination_is_absent(
    cli, tmp_project, kind
):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    create(cli, "plain", sink=kind)
    target = other_kind(kind)

    out = cli("migrate", "--to", target, sink=kind).stdout
    assert f"migrated 1 ticket(s) from {kind} to {target}" in out
    assert "coordination" not in out


# ---------------------------------------------------------------------------
# rebind
# ---------------------------------------------------------------------------


def test_rebind_switches_marker_binding_and_config(cli, tmp_project, kind):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    state = seed_coordination(tmp_project, kind)
    ws = state["workspace"].id
    target = other_kind(kind)

    payload = json.loads(cli("rebind", "--to", target, "--json", sink=kind).stdout)
    assert set(payload) == OK_RESULT_KEYS
    data = payload["data"]
    assert data["rebound"] is True
    assert data["dry_run"] is False
    assert data["verified"] is True
    assert data["workspace_id"] == ws
    assert data["from"]["kind"] == kind
    assert data["to"]["kind"] == target

    marker = read_marker(tmp_project)
    assert marker["sink_kind"] == target
    assert (tmp_project / "arbite.yaml").read_text().strip() == f"sink: {target}"
    binding = project_sink(tmp_project, target).coordination().store_binding(ws)
    assert binding is not None and binding.sink_kind == target


def test_rebind_refuses_when_the_current_store_is_busy(cli, tmp_project, kind):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    seed_coordination(tmp_project, kind, active=True)
    marker_before = (tmp_project / ".arbite" / "workspace-binding.json").read_bytes()
    config_before = config_bytes(tmp_project)

    proc = cli("rebind", "--to", other_kind(kind), expect=1, sink=kind)
    assert "rebind" in proc.stderr
    assert "active attempt" in proc.stderr
    assert (tmp_project / ".arbite" / "workspace-binding.json").read_bytes() == marker_before
    assert config_bytes(tmp_project) == config_before


def test_rebind_refuses_a_destination_with_coordination_problems(cli, tmp_project, kind):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    seed_coordination(tmp_project, kind)
    target = other_kind(kind)
    orphan_claim_in(tmp_project, target)
    marker_before = (tmp_project / ".arbite" / "workspace-binding.json").read_bytes()
    config_before = config_bytes(tmp_project)

    proc = cli("rebind", "--to", target, expect=1, sink=kind)
    assert "integrity problems" in proc.stderr
    assert "coordination_orphan_claim" in proc.stderr
    assert (tmp_project / ".arbite" / "workspace-binding.json").read_bytes() == marker_before
    assert config_bytes(tmp_project) == config_before


def test_rebind_dry_run_writes_nothing(cli, tmp_project, kind):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    seed_coordination(tmp_project, kind)
    target = other_kind(kind)
    marker_before = (tmp_project / ".arbite" / "workspace-binding.json").read_bytes()
    config_before = config_bytes(tmp_project)

    payload = json.loads(
        cli("rebind", "--to", target, "--dry-run", "--json", sink=kind).stdout
    )
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["rebound"] is False
    assert payload["data"]["verified"] is True

    assert (tmp_project / ".arbite" / "workspace-binding.json").read_bytes() == marker_before
    assert config_bytes(tmp_project) == config_before


def test_rebind_requires_a_destination(cli, tmp_project):
    cli("init")
    proc = cli("rebind", expect=1)
    assert "needs --to" in proc.stderr


# ---------------------------------------------------------------------------
# doctor CLI surface
# ---------------------------------------------------------------------------


def test_doctor_reports_and_does_not_fix_an_orphan_claim(cli, tmp_project, kind):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    claim = orphan_claim_in(tmp_project, kind)

    report = json.loads(cli("doctor", "--json", expect=3, sink=kind).stdout)
    coordination = [p for p in report["problems"] if p["kind"] == "coordination_orphan_claim"]
    assert coordination, report["problems"]
    assert any(p["id"] == claim.id or claim.id in p["detail"] for p in coordination)
    for problem in report["problems"]:
        assert set(problem) == PROBLEM_KEYS
    assert report["remaining"] >= 1

    # --fix repairs only the unambiguous findings; an orphan claim is report-only.
    fixed = json.loads(cli("doctor", "--json", "--fix", expect=3, sink=kind).stdout)
    assert any(
        p["kind"] == "coordination_orphan_claim" for p in fixed["problems"]
    )
    assert fixed["remaining"] >= 1


def _write_legacy_workspace_record(project_root: Path, kind: str, workspace_id: str) -> None:
    """Store a workspace as an unversioned (legacy) envelope, bypassing the API."""
    now = c.utc_now()
    record = {
        "kind": "workspace",
        "id": workspace_id,
        "root": str(project_root),
        "created": now,
        "updated": now,
    }
    payload = json.dumps({"record": record})
    if kind == "file":
        path = (
            project_root
            / ".arbite"
            / "coordination"
            / "records"
            / "workspace"
            / f"{workspace_id}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        return
    conn = sqlite3.connect(str(project_root / ".arbite" / SQLITE_FILENAME))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO coordination_records "
            "(kind, record_id, revision, payload) VALUES (?, ?, ?, ?)",
            ("workspace", workspace_id, 0, payload),
        )
        conn.commit()
    finally:
        conn.close()


def test_doctor_fixes_a_fixable_coordination_problem(cli, tmp_project, kind):
    cli("init", *(["--sink", kind] if kind == "sqlite" else []))
    state = seed_coordination(tmp_project, kind)
    ws = state["workspace"].id
    _write_legacy_workspace_record(tmp_project, kind, ws)

    report = json.loads(cli("doctor", "--json", expect=3, sink=kind).stdout)
    assert any(p["kind"] == "coordination_legacy_record" for p in report["problems"])

    fixed = json.loads(cli("doctor", "--json", "--fix", sink=kind).stdout)
    assert fixed["remaining"] == 0
    assert fixed["fixed"] >= 1

    clean = json.loads(cli("doctor", "--json", sink=kind).stdout)
    assert clean["problems"] == []
